"""Turn a Pinout into an Altium symbol spec.

Output is the exact pipe-string format create_schematic_symbol consumes:

    pins      "pin_number|pin_name|eElectric*|eRotate*|x|y|owner_part|length"
    graphics  "rectangle|part|width|solid|x1|y1|x2|y2"
              "line|part|width|x1|y1|x2|y2"
              "polyline|part|width|x1|y1|x2|y2|..."
              "label|part|x|y|text"

Coordinates are mils, y grows UP (so the pin column runs from 0 downward into
negative y, exactly as the measured symbols do). Two strategies, both copied
from real symbols in the house library:

  port_split        one part per GPIO port, all pins on the left, open-bracket
                    body; power/reset/oscillator gathered into a final part.
                    Pinout-agnostic - built from the MCU database alone.
                    (EMBEDDED_IC_STM32G431C8U6TR)

  functional_split  pins clustered under peripheral labels (SWD, SPI1, USART2)
                    in two columns with a divider, power part last, electrical
                    types derived per pin. Built from the .ioc.
                    (EMBEDDED_IC_STM32H753IIK6_X57)
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import naming
from .model import PinRecord, Pinout

PITCH = 100
LEFT, RIGHT = "eRotate180", "eRotate0"   # left-facing pin sits on the left edge


@dataclass
class SymbolSpec:
    name: str
    description: str
    part_count: int
    pins: List[str] = field(default_factory=list)
    graphics: List[str] = field(default_factory=list)

    def to_tool_args(self) -> dict:
        return {"symbol_name": self.name, "description": self.description,
                "pins": list(self.pins), "part_count": self.part_count,
                "graphics": list(self.graphics)}


def parse_pin_spec(s: str) -> dict:
    f = s.split("|")
    if len(f) < 6:
        raise ValueError(f"malformed pin spec: {s!r}")
    return {"number": f[0], "name": f[1], "type": f[2], "orientation": f[3],
            "x": int(f[4]), "y": int(f[5]),
            "part": int(f[6]) if len(f) > 6 else 1,
            "length": int(f[7]) if len(f) > 7 else 300}


# ---- shared helpers ---------------------------------------------------------

def _is_power_part_pin(p: PinRecord) -> bool:
    """Supplies, reset, boot and oscillator pins live on the power part - even
    when they are port pins (PF0-OSC_IN, PG10-nRST), as the house symbols do."""
    u = p.name.upper()
    if p.port is None:
        return True
    return "-OSC" in u or "NRST" in u or p.mcu_type in ("Reset", "Boot")


def _is_control(p: PinRecord) -> bool:
    u = p.name.upper()
    return p.mcu_type in ("Reset", "Boot") or "OSC" in u or "NRST" in u or "BOOT" in u


_SUPPLY_ORDER = ("VBAT", "VDD", "VDDA", "VDDUSB", "VREF+", "VREF-", "VSSA", "VSS", "NC")


def _supply_rank(p: PinRecord):
    u = p.name.upper()
    for i, k in enumerate(_SUPPLY_ORDER):
        if u.startswith(k):
            return (i, u, _num(p.number))
    return (len(_SUPPLY_ORDER), u, _num(p.number))


def _num(s: str) -> int:
    d = "".join(ch for ch in s if ch.isdigit())
    return int(d) if d else 0


def _pin_sort_key(p: PinRecord):
    """PA0, PA1, ... PA13 numerically, not as strings."""
    base = naming.base_pin(p.name)
    return (base[:2], _num(base[2:]), p.name)


def _pin_line(p: PinRecord, orient: str, x: int, y: int, part: int, length: int,
              style: str) -> str:
    return "|".join([p.number, naming.display_name(p.name, p.signal, style),
                     naming.electrical_type(p.name, p.mcu_type, p.signal),
                     orient, str(x), str(y), str(part), str(length)])


def _power_part(pins: List[PinRecord], part: int, pin_length: int, body_left: int,
                body_right: int, style: str):
    """Control pins down the left, supplies down the right, rectangle body."""
    left = sorted([p for p in pins if _is_control(p)], key=_pin_sort_key)
    right = sorted([p for p in pins if not _is_control(p)], key=_supply_rank)
    out, gfx = [], []
    for i, p in enumerate(left):
        out.append(_pin_line(p, LEFT, body_left, -PITCH * i, part, pin_length, style))
    for i, p in enumerate(right):
        out.append(_pin_line(p, RIGHT, body_right, -PITCH * i, part, pin_length, style))
    bottom = -PITCH * max(len(left), len(right))
    gfx.append(f"rectangle|{part}|1|1|{body_left}|{bottom}|{body_right}|{PITCH}")
    return out, gfx


# ---- strategy 1: port split ----------------------------------------------------

def port_split(pinout: Pinout, pin_length: int = 200, mpn: Optional[str] = None,
               board: Optional[str] = None, description: Optional[str] = None,
               name_style: str = "plain") -> SymbolSpec:
    body_left, bracket_right = 200, 600
    ports: Dict[str, List[PinRecord]] = defaultdict(list)
    power: List[PinRecord] = []
    for p in pinout.pins:
        (power if _is_power_part_pin(p) else ports[p.port]).append(p)

    spec = SymbolSpec(name=naming.symbol_name(mpn or pinout.meta.get("mpn", pinout.mcu),
                                              board or pinout.board or None),
                      description=description or pinout.meta.get(
                          "description", f"EMBEDDED IC MCU {pinout.package}"),
                      part_count=len(ports) + 1)
    part = 0
    for port in sorted(ports):
        part += 1
        pins = sorted(ports[port], key=_pin_sort_key)
        for i, p in enumerate(pins):
            spec.pins.append(_pin_line(p, LEFT, body_left, -PITCH * i, part, pin_length,
                                       name_style))
        bottom = -PITCH * len(pins)
        # open bracket: top, left edge, bottom - no right edge, as measured
        spec.graphics += [
            f"line|{part}|1|{body_left}|{PITCH}|{bracket_right}|{PITCH}",
            f"line|{part}|1|{body_left}|{bottom}|{body_left}|{PITCH}",
            f"line|{part}|1|{bracket_right}|{bottom}|{body_left}|{bottom}",
        ]
    part += 1
    pins, gfx = _power_part(power, part, pin_length, body_left, 1500, name_style)
    spec.pins += pins
    spec.graphics += gfx
    return spec


# ---- strategy 2: functional split ---------------------------------------------

def functional_split(pinout: Pinout, pin_length: int = 300, max_pins_per_part: int = 48,
                     mpn: Optional[str] = None, board: Optional[str] = None,
                     description: Optional[str] = None,
                     name_style: str = "debug") -> SymbolSpec:
    body_left, body_right = 300, 2400
    divider_x = ((body_left + body_right) // 2) // PITCH * PITCH
    label_inset = 400

    groups: Dict[str, List[PinRecord]] = defaultdict(list)
    power: List[PinRecord] = []
    for p in pinout.pins:
        if _is_power_part_pin(p):
            power.append(p)
        else:
            groups[p.effective_group or "UNUSED"].append(p)
    for g in groups.values():
        g.sort(key=_pin_sort_key)

    # Pack groups into parts: honour an explicit pin.part where every pin of
    # the group agrees, otherwise fill parts in group order up to the cap.
    ordered = sorted(groups, key=lambda g: (g == "UNUSED", g))
    parts: Dict[int, List[str]] = defaultdict(list)
    part, used = 1, 0
    for g in ordered:
        forced = {p.part for p in groups[g] if p.part is not None}
        if len(forced) == 1:
            parts[forced.pop()].append(g)
            continue
        if used and used + len(groups[g]) > max_pins_per_part:
            part, used = part + 1, 0
        parts[part].append(g)
        used += len(groups[g])
    power_part = (max(parts) if parts else 0) + 1

    spec = SymbolSpec(name=naming.symbol_name(mpn or pinout.meta.get("mpn", pinout.mcu),
                                              board or pinout.board or None),
                      description=description or pinout.meta.get(
                          "description", f"EMBEDDED IC MCU {pinout.package}"),
                      part_count=power_part)

    for part in sorted(parts):
        # two columns, each group whole, next group to the shorter column
        cols = {LEFT: 0, RIGHT: 0}       # rows consumed per column
        for g in parts[part]:
            side = LEFT if cols[LEFT] <= cols[RIGHT] else RIGHT
            x = body_left if side == LEFT else body_right
            lx = x + label_inset if side == LEFT else x - label_inset
            top = -PITCH * cols[side]
            spec.graphics.append(f"label|{part}|{lx}|{top}|{g}")
            for i, p in enumerate(groups[g]):
                spec.pins.append(_pin_line(p, side, x, top - PITCH * (i + 1), part,
                                           pin_length, name_style))
            last = top - PITCH * len(groups[g])
            x0, x1 = (body_left, divider_x) if side == LEFT else (divider_x, body_right)
            spec.graphics.append(f"line|{part}|1|{x0}|{last - PITCH}|{x1}|{last - PITCH}")
            cols[side] += len(groups[g]) + 2     # pins + label row + gap row
        bottom = -PITCH * max(cols.values())
        spec.graphics.append(f"rectangle|{part}|1|1|{body_left}|{bottom}|{body_right}|{PITCH}")
        spec.graphics.append(f"polyline|{part}|1|{divider_x}|{PITCH}|{divider_x}|{bottom}")

    pins, gfx = _power_part(power, power_part, pin_length, body_left, body_right, name_style)
    spec.pins += pins
    spec.graphics += gfx
    return spec
