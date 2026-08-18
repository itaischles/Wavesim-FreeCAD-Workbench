# -*- coding: utf-8 -*-
"""Even-odd point-in-section over a *regular lattice* of sample points.

A section is either a chord **polygon** (:func:`lattice_inside`, the workhorse)
or, when the cut curves are primitives OCC can hand over exactly, a list of
**exact segments** (:func:`analytic_inside`). Both answer the same even-odd
question with the same crossing convention; only the arithmetic that locates a
crossing differs.

The voxeliser tests one cross-section against a large set of sample
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

import collections

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


# --------------------------------------------------------------------------- #
# Samples lying exactly *on* the polygon
#
# The crossing rule above is half-open in **both** directions. In the row
# direction its ``>=`` counts a vertex on the row as "above", so a sample sitting
# exactly on a polygon's *upper* horizontal edge reads inside and one on the
# *lower* edge reads outside. In the column direction a crossing toggles the
# samples strictly left of it, so a sample sitting exactly on a rectangle's
# *left* vertical edge reads inside (the right edge's crossing toggles it) and
# one on its *right* edge reads outside. Both are legitimate conventions for a
# fill rule -- matplotlib's, and the ones :func:`lattice_inside` must keep
# reproducing -- but neither is mirror-symmetric, and the voxeliser's whole
# geometry contract is.
#
# It is not an exotic case either. The grid snapper deliberately puts nodes on
# feature extents, so a round conductor's tangent planes land exactly on node
# lines, and the section of a cylinder through its own axis is a rectangle whose
# edges lie exactly along them -- no chord error to blur the tie. On the
# bent_coax port that read ``pec_edge_open_x`` = 1 at y = 12.5 mm against 0 at
# y = 17.5 mm on a pin centred at 15: two mirror-image edges of one conductor,
# opposite answers, on 29 of 56 x planes.
#
# :func:`on_axis_edge` and :func:`on_axis_edge_points` locate those samples so a
# caller can settle them symmetrically. The voxeliser resolves them to *open*,
# which is what everything downstream of the fractions already assumes: an edge
# lying in a grid-aligned conductor surface is fully open by these fractions' own
# measure, and shorting it is ``wavesim.parts.pec_node_mask``'s job. Callers that
# must stay bit-compatible with matplotlib simply do not ask.
# --------------------------------------------------------------------------- #


def _mark_spans(rows, c0, c1, n_row, nx):
    """``(n_row, nx)`` bool: column ranges ``[c0, c1)`` marked on ``rows``."""
    marks = np.zeros((n_row, nx + 1), dtype=np.intp)
    keep = c1 > c0
    if keep.any():
        np.add.at(marks, (rows[keep], c0[keep]), 1)
        np.add.at(marks, (rows[keep], c1[keep]), -1)
    return np.cumsum(marks, axis=1)[:, :nx] > 0


def _axis_edge_lattice(x0, y0, x1, y1, xs, ys):
    """``(len(xs), len(ys))`` mask of samples on an exactly axis-aligned segment.

    The segments are given as four parallel arrays, so this serves both a closed
    polygon (:func:`on_axis_edge`) and an exact wire's straight chords
    (:func:`analytic_inside`).

    Deliberately narrow: a segment qualifies only when its two endpoints share a
    ``y`` -- or a ``x`` -- **bit for bit**, and a sample only when its own
    coordinate equals that value exactly. Exactness is the whole point. Any rule
    with a tolerance around it inherits ``Wire.discretize``'s seam: on a chord
    polygon of a circle the vertices are placed asymmetrically, so "near an edge"
    is itself asymmetric, and a tolerant tie-break trades a clean 1.0 mirror
    error for a 0.125 one instead of removing it (measured). A curve that is
    carried exactly has no such seam and settles its own boundary samples --
    see :func:`analytic_inside`.
    """
    hit = np.zeros((xs.size, ys.size), dtype=bool)

    flat = np.nonzero((y0 == y1) & (x0 != x1))[0]
    if flat.size:
        ex0, ex1, ey = x0[flat], x1[flat], y0[flat]
        lo = np.searchsorted(xs, np.minimum(ex0, ex1), side="left")
        hi = np.searchsorted(xs, np.maximum(ex0, ex1), side="right")
        for n in range(flat.size):
            rows = np.nonzero(ys == ey[n])[0]
            if rows.size:
                hit[lo[n]:hi[n], rows] = True

    upright = np.nonzero((x0 == x1) & (y0 != y1))[0]
    for n in upright:
        cols = np.nonzero(xs == x0[n])[0]
        if not cols.size:
            continue
        lo, hi = min(y0[n], y1[n]), max(y0[n], y1[n])
        rows = np.nonzero((ys >= lo) & (ys <= hi))[0]
        if rows.size:
            hit[np.ix_(cols, rows)] = True
    return hit


def _axis_edge_points(x0, y0, x1, y1, pts):
    """:func:`_axis_edge_lattice` for an arbitrary ``(P, 2)`` point list."""
    px, py = pts[:, 0][:, None], pts[:, 1][:, None]
    hit = np.zeros(pts.shape[0], dtype=bool)

    flat = np.nonzero((y0 == y1) & (x0 != x1))[0]
    if flat.size:
        ex0, ex1, ey = x0[flat][None, :], x1[flat][None, :], y0[flat][None, :]
        hit |= ((py == ey)
                & (px >= np.minimum(ex0, ex1))
                & (px <= np.maximum(ex0, ex1))).any(axis=1)

    upright = np.nonzero((x0 == x1) & (y0 != y1))[0]
    if upright.size:
        ey0, ey1, ex = (y0[upright][None, :], y1[upright][None, :],
                        x0[upright][None, :])
        hit |= ((px == ex)
                & (py >= np.minimum(ey0, ey1))
                & (py <= np.maximum(ey0, ey1))).any(axis=1)
    return hit


def on_axis_edge(poly, xs, ys):
    """Samples lying on an exactly horizontal or vertical edge of *poly*.

    Returns a ``(len(xs), len(ys))`` bool mask. See :func:`_axis_edge_lattice`
    for why the test is exact rather than tolerant. ``xs`` must be ascending.
    """
    poly = np.asarray(poly, dtype=np.float64)
    xs = np.ascontiguousarray(xs, dtype=np.float64)
    ys = np.ascontiguousarray(ys, dtype=np.float64)
    if xs.size == 0 or ys.size == 0 or poly.shape[0] < 3:
        return np.zeros((xs.size, ys.size), dtype=bool)
    x0, y0 = poly[:, 0], poly[:, 1]
    return _axis_edge_lattice(x0, y0, np.roll(x0, -1), np.roll(y0, -1), xs, ys)


def on_axis_edge_points(poly, pts):
    """:func:`on_axis_edge` for an arbitrary ``(P, 2)`` point list."""
    poly = np.asarray(poly, dtype=np.float64)
    pts = np.asarray(pts, dtype=np.float64)
    if pts.shape[0] == 0 or poly.shape[0] < 3:
        return np.zeros(pts.shape[0], dtype=bool)
    x0, y0 = poly[:, 0], poly[:, 1]
    return _axis_edge_points(x0, y0, np.roll(x0, -1), np.roll(y0, -1), pts)


def _parity(rows, col, n_row, nx):
    """``(nx, n_row)`` inside mask from crossings whose prefix ends at *col*.

    A crossing toggles the samples strictly left of ``col``, so a sample is
    inside exactly when an odd number of its row's crossings end their prefix to
    its right -- a reverse cumulative sum, with no loop over samples.
    """
    acc = np.bincount(rows * (nx + 1) + col,
                      minlength=n_row * (nx + 1)).reshape(n_row, nx + 1)
    tail = acc.sum(axis=1, keepdims=True) - np.cumsum(acc, axis=1)
    return (tail[:, :nx] & 1).astype(bool).T


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
        inside[:, a:b] = _parity(rr, col, b - a, nx)
    return inside


# --------------------------------------------------------------------------- #
# Exact section wires
#
# ``Wire.discretize`` does two things to a curved section, and both are visible
# in the arrays: it *inscribes* (so every round conductor comes out undersized by
# the deflection -- the reference coax's r = 9 mm shield measured 8.909 mm at the
# old tolerance) and it walks from the wire's **seam** (so the polygon is not
# mirror-symmetric, which is what the three chord fractions in :mod:`voxelize`
# are tightened to hide). Where OCC hands over a primitive there is no need to
# suffer either: a circle is three numbers, and its own equation answers a sample
# exactly, with no seam and no chord.
#
# An :class:`AnalyticWire` is such a section: a closed loop of exact segments,
# straight chords and circular arcs, in any order (parity depends only on each
# segment's endpoints, not on how they are strung together). Every arc is
# **y-monotone** -- an arc spanning a y extremum of its circle is split there, at
# the exact point ``(cx, cy +- r)`` -- which is what lets the crossing test stay
# the polygon rule verbatim: a segment crosses a row iff ``(y0 >= y) != (y1 >= y)``
# and then crosses it exactly once. The half-open convention therefore survives
# intact across a junction between a chord and an arc.
#
# What is *not* inherited is the boundary asymmetry. A snapped grid puts sample
# lines exactly on a body's tangent planes, so an exact circle is asked about
# samples sitting exactly on it all day -- and the parity rule answers the two
# mirror partners of such a pair oppositely (the sample at ``cx - r`` reads
# inside, the one at ``cx + r`` outside). So :func:`analytic_inside` also returns
# the samples lying **on** the curve, to round-off, and the voxeliser subtracts
# them unconditionally: on the surface reads open, the same answer the exact
# axis-aligned-edge rule above settles a polygon's tangency to. Unconditionally,
# because unlike a chord polygon there is no older answer here to stay
# bit-compatible with.
# --------------------------------------------------------------------------- #

#: A closed section wire as exact segments.
#:
#: ``lines`` is an ``(n, 4)`` array of ``(x0, y0, x1, y1)`` chords; ``arcs`` is an
#: ``(m, 6)`` array of ``(y0, y1, cx, cy, r, side)`` y-monotone circular arcs,
#: where ``side`` is ``+1`` for the half of the circle at ``x >= cx`` and ``-1``
#: for the other. An arc needs no x endpoints: its crossing of a row follows from
#: the circle and the side.
AnalyticWire = collections.namedtuple("AnalyticWire", "lines arcs")

# Half-width of the "this sample is *on* the arc" window, relative to r^2, applied
# to the circle's own implicit function ``(x-cx)^2 + (y-cy)^2 - r^2``. Round-off
# scale and nothing more: at r = 9 mm it is 8e-12 mm^2, i.e. 5e-13 mm of radius,
# eleven orders below the finest sub-cell the voxeliser samples on. The implicit
# form rather than a distance to the crossing because the two differ exactly where
# it matters: on a row tangent to the arc the crossing collapses to a point and
# ``sqrt`` loses half its digits to cancellation, while the implicit residual
# stays linear in the error.
_ON_ARC_REL = 1.0e-13


def _line_crossings(lines, yc):
    """``(rows, xc, tol)`` for the rows in *yc* each chord of *lines* crosses."""
    x0, y0, x1, y1 = lines[:, 0], lines[:, 1], lines[:, 2], lines[:, 3]
    f0 = y0[None, :] >= yc[:, None]
    f1 = y1[None, :] >= yc[:, None]
    rr, ee = np.nonzero(f0 != f1)
    if rr.size == 0:
        return None
    ex0, ex1, ey0, ey1 = x0[ee], x1[ee], y0[ee], y1[ee]
    # Interpolated from vertex 1, as in lattice_inside, so a vertical chord gives
    # xc == x1 with no rounding at all.
    xc = ex1 + (yc[rr] - ey1) * (ex1 - ex0) / (ey1 - ey0)
    tol = _AMBIGUOUS_REL * (np.abs(ex1) + np.abs(xc) + np.abs(ex1 - ex0))
    return rr, xc, tol


def _arc_crossings(arcs, yc):
    """``(rows, xc, tol)`` for the rows in *yc* each arc of *arcs* crosses."""
    y0, y1 = arcs[:, 0], arcs[:, 1]
    f0 = y0[None, :] >= yc[:, None]
    f1 = y1[None, :] >= yc[:, None]
    rr, ee = np.nonzero(f0 != f1)
    if rr.size == 0:
        return None
    cx, cy, r, side = arcs[ee, 2], arcs[ee, 3], arcs[ee, 4], arcs[ee, 5]
    dy = yc[rr] - cy
    # A y-monotone arc lies in one closed half-plane about cx and so crosses the
    # row exactly once, at the root on its own side.
    xc = cx + side * np.sqrt(np.maximum(r * r - dy * dy, 0.0))
    return rr, xc, _AMBIGUOUS_REL * (np.abs(cx) + r)


def _on_arc_lattice(arcs, xs, ys):
    """``(len(xs), len(ys))`` mask of samples lying on an arc of *arcs*."""
    hit = np.zeros((xs.size, ys.size), dtype=bool)
    for y0, y1, cx, cy, r, side in arcs:
        dx = xs - cx
        dy = ys - cy
        slack = _ON_ARC_REL * r
        on = np.abs(dx[:, None] ** 2 + dy[None, :] ** 2 - r * r) <= _ON_ARC_REL * r * r
        on &= (dx * side >= -slack)[:, None]
        on &= ((ys >= min(y0, y1) - slack) & (ys <= max(y0, y1) + slack))[None, :]
        hit |= on
    return hit


def _on_arc_points(arcs, pts):
    """:func:`_on_arc_lattice` for an arbitrary ``(P, 2)`` point list."""
    px, py = pts[:, 0], pts[:, 1]
    hit = np.zeros(pts.shape[0], dtype=bool)
    for y0, y1, cx, cy, r, side in arcs:
        dx, dy = px - cx, py - cy
        slack = _ON_ARC_REL * r
        on = np.abs(dx * dx + dy * dy - r * r) <= _ON_ARC_REL * r * r
        on &= dx * side >= -slack
        on &= (py >= min(y0, y1) - slack) & (py <= max(y0, y1) + slack)
        hit |= on
    return hit


def analytic_inside(wire, xs, ys):
    """``(inside, on_curve)`` masks of *wire* over the lattice ``xs`` x ``ys``.

    *wire* is an :class:`AnalyticWire`. Both masks are ``(len(xs), len(ys))``,
    the same layout :func:`lattice_inside` returns; ``xs`` must be ascending.

    ``on_curve`` is the samples lying on the wire to round-off -- exactly on an
    axis-aligned chord, within the arithmetic's own window of any other crossing,
    or on an arc by its circle's implicit function. They are reported rather than
    resolved because the caller composes several wires: a sample on a hole's rim
    is on that conductor's surface just as much as one on the outer rim, and the
    two must not cancel in the XOR.
    """
    lines = np.asarray(wire.lines, dtype=np.float64).reshape(-1, 4)
    arcs = np.asarray(wire.arcs, dtype=np.float64).reshape(-1, 6)
    xs = np.ascontiguousarray(xs, dtype=np.float64)
    ys = np.ascontiguousarray(ys, dtype=np.float64)
    nx, ny = xs.size, ys.size
    inside = np.zeros((nx, ny), dtype=bool)
    border = np.zeros((nx, ny), dtype=bool)
    n_seg = lines.shape[0] + arcs.shape[0]
    if nx == 0 or ny == 0 or n_seg < 2:
        return inside, border

    if lines.shape[0]:
        border |= _axis_edge_lattice(lines[:, 0], lines[:, 1],
                                     lines[:, 2], lines[:, 3], xs, ys)
    if arcs.shape[0]:
        border |= _on_arc_lattice(arcs, xs, ys)

    chunk = max(1, min(ny, _CHUNK_ELEMS // n_seg))
    for a in range(0, ny, chunk):
        b = min(ny, a + chunk)
        yc = ys[a:b]
        found = []
        if lines.shape[0]:
            found.append(_line_crossings(lines, yc))
        if arcs.shape[0]:
            found.append(_arc_crossings(arcs, yc))
        found = [c for c in found if c is not None]
        if not found:
            continue
        rr = np.concatenate([c[0] for c in found])
        xc = np.concatenate([c[1] for c in found])
        tol = np.concatenate([np.broadcast_to(c[2], c[1].shape) for c in found])
        # Samples strictly left of the window toggle; the window itself is the
        # curve, and is reported above rather than decided here.
        col = np.searchsorted(xs, xc - tol, side="left")
        hi = np.searchsorted(xs, xc + tol, side="right")
        border[:, a:b] |= _mark_spans(rr, col, hi, b - a, nx).T
        inside[:, a:b] = _parity(rr, col, b - a, nx)
    return inside, border


def analytic_inside_points(wire, pts):
    """:func:`analytic_inside` for an arbitrary ``(P, 2)`` point list."""
    lines = np.asarray(wire.lines, dtype=np.float64).reshape(-1, 4)
    arcs = np.asarray(wire.arcs, dtype=np.float64).reshape(-1, 6)
    pts = np.asarray(pts, dtype=np.float64)
    n = pts.shape[0]
    inside = np.zeros(n, dtype=bool)
    border = np.zeros(n, dtype=bool)
    if n == 0 or lines.shape[0] + arcs.shape[0] < 2:
        return inside, border

    px, py = pts[:, 0][:, None], pts[:, 1][:, None]
    toggles = np.zeros(n, dtype=np.intp)
    if lines.shape[0]:
        border |= _axis_edge_points(lines[:, 0], lines[:, 1],
                                    lines[:, 2], lines[:, 3], pts)
        x0, y0, x1, y1 = (lines[:, 0][None, :], lines[:, 1][None, :],
                          lines[:, 2][None, :], lines[:, 3][None, :])
        cross = (y0 >= py) != (y1 >= py)
        den = np.where(y1 == y0, 1.0, y1 - y0)
        xc = x1 + (py - y1) * (x1 - x0) / den
        tol = _AMBIGUOUS_REL * (np.abs(x1) + np.abs(xc) + np.abs(x1 - x0))
        toggles += (cross & (px < xc - tol)).sum(axis=1)
        border |= (cross & (np.abs(px - xc) <= tol)).any(axis=1)
    if arcs.shape[0]:
        border |= _on_arc_points(arcs, pts)
        y0, y1 = arcs[:, 0][None, :], arcs[:, 1][None, :]
        cx, cy = arcs[:, 2][None, :], arcs[:, 3][None, :]
        r, side = arcs[:, 4][None, :], arcs[:, 5][None, :]
        cross = (y0 >= py) != (y1 >= py)
        dy = py - cy
        xc = cx + side * np.sqrt(np.maximum(r * r - dy * dy, 0.0))
        tol = _AMBIGUOUS_REL * (np.abs(cx) + r)
        toggles += (cross & (px < xc - tol)).sum(axis=1)
        border |= (cross & (np.abs(px - xc) <= tol)).any(axis=1)
    return (toggles & 1).astype(bool), border
