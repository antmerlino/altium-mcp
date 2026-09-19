"""netnames: the label convention measured on the X2 Control Board."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pinout import netnames as nn  # noqa: E402


class SpellingTest(unittest.TestCase):
    def test_spellings(self):
        self.assertEqual(nn.signal_spellings("USB_OTG_HS_ULPI_D0"), ["USB_OTG_HS_ULPI_D0", "OTGHS_ULPI_D0"])
        self.assertEqual(nn.signal_spellings("S_TIM5_CH1"), ["S_TIM5_CH1", "TIM5_CH1"])
        self.assertIn("SWDIO", nn.signal_spellings("DEBUG_JTMS-SWDIO"))
        self.assertEqual(nn.signal_spellings("FMC_D1_DA1"), ["FMC_D1_DA1", "FMC_D1"])
        self.assertEqual(nn.signal_spellings("RCC_OSC_IN"), ["RCC_OSC_IN", "OSC_IN"])
        self.assertEqual(nn.signal_spellings("S_TIM2_CH1_ETR"), ["S_TIM2_CH1_ETR", "TIM2_CH1_ETR", "TIM2_CH1"])
        self.assertEqual(nn.signal_spellings("FMC_A15_BA1"), ["FMC_A15_BA1", "FMC_BA1"])
        self.assertIn("ADC1_INP18", nn.signal_spellings("ADCx_INP18"))
        self.assertEqual(nn.split_gpio_label("SPI3_nCS2__SE1"), ("SPI3_nCS2", "SE1"))
        self.assertEqual(nn.split_gpio_label("SPI5_nCS1"), ("SPI5_nCS1", None))
        self.assertEqual(nn.split_gpio_label("DBG_IO1"), (None, "DBG_IO1"))
        self.assertEqual(nn.split_gpio_label("nLED_RED"), (None, "nLED_RED"))
        self.assertEqual(nn.generate("MC", "PK6", "GPIO_Output", "BB", role="SPI5_nCS1"), "MC_PK6_SPI5_nCS1__BB")
        self.assertEqual(nn.signal_spellings(None), [])
        self.assertEqual(nn.preferred_spelling("USART2_RX"), "USART2_RX")
        self.assertEqual(nn.preferred_spelling("S_TIM8_CH2"), "TIM8_CH2")
        self.assertEqual(nn.unabbreviate("OTGHS_ULPI_D0"), "USB_OTG_HS_ULPI_D0")
        self.assertEqual(nn.unabbreviate("SWCLK"), "DEBUG_JTCK-SWCLK")
        self.assertEqual(nn.unabbreviate("USART2_RX"), "USART2_RX")


class ParseGenerateTest(unittest.TestCase):
    def test_parse_forms(self):
        p = nn.parse("FC_PA3_USART2_RX__BB_ESC_TX", "FC")
        self.assertEqual((p.form, p.pin, p.signal, p.function), ("peripheral", "PA3", "USART2_RX", "BB_ESC_TX"))
        p = nn.parse("FC_PC0__DBG_IO1", "FC")
        self.assertEqual((p.form, p.pin, p.signal, p.function), ("gpio", "PC0", None, "DBG_IO1"))
        self.assertEqual(nn.parse("FC_PH13", "FC").form, "spare")
        self.assertEqual(nn.parse("MC_PA0_C", "MC").pin, "PA0_C")
        self.assertEqual(nn.parse("FC_OTGFS_N", "FC").form, "pinless")
        self.assertEqual(nn.parse("SE3_PPS_INPUT", "FC").form, "other")
        self.assertEqual(nn.parse("MC_PB6_USART1_TX__MEI", "FC").form, "other", "wrong prefix")
        self.assertEqual(nn.parse("MC_PA3_OTGHS_ULPI_D0", "MC").cubemx_signal, "USB_OTG_HS_ULPI_D0")
        self.assertEqual(nn.parse("MC_PE3_SPI4_nCS1__MEI").signal, "SPI4_nCS1")

    def test_generate(self):
        self.assertEqual(nn.generate("FC", "PA3", "USART2_RX", "BB_ESC_TX"), "FC_PA3_USART2_RX__BB_ESC_TX")
        self.assertEqual(nn.generate("FC", "PC0", "GPIO_Output", "DBG_IO1"), "FC_PC0__DBG_IO1")
        self.assertEqual(nn.generate("FC", "PH13", None), "FC_PH13")
        self.assertEqual(nn.generate("MC", "PA3", "USB_OTG_HS_ULPI_D0"), "MC_PA3_OTGHS_ULPI_D0")
        self.assertEqual(nn.generate("MC", "PA13 (JTMS/SWDIO)", "DEBUG_JTMS-SWDIO"), "MC_PA13_SWDIO")
        self.assertEqual(nn.generate("MC", "PA0", "ADC1_INP16", "FC_3V3_SENSE", gpio_style=True), "MC_PA0__FC_3V3_SENSE")
        self.assertEqual(nn.generate("MC", "PI5", "S_TIM8_CH1", "BB_PWM2"), "MC_PI5_TIM8_CH1__BB_PWM2")
        # round trip
        p = nn.parse(nn.generate("FC", "PA5", "SPI1_SCK", "OBS"), "FC")
        self.assertEqual((p.pin, p.cubemx_signal, p.function), ("PA5", "SPI1_SCK", "OBS"))


class RelateTest(unittest.TestCase):
    def rel(self, label, pin, signal, function, prefix="FC"):
        return nn.relate(label, prefix, pin, signal, function)["relation"]

    def test_measured_cases(self):
        self.assertEqual(self.rel("FC_PA3_USART2_RX__BB_ESC_TX", "PA3", "USART2_RX", "BB_ESC_TX"), "same")
        self.assertEqual(self.rel("FC_PC0__DBG_IO1", "PC0", "GPIO_Output", "DBG_IO1"), "same")
        self.assertEqual(self.rel("FC_PA13_SWDIO", "PA13 (JTMS/SWDIO)", "DEBUG_JTMS-SWDIO", None), "same")
        self.assertEqual(self.rel("MC_PA3_OTGHS_ULPI_D0", "PA3", "USB_OTG_HS_ULPI_D0", None, "MC"), "same")
        self.assertEqual(self.rel("MC_PA0__FC_3V3_SENSE_BUF", "PA0", "ADC1_INP16", "FC_3V3_SENSE_BUF", "MC"), "same",
                         "analog pin written GPIO-style with the function")
        self.assertEqual(self.rel("MC_PG9_USART6_RX__DBG", "PG9", "USART6_RX", None, "MC"), "function_only_differs")
        self.assertEqual(self.rel("MC_PE3_SPI4_nCS1__MEI", "PE3", "GPIO_Output", "SPI4_nCS1", "MC"), "function_only_differs",
                         "sheet adds a suffix the .ioc label lacks")
        self.assertEqual(self.rel("MC_PG5_FMC_BA1", "PG5", "FMC_A15_BA1", None, "MC"), "same", "FMC_BA1 is a known spelling")
        self.assertEqual(self.rel("MC_PK6_SPI5_nCS1__BB", "PK6", "GPIO_Output", "BB", "MC"), "function_only_differs",
                         "the label's role word is not the function; relate() reports it, reconcile decides")
        self.assertEqual(self.rel("MC_PD15_FMC_D1", "PD15", "FMC_D1_DA1", None, "MC"), "same")
        self.assertEqual(self.rel("FC_OTGFS_N", "PA11", "USB_OTG_FS_DM", "OTGFS_N"), "same")
        self.assertEqual(self.rel("MC_PC12_UART5_TX__FC_PD2_UART5_RX", "PC12", "UART5_TX", "FC_PD2_UART5_RX", "MC"), "same")
        self.assertEqual(self.rel("MC_PC9_UART5_CTS__FC_PC8_UART5_RTS", "PC8", "UART5_RTS", None), "shared_net")
        self.assertEqual(self.rel("FC_PH13", "PH13", None, None), "spare")
        self.assertEqual(self.rel("FC_OSC_IN", "PH0-OSC_IN (PH0)", None, None), "pinless")
        self.assertEqual(self.rel("SE3_PPS_INPUT", "PE11", "S_TIM1_CH2", "PPS_INPUT"), "outside_convention")
        self.assertEqual(self.rel("FC_PB5_UART5_RX", "PB5", "SPI1_MOSI", None), "different")
        self.assertEqual(self.rel("FC_PA1_ETH_REF_CLK", "PA1", None, None), "unassigned")
        self.assertEqual(self.rel("FC_PA2_ETH_MDIO", "PA3", "ETH_MDIO", None), "outside_convention", "names another pin")


if __name__ == "__main__":
    unittest.main(verbosity=2)
