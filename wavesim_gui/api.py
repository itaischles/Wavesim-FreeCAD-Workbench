# -*- coding: utf-8 -*-
"""The Wavesim scripting API -- one import for everything.

    from wavesim_gui import api
    sim = api.new_simulation(App.ActiveDocument)
    api.create_material(sim, label="FR4", eps=4.4)

GUI-free functions that create and modify exactly the objects the workbench's
commands create (the commands themselves call these functions). Usable from
the FreeCAD Python console, from ``FreeCADCmd`` and -- through the whitelist
in :func:`registered_functions` -- from the Agent bridge.

Conventions (argument order, references, quantities with units and param
expressions, transactions, errors) are documented in
:mod:`wavesim_gui.api_common`; ``docs/API_INVENTORY.md`` maps every GUI
command to its API function.
"""

import importlib

from wavesim_gui.api_common import (  # noqa: F401
    API_VERSION, ApiError, describe, registered_functions, select_subshapes,
)

_MODULES = (
    "api_simulation", "api_geometry", "api_excitation", "api_monitors",
    "api_params", "api_preview", "api_run",
)

__all__ = ["API_VERSION", "ApiError", "describe", "registered_functions",
           "select_subshapes"]

for _name in _MODULES:
    try:
        _mod = importlib.import_module("wavesim_gui." + _name)
    except ModuleNotFoundError as _exc:
        if _exc.name != "wavesim_gui." + _name:
            raise
        continue
    for _sym in getattr(_mod, "__all__", ()):
        globals()[_sym] = getattr(_mod, _sym)
        __all__.append(_sym)
