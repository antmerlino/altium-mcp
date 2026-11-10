"""pinout.cubemx: the diff logic that turns a CubeMX headless round trip into a
verdict. CubeMX itself is not run here (it is slow, and not on every machine);
the fixtures are the key patterns it actually produced on the X9 Mission
Computer .ioc, 2026-09-22."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pinout.cubemx import copy_name, diff, find_cubemx, subtract_baseline  # noqa: E402

NV = "true\\:0\\:0\\:false\\:false\\:true\\:true\\:true\\:true"


def ioc(*lines):
    return "\r\n".join(["#MicroXplorer Configuration settings - do not modify", *lines, ""])


# what we handed CubeMX: GPXTI signal and an NVIC vector, but no SH binding
HANDED = ioc(
    "Mcu.Pin0=PC7", "Mcu.Pin1=PE1", "Mcu.PinsNb=2",
    f"NVIC.EXTI1_IRQn={NV}", f"NVIC.EXTI9_5_IRQn={NV}",
    "PC7.GPIOParameters=GPIO_Label", "PC7.GPIO_Label=WIFI_IRQ", "PC7.Signal=GPXTI7",
    "PE1.Signal=GPXTI1",
    "ProjectManager.functionlistsort=1-SystemClock_Config,30-MX_TIM16_Init",
)
# what it saved: modes forced, bindings with ConfNb=0, every vector dropped,
# pin list re-sorted, function list normalised
SAVED = ioc(
    "Mcu.Pin0=PE1", "Mcu.Pin1=PC7", "Mcu.PinsNb=2",
    "PC7.GPIOParameters=GPIO_Label,GPIO_ModeDefaultEXTI", "PC7.GPIO_Label=WIFI_IRQ",
    "PC7.GPIO_ModeDefaultEXTI=GPIO_MODE_IT_RISING", "PC7.Signal=GPXTI7",
    "PE1.GPIOParameters=GPIO_ModeDefaultEXTI", "PE1.GPIO_ModeDefaultEXTI=GPIO_MODE_IT_RISING",
    "PE1.Signal=GPXTI1",
    "ProjectManager.functionlistsort=1-SystemClock_Config,32-MX_TIM4_Init",
    "SH.GPXTI1.ConfNb=0", "SH.GPXTI7.ConfNb=0",
)


class DiffTest(unittest.TestCase):
    def test_dropped_vectors_are_failures(self):
        r = diff(HANDED, SAVED)
        self.assertFalse(r.ok)
        self.assertEqual(sorted(r.removed), ["NVIC.EXTI1_IRQn", "NVIC.EXTI9_5_IRQn"])
        self.assertEqual(r.added["SH.GPXTI7.ConfNb"], "0")
        self.assertEqual(r.changed["PC7.GPIOParameters"],
                         ("GPIO_Label", "GPIO_Label,GPIO_ModeDefaultEXTI"))
        self.assertIn("REMOVED NVIC.EXTI9_5_IRQn", r.summary())

    def test_pin_list_order_is_not_a_change(self):
        r = diff(HANDED, SAVED)
        self.assertEqual((r.pins_added, r.pins_removed), ([], []))
        self.assertFalse(any(k.startswith("Mcu.Pin") for k in {**r.added, **r.removed, **r.changed}))

    def test_pin_list_membership_is(self):
        r = diff(HANDED, HANDED.replace("Mcu.Pin1=PE1", "Mcu.Pin1=PG2"))
        self.assertEqual((r.pins_added, r.pins_removed), (["PG2"], ["PE1"]))
        self.assertFalse(r.ok)

    def test_identical_is_clean(self):
        r = diff(HANDED, HANDED)
        self.assertTrue(r.clean and r.ok)
        self.assertIn("no changes", r.summary())

    def test_baseline_normalisation_is_subtracted(self):
        # the untouched original gets the same function-list rewrite on any save
        base_before = ioc("ProjectManager.functionlistsort=1-SystemClock_Config,30-MX_TIM16_Init")
        base_after = ioc("ProjectManager.functionlistsort=1-SystemClock_Config,32-MX_TIM4_Init")
        r = subtract_baseline(diff(HANDED, SAVED), diff(base_before, base_after))
        self.assertNotIn("ProjectManager.functionlistsort", r.changed)
        self.assertIn("PC7.GPIOParameters", r.changed, "caused by our edit, so it stays")
        self.assertEqual(sorted(r.removed), ["NVIC.EXTI1_IRQn", "NVIC.EXTI9_5_IRQn"])


class CopyNameTest(unittest.TestCase):
    def test_copy_is_named_after_the_project(self):
        text = ioc("ProjectManager.ProjectFileName=STM32H753XIHx.ioc")
        self.assertEqual(copy_name(text, "STM32H753XIHx.fixed.ioc"), "STM32H753XIHx.ioc")

    def test_fallback_when_absent_or_odd(self):
        self.assertEqual(copy_name(ioc(), "a.ioc"), "a.ioc")
        self.assertEqual(copy_name(ioc("ProjectManager.ProjectFileName=..\\evil.ioc"), "a.ioc"), "a.ioc")


class FindTest(unittest.TestCase):
    def test_env_override_comes_first(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            for d in (a, b):
                Path(d, "STM32CubeMX.exe").write_bytes(b"")
            Path(b, "Programs", "STM32CubeMX").mkdir(parents=True)
            Path(b, "Programs", "STM32CubeMX", "STM32CubeMX.exe").write_bytes(b"")
            env = {"STM32CUBEMX_HOME": a, "LOCALAPPDATA": b, "ProgramFiles": "", "ProgramFiles(x86)": ""}
            with mock.patch.dict(os.environ, env):
                homes = find_cubemx()
            self.assertEqual(homes[0], Path(a))
            self.assertIn(Path(b, "Programs", "STM32CubeMX"), homes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
