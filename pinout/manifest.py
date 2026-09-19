"""The board manifest: one JSON that ties a board's sources together.

    {
      "name": "X7 Flight Computer", "prefix": "FC",
      "ioc": "X:/.../Flight Computer.ioc",
      "mcu_xml": "C:/Program Files/.../STM32H753IIKx.xml",
      "debug_style": "SWDIO",                       # or "SWD_SWDIO" (how debug pins are spelled in nets)
      "sheet": {
        "workbook": "<spreadsheet id or url>",
        "review_tab": "Reconcile",
        "pins":    {"tab": "Pin Definitions", "header_row": 3, "key": "Pin name (function after reset)",
                    "columns": {"signal": "CubeMX Peripheral Function", "cube_label": "CubeMX Label", ...}},
        "signals": {"tab": "Signal Definitions", "header_row": 1, "key": "Assigned Pin",
                    "columns": {"peripheral": "Peripheral", "peripheral_pin": "Peripheral Pin", ...}}
      },
      "altium": {"project_dir": "D:/Altium/X2 Control Board", "symbol": "EMBEDDED_IC_STM32H753IIK6_X2_CB",
                 "sheets": ["FC_STM32H753IIKx_Comms", ...], "library": "D:/Altium/VergeLibrary/Libraries/VergeLibrary.SchLib"}
    }

Column roles the sheet adapter understands (any subset):
    signal, cube_label, label_override, function, net, net_generated, net_override,
    group, peripheral, peripheral_pin, direction, other_end, postfix,
    power_domain, software_name, notes, checked
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

ROLES = ("ball", "signal", "cube_label", "label_override", "function", "net", "net_generated", "net_override", "group",
         "peripheral", "peripheral_pin", "direction", "other_end", "postfix", "power_domain", "software_name",
         "notes", "checked")


@dataclass
class TableSpec:
    tab: Union[str, int]
    key: str
    columns: Dict[str, str]
    header_row: Union[str, int] = "auto"

    def __post_init__(self):
        bad = [r for r in self.columns if r not in ROLES]
        if bad:
            raise ValueError(f"unknown column roles {bad}; known: {ROLES}")


@dataclass
class SheetSpec:
    workbook: str
    pins: Optional[TableSpec] = None
    signals: Optional[TableSpec] = None
    review_tab: str = "Reconcile"


@dataclass
class AltiumSpec:
    project_dir: str
    symbol: str
    sheets: List[str]
    library: Optional[str] = None


@dataclass
class Board:
    name: str
    prefix: str
    ioc: str
    mcu_xml: str
    sheet: Optional[SheetSpec] = None
    altium: Optional[AltiumSpec] = None
    debug_style: str = "SWDIO"
    options: Dict[str, object] = field(default_factory=dict)
    path: Optional[str] = None


def load(path: Union[str, Path]) -> Board:
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    sheet = None
    if raw.get("sheet"):
        s = raw["sheet"]
        sheet = SheetSpec(s["workbook"],
                          TableSpec(**s["pins"]) if s.get("pins") else None,
                          TableSpec(**s["signals"]) if s.get("signals") else None,
                          s.get("review_tab", "Reconcile"))
    altium = AltiumSpec(**raw["altium"]) if raw.get("altium") else None
    return Board(raw["name"], raw["prefix"], raw["ioc"], raw["mcu_xml"], sheet, altium,
                 raw.get("debug_style", "SWDIO"), raw.get("options", {}), str(p))
