# -*- coding: utf-8 -*-
"""Run the scripting-API tests headless under FreeCAD's console.

    "<FreeCAD>\\bin\\FreeCADCmd.exe" tests\\run_api_tests.py [pattern]

Discovers ``tests/api/test_*.py`` (or ``test_<pattern>*.py``), runs them with
unittest and prints a final ``API TESTS: OK`` / ``API TESTS: FAILED`` line --
FreeCADCmd's own exit code is not reliable, so scripts should grep for it.
Optional pattern via the ``WAVESIM_API_TESTS`` environment variable too.
"""

import os
import sys
import unittest

_HERE = os.path.join(
    __import__("FreeCAD").getUserAppDataDir(), "Mod", "wavesim-workbench",
    "tests")
if "__file__" in globals():
    _HERE = os.path.dirname(os.path.abspath(__file__))
_WB = os.path.dirname(_HERE)
for path in (_WB, os.path.join(_HERE, "api")):
    if path not in sys.path:
        sys.path.insert(0, path)

# Same convention as tools/check_*.py: under FreeCADCmd ``import FreeCADGui``
# succeeds but has no addCommand, which the modules' GUI halves call at import.
try:
    import FreeCADGui

    if not hasattr(FreeCADGui, "addCommand"):
        FreeCADGui.addCommand = lambda *a, **k: None
except ImportError:
    pass

_pattern = os.environ.get("WAVESIM_API_TESTS", "")
_args = [a for a in sys.argv[1:] if not a.lower().endswith(".py")]
if _args:
    _pattern = _args[-1]

_suite = unittest.defaultTestLoader.discover(
    os.path.join(_HERE, "api"),
    pattern="test_{}*.py".format(_pattern) if _pattern else "test_*.py",
    top_level_dir=os.path.join(_HERE, "api"))
_result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(_suite)
print("API TESTS: {} (ran {}, failures {}, errors {}, skipped {})".format(
    "OK" if _result.wasSuccessful() else "FAILED", _result.testsRun,
    len(_result.failures), len(_result.errors), len(_result.skipped)))
sys.stdout.flush()
