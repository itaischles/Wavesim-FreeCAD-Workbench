# -*- coding: utf-8 -*-
"""Gate: ``scanline.lattice_inside`` must agree with matplotlib bit for bit.

The lattice scanline replaces ``matplotlib.path.Path.contains_points`` inside the
voxeliser, so it has to reproduce matplotlib's answer *everywhere*, including on
the polygon boundary where the answer is a convention rather than a fact (see
``wavesim_gui/scanline.py``). This is the check that says it does.

Run under **FreeCAD's bundled Python** (any Python with numpy + matplotlib will
do -- neither FreeCAD nor ``Part`` is needed)::

    "%LOCALAPPDATA%\\Programs\\FreeCAD 1.1\\bin\\python.exe" tools/check_scanline.py

Four families, each a full lattice-vs-``contains_points`` comparison:

1. **Hand-built degeneracies** -- axis-aligned boxes, triangles and a bow-tie
   sampled on lattices deliberately built from the polygon's own vertex and edge
   coordinates, so points land exactly on corners, on horizontal edges (which no
   ray crosses), on vertical edges, and on local minima/maxima in y.
2. **Axis-aligned random boxes on a coincident lattice** -- the case the
   voxeliser's bit-identical promise actually protects: a PEC brick whose face
   falls exactly on a cell centre.
3. **Random polygons** (star-shaped, so they are simple) on random lattices.
4. **Discretised circles** with holes -- the coax cross-section, XOR-ed the way
   ``_layer_inside`` composes wires.

Exit status is non-zero if any point disagrees.
"""

import os
import sys

import numpy as np
from matplotlib.path import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wavesim_gui.scanline import lattice_inside                  # noqa: E402

_failures = 0
_points = 0


def check(name, poly, xs, ys):
    """Compare both implementations over the lattice; report any disagreement."""
    global _failures, _points
    poly = np.asarray(poly, dtype=np.float64)
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    ref = Path(poly).contains_points(pts).reshape(len(xs), len(ys))
    got = lattice_inside(poly, xs, ys)
    bad = int((ref != got).sum())
    _points += ref.size
    if bad:
        _failures += 1
        i, j = np.nonzero(ref != got)
        print("  FAIL %-34s %d/%d differ, first at (%.17g, %.17g) "
              "mpl=%s scan=%s" % (name, bad, ref.size, xs[i[0]], ys[j[0]],
                                  ref[i[0], j[0]], got[i[0], j[0]]))
    return bad == 0


# --------------------------------------------------------------------------- #
# 1. hand-built degeneracies
# --------------------------------------------------------------------------- #
print("degenerate lattices (points on corners / edges / y-extrema)")

square = [(0., 0.), (1., 0.), (1., 1.), (0., 1.)]
cases = {
    "square ccw": square,
    "square cw": square[::-1],
    "square closed": square + square[:1],
    "triangle": [(0., 0.), (2., 0.), (1., 1.)],
    "triangle flipped": [(0., 1.), (2., 1.), (1., 0.)],
    "bow-tie (double vertex)": [(0., 0.), (1., 1.), (2., 0.),
                                (2., 2.), (1., 1.), (0., 2.)],
    "notch (horizontal edges)": [(0., 0.), (3., 0.), (3., 2.), (2., 2.),
                                 (2., 1.), (1., 1.), (1., 2.), (0., 2.)],
    "collinear run": [(0., 0.), (1., 0.), (2., 0.), (2., 2.), (0., 2.)],
}
for name, poly in cases.items():
    p = np.asarray(poly, dtype=np.float64)
    # A lattice made of the polygon's own coordinates plus midpoints and points
    # just outside, so every corner and edge is sampled exactly.
    def axis(v):
        u = np.unique(np.concatenate([v, v + 0.5, v - 0.5, v + 1e-12, v - 1e-12]))
        return np.sort(u)
    check(name, p, axis(p[:, 0]), axis(p[:, 1]))

# --------------------------------------------------------------------------- #
# 2. axis-aligned boxes landing exactly on lattice points
# --------------------------------------------------------------------------- #
print("axis-aligned boxes on coincident lattices")
rng = np.random.default_rng(20260801)
for t in range(300):
    xs = np.sort(rng.uniform(-2, 2, 23))
    ys = np.sort(rng.uniform(-2, 2, 19))
    # Snap the box corners *onto* lattice points -- a PEC brick whose face falls
    # exactly on a cell centre.
    i0, i1 = sorted(rng.choice(len(xs), 2, replace=False))
    j0, j1 = sorted(rng.choice(len(ys), 2, replace=False))
    if i0 == i1 or j0 == j1:
        continue
    box = [(xs[i0], ys[j0]), (xs[i1], ys[j0]), (xs[i1], ys[j1]), (xs[i0], ys[j1])]
    if t % 2:
        box = box[::-1]
    check("box #%d" % t, box, xs, ys)

# --------------------------------------------------------------------------- #
# 3. random star-shaped polygons on random lattices
# --------------------------------------------------------------------------- #
print("random polygons")
for t in range(300):
    nv = int(rng.integers(3, 40))
    th = np.sort(rng.uniform(0, 2 * np.pi, nv))
    r = rng.uniform(0.2, 1.0, nv)
    poly = np.column_stack([r * np.cos(th), r * np.sin(th)])
    xs = np.sort(rng.uniform(-1.2, 1.2, int(rng.integers(5, 60))))
    ys = np.sort(rng.uniform(-1.2, 1.2, int(rng.integers(5, 60))))
    check("poly #%d (%d verts)" % (t, nv), poly, xs, ys)

# Same, but with the lattice drawn from the polygon's own vertex coordinates.
for t in range(300):
    nv = int(rng.integers(3, 24))
    th = np.sort(rng.uniform(0, 2 * np.pi, nv))
    r = rng.uniform(0.2, 1.0, nv)
    poly = np.column_stack([r * np.cos(th), r * np.sin(th)])
    xs = np.unique(poly[:, 0])
    ys = np.unique(poly[:, 1])
    check("poly-on-vertices #%d" % t, poly, xs, ys)

# --------------------------------------------------------------------------- #
# 4. discretised circles, XOR-composed like _layer_inside does
# --------------------------------------------------------------------------- #
print("coax cross-sections (XOR of wires)")
for nv in (12, 37, 114, 400):
    for radii in ((1.65, 0.5), (2.0, 1.65)):
        th = np.linspace(0, 2 * np.pi, nv, endpoint=False)
        xs = np.linspace(-2.2, 2.2, 91)
        ys = np.linspace(-2.2, 2.2, 87)
        gx, gy = np.meshgrid(xs, ys, indexing="ij")
        pts = np.column_stack([gx.ravel(), gy.ravel()])
        ref = np.zeros((len(xs), len(ys)), dtype=bool)
        got = np.zeros((len(xs), len(ys)), dtype=bool)
        for rad in radii:
            poly = np.column_stack([rad * np.cos(th), rad * np.sin(th)])
            ref ^= Path(poly).contains_points(pts).reshape(ref.shape)
            got ^= lattice_inside(poly, xs, ys)
        bad = int((ref != got).sum())
        _points += ref.size
        if bad:
            _failures += 1
            print("  FAIL annulus nv=%d r=%s: %d differ" % (nv, radii, bad))

print()
if _failures:
    print("FAILED: %d cases disagree with matplotlib" % _failures)
    sys.exit(1)
print("OK -- every case agrees with matplotlib (%,d points compared)"
      .replace(",d", "d") % _points)
