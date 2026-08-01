# -*- coding: utf-8 -*-
"""Modal Port for the Wavesim workbench (replaces the Session 9 TEM source).

A *Modal Port* terminates one domain face with the TEM mode of the PEC
cross-section lying on it (a coax, stripline or microstrip port), and optionally
drives that mode inward. It is a scripted DocumentObject grouped under the
simulation's "Sources" child group.

The port is the boundary
------------------------
Solver-side this maps onto :class:`wavesim.sources.ModalPort`, an **impedance
sheet** registered with ``Simulation.add_boundary`` (not ``add_source``): each
step it writes the ghost tangential H just outside the face to the value a
matched continuation of the mode would carry,

    ``H_ghost = ±s · Y0 · (V̄ − 2a) · (n̂ × ê)``

with ``V̄`` the modal voltage read back off the plane -- sampled at ``n + h_tau``,
the same E<->H space-time offset ``dt/2 - dn/(2*v)`` the launch applies to its H
sheet, not the naive time-centred ``½(Vⁿ+Vⁿ⁻¹)`` -- and ``a`` the drive. The
``−2a`` term makes one expression both radiate a forward wave of ``a`` volts
inward *and* absorb whatever returns.

Two consequences shape this module:

* **The face carries no PML and no PEC.** The sheet absorbs the mode itself, with
  no reflection and -- unlike the CPML, which is propagating-only -- no DC error,
  so there is nothing left for an absorber behind it to do. The face also carries
  no background spacing: the port plane has to cut the real cross-section. All
  three are applied by ``domain.domain_grid_params`` via
  ``domain.modal_port_faces``, and the Domain panel shows such a face as a locked
  "Modal port" entry. (This is what the older matched-Thevenin TEM port could not
  do: it was a lumped drive on an *interior* plane and still needed a PML pad
  behind it.)
* **No mode-mesh refinement.** The port's profile ``ê`` is built by the solver as
  a forward difference of φ landed on the Yee edges, which makes it an exact null
  vector of *this grid's* transverse divergence (so the launch deposits no charge)
  and lets Z0 and the sheet's modal conductance share their discretisation error.
  Both properties are destroyed by solving on a finer mesh and interpolating back,
  and the Z0 so found is not the Z0 the FDTD grid actually presents. The mode is
  therefore always solved on the real coarse FDTD plane.

Drive modes
-----------
A modal port is driven one of two ways, chosen in its task panel
(``ExcitationMode``):

* **Waveform** -- a temporal excitation (Gaussian pulse, sine, ...). Its spec goes
  into the job's ``modal_ports``, and the runner builds the ``ws.ModalPort``. The
  excitation's Amplitude is the launched **forward-wave** voltage: the solver
  calibrates ``amplitude=1`` to land one forward volt on any grid or fill, so the
  workbench applies no scaling of its own.
* **SPICE** -- co-simulated in lockstep with an external ngspice netlist. This is
  still a lumped :class:`wavesim.sources.SpicePort` on an interior plane, so its
  spec goes into the job's ``spice_ports`` (``kind: "tem"``, built by
  :func:`wavesim_gui.spice_port.spice_tem_port_spec`) and **its face is still
  forced to PML**.

Both modes share the launch-plane geometry below; only the drive differs.

Workflow
--------
* The user adds a modal port and picks one of the six domain faces in the task
  panel; that face's boundary becomes the port itself (see above).
* The mode is solved out-of-process by the conda-side ``runner.py`` (it needs
  scipy/numba, unavailable in FreeCAD's Python). The panel's **Compute Mode**
  button runs a *mode-only* job (no FDTD time-stepping) for **that port alone**
  and plots the result; nothing is saved, because the main Run re-solves every
  port's mode just before stepping and stores those with its own results, as
  clickable Results-tree nodes. Either view shows the mode shape and the port's
  per-unit-length parameters (Z0, eps_eff, C, L, v).

Rendering
---------
The port draws as a translucent teal plane on the chosen face spanning the
domain box (mirroring the snapshot monitor's plane), so the port plane is visible
and the standard "eye" toggle shows/hides it. A matching teal arrow, anchored to
a plane corner and kept at a fixed on-screen size, shows the direction of energy
flow -- always *into* the simulation domain.

Units: FreeCAD geometry/properties are in millimetres; the solver works in
metres. :func:`modal_port_spec` converts the face plane's position to metres and
into the solver frame (measured from the domain origin) for the runner.

Legacy documents
----------------
This module was ``tem_source.py`` and its objects were marked
``WavesimType="TEMSource"``. :mod:`wavesim_gui.tem_source` survives as an
unpickling shim so those documents still load, and :meth:`ModalPortObject.
onDocumentRestored` re-stamps the marker; :func:`is_modal_port` accepts both.

Importing this module registers ``Wavesim_AddModalPort`` with ``Gui.addCommand``
when a GUI is available (plus the old ``Wavesim_AddTEMSource`` id, so macros and
saved toolbars keep working).
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
_MODAL_ICON = os.path.join(_RESOURCES_DIR, "tem_port.png")

_TYPE_PROP = "WavesimType"
_MODAL_TYPE = "ModalPort"
# Marker written by the pre-rename "TEM Source". Accepted by :func:`is_modal_port`
# and rewritten on restore, so documents saved before the rename keep working.
_LEGACY_TYPE = "TEMSource"

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

# Which transverse fields the *SPICE* drive injects on its interior plane:
# both E and H for a directional (one-way) launch, E only for a bidirectional
# one. Display label -> token. A **modal port has no such choice** -- an impedance
# sheet is inherently one-way -- so the ``Fields`` property below is read only in
# SPICE mode, and these are shared with :mod:`wavesim_gui.spice_port`.
_FIELDS_LABELS = ["E and H (directional)", "E only (bidirectional)"]
_FIELDS_TOKEN = {"E and H (directional)": "EH", "E only (bidirectional)": "E"}
_FIELDS_FROM_TOKEN = {v: k for k, v in _FIELDS_TOKEN.items()}

# How the port is driven. A modal port either launches a temporal *Waveform*
# excitation (the impedance-sheet ``ws.ModalPort``) or is co-simulated with an
# external *SPICE* circuit (a lumped ``ws.SpicePort`` on an interior plane). Both
# share the same launch-plane geometry; only the drive -- and, because of it, the
# face's boundary treatment -- differs. Display labels map to these tokens, stored
# in the object's ``ExcitationMode`` enum.
MODE_WAVEFORM = "Waveform"
MODE_SPICE = "SPICE"
_MODE_LABELS = {
    MODE_WAVEFORM: "Excitation waveform (modal port)",
    MODE_SPICE: "External SPICE circuit",
}
_MODE_ORDER = [MODE_WAVEFORM, MODE_SPICE]
_MODE_FROM_LABEL = {label: mode for mode, label in _MODE_LABELS.items()}

# Boundary condition forced on a SPICE-driven port's face (it drives an interior
# plane and needs the absorber behind it). A waveform-driven modal port sets no
# face BC at all -- it *is* the boundary, see ``domain.modal_port_faces``.
_SPICE_PORT_BC = "PML"

# Translucent teal plane, distinct from the orange monitor / green point source.
_PORT_COLOR = (0.0, 0.80, 0.80)
_PORT_TRANSPARENCY = 0.6

# Energy-flow arrow: kept at a fixed on-screen length (pixels) regardless of zoom.
_ARROW_PIXELS = 90.0

_MM_PER_M = 1000.0
_AXIS_IDX = {"x": 0, "y": 1, "z": 2}

# The two transverse axes of a face, in the solver's mode-slice order (matching
# ``wavesim.mode_solver._NORMAL_CFG``): the ``bounds`` rect is (a, b) in this
# order, so ``_bounds_rect_mm`` / ``modal_port_spec`` stay consistent with it.
_TRANSVERSE = {"x": ("y", "z"), "y": ("x", "z"), "z": ("x", "y")}


# --------------------------------------------------------------------------- #
# Document-object model
# --------------------------------------------------------------------------- #

class ModalPortObject:
    """``Proxy`` for a Modal Port document object.

    Properties:
        ``Face``       -- domain face the port terminates ('x0'..'z1').
        ``Conductor``  -- which solved mode to energize (0 = dominant).
        ``ExcitationMode`` -- Waveform (modal port) or SPICE (lumped co-sim).
        ``Fields``     -- transverse fields injected; **SPICE drive only** (a
                          modal impedance sheet is inherently one-way).
        ``Excitation`` + one property per waveform parameter (Gaussian pulse,
                          sine, rectangular, Gaussian+sine); added and kept in
                          sync by :func:`excitation.ensure_object_props`.

    Hidden ``Corners`` carries the port plane's four world-mm corners for the
    view provider; ``execute`` keeps them in sync with the domain bounds + face.
    """

    def __init__(self, obj):
        self.Type = _MODAL_TYPE
        obj.Proxy = self

        if not hasattr(obj, _TYPE_PROP):
            obj.addProperty(
                "App::PropertyString", _TYPE_PROP, "Wavesim",
                "Marks this object as a Wavesim modal port",
            )
            setattr(obj, _TYPE_PROP, _MODAL_TYPE)
            obj.setEditorMode(_TYPE_PROP, 1)  # read-only identity marker

        if not hasattr(obj, "Face"):
            obj.addProperty(
                "App::PropertyEnumeration", "Face", "Port",
                "Domain face this port terminates. The port is the boundary "
                "there: no PML, no PEC and no background spacing.",
            )
            obj.Face = list(_FACES)
            obj.Face = "z0"
        if not hasattr(obj, "Fields"):
            obj.addProperty(
                "App::PropertyEnumeration", "Fields", "Port",
                "Transverse fields injected by the SPICE drive: E and H "
                "(directional) or E only. Unused by the modal (waveform) drive, "
                "which is inherently one-way.",
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
        # mode solve to a sub-rectangle of the port face (empty = whole face).
        if not hasattr(obj, "BoundsSel"):
            obj.addProperty(
                "App::PropertyLinkSub", "BoundsSel", "Port",
                "Optional edge/face whose in-plane bounding box confines the "
                "mode solve to a sub-rectangle of the port face (empty = whole "
                "face). Set via the task panel.",
            )
            obj.setEditorMode("BoundsSel", 2)  # hidden; set via the task panel

        # Drive mode: temporal waveform or external SPICE circuit.
        if not hasattr(obj, "ExcitationMode"):
            obj.addProperty(
                "App::PropertyEnumeration", "ExcitationMode", "Port",
                "How the port is driven: a temporal Waveform excitation, or an "
                "external SPICE circuit (co-simulation).",
            )
            obj.ExcitationMode = list(_MODE_ORDER)
            obj.ExcitationMode = MODE_WAVEFORM

        # Excitation enum + one property per waveform parameter (shared scheme).
        exc.ensure_object_props(obj)
        # SPICE netlist/node/advanced properties (used when ExcitationMode==SPICE).
        _ensure_spice_props(obj)

        # Plane corners (hidden, four world-mm points) for the view provider.
        if not hasattr(obj, "Corners"):
            obj.addProperty("App::PropertyVectorList", "Corners", "Plane", "")
            obj.setEditorMode("Corners", 2)  # hidden

        _sync_mode_visibility(obj)

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = _MODAL_TYPE
        # Documents saved as a "TEM Source" carry the old identity marker (and
        # unpickle through the :mod:`wavesim_gui.tem_source` shim). Re-stamp it so
        # every lookup -- including ``domain.modal_port_faces``, which decides the
        # face's boundary treatment -- sees one type from here on.
        if getattr(obj, _TYPE_PROP, None) == _LEGACY_TYPE:
            setattr(obj, _TYPE_PROP, _MODAL_TYPE)
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
        # Back-fill the drive-mode enum on ports saved before it existed (they
        # were all waveform-driven; SPICE ports were a separate object type).
        if not hasattr(obj, "ExcitationMode"):
            obj.addProperty(
                "App::PropertyEnumeration", "ExcitationMode", "Port",
                "How the port is driven: a temporal Waveform excitation, or an "
                "external SPICE circuit (co-simulation).",
            )
            obj.ExcitationMode = list(_MODE_ORDER)
            obj.ExcitationMode = MODE_WAVEFORM
        # Re-run property setup so ports saved before the extra waveforms gain
        # the new options + parameter properties and editor modes are re-asserted.
        exc.ensure_object_props(obj)
        _ensure_spice_props(obj)
        _sync_mode_visibility(obj)

    def execute(self, obj):
        """Size/orient the drawn port plane to the domain bounds and face."""
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
        return {"Type": _MODAL_TYPE}

    def loads(self, state):
        # Legacy state carries Type="TEMSource"; normalise it (onDocumentRestored
        # rewrites the object's marker property to match).
        self.Type = _MODAL_TYPE
        return None

    __getstate__ = dumps
    __setstate__ = loads


# Property names carried for the SPICE drive mode (mirrors the group
# ``spice_port.ensure_spice_props`` adds); toggled visible only in SPICE mode.
_SPICE_PROPS = ("Netlist", "NodePlus", "NodeMinus",
                "UseInitialConditions", "InvertPortCurrent")


def _ensure_spice_props(obj):
    """Add the netlist/node/advanced properties for the SPICE drive mode.

    Delegates to :func:`wavesim_gui.spice_port.ensure_spice_props` (lazily
    imported to avoid the import cycle with that module), so a TEM source and a
    legacy SPICE TEM port expose exactly the same SPICE fields.
    """
    from wavesim_gui import spice_port as spice_mod
    spice_mod.ensure_spice_props(obj)


def excitation_mode(obj):
    """Return the port's drive mode token (``MODE_WAVEFORM`` / ``MODE_SPICE``)."""
    return str(getattr(obj, "ExcitationMode", MODE_WAVEFORM))


def _sync_mode_visibility(obj):
    """Show only the active drive mode's properties in the property editor.

    Waveform mode hides the SPICE fields; SPICE mode hides the excitation enum
    and all waveform parameters. (In waveform mode the waveform parameters'
    visibility is already managed by :func:`excitation.sync_visibility`.)
    """
    spice = excitation_mode(obj) == MODE_SPICE
    if hasattr(obj, "Excitation"):
        obj.setEditorMode("Excitation", 2 if spice else 1)
    if spice:
        for _key, prop in exc.PROP_FOR_KEY.items():
            if hasattr(obj, prop):
                obj.setEditorMode(prop, 2)  # hide all waveform params
    for prop in _SPICE_PROPS:
        if hasattr(obj, prop):
            obj.setEditorMode(prop, 1 if spice else 2)  # read-only vs hidden


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
    """Unit vector of energy flow *into* the domain from the port *face*.

    A port on a low face (``x0``/``y0``/``z0``) radiates in the +axis direction;
    one on a high face radiates in the -axis direction. Either way the wave flows
    inward, away from the face. (The solver's ``ModalPort`` derives the same thing
    from the face name; this only aims the viewport arrow.)
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

def is_modal_port(obj):
    """Return True if *obj* is a Wavesim Modal Port object.

    Accepts the pre-rename ``"TEMSource"`` marker as well, so a legacy document
    is recognised even before its proxy re-attaches and rewrites it (see
    :meth:`ModalPortObject.onDocumentRestored`).
    """
    return getattr(obj, _TYPE_PROP, None) in (_MODAL_TYPE, _LEGACY_TYPE)


def sources_group(sim):
    """Return the "Sources" child group of *sim* (or *sim* itself if missing)."""
    if sim is None:
        return None
    for child in sim.Group:
        if child.Name == _SOURCES_GROUP or child.Label == _SOURCES_GROUP:
            return child
    return sim


def find_modal_ports(sim):
    """Return all Modal Port objects under the Simulation container *sim*."""
    grp = sources_group(sim)
    if grp is None:
        return []
    return [obj for obj in grp.Group if is_modal_port(obj)]


def modal_port_spec(obj, origin_m):
    """Return the ``job.json`` ``modal_ports`` dict for *obj* in the solver frame.

    The port plane sits on the chosen domain face; its position along the face
    normal is taken from the domain box and shifted into the solver frame (the
    domain origin is subtracted, mirroring :func:`source.source_spec`). Because a
    modal-port face carries no PML pad and no background gap
    (``domain.domain_grid_params``), that plane lands on the grid boundary, where
    the geometry it must cut ends; the runner then nudges it the one cell inward
    the solver's ghost-H stencil needs (``runner._interior_position``).

    ``face`` is carried through verbatim: :class:`wavesim.sources.ModalPort` takes
    the face name and derives the ghost-H plane index and sign from it, so nothing
    here has to encode a propagation direction.
    """
    sim = active_simulation(obj.Document)
    dom = domain_mod.find_domain(sim) if sim else None
    face = str(obj.Face)
    axis = domain_mod.face_axis(face)
    world_mm = domain_mod.face_world_coord_mm(dom, face) if dom is not None else 0.0
    position = world_mm / _MM_PER_M - origin_m[_AXIS_IDX[axis]]
    spec = {
        "name": str(obj.Label or obj.Name),
        "normal": axis,
        "position": position,
        "face": face,
        "conductor_id": int(getattr(obj, "Conductor", 0)),
        "excitation": exc.spec_from_object(obj),
    }
    _add_bounds_spec(spec, dom, face, axis, getattr(obj, "BoundsSel", None), origin_m)
    return spec


def _add_bounds_spec(spec, dom, face, axis, bounds_sel, origin_m):
    """Attach a solver-frame ``"bounds"`` rect to *spec* when one is selected.

    Shared by the modal port and the SPICE TEM port. The world-mm in-plane rect
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

    Waveform mode uses the simulation's frequency unit (the rectangular pulse has
    no frequency); SPICE mode names the linked netlist file instead.
    """
    face = str(getattr(obj, "Face", "z0"))
    if excitation_mode(obj) == MODE_SPICE:
        from wavesim_gui import spice_port as spice_mod
        return "{}, {}".format(face, spice_mod._netlist_name(obj))
    doc = getattr(obj, "Document", None)
    sim = active_simulation(doc) if doc is not None else None
    return "{}, {}".format(face, exc.excitation_label(obj, sim))


# --------------------------------------------------------------------------- #
# GUI: view provider, task panel, command
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

    # The modal-port panel reuses the point source's excitation widgets/plot mixin.
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

    class ModalPortViewProvider:
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
            material.diffuseColor.setValue(*_PORT_COLOR)
            material.transparency.setValue(_PORT_TRANSPARENCY)
            root.addChild(material)

            self._coords = coin.SoCoordinate3()
            root.addChild(self._coords)
            self._face = coin.SoFaceSet()
            root.addChild(self._face)

            # Opaque border so the plane edges read clearly.
            border = coin.SoSeparator()
            bcolor = coin.SoBaseColor()
            bcolor.rgb.setValue(*_PORT_COLOR)
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
            acolor.rgb.setValue(*_PORT_COLOR)
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
            return _MODAL_ICON

        def setEdit(self, vobj, mode=0):
            _open_modal_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_modal_panel(vobj.Object)
            return True

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class TaskModalPortPanel(source_mod.ExcitationParamsMixin):
        """Task panel to edit a modal port: face, energized conductor and drive.

        The **Drive** combo picks how the port is excited — a temporal waveform
        (the excitation widgets, driving the modal impedance sheet) or an external
        SPICE circuit (netlist + port nodes) — showing one control group at a
        time. "Inject fields" belongs to the SPICE drive only and rides in its
        container: a modal sheet is inherently one-way and has no such choice.
        "Compute Mode" solves and visualises *this* port's mode now (out of
        process, no FDTD, nothing saved); OK commits the port and leaves the mode
        for the main Run. Cancel removes a freshly-created port so it leaves no
        trace.
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
            form.setWindowTitle("Wavesim Modal Port")
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

            layout.addRow("Port face:", self._face)
            layout.addRow("Energize conductor:", self._conductor)

            # Optional in-plane bounds: pick an edge/face whose bounding box
            # confines the mode solve to a sub-rectangle of the port face.
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

            # Drive mode: temporal waveform or external SPICE circuit. The two
            # sets of controls live in their own containers, shown one at a time.
            self._mode = QtWidgets.QComboBox()
            for m in _MODE_ORDER:
                self._mode.addItem(_MODE_LABELS[m], m)
            self._mode.setCurrentIndex(
                max(0, _MODE_ORDER.index(excitation_mode(obj)))
            )
            layout.addRow("Drive:", self._mode)

            # Waveform container: the excitation combo + parameter rows + preview
            # button (shared with the point-source panel), inside its own form.
            self._wave_widget = QtWidgets.QWidget()
            wave_form = QtWidgets.QFormLayout(self._wave_widget)
            wave_form.setContentsMargins(0, 0, 0, 0)
            self.build_excitation_ui(wave_form, QtWidgets)
            layout.addRow(self._wave_widget)

            # SPICE container: netlist path + the two port node names.
            self._spice_widget = QtWidgets.QWidget()
            spice_form = QtWidgets.QFormLayout(self._spice_widget)
            spice_form.setContentsMargins(0, 0, 0, 0)
            self._netlist = QtWidgets.QLineEdit(str(getattr(obj, "Netlist", "")))
            self._node_plus = QtWidgets.QLineEdit(
                str(getattr(obj, "NodePlus", "port1p"))
            )
            self._node_minus = QtWidgets.QLineEdit(
                str(getattr(obj, "NodeMinus", "0"))
            )
            from wavesim_gui import spice_port as spice_mod
            spice_form.addRow("Netlist:", spice_mod._netlist_row(
                QtWidgets, self._netlist, form))
            spice_form.addRow("Node + :", self._node_plus)
            spice_form.addRow("Node - :", self._node_minus)
            # Only the lumped SPICE launch chooses which transverse fields it
            # injects; the modal impedance sheet is one-way by construction.
            spice_form.addRow("Inject fields:", self._fields)
            layout.addRow(self._spice_widget)

            self._compute = QtWidgets.QPushButton("Compute Mode")
            layout.addRow(self._compute)

            self._mode.currentIndexChanged.connect(self._on_mode_changed)
            self._on_mode_changed()

            info = QtWidgets.QLabel(
                "The port carries the TEM mode of the PEC cross-section on the "
                "chosen face, which must cut at least two conductors. With the "
                "waveform drive it is an impedance sheet placed on that face: it "
                "launches the mode inward and simultaneously terminates the line, "
                "absorbing what comes back with no reflection and — unlike a PML "
                "— no DC error, so a Gaussian's DC content drains out instead of "
                "being stranded as static charge. The face therefore gets no PML, "
                "no PEC wall and no background gap (the geometry must reach the "
                "port plane); the Domain panel shows it as 'Modal port'. The "
                "excitation's Amplitude is the launched forward-wave voltage. "
                "Under 'Drive', pick a temporal "
                "waveform (preview with the plot button) or couple the port to an "
                "external ngspice netlist (SPICE co-simulation): the two nodes "
                "must already exist in the netlist and have a DC path to ground "
                "'0' — wavesim splices its own port companion across them (add no "
                "port component); the ngspice library path is set in Wavesim → "
                "Settings. A SPICE-driven port is still a lumped launch on an "
                "interior plane, so its face keeps a PML. "
                "'Compute Mode' solves and plots this port's mode(s) "
                "now, for viewing only; they are re-solved and saved when you Run. "
                "The mode is always solved on the actual simulation grid, so the "
                "Z₀ reported is the one the run really presents — and on the "
                "same conductor geometry, so with the Simulation's 'Conformal "
                "(cut-cell) PEC' on it is the conformal Z₀, not the staircased "
                "one (they differ by several percent on a round conductor). "
                "With several conductors on the face (e.g. two coax cross-sections), "
                "Compute Mode plots one mode per signal conductor (pick between "
                "them in the plot window) — set 'Energize conductor' to that "
                "conductor's N to drive it (0 launches the dominant mode). "
                "Optionally select an edge/face to confine the mode solve to its "
                "in-plane bounding box (e.g. a single connector's cross-section on "
                "a shared plane); Clear restores the whole face. "
                "Frequency and time units are set on the Simulation object."
            )
            info.setWordWrap(True)
            layout.addRow(info)

            # Live-update the drawn plane as the face changes.
            self._face.currentIndexChanged.connect(self._live_face)
            self._compute.clicked.connect(self._on_compute)

            self.form = form

        def _selected_face(self):
            return self._face.currentData() or _FACES[self._face.currentIndex()]

        def _selected_mode(self):
            return self._mode.currentData() or _MODE_ORDER[self._mode.currentIndex()]

        def _on_mode_changed(self, *_):
            """Show the controls for the selected drive mode, hide the other."""
            spice = self._selected_mode() == MODE_SPICE
            self._wave_widget.setVisible(not spice)
            self._spice_widget.setVisible(spice)

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
                self.form, "Wavesim Modal Port",
                "Select an edge or face in the 3D view first, then click "
                "'Select bounding edge/face'.",
            )

        def _clear_bounds(self, *_):
            self.obj.BoundsSel = None
            self._bounds_label.setText(_bounds_desc(self.obj))
            self.obj.Document.recompute()

        def _commit(self, title):
            """Write the widget values onto the object and settle the face's BC.

            A **waveform** port needs no stored boundary condition at all: it *is*
            the face's boundary, and ``domain.modal_port_faces`` overrides whatever
            the property says everywhere the grid is built. A **SPICE** port still
            drives an interior plane, so its face is forced to PML as before.

            Returns after committing + recomputing; the domain is re-synced so it
            re-sizes to the (possibly changed) port plane -- which for a modal port
            also drops that face's PML pad and background gap. Shared by Accept and
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
            self.obj.ExcitationMode = self._selected_mode()
            # Persist both drives' inputs (only the active one is read at run
            # time) so toggling the mode back and forth keeps what was entered.
            self.write_excitation(self.obj)
            self.obj.Netlist = self._netlist.text().strip()
            self.obj.NodePlus = self._node_plus.text().strip() or "port1p"
            self.obj.NodeMinus = self._node_minus.text().strip() or "0"
            _sync_mode_visibility(self.obj)
            self.obj.Label = "Modal Port ({})".format(_describe(self.obj))
            if self._selected_mode() == MODE_SPICE:
                # Lumped launch on an interior plane: the face behind it must
                # absorb, so force PML (a waveform port terminates itself).
                domain_mod.set_face_bc(
                    domain_mod.find_domain(active_simulation(doc)),
                    face, _SPICE_PORT_BC)
            doc.commitTransaction()
            doc.recompute()
            domain_mod.notify_domain_inputs_changed(doc)
            self._orig_face = face
            self._orig_bounds = new_bounds

        def _on_compute(self, *_):
            self._commit("Wavesim: Edit Modal Port")
            run_mode_solve(self.obj.Document, self.obj)

        def accept(self):
            self._commit("Wavesim: Edit Modal Port")
            Gui.Control.closeDialog()
            return True

        def reject(self):
            doc = self.obj.Document
            if self.created:
                doc.openTransaction("Wavesim: Cancel Modal Port")
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

    def _open_modal_panel(obj, created=False):
        """Open (or replace) the modal port task panel bound to *obj*."""
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskModalPortPanel(obj, created=created))

    def _isolate_port(spec, arrays, port_obj):
        """Cut a job *spec* down to the single mode-solved port *port_obj*.

        "Compute Mode" previews one port, so the other ports' (expensive) mode
        solves have nothing to do here. Ports are matched on the ``name`` their
        ``*_spec`` writes -- the object's label, the same key the runner echoes
        into ``summary["modes"]``. Returns ``False`` when *port_obj* has no entry
        in the job at all.

        *arrays* is accepted (and left alone) so the signature survives: the mode
        is solved on the run's own grid, so ``materials.npz`` carries nothing
        port-specific to prune.
        """
        name = str(getattr(port_obj, "Label", "") or getattr(port_obj, "Name", ""))
        modal = [t for t in spec.get("modal_ports") or [] if t.get("name") == name]
        spice = [p for p in spec.get("spice_ports") or []
                 if p.get("kind") == "tem" and p.get("name") == name]
        if not modal and not spice:
            return False
        spec["modal_ports"] = modal
        spec["spice_ports"] = spice
        return True

    def run_mode_solve(doc, port_obj):
        """Solve and plot the mode of *port_obj* out of process (no FDTD run).

        Builds the usual voxelised job, cuts it down to this one port (see
        :func:`_isolate_port`), flags it ``mode_only`` and runs the conda-side
        runner in a throwaway directory. The solved mode is plotted straight from
        there and the directory is deleted afterwards: the preview exists to be
        looked at, and a full Run re-solves every port's mode and saves those
        alongside its own results. *port_obj* is the modal port or legacy SPICE
        TEM port whose panel pressed "Compute Mode".
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
                "Wavesim: solving the mode of '{}' in {}\n".format(
                    port_obj.Label, workdir
                )
            )
            summary = run_mod.run_job(
                workdir, 1, parent=main,
                message="Preparing port mode solve...", busy=True,
            )
            if summary is None:
                return
            if not summary.get("modes"):
                QtWidgets.QMessageBox.information(
                    main, "Wavesim Mode Solve",
                    "No TEM mode was found. A port plane needs at least two PEC "
                    "conductors on it (e.g. a signal conductor and a "
                    "ground/shield) — check the geometry actually reaches the "
                    "port face.",
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

    class CommandAddModalPort:
        """Create a Modal Port on a domain face and open its editor."""

        def GetResources(self):
            return {
                "Pixmap": _MODAL_ICON,
                "MenuText": "Add Modal Port",
                "ToolTip": "Add a modal waveguide port that terminates a domain "
                "face with the TEM mode of the PEC cross-section there — "
                "launching the mode inward and absorbing what returns (exactly, "
                "and at DC), with no PML needed on that face. Driven by a "
                "temporal waveform or an external SPICE circuit (chosen in the "
                "task panel)",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = active_simulation(doc)
            if sim is None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: create a Simulation before adding a modal port.\n"
                )
                return

            doc.openTransaction("Wavesim: Add Modal Port")
            try:
                port = doc.addObject("App::FeaturePython", "ModalPort")
                ModalPortObject(port)
                port.Label = "Modal Port ({})".format(_describe(port))
                if port.ViewObject is not None:
                    ModalPortViewProvider(port.ViewObject)
                sources_group(sim).addObject(port)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()

            _open_modal_panel(port, created=True)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    Gui.addCommand("Wavesim_AddModalPort", CommandAddModalPort())
    # The pre-rename id, kept registered so saved toolbars, custom menus and user
    # macros that reference it keep working.
    Gui.addCommand("Wavesim_AddTEMSource", CommandAddModalPort())
