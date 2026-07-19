# -*- coding: utf-8 -*-
"""Automatic non-uniform grid builder -- the "snapper" (FreeCAD side).

When the Domain's ``UseNonuniformGrid`` is on, :func:`build_domain_nodes` places
grid lines *on* the material geometry's features and grades the spacing out to a
coarse interior, so small features get fine cells without refining the whole
domain. The result is the Domain's ``NodesX/Y/Z`` arrays (world mm); everything
downstream (voxeliser, runner, plots, 3D preview) already consumes those (Phase 2).

The mesh is built per axis, independently:

* **Snap (forced) lines.** Every material body contributes its bounding-box
  min/max planes on all three axes; every axis-aligned cylindrical face adds its
  transverse silhouette (centre +/- radius) on the two axes it spans and its own
  extent on its axis. Grid lines are forced exactly at these coordinates so cells
  conform to the geometry.
* **Graded fill.** Between consecutive forced lines the interval is tiled with
  cells no larger than the coarse target (the Domain's ``Dx/Dy/Dz``, now the
  *background* resolution); a small gap gets fine cells and the size grows toward
  the interior by at most ``MaxGradingRatio`` per step (solver guidance ~1.5-2x).
* **Material refinement.** Each dielectric body tightens the coarse target over
  the axis interval it spans to its own per-medium resolution
  ``c0 / (fmax * N_lambda * sqrt(eps_r * mu_r))`` (see
  :func:`collect_material_caps`): a higher relative permittivity / permeability
  means a shorter wavelength, so that band of the grid is meshed finer while the
  low-index void stays coarse. Being axis-separable, the refinement fills the
  body's projected slab on each axis; the body itself (the slabs' intersection)
  is fine on all three.
* **PML pads.** ``pad_lo``/``pad_hi`` uniform cells (coarse size) are appended
  outside the inner region for the absorber, matching ``domain_grid_params``.

A global guard caps the grid at :data:`_MAX_TOTAL_CELLS`; if a build exceeds it
the whole mesh is coarsened uniformly and rebuilt.

Pure numpy-free FreeCAD geometry + Python math (no Qt), so it stays importable in
console mode; only ``execute`` (FreeCAD side) calls it.

Units: FreeCAD geometry is millimetres throughout here; the metre conversion for
the solver happens later in ``node_coords_m`` / the voxeliser.
"""

import math

import FreeCAD

_MM_PER_M = 1000.0

# Global cell-count guard, mirroring the voxeliser's ``max_total_cells``. A build
# over this is coarsened and retried rather than handed to the voxeliser (which
# would reject it).
_MAX_TOTAL_CELLS = 10_000_000


# --------------------------------------------------------------------------- #
# Snap-coordinate collection
# --------------------------------------------------------------------------- #

def _cyl_axis_index(axis_dir, tol=1.0e-6):
    """Return 0/1/2 if *axis_dir* is ~parallel to the x/y/z axis, else ``None``.

    Only axis-aligned cylinders get feature snapping; a tilted cylinder's silhouette
    is not axis-separable, so it falls back to its body bounding box.
    """
    comps = (abs(axis_dir.x), abs(axis_dir.y), abs(axis_dir.z))
    for i, c in enumerate(comps):
        if c > 1.0 - tol and comps[(i + 1) % 3] < tol and comps[(i + 2) % 3] < tol:
            return i
    return None


def _add_cylinder_snaps(shape, axes):
    """Append every axis-aligned cylindrical face's snap lines to *axes* (mm).

    A z-axis cylinder contributes ``xc +/- r`` on x, ``yc +/- r`` on y and its z
    extent on z, so a round conductor gets grid lines on its tangent planes and
    end caps. Non-cylindrical or tilted faces are ignored (the body bbox covers
    them).
    """
    try:
        import Part
    except Exception:
        return
    for face in getattr(shape, "Faces", []) or []:
        surf = getattr(face, "Surface", None)
        if not isinstance(surf, Part.Cylinder):
            continue
        ai = _cyl_axis_index(surf.Axis)
        if ai is None:
            continue
        centre = (surf.Center.x, surf.Center.y, surf.Center.z)
        r = float(surf.Radius)
        fb = face.BoundBox
        axial = ((fb.XMin, fb.XMax), (fb.YMin, fb.YMax), (fb.ZMin, fb.ZMax))[ai]
        for t in range(3):
            if t == ai:
                axes[t].extend(axial)
            else:
                axes[t].extend((centre[t] - r, centre[t] + r))


def collect_axis_snaps(materials):
    """Per-axis forced grid-line coordinates (world mm) from material geometry.

    Returns ``(xs, ys, zs)`` lists (unsorted, possibly with duplicates -- the
    per-axis builder dedupes with a tolerance). Every solid body contributes its
    bounding-box faces on all three axes; axis-aligned cylindrical faces add their
    silhouettes (see :func:`_add_cylinder_snaps`).
    """
    from wavesim_gui import voxelize as vox

    axes = ([], [], [])
    for shape, _eps, _mu, _pec in vox._gather(materials):
        bb = shape.BoundBox
        axes[0].extend((bb.XMin, bb.XMax))
        axes[1].extend((bb.YMin, bb.YMax))
        axes[2].extend((bb.ZMin, bb.ZMax))
        _add_cylinder_snaps(shape, axes)
    return axes


def collect_material_caps(sim, domain, materials):
    """Per-axis material cell-size caps ``(lo_mm, hi_mm, target_mm)``.

    Each dielectric body imposes, over the interval it spans on an axis, a maximum
    cell size equal to its own per-medium resolution
    ``c0 / (fmax * N_lambda * sqrt(eps_r * mu_r))`` (see
    :func:`wavesim_gui.domain.wavelength_cell_size_m`). A higher-index body has a
    shorter wavelength and so a smaller target, refining that band of the grid;
    the void keeps the coarse target. The body's *bounding box* on each axis is
    used, so the refinement fills the axis-projected slab of the body -- the best
    a rectilinear (axis-separable) grid can do, and the 3D intersection of the
    three slabs (the body itself) ends up fine on all axes.

    PEC bodies are skipped (no meaningful wavelength). Returns three empty lists
    when the max frequency is unset, so the snapper falls back to the plain coarse
    target.
    """
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import voxelize as vox

    caps = ([], [], [])
    for shape, eps, mu, pec in vox._gather(materials):
        if pec:
            continue
        target_m = domain_mod.wavelength_cell_size_m(sim, eps, mu, domain)
        if target_m is None:
            return ([], [], [])  # no max frequency -> no material sizing
        target_mm = target_m * _MM_PER_M
        bb = shape.BoundBox
        caps[0].append((bb.XMin, bb.XMax, target_mm))
        caps[1].append((bb.YMin, bb.YMax, target_mm))
        caps[2].append((bb.ZMin, bb.ZMax, target_mm))
    return caps


def _gap_coarse(a, b, coarse, caps):
    """Coarse cell target (mm) for the interval ``[a, b]`` given material *caps*.

    The axis *coarse* (void target), tightened to the smallest material target of
    any cap interval covering the gap's midpoint. Because material bounding-box
    faces are forced grid lines, each gap lies wholly inside or outside every cap
    interval, so the midpoint test classifies the whole gap.
    """
    mid = 0.5 * (a + b)
    target = coarse
    for lo, hi, t in caps:
        if lo <= mid <= hi and t < target:
            target = t
    return target


# --------------------------------------------------------------------------- #
# Per-axis graded meshing
# --------------------------------------------------------------------------- #

def _forced_lines(snaps, lo, hi, coarse, min_cell=0.0):
    """Sorted, deduped forced grid lines spanning ``[lo, hi]`` (mm).

    Snap coordinates outside the inner region are dropped; the rest are clamped
    into ``[lo, hi]`` and merged when closer than a small tolerance (so two nearly
    coincident feature planes don't create a zero-width cell). When *min_cell* is
    positive, lines closer together than it are also merged, so two nearby
    features cannot force a sub-minimum cell. The result always starts at *lo* and
    ends at *hi* and is strictly increasing.
    """
    tol = max(coarse * 1.0e-3, 1.0e-6, float(min_cell))
    merged = [lo]
    for v in sorted(snaps):
        if v < lo - tol or v > hi + tol:
            continue
        v = min(max(v, lo), hi)
        if v - merged[-1] > tol:
            merged.append(v)
    if hi - merged[-1] > tol:
        merged.append(hi)
    else:
        merged[-1] = hi
    return merged


def _graded_widths(w, hL, hR, H, r):
    """Cell widths (mm) tiling ``[0, w]``, summing exactly to *w*.

    Cells start ~``hL`` on the left and ~``hR`` on the right, growing by at most
    factor *r* toward a coarse cap *H* in the middle. The smaller pending cell is
    always laid next so the two sides stay balanced; the (sub-cell) leftover is
    removed by scaling all widths uniformly, which preserves the grading ratios.
    Always returns at least one positive width.
    """
    if w <= 0.0:
        return []
    r = max(float(r), 1.0 + 1.0e-9)
    # A single cell can never exceed the interval; a zero request means "coarse".
    hL = min(hL if hL > 0.0 else w, w)
    hR = min(hR if hR > 0.0 else w, w)
    H = min(max(H, hL, hR), w)

    left, right = [], []
    xl = xr = 0.0
    sl, sr = hL, hR
    while True:
        remaining = w - xl - xr
        if remaining <= 0.0:
            break
        if sl <= sr:
            if sl >= remaining:
                break
            left.append(sl)
            xl += sl
            sl = min(sl * r, H)
        else:
            if sr >= remaining:
                break
            right.append(sr)
            xr += sr
            sr = min(sr * r, H)

    widths = left + right[::-1]
    if not widths:
        return [w]
    scale = w / math.fsum(widths)
    return [x * scale for x in widths]


def build_axis_nodes(snaps, lo, hi, coarse, ratio, pad_lo, pad_hi, min_cell=0.0,
                     caps=()):
    """Graded node coordinates (mm) for one axis, PML pad cells included.

    Parameters
    ----------
    snaps : iterable of float
        Forced interior grid-line coordinates (world mm) for this axis.
    lo, hi : float
        Bounds of the inner (air-padded) region on this axis, world mm.
    coarse : float
        Target interior (void) cell size (mm) -- the background-medium
        resolution.
    ratio : float
        Max size ratio between adjacent cells the graded fill may use.
    pad_lo, pad_hi : int
        Uniform PML cells (width *coarse*) appended below *lo* / above *hi*.
    min_cell : float
        Smallest cell the fill may use (mm); 0 disables the limit. Nearby forced
        lines are merged and the fine feature cells are clamped to it, so
        snapping cannot produce an extremely fine mesh.
    caps : iterable of (lo_mm, hi_mm, target_mm)
        Material cell-size caps (:func:`collect_material_caps`): over each
        interval the coarse target is tightened to *target_mm* so a high-index
        body's band gets finer cells. Empty ⇒ uniform coarse target everywhere.

    Returns a strictly-increasing list of node coordinates. The inner region is
    tiled so every gap between forced lines is resolved with cells no larger than
    the (material-tightened) coarse target, fine next to small features and
    grading out toward the void.
    """
    coarse = max(float(coarse), 1.0e-9)
    min_cell = min(max(float(min_cell), 0.0), coarse)
    if hi - lo < coarse:
        hi = lo + coarse  # degenerate/thin axis: at least one inner cell

    forced = _forced_lines(snaps, lo, hi, coarse, min_cell)
    gaps = [b - a for a, b in zip(forced[:-1], forced[1:])]

    # Per-gap coarse target: the void size, tightened where a material body covers
    # the gap (its shorter wavelength wants smaller cells), then floored by the
    # min-cell limit so material refinement can't undercut it either.
    gap_coarse = [max(_gap_coarse(a, b, coarse, caps), min_cell)
                  for a, b in zip(forced[:-1], forced[1:])]

    # Intrinsic desired size per interval (small gaps want small cells) and, from
    # that, the desired cell size at each forced line: the finer of its neighbours
    # so a line bounding a small feature carries fine cells into the void. The
    # min-cell floor keeps a small gap from spawning sub-minimum cells (a gap that
    # is itself below the floor stays a single cell).
    intrinsic = [min(gc, g) for gc, g in zip(gap_coarse, gaps)]
    if min_cell > 0.0:
        intrinsic = [s if g < min_cell else max(s, min_cell)
                     for s, g in zip(intrinsic, gaps)]
    n = len(forced)
    end_size = [0.0] * n
    for i in range(n):
        left = intrinsic[i - 1] if i > 0 else intrinsic[0]
        right = intrinsic[i] if i < len(intrinsic) else intrinsic[-1]
        end_size[i] = min(left, right)

    nodes = [forced[0]]
    for k, g in enumerate(gaps):
        pos = nodes[-1]
        for cw in _graded_widths(g, end_size[k], end_size[k + 1],
                                 gap_coarse[k], ratio):
            pos += cw
            nodes.append(pos)
        nodes[-1] = forced[k + 1]  # land exactly on the forced line

    # PML pads: uniform coarse cells outside the inner region.
    lo_pad = [nodes[0] - (pad_lo - i) * coarse for i in range(int(pad_lo))]
    hi_pad = [nodes[-1] + (i + 1) * coarse for i in range(int(pad_hi))]
    return lo_pad + nodes + hi_pad


# --------------------------------------------------------------------------- #
# Domain-level entry point
# --------------------------------------------------------------------------- #

def build_domain_nodes(sim, domain, force_pml_faces=()):
    """Snapped, graded ``(NodesX, NodesY, NodesZ)`` (world mm) for *domain*.

    Uses the material geometry bounds (grown for sources/monitors, via
    ``combined_bbox_mm``) as the inner region, the Domain's ``Dx/Dy/Dz`` as the
    coarse (background) interior target, its ``MaxGradingRatio`` as the grading
    bound and the per-face PML padding from ``domain_grid_params``. *force_pml_faces*
    (the TEM-port launch faces) is forwarded so their PML pad is present in the
    node arrays even if the face is set to PEC, matching the run's boundary. Each material
    body additionally refines its own band down to its per-medium resolution (see
    :func:`collect_material_caps`), so higher-index regions are meshed finer than
    the void. Returns ``None`` when there is no geometry to bound (the caller
    falls back to a uniform grid).

    If the grid exceeds :data:`_MAX_TOTAL_CELLS`, the coarse target is scaled up
    and the whole mesh rebuilt until it fits (bounded number of attempts).
    """
    from wavesim_gui import materials as materials_mod
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import voxelize as vox

    materials = materials_mod.find_materials(sim) if sim else []
    bbox = vox.combined_bbox_mm(sim, materials) if sim else None
    if bbox is None:
        return None

    params = domain_mod.domain_grid_params(domain, force_pml_faces=force_pml_faces)
    sp_lo_mm = tuple(s * _MM_PER_M for s in params["spacing_lo"])
    sp_hi_mm = tuple(s * _MM_PER_M for s in params["spacing_hi"])
    pad_lo, pad_hi = params["pad_lo"], params["pad_hi"]
    coarse_mm = tuple(c * _MM_PER_M for c in domain_mod.cell_sizes_m(domain))
    ratio = max(float(getattr(domain, "MaxGradingRatio", 1.5)), 1.0 + 1.0e-6)
    min_cell_mm = domain_mod.min_cell_size_m(domain) * _MM_PER_M

    los = (bbox.XMin - sp_lo_mm[0], bbox.YMin - sp_lo_mm[1], bbox.ZMin - sp_lo_mm[2])
    his = (bbox.XMax + sp_hi_mm[0], bbox.YMax + sp_hi_mm[1], bbox.ZMax + sp_hi_mm[2])
    snaps = collect_axis_snaps(materials)
    # Per-axis material cell-size caps: higher-index bodies refine their band
    # below the coarse (background) target. Scaled alongside ``coarse`` in the
    # cell-count guard below, so a grid that must be coarsened to fit coarsens
    # material regions and void together.
    caps = collect_material_caps(sim, domain, materials)

    nodes = None
    scale = 1.0
    for _attempt in range(12):
        scaled_caps = tuple(
            [(lo, hi, t * scale) for lo, hi, t in caps[a]] for a in range(3)
        )
        nodes = tuple(
            build_axis_nodes(
                snaps[a], los[a], his[a], coarse_mm[a] * scale, ratio,
                pad_lo[a], pad_hi[a], min_cell_mm, scaled_caps[a],
            )
            for a in range(3)
        )
        total = (len(nodes[0]) - 1) * (len(nodes[1]) - 1) * (len(nodes[2]) - 1)
        if total <= _MAX_TOTAL_CELLS:
            break
        scale *= 1.5
    return nodes
