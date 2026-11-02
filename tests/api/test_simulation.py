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


if __name__ == "__main__":
    unittest.main()
