"""The vehicle half of the loop.

Signals are addressed by dotted path strings from config.py rather than hard
coded attribute chains, because the exact VSS path is the single most likely
thing to differ between the model you develop against and the model on the
hackathon machine. One string to change beats hunting through code at 3am.
"""

import asyncio
import logging

import config

logger = logging.getLogger(__name__)


def resolve(vehicle, path):
    """Walk a dotted VSS path like "Body.Horn.IsActive" to its datapoint object.

    Raises AttributeError naming the full path, not just the failing segment -
    a bare "'Lights' object has no attribute 'Beam'" tells you nothing at speed.
    """
    node = vehicle
    for part in path.split("."):
        try:
            node = getattr(node, part)
        except AttributeError as exc:
            raise AttributeError(
                f"signal path {path!r} is not in this vehicle model "
                f"(failed at {part!r}) - check the API catalogue in the playground"
            ) from exc
    return node


class VehicleIO:
    """Thin wrapper over the Velocitas datapoints this app touches."""

    def __init__(self, vehicle):
        self.vehicle = vehicle
        self._missing = set()
        self._warned = set()

    async def set_signal(self, path, value):
        """Set one signal. Returns False instead of raising if it is unusable.

        A wrong signal path should degrade the demo to "no headlights", not
        crash the app and leave the judges looking at a stack trace.
        """
        # Resolution happens OUTSIDE the try that catches call failures, so a
        # path that resolves to a branch rather than a datapoint is reported as
        # what it is instead of being blamed on the vehicle model.
        try:
            datapoint = resolve(self.vehicle, path)
        except AttributeError as exc:
            if path not in self._missing:  # log the path once, not every frame
                logger.error("%s", exc)
                self._missing.add(path)
            return False

        setter = getattr(datapoint, "set", None)
        if setter is None:
            if path not in self._missing:
                logger.error(
                    "signal path %r resolves to a branch, not a settable datapoint", path
                )
                self._missing.add(path)
            return False

        try:
            await setter(value)
            self._warned.discard(path)
            return True
        except Exception as exc:  # noqa: BLE001 - databroker errors vary
            # Throttle: a databroker that rejects every write would otherwise
            # log once per frame for the life of the demo.
            if path not in self._warned:
                logger.warning("could not set %s to %r: %s", path, value, exc)
                self._warned.add(path)
            return False

    async def get_signal(self, path):
        """Read one signal, or None if unavailable."""
        try:
            return (await resolve(self.vehicle, path).get()).value
        except Exception as exc:  # noqa: BLE001
            if path not in self._warned:
                logger.warning("could not read %s: %s", path, exc)
                self._warned.add(path)
            return None

    async def set_status(self, code):
        """Optional dashboard status signal. No-op until one is configured."""
        path = config.SIGNALS.get("status")
        if not path:
            return False
        return await self.set_signal(path, code)

    async def alert(self, duration=None):
        """The visible vehicle reaction: headlights and horn on, then off.

        This mirrors the drowsy-driver reference prototype in the guidelines on
        purpose - the judges have already been shown that shape, so they read
        yours without needing it explained.
        """
        duration = config.ALERT_DURATION if duration is None else duration
        await self.set_signal(config.SIGNALS["low_beam"], True)
        await self.set_signal(config.SIGNALS["horn"], True)
        try:
            await asyncio.sleep(duration)
        finally:
            # Always release, even if the alert is cancelled mid-sleep. Guarded
            # so a second cancellation arriving here cannot leave the horn on
            # without a trace; main.py also calls safe_off() on the way out, so
            # this is the first of two chances, not the only one.
            try:
                await self.safe_off()
            except asyncio.CancelledError:
                logger.error("cancelled while releasing signals; horn may still be on")
                raise

    async def safe_off(self):
        """Force horn and headlights off. Safe to call from a shutdown path."""
        await self.set_signal(config.SIGNALS["horn"], False)
        await self.set_signal(config.SIGNALS["low_beam"], False)
