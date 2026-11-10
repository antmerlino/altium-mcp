"""Read and MODIFY a CubeMX .ioc file without disturbing anything else in it.

An .ioc is a flat `Key=Value` file that CubeMX keeps sorted by key. Pin
assignments are the `<PIN>.Signal`, `.Mode`, `.GPIO_Label`, `.Locked` and
`.GPIOParameters` keys; the list of assigned pins is mirrored in
`Mcu.Pin0..N` and counted in `Mcu.PinsNb`. Everything else - RCC settings,
peripheral parameters, project manager options - must survive a round trip
byte for byte, because CubeMX owns it and we do not understand all of it.

Physical pins are `PA0`, `PB12`, `PF0-OSC_IN`, `PG10-nRST` ... Virtual pins are
`VP_SYS_VS_Systick` and friends; they carry `.Signal` too but have no package
position and never appear on a symbol.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

# PA0, PB12, PF0-OSC_IN, PG10-nRST, PC2_C (an H7 direct-ADC pad) and the H7
# debug/oscillator pins whose CubeMX name carries an annotation in
# parentheses: "PA13 (JTMS/SWDIO)", "PH0-OSC_IN (PH0)", "PC14-OSC32_IN (OSC32_IN)".
PHYSICAL_PIN = re.compile(r"^P[A-K]\d{1,2}(?:[-_][A-Za-z0-9_+\-]+)?(?: \([^()]*\))?$")
VIRTUAL_PREFIX = "VP_"
PIN_KEY = re.compile(r"^([^.]+)\.(Signal|Mode|GPIO_Label|Locked|GPIOParameters)$")

# An .ioc is a Java .properties file: a space, '=', ':' or backslash inside a
# KEY is written with a backslash in front of it, so the pin above appears as
#     PA13\ (JTMS/SWDIO).Signal=DEBUG_JTMS-SWDIO
# Values are never escaped. Keys are compared and reported UNESCAPED
# everywhere in this module; the escaping exists only on the line in the file.
_KEY_ESCAPES = {" ": "\\ ", "=": "\\=", ":": "\\:", "\\": "\\\\"}

# An EXTI input on line n is `<pin>.Signal=GPXTIn`, but that key alone is not
# what CubeMX writes. It also binds the signal to its GPIO function,
#     SH.GPXTIn.0=GPIO_EXTIn
#     SH.GPXTIn.ConfNb=1
# and without the binding it treats the line as unconfigured: on load it warns
# and forces the GPIO mode, on save it writes ConfNb=0 and DROPS the NVIC
# vector, so the interrupt silently ends up disabled. Every EXTI pin in the
# 6.x board files CubeMX ships (153 of them) carries the binding. Found on the
# X9 Mission Computer .ioc, 2026-09-22: use set_exti(), never the raw keys.
EXTI_EDGES = {
    "rising": "GPIO_MODE_IT_RISING",
    "falling": "GPIO_MODE_IT_FALLING",
    "both": "GPIO_MODE_IT_RISING_FALLING",
}
# Enabled, preemption 0, sub 0: the most common EXTI vector value in the board
# files CubeMX ships. Values are written verbatim, backslashes included.
EXTI_NVIC_ENABLED = r"true\:0\:0\:false\:false\:true\:true\:true\:true"
# How a family maps EXTI lines onto NVIC vectors. Only layouts checked against
# CubeMX output are listed (its board files, and its own H7 load warnings
# naming EXTI1 / EXTI2 / EXTI9_5 / EXTI15_10); any other family must pass the
# vector explicitly rather than have one guessed.
_EXTI_VECTOR_STYLE = {
    "STM32F1": "grouped", "STM32F4": "grouped", "STM32G4": "grouped",
    "STM32H7": "grouped", "STM32L4": "grouped", "STM32WB": "grouped",
    "STM32C0": "paired", "STM32L0": "paired",
    "STM32U3": "per_line", "STM32U5": "per_line", "STM32WBA": "per_line",
}
_PIN_LINE = re.compile(r"^P[A-K](\d{1,2})(?!\d)")


def exti_line(pin: str) -> int:
    """The EXTI line a pin can drive: its pin number (PC7 and PG7 -> 7)."""
    m = _PIN_LINE.match(pin)
    if not m or int(m.group(1)) > 15:
        raise ValueError(f"not a GPIO port pin: {pin!r}")
    return int(m.group(1))


def exti_vector(line: int, family: Optional[str]) -> str:
    """NVIC vector name serving EXTI `line` on `family` (the file's Mcu.Family)."""
    style = _EXTI_VECTOR_STYLE.get(family or "")
    if style == "grouped":
        name = f"EXTI{line}" if line < 5 else ("EXTI9_5" if line < 10 else "EXTI15_10")
    elif style == "paired":
        name = "EXTI0_1" if line < 2 else ("EXTI2_3" if line < 4 else "EXTI4_15")
    elif style == "per_line":
        name = f"EXTI{line}"
    else:
        raise ValueError(f"EXTI vector layout for {family!r} is not known; pass vector= explicitly")
    return name + "_IRQn"


def _signal_line(signal: Optional[str]) -> Optional[int]:
    m = re.fullmatch(r"GPXTI(\d{1,2})", signal or "")
    return int(m.group(1)) if m else None


def escape_key(key: str) -> str:
    return "".join(_KEY_ESCAPES.get(c, c) for c in key)


def unescape_key(raw: str) -> str:
    out, i = [], 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw):
            out.append(raw[i + 1])
            i += 2
        else:
            out.append(raw[i])
            i += 1
    return "".join(out)


def is_physical_pin(name: str) -> bool:
    return bool(PHYSICAL_PIN.match(name))


def is_virtual_pin(name: str) -> bool:
    return name.startswith(VIRTUAL_PREFIX)


@dataclass
class PinAssignment:
    signal: Optional[str] = None
    mode: Optional[str] = None
    label: Optional[str] = None
    locked: bool = False


class IocFile:
    def __init__(self, text: str):
        self._eol = "\r\n" if "\r\n" in text else "\n"
        self._lines: List[str] = text.split(self._eol)
        # a trailing newline yields one empty final element; remember to
        # restore it rather than growing the file by a line per save
        self._trailing_newline = text.endswith(self._eol)
        if self._trailing_newline:
            self._lines.pop()

    # ---- file level ------------------------------------------------------
    @classmethod
    def load(cls, path) -> "IocFile":
        # bytes, not read_text(): text mode turns CRLF into LF on the way in,
        # and save() would then hand CubeMX its own file back with LF endings
        return cls(Path(path).read_bytes().decode("utf-8"))

    def text(self) -> str:
        body = self._eol.join(self._lines)
        return body + self._eol if self._trailing_newline else body

    def save(self, path) -> None:
        Path(path).write_text(self.text(), encoding="utf-8", newline="")

    # ---- key/value level -------------------------------------------------
    @staticmethod
    def _split(line: str):
        """(key, value) for a `key=value` line, key unescaped; None otherwise.

        The separator is the first '=' that is not itself escaped. A raw,
        unescaped space in the key means this is not a key CubeMX wrote (in
        .properties it would terminate the key), so the line is left alone.
        """
        if not line or line[0] in "#!":
            return None
        i, n = 0, len(line)
        while i < n:
            c = line[i]
            if c == "\\":
                i += 2
                continue
            if c == "=":
                break
            if c == " ":
                return None
            i += 1
        else:
            return None
        return unescape_key(line[:i]), line[i + 1:]

    def _index_of(self, key: str) -> int:
        for i, line in enumerate(self._lines):
            kv = self._split(line)
            if kv and kv[0] == key:
                return i
        return -1

    def get(self, key: str) -> Optional[str]:
        i = self._index_of(key)
        return None if i < 0 else self._split(self._lines[i])[1]

    def set(self, key: str, value: str) -> None:
        """Replace a key in place, or insert it in sorted position.

        CubeMX writes keys sorted; inserting sorted keeps our edits where
        CubeMX would have put them, so a later CubeMX save produces a minimal
        diff rather than a reshuffle.
        """
        i = self._index_of(key)
        line = f"{escape_key(key)}={value}"
        if i >= 0:
            self._lines[i] = line
            return
        last_key_line = -1
        for j, existing in enumerate(self._lines):
            kv = self._split(existing)
            if not kv:
                continue
            last_key_line = j
            if kv[0] > key:
                self._lines.insert(j, line)
                return
        self._lines.insert(last_key_line + 1, line)

    def delete(self, key: str) -> bool:
        i = self._index_of(key)
        if i < 0:
            return False
        del self._lines[i]
        return True

    def keys(self, prefix: str = "") -> List[str]:
        out = []
        for line in self._lines:
            kv = self._split(line)
            if kv and kv[0].startswith(prefix):
                out.append(kv[0])
        return out

    # ---- convenience -----------------------------------------------------
    @property
    def mcu_name(self) -> Optional[str]:
        return self.get("Mcu.Name")

    @property
    def package(self) -> Optional[str]:
        return self.get("Mcu.Package")

    # ---- pin level -------------------------------------------------------
    def pins(self, include_virtual: bool = False) -> Dict[str, PinAssignment]:
        """Every pin with any assignment key, physical ones by default."""
        found: Dict[str, PinAssignment] = {}
        for line in self._lines:
            kv = self._split(line)
            if not kv:
                continue
            m = PIN_KEY.match(kv[0])
            if not m:
                continue
            pin, field = m.group(1), m.group(2)
            if not (is_physical_pin(pin) or (include_virtual and is_virtual_pin(pin))):
                continue
            rec = found.setdefault(pin, PinAssignment())
            if field == "Signal":
                rec.signal = kv[1]
            elif field == "Mode":
                rec.mode = kv[1]
            elif field == "GPIO_Label":
                rec.label = kv[1]
            elif field == "Locked":
                rec.locked = kv[1].lower() == "true"
        return found

    def set_pin(self, pin: str, signal: Optional[str] = None, mode: Optional[str] = None,
                label: Optional[str] = None, locked: Optional[bool] = None) -> None:
        """Assign or update a pin. Pass None to leave a field alone, "" to clear it."""
        if not (is_physical_pin(pin) or is_virtual_pin(pin)):
            raise ValueError(f"not a CubeMX pin name: {pin!r}")
        if signal is not None and signal != self.get(f"{pin}.Signal"):
            self._release_exti(pin)
        if signal is not None:
            self.set(f"{pin}.Signal", signal) if signal else self.delete(f"{pin}.Signal")
        if mode is not None:
            self.set(f"{pin}.Mode", mode) if mode else self.delete(f"{pin}.Mode")
        if locked is not None:
            self.set(f"{pin}.Locked", "true") if locked else self.delete(f"{pin}.Locked")
        if label is not None:
            if label:
                self.set(f"{pin}.GPIO_Label", label)
                self._add_gpio_parameter(pin, "GPIO_Label")
            else:
                self.delete(f"{pin}.GPIO_Label")
                self._remove_gpio_parameter(pin, "GPIO_Label")
        self._sync_pin_list()

    def clear_pin(self, pin: str) -> None:
        self._release_exti(pin)
        for field in ("Signal", "Mode", "GPIO_Label", "Locked", "GPIOParameters"):
            self.delete(f"{pin}.{field}")
        for key in self.keys(f"{pin}.GPIO_"):
            self.delete(key)
        self._sync_pin_list()

    # ---- EXTI --------------------------------------------------------------
    def set_exti(self, pin: str, edge: str = "rising", label: Optional[str] = None,
                 nvic: bool = True, vector: Optional[str] = None) -> None:
        """Make `pin` an external-interrupt input exactly as CubeMX writes one.

        Writes the signal, the explicit edge, the SH.GPXTIn binding and, with
        nvic=True, enables the vector - leaving an existing vector entry (and
        any priority set on it) alone. Refuses a line another pin already
        holds: EXTI line n serves pin n of ONE port only.
        """
        if edge not in EXTI_EDGES:
            raise ValueError(f"edge must be one of {sorted(EXTI_EDGES)}, not {edge!r}")
        n = exti_line(pin)
        for other, a in self.pins().items():
            if other != pin and _signal_line(a.signal) == n:
                raise ValueError(f"EXTI line {n} is already used by {other}; {pin} cannot have it too")
        key = "NVIC." + (vector or exti_vector(n, self.get("Mcu.Family"))) if nvic else None
        self.set_pin(pin, signal=f"GPXTI{n}", label=label, locked=True)
        self.set(f"{pin}.GPIO_ModeDefaultEXTI", EXTI_EDGES[edge])
        self._add_gpio_parameter(pin, "GPIO_ModeDefaultEXTI")
        self.set(f"SH.GPXTI{n}.0", f"GPIO_EXTI{n}")
        self.set(f"SH.GPXTI{n}.ConfNb", "1")
        if key and self.get(key) is None:
            self.set(key, EXTI_NVIC_ENABLED)

    def _release_exti(self, pin: str) -> None:
        """Undo the side keys set_exti wrote, once `pin` stops being an EXTI input."""
        n = _signal_line(self.get(f"{pin}.Signal"))
        if n is None:
            return
        self.delete(f"{pin}.GPIO_ModeDefaultEXTI")
        self._remove_gpio_parameter(pin, "GPIO_ModeDefaultEXTI")
        for key in self.keys(f"SH.GPXTI{n}."):
            self.delete(key)
        family = self.get("Mcu.Family")
        try:
            vec = exti_vector(n, family)
        except ValueError:
            return                      # unknown layout: the caller owns NVIC
        others = [m for p, a in self.pins().items()
                  if p != pin and (m := _signal_line(a.signal)) is not None]
        if not any(exti_vector(m, family) == vec for m in others):
            self.delete(f"NVIC.{vec}")

    # ---- consistency -----------------------------------------------------
    def check(self) -> List[str]:
        """Problems CubeMX would silently 'repair' on its next load or save.

        Empty is necessary, not sufficient: pinout.cubemx.roundtrip() asks
        CubeMX itself. Run both before handing a hand-edited .ioc to anyone.
        """
        problems: List[str] = []
        family = self.get("Mcu.Family")
        holders: Dict[int, str] = {}
        for pin, a in self.pins().items():
            n = _signal_line(a.signal)
            if n is None:
                continue
            if n in holders:
                problems.append(f"EXTI line {n} is on both {holders[n]} and {pin}; one port per line")
            holders[n] = pin
            if (self.get(f"SH.GPXTI{n}.0") != f"GPIO_EXTI{n}"
                    or self.get(f"SH.GPXTI{n}.ConfNb") != "1"):
                problems.append(f"{pin} is GPXTI{n} without SH.GPXTI{n}.0=GPIO_EXTI{n} / "
                                f"SH.GPXTI{n}.ConfNb=1: CubeMX forces its mode on load and "
                                f"drops its NVIC vector on save")
        try:
            used = {exti_vector(n, family) for n in holders}
            for key in self.keys("NVIC.EXTI"):
                if key[len("NVIC."):] not in used:
                    problems.append(f"{key} is set but no EXTI pin uses it; CubeMX drops it on save")
        except ValueError:
            pass                        # unknown vector layout: cannot judge NVIC keys
        listed = self.pin_list()
        assigned = {p for p, a in self.pins(include_virtual=True).items() if a.signal}
        if self.get("Mcu.PinsNb") != str(len(listed)):
            problems.append(f"Mcu.PinsNb={self.get('Mcu.PinsNb')} but Mcu.PinN lists {len(listed)} pins")
        problems += [f"{p} has a Signal but is missing from Mcu.PinN" for p in sorted(assigned - set(listed))]
        problems += [f"{p} is in Mcu.PinN but has no Signal" for p in sorted(set(listed) - assigned)]
        return problems

    def _add_gpio_parameter(self, pin: str, param: str) -> None:
        key = f"{pin}.GPIOParameters"
        current = [p for p in (self.get(key) or "").split(",") if p]
        if param not in current:
            current.append(param)
        self.set(key, ",".join(current))

    def _remove_gpio_parameter(self, pin: str, param: str) -> None:
        key = f"{pin}.GPIOParameters"
        current = [p for p in (self.get(key) or "").split(",") if p and p != param]
        if current:
            self.set(key, ",".join(current))
        else:
            self.delete(key)

    def pin_list(self) -> List[str]:
        """Mcu.Pin0..N in INDEX order (the file string-sorts the keys, so
        Mcu.Pin9 sits after Mcu.Pin89 on disk)."""
        indexed = []
        for key in self.keys("Mcu.Pin"):
            suffix = key[len("Mcu.Pin"):]
            if suffix.isdigit():
                indexed.append((int(suffix), self.get(key)))
        return [name for _, name in sorted(indexed)]

    def _sync_pin_list(self) -> None:
        """Rebuild Mcu.Pin0..N and Mcu.PinsNb from the pins carrying a Signal.

        CubeMX lists physical pins in package-position order (ball A1, A2,
        ... then B1 ...) and virtual pins after them. This module has no
        package data, so it keeps the existing order for pins that stay
        assigned, drops the ones that lost their signal, and appends newly
        assigned pins - physical ones just before the first virtual pin,
        virtual ones at the end. CubeMX re-sorts on its next save; until
        then the diff is minimal.
        """
        assigned = self.pins(include_virtual=True)
        names = {p for p, a in assigned.items() if a.signal}
        kept = [p for p in self.pin_list() if p in names]
        new_physical = sorted(p for p in names - set(kept) if is_physical_pin(p))
        new_virtual = sorted(p for p in names - set(kept) if is_virtual_pin(p))
        first_virtual = next((i for i, p in enumerate(kept) if is_virtual_pin(p)), len(kept))
        ordered = kept[:first_virtual] + new_physical + kept[first_virtual:] + new_virtual
        for key in self.keys("Mcu.Pin"):
            if re.match(r"^Mcu\.Pin\d+$", key):
                self.delete(key)
        for i, name in enumerate(ordered):
            self.set(f"Mcu.Pin{i}", name)
        self.set("Mcu.PinsNb", str(len(ordered)))
