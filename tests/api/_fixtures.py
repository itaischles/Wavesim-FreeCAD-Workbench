# -*- coding: utf-8 -*-
"""Shared fixtures for the scripting-API tests (run under FreeCADCmd)."""

import unittest

import FreeCAD


class DocTestCase(unittest.TestCase):
    """Gives each test a fresh, active document and closes it afterwards."""

    def setUp(self):
        self.doc = FreeCAD.newDocument("ApiTest")
        FreeCAD.setActiveDocument(self.doc.Name)

    def tearDown(self):
        try:
            FreeCAD.closeDocument(self.doc.Name)
        except Exception:
            pass

    def new_sim(self, mode="fdtd"):
        from wavesim_gui import api
        return api.new_simulation(self.doc, mode=mode)
