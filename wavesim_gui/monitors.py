# -*- coding: utf-8 -*-
"""Diagnostic monitors for the Wavesim workbench (Session 7).

Seven kinds of monitor are scripted FreeCAD DocumentObjects grouped under the
simulation's "Monitors" child group, each mapping onto one of the solver's
monitor dataclasses (:mod:`wavesim.monitors`):

* **Probe** (``FieldProbe``) -- records a single field component (or ``|E|`` /
  ``|H|`` magnitude) at one point over time. Drawn as a teal point marker.
* **Snapshot** (``SnapshotMonitor``) -- captures a 2D slice of a whole field
  (``E`` or ``H``) every *N* steps. The user picks the field, not a component:
  the runner puts one solver monitor on each of its three components and the
  results window offers Ex/Ey/Ez/|E| (magnitude derived from the components), so
  one monitor covers what used to take three. Drawn as a semi-transparent teal
  plane on the chosen slice plane, offset along that plane's normal axis.
* **Energy** (``EnergyMonitor``) -- the total-energy diagnostic. It has no
  location, so it is a tree-only object with no 3D representation; its panel
  instead picks the volume(s) summed: the PML-free interior (the default), the
  whole grid including the PML, or both as two separate series.
* **Dissipation** (``DissipationMonitor``) -- the ohmic-loss diagnostic,
  P(t) = Σ σ|E|²·dV over the chosen volume. Location-free and region-selected
  exactly like Energy, and its companion: the dissipated power is what makes a
  lossy run's energy legitimately decay, so ``U(0) − U(t) = ∫P dt`` is the check
  that tells absorbed power from a solver leak. Only a material with a nonzero
  ``Sigma`` dissipates; on a lossless run the series is flat zero.
* **Voltage** (``VoltageMonitor``) -- records V(t) = ∫E·dl along an *open*
  curve, integrated from the curve's first vertex to its last.
* **Current** (``CurrentMonitor``) -- records I(t) = ∮H·dl around a *closed*
  curve (Ampère's law; the solver closes an open path automatically).
* **Field along curve** (electrostatic only) -- samples phi, E or D along a
  curve between two of its vertices; the result plots it against the distance
  travelled along the curve. The curve is sampled here (points, exact edge
  tangents, arc length) and the runner interpolates the solution at the points.

The voltage/current and field-along-curve monitors take their path from a
**sketch**: the
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
``Wavesim_AddEnergyMonitor``, ``Wavesim_AddDissipationMonitor``,
``Wavesim_AddVoltageMonitor``, ``Wavesim_AddCurrentMonitor`` and
``Wavesim_AddFieldLineMonitor`` with
``Gui.addCommand`` when a GUI is available.
"""

import os

import FreeCAD

from wavesim_gui import labels as labels_mod
from wavesim_gui import units
from wavesim_gui.commands import active_simulation


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_RESOURCES_DIR = os.path.join(_WB_DIR, "Resources")
# The 24x24 SVG icon set (grouped by colour: blue setup, amber sources,
# teal monitors). The retired PNGs are still in Resources/ alongside it.
_ICONS_DIR = os.path.join(_RESOURCES_DIR, "icons")
# Generic monitor icon. Every monitor kind below names its own; this one is the
# fallback a path monitor's view provider returns when the object is neither a
# voltage nor a current monitor (see ``PathMonitorViewProvider.getIcon``).
_MONITOR_ICON = os.path.join(_ICONS_DIR, "monitor.svg")
_SNAPSHOT_MONITOR_ICON = os.path.join(_ICONS_DIR, "snapshot.svg")
_VOLTAGE_MONITOR_ICON = os.path.join(_ICONS_DIR, "voltage.svg")
_CURRENT_MONITOR_ICON = os.path.join(_ICONS_DIR, "current.svg")
_FIELD_PROBE_ICON = os.path.join(_ICONS_DIR, "probe.svg")
_ENERGY_MONITOR_ICON = os.path.join(_ICONS_DIR, "energy.svg")
_DISSIPATION_MONITOR_ICON = os.path.join(_ICONS_DIR, "dissipation.svg")
_FIELD_LINE_ICON = os.path.join(_ICONS_DIR, "field_line.svg")

# Marker property, mirroring the other entities' identity scheme so the object is
# recognisable before its Python proxy is re-attached on reload.
_TYPE_PROP = "WavesimType"
_PROBE_TYPE = "Probe"
_SNAPSHOT_TYPE = "Snapshot"
_ENERGY_TYPE = "EnergyMonitor"
_DISSIPATION_TYPE = "DissipationMonitor"
_VOLTAGE_TYPE = "VoltageMonitor"
_CURRENT_TYPE = "CurrentMonitor"
_FIELD_LINE_TYPE = "FieldLineMonitor"

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


# A Snapshot records a whole *field* rather than one component: the runner puts a
# solver monitor on each of its three components, and the magnitude is derived
# from them at plot time (exactly the solver's own sqrt(Fx²+Fy²+Fz²) over the same
# slices). So the user picks E or H once and gets Ex/Ey/Ez/|E| in one monitor.
# 'S' is the Poynting vector S = E x H: the runner records it with one solver
# ``PoyntingMonitor`` and splits it into Sx/Sy/Sz, so |S| (power-flux density) and
# the in-plane power-flow quiver come out of the same plot machinery as a field.
_FIELDS = ["E", "H", "S"]

# What an *electrostatic* run can put on a slice. 'phi' is the potential itself,
# which is the only quantity in the picture that is exact -- it is what was
# solved for, on the nodes; E and D are differences of it. There is no H and no
# Poynting vector to record: nothing is propagating.
_ES_FIELDS = ["phi", "E", "D"]

# The stored enumeration carries every value either mode can produce, so
# switching the solver mode never invalidates a saved monitor. The panel offers
# the subset that the current mode can actually record.
_ALL_FIELDS = _FIELDS + ["phi", "D"]


def fields_for_mode(sim):
    """The Field values a snapshot may take under *sim*'s solver mode."""
    from wavesim_gui.commands import is_electrostatic

    return list(_ES_FIELDS) if is_electrostatic(sim) else list(_FIELDS)


def _field_components(field):
    """The component tokens of *field*.

    Three for a vector field (E/H/S, and electrostatic D); one for the scalar
    potential, which has no components to choose between.
    """
    text = str(field)
    if text.lower().startswith("phi"):
        return ["phi"]
    u = text.upper()
    if u.startswith("D"):
        return ["D" + axis for axis in ("x", "y", "z")]
    f = "S" if u.startswith("S") else ("H" if u.startswith("H") else "E")
    return [f + axis for axis in ("x", "y", "z")]


def _field_of(component):
    """The field ('E'/'H'/'S') a legacy component label belongs to ('Ez', '∣H∣')."""
    text = str(component).replace(_BAR, "").replace("|", "")
    head = text[:1].upper()
    return head if head in ("H", "S") else "E"


# Snapshot slice planes: display label -> (normal axis, in-plane axes).
_PLANES = ["XY", "YZ", "XZ"]
_PLANE_NORMAL = {"XY": "z", "YZ": "x", "XZ": "y"}
# Which world-axis the slice offset is measured along, per plane (== the normal).
_PLANE_OFFSET_AXIS = {"XY": "z", "YZ": "x", "XZ": "y"}

# Teal marker / plane colour, matching the monitor group in Resources/icons
# (#2f9a86) and set against the amber a source or port draws in.
_MONITOR_COLOR = (0.184, 0.604, 0.525)
# Transparency of the snapshot plane (0 opaque .. 1 invisible).
_SNAPSHOT_TRANSPARENCY = 0.65

_MM_PER_M = 1000.0

# Field-line sampling. The job samples every quarter of the smallest cell (well
# below the grid, so the plot shows what the interpolation gives rather than
# where the samples happened to land), capped so a long curve on a fine grid
# cannot write a job of millions of points. The tree preview needs far fewer.
_FIELD_LINE_MAX_SAMPLES = 20000
_PREVIEW_SAMPLES = 400


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


def _ensure_snapshot_field(obj):
    """Add the ``Field`` property, migrating a pre-merge ``Component`` choice.

    Snapshots used to record a single component, so one monitor per component was
    needed to see a whole field. They now record E or H whole; an old monitor's
    ``Component`` picks the field it belonged to and the property is dropped.
    """
    if not hasattr(obj, "Field"):
        obj.addProperty(
            "App::PropertyEnumeration", "Field", "Monitor",
            "Quantity captured in the slice. Full-wave: E, H, or S (the "
            "Poynting vector S = E x H, i.e. power-flux density). "
            "Electrostatic: phi (the potential), E or D. Every component is "
            "recorded (and the magnitude derived), selectable when plotting",
        )
        obj.Field = _ALL_FIELDS
        obj.Field = _field_of(getattr(obj, "Component", "E"))
    else:
        # A monitor saved before electrostatics existed carries the three-value
        # enumeration; widen it in place, keeping whatever it was set to.
        try:
            current = str(obj.Field)
            if set(obj.getEnumerationsOfProperty("Field")) != set(_ALL_FIELDS):
                obj.Field = _ALL_FIELDS
                obj.Field = current if current in _ALL_FIELDS else _ALL_FIELDS[0]
        except Exception:
            pass
    if hasattr(obj, "Component"):
        try:
            obj.removeProperty("Component")
        except Exception:
            obj.setEditorMode("Component", 2)  # can't remove it: hide it


class SnapshotObject:
    """``Proxy`` for a snapshot (2D slice) monitor document object.

    Properties:
        ``Field``       -- field captured, 'E' or 'H'. Every component of it is
                           recorded (the magnitude is derived at plot time), so
                           one monitor covers Ex/Ey/Ez/|E|.
        ``Plane``       -- slice orientation: 'XY' (perpendicular to z, the
                           default), 'YZ' (perpendicular to x) or 'XZ' (to y).
        ``Offset``      -- position (mm, world) of the slice plane along its
                           normal axis.
        ``RecordInterval`` -- record a frame every this much simulated time
                           (seconds, SI). The primary control; the step count is
                           derived from it and the grid's CFL time step.
        ``EveryNSteps`` -- record a frame every this many time steps (derived
                           from ``RecordInterval``; the fallback when no grid /
                           interval is set, and the value emitted to the solver).

    Hidden ``Corners`` carries the plane's four world-mm corners for the view
    provider; ``execute`` keeps them in sync with the domain bounds, the plane
    orientation and the offset.
    """

    def __init__(self, obj):
        self.Type = _SNAPSHOT_TYPE
        obj.Proxy = self
        _add_type_marker(obj, _SNAPSHOT_TYPE)

        _ensure_snapshot_field(obj)
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
                "Record a frame every this many time steps (derived from "
                "RecordInterval)",
            )
            obj.EveryNSteps = 20
        if not hasattr(obj, "RecordInterval"):
            # Seconds (SI). The recording cadence in real time; the step count is
            # derived from it and the grid's CFL step at edit/job-build time so it
            # stays fixed if the grid (and therefore dt) changes. 0 = unset (fall
            # back to EveryNSteps).
            obj.addProperty(
                "App::PropertyFloat", "RecordInterval", "Monitor",
                "Record a frame every this much simulated time (seconds)",
            )
            obj.RecordInterval = 0.0

        # Plane corners (hidden, four world-mm points) for the view provider.
        if not hasattr(obj, "Corners"):
            obj.addProperty("App::PropertyVectorList", "Corners", "Plane", "")
            obj.setEditorMode("Corners", 2)  # hidden

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _SNAPSHOT_TYPE)
        # Documents saved before snapshots recorded whole fields carry a
        # per-component ``Component`` instead of ``Field``.
        _ensure_snapshot_field(obj)

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


def _ensure_region_props(obj, quantity, legacy=False):
    """Add the two region-selection properties, defaulting per *legacy*.

    Shared by the Energy and Dissipation monitors, which map onto the two solver
    monitors carrying the same ``region``/``d_pml``/``faces`` trio (and filled
    from the run's CPML by the same hook). *quantity* only names the summed
    quantity in the property tooltips.

    A new monitor defaults to the interior alone: the PML absorbs whatever
    reaches it, so a whole-grid sum conflates what is still stored in the model
    with what is already on its way out, while the interior series decays to ~0
    once the fields have left. *legacy* is for a monitor restored from a document
    saved before the regions existed: it recorded the whole grid, so it migrates
    to that and keeps recording what it always did.
    """
    if not hasattr(obj, "RecordInterior"):
        obj.addProperty(
            "App::PropertyBool", "RecordInterior", "Monitor",
            "Record the {} of the physical domain only, excluding the PML "
            "absorbing layers".format(quantity),
        )
        obj.RecordInterior = not legacy
    if not hasattr(obj, "RecordFullDomain"):
        obj.addProperty(
            "App::PropertyBool", "RecordFullDomain", "Monitor",
            "Record the {} of the whole grid, including the PML absorbing "
            "layers".format(quantity),
        )
        obj.RecordFullDomain = legacy


def _ensure_energy_regions(obj, legacy=False):
    """Region properties for the Energy monitor (see :func:`_ensure_region_props`)."""
    _ensure_region_props(obj, "energy", legacy=legacy)


def _ensure_dissipation_regions(obj):
    """Region properties for the Dissipation monitor.

    No *legacy* case: this monitor postdates the region split, so every
    document that has one has one with both properties already.
    """
    _ensure_region_props(obj, "ohmic power dissipated in")


class EnergyObject:
    """``Proxy`` for the total-energy monitor.

    The energy monitor has no spatial location, so it carries no geometry and is
    purely a tree object recording that the total-energy diagnostic is active.

    Properties:
        ``RecordInterior``   -- sum only the physical domain, dropping the PML
                                cells (the solver's ``region='interior'``).
        ``RecordFullDomain`` -- sum the whole grid, PML included (``'full'``).

    The two are independent: tick either, or both to record both series. The
    panel keeps at least one ticked, since a monitor recording neither records
    nothing at all.
    """

    def __init__(self, obj):
        self.Type = _ENERGY_TYPE
        obj.Proxy = self
        _add_type_marker(obj, _ENERGY_TYPE)
        _ensure_energy_regions(obj)

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        # Documents saved before the region choice existed summed the whole grid.
        _ensure_energy_regions(obj, legacy=True)
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


class DissipationObject:
    """``Proxy`` for the ohmic-dissipation monitor.

    Like the energy monitor it has no spatial location -- it is a volume sum, so
    it is a tree-only object whose panel picks the volume(s), not a position.

    Properties:
        ``RecordInterior``   -- sum only the physical domain, dropping the PML
                                cells (the solver's ``region='interior'``).
        ``RecordFullDomain`` -- sum the whole grid, PML included (``'full'``).

    ``'interior'`` is the default here for a sharper reason than on the energy
    monitor: the PML **dissipates by design**, and its absorbed power swamps the
    material's own loss, which is the quantity this monitor exists to report.
    """

    def __init__(self, obj):
        self.Type = _DISSIPATION_TYPE
        obj.Proxy = self
        _add_type_marker(obj, _DISSIPATION_TYPE)
        _ensure_dissipation_regions(obj)

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        _ensure_dissipation_regions(obj)
        self.Type = getattr(self, "Type", _DISSIPATION_TYPE)

    def execute(self, obj):
        pass

    def dumps(self):
        return {"Type": getattr(self, "Type", _DISSIPATION_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _DISSIPATION_TYPE)
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


# End-vertex sentinel for a field-line monitor: "the end of the curve", which is
# the last vertex of an open curve and the start again (one full lap) of a closed
# one. Not just Python's ``-1``: on a closed curve that would be the vertex one
# edge short of the start, and the default would silently drop the last edge.
_CURVE_END = -1


class FieldLineObject:
    """``Proxy`` for the electrostatic field-along-a-curve monitor.

    Samples one quantity (phi, E or D) along a sketch curve, between two of its
    vertices, and plots it against the distance travelled along the curve. The
    curve comes from a sketch dragged onto the monitor, exactly as for the
    voltage/current monitors.

    Properties:
        ``Sketch``      -- link to the curve (drag a sketch onto the monitor).
        ``Field``       -- 'phi', 'E' or 'D'. A vector field records all three
                           components; the plot derives |F|, the tangential and
                           the perpendicular parts from them.
        ``StartVertex`` -- index into the curve's ordered vertices where the
                           path starts (distance 0).
        ``EndVertex``   -- index where it ends; ``-1`` is the curve's own end.
                           Below the start on an open curve, the path runs
                           backwards; on a closed curve it runs forward,
                           wrapping past the curve's first vertex.
        ``Reversed``    -- closed curves only: go round the other way.

    Hidden ``PathPoints`` holds a coarse world-mm polyline of the selected
    portion for the view provider, rebuilt by ``execute``.
    """

    def __init__(self, obj):
        self.Type = _FIELD_LINE_TYPE
        obj.Proxy = self
        _add_type_marker(obj, _FIELD_LINE_TYPE)
        _ensure_field_line_props(obj)

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _FIELD_LINE_TYPE)
        _ensure_field_line_props(obj)

    def execute(self, obj):
        """Rebuild the drawn path preview from the curve and its end vertices."""
        pts = []
        sampled = sample_field_line(obj, _PREVIEW_SAMPLES)
        if sampled is not None:
            pts = [FreeCAD.Vector(*p) for p in sampled["points"]]
        obj.PathPoints = pts

    def dumps(self):
        return {"Type": getattr(self, "Type", _FIELD_LINE_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _FIELD_LINE_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


def _ensure_field_line_props(obj):
    """Add any of the field-line monitor's properties *obj* is missing."""
    if not hasattr(obj, "Sketch"):
        obj.addProperty(
            "App::PropertyLink", "Sketch", "Monitor",
            "Sketch whose curve the field is sampled along (drag a sketch "
            "from the tree onto this monitor to assign it)",
        )
    if not hasattr(obj, "Field"):
        obj.addProperty(
            "App::PropertyEnumeration", "Field", "Monitor",
            "Quantity sampled along the curve: phi (the potential), E or D. "
            "Every component of a vector is recorded; the one to view is "
            "chosen when plotting",
        )
        obj.Field = list(_ES_FIELDS)
        obj.Field = "E"
    if not hasattr(obj, "StartVertex"):
        obj.addProperty(
            "App::PropertyInteger", "StartVertex", "Monitor",
            "Curve vertex the path starts at (distance 0)",
        )
        obj.StartVertex = 0
    if not hasattr(obj, "EndVertex"):
        obj.addProperty(
            "App::PropertyInteger", "EndVertex", "Monitor",
            "Curve vertex the path ends at (-1: the end of the curve)",
        )
        obj.EndVertex = _CURVE_END
    if not hasattr(obj, "Reversed"):
        obj.addProperty(
            "App::PropertyBool", "Reversed", "Monitor",
            "Closed curves only: go round the curve against its own "
            "direction from the start vertex to the end vertex",
        )
        obj.Reversed = False
    if not hasattr(obj, "PathPoints"):
        obj.addProperty("App::PropertyVectorList", "PathPoints", "Monitor", "")
        obj.setEditorMode("PathPoints", 2)  # hidden


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


def is_dissipation_monitor(obj):
    """Return True if *obj* is a Wavesim Dissipation monitor."""
    return _is_type(obj, _DISSIPATION_TYPE)


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


def find_dissipation_monitors(sim):
    """Return all Dissipation monitors under the Simulation container *sim*."""
    return _find(sim, is_dissipation_monitor)


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


def is_field_line_monitor(obj):
    """Return True if *obj* is a Wavesim field-along-a-curve monitor."""
    return _is_type(obj, _FIELD_LINE_TYPE)


def is_sketch_monitor(obj):
    """True for every monitor kind whose geometry is a sketch dropped on it."""
    return (is_voltage_monitor(obj) or is_current_monitor(obj)
            or is_field_line_monitor(obj))


def find_field_line_monitors(sim):
    """Return all field-along-a-curve monitors under the Simulation *sim*."""
    return _find(sim, is_field_line_monitor)


def path_monitor_points_mm(sim):
    """World-mm bbox corners of every sketch-path monitor curve under *sim*.

    Feeds the domain auto-sizing (like source points and snapshot offsets), so a
    monitor curve outside the material bounds enlarges the domain to contain it
    rather than having its quadrature points clipped to the grid edge.
    """
    pts = []
    for mon in _find(sim, is_sketch_monitor):
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


def snapshot_dt_s(sim):
    """The grid's CFL time step (seconds) for *sim*, or 0.0 when unavailable.

    Snapshots record every so-many time steps; this is what converts a real-time
    recording interval into that step count (and back for display).
    """
    from wavesim_gui import domain as domain_mod

    dom = domain_mod.find_domain(sim) if sim is not None else None
    if dom is None:
        return 0.0
    try:
        return float(domain_mod.cfl_dt(dom))
    except Exception:
        return 0.0


def steps_for_interval(interval_s, dt_s):
    """Number of time steps closest to *interval_s* at CFL step *dt_s*.

    Returns 0 when either is non-positive (the caller then falls back to the
    stored ``EveryNSteps``).
    """
    if interval_s <= 0.0 or dt_s <= 0.0:
        return 0
    return max(1, int(round(interval_s / dt_s)))


def snapshot_every_n(snap, sim=None):
    """Resolve *snap*'s record cadence to a step count for the solver.

    Derived from ``RecordInterval`` (real time) and the current grid's CFL step
    when both are available; otherwise the stored ``EveryNSteps`` fallback.
    """
    if sim is None:
        sim = active_simulation(snap.Document)
    interval = float(getattr(snap, "RecordInterval", 0.0))
    steps = steps_for_interval(interval, snapshot_dt_s(sim))
    if steps <= 0:
        steps = max(1, int(getattr(snap, "EveryNSteps", 20)))
    return steps


def snapshot_spec(snap, origin_m):
    """Return the ``job.json`` snapshot dict for *snap* in the solver frame.

    ``field`` is 'E' or 'H'; the runner records each of its three components.
    ``normal`` is the slice's normal axis ('x'/'y'/'z'); ``position`` is the
    plane's offset along that axis, in the solver frame (origin subtracted).
    ``every_N_steps`` is derived from the real-time ``RecordInterval`` and the
    current grid's CFL step (see :func:`snapshot_every_n`).
    """
    plane = str(snap.Plane)
    normal = _PLANE_NORMAL.get(plane, "z")
    axis = _PLANE_OFFSET_AXIS.get(plane, "z")
    origin_along = {"x": origin_m[0], "y": origin_m[1], "z": origin_m[2]}[axis]
    return {
        "name": str(snap.Label or snap.Name),
        "field": str(getattr(snap, "Field", "E")),
        "normal": normal,
        "position": float(snap.Offset.Value) / _MM_PER_M - origin_along,
        "every_N_steps": snapshot_every_n(snap),
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


def _curve_wire(mon, warn=True):
    """The wire of *mon*'s linked sketch curve, or ``None`` if it has none.

    The sketch's edges are sorted into connected wires and the longest one is
    used; *warn* reports the others being dropped (off for the quiet callers --
    the tree preview and the panel -- which would otherwise repeat it on every
    recompute).
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
    if len(wires) > 1 and warn:
        FreeCAD.Console.PrintWarning(
            "Wavesim: sketch '{}' on monitor '{}' has {} disconnected curves; "
            "using the longest.\n".format(sketch.Label, mon.Label, len(wires))
        )
    return max(wires, key=lambda w: w.Length)


def _monitor_path_mm(mon, deflection_mm):
    """Ordered world-mm vertices of *mon*'s linked sketch curve, or ``None``.

    The longest wire of the sketch is discretised to the given chordal
    tolerance. Vertex order (and so the sign of the recorded integral) follows
    the wire's own direction.
    """
    wire = _curve_wire(mon)
    if wire is None:
        return None
    return wire.discretize(Deflection=max(float(deflection_mm), 1.0e-6))


# --------------------------------------------------------------------------- #
# Field-line geometry
# --------------------------------------------------------------------------- #

def curve_vertices_mm(mon):
    """``(vertices, closed)`` of *mon*'s curve, in the wire's own order.

    *vertices* are world-mm ``FreeCAD.Vector`` s, one per edge end; a closed
    curve lists its first vertex once, not again at the end. ``([], False)``
    when the monitor has no curve yet. These are what the panel's Start/End
    boxes offer, and what ``StartVertex``/``EndVertex`` index.
    """
    wire = _curve_wire(mon, warn=False)
    if wire is None:
        return [], False
    closed = bool(wire.isClosed())
    verts = [FreeCAD.Vector(v.Point) for v in wire.OrderedVertexes]
    # A closed wire may or may not repeat its first vertex; normalise to once.
    if closed and len(verts) > 1 and (verts[0] - verts[-1]).Length < 1.0e-7:
        verts = verts[:-1]
    return verts, closed


def _resolve_ends(n_verts, closed, start, end):
    """Clamp *start*/*end* to the curve and return ``(i, j)`` vertex indices.

    A stored index out of range (the sketch lost vertices since it was set)
    falls back to the curve's own start/end, rather than failing the run.
    :data:`_CURVE_END` is the end of the curve: the last vertex of an open one,
    the start again (a full lap) of a closed one.
    """
    last = n_verts if closed else n_verts - 1
    i = int(start) if 0 <= int(start) < n_verts else 0
    if int(end) == _CURVE_END or not 0 <= int(end) < n_verts:
        j = i if closed else last
    else:
        j = int(end)
    return i, j


def _path_edges(wire, closed, i, j, reverse=False):
    """The ``[(edge, reversed), ...]`` that walk the wire from vertex *i* to *j*.

    An open curve walks backwards when *j* is below *i*. A closed curve walks
    forward (in the wire's own order), wrapping past vertex 0, or the other way
    round when *reverse* is set; ``i == j`` there is a full lap. The edge's
    *reversed* flag is whether it has to be traversed against its own
    parametrisation -- worked out from where its ends actually are, since a
    sketch edge's direction has nothing to do with its order in the wire.
    """
    edges = list(wire.OrderedEdges)
    verts = [v.Point for v in wire.OrderedVertexes]
    n = len(edges)
    if n == 0:
        return []
    if closed and reverse:
        count = (i - j) % n or n
        order = [((i - 1 - k) % n, True) for k in range(count)]
    elif closed:
        count = (j - i) % n or n
        order = [((i + k) % n, False) for k in range(count)]
    elif j >= i:
        order = [(k, False) for k in range(i, j)]
    else:
        order = [(k, True) for k in range(i - 1, j - 1, -1)]

    out = []
    for k, backwards in order:
        edge = edges[k]
        # The wire traverses edge k from verts[k] to verts[k+1]; walking the
        # path backwards swaps them.
        start = verts[k] if not backwards else verts[(k + 1) % len(verts)]
        first = edge.valueAt(edge.FirstParameter)
        last = edge.valueAt(edge.LastParameter)
        out.append((edge, (last - start).Length < (first - start).Length))
    return out


def _curve_plane_normal(mon, wire):
    """Unit normal of the plane *mon*'s curve lies in, or ``None`` if none.

    A sketch's own plane when the curve is a sketch -- which also covers a
    single straight segment, which lies in infinitely many planes and so
    defines none of its own. Otherwise whatever plane OCC finds the wire in.
    """
    sketch = getattr(mon, "Sketch", None)
    if sketch is not None and str(getattr(sketch, "TypeId", "")).startswith(
            "Sketcher::"):
        n = sketch.getGlobalPlacement().Rotation.multVec(FreeCAD.Vector(0, 0, 1))
        if n.Length > 0:
            return n.normalize()
    try:
        plane = wire.findPlane()
    except Exception:
        plane = None
    if plane is None:
        return None
    n = FreeCAD.Vector(plane.Axis)
    return n.normalize() if n.Length > 0 else None


def sample_field_line(mon, max_samples=_FIELD_LINE_MAX_SAMPLES, step_mm=None):
    """Sample *mon*'s curve between its Start and End vertices, or ``None``.

    Returns a dict of world-mm samples spaced evenly along the curve:

    * ``points``   -- ``[(x, y, z), ...]`` mm;
    * ``tangents`` -- unit tangent at each point, in the direction of travel,
      taken from the edge's own geometry rather than a finite difference;
    * ``s``        -- distance along the path from the start (mm);
    * ``vertex_s`` -- the distance at each curve vertex the path passes;
    * ``normal``   -- the curve plane's unit normal, or ``None``;
    * ``length``   -- total path length (mm).

    At a corner the vertex is kept twice, once per edge, with each edge's own
    tangent: the tangential field genuinely jumps there, and smoothing the two
    sides together would plot a value that exists on neither.

    *step_mm* is the sample spacing; ``max_samples`` caps the count, coarsening
    the step for a long path. ``None`` when there is no curve or the selected
    portion has no length.
    """
    wire = _curve_wire(mon, warn=False)
    if wire is None:
        return None
    verts, closed = curve_vertices_mm(mon)
    if not verts:
        return None
    i, j = _resolve_ends(len(verts), closed,
                         getattr(mon, "StartVertex", 0),
                         getattr(mon, "EndVertex", _CURVE_END))
    path = _path_edges(wire, closed, i, j,
                       reverse=bool(getattr(mon, "Reversed", False)))
    total = sum(float(edge.Length) for edge, _rev in path)
    if total <= 1.0e-9:
        return None
    step = total / max(1, int(max_samples) - 1)
    if step_mm is not None and float(step_mm) > step:
        step = float(step_mm)

    points, tangents, dist, vertex_s = [], [], [], [0.0]
    travelled = 0.0
    for edge, backwards in path:
        length = float(edge.Length)
        if length <= 1.0e-12:
            continue
        count = max(2, int(round(length / step)) + 1)
        samples = []
        for k in range(count):
            along = length * k / (count - 1)
            u = edge.getParameterByLength(length - along if backwards else along)
            t = FreeCAD.Vector(edge.tangentAt(u))
            samples.append((along, edge.valueAt(u),
                            t.normalize() if t.Length > 0 else t))
        # Point the tangents the way the samples actually travel. Checked
        # rather than assumed: whether ``tangentAt`` honours a reversed edge's
        # orientation is the library's business, and a wrong guess here would
        # flip the sign of every tangential value on that edge.
        if samples[0][2].dot(samples[1][1] - samples[0][1]) < 0:
            samples = [(a, p, -t) for a, p, t in samples]
        for k, (along, p, t) in enumerate(samples):
            # The vertex shared with the previous edge: keep it only when the
            # direction turns there (see the docstring).
            if k == 0 and tangents:
                if (p - FreeCAD.Vector(*points[-1])).Length < 1.0e-9 and \
                        t.dot(FreeCAD.Vector(*tangents[-1])) > 0.9998:
                    continue
            points.append((p.x, p.y, p.z))
            tangents.append((t.x, t.y, t.z))
            dist.append(travelled + along)
        travelled += length
        vertex_s.append(travelled)
    if len(points) < 2:
        return None
    normal = _curve_plane_normal(mon, wire)
    return {
        "points": points,
        "tangents": tangents,
        "s": dist,
        "vertex_s": vertex_s,
        "normal": (normal.x, normal.y, normal.z) if normal is not None else None,
        "length": travelled,
    }


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


def field_line_spec(mon, origin_m, step_mm):
    """Return the ``job.json`` dict for a field-line monitor, or ``None``.

    The curve is sampled here, not in the runner: the runner has no geometry
    kernel, and the tangents come exactly from the edges rather than from
    differencing the samples. Points are solver-frame metres; ``s`` is the
    distance along the path in metres, from 0 at the start vertex. A monitor
    with no curve, or whose start and end coincide on an open curve, is skipped
    with a warning so the run still proceeds.
    """
    sampled = sample_field_line(mon, step_mm=step_mm)
    if sampled is None:
        FreeCAD.Console.PrintWarning(
            "Wavesim: field-line monitor '{}' has no path to sample (drag a "
            "sketch onto it, and pick different start and end vertices); "
            "skipping it.\n".format(mon.Label)
        )
        return None
    return {
        "name": str(mon.Label or mon.Name),
        "field": str(getattr(mon, "Field", "E")),
        "points": [
            [p[0] / _MM_PER_M - origin_m[0],
             p[1] / _MM_PER_M - origin_m[1],
             p[2] / _MM_PER_M - origin_m[2]]
            for p in sampled["points"]
        ],
        "tangents": [list(t) for t in sampled["tangents"]],
        "s": [d / _MM_PER_M for d in sampled["s"]],
        "vertex_s": [d / _MM_PER_M for d in sampled["vertex_s"]],
        "normal": list(sampled["normal"]) if sampled["normal"] else None,
    }


def monitors_spec(sim, origin_m):
    """Return the ``job.json`` ``monitors`` dict for the simulation *sim*.

    Every entry is user-defined: ``energy`` and ``dissipation`` name a volume
    only when an explicit monitor asks for it (see :func:`_region_spec`). A
    simulation with no monitors records nothing, so the job contains exactly
    what was asked for and nothing else.

    ``field_lines`` is filled only for an electrostatic run, the one mode that
    samples them; the full-wave runner has nothing to read them with.
    """
    from wavesim_gui.commands import is_electrostatic

    field_lines = []
    if is_electrostatic(sim):
        # Same quarter-cell spacing the voltage paths are discretised to.
        step = _path_deflection_mm(sim)
        field_lines = [
            s for s in (field_line_spec(m, origin_m, step)
                        for m in find_field_line_monitors(sim)) if s
        ]
    elif find_field_line_monitors(sim):
        FreeCAD.Console.PrintWarning(
            "Wavesim: field-along-curve monitors only record in an "
            "electrostatic run; this full-wave run ignores them.\n"
        )
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
    return {
        "energy": energy_spec(find_energy_monitors(sim)),
        "dissipation": dissipation_spec(find_dissipation_monitors(sim)),
        "probes": probes, "snapshots": snapshots,
        "voltages": voltages, "currents": currents,
        "field_lines": field_lines,
    }


def _region_spec(objs):
    """Return the ``{"full": .., "interior": ..}`` dict for region monitors *objs*.

    One flag per solver ``region`` value: ``interior`` sums the physical domain
    with the PML cells dropped, ``full`` sums the whole grid. Both false (the
    no-monitor case) means the quantity is not recorded at all. Shared by the
    energy and dissipation entries, which the runner reads with one helper.
    """
    return {
        "full": any(bool(getattr(o, "RecordFullDomain", False)) for o in objs),
        "interior": any(bool(getattr(o, "RecordInterior", True)) for o in objs),
    }


def energy_spec(energy_objs):
    """Return the ``monitors.energy`` dict for the Energy monitors *energy_objs*."""
    return _region_spec(energy_objs)


def dissipation_spec(dissipation_objs):
    """Return the ``monitors.dissipation`` dict for the Dissipation monitors."""
    return _region_spec(dissipation_objs)


def _region_text(obj):
    """The "(excl. PML)"-style suffix naming the volume(s) a monitor sums."""
    interior = bool(getattr(obj, "RecordInterior", True))
    full = bool(getattr(obj, "RecordFullDomain", False))
    if interior and full:
        return "excl. + incl. PML"
    if full:
        return "incl. PML"
    if interior:
        return "excl. PML"
    return "nothing selected"


def _energy_label(obj):
    """Tree label naming the volume(s) this energy monitor sums."""
    return "Energy ({})".format(_region_text(obj))


def _dissipation_label(obj):
    """Tree label naming the volume(s) this dissipation monitor sums."""
    return "Dissipation ({})".format(_region_text(obj))


def _probe_label(obj):
    return "Probe ({})".format(getattr(obj, "Component", "Ez"))


def _snapshot_label(obj):
    plane = str(getattr(obj, "Plane", "XY"))
    axis = _PLANE_OFFSET_AXIS.get(plane, "z")
    off_mm = float(obj.Offset.Value) if hasattr(obj, "Offset") else 0.0
    interval = float(getattr(obj, "RecordInterval", 0.0))
    sim = active_simulation(obj.Document)
    from wavesim_gui.commands import is_electrostatic

    if is_electrostatic(sim):
        # One solve, one picture: a cadence in the label would be a lie.
        return "Snapshot ({} {} @ {}={:g} mm)".format(
            getattr(obj, "Field", "E"), plane, axis, off_mm,
        )
    if interval > 0.0 and sim is not None:
        unit = units.get_time_unit(sim)
        every = "{:g} {}".format(units.time_from_si(interval, unit), unit)
    else:
        every = "{} steps".format(int(getattr(obj, "EveryNSteps", 20)))
    return "Snapshot ({} {} @ {}={:g} mm, every {})".format(
        getattr(obj, "Field", "E"), plane, axis, off_mm, every,
    )


def _path_monitor_label(obj):
    kind = "Voltage" if is_voltage_monitor(obj) else "Current"
    sketch = getattr(obj, "Sketch", None)
    if sketch is not None:
        return "{} Monitor ({})".format(kind, sketch.Label)
    return "{} Monitor (no curve)".format(kind)


def _field_line_label(obj):
    sketch = getattr(obj, "Sketch", None)
    return "Field Along Curve ({}, {})".format(
        getattr(obj, "Field", "E"),
        sketch.Label if sketch is not None else "no curve",
    )


def _sketch_monitor_label(obj):
    """The automatic label of any sketch-path monitor, by its kind."""
    if is_field_line_monitor(obj):
        return _field_line_label(obj)
    return _path_monitor_label(obj)


# --------------------------------------------------------------------------- #
# GUI: view providers, task panels, commands
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

    from wavesim_gui import visibility

    class ProbeViewProvider:
        """Coin view provider drawing a probe as a teal point marker."""

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
            return _FIELD_PROBE_ICON

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
        """Coin view provider drawing a snapshot as a translucent teal plane."""

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

            # An opaque teal border to make the plane edges read clearly.
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
            return _SNAPSHOT_MONITOR_ICON

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
        """Tree-only view provider for the energy monitor.

        No 3D geometry (the energy monitor has no location), so the object is
        simply listed under Monitors; double-click opens its panel to choose
        which volume(s) the energy sum covers.
        """

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            self.ViewObject = vobj
            self.Object = vobj.Object

        def getIcon(self):
            return _ENERGY_MONITOR_ICON

        def setEdit(self, vobj, mode=0):
            _open_energy_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_energy_panel(vobj.Object)
            return True

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class DissipationViewProvider:
        """Tree-only view provider for the dissipation monitor.

        No 3D geometry (it is a volume sum, with no location), so the object is
        simply listed under Monitors; double-click opens its panel to choose
        which volume(s) the power sum covers.
        """

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            self.ViewObject = vobj
            self.Object = vobj.Object

        def getIcon(self):
            return _DISSIPATION_MONITOR_ICON

        def setEdit(self, vobj, mode=0):
            _open_dissipation_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_dissipation_panel(vobj.Object)
            return True

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
        # The monitor's own drawn path is rebuilt on recompute, which the
        # domain notification skips when there is no domain yet.
        doc.recompute()
        domain_mod.notify_domain_inputs_changed(doc)

    # A path monitor's eye drives the sketch nested under it, exactly as a
    # Material's drives its bodies -- the monitor draws nothing itself, so
    # without this its eye would be a flag that does nothing.
    visibility.register_owner(
        "path_monitor",
        is_sketch_monitor,
        lambda mon: [getattr(mon, "Sketch", None)],
    )
    visibility.install()

    class PathMonitorViewProvider(visibility.LinkedVisibilityMixin):
        """Tree view provider for voltage/current monitors.

        No 3D geometry of its own -- the linked sketch *is* the curve, and it is
        claimed as a tree child of the monitor. Assignment mirrors Materials:
        drag a sketch onto the monitor to attach it, drag it off to detach --
        and so does its eye, inherited from
        :class:`visibility.LinkedVisibilityMixin`.
        """

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            self.ViewObject = vobj
            self.Object = vobj.Object
            self.attach_display_mode(vobj)

        def getIcon(self):
            obj = getattr(self, "Object", None)
            if obj is not None and is_current_monitor(obj):
                return _CURRENT_MONITOR_ICON
            if obj is not None and is_voltage_monitor(obj):
                return _VOLTAGE_MONITOR_ICON
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
                old_auto = _sketch_monitor_label(mon)
                mon.Sketch = None
                labels_mod.retitle(mon, old_auto, _sketch_monitor_label(mon))
                # The eye stands for the sketch; with none there is nothing
                # left for it to stand for.
                visibility.sync_from_children(mon)
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
            old_auto = _sketch_monitor_label(mon)
            mon.Sketch = obj
            if is_field_line_monitor(mon):
                # Vertex indices belong to the old curve; a new one starts
                # from its own start and runs to its own end.
                mon.StartVertex = 0
                mon.EndVertex = _CURVE_END
                mon.Reversed = False
            labels_mod.retitle(mon, old_auto, _sketch_monitor_label(mon))
            visibility.sync_from_children(mon)
            _after_path_changed(mon.Document)

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class FieldLineViewProvider(PathMonitorViewProvider):
        """Tree/3D view provider for the field-along-a-curve monitor.

        Inherits the sketch drag & drop and the linked eye from the path
        monitors. What it adds is a drawing of the **selected portion** of the
        curve -- a thick teal line with a filled dot at the start (distance 0)
        and a ring at the end -- because the panel can pick a stretch of the
        sketch rather than all of it, and the sketch alone cannot show which.
        Double-click opens the panel.
        """

        def attach(self, vobj):
            from pivy import coin

            self.ViewObject = vobj
            self.Object = vobj.Object
            root = coin.SoSeparator()

            style = coin.SoDrawStyle()
            style.lineWidth = 4
            style.pointSize = 1
            root.addChild(style)
            color = coin.SoBaseColor()
            color.rgb.setValue(*_MONITOR_COLOR)
            root.addChild(color)

            self._coords = coin.SoCoordinate3()
            root.addChild(self._coords)
            self._line = coin.SoLineSet()
            root.addChild(self._line)

            # The two ends, each with its own coordinate so a marker set can
            # draw exactly one point.
            self._ends = []
            for marker in (coin.SoMarkerSet.CIRCLE_FILLED_9_9,
                           coin.SoMarkerSet.CIRCLE_LINE_9_9):
                sep = coin.SoSeparator()
                coords = coin.SoCoordinate3()
                sep.addChild(coords)
                markers = coin.SoMarkerSet()
                markers.markerIndex = marker
                sep.addChild(markers)
                root.addChild(sep)
                self._ends.append(coords)

            # Registered as the display mode itself (in place of the mixin's
            # empty group), so the monitor's eye hides the preview too.
            vobj.addDisplayMode(root, "Default")
            self._display_node = root
            self._rebuild()

        def _rebuild(self):
            obj = getattr(self, "Object", None)
            if obj is None or not hasattr(self, "_coords"):
                return
            pts = [(v.x, v.y, v.z) for v in (getattr(obj, "PathPoints", [])
                                              or [])]
            self._coords.point.setNum(len(pts))
            if pts:
                self._coords.point.setValues(0, len(pts), pts)
            self._line.numVertices.setValue(len(pts) if len(pts) >= 2 else 0)
            for coords, pt in zip(self._ends, (pts[:1], pts[-1:])):
                coords.point.setNum(len(pt))
                if pt:
                    coords.point.setValues(0, 1, pt)

        def updateData(self, obj, prop):
            if prop == "PathPoints":
                self._rebuild()

        def getIcon(self):
            return _FIELD_LINE_ICON

        def setEdit(self, vobj, mode=0):
            _open_field_line_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_field_line_panel(vobj.Object)
            return True

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
            # The label the object still carries if nobody renamed it -- read
            # before the edits land. See wavesim_gui/labels.py.
            old_auto = _probe_label(self.obj)
            doc.openTransaction("Wavesim: Edit Probe")
            self.obj.Component = self._component.currentText()
            self.obj.Position = FreeCAD.Vector(
                self._x.value(), self._y.value(), self._z.value()
            )
            labels_mod.retitle(self.obj, old_auto, _probe_label(self.obj))
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

            # Only the quantities this simulation's solver can produce: a static
            # solve has no H and no Poynting vector, and a time-stepping one has
            # no potential. The stored property keeps every value, so switching
            # modes does not silently rewrite a monitor.
            _sim_mode = active_simulation(obj.Document)
            self._fields = fields_for_mode(_sim_mode)
            self._electrostatic = self._fields is not None and "phi" in self._fields
            self._field = QtWidgets.QComboBox()
            self._field.addItems(self._fields)
            current_field = str(getattr(obj, "Field", "E"))
            self._field.setCurrentText(
                current_field if current_field in self._fields else self._fields[0]
            )

            self._plane = QtWidgets.QComboBox()
            self._plane.addItems(_PLANES)
            self._plane.setCurrentText(str(getattr(obj, "Plane", "XY")))

            self._offset = QtWidgets.QDoubleSpinBox()
            self._offset.setRange(-1.0e6, 1.0e6)
            self._offset.setDecimals(4)
            self._offset.setSuffix(" mm")
            self._offset.setSingleStep(0.5)
            self._offset.setValue(float(obj.Offset.Value))

            # Recording cadence entered as real (simulated) time in the sim's
            # display unit; the equivalent step count is derived from the grid's
            # CFL step and shown live.
            sim = active_simulation(obj.Document)
            self._time_unit = units.get_time_unit(sim)
            self._dt_s = snapshot_dt_s(sim)
            self._interval = QtWidgets.QDoubleSpinBox()
            self._interval.setRange(0.0, 1.0e12)
            self._interval.setDecimals(6)
            self._interval.setSuffix(" " + self._time_unit)
            self._interval.setSingleStep(0.1)
            self._interval.setValue(self._initial_interval_display(obj))

            layout.addRow("Field:", self._field)
            layout.addRow("Slice plane:", self._plane)
            self._offset_label = QtWidgets.QLabel()
            layout.addRow(self._offset_label, self._offset)
            layout.addRow("Record every:", self._interval)
            self._steps_label = QtWidgets.QLabel()
            layout.addRow("Time steps:", self._steps_label)

            info = QtWidgets.QLabel(
                "The snapshot captures a 2D slice of the chosen quantity on the "
                "selected plane, offset along its normal axis. Pick phi (the "
                "potential), E or D; the electrostatic solve produces one "
                "picture, so the recording interval does not apply. All three "
                "components of a vector quantity are recorded — the one to view "
                "is chosen in the results window."
                if self._electrostatic else
                "The snapshot captures a 2D slice of the chosen quantity on the "
                "selected plane, offset along its normal axis. Pick E, H, or S "
                "(the Poynting vector S = E x H, i.e. power flow). All three "
                "components are recorded, so one monitor is enough — the "
                "component to view (e.g. Ex, Ey, Ez or |E|) is chosen in the "
                "results window. It records a frame every 'Record every' of "
                "simulated time; the equivalent number of time steps is computed "
                "from the grid's CFL time step."
            )
            info.setWordWrap(True)
            layout.addRow(info)

            # An electrostatic solve happens once, so a recording cadence has
            # nothing to describe. Hidden rather than disabled: the value is
            # still there, and still right, if the mode is switched back.
            if self._electrostatic:
                for widget in (self._interval, self._steps_label):
                    widget.setVisible(False)
                    row_label = layout.labelForField(widget)
                    if row_label is not None:
                        row_label.setVisible(False)

            self._plane.currentTextChanged.connect(self._update_offset_label)
            self._update_offset_label(self._plane.currentText())
            self._interval.valueChanged.connect(self._update_steps)
            self._update_steps()

            # Live-update the slice plane in the 3D view as the plane orientation
            # or offset change, rather than only on OK.
            self._offset.valueChanged.connect(self._live_plane)
            self._plane.currentTextChanged.connect(self._live_plane)

            self.form = form

        def _initial_interval_display(self, obj):
            """Recording interval to pre-fill, in the display time unit.

            Uses the stored ``RecordInterval`` (seconds) when set; otherwise
            derives an equivalent from the legacy ``EveryNSteps`` and the grid's
            CFL step so existing snapshots open showing a sensible time.
            """
            interval_s = float(getattr(obj, "RecordInterval", 0.0))
            if interval_s <= 0.0 and self._dt_s > 0.0:
                interval_s = int(getattr(obj, "EveryNSteps", 20)) * self._dt_s
            return units.time_from_si(interval_s, self._time_unit)

        def _update_steps(self, *_):
            si = units.time_to_si(self._interval.value(), self._time_unit)
            steps = steps_for_interval(si, self._dt_s)
            if steps > 0:
                self._steps_label.setText("{:,}".format(steps))
            elif self._dt_s <= 0.0:
                self._steps_label.setText("(set a grid cell size)")
            else:
                self._steps_label.setText("(set a record interval)")

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
            # The label the object still carries if nobody renamed it -- read
            # before the edits land. See wavesim_gui/labels.py.
            old_auto = _snapshot_label(self.obj)
            doc.openTransaction("Wavesim: Edit Snapshot")
            self.obj.Field = self._field.currentText()
            self.obj.Plane = self._plane.currentText()
            self.obj.Offset = "{} mm".format(self._offset.value())
            interval_s = units.time_to_si(self._interval.value(), self._time_unit)
            self.obj.RecordInterval = interval_s
            # Keep the derived step count in sync so it stays a sensible fallback
            # (and the value emitted to the solver) even if the grid later changes.
            steps = steps_for_interval(interval_s, self._dt_s)
            if steps > 0:
                self.obj.EveryNSteps = steps
            labels_mod.retitle(self.obj, old_auto, _snapshot_label(self.obj))
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

    class TaskEnergyPanel:
        """Task-tab panel: which volume(s) the total-energy sum covers."""

        def __init__(self, obj, created=False):
            QtWidgets = _qt_widgets()
            self.obj = obj
            self.created = created

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Energy Monitor")
            layout = QtWidgets.QVBoxLayout(form)

            self._interior = QtWidgets.QCheckBox(
                "Interior only — exclude the PML volume"
            )
            self._interior.setChecked(bool(getattr(obj, "RecordInterior", True)))
            self._full = QtWidgets.QCheckBox(
                "Whole domain — include the PML volume"
            )
            self._full.setChecked(bool(getattr(obj, "RecordFullDomain", False)))
            layout.addWidget(self._interior)
            layout.addWidget(self._full)

            info = QtWidgets.QLabel(
                "Records the total electromagnetic energy at every timestep. "
                "The PML absorbs whatever reaches it, so the interior energy of "
                "a stable run decays to ~0 once the fields have left, while the "
                "whole-domain energy also counts what is still inside the "
                "absorbing layers. Tick both to record both series."
            )
            info.setWordWrap(True)
            layout.addWidget(info)
            layout.addStretch(1)

            # A monitor recording neither volume records nothing, so unticking
            # the last remaining box ticks the other one instead.
            self._interior.toggled.connect(
                lambda on: self._keep_one_checked(on, self._full))
            self._full.toggled.connect(
                lambda on: self._keep_one_checked(on, self._interior))

            self.form = form

        @staticmethod
        def _keep_one_checked(checked, other):
            if not checked and not other.isChecked():
                other.setChecked(True)

        def accept(self):
            doc = self.obj.Document
            old_auto = _energy_label(self.obj)   # see wavesim_gui/labels.py
            doc.openTransaction("Wavesim: Edit Energy Monitor")
            self.obj.RecordInterior = self._interior.isChecked()
            self.obj.RecordFullDomain = self._full.isChecked()
            labels_mod.retitle(self.obj, old_auto, _energy_label(self.obj))
            doc.commitTransaction()
            doc.recompute()
            Gui.Control.closeDialog()
            return True

        def reject(self):
            doc = self.obj.Document
            if self.created:
                doc.openTransaction("Wavesim: Cancel Energy Monitor")
                doc.removeObject(self.obj.Name)
                doc.commitTransaction()
                doc.recompute()
            Gui.Control.closeDialog()
            return True

        def getStandardButtons(self):
            return _ok_cancel_buttons()

    class TaskDissipationPanel:
        """Task-tab panel: which volume(s) the ohmic-power sum covers."""

        def __init__(self, obj, created=False):
            QtWidgets = _qt_widgets()
            self.obj = obj
            self.created = created

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Dissipation Monitor")
            layout = QtWidgets.QVBoxLayout(form)

            self._interior = QtWidgets.QCheckBox(
                "Interior only — exclude the PML volume"
            )
            self._interior.setChecked(bool(getattr(obj, "RecordInterior", True)))
            self._full = QtWidgets.QCheckBox(
                "Whole domain — include the PML volume"
            )
            self._full.setChecked(bool(getattr(obj, "RecordFullDomain", False)))
            layout.addWidget(self._interior)
            layout.addWidget(self._full)

            info = QtWidgets.QLabel(
                "Records the ohmic power P = Σ σ|E|²·dV absorbed by lossy "
                "material at every timestep — the term that makes a lossy run's "
                "energy legitimately decay. Only a material with a nonzero "
                "conductivity dissipates; on a lossless model the series is "
                "flat zero.\n\n"
                "Keep to the interior unless you mean otherwise: the PML "
                "absorbs by design, and its power swamps the material's own "
                "loss."
            )
            info.setWordWrap(True)
            layout.addWidget(info)

            self._lossy_hint = QtWidgets.QLabel("")
            self._lossy_hint.setWordWrap(True)
            layout.addWidget(self._lossy_hint)
            self._update_lossy_hint()
            layout.addStretch(1)

            # A monitor recording neither volume records nothing, so unticking
            # the last remaining box ticks the other one instead.
            self._interior.toggled.connect(
                lambda on: self._keep_one_checked(on, self._full))
            self._full.toggled.connect(
                lambda on: self._keep_one_checked(on, self._interior))

            self.form = form

        def _update_lossy_hint(self):
            """Say so when the model has nothing for this monitor to measure."""
            from wavesim_gui import materials as materials_mod

            sim = active_simulation(self.obj.Document)
            lossy = any(materials_mod.material_is_lossy(m)
                        for m in materials_mod.find_materials(sim))
            if lossy:
                self._lossy_hint.setText("")
                self._lossy_hint.setVisible(False)
                return
            self._lossy_hint.setText(
                "No material in this simulation carries a conductivity, so "
                "this monitor will record zero. Set a material's Sigma to make "
                "it lossy."
            )
            self._lossy_hint.setStyleSheet("color: #806000;")
            self._lossy_hint.setVisible(True)

        @staticmethod
        def _keep_one_checked(checked, other):
            if not checked and not other.isChecked():
                other.setChecked(True)

        def accept(self):
            doc = self.obj.Document
            old_auto = _dissipation_label(self.obj)  # see wavesim_gui/labels.py
            doc.openTransaction("Wavesim: Edit Dissipation Monitor")
            self.obj.RecordInterior = self._interior.isChecked()
            self.obj.RecordFullDomain = self._full.isChecked()
            labels_mod.retitle(self.obj, old_auto, _dissipation_label(self.obj))
            doc.commitTransaction()
            doc.recompute()
            Gui.Control.closeDialog()
            return True

        def reject(self):
            doc = self.obj.Document
            if self.created:
                doc.openTransaction("Wavesim: Cancel Dissipation Monitor")
                doc.removeObject(self.obj.Name)
                doc.commitTransaction()
                doc.recompute()
            Gui.Control.closeDialog()
            return True

        def getStandardButtons(self):
            return _ok_cancel_buttons()

    class TaskFieldLinePanel:
        """Task-tab panel: field, and which stretch of the curve to sample.

        Start and End are picked from the curve's own vertices, so a path can
        be part of a sketch rather than all of it. Changes show live in the 3D
        view (the drawn stretch and its end markers) and are rolled back on
        Cancel.
        """

        def __init__(self, obj):
            QtWidgets = _qt_widgets()
            self.obj = obj
            self._orig = (int(obj.StartVertex), int(obj.EndVertex),
                          bool(obj.Reversed))
            # The field and label change live too, so the tree names what the
            # panel shows; both are put back on Cancel.
            self._orig_field = str(getattr(obj, "Field", "E"))
            self._orig_label = str(obj.Label)
            self._label_is_auto = labels_mod.is_auto(
                obj.Label, _field_line_label(obj))
            self._verts, self._closed = curve_vertices_mm(obj)

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Field Along Curve")
            layout = QtWidgets.QFormLayout(form)

            self._field = QtWidgets.QComboBox()
            self._field.addItems(_ES_FIELDS)
            self._field.setCurrentText(str(getattr(obj, "Field", "E")))
            layout.addRow("Field:", self._field)

            sketch = getattr(obj, "Sketch", None)
            curve = QtWidgets.QLabel(
                "{} ({} curve, {} vertices)".format(
                    sketch.Label, "closed" if self._closed else "open",
                    len(self._verts))
                if self._verts else
                "none — drag a sketch from the tree onto this monitor, then "
                "open this panel again")
            curve.setWordWrap(True)
            layout.addRow("Curve:", curve)

            self._start = QtWidgets.QComboBox()
            self._end = QtWidgets.QComboBox()
            n = len(self._verts)
            for idx, v in enumerate(self._verts):
                text = "{}: ({:g}, {:g}, {:g}) mm".format(
                    idx + 1, round(v.x, 4), round(v.y, 4), round(v.z, 4))
                if idx == 0:
                    text += "  — curve start"
                elif idx == n - 1 and not self._closed:
                    text += "  — curve end"
                self._start.addItem(text, idx)
                self._end.addItem(text, idx)
            i, j = (_resolve_ends(n, self._closed, obj.StartVertex,
                                  obj.EndVertex) if n else (0, 0))
            self._start.setCurrentIndex(i)
            self._end.setCurrentIndex(j)
            layout.addRow("Start (0 mm):", self._start)
            layout.addRow("End:", self._end)

            buttons = QtWidgets.QHBoxLayout()
            self._swap = QtWidgets.QPushButton("Swap start and end")
            buttons.addWidget(self._swap)
            buttons.addStretch(1)
            layout.addRow(buttons)

            self._reverse = QtWidgets.QCheckBox(
                "Go round the curve the other way")
            self._reverse.setChecked(bool(obj.Reversed))
            layout.addRow(self._reverse)
            self._reverse.setVisible(self._closed)

            self._length = QtWidgets.QLabel()
            layout.addRow("Path length:", self._length)

            info = QtWidgets.QLabel(
                "After an electrostatic run the result plots the chosen field "
                "against the distance travelled along the curve, from 0 mm at "
                "the start vertex (the filled dot in the 3D view) to the end "
                "(the ring). For E or D the plot offers |F|, Fx, Fy, Fz, the "
                "component along the curve (in the direction of travel) and "
                "the component perpendicular to it."
                + (" On a closed curve, picking the same start and end vertex "
                   "samples one full lap." if self._closed else ""))
            info.setWordWrap(True)
            layout.addRow(info)

            for widget in (self._start, self._end, self._swap, self._reverse):
                widget.setEnabled(bool(self._verts))

            self._field.currentTextChanged.connect(self._live_field)
            self._start.currentIndexChanged.connect(self._live)
            self._end.currentIndexChanged.connect(self._live)
            self._reverse.toggled.connect(self._live)
            self._swap.clicked.connect(self._on_swap)
            self._update_length()
            self.form = form

        def _chosen(self):
            """``(StartVertex, EndVertex, Reversed)`` as the boxes set them.

            The curve's own end is stored as :data:`_CURVE_END` rather than
            its index, so the path still ends at the end if the sketch later
            gains vertices.
            """
            if not self._verts:
                return self._orig
            i = int(self._start.currentData())
            j = int(self._end.currentData())
            n = len(self._verts)
            if (self._closed and j == i) or (not self._closed and j == n - 1):
                j = _CURVE_END
            return i, j, self._closed and self._reverse.isChecked()

        def _apply(self, values):
            self.obj.StartVertex, self.obj.EndVertex, self.obj.Reversed = values

        def _on_swap(self):
            i, j = self._start.currentIndex(), self._end.currentIndex()
            for box in (self._start, self._end):
                box.blockSignals(True)
            self._start.setCurrentIndex(j)
            self._end.setCurrentIndex(i)
            for box in (self._start, self._end):
                box.blockSignals(False)
            # On a loop, swapping alone would select the *other* arc; going
            # round the other way too is what keeps the same stretch, reversed.
            if self._closed and i != j:
                self._reverse.blockSignals(True)
                self._reverse.setChecked(not self._reverse.isChecked())
                self._reverse.blockSignals(False)
            self._live()

        def _set_field(self, field):
            """Set ``Field`` and, unless the user renamed it, the label."""
            self.obj.Field = field
            if self._label_is_auto:
                new = _field_line_label(self.obj)
                if str(self.obj.Label) != new:
                    self.obj.Label = new

        def _live_field(self, text):
            self._set_field(str(text))

        def _live(self, *_):
            self._apply(self._chosen())
            self.obj.touch()
            self.obj.Document.recompute()
            self._update_length()

        def _update_length(self):
            sampled = sample_field_line(self.obj, max_samples=2)
            if sampled is None:
                self._length.setText(
                    "no path — pick different start and end vertices"
                    if self._verts else "—")
                self._length.setStyleSheet("color: #a03000;")
                return
            edges = len(sampled["vertex_s"]) - 1
            self._length.setText("{:.4g} mm along {} edge{}".format(
                sampled["length"], edges, "" if edges == 1 else "s"))
            self._length.setStyleSheet("")

        def accept(self):
            from wavesim_gui import domain as domain_mod

            doc = self.obj.Document
            chosen = self._chosen()
            field = self._field.currentText()
            # Back to the originals first so the transaction records the whole
            # change (the live edits landed outside it).
            self._restore()
            doc.openTransaction("Wavesim: Edit Field Along Curve")
            self._set_field(field)
            self._apply(chosen)
            doc.commitTransaction()
            self.obj.touch()
            doc.recompute()
            domain_mod.notify_domain_inputs_changed(doc)
            Gui.Control.closeDialog()
            return True

        def _restore(self):
            """Undo every live edit: vertices, field and label."""
            self._apply(self._orig)
            self.obj.Field = self._orig_field
            if str(self.obj.Label) != self._orig_label:
                self.obj.Label = self._orig_label

        def reject(self):
            self._restore()
            self.obj.touch()
            self.obj.Document.recompute()
            Gui.Control.closeDialog()
            return True

        def getStandardButtons(self):
            return _ok_cancel_buttons()

    def _open_field_line_panel(obj):
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskFieldLinePanel(obj))

    def _open_probe_panel(obj, created=False):
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskProbePanel(obj, created=created))

    def _open_energy_panel(obj, created=False):
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskEnergyPanel(obj, created=created))

    def _open_dissipation_panel(obj, created=False):
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskDissipationPanel(obj, created=created))

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
                "Pixmap": _FIELD_PROBE_ICON,
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
                "Pixmap": _SNAPSHOT_MONITOR_ICON,
                "MenuText": "Add Snapshot",
                "ToolTip": "Add a snapshot monitor capturing a 2D field slice "
                "at a chosen time interval",
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
        """Add the total-energy monitor (a tree-only object) and open its editor."""

        def GetResources(self):
            return {
                "Pixmap": _ENERGY_MONITOR_ICON,
                "MenuText": "Add Energy Monitor",
                "ToolTip": "Record the total electromagnetic energy in the "
                "domain over time, with or without the PML volume",
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
                energy.Label = _energy_label(energy)
                if energy.ViewObject is not None:
                    EnergyViewProvider(energy.ViewObject)
                monitors_group(sim).addObject(energy)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()
            _open_energy_panel(energy, created=True)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    class CommandAddDissipationMonitor:
        """Add the ohmic-dissipation monitor (tree-only) and open its editor."""

        def GetResources(self):
            return {
                "Pixmap": _DISSIPATION_MONITOR_ICON,
                "MenuText": "Add Dissipation Monitor",
                "ToolTip": "Record the ohmic power absorbed by lossy material "
                "over time, with or without the PML volume",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = _require_simulation()
            if sim is None:
                return
            if find_dissipation_monitors(sim):
                FreeCAD.Console.PrintWarning(
                    "Wavesim: a dissipation monitor already exists.\n"
                )
                return
            doc.openTransaction("Wavesim: Add Dissipation Monitor")
            try:
                mon = doc.addObject("App::FeaturePython", "DissipationMonitor")
                DissipationObject(mon)
                mon.Label = _dissipation_label(mon)
                if mon.ViewObject is not None:
                    DissipationViewProvider(mon.ViewObject)
                monitors_group(sim).addObject(mon)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()
            _open_dissipation_panel(mon, created=True)

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
                "Pixmap": _VOLTAGE_MONITOR_ICON,
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
                "Pixmap": _CURRENT_MONITOR_ICON,
                "MenuText": "Add Current Monitor",
                "ToolTip": "Record the current I(t) = ∮H·dl around a closed "
                "sketch curve (drag the sketch onto the monitor in the tree)",
            }

    class CommandAddFieldLineMonitor:
        """Add a field-along-a-curve monitor (electrostatic runs only).

        Like the voltage monitor it is created empty and takes its curve from a
        sketch dragged onto it; a curve already selected when the button is
        pressed is attached straight away, which saves the drag.
        """

        def GetResources(self):
            return {
                "Pixmap": _FIELD_LINE_ICON,
                "MenuText": "Add Field Along Curve",
                "ToolTip": "Plot phi, E or D along a sketch curve against the "
                "distance travelled along it (electrostatic runs; drag the "
                "sketch onto the monitor in the tree)",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = _require_simulation()
            if sim is None:
                return
            picked = [o for o in Gui.Selection.getSelection()
                      if _is_curve_object(o)]
            doc.openTransaction("Wavesim: Add Field Along Curve")
            try:
                mon = doc.addObject("App::FeaturePython", _FIELD_LINE_TYPE)
                FieldLineObject(mon)
                if len(picked) == 1:
                    mon.Sketch = picked[0]
                mon.Label = _field_line_label(mon)
                if mon.ViewObject is not None:
                    FieldLineViewProvider(mon.ViewObject)
                monitors_group(sim).addObject(mon)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            if mon.Sketch is not None:
                visibility.sync_from_children(mon)
                _after_path_changed(doc)
                FreeCAD.Console.PrintMessage(
                    "Wavesim: field-along-curve monitor uses '{}'. "
                    "Double-click it to choose the field and the start and "
                    "end vertices.\n".format(mon.Sketch.Label))
            else:
                doc.recompute()
                FreeCAD.Console.PrintMessage(
                    "Wavesim: drag a sketch from the model tree onto the "
                    "Field Along Curve monitor to set its path, then "
                    "double-click the monitor to choose the field and the "
                    "start and end vertices.\n")

        def IsActive(self):
            # Electrostatics only: the field it samples is the static
            # solution. The full-wave runner has no single field to sample.
            from wavesim_gui.commands import is_electrostatic

            sim = active_simulation(FreeCAD.ActiveDocument)
            return sim is not None and is_electrostatic(sim)

    Gui.addCommand("Wavesim_AddFieldLineMonitor", CommandAddFieldLineMonitor())
    Gui.addCommand("Wavesim_AddProbe", CommandAddProbe())
    Gui.addCommand("Wavesim_AddSnapshot", CommandAddSnapshot())
    Gui.addCommand("Wavesim_AddEnergyMonitor", CommandAddEnergyMonitor())
    Gui.addCommand("Wavesim_AddDissipationMonitor",
                   CommandAddDissipationMonitor())
    Gui.addCommand("Wavesim_AddVoltageMonitor", CommandAddVoltageMonitor())
    Gui.addCommand("Wavesim_AddCurrentMonitor", CommandAddCurrentMonitor())
