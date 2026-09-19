"""Does a library symbol agree with a CubeMX pinout?

A functional-split symbol states the design intent twice over: which pins
exist (number and name, from the MCU database) and which peripheral each
pin serves (the group label a pin sits under). The .ioc holds the same two
facts. This module reads both back out of a get_symbol_primitives dump and
reports every disagreement, so an existing symbol can be checked against
the .ioc that is supposed to describe it before anything is regenerated.

Grouping is inferred from geometry the way a reader does it, measured on
the house symbols EMBEDDED_IC_STM32H753IIK6_X57 and _XIH6_X9_MC:

  * pins on one edge of a part are read top to bottom; a missing row (gap
    wider than one pitch) or a horizontal divider starts a new cluster;
  * the label written within a cluster's rows, on the same side of the
    vertical divider, names the cluster. Labels are placed by hand, so they
    may sit on the first, a middle or the last row;
  * horizontal dividers bound REGIONS. A region drawn with a single label
    (a long GPIO column broken into readable runs) applies that label to
    every cluster in it; the label is marked as inherited;
  * a label such as PORTA / PA names a port-split part and carries no
    peripheral information; a label such as TIM8_PWM or ETH_RMII refines
    the peripheral and matches it.

Nothing here needs Altium: the dump is a JSON file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import naming
from .model import Pinout
from .verify import ELECTRICAL_NAME

PITCH = 100
SIDE_OF_ORIENTATION = {2: "left", 0: "right", 1: "top", 3: "bottom"}

# Labels that mean "nothing in particular is connected here".
FREE_GROUPS = {"GPIO", "UNUSED", "SPARE", "NC", "N/C", "FREE", "UNASSIGNED", "MISC", "IO"}
# Peripheral groups that live with the power pins by convention and need no label.
SYSTEM_GROUPS = {"RCC", "SYS", "PWR"}
_PORT_LABEL = re.compile(r"^(?:PORT|GPIO|P)?([A-K])$")
_ANNOTATION = re.compile(r"\s*\([^()]*\)\s*$")


def is_port_label(text: Optional[str]) -> bool:
    return bool(text) and bool(_PORT_LABEL.match(text.strip().upper().replace(" ", "")))


def group_matches(want: Optional[str], have: Optional[str]) -> bool:
    """Canonical groups equal; or the label refines the peripheral (TIM8_PWM
    for TIM8, ETH_RMII for ETH); or one side names the family without an
    index (ADC for the shared ADC1/ADC2 inputs CubeMX calls ADCx, SPI for
    SPI1) - the family alone is not a contradiction."""
    if not want or not have:
        return False
    if want == have or have.startswith(want + "_") or have.startswith(want + "/"):
        return True
    fw, fh = want.rstrip("0123456789"), have.rstrip("0123456789")
    return fw == fh and (fw == want or fh == have)


def _name_key(s: str) -> str:
    # backslashes are Altium's overbar markup (R\S\T\ is RST with a bar)
    return re.sub(r"[-_ \\]", "", _ANNOTATION.sub("", s)).casefold()


def name_relation(mcu_name: str, signal: Optional[str], symbol_name: str) -> str:
    """'same' (the MCU-DB name or its debug-style display form), 'variant'
    (case, separators, a dropped annotation, a numeric suffix such as VCAP3
    for one of several VCAP pins) or 'different' (VSS drawn as NC)."""
    if symbol_name == mcu_name or symbol_name == naming.display_name(mcu_name, signal, "debug"):
        return "same"
    a, b = _name_key(mcu_name), _name_key(symbol_name)
    if a == b or (b.startswith(a) and b[len(a):].isdigit()):
        return "variant"
    if a == "n" + b or b == "n" + a:            # NRST drawn as an overbarred RST
        return "variant"
    return "different"


@dataclass
class Cluster:
    part: int
    side: str
    pins: List[dict]                       # dump pin primitives, top to bottom
    labels: List[str] = field(default_factory=list)
    inherited: bool = False
    region: int = 0

    @property
    def label(self) -> Optional[str]:
        return self.labels[0] if self.labels else None

    @property
    def y_top(self) -> int:
        return max(int(round(p["y"])) for p in self.pins)

    @property
    def y_bottom(self) -> int:
        return min(int(round(p["y"])) for p in self.pins)

    def summary(self) -> dict:
        return {"part": self.part, "side": self.side, "region": self.region, "label": self.label,
                "inherited": self.inherited, "extra_labels": self.labels[1:], "pins": len(self.pins),
                "first": self.pins[0]["pin_name"], "last": self.pins[-1]["pin_name"],
                "y_top": self.y_top, "y_bottom": self.y_bottom}


def _part(p: dict) -> int:
    return int(p.get("owner_part_id", 1))


def _dividers(prims: List[dict], part: int) -> Tuple[Optional[int], List[Tuple[int, int, int]]]:
    """(x of the vertical divider or None, [(y, x_min, x_max)] horizontal ones)."""
    vertical, horizontal = [], []
    for p in prims:
        if _part(p) != part or p.get("type") not in ("line", "polyline"):
            continue
        if p["type"] == "line":
            pts = [(int(round(p["x1"])), int(round(p["y1"]))), (int(round(p["x2"])), int(round(p["y2"])))]
        else:
            pts = [(int(round(v["x"])), int(round(v["y"]))) for v in p.get("vertices", [])]
        if len(pts) < 2:
            continue
        xs = {x for x, _ in pts}
        ys = {y for _, y in pts}
        if len(xs) == 1 and len(ys) > 1:
            vertical.append((max(ys) - min(ys), xs.pop()))
        elif len(ys) == 1 and len(xs) > 1:
            horizontal.append((ys.pop(), min(xs), max(xs)))
    vx = max(vertical)[1] if vertical else None
    return vx, horizontal


def _body_centre_x(prims: List[dict], part: int) -> Optional[int]:
    rects = [p for p in prims if _part(p) == part and p.get("type") == "rectangle"]
    if not rects:
        return None
    r = max(rects, key=lambda r: abs(r["x2"] - r["x1"]) * abs(r["y2"] - r["y1"]))
    return int(round((r["x1"] + r["x2"]) / 2))


def infer_clusters(dump: dict) -> List[Cluster]:
    prims = dump.get("primitives", [])
    out: List[Cluster] = []
    for part in sorted({_part(p) for p in prims}):
        pins = [p for p in prims if _part(p) == part and p.get("type") == "pin"]
        if not pins:
            continue
        vx, horizontals = _dividers(prims, part)
        split_x = vx if vx is not None else _body_centre_x(prims, part)

        def side_of_x(x: int) -> str:
            return "left" if split_x is None or x < split_x else "right"

        def side_dividers(side: str) -> List[int]:
            return sorted((h[0] for h in horizontals
                           if split_x is None or (h[1] < split_x if side == "left" else h[2] > split_x)),
                          reverse=True)

        clusters_here: List[Cluster] = []
        for side in ("left", "right", "top", "bottom"):
            col = [p for p in pins if SIDE_OF_ORIENTATION.get(int(p["orientation"])) == side]
            if not col:
                continue
            if side in ("left", "right"):
                col.sort(key=lambda p: (-int(round(p["y"])), int(round(p["x"]))))
                hs = side_dividers(side)
                current: List[dict] = []
                for p in col:
                    y = int(round(p["y"]))
                    if current:
                        prev = int(round(current[-1]["y"]))
                        crossed = any(y < hy < prev for hy in hs)
                        if prev - y > PITCH or crossed:
                            clusters_here.append(Cluster(part, side, current))
                            current = []
                    current.append(p)
                if current:
                    clusters_here.append(Cluster(part, side, current))
                for c in clusters_here:
                    if c.side == side:
                        c.region = sum(1 for hy in hs if hy > c.y_top)
            else:
                col.sort(key=lambda p: (int(round(p["x"])), -int(round(p["y"]))))
                clusters_here.append(Cluster(part, side, col))

        # attach labels: within the cluster's rows on its side, else the
        # cluster whose top row is within one pitch below the label
        for lab in (p for p in prims if _part(p) == part and p.get("type") == "label"):
            lx, ly = int(round(lab["x"])), int(round(lab["y"]))
            side = side_of_x(lx)
            same = [c for c in clusters_here if c.side == side]
            hit = [c for c in same if c.y_bottom <= ly <= c.y_top]
            if not hit:
                near = [c for c in same if 0 < ly - c.y_top <= PITCH]
                hit = sorted(near, key=lambda c: ly - c.y_top)[:1]
            if hit:
                hit[0].labels.append(str(lab["text"]))
            else:
                out.append(Cluster(part, side, [], [str(lab["text"])]))     # orphan label

        # a region drawn with one label names every cluster in it
        by_region: Dict[Tuple[str, int], List[Cluster]] = {}
        for c in clusters_here:
            by_region.setdefault((c.side, c.region), []).append(c)
        for group in by_region.values():
            labels = {c.label for c in group if c.label}
            if len(labels) == 1:
                lab = labels.pop()
                for c in group:
                    if not c.label:
                        c.labels.append(lab)
                        c.inherited = True
        out.extend(clusters_here)
    return out


def _poskey(pos: str):
    """Sort package positions the way a datasheet lists them: 1,2,3 or A1..A15,B1.."""
    s = str(pos)
    letters = s.rstrip("0123456789")
    digits = s[len(letters):]
    return (0 if not letters else 1, letters, int(digits) if digits.isdigit() else 0, s)


def audit(pinout: Pinout, dump: dict) -> dict:
    """Compare a Pinout (MCU DB + .ioc) with a symbol dump.

    Hard findings (the symbol contradicts the MCU database or the .ioc):
        pins_missing_on_symbol      package positions the symbol lacks
        pins_not_in_mcu             symbol pin numbers the MCU database lacks
        duplicate_pin_numbers
        name_mismatches             same position, a different pin (VSS drawn as NC)
        peripheral_mismatches       .ioc assigns peripheral A, symbol files it under B
        peripheral_pin_under_gpio   .ioc assigns a peripheral, symbol files it under
                                    GPIO / UNUSED / SPARE
    Soft findings (worth a look, often intentional):
        name_variants               case, separators, dropped annotation, VCAP3 for VCAP
        gpio_under_peripheral       .ioc says GPIO_*, symbol files it under a peripheral
                                    (a chip-select drawn with its SPI)
        unassigned_but_grouped      no .ioc signal, yet under a peripheral label
        assigned_but_unlabelled     .ioc assigns a signal, cluster has no label at all
        electrical_mismatches       derived eElectric* differs from the symbol's
        orphan_labels               labels attached to no pin cluster
    Informational:
        system_pins                 RCC/SYS pins and where the symbol put them
        port_split_pins             pins under a PORTx label (no peripheral to compare)
    """
    db = {p.number: p for p in pinout.pins}
    prims = dump.get("primitives", [])
    sym: Dict[str, dict] = {}
    duplicates = []
    for p in prims:
        if p.get("type") != "pin":
            continue
        n = str(p["pin_number"])
        if n in sym:
            duplicates.append(n)
        sym[n] = p

    missing = sorted(set(db) - set(sym), key=_poskey)
    extra = sorted(set(sym) - set(db), key=_poskey)
    common = sorted(set(db) & set(sym), key=_poskey)

    clusters = infer_clusters(dump)
    group_of: Dict[str, Cluster] = {}
    for c in clusters:
        for p in c.pins:
            group_of[str(p["pin_number"])] = c

    names, variants, periph_mism, periph_under_gpio, gpio_under_periph = [], [], [], [], []
    unassigned_grouped, assigned_unlabelled, elec_mism, system_pins, port_pins = [], [], [], [], []
    for n in common:
        rec, sp = db[n], sym[n]
        sname = str(sp["pin_name"])
        rel = name_relation(rec.name, rec.signal, sname)
        if rel != "same":
            (variants if rel == "variant" else names).append(
                {"number": n, "mcu": rec.name, "symbol": sname})
        want_el = naming.electrical_type(rec.name, rec.mcu_type, rec.signal)
        have_el = ELECTRICAL_NAME.get(int(sp["electrical"]), sp["electrical"])
        if want_el != have_el:
            elec_mism.append({"number": n, "name": rec.name, "signal": rec.signal,
                              "derived": want_el, "symbol": have_el})
        if not naming.is_port_pin(rec.name):
            continue                                  # power/reset/boot: no grouping
        c = group_of.get(n)
        label = c.label if c else None
        have = naming.canonical_group(label) if label else None
        want = naming.canonical_group(naming.peripheral_group(rec.signal)) if rec.signal else None
        row = {"number": n, "name": rec.name, "signal": rec.signal, "label": rec.label,
               "ioc_group": want, "symbol_group": have, "symbol_label": label,
               "inherited": bool(c and c.inherited),
               "part": c.part if c else None, "side": c.side if c else None}
        if want in SYSTEM_GROUPS:
            system_pins.append(row)
            continue
        if is_port_label(label):
            port_pins.append(row)
            continue
        have_free = have in FREE_GROUPS if have else False
        if want and have:
            if group_matches(want, have) or (want == "GPIO" and have_free):
                continue
            if want == "GPIO":
                gpio_under_periph.append(row)
            elif have_free:
                periph_under_gpio.append(row)
            else:
                periph_mism.append(row)
        elif want and not have:
            assigned_unlabelled.append(row)
        elif have and not want and not have_free:
            unassigned_grouped.append(row)

    orphans = [{"part": c.part, "side": c.side, "label": c.label} for c in clusters if not c.pins]
    multi = [c.summary() for c in clusters if len(c.labels) > 1]
    hard = (len(missing) + len(extra) + len(duplicates) + len(names) + len(periph_mism)
            + len(periph_under_gpio))
    soft = (len(variants) + len(gpio_under_periph) + len(unassigned_grouped)
            + len(assigned_unlabelled) + len(elec_mism) + len(orphans))
    return {
        "symbol": dump.get("symbol_name"), "mcu": pinout.mcu, "package": pinout.package,
        "match": hard == 0,
        "summary": {"mcu_pins": len(db), "symbol_pins": len(sym), "common": len(common),
                    "ioc_assigned": len(pinout.assigned()),
                    "clusters": len([c for c in clusters if c.pins]),
                    "labelled_clusters": len([c for c in clusters if c.pins and c.label]),
                    "inherited_labels": len([c for c in clusters if c.inherited]),
                    "port_split_pins": len(port_pins),
                    "hard_findings": hard, "soft_findings": soft},
        "pins_missing_on_symbol": [{"number": n, "name": db[n].name, "type": db[n].mcu_type,
                                    "signal": db[n].signal} for n in missing],
        "pins_not_in_mcu": [{"number": n, "name": sym[n]["pin_name"]} for n in extra],
        "duplicate_pin_numbers": duplicates,
        "name_mismatches": names,
        "name_variants": variants,
        "peripheral_mismatches": periph_mism,
        "peripheral_pin_under_gpio": periph_under_gpio,
        "gpio_under_peripheral": gpio_under_periph,
        "unassigned_but_grouped": unassigned_grouped,
        "assigned_but_unlabelled": assigned_unlabelled,
        "electrical_mismatches": elec_mism,
        "orphan_labels": orphans,
        "clusters_with_several_labels": multi,
        "system_pins": system_pins,
        "port_split_pins": port_pins,
        "clusters": [c.summary() for c in clusters if c.pins],
        "ioc_pins_not_in_mcu": pinout.meta.get("ioc_pins_not_in_mcu", ""),
    }


def format_report(r: dict, limit: int = 12, soft: bool = True) -> str:
    """A terminal-sized rendering of audit()."""
    s = r["summary"]
    lines = [f"{r['symbol']}  vs  {r['mcu']} ({r['package']})",
             f"  MCU pins {s['mcu_pins']}, symbol pins {s['symbol_pins']}, .ioc-assigned {s['ioc_assigned']}, "
             f"clusters {s['clusters']} ({s['labelled_clusters']} labelled, {s['inherited_labels']} by region"
             f"{', ' + str(s['port_split_pins']) + ' pins on port-labelled parts' if s['port_split_pins'] else ''})"
             f"  ->  {'MATCH' if r['match'] else 'MISMATCH'} ({s['hard_findings']} hard, {s['soft_findings']} soft)"]

    def block(title, rows, fmt):
        if not rows:
            return
        lines.append(f"  {title} ({len(rows)}):")
        for row in rows[:limit]:
            lines.append("    " + fmt(row))
        if len(rows) > limit:
            lines.append(f"    ... {len(rows) - limit} more")

    def grp(x):
        inh = " (region label)" if x.get("inherited") else ""
        return f"{x['symbol_label']}{inh}"

    block("pins missing on symbol", r["pins_missing_on_symbol"],
          lambda x: f"{x['number']:>4} {x['name']} [{x['type']}] {x['signal'] or ''}")
    block("symbol pins not in MCU", r["pins_not_in_mcu"], lambda x: f"{x['number']:>4} {x['name']}")
    block("duplicate pin numbers", r["duplicate_pin_numbers"], str)
    block("different pin at the same position", r["name_mismatches"],
          lambda x: f"{x['number']:>4} MCU '{x['mcu']}'  symbol '{x['symbol']}'")
    block("peripheral differs", r["peripheral_mismatches"],
          lambda x: f"{x['number']:>4} {x['name']:<26} .ioc {x['signal']:<22} symbol: {grp(x)} (part {x['part']})")
    block("peripheral pin filed under GPIO/UNUSED", r["peripheral_pin_under_gpio"],
          lambda x: f"{x['number']:>4} {x['name']:<26} .ioc {x['signal']:<22} symbol: {grp(x)} (part {x['part']})")
    if soft:
        block("pin text variant (soft)", r["name_variants"],
              lambda x: f"{x['number']:>4} MCU '{x['mcu']}'  symbol '{x['symbol']}'")
        block("GPIO drawn with a peripheral (soft)", r["gpio_under_peripheral"],
              lambda x: f"{x['number']:>4} {x['name']:<26} {x['signal']:<12} '{x['label'] or ''}' under {grp(x)}")
        block("no .ioc signal but under a peripheral (soft)", r["unassigned_but_grouped"],
              lambda x: f"{x['number']:>4} {x['name']:<26} under {grp(x)} (part {x['part']})")
        block("assigned but in an unlabelled cluster (soft)", r["assigned_but_unlabelled"],
              lambda x: f"{x['number']:>4} {x['name']:<26} {x['signal']} (part {x['part']} {x['side']})")
        block("electrical type differs (soft)", r["electrical_mismatches"],
              lambda x: f"{x['number']:>4} {x['name']:<26} {x['signal'] or '-':<22} derived {x['derived']}  symbol {x['symbol']}")
        block("orphan labels", r["orphan_labels"], lambda x: f"part {x['part']} {x['side']}: {x['label']}")
        block("clusters with several labels", r["clusters_with_several_labels"],
              lambda x: f"part {x['part']} {x['side']}: {[x['label']] + x['extra_labels']} ({x['pins']} pins)")
    return "\n".join(lines)
