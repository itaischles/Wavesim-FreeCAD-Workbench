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
1. **Mirror symmetry** of ``pec_mask`` and the ``eps_*`` arrays about both
   in-plane axes, in all three voxelisation modes (plain / subpixel /
   conformal), on a graded grid -- the case that broke, since a uniform grid can
   hide it by keeping every cell centre clear of the window.
2. **The conformal open fractions** are mirror-symmetric too, with the correct
   per-array index convention (an edge-spanning axis mirrors as a reversal, a
   node-indexed one about node ``N``).
3. A **regression pin**: the old 0.25 tolerance still fails (1), so a revert
   cannot pass this file silently.

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
CX = CY = BOX_MM / 2.0
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
    axis = FreeCAD.Vector(CX, CY, 0.0)
    inner = Part.makeCylinder(A_MM, LEN_MM, axis)
    outer = Part.makeCylinder(B_MM, LEN_MM, axis)
    shield = Part.makeBox(BOX_MM, BOX_MM, LEN_MM).cut(outer)
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


# --------------------------------------------------------------------------- #
# Mirror conventions
# --------------------------------------------------------------------------- #
# A cell-centred array (pec_mask, eps_*) mirrors as a plain reversal. A Yee
# fraction array mirrors per axis according to what that axis indexes: an axis
# the quantity *spans* (an Ex edge along x, an Hz face across x) reverses; an
# axis it sits *on* a node of maps node i <-> node N, so index 0 has no partner
# and the comparison starts at 1.
_FRACTION_AXES = {
    "pec_edge_open_x": ("span", "node"), "pec_edge_open_y": ("node", "span"),
    "pec_edge_open_z": ("node", "node"), "pec_face_open_x": ("node", "span"),
    "pec_face_open_y": ("span", "node"), "pec_face_open_z": ("span", "span"),
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


def run(fractions, label, expect_symmetric=True):
    set_fractions(fractions)
    mats, nm = build_materials(), nodes()
    emit()
    emit("%s (coarse/subpixel/conformal chord = %g / %g / %g)"
         % ((label,) + tuple(fractions)))
    for mode, kw in (("plain", {}), ("subpixel", {"subpixel": True}),
                     ("conformal", {"conformal": True})):
        res = vox.voxelize_materials(mats, (4.0e-4,) * 3, nodes_m=nm, **kw)
        arrays = res["arrays"]
        worst, where = 0.0, ""
        for key, arr in arrays.items():
            axes = _FRACTION_AXES.get(key, ("span", "span"))
            for ax in (0, 1):
                err = mirror_error(arr, ax, axes[ax])
                if err > worst:
                    worst, where = err, "%s/%s" % (key, "xy"[ax])
        # The boolean mask has no round-off excuse, so it is held to exact.
        pm = arrays["pec_mask"]
        mask_bad = int((pm != pm[::-1]).sum() + (pm != pm[:, ::-1]).sum())
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


def main():
    emit("Voxeliser mask-symmetry gate")
    emit("coax a=%.1f b=%.1f in a %.0f mm box, graded grid, centre on a node"
         % (A_MM, B_MM, BOX_MM))
    try:
        run(SHIPPED, "shipped tolerances")
        run(PRE_FIX, "pre-fix tolerances (regression pin)",
            expect_symmetric=False)
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
