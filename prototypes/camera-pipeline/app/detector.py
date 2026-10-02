"""Detection layer.

The challenge is not picked yet, so the rest of the app depends only on the
`Detection` and `DetectionResult` shapes below. Choosing tools vs gas cylinders
vs animals later means writing one class here and changing one string in
config.py - nothing else in the pipeline moves.

The important contract: `DetectionResult.ok` says whether this frame produced a
usable OBSERVATION. `ok=False` means "we could not see", which is not the same
as "we looked and there was nothing there". Collapsing those two is what turns
a detector outage into a false alarm.
"""

import logging
import time
from dataclasses import dataclass, field

import requests

import config

logger = logging.getLogger(__name__)


@dataclass
class Detection:
    label: str
    confidence: float
    # x1, y1, x2, y2 normalised to 0..1, so the rules never need to know the
    # camera resolution - which changes the moment you swap CAM-1 for CAM-2.
    box: tuple = (0.0, 0.0, 0.0, 0.0)
    extra: dict = field(default_factory=dict)


@dataclass
class DetectionResult:
    detections: list = field(default_factory=list)
    ok: bool = True
    reason: str = ""

    @classmethod
    def unavailable(cls, reason: str):
        return cls(detections=[], ok=False, reason=reason)

    def __len__(self):
        return len(self.detections)


def _normalise_box(raw):
    """Coerce a box from a remote service into exactly four floats.

    A short box array used to build a 2-tuple, which then blew up in
    ZoneBreach's unpack. Pad and clamp instead of trusting the sender.
    """
    try:
        values = [float(v) for v in list(raw)[:4]]
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)
    values += [0.0] * (4 - len(values))
    return tuple(min(1.0, max(0.0, v)) for v in values)


class Detector:
    kind = "base"

    def detect(self, jpeg_bytes: bytes) -> DetectionResult:
        raise NotImplementedError


class StubDetector(Detector):
    """Fakes detections on a cycle. Decodes nothing, installs nothing.

    Here so you can prove the full loop - camera to vehicle signal to dashboard -
    on day one, before anyone has trained or wired up a model. It reports the
    expected number of objects, then periodically one fewer, which is exactly
    the event the default rule watches for.

    `clock` is injectable so tests are not wall-clock flaky.
    """

    kind = "stub"

    def __init__(self, expected=4, drop_every=15.0, drop_for=6.0, clock=time.monotonic):
        self.expected = expected
        self.drop_every = drop_every
        self.drop_for = drop_for
        self._clock = clock
        self._t0 = clock()

    def detect(self, jpeg_bytes: bytes) -> DetectionResult:
        phase = (self._clock() - self._t0) % self.drop_every
        count = self.expected - 1 if phase < self.drop_for else self.expected
        return DetectionResult(
            [
                Detection(
                    label="object",
                    confidence=0.9,
                    box=(0.05 + i * 0.12, 0.4, 0.13 + i * 0.12, 0.7),
                    extra={"stub": True},
                )
                for i in range(max(0, count))
            ]
        )


class RemoteDetector(Detector):
    """POSTs the frame to a vision service and reads detections back.

    The recommended route. It keeps model weights off the runtime entirely, so
    requirements.txt stays tiny and startup stays fast. It is also the honest
    BYOD story: the device and its analysis live outside the vehicle, and the
    vehicle consumes the result.

    Expects JSON like:
        {"detections": [{"label": "cylinder", "confidence": 0.91,
                         "box": [0.1, 0.2, 0.3, 0.6]}]}

    Anything it cannot parse is reported as UNAVAILABLE, never as zero
    detections - see the module docstring.
    """

    kind = "remote"

    def __init__(self, url: str, min_confidence: float = 0.4, classes=None):
        if not url:
            raise ValueError("DETECTOR['remote_url'] must be set for the remote detector")
        self.url = url
        self.min_confidence = min_confidence
        self.classes = set(classes or [])

    def detect(self, jpeg_bytes: bytes) -> DetectionResult:
        try:
            response = requests.post(
                self.url,
                files={"file": ("frame.jpg", jpeg_bytes, "image/jpeg")},
                timeout=config.HTTP_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            return DetectionResult.unavailable(f"request failed: {exc}")
        except ValueError as exc:
            return DetectionResult.unavailable(f"response was not JSON: {exc}")

        # Everything below is attacker-shaped input as far as we are concerned:
        # a malformed response must degrade to "unavailable", not crash the loop
        # one frame at a time forever.
        try:
            if not isinstance(payload, dict):
                return DetectionResult.unavailable("response was not a JSON object")
            items = payload.get("detections")
            if items is None:
                items = []
            if not isinstance(items, list):
                return DetectionResult.unavailable("'detections' was not a list")

            out = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    confidence = float(item.get("confidence", 0.0))
                except (TypeError, ValueError):
                    continue
                label = str(item.get("label", "object"))
                if confidence < self.min_confidence:
                    continue
                if self.classes and label not in self.classes:
                    continue
                out.append(
                    Detection(
                        label=label,
                        confidence=confidence,
                        box=_normalise_box(item.get("box") or ()),
                    )
                )
            return DetectionResult(out)
        except Exception as exc:  # noqa: BLE001 - last line of defence
            return DetectionResult.unavailable(f"malformed response: {exc}")


class LocalDetector(Detector):
    """Decodes and analyses the frame on the runtime itself.

    Needs numpy + opencv-python-headless uncommented in requirements.txt. The
    contour pass below is a placeholder that finds large bright blobs - useful
    for checking that decoding works, not for a real demo. Swap the body for
    your model once the challenge is chosen.
    """

    kind = "local"

    def __init__(self, min_area_ratio=0.004, label="object"):
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "LocalDetector needs numpy and opencv-python-headless - "
                "uncomment them in app/requirements.txt"
            ) from exc
        self.min_area_ratio = min_area_ratio
        self.label = label

    def detect(self, jpeg_bytes: bytes) -> DetectionResult:
        import cv2
        import numpy as np

        frame = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            # Could not decode: we did not observe an empty scene, we failed to look.
            return DetectionResult.unavailable("frame did not decode")

        height, width = frame.shape[:2]
        gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (7, 7), 0)
        _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        )
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        min_area = self.min_area_ratio * width * height
        out = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < min_area:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            out.append(
                Detection(
                    label=self.label,
                    confidence=min(1.0, area / (width * height) * 8),
                    box=(x / width, y / height, (x + w) / width, (y + h) / height),
                )
            )
        return DetectionResult(out)


def build(cfg=None) -> Detector:
    cfg = config.DETECTOR if cfg is None else cfg
    kind = cfg.get("kind", "stub")
    if kind == "remote":
        return RemoteDetector(
            cfg.get("remote_url", ""),
            float(cfg.get("min_confidence", 0.4)),
            cfg.get("classes") or None,
        )
    if kind == "local":
        return LocalDetector()
    return StubDetector(expected=int(config.RULE.get("expected_count", 4)))
