# -*- coding: utf-8 -*-
"""Results visualisation for the Wavesim workbench (Session 8).

After a successful run, :func:`build_results` adds a "Results" group to the
Simulation tree holding one leaf object per monitor that produced data:

* **Energy** -- the total-domain energy time series.
* **Probe**  -- a single field component (or magnitude) at one point vs. time.
* **Voltage** / **Current** -- line-integral (V = ∫E·dl / I = ∮H·dl) time series.
* **Snapshot** -- a stack of 2D field slices animated over time.

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
_RESULTS_ICON = os.path.join(_RESOURCES_DIR, "run.png")
_RESULT_ICON = os.path.join(_RESOURCES_DIR, "mesh.png")

_TYPE_PROP = "WavesimType"
_RESULTS_TYPE = "Results"   # the container group
_RESULT_TYPE = "Result"     # a single result leaf

# Result kinds (stored on each leaf's ResultKind property).
_KIND_ENERGY = "energy"
_KIND_PROBE = "probe"
_KIND_SNAPSHOT = "snapshot"
_KIND_MODE = "mode"
_KIND_VOLTAGE = "voltage"
_KIND_CURRENT = "current"
_KIND_SPICE_V = "spice_v"   # SPICE co-simulation port voltage V(t)
_KIND_SPICE_I = "spice_i"   # SPICE co-simulation port current I(t)

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


class ResultObject:
    """``Proxy`` for one result leaf (energy / probe / snapshot).

    Carries everything needed to (re)open its plot without the producing
    monitor: the run directory (``ResultsDir``), the ``results.npz`` array key
    (``DataKey``) and the kind/component. Snapshot leaves also store the slice's
    physical in-plane extent and axis labels so the animation can be drawn in mm.
    """

    def __init__(self, obj, kind):
        self.Type = _RESULT_TYPE
        obj.Proxy = self
        _add_type_marker(obj, _RESULT_TYPE)

        if not hasattr(obj, "ResultKind"):
            obj.addProperty(
                "App::PropertyString", "ResultKind", "Result",
                "Kind of result: energy, probe or snapshot",
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
    """Delete any existing Results group (and its leaves) under *sim*."""
    grp = find_results(sim)
    if grp is None:
        return
    for child in list(grp.Group):
        doc.removeObject(child.Name)
    doc.removeObject(grp.Name)


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
        sim.addObject(grp)

        def _new_leaf(name, kind, data_key, component=""):
            leaf = doc.addObject("App::FeaturePython", "Result")
            ResultObject(leaf, kind)
            leaf.Label = name
            leaf.DataKey = data_key
            leaf.ResultsDir = workdir
            leaf.Component = component
            if leaf.ViewObject is not None:
                ResultViewProvider(leaf.ViewObject)
            grp.addObject(leaf)
            return leaf

        # Energy (whole-domain total energy).
        if "energy_values" in keys:
            _new_leaf("Energy", _KIND_ENERGY, "energy")

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
        for idx, meta in enumerate(summary.get("spice_ports", [])):
            name = meta.get("name") or "SPICE Port {}".format(idx)
            if "spice_{}v_values".format(idx) in keys:
                _new_leaf("{} voltage".format(name), _KIND_SPICE_V,
                          "spice_{}v".format(idx))
            if "spice_{}i_values".format(idx) in keys:
                _new_leaf("{} current".format(name), _KIND_SPICE_I,
                          "spice_{}i".format(idx))

        # Snapshots (frame stacks). Capture the slice's physical extent from the
        # producing monitor so the animation can be drawn in millimetres.
        for idx, meta in enumerate(summary.get("snapshots", [])):
            if "snapshot_{}_data".format(idx) not in keys:
                continue
            comp = meta.get("component", "")
            name = meta.get("name") or "Snapshot {}".format(idx)
            leaf = _new_leaf(
                name, _KIND_SNAPSHOT, "snapshot_{}".format(idx), comp,
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

        # TEM modes (one leaf per solved port mode). Each opens a figure of the
        # mode shape plus the port's per-unit-length parameters.
        for meta in summary.get("modes", []):
            key = "mode_{}_{}".format(
                meta.get("source_index", 0), meta.get("mode_index", 0)
            )
            if key + "_phi" not in keys:
                continue
            cond = meta.get("conductor_id", "?")
            name = "{} mode (conductor {})".format(
                meta.get("name", "TEM"), cond
            )
            leaf = _new_leaf(name, _KIND_MODE, key, "")
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
    except Exception:
        doc.abortTransaction()
        raise
    doc.commitTransaction()
    doc.recompute()
    FreeCAD.Console.PrintMessage(
        "Wavesim: results added to the tree (double-click a node to plot).\n"
    )
    return grp


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


# --------------------------------------------------------------------------- #
# GUI: view providers + matplotlib plot windows
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

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

    class ResultsViewProvider:
        """Tree icon for the Results group (no 3D geometry, no editor)."""

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            self.ViewObject = vobj
            self.Object = vobj.Object

        def getIcon(self):
            return _RESULTS_ICON

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class ResultViewProvider:
        """Tree view provider for a result leaf; double-click opens its plot."""

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            self.ViewObject = vobj
            self.Object = vobj.Object

        def getIcon(self):
            return _RESULT_ICON

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
            elif kind == _KIND_SNAPSHOT:
                _plot_snapshot(obj)
            elif kind == _KIND_MODE:
                _plot_mode(obj)
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

    def _plot_series(obj, ylabel, title, color):
        """1D time-series plot shared by the energy/probe/voltage/current leaves.

        Reads ``<DataKey>_times`` / ``<DataKey>_values`` from the leaf's
        ``results.npz`` and draws them in a non-modal window.
        """
        workdir = str(obj.ResultsDir)
        key = str(obj.DataKey)
        times = _load_array(workdir, key + "_times")
        values = _load_array(workdir, key + "_values")
        if times is None or values is None:
            _missing(obj)
            return
        unit = _time_unit(obj)
        t = [units.time_from_si(float(v), unit) for v in times]

        made = _make_window("Wavesim Results - {}".format(obj.Label))
        if made is None:
            return
        dialog, figure, _layout = made
        ax = figure.add_subplot(111)
        ax.plot(t, values, color=color)
        ax.set_xlabel("time ({})".format(unit))
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(True, alpha=0.3)
        dialog._canvas.draw()
        dialog.show()
        _register_window(dialog)

    def _plot_energy(obj):
        _plot_series(obj, "total energy", "Total domain energy", "#d65a00")

    def _plot_probe(obj):
        comp = str(getattr(obj, "Component", "")) or "field"
        _plot_series(obj, comp, "Probe: {} vs. time".format(comp), "#1f77b4")

    def _plot_voltage(obj):
        _plot_series(
            obj, "voltage (V)", "Voltage: ∫E·dl vs. time", "#2ca02c"
        )

    def _plot_current(obj):
        _plot_series(
            obj, "current (A)", "Current: ∮H·dl vs. time", "#9467bd"
        )

    def _plot_spice_voltage(obj):
        _plot_series(
            obj, "voltage (V)", "SPICE port voltage vs. time", "#2ca02c"
        )

    def _plot_spice_current(obj):
        _plot_series(
            obj, "current (A)", "SPICE port current vs. time", "#9467bd"
        )

    def _plot_snapshot(obj):
        import numpy as np

        workdir = str(obj.ResultsDir)
        key = str(obj.DataKey)
        frames = _load_array(workdir, key + "_data")
        times = _load_array(workdir, key + "_times")
        if frames is None or len(frames) == 0:
            _missing(obj)
            return

        unit = _time_unit(obj)
        comp = str(getattr(obj, "Component", "")) or "field"
        is_magnitude = comp.startswith("|") or comp.startswith("∣")

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
        from matplotlib import colors as mcolors

        vmax = float(np.nanmax(np.abs(frames))) or 1.0
        cmap = "inferno" if is_magnitude else "RdBu_r"
        linthresh = vmax / 1e3

        def _make_norm(log):
            if is_magnitude:
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
        # frames[f] has shape (axis1, axis2); show axis1 horizontal, axis2
        # vertical with a lower-left origin. Equal aspect so a square physical
        # extent renders square rather than stretched to fill the axes.
        if use_mesh:
            # Gouraud shading interpolates smoothly between cell centres (rather
            # than drawing flat cells), so it takes centre coordinates matching
            # C's shape -- not the N+1 edge arrays. C is (Ny, Nx), the transposed
            # frame against (xcenters, ycenters). Non-uniform spacing is honoured.
            xe = np.asarray(xedges)
            ye = np.asarray(yedges)
            xcenters = 0.5 * (xe[:-1] + xe[1:])
            ycenters = 0.5 * (ye[:-1] + ye[1:])
            artist = ax.pcolormesh(
                xcenters, ycenters, np.asarray(frames[0]).T,
                cmap=cmap, norm=_make_norm(False), shading="gouraud",
            )
            ax.set_aspect("equal")
        else:
            artist = ax.imshow(
                np.asarray(frames[0]).T, origin="lower", extent=extent,
                cmap=cmap, norm=_make_norm(False), aspect="equal",
                interpolation="bilinear",
            )

        def _set_frame_data(idx):
            data2d = np.asarray(frames[idx]).T
            if use_mesh:
                artist.set_array(data2d.ravel())
            else:
                artist.set_data(data2d)

        figure.colorbar(artist, ax=ax, label=comp)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)

        plane = str(getattr(obj, "Plane", ""))
        off = float(obj.Offset.Value) if hasattr(obj, "Offset") else 0.0
        suffix = " ({} @ {:g} mm)".format(plane, off) if plane else ""

        def _frame_time(idx):
            if times is not None and idx < len(times):
                return units.time_from_si(float(times[idx]), unit)
            return float("nan")

        def _set_title(idx):
            ax.set_title("{}{}\nframe {}/{}  t = {:.4g} {}".format(
                comp, suffix, idx + 1, len(frames), _frame_time(idx), unit,
            ))

        _set_title(0)
        dialog._canvas.draw()

        # --- frame controls: slider + Play + log-scale toggle ------------- #
        controls = QtWidgets.QHBoxLayout()
        play = QtWidgets.QPushButton("Play")
        play.setCheckable(True)
        slider = QtWidgets.QSlider(_QtCore.Qt.Horizontal)
        slider.setRange(0, len(frames) - 1)
        log_check = QtWidgets.QCheckBox("Log scale")
        controls.addWidget(play)
        controls.addWidget(slider, 1)
        controls.addWidget(log_check)
        layout.addLayout(controls)

        def on_log(checked):
            artist.set_norm(_make_norm(bool(checked)))
            dialog._canvas.draw_idle()

        log_check.toggled.connect(on_log)

        def show_frame(idx):
            idx = max(0, min(int(idx), len(frames) - 1))
            _set_frame_data(idx)
            _set_title(idx)
            dialog._canvas.draw_idle()

        slider.valueChanged.connect(show_frame)

        timer = _QtCore.QTimer(dialog)
        timer.setInterval(100)  # ms between frames

        def advance():
            nxt = (slider.value() + 1) % len(frames)
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
    # TEM mode plotter
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
        name = meta.get("name", "TEM")
        return {
            "label": "{} — energized conductor {}".format(
                name, meta.get("conductor_id", "?")
            ),
            "port_name": name,
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

    def _draw_mode(figure, data):
        """Draw a solved TEM mode into *figure*: φ contours + E quiver + PEC outline.

        Mirrors :func:`wavesim.viz.plot_tem_mode` but works from the raw 2D arrays
        (FreeCAD's Python cannot import the solver), drawing with its own
        matplotlib. The port's per-unit-length parameters go in an annotation box.
        *figure* is cleared first, so the mode selector can redraw in place.
        """
        import math

        import numpy as np

        figure.clear()
        ax = figure.add_subplot(111)

        phi = data["phi"]
        Na, Nb = phi.shape
        # Cell-centre coordinates in mm (the workbench's display unit). Prefer the
        # real transverse coordinate arrays from the runner (which honour a
        # non-uniform grid); fall back to a constant da/db spacing for older runs.
        coords_a, coords_b = data["coords_a"], data["coords_b"]
        if len(coords_a) == Na and len(coords_b) == Nb:
            xa = np.asarray(coords_a)
            yb = np.asarray(coords_b)
        else:
            xa = (np.arange(Na) + 0.5) * (data["da"] or 1.0) * 1.0e3
            yb = (np.arange(Nb) + 0.5) * (data["db"] or 1.0) * 1.0e3

        cf = ax.contourf(xa, yb, phi.T, levels=20, cmap="RdBu_r")
        figure.colorbar(cf, ax=ax, pad=0.02, label="potential φ (V)")

        Ea, Eb = data["Ea"], data["Eb"]
        if Ea is not None and Eb is not None:
            step = max(1, min(Na, Nb) // 25)
            AX, BY = np.meshgrid(xa[::step], yb[::step])
            ax.quiver(AX, BY, Ea.T[::step, ::step], Eb.T[::step, ::step],
                      color="k", alpha=0.7, pivot="mid")

        pec = data["pec"]
        if pec is not None and np.any(pec):
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

        lines = ["energized conductor {}".format(data["conductor_id"]),
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

    def _plot_mode(obj):
        """Open the figure of a mode leaf saved by a run (double-click in the tree)."""
        data = _mode_data_from_leaf(obj)
        if data is None:
            _missing(obj)
            return
        made = _make_window("Wavesim Results - {}".format(obj.Label))
        if made is None:
            return
        dialog, figure, _layout = made
        _draw_mode(figure, data)
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
                _draw_mode(figure, datas[idx])
                dialog._canvas.draw_idle()

            def step(delta):
                combo.setCurrentIndex(
                    max(0, min(combo.currentIndex() + delta, total - 1))
                )

            combo.currentIndexChanged.connect(show_mode)
            prev.clicked.connect(lambda *_: step(-1))
            nxt.clicked.connect(lambda *_: step(+1))
            prev.setEnabled(False)

        _draw_mode(figure, datas[0])
        dialog._canvas.draw()
        dialog.show()
        _register_window(dialog)
        return True
