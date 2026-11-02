# -*- coding: utf-8 -*-
"""Tests for wavesim_gui.api_preview (headless parts)."""

import os
import shutil
import tempfile
import unittest

import FreeCAD

from _fixtures import DocTestCase
from wavesim_gui import api


class PreviewTests(DocTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="wavesim_api_preview_")

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_voxel_preview(self):
        sim = api.new_simulation(self.doc)
        api.create_box(self.doc, label="Sub", length=10, width=10, height=2)
        api.create_cylinder(self.doc, label="Pin", radius=1, height=4,
                            position=[5, 5, 0])
        api.create_material(sim, label="FR4", eps=4.4, bodies=["Sub"])
        api.assign_material(sim, "Pin", "PEC")
        api.set_mesh(sim, cell_size=0.5, nonuniform=False)
        path = os.path.join(self.tmp, "01_voxels_z.png")
        res = api.voxel_preview(self.doc, plane="z", offset=1.0, path=path)
        self.assertTrue(os.path.getsize(path) > 1000)
        self.assertGreater(res["pec_cells_in_slice"], 0)
        self.assertIn(4.4, res["materials_seen"])
        with self.assertRaises(api.ApiError):
            api.voxel_preview(self.doc, plane="q", path=path)
        with self.assertRaises(api.ApiError):
            api.voxel_preview(self.doc, path=os.path.join(self.tmp, "a.jpg"))

    def test_gui_only_functions_refuse_headless(self):
        if FreeCAD.GuiUp:
            self.skipTest("GUI is up")
        api.new_simulation(self.doc)
        with self.assertRaises(api.ApiError):
            api.screenshot(self.doc, path=os.path.join(self.tmp, "x.png"))
        with self.assertRaises(api.ApiError):
            api.cross_section(self.doc, on=True)


if __name__ == "__main__":
    unittest.main()
