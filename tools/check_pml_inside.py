# -*- coding: utf-8 -*-
"""Gate: the PML lives *inside* the domain box, and the mesh says so.

Run under **FreeCAD's bundled Python** (it needs ``Part``), not the solver conda
Python::

    "%LOCALAPPDATA%\\Programs\\FreeCAD 1.1\\bin\\freecadcmd.exe" \\
        tools/check_pml_inside.py

Why this is a gate
------------------
Moving the absorber inside the box buys two things -- the drawn box becomes the
true extent of what is solved, and geometry can be carried *through* the
absorber to infinity instead of ending at a wall -- and it puts three
invariants at risk, each of which fails silently:

1. **The box must not grow with ``PMLThickness``.** The whole claim is that the
   domain box is the grid. If the node arrays ran past it the drawn box would
   be a lie again, and every world-frame reading built on ``DomainMin`` (the
   snapshot origin, the port face planes) would be off by a shell.
2. **The shell must be one constant cell width.** ``wavesim.pml._pml_face_widths``
   *raises* on a graded absorber, so a snap landing in the shell is not a
   cosmetic problem -- it is a run that dies at ``init_cpml``. The snapper has
   to drop those lines, and the shell has to be the coarse target even where
   the interior is refined far below it.
3. **A face that does not absorb takes no shell.** A PEC face and a modal-port
   face own their own boundary; taking cells from them would move the wall the
   port plane has to cut.

And one behaviour, which is the feature itself: with a face's background
spacing at 0 the geometry reaches that wall, so its outermost cells lie in the
absorber. That is what "carry the structure to infinity" means here, and
:func:`~wavesim_gui.voxelize.pml_shell_warnings` is what tells the user when the
cross-section they carried in there is not actually invariant through it.

``freecadcmd`` swallows ``stdout``, so the report is also written to
``tools/pml_inside.txt`` (override with ``PML_INSIDE_REPORT``). Exits non-zero
on any failure.
"""

import os
import sys

import FreeCAD
import Part

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:                                                           # noqa: E402
    import FreeCADGui
    if not hasattr(FreeCADGui, "addCommand"):
        FreeCADGui.addCommand = lambda *a, **k: None
except ImportError:
    pass

from wavesim_gui import commands as cmd                        # noqa: E402
from wavesim_gui import domain as domain_mod                   # noqa: E402
from wavesim_gui import materials as mat_mod                   # noqa: E402
from wavesim_gui import voxelize as vox_mod                    # noqa: E402

FMAX_HZ = 5.0e9          # -> a 1 mm background cell
COARSE = 1.0
TOL = 1.0e-9

_report = []
_failures = []


def emit(line=""):
    _report.append(line)
    FreeCAD.Console.PrintMessage(line + "\n")


def check(ok, label, detail=""):
    _report.append("  [%s] %-52s %s" % ("ok" if ok else "FAIL", label, detail))
    FreeCAD.Console.PrintMessage(_report[-1] + "\n")
    if not ok:
        _failures.append(label)
    return ok


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #

def _document(build, spacing_mm=5.0, nonuniform=True, d_pml=8, bcs=None,
              spacings=None):
    """A simulation document with one material and the given per-face settings.

    *spacing_mm* sets every face; *spacings* overrides named ones
    (``{"x0": 0.0, ...}``) -- which is how a face is told to hand its geometry
    to the absorber. Returns ``(doc, sim, dom)``; the caller closes the
    document.
    """
    doc = FreeCAD.newDocument("pmlinside")
    sim = doc.addObject("App::DocumentObjectGroupPython", "Simulation")
    cmd.SimulationContainer(sim)
    dom = domain_mod.create_domain(doc, sim)
    _vacuum, pec = mat_mod.create_default_materials(doc, sim)
    pec.Bodies = build(doc)
    sim.MaxFrequency = FMAX_HZ
    dom.UseNonuniformGrid = bool(nonuniform)
    dom.PMLThickness = int(d_pml)
    for face, prop, _doc in domain_mod._SPACING_PROPS:
        setattr(dom, prop, "%g mm" % (spacings or {}).get(face, spacing_mm))
    for face, value in (bcs or {}).items():
        setattr(dom, domain_mod._FACE_TO_PROP[face], value)
    doc.recompute()
    return doc, sim, dom


def slab(doc):
    """A 20x20x4 mm dielectric slab centred on the origin in x/y."""
    body = doc.addObject("Part::Feature", "Slab")
    body.Shape = Part.makeBox(20, 20, 4, FreeCAD.Vector(-10, -10, -2))
    return [body]


def slab_with_detail(doc):
    """The same slab with a small hole -- a feature the snapper wants to snap."""
    shape = (Part.makeBox(20, 20, 4, FreeCAD.Vector(-10, -10, -2))
             .cut(Part.makeCylinder(0.7, 8.0, FreeCAD.Vector(-8.5, 0, -4))))
    body = doc.addObject("Part::Feature", "Slab")
    body.Shape = shape
    return [body]


# --------------------------------------------------------------------------- #
# 1. The box does not grow with the absorber
# --------------------------------------------------------------------------- #

def check_extent():
    emit("1. the domain box is the grid (it does not grow with PMLThickness)")
    for nonuniform in (True, False):
        kind = "graded" if nonuniform else "uniform"
        spans = {}
        for d_pml in (0, 4, 12):
            doc, _sim, dom = _document(slab, nonuniform=nonuniform, d_pml=d_pml)
            try:
                nodes = domain_mod.node_coords_mm(dom)
                spans[d_pml] = tuple((a[0], a[-1]) for a in nodes)
                mn, mx = dom.DomainMin, dom.DomainMax
                if d_pml == 8 or d_pml == 4:
                    pass
                ok = (abs(nodes[0][0] - mn.x) < TOL and
                      abs(nodes[1][0] - mn.y) < TOL and
                      abs(nodes[2][0] - mn.z) < TOL)
                check(ok, "%s d=%d: node origin is DomainMin" % (kind, d_pml),
                      "(%.3f, %.3f, %.3f)" % (nodes[0][0], nodes[1][0],
                                              nodes[2][0]))
            finally:
                FreeCAD.closeDocument(doc.Name)
        # The geometry is 20 mm wide with a 5 mm gap each side: 30 mm, whatever
        # the absorber does. A grown box would show up here as a wider span.
        widths = {d: round(s[0][1] - s[0][0], 6) for d, s in spans.items()}
        check(len(set(widths.values())) == 1,
              "%s: x span independent of d_pml" % kind, str(widths))
        check(abs(widths[0] - 30.0) < 0.5,
              "%s: x span is bbox + spacing" % kind,
              "%.3f mm (want 30 +/- one cell)" % widths[0])


# --------------------------------------------------------------------------- #
# 2. The shell is uniform -- the solver's own constraint
# --------------------------------------------------------------------------- #

def _shell_widths(nodes, depth, high):
    """The *depth* cell widths at one face of one axis."""
    w = [b - a for a, b in zip(nodes[:-1], nodes[1:])]
    return w[len(w) - depth:] if high else w[:depth]


def check_uniform_shell():
    emit()
    emit("2. the absorber shell is one constant width per face")
    # A hole 1.5 mm from the low-x wall: without an exclusion its silhouette
    # snaps land inside the shell and the solver refuses the mesh.
    doc, _sim, dom = _document(slab_with_detail, spacing_mm=1.0, d_pml=6)
    try:
        nodes = domain_mod.node_coords_mm(dom)
        snaps = [list(getattr(dom, p, []) or []) for p in domain_mod._SNAP_PROPS]
        for axis, name in enumerate("xyz"):
            for high in (False, True):
                widths = _shell_widths(nodes[axis], 6, high)
                span = max(widths) - min(widths)
                check(span < 1.0e-6,
                      "%s%d shell constant" % (name, int(high)),
                      "%.6f mm spread over %d cells (w=%.4f)"
                      % (span, len(widths), widths[0]))
        # ...and no forced line inside either shell, which is what guarantees it.
        for axis, name in enumerate("xyz"):
            lo = nodes[axis][6]
            hi = nodes[axis][-7]
            stray = [v for v in snaps[axis] if v < lo - TOL or v > hi + TOL]
            check(not stray, "no snapped line in the %s shells" % name,
                  "" if not stray else str(stray))
    finally:
        FreeCAD.closeDocument(doc.Name)


# --------------------------------------------------------------------------- #
# 3. A face that does not absorb takes no shell
# --------------------------------------------------------------------------- #

def check_non_absorbing_faces():
    emit()
    emit("3. a PEC face keeps its cells (only absorbing faces take a shell)")
    doc, _sim, dom = _document(slab, d_pml=6, bcs={"x0": "PEC"})
    try:
        params = domain_mod.domain_grid_params(dom)
        check(params["pad_lo"][0] == 0 and params["pad_hi"][0] == 6,
              "PEC x0 -> pad_lo[x] = 0, pad_hi[x] = 6",
              str((params["pad_lo"], params["pad_hi"])))
        nodes = domain_mod.node_coords_mm(dom)
        mn, mx = dom.DomainMin, dom.DomainMax
        pmn, pmx = dom.PmlMin, dom.PmlMax
        check(abs(pmn.x - mn.x) < TOL,
              "PML box meets the domain box on the PEC face",
              "PmlMin.x=%.4f DomainMin.x=%.4f" % (pmn.x, mn.x))
        check(abs(pmx.x - nodes[0][-7]) < TOL,
              "PML box is 6 cells in from the absorbing face",
              "PmlMax.x=%.4f node[-7]=%.4f" % (pmx.x, nodes[0][-7]))
        check(pmn.y > mn.y + TOL and pmx.y < mx.y - TOL,
              "PML box is strictly inside the domain box on y",
              "%.4f..%.4f in %.4f..%.4f" % (pmn.y, pmx.y, mn.y, mx.y))
    finally:
        FreeCAD.closeDocument(doc.Name)


# --------------------------------------------------------------------------- #
# 4. The feature: spacing 0 carries geometry into the absorber
# --------------------------------------------------------------------------- #

def check_geometry_reaches_absorber():
    emit()
    emit("4. spacing 0 carries the geometry into the absorber")
    # Zero gap on the two x faces only: the slab runs straight out through
    # them, while y and z keep a background gap *wider than their shell* (10 mm
    # against 6 cells of 1 mm) and so see plain vacuum. A gap narrower than the
    # shell would put the slab's own y and z ends inside the absorber, which is
    # a real warning and not a false one -- clearance is measured against the
    # shell now, not against the wall.
    doc, sim, dom = _document(slab, spacing_mm=10.0, d_pml=6,
                              spacings={"x0": 0.0, "x1": 0.0})
    try:
        nodes = domain_mod.node_coords_mm(dom)
        # The slab spans -10..10 in x and the box now stops there, so the six
        # outermost cells on each x face are metal *and* absorber.
        check(abs(nodes[0][0] + 10.0) < 0.5 and abs(nodes[0][-1] - 10.0) < 0.5,
              "the box wall is the body wall", "x %.3f..%.3f"
              % (nodes[0][0], nodes[0][-1]))
        check(nodes[0][6] < 10.0 - TOL and nodes[0][6] > -10.0 + TOL,
              "the absorber's inner edge is inside the body",
              "x=%.3f" % nodes[0][6])
        # The warning must stay quiet: this section really is invariant along x
        # and y through its shells.
        spec, arrays = vox_mod.build_job_from_document(doc, steps=1)
        params = domain_mod.domain_grid_params(dom)
        msgs = list(vox_mod.pml_shell_warnings(
            arrays, params["pad_lo"], params["pad_hi"], params["pml_faces"]))
        check(not msgs, "an invariant section through the shell warns not at all",
              "" if not msgs else msgs[0][:90])
        panel = domain_mod.shell_geometry_warnings(sim, dom)
        check(not panel, "and the panel notice stays quiet too",
              "" if not panel else panel[0][:90])
    finally:
        FreeCAD.closeDocument(doc.Name)


def check_shell_warning():
    emit()
    emit("5. a body that stops inside the shell is reported")
    def stub(doc):
        # The slab runs cleanly out through both x faces; the rib on top of it
        # stops at x = 9, one cell short of the wall and four cells deep into
        # the x1 shell. So x0 is invariant and x1 is not -- the two answers this
        # check has to tell apart.
        body = doc.addObject("Part::Feature", "Slab")
        body.Shape = Part.makeBox(20, 20, 4, FreeCAD.Vector(-10, -10, -2))
        rib = doc.addObject("Part::Feature", "Rib")
        rib.Shape = Part.makeBox(4, 6, 2, FreeCAD.Vector(5, -3, 2))
        return [body, rib]

    doc, sim, dom = _document(stub, spacing_mm=10.0, d_pml=6,
                              spacings={"x0": 0.0, "x1": 0.0})
    try:
        _spec, arrays = vox_mod.build_job_from_document(doc, steps=1)
        params = domain_mod.domain_grid_params(dom)
        msgs = list(vox_mod.pml_shell_warnings(
            arrays, params["pad_lo"], params["pad_hi"], params["pml_faces"]))
        faces = sorted(m.split()[1] for m in msgs)
        check("x1" in faces, "the face the body stops inside is named",
              "faces reported: %s" % (faces or "none"))
        check("x0" not in faces, "the face it runs cleanly out through is not",
              "faces reported: %s" % (faces or "none"))
        # ...and the cheap bounding-box reading the Domain panel shows live
        # agrees with the voxel one, which is the whole point of having it.
        panel = domain_mod.shell_geometry_warnings(sim, dom)
        named = sorted(m.split("the ")[1].split()[0] for m in panel)
        check(named == ["x1"], "the panel notice names the same face",
              "faces reported: %s" % (named or "none"))
        check("Rib" in " ".join(panel), "and the body that causes it",
              panel[0][:60] if panel else "")
    finally:
        FreeCAD.closeDocument(doc.Name)


# --------------------------------------------------------------------------- #
# 6. The drawn grid: every line goes to exactly one set
# --------------------------------------------------------------------------- #

def check_grid_segments():
    emit()
    emit("6. the drawn grid hands each segment to exactly one colour")
    if not getattr(domain_mod, "_GUI_AVAILABLE", False):
        check(True, "skipped: no view provider in this build")
        return
    doc, _sim, dom = _document(slab, d_pml=6)
    try:
        vp = domain_mod.DomainViewProvider.__new__(
            domain_mod.DomainViewProvider)
        nodes = domain_mod.node_coords_mm(dom)
        snaps = tuple(list(getattr(dom, p, []) or [])
                      for p in domain_mod._SNAP_PROPS)
        plane = domain_mod.grid_plane_positions_mm(dom)
        interior = (dom.PmlMin, dom.PmlMax)
        thin, bold, pml = vp._grid_segments(
            dom.DomainMin, dom.DomainMax, nodes[0], nodes[1], nodes[2],
            snaps=snaps, plane=plane, interior=interior)
        check(len(pml[0]) > 0, "the absorber gets its own segments",
              "%d points" % len(pml[0]))
        # Total drawn length is conserved: splitting a line at the shell
        # boundary must neither lose nor duplicate any of it.
        def length(pts):
            return sum(
                sum((a - b) ** 2 for a, b in zip(pts[i], pts[i + 1])) ** 0.5
                for i in range(0, len(pts), 2)
            )
        whole, _b2, _p2 = vp._grid_segments(
            dom.DomainMin, dom.DomainMax, nodes[0], nodes[1], nodes[2],
            snaps=snaps, plane=plane, interior=None)
        split = length(thin[0]) + length(pml[0])
        check(abs(split - length(whole[0])) < 1.0e-6,
              "split length == unsplit length",
              "%.4f vs %.4f mm" % (split, length(whole[0])))
        check(length(bold[0]) > 0.0 or not any(snaps),
              "snapped lines are still drawn whole")
    finally:
        FreeCAD.closeDocument(doc.Name)


# --------------------------------------------------------------------------- #
# 7. The snapshot overlay's own geometry
# --------------------------------------------------------------------------- #

def check_plot_frame():
    emit()
    emit("7. the snapshot 'Mask PML' frame")
    try:
        from wavesim_gui import results as res_mod
    except Exception as exc:                    # no Qt / matplotlib here
        check(True, "skipped: results module not importable", str(exc)[:60])
        return
    box = (0.0, 30.0, 0.0, 20.0)
    frame = res_mod._pml_frame(box, [6.0, 24.0], [6.0, 14.0])
    check(frame == (box, (6.0, 24.0, 6.0, 14.0)),
          "both axes absorbing -> the interior rectangle", str(frame))
    frame = res_mod._pml_frame(box, [6.0, 24.0], [])
    check(frame is not None and frame[1] == (6.0, 24.0, 0.0, 20.0),
          "an axis with no absorber spans the whole picture", str(frame))
    check(res_mod._pml_frame(box, [], []) is None,
          "no absorber at all -> no overlay")
    check(res_mod._pml_frame(None, [6.0, 24.0], []) is None,
          "no drawn extent -> no overlay")
    check(res_mod._pml_frame(box, [16.0, 14.0], []) is None,
          "shells that have closed over -> no overlay",
          "an inside-out rectangle would fill nothing and read as 'no PML'")
    frame = res_mod._pml_frame(box, [-5.0, 99.0], [])
    check(frame is not None and frame[1][:2] == (0.0, 30.0),
          "an interior past the picture is clamped to it", str(frame))


# --------------------------------------------------------------------------- #

def main():
    emit("PML-inside-the-domain gate")
    check_extent()
    check_uniform_shell()
    check_non_absorbing_faces()
    check_geometry_reaches_absorber()
    check_shell_warning()
    check_grid_segments()
    check_plot_frame()

    emit()
    emit("FAILED: " + "; ".join(_failures) if _failures
         else "All checks passed.")
    path = os.environ.get(
        "PML_INSIDE_REPORT",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "pml_inside.txt"))
    with open(path, "w") as fh:
        fh.write("\n".join(_report) + "\n")
    FreeCAD.Console.PrintMessage("report written to %s\n" % path)
    sys.stdout.flush()


main()
if _failures:
    raise SystemExit(1)
