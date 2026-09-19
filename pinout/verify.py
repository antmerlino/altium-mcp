"""Compare a SymbolSpec with what Altium actually holds.

After create_schematic_symbol runs, get_symbol_primitives dumps the symbol
back: a flat `primitives` list of pins, rectangles, lines, polylines and
labels with integer enum values and mil coordinates. This module normalises
both sides to the same shape and reports exactly what differs.

This is the step that makes generation trustworthy. A tool reporting success
once created a component Altium did not draw; only reading the result back
showed it. Nothing generated is believed until it has been compared here.

The dump shape (measured on AD Develop):
    pin        pin_number, pin_name, electrical (int), orientation (int),
               x, y, length, owner_part_id, show_name, show_designator
    rectangle  x1, y1, x2, y2, line_width, is_solid, owner_part_id
    line       x1, y1, x2, y2, line_width, owner_part_id
    polyline   vertices [{x, y}, ...], line_width, owner_part_id
    label      text, x, y, owner_part_id
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from .layout import SymbolSpec, parse_pin_spec

ELECTRICAL = {"eElectricInput": 0, "eElectricIO": 1, "eElectricOutput": 2,
              "eElectricOpenCollector": 3, "eElectricPassive": 4, "eElectricHiZ": 5,
              "eElectricOpenEmitter": 6, "eElectricPower": 7}
ORIENT = {"eRotate0": 0, "eRotate90": 1, "eRotate180": 2, "eRotate270": 3}
ELECTRICAL_NAME = {v: k for k, v in ELECTRICAL.items()}
ORIENT_NAME = {v: k for k, v in ORIENT.items()}

PinKey = Tuple[str, str, int, int, int, int, int, int]   # number,name,elec,orient,x,y,part,len


@dataclass
class Normalized:
    pins: Set[PinKey] = field(default_factory=set)
    rects: Set[tuple] = field(default_factory=set)
    lines: Set[tuple] = field(default_factory=set)
    polylines: Set[tuple] = field(default_factory=set)
    labels: Set[tuple] = field(default_factory=set)

    @property
    def graphics(self) -> Set[tuple]:
        return ({("rectangle",) + r for r in self.rects} | {("line",) + l for l in self.lines}
                | {("polyline",) + p for p in self.polylines}
                | {("label",) + l for l in self.labels})


def spec_as_dump(spec: SymbolSpec) -> dict:
    """Synthesise the get_symbol_primitives shape from a spec - what Altium
    SHOULD return if it applied the spec faithfully."""
    prims: List[dict] = []
    for raw in spec.pins:
        p = parse_pin_spec(raw)
        prims.append({"type": "pin", "pin_number": p["number"], "pin_name": p["name"],
                      "electrical": ELECTRICAL[p["type"]], "orientation": ORIENT[p["orientation"]],
                      "x": p["x"], "y": p["y"], "length": p["length"],
                      "owner_part_id": p["part"], "show_name": True, "show_designator": True})
    for raw in spec.graphics:
        f = raw.split("|")
        part = int(f[1])
        if f[0] == "rectangle":
            prims.append({"type": "rectangle", "x1": int(f[4]), "y1": int(f[5]), "x2": int(f[6]),
                          "y2": int(f[7]), "line_width": int(f[2]), "is_solid": f[3] == "1",
                          "owner_part_id": part})
        elif f[0] == "line":
            prims.append({"type": "line", "x1": int(f[3]), "y1": int(f[4]), "x2": int(f[5]),
                          "y2": int(f[6]), "line_width": int(f[2]), "owner_part_id": part})
        elif f[0] == "polyline":
            v = list(map(int, f[3:]))
            prims.append({"type": "polyline", "line_width": int(f[2]), "owner_part_id": part,
                          "vertices": [{"x": v[i], "y": v[i + 1]} for i in range(0, len(v), 2)]})
        elif f[0] == "label":
            prims.append({"type": "label", "text": f[4], "x": int(f[2]), "y": int(f[3]),
                          "owner_part_id": part})
    return {"symbol_name": spec.name, "description": spec.description,
            "part_count": spec.part_count, "primitives": prims}


def _pt(p) -> Tuple[int, int]:
    return (int(round(p["x"])), int(round(p["y"])))


def normalize(dump: dict) -> Normalized:
    n = Normalized()
    for p in dump.get("primitives", []):
        kind, part = p.get("type"), int(p.get("owner_part_id", 1))
        if kind == "pin":
            n.pins.add((str(p["pin_number"]), str(p["pin_name"]), int(p["electrical"]),
                        int(p["orientation"]), int(round(p["x"])), int(round(p["y"])), part,
                        int(round(p.get("length", 0)))))
        elif kind == "rectangle":
            xs, ys = sorted((int(round(p["x1"])), int(round(p["x2"])))), \
                     sorted((int(round(p["y1"])), int(round(p["y2"]))))
            n.rects.add((part, xs[0], ys[0], xs[1], ys[1]))
        elif kind == "line":
            a = (int(round(p["x1"])), int(round(p["y1"])))
            b = (int(round(p["x2"])), int(round(p["y2"])))
            n.lines.add((part,) + tuple(sorted((a, b))))
        elif kind == "polyline":
            v = tuple(_pt(q) for q in p.get("vertices", []))
            n.polylines.add((part,) + min(v, v[::-1]))   # direction-agnostic
        elif kind == "label":
            n.labels.add((part, int(round(p["x"])), int(round(p["y"])), str(p["text"])))
    return n


def _pin_dict(k: PinKey) -> dict:
    return {"pin_number": k[0], "pin_name": k[1], "electrical": ELECTRICAL_NAME.get(k[2], k[2]),
            "orientation": ORIENT_NAME.get(k[3], k[3]), "x": k[4], "y": k[5], "part": k[6],
            "length": k[7]}


def compare(spec: SymbolSpec, dump: dict) -> dict:
    want, have = normalize(spec_as_dump(spec)), normalize(dump)

    missing, unexpected = want.pins - have.pins, have.pins - want.pins
    # a pin present on both sides by number but with different attributes is a
    # CHANGE, which is far more useful to read than a missing+unexpected pair
    by_num_w = {k[0]: k for k in missing}
    by_num_h: Dict[str, List[PinKey]] = {}
    for k in unexpected:
        by_num_h.setdefault(k[0], []).append(k)
    changed = []
    for num, w in sorted(by_num_w.items()):
        if num in by_num_h and len(by_num_h[num]) == 1:
            h = by_num_h[num][0]
            diff = {f: (a, b) for f, a, b in zip(
                ("pin_number", "pin_name", "electrical", "orientation", "x", "y", "part", "length"),
                w, h) if a != b}
            changed.append({"pin_number": num, "spec": _pin_dict(w), "altium": _pin_dict(h),
                            "differs_in": sorted(diff)})
            missing.discard(w)
            unexpected.discard(h)

    g_missing = sorted(want.graphics - have.graphics)
    g_unexpected = sorted(have.graphics - want.graphics)
    name_same = dump.get("symbol_name") == spec.name
    parts_same = int(dump.get("part_count", -1)) == spec.part_count
    match = (name_same and parts_same and not missing and not unexpected and not changed
             and not g_missing and not g_unexpected)
    return {
        "match": match,
        "symbol_name": {"spec": spec.name, "altium": dump.get("symbol_name"), "same": name_same},
        "part_count": {"spec": spec.part_count, "altium": dump.get("part_count"),
                       "same": parts_same},
        "pins": {"missing_in_altium": [_pin_dict(k) for k in sorted(missing)],
                 "unexpected_in_altium": [_pin_dict(k) for k in sorted(unexpected)],
                 "changed": changed},
        "graphics": {"missing_in_altium": [list(g) for g in g_missing],
                     "unexpected_in_altium": [list(g) for g in g_unexpected]},
        "summary": {"spec_pins": len(want.pins), "altium_pins": len(have.pins),
                    "pins_identical": len(want.pins & have.pins),
                    "spec_graphics": len(want.graphics), "altium_graphics": len(have.graphics),
                    "graphics_identical": len(want.graphics & have.graphics)},
    }
