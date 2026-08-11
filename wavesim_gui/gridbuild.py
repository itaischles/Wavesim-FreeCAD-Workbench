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
  extent on its axis; every axis-normal *planar* face adds its own plane. Grid
  lines are forced exactly at these coordinates so cells conform to the geometry.
  The planar and cylindrical faces are what make **interior** features visible:
  a bounding box only describes a body's outer extent, so a slot, pocket, step or
  aperture cut into it -- geometry that never touches the box -- is snapped by its
  own faces or not at all.
* **Circle centre lines.** A cylindrical face that closes a full turn -- a rod, a
  bore, anything whose cross-section really is a *circle*, as opposed to the arc
  of a fillet or blend -- also forces a line through its **centre**, so a node
  lands on the axis where the port, probe or voltage path is, and a round feature
  narrower than one cell gets an interior instead of being a single lump of
  metal. It is added only where the graded fill below can absorb it for free
  (:func:`_insert_centre_lines`): splitting a gap that spans an *odd* number of
  coarse cells would cost one of them, and that circle keeps the mesh it had.
* **Graded fill.** Between consecutive forced lines the interval is tiled with
  cells no larger than the coarse target (the Domain's ``Dx/Dy/Dz``, now the
  *background* resolution); a small gap gets fine cells and the size grows toward
  the interior by at most ``MaxGradingRatio`` per step (solver guidance ~1.5-2x).
* **Grazing-surface refinement.** Where a curved face runs *tangent* to an axis's
  node planes -- a sphere's pole, a cylinder's silhouette line, the crown of a
  fillet -- the surface departs from that plane only as ``kappa * h_t^2 / 2`` per
  transverse cell ``h_t``, so a whole disc of it can sit inside a single cell and
  the curvature becomes invisible to the mesh. :func:`collect_curvature_sizes`
  asks for a fine cell *at* such a line, and the graded fill spreads it back out
  to the bulk. Note the size scales **inversely with curvature**: a tightly curved
  body leaves its own tangent plane within a cell and needs nothing, a gently
  curved one is the pathological case. Off by default (Domain's
  ``CurvatureRefinement`` = 1).
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

# Parameter samples per direction when hunting a face's axis-tangent points, and
# the ``|n . e_axis|`` above which a sample counts as grazing (~10 degrees). The
# sweep only has to *bracket* each tangent point; :func:`_refine_graze` then
# locates it, so this stays coarse and cheap.
_CURVATURE_SAMPLES = 24
_GRAZE_COS = 0.985

# Halvings used to locate the tangent point once the sweep has bracketed it.
# Each round halves the parameter step, so 16 takes a 0.27 rad sample step down
# to 4e-6 rad -- on a 20 mm sphere that is 1e-10 mm of coordinate error, which is
# the point: the coordinate has to agree with ``_exact_bbox``' line to far better
# than the merge tolerance, not merely to within a cell.
_GRAZE_REFINE_ROUNDS = 16

# Angular span (rad) above which a cylindrical face counts as a whole circle and
# so earns a centre line -- see :func:`_is_full_circle`. A hair under a full turn,
# because OCC reports the seam range as exactly 2*pi but a trimmed-then-healed
# face can come back a rounding short.
_FULL_TURN_MIN = 2.0 * math.pi - 1.0e-6


# --------------------------------------------------------------------------- #
# Snap-coordinate collection
# --------------------------------------------------------------------------- #

def _axis_index(axis_dir, tol=1.0e-6):
    """Return 0/1/2 if *axis_dir* is ~parallel to the x/y/z axis, else ``None``.

    Used for both a cylinder's axis and a plane's normal. Only axis-aligned
    features get snapping: a tilted cylinder's silhouette (or a tilted plane) is
    not axis-separable, so it falls back to its body bounding box.
    """
    comps = (abs(axis_dir.x), abs(axis_dir.y), abs(axis_dir.z))
    for i, c in enumerate(comps):
        if c > 1.0 - tol and comps[(i + 1) % 3] < tol and comps[(i + 2) % 3] < tol:
            return i
    return None


def _exact_bbox(shape):
    """*shape*'s bounding box, from the exact geometry rather than a tessellation.

    ``Shape.BoundBox`` is derived from the shape's triangulation, so a curved
    body's box can be *under*-sized: a cylinder faceted with vertices on the x
    axis reports XMin/XMax exactly but YMin/YMax short by ``r*(1 - cos(pi/n))``
    -- 0.066 mm on a 9 mm radius at n=26. That lands a fraction of a cell away
    from :func:`_add_cylinder_snaps`' exact ``centre +/- r``, and since
    :func:`_forced_lines` merges near-coincident lines *keeping the lower one*,
    the low side of a round body then snaps to the true silhouette while the high
    side snaps to the faceted one. A symmetric body gets an asymmetric mesh, and
    the symmetry break shows up in the fields (a coax rings in its m=1 mode).

    ``optimalBoundingBox(useTriangulation=False)`` asks OCC for the box of the
    real surfaces instead. Falls back to ``BoundBox`` if it is unavailable.
    """
    try:
        return shape.optimalBoundingBox(False, False)
    except Exception:
        return shape.BoundBox


def _is_full_circle(face):
    """True when *face* wraps a whole turn, so its cross-section is a circle.

    A cylindrical surface is also what OCC hands back for a **fillet, a blend or
    a boolean's leftover arc**, and those have no circle to speak of: their
    centre is a construction point that need not lie on -- or even near -- the
    body. Forcing a grid line through it would subdivide a gap (and, for a small
    fillet, pull fine cells in with it) to resolve an interface that is not
    there. The full-turn test is what separates "this face *is* a circle" from
    "this face was cut from one", and it is deliberately the conservative side of
    the trade: a hole that a boolean happened to split into two half-cylinders
    loses its centre line and simply meshes as it did before.
    """
    try:
        u0, u1, _v0, _v1 = face.ParameterRange
    except Exception:
        return False
    return (u1 - u0) >= _FULL_TURN_MIN


def _add_cylinder_snaps(shape, axes, centres=None):
    """Append every axis-aligned cylindrical face's snap lines to *axes* (mm).

    A z-axis cylinder contributes ``xc +/- r`` on x, ``yc +/- r`` on y and its z
    extent on z, so a round conductor gets grid lines on its tangent planes and
    end caps. Non-cylindrical or tilted faces are ignored (the body bbox covers
    them).

    When *centres* is given, a face that closes the full turn
    (:func:`_is_full_circle`) also **proposes** its centre coordinate on those
    two transverse axes -- a grid line through the circle's axis. It is only a
    proposal: whether it becomes a line is decided by
    :func:`_insert_centre_lines`, once the rest of the forced set is known.
    Everything appended to *axes* here is unconditional, as it always was.
    """
    try:
        import Part
    except Exception:
        return
    for face in getattr(shape, "Faces", []) or []:
        surf = getattr(face, "Surface", None)
        if not isinstance(surf, Part.Cylinder):
            continue
        ai = _axis_index(surf.Axis)
        if ai is None:
            continue
        centre = (surf.Center.x, surf.Center.y, surf.Center.z)
        r = float(surf.Radius)
        circle = centres is not None and _is_full_circle(face)
        # Plain BoundBox is safe for the *axial* extent (unlike the transverse
        # one -- see :func:`_exact_bbox`): the end circles' facet vertices sit
        # exactly on the cap planes, so faceting cannot shorten this axis.
        fb = face.BoundBox
        axial = ((fb.XMin, fb.XMax), (fb.YMin, fb.YMax), (fb.ZMin, fb.ZMax))[ai]
        for t in range(3):
            if t == ai:
                axes[t].extend(axial)
                continue
            axes[t].extend((centre[t] - r, centre[t] + r))
            if circle:
                centres[t].append(centre[t])


def _add_planar_snaps(shape, axes):
    """Append every axis-normal planar face's own plane to *axes* (mm).

    This is what makes a body's **interior** planar features visible to the
    snapper. ``BoundBox`` only describes the outer extent, so a slot, pocket,
    step or aperture cut into a body -- geometry that never touches the box --
    used to force no grid line at all, and its walls landed wherever the uniform
    fill happened to put them. Worst case that is dead centre of a cell, i.e.
    half a cell of staircase error on a material interface, and (for an aperture)
    a width quantised to the nearest whole cell. A face normal to an axis now
    forces a line at its own plane, so a cell boundary lands exactly on the
    interface.

    Note the asymmetry this removes: :func:`_add_cylinder_snaps` already walks
    *every* face with no inner/outer distinction, so a round hole through a plate
    was snapped while a rectangular slot through the same plate was not.

    Only faces whose normal is parallel to an axis qualify -- a tilted plane is
    not axis-separable, exactly as for a tilted cylinder. Each axis is covered by
    the faces normal to it, so a face contributes one coordinate rather than
    three (its in-plane extent is some other face's normal). The coordinate comes
    from the analytic ``Surface.Position`` rather than the face's ``BoundBox``,
    for the tessellation reason spelt out in :func:`_exact_bbox`.

    Deliberately **no size filter**: a small aperture is precisely the feature
    worth snapping to, so dropping small faces would defeat the purpose. Nearly
    coincident lines are merged (symmetrically) by :func:`_forced_lines`, and the
    Domain's ``MinCellSize`` is what bounds the timestep a fine feature costs.
    """
    try:
        import Part
    except Exception:
        return
    for face in getattr(shape, "Faces", []) or []:
        surf = getattr(face, "Surface", None)
        if not isinstance(surf, Part.Plane):
            continue
        ai = _axis_index(surf.Axis)
        if ai is None:
            continue
        pos = surf.Position  # a point on the plane, by definition
        axes[ai].append((pos.x, pos.y, pos.z)[ai])


def collect_axis_snaps(materials, centres=None):
    """Per-axis forced grid-line coordinates (world mm) from material geometry.

    Returns ``(xs, ys, zs)`` lists (unsorted, with duplicates -- deduping is
    :func:`_forced_lines`' job, and is deliberately left there so *all* merging
    happens in one place under one symmetric rule). Every solid body contributes
    its bounding-box faces on all three axes; axis-aligned cylindrical faces add
    their silhouettes (:func:`_add_cylinder_snaps`) and axis-normal planar faces
    their own planes (:func:`_add_planar_snaps`) -- the latter two are what reach
    features *inside* a body. The box comes from :func:`_exact_bbox`, so a curved
    body's box agrees with its own analytic silhouette instead of landing a
    sliver away from it.

    *centres*, when given, is a per-axis triple of lists that collects each full
    **circle's** centre coordinate as a *candidate* line
    (:func:`_insert_centre_lines` decides). It is a separate channel rather than
    more snaps because the two are not the same kind of thing: a snap is an
    interface the mesh must land on, while a centre line is wanted only where it
    is free, and that is not knowable per body.
    """
    from wavesim_gui import voxelize as vox

    axes = ([], [], [])
    for shape, _eps, _mu, _pec, _sigma, _body in vox._gather(materials):
        bb = _exact_bbox(shape)
        axes[0].extend((bb.XMin, bb.XMax))
        axes[1].extend((bb.YMin, bb.YMax))
        axes[2].extend((bb.ZMin, bb.ZMax))
        _add_cylinder_snaps(shape, axes, centres)
        _add_planar_snaps(shape, axes)
    return axes


def _sample_params(lo, hi, n):
    """*n* samples spanning ``[lo, hi]``, both endpoints included."""
    if hi - lo <= 0.0:
        return [lo]
    return [lo + (hi - lo) * i / (n - 1.0) for i in range(n)]


def _graze_at(face, axis, u, v):
    """``|n . e_axis|`` at ``(u, v)``, or -1 where the surface cannot be evaluated."""
    try:
        normal = face.normalAt(u, v)
    except Exception:
        return -1.0
    return abs((normal.x, normal.y, normal.z)[axis])


def _refine_graze(face, axis, u, v, du, dv, rng):
    """Walk ``(u, v)`` onto the true tangent point by shrinking local search.

    The sweep in :func:`_add_curvature_requests` can only report the best sample
    it *took*, and the samples do not land on the tangent point -- worse, they
    miss it by different amounts on opposite sides of a body. On a sphere sampled
    over ``u`` in ``[0, 2*pi]``, ``u = 0`` is a sample and ``u = pi`` is not, so
    the ``+x`` pole came out exact while ``-x`` came out 0.19 mm short. Any
    tolerance downstream then treats the two sides differently: the near-side
    request attaches to its bounding-box line, the far-side one lands far enough
    away to spawn its own line or (on a finer grid, where the tolerances scale
    with the cell) to be dropped entirely. **One side of the body gets refined
    and the other does not** -- which is exactly how this was reported.

    Maximising ``|n . e_axis|`` rather than the coordinate itself is what keeps
    this valid for a concave tangency (a torus throat, a fillet's inner crown),
    where the tangent point is a *minimum* of the coordinate rather than a
    maximum, and no convexity assumption is available to tell the two apart.
    """
    u0, u1, v0, v1 = rng
    best = _graze_at(face, axis, u, v)
    for _ in range(_GRAZE_REFINE_ROUNDS):
        du *= 0.5
        dv *= 0.5
        cu, cv = u, v
        for su in (-1, 0, 1):
            for sv in (-1, 0, 1):
                if su == 0 and sv == 0:
                    continue
                uu = min(max(cu + su * du, u0), u1)
                vv = min(max(cv + sv * dv, v0), v1)
                graze = _graze_at(face, axis, uu, vv)
                if graze > best:
                    best, u, v = graze, uu, vv
    return u, v


def _curvature_at(face, u, v, du, dv):
    """Largest principal curvature at ``(u, v)``, backing off if it is undefined.

    A sphere's uv parametrisation is **singular at its poles**, and OCC raises
    "curvature not defined" there -- at precisely the point this whole pass is
    hunting for. Evaluating a hair off the pole gives the same answer on any
    smooth surface (curvature is continuous even where the parametrisation is
    not), so a failure backs off along each parameter rather than discarding the
    tangent point. Discarding it is what put the z request 0.047 mm off both
    sphere poles.

    Returns 0.0 when no nearby evaluation succeeds, or on a locally flat patch --
    either way there is no sag to resolve and the caller drops the request.
    """
    for su, sv in ((0, 0), (0, 1), (0, -1), (1, 0), (-1, 0)):
        try:
            k1, k2 = face.curvatureAt(u + su * du * 1.0e-3, v + sv * dv * 1.0e-3)
        except Exception:
            continue
        kappa = max(abs(k1), abs(k2))
        if kappa > 0.0:
            return kappa
    return 0.0


def _add_curvature_requests(shape, coarse_mm, factor, min_cell, reqs):
    """Append *shape*'s grazing-surface cell-size requests to *reqs* (mm).

    A face **grazes** an axis where its normal turns parallel to that axis. There
    the surface is tangent to the axis's node planes, and over a transverse cell
    ``h_t`` it departs from the tangent plane by only the sag
    ``kappa * h_t^2 / 2``. Unless the cells along the axis are that small, the
    whole tangent disc -- radius ``sqrt(2 h_axis / kappa)``, which can be several
    cells -- collapses onto one staircase step and the mesh sees a flat plateau
    instead of a curve. Two 20 mm spheres facing each other across a 3 mm gap on a
    1.5 mm grid are the worst case: the sag over one cell is 0.06 mm, so a 7.7 mm
    disc of each pole lives inside a single cell.

    The requested size is therefore ``kappa * h_t^2 / 2``, using the **smaller**
    of the two transverse coarse sizes (the binding direction), and it grows with
    curvature: a tightly curved body leaves its tangent plane inside one cell and
    asks for nothing, while a gently curved one -- the "low curvature" case -- is
    the one that needs the fine cells. Since that ratio is ``2/(kappa*h_t)`` and
    so unbounded for any well-resolved round body, *factor* caps it: the request
    never goes below ``coarse/factor``. Without that cap this rule would refine
    every cylindrical conductor in every existing model by an order of magnitude.

    Planar patches (``kappa == 0``) are skipped outright: a plane normal to an
    axis grazes it everywhere, but it has no sag, so refining there would buy
    nothing and the *factor* floor alone would have refined it.

    One request per (axis, normal sign) per face -- the sample whose normal is
    most nearly axis-parallel -- so a sphere contributes its two poles on each
    axis rather than a smear of near-tangent lines.
    """
    try:
        import Part
    except Exception:
        return
    for face in getattr(shape, "Faces", []) or []:
        # A plane grazes an axis over its whole area but has no sag, so every one
        # of its samples would be discarded. Skipping it up front is what keeps
        # this pass off the clock for the many models that are all boxes and
        # slabs (an all-planar document costs nothing measurable).
        if isinstance(getattr(face, "Surface", None), Part.Plane):
            continue
        try:
            u0, u1, v0, v1 = face.ParameterRange
        except Exception:
            continue
        us = _sample_params(u0, u1, _CURVATURE_SAMPLES)
        vs = _sample_params(v0, v1, _CURVATURE_SAMPLES)
        du = (u1 - u0) / max(len(us) - 1, 1)
        dv = (v1 - v0) / max(len(vs) - 1, 1)
        # The sweep only brackets each tangent point, so it asks for nothing but
        # the normal. Curvature is looked up once per bracket afterwards, via
        # ``_curvature_at`` -- reading it here instead would discard the sample
        # sitting exactly on a parametrisation pole, which is the very sample
        # worth keeping.
        best = {}  # (axis, sign) -> (|n_axis|, u, v)
        for u in us:
            for v in vs:
                try:
                    if not face.isPartOfDomain(u, v):
                        continue  # sample fell in a hole of a trimmed face
                    normal = face.normalAt(u, v)
                except Exception:
                    continue
                nc = (normal.x, normal.y, normal.z)
                for a in range(3):
                    graze = abs(nc[a])
                    if graze < _GRAZE_COS:
                        continue
                    key = (a, 1 if nc[a] > 0.0 else -1)
                    if key not in best or graze > best[key][0]:
                        best[key] = (graze, u, v)
        for (a, _sign), (_graze, u, v) in best.items():
            u, v = _refine_graze(face, a, u, v, du, dv, (u0, u1, v0, v1))
            # The larger principal curvature is what sets the plateau: a cylinder
            # grazing on its side is flat along its axis (kappa2 = 0) but curved
            # across it, and it is the curved direction that decides how far the
            # staircase step runs.
            kappa = _curvature_at(face, u, v, du, dv)
            if kappa <= 0.0:
                continue
            try:
                point = face.valueAt(u, v)
            except Exception:
                continue
            coord = (point.x, point.y, point.z)[a]
            h_t = min(coarse_mm[t] for t in range(3) if t != a)
            size = max(kappa * h_t * h_t * 0.5, coarse_mm[a] / factor, min_cell)
            if size < coarse_mm[a]:
                reqs[a].append((coord, size))


def collect_curvature_sizes(materials, snaps, coarse_mm, factor, min_cell=0.0):
    """Per-axis grazing-surface size requests ``(coord_mm, size_mm)``.

    Walks every material body's faces (:func:`_add_curvature_requests`) and, as a
    side effect, appends to *snaps* the coordinate of any request that has no
    forced line near it yet, so an **interior** tangency -- a torus throat, a
    concave fillet, anything that never touches a bounding box -- still gets a
    line to be fine at. ``factor <= 1`` disables the whole pass.

    The "near" test is what keeps the sampling error harmless. A convex body's
    grazing point *is* its bounding-box extreme, and :func:`_exact_bbox` already
    put an exact line there; adding this pass's approximate coordinate as a second
    snap would let :func:`_forced_lines` mean-merge the two and drag the exact
    line off the geometry (or, past the merge tolerance, leave a sliver cell).
    Requests within a quarter cell of an existing snap therefore add no line and
    simply attach to the one that is already there -- which is the only reason the
    sampling above can afford to be coarse.
    """
    from wavesim_gui import voxelize as vox

    reqs = ([], [], [])
    if factor <= 1.0:
        return reqs
    for shape, _eps, _mu, _pec, _sigma, _body in vox._gather(materials):
        _add_curvature_requests(shape, coarse_mm, factor, min_cell, reqs)
    for a in range(3):
        near = 0.25 * coarse_mm[a]
        for coord, _size in reqs[a]:
            if all(abs(coord - s) > near for s in snaps[a]):
                snaps[a].append(coord)
    return reqs


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
    for shape, eps, mu, pec, _sigma, _body in vox._gather(materials):
        if pec:
            continue
        target_m = domain_mod.wavelength_cell_size_m(sim, eps, mu, domain)
        if target_m is None:
            return ([], [], [])  # no max frequency -> no material sizing
        target_mm = target_m * _MM_PER_M
        # Same box as :func:`collect_axis_snaps`, so every cap edge really is a
        # forced grid line -- the invariant :func:`_gap_coarse`'s midpoint test
        # relies on.
        bb = _exact_bbox(shape)
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

def _cluster_means(values, tol):
    """Single-linkage clusters of sorted *values* closer than *tol*, each meaned.

    The one merge rule in this module, shared by :func:`_forced_lines` and
    :func:`_insert_centre_lines` so a coordinate cannot be collapsed one way in
    one place and another way in the other. Both halves are **mirror-invariant**:
    the gaps between consecutive sorted values mirror, so the clustering does,
    and a cluster's mean mirrors to the mirrored cluster's mean. That is the
    whole point -- see :func:`_forced_lines` for the m=1 coax residual that
    "keep the first of a close pair" produced.
    """
    clusters = []
    for v in values:
        if clusters and v - clusters[-1][-1] <= tol:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return [math.fsum(c) / len(c) for c in clusters]


def merge_tolerance(coarse, min_cell=0.0):
    """Separation (mm) below which two forced lines are one line on this axis."""
    return max(float(coarse) * 1.0e-3, 1.0e-6, float(min_cell))


def _forced_lines(snaps, lo, hi, coarse, min_cell=0.0):
    """Sorted, deduped forced grid lines spanning ``[lo, hi]`` (mm).

    Snap coordinates outside the inner region are dropped; the rest are clamped
    into ``[lo, hi]`` and merged when closer than a small tolerance (so two nearly
    coincident feature planes don't create a zero-width cell). When *min_cell* is
    positive it widens that tolerance, so two nearby features cannot force a
    sub-minimum cell. The result always starts at *lo* and ends at *hi* and is
    strictly increasing.

    **A merged cluster collapses to its mean**, which is what keeps the mesh
    mirror-symmetric when the geometry is. The obvious alternative -- walk the
    sorted lines and keep the first of any close pair -- is not: given the
    symmetric snaps ``[-2.5001, -2.4999, 2.4999, 2.5001]`` it keeps ``-2.5001``
    on the low side but ``+2.4999`` on the high side, so a body symmetric about
    the origin gets an asymmetric mesh and the fields inherit the break (this is
    how a coax picked up an m=1 residual). The mean is invariant under mirroring,
    and so is the single-linkage clustering that feeds it -- the gaps between
    consecutive sorted values are themselves mirrored, so both sides cluster the
    same way. (Single-linkage can chain across a run of closely spaced lines;
    that is accepted as the price of the symmetry guarantee, and *tol* is tiny.)

    A line merged under a large *min_cell* therefore moves by up to
    ``min_cell/2`` -- an interface deliberately traded away for the timestep,
    which is what asking for a minimum cell size means. *lo* and *hi* never move:
    they are the domain bounds the PML pads are measured from, so a cluster
    within *tol* of either is dropped rather than allowed to drag the edge.
    """
    tol = merge_tolerance(coarse, min_cell)

    inside = sorted(min(max(v, lo), hi) for v in snaps
                    if lo - tol <= v <= hi + tol)

    lines = [lo]
    for v in _cluster_means(inside, tol):
        if v - lines[-1] > tol and hi - v > tol:
            lines.append(v)
    if hi - lines[-1] > tol:
        lines.append(hi)
    else:
        lines[-1] = hi
    return lines


def _tiled_cells(w, coarse):
    """How many cells :func:`_graded_widths` lays across a gap of width *w*.

    ``floor(w / coarse)``, at least one -- the fill lays whole cells of at most
    *coarse* and absorbs the sub-cell leftover by scaling them all up. Only an
    estimate: a fine end size dragged in from a neighbouring feature makes the
    real fill lay more. Used solely to compare a gap against the two halves a
    candidate line would cut it into, where any such refinement applies to both
    sides of the comparison.
    """
    return max(int(math.floor(w / coarse + 1.0e-9)), 1)


def _insert_centre_lines(forced, centres, coarse, min_cell, caps):
    """*forced* plus every circle centre that costs the mesh nothing (mm).

    A centre line splits the gap it lands in, and the fill tiles a gap of width
    *w* with ``floor(w / target)`` cells (:func:`_tiled_cells`). Splitting is
    therefore free when the two halves still lay as many cells as the whole did,
    and **costs one** when they don't -- a gap spanning an odd number of targets
    is the case, halving to two cells the fill then stretches. Measured on the
    reference coax: the 3 mm inner conductor on a 1 mm grid went from three
    1.0 mm cells to two of 1.5 mm, 50% coarser through the conductor. Forcing a
    node onto the centre of an odd span costs a cell either way (``n-1`` or
    ``n+1``); ``n+1`` means overriding the fill's cap rule for every gap in every
    model, which on that coax was +23% cells and a 25% shorter timestep. So the
    rule here is simply: take the line where it is free, leave the mesh alone
    where it is not.

    **The decision has to be made here, against the real forced set** -- not per
    circle when the centres are collected. A circle's centre need not land in a
    gap bounded by its *own* silhouettes: in the coax, the shield's centre and
    the inner conductor's coincide, the shield's own diameter is already split by
    the conductor, and the gap the line actually falls in is the conductor's. Ask
    each circle about its own diameter and the shield answers "free" (10 cells
    across, even) while paying for it out of the conductor. Only the gap knows.

    Candidates are judged against the *original* line set and inserted together,
    rather than one at a time against the growing one, so that two mirrored
    candidates are judged identically -- the same mirror-invariance
    :func:`_cluster_means` exists for. A candidate landing within the merge
    tolerance of a line already there is dropped, not merged: it is a
    convenience, never something to move an interface for.
    """
    tol = merge_tolerance(coarse, min_cell)
    lo, hi = forced[0], forced[-1]
    accepted = []
    for c in _cluster_means(sorted(centres), tol):
        if not lo + tol < c < hi - tol:
            continue
        gap = next(((a, b) for a, b in zip(forced[:-1], forced[1:])
                    if a < c < b), None)
        if gap is None:
            continue  # already a forced line, or inside a merged cluster
        a, b = gap
        if c - a <= tol or b - c <= tol:
            continue
        target = max(_gap_coarse(a, b, coarse, caps), min_cell)
        if (_tiled_cells(c - a, target) + _tiled_cells(b - c, target)
                >= _tiled_cells(b - a, target)):
            accepted.append(c)
    if not accepted:
        return forced
    return sorted(forced + accepted)


def _graded_widths(w, hL, hR, H, r):
    """Cell widths (mm) tiling ``[0, w]``, summing exactly to *w*.

    Cells start ~``hL`` on the left and ~``hR`` on the right, growing by at most
    factor *r* toward a coarse cap *H* in the middle. The smaller pending cell is
    always laid next so the two sides stay balanced; the (sub-cell) leftover is
    removed by scaling all widths uniformly, which preserves the grading ratios.
    Always returns at least one positive width.

    A cell that *exactly* fills the remaining space is laid, not dropped: the
    stopping test is ``> remaining`` (within a relative epsilon), not ``>=``. With
    ``>=``, a gap that divides evenly by the target lost its last cell and the
    uniform rescale then stretched the survivors to cover it -- asking for 5
    cells of 1.0 mm across a 5 mm gap yielded 4 cells of 1.25 mm, 25% coarser
    than requested on precisely the gaps whose size was chosen deliberately.
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
    eps = w * 1.0e-12  # `remaining` accumulates rounding; don't reject an exact fit
    while True:
        remaining = w - xl - xr
        if remaining <= 0.0:
            break
        if sl <= sr:
            if sl > remaining + eps:
                break
            left.append(sl)
            xl += sl
            sl = min(sl * r, H)
        else:
            if sr > remaining + eps:
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
                     caps=(), line_sizes=(), centres=()):
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
    line_sizes : iterable of (coord_mm, size_mm)
        Grazing-surface requests (:func:`collect_curvature_sizes`): the cell size
        *at* a forced line, rather than over an interval. This is the natural
        shape for the request -- the surface is only unresolved right at its
        tangent plane, and the graded fill already knows how to spread a fine end
        size back out to the bulk at *ratio*. Empty ⇒ no curvature refinement.
    centres : iterable of float
        Circle-centre *candidates* (:func:`collect_axis_snaps`), promoted to
        forced lines only where they cost no cells (:func:`_insert_centre_lines`).
        Empty ⇒ no centre lines.

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
    if centres:
        forced = _insert_centre_lines(forced, centres, coarse, min_cell, caps)
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

    # Grazing-surface requests pin the size at a line rather than across a gap,
    # so they are applied after the gap-derived sizes and only tighten them. Each
    # attaches to the nearest forced line within half a coarse cell: the request's
    # own coordinate is sampled and approximate, while the line it belongs to was
    # placed exactly (a bounding-box face, or the snap this pass contributed when
    # there was none). A request with no line in range is dropped rather than
    # allowed to refine the wrong one.
    for coord, size in line_sizes:
        i = min(range(n), key=lambda k: abs(forced[k] - coord))
        if abs(forced[i] - coord) <= 0.5 * coarse:
            end_size[i] = min(end_size[i], max(size, min_cell))

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

def build_domain_nodes(sim, domain, force_pml_faces=(), modal_faces=()):
    """Snapped, graded ``(NodesX, NodesY, NodesZ)`` (world mm) for *domain*.

    Uses the material geometry bounds (grown for sources/monitors, via
    ``combined_bbox_mm``) as the inner region, the Domain's ``Dx/Dy/Dz`` as the
    coarse (background) interior target, its ``MaxGradingRatio`` as the grading
    bound and the per-face PML padding from ``domain_grid_params``.
    *force_pml_faces* (beam / SPICE-TEM launch faces) and *modal_faces* (Modal
    Port faces, which get no pad and no background gap) are forwarded so the node
    arrays carry exactly the padding and spacing the run's boundary assumes, even
    when the face's stored property says otherwise. Each material
    body additionally refines its own band down to its per-medium resolution (see
    :func:`collect_material_caps`), so higher-index regions are meshed finer than
    the void, and -- when the Domain's ``CurvatureRefinement`` factor is above 1 --
    lines where a curved face grazes an axis are refined and graded back out (see
    :func:`collect_curvature_sizes`). Every full circle offers a line through its
    centre, taken wherever the fill can absorb it for free
    (:func:`_insert_centre_lines`). Returns ``None`` when there is no geometry
    to bound (the caller falls back to a uniform grid).

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

    params = domain_mod.domain_grid_params(
        domain, force_pml_faces=force_pml_faces, modal_faces=modal_faces)
    sp_lo_mm = tuple(s * _MM_PER_M for s in params["spacing_lo"])
    sp_hi_mm = tuple(s * _MM_PER_M for s in params["spacing_hi"])
    pad_lo, pad_hi = params["pad_lo"], params["pad_hi"]
    coarse_mm = tuple(c * _MM_PER_M for c in domain_mod.cell_sizes_m(domain))
    ratio = max(float(getattr(domain, "MaxGradingRatio", 1.5)), 1.0 + 1.0e-6)
    min_cell_mm = domain_mod.min_cell_size_m(domain) * _MM_PER_M

    los = (bbox.XMin - sp_lo_mm[0], bbox.YMin - sp_lo_mm[1], bbox.ZMin - sp_lo_mm[2])
    his = (bbox.XMax + sp_hi_mm[0], bbox.YMax + sp_hi_mm[1], bbox.ZMax + sp_hi_mm[2])
    # Circle centres ride alongside the snaps as candidates; each attempt below
    # re-judges them at its own coarse target, since which of them are free
    # depends on it.
    centres = ([], [], [])
    snaps = collect_axis_snaps(materials, centres)
    # Per-axis material cell-size caps: higher-index bodies refine their band
    # below the coarse (background) target. Scaled alongside ``coarse`` in the
    # cell-count guard below, so a grid that must be coarsened to fit coarsens
    # material regions and void together.
    caps = collect_material_caps(sim, domain, materials)
    # Grazing-surface refinement. Collected after the snaps it attaches to, and
    # allowed to extend them for tangencies no bounding box reaches.
    curvature = float(getattr(domain, "CurvatureRefinement", 1.0))
    line_sizes = collect_curvature_sizes(
        materials, snaps, coarse_mm, curvature, min_cell_mm)

    nodes = None
    scale = 1.0
    for _attempt in range(12):
        scaled_caps = tuple(
            [(lo, hi, t * scale) for lo, hi, t in caps[a]] for a in range(3)
        )
        # Scaled alongside ``coarse`` for the same reason as the caps. The sag
        # term really goes as ``scale**2`` (it is quadratic in the transverse
        # cell), so this under-coarsens slightly -- acceptable in what is already
        # an emergency coarsening path, and it errs toward the finer mesh.
        scaled_sizes = tuple(
            [(c, s * scale) for c, s in line_sizes[a]] for a in range(3)
        )
        nodes = tuple(
            build_axis_nodes(
                snaps[a], los[a], his[a], coarse_mm[a] * scale, ratio,
                pad_lo[a], pad_hi[a], min_cell_mm, scaled_caps[a],
                scaled_sizes[a], centres[a],
            )
            for a in range(3)
        )
        total = (len(nodes[0]) - 1) * (len(nodes[1]) - 1) * (len(nodes[2]) - 1)
        if total <= _MAX_TOTAL_CELLS:
            break
        scale *= 1.5
    return nodes
