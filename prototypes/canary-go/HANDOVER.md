# Canary Go team handover

IAA Transportation LCV BYOD Hackathon.
Playground model: `IAA Hannover Hackathon 2026`. Prototype: **Canary Go**.

Read sections 1 and 2 before doing anything. The rest is reference.

---

## 1. What we are building

A portable load bay monitor for hired commercial vehicles carrying LPG and
industrial gas.

The customer is a rental and leasing fleet. They own the vans and the
liability. They do not own the load, they did not pack it, and their fleet is
old.

Two things can go wrong in the back of a van on a gas run, and nobody in the
cab can see either of them. A cylinder can shift or fall, which in a braking
van makes it a missile, and a sheared valve turns it into a rocket. Or a
cylinder can leak, which nobody smells over the engine, and a sealed van
filling with LPG needs one ignition source to end very badly.

Canary Go watches the load bay with two devices on the BYOD box and makes the
van itself react.

## 2. The two arguments the pitch rests on

**Why this has to be bring-your-own-device.**

Millions of commercial vans in Europe are over ten years old. Not one of them
will ever be a software-defined vehicle. None of them will ever get a
factory-fitted cargo monitor. You cannot retrofit a software-defined vehicle
into a 2013 panel van. You can clip a box into it.

Every one of those vans has a horn and hazard lights, standard since the 1960s.
That is all the actuation this needs. For this fleet, BYOD is not the
convenient option, it is the only option that will ever exist.

The kit has to be portable because a rental fleet's vehicles rotate constantly.
Bolt sensors to a van and they are worth one van. Let the kit move and a fleet
buys fifty kits instead of five thousand vehicle fittings. The kit follows the
cargo operation, which is also what the insurer and the ADR inspector care
about, because the risk travels with the gas and not with the van.

**Why two devices.**

Neither can do the other's job.

The camera catches a cylinder displaced or knocked over. It cannot see gas at
any resolution, because that is the wrong physics. Point it at four upright,
correctly secured cylinders and it will report the load is perfect, and it will
be right, while the van fills.

The gas sensor catches a leak long before it is dangerous. It has nothing to
say about a cylinder lying on its side, because a cylinder can lie there for an
hour without venting anything, right up until the valve shears.

Each is blind exactly where the other can see. That is why the van needs both,
and why it needs something to plug them into that can reach the horn. One
sensor on its own is a gadget in a crate.

## 3. Why this use case and not another

The organisers' own reference prototype is YOLO tool detection: missing
screwdriver, horn and hazards on the parking brake. Challenge 1 is taken.
Anyone demoing tool inventory today is demoing the sample back at them. The
driver drowsiness demo is also already built.

Challenge 2, dangerous goods, is untouched.

## 4. How it works

Arming is binding. It ties this kit to this vehicle, for this load, for this
hire. It reads the van's own identity out of the vehicle API and captures the
baseline: how many cylinders, how many upright, what the background gas reading
is. The app prints this at arming:

```
ARMED at 13:00:14. Kit CNY-014 is now bound to:
   vehicle : B-XY 1234, Ford Transit
   first registered : 2013-03-18
   load    : 4 cylinders, 4 upright
   gas     : 13 ppm background
```

The registration date is read live from `Vehicle.VehicleIdentification`, not
hardcoded. The van states its own age and makes our argument for us.

Everything afterwards is measured against that baseline. You cannot know a
cylinder moved unless you know where it started, so arming is what makes
displacement detection possible at all.

At disarm the hire ends, the kit comes off the van, and the record of the run
goes with the kit rather than the vehicle.

The response depends on what the van is doing. Stationary and alarming gets
hazards, high beam and the horn, because the van has not moved yet and the job
is to bring someone back to the load bay before a key turns. Moving and
alarming gets hazards and beams but no horn, because the driver is in traffic
and startling him is not help. Elevated but not alarming gets hazards only,
visible from outside before anyone opens a door.

Reading the vehicle's own motion state to decide this is what makes it a
vehicle application rather than a gadget with a buzzer.

## 5. Where we actually are

**Done and verified.** `app/main.py` is the whole app, 705 lines, one file,
standard library only, so the runtime installs nothing and starts immediately.
It runs end to end in about 33 seconds at demo speed. Both events work. Arming,
binding, baseline capture, threshold hysteresis and disarm all work. Vehicle
writes are confirmed: hazards, then hazards plus high beam plus three horn
pulses, then everything off.

**Three details that cost time to find. Do not undo them.**

The SDK import is `sdv`, not `velocitas_sdk`. The second raises ImportError on
this runtime and nothing runs at all. There is a fallback so it works on either.

Every signal is written as a literal attribute chain rather than assembled from
strings. The Playground scans the source text to build the Used APIs list, and
that list is what registers the mock signals. Resolve paths dynamically and the
panel comes up empty and the dashboard widgets sit there dead.

Output uses `print(flush=True)`, never `logging`. The demo is its terminal
output, and logging can be buffered or swallowed by the Playground console.

**Not done.** Both devices are simulated. The app prints SIMULATED on startup
and never presents a fake reading as live. Do not let anyone imply otherwise.
The gas sensor is in hand but not wired to an endpoint, and the camera path is a
stub.

**Open questions.** Do the two wishlist signals accept writes? They are typed
`[W]` with no datatype, so the databroker may refuse them. Running the app
answers it, and a refusal is handled without breaking anything. Second, is there
an endpoint for the Bosch gas sensor? Ask a mentor, it is a one line change.

## 6. Running it

Open the Playground, model `IAA Hannover Hackathon 2026`, prototype Canary Go,
a multi-files project. Put `app/main.py` and `app/requirements.txt` into the SDV
Code tab. The requirements file is empty on purpose.

Open the terminal with the arrow at bottom right, find the runtime box, choose
add runtime, enter `Runtime-<name from your supervisor>`, then run. The demo is
hands-free and narrates itself.

Check the Used APIs panel lists these eleven. If it is empty, stop and say so,
because the signals will not register:

```
Vehicle.Body.Horn.IsActive
Vehicle.Body.Lights.Hazard.IsSignaling
Vehicle.Body.Lights.Beam.High.IsOn
Vehicle.Chassis.ParkingBrake.IsEngaged
Vehicle.IsMoving
Vehicle.Cargo.LeakageDetectionSignal
Vehicle.GasLeakage
Vehicle.VehicleIdentification.LicensePlate
Vehicle.VehicleIdentification.Brand
Vehicle.VehicleIdentification.Model
Vehicle.VehicleIdentification.DateVehicleFirstRegistered
```

For the dashboard, add a Table-APIs-Widget:

```json
{
  "apis": [
    "Vehicle.Body.Lights.Hazard.IsSignaling",
    "Vehicle.Body.Horn.IsActive",
    "Vehicle.Body.Lights.Beam.High.IsOn",
    "Vehicle.IsMoving"
  ]
}
```

Four rows flip live as the van reacts. Two minutes, and nothing about it can
break.

If the wishlist signals accept writes, add a Chart-APIs-Widget for a live ppm
graph:

```json
{
  "chartType": "line",
  "signals": [{ "api": "Vehicle.Cargo.LeakageDetectionSignal" }]
}
```

To change pacing, `SAMPLE_SECONDS` and `DEMO_PAUSE` are at the top of the file.
Raise `DEMO_PAUSE` to 4 for more room to talk over it.

If the identity attributes come back empty on the runtime, the binding line
prints "unknown plate" and the demo carries on. It degrades, it does not break.

## 7. A platform gap worth naming in the pitch

Playground widgets can only display vehicle signals, meaning names from a fixed
standard list of 1273. There is no standard signal for the number of cylinders
in a load bay, and none for gas concentration. We checked all of them. The
concept does not exist in the vehicle standard because no carmaker ever needed
it.

We worked around it by adding two wishlist signals to the model,
`Vehicle.Cargo.LeakageDetectionSignal` and `Vehicle.GasLeakage`.

This is a real gap rather than an oversight on our part. Cargo state does not
exist in the vehicle standard and it should, which makes it a good answer to
"what would you build next".

## 8. Going live

For the gas sensor, change one line in the `# --- choose the device ---` block:

```python
SENSOR = SimulatedGasSensor()
# becomes
SENSOR = HttpGasSensor("https://<endpoint>/gas")
```

`HttpGasSensor` is already written, expects `{"ppm": 412}`, and already sends
the ngrok header. If the Bosch unit uses a different field name, pass
`field="..."`.

For the camera, change one method: `SimulatedLoadCamera.observe()` (or switch to `LiveLoadCamera`). Keep the return shape
`{"seen": n, "upright": n}` and nothing else in the file moves.

Toppled detection needs no second model. A standing cylinder's bounding box is
tall and a toppled one is wide, so `h / w > 1.3` means upright and anything else
means toppled. Count the boxes for the cylinder count and classify each by
ratio, both out of one YOLO pass.

You will need to uncomment requests, numpy and opencv-python-headless in
requirements.txt and add ultralytics, which makes startup slow, possibly two
minutes. Use YOLO-World with `set_classes(["gas cylinder", "bottle",
"cylinder"])`, which is open vocabulary and needs no training. The stream is at
`https://domain-reenter-boxer.ngrok-free.dev/video_feed_1`.

The host is up, confirmed. But it serves an ngrok warning page unless you send
the header `ngrok-skip-browser-warning: true`. That one detail silently eats
twenty minutes if nobody tells you. Copy the reference prototype's
`run_stream_worker` rather than writing your own.

## 9. The pitch

Five minutes, roughly.

Open with the market, before any screen. "There are millions of commercial vans
in Europe over ten years old. Not one of them will ever be a software-defined
vehicle. Every one of them has a horn."

Then the customer. "A rental fleet owns the van and the liability. It does not
own the load and did not pack it. Today it has no idea what is happening in the
back."

Then why the obvious answer fails. "You would put a camera in. We did. Watch
what it cannot do."

Run the demo, 33 seconds, hands-free. Then the line about neither device
covering the other's gap. Then portability: fifty kits, not five thousand
fittings, and the kit follows the cargo. Then honest status and what is next.
Leave a minute for questions.

### Narrating over the live run

At loading: "A hired van on a gas run. Four LPG cylinders in the back. The fleet
owns the van and the liability, not the load."

At arm, let the binding block sit on screen: "Watch what it binds to. Licence
plate, make, model, and first registered 2013. This van will never be
software-defined. It has a horn and hazard lights, and that is enough."

At event 1: "A strap was loose. The camera catches it. And notice the gas sensor
is silent, and it is right to be silent. A cylinder can lie on its side for an
hour without venting anything."

At event 2, slow down: "Re-secured, driving on. The camera is satisfied, because
the load looks exactly as it did at arming. Now watch the ppm column."

At the alarm, the important one: "Look at the camera column. Still OK. Still
correct. Four cylinders, upright, exactly as they were at arming. The camera is
not broken and it is not wrong. It is blind to this."

To close: "Ten minutes ago the camera caught what the gas sensor could not. Now
the gas sensor is catching what the camera cannot. Tomorrow this kit is in a
different van."

### Say this before they ask

"Both devices are simulated today. The gas sensor is in our hands but not wired,
and the camera path is stubbed. Going live is one line for the sensor and one
method for the camera. We spent the time on the loop instead, because the loop
is the contribution."

Volunteering that lands better than being caught on it.

### Judge questions

**Why not use a better camera?** Resolution is not the problem. A camera cannot
see gas at any resolution, because it is the wrong physics. That is what the
demo shows.

**Isn't a gas alarm a solved product?** Standalone gas alarms exist. What does
not exist is one that knows which vehicle it is in, whether that vehicle is
moving, and can use the vehicle's own horn. That needs the vehicle, which needs
the box.

**Why portable rather than fitted?** Because a rental fleet's vehicles rotate
constantly, and because the risk travels with the gas, not the van. Fifty kits
instead of five thousand fittings, and the safety record follows the cargo
operation across whatever vehicle it is in this week.

**Why would an old van have any of this?** It does not need to. It needs a horn,
hazard lights and a port. Those have been standard for sixty years. That is the
point: BYOD is the only route these vehicles will ever have.

**What if the sensor fails?** It holds its last known state and says so, rather
than reporting a failed read as a clean one. A safety system that says all clear
when it has gone blind is worse than no system.

**What would you build next?** Custom vehicle signals for cargo telemetry, per
section 7. Cargo state genuinely does not exist in the vehicle standard.

### Slides

Five. Market, customer, the two-device argument, the demo, honest status.

## 10. Who does what

Roughly in priority order.

1. Paste and run, then confirm the Used APIs panel lists eleven. Everything
   else depends on this.
2. Add the Table widget. Two minutes.
3. Rehearse out loud, twice, timed. This is the step everyone skips and then
   regrets.
4. Check whether the wishlist signals accept writes. If they do, add the ppm
   chart.
5. Chase a mentor for a gas sensor endpoint.
6. Slides.
7. Camera live, but only once 1 to 6 are done.

Stop adding things thirty minutes before the pitch. A rehearsed simple demo
beats an unrehearsed clever one every time. The story works with the camera
stubbed, because the camera's job in the pitch is to be correct and blind, and a
stub does that just as convincingly as the real thing.
