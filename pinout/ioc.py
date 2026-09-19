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

PHYSICAL_PIN = re.compile(r"^P[A-K]\d{1,2}(?:-[A-Za-z0-9_+\-]+)?$")
VIRTUAL_PREFIX = "VP_"
PIN_KEY = re.compile(r"^([^.=\s]+)\.(Signal|Mode|GPIO_Label|Locked|GPIOParameters)$")


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
        return cls(Path(path).read_text(encoding="utf-8"))

    def text(self) -> str:
        body = self._eol.join(self._lines)
        return body + self._eol if self._trailing_newline else body

    def save(self, path) -> None:
        Path(path).write_text(self.text(), encoding="utf-8", newline="")

    # ---- key/value level -------------------------------------------------
    @staticmethod
    def _split(line: str):
        if not line or line.startswith("#") or "=" not in line:
            return None
        key, _, value = line.partition("=")
        if " " in key:
            return None
        return key, value

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
        line = f"{key}={value}"
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
        for field in ("Signal", "Mode", "GPIO_Label", "Locked", "GPIOParameters"):
            self.delete(f"{pin}.{field}")
        for key in self.keys(f"{pin}.GPIO_"):
            self.delete(key)
        self._sync_pin_list()

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

    def _sync_pin_list(self) -> None:
        """Rebuild Mcu.Pin0..N and Mcu.PinsNb from the pins carrying a Signal.

        CubeMX counts virtual pins here too, so they are kept. Physical pins
        are listed first, sorted, then virtual ones.
        """
        assigned = self.pins(include_virtual=True)
        names = [p for p, a in assigned.items() if a.signal]
        physical = sorted(p for p in names if is_physical_pin(p))
        virtual = sorted(p for p in names if is_virtual_pin(p))
        ordered = physical + virtual
        for key in self.keys("Mcu.Pin"):
            if re.match(r"^Mcu\.Pin\d+$", key):
                self.delete(key)
        for i, name in enumerate(ordered):
            self.set(f"Mcu.Pin{i}", name)
        self.set("Mcu.PinsNb", str(len(ordered)))
