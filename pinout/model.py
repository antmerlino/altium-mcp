"""The canonical Pinout - the hinge between CubeMX and Altium.

One record per PHYSICAL pin of the MCU. The MCU database supplies what the pin
IS (position, type, every signal it could carry); the .ioc supplies what was
CHOSEN (signal, mode, label, locked); the designer supplies what nothing else
knows (a function name, a grouping, which symbol part, a net name). Those
designer columns are the reason this file exists: they survive regenerating
from a changed .ioc, and they are what a spreadsheet exposes for editing.

JSON is the primary on-disk form (diffs cleanly in git). XLSX export/import is
optional and needs openpyxl.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .ioc import IocFile
from .mcu_db import Mcu
from . import naming

# The designer-owned columns. Regeneration from sources never overwrites them.
OVERRIDE_FIELDS = ("function", "group", "part", "net")

XLSX_COLUMNS = ["Pkg #", "Pin", "Port", "Type", "Signal", "Mode", "Label", "Locked",
                "Function", "Group", "Part", "Net", "Available signals"]


@dataclass
class PinRecord:
    number: str
    name: str
    port: Optional[str]
    mcu_type: str
    signals: List[str] = field(default_factory=list)
    signal: Optional[str] = None
    mode: Optional[str] = None
    label: Optional[str] = None
    locked: bool = False
    function: Optional[str] = None
    group: Optional[str] = None
    part: Optional[int] = None
    net: Optional[str] = None

    @property
    def assigned(self) -> bool:
        return bool(self.signal)

    @property
    def effective_group(self) -> Optional[str]:
        return self.group or naming.peripheral_group(self.signal)

    @property
    def effective_function(self) -> Optional[str]:
        return self.function or self.label


@dataclass
class Pinout:
    mcu: str
    package: str
    board: str = ""
    prefix: str = ""
    pins: List[PinRecord] = field(default_factory=list)
    meta: Dict[str, str] = field(default_factory=dict)

    # ---- construction ----------------------------------------------------
    @classmethod
    def from_sources(cls, mcu: Mcu, ioc: Optional[IocFile] = None, board: str = "",
                     prefix: str = "") -> "Pinout":
        assigned = ioc.pins() if ioc else {}
        out = cls(mcu=mcu.ref_name, package=mcu.package, board=board, prefix=prefix)
        for p in sorted(mcu.pins, key=_position_key):
            a = assigned.get(p.name)
            out.pins.append(PinRecord(
                number=p.position, name=p.name, port=naming.port_of(p.name),
                mcu_type=p.type, signals=list(p.signals),
                signal=a.signal if a else None, mode=a.mode if a else None,
                label=a.label if a else None, locked=a.locked if a else False))
        if ioc:
            unknown = sorted(set(assigned) - {p.name for p in mcu.pins})
            if unknown:
                out.meta["ioc_pins_not_in_mcu"] = ",".join(unknown)
        return out

    def pin(self, name: str) -> Optional[PinRecord]:
        for p in self.pins:
            if p.name == name:
                return p
        return None

    def assigned(self) -> List[PinRecord]:
        return [p for p in self.pins if p.assigned]

    # ---- merging designer columns from a previous version -----------------
    def merge_overrides(self, previous: "Pinout") -> int:
        """Carry function/group/part/net from an older Pinout onto this one.

        Regenerating from a changed .ioc must not lose what the designer
        typed. Returns how many pins received something.
        """
        n = 0
        for old in previous.pins:
            cur = self.pin(old.name)
            if not cur:
                continue
            touched = False
            for f in OVERRIDE_FIELDS:
                v = getattr(old, f)
                if v not in (None, "") and getattr(cur, f) in (None, ""):
                    setattr(cur, f, v)
                    touched = True
            n += touched
        return n

    # ---- pushing back into CubeMX ----------------------------------------
    def apply_to_ioc(self, ioc: IocFile) -> int:
        """Write signal/mode/label/locked for every pin back into the .ioc.

        Pins with no signal are cleared. Returns the number of pins touched.
        The .ioc's non-pin content is untouched by construction (IocFile).
        """
        current = ioc.pins()
        n = 0
        for p in self.pins:
            was = current.get(p.name)
            if p.signal:
                same = (was and was.signal == p.signal and was.mode == p.mode
                        and was.label == p.label and was.locked == p.locked)
                if same:
                    continue
                ioc.set_pin(p.name, signal=p.signal, mode=p.mode or "",
                            label=p.label or "", locked=p.locked)
                n += 1
            elif was:
                ioc.clear_pin(p.name)
                n += 1
        return n

    # ---- JSON ---------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"mcu": self.mcu, "package": self.package, "board": self.board,
                "prefix": self.prefix, "meta": self.meta,
                "pins": [asdict(p) for p in self.pins]}

    @classmethod
    def from_dict(cls, d: dict) -> "Pinout":
        return cls(mcu=d["mcu"], package=d["package"], board=d.get("board", ""),
                   prefix=d.get("prefix", ""), meta=dict(d.get("meta", {})),
                   pins=[PinRecord(**p) for p in d["pins"]])

    def to_json(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")

    @classmethod
    def from_json(cls, path) -> "Pinout":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---- spreadsheet (optional) ---------------------------------------------
    def to_xlsx(self, path) -> None:
        openpyxl = _require_openpyxl()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Pinout"
        ws.append(XLSX_COLUMNS)
        for p in self.pins:
            ws.append([p.number, p.name, p.port or "", p.mcu_type, p.signal or "",
                       p.mode or "", p.label or "", "yes" if p.locked else "",
                       p.function or "", p.group or "",
                       p.part if p.part is not None else "", p.net or "",
                       ", ".join(p.signals)])
        meta = wb.create_sheet("Meta")
        for k, v in (("mcu", self.mcu), ("package", self.package), ("board", self.board),
                     ("prefix", self.prefix), *self.meta.items()):
            meta.append([k, v])
        wb.save(str(path))

    @classmethod
    def from_xlsx(cls, path) -> "Pinout":
        openpyxl = _require_openpyxl()
        wb = openpyxl.load_workbook(str(path), data_only=True)
        meta = {}
        if "Meta" in wb.sheetnames:
            for k, v in wb["Meta"].iter_rows(values_only=True):
                if k is not None:
                    meta[str(k)] = "" if v is None else str(v)
        out = cls(mcu=meta.pop("mcu", ""), package=meta.pop("package", ""),
                  board=meta.pop("board", ""), prefix=meta.pop("prefix", ""), meta=meta)
        ws = wb["Pinout"]
        rows = ws.iter_rows(values_only=True)
        header = [str(h) for h in next(rows)]
        idx = {h: i for i, h in enumerate(header)}

        def cell(row, col):
            v = row[idx[col]] if col in idx and idx[col] < len(row) else None
            return None if v in (None, "") else v

        for row in rows:
            if cell(row, "Pin") is None:
                continue
            part = cell(row, "Part")
            out.pins.append(PinRecord(
                number=str(cell(row, "Pkg #") or ""), name=str(cell(row, "Pin")),
                port=cell(row, "Port"), mcu_type=str(cell(row, "Type") or ""),
                signals=[s.strip() for s in str(cell(row, "Available signals") or "").split(",")
                         if s.strip()],
                signal=cell(row, "Signal"), mode=cell(row, "Mode"), label=cell(row, "Label"),
                locked=str(cell(row, "Locked") or "").lower() in ("yes", "true", "1"),
                function=cell(row, "Function"), group=cell(row, "Group"),
                part=int(part) if part is not None else None, net=cell(row, "Net")))
        return out


def _position_key(p):
    """Sort QFN positions numerically, BGA positions (D17) row-then-column."""
    s = p.position
    if s.isdigit():
        return (0, int(s), "")
    head = s.rstrip("0123456789")
    tail = s[len(head):]
    return (1, int(tail) if tail.isdigit() else 0, head)


def _require_openpyxl():
    try:
        import openpyxl  # noqa: F401
    except ImportError as e:
        raise RuntimeError("spreadsheet import/export needs openpyxl "
                           "(pip install openpyxl); JSON works without it") from e
    return openpyxl
