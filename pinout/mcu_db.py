"""Read a CubeMX MCU database XML - the authoritative pin table.

CubeMX ships one XML per part under `STM32CubeMX/db/mcu/`, named by the
RefName (`STM32G431C(6-8-B)Ux.xml`). It lists every physical pin with its
package position, its type (I/O, Power, Reset, MonoIO, ...) and every
alternate signal it can carry. The .ioc only records which signal was CHOSEN;
this is where the choices come from, and it is what a pinout-agnostic symbol
is generated from.

The schema uses a default XML namespace, which ElementTree bakes into every
tag name; tags are matched on their local name so a namespace change in a
future CubeMX does not break parsing.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class McuPin:
    name: str          # PA0, PF0-OSC_IN, VDD, NRST
    position: str      # package position: "12" on a QFN, "D17" on a BGA
    type: str          # I/O, Power, Reset, MonoIO, Boot, NC ...
    signals: List[str] = field(default_factory=list)

    @property
    def is_gpio(self) -> bool:
        return self.type in ("I/O", "MonoIO")


@dataclass
class Mcu:
    ref_name: str
    package: str
    family: str
    pins: List[McuPin] = field(default_factory=list)

    def pin(self, name: str) -> Optional[McuPin]:
        for p in self.pins:
            if p.name == name:
                return p
        return None

    def by_position(self) -> Dict[str, McuPin]:
        return {p.position: p for p in self.pins}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def load_mcu_xml(path) -> Mcu:
    root = ET.parse(str(Path(path))).getroot()
    if _local(root.tag) != "Mcu":
        raise ValueError(f"not a CubeMX MCU file: root is <{_local(root.tag)}>")
    mcu = Mcu(ref_name=root.get("RefName", ""), package=root.get("Package", ""),
              family=root.get("Family", ""))
    for el in root:
        if _local(el.tag) != "Pin":
            continue
        pin = McuPin(name=el.get("Name", ""), position=el.get("Position", ""),
                     type=el.get("Type", ""))
        for sig in el:
            if _local(sig.tag) == "Signal" and sig.get("Name"):
                pin.signals.append(sig.get("Name"))
        mcu.pins.append(pin)
    return mcu
