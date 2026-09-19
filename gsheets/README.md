# gsheets - Google Sheets for Claude, our own way

A small library over the Sheets v4 and Drive v3 APIs and a thin MCP server on
top. Built because the stock Drive connector returns a workbook as one flat
export (no tab names, no ranges, no writes), and the community servers lack
the discipline the Altium tools already have: preview before apply, read back
after, snapshot for undo.

```
gsheets/
  a1.py        A1 notation <-> rows/columns, tab quoting
  backend.py   the API surface, twice: GoogleBackend (real) and MemoryBackend (tests)
  core.py      what the tools do: paged reads, keyed lookups, write PLANS, snapshots
  auth.py      credentials + the one-time browser login (never inside the server)
  server.py    the MCP tools (sheets_*)
  tests/       everything in core, against the memory backend, no network
```

## Tools

| Read | Write (preview by default, `apply=true` snapshots then writes then verifies) | Structure / safety |
|---|---|---|
| `sheets_list` find workbooks | `sheets_write` a 2-D block at a cell | `sheets_tabs` create / rename / duplicate / move / hide / show |
| `sheets_open` tabs, gids, headers | `sheets_update_cells` scattered cells | `sheets_dimensions` insert / delete rows or columns |
| `sheets_read` paged, header-aware, records | `sheets_update_by_key` rows keyed by a column (pin tables) | `sheets_batch_update` raw API requests (formatting etc.) |
| `sheets_find` text or regex, values or formulas | `sheets_append_rows` | `sheets_snapshot` / `sheets_restore` the undo |
| `sheets_lookup` many keys at once | `sheets_clear`, `sheets_find_replace` | `sheets_export` .xlsx (tab names kept) / .csv |

Tabs resolve by title, gid (`gid=123` or the `#gid=` in a URL), or `#index`.
In written values `null` leaves a cell alone, `""` clears it, `"=..."` is a
formula. Deleting tabs is deliberately not offered.

## Setup

Runtime lives outside the repo so credentials never land in it:

```
C:\Users\<you>\mcp-servers\google-sheets\
  .venv\                 python -m venv, then pip install -r gsheets/requirements.txt
  credentials.json       OAuth "Desktop app" client from Google Cloud Console   (option A)
  token.json             written by the login below, refreshed silently
  service_account.json   a service-account key                                 (option B)
  backups\               snapshots taken before every applied write
  server.log
```

Google Cloud Console, once: a project with the **Google Sheets API** and
**Google Drive API** enabled. Option A: OAuth consent screen *Internal* (a
Workspace org needs no verification and tokens do not expire), an OAuth
client of type *Desktop app*, its JSON saved as `credentials.json`, then

```bash
C:/Users/<you>/mcp-servers/google-sheets/.venv/Scripts/python.exe -m gsheets.auth
```

opens the browser once and writes `token.json`. Option B: a service account
whose email you share the sheets (or their folder) with; drop its key as
`service_account.json`. Default scopes are Sheets read/write and Drive
read-only (`GSHEETS_SCOPES` overrides; delete `token.json` after changing).

Register the server for Claude Code (user scope, `~/.claude.json`):

```json
"google-sheets": {"type": "stdio",
                  "command": "C:\\Users\\<you>\\mcp-servers\\google-sheets\\.venv\\Scripts\\python.exe",
                  "args": ["D:\\codebase\\altium-mcp\\gsheets\\server.py"], "env": {}}
```

`GSHEETS_HOME` moves the runtime folder; `GSHEETS_BACKUP_DIR` the snapshots.

## Tests

```bash
server/.venv/Scripts/python.exe -m unittest discover -s gsheets/tests -t .
```

## Facts about the API the code leans on

- Trailing empty rows and cells are omitted from returned values.
- Ranges past the grid are an error; the plan appends rows/columns first.
- A `null` in written values skips the cell; `""` clears it.
- `FORMULA` rendering returns `=...` for formula cells and the value for others;
  writing `USER_ENTERED` behaves like typing (formulas, numbers, dates parse).
- Tab names with spaces or that look like cells must be quoted: `'Device Tree'!A1`.
- The Sheets API has no undo; snapshots are ours.
