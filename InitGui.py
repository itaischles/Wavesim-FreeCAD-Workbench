# -*- coding: utf-8 -*-
"""GUI initialization for the Wavesim workbench.

FreeCAD imports this file at startup for every package under its ``Mod``
directory. Defining a :class:`Gui.Workbench` subclass and registering it with
``Gui.addWorkbench`` is what makes the workbench appear in the workbench
selector. Command registration happens lazily in :meth:`Initialize`, which
FreeCAD calls the first time the user activates the workbench.
"""

import os

import FreeCAD
import FreeCADGui as Gui


# Locate this workbench's resources. FreeCAD ``exec``s the init files rather
# than importing them as modules, so ``__file__`` is not available here; build
# the path from the user's app-data directory instead.
WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
RESOURCES_DIR = os.path.join(WB_DIR, "Resources")

# Placeholder workbench icon — swap for the final artwork later.
WB_ICON = os.path.join(RESOURCES_DIR, "wavesim_workbench.svg")


class WavesimWorkbench(Gui.Workbench):
    """Wavesim FDTD electromagnetics solver workbench."""

    # NOTE: FreeCAD ``exec``s this file with separate globals/locals dicts, so a
    # class body cannot see module-level names (WB_ICON, os, ...). Keep only
    # literals here; the Icon path is attached after the class definition below.
    MenuText = "Wavesim"
    ToolTip = "FDTD electromagnetic simulation powered by the Wavesim solver"

    def Initialize(self):
        """Set up commands, toolbars and menus.

        Called once, on first activation. Commands will be imported and
        registered here as the workbench grows; for now the toolbar and menu
        are created empty so the workbench loads cleanly.
        """
        self.command_list = []

        # Core simulation commands. Importing the package registers them with
        # Gui.addCommand; the import is guarded so a failure is reported rather
        # than aborting Initialize and leaving the workbench commandless.
        try:
            from wavesim_gui import commands  # noqa: F401  (registers commands)
            self.command_list.append("Wavesim_NewSimulation")
            self.command_list.append("Wavesim_AssignMaterial")
            self.command_list.append("Wavesim_AddSource")
            self.command_list.append("Wavesim_AddTEMSource")
            self.command_list.append("Wavesim_AddPlaneWave")
            self.command_list.append("Wavesim_AddSpiceLinePort")
            # SPICE TEM ports are now a drive mode of the unified TEM Source
            # (Wavesim_AddTEMSource), so no separate toolbar button. The
            # Wavesim_AddSpiceTEMPort command stays registered for backward
            # compatibility with documents that still hold legacy SpiceTEMPort
            # objects.
            self.command_list.append("Wavesim_AddProbe")
            self.command_list.append("Wavesim_AddSnapshot")
            self.command_list.append("Wavesim_AddEnergyMonitor")
            self.command_list.append("Wavesim_AddVoltageMonitor")
            self.command_list.append("Wavesim_AddCurrentMonitor")
            self.command_list.append("Wavesim_Run")
        except Exception as exc:
            FreeCAD.Console.PrintError(
                "Wavesim: failed to load commands module ({}: {})\n".format(
                    type(exc).__name__, exc
                )
            )

        # Workbench-local settings (solver interpreter and repository paths).
        # Importing the module registers the "Wavesim_Settings" command. It
        # lives in its own menu entry rather than FreeCAD's global preferences.
        # Import failures are reported rather than silently aborting Initialize.
        try:
            import wavesim_settings  # noqa: F401  (registers Gui.addCommand)
            self.command_list.append("Wavesim_Settings")
        except Exception as exc:
            FreeCAD.Console.PrintError(
                "Wavesim: failed to load settings module ({}: {})\n".format(
                    type(exc).__name__, exc
                )
            )

        # appendToolbar/appendMenu reject empty lists, so only call them once we
        # have at least one command registered.
        if self.command_list:
            self.appendToolbar("Wavesim", self.command_list)
            self.appendMenu("Wavesim", self.command_list)

    def Activated(self):
        """Called when the user switches to this workbench."""
        pass

    def Deactivated(self):
        """Called when the user switches away from this workbench."""
        pass

    def GetClassName(self):
        # A pure-Python workbench must report this exact sentinel.
        return "Gui::PythonWorkbench"


# Attach the icon at module level, where WB_ICON resolves (see note above).
WavesimWorkbench.Icon = WB_ICON

Gui.addWorkbench(WavesimWorkbench())
