# -*- coding: utf-8 -*-
"""Gate: per-body "N cells across" refinement does what it says, and only there.

Run under **FreeCAD's bundled Python** (it needs ``Part``), not the solver conda
Python::

    "%LOCALAPPDATA%\\Programs\\FreeCAD 1.1\\bin\\freecadcmd.exe" \\
        tools/check_body_refinement.py

What it pins
------------
1. **Absent means absent.** A document where nothing is refined builds a
   bit-identical mesh -- the whole reason the request lives in a property a body
   may not carry, rather than in a default that would move every existing model.
2. **N is honoured, and it is a floor.** A body whose span carries no interior
   forced line gets *exactly* N cells across; one whose faces already split the
   span gets at least N. The distinction is real and is what the task panel
   warns about, so it is asserted rather than glossed.
3. **It is local.** Refining one body does not refine the other, and the
   background cell far from both is untouched -- the graded fill is what carries
   the fine cells back out, so the cost is bounded.
4. **The caps compose the right way round.** A manual request refines a
   dielectric band further, and a *coarser* manual request does not undo the
   material sizing: ``_gap_coarse`` takes the smallest cap covering a gap.
5. **MinCellSize still wins**, and the mesh stays mirror-symmetric for symmetric
   geometry (the invariant every snapper change is measured against).

``freecadcmd`` swallows ``stdout``, so the report is also written to
``tools/body_refinement.txt`` (override with ``BODY_REFINEMENT_REPORT``). Exits
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
from wavesim_gui import refine as refine_mod                   # noqa: E402

FMAX_HZ = 5.0e9          # -> a 1 mm background cell, as in check_circle_centres

# Background gap that clears the absorber shell (PMLThickness 8 cells of the
# 1 mm background target): the snapper forces no line inside the PML, so a body
# must sit clear of it for a refinement request to mean anything.
_CLEARANCE_MM = 10.0
COARSE = 1.0

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
# Document scaffolding
# --------------------------------------------------------------------------- #

class Model(object):
    """A live document with two boxes -- one PEC, one dielectric -- in a row.

    Deliberately all-planar and axis-aligned: every bounding-box face is a forced
    grid line and nothing else is, so the cell count across a body is exactly
    what the cap asks for and a deviation is a real one rather than an interior
    feature. The curved case is checked separately below.
    """

    def __init__(self, eps=1.0):
        self.doc = FreeCAD.newDocument("bodyrefine")
        sim = self.doc.addObject("App::DocumentObjectGroupPython", "Simulation")
        cmd.SimulationContainer(sim)
        self.sim = sim
        self.dom = domain_mod.create_domain(self.doc, sim)
        _vacuum, self.pec = mat_mod.create_default_materials(self.doc, sim)

        self.metal = self.doc.addObject("Part::Feature", "Metal")
        self.metal.Shape = Part.makeBox(4, 20, 20, FreeCAD.Vector(-12, -10, -10))
        self.pec.Bodies = [self.metal]

        self.slab = mat_mod.create_material(self.doc, sim, "Slab")
        self.slab.Eps = eps
        self.diel = self.doc.addObject("Part::Feature", "Diel")
        self.diel.Shape = Part.makeBox(6, 20, 20, FreeCAD.Vector(4, -10, -10))
        self.slab.Bodies = [self.diel]

        sim.MaxFrequency = FMAX_HZ
        # Clear the absorber. The PML is taken out of the *inside* of the
        # domain box, and no grid line may be forced within it (the CPML needs
        # a constant-width shell), so a body sitting in the shell cannot be
        # refined at all -- correct, and not what this gate is measuring. A gap
        # wider than PMLThickness cells puts every body in the interior, where
        # the snapper is free.
        for _face, prop, _doc in domain_mod._SPACING_PROPS:
            setattr(self.dom, prop, "%g mm" % _CLEARANCE_MM)
        self.doc.recompute()

    def close(self):
        FreeCAD.closeDocument(self.doc.Name)

    def nodes(self):
        return [list(a) for a in gb.build_domain_nodes(self.sim, self.dom)]

    def refine(self, body, counts):
        refine_mod.set_body_cell_counts(body, counts)
        self.doc.recompute()


def across(nodes, axis, lo, hi):
    """Cells wholly inside ``[lo, hi]`` on *axis* -- what "N across" means."""
    a = nodes[axis]
    tol = 1.0e-9
    return sum(1 for i in range(len(a) - 1)
               if a[i] >= lo - tol and a[i + 1] <= hi + tol)


def widest(nodes, axis):
    a = nodes[axis]
    return max(a[i + 1] - a[i] for i in range(len(a) - 1))


def finest(nodes):
    return min(min(a[i + 1] - a[i] for i in range(len(a) - 1)) for a in nodes)


def asymmetry(nodes, axis):
    ax = nodes[axis]
    return max(abs(p - q) for p, q in zip(ax, sorted(-v for v in ax)))


# --------------------------------------------------------------------------- #
# 1. Absent means absent
# --------------------------------------------------------------------------- #

def check_absent():
    emit("1. a document with no refinement is untouched")
    m = Model()
    try:
        before = m.nodes()
        # The properties existing but reading zero must be the same case as the
        # properties not existing at all -- the panel's "Clear" leaves them at 0.
        refine_mod.set_body_cell_counts(m.metal, (0, 0, 0))
        m.doc.recompute()
        after = m.nodes()
        same = all(len(b) == len(a) and
                   all(abs(p - q) < 1e-12 for p, q in zip(b, a))
                   for b, a in zip(before, after))
        check(same, "zeroed properties give a bit-identical mesh",
              "%d x %d x %d nodes"
              % (len(after[0]), len(after[1]), len(after[2])))
        check(gb.collect_refinement_caps(mat_mod.find_materials(m.sim))
              == ([], [], []), "no body refined -> no caps emitted")
    finally:
        m.close()


# --------------------------------------------------------------------------- #
# 2. N is honoured, and it is a floor
# --------------------------------------------------------------------------- #

def check_counts():
    emit()
    emit("2. N cells across the bounding box (background %.1f mm)" % COARSE)
    m = Model()
    try:
        base = m.nodes()
        check(across(base, 0, -12.0, -8.0) == 4,
              "unrefined 4 mm PEC box: 4 cells across x", "the background size")

        for n in (8, 16, 40):
            m.refine(m.metal, (n, 0, 0))
            got = across(m.nodes(), 0, -12.0, -8.0)
            check(got == n, "PEC box asked %d across x -> %d" % (n, got),
                  "span/N = %.3f mm" % (4.0 / n))

        # 40 across the 20 mm y span = 0.5 mm. A *coarser* request than the
        # background would do nothing at all -- a cap only ever refines -- which
        # is checked in section 4.
        m.refine(m.metal, (16, 40, 0))
        nodes = m.nodes()
        check(across(nodes, 1, -10.0, 10.0) == 40,
              "a second axis is independent", "y: 40 across a 20 mm span")
        check(across(nodes, 2, -10.0, 10.0) == across(base, 2, -10.0, 10.0),
              "the axis left at 0 is unchanged",
              "z: %d cells" % across(nodes, 2, -10.0, 10.0))
        m.refine(m.metal, (0, 0, 0))

        # A body whose own faces split its span meshes finer than asked, never
        # coarser: the panel says so, so the gate had better agree.
        slotted = m.doc.addObject("Part::Feature", "Slotted")
        slotted.Shape = (Part.makeBox(4, 20, 20, FreeCAD.Vector(-12, -10, -10))
                         .cut(Part.makeBox(1, 30, 30,
                                           FreeCAD.Vector(-10.5, -15, -15))))
        m.pec.Bodies = [slotted]
        m.doc.removeObject(m.metal.Name)
        m.doc.recompute()
        plain = across(m.nodes(), 0, -12.0, -8.0)
        refine_mod.set_body_cell_counts(slotted, (5, 0, 0))
        m.doc.recompute()
        got = across(m.nodes(), 0, -12.0, -8.0)
        # Without _exact_target this is the case that comes out *coarser* than
        # the background: the slot walls cut the 4 mm span into 1.5/1.0/1.5 mm,
        # and the fill lays one stretched cell per piece.
        check(got >= 5, "an interior slot meshes finer than asked, not coarser",
              "asked 5 across x, got %d (unrefined: %d; slot walls at "
              "-10.5/-9.5 are forced lines)" % (got, plain))
    finally:
        m.close()


# --------------------------------------------------------------------------- #
# 3. It is local
# --------------------------------------------------------------------------- #

def check_locality():
    emit()
    emit("3. refinement stays where it was asked for")
    m = Model(eps=1.0)   # equal media, so only the manual request refines
    try:
        before = m.nodes()
        n_other_before = across(before, 0, 4.0, 10.0)
        m.refine(m.metal, (40, 0, 0))
        after = m.nodes()
        check(across(after, 0, -12.0, -8.0) == 40,
              "the refined body gets its 40 cells")
        check(across(after, 0, 4.0, 10.0) == n_other_before,
              "the unrefined body keeps its mesh",
              "%d cells across, both ways" % n_other_before)
        # The fine cells grade back out, so the void keeps its background size --
        # up to _graded_widths' longstanding scale-to-fit, which stretches a
        # gap's cells by whatever sub-cell remainder is left over. That is
        # pre-existing and bounded by the grading ratio; what matters here is
        # that a 0.1 mm request does not drag the far side of the domain with it.
        ratio = float(m.dom.MaxGradingRatio)
        check(widest(after, 0) <= widest(before, 0) * ratio,
              "the void keeps its background cell size",
              "coarsest cell %.4g -> %.4g mm (bound %.4g)"
              % (widest(before, 0), widest(after, 0),
                 widest(before, 0) * ratio))
        check(after[1] == before[1] and after[2] == before[2],
              "the untouched axes are bit-identical")
    finally:
        m.close()


# --------------------------------------------------------------------------- #
# 4. How the caps compose
# --------------------------------------------------------------------------- #

def check_composition():
    emit()
    emit("4. manual and material caps compose by taking the smallest")
    # eps=36 -> c0/(fmax * N_lambda * 6) = 0.5 mm, half the 1 mm background, so
    # the slab is already refined before anything manual is asked for.
    m = Model(eps=36.0)
    try:
        auto = across(m.nodes(), 0, 4.0, 10.0)
        check(auto == 12, "eps=36 slab is already 12 cells across (0.5 mm)",
              "got %d" % auto)

        m.refine(m.diel, (24, 0, 0))
        check(across(m.nodes(), 0, 4.0, 10.0) == 24,
              "a finer manual request refines it further", "0.25 mm")

        m.refine(m.diel, (3, 0, 0))
        got = across(m.nodes(), 0, 4.0, 10.0)
        check(got == auto,
              "a coarser manual request cannot undo the material sizing",
              "asked 3 (2 mm), still %d cells" % got)
    finally:
        m.close()


# --------------------------------------------------------------------------- #
# 5. MinCellSize, and symmetry
# --------------------------------------------------------------------------- #

def check_limits():
    emit()
    emit("5. the Domain's floor, and mirror symmetry")
    m = Model(eps=1.0)
    try:
        m.dom.MinCellSize = "0.5 mm"
        m.refine(m.metal, (40, 0, 0))   # would want 0.1 mm
        nodes = m.nodes()
        check(finest(nodes) >= 0.5 - 1e-9,
              "MinCellSize floors the request",
              "finest cell %.4g mm against a 0.1 mm request" % finest(nodes))
        check(across(nodes, 0, -12.0, -8.0) == 8,
              "so the body meshes at the floor instead",
              "8 cells of 0.5 mm across 4 mm")
        m.dom.MinCellSize = "0 mm"

        # A body centred on the origin, refined, must stay mirror-symmetric --
        # the invariant every snapper change is measured against.
        centred = m.doc.addObject("Part::Feature", "Centred")
        centred.Shape = Part.makeBox(6, 20, 20, FreeCAD.Vector(-3, -10, -10))
        m.pec.Bodies = [centred]
        m.slab.Bodies = []
        m.doc.removeObject(m.metal.Name)
        m.doc.removeObject(m.diel.Name)
        refine_mod.set_body_cell_counts(centred, (24, 0, 0))
        m.doc.recompute()
        nodes = m.nodes()
        check(asymmetry(nodes, 0) < 1e-9,
              "a centred refined body meshes mirror-symmetrically",
              "max |x + mirror(x)| = %.3g mm" % asymmetry(nodes, 0))
    finally:
        m.close()


def main():
    emit("Per-body mesh refinement gate")
    check_absent()
    check_counts()
    check_locality()
    check_composition()
    check_limits()

    emit()
    emit("FAILED: " + "; ".join(_failures) if _failures else "All checks passed.")
    path = os.environ.get(
        "BODY_REFINEMENT_REPORT",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "body_refinement.txt"))
    with open(path, "w") as fh:
        fh.write("\n".join(_report) + "\n")
    if _failures:
        sys.exit(1)


main()
