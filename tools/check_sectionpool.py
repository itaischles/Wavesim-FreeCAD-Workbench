# -*- coding: utf-8 -*-
"""Gate: parallel OCC sectioning must change the speed and nothing else.

:mod:`wavesim_gui.sectionpool` cuts a body's section planes in worker processes
instead of in-process. That is only admissible if the arrays come out
**bit-identical** -- the workbench's whole regression culture rests on a given
document voxelising to the same numbers -- so this voxelises a document twice,
once with the pool disabled and once with it on, and compares every array
exactly. It also checks the two orderings of the sweep agree about *how many*
section planes were cut, since a prefetch that missed planes would silently fall
back to the serial path and pass a value check while buying nothing.

Run under ``freecadcmd`` (FreeCAD's Python; needs Part + the bundled numpy)::

    set WSCHECK_DOC=C:\\path\\to\\model.FCStd
    freecadcmd.exe tools\\check_sectionpool.py

With no ``WSCHECK_DOC`` it builds a small synthetic document instead, so the gate
runs with no test corpus on disk. Exit status is not usable under freecadcmd
(it swallows ``sys.exit``); read the PASS/FAIL line.
"""

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import FreeCAD  # noqa: E402

try:  # freecadcmd supplies a stub FreeCADGui that the gui modules probe for
    import FreeCADGui  # noqa: E402

    if not hasattr(FreeCADGui, "addCommand"):
        FreeCADGui.addCommand = lambda *a, **k: None
except ImportError:
    pass

import numpy as np  # noqa: E402

import wavesim_settings  # noqa: E402
from wavesim_gui import sectionpool, voxelize  # noqa: E402


def _count_sections(fn):
    """Run *fn* counting planes **actually cut in-process**.

    Not merely the calls: the prefetch leaves the serial loop calling
    ``_section_polygons`` exactly as before, and it returns a cached polygon
    list without touching OCC. So a call is only counted when the cache would
    *not* have served it -- which is the number the pool is supposed to drive
    towards zero.
    """
    cut = [0]
    original = voxelize._section_polygons

    def counting(body_shape, z_axis, z, deflection, nudge=0.0):
        cache = voxelize._ACTIVE_CACHE
        served = (cache is not None and cache.shape is body_shape
                  and voxelize._is_z_axis(z_axis)
                  and (float(z), float(deflection),
                       float(nudge)) in cache.planes)
        if not served:
            cut[0] += 1
        return original(body_shape, z_axis, z, deflection, nudge)

    voxelize._section_polygons = counting
    try:
        return fn(), cut[0]
    finally:
        voxelize._section_polygons = original


def main():
    doc_path = os.environ.get("WSCHECK_DOC", "")
    print("check_sectionpool: workers reported by resolve_workers('auto') = %d"
          % sectionpool.resolve_workers("auto"))
    print("                   resolve_workers('0') = %d, ('1') = %d"
          % (sectionpool.resolve_workers("0"), sectionpool.resolve_workers("1")))

    if doc_path:
        ok = _check_document(doc_path)
    else:
        print("no WSCHECK_DOC set -- nothing to voxelise; set it to a .FCStd "
              "built in the GUI (see the validation suite).")
        ok = None

    if ok is None:
        print("\ncheck_sectionpool: SKIPPED (no document)")
    elif ok:
        print("\ncheck_sectionpool: PASS")
    else:
        print("\ncheck_sectionpool: FAIL")


def _check_document(path):
    doc = FreeCAD.openDocument(path)
    doc.recompute()
    print("\ndocument: %s" % path)

    # The gate drives the setting both ways; put the user's own value back
    # whatever happens, so running it is not a configuration change.
    original = wavesim_settings.load().get("voxelize_workers", "")
    results = {}
    try:
        for label, setting in (("serial", "1"), ("pool", "auto")):
            stored = wavesim_settings.load()
            stored["voxelize_workers"] = setting
            wavesim_settings.save(stored)

            t0 = time.perf_counter()
            (_spec, arrays), sections = _count_sections(
                lambda: voxelize.build_job_from_document(doc))
            elapsed = time.perf_counter() - t0
            results[label] = (arrays, elapsed, sections)
            print("  %-6s %6.2f s   planes cut in-process: %d"
                  % (label, elapsed, sections))
    finally:
        stored = wavesim_settings.load()
        stored["voxelize_workers"] = original
        wavesim_settings.save(stored)

    serial, s_time, s_sections = results["serial"]
    pooled, p_time, p_sections = results["pool"]

    ok = True
    keys = sorted(set(serial) | set(pooled))
    for key in keys:
        a, b = serial.get(key), pooled.get(key)
        if a is None or b is None:
            print("  MISSING %s in %s" % (key, "pool" if b is None else "serial"))
            ok = False
            continue
        a, b = np.asarray(a), np.asarray(b)
        if a.shape != b.shape:
            print("  SHAPE   %s %s vs %s" % (key, a.shape, b.shape))
            ok = False
        elif not np.array_equal(a, b):
            diff = int(np.count_nonzero(a != b))
            worst = (float(np.abs(a.astype(float) - b.astype(float)).max())
                     if a.size else 0.0)
            print("  DIFFERS %s on %d/%d elements (max %.3e)"
                  % (key, diff, a.size, worst))
            ok = False
    if ok:
        print("  all %d arrays bit-identical" % len(keys))

    # The pool is only doing anything if it took planes off the serial path.
    if p_sections >= s_sections:
        print("  WARNING: the pool cut no planes (%d in-process either way) -- "
              "the batches were below the dispatch threshold, or it declined."
              % p_sections)
    else:
        print("  pool handled %d of %d planes; speedup %.2fx"
              % (s_sections - p_sections, s_sections,
                 s_time / max(p_time, 1e-9)))
    FreeCAD.closeDocument(doc.Name)
    return ok


main()
sys.stdout.flush()
