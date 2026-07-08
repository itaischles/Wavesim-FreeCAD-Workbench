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


def _grid_extent(bbox, cell_mm, spacing_mm, pad_lo, pad_hi):
    """Per-axis ``(counts, origin_mm)`` for the given sizing.

    The inner region is the material bounds grown by ``spacing_mm`` on every
    side and rounded up to whole cells; ``pad_lo``/``pad_hi`` add the per-side
    PML cells outside that. The origin is the min corner of the *padded* grid.
    """
    exts = (bbox.XLength, bbox.YLength, bbox.ZLength)
    mins = (bbox.XMin, bbox.YMin, bbox.ZMin)
    counts = []
    origin = []
    for a in range(3):
        inner = max(1, int(math.ceil((exts[a] + 2.0 * spacing_mm) / cell_mm[a])))
        counts.append(inner + int(pad_lo[a]) + int(pad_hi[a]))
        origin.append(mins[a] - spacing_mm - int(pad_lo[a]) * cell_mm[a])
    return tuple(counts), tuple(origin)


def _sizing_for(sim, default_padding):
    """Resolve ``(spacing_m, pad_lo, pad_hi, domain)`` for *sim*.

    Uses the Domain object's spacing and per-face PML padding when one exists;
    otherwise falls back to the legacy uniform ``default_padding`` cells on every
    side with no air spacing (so a document without a domain runs as before).
    """
    from wavesim_gui import domain as domain_mod

    dom = domain_mod.find_domain(sim) if sim else None
    if dom is not None:
        p = domain_mod.domain_grid_params(dom)
        return p["spacing_m"], p["pad_lo"], p["pad_hi"], dom
    pad = (default_padding, default_padding, default_padding)
    return 0.0, pad, pad, None


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
    spacing_m, pad_lo, pad_hi, _dom = _sizing_for(sim, padding_cells)
    cell_mm = tuple(c * _MM_PER_M for c in cell_size_m)
    (Nx, Ny, Nz), _origin = _grid_extent(
        bbox, cell_mm, spacing_m * _MM_PER_M, pad_lo, pad_hi
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
    boundary-most material cross-section is copied outward across the air spacing
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


def voxelize_materials(materials, cell_size_m, spacing_m=0.0,
                       pad_lo=(8, 8, 8), pad_hi=(8, 8, 8),
                       extra_points_mm=(), extra_axis_offsets=(),
                       port_faces=(), bg_eps=1.0, bg_mu=1.0, bg_pec=False,
                       max_total_cells=4_000_000, progress=None):
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
    spacing_m : float
        Air gap (metres) added around the material bounds on every side, before
        any PML padding. From the Domain object's ``Spacing`` (0 with no domain).
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

    bbox = _expand_bbox_points(_combined_bbox(entries), extra_points_mm)
    bbox = _expand_bbox_axis(bbox, extra_axis_offsets)
    dx_mm, dy_mm, dz_mm = (c * _MM_PER_M for c in cell_size_m)
    cell_mm = (dx_mm, dy_mm, dz_mm)

    (Nx, Ny, Nz), (ox, oy, oz) = _grid_extent(
        bbox, cell_mm, spacing_m * _MM_PER_M, pad_lo, pad_hi
    )
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
    eps_arr = np.full(shape, float(bg_eps), dtype=np.float64)
    mu_arr = np.full(shape, float(bg_mu), dtype=np.float64)
    pec_mask = np.full(shape, bool(bg_pec), dtype=bool)

    Z_AXIS = FreeCAD.Vector(0.0, 0.0, 1.0)
    tol = min(dx_mm, dy_mm, dz_mm) * 1.0e-6
    # Chord tolerance for turning curved section edges into polygons: a quarter
    # of the smallest in-plane cell, so the polygon tracks curves to well below
    # cell resolution (never below the geometric tolerance).
    deflection = max(min(dx_mm, dy_mm) * 0.25, tol)

    # Cell-centre world coordinates (mm) along each axis, precomputed (numpy).
    xs = ox + (np.arange(Nx) + 0.5) * dx_mm
    ys = oy + (np.arange(Ny) + 0.5) * dy_mm
    zs = oz + (np.arange(Nz) + 0.5) * dz_mm

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
        plans.append((body_shape, eps, mu, pec, i_idx, j_idx, k_idx))
        total_layers += len(k_idx)

    done_layers = 0
    if progress is not None:
        progress(0, total_layers)
    for body_shape, eps, mu, pec, i_idx, j_idx, k_idx in plans:
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
                    eps_arr[gi, gj, k] = eps
                    mu_arr[gi, gj, k] = mu
                    # A dielectric body overrides a PEC background at its cells.
                    pec_mask[gi, gj, k] = False
            done_layers += 1
            if progress is not None and progress(done_layers, total_layers):
                raise VoxelizationCancelled()

    arrays = {
        "eps_x": eps_arr, "eps_y": eps_arr.copy(), "eps_z": eps_arr.copy(),
        "mu_x": mu_arr, "mu_y": mu_arr.copy(), "mu_z": mu_arr.copy(),
        "pec_mask": pec_mask,
    }
    # Extend each TEM-port cross-section through its spacing + PML cells so a
    # guided mode stays supported into the absorber (modifies arrays in place;
    # done before the counts below so they reflect the extruded geometry).
    _extrude_port_faces(arrays, port_faces, bg_eps=bg_eps, bg_mu=bg_mu,
                        bg_pec=bg_pec)
    return {
        "arrays": arrays,
        "grid": {
            "Nx": Nx, "Ny": Ny, "Nz": Nz,
            "dx": dx_mm / _MM_PER_M,
            "dy": dy_mm / _MM_PER_M,
            "dz": dz_mm / _MM_PER_M,
        },
        "origin_m": (ox / _MM_PER_M, oy / _MM_PER_M, oz / _MM_PER_M),
        "counts": {
            "dielectric_cells": int(np.count_nonzero(eps_arr != float(bg_eps))),
            "pec_cells": int(np.count_nonzero(pec_mask)),
        },
    }


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

    from wavesim_gui import tem_source as tem_mod

    spacing_m, pad_lo, pad_hi, _dom = _sizing_for(sim, 8)
    # TEM-port faces have their cross-section extruded through the spacing + PML
    # so the guided mode exits the absorber without re-reflecting back inside.
    from wavesim_gui import spice_port as spice_mod

    port_faces = [str(t.Face) for t in tem_mod.find_tem_sources(sim)]
    # SPICE TEM ports launch a guided mode too, so extrude their cross-sections
    # through the absorber as well.
    port_faces += [str(p.Face) for p in spice_mod.find_spice_tem_ports(sim)]
    # Background (empty-voxel) medium: the Domain's chosen background Material,
    # defaulting to vacuum when unset.
    bg_mat = domain_mod.background_material(dom)
    bg_eps = float(getattr(bg_mat, "Eps", 1.0)) if bg_mat is not None else 1.0
    bg_mu = float(getattr(bg_mat, "Mu", 1.0)) if bg_mat is not None else 1.0
    bg_pec = bool(getattr(bg_mat, "Pec", False)) if bg_mat is not None else False
    # Grow the grid to include every source position and snapshot slice, so an
    # input outside the material bounds (or in the PML) still lands inside it.
    vox = voxelize_materials(
        materials, cell_size_m, spacing_m=spacing_m, pad_lo=pad_lo, pad_hi=pad_hi,
        extra_points_mm=source_points_mm(sim),
        extra_axis_offsets=snapshot_axis_offsets(sim),
        port_faces=port_faces,
        bg_eps=bg_eps, bg_mu=bg_mu, bg_pec=bg_pec,
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

    tem_sources = [tem_mod.tem_source_spec(t, origin_m)
                   for t in tem_mod.find_tem_sources(sim)]

    # SPICE co-simulation ports (line + TEM); drop any that could not serialise
    # (e.g. a line port with no curve assigned).
    spice_ports = [
        s for s in (
            [spice_mod.spice_line_port_spec(p, origin_m)
             for p in spice_mod.find_spice_line_ports(sim)]
            + [spice_mod.spice_tem_port_spec(p, origin_m)
               for p in spice_mod.find_spice_tem_ports(sim)]
        ) if s
    ]

    sources = source_mod.find_sources(sim)
    if sources:
        source = source_mod.source_spec(sources[0], origin_m)
    elif tem_sources or spice_ports:
        # A TEM source or a (driven) SPICE port is excitation enough; skip the
        # centre-Gaussian fallback.
        source = None
    else:
        source = {
            "component": "Ez",
            "x": (Nx // 2) * dx, "y": (Ny // 2) * dy, "z": (Nz // 2) * dz,
            "fmax": fmax,
            "amplitude": 1.0,
        }

    # Boundary: from the Domain's per-face settings when one exists, else the
    # legacy auto heuristic (in-plane faces for a thin domain, all six otherwise;
    # no PEC walls).
    if dom is not None:
        from wavesim_gui import domain as domain_mod

        p = domain_mod.domain_grid_params(dom)
        pml_faces = p["pml_faces"]
        pec_faces = p["pec_faces"]
        d_pml = p["d_pml"]
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
        "boundary": {
            "d_pml": int(d_pml),
            "faces": pml_faces,
            "pec_faces": pec_faces,
        },
        "source": source,
        "tem_sources": tem_sources,
        "spice_ports": spice_ports,
        "monitors": monitors,
    }
    return spec, vox["arrays"]
