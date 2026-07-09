# -*- coding: utf-8 -*-
"""Material assignment for the Wavesim workbench.

A *Material* is a scripted FreeCAD DocumentObject grouped under the simulation's
"Materials" child group. It carries the electromagnetic parameters (relative
permittivity / permeability, or a PEC flag) and a link list to the CAD bodies it
applies to. Session 3's voxeliser (:mod:`wavesim_gui.voxelize`) reads these to
fill the per-cell material arrays the solver consumes.

Editing follows the standard FreeCAD task-panel pattern: ``Wavesim_AssignMaterial``
creates the Material object from the current selection and immediately opens a
panel in the Task tab; OK commits the values, Cancel removes the freshly-created
object. Double-clicking a Material in the tree re-opens the same panel.

Importing this module registers ``Wavesim_AssignMaterial`` with ``Gui.addCommand``
when a GUI is available.
"""

import os

import FreeCAD

from wavesim_gui.commands import active_simulation


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_RESOURCES_DIR = os.path.join(_WB_DIR, "Resources")
_MATERIAL_ICON = os.path.join(_RESOURCES_DIR, "material.png")

# Marker property, mirroring the Simulation container's identity scheme so the
# object is recognisable before its Python proxy is re-attached on reload.
_TYPE_PROP = "WavesimType"
_MATERIAL_TYPE = "Material"

# Name of the child group (created by CommandNewSimulation) that holds materials.
_MATERIALS_GROUP = "Materials"

# Default colour for a freshly-created material, and the colour a body is reset
# to when it is detached from a material (FreeCAD's default light grey).
_DEFAULT_MATERIAL_COLOR = (0.30, 0.60, 0.90)
_DEFAULT_BODY_COLOR = (0.80, 0.80, 0.80)

# Colours for the two materials seeded with every new simulation.
_VACUUM_COLOR = (0.75, 0.90, 1.00)
_PEC_COLOR = (0.78, 0.78, 0.82)


# --------------------------------------------------------------------------- #
# Document-object model
# --------------------------------------------------------------------------- #

class MaterialObject:
    """``Proxy`` for a Material document object.

    Properties:
        ``Eps`` / ``Mu`` -- relative permittivity / permeability of the fill.
        ``Pec``          -- if True the bodies are perfect electric conductor;
                            ``Eps``/``Mu`` are ignored and the cells are masked.
        ``Color``        -- display colour applied to every assigned body.
        ``Bodies``       -- the CAD objects this material applies to. Bodies are
                            attached by dragging them onto the material in the
                            tree (not by pre-selecting them).
    """

    def __init__(self, obj):
        self.Type = _MATERIAL_TYPE
        obj.Proxy = self

        if not hasattr(obj, _TYPE_PROP):
            obj.addProperty(
                "App::PropertyString", _TYPE_PROP, "Wavesim",
                "Marks this object as a Wavesim material",
            )
            setattr(obj, _TYPE_PROP, _MATERIAL_TYPE)
            obj.setEditorMode(_TYPE_PROP, 1)  # read-only identity marker

        if not hasattr(obj, "Eps"):
            obj.addProperty(
                "App::PropertyFloat", "Eps", "Material",
                "Relative permittivity (eps_r)",
            )
            obj.Eps = 1.0
        if not hasattr(obj, "Mu"):
            obj.addProperty(
                "App::PropertyFloat", "Mu", "Material",
                "Relative permeability (mu_r)",
            )
            obj.Mu = 1.0
        if not hasattr(obj, "Pec"):
            obj.addProperty(
                "App::PropertyBool", "Pec", "Material",
                "Perfect electric conductor (overrides Eps/Mu)",
            )
            obj.Pec = False
        if not hasattr(obj, "Color"):
            obj.addProperty(
                "App::PropertyColor", "Color", "Material",
                "Display colour applied to the assigned bodies",
            )
            obj.Color = _DEFAULT_MATERIAL_COLOR
        if not hasattr(obj, "Bodies"):
            obj.addProperty(
                "App::PropertyLinkList", "Bodies", "Material",
                "CAD bodies this material is assigned to (drag bodies from the "
                "tree onto the material to attach them)",
            )

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _MATERIAL_TYPE)

    def execute(self, obj):
        # Pure data object; geometry lives on the linked bodies.
        pass

    def dumps(self):
        return {"Type": getattr(self, "Type", _MATERIAL_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _MATERIAL_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


def is_material(obj):
    """Return True if *obj* is a Wavesim Material object."""
    return getattr(obj, _TYPE_PROP, None) == _MATERIAL_TYPE


def materials_group(sim):
    """Return the "Materials" child group of the Simulation container *sim*.

    Falls back to *sim* itself if the expected child group is missing (e.g. an
    older document), so a material is never left ungrouped.
    """
    if sim is None:
        return None
    for child in sim.Group:
        if child.Name == _MATERIALS_GROUP or child.Label == _MATERIALS_GROUP:
            return child
    return sim


def find_materials(sim):
    """Return all Material objects under the Simulation container *sim*."""
    grp = materials_group(sim)
    if grp is None:
        return []
    return [obj for obj in grp.Group if is_material(obj)]


def _describe(obj):
    """Short human label for a material, e.g. ``PEC`` or ``eps=2.20``."""
    if getattr(obj, "Pec", False):
        return "PEC"
    return "eps={:.3g}".format(getattr(obj, "Eps", 1.0))


def _is_solid_body(obj):
    """Return True if *obj* carries a solid Shape (a valid material body)."""
    shape = getattr(obj, "Shape", None)
    return shape is not None and bool(getattr(shape, "Solids", None))


def _color_rgb(value):
    """Normalise a colour property/tuple to a 3-tuple of floats in 0..1."""
    if not value:
        return _DEFAULT_BODY_COLOR
    return (float(value[0]), float(value[1]), float(value[2]))


def material_color(mat):
    """Return the material's display colour as a 3-tuple of floats in 0..1."""
    return _color_rgb(getattr(mat, "Color", None))


def _set_body_color(body, rgb):
    """Tint a single body's shape with *rgb* (no-op without a view object)."""
    vobj = getattr(body, "ViewObject", None)
    if vobj is None:
        return
    try:
        vobj.ShapeColor = (rgb[0], rgb[1], rgb[2])
    except Exception:
        pass


def apply_material_color(mat):
    """Tint every body assigned to *mat* with the material's colour."""
    rgb = material_color(mat)
    for body in getattr(mat, "Bodies", []) or []:
        _set_body_color(body, rgb)


def _restore_body_color(body):
    """Reset a detached body to the neutral default colour."""
    _set_body_color(body, _DEFAULT_BODY_COLOR)


def _detach_body(body, keep):
    """Remove *body* from every material except *keep* (a body has one owner)."""
    sim = active_simulation(getattr(body, "Document", None))
    for mat in find_materials(sim):
        if mat is keep:
            continue
        bodies = getattr(mat, "Bodies", []) or []
        if body in bodies:
            mat.Bodies = [b for b in bodies if b is not body]


def create_material(doc, sim, label, eps=1.0, mu=1.0, pec=False, color=None):
    """Create a Material under *sim* with the given parameters and return it.

    Attaches the tree view provider when a GUI is available, so it works both
    from the New Material command and when seeding default materials in console
    mode. The caller owns the transaction/recompute.
    """
    mat = doc.addObject("App::FeaturePython", "Material")
    MaterialObject(mat)
    mat.Pec = bool(pec)
    mat.Eps = float(eps)
    mat.Mu = float(mu)
    if color is not None:
        mat.Color = color
    mat.Label = label
    if _GUI_AVAILABLE and mat.ViewObject is not None:
        MaterialViewProvider(mat.ViewObject)
    materials_group(sim).addObject(mat)
    return mat


def create_default_materials(doc, sim):
    """Seed a new simulation with the standard Vacuum and PEC materials.

    Returns ``(vacuum, pec)``. Called by New Simulation so every document starts
    with the two materials users almost always need; Vacuum also becomes the
    Domain's default background (empty-voxel) medium.
    """
    vacuum = create_material(doc, sim, "Vacuum", eps=1.0, mu=1.0, pec=False,
                             color=_VACUUM_COLOR)
    pec = create_material(doc, sim, "PEC", pec=True, color=_PEC_COLOR)
    return vacuum, pec


# --------------------------------------------------------------------------- #
# GUI: view provider, task panel, command
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

    def _after_bodies_changed(doc):
        """Recompute and re-size the domain after a material's bodies change."""
        from wavesim_gui import domain as domain_mod
        domain_mod.notify_materials_changed(doc)

    class MaterialViewProvider:
        """Tree view provider for a Material; double-click opens the editor."""

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            self.ViewObject = vobj
            self.Object = vobj.Object

        def getIcon(self):
            return _MATERIAL_ICON

        def claimChildren(self):
            # Show the assigned bodies nested under the material for context.
            obj = getattr(self, "Object", None)
            return list(getattr(obj, "Bodies", []) or []) if obj else []

        # -- Drag & drop: attach bodies by dropping them onto the material ---- #

        def canDragObjects(self):
            return True

        def canDragObject(self, obj):
            return True

        def dragObject(self, vobj, obj):
            """Detach a body dragged off the material in the tree."""
            mat = vobj.Object
            bodies = getattr(mat, "Bodies", []) or []
            if obj in bodies:
                mat.Bodies = [b for b in bodies if b is not obj]
                _restore_body_color(obj)
                _after_bodies_changed(mat.Document)

        def canDropObjects(self):
            return True

        def canDropObject(self, obj):
            # Only accept solid bodies; reject other tree items (and the
            # material's own children re-dropped on themselves).
            return _is_solid_body(obj)

        def dropObject(self, vobj, obj):
            """Attach a body dropped onto the material and tint it."""
            mat = vobj.Object
            if not _is_solid_body(obj):
                return
            _detach_body(obj, keep=mat)
            bodies = getattr(mat, "Bodies", []) or []
            if obj not in bodies:
                mat.Bodies = list(bodies) + [obj]
            _set_body_color(obj, material_color(mat))
            _after_bodies_changed(mat.Document)

        def setEdit(self, vobj, mode=0):
            _open_material_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_material_panel(vobj.Object)
            return True

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class TaskMaterialPanel:
        """Task-tab panel to create or edit a Material's name/colour/parameters.

        Shown via ``Gui.Control.showDialog``. For a brand-new material *obj* is
        ``None`` and *sim* is the container it will live under: the object is only
        created in ``accept``, so a cancelled (or abandoned) creation leaves no
        trace -- no dangling object, and no name/label counter bump. Editing an
        existing material passes that object as *obj*. Bodies are not edited here;
        they are attached by dragging them onto the material in the tree.
        """

        def __init__(self, obj=None, sim=None, created=False):
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets

            self.obj = obj
            self.sim = sim
            self.created = created  # retained for callers; unused in the new flow
            self._color = (
                material_color(obj) if obj is not None else _DEFAULT_MATERIAL_COLOR
            )

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Material")
            layout = QtWidgets.QFormLayout(form)

            self._name = QtWidgets.QLineEdit(obj.Label if obj is not None else "")

            self._color_btn = QtWidgets.QPushButton()
            self._color_btn.clicked.connect(self._pick_color)
            self._update_color_swatch()

            self._pec = QtWidgets.QCheckBox("Perfect electric conductor (PEC)")
            self._pec.setChecked(bool(getattr(obj, "Pec", False)))

            self._eps = QtWidgets.QDoubleSpinBox()
            self._eps.setRange(1.0, 1.0e6)
            self._eps.setDecimals(4)
            self._eps.setSingleStep(0.1)
            self._eps.setValue(float(getattr(obj, "Eps", 1.0)))

            self._mu = QtWidgets.QDoubleSpinBox()
            self._mu.setRange(1.0e-6, 1.0e6)
            self._mu.setDecimals(4)
            self._mu.setSingleStep(0.1)
            self._mu.setValue(float(getattr(obj, "Mu", 1.0)))

            self._bodies_label = QtWidgets.QLabel(self._bodies_text())
            self._bodies_label.setWordWrap(True)

            layout.addRow("Name:", self._name)
            layout.addRow("Colour:", self._color_btn)
            layout.addRow(self._pec)
            layout.addRow("Relative permittivity (eps_r):", self._eps)
            layout.addRow("Relative permeability (mu_r):", self._mu)
            layout.addRow("Assigned bodies:", self._bodies_label)

            hint = QtWidgets.QLabel(
                "Drag bodies from the model tree onto this material to assign "
                "them; they take on the material colour."
            )
            hint.setWordWrap(True)
            layout.addRow(hint)

            self._pec.toggled.connect(self._on_pec)
            self._on_pec(self._pec.isChecked())

            self.form = form

        def _bodies_text(self):
            bodies = getattr(self.obj, "Bodies", []) or []
            if not bodies:
                return "(none -- drag bodies here)"
            return ", ".join(b.Label for b in bodies)

        def _update_color_swatch(self):
            r, g, b = (int(round(c * 255)) for c in self._color)
            self._color_btn.setText("  {}, {}, {}  ".format(r, g, b))
            self._color_btn.setStyleSheet(
                "background-color: rgb({}, {}, {});".format(r, g, b)
            )

        def _pick_color(self):
            try:
                from PySide import QtWidgets, QtGui
            except ImportError:
                from PySide import QtGui
                QtWidgets = QtGui
            r, g, b = (int(round(c * 255)) for c in self._color)
            chosen = QtWidgets.QColorDialog.getColor(
                QtGui.QColor(r, g, b), self.form, "Material colour"
            )
            if chosen.isValid():
                self._color = (
                    chosen.red() / 255.0,
                    chosen.green() / 255.0,
                    chosen.blue() / 255.0,
                )
                self._update_color_swatch()

        def _on_pec(self, checked):
            # eps/mu are meaningless for a PEC region.
            self._eps.setEnabled(not checked)
            self._mu.setEnabled(not checked)

        def accept(self):
            # The object is created here (not at command time), so a cancelled
            # creation never leaves a dangling material behind.
            new = self.obj is None
            doc = self.sim.Document if new else self.obj.Document
            doc.openTransaction(
                "Wavesim: New Material" if new else "Wavesim: Edit Material"
            )
            try:
                if new:
                    mat = doc.addObject("App::FeaturePython", "Material")
                    MaterialObject(mat)
                    if mat.ViewObject is not None:
                        MaterialViewProvider(mat.ViewObject)
                    materials_group(self.sim).addObject(mat)
                    self.obj = mat
                self.obj.Pec = self._pec.isChecked()
                self.obj.Eps = self._eps.value()
                self.obj.Mu = self._mu.value()
                self.obj.Color = self._color
                name = self._name.text().strip()
                self.obj.Label = name or "Material ({})".format(_describe(self.obj))
                apply_material_color(self.obj)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()
            # Resize the domain to include this material's bodies.
            from wavesim_gui import domain as domain_mod
            domain_mod.notify_materials_changed(doc)
            Gui.Control.closeDialog()
            return True

        def reject(self):
            # Nothing is created until accept, so Cancel just closes the panel --
            # no object to remove, no name/label counter left behind.
            Gui.Control.closeDialog()
            return True

        def getStandardButtons(self):
            try:
                from PySide import QtWidgets as _w
            except ImportError:
                from PySide import QtGui as _w
            buttons = _w.QDialogButtonBox.Ok | _w.QDialogButtonBox.Cancel
            # PySide6/Qt6 yields a StandardButton flag (use .value); PySide2/Qt5
            # yields a plain int already.
            return int(getattr(buttons, "value", buttons))

    def _open_material_panel(obj=None, sim=None, created=False):
        """Open (or replace) the material task panel.

        Pass *obj* to edit an existing material, or *sim* (with ``obj=None``) to
        create a new one -- the object is only materialised when the panel is
        accepted.
        """
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskMaterialPanel(obj, sim=sim, created=created))

    class CommandAssignMaterial:
        """Create an empty Material and open its editor panel.

        Materials are created without pre-selecting any geometry; the user names
        the material, picks its colour, and then drags bodies onto it in the tree
        to assign them.
        """

        def GetResources(self):
            return {
                "Pixmap": _MATERIAL_ICON,
                "MenuText": "New Material",
                "ToolTip": "Create an EM material (eps_r / mu_r / PEC); drag "
                "bodies onto it in the tree to assign them",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = active_simulation(doc)
            if sim is None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: create a Simulation before adding materials.\n"
                )
                return

            # The material object is created only when the panel is accepted, so
            # cancelling leaves no trace (no dangling object, no name counter).
            _open_material_panel(sim=sim)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    Gui.addCommand("Wavesim_AssignMaterial", CommandAssignMaterial())
