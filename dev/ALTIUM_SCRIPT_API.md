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
| **Compile** error (undeclared identifier, syntax) | **nothing** - the document is untouched | fix and retry, free |
| **Runtime** error (bad member, bad cast) | everything up to the failing statement, **and it is applied** | check for partial edits first |

So: put every uncertain identifier in a **throwaway probe script** before it
goes anywhere near a script that modifies a document. A compile failure costs
one round trip; a runtime failure halfway through a loop over 33 net labels
leaves the sheet in a state nobody planned.

The corollary from the main README still holds - put risky reads at the **end**
of an experiment - but it is weaker than probing separately, because a risky
read inside a loop is not at the end of anything.

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
