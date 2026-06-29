# -*- coding: utf-8 -*-
"""GUI package for the Wavesim FreeCAD workbench.

Holds the command classes, document-object proxies, view providers and (later)
the voxelizer, job builder and run driver. ``InitGui.py`` imports
:mod:`wavesim_gui.commands` during workbench initialization, which registers the
commands with ``Gui.addCommand``.

The workbench's ``Mod`` directory is on ``sys.path`` (that is how
``wavesim_settings`` is importable), so ``import wavesim_gui`` resolves both at
startup and when FreeCAD restores scripted objects from a saved ``.FCStd``.
"""
