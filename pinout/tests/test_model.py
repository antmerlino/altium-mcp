"""Pinout: MCU DB + .ioc -> canonical model, and back, and through JSON/XLSX."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pinout.ioc import IocFile          # noqa: E402
from pinout.mcu_db import load_mcu_xml  # noqa: E402
from pinout.model import Pinout         # noqa: E402
from pinout import naming               # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def build():
    return Pinout.from_sources(load_mcu_xml(FIX / "FAKE32C20.xml"),
                               IocFile.load(FIX / "fake_board.ioc"),
                               board="TESTBOARD", prefix="TB")


class McuDbTest(unittest.TestCase):
    def test_parses_namespaced_schema(self):
        mcu = load_mcu_xml(FIX / "FAKE32C20.xml")
        self.assertEqual((mcu.ref_name, mcu.package, len(mcu.pins)), ("FAKE32C20Ux", "UFQFPN20", 20))
        pa2 = mcu.pin("PA2")
        self.assertEqual(pa2.position, "4")
        self.assertIn("USART2_TX", pa2.signals)
        self.assertTrue(pa2.is_gpio)
        self.assertFalse(mcu.pin("VDD").is_gpio)


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.p = build()

    def test_every_physical_pin_present_once_in_position_order(self):
        self.assertEqual(len(self.p.pins), 20)
        self.assertEqual([x.number for x in self.p.pins], [str(i) for i in range(1, 21)])

    def test_assignments_merged_from_ioc(self):
        self.assertEqual(len(self.p.assigned()), 14)
        pa2 = self.p.pin("PA2")
        self.assertEqual((pa2.signal, pa2.mode, pa2.label, pa2.locked),
                         ("USART2_TX", "Asynchronous", "DBG_TX", True))
        self.assertIn("ADC1_IN3", pa2.signals, "MCU-DB alternatives kept alongside the choice")
        self.assertIsNone(self.p.pin("PB1").signal)

    def test_ports_and_groups_derived(self):
        self.assertEqual(self.p.pin("PB6").port, "PB")
        self.assertIsNone(self.p.pin("VDD").port)
        self.assertEqual(self.p.pin("PB6").effective_group, "I2C1")
        self.assertEqual(self.p.pin("PA13").effective_group, "SWD")
        self.assertEqual(self.p.pin("PA4").effective_group, "GPIO")
        self.assertEqual(self.p.pin("PF0-OSC_IN").effective_group, "RCC")

    def test_designer_override_wins(self):
        pin = self.p.pin("PA4")
        pin.group, pin.function = "SPI1", "CS_IMU"
        self.assertEqual(pin.effective_group, "SPI1")
        self.assertEqual(pin.effective_function, "CS_IMU")
        self.assertEqual(self.p.pin("PB0").effective_function, "LED_STATUS", "falls back to label")


class JsonRoundTripTest(unittest.TestCase):
    def test_identical_after_json(self):
        p = build()
        p.pin("PA4").function = "CS_IMU"
        p.pin("PA4").part = 2
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "pinout.json"
            p.to_json(f)
            back = Pinout.from_json(f)
            json.loads(f.read_text())  # valid JSON
        self.assertEqual(back.to_dict(), p.to_dict())
        self.assertEqual(back.pin("PA4").part, 2)


class MergeOverridesTest(unittest.TestCase):
    def test_designer_columns_survive_regeneration(self):
        old = build()
        old.pin("PA4").function = "CS_IMU"
        old.pin("PB0").net = "LED_STATUS_N"
        old.pin("PA2").part = 1
        # regenerate from a CHANGED .ioc: PA4 becomes SPI1_NSS, PB1 gets used
        ioc = IocFile.load(FIX / "fake_board.ioc")
        ioc.set_pin("PA4", signal="SPI1_NSS", mode="NSS_Signal_Hard_Output")
        ioc.set_pin("PB1", signal="TIM3_CH4")
        new = Pinout.from_sources(load_mcu_xml(FIX / "FAKE32C20.xml"), ioc)
        touched = new.merge_overrides(old)
        self.assertEqual(touched, 3)
        self.assertEqual(new.pin("PA4").signal, "SPI1_NSS", "source wins on signal")
        self.assertEqual(new.pin("PA4").function, "CS_IMU", "designer wins on function")
        self.assertEqual(new.pin("PB0").net, "LED_STATUS_N")
        self.assertEqual(new.pin("PB1").signal, "TIM3_CH4")


class ApplyToIocTest(unittest.TestCase):
    def test_no_change_is_a_no_op(self):
        p = build()
        ioc = IocFile.load(FIX / "fake_board.ioc")
        before = ioc.text()
        self.assertEqual(p.apply_to_ioc(ioc), 0)
        self.assertEqual(ioc.text(), before)

    def test_model_edits_flow_back_into_cubemx(self):
        p = build()
        p.pin("PB1").signal, p.pin("PB1").mode, p.pin("PB1").label = "TIM3_CH4", "PWM Generation4 CH4", "FAN"
        p.pin("PA4").signal = None                       # unassign
        p.pin("PA0").label = "VBAT_DIV"                  # rename a label
        ioc = IocFile.load(FIX / "fake_board.ioc")
        self.assertEqual(p.apply_to_ioc(ioc), 3)
        pins = ioc.pins()
        self.assertEqual((pins["PB1"].signal, pins["PB1"].label), ("TIM3_CH4", "FAN"))
        self.assertNotIn("PA4", pins)
        self.assertEqual(pins["PA0"].label, "VBAT_DIV")
        self.assertEqual(ioc.get("Mcu.PinsNb"), "15")    # +PB1 -PA4
        self.assertEqual(ioc.get("USART2.BaudRate"), "115200", "unrelated content intact")

    def test_round_trip_ioc_to_model_to_ioc_is_stable(self):
        ioc = IocFile.load(FIX / "fake_board.ioc")
        p = Pinout.from_sources(load_mcu_xml(FIX / "FAKE32C20.xml"), ioc)
        p.apply_to_ioc(ioc)
        self.assertEqual(ioc.text(), (FIX / "fake_board.ioc").read_text(encoding="utf-8"))


class NamingTest(unittest.TestCase):
    def test_net_labels_follow_house_convention(self):
        self.assertEqual(naming.net_label("ESC", "PA13", "SYS_JTMS-SWDIO"), "ESC_PA13_SYS_JTMS-SWDIO")
        self.assertEqual(naming.net_label("ESC", "PC13", "TIM1_CH1N", "PWM_UL"),
                         "ESC_PC13_TIM1_CH1N__PWM_UL")
        self.assertEqual(naming.net_label("ESC", "PF0-OSC_IN", "RCC_OSC_IN"), "ESC_PF0_RCC_OSC_IN")

    def test_display_names(self):
        self.assertEqual(naming.display_name("PB8-BOOT0"), "PB8-BOOT0")
        self.assertEqual(naming.display_name("PA13", "SYS_JTMS-SWDIO", "debug"), "PA13 (JTMS/SWDIO)")
        self.assertEqual(naming.display_name("PA13", "SYS_JTMS-SWDIO", "plain"), "PA13")

    def test_symbol_and_description(self):
        self.assertEqual(naming.symbol_name("STM32G431C8U6TR"), "EMBEDDED_IC_STM32G431C8U6TR")
        self.assertEqual(naming.symbol_name("STM32H753IIK6", "X57"), "EMBEDDED_IC_STM32H753IIK6_X57")
        self.assertEqual(naming.description(32, "128KB", "48UFQFPN"),
                         "EMBEDDED IC MCU 32BIT 128KB FLASH 48UFQFPN")

    def test_electrical_types(self):
        et = naming.electrical_type
        self.assertEqual(et("VDD", "Power", None), "eElectricPower")
        self.assertEqual(et("VSS", "Power", None), "eElectricPassive")
        self.assertEqual(et("NRST", "Reset", None), "eElectricInput")
        self.assertEqual(et("PA2", "I/O", "USART2_TX"), "eElectricOutput")
        self.assertEqual(et("PA3", "I/O", "USART2_RX"), "eElectricInput")
        self.assertEqual(et("PB6", "I/O", "I2C1_SCL"), "eElectricIO")
        self.assertEqual(et("PA0", "I/O", "ADC1_IN1"), "eElectricInput")
        self.assertEqual(et("PB0", "I/O", "GPIO_Output"), "eElectricOutput")
        self.assertEqual(et("PF0-OSC_IN", "I/O", "RCC_OSC_IN"), "eElectricIO")
        self.assertEqual(et("PB1", "I/O", None), "eElectricIO")


@unittest.skipUnless(__import__("importlib").util.find_spec("openpyxl"), "openpyxl not installed")
class XlsxRoundTripTest(unittest.TestCase):
    def test_spreadsheet_round_trip_keeps_designer_columns(self):
        p = build()
        p.pin("PA4").function, p.pin("PA4").part = "CS_IMU", 2
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "pinout.xlsx"
            p.to_xlsx(f)
            back = Pinout.from_xlsx(f)
        self.assertEqual(back.to_dict(), p.to_dict())


if __name__ == "__main__":
    unittest.main(verbosity=2)
