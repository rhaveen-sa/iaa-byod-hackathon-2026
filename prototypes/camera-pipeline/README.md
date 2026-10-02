# BYOD device-to-vehicle loop

A prototype for the IAA Transportation LCV BYOD hackathon. An external device
observes something, the app decides whether it matters, and the vehicle reacts
visibly:

```
camera frame  ->  detection  ->  windowed event  ->  vehicle signal  ->  evidence
```

The challenge area (tools / gas cylinders / animals) is **not** baked in. Picking
one means writing a detector class and changing a few values in `config.py`.

## Where it runs

Inside the digital.auto SDV runtime — a Docker container with Python 3.10, the
Kuksa databroker, VSS 4.0 and the Velocitas Python SDK 0.14.1. Note this is real
CPython, not Brython in the browser: the older `from browser import aio` examples
in the docs are the in-browser mode, not this one.

The runtime unpacks a multi-files project into `app/`, pip-installs
`app/requirements.txt`, and runs `python -u app/main.py`. Two consequences shape
the code:

- `app/` is on `sys.path` as a plain directory, so imports are top-level
  (`import config`), not package-relative. There is no `app/__init__.py`.
- `requirements.txt` is reinstalled on **every** run, so it stays tiny. OpenCV is
  commented out deliberately; adding it turns a 2-second start into a 2-minute one.

## Files

| File | What it does |
|---|---|
| `app/main.py` | Entrypoint. Camera thread, processing loop, watchdog, shutdown. |
| `app/camera.py` | MJPEG stream reader and snapshot upload. Pure `requests`. |
| `app/detector.py` | `stub` / `remote` / `local` detectors behind one interface. |
| `app/rules.py` | Rolling-window event rules: count mismatch, zone breach. |
| `app/vehicle_io.py` | VSS signal set/get, the alert, and `safe_off()`. |
| `app/health.py` | Camera/detector health and the optional dashboard status code. |
| `app/config.py` | Every tunable. Start here. |
| `tests/` | Offline tests. `python3 tests/test_loop.py` from the repo root. |

## Getting it into the Playground

1. Open [playground.digital.auto](https://playground.digital.auto/), log in, and
   select the vehicle model **IAA Hannover Hackathon 2026**.
2. Create a prototype, choose **multi-files project**, name it after your team.
3. In the **SDV Code** tab, recreate the `app/` folder and its files.
4. Open the terminal (arrow, bottom right) → runtime box → **add runtime** →
   `Runtime-<name your supervisor gave you>`.
5. Run. The stub detector fires an event every ~15s, so you should see the
   headlights and horn react without any camera being connected at all.

## What to change on the day

Everything below is in `config.py`.

- `CAM` — the real camera URLs. Only CAM-1 had a live URL when this was written;
  CAM-2 and CAM-3 were placeholders.
- `SIGNALS["low_beam"]` / `SIGNALS["horn"]` — **confirm these against the API
  catalogue first.** They are VSS 4.0 paths; VSS 3.x called the headlight signal
  `Body.Lights.IsLowBeamOn` and some vehicle models still carry the older tree.
  A wrong path degrades to "no headlights" and logs once — it will not crash, but
  it will also not impress anyone.
- `DETECTOR["kind"]` — `stub` to prove the loop, `remote` for a real demo,
  `local` only if you truly need decoding on the runtime.
- `RULE["expected_count"]` and `RULE["label"]` — once you know what you're counting.
- `SIGNALS["status"]` — optional. Point it at a spare writable numeric signal and
  the dashboard shows 0 = OK, 1 = degraded, 2 = alerting.

## Two design decisions worth knowing about

Both were bugs first, found by review, and both would have embarrassed us on stage.

**"No data" is not "nothing there."** Detectors return a `DetectionResult` with an
`ok` flag. When the vision service is unreachable, `ok` is `False` and the rule
holds its state. The obvious alternative — returning an empty list — means a
detector outage reads as "all four cylinders are missing" and the horn goes off
because the network hiccuped.

**The event window is rolling, not consecutive.** The rule fires when the bad
fraction of the last `window` frames crosses `fire_ratio`, and re-arms when it
falls below `clear_ratio`. Counting *consecutive* clean frames to re-arm sounds
equivalent and is not: one flicker inside the clearing run resets the count, so
flicker arriving more often than the window is long latches the rule on forever,
silently. Flicker is the exact thing this module exists to tolerate.

## Testing

```
python3 tests/test_loop.py
```

42 tests, no runtime needed — `tests/conftest_stubs.py` stands in for the
`vehicle` and `velocitas_sdk` modules so `main.py` itself is importable and the
loop, the shutdown path and the frame queue are all covered.

Each test was checked against a deliberately broken version of the code it
guards, so a green suite means the fixes are actually in place. That check was
worth doing: the previous suite was green on code with four demo-breaking bugs.

## Unknowns

Things that could not be settled off-site:

- **Can the runtime reach the camera host?** The camera URL is an ngrok tunnel.
  Both a cloud sandbox and a local dev VM are blocked from it by their egress
  proxies, so this is untested. Check it first on the day — the whole loop
  depends on it, and so does the `remote` detector.
- **The two VSS paths.** See above.
- **CAM-2 and CAM-3.** Placeholders in the organisers' guidelines; they said the
  links get updated at the event.
