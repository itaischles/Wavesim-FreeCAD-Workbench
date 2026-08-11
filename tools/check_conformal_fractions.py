# -*- coding: utf-8 -*-
"""Phase-5 gate: the voxeliser's conformal PEC fractions vs the analytic coax.

Run under **FreeCAD's bundled Python** (it needs ``Part``), not the solver conda
Python::

    "%LOCALAPPDATA%\\Programs\\FreeCAD 1.1\\bin\\freecadcmd.exe" \\
        tools/check_conformal_fractions.py

Builds the reference coax of ``CONFORMAL_PEC_PLAN.md`` section 7 (a = 3 mm,
b = 9 mm, cell 0.5 mm) out of ``Part`` solids, voxelises it with
``conformal=True``, and compares all six open-fraction arrays against a
closed-form answer written here **independently of the voxeliser**. The maths
mirrors the solver's ``tests/conformal_shapes.py`` but is driven by node arrays
rather than an ``FDTDGrid``, so this side needs no ``import wavesim``.

It reports three things, in descending order of importance:

1. **Killed faces.** A face whose open fraction rounds to exactly 0 tells the
   solver "no contour" (``inv_A = 0``), which is the small-cut remedy S4
   measured as harmful (+5.77% against +0.21% for clamping). It is only safe
   when all four of its contour edges are covered too, so the tally that matters
   is faces killed while a contour edge is still live.
2. **Per-array error** against the closed form, next to the staircase each
   fraction would otherwise have been.
3. **V2 at the voxeliser** — conformal off emits no extra keys and leaves the
   binary arrays bit-identical.

``freecadcmd`` swallows ``stdout``, so the report is also written to
``tools/conformal_fractions.txt``. Note that it does **not** read ``sys.argv``:
``freecadcmd`` puts the script's own path in ``argv[1]``, so a naive
"output file" argument overwrites this source file with its own report.
Set ``CONFORMAL_REPORT`` to redirect it, ``CONFORMAL_OVERSAMPLE`` to change the
sampling density.
"""

import os
import sys
import time

import numpy as np
import FreeCAD
import Part

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ``freecadcmd`` ships a stub ``FreeCADGui`` that satisfies every gui module's
# ``try: import FreeCADGui`` guard but has no ``addCommand``, so importing
# anything under ``wavesim_gui`` raises on the command registration. Give it the
# one attribute the guards need; nothing here goes near a real command.
try:                                                           # noqa: E402
    import FreeCADGui
    if not hasattr(FreeCADGui, "addCommand"):
        FreeCADGui.addCommand = lambda *a, **k: None
except ImportError:
    pass

from wavesim_gui import voxelize as vox                        # noqa: E402

A_MM, B_MM, OUT_MM = 3.0, 9.0, 15.0
D_MM = 0.5
N = 36                      # transverse cells -> 18 mm across, coax on node 18
NZ = 3                      # z-invariant, so a few layers prove the whole line
CX = CY = 9.0

_report = []


def emit(line=""):
    _report.append(line)
    FreeCAD.Console.PrintMessage(line + "\n")


# --------------------------------------------------------------------------- #
# Analytic reference — closed form, independent of the voxeliser
# --------------------------------------------------------------------------- #

def _ann_len(u0, u1, v, r0, r1):
    """Length of ``[u0, u1]`` at offset *v* lying in the annulus ``r0<=r<=r1``.

    ``r >= r0`` iff ``|u| >= sqrt(r0^2 - v^2)`` and ``r <= r1`` iff
    ``|u| <= sqrt(r1^2 - v^2)``, so the set is the interval pair ``+-[s, t]``.
    """
    v2 = v * v
    if v2 > r1 * r1:
        return 0.0
    t = np.sqrt(max(r1 * r1 - v2, 0.0))
    s = np.sqrt(max(r0 * r0 - v2, 0.0))

    def ov(lo, hi):
        return max(0.0, min(u1, hi) - max(u0, lo))

    return ov(s, t) + ov(-t, -s)


def _covered_len(u0, u1, v):
    """Metal is the inner conductor ``r < a`` plus the shield ``b < r < OUT``."""
    return _ann_len(u0, u1, v, 0.0, A_MM) + _ann_len(u0, u1, v, B_MM, OUT_MM)


def _covered_pt(x, y):
    r = np.hypot(x, y)
    return (r < A_MM) | ((r > B_MM) & (r < OUT_MM))


def analytic(nodes_mm, nsub=256):
    """The six open-fraction arrays for the coax, in closed form where possible.

    z-invariance buys an exactness a general sampler could not: the Hx face
    spans (y, z) at x-node i and the geometry does not vary in z, so its open
    *area* fraction equals the open *length* fraction of the Ey edge at the same
    (i, j). Likewise Hy and Ex. Only the Hz face genuinely needs two dimensions.
    """
    nx, ny, nz = nodes_mm
    Nx, Ny, Nz = nx.size - 1, ny.size - 1, nz.size - 1
    x, y = nx - CX, ny - CY
    shape = (Nx, Ny, Nz)

    fx = np.zeros(shape)
    fy = np.zeros(shape)
    for i in range(Nx):
        dxi = nx[i + 1] - nx[i]
        for j in range(Ny):
            dyj = ny[j + 1] - ny[j]
            fx[i, j, :] = 1.0 - _covered_len(x[i], x[i + 1], y[j]) / dxi
            fy[i, j, :] = 1.0 - _covered_len(y[j], y[j + 1], x[i]) / dyj

    # Ez runs along z at fixed (x, y), so r is constant on it: all in or all out.
    # The solids overhang the grid in z, so there is no end effect.
    fz = np.broadcast_to(
        (~_covered_pt(x[:Nx, None], y[None, :Ny]))[:, :, None], shape
    ).astype(float)

    faz = np.zeros(shape)
    q = (np.arange(nsub) + 0.5) / nsub
    for i in range(Nx):
        xs = x[i] + q * (nx[i + 1] - nx[i])
        for j in range(Ny):
            ys = y[j] + q * (ny[j + 1] - ny[j])
            faz[i, j, :] = np.mean(~_covered_pt(xs[:, None], ys[None, :]))

    return {
        "pec_edge_open_x": np.clip(fx, 0, 1),
        "pec_edge_open_y": np.clip(fy, 0, 1),
        "pec_edge_open_z": fz,
        "pec_face_open_x": np.clip(fy, 0, 1),
        "pec_face_open_y": np.clip(fx, 0, 1),
        "pec_face_open_z": faz,
    }


# --------------------------------------------------------------------------- #
# Geometry — duck-typed stand-ins for the Material / body document objects
# --------------------------------------------------------------------------- #

class _Body(object):
    def __init__(self, shape):
        self.Shape = shape


class _Mat(object):
    def __init__(self, bodies, pec):
        self.Bodies = bodies
        self.Pec = pec
        self.Eps = 1.0
        self.Mu = 1.0


def build(flush=False):
    """Inner conductor + shield.

    *flush* puts their end faces exactly on the z = 0 and z = NZ*D_MM node
    planes instead of overhanging -- which is what a transmission line running
    the length of the domain actually looks like, and the case an overhanging
    reference cannot test. OCC's ``Shape.slice`` returns *nothing* for a plane
    inside its tolerance band of a planar face, so before
    ``voxelize._section_nudge`` those node planes sectioned as empty and the
    three quantities carried on them (``edge_x``, ``edge_y``, ``face_z``) read
    "no metal at all" across the whole z = 0 plane.
    """
    z0, h = (0.0, NZ * D_MM) if flush else (-1.0, 6.0)
    base = FreeCAD.Vector(CX, CY, z0)
    inner = Part.makeCylinder(A_MM, h, base)
    shield = Part.makeCylinder(OUT_MM, h, base).cut(
        Part.makeCylinder(B_MM, h + 2.0, FreeCAD.Vector(CX, CY, z0 - 1.0)))
    return _Mat([_Body(inner), _Body(shield)], True)


TAN_BOX, TAN_WALL, TAN_R = 10.0, 1.5, 2.5
TAN_CZ, TAN_CY = 5.0, 5.13      # bore axis: tangent in z on a node plane, and
                                # off the lattice in y so no sample point sits
                                # on the tangent line itself


def check_tangent_plane():
    """A node plane tangent to a curved face must section like its neighbours.

    The mirror of the flush-ends case below, and the more dangerous half: a
    plane inside OCC's tolerance band of a *planar* face sections to nothing,
    which reads as "no metal", while a plane **tangent to a curved** one
    sections to a single wire that does not close -- and an open wire
    discretises to a polygon that matplotlib's fill rule closes for it, which is
    geometry the body does not have.

    This is not a corner case a model has to go looking for. ``gridbuild`` snaps
    grid lines onto every material bbox face, so a jacket of radius r puts node
    planes exactly at +-r, and any conductor sharing that radius -- the casing
    bore the cable passes through -- is tangent to them by construction. On
    ``floating_shield_on_unshielded_coax_in_a_box`` those two planes came out
    97.8% and 98.5% "inside the casing" against 23% on the planes either side,
    which zeroed 3564 of 3564 y-edge fractions on each, and the electrostatic
    solve took the two sheets for conductor and walled the cable off from the
    rest of the box: every field in the domain read identically zero.

    A hollow box with a bore through one wall reproduces the open wire exactly.
    The assertion needs no reference geometry and no tolerance: the section of a
    solid varies continuously in z, so the layer at the tangent plane must equal
    the layer a hair either side of it, point for point.
    """
    z = FreeCAD.Vector(0.0, 0.0, 1.0)
    shell = Part.makeBox(TAN_BOX, TAN_BOX, TAN_BOX).cut(
        Part.makeBox(TAN_BOX - 2 * TAN_WALL, TAN_BOX - 2 * TAN_WALL,
                     TAN_BOX - 2 * TAN_WALL,
                     FreeCAD.Vector(TAN_WALL, TAN_WALL, TAN_WALL)))
    shape = shell.cut(Part.makeCylinder(
        TAN_R, TAN_WALL + 1.0,
        FreeCAD.Vector(-0.5, TAN_CY, TAN_CZ), FreeCAD.Vector(1, 0, 0)))

    xs = np.arange(int(TAN_BOX / D_MM) + 1) * D_MM
    sub = D_MM / vox.CONFORMAL_OVERSAMPLE
    deflection = max(vox._CONFORMAL_CHORD_FRACTION * sub, sub * 1.0e-6)
    nudge = vox._section_nudge(shape, sub)
    eps = 1.0e-3                # a hair, but 5x the nudge and 1/500 of a cell

    def layer(zz):
        return vox._layer_inside_lattice(shape, z, zz, xs, xs, deflection,
                                         nudge)

    def as_drawn(zz):
        """The layer with open wires filled -- what this did before the fix."""
        from wavesim_gui.scanline import lattice_inside
        out = np.zeros((xs.size, xs.size), dtype=bool)
        n_open = 0
        for w in shape.slice(z, zz):
            if not w.isClosed():
                n_open += 1
            verts = w.discretize(Deflection=deflection)
            if len(verts) >= 3:
                out ^= lattice_inside(
                    np.array([(v.x, v.y) for v in verts]), xs, xs)
        return out, n_open

    emit()
    emit("node plane tangent to a curved face (bore r=%.1f, tangent at z=%.1f "
         "and %.1f)" % (TAN_R, TAN_CZ - TAN_R, TAN_CZ + TAN_R))
    ok = True
    for zt in (TAN_CZ - TAN_R, TAN_CZ + TAN_R):
        at, lo, hi = layer(zt), layer(zt - eps), layer(zt + eps)
        drawn, n_open = as_drawn(zt)
        differ = (0 if at is None or lo is None
                  else int(np.count_nonzero(at != lo)))
        differ += (0 if at is None or hi is None
                   else int(np.count_nonzero(at != hi)))
        # An OCC that stops returning the open wire would make this section pass
        # without testing anything, so say which case actually ran.
        emit("  z=%4.1f  open wires in the section: %d" % (zt, n_open))
        emit("           samples differing from a hair either side: %d of %d"
             % (differ, 2 * at.size if at is not None else 0))
        emit("           filling the open wire instead: %d differ (%.1f%% of "
             "the plane inside, against %.1f%%)"
             % (int(np.count_nonzero(drawn != lo)), 100.0 * drawn.mean(),
                100.0 * lo.mean()))
        ok = ok and at is not None and differ == 0
    return ok


def check_pec_material_fill(cell, nodes_m, ovr):
    """The conductor cells must carry the material of the medium beside them.

    A PEC cell's eps/mu is meaningless -- nothing propagates inside metal -- so
    the sweep leaves the *background* value there. The staircase path never reads
    it, because ``pec.build_pec_edge_masks`` dilates and zeroes every edge that
    touches a metal cell. Conformal PEC drops that dilation on purpose (plan S3),
    so the edges running just outside a conductor stay alive and each one reads
    its material from the cell it is **indexed** by -- which, on the low-index
    side of a surface, is the cell whose centre sits in the metal.

    Left alone that puts eps_r = 1 on the live edges hugging one side of each
    conductor and the fill's eps_r on the other. The one-sidedness is the damage:
    the mode's ê stops being a null vector of the FDTD's own transverse curl at
    the conductor-adjacent free nodes, so a ``ModalPort``'s sheet deposits a
    static field on its own plane every step. Measured on the workbench's
    ``coaxial_line`` case, port-plane residual −8.6 dB of peak and a 1 mA DC
    current down the line, against −104 dB and −158 dB once the conductor cells
    carry the surrounding 2.3.

    This is the air-filled coax of :func:`build` with a dielectric annulus added,
    which is the smallest model that can show the defect at all (with an air fill
    the background value *is* the right answer and the bug is invisible). Two
    assertions, and the second is the regression pin:

    * conformal **off** -- the conductor cells still read the background, so a
      staircase ``materials.npz`` is what it always was (V2);
    * conformal **on** -- every PEC cell that still owns an open Yee edge or face
      reads the annulus permittivity.
    """
    eps_fill = 2.3
    z0, h = 0.0, NZ * D_MM
    base = FreeCAD.Vector(CX, CY, z0)
    annulus = Part.makeCylinder(B_MM, h, base).cut(
        Part.makeCylinder(A_MM, h + 2.0, FreeCAD.Vector(CX, CY, z0 - 1.0)))
    diel = _Mat([_Body(annulus)], False)
    diel.Eps = eps_fill
    # The dielectric is listed first: a later body wins the overlap, and the
    # conductors must keep the cells they share with the annulus.
    mats = [diel, build(flush=True)]

    off = vox.voxelize_materials(mats, cell, nodes_m=nodes_m, subpixel=False)
    on = vox.voxelize_materials(mats, cell, nodes_m=nodes_m, subpixel=False,
                                conformal=True, conformal_oversample=ovr)
    a_off, a_on = off["arrays"], on["arrays"]
    pec = a_on["pec_mask"]

    emit()
    emit("PEC cells carry the neighbouring medium (eps_r = %.1f annulus)" % eps_fill)
    off_bg = bool(np.all(a_off["eps_x"][pec] == 1.0))
    emit("  conformal off: conductor cells still read the background : %s"
         % ("yes" if off_bg else "NO"))

    readable = np.zeros(pec.shape, dtype=bool)
    for key in vox.CONFORMAL_KEYS:
        readable |= a_on[key] > 0.0
    readable &= pec
    ok_fill = True
    for key in ("eps_x", "eps_y", "eps_z"):
        bad = int(np.count_nonzero(a_on[key][readable] != eps_fill))
        emit("  conformal on : %-6s wrong on %d of %d readable PEC cells"
             % (key, bad, int(readable.sum())))
        ok_fill = ok_fill and bad == 0
    emit("  cells filled: %d" % on["counts"].get("pec_material_cells", 0))
    return off_bg and ok_fill and int(readable.sum()) > 0


# --------------------------------------------------------------------------- #

def main(out_path):
    nodes_mm = (np.arange(N + 1) * D_MM,
                np.arange(N + 1) * D_MM,
                np.arange(NZ + 1) * D_MM)
    nodes_m = tuple(a / 1000.0 for a in nodes_mm)
    cell = (D_MM / 1000.0,) * 3
    ovr = int(os.environ.get("CONFORMAL_OVERSAMPLE", vox.CONFORMAL_OVERSAMPLE))

    t0 = time.time()
    on = vox.voxelize_materials([build()], cell, nodes_m=nodes_m,
                                subpixel=False, conformal=True,
                                conformal_oversample=ovr)
    elapsed = time.time() - t0
    got = on["arrays"]

    emit("reference coax  a=%.0f b=%.0f mm, cell %.3f mm, %dx%dx%d"
         % (A_MM, B_MM, D_MM, N, N, NZ))
    emit("oversample %d, voxelised in %.2f s" % (ovr, elapsed))
    emit("counts: %s" % (on["counts"],))
    emit()

    missing = [k for k in vox.CONFORMAL_KEYS if k not in got]
    if missing:
        emit("FAIL: fraction arrays not emitted: %s" % missing)
        return 1

    # -- V2 at the voxeliser -------------------------------------------------
    off = vox.voxelize_materials([build()], cell, nodes_m=nodes_m,
                                 subpixel=False)
    extra = [k for k in vox.CONFORMAL_KEYS if k in off["arrays"]]
    staircase_same = all(
        np.array_equal(off["arrays"][k], got[k])
        for k in ("eps_x", "eps_y", "eps_z", "mu_x", "mu_y", "mu_z", "pec_mask")
    )
    emit("V2  conformal off emits no fraction keys : %s" % (not extra))
    emit("V2  binary arrays identical either way   : %s" % staircase_same)
    ok = (not extra) and staircase_same

    # -- killed faces --------------------------------------------------------
    ref = analytic(nodes_mm)
    ex, ey, ez = (got["pec_edge_open_x"], got["pec_edge_open_y"],
                  got["pec_edge_open_z"])

    def live4(a, b, c, d):
        return (a > 0) | (b > 0) | (c > 0) | (d > 0)

    o = (slice(0, -1),) * 3
    contour = {
        # Hx: Ey[i,j,k], Ey[i,j,k+1], Ez[i,j,k], Ez[i,j+1,k]
        "pec_face_open_x": live4(ey[o], ey[:-1, :-1, 1:],
                                 ez[o], ez[:-1, 1:, :-1]),
        # Hy: Ez[i,j,k], Ez[i+1,j,k], Ex[i,j,k], Ex[i,j,k+1]
        "pec_face_open_y": live4(ez[o], ez[1:, :-1, :-1],
                                 ex[o], ex[:-1, :-1, 1:]),
        # Hz: Ex[i,j,k], Ex[i,j+1,k], Ey[i,j,k], Ey[i+1,j,k]
        "pec_face_open_z": live4(ex[o], ex[:-1, 1:, :-1],
                                 ey[o], ey[1:, :-1, :-1]),
    }
    emit()
    for key, live in contour.items():
        killed = (got[key][o] == 0.0) & live
        worst = float(ref[key][o][killed].max()) if killed.any() else 0.0
        emit("%-18s killed with a live contour edge: %5d "
             "(true open fraction up to %.4f)"
             % (key, int(np.count_nonzero(killed)), worst))
        # A tally is fine as long as those faces really have no open area; a
        # genuinely cut face killed this way is the S4 failure mode.
        ok = ok and worst < 1.0e-9

    # -- accuracy ------------------------------------------------------------
    emit()
    emit("%-20s %10s %10s %10s" % ("array", "max err", "rms err", "cut cells"))
    for key in vox.CONFORMAL_KEYS:
        d = np.abs(got[key] - ref[key])
        emit("%-20s %10.4f %10.5f %10d"
             % (key, d.max(), np.sqrt((d ** 2).mean()),
                int(np.count_nonzero((ref[key] > 0) & (ref[key] < 1)))))

    ref_all = np.concatenate([ref[k].ravel() for k in vox.CONFORMAL_KEYS])
    got_all = np.concatenate([got[k].ravel() for k in vox.CONFORMAL_KEYS])
    emit()
    emit("rms vs analytic : voxeliser %.5f   staircase %.5f"
         % (np.sqrt(((got_all - ref_all) ** 2).mean()),
            np.sqrt(((np.round(ref_all) - ref_all) ** 2).mean())))
    emit("min open face   : voxeliser %.5f   analytic %.5f"
         % (min(got[k][got[k] > 0].min() for k in vox.CONFORMAL_KEYS[3:]),
            min(ref[k][ref[k] > 0].min() for k in vox.CONFORMAL_KEYS[3:])))

    # -- remaining whole-element disagreements -------------------------------
    # Expected: the nodes sitting *exactly* on the shield wall (r = b). Whether
    # a surface-tangent Ez edge counts as covered is a measure-zero tie; the
    # voxeliser zeroes it, which is right on its own terms (that Ez is
    # tangential to the conductor, so it must vanish there anyway).
    emit()
    emit("whole-element disagreements (index, r/mm, got, ref):")
    for key in vox.CONFORMAL_KEYS:
        bad = np.argwhere(np.abs(got[key] - ref[key]) > 0.5)
        for (i, j, k) in bad[:8]:
            emit("  %-18s (%2d,%2d,%d) r=%7.4f got=%.3f ref=%.3f"
                 % (key, i, j, k,
                    np.hypot(nodes_mm[0][i] - CX, nodes_mm[1][j] - CY),
                    got[key][i, j, k], ref[key][i, j, k]))
        if len(bad) > 8:
            emit("  %-18s ... %d more" % (key, len(bad) - 8))

    # -- ends flush with the grid in z ---------------------------------------
    # A z-invariant body must give a z-invariant answer, whatever its end faces
    # land on. That is a zero-tolerance assertion needing no analytic reference,
    # and it is far sharper than an error threshold: the failure it catches
    # wiped an entire node plane while every other plane stayed correct.
    emit()
    flush = vox.voxelize_materials([build(flush=True)], cell, nodes_m=nodes_m,
                                   subpixel=False, conformal=True,
                                   conformal_oversample=ovr)["arrays"]
    for key in vox.CONFORMAL_KEYS:
        a = flush[key]
        spread = float(np.abs(a - a[:, :, 1:2]).max())
        emit("%-18s flush ends, z-invariant: %s (max plane-to-plane %.4f)"
             % (key, "yes" if spread == 0.0 else "NO", spread))
        ok = ok and spread == 0.0

    # -- a node plane tangent to a curved face --------------------------------
    ok = check_tangent_plane() and ok

    # -- conductor cells carry the medium beside them ------------------------
    ok = check_pec_material_fill(cell, nodes_m, ovr) and ok

    emit()
    emit("RESULT: %s" % ("PASS" if ok else "FAIL"))
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_report) + "\n")
    return 0 if ok else 1


sys.exit(main(os.environ.get(
    "CONFORMAL_REPORT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "conformal_fractions.txt"))))
