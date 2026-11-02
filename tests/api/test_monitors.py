# -*- coding: utf-8 -*-
"""Tests for wavesim_gui.api_monitors (probes, snapshots, path monitors...)."""

import unittest

import FreeCAD

from _fixtures import DocTestCase
from wavesim_gui import api
from wavesim_gui import labels as labels_mod
from wavesim_gui import monitors


def _params(doc, **values):
    """A ``Params`` spreadsheet with the given aliases (storage units)."""
    sheet = doc.getObject("Params") or doc.addObject("Spreadsheet::Sheet",
                                                     "Params")
    for row, (name, value) in enumerate(sorted(values.items()), start=1):
        sheet.set("A{}".format(row), str(value))
        sheet.setAlias("A{}".format(row), name)
    doc.recompute()
    return sheet


class MonitorTestCase(DocTestCase):

    def assertVec(self, vec, xyz, places=6):
        for a, b in zip((vec.x, vec.y, vec.z), xyz):
            self.assertAlmostEqual(a, b, places=places)

    def assertInMonitors(self, sim, obj):
        self.assertIn(obj, monitors.monitors_group(sim).Group)


class ProbeTests(MonitorTestCase):

    def test_defaults_match_gui(self):
        sim = self.new_sim()
        gui = monitors.new_probe(sim)        # what Wavesim_AddProbe creates
        self.doc.recompute()
        probe = api.add_probe(sim)
        self.assertEqual(probe.WavesimType, "Probe")
        self.assertIsInstance(probe.Proxy, monitors.ProbeObject)
        self.assertEqual(probe.TypeId, gui.TypeId)
        self.assertEqual(probe.Component, gui.Component)
        self.assertEqual(probe.Component, "Ez")
        self.assertVec(probe.Position, (gui.Position.x, gui.Position.y,
                                        gui.Position.z))
        self.assertTrue(labels_mod.is_auto(probe.Label, "Probe (Ez)"))
        self.assertInMonitors(sim, probe)

    def test_arguments_and_expressions(self):
        sim = self.new_sim()
        _params(self.doc, PX=5)
        probe = api.add_probe(sim, position=["=PX", "2 mm", "0.1 cm"],
                              component="|E|")
        self.doc.recompute()
        self.assertVec(probe.Position, (5, 2, 1))
        self.assertEqual(probe.Component, monitors._E_MAG)
        self.assertEqual(monitors.probe_spec(probe, (0, 0, 0))["component"],
                         "|E|")
        self.assertEqual(probe.Label, "Probe ({})".format(monitors._E_MAG))
        exprs = dict((str(k), str(v)) for k, v in probe.ExpressionEngine)
        self.assertEqual(exprs.get(".Position.x"), "Params.PX")
        named = api.add_probe(sim, label="Rx", component="hy")
        self.assertEqual((named.Label, named.Component), ("Rx", "Hy"))

    def test_validation(self):
        sim = self.new_sim()
        n = len(self.doc.Objects)
        with self.assertRaises(api.ApiError):
            api.add_probe(sim, component="Exx")
        with self.assertRaises(api.ApiError):
            api.add_probe(sim, position=[1, 2])
        with self.assertRaises(api.ApiError):
            api.add_probe(sim, position=[1, 2, "3 parsecs"])
        with self.assertRaises(api.ApiError):
            api.add_probe(sim, label="Monitors")
        self.assertEqual(len(self.doc.Objects), n)


class SnapshotTests(MonitorTestCase):

    def test_defaults_match_gui(self):
        sim = self.new_sim()
        gui = monitors.new_snapshot(sim)
        self.doc.recompute()
        snap = api.add_snapshot(sim)
        self.assertIsInstance(snap.Proxy, monitors.SnapshotObject)
        self.assertEqual(snap.WavesimType, "Snapshot")
        for prop in ("Field", "Plane", "EveryNSteps"):
            self.assertEqual(getattr(snap, prop), getattr(gui, prop), prop)
        self.assertAlmostEqual(snap.Offset.Value, gui.Offset.Value)
        # The panel stores EveryNSteps' worth of time on OK.
        dt = monitors.snapshot_dt_s(sim)
        self.assertAlmostEqual(snap.RecordInterval,
                               20 * dt if dt > 0 else 0.0)
        self.assertEqual(monitors.snapshot_every_n(snap), 20)
        self.assertTrue(snap.Label.startswith("Snapshot (E XY @ z="))
        self.assertEqual(len(snap.Corners), 4)
        self.assertInMonitors(sim, snap)

    def test_arguments(self):
        sim = self.new_sim()
        _params(self.doc, OFF=7)
        snap = api.add_snapshot(sim, field="h", normal="x", offset="=OFF",
                                every_n_steps=7)
        self.doc.recompute()
        self.assertEqual((snap.Field, snap.Plane), ("H", "YZ"))
        self.assertAlmostEqual(snap.Offset.Value, 7.0)
        self.assertEqual((snap.EveryNSteps, snap.RecordInterval), (7, 0.0))
        self.assertEqual(monitors.snapshot_every_n(snap), 7)
        spec = monitors.snapshot_spec(snap, (0, 0, 0))
        self.assertEqual((spec["normal"], spec["field"]), ("x", "H"))
        self.assertAlmostEqual(spec["position"], 0.007)
        snap2 = api.add_snapshot(sim, label="S", field="S", normal="XZ",
                                 offset=1, record_interval="1 ps")
        self.assertEqual((snap2.Label, snap2.Plane, snap2.Field),
                         ("S", "XZ", "S"))
        self.assertAlmostEqual(snap2.RecordInterval, 1e-12)
        dt = monitors.snapshot_dt_s(sim)
        if dt > 0:
            self.assertEqual(snap2.EveryNSteps,
                             monitors.steps_for_interval(1e-12, dt))

    def test_validation(self):
        sim = self.new_sim()
        for kwargs in ({"field": "phi"}, {"normal": "w"},
                       {"every_n_steps": 0}, {"every_n_steps": 2.5},
                       {"record_interval": -1},
                       {"record_interval": "1 ps", "every_n_steps": 3},
                       {"offset": "3 Hz"}):
            with self.assertRaises(api.ApiError, msg=str(kwargs)):
                api.add_snapshot(sim, **kwargs)
        self.assertEqual(monitors.find_snapshots(sim), [])

    def test_electrostatic_fields(self):
        sim = self.new_sim(mode="electrostatic")
        snap = api.add_snapshot(sim, field="phi")
        self.assertEqual(snap.Field, "phi")
        self.assertTrue(snap.Label.startswith("Snapshot (phi XY"))
        with self.assertRaises(api.ApiError):
            api.add_snapshot(sim, field="H")


class RegionMonitorTests(MonitorTestCase):

    def test_energy(self):
        sim = self.new_sim()
        mon = api.add_energy_monitor(sim)
        self.assertIsInstance(mon.Proxy, monitors.EnergyObject)
        self.assertEqual((mon.RecordInterior, mon.RecordFullDomain),
                         (True, False))
        self.assertEqual(mon.Label, "Energy (excl. PML)")
        self.assertInMonitors(sim, mon)
        with self.assertRaises(api.ApiError):
            api.add_energy_monitor(sim)                  # singleton

    def test_energy_regions_and_gui_defaults(self):
        sim = self.new_sim()
        gui = monitors.new_energy_monitor(sim)
        self.assertEqual((gui.RecordInterior, gui.RecordFullDomain),
                         (True, False))
        self.doc.removeObject(gui.Name)
        with self.assertRaises(api.ApiError):
            api.add_energy_monitor(sim, interior=False, full=False)
        mon = api.add_energy_monitor(sim, full=True)
        self.assertEqual(mon.Label, "Energy (excl. + incl. PML)")
        self.assertEqual(monitors.energy_spec([mon]),
                         {"full": True, "interior": True})

    def test_dissipation(self):
        sim = self.new_sim()
        mon = api.add_dissipation_monitor(sim, label="Loss", interior=False,
                                          full=True)
        self.assertIsInstance(mon.Proxy, monitors.DissipationObject)
        self.assertEqual(mon.WavesimType, "DissipationMonitor")
        self.assertEqual((mon.Label, mon.RecordInterior, mon.RecordFullDomain),
                         ("Loss", False, True))
        with self.assertRaises(api.ApiError):
            api.add_dissipation_monitor(sim)


class PathMonitorTests(MonitorTestCase):

    def test_voltage_line_with_expressions(self):
        sim = self.new_sim()
        _params(self.doc, HGT=3)
        mon = api.add_voltage_monitor(sim, path=[[0, 0, 0], [0, 0, "=HGT"]])
        self.assertIsInstance(mon.Proxy, monitors.PathMonitorObject)
        self.assertEqual(mon.WavesimType, "VoltageMonitor")
        self.assertEqual(mon.Sketch.TypeId, "Part::Line")
        self.assertEqual(mon.Label,
                         "Voltage Monitor ({})".format(mon.Sketch.Label))
        self.assertAlmostEqual(mon.Sketch.Shape.Length, 3.0)
        self.assertInMonitors(sim, mon)
        pts = monitors._monitor_path_mm(mon, 0.5)
        self.assertVec(pts[0], (0, 0, 0))
        self.assertVec(pts[-1], (0, 0, 3))
        self.assertIn((0, 0, 3.0), [tuple(p) for p in
                                     monitors.path_monitor_points_mm(sim)])

    def test_matches_gui_drop(self):
        sim = self.new_sim()
        line = self.doc.addObject("Part::Line", "Gap")
        line.Z2 = 2
        self.doc.recompute()
        gui = monitors.new_path_monitor(sim, monitors._VOLTAGE_TYPE)
        self.assertEqual(gui.Label, "Voltage Monitor (no curve)")
        monitors.attach_path(gui, line)
        mon = api.add_voltage_monitor(sim, path="Gap")
        self.assertIs(mon.Sketch, line)
        self.assertTrue(labels_mod.is_auto(mon.Label, gui.Label))
        named = api.add_voltage_monitor(sim, label="V1",
                                        path=[[0, 0, 0], [1, 0, 0], [1, 1, 0]])
        self.assertEqual(named.Label, "V1")
        self.assertEqual(named.Sketch.TypeId, "Part::Polygon")
        self.assertFalse(named.Sketch.Close)
        self.assertEqual(named.Sketch.Label, "V1 Path")

    def test_voltage_validation(self):
        sim = self.new_sim()
        box = self.doc.addObject("Part::Box", "Box")
        self.doc.recompute()
        n = len(self.doc.Objects)
        for kwargs in ({}, {"path": [[0, 0, 0]]},
                       {"path": [[0, 0, 0], [0, 0, 0]]},
                       {"path": [[0, 0, 0], [1, 0, 0], [0, 0, 0]]},
                       {"path": [[0, 0, 0], [1, 0, 0], ["=H", 0, 0]]},
                       {"path": box.Name}, {"path": "Nope"}):
            with self.assertRaises(api.ApiError, msg=str(kwargs)):
                api.add_voltage_monitor(sim, **kwargs)
        self.assertEqual(len(self.doc.Objects), n)

    def test_current_circle_and_polygon(self):
        sim = self.new_sim()
        _params(self.doc, R=2)
        mon = api.add_current_monitor(
            sim, circle={"center": [1, 2, 3], "radius": "=R", "normal": "x"})
        self.assertEqual(mon.WavesimType, "CurrentMonitor")
        circ = mon.Sketch
        self.assertEqual(circ.TypeId, "Part::Circle")
        self.assertAlmostEqual(circ.Radius.Value, 2.0)
        self.assertVec(circ.Placement.Base, (1, 2, 3))
        self.assertVec(circ.Placement.Rotation.multVec(
            FreeCAD.Vector(0, 0, 1)), (1, 0, 0))
        self.assertTrue(circ.Shape.Edges[0].isClosed())
        poly = api.add_current_monitor(
            sim, path=[[0, 0, 0], [1, 0, 0], [1, 1, 0]])
        self.assertEqual(poly.Sketch.TypeId, "Part::Polygon")
        self.assertTrue(poly.Sketch.Close)
        self.assertEqual(len(poly.Sketch.Shape.Edges), 3)
        for kwargs in ({"path": [[0, 0, 0], [1, 0, 0]]},
                       {"path": [[0, 0, 0], [1, 0, 0], [1, 1, 0]],
                        "circle": {"center": [0, 0, 0], "radius": 1}},
                       {"circle": {"center": [0, 0, 0], "radius": 0}},
                       {"circle": {"center": [0, 0, 0]}}):
            with self.assertRaises(api.ApiError, msg=str(kwargs)):
                api.add_current_monitor(sim, **kwargs)
        volt = api.add_voltage_monitor(sim, path=[[0, 0, 0], [0, 0, 1]])
        with self.assertRaises(api.ApiError):   # a voltage path is open
            api.set_monitor_path(sim, volt, circle={"center": [0, 0, 0],
                                                    "radius": 1})

    def test_set_monitor_path(self):
        sim = self.new_sim()
        mon = api.add_voltage_monitor(sim, path=[[0, 0, 0], [0, 0, 1]])
        api.set_monitor_path(sim, mon.Name, path=[[0, 0, 0], [0, 0, 5]])
        self.assertAlmostEqual(mon.Sketch.Shape.Length, 5.0)
        self.assertEqual(mon.Label,
                         "Voltage Monitor ({})".format(mon.Sketch.Label))
        with self.assertRaises(api.ApiError):
            api.set_monitor_path(sim, "Monitors", path=[[0, 0, 0], [1, 0, 0]])


class FieldLineTests(MonitorTestCase):

    def test_fdtd_rejected(self):
        sim = self.new_sim()
        with self.assertRaises(api.ApiError):
            api.add_field_line_monitor(sim, path=[[0, 0, 0], [1, 0, 0]])

    def test_create_and_vertices(self):
        sim = self.new_sim(mode="electrostatic")
        mon = api.add_field_line_monitor(
            sim, path=[[0, 0, 0], [4, 0, 0], [4, 4, 0]], field="phi",
            start_vertex=0, end_vertex=2)
        self.assertIsInstance(mon.Proxy, monitors.FieldLineObject)
        self.assertEqual(mon.WavesimType, "FieldLineMonitor")
        self.assertEqual((mon.Field, mon.StartVertex, mon.EndVertex,
                          mon.Reversed), ("phi", 0, -1, False))
        self.assertEqual(mon.Label, "Field Along Curve (phi, {})".format(
            mon.Sketch.Label))
        self.assertGreater(len(mon.PathPoints), 2)
        sampled = monitors.sample_field_line(mon, max_samples=10)
        self.assertAlmostEqual(sampled["length"], 8.0)
        back = api.add_field_line_monitor(
            sim, path=[[0, 0, 0], [4, 0, 0], [4, 4, 0]], start_vertex=2,
            end_vertex=1)
        self.assertEqual((back.Field, back.StartVertex, back.EndVertex),
                         ("E", 2, 1))
        loop = api.add_field_line_monitor(
            sim, circle={"center": [0, 0, 0], "radius": 1}, reversed=True)
        self.assertTrue(loop.Reversed)
        for kwargs in ({"start_vertex": 3}, {"end_vertex": 5},
                       {"reversed": True}, {"start_vertex": 2},
                       {"field": "H"}, {"end_vertex": -2}):
            with self.assertRaises(api.ApiError, msg=str(kwargs)):
                api.add_field_line_monitor(
                    sim, path=[[0, 0, 0], [4, 0, 0], [4, 4, 0]], **kwargs)


class JobExportTests(MonitorTestCase):

    def test_job_builder_picks_monitors_up(self):
        sim = self.new_sim()
        box = self.doc.addObject("Part::Box", "Box")
        box.Length = box.Width = box.Height = 4
        self.doc.recompute()
        api.assign_material(sim, "Box", "PEC")
        probe = api.add_probe(sim, label="P1", position=[2, 2, 6])
        snap = api.add_snapshot(sim, label="S1", offset=2, every_n_steps=5)
        volt = api.add_voltage_monitor(sim, label="V1",
                                       path=[[2, 2, 4], [2, 2, 6]])
        cur = api.add_current_monitor(
            sim, label="I1", circle={"center": [2, 2, 2], "radius": 3})
        api.add_energy_monitor(sim)
        try:
            from wavesim_gui import voxelize
            spec, _arrays = voxelize.build_job_from_document(self.doc,
                                                             steps=10)
        except ImportError as exc:  # pragma: no cover - environment specific
            self.skipTest("voxelizing headless is impossible here: {}".format(
                exc))
        self.assertIsNotNone(spec, "no job built")
        mons = spec["monitors"]
        self.assertEqual([p["name"] for p in mons["probes"]], [probe.Label])
        self.assertEqual(mons["probes"][0]["component"], "Ez")
        self.assertEqual([s["name"] for s in mons["snapshots"]], [snap.Label])
        self.assertEqual(mons["snapshots"][0]["every_N_steps"], 5)
        self.assertEqual([v["name"] for v in mons["voltages"]], [volt.Label])
        path = mons["voltages"][0]["path"]
        self.assertGreaterEqual(len(path), 2)
        dz = path[-1][2] - path[0][2]
        self.assertAlmostEqual(dz, 0.002, places=6)
        self.assertEqual([i["name"] for i in mons["currents"]], [cur.Label])
        self.assertGreater(len(mons["currents"][0]["path"]), 4)
        self.assertTrue(mons["energy"]["interior"])


if __name__ == "__main__":
    unittest.main()
