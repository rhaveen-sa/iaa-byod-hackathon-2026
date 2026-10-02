# Architecture

All three prototypes run as a Velocitas `VehicleApp` on the digital.auto SDV
runtime (Python 3.10, Kuksa databroker, model **IAA Hannover Hackathon 2026**).
The runtime unpacks `app/`, pip-installs `app/requirements.txt` and runs
`python -u app/main.py`.

[`visualization.html`](visualization.html) has the same class diagrams and a
step-through simulation of the Canary Go leak monitor. Open it in a browser;
GitHub shows HTML files as source.

---

## 1. Canary Go: portable load bay monitor

Canary Go checks a gas sensor and a camera against a baseline captured at
arming, then drives the van's own hazards, high beam and horn. The whole app is
one file: [`prototypes/canary-go/app/main.py`](../prototypes/canary-go/app/main.py).

### Class diagram

```mermaid
classDiagram
    direction LR

    class VehicleApp {
        <<Velocitas SDK>>
        +run()
        +on_start()
    }

    class Canary {
        -io: VehicleIO
        -gas: GasSensor
        -camera: LoadCamera
        -monitor: LeakMonitor
        -state: DISARMED | ARMED
        -baseline: dict
        +arm(background_ppm)
        +disarm()
        +respond(severity, headline, parked)
        +on_start()
    }

    class GasSensor {
        <<abstract>>
        +name
        +simulated
        +read_ppm() float | None
    }
    class HttpGasSensor {
        +url
        +field
        +read_ppm()
    }
    class SimulatedGasSensor {
        +profile: list
        +read_ppm()
    }

    class LoadCamera {
        <<abstract>>
        +observe() dict | None
        +script(*events)
        +check(baseline) (ok, text)
    }
    class SimulatedLoadCamera {
        +expected
        +upright
        +observe()
        +script(*events)
    }
    class LiveLoadCamera {
        -model: YOLO-World
        -_worker() thread
        +observe()
    }

    class LeakMonitor {
        +level: SAFE | ELEVATED | ALARM
        -_streak
        +classify(ppm)$
        +update(ppm) level | None
    }

    class VehicleIO {
        +set_hazards(on)
        +set_horn(on)
        +set_high_beam(on)
        +identify() dict
        +is_stationary() bool
        +is_parked() bool
        +publish_cargo(ppm, leaking)
        +all_off()
    }

    VehicleApp <|-- Canary
    GasSensor <|-- HttpGasSensor
    GasSensor <|-- SimulatedGasSensor
    LoadCamera <|-- SimulatedLoadCamera
    LoadCamera <|-- LiveLoadCamera
    Canary *-- VehicleIO
    Canary *-- LeakMonitor
    Canary o-- GasSensor
    Canary o-- LoadCamera
```

### Lifecycle

```mermaid
stateDiagram-v2
    [*] --> DISARMED
    DISARMED --> ARMED: arm()\nread identity + capture baseline
    ARMED --> ARMED: camera.check() / monitor.update()
    ARMED --> DISARMED: disarm()\nall actuators off
```

### Leak monitor levels (with hysteresis)

```mermaid
stateDiagram-v2
    SAFE --> ELEVATED: ppm >= 400 for 3 readings
    SAFE --> ALARM: ppm >= 1000 for 3 readings
    ELEVATED --> ALARM: ppm >= 1000 for 3 readings
    ALARM --> ELEVATED: 320 < ppm < 1000 for 3 readings
    ELEVATED --> SAFE: ppm <= 320 for 3 readings
    ALARM --> SAFE: ppm <= 320 for 3 readings
    note right of SAFE: None (sensor unreadable) holds the current level
```

### Vehicle response

| Severity | Parked | Moving |
|---|---|---|
| `ELEVATED` | Hazards | Hazards |
| `ALARM` | Hazards + high beam + 3 horn pulses | Hazards + high beam, **no horn** |
| `SAFE` | All off | All off |

### Runtime constraints (do not undo)

- The SDK import is `sdv`, with a `velocitas_sdk` fallback.
- Signals are literal attribute chains (`self.Vehicle.Body.Horn.IsActive`).
  The Playground scans the source text to build its *Used APIs* list, which is
  what registers the mock signals.
- Output is `print(flush=True)`, never `logging`.
- `requirements.txt` stays empty so the runtime starts instantly.

---

## 2. Camera pipeline: device-to-vehicle loop

```
camera frame -> detection -> windowed event -> vehicle signal -> evidence
```

Each stage is its own module in [`prototypes/camera-pipeline/app/`](../prototypes/camera-pipeline/app/),
so picking a challenge only touches `detector.py` and `config.py`.

```mermaid
classDiagram
    direction LR

    class ByodApp {
        -vehicle_io: VehicleIO
        -detector: Detector
        -rule: WindowedRule
        -feed: FrameFeed
        -health: Health
        +on_start()
        -_loop()
        -_watchdog()
        -_handle_event(event, frame)
        +on_stop()
    }
    class FrameFeed {
        -cam: MjpegCamera
        -_queue: Queue(maxsize=1)
        +start()
        +next_frame() bytes | None
        +stop()
    }
    class MjpegCamera {
        +url
        +sample_interval
        +frames() iterator~bytes~
        +grab_one() bytes
        -_parse_multipart()
        -_parse_markers()
    }

    class Detector {
        <<abstract>>
        +detect(jpeg) DetectionResult
    }
    class StubDetector
    class RemoteDetector {
        +url
        +min_confidence
    }
    class LocalDetector {
        OpenCV contours
    }
    class DetectionResult {
        +detections: list~Detection~
        +ok: bool
        +reason: str
        +unavailable(reason)$
    }
    class Detection {
        +label
        +confidence
        +box: x1,y1,x2,y2 (0..1)
    }

    class WindowedRule {
        <<abstract>>
        +window
        +fire_ratio
        +clear_ratio
        +cooldown_seconds
        +evaluate(result) Event | None
        +condition(detections)*
    }
    class CountMismatch {
        +expected_count
        +label
    }
    class ZoneBreach {
        +zone
        +label
    }
    class Event {
        +kind
        +message
        +payload
    }

    class VehicleIO {
        +set_signal(path, value)
        +get_signal(path)
        +alert(duration)
        +safe_off()
        +set_status(code)
    }
    class Health {
        +camera(ok)
        +detector(ok, reason)
        +status_code() OK | DEGRADED | ALERTING
    }

    VehicleApp <|-- ByodApp
    ByodApp *-- FrameFeed
    FrameFeed *-- MjpegCamera
    ByodApp *-- VehicleIO
    ByodApp *-- Health
    ByodApp o-- Detector
    ByodApp o-- WindowedRule
    Detector <|-- StubDetector
    Detector <|-- RemoteDetector
    Detector <|-- LocalDetector
    Detector ..> DetectionResult
    DetectionResult *-- Detection
    WindowedRule <|-- CountMismatch
    WindowedRule <|-- ZoneBreach
    WindowedRule ..> Event
```

### Frame-to-horn sequence

```mermaid
sequenceDiagram
    participant Cam as MjpegCamera (thread)
    participant Feed as FrameFeed
    participant App as ByodApp._loop
    participant Det as Detector
    participant Rule as WindowedRule
    participant VIO as VehicleIO
    participant Snap as upload_snapshot

    Cam->>Feed: newest JPEG (queue size 1, stale frame dropped)
    App->>Feed: await next_frame()
    App->>Det: detect(frame) in worker thread
    Det-->>App: DetectionResult(ok, detections)
    App->>Rule: evaluate(result)
    Note over Rule: ok=False never enters the window
    Rule-->>App: Event (bad ratio >= fire_ratio, past cooldown)
    App->>VIO: alert(): low beam + horn, then safe_off()
    App-)Snap: evidence upload (after the vehicle reacts)
```

---

## 3. Device permissions: onboarding and scoped grants

This demo shows how a device the OEM has never seen could get permission to
act on a vehicle. Grants use the `ACTION:PATH` scope grammar that the Kuksa
databroker already understands. The app is one file: [`prototypes/device-permissions/app/main.py`](../prototypes/device-permissions/app/main.py).

```mermaid
classDiagram
    direction LR
    class Scope {
        +action: read | actuate | provide
        +path
        +covers(action, path) bool
    }
    class DeviceManifest {
        +device_id
        +kind
        +owner
        +provides
        +requests: list~Scope~
        +reason
    }
    class Grant {
        +state: PENDING | GRANTED | DENIED | REVOKED
        +scopes: list~Scope~
        +ttl
        +approve()
        +revoke()
        +allows(action, path)
    }
    class GrantRegistry {
        +grants: dict
        +audit: list
        +onboard(manifest)
        +approve(device_id, scopes, ttl)
        +revoke(device_id)
        +check(device_id, action, path)
    }
    class GuardedVehicle {
        +writes
        +denials
        +actuate(device_id, path, value)
        +read(device_id, path)
    }
    class ByodPermissionsApp {
        +on_start()
    }

    VehicleApp <|-- ByodPermissionsApp
    ByodPermissionsApp *-- GrantRegistry
    ByodPermissionsApp *-- GuardedVehicle
    GuardedVehicle --> GrantRegistry : check()
    GrantRegistry *-- Grant
    Grant --> DeviceManifest
    Grant *-- Scope
    DeviceManifest *-- Scope
```

```mermaid
stateDiagram-v2
    [*] --> PENDING: onboard(manifest)
    PENDING --> GRANTED: approve(scopes narrowed, ttl)
    GRANTED --> REVOKED: revoke()
    GRANTED --> EXPIRED: ttl elapsed
    REVOKED --> [*]
    EXPIRED --> [*]
```
