# -*- coding: utf-8 -*-
"""Point source + excitation for the Wavesim workbench (Session 6).

A *Source* is a scripted FreeCAD DocumentObject grouped under the simulation's
"Sources" child group. It bundles the two halves of a soft point excitation:

* the **source** -- which field component to drive ('Ex'..'Hz') and where (a
  world-coordinate position, snapped to the nearest grid cell at run time);
* the **excitation** -- the temporal waveform, chosen from a family (Gaussian
  pulse, sine wave, rectangular pulse, Gaussian+sine) with per-family parameters.
  The catalogue + maths live in :mod:`wavesim_gui.excitation`; the panel rebuilds
  its parameter widgets to match the selection and can plot a preview. The chosen
  waveform is emitted as an ``excitation`` spec dict in :func:`source_spec`, from
  which the conda-side runner builds the actual solver waveform.

This replaces the Session-2/3 hardcoded centre source: :mod:`wavesim_gui.voxelize`
now reads the first Source under the simulation when building the job.

Rendering
---------
The source draws as a single amber point marker in the 3D view at its world
position. Like every scripted ViewProvider it carries the standard ``Visibility``
property, so the tree's "eye" toggle shows/hides it. Double-clicking the source
in the tree (or Edit) opens a Task-tab panel to change its component, position
and excitation parameters.

Units: FreeCAD geometry/properties are in millimetres; the solver works in
metres. ``Position`` is stored in mm; :func:`source_spec` converts to metres and
into the solver frame (measured from the domain origin) for the runner.

Importing this module registers ``Wavesim_AddSource`` with ``Gui.addCommand``
when a GUI is available.
"""

import os

import FreeCAD

from wavesim_gui.commands import active_simulation
from wavesim_gui import expressions, labels as labels_mod, units
from wavesim_gui import excitation as exc


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_RESOURCES_DIR = os.path.join(_WB_DIR, "Resources")
# The 24x24 SVG icon set (grouped by colour: blue setup, amber sources,
# teal monitors). The retired PNGs are still in Resources/ alongside it.
_ICONS_DIR = os.path.join(_RESOURCES_DIR, "icons")
_POINT_SOURCE_ICON = os.path.join(_ICONS_DIR, "source_point.svg")
# Tree/view-provider icon for a point source (the toolbar command uses the same).
_SOURCE_ICON = _POINT_SOURCE_ICON

# Marker property, mirroring the other entities' identity scheme so the object is
# recognisable before its Python proxy is re-attached on reload.
_TYPE_PROP = "WavesimType"
_SOURCE_TYPE = "Source"

# Name of the child group (created by CommandNewSimulation) holding sources.
_SOURCES_GROUP = "Sources"

# Field components a point source may drive.
_COMPONENTS = ["Ex", "Ey", "Ez", "Hx", "Hy", "Hz"]

# Available excitation waveforms (labels, in panel order) + the object<->spec
# glue live in the shared workbench-side catalogue :mod:`wavesim_gui.excitation`.
_EXCITATIONS = exc.EXCITATION_LABELS

# Amber point marker, matching the source/port group in Resources/icons
# (#e0912f) -- the toolbar and the 3D view name a thing the same way.
_SOURCE_COLOR = (0.878, 0.569, 0.184)

_MM_PER_M = 1000.0


# --------------------------------------------------------------------------- #
# Document-object model
# --------------------------------------------------------------------------- #

class SourceObject:
    """``Proxy`` for a point-source document object.

    Properties:
        ``Component``  -- field component driven ('Ex'..'Hz').
        ``Position``   -- injection point, world coordinates (mm).
        ``Excitation`` -- temporal waveform family (Gaussian Pulse, Sine Wave,
                          Rectangular Pulse, Gaussian + Sine).
        ``Amplitude``  -- peak amplitude of the waveform (all families).
        ``Fmax``       -- Gaussian bandwidth (and Gaussian+Sine envelope BW), Hz.
        ``Frequency``  -- carrier frequency of the sine / Gaussian+Sine, Hz.
        ``PhaseDeg``   -- carrier phase offset of the sine / Gaussian+Sine, deg.
        ``StartTime``/``RiseTime``/``FlatTime``/``FallTime`` -- rectangular-pulse
                          trapezoid timings, seconds.

    Frequencies/times are stored in SI (Hz / s) and edited via the source panel
    in the simulation's display units; the parameters used by the active
    Excitation are read-only in the property editor and the rest are hidden.
    """

    def __init__(self, obj):
        self.Type = _SOURCE_TYPE
        obj.Proxy = self

        if not hasattr(obj, _TYPE_PROP):
            obj.addProperty(
                "App::PropertyString", _TYPE_PROP, "Wavesim",
                "Marks this object as a Wavesim source",
            )
            setattr(obj, _TYPE_PROP, _SOURCE_TYPE)
            obj.setEditorMode(_TYPE_PROP, 1)  # read-only identity marker

        if not hasattr(obj, "Component"):
            obj.addProperty(
                "App::PropertyEnumeration", "Component", "Source",
                "Field component the point source drives",
            )
            obj.Component = _COMPONENTS
            obj.Component = "Ez"
        if not hasattr(obj, "Position"):
            obj.addProperty(
                "App::PropertyVector", "Position", "Source",
                "Injection point in world coordinates (mm), snapped to the "
                "nearest grid cell",
            )
        exc.ensure_object_props(obj)

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _SOURCE_TYPE)
        # Re-run property setup so sources saved before the extra waveforms were
        # added gain the new Excitation options + parameter properties, and so
        # the runtime-only editor modes are re-asserted after reload.
        exc.ensure_object_props(obj)

    def execute(self, obj):
        # Pure data object; the ViewProvider draws the marker from Position.
        pass

    def dumps(self):
        return {"Type": getattr(self, "Type", _SOURCE_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _SOURCE_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


# --------------------------------------------------------------------------- #
# Lookup helpers
# --------------------------------------------------------------------------- #

def is_source(obj):
    """Return True if *obj* is a Wavesim Source object."""
    return getattr(obj, _TYPE_PROP, None) == _SOURCE_TYPE


def sources_group(sim):
    """Return the "Sources" child group of the Simulation container *sim*.

    Falls back to *sim* itself if the expected child group is missing (e.g. an
    older document), so a source is never left ungrouped.
    """
    if sim is None:
        return None
    for child in sim.Group:
        if child.Name == _SOURCES_GROUP or child.Label == _SOURCES_GROUP:
            return child
    return sim


def find_sources(sim):
    """Return all Source objects under the Simulation container *sim*."""
    grp = sources_group(sim)
    if grp is None:
        return []
    return [obj for obj in grp.Group if is_source(obj)]


def source_spec(source, origin_m):
    """Return the ``job.json`` source dict for *source* in the solver frame.

    *origin_m* is the domain min corner in FreeCAD world metres (from the
    voxeliser). The stored ``Position`` is world mm; the solver frame measures
    from the domain origin, so we subtract it after converting to metres. The
    temporal waveform is carried in the ``excitation`` sub-dict (see
    :mod:`wavesim_gui.excitation`); the runner builds the solver waveform from it.
    """
    pos = source.Position
    x = pos.x / _MM_PER_M - origin_m[0]
    y = pos.y / _MM_PER_M - origin_m[1]
    z = pos.z / _MM_PER_M - origin_m[2]
    return {
        "component": str(source.Component),
        "x": x, "y": y, "z": z,
        "excitation": exc.spec_from_object(source),
    }


def _describe(obj):
    """Short human label for a source, e.g. ``Ez, Gaussian Pulse @ 30 GHz``.

    Appends the characteristic frequency (in the simulation's unit) for the
    waveforms that have one; the rectangular pulse has none.
    """
    doc = getattr(obj, "Document", None)
    sim = active_simulation(doc) if doc is not None else None
    return "{}, {}".format(getattr(obj, "Component", "Ez"),
                           exc.excitation_label(obj, sim))


def default_position_mm(sim):
    """A sensible default source position (world mm): the domain/geometry centre.

    Uses the Domain box midpoint when it has been sized, else the material
    bounding-box centre, else the origin.
    """
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import materials as materials_mod
    from wavesim_gui import voxelize as vox

    dom = domain_mod.find_domain(sim)
    if dom is not None:
        mn, mx = getattr(dom, "DomainMin", None), getattr(dom, "DomainMax", None)
        if mn is not None and mx is not None and (mx - mn).Length > 1.0e-9:
            return FreeCAD.Vector((mn.x + mx.x) / 2.0,
                                  (mn.y + mx.y) / 2.0,
                                  (mn.z + mx.z) / 2.0)

    bbox = vox.materials_bbox_mm(materials_mod.find_materials(sim)) if sim else None
    if bbox is not None:
        return FreeCAD.Vector(bbox.Center.x, bbox.Center.y, bbox.Center.z)
    return FreeCAD.Vector(0, 0, 0)


# --------------------------------------------------------------------------- #
# GUI: view provider, task panel, command
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

    # ------------------------------------------------------------------ #
    # Excitation preview plot (FreeCAD-side matplotlib, mirroring results.py)
    # ------------------------------------------------------------------ #

    # Keep preview windows alive: a QDialog with no Python reference is garbage
    # collected and vanishes immediately.
    _PLOT_WINDOWS = []

    def _qt():
        try:
            from PySide import QtCore, QtWidgets
        except ImportError:
            from PySide import QtCore
            from PySide import QtGui as QtWidgets
        return QtCore, QtWidgets

    def _mpl():
        """Import matplotlib's Qt6 backend; raises on failure."""
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

    def _show_excitation_plot(spec, t_max_s, time_unit):
        """Open a non-modal window plotting excitation *spec* over ``[0, t_max]``.

        *t_max_s* is the simulation window (``MaxTime``); when unset a sensible
        span for the waveform is used. The x-axis is drawn in *time_unit*.
        """
        import numpy as np

        QtCore, QtWidgets = _qt()
        try:
            FigureCanvas, NavToolbar, Figure = _mpl()
        except Exception as exc_err:
            QtWidgets.QMessageBox.critical(
                Gui.getMainWindow(), "Wavesim Source",
                "Could not load matplotlib for plotting:\n{}".format(exc_err),
            )
            return

        if t_max_s <= 0.0:
            t_max_s = exc.suggested_tmax(spec)
        t = np.linspace(0.0, t_max_s, 2000)
        y = exc.evaluate(spec, t)
        scale = units.time_from_si(1.0, time_unit)  # SI seconds -> display unit

        dialog = QtWidgets.QDialog(Gui.getMainWindow())
        dialog.setWindowTitle("Excitation preview")
        dialog.setWindowFlags(QtCore.Qt.Window)
        dialog.resize(640, 480)
        dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        layout = QtWidgets.QVBoxLayout(dialog)

        figure = Figure(figsize=(6, 4.5), tight_layout=True)
        canvas = FigureCanvas(figure)
        layout.addWidget(NavToolbar(canvas, dialog))
        layout.addWidget(canvas)

        ax = figure.add_subplot(111)
        ax.plot(t * scale, y)
        ax.set_xlabel("time ({})".format(time_unit))
        ax.set_ylabel("amplitude")
        ax.set_title(exc.label_for_type(spec.get("type", exc.GAUSSIAN)))
        ax.grid(True, alpha=0.3)
        canvas.draw_idle()

        dialog._figure = figure  # keep refs on the dialog
        dialog._canvas = canvas

        def _cleanup(_result, d=dialog):
            try:
                d._figure.clear()
            except Exception:
                pass
            try:
                _PLOT_WINDOWS.remove(d)
            except ValueError:
                pass

        dialog.finished.connect(_cleanup)
        _PLOT_WINDOWS[:] = [w for w in _PLOT_WINDOWS if _plot_win_visible(w)]
        _PLOT_WINDOWS.append(dialog)
        dialog.show()

    def _plot_win_visible(win):
        try:
            return win.isVisible()
        except RuntimeError:  # underlying C++ object already deleted
            return False

    class ExcitationEditor:
        """One excitation's combo + dynamic parameter widgets + preview plot.

        Owns the widgets for a **single** waveform: the family combo, a parameter
        block rebuilt from :data:`excitation.PARAMS` whenever the family changes,
        and (optionally) a preview-plot button. It is a plain object rather than a
        task-panel mixin because a multi-conductor modal port needs *several* of
        them at once -- one per drive row, each bound to its own suffixed property
        set (``index``; see :func:`excitation.prop_for_key`) -- and panel-level
        ``self.`` state cannot hold more than one.

        *layout* is the ``QFormLayout`` the widgets are appended to. The combo's
        change signal is left **unconnected**: the owner wires it, so a subclass
        overriding the panel's rebuild hook still sees it (see
        :class:`ExcitationParamsMixin`), and calls :meth:`rebuild_params` once.

        Two options let a host lay the same excitation out across two places --
        the modal port's conductor table puts the family and the amplitude in the
        table row itself and only the remaining parameters below it:

        * *combo* -- an externally owned family combo to read instead of building
          one (no "Excitation:" row is added).
        * *exclude* -- parameter keys to build no row for. Their cached SI values
          survive :meth:`save_param_values` untouched, so whoever owns the widget
          outside writes them through :meth:`set_param` and the spec still
          carries them.
        """

        def __init__(self, obj, index, layout, QtWidgets, sim, with_plot=True,
                     combo=None, exclude=()):
            self.obj = obj
            self.index = int(index)
            self._QtWidgets = QtWidgets
            self._freq_unit = units.get_frequency_unit(sim)
            self._time_unit = units.get_time_unit(sim)
            self._exclude = set(exclude)
            # SI value cache for every parameter, seeded from the object so
            # switching waveforms mid-edit keeps previously entered values.
            self._values = {
                key: float(getattr(obj, exc.prop_for_key(key, self.index),
                                   exc.ALL_PARAMS[key][1]))
                for key in exc.PROP_FOR_KEY
            }

            if combo is not None:
                self.combo = combo
            else:
                self.combo = QtWidgets.QComboBox()
                self.combo.addItems(_EXCITATIONS)
                self.combo.setCurrentText(
                    str(getattr(obj, exc.excitation_prop(self.index),
                                _EXCITATIONS[0]))
                )
                layout.addRow("Excitation:", self.combo)

            # Container whose rows are rebuilt to match the selected waveform's
            # parameter set (see :data:`excitation.PARAMS`).
            self.params_widget = QtWidgets.QWidget()
            self._params_form = QtWidgets.QFormLayout(self.params_widget)
            self._params_form.setContentsMargins(0, 0, 0, 0)
            self.param_spins = {}  # key -> (spin, to_si_callable)
            layout.addRow(self.params_widget)

            self.plot_button = None
            if with_plot:
                self.plot_button = QtWidgets.QPushButton(
                    "Plot excitation vs. time…")
                layout.addRow(self.plot_button)
                self.plot_button.clicked.connect(self.plot)

        @property
        def time_unit(self):
            return self._time_unit

        def current_type(self):
            return exc.type_for_label(self.combo.currentText())

        def _make_param_spin(self, kind, si_value):
            """Return ``(spin, to_si)`` for a parameter of the given *kind*.

            The spin box shows the value in the simulation's display unit (for
            frequency/time) or raw (amplitude/phase); ``to_si`` converts its
            current value back to the SI base stored on the object.
            """
            spin = self._QtWidgets.QDoubleSpinBox()
            if kind == exc.KIND_FREQ:
                spin.setRange(1.0e-9, 1.0e15)
                spin.setDecimals(6)
                spin.setSuffix(" " + self._freq_unit)
                spin.setSingleStep(1.0)
                spin.setValue(units.freq_from_si(si_value, self._freq_unit))
                to_si = lambda v: units.freq_to_si(v, self._freq_unit)
            elif kind == exc.KIND_TIME:
                spin.setRange(0.0, 1.0e12)
                spin.setDecimals(6)
                spin.setSuffix(" " + self._time_unit)
                spin.setSingleStep(0.1)
                spin.setValue(units.time_from_si(si_value, self._time_unit))
                to_si = lambda v: units.time_to_si(v, self._time_unit)
            elif kind == exc.KIND_PHASE:
                spin.setRange(-360.0, 360.0)
                spin.setDecimals(2)
                spin.setSuffix(" deg")
                spin.setSingleStep(5.0)
                spin.setValue(si_value)
                to_si = lambda v: v
            elif kind == exc.KIND_COUNT:
                # A dimensionless count (e.g. Sinusoid ramp-up cycles): raw,
                # non-negative, shown in the "cycles" unit.
                spin.setRange(0.0, 1.0e6)
                spin.setDecimals(2)
                spin.setSuffix(" cycles")
                spin.setSingleStep(1.0)
                spin.setValue(si_value)
                to_si = lambda v: v
            else:  # KIND_AMP
                spin.setRange(-1.0e9, 1.0e9)
                spin.setDecimals(4)
                spin.setSingleStep(0.1)
                spin.setValue(si_value)
                to_si = lambda v: v
            return spin, to_si

        def save_param_values(self):
            """Persist the currently shown spins into the SI value cache."""
            for key, (spin, to_si) in self.param_spins.items():
                self._values[key] = float(to_si(spin.value()))

        def rebuild_params(self, *_):
            """Rebuild the parameter rows to match the selected waveform."""
            self.save_param_values()
            while self._params_form.rowCount():
                self._params_form.removeRow(0)
            self.param_spins = {}
            for key, label, kind, _default in exc.PARAMS[self.current_type()]:
                if key in self._exclude:
                    continue
                si_value = self._values.get(key, exc.ALL_PARAMS[key][1])
                spin, to_si = self._make_param_spin(kind, si_value)
                # A parameter bound to an expression in the property editor is
                # re-evaluated on every recompute, so this row cannot own it.
                expressions.lock_bound_widget(
                    spin, self.obj, exc.prop_for_key(key, self.index)
                )
                self._params_form.addRow(label + ":", spin)
                self.param_spins[key] = (spin, to_si)

        def spec_from_widgets(self):
            """Return the excitation spec (SI) for the current widget state."""
            self.save_param_values()
            typ = self.current_type()
            spec = {"type": typ}
            for key in exc.param_keys(typ):
                spec[key] = float(self._values.get(key, exc.ALL_PARAMS[key][1]))
            return spec

        def get_param(self, key):
            """The cached SI value of parameter *key* (shown or excluded)."""
            self.save_param_values()
            return float(self._values.get(key, exc.ALL_PARAMS[key][1]))

        def set_param(self, key, value):
            """Cache *key*'s **SI** value, for a parameter this editor excludes.

            The way a parameter whose widget the host owns elsewhere -- the
            amplitude column of the modal port's conductor table -- gets into the
            spec. Cache-only by design: an included parameter's spin shows a
            *display*-unit value (GHz, ns), and writing an SI number into it
            would be off by the unit scale.
            """
            self._values[key] = float(value)

        def uses_param(self, key):
            """True if the currently selected waveform family uses *key*."""
            return key in exc.param_keys(self.current_type())

        def plot(self, *_):
            """Open a preview window plotting the excitation vs. time."""
            sim = active_simulation(self.obj.Document)
            t_max_s = float(getattr(sim, "MaxTime", 0.0)) if sim else 0.0
            _show_excitation_plot(
                self.spec_from_widgets(), t_max_s, self._time_unit
            )

        def write(self, obj):
            """Persist this row's Excitation + every parameter onto *obj*.

            Writes all waveforms' parameters (not just the active one), so values
            entered under other waveforms are preserved -- except any the user
            has bound to an expression, which owns its own value and would
            overwrite this one at the next recompute anyway. Call inside an open
            transaction; refreshes the property-editor visibility afterwards.
            """
            self.save_param_values()
            setattr(obj, exc.excitation_prop(self.index),
                    self.combo.currentText())
            for key in exc.PROP_FOR_KEY:
                prop = exc.prop_for_key(key, self.index)
                if key in self._values and hasattr(obj, prop) \
                        and not expressions.is_bound(obj, prop):
                    setattr(obj, prop, float(self._values[key]))
            exc.sync_visibility(obj, self.index)

    class ExcitationParamsMixin:
        """Shared excitation combo + dynamic parameter widgets + preview plot.

        Reused by the point-source, modal-port and Gaussian-beam task panels for
        the object's **single/first** excitation (property index 0). The host
        panel must set ``self.obj`` first, then call ``build_excitation_ui`` while
        constructing its form, and call ``write_excitation`` inside its own open
        transaction to persist the values.

        A thin shim over :class:`ExcitationEditor`, which owns the widgets. The
        combo is wired to ``self._rebuild_params`` rather than to the editor's own
        method so a panel overriding it (the Gaussian beam re-hooks the fresh
        spins into its waist hint) still runs on every waveform change.
        """

        def build_excitation_ui(self, layout, QtWidgets):
            self._editor = ExcitationEditor(
                self.obj, 0, layout, QtWidgets,
                active_simulation(self.obj.Document),
            )
            self._rebuild_params()
            self._editor.combo.currentTextChanged.connect(self._rebuild_params)

        # The editor's widget state, exposed under the names panels already use.
        @property
        def _param_spins(self):
            return self._editor.param_spins

        @property
        def _excitation(self):
            return self._editor.combo

        @property
        def _time_unit(self):
            return self._editor.time_unit

        def _rebuild_params(self, *args):
            self._editor.rebuild_params(*args)

        def _spec_from_widgets(self):
            return self._editor.spec_from_widgets()

        def _plot(self, *_):
            self._editor.plot()

        def write_excitation(self, obj):
            self._editor.write(obj)

    class SourceViewProvider:
        """Coin view provider drawing the source as an amber point marker.

        The standard ``Visibility`` property (tree "eye") shows/hides the marker.
        Double-clicking (or Edit) opens the source task panel.
        """

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            from pivy import coin

            self.Object = vobj.Object
            root = coin.SoSeparator()

            color = coin.SoBaseColor()
            color.rgb.setValue(*_SOURCE_COLOR)
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
            return _SOURCE_ICON

        def setEdit(self, vobj, mode=0):
            _open_source_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_source_panel(vobj.Object)
            return True

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class TaskSourcePanel(ExcitationParamsMixin):
        """Task-tab panel to edit a source's component, position and excitation.

        ``accept`` writes the widget values back onto the object; ``reject``
        removes the object when it was created fresh for this edit (so a
        cancelled "Add Source" leaves no trace).
        """

        def __init__(self, obj, created=False):
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets

            self.obj = obj
            self.created = created
            # Original position, restored on Cancel and used so Accept records the
            # full change for undo (live edits below modify the object directly).
            self._orig_position = FreeCAD.Vector(obj.Position)

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Source")
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

            layout.addRow("Field component:", self._component)
            layout.addRow("Position X:", self._x)
            layout.addRow("Position Y:", self._y)
            layout.addRow("Position Z:", self._z)

            # Excitation combo + per-waveform parameter rows + preview button.
            self.build_excitation_ui(layout, QtWidgets)

            info = QtWidgets.QLabel(
                "The source softly injects the chosen field component at the "
                "given point (snapped to the nearest grid cell). Pick a temporal "
                "waveform and its parameters; use the plot button to preview it. "
                "Frequency/time units are set on the Simulation object."
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
            """Move the source marker immediately as the spin boxes change."""
            self.obj.Position = FreeCAD.Vector(
                self._x.value(), self._y.value(), self._z.value()
            )

        def accept(self):
            from wavesim_gui import domain as domain_mod

            doc = self.obj.Document
            # Restore the original position first so the transaction captures the
            # full change (live edits already moved the object outside it).
            self.obj.Position = self._orig_position
            # The label the object still carries if nobody renamed it -- read
            # before the edits land, so labels.retitle can tell an auto label
            # from a name the user typed in the tree. See wavesim_gui/labels.py.
            old_auto = "Source ({})".format(_describe(self.obj))
            doc.openTransaction("Wavesim: Edit Source")
            self.obj.Component = self._component.currentText()
            self.obj.Position = FreeCAD.Vector(
                self._x.value(), self._y.value(), self._z.value()
            )
            self.write_excitation(self.obj)
            labels_mod.retitle(
                self.obj, old_auto, "Source ({})".format(_describe(self.obj))
            )
            doc.commitTransaction()
            doc.recompute()
            # Enlarge the domain to include the source if it now sits outside it.
            domain_mod.notify_domain_inputs_changed(doc)
            Gui.Control.closeDialog()
            return True

        def reject(self):
            doc = self.obj.Document
            if self.created:
                doc.openTransaction("Wavesim: Cancel Source")
                doc.removeObject(self.obj.Name)
                doc.commitTransaction()
                doc.recompute()
            else:
                # Undo any live position edits.
                self.obj.Position = self._orig_position
            Gui.Control.closeDialog()
            return True

        def getStandardButtons(self):
            try:
                from PySide import QtWidgets as _w
            except ImportError:
                from PySide import QtGui as _w
            buttons = _w.QDialogButtonBox.Ok | _w.QDialogButtonBox.Cancel
            return int(getattr(buttons, "value", buttons))

    def _open_source_panel(obj, created=False):
        """Open (or replace) the source task panel bound to *obj*."""
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskSourcePanel(obj, created=created))

    class CommandAddSource:
        """Create a point Source at the domain centre and open its editor."""

        def GetResources(self):
            return {
                "Pixmap": _POINT_SOURCE_ICON,
                "MenuText": "Add Point Source",
                "ToolTip": "Add a soft point source with a selectable temporal "
                "excitation (Gaussian, sine, rectangular, Gaussian+sine)",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = active_simulation(doc)
            if sim is None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: create a Simulation before adding a source.\n"
                )
                return

            doc.openTransaction("Wavesim: Add Source")
            try:
                src = doc.addObject("App::FeaturePython", "Source")
                SourceObject(src)
                src.Position = default_position_mm(sim)
                src.Label = "Source ({})".format(_describe(src))
                if src.ViewObject is not None:
                    SourceViewProvider(src.ViewObject)
                sources_group(sim).addObject(src)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()

            _open_source_panel(src, created=True)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    Gui.addCommand("Wavesim_AddSource", CommandAddSource())
