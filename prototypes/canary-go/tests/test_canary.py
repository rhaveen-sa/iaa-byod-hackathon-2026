"""Tests for Canary Go.

Run from this folder:  python3 tests/test_canary.py
"""

import asyncio
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest_runtime  # noqa: E402

conftest_runtime.install()

import main  # noqa: E402
from conftest_runtime import VEHICLE_TREE, FakeNode  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class LeakMonitorTests(unittest.TestCase):
    def feed(self, monitor, readings):
        return [monitor.update(r) for r in readings]

    def test_classify_thresholds(self):
        self.assertEqual(main.LeakMonitor.classify(10), main.SAFE)
        self.assertEqual(main.LeakMonitor.classify(main.PPM_ELEVATED), main.ELEVATED)
        self.assertEqual(main.LeakMonitor.classify(main.PPM_ALARM), main.ALARM)

    def test_single_spike_does_not_change_level(self):
        m = main.LeakMonitor()
        self.feed(m, [10, 2000, 10, 10])
        self.assertEqual(m.level, main.SAFE)

    def test_sustained_reading_changes_level_once(self):
        m = main.LeakMonitor()
        changes = [c for c in self.feed(m, [2000] * 6) if c]
        self.assertEqual(changes, [main.ALARM])

    def test_standing_down_needs_a_clearly_lower_reading(self):
        m = main.LeakMonitor()
        self.feed(m, [2000] * 4)
        self.feed(m, [main.PPM_ELEVATED - 1] * 6)   # under the line, inside hysteresis
        self.assertNotEqual(m.level, main.SAFE)
        self.feed(m, [10] * 6)
        self.assertEqual(m.level, main.SAFE)

    def test_unreadable_sensor_holds_state(self):
        m = main.LeakMonitor()
        self.feed(m, [2000] * 4)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(all(c is None for c in self.feed(m, [None] * 50)))
        self.assertEqual(m.level, main.ALARM)


class CameraTests(unittest.TestCase):
    baseline = {"seen": 4, "upright": 4}

    def test_clean_load_is_ok(self):
        ok, _ = main.SimulatedLoadCamera().check(self.baseline)
        self.assertTrue(ok)

    def test_fallen_cylinder_is_reported(self):
        cam = main.SimulatedLoadCamera()
        cam.script(3)
        ok, text = cam.check(self.baseline)
        self.assertFalse(ok)
        self.assertIn("no longer upright", text)

    def test_missing_cylinder_is_reported(self):
        cam = main.SimulatedLoadCamera()
        cam.expected = 3
        ok, text = cam.check(self.baseline)
        self.assertFalse(ok)
        self.assertIn("missing", text)

    def test_empty_script_restores_a_clean_load(self):
        cam = main.SimulatedLoadCamera()
        cam.script(2, 2)
        cam.observe()
        cam.script()
        self.assertTrue(cam.check(self.baseline)[0])

    def test_no_frame_yet_holds_instead_of_alarming(self):
        class Blind(main.LoadCamera):
            def observe(self):
                return None

        self.assertTrue(Blind().check(self.baseline)[0])

    def test_base_script_is_a_noop_so_live_camera_cannot_crash_the_demo(self):
        class Blind(main.LoadCamera):
            def observe(self):
                return None

        Blind().script(4, 3)


class GasSensorTests(unittest.TestCase):
    def test_simulated_profile_reaches_alarm_and_recovers(self):
        s = main.SimulatedGasSensor()
        values = [s.read_ppm() for _ in s.profile]
        self.assertGreaterEqual(max(values), main.PPM_ALARM)
        self.assertLess(values[-1], main.PPM_ELEVATED)

    def test_http_sensor_reports_none_not_zero_on_failure(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(main.HttpGasSensor("http://x").read_ppm())


class VehicleTests(unittest.TestCase):
    def io(self, tree=None):
        log = []
        return main.VehicleIO(FakeNode(tree or VEHICLE_TREE, log)), log

    def test_all_off_clears_every_actuator(self):
        vio, log = self.io()
        run(vio.all_off())
        self.assertEqual({p for p, v in log if v is False}, {
            "Body.Horn.IsActive", "Body.Lights.Hazard.IsSignaling",
            "Body.Lights.Beam.High.IsOn"})

    def test_signal_missing_from_model_is_not_fatal(self):
        vio, _ = self.io(tree={"Body": {}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(run(vio.set_horn(True)))
            run(vio.set_horn(True))
        self.assertEqual(out.getvalue().count("not in this vehicle model"), 1)

    def test_unknown_parking_brake_assumes_parked(self):
        vio, _ = self.io(tree={})
        self.assertEqual(run(vio.is_parked()), main.ASSUME_PARKED_IF_UNKNOWN)

    def test_identity_falls_back_to_the_hire_record(self):
        vio, _ = self.io()
        ident = run(vio.identify())
        self.assertEqual(ident["plate"], main.HIRE["plate"])
        self.assertEqual(ident["source"], "hire record")


class AppTests(unittest.TestCase):
    def app(self):
        log = []
        app = main.Canary(FakeNode(VEHICLE_TREE, log))
        return app, log

    def test_arm_with_a_blind_camera_does_not_crash(self):
        app, _ = self.app()
        app.camera = mock.Mock(observe=mock.Mock(return_value=None))
        with contextlib.redirect_stdout(io.StringIO()):
            run(app.arm(background_ppm=13))
        self.assertEqual(app.baseline["seen"], 0)
        self.assertEqual(app.state, main.ARMED)

    def test_alarm_while_parked_sounds_the_horn(self):
        app, log = self.app()
        with mock.patch.object(main, "HORN_PULSE", 0), contextlib.redirect_stdout(io.StringIO()):
            run(app.respond(main.ALARM, "gas", parked=True))
        self.assertIn(("Body.Horn.IsActive", True), log)

    def test_alarm_while_moving_never_sounds_the_horn(self):
        app, log = self.app()
        with contextlib.redirect_stdout(io.StringIO()):
            run(app.respond(main.ALARM, "gas", parked=False))
        self.assertNotIn(("Body.Horn.IsActive", True), log)
        self.assertIn(("Body.Lights.Hazard.IsSignaling", True), log)

    def test_elevated_only_turns_on_hazards(self):
        app, log = self.app()
        with contextlib.redirect_stdout(io.StringIO()):
            run(app.respond(main.ELEVATED, "x", parked=True))
        self.assertEqual(log, [("Body.Lights.Hazard.IsSignaling", True)])

    def test_full_demo_runs_and_leaves_the_van_quiet(self):
        app, log = self.app()
        out = io.StringIO()
        with mock.patch.object(main, "DEMO_PAUSE", 0), \
                mock.patch.object(main, "SAMPLE_SECONDS", 0), \
                mock.patch.object(main, "HORN_PULSE", 0), \
                contextlib.redirect_stdout(out):
            run(app.on_start())
        text = out.getvalue()
        self.assertIn("EVENT 1", text)
        self.assertIn("ALARM, stationary", text)
        self.assertIn("Run complete", text)
        self.assertEqual(app.state, main.DISARMED)
        last = {}
        for path, value in log:
            last[path] = value
        for path in ("Body.Horn.IsActive", "Body.Lights.Hazard.IsSignaling",
                     "Body.Lights.Beam.High.IsOn"):
            self.assertIs(last[path], False, path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
