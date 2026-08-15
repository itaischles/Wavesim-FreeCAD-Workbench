# -*- coding: utf-8 -*-
"""Geometry voxelisation and job-from-document building (FreeCAD side).

Session 3 replaces the hardcoded Session-2 material box with real CAD geometry.
:func:`voxelize_materials` samples each Material's bodies onto a regular grid
(one planar ``Shape.slice`` per Z-layer, then a vectorised point-in-polygon test
of that cross-section over the layer's cell centres) to fill the per-cell
``eps``/``mu`` arrays and ``pec_mask`` the solver consumes via
``set_material_arrays``. With ``conformal=True`` a PEC body additionally
contributes the six Dey-Mittra open-fraction arrays (:data:`CONFORMAL_KEYS`), so
the solver can integrate the cut geometry instead of a staircase. A material
carrying a conductivity adds the three :data:`SIGMA_KEYS` arrays, which switch
the solver onto its lossy-dielectric E update.
:func:`build_job_from_document` derives a grid that bounds all material bodies,
voxelises into it, and returns a job spec plus the arrays to write as
``materials.npz``. Each future array-input concern (e.g. deferred array sources)
gets its own descriptively-named ``.npz`` rather than growing this one.

Empty voxels are filled with the Domain's chosen *background* Material (its
eps/mu/sigma/PEC), defaulting to vacuum; bodies overwrite the cells they cover.

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
import time

import FreeCAD

from wavesim_gui import sectionpool

# Forward-slash JSON paths and mm->m conversion are the only unit handling here.
_MM_PER_M = 1000.0

# The three ``materials.npz`` conductivity keys (S/m), in the order
# ``wavesim.set_material_arrays`` takes them. Written **all three or none**: the
# solver refuses a partial set, and a model with no conductivity anywhere emits
# none at all, which keeps it on the untouched one-coefficient E update.
SIGMA_KEYS = ("sigma_x", "sigma_y", "sigma_z")


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
    """Return ``[(shape_mm, eps, mu, pec, sigma, body), ...]`` per assigned body.

    One entry per body (a material with several bodies contributes several
    entries sharing its parameters). Bodies without a solid shape are skipped.
    ``sigma`` is the electric conductivity in S/m, always 0 for a PEC body
    (:func:`wavesim_gui.materials.material_sigma` owns that rule). ``body`` is the
    document object itself, carried so a conductor can be labelled by the solid
    it came from (see ``conductor_names`` in :func:`voxelize_materials`).
    """
    from wavesim_gui.materials import material_sigma

    entries = []
    for mat in materials:
        eps = float(getattr(mat, "Eps", 1.0))
        mu = float(getattr(mat, "Mu", 1.0))
        pec = bool(getattr(mat, "Pec", False))
        sigma = material_sigma(mat)
        for body in getattr(mat, "Bodies", []) or []:
            shape = getattr(body, "Shape", None)
            if shape is None or not getattr(shape, "Solids", None):
                continue
            entries.append((shape, eps, mu, pec, sigma, body))
    return entries


def _combined_bbox(entries):
    """Union BoundBox (mm) of all entry shapes, or ``None`` if there are none."""
    bbox = None
    for shape, _eps, _mu, _pec, _sigma, _body in entries:
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


# OCC's ``Shape.slice`` returns *nothing* for a plane lying within a tolerance
# band of a planar face of the solid, and that band scales with the shape rather
# than with the model unit: measured 3e-4 mm on a 100 mm cylinder, 3e-5 mm on a
# 1 mm one, 1e-6 mm on a 2 mm box. A conductor whose end face sits on a grid node
# plane -- the *normal* case, since a transmission line runs the full length of
# the domain -- therefore sections as empty, which reads as "no metal here at
# all" rather than as an error. This is what silently emptied the whole z = 0
# node plane of the conformal fractions; see :func:`_section_nudge`.
#
# A plane *tangent* to a curved face is the same degeneracy with the opposite
# symptom, and the more dangerous one because it produces geometry rather than
# none: the section comes back as a single **open** wire that walks the outer
# boundary and the inner one as one path. Discretised it is a polygon covering
# the body's whole footprint, so the layer reads as solid metal from wall to
# wall. Measured on a 250x320 mm casing with a 12.5 mm cable bore -- the plane
# tangent to that bore, which is a *node* plane because ``gridbuild`` snaps one
# onto the 12.5 mm jacket's bbox face:
#
#     z = -12.4999   2 wires, both closed   ->  23.1% of the plane inside
#     z = -12.5000   1 wire,  open          ->  97.8% of the plane inside
#
# Both planes then carried a full sheet of zero open fractions (3564 of 3564
# y-edges), the electrostatic solve took the two sheets for conductor and walled
# the cable off from the rest of the box, and every field in the domain came out
# identically zero. A solid's section is a set of *closed* curves, so an open
# wire is never geometry: :func:`_section_polygons` treats a section carrying one
# as degenerate and steps off the plane exactly as it does for an empty one.
_SLICE_DEAD_BAND = 1.0e-5           # of the shape's own extent (~30x measured)


def _section_nudge(body_shape, sub_mm):
    """How far (mm) to step off a degenerate section plane for *body_shape*.

    Large enough to clear :data:`_SLICE_DEAD_BAND` for a shape this size, and
    capped at a quarter of the finest sub-cell *sub_mm* so the retry can never
    smear geometry that genuinely varies within one sub-layer. A body enormous
    next to its sub-cell can hit that cap and still land inside the band; it
    then behaves exactly as it does today (empty layer), so the cap trades a
    rare unfixed case for never sampling the wrong sub-cell.
    """
    bb = body_shape.BoundBox
    extent = max(bb.XLength, bb.YLength, bb.ZLength, 1.0e-9)
    return min(_SLICE_DEAD_BAND * extent, 0.25 * sub_mm)


# Chord tolerance for the coarse (cell-centre) sweep's section polygons, as a
# fraction of the smallest in-plane cell.
#
# This decides ``pec_mask`` and the dielectric snap, so the polygon's radial
# error is a *whole cell* of mask error wherever a cell centre falls inside it.
# Two things follow, and the second is the reason this is 0.0025 and not the
# 0.25 it was until 2026-08-08:
#
# * **Systematic.** ``discretize`` inscribes, so every curved conductor comes out
#   undersized by the deflection. At 0.25 the reference coax's r = 9 mm shield
#   became a 22-gon of inscribed radius 8.909 mm -- 0.091 mm, 23% of a 0.4 mm
#   cell -- and the shield ate 2.8% more cells than it owns.
# * **Asymmetric.** ``Wire.discretize`` walks from the wire's seam, so the
#   polygon is not mirror-symmetric: measured on that shield, the boundary sits
#   at r = 9.00000 along +x against 8.98214 along -x. Cell centres landing in
#   that 0.018 mm window get opposite answers on the two sides, which put 4
#   stray cells per cross-section into the mask -- and a mask with a dipole
#   moment scatters TEM into a mode the modal port cannot absorb, leaving a
#   static m = 1 pile at both port planes (-31 dB) and a DC current the current
#   monitor reads forever. Tightening is the only lever: the error is bounded by
#   the deflection but *not* monotone in it (0.05 measured worse than 0.25), so
#   there is no safety factor to reason about, only "small enough that no sample
#   point lands in the window".
#
# Measured on that coax (48x48x252 graded, 0.4 mm cells), mask asymmetry against
# wall time -- the mirror mismatch vanishes at 0.01 and the cost does not move,
# because OCC sectioning dominates every mode and the scanline is linear in
# vertices off a small base:
#
#     fraction   deflection   verts   x-asym cells   plain   subpixel   conformal
#     0.25       0.100 mm       23        1008       3.88 s   10.71 s    46.78 s
#     0.05       0.020 mm       51        1512(!)      --        --         --
#     0.01       0.004 mm      113           0         --        --         --
#     0.0025     0.001 mm      222           0       4.03 s   10.24 s    45.76 s
#
# (those wall times isolate *this* fraction; tightening all three costs ~10%.)
#
# 0.0025 is 4x inside the measured threshold. **This deliberately re-baselines
# every staircase result** -- ``pec_mask`` moves by 2.8% of the metal on that
# model -- which is why it was not done as part of the conformal work (plan W2
# scoped itself to the conformal sampler to keep that promise, and
# CONFORMAL_PEC_PLAN.md W2 records why that scoping was wrong).
# ``tools/check_mask_symmetry.py`` is the gate.
COARSE_CHORD_FRACTION = 0.0025


# --------------------------------------------------------------------------- #
# Prefetched sections
#
# ``Shape.slice`` is ~80% of a conformal voxelisation and holds the GIL, so the
# only way to make it faster is to cut planes in other *processes*
# (:mod:`wavesim_gui.sectionpool`). Every sampler below has the same shape -- a
# loop over Z planes whose full plane list is knowable before the loop starts --
# so one hook serves all three: cut the whole list up front in parallel, park the
# polygons in this cache, and let the untouched serial loop find them here.
#
# The cache is consulted by :func:`_section_polygons` itself, so a plane that was
# *not* prefetched (or a pool that declined) simply falls through to the OCC call
# it always made. That is what keeps the pool an accelerator rather than a second
# source of truth: the worst case is the old speed, never a different answer.
# --------------------------------------------------------------------------- #

_MISSING = object()

# Set only for the duration of one sampler's pass over one body; see
# :func:`_prefetched`. Module-level rather than threaded through every call so
# the samplers keep their signatures.
_ACTIVE_CACHE = None

# ``[seconds, count]`` while a serial pass is being timed to decide whether the
# pool is worth starting, else None. It accumulates the time spent *cutting*
# only, deliberately not the sampler's own point-in-polygon work: the pool moves
# the sections and nothing else, so timing the whole loop overstates what it can
# save. On a 361x98x98 model whose in-plane scanline dwarfs its simple sections
# that error was enough to talk the pool into a 3.1 s sweep it returned in 5.0 s.
_SECTION_TIMER = None


class _SectionCache(object):
    """Polygons for one shape, keyed by the arguments that produced them."""

    __slots__ = ("shape", "planes")

    def __init__(self, shape):
        self.shape = shape
        self.planes = {}


def _is_z_axis(axis):
    """Whether *axis* is the +Z direction the cache keys assume."""
    return (getattr(axis, "x", None) == 0.0 and getattr(axis, "y", None) == 0.0
            and getattr(axis, "z", None) == 1.0)


def _worker_setting():
    """The ``voxelize_workers`` setting, or ``'auto'`` when it can't be read.

    Voxelisation must not fail because a settings file is missing or unreadable,
    so anything unexpected here falls back to the default rather than raising.
    """
    try:
        import wavesim_settings

        return wavesim_settings.get_voxelize_workers()
    except Exception:
        return "auto"


def _prefetched(pool, body_shape, planes, deflection, nudge=0.0, on_layer=None):
    """Context manager: cut *planes* in parallel, cache them, tear down after.

    *planes* is an iterable of Z coordinates, all to be cut at the same
    *deflection* and *nudge*. Yields ``True`` when the pool ran -- in which case
    it has already called *on_layer* once per plane, so the caller's own
    per-section tick must not count them again -- and ``False`` when nothing was
    prefetched and the caller should behave exactly as before.
    """
    import contextlib
    import time

    @contextlib.contextmanager
    def _run():
        global _ACTIVE_CACHE

        zs = [float(z) for z in planes]
        if pool is None or not zs:
            yield False
            return
        requests = [(z, float(deflection), float(nudge)) for z in zs]
        try:
            results = pool.sections(body_shape, requests, on_progress=on_layer)
        except sectionpool.SectionPoolCancelled:
            raise VoxelizationCancelled()
        if results is None:
            # Declined -- usually because the pool has yet to learn what a plane
            # of this geometry costs. Time the sections of the serial pass it
            # falls back to and tell it, so the *next* batch is decided on a
            # measurement of this model rather than a guess about it.
            global _SECTION_TIMER

            timer = [0.0, 0]
            previous_timer = _SECTION_TIMER
            _SECTION_TIMER = timer
            try:
                yield False
            finally:
                _SECTION_TIMER = previous_timer
            pool.observe(timer[1], timer[0])
            return

        cache = _SectionCache(body_shape)
        for (z, defl, nud), polys in zip(requests, results):
            # A worker that raised on one plane leaves the sentinel; drop it and
            # the serial path cuts that plane itself.
            if not isinstance(polys, str):
                cache.planes[(z, defl, nud)] = polys
        previous = _ACTIVE_CACHE
        _ACTIVE_CACHE = cache
        try:
            yield True
        finally:
            _ACTIVE_CACHE = previous

    return _run()


def _section_polygons(body_shape, z_axis, z, deflection, nudge=0.0):
    """Cross-section of *body_shape* at height *z* as a list of XY polygons.

    Cuts the body with the horizontal plane at *z* -- one OCC section per layer
    instead of one ``isInside`` per cell -- and turns each resulting wire into a
    polygon (curved edges discretised to chord tolerance *deflection*). Returns
    ``None`` when the plane misses the solid (no section wires, or none that
    close), so the caller can leave that whole layer empty.

    *nudge* > 0 asks for one retry just off a plane whose section was
    **degenerate** -- nothing at all, or a wire that does not close -- which is
    how a face-coincident or curve-tangent plane is told apart from a genuine
    miss (see :data:`_SLICE_DEAD_BAND`). ``+nudge`` is tried first, so a solid
    resting *on* the plane reports the material it carries; ``-nudge`` then
    catches a top face, where ``+`` is a real miss. An internal horizontal face
    gets the material above it, which is the same tie-break the tangency cases
    already take: a surface counts as covered. Defaults to 0 -- **only the
    conformal sampler passes it**, so the coarse binary sweep and the dielectric
    smoother stay on the plane they were asked for and take its closed wires
    alone. Dropping the open one is not free for them either -- it moves a cell
    whose centre the phantom polygon covered -- but it moves it off an answer
    that was never the body's shape, and their planes are cell *centres*, where
    a surface has no reason to land.
    """
    cache = _ACTIVE_CACHE
    if cache is not None and cache.shape is body_shape and _is_z_axis(z_axis):
        hit = cache.planes.get((float(z), float(deflection), float(nudge)),
                               _MISSING)
        if hit is not _MISSING:
            return hit

    timer = _SECTION_TIMER
    if timer is None:
        return _cut_section(body_shape, z_axis, z, deflection, nudge)
    started = time.perf_counter()
    try:
        return _cut_section(body_shape, z_axis, z, deflection, nudge)
    finally:
        timer[0] += time.perf_counter() - started
        timer[1] += 1


def _cut_section(body_shape, z_axis, z, deflection, nudge=0.0):
    """:func:`_section_polygons` without the cache: always cuts the plane."""
    import numpy as np

    def _at(zz):
        """``(polygons, clean)`` at *zz*: the closed wires, and whether all were.

        An open wire is dropped rather than filled, because a polygon closed by
        the drawing rule instead of by the geometry is the whole-footprint fill
        :data:`_SLICE_DEAD_BAND` describes. ``clean`` is False when one was seen,
        so the caller can prefer another plane over these polygons even though
        they are not empty.
        """
        try:
            wires = body_shape.slice(z_axis, zz)
        except Exception:
            return None, False
        if not wires:
            return None, False
        polys, clean = [], True
        for w in wires:
            try:
                if not w.isClosed():
                    clean = False
                    continue
                verts = w.discretize(Deflection=deflection)
            except Exception:
                continue
            if len(verts) < 3:
                continue
            polys.append(np.array([(v.x, v.y) for v in verts]))
        return (polys or None), clean

    polys, clean = _at(z)
    if (polys is not None and clean) or not nudge:
        return polys
    for zz in (z + nudge, z - nudge):
        retry, retry_clean = _at(zz)
        if retry is not None and retry_clean:
            return retry
    # Neither neighbour is any better: the closed wires of the requested plane
    # are still the best answer available, and are what this returned before the
    # retry existed.
    return polys


def _layer_inside(body_shape, z_axis, z, pts, deflection, nudge=0.0):
    """Boolean mask of which *pts* (XY, mm) lie inside *body_shape* at height *z*.

    Tests every point at once with matplotlib. XOR-ing the wires applies the
    even-odd rule, which carves holes and handles solids nested inside holes.
    Returns ``None`` when the plane misses the solid. *nudge* is
    :func:`_section_polygons`'s degenerate-plane retry.

    For the (usual) case of points forming an axis-aligned lattice, prefer
    :func:`_layer_inside_lattice` -- same answer, without the
    ``O(points x vertices)``.
    """
    import numpy as np
    from matplotlib.path import Path

    polys = _section_polygons(body_shape, z_axis, z, deflection, nudge)
    if polys is None:
        return None
    inside = np.zeros(len(pts), dtype=bool)
    for poly in polys:
        inside ^= Path(poly).contains_points(pts)
    return inside


def _layer_inside_lattice(body_shape, z_axis, z, xs, ys, deflection, nudge=0.0):
    """:func:`_layer_inside` for a lattice of sample points ``xs`` x ``ys``.

    Returns a ``(len(xs), len(ys))`` mask (or ``None``) rather than a flat one --
    the same values a flat call would give for
    ``meshgrid(xs, ys, indexing="ij")``, since
    :func:`wavesim_gui.scanline.lattice_inside` reproduces matplotlib's crossing
    rule exactly (``tools/check_scanline.py``). ``xs`` must be ascending.
    *nudge* is :func:`_section_polygons`'s degenerate-plane retry.
    """
    import numpy as np

    from wavesim_gui.scanline import lattice_inside

    polys = _section_polygons(body_shape, z_axis, z, deflection, nudge)
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


# Chord tolerance for the subpixel smoother's fine section polygons, as a
# fraction of the finest sub-cell width. Same story as
# :data:`COARSE_CHORD_FRACTION` one level down: the smoother's samples are
# sub-cell centres, and a polygon that is not mirror-symmetric hands mirror
# partners different ``inside`` answers, which Kottke then bakes into an
# asymmetric ``eps_*``. Measured on ``tools/check_mask_symmetry.py``'s graded
# coax, worst mirror mismatch in ``eps_x`` against the fraction:
#
#     0.25 -> 0.0945      0.05 -> 4.4e-16      0.01 -> 4.4e-16     0.0025 -> 4.4e-16
#
# i.e. it collapses to round-off (2 ULP of the Kottke reduction, the floor for a
# float array) by 0.05. 0.0025 keeps 20x margin for a body whose surface happens
# to sit closer to the sub-cell ruler than this coax's does; it costs ~12% of a
# smoothed run's voxelisation time (10.7 -> 12.0 s on the 0.6M-cell coax).
_SUBPIXEL_CHORD_FRACTION = 0.0025


def _smooth_dielectric_body(arrays, body_shape, eps_r, mu_r,
                            nodes_mm, span, oversample, on_layer=None,
                            sigma_r=0.0, pool=None):
    """Subpixel-smooth one dielectric body into ``eps_x/y/z`` (+ mu, sigma).

    *span* is ``((ia, ib), (ja, jb), (ka, kb))`` -- the half-open coarse sub-block
    covering the body's bbox (plus margin) from :func:`_cell_span`. The body is
    fine-sampled at *oversample* ``(ox, oy, oz)`` sub-cells per coarse cell per
    axis (one OCC section per fine Z sub-layer, matplotlib point-in-polygon over
    the fine XY sub-centres), then reduced with
    :func:`wavesim_gui.subpixel.reduce_fine_eps` to a diagonal effective tensor.

    The **background** inside the block is the current ``eps_x`` there, so bodies
    compose in placement order (mirrors the solver's repeated
    ``smooth_shape_region`` calls). ``mu_r != 1`` and ``sigma_r`` are applied by
    volume-fraction averaging -- Kottke's reduction is derived for a real
    permittivity and has no conductivity analogue that is not
    frequency-dependent (see :func:`voxelize_materials`); a dielectric that
    majority-covers a cell clears a PEC background. ``on_layer()`` is called once
    per fine Z sub-layer (progress + cancellation); a truthy return raises
    :class:`VoxelizationCancelled`.
    """
    import numpy as np

    from wavesim_gui import subpixel as sp

    nx_mm, ny_mm, nz_mm = nodes_mm
    (ia, ib), (ja, jb), (ka, kb) = span
    ox, oy, oz = sp.as_triplet(oversample)

    xf = sp.fine_axis(nx_mm, ox, ia, ib)
    yf = sp.fine_axis(ny_mm, oy, ja, jb)
    zf = sp.fine_axis(nz_mm, oz, ka, kb)

    # Chord tolerance for the fine section polygons, as a fraction of the
    # smallest fine sub-cell width (:data:`_SUBPIXEL_CHORD_FRACTION`).
    def _min_sub(nodes, o, a, b):
        w = np.diff(nodes[a:b + 1])
        return (float(w.min()) / o) if w.size else 1.0

    df = min(_min_sub(nx_mm, ox, ia, ib), _min_sub(ny_mm, oy, ja, jb))
    deflection = max(_SUBPIXEL_CHORD_FRACTION * df, df * 1.0e-6)

    Z_AXIS = FreeCAD.Vector(0.0, 0.0, 1.0)
    inside_fine = np.zeros((xf.size, yf.size, zf.size), dtype=bool)
    # *pool* cuts the whole fine Z sweep in parallel up front (see _prefetched);
    # it ticks on_layer for those planes itself, so the loop stops ticking.
    with _prefetched(pool, body_shape, [float(z) for z in zf], deflection,
                     0.0, on_layer) as prefetched:
        for kz in range(zf.size):
            layer = _layer_inside_lattice(body_shape, Z_AXIS, float(zf[kz]),
                                          xf, yf, deflection)
            if layer is not None and layer.any():
                inside_fine[:, :, kz] = layer
            if not prefetched and on_layer is not None and on_layer():
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
    # Conductivity, when this run carries any. Unconditional once the arrays
    # exist (not gated on ``sigma_r != 0`` the way mu is on ``mu_r != 1``): a
    # lossless body must blend the background's conductivity *down* over its own
    # cells, and skipping the write would leave a lossy background standing
    # inside it.
    if SIGMA_KEYS[0] in arrays:
        for key in SIGMA_KEYS:
            sigma_bg = arrays[key][ia:ib, ja:jb, ka:kb]
            arrays[key][ia:ib, ja:jb, ka:kb] = (
                frac * float(sigma_r) + (1.0 - frac) * sigma_bg
            )
    # A dielectric body clears a PEC background where it majority-covers a cell
    # (matching the coarse centre-inside rule to within half a cell).
    covered = frac >= 0.5
    if covered.any():
        sub = arrays["pec_mask"][ia:ib, ja:jb, ka:kb]
        sub[covered] = False
        # ...and the part label with it. A name identifies a conductor, it does
        # not create one, so a label left standing on a cell that is no longer
        # metal describes something the field solver cannot see -- which the
        # solver refuses outright. Found by a coax, where the dielectric annulus
        # majority-covers the conductors' own boundary cells.
        part = arrays.get("pec_id")
        if part is not None:
            part[ia:ib, ja:jb, ka:kb][covered] = 0


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
# the finest **sub-cell** width -- so with :data:`CONFORMAL_OVERSAMPLE` = 8 it is
# ~0.6% of a coarse cell. It exists because a polygon's radius error would
# otherwise sit straight on top of the fractions and cap the accuracy conformal
# PEC buys.
#
# Tightened from 0.05 for the same mirror-symmetry reason as
# :data:`COARSE_CHORD_FRACTION` -- averaging over ``oversample`` samples softens
# a polygon slip but does not cancel it, and a fraction array with a dipole
# moment feeds the mode solver and the H update directly. Measured on
# ``tools/check_mask_symmetry.py``'s graded coax, worst mirror mismatch across
# the six arrays:
#
#     0.05 -> 0.125 (one whole sample in 8, on pec_edge_open_x)
#     0.01 -> 0      0.0025 -> 0      0.0005 -> 0
#
# A uniform grid can hide this completely: on the 1 mm coax of the port
# investigation all six arrays were symmetric at 0.05, and only the graded case
# exposed it. Costs ~9% of a conformal run's voxelisation time (46.8 -> 51.0 s on
# the 0.6M-cell coax) and slightly *improves* the fractions against the closed
# form (``tools/check_conformal_fractions.py``: max edge error 0.0666 -> 0.0613,
# all-six rms 0.0181 -> 0.0180 at oversample 8).
_CONFORMAL_CHORD_FRACTION = 0.0025


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


def _band_blocks_lattice(body_shape, z_axis, z, lat, os_, deflection, nudge=0.0):
    """Per-band-cell ``(n, os_+1, os_+1)`` occupancy at *z*, via one scanline.

    *lat* is :func:`_band_lattice`'s tuple. Returns ``None`` when the plane
    misses the solid.
    """
    xs, ys, mi, mj = lat
    grid = _layer_inside_lattice(body_shape, z_axis, z, xs, ys, deflection,
                                 nudge)
    if grid is None:
        return None
    view = grid.reshape(xs.size // (os_ + 1), os_ + 1,
                        ys.size // (os_ + 1), os_ + 1)
    return view[mi, :, mj, :]


def _band_blocks_flat(body_shape, z_axis, z, xs, ys, os_, deflection, nudge=0.0):
    """:func:`_band_blocks_lattice`'s fallback: one flat point list per cell.

    *xs*/*ys* are the ``(n, os_+1)`` per-cell sample coordinates.
    """
    import numpy as np

    px = np.repeat(xs, os_ + 1, axis=1)
    py = np.tile(ys, (1, os_ + 1))
    flat = _layer_inside(body_shape, z_axis, z,
                         np.column_stack([px.ravel(), py.ravel()]), deflection,
                         nudge)
    if flat is None:
        return None
    return flat.reshape(xs.shape[0], os_ + 1, os_ + 1)


def _conformal_pec_body(covered, body_shape, nodes_mm, span, os_, on_layer=None,
                        pool=None):
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

    *pool* is an optional :class:`wavesim_gui.sectionpool.SectionPool`. Both
    passes know their whole plane list before they start looping -- pass 2's as
    soon as the band exists -- so each is prefetched in one parallel batch and
    the loops below then read cut polygons instead of cutting them. The pool
    ticks *on_layer* itself for the planes it cuts, which is why the loops stop
    ticking while it is in use.
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

    # Retry distance for a section plane that lands on a planar face of the body.
    # A conductor spanning the domain puts its end faces on node planes, and this
    # sampler is the only one that sections *on* node planes at all.
    nudge = _section_nudge(body_shape, _min_sub(nz_mm, ka, kb))

    Z_AXIS = FreeCAD.Vector(0.0, 0.0, 1.0)

    # False while a prefetch has already counted this pass's planes against the
    # progress total -- ticking again would double-count every section.
    counted = [True]

    def _tick():
        if not counted[0]:
            return
        if on_layer is not None and on_layer():
            raise VoxelizationCancelled()

    # ---------------- pass 1: node lattice -> the surface band -------------- #
    xn, yn, zn = nx_mm[ia:ib + 1], ny_mm[ja:jb + 1], nz_mm[ka:kb + 1]
    node_in = np.zeros((ni + 1, nj + 1, nk + 1), dtype=bool)
    with _prefetched(pool, body_shape, [float(z) for z in zn],
                     deflection, nudge, on_layer) as prefetched:
        counted[0] = not prefetched
        for kk in range(nk + 1):
            layer = _layer_inside_lattice(body_shape, Z_AXIS, float(zn[kk]),
                                          xn, yn, deflection, nudge)
            if layer is not None and layer.any():
                node_in[:, :, kk] = layer
            _tick()
    counted[0] = True

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
        layers = np.unique(bk)
        # Every plane this pass will cut, known now that the band exists: one
        # z-node plane plus os_ z-sub planes per occupied cell layer.
        planes = []
        for k in layers:
            planes.append(float(zb[k, 0]))
            planes.extend(float(zb[k, m + 1]) for m in range(os_))
        with _prefetched(pool, body_shape, planes, deflection, nudge,
                         on_layer) as prefetched:
            counted[0] = not prefetched
            for k in layers:
                sel = bk == k
                ii, jj = bi[sel], bj[sel]
                xs, ys = xb[ii], yb[jj]                  # (n, os_+1) each
                n = ii.size
                lat = _band_lattice(ii, jj, xb, yb)

                # z-node plane: the full (os_+1)^2 xy sub-block per cell.
                layer = (_band_blocks_lattice(body_shape, Z_AXIS,
                                              float(zb[k, 0]), lat, os_,
                                              deflection, nudge)
                         if lat is not None else
                         _band_blocks_flat(body_shape, Z_AXIS, float(zb[k, 0]),
                                           xs, ys, os_, deflection, nudge))
                _tick()
                blk = (np.zeros((n, os_ + 1, os_ + 1), dtype=bool)
                       if layer is None else layer)
                local["pec_edge_open_x"][ii, jj, k] = blk[:, 1:, 0].mean(axis=1)
                local["pec_edge_open_y"][ii, jj, k] = blk[:, 0, 1:].mean(axis=1)
                local["pec_face_open_z"][ii, jj, k] = (
                    blk[:, 1:, 1:].mean(axis=(1, 2)))

                # z-sub planes: only the 2*os_+1 point cross through the low
                # corner is needed -- [0] node/node, [1:1+os_] x-node/y-sub,
                # [1+os_:] x-sub/y-node. The flat path samples exactly that; the
                # lattice path answers the whole sub-block for the same work and
                # slices it.
                if lat is None:
                    px = np.concatenate([xs[:, :1],
                                         np.repeat(xs[:, :1], os_, axis=1),
                                         xs[:, 1:]], axis=1)
                    py = np.concatenate([ys[:, :1], ys[:, 1:],
                                         np.repeat(ys[:, :1], os_, axis=1)],
                                        axis=1)
                    cross_pts = np.column_stack([px.ravel(), py.ravel()])
                cross = np.zeros((n, os_, 1 + 2 * os_), dtype=bool)
                for m in range(os_):
                    z = float(zb[k, m + 1])
                    if lat is None:
                        layer = _layer_inside(body_shape, Z_AXIS, z,
                                              cross_pts, deflection, nudge)
                        _tick()
                        if layer is not None and layer.any():
                            cross[:, m, :] = layer.reshape(n, 1 + 2 * os_)
                        continue
                    sub = _band_blocks_lattice(body_shape, Z_AXIS, z, lat, os_,
                                               deflection, nudge)
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
        counted[0] = True

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


# Face-neighbour directions, in the fixed order :func:`_fill_pec_materials`
# consults them: ``(axis, step)``.
_FILL_DIRS = ((0, -1), (0, +1), (1, -1), (1, +1), (2, -1), (2, +1))


def _fill_pec_materials(arrays, pec_mask, keys):
    """Give each conductor cell the material of the open medium beside it.

    A PEC cell carries no meaningful eps/mu/sigma -- nothing propagates inside
    metal -- so the sweep above simply leaves the *background* value there. On
    the **staircase** path that is invisible: ``build_pec_edge_masks`` dilates,
    holding every edge that touches a metal cell at zero, so no live edge ever
    reads it.

    Conformal PEC removes that dilation on purpose (plan S3): an edge is zeroed
    by its **own** open length, so the edges running just outside a conductor
    stay alive and carry the field. Each of them reads its material from the cell
    it is *indexed* by -- and for an edge on the low-index side of a surface that
    is the cell whose centre sits in the metal. The error is therefore one-sided:
    on the reference coax the live edges hugging one side of each conductor read
    eps_r = 1 while their mirror partners read the fill's 2.3.

    That is enough to break the modal port. With eps non-uniform over the live
    edge set the mode's e-hat stops being a null vector of the FDTD's own
    transverse curl at the conductor-adjacent **free** nodes (measured: residual
    0.46 of scale at 18 nodes, against 5e-15 when eps is uniform there), so the
    port's impedance sheet deposits a static field on its own plane every step
    with nothing to restore it. Measured end to end on
    ``results/coaxial_line`` (19x19x101, eps_r = 2.3, Gaussian drive):

    ======================================  ==============  ==============
    conductor cells carry                   port residual   line DC current
    ======================================  ==============  ==============
    the background eps (before)              -8.6 dB         -26 dB
    the surrounding medium (this function)  -104 dB         -158 dB
    ======================================  ==============  ==============

    Z0 and eps_eff do not move: the mode solver already applies this rule for
    itself (solver S5c gives a face straddling the surface the eps of the face
    *outward*), which is exactly why the port's Z0 was right while the run it
    presented was not. Filling here makes the FDTD read the same material the
    mode solve assumed, in the one place they disagreed.

    The fill walks outward from the open medium one cell per pass and stops as
    soon as every cell a live element can read from has been reached -- a PEC
    cell that still owns an open Yee edge or face. Metal deeper than that keeps
    the background value, because every one of its edges and faces is fully
    covered and nothing reads it. On the reference coax that is 6 passes over
    36k cells; the frontier shrinks every pass, so a thick conductor costs
    roughly its own volume once, not its volume per pass.
    Ties are broken by the fixed direction order of :data:`_FILL_DIRS` so the
    result is deterministic; where two media meet on a conductor surface the
    cell can only carry one of them, and either is a defensible answer for a
    cell that is metal in the first place.

    Returns the number of cells filled.
    """
    import numpy as np

    open_any = np.zeros(pec_mask.shape, dtype=bool)
    for key in CONFORMAL_KEYS:
        open_any |= arrays[key] > 0.0
    needed = pec_mask & open_any
    if not needed.any():
        return 0

    unknown = pec_mask.copy()
    filled = 0
    while True:
        known = ~unknown
        took = np.zeros_like(unknown)
        for axis, step in _FILL_DIRS:
            dst = [slice(None)] * 3
            src = [slice(None)] * 3
            if step > 0:
                dst[axis], src[axis] = slice(0, -1), slice(1, None)
            else:
                dst[axis], src[axis] = slice(1, None), slice(0, -1)
            dst, src = tuple(dst), tuple(src)
            # ``known`` is sampled once per pass, so a cell filled earlier in
            # this same pass cannot donate -- which keeps the fill a proper
            # breadth-first walk outward from the open medium.
            take = unknown[dst] & known[src] & ~took[dst]
            if not take.any():
                continue
            for key in keys:
                a = arrays[key]
                a[dst][take] = a[src][take]
            took[dst] |= take
        if not took.any():
            break               # metal with no open medium anywhere beside it
        unknown &= ~took
        filled += int(np.count_nonzero(took))
        if not (unknown & needed).any():
            break
    return filled


def voxelize_materials(materials, cell_size_m,
                       spacing_lo_m=(0.0, 0.0, 0.0), spacing_hi_m=(0.0, 0.0, 0.0),
                       pad_lo=(8, 8, 8), pad_hi=(8, 8, 8),
                       extra_points_mm=(), extra_axis_offsets=(),
                       bg_eps=1.0, bg_mu=1.0, bg_pec=False, bg_sigma=0.0,
                       nodes_m=None, subpixel=False, oversample=4,
                       conformal=False, conformal_oversample=None,
                       conductor_names=None,
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

        A **lossy** body's conductivity is smoothed by plain volume fraction
        (``frac*sigma + (1-frac)*background``), the same rule ``mu_r`` already
        takes, not by Kottke's tensor -- that reduction is derived for a real
        permittivity, and conductivity is the imaginary part of
        ``eps~ = eps - j*sigma/(w*eps0)``, so its correct smoothing is
        frequency-dependent and a real (eps, sigma) pair cannot carry it. The
        volume fraction is the standard approximation and keeps the boundary
        cell consistent with its own smoothed eps; the exact treatment would be
        a dispersive material model.
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
    conductor_names : dict, optional
        ``{body.Name: "part label"}`` for the PEC bodies an **electrostatic** run
        must be able to address individually. Passing it adds a ``pec_id``
        integer array to the returned arrays (the part owning each conductor
        cell, 0 = unnamed metal) and a ``pec_names`` map to the result. Omitting
        it (the default) allocates neither, so a full-wave ``materials.npz`` is
        exactly what it always was -- the FDTD path has no use for a conductor's
        identity, a perfect conductor being a boundary condition rather than a
        thing with a name.
    bg_eps, bg_mu, bg_pec, bg_sigma : float / float / bool / float
        The background medium filling every "empty" voxel -- the
        eps/mu/PEC/conductivity of the Domain's chosen background Material
        (vacuum: ``1.0, 1.0, False, 0.0``). The arrays start filled with these;
        bodies overwrite the cells they cover.
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
                      on and the geometry actually cuts a face, plus the three
                      :data:`SIGMA_KEYS` arrays when any material (or the
                      background) is lossy. All three or none: the solver takes
                      them together, and a partial set would leave one field
                      component undamped. A model with no conductivity anywhere
                      emits none, and runs the untouched one-coefficient E
                      update.
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

    # Conductivity, allocated only when something in the model is actually
    # lossy. Absent arrays are not the same as all-zero ones: they select the
    # solver's one-coefficient E update, which cannot differ from a lossless run
    # because it is the same code (all-zero arrays are bit-identical too, but
    # cost three array reads per E component per step for nothing).
    bg_sigma = max(0.0, float(bg_sigma))
    lossy = bg_sigma > 0.0 or any(entry[4] > 0.0 for entry in entries)
    if lossy:
        for key in SIGMA_KEYS:
            arrays[key] = np.full(shape, bg_sigma, dtype=np.float64)
    sigma_arrays = [arrays[key] for key in SIGMA_KEYS] if lossy else []

    # Named PEC parts: an integer label per conductor cell (0 = unnamed metal),
    # so the electrostatic solver can hold *this* solid at a potential. The FDTD
    # path reads neither the array nor the names, and the array is only allocated
    # when names were asked for, so a full-wave materials.npz is unchanged.
    # Labels are 1-based and assigned in the order the caller listed them, not in
    # raster order: a refinement must not renumber a saved potential onto
    # different metal.
    pec_id = None
    part_ids = {}
    pec_names = {}
    if conductor_names:
        pec_id = np.zeros(shape, dtype=np.int32)
        arrays["pec_id"] = pec_id
        for body_name, label in conductor_names.items():
            part_ids[str(body_name)] = len(pec_names) + 1
            pec_names[str(label)] = part_ids[str(body_name)]

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
    # Chord tolerance for turning curved section edges into polygons
    # (:data:`COARSE_CHORD_FRACTION` of the smallest in-plane cell, never below
    # the geometric tolerance).
    deflection = max(min(dx_mm, dy_mm) * COARSE_CHORD_FRACTION, tol)

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
    for body_shape, eps, mu, pec, sigma, body in entries:
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
        # The part label this body's cells are stamped with, or 0 for metal the
        # electrostatic solver will treat as unnamed (and ground). Resolved here
        # so the sweep below has nothing to look up.
        part = 0
        if pec and conductor_names:
            part = part_ids.get(str(getattr(body, "Name", "")), 0)
        plans.append((body_shape, eps, mu, pec, sigma, i_idx, j_idx, k_idx,
                      smooth, span, c_span, part))
        total_layers += n_layers

    done_layers = 0
    if progress is not None:
        progress(0, total_layers)
    def _on_layer():
        nonlocal done_layers
        done_layers += 1
        return bool(progress is not None
                    and progress(done_layers, total_layers))

    # One pool of section workers for the whole sweep, so the (real, one-off)
    # cost of starting them is paid once across every body and pass rather than
    # per body. It starts lazily on the first batch big enough to be worth
    # dispatching, so a small model never spawns a process at all, and it is
    # closed unconditionally -- including on a cancel, which raises through here.
    pool = sectionpool.SectionPool(
        sectionpool.resolve_workers(_worker_setting()))
    pool.plan(total_layers)
    try:
        for (body_shape, eps, mu, pec, sigma, i_idx, j_idx, k_idx, smooth,
             span, c_span, part) in plans:
            # Conformal open fractions for a PEC body, alongside (not instead
            # of) the binary mask below: pec_mask stays in the contract as the
            # fully-covered test and as the staircase path's own geometry.
            if c_span is not None:
                _conformal_pec_body(covered, body_shape, nodes_mm, c_span,
                                    c_ovr, on_layer=_on_layer, pool=pool)
            if smooth:
                # Subpixel dielectric: fine-sample the body over its bbox
                # sub-block and reduce to an anisotropic effective permittivity
                # (in place).
                _smooth_dielectric_body(
                    arrays, body_shape, eps, mu, nodes_mm, span, ovr,
                    on_layer=_on_layer, sigma_r=sigma, pool=pool,
                )
                continue
            if len(i_idx) == 0 or len(j_idx) == 0 or len(k_idx) == 0:
                continue
            # XY cell centres for this body's bbox -- a lattice, tested in a
            # single vectorised call per Z-layer cross-section.
            bx, by = xs[i_idx], ys[j_idx]
            # *pool* cuts this body's whole Z sweep in parallel up front and
            # ticks the progress for those planes itself (see _prefetched), so
            # the loop's own tick below stands down while it is in use.
            with _prefetched(pool, body_shape, [float(zs[k]) for k in k_idx],
                             deflection, 0.0, _on_layer) as prefetched:
                for k in k_idx:
                    inside = _layer_inside_lattice(
                        body_shape, Z_AXIS, float(zs[k]), bx, by, deflection)
                    if inside is not None and inside.any():
                        ii, jj = np.nonzero(inside)
                        gi, gj = i_idx[ii], j_idx[jj]
                        if pec:
                            pec_mask[gi, gj, k] = True
                            if pec_id is not None:
                                # Later bodies win the overlap, exactly as
                                # pec_mask composes: the label has to describe
                                # the metal the mask ends up carrying, or the
                                # solver would pin a potential on cells the
                                # field solver does not see as that part.
                                pec_id[gi, gj, k] = part
                        else:
                            eps_x[gi, gj, k] = eps
                            eps_y[gi, gj, k] = eps
                            eps_z[gi, gj, k] = eps
                            mu_x[gi, gj, k] = mu
                            mu_y[gi, gj, k] = mu
                            mu_z[gi, gj, k] = mu
                            # Written even when this body is lossless, so a
                            # lossless body placed over a lossy background
                            # actually clears the conductivity there instead of
                            # inheriting it.
                            for arr in sigma_arrays:
                                arr[gi, gj, k] = sigma
                            # A dielectric body overrides a PEC background at
                            # its cells.
                            pec_mask[gi, gj, k] = False
                            if pec_id is not None:
                                # ...and takes the part label with it. A label
                                # outside the mask would describe metal that is
                                # no longer there.
                                pec_id[gi, gj, k] = 0
                    if prefetched:
                        continue
                    done_layers += 1
                    if progress is not None and progress(done_layers,
                                                         total_layers):
                        raise VoxelizationCancelled()
    finally:
        pool.close()

    # Covered -> open, and only if the geometry genuinely cuts something. A model
    # whose conductors land on cell edges produces 0/1 fractions everywhere: the
    # conformal path would then reduce to the staircase one anyway, so emitting
    # nothing keeps that run on the untouched (and faster) kernel.
    counts = {
        "dielectric_cells": int(np.count_nonzero(eps_x != float(bg_eps))),
        "pec_cells": int(np.count_nonzero(pec_mask)),
    }
    if lossy:
        counts["lossy_cells"] = int(np.count_nonzero(arrays["sigma_x"] > 0.0))
        counts["max_sigma"] = float(arrays["sigma_x"].max())
    if pec_id is not None:
        # Metal the electrostatic solve will ground for want of a name. Usually
        # zero -- every PEC body is named -- and worth reporting when it is not,
        # because a body whose cells were all overwritten by a later one looks
        # exactly like this.
        counts["unnamed_pec_cells"] = int(
            np.count_nonzero(pec_mask & (pec_id == 0)))
        counts["named_conductors"] = len(pec_names)
    if covered is not None:
        faces = [np.clip(1.0 - covered[key], 0.0, 1.0)
                 for key in CONFORMAL_KEYS[3:]]
        cut = sum(int(np.count_nonzero((f > 0.0) & (f < 1.0))) for f in faces)
        if cut:
            for key in CONFORMAL_KEYS:
                arrays[key] = np.clip(1.0 - covered[key], 0.0, 1.0)
            # Only now that the run is genuinely conformal do the conductor
            # cells' eps/mu/sigma become readable -- see _fill_pec_materials for
            # what goes wrong when they still carry the background. Gated on the
            # same condition as the fractions themselves, so a staircase
            # materials.npz stays bit-identical (V2).
            mat_keys = ["eps_x", "eps_y", "eps_z", "mu_x", "mu_y", "mu_z"]
            if lossy:
                mat_keys.extend(SIGMA_KEYS)
            counts["pec_material_cells"] = _fill_pec_materials(
                arrays, pec_mask, mat_keys)
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
        # ``{part label: pec_id value}``; empty unless conductor_names was given.
        # Metadata, not bulk, so it travels in job.json rather than the npz.
        "pec_names": pec_names,
    }


def _report_conformal(active, counts, threshold):
    """Console report after a voxelisation that asked for conformal PEC.

    Says whether it took effect and, when it did, how well the mesh resolves the
    conductor surfaces.

    ``min_open_face`` is a **geometry** diagnostic, not a stability one, and this
    used to claim otherwise. It cannot predict a divergence, because every face
    below *threshold* is clamped to exactly ``threshold * A_full`` -- so its own
    value never reaches the H update, and the measured record bears that out
    (0.0044 stable, 0.0015 diverging, 0.0073 stable again on the same geometry
    at three cell sizes). Stability is measured by the solver instead: it probes
    the assembled scheme when the ``Simulation`` is built, raises the threshold
    if it has to, and records what actually ran in ``summary.json`` alongside
    ``conformal_area_threshold_requested`` when the two differ (S7 in
    CONFORMAL_PEC_PLAN.md; the runner echoes the raise to the report view).

    What the number *does* say is how much clamping the run will carry, which is
    an accuracy cost concentrated in H near the conductor -- so that is what the
    warning is about now.
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
            "Wavesim: the mesh barely resolves the smallest cut ({:.4f} against "
            "a clamp threshold of {:.2f}), so those faces are clamped by more "
            "than 10x and the H field near the conductor there is "
            "correspondingly weak. Moving cell boundaries onto the conductor's "
            "tangents is what removes the slivers; the run's stability is "
            "measured by the solver at setup, and the threshold it actually "
            "used is in the run summary.\n".format(smallest, threshold)
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
    bg_sigma = (materials_mod.material_sigma(bg_mat)
                if bg_mat is not None else 0.0)
    # Lossy dielectrics: warn now, before the (slow) voxelisation, about any
    # material whose conductivity has outrun the timestep. The solver warns too,
    # but by then the run is already going -- and its symptom is a plausible
    # decaying-looking field rather than a failure.
    for message in materials_mod.loss_warnings(sim, dt=domain_mod.cfl_dt(dom)):
        FreeCAD.Console.PrintWarning("Wavesim: " + message + "\n")
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
    # Electrostatics: the solve addresses conductors by name, so every PEC body
    # is labelled. Only in that mode -- a full-wave materials.npz gains nothing
    # from an identity the FDTD update cannot read.
    from wavesim_gui.commands import is_electrostatic

    electrostatic = is_electrostatic(sim)
    conductor_names = None
    if electrostatic:
        conductor_names = {
            str(body.Name): name
            for body, name, _volts in materials_mod.conductors(sim)
        }
    # Grow the grid to include every source position and snapshot slice, so an
    # input outside the material bounds (or in the PML) still lands inside it.
    vox = voxelize_materials(
        materials, cell_size_m,
        spacing_lo_m=spacing_lo, spacing_hi_m=spacing_hi,
        pad_lo=pad_lo, pad_hi=pad_hi,
        extra_points_mm=source_points_mm(sim),
        extra_axis_offsets=snapshot_axis_offsets(sim),
        bg_eps=bg_eps, bg_mu=bg_mu, bg_pec=bg_pec, bg_sigma=bg_sigma,
        nodes_m=nodes_m, subpixel=subpixel, conformal=want_conformal,
        conductor_names=conductor_names,
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
    # Same rule as ``conformal``: what the arrays actually say, not what was
    # asked for. The runner reads this *before* it opens materials.npz, because
    # the backend choice (lossy is CPU-only) has to be made first.
    lossy = all(key in vox["arrays"] for key in SIGMA_KEYS)
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
    # One entry per *drive row*, not per port object: a multi-conductor port
    # energizes several modes on one face and the solver superposes the sheets
    # (see ``modal_port.modal_port_specs``). A port with no conductor table still
    # yields exactly one entry, unchanged.
    modal_ports = [spec for p in modal_wave_objs
                   for spec in modal_mod.modal_port_specs(p, origin_m)]

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
        # Whether materials.npz carries conductivity. Read by the runner before
        # the arrays are loaded, because a lossy grid is CPU-only (the CUDA E
        # update has no Ca/Cb pair) and the backend decides the grid dtype.
        "lossy": lossy,
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
    if electrostatic:
        spec.update(_electrostatic_spec(sim, dom, vox, monitors))
    return spec, vox["arrays"]


def _electrostatic_spec(sim, dom, vox, monitors):
    """The job.json keys that turn a run into an electrostatic solve.

    Layered on top of the ordinary spec rather than replacing it: the grid,
    materials, conformal geometry and snapshot planes are the same objects
    meaning the same things, and only what is *done* with them changes. The
    runner ignores ``source``/``modal_ports``/``spice_ports`` and the time-series
    monitors in this mode, so they are left in the job rather than stripped --
    switching the mode back must not have quietly discarded them.
    """
    from wavesim_gui import materials as materials_mod
    from wavesim_gui import domain as domain_mod
    from wavesim_gui.commands import MODE_ELECTROSTATIC, extract_capacitance

    potentials = materials_mod.conductor_potentials(sim)
    names = list(vox.get("pec_names") or {})
    # Extraction energises one conductor at a time, so it needs at least two:
    # a lone conductor in a box has a capacitance only to the box, which is a
    # legitimate answer but not a matrix, and the solver refuses an all-zero one.
    capacitance = bool(extract_capacitance(sim)) and len(names) >= 2

    boundary = domain_mod.electrostatic_boundary(dom)
    for message in _electrostatic_warnings(sim, dom, potentials, boundary,
                                           capacitance):
        FreeCAD.Console.PrintWarning("Wavesim: " + message + "\n")

    return {
        "mode": MODE_ELECTROSTATIC,
        # No time loop, so no step count for a progress bar to divide: the run
        # dialog runs indeterminate and is driven by the runner's STATUS lines.
        "steps": 0,
        "electrostatic": {
            "potentials": potentials,
            "boundary": boundary,
            "capacitance": capacitance,
            # Which planes to save, taken from the snapshot monitors: same
            # plane/offset the full-wave path uses, minus the cadence.
            "slices": [
                {"name": s["name"], "field": s["field"],
                 "normal": s["normal"], "position": s["position"]}
                for s in monitors.get("snapshots", [])
            ],
        },
        # The part labels behind ``pec_id`` in materials.npz. Metadata, so it
        # rides in the job rather than the array file.
        "pec_names": dict(vox.get("pec_names") or {}),
    }


def _electrostatic_warnings(sim, dom, potentials, boundary, capacitance):
    """Console warnings worth giving before an electrostatic run starts.

    Cheap checks the user can act on now; the solver makes the authoritative
    ones (two named parts that turn out to be one conductor, a singular problem)
    once it has the voxelised geometry in front of it.
    """
    from wavesim_gui import source as source_mod
    from wavesim_gui import modal_port as modal_mod
    from wavesim_gui import monitors as monitors_mod
    from wavesim_gui import materials as materials_mod
    from wavesim_gui import domain as domain_mod

    out = []
    # A conductor sitting on a Ground face is shorted to it. Harmless while that
    # conductor is itself at 0 V -- which is why the potential solve can succeed
    # and the extraction then fail, one solve later, when the same conductor is
    # driven to 1 V. Said here rather than left to surface halfway through.
    if capacitance:
        out.extend(_grounded_face_shorts(dom, sim, boundary))
    if not potentials:
        out.append(
            "electrostatic run with no PEC bodies: nothing holds a potential, "
            "so the field has no source. Assign bodies to a PEC material.")
    elif len(set(potentials.values())) == 1:
        out.append(
            "every conductor is at {:g} V, so the field is uniformly that "
            "potential and every charge is zero. Set at least two different "
            "potentials (a driven conductor and a ground).".format(
                next(iter(potentials.values()))))
    ignored = []
    if source_mod.find_sources(sim):
        ignored.append("point sources")
    if modal_mod.find_modal_ports(sim):
        ignored.append("modal ports")
    if monitors_mod.find_probes(sim):
        ignored.append("probes")
    if ignored:
        out.append(
            "electrostatic mode ignores {} — there is no time axis for them to "
            "act on.".format(", ".join(ignored)))
    return out


def _grounded_face_shorts(dom, sim, boundary):
    """Warn about each conductor that reaches a Ground domain face.

    Extraction drives every conductor to 1 V in turn, so a conductor touching a
    face held at 0 V has no solution then even though the potential solve before
    it was fine. The check is against the **grid** bounds (the node arrays, which
    include the PML pad) rather than the drawn domain box, because that is where
    the boundary condition is actually applied.
    """
    from wavesim_gui import materials as materials_mod
    from wavesim_gui import domain as domain_mod

    try:
        nodes = domain_mod.node_coords_m(dom)
    except Exception:
        return []
    if not all(len(a) >= 2 for a in nodes):
        return []
    lo = [float(a[0]) * _MM_PER_M for a in nodes]
    hi = [float(a[-1]) * _MM_PER_M for a in nodes]
    # Half the smallest cell: a body reaching within that of the wall lands on
    # the boundary node once voxelised.
    tol = 0.5 * min(min(float(a[i + 1] - a[i]) for i in range(len(a) - 1))
                    for a in nodes) * _MM_PER_M

    faces = (("xmin", 0, "XMin", lo), ("xmax", 0, "XMax", hi),
             ("ymin", 1, "YMin", lo), ("ymax", 1, "YMax", hi),
             ("zmin", 2, "ZMin", lo), ("zmax", 2, "ZMax", hi))
    out = []
    for body, name, _volts in materials_mod.conductors(sim):
        shape = getattr(body, "Shape", None)
        if shape is None:
            continue
        bb = shape.BoundBox
        touching = [key for key, axis, attr, bound in faces
                    if boundary.get(key) == "ground"
                    and abs(getattr(bb, attr) - bound[axis]) <= tol]
        if touching:
            out.append(
                "conductor {!r} reaches the grounded domain face(s) {}, so "
                "extracting its capacitance shorts it to the wall and the run "
                "will fail there. Set those faces to Symmetry (right for a "
                "shielded structure — the mutual capacitances stay exact), add "
                "background spacing, or clear Extract capacitance."
                .format(name, ", ".join(touching)))
    return out
