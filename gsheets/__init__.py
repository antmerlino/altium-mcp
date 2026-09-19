"""gsheets - Google Sheets for Claude, our own way.

A small library over the Sheets v4 / Drive v3 APIs (`backend`, `core`) and a
thin MCP server (`server`) that exposes it as tools. Everything that decides
WHAT to change is pure Python and unit-tested against an in-memory backend;
only `backend.GoogleBackend` talks to Google.
"""
