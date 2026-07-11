# -*- coding: utf-8 -*-
"""SPICE co-simulation ports for the Wavesim workbench.

A *SPICE port* couples the FDTD field solve to a user-authored ngspice netlist
through one lumped port, driven in lockstep by the solver's
:class:`wavesim.sources.SpicePort` (see :mod:`wavesim.spice`). Each FDTD step the
port hands ngspice its Thevenin equivalent (a voltage behind the port's discrete
self-coupling) and reads the resulting branch current back, so the circuit and
the fields advance together.

Two kinds are offered, mirroring ``SpicePort``'s two geometry modes:

* **SPICE Line Port** -- a straight lumped port across a gap, ``p0 -> p1``. Its
  endpoints come from a linked sketch/edge (drag a two-point sketch onto the
  port, exactly like the voltage/current path monitors). ``p0`` (the sketch
  curve's start) is the ``+`` terminal.
* **SPICE TEM Port** -- drives a whole waveguide-port plane on a domain face,
  using the solver's TEM mode of the PEC cross-section there (like the TEM
  source, but excited by the circuit rather than a waveform). "Compute Mode"
  previews the solved mode.

Both reference a netlist file and two node names ``(plus, minus)`` that must
already exist in it (wavesim splices its own port companion across them; the user
places no port component). One netlist file drives one port -- several ports run
independent ngspice instances, not a shared multi-port circuit.

The ngspice shared library is taken from the workbench Settings
(``ngspice_dll``); it is stamped into the job so the conda-side runner can load
it. Units: FreeCAD is millimetres, the solver metres -- the ``*_spec`` builders
convert to the solver frame (from the domain origin), mirroring
:func:`wavesim_gui.source.source_spec`.

Importing this module registers ``Wavesim_AddSpiceLinePort`` and
``Wavesim_AddSpiceTEMPort`` with ``Gui.addCommand`` when a GUI is available.
"""

import os

import FreeCAD

from wavesim_gui.commands import active_simulation
from wavesim_gui import domain as domain_mod
from wavesim_gui import tem_source as tem_mod


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_RESOURCES_DIR = os.path.join(_WB_DIR, "Resources")
_ICON = os.path.join(_RESOURCES_DIR, "port.png")
_SPICE_TEM_PORT_ICON = os.path.join(_RESOURCES_DIR, "spice_tem_port.png")
_SPICE_LINE_PORT_ICON = os.path.join(_RESOURCES_DIR, "spice_line_port.png")

_TYPE_PROP = "WavesimType"
_LINE_TYPE = "SpiceLinePort"
_TEM_TYPE = "SpiceTEMPort"

# SPICE ports are excitations/ports, so they live under the "Sources" group.
_SOURCES_GROUP = "Sources"

_MM_PER_M = 1000.0
_AXIS_IDX = {"x": 0, "y": 1, "z": 2}

# Boundary condition forced on a TEM port's launch face (absorbing port).
_PORT_BC = "PML"

# Line-port drawing: a magenta segment with pixel-sized end markers.
_LINE_COLOR = (0.85, 0.10, 0.85)
_PLUS_COLOR = (0.10, 0.80, 0.10)
_MINUS_COLOR = (0.90, 0.20, 0.20)

# Reuse the TEM source's face catalogue + field tokens so the two stay in step.
_FACES = tem_mod._FACES
_FACE_LABELS = tem_mod._FACE_LABELS
_FIELDS_LABELS = tem_mod._FIELDS_LABELS
_FIELDS_TOKEN = tem_mod._FIELDS_TOKEN
_FIELDS_FROM_TOKEN = tem_mod._FIELDS_FROM_TOKEN


# --------------------------------------------------------------------------- #
# Shared SPICE properties
# --------------------------------------------------------------------------- #

def _add_type_marker(obj, type_name):
    """Stamp the read-only ``WavesimType`` identity marker on *obj*."""
    if not hasattr(obj, _TYPE_PROP):
        obj.addProperty(
            "App::PropertyString", _TYPE_PROP, "Wavesim",
            "Marks this object as a Wavesim SPICE port",
        )
        setattr(obj, _TYPE_PROP, type_name)
        obj.setEditorMode(_TYPE_PROP, 1)  # read-only


def ensure_spice_props(obj):
    """Add the netlist/node/advanced properties shared by both SPICE ports.

    Idempotent (guarded by ``hasattr``) so it doubles as the ``onDocumentRestored``
    back-fill for ports saved before a property existed.
    """
    if not hasattr(obj, "Netlist"):
        obj.addProperty(
            "App::PropertyFile", "Netlist", "SPICE",
            "Path to the ngspice netlist file this port couples to. The two "
            "port nodes below must already exist in it; wavesim splices its own "
            "port companion across them (place no port component yourself).",
        )
    if not hasattr(obj, "NodePlus"):
        obj.addProperty(
            "App::PropertyString", "NodePlus", "SPICE",
            "Netlist node wired to the port '+' terminal (p0 / the launch "
            "direction). Must already exist in the netlist.",
        )
        obj.NodePlus = "port1p"
    if not hasattr(obj, "NodeMinus"):
        obj.addProperty(
            "App::PropertyString", "NodeMinus", "SPICE",
            "Netlist node wired to the port '-' terminal. A DC path to ground "
            "node '0' is required (simplest: use '0' here).",
        )
        obj.NodeMinus = "0"
    if not hasattr(obj, "UseInitialConditions"):
        obj.addProperty(
            "App::PropertyBool", "UseInitialConditions", "SPICE Advanced",
            "Skip ngspice's DC operating-point solve and start the transient "
            "from the netlist's initial conditions (SPICE '.tran ... uic'). "
            "Leave off unless your netlist sets nonzero initial conditions -- "
            "the fields start at zero, so the DC operating point is correct.",
        )
        obj.UseInitialConditions = False
    if not hasattr(obj, "InvertPortCurrent"):
        obj.addProperty(
            "App::PropertyBool", "InvertPortCurrent", "SPICE Advanced",
            "Flip the sign of the port branch current if it couples with the "
            "wrong polarity (maps to the solver's sign=-1). The default is "
            "correct for standard node ordering.",
        )
        obj.InvertPortCurrent = False


def _spice_common_spec(obj):
    """The netlist/nodes/sign/uic fields common to both port specs."""
    netlist = str(getattr(obj, "Netlist", "") or "")
    return {
        "netlist": os.path.abspath(netlist) if netlist else "",
        "nodes": [str(getattr(obj, "NodePlus", "port1p")),
                  str(getattr(obj, "NodeMinus", "0"))],
        "sign": -1.0 if bool(getattr(obj, "InvertPortCurrent", False)) else 1.0,
        "uic": bool(getattr(obj, "UseInitialConditions", False)),
    }


def _netlist_name(obj):
    """Short netlist file name for labels, or a placeholder when unset."""
    netlist = str(getattr(obj, "Netlist", "") or "")
    return os.path.basename(netlist) if netlist else "no netlist"


# --------------------------------------------------------------------------- #
# Line port: document-object model
# --------------------------------------------------------------------------- #

def _line_endpoints_mm(obj):
    """Ordered world-mm endpoints ``(p0, p1)`` of the port's linked curve.

    Mirrors :func:`wavesim_gui.monitors._monitor_path_mm`: the linked shape's
    edges are sorted into wires and the longest is taken; its two ends (following
    the curve's own direction, so ``p0`` is the ``+`` terminal) are returned.
    ``None`` when no usable curve is linked.
    """
    sketch = getattr(obj, "Sketch", None)
    shape = getattr(sketch, "Shape", None) if sketch is not None else None
    edges = list(getattr(shape, "Edges", []) or []) if shape is not None else []
    if not edges:
        return None
    import Part

    wires = []
    for group in Part.sortEdges(edges):
        try:
            wires.append(Part.Wire(group))
        except Exception:
            continue
    if not wires:
        return None
    wire = max(wires, key=lambda w: w.Length)
    pts = wire.discretize(Number=2)
    if len(pts) < 2:
        return None
    return pts[0], pts[-1]


class SpiceLinePortObject:
    """``Proxy`` for a straight SPICE line port (``p0 -> p1``).

    Properties:
        ``Sketch``  -- link to the sketch/edge whose curve gives the endpoints
                       (assigned by dragging it onto the port in the tree).
        SPICE props -- ``Netlist``/``NodePlus``/``NodeMinus`` (+ advanced flags).

    Hidden ``P0``/``P1`` carry the endpoints (world mm) for the view provider;
    ``execute`` keeps them in sync with the linked curve.
    """

    def __init__(self, obj):
        self.Type = _LINE_TYPE
        obj.Proxy = self
        _add_type_marker(obj, _LINE_TYPE)

        if not hasattr(obj, "Sketch"):
            obj.addProperty(
                "App::PropertyLink", "Sketch", "Port",
                "Sketch/edge whose curve is the port line (drag a two-point "
                "sketch from the tree onto this port). Its start is the '+' end.",
            )

        ensure_spice_props(obj)

        # Endpoints (hidden) for the view provider, synced by execute().
        for name in ("P0", "P1"):
            if not hasattr(obj, name):
                obj.addProperty("App::PropertyVector", name, "Line", "")
                obj.setEditorMode(name, 2)  # hidden

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _LINE_TYPE)
        ensure_spice_props(obj)

    def execute(self, obj):
        """Sync the drawn endpoints to the linked curve (or collapse them)."""
        ends = _line_endpoints_mm(obj)
        if ends is None:
            obj.P0 = FreeCAD.Vector(0, 0, 0)
            obj.P1 = FreeCAD.Vector(0, 0, 0)
        else:
            obj.P0 = FreeCAD.Vector(ends[0])
            obj.P1 = FreeCAD.Vector(ends[1])

    def dumps(self):
        return {"Type": getattr(self, "Type", _LINE_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _LINE_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


# --------------------------------------------------------------------------- #
# TEM port: document-object model
# --------------------------------------------------------------------------- #

class SpiceTEMPortObject:
    """``Proxy`` for a TEM-plane SPICE port (drives a solved waveguide mode).

    Shares the TEM source's face/conductor/fields geometry (and its plane view
    provider) but is excited by the linked netlist instead of a waveform.
    """

    def __init__(self, obj):
        self.Type = _TEM_TYPE
        obj.Proxy = self
        _add_type_marker(obj, _TEM_TYPE)

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
                "Which solved TEM mode to drive: the conductor label of the "
                "energized conductor (shown in the mode plot after 'Compute "
                "Mode'). 0 = the dominant (first) mode.",
            )
            obj.Conductor = 0

        # Optional in-plane bounds (edge/face) confining the mode solve to a
        # sub-rectangle of the launch face; shared behaviour with the TEM source.
        if not hasattr(obj, "BoundsSel"):
            obj.addProperty(
                "App::PropertyLinkSub", "BoundsSel", "Port",
                "Optional edge/face whose in-plane bounding box confines the TEM "
                "mode solve to a sub-rectangle of the launch face (empty = whole "
                "face). Set via the task panel.",
            )
            obj.setEditorMode("BoundsSel", 2)  # hidden; set via the task panel

        ensure_spice_props(obj)

        if not hasattr(obj, "Corners"):
            obj.addProperty("App::PropertyVectorList", "Corners", "Plane", "")
            obj.setEditorMode("Corners", 2)  # hidden

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _TEM_TYPE)
        if not hasattr(obj, "BoundsSel"):
            obj.addProperty(
                "App::PropertyLinkSub", "BoundsSel", "Port",
                "Optional edge/face whose in-plane bounding box confines the TEM "
                "mode solve to a sub-rectangle of the launch face (empty = whole "
                "face). Set via the task panel.",
            )
            obj.setEditorMode("BoundsSel", 2)  # hidden; set via the task panel
        ensure_spice_props(obj)

    def execute(self, obj):
        """Size/orient the drawn launch plane to the domain bounds and face."""
        sim = active_simulation(obj.Document)
        dom = domain_mod.find_domain(sim) if sim else None
        if dom is not None and (dom.DomainMax - dom.DomainMin).Length > 1.0e-9:
            mn, mx = dom.DomainMin, dom.DomainMax
        else:
            half = 5.0
            mn = FreeCAD.Vector(-half, -half, -half)
            mx = FreeCAD.Vector(half, half, half)
        rect = tem_mod._bounds_rect_mm(dom, str(obj.Face),
                                       getattr(obj, "BoundsSel", None))
        obj.Corners = [FreeCAD.Vector(*p)
                       for p in tem_mod._face_corners(mn, mx, str(obj.Face), rect)]

    def dumps(self):
        return {"Type": getattr(self, "Type", _TEM_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _TEM_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


# --------------------------------------------------------------------------- #
# Lookup helpers & job serialisation
# --------------------------------------------------------------------------- #

def is_spice_line_port(obj):
    """Return True if *obj* is a Wavesim SPICE line port."""
    return getattr(obj, _TYPE_PROP, None) == _LINE_TYPE


def is_spice_tem_port(obj):
    """Return True if *obj* is a Wavesim SPICE TEM port."""
    return getattr(obj, _TYPE_PROP, None) == _TEM_TYPE


def sources_group(sim):
    """Return the "Sources" child group of *sim* (or *sim* itself if missing)."""
    if sim is None:
        return None
    for child in sim.Group:
        if child.Name == _SOURCES_GROUP or child.Label == _SOURCES_GROUP:
            return child
    return sim


def find_spice_line_ports(sim):
    """All SPICE line ports under the Simulation container *sim*."""
    grp = sources_group(sim)
    return [o for o in grp.Group if is_spice_line_port(o)] if grp else []


def find_spice_tem_ports(sim):
    """All SPICE TEM ports under the Simulation container *sim*."""
    grp = sources_group(sim)
    return [o for o in grp.Group if is_spice_tem_port(o)] if grp else []


def spice_line_port_spec(obj, origin_m):
    """Return the ``job.json`` ``spice_ports`` dict for a line port, or ``None``.

    The endpoints are the linked curve's ends, converted to solver metres (the
    domain origin subtracted, mirroring :func:`source.source_spec`). Skips the
    port with a warning if no curve is linked, so the run still proceeds.
    """
    ends = _line_endpoints_mm(obj)
    if ends is None:
        FreeCAD.Console.PrintWarning(
            "Wavesim: SPICE line port '{}' has no sketch curve assigned (drag a "
            "two-point sketch onto it in the tree); skipping it.\n".format(
                obj.Label
            )
        )
        return None
    p0, p1 = ends
    spec = {
        "kind": "line",
        "name": str(obj.Label or obj.Name),
        "p0": [p0.x / _MM_PER_M - origin_m[0],
               p0.y / _MM_PER_M - origin_m[1],
               p0.z / _MM_PER_M - origin_m[2]],
        "p1": [p1.x / _MM_PER_M - origin_m[0],
               p1.y / _MM_PER_M - origin_m[1],
               p1.z / _MM_PER_M - origin_m[2]],
    }
    spec.update(_spice_common_spec(obj))
    return spec


def spice_tem_port_spec(obj, origin_m):
    """Return the ``job.json`` ``spice_ports`` dict for a TEM port.

    Mirrors :func:`wavesim_gui.tem_source.tem_source_spec` for the plane geometry
    (normal/position/direction/conductor), plus the netlist coupling; the ``EH``/
    ``E`` field choice maps to SpicePort's ``directional`` flag.
    """
    sim = active_simulation(obj.Document)
    dom = domain_mod.find_domain(sim) if sim else None
    face = str(obj.Face)
    axis = domain_mod.face_axis(face)
    world_mm = domain_mod.face_world_coord_mm(dom, face) if dom is not None else 0.0
    position = world_mm / _MM_PER_M - origin_m[_AXIS_IDX[axis]]
    spec = {
        "kind": "tem",
        "name": str(obj.Label or obj.Name),
        "normal": axis,
        "position": position,
        "direction": 1.0 if face.endswith("0") else -1.0,
        "conductor_id": int(getattr(obj, "Conductor", 0)),
        "directional": str(getattr(obj, "Fields", "EH")) == "EH",
    }
    tem_mod._add_bounds_spec(spec, dom, face, axis,
                             getattr(obj, "BoundsSel", None), origin_m)
    spec.update(_spice_common_spec(obj))
    return spec


def _line_describe(obj):
    return "{}, {}/{}".format(
        _netlist_name(obj),
        getattr(obj, "NodePlus", "port1p"), getattr(obj, "NodeMinus", "0"),
    )


def _tem_describe(obj):
    return "{}, {}".format(getattr(obj, "Face", "z0"), _netlist_name(obj))


# --------------------------------------------------------------------------- #
# GUI: view providers, task panels, commands
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

    def _qt_widgets():
        try:
            from PySide import QtWidgets
        except ImportError:
            from PySide import QtGui as QtWidgets
        return QtWidgets

    def _is_curve_object(obj):
        """True if *obj* carries a curve Shape (edges, no solids)."""
        shape = getattr(obj, "Shape", None)
        if shape is None or getattr(shape, "Solids", None):
            return False
        return bool(getattr(shape, "Edges", None))

    # ------------------------------------------------------------------ #
    # Line port view provider (magenta segment + / - end markers)
    # ------------------------------------------------------------------ #

    class SpiceLinePortViewProvider:
        """Draws the port line between its endpoints; claims the linked sketch."""

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            from pivy import coin

            self.Object = vobj.Object
            root = coin.SoSeparator()

            self._coords = coin.SoCoordinate3()
            root.addChild(self._coords)

            mat = coin.SoMaterial()
            mat.diffuseColor.setValue(*_LINE_COLOR)
            root.addChild(mat)
            style = coin.SoDrawStyle()
            style.lineWidth = 3
            root.addChild(style)
            self._line = coin.SoLineSet()
            root.addChild(self._line)

            # Pixel-sized end markers: '+' (green) at p0, '-' (red) at p1.
            self._plus = self._marker(coin, _PLUS_COLOR,
                                      coin.SoMarkerSet.PLUS_9_9)
            root.addChild(self._plus["sep"])
            self._minus = self._marker(coin, _MINUS_COLOR,
                                       coin.SoMarkerSet.MINUS_9_9)
            root.addChild(self._minus["sep"])

            self._root = root
            vobj.addDisplayMode(root, "Line")
            self._rebuild()

        def _marker(self, coin, color, marker_index):
            sep = coin.SoSeparator()
            base = coin.SoBaseColor()
            base.rgb.setValue(*color)
            sep.addChild(base)
            coords = coin.SoCoordinate3()
            sep.addChild(coords)
            mset = coin.SoMarkerSet()
            mset.markerIndex = marker_index
            sep.addChild(mset)
            return {"sep": sep, "coords": coords, "mset": mset}

        def _rebuild(self):
            obj = getattr(self, "Object", None)
            if obj is None:
                return
            p0 = getattr(obj, "P0", None)
            p1 = getattr(obj, "P1", None)
            if p0 is None or p1 is None or (p1 - p0).Length < 1.0e-9:
                # No usable line: collapse everything.
                self._line.numVertices.setValue(0)
                for m in (getattr(self, "_plus", None), getattr(self, "_minus", None)):
                    if m is not None:
                        m["mset"].numPoints.setValue(0)
                return
            pts = [(p0.x, p0.y, p0.z), (p1.x, p1.y, p1.z)]
            self._coords.point.setValues(0, 2, pts)
            if self._coords.point.getNum() > 2:
                self._coords.point.deleteValues(2)
            self._line.numVertices.setValue(2)

            self._plus["coords"].point.setValues(0, 1, [pts[0]])
            self._plus["mset"].numPoints.setValue(1)
            self._minus["coords"].point.setValues(0, 1, [pts[1]])
            self._minus["mset"].numPoints.setValue(1)

        def updateData(self, obj, prop):
            if prop in ("P0", "P1"):
                self._rebuild()

        def getDisplayModes(self, vobj):
            return ["Line"]

        def getDefaultDisplayMode(self):
            return "Line"

        def setDisplayMode(self, mode):
            return mode

        def getIcon(self):
            return _SPICE_LINE_PORT_ICON

        # -- Sketch assignment via drag & drop (mirrors path monitors) ------ #

        def claimChildren(self):
            obj = getattr(self, "Object", None)
            sketch = getattr(obj, "Sketch", None) if obj is not None else None
            return [sketch] if sketch is not None else []

        def canDragObjects(self):
            return True

        def canDragObject(self, obj):
            return True

        def dragObject(self, vobj, obj):
            port = vobj.Object
            if getattr(port, "Sketch", None) is obj:
                port.Sketch = None
                port.Label = "SPICE Line Port ({})".format(_line_describe(port))
                port.Document.recompute()

        def canDropObjects(self):
            return True

        def canDropObject(self, obj):
            return _is_curve_object(obj)

        def dropObject(self, vobj, obj):
            port = vobj.Object
            if not _is_curve_object(obj):
                return
            port.Sketch = obj
            port.Label = "SPICE Line Port ({})".format(_line_describe(port))
            port.Document.recompute()
            domain_mod.notify_domain_inputs_changed(port.Document)

        def setEdit(self, vobj, mode=0):
            _open_line_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_line_panel(vobj.Object)
            return True

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class SpiceTEMPortViewProvider(tem_mod.TEMSourceViewProvider):
        """Teal launch plane (reused from the TEM source), editing this port."""

        def getIcon(self):
            return _SPICE_TEM_PORT_ICON

        def setEdit(self, vobj, mode=0):
            _open_tem_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_tem_panel(vobj.Object)
            return True

    # ------------------------------------------------------------------ #
    # Task panels
    # ------------------------------------------------------------------ #

    def _netlist_row(QtWidgets, edit, parent):
        """A netlist path line-edit + Browse button in a horizontal layout."""
        container = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit)
        browse = QtWidgets.QPushButton("Browse...")

        def _pick():
            start = edit.text() or os.path.expanduser("~")
            chosen, _ = QtWidgets.QFileDialog.getOpenFileName(
                parent, "Select the SPICE netlist",
                os.path.dirname(start),
                "SPICE netlist (*.net *.cir *.sp *.spice *.txt);;All files (*)",
            )
            if chosen:
                edit.setText(chosen)

        browse.clicked.connect(_pick)
        row.addWidget(browse)
        return container

    class _SpicePanelBase(object):
        """Shared netlist/node widgets + info text for both SPICE port panels."""

        _NOTE = (
            "The two nodes must already exist in the netlist; wavesim splices "
            "its own port companion across them (add no port component). A DC "
            "path to ground node '0' is required. One netlist drives one port; "
            "several ports run independent ngspice instances. The ngspice "
            "library path is set in Wavesim -> Settings."
        )

        def _build_spice_ui(self, layout, QtWidgets):
            obj = self.obj
            self._netlist = QtWidgets.QLineEdit(str(getattr(obj, "Netlist", "")))
            self._node_plus = QtWidgets.QLineEdit(
                str(getattr(obj, "NodePlus", "port1p"))
            )
            self._node_minus = QtWidgets.QLineEdit(
                str(getattr(obj, "NodeMinus", "0"))
            )
            layout.addRow("Netlist:", _netlist_row(QtWidgets, self._netlist,
                                                   self.form))
            layout.addRow("Node + :", self._node_plus)
            layout.addRow("Node - :", self._node_minus)

        def _write_spice(self, obj):
            obj.Netlist = self._netlist.text().strip()
            obj.NodePlus = self._node_plus.text().strip() or "port1p"
            obj.NodeMinus = self._node_minus.text().strip() or "0"

        def getStandardButtons(self):
            QtWidgets = _qt_widgets()
            buttons = (QtWidgets.QDialogButtonBox.Ok
                       | QtWidgets.QDialogButtonBox.Cancel)
            return int(getattr(buttons, "value", buttons))

    class TaskSpiceLinePanel(_SpicePanelBase):
        """Edit a SPICE line port: its curve, netlist and port nodes."""

        def __init__(self, obj, created=False):
            QtWidgets = _qt_widgets()
            self.obj = obj
            self.created = created

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim SPICE Line Port")
            layout = QtWidgets.QFormLayout(form)
            self.form = form

            # Curve picker: the document's curve objects (sketches/edges).
            self._curve = QtWidgets.QComboBox()
            self._curve.addItem("(none)", None)
            current = getattr(obj, "Sketch", None)
            for cand in obj.Document.Objects:
                if _is_curve_object(cand):
                    self._curve.addItem(cand.Label, cand.Name)
                    if cand is current:
                        self._curve.setCurrentIndex(self._curve.count() - 1)
            layout.addRow("Port curve:", self._curve)

            self._build_spice_ui(layout, QtWidgets)

            info = QtWidgets.QLabel(
                "A SPICE line port is a straight lumped port across a gap "
                "(p0 -> p1), driven by the linked circuit. Assign a two-point "
                "sketch as the port line (its start is the '+' end); you can "
                "also drag a sketch onto the port in the tree. " + self._NOTE
            )
            info.setWordWrap(True)
            layout.addRow(info)

        def accept(self):
            doc = self.obj.Document
            doc.openTransaction("Wavesim: Edit SPICE Line Port")
            name = self._curve.currentData()
            self.obj.Sketch = doc.getObject(name) if name else None
            self._write_spice(self.obj)
            self.obj.Label = "SPICE Line Port ({})".format(
                _line_describe(self.obj)
            )
            doc.commitTransaction()
            doc.recompute()
            domain_mod.notify_domain_inputs_changed(doc)
            Gui.Control.closeDialog()
            return True

        def reject(self):
            doc = self.obj.Document
            if self.created:
                doc.openTransaction("Wavesim: Cancel SPICE Line Port")
                doc.removeObject(self.obj.Name)
                doc.commitTransaction()
                doc.recompute()
            Gui.Control.closeDialog()
            return True

    class TaskSpiceTEMPanel(_SpicePanelBase):
        """Edit a SPICE TEM port: face/conductor/fields, netlist and nodes."""

        def __init__(self, obj, created=False):
            QtWidgets = _qt_widgets()
            self.obj = obj
            self.created = created
            self._orig_face = str(getattr(obj, "Face", "z0"))
            self._orig_bounds = getattr(obj, "BoundsSel", None)

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim SPICE TEM Port")
            layout = QtWidgets.QFormLayout(form)
            self.form = form

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
            self._conductor = QtWidgets.QSpinBox()
            self._conductor.setRange(0, 999)
            self._conductor.setSpecialValueText("Dominant (first mode)")
            self._conductor.setValue(int(getattr(obj, "Conductor", 0)))

            layout.addRow("Launch face:", self._face)
            layout.addRow("Inject fields:", self._fields)
            layout.addRow("Energize conductor:", self._conductor)

            # Optional in-plane bounds (mirrors the TEM source panel).
            self._bounds_label = QtWidgets.QLabel(tem_mod._bounds_desc(obj))
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

            self._build_spice_ui(layout, QtWidgets)

            self._compute = QtWidgets.QPushButton("Compute Mode")
            layout.addRow(self._compute)
            self._compute.clicked.connect(self._on_compute)

            info = QtWidgets.QLabel(
                "A SPICE TEM port drives the TEM mode of the PEC cross-section "
                "on the chosen face (set to PML automatically) with the linked "
                "circuit. 'Compute Mode' solves and plots this port's mode(s) now "
                "for viewing only (a Run re-solves and saves them); put "
                "the matched source resistance in the netlist. Optionally select "
                "an edge/face to confine the mode solve to its in-plane bounding "
                "box (Clear restores the whole face). " + self._NOTE
            )
            info.setWordWrap(True)
            layout.addRow(info)

            self._face.currentIndexChanged.connect(self._live_face)

        def _selected_face(self):
            return self._face.currentData() or _FACES[self._face.currentIndex()]

        def _live_face(self, *_):
            self.obj.Face = self._selected_face()
            self.obj.Document.recompute()

        def _pick_bounds(self, *_):
            """Set BoundsSel from the first edge/face in the current selection."""
            QtWidgets = _qt_widgets()
            for s in Gui.Selection.getSelectionEx():
                picks = [n for n in (getattr(s, "SubElementNames", []) or [])
                         if n.startswith("Edge") or n.startswith("Face")]
                if picks:
                    self.obj.BoundsSel = (s.Object, [picks[0]])
                    self._bounds_label.setText(tem_mod._bounds_desc(self.obj))
                    self.obj.Document.recompute()
                    return
            QtWidgets.QMessageBox.information(
                self.form, "Wavesim SPICE TEM Port",
                "Select an edge or face in the 3D view first, then click "
                "'Select bounding edge/face'.",
            )

        def _clear_bounds(self, *_):
            self.obj.BoundsSel = None
            self._bounds_label.setText(tem_mod._bounds_desc(self.obj))
            self.obj.Document.recompute()

        def _commit(self, title):
            doc = self.obj.Document
            new_bounds = getattr(self.obj, "BoundsSel", None)
            self.obj.Face = self._orig_face
            self.obj.BoundsSel = self._orig_bounds
            doc.openTransaction(title)
            face = self._selected_face()
            self.obj.Face = face
            self.obj.BoundsSel = new_bounds
            self.obj.Fields = _FIELDS_TOKEN[self._fields.currentText()]
            self.obj.Conductor = int(self._conductor.value())
            self._write_spice(self.obj)
            self.obj.Label = "SPICE TEM Port ({})".format(
                _tem_describe(self.obj)
            )
            domain_mod.set_face_bc(
                domain_mod.find_domain(active_simulation(doc)), face, _PORT_BC
            )
            doc.commitTransaction()
            doc.recompute()
            domain_mod.notify_domain_inputs_changed(doc)
            self._orig_face = face
            self._orig_bounds = new_bounds

        def _on_compute(self, *_):
            self._commit("Wavesim: Edit SPICE TEM Port")
            tem_mod.run_mode_solve(self.obj.Document, self.obj)

        def accept(self):
            self._commit("Wavesim: Edit SPICE TEM Port")
            Gui.Control.closeDialog()
            return True

        def reject(self):
            doc = self.obj.Document
            if self.created:
                doc.openTransaction("Wavesim: Cancel SPICE TEM Port")
                doc.removeObject(self.obj.Name)
                doc.commitTransaction()
                doc.recompute()
            else:
                self.obj.Face = self._orig_face
                self.obj.BoundsSel = self._orig_bounds
                doc.recompute()
            Gui.Control.closeDialog()
            return True

    def _open_line_panel(obj, created=False):
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskSpiceLinePanel(obj, created=created))

    def _open_tem_panel(obj, created=False):
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskSpiceTEMPanel(obj, created=created))

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #

    class CommandAddSpiceLinePort:
        """Create a SPICE line port and open its editor."""

        def GetResources(self):
            return {
                "Pixmap": _SPICE_LINE_PORT_ICON,
                "MenuText": "Add SPICE Line Port",
                "ToolTip": "Add a lumped port across a gap, coupled in lockstep "
                "to a user ngspice netlist (SPICE co-simulation)",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = active_simulation(doc)
            if sim is None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: create a Simulation before adding a SPICE port.\n"
                )
                return
            doc.openTransaction("Wavesim: Add SPICE Line Port")
            try:
                port = doc.addObject("App::FeaturePython", "SpiceLinePort")
                SpiceLinePortObject(port)
                port.Label = "SPICE Line Port ({})".format(_line_describe(port))
                if port.ViewObject is not None:
                    SpiceLinePortViewProvider(port.ViewObject)
                sources_group(sim).addObject(port)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()
            _open_line_panel(port, created=True)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    class CommandAddSpiceTEMPort:
        """Create a SPICE TEM port on a domain face and open its editor."""

        def GetResources(self):
            return {
                "Pixmap": _SPICE_TEM_PORT_ICON,
                "MenuText": "Add SPICE TEM Port",
                "ToolTip": "Add a TEM waveguide-port plane coupled in lockstep "
                "to a user ngspice netlist (SPICE co-simulation)",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = active_simulation(doc)
            if sim is None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: create a Simulation before adding a SPICE port.\n"
                )
                return
            doc.openTransaction("Wavesim: Add SPICE TEM Port")
            try:
                port = doc.addObject("App::FeaturePython", "SpiceTEMPort")
                SpiceTEMPortObject(port)
                port.Label = "SPICE TEM Port ({})".format(_tem_describe(port))
                if port.ViewObject is not None:
                    SpiceTEMPortViewProvider(port.ViewObject)
                sources_group(sim).addObject(port)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()
            _open_tem_panel(port, created=True)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    Gui.addCommand("Wavesim_AddSpiceLinePort", CommandAddSpiceLinePort())
    Gui.addCommand("Wavesim_AddSpiceTEMPort", CommandAddSpiceTEMPort())
