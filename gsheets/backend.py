"""Two implementations of the same small surface: Google, and memory for tests.

Only what the tools need, with the Sheets API's own vocabulary so the memory
backend can mimic it faithfully:

    meta(sid)                              spreadsheets.get with a fields mask
    get_values(sid, ranges, render)        values.batchGet   -> [{range, values}]
    update_values(sid, data, input_option) values.batchUpdate
    clear_values(sid, ranges)              values.batchClear
    batch_update(sid, requests)            spreadsheets.batchUpdate (structure)
    list_spreadsheets(query, folder, n)    drive.files.list
    export(sid, mime)                      drive.files.export
    whoami()                               drive.about.get

API facts the memory backend reproduces: trailing empty rows and cells are
omitted from returned values; a `null` in written values leaves the cell
alone while "" clears it; writing past the grid is an error (the caller has
to appendDimension first); FORMULA rendering returns "=..." for formula
cells and the value for the rest.
"""
from __future__ import annotations

import copy
import io
import re
import time
from typing import Any, Dict, List, Optional, Protocol

from . import a1

RENDER = {"formatted": "FORMATTED_VALUE", "unformatted": "UNFORMATTED_VALUE", "formula": "FORMULA"}
META_FIELDS = ("spreadsheetId,properties.title,spreadsheetUrl,"
               "sheets(properties(sheetId,title,index,hidden,"
               "gridProperties(rowCount,columnCount,frozenRowCount,frozenColumnCount)))")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class BackendError(Exception):
    pass


class Backend(Protocol):
    def meta(self, sid: str) -> dict: ...
    def get_values(self, sid: str, ranges: List[str], render: str) -> List[dict]: ...
    def update_values(self, sid: str, data: List[dict], input_option: str) -> dict: ...
    def clear_values(self, sid: str, ranges: List[str]) -> dict: ...
    def batch_update(self, sid: str, requests: List[dict]) -> dict: ...
    def list_spreadsheets(self, query: Optional[str], folder: Optional[str], limit: int) -> List[dict]: ...
    def export(self, sid: str, mime: str) -> bytes: ...
    def whoami(self) -> dict: ...


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------

class GoogleBackend:
    """googleapiclient over real credentials. Retries the two transient
    statuses the Sheets API is known for (429 rate limit, 503)."""

    def __init__(self, credentials, retries: int = 4):
        from googleapiclient.discovery import build
        self.sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        self.drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self.retries = retries
        self.credentials = credentials

    def _run(self, request):
        from googleapiclient.errors import HttpError
        delay = 1.0
        for attempt in range(self.retries + 1):
            try:
                return request.execute(num_retries=0)
            except HttpError as e:
                status = getattr(e.resp, "status", None)
                if status in (429, 500, 503) and attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise BackendError(_describe_http_error(e)) from e

    def meta(self, sid):
        return self._run(self.sheets.spreadsheets().get(spreadsheetId=sid, fields=META_FIELDS))

    def get_values(self, sid, ranges, render):
        if not ranges:
            return []
        out = self._run(self.sheets.spreadsheets().values().batchGet(
            spreadsheetId=sid, ranges=ranges, valueRenderOption=RENDER.get(render, render),
            dateTimeRenderOption="FORMATTED_STRING", majorDimension="ROWS"))
        return [{"range": vr.get("range"), "values": vr.get("values", [])} for vr in out.get("valueRanges", [])]

    def update_values(self, sid, data, input_option):
        return self._run(self.sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=sid, body={"valueInputOption": input_option, "data": data,
                                     "includeValuesInResponse": False}))

    def clear_values(self, sid, ranges):
        return self._run(self.sheets.spreadsheets().values().batchClear(spreadsheetId=sid, body={"ranges": ranges}))

    def batch_update(self, sid, requests):
        return self._run(self.sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": requests}))

    def list_spreadsheets(self, query, folder, limit):
        q = ["mimeType='application/vnd.google-apps.spreadsheet'", "trashed=false"]
        if query:
            q.append("name contains '%s'" % query.replace("\\", "\\\\").replace("'", "\\'"))
        if folder:
            q.append(f"'{folder}' in parents")
        out = self._run(self.drive.files().list(
            q=" and ".join(q), pageSize=min(max(limit, 1), 100), orderBy="modifiedTime desc",
            fields="files(id,name,modifiedTime,webViewLink,parents,owners(emailAddress),driveId)",
            supportsAllDrives=True, includeItemsFromAllDrives=True, corpora="allDrives"))
        return out.get("files", [])

    def export(self, sid, mime):
        from googleapiclient.http import MediaIoBaseDownload
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, self.drive.files().export_media(fileId=sid, mimeType=mime))
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()

    def whoami(self):
        email = getattr(self.credentials, "service_account_email", None)
        if email:
            return {"kind": "service_account", "email": email}
        try:
            about = self._run(self.drive.about().get(fields="user(emailAddress,displayName)"))
            user = about.get("user", {})
            return {"kind": "oauth_user", "email": user.get("emailAddress"), "name": user.get("displayName")}
        except BackendError as e:
            return {"kind": "oauth_user", "email": None, "note": str(e)}


def _describe_http_error(e) -> str:
    status = getattr(e.resp, "status", "?")
    try:
        import json
        body = json.loads(e.content.decode("utf-8"))
        msg = body.get("error", {}).get("message") or str(body)
    except Exception:
        msg = e.content.decode("utf-8", "replace")[:300] if getattr(e, "content", None) else str(e)
    hint = ""
    if status == 403:
        hint = " (no access: with a service account the sheet must be shared with its email; with OAuth the token's scopes may be too narrow - delete token.json and log in again)"
    elif status == 404:
        hint = " (no such spreadsheet, or not visible to this account)"
    return f"HTTP {status}: {msg}{hint}"


# ---------------------------------------------------------------------------
# Memory (tests)
# ---------------------------------------------------------------------------

class MemoryBackend:
    """A workbook store that behaves like the API on the points that matter.

    Cells are stored as {"v": value, "f": formula-or-None}; FORMULA render
    returns "=..." for formula cells, the value otherwise. A formula's displayed
    value is faked as "(calc)" - the memory backend does not evaluate."""

    def __init__(self):
        self.books: Dict[str, dict] = {}
        self.calls: List[tuple] = []
        self._next_gid = 100
        self.user = {"kind": "oauth_user", "email": "tester@example.com", "name": "Tester"}

    # -- fixtures -----------------------------------------------------------
    def add_book(self, sid: str, title: str, tabs: Dict[str, List[list]], gids: Optional[Dict[str, int]] = None,
                 rows: int = 1000, cols: int = 26) -> None:
        book = {"title": title, "tabs": []}
        for i, (name, grid) in enumerate(tabs.items()):
            gid = (gids or {}).get(name, self._next_gid)
            self._next_gid = max(self._next_gid, gid) + 1
            cells = {}
            for r, row in enumerate(grid, start=1):
                for c, v in enumerate(row, start=1):
                    if v is None or v == "":
                        continue
                    if isinstance(v, str) and v.startswith("="):
                        cells[(r, c)] = {"v": "(calc)", "f": v}
                    else:
                        cells[(r, c)] = {"v": v, "f": None}
            book["tabs"].append({"gid": gid, "title": name, "index": i, "hidden": False,
                                 "rows": max(rows, len(grid)), "cols": max(cols, max((len(r) for r in grid), default=0)),
                                 "frozen_rows": 0, "frozen_cols": 0, "cells": cells})
        self.books[sid] = book

    def grid(self, sid: str, tab: str) -> List[list]:
        """The tab as a dense list of lists (values), for assertions."""
        t = self._tab(sid, tab)
        if not t["cells"]:
            return []
        R = max(r for r, _ in t["cells"])
        C = max(c for _, c in t["cells"])
        return [[t["cells"].get((r, c), {"v": ""})["v"] for c in range(1, C + 1)] for r in range(1, R + 1)]

    # -- helpers --------------------------------------------------------------
    def _book(self, sid):
        if sid not in self.books:
            raise BackendError("HTTP 404: Requested entity was not found. (no such spreadsheet, or not visible to this account)")
        return self.books[sid]

    def _tab(self, sid, name=None, gid=None):
        for t in self._book(sid)["tabs"]:
            if (name is not None and t["title"] == name) or (gid is not None and t["gid"] == gid):
                return t
        raise BackendError(f"HTTP 400: Unable to parse range: {name or gid}")

    def _resolve(self, sid, rng: str):
        ref = a1.parse(rng)
        book = self._book(sid)
        t = self._tab(sid, ref.tab) if ref.tab else book["tabs"][0]
        r1, c1 = ref.r1 or 1, ref.c1 or 1
        r2 = ref.r2 if ref.r2 is not None else t["rows"]
        c2 = ref.c2 if ref.c2 is not None else t["cols"]
        return t, r1, c1, min(r2, t["rows"]), min(c2, t["cols"])

    # -- protocol -------------------------------------------------------------
    def meta(self, sid):
        book = self._book(sid)
        self.calls.append(("meta", sid))
        return {"spreadsheetId": sid, "properties": {"title": book["title"]},
                "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{sid}/edit",
                "sheets": [{"properties": {"sheetId": t["gid"], "title": t["title"], "index": t["index"],
                                           "hidden": t["hidden"],
                                           "gridProperties": {"rowCount": t["rows"], "columnCount": t["cols"],
                                                              "frozenRowCount": t["frozen_rows"],
                                                              "frozenColumnCount": t["frozen_cols"]}}}
                           for t in sorted(book["tabs"], key=lambda t: t["index"])]}

    def get_values(self, sid, ranges, render):
        self.calls.append(("get_values", sid, tuple(ranges), render))
        out = []
        for rng in ranges:
            t, r1, c1, r2, c2 = self._resolve(sid, rng)
            rows = []
            for r in range(r1, r2 + 1):
                row = []
                for c in range(c1, c2 + 1):
                    cell = t["cells"].get((r, c))
                    if cell is None:
                        row.append("")
                    elif render == "formula" and cell["f"]:
                        row.append(cell["f"])
                    else:
                        row.append(cell["v"])
                while row and row[-1] == "":
                    row.pop()
                rows.append(row)
            while rows and not rows[-1]:
                rows.pop()
            out.append({"range": a1.to_a1(a1.Ref(t["title"], r1, c1, r2, c2)), "values": rows})
        return out

    def update_values(self, sid, data, input_option):
        self.calls.append(("update_values", sid, copy.deepcopy(data), input_option))
        total = 0
        for item in data:
            t, r1, c1, _, _ = self._resolve(sid, item["range"])
            for i, row in enumerate(item["values"]):
                for j, v in enumerate(row):
                    r, c = r1 + i, c1 + j
                    if r > t["rows"] or c > t["cols"]:
                        raise BackendError("HTTP 400: Range exceeds grid limits")
                    if v is None:
                        continue
                    total += 1
                    if v == "":
                        t["cells"].pop((r, c), None)
                    elif isinstance(v, str) and v.startswith("=") and input_option == "USER_ENTERED":
                        t["cells"][(r, c)] = {"v": "(calc)", "f": v}
                    else:
                        t["cells"][(r, c)] = {"v": v, "f": None}
        return {"totalUpdatedCells": total}

    def clear_values(self, sid, ranges):
        self.calls.append(("clear_values", sid, tuple(ranges)))
        for rng in ranges:
            t, r1, c1, r2, c2 = self._resolve(sid, rng)
            for key in list(t["cells"]):
                if r1 <= key[0] <= r2 and c1 <= key[1] <= c2:
                    del t["cells"][key]
        return {"clearedRanges": list(ranges)}

    def batch_update(self, sid, requests):
        self.calls.append(("batch_update", sid, copy.deepcopy(requests)))
        book = self._book(sid)
        replies = []
        for req in requests:
            (kind, body), = req.items()
            if kind == "addSheet":
                p = body.get("properties", {})
                gid = p.get("sheetId", self._next_gid)
                self._next_gid = max(self._next_gid, gid) + 1
                gp = p.get("gridProperties", {})
                idx = p.get("index", len(book["tabs"]))
                for t in book["tabs"]:
                    if t["index"] >= idx:
                        t["index"] += 1
                book["tabs"].append({"gid": gid, "title": p["title"], "index": idx, "hidden": False,
                                     "rows": gp.get("rowCount", 1000), "cols": gp.get("columnCount", 26),
                                     "frozen_rows": 0, "frozen_cols": 0, "cells": {}})
                replies.append({"addSheet": {"properties": {"sheetId": gid, "title": p["title"], "index": idx}}})
            elif kind == "duplicateSheet":
                src = self._tab(sid, gid=body["sourceSheetId"])
                gid = self._next_gid
                self._next_gid += 1
                idx = body.get("insertSheetIndex", src["index"] + 1)
                for t in book["tabs"]:
                    if t["index"] >= idx:
                        t["index"] += 1
                new = copy.deepcopy(src)
                new.update({"gid": gid, "title": body.get("newSheetName", f"Copy of {src['title']}"), "index": idx})
                book["tabs"].append(new)
                replies.append({"duplicateSheet": {"properties": {"sheetId": gid, "title": new["title"]}}})
            elif kind == "updateSheetProperties":
                p = body["properties"]
                t = self._tab(sid, gid=p["sheetId"])
                fields = body.get("fields", "")
                if "title" in fields:
                    t["title"] = p["title"]
                if "hidden" in fields:
                    t["hidden"] = bool(p.get("hidden"))
                if "index" in fields:
                    others = sorted((o for o in book["tabs"] if o is not t), key=lambda o: o["index"])
                    others.insert(min(int(p["index"]), len(others)), t)
                    for i, o in enumerate(others):
                        o["index"] = i
                replies.append({})
            elif kind == "appendDimension":
                t = self._tab(sid, gid=body["sheetId"])
                key = "rows" if body["dimension"] == "ROWS" else "cols"
                t[key] += body["length"]
                replies.append({})
            elif kind in ("insertDimension", "deleteDimension"):
                rng = body["range"]
                t = self._tab(sid, gid=rng["sheetId"])
                axis = 0 if rng["dimension"] == "ROWS" else 1
                start, end = rng["startIndex"], rng["endIndex"]      # 0-based, end exclusive
                n = end - start
                new_cells = {}
                for (r, c), cell in t["cells"].items():
                    pos = (r, c)[axis] - 1
                    if kind == "insertDimension":
                        shift = n if pos >= start else 0
                    else:
                        if start <= pos < end:
                            continue
                        shift = -n if pos >= end else 0
                    key = (r + shift, c) if axis == 0 else (r, c + shift)
                    new_cells[key] = cell
                t["cells"] = new_cells
                t["rows" if axis == 0 else "cols"] += n if kind == "insertDimension" else -n
                replies.append({})
            else:
                raise BackendError(f"HTTP 400: unsupported request in memory backend: {kind}")
        return {"replies": replies}

    def list_spreadsheets(self, query, folder, limit):
        self.calls.append(("list", query, folder, limit))
        out = []
        for sid, book in self.books.items():
            if query and query.lower() not in book["title"].lower():
                continue
            out.append({"id": sid, "name": book["title"], "modifiedTime": "2026-01-01T00:00:00Z",
                        "webViewLink": f"https://docs.google.com/spreadsheets/d/{sid}/edit"})
        return out[:limit]

    def export(self, sid, mime):
        self.calls.append(("export", sid, mime))
        return b"PK-fake-xlsx-" + sid.encode()

    def whoami(self):
        return dict(self.user)
