# -*- coding: utf-8 -*-
"""Per-body mesh refinement -- "N cells across this body" (FreeCAD side).

The snapper (:mod:`wavesim_gui.gridbuild`) already refines automatically: each
material body tightens the coarse target over its own band to its per-medium
wavelength resolution, and a curved face can ask for a fine cell at its tangent
plane. This module is the **manual override** on top of that -- the user picks a
body in the tree, presses *Refine Body Mesh*, and says how many cells they want
across it on each axis.

The request is stored as three integer properties **on the body itself**, not on
a Wavesim object:

* it belongs to one lump of geometry, the way a conductor's potential does (see
  ``materials.POTENTIAL_PROP``), and it must follow that body when it is moved,
  copied or re-assigned to a different material;
* a real property is visible in the property editor and can be driven by a
  FreeCAD expression, so a parameter sweep can reach it;
* a body carrying none is *absent*, not zero, so every existing model's mesh is
  bit-identical until a refinement is asked for.

How it reaches the mesh
-----------------------
``gridbuild.collect_refinement_caps`` turns each request into the same per-axis
``(lo_mm, hi_mm, target_mm)`` cap that ``collect_material_caps`` already emits,
with ``target = span / N`` over the body's own bounding box. Nothing else in the
snapper changes: ``_gap_coarse`` tightens every gap whose midpoint falls in the
interval, and the graded fill spreads the fine cells back out to the background
at ``MaxGradingRatio``. The body's bbox faces are forced grid lines already
(``collect_axis_snaps`` walks the same ``_exact_bbox``), which is the invariant
the midpoint test relies on.

Two consequences worth knowing, both surfaced by the task panel:

* **N is a floor, not an exact count.** A body whose span carries no interior
  forced line gets exactly N cells. But planar faces, cylinder silhouettes and
  circle centre lines already split that span, and each sub-gap is then tiled to
  at least its own share -- so a body with internal features gets *more*. (That
  it is a floor at all takes ``gridbuild._exact_target``: the graded fill's
  ``floor``-and-stretch would otherwise leave a split span **coarser** than
  asked, measured as 3 cells for a request of 5.)
* **The Domain's ``MinCellSize`` wins.** ``build_axis_nodes`` floors every gap
  target by it, so a minimum cell coarser than ``span / N`` silently clips the
  refinement.

Refining costs the timestep -- the finest cell in the whole domain sets the CFL
step -- which is why this is opt-in per body and why the panel prints the
resulting minimum cell and dt before the edit is committed.

Units: millimetres throughout (FreeCAD geometry); the metre conversion happens in
``domain.node_coords_m`` / the voxeliser, as everywhere else.

Importing this module registers ``Wavesim_RefineBody`` with ``Gui.addCommand``
when a GUI is available.
"""

import os

import FreeCAD


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_ICONS_DIR = os.path.join(_WB_DIR, "Resources", "icons")
_REFINE_ICON = os.path.join(_ICONS_DIR, "refine.svg")

_MM_PER_M = 1000.0

# The three per-axis cell-count properties, in x/y/z order. Added to a *foreign*
# document object (a Part::Box, a PartDesign Body, ...), so the names are
# namespaced -- exactly like ``materials.POTENTIAL_PROP``.
CELL_COUNT_PROPS = ("WavesimCellsX", "WavesimCellsY", "WavesimCellsZ")

_PROP_DOC = (
    "Wavesim mesh refinement: minimum number of grid cells across this body's "
    "bounding box on {} (0 = no refinement on this axis). The snapper meshes "
    "the body's band at no coarser than span/N and grades back out to the "
    "background; interior features can push the real count higher, and the "
    "Domain's MinCellSize can clip it."
)


# --------------------------------------------------------------------------- #
# The per-body properties
# --------------------------------------------------------------------------- #

def ensure_cell_count_props(body):
    """Add the three cell-count properties to *body* if it has none.

    Idempotent and guarded: a body type that refuses dynamic properties must not
    break the command, it simply cannot be refined.
    """
    if body is None:
        return body
    for prop, axis in zip(CELL_COUNT_PROPS, ("x", "y", "z")):
        if hasattr(body, prop):
            continue
        try:
            body.addProperty("App::PropertyInteger", prop, "Wavesim",
                             _PROP_DOC.format(axis))
            setattr(body, prop, 0)
        except Exception:
            pass
    return body


def body_cell_counts(body):
    """``(nx, ny, nz)`` requested for *body*; zeros when it carries no request.

    Zero on an axis means "no manual refinement there", which is what a body
    with no properties at all reports -- so the absent case and the explicitly
    cleared case are the same case, and neither emits a cap.
    """
    counts = []
    for prop in CELL_COUNT_PROPS:
        try:
            n = int(getattr(body, prop, 0) or 0)
        except Exception:
            n = 0
        counts.append(max(n, 0))
    return tuple(counts)


def set_body_cell_counts(body, counts):
    """Store ``(nx, ny, nz)`` on *body*, adding the properties if missing."""
    ensure_cell_count_props(body)
    for prop, n in zip(CELL_COUNT_PROPS, counts):
        if hasattr(body, prop):
            try:
                setattr(body, prop, max(int(n), 0))
            except Exception:
                pass


def is_refined(body):
    """True when *body* asks for manual refinement on at least one axis."""
    return any(body_cell_counts(body))


def refined_bodies(sim):
    """``[(body, (nx, ny, nz)), ...]`` for every refined body under *sim*.

    Enumerated through the material bodies, which is the only set of geometry
    the simulation knows about -- a body not assigned to a Material is not in
    the domain and has no band to refine.
    """
    from wavesim_gui import materials as materials_mod

    out = []
    seen = set()
    for mat in materials_mod.find_materials(sim):
        for body in getattr(mat, "Bodies", []) or []:
            if body.Name in seen:
                continue
            seen.add(body.Name)
            counts = body_cell_counts(body)
            if any(counts):
                out.append((body, counts))
    return out


# --------------------------------------------------------------------------- #
# Geometry helpers (shared by the panel and the command)
# --------------------------------------------------------------------------- #

def body_spans_mm(body):
    """Per-axis ``(lo_mm, hi_mm)`` of *body*'s exact bounding box, or ``None``.

    Uses ``gridbuild._exact_bbox`` rather than ``Shape.BoundBox`` so the span
    reported here is the very one the caps are built from -- and the one whose
    faces are already forced grid lines. A tessellated box is undersized on a
    curved body, which would make ``span / N`` disagree with the mesh.
    """
    from wavesim_gui import gridbuild

    shape = getattr(body, "Shape", None)
    if shape is None or not getattr(shape, "Solids", None):
        return None
    bb = gridbuild._exact_bbox(shape)
    return ((bb.XMin, bb.XMax), (bb.YMin, bb.YMax), (bb.ZMin, bb.ZMax))


def measured_cell_counts(domain, body):
    """How many grid cells currently lie inside *body*'s span, per axis.

    Counts only cells wholly within the bounding box, from the Domain's node
    arrays, so it reports the mesh the body actually has right now. Used to
    pre-fill the panel: the useful starting point for "make this finer" is what
    it is already. Zeros when there is no mesh or no shape yet.
    """
    from wavesim_gui import domain as domain_mod

    spans = body_spans_mm(body)
    if spans is None or domain is None:
        return (0, 0, 0)
    nodes = domain_mod.node_coords_mm(domain)
    counts = []
    for axis, (lo, hi) in zip(nodes, spans):
        tol = 1.0e-9 * max(abs(lo), abs(hi), 1.0)
        counts.append(sum(1 for i in range(len(axis) - 1)
                          if axis[i] >= lo - tol and axis[i + 1] <= hi + tol))
    return tuple(counts)


def target_sizes_mm(body, counts):
    """Per-axis requested cell size ``span / N`` (mm); ``None`` where N is 0."""
    spans = body_spans_mm(body)
    if spans is None:
        return (None, None, None)
    out = []
    for (lo, hi), n in zip(spans, counts):
        out.append((hi - lo) / n if n > 0 and hi > lo else None)
    return tuple(out)


# --------------------------------------------------------------------------- #
# GUI: task panel + command
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


def selected_bodies():
    """Solid document objects currently selected in the tree / 3D view.

    Sub-element picks (a face, an edge) resolve to their owning object, so
    clicking a face of a body in the 3D view refines that body -- the whole
    body, since a bounding-box cap has no smaller unit to work in.
    """
    if not _GUI_AVAILABLE:
        return []
    bodies = []
    for sel in Gui.Selection.getSelectionEx():
        obj = getattr(sel, "Object", None)
        if obj is None or obj in bodies:
            continue
        shape = getattr(obj, "Shape", None)
        if shape is None or not getattr(shape, "Solids", None):
            continue
        bodies.append(obj)
    return bodies


def _material_body_names(sim):
    """Internal names of every body assigned to a Material under *sim*."""
    from wavesim_gui import materials as materials_mod

    names = set()
    for mat in materials_mod.find_materials(sim):
        for body in getattr(mat, "Bodies", []) or []:
            names.add(body.Name)
    return names


if _GUI_AVAILABLE:

    from wavesim_gui.commands import active_simulation

    class TaskRefinePanel:
        """Task-tab panel: cells across the selected body/bodies, per axis.

        Live-applies like the Domain panel -- every edit writes the properties
        and recomputes, so the 3D grid, the cell counts and the timestep shown
        update as the numbers are typed. Cancel restores what the bodies carried
        when the panel opened.
        """

        def __init__(self, bodies, sim):
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets

            from wavesim_gui import domain as domain_mod

            self.bodies = list(bodies)
            self.sim = sim
            self.domain = domain_mod.find_domain(sim)
            self.doc = self.bodies[0].Document
            self._initializing = True
            # Pre-edit state, for Cancel. Captured before any property is added,
            # so a body that had no refinement at all goes back to zeros.
            self._orig = {b.Name: body_cell_counts(b) for b in self.bodies}

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Body Mesh Refinement")
            layout = QtWidgets.QFormLayout(form)

            names = ", ".join(b.Label for b in self.bodies)
            header = QtWidgets.QLabel(names)
            header.setWordWrap(True)
            layout.addRow("Body:", header)

            # Pre-fill from the first body's stored request, falling back to the
            # mesh it currently has: the natural starting point for "make this
            # finer" is the count it is already at.
            first = self.bodies[0]
            current = body_cell_counts(first)
            if not any(current):
                current = measured_cell_counts(self.domain, first)

            self._spins = []
            for axis, value in zip(("X", "Y", "Z"), current):
                spin = QtWidgets.QSpinBox()
                spin.setRange(0, 100000)
                spin.setSuffix(" cells")
                spin.setSpecialValueText("off")  # 0 reads as "no refinement"
                spin.setValue(int(value))
                spin.valueChanged.connect(self._live_apply)
                layout.addRow("Cells across {}:".format(axis), spin)
                self._spins.append(spin)

            self._clear_btn = QtWidgets.QPushButton("Clear refinement")
            self._clear_btn.setToolTip(
                "Set all three axes to 0, returning these bodies to the "
                "automatic (material / curvature) refinement only"
            )
            self._clear_btn.clicked.connect(self._on_clear)
            layout.addRow("", self._clear_btn)

            self._sizes = QtWidgets.QLabel()
            self._sizes.setWordWrap(True)
            layout.addRow("Cell size:", self._sizes)

            self._grid = QtWidgets.QLabel()
            self._grid.setWordWrap(True)
            layout.addRow("Grid:", self._grid)

            self._warning = QtWidgets.QLabel()
            self._warning.setWordWrap(True)
            self._warning.setStyleSheet("color: #b06000;")
            layout.addRow("", self._warning)

            self.form = form
            self._initializing = False
            self._refresh_labels()

        # -- reads ------------------------------------------------------- #

        def _counts(self):
            return tuple(int(s.value()) for s in self._spins)

        def _sizes_text(self):
            """Requested ``span / N`` per axis, for the first selected body."""
            counts = self._counts()
            if not any(counts):
                return "no manual refinement (automatic sizing only)"
            sizes = target_sizes_mm(self.bodies[0], counts)
            parts = []
            for axis, size in zip("xyz", sizes):
                parts.append("{} {:.4g} mm".format(axis, size)
                             if size is not None else "{} -".format(axis))
            return "at most " + ", ".join(parts)

        def _grid_text(self):
            """The Domain's cell counts, finest cell and CFL step, as recomputed.

            Read back from the object rather than re-derived here, for the reason
            ``TaskDomainPanel._counts_text`` gives: with the snapper on, only
            ``execute`` knows where the grid lines actually land. The timestep is
            the point of the readout -- refining costs it, and the whole domain
            pays.
            """
            from wavesim_gui import domain as domain_mod

            dom = self.domain
            if dom is None:
                return "(no domain)"
            nx = int(getattr(dom, "Nx", 0))
            ny = int(getattr(dom, "Ny", 0))
            nz = int(getattr(dom, "Nz", 0))
            if not (nx and ny and nz):
                return "(assign material geometry to size the grid)"
            finest = min(domain_mod.min_spacings_m(dom)) * _MM_PER_M
            return ("{} x {} x {}  ({:,} cells)\n"
                    "finest cell {:.4g} mm, dt {:.4g} ps".format(
                        nx, ny, nz, nx * ny * nz, finest,
                        domain_mod.cfl_dt(dom) * 1.0e12))

        def _warning_text(self):
            """Whatever would silently make the request not do what it says."""
            from wavesim_gui import domain as domain_mod

            counts = self._counts()
            if not any(counts) or self.domain is None:
                return ""
            notes = []
            min_cell_mm = domain_mod.min_cell_size_m(self.domain) * _MM_PER_M
            sizes = target_sizes_mm(self.bodies[0], counts)
            clipped = [a for a, s in zip("xyz", sizes)
                       if s is not None and min_cell_mm > 0.0 and s < min_cell_mm]
            if clipped:
                notes.append(
                    "The Domain's minimum cell size ({:.4g} mm) is coarser than "
                    "the request on {} -- raise it or the refinement is clipped."
                    .format(min_cell_mm, "/".join(clipped)))
            if not getattr(self.domain, "UseNonuniformGrid", False):
                notes.append(
                    "The Domain's non-uniform grid is off, so this request is "
                    "ignored; turn it on in the Domain panel.")
            measured = measured_cell_counts(self.domain, self.bodies[0])
            over = ["{}: asked {}, got {}".format(a, n, m)
                    for a, n, m in zip("xyz", counts, measured) if n and m > n]
            if over:
                notes.append(
                    "Interior features split the body's span, so it meshes finer "
                    "than asked (" + "; ".join(over) + ").")
            return "\n".join(notes)

        def _refresh_labels(self):
            self._sizes.setText(self._sizes_text())
            self._grid.setText(self._grid_text())
            self._warning.setText(self._warning_text())

        # -- writes ------------------------------------------------------ #

        def _write_to_bodies(self, counts):
            for body in self.bodies:
                set_body_cell_counts(body, counts)

        def _live_apply(self, *_):
            """Apply the current spin values and recompute so the mesh refreshes."""
            if self._initializing:
                return
            from wavesim_gui import domain as domain_mod

            self._write_to_bodies(self._counts())
            domain_mod.notify_domain_inputs_changed(self.doc)
            self._refresh_labels()

        def _on_clear(self):
            for spin in self._spins:
                spin.blockSignals(True)
                spin.setValue(0)
                spin.blockSignals(False)
            self._live_apply()

        def accept(self):
            # Live-applied while editing; wrap the final write in one transaction
            # so the whole edit is a single undo step.
            from wavesim_gui import domain as domain_mod

            self.doc.openTransaction("Wavesim: Refine Body Mesh")
            self._write_to_bodies(self._counts())
            self.doc.commitTransaction()
            domain_mod.notify_domain_inputs_changed(self.doc)
            Gui.Control.closeDialog()
            return True

        def reject(self):
            from wavesim_gui import domain as domain_mod

            for body in self.bodies:
                set_body_cell_counts(body, self._orig[body.Name])
            domain_mod.notify_domain_inputs_changed(self.doc)
            Gui.Control.closeDialog()
            return True

        def getStandardButtons(self):
            try:
                from PySide import QtWidgets as _w
            except ImportError:
                from PySide import QtGui as _w
            buttons = _w.QDialogButtonBox.Ok | _w.QDialogButtonBox.Cancel
            return int(getattr(buttons, "value", buttons))

    class CommandRefineBody:
        """Refine the mesh over the body/bodies selected in the tree."""

        def GetResources(self):
            return {
                "Pixmap": _REFINE_ICON,
                "MenuText": "Refine Body Mesh",
                "ToolTip": "Mesh the selected body with a chosen number of "
                           "cells across its bounding box in x/y/z",
            }

        def Activated(self):
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets

            doc = FreeCAD.ActiveDocument
            sim = active_simulation(doc)
            if sim is None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: create a Simulation before refining a body.\n"
                )
                return

            bodies = selected_bodies()
            if not bodies:
                QtWidgets.QMessageBox.warning(
                    Gui.getMainWindow(), "Wavesim Refine Body",
                    "Select a solid body in the tree (or a face of one in the "
                    "3D view) first.",
                )
                return

            # A body outside every Material is not in the domain at all: it is
            # not voxelised, its bounding box is not a forced grid line, and
            # there is no band of mesh to refine. Refuse it here rather than
            # store a request the snapper will never read.
            assigned = _material_body_names(sim)
            missing = [b.Label for b in bodies if b.Name not in assigned]
            if missing:
                QtWidgets.QMessageBox.warning(
                    Gui.getMainWindow(), "Wavesim Refine Body",
                    "These bodies are not assigned to a Material, so they are "
                    "not part of the simulation and cannot be refined:\n\n"
                    + "\n".join(missing)
                    + "\n\nDrag them onto a Material in the tree first.",
                )
                return

            Gui.Control.closeDialog()
            Gui.Control.showDialog(TaskRefinePanel(bodies, sim))

        def IsActive(self):
            return (active_simulation(FreeCAD.ActiveDocument) is not None
                    and bool(selected_bodies()))

    Gui.addCommand("Wavesim_RefineBody", CommandRefineBody())
