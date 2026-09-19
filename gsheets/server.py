"""The MCP server: thin tools over gsheets.core.Sheets.

Run:  python -m gsheets.server      (stdio; register it in Claude's config)
Logs go to stderr and GSHEETS_HOME/server.log, never stdout.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.fastmcp import FastMCP          # noqa: E402

from gsheets import auth                          # noqa: E402
from gsheets.backend import BackendError          # noqa: E402
from gsheets.core import Sheets, SheetsError      # noqa: E402

log = logging.getLogger("gsheets")
mcp = FastMCP("google-sheets")
_state: Dict[str, Any] = {"sheets": None}

TabArg = Union[str, int, None]
Text = Union[str, int, float]


def _sheets() -> Sheets:
    if _state["sheets"] is None:
        from gsheets.backend import GoogleBackend
        creds = auth.load_credentials(interactive=False)
        _state["sheets"] = Sheets(GoogleBackend(creds), auth.BACKUP_DIR)
    return _state["sheets"]


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _guard(fn):
    try:
        return _json(fn())
    except (SheetsError, BackendError, auth.AuthError) as e:
        log.warning("tool error: %s", e)
        return _json({"success": False, "error": str(e)})
    except Exception as e:                       # pragma: no cover
        log.exception("unexpected")
        return _json({"success": False, "error": f"{type(e).__name__}: {e}"})


def _s(v: Optional[Text]) -> Optional[str]:
    return None if v is None else str(v)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

@mcp.tool()
def sheets_auth_status() -> str:
    """Which Google credentials the server would use, as whom it acts, and what
    to do if it cannot. Call this first when anything fails with an auth error."""
    def run():
        st = auth.status()
        if st["method"] in ("service_account", "oauth_token"):
            try:
                st["acting_as"] = _sheets().whoami()
            except (auth.AuthError, BackendError) as e:
                st["acting_as"] = None
                st["error"] = str(e)
        return {"success": True, **st}
    return _guard(run)


@mcp.tool()
def sheets_list(query: Optional[Text] = None, folder_id: Optional[str] = None, limit: int = 25) -> str:
    """Find spreadsheets by title substring and/or Drive folder id, newest first,
    shared drives included. Returns title, id, url, modified time."""
    return _guard(lambda: _sheets().list(_s(query), folder_id, int(limit)))


@mcp.tool()
def sheets_open(spreadsheet: str, header_preview: bool = True) -> str:
    """Orient in a workbook: title, url and every tab with gid, index, size,
    frozen rows, detected header row and header cells. `spreadsheet` is a URL
    (a #gid= in it names the linked tab) or an id. Cheap; call before reading."""
    return _guard(lambda: _sheets().open(spreadsheet, header_preview))


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

@mcp.tool()
def sheets_read(spreadsheet: str, tab: TabArg = None, range: Optional[str] = None, render: str = "formatted",
                header_row: Union[str, int, None] = "auto", max_rows: int = 200, max_cols: int = 50, offset: int = 0,
                columns: Optional[List[Union[str, int]]] = None, as_records: bool = False,
                skip_empty_rows: bool = False) -> str:
    """Read a tab or a range as a compact table, paged.

    tab: title, gid (int or "gid=123"), "#index", or omitted (the URL's gid,
    else the first visible tab). range: A1 like "B2:F" (a tab prefix inside it
    wins). render: formatted (what you see) | unformatted (raw numbers, date
    serials) | formula (=... for formula cells). header_row: "auto" (first row
    with 2+ filled cells within the top 10), a row number, or null for none.
    columns: header names or letters to keep. as_records: rows as {header:
    value} dicts with _row. Output: header/letter per column, first_row, rows
    (consecutive sheet rows, trailing empties trimmed), total_data_rows,
    truncated + next_offset for paging. Defaults cap at 200 rows x 50 cols.
    """
    return _guard(lambda: _sheets().read(spreadsheet, tab, range, render, header_row, int(max_rows), int(max_cols),
                                         int(offset), columns, bool(as_records), bool(skip_empty_rows)))


@mcp.tool()
def sheets_find(spreadsheet: str, query: Text, tab: TabArg = None, regex: bool = False, match_case: bool = False,
                in_formulas: bool = False, max_results: int = 50) -> str:
    """Find cells whose text contains `query` (or matches it as a regex), in
    one tab or the whole workbook. Returns cell address, value and the row's
    first cells for context. in_formulas searches formula text instead of
    displayed values."""
    return _guard(lambda: _sheets().find(spreadsheet, _s(query), tab, bool(regex), bool(match_case), bool(in_formulas),
                                         int(max_results)))


@mcp.tool()
def sheets_lookup(spreadsheet: str, tab: TabArg, key_column: Union[str, int], keys: List[Text],
                  value_columns: Optional[List[Union[str, int]]] = None, header_row: Union[str, int, None] = "auto",
                  range: Optional[str] = None) -> str:
    """Row lookup by key, like a VLOOKUP for many keys at once: for each key
    (matched exactly, trimmed) in `key_column` (header name or letter) return
    the row number and the requested columns. Reports missing and ambiguous
    keys. Ideal for pin tables: keys=["PA0","PA1"], key_column="Pin"."""
    return _guard(lambda: _sheets().lookup(spreadsheet, tab, key_column, [_s(k) for k in keys], value_columns,
                                           header_row, range))


# ---------------------------------------------------------------------------
# writing - every tool previews by default; apply=true writes after a snapshot
# ---------------------------------------------------------------------------

@mcp.tool()
def sheets_write(spreadsheet: str, tab: TabArg, range: str, values: List[List[Any]],
                 input_option: str = "USER_ENTERED", apply: bool = False) -> str:
    """Write a 2-D block whose top-left cell is `range` (e.g. "B2"; a full
    range must be at least as large as the values). In values: null leaves a
    cell alone, "" clears it, "=..." is a formula (USER_ENTERED, like typing;
    RAW stores text literally). PREVIEW by default: per-cell old/new, formulas
    that would be overwritten, grid growth. apply=true snapshots the old
    values (undo via sheets_restore), writes, reads back and verifies."""
    return _guard(lambda: _sheets().write(spreadsheet, tab, range, values, input_option, bool(apply)))


@mcp.tool()
def sheets_update_cells(spreadsheet: str, tab: TabArg, cells: Dict[str, Any], input_option: str = "USER_ENTERED",
                        apply: bool = False) -> str:
    """Set scattered cells in one call: cells={"B7": "PA0", "D7": 3, "E9": ""}.
    Same preview/apply/snapshot behaviour as sheets_write."""
    return _guard(lambda: _sheets().update_cells(spreadsheet, tab, cells, input_option, bool(apply)))


@mcp.tool()
def sheets_update_by_key(spreadsheet: str, tab: TabArg, key_column: Union[str, int], updates: List[Dict[str, Any]],
                         header_row: Union[str, int, None] = "auto", range: Optional[str] = None,
                         add_missing: bool = False, all_matches: bool = False, input_option: str = "USER_ENTERED",
                         apply: bool = False) -> str:
    """Header-aware row updates keyed by a column value - the tool for pin
    tables. updates: [{"key": "PA3", "values": {"Net Name": "FC_PA3_...",
    "Function": "..."}}] or flat [{"Pin": "PA3", "Net Name": "..."}] (the key
    column named by its header). Columns are header names (case-insensitive)
    or letters. Keys not found are reported (add_missing=true appends rows for
    them); keys matching several rows are skipped unless all_matches=true.
    Preview by default, snapshot + verify on apply."""
    return _guard(lambda: _sheets().update_by_key(spreadsheet, tab, key_column, updates, header_row, range,
                                                  bool(add_missing), bool(all_matches), input_option, bool(apply)))


@mcp.tool()
def sheets_append_rows(spreadsheet: str, tab: TabArg, rows: List[Any], header_row: Union[str, int, None] = "auto",
                       input_option: str = "USER_ENTERED", apply: bool = False) -> str:
    """Append rows after the last data row of a tab. Each row is a list, or a
    {header: value} object aligned to the detected header. Preview shows the
    exact target rows."""
    return _guard(lambda: _sheets().append_rows(spreadsheet, tab, rows, header_row, input_option, bool(apply)))


@mcp.tool()
def sheets_clear(spreadsheet: str, tab: TabArg, range: str, apply: bool = False) -> str:
    """Clear the values in a range (formatting stays). Preview lists what would
    go; apply snapshots first. Refuses a whole tab."""
    return _guard(lambda: _sheets().clear(spreadsheet, tab, range, bool(apply)))


@mcp.tool()
def sheets_find_replace(spreadsheet: str, query: Text, replacement: Text, tab: TabArg = None, regex: bool = False,
                        match_case: bool = False, in_formulas: bool = False, whole_cell: bool = False,
                        input_option: str = "USER_ENTERED", max_changes: int = 500, apply: bool = False) -> str:
    """Find/replace in cell text (regex optional, \\1 groups allowed in the
    replacement). Formula cells are skipped unless in_formulas=true. Preview
    may span the workbook; apply one tab at a time. Snapshot + verify."""
    return _guard(lambda: _sheets().find_replace(spreadsheet, _s(query), _s(replacement), tab, bool(regex),
                                                 bool(match_case), bool(in_formulas), bool(whole_cell), input_option,
                                                 int(max_changes), bool(apply)))


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------

@mcp.tool()
def sheets_tabs(spreadsheet: str, action: str, tab: TabArg = None, title: Optional[Text] = None,
                index: Optional[int] = None, rows: Optional[int] = None, cols: Optional[int] = None,
                apply: bool = False) -> str:
    """Tab management: create (title, optional index/rows/cols), rename (tab,
    title), duplicate (tab, optional title/index), move (tab, index), hide,
    show. Deleting tabs is deliberately not offered. Preview by default."""
    return _guard(lambda: _sheets().tabs(spreadsheet, action, tab, _s(title), index, rows, cols, bool(apply)))


@mcp.tool()
def sheets_dimensions(spreadsheet: str, tab: TabArg, action: str, start: int, count: int = 1,
                      apply: bool = False) -> str:
    """insert_rows | insert_cols | delete_rows | delete_cols at 1-based `start`.
    Deletion previews the cells that would be removed and snapshots them."""
    return _guard(lambda: _sheets().dimensions(spreadsheet, tab, action, int(start), int(count), bool(apply)))


@mcp.tool()
def sheets_batch_update(spreadsheet: str, requests: List[Dict[str, Any]], apply: bool = False) -> str:
    """Raw Sheets API batchUpdate requests (formatting, conditional formats,
    data validation, protected ranges, frozen rows...). No cell-level preview
    is possible; apply=true sends them. deleteSheet is refused."""
    return _guard(lambda: _sheets().batch_update(spreadsheet, requests, bool(apply)))


# ---------------------------------------------------------------------------
# safety and files
# ---------------------------------------------------------------------------

@mcp.tool()
def sheets_snapshot(spreadsheet: str, tab: TabArg = None, range: Optional[str] = None) -> str:
    """Save the values (formulas kept) of a range, a tab, or the whole workbook
    to a local JSON under the backup dir. Applied writes do this automatically;
    call it yourself before a batch of edits."""
    return _guard(lambda: _sheets().snapshot(spreadsheet, tab, range))


@mcp.tool()
def sheets_restore(path: str, apply: bool = False) -> str:
    """Write a snapshot back (the undo for any applied write): preview shows the
    cells that would change; apply writes and takes a fresh snapshot first."""
    return _guard(lambda: _sheets().restore(path, bool(apply)))


@mcp.tool()
def sheets_export(spreadsheet: str, path: str, format: str = "xlsx", tab: TabArg = None) -> str:
    """Export the workbook as .xlsx (tab names preserved) or one tab as .csv to a
    local path, for offline analysis with Python."""
    return _guard(lambda: _sheets().export(spreadsheet, path, format, tab))


def main() -> None:
    auth.HOME.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(auth.HOME / "server.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    log.info("google-sheets MCP server starting; auth %s", auth.status()["method"])
    mcp.run()


if __name__ == "__main__":
    main()
