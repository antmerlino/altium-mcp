# Generating a readable schematic

Rules for programmatic schematic generation, each one learned by getting it
wrong and having a PCB designer point at the result. An agent that follows
these should not repeat the mistakes.

Tooling: a circuit spec (part data from
[`make_part_spec.py`](make_part_spec.py)) →
[`build_circuit.pas`](build_circuit.pas) (build) →
[`check_connectivity.pas`](check_connectivity.pas) (verify), with
[`harvest_param_placement.pas`](harvest_param_placement.pas) as a one-time
setup step per library.

## 1. A pin's connection point is not `Pin.Location`

`ISch_Pin.Location` is the end attached to the component **body**. The
electrical end is `PinLength` further along `Pin.Orientation`
(`0/1/2/3` = right/up/left/down):

```pascal
x := CoordToMils(Pin.Location.X);  y := CoordToMils(Pin.Location.Y);
len := CoordToMils(Pin.PinLength);
if (Pin.Orientation = 0) then x := x + len
else if (Pin.Orientation = 1) then y := y + len
else if (Pin.Orientation = 2) then x := x - len
else if (Pin.Orientation = 3) then y := y - len;
```

`Pin.Orientation` already composes with the component's rotation — do not add
the parent's orientation on top.

Verified against a hand-wired production sheet
([`verify_pin_convention.pas`](verify_pin_convention.pas)): wire endpoints land
on `Location + PinLength` **119** times and on `Location` **0** times.

Wiring to `Location` instead draws the wire through the pin and out the far
side. It looks attached, leaves the pin unconnected, and shorts any neighbour
sharing that connection column.

## 2. Never run a riser in a pin's connection column

All of a symbol's pins on one side usually share an x. A vertical wire in that
column passes through every one of them on its way past. Offset risers by at
least 100 mil, and prefer 200–300 for readability.

## 3. Leave a wire between a pin and a node

A junction sitting directly on a pin is bad practice. Place shunt parts one
grid *below* the rail they tap, so there is a short wire between the pin and
the junction:

```
rail  ────────┬────────         tap gap = 1 grid (100 mil)
              │
           ┌──┴──┐  C1 top pin
```

Tight-packing onto the pin is acceptable only when fitting a dense sheet,
which is rare.

## 4. Ground is one grid across, one grid down

Not three. `U1.GND → 100 left → 100 down → GND port`. Shunt parts drop
100 mil from their bottom pin to their own port.

## 5. Rails belong next to the pins they serve

Floating a rail 1000 mil above the part to "keep it clear" just wastes sheet.
Put the rail one pin-row above the highest pin it connects, and route signals
straight out of their pin's own height wherever possible:

```
U1 pin rows:  FB 3000, VOUT 3300, SW 3600   ->   input rail 4100
LED+ leaves VOUT at 3300 and runs straight to J1 - no riser at all
LED- leaves FB at 3000, one riser near the connector, done
```

A signal that leaves a pin, jogs down, runs across and comes back up is a
routing failure, not a style choice.

## 6. A wire must never cross a component body

A shunt part hanging off a rail occupies vertical space *below* it. If another
net runs through that band, the wire is drawn straight through the symbol - it
looks connected to the part and is not.

Concretely: LED+ at y=3300 with C2 hanging 300 mil below it occupies
y=2900..3200, and LED- ran at y=3000. The fix was not to bend the wire but to
turn LED- down to the left of C2 and drop LED+ to its right, so neither
crosses.

Before placing a shunt, check what else routes through the band it will
occupy. `check_connectivity.pas` now tests every axis-aligned segment against
each component's DRAWN body (graphics only - wires legitimately end on pins,
which stick out past the body).

## 7. Power ports: harvest the style, and leave room for the label

Do not guess between the ground symbols. Read what the house style uses from a
real sheet (`ePowerGndPower`=4, `ePowerGndSignal`=5, `ePowerGndEarth`=6 are
easy to confuse, and the wrong one looks plausible):

| Net | Style | Orientation | ShowNetName |
|---|---|---|---|
| GND | **5** `ePowerGndSignal` — digital ground | 3 (down) | False |
| supply rails | **2** `ePowerBar` | 1 (up) | True |

**`ShowNetName := False` does not hide the label.** It reads back as False and
Altium still draws "GND" below the symbol. So a ground port always occupies
roughly 300 mil below itself, and **no wire may be routed through that band** —
the label crowds it and looks like a connection. Either move the part clear of
the run, or drop the crossing wire further away.

## 8. Net labels must touch the wire

A label near a wire names nothing. Place it **on** a wire coordinate.
`check_connectivity.pas` tests each label against every segment (endpoint or
interior) and reports any that miss.

Caveat: that proves the geometric precondition, not the compiled net name.
Confirming the net Altium actually assigns needs the project compiled, which a
free document cannot do.

## 9. Parameter text: harvest it, do not invent it

Placement is an offset from the component's `Location`, plus `Justification`
and `Orientation` — the method in the user's `CopyParamPlacement.pas`.
**`Justification` is what makes a column line up**; setting position alone
leaves it ragged, and setting `Orientation`/`Autoposition` alone does nothing
visible at all.

`harvest_param_placement.pas` reads a hand-drawn sheet and records, per
`LibReference`:

```
PLACE|CAP-NP|1|DESIGNATOR|0|300|2|0      # dx, dy, justification, orientation
PLACE|CAP-NP|1|VALUE|0|200|2|0
```

Treat the harvested numbers as relative **spacing**, then translate the block:

- **Clear the body.** Applied raw, a right-justified column anchors at the
  component origin, which for `CAP-NP` is over the plates.
- **Keep the block together.** The designator's `dy` can reach above the top
  pin, leaving it on the far side of a rail from its own parameters.
- **Allocate room for it.** A block is roughly `records × 100` mil tall and
  hangs below the body. Two parts stacked in one column will collide through
  their text long before their symbols touch.

### Measuring the body: not with `BoundingRectangle`

`Component.BoundingRectangle` **includes the parameter text**, so anchoring
text to it feeds back on itself and walks the block off the sheet. Measure the
extent from a child iterator filtered to drawable primitives:

```pascal
Iter.AddFilter_ObjectSet(MkSet(ePin, eLine, eRectangle, eArc, ePolyline, eEllipse));
```

Unfiltered, the iterator returns objects whose `BoundingRectangle` is invalid
and kills the script.

## 10. Mirroring: `Mirror(Axis)`, not `IsMirrored`

| Call | Effect |
|---|---|
| `Component.IsMirrored := True` | flag sets, **pins do not move** — display only |
| `Component.Mirror(Axis)` | real transform; pins moved `+200 → -200` |

`Axis` can be the component's own `Location`. This is the editor's **X** key.
Check which way a symbol's pins already point before rotating: `HEADER-2X1`
points left natively, so rotating it 180° puts the body between the incoming
wires and its own connection points.

## 11. Rotating a part rotates its text

Straighten designator/comment/parameters **after** any parameter-position
reset, not before — the reset re-derives placement from the body and discards
an orientation assigned earlier.

## 12. Verify with something that does not share the builder's assumptions

The original `check_connectivity.pas` computed hot ends with the **same
formula as the builder**. They agreed with each other while both could have
been wrong, and it passed a sheet with two pins wired to dead ends. A checker
that inherits the hypothesis under test catches typos, never systematic error.

What the checker distinguishes now:

| Situation | Verdict |
|---|---|
| wire endpoint on a pin's hot end | connected |
| wire crossing a pin, **junction present** | valid tap (shunt on a rail) |
| wire crossing a pin, no junction | **short** |
| junction with < 3 branches | decoration — usually an overshoot |
| net label not on a wire | **names nothing** |
| wire crossing a drawn body | **routed through a symbol** |

Branch counting: a wire *ending* at a point is one branch, a wire *passing
through* is two, a pin landing there is one more.

## 13. Net labels go at the free end, reading back along the wire

A label next to its pin, with the wire running on past it to a dead end,
reads as two things. Put the label **at the free end** of its wire. Then
justify it so the text sits on the wire and runs back towards the pin:
bottom-right (2) when the wire ends to the right, bottom-left (0) when it
ends to the left. A middle-justified label (3/5) anchored on the end, with
its text continuing past the end like a port, is the other accepted style.
What reads wrong is bottom-justified text hanging off the end of the wire.

## 14. Part text: beside the body, never on a wire

A designator or value must not touch a wire, a pin, another part or other
text. For a vertical two-pin part with nets above and below, put the
designator left of the body and the value right of it rather than stacking
both on one side, where they run into the rail.

## 15. Sheet symbols: entries close to the top, text above

Keep Altium's default look. The first sheet entry sits one grid below the
top edge (100 mil, or 3 mm on a 1 mm grid), not several grids down; the
bottom may keep more room. The designator and the file name both go above
the symbol, bottom-left justified at its left edge: the file name on the top
edge and the designator one line (100 mil) above it.

## 16. Check the saved sheet

`check_schematic` (server/schematic_check.py) reads a saved `.SchDoc`, a
`.PrjPcb` or a folder without Altium and reports rules 3, 6, 8, 13, 14 and 15
with coordinates, plus suggested label positions and a text arrangement
that collides with nothing. Run it after every drawing pass.

## 17. A cluster is more than its wires and labels

Measured on the finished X2 Control Board and the X9 Main Board MCU comms
sheets (Verge Aero house style). Re-routing a cluster and stopping at wires
plus net labels leaves the rest on the wrong net without any warning. Each
interface cluster on an MCU sheet carries ALL of this:

| Object | Where | Detail |
|---|---|---|
| wire per pin | pin's electrical point to a fixed column (left 3700 mil, right 12000 mil) | one row per pin, 100 mil pitch |
| net label | on the wire, 200 mil from the far end (left x=3900, right x=10200) | `MC_<pin>_<signal>[__<function>]` |
| **net-class directive** | ON the wire at the pin's electrical point (left x=6900, right x=9500) | parameter set named `MC_<interface>`, hidden `ClassName=<pcb class>`, orientation 1, colour 255. Class is per interface (`MC_UART7`, `MC_SPI5`); ETH uses `ETH0` plus `ETH0_MD` on MDIO/MDC; TIM5 pins use `MC_PWM1` |
| **test pad** | `TESTPOINT_PAD_MFG` with its pin end on the wire: left x=6400, right x=9700 | plus an `MFG_TP` directive (colour 16711680, `ClassName=MFG_TP`) 100 mil inboard of the pad (left 6300, right 9600) |
| **differential pair directive** | on each wire of the pair, 500 mil inboard of the pin (x=6400 on the left) | `DIFFPAIR_USB`: `DifferentialPair=True`, `DifferentialPairClassName=USB`, orientation 0 |
| **harness connector** | its entries sit exactly on the wire ends: left clusters have the connector's RIGHT edge at x=3700 (entries `Side` 1), right clusters have the LEFT edge at x=12000 (entries `Side` 0). Location is the top-left corner, one row above the first entry; entry rows are `top - DistanceFromTop`; XSize 300-600 by name length; YSize = (entries + 1) x 100 | `HarnessType` = a type from the project's `<sheet>.Harness` file (`UART=TX,RX`, `UART_FC=TX,RX,RTS,CTS`, `SPI=SCK,MOSI,MISO`, `I2C`, `SMBUS`, `SDMMC=D3..CK`, `USB=DM,DP`, `CANTXRX`, `SWD`, `ETH_RMII`, `PWMx4`); colours 13213327 / 16511725, entries 7354880 / 8454143, line width 1. Entry names are the harness signal names, in pin order top to bottom |
| **signal harness** | a 3-vertex line (width 2, colour 15187117) from the connector's primary connection (opposite edge, `top - PrimaryConnectionPosition`, normally the cluster's middle row) to the port's near end | the type label (`SPI`, `UART_FC`) rides on it and is a child of the connector |
| **harness port** | at the harness end: left x=1800 (harness meets `Location.X + Width`); right ports right-aligned to x=14000 (`Location.X = 14000 - Width`, harness meets `Location.X`) | Name = harness (`UART7`, `SPI6`), same `HarnessType` as the connector. Height 100 mil, colours 128 / 8454143 / 128, font 1. Port names match sheet entries on the parent sheet |

So a cluster that moves one row moves **seven kinds of object** (wire,
label, class directive, pad + MFG_TP mark, harness connector, signal harness,
port), and a new cluster needs all of them, not just wires.
`move_schematic_objects`, `set_parameter_sets`, `set_schematic_ports` and
`set_harness_connectors` exist for exactly this; the `paramset`, `harness`
and `signalharness` kinds of `get_schematic_objects` show what is there.
**Look at `render_schematic` before declaring a sheet done** - the harness
family was invisible to every geometric check here until the picture showed
it. A blanket (dashed rectangle) with a directive on its border applies that
directive to every net inside it; moving wires under a blanket changes what
it marks. Before
re-routing, dump `paramset`, `port`, `component` and `pin` and record which
directive and pad sits on which pin row - after the wires move that
association is gone from the sheet and only the old dump has it.

Single-signal lines (one timer channel, an ADC input) get no harness: the
parent sheet exposes them as PLAIN ports named `<PIN>_<SIGNAL>`
(`PC6_TIM3_CH1`, `PA0_ADC1_INP16`), so the comms sheet carries a plain port
of that name at the wire end (right side: `Location.X` = 12000, the wire
end; width ~80 mil per character). A four-channel timer is a `PWMx4`
harness with a `MC_PWMn` class. Probe pads (`TESTPOINT_PAD`, 100-mil pin)
do not fit on 100-mil rows, so the house puts them in the free band above a
cluster with a vertical drop per signal, leftmost pad on the top row so
drops never cross; MFG pads (50-mil pin) sit directly on the wire. Notes
(`eNote` text frames) name buses too - retitle them when a bus is renamed
and grow the frame when a bus is added.

Which bus is which is in the OTHER sheets, not in your head: the FC comms
sheet's labels named the MC<->FC link, the parent's ports named the plain
single-signal ports, and neither had to be guessed.

Things that stay the user's: which PCB class a NEW interface gets (a name is
a routing rule), a harness type that is not in the `.Harness` file yet
(`QSPI` was placed and flagged), the parent sheet's sheet entries, and
test pads whose original net no longer exists on the sheet - those get a
proposal backed by a sibling sheet, stated as such.

## DelphiScript hazards that bit repeatedly

- **Shared scratch variables.** The sandbox convention (`I1..I5`, `S1..S5`) is
  the single most repeated bug here - THREE times in one session, each time
  silent. Reusing `I1` as a loop counter after storing a coordinate in it gave
  an anchor of `24 - 50 = -26` and flung text off-sheet; reusing `I4`/`I5` for
  bounding-box edges overwrote result counters and reported `1840` pins
  shorted. Every occurrence produced plausible-looking wrong output rather
  than an error. Stash a result before any loop that might reuse its variable
  (`SavedCounts`), and an MCP port should use properly scoped locals.
- `try/except` does **not** catch runtime errors; a bad call leaves the script
  paused in the debugger and every later run silently does nothing. Recover
  with `X2.EXE -REditScript:Stop` (see [`unwedge.py`](unwedge.py)), and verify
  recovery by running a probe rather than assuming.
- Nil is not guarded for you. `GetSchDocumentByPath` returns nil for a document
  that belongs to the project but is not open; the next call kills the script.
- Do not predict a rotated symbol's pin coordinates. Place, dump `pin_map.txt`,
  then route from the measured values.
