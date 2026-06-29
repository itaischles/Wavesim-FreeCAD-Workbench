# -*- coding: utf-8 -*-
"""Non-GUI initialization for the Wavesim workbench.

FreeCAD imports this file at startup (both GUI and console modes), before
``InitGui.py``. Its job is to make the Wavesim solver package importable from
FreeCAD's bundled Python so the workbench can drive it later.

The solver lives in its own repository, separate from this workbench, so we add
its location to ``sys.path``. The path is taken from the ``WAVESIM_PATH``
environment variable when set, otherwise it falls back to the development
checkout location.
"""

import os
import sys

import FreeCAD


# Location of the Wavesim repository (the folder that contains the ``wavesim``
# package). Sourced from the workbench-local settings (saved value > the
# WAVESIM_PATH environment variable > built-in default). The settings module is
# Qt-free and console-safe, so importing it here (before InitGui) is fine; fall
# back to the env/default if it cannot be imported. There is no built-in path:
# set it once via Wavesim -> Settings (or the WAVESIM_PATH environment variable).
_DEFAULT_WAVESIM_PATH = ""
try:
    import wavesim_settings
    WAVESIM_PATH = wavesim_settings.get_wavesim_path()
except Exception:
    WAVESIM_PATH = os.environ.get("WAVESIM_PATH", _DEFAULT_WAVESIM_PATH)


def _register_wavesim():
    """Put the Wavesim repository on ``sys.path`` and confirm it imports."""
    if not os.path.isdir(WAVESIM_PATH):
        FreeCAD.Console.PrintWarning(
            "Wavesim: solver path not found: {}\n"
            "Set the WAVESIM_PATH environment variable to the repository "
            "that contains the 'wavesim' package.\n".format(WAVESIM_PATH)
        )
        return

    if WAVESIM_PATH not in sys.path:
        sys.path.insert(0, WAVESIM_PATH)

    try:
        import wavesim  # noqa: F401
        FreeCAD.Console.PrintLog(
            "Wavesim: solver v{} found at {}\n".format(
                getattr(wavesim, "__version__", "?"), WAVESIM_PATH
            )
        )
    except Exception as exc:  # pragma: no cover - diagnostic path
        FreeCAD.Console.PrintWarning(
            "Wavesim: failed to import the solver package from {}: {}\n".format(
                WAVESIM_PATH, exc
            )
        )


_register_wavesim()
