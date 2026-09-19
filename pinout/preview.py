"""Render a SymbolSpec to SVG - inspect the layout BEFORE touching Altium.

Every Altium call is slow, focus-stealing and can wedge the executor. Most
layout mistakes - a group in the wrong column, a pin off-grid, labels
colliding - are visible in a plain drawing. So the spec is drawn here first,
in the same mil coordinates the symbol will use (y up, like Altium; the SVG
just flips it).
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from .layout import SymbolSpec, parse_pin_spec

SCALE = 0.3          # px per mil
MARGIN = 400         # mils around the drawing
FONT = 9             # px


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def render_part_svg(spec: SymbolSpec, part: int) -> str:
    pins = [parse_pin_spec(p) for p in spec.pins]
    pins = [p for p in pins if p["part"] == part]
    gfx = [g.split("|") for g in spec.graphics if int(g.split("|")[1]) == part]

    xs, ys = [], []
    for p in pins:
        far = p["x"] - p["length"] if p["orientation"] == "eRotate180" else p["x"] + p["length"]
        xs += [p["x"], far]
        ys.append(p["y"])
    for g in gfx:
        kind = g[0]
        if kind == "rectangle":
            xs += [int(g[4]), int(g[6])]; ys += [int(g[5]), int(g[7])]
        elif kind == "line":
            xs += [int(g[3]), int(g[5])]; ys += [int(g[4]), int(g[6])]
        elif kind == "polyline":
            vals = list(map(int, g[3:]))
            xs += vals[0::2]; ys += vals[1::2]
        elif kind == "label":
            xs.append(int(g[2])); ys.append(int(g[3]))
    if not xs:
        xs, ys = [0, 1000], [0, -1000]
    x0, x1 = min(xs) - MARGIN, max(xs) + MARGIN
    y0, y1 = min(ys) - MARGIN, max(ys) + MARGIN

    def X(v): return (v - x0) * SCALE
    def Y(v): return (y1 - v) * SCALE   # flip: mils y-up -> svg y-down

    w, h = (x1 - x0) * SCALE, (y1 - y0) * SCALE
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
           f'viewBox="0 0 {w:.0f} {h:.0f}" font-family="monospace" font-size="{FONT}">',
           '<rect width="100%" height="100%" fill="white"/>',
           f'<text x="6" y="{FONT + 4}" font-size="{FONT + 3}">{_esc(spec.name)}  -  part {part} '
           f'of {spec.part_count}  ({len(pins)} pins)</text>']

    for g in gfx:
        kind = g[0]
        if kind == "rectangle":
            ax, ay, bx, by = map(int, g[4:8])
            out.append(f'<rect x="{X(min(ax,bx)):.1f}" y="{Y(max(ay,by)):.1f}" '
                       f'width="{abs(bx-ax)*SCALE:.1f}" height="{abs(by-ay)*SCALE:.1f}" '
                       f'fill="#fff6c8" stroke="#7a1f1f" stroke-width="1.2"/>')
        elif kind == "line":
            ax, ay, bx, by = map(int, g[3:7])
            out.append(f'<line x1="{X(ax):.1f}" y1="{Y(ay):.1f}" x2="{X(bx):.1f}" y2="{Y(by):.1f}" '
                       f'stroke="#7a1f1f" stroke-width="1.2"/>')
        elif kind == "polyline":
            vals = list(map(int, g[3:]))
            pts = " ".join(f"{X(vals[i]):.1f},{Y(vals[i+1]):.1f}" for i in range(0, len(vals), 2))
            out.append(f'<polyline points="{pts}" fill="none" stroke="#7a1f1f" stroke-width="1"/>')
        elif kind == "label":
            out.append(f'<text x="{X(int(g[2])):.1f}" y="{Y(int(g[3])):.1f}" fill="#1f4e9a" '
                       f'font-weight="bold">{_esc(g[4])}</text>')

    for p in pins:
        left = p["orientation"] == "eRotate180"
        far = p["x"] - p["length"] if left else p["x"] + p["length"]
        out.append(f'<line x1="{X(p["x"]):.1f}" y1="{Y(p["y"]):.1f}" x2="{X(far):.1f}" '
                   f'y2="{Y(p["y"]):.1f}" stroke="#1a1a1a" stroke-width="1.4"/>')
        # pin number just outside the tip, pin name just inside the body
        num_x = X(far) + (-4 if left else 4)
        out.append(f'<text x="{num_x:.1f}" y="{Y(p["y"]) - 2:.1f}" '
                   f'text-anchor="{"end" if left else "start"}" fill="#444">{_esc(p["number"])}</text>')
        name_x = X(p["x"]) + (6 if left else -6)
        out.append(f'<text x="{name_x:.1f}" y="{Y(p["y"]) + FONT/2 - 1:.1f}" '
                   f'text-anchor="{"start" if left else "end"}">{_esc(p["name"])}</text>')
    out.append("</svg>")
    return "\n".join(out)


def write_svgs(spec: SymbolSpec, out_dir) -> List[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in spec.name)
    paths = []
    for part in range(1, spec.part_count + 1):
        p = out_dir / f"{safe}_part{part}.svg"
        p.write_text(render_part_svg(spec, part), encoding="utf-8")
        paths.append(p)
    return paths
