"""Each source of pin facts, read into one shape.

    facts[pin_key] = {"signal", "function", "cube_label", "net", "group", ... , "_src": {...raw...}}

pin_key is the bare port pin (PA13 for "PA13 (JTMS/SWDIO)", PA0_C stays distinct)
so the four sources join on it. Every adapter also returns diagnostics: what
it could not read, sheet cells holding #REF!, pins it saw that the MCU does
not have.

    ioc_facts(board)                    CubeMX: signal, mode, GPIO_Label, function derived from the label
    sheet_facts(board, sheets)          the pinning workbook, through gsheets.core.Sheets (read-only here)
    schematic_facts(board)              the Altium sheets read offline: net label(s) per pin, symbol group
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import naming, netnames
from .audit import infer_clusters, is_port_label
from .ioc import IocFile
from .manifest import Board, TableSpec
from .mcu_db import load_mcu_xml
from .model import Pinout
from .verify import ELECTRICAL_NAME

Facts = Dict[str, dict]
_ERR = re.compile(r"^#(REF|N/A|VALUE|NAME|DIV/0|NUM|ERROR)!?", re.I)


def pin_key(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    return naming.base_pin(str(name).strip().replace(" C", "_C"))


def is_sheet_error(v) -> bool:
    return isinstance(v, str) and bool(_ERR.match(v.strip()))


def _clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# ---------------------------------------------------------------------------
# CubeMX
# ---------------------------------------------------------------------------

def function_from_cube_label(signal: Optional[str], label: Optional[str]) -> Optional[str]:
    """OBS1_3V3_SENSE (analog/GPIO label) -> that; USART2_RX__BB_ESC_TX -> BB_ESC_TX;
    a label that merely re-spells the signal (SWDIO for DEBUG_JTMS-SWDIO) -> None."""
    if not label:
        return None
    if "__" in label:
        return label.split("__", 1)[1] or None
    if signal and label in netnames.signal_spellings(signal):
        return None
    if (not signal or signal.startswith("GPIO_")) and netnames.ROLE_WORD.match(label):
        return None                                 # a role word (SPI5_nCS1), not a function
    if signal and not signal.startswith("GPIO_") and label.startswith(netnames.preferred_spelling(signal) or "\0"):
        rest = label[len(netnames.preferred_spelling(signal)):]
        return rest.lstrip("_") or None
    return label


def ioc_facts(board: Board) -> Tuple[Facts, dict]:
    mcu = load_mcu_xml(board.mcu_xml)
    ioc = IocFile.load(board.ioc)
    p = Pinout.from_sources(mcu, ioc, board=board.prefix)
    facts: Facts = {}
    for rec in p.pins:
        if not naming.is_port_pin(rec.name):
            continue
        key = pin_key(rec.name)
        role = netnames.split_gpio_label(rec.label)[0] if (not rec.signal or rec.signal.startswith("GPIO_")) else None
        facts[key] = {"name": rec.name, "ball": rec.number, "signal": rec.signal, "mode": rec.mode,
                      "cube_label": rec.label, "function": function_from_cube_label(rec.signal, rec.label), "role": role,
                      "group": naming.canonical_group(naming.peripheral_group(rec.signal)) if rec.signal else None,
                      "mcu_type": rec.mcu_type, "alternates": list(rec.signals)}
    rcc = {k: ioc.get(k) for k in ioc.keys("RCC.")}
    pll = rcc.get("RCC.PLLSourceVirtual") or rcc.get("RCC.PLLSource") or ""
    diag = {"mcu": mcu.ref_name, "package": mcu.package, "ioc_pins_assigned": len(p.assigned()),
            "port_pins": len(facts), "ioc_pins_not_in_mcu": p.meta.get("ioc_pins_not_in_mcu", ""),
            "clock": {"pll_source": pll.replace("RCC_PLLSOURCE_", "") or None,
                      "hse": any(r.signal and r.signal.startswith("RCC_OSC_") for r in p.pins),
                      "lse": any(r.signal and r.signal.startswith("RCC_OSC32_") for r in p.pins),
                      "hse_value": rcc.get("RCC.HSE_VALUE")}}
    return facts, diag


# ---------------------------------------------------------------------------
# the pinning workbook
# ---------------------------------------------------------------------------

def _records(sheets, workbook: str, spec: TableSpec) -> Tuple[List[dict], List[str]]:
    r = sheets.read(workbook, spec.tab, header_row=spec.header_row, max_rows=5000, max_cols=60, as_records=True)
    headers = [c["header"] for c in r["columns"]]
    missing = [h for h in [spec.key, *spec.columns.values()] if h not in headers]
    return r["rows"], missing


def _mc_style_signal(peripheral: Optional[str], ppin: Optional[str], direction: Optional[str]) -> Optional[str]:
    """Signal Definitions rows: Peripheral + Peripheral Pin -> CubeMX signal;
    the IO peripheral is a GPIO whose 'pin' is the function name."""
    if not peripheral:
        return None
    if peripheral.upper() in ("IO", "GPIO"):
        d = (direction or "").upper()
        return "GPIO_Input" if d in ("I", "IN", "INPUT") else "GPIO_Output"
    return f"{peripheral}_{ppin}" if ppin else peripheral


def _mc_style_function(peripheral: Optional[str], ppin: Optional[str], other_end: Optional[str],
                       postfix: Optional[str]) -> Optional[str]:
    if peripheral and peripheral.upper() in ("IO", "GPIO"):
        return f"{other_end}_{ppin}" if other_end and ppin else (ppin or other_end)
    if other_end and postfix:
        return f"{other_end}_{postfix}"
    return other_end or postfix or None


def sheet_facts(board: Board, sheets) -> Tuple[Facts, dict]:
    """Read the pins table and/or the signals table named in the manifest."""
    if not board.sheet:
        return {}, {"note": "no sheet in manifest"}
    facts: Facts = {}
    diag: dict = {"errors": [], "missing_columns": {}, "unknown_pins": [], "duplicate_keys": []}
    wb = board.sheet.workbook

    def put(key, role, value, row, tab):
        f = facts.setdefault(key, {"_rows": {}})
        f["_rows"][tab] = row["_row"]
        if is_sheet_error(value):
            diag["errors"].append({"tab": tab, "row": row["_row"], "column": role, "value": value, "pin": key})
            return
        if value is not None and (role not in f or f.get(role) in (None, "")):
            f[role] = value
        elif value is not None and f.get(role) != value:
            f.setdefault("_conflicts", []).append({"role": role, "tab": tab, "kept": f.get(role), "also": value})

    for which in ("pins", "signals"):
        spec: Optional[TableSpec] = getattr(board.sheet, which)
        if not spec:
            continue
        rows, missing = _records(sheets, wb, spec)
        if missing:
            diag["missing_columns"][str(spec.tab)] = missing
        seen = set()
        for row in rows:
            raw_key = _clean(row.get(spec.key))
            if not raw_key:
                continue
            key = pin_key(raw_key)
            if not naming.is_port_pin(raw_key.replace(" C", "_C")):
                diag["unknown_pins"].append({"tab": str(spec.tab), "row": row["_row"], "key": raw_key})
                continue
            if "ball" in spec.columns and _clean(row.get(spec.columns["ball"])) in (None, "-"):
                diag["skipped_no_ball"] = diag.get("skipped_no_ball", 0) + 1
                continue
            if (which, key) in seen:
                diag["duplicate_keys"].append({"tab": str(spec.tab), "row": row["_row"], "key": raw_key})
            seen.add((which, key))
            vals = {role: _clean(row.get(col)) for role, col in spec.columns.items()}
            f = facts.setdefault(key, {"_rows": {}})
            f.setdefault("name", raw_key)
            for role in ("net", "net_generated", "net_override", "cube_label", "label_override", "group",
                         "direction", "power_domain", "software_name", "notes", "checked", "peripheral",
                         "peripheral_pin", "other_end", "postfix"):
                if role in vals and vals[role] is not None:
                    put(key, role, vals[role], row, str(spec.tab))
            # signal
            if vals.get("signal") is not None:
                put(key, "signal", vals["signal"], row, str(spec.tab))
            elif "peripheral" in vals:
                sig = _mc_style_signal(vals.get("peripheral"), vals.get("peripheral_pin"), vals.get("direction"))
                if sig:
                    put(key, "signal", sig, row, str(spec.tab))
            # function (and, for GPIO pins, the role word)
            sig_here = f.get("signal") or vals.get("signal")
            if vals.get("function") is not None:
                put(key, "function", vals["function"], row, str(spec.tab))
            elif vals.get("label_override") or vals.get("cube_label"):
                lab = vals.get("label_override") or vals.get("cube_label")
                fn = function_from_cube_label(sig_here, lab)
                if fn:
                    put(key, "function", fn, row, str(spec.tab))
                if not sig_here or sig_here.startswith("GPIO_"):
                    role = netnames.split_gpio_label(lab)[0]
                    if role:
                        put(key, "role", role, row, str(spec.tab))
            elif "peripheral" in vals:
                fn = _mc_style_function(vals.get("peripheral"), vals.get("peripheral_pin"), vals.get("other_end"),
                                        vals.get("postfix"))
                if fn:
                    put(key, "function", fn, row, str(spec.tab))
                per = vals.get("peripheral")
                if per and per.upper() not in ("IO", "GPIO") and vals.get("peripheral_pin"):
                    put(key, "role_candidate", f"{per}_{vals['peripheral_pin']}", row, str(spec.tab))
    for f in facts.values():
        if f.get("group"):
            f["group"] = naming.canonical_group(f["group"])
        elif f.get("signal"):
            f["group"] = naming.canonical_group(naming.peripheral_group(f["signal"]))
    diag["pins"] = len(facts)
    return facts, diag


# ---------------------------------------------------------------------------
# Altium, offline
# ---------------------------------------------------------------------------

def schematic_facts(board: Board) -> Tuple[Facts, dict]:
    from .schdoc import component_dump, find_components, pin_nets, read_records
    if not board.altium:
        return {}, {"note": "no altium in manifest"}
    proj = Path(board.altium.project_dir)
    ref = board.altium.symbol
    facts: Facts = {}
    diag: dict = {"sheets": {}, "symbol": None}
    dump = None
    for sh in board.altium.sheets:
        path = proj / f"{sh}.SchDoc"
        if not path.exists():
            diag["sheets"][sh] = "missing"
            continue
        recs = read_records(path)
        comps = find_components(recs, ref)
        diag["sheets"][sh] = f"{len(comps)} instance(s)"
        for ci in comps:
            if dump is None:
                dump = component_dump(recs, ci)
            for num, info in pin_nets(recs, ci).items():
                key = pin_key(info["name"])
                if not naming.is_port_pin(info["name"].replace(" C", "_C")):
                    continue
                f = facts.setdefault(key, {"name": info["name"], "ball": num, "labels": [], "sheets": []})
                f["labels"] = sorted(set(f["labels"]) | set(info["nets"]))
                f["sheets"].append(sh)
                f["wired"] = f.get("wired", False) or info["wired"]
    if dump is not None:
        diag["symbol"] = {"name": dump["symbol_name"], "parts": dump["part_count"],
                          "pins": sum(1 for p in dump["primitives"] if p["type"] == "pin")}
        group_of = {}
        for c in infer_clusters(dump):
            for p in c.pins:
                group_of[str(p["pin_number"])] = (c.label, c.inherited, c.part)
        for p in dump["primitives"]:
            if p["type"] != "pin" or not naming.is_port_pin(p["pin_name"].replace(" C", "_C")):
                continue
            key = pin_key(p["pin_name"])
            f = facts.setdefault(key, {"name": p["pin_name"], "ball": p["pin_number"], "labels": [], "sheets": []})
            lab, inherited, part = group_of.get(str(p["pin_number"]), (None, False, p.get("owner_part_id")))
            f["symbol_label"] = lab
            f["symbol_label_inherited"] = inherited
            f["symbol_part"] = part
            f["group"] = None if (lab is None or is_port_label(lab)) else naming.canonical_group(lab)
            f["electrical"] = ELECTRICAL_NAME.get(int(p["electrical"]), p["electrical"])
    for f in facts.values():
        labels = f.get("labels", [])
        f["net"] = labels[0] if len(labels) == 1 else None
        if len(labels) > 1:
            f["net_ambiguous"] = labels
        parsed = [netnames.parse(l, board.prefix) for l in labels]
        per = [q for q in parsed if q.form == "peripheral"]
        gp = [q for q in parsed if q.form == "gpio"]
        f["signal"] = per[0].cubemx_signal if per else ("GPIO" if gp else None)
        f["function"] = (per[0].function if per else gp[0].function if gp else None)
    diag["pins"] = len(facts)
    return facts, diag
