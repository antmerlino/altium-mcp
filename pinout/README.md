# pinout - CubeMX <-> Altium MCU symbols

Pure Python. No Altium is needed for anything in this package; Altium is only
ever the *last* step, where a finished, previewed spec is applied and then
read back to verify.

```
CubeMX db/mcu/*.xml  --mcu_db-->  \
                                     model.Pinout  --layout-->  SymbolSpec  --preview-->  SVG
project.ioc          --ioc----->  /       ^  |                      |
                                          |  +--apply_to_ioc-->  .ioc      +--(Altium) create_schematic_symbol
                                    JSON / XLSX                                   then get_symbol_primitives --verify.compare--> report
```

| Module | Purpose |
|---|---|
| `ioc.py` | Read and **modify** a CubeMX `.ioc` - only the pin keys move; everything CubeMX owns round-trips byte for byte, and `Mcu.PinN`/`Mcu.PinsNb` stay consistent. Handles the spellings real H7 files use: `.properties` key escaping (`PA13\ (JTMS/SWDIO).Signal`), `PC2_C`, `PH0-OSC_IN (PH0)`; keeps CubeMX's ball-position order of `Mcu.PinN`. |
| `mcu_db.py` | The authoritative pin table from CubeMX's own MCU XML: every pin, its package position, its type, every alternate signal. |
| `model.py` | The canonical `Pinout`: MCU DB + `.ioc` + **designer-owned columns** (function, group, part, net) that survive regeneration. JSON primary; XLSX optional via openpyxl. |
| `naming.py` | House rules measured from the library: `EMBEDDED_IC_<MPN>_<BOARD>`, `PREFIX_PIN_SIGNAL__FUNCTION` net labels, `PA13 (JTMS/SWDIO)`, electrical types derived per signal. Peripheral grouping for G4 (`SYS_JT*`) and H7 (`DEBUG_*`, `ADCx_*`, `USB_OTG_HS_*`) signal names; `canonical_group` folds hand-written labels (QSPI, OTGFS, UART6, OTG_ULPI) onto CubeMX's names. |
| `layout.py` | `Pinout` -> symbol spec in the exact pipe-string format `create_schematic_symbol` consumes. Two strategies: **port split** (one part per port, generic) and **functional split** (peripheral-labelled clusters, board-specific). |
| `preview.py` | Spec -> SVG, one file per part, so layout is inspected *before* any Altium call. |
| `style.py` | The palette (body fill/outline, **divider**, pin and label colours, line width) as RRGGBB, converted to Altium's `$00BBGGRR`. One `Style` drives preview, synthetic dumps and `verify.check_style` so they cannot drift. Defaults are Altium stock; `house_style.json` is the palette read from `EMBEDDED_IC_STM32H753IIK6_X57` (white body, `800000` outline, `980000` dividers and labels, black pins) - load it with `Style.load`. |
| `schdoc.py` | Read a schematic sheet (.SchDoc) **offline** with `olefile`: a placed multi-part component carries every part's pins and graphics, so `component_dump` rebuilds the library symbol in `get_symbol_primitives` shape without Altium, and `pin_nets` follows the sheet's wires to give pin -> net label. Used to check the X2 Control Board against its `.ioc` files while Altium stayed untouched. |
| `verify.py` | Spec vs a `get_symbol_primitives` dump, normalised to the same shape: exactly which pins are missing, unexpected or *changed* (and in which field), which graphics differ. Nothing generated is believed until it passes this. |
| `audit.py` | An **existing** symbol vs the `.ioc` that should describe it. Infers each pin's group from geometry (clusters by gaps and dividers, labels within a cluster's rows, one label naming a divider-bounded region, `PORTx` labels carrying no peripheral) and reports hard contradictions (pin missing, VSS drawn as NC, a pin the `.ioc` gives to SPI3 drawn under SDMMC1) separately from soft ones (name variants, a chip-select drawn with its SPI, electrical types). Run it before regenerating a board's symbol: it is the list of what changed. |

## Tests

Everything runs against synthetic fixtures in `tests/fixtures/` - an invented
20-pin `FAKE32C20Ux` and a `fake_board.ioc` for it. No real design data.

```bash
server/.venv/Scripts/python.exe -m unittest discover -s pinout/tests -v
```

The spreadsheet round-trip test skips unless `openpyxl` is installed.

## Design rules that matter

- **The `.ioc` owns pin <-> signal. The designer owns function/group/part/net.**
  Symbols and net labels are generated views. "Back and forth with CubeMX"
  means: edit the `.ioc` -> regenerate -> `merge_overrides` keeps what the
  designer typed -> diff -> apply.
- **The `.ioc` labels carry the function too.** The house `GPIO_Label`
  convention `<role>__<Function>` (`USART2_RX__BB_ESC_TX`) seeds the
  designer-owned `function` column, so a real `.ioc` arrives with most of
  the net-label suffixes already filled in.
- **An existing symbol is audited, not trusted.** `audit.py` against a
  `get_symbol_primitives` dump says exactly where the library part and the
  `.ioc` disagree; regeneration is then a decision, not a surprise.
- **Nothing is written to Altium until it has been previewed**, and nothing
  written is trusted until it has been read back (`get_symbol_primitives`)
  and compared (`verify.compare`) against the spec that produced it. A tool reporting success once drew
  nothing at all; only the render showed it.
- Coordinates are mils, y up, 100-mil pitch. Pin length is a parameter
  (200 in the G431 symbols, 300 in the H753 ones).
