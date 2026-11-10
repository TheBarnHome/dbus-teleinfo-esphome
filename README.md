# dbus-teleinfo-esphome for Venus OS

Publish a French **teleinfo** meter (Linky / Tempo / HP-HC) as a **grid meter** on a
Victron **Venus OS** device, using the values an **ESPHome** node already decodes
from the TIC. The meter then exists for the GX itself: the GUI draws it, systemcalc
sums it, and **VRM** shows the installation's grid and its energy totals.

Sibling projects, same conventions: [dbus-mpp-p18](https://github.com/TheBarnHome/dbus-mpp-p18)
and [dbus-r4850G2-charger](https://github.com/TheBarnHome/dbus-r4850G2-charger)
(D-Bus service on Venus OS, `/data/etc` install, persistent state outside the Git
checkout, serial-indexed identity).

---

## Why a translation layer

1. **The TIC already has a reader.** The ESPHome node sits on the teleinfo wire,
   decodes the frames and serves every value over its own HTTP API. A second
   reader on the same wire would be a second point of failure for no extra
   information.
2. **VRM does not know what teleinfo is.** It knows `com.victronenergy.grid`, with
   `/Ac/Power` and `/Ac/Energy/Forward`. The whole job is a translation, and the
   translation rules are the only interesting part: the sign of the power, the
   sum of the six tariff registers, and what "no data" means.
3. **Home Assistant already has the data.** This adds nothing to HA; it makes the
   GX, and therefore VRM, see the grid that HA already reads.

## Topology

```
 TIC (Linky) ──UART──► ESPHome node ──HTTP /sensor/... ──► this service ──D-Bus──► GX
                        (teleinfo + web_server)                 │                  ├─ systemcalc → /Ac/Grid/*
                                                                │                  ├─ GUI v2
                                                                │                  └─ vrmlogger → VRM
```

## What Venus OS does with it

Measured on Venus OS v3.70, by reading the installed sources:

- `dbus-systemcalc-py/delegates/acinput.py` picks up any service whose name starts
  with `com.victronenergy.grid`, keeps them by device instance, and uses the
  **lowest** instance as *the* grid meter.
- systemcalc then publishes `/Ac/Grid/L1/Power`, `/Ac/Grid/L1/Current`,
  `/Ac/Grid/NumberOfPhases`, `/Ac/Grid/ProductId` — the paths the GUI and VRM read
  for the grid tile.
- `vrmlogger/datalist.py` has a `com.victronenergy.grid` section, so every
  `/Ac/Lx/Power`, `/Ac/Lx/Voltage`, `/Ac/Lx/Current`, `/Ac/Frequency` and
  `/Ac/Energy/*` published here is logged to VRM, with `/ProductId`,
  `/Serial`, `/FirmwareVersion` and `/CustomName` as the device identity.
- `dbus-modbustcp/attributes.csv` maps the same service to registers 2600-2648,
  so a Modbus client reads it with no extra work. The unit id is the device
  instance — **40 answered for instance 40** on 3.70, not the `100 + instance`
  some Victron documents describe; `unitid2di.csv` has no grid entry, so the
  gateway falls back to matching the instance. Check it before wiring Home
  Assistant: `read 2638` on the unit returns `/Ac/Power`.

## Requirements

| Piece | What |
|---|---|
| GX | Venus OS 3.x, Python 3 + `dbus-python` + `gi` (stock), `velib_python` as a submodule |
| ESPHome | a `teleinfo:` component and `web_server:` (the web server v2 API, not only the native API) |
| Network | the GX must be able to open `http://<esphome-host>:80/sensor/<entity>` |
| Python | nothing outside the standard library at runtime |

### The network rule is not optional

A GX usually sits on its own VLAN. Measured on a UniFi setup with the default
"block inter-VLAN" policy in place: the GX received **no answer at all** (TCP
connect error 11, no RST), which looks exactly like a dead ESP. One policy is
enough — source *GX address*, destination *ESP address*, TCP port 80 — and the
driver proves it in one command:

```sh
python3 dbus-teleinfo.py --probe --host <esphome-host>
```

That prints every entity it would use, the decoded grid values, and the D-Bus map,
without registering anything. It exits non-zero if the power cannot be read.

## Install

On the Venus OS device, as root:

```sh
cd /data/etc
git clone --recurse-submodules https://github.com/TheBarnHome/dbus-teleinfo-esphome
cd dbus-teleinfo-esphome
sh install.sh --host <esphome-host>            # add --test for a second instance (41)
```

`install.sh` checks `dbus`/`gi`, initialises `velib_python`, fast-forwards the
checkout, writes `/data/etc/dbus-teleinfo-state/settings.json`, **runs the probe
and refuses to install if the link is dead** (`--force` overrides), writes
`/service/teleinfo-grid/run`, and restarts the service.

A firmware update restores the stock `/service` tree: re-run the script
afterwards. Nothing is written outside `/data` and `/service`.

Then:

```sh
python3 verify-runtime.py            # reads the service back from D-Bus
python3 verify-runtime.py --system   # and what systemcalc made of it
```

## Configuration

`/data/etc/dbus-teleinfo-state/settings.json`, created by `install.sh`:

```json
{
  "host": "<esphome-host>",
  "port": 80,
  "poll_interval_s": 10,
  "slow_interval_s": 60,
  "stale_after_s": 35,
  "http_timeout_s": 3.0,
  "device_instance": 40,
  "serial": "teleinfo",
  "product_name": "ESPHome teleinfo",
  "custom_name": "Grid (teleinfo)",
  "energy_in_kwh": true,
  "service_name": "com.victronenergy.grid.esphome-teleinfo",
  "layout": {
    "power_import": "sensor/PAPP",
    "power_export": "sensor/Injection",
    "tariff": "text_sensor/PTEC",
    "tariff_index": "sensor/ntarf",
    "colour_tomorrow": "sensor/njouf+1",
    "energy_indexes": [
      "sensor/bbrhcjb", "sensor/bbrhpjb", "sensor/bbrhcjw",
      "sensor/bbrhpjw", "sensor/bbrhcjr", "sensor/bbrhpjr"
    ],
    "energy_reverse": null
  }
}
```

| Key | Meaning |
|---|---|
| `host`, `port` | the ESPHome web server; the only site-specific values, never in this repo |
| `poll_interval_s` | power read period. The teleinfo component refreshes every `update_interval` (10 s in the published YAML), so polling faster only adds traffic |
| `slow_interval_s` | tariff labels and the six energy registers; they move once a day |
| `stale_after_s` | no successful power read for this long → `/Connected = 0` (default 35 s = 3 fast cycles + margin) |
| `device_instance` | grid meter instance; anything unused works, 40 by convention here |
| `serial` | VRM device identity, keep it stable across updates |
| `energy_in_kwh` | `true` when the ESPHome sensors already apply the 0.001 filter (the published example does); `false` divides by 1000 (Wh) |
| `layout` | which ESPHome entity carries which role |

### Entity naming

The web server keys entities on their **name**, not on the YAML `id`, and keeps the
case:

| Role | Default entity | Note |
|---|---|---|
| `power_import` | `sensor/PAPP` | `SINSTS`, apparent power drawn |
| `power_export` | `sensor/Injection` | `SINSTI`; absent on a consumption-only contract, which is normal |
| `tariff` | `text_sensor/PTEC` | `"HPJB"`, `"HCJR"`, ... |
| `tariff_index` | `sensor/ntarf` | **lower case**: the published YAML names it `ntarf` while the tag is `NTARF` |
| `colour_tomorrow` | `sensor/njouf+1` | `NJOURF+1`; 0 means "no coloured day", not blue |
| `energy_indexes` | the six `bbr*` sensors | `EASF01..06`, one per tariff; their **sum** is the forward counter |

A bare name (`PAPP`) is read as `sensor/PAPP`; a text sensor must carry its domain
(`text_sensor/PTEC`). Spaces and `+` are URL-escaped (`PAPP 1` → `PAPP%201`,
`njouf+1` → `njouf%2B1`), because a literal `+` in a URL means a space.

## D-Bus map

Service `com.victronenergy.grid.esphome-teleinfo`, instance 40:

| Path | Meaning |
|---|---|
| `/Ac/Power`, `/Ac/L1/Power` | grid power, **positive = import, negative = export** |
| `/Ac/NumberOfPhases` | 1 unless `layout.phase_powers` lists per-phase entities |
| `/Ac/Energy/Forward` | sum of the six tariff registers, kWh |
| `/Ac/Energy/Reverse` | injected energy, `0` unless `layout.energy_reverse` is mapped |
| `/Connected` | 1 while the power read is fresh |
| `/Teleinfo/PowerImport`, `/PowerExport` | raw apparent power, VA |
| `/Teleinfo/Tariff`, `/PricePeriod`, `/Colour`, `/ColourTomorrow` | `HPJB`, `HP`, `blue`, `blue` |
| `/Teleinfo/Endpoint` | what is being read |
| `/Teleinfo/Poll/Error`, `/Errors`, `/Cycles` | last failure, failure count, completed cycles |

Publishing the counter **before** the first power value is deliberate: VRM's daily
energy is built from the difference between two logged totals, so a meter that
appears with power but no counter loses its first day.

## Limits worth knowing

- **VA published as W.** Standard-mode teleinfo gives `SINSTS`, an *apparent*
  power in VA; there is no active power and no power factor on the wire. The
  published ESPHome example even labels it `W`. `/Ac/Power` is therefore `SINSTS`
  taken as active power: a few percent high on a house with a poor power factor,
  never a sign error.
- **No voltage, current or frequency.** Standard mode does not carry them; the
  corresponding paths are left out rather than filled with a guess.
- **Single phase by default.** Set `layout.phase_powers` to the three `SINSTS1..3`
  entities for a three-phase meter; the service then publishes `/Ac/L1..L3/Power`
  and `NumberOfPhases = 3`.
- **Adding a grid meter changes the installation's model.** systemcalc guesses the
  AC source from the presence of a grid meter, and the consumption figure changes
  accordingly. That is the point of the driver, but it is a visible change in the
  GUI and in VRM: compare the dashboard before and after.

## Failure behaviour

| Situation | What happens |
|---|---|
| ESP reboots / Wi-Fi drops | three missed cycles → `/Connected = 0`; `/Ac/Power` and the per-phase powers go to `None` so the grid tile blanks instead of showing a frozen number; the cumulative counters keep their last value |
| One entity 404s (renamed) | the rest keeps working; the error is visible in `/Teleinfo/Poll/Error` and the log |
| One tariff register is unreadable | the forward total is **held**, never published as a partial sum: a smaller total reads as a meter rollback and corrupts the energy history |
| The GX cannot reach the ESP | the service stays registered with `/Connected = 0` and logs the reason; `--probe` tells a firewall problem from a dead ESP |

## Tests

No dependency beyond the standard library:

```sh
./run-tests.sh                     # 49 tests + the offline self-test
python3 dbus-teleinfo.py --self-test   # decode a recorded answer, print the D-Bus map
```

The decoding tests use the payload shape captured from a live node with stand-in
values; the HTTP tests run against a local stand-in for the ESPHome web server and
assert the exact request paths, which is what catches URL-quoting mistakes.

## Status

| Piece | State |
|---|---|
| Decoding, tariff, energy rules | done, unit tested |
| HTTP client, URL quoting, failure paths | done, tested against a stand-in server |
| D-Bus `com.victronenergy.grid` service | written |
| Poll thread (never blocks D-Bus) | written |
| `--probe` against a real ESPHome node | **measured**: 11/11 entities, `HPJB` |
| `install.sh` + runit service | **installed** on the GX, up, `/Connected = 1` |
| systemcalc picks it up | **measured**: `/Ac/Grid/L1/Power` went from an empty value to 914 W |
| Modbus TCP | **measured**: unit 40, register 2638 = 896 W, 2634 = 82 783.69 kWh |
| VRM | vrmlogger logged `Found: com.victronenergy.grid.esphome-teleinfo ... storing items`; the portal shows it at its next upload |

End-to-end on the target, before → after:

| Path (com.victronenergy.system) | Before | After |
|---|---|---|
| `/Ac/Grid/L1/Power` | empty (no value) | 914 W |
| `/Ac/Grid/NumberOfPhases` | empty | 1 |
| `/Ac/Consumption/L1/Power` | 1 W | 922 W (914 grid + 8 on the AC output) |
| `/Ac/ConsumptionOnInput/L1/Power` | empty | 914 W |

The consumption figure is the visible consequence: the installation now counts
what the meter draws, which it simply could not see before. `/Ac/L1/Current`
stays empty because standard-mode teleinfo carries no current — the GUI shows a
blank there, not a zero.

What is **not** validated: the dashboard as VRM renders it, and behaviour across
a GX reboot (the runit service is in the stock `/service` tree, re-created by
`install.sh` after a firmware update).

## License

GPL-3.0, like the sibling drivers.
