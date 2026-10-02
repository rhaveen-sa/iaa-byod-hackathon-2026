"""Tests for the BYOD device-permissions prototype.

Run from this folder:  python3 tests/test_permissions.py
"""

import asyncio
import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest_stubs  # noqa: E402

conftest_stubs.install()

import main  # noqa: E402
from conftest_stubs import VEHICLE_TREE, FakeNode  # noqa: E402


def quiet(fn, *a):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a)


class ScopeGrammar(unittest.TestCase):
    def test_built_in_self_check_passes(self):
        quiet(main.self_check)

    def test_rejects_unknown_action(self):
        with self.assertRaises(ValueError):
            main.Scope("delete:Vehicle.Body")

    def test_rejects_missing_path(self):
        with self.assertRaises(ValueError):
            main.Scope("read")


class Enforcement(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.log = []
        self.registry = main.GrantRegistry(clock=lambda: self.now[0])
        self.guard = main.GuardedVehicle(FakeNode(VEHICLE_TREE, self.log), self.registry)

    def actuate(self, path):
        return quiet(asyncio.run, self.guard.actuate("TOOLCRATE-7", path, True))

    def test_unapproved_device_performs_no_write(self):
        self.registry.onboard(main.TOOLCRATE)
        self.assertFalse(self.actuate(main.HORN))
        self.assertEqual(self.log, [])

    def test_narrowed_grant_withholds_the_parking_brake(self):
        self.registry.onboard(main.TOOLCRATE)
        self.registry.approve("TOOLCRATE-7", scopes=["actuate:" + main.HORN], ttl=60)
        self.assertTrue(self.actuate(main.HORN))
        self.assertFalse(self.actuate(main.PARKING_BRAKE))
        self.assertEqual(len(self.guard.writes), 1)

    def test_revoked_grant_denies(self):
        self.registry.onboard(main.TOOLCRATE)
        self.registry.approve("TOOLCRATE-7", scopes=["actuate:" + main.HORN], ttl=60)
        self.registry.revoke("TOOLCRATE-7")
        self.assertFalse(self.actuate(main.HORN))

    def test_every_decision_is_audited(self):
        self.registry.onboard(main.TOOLCRATE)
        self.actuate(main.HORN)
        events = [e[2] for e in self.registry.audit]
        self.assertEqual(events, ["ONBOARD", "DENIED"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
