# -*- coding: utf-8 -*-
"""Subpixel smoothing of the permittivity at dielectric interfaces (FreeCAD side).

Motivation
----------
A staircased (piecewise-constant, per-cell) permittivity makes the FDTD spatial
error only *first order* whenever a material boundary does not fall on a cell
edge, and makes derived quantities (resonances, S-params) jump discontinuously as
the geometry is nudged by less than a cell -- fatal for shape studies. Replacing
the boundary cells with an *anisotropic effective permittivity* restores (close
to) second-order accuracy and makes results vary smoothly with geometry. This is
the Kottke/Meep technique: https://meep.readthedocs.io/en/latest/Subpixel_Smoothing/

    eps_eff^-1 = <eps^-1> (n n^T) + <eps>^-1 (I - n n^T)

with ``<.>`` the volume average over the cell and ``n`` the unit interface normal.
The component *perpendicular* to the interface sees the harmonic mean; the
components *parallel* see the arithmetic mean. Both limits are exact.

This solver stores only a **diagonal** (per-axis) permittivity -- ``eps_x`` seen
by ``Ex`` etc. -- so we carry the diagonal of the inverse tensor:

    1/eps_d = <eps^-1> n_d^2 + <eps>^-1 (1 - n_d^2)          (d = x, y, z)

The two physically exact limits are reproduced regardless; only obliquely-cut
cells (a genuinely non-diagonal tensor no diagonal solver can represent) are
approximated. The interface normal is estimated from the gradient of the finely
sampled permittivity, averaged over the cell.

This is a **verbatim numeric port** of ``wavesim.subpixel.reduce_fine_eps`` from
the solver repo. It lives here because the FreeCAD-side voxeliser (which owns the
CAD geometry and therefore the sub-cell sampling) is the only place that can build
the fine permittivity field; the solver Python cannot import FreeCAD/``Part``.
Keep the two reducers in step -- they share the physics, not the code (the same
"shared contract, not shared code" split as :mod:`wavesim_gui.excitation`).

PEC / metals are **not** smoothed here: a perfect conductor is a hard field
constraint (tangential E == 0), not a material average, so the voxeliser keeps
its binary ``pec_mask`` (see :mod:`wavesim_gui.voxelize`).
"""

import numpy as np


def as_triplet(oversample):
    """Normalise ``oversample`` to an ``(ox, oy, oz)`` int tuple (>=1)."""
    if np.isscalar(oversample):
        o = int(oversample)
        trip = (o, o, o)
    else:
        trip = tuple(int(v) for v in oversample)
        if len(trip) != 3:
            raise ValueError("oversample must be an int or a length-3 sequence")
    if any(o < 1 for o in trip):
        raise ValueError("oversample factors must be >= 1")
    return trip


def block_mean(a, os):
    """Mean of ``a`` over non-overlapping ``os = (ox, oy, oz)`` sub-blocks.

    ``a`` has shape ``(Nx*ox, Ny*oy, Nz*oz)``; the result is ``(Nx, Ny, Nz)``.
    """
    ox, oy, oz = os
    nx, ny, nz = a.shape[0] // ox, a.shape[1] // oy, a.shape[2] // oz
    return a.reshape(nx, ox, ny, oy, nz, oz).mean(axis=(1, 3, 5))


def _fine_gradient(a):
    """``grad(a)`` on the fine grid, one component per axis, robust to singletons.

    Only direction matters (the normal is normalised later), so the index-space
    gradient is used; axes of length 1 (a 2D-in-3D slice) contribute a zero
    component.
    """
    grads = []
    for ax in range(3):
        if a.shape[ax] > 1:
            grads.append(np.gradient(a, axis=ax))
        else:
            grads.append(np.zeros_like(a))
    return grads


def reduce_fine_eps(eps_fine, oversample):
    """Reduce a finely-sampled scalar permittivity to a smoothed diagonal tensor.

    Parameters
    ----------
    eps_fine : ndarray, shape ``(Nx*ox, Ny*oy, Nz*oz)``
        Relative permittivity sampled on a uniform sub-grid, ``ox``/``oy``/``oz``
        samples per coarse cell along each axis. Must be strictly positive.
    oversample : int or (int, int, int)
        Sub-samples per cell per axis.

    Returns
    -------
    (eps_x, eps_y, eps_z) : tuple of ndarray, each shape ``(Nx, Ny, Nz)``
        The smoothed per-axis relative permittivity. In a homogeneous cell all
        three equal the (common) cell value; only interface cells differ.
    """
    os = as_triplet(oversample)
    eps_fine = np.asarray(eps_fine, dtype=np.float64)
    if np.any(eps_fine <= 0):
        raise ValueError("eps_fine must be strictly positive")

    mean_eps = block_mean(eps_fine, os)             # <eps>   (arithmetic)
    mean_inv = block_mean(1.0 / eps_fine, os)       # <eps^-1> (-> harmonic mean)

    # Interface normal from the sub-grid gradient of eps, averaged over the cell.
    gx, gy, gz = _fine_gradient(eps_fine)
    nx = block_mean(gx, os)
    ny = block_mean(gy, os)
    nz = block_mean(gz, os)
    norm = np.sqrt(nx * nx + ny * ny + nz * nz)
    scale = np.where(norm > 0.0, 1.0 / np.where(norm > 0.0, norm, 1.0), 0.0)
    nx *= scale
    ny *= scale
    nz *= scale

    inv_mean_eps = 1.0 / mean_eps                   # <eps>^-1 (tangential inverse)

    def _component(n2):
        inv_d = mean_inv * n2 + inv_mean_eps * (1.0 - n2)
        return 1.0 / inv_d

    eps_x = _component(nx * nx)
    eps_y = _component(ny * ny)
    eps_z = _component(nz * nz)
    return eps_x, eps_y, eps_z


def fine_axis(nodes, os, i0, i1):
    """Fine sample coordinates (cell-sub-centres) for cells ``[i0, i1)``.

    ``os`` sample points per cell are placed at the centres of ``os`` equal
    sub-intervals of each cell, so cell ``i`` (spanning ``nodes[i]..nodes[i+1]``)
    contributes ``nodes[i] + (m+0.5)/os * width`` for ``m = 0..os-1``. Works for
    non-uniform (graded) cell widths.
    """
    nodes = np.asarray(nodes, dtype=np.float64)
    frac = (np.arange(os) + 0.5) / os                       # (os,)
    left = nodes[i0:i1]                                      # (n,)
    width = nodes[i0 + 1:i1 + 1] - left                     # (n,)
    return (left[:, None] + frac[None, :] * width[:, None]).ravel()
