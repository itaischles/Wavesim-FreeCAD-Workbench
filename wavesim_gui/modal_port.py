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

  On a multi-conductor cross-section the drive is a **conductor table**: one row
  per conductor the port terminates, each with its own waveform and amplitude, so
  the launch is their superposition ``Σ aᵢ·fᵢ(t)·modeᵢ``. Each row becomes its
  own ``modal_ports`` entry (co-planar ``ws.ModalPort`` sheets superpose on the
  ghost H plane by construction) and records its own V(t)/I(t). Which mode a row
  drives is settled by geometry -- a point inside that conductor's cross-section,
  where the solved φ is 1 V -- not by the solver's raster-order conductor
  numbering. See the conductor-table section below.
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
The port draws as a translucent amber plane on the chosen face spanning the
domain box (mirroring the snapshot monitor's plane), so the port plane is visible
and the standard "eye" toggle shows/hides it. A matching amber arrow, anchored to
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
from wavesim_gui import labels as labels_mod


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_RESOURCES_DIR = os.path.join(_WB_DIR, "Resources")
# The 24x24 SVG icon set (grouped by colour: blue setup, amber sources,
# teal monitors). The retired PNGs are still in Resources/ alongside it.
_ICONS_DIR = os.path.join(_RESOURCES_DIR, "icons")
_MODAL_ICON = os.path.join(_ICONS_DIR, "port_modal.svg")

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

# Translucent amber plane: the deep end of the source/port group's icon
# palette (#c9741a), against the teal a monitor draws in.
_PORT_COLOR = (0.788, 0.455, 0.102)
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
                "Mode'). 0 = the dominant (first) mode. Legacy fallback, read "
                "only when the port carries no conductor table.",
            )
            obj.Conductor = 0

        # The conductor table: which conductors this port terminates, which of
        # them it energizes, and each one's own waveform (see ``_ensure_table_props``).
        _ensure_table_props(obj)

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
                "Mode'). 0 = the dominant (first) mode. Legacy fallback, read "
                "only when the port carries no conductor table.",
            )
            obj.Conductor = 0
        # Back-fill the conductor table. It comes back **empty** on a port saved
        # before it existed, which is exactly what keeps that port on its old
        # single-mode behaviour until the user opens the panel (see
        # :func:`drive_rows`).
        _ensure_table_props(obj)
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

    Waveform mode hides the SPICE fields; SPICE mode hides every drive row's
    excitation enum and waveform parameters. (In waveform mode the parameters'
    visibility is already managed by :func:`excitation.sync_visibility`, per row.)
    The active mode's properties stay editable (mode 0) rather than read-only,
    so the property editor lets an expression drive them -- see
    :mod:`wavesim_gui.expressions`.
    """
    spice = excitation_mode(obj) == MODE_SPICE
    for index in range(max(drive_row_count(obj), 1)):
        if spice:
            exc.hide_props(obj, index)
        else:
            enum_prop = exc.excitation_prop(index)
            if hasattr(obj, enum_prop):
                obj.setEditorMode(enum_prop, 0)
            exc.sync_visibility(obj, index)
    for prop in _SPICE_PROPS:
        if hasattr(obj, prop):
            obj.setEditorMode(prop, 0 if spice else 2)  # editable vs hidden


# --------------------------------------------------------------------------- #
# The conductor table: which conductors the port terminates, and how it drives
# them
# --------------------------------------------------------------------------- #
#
# A modal port on a multi-conductor cross-section has one TEM mode per signal
# conductor, and the launch the user wants is a **superposition** of them,
#
#     E(t) = Σ_i  a_i · f_i(t) · ê_i
#
# with an independent amplitude and waveform per conductor. Solver-side that is
# not one object but N: :class:`wavesim.sources.ModalPort` sheets sharing a face
# superpose on the ghost H plane by construction (``sources._GhostPlaneGroup``
# clears it once and sums the contributors), so this port emits **one job
# ``modal_ports`` entry per table row**, and the plane carries the sum. Each row
# is therefore also a port in its own right: it records its own V(t)/I(t)
# against its own Z_ref, which is what makes a multi-mode S-matrix fall out.
#
# Every listed conductor gets a sheet, driven or not. An undriven row launches
# nothing (amplitude 0) but still **terminates its mode** -- without it that mode
# has no absorber on a face that carries no PML, and it reflects.
#
# Which mode belongs to which row is settled geometrically, not by the solver's
# labelling: each row carries a point inside its conductor's cross-section
# (``conductor_point``, see :func:`conductor_plane_point_mm`), and the runner
# picks the mode whose φ is 1 V there. ``mode_solver._solve_one`` pins φ to
# exactly 1.0 on the energized conductor and 0.0 on every other one, so the match
# is exact and owes nothing to ``ndimage.label``'s raster ordering -- which is
# what the old integer ``Conductor`` had to be read against, and why it could
# only be chosen *after* a mode solve had been run and looked at.

# Row conductors: the PEC body per row, in row order.
#
# An ``App::PropertyLinkList`` and a parallel ``App::PropertyStringList``, not the
# ``App::PropertyLinkSubList`` this obviously wants to be. That property **groups
# its entries by object**: ``[(b,"Face13"), (b,"Face16")]`` comes back as one
# ``(b, ("Face13","Face16"))`` pair. Two rows on one body -- exactly the case
# this feature exists for, a shield and its pins padded from one sketch into a
# single Part object -- would collapse into one row, and interleaved rows would
# come back reordered, sliding every row off its ``Energized`` flag and its
# excitation property set. A LinkList keeps duplicates and order (verified across
# a save/reload), and the subnames ride beside it.
_CONDUCTORS_PROP = "Conductors"
# Per-row face naming which cross-section region of that body the row drives
# ("" = the body's largest region, which is all a single-conductor body has).
_CONDUCTOR_FACES_PROP = "ConductorFaces"
# Per-row launch flag; False = terminate only (amplitude forced to 0).
_ENERGIZED_PROP = "Energized"


def _ensure_table_props(obj):
    """Add/refresh the conductor-table properties and every row's waveform props.

    Idempotent. On a port saved before the table existed all three lists come
    back empty, which :func:`drive_rows` reads as "no table" and answers with the
    legacy single-drive row -- so such a document runs exactly as it did.
    """
    # A ``Conductors`` of the wrong type is from a pre-release build of this
    # feature; drop it (with its rows) rather than read it as something it is not.
    if hasattr(obj, _CONDUCTORS_PROP):
        try:
            kind = obj.getTypeIdOfProperty(_CONDUCTORS_PROP)
        except Exception:
            kind = "App::PropertyLinkList"
        if kind != "App::PropertyLinkList":
            try:
                obj.removeProperty(_CONDUCTORS_PROP)
            except Exception:
                pass
    if not hasattr(obj, _CONDUCTORS_PROP):
        obj.addProperty(
            "App::PropertyLinkList", _CONDUCTORS_PROP, "Port",
            "Conductors this port terminates, one per drive row: the PEC body "
            "each row's conductor belongs to. Edited in the task panel's "
            "conductor table.",
        )
        obj.setEditorMode(_CONDUCTORS_PROP, 1)  # read-only: the table owns it
    if not hasattr(obj, _CONDUCTOR_FACES_PROP):
        obj.addProperty(
            "App::PropertyStringList", _CONDUCTOR_FACES_PROP, "Port",
            "Per drive row: a face of that row's body which the port plane "
            "crosses, naming which cross-section region (which conductor) the "
            "row drives. Empty = the body's largest region.",
        )
        obj.setEditorMode(_CONDUCTOR_FACES_PROP, 1)  # read-only: the table owns it
    if not hasattr(obj, _ENERGIZED_PROP):
        obj.addProperty(
            "App::PropertyBoolList", _ENERGIZED_PROP, "Port",
            "Per drive row: True launches that conductor's mode with the row's "
            "waveform, False terminates it only (amplitude 0).",
        )
        obj.setEditorMode(_ENERGIZED_PROP, 1)  # read-only: the table owns it
    # One excitation property set per row (row 0 is the port's original one).
    for index in range(drive_row_count(obj)):
        exc.ensure_object_props(obj, index)


def drive_row_count(obj):
    """Number of rows in *obj*'s conductor table (0 ⇒ no table)."""
    return len(getattr(obj, _CONDUCTORS_PROP, None) or [])


def conductor_label(body, sub=""):
    """Human label of one drive row's conductor."""
    if body is None:
        return "?"
    return region_name(body, sub, 2 if sub else 1)


def drive_rows(obj):
    """``[(body, sub, energized, excitation_index), ...]`` for *obj*'s table.

    Empty when the port carries no table -- callers then fall back to the legacy
    single drive (the whole face, the integer ``Conductor``, excitation index 0).
    """
    bodies = list(getattr(obj, _CONDUCTORS_PROP, None) or [])
    subs = list(getattr(obj, _CONDUCTOR_FACES_PROP, None) or [])
    flags = list(getattr(obj, _ENERGIZED_PROP, None) or [])
    rows = []
    for index, body in enumerate(bodies):
        if body is None:
            continue
        sub = str(subs[index]) if index < len(subs) else ""
        # A row added before the flag list caught up defaults to energized: a
        # conductor the user went out of their way to pick is one they meant.
        energized = bool(flags[index]) if index < len(flags) else True
        rows.append((body, sub, energized, index))
    return rows


def set_drive_rows(obj, conductors, energized):
    """Replace *obj*'s conductor table with *conductors* / *energized*.

    *conductors* is a list of ``(body, sub)`` pairs (a bare body is accepted and
    means its largest cross-section region). *sub* names a face of that body the
    port plane crosses, which is what picks out one conductor of a body holding
    several.

    Adds the waveform property set each new row needs and removes the ones a
    shrunk table no longer uses, so the property editor never shows an orphan
    row. Row 0's properties are never removed -- they are the set every
    single-excitation source carries and the legacy fallback still reads.
    """
    _ensure_table_props(obj)
    old_count = drive_row_count(obj)
    bodies, subs = [], []
    for item in conductors:
        body, sub = item if isinstance(item, (tuple, list)) else (item, "")
        bodies.append(body)
        subs.append(str(sub or ""))
    setattr(obj, _CONDUCTORS_PROP, bodies)
    setattr(obj, _CONDUCTOR_FACES_PROP, subs)
    setattr(obj, _ENERGIZED_PROP, [bool(e) for e in energized])
    for index in range(len(bodies)):
        exc.ensure_object_props(obj, index)
    for index in range(len(bodies), old_count):
        exc.remove_object_props(obj, index)


# --------------------------------------------------------------------------- #
# Conductor cross-sections on the port plane
# --------------------------------------------------------------------------- #

def _plane_point(axis, coord, a, b):
    """A world point at ``axis == coord`` with transverse coordinates *(a, b)*."""
    ax_a, ax_b = _TRANSVERSE[axis]
    d = {axis: coord, ax_a: a, ax_b: b}
    return FreeCAD.Vector(d["x"], d["y"], d["z"])


def _plane_face(bbox, axis, coord, pad=10.0):
    """A bounded planar face at ``axis == coord`` covering *bbox* plus *pad* mm.

    Built from four explicit corners rather than ``Part.makePlane``, whose local
    axes are derived from the normal and do not span the transverse pair for
    every face orientation (a y-normal plane came out degenerate).
    """
    import Part

    ax_a, ax_b = _TRANSVERSE[axis]
    lo = {"x": bbox.XMin, "y": bbox.YMin, "z": bbox.ZMin}
    hi = {"x": bbox.XMax, "y": bbox.YMax, "z": bbox.ZMax}
    a0, a1 = lo[ax_a] - pad, hi[ax_a] + pad
    b0, b1 = lo[ax_b] - pad, hi[ax_b] + pad
    pts = [_plane_point(axis, coord, a0, b0), _plane_point(axis, coord, a1, b0),
           _plane_point(axis, coord, a1, b1), _plane_point(axis, coord, a0, b1)]
    return Part.Face(Part.makePolygon(pts + [pts[0]]))


# Tolerance for ``Face.isInside`` (mm). Small against any cell the port is
# solved on, large against OCC's own surface tolerance.
_INSIDE_TOL = 1.0e-7


def _section_faces(shape, axis, coord):
    """The cross-section faces of *shape* on the plane ``axis == coord``.

    Empty when the shape does not reach the plane. Uses a boolean common with a
    bounded plane face rather than ``Shape.slice``, so a hollow or multi-part
    cross-section comes back as real faces whose interior can be tested.
    """
    if shape is None:
        return []
    try:
        section = shape.common(_plane_face(shape.BoundBox, axis, coord))
    except Exception:
        return []
    return list(getattr(section, "Faces", []) or [])


def _face_interior_point(face, axis, coord):
    """A point strictly inside *face* (world mm), or ``None``.

    The centroid first -- exact and free for the convex cross-sections most
    conductors have -- then a widening scan of the face's bounding box, because a
    coax shield's cross-section is an annulus whose centroid lies in the *hole*,
    i.e. in the other conductor.
    """
    com = face.CenterOfMass
    if face.isInside(com, _INSIDE_TOL, True):
        return com
    bbox = face.BoundBox
    ax_a, ax_b = _TRANSVERSE[axis]
    lo = {"x": bbox.XMin, "y": bbox.YMin, "z": bbox.ZMin}
    hi = {"x": bbox.XMax, "y": bbox.YMax, "z": bbox.ZMax}
    for n in (5, 11, 23, 47):
        for i in range(1, n):
            a = lo[ax_a] + (hi[ax_a] - lo[ax_a]) * i / n
            for j in range(1, n):
                b = lo[ax_b] + (hi[ax_b] - lo[ax_b]) * j / n
                point = _plane_point(axis, coord, a, b)
                if face.isInside(point, _INSIDE_TOL, True):
                    return point
    return None


def _plane_coords_mm(dom, face):
    """Candidate section coordinates (world mm) for the *face*'s port plane.

    The face plane itself first. A modal-port face carries no background gap, so
    it sits exactly where the geometry ends and a conductor's flat end cap is
    coplanar with it -- which sections cleanly, but leaves nothing to fall back on
    if the body stops a hair short of the boundary. The second candidate is half a
    cell **inward**, where the mode is actually solved (the runner nudges the
    plane one cell in), so a conductor that does not quite reach the face is still
    found on the plane that matters.
    """
    if dom is None:
        return []
    coord = domain_mod.face_world_coord_mm(dom, face)
    inward = 1.0 if face.endswith("0") else -1.0
    axis = domain_mod.face_axis(face)
    try:
        step = domain_mod.min_spacings_m(dom)[_AXIS_IDX[axis]] * _MM_PER_M
    except Exception:
        step = 0.0
    if step <= 0.0:
        return [coord]
    return [coord, coord + inward * 0.5 * step, coord + inward * 1.5 * step]


def conductor_plane_point_mm(body, dom, face, sub=None):
    """A point inside a conductor's cross-section on *face*'s port plane (mm).

    Returns ``(a, b)`` in the face's two transverse axes (solver slice order, see
    :data:`_TRANSVERSE`) or ``None`` when nothing is found. This is what makes the
    conductor→mode assignment deterministic: the runner reads the solved φ at this
    point, and φ is exactly 1 V on the energized conductor.

    *sub* names one of *body*'s faces (``"Face7"``, as picked in the 3D view). The
    **cross-section region that face bounds** is the conductor -- which is what
    lets one Part object hold several conductors, the normal case for a shield and
    its pins padded from one sketch (three solids, sixteen faces, one ``Body``).
    Without *sub* the largest region is taken, which is what a one-conductor body
    means and what this did before regions existed.
    """
    region = _region_on_plane(body, dom, face, sub)
    return None if region is None else region["point"]


def _subshape(body, sub):
    """Resolve ``body.Shape``'s named sub-element, or ``None``."""
    shape = getattr(body, "Shape", None)
    if shape is None or not sub:
        return shape
    try:
        return shape.getElement(sub)
    except Exception:
        return None


# How close a sub-element's plane-section must lie to a cross-section region to
# count as bounding it (mm). A picked face's section lies *on* the region's
# boundary, so this only absorbs OCC's own tolerance.
_ON_REGION_TOL = 1.0e-4


def _region_records(body, dom, face):
    """Every disjoint cross-section region of *body* on *face*'s port plane.

    ``[{"sub", "point", "area", "faces"}, ...]``, largest first. Each record is
    one conductor as the mode solver sees it: ``ndimage.label`` on the plane
    splits metal into connected components, and two solids of one Part object --
    or one U-shaped solid -- are separate components with separate modes. So the
    unit the port's table addresses is a *region*, not a body.

    ``sub`` is the name of a face of *body* that bounds the region (``""`` if
    none could be attributed), which is what gives the row a stable identity to
    store and what a 3D-view pick resolves against.
    """
    shape = getattr(body, "Shape", None)
    if shape is None:
        return []
    axis = domain_mod.face_axis(face)
    ax_a, ax_b = _TRANSVERSE[axis]
    for coord in _plane_coords_mm(dom, face):
        regions = _section_faces(shape, axis, coord)
        if not regions:
            continue
        # The body's own faces, sectioned once each, so every region can be
        # attributed to one without re-cutting the solid per region.
        cuts = []
        for idx, bface in enumerate(shape.Faces):
            cut = _section_edges(bface, axis, coord)
            if cut is not None:
                cuts.append(("Face{}".format(idx + 1), cut))

        out = []
        for sec in sorted(regions, key=lambda f: -f.Area):
            point = _face_interior_point(sec, axis, coord)
            if point is None:
                continue
            out.append({
                "sub": _bounding_sub(sec, cuts),
                "point": (getattr(point, ax_a), getattr(point, ax_b)),
                "area": float(sec.Area),
                "shape": sec,
            })
        if out:
            for record in out:
                record["count"] = len(out)
            return out
    return []


def storage_sub(record):
    """The face a row should *store* for a region record.

    Empty for a body cutting the plane once: there is nothing to disambiguate,
    and storing a face would only pin the row to a number OCC may renumber and
    make the row read "Rod A (Face3)" where "Rod A" is the whole truth. A body
    holding several conductors stores the face, which is the only thing telling
    its rows apart.
    """
    return record["sub"] if record.get("count", 1) > 1 else ""


def _section_edges(shape, axis, coord):
    """*shape* cut by the plane ``axis == coord``, or ``None`` if it misses it.

    Used on a single face of a solid, where the cut is a curve rather than an
    area -- a rod's cylindrical wall meets the port plane in the circle bounding
    that rod's cross-section.
    """
    try:
        cut = shape.common(_plane_face(shape.BoundBox, axis, coord))
    except Exception:
        return None
    return cut if getattr(cut, "Edges", None) else None


def _bounding_sub(region, cuts):
    """Name of the face in *cuts* whose plane-section bounds *region*.

    *cuts* is ``[(subname, section_shape), ...]``. The winner is the one lying
    **on** the region -- distance ~0 -- which is exactly the relation "this face
    is part of this conductor's surface". Distance rather than a containment
    test, because the section of a lateral face *is* the region's boundary curve
    and boundary containment is the one case point-in-face predicates disagree
    about. ``""`` when nothing lies on it.
    """
    best, best_d = "", _ON_REGION_TOL
    for sub, cut in cuts:
        try:
            d = region.distToShape(cut)[0]
        except Exception:
            continue
        if d < best_d:
            best, best_d = sub, d
    return best


def _region_on_plane(body, dom, face, sub=None):
    """The cross-section region of *body* that *sub* bounds (or the largest).

    A picked face is matched to its region by sectioning it and taking the region
    that section lies on, rather than by trusting the stored region order: OCC
    renumbers faces on a geometry edit, but the face the user picked still cuts
    the conductor they meant.
    """
    regions = _region_records(body, dom, face)
    if not regions:
        return None
    if not sub:
        return regions[0]
    for record in regions:
        if record["sub"] == sub:
            return record
    # The stored subname no longer attributes to the same region (a renumbered
    # or reshaped body): re-derive from the picked face's own section.
    axis = domain_mod.face_axis(face)
    picked = _subshape(body, sub)
    if picked is None:
        return None
    for coord in _plane_coords_mm(dom, face):
        cut = _section_edges(picked, axis, coord)
        if cut is None:
            continue
        best, best_d = None, None
        for record in regions:
            try:
                d = record["shape"].distToShape(cut)[0]
            except Exception:
                continue
            if best_d is None or d < best_d:
                best, best_d = record, d
        if best is not None and best_d is not None and best_d <= _ON_REGION_TOL:
            return best
    return None


def _solve_region_rect_mm(dom, face, bounds_sel):
    """The in-plane rect the mode solve grounds the edge of (world mm), or None.

    ``mode_solver`` pins φ=0 on the edge of the region it solves, which is the
    ``bounds`` sub-rect when one is given and otherwise **the whole grid plane --
    PML padding included**, not the inner domain box. The distinction decides
    whether an outer shield is the grounded reference or a signal conductor with
    a mode of its own: with an absorber on the transverse faces the shield stops
    short of the grid edge and the solver gives it a mode. Reading the Domain's
    padded node arrays is what keeps this panel's answer the solver's answer.
    """
    rect = _bounds_rect_mm(dom, face, bounds_sel)
    if rect is not None:
        return rect
    if dom is None:
        return None
    ax_a, ax_b = _TRANSVERSE[domain_mod.face_axis(face)]
    nodes = dict(zip(("x", "y", "z"), domain_mod.node_coords_mm(dom)))
    na, nb = nodes.get(ax_a) or [], nodes.get(ax_b) or []
    if len(na) >= 2 and len(nb) >= 2:
        return (na[0], na[-1], nb[0], nb[-1])
    # No node arrays yet (the domain has never been recomputed with geometry):
    # the inner box is the best available guess.
    if (dom.DomainMax - dom.DomainMin).Length <= 1.0e-9:
        return None
    lo = {"x": dom.DomainMin.x, "y": dom.DomainMin.y, "z": dom.DomainMin.z}
    hi = {"x": dom.DomainMax.x, "y": dom.DomainMax.y, "z": dom.DomainMax.z}
    return (lo[ax_a], hi[ax_a], lo[ax_b], hi[ax_b])


def _section_touches_rect(faces, axis, rect, tol):
    """True if any of *faces* reaches the in-plane rect ``(a0, a1, b0, b1)`` edge.

    Mirrors what ``mode_solver._classify_conductors`` does with
    ``boundary='ground'``: a conductor touching the solve region's edge joins the
    grounded shield and gets **no mode of its own**. Advisory here -- it drives
    the table's "reference" hint, not a refusal.
    """
    if rect is None:
        return False
    ax_a, ax_b = _TRANSVERSE[axis]
    a0, a1, b0, b1 = rect
    for sec in faces:
        bbox = sec.BoundBox
        lo = {"x": bbox.XMin, "y": bbox.YMin, "z": bbox.ZMin}
        hi = {"x": bbox.XMax, "y": bbox.YMax, "z": bbox.ZMax}
        if (lo[ax_a] <= a0 + tol or hi[ax_a] >= a1 - tol
                or lo[ax_b] <= b0 + tol or hi[ax_b] >= b1 - tol):
            return True
    return False


def conductors_on_face(sim, dom, face, bounds_sel=None):
    """Every conductor the port plane cuts, one record per **cross-section region**.

    ``[{"body", "sub", "name", "point", "area", "reference"}, ...]`` -- the
    candidate rows of a port's conductor table. A region, not a body, because
    that is the unit the mode solver works in: it labels connected components of
    metal on the plane, so three solids padded from one sketch into a single
    ``Body`` are three conductors with three modes, and a body-per-row table
    could not name two of them. ``sub`` is a face of the body bounding the
    region, the same thing a 3D-view pick yields.

    ``reference`` flags the regions touching the solve region's edge -- the
    grounded shield, which ``mode_solver._classify_conductors`` gives no mode of
    its own.

    **Signal conductors come first**, largest cross-section leading, with the
    reference ones after them. Row 0 therefore lands on a conductor that actually
    has a mode, which matters because row 0's waveform properties are the ones a
    single-drive port already carried: a legacy port opened in the new panel finds
    its own excitation on a signal conductor rather than on the shield.
    """
    from wavesim_gui import materials as materials_mod

    axis = domain_mod.face_axis(face)
    rect = _solve_region_rect_mm(dom, face, bounds_sel)
    try:
        tol = max(domain_mod.min_spacings_m(dom)) * _MM_PER_M if dom else 0.0
    except Exception:
        tol = 0.0

    out = []
    for body, _name, _volts in materials_mod.conductors(sim):
        for record in _region_records(body, dom, face):
            sub = storage_sub(record)
            out.append({
                "body": body,
                "sub": sub,
                "name": region_name(body, sub, record["count"]),
                "point": record["point"],
                "area": record["area"],
                "reference": _section_touches_rect(
                    [record["shape"]], axis, rect, tol),
            })
    out.sort(key=lambda r: (bool(r["reference"]), -r["area"]))
    return out


def region_name(body, sub, region_count=1):
    """Human name of one conductor region: the body's Label, plus its face.

    A body cutting the plane once is just its Label -- the overwhelmingly common
    case, and what keeps a one-conductor-per-body document's port names, Results
    groups and mode labels exactly what they were. Only a body holding several
    conductors needs the face to tell them apart.
    """
    label = str(getattr(body, "Label", None) or getattr(body, "Name", "?"))
    if region_count <= 1 or not sub:
        return label
    return "{} ({})".format(label, sub)


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
        "port": str(obj.Label or obj.Name),
        "normal": axis,
        "position": position,
        "face": face,
        "conductor_id": int(getattr(obj, "Conductor", 0)),
        "excitation": exc.spec_from_object(obj),
    }
    _add_bounds_spec(spec, dom, face, axis, getattr(obj, "BoundsSel", None), origin_m)
    return spec


def modal_port_specs(obj, origin_m):
    """Every ``job.json`` ``modal_ports`` entry for *obj* -- **one per drive row**.

    A port whose conductor table is empty yields the single legacy entry
    :func:`modal_port_spec` builds, unchanged. A port with a table yields one
    entry per row, all sharing the plane (``normal``/``position``/``face``/
    ``bounds``) and differing in:

    * ``name`` -- ``"<port> (<conductor>)"``, so each row's V(t)/I(t) series and
      Results group name it. ``port`` carries the owning object's label on every
      entry, which is what groups them back into one plane.
    * ``conductor_point`` -- ``[a, b]`` in solver metres, transverse slice order:
      a point inside that row's conductor, where the runner reads φ to find the
      row's mode (see the conductor-table notes above).
    * ``excitation`` -- the row's own waveform. An **unenergized** row still emits
      one, with ``amplitude`` forced to 0: it is then a pure absorber, which is
      the whole point of listing it (its mode would otherwise reflect off a face
      that carries no PML).

    Rows whose conductor no longer reaches the port plane are dropped with a
    console warning rather than emitted pointing nowhere -- the runner would fall
    back to the dominant mode and drive the wrong conductor silently.
    """
    base = modal_port_spec(obj, origin_m)
    rows = drive_rows(obj)
    if not rows:
        return [base]

    sim = active_simulation(obj.Document)
    dom = domain_mod.find_domain(sim) if sim else None
    face = str(obj.Face)
    axis = domain_mod.face_axis(face)
    ax_a, ax_b = _TRANSVERSE[axis]
    ia, ib = _AXIS_IDX[ax_a], _AXIS_IDX[ax_b]

    specs = []
    for body, sub, energized, index in rows:
        point = conductor_plane_point_mm(body, dom, face, sub)
        label = region_name(body, sub, 2 if sub else 1)
        if point is None:
            FreeCAD.Console.PrintWarning(
                "Wavesim: modal port '{}' lists conductor '{}', which does not "
                "reach the {} plane; skipping that drive.\n".format(
                    base["name"], label, face)
            )
            continue
        spec = dict(base)
        # An em dash, not parentheses: a conductor of a multi-conductor body is
        # already named "Body (Face13)", and nesting that reads as noise.
        spec["name"] = "{} — {}".format(base["name"], label)
        spec["conductor"] = label
        # The point is the authority; leave the legacy label at 0 so a point
        # matching no solved mode falls back to the dominant one rather than to
        # a number that was chosen for a different conductor.
        spec["conductor_id"] = 0
        spec["conductor_point"] = [
            point[0] / _MM_PER_M - origin_m[ia],
            point[1] / _MM_PER_M - origin_m[ib],
        ]
        excitation = exc.spec_from_object(obj, index)
        if not energized:
            excitation = dict(excitation, amplitude=0.0)
        spec["excitation"] = excitation
        specs.append(spec)
    return specs or [base]


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
    no frequency); SPICE mode names the linked netlist file instead. A port
    driving several conductors names the count instead of one row's waveform --
    there is no single waveform to name.
    """
    face = str(getattr(obj, "Face", "z0"))
    if excitation_mode(obj) == MODE_SPICE:
        from wavesim_gui import spice_port as spice_mod
        return "{}, {}".format(face, spice_mod._netlist_name(obj))
    doc = getattr(obj, "Document", None)
    sim = active_simulation(doc) if doc is not None else None
    rows = drive_rows(obj)
    driven = [row for row in rows if row[2]]
    if len(driven) > 1:
        return "{}, {} conductors driven".format(face, len(driven))
    if len(driven) == 1:
        body, sub, _energized, index = driven[0]
        return "{}, {}, {}".format(
            face, region_name(body, sub, 2 if sub else 1),
            exc.excitation_label(obj, sim, index))
    if rows:
        return "{}, terminating {} conductors".format(face, len(rows))
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
        """Coin view provider drawing the port as a translucent amber plane."""

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

    class TaskModalPortPanel:
        """Task panel to edit a modal port: face, conductor table and drive.

        The **Drive** combo picks how the port is excited — a temporal waveform
        (the conductor table, driving the modal impedance sheets) or an external
        SPICE circuit (netlist + port nodes) — showing one control group at a
        time. "Inject fields" belongs to the SPICE drive only and rides in its
        container: a modal sheet is inherently one-way and has no such choice.

        **The conductor table** is the waveform drive. One row per conductor the
        port terminates, listing which to energize, with what waveform and at
        what amplitude; the launch is their superposition,
        ``Σ a_i·f_i(t)·mode_i``. Rows are filled in from the geometry (every PEC
        body whose cross-section the port plane cuts, the grounded shield
        excluded — it carries no mode of its own) and can be added from a 3D-view
        selection. Selecting a row expands that row's remaining waveform
        parameters below the table; family and amplitude stay in the row itself.
        A port on a plane with no PEC geometry yet keeps a single "whole face"
        row driving the dominant mode, which is what such a port always did.

        "Compute Mode" solves and visualises *this* port's mode now (out of
        process, no FDTD, nothing saved); OK commits the port and leaves the mode
        for the main Run. Cancel removes a freshly-created port so it leaves no
        trace.
        """

        # Conductor-table columns.
        _COL_CONDUCTOR, _COL_DRIVE, _COL_WAVEFORM, _COL_AMPLITUDE = range(4)

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

            # Legacy fallback: which solved mode to launch, by the solver's own
            # conductor label. Only reachable for a port whose plane carries no
            # PEC geometry to build a table from -- with a table the conductor is
            # picked by identity, not by a number assigned after the fact.
            self._conductor = QtWidgets.QSpinBox()
            self._conductor.setRange(0, 999)
            self._conductor.setSpecialValueText("Dominant (first mode)")
            self._conductor.setValue(int(getattr(obj, "Conductor", 0)))

            layout.addRow("Port face:", self._face)

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

            # Waveform container: the conductor table (one drive per row) plus
            # the selected row's remaining waveform parameters.
            self._wave_widget = QtWidgets.QWidget()
            wave_form = QtWidgets.QFormLayout(self._wave_widget)
            wave_form.setContentsMargins(0, 0, 0, 0)
            self._build_conductor_ui(wave_form, QtWidgets)
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
                "With several conductors on the face, the table lists one row per "
                "conductor the port terminates: tick 'Drive' and give each its own "
                "waveform and amplitude, and the port launches their superposition "
                "(a₁·f₁(t)·mode₁ + a₂·f₂(t)·mode₂ + …) as one impedance sheet per "
                "mode summed on the face. Each driven row records its own V(t)/I(t), "
                "so an S-matrix follows. Rows are matched to modes by a point inside "
                "each conductor's cross-section, so which conductor a row drives is "
                "fixed by the geometry and does not depend on running a mode solve "
                "first. Leave a row unticked to terminate its mode without "
                "launching it — do list it, because a mode with no sheet has no "
                "absorber on this face and reflects. A row is one **cross-section "
                "region**, not one body: a single Part object padded from one "
                "sketch can hold a shield and two pins, which are three separate "
                "conductors here. The table fills itself in from the geometry; to "
                "name a particular one, select a face of it that the port plane "
                "crosses and press 'Add selected face'. "
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
            # The table is per plane: a different face cuts different conductors.
            self._refresh_conductors()

        # ------------------------------------------------------------------ #
        # Conductor table
        # ------------------------------------------------------------------ #

        def _build_conductor_ui(self, layout, QtWidgets):
            """Build the conductor table, its buttons and the row-detail area."""
            self._QtWidgets = QtWidgets
            # One entry per table row: {"body", "energized", "editor", "page",
            # "combo", "amp"}. ``body`` is None for the single "whole face" row a
            # port with no PEC geometry on its plane falls back to.
            self._rows = []

            self._table = QtWidgets.QTableWidget(0, 4)
            self._table.setHorizontalHeaderLabels(
                ["Conductor", "Drive", "Waveform", "Amplitude"])
            self._table.verticalHeader().setVisible(False)
            self._table.setSelectionBehavior(
                QtWidgets.QAbstractItemView.SelectRows)
            self._table.setSelectionMode(
                QtWidgets.QAbstractItemView.SingleSelection)
            self._table.setEditTriggers(
                QtWidgets.QAbstractItemView.NoEditTriggers)
            header = self._table.horizontalHeader()
            header.setStretchLastSection(False)
            header.setSectionResizeMode(
                self._COL_CONDUCTOR, QtWidgets.QHeaderView.Stretch)
            for col in (self._COL_DRIVE, self._COL_WAVEFORM,
                        self._COL_AMPLITUDE):
                header.setSectionResizeMode(
                    col, QtWidgets.QHeaderView.ResizeToContents)
            self._table.setMinimumHeight(120)
            layout.addRow(self._table)

            buttons = QtWidgets.QWidget()
            blay = QtWidgets.QHBoxLayout(buttons)
            blay.setContentsMargins(0, 0, 0, 0)
            self._btn_refresh = QtWidgets.QPushButton("Refresh from geometry")
            self._btn_add = QtWidgets.QPushButton("Add selected face")
            self._btn_add.setToolTip(
                "Add the conductor bounded by the face(s) selected in the 3D "
                "view. Pick a face the port plane crosses — that is how one "
                "conductor of a body holding several (a shield and its pins "
                "padded from one sketch) is named.")
            self._btn_remove = QtWidgets.QPushButton("Remove")
            for btn in (self._btn_refresh, self._btn_add, self._btn_remove):
                blay.addWidget(btn)
            layout.addRow(buttons)
            self._btn_refresh.clicked.connect(self._refresh_conductors)
            self._btn_add.clicked.connect(self._add_from_selection)
            self._btn_remove.clicked.connect(self._remove_row)

            self._table_hint = QtWidgets.QLabel("")
            self._table_hint.setWordWrap(True)
            layout.addRow(self._table_hint)

            # Legacy fallback row, shown only while the table has no conductor.
            self._conductor_row_label = QtWidgets.QLabel("Energize conductor:")
            layout.addRow(self._conductor_row_label, self._conductor)

            # The selected row's remaining waveform parameters (family and
            # amplitude live in the row itself), one page per row.
            self._detail = QtWidgets.QStackedWidget()
            layout.addRow(self._detail)

            self._table.itemSelectionChanged.connect(self._on_row_selected)
            self._load_rows()

        def _stored_rows(self):
            """The rows to open the panel with: the port's table, or a fresh one.

            A port that already carries a table keeps it verbatim -- reopening the
            panel must not silently re-pick conductors. One that does not (a new
            port, or one saved before the table existed) is filled in from the
            geometry, energizing the largest signal conductor and carrying that
            port's existing waveform on it (row 0's properties *are* the ones a
            single-drive port already had). With no PEC geometry on the plane,
            one bodiless "whole face" row, which behaves exactly as the port did
            before this table existed.
            """
            # Scanned either way: even a stored table wants the reference-
            # conductor hint, which only the scan knows.
            found = self._face_conductors()
            rows = drive_rows(self.obj)
            if rows:
                return [{"body": body, "sub": sub, "energized": energized}
                        for body, sub, energized, _index in rows]
            if not found:
                return [{"body": None, "sub": "", "energized": True}]
            return [{"body": r["body"], "sub": r["sub"], "energized": i == 0}
                    for i, r in enumerate(found)]

        def _face_conductors(self):
            """``conductors_on_face`` for the currently selected face, minus the
            reference ones (which carry no mode of their own).

            Sets ``_reference_names`` as a side effect, for the panel's hint.
            """
            sim = active_simulation(self.obj.Document)
            dom = domain_mod.find_domain(sim) if sim else None
            try:
                found = conductors_on_face(
                    sim, dom, self._selected_face(),
                    getattr(self.obj, "BoundsSel", None))
            except Exception as exc_:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: could not section the port plane: {}\n".format(exc_))
                return []
            self._reference_names = [r["name"] for r in found if r["reference"]]
            self._scan = {(id(r["body"]), r["sub"]): r for r in found}
            return [r for r in found if not r["reference"]]

        def _load_rows(self, rows=None):
            """(Re)build the table and the per-row editors from *rows*."""
            from PySide import QtCore

            QtWidgets = self._QtWidgets
            if rows is None:
                self._reference_names = []
                rows = self._stored_rows()

            sim = active_simulation(self.obj.Document)
            self._table.blockSignals(True)
            self._table.setRowCount(0)
            while self._detail.count():
                page = self._detail.widget(0)
                self._detail.removeWidget(page)
                page.deleteLater()
            self._rows = []

            for index, row in enumerate(rows):
                body = row.get("body")
                sub = row.get("sub", "")
                energized = bool(row.get("energized", True))
                self._table.insertRow(index)

                label = (region_name(body, sub, 2 if sub else 1)
                         if body is not None else "Whole face (dominant mode)")
                item = QtWidgets.QTableWidgetItem(label)
                item.setToolTip(self._row_tooltip(body, sub))
                self._table.setItem(index, self._COL_CONDUCTOR, item)

                check = QtWidgets.QCheckBox()
                check.setChecked(energized)
                # A bare checkbox in a cell hugs its left edge; centre it in a
                # holder so the column reads as a column.
                holder = QtWidgets.QWidget()
                hlay = QtWidgets.QHBoxLayout(holder)
                hlay.setContentsMargins(0, 0, 0, 0)
                hlay.setAlignment(QtCore.Qt.AlignCenter)
                hlay.addWidget(check)
                self._table.setCellWidget(index, self._COL_DRIVE, holder)

                combo = QtWidgets.QComboBox()
                combo.addItems(_EXCITATIONS)
                combo.setCurrentText(
                    str(getattr(self.obj, exc.excitation_prop(index),
                                _EXCITATIONS[0]))
                )
                self._table.setCellWidget(index, self._COL_WAVEFORM, combo)

                amp = QtWidgets.QDoubleSpinBox()
                amp.setRange(-1.0e9, 1.0e9)
                amp.setDecimals(4)
                amp.setSingleStep(0.1)
                amp.setValue(float(getattr(
                    self.obj, exc.prop_for_key("amplitude", index),
                    exc.ALL_PARAMS["amplitude"][1])))
                amp.setToolTip(
                    "Launched forward-wave voltage for this conductor's mode. "
                    "The solver calibrates the sheet so this is volts on any "
                    "grid; the port's total launch is the sum over driven rows.")
                self._table.setCellWidget(index, self._COL_AMPLITUDE, amp)

                # This row's remaining waveform parameters, below the table.
                page = QtWidgets.QWidget()
                page_form = QtWidgets.QFormLayout(page)
                page_form.setContentsMargins(0, 0, 0, 0)
                editor = source_mod.ExcitationEditor(
                    self.obj, index, page_form, QtWidgets, sim,
                    combo=combo, exclude={"amplitude"},
                )
                editor.rebuild_params()
                self._detail.addWidget(page)

                entry = {"body": body, "sub": sub, "check": check,
                         "combo": combo, "amp": amp, "editor": editor,
                         "page": page}
                self._rows.append(entry)

                combo.currentTextChanged.connect(
                    lambda _t, e=editor: e.rebuild_params())
                amp.valueChanged.connect(
                    lambda v, e=editor: e.set_param("amplitude", v))
                check.toggled.connect(self._on_drive_toggled)
                editor.set_param("amplitude", amp.value())

            self._table.blockSignals(False)
            if self._rows:
                self._table.selectRow(0)
            self._sync_table_state()

        def _row_tooltip(self, body, sub):
            """What this row's conductor is, in the plane's own terms.

            The cross-section's area and centre, because a body holding several
            conductors names its rows by face number and "Face7" says nothing
            about which pin that is; the in-plane position does.
            """
            if body is None:
                return ("No PEC body was found crossing this port plane, so the "
                        "port drives the solver's dominant mode as before.")
            base = ("The port terminates this conductor's mode. Tick 'Drive' to "
                    "launch it as well.")
            record = getattr(self, "_scan", {}).get((id(body), sub))
            if record is None:
                return base
            ax_a, ax_b = _TRANSVERSE[domain_mod.face_axis(self._selected_face())]
            return ("{}\n\nCross-section {:.4g} mm² at {} = {:.4g} mm, "
                    "{} = {:.4g} mm.".format(
                        base, record["area"], ax_a, record["point"][0],
                        ax_b, record["point"][1]))

        def _on_drive_toggled(self, *_):
            self._sync_table_state()

        def _sync_table_state(self):
            """Enable/disable per-row widgets and refresh the hint text."""
            has_body = any(r["body"] is not None for r in self._rows)
            self._conductor_row_label.setVisible(not has_body)
            self._conductor.setVisible(not has_body)
            self._btn_remove.setEnabled(has_body and len(self._rows) > 1)
            for entry in self._rows:
                driven = entry["check"].isChecked()
                # An undriven row is a pure absorber: it still terminates its
                # mode (which is why it is listed), but its waveform is unused.
                entry["combo"].setEnabled(driven)
                entry["amp"].setEnabled(driven)
                entry["page"].setEnabled(driven)

            bits = []
            driven = [e for e in self._rows if e["check"].isChecked()]
            if len(driven) > 1:
                bits.append(
                    "The port launches the superposition of the {} driven modes "
                    "(one impedance sheet each, summed on the face)."
                    .format(len(driven)))
            elif not driven:
                bits.append(
                    "No row is driven: this port is a pure absorber, "
                    "terminating every listed conductor's mode.")
            undriven = len(self._rows) - len(driven)
            if undriven and has_body:
                bits.append(
                    "{} listed but undriven conductor(s) are still terminated — "
                    "an unlisted mode has no absorber on this face and would "
                    "reflect.".format(undriven))
            refs = getattr(self, "_reference_names", None)
            if refs:
                bits.append(
                    "Reference (ground) on this plane: {} — it touches the solve "
                    "region's edge, so it carries no mode of its own."
                    .format(", ".join(refs)))
            self._table_hint.setText(" ".join(bits))

        def _on_row_selected(self, *_):
            row = self._table.currentRow()
            if 0 <= row < self._detail.count():
                self._detail.setCurrentIndex(row)

        def _refresh_conductors(self, *_):
            """Re-scan the port plane and rebuild the table from the geometry.

            Keeps each surviving conductor's Drive tick, so re-scanning after a
            geometry edit does not silently un-energize the port.
            """
            if not hasattr(self, "_table"):
                return
            driven = {(id(r["body"]), r["sub"]): r["check"].isChecked()
                      for r in self._rows if r["body"] is not None}
            found = self._face_conductors()
            if not found:
                self._load_rows([{"body": None, "sub": "", "energized": True}])
                return
            rows = []
            for i, record in enumerate(found):
                key = (id(record["body"]), record["sub"])
                energized = driven.get(key, i == 0 and not driven)
                rows.append({"body": record["body"], "sub": record["sub"],
                             "energized": energized})
            self._load_rows(rows)

        def _add_from_selection(self, *_):
            """Append a row for each conductor **face** selected in the 3D view.

            The way to name one conductor of a body that holds several: pick a
            face of it that the port plane crosses (a pin's cylindrical wall, a
            trace's side) and the row is the cross-section region that face
            bounds -- not the whole body, whose plane-section may be three
            disjoint conductors. Selecting the body with no face named still
            works and means its largest region.

            Also the escape hatch from the automatic scan: a region it skipped
            (one wrongly read as the grounded reference, or a body only just
            assigned to a PEC material) can be added by hand. Anything not PEC,
            or not reaching the port plane, is refused with a reason rather than
            added as a row that would match no mode.
            """
            from wavesim_gui import materials as materials_mod

            QtWidgets = self._QtWidgets
            sim = active_simulation(self.obj.Document)
            dom = domain_mod.find_domain(sim) if sim else None
            face = self._selected_face()
            pec_bodies = {id(b) for b, _n, _v in materials_mod.conductors(sim)}
            known = {(id(r["body"]), r["sub"])
                     for r in self._rows if r["body"] is not None}
            known_points = {r["point"] for r in
                            (getattr(self, "_scan", {}) or {}).values()
                            if (id(r["body"]), r["sub"]) in known}
            rows = [{"body": r["body"], "sub": r["sub"],
                     "energized": r["check"].isChecked()}
                    for r in self._rows if r["body"] is not None]
            added, not_pec, missed, dup = [], [], [], []
            for sel in Gui.Selection.getSelectionEx():
                body = sel.Object
                if body is None:
                    continue
                label = str(getattr(body, "Label", "?"))
                if id(body) not in pec_bodies:
                    not_pec.append(label)
                    continue
                # One row per picked face; a bare body pick means its largest
                # region, as a single-conductor body always did.
                picks = [n for n in (getattr(sel, "SubElementNames", []) or [])
                         if n.startswith("Face")] or [""]
                for sub in picks:
                    if (id(body), sub) in known:
                        dup.append(conductor_label(body, sub))
                        continue
                    region = _region_on_plane(body, dom, face, sub)
                    if region is None:
                        missed.append(conductor_label(body, sub))
                        continue
                    # Store the face only when the body holds several conductors
                    # -- otherwise the row is just the body, as it always was.
                    keep = storage_sub(region)
                    name = region_name(body, keep, region["count"])
                    # A *different* face of a conductor already listed resolves
                    # to the same region; adding it again would drive that mode
                    # twice on one plane.
                    if (id(body), keep) in known \
                            or region["point"] in known_points:
                        dup.append(name)
                        continue
                    known.add((id(body), sub))
                    known.add((id(body), keep))
                    known_points.add(region["point"])
                    rows.append({"body": body, "sub": keep, "energized": True})
                    added.append(name)
            if not added:
                why = []
                if not_pec:
                    why.append("Not assigned to a PEC material: {}.".format(
                        ", ".join(not_pec)))
                if missed:
                    why.append("Do not reach the {} plane: {}.".format(
                        face, ", ".join(missed)))
                if dup:
                    why.append("Already in the table (the same conductor): "
                               "{}.".format(", ".join(dup)))
                QtWidgets.QMessageBox.information(
                    self.form, "Wavesim Modal Port",
                    "Select a conductor face crossing the port plane in the 3D "
                    "view first.\n\n"
                    + (" ".join(why) if why else "Nothing new was selected."),
                )
                return
            self._load_rows(rows)

        def _remove_row(self, *_):
            """Drop the selected conductor row."""
            row = self._table.currentRow()
            if row < 0 or len(self._rows) <= 1:
                return
            rows = [{"body": r["body"], "sub": r["sub"],
                     "energized": r["check"].isChecked()}
                    for i, r in enumerate(self._rows) if i != row]
            self._load_rows(rows)

        def _write_conductor_table(self, obj):
            """Persist the table (conductors, drive flags, per-row waveforms).

            Call inside an open transaction. A table of bodiless rows clears the
            stored one, which is what puts the port back on the legacy single
            drive that :func:`drive_rows` answers ``[]`` for.
            """
            bodies = [(r["body"], r["sub"])
                      for r in self._rows if r["body"] is not None]
            flags = [r["check"].isChecked()
                     for r in self._rows if r["body"] is not None]
            if bodies:
                set_drive_rows(obj, bodies, flags)
            else:
                set_drive_rows(obj, [], [])
                obj.Conductor = int(self._conductor.value())
            # Each row's waveform. The properties must exist before the editor
            # writes them, and only ``set_drive_rows`` above has added them for
            # the rows that are staying.
            for index, entry in enumerate(self._rows):
                exc.ensure_object_props(obj, index)
                entry["editor"].write(obj)

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
            # The label the object still carries if nobody renamed it -- read
            # with the original properties in place, so labels.retitle can tell
            # an auto label from a name typed in the tree.
            old_auto = "Modal Port ({})".format(_describe(self.obj))
            doc.openTransaction(title)
            face = self._selected_face()
            self.obj.Face = face
            self.obj.BoundsSel = new_bounds
            self.obj.Fields = _FIELDS_TOKEN[self._fields.currentText()]
            self.obj.ExcitationMode = self._selected_mode()
            # Persist both drives' inputs (only the active one is read at run
            # time) so toggling the mode back and forth keeps what was entered.
            # The table owns Conductor too: it writes the legacy label only for a
            # bodiless ("whole face") table, which is the one case it still means
            # anything.
            self._write_conductor_table(self.obj)
            self.obj.Netlist = self._netlist.text().strip()
            self.obj.NodePlus = self._node_plus.text().strip() or "port1p"
            self.obj.NodeMinus = self._node_minus.text().strip() or "0"
            _sync_mode_visibility(self.obj)
            labels_mod.retitle(
                self.obj, old_auto, "Modal Port ({})".format(_describe(self.obj))
            )
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
        solves have nothing to do here. Ports are matched on the ``port`` key
        their ``*_spec`` writes -- the owning object's label, which every drive
        row of a multi-conductor port shares (its ``name`` is per row) and which
        the runner echoes into ``summary["modes"]``. A legacy job entry carrying
        no ``port`` key falls back to its ``name``. Returns ``False`` when
        *port_obj* has no entry in the job at all.

        *arrays* is accepted (and left alone) so the signature survives: the mode
        is solved on the run's own grid, so ``materials.npz`` carries nothing
        port-specific to prune.
        """
        name = str(getattr(port_obj, "Label", "") or getattr(port_obj, "Name", ""))

        def _owns(entry):
            return (entry.get("port") or entry.get("name")) == name

        modal = [t for t in spec.get("modal_ports") or [] if _owns(t)]
        spice = [p for p in spec.get("spice_ports") or []
                 if p.get("kind") == "tem" and _owns(p)]
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
