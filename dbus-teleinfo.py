#!/usr/bin/env python3
"""Publish a French teleinfo meter, read from ESPHome, as a Victron grid meter.

    ESPHome (teleinfo on the TIC)  --HTTP-->  this service  -->  D-Bus grid meter
                                                                    |
                                                              VRM / GUI / systemcalc

The meter is published as ``com.victronenergy.grid.esphome-teleinfo``. Venus OS
picks a grid meter up by service type, systemcalc sums it into ``/Ac/Grid/*``,
vrmlogger logs it, and VRM draws it as the installation's grid.

Why read the ESP rather than the TIC directly: the teleinfo stream already has
one reader (the ESPHome node, which decodes it and serves it over its web
server), and a second reader on the same wire is a second point of failure for
no extra information.

Layers, as in the sibling drivers:

    teleinfo.py        pure decoding, the only place a conversion can be wrong
    esphome_http.py    HTTP, one request per entity, standard library only
    this file          D-Bus, the clock, the settings file, the poll thread

Usage
-----
    ./dbus-teleinfo.py --probe --host <esphome-host>     # no D-Bus, check the link
    ./dbus-teleinfo.py --self-test                       # offline, prints the map
    ./dbus-teleinfo.py --host <esphome-host>             # the service
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import signal
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import teleinfo
from esphome_http import EspHomeClient, EspHomeError

VERSION = "v0.1"

DEFAULT_STATE_DIR = Path("/data/etc/dbus-teleinfo-state")
DEFAULT_CONFIG = DEFAULT_STATE_DIR / "settings.json"

DEFAULT_SERVICE_NAME = "com.victronenergy.grid.esphome-teleinfo"
DEFAULT_PRODUCT_NAME = "ESPHome teleinfo"

# Grid meters live outside the Victron instance ranges handed out automatically;
# systemcalc uses the lowest instance when several are on the bus, so a test
# instance is one above the real one.
DEFAULT_DEVICE_INSTANCE = 40

DEFAULT_SETTINGS: dict[str, Any] = {
    "host": "",
    "port": 80,
    # Seconds between two reads of the instantaneous power. The ESPHome teleinfo
    # component itself refreshes every update_interval (10 s in the published
    # example), so polling faster buys nothing but traffic.
    "poll_interval_s": 10,
    # Tariff registers and labels move once a day; read them once a minute.
    "slow_interval_s": 60,
    # Three fast cycles without an answer and the meter is declared absent.
    "stale_after_s": 35,
    "http_timeout_s": 3.0,
    "device_instance": DEFAULT_DEVICE_INSTANCE,
    "serial": "teleinfo",
    "product_name": DEFAULT_PRODUCT_NAME,
    "custom_name": "Grid (teleinfo)",
    "firmware_version": None,
    # True when the ESPHome entities already publish kWh (the published example
    # scales EASF01..06 with a 0.001 filter). False divides by 1000: Wh.
    "energy_in_kwh": True,
    "service_name": DEFAULT_SERVICE_NAME,
    "layout": teleinfo.DEFAULT_LAYOUT,
}

log = logging.getLogger("teleinfo")


# ---------------------------------------------------------------------------
# Persistent settings
# ---------------------------------------------------------------------------


class Settings:
    """Service configuration, outside the Git checkout so an update keeps it."""

    def __init__(self, path: Path, defaults: Mapping[str, Any]) -> None:
        self.path = path
        self.values = dict(defaults)
        self._load()

    def _load(self) -> None:
        try:
            stored = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (ValueError, OSError) as exc:
            log.warning("réglages illisibles (%s), valeurs par défaut", exc)
            try:
                self.path.rename(self.path.with_suffix(".corrupt"))
            except OSError:
                pass
            return
        if not isinstance(stored, dict):
            log.warning("réglages: objet JSON attendu, fichier ignoré")
            return
        self.values.update(stored)
        # A partially written layout must not disable the meter: unknown keys are
        # ignored by the builder, missing ones fall back to the defaults.
        layout = stored.get("layout")
        if isinstance(layout, dict):
            merged = dict(teleinfo.DEFAULT_LAYOUT)
            merged.update(layout)
            self.values["layout"] = merged

    @property
    def layout(self) -> Mapping[str, Any]:
        return self.values["layout"]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.values, indent=2, sort_keys=True))
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("réglages non enregistrés: %s", exc)


# ---------------------------------------------------------------------------
# Poll thread
# ---------------------------------------------------------------------------


class Poller(threading.Thread):
    """Reads the ESPHome entities off the D-Bus thread.

    dbus-python's main loop is single threaded: an HTTP read that hits its
    timeout inside a GLib callback freezes the whole service, and a frozen
    service is a meter that disappears from VRM without saying why. The MPP
    driver paid for this lesson already; the poll therefore runs here and only
    hands values over, under a lock.
    """

    def __init__(
        self,
        client: EspHomeClient,
        fast: list[str],
        slow: list[str],
        interval: float,
        slow_interval: float,
    ) -> None:
        super().__init__(name="teleinfo-poll", daemon=True)
        self.client = client
        self.fast = tuple(fast)
        self.slow = tuple(slow)
        self.interval = max(1.0, float(interval))
        self.slow_interval = max(self.interval, float(slow_interval))
        self._lock = threading.Lock()
        self._values: dict[str, Any] = {}
        self._last_ok = 0.0
        self._last_attempt = 0.0
        self._last_error = ""
        self._errors = 0
        self._cycles = 0
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "values": dict(self._values),
                "last_ok": self._last_ok,
                "last_attempt": self._last_attempt,
                "last_error": self._last_error,
                "errors": self._errors,
                "cycles": self._cycles,
            }

    def run(self) -> None:  # noqa: D102 - Thread.run
        next_fast = 0.0
        next_slow = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            due_fast = now >= next_fast
            due_slow = now >= next_slow
            if due_fast or due_slow:
                batch: list[str] = []
                if due_fast:
                    batch.extend(self.fast)
                if due_slow:
                    batch.extend(name for name in self.slow if name not in batch)
                if batch:
                    self._cycle(batch, now)
                if due_fast:
                    next_fast = now + self.interval
                if due_slow:
                    next_slow = now + self.slow_interval
            self._stop.wait(0.25)

    def _cycle(self, batch: list[str], now: float) -> None:
        values, errors = self.client.fetch_all(batch)
        with self._lock:
            self._values.update(values)
            self._last_attempt = now
            self._cycles += 1
            # Only the power entities decide whether the meter is alive: a
            # contract without injection has no SINSTI at all, and that is not
            # an outage.
            if any(name in values for name in self.fast):
                self._last_ok = now
            if errors:
                self._errors += len(errors)
                self._last_error = errors[-1]
        for message in errors:
            log.debug("lecture en échec: %s", message)


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class TeleinfoService:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.settings = Settings(Path(args.settings_file), DEFAULT_SETTINGS)
        if args.host:
            self.settings.values["host"] = args.host
        for key, value in (
            ("port", args.port),
            ("device_instance", args.device_instance),
            ("service_name", args.service_name),
            ("poll_interval_s", args.poll_interval),
            ("slow_interval_s", args.slow_interval),
            ("stale_after_s", args.stale_after),
            ("http_timeout_s", args.timeout),
        ):
            if value is not None:
                self.settings.values[key] = value
        self.client: EspHomeClient | None = None
        self.poller: Poller | None = None
        self.service = None
        self.mainloop = None
        self.forward: float | None = None
        self.reverse: float | None = None
        self._was_connected: bool | None = None
        self._notes: set[str] = set()

    def make_client(self) -> EspHomeClient:
        """Build the HTTP client, once the host is known.

        Deliberately not built in ``__init__``: ``--self-test`` decodes a
        recorded answer and must run on a laptop with no ESPHome in sight.
        """
        host = str(self.settings.values.get("host") or "").strip()
        if not host:
            raise ValueError("aucun hôte ESPHome configuré: --host ou settings.json")
        return EspHomeClient(
            host,
            int(self.settings.values["port"]),
            float(self.settings.values["http_timeout_s"]),
        )

    # -- one-shot modes ---------------------------------------------------

    def entities(self) -> tuple[list[str], list[str]]:
        """Fast and slow entity lists, with a readable error on a typo."""
        try:
            return teleinfo.entities_of(self.settings.layout)
        except ValueError as exc:
            raise ValueError(f"réglage layout invalide: {exc}") from exc

    def probe(self) -> int:
        """Read everything once and print what would be published."""
        client = self.make_client()
        self.client = client
        fast, slow = self.entities()
        entities = fast + [name for name in slow if name not in fast]
        log.info("sonde %s, %d entité(s), délai %.1f s", client.endpoint, len(entities), client.timeout)
        values, errors = client.fetch_all(entities)
        for entity in entities:
            role = self._role_of(entity)
            if entity in values:
                print(f"  {role:<16} {entity:<28} = {values[entity]!r}")
            else:
                print(f"  {role:<16} {entity:<28} = (illisible)")
        for message in errors:
            print(f"  erreur: {message}")
        snapshot = teleinfo.build_snapshot(values, self.settings.layout, energy_in_kwh=bool(self.settings.values["energy_in_kwh"]))
        print("")
        print(f"  {'puissance':<16} {snapshot.power_w!r} W  (import {snapshot.import_va!r} VA, export {snapshot.export_va!r} VA)")
        print(f"  {'énergie':<16} {snapshot.energy_forward_kwh!r} kWh")
        print(f"  {'tarif':<16} {snapshot.tariff!r} ({snapshot.price_period}, {snapshot.price_colour}), demain {snapshot.colour_tomorrow}")
        for path, value in teleinfo.dbus_values(snapshot, connected=True).items():
            print(f"  {path:<28} = {value!r}")
        required = [name for name in fast if name in values]
        if not required:
            print("")
            print("  ECHEC: aucune puissance lue, le service ne servirait à rien")
            return 1
        return 0

    def _role_of(self, entity: str) -> str:
        for role, configured in self.settings.layout.items():
            if isinstance(configured, str) and teleinfo.canonical_entity(configured) == entity:
                return role
            if isinstance(configured, (list, tuple)):
                for item in configured:
                    if isinstance(item, str) and teleinfo.canonical_entity(item) == entity:
                        return role
        return "?"

    def self_test(self) -> int:
        """Offline: a recorded ESPHome answer in, the D-Bus map out."""
        fixture = {
            "sensor/PAPP": 742,
            "sensor/Injection": None,
            "text_sensor/PTEC": "HPJB",
            "sensor/ntarf": 2,
            "sensor/njouf+1": 1,
            "sensor/bbrhcjb": 12480.25,
            "sensor/bbrhpjb": 5210.5,
            "sensor/bbrhcjw": 980.125,
            "sensor/bbrhpjw": 410.75,
            "sensor/bbrhcjr": 62.4,
            "sensor/bbrhpjr": 7.975,
        }
        snapshot = teleinfo.build_snapshot(fixture, teleinfo.DEFAULT_LAYOUT)
        print(f"puissance {snapshot.power_w} W, énergie {snapshot.energy_forward_kwh} kWh, tarif {snapshot.tariff}")
        for path, value in teleinfo.dbus_values(snapshot, connected=True).items():
            print(f"  {path:<28} = {value!r}")
        # One missing index: the total must freeze instead of dropping.
        broken = dict(fixture)
        del broken["sensor/bbrhpjr"]
        partial = teleinfo.build_snapshot(broken, teleinfo.DEFAULT_LAYOUT)
        print(f"index incomplet -> {partial.energy_forward_kwh!r} (doit être None)")
        held = teleinfo.build_snapshot(broken, teleinfo.DEFAULT_LAYOUT, previous_forward=snapshot.energy_forward_kwh)
        print(f"index incomplet, valeur précédente conservée -> {held.energy_forward_kwh!r}")
        return 0 if partial.energy_forward_kwh is None and held.energy_forward_kwh == snapshot.energy_forward_kwh else 1

    # -- D-Bus service ----------------------------------------------------

    def create_service(self) -> None:
        here = Path(__file__).resolve().parent
        sys.path.insert(0, str(here / "velib_python"))
        import dbus
        from vedbus import VeDbusService

        name = str(self.settings.values["service_name"])
        svc = VeDbusService(name, register=False)
        self.service = svc

        svc.add_path("/Mgmt/ProcessName", __file__)
        svc.add_path("/Mgmt/ProcessVersion", f"{VERSION} on Python {platform.python_version()}")
        svc.add_path("/Mgmt/Connection", f"HTTP {self.client.endpoint}")
        svc.add_path("/DeviceInstance", int(self.settings.values["device_instance"]))
        # 0 is "no Victron product id": this is not a Victron device, and
        # borrowing one would put a wrong icon and a wrong name in VRM.
        svc.add_path("/ProductId", 0)
        svc.add_path("/ProductName", str(self.settings.values["product_name"]))
        svc.add_path("/FirmwareVersion", self.settings.values.get("firmware_version"))
        svc.add_path("/HardwareVersion", None)
        svc.add_path("/Serial", str(self.settings.values["serial"]))
        svc.add_path("/CustomName", str(self.settings.values.get("custom_name") or ""))
        for path in teleinfo.DBUS_PATHS:
            if path == "/Connected":
                svc.add_path(path, 0)
            elif path == "/Ac/NumberOfPhases":
                svc.add_path(path, 1)
            else:
                svc.add_path(path, None)
        svc.add_path("/Teleinfo/Endpoint", self.client.endpoint)
        svc.add_path("/Teleinfo/Poll/Error", "")
        svc.add_path("/Teleinfo/Poll/Errors", 0)
        svc.add_path("/Teleinfo/Poll/Cycles", 0)
        svc.register()
        log.info("service D-Bus publié: %s (instance %s)", name, self.settings.values["device_instance"])

    # -- loop -------------------------------------------------------------

    def _tick(self) -> bool:
        try:
            self.publish()
        except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
            log.exception("tick en échec")
        return True

    def publish(self) -> None:
        state = self.poller.state()
        now = time.monotonic()
        age = None if not state["last_ok"] else now - state["last_ok"]
        connected = age is not None and age <= float(self.settings.values["stale_after_s"])

        snapshot = teleinfo.build_snapshot(
            state["values"],
            self.settings.layout,
            previous_forward=self.forward,
            previous_reverse=self.reverse,
            energy_in_kwh=bool(self.settings.values["energy_in_kwh"]),
        )
        if snapshot.energy_forward_kwh is None:
            self._note("index", "somme des index EASF incomplète: /Ac/Energy/Forward conservé")
        else:
            self._notes.discard("index")
        if snapshot.energy_forward_kwh is not None:
            self.forward = snapshot.energy_forward_kwh
        if snapshot.energy_reverse_kwh is not None:
            self.reverse = snapshot.energy_reverse_kwh

        values = teleinfo.dbus_values(snapshot, connected=connected)
        values["/Teleinfo/Poll/Error"] = "" if connected else (state["last_error"] or "aucune donnée")
        values["/Teleinfo/Poll/Errors"] = int(state["errors"])
        values["/Teleinfo/Poll/Cycles"] = int(state["cycles"])

        with self.service as svc:
            for path, value in values.items():
                svc[path] = value

        if connected != self._was_connected:
            if connected:
                log.info("meter en ligne: %s W, %.3f kWh", snapshot.power_w, snapshot.energy_forward_kwh or 0.0)
            elif self._was_connected is None:
                log.warning("aucune donnée de %s (le service reste enregistré, /Connected=0)", self.client.endpoint)
            else:
                log.warning("meter hors ligne (%s)", state["last_error"] or "pas de réponse")
            self._was_connected = connected

    def _note(self, key: str, message: str) -> None:
        if key not in self._notes:
            self._notes.add(key)
            log.warning("%s", message)

    def run(self) -> int:
        if self.args.self_test:
            return self.self_test()
        try:
            self.client = self.make_client()
        except ValueError as exc:
            log.error("%s", exc)
            return 2
        try:
            self.entities()
        except ValueError as exc:
            log.error("%s", exc)
            return 2
        if self.args.probe:
            return self.probe()

        from dbus.mainloop.glib import DBusGMainLoop
        from gi.repository import GLib

        DBusGMainLoop(set_as_default=True)
        # The serial and the instance go into VRM's device list; write the
        # settings once so an update never renames a device the portal knows.
        self.settings.save()
        self.create_service()

        fast, slow = self.entities()
        self.poller = Poller(
            self.client,
            fast,
            slow,
            float(self.settings.values["poll_interval_s"]),
            float(self.settings.values["slow_interval_s"]),
        )
        self.poller.start()

        self.mainloop = GLib.MainLoop()
        GLib.timeout_add_seconds(1, self._tick)
        signal.signal(signal.SIGTERM, lambda *_: self.mainloop.quit())
        signal.signal(signal.SIGINT, lambda *_: self.mainloop.quit())
        log.info(
            "boucle démarrée: %s toutes les %.0f s, index toutes les %.0f s",
            self.client.endpoint,
            float(self.settings.values["poll_interval_s"]),
            float(self.settings.values["slow_interval_s"]),
        )
        self.mainloop.run()
        self.poller.stop()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ESPHome teleinfo as a Victron grid meter")
    parser.add_argument("--host", help="ESPHome host name or address serving the teleinfo entities")
    parser.add_argument("--port", type=int, help="ESPHome web server port (default 80)")
    parser.add_argument("--device-instance", type=int, help=f"grid meter instance (default {DEFAULT_DEVICE_INSTANCE})")
    parser.add_argument("--service-name", help=f"D-Bus service name (default {DEFAULT_SERVICE_NAME})")
    parser.add_argument("--poll-interval", type=float, help="seconds between two power reads (default 10)")
    parser.add_argument("--slow-interval", type=float, help="seconds between two index/label reads (default 60)")
    parser.add_argument("--stale-after", type=float, help="seconds without data before the meter is absent (default 35)")
    parser.add_argument("--timeout", type=float, help="HTTP timeout per entity (default 3 s)")
    parser.add_argument("--settings-file", default=str(DEFAULT_CONFIG))
    parser.add_argument("--probe", action="store_true", help="read everything once, print it, no D-Bus")
    parser.add_argument("--self-test", action="store_true", help="offline: decode a recorded answer and print the map")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return TeleinfoService(args).run()
    except ValueError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
