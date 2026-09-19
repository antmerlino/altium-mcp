"""What the tools do, independent of Google.

Reads are paged and trimmed. Every write is first turned into a Plan that
says, cell by cell, what would change; applying a plan snapshots the old
values to a local JSON file, writes, then reads the range back and reports
whether it holds what was asked. `restore` writes a snapshot back.

Values follow the API's rules: `None` in a written block leaves that cell
alone, "" clears it, a string starting with "=" is a formula when written
USER_ENTERED.
"""
from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from . import a1
from .backend import Backend, BackendError, XLSX

TabKey = Union[str, int, None]
_URL_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_URL_GID = re.compile(r"[#&?]gid=(\d+)")
_GID_WORD = re.compile(r"^gid[=:]\s*(\d+)$", re.I)
RENDERS = ("formatted", "unformatted", "formula")
INPUT_OPTIONS = ("USER_ENTERED", "RAW")


class SheetsError(Exception):
    pass


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------

@dataclass
class Tab:
    gid: int
    title: str
    index: int
    hidden: bool
    rows: int
    cols: int
    frozen_rows: int
    frozen_cols: int

    def summary(self) -> dict:
        return {"title": self.title, "gid": self.gid, "index": self.index, "rows": self.rows, "cols": self.cols,
                **({"hidden": True} if self.hidden else {}),
                **({"frozen_rows": self.frozen_rows} if self.frozen_rows else {}),
                **({"frozen_cols": self.frozen_cols} if self.frozen_cols else {})}


@dataclass
class Meta:
    sid: str
    title: str
    url: str
    tabs: List[Tab]

    def tab(self, key: TabKey, default_gid: Optional[int] = None) -> Tab:
        """By title (case-insensitive fallback), by gid (int or 'gid=123'),
        by 0-based index ('#2'); None -> the gid from the URL, else the first
        visible tab."""
        if key is None or key == "":
            if default_gid is not None:
                key = default_gid
            else:
                visible = [t for t in self.tabs if not t.hidden] or self.tabs
                if not visible:
                    raise SheetsError("spreadsheet has no tabs")
                return visible[0]
        if isinstance(key, bool):
            raise SheetsError(f"not a tab: {key!r}")
        if isinstance(key, int):
            for t in self.tabs:
                if t.gid == key:
                    return t
            raise SheetsError(f"no tab with gid {key}; tabs: {[t.title for t in self.tabs]}")
        text = str(key).strip()
        m = _GID_WORD.match(text)
        if m:
            return self.tab(int(m.group(1)))
        if text.startswith("#") and text[1:].isdigit():
            idx = int(text[1:])
            for t in self.tabs:
                if t.index == idx:
                    return t
            raise SheetsError(f"no tab at index {idx}")
        for t in self.tabs:
            if t.title == text:
                return t
        lowered = [t for t in self.tabs if t.title.lower() == text.lower()]
        if len(lowered) == 1:
            return lowered[0]
        if text.isdigit():
            return self.tab(int(text))
        raise SheetsError(f"no tab named {text!r}; tabs: {[t.title for t in self.tabs]}")

    def summary(self) -> dict:
        return {"spreadsheet": self.title, "id": self.sid, "url": self.url,
                "tabs": [t.summary() for t in self.tabs]}


def _parse_meta(raw: dict) -> Meta:
    tabs = []
    for s in raw.get("sheets", []):
        p = s.get("properties", {})
        g = p.get("gridProperties", {})
        tabs.append(Tab(int(p["sheetId"]), p["title"], int(p.get("index", len(tabs))), bool(p.get("hidden")),
                        int(g.get("rowCount", 0)), int(g.get("columnCount", 0)),
                        int(g.get("frozenRowCount", 0)), int(g.get("frozenColumnCount", 0))))
    tabs.sort(key=lambda t: t.index)
    return Meta(raw["spreadsheetId"], raw.get("properties", {}).get("title", ""), raw.get("spreadsheetUrl", ""), tabs)


# ---------------------------------------------------------------------------
# value helpers
# ---------------------------------------------------------------------------

def _same(a, b) -> bool:
    a = "" if a is None else a
    b = "" if b is None else b
    if isinstance(a, bool) or isinstance(b, bool):
        return str(a).upper() == str(b).upper()
    if a == "" or b == "":
        return a == b
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _is_formula(v) -> bool:
    return isinstance(v, str) and v.startswith("=")


def _trim(row: list) -> list:
    row = list(row)
    while row and (row[-1] is None or row[-1] == ""):
        row.pop()
    return row


def _cell_at(values: List[list], i: int, j: int):
    if i < len(values) and j < len(values[i]):
        return values[i][j]
    return ""


def _now() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# write plans
# ---------------------------------------------------------------------------

@dataclass
class Block:
    row: int                     # 1-based top-left
    col: int
    values: List[list]           # None = leave alone, "" = clear

    @property
    def nrows(self) -> int:
        return len(self.values)

    @property
    def ncols(self) -> int:
        return max((len(r) for r in self.values), default=0)

    def ref(self, tab: str) -> a1.Ref:
        return a1.block(tab, self.row, self.col, self.nrows, self.ncols)


@dataclass
class Plan:
    kind: str
    sid: str
    spreadsheet: str
    tab: Tab
    input_option: str
    blocks: List[Block]
    old: List[dict]                         # [{range, values}] FORMULA render, for the snapshot
    changes: List[dict] = field(default_factory=list)
    unchanged: int = 0
    skipped: int = 0
    formulas_overwritten: List[str] = field(default_factory=list)
    grow_rows: int = 0
    grow_cols: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def ranges(self) -> List[str]:
        return [b.ref(self.tab.title).a1() for b in self.blocks]

    def data(self) -> List[dict]:
        return [{"range": b.ref(self.tab.title).a1(), "values": b.values} for b in self.blocks]

    def preview(self, limit: int = 60) -> dict:
        cleared = sum(1 for c in self.changes if c["new"] == "")
        filled = sum(1 for c in self.changes if c["old"] == "" and c["new"] != "")
        out = {"success": True, "applied": False, "kind": self.kind, "spreadsheet": self.spreadsheet,
               "tab": self.tab.title, "gid": self.tab.gid, "input_option": self.input_option,
               "ranges": self.ranges if len(self.ranges) <= 12 else self.ranges[:12] + [f"... {len(self.ranges) - 12} more"],
               "cells_changed": len(self.changes), "cells_unchanged": self.unchanged, "cells_left_alone": self.skipped,
               "cells_filled": filled, "cells_cleared": cleared,
               "formulas_overwritten": self.formulas_overwritten[:20],
               "changes": self.changes[:limit]}
        if len(self.changes) > limit:
            out["changes_note"] = f"showing {limit} of {len(self.changes)} changes"
        if self.grow_rows or self.grow_cols:
            out["grid_grows_by"] = {"rows": self.grow_rows, "cols": self.grow_cols}
        if self.notes:
            out["notes"] = self.notes
        out["next"] = "call again with apply=true to write" if self.changes or self.kind == "restore" else "nothing would change"
        return out


# ---------------------------------------------------------------------------
# the facade
# ---------------------------------------------------------------------------

class Sheets:
    def __init__(self, backend: Backend, backup_dir: Union[str, Path], meta_ttl: float = 60.0):
        self.backend = backend
        self.backup_dir = Path(backup_dir)
        self.meta_ttl = meta_ttl
        self._meta: Dict[str, Tuple[float, Meta]] = {}

    # -- addressing -----------------------------------------------------------
    @staticmethod
    def resolve(spreadsheet: str) -> Tuple[str, Optional[int]]:
        """A URL (with optional #gid=), or a bare ID -> (id, gid or None)."""
        s = str(spreadsheet).strip()
        m = _URL_ID.search(s)
        if m:
            g = _URL_GID.search(s)
            return m.group(1), int(g.group(1)) if g else None
        if re.fullmatch(r"[A-Za-z0-9_-]{20,}", s):
            return s, None
        raise SheetsError(f"not a spreadsheet URL or ID: {spreadsheet!r} (use sheets_list to find it)")

    def meta(self, spreadsheet: str, refresh: bool = False) -> Meta:
        sid, _ = self.resolve(spreadsheet)
        hit = self._meta.get(sid)
        if hit and not refresh and time.time() - hit[0] < self.meta_ttl:
            return hit[1]
        meta = _parse_meta(self.backend.meta(sid))
        self._meta[sid] = (time.time(), meta)
        return meta

    def _forget(self, sid: str) -> None:
        self._meta.pop(sid, None)

    def _locate(self, spreadsheet: str, tab: TabKey, rng: Optional[str] = None) -> Tuple[Meta, Tab, a1.Ref]:
        """Meta, the tab, and the range (tab prefix inside `rng` wins)."""
        meta = self.meta(spreadsheet)
        _, url_gid = self.resolve(spreadsheet)
        ref = a1.parse(str(rng)) if rng not in (None, "") else a1.Ref()
        t = meta.tab(ref.tab if ref.tab is not None else tab, url_gid)
        return meta, t, ref.with_tab(t.title)

    def _clamp(self, t: Tab, ref: a1.Ref) -> a1.Ref:
        """The API rejects ranges past the grid; clamp what we read."""
        r1, c1 = ref.r1 or 1, ref.c1 or 1
        r2 = min(ref.r2, t.rows) if ref.r2 is not None else t.rows
        c2 = min(ref.c2, t.cols) if ref.c2 is not None else t.cols
        if r1 > t.rows or c1 > t.cols:
            return None
        return a1.Ref(t.title, r1, c1, max(r2, r1), max(c2, c1))

    def _values(self, sid: str, t: Tab, ref: a1.Ref, render: str) -> List[list]:
        clamped = self._clamp(t, ref)
        if clamped is None:
            return []
        return self.backend.get_values(sid, [clamped.a1()], render)[0]["values"]

    # -- tables -----------------------------------------------------------------
    @dataclass
    class Table:
        tab: "Tab"
        ref: a1.Ref
        origin_row: int
        origin_col: int
        header_row: Optional[int]        # sheet row number, None if no header
        header: List[str]
        rows: List[list]                 # data rows, aligned to header width where possible
        first_data_row: int

        def col(self, key: Union[str, int]) -> int:
            """0-based column offset for a header name (case-insensitive), a
            column letter, or a 1-based number."""
            if isinstance(key, int) and not isinstance(key, bool):
                idx = key - self.origin_col
                if idx < 0:
                    raise SheetsError(f"column {key} is left of the table")
                return idx
            text = str(key).strip()
            for i, h in enumerate(self.header):
                if h == text:
                    return i
            low = [i for i, h in enumerate(self.header) if h.lower() == text.lower()]
            if len(low) == 1:
                return low[0]
            if re.fullmatch(r"[A-Za-z]{1,3}", text):
                idx = a1.col_to_index(text) - self.origin_col
                if idx >= 0:
                    return idx
            raise SheetsError(f"no column {key!r}; headers: {[h for h in self.header if h]}")

        def letter(self, offset: int) -> str:
            return a1.index_to_col(self.origin_col + offset)

    def _table(self, sid: str, t: Tab, ref: a1.Ref, render: str, header_row: Union[str, int, None]) -> "Sheets.Table":
        values = self._values(sid, t, ref, render)
        origin_row, origin_col = ref.r1 or 1, ref.c1 or 1
        hdr_idx: Optional[int] = None
        if header_row == "auto":
            for i, row in enumerate(values[:10]):
                if sum(1 for v in row if v not in ("", None)) >= 2:
                    hdr_idx = i
                    break
            if hdr_idx is None and values:
                hdr_idx = 0
        elif header_row not in (None, 0, False, "none", "no"):
            hdr_idx = int(header_row) - origin_row
            if hdr_idx < 0 or hdr_idx >= max(len(values), 1):
                raise SheetsError(f"header_row {header_row} is outside the range read")
        if hdr_idx is None:
            header = []
            rows = values
            first = origin_row
        else:
            header = [str(v) if v is not None else "" for v in values[hdr_idx]]
            rows = values[hdr_idx + 1:]
            first = origin_row + hdr_idx + 1
        return Sheets.Table(t, ref, origin_row, origin_col, None if hdr_idx is None else origin_row + hdr_idx,
                            header, rows, first)

    # -- reading ----------------------------------------------------------------
    def open(self, spreadsheet: str, header_preview: bool = True) -> dict:
        meta = self.meta(spreadsheet, refresh=True)
        sid, url_gid = self.resolve(spreadsheet)
        out = meta.summary()
        if header_preview and meta.tabs:
            ranges = [a1.Ref(t.title, 1, 1, min(10, t.rows), min(30, t.cols)).a1() for t in meta.tabs if t.rows and t.cols]
            got = self.backend.get_values(sid, ranges, "formatted") if ranges else []
            by_title = {}
            for vr in got:
                tab_name = a1.parse(vr["range"]).tab
                by_title[tab_name] = vr["values"]
            for ts in out["tabs"]:
                vals = by_title.get(ts["title"], [])
                hdr = next(((i + 1, _trim(r)) for i, r in enumerate(vals) if sum(1 for v in r if v not in ("", None)) >= 2), None)
                if hdr:
                    ts["header_row"] = hdr[0]
                    ts["header"] = [str(v) for v in hdr[1]][:30]
                else:
                    ts["header"] = []
                ts["empty"] = not any(_trim(r) for r in vals)
        if url_gid is not None:
            out["linked_tab"] = next((t.title for t in meta.tabs if t.gid == url_gid), None)
        return out

    def read(self, spreadsheet: str, tab: TabKey = None, rng: Optional[str] = None, render: str = "formatted",
             header_row: Union[str, int, None] = "auto", max_rows: int = 200, max_cols: int = 50, offset: int = 0,
             columns: Optional[Sequence[Union[str, int]]] = None, as_records: bool = False,
             skip_empty_rows: bool = False) -> dict:
        if render not in RENDERS:
            raise SheetsError(f"render must be one of {RENDERS}")
        meta, t, ref = self._locate(spreadsheet, tab, rng)
        sid = meta.sid
        table = self._table(sid, t, ref, render, header_row)
        sel = list(range(min(max(len(table.header), max((len(r) for r in table.rows), default=0)), 10_000)))
        if columns:
            sel = [table.col(c) for c in columns]
        sel = sel[:max_cols]
        header = [table.header[i] if i < len(table.header) else table.letter(i) for i in sel]
        numbered = [(table.first_data_row + i, r) for i, r in enumerate(table.rows)]
        if skip_empty_rows:
            numbered = [(n, r) for n, r in numbered if _trim(r)]
        total = len(numbered)
        page = numbered[offset:offset + max_rows]
        rows_out = []
        for n, r in page:
            picked = [r[i] if i < len(r) else "" for i in sel]
            if as_records:
                rec = {"_row": n}
                rec.update({header[k]: v for k, v in enumerate(picked) if v not in ("", None)})
                rows_out.append(rec)
            else:
                rows_out.append(_trim(picked))
        out = {"success": True, "spreadsheet": meta.title, "tab": t.title, "gid": t.gid, "range": ref.a1(),
               "render": render, "header_row": table.header_row,
               "columns": [{"header": h, "letter": table.letter(i)} for h, i in zip(header, sel)],
               "first_row": page[0][0] if page else None, "rows": rows_out,
               "returned": len(rows_out), "total_data_rows": total, "truncated": offset + len(page) < total}
        if out["truncated"]:
            out["next_offset"] = offset + len(page)
        if not as_records:
            out["note"] = "rows are consecutive sheet rows starting at first_row; trailing empty cells trimmed"
        return out

    def find(self, spreadsheet: str, query: str, tab: TabKey = None, regex: bool = False, match_case: bool = False,
             in_formulas: bool = False, max_results: int = 50) -> dict:
        meta = self.meta(spreadsheet)
        sid, url_gid = self.resolve(spreadsheet)
        tabs = [meta.tab(tab, url_gid)] if tab not in (None, "") else [t for t in meta.tabs if t.rows and t.cols]
        flags = 0 if match_case else re.I
        pat = re.compile(str(query) if regex else re.escape(str(query)), flags)
        ranges = [a1.Ref(t.title).a1() for t in tabs]
        got = self.backend.get_values(sid, ranges, "formula" if in_formulas else "formatted")
        hits, scanned = [], 0
        for t, vr in zip(tabs, got):
            for i, row in enumerate(vr["values"], start=1):
                for j, v in enumerate(row, start=1):
                    if v in ("", None):
                        continue
                    scanned += 1
                    if pat.search(str(v)):
                        hits.append({"cell": a1.Ref(t.title, i, j, i, j).a1(), "row": i, "col": a1.index_to_col(j),
                                     "value": v, "row_values": _trim(row)[:12]})
                        if len(hits) >= max_results:
                            break
                if len(hits) >= max_results:
                    break
            if len(hits) >= max_results:
                break
        return {"success": True, "spreadsheet": meta.title, "query": query, "regex": regex,
                "tabs_searched": [t.title for t in tabs], "cells_scanned": scanned, "matches": hits,
                "truncated": len(hits) >= max_results}

    def lookup(self, spreadsheet: str, tab: TabKey, key_column: Union[str, int], keys: Sequence,
               value_columns: Optional[Sequence[Union[str, int]]] = None, header_row: Union[str, int, None] = "auto",
               rng: Optional[str] = None) -> dict:
        meta, t, ref = self._locate(spreadsheet, tab, rng)
        table = self._table(meta.sid, t, ref, "formatted", header_row)
        k = table.col(key_column)
        vcols = [table.col(c) for c in value_columns] if value_columns else list(range(len(table.header)))
        index: Dict[str, List[int]] = {}
        for i, r in enumerate(table.rows):
            v = r[k] if k < len(r) else ""
            if v not in ("", None):
                index.setdefault(str(v).strip(), []).append(i)
        found, missing, ambiguous = {}, [], {}
        for key in keys:
            skey = str(key).strip()
            rows = index.get(skey)
            if not rows:
                missing.append(key)
                continue
            if len(rows) > 1:
                ambiguous[skey] = [table.first_data_row + i for i in rows]
            i = rows[0]
            r = table.rows[i]
            found[skey] = {"_row": table.first_data_row + i,
                           **{(table.header[c] if c < len(table.header) and table.header[c] else table.letter(c)):
                              (r[c] if c < len(r) else "") for c in vcols}}
        return {"success": True, "spreadsheet": meta.title, "tab": t.title, "key_column": table.header[k] if k < len(table.header) else table.letter(k),
                "found": found, "missing": missing, "ambiguous_keys": ambiguous}

    # -- planning writes --------------------------------------------------------
    def _plan(self, kind: str, meta: Meta, t: Tab, blocks: List[Block], input_option: str,
              notes: Optional[List[str]] = None) -> Plan:
        if input_option not in INPUT_OPTIONS:
            raise SheetsError(f"input_option must be one of {INPUT_OPTIONS}")
        blocks = [b for b in blocks if b.nrows and b.ncols]
        for b in blocks:
            b.values = [list(r) + [None] * (b.ncols - len(r)) for r in b.values]
        plan = Plan(kind, meta.sid, meta.title, t, input_option, blocks, [], notes=list(notes or []))
        if not blocks:
            return plan
        max_row = max(b.row + b.nrows - 1 for b in blocks)
        max_col = max(b.col + b.ncols - 1 for b in blocks)
        plan.grow_rows = max(0, max_row - t.rows)
        plan.grow_cols = max(0, max_col - t.cols)
        read_refs = [self._clamp(t, b.ref(t.title)) for b in blocks]
        got = self.backend.get_values(meta.sid, [r.a1() for r in read_refs if r is not None], "formula")
        got_iter = iter(got)
        for b, rr in zip(blocks, read_refs):
            old = next(got_iter)["values"] if rr is not None else []
            plan.old.append({"range": b.ref(t.title).a1(), "values": old})
            for i, row in enumerate(b.values):
                for j, new in enumerate(row):
                    if new is None:
                        plan.skipped += 1
                        continue
                    o = _cell_at(old, i, j)
                    if _same(o, new):
                        plan.unchanged += 1
                        continue
                    cell = a1.Ref(t.title, b.row + i, b.col + j, b.row + i, b.col + j).a1()
                    plan.changes.append({"cell": cell, "old": o, "new": new})
                    if _is_formula(o):
                        plan.formulas_overwritten.append(cell)
        return plan

    def _snapshot_file(self, meta: Meta, t: Optional[Tab], kind: str, blocks: List[dict]) -> str:
        folder = self.backup_dir / meta.sid
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", t.title if t else "workbook")[:40]
        path = folder / f"{_now()}_{kind}_{safe}.json"
        path.write_text(json.dumps({"spreadsheet_id": meta.sid, "spreadsheet": meta.title, "taken": datetime.now().isoformat(timespec="seconds"),
                                    "kind": kind, "render": "formula", "blocks": blocks}, ensure_ascii=False, indent=0),
                        encoding="utf-8")
        return str(path)

    def apply(self, plan: Plan) -> dict:
        if not plan.changes and plan.kind != "restore":
            return {**plan.preview(), "applied": False, "note": "nothing to write"}
        snapshot = self._snapshot_file(Meta(plan.sid, plan.spreadsheet, "", []), plan.tab, plan.kind, plan.old)
        if plan.grow_rows or plan.grow_cols:
            reqs = []
            if plan.grow_rows:
                reqs.append({"appendDimension": {"sheetId": plan.tab.gid, "dimension": "ROWS", "length": plan.grow_rows}})
            if plan.grow_cols:
                reqs.append({"appendDimension": {"sheetId": plan.tab.gid, "dimension": "COLUMNS", "length": plan.grow_cols}})
            self.backend.batch_update(plan.sid, reqs)
            self._forget(plan.sid)
        result = self.backend.update_values(plan.sid, plan.data(), plan.input_option)
        # read back what we wrote
        back = self.backend.get_values(plan.sid, plan.ranges, "formula")
        mismatches = []
        for b, vr in zip(plan.blocks, back):
            for i, row in enumerate(b.values):
                for j, new in enumerate(row):
                    if new is None:
                        continue
                    got = _cell_at(vr["values"], i, j)
                    if not _same(got, new) and not (plan.input_option == "USER_ENTERED" and str(got).upper() == str(new).upper()):
                        mismatches.append({"cell": a1.Ref(plan.tab.title, b.row + i, b.col + j, b.row + i, b.col + j).a1(),
                                           "asked": new, "sheet_holds": got})
        return {"success": True, "applied": True, "kind": plan.kind, "spreadsheet": plan.spreadsheet, "tab": plan.tab.title,
                "cells_changed": len(plan.changes), "updated_cells_reported": result.get("totalUpdatedCells"),
                "formulas_overwritten": plan.formulas_overwritten[:20],
                "verified": not mismatches, "mismatches": mismatches[:20],
                "snapshot": snapshot, "undo": f"sheets_restore(path={snapshot!r})",
                "changes": plan.changes[:60]}

    def _run(self, plan: Plan, apply: bool) -> dict:
        return self.apply(plan) if apply else plan.preview()

    # -- write tools ------------------------------------------------------------
    def write(self, spreadsheet: str, tab: TabKey, rng: str, values: List[list], input_option: str = "USER_ENTERED",
              apply: bool = False) -> dict:
        if not isinstance(values, list) or not values or not all(isinstance(r, list) for r in values):
            raise SheetsError("values must be a non-empty list of rows (lists)")
        meta, t, ref = self._locate(spreadsheet, tab, rng)
        if ref.r1 is None or ref.c1 is None:
            raise SheetsError("range must start at a cell, e.g. B2 or B2:D10")
        nrows, ncols = len(values), max(len(r) for r in values)
        notes = []
        if not ref.is_cell and ref.r2 is not None and ref.c2 is not None:
            span_r, span_c = ref.r2 - ref.r1 + 1, ref.c2 - ref.c1 + 1
            if nrows > span_r or ncols > span_c:
                raise SheetsError(f"values are {nrows}x{ncols} but the range {ref.a1()} is {span_r}x{span_c}; "
                                  "give just the top-left cell to size the block from the values")
            if nrows < span_r or ncols < span_c:
                notes.append(f"values fill the top-left {nrows}x{ncols} of {ref.a1()}; the rest is left alone")
        block = Block(ref.r1, ref.c1, values)
        return self._run(self._plan("write", meta, t, [block], input_option, notes), apply)

    def update_cells(self, spreadsheet: str, tab: TabKey, cells: Dict[str, Any], input_option: str = "USER_ENTERED",
                     apply: bool = False) -> dict:
        if not isinstance(cells, dict) or not cells:
            raise SheetsError("cells must be a non-empty mapping like {\"B7\": \"PA0\", \"C7\": 12}")
        meta, t, _ = self._locate(spreadsheet, tab)
        blocks = []
        for key, value in cells.items():
            ref = a1.parse(str(key))
            if ref.tab and ref.tab != t.title:
                raise SheetsError(f"cell {key!r} names another tab; one tab per call")
            if not ref.is_cell:
                raise SheetsError(f"{key!r} is not a single cell")
            blocks.append(Block(ref.r1, ref.c1, [[value]]))
        return self._run(self._plan("update_cells", meta, t, blocks, input_option), apply)

    def update_by_key(self, spreadsheet: str, tab: TabKey, key_column: Union[str, int], updates: List[dict],
                      header_row: Union[str, int, None] = "auto", rng: Optional[str] = None, add_missing: bool = False,
                      all_matches: bool = False, input_option: str = "USER_ENTERED", apply: bool = False) -> dict:
        if not isinstance(updates, list) or not updates or not all(isinstance(u, dict) for u in updates):
            raise SheetsError("updates must be a list of objects: {\"key\": \"PA0\", \"values\": {\"Net Name\": \"...\"}} "
                              "or flat {\"Pin\": \"PA0\", \"Net Name\": \"...\"}")
        meta, t, ref = self._locate(spreadsheet, tab, rng)
        table = self._table(meta.sid, t, ref, "formatted", header_row)
        k = table.col(key_column)
        key_header = table.header[k] if k < len(table.header) else table.letter(k)
        index: Dict[str, List[int]] = {}
        for i, r in enumerate(table.rows):
            v = r[k] if k < len(r) else ""
            if v not in ("", None):
                index.setdefault(str(v).strip(), []).append(i)
        blocks: List[Block] = []
        missing, ambiguous, notes = [], {}, []
        appended_rows: List[Tuple[str, dict]] = []
        for u in updates:
            if "key" in u and isinstance(u.get("values"), dict):
                key, vals = u["key"], u["values"]
            else:
                flat = dict(u)
                match = next((kk for kk in flat if str(kk).strip().lower() == key_header.strip().lower()), None)
                if match is None:
                    raise SheetsError(f"update {u!r} has no key: give {{'key': ..., 'values': {{...}}}} or include the key column {key_header!r}")
                key = flat.pop(match)
                vals = flat
            skey = str(key).strip()
            rows = index.get(skey, [])
            if not rows:
                missing.append(skey)
                if add_missing:
                    appended_rows.append((skey, vals))
                continue
            if len(rows) > 1:
                ambiguous[skey] = [table.first_data_row + i for i in rows]
                if not all_matches:
                    continue
            for i in (rows if all_matches else rows[:1]):
                for col_key, value in vals.items():
                    c = table.col(col_key)
                    blocks.append(Block(table.first_data_row + i, table.origin_col + c, [[value]]))
        if ambiguous and not all_matches:
            notes.append(f"keys matching several rows were skipped (pass all_matches=true to update them all): {ambiguous}")
        if missing and not add_missing:
            notes.append(f"keys not found (pass add_missing=true to append them): {missing}")
        if appended_rows:
            width = len(table.header)
            last = table.first_data_row + len(table.rows)
            for n, (skey, vals) in enumerate(appended_rows):
                row = [None] * width
                row[k] = skey
                for col_key, value in vals.items():
                    c = table.col(col_key)
                    if c >= width:
                        row += [None] * (c - width + 1)
                        width = c + 1
                    row[c] = value
                blocks.append(Block(last + n, table.origin_col, [row]))
            notes.append(f"{len(appended_rows)} new row(s) appended at row {last}")
        plan = self._plan("update_by_key", meta, t, blocks, input_option, notes)
        out = self._run(plan, apply)
        out["key_column"] = key_header
        out["missing_keys"] = missing
        out["ambiguous_keys"] = ambiguous
        return out

    def append_rows(self, spreadsheet: str, tab: TabKey, rows: List[Union[list, dict]],
                    header_row: Union[str, int, None] = "auto", input_option: str = "USER_ENTERED",
                    apply: bool = False) -> dict:
        if not isinstance(rows, list) or not rows:
            raise SheetsError("rows must be a non-empty list of lists or of {header: value} objects")
        meta, t, ref = self._locate(spreadsheet, tab)
        table = self._table(meta.sid, t, ref, "formatted", header_row)
        last = table.first_data_row + len(table.rows)
        width = len(table.header)
        out_rows = []
        for r in rows:
            if isinstance(r, dict):
                row = [None] * width
                for col_key, value in r.items():
                    c = table.col(col_key)
                    if c >= len(row):
                        row += [None] * (c - len(row) + 1)
                    row[c] = value
                out_rows.append(row)
            else:
                out_rows.append(list(r))
        plan = self._plan("append_rows", meta, t, [Block(last, table.origin_col, out_rows)], input_option,
                          [f"appending {len(out_rows)} row(s) at row {last}"])
        return self._run(plan, apply)

    def clear(self, spreadsheet: str, tab: TabKey, rng: str, apply: bool = False) -> dict:
        meta, t, ref = self._locate(spreadsheet, tab, rng)
        if ref.whole_tab:
            raise SheetsError("refusing to clear a whole tab in one call; give a range")
        old = self._values(meta.sid, t, ref, "formula")
        cells = [(i, j, v) for i, r in enumerate(old) for j, v in enumerate(r) if v not in ("", None)]
        r1, c1 = ref.r1 or 1, ref.c1 or 1
        sample = [{"cell": a1.Ref(t.title, r1 + i, c1 + j, r1 + i, c1 + j).a1(), "old": v} for i, j, v in cells[:60]]
        preview = {"success": True, "applied": False, "kind": "clear", "spreadsheet": meta.title, "tab": t.title,
                   "range": ref.a1(), "cells_cleared": len(cells), "formulas_cleared": sum(1 for _, _, v in cells if _is_formula(v)),
                   "sample": sample, "next": "call again with apply=true to clear" if cells else "range is already empty"}
        if not apply or not cells:
            return preview
        snapshot = self._snapshot_file(meta, t, "clear", [{"range": ref.a1(), "values": old}])
        self.backend.clear_values(meta.sid, [ref.a1()])
        left = self._values(meta.sid, t, ref, "formula")
        remaining = sum(1 for r in left for v in r if v not in ("", None))
        return {**preview, "applied": True, "verified": remaining == 0, "cells_still_holding_values": remaining,
                "snapshot": snapshot, "undo": f"sheets_restore(path={snapshot!r})"}

    def find_replace(self, spreadsheet: str, query: str, replacement: str, tab: TabKey = None, regex: bool = False,
                     match_case: bool = False, in_formulas: bool = False, whole_cell: bool = False,
                     input_option: str = "USER_ENTERED", max_changes: int = 500, apply: bool = False) -> dict:
        meta = self.meta(spreadsheet)
        _, url_gid = self.resolve(spreadsheet)
        tabs = [meta.tab(tab, url_gid)] if tab not in (None, "") else [t for t in meta.tabs if t.rows and t.cols]
        if len(tabs) > 1 and apply:
            raise SheetsError("apply one tab at a time (pass tab=...); preview may span the workbook")
        flags = 0 if match_case else re.I
        pat = re.compile((str(query) if regex else re.escape(str(query))) if not whole_cell
                         else "^" + (str(query) if regex else re.escape(str(query))) + "$", flags)
        got = self.backend.get_values(meta.sid, [a1.Ref(t.title).a1() for t in tabs], "formula")
        per_tab: Dict[str, List[Block]] = {}
        skipped_formulas = 0
        total = 0
        for t, vr in zip(tabs, got):
            for i, row in enumerate(vr["values"], start=1):
                for j, v in enumerate(row, start=1):
                    if v in ("", None) or not isinstance(v, str):
                        continue
                    if _is_formula(v) and not in_formulas:
                        if pat.search(v):
                            skipped_formulas += 1
                        continue
                    if pat.search(v):
                        new = pat.sub(str(replacement), v)
                        if new != v:
                            per_tab.setdefault(t.title, []).append(Block(i, j, [[new]]))
                            total += 1
                            if total >= max_changes:
                                break
                if total >= max_changes:
                    break
        if not per_tab:
            return {"success": True, "applied": False, "kind": "find_replace", "spreadsheet": meta.title,
                    "query": query, "replacement": replacement, "cells_changed": 0,
                    "formula_cells_skipped": skipped_formulas, "next": "nothing matches"}
        if len(per_tab) > 1:
            previews = {name: self._plan("find_replace", meta, meta.tab(name), blocks, input_option).preview(30)
                        for name, blocks in per_tab.items()}
            return {"success": True, "applied": False, "kind": "find_replace", "spreadsheet": meta.title,
                    "query": query, "replacement": replacement, "per_tab": previews,
                    "formula_cells_skipped": skipped_formulas, "truncated": total >= max_changes,
                    "next": "apply per tab: call again with tab=<name> and apply=true"}
        (name, blocks), = per_tab.items()
        plan = self._plan("find_replace", meta, meta.tab(name), blocks, input_option,
                          [f"{skipped_formulas} formula cell(s) also match; pass in_formulas=true to include them"] if skipped_formulas else [])
        out = self._run(plan, apply)
        out.update({"query": query, "replacement": replacement, "truncated": total >= max_changes})
        return out

    # -- structure --------------------------------------------------------------
    def tabs(self, spreadsheet: str, action: str, tab: TabKey = None, title: Optional[str] = None,
             index: Optional[int] = None, rows: Optional[int] = None, cols: Optional[int] = None,
             apply: bool = False) -> dict:
        meta = self.meta(spreadsheet, refresh=True)
        _, url_gid = self.resolve(spreadsheet)
        action = str(action).lower()
        req: dict
        if action == "create":
            if not title:
                raise SheetsError("create needs title")
            if any(t.title == title for t in meta.tabs):
                raise SheetsError(f"a tab named {title!r} already exists")
            props = {"title": str(title)}
            if index is not None:
                props["index"] = int(index)
            if rows or cols:
                props["gridProperties"] = {"rowCount": int(rows or 1000), "columnCount": int(cols or 26)}
            req = {"addSheet": {"properties": props}}
            describe = f"create tab {title!r}"
        else:
            t = meta.tab(tab, url_gid)
            if action == "rename":
                if not title:
                    raise SheetsError("rename needs title")
                req = {"updateSheetProperties": {"properties": {"sheetId": t.gid, "title": str(title)}, "fields": "title"}}
                describe = f"rename {t.title!r} -> {title!r}"
            elif action == "duplicate":
                body = {"sourceSheetId": t.gid, "newSheetName": str(title or f"Copy of {t.title}")}
                if index is not None:
                    body["insertSheetIndex"] = int(index)
                req = {"duplicateSheet": body}
                describe = f"duplicate {t.title!r} as {body['newSheetName']!r}"
            elif action == "move":
                if index is None:
                    raise SheetsError("move needs index (0-based)")
                req = {"updateSheetProperties": {"properties": {"sheetId": t.gid, "index": int(index)}, "fields": "index"}}
                describe = f"move {t.title!r} to index {index}"
            elif action in ("hide", "show"):
                req = {"updateSheetProperties": {"properties": {"sheetId": t.gid, "hidden": action == "hide"}, "fields": "hidden"}}
                describe = f"{action} {t.title!r}"
            elif action == "delete":
                raise SheetsError("deleting tabs is deliberately not offered; do it in the Sheets UI (version history can undo it there)")
            else:
                raise SheetsError("action must be create | rename | duplicate | move | hide | show")
        if not apply:
            return {"success": True, "applied": False, "kind": "tabs", "spreadsheet": meta.title, "action": action,
                    "would": describe, "request": req, "next": "call again with apply=true"}
        reply = self.backend.batch_update(meta.sid, [req])
        self._forget(meta.sid)
        return {"success": True, "applied": True, "kind": "tabs", "spreadsheet": meta.title, "action": action,
                "did": describe, "reply": reply.get("replies", [{}])[0], "tabs": [t.summary() for t in self.meta(spreadsheet, refresh=True).tabs]}

    def dimensions(self, spreadsheet: str, tab: TabKey, action: str, start: int, count: int = 1,
                   apply: bool = False) -> dict:
        meta, t, _ = self._locate(spreadsheet, tab)
        action = str(action).lower()
        if action not in ("insert_rows", "insert_cols", "delete_rows", "delete_cols"):
            raise SheetsError("action must be insert_rows | insert_cols | delete_rows | delete_cols")
        start, count = int(start), int(count)
        if start < 1 or count < 1:
            raise SheetsError("start is 1-based and count >= 1")
        dim = "ROWS" if action.endswith("rows") else "COLUMNS"
        limit = t.rows if dim == "ROWS" else t.cols
        if start > limit + (1 if action.startswith("insert") else 0):
            raise SheetsError(f"start {start} is past the grid ({limit} {dim.lower()})")
        rng = {"sheetId": t.gid, "dimension": dim, "startIndex": start - 1, "endIndex": start - 1 + count}
        out = {"success": True, "applied": False, "kind": "dimensions", "spreadsheet": meta.title, "tab": t.title,
               "action": action, "start": start, "count": count}
        old = None
        if action.startswith("delete"):
            ref = a1.Ref(t.title, start, None, start + count - 1, None) if dim == "ROWS" else \
                a1.Ref(t.title, None, start, None, start + count - 1)
            old = self._values(meta.sid, t, ref, "formula")
            held = sum(1 for r in old for v in r if v not in ("", None))
            out.update({"cells_with_values_removed": held,
                        "removed_preview": [_trim(r)[:12] for r in old[:15]],
                        "warning": "the cells below/right shift up/left; formulas elsewhere referencing them adjust as in the UI"})
            req = {"deleteDimension": {"range": rng}}
            describe = f"delete {count} {dim.lower()} starting at {start}"
        else:
            req = {"insertDimension": {"range": rng, "inheritFromBefore": start > 1}}
            describe = f"insert {count} {dim.lower()} before {dim.lower()[:-1]} {start}"
        out["would"] = describe
        if not apply:
            out["next"] = "call again with apply=true"
            return out
        snapshot = None
        if old is not None:
            ref_a1 = (a1.Ref(t.title, start, 1, start + count - 1, t.cols) if dim == "ROWS"
                      else a1.Ref(t.title, 1, start, t.rows, start + count - 1)).a1()
            snapshot = self._snapshot_file(meta, t, action, [{"range": ref_a1, "values": old}])
        self.backend.batch_update(meta.sid, [req])
        self._forget(meta.sid)
        t2 = self.meta(spreadsheet, refresh=True).tab(t.gid)
        out.update({"applied": True, "did": describe, "tab_now": t2.summary(), "next": None})
        if snapshot:
            out["snapshot"] = snapshot
            out["undo_hint"] = "re-insert the dimension at the same place, then sheets_restore the snapshot"
        return out

    def batch_update(self, spreadsheet: str, requests: List[dict], apply: bool = False) -> dict:
        if not isinstance(requests, list) or not requests or not all(isinstance(r, dict) and len(r) == 1 for r in requests):
            raise SheetsError("requests must be a list of single-key Sheets API request objects")
        meta = self.meta(spreadsheet)
        kinds = [next(iter(r)) for r in requests]
        forbidden = [k for k in kinds if k in ("deleteSheet",)]
        if forbidden:
            raise SheetsError(f"{forbidden} is not allowed through this tool")
        if not apply:
            return {"success": True, "applied": False, "kind": "batch_update", "spreadsheet": meta.title,
                    "requests": kinds, "count": len(requests), "next": "call again with apply=true; no cell-level preview is possible for raw requests"}
        reply = self.backend.batch_update(meta.sid, requests)
        self._forget(meta.sid)
        return {"success": True, "applied": True, "kind": "batch_update", "spreadsheet": meta.title, "requests": kinds,
                "replies": reply.get("replies", [])}

    # -- snapshots ----------------------------------------------------------------
    def snapshot(self, spreadsheet: str, tab: TabKey = None, rng: Optional[str] = None) -> dict:
        meta = self.meta(spreadsheet)
        _, url_gid = self.resolve(spreadsheet)
        if tab in (None, "") and not rng:
            tabs = [t for t in meta.tabs if t.rows and t.cols]
            refs = [a1.Ref(t.title) for t in tabs]
            label = None
        else:
            _, t, ref = self._locate(spreadsheet, tab, rng)
            tabs, refs, label = [t], [ref], t
        got = self.backend.get_values(meta.sid, [r.a1() for r in refs], "formula")
        blocks = [{"range": r.a1(), "values": vr["values"]} for r, vr in zip(refs, got)]
        path = self._snapshot_file(meta, label, "snapshot", blocks)
        return {"success": True, "spreadsheet": meta.title, "path": path, "tabs": [t.title for t in tabs],
                "cells": sum(len(r) for b in blocks for r in b["values"])}

    def restore(self, path: str, apply: bool = False) -> dict:
        p = Path(path)
        if not p.exists():
            raise SheetsError(f"no snapshot at {path}")
        snap = json.loads(p.read_text(encoding="utf-8"))
        sid = snap["spreadsheet_id"]
        meta = self.meta(sid, refresh=True)
        results = []
        for b in snap["blocks"]:
            ref = a1.parse(b["range"])
            t = meta.tab(ref.tab)
            r1, c1 = ref.r1 or 1, ref.c1 or 1
            # read what the block covers NOW (open-ended stays open, so cells
            # written since the snapshot are seen and cleared by the restore)
            current = self._values(sid, t, a1.Ref(t.title, r1, c1, ref.r2, ref.c2), "formula")
            nrows = max(len(b["values"]), len(current), 1)
            ncols = max(max((len(r) for r in b["values"]), default=0), max((len(r) for r in current), default=0), 1)
            values = [[_cell_at(b["values"], i, j) for j in range(ncols)] for i in range(nrows)]
            plan = self._plan("restore", meta, t, [Block(r1, c1, values)], "USER_ENTERED",
                              [f"restoring snapshot taken {snap.get('taken')} ({snap.get('kind')})"])
            results.append(self._run(plan, apply))
        return {"success": True, "applied": apply, "kind": "restore", "snapshot": str(p), "taken": snap.get("taken"),
                "blocks": results}

    # -- files ---------------------------------------------------------------------
    def export(self, spreadsheet: str, path: str, fmt: str = "xlsx", tab: TabKey = None) -> dict:
        meta = self.meta(spreadsheet)
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        fmt = fmt.lower()
        if fmt == "xlsx":
            data = self.backend.export(meta.sid, XLSX)
            out.write_bytes(data)
            return {"success": True, "spreadsheet": meta.title, "path": str(out), "bytes": len(data), "format": "xlsx",
                    "tabs": [t.title for t in meta.tabs], "note": "tab names are preserved in the workbook"}
        if fmt == "csv":
            _, t, ref = self._locate(spreadsheet, tab)
            values = self._values(meta.sid, t, ref, "formatted")
            with out.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(values)
            return {"success": True, "spreadsheet": meta.title, "tab": t.title, "path": str(out), "rows": len(values), "format": "csv"}
        raise SheetsError("format must be xlsx or csv")

    def list(self, query: Optional[str] = None, folder: Optional[str] = None, limit: int = 25) -> dict:
        files = self.backend.list_spreadsheets(query, folder, limit)
        return {"success": True, "count": len(files),
                "spreadsheets": [{"title": f.get("name"), "id": f.get("id"), "url": f.get("webViewLink"),
                                  "modified": f.get("modifiedTime"),
                                  **({"owner": f["owners"][0].get("emailAddress")} if f.get("owners") else {}),
                                  **({"shared_drive": f["driveId"]} if f.get("driveId") else {})} for f in files]}

    def whoami(self) -> dict:
        return self.backend.whoami()
