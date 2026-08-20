# -*- coding: utf-8 -*-
"""Results visualisation for the Wavesim workbench (Session 8).

After a successful run, :func:`build_results` adds a "Results" group to the
Simulation tree holding one leaf object per monitor that produced data:

* **Energy** -- the total-domain energy time series.
* **Dissipation** -- the ohmic power P(t) = Σ σ|E|²·dV absorbed by lossy
  material; the companion to Energy (``U(0) − U(t) = ∫P dt``).
* **Probe**  -- a single field component (or magnitude) at one point vs. time.
* **Voltage** / **Current** -- line-integral (V = ∫E·dl / I = ∮H·dl) time series.
* **Snapshot** -- a whole field's 2D slices animated over time, with a dropdown
  choosing the component to view (Ex/Ey/Ez, plus the |E| magnitude derived from
  them) — one monitor, every component.

A port that solves a mode (a Modal Port, or a SPICE port driving a TEM mode) gets
a **group node of its own** under Results, holding everything that port produced:
its mode shape(s) and its V(t)/I(t) series. Those three are one measurement of
one plane -- the V and I are projections onto the very mode the shape draws, and
the reference impedance tying them together is the mode's -- so the tree nests
them rather than scattering three unrelated-looking leaves among the monitors.

Each leaf is self-contained: it stores the run's output directory and the key
of its array inside ``results.npz``, so double-clicking it reopens the plot even
after the document has been saved and reloaded (the run output is kept on disk).

All plotting happens here, on the *FreeCAD* side, using FreeCAD's bundled
matplotlib (3.10) driven through the Qt6/PySide6 ``QtAgg`` backend. The conda
solver is not involved in viewing -- it only wrote ``results.npz`` /
``summary.json``. Plots open in their own non-modal windows (the snapshot view
includes a frame slider and Play control); they are deliberately separate from
the 3D viewport, trading a weaker geometric link for robustness and a UX
consistent across the three result types.

The Results group is a singleton per simulation: re-running refreshes it so the
tree always reflects the latest run.
"""

import os

import FreeCAD

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


def _icon(name):
    return os.path.join(_ICONS_DIR, name)


_RESULTS_ICON = _icon("results.svg")
_RESULT_ICON = _icon("result.svg")

_TYPE_PROP = "WavesimType"
_RESULTS_TYPE = "Results"   # the container group
_RESULT_TYPE = "Result"     # a single result leaf
_PORT_TYPE = "ResultPort"   # a per-port group holding one port's leaves

# Result kinds (stored on each leaf's ResultKind property).
_KIND_ENERGY = "energy"
_KIND_DISSIPATION = "dissipation"
_KIND_PROBE = "probe"
_KIND_SNAPSHOT = "snapshot"
_KIND_MODE = "mode"
_KIND_VOLTAGE = "voltage"
_KIND_CURRENT = "current"
_KIND_SPICE_V = "spice_v"   # SPICE co-simulation port voltage V(t)
_KIND_SPICE_I = "spice_i"   # SPICE co-simulation port current I(t)
_KIND_LUMPED_V = "lumped_v"  # lumped R/L/C port voltage V(t)
_KIND_LUMPED_I = "lumped_i"  # lumped R/L/C port current I(t)
# A modal port's own V(t)/I(t), recorded at its impedance sheet. Distinct kinds
# from the line-integral monitors above because they are a different measurement:
# a projection onto the port's mode rather than an integral along a user curve,
# referenced to the port's own Z_ref -- which the plot says out loud.
_KIND_PORT_V = "port_v"
_KIND_PORT_I = "port_i"
# Z(f) = V(f)/I(f) at a port that recorded both -- modal, SPICE or lumped. One
# leaf per port rather than a view on the V leaf, because it is a different
# measurement of the pair: neither series alone produces it, and it is the
# answer a broadband run was for.
_KIND_IMPEDANCE = "impedance"
# The electrostatic run's scalar results: applied potentials, per-conductor
# charge, field energy and the capacitance matrix. One leaf, because they are one
# answer -- the charges are the matrix's raw material and the energy is the same
# quadratic form.
_KIND_CAPACITANCE = "capacitance"

# Each result leaf shows the toolbar icon of the monitor/port that produced it.
_KIND_ICONS = {
    _KIND_ENERGY: _icon("energy.svg"),
    _KIND_DISSIPATION: _icon("dissipation.svg"),
    _KIND_PROBE: _icon("probe.svg"),
    _KIND_SNAPSHOT: _icon("snapshot.svg"),
    _KIND_MODE: _icon("port_modal.svg"),
    _KIND_VOLTAGE: _icon("voltage.svg"),
    _KIND_CURRENT: _icon("current.svg"),
    _KIND_SPICE_V: _icon("port_spice.svg"),
    _KIND_SPICE_I: _icon("port_spice.svg"),
    _KIND_LUMPED_V: _icon("port_lumped.svg"),
    _KIND_LUMPED_I: _icon("port_lumped.svg"),
    # The monitor icons, not the port's: these rows say "voltage" / "current",
    # and the group they sit in already says which port they belong to.
    _KIND_PORT_V: _icon("voltage.svg"),
    _KIND_PORT_I: _icon("current.svg"),
    _KIND_IMPEDANCE: _icon("impedance.svg"),
    # Not the energy icon: the electrostatic leaf's headline is the matrix, and
    # two different answers sharing a picture is how a tree stops being read.
    _KIND_CAPACITANCE: _icon("capacitance.svg"),
}

_RESULTS_GROUP = "Results"

_MM_PER_M = 1000.0

# In-plane axis labels per snapshot plane (mirrors monitors._PLANES). The first
# axis is the array's first in-plane index, the second its second.
_PLANE_AXES = {"XY": ("x", "y"), "YZ": ("y", "z"), "XZ": ("x", "z")}


# --------------------------------------------------------------------------- #
# Document-object model
# --------------------------------------------------------------------------- #

def _add_type_marker(obj, type_name):
    """Stamp the read-only ``WavesimType`` identity marker on *obj*."""
    if not hasattr(obj, _TYPE_PROP):
        obj.addProperty(
            "App::PropertyString", _TYPE_PROP, "Wavesim",
            "Marks this object as a Wavesim results node",
        )
        setattr(obj, _TYPE_PROP, type_name)
        obj.setEditorMode(_TYPE_PROP, 1)  # read-only identity marker


class ResultsContainer:
    """``Proxy`` for the "Results" group holding one run's result leaves."""

    def __init__(self, obj):
        self.Type = _RESULTS_TYPE
        obj.Proxy = self
        _add_type_marker(obj, _RESULTS_TYPE)
        if not hasattr(obj, "ResultsDir"):
            obj.addProperty(
                "App::PropertyString", "ResultsDir", "Results",
                "Directory holding this run's results.npz / summary.json",
            )
            obj.setEditorMode("ResultsDir", 1)  # informational, read-only

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _RESULTS_TYPE)

    def execute(self, obj):
        pass

    def dumps(self):
        return {"Type": getattr(self, "Type", _RESULTS_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _RESULTS_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


class PortResultContainer:
    """``Proxy`` for one port's group of result leaves under "Results".

    Holds everything a mode-solving port produced -- its mode shape(s) and its
    V(t)/I(t) series -- and carries the port's own scalars, which belong to the
    port rather than to any one of those leaves: ``PortName``, ``PortKind``
    (``modal``/``spice``) and, for a modal port, ``ReferenceImpedance``.
    """

    def __init__(self, obj, kind="modal"):
        self.Type = _PORT_TYPE
        obj.Proxy = self
        _add_type_marker(obj, _PORT_TYPE)
        for prop, ptype, value in (
            ("PortName", "App::PropertyString", ""),
            ("PortKind", "App::PropertyString", str(kind)),
        ):
            if not hasattr(obj, prop):
                obj.addProperty(ptype, prop, "Port", "")
                setattr(obj, prop, value)
                obj.setEditorMode(prop, 1)

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _PORT_TYPE)

    def execute(self, obj):
        pass

    def dumps(self):
        return {"Type": getattr(self, "Type", _PORT_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _PORT_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


class ResultObject:
    """``Proxy`` for one result leaf (energy / probe / snapshot).

    Carries everything needed to (re)open its plot without the producing
    monitor: the run directory (``ResultsDir``), the ``results.npz`` array key
    (``DataKey``) and the kind/component. Snapshot leaves also store the slice's
    physical in-plane extent and axis labels so the animation can be drawn in mm,
    plus the recorded ``Field``/``Components`` the plot's selector offers
    (``Component`` is set only on pre-merge single-component leaves).
    """

    def __init__(self, obj, kind):
        self.Type = _RESULT_TYPE
        obj.Proxy = self
        _add_type_marker(obj, _RESULT_TYPE)

        if not hasattr(obj, "ResultKind"):
            obj.addProperty(
                "App::PropertyString", "ResultKind", "Result",
                "Kind of result: energy, dissipation, probe, snapshot, mode, "
                "voltage, current, impedance or a SPICE port series",
            )
            obj.ResultKind = kind
            obj.setEditorMode("ResultKind", 1)
        if not hasattr(obj, "DataKey"):
            obj.addProperty(
                "App::PropertyString", "DataKey", "Result",
                "Base key of this result's array(s) inside results.npz",
            )
            obj.setEditorMode("DataKey", 1)
        if not hasattr(obj, "ResultsDir"):
            obj.addProperty(
                "App::PropertyString", "ResultsDir", "Result",
                "Directory holding this run's results.npz",
            )
            obj.setEditorMode("ResultsDir", 1)
        if not hasattr(obj, "Component"):
            obj.addProperty(
                "App::PropertyString", "Component", "Result",
                "Field quantity recorded (probe/snapshot)",
            )
            obj.setEditorMode("Component", 1)
        # An impedance leaf is the one kind built from *two* series: DataKey is
        # the voltage's and this is the current's. Empty everywhere else.
        if not hasattr(obj, "CurrentKey"):
            obj.addProperty(
                "App::PropertyString", "CurrentKey", "Result",
                "Key of the paired current array (impedance leaves only)",
            )
            obj.setEditorMode("CurrentKey", 1)

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _RESULT_TYPE)

    def execute(self, obj):
        pass

    def dumps(self):
        return {"Type": getattr(self, "Type", _RESULT_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _RESULT_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


# --------------------------------------------------------------------------- #
# Tree building (called after a successful run)
# --------------------------------------------------------------------------- #

def _is_type(obj, type_name):
    return getattr(obj, _TYPE_PROP, None) == type_name


def find_results(sim):
    """Return the existing Results group under *sim*, or ``None``."""
    if sim is None:
        return None
    for child in sim.Group:
        if _is_type(child, _RESULTS_TYPE):
            return child
    return None


def _remove_results(doc, sim):
    """Delete any existing Results group (and its leaves) under *sim*.

    Walks the tree rather than one level: a port's leaves live in that port's
    own group, and removing the group alone would leave them behind at the
    document root -- visible, undeletable-looking and pointing at a run that has
    just been replaced.
    """
    grp = find_results(sim)
    if grp is None:
        return

    def _purge(node):
        for child in list(getattr(node, "Group", []) or []):
            _purge(child)
        doc.removeObject(node.Name)

    _purge(grp)


def _snapshot_extent(sim, name):
    """Return (width_mm, height_mm, axis_x, axis_y, plane, offset_mm) for the
    snapshot monitor labelled *name*, or ``None`` if it cannot be resolved."""
    from wavesim_gui import monitors as mon_mod

    for snap in mon_mod.find_snapshots(sim):
        if str(snap.Label or snap.Name) != name:
            continue
        corners = list(getattr(snap, "Corners", []) or [])
        plane = str(getattr(snap, "Plane", "XY"))
        offset = float(snap.Offset.Value) if hasattr(snap, "Offset") else 0.0
        ax = _PLANE_AXES.get(plane, ("x", "y"))
        if len(corners) == 4:
            width = (corners[1] - corners[0]).Length
            height = (corners[3] - corners[0]).Length
        else:
            width = height = 0.0
        return (width, height, ax[0], ax[1], plane, offset)
    return None


def _geometry_outlines(obj, chord_mm):
    """Material cross-sections on a snapshot leaf's plane, in the plot's frame.

    Returns ``[(rgb, [polyline, ...], is_pec), ...]``: one entry per Material
    with geometry on the plane, each polyline an ``(N, 2)`` array of
    millimetres in the frame the snapshot is drawn in (world mm less the leaf's
    ``XWorld``/``YWorld``). Empty when the leaf predates those properties, or
    when the document no longer holds the simulation.

    ``is_pec`` is carried because the two overlays built from this section want
    different subsets of it -- every material is outlined, only conductors are
    masked -- and sectioning twice would pay OCC twice for one answer.

    The outline is sectioned from the **live CAD**, not rebuilt from the voxel
    arrays: those hold materials, not boundaries, so an outline derived from
    them would draw the staircase instead of the shape it approximates. A body
    moved since the run therefore draws where it is now -- the same deal the 3D
    view offers.
    """
    import numpy as np
    from wavesim_gui import materials as mat_mod
    from wavesim_gui.commands import active_simulation

    from wavesim_gui import domain as domain_mod

    axes = _PLANE_AXES.get(str(getattr(obj, "Plane", "") or ""))
    if axes is None:
        return []
    sim = active_simulation(obj.Document)
    if sim is None:
        return []
    ax0, ax1 = axes
    x0, y0 = getattr(obj, "XWorld", None), getattr(obj, "YWorld", None)
    if x0 is None or y0 is None:
        # A leaf built before the world frame was stored. The runner crops
        # exactly the PML pad, so the drawn region is the domain interior and
        # its low corner is the Domain's own DomainMin -- right unless the
        # domain has been resized since the run, in which case a stored value
        # would be stale too.
        dom = domain_mod.find_domain(sim)
        dmin = getattr(dom, "DomainMin", None) if dom is not None else None
        if dmin is None:
            return []
        x0, y0 = getattr(dmin, ax0), getattr(dmin, ax1)
    x0, y0 = float(x0), float(y0)
    normal = ({"x", "y", "z"} - set(axes)).pop()
    nvec = FreeCAD.Vector(*[1.0 if a == normal else 0.0 for a in ("x", "y", "z")])
    offset = float(obj.Offset.Value) if hasattr(obj, "Offset") else 0.0

    groups = []
    for mat in mat_mod.find_materials(sim):
        polys = []
        for body in getattr(mat, "Bodies", []) or []:
            shape = getattr(body, "Shape", None)
            if shape is None:
                continue
            try:
                wires = shape.slice(nvec, offset)
            except Exception:
                continue          # a plane that misses the solid, or a bad shape
            for wire in wires or []:
                try:
                    verts = wire.discretize(Deflection=chord_mm)
                except Exception:
                    continue
                if len(verts) < 2:
                    continue
                pts = np.array([(getattr(v, ax0) - x0, getattr(v, ax1) - y0)
                                for v in verts], dtype=float)
                if len(pts) > 2 and wire.isClosed():
                    pts = np.vstack([pts, pts[:1]])   # discretize omits the seam
                polys.append(pts)
        if polys:
            groups.append((mat_mod.material_color(mat), polys,
                           bool(getattr(mat, "Pec", False))))
    return groups


def _pec_rings(groups):
    """The closed conductor rings in a :func:`_geometry_outlines` section.

    Only a **closed** polyline bounds an area, and that function repeats the
    first vertex on a closed wire -- so an open section curve (a plane grazing
    a face) is exactly what fails this test, and filling across its chord would
    invent metal that is not there.

    One helper for both consumers of a PEC section -- the drawn mask and the
    interpolator's dead-cell test -- because the two must not disagree about
    where the metal is: a cell the interpolator treats as dead but the mask
    leaves uncovered shows an invented value, and one covered but left live
    bleeds a zero under the patch.
    """
    import numpy as np

    return [np.asarray(poly, dtype=float)
            for _rgb, polys, is_pec in groups if is_pec
            for poly in polys
            if len(poly) >= 4 and np.allclose(poly[0], poly[-1])]


def _filled_path(rings):
    """One compound path filling *rings*, with nested ones cut out as holes.

    A section through a coax yields two concentric rings and the bore must not
    be filled, so each ring's nesting depth is counted and its winding set to
    match: even depth (an outer boundary) counter-clockwise, odd (a hole)
    clockwise. matplotlib fills by the nonzero winding rule, which is what
    turns that pair into an annulus -- OCC's own wire orientation says nothing
    about which ring is a hole, so it cannot be relied on here. For a validly
    nested section that is the same region :func:`_dead_cells` gets from
    even-odd, which is what keeps the drawn mask and the fill in agreement.

    Returns ``None`` when *rings* is empty.
    """
    import numpy as np
    from matplotlib.path import Path

    if not rings:
        return None

    def _signed_area2(r):
        x, y = r[:-1, 0], r[:-1, 1]
        return float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))

    paths = [Path(r) for r in rings]
    verts, codes = [], []
    for i, ring in enumerate(rings):
        depth = sum(1 for j, other in enumerate(paths)
                    if j != i and other.contains_point(ring[0]))
        if (_signed_area2(ring) > 0.0) != (depth % 2 == 0):
            ring = ring[::-1]
        verts.append(ring)
        codes.extend([Path.MOVETO] + [Path.LINETO] * (len(ring) - 2)
                     + [Path.CLOSEPOLY])
    return Path(np.vstack(verts), codes)


# Half-width of the probe used to decide a sample lying *on* a conductor
# surface, as a fraction of the finest sample spacing. See :func:`_dead_cells`.
_BOUNDARY_PROBE_FRACTION = 1.0e-3


def _dead_cells(rings, cx, cy):
    """Which cell centres sit inside a conductor, in a frame's ``(ny, nx)`` layout.

    *rings* are :func:`_pec_rings`' closed polylines and *cx*/*cy* the sample
    coordinates of the drawn axes, in the same millimetre frame. Returns a bool
    array shaped like the transposed frame, or ``None`` when nothing is inside.

    XOR across the rings, which *is* the even-odd rule: it cuts a coax's bore
    out of its shield for the same reason :func:`_filled_path`'s winding does,
    and agrees with it on any validly nested section. ``scanline.lattice_inside``
    is what makes this affordable -- the fine smoothing lattice would be a
    million ``contains_points`` tests, but the sample coordinates are a lattice
    and each polygon edge costs one ``searchsorted`` per row.

    **The region is closed**: a sample lying exactly *on* a conductor surface
    counts as dead. That is not a detail -- ``gridbuild`` snaps grid lines onto
    every material bbox face, so on a typical model the surfaces land exactly on
    sample coordinates and this case is the common one, not the corner one. A
    single ``lattice_inside`` cannot decide it consistently: it reproduces
    matplotlib's crossing rule, which is not symmetric between the axes for a
    point on the boundary, and so called a capacitor plate's z-faces inside and
    its x-faces outside -- one lump of metal masked on two sides and not the
    other two. It also has to agree with the drawn patch, which fills the closed
    ring: a sample left live under the patch bleeds its zero into the smoother
    from beneath the very thing meant to hide it.

    So the lattice is probed at four diagonal offsets of
    :data:`_BOUNDARY_PROBE_FRACTION` of a sample spacing and the results OR-ed
    (each offset composed across the rings first, so a sample on a *bore* wall
    is caught by the probe that lands in the metal). A sample on any face has at
    least one probe inside; the solid grows by a thousandth of a cell, which is
    below anything the picture can show.
    """
    import numpy as np
    from wavesim_gui import scanline

    if not rings:
        return None
    cx = np.asarray(cx, dtype=float)
    cy = np.asarray(cy, dtype=float)

    def _probe(coords):
        step = np.diff(coords)
        step = step[step > 0.0]
        return float(step.min()) * _BOUNDARY_PROBE_FRACTION if step.size else 0.0

    ex, ey = _probe(cx), _probe(cy)
    inside = None
    for dx, dy in ((-ex, -ey), (-ex, ey), (ex, -ey), (ex, ey)):
        probe = None
        for ring in rings:
            hit = scanline.lattice_inside(ring, cx + dx, cy + dy)   # (nx, ny)
            probe = hit if probe is None else (probe ^ hit)
        inside = probe if inside is None else (inside | probe)
    if not inside.any():
        return None
    return inside.T


# Samples the Catmull-Rom stencil reaches: an output point between source i and
# i+1 gathers i-1 .. i+2, so two layers of fill is exactly enough to keep metal
# out of every tap. Filling deeper would only rewrite cells that are hidden
# under the mask and read by nothing.
_DEAD_FILL_LAYERS = 2


def _shift(arr, axis, step, fill):
    """*arr* shifted by *step* along *axis*, vacated entries set to *fill*.

    Not ``np.roll``: that wraps, which would let a conductor at one edge of the
    slice donate its neighbours to the opposite edge.
    """
    import numpy as np

    out = np.full_like(arr, fill)
    src, dst = [slice(None)] * arr.ndim, [slice(None)] * arr.ndim
    if step > 0:
        dst[axis], src[axis] = slice(step, None), slice(None, -step)
    else:
        dst[axis], src[axis] = slice(None, step), slice(-step, None)
    out[tuple(dst)] = arr[tuple(src)]
    return out


def _fill_dead(data, dead, layers=_DEAD_FILL_LAYERS):
    """*data* with conductor samples replaced by their live neighbours' mean.

    The smoother interpolates between cell centres, and a centre inside metal
    holds a zero the solver put there -- so without this the 4-tap stencil
    drags those zeros up to two cells *outside* the conductor and paints a
    false dark fringe along it, in the staircase's own shape.

    The replacement is a plain outward flood of the nearest live values, one
    layer per pass. Deliberately not a fit to the boundary condition: the
    component dropdown means one artist serves tangential E (which does vanish
    at a PEC), normal E (which does not -- it is the surface charge, often the
    largest thing in the picture), H, and the magnitudes. A rule that forced
    zero at the wall would erase the corner concentration people open these
    plots to see, so the fill invents no zeros and no peaks; its only job is to
    keep the stencil reading physical numbers right up to the mask that hides
    it. Cells the flood never reaches keep 0 -- they are deeper than the
    stencil looks.
    """
    import numpy as np

    val = np.where(dead, 0.0, np.asarray(data, dtype=float))
    known = ~dead
    for _ in range(layers):
        if known.all():
            break
        total = np.zeros_like(val)
        count = np.zeros(val.shape, dtype=np.int32)
        contrib = np.where(known, val, 0.0)
        for axis in (0, 1):
            for step in (-1, 1):
                total += _shift(contrib, axis, step, 0.0)
                count += _shift(known.astype(np.int32), axis, step, 0)
        new = ~known & (count > 0)
        if not new.any():
            break
        val[new] = total[new] / count[new]
        known = known | new
    return val


def _impedance_leaf(new_leaf, keys, name, v_key, i_key, parent=None):
    """Add the ``Z(f)`` leaf for a port that recorded both V(t) and I(t).

    Every port family that records a pair gets one -- modal, SPICE, lumped --
    and they all reach it the same way, so the three loops in
    :func:`build_results` call this rather than each growing a copy. Returns the
    new leaf (for the caller to stamp its port meta on), or ``None`` when either
    series is missing: half a pair is not an impedance, and an empty leaf that
    errors on double-click is worse than no leaf.
    """
    if v_key + "_values" not in keys or i_key + "_values" not in keys:
        return None
    return new_leaf("{} impedance".format(name), _KIND_IMPEDANCE, v_key,
                    parent=parent, current_key=i_key)


def build_results(doc, sim, workdir, summary):
    """(Re)build the Results group under *sim* from a finished run.

    *workdir* holds ``results.npz``; *summary* is the parsed ``summary.json``
    (used for monitor names/components). Returns the Results group, or ``None``
    if nothing could be loaded.
    """
    import numpy as np

    npz_path = os.path.join(workdir, "results.npz")
    if not os.path.isfile(npz_path):
        FreeCAD.Console.PrintWarning(
            "Wavesim: no results.npz to visualise in {}\n".format(workdir)
        )
        return None
    try:
        npz = np.load(npz_path)
        keys = set(npz.files)
    except Exception as exc:
        FreeCAD.Console.PrintError(
            "Wavesim: could not read results.npz ({})\n".format(exc)
        )
        return None

    workdir = workdir.replace("\\", "/")

    doc.openTransaction("Wavesim: Build Results")
    try:
        _remove_results(doc, sim)

        grp = doc.addObject("App::DocumentObjectGroupPython", "Results")
        ResultsContainer(grp)
        grp.Label = "Results"
        grp.ResultsDir = workdir
        if grp.ViewObject is not None:
            ResultsViewProvider(grp.ViewObject)
            grp.ViewObject.Visibility = True
        sim.addObject(grp)

        def _new_leaf(name, kind, data_key, component="", parent=None,
                      current_key=""):
            leaf = doc.addObject("App::FeaturePython", "Result")
            ResultObject(leaf, kind)
            leaf.Label = name
            leaf.DataKey = data_key
            leaf.ResultsDir = workdir
            leaf.Component = component
            leaf.CurrentKey = current_key
            if leaf.ViewObject is not None:
                ResultViewProvider(leaf.ViewObject)
                # A result leaf exists or it does not -- there is no hidden
                # state for it to be in, and a greyed row reads as "this run
                # produced nothing". Its view provider declares a display mode
                # for the same reason (see ResultViewProvider).
                leaf.ViewObject.Visibility = True
            (parent if parent is not None else grp).addObject(leaf)
            return leaf

        # One group node per mode-solving port, holding that port's own leaves.
        # Built here, before any of them, so each loop below can drop its leaf
        # straight into the right group; they are attached to Results at the end
        # so the familiar monitor order is left alone and the ports follow it.
        #
        # A modal port is listed in summary["ports"]; a SPICE port is one only if
        # it solved a mode (kind "tem"), which summary["modes"] is what knows --
        # a SPICE *line* port has no plane and stays flat.
        port_groups = {}     # port name -> group object

        def _port_group(name, kind):
            group = port_groups.get(name)
            if group is None:
                group = doc.addObject("App::DocumentObjectGroupPython", "Port")
                PortResultContainer(group, kind)
                group.Label = name
                group.PortName = name
                if group.ViewObject is not None:
                    PortResultViewProvider(group.ViewObject)
                    group.ViewObject.Visibility = True
                port_groups[name] = group
            return group

        for meta in summary.get("ports", []):
            group = _port_group(str(meta.get("name") or "Port"), "modal")
            _store_port_meta(group, meta)
        for meta in summary.get("modes", []):
            if meta.get("spice"):
                _port_group(str(meta.get("name") or "Port"), "spice")

        # Energy: one leaf per region the run recorded (the whole grid, the
        # PML-free interior, or both as two independent series).
        for data_key, name in (("energy", "Energy (incl. PML)"),
                               ("energy_interior", "Energy (excl. PML)")):
            if data_key + "_values" in keys:
                _new_leaf(name, _KIND_ENERGY, data_key)

        # Dissipation: the same per-region split, one leaf each.
        for data_key, name in (("dissipation", "Dissipation (incl. PML)"),
                               ("dissipation_interior",
                                "Dissipation (excl. PML)")):
            if data_key + "_values" in keys:
                _new_leaf(name, _KIND_DISSIPATION, data_key)

        # Probes (one time series each).
        for idx, meta in enumerate(summary.get("probes", [])):
            if "probe_{}_values".format(idx) not in keys:
                continue
            comp = meta.get("component", "")
            name = meta.get("name") or "Probe {}".format(idx)
            _new_leaf(
                "{} ({})".format(name, comp) if comp else name,
                _KIND_PROBE, "probe_{}".format(idx), comp,
            )

        # Voltage/current line integrals (one time series each).
        for kind, prefix in ((_KIND_VOLTAGE, "voltage"), (_KIND_CURRENT, "current")):
            for idx, meta in enumerate(summary.get(prefix + "s", [])):
                if "{}_{}_values".format(prefix, idx) not in keys:
                    continue
                name = meta.get("name") or "{} {}".format(prefix.title(), idx)
                _new_leaf(name, kind, "{}_{}".format(prefix, idx))

        # SPICE co-simulation ports: a voltage and a current time series each.
        # A TEM port's pair goes under its port group (next to the mode it
        # drives); a line port has no mode and stays a flat pair of leaves.
        for idx, meta in enumerate(summary.get("spice_ports", [])):
            name = meta.get("name") or "SPICE Port {}".format(idx)
            parent = port_groups.get(name)
            if "spice_{}v_values".format(idx) in keys:
                _new_leaf("{} voltage".format(name), _KIND_SPICE_V,
                          "spice_{}v".format(idx), parent=parent)
            if "spice_{}i_values".format(idx) in keys:
                _new_leaf("{} current".format(name), _KIND_SPICE_I,
                          "spice_{}i".format(idx), parent=parent)
            _impedance_leaf(_new_leaf, keys, name, "spice_{}v".format(idx),
                            "spice_{}i".format(idx), parent=parent)

        # Lumped R/L/C ports: the same V(t)/I(t) pair. A lumped port has no mode
        # and so no port group -- it is a line element, and its two leaves sit
        # flat beside the monitors.
        for idx, meta in enumerate(summary.get("lumped_ports", [])):
            name = meta.get("name") or "Lumped Port {}".format(idx)
            for kind, suffix, label in ((_KIND_LUMPED_V, "v", "voltage"),
                                        (_KIND_LUMPED_I, "i", "current")):
                key = "lumped_{}{}".format(idx, suffix)
                if key + "_values" not in keys:
                    continue
                leaf = _new_leaf("{} {}".format(name, label), kind, key)
                _store_lumped_meta(leaf, meta)
            leaf = _impedance_leaf(_new_leaf, keys, name,
                                   "lumped_{}v".format(idx),
                                   "lumped_{}i".format(idx))
            if leaf is not None:
                _store_lumped_meta(leaf, meta)

        # Modal ports: the V(t)/I(t) the port's own impedance sheet recorded --
        # V the modal projection of the plane E, I the Poynting-paired
        # projection of the H one cell inside it, positive into the domain. Both
        # keyed by the port's plane index, the same one its mode shapes carry.
        for meta in summary.get("ports", []):
            name = str(meta.get("name") or "Port")
            si = int(meta.get("source_index", 0))
            parent = port_groups.get(name)
            for kind, suffix, label in ((_KIND_PORT_V, "v", "voltage"),
                                        (_KIND_PORT_I, "i", "current")):
                key = "port_{}{}".format(si, suffix)
                if key + "_values" not in keys:
                    continue
                leaf = _new_leaf("{} {}".format(name, label), kind, key,
                                 parent=parent)
                # The plot annotates Z_ref, so the leaf carries it too rather
                # than reaching back into its group at draw time.
                _store_port_meta(leaf, meta)
            leaf = _impedance_leaf(_new_leaf, keys, name,
                                   "port_{}v".format(si), "port_{}i".format(si),
                                   parent=parent)
            if leaf is not None:
                _store_port_meta(leaf, meta)

        # Snapshots (frame stacks). Capture the slice's physical extent from the
        # producing monitor so the animation can be drawn in millimetres.
        for idx, meta in enumerate(summary.get("snapshots", [])):
            # A snapshot records a whole field as one stack per component; runs
            # from before the merge carry a single unsuffixed stack instead.
            comps = [c for c in meta.get("components", [])
                     if "snapshot_{}_{}_data".format(idx, c) in keys]
            legacy = meta.get("component", "") \
                if "snapshot_{}_data".format(idx) in keys else ""
            if not comps and not legacy:
                continue
            name = meta.get("name") or "Snapshot {}".format(idx)
            leaf = _new_leaf(
                name, _KIND_SNAPSHOT, "snapshot_{}".format(idx), legacy,
            )
            if comps:
                _store_snapshot_components(
                    leaf, meta.get("field", "E"), comps,
                    meta.get("inplane", []),
                )
            extent = _snapshot_extent(sim, name)
            if extent is not None:
                _store_snapshot_extent(leaf, *extent)
            # In-plane node/edge coordinates (metres, solver frame) from the
            # runner: stored on the leaf as mm relative to the slice corner so
            # the plot uses pcolormesh on the real (possibly non-uniform) grid.
            e0k = "snapshot_{}_edges0".format(idx)
            e1k = "snapshot_{}_edges1".format(idx)
            if e0k in keys and e1k in keys:
                _store_edges(leaf, "XEdges", npz[e0k])
                _store_edges(leaf, "YEdges", npz[e1k])
                if extent is not None:
                    # ...and where that (relative) frame sits in the world, so
                    # the plot can draw the CAD cross-section over it.
                    _store_snapshot_world(leaf, sim, extent[2], extent[3],
                                          npz[e0k][0], npz[e1k][0])

        # Electrostatics: one leaf for the scalar results. Created whenever the
        # run was electrostatic, even with no capacitance matrix -- the applied
        # potentials, the conductor charges and the field energy are the answer
        # the run was for, and a run that produced them and showed nothing would
        # look like a failure.
        es_meta = summary.get("electrostatic")
        if es_meta:
            leaf = _new_leaf("Capacitance matrix"
                             if es_meta.get("capacitance")
                             else "Electrostatic solution",
                             _KIND_CAPACITANCE, "capacitance")
            _store_electrostatic_meta(leaf, es_meta)

        # Port modes (one leaf per solved port mode). Each opens a figure of the
        # mode shape plus the port's per-unit-length parameters.
        for meta in summary.get("modes", []):
            key = "mode_{}_{}".format(
                meta.get("source_index", 0), meta.get("mode_index", 0)
            )
            if key + "_phi" not in keys:
                continue
            port_name = str(meta.get("name", "port"))
            # Name the conductor the way the user does when the port's table
            # picked it; the solver's raster-order label is what is left when
            # nothing named it (a legacy port, or a mode no row claimed).
            cond = meta.get("conductor") if meta.get("driven") else None
            name = "{} mode ({})".format(
                port_name,
                cond if cond else
                "conductor {}".format(meta.get("conductor_id", "?")))
            leaf = _new_leaf(name, _KIND_MODE, key, "",
                             parent=port_groups.get(port_name))
            _store_mode_meta(leaf, meta)
            # Transverse cell-centre coordinates (metres, solver frame) from the
            # runner: stored as absolute mm so the plot draws the mode on the
            # real (possibly non-uniform) transverse axes.
            cak, cbk = key + "_ca", key + "_cb"
            if cak in keys and cbk in keys:
                _store_edges(leaf, "CoordsA", npz[cak], relative=False,
                             group="Mode")
                _store_edges(leaf, "CoordsB", npz[cbk], relative=False,
                             group="Mode")

        # The port groups join Results last, so they follow the monitor leaves
        # in the tree instead of pushing them down. A group that ended up empty
        # (a port whose arrays are all missing) is dropped rather than shown as
        # an empty folder.
        for group in port_groups.values():
            if group.Group:
                grp.addObject(group)
            else:
                doc.removeObject(group.Name)
    except Exception:
        doc.abortTransaction()
        raise
    doc.commitTransaction()
    doc.recompute()
    FreeCAD.Console.PrintMessage(
        "Wavesim: results added to the tree (double-click a node to plot).\n"
    )
    return grp


def _store_snapshot_components(leaf, field, comps, inplane):
    """Record which field (and components) a snapshot leaf's stacks hold.

    The plot window offers these plus the derived magnitude; an empty
    ``Components`` marks a pre-merge single-component leaf, which falls back to
    its ``Component``. ``InPlane`` names the two components lying in the slice
    plane (in array-index order), which the quiver overlay draws as a vector.
    """
    for prop, value in (("Field", str(field)),
                        ("Components", ",".join(comps)),
                        ("InPlane", ",".join(inplane))):
        if not hasattr(leaf, prop):
            leaf.addProperty("App::PropertyString", prop, "Snapshot", "")
            leaf.setEditorMode(prop, 1)
        setattr(leaf, prop, value)


def _store_snapshot_extent(leaf, width, height, axis_x, axis_y, plane, offset):
    """Stash a snapshot slice's physical extent on its result leaf."""
    if not hasattr(leaf, "InPlaneSize"):
        leaf.addProperty(
            "App::PropertyVector", "InPlaneSize", "Snapshot",
            "Slice size (width, height, 0) in mm",
        )
        leaf.setEditorMode("InPlaneSize", 1)
    leaf.InPlaneSize = FreeCAD.Vector(width, height, 0.0)
    for prop, value in (
        ("AxisX", axis_x), ("AxisY", axis_y), ("Plane", plane),
    ):
        if not hasattr(leaf, prop):
            leaf.addProperty("App::PropertyString", prop, "Snapshot", "")
            leaf.setEditorMode(prop, 1)
        setattr(leaf, prop, value)
    if not hasattr(leaf, "Offset"):
        leaf.addProperty(
            "App::PropertyDistance", "Offset", "Snapshot",
            "Plane offset along its normal axis (mm)",
        )
        leaf.setEditorMode("Offset", 1)
    leaf.Offset = "{} mm".format(offset)


def _store_snapshot_world(leaf, sim, axis_x, axis_y, first_x_m, first_y_m):
    """Stash the world-mm position of the drawn slice's (0, 0) corner.

    The stored edges are relative to the first *drawn* cell edge -- the
    interior low corner, PML cropped -- while the CAD is in world millimetres.
    This pair is the only thing that maps between them, and it is knowable only
    here: the leaf has no idea where the grid origin was, and the Domain's node
    arrays (which do, in world mm) may have moved by the time the plot opens.
    """
    from wavesim_gui import domain as domain_mod

    dom = domain_mod.find_domain(sim)
    if dom is None:
        return
    nodes = dict(zip(("x", "y", "z"), domain_mod.node_coords_mm(dom)))
    origin_x, origin_y = nodes.get(axis_x) or [], nodes.get(axis_y) or []
    if not origin_x or not origin_y:
        return
    for prop, value in (
        ("XWorld", origin_x[0] + float(first_x_m) * _MM_PER_M),
        ("YWorld", origin_y[0] + float(first_y_m) * _MM_PER_M),
    ):
        if not hasattr(leaf, prop):
            leaf.addProperty("App::PropertyFloat", prop, "Snapshot",
                             "World position (mm) of the drawn slice's origin")
            leaf.setEditorMode(prop, 1)
        setattr(leaf, prop, value)


def _store_edges(leaf, prop, coords_m, relative=True, group="Snapshot"):
    """Stash a coordinate array (solver-frame metres) on a leaf as an mm list.

    *relative* subtracts the first coordinate (used for snapshot edges, which are
    drawn from the slice corner at 0); mode transverse coordinates keep their
    absolute position. Stored as a read-only ``App::PropertyFloatList`` so it
    survives save/reload with the run output.
    """
    vals = [float(v) for v in coords_m]
    if not vals:
        return
    origin = vals[0] if relative else 0.0
    mm = [(v - origin) * _MM_PER_M for v in vals]
    if not hasattr(leaf, prop):
        leaf.addProperty("App::PropertyFloatList", prop, group,
                         "Axis coordinates (mm)")
        leaf.setEditorMode(prop, 1)
    setattr(leaf, prop, mm)


def _store_mode_meta(leaf, meta):
    """Stash a solved TEM mode's geometry + per-unit-length parameters on a leaf.

    These read-only properties carry everything the figure needs to draw the
    mode shape (cell sizes, transverse axes, E-component keys) and to report the
    port parameters (Z0, eps_eff, C, L, v) without re-reading ``summary.json``.
    """
    def _add(prop, kind, value, group="Mode"):
        if not hasattr(leaf, prop):
            leaf.addProperty(kind, prop, group, "")
            leaf.setEditorMode(prop, 1)
        setattr(leaf, prop, value)

    axes = meta.get("transverse_axes", ["a", "b"])
    _add("AxisA", "App::PropertyString", str(axes[0]))
    _add("AxisB", "App::PropertyString", str(axes[1]))
    _add("Da", "App::PropertyFloat", float(meta.get("da", 0.0)))
    _add("Db", "App::PropertyFloat", float(meta.get("db", 0.0)))
    _add("PortName", "App::PropertyString", str(meta.get("name", "")))
    _add("Normal", "App::PropertyString", str(meta.get("normal", "")))
    _add("ModePosition", "App::PropertyFloat", float(meta.get("position", 0.0)))
    _add("ConductorId", "App::PropertyInteger", int(meta.get("conductor_id", 0)))
    # The conductor the port's table named, when one did. Empty for a legacy
    # port and for any mode no drive row claimed -- then ConductorId, the
    # solver's own raster-order label, is all there is.
    _add("Conductor", "App::PropertyString",
         str(meta.get("conductor", "") if meta.get("driven") else ""))
    _add("Ecomps", "App::PropertyString", ",".join(meta.get("Ecomps", [])))

    # Per-unit-length parameters may be None (params skipped / degenerate solve);
    # store NaN so the figure can detect and omit them.
    def _num(value):
        return float("nan") if value is None else float(value)

    _add("Impedance", "App::PropertyFloat", _num(meta.get("impedance")))
    _add("EpsEff", "App::PropertyFloat", _num(meta.get("eps_eff")))
    _add("Capacitance", "App::PropertyFloat", _num(meta.get("capacitance")))
    _add("Inductance", "App::PropertyFloat", _num(meta.get("inductance")))
    _add("VPhase", "App::PropertyFloat", _num(meta.get("v_phase")))
    _add("Fmax", "App::PropertyFloat", float(meta.get("fmax", 0.0)))
    _add("Amplitude", "App::PropertyFloat", float(meta.get("amplitude", 1.0)))
    _add("Fields", "App::PropertyString", str(meta.get("fields", "")))


def _store_port_meta(obj, meta):
    """Stash a modal port's own scalars on its group node / V-I leaves.

    ``ReferenceImpedance`` is ``Z_ref = 1/(s·G)`` -- the impedance the port's
    (V, I) pair is self-consistent against, so ``a = (V + Z_ref·I)/2`` and
    ``b = (V − Z_ref·I)/2`` are the incident and outgoing wave amplitudes. It is
    the mode's Z₀ whenever the solver derived the admittance scale (it always
    does here), but it is *this* number the two series are referenced to, so
    this is the one stored beside them. NaN when the run predates it.
    """
    def _add(prop, kind, value, group="Port"):
        if not hasattr(obj, prop):
            obj.addProperty(kind, prop, group, "")
            obj.setEditorMode(prop, 1)
        setattr(obj, prop, value)

    def _num(value):
        return float("nan") if value is None else float(value)

    _add("PortName", "App::PropertyString", str(meta.get("name", "")))
    _add("ReferenceImpedance", "App::PropertyFloat",
         _num(meta.get("reference_impedance")))
    _add("ModalConductance", "App::PropertyFloat",
         _num(meta.get("modal_conductance")))


def _store_lumped_meta(obj, meta):
    """Stash a lumped port's network description and its cell's gap C on its leaf.

    ``Network`` is what the element *was* (the branches, their wiring and any
    drive), ``SelfCoupling`` is the ``kappa`` the solver measured for it on this
    grid, and ``CellCapacitance`` is the ``dt/kappa = eps*dA/dl`` the bridged
    cells keep in **parallel** with the element. All three ride on the leaf
    because the plot annotates them: a V/I pair read against a nominal 50 ohm is
    off by that shunt (the element itself delivers exactly its nominal value),
    and the number belongs to the run rather than to the document, which the user
    may have edited since.
    """
    def _add(prop, kind, value, group="Port"):
        if not hasattr(obj, prop):
            obj.addProperty(kind, prop, group, "")
            obj.setEditorMode(prop, 1)
        setattr(obj, prop, value)

    parts = []
    for key, kind, unit in (("resistance", "R", "ohm"),
                            ("inductance", "L", "H"),
                            ("capacitance", "C", "F")):
        if meta.get(key) is not None:
            parts.append("{} {:g} {}".format(kind, float(meta[key]), unit))
    text = " + ".join(parts) if parts else "no load"
    if len(parts) > 1:
        text += " ({})".format(meta.get("topology", "series"))
    drive = str(meta.get("drive", "none"))
    if drive != "none":
        text += ", {} drive".format(drive)

    _add("PortName", "App::PropertyString", str(meta.get("name", "")))
    _add("Network", "App::PropertyString", text)
    _add("SelfCoupling", "App::PropertyFloat",
         float("nan") if meta.get("kappa") is None else float(meta["kappa"]))
    _add("CellCapacitance", "App::PropertyFloat",
         float("nan") if meta.get("c_cell") is None else float(meta["c_cell"]))


def _store_electrostatic_meta(leaf, meta):
    """Stash an electrostatic run's scalar results on its leaf.

    Everything the window shows lives on the object, not in ``summary.json``, so
    the leaf still opens after the document is reloaded and the run directory has
    been cleaned out -- the same reason a mode leaf carries its own parameters.
    The matrix is stored flattened row-major with its conductor names beside it;
    the two are read back together, so the width never has to be guessed.
    """
    def _add(prop, kind, value, group="Electrostatic"):
        if not hasattr(leaf, prop):
            leaf.addProperty(kind, prop, group, "")
            leaf.setEditorMode(prop, 1)
        setattr(leaf, prop, value)

    charges = meta.get("charges") or {}
    potentials = meta.get("potentials") or {}
    names = sorted(set(charges) | set(potentials))
    _add("Conductors", "App::PropertyStringList", names)
    _add("Potentials", "App::PropertyFloatList",
         [float(potentials.get(n, 0.0)) for n in names])
    _add("Charges", "App::PropertyFloatList",
         [float(charges.get(n, 0.0)) for n in names])
    _add("FieldEnergy", "App::PropertyFloat", float(meta.get("energy", 0.0)))
    _add("SolveMethod", "App::PropertyString", str(meta.get("method", "")))
    _add("Iterations", "App::PropertyInteger", int(meta.get("iterations", 0)))
    _add("Unknowns", "App::PropertyInteger", int(meta.get("unknowns", 0)))

    cap = meta.get("capacitance") or {}
    cap_names = list(cap.get("names") or [])
    maxwell = cap.get("maxwell") or []
    _add("CapNames", "App::PropertyStringList", cap_names)
    _add("CapMaxwell", "App::PropertyFloatList",
         [float(v) for row in maxwell for v in row])
    # Conductors the run found fused into one body, each group reported as one
    # comma-joined entry: they are why a named part may be missing from the
    # matrix, and the answer is a modelling fix rather than a solver setting.
    _add("CapFused", "App::PropertyStringList",
         [", ".join(group) for group in (cap.get("fused") or [])])


# --------------------------------------------------------------------------- #
# GUI: view providers + matplotlib plot windows
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

    from wavesim_gui import visibility

    # Keep plot windows alive: a QDialog with no Python reference is garbage
    # collected and vanishes immediately. Pruned lazily of closed windows.
    _OPEN_WINDOWS = []

    def _register_window(win):
        _OPEN_WINDOWS[:] = [w for w in _OPEN_WINDOWS if _is_visible(w)]
        _OPEN_WINDOWS.append(win)

    def _is_visible(win):
        try:
            return win.isVisible()
        except RuntimeError:  # underlying C++ object already deleted
            return False

    def _cleanup_window(dialog):
        """Release a plot window's resources when it is closed.

        Without this a closed window leaks: its animation ``QTimer`` keeps firing
        (redrawing a hidden canvas every 100 ms -> growing sluggishness) and its
        matplotlib figure / frame arrays stay alive because the QDialog is owned
        by its parent (the main window). Stops the timer, clears the figure and
        drops our reference; combined with ``WA_DeleteOnClose`` the C++ object is
        then destroyed too.
        """
        timer = getattr(dialog, "_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except RuntimeError:
                pass
        figure = getattr(dialog, "_figure", None)
        if figure is not None:
            try:
                figure.clear()
            except Exception:
                pass
        try:
            _OPEN_WINDOWS.remove(dialog)
        except ValueError:
            pass

    class ResultsViewProvider(visibility.DisplayModeMixin):
        """Tree icon for the Results group (no 3D geometry, no editor)."""

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            self.ViewObject = vobj
            self.Object = vobj.Object
            self.attach_display_mode(vobj)

        def getIcon(self):
            return _RESULTS_ICON

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class PortResultViewProvider(visibility.DisplayModeMixin):
        """Tree icon for a port's result group -- its own toolbar icon.

        The group is a folder, so it has no plot of its own: what it offers is
        the port's scalars in the property editor (Z_ref) and its leaves under
        it. The icon says which kind of port produced them.
        """

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            self.ViewObject = vobj
            self.Object = vobj.Object
            self.attach_display_mode(vobj)

        def getIcon(self):
            obj = getattr(self, "Object", None)
            kind = str(getattr(obj, "PortKind", "modal"))
            return _icon("port_spice.svg" if kind == "spice"
                         else "port_modal.svg")

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class ResultViewProvider(visibility.DisplayModeMixin):
        """Tree view provider for a result leaf; double-click opens its plot.

        A leaf has nothing to show in 3D, but it is not *hidden* either -- it
        either exists or the run did not produce it. The display mode is what
        keeps the tree from greying the row (see visibility.DisplayModeMixin).
        """

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            self.ViewObject = vobj
            self.Object = vobj.Object
            self.attach_display_mode(vobj)

        def getIcon(self):
            obj = getattr(self, "Object", None)
            kind = str(getattr(obj, "ResultKind", ""))
            return _KIND_ICONS.get(kind, _RESULT_ICON)

        def setEdit(self, vobj, mode=0):
            open_result(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            open_result(vobj.Object)
            return True

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    # ------------------------------------------------------------------ #
    # matplotlib plumbing
    # ------------------------------------------------------------------ #

    def _qt():
        try:
            from PySide import QtCore, QtWidgets
        except ImportError:
            from PySide import QtCore
            from PySide import QtGui as QtWidgets
        return QtCore, QtWidgets

    def _mpl():
        """Import matplotlib's Qt6 backend, returning (FigureCanvas, Toolbar,
        Figure). Raises on failure so callers can show an error dialog."""
        os.environ.setdefault("QT_API", "pyside6")
        import matplotlib
        try:
            matplotlib.use("QtAgg", force=False)
        except Exception:
            pass
        from matplotlib.backends.backend_qtagg import (
            FigureCanvasQTAgg, NavigationToolbar2QT,
        )
        from matplotlib.figure import Figure
        return FigureCanvasQTAgg, NavigationToolbar2QT, Figure

    def _load_array(workdir, key):
        """Return the named array from ``<workdir>/results.npz`` (or None)."""
        import numpy as np
        try:
            data = np.load(os.path.join(workdir, "results.npz"))
            return data[key] if key in data.files else None
        except Exception:
            return None

    def _make_window(title):
        """Create a non-modal plot window with an embedded matplotlib canvas.

        Returns (dialog, figure, vbox_layout) -- the caller adds extra controls
        to the layout. Returns ``None`` if matplotlib could not be loaded.
        """
        _QtCore, QtWidgets = _qt()
        try:
            FigureCanvas, NavToolbar, Figure = _mpl()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                Gui.getMainWindow(), "Wavesim Results",
                "Could not load matplotlib for plotting:\n{}".format(exc),
            )
            return None

        dialog = QtWidgets.QDialog(Gui.getMainWindow())
        dialog.setWindowTitle(title)
        dialog.setWindowFlags(_QtCore.Qt.Window)
        dialog.resize(640, 480)
        # Destroy the C++ object on close so it (and its figure/canvas/timer) is
        # freed rather than lingering as a hidden child of the main window.
        dialog.setAttribute(_QtCore.Qt.WA_DeleteOnClose, True)
        layout = QtWidgets.QVBoxLayout(dialog)

        figure = Figure(figsize=(6, 4.5), tight_layout=True)
        canvas = FigureCanvas(figure)
        toolbar = NavToolbar(canvas, dialog)
        layout.addWidget(toolbar)
        layout.addWidget(canvas)

        dialog._figure = figure   # keep refs on the dialog
        dialog._canvas = canvas
        dialog._timer = None      # set by the snapshot animator, if any
        # Stop the timer / release the figure when the window is closed.
        dialog.finished.connect(lambda _result, d=dialog: _cleanup_window(d))
        return dialog, figure, layout

    def _time_unit(obj):
        return units.get_time_unit(active_simulation(obj.Document))

    # ------------------------------------------------------------------ #
    # Per-kind plotters
    # ------------------------------------------------------------------ #

    def open_result(obj):
        """Dispatch a result leaf to its plot window by ResultKind."""
        kind = str(getattr(obj, "ResultKind", ""))
        try:
            if kind == _KIND_ENERGY:
                _plot_energy(obj)
            elif kind == _KIND_DISSIPATION:
                _plot_dissipation(obj)
            elif kind == _KIND_PROBE:
                _plot_probe(obj)
            elif kind == _KIND_VOLTAGE:
                _plot_voltage(obj)
            elif kind == _KIND_CURRENT:
                _plot_current(obj)
            elif kind == _KIND_SPICE_V:
                _plot_spice_voltage(obj)
            elif kind == _KIND_SPICE_I:
                _plot_spice_current(obj)
            elif kind == _KIND_LUMPED_V:
                _plot_lumped_voltage(obj)
            elif kind == _KIND_LUMPED_I:
                _plot_lumped_current(obj)
            elif kind == _KIND_PORT_V:
                _plot_port_voltage(obj)
            elif kind == _KIND_PORT_I:
                _plot_port_current(obj)
            elif kind == _KIND_IMPEDANCE:
                _plot_impedance(obj)
            elif kind == _KIND_SNAPSHOT:
                _plot_snapshot(obj)
            elif kind == _KIND_MODE:
                _plot_mode(obj)
            elif kind == _KIND_CAPACITANCE:
                _show_electrostatic(obj)
            else:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: unknown result kind '{}'.\n".format(kind)
                )
        except Exception as exc:
            _QtCore, QtWidgets = _qt()
            FreeCAD.Console.PrintError(
                "Wavesim: failed to plot result: {}\n".format(exc)
            )
            QtWidgets.QMessageBox.critical(
                Gui.getMainWindow(), "Wavesim Results",
                "Could not plot this result:\n{}".format(exc),
            )

    def _missing(obj):
        _QtCore, QtWidgets = _qt()
        QtWidgets.QMessageBox.warning(
            Gui.getMainWindow(), "Wavesim Results",
            "The result data is missing. The run output may have been moved "
            "or deleted:\n{}".format(getattr(obj, "ResultsDir", "?")),
        )

    def _plot_series(obj, ylabel, title, color, annotate=None, quantity=None):
        """1D time-series plot shared by the energy/probe/voltage/current leaves.

        Reads ``<DataKey>_times`` / ``<DataKey>_values`` from the leaf's
        ``results.npz`` and draws them in a non-modal window. *annotate*, if
        given, is called as ``annotate(ax, times_si, values)`` once the curve is
        drawn, for a per-kind derived quantity (see :func:`_plot_dissipation`).

        *quantity* (``'V'`` or ``'I'``) marks a port-like series and adds the
        **domain switch**: the same record seen in time or in frequency, in one
        window rather than two leaves. It names the quantity because that is
        what fixes the transform's half-step stagger -- a voltage is E-derived
        and a current H-derived, and getting it wrong is what turns a lossless
        structure's Z into one with a resistance (see
        :mod:`wavesim_gui.spectrum`). Leaves that are neither (energy,
        dissipation, probe) keep the plain time plot.
        """
        workdir = str(obj.ResultsDir)
        key = str(obj.DataKey)
        times = _load_array(workdir, key + "_times")
        values = _load_array(workdir, key + "_values")
        if times is None or values is None:
            _missing(obj)
            return

        made = _make_window("Wavesim Results - {}".format(obj.Label))
        if made is None:
            return
        dialog, figure, layout = made

        def draw_time(fig):
            unit = _time_unit(obj)
            t = [units.time_from_si(float(v), unit) for v in times]
            ax = fig.add_subplot(111)
            ax.plot(t, values, color=color)
            ax.set_xlabel("time ({})".format(unit))
            ax.set_ylabel(ylabel)
            ax.set_title(title)
            ax.grid(True, alpha=0.3)
            if annotate is not None:
                annotate(ax, times, values)

        if quantity is None:
            draw_time(figure)
        else:
            _add_domain_switch(dialog, layout, draw_time,
                               lambda fig, opts: _draw_series_spectrum(
                                   fig, obj, times, values, quantity, color,
                                   opts))
        dialog._canvas.draw()
        dialog.show()
        _register_window(dialog)

    # ------------------------------------------------------------------ #
    # Frequency domain
    # ------------------------------------------------------------------ #

    #: Window tapers offered in the frequency views, in the order shown.
    #: Tukey leads the non-trivial ones deliberately: an FDTD port record is
    #: front-loaded (the drive lands in the first few percent, the rest is
    #: decay), so a symmetric Hann puts its steep rising edge on top of the
    #: excitation and reshapes it, while a Tukey is flat across the record and
    #: tapers only the tail where the ringing actually is.
    _WINDOW_CHOICES = (
        ("none (as recorded)", None),
        ("Tukey", "tukey"),
        ("Exponential", "exponential"),
        ("Hann", "hann"),
        ("Hamming", "hamming"),
    )

    #: Out-of-band cutoff: a bin counts as carrying signal when it stands at
    #: least this fraction of its own spectrum's peak. Sets the default x-limit
    #: and, for a ratio, which bins are worth dividing.
    _BAND_FLOOR = 1e-3

    #: Warn on the figure when the last 5% of a record still reaches this much
    #: of its peak -- the transform of a record that ends mid-ring smears every
    #: sharp feature across neighbouring bins.
    _DECAY_TOL = 0.01

    def _freq_unit(obj):
        return units.get_frequency_unit(active_simulation(obj.Document))

    def _spectrum_of(times, values, quantity, opts):
        """Transform one recorded series under the Window box's current choice.

        *quantity* is ``'V'`` or ``'I'``; it chooses the stagger the transform
        divides back out, and the unit the spectrum announces.
        """
        from wavesim_gui import spectrum as spec

        stagger = spec.STAGGER_H if quantity == "I" else spec.STAGGER_E
        unit = "A" if quantity == "I" else "V"
        return spec.spectrum(times, values, window=opts.get("window"),
                             stagger=stagger, label=quantity, unit=unit)

    def _set_band_xlim(ax, spectra, scale):
        """Limit the frequency axis to the band the data actually supports.

        An rfft of an FDTD record runs to Nyquist -- hundreds of GHz for a
        microwave mesh -- typically far past anything the excitation
        illuminated. Left to autoscale, every one of these plots would be a
        spike in the leftmost pixel column.
        """
        import numpy as np

        from wavesim_gui import spectrum as spec

        _lo, hi = spec.usable_band(*spectra, floor=_BAND_FLOOR)
        if np.isfinite(hi) and hi > 0.0:
            ax.set_xlim(0.0, 1.05 * hi / scale)

    def _fit_ylim(ax, freqs, mag, scale, floor_db=100.0):
        """Scale a magnitude axis to what is *inside* the frequency window.

        Autoscaling would take in the whole rfft, which runs to Nyquist -- for a
        microwave mesh, hundreds of GHz of out-of-band numerical floor that the
        x-limit already hides. The visible curve then occupies the top decade of
        a ten-decade axis. Floored at *floor_db* below the peak as well, so a
        band edge that dives into round-off cannot stretch the axis either.

        Returns ``True`` if a log axis is worth it -- the visible curve spans
        more than a decade. Under that, a log axis labels a flat 50 Ω line
        "5.06 × 10¹", which is not a reading.
        """
        import numpy as np

        hi = ax.get_xlim()[1] * scale
        m = mag[(freqs <= hi) & np.isfinite(mag) & (mag > 0.0)]
        if not m.size:
            return False
        peak, low = float(m.max()), float(m.min())
        low = max(low, peak * 10.0 ** (-floor_db / 20.0))
        wide = peak > 10.0 * low
        if wide:
            ax.set_ylim(low / 2.0, peak * 2.0)
        return wide

    def _shade_band(ax, spectra, scale, unit):
        """Shade the frequencies where every given spectrum clears the floor.

        Outside it the excitation put no energy, so any Z read there would be
        the quotient of two round-off numbers -- which is why the impedance
        curves are blank there rather than wild.
        """
        import numpy as np

        from wavesim_gui import spectrum as spec

        lo, hi = spec.usable_band(*spectra, floor=_BAND_FLOOR)
        if not (np.isfinite(lo) and np.isfinite(hi) and hi > lo):
            return
        ax.axvspan(lo / scale, hi / scale, color="0.5", alpha=0.12, zorder=0,
                   label="excited band {:.3g}-{:.3g} {}".format(
                       lo / scale, hi / scale, unit))

    def _decay_warning(records, opts):
        """The "this record ends mid-ring" note, or ``None``.

        Silent once a window is on: the taper is the answer to it, and a note
        that never goes away stops being read.
        """
        from wavesim_gui import spectrum as spec

        if opts.get("window") is not None:
            return None
        worst = max((spec.tail_ratio(v) for v in records), default=0.0)
        if worst <= _DECAY_TOL:
            return None
        return ("record ends mid-ring ({:.0%} of peak in the last 5%)\n"
                "-- features smear; try a Tukey window or a longer run"
                .format(worst))

    def _redraw_guarded(dialog, paint):
        """Run *paint* on the window's figure, showing a failure in the figure.

        The switches call this from Qt slots, where an escaping exception is
        not merely a failed plot -- it surfaces as an unhandled error in the
        event loop, and the window is left showing the *previous* view's
        picture under the new combo setting. A record the transform rejects
        (non-uniform timestamps, a single sample) says so on the canvas
        instead, and the box can be moved back.
        """
        figure = dialog._figure
        figure.clear()
        try:
            paint(figure)
        except Exception as exc:
            FreeCAD.Console.PrintError(
                "Wavesim: could not draw this view: {}\n".format(exc))
            figure.clear()
            ax = figure.add_subplot(111)
            ax.axis("off")
            ax.text(0.5, 0.5, "Could not draw this view:\n{}".format(exc),
                    ha="center", va="center", wrap=True, fontsize=9)
        dialog._canvas.draw_idle()

    def _corner_text(ax, lines, va="top", y=0.95):
        """Draw *lines* in a boxed corner note, the shared annotation style."""
        if not lines:
            return
        ax.text(0.98, y, "\n".join(lines), transform=ax.transAxes,
                ha="right", va=va, fontsize=9,
                bbox=dict(boxstyle="round", fc="white", ec="0.7", alpha=0.85))

    def _draw_series_spectrum(figure, obj, times, values, quantity, color,
                              opts):
        """|X(f)| or its phase for one recorded series -- the frequency half of
        a voltage/current window.

        The companion to the time plot, not a replacement: this is where the
        excitation's band is legible, which is the first thing to check before
        reading any Z off the same run.
        """
        import numpy as np

        s = _spectrum_of(times, values, quantity, opts)
        funit = _freq_unit(obj)
        scale = units.freq_to_si(1.0, funit)

        ax = figure.add_subplot(111)
        f = s.freqs / scale
        view = opts.get("view")
        if view == "phase":
            ax.plot(f, s.phase_deg(), color=color,
                    label="∠{}(f)".format(quantity))
            ax.set_ylabel("phase of {}(f) (deg)".format(quantity))
            # Unwrapped, so this runs to many turns for a delayed pulse and the
            # ±90° guides the Z panel draws would mean nothing here. Only zero
            # is a landmark.
            ax.axhline(0.0, color="0.6", lw=0.7, ls=":")
        elif view == "db":
            db = s.db
            ax.plot(f, db, color=color, label="|{}(f)|".format(quantity))
            ax.set_ylabel("|{}(f)| (dB re 1 {}/Hz)".format(quantity, s.unit))
            finite = db[np.isfinite(db)]
            if finite.size:
                # A fixed 100 dB below the peak rather than autoscaling: deep
                # enough to show a notch, shallow enough to keep the numerical
                # floor and its noise out of the picture.
                top = float(finite.max())
                ax.set_ylim(top - 100.0, top + 6.0)
        else:
            ax.plot(f, s.magnitude, color=color,
                    label="|{}(f)|".format(quantity))
            ax.set_ylabel("|{}(f)| ({}/Hz)".format(quantity, s.unit))

        _shade_band(ax, [s], scale, funit)
        _set_band_xlim(ax, [s], scale)
        if view not in ("phase", "db"):
            # After the x-limit, which is what "visible" means here.
            ax.set_yscale("log" if _fit_ylim(ax, s.freqs, s.magnitude, scale)
                          else "linear")
        ax.set_xlabel("frequency ({})".format(funit))
        ax.set_title("{} spectrum".format(str(obj.Label)))
        ax.grid(True, which="both", alpha=0.3)
        ax.legend(fontsize=9, loc="upper left")
        _corner_text(ax, [t for t in (_decay_warning([values], opts),) if t])

    def _add_domain_switch(dialog, layout, draw_time, draw_freq):
        """Add the "View / Window" control row that swaps a window's domain.

        One window, two domains: the time series a run recorded and its
        transform are the same measurement, and making the user open a second
        leaf to see the other one is how the two stop being compared. *draw_time*
        takes the figure; *draw_freq* takes the figure and the options dict.
        The Window box only means anything to the transform, so it is greyed in
        the time view rather than removed -- it stays where the eye left it.
        """
        _QtCore, QtWidgets = _qt()
        figure = dialog._figure

        opts = {"view": "time", "window": None}
        views = (("Time domain", "time"),
                 ("Spectrum |X(f)|", "mag"),
                 ("Spectrum |X(f)| (dB)", "db"),
                 ("Spectrum phase", "phase"))

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("View:"))
        view_box = QtWidgets.QComboBox()
        for text, _key in views:
            view_box.addItem(text)
        view_box.setToolTip(
            "The same record in time or in frequency. The spectrum is what\n"
            "shows the band the excitation actually covered -- outside it a\n"
            "V/I ratio is the quotient of two round-off numbers."
        )
        row.addWidget(view_box)

        row.addSpacing(12)
        row.addWidget(QtWidgets.QLabel("Window:"))
        win_box = QtWidgets.QComboBox()
        for text, _key in _WINDOW_CHOICES:
            win_box.addItem(text)
        win_box.setEnabled(False)
        win_box.setToolTip(
            "Taper applied before transforming. Leave it off for a record that\n"
            "has rung down -- it is the only choice that preserves absolute\n"
            "amplitudes. For a truncated one, Tukey: an FDTD record is\n"
            "front-loaded, so a symmetric Hann lands its rising edge on the\n"
            "excitation and reshapes it, while a Tukey is flat across the\n"
            "record and tapers only the ringing tail."
        )
        row.addWidget(win_box)
        row.addStretch(1)
        layout.addLayout(row)

        def redraw():
            _redraw_guarded(dialog, lambda fig: (
                draw_time(fig) if opts["view"] == "time"
                else draw_freq(fig, opts)))

        def on_view(index):
            opts["view"] = views[index][1]
            win_box.setEnabled(opts["view"] != "time")
            redraw()

        def on_window(index):
            opts["window"] = _WINDOW_CHOICES[index][1]
            redraw()

        view_box.currentIndexChanged.connect(on_view)
        win_box.currentIndexChanged.connect(on_window)
        draw_time(figure)
        return opts

    def _plot_energy(obj):
        # Titled from the leaf label so the two regions' plots are told apart.
        _plot_series(obj, "total energy (J)", str(obj.Label), "#d65a00")

    def _plot_dissipation(obj):
        """Ohmic power vs. time, annotated with the energy it integrates to.

        The integral is the number worth reading next to the energy series --
        for a closed run, ``U(0) - U(t)`` should equal it -- and it is the same
        trapezoid the solver's ``DissipationMonitor.energy()`` takes, so the
        plot and ``summary.json`` cannot disagree.
        """
        def _total(ax, times, values):
            import numpy as np

            if len(times) < 2:
                return
            # FreeCAD 1.1 bundles numpy 1.26, which has no ``trapezoid`` (the
            # numpy-2 rename the solver side uses); ``trapz`` is the same rule.
            trapz = getattr(np, "trapezoid", None) or np.trapz
            joules = float(trapz(values, times))
            ax.text(0.98, 0.95, "∫P dt = {:.4g} J".format(joules),
                    transform=ax.transAxes, ha="right", va="top", fontsize=9,
                    bbox=dict(boxstyle="round", fc="white", ec="0.7",
                              alpha=0.85))

        _plot_series(obj, "dissipated power (W)", str(obj.Label), "#b03000",
                     annotate=_total)

    def _plot_probe(obj):
        comp = str(getattr(obj, "Component", "")) or "field"
        _plot_series(obj, comp, "Probe: {} vs. time".format(comp), "#1f77b4")

    def _plot_voltage(obj):
        _plot_series(
            obj, "voltage (V)", "Voltage: ∫E·dl vs. time", "#2ca02c",
            quantity="V",
        )

    def _plot_current(obj):
        _plot_series(
            obj, "current (A)", "Current: ∮H·dl vs. time", "#9467bd",
            quantity="I",
        )

    def _plot_spice_voltage(obj):
        _plot_series(
            obj, "voltage (V)", "SPICE port voltage vs. time", "#2ca02c",
            quantity="V",
        )

    def _plot_spice_current(obj):
        _plot_series(
            obj, "current (A)", "SPICE port current vs. time", "#9467bd",
            quantity="I",
        )

    def _lumped_lines(obj):
        """A lumped port's network and its cell's gap C, as annotation lines.

        Worth the corner of the figure for the same reason the panel says it
        before the run: the element delivers exactly the R/L/C it was given, but
        the cells it bridges keep ``C_cell`` across it, so a V/I pair read against
        the nominal value quietly disagrees by that shunt — and ``C_cell`` came
        from the mesh, not from the network the label names. On the Z(f) plot it
        is the same caveat, made concrete: the curve is the element *and* the
        shunt in parallel.
        """
        network = str(getattr(obj, "Network", "") or "")
        c_cell = getattr(obj, "CellCapacitance", None)
        lines = [network] if network else []
        if c_cell is not None and c_cell == c_cell and c_cell > 0.0:  # not NaN
            lines.append("cell gap C = {:.4g} fF in parallel".format(
                1.0e15 * float(c_cell)))
        return lines

    def _zref_lines(obj):
        """The impedance a modal port's V/I pair is referenced to, as lines.

        Worth the corner of the figure: the pair is only half the story on its
        own -- Z_ref is what turns it into incident/outgoing wave amplitudes
        ``(V ± Z_ref·I)/2``, and it is a property of this port's own discrete
        mode, not a number to look up elsewhere. Empty when the leaf carries
        none (a run predating it).
        """
        z_ref = _zref_of(obj)
        return [] if z_ref is None else ["Z$_{{ref}}$ = {:.4g} Ω".format(z_ref)]

    def _zref_of(obj):
        """The leaf's ``ReferenceImpedance`` as a positive float, or ``None``."""
        z_ref = getattr(obj, "ReferenceImpedance", None)
        if z_ref is None or z_ref != z_ref or z_ref <= 0.0:   # None / NaN
            return None
        return float(z_ref)

    def _corner_note(lines):
        """An *annotate* callback drawing *lines* in the figure's corner, or
        ``None`` when there is nothing to say."""
        if not lines:
            return None

        def _note(ax, _times, _values):
            _corner_text(ax, lines)

        return _note

    def _plot_lumped_voltage(obj):
        _plot_series(obj, "voltage (V)", "Lumped port voltage vs. time",
                     "#2ca02c", annotate=_corner_note(_lumped_lines(obj)),
                     quantity="V")

    def _plot_lumped_current(obj):
        _plot_series(obj, "current (A)", "Lumped port current vs. time",
                     "#9467bd", annotate=_corner_note(_lumped_lines(obj)),
                     quantity="I")

    def _plot_port_voltage(obj):
        # The modal projection of the plane E, not a line integral -- said in
        # the title so it is not read as an ∫E·dl monitor that happens to sit
        # on the port plane.
        _plot_series(
            obj, "voltage (V)", "Modal port voltage vs. time", "#2ca02c",
            annotate=_corner_note(_zref_lines(obj)), quantity="V",
        )

    def _plot_port_current(obj):
        # Positive *into* the domain (the solver signs it that way), so V·I is
        # the power the port delivers inward.
        _plot_series(
            obj, "current (A)", "Modal port current vs. time (into the domain)",
            "#9467bd", annotate=_corner_note(_zref_lines(obj)), quantity="I",
        )

    # ------------------------------------------------------------------ #
    # Port impedance Z(f)
    # ------------------------------------------------------------------ #

    def _plot_impedance(obj):
        """Open a port's ``Z(f) = V(f)/I(f)`` window.

        Two views of the same complex curve, because they answer different
        questions. **Bode** -- |Z| over the phase -- is how a resonance and its
        Q are read, and where ±90° says "purely reactive".
        **R and X** puts the real and imaginary parts on one linear axis, which
        is where the lumped content is legible: a series inductance is a
        straight line X = 2πfL through the origin, and a zero crossing of X is
        a resonance. A log-magnitude view compresses exactly those features.

        Gaps in the curves are not missing data: they are the bins masked as
        out-of-band, where the excitation put no energy and the ratio would be
        two round-off numbers divided. If the plot is mostly gap, switch either
        V or I leaf to its spectrum view and look at what the drive covered.
        """
        workdir = str(obj.ResultsDir)
        v_key = str(obj.DataKey)
        i_key = str(getattr(obj, "CurrentKey", "") or "")
        v_times = _load_array(workdir, v_key + "_times")
        v_values = _load_array(workdir, v_key + "_values")
        i_values = _load_array(workdir, i_key + "_values")
        if v_times is None or v_values is None or i_values is None:
            _missing(obj)
            return

        made = _make_window("Wavesim Results - {}".format(obj.Label))
        if made is None:
            return
        dialog, figure, layout = made

        def draw(fig, opts):
            _draw_impedance(fig, obj, v_times, v_values, i_values, opts)

        _add_impedance_switch(dialog, layout, draw)
        dialog._canvas.draw()
        dialog.show()
        _register_window(dialog)

    def _impedance_of(obj, times, v_values, i_values, opts):
        """``Z(f)`` from the leaf's pair, both transformed the same way.

        Same window on both -- which is what makes a taper safe for a ratio --
        and each with its own stagger: V is E-derived and I is the impressed
        current half a step behind it. Dividing them as recorded would put an
        ``exp(+jπf·dt)`` on Z, which shows up as a *resistance* a lossless
        structure does not have (see :mod:`wavesim_gui.spectrum`).
        """
        from wavesim_gui import spectrum as spec

        v = _spectrum_of(times, v_values, "V", opts)
        i = _spectrum_of(times, i_values, "I", opts)
        return v, i, spec.impedance(v, i, floor=_BAND_FLOOR)

    def _draw_impedance(figure, obj, times, v_values, i_values, opts):
        """Draw the Bode or R/X view of a port's Z(f) into *figure*."""
        v, i, z = _impedance_of(obj, times, v_values, i_values, opts)
        funit = _freq_unit(obj)
        scale = units.freq_to_si(1.0, funit)
        f = z.freqs / scale
        z_ref = _zref_of(obj)
        # The lumped caveat and the truncation warning belong on every view; the
        # Z_ref line only where the Bode panel does not already draw it.
        notes = _lumped_lines(obj)
        warn = _decay_warning([v_values, i_values], opts)

        if opts.get("view") == "parts":
            ax = figure.add_subplot(111)
            ax.plot(f, z.real, lw=1.3, color="#1f77b4", label="R = Re Z")
            ax.plot(f, z.imag, lw=1.3, ls="--", color="#d62728",
                    label="X = Im Z")
            ax.axhline(0.0, color="0.6", lw=0.8)   # X = 0 marks a resonance
            ax.set_ylabel("R, X (Ω)")
            ax.set_xlabel("frequency ({})".format(funit))
            ax.set_title("{}: R and X".format(str(obj.Label)))
            ax.grid(True, alpha=0.3)
            ax.legend(fontsize=9, loc="upper left")
            _set_band_xlim(ax, [v, i], scale)
            _corner_text(ax, _zref_lines(obj) + notes
                         + ([warn] if warn else []))
            return

        ax_mag = figure.add_subplot(211)
        ax_ph = figure.add_subplot(212, sharex=ax_mag)
        ax_mag.plot(f, z.magnitude, lw=1.3, color="#1f77b4", label="|Z|")
        ax_mag.set_ylabel("|Z| (Ω)")
        if z_ref is not None:
            # The port's own reference impedance drawn where |Z| can be read
            # against it: a matched termination sits on this line.
            ax_mag.axhline(z_ref, color="0.6", lw=0.8, ls=":",
                           label="Z$_{{ref}}$ = {:.4g} Ω".format(z_ref))
        ax_mag.grid(True, which="both", alpha=0.3)
        ax_mag.legend(fontsize=9, loc="upper left")
        ax_mag.set_title("{}: magnitude and phase".format(str(obj.Label)))

        ax_ph.plot(f, z.phase_deg(), lw=1.3, color="#d62728")
        ax_ph.set_ylabel("∠Z (deg)")
        ax_ph.set_xlabel("frequency ({})".format(funit))
        ax_ph.grid(True, which="both", alpha=0.3)
        # ±90° is a purely reactive port and 0° a purely resistive one: the
        # lines a lumped extraction is read against.
        for y in (-90.0, 0.0, 90.0):
            ax_ph.axhline(y, color="0.6", lw=0.7, ls=":")

        _set_band_xlim(ax_mag, [v, i], scale)
        # Log only if the visible |Z| spans more than a decade -- a matched port
        # is a flat line, and a log axis labels that "5.06 × 10¹" rather than
        # "50.6". Out-of-band bins are already NaN, so "visible" is the whole
        # curve here and the autoscaled linear axis includes the Z_ref line.
        if _fit_ylim(ax_mag, z.freqs, z.magnitude, scale):
            ax_mag.set_yscale("log")
        # Notes go on the phase panel: the magnitude panel's upper-right is
        # where a resonance peak lands.
        _corner_text(ax_ph, notes + ([warn] if warn else []))

    def _add_impedance_switch(dialog, layout, draw):
        """The Z(f) window's "View / Window" row -- same idea as the domain
        switch, but both entries are already frequency, so the Window box is
        always live."""
        _QtCore, QtWidgets = _qt()
        figure = dialog._figure

        opts = {"view": "bode", "window": None}
        views = (("Bode (|Z| and phase)", "bode"), ("R and X", "parts"))

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("View:"))
        view_box = QtWidgets.QComboBox()
        for text, _key in views:
            view_box.addItem(text)
        view_box.setToolTip(
            "Bode reads a resonance and its Q; R and X is where an L or a C\n"
            "is read off -- a series inductance is the straight line\n"
            "X = 2πfL, and a zero crossing of X is a resonance."
        )
        row.addWidget(view_box)

        row.addSpacing(12)
        row.addWidget(QtWidgets.QLabel("Window:"))
        win_box = QtWidgets.QComboBox()
        for text, _key in _WINDOW_CHOICES:
            win_box.addItem(text)
        win_box.setToolTip(
            "Taper applied to both series before transforming. A ratio is safe\n"
            "under a window as long as V and I get the same one, which they do\n"
            "here. Tukey for a record that ends mid-ring -- see the note on\n"
            "the plot when it does."
        )
        row.addWidget(win_box)
        row.addStretch(1)
        layout.addLayout(row)

        def redraw():
            _redraw_guarded(dialog, lambda fig: draw(fig, opts))

        def on_view(index):
            opts["view"] = views[index][1]
            redraw()

        def on_window(index):
            opts["window"] = _WINDOW_CHOICES[index][1]
            redraw()

        view_box.currentIndexChanged.connect(on_view)
        win_box.currentIndexChanged.connect(on_window)
        draw(figure, opts)
        return opts

    # Colour-scale clip percentiles offered for snapshot maps, strongest first.
    # 100 means "scale on the true peak" (the old behaviour).
    _CLIP_CHOICES = (99.5, 99.9, 99.0, 98.0, 95.0, 100.0)

    def _robust_vmax(stack, pct):
        """Colour-scale limit for *stack*: the *pct* percentile of ``|stack|``.

        Scaling a linear map on the true peak is usually wrong here. A source
        cell, a PEC corner or a PML sliver sits orders of magnitude above the
        propagating field, so the peak sets a limit the rest of the domain never
        approaches and everything else renders as the colour map's midpoint —
        near-white in RdBu_r. Taking a high percentile instead puts the full
        colour range over the field that actually fills the picture and lets the
        few hot cells saturate (the colour bar's arrows mark that).

        Exact zeros are excluded: pre-arrival frames and conductor interiors are
        zero over much of the stack, and counting them would drag the percentile
        down by an amount that depends on domain size rather than on the field.
        """
        import numpy as np

        arr = np.asarray(stack)
        if arr.size == 0:
            return 0.0
        # Percentiles sort a copy, so sample rather than sort a whole run's
        # frames; a strided view over the flat stack spans every frame evenly.
        flat = arr.reshape(-1)
        step = max(1, flat.size // 2000000)
        v = np.abs(np.asarray(flat[::step], dtype=float))
        v = v[np.isfinite(v) & (v > 0.0)]
        if v.size == 0:
            return 0.0
        if pct >= 100.0:
            return float(v.max())
        return float(np.percentile(v, pct))

    # --- smoothing --------------------------------------------------------- #
    # Shading between cell centres rather than painting each cell flat. Shared
    # by the snapshot animation (which rebuilds the same resampler once and
    # replays it per frame) and the mode plot (which needs it once).

    # ~4x upsampling, capped so a fine grid does not blow up the redraw.
    _SMOOTH_TARGET, _SMOOTH_MAX = 4, 900

    def _cubic_weights(src, n_out):
        """Catmull-Rom taps resampling *src* samples onto `n_out` uniform ones.

        Returns ``(idx, w)`` with ``idx`` four index arrays into the sample
        axis and ``w`` their four weights, so the interpolation is four
        gathers and a dot product per frame.
        """
        import numpy as np

        n = len(src)
        pos = np.interp(
            np.linspace(src[0], src[-1], n_out), src, np.arange(n, dtype=float)
        )
        i1 = np.clip(np.floor(pos).astype(int), 0, n - 1)
        t = (pos - i1)[:, None] ** np.arange(4)   # [1, t, t^2, t^3]
        idx = [np.clip(i1 + k, 0, n - 1) for k in (-1, 0, 1, 2)]
        # Catmull-Rom basis, as polynomials in t.
        basis = np.array([
            [0.0, -0.5, 1.0, -0.5],
            [1.0, 0.0, -2.5, 1.5],
            [0.0, 0.5, 2.0, -1.5],
            [0.0, 0.0, -0.5, 0.5],
        ])
        return idx, t @ basis.T                   # (n_out, 4)

    def _smooth_grid(data2d, cx, cy):
        """Resample ``(ny, nx)`` *data2d* onto a fine uniform grid.

        Returns ``(fine, extent)`` for ``imshow(origin="lower")``. The one-shot
        form of the animation's ``_make_warp``, for a picture drawn once rather
        than replayed: same taps, no per-frame closure to keep. Adds no data --
        the samples are cell centres either way, and this interpolates between
        the same numbers.
        """
        import numpy as np

        arr = np.asarray(data2d, dtype=float)
        ny, nx = arr.shape
        nfx = int(min(max(nx, nx * _SMOOTH_TARGET), _SMOOTH_MAX))
        nfy = int(min(max(ny, ny * _SMOOTH_TARGET), _SMOOTH_MAX))
        ix, wx = _cubic_weights(cx, nfx)
        iy, wy = _cubic_weights(cy, nfy)
        a = sum(arr[:, ix[k]] * wx[:, k] for k in range(4))
        fine = sum(a[iy[k], :] * wy[:, k][:, None] for k in range(4))
        return fine, [float(cx[0]), float(cx[-1]), float(cy[0]), float(cy[-1])]

    def _edges_from_centres(c):
        """Cell edges around the sample centres *c* (halfway between, ends kept)."""
        import numpy as np

        c = np.asarray(c, dtype=float)
        if c.size < 2:
            return np.array([c[0] - 0.5, c[0] + 0.5]) if c.size else np.zeros(2)
        mid = 0.5 * (c[1:] + c[:-1])
        return np.concatenate((
            [c[0] - (mid[0] - c[0])], mid, [c[-1] + (c[-1] - mid[-1])],
        ))

    # --- arrow overlays ---------------------------------------------------- #
    # Two pictures draw a vector field over a colour map: the snapshot
    # animation and the solved TEM mode. They place their arrows the same way,
    # from here, so a mode and a field frame read alike -- and so neither one
    # can drift into the mess that per-cell striding produces (arrows longer
    # than their own spacing, overlapping into a smear that shows neither
    # direction nor magnitude).
    _ARROWS_ACROSS = 56     # arrows along the longer visible axis
    # Longest arrow, in arrow pitches. Below 1 by design: with ``pivot="mid"``
    # an arrow reaches half its length each way, so at most half a pitch, and
    # two neighbours at full length pointing straight at each other still leave
    # a gap. That makes "no overlap" a property of the lattice rather than
    # something that happens to hold on a particular field.
    _ARROW_SPAN = 0.9

    # Slim shaft, small head: this is a direction field laid over a colour map,
    # not a chart of a handful of vectors, so the arrows must stay readable at
    # that density without blotting out what they sit on. ``width`` is in axes
    # widths like the length, so it tracks ``_ARROWS_ACROSS`` inversely -- a
    # denser lattice needs a proportionally thinner arrow to keep the same
    # shape. The head is kept short for a second reason as well -- see
    # ``_ARROW_FLOOR``.
    _ARROW_STYLE = dict(width=0.0019, headwidth=3.0, headlength=3.0,
                        headaxislength=2.8, pivot="mid")

    # Shortest arrow drawn, as a fraction of full length. Below ``headlength *
    # width`` (both in axes-width units) matplotlib stops shortening the shaft
    # and scales the whole glyph down instead: all head and no shaft, which
    # reads as a dot. A dot carries no direction, and the colour map underneath
    # already says the field is weak there, so those are hidden rather than
    # drawn. Derived from the style above rather than picked, so it stays
    # exactly at the dot boundary if the style is ever retuned.
    _ARROW_FLOOR = (_ARROW_STYLE["headlength"] * _ARROW_STYLE["width"]
                    * _ARROWS_ACROSS / _ARROW_SPAN)

    def _nearest_index(coords, vals):
        """Index of the sample in *coords* nearest each of *vals*."""
        import numpy as np

        n = len(coords)
        if n < 2:
            return np.zeros(np.shape(vals), dtype=int)
        j = np.clip(np.searchsorted(coords, vals), 1, n - 1)
        return np.where(vals - coords[j - 1] <= coords[j] - vals, j - 1, j)

    def _arrow_lattice(cx, cy, xlim, ylim):
        """Square-lattice arrow sites over the window *xlim* x *ylim*.

        Returns ``(px, py, ix, iy)``: flat site coordinates plus the
        nearest-cell indices to read the field at, or ``None`` if the window
        and the sample coordinates *cx*, *cy* do not overlap.

        The sites are a lattice of fixed *physical* pitch, not every k-th cell.
        Index striding puts the same arrow count on each axis, so a long thin
        slice (a coax down z) comes out dense across and sparse along it -- and
        on a graded grid the cells being strided are not even the same size, so
        the arrows bunch up wherever the mesh is fine. The pitch is one number,
        set by the longer axis and used for *both*, so the lattice stays square
        whatever the aspect ratio: the short axis gets however many rows fit at
        that pitch, centred, rather than its own extent divided into the same
        count.
        """
        import numpy as np

        xlo, xhi = sorted(xlim)
        ylo, yhi = sorted(ylim)
        x0, x1 = max(xlo, cx[0]), min(xhi, cx[-1])
        y0, y1 = max(ylo, cy[0]), min(yhi, cy[-1])
        if x1 <= x0 or y1 <= y0:
            return None
        # One pitch for both axes -- the lattice is square by construction.
        pitch = max(x1 - x0, y1 - y0) / float(_ARROWS_ACROSS)
        nx = max(1, int(round((x1 - x0) / pitch)))
        ny = max(1, int(round((y1 - y0) / pitch)))
        # Centred, so the leftover strip on the short axis is split evenly
        # rather than piling up at one edge.
        gx = x0 + 0.5 * ((x1 - x0) - (nx - 1) * pitch) + np.arange(nx) * pitch
        gy = y0 + 0.5 * ((y1 - y0) - (ny - 1) * pitch) + np.arange(ny) * pitch
        px, py = np.meshgrid(gx, gy)
        px = np.clip(px, cx[0], cx[-1]).ravel()
        py = np.clip(py, cy[0], cy[-1]).ravel()
        return px, py, _nearest_index(cx, px), _nearest_index(cy, py)

    def _arrow_scale(ref):
        """``quiver`` scale that draws magnitude *ref* at full arrow length.

        Goes with ``scale_units="width"``: arrow length is then a fraction of
        the axes width rather than a data distance, so a *ref* vector spans
        ``_ARROW_SPAN`` lattice pitches whatever the view is zoomed to, instead
        of the lattice thinning out while the arrows keep their data length.
        """
        return (float(ref) or 1.0) * _ARROWS_ACROSS / _ARROW_SPAN

    def _arrow_reference(mags):
        """Magnitude drawn at full arrow length, from a sample of ``|field|``.

        A *percentile*, not the peak: a source cell or a conductor edge is
        often orders of magnitude above the field filling the picture, and
        scaling on it collapses every other arrow to nothing (unlike the colour
        map, a too-short arrow is invisible rather than merely faint). Vectors
        above it are clipped to full length, keeping their exact direction.
        Exact zeros -- conductor interiors, pre-arrival frames -- are left out,
        so the reference does not depend on how much empty domain is in view.
        """
        import numpy as np

        arr = np.asarray(mags, dtype=float)
        nonzero = arr[np.isfinite(arr) & (arr > 0.0)]
        if nonzero.size == 0:
            return 1.0
        return float(np.percentile(nonzero, 99.0)) or float(arr.max()) or 1.0

    def _add_quiver(ax, px, py, u, v, ref, color):
        """Add an arrow overlay to *ax* without letting it touch the view.

        Not ``ax.quiver``: that adds the arrows with ``autolim=True`` and then
        requests an autoscale. The overlay is a *reader* of the view -- both
        its sites and its scale are chosen from the current limits, and it is
        rebuilt whenever they change -- so letting it write to the limits as
        well closes a loop. Adding arrows re-stales the view, settling the view
        emits ``xlim_changed``, and that rebuilds the arrows; under Qt, where
        the paint that settles the limits comes after the figure is assembled,
        it recurses until the stack runs out ("maximum recursion depth exceeded"
        on opening the plot). Going through ``add_collection(..., autolim=
        False)`` breaks the loop at the source rather than papering over it
        with re-entrancy flags -- the same rule the geometry outlines follow.

        Length is in axes width rather than data units (``scale_units``), which
        is what keeps an arrow the same size on screen, and a ``ref`` one
        spanning ``_ARROW_SPAN`` pitches, however the view is zoomed.
        """
        from matplotlib.quiver import Quiver

        art = Quiver(ax, px, py, u, v, angles="xy", scale_units="width",
                     scale=_arrow_scale(ref), color=color, zorder=3,
                     **_ARROW_STYLE)
        ax.add_collection(art, autolim=False)
        return art

    def _arrow_uv(u, v, ref, length=None):
        """``quiver`` components for the vectors (*u*, *v*), drawn at *length*.

        Direction is kept exact; magnitude is carried by the drawn length,
        which defaults to the vector's own clipped at *ref* (so the strongest
        cells saturate at full length instead of setting the scale for
        everything else). Vectors that would draw shorter than ``_ARROW_FLOOR``
        come back as NaN, which ``quiver`` renders as nothing at all -- the
        weak field then fades out of the picture rather than stippling it with
        dots. Zero-length vectors -- conductor interiors, an unreached domain
        -- go the same way.
        """
        import numpy as np

        u = np.asarray(u, dtype=float)
        v = np.asarray(v, dtype=float)
        m = np.sqrt(u ** 2 + v ** 2)
        if length is None:
            length = np.minimum(m, ref)
        with np.errstate(divide="ignore", invalid="ignore"):
            f = np.where(m > 0, length / m, 0.0)
            f = np.where(length >= _ARROW_FLOOR * ref, f, np.nan)
        return u * f, v * f

    def _plot_snapshot(obj):
        import numpy as np

        workdir = str(obj.ResultsDir)
        key = str(obj.DataKey)
        times = _load_array(workdir, key + "_times")

        # What this leaf can show: every recorded component plus the magnitude
        # derived from them. Pre-merge leaves recorded one quantity and offer
        # only that.
        comps = [c for c in str(getattr(obj, "Components", "")).split(",") if c]
        field = str(getattr(obj, "Field", "")) or "E"
        if len(comps) > 1:
            magnitude = "|{}|".format(field)
            choices = comps + [magnitude]
        elif comps:
            # A scalar quantity (the electrostatic potential): there is nothing
            # to take a magnitude of, and offering |phi| would suggest otherwise.
            magnitude = None
            choices = list(comps)
        else:
            magnitude = None
            choices = [str(getattr(obj, "Component", "")) or "field"]

        # Stacks are big, so load each component only when it is first shown
        # (the magnitude pulls in all three).
        _cache = {}

        def _frames(choice):
            """The frame stack for *choice*, or ``None`` if its array is gone."""
            if choice not in _cache:
                if choice == magnitude:
                    total = None
                    for c in comps:
                        arr = _frames(c)
                        if arr is None:
                            _cache[choice] = None
                            return None
                        sq = np.asarray(arr, dtype=float) ** 2
                        total = sq if total is None else total + sq
                    _cache[choice] = None if total is None else np.sqrt(total)
                else:
                    suffix = "_{}_data".format(choice) if comps else "_data"
                    _cache[choice] = _load_array(workdir, key + suffix)
            return _cache[choice]

        # Open on the magnitude: it is the one view that means the same thing for
        # either field, and the components are a dropdown away.
        comp = magnitude or choices[0]
        frames = _frames(comp)
        if frames is None or len(frames) == 0:
            _missing(obj)
            return

        unit = _time_unit(obj)

        # A one-sided (0..max) colour scale, as against the zero-centred one a
        # signed field wants. Magnitudes always; and a scalar quantity that never
        # goes negative -- an electrostatic potential between a grounded and a
        # driven conductor -- for the same reason, since a diverging map would
        # spend half its range on values the picture does not contain.
        _positive_scalar = (
            magnitude is None and len(comps) == 1
            and float(np.nanmin(np.asarray(frames, dtype=float))) >= 0.0
        )

        def _one_sided(choice):
            return (choice.startswith("|") or choice.startswith("∣")
                    or _positive_scalar)

        # In-plane node/edge coordinates (mm) from the runner: when present the
        # frame is drawn with pcolormesh on the real (possibly non-uniform) grid.
        xedges = list(getattr(obj, "XEdges", []) or [])
        yedges = list(getattr(obj, "YEdges", []) or [])
        use_mesh = len(xedges) >= 2 and len(yedges) >= 2

        # Physical extent / axis labels (fall back to cell indices).
        size = getattr(obj, "InPlaneSize", None)
        have_size = size is not None and size.x > 0 and size.y > 0
        if use_mesh or have_size:
            xlabel = "{} (mm)".format(getattr(obj, "AxisX", "x"))
            ylabel = "{} (mm)".format(getattr(obj, "AxisY", "y"))
        else:
            xlabel, ylabel = "cell i", "cell j"
        extent = [0.0, float(size.x), 0.0, float(size.y)] if have_size else None

        # Symmetric colour scale for signed fields (RdBu_r); 0..max for
        # magnitudes (inferno). Log scaling mirrors wavesim's animate_snapshots:
        # SymLogNorm for signed fields (linear within +/-vmax/1e3, log beyond),
        # LogNorm for magnitudes.
        # Each component is scaled on its own peak, so switching to a weak
        # component rescales rather than showing it washed out.
        from matplotlib import colors as mcolors

        view = {"comp": comp, "frames": frames, "clip": _CLIP_CHOICES[0]}

        def _cmap_for(choice):
            return "turbo" if _one_sided(choice) else "RdBu_r"

        # (component, percentile) -> vmax. Each limit spans the whole run so the
        # scale holds still while the animation plays, which makes recomputing
        # it on every norm change pure waste.
        _vmax_cache = {}

        def _vmax_for(pct):
            hit = _vmax_cache.get((view["comp"], pct))
            if hit is None:
                hit = _robust_vmax(view["frames"], pct) or 1.0
                _vmax_cache[(view["comp"], pct)] = hit
            return hit

        def _make_norm(log):
            # Log scaling shows the whole dynamic range by construction, so it
            # keeps the true peak; the clip percentile is what rescues the
            # linear map from a handful of outlying cells.
            vmax = _vmax_for(100.0 if log else view["clip"])
            linthresh = vmax / 1e3
            if _one_sided(view["comp"]):
                if log:
                    return mcolors.LogNorm(vmin=linthresh, vmax=vmax)
                return mcolors.Normalize(vmin=0.0, vmax=vmax)
            if log:
                return mcolors.SymLogNorm(
                    linthresh=linthresh, vmin=-vmax, vmax=vmax,
                )
            return mcolors.Normalize(vmin=-vmax, vmax=vmax)

        made = _make_window("Wavesim Results - {}".format(obj.Label))
        if made is None:
            return
        dialog, figure, layout = made
        _QtCore, QtWidgets = _qt()

        ax = figure.add_subplot(111)

        def _centres(edges, n, length):
            """Cell-centre coordinates along an axis, in the drawn axis units."""
            e = np.asarray(edges, dtype=float)
            if len(e) >= n + 1:
                return 0.5 * (e[:n] + e[1:n + 1])
            if length:
                return (np.arange(n) + 0.5) * (float(length) / n)
            return np.arange(n, dtype=float)   # cell indices

        # frames[f] has shape (axis1, axis2); show axis1 horizontal, axis2
        # vertical with a lower-left origin. Equal aspect so a square physical
        # extent renders square rather than stretched to fill the axes.
        #
        # Smoothing interpolates between neighbouring *cell centres* instead of
        # painting one flat rectangle per cell, which is what makes a coarse
        # grid read as pixels. It is honest about the data rather than a
        # cosmetic blur: the frames are point samples at cell centres (see
        # ARCHITECTURE's collocation note), so interpolating between them is a
        # plain reading of the same numbers -- but it does hide the cells,
        # which is why it is a toggle. Off shows exactly the grid the solver
        # stepped (staircasing at a conductor, a mesh that is too coarse).
        #
        # Gouraud shading was the obvious route and is not enough: it is
        # piecewise *linear*, so a coarse grid trades pixels for visible facets
        # and diamond seams. Instead the frame is resampled onto a fine uniform
        # lattice with a separable Catmull-Rom cubic (C1, passes through every
        # sample) and drawn with `imshow`, which is both smoother and faster to
        # redraw than a large QuadMesh. The interpolation runs in *index* space
        # with the coordinate -> index map carrying the geometry, so it is
        # correct on a graded grid. A cubic can overshoot at a sharp edge (a
        # conductor); the colour norm clips it, and the flat view is a tick
        # away. The artist kind differs per mode, so the toggle rebuilds it.
        # ``fill`` follows the Mask PEC checkbox: with the conductor blanked the
        # smoother must not read the zeros inside it either, and with the mask
        # off the picture has to be the run's own numbers everywhere -- filling
        # under a lifted mask would show invented values as data.
        image = {"art": None, "smooth": True, "warp": None, "fill": True}

        def _section_groups():
            """Section the CAD on this leaf's plane -- once, for every overlay."""
            # Chord tolerance for discretising curved edges: a fraction of the
            # drawn extent, so a bore reads as a circle rather than a polygon
            # and no CAD detail costs much more than a pixel.
            spanx = ((float(xedges[-1]) - float(xedges[0])) if use_mesh
                     else (float(size.x) if have_size else 0.0))
            spany = ((float(yedges[-1]) - float(yedges[0])) if use_mesh
                     else (float(size.y) if have_size else 0.0))
            chord = max(spanx, spany) / 400.0
            if chord <= 0.0:
                return []
            try:
                return _geometry_outlines(obj, chord)
            except Exception as exc:      # never let the CAD break the plot
                FreeCAD.Console.PrintWarning(
                    "Wavesim: could not section the geometry for {} ({})\n"
                    .format(obj.Label, exc)
                )
                return []

        # Sectioned here rather than beside the overlays it also feeds, because
        # the very first `_make_image` already needs the conductor rings.
        groups = _section_groups()
        pec_rings = _pec_rings(groups)

        def _make_warp(nx, ny):
            """Build the resampler for an (ny, nx) frame, plus its imshow extent."""
            cx = _centres(xedges, nx, size.x if have_size else 0.0)
            cy = _centres(yedges, ny, size.y if have_size else 0.0)
            nfx = int(min(max(nx, nx * _SMOOTH_TARGET), _SMOOTH_MAX))
            nfy = int(min(max(ny, ny * _SMOOTH_TARGET), _SMOOTH_MAX))
            ix, wx = _cubic_weights(cx, nfx)
            iy, wy = _cubic_weights(cy, nfy)
            # Which of these samples are inside metal. Fixed for the run, so it
            # is found once here and only the arithmetic repeats per frame --
            # a handful of shifted adds on the coarse frame, against the eight
            # gathers over the fine one that follow.
            dead = _dead_cells(pec_rings, cx, cy) if pec_rings else None

            def warp(data2d):
                if dead is not None and image["fill"]:
                    data2d = _fill_dead(data2d, dead)
                # Columns (x) first, then rows (y); each pass is 4 gathers.
                a = sum(data2d[:, ix[k]] * wx[:, k] for k in range(4))
                return sum(a[iy[k], :] * wy[:, k][:, None] for k in range(4))

            extent = [float(cx[0]), float(cx[-1]), float(cy[0]), float(cy[-1])]
            return warp, extent

        def _make_image(smooth, idx=0, log=False):
            """(Re)create the field map artist, smoothed or flat."""
            if image["art"] is not None:
                image["art"].remove()
            data2d = np.asarray(view["frames"][idx]).T
            cmap, norm = _cmap_for(view["comp"]), _make_norm(log)
            image["warp"] = None
            if use_mesh:
                if smooth:
                    warp, box = _make_warp(data2d.shape[1], data2d.shape[0])
                    image["warp"] = warp
                    art = ax.imshow(
                        warp(np.asarray(data2d, dtype=float)), origin="lower",
                        extent=box, cmap=cmap, norm=norm, aspect="equal",
                        interpolation="bilinear",
                    )
                else:
                    # pcolormesh honours non-uniform edge spacing; C is
                    # (Ny, Nx) so pass the transposed frame against
                    # (xedges, yedges).
                    art = ax.pcolormesh(
                        np.asarray(xedges), np.asarray(yedges),
                        data2d, cmap=cmap, norm=norm,
                    )
                # Frame the full cell-edge extent either way, so the axes box is
                # the physical slice and toggling smooth does not shift the
                # picture (the smoothed image stops at the outer cell centres,
                # and a removed artist's data limits would otherwise linger).
                ax.set_xlim(float(xedges[0]), float(xedges[-1]))
                ax.set_ylim(float(yedges[0]), float(yedges[-1]))
                ax.set_aspect("equal")
            else:
                art = ax.imshow(
                    data2d, origin="lower", extent=extent, cmap=cmap,
                    norm=norm, aspect="equal",
                    interpolation="bilinear" if smooth else "nearest",
                )
            image["art"], image["smooth"] = art, bool(smooth)
            return art

        _make_image(image["smooth"])

        # --- geometry outline overlay ------------------------------------- #
        # The field alone does not say where the metal and the dielectric are,
        # and on a slice through a coax that is most of the reading. Each
        # material's cross-section is drawn in the material's own colour --
        # the same one the body is tinted with in the 3D view, so the two
        # pictures name their parts alike -- semi-transparent and under the
        # arrows, since it is a reference layer and not the result.
        def _build_outlines():
            """Draw each material's cross-section; returns the added artists."""
            from matplotlib.collections import LineCollection

            arts = []
            for rgb, polys, _is_pec in groups:
                lc = LineCollection(
                    polys, colors=[rgb], linewidths=1.4, alpha=0.65, zorder=2,
                )
                # No autolim: the axes are framed on the field, and a body
                # running past the domain must not stretch them.
                ax.add_collection(lc, autolim=False)
                arts.append(lc)
            return arts

        outlines = _build_outlines()

        # --- conductor mask ------------------------------------------------ #
        # Nothing propagates in metal, so the field map has nothing to say
        # there -- but it paints the interior all the same, and the visible
        # edge of the field is then wherever the interpolated cell-centre
        # zeros happen to fall: a staircase at grid resolution, disagreeing
        # with the conformal outline drawn straight over it. Blanking the
        # conductor in the axes' own background colour cuts that edge at the
        # CAD surface instead, which is exact rather than sub-cell-accurate
        # and needs no fraction arrays -- so it reads the same on a staircase
        # run as on a conformal one. The patch only hides those zeros; keeping
        # them out of the smoother, which reaches two cells past the surface,
        # is `_fill_dead`'s job and rides the same checkbox.
        #
        # Added with ``add_artist``: ``add_patch`` would fold the patch into
        # the data limits, and a body running past the domain must not stretch
        # the axes (the same rule the outlines follow). zorder sits above the
        # field map in either of its two artist kinds (imshow 0, QuadMesh 1)
        # and below the outlines, so a masked conductor still keeps its own
        # coloured boundary.
        def _build_mask():
            """Blank every PEC cross-section, or ``None`` if there is none."""
            from matplotlib.patches import PathPatch

            path = _filled_path(pec_rings)
            if path is None:
                return None
            patch = PathPatch(path, facecolor=ax.get_facecolor(),
                              edgecolor="none", zorder=1.8)
            ax.add_artist(patch)
            return patch

        pec_mask = _build_mask()

        # --- in-plane vector overlay -------------------------------------- #
        # The two components lying in the slice plane form a vector the colour
        # map cannot show (it is one scalar at a time), so they are drawn as
        # arrows over it. Independent of the displayed component: on an XY slice
        # of E the arrows are always (Ex, Ey), whichever scalar is underneath.
        inplane = [c for c in str(getattr(obj, "InPlane", "")).split(",") if c]
        can_quiver = len(inplane) == 2 and all(c in comps for c in inplane)

        # Arrows sit on the shared square lattice (``_arrow_lattice``), clipped
        # to the *visible* axes: sites are recomputed on zoom, and arrow length
        # is measured in axes width rather than data units, so zooming in
        # yields its own arrows at the same on-screen size instead of a handful
        # of giant ones.
        def _arrow_sites(cx, cy):
            """Arrow sites over the visible axes; ``None`` if nothing is in view."""
            return _arrow_lattice(cx, cy, ax.get_xlim(), ax.get_ylim())

        def _build_quiver():
            """Create the arrow overlay, or ``None`` if its components are gone.

            Sites and scale are both taken from the current view, so the
            overlay looks the same at any zoom; one scale for the whole run
            keeps arrow length comparable between frames.
            """
            stacks = [_frames(c) for c in inplane]
            if any(s is None or len(s) == 0 for s in stacks):
                return None
            n0, n1 = stacks[0].shape[1], stacks[0].shape[2]
            cx = _centres(xedges, n0, size.x if have_size else 0.0)
            cy = _centres(yedges, n1, size.y if have_size else 0.0)
            sites = _arrow_sites(cx, cy)
            if sites is None:
                return None
            px, py, ix, iy = sites

            # Reference magnitude drawn at full arrow length (see
            # ``_arrow_reference``), read at the sites in view over every frame
            # -- so it is exactly a percentile of what the overlay will draw,
            # and it follows the view. Zoom into a quiet corner and its own
            # structure comes up to full length, instead of a lattice of
            # vectors all too short to draw; the price is that a length means
            # "strong for this view", which the colour bar -- fixed for the
            # whole run -- is there to qualify. It does *not* follow the frame:
            # one number for the whole run keeps the animation comparable, and
            # a wave would otherwise pump the arrows as it passed. Frames are
            # strided (and cast only then, since a CUDA run's stacks are
            # float32) to keep the gather small on a long run.
            sf = max(1, len(stacks[0]) // 256)
            ref = _arrow_reference(np.sqrt(
                np.asarray(stacks[0][::sf][:, ix, iy], dtype=float) ** 2
                + np.asarray(stacks[1][::sf][:, ix, iy], dtype=float) ** 2
            ))
            linthresh = ref / 1e3

            def _uv(idx):
                # One frame, read at the arrow sites (nearest cell).
                u = np.asarray(stacks[0][idx][ix, iy], dtype=float)
                v = np.asarray(stacks[1][idx][ix, iy], dtype=float)
                length = None
                if log_check.isChecked():
                    # Match the colour map's log compression, so both layers
                    # say the same thing about a weak field -- including which
                    # arrows survive the floor, since the compression lifts a
                    # weak one back above it.
                    m = np.sqrt(u ** 2 + v ** 2)
                    with np.errstate(divide="ignore", invalid="ignore"):
                        length = ref * (np.log10(1.0 + m / linthresh)
                                        / np.log10(1.0 + ref / linthresh))
                return _arrow_uv(u, v, ref, length)

            u0, v0 = _uv(slider.value())
            art = _add_quiver(ax, px, py, u0, v0, ref, _arrow_color(view["comp"]))
            quiver["art"], quiver["uv"] = art, _uv
            return art

        def _arrow_color(choice):
            """Arrow colour that reads against the current colour map."""
            return "white" if _one_sided(choice) else "black"

        quiver = {"art": None, "uv": None, "busy": False}

        def _resite(*_args):
            """Re-place the arrows for the current view (zoom or pan).

            A rebuild rather than a move: the site count follows the view, and
            ``Quiver`` fixes its arrow count at construction.
            """
            if quiver["art"] is None or quiver["busy"]:
                return
            quiver["busy"] = True
            try:
                visible = quiver["art"].get_visible()
                quiver["art"].remove()
                quiver["art"] = None
                art = _build_quiver()
                if art is not None:
                    art.set_visible(visible)
            finally:
                quiver["busy"] = False
            dialog._canvas.draw_idle()

        def _set_frame_data(idx):
            data2d = np.asarray(view["frames"][idx]).T
            if image["warp"] is not None:
                image["art"].set_data(image["warp"](np.asarray(data2d, dtype=float)))
            elif use_mesh:
                image["art"].set_array(data2d)
            else:
                image["art"].set_data(data2d)
            if quiver["art"] is not None:
                quiver["art"].set_UVC(*quiver["uv"](idx))

        # Arrows on the clipped end(s): with a percentile scale the hottest cells
        # are out of range by design, and the bar should say so rather than let
        # them pass for the extreme colour. A magnitude has no bottom end to
        # clip, so switching between the two kinds of map rebuilds the bar --
        # `extend` is fixed at construction.
        def _extend_for(choice):
            return "max" if _one_sided(choice) else "both"

        cbar = {"art": None, "extend": None}

        def _make_cbar(choice):
            ext = _extend_for(choice)
            if cbar["art"] is not None:
                if cbar["extend"] == ext:
                    # Re-point it: toggling smooth replaces the artist, and a
                    # bar left on the removed one stops tracking the norm.
                    cbar["art"].update_normal(image["art"])
                    cbar["art"].set_label(choice)
                    return
                cbar["art"].remove()
            cbar["art"] = figure.colorbar(
                image["art"], ax=ax, label=choice, extend=ext,
            )
            cbar["extend"] = ext

        _make_cbar(comp)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)

        plane = str(getattr(obj, "Plane", ""))
        off = float(obj.Offset.Value) if hasattr(obj, "Offset") else 0.0
        suffix = " ({} @ {:g} mm)".format(plane, off) if plane else ""

        def _frame_time(idx):
            if times is not None and idx < len(times):
                return units.time_from_si(float(times[idx]), unit)
            return float("nan")

        # A static solution is one frame: there is no time to report and no
        # animation to drive, so the title and the controls drop both rather
        # than reading "frame 1/1 at t = 0" as though the run had stopped there.
        static = len(frames) == 1

        def _set_title(idx):
            if static:
                ax.set_title("{}{}".format(view["comp"], suffix))
                return
            ax.set_title("{}{}\nframe {}/{}  t = {:.4g} {}".format(
                view["comp"], suffix, idx + 1, len(view["frames"]),
                _frame_time(idx), unit,
            ))

        _set_title(0)
        dialog._canvas.draw()

        # --- controls: component + slider + Play + log-scale toggle -------- #
        controls = QtWidgets.QHBoxLayout()
        play = QtWidgets.QPushButton("Play")
        play.setCheckable(True)
        slider = QtWidgets.QSlider(_QtCore.Qt.Horizontal)
        slider.setRange(0, len(frames) - 1)
        log_check = QtWidgets.QCheckBox("Log scale")
        component = None
        if len(choices) > 1:
            component = QtWidgets.QComboBox()
            component.addItems(choices)
            component.setCurrentText(comp)
            controls.addWidget(component)
        if static:
            play.setVisible(False)
            slider.setVisible(False)
            controls.addStretch(1)
        else:
            controls.addWidget(play)
            controls.addWidget(slider, 1)
        clip = QtWidgets.QComboBox()
        for pct in _CLIP_CHOICES:
            clip.addItem(
                "peak" if pct >= 100.0 else "{:g}%".format(pct), float(pct)
            )
        clip.setToolTip(
            "Percentile of |field| mapped to the end of the colour scale.\n"
            "Below 'peak', the hottest cells saturate so the rest of the\n"
            "domain uses the full colour range. Linear scale only."
        )
        controls.addWidget(QtWidgets.QLabel("Clip:"))
        controls.addWidget(clip)
        controls.addWidget(log_check)
        smooth_check = QtWidgets.QCheckBox("Smooth")
        smooth_check.setChecked(image["smooth"])
        smooth_check.setToolTip(
            "Shade between neighbouring cell centres instead of painting each\n"
            "cell flat. The frames are point samples at cell centres, so this\n"
            "adds no data -- it interpolates the same numbers. Turn it off to\n"
            "see the actual cells, or to speed up playback on a fine grid."
        )
        controls.addWidget(smooth_check)
        mask_check = None
        if pec_mask is not None:
            mask_check = QtWidgets.QCheckBox("Mask PEC")
            mask_check.setChecked(True)
            mask_check.setToolTip(
                "Treat conductors as holding no field: blank them, cut at the\n"
                "CAD surface rather than at the cell boundaries the solver\n"
                "stepped, and keep the zeros inside them out of the smoothing\n"
                "(which would otherwise drag them two cells outside and paint\n"
                "a false dark fringe along the metal).\n"
                "Turn it off to see what the run actually holds in there."
            )
            controls.addWidget(mask_check)
        geom_check = None
        if outlines:
            geom_check = QtWidgets.QCheckBox("Geometry")
            geom_check.setChecked(True)
            geom_check.setToolTip(
                "Outline each material's cross-section on this plane, in the\n"
                "material's own colour. Sectioned from the CAD as it is now,\n"
                "not from the voxels the run used."
            )
            controls.addWidget(geom_check)
        vector_check = None
        if can_quiver:
            vector_check = QtWidgets.QCheckBox("Vectors")
            vector_check.setToolTip(
                "Overlay the in-plane field vector ({}, {}) as arrows".format(*inplane)
            )
            controls.addWidget(vector_check)
        layout.addLayout(controls)

        def on_log(checked):
            # Log spans the full range on its own, so the clip has nothing to do.
            clip.setEnabled(not checked)
            image["art"].set_norm(_make_norm(bool(checked)))
            if quiver["art"] is not None:
                # Arrow lengths follow the same compression as the colour map.
                quiver["art"].set_UVC(*quiver["uv"](slider.value()))
            dialog._canvas.draw_idle()

        log_check.toggled.connect(on_log)

        def on_clip(idx):
            view["clip"] = float(clip.itemData(idx))
            image["art"].set_norm(_make_norm(log_check.isChecked()))
            dialog._canvas.draw_idle()

        clip.currentIndexChanged.connect(on_clip)

        def on_smooth(checked):
            # Shading is set when the mesh is built, so this rebuilds the
            # artist on the frame and scale currently shown. The quiver keeps
            # its own zorder above it and is untouched.
            _make_image(bool(checked), slider.value(), log_check.isChecked())
            _make_cbar(view["comp"])
            dialog._canvas.draw_idle()

        smooth_check.toggled.connect(on_smooth)

        def show_frame(idx):
            idx = max(0, min(int(idx), len(view["frames"]) - 1))
            _set_frame_data(idx)
            _set_title(idx)
            dialog._canvas.draw_idle()

        slider.valueChanged.connect(show_frame)

        def on_component(choice):
            """Switch the animated quantity, keeping the current frame."""
            choice = str(choice)
            stack = _frames(choice)
            if stack is None or len(stack) == 0:
                _missing(obj)
                # Put the combo back on what is actually displayed.
                component.blockSignals(True)
                component.setCurrentText(view["comp"])
                component.blockSignals(False)
                return
            view["comp"], view["frames"] = choice, stack
            image["art"].set_cmap(_cmap_for(choice))
            image["art"].set_norm(_make_norm(log_check.isChecked()))
            _make_cbar(choice)
            if quiver["art"] is not None:
                # The vector is the same; only its contrast with the map changes.
                quiver["art"].set_color(_arrow_color(choice))
            show_frame(slider.value())

        if component is not None:
            component.currentTextChanged.connect(on_component)

        def on_vectors(checked):
            """Show/hide the arrow overlay, building it on first use."""
            if checked and quiver["art"] is None and _build_quiver() is None:
                _missing(obj)
                vector_check.setChecked(False)
                return
            if quiver["art"] is not None:
                quiver["art"].set_visible(bool(checked))
                dialog._canvas.draw_idle()

        def on_mask(checked):
            # The patch and the smoother's dead-cell fill are one setting: with
            # the mask lifted the picture must be the run's own numbers, and a
            # filled cell showing under it would read as data.
            pec_mask.set_visible(bool(checked))
            image["fill"] = bool(checked)
            if image["warp"] is not None:
                _set_frame_data(slider.value())
            dialog._canvas.draw_idle()

        if mask_check is not None:
            mask_check.toggled.connect(on_mask)

        def on_geometry(checked):
            for art in outlines:
                art.set_visible(bool(checked))
            dialog._canvas.draw_idle()

        if geom_check is not None:
            geom_check.toggled.connect(on_geometry)

        if vector_check is not None:
            vector_check.toggled.connect(on_vectors)
            # Zoom/pan re-places the arrows, so a zoomed-in view gets its own
            # sites at the same density instead of the two that happened to
            # fall inside it. No-op until the overlay exists.
            ax.callbacks.connect("xlim_changed", _resite)
            ax.callbacks.connect("ylim_changed", _resite)

        timer = _QtCore.QTimer(dialog)
        timer.setInterval(100)  # ms between frames

        def advance():
            nxt = (slider.value() + 1) % len(view["frames"])
            slider.setValue(nxt)

        timer.timeout.connect(advance)

        def on_play(checked):
            play.setText("Pause" if checked else "Play")
            if checked:
                timer.start()
            else:
                timer.stop()

        play.toggled.connect(on_play)
        dialog._timer = timer  # keep the timer alive with the dialog

        dialog.show()
        _register_window(dialog)

    # ------------------------------------------------------------------ #
    # Electrostatic scalar results
    # ------------------------------------------------------------------ #

    # Capacitance spans many decades between a bond pad and a plate capacitor,
    # and reading 3.7e-13 F off a table is work the window can do instead.
    _CAP_UNITS = ((1e-3, "mF"), (1e-6, "uF"), (1e-9, "nF"),
                  (1e-12, "pF"), (1e-15, "fF"), (1e-18, "aF"))

    def _fmt_cap(value):
        """A capacitance in farads as a number and a unit, e.g. ``'1.234 pF'``."""
        mag = abs(float(value))
        if mag == 0.0:
            return "0"
        for scale, name in _CAP_UNITS:
            if mag >= scale:
                return "{:.4g} {}".format(value / scale, name)
        return "{:.3e} F".format(value)

    def _fill_matrix(table, names, rows, fmt):
        """Fill *table* with a square matrix labelled by *names* on both axes."""
        _QtCore, QtWidgets = _qt()
        n = len(names)
        table.setRowCount(n)
        table.setColumnCount(n)
        table.setHorizontalHeaderLabels(list(names))
        table.setVerticalHeaderLabels(list(names))
        for i in range(n):
            for j in range(n):
                item = QtWidgets.QTableWidgetItem(fmt(rows[i][j]))
                item.setFlags(_QtCore.Qt.ItemIsEnabled | _QtCore.Qt.ItemIsSelectable)
                item.setTextAlignment(
                    _QtCore.Qt.AlignRight | _QtCore.Qt.AlignVCenter)
                table.setItem(i, j, item)
        table.resizeColumnsToContents()

    def _show_electrostatic(obj):
        """Window for the electrostatic run's scalar results.

        Reports both capacitance conventions explicitly rather than calling one
        of them "the" capacitance matrix. Maxwell is dQ_i/dV_j with every other
        conductor grounded -- what the field solve measures and what circuit
        extraction consumes; mutual is the two-terminal capacitor people draw
        between pins. Reporting one under the other's name is wrong by more than
        a sign.
        """
        _QtCore, QtWidgets = _qt()

        names = list(getattr(obj, "Conductors", []) or [])
        potentials = list(getattr(obj, "Potentials", []) or [])
        charges = list(getattr(obj, "Charges", []) or [])
        cap_names = list(getattr(obj, "CapNames", []) or [])
        flat = [float(v) for v in (getattr(obj, "CapMaxwell", []) or [])]

        dialog = QtWidgets.QDialog(Gui.getMainWindow())
        dialog.setWindowTitle("Wavesim Results - {}".format(obj.Label))
        dialog.setAttribute(_QtCore.Qt.WA_DeleteOnClose, False)
        layout = QtWidgets.QVBoxLayout(dialog)

        header = QtWidgets.QLabel(
            "Field energy: {:.6g} J    Solver: {} ({:,} unknowns, {:,} "
            "iterations)".format(
                float(getattr(obj, "FieldEnergy", 0.0)),
                str(getattr(obj, "SolveMethod", "?")),
                int(getattr(obj, "Unknowns", 0)),
                int(getattr(obj, "Iterations", 0)),
            )
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        # Conductors: what was applied and what came back. The charge is the flux
        # the solved equations balanced, not a re-integration of the field, so it
        # agrees with the matrix below by construction.
        layout.addWidget(QtWidgets.QLabel("<b>Conductors</b>"))
        table = QtWidgets.QTableWidget(len(names), 3)
        table.setHorizontalHeaderLabels(["Conductor", "Potential (V)", "Charge (C)"])
        table.verticalHeader().setVisible(False)
        for row, name in enumerate(names):
            volts = potentials[row] if row < len(potentials) else 0.0
            charge = charges[row] if row < len(charges) else 0.0
            for col, text in enumerate(
                    (name, "{:g}".format(volts), "{:.6g}".format(charge))):
                item = QtWidgets.QTableWidgetItem(text)
                item.setFlags(_QtCore.Qt.ItemIsEnabled | _QtCore.Qt.ItemIsSelectable)
                if col:
                    item.setTextAlignment(
                        _QtCore.Qt.AlignRight | _QtCore.Qt.AlignVCenter)
                table.setItem(row, col, item)
        table.resizeColumnsToContents()
        table.setMaximumHeight(220)
        layout.addWidget(table)

        if cap_names and len(flat) == len(cap_names) ** 2:
            n = len(cap_names)
            maxwell = [flat[i * n:(i + 1) * n] for i in range(n)]
            # The two-terminal form, derived here exactly as the solver derives
            # it: off-diagonal -C_ij, diagonal the row sum (to ground).
            mutual = [[-maxwell[i][j] if i != j else sum(maxwell[i])
                       for j in range(n)] for i in range(n)]

            layout.addWidget(QtWidgets.QLabel(
                "<b>Maxwell (short-circuit) capacitance</b> — dQ<sub>i</sub>/"
                "dV<sub>j</sub> with every other conductor at 0 V. Each row sums "
                "to that conductor's capacitance to ground."))
            m_table = QtWidgets.QTableWidget()
            _fill_matrix(m_table, cap_names, maxwell, _fmt_cap)
            layout.addWidget(m_table)

            layout.addWidget(QtWidgets.QLabel(
                "<b>Mutual (two-terminal) capacitance</b> — the lumped capacitor "
                "between each pair; the diagonal is to ground."))
            t_table = QtWidgets.QTableWidget()
            _fill_matrix(t_table, cap_names, mutual, _fmt_cap)
            layout.addWidget(t_table)

            if all(abs(sum(row)) <= 1e-12 * max(abs(v) for v in row)
                   for row in maxwell if any(row)):
                layout.addWidget(QtWidgets.QLabel(
                    "Every row sums to zero: with no path to ground the Maxwell "
                    "matrix is genuinely rank-deficient — there is no ground to "
                    "have a capacitance to. The mutual capacitances are still "
                    "exact; this is the ordinary way a shielded structure "
                    "measures."))

        fused = list(getattr(obj, "CapFused", []) or [])
        if fused:
            note = QtWidgets.QLabel(
                "These bodies touch and are one conductor, so only the first of "
                "each group appears above: {}. Two solids that clear each other "
                "by less than a cell voxelise as one lump of metal.".format(
                    "; ".join(fused)))
            note.setWordWrap(True)
            layout.addWidget(note)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)

        dialog.resize(640, 520)
        dialog.show()
        _register_window(dialog)

    # ------------------------------------------------------------------ #
    # Port mode plotter
    # ------------------------------------------------------------------ #

    _NAN = float("nan")

    def _mode_data_from_leaf(obj):
        """Everything :func:`_draw_mode` needs, read from a saved mode leaf.

        Arrays come from the run's ``results.npz``; the geometry and
        per-unit-length parameters from the read-only properties
        :func:`_store_mode_meta` stashed on the leaf. ``None`` when the arrays
        are gone (the run output was moved or deleted).
        """
        workdir = str(obj.ResultsDir)
        key = str(obj.DataKey)
        phi = _load_array(workdir, key + "_phi")
        if phi is None:
            return None

        ecomps = [c for c in str(getattr(obj, "Ecomps", "")).split(",") if c]
        Ea = Eb = None
        if len(ecomps) >= 2:
            Ea = _load_array(workdir, "{}_E_{}".format(key, ecomps[0]))
            Eb = _load_array(workdir, "{}_E_{}".format(key, ecomps[1]))

        def _num(prop):
            return float(getattr(obj, prop, _NAN))

        return {
            "label": str(obj.Label),
            "port_name": str(getattr(obj, "PortName", "")),
            "phi": phi,
            "pec": _load_array(workdir, key + "_pec"),
            "Ea": Ea, "Eb": Eb,
            # Stored absolute, already in mm.
            "coords_a": list(getattr(obj, "CoordsA", []) or []),
            "coords_b": list(getattr(obj, "CoordsB", []) or []),
            "da": _num("Da"), "db": _num("Db"),
            "axis_a": str(getattr(obj, "AxisA", "a")),
            "axis_b": str(getattr(obj, "AxisB", "b")),
            "conductor_id": int(getattr(obj, "ConductorId", 0)),
            "conductor": str(getattr(obj, "Conductor", "")),
            "normal": str(getattr(obj, "Normal", "")),
            "position": _num("ModePosition"),
            "impedance": _num("Impedance"), "eps_eff": _num("EpsEff"),
            "capacitance": _num("Capacitance"), "inductance": _num("Inductance"),
            "v_phase": _num("VPhase"),
            "fmax": float(getattr(obj, "Fmax", 0.0)),
            "fields": str(getattr(obj, "Fields", "")),
        }

    def _mode_data_from_summary(workdir, meta):
        """The same fields, read straight from a solve's npz + ``summary["modes"]``.

        Used by :func:`show_mode_preview`, which has no document leaf to read
        from. Every array is pulled into memory here so the caller can delete the
        temp workdir as soon as the figure exists.
        """
        key = "mode_{}_{}".format(
            meta.get("source_index", 0), meta.get("mode_index", 0)
        )
        phi = _load_array(workdir, key + "_phi")
        if phi is None:
            return None

        ecomps = list(meta.get("Ecomps", []))
        Ea = Eb = None
        if len(ecomps) >= 2:
            Ea = _load_array(workdir, "{}_E_{}".format(key, ecomps[0]))
            Eb = _load_array(workdir, "{}_E_{}".format(key, ecomps[1]))

        def _coords(suffix):
            """Transverse cell centres as mm (the runner writes solver metres)."""
            arr = _load_array(workdir, key + suffix)
            return [] if arr is None else [float(v) * _MM_PER_M for v in arr]

        def _num(value):
            return _NAN if value is None else float(value)

        axes = meta.get("transverse_axes", ["a", "b"])
        name = meta.get("name", "port")
        conductor = str(meta.get("conductor", "") if meta.get("driven") else "")
        return {
            "label": "{} — energized {}".format(
                name, conductor or "conductor {}".format(
                    meta.get("conductor_id", "?"))
            ),
            "port_name": name,
            "conductor": conductor,
            "phi": phi,
            "pec": _load_array(workdir, key + "_pec"),
            "Ea": Ea, "Eb": Eb,
            "coords_a": _coords("_ca"), "coords_b": _coords("_cb"),
            "da": float(meta.get("da", 0.0)), "db": float(meta.get("db", 0.0)),
            "axis_a": str(axes[0]), "axis_b": str(axes[1]),
            "conductor_id": int(meta.get("conductor_id", 0)),
            "normal": str(meta.get("normal", "")),
            "position": float(meta.get("position", 0.0)),
            "impedance": _num(meta.get("impedance")),
            "eps_eff": _num(meta.get("eps_eff")),
            "capacitance": _num(meta.get("capacitance")),
            "inductance": _num(meta.get("inductance")),
            "v_phase": _num(meta.get("v_phase")),
            "fmax": float(meta.get("fmax", 0.0)),
            "fields": str(meta.get("fields", "")),
        }

    # What the mode window's checkboxes hold, and what a plot drawn without a
    # window (a caller that passes no options) gets. Both default on, as the
    # snapshot's do.
    _MODE_VIEW_DEFAULTS = {"smooth": True, "mask": True}

    def _draw_mode(figure, data, opts=None):
        """Draw a solved TEM mode into *figure*: φ map + E quiver + PEC outline.

        Mirrors :func:`wavesim.viz.plot_tem_mode` but works from the raw 2D arrays
        (FreeCAD's Python cannot import the solver), drawing with its own
        matplotlib. The port's per-unit-length parameters go in an annotation box.
        *figure* is cleared first, so the mode selector can redraw in place.

        *opts* is the view state from :func:`_mode_view_controls` -- which of
        smoothing and PEC masking are on.
        """
        import math

        import numpy as np

        figure.clear()
        ax = figure.add_subplot(111)

        phi = data["phi"]
        Na, Nb = phi.shape
        # Where each sample sits, in mm (the workbench's display unit). Prefer the
        # real transverse coordinate arrays from the runner (which honour a
        # non-uniform grid); fall back to a constant da/db spacing for older runs.
        #
        # φ and the PEC mask are **node**-indexed -- ``phi[i, j]`` is the
        # potential at node ``(a[i], b[j])`` -- and the runner's arrays are the
        # node coordinates to match. Everything below treats these purely as
        # sample positions (``_edges_from_centres`` builds midpoint edges around
        # whatever it is handed), so nothing here cares which lattice they name;
        # what matters is that they are the lattice the data is actually on.
        # Runs saved before that was fixed carry cell centres instead and plot
        # skewed on a graded mesh -- re-run the port to redraw them straight.
        #
        # The E components are the one thing still half a cell out: ``Ea[i]`` is
        # the field on the *edge* from node i to node i+1, so drawing it at node
        # i offsets it by half a cell along its own axis. Left as is deliberately
        # -- the arrows are decimated onto a fixed physical lattice many cells
        # wide, so the offset is well inside one arrow's own footprint, and
        # averaging the two edges onto the node would smear the exact zero that
        # marks the metal and put arrows on conductor surfaces.
        coords_a, coords_b = data["coords_a"], data["coords_b"]
        if len(coords_a) == Na and len(coords_b) == Nb:
            xa = np.asarray(coords_a)
            yb = np.asarray(coords_b)
        else:
            xa = np.arange(Na) * (data["da"] or 1.0) * 1.0e3
            yb = np.arange(Nb) * (data["db"] or 1.0) * 1.0e3

        opts = dict(_MODE_VIEW_DEFAULTS) if opts is None else opts
        pec = data["pec"]
        has_pec = pec is not None and np.any(pec)

        # φ is painted the way a snapshot paints its field, rather than as
        # contour bands: smoothing here means the same Catmull-Rom resample and
        # the same bilinear imshow, and turning it off shows the solver's actual
        # cells (pcolormesh on the cell edges, which honours a graded grid).
        # Nothing has to be kept out of the smoother the way a snapshot keeps
        # its conductor zeros out: φ inside the metal is that conductor's own
        # potential and joins continuously to the field outside it, so
        # interpolating across the surface invents no edge.
        if opts.get("smooth", True):
            fine, box = _smooth_grid(np.asarray(phi, dtype=float).T, xa, yb)
            art = ax.imshow(fine, origin="lower", extent=box, cmap="RdBu_r",
                            aspect="equal", interpolation="bilinear")
        else:
            art = ax.pcolormesh(_edges_from_centres(xa), _edges_from_centres(yb),
                                np.asarray(phi, dtype=float).T, cmap="RdBu_r")
        figure.colorbar(art, ax=ax, pad=0.02, label="potential φ (V)")

        # Blank the conductors: φ is constant in there and says nothing about
        # the mode, and painted at full scale each one is the loudest thing in
        # the picture. Cut from the saved PEC mask at the same 0.5 level the
        # outline below is drawn at, so the blanking and the outline are the
        # same curve rather than two near-misses. Under the arrows, over the
        # field map -- the zorder the snapshot's mask uses.
        if has_pec and opts.get("mask", True):
            ax.contourf(xa, yb, np.asarray(pec).T.astype(float),
                        levels=[0.5, 1.5], colors=[ax.get_facecolor()],
                        zorder=1.8)

        # E on the same arrow overlay the snapshot animation uses -- a lattice
        # of fixed physical pitch over the *visible* axes, lengths clipped to
        # one reference magnitude -- so a solved mode and a field frame of the
        # same port read alike. What it replaces strided the cell grid and left
        # the lengths to matplotlib's autoscale, and on any real port that drew
        # arrows several pitches long on a lattice as dense as the mesh: they
        # overlapped into a smear that showed neither direction nor magnitude,
        # worst exactly at the conductor where the field varies fastest and
        # matters most.
        Ea, Eb = data["Ea"], data["Eb"]
        arrows = {"art": None, "busy": False}

        def _build_arrows():
            """Draw the arrows for the current view; ``None`` if none are in it."""
            sites = _arrow_lattice(xa, yb, ax.get_xlim(), ax.get_ylim())
            if sites is None:
                return None
            px, py, ix, iy = sites
            # Reference over the sites in view rather than the whole
            # cross-section: zoomed in between the conductors, where the field
            # is a fraction of what it is at the inner one, the local structure
            # comes up to full length instead of vanishing under the floor.
            u_site, v_site = Ea[ix, iy], Eb[ix, iy]
            ref = _arrow_reference(np.sqrt(u_site ** 2 + v_site ** 2))
            u, v = _arrow_uv(u_site, v_site, ref)
            # ``_arrow_uv`` hands back NaN for the sites it hides -- inside the
            # metal (E is exactly zero there) and wherever the field is too weak
            # to draw as an arrow. Nothing redraws this artist in place, so drop
            # them outright rather than leave unrendered vertices in it.
            keep = np.isfinite(u) & np.isfinite(v)
            arrows["art"] = _add_quiver(ax, px[keep], py[keep], u[keep], v[keep],
                                        ref, "black")
            return arrows["art"]

        def _resite(*_args):
            """Re-place the arrows for the current view (zoom or pan).

            A rebuild rather than a move: both the site count and the scale
            follow the view, and ``Quiver`` fixes its arrow count at
            construction. Without this a zoom keeps the arrows it started with,
            so the picture thins out to a handful the further in one goes --
            the one thing the fixed-pitch lattice is meant to prevent.

            The redraw stays *inside* the busy guard. Drawing can itself settle
            the axes limits and so re-enter this callback, and on a canvas that
            draws synchronously that is unbounded recursion rather than one
            wasted rebuild.
            """
            if arrows["busy"]:
                return
            arrows["busy"] = True
            try:
                if arrows["art"] is not None:
                    arrows["art"].remove()
                    arrows["art"] = None
                _build_arrows()
                canvas = figure.canvas
                if canvas is not None:
                    canvas.draw_idle()
            finally:
                arrows["busy"] = False

        if has_pec:
            ax.contour(xa, yb, np.asarray(pec).T.astype(float),
                       levels=[0.5], colors="dimgray", linewidths=1.5)

        ax.set_aspect("equal")
        ax.set_xlabel("{} (mm)".format(data["axis_a"]))
        ax.set_ylabel("{} (mm)".format(data["axis_b"]))
        # ``subtitle`` (the preview's "mode i of N") goes on a second title line.
        title = data["label"]
        if data.get("subtitle"):
            title = "{}\n{}".format(title, data["subtitle"])
        ax.set_title(title)

        # Annotation box: every port parameter that was computed (NaN == skipped).
        c0 = 299792458.0
        z0, eps_eff = data["impedance"], data["eps_eff"]
        cap, ind, vph = data["capacitance"], data["inductance"], data["v_phase"]
        fmax = data["fmax"]

        lines = ["energized {}".format(
                     data.get("conductor")
                     or "conductor {}".format(data["conductor_id"])),
                 "{}-propagation @ {:.4g} mm".format(
                     data["normal"], data["position"] * 1.0e3)]
        if math.isfinite(z0):
            lines.append("Z₀ = {:.2f} Ω".format(z0))
        if math.isfinite(eps_eff):
            lines.append("ε_eff = {:.3f}".format(eps_eff))
        if math.isfinite(cap):
            lines.append("C = {:.4g} pF/m".format(cap * 1.0e12))
        if math.isfinite(ind):
            lines.append("L = {:.4g} nH/m".format(ind * 1.0e9))
        if math.isfinite(vph):
            lines.append("v = {:.4g} m/s ({:.1f}% c)".format(vph, 100.0 * vph / c0))
        if fmax > 0:
            lines.append("f_max = {:.4g} GHz".format(fmax / 1.0e9))
        if data["fields"]:
            lines.append("inject: {}".format(data["fields"]))

        ax.text(0.02, 0.98, "\n".join(lines), transform=ax.transAxes,
                va="top", ha="left", fontsize=8,
                bbox=dict(boxstyle="round", facecolor="white", alpha=0.75))

        # Arrows go on last, once the axes limits have stopped moving: both the
        # lattice and the scale are read off those limits, and every contour
        # added above autoscales the view as it lands -- which would fire the
        # re-site mid-assembly, over limits that are not the final ones.
        if Ea is not None and Eb is not None:
            Ea = np.asarray(Ea, dtype=float)
            Eb = np.asarray(Eb, dtype=float)
            _build_arrows()
            # The callbacks are held by the axes' own registry, and dropped with
            # it when the mode selector clears the figure to draw another mode.
            ax.callbacks.connect("xlim_changed", _resite)
            ax.callbacks.connect("ylim_changed", _resite)

    def _mode_view_controls(layout, redraw, has_pec):
        """Add the mode plot's Smooth / Mask PEC checkboxes to *layout*.

        Returns the options dict :func:`_draw_mode` reads; toggling a box
        updates it and calls *redraw*. The same two controls the snapshot
        animation carries, worded for a solved mode -- both windows that draw a
        mode share them, so the preview after a solve and the leaf reopened
        later out of the tree answer to the same switches.
        """
        _QtCore, QtWidgets = _qt()

        opts = dict(_MODE_VIEW_DEFAULTS)
        row = QtWidgets.QHBoxLayout()

        smooth = QtWidgets.QCheckBox("Smooth")
        smooth.setChecked(opts["smooth"])
        smooth.setToolTip(
            "Shade between neighbouring cell centres instead of painting each\n"
            "cell flat. φ is a point sample at each cell centre, so this adds\n"
            "no data -- it interpolates the same numbers. Turn it off to see\n"
            "the cells the mode was solved on."
        )
        row.addWidget(smooth)

        if has_pec:
            mask = QtWidgets.QCheckBox("Mask PEC")
            mask.setChecked(opts["mask"])
            mask.setToolTip(
                "Blank the conductors. φ in there is that conductor's own\n"
                "potential, constant and at the end of the colour scale, which\n"
                "makes the metal the loudest thing in a picture that is about\n"
                "the field between the conductors.\n"
                "Turn it off to see what the solve actually holds in there."
            )
            row.addWidget(mask)

            def on_mask(checked):
                opts["mask"] = bool(checked)
                redraw()

            mask.toggled.connect(on_mask)

        def on_smooth(checked):
            opts["smooth"] = bool(checked)
            redraw()

        smooth.toggled.connect(on_smooth)
        row.addStretch(1)
        layout.addLayout(row)
        return opts

    def _has_pec(data):
        """Whether *data* carries a PEC mask worth offering a switch for."""
        import numpy as np

        pec = data.get("pec")
        return pec is not None and bool(np.any(pec))

    def _plot_mode(obj):
        """Open the figure of a mode leaf saved by a run (double-click in the tree)."""
        data = _mode_data_from_leaf(obj)
        if data is None:
            _missing(obj)
            return
        made = _make_window("Wavesim Results - {}".format(obj.Label))
        if made is None:
            return
        dialog, figure, layout = made

        def redraw():
            _draw_mode(figure, data, opts)
            dialog._canvas.draw_idle()

        opts = _mode_view_controls(layout, redraw, _has_pec(data))
        _draw_mode(figure, data, opts)
        dialog._canvas.draw()
        dialog.show()
        _register_window(dialog)

    def show_mode_preview(workdir, summary):
        """Plot the modes of a "Compute Mode" solve, without touching the document.

        The preview's ``results.npz`` lives in a temp directory the caller deletes
        as soon as this returns (the modes are re-solved and saved by the next
        real run), so every array is read into the figure's data up front and no
        Results leaf is created.

        A port whose plane cuts several signal conductors solves one mode per
        conductor. They all share the one window: a ``<`` / ``>`` pair plus a
        dropdown scroll through them, and each mode names its energized conductor
        in the dropdown, the figure title and the parameter box. Returns ``False``
        when there was no plottable mode.
        """
        datas = []
        for meta in summary.get("modes", []):
            data = _mode_data_from_summary(workdir, meta)
            if data is not None:
                datas.append(data)
        if not datas:
            return False

        made = _make_window("Wavesim Mode - {}".format(datas[0]["port_name"]))
        if made is None:
            return False
        dialog, figure, layout = made

        # Which mode the switches redraw. One entry, so the ◀ / ▶ pair and the
        # checkboxes agree on what is on screen whichever moved last.
        shown = {"idx": 0}

        def redraw():
            _draw_mode(figure, datas[shown["idx"]], opts)
            dialog._canvas.draw_idle()

        total = len(datas)
        if total > 1:
            for idx, data in enumerate(datas):
                data["subtitle"] = "mode {} of {}".format(idx + 1, total)

            _QtCore, QtWidgets = _qt()
            row = QtWidgets.QHBoxLayout()
            prev = QtWidgets.QPushButton("◀")
            nxt = QtWidgets.QPushButton("▶")
            for button in (prev, nxt):
                button.setMaximumWidth(36)
            combo = QtWidgets.QComboBox()
            for idx, data in enumerate(datas):
                combo.addItem("Mode {} of {} — energized conductor {}".format(
                    idx + 1, total, data["conductor_id"]))
            row.addWidget(QtWidgets.QLabel("Solved modes:"))
            row.addWidget(combo, 1)
            row.addWidget(prev)
            row.addWidget(nxt)
            layout.addLayout(row)

            def show_mode(idx):
                idx = max(0, min(int(idx), total - 1))
                # The ends of the list are hard stops rather than wrapping, so
                # the buttons say how many modes are left to look at.
                prev.setEnabled(idx > 0)
                nxt.setEnabled(idx < total - 1)
                shown["idx"] = idx
                redraw()

            def step(delta):
                combo.setCurrentIndex(
                    max(0, min(combo.currentIndex() + delta, total - 1))
                )

            combo.currentIndexChanged.connect(show_mode)
            prev.clicked.connect(lambda *_: step(-1))
            nxt.clicked.connect(lambda *_: step(+1))
            prev.setEnabled(False)

        # A mask switch if any of the modes has metal to mask -- they are all
        # the same cross-section, so one answer covers the window.
        opts = _mode_view_controls(layout, redraw,
                                   any(_has_pec(d) for d in datas))
        _draw_mode(figure, datas[0], opts)
        dialog._canvas.draw()
        dialog.show()
        _register_window(dialog)
        return True
