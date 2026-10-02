"""Stubs for the two modules that only exist inside the SDV runtime.

Installing these in sys.modules is what lets the tests import app/main.py and
exercise FrameFeed and the processing loop. Without it the whole orchestration
layer - which is where the interesting bugs live - is untestable by construction.
"""

import asyncio
import os
import sys
import types

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")


class FakeDatapoint:
    """A datapoint that actually suspends on set/get.

    The await points matter: a fake whose `set` never suspends makes
    cancellation tests pass vacuously, because the coroutine never yields and
    the `finally` always completes.
    """

    def __init__(self, path, log, fail=False):
        self.path = path
        self.log = log
        self.fail = fail

    async def set(self, value):
        await asyncio.sleep(0)
        if self.fail:
            raise RuntimeError(f"databroker rejected {self.path}")
        self.log.append((self.path, value))

    # Values a real databroker would return for the identity attributes, so a
    # local run shows the same binding line the runtime will.
    IDENTITY = {"IsMoving": False}   # runtime returns None for identity attrs

    async def get(self):
        await asyncio.sleep(0)
        return types.SimpleNamespace(value=self.IDENTITY.get(self.path))


class FakeNode:
    """Stands in for the Velocitas vehicle model tree."""

    def __init__(self, tree, log, prefix="", fail_paths=()):
        self._tree = tree
        self._log = log
        self._prefix = prefix
        self._fail_paths = set(fail_paths)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._tree:
            raise AttributeError(name)
        path = f"{self._prefix}.{name}".lstrip(".")
        child = self._tree[name]
        if child is None:
            return FakeDatapoint(path, self._log, fail=path in self._fail_paths)
        return FakeNode(child, self._log, path, self._fail_paths)


VEHICLE_TREE = {
    # Mirrors the API catalogue of vehicle model "IAA Hannover Hackathon 2026".
    "Body": {
        "Lights": {
            "Beam": {"High": {"IsOn": None}},
            "Hazard": {"IsSignaling": None},
            "DirectionIndicator": {
                "Left": {"IsSignaling": None},
                "Right": {"IsSignaling": None},
            },
        },
        "Horn": {"IsActive": None},
    },
    "Chassis": {"ParkingBrake": {"IsEngaged": None}},
    "IsMoving": None,
    "VehicleIdentification": {
        "LicensePlate": None, "Brand": None, "Model": None,
        "DateVehicleFirstRegistered": None,
    },
}


class FakeVehicleApp:
    """Minimal stand-in for velocitas_sdk.vehicle_app.VehicleApp."""

    def __init__(self):
        self._running = False

    async def run(self):
        self._running = True
        await self.on_start()
        while self._running:
            await asyncio.sleep(0.05)

    async def on_start(self):
        pass

    async def on_stop(self):
        pass


def install():
    """Put the stub modules on sys.path/sys.modules. Idempotent."""
    if APP_DIR not in sys.path:
        sys.path.insert(0, APP_DIR)

    if "vehicle" not in sys.modules:
        module = types.ModuleType("vehicle")
        module.Vehicle = FakeNode
        module.vehicle = FakeNode(VEHICLE_TREE, [])
        sys.modules["vehicle"] = module

    if "sdv" not in sys.modules:
        sdv = types.ModuleType("sdv")
        sdv_app = types.ModuleType("sdv.vehicle_app")
        sdv_app.VehicleApp = FakeVehicleApp
        sdv_vdb = types.ModuleType("sdv.vdb")
        sdv_reply = types.ModuleType("sdv.vdb.reply")
        sdv_reply.DataPointReply = object
        sdv.vehicle_app = sdv_app
        sdv.vdb = sdv_vdb
        sdv_vdb.reply = sdv_reply
        sys.modules.update({
            "sdv": sdv, "sdv.vehicle_app": sdv_app,
            "sdv.vdb": sdv_vdb, "sdv.vdb.reply": sdv_reply,
        })

    if "velocitas_sdk" not in sys.modules:
        root = types.ModuleType("velocitas_sdk")
        util = types.ModuleType("velocitas_sdk.util")
        log = types.ModuleType("velocitas_sdk.util.log")
        log.get_opentelemetry_log_factory = lambda: __import__("logging").LogRecord
        log.get_opentelemetry_log_format = lambda: "%(levelname)s %(name)s %(message)s"
        app_mod = types.ModuleType("velocitas_sdk.vehicle_app")
        app_mod.VehicleApp = FakeVehicleApp
        app_mod.subscribe_topic = lambda *a, **k: (lambda f: f)
        root.util = util
        util.log = log
        root.vehicle_app = app_mod
        sys.modules.update({
            "velocitas_sdk": root,
            "velocitas_sdk.util": util,
            "velocitas_sdk.util.log": log,
            "velocitas_sdk.vehicle_app": app_mod,
        })
