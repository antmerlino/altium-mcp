// Isolated DelphiScript sandbox used by the run_altium_script MCP tool.
//
// This file is a TEMPLATE: run_altium_script never edits it. For every run the
// tool writes a copy with a unique file name under C:\Users\Public\altium_mcp\runs,
// fills in the markers below and starts it with ScriptingSystem:RunScriptFile.
// A unique file per run means a broken script left open in Altium's script
// editor or paused in its debugger can never be re-run by accident.
//
// SandboxLog() flushes to disk on every call, so when a script dies the log
// still shows the last step that completed - the statement after it is the
// culprit.
//
// The tool also makes every while and repeat loop of the script call
// SandboxTick(). A script runs on Altium's UI thread and nothing can stop it
// from outside, so a loop that never ends would freeze Altium until X2.EXE is
// killed. Past the deadline (%GUARD_SECONDS% s) every guarded loop exits.

const
    REPLACEALL = 1;

var
    LogLines : TStringList;
    LogPath  : String;
    OutPath  : String;
    // Scratch variables: DelphiScript has no inline declarations, so scripts
    // either reuse these or start with their own "var" block (the tool moves
    // that block into Run's declarations).
    S1, S2, S3, S4, S5, S6, S7, S8, S9 : String;
    I1, I2, I3, I4, I5, I6, I7, I8, I9 : Integer;
    B1, B2, B3 : Integer;
    F1, F2, F3, F4 : Double;
    Obj1, Obj2, Obj3, Obj4, Obj5, Obj6, Obj7, Obj8, Obj9, Obj10, Obj11, Obj12 : IDispatch;
    List1, List2, List3 : TStringList;
    IntMan     : IIntegratedLibraryManager;
    DbDoc      : IDatabaseLibDocument;
    // Loop guard state, see SandboxTick
    SbxGuardUntil   : Double;
    SbxGuardTripped : Boolean;
    // Undo groups the script has opened and not yet closed, see SbxPreProcess
    SbxUndoDocs  : TList;
    SbxUndoNames : TStringList;
    SbxUndoPath  : String;

procedure SandboxLog(Msg: String);
begin
    LogLines.Add(Msg);
    LogLines.SaveToFile(LogPath);
end;

// Called on every pass of every while and repeat loop in the script, with the
// loop's line in the submitted script. False once the deadline has passed,
// which ends that loop and every guarded loop after it, so the script runs to
// its end and Altium is released. Code after the loops still runs (so a
// PostProcess after a loop is not skipped); SaveDoc refuses to save.
function SandboxTick(Line : Integer) : Boolean;
begin
    if (not SbxGuardTripped) and (Now > SbxGuardUntil) then
    begin
        SbxGuardTripped := True;
        SandboxLog('LOOP GUARD: the loop at line ' + IntToStr(Line) +
                   ' was still running after %GUARD_SECONDS% s; every loop was cut short from here on');
    end;
    Result := not SbxGuardTripped;
end;

// The tool rewrites the script's SchServer.ProcessControl.PreProcess/PostProcess
// and PCBServer.PreProcess/PostProcess calls into these. PreProcess opens an
// undo group on a document and PostProcess closes it; a group left open (the
// script failed between the two) makes every later edit in that document,
// including the user's, impossible to undo until Altium restarts. So the open
// groups are kept here, closed by SbxCloseUndo when the script ends, and also
// written to a file, so the tool can close them even when the script dies in
// Altium's debugger and never reaches its end.
// The list is a fallback for a script that dies in the debugger; failing to
// write it must never fail the script. A server started before this template
// existed leaves the path token unfilled.
procedure SbxSaveUndo;
begin
    if (SbxUndoPath = '') or (Copy(SbxUndoPath, 1, 1) = '%') then Exit;
    try
        SbxUndoNames.SaveToFile(SbxUndoPath);
    except
    end;
end;

procedure SbxPreProcess(Doc : IDispatch; Param : String);
begin
    SchServer.ProcessControl.PreProcess(Doc, Param);
    SbxUndoDocs.Add(Doc);
    SbxUndoNames.Add('SCH|' + Doc.DocumentName);
    SbxSaveUndo;
end;

procedure SbxPostProcess(Doc : IDispatch; Param : String);
begin
    SchServer.ProcessControl.PostProcess(Doc, Param);
    if SbxUndoNames.Count > 0 then
    begin
        SbxUndoDocs.Delete(SbxUndoDocs.Count - 1);
        SbxUndoNames.Delete(SbxUndoNames.Count - 1);
        SbxSaveUndo;
    end;
end;

procedure SbxPcbPreProcess;
begin
    PCBServer.PreProcess;
    SbxUndoDocs.Add(nil);
    SbxUndoNames.Add('PCB|');
    SbxSaveUndo;
end;

procedure SbxPcbPostProcess;
begin
    PCBServer.PostProcess;
    if SbxUndoNames.Count > 0 then
    begin
        SbxUndoDocs.Delete(SbxUndoDocs.Count - 1);
        SbxUndoNames.Delete(SbxUndoNames.Count - 1);
        SbxSaveUndo;
    end;
end;

// Close, newest first, every undo group the script left open
procedure SbxCloseUndo;
var
    I : Integer;
begin
    for I := SbxUndoNames.Count - 1 downto 0 do
    begin
        try
            if Copy(SbxUndoNames[I], 1, 4) = 'PCB|' then
                PCBServer.PostProcess
            else
                SchServer.ProcessControl.PostProcess(SbxUndoDocs[I], '');
            SandboxLog('UNDO GROUP LEFT OPEN: closed it on ' + SbxUndoNames[I]);
        except
            SandboxLog('UNDO GROUP LEFT OPEN: could not close it on ' + SbxUndoNames[I]);
        end;
    end;
    SbxUndoDocs.Clear;
    SbxUndoNames.Clear;
    SbxSaveUndo;
end;

// Millimetres to Altium internal coordinate units (10000 per mil).
function MM(V : Double) : Integer;
begin
    Result := Round(V * 393700.787);
end;

// The ISch_Document for Path, opening the file first if it is not open.
// SchServer.GetSchDocumentByPath alone returns nil for a closed document, and
// calling anything on nil stops the script.
function OpenSchDoc(Path : String) : IDispatch;
var
    D : IDispatch;
begin
    Result := SchServer.GetSchDocumentByPath(Path);
    if Result = nil then
    begin
        D := Client.OpenDocument('SCH', Path);
        if D <> nil then Client.ShowDocument(D);
        Result := SchServer.GetSchDocumentByPath(Path);
    end;
    if Result = nil then SandboxLog('OpenSchDoc: could not open ' + Path);
end;

// Mark an open document modified and save it. Not after the loop guard has
// cut the script short: the document then holds a half-finished edit.
procedure SaveDoc(Path : String);
var
    D : IDispatch;
begin
    D := Client.GetDocumentByPath(Path);
    if SbxGuardTripped then
        SandboxLog('SaveDoc: not saved after the loop guard stopped the script ' + Path)
    else if D = nil then
        SandboxLog('SaveDoc: not open ' + Path)
    else
    begin
        D.SetModified(True);
        D.DoFileSave('');
    end;
end;

// Data that must survive a late failure: logged with the "EMIT: " prefix (the log is flushed on every
// line); the schematic tools get the lines back as "emitted" even when the script dies later.
procedure SandboxEmit(Line: String);
begin
    SandboxLog('EMIT: ' + Line);
end;

// ---- helpers for the patterns every sheet script repeats -------------------
// All verified live on Altium Designer (X2.EXE) 2026-09-19/20.

// The sheet to work on: by full path when given (open, not necessarily
// focused), else the focused schematic. nil when neither is available.
function SbDoc(Path: String): IDispatch;
begin
    if Path = '' then Result := SchServer.GetCurrentSchDocument
    else Result := SchServer.GetSchDocumentByPath(Path);
end;

procedure SbBeginModify(O: IDispatch);
begin
    SchServer.RobotManager.SendMessage(O.I_ObjectAddress, c_BroadCast, SCHM_BeginModify, c_NoEventData);
end;

procedure SbEndModify(O: IDispatch);
begin
    SchServer.RobotManager.SendMessage(O.I_ObjectAddress, c_BroadCast, SCHM_EndModify, c_NoEventData);
end;

// Put a new (or replicated) object on the sheet.
procedure SbRegister(Doc, O: IDispatch);
begin
    Doc.RegisterSchObjectInContainer(O);
    SchServer.RobotManager.SendMessage(Doc.I_ObjectAddress, c_BroadCast, SCHM_PrimitiveRegistration, O.I_ObjectAddress);
end;

// Remove an object from the sheet. Never call this while iterating the
// sheet - find the object, destroy the iterator, then remove.
procedure SbRemove(Doc, O: IDispatch);
begin
    Doc.RemoveSchObject(O);
    SchServer.RobotManager.SendMessage(Doc.I_ObjectAddress, c_BroadCast, SCHM_PrimitiveRegistration, O.I_ObjectAddress);
end;

// MoveByXY carries children (a directive's hidden parameters, a component's
// pins, a connector's entries); assigning Location does not.
procedure SbMove(O: IDispatch; DX, DY: Integer);
begin
    SbBeginModify(O);
    O.MoveByXY(DX, DY);
    SbEndModify(O);
end;

// The identifying text of an object, by kind: Text for labels/power ports,
// Designator.Text for components, HarnessType for harness connectors, Name
// for ports / directives / entries, '' otherwise.
function SbIdent(O: IDispatch): String;
begin
    Result := '';
    if (O.ObjectId = eNetLabel) or (O.ObjectId = ePowerObject) or (O.ObjectId = eLabel) then Result := O.Text
    else if O.ObjectId = eSchComponent then Result := O.Designator.Text
    else if O.ObjectId = eHarnessConnector then Result := O.HarnessType
    else if (O.ObjectId = ePort) or (O.ObjectId = eParameterSet) or (O.ObjectId = eHarnessEntry) then Result := O.Name;
end;

// First object of kind ObjId whose identifying text is Ident (any if '') and
// whose Location is (X, Y) (either -1 to ignore). nil when nothing matches.
// The iterator is destroyed before returning, so the result is safe to
// modify or remove.
function SbFind(Doc: IDispatch; ObjId: Integer; Ident: String; X, Y: Integer): IDispatch;
var
    It, O : IDispatch;
begin
    Result := nil;
    It := Doc.SchIterator_Create;
    It.AddFilter_ObjectSet(MkSet(ObjId));
    O := It.FirstSchObject;
    while (O <> nil) and (Result = nil) do
    begin
        if ((Ident = '') or (SbIdent(O) = Ident)) and ((X < 0) or (O.Location.X = X)) and ((Y < 0) or (O.Location.Y = Y)) then
            Result := O;
        O := It.NextSchObject;
    end;
    Doc.SchIterator_Destroy(It);
end;

// How many objects of kind ObjId the sheet holds.
function SbCount(Doc: IDispatch; ObjId: Integer): Integer;
var
    It, O : IDispatch;
begin
    Result := 0;
    It := Doc.SchIterator_Create;
    It.AddFilter_ObjectSet(MkSet(ObjId));
    O := It.FirstSchObject;
    while O <> nil do
    begin
        Result := Result + 1;
        O := It.NextSchObject;
    end;
    Doc.SchIterator_Destroy(It);
end;

// Copy an object (children included), shift the copy, register it.
function SbReplicateMove(Doc, O: IDispatch; DX, DY: Integer): IDispatch;
begin
    Result := O.Replicate;
    Result.MoveByXY(DX, DY);
    SbRegister(Doc, Result);
end;

// A two-point wire. eCreate_Default, never eCreate_GlobalCopy (that clones
// the last-edited wire's vertices).
function SbWire(Doc: IDispatch; X1, Y1, X2, Y2: Integer): IDispatch;
begin
    Result := SchServer.SchObjectFactory(eWire, eCreate_Default);
    Result.Location := Point(X1, Y1);
    Result.InsertVertex := 1;
    Result.SetState_Vertex(1, Point(X1, Y1));
    Result.InsertVertex := 2;
    Result.SetState_Vertex(2, Point(X2, Y2));
    SbRegister(Doc, Result);
end;

// A net label; names a net only when (X, Y) lies on a wire.
function SbNetLabel(Doc: IDispatch; Text: String; X, Y, Orientation: Integer): IDispatch;
begin
    Result := SchServer.SchObjectFactory(eNetLabel, eCreate_Default);
    Result.Text := Text;
    Result.Location := Point(X, Y);
    Result.Orientation := Orientation;
    SbRegister(Doc, Result);
end;

// A directive (parameter set): visible Name plus one hidden parameter, e.g.
// ('MC_SPI6', ..., 'ClassName', 'MC_SPI6'). Style 1 draws the house circle.
function SbDirective(Doc: IDispatch; Name: String; X, Y, Orientation, Color, Style: Integer; ParamName, ParamText: String): IDispatch;
var
    P : IDispatch;
begin
    Result := SchServer.SchObjectFactory(eParameterSet, eCreate_Default);
    Result.Name := Name;
    Result.Location := Point(X, Y);
    Result.Orientation := Orientation;
    Result.Color := Color;
    Result.Style := Style;
    P := SchServer.SchObjectFactory(eParameter, eCreate_Default);
    P.Name := ParamName;
    P.Text := ParamText;
    P.IsHidden := True;
    Result.AddSchObject(P);
    SbRegister(Doc, Result);
end;

// A port in the house colours; HarnessType '' for a plain port.
function SbPort(Doc: IDispatch; Name, HarnessType: String; X, Y, Width: Integer): IDispatch;
begin
    Result := SchServer.SchObjectFactory(ePort, eCreate_Default);
    Result.Location := Point(X, Y);
    Result.Name := Name;
    if HarnessType <> '' then Result.HarnessType := HarnessType;
    Result.Width := Width;
    Result.Height := 1000000;
    Result.Style := 0;
    Result.Alignment := 0;
    Result.Color := 128;
    Result.AreaColor := 8454143;
    Result.TextColor := 128;
    Result.FontID := 1;
    SbRegister(Doc, Result);
end;

procedure Run;
var
    ResultText : String;
    OutLines   : TStringList;
    // === USER VARS (filled in by the run_altium_script tool) ===
    // === END USER VARS ===
begin
    LogPath := '%LOG_PATH%';
    OutPath := '%OUT_PATH%';
    LogLines := TStringList.Create;
    SbxGuardUntil := Now + %GUARD_SECONDS% / 86400;
    SbxGuardTripped := False;
    SbxUndoDocs := TList.Create;
    SbxUndoNames := TStringList.Create;
    SbxUndoPath := '%UNDO_PATH%';
    // The scratch lists are ready to use: a list that was never created is
    // nil, and the first List1.Add would stop the script
    List1 := TStringList.Create;
    List2 := TStringList.Create;
    List3 := TStringList.Create;
    ResultText := '{"sandbox": "no result set"}';
    SandboxLog('sandbox start');

    // Everything after the script runs in a finally block, so the lists are
    // freed, undo groups closed and the result written also when the script
    // leaves with Exit: Exit ends Run, and DelphiScript still runs finally
    // blocks on the way out (tested on Altium 26.11)
    try
    try
        // === BEGIN EXPERIMENT (filled in by the run_altium_script tool) ===
        SandboxLog('no script loaded');
        // === END EXPERIMENT ===
    except
        // Errors inside the body - undeclared identifiers, calls on nil, bad
        // conversions - end up here instead of in a blocking dialog. The tool
        // reads this log line and reports the run as failed.
        SandboxLog('EXCEPTION: ' + ExceptionMessage);
        ResultText := '{"error": "script raised an exception - see steps"}';
    end;
    finally
        List1.Free;
        List2.Free;
        List3.Free;

        SbxCloseUndo;
        SandboxLog('sandbox end');

        OutLines := TStringList.Create;
        try
            OutLines.Text := ResultText;
            OutLines.SaveToFile(OutPath);
        finally
            OutLines.Free;
        end;
    end;
end;
