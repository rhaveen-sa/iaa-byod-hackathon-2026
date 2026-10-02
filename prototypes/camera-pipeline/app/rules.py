"""Turning a stream of per-frame detections into one stable event.

Detections flicker: a cylinder is occluded for two frames, a tool glints, a
sheep turns sideways. A demo that honks on every flicker reads to a judge as a
demo that does not work.

Two design decisions follow from that, and both were bugs the first time round:

1. The window is ROLLING, not consecutive. Counting consecutive clean frames to
   re-arm means a single bad frame inside the clearing run resets the count -
   so one flicker every N frames disarms the rule permanently and silently.
   A rolling window with separate fire/clear thresholds gives hysteresis
   without that failure mode.

2. A frame with no usable observation (`DetectionResult.ok is False`) is NOT
   evidence. It does not enter the window at all. Otherwise a detector outage
   looks exactly like an empty cargo bay and the horn goes off because the
   network hiccuped.
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field

import config
from detector import DetectionResult

logger = logging.getLogger(__name__)


@dataclass
class Event:
    kind: str
    message: str
    detections: list = field(default_factory=list)
    payload: dict = field(default_factory=dict)


class WindowedRule:
    """Base class. Subclasses implement `condition` for a single frame."""

    kind = "base"

    def __init__(self, window=8, fire_ratio=0.6, clear_ratio=0.25,
                 cooldown_seconds=20.0, clock=time.monotonic):
        if not 0 < clear_ratio <= fire_ratio <= 1:
            raise ValueError("need 0 < clear_ratio <= fire_ratio <= 1")
        self.window = window
        self.fire_ratio = fire_ratio
        self.clear_ratio = clear_ratio
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._frames = deque(maxlen=window)
        self._active = False
        self._last_fired = None
        self._skipped = 0

    def condition(self, detections):
        """Return (is_bad, human-readable message, payload dict)."""
        raise NotImplementedError

    @property
    def bad_ratio(self):
        if not self._frames:
            return 0.0
        return sum(self._frames) / len(self._frames)

    def evaluate(self, result):
        """Feed one frame. Returns an Event when the rule fires, else None.

        Accepts a DetectionResult; a bare list is treated as a successful
        observation for convenience.
        """
        if isinstance(result, DetectionResult):
            if not result.ok:
                # We could not see. Hold state, count it, say so periodically.
                self._skipped += 1
                if self._skipped == 1 or self._skipped % 20 == 0:
                    logger.warning(
                        "rule %s holding: no usable observation (%s), %d frame(s) skipped",
                        self.kind, result.reason, self._skipped,
                    )
                return None
            detections = result.detections
        else:
            detections = result

        self._skipped = 0
        is_bad, message, payload = self.condition(detections)
        self._frames.append(bool(is_bad))

        # Only judge on a full window; a partial one exaggerates whichever
        # frames happen to have arrived first.
        if len(self._frames) < self._frames.maxlen:
            return None

        ratio = self.bad_ratio

        if self._active:
            if ratio <= self.clear_ratio:
                logger.info("rule %s cleared (%.0f%% bad): %s", self.kind, ratio * 100, message)
                self._active = False
            return None

        if ratio < self.fire_ratio:
            return None

        now = self._clock()
        if self._last_fired is not None and now - self._last_fired < self.cooldown_seconds:
            # Suppress, do not defer: mark it handled so the fire does not land
            # the instant the cooldown expires, in the middle of the situation.
            self._active = True
            logger.info("rule %s suppressed by cooldown: %s", self.kind, message)
            return None

        self._active = True
        self._last_fired = now
        return Event(kind=self.kind, message=message, detections=list(detections), payload=payload)


class CountMismatch(WindowedRule):
    """Fires when fewer objects are visible than expected.

    One rule, three challenges: a tool missing from the contractor's cart, a gas
    cylinder short on the pallet, an animal no longer visible in its pen. Set
    expected_count and point `label` at whatever your detector emits.
    """

    kind = "count_mismatch"

    def __init__(self, expected_count, label=None, **kwargs):
        super().__init__(**kwargs)
        self.expected_count = expected_count
        self.label = label

    def condition(self, detections):
        relevant = [d for d in detections if self.label is None or d.label == self.label]
        seen = len(relevant)
        missing = self.expected_count - seen
        payload = {"expected": self.expected_count, "seen": seen, "missing": missing}
        if missing > 0:
            noun = "item" if missing == 1 else "items"
            return True, f"{missing} {noun} missing ({seen} of {self.expected_count} visible)", payload
        return False, f"all {self.expected_count} items visible", payload


class ZoneBreach(WindowedRule):
    """Fires when a detection's centre enters a forbidden region of the frame.

    The zone is normalised (0..1), so it survives a camera swap or a resolution
    change. Good fit for "cylinder stored against the tailgate" or "animal in
    the loading corridor".
    """

    kind = "zone_breach"

    def __init__(self, zone, label=None, **kwargs):
        super().__init__(**kwargs)
        self.zone = tuple(zone)
        self.label = label

    def condition(self, detections):
        zx1, zy1, zx2, zy2 = self.zone
        breaching = []
        for det in detections:
            if self.label is not None and det.label != self.label:
                continue
            x1, y1, x2, y2 = det.box  # detector guarantees 4 values
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            if zx1 <= cx <= zx2 and zy1 <= cy <= zy2:
                breaching.append(det)
        payload = {"zone": self.zone, "breaching": len(breaching)}
        if breaching:
            noun = "item" if len(breaching) == 1 else "items"
            return True, f"{len(breaching)} {noun} inside the restricted zone", payload
        return False, "restricted zone clear", payload


def build(cfg=None):
    cfg = config.RULE if cfg is None else cfg
    shared = {
        "window": int(cfg.get("window", 8)),
        "fire_ratio": float(cfg.get("fire_ratio", 0.6)),
        "clear_ratio": float(cfg.get("clear_ratio", 0.25)),
        "cooldown_seconds": float(cfg.get("cooldown_seconds", 20)),
    }
    if cfg.get("kind") == "zone_breach":
        return ZoneBreach(cfg.get("zone", (0.6, 0.0, 1.0, 1.0)), cfg.get("label"), **shared)
    return CountMismatch(int(cfg.get("expected_count", 4)), cfg.get("label"), **shared)
