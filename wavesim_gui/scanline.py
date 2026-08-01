# -*- coding: utf-8 -*-
"""Even-odd point-in-polygon over a *regular lattice* of sample points.

The voxeliser tests one cross-section polygon against a large set of sample
points that is always an axis-aligned lattice (cell centres, subpixel fine
centres, or the conformal node lattice -- see :mod:`wavesim_gui.voxelize`).
``matplotlib.path.Path.contains_points`` does not know that: it walks every
polygon edge for every point, costing ``O(points x vertices)``. Profiling a
0.9M-cell coax put 40% (subpixel) to 60% (conformal) of the whole voxelisation
in that one call.

Exploiting the lattice removes the product. Each polygon edge crosses a given
sample *row* at most once, and -- because ``xs`` is common to every row -- the
crossing's column position is one ``searchsorted`` into ``xs``. A crossing then
toggles a whole *prefix* of its row, so the row's parity is a reverse cumulative
sum of prefix ends. Cost is ``O(rows x edges + rows x cols)`` with no inner loop
over points.

Matching matplotlib exactly
---------------------------
The voxeliser promises that subpixel/conformal *off* reproduces the older
behaviour bit for bit, so this must not merely be "a correct" point-in-polygon --
it has to agree with matplotlib everywhere, including where the answer is a
convention rather than a fact. matplotlib uses the Haines "crossings" test: for
an edge ``(x0,y0) -> (x1,y1)`` and query ``(x, y)``,

.. code-block:: text

    crossing := (y0 >= y) != (y1 >= y)
    toggle   := ((y1 - y)*(x0 - x1) >= (x1 - x)*(y0 - y1)) == (y1 >= y)

(verified against ``Path.contains_points`` over a degeneracy-rich corpus).
:func:`_exact_toggle` is that expression, transcribed. The right-hand side is
affine in ``x`` with a sign fixed by the edge's direction, so ``toggle`` is
always **true on a prefix** of the sorted row and false after it -- which is what
makes the reverse-cumsum parity legitimate, degeneracies and all. Only the
*position* of that prefix boundary is in question.

The fast path finds the boundary by dividing through: the edge meets the row at
``xc = x1 + (y - y1)*(x1 - x0)/(y1 - y0)``, and the prefix ends there. That
division is the one departure from matplotlib's arithmetic, and it can land on
the wrong side of a sample sitting within a few ULPs of the edge -- rare, but
this is a geometry kernel, and "rare" is what a CAD model that happens to be
axis-aligned on the grid does all day. So each crossing also gets a tolerance
window around ``xc``: samples outside it are settled by ``xc`` alone, and the
handful inside are decided by evaluating matplotlib's expression itself. The
window is normally empty, so the fix-up costs nothing on curved geometry, and
the result is matplotlib's answer by construction rather than by hope.
See ``tools/check_scanline.py`` for the gate.

Pure bundled-numpy, no FreeCAD and no matplotlib: FreeCAD-side, but importable
anywhere.
"""

import numpy as np

# Row-chunking budget for the (rows x edges) crossing test, in array elements.
# Every voxeliser call site is far below this (a body's bbox in cells, or its
# fine sub-block -- hundreds of rows against ~100 edges); the chunking exists so
# a pathologically large lattice degrades in time rather than in memory.
_CHUNK_ELEMS = 4_000_000

# Half-width of the "decide this one exactly" window around xc, relative to the
# magnitudes that went into the division. A wider window only moves more samples
# onto the exact predicate (correct either way, just slower); too narrow would
# let a rounding error through, so this is deliberately generous -- at 1e-13 the
# window still catches no samples at all on curved geometry.
_AMBIGUOUS_REL = 1.0e-13


def _exact_toggle(x, y, x0, y0, x1, y1):
    """matplotlib's own crossing predicate, verbatim (assumes a crossing)."""
    return (((y1 - y) * (x0 - x1) >= (x1 - x) * (y0 - y1)) == (y1 >= y))


def lattice_inside(poly, xs, ys):
    """Even-odd inside mask of *poly* over the lattice ``xs`` x ``ys``.

    Parameters
    ----------
    poly : (V, 2) array
        Polygon vertices. Treated as closed (the edge ``V-1 -> 0`` is implied),
        so a repeated final vertex is harmless -- it degenerates to a horizontal
        zero-length edge, which never crosses a row.
    xs, ys : 1-D arrays
        Sample coordinates along each axis. ``xs`` **must be sorted ascending**
        (every voxeliser call site passes cell centres / node coordinates, which
        are); ``ys`` needs no ordering.

    Returns
    -------
    (len(xs), len(ys)) bool array
        ``out[i, j]`` is whether ``(xs[i], ys[j])`` is inside. That is the same
        layout as ``meshgrid(xs, ys, indexing="ij")`` raveled in C order, which
        is how the callers build their point lists.
    """
    poly = np.asarray(poly, dtype=np.float64)
    xs = np.ascontiguousarray(xs, dtype=np.float64)
    ys = np.ascontiguousarray(ys, dtype=np.float64)
    nx, ny = xs.size, ys.size
    inside = np.zeros((nx, ny), dtype=bool)
    if nx == 0 or ny == 0 or poly.shape[0] < 3:
        return inside

    x0, y0 = poly[:, 0], poly[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    n_edge = x0.size

    chunk = max(1, min(ny, _CHUNK_ELEMS // max(1, n_edge)))
    for a in range(0, ny, chunk):
        b = min(ny, a + chunk)
        yc = ys[a:b]
        # (rows, edges): matplotlib's ">=" row comparison, so a vertex sitting
        # exactly on the row counts as "above" for both of its edges.
        f0 = y0[None, :] >= yc[:, None]
        f1 = y1[None, :] >= yc[:, None]
        rr, ee = np.nonzero(f0 != f1)
        if rr.size == 0:
            continue
        ey0, ey1 = y0[ee], y1[ee]
        ex0, ex1 = x0[ee], x1[ee]
        yq = yc[rr]
        # Where the edge meets the row. Interpolated from vertex 1 so that a
        # *vertical* edge gives xc == x1 with no rounding at all.
        xc = ex1 + (yq - ey1) * (ex1 - ex0) / (ey1 - ey0)

        # Samples strictly left of the window toggle; strictly right do not.
        tol = _AMBIGUOUS_REL * (np.abs(ex1) + np.abs(xc) + np.abs(ex1 - ex0))
        col = np.searchsorted(xs, xc - tol, side="left")
        hi = np.searchsorted(xs, xc + tol, side="right")

        # ...and whatever sits inside the window is decided by matplotlib's
        # expression itself. Normally there is nothing there.
        width = hi - col
        if width.any():
            sel = np.nonzero(width)[0]
            n = width[sel]
            rep = np.repeat(sel, n)
            starts = np.repeat(np.cumsum(n) - n, n)
            cols = np.repeat(col[sel], n) + (np.arange(rep.size) - starts)
            tog = _exact_toggle(xs[cols], yq[rep], ex0[rep], ey0[rep],
                                ex1[rep], ey1[rep])
            col = col + np.bincount(rep, weights=tog,
                                    minlength=rr.size).astype(np.intp)

        # Parity of the crossings whose prefix ends strictly right of a column.
        acc = np.bincount(rr * (nx + 1) + col,
                          minlength=(b - a) * (nx + 1)).reshape(b - a, nx + 1)
        tail = acc.sum(axis=1, keepdims=True) - np.cumsum(acc, axis=1)
        inside[:, a:b] = (tail[:, :nx] & 1).astype(bool).T
    return inside
