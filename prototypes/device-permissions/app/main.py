"""
BYOD device onboarding and scoped permissions for a software-defined vehicle.
IAA Transportation LCV BYOD Hackathon.

THE PROBLEM
-----------
Every production SDV stack assumes a sensor set fixed at design time. AUTOSAR
Adaptive, VSS, SOME/IP all work from a signal tree the OEM defined years before
the van shipped. CarPlay and Android Auto project a phone screen - they never
enter the vehicle's data layer and never actuate anything. ISO 11992 trailer
buses are fixed-function.

So there is no answer to: a device the OEM has never heard of joins at runtime,
publishes into the vehicle's signal namespace, and gets permission to act.

WHAT ALREADY EXISTS
-------------------
The Kuksa databroker running in THIS runtime already has the primitive. Access
is an RFC 9068 JWT whose `scope` claim looks like:

    read:Vehicle.Speed
    actuate:Vehicle.Body.Horn.IsActive
    provide:data:Vehicle.Body

...with an `aud` claim binding the token to one vehicle. So "this device may
read speed and sound the horn, on this van, until Friday" is already
EXPRESSIBLE.

WHAT IS MISSING - AND WHAT THIS PROTOTYPE IS
--------------------------------------------
Nothing defines how a device GETS one. There is no discovery, no capability
declaration, no request-and-grant step, no revocation, and no record of who
approved what. Tokens are minted out of band by a developer holding a signing
key.

This is that missing layer: a device declares what it offers and what it needs,
a human decides, and a scoped, expiring grant is issued and enforced - in the
same scope grammar the databroker already understands.

HONEST SCOPE
------------
Grants are enforced in THIS layer, not signed by the databroker's key. Given the
key, the same manifest and the same decision would mint a real Kuksa token and
the databroker would do the enforcing. The flow is the contribution; the
signature is a deployment detail.

Runs on the digital.auto SDV runtime: `python -u app/main.py`. Standard library
only - nothing to pip-install, nothing to reach over the network.
"""

import asyncio
import time

from vehicle import vehicle  # type: ignore

# The runtime exposes the Velocitas SDK under the older `sdv` namespace - that is
# what the organisers' own reference prototype imports. Newer images use
# `velocitas_sdk`. Try both so this file runs on either.
try:
    from sdv.vehicle_app import VehicleApp  # type: ignore
except ImportError:  # pragma: no cover
    from velocitas_sdk.vehicle_app import VehicleApp  # type: ignore


class _Out:
    """print(flush=True), not logging.

    The entire demo is its terminal output, and the reference prototype uses
    print+flush throughout. Logging may be swallowed or buffered in the
    Playground console, which on this demo would mean showing the judges a
    blank screen.
    """

    @staticmethod
    def info(fmt, *args):
        print(fmt % args if args else fmt, flush=True)

    warning = info
    error = info


log = _Out()


# ===========================================================================
# 1. SCOPE GRAMMAR  (Kuksa-compatible)
# ===========================================================================

class Scope:
    """One permission, in the databroker's own grammar: ACTION:PATH.

    `actuate` implies `read`, and `provide` implies `read`, which is Kuksa's
    rule, not ours. Wildcards match a SINGLE level: Vehicle.Body.*.IsOpen does
    not match Vehicle.Body.Trunk.Rear.IsOpen. Getting that wrong is how a
    permission model quietly grants more than anyone intended.
    """

    IMPLIES = {
        "actuate": {"actuate", "read"},
        "provide": {"provide", "read"},
        "read": {"read"},
    }

    def __init__(self, text):
        parts = text.split(":")
        if len(parts) < 2:
            raise ValueError("scope must be ACTION:PATH, got %r" % text)
        self.action = parts[0]
        if self.action not in self.IMPLIES:
            raise ValueError("unknown action %r" % self.action)
        self.path = parts[-1]
        self.text = text

    def _path_covers(self, target):
        mine = self.path.split(".")
        theirs = target.split(".")
        # A branch scope covers everything under it: read:Vehicle.ADAS grants
        # read on Vehicle.ADAS.ABS.IsEnabled.
        if len(mine) > len(theirs):
            return False
        for i, segment in enumerate(mine):
            if segment == "*":
                continue          # single level only - never spans a dot
            if segment != theirs[i]:
                return False
        return True

    def covers(self, action, path):
        return action in self.IMPLIES[self.action] and self._path_covers(path)

    def __repr__(self):
        return self.text


# ===========================================================================
# 2. DEVICE MANIFEST
# ===========================================================================

class DeviceManifest:
    """What a device says about itself when it shows up.

    Deliberately small. A device that has to be pre-registered with the OEM is
    not a BYOD device.
    """

    def __init__(self, device_id, kind, owner, provides=None, requests=None, reason=""):
        self.device_id = device_id
        self.kind = kind
        self.owner = owner
        self.provides = list(provides or [])       # signals it can publish
        self.requests = [Scope(s) for s in (requests or [])]
        self.reason = reason                       # shown to the human, in words


# ===========================================================================
# 3. GRANT REGISTRY
# ===========================================================================

PENDING, GRANTED, DENIED, REVOKED, EXPIRED = "PENDING", "GRANTED", "DENIED", "REVOKED", "EXPIRED"


class Grant:
    def __init__(self, manifest, scopes, ttl, clock):
        self.manifest = manifest
        self.scopes = scopes
        self.ttl = ttl
        self._clock = clock
        self.state = PENDING
        self.issued_at = None

    def approve(self):
        self.state = GRANTED
        self.issued_at = self._clock()

    def revoke(self):
        self.state = REVOKED

    @property
    def expired(self):
        return (
            self.state == GRANTED
            and self.issued_at is not None
            and self._clock() - self.issued_at > self.ttl
        )

    def allows(self, action, path):
        """Returns (allowed, reason). An expired grant denies like a revoked one."""
        if self.expired:
            return False, "grant expired"
        if self.state != GRANTED:
            return False, "no grant (%s)" % self.state.lower()
        for scope in self.scopes:
            if scope.covers(action, path):
                return True, str(scope)
        return False, "%s:%s outside granted scopes" % (action, path)


class GrantRegistry:
    """Who asked for what, who decided, and when. Append-only decision log."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._t0 = clock()          # audit times are relative to app start,
        self.grants = {}            # not to machine boot - readable on stage
        self.audit = []

    def _record(self, device_id, event, detail):
        entry = (round(self._clock() - self._t0, 1), device_id, event, detail)
        self.audit.append(entry)
        return entry

    def onboard(self, manifest):
        grant = Grant(manifest, list(manifest.requests), ttl=0, clock=self._clock)
        self.grants[manifest.device_id] = grant
        self._record(manifest.device_id, "ONBOARD", "requested %s" % manifest.requests)
        return grant

    def approve(self, device_id, scopes=None, ttl=3600):
        """Approve, optionally NARROWING what was asked for.

        Narrowing matters: the operator granting less than the device requested
        is the normal case, not an edge case.
        """
        grant = self.grants[device_id]
        if scopes is not None:
            grant.scopes = [Scope(s) for s in scopes]
        grant.ttl = ttl
        grant.approve()
        self._record(device_id, "APPROVE", "%s for %ss" % (grant.scopes, ttl))
        return grant

    def revoke(self, device_id):
        grant = self.grants[device_id]
        grant.revoke()
        self._record(device_id, "REVOKE", "operator revoked")
        return grant

    def check(self, device_id, action, path):
        grant = self.grants.get(device_id)
        if grant is None:
            return False, "device not onboarded"
        return grant.allows(action, path)


# ===========================================================================
# 4. ENFORCEMENT
# ===========================================================================

class GuardedVehicle:
    """Every signal write passes through here. No grant, no databroker call.

    The important property: a denial performs NO write. Not a write that is
    later undone - no write at all.
    """

    def __init__(self, vehicle_client, registry):
        self.vehicle = vehicle_client
        self.registry = registry
        self.writes = []          # what actually reached the databroker
        self.denials = []

    def _resolve(self, path):
        node = self.vehicle
        for part in path.replace("Vehicle.", "", 1).split("."):
            node = getattr(node, part)
        return node

    async def actuate(self, device_id, path, value):
        allowed, why = self.registry.check(device_id, "actuate", path)
        if not allowed:
            self.denials.append((device_id, path))
            self.registry._record(device_id, "DENIED", "%s -> %s" % (path, why))
            log.warning("   DENIED  %s may not actuate %s  (%s)", device_id, path, why)
            return False

        try:
            await self._resolve(path).set(value)
        except AttributeError:
            log.error("   signal %s is not in this vehicle model - check the API catalogue", path)
            return False
        except Exception as exc:
            log.error("   databroker refused %s: %s", path, exc)
            return False

        self.writes.append((device_id, path, value))
        self.registry._record(device_id, "ACTUATE", "%s = %s (via %s)" % (path, value, why))
        log.info("   GRANTED %s actuated %s = %s  (via %s)", device_id, path, value, why)
        return True

    async def read(self, device_id, path):
        allowed, why = self.registry.check(device_id, "read", path)
        if not allowed:
            self.registry._record(device_id, "DENIED", "read %s -> %s" % (path, why))
            log.warning("   DENIED  %s may not read %s  (%s)", device_id, path, why)
            return None
        try:
            value = (await self._resolve(path).get()).value
        except Exception as exc:
            log.error("   could not read %s: %s", path, exc)
            return None
        log.info("   GRANTED %s read %s = %s", device_id, path, value)
        return value


# ===========================================================================
# 5. CONFIGURATION
# ===========================================================================
# Taken from the API catalogue of vehicle model "IAA Hannover Hackathon 2026"
# and confirmed against the organisers' own reference prototype. Note it is
# Beam.HIGH on this model - Beam.Low is not in the catalogue.
#
#   Vehicle.Body.Horn.IsActive                    ACTUATOR
#   Vehicle.Body.Lights.Beam.High.IsOn            ACTUATOR
#   Vehicle.Body.Lights.Hazard.IsSignaling        ACTUATOR
#   Vehicle.Chassis.ParkingBrake.IsEngaged        ACTUATOR

HORN = "Vehicle.Body.Horn.IsActive"
HIGH_BEAM = "Vehicle.Body.Lights.Beam.High.IsOn"
HAZARDS = "Vehicle.Body.Lights.Hazard.IsSignaling"
PARKING_BRAKE = "Vehicle.Chassis.ParkingBrake.IsEngaged"

BEAT_PAUSE = 2.5      # seconds between demo beats - room to narrate
ALERT_HOLD = 1.5      # how long the horn stays on


TOOLCRATE = DeviceManifest(
    device_id="TOOLCRATE-7",
    kind="Smart tool crate",
    owner="Contractor (not the fleet, not the OEM)",
    provides=["Cargo.ToolCrate.ItemsPresent", "Cargo.ToolCrate.LidOpen"],
    requests=[
        "actuate:Vehicle.Body.Horn.IsActive",
        "actuate:Vehicle.Body.Lights.Hazard.IsSignaling",
        "actuate:Vehicle.Body.Lights.Beam.High.IsOn",
        # It also asks to stop the van from being driven away. That is a
        # safety-critical actuator, and a tool crate has no business holding it.
        "actuate:Vehicle.Chassis.ParkingBrake.IsEngaged",
    ],
    reason="Warn the driver if a tool is left behind at a job site",
)

GAS_SENSOR = DeviceManifest(
    device_id="GASSENSE-2",
    kind="Bosch gas sensor on a cylinder rack",
    owner="Haulier",
    provides=["Cargo.Rack.GasPPM"],
    requests=[
        "actuate:Vehicle.Body.Horn.IsActive",
        "actuate:Vehicle.Body.Lights.Hazard.IsSignaling",
    ],
    reason="Sound an alarm if a cylinder leaks in the load bay",
)


# ===========================================================================
# 6. THE DEMO
# ===========================================================================

def banner(number, title):
    log.info("")
    log.info("=" * 68)
    log.info("  %d. %s", number, title)
    log.info("=" * 68)


def show_request(manifest):
    """The request, in words a fleet operator would actually use."""
    log.info("   Device : %s  (%s)", manifest.device_id, manifest.kind)
    log.info("   Owner  : %s", manifest.owner)
    log.info("   Because: %s", manifest.reason)
    log.info("   Offers : %s", ", ".join(manifest.provides))
    log.info("   Wants  :")
    for scope in manifest.requests:
        verb = "sound / switch" if scope.action == "actuate" else "see"
        log.info("            - %s %s      [%s]", verb, scope.path, scope)


class ByodPermissionsApp(VehicleApp):
    def __init__(self, vehicle_client):
        super().__init__()
        self.registry = GrantRegistry()
        self.guard = GuardedVehicle(vehicle_client, self.registry)

    async def _try_alert(self, device_id, label):
        """A device attempting the same action throughout the demo.

        The call is IDENTICAL every time it runs. Only the permission changes.
        That is the whole point - nothing about the device changes between a
        refusal and a success.
        """
        log.info("   >> %s attempts to alert the driver...", device_id)
        acted = False
        for path in (HAZARDS, HIGH_BEAM, HORN):
            if await self.guard.actuate(device_id, path, True):
                acted = True
        if acted:
            await asyncio.sleep(ALERT_HOLD)
            for path in (HORN, HIGH_BEAM, HAZARDS):
                await self.guard.actuate(device_id, path, False)
            log.info("   %s <<< the vehicle reacted. Watch the dashboard.", label)
        else:
            log.info("   %s <<< nothing happened. The vehicle did not move a muscle.", label)
        return acted

    async def on_start(self):
        await asyncio.sleep(1)

        log.info("")
        log.info("#" * 68)
        log.info("#  BYOD: how does a device the OEM never heard of earn the right")
        log.info("#  to act on a vehicle?")
        log.info("#" * 68)

        # --- 1 ---------------------------------------------------------
        banner(1, "A device appears")
        self.registry.onboard(TOOLCRATE)
        show_request(TOOLCRATE)
        log.info("")
        log.info("   Nothing has been granted. It has only ASKED.")
        await asyncio.sleep(BEAT_PAUSE)

        # --- 2 ---------------------------------------------------------
        banner(2, "Before approval: the device tries anyway")
        await self._try_alert(TOOLCRATE.device_id, "[BLOCKED]")
        log.info("")
        log.info("   This is the part that matters. An unapproved device is not")
        log.info("   trusted-by-default - it is refused, and the refusal is logged.")
        await asyncio.sleep(BEAT_PAUSE)

        # --- 3 ---------------------------------------------------------
        banner(3, "The operator decides - and grants LESS than was asked")
        log.info("   Asked for : horn, hazards, high beam, AND the parking brake")
        log.info("   Granted   : horn + hazards + high beam, for 30 seconds.")
        log.info("               Parking brake REFUSED.")
        log.info("")
        log.info("   Narrowing is the normal case, not an edge case. Warning the")
        log.info("   driver is reasonable. Holding the van still is a safety")
        log.info("   function, and a tool crate does not get to own it.")
        self.registry.approve(
            TOOLCRATE.device_id,
            scopes=["actuate:Vehicle.Body.Horn.IsActive",
                    "actuate:Vehicle.Body.Lights.Hazard.IsSignaling",
                    "actuate:Vehicle.Body.Lights.Beam.High.IsOn"],
            ttl=30,
        )
        await asyncio.sleep(BEAT_PAUSE)

        # --- 4 ---------------------------------------------------------
        banner(4, "After approval: the same call, now permitted")
        await self._try_alert(TOOLCRATE.device_id, "[ALLOWED]")
        await asyncio.sleep(BEAT_PAUSE)

        # --- 5 ---------------------------------------------------------
        banner(5, "The withheld permission is genuinely withheld")
        log.info("   Same device, same grant. It now tries the one thing it asked")
        log.info("   for and did NOT get - engaging the parking brake:")
        log.info("")
        await self.guard.actuate(TOOLCRATE.device_id, PARKING_BRAKE, True)
        log.info("")
        log.info("   Approval was per-signal, not all-or-nothing. The device is")
        log.info("   trusted with the horn and not with the brakes, at the same")
        log.info("   time, from the same grant.")
        await asyncio.sleep(BEAT_PAUSE)

        # --- 6 ---------------------------------------------------------
        banner(6, "Revocation - the van goes back to the rental company")
        self.registry.revoke(TOOLCRATE.device_id)
        log.info("   Grant revoked. The device has not changed. Its code has not")
        log.info("   changed. It simply no longer has permission.")
        log.info("")
        await self._try_alert(TOOLCRATE.device_id, "[BLOCKED]")
        await asyncio.sleep(BEAT_PAUSE)

        # --- 7 ---------------------------------------------------------
        banner(7, "A different device, same flow - this is what scales")
        self.registry.onboard(GAS_SENSOR)
        show_request(GAS_SENSOR)
        log.info("")
        log.info("   A camera would onboard exactly the same way. So would a")
        log.info("   phone, a trailer sensor, a wearable. The vehicle does not")
        log.info("   need to know what the device IS - only what it asks for.")
        self.registry.approve(
            GAS_SENSOR.device_id,
            scopes=["actuate:Vehicle.Body.Horn.IsActive"],
            ttl=3600,
        )
        log.info("")
        log.info("   Note it was granted the horn but NOT the lights, so the")
        log.info("   next attempt is part-allowed and part-refused, signal by signal:")
        await self._try_alert(GAS_SENSOR.device_id, "[PARTLY ALLOWED]")
        await asyncio.sleep(BEAT_PAUSE)

        # --- audit -----------------------------------------------------
        banner(8, "Every decision, on the record")
        for when, device, event, detail in self.registry.audit:
            log.info("   %7.1fs  %-12s %-8s %s", when, device, event, detail)

        log.info("")
        log.info("=" * 68)
        log.info("  Writes that reached the databroker : %d", len(self.guard.writes))
        log.info("  Attempts refused                   : %d", len(self.guard.denials))
        log.info("=" * 68)
        log.info("")
        log.info("  Kuksa already understands these scope strings. What was missing")
        log.info("  is how an unknown device gets one. That is this layer.")
        log.info("")

        # Leave the vehicle as we found it.
        for path in (HORN, HIGH_BEAM, HAZARDS, PARKING_BRAKE):
            try:
                await self.guard._resolve(path).set(False)
            except Exception:
                pass


# ===========================================================================
# SELF-CHECK - runs before the demo, fails loudly rather than on stage
# ===========================================================================

def self_check():
    actuate_horn = Scope("actuate:Vehicle.Body.Horn.IsActive")
    assert actuate_horn.covers("actuate", "Vehicle.Body.Horn.IsActive")
    assert actuate_horn.covers("read", "Vehicle.Body.Horn.IsActive"), "actuate implies read"
    # The near-miss that matters: one granted signal must not grant its neighbour.
    # The near-misses that matter on THIS model: one granted actuator must not
    # drag its neighbours along with it.
    assert not actuate_horn.covers("actuate", "Vehicle.Body.Lights.Hazard.IsSignaling")
    assert not actuate_horn.covers("actuate", "Vehicle.Chassis.ParkingBrake.IsEngaged")

    branch = Scope("read:Vehicle.ADAS")
    assert branch.covers("read", "Vehicle.ADAS.ABS.IsEnabled"), "branch covers children"
    assert not branch.covers("actuate", "Vehicle.ADAS.ABS.IsEnabled"), "read is not actuate"

    star = Scope("read:Vehicle.Body.*.IsOpen")
    assert star.covers("read", "Vehicle.Body.Trunk.IsOpen")
    assert not star.covers("read", "Vehicle.Body.Trunk.Rear.IsOpen"), "wildcard is single-level"

    # An expired grant must deny exactly like a revoked one.
    now = [0.0]
    registry = GrantRegistry(clock=lambda: now[0])
    registry.onboard(TOOLCRATE)
    registry.approve(TOOLCRATE.device_id, scopes=[str(actuate_horn)], ttl=10)
    assert registry.check(TOOLCRATE.device_id, "actuate", HORN)[0]
    now[0] = 11.0
    allowed, why = registry.check(TOOLCRATE.device_id, "actuate", HORN)
    assert not allowed and "expired" in why, why

    log.info("self-check passed: scope grammar, narrowing, expiry")


async def main():
    self_check()
    await ByodPermissionsApp(vehicle).run()


if __name__ == "__main__":
    # Same bootstrap as the organisers' reference prototype, so this app fails
    # (or doesn't) in exactly the same way their known-good sample does.
    import signal

    LOOP = asyncio.get_event_loop()
    try:
        LOOP.add_signal_handler(signal.SIGTERM, LOOP.stop)
    except NotImplementedError:
        pass
    try:
        LOOP.run_until_complete(main())
    finally:
        LOOP.close()
