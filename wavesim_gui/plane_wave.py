# -*- coding: utf-8 -*-
"""Plane-wave boundary source for the Wavesim workbench.

A *Plane Wave* launches a directional, uniform plane wave from one domain face,
one PML-depth inside the boundary. Unlike the TEM port (which launches the modal
field of a PEC cross-section) it needs no geometry on the face at all: it drives
the whole cross-section with a uniform transverse E field and, when directional,
the paired H = (n̂ × E)/η sheet for a one-way (into-the-domain) launch. It maps
directly onto the solver's :class:`wavesim.sources.PlaneWave`.

Workflow
--------
* The user adds a plane wave, picks one of the six domain faces (the launch
  plane) and a polarization angle, and that face's boundary condition is set to
  **PML** automatically so the wave is launched cleanly and its backward lobe
  absorbed -- the standard boundary-source setup.
* The launch is *not* amplitude-calibrated (it scales as ≈ 1/S_n × the waveform,
  S_n the Courant number along the normal); use a monitor to normalise if an
  absolute level is needed. The waveform carries the amplitude.

Polarization angle
------------------
``Angle`` (degrees) rotates E within the launch plane, measured from the face's
first transverse axis â towards its second b̂: ``E ∝ cos(angle)·â + sin(angle)·b̂``.
The (â, b̂) pair is right-handed with the inward propagation normal (this mirrors
the solver's ``wavesim.sources._FACE_CFG``), so the SAME physical polarization
takes a DIFFERENT angle on opposite faces — e.g. +z-polarized light is 90° on the
x0 (low-x) face but 0° on x1. :data:`_FACE_AXES` documents the pair per face.

Rendering
---------
Like the TEM source, the plane draws as a translucent plane on the chosen face
spanning the domain box, with an arrow (kept at a fixed on-screen size) pointing
into the domain along the propagation direction. A distinct violet colour tells
it apart from the teal TEM port and green point source.

Units: FreeCAD geometry/properties are in millimetres; the solver works in SI.
:func:`plane_wave_spec` emits the face, the polarization angle (degrees) and the
directional flag; the runner places the sheet (its cell index derived from the
boundary's PML depth) and builds the solver waveform from the excitation dict.

Importing this module registers ``Wavesim_AddPlaneWave`` with ``Gui.addCommand``
when a GUI is available.
"""

import os

import FreeCAD

from wavesim_gui.commands import active_simulation
from wavesim_gui import domain as domain_mod
from wavesim_gui import excitation as exc


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_RESOURCES_DIR = os.path.join(_WB_DIR, "Resources")
# No dedicated plane-wave icon; reuse the generic port marker.
_PLANE_ICON = os.path.join(_RESOURCES_DIR, "port.png")

_TYPE_PROP = "WavesimType"
_PLANE_TYPE = "PlaneWaveSource"

# Name of the child group (created by CommandNewSimulation) holding sources.
_SOURCES_GROUP = "Sources"

# The six domain faces, in the solver's '<axis><0|1>' naming.
_FACES = ("x0", "x1", "y0", "y1", "z0", "z1")
_FACE_LABELS = {
    "x0": "X min (x0) → +X", "x1": "X max (x1) → −X",
    "y0": "Y min (y0) → +Y", "y1": "Y max (y1) → −Y",
    "z0": "Z min (z0) → +Z", "z1": "Z max (z1) → −Z",
}

# The face's ordered transverse pair (â, b̂), matching the solver's
# ``wavesim.sources._FACE_CFG``: â × b̂ = the inward propagation normal, so
# (â, b̂, n̂) is right-handed on every face. The polarization ``Angle`` is
# measured from â towards b̂; shown in the panel help so the convention is clear.
_FACE_AXES = {
    "x0": ("y", "z"), "x1": ("z", "y"),
    "y0": ("z", "x"), "y1": ("x", "z"),
    "z0": ("x", "y"), "z1": ("y", "x"),
}

# Excitation waveform families + object<->spec glue live in the shared
# workbench-side catalogue :mod:`wavesim_gui.excitation`.
_EXCITATIONS = exc.EXCITATION_LABELS

# Boundary condition forced on the launch face (clean directional launch).
_PORT_BC = "PML"

# Translucent violet plane, distinct from the teal TEM port / green point source.
_PLANE_COLOR = (0.62, 0.32, 0.92)
_PLANE_TRANSPARENCY = 0.6

_MM_PER_M = 1000.0


# --------------------------------------------------------------------------- #
# Document-object model
# --------------------------------------------------------------------------- #

class PlaneWaveObject:
    """``Proxy`` for a plane-wave source document object.

    Properties:
        ``Face``        -- domain face the wave launches from ('x0'..'z1'); set
                           to PML automatically.
        ``Angle``       -- E polarization angle (degrees) in the face frame (see
                           :data:`_FACE_AXES`).
        ``Directional`` -- pair the E sheet with an H sheet for a one-way launch
                           (True) or launch a bare E sheet, radiating both ways.
        ``Excitation``  + one property per waveform parameter (Gaussian pulse,
                           sine, sinusoid, rectangular, Gaussian+sine); added and
                           kept in sync by :func:`excitation.ensure_object_props`.

    Hidden ``Corners`` carries the launch plane's four world-mm corners for the
    view provider; ``execute`` keeps them in sync with the domain bounds + face.
    """

    def __init__(self, obj):
        self.Type = _PLANE_TYPE
        obj.Proxy = self

        if not hasattr(obj, _TYPE_PROP):
            obj.addProperty(
                "App::PropertyString", _TYPE_PROP, "Wavesim",
                "Marks this object as a Wavesim plane-wave source",
            )
            setattr(obj, _TYPE_PROP, _PLANE_TYPE)
            obj.setEditorMode(_TYPE_PROP, 1)  # read-only identity marker

        if not hasattr(obj, "Face"):
            obj.addProperty(
                "App::PropertyEnumeration", "Face", "Plane Wave",
                "Domain face the plane wave launches from, propagating into the "
                "domain (set to PML automatically)",
            )
            obj.Face = list(_FACES)
            obj.Face = "z0"
        if not hasattr(obj, "Angle"):
            obj.addProperty(
                "App::PropertyAngle", "Angle", "Plane Wave",
                "E polarization angle, measured in the launch face's transverse "
                "frame (from its first transverse axis towards its second)",
            )
            obj.Angle = 0.0
        if not hasattr(obj, "Directional"):
            obj.addProperty(
                "App::PropertyBool", "Directional", "Plane Wave",
                "Pair the E sheet with an H sheet for a one-way (into-domain) "
                "launch. Off launches a bare E sheet, radiating both ways.",
            )
            obj.Directional = True

        # Excitation enum + one property per waveform parameter (shared scheme).
        exc.ensure_object_props(obj)

        # Plane corners (hidden, four world-mm points) for the view provider.
        if not hasattr(obj, "Corners"):
            obj.addProperty("App::PropertyVectorList", "Corners", "Plane", "")
            obj.setEditorMode("Corners", 2)  # hidden

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _PLANE_TYPE)
        # Re-run property setup so sources saved before the extra waveforms gain
        # the new options + parameter properties and editor modes are re-asserted.
        exc.ensure_object_props(obj)

    def execute(self, obj):
        """Size/orient the drawn launch plane to the domain bounds and face."""
        from wavesim_gui import tem_source as tem_mod

        sim = active_simulation(obj.Document)
        dom = domain_mod.find_domain(sim) if sim else None
        if dom is not None and (dom.DomainMax - dom.DomainMin).Length > 1.0e-9:
            mn, mx = dom.DomainMin, dom.DomainMax
        else:
            # No sized domain yet: a small default cube so the plane is visible.
            half = 5.0
            mn = FreeCAD.Vector(-half, -half, -half)
            mx = FreeCAD.Vector(half, half, half)
        obj.Corners = [FreeCAD.Vector(*p)
                       for p in tem_mod._face_corners(mn, mx, str(obj.Face))]

    def dumps(self):
        return {"Type": getattr(self, "Type", _PLANE_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _PLANE_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


# --------------------------------------------------------------------------- #
# Lookup helpers & job serialisation
# --------------------------------------------------------------------------- #

def is_plane_wave(obj):
    """Return True if *obj* is a Wavesim plane-wave source object."""
    return getattr(obj, _TYPE_PROP, None) == _PLANE_TYPE


def sources_group(sim):
    """Return the "Sources" child group of *sim* (or *sim* itself if missing)."""
    if sim is None:
        return None
    for child in sim.Group:
        if child.Name == _SOURCES_GROUP or child.Label == _SOURCES_GROUP:
            return child
    return sim


def find_plane_waves(sim):
    """Return all plane-wave Source objects under the Simulation container *sim*."""
    grp = sources_group(sim)
    if grp is None:
        return []
    return [obj for obj in grp.Group if is_plane_wave(obj)]


def plane_wave_spec(obj, origin_m=None):
    """Return the ``job.json`` ``plane_waves`` dict for *obj*.

    A plane wave is placed by the runner from its face + the boundary's PML depth
    (the E sheet sits one PML-depth inside the face), so — unlike the point/TEM
    sources — there is no position to shift into the solver frame; *origin_m* is
    accepted only to match the other ``*_spec`` call signature and is unused.
    """
    return {
        "face": str(obj.Face),
        "angle_deg": float(getattr(obj, "Angle", 0.0)),
        "directional": bool(getattr(obj, "Directional", True)),
        "excitation": exc.spec_from_object(obj),
    }


def _describe(obj):
    """Short human label, e.g. ``z0, 0°, Gaussian Pulse @ 30 GHz``."""
    doc = getattr(obj, "Document", None)
    sim = active_simulation(doc) if doc is not None else None
    return "{}, {:g}°, {}".format(
        getattr(obj, "Face", "z0"),
        float(getattr(obj, "Angle", 0.0)),
        exc.excitation_label(obj, sim),
    )


# --------------------------------------------------------------------------- #
# GUI: view provider, task panel, command
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

    # Reuse the point source's excitation widgets/plot mixin and the TEM source's
    # plane/arrow coin geometry (identical launch-plane visual, different colour).
    from wavesim_gui import source as source_mod
    from wavesim_gui import tem_source as tem_mod

    class PlaneWaveViewProvider:
        """Coin view provider drawing the plane wave as a translucent plane.

        Mirrors the TEM source's plane + fixed-pixel propagation arrow, in a
        distinct violet, so a launch face reads the same way for both.
        """

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            from pivy import coin

            self.Object = vobj.Object
            root = coin.SoSeparator()

            # Two-sided lighting so the translucent plane shows from behind.
            hints = coin.SoShapeHints()
            hints.vertexOrdering = coin.SoShapeHints.COUNTERCLOCKWISE
            hints.shapeType = coin.SoShapeHints.UNKNOWN_SHAPE_TYPE
            root.addChild(hints)

            material = coin.SoMaterial()
            material.diffuseColor.setValue(*_PLANE_COLOR)
            material.transparency.setValue(_PLANE_TRANSPARENCY)
            root.addChild(material)

            self._coords = coin.SoCoordinate3()
            root.addChild(self._coords)
            self._face = coin.SoFaceSet()
            root.addChild(self._face)

            # Opaque border so the plane edges read clearly.
            border = coin.SoSeparator()
            bcolor = coin.SoBaseColor()
            bcolor.rgb.setValue(*_PLANE_COLOR)
            border.addChild(bcolor)
            bstyle = coin.SoDrawStyle()
            bstyle.lineWidth = 2
            border.addChild(bstyle)
            self._border_coords = coin.SoCoordinate3()
            border.addChild(self._border_coords)
            self._border_lines = coin.SoIndexedLineSet()
            border.addChild(self._border_lines)
            root.addChild(border)

            # Propagation arrow, anchored to a plane corner, pointing into the
            # domain and kept a constant on-screen size (same machinery as the
            # TEM source's energy-flow arrow).
            arrow = coin.SoSeparator()
            acolor = coin.SoBaseColor()
            acolor.rgb.setValue(*_PLANE_COLOR)
            arrow.addChild(acolor)
            self._arrow_pos = coin.SoTranslation()
            arrow.addChild(self._arrow_pos)
            self._arrow_cb = coin.SoCallback()
            self._arrow_cb.setCallback(self._scale_arrow_cb)
            arrow.addChild(self._arrow_cb)
            self._arrow_scale = coin.SoScale()
            self._arrow_scale.scaleFactor.setValue(0.0, 0.0, 0.0)
            arrow.addChild(self._arrow_scale)
            self._arrow_rot = coin.SoRotation()
            arrow.addChild(self._arrow_rot)
            arrow.addChild(tem_mod._build_arrow_geometry())
            self._arrow_on = False
            root.addChild(arrow)

            self._root = root
            vobj.addDisplayMode(root, "Plane")
            self._rebuild()

        def _scale_arrow_cb(self, user, action):
            """Keep the arrow a fixed pixel length by setting its SoScale."""
            from pivy import coin

            if not getattr(self, "_arrow_on", False):
                return
            if not action.isOfType(coin.SoGLRenderAction.getClassTypeId()):
                return
            state = action.getState()
            vv = coin.SoViewVolumeElement.get(state)
            vp = coin.SoViewportRegionElement.get(state)
            mm = coin.SoModelMatrixElement.get(state)
            height_px = float(vp.getViewportSizePixels()[1])
            if height_px <= 0.0:
                return
            world = mm.multVecMatrix(coin.SbVec3f(0.0, 0.0, 0.0))
            size = vv.getWorldToScreenScale(world, tem_mod._ARROW_PIXELS / height_px)
            last = getattr(self, "_arrow_last_size", 0.0)
            if size > 0.0 and abs(size - last) > 1e-6 * max(size, last):
                self._arrow_last_size = size
                self._arrow_scale.scaleFactor.setValue(size, size, size)

        def _clear(self):
            if self._coords.point.getNum():
                self._coords.point.deleteValues(0)
            self._face.numVertices.setValue(0)
            if self._border_coords.point.getNum():
                self._border_coords.point.deleteValues(0)
            if self._border_lines.coordIndex.getNum():
                self._border_lines.coordIndex.deleteValues(0)
            self._arrow_on = False
            self._arrow_last_size = 0.0
            self._arrow_scale.scaleFactor.setValue(0.0, 0.0, 0.0)

        def _rebuild(self):
            from pivy import coin

            obj = getattr(self, "Object", None)
            if obj is None:
                return
            corners = list(getattr(obj, "Corners", []) or [])
            if len(corners) != 4:
                self._clear()
                return
            pts = [(v.x, v.y, v.z) for v in corners]

            self._coords.point.setValues(0, len(pts), pts)
            if self._coords.point.getNum() > len(pts):
                self._coords.point.deleteValues(len(pts))
            self._face.numVertices.setValue(len(pts))

            self._border_coords.point.setValues(0, len(pts), pts)
            if self._border_coords.point.getNum() > len(pts):
                self._border_coords.point.deleteValues(len(pts))
            edges = [0, 1, 2, 3, 0, -1]
            self._border_lines.coordIndex.setValues(0, len(edges), edges)
            if self._border_lines.coordIndex.getNum() > len(edges):
                self._border_lines.coordIndex.deleteValues(len(edges))

            self._arrow_pos.translation.setValue(*pts[0])
            d = tem_mod._flow_direction(str(obj.Face))
            self._arrow_rot.rotation.setValue(
                coin.SbRotation(coin.SbVec3f(0.0, 1.0, 0.0), coin.SbVec3f(*d))
            )
            self._arrow_on = True

        def updateData(self, obj, prop):
            if prop in ("Corners", "Face"):
                self._rebuild()

        def getDisplayModes(self, vobj):
            return ["Plane"]

        def getDefaultDisplayMode(self):
            return "Plane"

        def setDisplayMode(self, mode):
            return mode

        def getIcon(self):
            return _PLANE_ICON

        def setEdit(self, vobj, mode=0):
            _open_plane_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_plane_panel(vobj.Object)
            return True

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class TaskPlaneWavePanel(source_mod.ExcitationParamsMixin):
        """Task panel to edit a plane wave: face, polarization, directionality and
        excitation. OK commits and forces the launch face to PML; Cancel removes a
        freshly-created source so it leaves no trace."""

        def __init__(self, obj, created=False):
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets

            self.obj = obj
            self.created = created
            self._orig_face = str(getattr(obj, "Face", "z0"))

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Plane Wave")
            layout = QtWidgets.QFormLayout(form)

            self._face = QtWidgets.QComboBox()
            for f in _FACES:
                self._face.addItem(_FACE_LABELS[f], f)
            self._face.setCurrentIndex(
                max(0, list(_FACES).index(str(getattr(obj, "Face", "z0"))))
            )

            self._angle = QtWidgets.QDoubleSpinBox()
            self._angle.setRange(-360.0, 360.0)
            self._angle.setDecimals(2)
            self._angle.setSuffix(" deg")
            self._angle.setSingleStep(5.0)
            self._angle.setValue(float(getattr(obj, "Angle", 0.0)))

            self._directional = QtWidgets.QCheckBox(
                "Directional (one-way launch into the domain)"
            )
            self._directional.setChecked(bool(getattr(obj, "Directional", True)))

            layout.addRow("Launch face:", self._face)
            layout.addRow("Polarization angle:", self._angle)
            layout.addRow("", self._directional)

            # Polarization convention for the current face, refreshed on change.
            self._pol_hint = QtWidgets.QLabel()
            self._pol_hint.setWordWrap(True)
            self._pol_hint.setStyleSheet("color: gray;")
            layout.addRow(self._pol_hint)

            # Excitation combo + per-waveform parameter rows + preview button
            # (shared with the point-source panel).
            self.build_excitation_ui(layout, QtWidgets)

            info = QtWidgets.QLabel(
                "The plane wave launches a uniform transverse field from the "
                "chosen face (set to PML automatically), propagating into the "
                "domain. Pick a temporal waveform and its parameters (preview with "
                "the plot button) — a ramped Sinusoid is a good CW choice. The "
                "launch is not amplitude-calibrated; normalise with a monitor if "
                "you need an absolute level. Frequency/time units are set on the "
                "Simulation object."
            )
            info.setWordWrap(True)
            layout.addRow(info)

            self._face.currentIndexChanged.connect(self._live_face)
            self._update_pol_hint()

            self.form = form

        def _selected_face(self):
            return self._face.currentData() or _FACES[self._face.currentIndex()]

        def _update_pol_hint(self, *_):
            a_ax, b_ax = _FACE_AXES[self._selected_face()]
            self._pol_hint.setText(
                "On this face, angle 0° polarizes E along +{a}, 90° along +{b} "
                "(measured from {a} towards {b}).".format(a=a_ax, b=b_ax)
            )

        def _live_face(self, *_):
            self.obj.Face = self._selected_face()
            self._update_pol_hint()
            self.obj.Document.recompute()

        def _commit(self, title):
            doc = self.obj.Document
            # Restore the original face first so the transaction captures the full
            # change (the live edit already moved it outside the transaction).
            self.obj.Face = self._orig_face
            doc.openTransaction(title)
            face = self._selected_face()
            self.obj.Face = face
            self.obj.Angle = float(self._angle.value())
            self.obj.Directional = bool(self._directional.isChecked())
            self.write_excitation(self.obj)
            self.obj.Label = "Plane Wave ({})".format(_describe(self.obj))
            # Directional boundary launch: force the launch face to PML.
            domain_mod.set_face_bc(domain_mod.find_domain(active_simulation(doc)),
                                   face, _PORT_BC)
            doc.commitTransaction()
            doc.recompute()
            domain_mod.notify_domain_inputs_changed(doc)
            self._orig_face = face

        def accept(self):
            self._commit("Wavesim: Edit Plane Wave")
            Gui.Control.closeDialog()
            return True

        def reject(self):
            doc = self.obj.Document
            if self.created:
                doc.openTransaction("Wavesim: Cancel Plane Wave")
                doc.removeObject(self.obj.Name)
                doc.commitTransaction()
                doc.recompute()
            else:
                self.obj.Face = self._orig_face
                doc.recompute()
            Gui.Control.closeDialog()
            return True

        def getStandardButtons(self):
            try:
                from PySide import QtWidgets as _w
            except ImportError:
                from PySide import QtGui as _w
            buttons = _w.QDialogButtonBox.Ok | _w.QDialogButtonBox.Cancel
            return int(getattr(buttons, "value", buttons))

    def _open_plane_panel(obj, created=False):
        """Open (or replace) the plane-wave task panel bound to *obj*."""
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskPlaneWavePanel(obj, created=created))

    class CommandAddPlaneWave:
        """Create a plane-wave Source on a domain face and open its editor."""

        def GetResources(self):
            return {
                "Pixmap": _PLANE_ICON,
                "MenuText": "Add Plane Wave",
                "ToolTip": "Add a directional plane-wave source launched from a "
                "domain face, with a selectable polarization and temporal "
                "excitation",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = active_simulation(doc)
            if sim is None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: create a Simulation before adding a plane wave.\n"
                )
                return

            doc.openTransaction("Wavesim: Add Plane Wave")
            try:
                pw = doc.addObject("App::FeaturePython", "PlaneWave")
                PlaneWaveObject(pw)
                pw.Label = "Plane Wave ({})".format(_describe(pw))
                if pw.ViewObject is not None:
                    PlaneWaveViewProvider(pw.ViewObject)
                sources_group(sim).addObject(pw)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()

            _open_plane_panel(pw, created=True)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    Gui.addCommand("Wavesim_AddPlaneWave", CommandAddPlaneWave())
