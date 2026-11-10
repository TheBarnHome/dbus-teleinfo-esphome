#!/usr/bin/env python3
"""Decoding tests, anchored on the payload shape of a live ESPHome node.

The JSON shape, the null-for-unknown convention and the entity naming were
captured from a real node; the readings below are plausible stand-ins, because a
public repository has no business carrying a household's meter history.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import teleinfo  # noqa: E402

# One full answer set, in the shape the ESPHome web server returns.
MEASURED = {
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

# Sum of the six tariff registers, in kWh.
MEASURED_TOTAL = 19152.0


class EntityNameTests(unittest.TestCase):
    def test_bare_name_is_a_sensor(self):
        self.assertEqual(teleinfo.normalize_entity("PAPP"), ("sensor", "PAPP"))

    def test_explicit_domain(self):
        self.assertEqual(teleinfo.normalize_entity("text_sensor/PTEC"), ("text_sensor", "PTEC"))

    def test_case_and_spaces_are_kept(self):
        # The web server keys entities on their name, not on the YAML id.
        self.assertEqual(teleinfo.normalize_entity("sensor/PAPP 1"), ("sensor", "PAPP 1"))

    def test_unknown_domain_is_refused(self):
        with self.assertRaises(ValueError):
            teleinfo.normalize_entity("switch/relay")

    def test_empty_name_is_refused(self):
        for bad in ("", "   ", None, "sensor/"):
            with self.assertRaises(ValueError):
                teleinfo.normalize_entity(bad)  # type: ignore[arg-type]

    def test_canonical_forms_agree(self):
        self.assertEqual(teleinfo.canonical_entity("PAPP"), "sensor/PAPP")
        self.assertEqual(teleinfo.canonical_entity("sensor/PAPP"), "sensor/PAPP")


class PayloadTests(unittest.TestCase):
    def test_numeric_payload(self):
        raw = b'{"id":"sensor/PAPP","value":918,"state":"918 W"}'
        self.assertEqual(teleinfo.parse_payload(raw), 918)

    def test_null_payload_is_no_data(self):
        raw = '{"id":"sensor/Injection","value":null,"state":"NA"}'
        self.assertIsNone(teleinfo.parse_payload(raw))

    def test_text_payload(self):
        raw = '{"id":"text_sensor/PTEC","value":"HPJB","state":"HPJB"}'
        self.assertEqual(teleinfo.parse_payload(raw), "HPJB")

    def test_invalid_json_is_refused(self):
        with self.assertRaises(teleinfo.PayloadError):
            teleinfo.parse_payload("<html>404</html>")

    def test_payload_without_value_is_refused(self):
        with self.assertRaises(teleinfo.PayloadError):
            teleinfo.parse_payload(json.dumps({"id": "sensor/PAPP", "state": "918 W"}))


class FloatTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(teleinfo.as_float(918), 918.0)
        self.assertEqual(teleinfo.as_float(1.5), 1.5)
        self.assertEqual(teleinfo.as_float(" 42 "), 42.0)

    def test_non_numbers(self):
        for value in (None, "NA", "", "HPJB", True, False, [], {}, float("nan"), float("inf")):
            self.assertIsNone(teleinfo.as_float(value), msg=repr(value))


class TariffTests(unittest.TestCase):
    def test_peak_blue(self):
        self.assertEqual(teleinfo.decode_period("HPJB"), ("HP", "blue"))

    def test_off_peak_red(self):
        self.assertEqual(teleinfo.decode_period("hCJr"), ("HC", "red"))

    def test_unknown_label_is_not_guessed(self):
        self.assertEqual(teleinfo.decode_period("XXXX"), (None, None))
        self.assertEqual(teleinfo.decode_period(None), (None, None))

    def test_tariff_index(self):
        self.assertEqual(teleinfo.decode_tariff_index(2), ("HP", "blue"))
        self.assertEqual(teleinfo.decode_tariff_index(5), ("HC", "red"))
        self.assertEqual(teleinfo.decode_tariff_index(9), (None, None))

    def test_day_colour(self):
        self.assertEqual(teleinfo.decode_day_colour(1), "blue")
        self.assertEqual(teleinfo.decode_day_colour(3), "red")
        # A meter that is not on Tempo sends 0, which is "no colour", not blue.
        self.assertIsNone(teleinfo.decode_day_colour(0))
        self.assertIsNone(teleinfo.decode_day_colour(None))


class PowerTests(unittest.TestCase):
    def test_import_only(self):
        self.assertEqual(teleinfo.grid_power(918, None), 918.0)

    def test_export_is_negative(self):
        # SINSTI is a positive counter of what is fed in; Victron wants a sign.
        self.assertEqual(teleinfo.grid_power(0, 500), -500.0)
        self.assertEqual(teleinfo.grid_power(None, 500), -500.0)

    def test_no_measurement(self):
        self.assertIsNone(teleinfo.grid_power(None, None))


class EnergyTests(unittest.TestCase):
    def test_complete_sum(self):
        values = [12480.25, 5210.5, 980.125, 410.75, 62.4, 7.975]
        self.assertAlmostEqual(teleinfo.total_energy(values, expected=6), MEASURED_TOTAL, places=3)

    def test_partial_sum_is_refused(self):
        # One unread register must never produce a smaller total: the GUI and VRM
        # read a grid counter as monotonic, so a drop is a rollback.
        values = [12480.25, 5210.5, 980.125, 410.75, 62.4, None]
        self.assertIsNone(teleinfo.total_energy(values, expected=6))

    def test_wrong_count_is_refused(self):
        self.assertIsNone(teleinfo.total_energy([1.0, 2.0], expected=6))
        self.assertIsNone(teleinfo.total_energy([], expected=6))

    def test_monotonic_holds_the_previous_value(self):
        self.assertEqual(teleinfo.monotonic(100.0, 99.0), 100.0)
        self.assertEqual(teleinfo.monotonic(100.0, 101.0), 101.0)
        self.assertEqual(teleinfo.monotonic(100.0, None), 100.0)
        self.assertEqual(teleinfo.monotonic(None, 50.0), 50.0)
        self.assertIsNone(teleinfo.monotonic(None, None))


class SnapshotTests(unittest.TestCase):
    def test_measured_payload(self):
        snapshot = teleinfo.build_snapshot(MEASURED)
        self.assertEqual(snapshot.power_w, 742.0)
        self.assertAlmostEqual(snapshot.energy_forward_kwh, MEASURED_TOTAL, places=3)
        self.assertEqual(snapshot.tariff, "HPJB")
        self.assertEqual(snapshot.price_period, "HP")
        self.assertEqual(snapshot.price_colour, "blue")
        self.assertEqual(snapshot.colour_tomorrow, "blue")
        self.assertEqual(snapshot.phases, ())

    def test_bare_entity_names_resolve(self):
        values = {"PAPP": 500, "text_sensor/PTEC": "HCJR", "bbrhcjb": 1.0}
        snapshot = teleinfo.build_snapshot(values)
        self.assertEqual(snapshot.power_w, 500.0)
        self.assertEqual(snapshot.price_period, "HC")
        self.assertEqual(snapshot.price_colour, "red")

    def test_bare_name_is_read_as_a_sensor(self):
        # A bare name means sensor/, and PTEC is a text sensor: without the
        # domain the tariff stays unknown instead of matching another entity by
        # accident. The default layout carries the domain for that reason.
        snapshot = teleinfo.build_snapshot({"PAPP": 500, "PTEC": "HCJR"})
        self.assertEqual(snapshot.power_w, 500.0)
        self.assertIsNone(snapshot.price_period)

    def test_wh_payload_is_scaled(self):
        values = {key: value * 1000 for key, value in MEASURED.items() if key.startswith("sensor/bbr")}
        snapshot = teleinfo.build_snapshot(values, energy_in_kwh=False)
        self.assertAlmostEqual(snapshot.energy_forward_kwh, MEASURED_TOTAL, places=3)

    def test_tariff_label_of_the_meter(self):
        values = dict(MEASURED, **{"text_sensor/PTEC": "HCJW", "sensor/ntarf": 1})
        snapshot = teleinfo.build_snapshot(values)
        # PTEC wins over NTARF: it is the human readable one.
        self.assertEqual(snapshot.price_period, "HC")
        self.assertEqual(snapshot.price_colour, "white")

    def test_tariff_falls_back_to_the_index(self):
        values = dict(MEASURED, **{"text_sensor/PTEC": None})
        snapshot = teleinfo.build_snapshot(values)
        self.assertEqual(snapshot.price_period, "HP")
        self.assertEqual(snapshot.price_colour, "blue")

    def test_missing_index_keeps_the_previous_total(self):
        values = dict(MEASURED)
        del values["sensor/bbrhpjr"]
        snapshot = teleinfo.build_snapshot(values, previous_forward=MEASURED_TOTAL)
        self.assertEqual(snapshot.energy_forward_kwh, MEASURED_TOTAL)

    def test_phases(self):
        layout = dict(teleinfo.DEFAULT_LAYOUT, phase_powers=["sensor/PAPP 1", "sensor/PAPP 2", "sensor/PAPP 3"])
        values = {"sensor/PAPP": 742, "sensor/PAPP 1": 0, "sensor/PAPP 2": 0, "sensor/PAPP 3": 742}
        snapshot = teleinfo.build_snapshot(values, layout)
        self.assertEqual(snapshot.phases, (0.0, 0.0, 742.0))
        published = teleinfo.dbus_values(snapshot, connected=True)
        self.assertEqual(published["/Ac/NumberOfPhases"], 3)
        self.assertEqual(published["/Ac/L3/Power"], 742.0)


class DbusMapTests(unittest.TestCase):
    def test_single_phase_map(self):
        published = teleinfo.dbus_values(teleinfo.build_snapshot(MEASURED), connected=True)
        self.assertEqual(published["/Connected"], 1)
        self.assertEqual(published["/Ac/Power"], 742.0)
        self.assertEqual(published["/Ac/L1/Power"], 742.0)
        self.assertEqual(published["/Ac/NumberOfPhases"], 1)
        self.assertAlmostEqual(published["/Ac/Energy/Forward"], MEASURED_TOTAL, places=3)
        # No injection counter configured: VRM must read 0, not "unknown".
        self.assertEqual(published["/Ac/Energy/Reverse"], 0.0)
        self.assertEqual(published["/Teleinfo/Tariff"], "HPJB")

    def test_offline_hides_power_but_keeps_the_counters(self):
        published = teleinfo.dbus_values(teleinfo.build_snapshot(MEASURED), connected=False)
        self.assertEqual(published["/Connected"], 0)
        self.assertIsNone(published["/Ac/Power"])
        self.assertIsNone(published["/Ac/L1/Power"])
        self.assertAlmostEqual(published["/Ac/Energy/Forward"], MEASURED_TOTAL, places=3)

    def test_every_published_path_is_declared(self):
        # The service creates the paths before it writes them; a value written to
        # an undeclared path is an exception at runtime, not a display bug.
        for connected in (True, False):
            published = teleinfo.dbus_values(teleinfo.build_snapshot(MEASURED), connected=connected)
            self.assertLessEqual(set(published), set(teleinfo.DBUS_PATHS))
            self.assertIn("/Ac/Power", published)
            self.assertIn("/Connected", published)


class SchedulingTests(unittest.TestCase):
    def test_power_is_fast_everything_else_is_slow(self):
        fast, slow = teleinfo.entities_of()
        self.assertEqual(fast, ["sensor/PAPP", "sensor/Injection"])
        self.assertIn("text_sensor/PTEC", slow)
        self.assertIn("sensor/bbrhcjb", slow)
        self.assertEqual(len(slow), 9)

    def test_no_entity_is_polled_twice(self):
        fast, slow = teleinfo.entities_of()
        self.assertEqual(set(fast) & set(slow), set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
