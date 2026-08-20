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

# Workbench icon, from the 24x24 SVG set in Resources/icons (the retired PNGs
# and the old placeholder wavesim_workbench.svg are still in Resources/).
WB_ICON = os.path.join(RESOURCES_DIR, "icons", "wavesim.svg")


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

        def add_group(*command_ids):
            """Append a group of commands, divided from the ones before it.

            The literal id ``"Separator"`` is what FreeCAD turns into a toolbar
            divider and a menu separator, so grouping is expressed here as the
            call boundary rather than as separators sprinkled through one flat
            list. Nothing is added for an empty group, and a divider is only
            emitted when there is something to divide from -- so a group whose
            import failed cannot leave a stray separator behind.
            """
            ids = [cid for cid in command_ids if cid]
            if not ids:
                return
            if self.command_list:
                self.command_list.append("Separator")
            self.command_list.extend(ids)

        # Core simulation commands, in toolbar/menu order: set up the model,
        # then drive it, then measure it, then run it. Importing the package
        # registers them with Gui.addCommand; the import is guarded so a failure
        # is reported rather than aborting Initialize and leaving the workbench
        # commandless.
        try:
            from wavesim_gui import commands  # noqa: F401  (registers commands)

            # Model setup: the container and what the geometry is made of.
            add_group(
                "Wavesim_NewSimulation",
                "Wavesim_AssignMaterial",
            )
            # Sources and ports: everything that puts energy into the domain or
            # terminates it. SPICE TEM ports are a drive mode of the Modal Port
            # (Wavesim_AddModalPort), so no separate toolbar button. The
            # Wavesim_AddSpiceTEMPort command stays registered for backward
            # compatibility with documents that still hold legacy SpiceTEMPort
            # objects, as does the old Wavesim_AddTEMSource id.
            add_group(
                "Wavesim_AddSource",
                "Wavesim_AddModalPort",
                "Wavesim_AddGaussianBeam",
                "Wavesim_AddLumpedPort",
                "Wavesim_AddSpiceLinePort",
            )
            # Monitors: everything that records, point to whole-domain.
            add_group(
                "Wavesim_AddProbe",
                "Wavesim_AddSnapshot",
                "Wavesim_AddEnergyMonitor",
                "Wavesim_AddDissipationMonitor",
                "Wavesim_AddVoltageMonitor",
                "Wavesim_AddCurrentMonitor",
            )
            # The solves, together: the two buttons that cost minutes. The port
            # matrix is the same run repeated once per port with the drive
            # moved, so it belongs beside Run rather than among the monitors --
            # and importing the module is what registers it.
            from wavesim_gui import portmatrix  # noqa: F401

            add_group("Wavesim_Run", "Wavesim_PortMatrix")
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
            add_group("Wavesim_Settings")
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
