"""Offline tests for the convention lint: a synthetic house cluster that passes,
then one broken piece at a time."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sheetlint  # noqa: E402

U = 10000


def cluster():
    """A left-side SPI cluster the way the X9 sheet draws it: three pins at
    x=6900 on rows 63/64/65, wires to x=3700, labels at 3900, class marks at
    the pin end, an MFG pad with its mark on SCK, a harness connector with
    entries at the wire ends, a signal harness to the SPI5 port."""
    objs = [{"kind": "component", "designator": "U?", "lib_reference": "MCU", "x": 0, "y": 0,
             "current_part_id": 3, "part_count": 3},
            {"kind": "component", "designator": "BOT1", "lib_reference": "TESTPOINT_PAD_MFG",
             "x": 6400 * U, "y": 6500 * U, "current_part_id": 1, "part_count": 1},
            {"kind": "pin", "designator": "1", "name": "1", "owner_designator": "BOT1",
             "owner_lib_reference": "TESTPOINT_PAD_MFG", "owner_part_id": 1, "x": 6400 * U,
             "y": 6550 * U, "orientation": 3, "pin_length": 50 * U, "connect_x": 6400 * U, "connect_y": 6500 * U}]
    rows = [(6500, "SCK", "PK0"), (6400, "MOSI", "PJ10"), (6300, "MISO", "PJ11")]
    for i, (y, sig, pin) in enumerate(rows):
        objs.append({"kind": "pin", "designator": "P%d" % i, "name": pin, "owner_designator": "U?",
                     "owner_lib_reference": "MCU", "owner_part_id": 3, "x": 7300 * U, "y": y * U,
                     "orientation": 2, "pin_length": 400 * U, "connect_x": 6900 * U, "connect_y": y * U})
        # a 9th pin keeps the MCU above IC_MIN_PINS
        objs.append({"kind": "wire", "vertices": [{"x": 6900 * U, "y": y * U}, {"x": 3700 * U, "y": y * U}],
                     "x": 6900 * U, "y": y * U})
        objs.append({"kind": "netlabel", "text": "MC_%s_SPI5_%s__MEI" % (pin, sig), "x": 3900 * U, "y": y * U, "orientation": 0})
        objs.append({"kind": "paramset", "name": "MC_SPI5", "x": 6900 * U, "y": y * U, "orientation": 1, "color": 255,
                     "params": [{"name": "ClassName", "text": "MC_SPI5", "hidden": True}]})
    for k in range(6):   # filler pins of the MCU (other part), wired elsewhere; not displayed
        objs.append({"kind": "pin", "designator": "F%d" % k, "name": "PF%d" % k, "owner_designator": "U?",
                     "owner_lib_reference": "MCU", "owner_part_id": 2, "x": 0, "y": 0, "orientation": 0,
                     "pin_length": 0, "connect_x": 1, "connect_y": 1})
    objs.append({"kind": "paramset", "name": "MFG_TP", "x": 6300 * U, "y": 6500 * U, "orientation": 1, "color": 16711680,
                 "params": [{"name": "ClassName", "text": "MFG_TP", "hidden": True}]})
    objs.append({"kind": "harness", "harness_type": "SPI", "x": 3200 * U, "y": 6600 * U, "x_size": 500 * U,
                 "y_size": 400 * U, "primary_connection_position": 200 * U,
                 "entries": [{"name": "SCK", "x": 3700 * U, "y": 6500 * U, "side": 1, "distance_from_top": 100 * U},
                             {"name": "MOSI", "x": 3700 * U, "y": 6400 * U, "side": 1, "distance_from_top": 200 * U},
                             {"name": "MISO", "x": 3700 * U, "y": 6300 * U, "side": 1, "distance_from_top": 300 * U}]})
    objs.append({"kind": "signalharness", "vertices": [{"x": 2200 * U, "y": 6400 * U}, {"x": 3200 * U, "y": 6400 * U}],
                 "x": 2200 * U, "y": 6400 * U})
    objs.append({"kind": "port", "name": "SPI5", "harness_type": "SPI", "x": 1800 * U, "y": 6400 * U, "width": 400 * U,
                 "height": 100 * U, "io_type": 0, "style": 0, "alignment": 0})
    return objs


TYPES = {"SPI": ["SCK", "MOSI", "MISO"]}
GRAMMAR = r"^MC_P[A-K]\d{1,2}_\S+$"


def rules(res, severity=None):
    return sorted({f["rule"] for f in res["findings"] if severity is None or f["severity"] == severity})


class CleanClusterTest(unittest.TestCase):
    def test_complete_cluster_has_no_errors_or_warnings(self):
        res = sheetlint.lint(cluster(), TYPES, GRAMMAR)
        self.assertEqual(rules(res, "error"), [], res["findings"])
        self.assertEqual(rules(res, "warning"), [], res["findings"])
        self.assertEqual(res["summary"]["clusters"], 1)
        self.assertEqual(res["clusters"][0]["classes"], ["MC_SPI5"])
        self.assertEqual(len(res["clusters"][0]["nets"]), 3)
        self.assertIn("unannotated", rules(res, "info"))


class BrokenPiecesTest(unittest.TestCase):
    def broken(self, mutate):
        objs = cluster()
        mutate(objs)
        return sheetlint.lint(objs, TYPES, GRAMMAR)

    def test_wire_one_row_off_leaves_pin_unwired_and_label_floating(self):
        def m(o):
            w = [x for x in o if x["kind"] == "wire"][0]
            for v in w["vertices"]:
                v["y"] += 100 * U
        res = self.broken(m)
        self.assertIn("pin_unwired", rules(res, "error"))
        self.assertIn("label_floating", rules(res, "error"))
        self.assertIn("entry_unwired", rules(res, "error"))

    def test_directive_left_behind(self):
        def m(o):
            ps = [x for x in o if x["kind"] == "paramset" and x["name"] == "MC_SPI5"][0]
            ps["y"] += 100 * U
        res = self.broken(m)
        self.assertIn("directive_floating", rules(res, "error"))

    def test_missing_directive_on_an_interface_net(self):
        def m(o):
            # the MISO row: no MFG pad there, so removing its class mark leaves the net bare
            o.remove([x for x in o if x["kind"] == "paramset" and x["name"] == "MC_SPI5" and x["y"] == 6300 * 10000][0])
        res = self.broken(m)
        self.assertIn("directive_missing", rules(res, "warning"))

    def test_pad_off_wire_and_missing_mfg_mark(self):
        def m(o):
            pad = [x for x in o if x["kind"] == "pin" and x["owner_designator"] == "BOT1"][0]
            pad["connect_y"] += 100 * U
            o.remove([x for x in o if x["kind"] == "paramset" and x["name"] == "MFG_TP"][0])
        res = self.broken(m)
        self.assertIn("pad_floating", rules(res, "error"))

    def test_mfg_pad_without_mark(self):
        def m(o):
            o.remove([x for x in o if x["kind"] == "paramset" and x["name"] == "MFG_TP"][0])
        res = self.broken(m)
        self.assertIn("mfg_mark_missing", rules(res, "warning"))

    def test_port_moved_off_its_harness(self):
        def m(o):
            [x for x in o if x["kind"] == "port"][0]["y"] -= 200 * U
        res = self.broken(m)
        self.assertIn("port_unattached", rules(res, "error"))
        self.assertIn("harness_dangling", rules(res, "error"))

    def test_connector_without_harness(self):
        def m(o):
            o.remove([x for x in o if x["kind"] == "signalharness"][0])
        res = self.broken(m)
        self.assertIn("harness_missing", rules(res, "error"))

    def test_unknown_harness_type_and_entry(self):
        def m(o):
            h = [x for x in o if x["kind"] == "harness"][0]
            h["harness_type"] = "QSPI"
            h["entries"][0]["name"] = "CLK"
        res = self.broken(m)
        self.assertIn("harness_type_unknown", rules(res, "warning"))

    def test_entry_not_in_type(self):
        def m(o):
            [x for x in o if x["kind"] == "harness"][0]["entries"][0]["name"] = "SCLK"
        res = self.broken(m)
        self.assertIn("entry_not_in_type", rules(res, "warning"))

    def test_two_labels_on_one_net_and_bad_grammar(self):
        def m(o):
            o.append({"kind": "netlabel", "text": "spi5 clock", "x": 5000 * U, "y": 6500 * U, "orientation": 0})
        res = self.broken(m)
        self.assertIn("label_conflict", rules(res, "warning"))
        self.assertIn("label_grammar", rules(res, "warning"))

    def test_unnamed_net(self):
        def m(o):
            o.remove([x for x in o if x["kind"] == "netlabel" and "MISO" in x["text"]][0])
        res = self.broken(m)
        self.assertIn("net_unnamed", rules(res, "error"))

    def test_class_conflict_within_cluster(self):
        def m(o):
            ps = [x for x in o if x["kind"] == "paramset" and x["name"] == "MC_SPI5"][0]
            ps["params"][0]["text"] = "MC_SPI4"
        res = self.broken(m)
        self.assertIn("cluster_class_mixed", rules(res, "info"))


class HarnessFileTest(unittest.TestCase):
    def test_parses_type_lines(self):
        t = sheetlint.parse_harness_file("SPI=SCK,MOSI,MISO\nUART_FC=TX,RX,RTS,CTS\n")
        self.assertEqual(t["UART_FC"], ["TX", "RX", "RTS", "CTS"])

    def test_report_is_compact(self):
        res = sheetlint.lint(cluster(), TYPES, GRAMMAR)
        text = sheetlint.format_report(res)
        self.assertTrue(text.startswith("0 error(s), 0 warning(s)"))


if __name__ == "__main__":
    unittest.main()
