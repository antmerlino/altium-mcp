"""core against the memory backend: everything the tools do, without Google."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gsheets.backend import MemoryBackend, BackendError  # noqa: E402
from gsheets.core import Sheets, SheetsError              # noqa: E402

SID = "1QDLfB9z8z13GyIRlZ8aJGhlmNW_zDawdt9jtClgl9HU"
URL = f"https://docs.google.com/spreadsheets/d/{SID}/edit?gid=796636920#gid=796636920"
PINS = [
    [],
    ["Pin", "Peripheral", "Function", "Signal Name", "Net Name"],
    ["PA0", "ADC1", "ADC1_INP16", "OBS_3V3_SENSE", '=CONCATENATE("FC_",A3,"__",D3)'],
    ["PA0_C"],
    ["PA1", "ETH", "ETH_REF_CLK", "", "FC_PA1_ETH_REF_CLK"],
    ["PA2", "ETH", "ETH_MDIO", "", "FC_PA2_ETH_MDIO"],
    ["PA3", "USART2", "USART2_RX", "BB_ESC_TX", "FC_PA3_USART2_RX__BB_ESC_TX"],
    ["PA3", "dup", "", "", "FC_PA3_DUP"],
]


def make():
    be = MemoryBackend()
    be.add_book(SID, "Flight Computer Pinning",
                {"Device Tree": [["Domain", "Bus", "CS/Addr"], ["FC_3V3", "SPI1", "CS1"], ["FC_3V3", "I2C1", "0x20"]],
                 "Pins": PINS,
                 "Notes": [["Old Name", "New Name"], ["FC_OLD", "FC_NEW"]],
                 "Hidden": [["secret"]]},
                gids={"Pins": 796636920, "Device Tree": 210430986}, rows=1000, cols=26)
    be.books[SID]["tabs"][3]["hidden"] = True
    be.add_book("2" * 30, "Other Book", {"Sheet1": [["a", "b"]]})
    tmp = tempfile.mkdtemp()
    return be, Sheets(be, tmp)


class AddressingTest(unittest.TestCase):
    def setUp(self):
        self.be, self.s = make()

    def test_resolve(self):
        self.assertEqual(Sheets.resolve(URL), (SID, 796636920))
        self.assertEqual(Sheets.resolve(SID), (SID, None))
        self.assertEqual(Sheets.resolve(f"https://docs.google.com/spreadsheets/d/{SID}/edit"), (SID, None))
        with self.assertRaises(SheetsError):
            Sheets.resolve("Flight Computer Pinning")

    def test_tab_resolution(self):
        m = self.s.meta(SID)
        self.assertEqual(m.tab("Pins").gid, 796636920)
        self.assertEqual(m.tab("pins").title, "Pins", "case-insensitive fallback")
        self.assertEqual(m.tab(210430986).title, "Device Tree")
        self.assertEqual(m.tab("gid=796636920").title, "Pins")
        self.assertEqual(m.tab("#2").title, "Notes")
        self.assertEqual(m.tab(None).title, "Device Tree", "first visible tab")
        self.assertEqual(m.tab(None, 796636920).title, "Pins", "the URL's gid wins")
        with self.assertRaises(SheetsError) as cm:
            m.tab("Nope")
        self.assertIn("Pins", str(cm.exception))

    def test_meta_is_cached_and_forgotten(self):
        self.s.meta(SID)
        n = len(self.be.calls)
        self.s.meta(SID)
        self.assertEqual(len(self.be.calls), n, "second call served from cache")
        self.s.tabs(SID, "create", title="New", apply=True)
        self.assertIn("New", [t.title for t in self.s.meta(SID).tabs])

    def test_open(self):
        o = self.s.open(URL)
        self.assertTrue(o["success"])
        self.assertEqual(o["spreadsheet"], "Flight Computer Pinning")
        self.assertEqual(o["linked_tab"], "Pins")
        pins = next(t for t in o["tabs"] if t["title"] == "Pins")
        self.assertEqual((pins["gid"], pins["header_row"], pins["header"][:2]), (796636920, 2, ["Pin", "Peripheral"]))
        self.assertTrue(next(t for t in o["tabs"] if t["title"] == "Hidden")["hidden"])


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.be, self.s = make()

    def test_auto_header_and_trimming(self):
        r = self.s.read(URL)
        self.assertEqual((r["tab"], r["header_row"], r["first_row"]), ("Pins", 2, 3))
        self.assertEqual([c["header"] for c in r["columns"]], ["Pin", "Peripheral", "Function", "Signal Name", "Net Name"])
        self.assertEqual([c["letter"] for c in r["columns"]], ["A", "B", "C", "D", "E"])
        self.assertEqual(r["rows"][1], ["PA0_C"], "trailing empties trimmed")
        self.assertEqual(r["total_data_rows"], 6)
        self.assertFalse(r["truncated"])
        self.assertEqual(r["rows"][0][4], "(calc)", "formatted render shows the computed value")

    def test_render_formula_and_paging(self):
        r = self.s.read(URL, render="formula", max_rows=2)
        self.assertTrue(r["rows"][0][4].startswith("=CONCATENATE"))
        self.assertTrue(r["truncated"])
        self.assertEqual((r["returned"], r["next_offset"]), (2, 2))
        r2 = self.s.read(URL, max_rows=2, offset=r["next_offset"])
        self.assertEqual((r2["first_row"], r2["rows"][0][0]), (5, "PA1"))

    def test_columns_records_and_explicit_header(self):
        r = self.s.read(URL, columns=["Pin", "E"], as_records=True)
        self.assertEqual(r["rows"][2], {"_row": 5, "Pin": "PA1", "Net Name": "FC_PA1_ETH_REF_CLK"})
        self.assertEqual([c["letter"] for c in r["columns"]], ["A", "E"])
        raw = self.s.read(URL, header_row=None)
        self.assertEqual((raw["header_row"], raw["first_row"], raw["rows"][1][0]), (None, 1, "Pin"))
        self.assertEqual(self.s.read(URL, header_row=2)["header_row"], 2)
        with self.assertRaises(SheetsError):
            self.s.read(URL, render="pretty")

    def test_range_with_tab_prefix_wins(self):
        r = self.s.read(SID, tab="Pins", rng="'Device Tree'!A1:C3")
        self.assertEqual((r["tab"], r["header_row"], r["rows"][0]), ("Device Tree", 1, ["FC_3V3", "SPI1", "CS1"]))

    def test_find(self):
        f = self.s.find(SID, "PA3")
        # row-major: A7, then E7 (FC_PA3_...), then A8, E8
        self.assertEqual([h["cell"] for h in f["matches"]], ["Pins!A7", "Pins!E7", "Pins!A8", "Pins!E8"])
        self.assertEqual(f["matches"][0]["row_values"][:2], ["PA3", "USART2"])
        self.assertEqual(f["tabs_searched"], ["Device Tree", "Pins", "Notes", "Hidden"])
        self.assertEqual(len(self.s.find(SID, "^PA[0-2]$", regex=True, tab="Pins")["matches"]), 3)
        self.assertEqual(self.s.find(SID, "pa3", match_case=True)["matches"], [])
        self.assertEqual(self.s.find(SID, "CONCATENATE", tab="Pins", in_formulas=True)["matches"][0]["cell"], "Pins!E3")
        self.assertEqual(self.s.find(SID, "CONCATENATE", tab="Pins")["matches"], [], "displayed values do not contain the formula text")

    def test_lookup(self):
        r = self.s.lookup(URL, None, "Pin", ["PA0", "PA3", "PZ9"], value_columns=["Function", "Net Name"])
        self.assertEqual(r["found"]["PA0"], {"_row": 3, "Function": "ADC1_INP16", "Net Name": "(calc)"})
        self.assertEqual(r["missing"], ["PZ9"])
        self.assertEqual(r["ambiguous_keys"], {"PA3": [7, 8]})
        self.assertEqual(r["found"]["PA3"]["_row"], 7, "first match returned")
        self.assertEqual(self.s.lookup(URL, None, "A", ["PA1"])["found"]["PA1"]["Peripheral"], "ETH")


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.be, self.s = make()

    def test_preview_classifies_every_cell(self):
        p = self.s.write(URL, None, "D3", [["OBS_3V3_SENSE", "NEW"], [None, ""], ["x", "=SUM(1,2)"]])
        self.assertFalse(p["applied"])
        self.assertEqual(p["ranges"], ["Pins!D3:E5"])
        self.assertEqual(p["cells_unchanged"], 2, "D3 already holds OBS_3V3_SENSE; E4 empty -> empty")
        self.assertEqual(p["cells_left_alone"], 1, "None skips D4")
        self.assertEqual(p["cells_cleared"], 0, "E4 was already empty")
        self.assertEqual(p["formulas_overwritten"], ["Pins!E3"])
        changed = {c["cell"]: c for c in p["changes"]}
        self.assertEqual(set(changed), {"Pins!E3", "Pins!D5", "Pins!E5"})
        self.assertTrue(changed["Pins!E3"]["old"].startswith("=CONCATENATE"))
        self.assertEqual(p["next"], "call again with apply=true to write")
        self.assertEqual(len([c for c in self.be.calls if c[0] == "update_values"]), 0, "preview writes nothing")

    def test_apply_snapshots_writes_and_verifies(self):
        r = self.s.write(URL, None, "F3", [["a"], ["b"]], apply=True)
        self.assertTrue(r["applied"] and r["verified"])
        self.assertEqual(r["cells_changed"], 2)
        self.assertEqual(self.be.grid(SID, "Pins")[2][5], "a")
        snap = json.loads(Path(r["snapshot"]).read_text(encoding="utf-8"))
        self.assertEqual(snap["blocks"][0]["range"], "Pins!F3:F4")
        self.assertEqual(snap["blocks"][0]["values"], [], "was empty before")
        self.assertIn("sheets_restore", r["undo"])

    def test_nothing_to_write(self):
        r = self.s.write(URL, None, "A3", [["PA0"]], apply=True)
        self.assertFalse(r["applied"])
        self.assertEqual(r["cells_unchanged"], 1)

    def test_numbers_compare_loosely_and_formula_writes(self):
        self.s.write(SID, "Notes", "C2", [[5]], apply=True)
        p = self.s.write(SID, "Notes", "C2", [["5.0"]])
        self.assertEqual(p["cells_unchanged"], 1)
        r = self.s.write(SID, "Notes", "C3", [["=SUM(1,2)"]], apply=True)
        self.assertTrue(r["verified"])
        self.assertTrue(self.s.read(SID, "Notes", "C3", render="formula", header_row=None)["rows"][0][0].startswith("=SUM"))

    def test_range_sizing(self):
        self.assertEqual(self.s.write(URL, None, "A3:A3", [["x", "y"]])["ranges"], ["Pins!A3:B3"],
                         "a one-cell range is an anchor, sized by the values")
        with self.assertRaises(SheetsError):
            self.s.write(URL, None, "A3:B3", [["x", "y"], ["z", "w"]])
        p = self.s.write(URL, None, "F3:H5", [["x"]])
        self.assertIn("left alone", p["notes"][0])
        with self.assertRaises(SheetsError):
            self.s.write(URL, None, "Pins", [["x"]])

    def test_grid_grows(self):
        p = self.s.write(URL, None, "A1003", [["late"]])
        self.assertEqual(p["grid_grows_by"], {"rows": 3, "cols": 0})
        r = self.s.write(URL, None, "A1003", [["late"]], apply=True)
        self.assertTrue(r["verified"])
        grow = [c for c in self.be.calls if c[0] == "batch_update" and "appendDimension" in c[2][0]]
        self.assertEqual(grow[0][2][0]["appendDimension"]["length"], 3)
        self.assertEqual(self.s.meta(SID).tab("Pins").rows, 1003)

    def test_update_cells(self):
        p = self.s.update_cells(URL, None, {"B5": "ETHERNET", "C5": None, "E5": ""})
        self.assertEqual(p["cells_changed"], 2)
        self.assertEqual(p["cells_cleared"], 1)
        r = self.s.update_cells(URL, None, {"B5": "ETHERNET", "E5": ""}, apply=True)
        self.assertTrue(r["verified"])
        self.assertEqual(self.be.grid(SID, "Pins")[4][1], "ETHERNET")
        self.assertEqual(self.be.grid(SID, "Pins")[4][4], "")
        with self.assertRaises(SheetsError):
            self.s.update_cells(URL, None, {"Notes!A1": "x"})
        with self.assertRaises(SheetsError):
            self.s.update_cells(URL, None, {"A1:B2": "x"})

    def test_update_by_key_flat_and_structured(self):
        p = self.s.update_by_key(URL, None, "Pin", [{"Pin": "PA1", "Net Name": "FC_PA1_NEW", "Function": "ETH_REF_CLK"},
                                                    {"key": "PA2", "values": {"net name": "FC_PA2_NEW"}},
                                                    {"key": "PZ9", "values": {"Net Name": "nope"}},
                                                    {"key": "PA3", "values": {"Net Name": "amb"}}])
        self.assertEqual(p["key_column"], "Pin")
        self.assertEqual(p["missing_keys"], ["PZ9"])
        self.assertEqual(p["ambiguous_keys"], {"PA3": [7, 8]})
        self.assertEqual({c["cell"] for c in p["changes"]}, {"Pins!E5", "Pins!E6"}, "Function unchanged, PA3 skipped")
        r = self.s.update_by_key(URL, None, "Pin", [{"key": "PA1", "values": {"Net Name": "FC_PA1_NEW"}}], apply=True)
        self.assertTrue(r["applied"] and r["verified"])
        self.assertEqual(self.be.grid(SID, "Pins")[4][4], "FC_PA1_NEW")

    def test_update_by_key_add_missing_and_all_matches(self):
        p = self.s.update_by_key(URL, None, "Pin", [{"key": "PZ9", "values": {"Net Name": "FC_PZ9", "Peripheral": "GPIO"}}],
                                 add_missing=True)
        self.assertEqual(p["ranges"], ["Pins!A9:E9"])
        self.assertEqual({c["cell"]: c["new"] for c in p["changes"]}, {"Pins!A9": "PZ9", "Pins!B9": "GPIO", "Pins!E9": "FC_PZ9"})
        r = self.s.update_by_key(URL, None, "Pin", [{"key": "PA3", "values": {"Function": "BOTH"}}], all_matches=True, apply=True)
        self.assertEqual(r["cells_changed"], 2)
        g = self.be.grid(SID, "Pins")
        self.assertEqual((g[6][2], g[7][2]), ("BOTH", "BOTH"))
        with self.assertRaises(SheetsError):
            self.s.update_by_key(URL, None, "Pin", [{"Net Name": "no key here"}])

    def test_append_rows(self):
        p = self.s.append_rows(URL, None, [["PB0", "TIM3"], {"Pin": "PB1", "Net Name": "FC_PB1"}])
        self.assertEqual(p["ranges"], ["Pins!A9:E10"])
        self.assertIn("appending 2 row(s) at row 9", p["notes"][0])
        r = self.s.append_rows(URL, None, [["PB0", "TIM3"], {"Pin": "PB1", "Net Name": "FC_PB1"}], apply=True)
        self.assertTrue(r["verified"])
        g = self.be.grid(SID, "Pins")
        self.assertEqual((g[8][0], g[8][1], g[9][0], g[9][4]), ("PB0", "TIM3", "PB1", "FC_PB1"))

    def test_clear(self):
        p = self.s.clear(URL, None, "D3:E4")
        self.assertEqual((p["cells_cleared"], p["formulas_cleared"]), (2, 1))
        with self.assertRaises(SheetsError):
            self.s.clear(URL, None, "Pins")
        r = self.s.clear(URL, None, "D3:E4", apply=True)
        self.assertTrue(r["applied"] and r["verified"])
        self.assertEqual(self.be.grid(SID, "Pins")[2][3], "")
        snap = json.loads(Path(r["snapshot"]).read_text(encoding="utf-8"))
        self.assertTrue(snap["blocks"][0]["values"][0][1].startswith("=CONCATENATE"))

    def test_find_replace(self):
        p = self.s.find_replace(SID, "FC_", "X_", tab="Pins")
        self.assertEqual({c["cell"] for c in p["changes"]}, {"Pins!E5", "Pins!E6", "Pins!E7", "Pins!E8"})
        self.assertIn("1 formula cell(s) also match", p["notes"][0])
        rx = self.s.find_replace(SID, r"^FC_(P[A-Z]\d+)_", r"FC_\1__", tab="Pins", regex=True)
        self.assertEqual({c["new"] for c in rx["changes"] if c["cell"] == "Pins!E5"}, {"FC_PA1__ETH_REF_CLK"})
        wb = self.s.find_replace(SID, "FC_", "X_")
        self.assertEqual(set(wb["per_tab"]), {"Device Tree", "Pins", "Notes"}, "workbook-wide preview groups per tab")
        with self.assertRaises(SheetsError):
            self.s.find_replace(SID, "FC_", "X_", apply=True)
        r = self.s.find_replace(SID, "FC_", "X_", tab="Notes", apply=True)
        self.assertTrue(r["verified"])
        self.assertEqual(self.be.grid(SID, "Notes")[1], ["X_OLD", "X_NEW"])
        self.assertEqual(self.s.find_replace(SID, "zzz", "y")["cells_changed"], 0)
        wc = self.s.find_replace(SID, "PA3", "PA9", tab="Pins", whole_cell=True)
        self.assertEqual({c["cell"] for c in wc["changes"]}, {"Pins!A7", "Pins!A8"}, "whole-cell: not inside FC_PA3_...")


class StructureTest(unittest.TestCase):
    def setUp(self):
        self.be, self.s = make()

    def test_tabs(self):
        p = self.s.tabs(SID, "create", title="Audit", index=1)
        self.assertFalse(p["applied"])
        self.assertEqual(p["request"]["addSheet"]["properties"]["index"], 1)
        r = self.s.tabs(SID, "create", title="Audit", index=1, apply=True)
        self.assertEqual([t["title"] for t in r["tabs"]][:3], ["Device Tree", "Audit", "Pins"])
        with self.assertRaises(SheetsError):
            self.s.tabs(SID, "create", title="Audit", apply=True)
        self.s.tabs(SID, "rename", tab="Audit", title="Audit 2", apply=True)
        self.s.tabs(SID, "duplicate", tab="Notes", title="Notes copy", apply=True)
        self.assertEqual(self.be.grid(SID, "Notes copy"), self.be.grid(SID, "Notes"))
        self.s.tabs(SID, "move", tab="Notes copy", index=0, apply=True)
        self.assertEqual(self.s.meta(SID).tabs[0].title, "Notes copy")
        self.s.tabs(SID, "hide", tab="Notes copy", apply=True)
        self.assertEqual(self.s.meta(SID).tab(None).title, "Device Tree", "hidden tabs are never the default")
        with self.assertRaises(SheetsError):
            self.s.tabs(SID, "delete", tab="Notes copy", apply=True)
        with self.assertRaises(SheetsError):
            self.s.tabs(SID, "explode", tab="Notes")

    def test_dimensions(self):
        p = self.s.dimensions(URL, None, "delete_rows", 4, 1)
        self.assertEqual(p["cells_with_values_removed"], 1)
        self.assertEqual(p["removed_preview"], [["PA0_C"]])
        r = self.s.dimensions(URL, None, "delete_rows", 4, 1, apply=True)
        self.assertTrue(r["applied"])
        self.assertEqual(self.be.grid(SID, "Pins")[3][0], "PA1", "rows shifted up")
        self.assertEqual(r["tab_now"]["rows"], 999)
        self.assertTrue(Path(r["snapshot"]).exists())
        r2 = self.s.dimensions(URL, None, "insert_rows", 3, 2, apply=True)
        self.assertEqual(self.be.grid(SID, "Pins")[4][0], "PA0", "PA0 moved from row 3 to row 5")
        self.assertEqual(r2["tab_now"]["rows"], 1001)
        with self.assertRaises(SheetsError):
            self.s.dimensions(URL, None, "delete_rows", 0)
        with self.assertRaises(SheetsError):
            self.s.dimensions(URL, None, "sideways", 1)

    def test_batch_update(self):
        p = self.s.batch_update(SID, [{"appendDimension": {"sheetId": 796636920, "dimension": "COLUMNS", "length": 2}}])
        self.assertEqual((p["applied"], p["requests"]), (False, ["appendDimension"]))
        r = self.s.batch_update(SID, [{"appendDimension": {"sheetId": 796636920, "dimension": "COLUMNS", "length": 2}}], apply=True)
        self.assertTrue(r["applied"])
        self.assertEqual(self.s.meta(SID).tab("Pins").cols, 28)
        with self.assertRaises(SheetsError):
            self.s.batch_update(SID, [{"deleteSheet": {"sheetId": 1}}], apply=True)


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.be, self.s = make()

    def test_snapshot_and_restore_round_trip(self):
        snap = self.s.snapshot(SID, "Pins")
        self.assertEqual(snap["tabs"], ["Pins"])
        self.s.update_cells(URL, None, {"E5": "CHANGED", "F3": "extra"}, apply=True)
        p = self.s.restore(snap["path"])
        self.assertFalse(p["applied"])
        cells = {c["cell"]: (c["old"], c["new"]) for b in p["blocks"] for c in b["changes"]}
        self.assertEqual(cells["Pins!E5"], ("CHANGED", "FC_PA1_ETH_REF_CLK"))
        self.assertEqual(cells["Pins!F3"], ("extra", ""), "a cell written after the snapshot is cleared by the restore")
        r = self.s.restore(snap["path"], apply=True)
        self.assertTrue(all(b["verified"] for b in r["blocks"]))
        g = self.be.grid(SID, "Pins")
        self.assertEqual(g[4][4], "FC_PA1_ETH_REF_CLK")
        self.assertEqual(len(g[2]), 5, "F3 gone again")
        self.assertTrue(self.s.read(URL, render="formula")["rows"][0][4].startswith("=CONCATENATE"), "formula restored as a formula")
        with self.assertRaises(SheetsError):
            self.s.restore(str(Path(snap["path"]).with_name("missing.json")))

    def test_whole_workbook_snapshot(self):
        snap = self.s.snapshot(SID)
        self.assertEqual(snap["tabs"], ["Device Tree", "Pins", "Notes", "Hidden"])
        self.assertGreater(snap["cells"], 10)


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.be, self.s = make()

    def test_export_and_list(self):
        d = tempfile.mkdtemp()
        r = self.s.export(URL, str(Path(d) / "fc.csv"), "csv")
        self.assertEqual((r["tab"], r["rows"]), ("Pins", 8))
        self.assertIn("PA3,USART2", Path(r["path"]).read_text(encoding="utf-8"))
        x = self.s.export(SID, str(Path(d) / "fc.xlsx"))
        self.assertTrue(Path(x["path"]).read_bytes().startswith(b"PK"))
        self.assertEqual(x["tabs"][1], "Pins")
        with self.assertRaises(SheetsError):
            self.s.export(SID, str(Path(d) / "x.pdf"), "pdf")
        self.assertEqual([f["title"] for f in self.s.list("pinning")["spreadsheets"]], ["Flight Computer Pinning"])
        self.assertEqual(self.s.list()["count"], 2)
        self.assertEqual(self.s.whoami()["email"], "tester@example.com")

    def test_backend_errors_surface(self):
        with self.assertRaises(BackendError):
            self.s.meta("3" * 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
