"""Reconcile the sources of a board's pinout and let the designer choose.

    facts (ioc, sheet, schematic)  --compare per field-->  Diff rows
        --publish-->  a "Reconcile" tab in the pinning workbook, one row per
                      disagreement, with a Decision dropdown
        --collect-->  the decisions  --apply-->  sheet cells (gsheets), the
                      .ioc (a reviewed copy), Altium payloads (net-label
                      renames, symbol label edits) for the live step

Ownership (agreed 2026-09-19): the .ioc owns the SIGNAL; the sheet owns the
FUNCTION and the NET NAME (its Net Name column, overrides included); symbol
GROUPING and electrical type are derived. A disagreement therefore proposes
the owner's value and lists the writes that bring the others in line.

Status per row: agree | variant (spelling only) | conflict | missing:<source>
| sheet_error | info. Only non-agree rows are published.
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import naming, netnames
from .audit import group_matches, _poskey
from .manifest import Board
from .sources import Facts, is_sheet_error

SOURCES = ("cubemx", "sheet", "schematic", "symbol")
FIELDS = ("signal", "function", "net", "group")
DECISIONS = ["accept", "keep cubemx", "keep sheet", "keep schematic", "custom", "ignore"]
REVIEW_HEADER = ["Id", "Pin", "Ball", "Field", "CubeMX", "Sheet", "Schematic", "Symbol", "Status", "Proposed",
                 "Apply to", "Decision", "Custom value", "Note"]


@dataclass
class Diff:
    id: str
    pin: str
    ball: Optional[str]
    field: str
    values: Dict[str, Optional[str]]
    status: str
    proposed: Optional[str] = None
    apply_to: List[dict] = field(default_factory=list)
    note: str = ""

    def row(self) -> list:
        v = self.values
        return [self.id, self.pin, self.ball or "", self.field, v.get("cubemx") or "", v.get("sheet") or "",
                v.get("schematic") or "", v.get("symbol") or "", self.status, self.proposed or "",
                ", ".join(sorted({a["target"] for a in self.apply_to})), "", "", self.note]


@dataclass
class Report:
    board: str
    prefix: str
    diffs: List[Diff]
    agree: Dict[str, int]
    diagnostics: dict

    def summary(self) -> dict:
        by = {}
        for d in self.diffs:
            by.setdefault(d.field, {}).setdefault(d.status, 0)
            by[d.field][d.status] += 1
        targets = {}
        for d in self.diffs:
            for a in d.apply_to:
                targets[a["target"]] = targets.get(a["target"], 0) + 1
        return {"board": self.board, "disagreements": len(self.diffs), "agree": self.agree, "by_field": by,
                "writes_proposed_by_target": targets}

    def to_json(self) -> str:
        return json.dumps({"summary": self.summary(), "diffs": [asdict(d) for d in self.diffs],
                           "diagnostics": self.diagnostics}, indent=1, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# comparing
# ---------------------------------------------------------------------------

def expected_cube_label(signal: Optional[str], function: Optional[str], current: Optional[str] = None,
                        role: Optional[str] = None) -> Optional[str]:
    """What the .ioc GPIO_Label should say for a pin, from the owners: the
    signal (CubeMX) and the function (sheet). GPIO/analog pins carry just the
    function, or <role>__<function> when the GPIO has a role word (a chip
    select); peripheral pins <abbreviated signal>__<function>. With no
    function the current label is kept if it merely re-spells the signal."""
    is_gpio = not signal or signal.startswith("GPIO_")
    if not function:
        if is_gpio and role:
            return role
        return current if current and signal and current in netnames.signal_spellings(signal) else None
    if is_gpio:
        return f"{role}__{function}" if role else function
    if naming.peripheral_group(signal) in ("ADC", "ADC1", "ADC2", "ADC3"):
        return function
    return f"{netnames.preferred_spelling(signal)}__{function}"


def _sig_same(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return a == b
    if a == b:
        return True
    return b in netnames.signal_spellings(a) or a in netnames.signal_spellings(b)


def compare_pin(board: Board, key: str, ioc: dict, sh: dict, sch: dict) -> List[Diff]:
    out: List[Diff] = []
    pin_name = ioc.get("name") or sh.get("name") or sch.get("name") or key
    ball = ioc.get("ball") or sch.get("ball")
    prefix = board.prefix
    i_sig, s_sig, c_sig = ioc.get("signal"), sh.get("signal"), sch.get("signal")
    s_net, c_net = sh.get("net"), sch.get("net")
    sym_group = sch.get("group")
    s_is_role = False
    n = 0

    def diff(fld, values, status, proposed=None, apply_to=None, note=""):
        nonlocal n
        n += 1
        out.append(Diff(f"{key}:{fld}", key, ball, fld, values, status, proposed, apply_to or [], note))

    # ---- signal: owner cubemx ------------------------------------------------
    values = {"cubemx": i_sig, "sheet": s_sig, "schematic": c_sig if c_sig != "GPIO" else None,
              "symbol": sym_group}
    if i_sig or s_sig or (c_sig and c_sig != "GPIO"):
        problems, apply_to = [], []
        alternates = set(ioc.get("alternates") or [])
        s_is_role = bool(s_sig and i_sig and i_sig.startswith("GPIO_") and not s_sig.startswith("GPIO_")
                         and s_sig not in alternates and not any(s_sig in netnames.signal_spellings(a) for a in alternates))
        if s_is_role:
            values["sheet"] = f"{s_sig} (role)"
        if s_sig and not _sig_same(i_sig, s_sig) and not s_is_role:
            if s_sig.startswith("GPIO_") and i_sig and i_sig.startswith("GPIO_"):
                pass                                        # Input vs Output guessed from direction
            else:
                problems.append("sheet")
                apply_to.append({"target": "sheet", "field": "signal", "pin": key, "value": i_sig})
        elif not s_sig and i_sig and board.sheet:
            problems.append("missing:sheet")
        if c_net and i_sig:
            rel = netnames.relate(c_net, prefix, pin_name, i_sig, sh.get("function") or ioc.get("function"))["relation"]
            if rel == "different":
                problems.append("schematic")
        elif c_sig and c_sig != "GPIO" and not _sig_same(i_sig, c_sig):
            if not (i_sig and naming.canonical_group(naming.peripheral_group(i_sig)) ==
                    naming.canonical_group(naming.peripheral_group(c_sig))):
                problems.append("schematic")
        if sym_group and i_sig:
            want = naming.canonical_group(naming.peripheral_group(i_sig))
            if want not in ("GPIO", "RCC", "SYS") and not group_matches(want, sym_group):
                if sym_group in ("GPIO", "UNUSED", "SPARE", "IO"):
                    problems.append("symbol")
                else:
                    problems.append("symbol")
                apply_to.append({"target": "altium", "kind": "symbol_label", "pin": key,
                                 "symbol": board.altium.symbol if board.altium else None,
                                 "from": sch.get("symbol_label"), "to": want})
        if not i_sig and (s_sig or c_sig):
            diff("signal", values, "missing:cubemx", s_sig or None,
                 [{"target": "ioc", "field": "Signal", "pin": key, "value": s_sig}] if s_sig else [],
                 "the .ioc assigns nothing; the sheet/schematic do")
        elif problems:
            status = "conflict" if any(p in ("sheet", "schematic", "symbol") for p in problems) else problems[0]
            diff("signal", values, status, i_sig, apply_to, "; ".join(problems))
    elif (s_net or c_net) and not i_sig:
        lab = s_net or c_net
        if netnames.parse(lab, prefix).form != "spare":
            diff("signal", values, "missing:cubemx", None, [],
                 f"net {lab!r} names a use for this pin but the .ioc leaves it unassigned (configure it in CubeMX)")
    # ---- function: owner sheet -----------------------------------------------
    i_fn, s_fn, c_fn = ioc.get("function"), sh.get("function"), sch.get("function")
    role = (sh.get("role") or ((sh.get("role_candidate") or s_sig) if s_is_role else None) or ioc.get("role")
            if (not i_sig or i_sig.startswith("GPIO_")) else None)
    values = {"cubemx": i_fn, "sheet": s_fn, "schematic": c_fn, "symbol": None}
    if role:
        values["symbol"] = f"role {role}"
    if i_fn or s_fn or c_fn:
        owner = s_fn if board.sheet else (i_fn or c_fn)
        apply_to, problems = [], []
        if owner:
            if (i_fn or None) != owner:
                exp = expected_cube_label(i_sig, owner, ioc.get("cube_label"), role)
                if exp and exp != ioc.get("cube_label"):
                    problems.append("cubemx")
                    apply_to.append({"target": "ioc", "field": "GPIO_Label", "pin": key, "value": exp,
                                     "current": ioc.get("cube_label")})
            if c_fn is not None and c_fn != owner and not (sch.get("net") and sh.get("net") == sch.get("net")):
                problems.append("schematic")
            if problems:
                diff("function", values, "conflict" if "cubemx" in problems and i_fn else
                     ("missing:cubemx" if "cubemx" in problems else "conflict"), owner, apply_to,
                     "; ".join(problems))
        elif board.sheet and (i_fn or c_fn):
            diff("function", values, "missing:sheet", i_fn or c_fn, [], "sheet has no function for this pin")
    # ---- net: owner sheet ------------------------------------------------------
    gen = netnames.generate(prefix, pin_name, i_sig or s_sig, s_fn or i_fn, role=role) if (i_sig or s_sig or s_fn or i_fn) else None
    values = {"cubemx": gen, "sheet": s_net, "schematic": c_net or (", ".join(sch.get("net_ambiguous", [])) or None),
              "symbol": None}
    if s_net or c_net or sch.get("net_ambiguous"):
        if sch.get("net_ambiguous"):
            diff("net", values, "conflict", s_net or gen, [], f"pin sits on several labelled nets: {sch['net_ambiguous']}")
        elif s_net and c_net:
            if s_net == c_net:
                rel = netnames.relate(s_net, prefix, pin_name, i_sig, s_fn or i_fn)["relation"] if i_sig else "same"
                if rel == "different":
                    diff("net", values, "conflict", gen,
                         [{"target": "sheet", "field": "net", "pin": key, "value": gen},
                          {"target": "altium", "kind": "net_label", "pin": key, "from": c_net, "to": gen,
                           "sheets": sch.get("sheets", [])}],
                         "sheet and schematic agree, but the name encodes a signal the .ioc does not assign")
            else:
                rel = netnames.relate(c_net, prefix, pin_name, i_sig, s_fn or i_fn)["relation"]
                status = "variant" if rel in ("signal_spelling",) else "conflict"
                diff("net", values, status, s_net,
                     [{"target": "altium", "kind": "net_label", "pin": key, "from": c_net, "to": s_net,
                       "sheets": sch.get("sheets", [])}],
                     f"schematic label differs from the sheet's Net Name ({rel})")
        elif s_net and not c_net:
            if sch.get("labels") is not None and sch.get("wired") is not None:
                diff("net", values, "missing:schematic", s_net, [], "pin has no net label on the sheets"
                     + ("" if sch.get("wired") else " (and no wire)"))
        elif c_net and not s_net and board.sheet:
            if netnames.parse(c_net, prefix).form == "spare" and not i_sig:
                return out                                  # a free pin named after itself: nothing to record
            prop = c_net if netnames.parse(c_net, prefix).form != "other" else gen
            diff("net", values, "missing:sheet", prop,
                 [{"target": "sheet", "field": "net", "pin": key, "value": prop}],
                 "sheet has no Net Name for this pin")
    elif gen and board.sheet and (i_sig or s_sig) and sh:
        pass
    # ---- group: symbol vs derived (informational unless the signal row caught it) -----
    return out


def reconcile(board: Board, ioc: Facts, sheet: Facts, schem: Facts, diagnostics: Optional[dict] = None) -> Report:
    keys = sorted(set(ioc) | set(sheet) | set(schem), key=lambda k: _poskey(str((ioc.get(k) or schem.get(k) or {}).get("ball") or "ZZ99")) + (k,))
    diffs: List[Diff] = []
    agree = {f: 0 for f in FIELDS}
    diag = dict(diagnostics or {})
    for key in keys:
        i, s, c = ioc.get(key, {}), sheet.get(key, {}), schem.get(key, {})
        if ioc and key not in ioc:
            diffs.append(Diff(f"{key}:pin", key, c.get("ball"), "pin", {"cubemx": None, "sheet": s.get("name"),
                              "schematic": c.get("name"), "symbol": None}, "missing:cubemx", None, [],
                              "pin unknown to the MCU database / .ioc"))
            continue
        before = len(diffs)
        diffs += compare_pin(board, key, i, s, c)
        seen_fields = {d.field for d in diffs[before:]}
        for f in FIELDS:
            if f not in seen_fields and any(src.get(f) for src in (i, s, c)):
                agree[f] += 1
    # sheet-internal breakage
    for err in (diag.get("sheet", {}) or {}).get("errors", []):
        diffs.append(Diff(f"{err['pin']}:sheet_error:{err['column']}", err["pin"], None, err["column"],
                          {"cubemx": None, "sheet": err["value"], "schematic": None, "symbol": None}, "sheet_error",
                          None, [{"target": "sheet", "field": err["column"], "pin": err["pin"], "value": None}],
                          f"{err['tab']} row {err['row']}: formula error"))
    return Report(board.name, board.prefix, diffs, agree, diag)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def format_report(rep: Report, limit: int = 25) -> str:
    s = rep.summary()
    lines = [f"{rep.board}: {s['disagreements']} disagreements; agree: {s['agree']}",
             f"  writes proposed by target: {s['writes_proposed_by_target']}"]
    for fld, by in s["by_field"].items():
        lines.append(f"  {fld}: {by}")
    shown = 0
    for fld in FIELDS + ("pin",):
        rows = [d for d in rep.diffs if d.field == fld]
        if not rows:
            continue
        lines.append(f"  -- {fld} ({len(rows)}):")
        for d in rows[:limit]:
            v = d.values
            lines.append(f"     {d.pin:<9} {d.status:<17} cubemx={v.get('cubemx')!s:<28} sheet={v.get('sheet')!s:<34} "
                         f"schematic={v.get('schematic')!s:<34} -> {d.proposed!s}  [{', '.join(sorted({a['target'] for a in d.apply_to}))}] {d.note}")
            shown += 1
        if len(rows) > limit:
            lines.append(f"     ... {len(rows) - limit} more")
    errs = [d for d in rep.diffs if d.status == "sheet_error"]
    if errs:
        lines.append(f"  -- sheet errors ({len(errs)}): e.g. {[(d.pin, d.field, d.note) for d in errs[:3]]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the review tab
# ---------------------------------------------------------------------------

def review_rows(rep: Report) -> List[list]:
    return [REVIEW_HEADER] + [d.row() for d in rep.diffs]


def publish(rep: Report, board: Board, sheets, apply: bool = False) -> dict:
    """Write the review tab. Preview (apply=False) reports what would happen."""
    if not board.sheet:
        raise ValueError("board has no sheet")
    wb, tab = board.sheet.workbook, board.sheet.review_tab
    rows = review_rows(rep)
    meta = sheets.meta(wb, refresh=True)
    exists = any(t.title == tab for t in meta.tabs)
    plan = {"tab": tab, "exists": exists, "rows": len(rows) - 1, "columns": REVIEW_HEADER,
            "steps": ([] if exists else [f"create tab {tab!r}"]) + [f"clear {tab}!A1:N5000", f"write {len(rows)} rows",
                                                                     "dropdown on Decision (L)", "freeze row 1"]}
    if not apply:
        return {"success": True, "applied": False, **plan, "next": "call with apply=True to write"}
    if not exists:
        sheets.tabs(wb, "create", title=tab, rows=max(len(rows) + 50, 200), cols=len(REVIEW_HEADER) + 2, apply=True)
        meta = sheets.meta(wb, refresh=True)
    t = meta.tab(tab)
    sheets.clear(wb, tab, "A1:N5000", apply=True) if any(sheets.read(wb, tab, header_row=None, max_rows=1)["rows"]) else None
    w = sheets.write(wb, tab, "A1", rows, apply=True)
    n = len(rows)
    reqs = [{"setDataValidation": {"range": {"sheetId": t.gid, "startRowIndex": 1, "endRowIndex": n, "startColumnIndex": 11,
                                             "endColumnIndex": 12},
                                   "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": d} for d in DECISIONS]},
                                            "showCustomUi": True, "strict": False}}},
            {"updateSheetProperties": {"properties": {"sheetId": t.gid, "gridProperties": {"frozenRowCount": 1}},
                                       "fields": "gridProperties.frozenRowCount"}}]
    sheets.batch_update(wb, reqs, apply=True)
    return {"success": True, "applied": True, **plan, "write": {k: w.get(k) for k in ("cells_changed", "verified", "snapshot")}}


def collect(board: Board, sheets) -> Dict[str, dict]:
    """Decisions from the review tab: {diff id: {decision, custom, note}}."""
    r = sheets.read(board.sheet.workbook, board.sheet.review_tab, header_row=1, max_rows=5000, as_records=True)
    out = {}
    for rec in r["rows"]:
        if rec.get("Id") and rec.get("Decision"):
            out[rec["Id"]] = {"decision": rec["Decision"], "custom": rec.get("Custom value"), "note": rec.get("Note"),
                              "proposed": rec.get("Proposed")}
    return out


# ---------------------------------------------------------------------------
# applying decisions
# ---------------------------------------------------------------------------

def plan_writes(rep: Report, decisions: Dict[str, dict]) -> dict:
    """Turn decisions into per-target write lists. 'accept' takes the proposed
    value with the diff's apply_to; 'keep X' makes X the owner and writes the
    others; 'custom' uses the custom value everywhere; 'ignore' skips."""
    by_target: Dict[str, list] = {"sheet": [], "ioc": [], "altium": []}
    skipped, unknown = [], []
    by_id = {d.id: d for d in rep.diffs}
    for did, dec in decisions.items():
        d = by_id.get(did)
        if not d:
            unknown.append(did)
            continue
        choice = (dec.get("decision") or "").lower()
        if choice == "ignore" or not choice:
            skipped.append(did)
            continue
        if choice == "accept":
            writes = d.apply_to
        else:
            if choice == "custom":
                value = dec.get("custom")
            else:
                src = choice.replace("keep ", "")
                value = d.values.get(src)
            writes = []
            for a in d.apply_to:
                writes.append({**a, "value": value, "to": value})
            # the previous owner also gets the value when the user overrode it
            have = {(w["target"], w.get("field")) for w in writes}
            if d.field == "net" and value != d.values.get("sheet") and ("sheet", "net") not in have:
                writes.append({"target": "sheet", "field": "net", "pin": d.pin, "value": value})
            if d.field == "signal" and value != d.values.get("cubemx") and ("ioc", "Signal") not in have:
                writes.append({"target": "ioc", "field": "Signal", "pin": d.pin, "value": value})
        for w in writes:
            by_target.setdefault(w["target"], []).append({**w, "diff": did})
    return {"writes": by_target, "skipped": skipped, "unknown_ids": unknown,
            "counts": {k: len(v) for k, v in by_target.items()}}


def apply_sheet_writes(board: Board, sheets, writes: List[dict], apply: bool = False) -> dict:
    """Sheet writes go through update_by_key on the pins/signals table that
    holds the column for that role."""
    if not writes:
        return {"success": True, "applied": apply, "note": "no sheet writes"}
    results = []
    for which in ("pins", "signals"):
        spec = getattr(board.sheet, which)
        if not spec:
            continue
        updates = {}
        for w in writes:
            col = spec.columns.get(w["field"])
            if not col:
                continue
            updates.setdefault(w["pin"], {})[col] = w["value"] if w["value"] is not None else ""
        if updates:
            ups = [{"key": pin, "values": vals} for pin, vals in updates.items()]
            results.append({"table": which, **sheets.update_by_key(board.sheet.workbook, spec.tab, spec.key, ups,
                                                                    header_row=spec.header_row, apply=apply)})
    return {"success": True, "applied": apply, "tables": results}


def apply_ioc_writes(board: Board, writes: List[dict], out_path: Optional[str] = None) -> dict:
    """Write a reviewed COPY of the .ioc (never the original) and show the diff."""
    from .ioc import IocFile
    ioc = IocFile.load(board.ioc)
    before = ioc.text()
    ioc_pins = {naming.base_pin(p): p for p in ioc.pins()}
    changed = []
    for w in writes:
        pin = ioc_pins.get(w["pin"], w["pin"])
        if w["field"] == "GPIO_Label":
            ioc.set_pin(pin, label=w["value"] or "")
        elif w["field"] == "Signal":
            ioc.set_pin(pin, signal=w["value"] or "")
        changed.append((pin, w["field"], w["value"]))
    after = ioc.text()
    diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(), "original", "reconciled", lineterm="", n=0))
    out = Path(out_path) if out_path else None
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        ioc.save(out)
    return {"success": True, "changes": changed, "diff_lines": diff, "written_to": str(out) if out else None,
            "note": "the original .ioc is untouched; copy the reviewed file over it when satisfied"}


def altium_payloads(writes: List[dict]) -> dict:
    """Group Altium writes for the live step: set_net_labels renames per sheet,
    symbol label edits per symbol."""
    renames: Dict[str, Dict[str, str]] = {}
    symbol_edits: Dict[str, List[dict]] = {}
    for w in writes:
        if w.get("kind") == "net_label" and w.get("from") and w.get("to"):
            for sh in (w.get("sheets") or ["?"]):
                renames.setdefault(sh, {})[w["from"]] = w["to"]
        elif w.get("kind") == "symbol_label":
            symbol_edits.setdefault(w.get("symbol") or "?", []).append({"pin": w["pin"], "from": w.get("from"), "to": w.get("to")})
    return {"net_label_renames_by_sheet": renames, "symbol_label_edits": symbol_edits,
            "note": "apply with the altium MCP tools (set_net_labels per sheet; symbol edits need the symbol-edit tool)"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(board_path: str, json_out: Optional[str] = None, publish_tab: bool = False, sheets=None) -> Report:
    from . import manifest as mf
    from .sources import ioc_facts, sheet_facts, schematic_facts
    board = mf.load(board_path)
    diag = {}
    ioc, diag["ioc"] = ioc_facts(board)
    sh, diag["sheet"] = ({}, {"note": "no sheets client"})
    if board.sheet and sheets is not None:
        sh, diag["sheet"] = sheet_facts(board, sheets)
    sc, diag["schematic"] = schematic_facts(board)
    rep = reconcile(board, ioc, sh, sc, diag)
    if json_out:
        Path(json_out).write_text(rep.to_json(), encoding="utf-8")
    return rep


def _sheets_client():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from gsheets.auth import BACKUP_DIR, load_credentials
    from gsheets.backend import GoogleBackend
    from gsheets.core import Sheets
    return Sheets(GoogleBackend(load_credentials()), BACKUP_DIR)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reconcile CubeMX, the pinning sheet and Altium for one board")
    ap.add_argument("board", help="path to a board manifest JSON")
    ap.add_argument("--json", help="write the full report here")
    ap.add_argument("--no-sheet", action="store_true", help="skip the Google Sheet (offline)")
    ap.add_argument("--publish", action="store_true", help="write the Reconcile tab (asks nothing; use after the user agreed)")
    ap.add_argument("--limit", type=int, default=25)
    a = ap.parse_args(argv)
    sheets = None if a.no_sheet else _sheets_client()
    rep = run(a.board, a.json, sheets=sheets)
    print(format_report(rep, a.limit))
    if a.publish and sheets is not None:
        from . import manifest as mf
        print(json.dumps(publish(rep, mf.load(a.board), sheets, apply=True), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
