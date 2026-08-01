# -*- coding: utf-8 -*-
"""Geometry voxelisation and job-from-document building (FreeCAD side).

Session 3 replaces the hardcoded Session-2 material box with real CAD geometry.
:func:`voxelize_materials` samples each Material's bodies onto a regular grid
(one planar ``Shape.slice`` per Z-layer, then a vectorised point-in-polygon test
of that cross-section over the layer's cell centres) to fill the per-cell
``eps``/``mu`` arrays and ``pec_mask`` the solver consumes via
``set_material_arrays``. With ``conformal=True`` a PEC body additionally
contributes the six Dey-Mittra open-fraction arrays (:data:`CONFORMAL_KEYS`), so
the solver can integrate the cut geometry instead of a staircase.
:func:`build_job_from_document` derives a grid that bounds all material bodies,
voxelises into it, and returns a job spec plus the arrays to write as
``materials.npz``. Each future array-input concern (e.g. deferred array sources)
gets its own descriptively-named ``.npz`` rather than growing this one.

Empty voxels are filled with the Domain's chosen *background* Material (its
eps/mu/PEC), defaulting to vacuum; bodies overwrite the cells they cover.

This module is FreeCAD-side: it uses ``Part``/``Shape`` (FreeCAD's bundled
``numpy`` for the arrays) and is **not** importable by the solver Python.

Coordinate convention
---------------------
The solver grid has its origin at cell (0, 0, 0) == physical (0, 0, 0) and maps
a position to a cell by ``round(coord / d)`` (see ``grid.position_to_index``).
The voxel arrays already bake in the domain origin (cell ``i`` samples world
point ``origin + (i + 0.5)·d``), so the runner never needs the origin. Only
point-like inputs (the source) are emitted in the solver frame — i.e. measured
from the domain origin, not FreeCAD world coordinates.

Units: FreeCAD geometry is in millimetres; the job/solver work in metres. All
``*_m`` quantities are metres; voxelisation runs in mm and converts at the end.

The voxeliser works layer by layer: one OCC planar section per Z cell-plane,
then matplotlib's vectorised point-in-polygon over that layer's cell centres
(matplotlib + numpy are both in FreeCAD's bundled Python). This replaces the
original ``isInside``-per-cell sweep, which was O(N^3) BREP point queries.

The geometry is left to stop where the CAD stops -- nothing is extruded to meet a
port. A modal port's face carries no PML pad and no background gap (see
``domain.modal_port_faces``), so the domain face lands exactly on the material
bound and the port plane cuts the real cross-section. The port then terminates
the line at Z0 there; a conductor carried *past* it would put a second Z0 in
parallel with the port and reflect about a third of the wave back.
"""

import math

import FreeCAD

# Forward-slash JSON paths and mm->m conversion are the only unit handling here.
_MM_PER_M = 1000.0


class GridRequiredError(Exception):
    """Raised when materials are assigned but no Grid object exists.

    There is deliberately no default cell size: the run is refused so the user
    must create a Grid (Wavesim -> Create Grid) and choose the cell sizes
    explicitly before any voxelisation happens.
    """


class VoxelizationCancelled(Exception):
    """Raised when a voxelisation ``progress`` callback requests cancellation.

    The section sweep runs on the GUI thread and can still be slow on fine grids
    with many bodies; a caller showing a progress dialog returns truthy from the
    callback to abort, which surfaces here so the caller can clean up.
    """


def _gather(materials):
    """Return ``[(shape_mm, eps, mu, pec), ...]`` for every assigned body.

    One entry per body (a material with several bodies contributes several
    entries sharing its parameters). Bodies without a solid shape are skipped.
    """
    entries = []
    for mat in materials:
        eps = float(getattr(mat, "Eps", 1.0))
        mu = float(getattr(mat, "Mu", 1.0))
        pec = bool(getattr(mat, "Pec", False))
        for body in getattr(mat, "Bodies", []) or []:
            shape = getattr(body, "Shape", None)
            if shape is None or not getattr(shape, "Solids", None):
                continue
            entries.append((shape, eps, mu, pec))
    return entries


def _combined_bbox(entries):
    """Union BoundBox (mm) of all entry shapes, or ``None`` if there are none."""
    bbox = None
    for shape, _eps, _mu, _pec in entries:
        bb = shape.BoundBox
        if bbox is None:
            bbox = FreeCAD.BoundBox(bb)
        else:
            bbox.add(bb)
    return bbox


def materials_bbox_mm(materials):
    """Union BoundBox (mm) of all solid bodies on *materials*, or ``None``."""
    return _combined_bbox(_gather(materials))


_AXIS_IDX = {"x": 0, "y": 1, "z": 2}

# The two transverse axes of a normal in the solver's mode-slice order (matching
# ``mode_solver._NORMAL_CFG`` and ``modal_port._TRANSVERSE``).
_TRANSVERSE_AXES = {"x": ("y", "z"), "y": ("x", "z"), "z": ("x", "y")}


def _expand_bbox_points(bbox, points_mm):
    """Grow *bbox* (mm, possibly ``None``) to include each ``(x, y, z)`` point."""
    for p in points_mm:
        v = FreeCAD.Vector(p[0], p[1], p[2])
        if bbox is None:
            bbox = FreeCAD.BoundBox(v.x, v.y, v.z, v.x, v.y, v.z)
        else:
            bbox.add(v)
    return bbox


def _expand_bbox_axis(bbox, axis_offsets_mm):
    """Grow *bbox* along single axes to include each ``(axis, value_mm)``.

    Unlike a point, a snapshot slice only constrains its normal axis (its
    in-plane extent already follows the domain), so each offset extends *bbox*
    on one axis only. A no-op when *bbox* is ``None`` (nothing else to bound it).
    """
    if bbox is None:
        return bbox
    for axis, value in axis_offsets_mm:
        c = bbox.Center
        p = [c.x, c.y, c.z]
        p[_AXIS_IDX[axis]] = value
        bbox.add(FreeCAD.Vector(*p))
    return bbox


def source_points_mm(sim):
    """World-mm positions of every point source under *sim* (empty if none)."""
    if sim is None:
        return []
    from wavesim_gui import source as source_mod

    pts = []
    for src in source_mod.find_sources(sim):
        pos = src.Position
        pts.append((pos.x, pos.y, pos.z))
    return pts


def snapshot_axis_offsets(sim):
    """``[(axis, offset_mm), ...]`` of every snapshot slice under *sim*."""
    if sim is None:
        return []
    from wavesim_gui import monitors as monitors_mod

    return monitors_mod.snapshot_axis_offsets(sim)


def path_monitor_points_mm(sim):
    """Bbox corners (mm) of every voltage/current monitor curve under *sim*."""
    if sim is None:
        return []
    from wavesim_gui import monitors as monitors_mod

    return monitors_mod.path_monitor_points_mm(sim)


def spice_line_port_points_mm(sim):
    """World-mm endpoints of every SPICE line port's curve under *sim*."""
    if sim is None:
        return []
    from wavesim_gui import spice_port as spice_mod

    pts = []
    for port in spice_mod.find_spice_line_ports(sim):
        ends = spice_mod._line_endpoints_mm(port)
        if ends is not None:
            pts.append((ends[0].x, ends[0].y, ends[0].z))
            pts.append((ends[1].x, ends[1].y, ends[1].z))
    return pts


def combined_bbox_mm(sim, materials):
    """Material union-bbox (mm) grown to include sources and monitor geometry.

    The domain auto-sizes to this combined box, so a source, snapshot slice,
    voltage/current monitor curve or SPICE line port placed outside the material
    bounds (or in the PML) enlarges the domain to contain it. Returns ``None``
    when there is nothing to bound.
    """
    bbox = materials_bbox_mm(materials)
    bbox = _expand_bbox_points(bbox, source_points_mm(sim))
    bbox = _expand_bbox_points(bbox, path_monitor_points_mm(sim))
    bbox = _expand_bbox_points(bbox, spice_line_port_points_mm(sim))
    bbox = _expand_bbox_axis(bbox, snapshot_axis_offsets(sim))
    return bbox


def _grid_extent(bbox, cell_mm, spacing_lo_mm, spacing_hi_mm, pad_lo, pad_hi):
    """Per-axis ``(counts, origin_mm)`` for the given sizing.

    The inner region is the material bounds grown by ``spacing_lo_mm`` /
    ``spacing_hi_mm`` on the low/high side of each axis and rounded up to whole
    cells; ``pad_lo``/``pad_hi`` add the per-side PML cells outside that. The
    origin is the min corner of the *padded* grid.
    """
    exts = (bbox.XLength, bbox.YLength, bbox.ZLength)
    mins = (bbox.XMin, bbox.YMin, bbox.ZMin)
    counts = []
    origin = []
    for a in range(3):
        grown = exts[a] + float(spacing_lo_mm[a]) + float(spacing_hi_mm[a])
        inner = max(1, int(math.ceil(grown / cell_mm[a])))
        counts.append(inner + int(pad_lo[a]) + int(pad_hi[a]))
        origin.append(
            mins[a] - float(spacing_lo_mm[a]) - int(pad_lo[a]) * cell_mm[a]
        )
    return tuple(counts), tuple(origin)


def _sizing_for(sim, default_padding):
    """Resolve ``(spacing_lo, spacing_hi, pad_lo, pad_hi, domain)`` for *sim*.

    Uses the Domain object's per-face spacing and PML padding when one exists
    (with beam / SPICE-TEM faces forced to PML and modal-port faces stripped of
    their pad and gap, matching the drawn box and the run so the derived cell
    counts are the ones the job will use); otherwise falls back to the legacy
    uniform ``default_padding`` cells on every side with no background spacing
    (so a document without a domain runs as before).
    """
    from wavesim_gui import domain as domain_mod

    dom = domain_mod.find_domain(sim) if sim else None
    if dom is not None:
        p = domain_mod.domain_grid_params(
            dom,
            force_pml_faces=domain_mod.pml_port_faces(sim),
            modal_faces=domain_mod.modal_port_faces(sim),
        )
        return p["spacing_lo"], p["spacing_hi"], p["pad_lo"], p["pad_hi"], dom
    pad = (default_padding, default_padding, default_padding)
    return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), pad, pad, None


def derive_grid_dims(sim, cell_size_m, padding_cells=8):
    """Cheap (bbox-only) grid dims for the given cell sizes; no voxelisation.

    Returns ``{Nx, Ny, Nz, dx, dy, dz}`` (spacings in metres) or ``None`` if the
    simulation has no material-assigned geometry yet. Used by the Grid object to
    show derived cell counts without paying for a full ``isInside`` sweep.
    """
    from wavesim_gui import materials as materials_mod

    if sim is None:
        return None
    bbox = combined_bbox_mm(sim, materials_mod.find_materials(sim))
    if bbox is None:
        return None
    sp_lo, sp_hi, pad_lo, pad_hi, _dom = _sizing_for(sim, padding_cells)
    cell_mm = tuple(c * _MM_PER_M for c in cell_size_m)
    (Nx, Ny, Nz), _origin = _grid_extent(
        bbox, cell_mm,
        tuple(s * _MM_PER_M for s in sp_lo), tuple(s * _MM_PER_M for s in sp_hi),
        pad_lo, pad_hi,
    )
    return {
        "Nx": Nx, "Ny": Ny, "Nz": Nz,
        "dx": cell_size_m[0], "dy": cell_size_m[1], "dz": cell_size_m[2],
    }


def _section_polygons(body_shape, z_axis, z, deflection):
    """Cross-section of *body_shape* at height *z* as a list of XY polygons.

    Cuts the body with the horizontal plane at *z* -- one OCC section per layer
    instead of one ``isInside`` per cell -- and turns each resulting wire into a
    polygon (curved edges discretised to chord tolerance *deflection*). Returns
    ``None`` when the plane misses the solid (no section wires, or none that
    close), so the caller can leave that whole layer empty.
    """
    import numpy as np

    try:
        wires = body_shape.slice(z_axis, z)
    except Exception:
        return None
    if not wires:
        return None
    polys = []
    for w in wires:
        try:
            verts = w.discretize(Deflection=deflection)
        except Exception:
            continue
        if len(verts) < 3:
            continue
        polys.append(np.array([(v.x, v.y) for v in verts]))
    return polys or None


def _layer_inside(body_shape, z_axis, z, pts, deflection):
    """Boolean mask of which *pts* (XY, mm) lie inside *body_shape* at height *z*.

    Tests every point at once with matplotlib. XOR-ing the wires applies the
    even-odd rule, which carves holes and handles solids nested inside holes.
    Returns ``None`` when the plane misses the solid.

    For the (usual) case of points forming an axis-aligned lattice, prefer
    :func:`_layer_inside_lattice` -- same answer, without the
    ``O(points x vertices)``.
    """
    import numpy as np
    from matplotlib.path import Path

    polys = _section_polygons(body_shape, z_axis, z, deflection)
    if polys is None:
        return None
    inside = np.zeros(len(pts), dtype=bool)
    for poly in polys:
        inside ^= Path(poly).contains_points(pts)
    return inside


def _layer_inside_lattice(body_shape, z_axis, z, xs, ys, deflection):
    """:func:`_layer_inside` for a lattice of sample points ``xs`` x ``ys``.

    Returns a ``(len(xs), len(ys))`` mask (or ``None``) rather than a flat one --
    the same values a flat call would give for
    ``meshgrid(xs, ys, indexing="ij")``, since
    :func:`wavesim_gui.scanline.lattice_inside` reproduces matplotlib's crossing
    rule exactly (``tools/check_scanline.py``). ``xs`` must be ascending.
    """
    import numpy as np

    from wavesim_gui.scanline import lattice_inside

    polys = _section_polygons(body_shape, z_axis, z, deflection)
    if polys is None:
        return None
    inside = np.zeros((len(xs), len(ys)), dtype=bool)
    for poly in polys:
        inside ^= lattice_inside(poly, xs, ys)
    return inside


# --------------------------------------------------------------------------- #
# Subpixel smoothing of dielectric interfaces (see wavesim_gui.subpixel)
#
# The plain sweep above snaps a material boundary to whole cells (staircasing),
# which drops the FDTD to first-order accuracy off-grid and makes derived
# quantities jump as geometry is nudged by sub-cell amounts. When enabled, a
# dielectric body is instead *fine-sampled* over its bounding-box sub-block and
# reduced to an anisotropic effective permittivity (the diagonal Kottke tensor),
# anti-staircasing the boundary cells. PEC stays binary -- a perfect conductor is
# a hard field constraint, not a material average.
# --------------------------------------------------------------------------- #

def _cell_span(nodes_mm, lo, hi, margin=1):
    """Half-open coarse cell range ``[a, b)`` whose cells overlap ``[lo, hi]``.

    ``nodes_mm`` are the ``N+1`` cell edges (world mm). Grown by *margin* cells on
    each side (so boundary cells keep valid fine-gradient neighbours for the
    normal estimate) and clamped to ``[0, N]``. Works on a non-uniform grid.
    """
    import numpy as np

    ncell = len(nodes_mm) - 1
    left = nodes_mm[:-1]
    right = nodes_mm[1:]
    overlap = np.nonzero((right > lo) & (left < hi))[0]
    if overlap.size == 0:
        # Shape falls between cell edges -- still touch the nearest cell.
        c = int(np.clip(np.searchsorted(nodes_mm, 0.5 * (lo + hi)) - 1,
                        0, ncell - 1))
        return max(0, c - margin), min(ncell, c + 1 + margin)
    a = max(0, int(overlap[0]) - margin)
    b = min(ncell, int(overlap[-1]) + 1 + margin)
    return a, b


def _smooth_dielectric_body(arrays, body_shape, eps_r, mu_r,
                            nodes_mm, span, oversample, on_layer=None):
    """Subpixel-smooth one dielectric body into ``eps_x/y/z`` (+ mu) in place.

    *span* is ``((ia, ib), (ja, jb), (ka, kb))`` -- the half-open coarse sub-block
    covering the body's bbox (plus margin) from :func:`_cell_span`. The body is
    fine-sampled at *oversample* ``(ox, oy, oz)`` sub-cells per coarse cell per
    axis (one OCC section per fine Z sub-layer, matplotlib point-in-polygon over
    the fine XY sub-centres), then reduced with
    :func:`wavesim_gui.subpixel.reduce_fine_eps` to a diagonal effective tensor.

    The **background** inside the block is the current ``eps_x`` there, so bodies
    compose in placement order (mirrors the solver's repeated
    ``smooth_shape_region`` calls). ``mu_r != 1`` is applied by volume-fraction
    averaging; a dielectric that majority-covers a cell clears a PEC background.
    ``on_layer()`` is called once per fine Z sub-layer (progress + cancellation);
    a truthy return raises :class:`VoxelizationCancelled`.
    """
    import numpy as np

    from wavesim_gui import subpixel as sp

    nx_mm, ny_mm, nz_mm = nodes_mm
    (ia, ib), (ja, jb), (ka, kb) = span
    ox, oy, oz = sp.as_triplet(oversample)

    xf = sp.fine_axis(nx_mm, ox, ia, ib)
    yf = sp.fine_axis(ny_mm, oy, ja, jb)
    zf = sp.fine_axis(nz_mm, oz, ka, kb)

    # Chord tolerance for the fine section polygons: a quarter of the smallest
    # fine sub-cell width, so curves are tracked well below sub-cell resolution.
    def _min_sub(nodes, o, a, b):
        w = np.diff(nodes[a:b + 1])
        return (float(w.min()) / o) if w.size else 1.0

    df = min(_min_sub(nx_mm, ox, ia, ib), _min_sub(ny_mm, oy, ja, jb))
    deflection = max(0.25 * df, df * 1.0e-6)

    Z_AXIS = FreeCAD.Vector(0.0, 0.0, 1.0)
    inside_fine = np.zeros((xf.size, yf.size, zf.size), dtype=bool)
    for kz in range(zf.size):
        layer = _layer_inside_lattice(body_shape, Z_AXIS, float(zf[kz]),
                                      xf, yf, deflection)
        if layer is not None and layer.any():
            inside_fine[:, :, kz] = layer
        if on_layer is not None and on_layer():
            raise VoxelizationCancelled()

    # Fine permittivity field: the body's eps where inside, else the existing
    # (background) eps of the covering coarse cell, tiled to the sub-grid.
    bg = arrays["eps_x"][ia:ib, ja:jb, ka:kb]
    bg_fine = np.repeat(np.repeat(np.repeat(bg, ox, axis=0), oy, axis=1),
                        oz, axis=2)
    eps_fine = np.where(inside_fine, float(eps_r), bg_fine)
    ex, ey, ez = sp.reduce_fine_eps(eps_fine, (ox, oy, oz))
    arrays["eps_x"][ia:ib, ja:jb, ka:kb] = ex
    arrays["eps_y"][ia:ib, ja:jb, ka:kb] = ey
    arrays["eps_z"][ia:ib, ja:jb, ka:kb] = ez

    frac = sp.block_mean(inside_fine.astype(np.float64), (ox, oy, oz))
    if mu_r != 1.0:
        for key in ("mu_x", "mu_y", "mu_z"):
            mu_bg = arrays[key][ia:ib, ja:jb, ka:kb]
            arrays[key][ia:ib, ja:jb, ka:kb] = (
                frac * float(mu_r) + (1.0 - frac) * mu_bg
            )
    # A dielectric body clears a PEC background where it majority-covers a cell
    # (matching the coarse centre-inside rule to within half a cell).
    covered = frac >= 0.5
    if covered.any():
        sub = arrays["pec_mask"][ia:ib, ja:jb, ka:kb]
        sub[covered] = False


# --------------------------------------------------------------------------- #
# Conformal (Dey-Mittra / PBA) PEC open fractions
#
# A staircased conductor is only first-order accurate and, on a coax modal port,
# reads Z0 14% high and leaves a parasitic TE11 mode rattling between the ports
# forever (see CONFORMAL_PEC_PLAN.md). The cure is to let the solver's Faraday
# contour integrate the *cut* geometry, which needs six dimensionless arrays: the
# fraction of each Yee E edge, and of each Yee H face, that is NOT inside metal.
#
# The geometric conventions are the solver's (``wavesim.grid.FDTDGrid``), and
# there is exactly one definition of them across the process boundary:
#
#   pec_edge_open_x[i,j,k]  the Ex edge, node (i,j,k) -> (i+1,j,k)
#   pec_face_open_z[i,j,k]  the Hz face, nodes (i..i+1, j..j+1) at z-node k
#
# Fractions, not metres: the solver multiplies by its own spacing arrays, so the
# contract survives a graded grid.
# --------------------------------------------------------------------------- #

# The six ``materials.npz`` keys, in the order ``set_material_arrays`` takes them.
CONFORMAL_KEYS = (
    "pec_edge_open_x", "pec_edge_open_y", "pec_edge_open_z",
    "pec_face_open_x", "pec_face_open_y", "pec_face_open_z",
)

# Sub-samples per coarse cell per axis when measuring the open fractions. Higher
# than the subpixel smoother's 4 because an *edge* fraction is a mean of exactly
# this many samples, so it is quantised to 1/oversample -- the quantity the whole
# effort exists to resolve. The cost is not the cube of it (see
# :func:`_conformal_pec_body`: only the node planes carry a full 2D sub-block).
CONFORMAL_OVERSAMPLE = 8

# Chord tolerance for the conformal sampler's section polygons, as a fraction of
# the finest sub-cell width. Much tighter than the coarse sweep's 0.25*cell,
# which turns a 3 mm conductor into a ~10-gon (inscribed radius 2.853 mm) -- an
# error that would sit straight on top of the fractions and cap the accuracy
# conformal PEC buys. Deliberately *not* applied to the coarse sweep: that would
# move the binary pec_mask and break the "conformal off is bit-identical"
# guarantee. A polygon's radius error is its deflection, so this is ~0.6% of a
# coarse cell; tightening further costs polygon vertices, and matplotlib's
# point-in-polygon is linear in them.
_CONFORMAL_CHORD_FRACTION = 0.05


def _interleaved_block(nodes_mm, os_, a, b):
    """Sample coordinates for cells ``[a, b)``: ``(n, os_+1)``, node first.

    Column 0 of row ``c`` is the coordinate of node ``a + c`` (the cell's low
    edge); columns ``1..os_`` are the centres of its ``os_`` equal sub-intervals.

    The two kinds of point are carried on one lattice because the six reductions
    need both and need them *consistent*: an edge fraction is sub-centres along
    the edge's own axis but exactly **on** nodes across it. That is why
    :func:`wavesim_gui.subpixel.fine_axis` cannot serve here -- it places every
    sample at a sub-centre, and no sub-centre is ever a node.
    """
    import numpy as np

    nodes = np.asarray(nodes_mm, dtype=np.float64)
    left = nodes[a:b]
    width = nodes[a + 1:b + 1] - left
    frac = np.concatenate([[0.0], (np.arange(os_) + 0.5) / os_])
    return left[:, None] + frac[None, :] * width[:, None]


def _dilate_band(mask):
    """*mask* OR-ed with itself shifted +-1 cell along each axis (clamped)."""
    import numpy as np

    out = mask.copy()
    for ax in range(3):
        if mask.shape[ax] < 2:
            continue
        lo = [slice(None)] * 3
        hi = [slice(None)] * 3
        lo[ax] = slice(0, -1)
        hi[ax] = slice(1, None)
        out[tuple(lo)] |= mask[tuple(hi)]
        out[tuple(hi)] |= mask[tuple(lo)]
    return out


# The band's sample points are a *subset* of a lattice, not a lattice: only the
# cells straddling the surface are sampled. Testing them on the smallest lattice
# containing them (the band's occupied rows x its occupied columns) and gathering
# the wanted cells back is far cheaper anyway, because the scanline's cost barely
# depends on how many points it answers -- but only while the containing lattice
# stays comparable to the band. It does for a compact cross-section; for a long
# diagonal conductor the enclosing lattice is quadratic in the band, and past this
# ratio the flat matplotlib path is the better of the two. Kept well under the
# scanline's measured advantage so the chosen path always wins.
_BAND_LATTICE_MAX_WASTE = 12.0


def _band_lattice(ii, jj, xb, yb):
    """Smallest sample lattice containing band cells ``(ii, jj)``, or ``None``.

    Returns ``(xs, ys, mi, mj)``: the ascending sample coordinates of the
    occupied cell columns/rows (each cell contributing its node + sub-centres),
    and the index of each band cell within them, so a
    ``(len(ui), os_+1, len(uj), os_+1)`` view of the lattice result gathers back
    to one ``(n, os_+1, os_+1)`` sub-block per band cell. ``None`` asks the caller
    to use the flat path -- see :data:`_BAND_LATTICE_MAX_WASTE`.
    """
    import numpy as np

    ui = np.unique(ii)
    uj = np.unique(jj)
    if ui.size * uj.size > _BAND_LATTICE_MAX_WASTE * ii.size:
        return None
    return (xb[ui].ravel(), yb[uj].ravel(),
            np.searchsorted(ui, ii), np.searchsorted(uj, jj))


def _band_blocks_lattice(body_shape, z_axis, z, lat, os_, deflection):
    """Per-band-cell ``(n, os_+1, os_+1)`` occupancy at *z*, via one scanline.

    *lat* is :func:`_band_lattice`'s tuple. Returns ``None`` when the plane
    misses the solid.
    """
    xs, ys, mi, mj = lat
    grid = _layer_inside_lattice(body_shape, z_axis, z, xs, ys, deflection)
    if grid is None:
        return None
    view = grid.reshape(xs.size // (os_ + 1), os_ + 1,
                        ys.size // (os_ + 1), os_ + 1)
    return view[mi, :, mj, :]


def _band_blocks_flat(body_shape, z_axis, z, xs, ys, os_, deflection):
    """:func:`_band_blocks_lattice`'s fallback: one flat point list per cell.

    *xs*/*ys* are the ``(n, os_+1)`` per-cell sample coordinates.
    """
    import numpy as np

    px = np.repeat(xs, os_ + 1, axis=1)
    py = np.tile(ys, (1, os_ + 1))
    flat = _layer_inside(body_shape, z_axis, z,
                         np.column_stack([px.ravel(), py.ravel()]), deflection)
    if flat is None:
        return None
    return flat.reshape(xs.shape[0], os_ + 1, os_ + 1)


def _conformal_pec_body(covered, body_shape, nodes_mm, span, os_, on_layer=None):
    """Accumulate one PEC body's **covered** fractions into the six arrays.

    *covered* holds the six ``(Nx, Ny, Nz)`` float arrays of covered (not open)
    fraction; this body is merged in with a per-element ``maximum``. That is
    exact for conductors that do not overlap -- the normal case, and the only one
    with a well-posed answer -- and under-covers an edge shared by two
    *overlapping* PEC solids, since a maximum is not a union. Fusing the solids
    first would be exact and is a BREP boolean of unbounded cost, so it is not
    done; a model that needs it should fuse in CAD.

    *span* is ``((ia, ib), (ja, jb), (ka, kb))`` from :func:`_cell_span` with no
    margin -- the half-open coarse block covering the body's bounding box.

    Two passes:

    1. **Node lattice** (``nk+1`` sections). A coarse cell whose eight corner
       nodes agree is settled with no further work: all-inside contributes 1 to
       all six fractions, all-outside contributes 0. Only *mixed* cells --
       dilated one cell, so a surface passing between two corner samples is still
       caught -- reach the fine pass. This is what keeps a PEC-heavy model
       affordable; without it every conductor cell would be fine-sampled.
       A feature thinner than a cell can slip the band entirely, which is the
       sub-cell conductor the formulation cannot represent anyway.

    2. **Fine lattice**, band cells only. Deliberately *not* the full
       ``(os+1)^3`` block: which samples a plane needs depends on whether its z
       coordinate is a node or a sub-centre.

       - a **z-node** plane carries the three quantities with no z sub-sampling
         (``edge_x``, ``edge_y``, ``face_z``) and needs the whole ``(os+1)^2``
         xy block;
       - a **z-sub** plane carries the other three (``edge_z``, ``face_x``,
         ``face_y``), every one of which is at a node in x or in y, so it needs
         only the ``2*os+1`` point *cross* through the cell's low corner.

       That is ``~3*os^2`` samples per cell rather than ``os^3``.

       Band cells are scattered, so their samples are a subset of a lattice
       rather than one. :func:`_band_lattice` supplies the smallest lattice
       containing them, which :func:`_layer_inside_lattice` answers in one
       scanline pass and the caller gathers back per cell -- cheap enough that a
       z-sub plane takes the whole sub-block and slices its cross out of it.
       When that enclosing lattice would be much larger than the band itself the
       helper declines and the original per-cell flat sampling runs instead; both
       produce the same arrays.

    ``on_layer()`` is called once per OCC section (progress + cancellation); a
    truthy return raises :class:`VoxelizationCancelled`.
    """
    import numpy as np

    nx_mm, ny_mm, nz_mm = nodes_mm
    (ia, ib), (ja, jb), (ka, kb) = span
    ni, nj, nk = ib - ia, jb - ja, kb - ka
    if ni <= 0 or nj <= 0 or nk <= 0:
        return

    xb = _interleaved_block(nx_mm, os_, ia, ib)          # (ni, os_+1)
    yb = _interleaved_block(ny_mm, os_, ja, jb)
    zb = _interleaved_block(nz_mm, os_, ka, kb)

    # Chord tolerance from the finest in-plane sub-cell in this block (see
    # _CONFORMAL_CHORD_FRACTION).
    def _min_sub(nodes, a, b):
        w = np.diff(nodes[a:b + 1])
        return (float(w.min()) / os_) if w.size else 1.0

    df = min(_min_sub(nx_mm, ia, ib), _min_sub(ny_mm, ja, jb))
    deflection = max(_CONFORMAL_CHORD_FRACTION * df, df * 1.0e-6)

    Z_AXIS = FreeCAD.Vector(0.0, 0.0, 1.0)

    def _tick():
        if on_layer is not None and on_layer():
            raise VoxelizationCancelled()

    # ---------------- pass 1: node lattice -> the surface band -------------- #
    xn, yn, zn = nx_mm[ia:ib + 1], ny_mm[ja:jb + 1], nz_mm[ka:kb + 1]
    node_in = np.zeros((ni + 1, nj + 1, nk + 1), dtype=bool)
    for kk in range(nk + 1):
        layer = _layer_inside_lattice(body_shape, Z_AXIS, float(zn[kk]),
                                      xn, yn, deflection)
        if layer is not None and layer.any():
            node_in[:, :, kk] = layer
        _tick()

    c = node_in
    corners = (c[:-1, :-1, :-1], c[1:, :-1, :-1], c[:-1, 1:, :-1], c[1:, 1:, :-1],
               c[:-1, :-1, 1:], c[1:, :-1, 1:], c[:-1, 1:, 1:], c[1:, 1:, 1:])
    all_in = corners[0].copy()
    any_in = corners[0].copy()
    for arr in corners[1:]:
        all_in &= arr
        any_in |= arr
    band = _dilate_band(any_in & ~all_in)

    # A settled cell is fully covered exactly when all eight corners are inside;
    # band cells are overwritten below.
    local = {key: all_in.astype(np.float64) for key in CONFORMAL_KEYS}

    # ---------------- pass 2: fine lattice over the band ------------------- #
    bi, bj, bk = np.nonzero(band)
    if bi.size:
        for k in np.unique(bk):
            sel = bk == k
            ii, jj = bi[sel], bj[sel]
            xs, ys = xb[ii], yb[jj]                      # (n, os_+1) each
            n = ii.size
            lat = _band_lattice(ii, jj, xb, yb)

            # z-node plane: the full (os_+1)^2 xy sub-block per cell.
            layer = (_band_blocks_lattice(body_shape, Z_AXIS, float(zb[k, 0]),
                                          lat, os_, deflection)
                     if lat is not None else
                     _band_blocks_flat(body_shape, Z_AXIS, float(zb[k, 0]),
                                       xs, ys, os_, deflection))
            _tick()
            blk = (np.zeros((n, os_ + 1, os_ + 1), dtype=bool)
                   if layer is None else layer)
            local["pec_edge_open_x"][ii, jj, k] = blk[:, 1:, 0].mean(axis=1)
            local["pec_edge_open_y"][ii, jj, k] = blk[:, 0, 1:].mean(axis=1)
            local["pec_face_open_z"][ii, jj, k] = blk[:, 1:, 1:].mean(axis=(1, 2))

            # z-sub planes: only the 2*os_+1 point cross through the low corner
            # is needed -- [0] node/node, [1:1+os_] x-node/y-sub, [1+os_:]
            # x-sub/y-node. The flat path samples exactly that; the lattice path
            # answers the whole sub-block for the same work and slices it.
            if lat is None:
                px = np.concatenate([xs[:, :1], np.repeat(xs[:, :1], os_, axis=1),
                                     xs[:, 1:]], axis=1)
                py = np.concatenate([ys[:, :1], ys[:, 1:],
                                     np.repeat(ys[:, :1], os_, axis=1)], axis=1)
                cross_pts = np.column_stack([px.ravel(), py.ravel()])
            cross = np.zeros((n, os_, 1 + 2 * os_), dtype=bool)
            for m in range(os_):
                z = float(zb[k, m + 1])
                if lat is None:
                    layer = _layer_inside(body_shape, Z_AXIS, z,
                                          cross_pts, deflection)
                    _tick()
                    if layer is not None and layer.any():
                        cross[:, m, :] = layer.reshape(n, 1 + 2 * os_)
                    continue
                sub = _band_blocks_lattice(body_shape, Z_AXIS, z, lat, os_,
                                           deflection)
                _tick()
                if sub is not None and sub.any():
                    cross[:, m, 0] = sub[:, 0, 0]
                    cross[:, m, 1:1 + os_] = sub[:, 0, 1:]
                    cross[:, m, 1 + os_:] = sub[:, 1:, 0]
            local["pec_edge_open_z"][ii, jj, k] = cross[:, :, 0].mean(axis=1)
            local["pec_face_open_x"][ii, jj, k] = (
                cross[:, :, 1:1 + os_].mean(axis=(1, 2)))
            local["pec_face_open_y"][ii, jj, k] = (
                cross[:, :, 1 + os_:].mean(axis=(1, 2)))

    for key in CONFORMAL_KEYS:
        target = covered[key][ia:ib, ja:jb, ka:kb]
        np.maximum(target, local[key], out=target)


def conformal_layer_estimate(nk, os_):
    """Upper bound on the OCC sections :func:`_conformal_pec_body` will cut.

    ``nk + 1`` node planes always, plus ``1 + os_`` per cell layer that turns out
    to hold a band cell. The band is unknown until the first pass has run, so
    this over-counts for a body whose surface does not reach every layer -- a
    progress bar that finishes early rather than one that overruns.
    """
    return (nk + 1) + nk * (1 + os_)


def voxelize_materials(materials, cell_size_m,
                       spacing_lo_m=(0.0, 0.0, 0.0), spacing_hi_m=(0.0, 0.0, 0.0),
                       pad_lo=(8, 8, 8), pad_hi=(8, 8, 8),
                       extra_points_mm=(), extra_axis_offsets=(),
                       bg_eps=1.0, bg_mu=1.0, bg_pec=False,
                       nodes_m=None, subpixel=False, oversample=4,
                       conformal=False, conformal_oversample=None,
                       max_total_cells=10_000_000, progress=None):
    """Voxelise *materials* onto a regular grid bounding all their bodies.

    Parameters
    ----------
    materials : list
        Material document objects (see :mod:`wavesim_gui.materials`).
    cell_size_m : tuple
        ``(dx, dy, dz)`` cell sizes in metres, taken from the Grid object. There
        is intentionally no auto-chosen default: the cell size is a deliberate
        user decision, so the caller must supply one (see
        :func:`build_job_from_document`, which refuses to run without a Grid).
    spacing_lo_m, spacing_hi_m : tuple of float
        Background gap (metres) added outside the material bounds on the low/high
        side of x, y, z, before any PML padding. From the Domain object's
        per-face ``Spacing*`` properties (all zero with no domain).
    pad_lo, pad_hi : tuple of int
        Per-axis PML padding in cells on the low/high side of x, y, z. From the
        Domain's per-face boundary settings; the legacy default is 8 cells all
        round (room for PML when no domain has been defined yet).
    extra_points_mm : iterable of (x, y, z)
        Extra world-mm points the grid must contain (the source positions). The
        bounding box is grown to include them so a source outside the material
        bounds still lands inside the grid, matching the auto-enlarged domain.
    extra_axis_offsets : iterable of (axis, value_mm)
        Single-axis constraints the grid must contain (snapshot slice offsets,
        which only bound their normal axis). Grows the box on that axis only.
    nodes_m : tuple of array, optional
        Explicit per-axis node coordinates ``(x, y, z)`` in **world metres**
        (strictly increasing, PML pad cells included) from the Domain's graded
        grid. When given, the grid extent/cell centres come from these directly
        and *cell_size_m*/*spacing_**/*pad_lo*/*pad_hi*/*extra_** are ignored (the
        node arrays already bake them in). When ``None`` (the uniform default),
        a regular grid is derived from *cell_size_m* + the bounds, exactly as
        before. Cell centres are always ``0.5*(nodes[:-1]+nodes[1:])``, so the
        two paths coincide bit-for-bit on a uniform grid.
    subpixel : bool
        When True, each **dielectric** body is placed with subpixel smoothing:
        its boundary cells receive the anisotropic effective permittivity from
        :func:`wavesim_gui.subpixel.reduce_fine_eps` instead of being snapped to
        whole cells (anti-staircasing; ~2nd-order accuracy; smooth variation with
        geometry). PEC bodies are unaffected (a hard field constraint, not a
        material average). When False (default) every body is snapped as before
        and ``eps_x == eps_y == eps_z``.
    oversample : int or (int, int, int)
        Sub-samples per cell per axis used when ``subpixel=True`` (default 4).
        Higher is more accurate but costs ``O(oversample^3)`` setup memory/time
        per body's bounding box.
    conformal : bool
        When True, each **PEC** body additionally contributes to the six
        conformal open-fraction arrays (:data:`CONFORMAL_KEYS`), which let the
        solver's Faraday contour integrate the cut geometry instead of a
        staircase. The arrays are returned only if some face is genuinely *cut*
        (a fraction strictly between 0 and 1); an axis-aligned model that lands
        on cell edges emits nothing and runs the untouched staircase path. A PEC
        **background** material disables it outright -- a solid-metal domain has
        no cut geometry, and the "a dielectric clears a PEC background where it
        majority-covers a cell" rule has no conformal analogue. ``pec_mask`` is
        produced exactly as before either way.
    conformal_oversample : int, optional
        Sub-samples per coarse cell per axis for the conformal fractions
        (default :data:`CONFORMAL_OVERSAMPLE`). An edge fraction is the mean of
        this many samples, so it is quantised to ``1/conformal_oversample``.
    bg_eps, bg_mu, bg_pec : float / float / bool
        The background medium filling every "empty" voxel -- the eps/mu/PEC of
        the Domain's chosen background Material (vacuum: ``1.0, 1.0, False``).
        The arrays start filled with these; bodies overwrite the cells they
        cover.
    max_total_cells : int
        Guard against an accidentally huge grid; raises ``ValueError`` above it.
    progress : callable, optional
        ``progress(done, total)`` called after each Z-layer of the section sweep,
        where the units are body cross-section planes processed. Return truthy to
        abort, which raises :class:`VoxelizationCancelled`.

    Returns
    -------
    dict
        ``arrays``  : the six ``eps``/``mu`` arrays + ``pec_mask`` (numpy), plus
                      the six :data:`CONFORMAL_KEYS` arrays when *conformal* is
                      on and the geometry actually cuts a face.
        ``grid``    : ``{Nx, Ny, Nz, dx, dy, dz}`` with spacings in metres.
        ``origin_m``: domain min corner in FreeCAD world metres.
        ``counts``  : ``{dielectric_cells, pec_cells}`` for a quick sanity check,
                      plus ``{cut_faces, min_open_face}`` for a conformal run.
                      ``min_open_face`` is worth watching: the solver's
                      small-cut stability threshold clamps every face below it,
                      and a run whose smallest open face is far under the
                      threshold is the case that has been measured to diverge.
    """
    import numpy as np

    entries = _gather(materials)
    if not entries:
        raise ValueError("No solid bodies are assigned to any material.")

    # Per-axis node coordinates (world mm) spanning the padded grid. Either
    # supplied explicitly (the Domain's graded grid) or derived as a uniform grid
    # bounding the geometry + extras. Both then share one centre-based sweep.
    if nodes_m is not None:
        nodes_mm = tuple(
            np.ascontiguousarray(a, dtype=np.float64) * _MM_PER_M for a in nodes_m
        )
    else:
        bbox = _expand_bbox_points(_combined_bbox(entries), extra_points_mm)
        bbox = _expand_bbox_axis(bbox, extra_axis_offsets)
        dx_mm, dy_mm, dz_mm = (c * _MM_PER_M for c in cell_size_m)
        (Nx, Ny, Nz), (ox, oy, oz) = _grid_extent(
            bbox, (dx_mm, dy_mm, dz_mm),
            tuple(s * _MM_PER_M for s in spacing_lo_m),
            tuple(s * _MM_PER_M for s in spacing_hi_m),
            pad_lo, pad_hi,
        )
        nodes_mm = (
            ox + np.arange(Nx + 1) * dx_mm,
            oy + np.arange(Ny + 1) * dy_mm,
            oz + np.arange(Nz + 1) * dz_mm,
        )

    nx_mm, ny_mm, nz_mm = nodes_mm
    Nx, Ny, Nz = nx_mm.size - 1, ny_mm.size - 1, nz_mm.size - 1
    ox, oy, oz = float(nx_mm[0]), float(ny_mm[0]), float(nz_mm[0])
    # Representative scalar spacings: the constant cell size on a uniform grid,
    # the minimum width on a graded one (matching the solver's scalar grid.dx).
    dx_mm = float(np.diff(nx_mm).min())
    dy_mm = float(np.diff(ny_mm).min())
    dz_mm = float(np.diff(nz_mm).min())

    total = Nx * Ny * Nz
    if total > max_total_cells:
        raise ValueError(
            "Voxel grid too large: {}x{}x{} = {:,} cells (limit {:,}). "
            "Use a coarser grid or smaller geometry.".format(
                Nx, Ny, Nz, total, max_total_cells
            )
        )

    shape = (Nx, Ny, Nz)
    # Start every voxel as the background medium; bodies overwrite their cells.
    # eps/mu are per-axis (diagonal) from the outset so subpixel smoothing can
    # make boundary cells anisotropic; with smoothing off the three stay equal
    # (bit-for-bit the old single-array behaviour).
    eps_x = np.full(shape, float(bg_eps), dtype=np.float64)
    eps_y = np.full(shape, float(bg_eps), dtype=np.float64)
    eps_z = np.full(shape, float(bg_eps), dtype=np.float64)
    mu_x = np.full(shape, float(bg_mu), dtype=np.float64)
    mu_y = np.full(shape, float(bg_mu), dtype=np.float64)
    mu_z = np.full(shape, float(bg_mu), dtype=np.float64)
    pec_mask = np.full(shape, bool(bg_pec), dtype=bool)
    arrays = {
        "eps_x": eps_x, "eps_y": eps_y, "eps_z": eps_z,
        "mu_x": mu_x, "mu_y": mu_y, "mu_z": mu_z,
        "pec_mask": pec_mask,
    }

    # Subpixel oversampling factors (only used for dielectric bodies when on).
    if subpixel:
        from wavesim_gui import subpixel as _sp

        ovr = _sp.as_triplet(oversample)
    else:
        ovr = (1, 1, 1)

    # Conformal PEC: a solid-metal background has no cut geometry to measure, and
    # the dielectric-clears-PEC composition rule the coarse sweep uses has no
    # conformal counterpart -- so fall back to the staircase path rather than
    # emit fractions that disagree with the mask.
    conformal = bool(conformal) and not bool(bg_pec)
    c_ovr = int(conformal_oversample or CONFORMAL_OVERSAMPLE)
    covered = None
    if conformal:
        covered = {key: np.zeros(shape, dtype=np.float64)
                   for key in CONFORMAL_KEYS}

    Z_AXIS = FreeCAD.Vector(0.0, 0.0, 1.0)
    tol = min(dx_mm, dy_mm, dz_mm) * 1.0e-6
    # Chord tolerance for turning curved section edges into polygons: a quarter
    # of the smallest in-plane cell, so the polygon tracks curves to well below
    # cell resolution (never below the geometric tolerance).
    deflection = max(min(dx_mm, dy_mm) * 0.25, tol)

    # Cell-centre world coordinates (mm) along each axis, from the node arrays.
    # On a uniform grid this is exactly ``ox + (arange(N) + 0.5) * d``.
    xs = 0.5 * (nx_mm[:-1] + nx_mm[1:])
    ys = 0.5 * (ny_mm[:-1] + ny_mm[1:])
    zs = 0.5 * (nz_mm[:-1] + nz_mm[1:])

    def cell_range(lo, hi, axis_coords):
        """Indices of cell centres falling within [lo, hi] (a shape's bbox)."""
        return np.nonzero((axis_coords >= lo) & (axis_coords <= hi))[0]

    # Pre-plan each body's cell-index ranges so the total work (section planes to
    # sweep) is known up front -- lets a caller show a determinate progress bar
    # over the otherwise opaque, GUI-blocking sweep.
    plans = []
    total_layers = 0
    for body_shape, eps, mu, pec in entries:
        bb = body_shape.BoundBox
        # Only test cells whose centre lies inside this body's bounding box.
        i_idx = cell_range(bb.XMin, bb.XMax, xs)
        j_idx = cell_range(bb.YMin, bb.YMax, ys)
        k_idx = cell_range(bb.ZMin, bb.ZMax, zs)
        # Dielectric bodies are subpixel-smoothed when the option is on; PEC is
        # always snapped (a hard field constraint, not a material average).
        smooth = bool(subpixel) and not pec
        span = None
        if smooth:
            span = (
                _cell_span(nx_mm, bb.XMin, bb.XMax),
                _cell_span(ny_mm, bb.YMin, bb.YMax),
                _cell_span(nz_mm, bb.ZMin, bb.ZMax),
            )
            # Fine Z sub-layers swept over the (margin-grown) sub-block.
            (ka, kb) = span[2]
            n_layers = ovr[2] * (kb - ka)
        else:
            n_layers = len(k_idx)
        # A conformal PEC body pays for its binary sweep *and* the open-fraction
        # sampler, which is a second, finer pass over its own (margin-free) span.
        c_span = None
        if conformal and pec:
            c_span = (
                _cell_span(nx_mm, bb.XMin, bb.XMax, margin=0),
                _cell_span(ny_mm, bb.YMin, bb.YMax, margin=0),
                _cell_span(nz_mm, bb.ZMin, bb.ZMax, margin=0),
            )
            n_layers += conformal_layer_estimate(c_span[2][1] - c_span[2][0],
                                                 c_ovr)
        plans.append((body_shape, eps, mu, pec, i_idx, j_idx, k_idx, smooth,
                      span, c_span))
        total_layers += n_layers

    done_layers = 0
    if progress is not None:
        progress(0, total_layers)
    def _on_layer():
        nonlocal done_layers
        done_layers += 1
        return bool(progress is not None
                    and progress(done_layers, total_layers))

    for (body_shape, eps, mu, pec, i_idx, j_idx, k_idx, smooth,
         span, c_span) in plans:
        # Conformal open fractions for a PEC body, alongside (not instead of) the
        # binary mask below: pec_mask stays in the contract as the fully-covered
        # test and as the staircase path's own geometry.
        if c_span is not None:
            _conformal_pec_body(covered, body_shape, nodes_mm, c_span, c_ovr,
                                on_layer=_on_layer)
        if smooth:
            # Subpixel dielectric: fine-sample the body over its bbox sub-block
            # and reduce to an anisotropic effective permittivity (in place).
            _smooth_dielectric_body(
                arrays, body_shape, eps, mu, nodes_mm, span, ovr,
                on_layer=_on_layer,
            )
            continue
        if len(i_idx) == 0 or len(j_idx) == 0 or len(k_idx) == 0:
            continue
        # XY cell centres for this body's bbox -- a lattice, tested in a single
        # vectorised call per Z-layer cross-section.
        bx, by = xs[i_idx], ys[j_idx]
        for k in k_idx:
            inside = _layer_inside_lattice(body_shape, Z_AXIS, float(zs[k]),
                                           bx, by, deflection)
            if inside is not None and inside.any():
                ii, jj = np.nonzero(inside)
                gi, gj = i_idx[ii], j_idx[jj]
                if pec:
                    pec_mask[gi, gj, k] = True
                else:
                    eps_x[gi, gj, k] = eps
                    eps_y[gi, gj, k] = eps
                    eps_z[gi, gj, k] = eps
                    mu_x[gi, gj, k] = mu
                    mu_y[gi, gj, k] = mu
                    mu_z[gi, gj, k] = mu
                    # A dielectric body overrides a PEC background at its cells.
                    pec_mask[gi, gj, k] = False
            done_layers += 1
            if progress is not None and progress(done_layers, total_layers):
                raise VoxelizationCancelled()

    # Covered -> open, and only if the geometry genuinely cuts something. A model
    # whose conductors land on cell edges produces 0/1 fractions everywhere: the
    # conformal path would then reduce to the staircase one anyway, so emitting
    # nothing keeps that run on the untouched (and faster) kernel.
    counts = {
        "dielectric_cells": int(np.count_nonzero(eps_x != float(bg_eps))),
        "pec_cells": int(np.count_nonzero(pec_mask)),
    }
    if covered is not None:
        faces = [np.clip(1.0 - covered[key], 0.0, 1.0)
                 for key in CONFORMAL_KEYS[3:]]
        cut = sum(int(np.count_nonzero((f > 0.0) & (f < 1.0))) for f in faces)
        if cut:
            for key in CONFORMAL_KEYS:
                arrays[key] = np.clip(1.0 - covered[key], 0.0, 1.0)
            open_faces = [f[f > 0.0] for f in faces]
            open_faces = [f for f in open_faces if f.size]
            counts["cut_faces"] = cut
            counts["min_open_face"] = (
                float(min(f.min() for f in open_faces)) if open_faces else 1.0
            )

    grid_dict = {
        "Nx": Nx, "Ny": Ny, "Nz": Nz,
        "dx": dx_mm / _MM_PER_M,
        "dy": dy_mm / _MM_PER_M,
        "dz": dz_mm / _MM_PER_M,
    }
    # Solver-frame node coordinates (metres, origin at 0): the runner calls
    # create_grid_rectilinear with these. Only emitted for a genuinely
    # non-uniform grid -- a uniform run stays on create_grid, which sets exact
    # constant spacing arrays (create_grid_rectilinear derives them via
    # ``diff(coords)``, which rounds ~1 ULP off a uniform tick and would perturb
    # dt / the field evolution). The origin is baked into the voxel arrays, so
    # subtract it. The runner still writes plot coordinate arrays for both paths
    # from ``grid.x``/``grid.xc``, which exist on a uniform grid too.
    if nodes_m is not None:
        grid_dict["x"] = [(float(v) - ox) / _MM_PER_M for v in nx_mm]
        grid_dict["y"] = [(float(v) - oy) / _MM_PER_M for v in ny_mm]
        grid_dict["z"] = [(float(v) - oz) / _MM_PER_M for v in nz_mm]
    return {
        "arrays": arrays,
        "grid": grid_dict,
        "origin_m": (ox / _MM_PER_M, oy / _MM_PER_M, oz / _MM_PER_M),
        "counts": counts,
    }


def _report_conformal(active, counts, threshold):
    """Console report after a voxelisation that asked for conformal PEC.

    Says whether it took effect and, when it did, prints the two numbers that
    predict trouble. ``min_open_face`` is the important one: the solver clamps
    every face below *threshold* to keep ``dt`` untouched, and a run whose
    smallest cut is far under it is the case measured to diverge -- with the
    nasty property that stability is **not** monotone in resolution, so a finer
    mesh is no guarantee. Warning here costs nothing and is the only notice the
    user gets before a run that ends in NaN.
    """
    if not active:
        FreeCAD.Console.PrintWarning(
            "Wavesim: conformal PEC was requested but is not in effect "
            "(no conductor cuts a cell, or the background material is PEC). "
            "Running staircased.\n"
        )
        return
    cut = counts.get("cut_faces", 0)
    smallest = counts.get("min_open_face", 1.0)
    FreeCAD.Console.PrintMessage(
        "Wavesim: conformal PEC on -- {:,} cut faces, smallest open fraction "
        "{:.4f} (clamp threshold {:.2f}).\n".format(cut, smallest, threshold)
    )
    if smallest < 0.1 * threshold:
        FreeCAD.Console.PrintWarning(
            "Wavesim: the smallest cut cell is far below the clamp threshold "
            "({:.4f} vs {:.2f}). Conformal runs have been seen to diverge in "
            "this regime; if this one does, raise the Simulation's "
            "ConformalAreaThreshold (towards 0.5) rather than refining the "
            "mesh -- stability is not monotone in resolution.\n".format(
                smallest, threshold)
        )


def write_materials(workdir, arrays):
    """Save the voxelised material *arrays* to ``<workdir>/materials.npz``."""
    import os

    import numpy as np

    np.savez(os.path.join(workdir, "materials.npz"), **arrays)


def build_job_from_document(doc, steps=None, fmax=30.0e9, progress=None):
    """Build a solver job from the active simulation's materials.

    Returns ``(spec, arrays)`` where *spec* is the ``job.json`` dict and *arrays*
    is the voxelised material dict to write as ``materials.npz`` -- or ``(None, None)``
    if there is no simulation or no material-assigned geometry, so the caller can
    fall back to the Session-2 demo box.

    Raises :class:`GridRequiredError` if materials are assigned but no Domain
    object exists (it should always exist, created with the simulation).
    """
    from wavesim_gui.commands import active_simulation
    from wavesim_gui import materials as materials_mod
    from wavesim_gui import domain as domain_mod

    sim = active_simulation(doc)
    if sim is None:
        return None, None
    materials = materials_mod.find_materials(sim)
    if not materials:
        return None, None
    # Materials may exist (every new simulation seeds Vacuum + PEC) without any
    # bodies assigned yet. With nothing to voxelise, behave like an empty
    # document so the caller falls back to the demo box rather than erroring.
    if not _gather(materials):
        return None, None

    # The domain (cell sizes + boundaries) is created with the simulation; its
    # absence means a malformed document rather than something to guess around.
    dom = domain_mod.find_domain(sim)
    if dom is None:
        raise GridRequiredError(
            "Materials are assigned but the simulation has no Domain object. "
            "Re-create the simulation (Wavesim -> New Simulation)."
        )
    cell_size_m = domain_mod.cell_sizes_m(dom)

    # Number of time steps: derived from the simulation's maximum time and the
    # CFL step, unless an explicit count was passed. Fall back to a fixed count
    # for older documents that predate the MaxTime setting.
    if steps is None:
        max_time_s = float(getattr(sim, "MaxTime", 0.0))
        steps = domain_mod.time_steps_for(dom, max_time_s) or 800

    from wavesim_gui import modal_port as modal_mod
    from wavesim_gui import spice_port as spice_mod

    # The geometry is left to stop where the CAD stops -- nothing is extruded to
    # meet a port. A **modal port** face carries no PML pad and no background gap
    # (below), so the domain face lands exactly on the material bound and the port
    # plane cuts the real cross-section, which is what the mode solve needs; the
    # port then terminates the line there, and a conductor carried *past* it would
    # hang a second Z0 in parallel with the port instead (Z0||Z0, a measured
    # reflection of about -1/3 that rings for many round trips).

    # Faces launching a beam or a SPICE-TEM port are forced to PML (they drive an
    # interior plane and need the absorber behind it); faces carrying a modal port
    # lose their PML pad, PEC wall and background gap entirely. Both go through
    # the single source of truth, so the grid padding *and* the emitted boundary
    # (below) agree with the drawn box and node arrays, which are built from the
    # same two face lists.
    grid_params = domain_mod.domain_grid_params(
        dom,
        force_pml_faces=domain_mod.pml_port_faces(sim),
        modal_faces=domain_mod.modal_port_faces(sim),
    )
    spacing_lo = grid_params["spacing_lo"]
    spacing_hi = grid_params["spacing_hi"]
    pad_lo, pad_hi = grid_params["pad_lo"], grid_params["pad_hi"]
    # Background (empty-voxel) medium: the Domain's chosen background Material,
    # defaulting to vacuum when unset.
    bg_mat = domain_mod.background_material(dom)
    bg_eps = float(getattr(bg_mat, "Eps", 1.0)) if bg_mat is not None else 1.0
    bg_mu = float(getattr(bg_mat, "Mu", 1.0)) if bg_mat is not None else 1.0
    bg_pec = bool(getattr(bg_mat, "Pec", False)) if bg_mat is not None else False
    # Non-uniform grid: when the Domain's snapper is enabled, hand its explicit
    # node arrays to the voxeliser (which then ignores cell size / spacing / PML
    # padding -- the snapper already baked them in). Off (the default) leaves
    # nodes_m None so the voxeliser derives the usual uniform grid.
    nodes_m = None
    if getattr(dom, "UseNonuniformGrid", False):
        candidate = domain_mod.node_coords_m(dom)
        if all(len(a) >= 2 for a in candidate):
            nodes_m = candidate
    # Subpixel smoothing of dielectric interfaces: on unless the Simulation
    # container's checkbox is cleared (default True, and True for legacy
    # documents that predate the property).
    subpixel = bool(getattr(sim, "SubpixelSmoothing", True))
    # Conformal (Dey-Mittra) PEC: off unless the Simulation asks for it, and off
    # for legacy documents that predate the property.
    from wavesim_gui.commands import conformal_pec

    want_conformal, area_threshold = conformal_pec(sim)
    # Grow the grid to include every source position and snapshot slice, so an
    # input outside the material bounds (or in the PML) still lands inside it.
    vox = voxelize_materials(
        materials, cell_size_m,
        spacing_lo_m=spacing_lo, spacing_hi_m=spacing_hi,
        pad_lo=pad_lo, pad_hi=pad_hi,
        extra_points_mm=source_points_mm(sim),
        extra_axis_offsets=snapshot_axis_offsets(sim),
        bg_eps=bg_eps, bg_mu=bg_mu, bg_pec=bg_pec,
        nodes_m=nodes_m, subpixel=subpixel, conformal=want_conformal,
        progress=progress,
    )
    # What actually ran, not what was asked for: the fractions are absent when
    # the geometry cuts nothing (an axis-aligned model), when the background is
    # itself PEC, or when there are no conductors at all -- and in every one of
    # those cases the run is the ordinary staircase one. The runner echoes this
    # into summary.json and keys its backend choice off it, so it has to be the
    # truth rather than the request.
    conformal = all(key in vox["arrays"] for key in CONFORMAL_KEYS)
    if want_conformal:
        _report_conformal(conformal, vox["counts"], area_threshold)
    grid = vox["grid"]
    Nx, Ny, Nz = grid["Nx"], grid["Ny"], grid["Nz"]
    dx, dy, dz = grid["dx"], grid["dy"], grid["dz"]
    origin_m = vox["origin_m"]

    # Sources: the user-defined point source (Session 6) and the modal ports,
    # converted to the solver frame (the domain origin is baked into the voxel
    # arrays). With no point source and no port, fall back to a centre Gaussian
    # pulse so a bare run still works; a port is excitation enough, so the
    # fallback is skipped when one is present.
    from wavesim_gui import source as source_mod
    from wavesim_gui import spice_port as spice_mod
    from wavesim_gui import gaussian_beam as beam_mod

    # Modal ports split by drive mode: waveform-driven ones go into
    # ``modal_ports`` (the runner builds a ``ws.ModalPort`` boundary from each);
    # SPICE-driven ones are co-simulated, so they join the SPICE ports below (as
    # ``kind: "tem"`` entries) instead.
    port_objs = modal_mod.find_modal_ports(sim)
    modal_wave_objs = [p for p in port_objs
                       if modal_mod.excitation_mode(p) == modal_mod.MODE_WAVEFORM]
    modal_spice_objs = [p for p in port_objs
                        if modal_mod.excitation_mode(p) == modal_mod.MODE_SPICE]
    modal_ports = [modal_mod.modal_port_spec(p, origin_m) for p in modal_wave_objs]

    # Boundary Gaussian beams: launched from a (forced-PML) domain face; the
    # runner places the sheet from the face + the boundary's PML depth, so no
    # per-source geometry is needed here beyond the face/angle/waist/directional
    # flag. An auto waist is resolved against the domain by the spec.
    gaussian_beams = [beam_mod.gaussian_beam_spec(b, origin_m)
                      for b in beam_mod.find_gaussian_beams(sim)]

    # SPICE co-simulation ports (line + TEM); drop any that could not serialise
    # (e.g. a line port with no curve assigned).
    spice_line_specs = [spice_mod.spice_line_port_spec(p, origin_m)
                        for p in spice_mod.find_spice_line_ports(sim)]
    # SPICE-driven TEM ports = legacy SpiceTEMPort objects + modal ports whose
    # drive mode is SPICE. Both share the same TEM-plane spec builder.
    spice_tem_objs = spice_mod.find_spice_tem_ports(sim) + modal_spice_objs
    spice_tem_specs = [spice_mod.spice_tem_port_spec(p, origin_m)
                       for p in spice_tem_objs]
    spice_ports = [s for s in (spice_line_specs + spice_tem_specs) if s]

    sources = source_mod.find_sources(sim)
    if sources:
        source = source_mod.source_spec(sources[0], origin_m)
    elif modal_ports or spice_ports or gaussian_beams:
        # A modal port, a Gaussian beam or a (driven) SPICE port is excitation
        # enough; skip the centre-Gaussian fallback.
        source = None
    else:
        source = {
            "component": "Ez",
            "x": (Nx // 2) * dx, "y": (Ny // 2) * dy, "z": (Nz // 2) * dz,
            "fmax": fmax,
            "amplitude": 1.0,
        }

    # Boundary: from the Domain's per-face settings when one exists (with beam /
    # SPICE-TEM faces already forced to PML and modal-port faces already stripped
    # of both lists in ``grid_params`` above, so padding and boundary condition
    # agree), else the legacy auto heuristic (in-plane faces for a thin domain,
    # all six otherwise; no PEC walls).
    if dom is not None:
        pml_faces = grid_params["pml_faces"]
        pec_faces = grid_params["pec_faces"]
        d_pml = grid_params["d_pml"]
    else:
        if Nz == 1:
            pml_faces = ["x0", "x1", "y0", "y1"]
        else:
            pml_faces = ["x0", "x1", "y0", "y1", "z0", "z1"]
        pec_faces = []
        # PML thickness: scale gently with the in-plane size, clamped to a sane
        # band and never thicker than a quarter of the smallest absorbing axis.
        d_pml = max(4, min(10, min(Nx, Ny) // 6))
        d_pml = min(d_pml, min(Nx, Ny) // 4)

    # Monitors: the user-defined probes/snapshots/energy (Session 7), converted to
    # the solver frame. With none defined the job records nothing -- add an Energy
    # monitor explicitly to get the whole-domain energy diagnostic.
    from wavesim_gui import monitors as monitors_mod

    monitors = monitors_mod.monitors_spec(sim, origin_m)

    spec = {
        # backend is stamped by job.write_job from settings (default 'auto',
        # which the runner resolves to the CUDA GPU when one is available).
        "steps": int(steps),
        "grid": grid,
        # Run provenance, echoed into summary.json by the runner alongside
        # backend/pml_faces: whether this run's dielectric boundaries were
        # smoothed, and whether its conductors were cut cells. Records what
        # actually ran, which the Simulation container cannot answer later once
        # the user flips a checkbox.
        "subpixel": subpixel,
        "conformal_pec": conformal,
        "conformal_area_threshold": area_threshold,
        "boundary": {
            "d_pml": int(d_pml),
            "faces": pml_faces,
            "pec_faces": pec_faces,
        },
        "source": source,
        "modal_ports": modal_ports,
        "gaussian_beams": gaussian_beams,
        "spice_ports": spice_ports,
        "monitors": monitors,
    }
    return spec, vox["arrays"]
