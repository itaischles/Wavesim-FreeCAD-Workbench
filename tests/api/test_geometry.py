# -*- coding: utf-8 -*-
"""Tests for wavesim_gui.api_geometry (Part-based parametric geometry)."""

import math
import os
import shutil
import tempfile
import unittest

import FreeCAD

from _fixtures import DocTestCase
from wavesim_gui import api
from wavesim_gui import api_common as c


class GeometryTests(DocTestCase):

    def params(self, **values):
        sheet = self.doc.getObject("Params") or self.doc.addObject(
            "Spreadsheet::Sheet", "Params")
        for row, (name, value) in enumerate(sorted(values.items()), 1):
            sheet.set("A{}".format(row), str(value))
            sheet.setAlias("A{}".format(row), name)
        self.doc.recompute()
        return sheet

    def set_param(self, sheet, name, value):
        sheet.set(sheet.getCellFromAlias(name), str(value))
        self.doc.recompute()

    # -- registration -------------------------------------------------------- #

    def test_all_registered(self):
        from wavesim_gui import api_geometry
        reg = c.registered_functions()
        for name in api_geometry.__all__:
            self.assertIn(name, reg)
            self.assertEqual(reg[name][1], "geometry")
            self.assertIs(getattr(api, name), reg[name][0])
            self.assertTrue(reg[name][0].__doc__)

    # -- primitives ----------------------------------------------------------- #

    def test_box_units_and_placement(self):
        box = api.create_box(self.doc, label="Sub", length=10,
                             width="2 cm", height={"value": 1.6,
                                                   "unit": "mm"},
                             position=[1, 2, 3])
        self.assertEqual(box.TypeId, "Part::Box")
        self.assertEqual(box.Label, "Sub")
        self.assertAlmostEqual(box.Width.Value, 20.0)
        self.assertAlmostEqual(box.Shape.Volume, 10 * 20 * 1.6, places=6)
        self.assertEqual(list(box.Placement.Base), [1, 2, 3])
        with self.assertRaises(api.ApiError):
            api.create_box(self.doc, label="Sub", length=1, width=1, height=1)
        with self.assertRaises(api.ApiError):
            api.create_box(self.doc, label="Neg", length=-1, width=1,
                           height=1)
        with self.assertRaises(api.ApiError):
            api.create_box(self.doc, label="", length=1, width=1, height=1)
        self.assertEqual(self.doc.getObjectsByLabel("Neg"), [])

    def test_box_rotation(self):
        box = api.create_box(self.doc, label="R", length=10, width=1,
                             height=1, rotation={"axis": "z", "angle": 90})
        bb = box.Shape.BoundBox
        self.assertAlmostEqual(bb.YLength, 10, places=6)
        self.assertAlmostEqual(bb.XLength, 1, places=6)

    def test_param_binding(self):
        sheet = self.params(L=10, HALF=4, ANG=30, OFF=2)
        box = api.create_box(self.doc, label="B", length="=L",
                             width="=L/2", height=1,
                             position=["=OFF", 0, "=-OFF"],
                             rotation={"axis": "x", "angle": "=ANG"})
        self.assertAlmostEqual(box.Length.Value, 10)
        self.assertAlmostEqual(box.Width.Value, 5)
        self.assertAlmostEqual(box.Placement.Base.x, 2)
        self.assertAlmostEqual(box.Placement.Base.z, -2)
        self.assertAlmostEqual(math.degrees(box.Placement.Rotation.Angle), 30)
        self.set_param(sheet, "L", 20)
        self.set_param(sheet, "OFF", 5)
        self.set_param(sheet, "ANG", 45)
        self.assertAlmostEqual(box.Length.Value, 20)
        self.assertAlmostEqual(box.Width.Value, 10)
        self.assertAlmostEqual(box.Placement.Base.x, 5)
        self.assertAlmostEqual(math.degrees(box.Placement.Rotation.Angle), 45)
        axis = box.Placement.Rotation.Axis
        self.assertAlmostEqual(axis.x, 1)
        exprs = c.describe(box)["expressions"]
        self.assertEqual(exprs["Length"], "Params.L")
        self.assertIn("Placement.Base.x", exprs)

        cyl = api.create_cylinder(self.doc, label="C", diameter="=HALF",
                                  height="=L", axis="y")
        self.assertAlmostEqual(cyl.Radius.Value, 2)
        self.set_param(sheet, "HALF", 6)
        self.assertAlmostEqual(cyl.Radius.Value, 3)

        ext = api.create_extruded_polygon(
            self.doc, label="P", points=[[0, 0], [4, 0], [0, 3]],
            height="=HALF", offset="=OFF")
        self.assertAlmostEqual(ext.Shape.BoundBox.ZMin, 5)
        self.assertAlmostEqual(ext.Shape.BoundBox.ZLength, 6)
        self.set_param(sheet, "OFF", 1)
        self.assertAlmostEqual(ext.Shape.BoundBox.ZMin, 1)

        with self.assertRaises(api.ApiError):
            api.create_box(self.doc, label="Bad", length="=NOPE", width=1,
                           height=1)
        self.assertEqual(self.doc.getObjectsByLabel("Bad"), [])

    def test_cylinder_axis_and_angle(self):
        cyl = api.create_cylinder(self.doc, label="Rod", radius=1,
                                  height=10, axis="x", position=[0, 0, 0])
        bb = cyl.Shape.BoundBox
        self.assertAlmostEqual(bb.XLength, 10, places=6)
        self.assertAlmostEqual(bb.YLength, 2, places=6)
        half = api.create_cylinder(self.doc, label="Half", radius=1,
                                   height=1, angle=180)
        self.assertAlmostEqual(half.Shape.Volume, math.pi / 2, places=6)
        with self.assertRaises(api.ApiError):
            api.create_cylinder(self.doc, label="X", radius=1, diameter=2,
                                height=1)
        with self.assertRaises(api.ApiError):
            api.create_cylinder(self.doc, label="X", height=1)

    def test_other_primitives(self):
        sph = api.create_sphere(self.doc, label="S", radius=2,
                                position=[0, 0, 5])
        self.assertAlmostEqual(sph.Shape.Volume, 4 / 3 * math.pi * 8, places=4)
        self.assertAlmostEqual(sph.Shape.BoundBox.Center.z, 5)
        cone = api.create_cone(self.doc, label="K", radius1=2, radius2=0,
                               height=3, axis="-z")
        self.assertAlmostEqual(cone.Shape.Volume, math.pi * 4 * 3 / 3,
                               places=4)
        self.assertAlmostEqual(cone.Shape.BoundBox.ZMin, -3)
        tor = api.create_torus(self.doc, label="T", radius1=10, radius2=1)
        self.assertAlmostEqual(tor.Shape.Volume,
                               2 * math.pi ** 2 * 10 * 1, places=3)
        with self.assertRaises(api.ApiError):
            api.create_torus(self.doc, label="T2", radius1=1, radius2=2)
        wedge = api.create_wedge(self.doc, label="W", x2min=0, x2max=10,
                                 z2min=0, z2max=10)
        self.assertAlmostEqual(wedge.Shape.Volume, 1000, places=4)
        self.assertEqual(wedge.TypeId, "Part::Wedge")
        with self.assertRaises(api.ApiError):
            api.create_wedge(self.doc, label="W2", xmin=5, xmax=1)

    def test_extruded_polygon_planes(self):
        ext = api.create_extruded_polygon(
            self.doc, label="Tri", points=[[0, 0], [10, 0], [0, 10]],
            height=2, plane="xz", offset=1)
        self.assertEqual(ext.TypeId, "Part::Extrusion")
        self.assertAlmostEqual(ext.Shape.Volume, 100, places=6)
        bb = ext.Shape.BoundBox
        self.assertAlmostEqual(bb.YMin, 1)
        self.assertAlmostEqual(bb.YMax, 3)
        self.assertAlmostEqual(bb.ZMax, 10)
        profile = self.doc.getObjectsByLabel("Tri profile")[0]
        self.assertFalse(profile.Visibility)
        with self.assertRaises(api.ApiError):
            api.create_extruded_polygon(self.doc, label="Line",
                                        points=[[0, 0], [1, 1], [2, 2]],
                                        height=1)
        with self.assertRaises(api.ApiError):
            api.create_extruded_polygon(self.doc, label="Two",
                                        points=[[0, 0], [1, 1]], height=1)

    # -- booleans / transforms / patterns ------------------------------------ #

    def test_coax_cut(self):
        outer = api.create_cylinder(self.doc, label="Outer", radius=2.3,
                                    height=20, axis="x")
        inner = api.create_cylinder(self.doc, label="Inner", radius=0.5,
                                    height=20, axis="x")
        diel = api.boolean(self.doc, label="Dielectric", op="cut",
                           base="Outer", tools=["Inner"])
        self.assertEqual(diel.TypeId, "Part::Cut")
        self.assertAlmostEqual(diel.Shape.Volume,
                               math.pi * (2.3 ** 2 - 0.5 ** 2) * 20, places=4)
        self.assertFalse(outer.Visibility)
        self.assertFalse(inner.Visibility)
        m = api.measure(self.doc, obj="Dielectric")
        self.assertEqual(m["solids"], 1)
        self.assertTrue(m["valid"])
        self.assertAlmostEqual(m["bbox"]["size"][0], 20)

    def test_fuse_common_multicut(self):
        api.create_box(self.doc, label="A", length=2, width=1, height=1)
        api.create_box(self.doc, label="B", length=2, width=1, height=1,
                       position=[1, 0, 0])
        api.create_box(self.doc, label="C", length=1, width=1, height=1,
                       position=[3, 0, 0])
        fuse = api.boolean(self.doc, label="F", op="fuse", base="A",
                           tools=["B", "C"])
        self.assertAlmostEqual(fuse.Shape.Volume, 4, places=6)
        api.create_box(self.doc, label="D", length=2, width=1, height=1)
        api.create_box(self.doc, label="E", length=2, width=1, height=1,
                       position=[1, 0, 0])
        common = api.boolean(self.doc, label="Co", op="common", base="D",
                             tools="E")
        self.assertAlmostEqual(common.Shape.Volume, 1, places=6)
        api.create_box(self.doc, label="G", length=10, width=1, height=1)
        api.create_box(self.doc, label="H1", length=1, width=1, height=1)
        api.create_box(self.doc, label="H2", length=1, width=1, height=1,
                       position=[5, 0, 0])
        cut = api.boolean(self.doc, label="Cut2", op="cut", base="G",
                          tools=["H1", "H2"])
        self.assertAlmostEqual(cut.Shape.Volume, 8, places=6)
        self.assertEqual(len(self.doc.getObjectsByLabel("Cut2 tools")), 1)
        with self.assertRaises(api.ApiError):
            api.boolean(self.doc, label="X", op="xor", base="G", tools=["F"])
        with self.assertRaises(api.ApiError):
            api.boolean(self.doc, label="X", op="cut", base="G", tools=[])
        with self.assertRaises(api.ApiError):
            api.boolean(self.doc, label="X", op="cut", base="G",
                        tools=["G"])

    def test_transform(self):
        box = api.create_box(self.doc, label="M", length=2, width=2,
                             height=2)
        api.transform(self.doc, obj="M", translate=[1, 0, "1 cm"])
        self.assertAlmostEqual(box.Placement.Base.x, 1)
        self.assertAlmostEqual(box.Placement.Base.z, 10)
        api.transform(self.doc, obj="M", rotate_axis="z", angle=90)
        bb = box.Shape.BoundBox
        self.assertAlmostEqual(bb.Center.x, 2, places=6)
        self.assertAlmostEqual(bb.Center.y, 1, places=6)
        api.transform(self.doc, obj="M", rotate_axis="z", angle=90,
                      center=[0, 0, 0])
        bb = box.Shape.BoundBox
        self.assertAlmostEqual(bb.Center.x, -1, places=6)
        self.assertAlmostEqual(bb.Center.y, 2, places=6)
        self.params(P=1)
        api.create_box(self.doc, label="Bound", length=1, width=1, height=1,
                       position=["=P", 0, 0])
        with self.assertRaises(api.ApiError):
            api.transform(self.doc, obj="Bound", translate=[1, 0, 0])

    def test_mirror(self):
        api.create_box(self.doc, label="Arm", length=5, width=1, height=1,
                       position=[1, 0, 0])
        mir = api.mirror(self.doc, label="Arm2", obj="Arm", plane="yz")
        self.assertEqual(mir.TypeId, "Part::Mirroring")
        bb = mir.Shape.BoundBox
        self.assertAlmostEqual(bb.XMin, -6)
        self.assertAlmostEqual(bb.XMax, -1)
        mir2 = api.mirror(self.doc, label="Arm3", obj="Arm",
                          plane=[1, 0, 0], base_point=[10, 0, 0])
        self.assertAlmostEqual(mir2.Shape.BoundBox.XMin, 14)

    def test_arrays(self):
        sheet = self.params(N_VIAS=3, PITCH=5)
        api.create_cylinder(self.doc, label="Via", radius=0.5, height=1)
        arr = api.linear_array(self.doc, label="Vias", obj="Via",
                               direction="y", count="=N_VIAS", spacing="=PITCH")
        self.assertEqual(len(arr.Shape.Solids), 3)
        self.assertAlmostEqual(arr.Shape.BoundBox.YLength, 11, places=6)
        self.set_param(sheet, "N_VIAS", 4)
        self.assertEqual(len(arr.Shape.Solids), 4)
        api.create_box(self.doc, label="Pad", length=1, width=1, height=1)
        arr2 = api.linear_array(self.doc, label="Pads", obj="Pad",
                                direction=[1, 0, 0], count=2, spacing=3)
        self.assertAlmostEqual(arr2.Shape.Volume, 2, places=6)
        self.assertAlmostEqual(arr2.Shape.BoundBox.XLength, 4, places=6)
        api.create_box(self.doc, label="Spoke", length=1, width=1, height=1,
                       position=[5, 0, 0])
        pol = api.polar_array(self.doc, label="Spokes", obj="Spoke",
                              count=4, angle=360)
        self.assertEqual(len(pol.Shape.Solids), 4)
        self.assertAlmostEqual(pol.Shape.BoundBox.XMin, -6, places=6)
        with self.assertRaises(api.ApiError):
            api.linear_array(self.doc, label="Bad", obj="Pad", count=0,
                             spacing=1)

    # -- fillet / chamfer --------------------------------------------------- #

    def test_fillet_chamfer_selectors(self):
        api.create_box(self.doc, label="Blk", length=10, width=10,
                       height=10)
        res = api.fillet(self.doc, label="Round", obj="Blk",
                         edges={"parallel_to": "z"}, radius=1)
        self.assertEqual(res["edges_matched"], 4)
        self.assertEqual(res["object"]["label"], "Round")
        rnd = self.doc.getObjectsByLabel("Round")[0]
        self.assertEqual(rnd.TypeId, "Part::Fillet")
        expected = 1000 - 4 * (1 - math.pi / 4) * 10
        self.assertAlmostEqual(rnd.Shape.Volume, expected, places=4)

        api.create_box(self.doc, label="Blk2", length=10, width=10,
                       height=10)
        res = api.chamfer(self.doc, label="Bev", obj="Blk2",
                          edges={"at_z": 10}, size="1 mm")
        self.assertEqual(res["edges_matched"], 4)
        res = api.chamfer(self.doc, label="Bev1", obj="Blk2",
                          edges={"at_z": 10, "parallel_to": "x"}, size=1)
        self.assertEqual(res["edges_matched"], 2)
        res = api.fillet(self.doc, label="One", obj="Blk2",
                         edges={"near_point": [0, 0, 5]}, radius=0.5)
        self.assertEqual(res["edges_matched"], 1)
        res = api.fillet(self.doc, label="All", obj="Blk2",
                         edges={"type": "all"}, radius=0.5)
        self.assertEqual(res["edges_matched"], 12)

        with self.assertRaises(api.ApiError):
            api.fillet(self.doc, label="None", obj="Blk2",
                       edges={"at_z": 99}, radius=1)
        with self.assertRaises(api.ApiError) as ctx:
            api.fillet(self.doc, label="Huge", obj="Blk2",
                       edges={"parallel_to": "z"}, radius=50)
        self.assertIn("failed to compute", str(ctx.exception))
        self.assertEqual(self.doc.getObjectsByLabel("Huge"), [])

    def test_failed_fillet_restores_visibility(self):
        box = api.create_box(self.doc, label="V", length=2, width=2,
                             height=2)
        with self.assertRaises(api.ApiError):
            api.fillet(self.doc, label="Big", obj="V",
                       edges={"type": "all"}, radius=5)
        self.assertTrue(box.Visibility)
        self.assertEqual(self.doc.getObjectsByLabel("Big"), [])

    def test_undo_one_step(self):
        self.doc.UndoMode = 1
        api.create_box(self.doc, label="U", length=2, width=2, height=2)
        with self.assertRaises(api.ApiError):
            api.fillet(self.doc, label="UF", obj="U", edges={"type": "all"},
                       radius=5)
        self.assertEqual(self.doc.getObjectsByLabel("UF"), [])
        api.transform(self.doc, obj="U", translate=[5, 0, 0])
        self.doc.undo()
        self.assertAlmostEqual(
            self.doc.getObjectsByLabel("U")[0].Placement.Base.x, 0)
        self.doc.undo()
        self.assertEqual(self.doc.getObjectsByLabel("U"), [])

    # -- STEP --------------------------------------------------------------- #

    def test_import_step_roundtrip(self):
        import Part
        folder = tempfile.mkdtemp(prefix="wavesim_api_")
        try:
            path = os.path.join(folder, "part.step")
            Part.makeBox(3, 4, 5).exportStep(path)
            objs = api.import_step(self.doc, path=path, label_prefix="Imp")
            labels = [o.Label for o in objs]
            self.assertIn("Imp", labels)
            imp = self.doc.getObjectsByLabel("Imp")[0]
            self.assertAlmostEqual(imp.Shape.Volume, 60, places=4)
            with self.assertRaises(api.ApiError):
                api.import_step(self.doc, path=os.path.join(folder, "x.step"))
            txt = os.path.join(folder, "a.txt")
            open(txt, "w").close()
            with self.assertRaises(api.ApiError):
                api.import_step(self.doc, path=txt)
            with self.assertRaises(api.ApiError):
                api.import_step(self.doc, path=path, label_prefix="Imp")
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    # -- inspection / housekeeping ----------------------------------------- #

    def test_check_geometry_overlap(self):
        api.create_box(self.doc, label="A", length=2, width=2, height=2)
        api.create_box(self.doc, label="B", length=2, width=2, height=2,
                       position=[1, 0, 0])
        api.create_box(self.doc, label="Touch", length=1, width=1, height=1,
                       position=[-1, 0, 0])
        api.create_box(self.doc, label="Far", length=1, width=1, height=1,
                       position=[50, 0, 0])
        rep = api.check_geometry(self.doc)
        self.assertFalse(rep["ok"])
        self.assertEqual(len(rep["overlaps"]), 1)
        ov = rep["overlaps"][0]
        self.assertEqual({ov["a"], ov["b"]}, {"A", "B"})
        self.assertAlmostEqual(ov["volume_mm3"], 4, places=6)
        # Boolean inputs are not checked against their result.
        api.boolean(self.doc, label="AB", op="fuse", base="A", tools=["B"])
        rep = api.check_geometry(self.doc)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual({r["label"] for r in rep["objects"]},
                         {"AB", "Touch", "Far"})
        rep = api.check_geometry(self.doc, objects=["Touch"])
        self.assertEqual(len(rep["objects"]), 1)

    def test_check_geometry_ignores_wavesim(self):
        sim = self.new_sim()
        api.create_box(self.doc, label="Sub", length=1, width=1, height=1)
        rep = api.check_geometry(self.doc)
        self.assertEqual([r["label"] for r in rep["objects"]], ["Sub"])
        with self.assertRaises(api.ApiError):
            api.delete_object(self.doc, obj=sim.Group[0].Name)

    def test_delete_object(self):
        api.create_box(self.doc, label="A", length=2, width=2, height=2)
        api.create_box(self.doc, label="B", length=1, width=1, height=1)
        api.boolean(self.doc, label="AB", op="cut", base="A", tools=["B"])
        with self.assertRaises(api.ApiError) as ctx:
            api.delete_object(self.doc, obj="B")
        self.assertIn("AB", str(ctx.exception))
        res = api.delete_object(self.doc, obj="AB")
        self.assertEqual(res["deleted"], "AB")
        self.assertEqual(sorted(res["shown"]), ["A", "B"])
        self.assertTrue(self.doc.getObjectsByLabel("A")[0].Visibility)
        api.delete_object(self.doc, obj="B")
        self.assertEqual(self.doc.getObjectsByLabel("B"), [])
        api.create_box(self.doc, label="C", length=1, width=1, height=1)
        api.mirror(self.doc, label="C2", obj="C")
        res = api.delete_object(self.doc, obj="C", force=True)
        self.assertEqual(res["dependents"], ["C2"])
        self.assertEqual(self.doc.getObjectsByLabel("C"), [])

    def test_rename_and_list(self):
        sim = self.new_sim()
        api.create_box(self.doc, label="Box", length=1, width=2, height=3)
        api.create_box(self.doc, label="Other", length=1, width=1, height=1)
        api.rename_object(self.doc, obj="Box", new_label="Substrate")
        self.assertEqual(len(self.doc.getObjectsByLabel("Substrate")), 1)
        with self.assertRaises(api.ApiError):
            api.rename_object(self.doc, obj="Other", new_label="Substrate")
        api.create_material(sim, label="FR4", eps=4.4, bodies=["Substrate"])
        items = {o["label"]: o for o in api.list_objects(self.doc)}
        self.assertEqual(set(items), {"Substrate", "Other"})
        sub = items["Substrate"]
        self.assertEqual(sub["type"], "Part::Box")
        self.assertEqual(sub["material"], "FR4")
        self.assertTrue(sub["is_solid"])
        self.assertAlmostEqual(sub["volume"], 6)
        self.assertEqual(sub["bbox"]["size"], [1, 2, 3])
        self.assertIsNone(items["Other"]["material"])
        with self.assertRaises(api.ApiError) as ctx:
            api.delete_object(self.doc, obj="Substrate")
        self.assertIn("FR4", str(ctx.exception))

    def test_measure(self):
        api.create_box(self.doc, label="Bx", length=2, width=4, height=6,
                       position=[1, 1, 1])
        m = api.measure(self.doc, obj="Bx")
        self.assertAlmostEqual(m["volume_mm3"], 48)
        self.assertAlmostEqual(m["area_mm2"], 2 * (8 + 12 + 24))
        self.assertEqual([round(v, 6) for v in m["center_of_mass"]],
                         [2, 3, 4])
        self.assertEqual(m["bbox"]["min"], [1, 1, 1])
        self.assertEqual(m["solids"], 1)
        self.assertTrue(m["valid"])
        with self.assertRaises(api.ApiError):
            api.measure(self.doc, obj="Nope")


if __name__ == "__main__":
    unittest.main()
