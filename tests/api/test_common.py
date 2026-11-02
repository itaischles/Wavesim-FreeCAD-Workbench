# -*- coding: utf-8 -*-
"""Tests for wavesim_gui.api_common (quantities, references, selectors)."""

import unittest

import FreeCAD
import Part

from _fixtures import DocTestCase
from wavesim_gui import api_common as c


class QuantityTests(unittest.TestCase):

    def test_numbers_use_default_unit(self):
        self.assertEqual(c.parse_quantity(3, "length"), ("value", 3.0))
        self.assertEqual(c.parse_quantity(2, "length", "m"), ("value", 2000.0))

    def test_strings_are_case_sensitive(self):
        self.assertAlmostEqual(c.parse_quantity("2.4 GHz", "frequency")[1], 2.4e9)
        self.assertAlmostEqual(c.parse_quantity("50 mHz", "frequency")[1], 0.05)
        self.assertAlmostEqual(c.parse_quantity("50 MHz", "frequency")[1], 5e7)
        with self.assertRaises(c.ApiError):
            c.parse_quantity("3 MM", "length")

    def test_dimension_mismatch(self):
        with self.assertRaises(c.ApiError):
            c.parse_quantity("3 GHz", "length")

    def test_expression(self):
        kind, expr = c.parse_quantity("=CYL_D/2 + GAP", "length")
        self.assertEqual(kind, "expr")
        self.assertEqual(expr, "Params.CYL_D/2 + Params.GAP")
        self.assertEqual(c.param_expression("=1E6*A"), "1E6*Params.A")
        with self.assertRaises(c.ApiError):
            c.parse_quantity("=__import__('os')", "length")

    def test_angle_rad(self):
        self.assertAlmostEqual(c.parse_quantity("3.141592653589793 rad",
                                                "angle")[1], 180.0)


class ObjectTests(DocTestCase):

    def test_find_and_label(self):
        box = self.doc.addObject("Part::Box", "Box")
        box.Label = "Substrate"
        self.assertIs(c.find_object(self.doc, "Substrate"), box)
        self.assertIs(c.find_object(None, "Box"), box)
        with self.assertRaises(c.ApiError):
            c.find_object(self.doc, "Nope")
        with self.assertRaises(c.ApiError):
            c.check_new_label(self.doc, "Substrate")

    def test_transaction_aborts(self):
        self.doc.UndoMode = 1
        with self.assertRaises(RuntimeError):
            with c.transaction(self.doc, "t"):
                self.doc.addObject("Part::Box", "Gone")
                raise RuntimeError("x")
        self.assertIsNone(self.doc.getObject("Gone"))

    def test_set_quantity_expression(self):
        sheet = self.doc.addObject("Spreadsheet::Sheet", "Params")
        sheet.set("A1", "4")
        sheet.setAlias("A1", "CYL_D")
        cyl = self.doc.addObject("Part::Cylinder", "Cyl")
        c.set_quantity(cyl, "Radius", "=CYL_D/2", "length")
        self.doc.recompute()
        self.assertAlmostEqual(cyl.Radius.Value, 2.0)
        c.set_quantity(cyl, "Radius", "3 mm", "length")
        self.doc.recompute()
        self.assertAlmostEqual(cyl.Radius.Value, 3.0)

    def test_selectors(self):
        box = Part.makeBox(10, 10, 2)
        self.assertEqual(len(c.select_subshapes(box, {"type": "all"})), 12)
        self.assertEqual(len(c.select_subshapes(box, {"parallel_to": "z"})), 4)
        self.assertEqual(len(c.select_subshapes(box, {"at_z": 2})), 4)
        self.assertEqual(len(c.select_subshapes(
            box, {"near_point": [5, 0, 2]})), 1)
        top = c.select_subshapes(box, {"normal": "+z"}, kind="face")
        self.assertEqual(len(top), 1)
        self.assertAlmostEqual(top[0][1].BoundBox.ZMin, 2)


if __name__ == "__main__":
    unittest.main()
