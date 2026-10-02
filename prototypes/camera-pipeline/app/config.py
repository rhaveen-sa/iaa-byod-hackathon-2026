"""All tunables in one place.

Nothing here is secret, so it stays in the repo rather than in a config file you
have to remember to paste into the playground editor.
"""

# --- Camera -----------------------------------------------------------------
# From the hackathon guidelines. CAM-2 and CAM-3 were still placeholders when
# this was written; the supervisors update them on the day.
CAM = {
    "video_feed": "https://domain-reenter-boxer.ngrok-free.dev/video_feed",
    "upload_snapshot": "https://domain-reenter-boxer.ngrok-free.dev/upload_snapshot",
    "view": "https://domain-reenter-boxer.ngrok-free.dev/view",
}

# Seconds between frames we actually look at. The stream runs faster than this;
# we deliberately sample. 2-5 fps is plenty for every challenge in the brief and
# keeps the runtime from burning CPU it does not have.
SAMPLE_INTERVAL = 0.5

# Wall-clock seconds to wait on the camera before giving up on a read.
HTTP_TIMEOUT = 10

# Warn when no frame has arrived for this long. Without it, a dead camera is
# completely silent at the app level - the loop just blocks forever.
STALE_FRAME_SECONDS = 30

# --- Detection --------------------------------------------------------------
DETECTOR = {
    # "stub"   - no image decoding, fires on a schedule. Proves the loop end to end.
    # "remote" - POST the frame to a vision service, read labels back. Recommended:
    #            keeps heavy models off the runtime.
    # "local"  - decode and analyse on the runtime. Needs the commented-out deps.
    "kind": "stub",
    "remote_url": "",          # your detection endpoint, if kind == "remote"
    "min_confidence": 0.4,
    "classes": [],             # empty = accept every label the detector returns
}

# --- Event rule -------------------------------------------------------------
# Detections flicker, so the rule works on a ROLLING WINDOW rather than
# consecutive frames: it fires when the bad fraction of the last `window` frames
# crosses fire_ratio, and re-arms when it falls below clear_ratio. Two
# thresholds give hysteresis without the "one flicker disarms it forever" bug
# that consecutive counting has.
RULE = {
    "kind": "count_mismatch",  # or "zone_breach"
    "expected_count": 4,
    "label": None,             # None = count every detection
    # Normalised x1,y1,x2,y2 for zone_breach. Kept clear of the stub detector's
    # boxes (which start at x=0.1) so the stub does not self-breach on frame 1.
    "zone": (0.6, 0.0, 1.0, 1.0),
    "window": 8,               # frames considered
    "fire_ratio": 0.6,         # >= this fraction bad -> fire
    "clear_ratio": 0.25,       # <= this fraction bad -> re-arm
    "cooldown_seconds": 20,
}

# --- Vehicle response -------------------------------------------------------
# VSS 4.0 paths, relative to the `vehicle` root object. The runtime ships VSS
# 4.0, but CONFIRM these against the API catalogue in the playground before the
# demo: VSS 3.x called the headlight signal Body.Lights.IsLowBeamOn and some
# vehicle models still carry the older tree.
SIGNALS = {
    "low_beam": "Body.Lights.Beam.Low.IsOn",
    "horn": "Body.Horn.IsActive",
    # Optional. Set to a spare WRITABLE numeric signal to surface app health on
    # the dashboard (see health.StatusCode). Which signal to borrow depends on
    # the vehicle model, so this is off until you pick one at the event.
    "status": None,
}

# How long the alert stays on, in seconds.
ALERT_DURATION = 3.0

# Post the frame that triggered the event to the camera's /upload_snapshot, so
# the dashboard shows the evidence next to the vehicle reaction. Sent AFTER the
# vehicle reacts and off the hot path - an unreachable camera host must never
# delay the horn.
UPLOAD_EVIDENCE = True
