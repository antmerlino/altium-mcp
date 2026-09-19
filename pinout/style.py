"""Symbol appearance - the colours and widths a house library uses.

Altium stores colours as TColor: an integer laid out $00BBGGRR (blue in the
high byte). The creator shipped with this repo hard-codes Altium's stock
palette - body fill $00B0FFFF (light yellow) and outline $00FF0000 (blue) -
regardless of what the target library looks like, which is why generated
symbols did not match the user's. Humans think in RRGGBB hex; this module
converts, and the same Style drives generation, verification and preview so
they cannot drift apart.

The defaults ARE Altium's stock palette, so behaviour is unchanged until a
house Style is supplied.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Tuple


def hex_to_bgr(h: str) -> int:
    """'RRGGBB' -> Altium TColor integer ($00BBGGRR)."""
    h = h.strip().lstrip("#")
    if len(h) != 6:
        raise ValueError(f"colour must be RRGGBB hex, got {h!r}")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r


def bgr_to_hex(v: int) -> str:
    """Altium TColor integer -> 'RRGGBB'."""
    v = int(v) & 0xFFFFFF
    return "%02X%02X%02X" % (v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)


def hex_to_rgb(h: str) -> Tuple[int, int, int]:
    h = h.strip().lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


@dataclass(frozen=True)
class Style:
    body_fill: str = "FFFFB0"     # Altium stock: light yellow  ($00B0FFFF)
    body_line: str = "0000FF"     # Altium stock: blue           ($00FF0000)
    divider_color: str = ""       # lines/polylines/arcs; "" = same as body_line
    pin_color: str = "000000"
    label_color: str = "0000FF"
    line_width: int = 1           # Altium eSmall

    def colour(self, field: str) -> str:
        """Resolve a colour field; divider_color falls back to body_line."""
        v = getattr(self, field)
        return v if v else self.body_line

    def rgb(self, field: str) -> Tuple[int, int, int]:
        return hex_to_rgb(self.colour(field))

    def bgr(self, field: str) -> int:
        return hex_to_bgr(self.colour(field))

    def save(self, path, provenance: str = "") -> None:
        import json
        d = self.to_dict()
        if provenance:
            d["_provenance"] = provenance
        __import__("pathlib").Path(path).write_text(json.dumps(d, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path) -> "Style":
        import json
        return cls.from_dict(json.loads(__import__("pathlib").Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Style":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


ALTIUM_STOCK = Style()
