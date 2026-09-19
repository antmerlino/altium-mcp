"""House naming rules, measured from the library rather than invented.

Every rule here was read off real symbols and sheets:

    pin display names   PB8-BOOT0, PG10-nRST, PC2_C      (the MCU-DB name as-is)
                        PA13 (JTMS/SWDIO)                 ("debug" style, H753)
    net labels          ESC_PA13_SYS_JTMS-SWDIO           <PREFIX>_<PIN>_<Signal>
                        ESC_PC13_TIM1_CH1N__PWM_UL        ...__<Function> when named
    symbol names        EMBEDDED_IC_STM32G431C8U6TR       EMBEDDED_IC_<MPN>
                        EMBEDDED_IC_STM32H753IIK6_X57     EMBEDDED_IC_<MPN>_<BOARD>
    descriptions        EMBEDDED IC MCU 32BIT 128KB FLASH 48UFQFPN
    group labels        SWD, FDCAN1, SPI1, USART2, I2C1   (peripheral instance)

Electrical types are Altium's enum names; values verified on AD Develop:
Input=0 IO=1 Output=2 OpenCollector=3 Passive=4 HiZ=5 OpenEmitter=6 Power=7.
"""
from __future__ import annotations

import re
from typing import Optional

PORT_PIN = re.compile(r"^(P([A-K])(\d{1,2}))(?:[-_].*)?$")

# Signal prefixes that are not peripherals in the grouping sense.
_SPECIAL_GROUPS = {
    "SYS_JTMS-SWDIO": "SWD", "SYS_JTCK-SWCLK": "SWD", "SYS_JTDO-SWO": "SWD",
    "SYS_JTDI": "SWD", "SYS_JTRST": "SWD", "SYS_WKUP": "SYS",
}
_DEBUG_ALIASES = {
    "SYS_JTMS-SWDIO": "JTMS/SWDIO", "SYS_JTCK-SWCLK": "JTCK/SWCLK",
    "SYS_JTDO-SWO": "JTDO/SWO", "SYS_JTDI": "JTDI", "SYS_JTRST": "nJTRST",
}

OUTPUT_HINTS = ("_TX", "_SCK", "_MOSI", "_NSS", "_CH", "GPIO_Output", "DAC", "_OUT",
                "_MCLK", "_WS", "_CLK", "_CS")
INPUT_HINTS = ("_RX", "_MISO", "ADC", "COMP", "GPIO_Input", "EXTI", "_IN", "_CTS", "_DE",
               "WKUP")


def base_pin(name: str) -> str:
    """PF0-OSC_IN -> PF0; PC2_C -> PC2; VDD -> VDD."""
    m = PORT_PIN.match(name)
    return m.group(1) if m else name


def port_of(name: str) -> Optional[str]:
    """PB12 -> PB; VDD -> None."""
    m = PORT_PIN.match(name)
    return f"P{m.group(2)}" if m else None


def is_port_pin(name: str) -> bool:
    return port_of(name) is not None


def peripheral_group(signal: Optional[str]) -> Optional[str]:
    """USART2_TX -> USART2; SYS_JTMS-SWDIO -> SWD; GPIO_Output -> GPIO; None -> None."""
    if not signal:
        return None
    if signal in _SPECIAL_GROUPS:
        return _SPECIAL_GROUPS[signal]
    if signal.startswith("GPIO_") or signal.startswith("GPXTI"):
        return "GPIO"
    if signal.startswith("RCC_"):
        return "RCC"
    head = signal.split("_", 1)[0]
    # S_TIM3_CH1 (a shared-timer signal in some .ioc files) -> TIM3
    if head == "S" and "_" in signal:
        head = signal.split("_", 2)[1]
    return head


def display_name(pin_name: str, signal: Optional[str] = None, style: str = "plain") -> str:
    """The text on the pin. 'plain' keeps the MCU-DB name (PB8-BOOT0, PF0-OSC_IN).
    'debug' appends the H753-style annotation for SWD/JTAG pins."""
    if style == "debug" and signal in _DEBUG_ALIASES:
        return f"{base_pin(pin_name)} ({_DEBUG_ALIASES[signal]})"
    return pin_name


def net_label(prefix: str, pin_name: str, signal: Optional[str],
              function: Optional[str] = None) -> str:
    """ESC_PA13_SYS_JTMS-SWDIO, or ESC_PC13_TIM1_CH1N__PWM_UL when a function is named."""
    parts = [p for p in (prefix, base_pin(pin_name)) if p]
    if signal:
        parts.append(signal)
    name = "_".join(parts)
    if function:
        name += f"__{function}"
    return name


def electrical_type(pin_name: str, mcu_type: str, signal: Optional[str]) -> str:
    """Altium eElectric* name for a pin, derived the way the H753 symbols do it."""
    upper = pin_name.upper()
    if mcu_type == "Power":
        if upper.startswith(("VSS", "GND")) or upper.startswith("VREF-") or upper == "NC":
            return "eElectricPassive"
        return "eElectricPower"
    if mcu_type in ("Reset", "Boot") or upper in ("NRST", "BOOT0") or upper.endswith("-NRST") \
            or upper.endswith("-BOOT0"):
        return "eElectricInput"
    if mcu_type == "NC":
        return "eElectricPassive"
    if signal:
        if signal.startswith("RCC_OSC"):
            return "eElectricIO"
        if any(h in signal for h in INPUT_HINTS) and not any(h in signal for h in ("_OUT",)):
            return "eElectricInput"
        if any(h in signal for h in OUTPUT_HINTS):
            return "eElectricOutput"
    return "eElectricIO"


def symbol_name(mpn: str, board: Optional[str] = None) -> str:
    return f"EMBEDDED_IC_{mpn}" + (f"_{board}" if board else "")


def description(bits: int, flash: str, package: str) -> str:
    return f"EMBEDDED IC MCU {bits}BIT {flash} FLASH {package}".upper()
