"""Whether the app is actually working, and saying so out loud.

A BYOD device dropping out is part of the story this prototype is telling, so
it should be visible rather than silent. Two rules here:

  - transitions are logged, steady states are not (no per-frame log spam)
  - "we cannot see" is never reported as "we see nothing"
"""

import logging
import time

logger = logging.getLogger(__name__)


class StatusCode:
    """Numeric codes, for the optional dashboard signal."""

    OK = 0
    DEGRADED = 1
    ALERTING = 2


class Health:
    """Tracks camera and detector availability and reports changes once each."""

    def __init__(self, stale_after: float = 30.0):
        self.stale_after = stale_after
        self._camera_ok = None       # None = not yet known
        self._detector_ok = None
        self._last_frame_at = None
        self._alerting = False

    # --- transitions --------------------------------------------------------

    def _transition(self, current, new, label):
        if current is new:
            return new
        if current is not None:  # do not announce the very first observation
            logger.warning("%s %s", label, "RECOVERED" if new else "OFFLINE")
        return new

    def camera(self, ok: bool) -> None:
        self._camera_ok = self._transition(self._camera_ok, ok, "CAMERA")
        if ok:
            self._last_frame_at = time.monotonic()

    def detector(self, ok: bool, reason: str = "") -> None:
        if self._detector_ok is True and not ok and reason:
            logger.warning("DETECTOR OFFLINE (%s)", reason)
            self._detector_ok = False
            return
        self._detector_ok = self._transition(self._detector_ok, ok, "DETECTOR")

    def alerting(self, value: bool) -> None:
        self._alerting = value

    # --- queries ------------------------------------------------------------

    def frame_age(self):
        """Seconds since the last frame, or None if none has ever arrived."""
        if self._last_frame_at is None:
            return None
        return time.monotonic() - self._last_frame_at

    def is_stale(self) -> bool:
        age = self.frame_age()
        return age is not None and age > self.stale_after

    @property
    def ok(self) -> bool:
        return bool(self._camera_ok) and self._detector_ok is not False and not self.is_stale()

    def status_code(self) -> int:
        if self._alerting:
            return StatusCode.ALERTING
        return StatusCode.OK if self.ok else StatusCode.DEGRADED

    def summary(self) -> str:
        parts = [
            f"camera={'ok' if self._camera_ok else 'down'}",
            f"detector={'ok' if self._detector_ok is not False else 'down'}",
        ]
        age = self.frame_age()
        if age is not None:
            parts.append(f"last_frame={age:.0f}s ago")
        return " ".join(parts)
