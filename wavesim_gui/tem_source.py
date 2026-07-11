# -*- coding: utf-8 -*-
"""TEM (waveguide-port) source for the Wavesim workbench (Session 9).

A *TEM Source* launches a transverse-electromagnetic port mode on a domain face.
Unlike the point :mod:`~wavesim_gui.source` (a single soft cell), it excites a
whole grid plane with the modal field profile the solver's TEM mode solver finds
for the PEC cross-section lying on that plane (e.g. a coax, stripline or
microstrip port). It is a scripted DocumentObject grouped under the simulation's
"Sources" child group.

Workflow
--------
* The user adds a TEM source, picks one of the six domain faces (the launch
  plane) in the task panel, and that face's boundary condition is set to **PML**
  automatically so the backward/reflected wave is absorbed -- the standard FDTD
  port setup.
* The mode itself is solved out-of-process by the conda-side ``runner.py`` (it
  needs scipy/numba, unavailable in FreeCAD's Python). The panel's **Compute
  Mode** button runs a *mode-only* job (no FDTD time-stepping) for **that port
  alone** and plots the result; nothing is saved, because the main Run re-solves
  every port's mode just before stepping the simulation and stores those with its
  own results, as clickable Results-tree nodes. Either view shows the mode shape
  and the port's per-unit-length parameters (Z0, eps_eff, C, L, v).

Rendering
---------
The source draws as a translucent teal plane on the chosen face spanning the
domain box (mirroring the snapshot monitor's plane), so the launch plane is
visible and the standard "eye" toggle shows/hides it. A matching teal arrow,
anchored to a plane corner and kept at a fixed on-screen size, shows the
direction of energy flow -- always *into* the simulation domain.

Units: FreeCAD geometry/properties are in millimetres; the solver works in
metres. :func:`tem_source_spec` converts the face plane's position to metres and
into the solver frame (measured from the domain origin) for the runner.

Importing this module registers ``Wavesim_AddTEMSource`` with ``Gui.addCommand``
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
_TEM_ICON = os.path.join(_RESOURCES_DIR, "tem_port.png")

_TYPE_PROP = "WavesimType"
_TEM_TYPE = "TEMSource"

# Name of the child group (created by CommandNewSimulation) holding sources.
_SOURCES_GROUP = "Sources"

# The six domain faces, in the solver's '<axis><0|1>' naming.
_FACES = ("x0", "x1", "y0", "y1", "z0", "z1")
# Human labels for the face dropdown.
_FACE_LABELS = {
    "x0": "X min (x0)", "x1": "X max (x1)",
    "y0": "Y min (y0)", "y1": "Y max (y1)",
    "z0": "Z min (z0)", "z1": "Z max (z1)",
}

# Excitation waveform families + object<->spec glue live in the shared
# workbench-side catalogue :mod:`wavesim_gui.excitation`.
_EXCITATIONS = exc.EXCITATION_LABELS

# Which transverse fields to inject. Driving both E and H launches a directional
# (one-way) wave; E only is simpler but bidirectional. Display label -> token.
_FIELDS_LABELS = ["E and H (directional)", "E only (bidirectional)"]
_FIELDS_TOKEN = {"E and H (directional)": "EH", "E only (bidirectional)": "E"}
_FIELDS_FROM_TOKEN = {v: k for k, v in _FIELDS_TOKEN.items()}

# Boundary condition forced on the launch face (absorbing port).
_PORT_BC = "PML"

# Translucent teal plane, distinct from the orange monitor / green point source.
_TEM_COLOR = (0.0, 0.80, 0.80)
_TEM_TRANSPARENCY = 0.6

# Energy-flow arrow: kept at a fixed on-screen length (pixels) regardless of zoom.
_ARROW_PIXELS = 90.0

_MM_PER_M = 1000.0
_AXIS_IDX = {"x": 0, "y": 1, "z": 2}

# The two transverse axes of a face, in the solver's mode-slice order (matching
# ``wavesim.mode_solver._NORMAL_CFG``): the ``bounds`` rect is (a, b) in this
# order, so ``_bounds_rect_mm`` / ``tem_source_spec`` stay consistent with it.
_TRANSVERSE = {"x": ("y", "z"), "y": ("x", "z"), "z": ("x", "y")}


# --------------------------------------------------------------------------- #
# Document-object model
# --------------------------------------------------------------------------- #

class TEMSourceObject:
    """``Proxy`` for a TEM port-source document object.

    Properties:
        ``Face``       -- domain face the port launches from ('x0'..'z1').
        ``Fields``     -- transverse fields injected ('EH' directional / 'E').
        ``Excitation`` + one property per waveform parameter (Gaussian pulse,
                          sine, rectangular, Gaussian+sine); added and kept in
                          sync by :func:`excitation.ensure_object_props`.

    Hidden ``Corners`` carries the launch plane's four world-mm corners for the
    view provider; ``execute`` keeps them in sync with the domain bounds + face.
    """

    def __init__(self, obj):
        self.Type = _TEM_TYPE
        obj.Proxy = self

        if not hasattr(obj, _TYPE_PROP):
            obj.addProperty(
                "App::PropertyString", _TYPE_PROP, "Wavesim",
                "Marks this object as a Wavesim TEM source",
            )
            setattr(obj, _TYPE_PROP, _TEM_TYPE)
            obj.setEditorMode(_TYPE_PROP, 1)  # read-only identity marker

        if not hasattr(obj, "Face"):
            obj.addProperty(
                "App::PropertyEnumeration", "Face", "Port",
                "Domain face the TEM port launches from (set to PML "
                "automatically)",
            )
            obj.Face = list(_FACES)
            obj.Face = "z0"
        if not hasattr(obj, "Fields"):
            obj.addProperty(
                "App::PropertyEnumeration", "Fields", "Port",
                "Transverse fields injected: E and H (directional) or E only",
            )
            obj.Fields = ["EH", "E"]
            obj.Fields = "EH"
        if not hasattr(obj, "Conductor"):
            obj.addProperty(
                "App::PropertyInteger", "Conductor", "Port",
                "Which solved TEM mode to launch: the conductor label of the "
                "energized conductor (shown in the mode plot after 'Compute "
                "Mode'). 0 = the dominant (first) mode.",
            )
            obj.Conductor = 0

        # Optional in-plane bounds: an edge/face whose bounding box confines the
        # mode solve to a sub-rectangle of the launch face (empty = whole face).
        if not hasattr(obj, "BoundsSel"):
            obj.addProperty(
                "App::PropertyLinkSub", "BoundsSel", "Port",
                "Optional edge/face whose in-plane bounding box confines the TEM "
                "mode solve to a sub-rectangle of the launch face (empty = whole "
                "face). Set via the task panel.",
            )
            obj.setEditorMode("BoundsSel", 2)  # hidden; set via the task panel

        # Excitation enum + one property per waveform parameter (shared scheme).
        exc.ensure_object_props(obj)

        # Plane corners (hidden, four world-mm points) for the view provider.
        if not hasattr(obj, "Corners"):
            obj.addProperty("App::PropertyVectorList", "Corners", "Plane", "")
            obj.setEditorMode("Corners", 2)  # hidden

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _TEM_TYPE)
        # Back-fill the conductor-selection property on ports saved before it
        # existed (defaults to the dominant mode, the old behaviour).
        if not hasattr(obj, "Conductor"):
            obj.addProperty(
                "App::PropertyInteger", "Conductor", "Port",
                "Which solved TEM mode to launch: the conductor label of the "
                "energized conductor (shown in the mode plot after 'Compute "
                "Mode'). 0 = the dominant (first) mode.",
            )
            obj.Conductor = 0
        # Back-fill the optional in-plane bounds selection (whole face when unset).
        if not hasattr(obj, "BoundsSel"):
            obj.addProperty(
                "App::PropertyLinkSub", "BoundsSel", "Port",
                "Optional edge/face whose in-plane bounding box confines the TEM "
                "mode solve to a sub-rectangle of the launch face (empty = whole "
                "face). Set via the task panel.",
            )
            obj.setEditorMode("BoundsSel", 2)  # hidden; set via the task panel
        # Re-run property setup so ports saved before the extra waveforms gain
        # the new options + parameter properties and editor modes are re-asserted.
        exc.ensure_object_props(obj)

    def execute(self, obj):
        """Size/orient the drawn launch plane to the domain bounds and face."""
        sim = active_simulation(obj.Document)
        dom = domain_mod.find_domain(sim) if sim else None
        if dom is not None and (dom.DomainMax - dom.DomainMin).Length > 1.0e-9:
            mn, mx = dom.DomainMin, dom.DomainMax
        else:
            # No sized domain yet: a small default cube so the plane is visible.
            half = 5.0
            mn = FreeCAD.Vector(-half, -half, -half)
            mx = FreeCAD.Vector(half, half, half)
        rect = _bounds_rect_mm(dom, str(obj.Face), getattr(obj, "BoundsSel", None))
        obj.Corners = [FreeCAD.Vector(*p)
                       for p in _face_corners(mn, mx, str(obj.Face), rect)]

    def dumps(self):
        return {"Type": getattr(self, "Type", _TEM_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _TEM_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


def _face_corners(mn, mx, face, rect=None):
    """Four (x, y, z) corners of the *face* plane spanning the box *mn*..*mx*.

    When *rect* ``(a0, a1, b0, b1)`` (world mm, transverse slice order) is given
    the plane is shrunk to that in-plane sub-rectangle, so a bounded TEM port
    draws only the region its mode is solved on.
    """
    axis = face[0]
    hi = face.endswith("1")
    if axis == "x":
        x = mx.x if hi else mn.x
        y0, y1, z0, z1 = (rect if rect is not None else (mn.y, mx.y, mn.z, mx.z))
        return [(x, y0, z0), (x, y1, z0), (x, y1, z1), (x, y0, z1)]
    if axis == "y":
        y = mx.y if hi else mn.y
        x0, x1, z0, z1 = (rect if rect is not None else (mn.x, mx.x, mn.z, mx.z))
        return [(x0, y, z0), (x1, y, z0), (x1, y, z1), (x0, y, z1)]
    z = mx.z if hi else mn.z
    x0, x1, y0, y1 = (rect if rect is not None else (mn.x, mx.x, mn.y, mx.y))
    return [(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)]


def _bounds_sel_bbox(bounds_sel):
    """World-mm :class:`FreeCAD.BoundBox` of a ``BoundsSel`` LinkSub, or ``None``.

    *bounds_sel* is an ``App::PropertyLinkSub`` value ``(object, (subnames,))``.
    The picked sub-elements' bounding boxes are unioned; when no sub-element is
    named the whole linked shape is used. ``None`` when it can't be resolved.
    """
    if not bounds_sel:
        return None
    link, subs = bounds_sel[0], bounds_sel[1]
    shape = getattr(link, "Shape", None)
    if shape is None:
        return None
    subs = [s for s in (subs or []) if s]
    elems = []
    if subs:
        for sub in subs:
            try:
                elems.append(shape.getElement(sub))
            except Exception:
                continue
    else:
        elems = [shape]
    boxes = []
    for elem in elems:
        try:
            boxes.append(elem.BoundBox)
        except Exception:
            continue
    if not boxes:
        return None
    return FreeCAD.BoundBox(
        min(b.XMin for b in boxes), min(b.YMin for b in boxes),
        min(b.ZMin for b in boxes), max(b.XMax for b in boxes),
        max(b.YMax for b in boxes), max(b.ZMax for b in boxes),
    )


def _bounds_rect_mm(dom, face, bounds_sel):
    """In-plane rect ``(a0, a1, b0, b1)`` (world mm) of *bounds_sel* on *face*.

    Projects the selection's bounding box onto the face's two transverse axes
    (solver slice order, see :data:`_TRANSVERSE`) and clamps it to the domain
    face. Returns ``None`` when no usable selection is set (⇒ whole face).
    """
    bb = _bounds_sel_bbox(bounds_sel)
    if bb is None:
        return None
    ax_a, ax_b = _TRANSVERSE[face[0]]
    lo = {"x": bb.XMin, "y": bb.YMin, "z": bb.ZMin}
    hi = {"x": bb.XMax, "y": bb.YMax, "z": bb.ZMax}
    a0, a1, b0, b1 = lo[ax_a], hi[ax_a], lo[ax_b], hi[ax_b]
    if dom is not None and (dom.DomainMax - dom.DomainMin).Length > 1.0e-9:
        dmn, dmx = dom.DomainMin, dom.DomainMax
        dlo = {"x": dmn.x, "y": dmn.y, "z": dmn.z}
        dhi = {"x": dmx.x, "y": dmx.y, "z": dmx.z}
        a0, a1 = max(a0, dlo[ax_a]), min(a1, dhi[ax_a])
        b0, b1 = max(b0, dlo[ax_b]), min(b1, dhi[ax_b])
    if a1 <= a0 or b1 <= b0:
        return None
    return (a0, a1, b0, b1)


def _bounds_desc(obj):
    """Short human label for a port's ``BoundsSel`` (or a 'whole face' note)."""
    sel = getattr(obj, "BoundsSel", None)
    if not sel:
        return "Whole face (no bounds)"
    link, subs = sel[0], sel[1]
    subs = [s for s in (subs or []) if s]
    name = getattr(link, "Label", None) or getattr(link, "Name", "?")
    return "{} ({})".format(name, ", ".join(subs)) if subs else str(name)


def _flow_direction(face):
    """Unit vector of energy flow *into* the domain from the launch *face*.

    A port on a low face (``x0``/``y0``/``z0``) radiates in the +axis direction;
    one on a high face radiates in the -axis direction. Either way the wave
    flows inward, away from the absorbing PML face it launches from.
    """
    axis = face[0]
    sign = 1.0 if face.endswith("0") else -1.0
    return {
        "x": (sign, 0.0, 0.0),
        "y": (0.0, sign, 0.0),
        "z": (0.0, 0.0, sign),
    }[axis]


# --------------------------------------------------------------------------- #
# Lookup helpers & job serialisation
# --------------------------------------------------------------------------- #

def is_tem_source(obj):
    """Return True if *obj* is a Wavesim TEM Source object."""
    return getattr(obj, _TYPE_PROP, None) == _TEM_TYPE


def sources_group(sim):
    """Return the "Sources" child group of *sim* (or *sim* itself if missing)."""
    if sim is None:
        return None
    for child in sim.Group:
        if child.Name == _SOURCES_GROUP or child.Label == _SOURCES_GROUP:
            return child
    return sim


def find_tem_sources(sim):
    """Return all TEM Source objects under the Simulation container *sim*."""
    grp = sources_group(sim)
    if grp is None:
        return []
    return [obj for obj in grp.Group if is_tem_source(obj)]


def tem_source_spec(obj, origin_m):
    """Return the ``job.json`` ``tem_sources`` dict for *obj* in the solver frame.

    The launch plane sits on the chosen domain face; its position along the face
    normal is taken from the domain box and shifted into the solver frame (the
    domain origin is subtracted, mirroring :func:`source.source_spec`).
    """
    sim = active_simulation(obj.Document)
    dom = domain_mod.find_domain(sim) if sim else None
    face = str(obj.Face)
    axis = domain_mod.face_axis(face)
    world_mm = domain_mod.face_world_coord_mm(dom, face) if dom is not None else 0.0
    position = world_mm / _MM_PER_M - origin_m[_AXIS_IDX[axis]]
    # Propagation sign along ``normal`` for the launch to flow *into* the domain:
    # +1 from a low face (x0/y0/z0), -1 from a high face (x1/y1/z1). The solver's
    # mode profiles assume +normal propagation, so the runner flips H when this is
    # negative (mirrors _flow_direction, which aims the viewport arrow).
    spec = {
        "name": str(obj.Label or obj.Name),
        "normal": axis,
        "position": position,
        "direction": 1.0 if face.endswith("0") else -1.0,
        "conductor_id": int(getattr(obj, "Conductor", 0)),
        "excitation": exc.spec_from_object(obj),
        "fields": str(getattr(obj, "Fields", "EH")),
    }
    _add_bounds_spec(spec, dom, face, axis, getattr(obj, "BoundsSel", None), origin_m)
    return spec


def _add_bounds_spec(spec, dom, face, axis, bounds_sel, origin_m):
    """Attach a solver-frame ``"bounds"`` rect to *spec* when one is selected.

    Shared by the TEM source and the SPICE TEM port. The world-mm in-plane rect
    from :func:`_bounds_rect_mm` is converted to solver metres on the two
    transverse axes (the domain origin subtracted, like the plane position);
    absent ⇒ the runner solves on the whole face.
    """
    rect = _bounds_rect_mm(dom, face, bounds_sel)
    if rect is None:
        return
    ax_a, ax_b = _TRANSVERSE[axis]
    ia, ib = _AXIS_IDX[ax_a], _AXIS_IDX[ax_b]
    a0, a1, b0, b1 = rect
    spec["bounds"] = [
        a0 / _MM_PER_M - origin_m[ia], a1 / _MM_PER_M - origin_m[ia],
        b0 / _MM_PER_M - origin_m[ib], b1 / _MM_PER_M - origin_m[ib],
    ]


def _describe(obj):
    """Short human label, e.g. ``z0, Gaussian Pulse @ 30 GHz``.

    Uses the simulation's frequency unit; the rectangular pulse has no frequency.
    """
    doc = getattr(obj, "Document", None)
    sim = active_simulation(doc) if doc is not None else None
    return "{}, {}".format(getattr(obj, "Face", "z0"),
                           exc.excitation_label(obj, sim))


# --------------------------------------------------------------------------- #
# GUI: view provider, task panel, command
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

    # The TEM panel reuses the point source's excitation widgets/plot mixin.
    from wavesim_gui import source as source_mod

    def _build_arrow_geometry():
        """A unit arrow (shaft + head) pointing along +Y, base at the origin.

        Nominal total length 1.0; the view provider's SoScale stretches it to a
        fixed pixel size and an SoRotation aims it along the energy-flow
        direction. Coin's SoCylinder/SoCone are centred on the origin with their
        axis along +Y, so each is translated up by half its height to stack.
        """
        from pivy import coin

        sep = coin.SoSeparator()

        shaft_t = coin.SoTranslation()
        shaft_t.translation.setValue(0.0, 0.35, 0.0)
        sep.addChild(shaft_t)
        shaft = coin.SoCylinder()
        shaft.radius = 0.04
        shaft.height = 0.7
        sep.addChild(shaft)

        head_t = coin.SoTranslation()
        head_t.translation.setValue(0.0, 0.5, 0.0)  # 0.35 -> 0.85 (head centre)
        sep.addChild(head_t)
        head = coin.SoCone()
        head.bottomRadius = 0.12
        head.height = 0.3
        sep.addChild(head)

        return sep

    class TEMSourceViewProvider:
        """Coin view provider drawing the port as a translucent teal plane."""

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
            material.diffuseColor.setValue(*_TEM_COLOR)
            material.transparency.setValue(_TEM_TRANSPARENCY)
            root.addChild(material)

            self._coords = coin.SoCoordinate3()
            root.addChild(self._coords)
            self._face = coin.SoFaceSet()
            root.addChild(self._face)

            # Opaque border so the plane edges read clearly.
            border = coin.SoSeparator()
            bcolor = coin.SoBaseColor()
            bcolor.rgb.setValue(*_TEM_COLOR)
            border.addChild(bcolor)
            bstyle = coin.SoDrawStyle()
            bstyle.lineWidth = 2
            border.addChild(bstyle)
            self._border_coords = coin.SoCoordinate3()
            border.addChild(self._border_coords)
            self._border_lines = coin.SoIndexedLineSet()
            border.addChild(self._border_lines)
            root.addChild(border)

            # Energy-flow arrow, anchored to a plane corner, pointing into the
            # domain. A callback rescales it every frame so it keeps a constant
            # on-screen size; the SoScale it writes feeds Coin's element stack so
            # bounding boxes stay correct.
            arrow = coin.SoSeparator()
            acolor = coin.SoBaseColor()
            acolor.rgb.setValue(*_TEM_COLOR)
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
            arrow.addChild(_build_arrow_geometry())
            self._arrow_on = False
            root.addChild(arrow)

            self._root = root
            vobj.addDisplayMode(root, "Plane")
            self._rebuild()

        def _scale_arrow_cb(self, user, action):
            """Keep the arrow a fixed pixel length by setting its SoScale.

            Runs only for the GL render action (the others have no view volume).
            Reads the current view volume, viewport and model matrix from the
            traversal state to map :data:`_ARROW_PIXELS` to world units at the
            arrow's anchor, which works for both perspective and orthographic
            cameras.
            """
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
            size = vv.getWorldToScreenScale(world, _ARROW_PIXELS / height_px)
            # Only write the field when the size meaningfully changed: setting it
            # every frame would notify the scene graph and spin a redraw loop.
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
            # Collapse the arrow (the scale callback no-ops while off).
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

            # Anchor the arrow to the first plane corner and point it into the
            # domain along the face normal.
            self._arrow_pos.translation.setValue(*pts[0])
            d = _flow_direction(str(obj.Face))
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
            return _TEM_ICON

        def setEdit(self, vobj, mode=0):
            _open_tem_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_tem_panel(vobj.Object)
            return True

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class TaskTEMSourcePanel(source_mod.ExcitationParamsMixin):
        """Task panel to edit a TEM port: face, fields and excitation.

        "Compute Mode" solves and visualises *this* port's mode now (out of
        process, no FDTD, nothing saved); OK commits the source and leaves the
        mode for the main Run. Cancel removes a freshly-created source so it
        leaves no trace.
        """

        def __init__(self, obj, created=False):
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets

            self.obj = obj
            self.created = created
            self._orig_face = str(getattr(obj, "Face", "z0"))
            self._orig_bounds = getattr(obj, "BoundsSel", None)

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim TEM Source")
            layout = QtWidgets.QFormLayout(form)

            self._face = QtWidgets.QComboBox()
            for f in _FACES:
                self._face.addItem(_FACE_LABELS[f], f)
            self._face.setCurrentIndex(
                max(0, list(_FACES).index(str(getattr(obj, "Face", "z0"))))
            )

            self._fields = QtWidgets.QComboBox()
            self._fields.addItems(_FIELDS_LABELS)
            self._fields.setCurrentText(
                _FIELDS_FROM_TOKEN.get(str(getattr(obj, "Fields", "EH")),
                                       _FIELDS_LABELS[0])
            )

            # Which solved mode to launch, by energized-conductor label. 0 means
            # the dominant (first) mode; other values match the "conductor N"
            # modes Compute Mode plots.
            self._conductor = QtWidgets.QSpinBox()
            self._conductor.setRange(0, 999)
            self._conductor.setSpecialValueText("Dominant (first mode)")
            self._conductor.setValue(int(getattr(obj, "Conductor", 0)))

            layout.addRow("Launch face:", self._face)
            layout.addRow("Inject fields:", self._fields)
            layout.addRow("Energize conductor:", self._conductor)

            # Optional in-plane bounds: pick an edge/face whose bounding box
            # confines the mode solve to a sub-rectangle of the launch face.
            self._bounds_label = QtWidgets.QLabel(_bounds_desc(obj))
            self._bounds_label.setWordWrap(True)
            pick = QtWidgets.QPushButton("Select bounding edge/face")
            clear = QtWidgets.QPushButton("Clear")
            brow = QtWidgets.QWidget()
            blay = QtWidgets.QHBoxLayout(brow)
            blay.setContentsMargins(0, 0, 0, 0)
            blay.addWidget(pick)
            blay.addWidget(clear)
            layout.addRow("Solve bounds:", self._bounds_label)
            layout.addRow("", brow)
            pick.clicked.connect(self._pick_bounds)
            clear.clicked.connect(self._clear_bounds)

            # Excitation combo + per-waveform parameter rows + preview button
            # (shared with the point-source panel).
            self.build_excitation_ui(layout, QtWidgets)

            self._compute = QtWidgets.QPushButton("Compute Mode")
            layout.addRow(self._compute)

            info = QtWidgets.QLabel(
                "The port launches the TEM mode of the PEC cross-section on the "
                "chosen face (which is set to PML automatically). The face must "
                "cut at least two conductors. Pick a temporal waveform and its "
                "parameters (preview with the plot button). 'Compute Mode' solves "
                "and plots this port's mode(s) now, for viewing only; they are "
                "re-solved and saved when you Run. "
                "With several conductors on the face (e.g. two coax cross-sections), "
                "Compute Mode plots one mode per signal conductor (pick between "
                "them in the plot window) — set 'Energize conductor' to that "
                "conductor's N to drive it (0 launches the dominant mode). "
                "Optionally select an edge/face to confine the mode solve to its "
                "in-plane bounding box (e.g. a single connector's cross-section on "
                "a shared plane); Clear restores the whole face. "
                "Frequency/time units are set on the Simulation object."
            )
            info.setWordWrap(True)
            layout.addRow(info)

            # Live-update the drawn plane as the face changes.
            self._face.currentIndexChanged.connect(self._live_face)
            self._compute.clicked.connect(self._on_compute)

            self.form = form

        def _selected_face(self):
            return self._face.currentData() or _FACES[self._face.currentIndex()]

        def _live_face(self, *_):
            self.obj.Face = self._selected_face()
            self.obj.Document.recompute()

        def _pick_bounds(self, *_):
            """Set BoundsSel from the first edge/face in the current selection."""
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets
            for s in Gui.Selection.getSelectionEx():
                picks = [n for n in (getattr(s, "SubElementNames", []) or [])
                         if n.startswith("Edge") or n.startswith("Face")]
                if picks:
                    self.obj.BoundsSel = (s.Object, [picks[0]])
                    self._bounds_label.setText(_bounds_desc(self.obj))
                    self.obj.Document.recompute()
                    return
            QtWidgets.QMessageBox.information(
                self.form, "Wavesim TEM Source",
                "Select an edge or face in the 3D view first, then click "
                "'Select bounding edge/face'.",
            )

        def _clear_bounds(self, *_):
            self.obj.BoundsSel = None
            self._bounds_label.setText(_bounds_desc(self.obj))
            self.obj.Document.recompute()

        def _commit(self, title):
            """Write the widget values onto the object and force PML on the face.

            Returns after committing + recomputing; the domain is re-synced so it
            re-sizes to the (possibly changed) port plane. Shared by Accept and
            Compute Mode so both see exactly the same persisted state.
            """
            doc = self.obj.Document
            # Restore the original face/bounds first so the transaction captures
            # the full change (the live edits already moved them outside it).
            new_bounds = getattr(self.obj, "BoundsSel", None)
            self.obj.Face = self._orig_face
            self.obj.BoundsSel = self._orig_bounds
            doc.openTransaction(title)
            face = self._selected_face()
            self.obj.Face = face
            self.obj.BoundsSel = new_bounds
            self.obj.Fields = _FIELDS_TOKEN[self._fields.currentText()]
            self.obj.Conductor = int(self._conductor.value())
            self.write_excitation(self.obj)
            self.obj.Label = "TEM Source ({})".format(_describe(self.obj))
            # Absorbing port: force the launch face to PML.
            domain_mod.set_face_bc(domain_mod.find_domain(active_simulation(doc)),
                                   face, _PORT_BC)
            doc.commitTransaction()
            doc.recompute()
            domain_mod.notify_domain_inputs_changed(doc)
            self._orig_face = face
            self._orig_bounds = new_bounds

        def _on_compute(self, *_):
            self._commit("Wavesim: Edit TEM Source")
            run_mode_solve(self.obj.Document, self.obj)

        def accept(self):
            self._commit("Wavesim: Edit TEM Source")
            Gui.Control.closeDialog()
            return True

        def reject(self):
            doc = self.obj.Document
            if self.created:
                doc.openTransaction("Wavesim: Cancel TEM Source")
                doc.removeObject(self.obj.Name)
                doc.commitTransaction()
                doc.recompute()
            else:
                self.obj.Face = self._orig_face
                self.obj.BoundsSel = self._orig_bounds
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

    def _open_tem_panel(obj, created=False):
        """Open (or replace) the TEM source task panel bound to *obj*."""
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskTEMSourcePanel(obj, created=created))

    def _isolate_port(spec, arrays, port_obj):
        """Cut a job *spec* down to the single mode-solved port *port_obj*.

        "Compute Mode" previews one port, so the other ports' (expensive) mode
        solves have nothing to do here. Ports are matched on the ``name`` their
        ``*_spec`` writes -- the object's label, the same key the runner echoes
        into ``summary["modes"]``. Mode-mesh arrays belonging to the dropped ports
        are pruned from *arrays* so the preview's ``materials.npz`` carries only
        what its one mode solve reads. Returns ``False`` when *port_obj* has no
        entry in the job at all.
        """
        name = str(getattr(port_obj, "Label", "") or getattr(port_obj, "Name", ""))
        tem = [t for t in spec.get("tem_sources") or [] if t.get("name") == name]
        spice = [p for p in spec.get("spice_ports") or []
                 if p.get("kind") == "tem" and p.get("name") == name]
        if not tem and not spice:
            return False
        spec["tem_sources"] = tem
        spec["spice_ports"] = spice
        keep = {e["mode_mesh"]["key"] for e in tem + spice if e.get("mode_mesh")}
        for key in [k for k in arrays if k.startswith("modemesh_")]:
            if key.rsplit("_", 1)[0] not in keep:  # 'modemesh_<i>_pec' -> 'modemesh_<i>'
                del arrays[key]
        return True

    def run_mode_solve(doc, port_obj):
        """Solve and plot the TEM mode of *port_obj* out of process (no FDTD run).

        Builds the usual voxelised job, cuts it down to this one port (see
        :func:`_isolate_port`), flags it ``mode_only`` and runs the conda-side
        runner in a throwaway directory. The solved mode is plotted straight from
        there and the directory is deleted afterwards: the preview exists to be
        looked at, and a full Run re-solves every port's mode and saves those
        alongside its own results. *port_obj* is the TEM source or SPICE TEM port
        whose panel pressed "Compute Mode".
        """
        try:
            from PySide import QtWidgets
        except ImportError:
            from PySide import QtGui as QtWidgets
        from wavesim_gui import job as job_mod
        from wavesim_gui import run as run_mod
        from wavesim_gui import voxelize as vox_mod
        from wavesim_gui import results as results_mod

        main = Gui.getMainWindow()
        # Voxelisation runs on the GUI thread and can be slow; show a cancelable
        # progress dialog while it sweeps the geometry.
        vox_dialog, vox_cb = run_mod.voxelization_progress(
            main, "Wavesim Mode Solve", "Voxelizing geometry..."
        )
        try:
            spec, arrays = vox_mod.build_job_from_document(doc, progress=vox_cb)
        except vox_mod.VoxelizationCancelled:
            vox_dialog.close()
            FreeCAD.Console.PrintWarning("Wavesim: mode solve cancelled.\n")
            return
        except vox_mod.GridRequiredError as exc:
            vox_dialog.close()
            QtWidgets.QMessageBox.warning(main, "Wavesim Mode Solve", str(exc))
            return
        vox_dialog.close()
        if spec is None or arrays is None:
            QtWidgets.QMessageBox.warning(
                main, "Wavesim Mode Solve",
                "Assign materials (with PEC conductors crossing the port plane) "
                "before computing a mode.",
            )
            return
        if not _isolate_port(spec, arrays, port_obj):
            QtWidgets.QMessageBox.warning(
                main, "Wavesim Mode Solve",
                "This port has no plane to solve. Check it sits under the "
                "simulation's Sources group.",
            )
            return

        spec["mode_only"] = True
        spec["steps"] = 1
        # A preview is never saved: it runs in a temp dir, is plotted from there,
        # and the dir goes away. Only a full Run writes modes to the results path.
        workdir = job_mod.temp_workdir()
        try:
            job_mod.write_job(workdir, spec)
            vox_mod.write_materials(workdir, arrays)

            FreeCAD.Console.PrintMessage(
                "Wavesim: solving the TEM mode of '{}' in {}\n".format(
                    port_obj.Label, workdir
                )
            )
            summary = run_mod.run_job(
                workdir, 1, parent=main,
                message="Preparing TEM mode solve...", busy=True,
            )
            if summary is None:
                return
            if not summary.get("modes"):
                QtWidgets.QMessageBox.information(
                    main, "Wavesim Mode Solve",
                    "No TEM mode was found. A TEM port plane needs at least two "
                    "PEC conductors (e.g. a signal conductor and a ground/shield).",
                )
                return
            # Reads every array it needs before returning, so the temp dir below
            # can go while the plot window stays open.
            if not results_mod.show_mode_preview(workdir, summary):
                FreeCAD.Console.PrintWarning(
                    "Wavesim: the solved mode of '{}' could not be plotted.\n"
                    .format(port_obj.Label)
                )
        finally:
            job_mod.discard_workdir(workdir)

    class CommandAddTEMSource:
        """Create a TEM port Source on a domain face and open its editor."""

        def GetResources(self):
            return {
                "Pixmap": _TEM_ICON,
                "MenuText": "Add TEM Source",
                "ToolTip": "Add a TEM waveguide-port source that launches the "
                "modal field of a PEC cross-section on a domain face",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = active_simulation(doc)
            if sim is None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: create a Simulation before adding a TEM source.\n"
                )
                return

            doc.openTransaction("Wavesim: Add TEM Source")
            try:
                tem = doc.addObject("App::FeaturePython", "TEMSource")
                TEMSourceObject(tem)
                tem.Label = "TEM Source ({})".format(_describe(tem))
                if tem.ViewObject is not None:
                    TEMSourceViewProvider(tem.ViewObject)
                sources_group(sim).addObject(tem)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()

            _open_tem_panel(tem, created=True)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    Gui.addCommand("Wavesim_AddTEMSource", CommandAddTEMSource())
