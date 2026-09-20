# DelphiScript API notes

Verified against **AD21** from the sandbox. Every value here was read out of a
running Altium, not copied from a manual. Check anything you add the same way.

Altium's failure modes make guessing expensive: an identifier that does not
exist costs a wedged executor and, sometimes, a human pressing Ctrl+F3. The
point of this file is that nobody should have to guess twice.

## Probe before you mutate

The single most useful rule here, learned by nearly applying a half-finished
rename to a production sheet:

| Failure | What ran | Recovery |
|---|---|---|
| **Syntax** error (malformed code) | **nothing** - the document is untouched | fix and retry, free |
| **Undeclared identifier** | everything up to that statement, **and it is applied** | check for partial edits first |
| **Runtime** error (bad member, bad cast, nil receiver) | same - partial edits stand | check before retrying |

**An undeclared identifier is a RUNTIME failure, not a compile failure.**
Measured: a script referencing `DM_ErrorCount` logged eight steps, compiled
and ran, then died at that statement. Only genuinely malformed code
(`Expression expected but " found`) aborts before anything executes. So a bad
identifier will happily perform half of your mutation first.

That makes the rule stronger, not weaker: put every uncertain identifier in a
**throwaway probe script** before it goes anywhere near a script that modifies
a document.

Two habits that make probing cheap:

- **Order probes least-risky first.** A failure costs you everything after it,
  nothing before it.
- **`SandboxLog` the VALUES, not just the step names.** The log is flushed on
  every call and survives a wedge; `ResultText` is written at the very end and
  is lost. A probe that logs only "reading component count" tells you nothing
  when the next line dies; one that logs `components=114` keeps the answer.

The corollary from the main README still holds - put risky reads at the **end**
of an experiment - but it is weaker than probing separately, because a risky
read inside a loop is not at the end of anything.

## "Undeclared identifier: X" usually means the RECEIVER IS NIL

This one wastes real time. `DocumentName` is a valid member and is used all
over this codebase, yet:

    Obj5 := SchServer.GetCurrentSchDocument;   // returns nil - a PCB is focused
    S2  := Obj5.DocumentName;                  // Error: Undeclared identifier: DocumentName

Late-bound IDispatch cannot resolve a member on nothing, so a nil object is
reported as a missing *member*. Before concluding an API does not exist, check
that the object you called it on is non-nil. `GetCurrentSchDocument` returns
nil whenever the focused document is not a schematic, which happens the moment
the user clicks another tab.

## Object IDs

`ObjectId` on any `ISch_GraphicalObject`. These are what iterators give you.

| Constant | Value | | Constant | Value |
|---|---|---|---|---|
| `eNote` | 2 | | `eLabel` | 22 |
| `eRectangle` | 4 | | `eNetLabel` | 24 |
| `eLine` | 5 | | `eDesignator` | 25 |
| `eBusEntry` | 7 | | `eSchComponent` | 26 |
| `eArc` | 8 | | `eParameter` | 27 |
| `eImage` | 11 | | `eParameterSet` | 28 |
| `eTextFrame` | 13 | | `eNoERC` | 35 |
| `eEllipse` | 15 | | `ePin` | 37 |
| `eJunction` | 16 | | `ePort` | 38 |
| `ePolygon` | 17 | | `ePowerObject` | 39 |
| `ePolyline` | 18 | | `eSheetEntry` | 40 |
| `eWire` | 19 | | `eSheetSymbol` | 41 |
| `eBus` | 20 | | `eTemplate` | 42 |
| | | | `eCrossSheetConnector` | 48 |
| | | | `eHarnessConnector` | 53 |

## Document kinds

`Document.ObjectId`: **32** = schematic sheet, **33** = schematic library.

`SchServer.GetCurrentSchDocument` returns whichever is focused, so a script
that edits a sheet must check this. Registering objects into a library because
the user happened to have one open is the accident this guard prevents:

```pascal
Obj1 := SchServer.GetCurrentSchDocument;
if Obj1.ObjectId <> 32 then
    ResultText := 'ABORT: not a schematic, ObjectId=' + IntToStr(Obj1.ObjectId)
else
begin
    // safe to modify
end;
```

## Modify / robot constants

| Constant | Value |
|---|---|
| `c_BroadCast` | 0 |
| `c_NoEventData` | 0 |
| `SCHM_PrimitiveRegistration` | 1 |
| `SCHM_BeginModify` | 2 |
| `SCHM_EndModify` | 3 |
| `eCreate_Default` | 0 |

## Traps - things that look right and are not

- **`eSchDoc` is not a script identifier.** Referencing it is a compile error
  ("Undeclared identifier: eSchDoc"). Use the literal `32`. The main README
  documents the *value* 32 = eSchDoc, which reads as though the name were
  available. It is not.
- **`I_ObjectAddress` returns a Dispatch, not an integer.** `IntToStr()` on it
  raises `EVariantTypeCastError: Could not convert variant of type (Dispatch)
  into type (Boolean)`. Pass it to `SendMessage`; never print it.
- **`ISch_Component.GetState_PinCount` does not exist.** It kills the script at
  runtime. Count pins by iterating and filtering `ObjectId = ePin`.
- **`IProject.DM_ErrorCount` does not exist**, and neither does
  `Client.GetDocumentCount`. There is no DM interface to ERC violations;
  `check_schematic` derives structural faults from the compiled netlist
  instead.
- **`SchLibIterator_Destroy` does not exist.** `SchLibIterator_Create` pairs
  with plain `SchIterator_Destroy`. Calling the symmetric-looking name kills
  the script in the `finally` block, after the useful work has completed.
- **`GetWorkspace.DM_CreateNewDocument('SCH')` fails** with "Could not convert
  variant of type (OleStr) into type (Double)" on AD21, despite being recorded
  as verified in `DBLIB_FINDINGS.md`. To get a scratch sheet, copy an existing
  `.SchDoc` to a new path and `Client.OpenDocument('SCH', path)` it - that
  works, and closing with `Modified := False` then `Client.CloseDocument`
  leaves nothing behind.
- **A replicated component's pins keep LIBRARY-RELATIVE coordinates.** For a
  normally placed part, `Pin.Location` is absolute (component location plus
  the library offset). For one created with `Component.Replicate`, pins report
  the raw library offset - measured: component at (20000000, 50000000), its
  pin 8 at (2000000, 0). Re-assigning `Component.Location` after registration
  does not fix it. Any wire routed to those coordinates misses.
- **The document iterator recurses into components.** A single pass sees
  component pins, parameters and designators as well as sheet-level objects.
  `Obj.Container.ObjectId` identifies the owner - 32 means the sheet itself
  owns it. (`SHEET_DUPLICATION.md` records the same trap.)
- **A pin's connection point is not `Pin.Location`.** It is `PinLength` further
  along the pin: orientation 2 (facing left) → `Location.X - PinLength`;
  orientation 0 (facing right) → `Location.X + PinLength`. See
  `SCHEMATIC_CONVENTIONS.md`.
- **A net label names nothing unless its `Location` lies on a wire segment** -
  including mid-segment, not only at a vertex. Half the labels on a real sheet
  turned out to be floating annotation.
- **A multi-part component stores every part's pins.** Each placement is a
  separate `ISch_Component` carrying all pins; `Pin.OwnerPartId` says which
  part a pin belongs to and `Component.CurrentPartID` says which part that
  placement displays. Coordinates of pins from other parts are meaningless.

### The MCP layer turns an all-digit string argument into an int

`mcp` 1.5.0 (`FuncMetadata.pre_parse_json`) tries `json.loads` on every string
argument and keeps the result unless it is itself a string. So a tool
declared `body_line: str` receives the hex colour `"800000"` as the **integer
800000**, while `"FFFFFF"` (not JSON) and `"000000"` (a leading zero is
invalid JSON) arrive intact. Pydantic then rejects the int with
`Input should be a valid string`, and the caller sees nothing but a validation
error. Any parameter whose legitimate values can be all digits (hex colours,
pin numbers, designator suffixes) must be typed `Union[str, int]` and
normalised, reading the int's decimal digits back verbatim and zero-padding
(`_hex6` in main.py). Measured 2026-09-19 on `apply_symbol_style`.

### eCreate_GlobalCopy clones the last-edited object's VERTICES

`SchObjectFactory(eWire, eCreate_GlobalCopy)` starts from Altium's "global"
wire, which carries the vertex list of the wire the user last edited. With a
3-vertex template every wire you then build by InsertVertex/SetState_Vertex
keeps the leftover vertex, and 56 new wires fanned diagonals to one point
(measured 2026-09-19 on MC_STM32H753XIHx_Comms). There is NO `DeleteVertex`
(Undeclared identifier). Use `eCreate_Default` for wires: 0 vertices, same
LineWidth (1) and Color (8388608). Probe results: GlobalCopy VerticesCount
was 1 on a later probe, so the template also changes as the user works.

### `List1` is declared, not created

The sandbox declares `List1: TStringList` and nothing else. `List1.Add(...)`
on it dies with an unreadable "Error" box after the previous `SandboxLog`.
Start every script that uses it with `List1 := TStringList.Create;`.

### `MoveByXY` moves a container WITH its children; `Location :=` does not

Verified on a parameter set (its hidden `ClassName` parameter kept its
offset) and on a test-pad component (its pin's electrical point followed).
Use `Obj.MoveByXY(dx, dy)` inside a Begin/EndModify pair for anything that
owns children. Wrapping several moves in one PreProcess/PostProcess gives one
undo step; 49 moves in one script took under two seconds.

### A directive is a container: `eParameterSet` + hidden `eParameter` children

What the GUI calls a Net Class / Differential Pair / generic directive is one
object kind, `eParameterSet` (ObjectId 28). Its `Name` is the visible label;
the meaning is in HIDDEN child parameters:

| Directive | Children |
|---|---|
| net class | `ClassName=<pcb class>` |
| differential pair | `DifferentialPair=True`, `DifferentialPairClassName=<pair>` |

Read them with a child iterator filtered on `eParameter` (`IsHidden` is
readable). Create one with `SchObjectFactory(eParameterSet, eCreate_Default)`,
set `Name`, `Location`, `Orientation`, `Color`, then
`SchObjectFactory(eParameter, eCreate_Default)` for each child with `Name`,
`Text`, `IsHidden := True`, `Set.AddSchObject(Child)`, and finally register
the set on the sheet. Verified: the read-back shows the child, hidden. The
parameter set marks a net only when its Location lies ON a wire.

### Ports: all of these members read and write

`Name`, `HarnessType` (string), `Width`, `Height`, `Style`, `Alignment`,
`Color`, `AreaColor`, `TextColor`, `FontID`, `Location`. Create with
`SchObjectFactory(ePort, eCreate_Default)` and register. **`IOType` is the
exception**: writing 3 (bidirectional) reads back 0, and ports whose file
record says `IOType=3` also read 0 through the API, so treat that member as
unverified rather than broken - check the rendered port arrows instead.

### Harness objects live in a second stream, and the API sees them

`eHarnessConnector` = 53, `eHarnessEntry` = 54, `eSignalHarness` = 56,
`eBlanket` = 61 (`eCompileMask` also exists). In the .SchDoc these are
RECORD 215 / 216 / 218 / 225 in the **"Additional"** OLE stream, not in
"FileHeader" - an offline parser that reads only FileHeader reports a sheet
with no harnesses, which is exactly what happened here before the render
showed them. `pinout/schdoc.py` now reads both streams.

Connector geometry (all verified live): `Location` is the TOP-LEFT corner;
`XSize` / `YSize`; each entry's `Location` is the electrical point at
`Location.Y - DistanceFromTop` on the right edge (`Side` = 1) or the left
edge (`Side` = 0); `PrimaryConnectionPosition` is how far below the top the
signal harness attaches on the opposite edge; `HarnessType` is a string and
setting it also re-labels the connector. A signal harness is a polyline:
`VerticesCount`, `Vertex[i]`, `SetState_Vertex`, `LineWidth`, `Color`.

What does NOT exist through the API: `HarnessConnectorSide` (the file key
that flips a connector so its entries are on the left) and `Corner` on a
blanket (`BoundingRectangle.Left/Top/Right/Bottom` works). So a right-side
connector cannot be built from `eCreate_Default`; **`Replicate` an existing
one of the right side and type**, `MoveByXY` it (entries and type label
follow), register it, then edit `HarnessType`, `YSize`,
`PrimaryConnectionPosition`, entry `Name`s, remove entries with
`Connector.RemoveSchObject(entry)`, and add entries with
`SchObjectFactory(eHarnessEntry, eCreate_Default)` + `Connector.AddSchObject`.
An entry added that way reports `Location` 0,0 until it is placed
explicitly - see the fix in `set_harness_connectors`.

### `ResultText` dies with the script

If any statement after `ResultText := ...` raises, the tool returns no
result at all - the value is only collected at a clean end. Two probes lost
their whole payload that way. Put uncertain members in their own script, or
log the values with `SandboxLog` (flushed immediately) instead.

### Address the sheet by PATH, not by focus

`SchServer.GetCurrentSchDocument` is nil whenever the focused document is
not a schematic: the user clicked another tab, or a compile focused the
sandbox script project. Every sheet tool now takes `document=` and uses
`SchServer.GetSchDocumentByPath('D:\\...\\Sheet.SchDoc')`, which needs the
sheet OPEN but not focused and never steals the user's view. Edits on a
non-focused sheet apply and render normally.

### `DM_FocusedProject` is the sandbox script project after any script

After `run_altium_script` (and after a compile) the focused project is
`Sandbox.PrjScr`; compiling it yields "no flattened document". The
netlist / doc-state / save scripts now pick the project that owns the
focused schematic and never a `.PrjScr` while a `.PrjPcb` is open.

### `REPLACEALL` is a CONSTANT, not a function

The sandbox defines `REPLACEALL = 1` as the `StringReplace` flag. Calling
`REPLACEALL(s, a, b)` is the compile error "; expected", so nothing runs.
Use `Copy` / `Pos` string surgery, or `StringReplace(s, a, b, REPLACEALL)`.

### Notes (`eNote`) resize through `Location`; junctions draw themselves

A note's `Text` is read/write with real line breaks (CR LF), `Corner` reads
back, and assigning `Location` moves only that corner - so growing a text
frame downward is `Location := Point(X, Y - 5000000)`. `MoveByXY` moves the
whole frame. The X9 sheet has ZERO `eJunction` objects although every
pull-up drop shows a dot: Altium draws T-junctions itself, so a drop wire
ending on another wire needs no junction object.

## Members verified working

`SchServer`: `GetCurrentSchDocument`, `SchObjectFactory(kind, eCreate_Default)`,
`RobotManager`, `ProcessControl`.

`ISch_Document`: `SchIterator_Create` / `SchIterator_Destroy`,
`RegisterSchObjectInContainer`, `RemoveSchObject`, `DocumentName`, `ObjectId`.

Iterator: `FirstSchObject`, `NextSchObject`.

`ISch_GraphicalObject`: `ObjectId`, `Container`, `Location.X` / `.Y`,
`OwnerPartId`, `I_ObjectAddress`.

`ISch_Pin`: `Designator`, `Name`, `Orientation`, `PinLength`.
`ISch_NetLabel` / `ISch_PowerObject`: `Text`, `Orientation`, `Justification`,
`FontID`. `ISch_Port`: `Name`.
`ISch_Component`: `Designator.Text`, `LibReference`, `CurrentPartID`.
`ISch_Wire`: `VerticesCount`, `Vertex[i]` - **1-based**.

`Point(x, y)` is available for building a `TLocation`.

### The compiled netlist (all verified)

This is Altium's own connectivity, correct for mirrored and multi-part
components where a geometric walk is not:

    Project := GetWorkspace.DM_FocusedProject;
    Project.DM_Compile;                     // no return value to check
    Flat := Project.DM_DocumentFlattened;   // nil if the compile produced nothing
    Flat.DM_NetCount / DM_Nets(i)           // INet
    Net.DM_NetName / DM_PinCount / DM_Pins(i)
    Pin.DM_PinNumber / DM_LogicalPartDesignator / DM_FlattenedNetName
    Flat.DM_ComponentCount / DM_Components(i)
    Comp.DM_PhysicalDesignator / DM_LogicalDesignator / DM_PinCount
    Comp.DM_OwnerDocumentName

Measured on a real project: 855 nets across the flattened document, and on a
smaller one 97 nets / 114 components of which 23 were unannotated.

### A path is a STRING to Altium - spell it exactly one way

`Client.IsDocumentOpen(path)` and `Client.GetDocumentByPath(path)` compare the
path text. A forward-slash spelling of a document that is open under
backslashes is "not open", so `Client.OpenDocument` loads a **second instance
of the same file from disk**. Measured: `scratch_fake.SchLib` open twice at
once - `C:\Users\...` holding two freshly created symbols, `C:/Users/...`
holding only the on-disk `Component_1` - and every API reporting success
throughout. Symptoms: "symbol not found" right after a successful create, a
library inventory that looks stale, edits that vanish.

Always pass the canonical Windows path (absolute, backslashes) exactly as
Altium reports `DocumentName`. The Python side does this in `_canon()` for
every path-taking tool. `SchServer.GetSchDocumentByPath(path)` (verified) is
how to reach an open library's `ISch_Document` by path without focusing it.

### Creating a library (verified on AD Develop)

```pascal
Doc := Client.OpenNewDocument('SCHLIB', 'name.SchLib', 'description', False);
Client.ShowDocument(Doc);
Doc.DoSafeChangeFileNameAndSave('C:\path\name.SchLib', 'SCHLIB');  // no dialog
```

Dead ends, so they are not retried: `GetWorkspace.DM_CreateNewDocument('SCH')`
fails with an OleStr/Double conversion error; the process
`WorkspaceManager:OpenObject` with `ObjectKind=Document|DocumentKind=SCHLIB`
opens a "Choose Document to Open" **file chooser** - it means open-existing,
not new. A new library arrives with Altium's default empty `Component_1`.

Adding a component to a library: `SchObjectFactory(eSchComponent,
eCreate_Default)`, `AddSchObject` each pin, then `Lib.AddSchComponent(Comp)`.
Verified iterable immediately afterwards on AD Develop (the
`SCHM_PrimitiveRegistration` message with a nil first argument, as the
shipped code sends, is harmless but was not what made it appear).

### Saving a document

`Client.GetDocumentByPath(path)` returns an `IServerDocument`:

    Doc.Modified            // read and WRITABLE - setting False then
                            // Client.CloseDocument discards changes cleanly
    Doc.DoFileSave('')      // verified: Modified goes True -> False and the
                            // file on disk grows / its mtime updates

Nothing in this repository saved a document before `save_documents`; every
`SaveToFile` in the .pas units writes a TStringList to a text file, not an
Altium document. Tools that edit a sheet therefore leave it dirty.

### Annotation and multi-part components

`ISch_Component` exposes `CurrentPartID` (which part this placement shows)
and `PartCount` (how many parts the symbol has). A multi-part symbol is
placed as SEPARATE components, one per part, and **their `UniqueId`s differ**
- measured on a 3-part MCU: `AWIQLVOH`, `ZRYKKRQV`, `WTVLSXNI`. Nothing in
the object model links them into one physical component, so deciding which
placements share a designator is a judgement Altium's own annotator makes
from the user's settings. `annotate_schematic` therefore assigns designators
only where `PartCount = 1`, and reports the rest.

`Component.Designator.Text` is writable and takes effect immediately.

### Creating wires

Wires use `eCreate_Default` (a clean object with zero vertices) and need their
vertices inserted one at a time (1-based). Do NOT use `eCreate_GlobalCopy`: it
clones the last-edited wire's vertex list, and there is no `DeleteVertex` to
clean that up - see the trap below.

```pascal
Obj := SchServer.SchObjectFactory(eWire, eCreate_Default);
Obj.Location := Point(x1, y1);
Obj.InsertVertex := 1;  Obj.SetState_Vertex(1, Point(x1, y1));
Obj.InsertVertex := 2;  Obj.SetState_Vertex(2, Point(x2, y2));
Doc.RegisterSchObjectInContainer(Obj);
SchServer.RobotManager.SendMessage(Doc.I_ObjectAddress, c_BroadCast,
                                   SCHM_PrimitiveRegistration, Obj.I_ObjectAddress);
```

### Placing a library component

`Component.Replicate` onto the target sheet. Both obvious alternatives are
dead ends that return without error and place nothing - see
[`DBLIB_FINDINGS.md`](DBLIB_FINDINGS.md):

- `IntegratedLibraryManager.PlaceLibraryComponent(...)`
- process `Sch:PlaceIntegratedComponentFromDB`

`ISch_Component.DatabaseLibraryName` / `.DatabaseTableName` are **not
writable** - assigning to them kills the script. `.DesignItemID` is writable.

## Modifying a document

Wrap the whole change so it is **one** undo step, and pair every property write
with begin/end modify or the GUI keeps showing stale content:

```pascal
SchServer.ProcessControl.PreProcess(Doc, '');
// per object:
SchServer.RobotManager.SendMessage(Obj.I_ObjectAddress, c_BroadCast,
                                   SCHM_BeginModify, c_NoEventData);
Obj.Text := 'NEW';
SchServer.RobotManager.SendMessage(Obj.I_ObjectAddress, c_BroadCast,
                                   SCHM_EndModify, c_NoEventData);
SchServer.ProcessControl.PostProcess(Doc, '');
```

Creating:

```pascal
Obj := SchServer.SchObjectFactory(eNetLabel, eCreate_Default);
Obj.Text := 'NET';
Obj.Location := Point(114000000, 51000000);
Doc.RegisterSchObjectInContainer(Obj);
SchServer.RobotManager.SendMessage(Doc.I_ObjectAddress, c_BroadCast,
                                   SCHM_PrimitiveRegistration, Obj.I_ObjectAddress);
```

Removing - **never while iterating**; the iterator does not survive it. Find
one, destroy the iterator, remove, repeat:

```pascal
Doc.RemoveSchObject(Obj);
SchServer.RobotManager.SendMessage(Doc.I_ObjectAddress, c_BroadCast,
                                   SCHM_PrimitiveRegistration, Obj.I_ObjectAddress);
```

## Units

Schematic coordinates are internal units: **1 mil = 10000**. A 100-mil grid
step is 1000000. Pin lengths of 2000000 are 200 mil.
