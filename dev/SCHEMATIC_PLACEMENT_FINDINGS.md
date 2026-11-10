# Placing symbols on a schematic sheet by script

General findings (no company data). Moved out of `DBLIB_FINDINGS.md` when upstream untracked that file
(2026-11, merge of origin/main).

## Replicating a symbol out of a SchLib does not draw

The recipe "open the `.SchLib`, `Replicate` the component, register the copy on the target sheet" was
re-measured on AD21 with a multi-part symbol (`EMBEDDED_IC_STM32G431C8U6`) onto a scratch sheet:

- the component IS created - the iterator finds it with the right designator, `LibReference` and
  `Location` (20000000, 50000000)
- but Altium **does not draw it**. A `render_schematic` of the sheet shows no such component anywhere,
  while every API-level signal reports success
- and its pins keep **library-relative** coordinates (pin 8 at 2000000,0 rather than 22000000,50000000),
  so a wire routed to them would miss. Re-assigning `Component.Location` after registration does not
  change them; `GraphicallyInvalidate` does not help.

Re-confirmed on Altium Designer Develop (2026-11-05) with a single-part 2x2 connector symbol and
`MoveToXY` before registering: only the designator and comment text appear, no body or pins.

## What works: copy a placed instance from another sheet

1. Open the source sheet that has the symbol placed (any project) and `SbFind` the placed component.
2. `Replicate` it, `MoveByXY` to the target spot, `SbRegister` it on the target sheet - pins come with
   absolute coordinates and it draws.
3. Re-point it: `LibReference`, `DesignItemId`, `SourceLibraryName` (writable), and give it a NEW
   `UniqueId` (the first replica keeps the source's). `DatabaseTableName` is not writable.
4. Remove the source's parameters and implementations: collect them in a `TInterfaceList` first
   (`TStringList.AddObject` breaks the interfaces and removal silently does nothing), then
   `RemoveSchObject` / `RemoveSchImplementation`; add the new footprint implementation and parameters.

A database-linked part placed by a person (Components panel) is still the cleanest result; prefer asking
for one when the DbLib has the part.
