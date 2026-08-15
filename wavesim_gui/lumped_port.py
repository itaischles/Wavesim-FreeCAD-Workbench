# -*- coding: utf-8 -*-
"""Lumped R/L/C port for the Wavesim workbench.

A *Lumped Port* is a two-terminal circuit element sitting on a straight line
across a gap in the geometry, driven by the solver's
:class:`wavesim.sources.LineSource` (the companion model in
:mod:`wavesim.lumped`). Between the terminals it puts one to three branches --
a resistance, an inductance and a capacitance -- wired in series or in
parallel, optionally in company with an ideal voltage or current source:

======================  ===================================================
Drive + load            Element
======================  ===================================================
load only               passive R / L / C network (e.g. a termination)
voltage + load          Thevenin source (the load in series with the EMF)
current + load          Norton source (the load in parallel with it)
voltage, no load        ideal voltage source (a hard write on the line)
current, no load        ideal impressed current
======================  ===================================================

This is the simple sibling of the SPICE line port (:mod:`wavesim_gui.spice_port`).
Use a SPICE port when the network is nonlinear, active, or larger than three
branches -- it hands the whole thing to ngspice. Use this one for plain R/L/C
plus a source: no ngspice, no netlist file, and the reactive branches are
integrated trapezoidally, so no value of L or C can cost the run its timestep.

Geometry
--------
The port line comes from picked geometry rather than from a dedicated sketch:

* **two vertices** -- the line joins them;
* **two planar faces** -- the ``+`` face's centre of mass, projected along its
  normal onto the ``-`` face's plane (so the line is normal to both);
* **one edge** -- its two ends, the curve's own direction giving the polarity;
* a **vertex and a face**, or a dropped sketch/curve object, both of which fall
  out of the same two rules.

``TerminalPlus``/``TerminalMinus`` are two separate ``App::PropertyLinkSub``
properties, emphatically *not* one ``App::PropertyLinkSubList``: that property
groups its entries by object, so two vertices picked on a single body come back
as one ``(body, ("Vertex3", "Vertex9"))`` pair with no dependable order -- and
the order *is* the polarity here. ``ReversePolarity`` is the panel's "Swap"
button, and the only way to flip a single-edge pick.

Nothing is added to the document: the resolved endpoints live in the hidden
``P0``/``P1`` and the view provider draws its own bold segment, ``+``/``-`` end
markers and a mid-line arrow along the positive-current direction. The picked
bodies keep their own appearance.

The grid parasitic
------------------
A discrete lumped element presents ``Z_eq + kappa/2`` to the field, not
``Z_eq``: ``kappa = sum(dt*dl^2/(eps*dV))`` over the line's Yee edges is the
port voltage change per unit injected current, and half of it lands in series
with the load. A resistor can pre-compensate (``R = R_target - kappa/2``); a
reactive branch cannot, and kappa/2 runs to ~100 ohm per edge on a short line,
which is the practical limit on how ideal a discrete capacitor can be. The task
panel estimates it (:func:`self_coupling_ohms`) and compares it against the
load, because nothing else in the run will say so.

Units: FreeCAD is millimetres, the solver metres -- :func:`lumped_port_spec`
converts to the solver frame (from the domain origin), mirroring
:func:`wavesim_gui.source.source_spec`. R/L/C are stored in SI (ohms, henries,
farads) and edited in the panel with an engineering-prefix combo.

Importing this module registers ``Wavesim_AddLumpedPort`` with ``Gui.addCommand``
when a GUI is available.
"""

import math
import os

import FreeCAD

from wavesim_gui.commands import active_simulation
from wavesim_gui import domain as domain_mod
from wavesim_gui import excitation as exc
from wavesim_gui import labels as labels_mod


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_RESOURCES_DIR = os.path.join(_WB_DIR, "Resources")
# The 24x24 SVG icon set (grouped by colour: blue setup, amber sources,
# teal monitors).
_ICONS_DIR = os.path.join(_RESOURCES_DIR, "icons")
_LUMPED_PORT_ICON = os.path.join(_ICONS_DIR, "port_lumped.svg")

_TYPE_PROP = "WavesimType"
_LUMPED_TYPE = "LumpedPort"

# The terminal links are the **Global**-scope variant on purpose: a terminal is
# normally a face or vertex of a PartDesign Body, and a local-scope
# App::PropertyLinkSub refuses to point outside its own container. See
# _ensure_terminal_prop.
_TERMINAL_PROP_TYPE = "App::PropertyLinkSubGlobal"

# Lumped ports are excitations/ports, so they live under the "Sources" group.
_SOURCES_GROUP = "Sources"

_MM_PER_M = 1000.0
# F/m. Deliberately ``wavesim.constants.EPS0``'s own (truncated) value rather
# than the CODATA one, so the panel's kappa estimate equals the kappa the run
# will report rather than differing from it in the ninth digit.
_EPS0 = 8.8541878e-12

# Drawing: the same amber segment as the SPICE line port (the source/port
# group's darkest icon shade) with polarity-coloured end markers.
_LINE_COLOR = (0.655, 0.373, 0.075)
_PLUS_COLOR = (0.10, 0.80, 0.10)
_MINUS_COLOR = (0.90, 0.20, 0.20)
# Arrow head length as a fraction of the line, pointing p0 -> p1 (the direction
# positive port current is delivered).
_ARROW_FRACTION = 0.18

# Drive modes: the enum stored on the object, and the job.json token for each.
DRIVE_NONE = "None (passive load)"
DRIVE_VOLTAGE = "Voltage source"
DRIVE_CURRENT = "Current source"
DRIVE_LABELS = [DRIVE_NONE, DRIVE_VOLTAGE, DRIVE_CURRENT]
_DRIVE_TOKEN = {
    DRIVE_NONE: "none",
    DRIVE_VOLTAGE: "voltage",
    DRIVE_CURRENT: "current",
}

# Branch wiring between the two terminals; irrelevant with a single branch.
TOPOLOGY_LABELS = ["Series", "Parallel"]
_TOPOLOGY_TOKEN = {"Series": "series", "Parallel": "parallel"}
_TOPOLOGY_FROM_TOKEN = {"series": "Series", "parallel": "Parallel"}

# The three branches: (property suffix, enable property, job key, unit table
# index into _UNIT_TABLES, panel label).
_BRANCHES = (
    ("Resistance", "UseResistance", "resistance", "R", "Resistance"),
    ("Inductance", "UseInductance", "inductance", "L", "Inductance"),
    ("Capacitance", "UseCapacitance", "capacitance", "C", "Capacitance"),
)

# Engineering-prefix tables per branch kind: (suffix, SI factor), coarse to fine
# is irrelevant -- _best_unit picks by magnitude.
_UNIT_TABLES = {
    "R": [("mohm", 1.0e-3), ("ohm", 1.0), ("kohm", 1.0e3), ("Mohm", 1.0e6)],
    "L": [("pH", 1.0e-12), ("nH", 1.0e-9), ("uH", 1.0e-6), ("mH", 1.0e-3),
          ("H", 1.0)],
    "C": [("fF", 1.0e-15), ("pF", 1.0e-12), ("nF", 1.0e-9), ("uF", 1.0e-6),
          ("F", 1.0)],
}
# Default unit shown for a fresh (zero) value, and the SI default itself.
_UNIT_DEFAULT = {"R": ("ohm", 50.0), "L": ("nH", 1.0e-9), "C": ("pF", 1.0e-12)}

# How far off a grid axis the line may lie before it is worth mentioning
# (radians; the solver rasterises an oblique line correctly either way).
_AXIS_TOL_DEG = 1.0
# Face normals further apart than this are not a facing pair.
_PARALLEL_TOL_DEG = 2.0


# --------------------------------------------------------------------------- #
# Value formatting
# --------------------------------------------------------------------------- #

def _best_unit(value_si, kind):
    """Return ``(suffix, factor)`` showing *value_si* with a readable magnitude."""
    table = _UNIT_TABLES[kind]
    if not value_si:
        suffix = _UNIT_DEFAULT[kind][0]
        return suffix, dict(table)[suffix]
    best = table[0]
    for suffix, factor in table:
        if abs(value_si) >= factor:
            best = (suffix, factor)
    return best


def format_value(value_si, kind):
    """Human string for an SI branch value, e.g. ``50 ohm`` / ``0.2 pF``."""
    suffix, factor = _best_unit(value_si, kind)
    return "{:g} {}".format(value_si / factor, suffix)


# --------------------------------------------------------------------------- #
# Geometry resolution
# --------------------------------------------------------------------------- #

def _container_offset(obj):
    """The placement *obj*'s containers add on top of what its Shape carries.

    A **PartDesign feature's ``Shape`` is in its Body's local frame** — the
    Body's own Placement is applied above it — so reading ``Pad.Shape`` straight
    gives geometry in the wrong place, and a terminal picked on a second Body
    resolves as if it sat on the first. (A plain ``Part::`` primitive has no such
    gap: its Shape already includes its Placement.)

    Returns ``global * own^-1`` — the part of the transform the shape does not
    already carry — or ``None`` when that is the identity, which is the common
    case and worth not paying for.
    """
    try:
        extra = obj.getGlobalPlacement().multiply(obj.Placement.inverse())
    except Exception:
        return None
    if extra.Base.Length < 1.0e-12 and abs(extra.Rotation.Angle) < 1.0e-12:
        return None
    return extra


def _link_shapes(link):
    """Resolve an ``App::PropertyLinkSub`` value into ``(obj, [shape, ...])``.

    An empty sub-element list means the whole linked shape (what a dropped
    sketch leaves behind). Shapes come back in **global** coordinates (see
    :func:`_container_offset`). Missing objects/sub-names yield an empty list
    rather than raising: geometry can be deleted or renumbered under a saved
    port.
    """
    if not link:
        return None, []
    try:
        obj = link[0]
        subs = [s for s in (link[1] or []) if s]
    except Exception:
        return None, []
    shape = getattr(obj, "Shape", None) if obj is not None else None
    if shape is None:
        return obj, []
    extra = _container_offset(obj)

    def _placed(sub):
        # Copy before transforming: transformShape mutates, and the uncopied
        # shape of a whole-object link is the document's own.
        if extra is None:
            return sub
        moved = sub.copy()
        moved.transformShape(extra.Matrix)
        return moved

    if not subs:
        return obj, [_placed(shape)]
    out = []
    for name in subs:
        try:
            out.append(_placed(shape.getElement(name)))
        except Exception:
            continue
    return obj, out


def _face_plane(face):
    """``(origin, unit normal)`` of a planar *face*, or ``None`` if not planar."""
    try:
        surface = face.Surface
        if surface.__class__.__name__ != "Plane":
            return None
        u0, u1, v0, v1 = face.ParameterRange
        normal = face.normalAt(0.5 * (u0 + u1), 0.5 * (v0 + v1))
        normal = FreeCAD.Vector(normal).normalize()
    except Exception:
        return None
    return FreeCAD.Vector(face.CenterOfMass), normal


def _terminal_point(shape):
    """``(point_mm, plane, face)`` for one terminal shape, or ``None``.

    *plane* is ``(origin, normal)`` for a planar face (the port line is made
    normal to it) and ``None`` otherwise; *face* is kept so the caller can ask
    whether the projected point actually lands on it.
    """
    if shape is None:
        return None
    point = getattr(shape, "Point", None)
    if point is not None:                       # a vertex
        return FreeCAD.Vector(point), None, None
    if getattr(shape, "ShapeType", "") == "Face":
        return (FreeCAD.Vector(shape.CenterOfMass), _face_plane(shape), shape)
    faces = list(getattr(shape, "Faces", []) or [])
    if len(faces) == 1:                         # a single-face pick, unsubbed
        return (FreeCAD.Vector(faces[0].CenterOfMass), _face_plane(faces[0]),
                faces[0])
    try:
        return FreeCAD.Vector(shape.CenterOfMass), None, None
    except Exception:
        return None


def _curve_ends(shape):
    """Ordered world-mm ends ``(p0, p1)`` of a curve *shape*, or ``None``.

    Mirrors :func:`wavesim_gui.spice_port._line_endpoints_mm`: the edges are
    sorted into wires and the longest is taken, its ends following the curve's
    own direction so the start is the ``+`` terminal. A shape carrying solids is
    not a curve (a picked body is a terminal, not a path).
    """
    if shape is None or getattr(shape, "Solids", None):
        return None
    edges = list(getattr(shape, "Edges", []) or [])
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
    try:
        pts = wire.discretize(Number=2)
    except Exception:
        return None
    if len(pts) < 2:
        return None
    return FreeCAD.Vector(pts[0]), FreeCAD.Vector(pts[-1])


def _project_onto(point, plane):
    """*point* projected along the plane normal onto ``plane = (origin, n)``."""
    origin, normal = plane
    return point - normal * (point - origin).dot(normal)


def _angle_deg(a, b):
    """Angle between two vectors in degrees (0 if either is degenerate)."""
    if a.Length < 1.0e-12 or b.Length < 1.0e-12:
        return 0.0
    cos = max(-1.0, min(1.0, a.dot(b) / (a.Length * b.Length)))
    return math.degrees(math.acos(cos))


def resolve_line(obj):
    """Return ``(p0, p1, warnings)`` for *obj*'s terminals, or ``(None, None, w)``.

    ``p0`` is the ``+`` terminal (positive port current leaves it), in world
    millimetres. *warnings* is a list of human strings about the geometry that
    are worth saying but not worth refusing over -- the panel shows them live
    and the job build prints them.
    """
    warnings = []
    _obj_a, shapes_a = _link_shapes(getattr(obj, "TerminalPlus", None))
    _obj_b, shapes_b = _link_shapes(getattr(obj, "TerminalMinus", None))

    if not shapes_a and not shapes_b:
        return None, None, ["no terminals selected"]

    if shapes_a and not shapes_b:
        # One pick: only a curve carries both ends by itself.
        ends = _curve_ends(shapes_a[0])
        if ends is None:
            return None, None, [
                "only one terminal is selected, and it is not an edge or a "
                "curve; pick a second vertex or face"]
        p0, p1 = ends
        chord = p1 - p0
        try:
            length = float(shapes_a[0].Length)
        except Exception:
            length = chord.Length
        if chord.Length > 0.0 and length > 1.001 * chord.Length:
            warnings.append(
                "the picked edge is curved ({:g} mm long against a {:g} mm "
                "chord); the element is the straight chord between its ends"
                .format(length, chord.Length))
    else:
        if not shapes_a or not shapes_b:
            return None, None, ["only one terminal is selected"]
        term_a = _terminal_point(shapes_a[0])
        term_b = _terminal_point(shapes_b[0])
        if term_a is None or term_b is None:
            return None, None, ["a terminal's geometry could not be resolved"]
        p0, plane_a, _face_a = term_a
        p1, plane_b, face_b = term_b
        if plane_a is not None and plane_b is not None:
            off = _angle_deg(plane_a[1], plane_b[1])
            if min(off, 180.0 - off) > _PARALLEL_TOL_DEG:
                # Neither face's normal describes the gap, so projecting onto
                # either one would move an endpoint for no reason. Fall back to
                # the two centres, which is at least what the user picked.
                warnings.append(
                    "the two faces are {:.1f} deg from parallel; the port line "
                    "joins their centres instead of running normal to them"
                    .format(min(off, 180.0 - off)))
                plane_a = plane_b = None
        if plane_b is not None:
            p1 = _project_onto(p0, plane_b)
            if face_b is not None:
                try:
                    if not face_b.isInside(p1, 1.0e-3, True):
                        warnings.append(
                            "the '+' terminal projects outside the '-' face; "
                            "the line ends on that face's plane but off the "
                            "face itself")
                except Exception:
                    pass
        elif plane_a is not None:
            p0 = _project_onto(p1, plane_a)

    if bool(getattr(obj, "ReversePolarity", False)):
        p0, p1 = p1, p0

    delta = p1 - p0
    if delta.Length < 1.0e-9:
        return None, None, warnings + ["the two terminals are the same point"]

    axis, off_deg = _line_axis(delta)
    if axis is None:
        warnings.append(
            "the port line is {:.1f} deg off the nearest grid axis; it will be "
            "rasterised onto staggered edges per axis, which spreads the "
            "element over more cells than a gap-crossing element wants"
            .format(off_deg))
    return p0, p1, warnings


def _component(vec, axis):
    """Component *axis* (0/1/2) of a FreeCAD vector, by name rather than index."""
    return (vec.x, vec.y, vec.z)[axis]


def _line_axis(delta):
    """``(axis_index, off-axis angle deg)``; *axis_index* is None if oblique."""
    comps = [abs(delta.x), abs(delta.y), abs(delta.z)]
    axis = comps.index(max(comps))
    unit = FreeCAD.Vector(*(1.0 if i == axis else 0.0 for i in range(3)))
    off = _angle_deg(delta, unit)
    off = min(off, 180.0 - off)
    return (axis if off <= _AXIS_TOL_DEG else None), off


def line_endpoints_mm(obj):
    """The port's ``(p0, p1)`` in world mm, or ``None`` when unresolvable."""
    p0, p1, _warnings = resolve_line(obj)
    if p0 is None:
        return None
    return p0, p1


# --------------------------------------------------------------------------- #
# The network
# --------------------------------------------------------------------------- #

def branches(obj):
    """Enabled branches as ``[('R', 50.0), ('C', 2e-13), ...]`` (SI).

    A branch counts only when its ``Use*`` flag is on *and* its value is
    positive: the solver refuses a non-positive R/L/C, and "absent" is a real
    state there -- a missing branch is a short in series and an open in
    parallel, not a zero.
    """
    out = []
    for prop, enable, _key, kind, _label in _BRANCHES:
        if not bool(getattr(obj, enable, False)):
            continue
        value = float(getattr(obj, prop, 0.0) or 0.0)
        if value > 0.0:
            out.append((kind, value))
    return out


def topology(obj):
    """The branch wiring token (``'series'`` / ``'parallel'``)."""
    return _TOPOLOGY_TOKEN.get(str(getattr(obj, "Topology", "Series")), "series")


def drive_mode(obj):
    """The drive token (``'none'`` / ``'voltage'`` / ``'current'``)."""
    return _DRIVE_TOKEN.get(str(getattr(obj, "Drive", DRIVE_NONE)), "none")


def network_label(obj):
    """Short human description of the load, e.g. ``R 50 ohm + C 0.2 pF``."""
    parts = branches(obj)
    if not parts:
        return "no load"
    text = " + ".join("{} {}".format(kind, format_value(value, kind))
                      for kind, value in parts)
    if len(parts) > 1:
        text += " ({})".format(topology(obj))
    return text


def _describe(obj):
    """Tree-label description: the load, and the drive when there is one."""
    text = network_label(obj)
    mode = drive_mode(obj)
    if mode != "none":
        text = "{}, {} drive".format(text, mode)
    return text


# --------------------------------------------------------------------------- #
# The grid parasitic (kappa)
# --------------------------------------------------------------------------- #

def _cell_span(nodes, lo, hi):
    """Indices of the cells a ``[lo, hi]`` interval covers in *nodes* (metres)."""
    out = []
    for i in range(len(nodes) - 1):
        if nodes[i + 1] > lo and nodes[i] < hi:
            out.append(i)
    return out


def _nearest_yee_index(locs, values):
    """Nearest index in the sorted Yee locations *locs* for each of *values*.

    The workbench-side twin of :func:`wavesim.monitors._yee_index`: snap to the
    nearest actual sample position (so a graded grid works), by searching the
    midpoints between them.
    """
    import bisect

    bounds = [0.5 * (locs[i] + locs[i + 1]) for i in range(len(locs) - 1)]
    last = len(locs) - 1
    return [min(max(bisect.bisect_right(bounds, v), 0), last) for v in values]


def self_coupling_ohms(obj, sim=None):
    """Estimated ``kappa`` in ohms for this port on the current grid, or ``None``.

    ``kappa = sum(dt*dl^2/(eps*dV))`` over the Yee E-edges the line occupies
    (:meth:`wavesim.sources.LineSource.self_coupling`). Half of it sits in
    series with the load.

    This **replicates the solver's own quadrature** rather than assuming the
    line covers whole edges, because it does not: the path is walked in
    sub-steps of half a cell and each sub-step's length is binned onto the Yee
    edge nearest its midpoint, so a line whose endpoints sit on grid *nodes*
    puts a half weight on an edge at either end. Since kappa sums ``w^2`` that
    is not a rounding detail -- for the commonest case of all, a gap crossed in
    one cell, the whole-edge assumption overstates kappa by exactly 2x.

    Estimated in one respect and honest about it: the permittivity used is the
    domain **background** material's, since the run reads a per-edge epsilon off
    a voxelised grid that does not exist until the job is built. An oblique line
    returns ``None`` rather than a number from a quadrature (per-axis staggered
    edges) this does not implement.
    """
    doc = getattr(obj, "Document", None)
    if sim is None and doc is not None:
        sim = active_simulation(doc)
    dom = domain_mod.find_domain(sim) if sim is not None else None
    if dom is None:
        return None
    ends = line_endpoints_mm(obj)
    if ends is None:
        return None
    delta = ends[1] - ends[0]
    axis, _off = _line_axis(delta)
    if axis is None:
        return None                     # oblique: not this estimator's business

    nodes = domain_mod.node_coords_m(dom)
    if any(len(a) < 2 for a in nodes):
        return None
    dt = domain_mod.cfl_dt(dom)
    if dt <= 0.0:
        return None

    background = domain_mod.background_material(dom)
    eps_r = float(getattr(background, "Eps", 1.0) or 1.0) if background else 1.0
    if bool(getattr(background, "Pec", False)):
        eps_r = 1.0

    p0_m = [ends[0].x / _MM_PER_M, ends[0].y / _MM_PER_M, ends[0].z / _MM_PER_M]
    p1_m = [ends[1].x / _MM_PER_M, ends[1].y / _MM_PER_M, ends[1].z / _MM_PER_M]
    widths = [[a[i + 1] - a[i] for i in range(len(a) - 1)] for a in nodes]
    if any(not w for w in widths):
        return None

    # The transverse Yee offset of the line's own component is half a cell, so
    # those two indices come from the cell centres; the offset along the line is
    # zero, so those sample positions are the nodes themselves (see
    # wavesim.monitors._YEE_OFFSETS -- this is what makes a node-aligned end
    # contribute a half weight).
    trans_idx = {}
    for a in range(3):
        if a == axis:
            continue
        centres = [0.5 * (nodes[a][i] + nodes[a][i + 1])
                   for i in range(len(widths[a]))]
        trans_idx[a] = _nearest_yee_index(centres, [p0_m[a]])[0]
    area = 1.0
    for a, i in trans_idx.items():
        area *= widths[a][i]
    if area <= 0.0:
        return None

    # Sub-step exactly as the solver's path quadrature does: steps of at most
    # half the smallest cell anywhere, each binned onto the nearest Yee edge.
    length = abs(p1_m[axis] - p0_m[axis])
    max_step = 0.5 * min(domain_mod.min_spacings_m(dom))
    if max_step <= 0.0 or length <= 0.0:
        return None
    n_sub = max(1, int(math.ceil(length / max_step * (1.0 - 1.0e-9))))
    n_sub = min(n_sub, 100000)
    step = (p1_m[axis] - p0_m[axis]) / n_sub
    mids = [p0_m[axis] + (m + 0.5) * step for m in range(n_sub)]
    locs = nodes[axis][:len(widths[axis])]      # E_a samples sit on the nodes
    per_edge = {}
    for i in _nearest_yee_index(locs, mids):
        per_edge[i] = per_edge.get(i, 0.0) + abs(step)

    total = 0.0
    for i, w in per_edge.items():
        dv = widths[axis][i] * area
        if dv > 0.0:
            total += dt * w * w / (_EPS0 * eps_r * dv)
    return total if total > 0.0 else None


def cells_crossed(obj, sim=None):
    """How many grid cells the port line spans along its axis, or ``None``."""
    doc = getattr(obj, "Document", None)
    if sim is None and doc is not None:
        sim = active_simulation(doc)
    dom = domain_mod.find_domain(sim) if sim is not None else None
    ends = line_endpoints_mm(obj)
    if dom is None or ends is None:
        return None
    axis, _off = _line_axis(ends[1] - ends[0])
    if axis is None:
        return None
    nodes = domain_mod.node_coords_m(dom)
    if len(nodes[axis]) < 2:
        return None
    lo, hi = sorted((_component(ends[0], axis) / _MM_PER_M,
                     _component(ends[1], axis) / _MM_PER_M))
    return len(_cell_span(nodes[axis], lo, hi)) or None


# --------------------------------------------------------------------------- #
# Document-object model
# --------------------------------------------------------------------------- #

class LumpedPortObject:
    """``Proxy`` for a lumped R/L/C port.

    Properties:
        ``TerminalPlus`` / ``TerminalMinus`` -- the picked geometry (vertex,
            planar face, edge or dropped curve) giving the two terminals.
        ``ReversePolarity`` -- swap ``+`` and ``-``.
        ``UseResistance``/``Resistance`` (and the L / C pairs) -- the branches,
            in SI ohms / henries / farads.
        ``Topology`` -- how several branches are wired between the terminals.
        ``Drive`` -- none / voltage source / current source, plus the shared
            excitation property set (:mod:`wavesim_gui.excitation`).

    Hidden ``P0``/``P1`` carry the resolved endpoints (world mm) for the view
    provider; ``execute`` keeps them in sync with the picked geometry.
    """

    def __init__(self, obj):
        self.Type = _LUMPED_TYPE
        obj.Proxy = self
        if not hasattr(obj, _TYPE_PROP):
            obj.addProperty(
                "App::PropertyString", _TYPE_PROP, "Wavesim",
                "Marks this object as a Wavesim lumped port",
            )
            setattr(obj, _TYPE_PROP, _LUMPED_TYPE)
            obj.setEditorMode(_TYPE_PROP, 1)  # read-only identity marker
        ensure_port_props(obj)

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _LUMPED_TYPE)
        ensure_port_props(obj)
        # Re-derive the drawn endpoints rather than trusting the stored ones.
        # Opening a document does not recompute an untouched object, so a port
        # saved with endpoints from an earlier (or buggier) resolver would go on
        # drawing them until something else touched it. The linked shapes are
        # restored by the time this runs, which is what makes it safe here.
        try:
            self.execute(obj)
        except Exception:
            pass

    def execute(self, obj):
        """Sync the drawn endpoints to the picked terminals (or collapse them)."""
        ends = line_endpoints_mm(obj)
        if ends is None:
            obj.P0 = FreeCAD.Vector(0, 0, 0)
            obj.P1 = FreeCAD.Vector(0, 0, 0)
        else:
            obj.P0 = FreeCAD.Vector(ends[0])
            obj.P1 = FreeCAD.Vector(ends[1])

    def dumps(self):
        return {"Type": getattr(self, "Type", _LUMPED_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _LUMPED_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


def _ensure_terminal_prop(obj, name, doc):
    """Add — or upgrade in place — one global-scope terminal link property.

    The scope matters. A plain ``App::PropertyLinkSub`` is *local*, and a
    terminal is almost always a face or vertex of a PartDesign **Body**, which
    is a different container: FreeCAD then refuses the link with "go out of the
    allowed scope ... reside within 'Body'". ``App::PropertyLinkSubGlobal`` is
    the same property with the scope check lifted, which is what a port picking
    geometry anywhere in the document needs.

    A port saved with the local property is migrated here rather than left
    broken: the value is read back, the property replaced, and the value
    restored, so an existing document keeps its terminals.
    """
    value = None
    if hasattr(obj, name):
        try:
            if obj.getTypeIdOfProperty(name) == _TERMINAL_PROP_TYPE:
                return
            value = getattr(obj, name)
            obj.removeProperty(name)
        except Exception:
            return          # an expression-bound or otherwise pinned property
    obj.addProperty(_TERMINAL_PROP_TYPE, name, "Port", doc)
    if value:
        try:
            setattr(obj, name, value)
        except Exception:
            pass


def ensure_port_props(obj):
    """Add the lumped port's properties; idempotent, so it doubles as a back-fill."""
    _ensure_terminal_prop(
        obj, "TerminalPlus",
        "Geometry giving the '+' terminal: a vertex, a planar face, or an "
        "edge that carries both ends by itself. Set via the task panel.")
    _ensure_terminal_prop(
        obj, "TerminalMinus",
        "Geometry giving the '-' terminal (leave empty when the '+' pick is "
        "an edge). Set via the task panel.")
    if not hasattr(obj, "ReversePolarity"):
        obj.addProperty(
            "App::PropertyBool", "ReversePolarity", "Port",
            "Swap the '+' and '-' ends of the port line. Positive port current "
            "is delivered out of the '+' terminal.",
        )
        obj.ReversePolarity = False

    for prop, enable, _key, kind, label in _BRANCHES:
        if not hasattr(obj, enable):
            obj.addProperty(
                "App::PropertyBool", enable, "Load",
                "Include the {} branch in the lumped network".format(
                    label.lower()),
            )
            setattr(obj, enable, prop == "Resistance")
        if not hasattr(obj, prop):
            obj.addProperty(
                "App::PropertyFloat", prop, "Load",
                "{} of the lumped element, in SI ({}); edit via the task panel "
                "for engineering units".format(
                    label, {"R": "ohms", "L": "henries", "C": "farads"}[kind]),
            )
            setattr(obj, prop, float(_UNIT_DEFAULT[kind][1]))
    if not hasattr(obj, "Topology"):
        obj.addProperty(
            "App::PropertyEnumeration", "Topology", "Load",
            "How several R/L/C branches are wired between the two terminals "
            "(irrelevant with a single branch)",
        )
        obj.Topology = list(TOPOLOGY_LABELS)
        obj.Topology = TOPOLOGY_LABELS[0]

    if not hasattr(obj, "Drive"):
        obj.addProperty(
            "App::PropertyEnumeration", "Drive", "Drive",
            "Ideal source in company with the load: none (a passive element), a "
            "voltage source (Thevenin, the load in series) or a current source "
            "(Norton, the load in parallel)",
        )
        obj.Drive = list(DRIVE_LABELS)
        obj.Drive = DRIVE_NONE
    exc.ensure_object_props(obj)
    sync_drive_visibility(obj)

    # Endpoints (hidden) for the view provider, synced by execute().
    for name in ("P0", "P1"):
        if not hasattr(obj, name):
            obj.addProperty("App::PropertyVector", name, "Line", "")
            obj.setEditorMode(name, 2)  # hidden


def sync_drive_visibility(obj):
    """Hide the excitation properties outright on a passive (undriven) port."""
    if drive_mode(obj) == "none":
        exc.hide_props(obj)
    else:
        exc.sync_visibility(obj)


# --------------------------------------------------------------------------- #
# Lookup helpers & job serialisation
# --------------------------------------------------------------------------- #

def is_lumped_port(obj):
    """Return True if *obj* is a Wavesim lumped port."""
    return getattr(obj, _TYPE_PROP, None) == _LUMPED_TYPE


def sources_group(sim):
    """Return the "Sources" child group of *sim* (or *sim* itself if missing)."""
    if sim is None:
        return None
    for child in sim.Group:
        if child.Name == _SOURCES_GROUP or child.Label == _SOURCES_GROUP:
            return child
    return sim


def find_lumped_ports(sim):
    """All lumped ports under the Simulation container *sim*."""
    grp = sources_group(sim)
    return [o for o in grp.Group if is_lumped_port(o)] if grp else []


def lumped_port_points_mm(sim):
    """World-mm endpoints of every lumped port under *sim* (for the domain bbox)."""
    if sim is None:
        return []
    pts = []
    for port in find_lumped_ports(sim):
        ends = line_endpoints_mm(port)
        if ends is not None:
            pts.append((ends[0].x, ends[0].y, ends[0].z))
            pts.append((ends[1].x, ends[1].y, ends[1].z))
    return pts


def lumped_port_spec(obj, origin_m):
    """Return the ``job.json`` ``lumped_ports`` dict for *obj*, or ``None``.

    Skipped with a warning (so the rest of the run proceeds) when the terminals
    do not resolve to a line, or when the port has neither a load nor a drive --
    the solver's ``LineSource`` refuses that combination, and it means the user
    has not finished configuring the port rather than that the run is wrong.
    """
    p0, p1, warnings = resolve_line(obj)
    for text in warnings:
        FreeCAD.Console.PrintWarning(
            "Wavesim: lumped port '{}': {}.\n".format(obj.Label, text))
    if p0 is None:
        FreeCAD.Console.PrintWarning(
            "Wavesim: lumped port '{}' has no usable terminals; skipping it.\n"
            .format(obj.Label))
        return None
    parts = branches(obj)
    mode = drive_mode(obj)
    if not parts and mode == "none":
        FreeCAD.Console.PrintWarning(
            "Wavesim: lumped port '{}' has neither a load (R/L/C) nor a drive; "
            "skipping it.\n".format(obj.Label))
        return None

    spec = {
        "name": str(obj.Label or obj.Name),
        "p0": [p0.x / _MM_PER_M - origin_m[0],
               p0.y / _MM_PER_M - origin_m[1],
               p0.z / _MM_PER_M - origin_m[2]],
        "p1": [p1.x / _MM_PER_M - origin_m[0],
               p1.y / _MM_PER_M - origin_m[1],
               p1.z / _MM_PER_M - origin_m[2]],
        "topology": topology(obj),
        "drive": mode,
    }
    for _prop, _enable, key, kind, _label in _BRANCHES:
        value = dict(parts).get(kind)
        if value is not None:
            spec[key] = float(value)
    if mode != "none":
        spec["excitation"] = exc.spec_from_object(obj)
    return spec


def colocation_warnings(sim):
    """Warn about lumped ports sharing a line with another port.

    Co-located elements inject sequentially rather than as one solved circuit,
    so each contributes its own ``kappa/2`` in series and the pair settles to a
    divider the user did not ask for. One port with a ``Topology`` network is
    the supported way to put two branches on one gap.
    """
    ports = [(p, line_endpoints_mm(p)) for p in find_lumped_ports(sim)]
    ports = [(p, e) for p, e in ports if e is not None]
    out = []
    for i, (port_a, ends_a) in enumerate(ports):
        for port_b, ends_b in ports[i + 1:]:
            same = ((ends_a[0] - ends_b[0]).Length < 1.0e-6
                    and (ends_a[1] - ends_b[1]).Length < 1.0e-6)
            flipped = ((ends_a[0] - ends_b[1]).Length < 1.0e-6
                       and (ends_a[1] - ends_b[0]).Length < 1.0e-6)
            if same or flipped:
                out.append(
                    "lumped ports '{}' and '{}' sit on the same line. They "
                    "inject one after the other, not as a jointly solved "
                    "circuit, so each adds its own kappa/2 in series -- use one "
                    "port with a series/parallel network instead."
                    .format(port_a.Label, port_b.Label))
    return out


# --------------------------------------------------------------------------- #
# GUI: view provider, task panel, command
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

    # The lumped-port panel reuses the point source's excitation widgets/plot.
    from wavesim_gui import source as source_mod

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

    def _terminal_desc(link):
        """Human description of a terminal link, e.g. ``Body (Vertex7)``."""
        if not link:
            return "(none)"
        try:
            obj = link[0]
            subs = [s for s in (link[1] or []) if s]
        except Exception:
            return "(none)"
        if obj is None:
            return "(none)"
        return "{} ({})".format(obj.Label, ", ".join(subs)) if subs else obj.Label

    # ------------------------------------------------------------------ #
    # View provider: bold amber segment, +/- markers, direction arrow
    # ------------------------------------------------------------------ #

    class LumpedPortViewProvider:
        """Draws the port line between its endpoints, with a current arrow.

        The port owns this drawing entirely -- the picked vertices/faces belong
        to the user's bodies and are left looking exactly as they did, and no
        edge object is added to the document.
        """

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
            style.lineWidth = 4          # bolder than a monitor path or a
            root.addChild(style)         # SPICE line port: this one carries a load
            self._line = coin.SoLineSet()
            root.addChild(self._line)

            # Mid-line arrow along p0 -> p1, the direction positive port current
            # is delivered. Sized as a fraction of the line (rather than a fixed
            # pixel size) so it stays readable against the element it labels.
            arrow = coin.SoSeparator()
            acolor = coin.SoBaseColor()
            acolor.rgb.setValue(*_LINE_COLOR)
            arrow.addChild(acolor)
            self._arrow_pos = coin.SoTranslation()
            arrow.addChild(self._arrow_pos)
            self._arrow_rot = coin.SoRotation()
            arrow.addChild(self._arrow_rot)
            self._arrow_scale = coin.SoScale()
            self._arrow_scale.scaleFactor.setValue(0.0, 0.0, 0.0)
            arrow.addChild(self._arrow_scale)
            head = coin.SoCone()         # axis +Y, centred on the origin
            head.bottomRadius = 0.35
            head.height = 1.0
            arrow.addChild(head)
            root.addChild(arrow)

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
            from pivy import coin

            obj = getattr(self, "Object", None)
            if obj is None:
                return
            p0 = getattr(obj, "P0", None)
            p1 = getattr(obj, "P1", None)
            if p0 is None or p1 is None or (p1 - p0).Length < 1.0e-9:
                # No usable line: collapse everything.
                self._line.numVertices.setValue(0)
                self._arrow_scale.scaleFactor.setValue(0.0, 0.0, 0.0)
                for m in (getattr(self, "_plus", None),
                          getattr(self, "_minus", None)):
                    if m is not None:
                        m["mset"].numPoints.setValue(0)
                return
            pts = [(p0.x, p0.y, p0.z), (p1.x, p1.y, p1.z)]
            self._coords.point.setValues(0, 2, pts)
            if self._coords.point.getNum() > 2:
                self._coords.point.deleteValues(2)
            self._line.numVertices.setValue(2)

            delta = p1 - p0
            size = _ARROW_FRACTION * delta.Length
            mid = p0 + delta * 0.5
            self._arrow_pos.translation.setValue(mid.x, mid.y, mid.z)
            self._arrow_rot.rotation.setValue(
                coin.SbRotation(coin.SbVec3f(0.0, 1.0, 0.0),
                                coin.SbVec3f(delta.x, delta.y, delta.z)))
            self._arrow_scale.scaleFactor.setValue(size, size, size)

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
            return _LUMPED_PORT_ICON

        # -- Curve drag & drop (a sketch may stand in for the two picks) ---- #
        #
        # No claimChildren: a terminal is normally a face or vertex of the
        # user's own body, and claiming that body would move it under the port
        # in the tree. A dropped sketch stays where the user put it too.

        def canDropObjects(self):
            return True

        def canDropObject(self, obj):
            return _is_curve_object(obj)

        def dropObject(self, vobj, obj):
            port = vobj.Object
            if not _is_curve_object(obj):
                return
            old_auto = "Lumped Port ({})".format(_describe(port))
            port.TerminalPlus = (obj, [])
            port.TerminalMinus = None
            labels_mod.retitle(port, old_auto,
                               "Lumped Port ({})".format(_describe(port)))
            port.Document.recompute()
            domain_mod.notify_domain_inputs_changed(port.Document)

        def setEdit(self, vobj, mode=0):
            _open_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_panel(vobj.Object)
            return True

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    # ------------------------------------------------------------------ #
    # Task panel
    # ------------------------------------------------------------------ #

    def selection_terminals():
        """``(plus, minus)`` link tuples from the current 3D selection.

        One picked sub-element becomes the ``+`` terminal on its own (an edge
        carries both ends); two become ``+`` and ``-`` in pick order. Anything
        else yields ``(None, None)``.
        """
        picks = []
        for sel in Gui.Selection.getSelectionEx():
            names = [n for n in (getattr(sel, "SubElementNames", []) or [])
                     if n.startswith(("Vertex", "Edge", "Face"))]
            if names:
                picks.extend((sel.Object, [n]) for n in names)
            elif _is_curve_object(sel.Object):
                picks.append((sel.Object, []))
        if not picks:
            return None, None
        if len(picks) == 1:
            return picks[0], None
        return picks[0], picks[1]

    class TaskLumpedPortPanel(object):
        """Edit a lumped port: its terminals, its R/L/C network and its drive."""

        def __init__(self, obj, created=False):
            QtWidgets = _qt_widgets()
            self.obj = obj
            self.created = created
            self._QtWidgets = QtWidgets
            # Live edits write straight onto the object (so the 3D line follows
            # the picks); these are what Cancel restores.
            self._orig_plus = getattr(obj, "TerminalPlus", None)
            self._orig_minus = getattr(obj, "TerminalMinus", None)
            self._orig_reverse = bool(getattr(obj, "ReversePolarity", False))

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Lumped Port")
            layout = QtWidgets.QFormLayout(form)
            self.form = form

            # -- Terminals ------------------------------------------------- #
            self._plus_label = QtWidgets.QLabel(
                _terminal_desc(getattr(obj, "TerminalPlus", None)))
            self._minus_label = QtWidgets.QLabel(
                _terminal_desc(getattr(obj, "TerminalMinus", None)))
            self._plus_label.setWordWrap(True)
            self._minus_label.setWordWrap(True)
            layout.addRow("+ terminal:", self._plus_label)
            layout.addRow("", self._pick_row("+", self._pick_plus))
            layout.addRow("- terminal:", self._minus_label)
            layout.addRow("", self._pick_row("-", self._pick_minus))

            swap = QtWidgets.QPushButton("Swap + / -")
            swap.clicked.connect(self._swap)
            layout.addRow("", swap)

            # -- Load ------------------------------------------------------ #
            self._branch_widgets = {}
            for prop, enable, _key, kind, label in _BRANCHES:
                check = QtWidgets.QCheckBox()
                check.setChecked(bool(getattr(obj, enable, False)))
                spin = QtWidgets.QDoubleSpinBox()
                spin.setRange(0.0, 1.0e12)
                spin.setDecimals(4)
                spin.setSingleStep(1.0)
                combo = QtWidgets.QComboBox()
                for suffix, _factor in _UNIT_TABLES[kind]:
                    combo.addItem(suffix)
                value_si = float(getattr(obj, prop, 0.0) or 0.0)
                suffix, factor = _best_unit(value_si, kind)
                combo.setCurrentText(suffix)
                spin.setValue(value_si / factor)

                row = QtWidgets.QWidget()
                hbox = QtWidgets.QHBoxLayout(row)
                hbox.setContentsMargins(0, 0, 0, 0)
                hbox.addWidget(check)
                hbox.addWidget(spin)
                hbox.addWidget(combo)
                layout.addRow(label + ":", row)
                self._branch_widgets[kind] = (check, spin, combo)
                check.toggled.connect(self._update_hint)
                spin.valueChanged.connect(self._update_hint)
                combo.currentTextChanged.connect(self._update_hint)

            self._topology = QtWidgets.QComboBox()
            self._topology.addItems(TOPOLOGY_LABELS)
            self._topology.setCurrentText(
                _TOPOLOGY_FROM_TOKEN.get(topology(obj), TOPOLOGY_LABELS[0]))
            self._topology.currentTextChanged.connect(self._update_hint)
            layout.addRow("Branches wired:", self._topology)

            # -- Drive ----------------------------------------------------- #
            self._drive = QtWidgets.QComboBox()
            self._drive.addItems(DRIVE_LABELS)
            self._drive.setCurrentText(str(getattr(obj, "Drive", DRIVE_NONE)))
            layout.addRow("Drive:", self._drive)

            # The excitation widgets live in their own container so the whole
            # block (combo, parameter rows and preview button) hides in one go
            # when the port is passive.
            self._exc_box = QtWidgets.QWidget()
            exc_form = QtWidgets.QFormLayout(self._exc_box)
            exc_form.setContentsMargins(0, 0, 0, 0)
            self._editor = source_mod.ExcitationEditor(
                obj, 0, exc_form, QtWidgets, active_simulation(obj.Document))
            self._editor.rebuild_params()
            self._editor.combo.currentTextChanged.connect(
                self._editor.rebuild_params)
            layout.addRow(self._exc_box)
            self._drive.currentTextChanged.connect(self._sync_drive)

            # -- Live hint + info ------------------------------------------ #
            self._hint = QtWidgets.QLabel("")
            self._hint.setWordWrap(True)
            layout.addRow("Geometry:", self._hint)

            info = QtWidgets.QLabel(
                "A lumped port is a two-terminal R/L/C element on a straight "
                "line across a gap. Select two vertices, two parallel planar "
                "faces, or a single edge; the '+' terminal is where positive "
                "port current leaves. Give it a load, a drive, or both. For a "
                "nonlinear or larger circuit use a SPICE line port instead."
            )
            info.setWordWrap(True)
            layout.addRow(info)

            self._sync_drive()
            self._update_hint()

        # -- widget helpers ------------------------------------------------ #

        def _pick_row(self, which, slot):
            QtWidgets = self._QtWidgets
            row = QtWidgets.QWidget()
            hbox = QtWidgets.QHBoxLayout(row)
            hbox.setContentsMargins(0, 0, 0, 0)
            pick = QtWidgets.QPushButton("Set from selection")
            clear = QtWidgets.QPushButton("Clear")
            hbox.addWidget(pick)
            hbox.addWidget(clear)
            pick.clicked.connect(slot)
            clear.clicked.connect(
                self._clear_plus if which == "+" else self._clear_minus)
            return row

        def _selected_link(self):
            """The first usable pick in the 3D view, or None (with a message)."""
            plus, _minus = selection_terminals()
            if plus is None:
                self._QtWidgets.QMessageBox.information(
                    self.form, "Wavesim Lumped Port",
                    "Select a vertex, a planar face or an edge in the 3D view "
                    "first, then click 'Set from selection'.",
                )
            return plus

        def _pick_plus(self, *_):
            link = self._selected_link()
            if link is not None:
                self.obj.TerminalPlus = link
                self._refresh_geometry()

        def _pick_minus(self, *_):
            link = self._selected_link()
            if link is not None:
                self.obj.TerminalMinus = link
                self._refresh_geometry()

        def _clear_plus(self, *_):
            self.obj.TerminalPlus = None
            self._refresh_geometry()

        def _clear_minus(self, *_):
            self.obj.TerminalMinus = None
            self._refresh_geometry()

        def _swap(self, *_):
            self.obj.ReversePolarity = not bool(
                getattr(self.obj, "ReversePolarity", False))
            self._refresh_geometry()

        def _refresh_geometry(self):
            """Redraw the line and refresh the labels after a terminal change."""
            self._plus_label.setText(
                _terminal_desc(getattr(self.obj, "TerminalPlus", None)))
            self._minus_label.setText(
                _terminal_desc(getattr(self.obj, "TerminalMinus", None)))
            self.obj.Document.recompute()
            self._update_hint()

        def _sync_drive(self, *_):
            self._exc_box.setVisible(
                _DRIVE_TOKEN.get(self._drive.currentText(), "none") != "none")

        # -- the live hint ------------------------------------------------- #

        def _branch_values(self):
            """``{kind: SI value}`` for the currently ticked branches."""
            out = {}
            for kind, (check, spin, combo) in self._branch_widgets.items():
                if not check.isChecked():
                    continue
                factor = dict(_UNIT_TABLES[kind]).get(combo.currentText(), 1.0)
                value = spin.value() * factor
                if value > 0.0:
                    out[kind] = value
            return out

        def _widget_impedance(self, frequency_hz):
            """Load impedance from the *widgets* (not the object) at a frequency."""
            values = self._branch_values()
            if not values or frequency_hz <= 0.0:
                return None
            w = 2.0 * math.pi * frequency_hz
            zs = []
            for kind, value in values.items():
                if kind == "R":
                    zs.append(complex(value, 0.0))
                elif kind == "L":
                    zs.append(complex(0.0, w * value))
                else:
                    zs.append(complex(0.0, -1.0 / (w * value)))
            series = _TOPOLOGY_TOKEN.get(
                self._topology.currentText(), "series") == "series"
            if series or len(zs) == 1:
                return sum(zs)
            return 1.0 / sum(1.0 / z for z in zs)

        def _update_hint(self, *_):
            """Rebuild the geometry/parasitic hint from the current state.

            The kappa/2 line is the point of this panel: a discrete lumped
            element presents ``Z + kappa/2`` to the field, a reactive branch
            cannot pre-compensate for it, and no other part of the run says so.
            """
            from wavesim_gui.commands import max_frequency_hz

            obj = self.obj
            sim = active_simulation(obj.Document)
            lines = []
            p0, p1, warnings = resolve_line(obj)
            if p0 is None:
                lines.append("No port line yet - select the two terminals.")
            else:
                delta = p1 - p0
                axis, off = _line_axis(delta)
                axis_text = ("along {}".format("xyz"[axis]) if axis is not None
                             else "{:.1f} deg off axis".format(off))
                cells = cells_crossed(obj, sim)
                lines.append("Line: {:.4g} mm {}{}.".format(
                    delta.Length, axis_text,
                    ", {} cell{}".format(cells, "" if cells == 1 else "s")
                    if cells else ""))
                kappa = self_coupling_ohms(obj, sim)
                if kappa is None:
                    # Say why rather than quietly dropping the line the user is
                    # meant to read before trusting a reactive load.
                    lines.append(
                        "Grid parasitic: only estimated for an axis-aligned "
                        "line." if axis is None else
                        "Grid parasitic: unavailable until the domain has been "
                        "sized (assign geometry to a material).")
                if kappa is not None:
                    lines.append(
                        "Grid parasitic: kappa/2 = {:.4g} ohm in series with "
                        "the load (background epsilon).".format(0.5 * kappa))
                    fmax = max_frequency_hz(sim) if sim is not None else 0.0
                    z = self._widget_impedance(fmax)
                    if z is not None:
                        mag = abs(z)
                        lines.append(
                            "Load |Z| = {:.4g} ohm at {:.4g} GHz.".format(
                                mag, fmax / 1.0e9))
                        if 0.5 * kappa > 0.2 * mag:
                            # kappa ~ dt*L/(eps*A_transverse), so the gap length
                            # is the knob with the most authority -- said first.
                            lines.append(
                                "The parasitic is a large fraction of the "
                                "load: shorten the gap (kappa grows with its "
                                "length), or coarsen the cells across it, or "
                                "pre-compensate a pure resistance by "
                                "subtracting kappa/2.")
            for text in warnings:
                lines.append("Note: {}.".format(text))
            self._hint.setText(" ".join(lines))

        # -- task-panel protocol ------------------------------------------- #

        def getStandardButtons(self):
            QtWidgets = self._QtWidgets
            buttons = (QtWidgets.QDialogButtonBox.Ok
                       | QtWidgets.QDialogButtonBox.Cancel)
            return int(getattr(buttons, "value", buttons))

        def accept(self):
            doc = self.obj.Document
            # Restore the pre-edit terminals so the transaction records the
            # whole change for undo (the picks above wrote through live).
            new_plus = getattr(self.obj, "TerminalPlus", None)
            new_minus = getattr(self.obj, "TerminalMinus", None)
            new_reverse = bool(getattr(self.obj, "ReversePolarity", False))
            self.obj.TerminalPlus = self._orig_plus
            self.obj.TerminalMinus = self._orig_minus
            self.obj.ReversePolarity = self._orig_reverse
            # The label the object still carries if nobody renamed it -- read
            # before the edits land. See wavesim_gui/labels.py.
            old_auto = "Lumped Port ({})".format(_describe(self.obj))

            doc.openTransaction("Wavesim: Edit Lumped Port")
            self.obj.TerminalPlus = new_plus
            self.obj.TerminalMinus = new_minus
            self.obj.ReversePolarity = new_reverse
            for prop, enable, _key, kind, _label in _BRANCHES:
                check, spin, combo = self._branch_widgets[kind]
                factor = dict(_UNIT_TABLES[kind]).get(combo.currentText(), 1.0)
                setattr(self.obj, enable, bool(check.isChecked()))
                setattr(self.obj, prop, float(spin.value() * factor))
            self.obj.Topology = self._topology.currentText()
            self.obj.Drive = self._drive.currentText()
            self._editor.write(self.obj)
            sync_drive_visibility(self.obj)
            labels_mod.retitle(self.obj, old_auto,
                               "Lumped Port ({})".format(_describe(self.obj)))
            doc.commitTransaction()
            doc.recompute()
            domain_mod.notify_domain_inputs_changed(doc)
            Gui.Control.closeDialog()
            return True

        def reject(self):
            doc = self.obj.Document
            if self.created:
                doc.openTransaction("Wavesim: Cancel Lumped Port")
                doc.removeObject(self.obj.Name)
                doc.commitTransaction()
            else:
                self.obj.TerminalPlus = self._orig_plus
                self.obj.TerminalMinus = self._orig_minus
                self.obj.ReversePolarity = self._orig_reverse
            doc.recompute()
            Gui.Control.closeDialog()
            return True

    def _open_panel(obj, created=False):
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskLumpedPortPanel(obj, created=created))

    # ------------------------------------------------------------------ #
    # Command
    # ------------------------------------------------------------------ #

    class CommandAddLumpedPort:
        """Create a lumped R/L/C port and open its editor."""

        def GetResources(self):
            return {
                "Pixmap": _LUMPED_PORT_ICON,
                "MenuText": "Add Lumped Port",
                "ToolTip": "Add a lumped R/L/C element (optionally with a "
                "voltage or current source) across a gap, between two picked "
                "vertices/faces or along a picked edge",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = active_simulation(doc)
            if sim is None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: create a Simulation before adding a lumped port.\n"
                )
                return
            # Whatever is selected when the button is pressed is almost always
            # the gap the user means, so seed the terminals from it.
            plus, minus = selection_terminals()
            doc.openTransaction("Wavesim: Add Lumped Port")
            try:
                port = doc.addObject("App::FeaturePython", "LumpedPort")
                LumpedPortObject(port)
                if plus is not None:
                    port.TerminalPlus = plus
                if minus is not None:
                    port.TerminalMinus = minus
                port.Label = "Lumped Port ({})".format(_describe(port))
                if port.ViewObject is not None:
                    LumpedPortViewProvider(port.ViewObject)
                sources_group(sim).addObject(port)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()
            _open_panel(port, created=True)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    Gui.addCommand("Wavesim_AddLumpedPort", CommandAddLumpedPort())
