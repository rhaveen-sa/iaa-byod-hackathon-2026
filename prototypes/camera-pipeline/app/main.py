"""Entrypoint.

The SDV runtime unpacks this project into app/ and runs `python -u app/main.py`,
so this filename is a contract - do not rename it. It also means app/ is on
sys.path as a plain directory, which is why every import here is top-level
rather than package-relative.

The bootstrap at the bottom is guarded by `if __name__ == "__main__"`. Running
as a script is unaffected; the guard exists so the tests can import this module
and exercise FrameFeed and the processing loop, which is where the interesting
failure modes live.

The loop below is the one the hackathon brief asks for:

    camera frame -> detection -> windowed event -> vehicle signal -> evidence

Each stage is its own module, so choosing the challenge (tools vs gas cylinders
vs animals) touches detector.py and config.py and nothing else.
"""

import asyncio
import logging
import queue
import signal
import threading

from vehicle import vehicle  # type: ignore
from velocitas_sdk.util.log import (  # type: ignore
    get_opentelemetry_log_factory,
    get_opentelemetry_log_format,
)
from velocitas_sdk.vehicle_app import VehicleApp  # type: ignore

import camera
import config
import detector
import rules
from health import Health
from vehicle_io import VehicleIO

logging.setLogRecordFactory(get_opentelemetry_log_factory())
logging.basicConfig(format=get_opentelemetry_log_format())
logging.getLogger().setLevel("INFO")
logger = logging.getLogger(__name__)

# How long a blocked queue read waits before checking for shutdown. Without a
# timeout the executor thread blocks forever on a non-daemon pool thread, which
# hangs interpreter exit - the process then needs SIGKILL.
_QUEUE_POLL_SECONDS = 1.0


class FrameFeed:
    """Bridges the blocking camera stream into the asyncio world.

    `requests` streaming blocks, and blocking the event loop would stall every
    vehicle signal call. So the camera runs on its own thread and hands frames
    over through a size-1 queue that always keeps the NEWEST frame: reacting to
    a stale frame is worse than skipping one.
    """

    def __init__(self, cam=None):
        self._stop = threading.Event()
        self.cam = cam or camera.MjpegCamera(stop_event=self._stop)
        self._queue = queue.Queue(maxsize=1)
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def _run(self):
        try:
            for frame in self.cam.frames():
                if self._stop.is_set():
                    return
                if self._queue.full():
                    try:
                        self._queue.get_nowait()  # drop the stale frame
                    except queue.Empty:
                        pass
                self._queue.put(frame)
        except Exception:
            logger.exception("camera thread stopped")

    async def next_frame(self):
        """Wait for a frame. Returns None if none arrived before shutdown.

        The bounded get is what makes this interruptible; see _QUEUE_POLL_SECONDS.
        """
        while not self._stop.is_set():
            try:
                return await asyncio.to_thread(self._queue.get, True, _QUEUE_POLL_SECONDS)
            except queue.Empty:
                continue
        return None

    def stop(self):
        self._stop.set()


class ByodApp(VehicleApp):
    """Watches the BYOD camera and makes the vehicle react to what it sees."""

    def __init__(self, vehicle_client, feed=None):
        super().__init__()
        self.vehicle_io = VehicleIO(vehicle_client)
        self.detector = detector.build()
        self.rule = rules.build()
        self.feed = feed or FrameFeed()
        self.health = Health(stale_after=config.STALE_FRAME_SECONDS)
        self._frames_seen = 0
        self._tasks = set()  # keep strong refs; CPython only weak-refs tasks

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def on_start(self):
        """Called by the SDK once the databroker connection is up."""
        logger.info(
            "BYOD app starting - detector=%s rule=%s camera=%s",
            self.detector.kind, self.rule.kind, config.CAM["video_feed"],
        )
        self.feed.start()
        self._spawn(self._loop())
        self._spawn(self._watchdog())

    async def _watchdog(self):
        """Says something when the camera goes quiet.

        Without this a dead camera is completely silent at the app level: the
        loop simply blocks and the frame heartbeat never advances.
        """
        while True:
            await asyncio.sleep(config.STALE_FRAME_SECONDS)
            if self.health.frame_age() is None and self._frames_seen == 0:
                logger.warning("no frames yet after %ds - %s",
                               config.STALE_FRAME_SECONDS, self.health.summary())
            elif self.health.is_stale():
                self.health.camera(False)
                logger.warning("camera stale - %s", self.health.summary())
            await self.vehicle_io.set_status(self.health.status_code())

    async def _loop(self):
        while True:
            try:
                frame = await self.feed.next_frame()
                if frame is None:
                    return  # shutting down
                self._frames_seen += 1
                self.health.camera(True)

                result = await asyncio.to_thread(self.detector.detect, frame)
                self.health.detector(result.ok, result.reason)

                event = self.rule.evaluate(result)

                # A quiet heartbeat, so you can see the loop is alive in the
                # playground terminal without drowning it in per-frame logs.
                if self._frames_seen % 20 == 0:
                    logger.info("frame %d - %d detection(s) - %s",
                                self._frames_seen, len(result), self.health.summary())

                if event is not None:
                    await self._handle_event(event, frame)

            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad frame must never end the demo.
                logger.exception("error in processing loop; continuing")
                await asyncio.sleep(1)

    async def _handle_event(self, event, frame):
        logger.warning("EVENT %s: %s %s", event.kind, event.message, event.payload)

        # The vehicle reacts FIRST. Evidence upload is an optional artifact over
        # a network we may not even be able to reach; it must never sit between
        # the event and the horn.
        self.health.alerting(True)
        await self.vehicle_io.set_status(self.health.status_code())
        try:
            await self.vehicle_io.alert()
        finally:
            self.health.alerting(False)
            await self.vehicle_io.set_status(self.health.status_code())

        if config.UPLOAD_EVIDENCE:
            self._spawn(asyncio.to_thread(camera.upload_snapshot, frame, f"{event.kind}.jpg"))

    async def on_stop(self):
        self.feed.stop()
        await self.vehicle_io.safe_off()


async def _main():
    logger.info("Starting ByodApp...")
    app = ByodApp(vehicle)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Do NOT use loop.stop() here. It abandons any in-flight alert, so a SIGTERM
    # mid-alert leaves the horn latched on and raises out of run_until_complete.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except NotImplementedError:
            pass  # not available on every platform

    runner = asyncio.create_task(app.run())
    stopper = asyncio.create_task(stopping.wait())
    try:
        await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        logger.info("shutting down")
        app.feed.stop()
        runner.cancel()
        stopper.cancel()
        # Release the horn before anything else can go wrong on the way out.
        try:
            await asyncio.wait_for(app.vehicle_io.safe_off(), timeout=5)
        except Exception:
            logger.exception("failed to release vehicle signals on shutdown")
        for task in (runner, stopper):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


if __name__ == "__main__":
    asyncio.run(_main())
