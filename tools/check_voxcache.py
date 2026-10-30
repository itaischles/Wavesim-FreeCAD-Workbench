# -*- coding: utf-8 -*-
"""Gate: a reused voxelisation is the one a fresh sweep would produce, or none.

:mod:`wavesim_gui.voxcache` hands a run its document's previous voxelisation
when the fingerprint of everything the voxeliser reads comes out unchanged.
The danger is all on one side: a miss costs a sweep, but a **stale hit** runs
the solver on a model the user has since edited, and nothing downstream can
tell. So this gate checks both directions on a synthetic document:

* **Hits** where nothing the voxeliser reads has changed -- an immediate re-run,
  a recompute of untouched bodies, a different run time, a closed and reopened
  document, and the other solver mode's slot coming back after a switch. Every
  hit is compared bit for bit, arrays and job spec alike, against a fresh
  ``reuse=False`` sweep of the same document.
* **Misses** for each kind of change that must re-voxelise -- a body moved by
  10 um, a material's permittivity, subpixel smoothing, a domain spacing, the
  voxeliser's own code -- and always with the setting off.
* ``materials.npz`` written from a hit is the cache file, byte for byte.

Run under ``freecadcmd`` (FreeCAD's Python; needs Part + the bundled numpy)::

    "%LOCALAPPDATA%\\Programs\\FreeCAD 1.1\\bin\\freecadcmd.exe" \\
        tools/check_voxcache.py

Everything is written under a temporary folder, which is removed afterwards;
the user's results path is never touched. ``freecadcmd`` swallows ``stdout`` and
its exit status, so read the PASS/FAIL line.
"""

import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import FreeCAD  # noqa: E402
import Part  # noqa: E402

try:  # freecadcmd supplies a stub FreeCADGui that the gui modules probe for
    import FreeCADGui  # noqa: E402

    if not hasattr(FreeCADGui, "addCommand"):
        FreeCADGui.addCommand = lambda *a, **k: None
except ImportError:
    pass

import numpy as np  # noqa: E402

import wavesim_settings  # noqa: E402
from wavesim_gui import commands as cmd  # noqa: E402
from wavesim_gui import domain as domain_mod  # noqa: E402
from wavesim_gui import materials as mat_mod  # noqa: E402
from wavesim_gui import voxcache  # noqa: E402
from wavesim_gui import voxelize as vox_mod  # noqa: E402

_failures = []
_sweeps = [0]


def check(ok, label, detail=""):
    print("  [%s] %-50s %s" % ("ok" if ok else "FAIL", label, detail))
    if not ok:
        _failures.append(label)
    return ok


def _counting_sweeps():
    original = vox_mod.voxelize_materials

    def counting(*args, **kwargs):
        _sweeps[0] += 1
        return original(*args, **kwargs)

    vox_mod.voxelize_materials = counting
    return lambda: setattr(vox_mod, "voxelize_materials", original)


def _build(path):
    """A saved simulation document: a PEC rod through a dielectric block."""
    doc = FreeCAD.newDocument("voxcachegate")
    sim = doc.addObject("App::DocumentObjectGroupPython", "Simulation")
    cmd.SimulationContainer(sim)
    dom = domain_mod.create_domain(doc, sim)
    _vacuum, pec = mat_mod.create_default_materials(doc, sim)
    rod = doc.addObject("Part::Feature", "Rod")
    rod.Shape = Part.makeCylinder(1.3, 12.0, FreeCAD.Vector(0.2, 0.1, -6.0))
    block = doc.addObject("Part::Feature", "Block")
    block.Shape = Part.makeBox(8, 6, 4, FreeCAD.Vector(-4, -3, -2))
    pec.Bodies = [block, rod]
    sim.MaxFrequency = 20.0e9
    sim.ConformalPEC = True
    # Clear of the absorber, so the PML-shell warning stays out of the report.
    for _face, prop, _doc in domain_mod._SPACING_PROPS:
        setattr(dom, prop, "5 mm")
    doc.recompute()
    # Saved first: the cache folder is named after the file, and a gate that
    # reopens the document needs the same folder before and after.
    doc.saveAs(path)
    return doc


def _sim_parts(doc):
    sim = cmd.active_simulation(doc)
    mats = mat_mod.find_materials(sim)
    pec = [m for m in mats if getattr(m, "Pec", False)][0]
    return sim, domain_mod.find_domain(sim), pec


def _run(doc):
    """``build_job_from_document`` through the cache; (spec, arrays, swept?)."""
    before = _sweeps[0]
    spec, arrays = vox_mod.build_job_from_document(doc)
    return spec, arrays, _sweeps[0] > before


def _same(doc, spec, arrays, label):
    """*spec*/*arrays* must equal a fresh sweep of *doc*, to the bit."""
    fresh_spec, fresh = vox_mod.build_job_from_document(doc, reuse=False)
    ok = sorted(arrays) == sorted(fresh)
    for key in fresh:
        if key in arrays and not np.array_equal(arrays[key], fresh[key]):
            ok = False
    ok = ok and (json.dumps(spec, sort_keys=True, default=repr)
                 == json.dumps(fresh_spec, sort_keys=True, default=repr))
    return check(ok, label + ": identical to a fresh sweep")


def _expect(doc, hit, label):
    spec, arrays, swept = _run(doc)
    check(swept != hit, label, "hit" if not swept else "voxelised")
    if hit and not swept:
        _same(doc, spec, arrays, label)
    return spec, arrays


def main():
    tmp = tempfile.mkdtemp(prefix="wavesim_voxcache_")
    original_results = wavesim_settings.get_results_path
    wavesim_settings.get_results_path = lambda: os.path.join(tmp, "results")
    restore = _counting_sweeps()
    path = os.path.join(tmp, "voxcachegate.FCStd")
    try:
        doc = _build(path)
        sim, dom, pec = _sim_parts(doc)
        print("cache folder: %s" % voxcache.cache_dir_for(doc))

        print("\nhits")
        _expect(doc, False, "first run voxelises")
        _spec, arrays = _expect(doc, True, "immediate re-run")

        workdir = os.path.join(tmp, "work")
        os.makedirs(workdir)
        vox_mod.write_materials(workdir, arrays)
        with open(os.path.join(workdir, "materials.npz"), "rb") as a, \
                open(arrays.npz_path, "rb") as b:
            check(a.read() == b.read(), "materials.npz is the cache file")

        for body in pec.Bodies:
            body.touch()
        doc.recompute()
        _expect(doc, True, "bodies recomputed, unchanged")

        sim.MaxTime = 2.0 * float(getattr(sim, "MaxTime", 1.0e-9) or 1.0e-9)
        doc.recompute()
        spec, _arrays = _expect(doc, True, "run time changed")
        check(spec["steps"] != _spec["steps"], "  ...and the job's steps did")

        doc.save()
        FreeCAD.closeDocument(doc.Name)
        doc = FreeCAD.openDocument(path)
        sim, dom, pec = _sim_parts(doc)
        _expect(doc, True, "document closed and reopened")

        print("\nmisses")
        rod = doc.getObject("Rod")
        rod.Placement.Base = rod.Placement.Base + FreeCAD.Vector(0.01, 0, 0)
        doc.recompute()
        _expect(doc, False, "a body moved by 10 um")

        pec.Pec = False
        pec.Eps = 4.0
        doc.recompute()
        _expect(doc, False, "material changed (PEC -> eps 4)")
        pec.Eps = 4.5
        doc.recompute()
        _expect(doc, False, "permittivity changed")

        sim.SubpixelSmoothing = not sim.SubpixelSmoothing
        doc.recompute()
        _expect(doc, False, "subpixel smoothing toggled")

        prop = domain_mod._SPACING_PROPS[0][1]
        setattr(dom, prop, "%g mm" % (getattr(dom, prop).Value + 1.0))
        doc.recompute()
        _expect(doc, False, "a domain spacing changed")

        voxcache._CODE_HASH = "a different voxeliser"
        _expect(doc, False, "voxeliser code changed")
        voxcache._CODE_HASH = None
        _expect(doc, False, "  ...and changed back")

        enabled = voxcache.enabled
        voxcache.enabled = lambda: False
        try:
            _expect(doc, False, "setting off: voxelises")
            _expect(doc, False, "setting off: voxelises again")
        finally:
            voxcache.enabled = enabled

        print("\nslots")
        pec.Pec = True
        doc.recompute()
        _expect(doc, False, "full-wave model reset")
        try:
            sim.SolverMode = cmd.MODE_LABELS[1]
            doc.recompute()
            _expect(doc, False, "electrostatic run voxelises")
            sim.SolverMode = cmd.MODE_LABELS[0]
            doc.recompute()
            _expect(doc, True, "back to full wave: its slot kept")
            sim.SolverMode = cmd.MODE_LABELS[1]
            doc.recompute()
            _expect(doc, True, "back to electrostatic: its slot kept")
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            check(False, "electrostatic slot", repr(exc))
        FreeCAD.closeDocument(doc.Name)
    finally:
        restore()
        wavesim_settings.get_results_path = original_results
        shutil.rmtree(tmp, ignore_errors=True)

    print("\ncheck_voxcache: %s" % ("FAIL: " + "; ".join(_failures)
                                    if _failures else "PASS"))


main()
sys.stdout.flush()
