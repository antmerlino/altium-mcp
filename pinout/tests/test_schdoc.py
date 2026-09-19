"""schdoc: a sheet read offline, from a synthetic FileHeader stream."""
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pinout.schdoc import (parse_records, component_dump, find_components, pin_nets,  # noqa: E402
                           pin_connect_point, designator_of)
from pinout.audit import infer_clusters   # noqa: E402


def stream(records):
    """Encode pipe-delimited records the way Altium does: 4-byte header
    (length in the low 24 bits, 0 in the top byte), body, NUL."""
    out = b""
    for r in records:
        body = r.encode("latin-1") + b"\x00"
        out += struct.pack("<I", len(body)) + body
    return out


# Record list index 0 is the header; OwnerIndex counts from the record AFTER it,
# so the component at list index 1 is OwnerIndex=0.
SHEET = [
    "|HEADER=Protel for Windows - Schematic Capture Binary File Version 5.0|Weight=12",
    "|RECORD=1|LibReference=EMBEDDED_IC_TEST_X2_CB|ComponentDescription=TEST MCU|PartCount=2"
    "|CurrentPartId=1|Location.X=100|Location.Y=100|UniqueID=AAAAAAAA",
    # part 1: two pins on the left edge (orientation 2), one label, body, a divider
    "|RECORD=2|OwnerIndex=0|OwnerPartId=1|Electrical=1|PinConglomerate=26|PinLength=30"
    "|Location.X=130|Location.Y=200|Name=PA0|Designator=N3",
    "|RECORD=2|OwnerIndex=0|OwnerPartId=1|Electrical=2|PinConglomerate=26|PinLength=30"
    "|Location.X=130|Location.Y=190|Name=PA1|Designator=N2",
    "|RECORD=4|OwnerIndex=0|OwnerPartId=1|Location.X=170|Location.Y=200|Color=38912|FontID=1|Text=USART2",
    "|RECORD=14|OwnerIndex=0|OwnerPartId=1|Location.X=130|Location.Y=100|Corner.X=340|Corner.Y=210"
    "|LineWidth=1|Color=32768|AreaColor=16777215|IsSolid=T",
    "|RECORD=6|OwnerIndex=0|OwnerPartId=1|LocationCount=2|X1=230|Y1=210|X2=230|Y2=100|Color=38912",
    "|RECORD=13|OwnerIndex=0|OwnerPartId=1|Location.X=130|Location.Y=150|Corner.X=230|Corner.Y=150|Color=38912",
    # part 2: one pin, right edge (orientation 0)
    "|RECORD=2|OwnerIndex=0|OwnerPartId=2|Electrical=7|PinConglomerate=24|PinLength=30"
    "|Location.X=340|Location.Y=200|Name=VDD|Designator=A1",
    "|RECORD=34|OwnerIndex=0|Text=U22|Name=Designator",
    # wiring on this sheet: PA0's connect point is (100,200) units; a wire runs
    # from there to (60,200), with a net label on it and a power port at its end
    "|RECORD=27|LocationCount=2|X1=100|Y1=200|X2=60|Y2=200",
    "|RECORD=25|Location.X=80|Location.Y=200|Text=FC_PA0_ADC1_INP16__SENSE",
    "|RECORD=17|Location.X=60|Location.Y=200|Text=FC_3V3",
    # PA1's connect point is (100,190): a label sits directly on it, no wire
    "|RECORD=25|Location.X=100|Location.Y=190|Text=FC_PA1_SPARE",
    # a second, unrelated wire+label the MCU is not on
    "|RECORD=27|LocationCount=3|X1=500|Y1=500|X2=600|Y2=500|X3=600|Y3=600",
    "|RECORD=25|Location.X=600|Location.Y=550|Text=OTHER_NET",
]


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.recs = parse_records(stream(SHEET))

    def test_records_and_component(self):
        self.assertEqual(len(self.recs), len(SHEET))
        self.assertEqual(find_components(self.recs), [1])
        self.assertEqual(find_components(self.recs, "EMBEDDED_IC_TEST_X2_CB"), [1])
        self.assertEqual(find_components(self.recs, "NOPE"), [])
        self.assertEqual(designator_of(self.recs, 1), "U22")

    def test_component_dump_has_every_part_in_mils(self):
        d = component_dump(self.recs, 1)
        self.assertEqual((d["symbol_name"], d["part_count"], d["current_part_id"], d["designator"]),
                         ("EMBEDDED_IC_TEST_X2_CB", 2, 1, "U22"))
        pins = {p["pin_number"]: p for p in d["primitives"] if p["type"] == "pin"}
        self.assertEqual(sorted(pins), ["A1", "N2", "N3"])
        pa0 = pins["N3"]
        self.assertEqual((pa0["pin_name"], pa0["electrical"], pa0["orientation"], pa0["length"],
                          pa0["x"], pa0["y"], pa0["owner_part_id"]),
                         ("PA0", 1, 2, 300, 1300, 2000, 1))
        self.assertTrue(pa0["show_name"] and pa0["show_designator"])
        self.assertEqual(pins["A1"]["owner_part_id"], 2)
        self.assertEqual(pins["A1"]["orientation"], 0)
        kinds = sorted(p["type"] for p in d["primitives"])
        self.assertEqual(kinds, ["label", "line", "pin", "pin", "pin", "polyline", "rectangle"])
        rect = next(p for p in d["primitives"] if p["type"] == "rectangle")
        self.assertEqual((rect["x1"], rect["y1"], rect["x2"], rect["y2"], rect["is_solid"],
                          rect["area_color"]), (1300, 1000, 3400, 2100, True, 0xFFFFFF))
        poly = next(p for p in d["primitives"] if p["type"] == "polyline")
        self.assertEqual(poly["vertices"], [{"x": 2300, "y": 2100}, {"x": 2300, "y": 1000}])

    def test_dump_feeds_the_audit_clusterer(self):
        clusters = [c for c in infer_clusters(component_dump(self.recs, 1)) if c.pins]
        left = next(c for c in clusters if c.part == 1 and c.side == "left")
        self.assertEqual(left.label, "USART2")
        self.assertEqual([p["pin_name"] for p in left.pins], ["PA0", "PA1"])

    def test_pin_nets_follow_wires_and_direct_labels(self):
        pa0 = component_dump(self.recs, 1)["primitives"][0]
        self.assertEqual(pin_connect_point(pa0), (1000, 2000), "left-pointing pin: connect point is x - length")
        nets = pin_nets(self.recs, 1)
        self.assertEqual(sorted(nets), ["N2", "N3"], "only the part drawn on this sheet")
        self.assertEqual(nets["N3"]["nets"], ["FC_PA0_ADC1_INP16__SENSE"])
        self.assertEqual(nets["N3"]["power"], ["FC_3V3"])
        self.assertTrue(nets["N3"]["wired"])
        self.assertEqual(nets["N2"]["nets"], ["FC_PA1_SPARE"])
        self.assertFalse(nets["N2"]["wired"])
        self.assertNotIn("OTHER_NET", nets["N3"]["nets"] + nets["N2"]["nets"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
