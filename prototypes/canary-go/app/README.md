# Canary Go: portable load bay monitor

One portable kit. Two devices. Two events neither could catch alone.

## Problem

A tradesman — welder, plumber, HVAC fitter — carries propane, oxygen or
acetylene cylinders in an enclosed van. Every day, not as ADR freight.

Two things can go wrong back there and he cannot see either of them from the
driver's seat:

- A cylinder shifts or falls. In a braking van it is a missile, and a sheared
  valve turns it into a rocket.
- A cylinder starts to leak. He cannot smell it over the engine. A sealed van
  filling with propane needs one ignition source — a starter motor, a phone —
  and the van and the driver are gone.

## Says who?

Anyone who has opened the back of a trade van. Cylinders travel loose, straps
work themselves loose over a day of stops, and nothing in the vehicle is
watching the load.

## Solution

The driver **arms** the system when he loads. Arming captures the baseline —
how many cylinders, how many upright, what the background gas reading is.
Everything after that is measured against it. You cannot know a cylinder
*moved* unless you know where it started.

While armed, two devices on the BYOD box watch the bay:

| Device | Catches | Blind to |
|---|---|---|
| Camera | A cylinder displaced or fallen | Gas. Wrong physics. |
| Gas sensor | A leak, long before it is dangerous | A fallen cylinder that has not vented yet |

**Neither covers the other's gap.** That is the argument for two devices, and
for something to plug them into that can reach the horn. One sensor on its own
is a gadget in a crate.

The vehicle's response scales with what it is doing:

- **Parked** — hazards, high beam, horn. Bring him back to the load bay
  *before* he turns a key inside a van full of propane.
- **Moving** — hazards and beams. Pull over, open the doors. No horn: he is in
  traffic and startling him is not help.
- **Disarmed** — nothing. He is working out of the van and his own hands should
  not set the horn off.

## Running it

`app/main.py`, standard library only — nothing to pip-install.

Dashboard: a Table-APIs-Widget bound to

```
Vehicle.Body.Lights.Hazard.IsSignaling
Vehicle.Body.Horn.IsActive
Vehicle.Body.Lights.Beam.High.IsOn
Vehicle.Chassis.ParkingBrake.IsEngaged
```

## Honest status

Both devices are **simulated** — the gas sensor is in hand but not yet wired,
and the camera path is stubbed. The app prints `SIMULATED` on startup and never
presents a simulated reading as a live one.

Going live is one line each: `SENSOR = HttpGasSensor("<endpoint>")` for the
Bosch unit, and the camera follows the same MJPEG pattern as the organisers'
reference prototype.

## Complexity

Medium.

## Status

Developing.
