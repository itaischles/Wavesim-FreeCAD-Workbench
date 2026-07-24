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

Rendering
---------
The domain draws as two *wireframe* boxes (edges only, no fill, so neither
obscures the other or the geometry): the inner domain box and the outer box the
PML layers occupy, in two colours. Alongside them are three fully-transparent
*cell grids* (thin lines spaced ``Dx``/``Dy``/``Dz``) on the domain's three min
faces, so the meshing resolution is visible. All of these are drawn by one
object, so the single "eye" visibility toggle next to Domain shows/hides them
together.

:func:`domain_grid_params` is the single source of truth mapping the per-face
settings to the per-side PML padding (cells), the PML ``faces`` tuple and the PEC
``faces`` tuple that the voxeliser and runner consume.

Units: FreeCAD geometry/properties are in millimetres; the solver works in
metres. ``Dx``/``Dy``/``Dz`` and the ``Spacing*`` gaps are lengths (mm internally);
:func:`cell_sizes_m` / :func:`domain_grid_params` are the conversion points.
"""

import math
import os

import FreeCAD


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_RESOURCES_DIR = os.path.join(_WB_DIR, "Resources")
_DOMAIN_ICON = os.path.join(_RESOURCES_DIR, "domain.png")

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

# Faint grey for the cell-grid planes; cap on lines per axis so a fine grid on a
# large domain can't lock up the viewport.
_GRID_COLOR = (0.55, 0.55, 0.55)
_MAX_GRID_LINES = 400


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

    Hidden geometry properties (``DomainMin``/``Max``, ``PmlMin``/``Max``) carry
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

        # Per-axis node-coordinate arrays (world mm, including the PML pad cells)
        # spanning the padded grid -- the geometric source of truth every
        # downstream consumer derives from. ``execute`` populates them: uniform
        # ticks when UseNonuniformGrid is off, the snapper's lines when on.
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
                "PML absorbing-layer depth, in grid cells, on each PML face",
            )
            obj.PMLThickness = 8

        for _face, prop, doc in _FACE_PROPS:
            if not hasattr(obj, prop):
                obj.addProperty("App::PropertyEnumeration", prop, "Boundary", doc)
                setattr(obj, prop, _BC_CHOICES)
                setattr(obj, prop, "PML")

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
            return

        # TEM-port launch faces are forced to PML here too (not just at run) so
        # the drawn box and the node arrays include the absorber padding that the
        # run's boundary assumes -- keeping the non-uniform grid consistent.
        force_faces = tem_port_faces(sim)
        params = domain_grid_params(obj, force_pml_faces=force_faces)
        sp_lo = tuple(s * _MM_PER_M for s in params["spacing_lo"])
        sp_hi = tuple(s * _MM_PER_M for s in params["spacing_hi"])
        pad_lo, pad_hi = params["pad_lo"], params["pad_hi"]
        dx, dy, dz = (c * _MM_PER_M for c in cell_sizes_m(obj))

        dmin = FreeCAD.Vector(bbox.XMin - sp_lo[0], bbox.YMin - sp_lo[1],
                              bbox.ZMin - sp_lo[2])
        dmax = FreeCAD.Vector(bbox.XMax + sp_hi[0], bbox.YMax + sp_hi[1],
                              bbox.ZMax + sp_hi[2])
        obj.DomainMin, obj.DomainMax = dmin, dmax

        if params["pml_faces"]:
            obj.PmlMin = FreeCAD.Vector(
                dmin.x - pad_lo[0] * dx, dmin.y - pad_lo[1] * dy, dmin.z - pad_lo[2] * dz
            )
            obj.PmlMax = FreeCAD.Vector(
                dmax.x + pad_hi[0] * dx, dmax.y + pad_hi[1] * dy, dmax.z + pad_hi[2] * dz
            )
        else:
            obj.PmlMin = obj.PmlMax = zero  # no PML -> draw no outer box

        # Per-axis node-coordinate arrays (world mm) spanning the padded grid.
        # With UseNonuniformGrid on, the snapper places lines on the geometry's
        # features and grades the spacing; otherwise these are uniform ticks
        # whose extent matches the voxeliser's own ``_grid_extent`` for the same
        # geometry, so the preview and the run agree.
        nodes = None
        if getattr(obj, "UseNonuniformGrid", False):
            from wavesim_gui import gridbuild
            try:
                nodes = gridbuild.build_domain_nodes(
                    sim, obj, force_pml_faces=force_faces
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
        else:
            (nx, ny, nz), (ox, oy, oz) = vox._grid_extent(
                bbox, (dx, dy, dz), sp_lo, sp_hi, pad_lo, pad_hi
            )
            obj.NodesX = [ox + i * dx for i in range(nx + 1)]
            obj.NodesY = [oy + i * dy for i in range(ny + 1)]
            obj.NodesZ = [oz + i * dz for i in range(nz + 1)]
            obj.Nx, obj.Ny, obj.Nz = nx, ny, nz

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


def tem_port_faces(sim):
    """Domain faces (``'x0'``..``'z1'``) carrying a TEM/SPICE-TEM port or a plane
    wave -- every source that launches from a face and must absorb its own launch.

    A waveguide port launches a guided mode and a plane wave a directional sheet;
    both must absorb the backward/reflected wave, so these faces are forced to PML
    everywhere the grid is built (the drawn box, the node arrays, and the run)
    regardless of the Domain's per-face setting -- otherwise a face left (or set)
    to PEC would both trap the wave and, on a non-uniform grid, desync the node
    arrays (no PML pad) from the forced boundary (crash). Lazy imports avoid a
    circular import with the source modules; empty on failure.
    """
    if sim is None:
        return []
    faces = []
    try:
        from wavesim_gui import tem_source as tem_mod
        faces += [str(t.Face) for t in tem_mod.find_tem_sources(sim)]
    except Exception:
        pass
    try:
        from wavesim_gui import spice_port as spice_mod
        faces += [str(p.Face) for p in spice_mod.find_spice_tem_ports(sim)]
    except Exception:
        pass
    try:
        from wavesim_gui import plane_wave as plane_mod
        faces += [str(p.Face) for p in plane_mod.find_plane_waves(sim)]
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


def node_coords_mm(obj):
    """Return the Domain's ``(NodesX, NodesY, NodesZ)`` arrays (world mm lists).

    These are the per-axis grid node coordinates spanning the padded grid (PML
    included), the geometric source of truth ``execute`` maintains. Empty lists
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


def suggested_cell_size_m(sim, domain=None, cells_per_wavelength=None):
    """Cell size (metres) to fill the Domain's ``Dx/Dy/Dz`` from the max frequency.

    * **Uniform grid** -- the global-finest :func:`default_cell_size_m`: one
      constant size must resolve the highest-index medium in the model.
    * **Non-uniform grid** -- the coarse *background* resolution
      (:func:`wavelength_cell_size_m` for the background medium). ``Dx/Dy/Dz``
      then acts as the void/interior target and the snapper refines each material
      body down to its own per-medium requirement (see
      :mod:`wavesim_gui.gridbuild`), so higher-index regions get finer cells
      without meshing the whole domain that fine.

    Returns ``None`` when the max frequency is not positive.
    """
    if domain is None:
        domain = find_domain(sim)
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


def domain_grid_params(domain, force_pml_faces=()):
    """Map a domain's per-face boundary settings to grid/solver parameters.

    Returns a dict with ``spacing_lo``/``spacing_hi`` (per-axis background gaps in
    metres, low/high side), ``pad_lo``/``pad_hi`` (per-axis PML cells),
    ``pml_faces``, ``pec_faces`` and ``d_pml``. This is the one place the per-face
    properties are interpreted, so the drawn boxes, the voxelised grid and the
    runner all agree.

    *force_pml_faces* names faces (``'x0'``..``'z1'``) that must be PML no matter
    what the per-face property says -- TEM waveguide ports pass their launch faces
    so a face left (or set) to PEC still absorbs the launched mode, with its PML
    padding and boundary condition kept consistent (both derived from ``bc`` here).
    """
    d_pml = int(getattr(domain, "PMLThickness", 8))
    bc = {face: getattr(domain, prop) for face, prop, _doc in _FACE_PROPS}
    for face in force_pml_faces or ():
        if face in bc:
            bc[face] = "PML"

    pml_faces = [f for f in _FACES if bc.get(f) == "PML"]
    pec_faces = [f for f in _FACES if bc.get(f) == "PEC"]

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
    spacing = spacings_m(domain)
    return {
        "spacing_lo": (spacing["x0"], spacing["y0"], spacing["z0"]),
        "spacing_hi": (spacing["x1"], spacing["y1"], spacing["z1"]),
        "pad_lo": pad_lo,
        "pad_hi": pad_hi,
        "pml_faces": pml_faces,
        "pec_faces": pec_faces,
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

    Uses the inner domain box corners (``DomainMin``/``DomainMax``), so a TEM
    port placed on a PML face sits at the absorbing region's inner edge.
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

    class DomainViewProvider:
        """Custom coin view provider drawing the domain + PML as wireframes."""

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

            # Three fully-transparent cell grids (thin lines spaced dx/dy/dz) on
            # the domain's min faces, so the meshing is visible. They live under
            # this same display-mode root, so the Domain "eye" toggle hides them
            # together with the boxes.
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
            root.addChild(gsep)

            self._root = root
            vobj.addDisplayMode(root, "Wireframe")
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

        def _grid_segments(self, mn, mx, nodes_x, nodes_y, nodes_z):
            """Line segments for the three orthogonal cell grids on the min faces.

            Returns ``(points, indices)`` for an ``SoIndexedLineSet``: an XY grid
            at ``z = mn.z``, a YZ grid at ``x = mn.x`` and an XZ grid at
            ``y = mn.y``. Grid lines are placed at the explicit per-axis node
            coordinates (``nodes_*``, world mm) clamped to the inner domain box,
            so a non-uniform grid shows its real (graded) spacing. Line counts per
            axis are clamped to :data:`_MAX_GRID_LINES` by decimating.
            """
            pts = []
            idx = []

            def add_line(p0, p1):
                a = len(pts)
                pts.append(p0)
                pts.append(p1)
                idx.extend([a, a + 1, -1])

            def ticks(lo, hi, nodes):
                # Node coordinates inside [lo, hi] (the inner box), always closing
                # on both faces, then decimated so no axis exceeds the line cap.
                tol = 1e-9
                vals = [v for v in nodes if lo - tol <= v <= hi + tol]
                if not vals:
                    vals = [lo, hi]
                if vals[0] > lo + tol:
                    vals.insert(0, lo)
                if vals[-1] < hi - tol:
                    vals.append(hi)
                if len(vals) > _MAX_GRID_LINES:
                    step = int(math.ceil(len(vals) / float(_MAX_GRID_LINES)))
                    decimated = vals[::step]
                    if decimated[-1] != vals[-1]:
                        decimated.append(vals[-1])
                    vals = decimated
                return vals

            xs = ticks(mn.x, mx.x, nodes_x)
            ys = ticks(mn.y, mx.y, nodes_y)
            zs = ticks(mn.z, mx.z, nodes_z)

            # XY grid at z = mn.z
            for x in xs:
                add_line((x, mn.y, mn.z), (x, mx.y, mn.z))
            for y in ys:
                add_line((mn.x, y, mn.z), (mx.x, y, mn.z))
            # YZ grid at x = mn.x
            for y in ys:
                add_line((mn.x, y, mn.z), (mn.x, y, mx.z))
            for z in zs:
                add_line((mn.x, mn.y, z), (mn.x, mx.y, z))
            # XZ grid at y = mn.y
            for x in xs:
                add_line((x, mn.y, mn.z), (x, mn.y, mx.z))
            for z in zs:
                add_line((mn.x, mn.y, z), (mx.x, mn.y, z))
            return pts, idx

        def _rebuild_grid(self):
            obj = getattr(self, "Object", None)
            if obj is None:
                return
            mn, mx = obj.DomainMin, obj.DomainMax
            coords, lines = self._grid_coords, self._grid_lines
            if (mn - mx).Length < 1.0e-9:
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
            pts, idx = self._grid_segments(mn, mx, nodes_x, nodes_y, nodes_z)
            coords.point.setValues(0, len(pts), pts)
            if coords.point.getNum() > len(pts):
                coords.point.deleteValues(len(pts))
            lines.coordIndex.setValues(0, len(idx), idx)
            if lines.coordIndex.getNum() > len(idx):
                lines.coordIndex.deleteValues(len(idx))

        def updateData(self, obj, prop):
            if prop in ("DomainMin", "DomainMax", "PmlMin", "PmlMax"):
                self._rebuild()
            if prop in ("DomainMin", "DomainMax", "Dx", "Dy", "Dz",
                        "NodesX", "NodesY", "NodesZ"):
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

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Domain")
            layout = QtWidgets.QFormLayout(form)

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

            # Cells-per-wavelength + a one-click button that fills the cell sizes
            # from the simulation's max frequency (c / (fmax * N_lambda * n)).
            self._cpw = QtWidgets.QSpinBox()
            self._cpw.setRange(1, 1000)
            self._cpw.setSuffix(" cells/wavelength")
            self._cpw.setValue(int(getattr(obj, "CellsPerWavelength", 20)))

            self._default_btn = QtWidgets.QPushButton(
                "Default from max frequency"
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

            self._counts = QtWidgets.QLabel(self._counts_text())

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
            layout.addRow("", self._default_btn)
            layout.addRow(self._nonuniform)
            layout.addRow("Max grading ratio:", self._ratio)
            layout.addRow("Min cell size:", self._min_cell)
            layout.addRow("Cell counts:", self._counts)
            # Per-face background spacing. Headed, because the six face rows here
            # and the six boundary-condition rows below carry the same labels.
            layout.addRow(QtWidgets.QLabel("<b>Background spacing</b>"))
            layout.addRow(self._same_spacing)
            for face, prop, _doc in _SPACING_PROPS:
                layout.addRow(_FACE_LABELS[face], self._spacings[prop])
            layout.addRow("Background material:", self._background)
            layout.addRow("PML thickness:", self._dpml)

            # Per-face boundary conditions. A "same on all faces" checkbox drives
            # every face from the first (X min) combo and greys the rest out.
            layout.addRow(QtWidgets.QLabel("<b>Boundary conditions</b>"))
            self._bc_order = [prop for _f, prop, _d in _FACE_PROPS]
            self._same_bc = QtWidgets.QCheckBox("Same condition on all faces")
            all_same = len({str(getattr(obj, p)) for p in self._bc_order}) == 1
            self._same_bc.setChecked(all_same)
            layout.addRow(self._same_bc)

            self._combos = {}
            for face, prop, _doc in _FACE_PROPS:
                combo = QtWidgets.QComboBox()
                combo.addItems(_BC_CHOICES)
                combo.setCurrentText(str(getattr(obj, prop)))
                self._combos[prop] = combo
                layout.addRow(_FACE_LABELS[face], combo)

            info = QtWidgets.QLabel(
                "The domain box auto-sizes to the assigned geometry plus the "
                "per-face background spacing (filled with the background "
                "material). PML faces absorb outgoing waves and enlarge the grid; "
                "PEC faces are perfectly-conducting walls. The CFL time step is "
                "computed by the solver and reported in the run summary."
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
            for w in (self._dx, self._dy, self._dz, self._ratio, self._min_cell):
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

        def _counts_text(self, dims=None):
            if dims is not None:
                nx, ny, nz = dims["Nx"], dims["Ny"], dims["Nz"]
            else:
                nx = int(getattr(self.obj, "Nx", 0))
                ny = int(getattr(self.obj, "Ny", 0))
                nz = int(getattr(self.obj, "Nz", 0))
            if nx and ny and nz:
                return "{} x {} x {}  ({:,} cells)".format(nx, ny, nz, nx * ny * nz)
            return "(assign material geometry to size the grid)"

        def _update_counts(self, *_):
            """Recompute the derived cell counts from the spin-box cell sizes.

            Mirrors what ``execute`` derives, but uses the (possibly uncommitted)
            spin-box values so the count label tracks edits immediately. Falls
            back to the stored counts if the cheap bbox derivation is unavailable.
            """
            from wavesim_gui.commands import active_simulation
            from wavesim_gui import voxelize as vox

            sim = active_simulation(self.obj.Document)
            cell_m = (
                self._dx.value() / _MM_PER_M,
                self._dy.value() / _MM_PER_M,
                self._dz.value() / _MM_PER_M,
            )
            dims = vox.derive_grid_dims(sim, cell_m) if sim else None
            self._counts.setText(self._counts_text(dims))

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

        def _on_same_bc(self, checked):
            """Grey out all but the first BC combo and drive them from it."""
            first = self._combos[self._bc_order[0]]
            self._suspend = True
            for prop in self._bc_order[1:]:
                combo = self._combos[prop]
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
            obj.UseNonuniformGrid = bool(self._nonuniform.isChecked())
            obj.MaxGradingRatio = float(self._ratio.value())
            if hasattr(obj, "MinCellSize"):
                obj.MinCellSize = "{} mm".format(self._min_cell.value())
            for prop, spin in self._spacings.items():
                setattr(obj, prop, "{} mm".format(spin.value()))
            obj.PMLThickness = int(self._dpml.value())
            obj.Background = self._selected_background()
            for prop, combo in self._combos.items():
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
            obj.UseNonuniformGrid = o["UseNonuniformGrid"]
            obj.MaxGradingRatio = o["MaxGradingRatio"]
            if hasattr(obj, "MinCellSize"):
                obj.MinCellSize = "{} mm".format(o["MinCellSize"])
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

        def _default_cell_size_mm(self):
            """Cubic cell size (mm) from the max frequency, or None if it's unset.

            Uses the live (uncommitted) cells-per-wavelength and background
            selections. In non-uniform mode the size is the coarse *background*
            resolution (the snapper refines each material below it by its index);
            in uniform mode it is the global-finest size a single spacing needs.
            """
            from wavesim_gui.commands import active_simulation

            sim = active_simulation(self.obj.Document)
            if self._nonuniform.isChecked():
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
            """Re-derive the cell size from the max frequency and apply at once.

            Wired to every input that changes the frequency-driven default (the
            resolution, the background medium, the uniform/non-uniform toggle) so
            the mesh tracks them immediately -- pressing "Default from max
            frequency" is never required. Silent when the max frequency is unset
            (it just applies the other edits). Manual dx/dy/dz edits are left
            alone, so a hand-picked cell size survives until one of these inputs
            changes.
            """
            if self._initializing:
                return
            size_mm = self._default_cell_size_mm()
            if size_mm is not None:
                self._fill_cell_size(size_mm)
            self._live_apply()

        def _apply_default_cell_size(self):
            """Button handler: fill dx/dy/dz from the max frequency (warn if unset)."""
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets

            size_mm = self._default_cell_size_mm()
            if size_mm is None:
                QtWidgets.QMessageBox.warning(
                    Gui.getMainWindow(), "Wavesim Domain",
                    "Set a positive max frequency on the Simulation first "
                    "(double-click the Simulation object).",
                )
                return
            self._fill_cell_size(size_mm)
            self._update_counts()

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
