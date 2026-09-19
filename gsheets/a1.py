"""A1 notation, the way the Sheets API speaks it.

    A1          one cell             'My Tab'!B2:D10   a block on a tab
    A:C         whole columns        3:5               whole rows
    B2:D        open-ended downwards (as far as the data goes)

Rows and columns are 1-based everywhere in this module; None means "open".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_CELL = re.compile(r"^([A-Za-z]{0,3})(\d*)$")
_PLAIN_TAB = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOOKS_LIKE_CELL = re.compile(r"^[A-Za-z]{1,3}\d+$")


def col_to_index(letters: str) -> int:
    """A -> 1, Z -> 26, AA -> 27."""
    n = 0
    for ch in letters.upper():
        if not "A" <= ch <= "Z":
            raise ValueError(f"not a column: {letters!r}")
        n = n * 26 + ord(ch) - 64
    return n


def index_to_col(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA."""
    if n < 1:
        raise ValueError(f"column index must be >= 1, got {n}")
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def quote_tab(name: str) -> str:
    """Tab names with spaces or punctuation, or that could be read as a cell
    reference, must be single-quoted in a range string."""
    if _PLAIN_TAB.match(name) and not _LOOKS_LIKE_CELL.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def unquote_tab(text: str) -> str:
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1].replace("''", "'")
    return text


@dataclass(frozen=True)
class Ref:
    tab: Optional[str] = None
    r1: Optional[int] = None
    c1: Optional[int] = None
    r2: Optional[int] = None
    c2: Optional[int] = None

    @property
    def is_cell(self) -> bool:
        return self.r1 is not None and self.c1 is not None and (self.r1, self.c1) == (self.r2, self.c2)

    @property
    def whole_tab(self) -> bool:
        return self.r1 is None and self.c1 is None and self.r2 is None and self.c2 is None

    def with_tab(self, tab: str) -> "Ref":
        return Ref(tab, self.r1, self.c1, self.r2, self.c2)

    def a1(self) -> str:
        return to_a1(self)


def _split_tab(text: str):
    """Return (tab or None, rest). The tab is everything before the last '!'
    that is not inside quotes."""
    if text.startswith("'"):
        end = 1
        while True:
            end = text.find("'", end)
            if end < 0:
                raise ValueError(f"unterminated quote in {text!r}")
            if end + 1 < len(text) and text[end + 1] == "'":
                end += 2
                continue
            break
        if end + 1 < len(text) and text[end + 1] == "!":
            return unquote_tab(text[:end + 1]), text[end + 2:]
        if end + 1 == len(text):
            return unquote_tab(text), ""
        raise ValueError(f"cannot parse {text!r}")
    if "!" in text:
        tab, _, rest = text.rpartition("!")
        return tab, rest
    return None, text


def parse(text: str) -> Ref:
    """'Tab'!B2:D10 -> Ref. A bare word that is not a cell reference is a tab."""
    text = text.strip()
    if not text:
        return Ref()
    tab, rest = _split_tab(text)
    if not rest:
        return Ref(tab=tab)
    if tab is None and not (_CELL.match(rest) or ":" in rest):
        return Ref(tab=text)
    if tab is None and _CELL.match(rest) and not _LOOKS_LIKE_CELL.match(rest) and not rest.isdigit() \
            and not rest.isalpha():
        return Ref(tab=text)
    if tab is None and rest.isalpha() and len(rest) > 3:
        return Ref(tab=text)
    parts = rest.split(":")
    if len(parts) > 2:
        raise ValueError(f"cannot parse range {text!r}")
    m1 = _CELL.match(parts[0])
    if not m1:
        if tab is None:
            return Ref(tab=text)
        raise ValueError(f"cannot parse range {text!r}")
    c1 = col_to_index(m1.group(1)) if m1.group(1) else None
    r1 = int(m1.group(2)) if m1.group(2) else None
    if len(parts) == 1:
        if c1 is None or r1 is None:
            raise ValueError(f"a single reference needs a column and a row: {text!r}")
        return Ref(tab, r1, c1, r1, c1)
    m2 = _CELL.match(parts[1])
    if not m2:
        raise ValueError(f"cannot parse range {text!r}")
    c2 = col_to_index(m2.group(1)) if m2.group(1) else None
    r2 = int(m2.group(2)) if m2.group(2) else None
    if (c1 is None) != (c2 is None) and (r1 is None or r2 is None):
        raise ValueError(f"cannot parse range {text!r}")
    return Ref(tab, r1, c1, r2, c2)


def to_a1(ref: Ref) -> str:
    def end(r, c):
        return (index_to_col(c) if c else "") + (str(r) if r else "")
    body = ""
    if not ref.whole_tab:
        a, b = end(ref.r1, ref.c1), end(ref.r2, ref.c2)
        body = a if ref.is_cell else f"{a}:{b}"
    if ref.tab is None:
        return body
    return quote_tab(ref.tab) + (f"!{body}" if body else "")


def cell(row: int, col: int) -> str:
    return f"{index_to_col(col)}{row}"


def block(tab: Optional[str], row: int, col: int, nrows: int, ncols: int) -> Ref:
    """The block whose top-left cell is (row, col)."""
    return Ref(tab, row, col, row + max(nrows, 1) - 1, col + max(ncols, 1) - 1)
