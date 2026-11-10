"""Offline tests for the cluster-level builders and the small helpers behind
copy_schematic_area / move_schematic_area / copy_component / render area."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402

AREA = [30000000, 58000000, 40000000, 66000000]


class InsideTest(unittest.TestCase):
    def test_anchor_objects(self):
        self.assertTrue(main._obj_inside({"kind": "port", "x": 35000000, "y": 60000000}, AREA))
        self.assertFalse(main._obj_inside({"kind": "port", "x": 45000000, "y": 60000000}, AREA))

    def test_wire_needs_every_vertex_inside(self):
        w = {"kind": "wire", "vertices": [{"x": 31000000, "y": 60000000}, {"x": 39000000, "y": 60000000}]}
        self.assertTrue(main._obj_inside(w, AREA))
        w["vertices"][1]["x"] = 69000000
        self.assertFalse(main._obj_inside(w, AREA))

    def test_reversed_corners_are_fine(self):
        self.assertTrue(main._obj_inside({"kind": "port", "x": 35000000, "y": 60000000},
                                         [40000000, 66000000, 30000000, 58000000]))


class BlockingTest(unittest.TestCase):
    def test_picks_the_always_wrong_warnings(self):
        w = ["added label 'X' at (1, 2) is not on a wire - it will name nothing", "rename source not found"]
        self.assertEqual(main._blocking(w, ("is not on a wire",)), [w[0]])


class AreaScriptTest(unittest.TestCase):
    def test_copy_is_two_pass_replicate_with_renames(self):
        s = main._area_script(AREA, 0, -4000000, "copy", None, ["MCU"], {"MC_SPI5": "MC_SPI6"})
        self.assertIn("Obj3.Selection := True;", s, "pass one marks what is inside")
        self.assertIn("if Obj3.Selection = True then", s, "pass two acts on the marked only")
        self.assertIn("Obj5.Selection := False;", s, "a copy can never be copied again")
        self.assertNotIn("I_ObjectAddress))", s, "addresses cannot be stringified through late binding")
        self.assertIn("Obj5 := Obj3.Replicate;", s)
        self.assertIn("Obj5.MoveByXY(0, -4000000);", s)
        self.assertIn("List2.Add('MC_SPI5=MC_SPI6');", s)
        self.assertIn("if Obj3.LibReference = 'MCU' then B2 := 0;", s)
        self.assertIn("RegisterSchObjectInContainer(Obj5)", s)
        self.assertEqual(s.count("PreProcess"), 1)
        self.assertNotIn('"', s)

    def test_wires_need_every_vertex_inside_in_pascal_too(self):
        s = main._area_script(AREA, 1, 1, "move")
        self.assertIn("for I4 := 1 to Obj3.VerticesCount do", s)
        self.assertIn("(Obj3.Vertex[I4].X < 30000000) or (Obj3.Vertex[I4].X > 40000000)", s)

    def test_move_uses_modify_pair_not_replicate(self):
        s = main._area_script(AREA, 5, 6, "move")
        self.assertIn("Obj3.MoveByXY(5, 6);", s)
        self.assertIn("SCHM_BeginModify", s)
        self.assertNotIn("Replicate", s)

    def test_rename_covers_every_text_field(self):
        pas = "\n".join(main._rename_copy_pas("Obj5", {"A": "B"}))
        for field in ("Text", "Name", "Designator.Text", "HarnessType"):
            self.assertIn(field, pas)
        self.assertIn("MkSet(eParameter)", pas)
        self.assertIn("MkSet(eHarnessEntry)", pas)

    def test_kinds_subset(self):
        s = main._area_script(AREA, 1, 1, "move", ["netlabel", "paramset"])
        self.assertIn("(Obj3.ObjectId = eNetLabel) or (Obj3.ObjectId = eParameterSet)", s)
        self.assertNotIn("eWire)", s.split("B2 := 0;")[0].split("if ")[1])


class CopyComponentScriptTest(unittest.TestCase):
    def test_replicates_moves_and_renames_the_designator(self):
        s = main._copy_component_script("BOT54", "TESTPOINT_PAD_MFG", 64000000, 38000000, "BOT?")
        self.assertIn("SbFind(Obj1, eSchComponent, 'BOT54', -1, -1)", s)
        self.assertIn("if Obj3.LibReference <> 'TESTPOINT_PAD_MFG' then Obj3 := nil;", s)
        self.assertIn("Obj5 := Obj3.Replicate;", s)
        self.assertIn("Obj5.MoveByXY(64000000 - Obj3.Location.X, 38000000 - Obj3.Location.Y);", s)
        self.assertIn("Obj5.Designator.Text := 'BOT?';", s)
        self.assertIn("RegisterSchObjectInContainer(Obj5)", s)


class RenderScriptTest(unittest.TestCase):
    def test_area_selects_by_rectangle(self):
        s = main._render_script(r"D:\x\S.SchDoc", [10, 40, 30, 20])
        self.assertIn("Obj4.Selection := True", s)
        self.assertIn("(Obj4.Location.X < 10) or (Obj4.Location.X > 30) or (Obj4.Location.Y < 20) or (Obj4.Location.Y > 40)", s)
        self.assertNotIn("Action', 'All'", s)

    def test_no_area_selects_all(self):
        s = main._render_script(r"D:\x\S.SchDoc")
        self.assertIn("AddStringParameter('Action', 'All');", s)


class DumpBlanketTest(unittest.TestCase):
    def test_blanket_parses(self):
        _, objs = main._parse_sch_dump("blanket|103000000|18000000|115000000|25000000")
        self.assertEqual(objs[0], {"kind": "blanket", "x0": 103000000, "y0": 18000000, "x1": 115000000,
                                   "y1": 25000000, "x": 103000000, "y": 18000000})


if __name__ == "__main__":
    unittest.main()
