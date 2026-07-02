# -*- coding: utf-8 -*-
"""Simulation domain (grid + boundaries) for the Wavesim workbench.

The *Domain* is the single object that defines the FDTD grid: a singleton scripted
DocumentObject created automatically when a Simulation is created. It unifies what
were previously separate "Grid" and "Domain" concepts. It holds:

* the cell sizes ``Dx``/``Dy``/``Dz`` and the derived cell counts ``Nx``/``Ny``/``Nz``;
* a ``Spacing`` air gap around the geometry;
* per-face boundary conditions (PML or PEC) and the PML cell thickness.

The domain box auto-sizes to bound every material-assigned body plus ``Spacing``;
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
metres. ``Dx``/``Dy``/``Dz``/``Spacing`` are lengths (mm internally);
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
_DOMAIN_ICON = os.path.join(_RESOURCES_DIR, "boundary.png")

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
        ``Spacing``              -- air gap added around the material bounds.
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

        if not hasattr(obj, "Spacing"):
            obj.addProperty(
                "App::PropertyLength", "Spacing", "Domain",
                "Air gap added around the material bounds on every side",
            )
            obj.Spacing = "5 mm"
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
            # No geometry yet: empty boxes, zero counts.
            obj.DomainMin = obj.DomainMax = zero
            obj.PmlMin = obj.PmlMax = zero
            obj.Nx = obj.Ny = obj.Nz = 0
            return

        params = domain_grid_params(obj)
        sp_mm = params["spacing_m"] * _MM_PER_M
        pad_lo, pad_hi = params["pad_lo"], params["pad_hi"]
        dx, dy, dz = (c * _MM_PER_M for c in cell_sizes_m(obj))

        dmin = FreeCAD.Vector(bbox.XMin - sp_mm, bbox.YMin - sp_mm, bbox.ZMin - sp_mm)
        dmax = FreeCAD.Vector(bbox.XMax + sp_mm, bbox.YMax + sp_mm, bbox.ZMax + sp_mm)
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

        dims = vox.derive_grid_dims(sim, cell_sizes_m(obj))
        if dims is not None:
            obj.Nx, obj.Ny, obj.Nz = dims["Nx"], dims["Ny"], dims["Nz"]

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


def cell_sizes_m(obj):
    """Return the Domain's ``(dx, dy, dz)`` cell sizes in metres."""
    return (
        float(obj.Dx.Value) / _MM_PER_M,
        float(obj.Dy.Value) / _MM_PER_M,
        float(obj.Dz.Value) / _MM_PER_M,
    )


# CFL parameters mirroring the solver's ``wavesim.grid.make_grid``: dt is the
# conservative 3D Courant limit, set purely by the cell sizes (independent of the
# cell counts, and of whether the domain is 2D). Duplicated here because the
# solver package cannot be imported into FreeCAD's Python.
_C0 = 299792458.0   # speed of light, m/s (wavesim.constants.C0)
_CFL = 0.99


def cfl_dt(domain):
    """Return the solver's CFL time step (seconds) for *domain*'s cell sizes.

    Mirrors ``wavesim.grid.make_grid``::

        dt = CFL / (c * sqrt(1/dx^2 + 1/dy^2 + 1/dz^2)),  CFL = 0.99

    so the step count shown in the GUI matches what the runner actually uses.
    """
    dx, dy, dz = cell_sizes_m(domain)
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


def domain_grid_params(domain):
    """Map a domain's per-face boundary settings to grid/solver parameters.

    Returns a dict with ``spacing_m``, ``pad_lo``/``pad_hi`` (per-axis PML cells),
    ``pml_faces``, ``pec_faces`` and ``d_pml``. This is the one place the per-face
    properties are interpreted, so the drawn boxes, the voxelised grid and the
    runner all agree.
    """
    d_pml = int(getattr(domain, "PMLThickness", 8))
    bc = {face: getattr(domain, prop) for face, prop, _doc in _FACE_PROPS}

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
    spacing_m = float(domain.Spacing.Value) / _MM_PER_M
    return {
        "spacing_m": spacing_m,
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

        def _grid_segments(self, mn, mx, dx, dy, dz):
            """Line segments for the three orthogonal cell grids on the min faces.

            Returns ``(points, indices)`` for an ``SoIndexedLineSet``: an XY grid
            at ``z = mn.z`` (lines every dx and dy), a YZ grid at ``x = mn.x``
            (dy/dz) and an XZ grid at ``y = mn.y`` (dx/dz). Line counts per axis
            are clamped to :data:`_MAX_GRID_LINES`.
            """
            pts = []
            idx = []

            def add_line(p0, p1):
                a = len(pts)
                pts.append(p0)
                pts.append(p1)
                idx.extend([a, a + 1, -1])

            def ticks(lo, hi, step):
                if step <= 0.0:
                    return [lo, hi]
                n = min(int(math.floor((hi - lo) / step + 1e-9)), _MAX_GRID_LINES)
                vals = [lo + i * step for i in range(n + 1)]
                if vals[-1] < hi - 1e-9:
                    vals.append(hi)  # always close on the domain face
                return vals

            xs = ticks(mn.x, mx.x, dx)
            ys = ticks(mn.y, mx.y, dy)
            zs = ticks(mn.z, mx.z, dz)

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
            pts, idx = self._grid_segments(
                mn, mx, float(obj.Dx.Value), float(obj.Dy.Value), float(obj.Dz.Value)
            )
            coords.point.setValues(0, len(pts), pts)
            if coords.point.getNum() > len(pts):
                coords.point.deleteValues(len(pts))
            lines.coordIndex.setValues(0, len(idx), idx)
            if lines.coordIndex.getNum() > len(idx):
                lines.coordIndex.deleteValues(len(idx))

        def updateData(self, obj, prop):
            if prop in ("DomainMin", "DomainMax", "PmlMin", "PmlMax"):
                self._rebuild()
            if prop in ("DomainMin", "DomainMax", "Dx", "Dy", "Dz"):
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

            self._counts = QtWidgets.QLabel(self._counts_text())

            self._spacing = QtWidgets.QDoubleSpinBox()
            self._spacing.setRange(0.0, 1.0e6)
            self._spacing.setDecimals(4)
            self._spacing.setSuffix(" mm")
            self._spacing.setSingleStep(0.5)
            self._spacing.setValue(float(obj.Spacing.Value))

            self._dpml = QtWidgets.QSpinBox()
            self._dpml.setRange(1, 100)
            self._dpml.setSuffix(" cells")
            self._dpml.setValue(int(getattr(obj, "PMLThickness", 8)))

            # Background (empty-voxel) material: a dropdown of the simulation's
            # materials, with a leading vacuum entry for "unset". Index 0 maps to
            # None (vacuum); index i maps to self._materials[i - 1].
            from wavesim_gui.commands import active_simulation
            from wavesim_gui import materials as materials_mod

            sim = active_simulation(obj.Document)
            self._materials = materials_mod.find_materials(sim) if sim else []
            self._background = QtWidgets.QComboBox()
            self._background.addItem("Vacuum (eps=1, mu=1)")
            for mat in self._materials:
                self._background.addItem(mat.Label)
            current_bg = getattr(obj, "Background", None)
            bg_index = 0
            for i, mat in enumerate(self._materials, start=1):
                if mat is current_bg:
                    bg_index = i
                    break
            self._background.setCurrentIndex(bg_index)

            layout.addRow(self._cubic)
            layout.addRow("Cell size dx:", self._dx)
            layout.addRow("Cell size dy:", self._dy)
            layout.addRow("Cell size dz:", self._dz)
            layout.addRow("Cell counts:", self._counts)
            layout.addRow("Air spacing:", self._spacing)
            layout.addRow("Background material:", self._background)
            layout.addRow("PML thickness:", self._dpml)

            self._combos = {}
            labels = {
                "BoundaryXMin": "X min (x0):", "BoundaryXMax": "X max (x1):",
                "BoundaryYMin": "Y min (y0):", "BoundaryYMax": "Y max (y1):",
                "BoundaryZMin": "Z min (z0):", "BoundaryZMax": "Z max (z1):",
            }
            for _face, prop, _doc in _FACE_PROPS:
                combo = QtWidgets.QComboBox()
                combo.addItems(_BC_CHOICES)
                combo.setCurrentText(str(getattr(obj, prop)))
                self._combos[prop] = combo
                layout.addRow(labels[prop], combo)

            info = QtWidgets.QLabel(
                "The domain box auto-sizes to the assigned geometry plus the air "
                "spacing. PML faces absorb outgoing waves and enlarge the grid; "
                "PEC faces are perfectly-conducting walls. The CFL time step is "
                "computed by the solver and reported in the run summary."
            )
            info.setWordWrap(True)
            layout.addRow(info)

            self._cubic.toggled.connect(self._on_cubic)
            self._dx.valueChanged.connect(self._mirror_cubic)
            # Recompute the derived cell counts live as the cell sizes change,
            # before OK is pressed, so the user sees the grid resolution update.
            self._dx.valueChanged.connect(self._update_counts)
            self._dy.valueChanged.connect(self._update_counts)
            self._dz.valueChanged.connect(self._update_counts)
            self._on_cubic(self._cubic.isChecked())

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
            self._dy.setEnabled(not checked)
            self._dz.setEnabled(not checked)
            self._mirror_cubic()

        def _mirror_cubic(self, *_):
            if self._cubic.isChecked():
                self._dy.setValue(self._dx.value())
                self._dz.setValue(self._dx.value())

        def accept(self):
            doc = self.obj.Document
            doc.openTransaction("Wavesim: Edit Domain")
            self.obj.Dx = "{} mm".format(self._dx.value())
            self.obj.Dy = "{} mm".format(self._dy.value())
            self.obj.Dz = "{} mm".format(self._dz.value())
            self.obj.Spacing = "{} mm".format(self._spacing.value())
            self.obj.PMLThickness = int(self._dpml.value())
            bg_index = self._background.currentIndex()
            self.obj.Background = (
                None if bg_index == 0 else self._materials[bg_index - 1]
            )
            for prop, combo in self._combos.items():
                setattr(self.obj, prop, combo.currentText())
            doc.commitTransaction()
            doc.recompute()
            Gui.Control.closeDialog()
            return True

        def reject(self):
            # The domain is a permanent singleton, so Cancel just discards edits.
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
