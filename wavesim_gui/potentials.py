# -*- coding: utf-8 -*-
"""Conductor potentials for an electrostatic run: who is driven, who inherits.

One panel listing **every** PEC body in the document -- across every PEC
material -- with a checkbox saying whether it is held at a potential and the
volts it is held at. It is the one place the whole set is visible, which is what
makes a missing or a contradictory potential obvious; the property editor shows
them one body at a time (``materials.POTENTIAL_PROP`` /
``materials.POTENTIAL_SET_PROP``, which live on the body).

Two conditions, three ways of getting one
-----------------------------------------
A solve can do exactly two things with a lump of metal, and they are easy to
blur:

* **Potential** -- held at the volts it is told, drawing whatever charge that
  takes;
* **Floating** -- held at the charge it is told (0 C, normally: a body that was
  neutral before the field arrived stays neutral), sitting at whatever potential
  that takes. Its voltage is an *output* of the solve, read back from
  ``summary["electrostatic"]["floating_potentials"]``. This is the unconnected
  trace picking up a voltage, the unbiased guard ring, the charged isolated
  body.

A conductor is one lump of metal under one condition, so what a *body* carries
is a request, not a result. Three ways it acquires one:

* **set** -- the user chose Potential or Floating on this body;
* **inherited** -- the body is on Default but touches one that was set, so it is
  part of that same conductor and shares its condition;
* **default** -- on Default and touching nothing that was set, so the document's
  fallback applies (``Simulation.UnsetConductorMode``: Grounded at 0 V, which is
  what an enclosure or a shield normally is, or Floating at 0 C).

The solver already works this way. ``wavesim.electrostatics`` pins potentials
**body by body** over the voxelised grid's connected components: a lump with one
named potential holds it, including any metal fused to it, and a lump with none
is grounded; ``set_floating`` names the other condition and groups the same way.
What used to break was the workbench end -- it emitted a potential
for *every* PEC body, so an unset neighbour arrived as an explicit rival claim
of 0 V on the same lump and the solve was refused ("parts [...] are electrically
the same conductor but were given different potentials"). Sending only the bodies
the user actually set is the whole fix; inheritance then happens where the truth
is, on the grid.

So this module's grouping is a **preview**, not the mechanism. It runs on the
CAD solids (``Shape.distToShape``), before any voxelisation, because the panel
has to colour the viewport while the user types. The grid can fuse a little more
than the CAD does -- two solids clearing each other by less than a cell are one
conductor to the solver -- and that is fine: the extra fusion also inherits.
What still has no answer is one lump asked for two things at once -- two
potentials, two floating charges, or one of each -- and each of those is reported
here (and again before a run) rather than left to surface as the solver's
refusal.

A floating body is named to the solver **only when it was asked for**, exactly
as a driven one is: naming a Default body that touches a floating one would be
the same rival-claim mistake in the other direction. The one place the workbench
does name bodies it was not asked about is a Floating *document default*, which
has to be stated per body because the solver's own default for an unnamed body
is ground.

Colour
------
While the panel is open every PEC body is tinted by the potential it will be
held at, on a blue-white-red diverging scale centred on 0 V when the potentials
straddle zero and running min -> max when they share a sign. A floating body is
not on that scale -- its potential is what is being asked for -- so it gets an
off-scale slate instead, and is left out of the range the bar is built from.
Closing the panel puts back the colour each body had when it opened.

Importing this module registers ``Wavesim_SetPotential`` with ``Gui.addCommand``
when a GUI is available.
"""

import os

import FreeCAD

try:
    import FreeCADGui as Gui
    _GUI_AVAILABLE = True
except ImportError:                                   # headless (freecadcmd)
    Gui = None
    _GUI_AVAILABLE = False

from wavesim_gui import materials as materials_mod
from wavesim_gui.commands import UNSET_FLOAT, UNSET_GROUND


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_ICONS_DIR = os.path.join(_WB_DIR, "Resources", "icons")
_POTENTIAL_ICON = os.path.join(_ICONS_DIR, "potential.svg")

# How close two solids must be, in millimetres, to count as touching for the
# preview. Coincident CAD faces come back from ``distToShape`` at round-off, not
# at exactly zero, so this is a round-off tolerance and nothing more -- it is
# deliberately far below a grid cell. Widening it to "within a cell" would make
# the preview depend on a mesh the panel has not built and cannot show.
TOUCH_TOL_MM = 1.0e-6

# What a conductor's condition is, once inheritance is worked out. Two of them,
# because these are the two things a solve can do with a lump of metal: hold it
# at a potential it is told, or hold it at a charge and find the potential.
COND_POTENTIAL = "potential"
COND_FLOATING = "floating"

# Where that condition came from.
ORIGIN_SET = "set"                # the user asked for it on this body
ORIGIN_INHERITED = "inherited"    # asked for on metal this body touches
ORIGIN_DEFAULT = "default"        # nobody asked; the document's fallback


# --------------------------------------------------------------------------- #
# Touching bodies
# --------------------------------------------------------------------------- #

def _bboxes_touch(a, b, tol):
    """True if the two bounding boxes overlap once grown by *tol*.

    The cheap rejection in front of ``distToShape``, which is the expensive part
    of the pairwise sweep. Written out rather than using ``BoundBox.intersect``
    because that needs a grown *copy* of the box and the enlarge is in-place.
    """
    return not (a.XMax + tol < b.XMin or b.XMax + tol < a.XMin or
                a.YMax + tol < b.YMin or b.YMax + tol < a.YMin or
                a.ZMax + tol < b.ZMin or b.ZMax + tol < a.ZMin)


def bodies_touch(first, second, tol=TOUCH_TOL_MM):
    """True when the two bodies' solids touch (or overlap) within *tol* mm."""
    try:
        shape_a = getattr(first, "Shape", None)
        shape_b = getattr(second, "Shape", None)
        if shape_a is None or shape_b is None:
            return False
        if not _bboxes_touch(shape_a.BoundBox, shape_b.BoundBox, tol):
            return False
        return float(shape_a.distToShape(shape_b)[0]) <= tol
    except Exception:
        # A shape OCC cannot measure must not take the panel down with it; the
        # bodies simply read as separate, which is what they were before this
        # module existed.
        return False


def touching_groups(bodies, tol=TOUCH_TOL_MM):
    """Partition *bodies* into lists of indices that touch, directly or not.

    Union-find over the pairwise sweep, so a chain A-B-C comes back as one group
    even though A and C never meet -- which is what being one conductor means.
    """
    n = len(bodies)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue
            if bodies_touch(bodies[i], bodies[j], tol):
                parent[find(j)] = find(i)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [groups[k] for k in sorted(groups)]


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

def resolve(sim, overrides=None, tol=TOUCH_TOL_MM, groups=None,
            unset_mode=None):
    """What every PEC body in *sim* will be held at, and why.

    Returns ``(entries, groups)``.

    Each entry is a dict: ``body``, ``name`` (the solver-side name), ``mode``
    (what was asked of this body -- one of ``materials.CONDUCTOR_MODES``),
    ``volts`` / ``charge`` (the numbers stored on it), then the resolved
    condition of the conductor it belongs to: ``condition``
    (``potential`` / ``floating``), ``value`` (volts, or ``None`` when floating
    -- the potential is then an output of the solve, not an input),
    ``held_charge`` (coulombs, meaningful when floating), ``origin``
    (``set`` / ``inherited`` / ``default``) and ``group`` (index into *groups*).
    Each group carries its member indices, that same condition, and
    ``conflict`` -- ``None``, or the way this lump was asked for two things at
    once.

    *overrides* is ``{body.Name: (mode, volts, charge)}``, so the panel can
    resolve what the user has typed without writing it to the document first.
    *unset_mode* overrides the document's fallback for the same reason.

    *groups* is a pre-computed :func:`touching_groups` partition over the same
    conductor order, for a caller re-resolving repeatedly: ``distToShape`` over
    real CAD solids is the expensive half of this and the geometry does not
    change while a potential is being typed. It is ignored if it does not cover
    exactly the conductors found now, so a stale one cannot mis-assign a body.
    """
    overrides = overrides or {}
    unset = unset_mode if unset_mode is not None else _document_default(sim)

    entries = []
    for body, name, volts in materials_mod.conductors(sim):
        mode = materials_mod.conductor_mode(body)
        charge = materials_mod.body_charge(body)
        if body.Name in overrides:
            mode, volts, charge = overrides[body.Name]
        entries.append({
            "body": body, "name": name, "mode": mode,
            "volts": float(volts), "charge": float(charge),
            "condition": COND_POTENTIAL, "value": 0.0, "held_charge": 0.0,
            "origin": ORIGIN_DEFAULT, "group": 0,
        })

    partition = (groups if _covers(groups, len(entries))
                 else touching_groups([e["body"] for e in entries], tol))

    groups = []
    for gi, members in enumerate(partition):
        driven = [entries[i] for i in members
                  if entries[i]["mode"] == materials_mod.MODE_POTENTIAL]
        floaters = [entries[i] for i in members
                    if entries[i]["mode"] == materials_mod.MODE_FLOATING]
        group = _resolve_group(driven, floaters, unset)
        group["members"] = list(members)
        for i in members:
            entry = entries[i]
            entry["group"] = gi
            entry["condition"] = group["condition"]
            entry["value"] = group["value"]
            entry["held_charge"] = group["charge"]
            entry["origin"] = (
                ORIGIN_SET if entry["mode"] != materials_mod.MODE_DEFAULT
                else ORIGIN_INHERITED if (driven or floaters)
                else ORIGIN_DEFAULT)
        groups.append(group)
    return entries, groups


def _resolve_group(driven, floaters, unset):
    """The one condition a lump of touching metal ends up under.

    Every way of asking for two conditions at once is a conflict rather than a
    precedence rule: the solver refuses each of them, and picking a winner here
    would only move the surprise from the run to the geometry.
    """
    conflict = None
    if driven and floaters:
        conflict = {
            "kind": "mixed",
            "parts": ([(e["name"], "{:g} V".format(e["volts"]))
                       for e in driven]
                      + [(e["name"], "floating") for e in floaters]),
            "detail": "one held at a potential and the other left to find its "
                      "own. A conductor cannot do both",
        }
    elif len({e["volts"] for e in driven}) > 1:
        conflict = {
            "kind": "potential",
            "parts": [(e["name"], "{:g} V".format(e["volts"])) for e in driven],
            "detail": "held at different potentials. A conductor has one "
                      "potential",
        }
    elif len({e["charge"] for e in floaters}) > 1:
        conflict = {
            "kind": "charge",
            "parts": [(e["name"], _fmt_charge(e["charge"])) for e in floaters],
            "detail": "floating with different charges. They share one surface "
                      "and so one total charge",
        }

    if floaters:
        return {"condition": COND_FLOATING, "value": None,
                "charge": sorted(e["charge"] for e in floaters)[0],
                "conflict": conflict}
    if driven:
        return {"condition": COND_POTENTIAL,
                "value": sorted(e["volts"] for e in driven)[0],
                "charge": 0.0, "conflict": conflict}
    if unset == UNSET_FLOAT:
        return {"condition": COND_FLOATING, "value": None, "charge": 0.0,
                "conflict": None}
    return {"condition": COND_POTENTIAL, "value": 0.0, "charge": 0.0,
            "conflict": None}


def _document_default(sim):
    """The document's fallback for a lump nobody asked anything of."""
    try:
        from wavesim_gui.commands import unset_conductor_mode
        return unset_conductor_mode(sim)
    except Exception:
        return UNSET_GROUND


def _fmt_charge(coulombs):
    """Charge as picocoulombs, the scale an isolated body's charge lives on."""
    return "{:g} pC".format(float(coulombs) * 1.0e12)


def _covers(partition, n):
    """True when *partition* is a partition of exactly ``range(n)``."""
    if not partition:
        return False
    seen = sorted(i for members in partition for i in members)
    return seen == list(range(n))


def conflicts(sim, tol=TOUCH_TOL_MM):
    """One conflict record per contradicted conductor; empty when consistent.

    Checked before a run as well as in the panel: the solver refuses each of
    these outright, and saying so before the voxeliser spends minutes on the
    geometry is worth the pairwise sweep.
    """
    _entries, groups = resolve(sim, tol=tol)
    return [g["conflict"] for g in groups if g["conflict"]]


def describe_conflict(conflict):
    """One sentence naming a contradicted conductor and what to do about it."""
    return (
        "{} touch, so they are one conductor, but they are {}. Give them the "
        "same condition, or leave all but one of them on Default so it "
        "inherits.".format(
            " and ".join("{} ({})".format(name, what)
                         for name, what in conflict["parts"]),
            conflict["detail"]))


def describe(entry):
    """One line saying what *entry* is held at and where that came from."""
    if entry["condition"] == COND_FLOATING:
        charge = entry["held_charge"]
        held = "floating" if not charge else             "floating, {}".format(_fmt_charge(charge))
    else:
        held = "{:g} V".format(entry["value"])
    if entry["origin"] == ORIGIN_SET:
        return held
    if entry["origin"] == ORIGIN_INHERITED:
        return "{} (touching metal)".format(held)
    return "{} (default)".format(held)


# --------------------------------------------------------------------------- #
# Colour scale
# --------------------------------------------------------------------------- #

# Moreland's cool-warm: saturated blue, light grey, saturated red. Diverging
# because a potential has a sign and 0 V is the reference every other value is
# read against -- a sequential ramp puts the ground plane at one end of the bar
# and hides that.
_COLD = (0.230, 0.299, 0.754)
_MID = (0.865, 0.865, 0.865)
_HOT = (0.706, 0.016, 0.150)


# A floating conductor is not on the bar: its potential is what the solve is
# being asked for, so tinting it anywhere on a scale of known volts would be
# inventing the answer. It gets its own off-scale slate instead, dark enough
# that it cannot be read as the light grey at the middle of the bar.
FLOATING_COLOR = (0.42, 0.45, 0.50)


def scale_range(values):
    """``(lo, hi)`` for the colour bar over *values*.

    Symmetric about 0 when the potentials straddle it, so that 0 V lands on the
    neutral middle and a +5/-5 pair reads as mirror images. When they all share
    a sign there is no zero to centre on, so the bar runs min -> max and uses
    its full width.
    """
    vals = [float(v) for v in values]
    if not vals:
        return (-1.0, 1.0)
    lo, hi = min(vals), max(vals)
    if lo < 0.0 < hi:
        half = max(-lo, hi)
        return (-half, half)
    if lo == hi:
        # One potential everywhere: nothing to grade, and a zero-width range
        # would divide by zero. Widen it so the single value sits mid-bar.
        span = abs(lo) or 1.0
        return (lo - span, hi + span)
    return (lo, hi)


def entry_color(entry, lo, hi):
    """The tint for one resolved conductor: off-scale slate when it floats."""
    if entry["condition"] == COND_FLOATING:
        return FLOATING_COLOR
    return color_for(entry["value"], lo, hi)


def color_for(volts, lo, hi):
    """The RGB 3-tuple (floats in 0..1) for *volts* on the ``lo..hi`` bar."""
    if hi <= lo:
        return _MID
    t = (float(volts) - lo) / (hi - lo)
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    if t < 0.5:
        a, b, u = _COLD, _MID, t * 2.0
    else:
        a, b, u = _MID, _HOT, (t - 0.5) * 2.0
    return tuple(a[k] + (b[k] - a[k]) * u for k in range(3))


if _GUI_AVAILABLE:

    from wavesim_gui import commands as commands_mod
    from wavesim_gui.commands import active_simulation, is_electrostatic

    def _qt():
        try:
            from PySide import QtWidgets, QtGui, QtCore
        except ImportError:                            # PySide2/Qt5 spelling
            from PySide import QtGui, QtCore
            QtWidgets = QtGui
        return QtWidgets, QtGui, QtCore

    # ----------------------------------------------------------------- #
    # Viewport preview
    # ----------------------------------------------------------------- #

    class PotentialPreview:
        """Tint PEC bodies by potential, and put their own colours back.

        The colours are captured **as they are when the preview opens**, not
        re-derived from the material afterwards: a body whose tint was changed
        by hand keeps that change, and a body whose material is edited while the
        panel is open still ends up somewhere sane. Nothing here is a document
        change -- view properties are not undoable and are not what the panel
        commits.
        """

        def __init__(self):
            self._saved = {}

        def _save(self, body):
            key = id(body)
            if key in self._saved:
                return
            vobj = getattr(body, "ViewObject", None)
            if vobj is None:
                return
            try:
                self._saved[key] = (vobj, tuple(vobj.ShapeColor))
            except Exception:
                pass

        def apply(self, entries):
            """Colour every body in *entries* by its resolved condition.

            The bar is scaled over the *known* potentials only -- a floating
            body has none yet, so letting it stretch the range would be reading
            a number that does not exist.
            """
            lo, hi = scale_range([e["value"] for e in entries
                                  if e["condition"] == COND_POTENTIAL])
            for entry in entries:
                self._save(entry["body"])
                vobj = getattr(entry["body"], "ViewObject", None)
                if vobj is None:
                    continue
                try:
                    vobj.ShapeColor = entry_color(entry, lo, hi)
                except Exception:
                    pass
            return lo, hi

        def restore(self):
            """Put every tinted body back to the colour it had, then forget."""
            for vobj, rgb in self._saved.values():
                try:
                    vobj.ShapeColor = rgb
                except Exception:
                    pass
            self._saved = {}

    class _ColorBar:
        """A gradient strip with the two end potentials written under it."""

        def __init__(self):
            QtWidgets, QtGui, QtCore = _qt()
            self.widget = QtWidgets.QWidget()
            box = QtWidgets.QVBoxLayout(self.widget)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(2)
            self._bar = QtWidgets.QFrame()
            self._bar.setFixedHeight(14)
            box.addWidget(self._bar)
            row = QtWidgets.QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            self._lo = QtWidgets.QLabel("")
            self._hi = QtWidgets.QLabel("")
            self._hi.setAlignment(QtCore.Qt.AlignRight)
            row.addWidget(self._lo)
            row.addStretch(1)
            # The off-scale entry, spelled out: a floating body is a colour on
            # the model with no place on the bar, which is worth a swatch rather
            # than a footnote.
            swatch = QtWidgets.QLabel("  ")
            swatch.setStyleSheet(
                "background: rgb({},{},{}); border: 1px solid "
                "palette(mid);".format(
                    *(int(c * 255) for c in FLOATING_COLOR)))
            row.addWidget(swatch)
            row.addWidget(QtWidgets.QLabel("floating"))
            row.addStretch(1)
            row.addWidget(self._hi)
            box.addLayout(row)

        def set_range(self, lo, hi):
            stops = []
            for i in range(11):
                t = i / 10.0
                r, g, b = color_for(lo + (hi - lo) * t, lo, hi)
                stops.append("stop:{:.2f} rgb({},{},{})".format(
                    t, int(r * 255), int(g * 255), int(b * 255)))
            self._bar.setStyleSheet(
                "border: 1px solid palette(mid); background: "
                "qlineargradient(x1:0, y1:0, x2:1, y2:0, {});".format(
                    ", ".join(stops)))
            self._lo.setText("{:g} V".format(lo))
            self._hi.setText("{:g} V".format(hi))

    # ----------------------------------------------------------------- #
    # Task panel
    # ----------------------------------------------------------------- #

    _COL_BODY, _COL_COND, _COL_VALUE, _COL_APPLIED = range(4)

    def _suffix(mode):
        """The unit the value column shows for a row under *mode*.

        The column carries whichever number the row's condition reads -- volts
        for a driven body, picocoulombs for a floating one -- because a task
        panel is narrow and a fifth column of permanently-greyed cells says less
        than one that changes its suffix. The "Held at" column spells out which
        of them is in force.

        A function rather than a dict at class scope: this module is imported
        from ``commands``, which ``materials`` imports in turn, so reading a
        ``materials`` attribute while this class body executes can land on a
        half-initialised module depending on who imported what first.
        """
        return {materials_mod.MODE_POTENTIAL: " V",
                materials_mod.MODE_FLOATING: " pC"}.get(mode, "")

    class TaskPotentialPanel:
        """Every PEC body, its potential, and what it resolves to.

        Edits are held in the panel and written on OK, so Cancel is free -- but
        the *preview* is live, because seeing which metal a potential spreads
        onto is the whole reason to look at the 3D view while typing.
        """

        def __init__(self, sim):
            QtWidgets, QtGui, QtCore = _qt()
            self.sim = sim
            self.doc = getattr(sim, "Document", None) or FreeCAD.ActiveDocument
            self._preview = PotentialPreview()
            self._updating = False

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Conductor Potentials")
            layout = QtWidgets.QVBoxLayout(form)

            intro = QtWidgets.QLabel(
                "Hold a conductor at a <b>Potential</b>, or leave it "
                "<b>Floating</b> — an equipotential at a fixed charge, whose "
                "voltage the solve finds. A body on <b>Default</b> takes the "
                "condition of any conductor it touches, and falls back to the "
                "setting below only if it touches none.")
            intro.setWordWrap(True)
            layout.addWidget(intro)

            self.table = QtWidgets.QTableWidget(0, 4)
            self.table.setHorizontalHeaderLabels(
                ["Conductor", "Condition", "Value", "Held at"])
            self.table.verticalHeader().setVisible(False)
            self.table.setSelectionMode(
                QtWidgets.QAbstractItemView.NoSelection)
            header = self.table.horizontalHeader()
            for col in (_COL_BODY, _COL_APPLIED):
                try:
                    header.setSectionResizeMode(col,
                                                QtWidgets.QHeaderView.Stretch)
                except AttributeError:                 # PySide2/Qt5 spelling
                    header.setResizeMode(col, QtWidgets.QHeaderView.Stretch)
            layout.addWidget(self.table)

            # The fallback for a lump nobody asked anything of. Grounded holds
            # it at 0 V and lets whatever charge that takes flow in; floating
            # holds it at no charge and lets the potential go where it will.
            # They are easy to blur and are not the same conductor, which is
            # why both the label and the tooltip say it twice.
            self._default = QtWidgets.QComboBox()
            self._default.addItems(commands_mod.UNSET_LABELS)
            self._default.setCurrentIndex(
                1 if _document_default(sim) == UNSET_FLOAT else 0)
            self._default.setToolTip(
                "Grounded: 0 V, holding whatever charge that takes.\n"
                "Floating: 0 C, sitting at whatever potential that takes.")
            self._default.currentIndexChanged.connect(self._refresh)
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel("Conductors nobody set:"))
            row.addWidget(self._default, 1)
            layout.addLayout(row)

            self._bar = _ColorBar()
            layout.addWidget(self._bar.widget)

            self._warning = QtWidgets.QLabel("")
            self._warning.setWordWrap(True)
            self._warning.setStyleSheet("color: #b03030;")
            self._warning.setVisible(False)
            layout.addWidget(self._warning)

            self._note = QtWidgets.QLabel("")
            self._note.setWordWrap(True)
            layout.addWidget(self._note)

            self.form = form
            self._build_rows()
            # The touching sweep runs once, here: it is the expensive part of a
            # re-resolve and the solids cannot move while the panel is open.
            self._groups = touching_groups([r["body"] for r in self.rows])
            self._refresh()

        # -- rows ------------------------------------------------------ #

        def _build_rows(self):
            """One row per PEC body, in the order the conductors come back."""
            QtWidgets, QtGui, QtCore = _qt()
            self._updating = True
            self.rows = []
            conductors = materials_mod.conductors(self.sim)
            self.table.setRowCount(len(conductors))
            for row, (body, name, volts) in enumerate(conductors):
                mat = materials_mod.conductor_material(self.sim, body)
                item = QtWidgets.QTableWidgetItem(str(name))
                item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                if mat is not None:
                    item.setToolTip("PEC material: {}".format(mat.Label))
                self.table.setItem(row, _COL_BODY, item)

                mode = materials_mod.conductor_mode(body)
                combo = QtWidgets.QComboBox()
                combo.addItems(materials_mod.CONDUCTOR_MODES)
                combo.setCurrentIndex(
                    materials_mod.CONDUCTOR_MODES.index(mode))
                combo.currentIndexChanged.connect(self._refresh)
                self.table.setCellWidget(row, _COL_COND, combo)

                # One spinbox for both numbers: it holds volts on a driven row
                # and picocoulombs on a floating one. Each row remembers the
                # other number, so flipping the condition back and forth does
                # not quietly overwrite what was typed for the other one.
                spin = QtWidgets.QDoubleSpinBox()
                spin.setRange(-1.0e9, 1.0e9)
                spin.setDecimals(4)
                spin.setSingleStep(0.5)
                spin.valueChanged.connect(self._on_value_changed)
                self.table.setCellWidget(row, _COL_VALUE, spin)

                applied = QtWidgets.QTableWidgetItem("")
                applied.setFlags(applied.flags() & ~QtCore.Qt.ItemIsEditable)
                self.table.setItem(row, _COL_APPLIED, applied)

                rec = {"body": body, "name": name, "combo": combo,
                       "spin": spin, "volts": float(volts),
                       "charge": materials_mod.body_charge(body)}
                self.rows.append(rec)
                self._load_value(rec)
            self._updating = False

        def _load_value(self, rec):
            """Put the number the row's current condition reads into its spin."""
            mode = rec["combo"].currentText()
            rec["spin"].setSuffix(_suffix(mode))
            rec["spin"].setEnabled(mode != materials_mod.MODE_DEFAULT)
            rec["spin"].setValue(
                rec["charge"] * 1.0e12 if mode == materials_mod.MODE_FLOATING
                else rec["volts"])

        def _on_value_changed(self, *_args):
            """Bank the edited number against the row's current condition."""
            if self._updating:
                return
            self._harvest()
            self._refresh()

        def _harvest(self):
            """Copy each row's spin back into whichever number it stands for."""
            for rec in self.rows:
                mode = rec["combo"].currentText()
                if mode == materials_mod.MODE_FLOATING:
                    rec["charge"] = rec["spin"].value() * 1.0e-12
                elif mode == materials_mod.MODE_POTENTIAL:
                    rec["volts"] = rec["spin"].value()

        def _overrides(self):
            """``{body.Name: (mode, volts, charge)}`` for what the table says."""
            return {rec["body"].Name: (rec["combo"].currentText(),
                                       rec["volts"], rec["charge"])
                    for rec in self.rows}

        # -- live state ------------------------------------------------ #

        def _refresh(self, *_args):
            """Re-resolve, repaint the applied column, the bar and the view."""
            if self._updating:
                return
            QtWidgets, QtGui, QtCore = _qt()
            self._updating = True
            try:
                for rec in self.rows:
                    self._load_value(rec)
                entries, groups = resolve(
                    self.sim, self._overrides(), groups=self._groups,
                    unset_mode=self._unset_mode())
                by_body = {id(e["body"]): e for e in entries}
                for row, rec in enumerate(self.rows):
                    entry = by_body.get(id(rec["body"]))
                    item = self.table.item(row, _COL_APPLIED)
                    if entry is None or item is None:
                        continue
                    conflict = groups[entry["group"]]["conflict"]
                    if conflict:
                        item.setText("conflict")
                        item.setForeground(QtGui.QColor("#b03030"))
                    else:
                        item.setText(describe(entry))
                        item.setForeground(
                            QtGui.QColor("#202020")
                            if entry["origin"] == ORIGIN_SET
                            else QtGui.QColor("#707070"))
                lo, hi = self._preview.apply(entries)
                self._bar.set_range(lo, hi)
                self._set_warning(groups)
                self._set_note(entries)
            finally:
                self._updating = False

        def _unset_mode(self):
            """The fallback the default combo currently names."""
            return (UNSET_FLOAT if self._default.currentIndex() == 1
                    else UNSET_GROUND)

        def _set_warning(self, groups):
            """Name every conductor asked for two conditions at once."""
            bad = [g["conflict"] for g in groups if g["conflict"]]
            if not bad:
                self._warning.setVisible(False)
                return
            self._warning.setText(" ".join(describe_conflict(c) for c in bad))
            self._warning.setVisible(True)

        def _set_note(self, entries):
            """Count how the conductors got their conditions, and say so."""
            counts = {ORIGIN_SET: 0, ORIGIN_INHERITED: 0, ORIGIN_DEFAULT: 0}
            floating = 0
            for entry in entries:
                counts[entry["origin"]] += 1
                if entry["condition"] == COND_FLOATING:
                    floating += 1
            self._note.setText(
                "{} set, {} inheriting from touching metal, {} on the default. "
                "{} floating (slate — its potential is an output, not a point "
                "on the bar).".format(
                    counts[ORIGIN_SET], counts[ORIGIN_INHERITED],
                    counts[ORIGIN_DEFAULT], floating))

        # -- buttons --------------------------------------------------- #

        def accept(self):
            self._harvest()
            self.doc.openTransaction("Wavesim: Conductor Potentials")
            try:
                for rec in self.rows:
                    # Both numbers go on the body whatever the condition, so a
                    # row switched to Default (or between the other two) comes
                    # back to what was typed rather than to zero.
                    materials_mod.set_conductor_mode(
                        rec["body"], rec["combo"].currentText(),
                        volts=rec["volts"], charge=rec["charge"])
                # Back-filled here as well as on restore, so a document from
                # before the property existed can still be told what its
                # unset conductors do.
                commands_mod._ensure_solver_mode_props(self.sim)
                if hasattr(self.sim, "UnsetConductorMode"):
                    self.sim.UnsetConductorMode =                         commands_mod.UNSET_LABELS[self._default.currentIndex()]
            except Exception:
                self.doc.abortTransaction()
                self._preview.restore()
                raise
            self.doc.commitTransaction()
            self._preview.restore()
            self.doc.recompute()
            Gui.Control.closeDialog()
            return True

        def reject(self):
            self._preview.restore()
            Gui.Control.closeDialog()
            return True

        def getStandardButtons(self):
            QtWidgets, _QtGui, _QtCore = _qt()
            buttons = (QtWidgets.QDialogButtonBox.Ok
                       | QtWidgets.QDialogButtonBox.Cancel)
            # PySide6/Qt6 yields a StandardButton flag (use .value); PySide2/Qt5
            # yields a plain int already.
            return int(getattr(buttons, "value", buttons))

    def open_potential_panel(sim):
        """Open (or replace) the conductor-potential panel for *sim*."""
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskPotentialPanel(sim))

    class CommandSetPotential:
        """List every PEC body and say which of them is driven, and at what."""

        def GetResources(self):
            return {
                "Pixmap": _POTENTIAL_ICON,
                "MenuText": "Set Potential",
                "ToolTip": "Electrostatic mode: hold PEC bodies at potentials. "
                "Bodies touching a driven one inherit it; the rest are "
                "grounded at 0 V",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = active_simulation(doc)
            if sim is None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: create a Simulation before setting potentials.\n")
                return
            if not materials_mod.conductors(sim):
                FreeCAD.Console.PrintWarning(
                    "Wavesim: no PEC bodies to hold a potential. Assign solid "
                    "bodies to a material with PEC ticked.\n")
                return
            open_potential_panel(sim)

        def IsActive(self):
            # Electrostatics only: in a full-wave run a PEC body is a boundary
            # condition with no potential to speak of.
            sim = active_simulation(FreeCAD.ActiveDocument)
            return sim is not None and is_electrostatic(sim)

    Gui.addCommand("Wavesim_SetPotential", CommandSetPotential())
