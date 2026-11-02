# -*- coding: utf-8 -*-
"""Tests for wavesim_gui.api_simulation (container, materials)."""

import unittest

import FreeCAD

from _fixtures import DocTestCase
from wavesim_gui import api
from wavesim_gui import commands


class SimulationTests(DocTestCase):

    def test_new_simulation_matches_gui_structure(self):
        sim = api.new_simulation(self.doc)
        self.assertIs(commands.active_simulation(self.doc), sim)
        labels = [o.Label for o in sim.Group]
        self.assertEqual(labels, ["Domain", "Materials", "Sources", "Monitors"])
        mats = api.list_materials(sim)
        self.assertEqual([m["label"] for m in mats], ["Vacuum", "PEC"])
        domain = sim.Group[0]
        self.assertEqual(domain.Background.Label, "Vacuum")
        self.assertEqual(commands.solver_mode(sim), "fdtd")
        with self.assertRaises(api.ApiError):
            api.new_simulation(self.doc)

    def test_electrostatic_mode(self):
        sim = api.new_simulation(self.doc, mode="electrostatic")
        self.assertEqual(commands.solver_mode(sim), "electrostatic")

    def test_material_create_assign(self):
        sim = api.new_simulation(self.doc)
        box = self.doc.addObject("Part::Box", "Box")
        box.Label = "Substrate"
        self.doc.recompute()
        mat = api.create_material(sim, label="FR4", eps=4.4, sigma="0.01 S/m",
                                  bodies=["Substrate"])
        self.assertAlmostEqual(mat.Eps, 4.4)
        self.assertAlmostEqual(mat.Sigma, 0.01)
        self.assertEqual([b.Name for b in mat.Bodies], ["Box"])
        api.assign_material(sim, "Substrate", "PEC")
        self.assertEqual(list(mat.Bodies), [])
        self.assertTrue(hasattr(box, "WavesimPotential"))
        with self.assertRaises(api.ApiError):
            api.create_material(sim, label="FR4")
        with self.assertRaises(api.ApiError):
            api.create_material(sim, label="Bad", eps=-1)

    def _model(self):
        sim = api.new_simulation(self.doc)
        box = self.doc.addObject("Part::Box", "Box")
        box.Length = box.Width = 10
        box.Height = 2
        box.Label = "Substrate"
        self.doc.recompute()
        api.create_material(sim, label="FR4", eps=4.4, bodies=["Substrate"])
        return sim, box

    def test_run_settings_rederive_cell_size(self):
        sim, _box = self._model()
        api.set_run_settings(sim, max_time="2 ns", max_frequency="10 GHz")
        self.assertAlmostEqual(sim.MaxTime, 2e-9)
        self.assertAlmostEqual(sim.MaxFrequency, 1e10)
        domain = sim.Group[0]
        from wavesim_gui import domain as domain_mod
        expected = domain_mod.suggested_cell_size_m(sim, domain) * 1000.0
        self.assertAlmostEqual(domain.Dx.Value, expected, places=6)
        with self.assertRaises(api.ApiError):
            api.set_run_settings(sim, max_frequency="10 mm")

    def test_run_settings_param_expression(self):
        sim, _box = self._model()
        sheet = self.doc.addObject("Spreadsheet::Sheet", "Params")
        sheet.set("A1", "5000000000")
        sheet.setAlias("A1", "FMAX")
        self.doc.recompute()
        api.set_run_settings(sim, max_frequency="=FMAX")
        self.doc.recompute()
        self.assertAlmostEqual(sim.MaxFrequency, 5e9)
        self.assertIn("MaxFrequency", api.get_object_props(
            sim, "Simulation")["expressions"])

    def test_domain_and_mesh(self):
        sim, _box = self._model()
        api.set_domain(sim, spacing={"z1": "5 mm"}, boundaries={"z0": "PEC"},
                       pml_cells=10)
        domain = sim.Group[0]
        self.assertAlmostEqual(domain.SpacingZMax.Value, 5.0)
        self.assertEqual(domain.BoundaryZMin, "PEC")
        self.assertEqual(domain.PMLThickness, 10)
        info = api.set_mesh(sim, cell_size=[0.5, 0.5, 0.25], nonuniform=False)
        self.assertEqual(info["cell_size_mm"], [0.5, 0.5, 0.25])
        self.assertGreater(info["total_cells"], 0)
        self.assertGreater(info["steps"], 0)
        with self.assertRaises(api.ApiError):
            api.set_mesh(sim, max_grading=0.5)
        with self.assertRaises(api.ApiError):
            api.set_domain(sim, boundaries={"q0": "PML"})

    def test_refine_and_props(self):
        sim, _box = self._model()
        res = api.refine_body(sim, "Substrate", cells=[0, 0, 8])
        self.assertEqual(res["cells"], [0, 0, 8])
        props = api.get_object_props(sim, "FR4")
        self.assertEqual(props["dimensions"]["Sigma"], "conductivity")
        api.set_object_props(sim, "FR4", {"Eps": 2.2, "Sigma": "1e-3 S/m"})
        mat = self.doc.getObjectsByLabel("FR4")[0]
        self.assertAlmostEqual(mat.Eps, 2.2)
        with self.assertRaises(api.ApiError):
            api.set_object_props(sim, "Substrate", {"Length": 3})
        with self.assertRaises(api.ApiError):
            api.set_object_props(sim, "FR4", {"Nope": 3})

    def test_delete_and_validate(self):
        sim, _box = self._model()
        report = api.validate_setup(sim, max_cells=10)
        self.assertFalse(report["ok"])
        self.assertTrue(any("excitation" in e for e in report["errors"]))
        self.assertTrue(any("MAX_NUM_CELLS" in e for e in report["errors"]))
        with self.assertRaises(api.ApiError):
            api.delete_setup_object(sim, "Vacuum")  # domain background
        api.delete_setup_object(sim, "FR4")
        self.assertEqual(self.doc.getObjectsByLabel("FR4"), [])
        report = api.validate_setup(sim)
        self.assertTrue(any("No body" in e for e in report["errors"]))
        self.assertTrue(any("Substrate" in w for w in report["warnings"]))
        setup = api.list_setup(sim)
        self.assertEqual(setup["simulation"]["solver_mode"], "fdtd")

    def test_solver_mode_switch(self):
        sim, _box = self._model()
        api.set_solver_mode(sim, "electrostatic", unset_conductors="floating")
        from wavesim_gui import commands as cmd
        self.assertTrue(cmd.is_electrostatic(sim))
        self.assertEqual(cmd.unset_conductor_mode(sim), cmd.UNSET_FLOAT)


if __name__ == "__main__":
    unittest.main()
