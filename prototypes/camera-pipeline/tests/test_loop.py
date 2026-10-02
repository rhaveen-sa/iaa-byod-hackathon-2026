"""Tests for the BYOD app.

Written after a review found the previous suite green on code that was broken,
so the bar here is: every test must FAIL if its fix is reverted. In particular

  - MJPEG tests assert byte-for-byte equality with the input JPEGs, not merely
    that output starts with SOI and ends with EOI (which is tautological given
    how a marker-scanning parser slices).
  - The fake datapoint really suspends, so cancellation tests cannot pass
    vacuously.
  - The rolling-window tests interleave flicker into the clearing run, which is
    the exact case that used to disarm the rule forever.

Run from the repo root:  python3 tests/test_loop.py
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest_stubs  # noqa: E402

conftest_stubs.install()

import camera  # noqa: E402
import config  # noqa: E402
import detector  # noqa: E402
import health  # noqa: E402
import main  # noqa: E402
import rules  # noqa: E402
import vehicle_io  # noqa: E402
from conftest_stubs import VEHICLE_TREE, FakeNode  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def plain_jpeg(payload=b"PAYLOAD" * 8):
    return b"\xff\xd8\xff\xe0\x00\x10JFIF" + payload + b"\xff\xd9"


def jpeg_with_embedded_eoi():
    """A JPEG carrying a complete JPEG inside an APP1/EXIF segment.

    Real cameras emit these constantly - an EXIF thumbnail IS a whole JPEG, so
    the outer file contains FF D8 ... FF D9 ... FF D9. A parser that stops at
    the first EOI truncates it.
    """
    thumbnail = b"\xff\xd8\xff\xdb THUMBNAIL DATA \xff\xd9"
    return (
        b"\xff\xd8"                      # outer SOI
        b"\xff\xe1" + len(thumbnail).to_bytes(2, "big") + thumbnail
        + b"\xff\xda MAIN IMAGE SCAN DATA "
        + b"\xff\xd9"                    # outer EOI
    )


def multipart_stream(frames, boundary=b"frameboundary", with_length=True, close=True):
    out = bytearray()
    for frame in frames:
        out += b"--" + boundary + b"\r\n"
        out += b"Content-Type: image/jpeg\r\n"
        if with_length:
            out += b"Content-Length: %d\r\n" % len(frame)
        out += b"\r\n" + frame + b"\r\n"
    if close:
        out += b"--" + boundary + b"--\r\n"   # RFC 2046 close-delimiter
    return bytes(out)


def fake_response(body, content_type, chunk_size=7):
    response = mock.MagicMock()
    response.headers = {"Content-Type": content_type}
    response.iter_content.return_value = [
        body[i : i + chunk_size] for i in range(0, len(body), chunk_size)
    ]
    response.__enter__ = lambda self: self
    response.__exit__ = lambda self, *a: False
    return response


def collect(cam, response):
    """Drain one pass of the stream.

    Calls _stream_once rather than frames(), because frames() swallows the
    end-of-stream CameraError and sleeps before reconnecting - fine in
    production, useless for asserting on what one response produced.
    """
    with mock.patch.object(camera.requests, "get", return_value=response):
        out = []
        try:
            for frame in cam._stream_once(8_000_000):
                out.append(frame)
                if len(out) > 50:
                    break
        except camera.CameraError:
            pass
        return out


def ok(n, label="object", box=(0.1, 0.4, 0.2, 0.6)):
    return detector.DetectionResult(
        [detector.Detection(label, 0.9, box) for _ in range(n)]
    )


# --------------------------------------------------------------------------
# MJPEG framing
# --------------------------------------------------------------------------

class MultipartFraming(unittest.TestCase):
    def setUp(self):
        self.cam = camera.MjpegCamera(url="http://test/feed", sample_interval=0)

    def test_frames_round_trip_byte_for_byte(self):
        frames = [plain_jpeg(b"A" * 40), plain_jpeg(b"B" * 60)]
        body = multipart_stream(frames)
        got = collect(self.cam, fake_response(body, "multipart/x-mixed-replace; boundary=frameboundary"))
        self.assertEqual(got, frames)

    def test_embedded_eoi_is_not_truncated(self):
        """The regression. Marker scanning returns 21 of 64 bytes here."""
        frame = jpeg_with_embedded_eoi()
        body = multipart_stream([frame])
        got = collect(self.cam, fake_response(body, "multipart/x-mixed-replace; boundary=frameboundary"))
        self.assertEqual(got, [frame])
        self.assertIn(b"MAIN IMAGE SCAN DATA", got[0])

    def test_works_without_content_length(self):
        frames = [plain_jpeg(b"C" * 30), plain_jpeg(b"D" * 30)]
        body = multipart_stream(frames, with_length=False)
        got = collect(self.cam, fake_response(body, "multipart/x-mixed-replace; boundary=frameboundary"))
        self.assertEqual(got, frames)

    def test_quoted_boundary_is_parsed(self):
        frames = [plain_jpeg(b"E" * 20)]
        body = multipart_stream(frames, boundary=b"xyz")
        got = collect(self.cam, fake_response(body, 'multipart/x-mixed-replace; boundary="xyz"'))
        self.assertEqual(got, frames)

    def test_sampling_drops_frames_but_keeps_them_intact(self):
        cam = camera.MjpegCamera(url="http://test/feed", sample_interval=999)
        frames = [plain_jpeg(b"F" * 20), plain_jpeg(b"G" * 20), plain_jpeg(b"H" * 20)]
        body = multipart_stream(frames)
        got = collect(cam, fake_response(body, "multipart/x-mixed-replace; boundary=frameboundary"))
        self.assertEqual(got, [frames[0]])

    def test_falls_back_to_markers_without_a_boundary(self):
        frames = [plain_jpeg(b"I" * 20), plain_jpeg(b"J" * 20)]
        got = collect(self.cam, fake_response(b"".join(frames), "image/jpeg"))
        self.assertEqual(got, frames)

    def test_oversized_part_is_rejected_rather_than_buffered_forever(self):
        cam = camera.MjpegCamera(url="http://test/feed", sample_interval=0)
        response = fake_response(b"--frameboundary\r\n\r\n" + b"x" * 5000,
                                 "multipart/x-mixed-replace; boundary=frameboundary")
        with mock.patch.object(camera.requests, "get", return_value=response):
            with self.assertRaises(camera.CameraError):
                list(cam._stream_once(max_buffer=1024))

    def test_upload_failure_is_not_fatal(self):
        with mock.patch.object(camera.requests, "post",
                               side_effect=camera.requests.RequestException("down")):
            self.assertFalse(camera.upload_snapshot(plain_jpeg()))


# --------------------------------------------------------------------------
# rolling-window rule
# --------------------------------------------------------------------------

class RollingWindow(unittest.TestCase):
    def rule(self, **kw):
        opts = {"window": 8, "fire_ratio": 0.6, "clear_ratio": 0.25, "cooldown_seconds": 0}
        opts.update(kw)
        return rules.CountMismatch(4, **opts)

    def test_does_not_judge_on_a_partial_window(self):
        rule = self.rule()
        for _ in range(7):
            self.assertIsNone(rule.evaluate(ok(0)))
        self.assertIsNotNone(rule.evaluate(ok(0)), "fires once the window is full")

    def test_occasional_flicker_never_fires(self):
        rule = self.rule()
        fired = []
        for i in range(80):
            fired.append(rule.evaluate(ok(3 if i % 8 == 0 else 4)))
        self.assertTrue(all(f is None for f in fired),
                        "1-in-8 flicker is 12.5% bad, well under fire_ratio")

    def test_fires_once_per_situation(self):
        rule = self.rule()
        fires = [rule.evaluate(ok(3)) for _ in range(40)]
        self.assertEqual(sum(f is not None for f in fires), 1)

    def test_rearms_even_when_clearing_run_contains_flicker(self):
        """The regression that killed the old rule.

        Consecutive-miss counting needed a perfectly clean run to re-arm, so a
        single bad frame inside the clearing window disarmed it forever.
        """
        rule = self.rule()
        self.assertTrue(any(rule.evaluate(ok(3)) for _ in range(12)), "initial fire")

        # Flicker MORE often than the window is long: 1 bad frame in every 5,
        # with window=8. That is 20% bad - under clear_ratio, so a rolling
        # window re-arms - but there is never a clean run of 8, so consecutive
        # counting stays latched forever. That gap is the bug.
        for i in range(60):
            rule.evaluate(ok(3 if i % 5 == 0 else 4))
        self.assertFalse(rule._active, "should have re-armed despite the flicker")

        refired = [rule.evaluate(ok(0)) for _ in range(12)]
        self.assertTrue(any(f is not None for f in refired),
                        "a real, sustained event after flicker must still fire")

    def test_cooldown_suppresses_rather_than_defers(self):
        clock = [0.0]
        rule = self.rule(cooldown_seconds=30, clock=lambda: clock[0])
        self.assertTrue(any(rule.evaluate(ok(3)) for _ in range(12)), "first fire")
        for _ in range(12):
            rule.evaluate(ok(4))          # clear
        clock[0] = 10.0
        for _ in range(12):
            rule.evaluate(ok(3))          # bad again, inside cooldown -> suppressed
        clock[0] = 31.0                   # cooldown now expired, situation ongoing
        late = [rule.evaluate(ok(3)) for _ in range(12)]
        self.assertTrue(all(f is None for f in late),
                        "an expiring cooldown must not fire mid-situation")

    def test_no_usable_observation_holds_state(self):
        """A detector outage must not be evidence of anything."""
        rule = self.rule()
        for _ in range(4):
            rule.evaluate(ok(4))
        before = list(rule._frames)
        fired = [rule.evaluate(detector.DetectionResult.unavailable("service down"))
                 for _ in range(50)]
        self.assertTrue(all(f is None for f in fired), "outage must never fire the rule")
        self.assertEqual(list(rule._frames), before, "window must not advance on no-data")

    def test_outage_alone_cannot_trigger_an_alert(self):
        rule = self.rule()
        outage = detector.DetectionResult.unavailable("down")
        self.assertTrue(all(rule.evaluate(outage) is None for _ in range(200)))

    def test_bare_list_still_accepted(self):
        rule = self.rule()
        fired = [rule.evaluate([]) for _ in range(12)]
        self.assertTrue(any(f is not None for f in fired))

    def test_payload_reports_the_gap(self):
        rule = self.rule()
        events = [e for e in (rule.evaluate(ok(2)) for _ in range(12)) if e]
        self.assertEqual(events[0].payload, {"expected": 4, "seen": 2, "missing": 2})

    def test_rejects_incoherent_thresholds(self):
        with self.assertRaises(ValueError):
            rules.CountMismatch(4, fire_ratio=0.2, clear_ratio=0.8)

    def test_zone_breach(self):
        rule = rules.ZoneBreach((0.6, 0.0, 1.0, 1.0), window=4, fire_ratio=0.75,
                                clear_ratio=0.25, cooldown_seconds=0)
        outside = ok(1, box=(0.05, 0.4, 0.15, 0.6))
        inside = ok(1, box=(0.8, 0.4, 0.9, 0.6))
        for _ in range(4):
            self.assertIsNone(rule.evaluate(outside))
        fired = [rule.evaluate(inside) for _ in range(4)]
        self.assertTrue(any(f is not None for f in fired))

    def test_shipped_defaults_do_not_self_breach(self):
        """The stub detector's boxes must sit outside the default zone."""
        cfg = dict(config.RULE, kind="zone_breach")
        rule = rules.build(cfg)
        stub = detector.StubDetector(expected=4)
        fired = [rule.evaluate(stub.detect(plain_jpeg())) for _ in range(30)]
        self.assertTrue(all(f is None for f in fired))


# --------------------------------------------------------------------------
# vehicle signals
# --------------------------------------------------------------------------

class VehicleSignals(unittest.TestCase):
    def io(self, tree=None, fail_paths=()):
        log = []
        return vehicle_io.VehicleIO(FakeNode(tree or VEHICLE_TREE, log, fail_paths=fail_paths)), log

    def test_alert_sets_then_clears_both_signals(self):
        io, log = self.io()
        asyncio.run(io.alert(duration=0))
        self.assertEqual(log, [
            ("Body.Lights.Beam.Low.IsOn", True),
            ("Body.Horn.IsActive", True),
            ("Body.Horn.IsActive", False),
            ("Body.Lights.Beam.Low.IsOn", False),
        ])

    def test_horn_released_when_alert_is_cancelled(self):
        io, log = self.io()

        async def run():
            task = asyncio.create_task(io.alert(duration=10))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run())
        self.assertIn(("Body.Horn.IsActive", False), log)

    def test_safe_off_is_idempotent_and_standalone(self):
        io, log = self.io()
        asyncio.run(io.safe_off())
        self.assertEqual(log, [("Body.Horn.IsActive", False),
                               ("Body.Lights.Beam.Low.IsOn", False)])

    def test_missing_path_is_logged_once(self):
        io, _ = self.io(tree={"Body": {}})
        with mock.patch.object(vehicle_io.logger, "error") as logged:
            self.assertFalse(asyncio.run(io.set_signal("Body.Lights.Beam.Low.IsOn", True)))
            asyncio.run(io.set_signal("Body.Lights.Beam.Low.IsOn", True))
        self.assertEqual(logged.call_count, 1)

    def test_branch_path_is_reported_as_a_branch(self):
        io, _ = self.io()
        with mock.patch.object(vehicle_io.logger, "error") as logged:
            self.assertFalse(asyncio.run(io.set_signal("Body.Lights", True)))
        self.assertIn("branch", logged.call_args[0][0])

    def test_databroker_rejection_is_throttled(self):
        """The likelier runtime failure: the path exists, the write fails."""
        io, _ = self.io(fail_paths={"Body.Horn.IsActive"})
        with mock.patch.object(vehicle_io.logger, "warning") as logged:
            for _ in range(10):
                self.assertFalse(asyncio.run(io.set_signal("Body.Horn.IsActive", True)))
        self.assertEqual(logged.call_count, 1, "must not log once per frame")

    def test_status_signal_is_off_by_default(self):
        io, log = self.io()
        self.assertFalse(asyncio.run(io.set_status(health.StatusCode.DEGRADED)))
        self.assertEqual(log, [])

    def test_resolve_error_names_the_full_path(self):
        with self.assertRaises(AttributeError) as ctx:
            vehicle_io.resolve(FakeNode({"Body": {}}, []), "Body.Horn.IsActive")
        self.assertIn("Body.Horn.IsActive", str(ctx.exception))


# --------------------------------------------------------------------------
# the loop itself (previously untested by construction)
# --------------------------------------------------------------------------

class FakeCamera:
    """A camera that PACES its frames.

    Pacing matters: FrameFeed keeps a size-1 queue and deliberately drops stale
    frames, so a fake that dumps its whole list instantly delivers exactly one
    frame to the consumer. That is correct production behaviour and a useless
    test. `interval` mimics a real feed arriving frame by frame.
    """

    def __init__(self, frames, stop_event=None, block_forever=False, interval=0.01):
        self._frames = frames
        self.stop_event = stop_event
        self.block_forever = block_forever
        self.interval = interval

    def _stopped(self):
        return self.stop_event is not None and self.stop_event.is_set()

    def frames(self):
        import time

        for frame in self._frames:
            if self._stopped():
                return
            yield frame
            time.sleep(self.interval)
        while self.block_forever and not self._stopped():
            time.sleep(0.01)


class LoopBehaviour(unittest.TestCase):
    def app(self, frames, det, rule, block_forever=False):
        log = []
        feed = main.FrameFeed(cam=None)
        feed.cam = FakeCamera(frames, feed._stop, block_forever)
        with mock.patch.object(main.detector, "build", return_value=det), \
             mock.patch.object(main.rules, "build", return_value=rule):
            app = main.ByodApp(FakeNode(VEHICLE_TREE, log), feed=feed)
        return app, log

    def test_event_reaches_the_vehicle(self):
        det = detector.StubDetector(expected=1, drop_every=1e9, drop_for=1e9)  # always short
        rule = rules.CountMismatch(4, window=4, fire_ratio=0.75, clear_ratio=0.25,
                                   cooldown_seconds=0)
        app, log = self.app([plain_jpeg()] * 20, det, rule, block_forever=True)

        async def run():
            with mock.patch.object(config, "ALERT_DURATION", 0), \
                 mock.patch.object(config, "UPLOAD_EVIDENCE", False):
                await app.on_start()
                await asyncio.sleep(0.4)
                await app.on_stop()

        asyncio.run(run())
        self.assertIn(("Body.Horn.IsActive", True), log, "the loop must reach the horn")
        self.assertEqual(log[-1][1], False, "and must leave it off")

    def test_vehicle_reacts_before_evidence_is_uploaded(self):
        """Evidence must never sit between the event and the horn."""
        order = []
        det = detector.StubDetector(expected=1, drop_every=1e9, drop_for=1e9)
        rule = rules.CountMismatch(4, window=4, fire_ratio=0.75, clear_ratio=0.25,
                                   cooldown_seconds=0)
        app, _ = self.app([plain_jpeg()] * 20, det, rule, block_forever=True)

        async def fake_alert(duration=None):
            order.append("alert")

        def fake_upload(*a, **k):
            order.append("upload")
            return True

        async def run():
            with mock.patch.object(app.vehicle_io, "alert", fake_alert), \
                 mock.patch.object(main.camera, "upload_snapshot", fake_upload), \
                 mock.patch.object(config, "UPLOAD_EVIDENCE", True):
                await app.on_start()
                await asyncio.sleep(0.4)
                await app.on_stop()

        asyncio.run(run())
        self.assertIn("alert", order)
        self.assertLess(order.index("alert"), order.index("upload"))

    def test_next_frame_is_interruptible(self):
        """The shutdown hang: an unbounded queue.get wedges a pool thread."""
        feed = main.FrameFeed(cam=None)
        feed.cam = FakeCamera([], feed._stop, block_forever=True)

        async def run():
            feed.start()
            task = asyncio.create_task(feed.next_frame())
            await asyncio.sleep(0.05)
            feed.stop()
            return await asyncio.wait_for(task, timeout=5)

        self.assertIsNone(asyncio.run(run()), "must return None on shutdown, not hang")

    def test_loop_exits_when_the_feed_stops(self):
        det = detector.StubDetector(expected=4)
        rule = rules.CountMismatch(4, window=8, cooldown_seconds=0)
        app, _ = self.app([], det, rule, block_forever=True)

        async def run():
            app.feed.start()
            task = asyncio.create_task(app._loop())
            await asyncio.sleep(0.05)
            app.feed.stop()
            await asyncio.wait_for(task, timeout=5)

        asyncio.run(run())  # must not time out

    def test_one_bad_frame_does_not_kill_the_loop(self):
        class Exploding:
            kind = "boom"
            calls = 0

            def detect(self, frame):
                Exploding.calls += 1
                if Exploding.calls == 1:
                    raise RuntimeError("bad frame")
                return ok(4)

        rule = rules.CountMismatch(4, window=8, cooldown_seconds=0)
        app, _ = self.app([plain_jpeg()] * 10, Exploding(), rule, block_forever=True)

        async def run():
            app.feed.start()
            task = asyncio.create_task(app._loop())
            await asyncio.sleep(2.0)   # the loop's error path sleeps 1s by design
            app.feed.stop()
            await asyncio.wait_for(task, timeout=5)

        asyncio.run(run())
        self.assertGreater(Exploding.calls, 1, "loop must continue past the failure")


# --------------------------------------------------------------------------
# detectors and health
# --------------------------------------------------------------------------

class Detectors(unittest.TestCase):
    def test_stub_uses_an_injected_clock(self):
        now = [0.0]
        stub = detector.StubDetector(expected=4, drop_every=10, drop_for=4,
                                     clock=lambda: now[0])
        self.assertEqual(len(stub.detect(plain_jpeg())), 3)
        now[0] = 5.0
        self.assertEqual(len(stub.detect(plain_jpeg())), 4)

    def test_stub_boxes_are_normalised(self):
        for det in detector.StubDetector().detect(plain_jpeg()).detections:
            for coord in det.box:
                self.assertTrue(0.0 <= coord <= 1.0)

    def test_build_does_not_depend_on_live_config(self):
        self.assertIsInstance(detector.build({"kind": "stub"}), detector.StubDetector)
        self.assertIsInstance(
            detector.build({"kind": "remote", "remote_url": "http://x"}),
            detector.RemoteDetector)
        with self.assertRaises(ValueError):
            detector.build({"kind": "remote", "remote_url": ""})

    def test_outage_reports_unavailable_not_empty(self):
        det = detector.RemoteDetector("http://x")
        with mock.patch.object(detector.requests, "post",
                               side_effect=detector.requests.RequestException("down")):
            result = det.detect(plain_jpeg())
        self.assertFalse(result.ok)
        self.assertEqual(result.detections, [])

    def _remote(self, payload):
        det = detector.RemoteDetector("http://x", min_confidence=0.5)
        response = mock.MagicMock()
        response.json.return_value = payload
        with mock.patch.object(detector.requests, "post", return_value=response):
            return det.detect(plain_jpeg())

    def test_filters_by_confidence(self):
        result = self._remote({"detections": [
            {"label": "cylinder", "confidence": 0.9, "box": [0, 0, 1, 1]},
            {"label": "cylinder", "confidence": 0.2, "box": [0, 0, 1, 1]},
        ]})
        self.assertTrue(result.ok)
        self.assertEqual(len(result), 1)

    def test_malformed_payloads_never_raise(self):
        for payload in ({"detections": None},
                        {"detections": "nope"},
                        ["not", "an", "object"],
                        {"detections": [{"confidence": "high"}]},
                        {"detections": [None, 3]}):
            with self.subTest(payload=payload):
                result = self._remote(payload)
                self.assertIsInstance(result, detector.DetectionResult)

    def test_short_box_is_padded_not_crashed(self):
        result = self._remote({"detections": [
            {"label": "x", "confidence": 0.9, "box": [0.1, 0.2]}]})
        self.assertEqual(len(result.detections[0].box), 4)
        rule = rules.ZoneBreach((0.0, 0.0, 1.0, 1.0), window=1, fire_ratio=1.0,
                                clear_ratio=0.5, cooldown_seconds=0)
        rule.evaluate(result)  # must not raise


class HealthState(unittest.TestCase):
    def test_transitions_are_logged_once_each(self):
        state = health.Health(stale_after=1)
        with mock.patch.object(health.logger, "warning") as logged:
            state.camera(True)          # first observation: silent
            for _ in range(5):
                state.camera(False)     # one OFFLINE
            for _ in range(5):
                state.camera(True)      # one RECOVERED
        self.assertEqual(logged.call_count, 2)

    def test_status_codes(self):
        state = health.Health(stale_after=1000)
        state.camera(True)
        state.detector(True)
        self.assertEqual(state.status_code(), health.StatusCode.OK)
        state.detector(False, "down")
        self.assertEqual(state.status_code(), health.StatusCode.DEGRADED)
        state.alerting(True)
        self.assertEqual(state.status_code(), health.StatusCode.ALERTING)


if __name__ == "__main__":
    unittest.main(verbosity=2)
