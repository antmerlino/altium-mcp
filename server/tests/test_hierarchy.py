"""Offline tests for sheet symbols, sheet entries and the cross-sheet check.

Geometry and members measured on the X9 parent sheet (MissionComputer.SchDoc)
on 2026-09-20: a sheet symbol has SheetName.Text / SheetFileName.Text /
Location / XSize / YSize, and each entry a Name, Side (1 = right edge),
DistanceFromTop, IOType and HarnessType.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402

DUMP = ("sheetsymbol|MC_Comms|MC_Comms.SchDoc|45000000|40000000|20000000|29000000"
        "|I2C1,1,1000000,0,I2C|PA9,1,17000000,2,")

HIER = "\n".join([
    r"parent|D:\Altium\Board\Top.SchDoc",
    "symbol|Comms|Comms.SchDoc|I2C1,1,1000000,0,I2C|SPI3,1,11000000,0,SPI|SWD,1,21000000,0,SWD",
    "childports|Comms.SchDoc|I2C1,I2C|SPI6,SPI|SWD,SPI|PA0_ADC1_INP16,",
])


class DumpTest(unittest.TestCase):
    def test_kind_emitted_only_when_asked(self):
        self.assertIn("eSheetSymbol", main._sch_dump_script(["sheetsymbol"]))
        self.assertIn("eSheetEntry", main._sch_dump_script(["sheetsymbol"]))
        self.assertNotIn("eSheetSymbol", main._sch_dump_script(["port"]))

    def test_parses_symbol_and_entries(self):
        _, objs = main._parse_sch_dump(DUMP)
        o = objs[0]
        self.assertEqual((o["name"], o["file_name"]), ("MC_Comms", "MC_Comms.SchDoc"))
        self.assertEqual((o["x"], o["y"], o["x_size"], o["y_size"]),
                         (45000000, 40000000, 20000000, 29000000))
        self.assertEqual(o["entries"][0], {"name": "I2C1", "side": 1, "distance_from_top": 1000000,
                                           "io_type": 0, "harness_type": "I2C"})

    def test_entry_with_no_harness_type_parses(self):
        _, objs = main._parse_sch_dump(DUMP)
        self.assertEqual(objs[0]["entries"][1]["harness_type"], "")
        self.assertEqual(objs[0]["entries"][1]["io_type"], 2)


class EntryScriptTest(unittest.TestCase):
    def script(self):
        return main._set_sheet_entries_script(
            "MC_Comms.SchDoc",
            [{"name": "SPI6", "distance_from_top": 15000000, "harness_type": "SPI"}],
            [{"match": {"name": "SPI3"}, "name": "QSPI", "harness_type": "QSPI"}],
            ["FMC_SDRAM"])

    def test_finds_the_symbol_by_either_name(self):
        s = self.script()
        self.assertIn("Obj6.SheetName.Text = 'MC_Comms.SchDoc'", s)
        self.assertIn("Obj6.SheetFileName.Text = 'MC_Comms.SchDoc'", s)
        self.assertIn("ABORT|sheet symbol MC_Comms.SchDoc not found", s)

    def test_add_builds_the_entry_on_its_owning_symbol(self):
        s = self.script()
        self.assertIn("SchObjectFactory(eSheetEntry, eCreate_Default)", s)
        self.assertIn("Obj7.Name := 'SPI6';", s)
        self.assertIn("Obj7.DistanceFromTop := 15000000;", s)
        self.assertIn("Obj7.HarnessType := 'SPI';", s)
        self.assertIn("Obj9.AddSchObject(Obj7);", s)
        # the factory-child trap: placed explicitly, on the right edge for side 1
        self.assertIn("Obj7.Location := Point(Obj9.Location.X + Obj9.XSize, Obj9.Location.Y - 15000000);", s)

    def test_left_side_entry_anchors_on_the_left_edge(self):
        s = main._set_sheet_entries_script("X", [{"name": "nRST", "distance_from_top": 3000000, "side": 0}], [], [])
        self.assertIn("Obj7.Location := Point(Obj9.Location.X, Obj9.Location.Y - 3000000);", s)
        self.assertIn("Obj7.Side := 0;", s)
        self.assertNotIn("HarnessType", s, "a plain entry carries no harness type")

    def test_rename_sets_members_under_a_modify_pair(self):
        s = self.script()
        self.assertIn("if Obj7.Name = 'SPI3' then", s)
        self.assertIn("Obj7.Name := 'QSPI';", s)
        self.assertIn("Obj7.HarnessType := 'QSPI';", s)
        self.assertIn("SCHM_BeginModify", s)

    def test_delete_removes_from_the_symbol_not_the_sheet(self):
        s = self.script()
        self.assertIn("Obj9.RemoveSchObject(Obj7);", s)
        self.assertNotIn("Obj1.RemoveSchObject", s)

    def test_transaction_and_quoting(self):
        s = self.script()
        self.assertIn("if Obj1.ObjectId <> 32 then", s)
        self.assertEqual(s.count("PreProcess"), 1)
        # One pair per script: upstream's sandbox refuses an unbalanced PreProcess/PostProcess count and
        # closes the undo group an early Exit leaves open (SbxCloseUndo), so the guard no longer closes it.
        self.assertEqual(s.count("PostProcess"), 1, "the tail only")
        self.assertNotIn('"', s)


class HierarchyCompareTest(unittest.TestCase):
    def parsed(self):
        return main._parse_hierarchy(HIER)

    def test_parses_parent_symbols_and_child_ports(self):
        parent, syms, ports, missing = self.parsed()
        self.assertTrue(parent.endswith("Top.SchDoc"))
        self.assertEqual(syms[0]["file_name"], "Comms.SchDoc")
        self.assertEqual(len(syms[0]["entries"]), 3)
        self.assertEqual(ports["Comms.SchDoc"][0], {"name": "I2C1", "harness_type": "I2C"})
        self.assertEqual(ports["Comms.SchDoc"][3], {"name": "PA0_ADC1_INP16", "harness_type": ""})
        self.assertEqual(missing, [])

    def test_reports_each_kind_of_mismatch(self):
        _, syms, ports, missing = self.parsed()
        findings, per = main._compare_hierarchy(syms, ports, missing)
        rules = {f["rule"] for f in findings}
        self.assertIn("entry_without_port", rules, "SPI3 entry, no SPI3 port")
        self.assertIn("port_without_entry", rules, "SPI6 and the plain ADC port")
        self.assertIn("harness_type_mismatch", rules, "SWD entry SWD vs port SPI")
        self.assertEqual(per[0]["entries"], 3)
        self.assertEqual(per[0]["ports"], 4)
        self.assertEqual(per[0]["matched"], 2, "I2C1 and SWD match by name")

    def test_a_matching_pair_is_silent(self):
        text = "\n".join(["parent|X", "symbol|C|C.SchDoc|I2C1,1,1000000,0,I2C", "childports|C.SchDoc|I2C1,I2C"])
        _, syms, ports, missing = main._parse_hierarchy(text)
        findings, _ = main._compare_hierarchy(syms, ports, missing)
        self.assertEqual(findings, [])

    def test_child_not_in_memory_is_information_not_an_error(self):
        text = "\n".join(["parent|X", "symbol|C|C.SchDoc|I2C1,1,1000000,0,I2C", "childmissing|C.SchDoc"])
        _, syms, ports, missing = main._parse_hierarchy(text)
        findings, per = main._compare_hierarchy(syms, ports, missing)
        self.assertTrue(all(f["severity"] == "info" for f in findings), findings)
        self.assertIsNone(per[0]["ports"])


if __name__ == "__main__":
    unittest.main()
