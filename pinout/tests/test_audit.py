"""audit: an existing symbol checked against the .ioc that should describe it."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pinout.ioc import IocFile                      # noqa: E402
from pinout.mcu_db import load_mcu_xml              # noqa: E402
from pinout.model import Pinout                     # noqa: E402
from pinout.layout import functional_split          # noqa: E402
from pinout.audit import (audit, infer_clusters, format_report, _poskey, name_relation,  # noqa: E402
                          group_matches, is_port_label, FREE_GROUPS)
from pinout import naming                           # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def pinout():
    return Pinout.from_sources(load_mcu_xml(FIX / "FAKE32C20.xml"),
                               IocFile.load(FIX / "fake_board.ioc"), board="FCNSPLIT")


def fcn_dump():
    return json.loads((FIX / "altium_dump_fcnsplit_house.json").read_text(encoding="utf-8"))


def port_dump():
    return json.loads((FIX / "altium_dump_portsplit_house.json").read_text(encoding="utf-8"))


def sym_pin(dump, number):
    return next(p for p in dump["primitives"] if p["type"] == "pin" and p["pin_number"] == number)


class ClusterInferenceTest(unittest.TestCase):
    def test_every_labelled_cluster_has_exactly_one_label(self):
        clusters = [c for c in infer_clusters(fcn_dump()) if c.pins]
        labelled = [c for c in clusters if c.labels]
        self.assertTrue(labelled)
        self.assertTrue(all(len(c.labels) == 1 for c in labelled), [c.summary() for c in labelled])
        # the fake functional split labels every peripheral it draws
        drawn = {p["text"] for p in fcn_dump()["primitives"] if p["type"] == "label"}
        self.assertEqual({c.label for c in labelled}, drawn)

    def test_no_orphan_labels_on_generated_symbol(self):
        self.assertEqual([c for c in infer_clusters(fcn_dump()) if not c.pins], [])

    def test_clusters_match_the_ioc_groups(self):
        p = pinout()
        by_num = {r.number: r for r in p.pins}
        seen_free = False
        for c in infer_clusters(fcn_dump()):
            if not c.label:
                continue
            groups = {naming.peripheral_group(by_num[str(q["pin_number"])].signal) for q in c.pins}
            if c.label in FREE_GROUPS and c.label != "GPIO":
                seen_free = True
                self.assertEqual(groups, {None}, "UNUSED holds only unassigned pins")
            else:
                self.assertEqual(groups, {c.label}, c.summary())
        self.assertTrue(seen_free, "the fake layout draws an UNUSED cluster")

    def test_region_with_one_label_names_every_cluster_in_it(self):
        # two gap-separated runs on the left, one label, no divider between
        # them -> both are GPIO; a divider between them -> only the first is
        def pin(n, y):
            return {"type": "pin", "pin_number": str(n), "pin_name": f"PX{n}", "electrical": 1,
                    "orientation": 2, "x": 300, "y": y, "length": 300, "owner_part_id": 1}
        prims = [pin(1, 0), pin(2, -100), pin(3, -300), pin(4, -400),
                 {"type": "label", "text": "GPIO", "x": 700, "y": -100, "owner_part_id": 1},
                 {"type": "rectangle", "x1": 300, "y1": -600, "x2": 2400, "y2": 100, "owner_part_id": 1}]
        cs = [c for c in infer_clusters({"primitives": prims}) if c.pins]
        self.assertEqual([(c.label, c.inherited, len(c.pins)) for c in cs],
                         [("GPIO", False, 2), ("GPIO", True, 2)])
        prims.append({"type": "line", "x1": 300, "y1": -200, "x2": 1300, "y2": -200, "owner_part_id": 1})
        cs = [c for c in infer_clusters({"primitives": prims}) if c.pins]
        self.assertEqual([(c.label, c.inherited, c.region) for c in cs],
                         [("GPIO", False, 0), (None, False, 1)])

    def test_portsplit_has_clusters_but_no_labels(self):
        clusters = [c for c in infer_clusters(port_dump()) if c.pins]
        self.assertTrue(clusters)
        self.assertTrue(all(c.label is None for c in clusters))

    def test_position_sort(self):
        self.assertEqual(sorted(["B2", "A10", "A2", "10", "2", "AA1"], key=_poskey),
                         ["2", "10", "A2", "A10", "AA1", "B2"])


class AuditFaithfulTest(unittest.TestCase):
    def test_generated_symbol_matches_its_ioc(self):
        r = audit(pinout(), fcn_dump())
        self.assertTrue(r["match"], format_report(r))
        for key in ("pins_missing_on_symbol", "pins_not_in_mcu", "duplicate_pin_numbers",
                    "name_mismatches", "name_variants", "peripheral_mismatches",
                    "peripheral_pin_under_gpio", "gpio_under_peripheral", "unassigned_but_grouped",
                    "assigned_but_unlabelled", "electrical_mismatches", "orphan_labels"):
            self.assertEqual(r[key], [], key)
        # the oscillator pins live unlabelled with the power pins by convention
        self.assertEqual(sorted(x["name"] for x in r["system_pins"]), ["PF0-OSC_IN", "PF1-OSC_OUT"])
        self.assertEqual(r["summary"]["hard_findings"], 0)
        self.assertEqual(r["summary"]["soft_findings"], 0)
        self.assertEqual(r["summary"]["mcu_pins"], 20)
        self.assertEqual(r["summary"]["symbol_pins"], 20)
        self.assertIn("MATCH", format_report(r))

    def test_portsplit_reports_assigned_pins_as_unlabelled_not_as_errors(self):
        r = audit(pinout(), port_dump())
        self.assertTrue(r["match"], "no labels is not a contradiction")
        self.assertEqual(r["peripheral_mismatches"], [])
        assigned_ports = [p for p in pinout().assigned() if naming.is_port_pin(p.name)
                          and naming.peripheral_group(p.signal) not in ("RCC", "SYS")]
        self.assertEqual(len(r["assigned_but_unlabelled"]), len(assigned_ports))


class AuditDetectsTest(unittest.TestCase):
    def setUp(self):
        self.p = pinout()
        self.d = fcn_dump()

    def test_reassigned_pin_in_ioc_is_a_peripheral_mismatch(self):
        # the .ioc moved PA2 from USART2_TX to SPI1_MOSI; the symbol still
        # draws it under USART2
        pa2 = self.p.pin("PA2")
        self.assertEqual(pa2.signal, "USART2_TX")
        pa2.signal = "SPI1_MOSI"
        r = audit(self.p, self.d)
        self.assertFalse(r["match"])
        self.assertEqual(len(r["peripheral_mismatches"]), 1)
        m = r["peripheral_mismatches"][0]
        # canonical groups: USART2 is reported as UART2 (the two spellings carry no information)
        self.assertEqual((m["name"], m["ioc_group"], m["symbol_group"]), ("PA2", "SPI1", "UART2"))
        self.assertEqual(m["symbol_label"], "USART2")
        self.assertIn("peripheral differs", format_report(r))

    def test_pin_made_gpio_in_ioc_is_soft(self):
        self.p.pin("PA2").signal = "GPIO_Output"
        r = audit(self.p, self.d)
        self.assertTrue(r["match"], "GPIO drawn with its peripheral is a soft finding")
        self.assertEqual([x["name"] for x in r["gpio_under_peripheral"]], ["PA2"])

    def test_dropped_pin_and_renamed_pin(self):
        self.d["primitives"] = [q for q in self.d["primitives"]
                                if not (q["type"] == "pin" and q["pin_number"] == "4")]
        sym_pin(self.d, "5")["pin_name"] = "PB3"
        sym_pin(self.d, "1")["pin_name"] = "vdd"
        r = audit(self.p, self.d)
        self.assertFalse(r["match"])
        self.assertEqual([x["name"] for x in r["pins_missing_on_symbol"]], ["PA2"])
        self.assertEqual(r["name_mismatches"], [{"number": "5", "mcu": "PA3", "symbol": "PB3"}])
        self.assertEqual(r["name_variants"], [{"number": "1", "mcu": "VDD", "symbol": "vdd"}])

    def test_name_relations_measured_on_the_house_symbols(self):
        same = [("PA13", "SYS_JTMS-SWDIO", "PA13 (JTMS/SWDIO)"), ("PC2_C", None, "PC2_C"),
                ("PA13 (JTMS/SWDIO)", "DEBUG_JTMS-SWDIO", "PA13 (JTMS/SWDIO)")]
        variant = [("PB4 (NJTRST)", None, "PB4 (nJTRST)"), ("NRST", None, "nRST"),
                   ("PH0-OSC_IN (PH0)", "RCC_OSC_IN", "PH0-OSC_IN"),
                   ("PC15-OSC32_OUT (OSC32_OUT)", None, "PC15_OSC32_OUT"),
                   ("VDD33_USB", None, "VDD33USB"), ("VCAP", None, "VCAP3"),
                   ("PB3 (JTDO/TRACESWO)", None, "PB3 (JTDO/TRACE/SWO)")]
        different = [("VSS", None, "NC"), ("PA3", None, "PB3"), ("VCAP", None, "VCAPX")]
        for mcu, sig, sym in same:
            self.assertEqual(name_relation(mcu, sig, sym), "same", (mcu, sym))
        for mcu, sig, sym in variant:
            self.assertEqual(name_relation(mcu, sig, sym), "variant", (mcu, sym))
        for mcu, sig, sym in different:
            self.assertEqual(name_relation(mcu, sig, sym), "different", (mcu, sym))

    def test_refining_labels_and_port_labels(self):
        self.assertTrue(group_matches("TIM8", "TIM8_PWM"))
        self.assertTrue(group_matches("ETH", "ETH_RMII"))
        self.assertFalse(group_matches("TIM1", "TIM12"))
        self.assertFalse(group_matches("SPI1", "SPI2"))
        # family without an index on either side
        self.assertTrue(group_matches(naming.peripheral_group("ADCx_INP18"), "ADC1"))
        self.assertTrue(group_matches("SPI1", "SPI"))
        self.assertFalse(group_matches("ADC1", "ADC3"))
        # spellings measured on the 2023 and X9 symbols
        cg = naming.canonical_group
        self.assertEqual(cg("UART6"), cg("USART6"))
        self.assertEqual(cg("USART1"), "UART1")
        self.assertNotEqual(cg("UART5"), cg("USART2"))
        self.assertTrue(group_matches(naming.peripheral_group("USB_OTG_HS_ULPI_D3"), cg("OTG_ULPI")))
        self.assertTrue(group_matches(naming.peripheral_group("USB_OTG_FS_DM"), cg("OTG_FS")))
        self.assertTrue(group_matches(naming.peripheral_group("I2C3_SCL"), cg("I2C3_SMBUS")))
        self.assertEqual(name_relation("NRST", None, "R\\S\\T\\"), "variant")
        for t in ("PORTA", "PORT K", "PA", "GPIOB", "porth"):
            self.assertTrue(is_port_label(t), t)
        for t in ("PWR", "GPIO", "PA0", "PORT", "SPI1", None, ""):
            self.assertFalse(is_port_label(t), t)
        # a port-labelled part carries no peripheral information: relabel the
        # fake USART2 cluster PORTA and its pins move to port_split_pins
        lab = next(q for q in self.d["primitives"] if q["type"] == "label" and q["text"] == "USART2")
        lab["text"] = "PORTA"
        r = audit(self.p, self.d)
        self.assertTrue(r["match"])
        self.assertEqual(sorted(x["name"] for x in r["port_split_pins"]), ["PA2", "PA3"])
        self.assertEqual(r["summary"]["port_split_pins"], 2)

    def test_peripheral_under_unused_is_hard(self):
        lab = next(q for q in self.d["primitives"] if q["type"] == "label" and q["text"] == "SPI1")
        lab["text"] = "UNUSED"
        r = audit(self.p, self.d)
        self.assertFalse(r["match"])
        self.assertEqual(sorted(x["name"] for x in r["peripheral_pin_under_gpio"]), ["PA5", "PA6", "PA7"])

    def test_extra_and_duplicate_pins(self):
        ghost = copy.deepcopy(sym_pin(self.d, "4"))
        ghost["pin_number"] = "99"
        dup = copy.deepcopy(sym_pin(self.d, "6"))
        self.d["primitives"] += [ghost, dup]
        r = audit(self.p, self.d)
        self.assertEqual([x["number"] for x in r["pins_not_in_mcu"]], ["99"])
        self.assertEqual(r["duplicate_pin_numbers"], ["6"])

    def test_alias_labels_compare_equal(self):
        # a hand-drawn symbol says QSPI / OTGFS / JTAG where CubeMX says
        # QUADSPI / USB_OTG_FS / DEBUG_*
        self.assertEqual(naming.canonical_group("QSPI"), naming.canonical_group("QUADSPI"))
        self.assertEqual(naming.canonical_group("OTGFS"), naming.peripheral_group("USB_OTG_FS_DM"))
        self.assertEqual(naming.canonical_group("JTAG"), naming.peripheral_group("DEBUG_JTMS-SWDIO"))
        self.assertEqual(naming.peripheral_group("ADCx_INP18"), "ADC")
        self.assertEqual(naming.peripheral_group("USB_OTG_HS_ULPI_D0"), "USB_OTG_HS")
        self.assertEqual(naming.peripheral_group("DEBUG_TRACED0"), "TRACE")
        self.assertEqual(naming.peripheral_group("S_TIM5_CH1"), "TIM5")
        self.assertEqual(naming.peripheral_group("FMC_D0_DA0"), "FMC")
        # and a relabelled cluster is caught
        lab = next(q for q in self.d["primitives"] if q["type"] == "label" and q["text"] == "USART2")
        lab["text"] = "UART2"
        self.assertTrue(audit(self.p, self.d)["match"], "UART2 and USART2 are the same peripheral")
        lab["text"] = "UART3"
        r = audit(self.p, self.d)
        self.assertEqual(len(r["peripheral_mismatches"]), 2, "PA2 and PA3 now sit under the wrong UART")
        self.assertTrue(all(x["symbol_group"] == "UART3" for x in r["peripheral_mismatches"]))

    def test_h7_pin_names_resolve_to_ports(self):
        self.assertEqual(naming.base_pin("PA13 (JTMS/SWDIO)"), "PA13")
        self.assertEqual(naming.port_of("PA13 (JTMS/SWDIO)"), "PA")
        self.assertEqual(naming.base_pin("PC2_C"), "PC2_C", "a separate ball on the H7, not a spelling of PC2")
        self.assertEqual(naming.port_of("PC2_C"), "PC")
        self.assertEqual(naming.base_pin("PA0_C"), "PA0_C")
        self.assertEqual(naming.base_pin("PH0-OSC_IN (PH0)"), "PH0")
        self.assertTrue(naming.is_port_pin("PC14-OSC32_IN (OSC32_IN)"))
        self.assertFalse(naming.is_port_pin("VDD"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
