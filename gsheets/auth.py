"""Credentials, and the one-time browser login.

Files live OUTSIDE the repo, in GSHEETS_HOME (default ~/mcp-servers/google-sheets):

    service_account.json   a service-account key      -> used if present
    credentials.json       an OAuth "Desktop app" client -> browser login once
    token.json             the OAuth token that login writes; refreshed silently
    backups/               snapshots taken before every applied write

The MCP server never runs the interactive flow: google-auth prints to stdout
during it, and stdout is the MCP transport. Run this module directly for the
login:   python -m gsheets.auth
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List

DEFAULT_SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive.readonly"]

HOME = Path(os.environ.get("GSHEETS_HOME") or (Path.home() / "mcp-servers" / "google-sheets"))
CREDENTIALS = Path(os.environ.get("GSHEETS_CREDENTIALS") or HOME / "credentials.json")
TOKEN = Path(os.environ.get("GSHEETS_TOKEN") or HOME / "token.json")
SERVICE_ACCOUNT = Path(os.environ.get("GSHEETS_SERVICE_ACCOUNT") or HOME / "service_account.json")
BACKUP_DIR = Path(os.environ.get("GSHEETS_BACKUP_DIR") or HOME / "backups")
SCOPES: List[str] = [s.strip() for s in os.environ.get("GSHEETS_SCOPES", "").split(",") if s.strip()] or DEFAULT_SCOPES


class AuthError(Exception):
    pass


def status() -> dict:
    method = ("service_account" if SERVICE_ACCOUNT.exists() else "oauth_token" if TOKEN.exists()
              else "oauth_needs_login" if CREDENTIALS.exists() else "no_credentials")
    out = {"method": method, "home": str(HOME), "scopes": SCOPES,
           "files": {"service_account.json": SERVICE_ACCOUNT.exists(), "credentials.json": CREDENTIALS.exists(),
                     "token.json": TOKEN.exists()}, "backup_dir": str(BACKUP_DIR)}
    if TOKEN.exists():
        try:
            tok = json.loads(TOKEN.read_text(encoding="utf-8"))
            granted = tok.get("scopes") or []
            missing = [s for s in SCOPES if s not in granted]
            out["token_scopes"] = granted
            if missing:
                out["warning"] = f"token lacks scopes {missing}; delete token.json and log in again"
        except Exception as e:      # pragma: no cover - corrupt file
            out["warning"] = f"token.json unreadable: {e}"
    if method == "no_credentials":
        out["how_to_fix"] = (f"put a service-account key at {SERVICE_ACCOUNT} (and share the sheets with its email), "
                             f"or an OAuth Desktop client at {CREDENTIALS} and run: python -m gsheets.auth")
    elif method == "oauth_needs_login":
        out["how_to_fix"] = "run: python -m gsheets.auth   (opens a browser once, writes token.json)"
    return out


def load_credentials(interactive: bool = False):
    if SERVICE_ACCOUNT.exists():
        from google.oauth2 import service_account
        return service_account.Credentials.from_service_account_file(str(SERVICE_ACCOUNT), scopes=SCOPES)
    creds = None
    if TOKEN.exists():
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            TOKEN.write_text(creds.to_json(), encoding="utf-8")
        if creds.valid:
            return creds
    if not interactive:
        st = status()
        raise AuthError(f"no usable Google credentials ({st['method']}). {st.get('how_to_fix', '')}".strip())
    if not CREDENTIALS.exists():
        raise AuthError(f"no OAuth client at {CREDENTIALS}: in Google Cloud Console create an OAuth client of type "
                        "'Desktop app' (consent screen Internal for a Workspace org) and save its JSON there")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    TOKEN.parent.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds


def main() -> int:
    print(json.dumps(status(), indent=1))
    try:
        creds = load_credentials(interactive=True)
    except AuthError as e:
        print(f"\n{e}", file=sys.stderr)
        return 1
    from .backend import GoogleBackend
    who = GoogleBackend(creds).whoami()
    print("\nlogged in as:", json.dumps(who))
    print("token:", TOKEN if not SERVICE_ACCOUNT.exists() else SERVICE_ACCOUNT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
