# -*- coding: utf-8 -*-
"""Tests for wavesim_gui.api_excitation (sources, ports, potentials, matrix)."""

import json
import os
import shutil
import unittest

import FreeCAD

from _fixtures import DocTestCase
from wavesim_gui import api
from wavesim_gui import api_common as c
from wavesim_gui import domain as domain_mod
from wavesim_gui import excitation as exc

_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "_out_excitation")


class ExcitationTestCase(DocTestCase):

    def box(self, label, pos, size):
        obj = self.doc.addObject("Part::Box", label)
        obj.Label = label
        obj.Length, obj.Width, obj.Height = size
        obj.Placement.Base = FreeCAD.Vector(*pos)
        self.doc.recompute()
        return obj

    def gui_create(self, factory, *args):
        """Create through the command's shared helper, as Activated() does."""
        self.doc.openTransaction("gui")
        obj = factory(self.doc, *args)
        self.doc.commitTransaction()
        self.doc.recompute()
        return obj

    def gap_pair(self, sim):
        """Two PEC plates with a 1 mm gap between z=1 and z=2."""
        bottom = self.box("Bottom", (0, 0, 0), (10, 10, 1))
        top = self.box("Top", (0, 0, 2), (10, 10, 1))
        api.assign_material(sim, ["Bottom", "Top"], "PEC")
        return bottom, top

    def rods(self, sim):
        """Two PEC rods along z (a two-wire line), RodA the larger, inside a
        vacuum block that keeps them off the solve region's edge (so neither
        is the grounded reference)."""
        a = self.box("RodA", (0, 0, 0), (3, 3, 20))
        b = self.box("RodB", (8, 0, 0), (2, 2, 20))
        api.assign_material(sim, ["RodA", "RodB"], "PEC")
        self.box("Air", (-10, -10, 0), (40, 30, 20))
        api.create_material(sim, label="AirMat", eps=1.0, bodies=["Air"])
        return a, b


class SourceTests(ExcitationTestCase):

    def test_add_source_matches_gui_defaults(self):
        from wavesim_gui import source as source_mod

        sim = self.new_sim()
        src = api.add_source(sim)
        gui = self.gui_create(source_mod.create_source, sim)
        for prop in ("Component", "Position", "Excitation", "Fmax",
                     "Amplitude"):
            self.assertEqual(getattr(src, prop), getattr(gui, prop), prop)
        self.assertTrue(src.Label.startswith("Source (Ez, Gaussian Pulse"))
        self.assertIn(src, source_mod.sources_group(sim).Group)
        self.assertEqual(source_mod.find_sources(sim)[0], src)

    def test_add_source_settings(self):
        sim = self.new_sim()
        src = api.add_source(
            sim, component="hx", position=[1, "2 mm", "0.3 cm"],
            excitation={"type": "sinusoid", "frequency": "5 GHz",
                        "phase": "90 deg", "ramp_cycles": 2})
        self.assertEqual(src.Component, "Hx")
        self.assertEqual(src.Position, FreeCAD.Vector(1, 2, 3))
        self.assertEqual(src.Excitation, exc.LABELS[exc.SINUSOID])
        self.assertAlmostEqual(src.Frequency, 5e9)
        self.assertAlmostEqual(src.PhaseDeg, 90.0)
        self.assertAlmostEqual(src.RampCycles, 2.0)
        spec = exc.spec_from_object(src)
        self.assertEqual(spec["type"], "sinusoid")
        self.assertTrue(src.Label.startswith("Source (Hx, Sinusoid"))
        named = api.add_source(sim, label="Feed",
                               excitation={"type": "Rectangular Pulse",
                                           "rise_time": "20 ps"})
        self.assertEqual(named.Label, "Feed")
        self.assertAlmostEqual(named.RiseTime, 20e-12)

    def test_add_source_validation(self):
        sim = self.new_sim()
        n = len(self.doc.Objects)
        bad = [
            dict(component="Qx"),
            dict(position=[0, 0]),
            dict(position=[0, 0, 2e6]),
            dict(excitation={"type": "gaussian", "frequency": "1 GHz"}),
            dict(excitation={"type": "triangle"}),
            dict(excitation={"type": "gaussian", "fmax": 0}),
            dict(excitation={"type": "sine", "phase": 400}),
            dict(excitation={"type": "gaussian", "fmax": "3 mm"}),
            dict(excitation={"type": "gaussian", "colour": 1}),
        ]
        for kwargs in bad:
            with self.assertRaises(api.ApiError, msg=str(kwargs)):
                api.add_source(sim, **kwargs)
        self.assertEqual(len(self.doc.Objects), n)
        api.add_source(sim, label="S1")
        with self.assertRaises(api.ApiError):
            api.add_source(sim, label="S1")

    def test_electrostatic_refuses_fdtd_objects(self):
        sim = self.new_sim(mode="electrostatic")
        with self.assertRaises(api.ApiError):
            api.add_source(sim)
        with self.assertRaises(api.ApiError):
            api.add_gaussian_beam(sim)
        with self.assertRaises(api.ApiError):
            api.add_lumped_port(sim, points=[[0, 0, 0], [0, 0, 1]])
        with self.assertRaises(api.ApiError):
            api.add_modal_port(sim)
        with self.assertRaises(api.ApiError):
            api.configure_port_matrix(sim)


class BeamTests(ExcitationTestCase):

    def test_add_gaussian_beam(self):
        from wavesim_gui import gaussian_beam as gb

        sim = self.new_sim()
        gui = self.gui_create(gb.create_gaussian_beam, sim)
        default = api.add_gaussian_beam(sim)
        for prop in ("Face", "Angle", "Waist", "Directional", "Excitation"):
            self.assertEqual(getattr(default, prop), getattr(gui, prop), prop)
        beam = api.add_gaussian_beam(
            sim, face="x0", angle={"value": 90, "unit": "deg"}, waist="2 cm",
            directional=False,
            excitation={"type": "gaussian_sine", "frequency": "10 GHz",
                        "fmax": "2 GHz"})
        self.assertEqual(beam.Face, "x0")
        self.assertAlmostEqual(float(beam.Angle), 90.0)
        self.assertAlmostEqual(float(beam.Waist), 20.0)
        self.assertFalse(beam.Directional)
        self.assertEqual(beam.Excitation, exc.LABELS[exc.GAUSSIAN_SINE])
        self.assertAlmostEqual(beam.Fmax, 2e9)
        dom = domain_mod.find_domain(sim)
        self.assertEqual(str(getattr(dom, domain_mod._FACE_TO_PROP["x0"])),
                         "PML")
        self.assertTrue(beam.Label.startswith("Gaussian Beam (x0, 90°, w0 20 mm"))
        for kwargs in (dict(face="w0"), dict(angle=400), dict(waist=-1),
                       dict(directional="yes")):
            with self.assertRaises(api.ApiError, msg=str(kwargs)):
                api.add_gaussian_beam(sim, **kwargs)


class LumpedPortTests(ExcitationTestCase):

    def test_add_lumped_port_from_faces_matches_gui(self):
        from wavesim_gui import lumped_port as lumped_mod

        sim = self.new_sim()
        bottom, top = self.gap_pair(sim)
        port = api.add_lumped_port(
            sim, plus={"object": "Top", "face": {"normal": "-z"}},
            minus={"object": "Bottom", "face": {"normal": "+z"}},
            resistance="75 ohm", capacitance="1 pF", topology="parallel",
            drive="voltage",
            excitation={"type": "gaussian", "fmax": "10 GHz"})
        plus_sub = c.select_subshapes(top.Shape, {"normal": "-z"}, "face")[0][0]
        minus_sub = c.select_subshapes(bottom.Shape, {"normal": "+z"},
                                       "face")[0][0]
        self.assertEqual(port.TerminalPlus[0], top)
        self.assertEqual(list(port.TerminalPlus[1]), [plus_sub])
        self.assertEqual(list(port.TerminalMinus[1]), [minus_sub])
        self.assertTrue(port.P0.isEqual(FreeCAD.Vector(5, 5, 2), 1e-6))
        self.assertTrue(port.P1.isEqual(FreeCAD.Vector(5, 5, 1), 1e-6))
        self.assertAlmostEqual(port.Resistance, 75.0)
        self.assertTrue(port.UseCapacitance)
        self.assertAlmostEqual(port.Capacitance, 1e-12)
        self.assertFalse(port.UseInductance)
        self.assertEqual(port.Topology, "Parallel")
        self.assertEqual(port.Drive, lumped_mod.DRIVE_VOLTAGE)
        self.assertAlmostEqual(port.Fmax, 10e9)
        self.assertEqual(port.Label, "Lumped Port (R 75 ohm + C 1 pF "
                                     "(parallel), voltage drive)")

        gui = self.gui_create(lumped_mod.create_lumped_port, sim,
                              (top, [plus_sub]), (bottom, [minus_sub]))
        self.assertEqual(gui.P0, port.P0)
        self.assertEqual(gui.P1, port.P1)
        self.assertEqual(gui.Label, "Lumped Port (R 50 ohm)")
        default = api.add_lumped_port(
            sim, plus={"object": "Top", "face": {"normal": "-z"}},
            minus={"object": "Bottom", "face": {"normal": "+z"}})
        for prop in ("UseResistance", "Resistance", "UseInductance",
                     "UseCapacitance", "Topology", "Drive"):
            self.assertEqual(getattr(default, prop), getattr(gui, prop), prop)

    def test_add_lumped_port_points_and_vertices(self):
        sim = self.new_sim()
        self.gap_pair(sim)
        port = api.add_lumped_port(sim, points=[[1, 1, 1], [1, 1, 2]],
                                   reverse=True, resistance=None,
                                   inductance="2 nH")
        self.assertTrue(port.P0.isEqual(FreeCAD.Vector(1, 1, 2), 1e-6))
        self.assertTrue(port.P1.isEqual(FreeCAD.Vector(1, 1, 1), 1e-6))
        self.assertEqual(port.TerminalPlus[0].TypeId, "Part::Line")
        self.assertFalse(port.UseResistance)
        self.assertAlmostEqual(port.Inductance, 2e-9)
        vport = api.add_lumped_port(
            sim, plus={"object": "Top", "vertex": {"near_point": [0, 0, 2]}},
            minus={"object": "Bottom", "vertex": {"near_point": [0, 0, 1]}})
        self.assertTrue(vport.P0.isEqual(FreeCAD.Vector(0, 0, 2), 1e-6))
        self.assertTrue(vport.P1.isEqual(FreeCAD.Vector(0, 0, 1), 1e-6))

    def test_add_lumped_port_validation(self):
        sim = self.new_sim()
        self.gap_pair(sim)
        n = len(self.doc.Objects)
        faces = dict(plus={"object": "Top", "face": {"normal": "-z"}},
                     minus={"object": "Bottom", "face": {"normal": "+z"}})
        bad = [
            dict(),
            dict(minus=faces["minus"]),
            dict(points=[[0, 0, 0], [0, 0, 0]]),
            dict(points=[[0, 0, 0], [0, 0, 1]], plus="Top"),
            dict(faces, resistance=0),
            dict(faces, excitation={"type": "gaussian"}),
            dict(faces, resistance=None),
            dict(faces, drive="sideways"),
            dict(faces, topology="star"),
            dict(plus={"object": "Top", "face": {"parallel_to": "z"}},
                 minus=faces["minus"]),
            dict(plus={"object": "Top", "face": 3}, minus=faces["minus"]),
            dict(plus={"object": "Top", "sub": "Face1"}, minus=faces["minus"]),
            dict(plus={"object": "Nope", "face": {"normal": "-z"}},
                 minus=faces["minus"]),
        ]
        for kwargs in bad:
            with self.assertRaises(api.ApiError, msg=str(kwargs)):
                api.add_lumped_port(sim, **kwargs)
        self.assertEqual(len(self.doc.Objects), n)


class ModalPortTests(ExcitationTestCase):

    def test_add_modal_port_auto_table_matches_gui_defaults(self):
        from wavesim_gui import modal_port as modal_mod

        sim = self.new_sim()
        rod_a, rod_b = self.rods(sim)
        port = api.add_modal_port(
            sim, excitation={"type": "gaussian", "fmax": "8 GHz"})
        self.assertEqual(port.Face, "z0")
        self.assertEqual(list(port.Conductors), [rod_a, rod_b])
        self.assertEqual(list(port.Energized), [True, False])
        self.assertEqual(port.ExcitationMode, modal_mod.MODE_WAVEFORM)
        self.assertAlmostEqual(port.Fmax, 8e9)
        self.assertAlmostEqual(port.Fmax2, 8e9)
        self.assertIn("z0", domain_mod.modal_port_faces(sim))
        self.assertTrue(port.Label.startswith("Modal Port (z0, RodA"))
        signal, _ref, _scan = modal_mod.scan_port_plane(sim, "z0")
        rows = modal_mod.default_drive_rows(signal)
        self.assertEqual([r["body"] for r in rows], list(port.Conductors))
        specs = modal_mod.modal_port_specs(port, (0.0, 0.0, 0.0))
        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[1]["excitation"]["amplitude"], 0.0)

        gui = self.gui_create(modal_mod.create_modal_port, sim)
        self.assertEqual(gui.Face, "z0")
        self.assertEqual(list(gui.Conductors), [])

    def test_add_modal_port_explicit_rows_and_spice(self):
        from wavesim_gui import modal_port as modal_mod

        sim = self.new_sim()
        rod_a, rod_b = self.rods(sim)
        port = api.add_modal_port(
            sim, label="P1",
            conductors=[{"body": "RodB", "face": {"normal": "-z"},
                         "excitation": {"type": "sine",
                                        "frequency": "3 GHz"}},
                        {"body": "RodA", "drive": False}])
        self.assertEqual(port.Label, "P1")
        self.assertEqual(list(port.Conductors), [rod_b, rod_a])
        self.assertEqual(list(port.ConductorFaces), ["", ""])
        self.assertEqual(list(port.Energized), [True, False])
        self.assertEqual(port.Excitation, exc.LABELS[exc.SINE])
        self.assertAlmostEqual(port.Frequency, 3e9)
        self.assertEqual(port.Excitation2, exc.LABELS[exc.GAUSSIAN])

        spice = api.add_modal_port(sim, face="z1", drive_mode="spice",
                                   netlist="C:/circuits/load.cir",
                                   node_plus="in", conductor_index=1,
                                   fields="E")
        self.assertEqual(spice.ExcitationMode, modal_mod.MODE_SPICE)
        self.assertEqual(spice.Netlist, "C:/circuits/load.cir")
        self.assertEqual(spice.NodePlus, "in")
        self.assertEqual(spice.Fields, "E")
        self.assertEqual(spice.Conductor, 1)
        dom = domain_mod.find_domain(sim)
        self.assertEqual(str(getattr(dom, domain_mod._FACE_TO_PROP["z1"])),
                         "PML")
        self.assertEqual(spice.Label, "Modal Port (z1, load.cir)")

    def test_add_modal_port_validation(self):
        sim = self.new_sim()
        self.rods(sim)
        self.box("Dielectric", (20, 0, 0), (2, 2, 20))
        n = len(self.doc.Objects)
        bad = [
            dict(face="q1"),
            dict(drive_mode="laser"),
            dict(conductors=["Dielectric"]),
            dict(conductors=["RodA", "RodA"]),
            dict(conductors="RodA"),
            dict(drive_mode="spice"),
            dict(drive_mode="spice", netlist="a.cir",
                 excitation={"type": "gaussian"}),
            dict(netlist="a.cir"),
            dict(conductor_index=-1),
            dict(conductors=[{"body": "RodA", "colour": 1}]),
        ]
        for kwargs in bad:
            with self.assertRaises(api.ApiError, msg=str(kwargs)):
                api.add_modal_port(sim, **kwargs)
        self.assertEqual(len(self.doc.Objects), n)


class SpicePortTests(ExcitationTestCase):

    def test_spice_line_and_tem_ports(self):
        from wavesim_gui import spice_port as spice_mod

        sim = self.new_sim()
        self.gap_pair(sim)
        gui = self.gui_create(spice_mod.create_spice_line_port, sim)
        self.assertIsNone(gui.Sketch)
        line = api.add_spice_line_port(sim, points=[[5, 5, 2], [5, 5, 1]],
                                       netlist="c:/n/diode.cir",
                                       invert_port_current=True)
        self.assertTrue(line.P0.isEqual(FreeCAD.Vector(5, 5, 2), 1e-6))
        self.assertTrue(line.InvertPortCurrent)
        self.assertEqual(line.Label, "SPICE Line Port (diode.cir, port1p/0)")
        again = api.add_spice_line_port(sim, curve=line.Sketch.Label,
                                        netlist="c:/n/diode.cir")
        self.assertIs(again.Sketch, line.Sketch)
        tem = api.add_spice_tem_port(sim, face="y0", fields="E",
                                     netlist="c:/n/tl.cir",
                                     bounds={"object": "Top",
                                             "face": {"normal": "-y"}})
        self.assertEqual(tem.Face, "y0")
        self.assertEqual(tem.Fields, "E")
        self.assertEqual(tem.BoundsSel[0].Label, "Top")
        dom = domain_mod.find_domain(sim)
        self.assertEqual(str(getattr(dom, domain_mod._FACE_TO_PROP["y0"])),
                         "PML")
        for kwargs in (dict(netlist="x.cir"),
                       dict(curve="Top", netlist="x.cir"),
                       dict(points=[[0, 0, 0], [0, 0, 1]])):
            with self.assertRaises(api.ApiError, msg=str(kwargs)):
                api.add_spice_line_port(sim, **kwargs)
        with self.assertRaises(api.ApiError):
            api.add_spice_tem_port(sim, netlist="")


class SetExcitationTests(ExcitationTestCase):

    def test_set_excitation(self):
        sim = self.new_sim()
        self.rods(sim)
        src = api.add_source(sim)
        api.set_excitation(sim, obj=src.Label,
                           excitation={"type": "sine", "frequency": "2 GHz"})
        self.assertEqual(src.Excitation, exc.LABELS[exc.SINE])
        self.assertTrue(src.Label.startswith("Source (Ez, Sine Wave @ 2"))
        api.set_excitation(sim, obj=src, excitation={"amplitude": 3})
        self.assertEqual(src.Excitation, exc.LABELS[exc.SINE])
        self.assertAlmostEqual(src.Amplitude, 3.0)
        port = api.add_modal_port(sim)
        api.set_excitation(sim, obj=port, index=1,
                           excitation={"type": "rect", "flat_time": "1 ns"})
        self.assertEqual(port.Excitation2, exc.LABELS[exc.RECTANGULAR])
        self.assertAlmostEqual(port.FlatTime2, 1e-9)
        with self.assertRaises(api.ApiError):
            api.set_excitation(sim, obj=port, index=2,
                               excitation={"amplitude": 1})
        with self.assertRaises(api.ApiError):
            api.set_excitation(sim, obj=src, excitation={"fmax": "1 GHz"})
        with self.assertRaises(api.ApiError):
            api.set_excitation(sim, obj="RodA", excitation={"amplitude": 1})
        line = api.add_spice_line_port(sim, points=[[0, 0, 0], [0, 0, 1]],
                                       netlist="x.cir")
        with self.assertRaises(api.ApiError):
            api.set_excitation(sim, obj=line, excitation={"amplitude": 1})


class PotentialTests(ExcitationTestCase):

    def test_set_potential(self):
        from wavesim_gui import commands
        from wavesim_gui import materials as materials_mod
        from wavesim_gui import potentials as pot_mod

        sim = self.new_sim(mode="electrostatic")
        a = self.box("A", (0, 0, 0), (1, 1, 1))
        b = self.box("B", (1, 0, 0), (1, 1, 1))
        self.box("C", (5, 0, 0), (1, 1, 1))
        self.box("Diel", (9, 0, 0), (1, 1, 1))
        api.assign_material(sim, ["A", "B", "C"], "PEC")
        res = api.set_potential(sim, body="A", volts="5 V")
        self.assertEqual(res["mode"], materials_mod.MODE_POTENTIAL)
        self.assertEqual(res["held_at"], "5 V")
        self.assertEqual(res["conflicts"], [])
        self.assertAlmostEqual(materials_mod.body_potential(a), 5.0)
        entries, _groups = pot_mod.resolve(sim)
        by = {e["body"].Label: e for e in entries}
        self.assertEqual(by["B"]["origin"], pot_mod.ORIGIN_INHERITED)
        self.assertEqual(by["B"]["value"], 5.0)

        res = api.set_potential(sim, body="C", mode="floating",
                                charge="5 pC", unset_default="float")
        self.assertAlmostEqual(res["charge_C"], 5e-12)
        self.assertEqual(commands.unset_conductor_mode(sim),
                         commands.UNSET_FLOAT)
        res = api.set_potential(sim, body="B", volts=3)
        self.assertEqual(len(res["conflicts"]), 1)
        res = api.set_potential(sim, body="B", mode="default")
        self.assertEqual(res["volts"], 3.0)
        self.assertEqual(res["conflicts"], [])

        # Same storage as the panel's OK.
        pot_mod.write_conditions(sim, [(b, materials_mod.MODE_POTENTIAL, 5.0,
                                        None)], commands.UNSET_GROUND)
        self.assertEqual(materials_mod.conductor_mode(b),
                         materials_mod.MODE_POTENTIAL)

        for kwargs in (dict(body="Diel"), dict(body="A", mode="hot"),
                       dict(body="A", charge="3 parsec"),
                       dict(body="A", unset_default="maybe"),
                       dict(body="A", volts="3 A")):
            with self.assertRaises(api.ApiError, msg=str(kwargs)):
                api.set_potential(sim, **kwargs)

    def test_set_potential_refused_in_fdtd(self):
        sim = self.new_sim()
        self.box("A", (0, 0, 0), (1, 1, 1))
        api.assign_material(sim, ["A"], "PEC")
        with self.assertRaises(api.ApiError):
            api.set_potential(sim, body="A", volts=1)


class PortMatrixTests(ExcitationTestCase):

    def two_ports(self, sim):
        self.gap_pair(sim)
        p1 = api.add_lumped_port(sim, label="P1", points=[[2, 2, 1], [2, 2, 2]],
                                 drive="voltage",
                                 excitation={"type": "gaussian",
                                             "fmax": "20 GHz"})
        p2 = api.add_lumped_port(sim, label="P2",
                                 points=[[8, 8, 1], [8, 8, 2]])
        return p1, p2

    def test_configure_port_matrix(self):
        from wavesim_gui import portmatrix as pm

        sim = self.new_sim()
        self.two_ports(sim)
        plan = api.configure_port_matrix(sim)
        self.assertEqual([p["name"] for p in plan["ports"]], ["P1", "P2"])
        self.assertEqual(plan["runs"], 2)
        self.assertEqual(plan["excitation"]["type"], "gaussian")
        self.assertEqual([p.name for p in pm.document_ports(sim)],
                         [p["name"] for p in plan["available"]])
        self.assertEqual(api.port_matrix_plan(sim, ["P2", "P1"])["ports"][0]
                         ["name"], "P2")
        for ports in (["P1"], ["P1", "Nope"], ["P1", "P1"], "P1"):
            with self.assertRaises(api.ApiError, msg=str(ports)):
                api.configure_port_matrix(sim, ports=ports)
        api.add_spice_line_port(sim, points=[[5, 5, 1], [5, 5, 2]],
                                netlist="x.cir")
        with self.assertRaises(api.ApiError):
            api.configure_port_matrix(sim)

    def test_export_and_load_port_matrix(self):
        from wavesim_gui import portmatrix as pm

        sim = self.new_sim()
        self.two_ports(sim)
        shutil.rmtree(_OUT, ignore_errors=True)
        try:
            out = api.export_port_matrix_jobs(sim, root_dir=_OUT, steps=7)
            self.assertEqual(out["root_dir"], os.path.abspath(_OUT))
            self.assertEqual([d["port"] for d in out["drives"]], ["P1", "P2"])
            for k, drive in enumerate(out["drives"]):
                self.assertEqual(os.path.basename(drive["workdir"]),
                                 pm.drive_dir_name(k))
                self.assertTrue(os.path.isfile(
                    os.path.join(drive["workdir"], "materials.npz")))
                with open(drive["job"], encoding="utf-8") as handle:
                    job = json.load(handle)
                self.assertEqual(job["steps"], 7)
                amps = [(p.get("excitation") or {}).get("amplitude", 0.0)
                        for p in job["lumped_ports"]]
                self.assertEqual([a != 0.0 for a in amps],
                                 [i == k for i in range(2)])
                self.assertEqual(job["lumped_ports"][k]["drive"], "voltage")
            # Drive selection is recoverable from the jobs alone.
            from wavesim_gui import api_excitation as api_exc
            self.assertEqual([p.name for p in api_exc._infer_ports(_OUT)],
                             ["P1", "P2"])
            # Nothing has run yet: loading reports the incomplete sweep.
            with self.assertRaises(api.ApiError):
                api.load_port_matrix_results(sim, root_dir=_OUT)
            with self.assertRaises(api.ApiError):
                api.export_port_matrix_jobs(sim, root_dir=_OUT, steps=0)
        finally:
            shutil.rmtree(_OUT, ignore_errors=True)
        with self.assertRaises(api.ApiError):
            api.load_port_matrix_results(sim, root_dir=_OUT)


if __name__ == "__main__":
    unittest.main()
