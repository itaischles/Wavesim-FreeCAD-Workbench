# -*- coding: utf-8 -*-
"""Gate: the lumped port's terminal-geometry resolver.

Run under **FreeCAD's bundled Python** (it needs ``Part``/``PartDesign``), not
the solver conda Python::

    "%LOCALAPPDATA%\\Programs\\FreeCAD 1.1\\bin\\freecadcmd.exe" \\
        tools/check_lumped_geometry.py

Builds throwaway geometry and checks what
:func:`wavesim_gui.lumped_port.resolve_line` makes of every way a user can point
at a gap. Two of these cases are here because they were bugs:

* **container placement** — a PartDesign feature's ``Shape`` is in its Body's
  local frame, so reading ``Pad.Shape`` without the Body's Placement puts a
  terminal picked on the second Body on top of the first, and the element
  silently spans nothing. A ``Part::Box`` cannot catch this: its Shape already
  carries its own Placement, so the offset is the identity.
* **non-parallel faces** — the resolver used to project onto the ``+`` face's
  plane anyway, moving an endpoint while the warning claimed it joined the two
  centres.

It also covers the polarity rules (an edge's own direction, ``ReversePolarity``)
and the migration of a port saved with the old *local*-scope
``App::PropertyLinkSub`` terminals, which FreeCAD refuses to point into a Body.

The last section is about the **grid** rather than the pick: that the endpoints
reach the job exactly as picked (nothing snaps them), and that the
``kappa``/``C_cell`` estimate matches the closed form on both a uniform and a
graded mesh. Its identity ``kappa == dt*L/(eps*dA)`` for a node-to-node line is
the same one ``tools/check_lumped_port.py`` asserts against the solver's own
``LineSource.self_coupling``, which is what ties the two estimators together
across the process split without this file needing the solver.

``freecadcmd`` can swallow a crashed script's stdout, so the report is also
written to ``tools/lumped_geometry.txt``.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))          # the workbench package

import FreeCAD

# The gui modules guard on ``import FreeCADGui``, which freecadcmd provides
# without ``addCommand``; stub it so importing them registers nothing.
import FreeCADGui
if not hasattr(FreeCADGui, "addCommand"):
    FreeCADGui.addCommand = lambda *a, **k: None

import Part

from wavesim_gui import commands
from wavesim_gui import domain as domain_mod
from wavesim_gui import lumped_port as lp


TOL = 1.0e-9
# The kappa identity reaches through a sub-stepped quadrature, so it closes to
# round-off rather than exactly.
KAPPA_TOL = 1.0e-6
# wavesim.constants.EPS0's own (truncated) value -- the one lumped_port uses.
EPS0 = 8.8541878e-12
_LINES = []


def _say(text):
    _LINES.append(text)
    print(text)


def _vec(x, y, z):
    return FreeCAD.Vector(x, y, z)


def _check(label, got, expect_p0, expect_p1, expect_warn=0):
    """Compare a resolved ``(p0, p1, warnings)`` against what it should be."""
    p0, p1, warn = got
    if expect_p0 is None:
        ok = p0 is None
        detail = "p0 = {}".format(p0)
    else:
        ok = (p0 is not None
              and (p0 - expect_p0).Length < TOL
              and (p1 - expect_p1).Length < TOL)
        detail = "p0 = {}  p1 = {}".format(p0, p1)
    if expect_warn is not None:
        ok = ok and len(warn) == expect_warn
    _say("  {:24s} {}   {}".format(label, "OK  " if ok else "FAIL", detail))
    for text in warn:
        _say("  {:24s}      warn: {}".format("", text))
    return ok


def _sim_doc(name):
    doc = FreeCAD.newDocument(name)
    commands.CommandNewSimulation().Activated()
    return doc, commands.active_simulation(doc)


def _port(doc, sim, name, plus, minus=None, reverse=False):
    obj = doc.addObject("App::FeaturePython", name)
    lp.LumpedPortObject(obj)
    obj.TerminalPlus = plus
    if minus is not None:
        obj.TerminalMinus = minus
    obj.ReversePolarity = reverse
    lp.sources_group(sim).addObject(obj)
    doc.recompute()
    return obj


def _pad_body(doc, name, placement):
    """A 4 x 4 x 2 mm PartDesign pad in a Body carrying *placement*."""
    body = doc.addObject("PartDesign::Body", name)
    body.Placement = placement
    sketch = doc.addObject("Sketcher::SketchObject", name + "Sketch")
    body.addObject(sketch)
    pts = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
    for a, b in zip(pts, pts[1:] + pts[:1]):
        sketch.addGeometry(
            Part.LineSegment(_vec(a[0], a[1], 0.0), _vec(b[0], b[1], 0.0)), False)
    pad = doc.addObject("PartDesign::Pad", name + "Pad")
    body.addObject(pad)
    pad.Profile = sketch
    pad.Length = 2.0
    doc.recompute()
    return pad


def _face_named(pad, axis, value):
    """The pad face whose centre-of-mass sits at *value* along *axis* (local)."""
    for n, face in enumerate(pad.Shape.Faces, start=1):
        if abs(getattr(face.CenterOfMass, axis) - value) < 1.0e-7:
            return "Face{}".format(n)
    raise AssertionError("no face at {} = {}".format(axis, value))


def primitives():
    """Vertices / faces / edges of Part:: primitives, whose Shape is global."""
    doc, sim = _sim_doc("lp_gate_prim")
    box = doc.addObject("Part::Box", "Box")
    box.Length, box.Width, box.Height = 4.0, 4.0, 2.0
    tilt = doc.addObject("Part::Box", "Tilt")
    tilt.Length, tilt.Width, tilt.Height = 4.0, 4.0, 2.0
    tilt.Placement = FreeCAD.Placement(
        _vec(6, 0, 0), FreeCAD.Rotation(_vec(0, 0, 1), 20))
    wire = doc.addObject("Part::Feature", "Wire")
    wire.Shape = Part.makePolygon([_vec(0, 8, 0), _vec(0, 8, 3)])
    doc.recompute()

    ok = True
    _say("\nPart:: primitives")
    # Two vertices on ONE body: the case an App::PropertyLinkSubList would
    # collapse into a single unordered pair.
    ok &= _check("two vertices, one body",
                 lp.resolve_line(_port(doc, sim, "P1",
                                       (box, ["Vertex1"]), (box, ["Vertex3"]))),
                 _vec(0, 0, 2), _vec(0, 4, 2))
    # A vertex projected onto a face's plane.
    ok &= _check("vertex + face",
                 lp.resolve_line(_port(doc, sim, "P2",
                                       (box, ["Vertex1"]), (box, ["Face2"]))),
                 _vec(0, 0, 2), _vec(4, 0, 2))
    # An edge carries both ends; its own direction is the polarity.
    ok &= _check("whole curve object",
                 lp.resolve_line(_port(doc, sim, "P3", (wire, []))),
                 _vec(0, 8, 0), _vec(0, 8, 3))
    ok &= _check("...reversed",
                 lp.resolve_line(_port(doc, sim, "P4", (wire, []), reverse=True)),
                 _vec(0, 8, 3), _vec(0, 8, 0))
    # Non-parallel faces: both centres, unprojected, and it says so. Also off
    # axis, hence two warnings.
    ok &= _check("non-parallel faces",
                 lp.resolve_line(_port(doc, sim, "P5",
                                       (box, ["Face2"]), (tilt, ["Face1"]))),
                 _vec(4, 2, 1), tilt.Shape.Faces[0].CenterOfMass, expect_warn=2)
    # Refusals.
    ok &= _check("one vertex only",
                 lp.resolve_line(_port(doc, sim, "P6", (box, ["Vertex1"]))),
                 None, None, expect_warn=1)
    ok &= _check("same point twice",
                 lp.resolve_line(_port(doc, sim, "P7",
                                       (box, ["Vertex1"]), (box, ["Vertex1"]))),
                 None, None, expect_warn=1)
    FreeCAD.closeDocument(doc.Name)
    return ok


def part_design_bodies():
    """Two PartDesign Bodies, the second displaced -- the reported failure."""
    doc, sim = _sim_doc("lp_gate_pd")
    pad_a = _pad_body(doc, "BodyA", FreeCAD.Placement())
    pad_b = _pad_body(doc, "BodyB", FreeCAD.Placement(_vec(0, 10, 0),
                                                      FreeCAD.Rotation()))
    # The facing pair across the gap: A's +y face (local y = 4) and B's -y face
    # (local y = 0, global y = 10).
    fa = _face_named(pad_a, "y", 4.0)
    fb = _face_named(pad_b, "y", 0.0)

    ok = True
    _say("\nPartDesign bodies (second Body at y = 10 mm)")
    port = _port(doc, sim, "PD1", (pad_a, [fa]), (pad_b, [fb]))
    ok &= _check("across the gap",
                 lp.resolve_line(port), _vec(2, 4, 1), _vec(2, 10, 1))
    # The drawn endpoints must follow, not just the resolver.
    drawn_ok = ((port.P0 - _vec(2, 4, 1)).Length < TOL
                and (port.P1 - _vec(2, 10, 1)).Length < TOL)
    _say("  {:24s} {}   P0 = {}  P1 = {}".format(
        "drawn endpoints", "OK  " if drawn_ok else "FAIL", port.P0, port.P1))
    ok &= drawn_ok

    # A port saved with the old local-scope property must migrate in place,
    # keeping its terminals (FreeCAD refuses a local link into a Body).
    legacy = doc.addObject("App::FeaturePython", "Legacy")
    legacy.addProperty("App::PropertyLinkSub", "TerminalPlus", "Port", "")
    legacy.addProperty("App::PropertyLinkSub", "TerminalMinus", "Port", "")
    legacy.TerminalPlus = (pad_a, [fa])
    legacy.TerminalMinus = (pad_b, [fb])
    lp.LumpedPortObject(legacy)             # ensure_port_props -> upgrade
    lp.sources_group(sim).addObject(legacy)
    doc.recompute()
    typed = legacy.getTypeIdOfProperty("TerminalPlus") == "App::PropertyLinkSubGlobal"
    _say("  {:24s} {}   {}".format(
        "legacy property upgrade", "OK  " if typed else "FAIL",
        legacy.getTypeIdOfProperty("TerminalPlus")))
    ok &= typed
    ok &= _check("...terminals kept",
                 lp.resolve_line(legacy), _vec(2, 4, 1), _vec(2, 10, 1))
    FreeCAD.closeDocument(doc.Name)
    return ok


def _ok(label, condition, detail=""):
    _say("  {:24s} {}   {}".format(label, "OK  " if condition else "FAIL", detail))
    return bool(condition)


def endpoints_and_coupling():
    """Endpoints reach the job untouched, and kappa / C_cell match the closed form.

    A 1 mm uniform grid is **planted** straight onto the Domain's node arrays
    rather than meshed: what is under test is the quadrature the estimator models,
    and a hand-written grid makes the expected kappa a closed form. Nothing may
    recompute after the planting -- ``Domain.execute`` would rebuild the arrays
    from geometry that does not exist here -- so the ports are made first.

    The graded case is the one that used to be wrong on both sides: the weights
    are per-cell overlaps and the Ampere face is built from *dual* widths, so a
    node-to-node line still satisfies ``kappa == dt*L/(eps*dA)`` on an uneven
    mesh, and an element can span uneven cells at all.
    """
    doc, sim = _sim_doc("lp_gate_ends")
    # Picked gaps: 4 cells across, 1 cell across, ends mid-cell, and oblique.
    wide = doc.addObject("Part::Feature", "Wide")
    wide.Shape = Part.makePolygon([_vec(5, 5, 4), _vec(5, 5, 8)])
    narrow = doc.addObject("Part::Feature", "Narrow")
    narrow.Shape = Part.makePolygon([_vec(9, 9, 4), _vec(9, 9, 5)])
    partial = doc.addObject("Part::Feature", "Partial")
    partial.Shape = Part.makePolygon([_vec(7, 7, 4.5), _vec(7, 7, 7.5)])
    oblique = doc.addObject("Part::Feature", "Oblique")
    oblique.Shape = Part.makePolygon([_vec(12, 12, 4), _vec(12, 15, 8)])
    doc.recompute()

    port = _port(doc, sim, "S1", (wide, []))
    flipped = _port(doc, sim, "S2", (wide, []), reverse=True)
    one_cell = _port(doc, sim, "S3", (narrow, []))
    half_ends = _port(doc, sim, "S4", (partial, []))
    skew = _port(doc, sim, "S5", (oblique, []))

    dom = domain_mod.find_domain(sim)
    ticks = [float(i) for i in range(21)]        # 0..20 mm, 1 mm cells
    dom.NodesX = dom.NodesY = dom.NodesZ = ticks
    dom.Nx = dom.Ny = dom.Nz = 20
    dt = domain_mod.cfl_dt(dom)

    ok = True
    _say("\nendpoints & coupling (planted 1 mm grid, 20 cells per axis)")

    # The job carries the picked ends: nothing snaps them any more, because the
    # solver weights an edge by the line's overlap with that edge's own cell.
    spec = lp.lumped_port_spec(port, (0.0, 0.0, 0.0))
    ok &= _ok("spec carries the pick",
              spec is not None and abs(spec["p0"][2] - 0.004) < TOL
              and abs(spec["p1"][2] - 0.008) < TOL,
              "" if spec is None else "p0 = {}  p1 = {}".format(
                  spec["p0"], spec["p1"]))
    ok &= _ok("polarity kept",
              abs(lp.lumped_port_spec(flipped, (0.0, 0.0, 0.0))["p0"][2] - 0.008)
              < TOL, "'+' end stays the '+' end")

    # kappa == dt*L/(eps*dA) node to node -- the identity the solver-side gate
    # checks against LineSource.self_coupling on its own grid.
    area = 1.0e-6
    for obj, label, length, cells, partial_ends in (
            (port, "4-cell line", 4.0e-3, 4, 0),
            (one_cell, "1-cell line", 1.0e-3, 1, 0),
            (half_ends, "mid-cell ends", 3.0e-3, 4, 2)):
        report = lp.coupling_report(obj, sim)
        if report is None:
            ok &= _ok(label, False, "report is None")
            continue
        # kappa sums w^2/h: a whole cell contributes h, a half-covered end h/4.
        # The mid-cell line covers cells 4..7 as 0.5, 1, 1, 0.5 mm, so the sum is
        # 0.25 + 1 + 1 + 0.25 = 2.5 mm against its 3 mm length.
        eff = length if partial_ends == 0 else 2.5e-3
        expect_kappa = dt * eff / (EPS0 * area)
        ok &= _ok(label,
                  abs(report["length"] - length) < TOL
                  and report["cells"] == cells
                  and report["partial_ends"] == partial_ends
                  and abs(report["kappa"] - expect_kappa) / expect_kappa < KAPPA_TOL
                  and abs(report["c_cell"] * report["kappa"] - dt) / dt < KAPPA_TOL,
                  "L %.3f mm, %d cells, %d partial, kappa %.5g (expect %.5g), "
                  "C_cell %.4g fF" % (report["length"] * 1e3, report["cells"],
                                      report["partial_ends"], report["kappa"],
                                      expect_kappa, report["c_cell"] * 1e15))

    # An oblique line is left alone and gets no estimate (a different quadrature).
    ok &= _ok("oblique gets no estimate", lp.coupling_report(skew, sim) is None,
              "spec still written: %s" % (
                  lp.lumped_port_spec(skew, (0.0, 0.0, 0.0)) is not None,))

    # A graded mesh must not change the identity -- that is the whole point of
    # the per-cell overlap weights and the dual Ampere face.
    graded = [0.0]
    for i in range(20):
        graded.append(graded[-1] + (0.6 if i % 2 else 1.4))
    dom.NodesZ = graded
    dom.Nz = 20
    wide.Shape = Part.makePolygon([_vec(5, 5, graded[4]), _vec(5, 5, graded[8])])
    doc.recompute()
    dom.NodesX = dom.NodesY = ticks
    dom.NodesZ = graded
    dom.Nx = dom.Ny = dom.Nz = 20
    report = lp.coupling_report(port, sim)
    dt = domain_mod.cfl_dt(dom)
    length = (graded[8] - graded[4]) * 1.0e-3
    expect = dt * length / (EPS0 * area)
    ok &= _ok("graded mesh, node to node",
              report is not None
              and abs(report["kappa"] - expect) / expect < KAPPA_TOL,
              "kappa %.6g, expected %.6g (L = %.3f mm over 4 uneven cells)"
              % (report["kappa"] if report else float("nan"), expect,
                 length * 1e3))

    FreeCAD.closeDocument(doc.Name)
    return ok


def main():
    _say("lumped port geometry gate")
    ok = primitives()
    ok &= part_design_bodies()
    ok &= endpoints_and_coupling()
    _say("\nRESULT: {}".format("PASS" if ok else "FAIL"))
    with open(os.path.join(_HERE, "lumped_geometry.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(_LINES) + "\n")
    sys.stdout.flush()
    return 0 if ok else 1


main()
