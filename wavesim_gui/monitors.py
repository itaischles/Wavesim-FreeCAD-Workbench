# -*- coding: utf-8 -*-
"""Diagnostic monitors for the Wavesim workbench (Session 7).

Five kinds of monitor are scripted FreeCAD DocumentObjects grouped under the
simulation's "Monitors" child group, each mapping onto one of the solver's
monitor dataclasses (:mod:`wavesim.monitors`):

* **Probe** (``FieldProbe``) -- records a single field component (or ``|E|`` /
  ``|H|`` magnitude) at one point over time. Drawn as an orange point marker.
* **Snapshot** (``SnapshotMonitor``) -- captures a 2D XY slice of a field
  component every *N* steps. Drawn as a semi-transparent orange plane at the
  slice's z position; several snapshot monitors at different z read as a stack of
  parallel orange planes. (The solver only slices XY planes, so the plane is
  always perpendicular to z; its position along z is editable.)
* **Energy** (``EnergyMonitor``) -- the whole-domain total-energy diagnostic. It
  has no location, so it is a tree-only object with no 3D representation.
* **Voltage** (``VoltageMonitor``) -- records V(t) = ∫E·dl along an *open*
  curve, integrated from the curve's first vertex to its last.
* **Current** (``CurrentMonitor``) -- records I(t) = ∮H·dl around a *closed*
  curve (Ampère's law; the solver closes an open path automatically).

The voltage/current monitors take their integration path from a **sketch**: the
user draws an open/closed curve sketch, adds the monitor, then drags the sketch
onto the monitor in the model tree (mirroring how bodies are assigned to
Materials). The sketch is claimed as a tree child of the monitor and its curve
is discretised into a polyline for the solver at job-build time.

Like every scripted ViewProvider these carry the standard ``Visibility`` property,
so the tree's "eye" toggle shows/hides each monitor's marker/plane. Double-clicking
a monitor in the tree (or Edit) opens a Task-tab panel to edit its settings.

Units: FreeCAD geometry/properties are in millimetres; the solver works in metres.
Point/plane positions are stored in mm; :func:`probe_spec` / :func:`snapshot_spec`
convert to metres and into the solver frame (measured from the domain origin) for
the runner, mirroring :func:`wavesim_gui.source.source_spec`.

Importing this module registers ``Wavesim_AddProbe``, ``Wavesim_AddSnapshot``,
``Wavesim_AddEnergyMonitor``, ``Wavesim_AddVoltageMonitor`` and
``Wavesim_AddCurrentMonitor`` with ``Gui.addCommand`` when a GUI is available.
"""

import os

import FreeCAD

from wavesim_gui.commands import active_simulation


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_RESOURCES_DIR = os.path.join(_WB_DIR, "Resources")
# mesh.png is otherwise unused (the old Grid object was merged into Domain), so
# it serves as the shared monitor icon.
_MONITOR_ICON = os.path.join(_RESOURCES_DIR, "mesh.png")

# Marker property, mirroring the other entities' identity scheme so the object is
# recognisable before its Python proxy is re-attached on reload.
_TYPE_PROP = "WavesimType"
_PROBE_TYPE = "Probe"
_SNAPSHOT_TYPE = "Snapshot"
_ENERGY_TYPE = "EnergyMonitor"
_VOLTAGE_TYPE = "VoltageMonitor"
_CURRENT_TYPE = "CurrentMonitor"

# Name of the child group (created by CommandNewSimulation) holding monitors.
_MONITORS_GROUP = "Monitors"

# Field quantities a monitor may record: the six components plus the two field
# magnitudes the solver's monitors understand.
#
# FreeCAD's property-editor reserves the ASCII pipe '|' as an enumeration submenu
# separator, so a value like "|E|" is split into a nested submenu instead of
# showing as a flat entry. To keep the magnitudes as integral, flat dropdown
# items we display them with a look-alike "divides" bar (U+2223) and map back to
# the solver's ASCII "|E|" / "|H|" tokens in :func:`_solver_component`.
_BAR = "∣"  # looks like '|' but is not the ASCII '|' submenu separator
_E_MAG = _BAR + "E" + _BAR
_H_MAG = _BAR + "H" + _BAR
_COMPONENTS = ["Ex", "Ey", "Ez", "Hx", "Hy", "Hz", _E_MAG, _H_MAG]

# Display label -> solver component token (only the magnitudes need remapping).
_SOLVER_COMPONENT = {_E_MAG: "|E|", _H_MAG: "|H|"}


def _solver_component(label):
    """Map a display component label to the token the solver's monitors expect."""
    return _SOLVER_COMPONENT.get(str(label), str(label))


# Snapshot slice planes: display label -> (normal axis, in-plane axes).
_PLANES = ["XY", "YZ", "XZ"]
_PLANE_NORMAL = {"XY": "z", "YZ": "x", "XZ": "y"}
# Which world-axis the slice offset is measured along, per plane (== the normal).
_PLANE_OFFSET_AXIS = {"XY": "z", "YZ": "x", "XZ": "y"}

# Orange marker / plane colour, distinct from the green source marker.
_MONITOR_COLOR = (1.0, 0.55, 0.0)
# Transparency of the snapshot plane (0 opaque .. 1 invisible).
_SNAPSHOT_TRANSPARENCY = 0.65

_MM_PER_M = 1000.0


# --------------------------------------------------------------------------- #
# Document-object model
# --------------------------------------------------------------------------- #

def _add_type_marker(obj, type_name):
    """Stamp the read-only ``WavesimType`` identity marker on *obj*."""
    if not hasattr(obj, _TYPE_PROP):
        obj.addProperty(
            "App::PropertyString", _TYPE_PROP, "Wavesim",
            "Marks this object as a Wavesim monitor",
        )
        setattr(obj, _TYPE_PROP, type_name)
        obj.setEditorMode(_TYPE_PROP, 1)  # read-only identity marker


class ProbeObject:
    """``Proxy`` for a point field-probe document object.

    Properties:
        ``Component`` -- field quantity recorded ('Ex'..'Hz', '|E|', '|H|').
        ``Position``  -- probe point, world coordinates (mm).
    """

    def __init__(self, obj):
        self.Type = _PROBE_TYPE
        obj.Proxy = self
        _add_type_marker(obj, _PROBE_TYPE)

        if not hasattr(obj, "Component"):
            obj.addProperty(
                "App::PropertyEnumeration", "Component", "Monitor",
                "Field quantity recorded at the probe point",
            )
            obj.Component = _COMPONENTS
            obj.Component = "Ez"
        if not hasattr(obj, "Position"):
            obj.addProperty(
                "App::PropertyVector", "Position", "Monitor",
                "Probe point in world coordinates (mm), snapped to the nearest "
                "grid cell",
            )

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _PROBE_TYPE)

    def execute(self, obj):
        pass

    def dumps(self):
        return {"Type": getattr(self, "Type", _PROBE_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _PROBE_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


class SnapshotObject:
    """``Proxy`` for a snapshot (2D slice) monitor document object.

    Properties:
        ``Component``   -- field quantity recorded ('Ex'..'Hz', '|E|', '|H|').
        ``Plane``       -- slice orientation: 'XY' (perpendicular to z, the
                           default), 'YZ' (perpendicular to x) or 'XZ' (to y).
        ``Offset``      -- position (mm, world) of the slice plane along its
                           normal axis.
        ``EveryNSteps`` -- record a frame every this many time steps.

    Hidden ``Corners`` carries the plane's four world-mm corners for the view
    provider; ``execute`` keeps them in sync with the domain bounds, the plane
    orientation and the offset.
    """

    def __init__(self, obj):
        self.Type = _SNAPSHOT_TYPE
        obj.Proxy = self
        _add_type_marker(obj, _SNAPSHOT_TYPE)

        if not hasattr(obj, "Component"):
            obj.addProperty(
                "App::PropertyEnumeration", "Component", "Monitor",
                "Field quantity captured in the slice",
            )
            obj.Component = _COMPONENTS
            obj.Component = "Ez"
        if not hasattr(obj, "Plane"):
            obj.addProperty(
                "App::PropertyEnumeration", "Plane", "Monitor",
                "Slice orientation: XY (perpendicular to z), YZ (to x) or "
                "XZ (to y)",
            )
            obj.Plane = _PLANES
            obj.Plane = "XY"
        if not hasattr(obj, "Offset"):
            # PropertyDistance (not PropertyLength): a length quantity that allows
            # negative values, so the plane can sit on either side of the origin.
            obj.addProperty(
                "App::PropertyDistance", "Offset", "Monitor",
                "Position (world) of the slice plane along its normal axis",
            )
            obj.Offset = "0 mm"
        if not hasattr(obj, "EveryNSteps"):
            obj.addProperty(
                "App::PropertyInteger", "EveryNSteps", "Monitor",
                "Record a frame every this many time steps",
            )
            obj.EveryNSteps = 20

        # Plane corners (hidden, four world-mm points) for the view provider.
        if not hasattr(obj, "Corners"):
            obj.addProperty("App::PropertyVectorList", "Corners", "Plane", "")
            obj.setEditorMode("Corners", 2)  # hidden

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _SNAPSHOT_TYPE)

    def execute(self, obj):
        """Size/orient the drawn plane to the domain bounds, plane and offset."""
        from wavesim_gui import domain as domain_mod

        sim = active_simulation(obj.Document)
        dom = domain_mod.find_domain(sim) if sim else None
        if dom is not None and (dom.DomainMax - dom.DomainMin).Length > 1.0e-9:
            mn, mx = dom.DomainMin, dom.DomainMax
        else:
            # No sized domain yet: a small default cube centred on the origin so
            # the monitor is still visible/selectable.
            half = 5.0
            mn = FreeCAD.Vector(-half, -half, -half)
            mx = FreeCAD.Vector(half, half, half)

        off = float(obj.Offset.Value)
        plane = str(obj.Plane)
        if plane == "YZ":      # perpendicular to x, at x = off
            pts = [(off, mn.y, mn.z), (off, mx.y, mn.z),
                   (off, mx.y, mx.z), (off, mn.y, mx.z)]
        elif plane == "XZ":    # perpendicular to y, at y = off
            pts = [(mn.x, off, mn.z), (mx.x, off, mn.z),
                   (mx.x, off, mx.z), (mn.x, off, mx.z)]
        else:                  # "XY": perpendicular to z, at z = off
            pts = [(mn.x, mn.y, off), (mx.x, mn.y, off),
                   (mx.x, mx.y, off), (mn.x, mx.y, off)]
        obj.Corners = [FreeCAD.Vector(*p) for p in pts]

    def dumps(self):
        return {"Type": getattr(self, "Type", _SNAPSHOT_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _SNAPSHOT_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


class EnergyObject:
    """``Proxy`` for the whole-domain total-energy monitor.

    The energy monitor has no spatial location, so it carries no geometry and is
    purely a tree object recording that the total-energy diagnostic is active.
    """

    def __init__(self, obj):
        self.Type = _ENERGY_TYPE
        obj.Proxy = self
        _add_type_marker(obj, _ENERGY_TYPE)

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _ENERGY_TYPE)

    def execute(self, obj):
        pass

    def dumps(self):
        return {"Type": getattr(self, "Type", _ENERGY_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _ENERGY_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


class PathMonitorObject:
    """``Proxy`` shared by the Voltage and Current line-integral monitors.

    Both integrate a field along a curve drawn as a sketch (voltage: E along an
    open curve, current: H around a closed one); the only per-kind difference is
    the ``WavesimType`` marker, so one proxy class serves both.

    Properties:
        ``Sketch`` -- link to the sketch (or any edge-carrying object) whose
                      curve is the integration path. Assigned by dragging the
                      sketch onto the monitor in the model tree.
    """

    def __init__(self, obj, type_name):
        self.Type = type_name
        obj.Proxy = self
        _add_type_marker(obj, type_name)

        if not hasattr(obj, "Sketch"):
            obj.addProperty(
                "App::PropertyLink", "Sketch", "Monitor",
                "Sketch whose curve is the integration path (drag a sketch "
                "from the tree onto this monitor to assign it)",
            )

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        # Recover the kind from the identity marker if the pickled state is gone.
        self.Type = getattr(self, "Type", None) or getattr(
            obj, _TYPE_PROP, _VOLTAGE_TYPE
        )

    def execute(self, obj):
        pass

    def dumps(self):
        return {"Type": getattr(self, "Type", _VOLTAGE_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _VOLTAGE_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


# --------------------------------------------------------------------------- #
# Lookup helpers
# --------------------------------------------------------------------------- #

def _is_type(obj, type_name):
    return getattr(obj, _TYPE_PROP, None) == type_name


def is_probe(obj):
    """Return True if *obj* is a Wavesim Probe monitor."""
    return _is_type(obj, _PROBE_TYPE)


def is_snapshot(obj):
    """Return True if *obj* is a Wavesim Snapshot monitor."""
    return _is_type(obj, _SNAPSHOT_TYPE)


def is_energy_monitor(obj):
    """Return True if *obj* is a Wavesim Energy monitor."""
    return _is_type(obj, _ENERGY_TYPE)


def monitors_group(sim):
    """Return the "Monitors" child group of the Simulation container *sim*.

    Falls back to *sim* itself if the expected child group is missing (e.g. an
    older document), so a monitor is never left ungrouped.
    """
    if sim is None:
        return None
    for child in sim.Group:
        if child.Name == _MONITORS_GROUP or child.Label == _MONITORS_GROUP:
            return child
    return sim


def _find(sim, predicate):
    grp = monitors_group(sim)
    if grp is None:
        return []
    return [obj for obj in grp.Group if predicate(obj)]


def find_probes(sim):
    """Return all Probe monitors under the Simulation container *sim*."""
    return _find(sim, is_probe)


def find_snapshots(sim):
    """Return all Snapshot monitors under the Simulation container *sim*."""
    return _find(sim, is_snapshot)


def find_energy_monitors(sim):
    """Return all Energy monitors under the Simulation container *sim*."""
    return _find(sim, is_energy_monitor)


def is_voltage_monitor(obj):
    """Return True if *obj* is a Wavesim Voltage monitor."""
    return _is_type(obj, _VOLTAGE_TYPE)


def is_current_monitor(obj):
    """Return True if *obj* is a Wavesim Current monitor."""
    return _is_type(obj, _CURRENT_TYPE)


def find_voltage_monitors(sim):
    """Return all Voltage monitors under the Simulation container *sim*."""
    return _find(sim, is_voltage_monitor)


def find_current_monitors(sim):
    """Return all Current monitors under the Simulation container *sim*."""
    return _find(sim, is_current_monitor)


def path_monitor_points_mm(sim):
    """World-mm bbox corners of every voltage/current monitor curve under *sim*.

    Feeds the domain auto-sizing (like source points and snapshot offsets), so a
    monitor curve outside the material bounds enlarges the domain to contain it
    rather than having its quadrature points clipped to the grid edge.
    """
    pts = []
    for mon in find_voltage_monitors(sim) + find_current_monitors(sim):
        sketch = getattr(mon, "Sketch", None)
        shape = getattr(sketch, "Shape", None) if sketch is not None else None
        if shape is None or not getattr(shape, "Edges", None):
            continue
        bb = shape.BoundBox
        pts.append((bb.XMin, bb.YMin, bb.ZMin))
        pts.append((bb.XMax, bb.YMax, bb.ZMax))
    return pts


def snapshot_axis_offsets(sim):
    """Return ``[(axis, offset_mm), ...]`` for every snapshot under *sim*.

    *axis* is the slice's normal ('x'/'y'/'z') and *offset_mm* its world-mm
    position along that axis. The domain auto-sizes to include these so a slice
    placed outside the geometry enlarges the domain to contain it.
    """
    out = []
    for snap in find_snapshots(sim):
        plane = str(getattr(snap, "Plane", "XY"))
        axis = _PLANE_NORMAL.get(plane, "z")
        out.append((axis, float(snap.Offset.Value)))
    return out


def refresh_snapshots(doc):
    """Touch every snapshot monitor so its drawn plane re-sizes on recompute.

    Called when the domain changes (its XY extent drives the plane size); safe in
    console mode and a no-op when there are no snapshots.
    """
    sim = active_simulation(doc)
    snaps = find_snapshots(sim)
    if not snaps:
        return
    for snap in snaps:
        snap.touch()
    doc.recompute()


# --------------------------------------------------------------------------- #
# Job serialisation (solver-frame specs)
# --------------------------------------------------------------------------- #

def probe_spec(probe, origin_m):
    """Return the ``job.json`` probe dict for *probe* in the solver frame.

    *origin_m* is the domain min corner in FreeCAD world metres (from the
    voxeliser); the solver frame measures from it, so subtract after mm->m.
    """
    pos = probe.Position
    return {
        "name": str(probe.Label or probe.Name),
        "component": _solver_component(probe.Component),
        "x": pos.x / _MM_PER_M - origin_m[0],
        "y": pos.y / _MM_PER_M - origin_m[1],
        "z": pos.z / _MM_PER_M - origin_m[2],
    }


def snapshot_spec(snap, origin_m):
    """Return the ``job.json`` snapshot dict for *snap* in the solver frame.

    ``normal`` is the slice's normal axis ('x'/'y'/'z'); ``position`` is the
    plane's offset along that axis, in the solver frame (origin subtracted).
    """
    plane = str(snap.Plane)
    normal = _PLANE_NORMAL.get(plane, "z")
    axis = _PLANE_OFFSET_AXIS.get(plane, "z")
    origin_along = {"x": origin_m[0], "y": origin_m[1], "z": origin_m[2]}[axis]
    return {
        "name": str(snap.Label or snap.Name),
        "component": _solver_component(snap.Component),
        "normal": normal,
        "position": float(snap.Offset.Value) / _MM_PER_M - origin_along,
        "every_N_steps": max(1, int(getattr(snap, "EveryNSteps", 20))),
    }


def _path_deflection_mm(sim):
    """Chordal tolerance (mm) for discretising monitor curves.

    A quarter of the smallest domain cell keeps the polyline well below the grid
    resolution (the solver further subdivides each segment to half-cell steps).
    Falls back to 0.5 mm when no domain exists yet.
    """
    from wavesim_gui import domain as domain_mod

    dom = domain_mod.find_domain(sim)
    if dom is not None:
        try:
            return 0.25 * min(
                float(dom.Dx.Value), float(dom.Dy.Value), float(dom.Dz.Value)
            )
        except Exception:
            pass
    return 0.5


def _monitor_path_mm(mon, deflection_mm):
    """Ordered world-mm vertices of *mon*'s linked sketch curve, or ``None``.

    The sketch's edges are sorted into connected wires; the longest wire is
    discretised to the given chordal tolerance. Vertex order (and so the sign of
    the recorded integral) follows the wire's own direction.
    """
    sketch = getattr(mon, "Sketch", None)
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
    if len(wires) > 1:
        FreeCAD.Console.PrintWarning(
            "Wavesim: sketch '{}' on monitor '{}' has {} disconnected curves; "
            "using the longest.\n".format(sketch.Label, mon.Label, len(wires))
        )
    wire = max(wires, key=lambda w: w.Length)
    return wire.discretize(Deflection=max(float(deflection_mm), 1.0e-6))


def _path_spec(mon, origin_m, deflection_mm):
    """Return the ``job.json`` dict for a voltage/current monitor, or ``None``.

    The path is the discretised sketch curve in solver-frame metres. Monitors
    without an assigned (or empty) sketch are skipped with a warning so the run
    still proceeds.
    """
    pts = _monitor_path_mm(mon, deflection_mm)
    if pts is None or len(pts) < 2:
        FreeCAD.Console.PrintWarning(
            "Wavesim: monitor '{}' has no sketch curve assigned (drag a sketch "
            "onto it in the tree); skipping it.\n".format(mon.Label)
        )
        return None
    return {
        "name": str(mon.Label or mon.Name),
        "path": [
            [
                p.x / _MM_PER_M - origin_m[0],
                p.y / _MM_PER_M - origin_m[1],
                p.z / _MM_PER_M - origin_m[2],
            ]
            for p in pts
        ],
    }


def monitors_spec(sim, origin_m):
    """Return the ``job.json`` ``monitors`` dict for the simulation *sim*.

    ``energy`` is on when an explicit Energy monitor exists, or when no monitors
    are defined at all (preserving the always-on energy diagnostic of earlier
    sessions). When the user has defined only probes/snapshots/voltages/currents,
    energy is left off so the job records exactly what was asked for.
    """
    probes = [probe_spec(p, origin_m) for p in find_probes(sim)]
    snapshots = [snapshot_spec(s, origin_m) for s in find_snapshots(sim)]
    deflection = _path_deflection_mm(sim)
    voltages = [
        s for s in (_path_spec(m, origin_m, deflection)
                    for m in find_voltage_monitors(sim)) if s
    ]
    currents = [
        s for s in (_path_spec(m, origin_m, deflection)
                    for m in find_current_monitors(sim)) if s
    ]
    energy_objs = find_energy_monitors(sim)
    energy = bool(energy_objs) or not (probes or snapshots or voltages or currents)
    return {
        "energy": energy, "probes": probes, "snapshots": snapshots,
        "voltages": voltages, "currents": currents,
    }


def _probe_label(obj):
    return "Probe ({})".format(getattr(obj, "Component", "Ez"))


def _snapshot_label(obj):
    plane = str(getattr(obj, "Plane", "XY"))
    axis = _PLANE_OFFSET_AXIS.get(plane, "z")
    off_mm = float(obj.Offset.Value) if hasattr(obj, "Offset") else 0.0
    return "Snapshot ({} {} @ {}={:g} mm, every {})".format(
        getattr(obj, "Component", "Ez"), plane, axis, off_mm,
        int(getattr(obj, "EveryNSteps", 20)),
    )


def _path_monitor_label(obj):
    kind = "Voltage" if is_voltage_monitor(obj) else "Current"
    sketch = getattr(obj, "Sketch", None)
    if sketch is not None:
        return "{} Monitor ({})".format(kind, sketch.Label)
    return "{} Monitor (no curve)".format(kind)


# --------------------------------------------------------------------------- #
# GUI: view providers, task panels, commands
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

    class ProbeViewProvider:
        """Coin view provider drawing a probe as an orange point marker."""

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            from pivy import coin

            self.Object = vobj.Object
            root = coin.SoSeparator()

            color = coin.SoBaseColor()
            color.rgb.setValue(*_MONITOR_COLOR)
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
            return _MONITOR_ICON

        def setEdit(self, vobj, mode=0):
            _open_probe_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_probe_panel(vobj.Object)
            return True

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class SnapshotViewProvider:
        """Coin view provider drawing a snapshot as a translucent orange plane."""

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            from pivy import coin

            self.Object = vobj.Object
            root = coin.SoSeparator()

            # Two-sided lighting so the translucent plane is visible from behind.
            hints = coin.SoShapeHints()
            hints.vertexOrdering = coin.SoShapeHints.COUNTERCLOCKWISE
            hints.shapeType = coin.SoShapeHints.UNKNOWN_SHAPE_TYPE
            root.addChild(hints)

            material = coin.SoMaterial()
            material.diffuseColor.setValue(*_MONITOR_COLOR)
            material.transparency.setValue(_SNAPSHOT_TRANSPARENCY)
            root.addChild(material)

            self._coords = coin.SoCoordinate3()
            root.addChild(self._coords)

            self._face = coin.SoFaceSet()
            root.addChild(self._face)

            # An opaque orange border to make the plane edges read clearly.
            border = coin.SoSeparator()
            bcolor = coin.SoBaseColor()
            bcolor.rgb.setValue(*_MONITOR_COLOR)
            border.addChild(bcolor)
            bstyle = coin.SoDrawStyle()
            bstyle.lineWidth = 2
            border.addChild(bstyle)
            self._border_coords = coin.SoCoordinate3()
            border.addChild(self._border_coords)
            self._border_lines = coin.SoIndexedLineSet()
            border.addChild(self._border_lines)
            root.addChild(border)

            self._root = root
            vobj.addDisplayMode(root, "Plane")
            self._rebuild()

        def _clear(self):
            if self._coords.point.getNum():
                self._coords.point.deleteValues(0)
            self._face.numVertices.setValue(0)
            if self._border_coords.point.getNum():
                self._border_coords.point.deleteValues(0)
            if self._border_lines.coordIndex.getNum():
                self._border_lines.coordIndex.deleteValues(0)

        def _rebuild(self):
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

        def updateData(self, obj, prop):
            if prop == "Corners":
                self._rebuild()

        def getDisplayModes(self, vobj):
            return ["Plane"]

        def getDefaultDisplayMode(self):
            return "Plane"

        def setDisplayMode(self, mode):
            return mode

        def getIcon(self):
            return _MONITOR_ICON

        def setEdit(self, vobj, mode=0):
            _open_snapshot_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_snapshot_panel(vobj.Object)
            return True

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class EnergyViewProvider:
        """Tree-only view provider for the whole-domain energy monitor.

        No 3D geometry (the energy monitor has no location); double-click does
        nothing editable, so the object is simply listed under Monitors.
        """

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            self.ViewObject = vobj
            self.Object = vobj.Object

        def getIcon(self):
            return _MONITOR_ICON

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    def _is_curve_object(obj):
        """True if *obj* carries a curve Shape (edges, no solids) -- a sketch,
        Draft wire, etc. -- and so can serve as a monitor integration path."""
        shape = getattr(obj, "Shape", None)
        if shape is None or getattr(shape, "Solids", None):
            return False
        return bool(getattr(shape, "Edges", None))

    def _after_path_changed(doc):
        """Recompute and re-size the domain after a monitor's curve changes."""
        from wavesim_gui import domain as domain_mod
        domain_mod.notify_domain_inputs_changed(doc)

    class PathMonitorViewProvider:
        """Tree view provider for voltage/current monitors.

        No 3D geometry of its own -- the linked sketch *is* the curve, and it is
        claimed as a tree child of the monitor. Assignment mirrors Materials:
        drag a sketch onto the monitor to attach it, drag it off to detach.
        """

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            self.ViewObject = vobj
            self.Object = vobj.Object

        def getIcon(self):
            return _MONITOR_ICON

        def claimChildren(self):
            obj = getattr(self, "Object", None)
            sketch = getattr(obj, "Sketch", None) if obj is not None else None
            return [sketch] if sketch is not None else []

        # -- Drag & drop: attach the path sketch by dropping it here --------- #

        def canDragObjects(self):
            return True

        def canDragObject(self, obj):
            return True

        def dragObject(self, vobj, obj):
            """Detach the sketch when it is dragged off the monitor."""
            mon = vobj.Object
            if getattr(mon, "Sketch", None) is obj:
                mon.Sketch = None
                mon.Label = _path_monitor_label(mon)
                _after_path_changed(mon.Document)

        def canDropObjects(self):
            return True

        def canDropObject(self, obj):
            return _is_curve_object(obj)

        def dropObject(self, vobj, obj):
            """Attach the dropped sketch as this monitor's integration path."""
            mon = vobj.Object
            if not _is_curve_object(obj):
                return
            mon.Sketch = obj
            mon.Label = _path_monitor_label(mon)
            _after_path_changed(mon.Document)

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    # ------------------------------------------------------------------ #
    # Task panels
    # ------------------------------------------------------------------ #

    def _qt_widgets():
        try:
            from PySide import QtWidgets
        except ImportError:
            from PySide import QtGui as QtWidgets
        return QtWidgets

    def _ok_cancel_buttons():
        QtWidgets = _qt_widgets()
        buttons = QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        return int(getattr(buttons, "value", buttons))

    class TaskProbePanel:
        """Task-tab panel to edit a probe's component and point."""

        def __init__(self, obj, created=False):
            QtWidgets = _qt_widgets()
            self.obj = obj
            self.created = created
            # Original position, restored on Cancel and used so Accept records the
            # full change for undo (the live edits below modify the object directly).
            self._orig_position = FreeCAD.Vector(obj.Position)

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Probe")
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

            layout.addRow("Field quantity:", self._component)
            layout.addRow("Position X:", self._x)
            layout.addRow("Position Y:", self._y)
            layout.addRow("Position Z:", self._z)

            info = QtWidgets.QLabel(
                "The probe records the chosen field quantity at this point "
                "(snapped to the nearest grid cell) at every timestep."
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
            """Move the probe marker immediately as the spin boxes change."""
            self.obj.Position = FreeCAD.Vector(
                self._x.value(), self._y.value(), self._z.value()
            )

        def accept(self):
            doc = self.obj.Document
            # Restore the original position first so the transaction captures the
            # full change (live edits already moved the object outside it).
            self.obj.Position = self._orig_position
            doc.openTransaction("Wavesim: Edit Probe")
            self.obj.Component = self._component.currentText()
            self.obj.Position = FreeCAD.Vector(
                self._x.value(), self._y.value(), self._z.value()
            )
            self.obj.Label = _probe_label(self.obj)
            doc.commitTransaction()
            doc.recompute()
            Gui.Control.closeDialog()
            return True

        def reject(self):
            doc = self.obj.Document
            if self.created:
                doc.openTransaction("Wavesim: Cancel Probe")
                doc.removeObject(self.obj.Name)
                doc.commitTransaction()
                doc.recompute()
            else:
                # Undo any live position edits.
                self.obj.Position = self._orig_position
            Gui.Control.closeDialog()
            return True

        def getStandardButtons(self):
            return _ok_cancel_buttons()

    class TaskSnapshotPanel:
        """Task-tab panel: snapshot component, plane orientation, offset, interval."""

        def __init__(self, obj, created=False):
            QtWidgets = _qt_widgets()
            self.obj = obj
            self.created = created
            # Original plane/offset, restored on Cancel and used so Accept records
            # the full change for undo (live edits below modify the object directly).
            self._orig_offset = float(obj.Offset.Value)
            self._orig_plane = str(getattr(obj, "Plane", "XY"))

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Snapshot")
            layout = QtWidgets.QFormLayout(form)

            self._component = QtWidgets.QComboBox()
            self._component.addItems(_COMPONENTS)
            self._component.setCurrentText(str(getattr(obj, "Component", "Ez")))

            self._plane = QtWidgets.QComboBox()
            self._plane.addItems(_PLANES)
            self._plane.setCurrentText(str(getattr(obj, "Plane", "XY")))

            self._offset = QtWidgets.QDoubleSpinBox()
            self._offset.setRange(-1.0e6, 1.0e6)
            self._offset.setDecimals(4)
            self._offset.setSuffix(" mm")
            self._offset.setSingleStep(0.5)
            self._offset.setValue(float(obj.Offset.Value))

            self._every = QtWidgets.QSpinBox()
            self._every.setRange(1, 1_000_000)
            self._every.setSuffix(" time steps")
            self._every.setValue(int(getattr(obj, "EveryNSteps", 20)))

            layout.addRow("Field quantity:", self._component)
            layout.addRow("Slice plane:", self._plane)
            self._offset_label = QtWidgets.QLabel()
            layout.addRow(self._offset_label, self._offset)
            layout.addRow("Record every:", self._every)

            info = QtWidgets.QLabel(
                "The snapshot captures a 2D slice of the chosen field quantity on "
                "the selected plane, offset along its normal axis, every N time "
                "steps. The recorded frames feed the snapshot animation in the "
                "results view."
            )
            info.setWordWrap(True)
            layout.addRow(info)

            self._plane.currentTextChanged.connect(self._update_offset_label)
            self._update_offset_label(self._plane.currentText())

            # Live-update the slice plane in the 3D view as the plane orientation
            # or offset change, rather than only on OK.
            self._offset.valueChanged.connect(self._live_plane)
            self._plane.currentTextChanged.connect(self._live_plane)

            self.form = form

        def _update_offset_label(self, plane):
            axis = _PLANE_OFFSET_AXIS.get(str(plane), "z")
            self._offset_label.setText("Offset ({}):".format(axis))

        def _live_plane(self, *_):
            """Re-orient/move the drawn slice plane as the controls change."""
            self.obj.Plane = self._plane.currentText()
            self.obj.Offset = "{} mm".format(self._offset.value())
            # The drawn corners are rebuilt in execute(), so recompute to refresh.
            self.obj.Document.recompute()

        def accept(self):
            from wavesim_gui import domain as domain_mod

            doc = self.obj.Document
            # Restore originals first so the transaction captures the full change
            # (live edits already modified the object outside it).
            self.obj.Offset = "{} mm".format(self._orig_offset)
            self.obj.Plane = self._orig_plane
            doc.openTransaction("Wavesim: Edit Snapshot")
            self.obj.Component = self._component.currentText()
            self.obj.Plane = self._plane.currentText()
            self.obj.Offset = "{} mm".format(self._offset.value())
            self.obj.EveryNSteps = int(self._every.value())
            self.obj.Label = _snapshot_label(self.obj)
            doc.commitTransaction()
            doc.recompute()
            # Enlarge the domain to include the slice if it now sits outside it.
            domain_mod.notify_domain_inputs_changed(doc)
            Gui.Control.closeDialog()
            return True

        def reject(self):
            doc = self.obj.Document
            if self.created:
                doc.openTransaction("Wavesim: Cancel Snapshot")
                doc.removeObject(self.obj.Name)
                doc.commitTransaction()
                doc.recompute()
            else:
                # Undo any live plane/offset edits.
                self.obj.Offset = "{} mm".format(self._orig_offset)
                self.obj.Plane = self._orig_plane
                doc.recompute()
            Gui.Control.closeDialog()
            return True

        def getStandardButtons(self):
            return _ok_cancel_buttons()

    def _open_probe_panel(obj, created=False):
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskProbePanel(obj, created=created))

    def _open_snapshot_panel(obj, created=False):
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskSnapshotPanel(obj, created=created))

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #

    def _require_simulation():
        """Return the active simulation, warning and returning None if absent."""
        sim = active_simulation(FreeCAD.ActiveDocument)
        if sim is None:
            FreeCAD.Console.PrintWarning(
                "Wavesim: create a Simulation before adding a monitor.\n"
            )
        return sim

    def _default_point_mm(sim):
        """A sensible default monitor position: the domain/geometry centre."""
        from wavesim_gui.source import default_position_mm
        return default_position_mm(sim)

    class CommandAddProbe:
        """Create a field Probe at the domain centre and open its editor."""

        def GetResources(self):
            return {
                "Pixmap": _MONITOR_ICON,
                "MenuText": "Add Probe",
                "ToolTip": "Add a point field probe that records a field value "
                "over time",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = _require_simulation()
            if sim is None:
                return
            doc.openTransaction("Wavesim: Add Probe")
            try:
                probe = doc.addObject("App::FeaturePython", "Probe")
                ProbeObject(probe)
                probe.Position = _default_point_mm(sim)
                probe.Label = _probe_label(probe)
                if probe.ViewObject is not None:
                    ProbeViewProvider(probe.ViewObject)
                monitors_group(sim).addObject(probe)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()
            _open_probe_panel(probe, created=True)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    class CommandAddSnapshot:
        """Create a snapshot plane monitor and open its editor."""

        def GetResources(self):
            return {
                "Pixmap": _MONITOR_ICON,
                "MenuText": "Add Snapshot",
                "ToolTip": "Add a snapshot monitor capturing a 2D field slice "
                "every N steps",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = _require_simulation()
            if sim is None:
                return
            centre = _default_point_mm(sim)
            doc.openTransaction("Wavesim: Add Snapshot")
            try:
                snap = doc.addObject("App::FeaturePython", "Snapshot")
                SnapshotObject(snap)
                # Default XY plane through the domain centre (offset along z).
                snap.Offset = "{} mm".format(centre.z)
                snap.Label = _snapshot_label(snap)
                if snap.ViewObject is not None:
                    SnapshotViewProvider(snap.ViewObject)
                monitors_group(sim).addObject(snap)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()
            _open_snapshot_panel(snap, created=True)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    class CommandAddEnergyMonitor:
        """Add the whole-domain total-energy monitor (a tree-only object)."""

        def GetResources(self):
            return {
                "Pixmap": _MONITOR_ICON,
                "MenuText": "Add Energy Monitor",
                "ToolTip": "Record the total electromagnetic energy in the "
                "domain over time",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = _require_simulation()
            if sim is None:
                return
            if find_energy_monitors(sim):
                FreeCAD.Console.PrintWarning(
                    "Wavesim: an energy monitor already exists.\n"
                )
                return
            doc.openTransaction("Wavesim: Add Energy Monitor")
            try:
                energy = doc.addObject("App::FeaturePython", "EnergyMonitor")
                EnergyObject(energy)
                energy.Label = "Energy Monitor"
                if energy.ViewObject is not None:
                    EnergyViewProvider(energy.ViewObject)
                monitors_group(sim).addObject(energy)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    class _CommandAddPathMonitor:
        """Shared Activated/IsActive for the voltage and current commands."""

        _TYPE = _VOLTAGE_TYPE       # overridden per subclass
        _TRANSACTION = "Wavesim: Add Monitor"
        _HINT = ""

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = _require_simulation()
            if sim is None:
                return
            doc.openTransaction(self._TRANSACTION)
            try:
                mon = doc.addObject("App::FeaturePython", self._TYPE)
                PathMonitorObject(mon, self._TYPE)
                mon.Label = _path_monitor_label(mon)
                if mon.ViewObject is not None:
                    PathMonitorViewProvider(mon.ViewObject)
                monitors_group(sim).addObject(mon)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()
            FreeCAD.Console.PrintMessage(self._HINT)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    class CommandAddVoltageMonitor(_CommandAddPathMonitor):
        """Add a voltage monitor; the user then drags its path sketch onto it."""

        _TYPE = _VOLTAGE_TYPE
        _TRANSACTION = "Wavesim: Add Voltage Monitor"
        _HINT = (
            "Wavesim: drag an open-curve sketch from the model tree onto the "
            "Voltage Monitor to set its integration path (V = ∫E·dl from the "
            "curve's start to its end).\n"
        )

        def GetResources(self):
            return {
                "Pixmap": _MONITOR_ICON,
                "MenuText": "Add Voltage Monitor",
                "ToolTip": "Record the voltage V(t) = ∫E·dl along an open "
                "sketch curve (drag the sketch onto the monitor in the tree)",
            }

    class CommandAddCurrentMonitor(_CommandAddPathMonitor):
        """Add a current monitor; the user then drags its loop sketch onto it."""

        _TYPE = _CURRENT_TYPE
        _TRANSACTION = "Wavesim: Add Current Monitor"
        _HINT = (
            "Wavesim: drag a closed-curve sketch from the model tree onto the "
            "Current Monitor to set its integration loop (I = ∮H·dl, positive "
            "by the right-hand rule along the curve direction).\n"
        )

        def GetResources(self):
            return {
                "Pixmap": _MONITOR_ICON,
                "MenuText": "Add Current Monitor",
                "ToolTip": "Record the current I(t) = ∮H·dl around a closed "
                "sketch curve (drag the sketch onto the monitor in the tree)",
            }

    Gui.addCommand("Wavesim_AddProbe", CommandAddProbe())
    Gui.addCommand("Wavesim_AddSnapshot", CommandAddSnapshot())
    Gui.addCommand("Wavesim_AddEnergyMonitor", CommandAddEnergyMonitor())
    Gui.addCommand("Wavesim_AddVoltageMonitor", CommandAddVoltageMonitor())
    Gui.addCommand("Wavesim_AddCurrentMonitor", CommandAddCurrentMonitor())
