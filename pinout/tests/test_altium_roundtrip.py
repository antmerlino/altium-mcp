"""Golden round trip against REAL Altium output.

tests/fixtures/altium_dump_*.json are genuine get_symbol_primitives dumps,
captured on AD Develop, of symbols this package generated from the synthetic
fake_board.ioc and created with create_schematic_symbol. If the generator's
output for the same inputs stops matching them, either the generator changed
or Altium did - both are worth knowing before anything is written to a real
library.

No Altium is needed to run this; that is the point of keeping the dumps.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pinout.ioc import IocFile                                  # noqa: E402
from pinout.mcu_db import load_mcu_xml                          # noqa: E402
from pinout.model import Pinout                                 # noqa: E402
from pinout.layout import port_split, functional_split          # noqa: E402
from pinout.verify import compare                               # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def pinout():
    return Pinout.from_sources(load_mcu_xml(FIX / "FAKE32C20.xml"),
                               IocFile.load(FIX / "fake_board.ioc"))


def dump(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class RealAltiumRoundTripTest(unittest.TestCase):
    def test_fixture_really_came_from_altium(self):
        d = dump("altium_dump_fcnsplit.json")
        self.assertIn("get_symbol_primitives", d["_provenance"])
        pins = [p for p in d["primitives"] if p["type"] == "pin"]
        self.assertEqual(len(pins), 20)
        # Altium returns enum INTEGERS and its own key set - not our pipe strings
        self.assertIsInstance(pins[0]["electrical"], int)
        self.assertIn("owner_part_id", pins[0])

    def test_port_split_matches_what_altium_stored(self):
        spec = port_split(pinout(), pin_length=300, mpn="FAKE32C20U6", board="PORTSPLIT")
        r = compare(spec, dump("altium_dump_portsplit.json"))
        self.assertTrue(r["match"], json.dumps(r, indent=1)[:1500])
        self.assertEqual(r["summary"]["pins_identical"], 20)
        self.assertEqual(r["summary"]["graphics_identical"], 7)

    def test_functional_split_matches_what_altium_stored(self):
        spec = functional_split(pinout(), pin_length=300, mpn="FAKE32C20U6", board="FCNSPLIT")
        r = compare(spec, dump("altium_dump_fcnsplit.json"))
        self.assertTrue(r["match"], json.dumps(r, indent=1)[:1500])
        self.assertEqual(r["summary"]["pins_identical"], 20)
        self.assertEqual(r["summary"]["graphics_identical"], 17)

    def test_a_different_pin_length_is_caught(self):
        # the dumps were made at 300 mil; 200 must NOT match - proves the
        # comparison is sensitive, not vacuously true
        spec = port_split(pinout(), pin_length=200, mpn="FAKE32C20U6", board="PORTSPLIT")
        r = compare(spec, dump("altium_dump_portsplit.json"))
        self.assertFalse(r["match"])
        self.assertEqual(len(r["pins"]["changed"]), 20)
        self.assertTrue(all(c["differs_in"] == ["length"] for c in r["pins"]["changed"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
