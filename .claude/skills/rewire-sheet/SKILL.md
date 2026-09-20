---
name: rewire-sheet
description: Re-route or extend an existing Altium schematic sheet in the Verge house style (MCU comms sheets and the like) - the complete checklist of objects a cluster carries, the order of work, and the verification that catches what geometry checks miss. Use before touching wires on a sheet that already has directives, test pads, harness connectors or ports.
---

# Rewiring a sheet without leaving the rest behind

A cluster on a house sheet is seven kinds of object, not two. Moving wires and
net labels and stopping there leaves every other kind on the neighbour's net
with no warning from Altium. This happened on the X9 MC comms sheet on
2026-09-19; the tools and notes below came out of fixing it.

## 0. Ground rules

- Ask before touching Altium if the user is working in it; batch your writes;
  say when you are done. Never save unless asked (`save_documents` previews).
- Every writer previews by default. Preview, read the warnings, then apply.
- Probe uncertain API members in a throwaway `run_altium_script` FIRST, with
  `SandboxLog` after each risky statement. An undeclared identifier is a
  runtime failure: everything before it is applied, and `ResultText` is lost.
- Read `dev/SCHEMATIC_CONVENTIONS.md` section 13 and `dev/ALTIUM_SCRIPT_API.md`
  before writing DelphiScript.

## 1. Inventory BEFORE moving anything

```
get_schematic_objects(kinds=["pin","wire","netlabel","paramset","component",
                             "port","harness","signalharness","noerc"])
render_schematic()
```

Record, per pin row: the wire, the label, the net-class directive (paramset
at the pin's electrical point, hidden `ClassName=`), any test pad on the wire
(`TESTPOINT_PAD*` component whose pin electrical point lies on the wire) and
its `MFG_TP` directive, the harness entry the wire ends on, the connector,
its signal harness and its port. Once the wires move, that association is
gone from the sheet and only your dump has it. Blankets (dashed rectangles)
and the directive on their border mark every net inside them - note what is
inside now.

Look at the render. The harness family was invisible to every geometric
check until the picture showed it.

## 2. Move in this order

1. Wires (`draw_schematic_wires` / `delete_schematic_objects`) and labels
   (`set_net_labels`).
2. Directives, pads and their marks: `move_schematic_objects` with `by`
   deltas per cluster - the preview flags anything landing on no wire.
   Rename or create directives with `set_parameter_sets` (style 1 = the house
   circle; style 0 prints the name and collides with pin text).
3. Harness connectors and signal harnesses: `set_harness_connectors` - move
   existing assemblies with the same delta as their wires; create new ones by
   replicating an assembly of the same type AND side, then edit type, size,
   primary connection position and entries. The API cannot flip a connector.
4. Ports: `set_schematic_ports` / `move_schematic_objects`. A port must sit
   at its harness end (left: harness meets `x + width`; right: meets `x`).
5. Blankets: `move_schematic_objects` kind is not available for blankets yet -
   use `run_altium_script` with `MoveByXY` and check `BoundingRectangle`; put
   the border directive back on an edge row that carries no wire.

## 3. Verify

- `check_net_labels`: 0 floating.
- Re-dump `paramset`, `harness`, `component`+`pin`, `port` and confirm every
  directive / pad pin / entry lies on a wire end of the intended row, every
  harness end meets a connector and a port.
- `render_schematic` and READ it: text overlaps, missing entries (an entry
  added by factory reports location 0,0 until placed), ports off their
  harness, stray dashed blankets.

## 4. What stays the user's call - flag, do not guess

New PCB class names (a name is a routing rule), harness types missing from
the project's `.Harness` file, parent-sheet sheet entries, test pads whose
original net left the sheet, net-name placeholders, designators.
