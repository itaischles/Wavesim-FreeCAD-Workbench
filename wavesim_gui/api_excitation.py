# -*- coding: utf-8 -*-
"""Scripting API: sources, ports, beams, potentials and the port matrix.

GUI-free counterparts of the *Add Point Source*, *Add Gaussian Beam*, *Add
Lumped Port*, *Add Modal Port* (and its legacy *Add TEM Source* id), *Add SPICE
Line Port*, *Add SPICE TEM Port*, *Set Potential* and *Port Matrix* commands.
See :mod:`wavesim_gui.api_common` for the argument conventions (``sim`` first,
references by name/label, quantities as number/"3 GHz"/"=PARAM" expressions,
one undo step per call).

Every ``add_*`` function does what the command plus an OK on its task panel
does: the object is created through the same module-level helper the command
uses (same defaults, same auto label, same group), the given settings are
written, and the panel's commit side effects follow -- auto-label retitle,
forced boundary conditions, domain re-sizing. Geometry the panel takes from
the 3D selection is given here as object references plus declarative
sub-shape selectors (:func:`wavesim_gui.api_common.select_subshapes`) or as
explicit points in mm -- never as raw ``Edge3``/``Face7`` indices.

Excitation dicts
----------------
Every driven object carries a temporal waveform, given as::

    {"type": "gaussian" | "sine" | "sinusoid" | "rect" | "gaussian_sine",
     ...parameters...}

``type`` also accepts the GUI labels ("Gaussian Pulse", "Sinusoid (ramped
CW)", ...) and ``rectangular``. Parameters (only those the type uses are
allowed; omitted ones keep their stored/default value):

=================  =========  ===============================================
key                unit       used by
=================  =========  ===============================================
``fmax``           Hz         gaussian (bandwidth), gaussian_sine (envelope)
``frequency``      Hz         sine, sinusoid, gaussian_sine (carrier)
``amplitude``      --         all (peak; a modal port: forward-wave volts)
``phase``          deg        sine, sinusoid, gaussian_sine
``ramp_cycles``    periods    sinusoid (raised-cosine turn-on, 0 = abrupt)
``start_time``     s          rect (delay before the ramp up)
``rise_time``      s          rect
``flat_time``      s          rect
``fall_time``      s          rect
=================  =========  ===============================================

Each value is a quantity (``"2.4 GHz"``, ``"0.1 ns"``, ``{"value": 90, "unit":
"deg"}``) or a param expression (``"=F0"``).

FreeCAD-side only.
"""

import contextlib
import math
import os
import re

import FreeCAD

from wavesim_gui import api_common as c
from wavesim_gui.api_common import ApiError, api_function

__all__ = [
    "add_source", "add_gaussian_beam", "add_lumped_port", "add_modal_port",
    "add_spice_line_port", "add_spice_tem_port", "set_excitation",
    "set_potential", "configure_port_matrix", "port_matrix_plan",
    "export_port_matrix_jobs", "load_port_matrix_results",
]

c.register_dimensions({"Resistance": "resistance", "Inductance": "inductance",
                       "Capacitance": "capacitance"}, "LumpedPort")
c.register_dimensions({"WavesimPotential": "voltage"})


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _require_fdtd(sim, what):
    """Refuse a full-wave-only object in an electrostatic simulation.

    Mirrors the commands' ``IsActive`` (``commands.fdtd_only_active``): the
    toolbar greys sources and ports out in electrostatic mode.
    """
    from wavesim_gui import commands

    if commands.is_electrostatic(sim):
        raise ApiError(
            "{} only applies to a full-wave (FDTD) simulation, and this one is "
            "electrostatic. Switch the solver mode to 'fdtd' first.".format(what))


def _new_label(doc, label, what):
    return None if label is None else c.check_new_label(doc, label, what)


@contextlib.contextmanager
def _creating(doc, title):
    """:func:`api_common.transaction` that also removes what it added on error.

    Aborting the transaction is enough in the GUI, but a document with undo
    disabled (``UndoMode`` 0, the FreeCADCmd default) keeps whatever was
    created before the failure; a failed ``add_*`` must leave no half-made
    object either way.
    """
    before = {o.Name for o in doc.Objects}
    try:
        with c.transaction(doc, title):
            yield doc
    except BaseException:
        for obj in reversed(list(doc.Objects)):
            if obj.Name not in before:
                try:
                    doc.removeObject(obj.Name)
                except Exception:
                    pass
        raise


def _finish_label(obj, old_auto, new_auto, label):
    """Apply an explicit *label*, or retitle the auto label like the panels."""
    from wavesim_gui import labels as labels_mod

    if label:
        c.set_label(obj, label)
    else:
        labels_mod.retitle(obj, old_auto, new_auto)


def _settle(doc):
    """Recompute and let the domain grow to the new inputs (panel OK)."""
    from wavesim_gui import domain as domain_mod

    doc.recompute()
    domain_mod.notify_domain_inputs_changed(doc)


def _check_range(value, dimension, lo, hi, what):
    """Parse a quantity and range-check it when it is a plain number."""
    kind, number = c.parse_quantity(value, dimension)
    if kind == "value" and not (lo <= number <= hi):
        raise ApiError("{} must be between {:g} and {:g} {} (got {:g}).".format(
            what, lo, hi, c.STORAGE_UNIT[dimension], number))
    return kind, number


def _bool(value, what):
    if not isinstance(value, bool):
        raise ApiError("{} must be true or false (got {!r}).".format(
            what, value))
    return value


# --------------------------------------------------------------------------- #
# Excitations
# --------------------------------------------------------------------------- #

def _exc():
    from wavesim_gui import excitation
    return excitation


_TYPE_ALIASES = {
    "gaussian": "gaussian", "gauss": "gaussian", "gaussian_pulse": "gaussian",
    "sine": "sine", "sin": "sine", "sine_wave": "sine",
    "sinusoid": "sinusoid", "cw": "sinusoid", "ramped_sine": "sinusoid",
    "rect": "rectangular", "rectangular": "rectangular",
    "rectangle": "rectangular", "rectangular_pulse": "rectangular",
    "gaussian_sine": "gaussian_sine", "gaussian+sine": "gaussian_sine",
    "modulated_gaussian": "gaussian_sine",
}

_PARAM_ALIASES = {
    "fmax": "fmax", "bandwidth": "fmax", "frequency": "frequency",
    "amplitude": "amplitude", "phase": "phase_deg", "phase_deg": "phase_deg",
    "ramp_cycles": "ramp_cycles", "start_time": "start_time",
    "rise_time": "rise_time", "flat_time": "flat_time",
    "fall_time": "fall_time",
}

_PARAM_DIMENSION = {
    "fmax": "frequency", "frequency": "frequency",
    "amplitude": "dimensionless", "phase_deg": "angle",
    "ramp_cycles": "dimensionless", "start_time": "time",
    "rise_time": "time", "flat_time": "time", "fall_time": "time",
}

# Plain-number bounds, matching the task panel's spin boxes (frequency must be
# positive; the panel's lower bound is 1e-9 of the display unit).
_PARAM_RANGE = {
    "fmax": (1.0e-9, float("inf")), "frequency": (1.0e-9, float("inf")),
    "amplitude": (-1.0e9, 1.0e9), "phase_deg": (-360.0, 360.0),
    "ramp_cycles": (0.0, 1.0e6), "start_time": (0.0, 1.0e12),
    "rise_time": (0.0, 1.0e12), "flat_time": (0.0, 1.0e12),
    "fall_time": (0.0, 1.0e12),
}

_PUBLIC_KEY = {"phase_deg": "phase"}


def _excitation_type(value):
    """Waveform type key for *value* (alias, key or GUI label)."""
    exc = _exc()
    text = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if text in _TYPE_ALIASES:
        return _TYPE_ALIASES[text]
    for typ, label in exc.LABELS.items():
        if str(value).strip().lower() == label.lower():
            return typ
    raise ApiError(
        "Unknown excitation type {!r}; use one of: gaussian, sine, sinusoid, "
        "rect, gaussian_sine (or a GUI label such as {!r}).".format(
            value, exc.LABELS[exc.GAUSSIAN]))


def _parse_excitation(excitation, current_type=None, what="excitation"):
    """Validate an excitation dict. Returns ``(type, [(key, value, dim)])``.

    *current_type* is used when the dict has no ``type`` (only parameters).
    Nothing is written; :func:`_write_excitation` applies the result.
    """
    exc = _exc()
    if excitation is None:
        return None
    if not isinstance(excitation, dict):
        raise ApiError("{} must be a dict like {{'type': 'gaussian', 'fmax': "
                       "'10 GHz'}}.".format(what))
    if "type" in excitation:
        typ = _excitation_type(excitation["type"])
    elif current_type is not None:
        typ = current_type
    else:
        typ = exc.GAUSSIAN
    used = exc.param_keys(typ)
    params = []
    for raw_key, value in excitation.items():
        if raw_key == "type":
            continue
        key = _PARAM_ALIASES.get(str(raw_key).strip().lower())
        if key is None:
            raise ApiError("Unknown excitation parameter {!r}. Allowed: {}."
                           .format(raw_key, ", ".join(sorted(
                               set(_PUBLIC_KEY.get(k, k)
                                   for k in _PARAM_ALIASES.values())))))
        if key not in used:
            raise ApiError(
                "Parameter {!r} does not apply to a {} excitation; it uses: "
                "{}.".format(raw_key, exc.LABELS[typ], ", ".join(
                    _PUBLIC_KEY.get(k, k) for k in used)))
        dim = _PARAM_DIMENSION[key]
        lo, hi = _PARAM_RANGE[key]
        _check_range(value, dim, lo, hi,
                     "Excitation {}".format(_PUBLIC_KEY.get(key, key)))
        params.append((key, value, dim))
    return typ, params


def _write_excitation(obj, parsed, index=0):
    """Write a :func:`_parse_excitation` result onto drive row *index*."""
    exc = _exc()
    if parsed is None:
        return
    typ, params = parsed
    exc.ensure_object_props(obj, index)
    setattr(obj, exc.excitation_prop(index), exc.LABELS[typ])
    for key, value, dim in params:
        c.set_quantity(obj, exc.prop_for_key(key, index), value, dim)
    exc.sync_visibility(obj, index)


def _current_type(obj, index=0):
    exc = _exc()
    return exc.type_for_label(str(getattr(
        obj, exc.excitation_prop(index), exc.EXCITATION_LABELS[0])))


# --------------------------------------------------------------------------- #
# Geometry references (selectors, never raw indices)
# --------------------------------------------------------------------------- #

_VERTEX_KEYS = {"type", "near_point", "at_x", "at_y", "at_z", "tolerance"}


def _placed_shape(obj):
    """*obj*'s shape in world coordinates (PartDesign features included)."""
    from wavesim_gui import lumped_port as lumped_mod

    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        raise ApiError("{!r} has no shape to select from.".format(obj.Label))
    extra = lumped_mod._container_offset(obj)
    if extra is not None:
        shape = shape.copy()
        shape.transformShape(extra.Matrix)
    return shape


def _select_vertices(shape, selector):
    if not isinstance(selector, dict) or not selector:
        raise ApiError("A vertex selector is a dict such as {'near_point': "
                       "[x, y, z]} or {'at_z': 1.6}.")
    unknown = set(selector) - _VERTEX_KEYS
    if unknown:
        raise ApiError("Unknown vertex selector key(s): {}. Allowed: {}".format(
            ", ".join(sorted(unknown)), ", ".join(sorted(_VERTEX_KEYS))))
    tol = c.quantity_value(selector.get("tolerance", 1e-4), "length")
    picked = [("Vertex{}".format(i + 1), v)
              for i, v in enumerate(shape.Vertexes)]
    for axis in "xyz":
        key = "at_" + axis
        if key in selector:
            value = c.quantity_value(selector[key], "length", what=key)
            picked = [(n, v) for n, v in picked
                      if abs(getattr(v.Point, axis) - value) <= tol]
    if "near_point" in selector and picked:
        point = c.vector_mm(selector["near_point"], "near_point")
        picked = [min(picked, key=lambda nv: (nv[1].Point - point).Length)]
    return picked


def _select_one(obj, kind, selector, what):
    """The single ``Vertex``/``Edge``/``Face`` name *selector* picks on *obj*."""
    shape = _placed_shape(obj)
    if kind == "vertex":
        picked = _select_vertices(shape, selector)
    else:
        picked = c.select_subshapes(shape, selector, kind)
    if len(picked) != 1:
        raise ApiError(
            "{}: the {} selector {} matched {} {}s of {!r}; it must match "
            "exactly one (add 'near_point': [x, y, z] to pick one).".format(
                what, kind, selector, len(picked), kind, obj.Label))
    return picked[0][0]


def _link_sub(doc, spec, what, kinds=("vertex", "edge", "face")):
    """``(object, [subname])`` from a reference spec.

    *spec* is an object reference (the whole object -- a sketch, a line, a
    one-face shape) or ``{"object": ref, "<kind>": selector}`` with one kind of
    *kinds*.
    """
    if isinstance(spec, dict):
        allowed = {"object"} | set(kinds)
        unknown = set(spec) - allowed
        if unknown:
            raise ApiError("{}: unknown key(s) {}. Use 'object' plus one of "
                           "{}.".format(what, ", ".join(sorted(unknown)),
                                        ", ".join(kinds)))
        obj = c.find_object(doc, spec.get("object"), what)
        given = [k for k in kinds if k in spec]
        if len(given) > 1:
            raise ApiError("{}: give only one of {}.".format(
                what, ", ".join(kinds)))
        if not given:
            return obj, []
        return obj, [_select_one(obj, given[0], spec[given[0]], what)]
    return c.find_object(doc, spec, what), []


def _make_line(doc, points, label):
    """A ``Part::Line`` between two explicit points (mm), for a port path."""
    if not isinstance(points, (list, tuple)) or len(points) != 2:
        raise ApiError("points needs two [x, y, z] points (mm), '+' end first.")
    for xyz in points:
        if not isinstance(xyz, (list, tuple)) or len(xyz) != 3:
            raise ApiError("Each point needs three coordinates [x, y, z].")
    parsed = [c.parse_quantity(v, "length") for xyz in points for v in xyz]
    if all(kind == "value" for kind, _n in parsed):
        nums = [n for _k, n in parsed]
        if (FreeCAD.Vector(*nums[:3]) - FreeCAD.Vector(*nums[3:])).Length \
                < 1e-9:
            raise ApiError("The two points coincide; a port line needs "
                           "length.")
    line = doc.addObject("Part::Line", "PortLine")
    for end, xyz in zip("12", points):
        for axis, value in zip("XYZ", xyz):
            c.set_quantity(line, axis + end, value, "length")
    line.Label = label
    if line.ViewObject is not None:
        line.ViewObject.Visibility = False
    line.recompute()
    return line


# --------------------------------------------------------------------------- #
# Point source
# --------------------------------------------------------------------------- #

@api_function("excitation")
def add_source(sim=None, *, label=None, component="Ez", position=None,
               excitation=None):
    """Add a soft point source and return it.

    Use for a simple point excitation (dipole-like) of one field component.
    Same object as *Wavesim -> Add Point Source* + OK in its panel.

    :param label: unique label; default is the auto label
        ``"Source (Ez, Gaussian Pulse @ 30 GHz)"``, kept in sync with the
        settings until renamed.
    :param component: field component driven: ``Ex``, ``Ey``, ``Ez``, ``Hx``,
        ``Hy`` or ``Hz``.
    :param position: ``[x, y, z]`` world position (mm by default; quantities and
        param expressions allowed), snapped to the nearest cell at run time.
        Default: the domain centre (or the geometry centre). Each coordinate
        must lie within +-1e6 mm.
    :param excitation: waveform dict (see module doc), e.g.
        ``{"type": "gaussian", "fmax": "20 GHz"}``. Default: Gaussian pulse,
        fmax 30 GHz, amplitude 1.
    :returns: the Source object.

    Example::

        add_source(component="Ez", position=[0, 0, "=H/2"],
                   excitation={"type": "sinusoid", "frequency": "5 GHz"})

    Electrostatic simulations refuse it (no time axis). The domain grows to
    include the source.
    """
    from wavesim_gui import source as source_mod

    sim = c.get_sim(sim)
    doc = sim.Document
    _require_fdtd(sim, "A point source")
    label = _new_label(doc, label, "source")
    comp = c.enum_choice(component, source_mod._COMPONENTS, "field component")
    if position is not None:
        if not isinstance(position, (list, tuple)) or len(position) != 3:
            raise ApiError("position needs three coordinates [x, y, z].")
        for value in position:
            _check_range(value, "length", -1.0e6, 1.0e6, "Source position")
    parsed = _parse_excitation(excitation)
    with _creating(doc, "Wavesim: Add Source"):
        src = source_mod.create_source(doc, sim)
        old_auto = source_mod.auto_label(src)
        src.Component = comp
        if position is not None:
            c.set_vector(src, "Position", position)
        _write_excitation(src, parsed)
        _finish_label(src, old_auto, source_mod.auto_label(src), label)
        _settle(doc)
    return src


# --------------------------------------------------------------------------- #
# Gaussian beam
# --------------------------------------------------------------------------- #

@api_function("excitation")
def add_gaussian_beam(sim=None, *, label=None, face="z0", angle=0.0, waist=0.0,
                      directional=True, excitation=None):
    """Add a Gaussian-beam boundary source on a domain face and return it.

    Use for a (near) plane-wave or beam illumination launched from one side of
    the domain into it. The launch face's boundary is forced to PML (as the
    panel's OK does). The launch is not amplitude-calibrated.

    :param label: unique label (default: auto, e.g.
        ``"Gaussian Beam (z0, 0°, w0 auto, Gaussian Pulse @ 30 GHz)"``).
    :param face: launch face ``x0``, ``x1``, ``y0``, ``y1``, ``z0`` or ``z1``
        (``x0`` = the low-x face, launching towards +x).
    :param angle: E polarization angle in deg (-360..360), measured from the
        face's first transverse axis towards its second: x0 (y->z), x1 (z->y),
        y0 (z->x), y1 (x->z), z0 (x->y), z1 (y->x).
    :param waist: w0, the 1/e E-amplitude radius at the launch plane, in mm
        (>= 0). 0 = auto (half the face's smaller interior half-width).
    :param directional: True = one-way launch (E + H sheets); False = bare E
        sheet radiating both ways.
    :param excitation: waveform dict (see module doc); a ramped
        ``{"type": "sinusoid", "frequency": ...}`` is a good CW choice.
    :returns: the Gaussian-beam object.

    Example::

        add_gaussian_beam(face="x0", angle=90, waist="20 mm",
                          excitation={"type": "sinusoid", "frequency": "10 GHz"})
    """
    from wavesim_gui import gaussian_beam as gb

    sim = c.get_sim(sim)
    doc = sim.Document
    _require_fdtd(sim, "A Gaussian beam")
    label = _new_label(doc, label, "Gaussian beam")
    face = c.enum_choice(face, gb._FACES, "launch face")
    _check_range(angle, "angle", -360.0, 360.0, "Polarization angle")
    _check_range(waist, "length", 0.0, 1.0e6, "Beam waist")
    directional = _bool(directional, "directional")
    parsed = _parse_excitation(excitation)
    with _creating(doc, "Wavesim: Add Gaussian Beam"):
        beam = gb.create_gaussian_beam(doc, sim)
        old_auto = gb.auto_label(beam)
        beam.Face = face
        c.set_quantity(beam, "Angle", angle, "angle")
        c.set_quantity(beam, "Waist", waist, "length")
        beam.Directional = directional
        _write_excitation(beam, parsed)
        _finish_label(beam, old_auto, gb.auto_label(beam), label)
        gb.force_launch_bc(beam)
        _settle(doc)
    return beam


# --------------------------------------------------------------------------- #
# Lumped port
# --------------------------------------------------------------------------- #

_DRIVE_ALIASES = {"none": 0, "passive": 0, "load": 0, "voltage": 1,
                  "thevenin": 1, "current": 2, "norton": 2}


def _drive_label(value):
    from wavesim_gui import lumped_port as lumped_mod

    key = str(value).strip().lower()
    if key in _DRIVE_ALIASES:
        return lumped_mod.DRIVE_LABELS[_DRIVE_ALIASES[key]]
    return c.enum_choice(value, lumped_mod.DRIVE_LABELS, "drive")


def _topology_label(value):
    from wavesim_gui import lumped_port as lumped_mod

    key = str(value).strip().lower()
    if key in lumped_mod._TOPOLOGY_FROM_TOKEN:
        return lumped_mod._TOPOLOGY_FROM_TOKEN[key]
    return c.enum_choice(value, lumped_mod.TOPOLOGY_LABELS, "topology")


@api_function("excitation")
def add_lumped_port(sim=None, *, label=None, plus=None, minus=None, points=None,
                    reverse=False, resistance=50.0, inductance=None,
                    capacitance=None, topology="series", drive="none",
                    excitation=None):
    """Add a lumped R/L/C port (optionally a voltage/current source) across a gap.

    Use for a two-terminal circuit element on a straight line between two
    conductors: a 50 ohm termination, a Thevenin/Norton feed, an L or C load.
    For a nonlinear or larger circuit use :func:`add_spice_line_port`.

    The line is given by **terminals** (what the panel takes from the 3D
    selection) or by two explicit points:

    * ``plus`` / ``minus`` -- each ``{"object": ref, "vertex"|"edge"|"face":
      selector}``. Two planar faces facing each other across the gap give a
      line normal to them from the '+' face's centre (pick the faces that face
      the gap, e.g. a trace's underside ``{"normal": "-z"}`` and the ground's
      top ``{"normal": "+z"}``). Two vertices are joined directly. A single
      edge (``plus`` only, with ``"edge": selector``) or a whole curve object
      (``plus="Sketch"``) carries both ends, '+' at its start.
    * ``points`` -- ``[[x, y, z], [x, y, z]]`` in mm ('+' end first): a hidden
      ``Part::Line`` helper is added to the document and used as the terminal.

    Selectors: ``{"normal": "+z"}``, ``{"at_z": 1.6}``, ``{"parallel_to": "x"}``,
    ``{"near_point": [x, y, z]}`` (combinable; must match exactly one).

    :param label: unique label (default auto, e.g.
        ``"Lumped Port (R 50 ohm)"``).
    :param reverse: swap '+' and '-'. Positive port current leaves '+'.
    :param resistance: ohm (> 0), or None for no R branch. Default 50.
    :param inductance: H (> 0, e.g. ``"2 nH"``), or None (default) for no L.
    :param capacitance: F (> 0, e.g. ``"0.5 pF"``), or None (default) for no C.
    :param topology: ``"series"`` or ``"parallel"`` wiring of several branches.
    :param drive: ``"none"`` (passive load), ``"voltage"`` (Thevenin: load in
        series with the EMF) or ``"current"`` (Norton: load in parallel).
    :param excitation: waveform dict for a voltage/current drive (refused with
        ``drive="none"``); amplitude is volts or amperes.
    :returns: the LumpedPort object.

    Example::

        add_lumped_port(plus={"object": "Trace", "face": {"normal": "-z"}},
                        minus={"object": "Ground", "face": {"normal": "+z"}},
                        resistance=50, drive="voltage",
                        excitation={"type": "gaussian", "fmax": "10 GHz"})
    """
    from wavesim_gui import lumped_port as lumped_mod

    sim = c.get_sim(sim)
    doc = sim.Document
    _require_fdtd(sim, "A lumped port")
    label = _new_label(doc, label, "lumped port")
    if points is not None and (plus is not None or minus is not None):
        raise ApiError("Give either points or plus/minus terminals, not both.")
    if points is None and plus is None:
        if minus is not None:
            raise ApiError("A lumped port with one terminal needs it as plus "
                           "(an edge or curve carrying both ends).")
        raise ApiError("A lumped port needs its line: give plus/minus "
                       "terminals or points=[[x, y, z], [x, y, z]].")
    reverse = _bool(reverse, "reverse")
    plus_link = _link_sub(doc, plus, "plus terminal") if plus is not None \
        else None
    minus_link = _link_sub(doc, minus, "minus terminal") if minus is not None \
        else None

    values = {}
    for prop, enable, _key, kind, name in lumped_mod._BRANCHES:
        value = {"R": resistance, "L": inductance, "C": capacitance}[kind]
        if value is None:
            values[prop] = None
            continue
        dim = {"R": "resistance", "L": "inductance", "C": "capacitance"}[kind]
        qkind, number = c.parse_quantity(value, dim)
        if qkind == "value" and number <= 0.0:
            raise ApiError("{} must be > 0 (got {:g}); pass None to leave the "
                           "branch out.".format(name, number))
        values[prop] = (value, dim)
    topology = _topology_label(topology)
    drive = _drive_label(drive)
    if drive == lumped_mod.DRIVE_NONE:
        if excitation is not None:
            raise ApiError("An excitation only applies with drive='voltage' or "
                           "'current'; this port is a passive load.")
        if all(v is None for v in values.values()):
            raise ApiError("The port has neither a load (R/L/C) nor a drive; "
                           "the run would skip it.")
    parsed = _parse_excitation(excitation)

    with _creating(doc, "Wavesim: Add Lumped Port"):
        if points is not None:
            line = _make_line(doc, points, "Lumped Port Line")
            plus_link = (line, [])
        port = lumped_mod.create_lumped_port(doc, sim, plus_link, minus_link)
        old_auto = lumped_mod.auto_label(port)
        port.ReversePolarity = reverse
        for prop, enable, _key, _kind, _name in lumped_mod._BRANCHES:
            setattr(port, enable, values[prop] is not None)
            if values[prop] is not None:
                c.set_quantity(port, prop, *values[prop])
        port.Topology = topology
        port.Drive = drive
        _write_excitation(port, parsed)
        lumped_mod.sync_drive_visibility(port)
        _finish_label(port, old_auto, lumped_mod.auto_label(port), label)
        doc.recompute()
        p0, _p1, warnings = lumped_mod.resolve_line(port)
        if p0 is None:
            raise ApiError("The terminals do not give a port line: {}.".format(
                "; ".join(warnings)))
        for text in warnings:
            FreeCAD.Console.PrintWarning(
                "Wavesim: lumped port '{}': {}.\n".format(port.Label, text))
        _settle(doc)
    return port


# --------------------------------------------------------------------------- #
# Modal port (and the TEM source / SPICE TEM drive it replaced)
# --------------------------------------------------------------------------- #

_FIELDS_ALIASES = {"eh": "EH", "e": "E", "directional": "EH",
                   "bidirectional": "E"}


def _fields_token(value):
    from wavesim_gui import modal_port as modal_mod

    key = str(value).strip().lower()
    if key in _FIELDS_ALIASES:
        return _FIELDS_ALIASES[key]
    return modal_mod._FIELDS_TOKEN[c.enum_choice(
        value, modal_mod._FIELDS_LABELS, "fields")]


def _drive_mode(value):
    from wavesim_gui import modal_port as modal_mod

    key = str(value).strip().lower()
    if key in ("waveform", "modal", "wave"):
        return modal_mod.MODE_WAVEFORM
    if key in ("spice", "spice_tem", "circuit"):
        return modal_mod.MODE_SPICE
    label = c.enum_choice(value, list(modal_mod._MODE_FROM_LABEL),
                          "drive_mode")
    return modal_mod._MODE_FROM_LABEL[label]


def _conductor_index(value):
    if isinstance(value, bool) or not isinstance(value, int) \
            or not 0 <= value <= 999:
        raise ApiError("conductor_index must be an integer 0..999 (0 = the "
                       "dominant mode).")
    return value


def _bounds_link(doc, bounds):
    if bounds is None:
        return None
    return _link_sub(doc, bounds, "bounds", kinds=("edge", "face"))


def _spice_settings(netlist, node_plus, node_minus, use_initial_conditions,
                    invert_port_current):
    if not isinstance(netlist, str) or not netlist.strip():
        raise ApiError("A SPICE drive needs netlist='path/to/circuit.cir' (an "
                       "ngspice netlist containing both port nodes).")
    for name, node in (("node_plus", node_plus), ("node_minus", node_minus)):
        if not isinstance(node, str) or not node.strip():
            raise ApiError("{} must be a netlist node name.".format(name))
    return {"Netlist": netlist.strip(), "NodePlus": node_plus.strip(),
            "NodeMinus": node_minus.strip(),
            "UseInitialConditions": _bool(use_initial_conditions,
                                          "use_initial_conditions"),
            "InvertPortCurrent": _bool(invert_port_current,
                                       "invert_port_current")}


def _resolve_conductor_rows(sim, face, bounds_sel, conductors):
    """``[(body, sub, energized, excitation_dict)]`` for the table.

    ``None`` scans the port plane like the panel does for a fresh port.
    """
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import materials as materials_mod
    from wavesim_gui import modal_port as modal_mod

    doc = sim.Document
    if conductors is None:
        try:
            signal, _ref, _scan = modal_mod.scan_port_plane(sim, face,
                                                            bounds_sel)
        except Exception as err:
            raise ApiError("Could not section the {} port plane: {}".format(
                face, err))
        return [(r["body"], r["sub"], r["energized"], None)
                for r in modal_mod.default_drive_rows(signal)]
    if not isinstance(conductors, (list, tuple)):
        raise ApiError("conductors must be a list of body references or "
                       "{'body': ..., 'face': selector, 'drive': bool, "
                       "'excitation': {...}} dicts.")
    dom = domain_mod.find_domain(sim)
    pec = {id(b) for b, _n, _v in materials_mod.conductors(sim)}
    rows, known, points = [], set(), set()
    for item in conductors:
        if isinstance(item, dict):
            unknown = set(item) - {"body", "face", "drive", "excitation"}
            if unknown:
                raise ApiError("Unknown conductor key(s): {}. Allowed: body, "
                               "face, drive, excitation.".format(
                                   ", ".join(sorted(unknown))))
            body = c.find_object(doc, item.get("body"), "conductor body")
            sub = _select_one(body, "face", item["face"], "conductor face") \
                if item.get("face") is not None else ""
            energized = _bool(item.get("drive", True), "drive")
            row_exc = item.get("excitation")
        else:
            body, sub, energized, row_exc = (
                c.find_object(doc, item, "conductor body"), "", True, None)
        if id(body) not in pec:
            raise ApiError("{!r} is not assigned to a PEC material; only PEC "
                           "bodies are port conductors.".format(body.Label))
        region = modal_mod._region_on_plane(body, dom, face, sub)
        if region is None:
            raise ApiError("Conductor {!r} does not reach the {} port plane."
                           .format(modal_mod.conductor_label(body, sub), face))
        keep = modal_mod.storage_sub(region)
        if (id(body), keep) in known or region["point"] in points:
            raise ApiError("Conductor {!r} is listed twice (the same cross-"
                           "section region).".format(
                               modal_mod.region_name(body, keep,
                                                     region["count"])))
        known.add((id(body), keep))
        points.add(region["point"])
        rows.append((body, keep, energized, row_exc))
    return rows


@api_function("excitation")
def add_modal_port(sim=None, *, label=None, face="z0", drive_mode="waveform",
                   conductors=None, excitation=None, bounds=None,
                   conductor_index=0, fields="EH", netlist=None,
                   node_plus="port1p", node_minus="0",
                   use_initial_conditions=False, invert_port_current=False):
    """Add a modal (TEM) waveguide port on a domain face and return it.

    Use to feed and terminate a transmission line (coax, stripline,
    microstrip...) whose PEC cross-section cuts a domain face. This is also
    the old "TEM Source" and, with ``drive_mode="spice"``, the SPICE-driven
    TEM port. Same object as *Wavesim -> Add Modal Port* + OK in its panel.

    With the **waveform** drive the face becomes the port itself (no PML, no
    PEC, no background gap) and every listed conductor gets an impedance
    sheet: driven rows launch their mode, undriven rows only absorb it. The
    **SPICE** drive is a lumped launch on an interior plane coupled to an
    ngspice netlist; its face is forced to PML.

    :param label: unique label (default auto, e.g. ``"Modal Port (z0, Pin,
        Gaussian Pulse @ 30 GHz)"``).
    :param face: ``x0``, ``x1``, ``y0``, ``y1``, ``z0`` or ``z1``.
    :param drive_mode: ``"waveform"`` (default) or ``"spice"``.
    :param conductors: the conductor table, one row per PEC conductor the port
        terminates. ``None`` (default) = what the panel proposes: every signal
        conductor the plane cuts (the grounded shield touching the solve
        region's edge excluded), largest first, only the first driven. ``[]``
        = no table (whole face, solver's dominant mode / ``conductor_index``).
        Items: a body reference (label/name), or ``{"body": ref, "face":
        selector, "drive": bool, "excitation": {...}}`` where ``face`` picks
        one face of the body that the plane crosses (needed only when one body
        holds several conductors) and ``drive`` (default True) energizes it.
    :param excitation: waveform dict applied to every row (rows' own
        ``excitation`` override it); amplitude = launched forward-wave volts.
        Refused with the SPICE drive.
    :param bounds: optional ``{"object": ref, "edge"|"face": selector}`` whose
        in-plane bounding box confines the mode solve to a sub-rectangle.
    :param conductor_index: legacy solver conductor label (0 = dominant mode),
        used with no table and by the SPICE drive.
    :param fields: SPICE drive only: ``"EH"`` (directional) or ``"E"``.
    :param netlist: SPICE drive: path to the ngspice netlist (required).
    :param node_plus: SPICE drive: netlist node on '+' (default ``port1p``).
    :param node_minus: SPICE drive: netlist node on '-' (default ``0``).
    :param use_initial_conditions: SPICE drive: ``.tran ... uic``.
    :param invert_port_current: SPICE drive: flip the port current sign.
    :returns: the ModalPort object.

    Example::

        add_modal_port(face="z0", conductors=["Pin"],
                       excitation={"type": "gaussian", "fmax": "8 GHz"})
    """
    from wavesim_gui import modal_port as modal_mod

    sim = c.get_sim(sim)
    doc = sim.Document
    _require_fdtd(sim, "A modal port")
    label = _new_label(doc, label, "modal port")
    face = c.enum_choice(face, modal_mod._FACES, "port face")
    mode = _drive_mode(drive_mode)
    conductor_index = _conductor_index(conductor_index)
    fields = _fields_token(fields)
    bounds_sel = _bounds_link(doc, bounds)
    spice = None
    if mode == modal_mod.MODE_SPICE:
        if excitation is not None:
            raise ApiError("A SPICE-driven port takes its drive from the "
                           "netlist; drop excitation.")
        spice = _spice_settings(netlist, node_plus, node_minus,
                                use_initial_conditions, invert_port_current)
    elif netlist is not None:
        raise ApiError("netlist only applies with drive_mode='spice'.")
    parsed = _parse_excitation(excitation)
    if conductors is not None and not isinstance(conductors, (list, tuple)):
        raise ApiError("conductors must be a list of body references or "
                       "{'body': ..., 'face': selector, 'drive': bool, "
                       "'excitation': {...}} dicts.")

    with _creating(doc, "Wavesim: Add Modal Port"):
        port = modal_mod.create_modal_port(doc, sim)
        old_auto = modal_mod.auto_label(port)
        port.Face = face
        port.BoundsSel = bounds_sel
        # As after the command's own commit: the face now carries no
        # background gap, so the plane is where the panel scans it.
        _settle(doc)
        rows = _resolve_conductor_rows(sim, face, bounds_sel, conductors)
        row_parsed = []
        for body, sub, _energized, row_exc in rows:
            if row_exc is not None and spice is not None:
                raise ApiError("A SPICE-driven port takes its drive from the "
                               "netlist; drop the conductors' excitation.")
            row_parsed.append(_parse_excitation(
                row_exc, None if parsed is None else parsed[0],
                "excitation of conductor {!r}".format(
                    modal_mod.conductor_label(body, sub))))
        if rows:
            modal_mod.set_drive_rows(port, [(r[0], r[1]) for r in rows],
                                     [r[2] for r in rows])
        else:
            modal_mod.set_drive_rows(port, [], [])
        if not rows or mode == modal_mod.MODE_SPICE:
            # The legacy label is what a tableless port and the SPICE drive
            # read; with a waveform table the rows pick their own modes.
            port.Conductor = conductor_index
        for index in range(max(len(rows), 1)):
            _write_excitation(port, parsed, index)
            if index < len(row_parsed):
                _write_excitation(port, row_parsed[index], index)
        port.Fields = fields
        port.ExcitationMode = mode
        for prop, value in (spice or {}).items():
            setattr(port, prop, value)
        modal_mod._sync_mode_visibility(port)
        _finish_label(port, old_auto, modal_mod.auto_label(port), label)
        if mode == modal_mod.MODE_SPICE:
            modal_mod.force_spice_bc(port)
        _settle(doc)
    return port


# --------------------------------------------------------------------------- #
# Legacy SPICE ports
# --------------------------------------------------------------------------- #

def _is_curve(obj):
    shape = getattr(obj, "Shape", None)
    return shape is not None and not shape.isNull() \
        and bool(shape.Edges) and not shape.Solids


@api_function("excitation")
def add_spice_line_port(sim=None, *, label=None, curve=None, points=None,
                        netlist=None, node_plus="port1p", node_minus="0",
                        use_initial_conditions=False,
                        invert_port_current=False):
    """Add a straight lumped port coupled to an ngspice netlist (co-simulation).

    Use when the circuit across a gap is nonlinear, active or larger than an
    R/L/C (otherwise prefer :func:`add_lumped_port`). The port line is the
    linked curve's chord, '+' at its start.

    :param label: unique label (default auto ``"SPICE Line Port (<netlist>,
        port1p/0)"``).
    :param curve: a curve object (sketch, ``Part::Line``, wire...) giving the
        line -- what the panel's "Port curve" picks.
    :param points: alternatively ``[[x, y, z], [x, y, z]]`` (mm, '+' first): a
        hidden ``Part::Line`` helper is added and linked.
    :param netlist: path to the ngspice netlist (required). The two nodes must
        exist in it with a DC path to ground ``0``; place no port component.
    :param node_plus: node on the '+' terminal (default ``port1p``).
    :param node_minus: node on the '-' terminal (default ``0``).
    :param use_initial_conditions: skip the DC operating point (``uic``).
    :param invert_port_current: flip the coupling sign.
    :returns: the SpiceLinePort object.

    A document with SPICE ports cannot be swept by the port matrix.
    """
    from wavesim_gui import spice_port as spice_mod

    sim = c.get_sim(sim)
    doc = sim.Document
    _require_fdtd(sim, "A SPICE port")
    label = _new_label(doc, label, "SPICE port")
    if (curve is None) == (points is None):
        raise ApiError("Give exactly one of curve (a sketch/line object) or "
                       "points=[[x, y, z], [x, y, z]].")
    curve_obj = None
    if curve is not None:
        curve_obj = c.find_object(doc, curve, "port curve")
        if not _is_curve(curve_obj):
            raise ApiError("{!r} is not a curve (sketch, line or wire without "
                           "solids).".format(curve_obj.Label))
    settings = _spice_settings(netlist, node_plus, node_minus,
                               use_initial_conditions, invert_port_current)
    with _creating(doc, "Wavesim: Add SPICE Line Port"):
        if points is not None:
            curve_obj = _make_line(doc, points, "SPICE Port Line")
        port = spice_mod.create_spice_line_port(doc, sim)
        old_auto = spice_mod.line_auto_label(port)
        port.Sketch = curve_obj
        for prop, value in settings.items():
            setattr(port, prop, value)
        _finish_label(port, old_auto, spice_mod.line_auto_label(port), label)
        _settle(doc)
    return port


@api_function("excitation")
def add_spice_tem_port(sim=None, *, label=None, face="z0", fields="EH",
                       conductor_index=0, bounds=None, netlist=None,
                       node_plus="port1p", node_minus="0",
                       use_initial_conditions=False,
                       invert_port_current=False):
    """Add a legacy SPICE TEM port (a TEM plane driven by an ngspice netlist).

    Kept for the still-registered *Add SPICE TEM Port* command; new work
    should use ``add_modal_port(drive_mode="spice", ...)``, which is the same
    drive. The launch face is forced to PML.

    :param face: ``x0``..``z1`` launch face.
    :param fields: ``"EH"`` (directional) or ``"E"`` (bidirectional).
    :param conductor_index: solver conductor label to drive (0 = dominant).
    :param bounds: optional ``{"object": ref, "edge"|"face": selector}``
        confining the mode solve to its in-plane bounding box.
    :param netlist: ngspice netlist path (required); see
        :func:`add_spice_line_port` for the node rules.
    :returns: the SpiceTEMPort object.
    """
    from wavesim_gui import spice_port as spice_mod

    sim = c.get_sim(sim)
    doc = sim.Document
    _require_fdtd(sim, "A SPICE port")
    label = _new_label(doc, label, "SPICE port")
    face = c.enum_choice(face, spice_mod._FACES, "launch face")
    fields = _fields_token(fields)
    conductor_index = _conductor_index(conductor_index)
    bounds_sel = _bounds_link(doc, bounds)
    settings = _spice_settings(netlist, node_plus, node_minus,
                               use_initial_conditions, invert_port_current)
    with _creating(doc, "Wavesim: Add SPICE TEM Port"):
        port = spice_mod.create_spice_tem_port(doc, sim)
        old_auto = spice_mod.tem_auto_label(port)
        port.Face = face
        port.BoundsSel = bounds_sel
        port.Fields = fields
        port.Conductor = conductor_index
        for prop, value in settings.items():
            setattr(port, prop, value)
        _finish_label(port, old_auto, spice_mod.tem_auto_label(port), label)
        spice_mod.force_tem_bc(port)
        _settle(doc)
    return port


# --------------------------------------------------------------------------- #
# Waveform of an existing object
# --------------------------------------------------------------------------- #

def _driven_kind(obj):
    """``(auto_label_fn, sync_fn, max_rows)`` for a waveform-driven object."""
    from wavesim_gui import gaussian_beam as gb
    from wavesim_gui import lumped_port as lumped_mod
    from wavesim_gui import modal_port as modal_mod
    from wavesim_gui import source as source_mod

    if source_mod.is_source(obj):
        return source_mod.auto_label, None, 1
    if gb.is_gaussian_beam(obj):
        return gb.auto_label, None, 1
    if lumped_mod.is_lumped_port(obj):
        return lumped_mod.auto_label, lumped_mod.sync_drive_visibility, 1
    if modal_mod.is_modal_port(obj):
        return (modal_mod.auto_label, modal_mod._sync_mode_visibility,
                max(modal_mod.drive_row_count(obj), 1))
    raise ApiError("{!r} carries no excitation waveform (expected a point "
                   "source, Gaussian beam, lumped port or modal port; SPICE "
                   "ports are driven by their netlist).".format(obj.Label))


@api_function("excitation")
def set_excitation(sim=None, *, obj=None, excitation=None, index=0):
    """Change the temporal waveform of a source, beam or port.

    Use to retune an existing excitation (frequency, bandwidth, amplitude,
    waveform family) without recreating the object. Parameters left out keep
    their current values; ``type`` may be omitted to keep the current family.

    :param obj: the Source / Gaussian beam / LumpedPort / ModalPort (name or
        label).
    :param excitation: waveform dict (see module doc), e.g.
        ``{"type": "gaussian_sine", "frequency": "5 GHz", "fmax": "2 GHz"}``.
    :param index: drive row for a multi-conductor modal port (0 = first row,
        in the order of its ``Conductors`` table); 0 for everything else.
    :returns: the modified object.

    A lumped port with ``drive="none"`` stores the waveform but does not use
    it until it gets a drive. The auto label follows the change.
    """
    sim = c.get_sim(sim)
    doc = sim.Document
    target = c.find_object(doc, obj, "driven object")
    auto_fn, sync_fn, rows = _driven_kind(target)
    if isinstance(index, bool) or not isinstance(index, int) \
            or not 0 <= index < rows:
        raise ApiError("index must be an integer 0..{} for {!r}.".format(
            rows - 1, target.Label))
    if not excitation:
        raise ApiError("excitation must be a non-empty dict.")
    parsed = _parse_excitation(excitation, _current_type(target, index))
    with c.transaction(doc, "Wavesim: Set Excitation"):
        old_auto = auto_fn(target)
        _write_excitation(target, parsed, index)
        if sync_fn is not None:
            sync_fn(target)
        _finish_label(target, old_auto, auto_fn(target), None)
        _settle(doc)
    return target


# --------------------------------------------------------------------------- #
# Electrostatic conductor conditions
# --------------------------------------------------------------------------- #

_CHARGE_UNITS = {"C": 1.0, "mC": 1e-3, "uC": 1e-6, "nC": 1e-9, "pC": 1e-12,
                 "fC": 1e-15}
_CHARGE_RE = re.compile(
    r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([A-Za-z]*)\s*$")


def _charge_value(value):
    """Coulombs from a number (C) / ``"5 pC"`` / ``{"value", "unit"}``."""
    if isinstance(value, bool):
        raise ApiError("charge must be a number of coulombs.")
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ApiError("charge must be finite.")
        return float(value)
    if isinstance(value, dict) and "value" in value:
        value = "{} {}".format(value["value"], value.get("unit", "C"))
    if isinstance(value, str):
        match = _CHARGE_RE.match(value)
        if match:
            number, unit = match.groups()
            unit = unit or "C"
            if unit in _CHARGE_UNITS:
                return float(number) * _CHARGE_UNITS[unit]
    raise ApiError("Cannot read {!r} as a charge; write e.g. 0, '5 pC' or "
                   "'1e-12 C' (units: {}).".format(
                       value, ", ".join(_CHARGE_UNITS)))


def _conductor_mode(value):
    from wavesim_gui import materials as materials_mod

    key = str(value).strip().lower()
    table = {"potential": materials_mod.MODE_POTENTIAL,
             "driven": materials_mod.MODE_POTENTIAL,
             "floating": materials_mod.MODE_FLOATING,
             "float": materials_mod.MODE_FLOATING,
             "default": materials_mod.MODE_DEFAULT,
             "unset": materials_mod.MODE_DEFAULT}
    if key in table:
        return table[key]
    raise ApiError("Invalid conductor mode {!r}; use 'potential', 'floating' "
                   "or 'default'.".format(value))


def _unset_mode(value):
    from wavesim_gui import commands

    key = str(value).strip().lower()
    if key in ("ground", "grounded", "0 v", "grounded (0 v)"):
        return commands.UNSET_GROUND
    if key in ("float", "floating", "0 c", "floating (0 c)"):
        return commands.UNSET_FLOAT
    raise ApiError("Invalid unset_default {!r}; use 'ground' (0 V) or "
                   "'float' (0 C).".format(value))


@api_function("electrostatics")
def set_potential(sim=None, *, body=None, mode="potential", volts=None,
                  charge=None, unset_default=None):
    """Set an electrostatic conductor condition on a PEC body.

    Use in an electrostatic simulation to hold a conductor at a potential, or
    let it float at a fixed charge. Same effect as the *Set Potential* panel's
    row for that body (plus its "Conductors nobody set" combo).

    A body on ``default`` takes the condition of any conductor it touches,
    else the document fallback (*unset_default*). Touching bodies given two
    different conditions are a conflict the solver refuses; the result lists
    any conflict found.

    :param body: the PEC body (name or label); it must be assigned to a PEC
        material.
    :param mode: ``"potential"`` (held at *volts*), ``"floating"`` (holds
        *charge*, its potential is solved for) or ``"default"``.
    :param volts: potential in V (quantity or ``"=PARAM"``); None keeps the
        stored value. Stored whatever the mode, so switching back restores it.
    :param charge: floating charge in coulombs (number) or ``"5 pC"``; None
        keeps the stored value.
    :param unset_default: optional document fallback for bodies nobody set:
        ``"ground"`` (0 V) or ``"float"`` (0 C).
    :returns: ``{"body", "mode", "volts", "charge_C", "held_at", "origin",
        "unset_default", "conflicts"}`` -- the resolved condition.

    Example::

        set_potential(body="Electrode", mode="potential", volts="5 V")
    """
    from wavesim_gui import commands
    from wavesim_gui import materials as materials_mod
    from wavesim_gui import potentials as pot_mod

    sim = c.get_sim(sim)
    doc = sim.Document
    if not commands.is_electrostatic(sim):
        raise ApiError("Conductor potentials only apply to an electrostatic "
                       "simulation; switch the solver mode to "
                       "'electrostatic' first.")
    target = c.find_object(doc, body, "PEC body")
    if target not in [b for b, _n, _v in materials_mod.conductors(sim)]:
        raise ApiError("{!r} is not assigned to a PEC material; only PEC "
                       "bodies carry a potential.".format(target.Label))
    cond_mode = _conductor_mode(mode)
    volts_kind, volts_value = (None, None) if volts is None \
        else c.parse_quantity(volts, "voltage")
    charge_value = None if charge is None else _charge_value(charge)
    unset = None if unset_default is None else _unset_mode(unset_default)
    with c.transaction(doc, "Wavesim: Conductor Potentials"):
        pot_mod.write_conditions(
            sim, [(target, cond_mode,
                   volts_value if volts_kind == "value" else None,
                   charge_value)], unset)
        if volts_kind == "expr":
            c.set_quantity(target, materials_mod.POTENTIAL_PROP, volts,
                           "voltage")
        doc.recompute()
    entries, groups = pot_mod.resolve(sim)
    entry = next(e for e in entries if e["body"] is target)
    return {
        "body": target.Label,
        "mode": materials_mod.conductor_mode(target),
        "volts": materials_mod.body_potential(target),
        "charge_C": materials_mod.body_charge(target),
        "held_at": pot_mod.describe(entry),
        "origin": entry["origin"],
        "unset_default": commands.unset_conductor_mode(sim),
        "conflicts": [pot_mod.describe_conflict(g["conflict"])
                      for g in groups if g["conflict"]],
    }


# --------------------------------------------------------------------------- #
# Port matrix
# --------------------------------------------------------------------------- #

def _port_json(port):
    return {"name": port.name, "kind": port.kind_label,
            "family": port.family, "index": port.index}


def port_matrix_plan(sim=None, ports=None):
    """Validate a port-matrix selection; the plan :func:`configure_port_matrix`
    returns (see there). Not a registered call of its own."""
    from wavesim_gui import portmatrix as pm

    sim = c.get_sim(sim)
    doc = sim.Document
    _require_fdtd(sim, "A port matrix")
    spice = pm.document_spice_ports(sim)
    if spice:
        raise ApiError("{} (SPICE ports: {}.)".format(
            pm._SPICE_REFUSAL.replace("\n\n", " "), ", ".join(spice)))
    spec = pm.document_spec(sim)
    available = pm.spec_ports(spec)
    if len(available) < 2:
        raise ApiError(
            "A port matrix needs at least two ports that record their own "
            "V(t) and I(t). This document has {}. Add modal ports or lumped "
            "ports first.".format(len(available)))
    if ports is None:
        chosen = list(available)
    else:
        if isinstance(ports, str) or not isinstance(ports, (list, tuple)):
            raise ApiError("ports must be a list of port names.")
        by_name = {p.name: p for p in available}
        chosen = []
        for name in ports:
            matches = [by_name[name]] if name in by_name else [
                p for p in available
                if p.name.startswith("{} — ".format(name))]
            if not matches:
                raise ApiError("No matrix port named {!r}. Available: {}."
                               .format(name, ", ".join(by_name)))
            for port in matches:
                if port in chosen:
                    raise ApiError("Port {!r} is selected twice.".format(
                        port.name))
                chosen.append(port)
        if len(chosen) < 2:
            raise ApiError("A port matrix needs at least two ports; {} "
                           "selected.".format(len(chosen)))
    try:
        excitation = pm.common_excitation(spec, chosen)
    except pm.SweepError as err:
        raise ApiError(str(err))
    n = len(chosen)
    return {
        "ports": [_port_json(p) for p in chosen],
        "available": [_port_json(p) for p in available],
        "runs": n,
        "matrix": "{n}x{n}".format(n=n),
        "excitation": excitation,
        "sweep_dir": pm.sweep_dir_for(doc),
        "notes": [
            "One solver run per selected port: that port is driven with the "
            "common excitation, every other source in the document is "
            "silenced; unselected ports stay in their terminations.",
            "The matrix is reported at the port planes (no de-embedding).",
        ],
    }


@api_function("port_matrix")
def configure_port_matrix(sim=None, *, ports=None):
    """Validate a port-matrix (Z/Y) selection and return the sweep plan.

    Use before :func:`export_port_matrix_jobs` to see which ports a matrix can
    span and what the sweep will do. Nothing is stored on the document (the
    Port Matrix dialog keeps no state either).

    :param ports: list of port names to span (as listed in the plan's
        ``available``: a lumped port's label, a modal port's label or
        ``"<label> — <conductor>"`` for one row of a multi-conductor port; a
        modal port's label selects all its rows). None = every port (the
        dialog's default). At least two.
    :returns: ``{"ports": [{name, kind, family, index}], "available": [...],
        "runs": N, "matrix": "NxN", "excitation": {...}, "sweep_dir", "notes"}``.
        ``index`` is provisional (rebound to the real job at export).

    Refused in electrostatic mode, with fewer than two ports, with SPICE ports
    in the document, or when no selected port carries a waveform.
    """
    return port_matrix_plan(sim, ports)


@api_function("port_matrix")
def export_port_matrix_jobs(sim=None, *, ports=None, root_dir=None, steps=None,
                            progress=None):
    """Voxelise once and write one solver job per port-matrix drive.

    Use to run a port matrix without the modal GUI flow: each
    ``<root_dir>/drive_<k>`` directory gets a ``job.json`` (port *k* driven
    with the common excitation, every other source silenced) and the shared
    ``materials.npz``, ready for the runner. Run them all, then call
    :func:`load_port_matrix_results`.

    :param ports: port names (see :func:`configure_port_matrix`); None = all.
    :param root_dir: output directory (absolute path). Default: the
        document's ``matrix`` results folder, as the GUI uses. Existing
        ``drive_*`` folders and job artefacts there are deleted.
    :param steps: optional step-count override (int > 0); default the job's.
    :param progress: optional Python callback for the voxeliser (not over the
        bridge); return True from it to cancel.
    :returns: ``{"root_dir", "steps", "ports": [{name, kind, family, index}],
        "drives": [{"index", "port", "workdir", "job"}], "excitation"}``.
    """
    from wavesim_gui import portmatrix as pm
    from wavesim_gui import voxelize as vox_mod

    sim = c.get_sim(sim)
    doc = sim.Document
    plan = port_matrix_plan(sim, ports)
    if steps is not None and (isinstance(steps, bool)
                              or not isinstance(steps, int) or steps <= 0):
        raise ApiError("steps must be a positive integer.")
    root = os.path.abspath(root_dir) if root_dir else pm.sweep_dir_for(doc)
    names = [p["name"] for p in plan["ports"]]
    try:
        spec, arrays, mports, excitation = pm.build_sweep(doc, names,
                                                          progress=progress)
    except vox_mod.VoxelizationCancelled:
        raise ApiError("Port-matrix export cancelled during voxelisation.")
    except (vox_mod.GridRequiredError, pm.SweepError) as err:
        raise ApiError(str(err))
    root = pm.prepare_sweep_dir(doc, len(mports), root)
    dirs = pm.export_drive_jobs(root, spec, arrays, mports, excitation, steps)
    return {
        "root_dir": root,
        "steps": int(steps if steps is not None else spec.get("steps", 0)),
        "ports": [_port_json(p) for p in mports],
        "drives": [{"index": k, "port": p.name, "workdir": d,
                    "job": os.path.join(d, "job.json")}
                   for k, (p, d) in enumerate(zip(mports, dirs))],
        "excitation": excitation,
    }


def _infer_ports(root):
    """The driven port of each ``drive_<k>/job.json``, in drive order."""
    import json

    from wavesim_gui import portmatrix as pm

    ports, drive = [], 0
    while True:
        path = os.path.join(root, pm.drive_dir_name(drive), "job.json")
        if not os.path.isfile(path):
            break
        with open(path, encoding="utf-8") as handle:
            spec = json.load(handle)
        live = [p for p in pm.spec_ports(spec)
                if float((p.entry(spec).get("excitation") or {})
                         .get("amplitude", 0.0) or 0.0) != 0.0]
        if len(live) != 1:
            raise ApiError("Cannot tell which port {} drives ({} live "
                           "excitations); pass ports=[...] explicitly.".format(
                               pm.drive_dir_name(drive), len(live)))
        ports.append(live[0])
        drive += 1
    return ports


@api_function("port_matrix")
def load_port_matrix_results(sim=None, *, root_dir=None, ports=None):
    """Assemble a finished port-matrix sweep and add it to the Results tree.

    Use after every drive written by :func:`export_port_matrix_jobs` (or the
    Port Matrix command) has run. Builds the *Port Matrix* group under the
    Simulation, replacing a previous one, exactly as the GUI does.

    :param root_dir: the sweep directory (default: the document's ``matrix``
        results folder).
    :param ports: port names in drive order; None = read from each
        ``drive_<k>/job.json`` (the one live excitation).
    :returns: ``{"group": ref, "ports": [names], "root_dir", "bins",
        "valid_bins", "f_min_hz", "f_max_hz", "max_reciprocity_error"}``
        (frequency span and reciprocity over the reported bins).
    """
    import json

    import numpy as np

    from wavesim_gui import portmatrix as pm
    from wavesim_gui import results as results_mod

    sim = c.get_sim(sim)
    doc = sim.Document
    root = os.path.abspath(root_dir) if root_dir else pm.sweep_dir_for(doc)
    first = os.path.join(root, pm.drive_dir_name(0), "job.json")
    if not os.path.isfile(first):
        raise ApiError("No port-matrix sweep in {} (missing {}).".format(
            root, first))
    try:
        if ports is None:
            mports = _infer_ports(root)
        else:
            with open(first, encoding="utf-8") as handle:
                spec = json.load(handle)
            mports = pm.select_ports(spec, list(ports))
        grp = results_mod.build_matrix_results(doc, sim, root, mports)
        data = pm.assemble(root, mports)
    except pm.SweepError as err:
        raise ApiError(str(err))
    valid = np.asarray(data["valid"])
    freqs = np.asarray(data["freqs"])
    recip = pm.reciprocity_error(data["z"])
    finite = recip[np.isfinite(recip)]
    return {
        "group": c.ref(grp),
        "ports": [p.name for p in mports],
        "root_dir": root,
        "bins": int(valid.size),
        "valid_bins": int(valid.sum()),
        "f_min_hz": float(freqs[valid].min()) if valid.any() else None,
        "f_max_hz": float(freqs[valid].max()) if valid.any() else None,
        "max_reciprocity_error": float(finite.max()) if finite.size else None,
    }
