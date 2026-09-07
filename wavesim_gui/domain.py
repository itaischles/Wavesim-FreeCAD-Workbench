# -*- coding: utf-8 -*-
"""Simulation domain (grid + boundaries) for the Wavesim workbench.

The *Domain* is the single object that defines the FDTD grid: a singleton scripted
DocumentObject created automatically when a Simulation is created. It unifies what
were previously separate "Grid" and "Domain" concepts. It holds:

* the cell sizes ``Dx``/``Dy``/``Dz`` and the derived cell counts ``Nx``/``Ny``/``Nz``;
* a per-face ``Spacing`` gap of background medium around the geometry;
* per-face boundary conditions (PML or PEC) and the PML cell thickness.

The domain box auto-sizes to bound every material-assigned body plus the spacing;
it starts empty (no geometry) and grows/shrinks as bodies are assigned (the
material commands notify it via :func:`notify_materials_changed`).

**The PML sits inside the domain box**, not around it: the outermost
``PMLThickness`` cells of each absorbing face *are* the absorber, and the grid
spans exactly the box the user drew. Two consequences, both of them the point:
the drawn box is the true extent of what is solved (nothing is hidden outside
it), and **geometry can be carried into the absorber** -- set a face's
``Spacing`` to 0 and a body runs out through the PML, which is how a waveguide,
a substrate or a trace is taken to infinity instead of ending in a physical
discontinuity at the wall. The cross-section must not *change* through the
shell for that to be reflectionless; ``voxelize.pml_shell_warnings`` checks it
and warns before the run.

Rendering
---------
The domain draws as two *wireframe* boxes (edges only, no fill, so neither
obscures the other or the geometry): the domain box and, within it, the box
bounding the PML-free interior, in two colours -- so the frame between them is
the absorber. Alongside them are three fully-transparent *cell grids* (thin
lines spaced ``Dx``/``Dy``/``Dz``) on the domain's three min faces, so the
meshing resolution is visible; the segments of those lines that lie in the
absorber are drawn in the PML box's own colour, which is what makes the
absorber read as a *region of the mesh* rather than an annotation. All of these
are drawn by one object, so the single "eye" visibility toggle next to Domain
shows/hides them together.

:func:`domain_grid_params` is the single source of truth mapping the per-face
settings to the per-side PML depth (cells, taken from inside the box), the
PML ``faces`` tuple and the PEC
``faces`` tuple that the voxeliser and runner consume.

Units: FreeCAD geometry/properties are in millimetres; the solver works in
metres. ``Dx``/``Dy``/``Dz`` and the ``Spacing*`` gaps are lengths (mm internally);
:func:`cell_sizes_m` / :func:`domain_grid_params` are the conversion points.
"""

import bisect
import math
import os

import FreeCAD


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_RESOURCES_DIR = os.path.join(_WB_DIR, "Resources")
# The 24x24 SVG icon set (grouped by colour: blue setup, amber sources,
# teal monitors). The retired PNGs are still in Resources/ alongside it.
_ICONS_DIR = os.path.join(_RESOURCES_DIR, "icons")
_DOMAIN_ICON = os.path.join(_ICONS_DIR, "domain.svg")

_TYPE_PROP = "WavesimType"
_DOMAIN_TYPE = "Domain"

_MM_PER_M = 1000.0

# The six domain faces in the solver's naming: '<axis><0|1>', low/high index.
_FACES = ("x0", "x1", "y0", "y1", "z0", "z1")

# Per-face boundary-condition property names, in face order.
_FACE_PROPS = (
    ("x0", "BoundaryXMin", "Boundary condition on the low-x face"),
    ("x1", "BoundaryXMax", "Boundary condition on the high-x face"),
    ("y0", "BoundaryYMin", "Boundary condition on the low-y face"),
    ("y1", "BoundaryYMax", "Boundary condition on the high-y face"),
    ("z0", "BoundaryZMin", "Boundary condition on the low-z face"),
    ("z1", "BoundaryZMax", "Boundary condition on the high-z face"),
)

_BC_CHOICES = ["PML", "PEC"]

# Electrostatic per-face boundary conditions. A separate property set rather
# than a reinterpretation of the PML/PEC one: neither of those words means
# anything to a static field (an absorber has no curl to absorb at DC), and
# overloading them would make a document's boundary depend on which solver mode
# happened to be selected when it was read.
_ES_FACE_PROPS = (
    ("x0", "ESBoundaryXMin", "Electrostatic boundary on the low-x face"),
    ("x1", "ESBoundaryXMax", "Electrostatic boundary on the high-x face"),
    ("y0", "ESBoundaryYMin", "Electrostatic boundary on the low-y face"),
    ("y1", "ESBoundaryYMax", "Electrostatic boundary on the high-y face"),
    ("z0", "ESBoundaryZMin", "Electrostatic boundary on the low-z face"),
    ("z1", "ESBoundaryZMax", "Electrostatic boundary on the high-z face"),
)

# 'Ground' is Dirichlet phi = 0 -- the box becomes the reference conductor, which
# is what gives every conductor a capacitance to ground. 'Symmetry' is Neumann
# (no normal field), i.e. a mirror plane, which is the reason to halve a model.
_ES_BC_CHOICES = ["Ground", "Symmetry"]
_ES_BC_TOKENS = {"Ground": "ground", "Symmetry": "neumann"}

# Face name -> the solver's electrostatic boundary key.
_ES_FACE_KEYS = {
    "x0": "xmin", "x1": "xmax",
    "y0": "ymin", "y1": "ymax",
    "z0": "zmin", "z1": "zmax",
}

# Shown (read-only) in the Domain panel for a face whose boundary is not the
# user's to set. Neither is a stored ``App::PropertyEnumeration`` value -- the
# job builder overrides such a face through ``domain_grid_params``, so an
# editable combo would only look like it applied.
#
# ``_PML_PORT_BC_LABEL`` -- a Gaussian beam or a SPICE-TEM port launches from an
# interior plane one PML-depth in, so its face must absorb: forced to PML.
# ``_MODAL_BC_LABEL`` -- a Modal Port *is* the boundary. It sets the ghost
# tangential H on the face itself, so the face carries no PML and no PEC (and no
# background spacing, or the port plane would cut empty medium).
_PML_PORT_BC_LABEL = "Port / beam (absorbing)"
_MODAL_BC_LABEL = "Modal port"

# Sentinel written into the per-face ``bc`` map by ``domain_grid_params`` for a
# modal-port face: neither PML nor PEC, so it lands in neither face list and
# contributes no absorber padding.
_BC_MODAL = "MODAL"

# Per-face background-spacing property names, in face order (mirrors _FACE_PROPS).
_SPACING_PROPS = (
    ("x0", "SpacingXMin", "Background gap added outside the low-x material bound"),
    ("x1", "SpacingXMax", "Background gap added outside the high-x material bound"),
    ("y0", "SpacingYMin", "Background gap added outside the low-y material bound"),
    ("y1", "SpacingYMax", "Background gap added outside the high-y material bound"),
    ("z0", "SpacingZMin", "Background gap added outside the low-z material bound"),
    ("z1", "SpacingZMax", "Background gap added outside the high-z material bound"),
)

# Pre-2026-07 documents carried one scalar gap for all six faces; migrated on
# restore (see DomainObject.onDocumentRestored).
_LEGACY_SPACING_PROP = "Spacing"

# Task-panel row labels for the six faces (shared by the spacing and BC rows).
_FACE_LABELS = {
    "x0": "X min (x0):", "x1": "X max (x1):",
    "y0": "Y min (y0):", "y1": "Y max (y1):",
    "z0": "Z min (z0):", "z1": "Z max (z1):",
}

# Wireframe edge colours: a cool domain box and a warmer PML box.
_DOMAIN_COLOR = (0.30, 0.55, 1.00)
_PML_COLOR = (1.00, 0.45, 0.10)

# Color of grid lines
_GRID_COLOR = (0.55, 0.55, 0.55)
_MAX_GRID_LINES = 400

# The snapped lines -- the ones the snapper forced onto a geometry feature (a
# body's bounding box, a cylinder's silhouette, a bevel's outline circle) rather
# than laid down to fill a gap. Drawn **dark** against the grey of the rest, so
# the mesh can be read as what it is: which lines the geometry asked for, and
# which merely tile the space between them. The
# separation is by value, not by hue -- the two absorber colours (_PML_COLOR,
# _PML_GRID_COLOR) are the only warm things in the mesh, and a coloured snap
# line competed with them. Capped separately from the thin lines so decimating
# a fine grid can never drop a feature line -- they are the few that matter.
_SNAP_COLOR = (0.25, 0.25, 0.25)
_SNAP_LINE_WIDTH = 1
_MAX_SNAP_LINES = 400

# The grid lines that fall in the absorber, drawn in the PML box's own colour so
# the user can see how much of the domain the absorber is eating and how far a
# body carried out through it actually runs. Same width and cap as the thin
# lines: it is the same mesh, only differently labelled.
_PML_GRID_COLOR = (0.85, 0.45, 0.20)


def _node_at(nodes, index, fallback):
    """``nodes[index]`` (world mm) when the array reaches that far, else *fallback*.

    The node arrays are the geometric source of truth for where a cell wall is,
    so the PML box is read off them rather than recomputed from the cell size --
    exact on a graded grid, where the shell's own width is the *coarse* target
    and not the ``Dx`` the interior may have refined past. The fallback covers
    the first recompute of a restored document, before the arrays are filled.
    """
    try:
        return float(nodes[index])
    except (IndexError, TypeError, ValueError):
        return float(fallback)


# --------------------------------------------------------------------------- #
# Document-object model
# --------------------------------------------------------------------------- #

class DomainObject:
    """``Proxy`` for the unified simulation Domain object.

    Properties:
        ``Dx`` / ``Dy`` / ``Dz`` -- cell sizes (editable).
        ``Nx`` / ``Ny`` / ``Nz`` -- derived cell counts (read-only).
        ``SpacingX/Y/Z Min/Max`` -- per-face background gap outside the material
                                    bounds.
        ``Background``           -- Material filling empty voxels (the default
                                    medium); vacuum when unset.
        ``PMLThickness``         -- PML depth in cells, on every PML face.
        ``BoundaryX/Y/Z Min/Max`` -- per-face boundary condition (PML | PEC).

    Hidden geometry properties (``DomainMin``/``Max`` -- the whole grid --  and
    ``PmlMin``/``Max`` -- the interior the absorber leaves, *inside* it) carry
    the box corners for the view provider; ``execute`` keeps them in sync with the
    material bounds, cell sizes and boundary settings.
    """

    def __init__(self, obj):
        self.Type = _DOMAIN_TYPE
        obj.Proxy = self

        if not hasattr(obj, _TYPE_PROP):
            obj.addProperty(
                "App::PropertyString", _TYPE_PROP, "Wavesim",
                "Marks this object as the Wavesim simulation domain",
            )
            setattr(obj, _TYPE_PROP, _DOMAIN_TYPE)
            obj.setEditorMode(_TYPE_PROP, 1)  # read-only identity marker

        for name, doc in (
            ("Dx", "Cell size along x"),
            ("Dy", "Cell size along y"),
            ("Dz", "Cell size along z"),
        ):
            if not hasattr(obj, name):
                obj.addProperty("App::PropertyLength", name, "Grid", doc)
                setattr(obj, name, "1 mm")

        for name, doc in (
            ("Nx", "Derived cell count along x (from geometry + boundaries)"),
            ("Ny", "Derived cell count along y (from geometry + boundaries)"),
            ("Nz", "Derived cell count along z (from geometry + boundaries)"),
        ):
            if not hasattr(obj, name):
                obj.addProperty("App::PropertyInteger", name, "Grid", doc)
                obj.setEditorMode(name, 1)  # read-only (derived)

        if not hasattr(obj, "CellsPerWavelength"):
            obj.addProperty(
                "App::PropertyInteger", "CellsPerWavelength", "Grid",
                "Cells resolving one wavelength at the simulation's max "
                "frequency; used by the 'Default from max frequency' cell size",
            )
            obj.CellsPerWavelength = 20
        _ensure_feature_prop(obj)
        if not hasattr(obj, "UseNonuniformGrid"):
            obj.addProperty(
                "App::PropertyBool", "UseNonuniformGrid", "Grid",
                "Build a graded non-uniform rectilinear grid (snapper) instead "
                "of a uniform Dx/Dy/Dz grid",
            )
            obj.UseNonuniformGrid = True
        if not hasattr(obj, "MaxGradingRatio"):
            obj.addProperty(
                "App::PropertyFloat", "MaxGradingRatio", "Grid",
                "Largest size ratio between adjacent cells the non-uniform "
                "snapper may use when grading from a fine feature out to the "
                "coarse interior (solver guidance ~1.5-2x)",
            )
            obj.MaxGradingRatio = 1.5
        if not hasattr(obj, "MinCellSize"):
            obj.addProperty(
                "App::PropertyLength", "MinCellSize", "Grid",
                "Smallest cell the non-uniform snapper may create; feature "
                "lines closer than this are merged and graded fill is clamped "
                "to it, so automatic snapping cannot produce an extremely fine "
                "mesh (0 mm = no limit)",
            )
            obj.MinCellSize = "0 mm"
        _ensure_curvature_prop(obj)

        _ensure_grid_plane_props(obj)
        _ensure_snap_props(obj)

        # Per-axis node-coordinate arrays (world mm) spanning the grid -- the
        # outermost of them are the absorber's own cells, the PML being inside
        # the box. The geometric source of truth every consumer derives from;
        # ``execute`` populates them: uniform ticks when UseNonuniformGrid is
        # off, the snapper's lines when on.
        for name in ("NodesX", "NodesY", "NodesZ"):
            if not hasattr(obj, name):
                obj.addProperty(
                    "App::PropertyFloatList", name, "Grid",
                    "Grid node coordinates along {} (world mm; maintained "
                    "internally)".format(name[-1].lower()),
                )
                obj.setEditorMode(name, 2)  # hidden
        for _face, prop, doc in _SPACING_PROPS:
            if not hasattr(obj, prop):
                obj.addProperty("App::PropertyLength", prop, "Domain", doc)
                setattr(obj, prop, "0 mm")
        if not hasattr(obj, "Background"):
            obj.addProperty(
                "App::PropertyLink", "Background", "Domain",
                "Material filling every empty voxel (the background medium); "
                "vacuum when unset. Pick any Material under the simulation.",
            )
        if not hasattr(obj, "PMLThickness"):
            obj.addProperty(
                "App::PropertyInteger", "PMLThickness", "Boundary",
                "PML absorbing-layer depth, in grid cells, taken from *inside* "
                "each PML face (the domain box is the whole grid)",
            )
            obj.PMLThickness = 8

        for _face, prop, doc in _FACE_PROPS:
            if not hasattr(obj, prop):
                obj.addProperty("App::PropertyEnumeration", prop, "Boundary", doc)
                setattr(obj, prop, _BC_CHOICES)
                setattr(obj, prop, "PML")

        _ensure_es_bc_props(obj)

        # Geometry for the view provider (hidden corners, in mm world coords).
        for name in ("DomainMin", "DomainMax", "PmlMin", "PmlMax"):
            if not hasattr(obj, name):
                obj.addProperty("App::PropertyVector", name, "Box", "")
                obj.setEditorMode(name, 2)  # hidden

        # Bodies the domain tracks, so geometry edits trigger a recompute.
        if not hasattr(obj, "TrackedBodies"):
            obj.addProperty(
                "App::PropertyLinkList", "TrackedBodies", "Box",
                "Material bodies the domain auto-sizes to (maintained internally)",
            )
            obj.setEditorMode("TrackedBodies", 2)  # hidden

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _DOMAIN_TYPE)
        self._migrate_spacing(obj)
        _ensure_es_bc_props(obj)
        _ensure_grid_plane_props(obj)
        _ensure_curvature_prop(obj)
        _ensure_feature_prop(obj)
        _ensure_snap_props(obj)

    @staticmethod
    def _migrate_spacing(obj):
        """Ensure the six per-face spacings exist, carrying a legacy value over.

        Documents saved before the gap became per-face hold one scalar
        ``Spacing`` length; copy it onto every face (which is exactly what it
        meant) and drop the old property, so the geometry of an existing model is
        unchanged. Idempotent: a no-op once the six properties are in place.
        """
        legacy = getattr(obj, _LEGACY_SPACING_PROP, None)
        for _face, prop, doc in _SPACING_PROPS:
            if not hasattr(obj, prop):
                obj.addProperty("App::PropertyLength", prop, "Domain", doc)
                setattr(obj, prop, "0 mm")
            if legacy is not None:
                setattr(obj, prop, "{} mm".format(legacy.Value))
        if legacy is not None:
            obj.removeProperty(_LEGACY_SPACING_PROP)

    def execute(self, obj):
        """Resize the boxes and derived counts from the geometry + settings."""
        from wavesim_gui.commands import active_simulation
        from wavesim_gui import materials as materials_mod
        from wavesim_gui import voxelize as vox

        sim = active_simulation(obj.Document)
        # Combined bounds of the material geometry *and* every source position, so
        # the domain auto-grows to contain a source placed outside the geometry.
        bbox = vox.combined_bbox_mm(sim, materials_mod.find_materials(sim)) if sim else None

        zero = FreeCAD.Vector(0, 0, 0)
        if bbox is None:
            # No geometry yet: empty boxes, zero counts, no node arrays.
            obj.DomainMin = obj.DomainMax = zero
            obj.PmlMin = obj.PmlMax = zero
            obj.Nx = obj.Ny = obj.Nz = 0
            obj.NodesX = obj.NodesY = obj.NodesZ = []
            _set_snaps(obj, None)
            return

        # Face-launching sources override the per-face setting here too (not just
        # at run) so the drawn box and the node arrays carry the same padding and
        # spacing the run's boundary assumes -- keeping the non-uniform grid
        # consistent. Beams / SPICE-TEM ports force PML; modal ports remove the
        # absorber and the background gap on their face entirely.
        force_faces = pml_port_faces(sim)
        modal_faces = modal_port_faces(sim)
        # An electrostatic run has no absorber at all: no drawn PML box, and --
        # the part that moves numbers -- no reserved shell of uniform coarse
        # cells eating the feature snaps at each face.
        from wavesim_gui.commands import is_electrostatic

        es = is_electrostatic(sim)
        params = domain_grid_params(
            obj, force_pml_faces=force_faces, modal_faces=modal_faces,
            electrostatic=es)
        sp_lo = tuple(s * _MM_PER_M for s in params["spacing_lo"])
        sp_hi = tuple(s * _MM_PER_M for s in params["spacing_hi"])
        pad_lo, pad_hi = params["pad_lo"], params["pad_hi"]
        dx, dy, dz = (c * _MM_PER_M for c in cell_sizes_m(obj))

        dmin = FreeCAD.Vector(bbox.XMin - sp_lo[0], bbox.YMin - sp_lo[1],
                              bbox.ZMin - sp_lo[2])
        dmax = FreeCAD.Vector(bbox.XMax + sp_hi[0], bbox.YMax + sp_hi[1],
                              bbox.ZMax + sp_hi[2])
        obj.DomainMin, obj.DomainMax = dmin, dmax

        # Per-axis node-coordinate arrays (world mm) spanning the grid.
        # With UseNonuniformGrid on, the snapper places lines on the geometry's
        # features and grades the spacing; otherwise these are uniform ticks
        # whose extent matches the voxeliser's own ``_grid_extent`` for the same
        # geometry, so the preview and the run agree.
        nodes = None
        snaps = ([], [], [])
        if getattr(obj, "UseNonuniformGrid", False):
            from wavesim_gui import gridbuild
            try:
                nodes = gridbuild.build_domain_nodes(
                    sim, obj, force_pml_faces=force_faces,
                    modal_faces=modal_faces, snaps_out=snaps,
                )
            except Exception as exc:  # never let meshing break the recompute
                FreeCAD.Console.PrintWarning(
                    "Wavesim: non-uniform grid build failed ({}); "
                    "falling back to a uniform grid.\n".format(exc)
                )
                nodes = None

        if nodes is not None:
            obj.NodesX = list(nodes[0])
            obj.NodesY = list(nodes[1])
            obj.NodesZ = list(nodes[2])
            obj.Nx = len(nodes[0]) - 1
            obj.Ny = len(nodes[1]) - 1
            obj.Nz = len(nodes[2]) - 1
            _set_snaps(obj, snaps)
        else:
            (nx, ny, nz), (ox, oy, oz) = vox._grid_extent(
                bbox, (dx, dy, dz), sp_lo, sp_hi, pad_lo, pad_hi
            )
            obj.NodesX = [ox + i * dx for i in range(nx + 1)]
            obj.NodesY = [oy + i * dy for i in range(ny + 1)]
            obj.NodesZ = [oz + i * dz for i in range(nz + 1)]
            obj.Nx, obj.Ny, obj.Nz = nx, ny, nz
            # A uniform grid snaps to nothing, so every line is drawn thin.
            _set_snaps(obj, None)

        # Inner edge of the absorber. The PML sits *inside* the domain box -- the
        # outermost ``pad_lo``/``pad_hi`` cells of the grid *are* the PML -- so
        # this box is drawn within the domain box rather than around it, and it
        # is read off the node arrays so it lands exactly on a cell wall on a
        # graded grid as well as a uniform one.
        if params["pml_faces"]:
            nx_, ny_, nz_ = (list(obj.NodesX), list(obj.NodesY), list(obj.NodesZ))
            pml_min = FreeCAD.Vector(
                _node_at(nx_, pad_lo[0], dmin.x + pad_lo[0] * dx),
                _node_at(ny_, pad_lo[1], dmin.y + pad_lo[1] * dy),
                _node_at(nz_, pad_lo[2], dmin.z + pad_lo[2] * dz),
            )
            pml_max = FreeCAD.Vector(
                _node_at(nx_, -1 - pad_hi[0], dmax.x - pad_hi[0] * dx),
                _node_at(ny_, -1 - pad_hi[1], dmax.y - pad_hi[1] * dy),
                _node_at(nz_, -1 - pad_hi[2], dmax.z - pad_hi[2] * dz),
            )
            # An axis with no room left between its two shells has no interior
            # to bound: draw no inner box rather than an inside-out one.
            if any(getattr(pml_max, a) <= getattr(pml_min, a)
                   for a in ("x", "y", "z")):
                obj.PmlMin = obj.PmlMax = zero
            else:
                obj.PmlMin, obj.PmlMax = pml_min, pml_max
        else:
            obj.PmlMin = obj.PmlMax = zero  # no PML -> draw no inner box

        # First recompute with real bounds parks the grid planes on the min
        # faces -- where they used to be nailed. Later recomputes leave them
        # wherever the user put them (see place_grid_planes).
        place_grid_planes(obj)

    def dumps(self):
        return {"Type": getattr(self, "Type", _DOMAIN_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _DOMAIN_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


# --------------------------------------------------------------------------- #
# Lookup, conversions, and the grid-params single source of truth
# --------------------------------------------------------------------------- #

def is_domain(obj):
    """Return True if *obj* is the Wavesim Domain object."""
    return getattr(obj, _TYPE_PROP, None) == _DOMAIN_TYPE


def find_domain(sim):
    """Return the Domain object under the Simulation container *sim*, or None."""
    if sim is None:
        return None
    for child in sim.Group:
        if is_domain(child):
            return child
    return None


def background_material(domain):
    """Return the Domain's background (empty-voxel) Material, or None for vacuum."""
    if domain is None:
        return None
    return getattr(domain, "Background", None)


def modal_port_faces(sim):
    """Domain faces (``'x0'``..``'z1'``) terminated by a **Modal Port**.

    A modal port is an impedance-sheet *boundary*: it writes the ghost tangential
    H on the face each step, launching the mode inward and absorbing whatever
    returns, with no reflection and -- unlike PML -- no DC error. So the face
    needs **no PML shell and no PEC wall**; it also needs **no background spacing**,
    because the port plane has to cut the real cross-section and a gap of
    background medium would leave it nothing to solve. All three are applied by
    :func:`domain_grid_params`, everywhere the grid is built (the drawn box, the
    node arrays and the run), so the three stay in step.

    Only *waveform*-driven ports qualify. A SPICE-driven one is still a lumped
    ``SpicePort`` on an interior plane and belongs to :func:`pml_port_faces`.
    Lazy imports avoid a circular import with the source modules; empty on failure.
    """
    if sim is None:
        return []
    try:
        from wavesim_gui import modal_port as modal_mod
        return [str(p.Face) for p in modal_mod.find_modal_ports(sim)
                if modal_mod.excitation_mode(p) == modal_mod.MODE_WAVEFORM]
    except Exception:
        return []


def pml_port_faces(sim):
    """Domain faces launching a source that must absorb its own launch: forced PML.

    A Gaussian beam and a SPICE-TEM port both drive an *interior* plane placed one
    PML-depth inside the face, so that face has to be an absorber -- otherwise it
    traps the backward/reflected wave and, on a non-uniform grid, desyncs the node
    arrays (no absorber shell) from the forced boundary (crash). Applied everywhere the
    grid is built, regardless of the Domain's per-face setting.

    A **modal port** face is *not* in this list: it terminates itself, see
    :func:`modal_port_faces`. Lazy imports avoid a circular import; empty on
    failure.
    """
    if sim is None:
        return []
    faces = []
    try:
        from wavesim_gui import spice_port as spice_mod
        faces += [str(p.Face) for p in spice_mod.find_spice_tem_ports(sim)]
    except Exception:
        pass
    try:
        from wavesim_gui import modal_port as modal_mod
        faces += [str(p.Face) for p in modal_mod.find_modal_ports(sim)
                  if modal_mod.excitation_mode(p) == modal_mod.MODE_SPICE]
    except Exception:
        pass
    try:
        from wavesim_gui import gaussian_beam as beam_mod
        faces += [str(b.Face) for b in beam_mod.find_gaussian_beams(sim)]
    except Exception:
        pass
    return faces


def material_is_vacuum(mat):
    """True if *mat* is a plain vacuum medium (eps == mu == 1, not PEC).

    Used by the Domain panel so it does not offer a synthetic "Vacuum" background
    entry alongside a real Vacuum material (every new simulation seeds one) --
    that material *is* the vacuum choice.
    """
    if mat is None or bool(getattr(mat, "Pec", False)):
        return False
    return (abs(float(getattr(mat, "Eps", 1.0)) - 1.0) < 1.0e-9
            and abs(float(getattr(mat, "Mu", 1.0)) - 1.0) < 1.0e-9)


def cell_sizes_m(obj):
    """Return the Domain's ``(dx, dy, dz)`` cell sizes in metres."""
    return (
        float(obj.Dx.Value) / _MM_PER_M,
        float(obj.Dy.Value) / _MM_PER_M,
        float(obj.Dz.Value) / _MM_PER_M,
    )


def min_cell_size_m(obj):
    """Return the Domain's minimum snapper cell size in metres (0 = no limit).

    Only meaningful with ``UseNonuniformGrid`` on; the snapper clamps its finest
    cells to this so automatic feature-snapping cannot produce an extremely fine
    mesh. Falls back to 0 (no limit) for older documents lacking the property.
    """
    q = getattr(obj, "MinCellSize", None)
    if q is None:
        return 0.0
    return float(q.Value) / _MM_PER_M


def curvature_refinement(obj):
    """Return the Domain's grazing-surface refinement factor (1 = off).

    The snapper asks for a fine cell where a curved face is tangent to a grid
    plane; the size it wants scales inversely with curvature and so is unbounded
    for a gently curved body, which is what this caps -- the finest such cell is
    ``coarse / factor``. Defaults to 1 (off) for documents predating the
    property, so an existing model's mesh is unchanged until it is turned on.
    """
    return float(getattr(obj, "CurvatureRefinement", 1.0) or 1.0)


def node_coords_mm(obj):
    """Return the Domain's ``(NodesX, NodesY, NodesZ)`` arrays (world mm lists).

    These are the per-axis grid node coordinates spanning the whole grid, which
    is the domain box itself (the absorber's cells are its outermost ones) --
    the geometric source of truth ``execute`` maintains. Empty lists
    when the domain has no sized geometry yet.
    """
    return (
        list(getattr(obj, "NodesX", []) or []),
        list(getattr(obj, "NodesY", []) or []),
        list(getattr(obj, "NodesZ", []) or []),
    )


def node_coords_m(obj):
    """Return the Domain's node-coordinate arrays in metres (world frame)."""
    return tuple(
        [c / _MM_PER_M for c in axis] for axis in node_coords_mm(obj)
    )


def min_spacings_m(obj):
    """Return the minimum cell width (metres) on each axis from the node arrays.

    Falls back to the scalar ``Dx``/``Dy``/``Dz`` for an axis whose node array
    is not populated yet. On a uniform grid the minimum equals the cell size, so
    this reproduces the scalar spacing; on a graded grid it is the finest cell,
    which sets the CFL step for the whole domain (mirroring the solver's
    ``_cfl_dt`` over ``min(diff(nodes))``).
    """
    nodes = node_coords_mm(obj)
    out = []
    for axis, fallback in zip(nodes, cell_sizes_m(obj)):
        if len(axis) >= 2:
            widths = [axis[i + 1] - axis[i] for i in range(len(axis) - 1)]
            out.append(min(widths) / _MM_PER_M)
        else:
            out.append(fallback)
    return tuple(out)


def wavelength_cell_size_m(sim, eps_r=1.0, mu_r=1.0, domain=None,
                           cells_per_wavelength=None):
    """Cell size (metres) resolving ``N_lambda`` cells per wavelength in a medium.

    ``dx = c0 / (fmax * N_lambda * sqrt(eps_r * mu_r))`` -- the shortest
    wavelength at the max frequency in a medium of relative constants *eps_r* /
    *mu_r*, resolved with ``N_lambda`` cells. ``N_lambda`` is the Domain's
    ``CellsPerWavelength`` (overridable via *cells_per_wavelength*, so a task
    panel can preview an uncommitted value).

    Returns ``None`` when the max frequency is not positive (no sensible size).
    """
    from wavesim_gui.commands import max_frequency_hz

    fmax = max_frequency_hz(sim)
    if fmax <= 0.0:
        return None

    if cells_per_wavelength is not None:
        n_lambda = int(cells_per_wavelength)
    else:
        if domain is None:
            domain = find_domain(sim)
        n_lambda = int(getattr(domain, "CellsPerWavelength", 20)) if domain else 20
    if n_lambda <= 0:
        n_lambda = 20

    n = math.sqrt(max(float(eps_r) * float(mu_r), 1.0e-12))
    return _C0 / (fmax * n_lambda * n)


def feature_cell_size_m(sim, domain=None, cells_per_feature=None):
    """Cell size (metres) resolving the model's thinnest feature *N* times.

    The electrostatic counterpart of :func:`wavelength_cell_size_m`, and the
    reason the two exist separately: ``div(eps grad phi) = 0`` has no wavelength
    and no frequency, so nothing about a medium sets the mesh. What does is the
    geometry -- phi varies fastest across the narrowest thing in the model -- so
    the size is ``min(feature span) / N``, where the span is each assigned
    body's thinnest bounding-box dimension.

    **PEC bodies count here**, unlike in the wavelength form that skips them: in
    an electrostatic run the conductors are the whole problem, and a thin trace
    is exactly the feature the mesh has to resolve.

    Returns ``None`` when no material-assigned body has a solid shape, so the
    caller leaves the cell size alone rather than guessing one.
    """
    from wavesim_gui import gridbuild
    from wavesim_gui import materials as materials_mod
    from wavesim_gui import voxelize as vox

    if cells_per_feature is None:
        if domain is None:
            domain = find_domain(sim)
        n = int(getattr(domain, "CellsPerFeature", 10) or 10) if domain else 10
    else:
        n = int(cells_per_feature)
    if n <= 0:
        n = 10

    smallest_mm = None
    for shape, _eps, _mu, _pec, _sigma, _body in vox._gather(
            materials_mod.find_materials(sim) if sim else []):
        bb = gridbuild._exact_bbox(shape)
        # A body flat on an axis (a shell, a zero-thickness pad) has no
        # thickness to resolve there, so that axis is skipped rather than
        # driving the size to zero; its in-plane extent is what it offers.
        spans = [s for s in (bb.XLength, bb.YLength, bb.ZLength) if s > 1.0e-9]
        if not spans:
            continue
        span = min(spans)
        smallest_mm = span if smallest_mm is None else min(smallest_mm, span)

    if smallest_mm is None:
        return None
    return (smallest_mm / _MM_PER_M) / n


def background_eps_mu(domain):
    """Return the Domain background medium's ``(eps_r, mu_r)`` (vacuum default)."""
    bg = background_material(domain)
    eps = float(getattr(bg, "Eps", 1.0)) if bg is not None else 1.0
    mu = float(getattr(bg, "Mu", 1.0)) if bg is not None else 1.0
    return eps, mu


def default_cell_size_m(sim, domain=None, cells_per_wavelength=None):
    """Suggested uniform cell size (metres) for *sim*'s max frequency.

    ``dx = c0 / (fmax * N_lambda * sqrt(eps_r,max * mu_r,max))`` where
    ``eps_r,max`` / ``mu_r,max`` are the largest relative constants among the
    simulation's materials (PEC bodies are skipped; each falls back to 1.0). This
    resolves the shortest wavelength present -- the one in the highest-index
    medium at the top frequency -- so a single uniform cell size is fine enough
    everywhere. See :func:`wavelength_cell_size_m` for the per-medium form.

    Returns ``None`` when the max frequency is not positive (no sensible size).
    """
    from wavesim_gui import materials as materials_mod

    eps_max = 1.0
    mu_max = 1.0
    for mat in materials_mod.find_materials(sim):
        if getattr(mat, "Pec", False):
            continue  # PEC has no meaningful eps/mu wavelength
        eps_max = max(eps_max, float(getattr(mat, "Eps", 1.0)))
        mu_max = max(mu_max, float(getattr(mat, "Mu", 1.0)))

    return wavelength_cell_size_m(sim, eps_max, mu_max, domain, cells_per_wavelength)


def suggested_cell_size_m(sim, domain=None, cells_per_wavelength=None,
                          cells_per_feature=None):
    """Cell size (metres) to fill the Domain's ``Dx/Dy/Dz`` automatically.

    **Electrostatic mode** takes a different driver entirely:
    :func:`feature_cell_size_m`, cells across the model's thinnest feature.
    There is no wavelength to count against, and a permittivity does not change
    what a Laplace solve needs from its mesh -- only the geometry does. The same
    size is used with the snapper on or off; with it on it is the coarse target
    the gap, feature-outline and curvature snapping then refine below.

    In **full-wave** mode:

    * **Uniform grid** -- the global-finest :func:`default_cell_size_m`: one
      constant size must resolve the highest-index medium in the model.
    * **Non-uniform grid** -- the coarse *background* resolution
      (:func:`wavelength_cell_size_m` for the background medium). ``Dx/Dy/Dz``
      then acts as the void/interior target and the snapper refines each material
      body down to its own per-medium requirement (see
      :mod:`wavesim_gui.gridbuild`), so higher-index regions get finer cells
      without meshing the whole domain that fine.

    Returns ``None`` when the driver has nothing to work from -- an unset max
    frequency in full wave, no assigned geometry in electrostatics.
    """
    from wavesim_gui.commands import is_electrostatic

    if domain is None:
        domain = find_domain(sim)
    if is_electrostatic(sim):
        return feature_cell_size_m(sim, domain, cells_per_feature)
    if domain is not None and getattr(domain, "UseNonuniformGrid", False):
        eps_bg, mu_bg = background_eps_mu(domain)
        return wavelength_cell_size_m(sim, eps_bg, mu_bg, domain,
                                      cells_per_wavelength)
    return default_cell_size_m(sim, domain, cells_per_wavelength)


# CFL parameters mirroring the solver's ``wavesim.grid.make_grid``: dt is the
# conservative 3D Courant limit, set purely by the cell sizes (independent of the
# cell counts, and of whether the domain is 2D). Duplicated here because the
# solver package cannot be imported into FreeCAD's Python.
_C0 = 299792458.0   # speed of light, m/s (wavesim.constants.C0)
_CFL = 0.99


def cfl_dt(domain):
    """Return the solver's CFL time step (seconds) for *domain*'s cell sizes.

    Mirrors ``wavesim.grid._cfl_dt``::

        dt = CFL / (c * sqrt(1/dx^2 + 1/dy^2 + 1/dz^2)),  CFL = 0.99

    where each spacing is the *minimum* cell width on its axis (the solver
    reduces the CFL over the finest cell), so the step count shown in the GUI
    matches what the runner actually uses -- on a uniform grid the minimum is
    just the cell size, so this is unchanged there.
    """
    dx, dy, dz = min_spacings_m(domain)
    return _CFL / (_C0 * math.sqrt(1.0 / dx ** 2 + 1.0 / dy ** 2 + 1.0 / dz ** 2))


def time_steps_for(domain, max_time_s):
    """Number of time steps to reach *max_time_s* at *domain*'s CFL step.

    Returns 0 when there is no domain or no positive max time; otherwise
    ``ceil(max_time / dt)`` (at least one step), matching the runner's count.
    """
    if domain is None or max_time_s <= 0.0:
        return 0
    dt = cfl_dt(domain)
    if dt <= 0.0:
        return 0
    return max(1, int(math.ceil(max_time_s / dt)))


def spacings_m(domain):
    """Per-face background gap in metres, keyed by face name (``'x0'``..``'z1'``).

    Falls back to a legacy scalar ``Spacing`` (same gap on every face) for a
    domain that has not been through :meth:`DomainObject._migrate_spacing` yet,
    and to zero for one with neither.
    """
    legacy = getattr(domain, _LEGACY_SPACING_PROP, None)
    default = float(legacy.Value) if legacy is not None else 0.0
    out = {}
    for face, prop, _doc in _SPACING_PROPS:
        value = getattr(domain, prop, None)
        mm = float(value.Value) if value is not None else default
        out[face] = mm / _MM_PER_M
    return out


# The three cell-grid planes drawn inside the domain box: property name, the
# axis each one is normal to, and what it shows.
_GRID_PLANE_PROPS = (
    ("GridPlaneX", "x", "YZ"),
    ("GridPlaneY", "y", "XZ"),
    ("GridPlaneZ", "z", "XY"),
)
# Set once the planes have been parked on the min faces (see place_grid_planes).
_GRID_PLANES_PLACED = "GridPlanesPlaced"

# Whether each of those three planes is drawn at all, in the same x/y/z order.
# Default **off**. They belong to the cross-section tool (``crosssection.py``):
# its grid button is the only thing that turns one on, and it leaves up only the
# plane it is cutting on, because three grids at once over a sectioned model is
# unreadable. A mesh plane drawn through an unsectioned solid model is worse
# still -- it is the picture the cut exists to replace -- so with no cut up
# there is no grid, and a document restored with one saved has it cleared
# (``crosssection._RestoreSweeper``).
_GRID_SHOW_PROPS = ("ShowGridPlaneX", "ShowGridPlaneY", "ShowGridPlaneZ")

# The per-axis arrays of snapped (geometry-forced) grid lines -- a subset of
# ``NodesX/Y/Z``, kept only so the 3D preview can draw them bold.
_SNAP_PROPS = ("SnapsX", "SnapsY", "SnapsZ")


def _ensure_grid_plane_props(obj):
    """Add the three grid-plane positions, back-filling an older document.

    Idempotent; called from ``__init__`` and ``onDocumentRestored``. A restored
    domain gets them unplaced, so the next recompute parks them on the min faces
    -- exactly where they used to be nailed.
    """
    for prop, axis, plane in _GRID_PLANE_PROPS:
        if not hasattr(obj, prop):
            obj.addProperty(
                "App::PropertyDistance", prop, "Grid",
                "World {} of the {} cell-grid plane. Move it through the "
                "domain to read the mesh where the geometry is, instead of "
                "only at the wall; clamped to the domain box when drawn"
                .format(axis, plane),
            )
    if not hasattr(obj, _GRID_PLANES_PLACED):
        obj.addProperty(
            "App::PropertyBool", _GRID_PLANES_PLACED, "Grid",
            "Whether the grid planes have been given their initial position",
        )
        obj.setEditorMode(_GRID_PLANES_PLACED, 2)   # hidden bookkeeping
        setattr(obj, _GRID_PLANES_PLACED, False)
    for prop, (_position_prop, _axis, plane) in zip(_GRID_SHOW_PROPS,
                                                    _GRID_PLANE_PROPS):
        if not hasattr(obj, prop):
            obj.addProperty(
                "App::PropertyBool", prop, "Grid",
                "Whether the {} cell-grid plane is drawn. Off by default: the "
                "grid is drawn on a cross-section, and the cross-section "
                "toolbar's grid button is what turns it on.".format(plane),
            )
            setattr(obj, prop, False)


def place_grid_planes(obj):
    """Park the grid planes on the domain's min faces, once.

    Only on the *first* recompute that gives the domain real bounds: after that
    the stored positions are the user's, and a domain that grows later must not
    drag their planes back to the wall.
    """
    if getattr(obj, _GRID_PLANES_PLACED, False):
        return
    mn = getattr(obj, "DomainMin", None)
    mx = getattr(obj, "DomainMax", None)
    if mn is None or mx is None or (mn - mx).Length < 1.0e-9:
        return          # no geometry yet; try again on the next recompute
    for prop, axis, _plane in _GRID_PLANE_PROPS:
        setattr(obj, prop, "{} mm".format(getattr(mn, axis)))
    setattr(obj, _GRID_PLANES_PLACED, True)


def grid_plane_positions_mm(domain):
    """The three drawn grid-plane positions as ``(x, y, z)`` in world mm.

    Clamped into the domain box **here rather than in the property**: the stored
    value is what the user asked for, and a box that shrinks for unrelated
    reasons should not silently eat it. Falls back to the min face for a domain
    that has no such property yet.
    """
    mn = getattr(domain, "DomainMin", None)
    mx = getattr(domain, "DomainMax", None)
    out = []
    for prop, axis, _plane in _GRID_PLANE_PROPS:
        lo = getattr(mn, axis) if mn is not None else 0.0
        hi = getattr(mx, axis) if mx is not None else 0.0
        value = getattr(domain, prop, None)
        pos = float(value.Value) if value is not None else lo
        out.append(min(max(pos, min(lo, hi)), max(lo, hi)))
    return tuple(out)


def grid_plane_visibility(domain):
    """Which of the three cell-grid planes are drawn, as ``(x, y, z)`` bools.

    Defaults to none for a domain that somehow has no such property: a grid is
    drawn because the grid button asked for it, so silence means no.
    """
    return tuple(bool(getattr(domain, prop, False)) for prop in _GRID_SHOW_PROPS)


def _ensure_es_bc_props(obj):
    """Add the six electrostatic per-face boundary properties if absent.

    Idempotent; called from ``__init__`` and ``onDocumentRestored`` so a document
    saved before electrostatics existed gains a grounded box -- the condition
    that makes the domain the reference conductor, and the one a capacitance
    extraction assumes.
    """
    for _face, prop, doc in _ES_FACE_PROPS:
        if not hasattr(obj, prop):
            obj.addProperty("App::PropertyEnumeration", prop, "Boundary", doc)
            setattr(obj, prop, _ES_BC_CHOICES)
            setattr(obj, prop, _ES_BC_CHOICES[0])


def _ensure_curvature_prop(obj):
    """Add the grazing-surface refinement factor if absent.

    Idempotent; called from ``__init__`` **and** ``onDocumentRestored``. The
    second call is the load-bearing one: adding a property only in ``__init__``
    means every document saved before it existed never gains it, and since the
    panel writes it under a ``hasattr`` guard, its spin box then silently writes
    nothing and the mesh never changes when you move it.
    """
    if not hasattr(obj, "CurvatureRefinement"):
        obj.addProperty(
            "App::PropertyFloat", "CurvatureRefinement", "Grid",
            "How far the non-uniform snapper may refine below the coarse cell "
            "size where a curved face runs tangent to a grid plane (a sphere's "
            "pole, a cylinder's silhouette), so the curvature is not flattened "
            "into one staircase step; the size asked for grows with curvature, "
            "this bounds the cost (1 = off)",
        )
        obj.CurvatureRefinement = 1.0


def _ensure_feature_prop(obj):
    """Add the electrostatic resolution knob if absent.

    Idempotent, and called from ``__init__`` **and** ``onDocumentRestored`` for
    the same reason as :func:`_ensure_curvature_prop`: a document saved before
    electrostatics grew its own resolution driver would otherwise never gain the
    property, and the panel's spin box would write nothing.

    A static field has no wavelength, so ``CellsPerWavelength`` cannot size an
    electrostatic mesh: the frequency it needs is one the user has to invent.
    What sets the resolution instead is the geometry -- the field's gradient is
    sharpest across the model's thinnest feature -- so this counts cells across
    exactly that. See :func:`feature_cell_size_m`.
    """
    if not hasattr(obj, "CellsPerFeature"):
        obj.addProperty(
            "App::PropertyInteger", "CellsPerFeature", "Grid",
            "Electrostatic mode only: cells resolving the smallest assigned "
            "body's thinnest dimension, which is what sizes the cell in a mode "
            "with no wavelength to count against (the full-wave equivalent is "
            "CellsPerWavelength)",
        )
        obj.CellsPerFeature = 10


def _set_snaps(obj, snaps):
    """Store the snapped grid lines (per-axis mm lists), or clear them.

    Tolerant of a document whose ``SnapsX/Y/Z`` are missing, for the same reason
    every other write here is: a Domain restored from an older file gains them on
    the next ``onDocumentRestored``, and a recompute must not raise in between.
    """
    for a, name in enumerate(_SNAP_PROPS):
        if hasattr(obj, name):
            setattr(obj, name, list(snaps[a]) if snaps else [])


def _ensure_snap_props(obj):
    """Add the three snapped-line arrays if absent.

    Idempotent; called from ``__init__`` **and** ``onDocumentRestored`` -- the
    second call is the load-bearing one for every document saved before these
    existed (see :func:`_ensure_curvature_prop`). They are pure view data: the
    subset of ``NodesX/Y/Z`` the snapper forced onto the geometry, which the view
    provider draws bold so the "must-have" lines are told apart from the graded
    fill's. Nothing about the mesh or the job reads them.
    """
    for name in _SNAP_PROPS:
        if not hasattr(obj, name):
            obj.addProperty(
                "App::PropertyFloatList", name, "Grid",
                "Grid lines snapped onto a geometry feature along {} (world "
                "mm; maintained internally, drawn bold)".format(name[-1].lower()),
            )
            obj.setEditorMode(name, 2)  # hidden


def shell_geometry_warnings(sim, domain):
    """Bodies that lie in an absorber shell without running through it.

    The PML is carved out of the inside of the domain box, so a body reaches it
    as soon as that face's background spacing is under ``PMLThickness`` cells --
    and the per-face spacing defaults to zero, which is exactly the case that
    hands a whole model to the absorber without being asked for. Carrying
    geometry into the PML is a *feature* (that is how a trace or a substrate is
    taken to infinity instead of ending at a wall), so this cannot be an error;
    but a body that **stops, bends or changes section inside the shell** reflects
    off its own discontinuity, and the reflection reads as a mediocre absorber
    rather than as a modelling mistake. So it is said out loud, live, in the
    Domain panel where the spacing that causes it is typed.

    This is the cheap bounding-box reading -- a body whose bbox does not span the
    whole shell along that face's own axis. It runs on every keystroke, so it
    cannot voxelise; the exact one, over the material mask the run will use, is
    :func:`wavesim_gui.voxelize.pml_shell_warnings` at job build. A body that
    passes here and fails there is one whose *section* varies through the shell,
    which no bounding box can see.

    Returns a list of message strings, one per offending face.
    """
    from wavesim_gui import materials as materials_mod

    if domain is None or sim is None:
        return []
    from wavesim_gui.commands import is_electrostatic

    params = domain_grid_params(
        domain, force_pml_faces=pml_port_faces(sim),
        modal_faces=modal_port_faces(sim),
        electrostatic=is_electrostatic(sim))
    # In electrostatic mode ``pml_faces`` is empty by construction, so this
    # falls out here: there is no absorber for a body to be buried in.
    pml_faces = set(params["pml_faces"])
    if not pml_faces:
        return []
    nodes = node_coords_mm(domain)
    if not all(len(a) >= 2 for a in nodes):
        return []
    bodies = []
    for mat in materials_mod.find_materials(sim):
        for body in getattr(mat, "Bodies", []) or []:
            shape = getattr(body, "Shape", None)
            if shape is not None and not shape.isNull():
                bodies.append((str(body.Label or body.Name), shape.BoundBox))
    if not bodies:
        return []

    tol = 1.0e-6
    out = []
    for axis, name in enumerate(("x", "y", "z")):
        lo_attr, hi_attr = (("XMin", "XMax"), ("YMin", "YMax"),
                            ("ZMin", "ZMax"))[axis]
        for high in (False, True):
            face = name + ("1" if high else "0")
            depth = (params["pad_hi"] if high else params["pad_lo"])[axis]
            if face not in pml_faces or depth <= 0:
                continue
            coords = nodes[axis]
            if high:
                shell_lo, shell_hi = coords[-1 - depth], coords[-1]
            else:
                shell_lo, shell_hi = coords[0], coords[depth]
            caught = []
            for label, bb in bodies:
                b_lo, b_hi = getattr(bb, lo_attr), getattr(bb, hi_attr)
                if b_hi <= shell_lo + tol or b_lo >= shell_hi - tol:
                    continue                      # clear of this shell
                if b_lo <= shell_lo + tol and b_hi >= shell_hi - tol:
                    continue                      # runs right through it
                caught.append(label)
            if caught:
                out.append(
                    "{} ends inside the {} absorber ({:.4g} to {:.4g} mm). A PML "
                    "terminates what runs straight out through it; a body that "
                    "stops or changes section in there reflects. Carry it out to "
                    "the face, or raise that face's background spacing past "
                    "{:g} mm.".format(
                        ", ".join(sorted(set(caught))), face, shell_lo, shell_hi,
                        abs(shell_hi - shell_lo))
                )
    return out


def electrostatic_boundary(domain):
    """The solver's ``boundary`` dict for an electrostatic solve on *domain*.

    Keys are the solver's ``'xmin'``..``'zmax'``; values are ``'ground'``
    (Dirichlet phi = 0) or ``'neumann'`` (a symmetry plane). A domain with none
    of the properties reads as a grounded box.

    The PML/PEC faces are deliberately not consulted. A PML face is padding an
    electrostatic run keeps only as extra background -- the absorber corrects
    terms inside a curl, and a static field has no curl for it to act on -- so
    mapping it onto either condition would be an invention.
    """
    out = {}
    for face, prop, _doc in _ES_FACE_PROPS:
        label = str(getattr(domain, prop, _ES_BC_CHOICES[0]))
        out[_ES_FACE_KEYS[face]] = _ES_BC_TOKENS.get(label, "ground")
    return out


def domain_grid_params(domain, force_pml_faces=(), modal_faces=(),
                       electrostatic=False):
    """Map a domain's per-face boundary settings to grid/solver parameters.

    Returns a dict with ``spacing_lo``/``spacing_hi`` (per-axis background gaps in
    metres, low/high side), ``pad_lo``/``pad_hi`` (per-axis PML cells),
    ``pml_faces``, ``pec_faces``, ``modal_faces`` and ``d_pml``. This is the one
    place the per-face properties are interpreted, so the drawn boxes, the
    voxelised grid and the runner all agree.

    ``pad_lo``/``pad_hi`` count cells the absorber takes **out of the inside** of
    each face -- they do not extend the grid. The domain box is the material
    bounds plus the background spacing, full stop; how much of it the PML eats is
    ``d_pml`` cells, and the background spacing is what buys clearance between
    the geometry and the absorber (a face whose spacing is 0 hands its geometry
    straight to the PML, deliberately).

    *force_pml_faces* names faces (``'x0'``..``'z1'``) that must be PML no matter
    what the per-face property says -- Gaussian beams and SPICE-TEM ports pass
    their launch faces (:func:`pml_port_faces`) so a face left (or set) to PEC
    still absorbs the launched wave, with its PML padding and boundary condition
    kept consistent (both derived from ``bc`` here).

    *modal_faces* names faces terminated by a **Modal Port**
    (:func:`modal_port_faces`). The port *is* the boundary, so such a face gets
    **no PML shell, no PEC wall and no background spacing**: it appears in
    neither ``pml_faces`` nor ``pec_faces``, contributes zero to ``pad_*``, and
    its gap is zeroed so the domain face lands exactly on the geometry the port
    plane must cut. *modal_faces* wins over *force_pml_faces* for a face named by
    both (a port cannot both terminate a face and hide behind an absorber).

    *electrostatic* strips the absorber out entirely: no ``pml_faces``, zero
    ``pad_*``, ``d_pml`` 0. Not merely cosmetic. The absorber costs a static
    solve no *extent* -- it lies inside the box -- but ``pad_lo``/``pad_hi``
    reserve those cells as **uniform coarse** ones and
    :func:`wavesim_gui.gridbuild.build_axis_nodes` discards every feature snap
    that falls in the shell. So a leftover PML thickness would unsnap the
    outermost cells of each face: exactly where the Ground boundary sits, and
    where a conductor run out to a face lands. A static field has no curl for an
    absorber to correct, so there is nothing to keep.
    """
    d_pml = 0 if electrostatic else int(getattr(domain, "PMLThickness", 8))
    bc = {face: getattr(domain, prop) for face, prop, _doc in _FACE_PROPS}
    for face in force_pml_faces or ():
        if face in bc:
            bc[face] = "PML"
    for face in modal_faces or ():
        if face in bc:
            bc[face] = _BC_MODAL

    # With no absorber there is nothing for the drawn PML box or the shell-
    # coloured grid lines to mark, so the list is empty rather than a set of
    # faces whose shell happens to be zero cells deep -- ``execute`` reads it to
    # decide whether to draw an inner box at all.
    pml_faces = [] if electrostatic else [f for f in _FACES
                                          if bc.get(f) == "PML"]
    pec_faces = [f for f in _FACES if bc.get(f) == "PEC"]
    modal = [f for f in _FACES if bc.get(f) == _BC_MODAL]

    pad_lo = (
        d_pml if bc["x0"] == "PML" else 0,
        d_pml if bc["y0"] == "PML" else 0,
        d_pml if bc["z0"] == "PML" else 0,
    )
    pad_hi = (
        d_pml if bc["x1"] == "PML" else 0,
        d_pml if bc["y1"] == "PML" else 0,
        d_pml if bc["z1"] == "PML" else 0,
    )
    # A modal-port face gets no background gap: the port plane sits on the domain
    # face and must cut the real cross-section, so any spacing there would hand
    # the mode solver a plane of empty background medium and no conductors.
    spacing = spacings_m(domain)
    for face in modal:
        spacing[face] = 0.0
    return {
        "spacing_lo": (spacing["x0"], spacing["y0"], spacing["z0"]),
        "spacing_hi": (spacing["x1"], spacing["y1"], spacing["z1"]),
        "pad_lo": pad_lo,
        "pad_hi": pad_hi,
        "pml_faces": pml_faces,
        "pec_faces": pec_faces,
        "modal_faces": modal,
        "d_pml": d_pml,
    }


# Face name -> the per-face boundary-condition property it controls.
_FACE_TO_PROP = {face: prop for face, prop, _doc in _FACE_PROPS}


def face_axis(face):
    """The normal axis ('x'/'y'/'z') of a domain face name like ``'x0'``."""
    return face[0]


def face_is_high(face):
    """True for the high-index face of its axis ('x1'/'y1'/'z1')."""
    return face.endswith("1")


def face_world_coord_mm(domain, face):
    """World-mm coordinate of the *face* plane along its normal axis.

    The domain box corners (``DomainMin``/``DomainMax``) *are* the grid bounds --
    the absorber is carved out of the inside -- so this is the wall itself. A
    modal port sits exactly here, with no background gap
    (:func:`domain_grid_params`), where the geometry it must cut ends. A beam /
    SPICE-TEM port names this face too, but its sheet is placed a full PML depth
    in by the runner (``_interior_position`` / ``sources.GaussianBeam``), which
    puts it on the absorber's inner edge -- i.e. on ``PmlMin``/``PmlMax``.
    """
    v = domain.DomainMax if face_is_high(face) else domain.DomainMin
    return {"x": v.x, "y": v.y, "z": v.z}[face_axis(face)]


def set_face_bc(domain, face, bc):
    """Set the boundary condition (``'PML'``/``'PEC'``) on a single *face*.

    A no-op for an unknown face name or a missing domain. The caller owns the
    transaction/recompute (this only writes the property).
    """
    if domain is None:
        return
    prop = _FACE_TO_PROP.get(face)
    if prop is not None and hasattr(domain, prop):
        setattr(domain, prop, bc)


def notify_materials_changed(doc):
    """Re-sync and recompute the Domain after the material set changes.

    Tracks the current material bodies on the domain (so later geometry edits to
    those bodies recompute it) and touches it so it auto-resizes immediately.
    Safe to call in console mode; a no-op when there is no domain yet.
    """
    from wavesim_gui.commands import active_simulation
    from wavesim_gui import materials as materials_mod

    sim = active_simulation(doc)
    domain = find_domain(sim)
    if domain is None:
        return
    bodies = []
    for mat in materials_mod.find_materials(sim):
        for body in getattr(mat, "Bodies", []) or []:
            if body not in bodies:
                bodies.append(body)
    if hasattr(domain, "TrackedBodies"):
        domain.TrackedBodies = bodies
    domain.touch()
    doc.recompute()

    # Snapshot planes are sized to the domain's XY extent, so re-sync them after
    # the domain resizes to the new geometry.
    from wavesim_gui import monitors as monitors_mod
    monitors_mod.refresh_snapshots(doc)


def notify_domain_inputs_changed(doc):
    """Recompute the Domain after an input it auto-sizes to changes.

    The domain auto-sizes to include every source position and snapshot slice,
    so adding or moving one outside the current box (or into the PML) enlarges
    the domain to contain it. Safe in console mode; a no-op when there is no
    domain yet.
    """
    from wavesim_gui.commands import active_simulation

    sim = active_simulation(doc)
    domain = find_domain(sim)
    if domain is None:
        return
    domain.touch()
    doc.recompute()

    # The domain may have grown, and snapshot planes track its extent.
    from wavesim_gui import monitors as monitors_mod
    monitors_mod.refresh_snapshots(doc)


# --------------------------------------------------------------------------- #
# GUI: view provider, task panel, command, creation helper
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

    # Edge indices for a box's 12 edges into the 8-corner coordinate list.
    _BOX_EDGES = [
        0, 1, -1, 1, 2, -1, 2, 3, -1, 3, 0, -1,   # bottom (z = min)
        4, 5, -1, 5, 6, -1, 6, 7, -1, 7, 4, -1,   # top    (z = max)
        0, 4, -1, 1, 5, -1, 2, 6, -1, 3, 7, -1,   # verticals
    ]

    def _box_corners(mn, mx):
        """The 8 corners of the box spanned by vectors *mn*..*mx* (mm)."""
        return [
            (mn.x, mn.y, mn.z), (mx.x, mn.y, mn.z),
            (mx.x, mx.y, mn.z), (mn.x, mx.y, mn.z),
            (mn.x, mn.y, mx.z), (mx.x, mn.y, mx.z),
            (mx.x, mx.y, mx.z), (mn.x, mx.y, mx.z),
        ]

    # Name carried by the separator holding the three cell grids, so a
    # re-attach can find the one it left behind instead of hanging a second
    # grid off the same view provider.
    _GRID_ROOT_NAME = "WavesimDomainGrid"


    def _attach_grid_root(vobj, grid_root):
        """Hang *grid_root* off the view provider **above** its mode switch.

        A view provider's display-mode node sits under a switch that
        ``Visibility`` flips, which is what an eye in the tree does. The grid is
        deliberately not under it: hiding the Domain is how you get the box and
        the PML shell out of the way, and it used to take the mesh with them --
        so reading the mesh against the geometry meant looking through the two
        boxes drawn around it. The grid answers to its own button instead (see
        ``crosssection.set_grid``), through ``ShowGridPlaneX/Y/Z``.

        ``RootNode`` is the node above that switch. If a FreeCAD build does not
        expose it, the grid goes back under the display mode -- the old
        behaviour, which is worse but not broken.
        """
        try:
            root_node = vobj.RootNode
        except Exception:
            root_node = None
        if root_node is None:
            return False
        try:
            for index in reversed(range(root_node.getNumChildren())):
                child = root_node.getChild(index)
                if child is not None and child.getName() == _GRID_ROOT_NAME:
                    root_node.removeChild(index)
            root_node.addChild(grid_root)
        except Exception:
            return False
        return True


    class DomainViewProvider:
        """Custom coin view provider drawing the domain + PML as wireframes.

        The boxes are a display mode, so the tree's eye hides them. **The cell
        grids are not** -- they hang off ``RootNode``, above the visibility
        switch (:func:`_attach_grid_root`), and are switched by the Domain's own
        ``ShowGridPlane*`` flags, which the cross-section toolbar's grid button
        drives.
        """

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            from pivy import coin

            self.Object = vobj.Object
            root = coin.SoSeparator()

            style = coin.SoDrawStyle()
            style.lineWidth = 2
            root.addChild(style)

            # Inner domain box.
            self._domain_color = coin.SoBaseColor()
            self._domain_color.rgb.setValue(*_DOMAIN_COLOR)
            self._domain_coords = coin.SoCoordinate3()
            self._domain_lines = coin.SoIndexedLineSet()
            dsep = coin.SoSeparator()
            dsep.addChild(self._domain_color)
            dsep.addChild(self._domain_coords)
            dsep.addChild(self._domain_lines)
            root.addChild(dsep)

            # Outer PML box.
            self._pml_color = coin.SoBaseColor()
            self._pml_color.rgb.setValue(*_PML_COLOR)
            self._pml_coords = coin.SoCoordinate3()
            self._pml_lines = coin.SoIndexedLineSet()
            psep = coin.SoSeparator()
            psep.addChild(self._pml_color)
            psep.addChild(self._pml_coords)
            psep.addChild(self._pml_lines)
            root.addChild(psep)

            # The cell grids live on a root of their own (see _GRID_ROOT_NAME
            # and the end of this method): **outside** the display-mode switch,
            # so the Domain's eye does not reach them. The eye is for the boxes;
            # the grid has its own button on the cross-section toolbar.
            grid_root = coin.SoSeparator()
            grid_root.setName(_GRID_ROOT_NAME)

            # Three cell grids (thin lines at the node coordinates) on the
            # Domain's grid planes, so the meshing is visible.
            self._grid_color = coin.SoBaseColor()
            self._grid_color.rgb.setValue(*_GRID_COLOR)
            gstyle = coin.SoDrawStyle()
            gstyle.lineWidth = 1
            self._grid_coords = coin.SoCoordinate3()
            self._grid_lines = coin.SoIndexedLineSet()
            gsep = coin.SoSeparator()
            gsep.addChild(self._grid_color)
            gsep.addChild(gstyle)
            gsep.addChild(self._grid_coords)
            gsep.addChild(self._grid_lines)
            grid_root.addChild(gsep)

            # The absorber's own share of those same planes, in the PML box's
            # colour. Its own node set for the same reason the snapped subset
            # has one: Coin carries one colour per set, and ``_grid_segments``
            # hands every segment to exactly one of the three.
            self._pmlgrid_color = coin.SoBaseColor()
            self._pmlgrid_color.rgb.setValue(*_PML_GRID_COLOR)
            pgstyle = coin.SoDrawStyle()
            pgstyle.lineWidth = 1
            self._pmlgrid_coords = coin.SoCoordinate3()
            self._pmlgrid_lines = coin.SoIndexedLineSet()
            pgsep = coin.SoSeparator()
            pgsep.addChild(self._pmlgrid_color)
            pgsep.addChild(pgstyle)
            pgsep.addChild(self._pmlgrid_coords)
            pgsep.addChild(self._pmlgrid_lines)
            grid_root.addChild(pgsep)

            # The snapped subset, over the top of the thin grid: same planes,
            # own colour and width. A separate node set rather than a per-line
            # style because Coin has no per-segment line width, and drawing them
            # twice would z-fight -- so ``_grid_segments`` hands each line to
            # exactly one of the two.
            self._snap_color = coin.SoBaseColor()
            self._snap_color.rgb.setValue(*_SNAP_COLOR)
            sstyle = coin.SoDrawStyle()
            sstyle.lineWidth = _SNAP_LINE_WIDTH
            self._snap_coords = coin.SoCoordinate3()
            self._snap_lines = coin.SoIndexedLineSet()
            ssep = coin.SoSeparator()
            ssep.addChild(self._snap_color)
            ssep.addChild(sstyle)
            ssep.addChild(self._snap_coords)
            ssep.addChild(self._snap_lines)
            grid_root.addChild(ssep)

            self._root = root
            self._grid_root = grid_root
            vobj.addDisplayMode(root, "Wireframe")
            if not _attach_grid_root(vobj, grid_root):
                root.addChild(grid_root)      # no RootNode: the old behaviour
            self._rebuild()
            self._rebuild_grid()

        def _fill(self, coords, lines, mn, mx):
            """Set *coords*/*lines* to a box, or clear them when degenerate."""
            if (mn - mx).Length < 1.0e-9:
                if lines.coordIndex.getNum():
                    lines.coordIndex.deleteValues(0)
                if coords.point.getNum():
                    coords.point.deleteValues(0)
                return
            pts = _box_corners(mn, mx)
            coords.point.setValues(0, len(pts), pts)
            if coords.point.getNum() > len(pts):
                coords.point.deleteValues(len(pts))
            lines.coordIndex.setValues(0, len(_BOX_EDGES), _BOX_EDGES)
            if lines.coordIndex.getNum() > len(_BOX_EDGES):
                lines.coordIndex.deleteValues(len(_BOX_EDGES))

        def _rebuild(self):
            obj = getattr(self, "Object", None)
            if obj is None:
                return
            self._fill(self._domain_coords, self._domain_lines,
                       obj.DomainMin, obj.DomainMax)
            self._fill(self._pml_coords, self._pml_lines,
                       obj.PmlMin, obj.PmlMax)

        def _grid_segments(self, mn, mx, nodes_x, nodes_y, nodes_z,
                           snaps=None, plane=None, interior=None, show=None):
            """Line segments for the three orthogonal cell grids.

            Returns ``(thin, bold, pml)``, each an ``(points, indices)`` pair for
            an ``SoIndexedLineSet``: an XY grid at ``z = plane[2]``, a YZ grid at
            ``x = plane[0]`` and an XZ grid at ``y = plane[1]`` -- the Domain's
            ``GridPlaneX/Y/Z``, already clamped into the box by
            :func:`grid_plane_positions_mm`, so a plane can be walked through the
            model instead of sitting on the wall. *plane* defaults to the min
            corner, which is where they used to be nailed. Grid lines are placed
            at the explicit per-axis node coordinates (``nodes_*``, world mm)
            clamped to the inner domain box, so a non-uniform grid shows its real
            (graded) spacing.

            *snaps* is the matching triple of **snapped** coordinates (the
            Domain's ``SnapsX/Y/Z`` -- the lines the snapper forced onto a
            geometry feature). Those go in the *bold* set and the rest in the
            thin one, so every line is drawn exactly once and the two styles
            cannot z-fight. Line counts per axis are clamped to
            :data:`_MAX_GRID_LINES` / :data:`_MAX_SNAP_LINES` by decimating --
            separately, so thinning a fine grid never drops a feature line.

            *interior* is the ``(PmlMin, PmlMax)`` pair bounding the absorber-free
            region (``None`` when the domain has no PML). The absorber lies
            *inside* the box, so a thin line is cut where it crosses that
            boundary and the parts outside go to the *pml* set in
            :data:`_PML_GRID_COLOR`: the mesh reads as two regions rather than
            one, which is the whole point of drawing it there. A cell is in the
            absorber if **any** of its coordinates is past the interior on that
            coordinate's own axis, so a line whose fixed coordinate is already in
            a shell is absorber over its whole length -- that is what fills in
            the corners where two PMLs overlap. Bold snapped lines are never
            split: the snapper puts no forced line in the shell (the CPML needs a
            constant width there), so a bold line only ever *crosses* one, and
            cutting it would hide the feature it marks.

            *show* is the matching triple of per-plane visibility flags (the
            Domain's ``ShowGridPlaneX/Y/Z``); a plane switched off contributes no
            segments at all, to either set. Defaults to all three on, which is
            what every domain drew before the flags existed.
            """
            px, py, pz = plane if plane is not None else (mn.x, mn.y, mn.z)
            show_x, show_y, show_z = show if show is not None else (True, True, True)
            snaps = snaps if snaps is not None else ([], [], [])
            thin_pts, thin_idx = [], []
            bold_pts, bold_idx = [], []
            pml_pts, pml_idx = [], []
            # Per-axis interior interval; ``None`` on an axis (or altogether)
            # where nothing is absorbed, which makes every test below a no-op.
            if interior is None:
                bounds = (None, None, None)
            else:
                ilo, ihi = interior
                bounds = tuple(
                    (getattr(ilo, a), getattr(ihi, a)) for a in ("x", "y", "z")
                )

            def adder(pts, idx):
                def add_line(p0, p1):
                    a = len(pts)
                    pts.append(p0)
                    pts.append(p1)
                    idx.extend([a, a + 1, -1])
                return add_line

            add_thin = adder(thin_pts, thin_idx)
            add_bold = adder(bold_pts, bold_idx)
            add_pml = adder(pml_pts, pml_idx)

            tol = 1e-9

            def in_shell(axis, v):
                """Is coordinate *v* inside the absorber on this *axis*?"""
                b = bounds[axis]
                return b is not None and (v < b[0] - tol or v > b[1] + tol)

            def add_split(p0, p1, axis):
                """Draw one grid line, cut at the absorber boundary it crosses.

                *axis* is the index of the coordinate that varies along the line;
                the other two are fixed, and either of them landing in its own
                shell paints the whole line as absorber.
                """
                if any(in_shell(a, p0[a]) for a in range(3) if a != axis):
                    add_pml(p0, p1)
                    return
                b = bounds[axis]
                v0, v1 = p0[axis], p1[axis]
                if b is None or (v0 >= b[0] - tol and v1 <= b[1] + tol):
                    add_thin(p0, p1)
                    return

                def at(v):
                    q = list(p0)
                    q[axis] = v
                    return tuple(q)

                for lo_v, hi_v, add in ((v0, min(max(b[0], v0), v1), add_pml),
                                        (min(max(b[0], v0), v1),
                                         max(min(b[1], v1), v0), add_thin),
                                        (max(min(b[1], v1), v0), v1, add_pml)):
                    if hi_v - lo_v > tol:
                        add(at(lo_v), at(hi_v))

            def decimate(vals, cap):
                if len(vals) <= cap:
                    return vals
                step = int(math.ceil(len(vals) / float(cap)))
                out = vals[::step]
                if out and out[-1] != vals[-1]:
                    out.append(vals[-1])
                return out

            def ticks(lo, hi, nodes, axis_snaps):
                # Node coordinates inside [lo, hi] (the inner box), always closing
                # on both faces, split into the snapped ones and the rest, then
                # each decimated so no axis exceeds its line cap.
                tol = 1e-9
                vals = [v for v in nodes if lo - tol <= v <= hi + tol]
                if not vals:
                    vals = [lo, hi]
                if vals[0] > lo + tol:
                    vals.insert(0, lo)
                if vals[-1] < hi - tol:
                    vals.append(hi)
                marks = sorted(v for v in axis_snaps if lo - tol <= v <= hi + tol)
                bold, thin = [], []
                for v in vals:
                    # A snap coordinate *is* a node -- the fill lands exactly on
                    # it -- so this only has to tolerate the float round trip
                    # through the property, not search a neighbourhood.
                    k = bisect.bisect_left(marks, v - tol)
                    (bold if k < len(marks) and abs(marks[k] - v) <= tol
                     else thin).append(v)
                return (decimate(thin, _MAX_GRID_LINES),
                        decimate(bold, _MAX_SNAP_LINES))

            xs, bxs = ticks(mn.x, mx.x, nodes_x, snaps[0])
            ys, bys = ticks(mn.y, mx.y, nodes_y, snaps[1])
            zs, bzs = ticks(mn.z, mx.z, nodes_z, snaps[2])

            # (varying axis, endpoint pair) for every line of the three planes.
            def lines(ax, ay, az):
                if show_z:                         # XY grid at z = pz
                    for x in ax:
                        yield 1, (x, mn.y, pz), (x, mx.y, pz)
                    for y in ay:
                        yield 0, (mn.x, y, pz), (mx.x, y, pz)
                if show_x:                         # YZ grid at x = px
                    for y in ay:
                        yield 2, (px, y, mn.z), (px, y, mx.z)
                    for z in az:
                        yield 1, (px, mn.y, z), (px, mx.y, z)
                if show_y:                         # XZ grid at y = py
                    for x in ax:
                        yield 2, (x, py, mn.z), (x, py, mx.z)
                    for z in az:
                        yield 0, (mn.x, py, z), (mx.x, py, z)

            for axis, p0, p1 in lines(xs, ys, zs):
                add_split(p0, p1, axis)
            for _axis, p0, p1 in lines(bxs, bys, bzs):
                add_bold(p0, p1)
            return ((thin_pts, thin_idx), (bold_pts, bold_idx),
                    (pml_pts, pml_idx))

        def _rebuild_grid(self):
            obj = getattr(self, "Object", None)
            if obj is None or not hasattr(self, "_snap_coords"):
                return          # not attached yet; attach() rebuilds at the end
            mn, mx = obj.DomainMin, obj.DomainMax
            pairs = ((self._grid_coords, self._grid_lines),
                     (self._snap_coords, self._snap_lines),
                     (self._pmlgrid_coords, self._pmlgrid_lines))
            if (mn - mx).Length < 1.0e-9:
                for coords, lines in pairs:
                    if lines.coordIndex.getNum():
                        lines.coordIndex.deleteValues(0)
                    if coords.point.getNum():
                        coords.point.deleteValues(0)
                return
            nodes_x = list(getattr(obj, "NodesX", []) or [])
            nodes_y = list(getattr(obj, "NodesY", []) or [])
            nodes_z = list(getattr(obj, "NodesZ", []) or [])
            # Fall back to uniform ticks if the node arrays are not populated yet
            # (e.g. an older document restored before the first recompute).
            if not nodes_x:
                nodes_x = [mn.x + i * float(obj.Dx.Value)
                           for i in range(int((mx.x - mn.x) / max(float(obj.Dx.Value), 1e-9)) + 1)]
            if not nodes_y:
                nodes_y = [mn.y + i * float(obj.Dy.Value)
                           for i in range(int((mx.y - mn.y) / max(float(obj.Dy.Value), 1e-9)) + 1)]
            if not nodes_z:
                nodes_z = [mn.z + i * float(obj.Dz.Value)
                           for i in range(int((mx.z - mn.z) / max(float(obj.Dz.Value), 1e-9)) + 1)]
            snaps = tuple(list(getattr(obj, name, []) or [])
                          for name in _SNAP_PROPS)
            # The absorber-free interior, or None when nothing absorbs (the
            # PML box collapses to a point when no face carries a PML).
            pml_min, pml_max = obj.PmlMin, obj.PmlMax
            interior = (None if (pml_max - pml_min).Length < 1.0e-9
                        else (pml_min, pml_max))
            sets = self._grid_segments(
                mn, mx, nodes_x, nodes_y, nodes_z, snaps=snaps,
                plane=grid_plane_positions_mm(obj), interior=interior,
                show=grid_plane_visibility(obj),
            )
            for (coords, lines), (pts, idx) in zip(pairs, sets):
                coords.point.setValues(0, len(pts), pts)
                if coords.point.getNum() > len(pts):
                    coords.point.deleteValues(len(pts))
                lines.coordIndex.setValues(0, len(idx), idx)
                if lines.coordIndex.getNum() > len(idx):
                    lines.coordIndex.deleteValues(len(idx))

        def updateData(self, obj, prop):
            if prop in ("DomainMin", "DomainMax", "PmlMin", "PmlMax"):
                self._rebuild()
            if prop in ("DomainMin", "DomainMax", "PmlMin", "PmlMax",
                        "Dx", "Dy", "Dz",
                        "NodesX", "NodesY", "NodesZ",
                        "SnapsX", "SnapsY", "SnapsZ",
                        "GridPlaneX", "GridPlaneY", "GridPlaneZ",
                        "ShowGridPlaneX", "ShowGridPlaneY", "ShowGridPlaneZ"):
                self._rebuild_grid()

        def getDisplayModes(self, vobj):
            return ["Wireframe"]

        def getDefaultDisplayMode(self):
            return "Wireframe"

        def setDisplayMode(self, mode):
            return mode

        def getIcon(self):
            return _DOMAIN_ICON

        def setEdit(self, vobj, mode=0):
            _open_domain_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_domain_panel(vobj.Object)
            return True

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class TaskDomainPanel:
        """Task-tab panel: cell sizes, spacing, background, PML, per-face BCs."""

        def __init__(self, obj):
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets

            self.obj = obj

            # The solver mode is settled first: it decides which resolution
            # driver this panel offers (a wavelength has no meaning in a static
            # solve), whether the absorber rows exist at all, and which of the
            # two per-face boundary property sets the combos below edit.
            from wavesim_gui.commands import active_simulation, is_electrostatic

            self._es = is_electrostatic(active_simulation(obj.Document))

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Domain")
            layout = QtWidgets.QFormLayout(form)
            # Kept so _set_row_visible can reach a field's label: the rows that
            # only mean something in one solver mode are hidden, not disabled.
            self._layout = layout

            def cell_spin(value_mm):
                spin = QtWidgets.QDoubleSpinBox()
                spin.setRange(1.0e-6, 1.0e6)
                spin.setDecimals(6)
                spin.setSuffix(" mm")
                spin.setSingleStep(0.1)
                spin.setValue(value_mm)
                return spin

            self._cubic = QtWidgets.QCheckBox("Uniform cubic cells (dy = dz = dx)")
            cubic = (
                abs(obj.Dx.Value - obj.Dy.Value) < 1e-9
                and abs(obj.Dx.Value - obj.Dz.Value) < 1e-9
            )
            self._cubic.setChecked(cubic)

            self._dx = cell_spin(float(obj.Dx.Value))
            self._dy = cell_spin(float(obj.Dy.Value))
            self._dz = cell_spin(float(obj.Dz.Value))

            # The resolution knob, and the one-click button that fills the cell
            # sizes from it. Which of the two is on show depends on the solver
            # mode, because the driver does:
            #
            # * full wave -- cells per wavelength, c / (fmax * N_lambda * n);
            # * electrostatic -- cells across the model's thinnest feature.
            #
            # Both are built and both are stored, exactly as the two per-face
            # boundary sets below are, so switching modes back and forth never
            # discards either answer.
            self._cpw = QtWidgets.QSpinBox()
            self._cpw.setRange(1, 1000)
            self._cpw.setSuffix(" cells/wavelength")
            self._cpw.setValue(int(getattr(obj, "CellsPerWavelength", 20)))

            _ensure_feature_prop(obj)
            self._cpf = QtWidgets.QSpinBox()
            self._cpf.setRange(1, 1000)
            self._cpf.setSuffix(" cells/smallest feature")
            self._cpf.setValue(int(getattr(obj, "CellsPerFeature", 10)))
            self._cpf.setToolTip(
                "A static solve has no wavelength, so nothing about a medium "
                "sizes its mesh -- only the geometry does. This counts cells "
                "across the thinnest dimension of the smallest body assigned "
                "to a material (conductors included: in an electrostatic run "
                "they are the problem). The gap, feature-outline and curvature "
                "snapping then refine below it where the grid is non-uniform."
            )

            self._default_btn = QtWidgets.QPushButton(
                "Default from geometry" if self._es
                else "Default from max frequency"
            )
            self._default_btn.clicked.connect(self._apply_default_cell_size)

            # Non-uniform (snapper) grid: a mode toggle plus the grading bound.
            # When on, the cell-size boxes become the coarse interior target
            # (best set from the max frequency) and the snapper refines features.
            self._nonuniform = QtWidgets.QCheckBox(
                "Non-uniform grid (snap to features)"
            )
            self._nonuniform.setChecked(bool(getattr(obj, "UseNonuniformGrid", False)))
            self._ratio = QtWidgets.QDoubleSpinBox()
            self._ratio.setRange(1.0, 5.0)
            self._ratio.setDecimals(2)
            self._ratio.setSingleStep(0.1)
            self._ratio.setValue(float(getattr(obj, "MaxGradingRatio", 1.5)))

            # Smallest cell the snapper may create (0 = no limit): stops automatic
            # feature snapping from producing an extremely fine mesh.
            self._min_cell = QtWidgets.QDoubleSpinBox()
            self._min_cell.setRange(0.0, 1.0e6)
            self._min_cell.setDecimals(6)
            self._min_cell.setSuffix(" mm")
            self._min_cell.setSingleStep(0.1)
            self._min_cell.setValue(float(getattr(obj, "MinCellSize", 0.0).Value)
                                    if hasattr(obj, "MinCellSize") else 0.0)

            # How far the snapper may refine at a surface tangent to a grid
            # plane (1 = off). Bounds a request that is otherwise unbounded --
            # see gridbuild.collect_curvature_sizes -- and every extra factor
            # here costs cells on *whole planes* of the grid, so keep it modest.
            self._curvature = QtWidgets.QDoubleSpinBox()
            self._curvature.setRange(1.0, 64.0)
            self._curvature.setDecimals(1)
            self._curvature.setSingleStep(1.0)
            self._curvature.setPrefix("up to ")
            self._curvature.setSuffix("x finer")
            # Re-run here for the same reason as _migrate_spacing below: a
            # document whose Proxy failed to restore would otherwise reach the
            # panel without the property, and _write_to_obj's hasattr guard would
            # then drop every edit on the floor.
            _ensure_curvature_prop(obj)
            self._curvature.setValue(curvature_refinement(obj))

            self._counts = QtWidgets.QLabel(self._counts_text())

            # Live "your geometry is in the absorber" notice. The per-face
            # spacing defaults to zero and the PML is taken out of the inside of
            # the box, so the default puts a model in the absorber -- which is a
            # supported thing to want (a body carried out through a face runs to
            # infinity instead of ending at a wall) and a silent disaster when it
            # is not. It sits right under the spacing boxes that cause it,
            # refreshed by every live-apply, and the 3D view says the same thing
            # in the other language: the grid lines inside the shell are drawn in
            # the PML box's colour, so which part of the model is buried is
            # visible rather than inferred.
            self._shell = QtWidgets.QLabel("")
            self._shell.setWordWrap(True)
            self._shell.setStyleSheet("color: #c0392b;")

            # Per-face background spacing, built like the per-face BCs below: a
            # "same on all faces" checkbox drives every face from the first
            # (X min) box and greys the rest out. The migration is re-run here so
            # a document whose Proxy failed to restore still has the six
            # properties the widgets read and write (it no-ops once migrated).
            DomainObject._migrate_spacing(obj)
            self._spacing_order = [prop for _f, prop, _d in _SPACING_PROPS]
            self._spacings = {}
            for _face, prop, _doc in _SPACING_PROPS:
                spin = QtWidgets.QDoubleSpinBox()
                spin.setRange(0.0, 1.0e6)
                spin.setDecimals(4)
                spin.setSuffix(" mm")
                spin.setSingleStep(0.5)
                spin.setValue(float(getattr(obj, prop).Value))
                self._spacings[prop] = spin
            self._same_spacing = QtWidgets.QCheckBox(
                "Same background spacing on all faces"
            )
            self._same_spacing.setChecked(
                len({round(s.value(), 6) for s in self._spacings.values()}) == 1
            )

            self._dpml = QtWidgets.QSpinBox()
            self._dpml.setRange(1, 100)
            self._dpml.setSuffix(" cells")
            self._dpml.setValue(int(getattr(obj, "PMLThickness", 8)))

            # Background (empty-voxel) material: a dropdown of the simulation's
            # materials. ``self._bg_values`` runs parallel to the combo items,
            # each mapping to a Material (or None = vacuum). A synthetic "Vacuum"
            # entry is offered *only* when no real vacuum material exists, so the
            # seeded Vacuum material isn't duplicated by a second vacuum choice.
            from wavesim_gui.commands import active_simulation
            from wavesim_gui import materials as materials_mod

            sim = active_simulation(obj.Document)
            self._materials = materials_mod.find_materials(sim) if sim else []
            self._background = QtWidgets.QComboBox()
            self._bg_values = []
            if not any(material_is_vacuum(m) for m in self._materials):
                self._background.addItem("Vacuum (eps=1, mu=1)")
                self._bg_values.append(None)
            for mat in self._materials:
                self._background.addItem(mat.Label)
                self._bg_values.append(mat)
            current_bg = getattr(obj, "Background", None)
            if current_bg in self._bg_values:
                bg_index = self._bg_values.index(current_bg)
            else:
                # Unset (or a stale link): default to the vacuum material if there
                # is one, else the synthetic vacuum entry (index 0).
                bg_index = next(
                    (i for i, m in enumerate(self._bg_values)
                     if material_is_vacuum(m)), 0
                )
            self._background.setCurrentIndex(max(0, bg_index))

            layout.addRow(self._cubic)
            layout.addRow("Cell size dx:", self._dx)
            layout.addRow("Cell size dy:", self._dy)
            layout.addRow("Cell size dz:", self._dz)
            layout.addRow("Resolution:", self._cpw)
            layout.addRow("Resolution:", self._cpf)
            self._set_row_visible(self._cpw, not self._es)
            self._set_row_visible(self._cpf, self._es)
            layout.addRow("", self._default_btn)
            layout.addRow(self._nonuniform)
            layout.addRow("Max grading ratio:", self._ratio)
            layout.addRow("Min cell size:", self._min_cell)
            layout.addRow("Refine at curved surfaces:", self._curvature)
            layout.addRow("Cell counts:", self._counts)
            layout.addRow(self._shell)
            self._refresh_shell()
            # Per-face background spacing. Headed, because the six face rows here
            # and the six boundary-condition rows below carry the same labels.
            layout.addRow(QtWidgets.QLabel("<b>Background spacing</b>"))
            layout.addRow(self._same_spacing)
            for face, prop, _doc in _SPACING_PROPS:
                layout.addRow(_FACE_LABELS[face], self._spacings[prop])
            layout.addRow("Background material:", self._background)
            layout.addRow("PML thickness:", self._dpml)
            # No absorber in a static solve, so no depth to set. Hidden rather
            # than left inert: a leftover thickness used to reserve a shell of
            # uniform coarse cells at each face and throw away the feature snaps
            # inside it, so the row read as free and was not.
            self._set_row_visible(self._dpml, not self._es)

            # Per-face boundary conditions. A "same on all faces" checkbox drives
            # every face from the first (X min) combo and greys the rest out.
            #
            # Which set of six properties this edits depends on the solver mode:
            # PML/PEC for a full-wave run, Ground/Symmetry for an electrostatic
            # one. Both are stored, so switching modes back and forth never
            # discards either answer.
            _sim = active_simulation(obj.Document)
            if self._es:
                _ensure_es_bc_props(obj)
            self._bc_props = _ES_FACE_PROPS if self._es else _FACE_PROPS
            self._bc_choices = _ES_BC_CHOICES if self._es else _BC_CHOICES
            layout.addRow(QtWidgets.QLabel(
                "<b>Electrostatic boundary</b>" if self._es
                else "<b>Boundary conditions</b>"))
            self._bc_order = [prop for _f, prop, _d in self._bc_props]
            self._same_bc = QtWidgets.QCheckBox("Same condition on all faces")
            all_same = len({str(getattr(obj, p)) for p in self._bc_order}) == 1
            self._same_bc.setChecked(all_same)
            layout.addRow(self._same_bc)

            # Faces hosting a face-launching source are not the user's to set:
            # the job builder overrides them regardless (see ``pml_port_faces`` /
            # ``modal_port_faces`` and ``domain_grid_params``), so show that state
            # as its own locked entry rather than an editable combo that silently
            # does not apply. Electrostatics ignores ports entirely, so nothing is
            # locked there.
            self._modal_faces = set() if self._es else set(modal_port_faces(_sim))
            self._port_faces = (set() if self._es
                                else set(pml_port_faces(_sim)) - self._modal_faces)
            self._combos = {}
            for face, prop, _doc in self._bc_props:
                combo = QtWidgets.QComboBox()
                combo.addItems(self._bc_choices)
                if face in self._modal_faces:
                    combo.addItem(_MODAL_BC_LABEL)
                    combo.setCurrentText(_MODAL_BC_LABEL)
                    combo.setEnabled(False)
                    combo.setToolTip(
                        "A Modal Port terminates this face itself: it launches "
                        "the mode inward and absorbs what returns, so the face "
                        "carries no PML, no PEC wall and no background spacing. "
                        "Delete that port to set the boundary condition here."
                    )
                elif face in self._port_faces:
                    combo.addItem(_PML_PORT_BC_LABEL)
                    combo.setCurrentText(_PML_PORT_BC_LABEL)
                    combo.setEnabled(False)
                    combo.setToolTip(
                        "A Gaussian beam / SPICE-TEM port launches from an "
                        "interior plane behind this face, so it is always "
                        "absorbing. Delete that source to set the boundary "
                        "condition here."
                    )
                else:
                    combo.setCurrentText(str(getattr(obj, prop)))
                self._combos[prop] = combo
                layout.addRow(_FACE_LABELS[face], combo)

            info = QtWidgets.QLabel(
                "The domain box auto-sizes to the assigned geometry plus the "
                "per-face background spacing (filled with the background "
                "material). A Ground face holds phi = 0, which makes the box the "
                "reference conductor every capacitance to ground is measured "
                "against; a Symmetry face lets no field cross it, which is what "
                "halves a mirror-symmetric model. There is no absorber here at "
                "all -- one corrects terms inside a curl, which a static field "
                "has none of -- so no PML is drawn, no depth is set, and the "
                "cells at each face are meshed on the geometry like any other."
                if self._es else
                "The domain box auto-sizes to the assigned geometry plus the "
                "per-face background spacing (filled with the background "
                "material). PML faces absorb outgoing waves in the outermost "
                "cells *of* the box, so the box is the whole grid and a body "
                "run out to a face (spacing 0) is carried into the absorber; "
                "PEC faces are perfectly-conducting walls. A face carrying a "
                "Modal Port reads '{}': the port is the boundary — it launches "
                "the mode and absorbs what comes back, exactly and at DC too, so "
                "that face gets no absorber, no wall and no background gap (the "
                "port plane has to cut the real cross-section). A face launching "
                "a Gaussian beam or a SPICE-TEM port reads '{}' instead, since "
                "those drive an interior plane and need the absorber behind it. "
                "The CFL time step is computed by the solver and reported in the "
                "run summary."
                .format(_MODAL_BC_LABEL, _PML_PORT_BC_LABEL)
            )
            info.setWordWrap(True)
            layout.addRow(info)

            # Original values, so Cancel can restore the singleton after the
            # live edits below.
            self._orig = self._snapshot()
            # Re-entrancy guard: mirroring one widget onto others must trigger a
            # single recompute, not one per mirrored widget. ``_initializing``
            # suppresses the live recompute while the panel is first wired up.
            self._suspend = False
            self._initializing = True

            self._cubic.toggled.connect(self._on_cubic)
            self._dx.valueChanged.connect(self._mirror_cubic)
            # Live-apply: push edits onto the Domain and recompute so the derived
            # cell counts *and* the 3D mesh preview refresh immediately, before OK.
            for w in (self._dx, self._dy, self._dz, self._ratio, self._min_cell,
                      self._curvature):
                w.valueChanged.connect(self._live_apply)
            self._dpml.valueChanged.connect(self._live_apply)
            first_spacing = self._spacing_order[0]
            for prop, spin in self._spacings.items():
                if prop != first_spacing:
                    spin.valueChanged.connect(self._live_apply)
            # As with the BCs, the first face's box drives the rest (when "same"
            # is on) and then applies, so one recompute covers all six faces.
            self._spacings[first_spacing].valueChanged.connect(
                self._on_first_spacing
            )
            self._same_spacing.toggled.connect(self._on_same_spacing)
            # The resolution, background medium and grid mode all change the
            # frequency-driven cell size, so they re-derive it and apply
            # immediately (see _auto_fill_cell_size) -- no need to press the
            # "Default from max frequency" button after each. The enable/disable
            # handlers are connected first so they run before the re-derive.
            self._cpw.valueChanged.connect(self._auto_fill_cell_size)
            self._cpf.valueChanged.connect(self._auto_fill_cell_size)
            self._nonuniform.toggled.connect(self._on_nonuniform)
            self._nonuniform.toggled.connect(self._auto_fill_cell_size)
            self._cubic.toggled.connect(self._live_apply)
            self._background.currentIndexChanged.connect(self._auto_fill_cell_size)
            first_prop = self._bc_order[0]
            for prop, combo in self._combos.items():
                if prop == first_prop:
                    continue
                combo.currentTextChanged.connect(self._live_apply)
            # The first face's combo drives the rest (when "same" is on) and then
            # applies, so a single recompute reflects all six faces at once.
            self._combos[first_prop].currentTextChanged.connect(self._on_first_bc)
            self._same_bc.toggled.connect(self._on_same_bc)
            self._on_cubic(self._cubic.isChecked())
            self._on_nonuniform(self._nonuniform.isChecked())
            self._on_same_spacing(self._same_spacing.isChecked())
            self._on_same_bc(self._same_bc.isChecked())
            self._initializing = False

            self.form = form

        def _set_row_visible(self, widget, visible):
            """Show/hide a form row (its field widget and its label)."""
            widget.setVisible(visible)
            label = self._layout.labelForField(widget)
            if label is not None:
                label.setVisible(visible)

        def _counts_text(self):
            """The Domain's cell counts as recomputed by ``execute``.

            Always read back from the object rather than re-derived from the
            spin boxes: with the snapper on, only ``execute`` knows where the
            grid lines actually land, and a second (uniform, bbox-only) estimate
            here would disagree with it. Every edit path live-applies and
            recomputes first, so this is current.
            """
            nx = int(getattr(self.obj, "Nx", 0))
            ny = int(getattr(self.obj, "Ny", 0))
            nz = int(getattr(self.obj, "Nz", 0))
            if nx and ny and nz:
                return "{} x {} x {}  ({:,} cells)".format(nx, ny, nz, nx * ny * nz)
            return "(assign material geometry to size the grid)"

        def _on_cubic(self, checked):
            if self._nonuniform.isChecked():
                return  # cell-size boxes are driven by the snapper, not editable
            self._dy.setEnabled(not checked)
            self._dz.setEnabled(not checked)
            self._mirror_cubic()

        def _on_nonuniform(self, checked):
            # With the snapper on, Dx/Dy/Dz become the coarse interior target set
            # from the max frequency: disable manual editing (but keep the
            # "Default from max frequency" button), and expose the grading ratio
            # and the minimum cell size (both snapper-only).
            self._ratio.setEnabled(checked)
            self._min_cell.setEnabled(checked)
            self._curvature.setEnabled(checked)
            self._cubic.setEnabled(not checked)
            self._dx.setEnabled(not checked)
            if checked:
                self._dy.setEnabled(False)
                self._dz.setEnabled(False)
            else:
                self._on_cubic(self._cubic.isChecked())

        def _on_same_spacing(self, checked):
            """Grey out all but the first spacing box and drive them from it."""
            first = self._spacings[self._spacing_order[0]]
            self._suspend = True
            for prop in self._spacing_order[1:]:
                spin = self._spacings[prop]
                spin.setEnabled(not checked)
                if checked:
                    spin.setValue(first.value())
            self._suspend = False
            self._live_apply()

        def _on_first_spacing(self, value):
            """Mirror the first face's spacing onto the rest ('same' on), apply."""
            if self._same_spacing.isChecked():
                self._suspend = True
                for prop in self._spacing_order[1:]:
                    self._spacings[prop].setValue(value)
                self._suspend = False
            self._live_apply()

        def _is_port_combo(self, prop):
            """True when *prop*'s face hosts a face-launching source (locked)."""
            return self._combos[prop].currentText() in (
                _PML_PORT_BC_LABEL, _MODAL_BC_LABEL)

        def _on_same_bc(self, checked):
            """Grey out all but the first BC combo and drive them from it.

            A port face stays locked either way -- "same on all faces" must not
            re-enable it or overwrite its label.
            """
            first = self._combos[self._bc_order[0]]
            self._suspend = True
            for prop in self._bc_order[1:]:
                combo = self._combos[prop]
                if self._is_port_combo(prop):
                    continue
                combo.setEnabled(not checked)
                if checked:
                    combo.setCurrentText(first.currentText())
            self._suspend = False
            self._live_apply()

        def _on_first_bc(self, text):
            """Mirror the first face's BC onto the rest (when 'same' is on), apply."""
            if self._same_bc.isChecked():
                self._suspend = True
                for prop in self._bc_order[1:]:
                    if not self._is_port_combo(prop):
                        self._combos[prop].setCurrentText(text)
                self._suspend = False
            self._live_apply()

        def _mirror_cubic(self, *_):
            if self._cubic.isChecked():
                self._suspend = True
                self._dy.setValue(self._dx.value())
                self._dz.setValue(self._dx.value())
                self._suspend = False

        # ---- live edit: apply widgets to the object + restore on cancel ------ #

        def _snapshot(self):
            """Capture the Domain's editable state for a Cancel restore."""
            obj = self.obj
            return {
                "Dx": obj.Dx.Value, "Dy": obj.Dy.Value, "Dz": obj.Dz.Value,
                "CellsPerWavelength": int(getattr(obj, "CellsPerWavelength", 20)),
                "UseNonuniformGrid": bool(getattr(obj, "UseNonuniformGrid", True)),
                "MaxGradingRatio": float(getattr(obj, "MaxGradingRatio", 1.5)),
                "MinCellSize": (float(obj.MinCellSize.Value)
                                if hasattr(obj, "MinCellSize") else 0.0),
                "CurvatureRefinement": curvature_refinement(obj),
                "CellsPerFeature": int(getattr(obj, "CellsPerFeature", 10)),
                "spacing": {p: getattr(obj, p).Value
                            for p in self._spacing_order},
                "PMLThickness": int(getattr(obj, "PMLThickness", 8)),
                "Background": getattr(obj, "Background", None),
                "bc": {p: str(getattr(obj, p)) for p in self._bc_order},
            }

        def _write_to_obj(self):
            """Push every widget value onto the Domain object (no transaction)."""
            obj = self.obj
            obj.Dx = "{} mm".format(self._dx.value())
            obj.Dy = "{} mm".format(self._dy.value())
            obj.Dz = "{} mm".format(self._dz.value())
            obj.CellsPerWavelength = int(self._cpw.value())
            if hasattr(obj, "CellsPerFeature"):
                obj.CellsPerFeature = int(self._cpf.value())
            obj.UseNonuniformGrid = bool(self._nonuniform.isChecked())
            obj.MaxGradingRatio = float(self._ratio.value())
            if hasattr(obj, "MinCellSize"):
                obj.MinCellSize = "{} mm".format(self._min_cell.value())
            if hasattr(obj, "CurvatureRefinement"):
                obj.CurvatureRefinement = float(self._curvature.value())
            for prop, spin in self._spacings.items():
                setattr(obj, prop, "{} mm".format(spin.value()))
            obj.PMLThickness = int(self._dpml.value())
            obj.Background = self._selected_background()
            for prop, combo in self._combos.items():
                # A locked port face shows a label that is not a valid BC value;
                # leave the stored property alone (the job builder overrides that
                # face anyway, via ``domain_grid_params``).
                if not self._is_port_combo(prop):
                    setattr(obj, prop, combo.currentText())

        def _selected_background(self):
            """The Material (or None = vacuum) chosen in the background combo."""
            idx = self._background.currentIndex()
            if 0 <= idx < len(self._bg_values):
                return self._bg_values[idx]
            return None

        def _restore(self):
            """Restore the pre-edit state captured in :meth:`_snapshot`."""
            obj = self.obj
            o = self._orig
            obj.Dx = "{} mm".format(o["Dx"])
            obj.Dy = "{} mm".format(o["Dy"])
            obj.Dz = "{} mm".format(o["Dz"])
            obj.CellsPerWavelength = o["CellsPerWavelength"]
            if hasattr(obj, "CellsPerFeature"):
                obj.CellsPerFeature = o["CellsPerFeature"]
            obj.UseNonuniformGrid = o["UseNonuniformGrid"]
            obj.MaxGradingRatio = o["MaxGradingRatio"]
            if hasattr(obj, "MinCellSize"):
                obj.MinCellSize = "{} mm".format(o["MinCellSize"])
            if hasattr(obj, "CurvatureRefinement"):
                obj.CurvatureRefinement = o["CurvatureRefinement"]
            for prop, val in o["spacing"].items():
                setattr(obj, prop, "{} mm".format(val))
            obj.PMLThickness = o["PMLThickness"]
            obj.Background = o["Background"]
            for prop, val in o["bc"].items():
                setattr(obj, prop, val)

        def _live_apply(self, *_):
            """Apply the current widgets and recompute so the mesh refreshes."""
            if self._suspend or self._initializing:
                return
            self._write_to_obj()
            self.obj.Document.recompute()
            self._counts.setText(self._counts_text())
            self._refresh_shell()

        def _refresh_shell(self):
            """Update the absorber notice from the geometry as it now stands."""
            from wavesim_gui.commands import active_simulation

            try:
                sim = active_simulation(self.obj.Document)
                messages = shell_geometry_warnings(sim, self.obj)
            except Exception:
                messages = []      # a notice must never break the panel
            if messages:
                self._shell.setText("In the PML: " + " ".join(messages))
            else:
                self._shell.setText("")
            self._shell.setVisible(bool(messages))

        def _default_cell_size_mm(self):
            """Cubic cell size (mm) from the live resolution row, or None.

            Uses the live (uncommitted) resolution and background selections.

            In **electrostatic** mode the driver is the geometry: cells across
            the model's thinnest feature, the same number with the snapper on or
            off, since a permittivity says nothing about what a Laplace solve
            needs from its mesh. None when no body is assigned to a material.

            In **full wave** it is the max frequency: in non-uniform mode the
            coarse *background* resolution (the snapper refines each material
            below it by its index), in uniform mode the global-finest size a
            single spacing needs. None when the max frequency is unset.
            """
            from wavesim_gui.commands import active_simulation

            sim = active_simulation(self.obj.Document)
            if self._es:
                size_m = feature_cell_size_m(
                    sim, self.obj, cells_per_feature=self._cpf.value()
                )
            elif self._nonuniform.isChecked():
                bg = self._selected_background()
                eps_bg = float(getattr(bg, "Eps", 1.0)) if bg is not None else 1.0
                mu_bg = float(getattr(bg, "Mu", 1.0)) if bg is not None else 1.0
                size_m = wavelength_cell_size_m(
                    sim, eps_bg, mu_bg, cells_per_wavelength=self._cpw.value()
                )
            else:
                size_m = default_cell_size_m(
                    sim, cells_per_wavelength=self._cpw.value()
                )
            return None if size_m is None else size_m * _MM_PER_M

        def _fill_cell_size(self, size_mm):
            """Set cubic cells to *size_mm* on all three axes (via the cubic path)."""
            self._cubic.setChecked(True)
            self._dx.setValue(size_mm)
            self._mirror_cubic()

        def _auto_fill_cell_size(self, *_):
            """Re-derive the cell size from the live resolution and apply at once.

            Wired to every input that changes the derived default (either
            resolution box, the background medium, the uniform/non-uniform
            toggle) so the mesh tracks them immediately -- pressing the default
            button is never required. Silent when the driver has nothing to work
            from -- an unset max frequency in full wave, no assigned geometry in
            electrostatics -- and it just applies the other edits. Manual
            dx/dy/dz edits are left alone, so a hand-picked cell size survives
            until one of these inputs changes.
            """
            if self._initializing:
                return
            size_mm = self._default_cell_size_mm()
            if size_mm is not None:
                self._fill_cell_size(size_mm)
            self._live_apply()

        def _apply_default_cell_size(self):
            """Button handler: fill dx/dy/dz from the resolution row.

            Says which driver came up empty, since the two modes fail for
            different reasons and the fix is in a different place.
            """
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets

            size_mm = self._default_cell_size_mm()
            if size_mm is None:
                QtWidgets.QMessageBox.warning(
                    Gui.getMainWindow(), "Wavesim Domain",
                    "Assign at least one body to a material first: an "
                    "electrostatic mesh is sized from the geometry, so with "
                    "nothing assigned there is no feature to resolve."
                    if self._es else
                    "Set a positive max frequency on the Simulation first "
                    "(double-click the Simulation object).",
                )
                return
            self._fill_cell_size(size_mm)
            self._live_apply()

        def accept(self):
            # Values have been live-applied during editing; wrap a final write in
            # one transaction so the whole edit is a single undo step.
            doc = self.obj.Document
            doc.openTransaction("Wavesim: Edit Domain")
            self._write_to_obj()
            doc.commitTransaction()
            doc.recompute()
            Gui.Control.closeDialog()
            return True

        def reject(self):
            # The domain is a permanent singleton and was live-edited during the
            # session, so Cancel restores the values captured when the panel opened.
            self._restore()
            self.obj.Document.recompute()
            Gui.Control.closeDialog()
            return True

        def getStandardButtons(self):
            try:
                from PySide import QtWidgets as _w
            except ImportError:
                from PySide import QtGui as _w
            buttons = _w.QDialogButtonBox.Ok | _w.QDialogButtonBox.Cancel
            return int(getattr(buttons, "value", buttons))

    def _open_domain_panel(obj):
        """Open (or replace) the domain task panel bound to *obj*."""
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskDomainPanel(obj))

    def create_domain(doc, sim):
        """Create the Domain singleton under *sim* and return it.

        Called by the New Simulation command; the domain starts empty and sizes
        itself once material geometry is assigned.
        """
        domain = doc.addObject("App::FeaturePython", "Domain")
        DomainObject(domain)
        domain.Label = "Domain"
        if domain.ViewObject is not None:
            DomainViewProvider(domain.ViewObject)
        sim.addObject(domain)
        return domain
