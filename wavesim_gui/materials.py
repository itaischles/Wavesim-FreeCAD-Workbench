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


# --------------------------------------------------------------------------- #
# Document-object model
# --------------------------------------------------------------------------- #

class MaterialObject:
    """``Proxy`` for a Material document object.

    Properties:
        ``Eps`` / ``Mu`` -- relative permittivity / permeability of the fill.
        ``Pec``          -- if True the bodies are perfect electric conductor;
                            ``Eps``/``Mu`` are ignored and the cells are masked.
        ``Bodies``       -- the CAD objects this material applies to.
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
        if not hasattr(obj, "Bodies"):
            obj.addProperty(
                "App::PropertyLinkList", "Bodies", "Material",
                "CAD bodies this material is assigned to",
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


# --------------------------------------------------------------------------- #
# GUI: view provider, task panel, command
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

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
        """Task-tab panel to edit a Material's parameters and body links.

        Shown via ``Gui.Control.showDialog``. ``accept`` writes the widget
        values back onto the object; ``reject`` removes the object when it was
        created fresh for this edit (so a cancelled assignment leaves no trace).
        """

        def __init__(self, obj, created=False):
            from PySide import QtCore
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets

            self.obj = obj
            self.created = created

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Material")
            layout = QtWidgets.QFormLayout(form)

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

            layout.addRow(self._pec)
            layout.addRow("Relative permittivity (eps_r):", self._eps)
            layout.addRow("Relative permeability (mu_r):", self._mu)
            layout.addRow("Assigned bodies:", self._bodies_label)

            self._pec.toggled.connect(self._on_pec)
            self._on_pec(self._pec.isChecked())

            self.form = form

        def _bodies_text(self):
            bodies = getattr(self.obj, "Bodies", []) or []
            if not bodies:
                return "(none)"
            return ", ".join(b.Label for b in bodies)

        def _on_pec(self, checked):
            # eps/mu are meaningless for a PEC region.
            self._eps.setEnabled(not checked)
            self._mu.setEnabled(not checked)

        def accept(self):
            doc = self.obj.Document
            doc.openTransaction("Wavesim: Edit Material")
            self.obj.Pec = self._pec.isChecked()
            self.obj.Eps = self._eps.value()
            self.obj.Mu = self._mu.value()
            self.obj.Label = "Material ({})".format(_describe(self.obj))
            doc.commitTransaction()
            doc.recompute()
            # Resize the domain to include this material's bodies.
            from wavesim_gui import domain as domain_mod
            domain_mod.notify_materials_changed(doc)
            Gui.Control.closeDialog()
            return True

        def reject(self):
            if self.created:
                # Abandon the just-created object so Cancel undoes the command.
                doc = self.obj.Document
                doc.openTransaction("Wavesim: Cancel Material")
                doc.removeObject(self.obj.Name)
                doc.commitTransaction()
                doc.recompute()
                from wavesim_gui import domain as domain_mod
                domain_mod.notify_materials_changed(doc)
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

    def _open_material_panel(obj, created=False):
        """Open (or replace) the material task panel bound to *obj*."""
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskMaterialPanel(obj, created=created))

    def _selected_bodies():
        """Return selected document objects that carry a solid Shape."""
        bodies = []
        for obj in Gui.Selection.getSelection():
            shape = getattr(obj, "Shape", None)
            if shape is not None and getattr(shape, "Solids", None):
                bodies.append(obj)
        return bodies

    class CommandAssignMaterial:
        """Create a Material from the selection and open its editor panel."""

        def GetResources(self):
            return {
                "Pixmap": _MATERIAL_ICON,
                "MenuText": "Assign Material",
                "ToolTip": "Assign an EM material (eps_r / mu_r / PEC) to the "
                "selected solid bodies",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = active_simulation(doc)
            if sim is None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: create a Simulation before assigning materials.\n"
                )
                return

            bodies = _selected_bodies()
            if not bodies:
                try:
                    from PySide import QtWidgets
                except ImportError:
                    from PySide import QtGui as QtWidgets
                QtWidgets.QMessageBox.information(
                    Gui.getMainWindow(), "Assign Material",
                    "Select one or more solid bodies first, then assign a "
                    "material.",
                )
                return

            doc.openTransaction("Wavesim: Assign Material")
            try:
                mat = doc.addObject("App::FeaturePython", "Material")
                MaterialObject(mat)
                mat.Bodies = bodies
                mat.Label = "Material ({})".format(_describe(mat))
                if mat.ViewObject is not None:
                    MaterialViewProvider(mat.ViewObject)
                materials_group(sim).addObject(mat)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()

            _open_material_panel(mat, created=True)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    Gui.addCommand("Wavesim_AssignMaterial", CommandAssignMaterial())
