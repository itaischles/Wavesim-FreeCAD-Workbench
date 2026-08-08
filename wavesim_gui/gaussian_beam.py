# -*- coding: utf-8 -*-
"""Gaussian-beam boundary source for the Wavesim workbench.

A *Gaussian Beam* launches a directional beam from one domain face, one
PML-depth inside the boundary. Unlike the TEM port (which launches the modal
field of a PEC cross-section) it needs no geometry on the face at all: it drives
the cross-section with a transverse E sheet apodized by exp(-r²/w₀²) and, when
directional, the paired H = (n̂ × E)/η sheet for a one-way (into-the-domain)
launch. It maps directly onto the solver's :class:`wavesim.sources.GaussianBeam`.

Workflow
--------
* The user adds a beam, picks one of the six domain faces (the launch plane), a
  polarization angle and a waist, and that face's boundary condition is set to
  **PML** automatically so the beam is launched cleanly and its backward lobe
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

The waist
---------
``Waist`` is w₀, the 1/e radius of the transverse E amplitude (1/e² in
intensity), centred on the face. The sheet is driven with a flat phase front, so
**the waist sits at the launch plane** and the beam diverges downstream by the
usual Gaussian-beam laws: it stays collimated over roughly the Rayleigh range
z_R = πw₀²/λ and spreads at θ ≈ λ/(πw₀) beyond it. A waist large compared with
the wavelength therefore approximates a plane wave; a waist near the wavelength
is a strongly diverging beam. :func:`beam_hints` computes z_R and the edge
clipping so the task panel can show both before the run.

The solver always hard-zeroes the sheet over the transverse PML slabs. That is
what keeps a DC-containing waveform (a unipolar Gaussian pulse is mostly DC) from
biasing the corner cells where the sheet would otherwise overlap the absorber —
DC neither propagates nor absorbs there, so the field grows without bound and
swamps the energy monitor. Keep the waist comfortably inside the interior
half-width so that hard cut lands where the beam is already negligible;
``Waist = 0`` picks :data:`_AUTO_WAIST_FRACTION` × the half-width for you.

Rendering
---------
Like the TEM source, the beam draws as a translucent plane on the chosen face
spanning the domain box, with an arrow (kept at a fixed on-screen size) pointing
into the domain along the propagation direction. A distinct violet colour tells
it apart from the teal TEM port and green point source.

Units: FreeCAD geometry/properties are in millimetres; the solver works in SI.
:func:`gaussian_beam_spec` emits the face, the polarization angle (degrees), the
directional flag and the resolved waist in **metres**; the runner places the
sheet (its cell index derived from the boundary's PML depth) and builds the
solver waveform from the excitation dict.

Importing this module registers ``Wavesim_AddGaussianBeam`` with
``Gui.addCommand`` when a GUI is available.
"""

import math
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
# The 24x24 SVG icon set (grouped by colour: blue setup, amber sources,
# teal monitors). The retired PNGs are still in Resources/ alongside it.
_ICONS_DIR = os.path.join(_RESOURCES_DIR, "icons")
_BEAM_ICON = os.path.join(_ICONS_DIR, "beam_gaussian.svg")

_TYPE_PROP = "WavesimType"
_BEAM_TYPE = "GaussianBeamSource"

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
_BEAM_COLOR = (0.62, 0.32, 0.92)
_BEAM_TRANSPARENCY = 0.6

_MM_PER_M = 1000.0
_C0 = 299792458.0

# ``Waist = 0`` means "pick one": this fraction of the launch face's smaller
# interior half-width. At w₀ = a/2 the beam is down to exp(-4) ≈ 1.8 % where the
# solver hard-zeroes the transverse PML cells, so that cut costs nothing.
_AUTO_WAIST_FRACTION = 0.5


# --------------------------------------------------------------------------- #
# Document-object model
# --------------------------------------------------------------------------- #

class GaussianBeamObject:
    """``Proxy`` for a Gaussian-beam source document object.

    Properties:
        ``Face``        -- domain face the beam launches from ('x0'..'z1'); set
                           to PML automatically.
        ``Angle``       -- E polarization angle (degrees) in the face frame (see
                           :data:`_FACE_AXES`).
        ``Waist``       -- w₀, the 1/e E-amplitude radius at the launch plane
                           (mm; 0 = auto from the face's interior half-width).
        ``Directional`` -- pair the E sheet with an H sheet for a one-way launch
                           (True) or launch a bare E sheet, radiating both ways.
        ``Excitation``  + one property per waveform parameter (Gaussian pulse,
                           sine, sinusoid, rectangular, Gaussian+sine); added and
                           kept in sync by :func:`excitation.ensure_object_props`.

    Hidden ``Corners`` carries the launch plane's four world-mm corners for the
    view provider; ``execute`` keeps them in sync with the domain bounds + face.
    """

    def __init__(self, obj):
        self.Type = _BEAM_TYPE
        obj.Proxy = self

        if not hasattr(obj, _TYPE_PROP):
            obj.addProperty(
                "App::PropertyString", _TYPE_PROP, "Wavesim",
                "Marks this object as a Wavesim Gaussian-beam source",
            )
            setattr(obj, _TYPE_PROP, _BEAM_TYPE)
            obj.setEditorMode(_TYPE_PROP, 1)  # read-only identity marker

        if not hasattr(obj, "Face"):
            obj.addProperty(
                "App::PropertyEnumeration", "Face", "Gaussian Beam",
                "Domain face the beam launches from, propagating into the "
                "domain (set to PML automatically)",
            )
            obj.Face = list(_FACES)
            obj.Face = "z0"
        if not hasattr(obj, "Angle"):
            obj.addProperty(
                "App::PropertyAngle", "Angle", "Gaussian Beam",
                "E polarization angle, measured in the launch face's transverse "
                "frame (from its first transverse axis towards its second)",
            )
            obj.Angle = 0.0
        if not hasattr(obj, "Waist"):
            obj.addProperty(
                "App::PropertyLength", "Waist", "Gaussian Beam",
                "Beam waist w0 -- the 1/e radius of the transverse E amplitude "
                "(1/e^2 in intensity), located at the launch plane. The beam "
                "stays collimated over the Rayleigh range pi*w0^2/lambda and "
                "spreads at lambda/(pi*w0) beyond it, so a large waist "
                "approximates a plane wave. 0 = auto (half the launch face's "
                "smaller interior half-width).",
            )
            obj.Waist = 0.0
        if not hasattr(obj, "Directional"):
            obj.addProperty(
                "App::PropertyBool", "Directional", "Gaussian Beam",
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
        self.Type = getattr(self, "Type", _BEAM_TYPE)
        # Re-run property setup so sources saved before the extra waveforms gain
        # the new options + parameter properties and editor modes are re-asserted.
        exc.ensure_object_props(obj)

    def execute(self, obj):
        """Size/orient the drawn launch plane to the domain bounds and face."""
        from wavesim_gui import modal_port as modal_mod

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
                       for p in modal_mod._face_corners(mn, mx, str(obj.Face))]

    def dumps(self):
        return {"Type": getattr(self, "Type", _BEAM_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _BEAM_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


# --------------------------------------------------------------------------- #
# Lookup helpers & job serialisation
# --------------------------------------------------------------------------- #

def is_gaussian_beam(obj):
    """Return True if *obj* is a Wavesim Gaussian-beam source object."""
    return getattr(obj, _TYPE_PROP, None) == _BEAM_TYPE


def sources_group(sim):
    """Return the "Sources" child group of *sim* (or *sim* itself if missing)."""
    if sim is None:
        return None
    for child in sim.Group:
        if child.Name == _SOURCES_GROUP or child.Label == _SOURCES_GROUP:
            return child
    return sim


def find_gaussian_beams(sim):
    """Return all Gaussian-beam Source objects under the Simulation container *sim*."""
    grp = sources_group(sim)
    if grp is None:
        return []
    return [obj for obj in grp.Group if is_gaussian_beam(obj)]


def face_half_widths_mm(obj):
    """Return the launch face's two interior transverse half-widths (mm).

    Taken from the domain's *interior* box (``DomainMin``/``DomainMax``, which
    excludes the PML padding), in the face's (â, b̂) axis order. ``(None, None)``
    when there is no sized domain yet.
    """
    doc = getattr(obj, "Document", None)
    sim = active_simulation(doc) if doc is not None else None
    dom = domain_mod.find_domain(sim) if sim else None
    if dom is None or (dom.DomainMax - dom.DomainMin).Length <= 1.0e-9:
        return None, None
    extent = {ax: abs(getattr(dom.DomainMax, ax) - getattr(dom.DomainMin, ax))
              for ax in ("x", "y", "z")}
    a_ax, b_ax = _FACE_AXES[str(getattr(obj, "Face", "z0"))]
    return 0.5 * extent[a_ax], 0.5 * extent[b_ax]


def resolved_waist_mm(obj):
    """Return the waist actually sent to the solver, in mm.

    ``Waist > 0`` is used as typed. ``Waist = 0`` means auto:
    :data:`_AUTO_WAIST_FRACTION` × the *smaller* of the launch face's two
    interior half-widths, so the beam is negligible where the solver hard-zeroes
    the transverse PML cells. With no sized domain yet there is nothing to scale
    against and auto returns 0.0 — :func:`gaussian_beam_spec` refuses to emit
    that, since the solver requires a positive waist.
    """
    waist = float(getattr(obj, "Waist", 0.0))
    if waist > 0.0:
        return waist
    half_a, half_b = face_half_widths_mm(obj)
    if half_a is None:
        return 0.0
    return _AUTO_WAIST_FRACTION * min(half_a, half_b)


def beam_hints(obj):
    """Return ``(waist_mm, rayleigh_mm, edge_amplitude)`` for *obj*, or None.

    ``rayleigh_mm`` is z_R = πw₀²/λ at the excitation's representative frequency
    — the distance over which the beam stays collimated — and ``edge_amplitude``
    is exp(-(a/w₀)²), the fraction of the on-axis amplitude still standing where
    the solver hard-zeroes the transverse PML cells. Either may be ``None`` when
    the domain or the frequency is not known yet.
    """
    waist = resolved_waist_mm(obj)
    if waist <= 0.0:
        return None
    half_a, half_b = face_half_widths_mm(obj)
    edge = None
    if half_a is not None:
        edge = math.exp(-(min(half_a, half_b) / waist) ** 2)
    fmax = exc.representative_fmax(exc.spec_from_object(obj))
    rayleigh = None
    if fmax > 0.0:
        lam_mm = _C0 / fmax * _MM_PER_M
        rayleigh = math.pi * waist * waist / lam_mm
    return waist, rayleigh, edge


def gaussian_beam_spec(obj, origin_m=None):
    """Return the ``job.json`` ``gaussian_beams`` dict for *obj*.

    A beam is placed by the runner from its face + the boundary's PML depth (the
    E sheet sits one PML-depth inside the face), so — unlike the point/TEM
    sources — there is no position to shift into the solver frame; *origin_m* is
    accepted only to match the other ``*_spec`` call signature and is unused.

    ``waist`` is emitted in **metres**, already resolved (an auto ``Waist`` is
    turned into a number here, never passed through as 0).
    """
    waist_mm = resolved_waist_mm(obj)
    if waist_mm <= 0.0:
        raise ValueError(
            "Gaussian beam '{}' has no usable waist: set Waist explicitly (auto "
            "needs a sized domain to scale against).".format(
                getattr(obj, "Label", getattr(obj, "Name", "?"))))
    return {
        "face": str(obj.Face),
        "angle_deg": float(getattr(obj, "Angle", 0.0)),
        "waist": waist_mm / _MM_PER_M,
        "directional": bool(getattr(obj, "Directional", True)),
        "excitation": exc.spec_from_object(obj),
    }


def _describe(obj):
    """Short human label, e.g. ``z0, 0°, w0 10 mm, Gaussian Pulse @ 30 GHz``."""
    doc = getattr(obj, "Document", None)
    sim = active_simulation(doc) if doc is not None else None
    waist = resolved_waist_mm(obj)
    return "{}, {:g}°, w0 {}, {}".format(
        getattr(obj, "Face", "z0"),
        float(getattr(obj, "Angle", 0.0)),
        "{:g} mm".format(round(waist, 3)) if waist > 0.0 else "auto",
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
    from wavesim_gui import modal_port as modal_mod

    class GaussianBeamViewProvider:
        """Coin view provider drawing the beam's launch plane, translucent.

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
            material.diffuseColor.setValue(*_BEAM_COLOR)
            material.transparency.setValue(_BEAM_TRANSPARENCY)
            root.addChild(material)

            self._coords = coin.SoCoordinate3()
            root.addChild(self._coords)
            self._face = coin.SoFaceSet()
            root.addChild(self._face)

            # Opaque border so the plane edges read clearly.
            border = coin.SoSeparator()
            bcolor = coin.SoBaseColor()
            bcolor.rgb.setValue(*_BEAM_COLOR)
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
            acolor.rgb.setValue(*_BEAM_COLOR)
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
            arrow.addChild(modal_mod._build_arrow_geometry())
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
            size = vv.getWorldToScreenScale(world, modal_mod._ARROW_PIXELS / height_px)
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
            d = modal_mod._flow_direction(str(obj.Face))
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
            return _BEAM_ICON

        def setEdit(self, vobj, mode=0):
            _open_beam_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_beam_panel(vobj.Object)
            return True

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class TaskGaussianBeamPanel(source_mod.ExcitationParamsMixin):
        """Task panel to edit a Gaussian beam: face, polarization, waist,
        directionality and excitation. OK commits and forces the launch face to
        PML; Cancel removes a freshly-created source so it leaves no trace."""

        def __init__(self, obj, created=False):
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets

            self.obj = obj
            self.created = created
            self._orig_face = str(getattr(obj, "Face", "z0"))

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Gaussian Beam")
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

            self._waist = QtWidgets.QDoubleSpinBox()
            self._waist.setRange(0.0, 1.0e6)
            self._waist.setDecimals(3)
            self._waist.setSuffix(" mm")
            self._waist.setSpecialValueText("auto (half the face half-width)")
            self._waist.setValue(float(getattr(obj, "Waist", 0.0)))

            self._directional = QtWidgets.QCheckBox(
                "Directional (one-way launch into the domain)"
            )
            self._directional.setChecked(bool(getattr(obj, "Directional", True)))

            layout.addRow("Launch face:", self._face)
            layout.addRow("Polarization angle:", self._angle)
            layout.addRow("Beam waist w0:", self._waist)
            layout.addRow("", self._directional)

            # Resolved waist, Rayleigh range and PML-edge clipping, refreshed as
            # the face/waist/waveform change -- the divergence is the one thing
            # about this source you cannot see until the run is over.
            self._beam_hint = QtWidgets.QLabel()
            self._beam_hint.setWordWrap(True)
            self._beam_hint.setStyleSheet("color: gray;")
            layout.addRow(self._beam_hint)

            # Polarization convention for the current face, refreshed on change.
            self._pol_hint = QtWidgets.QLabel()
            self._pol_hint.setWordWrap(True)
            self._pol_hint.setStyleSheet("color: gray;")
            layout.addRow(self._pol_hint)

            # Excitation combo + per-waveform parameter rows + preview button
            # (shared with the point-source panel).
            self.build_excitation_ui(layout, QtWidgets)

            info = QtWidgets.QLabel(
                "The beam launches from the chosen face (set to PML "
                "automatically), propagating into the domain, with its waist at "
                "the launch plane. Pick a temporal waveform and its parameters "
                "(preview with the plot button) — a ramped Sinusoid is a good CW "
                "choice. The launch is not amplitude-calibrated; normalise with a "
                "monitor if you need an absolute level. Frequency/time units are "
                "set on the Simulation object.\n\n"
                "The waist trades collimation against aperture: the beam stays "
                "collimated over the Rayleigh range and spreads beyond it, so a "
                "waist many wavelengths across behaves like a plane wave — but it "
                "must still fit inside the cross-section, since the sheet is "
                "zeroed over the transverse PML cells. Widen the domain if the "
                "hint below says the beam is neither collimated nor contained."
            )
            info.setWordWrap(True)
            layout.addRow(info)

            self._face.currentIndexChanged.connect(self._live_face)
            self._waist.valueChanged.connect(self._update_beam_hint)
            self._update_pol_hint()
            self._update_beam_hint()

            self.form = form

        def _selected_face(self):
            return self._face.currentData() or _FACES[self._face.currentIndex()]

        def _update_pol_hint(self, *_):
            a_ax, b_ax = _FACE_AXES[self._selected_face()]
            self._pol_hint.setText(
                "On this face, angle 0° polarizes E along +{a}, 90° along +{b} "
                "(measured from {a} towards {b}).".format(a=a_ax, b=b_ax)
            )

        def _update_beam_hint(self, *_):
            """Show the resolved waist, z_R and the PML-edge clipping.

            Reads the *live* object (face edits are applied immediately) but the
            panel's own waist value, so the numbers track what is being typed.
            """
            try:
                waist = float(self._waist.value())
                if waist <= 0.0:
                    waist = resolved_waist_mm(self.obj)
                if waist <= 0.0:
                    self._beam_hint.setText(
                        "No sized domain yet — the auto waist has nothing to "
                        "scale against. Assign a material to some geometry, or "
                        "type a waist."
                    )
                    return
                half_a, half_b = face_half_widths_mm(self.obj)
                bits = ["w0 = {:g} mm".format(round(waist, 3))]
                fmax = exc.representative_fmax(self.read_excitation_spec())
                if fmax > 0.0:
                    lam = _C0 / fmax * _MM_PER_M
                    bits.append("z_R = {:g} mm at {:g} GHz (lambda {:g} mm)"
                                .format(round(math.pi * waist * waist / lam, 2),
                                        round(fmax / 1e9, 4), round(lam, 2)))
                if half_a is not None:
                    half = min(half_a, half_b)
                    bits.append("{:.2g} of the on-axis amplitude still standing "
                                "at the PML edge ({:g} mm off axis)"
                                .format(math.exp(-(half / waist) ** 2),
                                        round(half, 3)))
                self._beam_hint.setText("; ".join(bits) + ".")
            except Exception:
                self._beam_hint.setText("")

        def _rebuild_params(self, *args):
            """Re-hook the freshly built parameter spins into the beam hint.

            The mixin rebuilds these rows whenever the waveform family changes,
            so the frequency spin the Rayleigh range depends on is a different
            widget each time.
            """
            source_mod.ExcitationParamsMixin._rebuild_params(self, *args)
            for spin, _to_si in getattr(self, "_param_spins", {}).values():
                spin.valueChanged.connect(self._update_beam_hint)
            self._update_beam_hint()

        def read_excitation_spec(self):
            """The excitation spec as currently shown in the panel widgets.

            Falls back to the stored object when the mixin's widgets are not
            built yet (the hint is refreshed once during construction).
            """
            try:
                return self._spec_from_widgets()
            except Exception:
                return exc.spec_from_object(self.obj)

        def _live_face(self, *_):
            self.obj.Face = self._selected_face()
            self._update_pol_hint()
            self._update_beam_hint()
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
            self.obj.Waist = float(self._waist.value())
            self.obj.Directional = bool(self._directional.isChecked())
            self.write_excitation(self.obj)
            self.obj.Label = "Gaussian Beam ({})".format(_describe(self.obj))
            # Directional boundary launch: force the launch face to PML.
            domain_mod.set_face_bc(domain_mod.find_domain(active_simulation(doc)),
                                   face, _PORT_BC)
            doc.commitTransaction()
            doc.recompute()
            domain_mod.notify_domain_inputs_changed(doc)
            self._orig_face = face

        def accept(self):
            self._commit("Wavesim: Edit Gaussian Beam")
            Gui.Control.closeDialog()
            return True

        def reject(self):
            doc = self.obj.Document
            if self.created:
                doc.openTransaction("Wavesim: Cancel Gaussian Beam")
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

    def _open_beam_panel(obj, created=False):
        """Open (or replace) the Gaussian-beam task panel bound to *obj*."""
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskGaussianBeamPanel(obj, created=created))

    class CommandAddGaussianBeam:
        """Create a Gaussian-beam Source on a domain face and open its editor."""

        def GetResources(self):
            return {
                "Pixmap": _BEAM_ICON,
                "MenuText": "Add Gaussian Beam",
                "ToolTip": "Add a directional Gaussian-beam source launched from "
                "a domain face, with a selectable polarization, waist and "
                "temporal excitation",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = active_simulation(doc)
            if sim is None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: create a Simulation before adding a Gaussian beam.\n"
                )
                return

            doc.openTransaction("Wavesim: Add Gaussian Beam")
            try:
                beam = doc.addObject("App::FeaturePython", "GaussianBeam")
                GaussianBeamObject(beam)
                beam.Label = "Gaussian Beam ({})".format(_describe(beam))
                if beam.ViewObject is not None:
                    GaussianBeamViewProvider(beam.ViewObject)
                sources_group(sim).addObject(beam)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()

            _open_beam_panel(beam, created=True)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    Gui.addCommand("Wavesim_AddGaussianBeam", CommandAddGaussianBeam())
