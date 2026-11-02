# -*- coding: utf-8 -*-
"""Scripting API: probes, snapshots and the other monitors.

GUI-free counterparts of the Add Probe / Snapshot / Energy Monitor /
Dissipation Monitor / Voltage Monitor / Current Monitor / Field Along Curve
commands. Each function creates the monitor through the same helper the
command uses (:mod:`wavesim_gui.monitors` ``new_*``), then applies its
arguments the way the monitor's task panel does on OK -- so defaults, labels
and side effects are the GUI's.

The voltage, current and field-along-curve monitors take their path from a
curve object (in the GUI: a sketch dragged onto the monitor). Here the path is
either an existing curve object (sketch, wire, ...) or is built from points /
a circle, and is then attached exactly like a tree drop
(:func:`wavesim_gui.monitors.attach_path`).

See :mod:`wavesim_gui.api_common` for the argument conventions.
"""

import FreeCAD

from wavesim_gui import api_common as c
from wavesim_gui.api_common import ApiError, api_function

__all__ = [
    "add_probe", "add_snapshot", "add_energy_monitor",
    "add_dissipation_monitor", "add_voltage_monitor", "add_current_monitor",
    "add_field_line_monitor", "set_monitor_path",
]

c.register_dimensions({"RecordInterval": "time"}, "Snapshot")

_PROBE_COMPONENTS = ("Ex", "Ey", "Ez", "Hx", "Hy", "Hz", "|E|", "|H|")
_NORMAL_TO_PLANE = {"x": "YZ", "y": "XZ", "z": "XY"}


def _mon():
    from wavesim_gui import monitors
    return monitors


def _check_quantities(values, dimension, what):
    """Parse every quantity up front so bad input fails before any change."""
    for value in values:
        try:
            c.parse_quantity(value, dimension)
        except ApiError as exc:
            raise ApiError("{}: {}".format(what, exc))


def _check_point(xyz, what):
    if not isinstance(xyz, (list, tuple)) or len(xyz) != 3:
        raise ApiError("{} needs three coordinates [x, y, z], got {!r}.".format(
            what, xyz))
    _check_quantities(xyz, "length", what)


def _new_label(doc, label, what):
    return None if label is None else c.check_new_label(doc, label, what)


def _apply_label(obj, label):
    if label is not None:
        c.set_label(obj, label)


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #

def _probe_component(value):
    m = _mon()
    text = str(value).strip().replace(m._BAR, "|")
    choice = c.enum_choice(text, _PROBE_COMPONENTS, "probe component")
    return {"|E|": m._E_MAG, "|H|": m._H_MAG}.get(choice, choice)


@api_function("monitor")
def add_probe(sim=None, *, label=None, position=None, component="Ez"):
    """Add a point field probe and return it (*Wavesim -> Add Probe*).

    Records one field quantity at one point (snapped to the nearest grid
    cell) over time -- e.g. the field at a receiver location. Full-wave runs
    only record it; an electrostatic run ignores probes (warning at job build),
    just as in the GUI.

    :param label: unique label; default is the GUI's auto label
        ``"Probe (Ez)"`` (it follows the component).
    :param position: ``[x, y, z]`` in world coordinates, each a length (mm by
        default, ``"3 mm"``, or a param expression like ``"=GAP/2"``).
        Default: the domain/geometry centre, as in the GUI.
    :param component: ``'Ex'``, ``'Ey'``, ``'Ez'`` (default), ``'Hx'``,
        ``'Hy'``, ``'Hz'``, ``'|E|'`` or ``'|H|'`` (field magnitudes).
    :returns: the Probe object (``WavesimType`` ``"Probe"``).

    Example: ``add_probe(sim, position=[0, 0, "=SUB_H"], component="Ez")``
    """
    from wavesim_gui import labels as labels_mod

    m = _mon()
    sim = c.get_sim(sim)
    doc = sim.Document
    label = _new_label(doc, label, "probe")
    comp = _probe_component(component)
    if position is not None:
        _check_point(position, "position")
    with c.transaction(doc, "Wavesim: Add Probe"):
        probe = m.new_probe(sim)
        old_auto = m._probe_label(probe)
        probe.Component = comp
        if position is not None:
            c.set_vector(probe, "Position", position)
        labels_mod.retitle(probe, old_auto, m._probe_label(probe))
        _apply_label(probe, label)
    doc.recompute()
    return probe


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #

def _snapshot_plane(normal):
    text = str(normal).strip()
    if text.lower() in _NORMAL_TO_PLANE:
        return _NORMAL_TO_PLANE[text.lower()]
    m = _mon()
    for plane in m._PLANES:
        if text.upper() == plane:
            return plane
    raise ApiError("Invalid snapshot normal {!r}; use 'x', 'y' or 'z' (or the "
                   "plane 'YZ', 'XZ', 'XY').".format(normal))


@api_function("monitor")
def add_snapshot(sim=None, *, label=None, field="E", normal="z", offset=None,
                 record_interval=None, every_n_steps=None):
    """Add a 2D field-slice monitor and return it (*Wavesim -> Add Snapshot*).

    Captures a whole field on a plane: all three components are recorded and
    the magnitude is derived when plotting, so one snapshot covers Ex/Ey/Ez/|E|.
    Full-wave runs record a frame every *record_interval*; an electrostatic run
    saves one picture of the slice. A slice outside the domain enlarges the
    domain to contain it.

    :param label: unique label; default is the GUI's auto label, e.g.
        ``"Snapshot (E XY @ z=0 mm, every 20 steps)"``.
    :param field: full-wave: ``'E'`` (default), ``'H'`` or ``'S'`` (Poynting
        vector E x H). Electrostatic: ``'phi'`` (potential), ``'E'`` or ``'D'``.
    :param normal: slice normal axis ``'x'``, ``'y'`` or ``'z'`` (default),
        i.e. plane YZ, XZ or XY (the plane names are accepted too).
    :param offset: world position of the plane along its normal, a length
        (mm by default) or param expression. Default: the domain/geometry
        centre along that axis (the GUI's default for the XY plane).
    :param record_interval: simulated time between frames (s by default,
        e.g. ``"20 ps"``, or a param expression in seconds). The step count is
        derived from the grid's CFL step, exactly as the panel does.
    :param every_n_steps: alternative to *record_interval*: a frame every N
        time steps (integer >= 1); the interval is then left unset.
        Default (neither given): the panel's default, 20 steps' worth of time.
    :returns: the Snapshot object (``WavesimType`` ``"Snapshot"``).

    Example: ``add_snapshot(sim, field="E", normal="z", offset="=SUB_H/2",
    record_interval="10 ps")``
    """
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import labels as labels_mod

    m = _mon()
    sim = c.get_sim(sim)
    doc = sim.Document
    label = _new_label(doc, label, "snapshot")
    allowed = m.fields_for_mode(sim)
    if str(field).strip().lower() not in [f.lower() for f in allowed]:
        from wavesim_gui.commands import is_electrostatic
        raise ApiError("Field {!r} cannot be recorded in {} mode; choose one "
                       "of: {}.".format(field, "electrostatic"
                                        if is_electrostatic(sim) else
                                        "full-wave (fdtd)", ", ".join(allowed)))
    field = c.enum_choice(field, allowed, "snapshot field")
    plane = _snapshot_plane(normal)
    axis = m._PLANE_OFFSET_AXIS[plane]
    if offset is not None:
        _check_quantities([offset], "length", "offset")
    if record_interval is not None and every_n_steps is not None:
        raise ApiError("Give either record_interval or every_n_steps, not "
                       "both.")
    if record_interval is not None:
        kind, value = c.parse_quantity(record_interval, "time")
        if kind == "value" and value <= 0:
            raise ApiError("record_interval must be > 0 (got {} s).".format(
                value))
    if every_n_steps is not None:
        if isinstance(every_n_steps, bool) or not isinstance(
                every_n_steps, (int, float)) or int(every_n_steps) != \
                every_n_steps or every_n_steps < 1:
            raise ApiError("every_n_steps must be an integer >= 1 (got "
                           "{!r}).".format(every_n_steps))

    with c.transaction(doc, "Wavesim: Add Snapshot"):
        snap = m.new_snapshot(sim)
        old_auto = m._snapshot_label(snap)
        snap.Field = field
        snap.Plane = plane
        if offset is None:
            centre = m._default_point_mm(sim)
            snap.Offset = "{} mm".format(getattr(centre, axis))
        else:
            c.set_quantity(snap, "Offset", offset, "length")
        dt = m.snapshot_dt_s(sim)
        if every_n_steps is not None:
            snap.EveryNSteps = int(every_n_steps)
            snap.RecordInterval = 0.0
        else:
            if record_interval is None:
                # What the panel pre-fills (EveryNSteps' worth of time) and
                # stores on OK.
                snap.RecordInterval = (int(snap.EveryNSteps) * dt
                                       if dt > 0.0 else 0.0)
            else:
                c.set_quantity(snap, "RecordInterval", record_interval, "time")
                doc.recompute()
            steps = m.steps_for_interval(float(snap.RecordInterval), dt)
            if steps > 0:
                snap.EveryNSteps = steps
        doc.recompute()
        labels_mod.retitle(snap, old_auto, m._snapshot_label(snap))
        _apply_label(snap, label)
    doc.recompute()
    domain_mod.notify_domain_inputs_changed(doc)
    return snap


# --------------------------------------------------------------------------- #
# Energy / dissipation (one each per simulation)
# --------------------------------------------------------------------------- #

def _add_region_monitor(sim, label, interior, full, kind):
    from wavesim_gui import labels as labels_mod

    m = _mon()
    sim = c.get_sim(sim)
    doc = sim.Document
    if kind == "energy":
        existing = m.find_energy_monitors(sim)
        create, auto = m.new_energy_monitor, m._energy_label
        title, what = "Wavesim: Add Energy Monitor", "an energy monitor"
    else:
        existing = m.find_dissipation_monitors(sim)
        create, auto = m.new_dissipation_monitor, m._dissipation_label
        title, what = "Wavesim: Add Dissipation Monitor", "a dissipation monitor"
    if existing:
        raise ApiError("The simulation already has {} ({!r}); there can be only "
                       "one.".format(what, existing[0].Label))
    if not interior and not full:
        raise ApiError("Record at least one volume: interior=True and/or "
                       "full=True.")
    label = _new_label(doc, label, "monitor")
    with c.transaction(doc, title):
        mon = create(sim)
        old_auto = auto(mon)
        mon.RecordInterior = bool(interior)
        mon.RecordFullDomain = bool(full)
        labels_mod.retitle(mon, old_auto, auto(mon))
        _apply_label(mon, label)
    doc.recompute()
    return mon


@api_function("monitor")
def add_energy_monitor(sim=None, *, label=None, interior=True, full=False):
    """Add the total-energy monitor and return it (*Add Energy Monitor*).

    Records the total electromagnetic energy every time step -- the stability
    / decay diagnostic of a full-wave run. One per simulation: raises if one
    exists. It has no location; only the summed volume is chosen.

    :param label: unique label; default the GUI's ``"Energy (excl. PML)"``
        (follows the volume choice).
    :param interior: sum the physical domain only, PML excluded (default True).
    :param full: sum the whole grid including the PML (default False). Both
        may be True (two series); at least one must be.
    :returns: the EnergyMonitor object.

    Example: ``add_energy_monitor(sim, interior=True, full=True)``
    """
    return _add_region_monitor(sim, label, interior, full, "energy")


@api_function("monitor")
def add_dissipation_monitor(sim=None, *, label=None, interior=True,
                            full=False):
    """Add the ohmic-dissipation monitor and return it (*Add Dissipation Monitor*).

    Records P(t) = sum(sigma |E|^2 dV), the power absorbed by lossy material
    (zero when no material has a conductivity). One per simulation: raises if
    one exists. Keep to the interior: the PML dissipates by design.

    :param label: unique label; default ``"Dissipation (excl. PML)"``.
    :param interior: sum the physical domain only (default True).
    :param full: sum the whole grid including the PML (default False). At
        least one of the two must be True.
    :returns: the DissipationMonitor object.

    Example: ``add_dissipation_monitor(sim)``
    """
    return _add_region_monitor(sim, label, interior, full, "dissipation")


# --------------------------------------------------------------------------- #
# Path objects for the curve monitors
# --------------------------------------------------------------------------- #

def _is_expr(value):
    return isinstance(value, str) and value.strip().startswith("=")


def _same_point(a, b):
    return all(not _is_expr(x) and not _is_expr(y) for x, y in zip(a, b)) and \
        (c.vector_mm(a) - c.vector_mm(b)).Length < 1e-9


def _check_path_args(path, circle, kind):
    """Validate path/circle input; returns ``"ref"``, ``"points"`` or ``"circle"``."""
    if (path is None) == (circle is None):
        if kind == "voltage":
            raise ApiError("Give the integration path: path=[[x, y, z], ...] "
                           "(an open polyline, >= 2 points) or the name of an "
                           "existing curve object (sketch / wire).")
        raise ApiError("Give exactly one of path=[[x, y, z], ...] (or the name "
                       "of an existing curve object) and circle={'center': "
                       "[x, y, z], 'radius': r, 'normal': 'z'}.")
    if circle is not None:
        if kind == "voltage":
            raise ApiError("A voltage path must be open; circle= is only for "
                           "current and field-along-curve monitors.")
        if not isinstance(circle, dict) or "center" not in circle \
                or "radius" not in circle:
            raise ApiError("circle needs {'center': [x, y, z], 'radius': r} "
                           "and optionally 'normal' (axis, default 'z').")
        unknown = set(circle) - {"center", "radius", "normal"}
        if unknown:
            raise ApiError("Unknown circle key(s): {}.".format(
                ", ".join(sorted(unknown))))
        _check_point(circle["center"], "circle center")
        kind_r, radius = c.parse_quantity(circle["radius"], "length")
        if kind_r == "value" and radius <= 0:
            raise ApiError("circle radius must be > 0 mm.")
        c.axis_vector(circle.get("normal", "z"), "circle normal")
        return "circle"
    if isinstance(path, str) or (hasattr(path, "Name")
                                 and hasattr(path, "Document")):
        return "ref"
    if not isinstance(path, (list, tuple)) or len(path) < 2:
        raise ApiError("path must be a list of >= 2 points [[x, y, z], ...] or "
                       "the name of a curve object.")
    for i, pt in enumerate(path):
        _check_point(pt, "path point {}".format(i + 1))
    for i in range(1, len(path)):
        if _same_point(path[i - 1], path[i]):
            raise ApiError("path points {} and {} coincide (zero-length "
                           "segment).".format(i, i + 1))
    closed = len(path) > 2 and _same_point(path[0], path[-1])
    if kind == "voltage" and closed:
        raise ApiError("A voltage path must be open (V = integral of E from "
                       "the first point to the last); its first and last "
                       "points coincide.")
    if kind == "current" and len(path) - (1 if closed else 0) < 3:
        raise ApiError("A current loop needs at least 3 distinct points (the "
                       "loop is closed automatically) or circle=.")
    if len(path) > 2 and any(_is_expr(v) for pt in path for v in pt):
        raise ApiError("Param expressions in a path are only supported for a "
                       "2-point path (a straight line); give numbers for "
                       "polylines.")
    return "points"


def _make_path_object(doc, path, circle, kind, mon_label):
    """Create the curve object for *path*/*circle* (see :func:`_check_path_args`).

    A 2-point path is a ``Part::Line`` (every coordinate bindable to a param
    expression), a longer one a ``Part::Polygon`` (closed for a current loop),
    a circle a ``Part::Circle`` -- a full circle, counter-clockwise about its
    normal. An existing object is returned as-is after checking it is a curve.
    """
    m = _mon()
    how = _check_path_args(path, circle, kind)
    if how == "ref":
        obj = c.find_object(doc, path, "curve object")
        if not m._is_curve_object(obj):
            raise ApiError("{!r} is not a curve object (it needs edges and no "
                           "solids, like a sketch or a wire).".format(obj.Label))
        return obj
    base = "{} Path".format(mon_label) if mon_label else {
        "voltage": "Voltage Path", "current": "Current Path",
        "field_line": "Field Line Path"}[kind]
    if how == "circle":
        obj = doc.addObject("Part::Circle", "MonitorPath")
        normal = c.axis_vector(circle.get("normal", "z"), "circle normal")
        center = circle["center"]
        base_vec = FreeCAD.Vector(*[0.0 if _is_expr(v) else
                                    c.quantity_value(v, "length")
                                    for v in center])
        obj.Placement = FreeCAD.Placement(
            base_vec, FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), normal))
        for comp, value in zip("xyz", center):
            if _is_expr(value):
                obj.setExpression(".Placement.Base." + comp,
                                  c.param_expression(value))
        c.set_quantity(obj, "Radius", circle["radius"], "length")
    elif len(path) == 2:
        obj = doc.addObject("Part::Line", "MonitorPath")
        for (prop_x, prop_y, prop_z), pt in zip(
                (("X1", "Y1", "Z1"), ("X2", "Y2", "Z2")), path):
            for prop, value in zip((prop_x, prop_y, prop_z), pt):
                c.set_quantity(obj, prop, value, "length")
    else:
        pts = [c.vector_mm(p, "path point") for p in path]
        closed = (pts[0] - pts[-1]).Length < 1e-9
        if closed:
            pts = pts[:-1]
        obj = doc.addObject("Part::Polygon", "MonitorPath")
        obj.Nodes = pts
        obj.Close = bool(closed or kind == "current")
    obj.Label = base
    doc.recompute()
    if not m._is_curve_object(obj):
        raise ApiError("Could not build a curve from the given path.")
    return obj


def _add_path_monitor(sim, label, path, circle, kind):
    m = _mon()
    sim = c.get_sim(sim)
    doc = sim.Document
    label = _new_label(doc, label, "monitor")
    _check_path_args(path, circle, kind)
    type_name, title = {
        "voltage": (m._VOLTAGE_TYPE, "Wavesim: Add Voltage Monitor"),
        "current": (m._CURRENT_TYPE, "Wavesim: Add Current Monitor"),
    }[kind]
    with c.transaction(doc, title):
        curve = _make_path_object(doc, path, circle, kind, label)
        mon = m.new_path_monitor(sim, type_name)
        doc.recompute()
        m.attach_path(mon, curve)
        _apply_label(mon, label)
    doc.recompute()
    return mon


@api_function("monitor")
def add_voltage_monitor(sim=None, *, label=None, path=None):
    """Add a voltage monitor V(t) = integral of E.dl and return it.

    GUI: *Add Voltage Monitor*, then drag an open sketch onto it. Here the
    path is given directly and attached the same way. V is integrated from the
    path's first point to its last (sign follows that direction) -- e.g.
    across a gap between two conductors.

    :param label: unique label; default the GUI's auto label
        ``"Voltage Monitor (<path label>)"``.
    :param path: either ``[[x, y, z], ...]`` (>= 2 points, open; lengths in
        mm by default; a 2-point path also accepts param expressions per
        coordinate) -- a ``Part::Line`` / ``Part::Polygon`` curve object
        labelled ``"<label> Path"`` is created -- or the name/label of an
        existing curve object (sketch, Draft wire, ...).
    :returns: the VoltageMonitor object (its ``Sketch`` links the path).

    Example: ``add_voltage_monitor(sim, path=[[0, 0, 0], [0, 0, "=SUB_H"]])``
    """
    return _add_path_monitor(sim, label, path, None, "voltage")


@api_function("monitor")
def add_current_monitor(sim=None, *, label=None, path=None, circle=None):
    """Add a current monitor I(t) = loop integral of H.dl and return it.

    GUI: *Add Current Monitor*, then drag a closed sketch onto it. Here the
    loop is given directly and attached the same way. I is positive by the
    right-hand rule along the loop direction -- e.g. a loop around a wire.

    :param label: unique label; default ``"Current Monitor (<path label>)"``.
    :param path: ``[[x, y, z], ...]`` (>= 3 distinct points, closed
        automatically; mm by default) -- creates a closed ``Part::Polygon`` --
        or the name/label of an existing curve object.
    :param circle: instead of *path*: ``{"center": [x, y, z], "radius": r,
        "normal": "z"}`` (lengths mm by default; center/radius accept param
        expressions; normal ``'x'|'y'|'z'`` or ``[x, y, z]``). Creates a
        ``Part::Circle`` running counter-clockwise about *normal*, so a current
        flowing along +normal reads positive.
    :returns: the CurrentMonitor object (its ``Sketch`` links the loop).

    Example: ``add_current_monitor(sim, circle={"center": [0, 0, 5],
    "radius": 2, "normal": "z"})``
    """
    return _add_path_monitor(sim, label, path, circle, "current")


# --------------------------------------------------------------------------- #
# Field along a curve (electrostatic)
# --------------------------------------------------------------------------- #

def _check_vertex_index(value, what, allow_end=False):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError("{} must be an integer vertex index.".format(what))
    if value < 0 and not (allow_end and value == -1):
        raise ApiError("{} must be >= 0{}.".format(
            what, " or -1 (the end of the curve)" if allow_end else ""))


@api_function("monitor")
def add_field_line_monitor(sim=None, *, label=None, path=None, circle=None,
                           field="E", start_vertex=0, end_vertex=-1,
                           reversed=False):
    """Add a field-along-curve monitor and return it (*Add Field Along Curve*).

    Electrostatic runs only (raises in full-wave mode, where the GUI command
    is greyed out). After the run, the chosen quantity is plotted against the
    distance travelled along the curve, from *start_vertex* to *end_vertex*.

    :param label: unique label; default ``"Field Along Curve (E, <path>)"``.
    :param path: ``[[x, y, z], ...]`` (>= 2 points; mm by default) -- creates
        a ``Part::Line`` / ``Part::Polygon`` (closed if the first and last
        points coincide) -- or the name/label of an existing curve object.
    :param circle: instead of *path*: ``{"center", "radius", "normal"}`` as in
        :func:`add_current_monitor` (a closed curve with one vertex).
    :param field: ``'phi'`` (potential), ``'E'`` (default) or ``'D'``.
    :param start_vertex: index (0-based, in the curve's own order) of the
        vertex at distance 0. A polyline's vertices are its points.
    :param end_vertex: index of the end vertex; ``-1`` (default) = the end of
        the curve (a full lap on a closed curve). Below *start_vertex* on an
        open curve the path runs backwards.
    :param reversed: closed curves only: go round against the curve direction.
    :returns: the FieldLineMonitor object.

    Example: ``add_field_line_monitor(sim, path=[[0, 0, 0], [10, 0, 0]],
    field="phi")``
    """
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import labels as labels_mod
    from wavesim_gui import visibility
    from wavesim_gui.commands import is_electrostatic

    m = _mon()
    sim = c.get_sim(sim)
    doc = sim.Document
    if not is_electrostatic(sim):
        raise ApiError("Field-along-curve monitors only record in an "
                       "electrostatic run; switch the simulation to "
                       "electrostatic mode first.")
    label = _new_label(doc, label, "monitor")
    field = c.enum_choice(field, m._ES_FIELDS, "field")
    _check_vertex_index(start_vertex, "start_vertex")
    _check_vertex_index(end_vertex, "end_vertex", allow_end=True)
    _check_path_args(path, circle, "field_line")

    with c.transaction(doc, "Wavesim: Add Field Along Curve"):
        curve = _make_path_object(doc, path, circle, "field_line", label)
        # The command's "curve already selected" path: linked before the label.
        mon = m.new_field_line_monitor(sim, curve)
        visibility.sync_from_children(mon)
        m._after_path_changed(doc)

        verts, closed = m.curve_vertices_mm(mon)
        n = len(verts)
        if start_vertex >= n:
            raise ApiError("start_vertex {} is out of range: the curve has {} "
                           "vertices (0..{}).".format(start_vertex, n, n - 1))
        if end_vertex >= n:
            raise ApiError("end_vertex {} is out of range: the curve has {} "
                           "vertices (0..{}, or -1 for its end).".format(
                               end_vertex, n, n - 1))
        if reversed and not closed:
            raise ApiError("reversed only applies to a closed curve.")
        end = end_vertex
        # Stored like the panel stores it: the curve's own end as -1.
        if (closed and end == start_vertex) or (not closed and end == n - 1):
            end = m._CURVE_END
        if not closed and start_vertex == (n - 1 if end == m._CURVE_END
                                           else end):
            raise ApiError("start_vertex and end_vertex are the same vertex of "
                           "an open curve: the path has no length.")

        old_auto = m._field_line_label(mon)
        mon.Field = field
        labels_mod.retitle(mon, old_auto, m._field_line_label(mon))
        mon.StartVertex = int(start_vertex)
        mon.EndVertex = int(end)
        mon.Reversed = bool(reversed)
        mon.touch()
        doc.recompute()
        _apply_label(mon, label)
    domain_mod.notify_domain_inputs_changed(doc)
    return mon


# --------------------------------------------------------------------------- #
# Re-pathing (the tree drag & drop)
# --------------------------------------------------------------------------- #

@api_function("monitor")
def set_monitor_path(sim=None, monitor=None, *, path=None, circle=None):
    """Give a voltage/current/field-along-curve monitor a new path.

    The equivalent of dropping a sketch onto the monitor in the tree: the
    curve is linked, an auto label is updated, a field-along-curve monitor's
    vertex range restarts (0 .. end), and the domain re-sizes. The previous
    path object is left in the document.

    :param monitor: the monitor (name or label).
    :param path: points or an existing curve object, as in
        :func:`add_voltage_monitor` / :func:`add_current_monitor`.
    :param circle: ``{"center", "radius", "normal"}`` (current and
        field-along-curve monitors only).
    :returns: the monitor.

    Example: ``set_monitor_path(sim, "Voltage Monitor (Gap)", path="Gap")``
    """
    m = _mon()
    sim = c.get_sim(sim)
    doc = sim.Document
    mon = c.find_object(doc, monitor, "monitor")
    if m.is_voltage_monitor(mon):
        kind = "voltage"
    elif m.is_current_monitor(mon):
        kind = "current"
    elif m.is_field_line_monitor(mon):
        kind = "field_line"
    else:
        raise ApiError("{!r} is not a voltage, current or field-along-curve "
                       "monitor.".format(mon.Label))
    _check_path_args(path, circle, kind)
    with c.transaction(doc, "Wavesim: Set Monitor Path"):
        curve = _make_path_object(doc, path, circle, kind, mon.Label)
        m.attach_path(mon, curve)
    return mon
