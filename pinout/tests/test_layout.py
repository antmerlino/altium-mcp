"""layout: Pinout -> Altium symbol spec, in the format create_schematic_symbol eats."""
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pinout.ioc import IocFile                                   # noqa: E402
from pinout.mcu_db import load_mcu_xml                           # noqa: E402
from pinout.model import Pinout                                  # noqa: E402
from pinout.layout import port_split, functional_split, parse_pin_spec, PITCH  # noqa: E402
from pinout.preview import render_part_svg, write_svgs           # noqa: E402

FIX = Path(__file__).parent / "fixtures"
ETYPES = {"eElectricInput", "eElectricIO", "eElectricOutput", "eElectricOpenCollector",
          "eElectricPassive", "eElectricHiZ", "eElectricOpenEmitter", "eElectricPower"}
ORIENTS = {"eRotate0", "eRotate90", "eRotate180", "eRotate270"}


def build():
    return Pinout.from_sources(load_mcu_xml(FIX / "FAKE32C20.xml"),
                               IocFile.load(FIX / "fake_board.ioc"),
                               board="TESTBOARD", prefix="TB")


class SpecShapeMixin:
    """Checks that hold for ANY strategy."""

    def assertWellFormed(self, spec, pinout):
        pins = [parse_pin_spec(p) for p in spec.pins]
        # every physical pin exactly once, none invented
        self.assertEqual(Counter(p["number"] for p in pins),
                         Counter(x.number for x in pinout.pins))
        for raw, p in zip(spec.pins, pins):
            self.assertEqual(len(raw.split("|")), 8, raw)
            self.assertIn(p["type"], ETYPES, raw)
            self.assertIn(p["orientation"], ORIENTS, raw)
            self.assertEqual(p["y"] % PITCH, 0, f"off-grid pin: {raw}")
            self.assertTrue(1 <= p["part"] <= spec.part_count, raw)
        # no two pins of one part share a location
        locs = Counter((p["part"], p["x"], p["y"]) for p in pins)
        self.assertEqual(max(locs.values()), 1, f"overlapping pins: {locs.most_common(3)}")
        # graphics reference real parts and have the documented field counts
        counts = {"rectangle": 8, "line": 7, "label": 5}
        for g in spec.graphics:
            f = g.split("|")
            self.assertTrue(1 <= int(f[1]) <= spec.part_count, g)
            if f[0] in counts:
                self.assertEqual(len(f), counts[f[0]], g)
            elif f[0] == "polyline":
                self.assertGreaterEqual(len(f), 7, g)
            else:
                self.fail(f"unknown graphic kind {f[0]!r}")
        # the power part is last and holds every supply
        power = {p["number"] for p in pins if p["part"] == spec.part_count}
        for x in pinout.pins:
            if x.mcu_type in ("Power", "Reset"):
                self.assertIn(x.number, power, f"{x.name} not on the power part")
        args = spec.to_tool_args()
        self.assertEqual(set(args), {"symbol_name", "description", "pins", "part_count", "graphics"})


class PortSplitTest(unittest.TestCase, SpecShapeMixin):
    def setUp(self):
        self.pinout = build()
        self.spec = port_split(self.pinout, pin_length=200, mpn="FAKE32C20U6")

    def test_well_formed(self):
        self.assertWellFormed(self.spec, self.pinout)

    def test_one_part_per_port_plus_power(self):
        # PF0/PF1 are oscillator pins -> power part, so PF is NOT its own part
        self.assertEqual(self.spec.part_count, 3)
        by_part = {}
        for p in map(parse_pin_spec, self.spec.pins):
            by_part.setdefault(p["part"], []).append(p["name"])
        self.assertEqual(sorted(by_part[1]), sorted(["PA0", "PA1", "PA2", "PA3", "PA4", "PA5",
                                                     "PA6", "PA7", "PA13", "PA14"]))
        self.assertEqual(sorted(by_part[2]), ["PB0", "PB1", "PB6", "PB7"])
        self.assertEqual(sorted(by_part[3]), ["NRST", "PF0-OSC_IN", "PF1-OSC_OUT", "VDD", "VDDA", "VSS"])

    def test_gpio_parts_are_left_facing_numeric_order(self):
        pa = [p for p in map(parse_pin_spec, self.spec.pins) if p["part"] == 1]
        self.assertTrue(all(p["orientation"] == "eRotate180" for p in pa))
        names_top_down = [p["name"] for p in sorted(pa, key=lambda p: -p["y"])]
        self.assertEqual(names_top_down[:3], ["PA0", "PA1", "PA2"])
        self.assertEqual(names_top_down[-2:], ["PA13", "PA14"], "numeric, not string, order")
        self.assertTrue(all(p["length"] == 200 for p in pa))

    def test_open_bracket_body_on_gpio_parts_rectangle_on_power(self):
        kinds = Counter((int(g.split("|")[1]), g.split("|")[0]) for g in self.spec.graphics)
        self.assertEqual(kinds[(1, "line")], 3)
        self.assertEqual(kinds[(2, "line")], 3)
        self.assertEqual(kinds[(3, "rectangle")], 1)
        self.assertEqual(kinds[(1, "rectangle")], 0)

    def test_power_part_control_left_supplies_right(self):
        pw = {p["name"]: p for p in map(parse_pin_spec, self.spec.pins) if p["part"] == 3}
        for n in ("NRST", "PF0-OSC_IN", "PF1-OSC_OUT"):
            self.assertEqual(pw[n]["orientation"], "eRotate180", n)
        for n in ("VDD", "VDDA", "VSS"):
            self.assertEqual(pw[n]["orientation"], "eRotate0", n)
        self.assertEqual(pw["VDD"]["type"], "eElectricPower")
        self.assertEqual(pw["VSS"]["type"], "eElectricPassive")
        self.assertEqual(pw["NRST"]["type"], "eElectricInput")

    def test_naming(self):
        self.assertEqual(self.spec.name, "EMBEDDED_IC_FAKE32C20U6_TESTBOARD")


class FunctionalSplitTest(unittest.TestCase, SpecShapeMixin):
    def setUp(self):
        self.pinout = build()
        self.spec = functional_split(self.pinout, pin_length=300, mpn="FAKE32C20U6")

    def test_well_formed(self):
        self.assertWellFormed(self.spec, self.pinout)

    def test_small_mcu_fits_one_gpio_part_plus_power(self):
        self.assertEqual(self.spec.part_count, 2)

    def test_groups_are_labelled_by_peripheral(self):
        labels = {g.split("|")[4] for g in self.spec.graphics if g.startswith("label|")}
        self.assertEqual(labels, {"ADC1", "GPIO", "I2C1", "SPI1", "SWD", "USART2", "UNUSED"})

    def test_label_sits_directly_above_its_cluster(self):
        pins = [p for p in map(parse_pin_spec, self.spec.pins) if p["part"] == 1]
        labels = [g.split("|") for g in self.spec.graphics if g.startswith("label|1|")]
        for _, _, lx, ly, text in labels:
            lx, ly = int(lx), int(ly)
            side_x = 300 if lx < 1300 else 2400
            first = [p for p in pins if p["x"] == side_x and p["y"] == ly - PITCH]
            self.assertEqual(len(first), 1, f"no pin one row under label {text}")
        # the SWD cluster is exactly PA13 + PA14, contiguous
        swd = [l for l in labels if l[4] == "SWD"][0]
        y0, x = int(swd[3]), 300 if int(swd[2]) < 1300 else 2400
        under = sorted([p for p in pins if p["x"] == x and y0 - 2 * PITCH <= p["y"] <= y0 - PITCH],
                       key=lambda p: -p["y"])
        self.assertEqual([p["name"] for p in under], ["PA13 (JTMS/SWDIO)", "PA14 (JTCK/SWCLK)"])

    def test_electrical_types_derived_from_signals(self):
        t = {p["name"]: p["type"] for p in map(parse_pin_spec, self.spec.pins)}
        self.assertEqual(t["PA2"], "eElectricOutput")     # USART2_TX
        self.assertEqual(t["PA3"], "eElectricInput")      # USART2_RX
        self.assertEqual(t["PA0"], "eElectricInput")      # ADC1_IN1
        self.assertEqual(t["PB0"], "eElectricOutput")     # GPIO_Output
        self.assertEqual(t["PB6"], "eElectricIO")         # I2C1_SCL
        self.assertEqual(t["PB1"], "eElectricIO")         # unassigned
        self.assertEqual(t["VDDA"], "eElectricPower")

    def test_designer_part_override_is_honoured(self):
        for n in ("PB6", "PB7"):
            self.pinout.pin(n).part = 2
        spec = functional_split(self.pinout, mpn="FAKE32C20U6")
        parts = {p["name"]: p["part"] for p in map(parse_pin_spec, spec.pins)}
        self.assertEqual((parts["PB6"], parts["PB7"]), (2, 2))
        self.assertEqual(spec.part_count, 3, "power part moves after the forced part")
        self.assertWellFormed(spec, self.pinout)

    def test_body_rectangle_and_divider_per_gpio_part(self):
        gfx = [g.split("|") for g in self.spec.graphics if int(g.split("|")[1]) == 1]
        self.assertEqual(sum(1 for g in gfx if g[0] == "rectangle"), 1)
        self.assertEqual(sum(1 for g in gfx if g[0] == "polyline"), 1)


class PreviewTest(unittest.TestCase):
    def test_svg_mentions_every_pin_and_label(self):
        spec = functional_split(build(), mpn="FAKE32C20U6")
        svg = render_part_svg(spec, 1)
        self.assertTrue(svg.startswith("<svg") and svg.rstrip().endswith("</svg>"))
        for p in map(parse_pin_spec, spec.pins):
            if p["part"] == 1:
                self.assertIn(p["name"].replace("&", "&amp;"), svg)
        for lab in ("SWD", "USART2", "SPI1"):
            self.assertIn(f">{lab}</text>", svg)

    def test_one_file_per_part(self):
        spec = port_split(build(), mpn="FAKE32C20U6")
        with tempfile.TemporaryDirectory() as d:
            files = write_svgs(spec, d)
            self.assertEqual(len(files), spec.part_count)
            self.assertTrue(all(f.exists() and f.stat().st_size > 500 for f in files))

    @unittest.skipUnless(__import__("importlib").util.find_spec("PIL"), "Pillow not installed")
    def test_png_per_part_is_a_real_image(self):
        from PIL import Image
        from pinout.preview import write_pngs
        from pinout.style import Style
        st = Style()      # defaults ARE Altium stock, so this is the expected render
        spec = functional_split(build(), mpn="FAKE32C20U6")
        with tempfile.TemporaryDirectory() as d:
            files = write_pngs(spec, d)
            self.assertEqual(len(files), spec.part_count)
            for f in files:
                with Image.open(f) as im:
                    self.assertEqual(im.format, "PNG")
                    self.assertGreater(im.width * im.height, 10000)
                    # not a blank canvas: the body fill and ink must both appear
                    colours = {c for _, c in im.getcolors(maxcolors=100000) or []}
                    self.assertIn(st.rgb("body_fill"), colours, "body fill missing")
                    self.assertIn(st.rgb("pin_color"), colours, "pin ink missing")
            # part 1 carries group labels; their ink must survive the body fill.
            # (Drawing in spec order painted the rectangle last and hid them -
            # a bug the preview found in its own output.)
            with Image.open(files[0]) as im:
                colours = {c for _, c in im.getcolors(maxcolors=100000) or []}
                self.assertIn(st.rgb("label_color"), colours, "group labels painted over")

    @unittest.skipUnless(__import__("importlib").util.find_spec("PIL"), "Pillow not installed")
    def test_a_house_style_changes_every_colour_in_the_render(self):
        # the preview must show what the house palette will look like, not
        # Altium's stock yellow/blue - otherwise it previews the wrong thing
        from PIL import Image
        from pinout.style import Style
        from pinout.preview import write_pngs
        house = Style(body_fill="2B2B2B", body_line="E0E0E0", pin_color="FF8800", label_color="00CCFF")
        spec = functional_split(build(), mpn="FAKE32C20U6")
        with tempfile.TemporaryDirectory() as d:
            f = write_pngs(spec, d, style=house)[0]
            with Image.open(f) as im:
                colours = {c for _, c in im.getcolors(maxcolors=100000) or []}
        for field in ("body_fill", "body_line", "pin_color", "label_color"):
            self.assertIn(house.rgb(field), colours, field)
        self.assertNotIn(Style().rgb("body_fill"), colours, "stock yellow must be gone")

    def test_svg_paints_body_before_labels_and_lines(self):
        spec = functional_split(build(), mpn="FAKE32C20U6")
        svg = render_part_svg(spec, 1)
        first_rect = svg.index("<rect x=")            # the body (the page rect has no x=)
        first_label = svg.index('font-weight="bold"')
        first_line = svg.index("<line x1=")
        self.assertLess(first_rect, first_label, "label would be hidden under the body")
        self.assertLess(first_rect, first_line, "separator would be hidden under the body")


if __name__ == "__main__":
    unittest.main(verbosity=2)
