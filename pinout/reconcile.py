"""Reconcile the sources of a board's pinout and let the designer choose.

    facts (ioc, sheet, schematic)  --compare per field-->  Diff rows, each DIAGNOSED
        --publish-->  a "Reconcile" tab in the pinning workbook, one row per
                      disagreement: why it exists, what "accept" would do, a
                      suggested decision, and a Decision dropdown
        --collect-->  the decisions  --apply-->  sheet cells (gsheets), the
                      .ioc (a reviewed copy), Altium payloads (net-label
                      renames, symbol label edits) for the live step

Ownership (agreed 2026-09-19): the .ioc owns the SIGNAL; the sheet owns the
FUNCTION and the NET NAME (its Net Name column, overrides included); symbol
GROUPING and electrical type are derived.

Not every disagreement is an error. A pin often has two right descriptions:
CubeMX names the MECHANISM (TIM1_CH3, an input capture) while the net names
the MEANING (SPI2_DRDY1, the sensor's data-ready line). The diagnosis step
recognises those and suggests "ignore" rather than a rename. Kinds:

    symbol_label_wrong      CubeMX and sheet agree; only the symbol's group label differs
    role_vs_signal          mechanism vs meaning; the .ioc label already carries the meaning
    dual_use                one pin, two roles, both recorded (MCO1 clock out AND a reset line)
    clock_unconfigured      a crystal is wired but CubeMX's clock tree does not use it
    ioc_unassigned          the sheet/schematic use a pin the .ioc leaves free
    ioc_label_missing_function   the .ioc GPIO_Label lacks the __Function suffix
    sheet_stale             the sheet has an older signal / net than CubeMX or the schematic
    schematic_stale         the schematic label differs from the sheet's Net Name
    sheet_gap               the sheet has no Net Name for a wired pin
    convention_variant      right meaning, written in a form the convention does not use
    function_mismatch       the sources name different functions; a person must decide
    phantom_pin             the sheet lists a pin the package does not have
    sheet_error             a formula error in the sheet
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

SOURCES = ("cubemx", "sheet", "schematic", "symbol")
FIELDS = ("signal", "function", "net", "group")
DECISIONS = ["accept", "keep cubemx", "keep sheet", "keep schematic", "custom", "ignore"]
REVIEW_HEADER = ["Id", "Pin", "Ball", "Field", "CubeMX", "Sheet", "Schematic", "Symbol", "Status", "Kind", "Why",
                 "Suggested", "Proposed", "What accept does", "Decision", "Custom value", "Note"]
DECISION_COL = REVIEW_HEADER.index("Decision")            # 0-based, for the dropdown
_MECHANISM_FAMILIES = ("TIM", "LPTIM", "HRTIM", "ADC", "DAC", "COMP", "DFSDM")


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
    kind: str = ""
    why: str = ""
    suggested: str = "decide"
    ctx: Dict[str, object] = field(default_factory=dict)

    def fix_text(self) -> str:
        if not self.apply_to:
            return "no change" if self.suggested == "ignore" else "nothing the tool can write; see Why"
        return " | ".join(describe_write(w) for w in self.apply_to)

    def row(self) -> list:
        v = self.values
        return [self.id, self.pin, self.ball or "", self.field, v.get("cubemx") or "", v.get("sheet") or "",
                v.get("schematic") or "", v.get("symbol") or "", self.status, self.kind, self.why, self.suggested,
                self.proposed or "", self.fix_text(), "", "", self.note]


@dataclass
class Report:
    board: str
    prefix: str
    diffs: List[Diff]
    agree: Dict[str, int]
    diagnostics: dict

    def summary(self) -> dict:
        by, kinds, targets, sugg = {}, {}, {}, {}
        for d in self.diffs:
            by.setdefault(d.field, {}).setdefault(d.status, 0)
            by[d.field][d.status] += 1
            kinds[d.kind] = kinds.get(d.kind, 0) + 1
            sugg[d.suggested] = sugg.get(d.suggested, 0) + 1
            for a in d.apply_to:
                targets[a["target"]] = targets.get(a["target"], 0) + 1
        return {"board": self.board, "disagreements": len(self.diffs), "agree": self.agree, "by_field": by,
                "by_kind": kinds, "suggested": sugg, "writes_proposed_by_target": targets}

    def to_json(self) -> str:
        return json.dumps({"summary": self.summary(), "diffs": [asdict(d) for d in self.diffs],
                           "diagnostics": self.diagnostics}, indent=1, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# describing writes in words
# ---------------------------------------------------------------------------

def describe_write(w: dict) -> str:
    t = w["target"]
    if t == "ioc":
        cur = w.get("current")
        return (f"CubeMX .ioc: {w['pin']}.{w['field']} = {w['value']!r}"
                + (f" (now {cur!r})" if cur else " (now empty)")
                + (" - or make the same change in CubeMX and save" if w["field"] == "Signal" else ""))
    if t == "sheet":
        where = f"'{w['tab']}' row {w['row']}" if w.get("row") else f"'{w.get('tab') or 'sheet'}' (row of {w['pin']})"
        col = w.get("column") or w["field"]
        parts = w.get("parts")
        if parts:
            return f"Sheet: {where}: " + ", ".join(f"{c} = {v!r}" for c, v in parts.items())
        return f"Sheet: {where}, {col} = {w['value']!r}" + (f" (now {w['current']!r})" if w.get("current") else " (now empty)")
    if t == "altium":
        if w.get("kind") == "net_label":
            return f"Altium: rename net label {w['from']!r} -> {w['to']!r} on {', '.join(w.get('sheets') or ['?'])}"
        if w.get("kind") == "symbol_label":
            cover = w.get("covers")
            return (f"Altium: symbol {w.get('symbol')} part {w.get('part', '?')}: group label {w.get('from')!r} -> {w.get('to')!r}"
                    + (f" (covers {', '.join(cover)})" if cover else ""))
    return json.dumps(w)


# ---------------------------------------------------------------------------
# comparing
# ---------------------------------------------------------------------------

def expected_cube_label(signal: Optional[str], function: Optional[str], current: Optional[str] = None,
                        role: Optional[str] = None, debug_style: Optional[str] = None) -> Optional[str]:
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
    return f"{netnames.preferred_spelling(signal, debug_style)}__{function}"


def _sig_same(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return a == b
    if a == b:
        return True
    return b in netnames.signal_spellings(a) or a in netnames.signal_spellings(b)


def _family(signal: Optional[str]) -> str:
    g = naming.peripheral_group(signal) if signal else ""
    return (g or "").rstrip("0123456789")


def _sheet_write(board: Board, sh: dict, key: str, role: str, value, current=None) -> dict:
    """A sheet write with tab/row/column resolved from the manifest and the
    row numbers the adapter recorded. MC-style tables have no signal column:
    a signal becomes Peripheral + Peripheral Pin."""
    w = {"target": "sheet", "field": role, "pin": key, "value": value, "current": current}
    spec = None
    for which in ("pins", "signals"):
        s = getattr(board.sheet, which, None) if board.sheet else None
        if s and (role in s.columns or (role == "signal" and "peripheral" in s.columns)):
            spec = s
            break
    if spec:
        w["tab"] = str(spec.tab)
        w["row"] = (sh.get("_rows") or {}).get(str(spec.tab))
        if role in spec.columns:
            w["column"] = spec.columns[role]
        elif role == "signal":
            if value and value.startswith("GPIO_"):
                w["parts"] = {spec.columns["peripheral"]: "IO"}
            elif value:
                per, ppin = split_signal_for_sheet(value)
                w["parts"] = {spec.columns["peripheral"]: per, spec.columns["peripheral_pin"]: ppin}
    return w


def split_signal_for_sheet(signal: str):
    """A CubeMX signal as the MC sheet writes it: Peripheral + Peripheral Pin.
    S_TIM17_CH1 -> (TIM17, CH1); RCC_MCO_1 -> (RCC, MCO_1); USB_OTG_HS_ULPI_D0 ->
    (USB_OTG_HS, ULPI_D0); DEBUG_JTMS-SWDIO -> (DEBUG, JTMS-SWDIO); ADCx_INP18 -> (ADC1, INP18)."""
    if signal.startswith("DEBUG_"):
        return "DEBUG", signal[len("DEBUG_"):]
    if signal.startswith("ADCx_"):
        return "ADC1", signal[len("ADCx_"):]
    body = signal[2:] if signal.startswith("S_") else signal
    per = naming.peripheral_group(body) or body.split("_", 1)[0]
    if body.startswith(per + "_"):
        return per, body[len(per) + 1:]
    head, _, rest = body.partition("_")
    return head, rest


def compare_pin(board: Board, key: str, ioc: dict, sh: dict, sch: dict) -> List[Diff]:
    out: List[Diff] = []
    pin_name = ioc.get("name") or sh.get("name") or sch.get("name") or key
    ball = ioc.get("ball") or sch.get("ball")
    prefix = board.prefix
    i_sig, s_sig, c_sig = ioc.get("signal"), sh.get("signal"), sch.get("signal")
    s_net, c_net = sh.get("net"), sch.get("net")
    sym_group = sch.get("group")
    s_is_role = False
    ctx_base = {"signal": i_sig, "cube_label": ioc.get("cube_label"), "sheet_net": s_net, "schematic_net": c_net,
                "sheets": sch.get("sheets", []), "symbol_label": sch.get("symbol_label"), "symbol_part": sch.get("symbol_part"),
                "pin_name": pin_name, "mode": ioc.get("mode")}

    def diff(fld, values, status, proposed=None, apply_to=None, note="", **ctx):
        out.append(Diff(f"{key}:{fld}", key, ball, fld, values, status, proposed, apply_to or [], note,
                        ctx={**ctx_base, **ctx}))

    # ---- signal: owner cubemx ------------------------------------------------
    values = {"cubemx": i_sig, "sheet": s_sig, "schematic": c_sig if c_sig != "GPIO" else None, "symbol": sym_group}
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
                apply_to.append(_sheet_write(board, sh, key, "signal", i_sig, s_sig))
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
                problems.append("symbol")
                apply_to.append({"target": "altium", "kind": "symbol_label", "pin": key,
                                 "symbol": board.altium.symbol if board.altium else None,
                                 "part": sch.get("symbol_part"), "from": sch.get("symbol_label"), "to": want})
        if not i_sig and (s_sig or c_sig):
            diff("signal", values, "missing:cubemx", s_sig or None,
                 [{"target": "ioc", "field": "Signal", "pin": key, "value": s_sig, "current": None}] if s_sig else [],
                 "the .ioc assigns nothing; the sheet/schematic do", problems=["cubemx"])
        elif problems:
            status = "conflict" if any(p in ("sheet", "schematic", "symbol") for p in problems) else problems[0]
            diff("signal", values, status, i_sig, apply_to, "; ".join(problems), problems=problems)
    elif (s_net or c_net) and not i_sig:
        lab = s_net or c_net
        if netnames.parse(lab, prefix).form != "spare":
            diff("signal", values, "missing:cubemx", None, [],
                 f"net {lab!r} names a use for this pin but the .ioc leaves it unassigned", problems=["cubemx"])
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
                exp = expected_cube_label(i_sig, owner, ioc.get("cube_label"), role, board.debug_style)
                if exp and exp != ioc.get("cube_label"):
                    problems.append("cubemx")
                    apply_to.append({"target": "ioc", "field": "GPIO_Label", "pin": key, "value": exp,
                                     "current": ioc.get("cube_label")})
            if c_fn is not None and c_fn != owner and not (c_net and s_net == c_net):
                problems.append("schematic")
            if problems:
                diff("function", values, "conflict" if ("cubemx" in problems and i_fn) else
                     ("missing:cubemx" if "cubemx" in problems else "conflict"), owner, apply_to,
                     "; ".join(problems), role=role, problems=problems)
        elif board.sheet and (i_fn or c_fn):
            diff("function", values, "missing:sheet", i_fn or c_fn, [], "sheet has no function for this pin", role=role)
    # ---- net: owner sheet ------------------------------------------------------
    gen = netnames.generate(prefix, pin_name, i_sig or s_sig, s_fn or i_fn, role=role, debug_style=board.debug_style) \
        if (i_sig or s_sig or s_fn or i_fn) else None
    values = {"cubemx": gen, "sheet": s_net, "schematic": c_net or (", ".join(sch.get("net_ambiguous", [])) or None),
              "symbol": None}
    if s_net or c_net or sch.get("net_ambiguous"):
        if sch.get("net_ambiguous"):
            diff("net", values, "conflict", s_net or gen, [], f"pin sits on several labelled nets: {sch['net_ambiguous']}",
                 generated=gen, role=role)
        elif s_net and c_net:
            if s_net == c_net:
                rel = netnames.relate(s_net, prefix, pin_name, i_sig, s_fn or i_fn)["relation"] if i_sig else "same"
                if rel == "different":
                    diff("net", values, "conflict", gen,
                         [_sheet_write(board, sh, key, "net", gen, s_net),
                          {"target": "altium", "kind": "net_label", "pin": key, "from": c_net, "to": gen,
                           "sheets": sch.get("sheets", [])}],
                         "sheet and schematic agree, but the name encodes a signal the .ioc does not assign",
                         generated=gen, role=role)
            else:
                rel = netnames.relate(c_net, prefix, pin_name, i_sig, s_fn or i_fn)["relation"]
                status = "variant" if rel in ("signal_spelling",) else "conflict"
                diff("net", values, status, s_net,
                     [{"target": "altium", "kind": "net_label", "pin": key, "from": c_net, "to": s_net,
                       "sheets": sch.get("sheets", [])}],
                     f"schematic label differs from the sheet's Net Name ({rel})", generated=gen, role=role)
        elif s_net and not c_net:
            if sch.get("labels") is not None and sch.get("wired") is not None:
                diff("net", values, "missing:schematic", s_net, [], "pin has no net label on the sheets"
                     + ("" if sch.get("wired") else " (and no wire)"), generated=gen, role=role)
        elif c_net and not s_net and board.sheet:
            if netnames.parse(c_net, prefix).form == "spare" and not i_sig:
                return out                                  # a free pin named after itself: nothing to record
            prop = c_net if netnames.parse(c_net, prefix).form != "other" else gen
            diff("net", values, "missing:sheet", prop, [_sheet_write(board, sh, key, "net", prop, None)],
                 "sheet has no Net Name for this pin", generated=gen, role=role)
    return out


# ---------------------------------------------------------------------------
# diagnosis: why does the discrepancy exist, and is anything actually wrong?
# ---------------------------------------------------------------------------

def _mechanism_words(signal: Optional[str]) -> str:
    fam = _family(signal)
    if fam in ("TIM", "LPTIM", "HRTIM"):
        return "a timer channel: input capture or PWM"
    if fam == "ADC":
        return "an ADC input"
    if fam == "DAC":
        return "a DAC output"
    if fam == "COMP":
        return "a comparator input"
    if signal and signal.startswith("USB_OTG"):
        return "a USB controller pin"
    if fam == "RCC":
        return "a clock output"
    return "a peripheral signal"


def diagnose(d: Diff, board: Board, ioc: dict, sh: dict, sch: dict, ioc_diag: dict) -> None:
    v, c = d.values, d.ctx
    sig, label = c.get("signal"), c.get("cube_label")
    problems = c.get("problems") or []
    prefix = board.prefix

    if d.field == "pin":
        d.kind, d.suggested = "phantom_pin", "ignore"
        d.why = ("The sheet lists this pin, but the MCU database for this package does not have it "
                 "(the sheet enumerates every port pin of the family).")
        return
    if d.status == "sheet_error":
        d.kind, d.suggested = "sheet_error", "decide"
        d.why = "A formula in the sheet evaluates to an error, so the tool cannot read this value."
        return

    if d.field == "signal":
        if d.status == "missing:cubemx":
            net = c.get("sheet_net") or c.get("schematic_net") or ""
            if "OSC" in net.upper():
                clk = ioc_diag.get("clock", {})
                d.kind, d.suggested = "clock_unconfigured", "decide"
                d.why = (f"The board wires a crystal here (net {net}), but the .ioc's clock tree does not use it: PLL source is "
                         f"{clk.get('pll_source') or 'unknown'}, HSE {'enabled' if clk.get('hse') else 'not enabled'}, "
                         f"LSE {'enabled' if clk.get('lse') else 'not enabled'}. If the crystal is meant to be used, enable "
                         f"HSE/LSE (Crystal/Ceramic Resonator) in CubeMX's RCC pane; that assigns the pin. If it is an optional "
                         f"fit, the .ioc is right and this row can be ignored.")
                d.apply_to = []
                return
            d.kind, d.suggested = "ioc_unassigned", "decide"
            d.why = (f"The sheet/schematic use this pin (net {net}) but the .ioc leaves it unassigned. "
                     f"Either the .ioc is behind, or the use was dropped.")
            return
        if problems == ["symbol"]:
            d.kind, d.suggested = "symbol_label_wrong", "accept"
            d.why = (f"CubeMX and the sheet agree on {sig}; only the symbol's group label says {c.get('symbol_label')!r}. "
                     f"A drawing error on the symbol, not a pinout question.")
            return
        if "schematic" in problems and "sheet" not in problems:
            role_word = netnames.parse(c.get("schematic_net") or "", prefix).signal
            if label and role_word and label.startswith(role_word):
                strict = netnames.generate(prefix, c.get("pin_name") or d.pin, sig, netnames.split_gpio_label(label)[1] or label,
                                           debug_style=board.debug_style)
                d.kind, d.suggested = "role_vs_signal", "ignore"
                d.why = (f"Two right descriptions of one pin. CubeMX uses {sig} as the MECHANISM ({_mechanism_words(sig)}); the net "
                         f"names the MEANING ({role_word}). The .ioc label already carries the meaning ({label!r}), so all three "
                         f"sources agree on what the pin does and differ only in which word fills the signal slot of the net name. "
                         f"Keep as is; the strict form would be {strict!r}.")
                d.proposed = v.get("sheet") or v.get("schematic")
                d.apply_to = []
                return
            d.kind, d.suggested = "convention_variant", "decide"
            d.why = (f"The schematic label {c.get('schematic_net')!r} does not follow the PREFIX_PIN_SIGNAL[__FUNCTION] form "
                     f"for signal {sig}.")
            return
        if "sheet" in problems:
            s_raw = (sh.get("signal") or "")
            if s_raw.startswith("GPIO_") and sig and not sig.startswith("GPIO_"):
                fam = _family(sig)
                if fam in _MECHANISM_FAMILIES:
                    d.kind, d.suggested = "role_vs_signal", "decide"
                    d.why = (f"The sheet models this pin as plain IO ({label or 'no label'}); CubeMX drives it through {sig} "
                             f"({_mechanism_words(sig)}), a mechanism behind that IO-like line. Both are right about the wire. "
                             f"Accept if the sheet should record the mechanism; ignore to keep it as IO.")
                    return
                if fam == "RCC":
                    d.kind, d.suggested = "dual_use", "decide"
                    d.why = (f"One pin, two roles. CubeMX drives {sig} (mode {c.get('mode')!r}) and labels it {label!r}; the sheet "
                             f"models it as IO; the net name {c.get('sheet_net') or c.get('schematic_net')!r} carries both. Decide "
                             f"which role the sheet should model, or ignore if both are intended.")
                    return
            d.kind, d.suggested = "sheet_stale", "accept"
            d.why = (f"The sheet's signal ({v.get('sheet')}) differs from CubeMX ({sig}); the .ioc owns the signal, "
                     f"so the sheet is behind.")
            return
        d.kind, d.suggested = "sheet_stale", "decide"
        d.why = f"Sources disagree on the signal: {problems}."
        return

    if d.field == "function":
        if d.status == "missing:cubemx":
            d.kind, d.suggested = "ioc_label_missing_function", "accept"
            d.why = ((f"The .ioc GPIO_Label is {label!r}: it lacks the __Function suffix that the sheet and schematic use "
                      f"({v.get('sheet')}). Adding it makes CubeMX carry the same meaning and gives the generated code a telling name.")
                     if label else
                     f"The .ioc has no GPIO_Label for this pin; the sheet and schematic call it {v.get('sheet')}.")
            return
        if d.status == "missing:sheet":
            d.kind, d.suggested = "sheet_gap", "decide"
            d.why = f"The .ioc/schematic name a function ({v.get('cubemx') or v.get('schematic')}) the sheet does not record."
            return
        if sig and _family(sig) == "ADC" and label and (label in (c.get("sheet_net") or "") or label in (c.get("schematic_net") or "")):
            d.kind, d.suggested = "role_vs_signal", "ignore"
            d.why = (f"Analog pin. The .ioc label {label!r} is the MEANING and the net name uses it; the sheet's function "
                     f"({v.get('sheet')!r}) is where the signal goes, not what it is. Nothing to correct.")
            d.proposed = label
            d.apply_to = []
            return
        d.kind, d.suggested = "function_mismatch", "decide"
        d.why = (f"The sources name different functions (CubeMX {v.get('cubemx')!r}, sheet {v.get('sheet')!r}, "
                 f"schematic {v.get('schematic')!r}).")
        return

    if d.field == "net":
        if d.status == "missing:sheet":
            d.kind, d.suggested = "sheet_gap", "accept"
            d.why = f"The schematic labels this pin {v.get('schematic')!r}; the sheet's Net Name is empty."
            return
        if d.status == "missing:schematic":
            d.kind, d.suggested = "schematic_stale", "decide"
            d.why = "The sheet names a net but no label sits on this pin in the schematic."
            return
        if "encodes a signal" in d.note:
            role_word = netnames.parse(c.get("sheet_net") or "", prefix).signal
            if sig and _family(sig) == "ADC":
                alt = netnames.generate(prefix, c.get("pin_name") or d.pin, sig, label or role_word, gpio_style=True)
                d.kind, d.suggested = "convention_variant", "decide"
                d.why = (f"Analog pin named in the peripheral form ({v.get('sheet')}); the convention writes analog pins with the "
                         f"meaning after a double underscore ({alt}). Accept normalises it; ignore keeps the current name.")
                d.proposed = alt
                d.apply_to = writes_for(d, alt, board)
                return
            if label and role_word and label.startswith(role_word):
                d.kind, d.suggested = "role_vs_signal", "ignore"
                d.why = (f"Same pin as the signal row: the net names the meaning ({role_word}), CubeMX holds the mechanism ({sig}), "
                         f"and the .ioc label already says {label!r}. Nothing is wrong; accept only if you want the strict form "
                         f"{c.get('generated')!r} everywhere.")
                d.proposed = v.get("sheet")
                d.apply_to = []
                return
            d.kind, d.suggested = "sheet_stale", "decide"
            d.why = "The sheet and schematic agree on a name whose signal part is not what CubeMX assigns."
            return
        if v.get("sheet") and v.get("schematic"):
            d.kind, d.suggested = "schematic_stale", "accept"
            d.why = (f"The schematic label ({v.get('schematic')}) differs from the sheet's Net Name ({v.get('sheet')}); "
                     f"the sheet owns the name.")
            return
        d.kind, d.suggested = "function_mismatch", "decide"
        d.why = d.note
        return


def _aggregate_symbol_edits(diffs: List[Diff]) -> None:
    """Several pins under one wrong label are one edit: say which pins it covers."""
    groups: Dict[tuple, List[str]] = {}
    for d in diffs:
        for w in d.apply_to:
            if w.get("target") == "altium" and w.get("kind") == "symbol_label":
                groups.setdefault((w.get("symbol"), w.get("part"), w.get("from"), w.get("to")), []).append(d.pin)
    for d in diffs:
        for w in d.apply_to:
            if w.get("target") == "altium" and w.get("kind") == "symbol_label":
                w["covers"] = groups[(w.get("symbol"), w.get("part"), w.get("from"), w.get("to"))]


def reconcile(board: Board, ioc: Dict[str, dict], sheet: Dict[str, dict], schem: Dict[str, dict],
              diagnostics: Optional[dict] = None) -> Report:
    keys = sorted(set(ioc) | set(sheet) | set(schem),
                  key=lambda k: _poskey(str((ioc.get(k) or schem.get(k) or {}).get("ball") or "ZZ99")) + (k,))
    diffs: List[Diff] = []
    agree = {f: 0 for f in FIELDS}
    diag = dict(diagnostics or {})
    for key in keys:
        i, s, c = ioc.get(key, {}), sheet.get(key, {}), schem.get(key, {})
        if ioc and key not in ioc:
            d = Diff(f"{key}:pin", key, c.get("ball"), "pin", {"cubemx": None, "sheet": s.get("name"),
                     "schematic": c.get("name"), "symbol": None}, "missing:cubemx", None, [],
                     "pin unknown to the MCU database / .ioc")
            diagnose(d, board, i, s, c, diag.get("ioc", {}))
            diffs.append(d)
            continue
        before = len(diffs)
        new = compare_pin(board, key, i, s, c)
        for d in new:
            diagnose(d, board, i, s, c, diag.get("ioc", {}))
        diffs += new
        seen_fields = {d.field for d in diffs[before:]}
        for f in FIELDS:
            if f not in seen_fields and any(src.get(f) for src in (i, s, c)):
                agree[f] += 1
    for err in (diag.get("sheet", {}) or {}).get("errors", []):
        d = Diff(f"{err['pin']}:sheet_error:{err['column']}", err["pin"], None, err["column"],
                 {"cubemx": None, "sheet": err["value"], "schematic": None, "symbol": None}, "sheet_error", None,
                 [{"target": "sheet", "field": err["column"], "pin": err["pin"], "value": None, "tab": err["tab"], "row": err["row"]}],
                 f"{err['tab']} row {err['row']}: formula error")
        diagnose(d, board, {}, {}, {}, {})
        diffs.append(d)
    _aggregate_symbol_edits(diffs)
    return Report(board.name, board.prefix, diffs, agree, diag)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def format_report(rep: Report, limit: int = 25) -> str:
    s = rep.summary()
    lines = [f"{rep.board}: {s['disagreements']} disagreements; agree: {s['agree']}",
             f"  by kind: {s['by_kind']}", f"  suggested: {s['suggested']}",
             f"  writes proposed by target: {s['writes_proposed_by_target']}"]
    for d in rep.diffs[:limit]:
        v = d.values
        lines.append(f"  {d.id:<18} {d.kind:<26} [{d.suggested}] cubemx={v.get('cubemx')!s} sheet={v.get('sheet')!s} "
                     f"schematic={v.get('schematic')!s}")
        lines.append(f"      why: {d.why}")
        lines.append(f"      accept: {d.fix_text()}")
    if len(rep.diffs) > limit:
        lines.append(f"  ... {len(rep.diffs) - limit} more")
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
    ncols = len(REVIEW_HEADER)
    plan = {"tab": tab, "exists": exists, "rows": len(rows) - 1, "columns": REVIEW_HEADER,
            "steps": ([] if exists else [f"create tab {tab!r}"]) + [f"clear {tab}!A1:Z5000", f"write {len(rows)} rows",
                                                                     f"dropdown on Decision ({chr(ord('A') + DECISION_COL)})",
                                                                     "freeze row 1"]}
    if not apply:
        return {"success": True, "applied": False, **plan, "next": "call with apply=True to write"}
    if not exists:
        sheets.tabs(wb, "create", title=tab, rows=max(len(rows) + 50, 200), cols=ncols + 4, apply=True)
        meta = sheets.meta(wb, refresh=True)
    t = meta.tab(tab)
    if any(sheets.read(wb, tab, header_row=None, max_rows=1)["rows"]):
        sheets.clear(wb, tab, "A1:Z5000", apply=True)
    w = sheets.write(wb, tab, "A1", rows, apply=True)
    n = len(rows)
    reqs = [{"setDataValidation": {"range": {"sheetId": t.gid, "startRowIndex": 1, "endRowIndex": n,
                                             "startColumnIndex": DECISION_COL, "endColumnIndex": DECISION_COL + 1},
                                   "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": d} for d in DECISIONS]},
                                            "showCustomUi": True, "strict": False}}},
            {"updateSheetProperties": {"properties": {"sheetId": t.gid, "gridProperties": {"frozenRowCount": 1}},
                                       "fields": "gridProperties.frozenRowCount"}}]
    sheets.batch_update(wb, reqs, apply=True)
    return {"success": True, "applied": True, **plan, "write": {k: w.get(k) for k in ("cells_changed", "verified", "snapshot")}}


def collect(board: Board, sheets) -> Dict[str, dict]:
    """Decisions from the review tab: {diff id: {decision, custom, note}}."""
    r = sheets.read(board.sheet.workbook, board.sheet.review_tab, header_row=1, max_rows=5000,
                    max_cols=len(REVIEW_HEADER), as_records=True)
    out = {}
    for rec in r["rows"]:
        if rec.get("Id") and rec.get("Decision"):
            out[rec["Id"]] = {"decision": rec["Decision"], "custom": rec.get("Custom value"), "note": rec.get("Note"),
                              "proposed": rec.get("Proposed")}
    return out


# ---------------------------------------------------------------------------
# applying decisions
# ---------------------------------------------------------------------------

def writes_for(d: Diff, value, board: Board) -> List[dict]:
    """Every write that makes all sources say `value` for this diff's field."""
    c, v = d.ctx, d.values
    out: List[dict] = []
    if d.field == "signal":
        if value != v.get("cubemx"):
            out.append({"target": "ioc", "field": "Signal", "pin": d.pin, "value": value, "current": v.get("cubemx")})
        if value != (v.get("sheet") or "").replace(" (role)", "") and board.sheet:
            out.append({"target": "sheet", "field": "signal", "pin": d.pin, "value": value, "current": v.get("sheet")})
        out += [w for w in d.apply_to if w.get("kind") == "symbol_label"]
    elif d.field == "function":
        exp = expected_cube_label(c.get("signal"), value, c.get("cube_label"), c.get("role"), board.debug_style)
        if exp and exp != c.get("cube_label"):
            out.append({"target": "ioc", "field": "GPIO_Label", "pin": d.pin, "value": exp, "current": c.get("cube_label")})
        if value != v.get("sheet") and board.sheet:
            out.append({"target": "sheet", "field": "function", "pin": d.pin, "value": value, "current": v.get("sheet")})
        if v.get("schematic") is not None and value != v.get("schematic") and c.get("schematic_net"):
            new = netnames.generate(board.prefix, c.get("pin_name") or d.pin, c.get("signal"), value, role=c.get("role"),
                                    debug_style=board.debug_style)
            out.append({"target": "altium", "kind": "net_label", "pin": d.pin, "from": c.get("schematic_net"), "to": new,
                        "sheets": c.get("sheets", [])})
    elif d.field == "net":
        if value != v.get("sheet") and board.sheet:
            out.append({"target": "sheet", "field": "net", "pin": d.pin, "value": value, "current": v.get("sheet")})
        if c.get("schematic_net") and value != c.get("schematic_net"):
            out.append({"target": "altium", "kind": "net_label", "pin": d.pin, "from": c.get("schematic_net"), "to": value,
                        "sheets": c.get("sheets", [])})
    return out


def plan_writes(rep: Report, decisions: Dict[str, dict], board: Optional[Board] = None) -> dict:
    """Turn decisions into per-target write lists. 'accept' takes the diff's
    apply_to; 'keep X' / 'custom' compute the writes that make every source
    say that value; 'ignore' skips."""
    by_target: Dict[str, list] = {"sheet": [], "ioc": [], "altium": []}
    skipped, unknown = [], []
    by_id = {d.id: d for d in rep.diffs}
    for did, dec in decisions.items():
        d = by_id.get(did)
        if not d:
            unknown.append(did)
            continue
        choice = (dec.get("decision") or "").lower().strip()
        if choice == "ignore" or not choice:
            skipped.append(did)
            continue
        if choice == "accept":
            writes = list(d.apply_to)
        else:
            value = dec.get("custom") if choice == "custom" else d.values.get(choice.replace("keep ", ""))
            if isinstance(value, str):
                value = value.replace(" (role)", "")
            writes = writes_for(d, value, board) if board else [{**a, "value": value, "to": value} for a in d.apply_to]
        for w in writes:
            by_target.setdefault(w["target"], []).append({**w, "diff": did})
    return {"writes": by_target, "skipped": skipped, "unknown_ids": unknown,
            "counts": {k: len(v) for k, v in by_target.items()},
            "described": {k: [describe_write(w) for w in v] for k, v in by_target.items()}}


def apply_sheet_writes(board: Board, sheets, writes: List[dict], apply: bool = False) -> dict:
    """Sheet writes go through update_by_key on the table that holds the
    column for that role (a signal on an MC-style table becomes Peripheral +
    Peripheral Pin)."""
    if not writes:
        return {"success": True, "applied": apply, "note": "no sheet writes"}
    results = []
    for which in ("pins", "signals"):
        spec = getattr(board.sheet, which)
        if not spec:
            continue
        updates: Dict[str, dict] = {}
        for w in writes:
            if w.get("parts") and w.get("tab") == str(spec.tab):
                updates.setdefault(w["pin"], {}).update({c: (v if v is not None else "") for c, v in w["parts"].items()})
                continue
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
            key = (w.get("symbol") or "?")
            entry = {"part": w.get("part"), "from": w.get("from"), "to": w.get("to"), "covers": w.get("covers") or [w["pin"]]}
            if entry not in symbol_edits.setdefault(key, []):
                symbol_edits[key].append(entry)
    return {"net_label_renames_by_sheet": renames, "symbol_label_edits": symbol_edits,
            "note": "apply with the altium MCP tools (set_net_labels per sheet; symbol edits need the symbol-edit tool)"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(board_path: str, json_out: Optional[str] = None, sheets=None) -> Report:
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
    ap.add_argument("--publish", action="store_true", help="write the Reconcile tab (only after the user agreed)")
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
