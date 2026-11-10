"""IocFile: read, modify and round-trip a CubeMX .ioc without collateral damage."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pinout.ioc import (EXTI_NVIC_ENABLED, IocFile, exti_line, exti_vector,  # noqa: E402
                        is_physical_pin, is_virtual_pin)

FIX = Path(__file__).parent / "fixtures" / "fake_board.ioc"


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.ioc = IocFile.load(FIX)

    def test_header_fields(self):
        self.assertEqual(self.ioc.mcu_name, "FAKE32C20Ux")
        self.assertEqual(self.ioc.package, "UFQFPN20")

    def test_physical_pins_only_by_default(self):
        pins = self.ioc.pins()
        self.assertEqual(len(pins), 14)
        self.assertNotIn("VP_SYS_VS_Systick", pins)

    def test_virtual_pins_on_request(self):
        pins = self.ioc.pins(include_virtual=True)
        self.assertEqual(len(pins), 15)
        self.assertEqual(pins["VP_SYS_VS_Systick"].signal, "SYS_VS_Systick")

    def test_assignment_fields(self):
        pa2 = self.ioc.pins()["PA2"]
        self.assertEqual((pa2.signal, pa2.mode, pa2.label, pa2.locked),
                         ("USART2_TX", "Asynchronous", "DBG_TX", True))
        pa0 = self.ioc.pins()["PA0"]
        self.assertEqual((pa0.signal, pa0.label, pa0.locked), ("ADC1_IN1", "VBAT_SENSE", False))

    def test_hyphenated_pin_names(self):
        self.assertEqual(self.ioc.pins()["PF0-OSC_IN"].signal, "RCC_OSC_IN")

    def test_pin_name_classification(self):
        self.assertTrue(is_physical_pin("PA0") and is_physical_pin("PG10-nRST"))
        self.assertTrue(is_virtual_pin("VP_SYS_VS_Systick"))
        self.assertFalse(is_physical_pin("Mcu") or is_physical_pin("VP_X"))


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.original = FIX.read_text(encoding="utf-8")
        self.ioc = IocFile(self.original)

    def test_untouched_file_is_byte_identical(self):
        self.assertEqual(self.ioc.text(), self.original)

    def test_reassigning_identical_values_changes_nothing(self):
        # exercises the Mcu.PinN rebuild: it must land where CubeMX put it
        self.ioc.set_pin("PA2", signal="USART2_TX", mode="Asynchronous",
                         label="DBG_TX", locked=True)
        self.assertEqual(self.ioc.text(), self.original)

    def test_crlf_preserved(self):
        crlf = IocFile(self.original.replace("\n", "\r\n"))
        crlf.set_pin("PB1", signal="TIM3_CH4", mode="PWM Generation4 CH4")
        text = crlf.text()
        self.assertIn("\r\n", text)
        # every newline must still be a CRLF - no bare LF crept in with the edit
        self.assertNotIn("\n", text.replace("\r\n", ""))
        self.assertIn("PB1.Signal=TIM3_CH4\r\n", text)


class ModifyTest(unittest.TestCase):
    def setUp(self):
        self.ioc = IocFile.load(FIX)

    def test_assign_new_pin_updates_pin_list_and_count(self):
        self.ioc.set_pin("PB1", signal="TIM3_CH4", mode="PWM Generation4 CH4",
                         label="FAN_PWM")
        self.assertEqual(self.ioc.get("PB1.Signal"), "TIM3_CH4")
        self.assertEqual(self.ioc.get("PB1.GPIO_Label"), "FAN_PWM")
        self.assertEqual(self.ioc.get("PB1.GPIOParameters"), "GPIO_Label")
        self.assertEqual(self.ioc.get("Mcu.PinsNb"), "16")
        # by INDEX, not file order - CubeMX string-sorts keys so Mcu.Pin9 is
        # the last one in the file even though Mcu.Pin15 is the last entry
        indexed = sorted(((int(k[len("Mcu.Pin"):]), self.ioc.get(k))
                          for k in self.ioc.keys("Mcu.Pin") if k[len("Mcu.Pin"):].isdigit()))
        listed = [v for _, v in indexed]
        self.assertEqual(len(listed), 16)
        self.assertIn("PB1", listed)
        self.assertEqual(listed[-1], "VP_SYS_VS_Systick", "virtual pins stay last")
        self.assertEqual(listed[:2], ["PA0", "PA13"], "existing order is preserved")
        self.assertEqual(listed[-2], "PB1", "a new physical pin is appended before the virtual ones; "
                                             "CubeMX re-sorts by ball position on its next save")

    def test_new_keys_are_inserted_sorted(self):
        self.ioc.set_pin("PA1", signal="TIM2_CH2")
        keys = self.ioc.keys("PA")
        self.assertLess(keys.index("PA0.Signal"), keys.index("PA1.Signal"))
        self.assertLess(keys.index("PA1.Signal"), keys.index("PA13.Mode"))

    def test_clear_pin_removes_everything_and_recounts(self):
        self.ioc.clear_pin("PA2")
        self.assertEqual([k for k in self.ioc.keys("PA2.")], [])
        self.assertEqual(self.ioc.get("Mcu.PinsNb"), "14")
        self.assertNotIn("PA2", [self.ioc.get(k) for k in self.ioc.keys("Mcu.Pin")])

    def test_clearing_label_drops_gpio_parameters_when_last(self):
        self.ioc.set_pin("PA0", label="")
        self.assertIsNone(self.ioc.get("PA0.GPIO_Label"))
        self.assertIsNone(self.ioc.get("PA0.GPIOParameters"))
        self.assertEqual(self.ioc.get("PA0.Signal"), "ADC1_IN1", "other fields untouched")

    def test_label_added_to_existing_gpio_parameters(self):
        self.ioc.set("PA5.GPIOParameters", "GPIO_Speed")
        self.ioc.set_pin("PA5", label="SCK")
        self.assertEqual(self.ioc.get("PA5.GPIOParameters"), "GPIO_Speed,GPIO_Label")

    def test_non_pin_content_survives_edits(self):
        before = {k: self.ioc.get(k) for k in ("RCC.HSE_VALUE", "USART2.BaudRate",
                                               "ProjectManager.ProjectName", "MxCube.Version")}
        self.ioc.set_pin("PB1", signal="ADC1_IN9", mode="IN9")
        self.ioc.clear_pin("PA4")
        after = {k: self.ioc.get(k) for k in before}
        self.assertEqual(before, after)
        self.assertTrue(self.ioc.text().startswith("#MicroXplorer"))

    def test_invalid_pin_name_rejected(self):
        with self.assertRaises(ValueError):
            self.ioc.set_pin("Mcu", signal="X")


H7_SNIPPET = "\n".join([
    "#MicroXplorer Configuration settings - do not modify",
    "Mcu.Name=STM32H753IIKx",
    "Mcu.Package=UFBGA176",
    "Mcu.Pin0=PE3",
    "Mcu.Pin1=PA13 (JTMS/SWDIO)",
    "Mcu.Pin2=PH0-OSC_IN (PH0)",
    "Mcu.Pin3=PC2_C",
    "Mcu.Pin4=VP_SYS_VS_Systick",
    "Mcu.PinsNb=5",
    "PA13\\ (JTMS/SWDIO).Mode=Serial_Wire",
    "PA13\\ (JTMS/SWDIO).Signal=DEBUG_JTMS-SWDIO",
    "PC2_C.GPIOParameters=GPIO_Label",
    "PC2_C.GPIO_Label=OBS1_3V3_SENSE",
    "PC2_C.Signal=ADCx_INP12",
    "PE3.Signal=GPIO_Output",
    "PH0-OSC_IN\\ (PH0).Mode=HSE-External-Oscillator",
    "PH0-OSC_IN\\ (PH0).Signal=RCC_OSC_IN",
    "SPI1.Mode=Full_Duplex_Master",
    "VP_SYS_VS_Systick.Mode=SysTick",
    "VP_SYS_VS_Systick.Signal=SYS_VS_Systick",
    "",
])


class RealWorldKeyFormsTest(unittest.TestCase):
    """The spellings CubeMX actually writes for H7 parts, none of which the
    first parser saw: a space inside a key is written as backslash-space
    (Java .properties), direct-ADC pads are PC2_C, oscillator pins carry a
    parenthesised annotation. Silently dropping these pins is the worst
    failure mode, so every form is pinned down here."""

    def setUp(self):
        self.ioc = IocFile(H7_SNIPPET)

    def test_escaped_keys_are_read_unescaped(self):
        pins = self.ioc.pins()
        self.assertEqual(sorted(pins), ["PA13 (JTMS/SWDIO)", "PC2_C", "PE3", "PH0-OSC_IN (PH0)"])
        self.assertEqual(pins["PA13 (JTMS/SWDIO)"].signal, "DEBUG_JTMS-SWDIO")
        self.assertEqual(pins["PA13 (JTMS/SWDIO)"].mode, "Serial_Wire")
        self.assertEqual(pins["PH0-OSC_IN (PH0)"].signal, "RCC_OSC_IN")
        self.assertEqual(pins["PC2_C"].label, "OBS1_3V3_SENSE")
        self.assertEqual(self.ioc.get("PA13 (JTMS/SWDIO).Signal"), "DEBUG_JTMS-SWDIO")

    def test_peripheral_mode_keys_are_not_pins(self):
        self.assertNotIn("SPI1", self.ioc.pins(include_virtual=True))

    def test_pin_name_forms(self):
        for name in ("PA13 (JTMS/SWDIO)", "PH0-OSC_IN (PH0)", "PC14-OSC32_IN (OSC32_IN)",
                     "PC2_C", "PA0_C", "PB4 (NJTRST)", "PF0-OSC_IN", "PG10-nRST", "PB8-BOOT0"):
            self.assertTrue(is_physical_pin(name), name)
        for name in ("PA13 (JTMS/SWDIO", "PA13(JTMS)", "SPI1", "VP_SYS_VS_Systick", "PA 13"):
            self.assertFalse(is_physical_pin(name), name)

    def test_writing_an_escaped_key_reproduces_cubemx_spelling(self):
        self.ioc.set_pin("PA13 (JTMS/SWDIO)", mode="JTAG_5_pins")
        self.assertIn("PA13\\ (JTMS/SWDIO).Mode=JTAG_5_pins", self.ioc.text().split("\n"))
        self.ioc.set_pin("PC14-OSC32_IN (OSC32_IN)", signal="RCC_OSC32_IN")
        lines = self.ioc.text().split("\n")
        self.assertIn("PC14-OSC32_IN\\ (OSC32_IN).Signal=RCC_OSC32_IN", lines)
        # inserted sorted among the other pin keys, escaped form on disk
        self.assertLess(lines.index("PC14-OSC32_IN\\ (OSC32_IN).Signal=RCC_OSC32_IN"),
                        lines.index("PC2_C.GPIOParameters=GPIO_Label"))

    def test_rewriting_same_values_is_byte_identical(self):
        self.ioc.set_pin("PA13 (JTMS/SWDIO)", signal="DEBUG_JTMS-SWDIO", mode="Serial_Wire")
        self.ioc.set_pin("PH0-OSC_IN (PH0)", signal="RCC_OSC_IN")
        self.assertEqual(self.ioc.text(), H7_SNIPPET)

    def test_pin_list_keeps_cubemx_position_order(self):
        # PE3 before PA13: CubeMX orders by ball position, not alphabetically
        self.assertEqual(self.ioc.pin_list(),
                         ["PE3", "PA13 (JTMS/SWDIO)", "PH0-OSC_IN (PH0)", "PC2_C", "VP_SYS_VS_Systick"])
        self.ioc.set_pin("PB0", signal="TIM3_CH3")
        self.assertEqual(self.ioc.pin_list(),
                         ["PE3", "PA13 (JTMS/SWDIO)", "PH0-OSC_IN (PH0)", "PC2_C", "PB0",
                          "VP_SYS_VS_Systick"])
        self.assertEqual(self.ioc.get("Mcu.PinsNb"), "6")
        self.ioc.clear_pin("PH0-OSC_IN (PH0)")
        self.assertEqual(self.ioc.pin_list(),
                         ["PE3", "PA13 (JTMS/SWDIO)", "PC2_C", "PB0", "VP_SYS_VS_Systick"])
        self.assertNotIn("PH0-OSC_IN\\ (PH0).Signal=RCC_OSC_IN", self.ioc.text().split("\n"))


class LoadFromDiskTest(unittest.TestCase):
    """test_crlf_preserved builds the file from a string; this goes through
    load(), where text-mode reading once turned every CRLF into LF."""

    def test_crlf_file_round_trips_byte_identical(self):
        data = FIX.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp, "in.ioc"), Path(tmp, "out.ioc")
            src.write_bytes(data)
            IocFile.load(src).save(dst)
            self.assertEqual(dst.read_bytes(), data)


EXTI_SNIPPET = "\r\n".join([
    "#MicroXplorer Configuration settings - do not modify",
    "Mcu.Family=STM32H7",
    "Mcu.Name=STM32H753XIHx",
    "Mcu.Pin0=PC7",
    "Mcu.Pin1=PG7",
    "Mcu.Pin2=PI10",
    "Mcu.Pin3=VP_SYS_VS_Systick",
    "Mcu.PinsNb=4",
    "NVIC.SysTick_IRQn=true\\:0\\:0\\:false\\:false\\:true\\:false\\:true\\:false",
    "PC7.GPIOParameters=GPIO_Label",
    "PC7.GPIO_Label=WIFI_IRQ",
    "PC7.Locked=true",
    "PC7.Signal=GPIO_Input",
    "PG7.Signal=GPIO_Input",
    "PI10.Signal=GPIO_Input",
    "VP_SYS_VS_Systick.Mode=SysTick",
    "VP_SYS_VS_Systick.Signal=SYS_VS_Systick",
    "",
])


class ExtiTest(unittest.TestCase):
    """EXTI pins need the SH.GPXTIn binding CubeMX writes. Without it CubeMX
    forced the GPIO mode on load and dropped every NVIC vector on save - the
    X9 Mission Computer .ioc, 2026-09-22."""

    def setUp(self):
        self.ioc = IocFile(EXTI_SNIPPET)

    def test_vector_layouts(self):
        self.assertEqual([exti_vector(n, "STM32H7") for n in (0, 4, 5, 9, 10, 15)],
                         ["EXTI0_IRQn", "EXTI4_IRQn", "EXTI9_5_IRQn", "EXTI9_5_IRQn",
                          "EXTI15_10_IRQn", "EXTI15_10_IRQn"])
        self.assertEqual([exti_vector(n, "STM32L0") for n in (1, 2, 4, 15)],
                         ["EXTI0_1_IRQn", "EXTI2_3_IRQn", "EXTI4_15_IRQn", "EXTI4_15_IRQn"])
        self.assertEqual(exti_vector(13, "STM32U5"), "EXTI13_IRQn")
        with self.assertRaises(ValueError):
            exti_vector(3, "STM32MP1")      # unverified layout: never guessed

    def test_line_from_pin_name(self):
        self.assertEqual(exti_line("PC7"), 7)
        self.assertEqual(exti_line("PC14-OSC32_IN (OSC32_IN)"), 14)
        self.assertEqual(exti_line("PA13 (JTMS/SWDIO)"), 13)

    def test_set_exti_writes_what_cubemx_writes(self):
        self.ioc.set_exti("PC7", edge="falling")
        self.assertEqual(self.ioc.get("PC7.Signal"), "GPXTI7")
        self.assertEqual(self.ioc.get("PC7.GPIO_ModeDefaultEXTI"), "GPIO_MODE_IT_FALLING")
        self.assertEqual(self.ioc.get("PC7.GPIOParameters"), "GPIO_Label,GPIO_ModeDefaultEXTI")
        self.assertEqual(self.ioc.get("PC7.GPIO_Label"), "WIFI_IRQ", "label kept")
        self.assertEqual(self.ioc.get("SH.GPXTI7.0"), "GPIO_EXTI7")
        self.assertEqual(self.ioc.get("SH.GPXTI7.ConfNb"), "1")
        self.assertEqual(self.ioc.get("NVIC.EXTI9_5_IRQn"), EXTI_NVIC_ENABLED)
        self.assertEqual(self.ioc.check(), [])
        self.assertTrue(self.ioc.text().endswith("\r\n") and "\n" not in self.ioc.text().replace("\r\n", ""))

    def test_existing_vector_priority_is_kept(self):
        self.ioc.set("NVIC.EXTI15_10_IRQn", "true\\:5\\:0\\:false\\:false\\:true\\:true\\:true\\:true")
        self.ioc.set_exti("PI10")
        self.assertTrue(self.ioc.get("NVIC.EXTI15_10_IRQn").startswith("true\\:5"))

    def test_one_port_per_line(self):
        self.ioc.set_exti("PC7")
        with self.assertRaisesRegex(ValueError, "already used by PC7"):
            self.ioc.set_exti("PG7")
        self.assertEqual(self.ioc.get("PG7.Signal"), "GPIO_Input", "refused before touching anything")

    def test_unknown_family_refuses_before_writing(self):
        ioc = IocFile(EXTI_SNIPPET.replace("Mcu.Family=STM32H7", "Mcu.Family=STM32MP1"))
        with self.assertRaises(ValueError):
            ioc.set_exti("PC7")
        self.assertEqual(ioc.get("PC7.Signal"), "GPIO_Input")
        ioc.set_exti("PC7", vector="EXTI7_IRQn")
        self.assertEqual(ioc.get("NVIC.EXTI7_IRQn"), EXTI_NVIC_ENABLED)

    def test_back_to_gpio_releases_binding_and_last_vector_user(self):
        self.ioc.set_exti("PC7")
        self.ioc.set_pin("PC7", signal="GPIO_Input")
        self.assertIsNone(self.ioc.get("PC7.GPIO_ModeDefaultEXTI"))
        self.assertEqual(self.ioc.get("PC7.GPIOParameters"), "GPIO_Label")
        self.assertEqual(self.ioc.keys("SH.GPXTI7."), [])
        self.assertIsNone(self.ioc.get("NVIC.EXTI9_5_IRQn"))
        self.assertEqual(self.ioc.check(), [])

    def test_shared_vector_survives_while_another_pin_uses_it(self):
        self.ioc.set_exti("PC7")                    # line 7 -> EXTI9_5
        self.ioc.set_exti("PE8", label="UWB_IRQ")   # line 8 -> EXTI9_5 too; new pin
        self.assertIn("PE8", self.ioc.pin_list())
        self.ioc.clear_pin("PC7")
        self.assertEqual(self.ioc.get("NVIC.EXTI9_5_IRQn"), EXTI_NVIC_ENABLED, "PE8 still uses it")
        self.assertEqual(self.ioc.keys("SH.GPXTI7."), [])
        self.assertEqual(self.ioc.get("SH.GPXTI8.ConfNb"), "1")
        self.ioc.set_pin("PE8", signal="GPIO_Input")
        self.assertIsNone(self.ioc.get("NVIC.EXTI9_5_IRQn"), "last user of the vector gone")
        self.assertEqual(self.ioc.check(), [])

    def test_check_flags_the_x9_failure(self):
        # exactly what was handed to CubeMX: a GPXTI signal and a vector, no binding
        self.ioc.set_pin("PC7", signal="GPXTI7")
        self.ioc.set("NVIC.EXTI9_5_IRQn", EXTI_NVIC_ENABLED)
        problems = self.ioc.check()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("PC7 is GPXTI7 without SH.GPXTI7.0", problems[0])

    def test_check_flags_orphan_vector_and_pin_list(self):
        self.ioc.set("NVIC.EXTI2_IRQn", EXTI_NVIC_ENABLED)
        self.ioc.set("Mcu.PinsNb", "9")
        problems = self.ioc.check()
        self.assertTrue(any("NVIC.EXTI2_IRQn is set but no EXTI pin uses it" in p for p in problems))
        self.assertTrue(any("Mcu.PinsNb=9" in p for p in problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
