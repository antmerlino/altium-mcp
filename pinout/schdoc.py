"""Read an Altium schematic sheet (.SchDoc) OFFLINE, without Altium.

A .SchDoc is an OLE compound file whose `FileHeader` stream is a sequence of
records: a 4-byte little-endian header (low 24 bits = length, high byte =
0 for a pipe-delimited ASCII record, 1 for a binary one) followed by the
record body. ASCII records look like

    |RECORD=25|IndexInSheet=70|Location.X=500|Location.Y=340|Text=FC_PA14_SWCLK|UniqueID=...

Coordinates are in DXP units of 10 mil (`Location.X=680` is 6.8 in);
fractional parts live in `Location.X_Frac`. Fields equal to their default
(0, False, empty) are omitted.

A placed component (RECORD=1) owns its primitives through `OwnerIndex`, the
component's position in the record list not counting the sheet header, and
it carries EVERY part's primitives (pins RECORD=2, labels RECORD=4,
polylines RECORD=6, lines RECORD=13, rectangles RECORD=14, each tagged
`OwnerPartId`), with `CurrentPartId` selecting the part drawn on this
sheet. So one placed instance is enough to reconstruct the whole library
symbol in the shape `get_symbol_primitives` returns - which is what
`component_dump` does - and the wires (RECORD=27) and net labels
(RECORD=25) around the drawn part give pin -> net (`pin_nets`).

This was measured on AD Develop sheets (2026-09). It reads files only;
nothing here talks to Altium.

Records measured:
    1  component   LibReference, PartCount, CurrentPartId, Location.X/Y, Orientation
    2  pin         OwnerIndex, OwnerPartId, Electrical, PinConglomerate (bits 0-1 =
                   orientation 0 right/1 up/2 left/3 down), PinLength, Location.X/Y,
                   Name, Designator (= pin number)
    4  label       OwnerIndex, OwnerPartId, Location.X/Y, Text, Color, FontID
    6  polyline    OwnerIndex, OwnerPartId, LocationCount, X1..Xn, Y1..Yn, LineWidth, Color
    13 line        OwnerIndex, OwnerPartId, Location.X/Y, Corner.X/Y, LineWidth, Color
    14 rectangle   OwnerIndex, OwnerPartId, Location.X/Y, Corner.X/Y, LineWidth, Color,
                   AreaColor, IsSolid
    17 power port  Text, Location.X/Y
    25 net label   Text, Location.X/Y
    27 wire        LocationCount, X1..Xn, Y1..Yn
    34 designator  OwnerIndex, Text
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

UNIT_MILS = 10          # one DXP unit is 10 mil


def parse_records(data: bytes) -> List[dict]:
    """The FileHeader stream as a list of dicts (binary records become
    {"RECORD": "binary"}). Index 0 is the sheet header."""
    out: List[dict] = []
    i = 0
    while i + 4 <= len(data):
        n = struct.unpack_from("<I", data, i)[0]
        length, kind = n & 0xFFFFFF, n >> 24
        body = data[i + 4:i + 4 + length]
        i += 4 + length
        if kind != 0:
            out.append({"RECORD": "binary", "_length": length})
            continue
        kv: dict = {}
        for part in body.rstrip(b"\x00").decode("latin-1").split("|"):
            if "=" in part:
                k, _, v = part.partition("=")
                kv[k] = v
        out.append(kv)
    return out


def read_records(path, streams=("FileHeader", "Additional")) -> List[dict]:
    """Every record in the sheet, in stream order.

    A .SchDoc keeps two record streams. "FileHeader" holds the classic
    objects (components, pins, wires, labels, ports, parameter sets...).
    "Additional" holds the harness family - harness connectors (RECORD 215),
    harness entries (216), harness type labels (217), signal harnesses (218) -
    and blankets (225). Reading only FileHeader silently reports a sheet
    with no harnesses at all; that mistake cost a whole rewire pass once.

    Records from the second stream are tagged with "_stream" so an owner
    index (OwnerIndex / OwnerIndexAdditionalList) can be resolved within the
    right list; the second stream's own header record is dropped.
    """
    import olefile                      # only needed for real files
    ole = olefile.OleFileIO(str(path))
    out: List[dict] = []
    try:
        for n, name in enumerate(streams):
            if not ole.exists(name):
                continue
            recs = parse_records(ole.openstream(name).read())
            if n:
                recs = [r for r in recs if r.get("RECORD") is not None]
            for r in recs:
                r["_stream"] = name
            out.extend(recs)
    finally:
        ole.close()
    return out


def _num(r: dict, key: str, default: int = 0) -> int:
    """A coordinate in mils, including the fractional field Altium adds."""
    whole = int(r.get(key, default) or 0)
    frac = int(r.get(key + "_Frac", 0) or 0)
    return whole * UNIT_MILS + round(frac * UNIT_MILS / 100000)


def _int(r: dict, key: str, default: int = 0) -> int:
    v = r.get(key)
    return default if v in (None, "") else int(v)


def _bool(r: dict, key: str) -> bool:
    return r.get(key) == "T"


def _children(records: List[dict], comp_index: int) -> List[dict]:
    """Primitives owned by the component at list index comp_index. OwnerIndex
    counts records without the sheet header, hence the minus one."""
    owner = str(comp_index - 1)
    return [r for r in records if r.get("OwnerIndex") == owner]


def find_components(records: List[dict], lib_reference: Optional[str] = None) -> List[int]:
    """List indices of RECORD=1 components, optionally by LibReference."""
    return [i for i, r in enumerate(records)
            if r.get("RECORD") == "1" and (lib_reference is None or r.get("LibReference") == lib_reference)]


def designator_of(records: List[dict], comp_index: int) -> Optional[str]:
    for r in _children(records, comp_index):
        if r.get("RECORD") == "34":
            return r.get("Text")
    return None


def component_dump(records: List[dict], comp_index: int) -> dict:
    """The placed component's symbol in get_symbol_primitives shape: every
    part, coordinates in mils, ABSOLUTE sheet position (the audit only needs
    relative geometry). Colours are Altium TColor ints as stored."""
    comp = records[comp_index]
    prims: List[dict] = []
    for r in _children(records, comp_index):
        kind, part = r.get("RECORD"), _int(r, "OwnerPartId", 1)
        if kind == "2":
            cong = _int(r, "PinConglomerate")
            prims.append({"type": "pin", "pin_number": r.get("Designator", ""), "pin_name": r.get("Name", ""),
                          "electrical": _int(r, "Electrical"), "orientation": cong & 3,
                          "x": _num(r, "Location.X"), "y": _num(r, "Location.Y"),
                          "length": _num(r, "PinLength"), "owner_part_id": part,
                          "show_name": bool(cong & 0x08), "show_designator": bool(cong & 0x10),
                          "color": _int(r, "Color")})
        elif kind == "4":
            prims.append({"type": "label", "text": r.get("Text", ""), "x": _num(r, "Location.X"),
                          "y": _num(r, "Location.Y"), "owner_part_id": part,
                          "color": _int(r, "Color"), "font_id": _int(r, "FontID", 1)})
        elif kind == "14":
            prims.append({"type": "rectangle", "x1": _num(r, "Location.X"), "y1": _num(r, "Location.Y"),
                          "x2": _num(r, "Corner.X"), "y2": _num(r, "Corner.Y"),
                          "line_width": _int(r, "LineWidth"), "is_solid": _bool(r, "IsSolid"),
                          "owner_part_id": part, "color": _int(r, "Color"),
                          "area_color": _int(r, "AreaColor")})
        elif kind == "13":
            prims.append({"type": "line", "x1": _num(r, "Location.X"), "y1": _num(r, "Location.Y"),
                          "x2": _num(r, "Corner.X"), "y2": _num(r, "Corner.Y"),
                          "line_width": _int(r, "LineWidth"), "owner_part_id": part,
                          "color": _int(r, "Color")})
        elif kind == "6":
            n = _int(r, "LocationCount")
            prims.append({"type": "polyline", "line_width": _int(r, "LineWidth"), "owner_part_id": part,
                          "vertices": [{"x": _num(r, f"X{i}"), "y": _num(r, f"Y{i}")} for i in range(1, n + 1)],
                          "color": _int(r, "Color")})
    return {"symbol_name": comp.get("LibReference"), "description": comp.get("ComponentDescription", ""),
            "part_count": _int(comp, "PartCount", 1), "current_part_id": _int(comp, "CurrentPartId", 1),
            "designator": designator_of(records, comp_index),
            "source": "schdoc", "primitives": prims}


# ---- connectivity --------------------------------------------------------------

_DIR = {0: (1, 0), 1: (0, 1), 2: (-1, 0), 3: (0, -1)}


def pin_connect_point(pin: dict) -> Tuple[int, int]:
    dx, dy = _DIR[int(pin["orientation"]) & 3]
    return pin["x"] + dx * pin["length"], pin["y"] + dy * pin["length"]


def _wire_segments(records: List[dict]) -> List[Tuple[Tuple[int, int], Tuple[int, int]]]:
    segs = []
    for r in records:
        if r.get("RECORD") != "27":
            continue
        n = _int(r, "LocationCount")
        pts = [(_num(r, f"X{i}"), _num(r, f"Y{i}")) for i in range(1, n + 1)]
        segs += list(zip(pts, pts[1:]))
    return segs


def _on_segment(p: Tuple[int, int], a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    (px, py), (ax, ay), (bx, by) = p, a, b
    if min(ax, bx) - 1 <= px <= max(ax, bx) + 1 and min(ay, by) - 1 <= py <= max(ay, by) + 1:
        return (bx - ax) * (py - ay) == (by - ay) * (px - ax)     # collinear
    return False


class _Nets:
    """Union-find over wire endpoints; a point on the middle of a segment
    joins that segment's net too (Altium connects there)."""

    def __init__(self, segs):
        self.segs = segs
        self.parent: Dict[Tuple[int, int], Tuple[int, int]] = {}
        for a, b in segs:
            self._union(a, b)
        # T-junctions: an endpoint lying on another segment
        for a, b in segs:
            for p in (a, b):
                for c, d in segs:
                    if (c, d) != (a, b) and _on_segment(p, c, d):
                        self._union(p, c)

    def _find(self, p):
        self.parent.setdefault(p, p)
        while self.parent[p] != p:
            self.parent[p] = self.parent[self.parent[p]]
            p = self.parent[p]
        return p

    def _union(self, a, b):
        ra, rb = self._find(a), self._find(b)
        if ra != rb:
            self.parent[ra] = rb

    def net_of(self, p: Tuple[int, int]) -> Optional[Tuple[int, int]]:
        if p in self.parent:
            return self._find(p)
        for a, b in self.segs:
            if _on_segment(p, a, b):
                return self._find(a)
        return None


def pin_nets(records: List[dict], comp_index: int) -> Dict[str, dict]:
    """For the part DRAWN on this sheet: pin number -> {name, nets: [labels],
    power: [power port texts], direct: label placed on the pin end}."""
    dump = component_dump(records, comp_index)
    cur = dump["current_part_id"]
    nets = _Nets(_wire_segments(records))
    labels_at: Dict[Optional[Tuple[int, int]], Set[str]] = {}
    direct: Dict[Tuple[int, int], Set[str]] = {}
    for r in records:
        if r.get("RECORD") in ("25", "17") and r.get("Text"):
            p = (_num(r, "Location.X"), _num(r, "Location.Y"))
            key = ("pwr:" if r["RECORD"] == "17" else "") + r["Text"]
            root = nets.net_of(p)
            if root is not None:
                labels_at.setdefault(root, set()).add(key)
            direct.setdefault(p, set()).add(key)
    out: Dict[str, dict] = {}
    for p in dump["primitives"]:
        if p["type"] != "pin" or p["owner_part_id"] != cur:
            continue
        cp = pin_connect_point(p)
        root = nets.net_of(cp)
        found = set(labels_at.get(root, set())) if root is not None else set()
        found |= direct.get(cp, set())
        out[p["pin_number"]] = {"name": p["pin_name"], "part": cur,
                                "nets": sorted(k for k in found if not k.startswith("pwr:")),
                                "power": sorted(k[4:] for k in found if k.startswith("pwr:")),
                                "wired": root is not None}
    return out
