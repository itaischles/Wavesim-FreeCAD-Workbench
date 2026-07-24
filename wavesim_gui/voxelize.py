# -*- coding: utf-8 -*-
"""Geometry voxelisation and job-from-document building (FreeCAD side).

Session 3 replaces the hardcoded Session-2 material box with real CAD geometry.
:func:`voxelize_materials` samples each Material's bodies onto a regular grid
(one planar ``Shape.slice`` per Z-layer, then a vectorised point-in-polygon test
of that cross-section over the layer's cell centres) to fill the per-cell
``eps``/``mu`` arrays and ``pec_mask`` the solver consumes via
``set_material_arrays``.
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

TEM ports need the guide to continue through the absorber, so after the sweep
each port face's cross-section is extruded through its spacing + PML cells
(:func:`_extrude_port_faces`); otherwise the conductors stop at the geometry and
the mode re-reflects off the empty-vacuum PML behind the launch plane.
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

# Unit vector of each axis (for plane slicing) and the two transverse axes of a
# normal in the solver's mode-slice order (matching ``mode_solver._NORMAL_CFG``
# and ``tem_source._TRANSVERSE``): the connectivity-preserving mode mesh emits
# its ``(a, b)`` arrays in this order.
_AXIS_VEC = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}
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
    (with TEM-port faces forced to PML, matching the drawn box and the run so the
    derived cell counts include the port-face absorber padding); otherwise falls
    back to the legacy uniform ``default_padding`` cells on every side with no
    background spacing (so a document without a domain runs as before).
    """
    from wavesim_gui import domain as domain_mod

    dom = domain_mod.find_domain(sim) if sim else None
    if dom is not None:
        p = domain_mod.domain_grid_params(
            dom, force_pml_faces=domain_mod.tem_port_faces(sim)
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


def _layer_inside(body_shape, z_axis, z, pts, deflection):
    """Boolean mask of which *pts* (XY, mm) lie inside *body_shape* at height *z*.

    Cuts the body with the horizontal plane at *z* -- one OCC section per layer
    instead of one ``isInside`` per cell -- turns each cross-section wire into a
    polygon (curved edges discretised to chord tolerance *deflection*), and tests
    every point at once with matplotlib. XOR-ing the wires applies the even-odd
    rule, which carves holes and handles solids nested inside holes. Returns
    ``None`` when the plane misses the solid (no section wires), so the caller can
    leave that whole layer empty.
    """
    import numpy as np
    from matplotlib.path import Path

    try:
        wires = body_shape.slice(z_axis, z)
    except Exception:
        return None
    if not wires:
        return None
    inside = np.zeros(len(pts), dtype=bool)
    any_wire = False
    for w in wires:
        try:
            verts = w.discretize(Deflection=deflection)
        except Exception:
            continue
        if len(verts) < 3:
            continue
        poly = np.array([(v.x, v.y) for v in verts])
        inside ^= Path(poly).contains_points(pts)
        any_wire = True
    return inside if any_wire else None


def _extrude_port_faces(arrays, port_faces, bg_eps=1.0, bg_mu=1.0, bg_pec=False):
    """Make the material arrays invariant along each TEM port's normal axis.

    For every face in *port_faces* (solver names ``'x0'``..``'z1'``) the
    boundary-most material cross-section is copied outward across the background
    and PML cells, out to the grid edge. A waveguide mode is only guided where
    its PEC cross-section is invariant along the propagation axis; without this
    the conductors stop at the geometry and the PML behind a port plane is empty
    vacuum, so the mode is unsupported there and partially re-reflects -- a port
    that looks like a semi-open circuit. Only TEM-port faces are touched: their
    cross-section is uniform by construction so the copy is exact, whereas doing
    this on an arbitrary PML face could smear non-uniform geometry into the
    absorber. Modifies *arrays* in place.

    ``bg_eps``/``bg_mu``/``bg_pec`` describe the background medium so "has
    geometry" is detected relative to it (a non-vacuum background otherwise marks
    every cell as filled).
    """
    import numpy as np

    axis_of = {"x": 0, "y": 1, "z": 2}
    keys = ("eps_x", "eps_y", "eps_z", "mu_x", "mu_y", "mu_z", "pec_mask")
    # A cell holds geometry if it differs from the background medium. Computed
    # once from the original geometry so two ports on one axis both extrude the
    # real cross-section rather than each other's fill.
    present = arrays["pec_mask"] != bool(bg_pec)
    for key in ("eps_x", "eps_y", "eps_z"):
        present |= arrays[key] != bg_eps
    for key in ("mu_x", "mu_y", "mu_z"):
        present |= arrays[key] != bg_mu

    for face in port_faces:
        axis = axis_of.get(face[0])
        if axis is None:
            continue
        is_high = face.endswith("1")
        layers = present.any(axis=tuple(a for a in range(3) if a != axis))
        filled = np.nonzero(layers)[0]
        if filled.size == 0:
            continue  # no geometry along this axis -- nothing to extrude
        k = int(filled[-1] if is_high else filled[0])
        sel = [slice(None)] * 3
        sel[axis] = slice(k + 1, None) if is_high else slice(None, k)
        src = [slice(None)] * 3
        src[axis] = slice(k, k + 1)  # keep the axis (length 1) so it broadcasts
        for key in keys:
            arr = arrays[key]
            arr[tuple(sel)] = arr[tuple(src)]


# --------------------------------------------------------------------------- #
# Connectivity-preserving TEM mode mesh (Session B)
#
# At the FDTD cell size the voxeliser can shred one continuous PEC on a port
# plane into several disconnected cells, so the mode solver's
# ``ndimage.label(pec)`` miscounts conductors. These helpers re-voxelise *only
# that plane* on a finer transverse grid, auto-refining until the PEC connected-
# component count stabilises, and ship the fine 2D arrays to the runner (which
# solves the mode there and interpolates it back to launch on the coarse grid).
# The FDTD grid itself is untouched.
# --------------------------------------------------------------------------- #

def _plane_inside(body_shape, normal, pos_mm, a_coords, b_coords, deflection):
    """Boolean ``(Na, Nb)`` mask of transverse cell centres inside *body_shape*.

    Generalises :func:`_layer_inside` to any axis-aligned *normal* ('x'/'y'/'z').
    Slices the body with the plane at *pos_mm* along the normal, projects each
    section wire onto the two transverse axes (slice order, see
    :data:`_TRANSVERSE_AXES`), and even-odd point-in-polygon-tests the ``(a, b)``
    cell-centre grid. Returns ``None`` when the plane misses the solid.
    """
    import numpy as np
    from matplotlib.path import Path

    try:
        wires = body_shape.slice(FreeCAD.Vector(*_AXIS_VEC[normal]), float(pos_mm))
    except Exception:
        return None
    if not wires:
        return None
    ax_a, ax_b = _TRANSVERSE_AXES[normal]
    ga, gb = np.meshgrid(a_coords, b_coords, indexing="ij")
    pts = np.column_stack([ga.ravel(), gb.ravel()])
    inside = np.zeros(pts.shape[0], dtype=bool)
    any_wire = False
    for w in wires:
        try:
            verts = w.discretize(Deflection=deflection)
        except Exception:
            continue
        if len(verts) < 3:
            continue
        poly = np.array([(getattr(v, ax_a), getattr(v, ax_b)) for v in verts])
        inside ^= Path(poly).contains_points(pts)
        any_wire = True
    if not any_wire:
        return None
    return inside.reshape(len(a_coords), len(b_coords))


def _label_2d(mask):
    """Count 4-connected ``True`` components in a 2D boolean array.

    A compact numpy union-find standing in for ``scipy.ndimage.label`` (scipy is
    not in FreeCAD's bundled Python) with the same 4-connectivity the solver's
    ``ndimage.label`` uses by default, so the count here matches what
    :func:`wavesim.mode_solver.solve_tem_modes` would see on the same mask.
    """
    import numpy as np

    m = np.ascontiguousarray(mask, dtype=bool)
    if not m.any():
        return 0
    n = int(m.sum())
    idx = -np.ones(m.shape, dtype=np.int64)
    idx[m] = np.arange(n)
    parent = np.arange(n, dtype=np.int64)

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    # Adjacency pairs where both cells are True (4-connectivity: right + down).
    ra, rb = np.nonzero(m[:, :-1] & m[:, 1:])
    da, db = np.nonzero(m[:-1, :] & m[1:, :])
    u = np.concatenate([idx[ra, rb], idx[da, db]])
    v = np.concatenate([idx[ra, rb + 1], idx[da + 1, db]])
    for a, b in zip(u.tolist(), v.tolist()):
        ra_, rb_ = find(a), find(b)
        if ra_ != rb_:
            parent[max(ra_, rb_)] = min(ra_, rb_)
    return len({find(i) for i in range(n)})


def voxelize_port_plane(materials, normal, pos_mm, a_nodes_mm, b_nodes_mm,
                        bg_eps=1.0, bg_mu=1.0, bg_pec=False):
    """Voxelise the material cross-section on one plane into 2D ``(a, b)`` arrays.

    Returns ``(pec2d, eps2d, mu2d)`` shaped ``(Na, Nb)`` in transverse slice order
    (see :data:`_TRANSVERSE_AXES`), sampled at the cell centres of the node
    coordinate arrays *a_nodes_mm* / *b_nodes_mm* (world mm). Mirrors
    :func:`voxelize_materials`'s per-body assignment (later bodies overwrite
    earlier, a dielectric clears a PEC background) but for a single plane.
    """
    import numpy as np

    a_c = 0.5 * (a_nodes_mm[:-1] + a_nodes_mm[1:])
    b_c = 0.5 * (b_nodes_mm[:-1] + b_nodes_mm[1:])
    eps2d = np.full((a_c.size, b_c.size), float(bg_eps), dtype=np.float64)
    mu2d = np.full((a_c.size, b_c.size), float(bg_mu), dtype=np.float64)
    pec2d = np.full((a_c.size, b_c.size), bool(bg_pec), dtype=bool)
    da = float(np.diff(a_nodes_mm).min())
    db = float(np.diff(b_nodes_mm).min())
    deflection = max(min(da, db) * 0.25, min(da, db) * 1.0e-6)
    for body_shape, eps, mu, pec in _gather(materials):
        inside = _plane_inside(body_shape, normal, pos_mm, a_c, b_c, deflection)
        if inside is None or not inside.any():
            continue
        if pec:
            pec2d[inside] = True
        else:
            eps2d[inside] = eps
            mu2d[inside] = mu
            pec2d[inside] = False  # a dielectric body clears a PEC background
    return pec2d, eps2d, mu2d


def build_mode_mesh(materials, normal, pos_mm, span_mm, base_cell_mm,
                    bg_eps=1.0, bg_mu=1.0, bg_pec=False,
                    max_factor=128, max_cells=300_000,
                    min_resolved=64, stable_steps=2):
    """Auto-refine a transverse re-voxelisation until PEC connectivity stabilises.

    Voxelises the port plane over the transverse rectangle *span_mm*
    ``(a0, a1, b0, b1)`` (world mm, slice order) at successively finer cell sizes
    -- *base_cell_mm* ``(ca, cb)`` divided by factor 1, 2, 4, 8, ... -- counting
    the PEC connected components (:func:`_label_2d`) each pass. Coarse
    voxelisation over-counts conductors when it fragments a continuous PEC (a thin
    tube wall shatters into arcs); refining merges the fragments and the count
    *drops* toward the true value, then holds.

    Refinement continues **through flat steps** until the count is unchanged for
    *stable_steps* consecutive halvings *and* the mesh is genuinely fine
    (``min(Na, Nb) >= min_resolved``), or a cap (*max_cells* / *max_factor*) is
    hit. Stopping only on the very first flat step is wrong: a wall thinner than a
    cell stays *equally* fragmented for a halving or two before it connects, which
    would otherwise be mistaken for convergence at the coarse (fragmented) count.
    The ``min_resolved`` floor likewise forbids "converging" while still too coarse
    to represent the wall. Returns ``(a_nodes_mm, b_nodes_mm, pec2d, eps2d, mu2d)``
    at the finest mesh reached.

    Returns ``None`` when a mode mesh is unnecessary: no PEC on the plane, or the
    component count never dropped below the base-resolution count (connectivity is
    already stable, so the runner's ordinary coarse-slice solve is unchanged).
    """
    import numpy as np

    a0, a1, b0, b1 = span_mm
    span_a, span_b = a1 - a0, b1 - b0
    ca0, cb0 = base_cell_mm
    if span_a <= 0.0 or span_b <= 0.0 or ca0 <= 0.0 or cb0 <= 0.0:
        return None

    best = None
    best_count = None
    coarse_count = None
    stable = 0
    factor = 1
    while factor <= max_factor:
        Na = max(1, int(math.ceil(span_a / (ca0 / factor))))
        Nb = max(1, int(math.ceil(span_b / (cb0 / factor))))
        if Na * Nb > max_cells:
            break  # keep the finest mesh computed so far
        a_nodes = a0 + np.arange(Na + 1) * (span_a / Na)
        b_nodes = b0 + np.arange(Nb + 1) * (span_b / Nb)
        pec2d, eps2d, mu2d = voxelize_port_plane(
            materials, normal, pos_mm, a_nodes, b_nodes,
            bg_eps=bg_eps, bg_mu=bg_mu, bg_pec=bg_pec,
        )
        count = _label_2d(pec2d)
        if count == 0:
            return None  # no PEC on the plane; nothing to preserve
        if coarse_count is None:
            coarse_count = count
        stable = stable + 1 if count == best_count else 0
        best, best_count = (a_nodes, b_nodes, pec2d, eps2d, mu2d), count
        if stable >= stable_steps and min(Na, Nb) >= min_resolved:
            break  # count held across finer meshes at adequate resolution
        factor *= 2

    if best is None or best_count is None or coarse_count is None:
        return None
    # Only worth a fine mode mesh when refinement actually merged fragments; an
    # already-stable plane keeps today's coarse-slice behaviour (regression-safe).
    if best_count >= coarse_count:
        return None
    return best


_MODE_REFINE_RATIO = math.sqrt(2.0)  # per-axis cell growth per level (~2x face cells)


def build_mode_mesh_levels(materials, normal, pos_mm, span_mm, base_cell_mm,
                           bg_eps=1.0, bg_mu=1.0, bg_pec=False,
                           max_levels=5, max_cells=300_000, min_start_cells=8,
                           refine_ratio=_MODE_REFINE_RATIO):
    """A sequence of progressively finer transverse re-voxelisations of one plane.

    Unlike :func:`build_mode_mesh` (which auto-stops at the *connectivity* plateau
    and returns a single mesh), this emits a whole refinement ladder for the
    runner's characteristic-impedance convergence study: the port plane voxelised
    over the transverse rectangle *span_mm* ``(a0, a1, b0, b1)`` (world mm, slice
    order), each level scaling the previous transverse cell count per axis by
    *refine_ratio* (finest last). The default ratio is ``sqrt(2)`` per axis (~2x
    the face-cell count per level), a gentler step than plain doubling so more
    levels fit under *max_cells* while each remains a meaningful refinement. The
    runner solves the mode on each in turn and stops once its Z0 settles (see
    ``runner._solve_mode_convergence``), so the ladder only needs to be long enough
    to reach convergence; the tail is unused when it converges early.

    The coarsest level resolves each transverse axis with at least
    ``max(FDTD-cell count, min_start_cells)`` cells: *base_cell_mm* ``(ca, cb)`` is
    the FDTD transverse cell, but on a coarse grid it can span the geometry in a
    single cell, and the mode solver's ``np.gradient`` needs at least two cells per
    axis (one-cell axes crash with an ``IndexError``). *min_start_cells* is that
    floor, so even a coarse FDTD grid starts the study on a usable mesh. A
    non-integer *refine_ratio* can round two successive levels to the same count, so
    each axis is forced at least one cell finer than the previous level -- every
    level is genuinely a refinement (no wasted duplicate solve).

    *max_cells* counts transverse (face) cells only -- the mode solve happens on
    this 2D plane, so the cap governs mode-solver resolution, not the 3D FDTD grid.
    Refinement stops after *max_levels* entries or once the next level would exceed
    *max_cells* face cells (whichever comes first). Returns
    ``[(a_nodes_mm, b_nodes_mm, pec2d, eps2d, mu2d), ...]`` (at least one entry),
    or ``None`` when the plane carries no PEC (nothing to solve -> coarse path).
    """
    import numpy as np

    a0, a1, b0, b1 = span_mm
    span_a, span_b = a1 - a0, b1 - b0
    ca0, cb0 = base_cell_mm
    if span_a <= 0.0 or span_b <= 0.0 or ca0 <= 0.0 or cb0 <= 0.0:
        return None

    # Start count per axis: the FDTD-cell resolution over the span, floored so the
    # coarsest mesh always has >= 2 cells (min_start_cells) for a valid gradient.
    floor = max(2, int(min_start_cells))
    Na0 = max(floor, int(math.ceil(span_a / ca0)))
    Nb0 = max(floor, int(math.ceil(span_b / cb0)))
    ratio = max(1.0, float(refine_ratio))

    levels = []
    prev_na = prev_nb = 0
    for lvl in range(max(1, int(max_levels))):
        scale = ratio ** lvl
        # >= one cell finer than the last level so a rounded ratio never stalls.
        Na = max(prev_na + 1, int(round(Na0 * scale)))
        Nb = max(prev_nb + 1, int(round(Nb0 * scale)))
        if Na * Nb > max_cells:
            break  # too fine for this cap; keep the ladder built so far
        a_nodes = a0 + np.arange(Na + 1) * (span_a / Na)
        b_nodes = b0 + np.arange(Nb + 1) * (span_b / Nb)
        pec2d, eps2d, mu2d = voxelize_port_plane(
            materials, normal, pos_mm, a_nodes, b_nodes,
            bg_eps=bg_eps, bg_mu=bg_mu, bg_pec=bg_pec,
        )
        if _label_2d(pec2d) == 0:
            # No PEC at this resolution. If even the coarsest misses every
            # conductor there is nothing to solve; a finer level would only find
            # PEC the mode solve needs from level 0, so stop here.
            if not levels:
                return None
            break
        levels.append((a_nodes, b_nodes, pec2d, eps2d, mu2d))
        prev_na, prev_nb = Na, Nb
    return levels or None


def _plane_material_span_mm(materials, normal):
    """Transverse ``(a0, a1, b0, b1)`` world-mm extent of all bodies, slice order.

    The default mode-mesh span when no Session-A bounds rect is set: the plane
    region actually occupied by geometry, so the fine re-voxelisation concentrates
    its resolution on the conductors rather than the empty PML pads.
    """
    bbox = _combined_bbox(_gather(materials))
    if bbox is None:
        return None
    ax_a, ax_b = _TRANSVERSE_AXES[normal]
    lo = {"x": bbox.XMin, "y": bbox.YMin, "z": bbox.ZMin}
    hi = {"x": bbox.XMax, "y": bbox.YMax, "z": bbox.ZMax}
    return (lo[ax_a], hi[ax_a], lo[ax_b], hi[ax_b])


def _port_slice_pos_mm(materials, normal, face_is_high, normal_cell_mm):
    """World-mm coordinate along *normal* at which to sample the port section.

    A TEM port plane sits on a domain face, which is out in the spacing/PML region
    where the raw CAD does not reach -- slicing there misses the solid, or hits
    its exact end face (a floating-point-fragile degenerate slice that catches one
    end but not the other). Instead sample at the geometry's own boundary-most
    cross-section nearest the face, nudged just inside off the end face, mirroring
    how :func:`_extrude_port_faces` copies that cross-section out to the face for
    the coarse solve. Uses the PEC bodies' extent (the conductors define the
    guide), falling back to all bodies. ``None`` when there is no geometry.
    """
    bbox = _combined_bbox([e for e in _gather(materials) if e[3]]) \
        or _combined_bbox(_gather(materials))
    if bbox is None:
        return None
    lo = {"x": bbox.XMin, "y": bbox.YMin, "z": bbox.ZMin}[normal]
    hi = {"x": bbox.XMax, "y": bbox.YMax, "z": bbox.ZMax}[normal]
    if hi <= lo:
        return lo  # degenerate (zero-length along the normal): slice on the plane
    nudge = min(0.25 * normal_cell_mm, 0.49 * (hi - lo))
    return (hi - nudge) if face_is_high else (lo + nudge)


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
    gx, gy = np.meshgrid(xf, yf, indexing="ij")
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    shape2d = (xf.size, yf.size)
    inside_fine = np.zeros((xf.size, yf.size, zf.size), dtype=bool)
    for kz in range(zf.size):
        layer = _layer_inside(body_shape, Z_AXIS, float(zf[kz]), pts, deflection)
        if layer is not None and layer.any():
            inside_fine[:, :, kz] = layer.reshape(shape2d)
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


def voxelize_materials(materials, cell_size_m,
                       spacing_lo_m=(0.0, 0.0, 0.0), spacing_hi_m=(0.0, 0.0, 0.0),
                       pad_lo=(8, 8, 8), pad_hi=(8, 8, 8),
                       extra_points_mm=(), extra_axis_offsets=(),
                       port_faces=(), bg_eps=1.0, bg_mu=1.0, bg_pec=False,
                       nodes_m=None, subpixel=False, oversample=4,
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
    port_faces : iterable of str
        Solver face names (``'x0'``..``'z1'``) hosting a TEM port. Each port's
        cross-section is extruded through the spacing + PML cells on that face
        (see :func:`_extrude_port_faces`) so the guided mode exits the absorber
        without re-reflecting. Empty for a run with no TEM ports.
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
        ``arrays``  : the six ``eps``/``mu`` arrays + ``pec_mask`` (numpy).
        ``grid``    : ``{Nx, Ny, Nz, dx, dy, dz}`` with spacings in metres.
        ``origin_m``: domain min corner in FreeCAD world metres.
        ``counts``  : ``{dielectric_cells, pec_cells}`` for a quick sanity check.
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
        plans.append((body_shape, eps, mu, pec, i_idx, j_idx, k_idx, smooth, span))
        total_layers += n_layers

    done_layers = 0
    if progress is not None:
        progress(0, total_layers)
    for body_shape, eps, mu, pec, i_idx, j_idx, k_idx, smooth, span in plans:
        if smooth:
            # Subpixel dielectric: fine-sample the body over its bbox sub-block
            # and reduce to an anisotropic effective permittivity (in place).
            def _on_layer():
                nonlocal done_layers
                done_layers += 1
                return bool(progress is not None
                            and progress(done_layers, total_layers))

            _smooth_dielectric_body(
                arrays, body_shape, eps, mu, nodes_mm, span, ovr,
                on_layer=_on_layer,
            )
            continue
        if len(i_idx) == 0 or len(j_idx) == 0 or len(k_idx) == 0:
            continue
        # XY cell centres for this body's bbox, flattened to an (M, 2) point list
        # tested in a single vectorised call per Z-layer cross-section.
        gx, gy = np.meshgrid(xs[i_idx], ys[j_idx], indexing="ij")
        pts = np.column_stack([gx.ravel(), gy.ravel()])
        shape2d = (len(i_idx), len(j_idx))
        for k in k_idx:
            inside = _layer_inside(body_shape, Z_AXIS, float(zs[k]),
                                   pts, deflection)
            if inside is not None and inside.any():
                ii, jj = np.nonzero(inside.reshape(shape2d))
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

    # Extend each TEM-port cross-section through its spacing + PML cells so a
    # guided mode stays supported into the absorber (modifies arrays in place;
    # done before the counts below so they reflect the extruded geometry).
    _extrude_port_faces(arrays, port_faces, bg_eps=bg_eps, bg_mu=bg_mu,
                        bg_pec=bg_pec)
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
        "counts": {
            "dielectric_cells": int(np.count_nonzero(eps_x != float(bg_eps))),
            "pec_cells": int(np.count_nonzero(pec_mask)),
        },
    }


def write_materials(workdir, arrays):
    """Save the voxelised material *arrays* to ``<workdir>/materials.npz``."""
    import os

    import numpy as np

    np.savez(os.path.join(workdir, "materials.npz"), **arrays)


def _mode_mesh_geometry(dom, obj, materials, cell_size_m):
    """Resolve ``(normal, span_mm, base_cell_mm, slice_pos_mm)`` for a port's plane.

    Shared by the single-mesh and convergence-ladder attach paths. *span_mm* is
    the Session-A bounds rect if one is selected, else the transverse extent of the
    geometry; *slice_pos_mm* samples the CAD at the geometry's own boundary-most
    end near the face (not the domain face, which sits in empty spacing/PML).
    Returns ``None`` when there is no usable span or geometry to sample.
    """
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import tem_source as tem_mod

    face = str(getattr(obj, "Face", "z0"))
    normal = domain_mod.face_axis(face)
    rect = tem_mod._bounds_rect_mm(dom, face, getattr(obj, "BoundsSel", None))
    span_mm = rect if rect is not None else _plane_material_span_mm(
        materials, normal)
    if span_mm is None:
        return None
    ax_a, ax_b = _TRANSVERSE_AXES[normal]
    ia, ib = _AXIS_IDX[ax_a], _AXIS_IDX[ax_b]
    base_cell_mm = (cell_size_m[ia] * _MM_PER_M, cell_size_m[ib] * _MM_PER_M)
    slice_pos_mm = _port_slice_pos_mm(
        materials, normal, face.endswith("1"),
        cell_size_m[_AXIS_IDX[normal]] * _MM_PER_M,
    )
    if slice_pos_mm is None:
        return None
    return normal, span_mm, base_cell_mm, slice_pos_mm


def _attach_mode_meshes(arrays, dom, port_pairs, materials, cell_size_m,
                        origin_m, bg_eps, bg_mu, bg_pec, convergence=None):
    """Attach a ``mode_mesh`` block to each port spec that needs one.

    *port_pairs* is ``[(spec_dict, port_obj), ...]`` for every mode-solved port
    (TEM sources + SPICE-TEM ports). Two modes:

    * *convergence* is ``None`` (the default) -- the historical single-mesh path.
      :func:`build_mode_mesh` decides whether the coarse voxelisation fragments the
      PEC cross-section; when it does the fine 2D arrays land in *arrays* as
      ``modemesh_<i>_{pec,eps,mu}`` and the spec gains a single-level ``mode_mesh``.
      Ports whose connectivity is already stable are left untouched (coarse solve).

    * *convergence* ``{"max_iter", "rel_tol"}`` -- ship a whole refinement ladder
      (:func:`build_mode_mesh_levels`) so the runner can converge the port's
      characteristic impedance. The per-level arrays land as
      ``modemesh_<i>_lvl<j>_{pec,eps,mu}`` and the spec's ``mode_mesh`` carries a
      ``levels`` list plus the ``convergence`` criteria. Generated for every port
      with PEC on the plane (even one already connectivity-stable, since Z0 may
      still be under-resolved).

    Node coords are shifted into the solver frame. ``mode_mesh`` and ``bounds``
    stay mutually exclusive (the fine grid already spans the box), so a port that
    gains one has its ``bounds`` dropped from the spec.
    """
    import numpy as np

    mm_index = 0
    for spec, obj in port_pairs:
        face = str(getattr(obj, "Face", "z0"))
        geom = _mode_mesh_geometry(dom, obj, materials, cell_size_m)
        if geom is None:
            continue
        normal, span_mm, base_cell_mm, slice_pos_mm = geom
        ax_a, ax_b = _TRANSVERSE_AXES[normal]
        ia, ib = _AXIS_IDX[ax_a], _AXIS_IDX[ax_b]
        key = "modemesh_{}".format(mm_index)

        def _solver_nodes(nodes_mm, axis_idx):
            return [float(v) / _MM_PER_M - origin_m[axis_idx] for v in nodes_mm]

        if convergence is not None:
            # Convergence study: a refinement ladder the runner walks until Z0
            # settles. NB: no MinCellSize clamp -- the mode mesh deliberately
            # refines below the (possibly coarsened) FDTD grid.
            levels = build_mode_mesh_levels(
                materials, normal, slice_pos_mm, span_mm, base_cell_mm,
                bg_eps=bg_eps, bg_mu=bg_mu, bg_pec=bg_pec,
                max_levels=int(convergence["max_iter"]),
                max_cells=int(convergence.get("max_cells", 300_000)),
            )
            if not levels:
                continue
            level_specs = []
            for li, (a_nodes_mm, b_nodes_mm, pec2d, eps2d, mu2d) in enumerate(levels):
                lkey = "{}_lvl{}".format(key, li)
                arrays[lkey + "_pec"] = pec2d.astype(np.uint8)
                arrays[lkey + "_eps"] = eps2d
                arrays[lkey + "_mu"] = mu2d
                level_specs.append({
                    "a_nodes": _solver_nodes(a_nodes_mm, ia),
                    "b_nodes": _solver_nodes(b_nodes_mm, ib),
                })
            FreeCAD.Console.PrintMessage(
                "Wavesim: TEM port on {} -- impedance convergence study over up to "
                "{} mesh level(s) ({} to {}x{} cells, sampled at {}={:.3f} mm).\n"
                .format(
                    face, len(levels),
                    "{}x{}".format(levels[0][2].shape[0], levels[0][2].shape[1]),
                    levels[-1][2].shape[0], levels[-1][2].shape[1],
                    normal, slice_pos_mm,
                )
            )
            spec.pop("bounds", None)
            spec["mode_mesh"] = {
                "key": key,
                "normal": normal,
                "position": spec["position"],  # already solver frame
                "levels": level_specs,
                "convergence": {
                    "max_iter": int(convergence["max_iter"]),
                    "rel_tol": float(convergence["rel_tol"]),
                },
            }
            mm_index += 1
            continue

        # Single connectivity-preserving mesh (convergence off).
        mesh = build_mode_mesh(
            materials, normal, slice_pos_mm, span_mm, base_cell_mm,
            bg_eps=bg_eps, bg_mu=bg_mu, bg_pec=bg_pec,
        )
        if mesh is None:
            FreeCAD.Console.PrintLog(
                "Wavesim: TEM port on {} -- connectivity already stable at the "
                "grid resolution; no mode mesh.\n".format(face)
            )
            continue
        a_nodes_mm, b_nodes_mm, pec2d, eps2d, mu2d = mesh
        FreeCAD.Console.PrintMessage(
            "Wavesim: TEM port on {} -- coarse cross-section fragmented; solving "
            "the mode on a {}x{} connectivity-preserving fine mesh ({} PEC "
            "region(s), sampled at {}={:.3f} mm).\n".format(
                face, pec2d.shape[0], pec2d.shape[1], _label_2d(pec2d),
                normal, slice_pos_mm,
            )
        )
        spec.pop("bounds", None)
        spec["mode_mesh"] = {
            "key": key,
            "normal": normal,
            "position": spec["position"],  # already solver frame
            "a_nodes": _solver_nodes(a_nodes_mm, ia),
            "b_nodes": _solver_nodes(b_nodes_mm, ib),
        }
        arrays[key + "_pec"] = pec2d.astype(np.uint8)
        arrays[key + "_eps"] = eps2d
        arrays[key + "_mu"] = mu2d
        mm_index += 1


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

    from wavesim_gui import tem_source as tem_mod
    from wavesim_gui import spice_port as spice_mod

    # TEM (and SPICE-TEM) port faces launch a guided mode and must absorb it, so
    # force them to PML regardless of the Domain's per-face setting -- a face left
    # (or later set) to PEC would trap the launched mode. Their cross-section is
    # also extruded through the spacing + PML cells so the mode exits the absorber
    # without re-reflecting; a plane wave has no conductor to extrude, so it is
    # kept out of ``port_faces`` (extrusion) here.
    port_faces = [str(t.Face) for t in tem_mod.find_tem_sources(sim)]
    port_faces += [str(p.Face) for p in spice_mod.find_spice_tem_ports(sim)]

    # Force every face-launching source (TEM, SPICE-TEM *and* plane wave) to PML,
    # via the single source of truth. ``force_pml_faces`` makes the grid padding
    # *and* the emitted boundary (below) both treat these faces as PML, so they
    # stay consistent with the drawn box and node arrays (built from the same
    # ``tem_port_faces`` list) -- a plane-wave face left at PEC would reflect its
    # own launch and, on a non-uniform grid, desync the node arrays.
    grid_params = domain_mod.domain_grid_params(
        dom, force_pml_faces=domain_mod.tem_port_faces(sim))
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
    # Grow the grid to include every source position and snapshot slice, so an
    # input outside the material bounds (or in the PML) still lands inside it.
    vox = voxelize_materials(
        materials, cell_size_m,
        spacing_lo_m=spacing_lo, spacing_hi_m=spacing_hi,
        pad_lo=pad_lo, pad_hi=pad_hi,
        extra_points_mm=source_points_mm(sim),
        extra_axis_offsets=snapshot_axis_offsets(sim),
        port_faces=port_faces,
        bg_eps=bg_eps, bg_mu=bg_mu, bg_pec=bg_pec,
        nodes_m=nodes_m, subpixel=subpixel,
        progress=progress,
    )
    grid = vox["grid"]
    Nx, Ny, Nz = grid["Nx"], grid["Ny"], grid["Nz"]
    dx, dy, dz = grid["dx"], grid["dy"], grid["dz"]
    origin_m = vox["origin_m"]

    # Sources: the user-defined point source (Session 6) and TEM ports (Session
    # 9), converted to the solver frame (the domain origin is baked into the
    # voxel arrays). With no point source and no TEM port, fall back to a centre
    # Gaussian pulse so a bare run still works; a TEM port is excitation enough,
    # so the fallback is skipped when one is present.
    from wavesim_gui import source as source_mod
    from wavesim_gui import spice_port as spice_mod
    from wavesim_gui import plane_wave as plane_mod

    tem_objs = tem_mod.find_tem_sources(sim)
    tem_sources = [tem_mod.tem_source_spec(t, origin_m) for t in tem_objs]

    # Boundary plane waves: launched from a (forced-PML) domain face; the runner
    # places the sheet from the face + the boundary's PML depth, so no per-source
    # geometry is needed here beyond the face/angle/directional flag.
    plane_waves = [plane_mod.plane_wave_spec(p, origin_m)
                   for p in plane_mod.find_plane_waves(sim)]

    # SPICE co-simulation ports (line + TEM); drop any that could not serialise
    # (e.g. a line port with no curve assigned). The TEM specs are kept paired
    # with their objects so the mode-mesh pass below can size each port's plane.
    spice_line_specs = [spice_mod.spice_line_port_spec(p, origin_m)
                        for p in spice_mod.find_spice_line_ports(sim)]
    spice_tem_objs = spice_mod.find_spice_tem_ports(sim)
    spice_tem_specs = [spice_mod.spice_tem_port_spec(p, origin_m)
                       for p in spice_tem_objs]
    spice_ports = [s for s in (spice_line_specs + spice_tem_specs) if s]

    # Connectivity-preserving mode mesh (Session B): for every TEM / SPICE-TEM
    # port whose PEC cross-section fragments at the coarse cell size, attach a
    # finely re-voxelised transverse plane so the runner solves the mode on a
    # grid where the conductor count is correct, then interpolates it back onto
    # the coarse grid to launch. Absent ⇒ the runner solves on the coarse slice.
    # When the Simulation's impedance-convergence study is on, ship a whole
    # refinement ladder instead (the runner walks it until Z0 converges).
    from wavesim_gui import commands as commands_mod

    convergence = commands_mod.mode_convergence_settings(sim)
    _attach_mode_meshes(
        vox["arrays"], dom,
        list(zip(tem_sources, tem_objs))
        + [(spec, obj) for spec, obj in zip(spice_tem_specs, spice_tem_objs)
           if spec],
        materials, cell_size_m, origin_m, bg_eps, bg_mu, bg_pec,
        convergence=convergence,
    )

    sources = source_mod.find_sources(sim)
    if sources:
        source = source_mod.source_spec(sources[0], origin_m)
    elif tem_sources or spice_ports or plane_waves:
        # A TEM source, a plane wave or a (driven) SPICE port is excitation
        # enough; skip the centre-Gaussian fallback.
        source = None
    else:
        source = {
            "component": "Ez",
            "x": (Nx // 2) * dx, "y": (Ny // 2) * dy, "z": (Nz // 2) * dz,
            "fmax": fmax,
            "amplitude": 1.0,
        }

    # Boundary: from the Domain's per-face settings when one exists (with TEM-port
    # faces already forced to PML in ``grid_params`` above, so their padding and
    # boundary condition agree), else the legacy auto heuristic (in-plane faces for
    # a thin domain, all six otherwise; no PEC walls).
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
    # the solver frame. With none defined this still yields the always-on energy
    # diagnostic, so a bare sim behaves as in earlier sessions.
    from wavesim_gui import monitors as monitors_mod

    monitors = monitors_mod.monitors_spec(sim, origin_m)

    spec = {
        # backend is stamped by job.write_job from settings (default 'auto',
        # which the runner resolves to the CUDA GPU when one is available).
        "steps": int(steps),
        "grid": grid,
        # Run provenance, echoed into summary.json by the runner alongside
        # backend/pml_faces: whether this run's dielectric boundaries were
        # smoothed. Records what actually ran, which the Simulation container
        # cannot answer later once the user flips the checkbox.
        "subpixel": subpixel,
        "boundary": {
            "d_pml": int(d_pml),
            "faces": pml_faces,
            "pec_faces": pec_faces,
        },
        "source": source,
        "tem_sources": tem_sources,
        "plane_waves": plane_waves,
        "spice_ports": spice_ports,
        "monitors": monitors,
    }
    return spec, vox["arrays"]
