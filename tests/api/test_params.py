# -*- coding: utf-8 -*-
"""Tests for wavesim_gui.api_params (Params sheet, bindings, fingerprint)."""

import os
import tempfile
import unittest

import FreeCAD

from _fixtures import DocTestCase
from wavesim_gui import api


def _p(value, unit="mm", desc="d"):
    return {"value": value, "unit": unit, "description": desc}


class ParamsTests(DocTestCase):

    def test_sync_bind_drift(self):
        rep = api.sync_params_to_spreadsheet(self.doc, {
            "CYL_D": _p(3.0), "F_C": _p(2.4e9, "Hz"),
            "SUB": _p("FR4", "-")})
        self.assertEqual(sorted(rep["created"]), ["CYL_D", "F_C", "SUB"])
        self.assertEqual(api.read_params_sheet(self.doc)["SUB"], "FR4")
        cyl = self.doc.addObject("Part::Cylinder", "Cyl")
        res = api.bind_param(self.doc, "Cyl", "Radius", "=CYL_D/2")
        self.assertAlmostEqual(res["value"], 1.5)
        api.bind_param(self.doc, "Cyl", "Placement.Base.z", "CYL_D")
        self.doc.recompute()
        self.assertAlmostEqual(cyl.Placement.Base.z, 3.0)
        # File changed: an update, not drift.
        rep = api.sync_params_to_spreadsheet(self.doc, {
            "CYL_D": _p(4.0), "F_C": _p(2.4e9, "Hz"), "SUB": _p("FR4", "-")})
        self.assertEqual(rep["updated"], ["CYL_D"])
        self.assertEqual(rep["drift"], [])
        self.assertAlmostEqual(cyl.Radius.Value, 2.0)
        self.assertEqual(rep["unused"], ["F_C", "SUB"])
        # Hand edit in FreeCAD, then sync: drift reported, file wins.
        sheet = self.doc.getObject("Params")
        sheet.set("B2", "10")
        self.doc.recompute()
        rep = api.sync_params_to_spreadsheet(self.doc, {
            "CYL_D": _p(4.0), "F_C": _p(2.4e9, "Hz"), "SUB": _p("FR4", "-")})
        self.assertEqual([d["name"] for d in rep["drift"]], ["CYL_D"])
        self.assertAlmostEqual(cyl.Radius.Value, 2.0)
        # A removed-but-bound name is kept and reported.
        rep = api.sync_params_to_spreadsheet(self.doc, {"F_C": _p(1e9, "Hz")})
        self.assertIn("CYL_D", rep["kept_missing_bound"])
        self.assertIn("SUB", rep["removed"])
        self.assertTrue(any(r["name"] == "CYL_D"
                            for r in rep["unknown_references"]))
        self.assertEqual(len(api.list_bindings(self.doc)), 2)

    def test_auto_label_uses_bound_value(self):
        sim = api.new_simulation(self.doc)
        api.sync_params_to_spreadsheet(self.doc, {"FMAX": _p(8e9, "Hz")})
        src = api.add_source(sim, position=[0, 0, 0],
                             excitation={"type": "gaussian", "fmax": "=FMAX"})
        self.assertAlmostEqual(src.Fmax, 8e9)
        self.assertIn("8 GHz", src.Label)

    def test_bind_errors(self):
        self.doc.addObject("Part::Box", "Box")
        with self.assertRaises(api.ApiError):
            api.bind_param(self.doc, "Box", "Nope", "=A")
        with self.assertRaises(api.ApiError):
            api.bind_param(self.doc, "Box", "Length", "=import os")
        dim = api.property_dimension_of(self.doc, "Box", "Length")
        self.assertEqual(dim["dimension"], "length")


class FingerprintTests(DocTestCase):

    def _model(self):
        sim = api.new_simulation(self.doc)
        box = self.doc.addObject("Part::Box", "Box")
        box.Label = "Sub"
        self.doc.recompute()
        api.create_material(sim, label="FR4", eps=4.4, bodies=["Sub"])
        return sim, box

    def test_stable_and_sensitive(self):
        sim, box = self._model()
        fp1 = api.model_fingerprint(self.doc)
        self.assertEqual(fp1, api.model_fingerprint(self.doc))
        path = os.path.join(tempfile.mkdtemp(), "fp.FCStd")
        self.doc.saveAs(path)
        name = self.doc.Name
        FreeCAD.closeDocument(name)
        self.doc = FreeCAD.openDocument(path)
        self.doc.recompute()
        fp2 = api.model_fingerprint(self.doc)
        self.assertEqual(fp1["model_hash"], fp2["model_hash"])
        self.assertEqual(fp1["setup_hash"], fp2["setup_hash"])
        box = self.doc.getObject("Box")
        box.Height = 3
        self.doc.recompute()
        fp3 = api.model_fingerprint(self.doc)
        self.assertNotEqual(fp2["model_hash"], fp3["model_hash"])
        self.assertEqual(fp2["setup_hash"], fp3["setup_hash"])
        api.set_material(self.doc, "FR4", eps=2.2)
        fp4 = api.model_fingerprint(self.doc)
        self.assertNotEqual(fp3["setup_hash"], fp4["setup_hash"])


if __name__ == "__main__":
    unittest.main()
