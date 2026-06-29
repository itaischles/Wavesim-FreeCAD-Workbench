# -*- coding: utf-8 -*-
"""Point source + excitation for the Wavesim workbench (Session 6).

A *Source* is a scripted FreeCAD DocumentObject grouped under the simulation's
"Sources" child group. It bundles the two halves of a soft point excitation:

* the **source** -- which field component to drive ('Ex'..'Hz') and where (a
  world-coordinate position, snapped to the nearest grid cell at run time);
* the **excitation** -- the temporal waveform. For now a Gaussian pulse defined
  by its target maximum frequency ``Fmax`` and ``Amplitude`` (mapping to the
  solver's ``GaussianPulse.for_fmax``). The enumeration leaves room for more
  waveforms later.

This replaces the Session-2/3 hardcoded centre source: :mod:`wavesim_gui.voxelize`
now reads the first Source under the simulation when building the job.

Rendering
---------
The source draws as a single green point marker in the 3D view at its world
position. Like every scripted ViewProvider it carries the standard ``Visibility``
property, so the tree's "eye" toggle shows/hides it. Double-clicking the source
in the tree (or Edit) opens a Task-tab panel to change its component, position
and excitation parameters.

Units: FreeCAD geometry/properties are in millimetres; the solver works in
metres. ``Position`` is stored in mm; :func:`source_spec` converts to metres and
into the solver frame (measured from the domain origin) for the runner.

Importing this module registers ``Wavesim_AddSource`` with ``Gui.addCommand``
when a GUI is available.
"""

import os

import FreeCAD

from wavesim_gui.commands import active_simulation
from wavesim_gui import units


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_RESOURCES_DIR = os.path.join(_WB_DIR, "Resources")
_SOURCE_ICON = os.path.join(_RESOURCES_DIR, "port.png")

# Marker property, mirroring the other entities' identity scheme so the object is
# recognisable before its Python proxy is re-attached on reload.
_TYPE_PROP = "WavesimType"
_SOURCE_TYPE = "Source"

# Name of the child group (created by CommandNewSimulation) holding sources.
_SOURCES_GROUP = "Sources"

# Field components a point source may drive.
_COMPONENTS = ["Ex", "Ey", "Ez", "Hx", "Hy", "Hz"]

# Available excitation waveforms. Only the Gaussian pulse is wired today; the
# enumeration leaves room for CW / custom waveforms in a later session.
_EXCITATIONS = ["Gaussian Pulse"]

# Green point marker colour.
_SOURCE_COLOR = (0.10, 0.90, 0.20)

_MM_PER_M = 1000.0


# --------------------------------------------------------------------------- #
# Document-object model
# --------------------------------------------------------------------------- #

class SourceObject:
    """``Proxy`` for a point-source document object.

    Properties:
        ``Component``  -- field component driven ('Ex'..'Hz').
        ``Position``   -- injection point, world coordinates (mm).
        ``Excitation`` -- temporal waveform family ('Gaussian Pulse').
        ``Fmax``       -- target maximum frequency of the pulse, stored in hertz
                          (SI). Edited via the source panel in the simulation's
                          frequency unit; read-only in the property editor.
        ``Amplitude``  -- peak amplitude of the waveform.
    """

    def __init__(self, obj):
        self.Type = _SOURCE_TYPE
        obj.Proxy = self

        if not hasattr(obj, _TYPE_PROP):
            obj.addProperty(
                "App::PropertyString", _TYPE_PROP, "Wavesim",
                "Marks this object as a Wavesim source",
            )
            setattr(obj, _TYPE_PROP, _SOURCE_TYPE)
            obj.setEditorMode(_TYPE_PROP, 1)  # read-only identity marker

        if not hasattr(obj, "Component"):
            obj.addProperty(
                "App::PropertyEnumeration", "Component", "Source",
                "Field component the point source drives",
            )
            obj.Component = _COMPONENTS
            obj.Component = "Ez"
        if not hasattr(obj, "Position"):
            obj.addProperty(
                "App::PropertyVector", "Position", "Source",
                "Injection point in world coordinates (mm), snapped to the "
                "nearest grid cell",
            )
        if not hasattr(obj, "Excitation"):
            obj.addProperty(
                "App::PropertyEnumeration", "Excitation", "Excitation",
                "Temporal waveform driving the source",
            )
            obj.Excitation = _EXCITATIONS
            obj.Excitation = _EXCITATIONS[0]
        if not hasattr(obj, "Fmax"):
            obj.addProperty(
                "App::PropertyFloat", "Fmax", "Excitation",
                "Target maximum frequency of the Gaussian pulse, in hertz "
                "(edit via the source panel in the simulation's frequency unit)",
            )
            obj.Fmax = 30.0e9
            obj.setEditorMode("Fmax", 1)  # read-only; edit through the panel
        if not hasattr(obj, "Amplitude"):
            obj.addProperty(
                "App::PropertyFloat", "Amplitude", "Excitation",
                "Peak amplitude of the excitation waveform",
            )
            obj.Amplitude = 1.0

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _SOURCE_TYPE)
        # Editor modes are runtime-only; re-assert Fmax as read-only after reload
        # so it stays edited through the unit-aware panel rather than as raw Hz.
        if hasattr(obj, "Fmax"):
            obj.setEditorMode("Fmax", 1)

    def execute(self, obj):
        # Pure data object; the ViewProvider draws the marker from Position.
        pass

    def dumps(self):
        return {"Type": getattr(self, "Type", _SOURCE_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _SOURCE_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


# --------------------------------------------------------------------------- #
# Lookup helpers
# --------------------------------------------------------------------------- #

def is_source(obj):
    """Return True if *obj* is a Wavesim Source object."""
    return getattr(obj, _TYPE_PROP, None) == _SOURCE_TYPE


def sources_group(sim):
    """Return the "Sources" child group of the Simulation container *sim*.

    Falls back to *sim* itself if the expected child group is missing (e.g. an
    older document), so a source is never left ungrouped.
    """
    if sim is None:
        return None
    for child in sim.Group:
        if child.Name == _SOURCES_GROUP or child.Label == _SOURCES_GROUP:
            return child
    return sim


def find_sources(sim):
    """Return all Source objects under the Simulation container *sim*."""
    grp = sources_group(sim)
    if grp is None:
        return []
    return [obj for obj in grp.Group if is_source(obj)]


def source_spec(source, origin_m):
    """Return the ``job.json`` source dict for *source* in the solver frame.

    *origin_m* is the domain min corner in FreeCAD world metres (from the
    voxeliser). The stored ``Position`` is world mm; the solver frame measures
    from the domain origin, so we subtract it after converting to metres.
    """
    pos = source.Position
    x = pos.x / _MM_PER_M - origin_m[0]
    y = pos.y / _MM_PER_M - origin_m[1]
    z = pos.z / _MM_PER_M - origin_m[2]
    return {
        "component": str(source.Component),
        "x": x, "y": y, "z": z,
        "fmax": float(getattr(source, "Fmax", 0.0)),  # stored in Hz
        "amplitude": float(getattr(source, "Amplitude", 1.0)),
    }


def _describe(obj):
    """Short human label for a source in the simulation's unit, e.g. ``Ez @ 30 GHz``."""
    doc = getattr(obj, "Document", None)
    sim = active_simulation(doc) if doc is not None else None
    unit = units.get_frequency_unit(sim)
    value = units.freq_from_si(float(getattr(obj, "Fmax", 0.0)), unit)
    return "{} @ {:g} {}".format(getattr(obj, "Component", "Ez"), value, unit)


def default_position_mm(sim):
    """A sensible default source position (world mm): the domain/geometry centre.

    Uses the Domain box midpoint when it has been sized, else the material
    bounding-box centre, else the origin.
    """
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import materials as materials_mod
    from wavesim_gui import voxelize as vox

    dom = domain_mod.find_domain(sim)
    if dom is not None:
        mn, mx = getattr(dom, "DomainMin", None), getattr(dom, "DomainMax", None)
        if mn is not None and mx is not None and (mx - mn).Length > 1.0e-9:
            return FreeCAD.Vector((mn.x + mx.x) / 2.0,
                                  (mn.y + mx.y) / 2.0,
                                  (mn.z + mx.z) / 2.0)

    bbox = vox.materials_bbox_mm(materials_mod.find_materials(sim)) if sim else None
    if bbox is not None:
        return FreeCAD.Vector(bbox.Center.x, bbox.Center.y, bbox.Center.z)
    return FreeCAD.Vector(0, 0, 0)


# --------------------------------------------------------------------------- #
# GUI: view provider, task panel, command
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

    class SourceViewProvider:
        """Coin view provider drawing the source as a green point marker.

        The standard ``Visibility`` property (tree "eye") shows/hides the marker.
        Double-clicking (or Edit) opens the source task panel.
        """

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            from pivy import coin

            self.Object = vobj.Object
            root = coin.SoSeparator()

            color = coin.SoBaseColor()
            color.rgb.setValue(*_SOURCE_COLOR)
            root.addChild(color)

            self._coords = coin.SoCoordinate3()
            root.addChild(self._coords)

            self._markers = coin.SoMarkerSet()
            self._markers.markerIndex = coin.SoMarkerSet.CIRCLE_FILLED_9_9
            root.addChild(self._markers)

            self._root = root
            vobj.addDisplayMode(root, "Point")
            self._rebuild()

        def _rebuild(self):
            obj = getattr(self, "Object", None)
            if obj is None:
                return
            pos = obj.Position
            self._coords.point.setValues(0, 1, [(pos.x, pos.y, pos.z)])
            if self._coords.point.getNum() > 1:
                self._coords.point.deleteValues(1)

        def updateData(self, obj, prop):
            if prop == "Position":
                self._rebuild()

        def getDisplayModes(self, vobj):
            return ["Point"]

        def getDefaultDisplayMode(self):
            return "Point"

        def setDisplayMode(self, mode):
            return mode

        def getIcon(self):
            return _SOURCE_ICON

        def setEdit(self, vobj, mode=0):
            _open_source_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_source_panel(vobj.Object)
            return True

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class TaskSourcePanel:
        """Task-tab panel to edit a source's component, position and excitation.

        ``accept`` writes the widget values back onto the object; ``reject``
        removes the object when it was created fresh for this edit (so a
        cancelled "Add Source" leaves no trace).
        """

        def __init__(self, obj, created=False):
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets

            self.obj = obj
            self.created = created
            # Original position, restored on Cancel and used so Accept records the
            # full change for undo (live edits below modify the object directly).
            self._orig_position = FreeCAD.Vector(obj.Position)

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Source")
            layout = QtWidgets.QFormLayout(form)

            self._component = QtWidgets.QComboBox()
            self._component.addItems(_COMPONENTS)
            self._component.setCurrentText(str(getattr(obj, "Component", "Ez")))

            def pos_spin(value_mm):
                spin = QtWidgets.QDoubleSpinBox()
                spin.setRange(-1.0e6, 1.0e6)
                spin.setDecimals(4)
                spin.setSuffix(" mm")
                spin.setSingleStep(0.5)
                spin.setValue(value_mm)
                return spin

            pos = obj.Position
            self._x = pos_spin(float(pos.x))
            self._y = pos_spin(float(pos.y))
            self._z = pos_spin(float(pos.z))

            self._excitation = QtWidgets.QComboBox()
            self._excitation.addItems(_EXCITATIONS)
            self._excitation.setCurrentText(
                str(getattr(obj, "Excitation", _EXCITATIONS[0]))
            )

            # Frequency is shown in the simulation's frequency unit, which is a
            # fixed (display-only) suffix here -- it is chosen in the Simulation
            # panel, not editable per source.
            sim = active_simulation(obj.Document)
            self._freq_unit = units.get_frequency_unit(sim)
            self._fmax = QtWidgets.QDoubleSpinBox()
            self._fmax.setRange(1.0e-9, 1.0e15)
            self._fmax.setDecimals(6)
            self._fmax.setSuffix(" " + self._freq_unit)
            self._fmax.setSingleStep(1.0)
            self._fmax.setValue(
                units.freq_from_si(
                    float(getattr(obj, "Fmax", 30.0e9)), self._freq_unit
                )
            )

            self._amplitude = QtWidgets.QDoubleSpinBox()
            self._amplitude.setRange(-1.0e9, 1.0e9)
            self._amplitude.setDecimals(4)
            self._amplitude.setSingleStep(0.1)
            self._amplitude.setValue(float(getattr(obj, "Amplitude", 1.0)))

            layout.addRow("Field component:", self._component)
            layout.addRow("Position X:", self._x)
            layout.addRow("Position Y:", self._y)
            layout.addRow("Position Z:", self._z)
            layout.addRow("Excitation:", self._excitation)
            layout.addRow("Max frequency:", self._fmax)
            layout.addRow("Amplitude:", self._amplitude)

            info = QtWidgets.QLabel(
                "The source softly injects the chosen field component at the "
                "given point (snapped to the nearest grid cell). The Gaussian "
                "pulse's bandwidth is set by the max frequency. The frequency "
                "unit is set on the Simulation object."
            )
            info.setWordWrap(True)
            layout.addRow(info)

            # Live-update the marker in the 3D view as the position spin boxes
            # change, rather than only on OK.
            self._x.valueChanged.connect(self._live_position)
            self._y.valueChanged.connect(self._live_position)
            self._z.valueChanged.connect(self._live_position)

            self.form = form

        def _live_position(self, *_):
            """Move the source marker immediately as the spin boxes change."""
            self.obj.Position = FreeCAD.Vector(
                self._x.value(), self._y.value(), self._z.value()
            )

        def accept(self):
            from wavesim_gui import domain as domain_mod

            doc = self.obj.Document
            # Restore the original position first so the transaction captures the
            # full change (live edits already moved the object outside it).
            self.obj.Position = self._orig_position
            doc.openTransaction("Wavesim: Edit Source")
            self.obj.Component = self._component.currentText()
            self.obj.Position = FreeCAD.Vector(
                self._x.value(), self._y.value(), self._z.value()
            )
            self.obj.Excitation = self._excitation.currentText()
            self.obj.Fmax = units.freq_to_si(self._fmax.value(), self._freq_unit)
            self.obj.Amplitude = self._amplitude.value()
            self.obj.Label = "Source ({})".format(_describe(self.obj))
            doc.commitTransaction()
            doc.recompute()
            # Enlarge the domain to include the source if it now sits outside it.
            domain_mod.notify_domain_inputs_changed(doc)
            Gui.Control.closeDialog()
            return True

        def reject(self):
            doc = self.obj.Document
            if self.created:
                doc.openTransaction("Wavesim: Cancel Source")
                doc.removeObject(self.obj.Name)
                doc.commitTransaction()
                doc.recompute()
            else:
                # Undo any live position edits.
                self.obj.Position = self._orig_position
            Gui.Control.closeDialog()
            return True

        def getStandardButtons(self):
            try:
                from PySide import QtWidgets as _w
            except ImportError:
                from PySide import QtGui as _w
            buttons = _w.QDialogButtonBox.Ok | _w.QDialogButtonBox.Cancel
            return int(getattr(buttons, "value", buttons))

    def _open_source_panel(obj, created=False):
        """Open (or replace) the source task panel bound to *obj*."""
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskSourcePanel(obj, created=created))

    class CommandAddSource:
        """Create a point Source at the domain centre and open its editor."""

        def GetResources(self):
            return {
                "Pixmap": _SOURCE_ICON,
                "MenuText": "Add Point Source",
                "ToolTip": "Add a soft point source with a Gaussian-pulse "
                "excitation to the simulation",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = active_simulation(doc)
            if sim is None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: create a Simulation before adding a source.\n"
                )
                return

            doc.openTransaction("Wavesim: Add Source")
            try:
                src = doc.addObject("App::FeaturePython", "Source")
                SourceObject(src)
                src.Position = default_position_mm(sim)
                src.Label = "Source ({})".format(_describe(src))
                if src.ViewObject is not None:
                    SourceViewProvider(src.ViewObject)
                sources_group(sim).addObject(src)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()

            _open_source_panel(src, created=True)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    Gui.addCommand("Wavesim_AddSource", CommandAddSource())
