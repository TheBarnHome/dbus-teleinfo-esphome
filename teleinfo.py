"""Teleinfo decoding for the ESPHome web server: pure functions, no I/O.

Every decision that can be wrong lives here — the sign of the grid power, the
tariff period and its colour, and the one total that must never go backwards
(the energy index sum). No clock, no network, no D-Bus, so all of it runs and
is asserted on a laptop.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

# The ESPHome web server answers one entity per URL, and only these domains
# carry teleinfo data. Keeping the list closed stops a typo in the settings file
# from turning into a request to an arbitrary path.
ALLOWED_DOMAINS = ("sensor", "text_sensor", "binary_sensor")

# PTEC labels, as sent by an Enedis meter in standard mode. The two letters are
# the tariff period, the last one the Tempo colour.
PERIODS: dict[str, tuple[str, str]] = {
    "HCJB": ("HC", "blue"),
    "HPJB": ("HP", "blue"),
    "HCJW": ("HC", "white"),
    "HPJW": ("HP", "white"),
    "HCJR": ("HC", "red"),
    "HPJR": ("HP", "red"),
}

# NTARF is the index of the current period, in the EASF01..EASF06 order.
NTARF_TO_PERIOD = {1: "HCJB", 2: "HPJB", 3: "HCJW", 4: "HPJW", 5: "HCJR", 6: "HPJR"}

# NJOURF / NJOURF+1: 1 = blue, 2 = white, 3 = red. A meter that is not on a
# Tempo contract sends 0, which means "no coloured day", not "blue".
DAY_COLOURS = {0: None, 1: "blue", 2: "white", 3: "red"}

# Default entity layout, matching the teleinfo ESPHome configuration published
# by the community (the "home counter" YAML): SINSTS named PAPP, the six tariff
# registers EASF01..06, PTEC/NTARF/NJOURF. Override it in settings.json.
DEFAULT_LAYOUT: dict[str, Any] = {
    "power_import": "sensor/PAPP",  # SINSTS, apparent power drawn (VA)
    "power_export": "sensor/Injection",  # SINSTI, apparent power injected (VA)
    "tariff": "text_sensor/PTEC",  # current tariff label, "HPJB" and friends
    # Lower case on purpose: the published ESPHome example names this sensor
    # "ntarf" while the others keep the tag spelling. See AGENTS.md.
    "tariff_index": "sensor/ntarf",  # same information, as a number 1..6
    "colour_tomorrow": "sensor/njouf+1",  # NJOURF+1, tomorrow's Tempo colour
    "energy_indexes": [
        "sensor/bbrhcjb",  # EASF01, off-peak blue
        "sensor/bbrhpjb",  # EASF02, peak blue
        "sensor/bbrhcjw",  # EASF03, off-peak white
        "sensor/bbrhpjw",  # EASF04, peak white
        "sensor/bbrhcjr",  # EASF05, off-peak red
        "sensor/bbrhpjr",  # EASF06, peak red
    ],
    # Cumulative injected energy (EAIT), when the meter and the ESPHome config
    # provide one. Left empty, /Ac/Energy/Reverse is published as 0.
    "energy_reverse": None,
}


class PayloadError(ValueError):
    """The ESPHome answer is not a usable entity payload."""


# ---------------------------------------------------------------------------
# Entity names
# ---------------------------------------------------------------------------


def normalize_entity(entity: str) -> tuple[str, str]:
    """Split ``"text_sensor/PTEC"`` into ``(domain, object id)``.

    A bare name is a sensor, which is how every teleinfo value except PTEC is
    published. The web server keys entities on their *name*, not on the id set
    in the YAML, and keeps the case: ``PAPP 1`` really is ``sensor/PAPP 1``.
    """
    if not isinstance(entity, str) or not entity.strip():
        raise ValueError("empty entity name")
    name = entity.strip()
    if "/" in name:
        domain, _, object_id = name.partition("/")
        if domain not in ALLOWED_DOMAINS:
            raise ValueError(f"unsupported domain {domain!r} in {entity!r}")
        if not object_id:
            raise ValueError(f"empty object id in {entity!r}")
        return domain, object_id
    return "sensor", name


def canonical_entity(entity: str) -> str:
    """``"PAPP"`` and ``"sensor/PAPP"`` name the same entity; return one form."""
    domain, object_id = normalize_entity(entity)
    return f"{domain}/{object_id}"


# ---------------------------------------------------------------------------
# Payload decoding
# ---------------------------------------------------------------------------


def parse_payload(raw: str | bytes) -> Any:
    """Return the ``value`` field of an ESPHome ``/<domain>/<id>`` answer.

    The web server answers ``{"id": "sensor/PAPP", "value": 902, "state": "902 W"}``.
    ``value`` is null when the entity has never published — ``state`` then reads
    ``"NA"``. That is "no data", never zero.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise PayloadError(f"invalid JSON: {raw[:60]!r}") from exc
    if not isinstance(payload, dict) or "value" not in payload:
        raise PayloadError(f"not an entity payload: {raw[:60]!r}")
    return payload["value"]


def as_float(value: Any) -> float | None:
    """Coerce a sensor value to a finite float, or None.

    Text sensors answer with their label; a label is not a measurement, so it
    becomes None here instead of a number by accident.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def decode_period(label: Any) -> tuple[str | None, str | None]:
    """Return ``(period, colour)`` for a PTEC label: ``("HPJB")`` → ``("HP", "blue")``.

    An unknown label yields ``(None, None)`` rather than a guess. The raw label
    is published untouched on a teleinfo path, so it stays visible.
    """
    if not isinstance(label, str):
        return None, None
    return PERIODS.get(label.strip().upper(), (None, None))


def decode_tariff_index(value: Any) -> tuple[str | None, str | None]:
    """Same as :func:`decode_period`, from the NTARF number (1..6)."""
    number = as_float(value)
    if number is None:
        return None, None
    label = NTARF_TO_PERIOD.get(int(number))
    return decode_period(label)


def decode_day_colour(value: Any) -> str | None:
    """Tempo colour from a NJOURF number: 1 blue, 2 white, 3 red, 0 unknown."""
    number = as_float(value)
    if number is None:
        return None
    return DAY_COLOURS.get(int(number))


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------


def grid_power(import_va: float | None, export_va: float | None) -> float | None:
    """Victron grid convention: positive imports, negative exports.

    A Linky counts the two directions separately: SINSTS is what is drawn, SINSTI
    is what is injected, both positive. Publishing SINSTS alone would show an
    exporting house as consuming nothing instead of feeding in.
    """
    if import_va is None and export_va is None:
        return None
    return float(import_va or 0.0) - float(export_va or 0.0)


def total_energy(indexes: Iterable[float | None], *, expected: int) -> float | None:
    """Sum the tariff registers, or None when any of them is missing.

    A partial sum is *smaller* than the previous total, and both the GUI and VRM
    treat a grid counter as monotonic: one unread register would read as a meter
    rollback and corrupt the energy figure. Holding None, so the caller keeps the
    last complete value, is the only safe answer.
    """
    values = list(indexes)
    if expected <= 0 or len(values) != expected or any(v is None for v in values):
        return None
    return round(sum(values), 3)


def monotonic(previous: float | None, current: float | None, *, tolerance: float = 0.001) -> float | None:
    """Keep the larger counter: a cumulative energy total only goes up.

    The value can genuinely regress (meter replaced, ESPHome index reset). The
    caller logs that, because it is the only place it can be seen; publishing it
    would be read as a negative consumption.
    """
    if current is None:
        return previous
    if previous is not None and current < previous - tolerance:
        return previous
    return current


# ---------------------------------------------------------------------------
# The published view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """One decoded reading, exactly what the service publishes."""

    power_w: float | None
    import_va: float | None
    export_va: float | None
    phases: tuple[float | None, ...]
    energy_forward_kwh: float | None
    energy_reverse_kwh: float | None
    tariff: str | None
    price_period: str | None
    price_colour: str | None
    colour_tomorrow: str | None


# Every path the service owns. Created up front: a D-Bus path must exist before
# a value is written to it, and the energy total is published before the first
# power reading on purpose (see the README, "energy before power").
DBUS_PATHS: tuple[str, ...] = (
    "/Connected",
    "/Ac/Power",
    "/Ac/L1/Power",
    "/Ac/L2/Power",
    "/Ac/L3/Power",
    "/Ac/NumberOfPhases",
    "/Ac/Energy/Forward",
    "/Ac/Energy/Reverse",
    "/Teleinfo/PowerImport",
    "/Teleinfo/PowerExport",
    "/Teleinfo/Tariff",
    "/Teleinfo/PricePeriod",
    "/Teleinfo/Colour",
    "/Teleinfo/ColourTomorrow",
)


def build_snapshot(
    entity_values: Mapping[str, Any],
    layout: Mapping[str, Any] | None = None,
    *,
    previous_forward: float | None = None,
    previous_reverse: float | None = None,
    energy_in_kwh: bool = True,
) -> Snapshot:
    """Turn raw ESPHome entity values into a :class:`Snapshot`.

    ``entity_values`` is keyed by canonical entity name (``sensor/PAPP``). The
    layout decides which entity carries which role, so a differently named
    ESPHome configuration only needs a different settings file.
    """
    layout = DEFAULT_LAYOUT if layout is None else layout
    values = {canonical_entity(k): v for k, v in entity_values.items()}

    def raw(role: str) -> Any:
        entity = layout.get(role)
        if not entity:
            return None
        try:
            return values.get(canonical_entity(entity))
        except ValueError:
            return None

    import_va = as_float(raw("power_import"))
    export_va = as_float(raw("power_export"))

    # A meter reports its tariff period as a label (PTEC) or as a number
    # (NTARF); the label wins because it is the human-readable one.
    tariff_raw = raw("tariff")
    tariff = tariff_raw.strip() if isinstance(tariff_raw, str) and tariff_raw.strip() else None
    period, colour = decode_period(tariff)
    if period is None:
        period, colour = decode_tariff_index(raw("tariff_index"))

    scale = 1.0 if energy_in_kwh else 0.001
    indexes: list[float | None] = []
    expected = 0
    for entity in layout.get("energy_indexes") or ():
        expected += 1
        number = as_float(values.get(canonical_entity(entity)))
        indexes.append(None if number is None else number * scale)
    forward = monotonic(
        previous_forward,
        total_energy(indexes, expected=expected),
    )
    reverse = as_float(raw("energy_reverse"))
    if reverse is not None:
        reverse *= scale
    reverse = monotonic(previous_reverse, reverse)

    phases: list[float | None] = []
    for entity in layout.get("phase_powers") or ():
        number = as_float(values.get(canonical_entity(entity)))
        phases.append(None if number is None else number)

    return Snapshot(
        power_w=grid_power(import_va, export_va),
        import_va=import_va,
        export_va=export_va,
        phases=tuple(phases),
        energy_forward_kwh=forward,
        energy_reverse_kwh=reverse,
        tariff=tariff,
        price_period=period,
        price_colour=colour,
        colour_tomorrow=decode_day_colour(raw("colour_tomorrow")),
    )


def dbus_values(snapshot: Snapshot, *, connected: bool) -> dict[str, Any]:
    """Map a snapshot onto the D-Bus paths this service publishes.

    An absent meter is not a zero: when the link is down the instantaneous
    values go to None so the GUI shows "---" and systemcalc stops summing them,
    while the cumulative counters keep their last value (that is what a meter
    that lost its link does, and it keeps VRM's energy history continuous).
    """
    power = snapshot.power_w if connected else None
    values: dict[str, Any] = {
        "/Connected": 1 if connected else 0,
        "/Ac/Power": power,
        "/Ac/Energy/Forward": snapshot.energy_forward_kwh,
        "/Ac/Energy/Reverse": 0.0 if snapshot.energy_reverse_kwh is None else snapshot.energy_reverse_kwh,
        "/Teleinfo/PowerImport": snapshot.import_va if connected else None,
        "/Teleinfo/PowerExport": snapshot.export_va if connected else None,
        "/Teleinfo/Tariff": snapshot.tariff,
        "/Teleinfo/PricePeriod": snapshot.price_period,
        "/Teleinfo/Colour": snapshot.price_colour,
        "/Teleinfo/ColourTomorrow": snapshot.colour_tomorrow,
    }
    if snapshot.phases:
        values["/Ac/NumberOfPhases"] = len(snapshot.phases)
        for number, phase_power in enumerate(snapshot.phases[:3], start=1):
            values[f"/Ac/L{number}/Power"] = phase_power if connected else None
        # A phase that is configured but silent stays None; do not invent a 0.
    else:
        values["/Ac/NumberOfPhases"] = 1
        values["/Ac/L1/Power"] = power
    return values


def entities_of(layout: Mapping[str, Any] | None = None) -> tuple[list[str], list[str]]:
    """Split the layout into (fast, slow) entity lists.

    Power is what the GUI and systemcalc follow, so it is polled at the fast
    interval; indexes and tariff labels change once a day at most.
    """
    layout = DEFAULT_LAYOUT if layout is None else layout
    fast: list[str] = []
    slow: list[str] = []
    for role in ("power_import", "power_export"):
        entity = layout.get(role)
        if entity:
            fast.append(canonical_entity(entity))
    for entity in layout.get("phase_powers") or ():
        fast.append(canonical_entity(entity))
    candidates: list[Any] = [
        layout.get("tariff"),
        layout.get("tariff_index"),
        layout.get("colour_tomorrow"),
        layout.get("energy_reverse"),
    ]
    candidates.extend(layout.get("energy_indexes") or ())
    for entity in candidates:
        if not entity:
            continue
        name = canonical_entity(entity)
        if name not in fast and name not in slow:
            slow.append(name)
    return fast, slow


def required_indexes(layout: Mapping[str, Any] | None = None) -> Sequence[str]:
    """Energy registers whose sum is the grid forward counter."""
    layout = DEFAULT_LAYOUT if layout is None else layout
    return [canonical_entity(e) for e in layout.get("energy_indexes") or ()]
