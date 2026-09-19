"""The house net-label convention as a codec: parse a label into its parts,
generate a label from parts, and say how a label relates to what CubeMX
knows about the pin.

Measured on the X2 Control Board sheets (PREFIX = FC or MC) and the pinning
workbooks' own generator columns:

    PREFIX_<PIN>_<Signal>[__<Function>]     peripheral pin      FC_PA3_USART2_RX__BB_ESC_TX
    PREFIX_<PIN>__<Function>                GPIO or analog pin  FC_PC0__DBG_IO1, MC_PA0__FC_3V3_SENSE
    PREFIX_<PIN>                            spare pin           FC_PH13, MC_PA0_C
    PREFIX_<Function>                       differential pair   FC_OTGFS_N (the pin is implicit)
    MC_<pin>_<sig>__FC_<pin>_<sig>          a net shared by both MCUs, named from both ends

The signal part abbreviates CubeMX's name: USB_OTG_HS_ULPI_D0 -> OTGHS_ULPI_D0,
S_TIM5_CH1 -> TIM5_CH1, DEBUG_JTMS-SWDIO -> SWDIO (or SWD_SWDIO), RCC_OSC_IN
-> OSC_IN, FMC_D1_DA1 -> FMC_D1. The Function is the designer's word for what
the pin does (the CubeMX GPIO_Label after "__", the sheet's Postfix / other end).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Set

from . import naming

# CubeMX prefix -> label prefix (order matters: longest first)
PREFIX_ABBREV = [("USB_OTG_HS_", "OTGHS_"), ("USB_OTG_FS_", "OTGFS_"), ("S_TIM", "TIM"), ("ADCx_", "ADC_"), ("RCC_", "")]
# whole-signal spellings
WHOLE_ABBREV = {
    "DEBUG_JTMS-SWDIO": ["SWDIO", "SWD_SWDIO"], "DEBUG_JTCK-SWCLK": ["SWCLK", "SWD_SCLK", "SWD_SWCLK"],
    "DEBUG_JTDO-SWO": ["SWO", "SWD_SWO"], "DEBUG_JTDI": ["JTDI", "SWD_JTDI"], "DEBUG_JTRST": ["NJTRST", "SWD_NJTRST"],
    "SYS_JTMS-SWDIO": ["SWDIO", "SWD_SWDIO"], "SYS_JTCK-SWCLK": ["SWCLK", "SWD_SWCLK"], "SYS_JTDO-SWO": ["SWO"],
}
_FMC_ALIAS = re.compile(r"^(FMC_D\d+)_DA\d+$")
_ETR_ALIAS = re.compile(r"^(?:S_)?(TIM\d+_CH\d+N?)_ETR$")
_FMC_BA_ALIAS = re.compile(r"^FMC_A\d+_(BA\d)$")
_ADCX_ALIAS = re.compile(r"^ADCx_(.*)$")            # a shared ADC1/ADC2 input; the sheets say ADC1_...
# a GPIO label that names the pin's ROLE (SPI5_nCS1, I2C3_nRST) rather than its function
ROLE_WORD = re.compile(r"^(SPI|I2C|I2S|USART|UART|LPUART|TIM|LPTIM|FDCAN|CAN|SDMMC|ETH|USB|OTG|ADC|DAC|FMC|QUADSPI|QSPI|SAI|DCMI|LTDC|SWD|JTAG)\d*_", re.I)
_PIN_IN_LABEL = re.compile(r"^(?P<prefix>[A-Za-z0-9]+)_(?P<pin>P[A-K]\d{1,2}(?:_C)?)(?P<rest>(?:_.*)?)$")


def signal_spellings(signal: Optional[str]) -> List[str]:
    """Every spelling a label may use for a CubeMX signal, preferred first."""
    if not signal:
        return []
    out = [signal]
    out += WHOLE_ABBREV.get(signal, [])
    for a, b in PREFIX_ABBREV:
        if signal.startswith(a):
            out.append(b + signal[len(a):])
    m = _FMC_ALIAS.match(signal)
    if m:
        out.append(m.group(1))
    m = _ETR_ALIAS.match(signal)
    if m:
        out.append(m.group(1))
    m = _FMC_BA_ALIAS.match(signal)
    if m:
        out.append("FMC_" + m.group(1))
    m = _ADCX_ALIAS.match(signal)
    if m:
        out += ["ADC1_" + m.group(1), "ADC2_" + m.group(1), "ADC12_" + m.group(1)]
    seen: Set[str] = set()
    return [s for s in out if not (s in seen or seen.add(s))]


def preferred_spelling(signal: Optional[str]) -> Optional[str]:
    """The spelling the sheets use most: abbreviated form when one exists."""
    sp = signal_spellings(signal)
    if not sp:
        return None
    return sp[1] if len(sp) > 1 and sp[1] != signal else sp[0]


def unabbreviate(text: str) -> str:
    """Best-effort inverse for the head of a label's signal part."""
    for cube, short in WHOLE_ABBREV.items():
        if text in short:
            return cube
    for a, b in PREFIX_ABBREV:
        if b and text.startswith(b):
            return a + text[len(b):]
    return text


@dataclass(frozen=True)
class Parsed:
    prefix: Optional[str]
    pin: Optional[str]              # PA3, PA0_C; None for pin-less forms
    signal: Optional[str]           # as spelled in the label (abbreviated)
    function: Optional[str]
    form: str                       # peripheral | gpio | spare | pinless | other

    @property
    def cubemx_signal(self) -> Optional[str]:
        return unabbreviate(self.signal) if self.signal else None


def parse(label: str, prefix: Optional[str] = None) -> Parsed:
    """Split a label. With `prefix`, labels not starting with it are 'other'."""
    m = _PIN_IN_LABEL.match(label)
    if m and (prefix is None or m.group("prefix") == prefix):
        rest = m.group("rest")
        if rest == "":
            return Parsed(m.group("prefix"), m.group("pin"), None, None, "spare")
        if rest.startswith("__"):
            return Parsed(m.group("prefix"), m.group("pin"), None, rest[2:] or None, "gpio")
        body = rest[1:]
        sig, _, func = body.partition("__")
        return Parsed(m.group("prefix"), m.group("pin"), sig or None, func or None, "peripheral")
    if prefix and label.startswith(prefix + "_"):
        return Parsed(prefix, None, None, label[len(prefix) + 1:], "pinless")
    return Parsed(None, None, None, None, "other")


def split_gpio_label(label: Optional[str]):
    """A GPIO's CubeMX label -> (role, function): SPI3_nCS2__SE1 -> (SPI3_nCS2, SE1);
    SPI5_nCS1 -> (SPI5_nCS1, None); DBG_IO1 -> (None, DBG_IO1)."""
    if not label:
        return None, None
    if "__" in label:
        role, _, fn = label.partition("__")
        return role or None, fn or None
    if ROLE_WORD.match(label):
        return label, None
    return None, label


def generate(prefix: str, pin: str, signal: Optional[str], function: Optional[str] = None,
             gpio_style: Optional[bool] = None, role: Optional[str] = None) -> str:
    """The label for a pin. GPIO_* signals (and pins with no signal) use the
    PREFIX_PIN__Function form, or PREFIX_PIN_Role__Function when the GPIO has a
    role word (a chip select); peripherals PREFIX_PIN_Signal[__Function].
    `gpio_style` forces the form (analog pins on the MC are written that way)."""
    base = naming.base_pin(pin)
    is_gpio = gpio_style if gpio_style is not None else (not signal or signal.startswith("GPIO_"))
    if is_gpio:
        if role:
            return f"{prefix}_{base}_{role}" + (f"__{function}" if function else "")
        return f"{prefix}_{base}__{function}" if function else f"{prefix}_{base}"
    sig = preferred_spelling(signal)
    return f"{prefix}_{base}_{sig}" + (f"__{function}" if function else "")


# ---- relating a label to what CubeMX / the sheet know ------------------------

RELATIONS = ["same", "function_only_differs", "signal_spelling", "same_peripheral", "different", "unassigned",
             "spare", "pinless", "shared_net", "outside_convention"]


def relate(label: str, prefix: str, pin: str, signal: Optional[str], function: Optional[str],
           other_labels: Iterable[str] = ()) -> dict:
    """How a schematic label relates to the pin's CubeMX signal and the
    designer's function. Returns {relation, detail, parsed}."""
    p = parse(label, prefix)
    base = naming.base_pin(pin)
    spell = set(signal_spellings(signal))
    out = {"label": label, "relation": "different", "detail": "", "parsed": p}
    if p.form == "other":
        if signal and any(f"{prefix}_{base}_{s}" in label for s in spell):
            out.update(relation="shared_net", detail="named from both ends")
        else:
            out.update(relation="outside_convention")
        return out
    if p.form == "pinless":
        if function and p.function in (function, function.split("__", 1)[0]):
            out.update(relation="same", detail="pair/bus label without the pin")
        else:
            out.update(relation="pinless", detail=p.function or "")
        return out
    if p.pin != base:
        out.update(relation="outside_convention", detail=f"label names pin {p.pin}, not {base}")
        return out
    if p.form == "spare":
        out.update(relation="spare" if not signal else "different",
                   detail="" if not signal else f"pin-only label but CubeMX assigns {signal}")
        return out
    if not signal:
        out.update(relation="unassigned", detail=f"label says {p.signal or ''}{'__' + p.function if p.function else ''}, CubeMX assigns nothing")
        return out
    is_gpio = signal.startswith("GPIO_")
    if p.form == "gpio":
        if is_gpio or not p.signal:
            if function and p.function == function:
                out.update(relation="same")
            elif function and p.function and (p.function.startswith(function + "__") or function.startswith(p.function + "__")):
                out.update(relation="function_only_differs", detail=f"label {p.function!r} vs {function!r}")
            elif not function:
                out.update(relation="function_only_differs", detail=f"label names {p.function!r}; CubeMX label empty")
            else:
                out.update(relation="function_only_differs", detail=f"label {p.function!r} vs {function!r}")
        return out
    # peripheral form
    if is_gpio:
        # a GPIO whose label carries a role word: MC_PE3_SPI4_nCS1__MEI, function SPI4_nCS1
        role = p.signal + (f"__{p.function}" if p.function else "")
        if function and role == function:
            out.update(relation="same")
        else:
            out.update(relation="function_only_differs", detail=f"label {role!r} vs {function!r}")
        return out
    if p.signal in spell:
        if (p.function or None) == (function or None):
            out.update(relation="same")
        else:
            out.update(relation="function_only_differs", detail=f"label {p.function!r} vs {function!r}")
    elif naming.canonical_group(naming.peripheral_group(unabbreviate(p.signal))) == \
            naming.canonical_group(naming.peripheral_group(signal)) and not is_gpio:
        out.update(relation="signal_spelling", detail=f"label {p.signal!r} vs {signal!r}")
    else:
        out.update(relation="different", detail=f"label {p.signal!r} vs CubeMX {signal!r}")
    return out
