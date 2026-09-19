"""verify: a spec compared against what Altium reports back."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pinout.ioc import IocFile                  # noqa: E402
from pinout.mcu_db import load_mcu_xml          # noqa: E402
from pinout.model import Pinout                 # noqa: E402
from pinout.layout import functional_split      # noqa: E402
from pinout.verify import compare, spec_as_dump, normalize  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def spec():
    p = Pinout.from_sources(load_mcu_xml(FIX / "FAKE32C20.xml"),
                            IocFile.load(FIX / "fake_board.ioc"), board="TESTBOARD")
    return functional_split(p, mpn="FAKE32C20U6")


def pin(dump, number):
    return next(p for p in dump["primitives"] if p["type"] == "pin" and p["pin_number"] == number)


class IdentityTest(unittest.TestCase):
    def test_faithful_dump_matches(self):
        s = spec()
        r = compare(s, spec_as_dump(s))
        self.assertTrue(r["match"], r)
        self.assertEqual(r["pins"], {"missing_in_altium": [], "unexpected_in_altium": [], "changed": []})
        self.assertEqual(r["graphics"], {"missing_in_altium": [], "unexpected_in_altium": []})
        self.assertEqual(r["summary"]["pins_identical"], r["summary"]["spec_pins"], r["summary"])
        self.assertEqual(r["summary"]["spec_pins"], 20)

    def test_enum_names_become_altium_integers(self):
        d = spec_as_dump(spec())
        self.assertEqual(pin(d, "1")["electrical"], 7, "VDD -> eElectricPower = 7")
        self.assertEqual(pin(d, "19")["electrical"], 0, "NRST -> eElectricInput = 0")
        self.assertEqual(pin(d, "2")["orientation"], 2, "left-facing -> eRotate180 = 2")
        self.assertEqual(pin(d, "1")["orientation"], 0, "right-facing -> eRotate0 = 0")


class TolerantNormalisationTest(unittest.TestCase):
    def test_float_coordinates_and_missing_optional_keys(self):
        s = spec()
        d = spec_as_dump(s)
        for p in d["primitives"]:
            for k in ("x", "y", "x1", "y1", "x2", "y2", "length"):
                if k in p:
                    p[k] = float(p[k])
            p.pop("line_width", None)
            p.pop("show_name", None)
        self.assertTrue(compare(s, d)["match"])

    def test_polyline_direction_is_irrelevant(self):
        s = spec()
        d = spec_as_dump(s)
        for p in d["primitives"]:
            if p["type"] == "polyline":
                p["vertices"].reverse()
        self.assertTrue(compare(s, d)["match"])

    def test_line_endpoint_order_is_irrelevant(self):
        s = spec()
        d = spec_as_dump(s)
        for p in d["primitives"]:
            if p["type"] == "line":
                p["x1"], p["x2"], p["y1"], p["y2"] = p["x2"], p["x1"], p["y2"], p["y1"]
        self.assertTrue(compare(s, d)["match"])


class DetectsDifferencesTest(unittest.TestCase):
    def setUp(self):
        self.s = spec()
        self.d = spec_as_dump(self.s)

    def test_dropped_pin_is_missing(self):
        self.d["primitives"] = [p for p in self.d["primitives"]
                                if not (p["type"] == "pin" and p["pin_number"] == "4")]
        r = compare(self.s, self.d)
        self.assertFalse(r["match"])
        self.assertEqual([p["pin_name"] for p in r["pins"]["missing_in_altium"]], ["PA2"])
        self.assertEqual(r["pins"]["unexpected_in_altium"], [])
        self.assertEqual(r["pins"]["changed"], [])

    def test_moved_pin_reports_a_change_not_a_pair(self):
        pin(self.d, "4")["y"] -= 100
        r = compare(self.s, self.d)
        self.assertFalse(r["match"])
        self.assertEqual(len(r["pins"]["changed"]), 1)
        c = r["pins"]["changed"][0]
        self.assertEqual((c["pin_number"], c["differs_in"]), ("4", ["y"]))
        self.assertEqual(c["spec"]["y"] - c["altium"]["y"], 100)
        self.assertEqual(r["pins"]["missing_in_altium"], [])
        self.assertEqual(r["pins"]["unexpected_in_altium"], [])

    def test_wrong_electrical_type(self):
        pin(self.d, "4")["electrical"] = 1          # spec says Output (2) for USART2_TX
        c = compare(self.s, self.d)["pins"]["changed"][0]
        self.assertEqual(c["differs_in"], ["electrical"])
        self.assertEqual((c["spec"]["electrical"], c["altium"]["electrical"]),
                         ("eElectricOutput", "eElectricIO"))

    def test_renamed_label(self):
        lab = next(p for p in self.d["primitives"] if p["type"] == "label" and p["text"] == "SWD")
        lab["text"] = "JTAG"
        r = compare(self.s, self.d)
        self.assertFalse(r["match"])
        self.assertEqual([g[-1] for g in r["graphics"]["missing_in_altium"]], ["SWD"])
        self.assertEqual([g[-1] for g in r["graphics"]["unexpected_in_altium"]], ["JTAG"])

    def test_extra_graphic_in_altium(self):
        self.d["primitives"].append({"type": "rectangle", "x1": 0, "y1": 0, "x2": 10, "y2": 10,
                                     "owner_part_id": 1})
        r = compare(self.s, self.d)
        self.assertEqual(len(r["graphics"]["unexpected_in_altium"]), 1)
        self.assertEqual(r["graphics"]["missing_in_altium"], [])

    def test_part_count_and_name(self):
        self.d["part_count"] = 5
        self.d["symbol_name"] = "SOMETHING_ELSE"
        r = compare(self.s, self.d)
        self.assertFalse(r["match"])
        self.assertFalse(r["part_count"]["same"])
        self.assertFalse(r["symbol_name"]["same"])

    def test_empty_dump_reports_everything_missing(self):
        r = compare(self.s, {"symbol_name": self.s.name, "part_count": self.s.part_count,
                             "primitives": []})
        self.assertFalse(r["match"])
        self.assertEqual(len(r["pins"]["missing_in_altium"]), 20)
        self.assertEqual(r["summary"]["altium_pins"], 0)


class StyleCheckTest(unittest.TestCase):
    """Appearance is checked separately from geometry: the shipped creator
    paints Altium's stock yellow/blue whatever the house library looks like."""

    def test_bgr_round_trip_matches_altium_stock_constants(self):
        from pinout.style import hex_to_bgr, bgr_to_hex, Style
        # the two constants hard-coded in schematic_utils.pas
        self.assertEqual(hex_to_bgr("FFFFB0"), 0x00B0FFFF)
        self.assertEqual(hex_to_bgr("0000FF"), 0x00FF0000)
        self.assertEqual(bgr_to_hex(0x00B0FFFF), "FFFFB0")
        self.assertEqual(Style().bgr("body_fill"), 0x00B0FFFF, "default Style IS Altium stock")

    def test_styled_synthetic_dump_passes_check_style(self):
        from pinout.style import Style
        from pinout.verify import check_style
        st = Style(body_fill="2B2B2B", body_line="E0E0E0", pin_color="FF8800", label_color="00CCFF")
        s = spec()
        r = check_style(spec_as_dump(s, st), st)
        self.assertTrue(r["colours_reported"])
        self.assertTrue(r["match"], r["mismatches"][:3])
        self.assertGreater(r["checked"], 30)
        # and geometry comparison is unaffected by colours being present
        self.assertTrue(compare(s, spec_as_dump(s, st))["match"])

    def test_wrong_body_fill_is_pinpointed_in_hex(self):
        from pinout.style import Style, hex_to_bgr
        from pinout.verify import check_style
        st = Style()
        d = spec_as_dump(spec(), st)
        rect = next(p for p in d["primitives"] if p["type"] == "rectangle")
        rect["area_color"] = hex_to_bgr("123456")
        r = check_style(d, st)
        self.assertFalse(r["match"])
        self.assertEqual(len(r["mismatches"]), 1)
        m = r["mismatches"][0]
        self.assertEqual((m["kind"], m["field"], m["expected"], m["actual"]),
                         ("rectangle", "body_fill", "FFFFB0", "123456"))

    def test_divider_colour_is_its_own_check(self):
        # the house library uses a different red for dividers (980000) than for
        # the body outline (800000); the checker must not conflate them
        from pinout.style import Style, hex_to_bgr
        from pinout.verify import check_style
        st = Style(body_fill="FFFFFF", body_line="800000", divider_color="980000",
                   pin_color="000000", label_color="980000")
        d = spec_as_dump(spec(), st)
        poly = next(p for p in d["primitives"] if p["type"] == "polyline")
        self.assertEqual(poly["color"], hex_to_bgr("980000"), "synthetic dump uses divider colour")
        self.assertTrue(check_style(d, st)["match"])
        poly["color"] = hex_to_bgr("800000")            # painted like the outline instead
        r = check_style(d, st)
        self.assertFalse(r["match"])
        self.assertEqual(r["mismatches"][0]["field"], "divider_color")
        self.assertEqual(Style(body_line="800000").colour("divider_color"), "800000",
                         "empty divider_color falls back to body_line")

    def test_house_style_file_loads_and_is_not_stock(self):
        import tempfile
        from pinout.style import Style
        house = Style.load(Path(__file__).resolve().parents[1] / "house_style.json")
        self.assertEqual((house.body_fill, house.body_line, house.divider_color, house.pin_color,
                          house.label_color, house.line_width),
                         ("FFFFFF", "800000", "980000", "000000", "980000", 1))
        self.assertNotEqual(house, Style(), "the house palette is not Altium stock")
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.json"
            house.save(f, provenance="test")
            self.assertEqual(Style.load(f), house)

    def test_old_dump_without_colours_is_reported_not_failed(self):
        from pinout.style import Style
        from pinout.verify import check_style
        r = check_style(spec_as_dump(spec()), Style())          # no style -> no colour fields
        self.assertFalse(r["colours_reported"])
        self.assertIsNone(r["match"])
        # the real Altium fixtures predate colour reporting - same outcome
        golden = json.loads((FIX / "altium_dump_fcnsplit.json").read_text(encoding="utf-8"))
        self.assertFalse(check_style(golden, Style())["colours_reported"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
