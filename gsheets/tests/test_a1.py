import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gsheets import a1  # noqa: E402


class ColumnsTest(unittest.TestCase):
    def test_round_trip(self):
        for n, s in ((1, "A"), (26, "Z"), (27, "AA"), (52, "AZ"), (53, "BA"), (702, "ZZ"), (703, "AAA")):
            self.assertEqual(a1.index_to_col(n), s)
            self.assertEqual(a1.col_to_index(s), n)
        self.assertEqual(a1.col_to_index("aa"), 27)
        with self.assertRaises(ValueError):
            a1.col_to_index("A1")
        with self.assertRaises(ValueError):
            a1.index_to_col(0)


class ParseTest(unittest.TestCase):
    def test_forms(self):
        cases = {
            "A1": a1.Ref(None, 1, 1, 1, 1),
            "B2:D10": a1.Ref(None, 2, 2, 10, 4),
            "'My Tab'!B2:D": a1.Ref("My Tab", 2, 2, None, 4),
            "Sheet1!A:C": a1.Ref("Sheet1", None, 1, None, 3),
            "3:5": a1.Ref(None, 3, None, 5, None),
            "Pins": a1.Ref("Pins"),
            "'Device Tree'": a1.Ref("Device Tree"),
            "Device Tree": a1.Ref("Device Tree"),
            "'It''s'!A1": a1.Ref("It's", 1, 1, 1, 1),
            "AB12": a1.Ref(None, 12, 28, 12, 28),
            "Summary": a1.Ref("Summary"),
            "": a1.Ref(),
        }
        for text, ref in cases.items():
            with self.subTest(text):
                self.assertEqual(a1.parse(text), ref)

    def test_to_a1_round_trip_and_quoting(self):
        for text in ("A1", "B2:D10", "'My Tab'!B2:D", "Sheet1!A:C", "3:5", "Pins", "'Device Tree'"):
            self.assertEqual(a1.to_a1(a1.parse(text)), text)
        self.assertEqual(a1.quote_tab("Pins"), "Pins")
        self.assertEqual(a1.quote_tab("Device Tree"), "'Device Tree'")
        self.assertEqual(a1.quote_tab("A1"), "'A1'", "a tab that looks like a cell must be quoted")
        self.assertEqual(a1.quote_tab("It's"), "'It''s'")
        self.assertEqual(a1.to_a1(a1.Ref("Device Tree", 1, 1, 10, 30)), "'Device Tree'!A1:AD10")

    def test_helpers(self):
        self.assertEqual(a1.cell(7, 2), "B7")
        b = a1.block("Pins", 3, 2, 2, 3)
        self.assertEqual(b.a1(), "Pins!B3:D4")
        self.assertTrue(a1.parse("C3").is_cell)
        self.assertTrue(a1.parse("Pins").whole_tab)
        self.assertEqual(a1.parse("B2").with_tab("X").a1(), "X!B2")
        with self.assertRaises(ValueError):
            a1.parse("A1:B2:C3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
