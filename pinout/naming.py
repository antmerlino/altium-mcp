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

# PA13, PF0-OSC_IN, PC2_C, "PA13 (JTMS/SWDIO)" (H7 CubeMX names carry the
# debug/oscillator annotation after a space).
PORT_PIN = re.compile(r"^(P([A-K])(\d{1,2}))(?:[-_ ].*)?$")

# Signal prefixes that are not peripherals in the grouping sense. G4-series
# CubeMX calls the debug port SYS_*, H7-series calls it DEBUG_*.
_SPECIAL_GROUPS = {
    "SYS_JTMS-SWDIO": "SWD", "SYS_JTCK-SWCLK": "SWD", "SYS_JTDO-SWO": "SWD",
    "SYS_JTDI": "SWD", "SYS_JTRST": "SWD", "SYS_WKUP": "SYS",
    "DEBUG_JTMS-SWDIO": "SWD", "DEBUG_JTCK-SWCLK": "SWD", "DEBUG_JTDO-SWO": "SWD",
    "DEBUG_JTDI": "SWD", "DEBUG_JTRST": "SWD",
}
_DEBUG_ALIASES = {
    "SYS_JTMS-SWDIO": "JTMS/SWDIO", "SYS_JTCK-SWCLK": "JTCK/SWCLK",
    "SYS_JTDO-SWO": "JTDO/SWO", "SYS_JTDI": "JTDI", "SYS_JTRST": "nJTRST",
    "DEBUG_JTMS-SWDIO": "JTMS/SWDIO", "DEBUG_JTCK-SWCLK": "JTCK/SWCLK",
    "DEBUG_JTDO-SWO": "JTDO/SWO", "DEBUG_JTDI": "JTDI", "DEBUG_JTRST": "nJTRST",
}
# Peripheral names that are more than one underscore token.
_MULTI_TOKEN_HEADS = ("USB_OTG_HS", "USB_OTG_FS")

# What a hand-drawn symbol may call a group versus what CubeMX calls the
# peripheral. Both sides go through canonical_group() before being compared.
GROUP_ALIASES = {
    "QSPI": "QUADSPI", "OSPI": "OCTOSPI",
    "OTGFS": "USB_OTG_FS", "OTG_FS": "USB_OTG_FS", "USBFS": "USB_OTG_FS", "USB_FS": "USB_OTG_FS",
    "OTGHS": "USB_OTG_HS", "OTG_HS": "USB_OTG_HS", "USBHS": "USB_OTG_HS", "USB_HS": "USB_OTG_HS",
    "ULPI": "USB_OTG_HS", "OTG_ULPI": "USB_OTG_HS", "OTGULPI": "USB_OTG_HS",
    "OTG_HS_ULPI": "USB_OTG_HS", "USB_ULPI": "USB_OTG_HS",
    "JTAG": "SWD", "DEBUG": "SWD", "SWD/JTAG": "SWD", "JTAG/SWD": "SWD",
    "ADCX": "ADC", "ADC1/2": "ADC", "ADC1/ADC2": "ADC",
    "OSC": "RCC", "CLOCK": "RCC", "XTAL": "RCC",
    "CAN1": "FDCAN1", "CAN2": "FDCAN2", "CAN3": "FDCAN3",
    "SDIO": "SDMMC1", "ETHERNET": "ETH", "RMII": "ETH", "MII": "ETH",
}

OUTPUT_HINTS = ("_TX", "_SCK", "_MOSI", "_NSS", "_CH", "GPIO_Output", "DAC", "_OUT",
                "_MCLK", "_WS", "_CLK", "_CS")
INPUT_HINTS = ("_RX", "_MISO", "ADC", "COMP", "GPIO_Input", "EXTI", "_IN", "_CTS", "_DE",
               "WKUP")


def base_pin(name: str) -> str:
    """PF0-OSC_IN -> PF0; "PA13 (JTMS/SWDIO)" -> PA13; VDD -> VDD.

    PC2_C stays PC2_C: on the H7 the _C pad is a separate ball (the direct
    ADC path) that exists alongside PC2, so it is its own pin, not a spelling."""
    m = PORT_PIN.match(name)
    if not m:
        return name
    rest = name[len(m.group(1)):]
    return m.group(1) + ("_C" if rest.upper().startswith("_C") and len(rest.rstrip()) == 2 else "")


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
    if signal.startswith("DEBUG_TRACE"):
        return "TRACE"
    if signal.startswith("DEBUG_"):
        return "SWD"
    if signal.startswith("ADCx_"):          # an input shared by ADC1 and ADC2
        return "ADC"
    for h in _MULTI_TOKEN_HEADS:
        if signal.startswith(h + "_"):
            return h
    head = signal.split("_", 1)[0]
    # S_TIM3_CH1 (a shared-timer signal in some .ioc files) -> TIM3
    if head == "S" and "_" in signal:
        head = signal.split("_", 2)[1]
    return head


def canonical_group(text: Optional[str]) -> Optional[str]:
    """Normalise a group label or peripheral name for comparison:
    'QSPI' and 'QUADSPI' are the same thing, so are 'OTGFS' and 'USB_OTG_FS'."""
    if not text:
        return None
    t = text.strip().upper().replace(" ", "")
    t = GROUP_ALIASES.get(t, t)
    # designers write UART4 for USART4 and vice versa; no MCU has both with
    # the same index, so the distinction carries no information
    return re.sub(r"^USART(\d+)$", r"UART\1", t)


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
