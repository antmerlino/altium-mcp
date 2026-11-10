"""Tests for the generated net-label DelphiScript and the geometry helpers.

These run WITHOUT Altium. That is the point: the expensive way to find a
mistake in generated DelphiScript is a runtime failure halfway through
modifying a live sheet, which leaves the document partially edited.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402


class PascalStringTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(main._pas_str("ESC_PB5_BEMF_EN"), "'ESC_PB5_BEMF_EN'")

    def test_apostrophe_is_doubled(self):
        # An unescaped quote would end the literal and produce a compile error.
        self.assertEqual(main._pas_str("it's"), "'it''s'")

    def test_is_single_quote_delimited(self):
        # DelphiScript strings are single-quoted; using a double quote as the
        # DELIMITER is the "Expression expected but " found" compile error.
        # A double quote *inside* the literal is legal, so it is left alone.
        lit = main._pas_str('ESC_"X"')
        self.assertTrue(lit.startswith("'") and lit.endswith("'"))
        self.assertEqual(lit, "'ESC_\"X\"'")


class PinConnectPointTest(unittest.TestCase):
    """A pin connects PinLength along the pin, NOT at Pin.Location."""

    def test_facing_left(self):
        self.assertEqual(main._pin_connect_point(141000000, 97000000, 2, 2000000),
                         (139000000, 97000000))

    def test_facing_right(self):
        self.assertEqual(main._pin_connect_point(39000000, 35000000, 0, 2000000),
                         (41000000, 35000000))

    def test_vertical(self):
        self.assertEqual(main._pin_connect_point(1000, 2000, 1, 500), (1000, 2500))
        self.assertEqual(main._pin_connect_point(1000, 2000, 3, 500), (1000, 1500))


class PointOnSegmentTest(unittest.TestCase):
    def test_midsegment_counts(self):
        # A label attaches anywhere along a wire, not only at a vertex.
        self.assertTrue(main._point_on_segment((16000000, 30000000),
                                               (24000000, 30000000),
                                               (10000000, 30000000)))

    def test_endpoint_counts(self):
        self.assertTrue(main._point_on_segment((10, 10), (10, 10), (20, 10)))

    def test_off_the_line(self):
        self.assertFalse(main._point_on_segment((15, 11), (10, 10), (20, 10)))

    def test_beyond_the_end(self):
        self.assertFalse(main._point_on_segment((25, 10), (10, 10), (20, 10)))


class SetNetLabelsScriptTest(unittest.TestCase):
    def script(self, renames=None, add=None, delete=None):
        return main._set_net_labels_script(renames or {}, add or [], delete or [])

    def test_refuses_anything_but_a_schematic_sheet(self):
        # 33 is a schematic LIBRARY; registering objects into one is the
        # accident this guard exists to prevent.
        s = self.script(renames={"A": "B"})
        self.assertIn("if Obj1.ObjectId <> 32 then", s)
        self.assertIn("ABORT|not a schematic sheet", s)

    def test_single_undo_transaction(self):
        s = self.script(renames={"A": "B"}, delete=["C"],
                        add=[{"text": "D", "x": 1, "y": 2}])
        self.assertEqual(s.count("PreProcess"), 1)
        self.assertEqual(s.count("PostProcess"), 1)
        self.assertLess(s.index("PreProcess"), s.index("PostProcess"))

    def test_rename_pairs_begin_and_end_modify(self):
        # Without the pairing the GUI keeps showing stale text.
        s = self.script(renames={"A": "B"})
        self.assertEqual(s.count("SCHM_BeginModify"), s.count("SCHM_EndModify"))
        self.assertEqual(s.count("SCHM_BeginModify"), 1)

    def test_rename_map_uses_name_value_pairs(self):
        s = self.script(renames={"OLD_NET": "NEW_NET"})
        self.assertIn("List1.Add('OLD_NET=NEW_NET');", s)
        self.assertIn("IndexOfName(Obj3.Text)", s)

    def test_delete_restarts_the_iterator(self):
        # The iterator does not survive RemoveSchObject, so each delete must
        # find one, destroy the iterator, remove, and look again.
        s = self.script(delete=["GONE"])
        self.assertIn("RemoveSchObject", s)
        self.assertIn("SchIterator_Destroy", s)
        self.assertLess(s.index("SchIterator_Destroy"), s.index("RemoveSchObject"))

    def test_add_registers_and_notifies(self):
        s = self.script(add=[{"text": "NEW", "x": 114000000, "y": 51000000}])
        self.assertIn("SchObjectFactory(eNetLabel, eCreate_Default)", s)
        self.assertIn("Obj4.Text := 'NEW';", s)
        self.assertIn("Point(114000000, 51000000)", s)
        self.assertIn("RegisterSchObjectInContainer", s)
        self.assertIn("SCHM_PrimitiveRegistration", s)

    def test_no_double_quotes_anywhere(self):
        # Pascal has no double-quoted strings; one would fail to compile.
        s = self.script(renames={"A": "B"}, delete=["C"],
                        add=[{"text": "D", "x": 1, "y": 2}])
        self.assertNotIn('"', s)

    def test_quotes_in_a_net_name_are_escaped(self):
        s = self.script(delete=["ODD'NAME"])
        self.assertIn("'ODD''NAME'", s)

    def test_empty_edit_still_produces_a_valid_guarded_script(self):
        s = self.script()
        self.assertIn("if Obj1.ObjectId <> 32 then", s)
        self.assertIn("PostProcess", s)

    def test_block_keywords_balance(self):
        # Catches a dropped block. In Pascal 'end' closes both 'begin' and
        # 'try ... finally', so the balance is begin + try == end.
        s = self.script(renames={"A": "B"}, delete=["C"],
                        add=[{"text": "D", "x": 1, "y": 2}])
        words = [w.strip(";") for w in s.replace("(", " ").split()]
        begins = words.count("begin")
        trys = words.count("try")
        ends = words.count("end")
        self.assertEqual(begins + trys, ends,
                         f"begin={begins} try={trys} end={ends}")

    def test_no_double_quoted_pascal_literal(self):
        # A double quote may appear inside a net name, but never as a string
        # delimiter - that is a compile error.
        s = self.script(renames={"A": "B"}, delete=["C"],
                        add=[{"text": "D", "x": 1, "y": 2}])
        for line in s.split(chr(10)):
            code = line.split("//")[0]
            self.assertNotIn('"', code, f"double quote in generated line: {line}")


class DumpParserTest(unittest.TestCase):
    def test_parses_a_sheet_dump(self):
        dump = "\n".join([
            "doc|D:/x/ESC MCU.SchDoc|32",
            "netlabel|ESC_PB5_BEMF_EN|114000000|67000000|0",
            "power|GND|42000000|29000000|4",
            "wire|139000000,84000000|114000000,84000000",
        ])
        doc, objs = main._parse_sch_dump(dump)
        self.assertTrue(doc["is_schematic_sheet"])
        self.assertEqual(doc["object_id"], 32)
        kinds = [o["kind"] for o in objs]
        self.assertEqual(kinds, ["netlabel", "power", "wire"])
        self.assertEqual(objs[0]["text"], "ESC_PB5_BEMF_EN")
        self.assertEqual(len(objs[2]["vertices"]), 2)

    def test_library_is_not_a_sheet(self):
        doc, _ = main._parse_sch_dump("doc|D:/x/lib.SchLib|33")
        self.assertFalse(doc["is_schematic_sheet"])

    def test_pin_gets_its_electrical_point(self):
        dump = "pin|8|PA0|141000000|97000000|1|2|2000000|MCU|U?"
        _, objs = main._parse_sch_dump(dump)
        self.assertEqual(objs[0]["connect_x"], 139000000)
        self.assertEqual(objs[0]["connect_y"], 97000000)

    def test_bad_line_is_reported_not_swallowed(self):
        _, objs = main._parse_sch_dump("netlabel|only|two")
        self.assertEqual(objs[0]["kind"], "unparsed")


class DrawWiresScriptTest(unittest.TestCase):
    def test_vertices_are_one_based_and_complete(self):
        s = main._draw_wires_script([[10, 20, 30, 20, 30, 40]], [])
        self.assertIn("SchObjectFactory(eWire, eCreate_Default)", s)
        for n, (x, y) in enumerate([(10, 20), (30, 20), (30, 40)], start=1):
            self.assertIn(f"Obj4.InsertVertex := {n};", s)
            self.assertIn(f"Obj4.SetState_Vertex({n}, Point({x}, {y}));", s)

    def test_wire_uses_default_not_global_copy(self):
        # eCreate_GlobalCopy clones the last-edited wire's VERTICES (56 wires
        # once got a stray third vertex that way) and there is no DeleteVertex.
        s = main._draw_wires_script([[0, 0, 10, 0]], [])
        self.assertNotIn("eCreate_GlobalCopy", s)

    def test_junctions_and_single_transaction(self):
        s = main._draw_wires_script([[0, 0, 10, 0]], [{"x": 5, "y": 5}])
        self.assertIn("SchObjectFactory(eJunction, eCreate_GlobalCopy)", s)
        self.assertEqual(s.count("PreProcess"), 1)
        self.assertEqual(s.count("PostProcess"), 1)

    def test_guarded_against_non_sheets(self):
        s = main._draw_wires_script([[0, 0, 10, 0]], [])
        self.assertIn("Obj1.ObjectId <> 32", s)
        self.assertIn("ABORT", s)


class DeleteObjectsScriptTest(unittest.TestCase):
    def test_restarts_iterator_per_removal(self):
        s = main._delete_objects_script("eNetLabel", "X", None, None, None, None, 10)
        self.assertLess(s.index("SchIterator_Destroy"), s.index("RemoveSchObject"))
        self.assertIn("for I3 := 1 to 10 do", s)

    def test_text_filter_is_escaped(self):
        s = main._delete_objects_script("eNetLabel", "O'X", None, None, None, None, 5)
        self.assertIn("'O''X'", s)

    def test_area_filter_bounds(self):
        s = main._delete_objects_script("eWire", None, 1, 2, 3, 4, 5)
        self.assertIn("Obj3.Location.X < 1", s)
        self.assertIn("Obj3.Location.X > 3", s)


class NetlistParserTest(unittest.TestCase):
    def test_parses_nets_and_components(self):
        dump = "\n".join([
            "project|D:/x/y.PrjPcb",
            "counts|2|3",
            "comp|R1|R1|2|Sheet1.SchDoc",
            "comp|U?|U?|48|ESC MCU.SchDoc",
            "net|GND|3|R1.2|U1.49|C3.1",
            "net|FLOATING|1|R9.1",
        ])
        project, counts, nets, comps, errors = main._parse_netlist(dump)
        self.assertEqual(project, "D:/x/y.PrjPcb")
        self.assertEqual(counts, {"nets": 2, "components": 3})
        self.assertEqual(len(nets), 2)
        self.assertEqual(nets[0]["name"], "GND")
        self.assertEqual(nets[0]["pins"][0], {"designator": "R1", "pin": "2"})
        self.assertEqual(errors, [])

    def test_unannotated_is_flagged(self):
        _, _, _, comps, _ = main._parse_netlist("comp|U?|U?|48|ESC MCU.SchDoc")
        self.assertFalse(comps[0]["annotated"])

    def test_annotated_is_flagged(self):
        _, _, _, comps, _ = main._parse_netlist("comp|R1|R1|2|S.SchDoc")
        self.assertTrue(comps[0]["annotated"])

    def test_error_line_surfaces(self):
        _, _, _, _, errors = main._parse_netlist("ERR|no focused project")
        self.assertEqual(errors, ["no focused project"])


class SaveScriptTest(unittest.TestCase):
    def test_saves_each_path(self):
        s = main._save_script([r"D:\a.SchDoc", r"D:\b.PcbDoc"])
        self.assertEqual(s.count("DoFileSave('')"), 2)
        self.assertIn(r"'D:\a.SchDoc'", s)
        self.assertIn(r"'D:\b.PcbDoc'", s)

    def test_skips_documents_that_are_not_open(self):
        # GetDocumentByPath returns nil for a closed document; saving that
        # would be a nil-receiver crash.
        s = main._save_script([r"D:\a.SchDoc"])
        self.assertIn("if Obj2 = nil then", s)
        self.assertIn("skip|not open", s)

    def test_reports_whether_the_document_came_back_clean(self):
        s = main._save_script([r"D:\a.SchDoc"])
        self.assertIn("still_modified=", s)

    def test_path_quotes_escaped(self):
        s = main._save_script([r"D:\it's.SchDoc"])
        self.assertIn("'D:\\it''s.SchDoc'", s)


class AnnotateScriptTest(unittest.TestCase):
    def one(self):
        return main._annotate_script([{"from": "R?", "to": "R7",
                                       "x": 1000, "y": 2000}])

    def test_matches_on_location_and_current_text(self):
        # Location alone is not enough - a second pass must not rename a
        # component that already got its designator.
        s = self.one()
        self.assertIn("(Obj3.Location.X = 1000) and (Obj3.Location.Y = 2000)", s)
        self.assertIn("Obj3.Designator.Text = 'R?'", s)

    def test_sets_the_new_designator(self):
        self.assertIn("Obj5.Designator.Text := 'R7';", self.one())

    def test_pairs_begin_and_end_modify(self):
        s = self.one()
        self.assertEqual(s.count("SCHM_BeginModify"), s.count("SCHM_EndModify"))
        self.assertEqual(s.count("SCHM_BeginModify"), 1)

    def test_single_undo_transaction_and_sheet_guard(self):
        s = main._annotate_script([{"from": "R?", "to": "R7", "x": 1, "y": 2},
                                   {"from": "C?", "to": "C9", "x": 3, "y": 4}])
        self.assertEqual(s.count("PreProcess"), 1)
        self.assertEqual(s.count("PostProcess"), 1)
        self.assertIn("Obj1.ObjectId <> 32", s)

    def test_iterator_destroyed_per_lookup(self):
        s = main._annotate_script([{"from": "R?", "to": "R7", "x": 1, "y": 2},
                                   {"from": "C?", "to": "C9", "x": 3, "y": 4}])
        self.assertEqual(s.count("SchIterator_Create"), 2)
        self.assertEqual(s.count("SchIterator_Destroy"), 2)


class ComponentPartCountTest(unittest.TestCase):
    def test_part_count_is_parsed(self):
        _, objs = main._parse_sch_dump("component|U?|MCU|10|20|2|3")
        self.assertEqual(objs[0]["part_count"], 3)
        self.assertEqual(objs[0]["current_part_id"], 2)

    def test_part_count_defaults_to_one(self):
        # Older dumps without the field must not break annotation.
        _, objs = main._parse_sch_dump("component|R?|RES|10|20|1")
        self.assertEqual(objs[0]["part_count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class HexColourArgumentTest(unittest.TestCase):
    """mcp 1.5.0 pre-parses every string argument as JSON, so the hex colour
    "800000" reaches a tool as the int 800000 while "FFFFFF" and "000000"
    stay strings. The colour helpers must take both and agree."""

    def test_all_digit_colour_survives_as_int(self):
        self.assertEqual(main._hex6(800000), "800000")
        self.assertEqual(main._hex6(980000), "980000")
        self.assertEqual(main._hex6(100), "000100", "an int that lost nothing but is short is padded")
        self.assertEqual(main._hex_to_bgr(800000), main._hex_to_bgr("800000"))
        self.assertEqual(main._hex_to_bgr("800000"), 0x00000080, "RRGGBB 800000 -> $00BBGGRR")

    def test_string_forms(self):
        self.assertEqual(main._hex6(" #ffffb0 "), "FFFFB0")
        self.assertEqual(main._hex6("000000"), "000000")
        self.assertEqual(main._hex_to_bgr("FFFFB0"), 0x00B0FFFF, "Altium stock body fill")
        self.assertEqual(main._hex_to_bgr("0000FF"), 0x00FF0000, "Altium stock outline")

    def test_rejects_garbage(self):
        for bad in ("", "12345", "1234567", "GGGGGG", -1, 1000000, True, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                main._hex6(bad)


def _balanced(s):
    """begin/try and end balance for a generated block."""
    opens = sum(line.strip().split()[0] in ("begin", "try") for line in s.splitlines()
                if line.strip() and line.strip().split()[0] in ("begin", "try"))
    ends = sum(line.strip().startswith("end") for line in s.splitlines())
    return opens, ends


class DumpParamsetAndPortTest(unittest.TestCase):
    def test_paramset_branch_is_emitted_only_when_asked(self):
        self.assertIn("eParameterSet", main._sch_dump_script(["paramset"]))
        self.assertNotIn("eParameterSet", main._sch_dump_script(["netlabel"]))

    def test_paramset_line_parses_hidden_params(self):
        doc, objs = main._parse_sch_dump(
            "doc|X.SchDoc|32\nparamset|MC_ETH0|95000000|67000000|1|255|ClassName=ETH0=H\n"
            "paramset|DIFFPAIR_USB|39000000|15000000|0|255|DifferentialPair=True=H"
            "|DifferentialPairClassName=USB=H")
        self.assertEqual(len(objs), 2)
        self.assertEqual(objs[0]["name"], "MC_ETH0")
        self.assertEqual(objs[0]["params"], [{"name": "ClassName", "text": "ETH0", "hidden": True}])
        self.assertEqual([p["name"] for p in objs[1]["params"]],
                         ["DifferentialPair", "DifferentialPairClassName"])

    def test_port_line_reports_harness_fields_and_stays_backward_compatible(self):
        _, objs = main._parse_sch_dump("port|SPI2|18000000|72000000|SPI|4000000|1000000|3|0|0")
        self.assertEqual(objs[0]["harness_type"], "SPI")
        self.assertEqual(objs[0]["width"], 4000000)
        _, old = main._parse_sch_dump("port|SPI2|18000000|72000000|0")
        self.assertEqual(old[0]["name"], "SPI2")
        self.assertNotIn("harness_type", old[0])


class MatchConditionTest(unittest.TestCase):
    def test_kind_and_fields(self):
        c = main._match_condition("paramset", {"name": "MC_UART7", "y": 42000000})
        self.assertEqual(c, "(Obj3.ObjectId = eParameterSet) and (Obj3.Name = 'MC_UART7') "
                            "and (Obj3.Location.Y = 42000000)")

    def test_component_matches_designator_and_libref(self):
        c = main._match_condition("component", {"designator": "BOT?", "lib_reference": "TESTPOINT_PAD_MFG"})
        self.assertIn("Obj3.Designator.Text = 'BOT?'", c)
        self.assertIn("Obj3.LibReference = 'TESTPOINT_PAD_MFG'", c)

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            main._match_condition("bus", {})

    def test_python_twin_agrees(self):
        o = {"kind": "paramset", "name": "MFG_TP", "x": 96000000, "y": 51000000}
        self.assertTrue(main._object_matches("paramset", {"name": "MFG_TP", "x": 96000000}, o))
        self.assertFalse(main._object_matches("paramset", {"name": "MFG_TP", "y": 1}, o))
        self.assertFalse(main._object_matches("port", {"name": "MFG_TP"}, o))


class MoveObjectsScriptTest(unittest.TestCase):
    def test_to_and_by_use_MoveByXY_inside_a_modify_pair(self):
        s = main._move_objects_script([
            {"kind": "paramset", "match": {"name": "MC_ETH0"}, "by": [0, -1000000]},
            {"kind": "port", "match": {"name": "UART7"}, "to": [18000000, 33000000]}])
        self.assertIn("Obj3.MoveByXY(0, -1000000);", s)
        self.assertIn("Obj3.MoveByXY(18000000 - Obj3.Location.X, 33000000 - Obj3.Location.Y);", s)
        self.assertEqual(s.count("SCHM_BeginModify"), 2)
        self.assertEqual(s.count("SCHM_EndModify"), 2)
        self.assertNotIn(".Location := ", s, "Location assignment leaves children behind")

    def test_guard_and_single_transaction(self):
        s = main._move_objects_script([{"kind": "netlabel", "match": {"text": "N"}, "by": [1, 1]}])
        self.assertIn("if Obj1.ObjectId <> 32 then", s)
        self.assertEqual(s.count("PreProcess"), 1)
        self.assertEqual(s.count("PostProcess"), 1)
        self.assertNotIn('"', s)


class ParamSetScriptTest(unittest.TestCase):
    def test_add_creates_a_hidden_classname_and_registers(self):
        s = main._set_paramsets_script(
            [{"name": "MC_SPI6", "x": 69000000, "y": 61000000, "class_name": "MC_SPI6"}], [], [])
        self.assertIn("SchObjectFactory(eParameterSet, eCreate_Default)", s)
        self.assertIn("Obj4.Name := 'MC_SPI6';", s)
        self.assertIn("Obj5.Name := 'ClassName';", s)
        self.assertIn("Obj5.Text := 'MC_SPI6';", s)
        self.assertIn("Obj5.IsHidden := True;", s)
        self.assertIn("Obj4.AddSchObject(Obj5);", s)
        self.assertIn("RegisterSchObjectInContainer(Obj4)", s)
        self.assertIn("Obj4.Orientation := 1;", s)
        self.assertIn("Obj4.Color := 255;", s)

    def test_diff_pair_params_are_all_hidden(self):
        s = main._set_paramsets_script([{"name": "DIFFPAIR_USB", "x": 1, "y": 2, "orientation": 0,
                                         "params": {"DifferentialPair": "True",
                                                    "DifferentialPairClassName": "USB"}}], [], [])
        self.assertEqual(s.count("Obj5.IsHidden := True;"), 2)
        self.assertIn("Obj5.Text := 'USB';", s)

    def test_rename_sets_name_and_param_text_under_modify(self):
        s = main._set_paramsets_script([], [{"match": {"name": "MC_I2C1", "y": 85000000},
                                             "name": "MC_I2C2", "class_name": "MC_I2C2"}], [])
        self.assertIn("(Obj3.Name = 'MC_I2C1') and (Obj3.Location.Y = 85000000)", s)
        self.assertIn("Obj3.Name := 'MC_I2C2';", s)
        self.assertIn("if Obj5.Name = 'ClassName' then", s)
        self.assertIn("Obj5.Text := 'MC_I2C2';", s)
        self.assertGreaterEqual(s.count("SCHM_BeginModify"), 2, "the set and its parameter")

    def test_delete_restarts_the_iterator(self):
        s = main._set_paramsets_script([], [], [{"name": "50ohm trace", "x": 38000000}])
        self.assertIn("for B1 := 1 to 200 do", s)
        self.assertIn("Obj1.RemoveSchObject(Obj5);", s)
        self.assertIn("(Obj3.Name = '50ohm trace') and (Obj3.Location.X = 38000000)", s)

    def test_blocks_balance_and_no_double_quotes(self):
        s = main._set_paramsets_script(
            [{"name": "A", "x": 1, "y": 2, "class_name": "A"}],
            [{"match": {"name": "B"}, "name": "C", "params": {"ClassName": "C"}}],
            [{"name": "D"}])
        opens, ends = _balanced(s)
        self.assertEqual(opens, ends, s)
        self.assertNotIn('"', s)


class PortScriptTest(unittest.TestCase):
    def test_add_applies_house_defaults_and_given_members(self):
        s = main._set_ports_script([{"name": "SPI6", "harness_type": "SPI", "x": 18000000,
                                     "y": 60000000, "width": 4000000, "io_type": 3}], [], [])
        self.assertIn("SchObjectFactory(ePort, eCreate_Default)", s)
        self.assertIn("Obj4.Location := Point(18000000, 60000000);", s)
        self.assertIn("Obj4.Name := 'SPI6';", s)
        self.assertIn("Obj4.HarnessType := 'SPI';", s)
        self.assertIn("Obj4.Width := 4000000;", s)
        self.assertIn("Obj4.Height := 1000000;", s)
        self.assertIn("Obj4.IOType := 3;", s)
        self.assertIn("Obj4.AreaColor := 8454143;", s)
        self.assertIn("Obj4.FontID := 1;", s)
        self.assertIn("RegisterSchObjectInContainer(Obj4)", s)

    def test_rename_only_touches_given_members(self):
        s = main._set_ports_script([], [{"match": {"name": "UART5"}, "name": "USART3"}], [])
        self.assertIn("(Obj3.ObjectId = ePort) and (Obj3.Name = 'UART5')", s)
        self.assertIn("Obj3.Name := 'USART3';", s)
        self.assertNotIn("Obj3.Width", s)
        self.assertNotIn("HarnessType :=", s)

    def test_blocks_balance_and_no_double_quotes(self):
        s = main._set_ports_script([{"name": "A", "x": 1, "y": 2, "width": 3}],
                                   [{"match": {"name": "B"}, "harness_type": "UART"}],
                                   [{"name": "C"}])
        opens, ends = _balanced(s)
        self.assertEqual(opens, ends, s)
        self.assertNotIn('"', s)


class HarnessDumpTest(unittest.TestCase):
    def test_harness_branches_only_when_asked(self):
        self.assertIn("eHarnessConnector", main._sch_dump_script(["harness"]))
        self.assertIn("eSignalHarness", main._sch_dump_script(["signalharness"]))
        self.assertNotIn("eHarnessConnector", main._sch_dump_script(["port"]))

    def test_harness_line_parses_entries(self):
        _, objs = main._parse_sch_dump(
            "harness|SPI|32000000|66000000|5000000|4000000|2000000"
            "|MISO,37000000,63000000,1,3000000|SCK,37000000,65000000,1,1000000")
        h = objs[0]
        self.assertEqual((h["harness_type"], h["x"], h["y"]), ("SPI", 32000000, 66000000))
        self.assertEqual(h["primary_connection_position"], 2000000)
        self.assertEqual(h["entries"][1], {"name": "SCK", "x": 37000000, "y": 65000000,
                                           "side": 1, "distance_from_top": 1000000})

    def test_signal_harness_anchors_on_first_vertex(self):
        _, objs = main._parse_sch_dump("signalharness|22000000,64000000|29500000,64000000|32000000,64000000")
        self.assertEqual((objs[0]["x"], objs[0]["y"]), (22000000, 64000000))
        self.assertEqual(len(objs[0]["vertices"]), 3)


class HarnessScriptTest(unittest.TestCase):
    TEMPLATE = {"harness_type": "SPI", "top_y": 66000000, "harness_start": [22000000, 64000000]}

    def test_add_replicates_moves_edits_and_registers(self):
        s = main._set_harness_connectors_script([{
            "template": self.TEMPLATE, "by": [0, -10000000], "harness_type": "QSPI",
            "y_size": 7000000, "primary_connection_position": 4000000,
            "entries": {"rename": {"SCK": "CLK"}, "remove": ["MISO"],
                        "add": [{"name": "IO2", "distance_from_top": 4000000, "side": 1}]},
            "harness_end": [13200000, 2300000]}], [], [])
        self.assertIn("(Obj4.HarnessType = 'SPI') and (Obj4.Location.Y = 66000000)", s)
        self.assertIn("(Obj4.Vertex[1].X = 22000000) and (Obj4.Vertex[1].Y = 64000000)", s)
        self.assertEqual(s.count("Obj5 := Obj3.Replicate;"), 2, "connector and its harness")
        self.assertEqual(s.count("Obj5.MoveByXY(0, -10000000);"), 2)
        self.assertIn("Obj5.HarnessType := 'QSPI';", s)
        self.assertIn("Obj5.YSize := 7000000;", s)
        self.assertIn("Obj5.PrimaryConnectionPosition := 4000000;", s)
        self.assertIn("Obj2.Name := 'CLK';", s)
        self.assertIn("Obj5.RemoveSchObject(Obj2);", s)
        self.assertIn("SchObjectFactory(eHarnessEntry, eCreate_Default)", s)
        self.assertIn("Obj5.AddSchObject(Obj2);", s)
        self.assertIn("Obj5.SetState_Vertex(1, Point(13200000, 2300000));", s)
        self.assertEqual(s.count("RegisterSchObjectInContainer(Obj5)"), 2)

    def test_move_and_delete(self):
        s = main._set_harness_connectors_script(
            [], [{"harness_type": "UART", "top_y": 49000000, "harness_start": [27000000, 47000000],
                  "by": [7000000, -10000000]}],
            [{"harness_type": "CANTXRX", "top_y": 32500000, "harness_start": [32000000, 31000000]}])
        self.assertEqual(s.count("Obj3.MoveByXY(7000000, -10000000);"), 2)
        self.assertEqual(s.count("Obj1.RemoveSchObject(Obj3);"), 2)
        self.assertIn("SCHM_BeginModify", s)

    def test_guard_transaction_and_quotes(self):
        s = main._set_harness_connectors_script([], [{"harness_type": "SPI", "top_y": 1, "by": [1, 1]}], [])
        self.assertIn("if Obj1.ObjectId <> 32 then", s)
        self.assertEqual(s.count("PreProcess"), 1)
        self.assertNotIn('"', s)
        opens, ends = _balanced(s)
        self.assertEqual(opens, ends, s)


class ParamSetStyleTest(unittest.TestCase):
    def test_default_style_is_the_house_circle(self):
        s = main._set_paramsets_script([{"name": "A", "x": 1, "y": 2, "class_name": "A"}], [], [])
        self.assertIn("Obj4.Style := 1;", s)
        s0 = main._set_paramsets_script([{"name": "P", "x": 1, "y": 2, "style": 0,
                                          "params": {"DifferentialPair": "True"}}], [], [])
        self.assertIn("Obj4.Style := 0;", s0)


class TargetDocumentTest(unittest.TestCase):
    def test_replaces_only_the_first_focused_document_lookup(self):
        s = main._target_document(main._move_objects_script(
            [{"kind": "port", "match": {"name": "X"}, "by": [1, 1]}]),
            r"D:/Altium/X57 Main Board/MC_STM32H753XIHx_Comms.SchDoc")
        self.assertIn("SchServer.GetSchDocumentByPath('D:\\Altium\\X57 Main Board\\MC_STM32H753XIHx_Comms.SchDoc')", s)
        self.assertNotIn("GetCurrentSchDocument", s)

    def test_no_document_leaves_the_script_alone(self):
        s = main._move_objects_script([{"kind": "port", "match": {"name": "X"}, "by": [1, 1]}])
        self.assertEqual(main._target_document(s, None), s)

    def test_dump_script_accepts_a_document(self):
        s = main._sch_dump_script(["port"], r"D:\x\Sheet1.SchDoc")
        self.assertIn("GetSchDocumentByPath('D:\\x\\Sheet1.SchDoc')", s)
        self.assertIn("ePort", s)


class ProjectPreludeTest(unittest.TestCase):
    def test_netlist_never_compiles_the_script_project(self):
        s = main._NETLIST_PAS
        self.assertIn("'.PRJSCR'", s)
        self.assertIn("'.PRJPCB'", s)
        self.assertIn("UpperCase(Obj4.DM_LogicalDocuments(B1).DM_FullPath) = UpperCase(S3)", s)
        self.assertIn("several PCB projects are open", s, "refuses to guess between PCB projects")
        filled = main._project_script(main._NETLIST_PAS, "D:/x/Sheet1.SchDoc")
        self.assertIn("GetSchDocumentByPath(S3)", filled)
        self.assertIn("S3 := 'D:\\x\\Sheet1.SchDoc';", filled)
        self.assertLess(s.index("DM_FocusedProject"), s.index("DM_Compile"))
        self.assertIn(main._PROJECT_PRELUDE, main._DOCSTATE_PAS)
        self.assertIn(main._PROJECT_PRELUDE, main._SAVE_STATE_PAS)
