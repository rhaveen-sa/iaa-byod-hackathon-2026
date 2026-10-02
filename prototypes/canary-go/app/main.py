"""
Canary Go: load bay monitor for LPG transport.

Watches a vehicle load bay with two devices and drives the vehicle's own
actuators when something is wrong.

    gas sensor   ppm in the load bay        catches a leak
    camera       cylinder count/upright     catches a cylinder knocked over

Neither device detects what the other does, so both are needed.

Flow:
    arm      read vehicle identity, capture baseline (count, upright, ppm)
    monitor  compare each reading against the baseline
    respond  stationary: hazards + high beam + horn
             moving:     hazards + high beam, no horn
    disarm   clear all actuators

Both devices default to simulated. See the selector lines in each device
section to switch to live.

Runs on the digital.auto SDV runtime: python -u app/main.py
Standard library only.
"""

import asyncio
import random
import time

from vehicle import vehicle  # type: ignore

# The runtime exposes the Velocitas SDK under the older `sdv` namespace, that
# is what the organisers' own reference prototype imports. Newer images use
# `velocitas_sdk`. Try both so this runs on either.
try:
    from sdv.vehicle_app import VehicleApp  # type: ignore
except ImportError:  # pragma: no cover
    from velocitas_sdk.vehicle_app import VehicleApp  # type: ignore


def say(fmt="", *args):
    """print with flush, not logging.

    The Playground console can buffer or drop logging output. The reference
    prototype uses print+flush for the same reason.
    """
    print(fmt % args if args else fmt, flush=True)


# ===========================================================================
# CONFIGURATION
# ===========================================================================
# Actuators confirmed against the API catalogue of vehicle model
# "IAA Hannover Hackathon 2026" and the organisers' reference prototype.

# NOTE ON HOW THESE ARE WRITTEN
# -----------------------------
# Every signal below is referenced as a LITERAL attribute chain in VehicleIO -
# self.Vehicle.Body.Horn.IsActive, and never assembled from strings at
# runtime. That is not a style preference. The Playground populates the
# "Used APIs" panel by statically scanning the source for Vehicle.<path> text,
# and it hands that list to the runtime, which uses it to register the mock
# signals. Resolve the paths dynamically and the panel comes up empty, the
# signals are never registered, and the dashboard widgets sit there dead.
#
# Confirmed against the API catalogue of vehicle model
# "IAA Hannover Hackathon 2026" and the organisers' reference prototype:
#
#   Vehicle.Body.Horn.IsActive                    ACTUATOR
#   Vehicle.Body.Lights.Hazard.IsSignaling        ACTUATOR
#   Vehicle.Body.Lights.Beam.High.IsOn            ACTUATOR
#   Vehicle.Chassis.ParkingBrake.IsEngaged        ACTUATOR

# Camera endpoints, from the organisers' reference prototype. Swap CAM_HOST to
# change cameras.
#   /video_feed      MJPEG stream, multipart/x-mixed-replace, boundary=frame
#   /view            snapshot page
#   /upload_snapshot POST target for annotated frames
#
# routine-splurge-recast was returning ngrok 3004 then 3200 (offline) when last
# checked. domain-reenter-boxer was serving 640x480 and is the fallback.
CAM_HOST = "https://routine-splurge-recast.ngrok-free.dev"
# CAM_HOST = "https://domain-reenter-boxer.ngrok-free.dev"

CAM_STREAM = CAM_HOST + "/video_feed"
CAM_UPLOAD = CAM_HOST + "/upload_snapshot"

CAM_CLASSES = ["gas cylinder", "cylinder", "bottle", "canister"]
CAM_CONFIDENCE = 0.15        # the reference uses 0.15; small objects need it low
UPRIGHT_RATIO = 1.3          # box height / width above this counts as standing

# ppm thresholds. Propane's lower explosive limit is around 21,000 ppm, so
# these sit far below it on purpose: react while the number is still boring.
# Retune to whatever the Bosch unit actually reports.
PPM_ELEVATED = 400
PPM_ALARM = 1000

# Hysteresis. A sensor that flickers across a threshold and honks each time
# trains the driver to ignore it, which is worse than no alarm at all.
CONFIRM_READINGS = 3
CLEAR_FACTOR = 0.8

SAMPLE_SECONDS = 1.0
HORN_PULSE = 0.6
DEMO_PAUSE = 2.5        # room to narrate between beats

# If the parking brake cannot be read, assume PARKED, that is the response
# that brings someone back to the load bay, so it is the safe way to be wrong.
# It is also what you want at a booth, where the van is parked by definition.
ASSUME_PARKED_IF_UNKNOWN = True

# Publish the device's own readings back into the vehicle's namespace, so a
# dashboard widget can show them. These two are WISHLIST signals added to the
# model, they carry no declared type, so the databroker may refuse to store
# them. Publishing is therefore best-effort: it is tried once per loop, a
# rejection is reported once, and the demo carries on regardless. Set to False
# if the noise is not worth it.
#
#   Vehicle.Cargo.LeakageDetectionSignal   <- gas reading, ppm
#   Vehicle.GasLeakage                     <- True while the alarm stands
PUBLISH_CARGO_SIGNALS = True

SAFE, ELEVATED, ALARM = "SAFE", "ELEVATED", "ALARM"
DISARMED, ARMED = "DISARMED", "ARMED"

# The kit has its own identity, because it outlives any one vehicle. This is
# the serial on the side of the box the fleet hands out with the van.
KIT_ID = "CNY-014"

# The hire record. A rental fleet knows the plate, the model and the
# registration date from its own booking system, not from the vehicle bus, so
# this is where that detail legitimately comes from. The app still asks the
# vehicle first and prefers whatever the vehicle actually answers; these are
# used for the fields the vehicle model leaves empty, which on this runtime is
# all of them. The output says which source each field came from.
HIRE = {
    "plate": "B-XY 1234",
    "brand": "Ford",
    "model": "Transit",
    "registered": "2013-03-18",
}


# ===========================================================================
# DEVICE 1, THE GAS SENSOR
# ===========================================================================

class GasSensor:
    """Gas sensor interface. One method, so live and simulated are swappable."""

    name = "gas sensor"
    simulated = False

    def read_ppm(self):
        """Return ppm, or None if the sensor could not be read.

        None means "no measurement", not "zero". The rule holds its state on
        None rather than treating it as a clean reading.
        """
        raise NotImplementedError


class HttpGasSensor(GasSensor):
    """Reads a sensor over HTTP. Expects JSON like {"ppm": 412}."""

    name = "Bosch gas sensor (LIVE)"

    def __init__(self, url, field="ppm", timeout=3):
        self.url = url
        self.field = field
        self.timeout = timeout

    def read_ppm(self):
        import json
        import urllib.request

        try:
            req = urllib.request.Request(
                self.url, headers={"ngrok-skip-browser-warning": "true"}
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return float(payload[self.field])
        except Exception as exc:
            say("  [gas] sensor unreadable: %s", exc)
            return None


class SimulatedGasSensor(GasSensor):
    """Scripted ppm profile for when no sensor is connected.

    Reports itself as SIMULATED so output is never mistaken for live data.
    """

    name = "gas sensor (SIMULATED)"
    simulated = True

    def __init__(self):
        # Background, then a valve starts weeping, then it is found and closed.
        self.profile = (
            [12, 15, 11, 14, 13, 12, 14, 13]          # quiet
            + [90, 260, 430, 610, 780]                # something has opened
            + [1150, 1480, 1720, 1900, 1850]          # alarm territory
            + [900, 420, 180, 60, 20, 14]             # closed, venting
        )
        self.index = 0

    def read_ppm(self):
        if self.index >= len(self.profile):
            return self.profile[-1]
        value = self.profile[self.index]
        self.index += 1
        # Real sensors are noisy. A perfectly smooth ramp looks fake because
        # it is fake.
        return max(0.0, value + random.uniform(-4, 4))


# --- choose the device -------------------------------------------------------
# Live:  SENSOR = HttpGasSensor("https://<endpoint the mentors give you>/gas")
SENSOR = SimulatedGasSensor()


# ===========================================================================
# DEVICE 2, THE CAMERA
# ===========================================================================

class LoadCamera:
    """Camera interface. Subclasses implement observe(); check() is shared.

    observe() returns {"seen": n, "upright": n}, or None if nothing has been
    understood yet. None means "cannot see", not "nothing there", so check()
    holds on it instead of alarming.
    """

    name = "load bay camera"
    simulated = False

    def observe(self):
        raise NotImplementedError

    def script(self, *events):
        """Queue scripted observations. Only meaningful for the simulator."""

    def check(self, baseline):
        """Compare with what was captured at arming. Returns (ok, description)."""
        obs = self.observe()
        if obs is None:
            return True, "no frame understood yet, holding"
        if obs["seen"] < baseline["seen"]:
            return False, "%d of %d cylinders missing" % (
                baseline["seen"] - obs["seen"], baseline["seen"])
        if obs["upright"] < baseline["upright"]:
            down = baseline["upright"] - obs["upright"]
            return False, "%d cylinder%s no longer upright" % (
                down, "" if down == 1 else "s")
        return True, "%d cylinders, upright, as loaded" % obs["seen"]


class SimulatedLoadCamera(LoadCamera):
    """Scripted camera observations, for when no camera is connected."""

    name = "load bay camera (SIMULATED)"
    simulated = True

    def __init__(self):
        self.expected = 4
        self.upright = 4
        self._script = []

    def script(self, *events):
        """Queue observations for the demo, e.g. a cylinder going over."""
        self._script = list(events)
        if not events:
            self.upright = self.expected  # re-secured: back to a clean load

    def observe(self):
        if self._script:
            self.upright = self._script.pop(0)
        return {"seen": self.expected, "upright": self.upright}


class LiveLoadCamera(LoadCamera):
    """Pulls MJPEG from CAM_STREAM and counts cylinders with YOLO-World.

    Upright vs toppled comes from the bounding box aspect ratio: height/width
    above UPRIGHT_RATIO counts as standing. No second model needed.

    Inference runs on its own thread because the stream read blocks. The main
    loop reads only the last result, so a slow model cannot stall the response.
    """

    name = "load bay camera (LIVE)"
    simulated = False

    def __init__(self):
        import threading

        import cv2  # noqa: F401
        import numpy  # noqa: F401
        from ultralytics import YOLO

        say("  [camera] loading YOLO-World, this takes a moment")
        self.model = YOLO("yolov8s-worldv2.pt")
        self.model.set_classes(CAM_CLASSES)
        say("  [camera] ready, looking for %s", ", ".join(CAM_CLASSES))

        self._lock = threading.Lock()
        self._latest = None          # None until the first frame is understood
        self._frame = None
        threading.Thread(target=self._worker, name="camera", daemon=True).start()

    def _worker(self):
        import cv2
        import numpy as np
        import requests

        headers = {"ngrok-skip-browser-warning": "true"}
        while True:
            try:
                response = requests.get(CAM_STREAM, headers=headers,
                                        stream=True, timeout=10)
                if response.status_code != 200:
                    time.sleep(2)
                    continue

                buffer = bytearray()  # bytes += copies the whole buffer every chunk
                for chunk in response.iter_content(chunk_size=4096):
                    buffer.extend(chunk)
                    # rfind, not find: take the NEWEST complete frame and throw
                    # away anything queued behind it. Reacting to a stale frame
                    # is worse than skipping one.
                    start = buffer.rfind(b"\xff\xd8")
                    end = buffer.rfind(b"\xff\xd9")
                    if start == -1 or end == -1 or end <= start:
                        continue
                    jpeg = bytes(buffer[start:end + 2])
                    del buffer[:end + 2]

                    frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8),
                                         cv2.IMREAD_COLOR)
                    if frame is None:
                        continue

                    results = self.model.predict(frame, conf=CAM_CONFIDENCE,
                                                 verbose=False)
                    seen = upright = 0
                    for box in results[0].boxes:
                        x1, y1, x2, y2 = (float(v) for v in box.xyxy[0].tolist())
                        width, height = max(1.0, x2 - x1), max(1.0, y2 - y1)
                        seen += 1
                        if height / width >= UPRIGHT_RATIO:
                            upright += 1

                    with self._lock:
                        self._latest = {"seen": seen, "upright": upright}
                        self._frame = frame
            except Exception as exc:
                say("  [camera] stream dropped: %s. Reconnecting.", exc)
                time.sleep(2)

    def observe(self):
        """Last understood view of the bay.

        Returns None if nothing has been read yet, which means "cannot see",
        not "nothing there". check() below turns that into a hold rather than
        an alarm.
        """
        with self._lock:
            return dict(self._latest) if self._latest else None


# --- choose the camera -------------------------------------------------------
# Live:  CAMERA = LiveLoadCamera()
#        Needs requests, numpy, opencv-python-headless and ultralytics
#        uncommented in app/requirements.txt, and something cylinder-shaped
#        actually in front of the lens.
CAMERA = SimulatedLoadCamera()


# ===========================================================================
# THE RULE
# ===========================================================================

class LeakMonitor:
    """Maps ppm readings to SAFE/ELEVATED/ALARM with hysteresis."""

    def __init__(self):
        self.level = SAFE
        self._candidate = SAFE
        self._streak = 0
        self._unreadable = 0

    @staticmethod
    def classify(ppm):
        if ppm >= PPM_ALARM:
            return ALARM
        if ppm >= PPM_ELEVATED:
            return ELEVATED
        return SAFE

    def update(self, ppm):
        """Feed one reading. Returns the new level if it changed, else None."""
        if ppm is None:
            # Could not measure. Hold, and say so, silence here would be the
            # dangerous failure, not the safe one.
            self._unreadable += 1
            if self._unreadable in (1, 5) or self._unreadable % 20 == 0:
                say("  [gas] no reading, %d in a row. Holding at %s.",
                    self._unreadable, self.level)
            return None
        self._unreadable = 0

        proposed = self.classify(ppm)

        # Standing down needs a clearly lower reading, not one that has dipped
        # a single ppm under the line.
        if proposed == SAFE and self.level in (ELEVATED, ALARM):
            if ppm > PPM_ELEVATED * CLEAR_FACTOR:
                proposed = ELEVATED

        if proposed == self._candidate:
            self._streak += 1
        else:
            self._candidate = proposed
            self._streak = 1

        if self._candidate != self.level and self._streak >= CONFIRM_READINGS:
            self.level = self._candidate
            return self.level
        return None


# ===========================================================================
# THE VEHICLE
# ===========================================================================

class VehicleIO:
    """Vehicle signal access, one named method per signal.

    Literal attribute chains, not string paths: the Playground builds its Used
    APIs list by scanning the source text, and that list registers the mock
    signals.
    """

    def __init__(self, vehicle_client):
        self.Vehicle = vehicle_client
        self._complained = set()

    async def _guard(self, label, make_coro):
        """Never let a signal failure take the demo down with it.

        Takes a CALLABLE, not a coroutine. That matters: writing
        `self._guard(label, self.Vehicle.Cargo.X.set(v))` evaluates the
        attribute chain before _guard is entered, so a signal missing from the
        vehicle model raises AttributeError outside this try and kills the run.
        Deferring it behind a lambda keeps the literal chain in the source, so
        the Playground's Used APIs scan still finds it, while letting us catch
        a model that does not have the signal.
        """
        try:
            await make_coro()
            return True
        except AttributeError:
            if label not in self._complained:
                say("  [vehicle] %s not in this vehicle model, skipping", label)
                self._complained.add(label)
            return False
        except Exception as exc:
            if label not in self._complained:
                say("  [vehicle] could not set %s: %s", label, exc)
                self._complained.add(label)
            return False

    async def set_hazards(self, on):
        return await self._guard(
            "Vehicle.Body.Lights.Hazard.IsSignaling",
            lambda: self.Vehicle.Body.Lights.Hazard.IsSignaling.set(on))

    async def set_horn(self, on):
        return await self._guard(
            "Vehicle.Body.Horn.IsActive",
            lambda: self.Vehicle.Body.Horn.IsActive.set(on))

    async def set_high_beam(self, on):
        return await self._guard(
            "Vehicle.Body.Lights.Beam.High.IsOn",
            lambda: self.Vehicle.Body.Lights.Beam.High.IsOn.set(on))

    async def identify(self):
        """Read vehicle identity, preferring the vehicle's own values.

        This runtime returns nothing for these attributes, so HIRE fills the
        gaps. The result records which source was used.
        """
        async def attr(make_node, key):
            try:
                value = (await make_node().get()).value
                if value not in (None, ""):
                    return value, "vehicle"
            except Exception:
                pass
            return HIRE.get(key, ""), "hire record"

        plate, source = await attr(
            lambda: self.Vehicle.VehicleIdentification.LicensePlate, "plate")
        brand, _ = await attr(
            lambda: self.Vehicle.VehicleIdentification.Brand, "brand")
        model, _ = await attr(
            lambda: self.Vehicle.VehicleIdentification.Model, "model")
        registered, _ = await attr(
            lambda: self.Vehicle.VehicleIdentification.DateVehicleFirstRegistered,
            "registered")
        return {"plate": plate, "brand": brand, "model": model,
                "registered": registered, "source": source}

    async def is_stationary(self):
        """True when the vehicle is not moving.

        Tries Vehicle.IsMoving, falls back to the parking brake, then to
        ASSUME_PARKED_IF_UNKNOWN.
        """
        try:
            moving = (await self.Vehicle.IsMoving.get()).value
            if moving is not None:
                return not bool(moving)
        except Exception:
            pass
        return await self.is_parked()

    async def is_parked(self):
        """True if the parking brake is engaged.

        If the signal cannot be read we return ASSUME_PARKED_IF_UNKNOWN. That
        is the response which brings someone back to the load bay, so it is the
        safe way to be wrong, and at a booth the van is parked by definition.
        """
        try:
            value = (await self.Vehicle.Chassis.ParkingBrake.IsEngaged.get()).value
        except Exception:
            return ASSUME_PARKED_IF_UNKNOWN
        return ASSUME_PARKED_IF_UNKNOWN if value is None else bool(value)

    async def publish_cargo(self, ppm, leaking):
        """Push the device's readings into the vehicle's own signal namespace.

        This is the BYOD idea taken to its conclusion: the gas sensor does not
        just talk to our app, it publishes a vehicle signal that any other
        application, or a dashboard widget, can subscribe to. The device
        becomes part of the vehicle's API rather than a side channel.

        Both targets are wishlist signals with no declared type, so treat
        failure as expected, not exceptional.
        """
        if not PUBLISH_CARGO_SIGNALS:
            return
        await self._guard(
            "Vehicle.Cargo.LeakageDetectionSignal",
            lambda: self.Vehicle.Cargo.LeakageDetectionSignal.set(int(ppm)))
        await self._guard(
            "Vehicle.GasLeakage",
            lambda: self.Vehicle.GasLeakage.set(bool(leaking)))

    async def all_off(self):
        await self.set_horn(False)
        await self.set_hazards(False)
        await self.set_high_beam(False)


# ===========================================================================
# THE APP
# ===========================================================================

class Canary(VehicleApp):

    def __init__(self, vehicle_client):
        super().__init__()
        self.io = VehicleIO(vehicle_client)
        self.gas = SENSOR
        self.camera = CAMERA
        self.monitor = LeakMonitor()
        self.state = DISARMED
        self.baseline = None

    # --- arming ------------------------------------------------------------

    async def arm(self, background_ppm):
        """Bind the kit to the vehicle and capture the baseline."""
        vehicle_id = await self.io.identify()
        obs = self.camera.observe()
        if obs is None:
            # Live camera has not understood a frame yet. Arm against an empty
            # baseline rather than crash; check() will hold until it can see.
            say("  [camera] no frame yet, arming with an empty load baseline")
            obs = {"seen": 0, "upright": 0}
        self.baseline = {
            "seen": obs["seen"],
            "upright": obs["upright"],
            "ppm": background_ppm,
            "at": time.strftime("%H:%M:%S"),
            "vehicle": vehicle_id,
        }
        self.state = ARMED

        name = " ".join(x for x in (vehicle_id["brand"], vehicle_id["model"]) if x)
        say("  Kit %s bound to %s%s   [%s]",
            KIT_ID, vehicle_id["plate"], (", " + name) if name else "",
            vehicle_id["source"])
        if vehicle_id["registered"]:
            say("  First registered %s", vehicle_id["registered"])
        say("  Baseline: %d cylinders, %d upright, %d ppm",
            self.baseline["seen"], self.baseline["upright"], int(self.baseline["ppm"]))
        say("  Monitoring both devices against this baseline.")

    async def disarm(self):
        self.state = DISARMED
        await self.io.all_off()
        say("  All actuators cleared. Kit unbound.")

    # --- responses ---------------------------------------------------------

    async def respond(self, severity, headline, parked):
        """Drive the actuators for a severity level.

        The response differs stationary vs moving: no horn while moving.
        """
        if severity == SAFE:
            say("  -> STAND DOWN. Load bay back to baseline.")
            await self.io.all_off()
            return

        if severity == ELEVATED:
            say("  -> HAZARDS ON. %s", headline)
            await self.io.set_hazards(True)
            return

        if parked:
            say("  -> ALARM, stationary. Hazards + high beam + horn.")
            await self.io.set_hazards(True)
            await self.io.set_high_beam(True)
            for _ in range(3):
                await self.io.set_horn(True)
                await asyncio.sleep(HORN_PULSE)
                await self.io.set_horn(False)
                await asyncio.sleep(HORN_PULSE / 2)
        else:
            say("  -> ALARM, moving. Hazards + high beam. No horn while moving.")
            await self.io.set_hazards(True)
            await self.io.set_high_beam(True)

    async def parked(self):
        return await self.io.is_stationary()

    # --- the demo ----------------------------------------------------------

    async def on_start(self):
        say()
        say("=" * 60)
        say("  CANARY GO   portable load bay monitor      kit %s" % KIT_ID)
        say("  devices: %s | %s", self.gas.name, self.camera.name)
        if self.gas.simulated or self.camera.simulated:
            say("  Readings below are SIMULATED. No hardware attached.")
        say("=" * 60)

        await self.io.all_off()

        # ---------------- loading ----------------------------------------
        say()
        say("LOADING")
        say("  Four LPG cylinders loaded. State: %s.", self.state)
        await asyncio.sleep(DEMO_PAUSE)

        # ---------------- arm --------------------------------------------
        say()
        say("ARM   bind kit to vehicle")
        await self.arm(background_ppm=13)
        await asyncio.sleep(DEMO_PAUSE)

        # ---------------- event 1: the camera earns its place -------------
        say()
        say("EVENT 1   a cylinder shifts")
        self.camera.script(4, 3, 3, 3)
        for _ in range(2):
            ok, description = self.camera.check(self.baseline)
            ppm = self.gas.read_ppm()
            say("  camera   %s", description)
            if ppm is None:
                say("  gas      no reading")
            else:
                say("  gas      %d ppm, nothing to report", int(ppm))
            if not ok:
                say("  Gas reading is normal. Nothing has vented.")
                say()
                await self.respond(ELEVATED, description, await self.parked())
                break
            say()
            await asyncio.sleep(SAMPLE_SECONDS)
        await asyncio.sleep(DEMO_PAUSE)

        # ---------------- event 2: the gas sensor earns its place ---------
        say()
        say("EVENT 2   gas leak")
        say("  Cylinder re-secured. Camera back to baseline.")

        # Cylinder was re-secured, so the camera returns to a clean load.
        # Without this reset it keeps reporting event 1's fallen cylinder.
        self.camera.script()

        # Probe the cargo signals once so any "not in model" notice prints
        # above the table rather than inside it.
        await self.io.publish_cargo(self.baseline["ppm"], False)

        say()
        say("     ppm   state      camera   moving")
        say("  " + "-" * 40)

        readings = 0
        announced = False
        settled = 0
        while readings < len(self.gas.profile) + 4:
            ppm = self.gas.read_ppm()
            readings += 1
            stationary = await self.parked()
            changed = self.monitor.update(ppm)
            cam_ok, _ = self.camera.check(self.baseline)

            # A live sensor returns None when it cannot be read. Show the gap
            # in the table instead of crashing on int(None).
            if ppm is None:
                mark, shown = "?", " ---"
            else:
                mark = " " if self.monitor.classify(ppm) == SAFE else "!"
                shown = "%4d" % int(ppm)
            say("  %s %s   %-9s  %-6s   %s",
                mark, shown, self.monitor.level,
                "ok" if cam_ok else "ALERT", "no" if stationary else "yes")

            if ppm is not None:
                await self.io.publish_cargo(ppm, self.monitor.level == ALARM)

            if changed:
                say()
                await self.respond(changed, "gas in the load bay", stationary)
                say()

            if changed == ALARM and not announced:
                announced = True
                say("  Camera still reports the load as correct. It cannot")
                say("  detect gas. Only the gas sensor sees this.")
                say()

            if self.monitor.level == SAFE and readings > 10:
                settled += 1
                if settled >= 2:
                    break
            await asyncio.sleep(SAMPLE_SECONDS)

        # ---------------- close -------------------------------------------
        say()
        say("DISARM")
        say("  Leak cleared, bay vented.")
        await self.disarm()
        say()
        say("=" * 60)
        say("  Run complete. 2 events: 1 detected by camera, 1 by gas sensor.")
        say("=" * 60)


async def main():
    await Canary(vehicle).run()


if __name__ == "__main__":
    import signal

    LOOP = asyncio.get_event_loop()
    try:
        LOOP.add_signal_handler(signal.SIGTERM, LOOP.stop)
    except NotImplementedError:
        pass
    try:
        LOOP.run_until_complete(main())
    finally:
        LOOP.close()
