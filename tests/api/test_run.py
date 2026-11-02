# -*- coding: utf-8 -*-
"""Tests for wavesim_gui.api_run (export a job, run it, load results)."""

import os
import shutil
import subprocess
import tempfile
import unittest

from _fixtures import DocTestCase
from wavesim_gui import api

import wavesim_settings


class RunTests(DocTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="wavesim_api_run_")

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _model(self):
        sim = api.new_simulation(self.doc)
        box = self.doc.addObject("Part::Box", "Box")
        box.Length = box.Width = box.Height = 4
        box.Label = "Block"
        self.doc.recompute()
        api.create_material(sim, label="Diel", eps=2.0, bodies=["Block"])
        api.set_domain(sim, spacing=4)
        api.set_mesh(sim, cell_size=1.0, nonuniform=False)
        return sim

    def test_export_requires_geometry(self):
        api.new_simulation(self.doc)
        with self.assertRaises(api.ApiError):
            api.export_job(self.doc, workdir=self.tmp)
        with self.assertRaises(api.ApiError):
            api.export_job(self.doc)

    def test_export_run_load(self):
        self._model()
        calls = []
        info = api.export_job(self.doc, workdir=self.tmp, steps=20,
                              backend="numpy",
                              progress=lambda d, t: calls.append((d, t)))
        self.assertTrue(os.path.isfile(info["job"]))
        self.assertTrue(os.path.isfile(info["materials"]))
        self.assertEqual(info["steps"], 20)
        self.assertEqual(info["backend"], "numpy")
        self.assertGreater(info["grid"]["Nx"], 0)
        self.assertTrue(any("absorber" in w for w in info["warnings"]))
        python = wavesim_settings.get_wavesim_python()
        if not python or not os.path.isfile(python):
            self.skipTest("Wavesim Python not configured")
        runner = api.runner_path()["runner"]
        proc = subprocess.run([python, runner, self.tmp],
                              capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("DONE", proc.stdout)
        res = api.load_results(self.doc, workdir=self.tmp)
        self.assertEqual(res["results_group"], "Results")
        self.assertTrue(res["summary"]["ok"])
        # Loading results must not change the model fingerprint.
        fp = api.model_fingerprint(self.doc)
        api.load_results(self.doc, workdir=self.tmp)
        self.assertEqual(fp, api.model_fingerprint(self.doc))


if __name__ == "__main__":
    unittest.main()
