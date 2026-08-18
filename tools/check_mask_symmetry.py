# -*- coding: utf-8 -*-
"""Gate: a mirror-symmetric body on a mirror-symmetric grid voxelises symmetric.

Run under **FreeCAD's bundled Python** (it needs ``Part``), not the solver conda
Python::

    "%LOCALAPPDATA%\\Programs\\FreeCAD 1.1\\bin\\freecadcmd.exe" \\
        tools/check_mask_symmetry.py

Why this is a gate rather than a nicety
---------------------------------------
``Wire.discretize(Deflection=)`` walks from the wire's seam, so the section
polygon of a circle is **not** mirror-symmetric: its boundary radius differs
between +x and -x by up to the deflection. Any cell centre landing in that
window is decided by which chord it happens to fall under, and the two mirror
partners get opposite answers. Four stray cells per cross-section is enough --
a ``pec_mask`` with a dipole moment scatters TEM into a mode a
:class:`ModalPort` cannot absorb, which shows up as a static m = 1 field pinned
at both port planes and a DC offset the current monitor never sheds. Measured on
the coax below at the pre-2026-08-08 tolerance: residual field at the port
-31 dB and a 0.5%-of-peak DC current that decays with a ~110 ns time constant;
at :data:`~wavesim_gui.voxelize.COARSE_CHORD_FRACTION` = 0.0025, -129 dB and
9e-12 A.

The failure is bounded by the deflection but **not monotone** in it (0.05 is
worse than 0.25), so there is no safety factor to reason about and no
substitute for asserting the symmetry directly.

What it checks
--------------
1. **Mirror symmetry** of ``pec_mask`` and the ``eps_*`` arrays about all three
   axes, in all three voxelisation modes (plain / subpixel /
   conformal), on a graded grid -- the case that broke, since a uniform grid can
   hide it by keeping every cell centre clear of the window.
2. **The conformal open fractions** are mirror-symmetric too, with the correct
   per-array index convention (an edge-spanning axis mirrors as a reversal, a
   node-indexed one about node ``N``).
3. Two **regression pins** on where the symmetry comes from:

   * with exact section curves suppressed, the old 0.25 tolerance still fails
     (1) -- so a revert of the chord fractions cannot pass this file silently
     wherever chords are still what a section is made of;
   * with them on, this coax voxelises **bit-identically at both tolerances** --
     its sections are circles and straight lines, so no chord tolerance can
     touch them. That is the pin on the exact-curve path itself: fall back to
     ``discretize`` and the two runs stop agreeing.
4. A third pin on the exact axis-aligned-edge tie-break, which the transverse
   geometry needs and no chord tolerance can substitute for.

``freecadcmd`` swallows ``stdout``, so the report is also written to
``tools/mask_symmetry.txt`` (override with ``MASK_SYMMETRY_REPORT``). Exits
non-zero on failure.
"""

import os
import sys

import numpy as np
import FreeCAD
import Part

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:                                                           # noqa: E402
    import FreeCADGui
    if not hasattr(FreeCADGui, "addCommand"):
        FreeCADGui.addCommand = lambda *a, **k: None
except ImportError:
    pass

from wavesim_gui import voxelize as vox                        # noqa: E402

# A coax is the right shape for this: circular, so every tolerance question is
# live, and its centre sits on a node while the grid is graded around it.
A_MM, B_MM, BOX_MM = 2.5, 9.0, 20.0
CX = CY = CZ = BOX_MM / 2.0
LEN_MM = 6.0
NZ = 6

_report = []
_failures = []


def emit(line=""):
    _report.append(line)
    FreeCAD.Console.PrintMessage(line + "\n")


def check(ok, label):
    _report.append("  [%s] %s" % ("ok" if ok else "FAIL", label))
    FreeCAD.Console.PrintMessage(_report[-1] + "\n")
    if not ok:
        _failures.append(label)
    return ok


# --------------------------------------------------------------------------- #
# Geometry + a graded, mirror-symmetric grid
# --------------------------------------------------------------------------- #

class _Body(object):
    """The two attributes ``voxelize._gather`` reads off an App object."""

    def __init__(self, shape, name):
        self.Shape = shape
        self.Name = self.Label = name


class _Mat(object):
    def __init__(self, label, eps, mu, pec, bodies):
        self.Label = label
        self.Eps, self.Mu, self.Pec, self.Sigma = eps, mu, pec, 0.0
        self.Bodies = bodies


def build_materials():
    """The coax lying **along** the sectioning axis: every section is a circle."""
    axis = FreeCAD.Vector(CX, CY, 0.0)
    inner = Part.makeCylinder(A_MM, LEN_MM, axis)
    outer = Part.makeCylinder(B_MM, LEN_MM, axis)
    shield = Part.makeBox(BOX_MM, BOX_MM, LEN_MM).cut(outer)
    return [
        _Mat("PEC", 1.0, 1.0, True,
             [_Body(inner, "Inner"), _Body(shield, "Shield")]),
        _Mat("Dielectric", 2.3, 1.0, False, [_Body(outer.cut(inner), "Diel")]),
    ]


def build_materials_transverse():
    """The same coax lying **across** the sectioning axis -- a different failure.

    The voxeliser cuts its sections on z planes. With the coax along z (above)
    every section is a circle, so a node tangent to the bore misses the section
    polygon by chord error and the fill rule is never asked a tie. Turn the coax
    across the cut and each section is an exact **rectangle** whose edges lie
    along the tangent node planes -- and matplotlib's crossing rule, which is
    half-open in the row direction, answers the two mirror-image edges
    differently. That is the bent_coax port's 1-against-0 split of
    ``pec_edge_open_x``, and no round conductor on a transverse line avoids it,
    because the snapper puts nodes on feature extents on purpose.
    """
    base = FreeCAD.Vector(0.0, CY, CZ)
    x_dir = FreeCAD.Vector(1.0, 0.0, 0.0)
    inner = Part.makeCylinder(A_MM, LEN_MM, base, x_dir)
    outer = Part.makeCylinder(B_MM, LEN_MM, base, x_dir)
    shield = Part.makeBox(LEN_MM, BOX_MM, BOX_MM).cut(outer)
    return [
        _Mat("PEC", 1.0, 1.0, True,
             [_Body(inner, "Inner"), _Body(shield, "Shield")]),
        _Mat("Dielectric", 2.3, 1.0, False, [_Body(outer.cut(inner), "Diel")]),
    ]


def graded_axis():
    """Mirror-symmetric graded nodes across ``[0, BOX_MM]`` (metres).

    Symmetric *by construction* (built on one half and reflected) so a failure
    can only come from the voxeliser, never from the ruler.
    """
    half = [0.0, 0.6, 1.0]
    x = 1.0
    while x < CX - 1e-9:
        x = min(x + 0.40625, CX)
        half.append(x)
    half = np.array(half)
    nodes = np.concatenate([half[:-1], [CX], BOX_MM - half[::-1][1:]])
    return nodes / 1000.0


def nodes():
    ax = graded_axis()
    return ax, ax.copy(), np.linspace(0.0, LEN_MM / 1000.0, NZ + 1)


def nodes_transverse():
    """The same graded ruler on the two axes the transverse coax is round in."""
    ax = graded_axis()
    return np.linspace(0.0, LEN_MM / 1000.0, NZ + 1), ax, ax.copy()


# --------------------------------------------------------------------------- #
# Mirror conventions
# --------------------------------------------------------------------------- #
# A cell-centred array (pec_mask, eps_*) mirrors as a plain reversal. A Yee
# fraction array mirrors per axis according to what that axis indexes: an axis
# the quantity *spans* (an Ex edge along x, an Hz face across x) reverses; an
# axis it sits *on* a node of maps node i <-> node N, so index 0 has no partner
# and the comparison starts at 1.
#
# All **three** axes. This table was 2-tuples and the loop below ran axes 0 and 1
# only, which is why a tangency on the third axis could sit here unseen: the
# double-sphere spark gap (poles on z) split two identical conductors' self-
# capacitance by 2.3% with every checked axis clean, and the bent_coax port split
# ``pec_edge_open_x`` 1-against-0 on its own tangent plane. z is not special --
# it was just the axis nothing asked about.
_FRACTION_AXES = {
    "pec_edge_open_x": ("span", "node", "node"),
    "pec_edge_open_y": ("node", "span", "node"),
    "pec_edge_open_z": ("node", "node", "span"),
    "pec_face_open_x": ("node", "span", "span"),
    "pec_face_open_y": ("span", "node", "span"),
    "pec_face_open_z": ("span", "span", "node"),
}


# ``pec_mask`` is boolean and must mirror *exactly*; the float arrays cannot,
# because the subpixel reduction is a sum over sub-cells and re-associating it
# under a mirror costs a couple of ULP. Anything above this is geometry, not
# arithmetic -- the failures this file exists to catch are 0.09 and 0.125, three
# orders clear of it.
ROUNDOFF = 1.0e-12


def mirror_error(a, axis, kind="span"):
    a = np.moveaxis(a, axis, 0)
    if kind == "node":
        a = a[1:]
    return float(np.abs(np.asarray(a, float) - np.asarray(a[::-1], float)).max())


# The three chord tolerances, as (coarse, subpixel, conformal). Each sampler has
# its own because each samples a different ruler; all three had the same bug.
SHIPPED = (vox.COARSE_CHORD_FRACTION, vox._SUBPIXEL_CHORD_FRACTION,
           vox._CONFORMAL_CHORD_FRACTION)
PRE_FIX = (0.25, 0.25, 0.05)


def set_fractions(fr):
    (vox.COARSE_CHORD_FRACTION, vox._SUBPIXEL_CHORD_FRACTION,
     vox._CONFORMAL_CHORD_FRACTION) = fr


def run(fractions, label, expect_symmetric=True, geometry=None, modes=None,
        check_axes=(0, 1, 2)):
    """Voxelise in each mode and assert the symmetry; returns ``{mode: arrays}``."""
    set_fractions(fractions)
    build, node_fn = geometry or (build_materials, nodes)
    mats, nm = build(), node_fn()
    out = {}
    emit()
    emit("%s (coarse/subpixel/conformal chord = %g / %g / %g)"
         % ((label,) + tuple(fractions)))
    for mode, kw in (("plain", {}), ("subpixel", {"subpixel": True}),
                     ("conformal", {"conformal": True})):
        if modes is not None and mode not in modes:
            continue
        res = vox.voxelize_materials(mats, (4.0e-4,) * 3, nodes_m=nm, **kw)
        arrays = res["arrays"]
        out[mode] = arrays
        worst, where = 0.0, ""
        for key, arr in arrays.items():
            axes = _FRACTION_AXES.get(key, ("span", "span", "span"))
            for ax in (0, 1, 2):
                if ax not in check_axes:
                    continue
                err = mirror_error(arr, ax, axes[ax])
                if err > worst:
                    worst, where = err, "%s/%s" % (key, "xyz"[ax])
        # The boolean mask has no round-off excuse, so it is held to exact.
        pm = arrays["pec_mask"]
        pm_mirrors = (pm[::-1], pm[:, ::-1], pm[:, :, ::-1])
        mask_bad = sum(int((pm != pm_mirrors[ax]).sum())
                       for ax in (0, 1, 2) if ax in check_axes)
        if expect_symmetric:
            check(mask_bad == 0,
                  "%-9s pec_mask mirror-exact (%d cells differ)" % (mode, mask_bad))
            check(worst <= ROUNDOFF,
                  "%-9s float arrays mirror-symmetric to round-off "
                  "(worst %.3g on %s)" % (mode, worst, where or "-"))
        else:
            check(mask_bad > 0 or worst > ROUNDOFF,
                  "%-9s still asymmetric, as the old tolerance must be "
                  "(%d mask cells, worst float %.3g on %s)"
                  % (mode, mask_bad, worst, where or "-"))
        if mode == "plain":
            emit("            pec_cells %d, dielectric_cells %d"
                 % (res["counts"]["pec_cells"],
                    res["counts"]["dielectric_cells"]))
    return out


def _without_analytic_sections():
    """Put every section back on a chord polygon, the way it was cut before.

    ``voxelize._cut_section`` offers each closed wire to ``_analytic_wire``
    first, so replacing that one function is enough to restore ``discretize``
    everywhere -- which is the only way left to pin what the chord tolerances buy,
    now that this coax's own sections are exact and answer the same at any
    tolerance.
    """
    keep = vox._analytic_wire
    vox._analytic_wire = lambda wire: None
    return keep


def _without_boundary_tiebreak():
    """Disable the on-surface tie-break, the way the code behaved before it.

    ``voxelize`` imports these two inside the functions that use them, so
    replacing them on the module is enough -- and it keeps the switch here, in
    the gate that needs it, rather than as a flag in shipping code.
    """
    from wavesim_gui import scanline

    keep = (scanline.on_axis_edge, scanline.on_axis_edge_points)
    scanline.on_axis_edge = lambda poly, xs, ys: np.zeros(
        (len(xs), len(ys)), bool)
    scanline.on_axis_edge_points = lambda poly, pts: np.zeros(len(pts), bool)
    return keep


def _restore_boundary_tiebreak(keep):
    from wavesim_gui import scanline

    scanline.on_axis_edge, scanline.on_axis_edge_points = keep


def main():
    emit("Voxeliser mask-symmetry gate")
    emit("coax a=%.1f b=%.1f in a %.0f mm box, graded grid, centre on a node"
         % (A_MM, B_MM, BOX_MM))
    transverse = (build_materials_transverse, nodes_transverse)
    try:
        shipped = run(SHIPPED, "shipped tolerances")
        # The chord fractions still decide every section a curve *has* to be
        # discretised for (splines, tori, an ellipse off a tilted cylinder), so
        # their pin survives -- on chord polygons, which is where they apply.
        keep = _without_analytic_sections()
        try:
            run(PRE_FIX, "pre-fix tolerances, chord polygons (regression pin)",
                expect_symmetric=False)
        finally:
            vox._analytic_wire = keep
        # ...and with the exact curves back, the same coax cannot even *see* the
        # tolerance: circles and straight lines, cut as themselves. Bit-identical
        # arrays are the pin on that -- a silent fall back to ``discretize`` moves
        # the shield's radius by the deflection and breaks this line.
        loose = run(PRE_FIX, "pre-fix tolerances, exact curves (chord is moot)")
        for mode in sorted(shipped):
            a, b = shipped[mode], loose[mode]
            same = (sorted(a) == sorted(b)
                    and all(np.array_equal(a[k], b[k]) for k in a))
            check(same, "%-9s bit-identical at both tolerances (sections are "
                        "exact, so no chord tolerance applies)" % mode)
        set_fractions(SHIPPED)
        run(SHIPPED, "transverse coax (sections are exact rectangles)",
            geometry=transverse)
        # The pin for the on-surface tie-break, conformal only -- it is the only
        # mode that samples on node lines, so it is the only one the tie-break
        # can move. Chord tolerance cannot serve as this pin: a plane parallel to
        # a cylinder's axis sections it exactly, so there is no polygon error to
        # loosen, only the fill rule's answer on the tangent lines.
        keep = _without_boundary_tiebreak()
        try:
            run(SHIPPED, "transverse coax, tie-break disabled (regression pin)",
                expect_symmetric=False, geometry=transverse,
                modes=("conformal",))
        finally:
            _restore_boundary_tiebreak(keep)
    finally:
        set_fractions(SHIPPED)

    emit()
    emit("FAILED: " + "; ".join(_failures) if _failures else "All checks passed.")
    path = os.environ.get(
        "MASK_SYMMETRY_REPORT",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "mask_symmetry.txt"))
    with open(path, "w") as fh:
        fh.write("\n".join(_report) + "\n")
    if _failures:
        sys.exit(1)


main()
