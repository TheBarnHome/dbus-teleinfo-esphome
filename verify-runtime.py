#!/usr/bin/env python3
"""Read the teleinfo grid meter back from D-Bus, the way the GX sees it.

Run this on the Venus OS device after install.sh: it is the check that separates
"the service is up" from "the installation actually has a grid".

    python3 verify-runtime.py
    python3 verify-runtime.py --system      # also read com.victronenergy.system
"""

from __future__ import annotations

import argparse
import sys

INTERESTING = (
    "/DeviceInstance",
    "/ProductName",
    "/Serial",
    "/Connected",
    "/Ac/NumberOfPhases",
    "/Ac/Power",
    "/Ac/L1/Power",
    "/Ac/Energy/Forward",
    "/Ac/Energy/Reverse",
    "/Teleinfo/PowerImport",
    "/Teleinfo/PowerExport",
    "/Teleinfo/Tariff",
    "/Teleinfo/PricePeriod",
    "/Teleinfo/Colour",
    "/Teleinfo/ColourTomorrow",
    "/Teleinfo/Endpoint",
    "/Teleinfo/Poll/Error",
    "/Teleinfo/Poll/Errors",
    "/Teleinfo/Poll/Cycles",
)

SYSTEM_PATHS = (
    "/Ac/Grid/L1/Power",
    "/Ac/Grid/L1/Current",
    "/Ac/ActiveIn/Source",
)

ITEM = "com.victronenergy.BusItem"


def read(bus, dbus, service, path):
    try:
        item = bus.get_object(service, path)
        value = item.GetValue(dbus_interface=ITEM)
        return value
    except Exception as exc:  # noqa: BLE001 - a missing path is an answer too
        return f"<{type(exc).__name__}>"


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the teleinfo grid meter on D-Bus")
    parser.add_argument("--service", default="com.victronenergy.grid.esphome-teleinfo")
    parser.add_argument("--system", action="store_true", help="also read the system service")
    args = parser.parse_args()

    try:
        import dbus
    except ImportError:
        print("dbus-python absent: à lancer sur le GX", file=sys.stderr)
        return 2

    bus = dbus.SystemBus()
    names = [str(name) for name in bus.list_names() if "teleinfo" in str(name)]
    if args.service not in names:
        print(f"service {args.service} absent du bus", file=sys.stderr)
        if names:
            print("services proches: " + ", ".join(names), file=sys.stderr)
        else:
            print("aucun service teleinfo: le service runit tourne-t-il ? (svstat /service/teleinfo-grid)", file=sys.stderr)
        return 1

    print(f"== {args.service}")
    connected = None
    for path in INTERESTING:
        value = read(bus, dbus, args.service, path)
        if path == "/Connected":
            connected = value
        if isinstance(value, dbus.Double):
            value = float(value)
        elif isinstance(value, (dbus.Int16, dbus.Int32, dbus.Int64, dbus.UInt16, dbus.UInt32, dbus.UInt64)):
            value = int(value)
        print(f"  {path:<28} = {value!r}")

    if args.system:
        print("== com.victronenergy.system (ce que VRM agrège)")
        for path in SYSTEM_PATHS:
            print(f"  {path:<28} = {read(bus, dbus, 'com.victronenergy.system', path)!r}")

    if connected in (0, 0.0):
        print("", file=sys.stderr)
        print("ECHEC: /Connected=0 — le service répond mais ne lit pas l'ESPHome.", file=sys.stderr)
        print("Voir /Teleinfo/Poll/Error et /var/log/dbus-teleinfo/current.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
