"""Convention lint for a schematic sheet, over the object dump.

Everything a cluster on a house sheet carries (wire, net label, net-class
directive, test pad with its MFG_TP mark, harness connector entry, signal
harness, port) is checked here as a rule, so a sheet edit can be judged by a
table instead of by eye. The rules were written after a rewire on the X9 MC
comms sheet that moved wires and labels and silently left every other kind
of object on the neighbour's net; see dev/SCHEMATIC_CONVENTIONS.md, section
13, for the anatomy these rules encode.

Input is the object list produced by main._parse_sch_dump (kinds pin, wire,
netlabel, paramset, component, port, harness, signalharness, power, noerc),
so the same lint runs against a live sheet or an offline dump. No Altium
dependency: this module is importable and testable on its own.

Coordinates are Altium internal units (1 mil = 10000).
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Point = Tuple[int, int]

PAD_PREFIXES = ("TESTPOINT",)
MFG_PAD_TOKEN = "_MFG"
MFG_MARK = "MFG_TP"
MARK_CLASSES = {"MFG_TP"}   # ClassName values that are pad marks, not routing classes
IC_MIN_PINS = 8          # components with at least this many pins are "the part"


def _inside(p: Point, rect) -> bool:
    x0, y0, x1, y1 = rect
    return min(x0, x1) <= p[0] <= max(x0, x1) and min(y0, y1) <= p[1] <= max(y0, y1)


def _on_border(p: Point, rect) -> bool:
    x0, y0, x1, y1 = rect
    xs, ys = sorted((x0, x1)), sorted((y0, y1))
    on_v = p[0] in xs and ys[0] <= p[1] <= ys[1]
    on_h = p[1] in ys and xs[0] <= p[0] <= xs[1]
    return on_v or on_h


# --------------------------------------------------------------------------- geometry
def _on_segment(p: Point, a: Point, b: Point) -> bool:
    (px, py), (ax, ay), (bx, by) = p, a, b
    if not (min(ax, bx) <= px <= max(ax, bx) and min(ay, by) <= py <= max(ay, by)):
        return False
    return (bx - ax) * (py - ay) - (by - ay) * (px - ax) == 0


def _vertices(obj) -> List[Point]:
    out = []
    for v in obj.get("vertices", []):
        if isinstance(v, dict):
            out.append((int(v["x"]), int(v["y"])))
        else:
            out.append((int(v[0]), int(v[1])))
    return out


class _Nets:
    """Wire-connected nets: a union-find over wires that touch."""

    def __init__(self, wires: Sequence[dict]):
        self.segs: List[Tuple[Point, Point, int]] = []      # (a, b, wire index)
        self.ends: List[List[Point]] = []
        for i, w in enumerate(wires):
            pts = _vertices(w)
            self.ends.append(pts)
            for a, b in zip(pts, pts[1:]):
                self.segs.append((a, b, i))
        self.parent = list(range(len(wires)))
        for i, pts in enumerate(self.ends):
            for p in pts:
                for a, b, j in self.segs:
                    if j != i and _on_segment(p, a, b):
                        self._union(i, j)

    def _find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def _union(self, i, j):
        ri, rj = self._find(i), self._find(j)
        if ri != rj:
            self.parent[rj] = ri

    def net_of_point(self, p: Point) -> Optional[int]:
        for a, b, i in self.segs:
            if _on_segment(p, a, b):
                return self._find(i)
        return None

    def wire_ends(self) -> List[Tuple[Point, int]]:
        return [(pts[k], self._find(i)) for i, pts in enumerate(self.ends) if pts for k in (0, -1)]


# --------------------------------------------------------------------------- harness file
def parse_harness_file(text: str) -> Dict[str, List[str]]:
    """`TYPE=SIG1,SIG2` per line, the format of a project's .Harness files."""
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith(("#", ";", "[")):
            k, _, v = line.partition("=")
            out[k.strip()] = [s.strip() for s in v.split(",") if s.strip()]
    return out


# --------------------------------------------------------------------------- lint
def _finding(rule, severity, message, x=None, y=None, **extra):
    f = {"rule": rule, "severity": severity, "message": message}
    if x is not None:
        f["x"], f["y"] = int(x), int(y)
    f.update(extra)
    return f


def lint(objs: Iterable[dict], harness_types: Optional[Dict[str, List[str]]] = None,
         label_pattern: Optional[str] = None) -> dict:
    """Check a sheet's objects against the house cluster anatomy.

    harness_types: {type: [signals]} from the project's .Harness file; when
        given, connector / port types and entry names are checked against it.
    label_pattern: a regex every net label must match (e.g. the board's
        MC_<pin>_<signal>[__<function>] grammar); unmatched labels are warned.

    Returns {"summary", "findings", "nets", "clusters", "singles"}.
    """
    objs = list(objs)
    by = defaultdict(list)
    for o in objs:
        by[o.get("kind")].append(o)

    nets = _Nets(by["wire"])
    findings: List[dict] = []
    pat = re.compile(label_pattern) if label_pattern else None

    # ---- components and their displayed pins
    comps = {}
    for c in by["component"]:
        comps.setdefault((c.get("designator"), c.get("lib_reference")), c)
    pins_per_comp = defaultdict(int)
    for p in by["pin"]:
        pins_per_comp[(p.get("owner_designator"), p.get("owner_lib_reference"))] += 1

    def displayed(pin) -> bool:
        c = comps.get((pin.get("owner_designator"), pin.get("owner_lib_reference")))
        if not c:
            return True
        part = pin.get("owner_part_id", 0)
        return part in (0, c.get("current_part_id", 1))

    def is_pad(lib_ref: str) -> bool:
        return bool(lib_ref) and lib_ref.upper().startswith(PAD_PREFIXES)

    # ---- per net record
    netinfo: Dict[int, dict] = defaultdict(lambda: {
        "labels": [], "classes": [], "directives": [], "pins": [], "pads": [], "ports": [],
        "entries": [], "power": [], "noerc": 0})

    for lab in by["netlabel"]:
        n = nets.net_of_point((lab["x"], lab["y"]))
        if n is None:
            findings.append(_finding("label_floating", "error",
                                     "net label %r is on no wire and names nothing" % lab["text"],
                                     lab["x"], lab["y"], label=lab["text"]))
        else:
            netinfo[n]["labels"].append(lab["text"])
        if pat and not pat.match(lab["text"]):
            findings.append(_finding("label_grammar", "warning",
                                     "net label %r does not follow the label grammar" % lab["text"],
                                     lab["x"], lab["y"], label=lab["text"]))

    blankets = [(b["x0"], b["y0"], b["x1"], b["y1"]) for b in by["blanket"]]
    for ps in by["paramset"]:
        params = {p["name"]: p["text"] for p in ps.get("params", [])}
        cls = params.get("ClassName")
        n = nets.net_of_point((ps["x"], ps["y"]))
        if n is None:
            rect = next((r for r in blankets if _on_border((ps["x"], ps["y"]), r)), None)
            if rect is not None:
                inside = sorted({lab for m, info in netinfo.items() for lab in info["labels"]
                                 if any(_inside(p, rect) for p, r in nets.wire_ends() if r == m)})
                # labels are registered below; compute directly from wires instead
                inside = sorted({lab["text"] for lab in by["netlabel"]
                                 if nets.net_of_point((lab["x"], lab["y"])) is not None
                                 and any(_inside(p, rect) for p, r in nets.wire_ends()
                                         if r == nets.net_of_point((lab["x"], lab["y"])))})
                findings.append(_finding("blanket_directive", "info",
                                         "directive %r on a blanket border marks %d net(s): %s"
                                         % (ps["name"], len(inside), ", ".join(inside) or "none"),
                                         ps["x"], ps["y"], directive=ps["name"], nets=inside))
                if not inside:
                    findings.append(_finding("blanket_empty", "warning",
                                             "blanket with directive %r covers no wire" % ps["name"],
                                             ps["x"], ps["y"], directive=ps["name"]))
                continue
            findings.append(_finding("directive_floating", "error",
                                     "directive %r (%s) is on no wire and marks nothing"
                                     % (ps["name"], ", ".join("%s=%s" % kv for kv in params.items()) or "no parameters"),
                                     ps["x"], ps["y"], directive=ps["name"]))
            continue
        netinfo[n]["directives"].append(ps["name"])
        if cls and cls not in MARK_CLASSES:
            netinfo[n]["classes"].append(cls)

    for pw in by["power"]:
        n = nets.net_of_point((pw["x"], pw["y"]))
        if n is not None:
            netinfo[n]["power"].append(pw["text"])

    noerc_points = {(ne["x"], ne["y"]) for ne in by["noerc"]}
    for ne in by["noerc"]:
        n = nets.net_of_point((ne["x"], ne["y"]))
        if n is not None:
            netinfo[n]["noerc"] += 1

    for p in by["pin"]:
        if not displayed(p):
            continue
        key = (p.get("owner_designator"), p.get("owner_lib_reference"))
        pt = (p["connect_x"], p["connect_y"])
        n = nets.net_of_point(pt)
        if n is None:
            if is_pad(p.get("owner_lib_reference", "")):
                findings.append(_finding("pad_floating", "error",
                                         "test pad %s is on no wire" % p.get("owner_designator"),
                                         pt[0], pt[1], designator=p.get("owner_designator")))
            elif pins_per_comp[key] >= IC_MIN_PINS:
                findings.append(_finding("pin_unwired", "error",
                                         "%s pin %s (%s) has no wire at its electrical point"
                                         % (p.get("owner_designator"), p.get("designator"), p.get("name")),
                                         pt[0], pt[1], designator=p.get("owner_designator"), pin=p.get("designator")))
            else:
                findings.append(_finding("pin_unwired", "warning",
                                         "%s pin %s has no wire at its electrical point"
                                         % (p.get("owner_designator"), p.get("designator")),
                                         pt[0], pt[1], designator=p.get("owner_designator"), pin=p.get("designator")))
            continue
        if is_pad(p.get("owner_lib_reference", "")):
            netinfo[n]["pads"].append(p.get("owner_designator"))
            if MFG_PAD_TOKEN in p.get("owner_lib_reference", "").upper():
                netinfo[n].setdefault("mfg_pads", []).append(p.get("owner_designator"))
        else:
            netinfo[n]["pins"].append("%s.%s" % (p.get("owner_designator"), p.get("designator")))

    # ---- harness connectors, signal harnesses, ports
    types = harness_types or {}
    conn_primary: Dict[int, Point] = {}
    harness_ends: List[Tuple[Point, int]] = []
    for i, sh in enumerate(by["signalharness"]):
        pts = _vertices(sh)
        if pts:
            harness_ends += [(pts[0], i), (pts[-1], i)]
    harness_points = {p for p, _ in harness_ends}

    clusters = []
    for ci, hc in enumerate(by["harness"]):
        entries = hc.get("entries", [])
        side_right = all(e.get("side", 1) == 1 for e in entries)   # entries on the right edge
        px = hc["x"] if side_right else hc["x"] + hc.get("x_size", 0)
        primary = (px, hc["y"] - hc.get("primary_connection_position", 0))
        conn_primary[ci] = primary
        cluster = {"harness_type": hc["harness_type"], "x": hc["x"], "y": hc["y"], "nets": []}
        if types and hc["harness_type"] not in types:
            findings.append(_finding("harness_type_unknown", "warning",
                                     "harness connector type %r is not defined in the project's .Harness files"
                                     % hc["harness_type"], hc["x"], hc["y"], harness_type=hc["harness_type"]))
        for e in entries:
            pt = (e["x"], e["y"])
            n = nets.net_of_point(pt)
            if n is None:
                if pt in noerc_points:
                    findings.append(_finding("entry_unwired", "info",
                                             "harness entry %s.%s is unwired, marked No ERC (intentional)"
                                             % (hc["harness_type"], e["name"]), pt[0], pt[1],
                                             harness_type=hc["harness_type"], entry=e["name"]))
                else:
                    findings.append(_finding("entry_unwired", "error",
                                             "harness entry %s.%s has no wire at its connection point"
                                             % (hc["harness_type"], e["name"]), pt[0], pt[1],
                                             harness_type=hc["harness_type"], entry=e["name"]))
            else:
                netinfo[n]["entries"].append("%s.%s" % (hc["harness_type"], e["name"]))
                cluster["nets"].append(n)
            if types and hc["harness_type"] in types and e["name"] not in types[hc["harness_type"]]:
                findings.append(_finding("entry_not_in_type", "warning",
                                         "entry %r is not a signal of harness type %s (%s)"
                                         % (e["name"], hc["harness_type"], ", ".join(types[hc["harness_type"]])),
                                         pt[0], pt[1], harness_type=hc["harness_type"], entry=e["name"]))
        if primary not in harness_points:
            findings.append(_finding("harness_missing", "error",
                                     "harness connector %s has no signal harness at its primary connection"
                                     % hc["harness_type"], primary[0], primary[1], harness_type=hc["harness_type"]))
        clusters.append(cluster)

    port_points: Dict[Point, dict] = {}
    for po in by["port"]:
        w = po.get("width", 0)
        port_points[(po["x"], po["y"])] = po
        port_points[(po["x"] + w, po["y"])] = po
    for pt, i in harness_ends:
        if pt in conn_primary.values() or pt in port_points:
            continue
        # a harness end on another harness's interior is fine too
        joined = any(_on_segment(pt, a, b) for j, sh in enumerate(by["signalharness"]) if j != i
                     for a, b in zip(_vertices(sh), _vertices(sh)[1:]))
        if not joined:
            findings.append(_finding("harness_dangling", "error",
                                     "signal harness end at %s meets no connector and no port" % (pt,),
                                     pt[0], pt[1]))

    for po in by["port"]:
        w = po.get("width", 0)
        ends = [(po["x"], po["y"]), (po["x"] + w, po["y"])]
        ht = po.get("harness_type") or ""
        if ht:
            if not any(e in harness_points for e in ends):
                findings.append(_finding("port_unattached", "error",
                                         "harness port %s (%s) touches no signal harness" % (po["name"], ht),
                                         po["x"], po["y"], port=po["name"]))
            if types and ht not in types:
                findings.append(_finding("harness_type_unknown", "warning",
                                         "port %s uses harness type %r, not in the project's .Harness files"
                                         % (po["name"], ht), po["x"], po["y"], port=po["name"]))
        else:
            n = next((nets.net_of_point(e) for e in ends if nets.net_of_point(e) is not None), None)
            if n is None:
                findings.append(_finding("port_unattached", "error",
                                         "plain port %s is on no wire end" % po["name"], po["x"], po["y"], port=po["name"]))
            else:
                netinfo[n]["ports"].append(po["name"])
    names = defaultdict(int)
    for po in by["port"]:
        names[po["name"]] += 1
    for nm, k in names.items():
        if k > 1:
            findings.append(_finding("port_duplicate", "warning", "port name %r appears %d times" % (nm, k), port=nm))

    # ---- per-net rules
    net_rows = []
    for n, info in sorted(netinfo.items(), key=lambda kv: kv[0]):
        labels = sorted(set(info["labels"]))
        seg_pts = [p for p, r in nets.wire_ends() if r == n]
        x, y = (seg_pts[0] if seg_pts else (None, None))
        row = {"net": n, "labels": labels, "classes": sorted(set(info["classes"])),
               "pins": info["pins"], "pads": info["pads"], "ports": info["ports"],
               "entries": info["entries"], "power": info["power"]}
        net_rows.append(row)
        if not labels and not info["power"] and (info["pins"] or info["pads"] or info["entries"]):
            findings.append(_finding("net_unnamed", "error",
                                     "wire net with %s has no net label" % (", ".join(info["pins"] + info["pads"] + info["entries"]) or "connections"),
                                     x, y))
        if len(labels) > 1:
            findings.append(_finding("label_conflict", "warning",
                                     "one wire net carries %d different labels: %s (they merge)" % (len(labels), ", ".join(labels)),
                                     x, y, labels=labels))
        if info["entries"] and not info["directives"]:
            findings.append(_finding("directive_missing", "warning",
                                     "interface net %s reaches %s but carries no net-class directive"
                                     % (labels[0] if labels else "(unnamed)", ", ".join(info["entries"])), x, y,
                                     label=labels[0] if labels else None))
        if len(set(info["classes"])) > 1:
            findings.append(_finding("class_conflict", "warning",
                                     "net %s carries directives of different classes: %s"
                                     % (labels[0] if labels else "(unnamed)", ", ".join(sorted(set(info["classes"])))), x, y))
        mfg = info.get("mfg_pads", [])
        if mfg and MFG_MARK not in info["directives"]:
            findings.append(_finding("mfg_mark_missing", "warning",
                                     "MFG pad %s on net %s has no %s directive on that net"
                                     % (", ".join(mfg), labels[0] if labels else "(unnamed)", MFG_MARK), x, y))
        if labels and not info["pins"] and not info["pads"] and not info["entries"] and not info["ports"]:
            findings.append(_finding("net_isolated", "warning",
                                     "net %s has a label but no pin, pad, entry or port on this sheet" % labels[0], x, y))

    # ---- dangling wire ends: an end touching nothing at all
    attach = set()
    for p in by["pin"]:
        attach.add((p["connect_x"], p["connect_y"]))
    for hc in by["harness"]:
        for e in hc.get("entries", []):
            attach.add((e["x"], e["y"]))
    attach |= set(port_points)
    for pw in by["power"]:
        attach.add((pw["x"], pw["y"]))
    for pt, r in nets.wire_ends():
        if pt in attach:
            continue
        touches_other = sum(1 for a, b, i in nets.segs if _on_segment(pt, a, b)) > 1
        if not touches_other:
            has_label = any((l["x"], l["y"]) == pt for l in by["netlabel"])
            if not has_label:
                findings.append(_finding("wire_dangling", "info",
                                         "wire end at %s touches nothing (fine for a stub, wrong for a connection)" % (pt,),
                                         pt[0], pt[1]))

    # ---- clusters: consistent class within a connector
    for c in clusters:
        classes = sorted({cls for n in c["nets"] for cls in netinfo[n]["classes"]})
        c["classes"] = classes
        c["labels"] = [netinfo[n]["labels"][0] if netinfo[n]["labels"] else None for n in c["nets"]]
        if len(classes) > 1:
            findings.append(_finding("cluster_class_mixed", "info",
                                     "cluster %s mixes classes %s" % (c["harness_type"], ", ".join(classes)),
                                     c["x"], c["y"], harness_type=c["harness_type"]))
    in_cluster = {n for c in clusters for n in c["nets"]}
    singles = [r for r in net_rows if r["net"] not in in_cluster and r["pins"]]
    for r in singles:
        if not r["ports"] and not r["power"]:
            findings.append(_finding("single_no_port", "info",
                                     "net %s leaves the sheet by name only (no harness entry, no port)"
                                     % (r["labels"][0] if r["labels"] else "(unnamed)"), label=r["labels"][0] if r["labels"] else None))

    unannotated = sorted({c["designator"] for c in by["component"] if "?" in (c.get("designator") or "")})
    if unannotated:
        findings.append(_finding("unannotated", "info", "%d component designator(s) still contain '?': %s"
                                 % (len(unannotated), ", ".join(unannotated[:12]))))

    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["rule"], f.get("y", 0), f.get("x", 0)))
    summary = {"errors": sum(f["severity"] == "error" for f in findings),
               "warnings": sum(f["severity"] == "warning" for f in findings),
               "infos": sum(f["severity"] == "info" for f in findings),
               "nets": len(net_rows), "clusters": len(clusters), "singles": len(singles)}
    return {"summary": summary, "findings": findings, "nets": net_rows,
            "clusters": clusters, "singles": singles}


def format_report(result: dict, max_findings: int = 80) -> str:
    """A compact text view: summary, then findings by severity."""
    s = result["summary"]
    lines = ["%d error(s), %d warning(s), %d info - %d nets, %d harness clusters, %d single nets"
             % (s["errors"], s["warnings"], s["infos"], s["nets"], s["clusters"], s["singles"])]
    for f in result["findings"][:max_findings]:
        where = " @%d,%d" % (f["x"] // 10000, f["y"] // 10000) if "x" in f else ""
        lines.append("  %-7s %-22s %s%s" % (f["severity"], f["rule"], f["message"], where))
    if len(result["findings"]) > max_findings:
        lines.append("  ... %d more" % (len(result["findings"]) - max_findings))
    return "\n".join(lines)
