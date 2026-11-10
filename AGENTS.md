# AGENTS.md — contract for dbus-teleinfo-esphome

Read this before touching the driver. Every item below was measured on a real
installation or read from the Venus OS sources; nothing here is a preference.

## Scope

One teleinfo meter, already decoded by an ESPHome node, published to Venus OS as
a grid meter (`com.victronenergy.grid`). The driver is a translator: it converts,
it never decides what the house should do, and it is the only consumer of the
ESPHome web server.

## Layer boundaries — do not mix them

| Layer | Rule |
|---|---|
| `teleinfo.py` | pure decoding: payloads in, `Snapshot` out. No I/O, no clock, no D-Bus. If it needs the network, it is the wrong layer. |
| `esphome_http.py` | HTTP only: one request per entity, one exception type. It knows nothing about tariffs, energy or D-Bus. |
| `dbus-teleinfo.py` | the only file allowed to touch D-Bus, threads, files or the clock. |

The pure layer holds every conversion that can be silently wrong — the sign of
the power, the sum of the tariff registers, the "no data" rule — which is why it
is the only layer the laptop tests exercise.

## Invariants (assert them, do not comment them)

1. `/Ac/Power` follows the Victron convention: **positive imports, negative
   exports**. Export is `SINSTI`, a separate positive counter, never a negative
   `SINSTS`.
2. The published forward energy is the **complete** sum of the configured tariff
   registers, or the previous complete value. Never a partial sum, never a value
   that goes down.
3. `/Connected` describes data freshness, not HTTP health: three missed power
   reads and the meter is absent.
4. When disconnected, the instantaneous values are `None`; the cumulative counters
   keep their last value.
5. No HTTP on the GLib thread. Ever.
6. Every D-Bus path is declared before it is written.
7. No site data in this repository: no addresses, no serials, no meter readings.

## Traps

### The entity is the *name*, with its case and its spaces

The web server serves `/<domain>/<name>`; the YAML `id:` is not in the URL, and a
teleinfo configuration that names `SINSTS` as `PAPP` is reached at
`sensor/PAPP`. Two escapes are mandatory and were measured:

- `PAPP 1` → `sensor/PAPP%201`, because the space is literal;
- `njouf+1` → `sensor/njouf%2B1`, because a bare `+` in a URL means a space and
  the server would answer 404.

Quoting everything (`quote(object_id, safe='')`) is the only version that cannot
be wrong.

### `ntarf` is lower case while `NTARF` is the tag

In the widely copied teleinfo ESPHome YAML, the tariff index is named `ntarf`
while every other sensor keeps the tag spelling (`PAPP`, `Injection`, and the
`bbr*` registers). `sensor/NTARF` answers 404 with no other symptom than an
unknown tariff period — the fallback to `PTEC` hides it. Verify with `--probe`
rather than by reading the YAML.

### `null` is not zero

A sensor that never published answers `{"value": null, "state": "NA"}`. On a
consumption-only contract there is no `SINSTI` at all, so `sensor/Injection` is
null forever and that is normal. Turning it into 0 is right for the *export
power* (nothing is fed in) and wrong for a missing *import* (an absent meter is
not a house consuming nothing).

### `SINSTS` is apparent power, and the example labels it `W`

Standard-mode teleinfo carries no active power and no power factor. `SINSTS` is
in VA; the published ESPHome YAML sets `unit_of_measurement: "W"` on it, which
does not change the number. `/Ac/Power` is therefore VA read as W: a few percent
high on a poor power factor, and never a sign error. Do not "fix" it by inventing
a power factor.

### A partial energy sum is a rollback

VRM and the GUI both treat a grid counter as monotonic and build daily energy
from the difference between two logged totals. If one of the six `EASF` registers
is unreadable and the total is published anyway, the drop is read as a negative
consumption and the day is corrupted. `total_energy()` returns `None` for an
incomplete set, and the service holds the previous value.

A genuine regression (meter replaced, ESPHome index reset) is held for the same
reason. It is visible in the log and resolves on a restart, which clears the
in-memory reference.

### The GX is on its own VLAN

Measured with a default inter-VLAN block: the TCP connect to the ESP does not get
a reset, it **times out** (errno 11). The driver then looks dead while the ESP is
healthy and the ESPHome API, HA and the GUI all work. `--probe` is the tool that
separates the two, and `install.sh` refuses to install when it fails.

### systemcalc registers the service, not its `Connected`

`delegates/acinput.py` adds every `com.victronenergy.grid.*` service to its meter
list when it appears on the bus and reads `/Ac/<phase>/Power` from it without
looking at `/Connected`. A driver that only drops `/Connected` therefore keeps
contributing its **last** value to the system. Publishing `None` on the powers is
what actually removes the meter from the sums.

### The lowest device instance wins

`_get_meter()` sorts the grid meters by instance and keeps the first. A test
instance must have a **higher** instance than the real one, or it silently
replaces it in `/Ac/Grid/*`.

### Publish `/Ac/L1/Power`, not only `/Ac/Power`

systemcalc's consumption computation reads `/Ac/<phase>/Power` (and `Current`)
phase by phase. A driver that publishes only the total leaves
`/Ac/Consumption/*` empty while the grid tile still looks right.

### The Modbus unit id is the device instance, not `100 + instance`

Measured on 3.70: instance 40 answers on unit **40**. `unitid2di.csv` carries no
grid entry, so the gateway falls back to matching `/DeviceInstance`. Copying the
`100 + instance` rule from older Victron documentation gets a silent timeout and
a support conversation about a meter that "does not exist". Verify by reading
register 2638 on the unit that should serve `/Ac/Power`.

Also: a `None` value is published as an **empty array** on the bus, which is
Venus's "invalid", not a zero. That is what an unread tariff or an offline power
looks like to `dbus-send` and to the GUI.

### `/service` is volatile, `/opt/victronenergy/service` is not

`/etc/init.d/overlays.sh` copies `/opt/victronenergy/service/*` into
`/run/overlays/service` and bind-mounts it on `/service` at every boot. A runit
service written only into `/service` therefore works until the next reboot and
then disappears — the meter simply is not on the bus any more, with no error
anywhere. Both copies are written: `/opt/...` for the reboot, `/service` for the
running system. A firmware update replaces the rootfs (and `/opt`), which is what
`install.sh` is for.

The sibling `dbus-r4850G2-charger` installs into `/service` only and has this
failure mode; check it before relying on that driver after a power cut.

### The D-Bus main loop is single threaded

An HTTP read that waits for its timeout inside a GLib callback freezes the
service, and a frozen service disappears from the GUI without a message. The MPP
driver paid for this already: the poll runs in a thread, and the loop only
publishes what the thread left behind.

### The web server closes every connection

`Connection: close`, one entity per request, and an ESP8266 has a handful of
sockets. Do not open a long-lived stream next to this driver, do not parallelise
the polls, and do not add a keep-alive the server does not implement. The
10-second `update_interval` of the teleinfo component sets the useful poll rate;
polling faster reads the same number again.

### Adding a grid meter changes the installation, on purpose

With a grid meter on the bus, systemcalc guesses the AC source from its presence
and recomputes consumption. That is the requested behaviour, and it is a visible
change in the GUI and VRM — take a before/after reading rather than assuming the
rest of the dashboard is untouched.

### Units are a one-way door

`energy_in_kwh` decides a factor of 1000. Flipping it on a running service
publishes a jump of three orders of magnitude in a monotonic counter. Change it
with the service stopped, and accept that VRM will record one discontinuity.

## Testing rules

- Decoding tests assert numbers, not shapes: `(HP, blue)` from `HPJB`, and the
  exact sum of six registers.
- The energy tests must include the incomplete set and the backwards value; those
  two are the failure modes that survive a naive test suite.
- HTTP tests run against a local server that speaks the real payload shape and
  **record the request path**: quoting bugs are invisible in a mocked client.
- A test asserting that a partial sum is published is a bug in the test.
- `--self-test` must keep working with no network: it is what runs on a laptop
  and in CI.

## Conventions

- No runtime dependency outside the standard library; the GX provides Python 3,
  `dbus-python` and `gi`, nothing else.
- Install under `/data/etc/dbus-teleinfo-esphome`; state and settings under
  `/data/etc/dbus-teleinfo-state`; logs under `/var/log/dbus-teleinfo`; runit
  service under `/opt/victronenergy/service/teleinfo-grid` (persistent) and
  `/service/teleinfo-grid` (live). `/data` survives a firmware update, the rootfs
  does not.
- Service name `com.victronenergy.grid.esphome-teleinfo`, instance 40 by default.
- This repository is public. A host name, an address, a serial or a meter reading
  belongs in `settings.json` on the device, never in a commit.
