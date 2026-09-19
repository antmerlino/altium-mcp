"""reconcile: synthetic facts for one imaginary board, no Google, no Altium."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pinout import reconcile as rc                                  # noqa: E402
from pinout.manifest import Board, SheetSpec, TableSpec, AltiumSpec   # noqa: E402
from pinout.sources import function_from_cube_label, pin_key, sheet_facts, _mc_style_signal, _mc_style_function  # noqa: E402
from gsheets.backend import MemoryBackend                            # noqa: E402
from gsheets.core import Sheets                                      # noqa: E402

WB = "1" * 30


def board():
    return Board("Test FC", "FC", "unused.ioc", "unused.xml",
                 SheetSpec(WB, TableSpec("Pin Definitions", "Pin name (function after reset)",
                                         {"signal": "CubeMX Peripheral Function", "cube_label": "CubeMX Label",
                                          "label_override": "Label Override", "net": "Net Name", "notes": "Notes"},
                                         header_row=3)),
                 AltiumSpec("D:/none", "EMBEDDED_IC_TEST", ["Sheet1"]))


IOC = {
    "PA3": {"name": "PA3", "ball": "R2", "signal": "USART2_RX", "cube_label": "USART2_RX__BB_ESC_TX", "function": "BB_ESC_TX"},
    "PC0": {"name": "PC0", "ball": "M2", "signal": "GPIO_Output", "cube_label": "DBG_IO1", "function": "DBG_IO1"},
    "PB5": {"name": "PB5", "ball": "A6", "signal": "SPI1_MOSI", "cube_label": None, "function": None},
    "PG9": {"name": "PG9", "ball": "A10", "signal": "USART6_RX", "cube_label": None, "function": None},
    "PH0": {"name": "PH0-OSC_IN (PH0)", "ball": "G1", "signal": None, "cube_label": None, "function": None},
    "PH13": {"name": "PH13", "ball": "E12", "signal": None, "cube_label": None, "function": None},
    "PD5": {"name": "PD5", "ball": "C11", "signal": "USART2_TX", "cube_label": "USART2_TX__BB", "function": "BB"},
}
SHEET = {
    "PA3": {"name": "PA3", "signal": "USART2_RX", "function": "BB_ESC_TX", "net": "FC_PA3_USART2_RX__BB_ESC_TX"},
    "PC0": {"name": "PC0", "signal": "GPIO_Output", "function": "DBG_IO1", "net": "FC_PC0__DBG_IO1"},
    "PB5": {"name": "PB5", "signal": "UART5_RX", "function": None, "net": "FC_PB5_UART5_RX"},          # sheet stale
    "PG9": {"name": "PG9", "signal": "USART6_RX", "function": "DBG", "net": "FC_PG9_USART6_RX__DBG"},   # .ioc lacks fn
    "PH0": {"name": "PH0-OSC_IN (PH0)", "signal": None, "function": None, "net": "FC_OSC_IN"},
    "PD5": {"name": "PD5", "signal": "USART2_TX", "function": "BB", "net": None},                         # sheet net missing
}
SCHEM = {
    "PA3": {"name": "PA3", "ball": "R2", "labels": ["FC_PA3_USART2_RX__BB_ESC_TX"], "net": "FC_PA3_USART2_RX__BB_ESC_TX",
            "signal": "USART2_RX", "function": "BB_ESC_TX", "group": "UART2", "symbol_label": "USART2", "sheets": ["Sheet1"], "wired": True},
    "PC0": {"name": "PC0", "ball": "M2", "labels": ["FC_PC0__DBG_IO1"], "net": "FC_PC0__DBG_IO1", "signal": "GPIO",
            "function": "DBG_IO1", "group": "GPIO", "symbol_label": "GPIO", "sheets": ["Sheet1"], "wired": True},
    "PB5": {"name": "PB5", "ball": "A6", "labels": ["FC_PB5_UART5_RX"], "net": "FC_PB5_UART5_RX", "signal": "UART5_RX",
            "function": None, "group": "UART5", "symbol_label": "UART5", "sheets": ["Sheet1"], "wired": True},
    "PG9": {"name": "PG9", "ball": "A10", "labels": ["FC_PG9_USART6_RX__DBG"], "net": "FC_PG9_USART6_RX__DBG",
            "signal": "USART6_RX", "function": "DBG", "group": "UART6", "symbol_label": "USART6", "sheets": ["Sheet1"], "wired": True},
    "PH0": {"name": "PH0-OSC_IN", "ball": "G1", "labels": ["FC_OSC_IN"], "net": "FC_OSC_IN", "signal": None, "function": None,
            "group": None, "symbol_label": None, "sheets": ["Sheet1"], "wired": True},
    "PH13": {"name": "PH13", "ball": "E12", "labels": ["FC_PH13"], "net": "FC_PH13", "signal": None, "function": None,
             "group": None, "symbol_label": None, "sheets": ["Sheet1"], "wired": True},
    "PD5": {"name": "PD5", "ball": "C11", "labels": ["FC_PD5_USART2_TX__BB"], "net": "FC_PD5_USART2_TX__BB",
            "signal": "USART2_TX", "function": "BB", "group": "GPIO", "symbol_label": "GPIO", "symbol_label_inherited": True,
            "sheets": ["Sheet1"], "wired": True},
}


class HelpersTest(unittest.TestCase):
    def test_pin_key_and_labels(self):
        self.assertEqual(pin_key("PA13 (JTMS/SWDIO)"), "PA13")
        self.assertEqual(pin_key("PA0 C"), "PA0_C")
        self.assertEqual(pin_key("PH0-OSC_IN"), "PH0")
        self.assertEqual(function_from_cube_label("USART2_RX", "USART2_RX__BB_ESC_TX"), "BB_ESC_TX")
        self.assertEqual(function_from_cube_label("ADC1_INP16", "OBS1_3V3_SENSE"), "OBS1_3V3_SENSE")
        self.assertIsNone(function_from_cube_label("DEBUG_JTMS-SWDIO", "SWDIO"), "a label that re-spells the signal is not a function")
        self.assertEqual(function_from_cube_label("GPIO_Output", "DBG_IO1"), "DBG_IO1")
        self.assertIsNone(function_from_cube_label("GPIO_Output", "SPI5_nCS1"), "a role word is not a function")
        self.assertEqual(function_from_cube_label("GPIO_Output", "SPI3_nCS2__SE1"), "SE1")
        self.assertEqual(rc.expected_cube_label("GPIO_Output", "BB", None, role="SPI5_nCS1"), "SPI5_nCS1__BB")
        self.assertEqual(rc.expected_cube_label("GPIO_Output", None, None, role="SPI5_nCS1"), "SPI5_nCS1")
        self.assertEqual(function_from_cube_label("UART5_RTS", "UART5_RTS__MC"), "MC")
        self.assertEqual(rc.expected_cube_label("USART2_RX", "BB_ESC_TX"), "USART2_RX__BB_ESC_TX")
        self.assertEqual(rc.expected_cube_label("GPIO_Output", "DBG_IO1"), "DBG_IO1")
        self.assertEqual(rc.expected_cube_label("ADC1_INP16", "OBS1_3V3_SENSE"), "OBS1_3V3_SENSE")
        self.assertEqual(rc.expected_cube_label("S_TIM8_CH1", "BB_PWM2"), "TIM8_CH1__BB_PWM2")
        self.assertEqual(rc.expected_cube_label("DEBUG_JTMS-SWDIO", None, "SWDIO"), "SWDIO")
        self.assertIsNone(rc.expected_cube_label("USART1_TX", None, None))

    def test_mc_style_rows(self):
        self.assertEqual(_mc_style_signal("USART1", "TX", "O"), "USART1_TX")
        self.assertEqual(_mc_style_signal("IO", "PWROFF_EN", "O"), "GPIO_Output")
        self.assertEqual(_mc_style_signal("IO", "MEI_IRQ2", "I"), "GPIO_Input")
        self.assertEqual(_mc_style_signal("DEBUG", "JTMS-SWDIO", "I/O"), "DEBUG_JTMS-SWDIO")
        self.assertEqual(_mc_style_function("USART1", "TX", "MEI", None), "MEI")
        self.assertEqual(_mc_style_function("UART5", "TX", "FC", "PD2_UART5_RX"), "FC_PD2_UART5_RX")
        self.assertEqual(_mc_style_function("IO", "3V3_EN", "MEI", None), "MEI_3V3_EN")
        self.assertEqual(_mc_style_function("IO", "PWROFF_EN", None, None), "PWROFF_EN")
        self.assertIsNone(_mc_style_function("I2C1", "SCL", None, None))


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.rep = rc.reconcile(board(), IOC, SHEET, SCHEM)
        self.by = {d.id: d for d in self.rep.diffs}

    def test_gpio_with_a_role_word_is_not_a_signal_conflict(self):
        ioc = {"PE6": {"name": "PE6", "ball": "B3", "signal": "GPIO_Output", "cube_label": "SPI6_nCS2__SE2", "function": "SPI6_nCS2__SE2"}}
        sh = {"PE6": {"name": "PE6", "signal": "GPIO_Output", "function": "SPI6_nCS2__SE2", "net": "FC_PE6_SPI6_nCS2__SE2"}}
        sc = {"PE6": {"name": "PE6", "ball": "B3", "labels": ["FC_PE6_SPI6_nCS2__SE2"], "net": "FC_PE6_SPI6_nCS2__SE2",
                      "signal": "SPI6_nCS2", "function": "SE2", "group": "SPI6", "symbol_label": "SPI6", "sheets": ["S"], "wired": True}}
        rep = rc.reconcile(board(), ioc, sh, sc)
        self.assertEqual([d.id for d in rep.diffs], [], rc.format_report(rep))

    def test_sheet_role_word_on_a_gpio_pin_is_not_a_signal_conflict(self):
        # the MC sheet files a chip-select under SPI5/nCS1; CubeMX calls the pin GPIO_Output
        ioc = {"PK6": {"name": "PK6", "ball": "C7", "signal": "GPIO_Output", "cube_label": "SPI5_nCS1", "function": "SPI5_nCS1",
                       "alternates": ["SPI5_NSS", "TIM3_CH1"]}}
        sh = {"PK6": {"name": "PK6", "signal": "SPI5_nCS1", "function": "BB", "net": "MC_PK6_SPI5_nCS1__BB"}}
        sc = {"PK6": {"name": "PK6", "ball": "C7", "labels": ["MC_PK6_SPI5_nCS1__BB"], "net": "MC_PK6_SPI5_nCS1__BB",
                      "signal": "SPI5_nCS1", "function": "BB", "group": "SPI5", "symbol_label": "SPI5", "sheets": ["S"], "wired": True}}
        rep = rc.reconcile(Board("Test MC", "MC", "x", "y", board().sheet, board().altium), ioc, sh, sc)
        self.assertNotIn("PK6:signal", {d.id for d in rep.diffs})
        fn = next(d for d in rep.diffs if d.id == "PK6:function")
        self.assertEqual(fn.proposed, "BB")
        self.assertEqual(fn.apply_to[0]["value"], "SPI5_nCS1__BB", "the role word stays in the .ioc label")

    def test_agreeing_pins_produce_no_rows(self):
        self.assertNotIn("PA3:signal", self.by)
        self.assertNotIn("PA3:function", self.by)
        self.assertNotIn("PA3:net", self.by)
        self.assertNotIn("PC0:net", self.by)
        self.assertGreaterEqual(self.rep.agree["net"], 2)

    def test_stale_sheet_and_schematic_signal(self):
        d = self.by["PB5:signal"]
        self.assertEqual(d.status, "conflict")
        self.assertEqual(d.proposed, "SPI1_MOSI", "the .ioc owns the signal")
        targets = {a["target"] for a in d.apply_to}
        self.assertIn("sheet", targets)
        self.assertIn("altium", targets, "symbol files PB5 under UART5")
        n = self.by["PB5:net"]
        self.assertEqual(n.status, "conflict", "sheet and schematic agree on a name that encodes the wrong signal")
        self.assertEqual(n.proposed, "FC_PB5_SPI1_MOSI", "the generated name from the owners")
        self.assertEqual({a["target"] for a in n.apply_to}, {"sheet", "altium"})

    def test_function_missing_in_ioc_proposes_a_cube_label(self):
        d = self.by["PG9:function"]
        self.assertEqual(d.status, "missing:cubemx")
        self.assertEqual(d.proposed, "DBG")
        self.assertEqual(d.apply_to, [{"target": "ioc", "field": "GPIO_Label", "pin": "PG9", "value": "USART6_RX__DBG", "current": None}])

    def test_sheet_missing_net_is_proposed_from_schematic(self):
        d = self.by["PD5:net"]
        self.assertEqual(d.status, "missing:sheet")
        self.assertEqual(d.proposed, "FC_PD5_USART2_TX__BB")
        self.assertEqual(d.apply_to[0]["target"], "sheet")
        s = self.by["PD5:signal"]
        self.assertIn("symbol", s.note, "a USART pin drawn in the GPIO region is flagged")

    def test_unassigned_and_spare_pins(self):
        self.assertNotIn("PH0:net", self.by, "sheet and schematic agree on FC_OSC_IN")
        d = self.by["PH0:signal"]
        self.assertEqual(d.status, "missing:cubemx")
        self.assertIn("unassigned", d.note)
        self.assertEqual(d.apply_to, [], "configuring the oscillator is a CubeMX decision, not a write we make")
        self.assertNotIn("PH13:net", self.by, "a spare pin named after itself agrees everywhere")

    def test_report_rendering_and_json(self):
        text = rc.format_report(self.rep)
        self.assertIn("disagreements", text)
        j = json.loads(self.rep.to_json())
        self.assertEqual(j["summary"]["board"], "Test FC")
        self.assertTrue(all(len(r) == len(rc.REVIEW_HEADER) for r in rc.review_rows(self.rep)))


class PublishCollectApplyTest(unittest.TestCase):
    def setUp(self):
        self.be = MemoryBackend()
        self.be.add_book(WB, "Test FC Pinning", {
            "Pin Definitions": [[], [], ["Ball", "x", "y", "Pin name (function after reset)", "CubeMX Peripheral Function",
                                         "CubeMX Label", "Label Override", "Net Name", "Notes"],
                                ["R2", "", "", "PA3", "USART2_RX", "USART2_RX__BB_ESC_TX", "", "FC_PA3_USART2_RX__BB_ESC_TX", ""],
                                ["A6", "", "", "PB5", "UART5_RX", "", "", "FC_PB5_UART5_RX", ""],
                                ["C11", "", "", "PD5", "USART2_TX", "USART2_TX__BB", "", "#REF!", ""],
                                ["-", "", "", "PA0_C", "", "", "", "", ""],
                                ["Z9", "", "", "NRST", "", "", "", "", ""]]})
        self.sheets = Sheets(self.be, tempfile.mkdtemp())
        self.board = board()

    def test_sheet_facts_from_memory_backend(self):
        facts, diag = sheet_facts(self.board, self.sheets)
        self.assertEqual(facts["PA3"]["signal"], "USART2_RX")
        self.assertEqual(facts["PA3"]["function"], "BB_ESC_TX")
        self.assertEqual(facts["PA3"]["net"], "FC_PA3_USART2_RX__BB_ESC_TX")
        self.assertIn("PA0_C", facts)
        self.assertEqual([e["pin"] for e in diag["errors"]], ["PD5"], "#REF! is a diagnostic, not a value")
        self.assertNotIn("net", facts["PD5"])
        self.assertEqual([u["key"] for u in diag["unknown_pins"]], ["NRST"])

    def test_publish_collect_plan_apply(self):
        facts, diag = sheet_facts(self.board, self.sheets)
        rep = rc.reconcile(self.board, {k: v for k, v in IOC.items() if k in ("PA3", "PB5", "PD5")}, facts,
                           {k: v for k, v in SCHEM.items() if k in ("PA3", "PB5", "PD5")}, {"sheet": diag})
        ids = {d.id for d in rep.diffs}
        self.assertIn("PB5:signal", ids)
        self.assertIn("PD5:sheet_error:net", ids)
        pv = rc.publish(rep, self.board, self.sheets)
        self.assertFalse(pv["applied"])
        self.assertFalse(pv["exists"])
        out = rc.publish(rep, self.board, self.sheets, apply=True)
        self.assertTrue(out["applied"])
        self.assertIn("Reconcile", [t["title"] for t in self.sheets.open(WB)["tabs"]])
        grid = self.be.grid(WB, "Reconcile")
        self.assertEqual(grid[0][:4], ["Id", "Pin", "Ball", "Field"])
        dv = [c for c in self.be.calls if c[0] == "batch_update" and "setDataValidation" in c[2][0]]
        self.assertTrue(dv, "Decision column got a dropdown")
        # the designer decides in the sheet
        self.sheets.update_by_key(WB, "Reconcile", "Id", [{"key": "PB5:signal", "values": {"Decision": "accept"}},
                                                          {"key": "PD5:net", "values": {"Decision": "custom", "Custom value": "FC_PD5_CUSTOM"}}],
                                  header_row=1, apply=True)
        decisions = rc.collect(self.board, self.sheets)
        self.assertEqual(decisions["PB5:signal"]["decision"], "accept")
        plan = rc.plan_writes(rep, decisions)
        self.assertEqual(plan["counts"]["sheet"], 2, "PB5 signal column + PD5 net column")
        self.assertEqual(plan["counts"]["altium"], 1, "PB5 symbol label")
        self.assertEqual([w["value"] for w in plan["writes"]["sheet"] if w["pin"] == "PD5"], ["FC_PD5_CUSTOM"])
        pre = rc.apply_sheet_writes(self.board, self.sheets, plan["writes"]["sheet"])
        self.assertFalse(pre["applied"])
        done = rc.apply_sheet_writes(self.board, self.sheets, plan["writes"]["sheet"], apply=True)
        self.assertTrue(done["tables"][0]["verified"])
        g = self.be.grid(WB, "Pin Definitions")
        self.assertEqual(g[4][4], "SPI1_MOSI", "PB5 signal column now follows the .ioc")
        self.assertEqual(g[5][7], "FC_PD5_CUSTOM")
        pay = rc.altium_payloads(plan["writes"]["altium"])
        self.assertEqual(pay["symbol_label_edits"]["EMBEDDED_IC_TEST"][0]["to"], "SPI1")
        # republishing replaces the tab content instead of stacking
        rc.publish(rep, self.board, self.sheets, apply=True)
        self.assertEqual(self.be.grid(WB, "Reconcile")[0][0], "Id")


if __name__ == "__main__":
    unittest.main(verbosity=2)
