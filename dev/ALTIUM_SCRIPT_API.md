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

Wires use `eCreate_GlobalCopy`, not `eCreate_Default`, and need their vertices
inserted one at a time (1-based):

```pascal
Obj := SchServer.SchObjectFactory(eWire, eCreate_GlobalCopy);
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
