# -*- coding: utf-8 -*-
"""Gate: a circle's centre line is taken only where it costs the mesh nothing.

Run under **FreeCAD's bundled Python** (it needs ``Part``), not the solver conda
Python::

    "%LOCALAPPDATA%\\Programs\\FreeCAD 1.1\\bin\\freecadcmd.exe" \\
        tools/check_circle_centres.py

Why this is a gate rather than a nicety
---------------------------------------
Forcing a grid line through a circle's centre is wanted -- it puts a node on the
axis where the port, probe or voltage path lives, and it gives a round feature
narrower than one cell an interior instead of a single lump of metal. But it
splits the gap it lands in, and the graded fill tiles a gap of width *w* with
``floor(w / target)`` cells (:func:`~wavesim_gui.gridbuild._graded_widths`
absorbs the remainder by stretching them). A gap spanning an **odd** number of
targets therefore halves to two stretched cells: measured on the reference coax,
the 3 mm inner conductor on a 1 mm grid went from three 1.0 mm cells to two of
1.5 mm -- 50% coarser through the conductor, for a line meant to improve it.

The alternative -- letting the fill round the cell count *up* instead -- is not
free either: on the same coax it is +23% cells and a 25% shorter timestep, and it
changes every existing model's mesh whether or not it has a circle in it. So the
rule is to take the line only where it is free, and this file is what keeps that
honest.

The trap it pins is one of **locality**. The obvious place to decide is per
circle, against its own diameter -- and that is wrong. In a coax the shield's
centre and the conductor's coincide; the shield's own diameter (10 cells across,
even) says "free", but the gap the line actually lands in is the *conductor's*,
and the shield pays for its free line out of the conductor's resolution. Only the
assembled forced-line set knows, which is why
:func:`~wavesim_gui.gridbuild._insert_centre_lines` runs where it does. The coax
case below fails against a per-circle test and passes against the real one.

What it checks
--------------
1. **The rule**, on a bare gap: free for a sub-cell circle and for an even span,
   withheld for an odd one, and re-judged against a dielectric body's tightened
   material cap.
2. **Which faces propose a centre** -- a full turn does, a fillet arc and a
   boolean-split half-bore do not (their "centre" is a construction point that
   need not touch the body).
3. **The invariant**, on whole documents: no circle is resolved by fewer cells
   across than before, the grid never loses cells overall, and mirror symmetry is
   no worse -- including the coax whose conductor the naive rule coarsened.

``freecadcmd`` swallows ``stdout``, so the report is also written to
``tools/circle_centres.txt`` (override with ``CIRCLE_CENTRES_REPORT``). Exits
non-zero on failure.
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
from wavesim_gui import gridbuild as gb                        # noqa: E402
from wavesim_gui import materials as mat_mod                   # noqa: E402

FMAX_HZ = 5.0e9          # -> a 1 mm background cell, the grid these were sized on
COARSE = 1.0

_report = []
_failures = []


def emit(line=""):
    _report.append(line)
    FreeCAD.Console.PrintMessage(line + "\n")


def check(ok, label, detail=""):
    _report.append("  [%s] %-46s %s" % ("ok" if ok else "FAIL", label, detail))
    FreeCAD.Console.PrintMessage(_report[-1] + "\n")
    if not ok:
        _failures.append(label)
    return ok


# --------------------------------------------------------------------------- #
# 1. The free/not-free rule on a bare gap
# --------------------------------------------------------------------------- #

def takes_centre(lines, centre, coarse=COARSE, min_cell=0.0, caps=()):
    out = gb._insert_centre_lines(lines, [centre], coarse, min_cell, caps)
    return len(out) > len(lines)


def check_rule():
    emit("1. the rule, on a bare gap (coarse %.1f)" % COARSE)
    for span, want, why in ((0.6, True, "circle inside one cell: 1 -> 2 cells"),
                            (1.0, True, "n=1"),
                            (2.0, True, "n=2, even"),
                            (3.0, False, "n=3, odd -> would drop to 2"),
                            (4.0, True, "n=4, even"),
                            (5.0, False, "n=5, odd"),
                            (10.0, True, "n=10, even")):
        got = takes_centre([0.0, span], span / 2.0)
        check(got == want, "gap %4.1f -> %s" % (span, want), why)

    check(not takes_centre([0.0, 2.0, 4.0], 2.0),
          "a candidate already forced adds nothing")
    check(not takes_centre([0.0, 4.0], 2.0, min_cell=3.0),
          "a candidate inside the merge tolerance is dropped",
          "else single linkage would swallow the silhouettes")
    check(takes_centre([0.0, 3.0], 1.5, caps=[(0.0, 3.0, 0.5)]),
          "a material cap is what the odd span is judged against",
          "target 0.5 -> n=6, even, so this one is free")


# --------------------------------------------------------------------------- #
# 2. Which faces propose a centre at all
# --------------------------------------------------------------------------- #

def proposals(shape):
    axes, centres = ([], [], []), ([], [], [])
    gb._add_cylinder_snaps(shape, axes, centres)
    return tuple(sorted(set(round(v, 6) for v in c)) for c in centres)


def check_proposals():
    emit()
    emit("2. which cylindrical faces propose a centre")
    ce = proposals(Part.makeCylinder(2.0, 10.0, FreeCAD.Vector(3, -2, 0)))
    check(ce[0] == [3.0] and ce[1] == [-2.0],
          "a rod proposes both transverse centres", "x=%s y=%s" % (ce[0], ce[1]))

    plate = Part.makeBox(40, 40, 4, FreeCAD.Vector(-20, -20, 0))
    bore = plate.cut(Part.makeCylinder(3.0, 20.0, FreeCAD.Vector(6, 7, -5)))
    ce = proposals(bore)
    check(ce[0] == [6.0] and ce[1] == [7.0],
          "an interior bore proposes its centre",
          "the feature a bounding box cannot see")

    box = Part.makeBox(10, 10, 10)
    ce = proposals(box.makeFillet(2.0, list(box.Edges)))
    check(ce == ([], [], []), "fillet arcs propose nothing",
          "a 90 deg blend has no circle centre on the body")

    half = bore.cut(Part.makeBox(40, 40, 40, FreeCAD.Vector(6, -20, -5)))
    ce = proposals(half)
    check(ce == ([], [], []), "a boolean-split half-bore proposes nothing",
          "conservative: it loses a line it could have had")


# --------------------------------------------------------------------------- #
# 3. The invariant, on whole documents
# --------------------------------------------------------------------------- #

def coax(radius):
    def build(doc):
        inner = doc.addObject("Part::Feature", "Inner")
        inner.Shape = Part.makeCylinder(radius, 20.0)
        shield = doc.addObject("Part::Feature", "Shield")
        shield.Shape = (Part.makeCylinder(6.0, 20.0)
                        .cut(Part.makeCylinder(5.0, 20.0)))
        return [inner, shield]
    return build


def boxed_wire(doc):
    wire = doc.addObject("Part::Feature", "Wire")
    wire.Shape = Part.makeCylinder(0.3, 20.0)
    shell = doc.addObject("Part::Feature", "Shell")
    shell.Shape = (Part.makeBox(12, 12, 20, FreeCAD.Vector(-6, -6, 0))
                   .cut(Part.makeBox(10, 10, 20, FreeCAD.Vector(-5, -5, 0))))
    return [wire, shell]


def meshes(build_bodies):
    """``(without centre lines, with them)`` node arrays for one document."""
    doc = FreeCAD.newDocument("circlecentres")
    try:
        sim = doc.addObject("App::DocumentObjectGroupPython", "Simulation")
        cmd.SimulationContainer(sim)
        dom = domain_mod.create_domain(doc, sim)
        _vacuum, pec = mat_mod.create_default_materials(doc, sim)
        pec.Bodies = build_bodies(doc)
        sim.MaxFrequency = FMAX_HZ
        doc.recompute()
        original = gb._is_full_circle
        gb._is_full_circle = lambda face: False   # propose nothing
        try:
            before = [list(a) for a in gb.build_domain_nodes(sim, dom)]
        finally:
            gb._is_full_circle = original
        after = [list(a) for a in gb.build_domain_nodes(sim, dom)]
        return before, after
    finally:
        FreeCAD.closeDocument(doc.Name)


def cells(nodes):
    return (len(nodes[0]) - 1) * (len(nodes[1]) - 1) * (len(nodes[2]) - 1)


def across(nodes, radius):
    """Cells spanning ``[-radius, radius]`` on x -- the circle's own resolution."""
    return sum(1 for v in nodes[0][:-1] if -radius - 1e-6 <= v < radius - 1e-6)


def asymmetry(nodes):
    return max(max(abs(p - q) for p, q in zip(ax, sorted(-v for v in ax)))
               for ax in nodes[:2])


def check_documents():
    emit()
    emit("3. whole documents (fmax %.0f GHz -> %.1f mm background cell)"
         % (FMAX_HZ / 1e9, COARSE))
    cases = (
        # The coax the naive per-circle rule coarsened: 3 mm conductor, 1 mm
        # grid, and a shield sharing its centre.
        ("coax r=1.5 (odd span)", coax(1.5), 1.5, False),
        ("coax r=2.0 (even span)", coax(2.0), 2.0, True),
        ("wire r=0.3 in a shell", boxed_wire, 0.3, True),
    )
    for label, build, radius, wants_axis in cases:
        before, after = meshes(build)
        core = [round(v, 4) for v in after[0]
                if -radius - 1e-6 <= v <= radius + 1e-6]
        check(across(after, radius) >= across(before, radius),
              "%s: circle no coarser across" % label,
              "%d -> %d cells, core x=%s"
              % (across(before, radius), across(after, radius), core))
        check(cells(after) >= cells(before),
              "%s: grid no coarser overall" % label,
              "%d -> %d cells" % (cells(before), cells(after)))
        check(asymmetry(after) <= asymmetry(before) + 1e-9,
              "%s: mirror symmetry no worse" % label,
              "%.3g -> %.3g" % (asymmetry(before), asymmetry(after)))
        on_axis = (any(abs(v) < 1e-9 for v in after[0]) and
                   any(abs(v) < 1e-9 for v in after[1]))
        check(on_axis == wants_axis,
              "%s: node on the axis == %s" % (label, wants_axis),
              "" if wants_axis else "withheld: splitting would cost a cell")


def main():
    emit("Circle-centre snap gate")
    check_rule()
    check_proposals()
    check_documents()

    emit()
    emit("FAILED: " + "; ".join(_failures) if _failures else "All checks passed.")
    path = os.environ.get(
        "CIRCLE_CENTRES_REPORT",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "circle_centres.txt"))
    with open(path, "w") as fh:
        fh.write("\n".join(_report) + "\n")
    if _failures:
        sys.exit(1)


main()
