# -*- coding: utf-8 -*-
"""Wavesim workbench commands and the simulation document-object model.

This is the plumbing every later session builds on:

* :class:`SimulationContainer` is the ``Proxy`` for the top-level "Simulation"
  group -- an ``App::DocumentObjectGroupPython`` that holds the whole FDTD setup
  (Materials, Sources, Monitors child groups) and persists inside the ``.FCStd``.
* :func:`active_simulation` finds that container in a document; later commands'
  ``IsActive`` use it to stay greyed out until a simulation exists.
* :class:`CommandNewSimulation` creates the container and is the only command
  enabled while no simulation exists.

Importing this module registers the commands with ``Gui.addCommand`` (when a GUI
is available). Command classes follow the ``GetResources``/``Activated``/
``IsActive`` pattern used by ``CommandWavesimSettings`` in ``wavesim_settings``.
"""

import os

import FreeCAD

from wavesim_gui import units


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# FreeCAD ``exec``s the init files and restores objects without a stable
# ``__file__`` here, so build resource paths from the user app-data directory.
_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_RESOURCES_DIR = os.path.join(_WB_DIR, "Resources")
_SIM_ICON = os.path.join(_RESOURCES_DIR, "sim.png")
_RUN_ICON = os.path.join(_RESOURCES_DIR, "run.png")

# Marker property stamped on the container. Identifying the simulation by a
# stored property (rather than by ``Proxy`` type) keeps :func:`active_simulation`
# working even before FreeCAD has re-attached the Python proxy on reload.
_TYPE_PROP = "WavesimType"
_SIM_TYPE = "Simulation"

# Child groups created inside every simulation container.
_CHILD_GROUPS = ("Materials", "Sources", "Monitors")


# --------------------------------------------------------------------------- #
# Document-object model
# --------------------------------------------------------------------------- #

class SimulationContainer:
    """``Proxy`` for the top-level Simulation group object.

    Minimal for now -- it stamps the marker property and carries a ``Type`` tag.
    Later sessions hang grid/domain/boundary state off the same container.
    """

    def __init__(self, obj):
        self.Type = _SIM_TYPE
        obj.Proxy = self
        if not hasattr(obj, _TYPE_PROP):
            obj.addProperty(
                "App::PropertyString",
                _TYPE_PROP,
                "Wavesim",
                "Marks this group as the Wavesim simulation container",
            )
            setattr(obj, _TYPE_PROP, _SIM_TYPE)
            # Read-only in the property editor: it is an identity marker.
            obj.setEditorMode(_TYPE_PROP, 1)

        # Display units for time and frequency. The solver always works in SI
        # base units; these only control what the user sees and types. Edited
        # through the Simulation task panel (double-click the Simulation object).
        if not hasattr(obj, "TimeUnit"):
            obj.addProperty(
                "App::PropertyEnumeration", "TimeUnit", "Units",
                "Display unit for time values (converted to seconds for the "
                "solver)",
            )
            obj.TimeUnit = units.time_unit_labels()
            obj.TimeUnit = units.DEFAULT_TIME_UNIT
        if not hasattr(obj, "FrequencyUnit"):
            obj.addProperty(
                "App::PropertyEnumeration", "FrequencyUnit", "Units",
                "Display unit for frequency values (converted to hertz for the "
                "solver)",
            )
            obj.FrequencyUnit = units.freq_unit_labels()
            obj.FrequencyUnit = units.DEFAULT_FREQ_UNIT

        # Maximum simulation time, stored in SI seconds. Edited through the
        # Simulation task panel in the display time unit; the number of time
        # steps is derived from this and the CFL step (read-only in the editor).
        if not hasattr(obj, "MaxTime"):
            obj.addProperty(
                "App::PropertyFloat", "MaxTime", "Run",
                "Maximum simulation time, in seconds (edit via the Simulation "
                "panel in the display time unit)",
            )
            obj.MaxTime = 2.0e-9
            obj.setEditorMode("MaxTime", 1)  # read-only; edit through the panel

        # Maximum frequency of interest, stored in SI hertz. Drives the default
        # grid cell size (c / (fmax * cells-per-wavelength * sqrt(eps*mu))).
        # Edited through the panel in the display frequency unit.
        if not hasattr(obj, "MaxFrequency"):
            obj.addProperty(
                "App::PropertyFloat", "MaxFrequency", "Run",
                "Maximum frequency of interest, in hertz (edit via the "
                "Simulation panel in the display frequency unit). Drives the "
                "default cell size.",
            )
            obj.MaxFrequency = 1.0e9  # 1 GHz
            obj.setEditorMode("MaxFrequency", 1)  # read-only; edit through panel

    def onDocumentRestored(self, obj):
        # Re-assert the back-reference after a reload.
        obj.Proxy = self
        self.Type = getattr(self, "Type", _SIM_TYPE)
        # Editor modes are runtime-only; keep MaxTime/MaxFrequency edited through
        # the panel.
        if hasattr(obj, "MaxTime"):
            obj.setEditorMode("MaxTime", 1)
        if hasattr(obj, "MaxFrequency"):
            obj.setEditorMode("MaxFrequency", 1)

    def execute(self, obj):
        # Pure container; nothing to recompute.
        pass

    # FreeCAD 1.x serializes the proxy via dumps/loads; the __getstate__/
    # __setstate__ aliases keep older builds happy. Returning a dict keeps the
    # proxy reconstructable without external state.
    def dumps(self):
        return {"Type": getattr(self, "Type", _SIM_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _SIM_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


def active_simulation(doc):
    """Return the Simulation container in *doc*, or ``None`` if none exists.

    Matches on the stored marker property so it works regardless of whether the
    Python proxy has been re-attached yet.
    """
    if doc is None:
        return None
    for obj in doc.Objects:
        if getattr(obj, _TYPE_PROP, None) == _SIM_TYPE:
            return obj
    return None


def max_frequency_hz(sim):
    """Return the Simulation container's maximum frequency in hertz.

    Falls back to the 1 GHz default when *sim* is missing the property (e.g. a
    document created before this property existed).
    """
    if sim is None:
        return 1.0e9
    return float(getattr(sim, "MaxFrequency", 1.0e9))


# --------------------------------------------------------------------------- #
# GUI: view provider + commands
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception as exc:  # console mode / no Qt
    FreeCAD.Console.PrintWarning(
        "Wavesim: commands GUI not registered ({}: {})\n".format(
            type(exc).__name__, exc
        )
    )
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

    class SimulationViewProvider:
        """View provider for the Simulation container.

        Tree icon plus a double-click editor for the simulation-wide settings
        (currently the time and frequency display units).
        """

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            self.ViewObject = vobj
            self.Object = vobj.Object

        def getIcon(self):
            return _SIM_ICON

        def setEdit(self, vobj, mode=0):
            _open_simulation_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_simulation_panel(vobj.Object)
            return True

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class TaskSimulationPanel:
        """Task-tab panel for the simulation-wide settings.

        Holds the time/frequency display units and the maximum simulation time.
        The solver always runs in SI base units; the unit dropdowns only change
        what is shown (and typed) throughout the workbench, while the max time is
        stored in seconds. The number of time steps is derived from the max time
        and the CFL step (set by the grid cell sizes) and shown live.
        """

        def __init__(self, obj):
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets
            from wavesim_gui import domain as domain_mod

            self.obj = obj
            self._domain = domain_mod.find_domain(obj)

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Simulation")
            layout = QtWidgets.QFormLayout(form)

            self._time = QtWidgets.QComboBox()
            self._time.addItems(units.time_unit_labels())
            self._time.setCurrentText(units.get_time_unit(obj))

            self._freq = QtWidgets.QComboBox()
            self._freq.addItems(units.freq_unit_labels())
            self._freq.setCurrentText(units.get_frequency_unit(obj))

            # Maximum simulation time, shown in the selected time unit (a fixed
            # display-only suffix here). The current unit is tracked so changing
            # the dropdown re-expresses the same physical time.
            self._time_unit = self._time.currentText()
            self._max_time = QtWidgets.QDoubleSpinBox()
            self._max_time.setRange(0.0, 1.0e12)
            self._max_time.setDecimals(6)
            self._max_time.setSuffix(" " + self._time_unit)
            self._max_time.setSingleStep(1.0)
            self._max_time.setValue(
                units.time_from_si(
                    float(getattr(obj, "MaxTime", 0.0)), self._time_unit
                )
            )

            # Maximum frequency of interest, shown in the selected frequency
            # unit. Tracked like the max time so changing the dropdown
            # re-expresses the same physical frequency.
            self._freq_unit = self._freq.currentText()
            self._max_freq = QtWidgets.QDoubleSpinBox()
            self._max_freq.setRange(0.0, 1.0e12)
            self._max_freq.setDecimals(6)
            self._max_freq.setSuffix(" " + self._freq_unit)
            self._max_freq.setSingleStep(1.0)
            self._orig_max_freq = float(getattr(obj, "MaxFrequency", 1.0e9))
            self._max_freq.setValue(
                units.freq_from_si(self._orig_max_freq, self._freq_unit)
            )

            self._steps = QtWidgets.QLabel()

            layout.addRow("Time unit:", self._time)
            layout.addRow("Frequency unit:", self._freq)
            layout.addRow("Max simulation time:", self._max_time)
            layout.addRow("Max frequency:", self._max_freq)
            layout.addRow("Time steps:", self._steps)

            info = QtWidgets.QLabel(
                "The simulation runs until the maximum time is reached. The "
                "number of time steps is computed from the CFL time step (set by "
                "the grid cell sizes). Units are display-only; values are "
                "converted to seconds / hertz for the solver."
            )
            info.setWordWrap(True)
            layout.addRow(info)

            self._time.currentTextChanged.connect(self._on_time_unit_changed)
            self._freq.currentTextChanged.connect(self._on_freq_unit_changed)
            self._max_time.valueChanged.connect(self._update_steps)
            self._update_steps()

            self.form = form

        def _on_time_unit_changed(self, new_unit):
            """Re-express the max-time value when the time unit dropdown changes."""
            si = units.time_to_si(self._max_time.value(), self._time_unit)
            self._time_unit = new_unit
            self._max_time.setSuffix(" " + new_unit)
            blocked = self._max_time.blockSignals(True)
            self._max_time.setValue(units.time_from_si(si, new_unit))
            self._max_time.blockSignals(blocked)
            self._update_steps()

        def _on_freq_unit_changed(self, new_unit):
            """Re-express the max-frequency value when the freq dropdown changes."""
            si = units.freq_to_si(self._max_freq.value(), self._freq_unit)
            self._freq_unit = new_unit
            self._max_freq.setSuffix(" " + new_unit)
            blocked = self._max_freq.blockSignals(True)
            self._max_freq.setValue(units.freq_from_si(si, new_unit))
            self._max_freq.blockSignals(blocked)

        def _update_steps(self, *_):
            from wavesim_gui import domain as domain_mod

            si = units.time_to_si(self._max_time.value(), self._time_unit)
            steps = domain_mod.time_steps_for(self._domain, si)
            if steps > 0:
                self._steps.setText("{:,}".format(steps))
            else:
                self._steps.setText("(set a max time and grid cell size)")

        def accept(self):
            from wavesim_gui import domain as domain_mod

            doc = self.obj.Document
            doc.openTransaction("Wavesim: Edit Simulation")
            self.obj.TimeUnit = self._time.currentText()
            self.obj.FrequencyUnit = self._freq.currentText()
            self.obj.MaxTime = units.time_to_si(
                self._max_time.value(), self._time_unit
            )
            new_max_freq = units.freq_to_si(self._max_freq.value(), self._freq_unit)
            self.obj.MaxFrequency = new_max_freq
            # The max frequency drives the default cell size, so when it changes
            # re-derive the Domain's cell sizes from it and recompute -- the mesh
            # display and derived counts update immediately without opening the
            # Domain panel. Left alone when the frequency is unchanged, so custom
            # cell sizes survive an unrelated edit (e.g. changing a display unit).
            domain = self._domain
            freq_changed = abs(new_max_freq - self._orig_max_freq) > 1.0e-6
            if domain is not None and freq_changed:
                size_m = domain_mod.default_cell_size_m(self.obj, domain=domain)
                if size_m is not None:
                    size_mm = "{} mm".format(size_m * 1000.0)
                    domain.Dx = domain.Dy = domain.Dz = size_mm
            doc.commitTransaction()
            doc.recompute()
            Gui.Control.closeDialog()
            return True

        def reject(self):
            Gui.Control.closeDialog()
            return True

        def getStandardButtons(self):
            try:
                from PySide import QtWidgets as _w
            except ImportError:
                from PySide import QtGui as _w
            buttons = _w.QDialogButtonBox.Ok | _w.QDialogButtonBox.Cancel
            return int(getattr(buttons, "value", buttons))

    def _open_simulation_panel(obj):
        """Open (or replace) the simulation task panel bound to *obj*."""
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskSimulationPanel(obj))

    class CommandNewSimulation:
        """Create the top-level Simulation container and its child groups."""

        def GetResources(self):
            return {
                "Pixmap": _SIM_ICON,
                "MenuText": "New Simulation",
                "ToolTip": "Create a Wavesim simulation container in the active "
                "document",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            if doc is None:
                doc = FreeCAD.newDocument()

            if active_simulation(doc) is not None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: this document already has a Simulation.\n"
                )
                return

            doc.openTransaction("Wavesim: New Simulation")
            try:
                sim = doc.addObject(
                    "App::DocumentObjectGroupPython", "Simulation"
                )
                SimulationContainer(sim)
                sim.Label = "Simulation"
                if sim.ViewObject is not None:
                    SimulationViewProvider(sim.ViewObject)

                for name in _CHILD_GROUPS:
                    grp = doc.addObject("App::DocumentObjectGroup", name)
                    grp.Label = name
                    sim.addObject(grp)

                # The domain is a singleton created with the simulation; it
                # starts empty and auto-sizes once material geometry is assigned.
                from wavesim_gui import domain as domain_mod
                domain = domain_mod.create_domain(doc, sim)

                # Seed the two materials nearly every simulation needs, and make
                # Vacuum the domain's default background (empty-voxel) medium.
                from wavesim_gui import materials as materials_mod
                vacuum, _pec = materials_mod.create_default_materials(doc, sim)
                if hasattr(domain, "Background"):
                    domain.Background = vacuum
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()

            doc.recompute()
            FreeCAD.Console.PrintMessage("Wavesim: created Simulation.\n")

        def IsActive(self):
            # Enabled only while the active document has no simulation yet
            # (or there is no document, in which case Activated creates one).
            doc = FreeCAD.ActiveDocument
            return doc is None or active_simulation(doc) is None

    Gui.addCommand("Wavesim_NewSimulation", CommandNewSimulation())

    class CommandRun:
        """Run the (Session-2 hardcoded) simulation through the conda bridge.

        Serialises a minimal vacuum-box job to a working directory under the
        configured results folder, runs the conda-side ``runner.py`` out of
        process with a progress dialog, then reports the summary. Real geometry,
        materials, sources and monitors arrive in later sessions; this proves the
        bridge round-trip end to end.
        """

        def GetResources(self):
            return {
                "Pixmap": _RUN_ICON,
                "MenuText": "Run Simulation",
                "ToolTip": "Run the simulation out-of-process via the Wavesim "
                "solver and load the results",
            }

        def Activated(self):
            # Imported lazily so a failure here cannot abort command registration
            # at workbench Initialize time.
            from wavesim_gui import job as job_mod
            from wavesim_gui import run as run_mod
            from wavesim_gui import voxelize as vox_mod
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets

            # Build from real geometry when materials are assigned; otherwise
            # fall back to the Session-2 hardcoded vacuum box. Materials without
            # a grid is a user error -- refuse rather than guess a cell size.
            # Voxelisation runs on the GUI thread and can be slow on fine grids;
            # show a cancelable progress dialog while it sweeps the geometry.
            vox_dialog, vox_cb = run_mod.voxelization_progress(
                Gui.getMainWindow(), "Wavesim Run", "Voxelizing geometry..."
            )
            try:
                spec, arrays = vox_mod.build_job_from_document(
                    FreeCAD.ActiveDocument, progress=vox_cb
                )
            except vox_mod.VoxelizationCancelled:
                vox_dialog.close()
                FreeCAD.Console.PrintWarning("Wavesim: run cancelled.\n")
                return
            except vox_mod.GridRequiredError as exc:
                vox_dialog.close()
                QtWidgets.QMessageBox.warning(
                    Gui.getMainWindow(), "Wavesim Run", str(exc)
                )
                return
            vox_dialog.close()
            if spec is None:
                spec = job_mod.build_demo_job()
                FreeCAD.Console.PrintWarning(
                    "Wavesim: no materials assigned; running the demo box.\n"
                )

            workdir = job_mod.new_workdir()
            job_mod.write_job(workdir, spec)
            if arrays is not None:
                vox_mod.write_materials(workdir, arrays)

            FreeCAD.Console.PrintMessage(
                "Wavesim: running job in {}\n".format(workdir)
            )
            summary = run_mod.run_job(
                workdir, spec["steps"], parent=Gui.getMainWindow()
            )
            if summary is not None:
                from wavesim_gui import results as results_mod
                sim = active_simulation(FreeCAD.ActiveDocument)
                if sim is not None:
                    results_mod.build_results(
                        FreeCAD.ActiveDocument, sim, workdir, summary
                    )
                _show_run_summary(summary, workdir)

        def IsActive(self):
            # Enabled only once a simulation container exists in the document.
            return active_simulation(FreeCAD.ActiveDocument) is not None

    def _show_run_summary(summary, workdir):
        """Pop a short info dialog with the grid dims, CFL dt and timing."""
        try:
            from PySide import QtWidgets
        except ImportError:
            from PySide import QtGui as QtWidgets
        lines = [
            "Grid: {}x{}x{} cells".format(
                summary.get("Nx", "?"), summary.get("Ny", "?"),
                summary.get("Nz", "?"),
            ),
            "Time step dt: {:.4e} s".format(summary.get("dt", float("nan"))),
            "Steps: {}  (sim time {:.3e} s)".format(
                summary.get("steps", "?"), summary.get("sim_time_s", float("nan"))
            ),
            "Wall time: {:.2f} s".format(summary.get("wall_time_s", 0.0)),
        ]
        if "dielectric_cells" in summary:
            lines.append("Dielectric cells: {}".format(summary["dielectric_cells"]))
        if "pec_cells" in summary:
            lines.append("PEC cells: {}".format(summary["pec_cells"]))
        lines.append("\nOutput: {}".format(workdir))
        QtWidgets.QMessageBox.information(
            Gui.getMainWindow(), "Wavesim Run Complete", "\n".join(lines)
        )

    Gui.addCommand("Wavesim_Run", CommandRun())

    # Importing these modules registers their commands. Done last so
    # active_simulation / the child-group constants are fully defined before they
    # import from this module.
    from wavesim_gui import materials  # noqa: F401  (registers Wavesim_AssignMaterial)
    from wavesim_gui import domain  # noqa: F401  (registers the Domain object/VP)
    from wavesim_gui import source  # noqa: F401  (registers Wavesim_AddSource)
    from wavesim_gui import tem_source  # noqa: F401  (registers Wavesim_AddTEMSource)
    from wavesim_gui import spice_port  # noqa: F401  (registers the SPICE port commands)
    from wavesim_gui import monitors  # noqa: F401  (registers the monitor commands)
    from wavesim_gui import results  # noqa: F401  (registers result proxies/VPs)
