# -*- coding: utf-8 -*-
"""Scripting API: run plumbing -- export a job, load finished results.

The agent never runs the solver inside FreeCAD. :func:`export_job` voxelizes
the document and writes ``job.json`` + ``materials.npz`` into an explicit
working directory (the project's run folder); an external process then runs
``runner.py`` on it, and :func:`load_results` brings the finished results into
the document tree exactly like *Run Simulation* does after its dialog.
"""

import json
import os

import FreeCAD

from wavesim_gui import api_common as c
from wavesim_gui.api_common import ApiError, api_function

__all__ = ["export_job", "load_results", "runner_path"]


@api_function("run")
def runner_path():
    """Absolute path of the workbench's ``runner.py`` (and ``exporter.py``
    directory), so an external launcher uses the matching solver bridge."""
    from wavesim_gui import run as run_mod
    folder = os.path.dirname(run_mod.RUNNER_PATH)
    return {"runner": run_mod.RUNNER_PATH,
            "exporter": os.path.join(folder, "exporter.py"),
            "workbench": folder}


def _clean_workdir(workdir):
    from wavesim_gui import job as job_mod
    os.makedirs(workdir, exist_ok=True)
    for name in job_mod.JOB_ARTEFACTS:
        path = os.path.join(workdir, name)
        if os.path.isfile(path):
            os.remove(path)


@api_function("run")
def export_job(doc=None, workdir=None, steps=None, backend=None,
               wavesim_path=None, progress=None):
    """Voxelize the document and write ``job.json`` + ``materials.npz``.

    Non-modal: no dialog, progress goes to *progress(done, total)* (the Agent
    bridge reports it). Previous job artefacts in *workdir* are removed first;
    other files are left alone. The GUI's own results folder is not touched.

    :param workdir: explicit output folder (created if needed). Required.
    :param steps: FDTD time steps; default derives them from the
        Simulation's max time and the CFL step (like the GUI Run).
    :param backend: ``'auto'|'numba'|'numpy'`` to stamp into the job
        (default: the workbench setting).
    :param wavesim_path: Wavesim repo path to stamp (default: setting).
    :returns: ``{"workdir", "job", "materials", "mode", "steps", "grid":
        {"Nx","Ny","Nz"}, "backend"}``.
    """
    from wavesim_gui import commands
    from wavesim_gui import job as job_mod
    from wavesim_gui import voxelize as vox_mod

    doc = c.get_doc(doc)
    sim = c.get_sim(doc)
    if not workdir:
        raise ApiError("export_job needs an explicit workdir.")
    if steps is not None:
        steps = int(steps)
        if steps <= 0:
            raise ApiError("steps must be a positive integer.")
    if backend is not None and backend not in ("auto", "numba", "numpy"):
        raise ApiError("backend must be 'auto', 'numba' or 'numpy'.")
    doc.recompute()
    workdir = os.path.abspath(workdir)
    with c.capture_warnings() as warnings:
        try:
            spec, arrays = vox_mod.build_job_from_document(
                doc, steps=steps, progress=progress)
        except vox_mod.VoxelizationCancelled:
            raise ApiError("Job export was cancelled.")
        except vox_mod.GridRequiredError as exc:
            raise ApiError(str(exc))
    if spec is None:
        raise ApiError("Nothing to simulate: assign at least one body to a "
                       "material first.")
    _clean_workdir(workdir)
    if backend is not None:
        spec["backend"] = backend
    if wavesim_path:
        spec["wavesim_path"] = wavesim_path
    job_path = job_mod.write_job(workdir, spec)
    mat_path = None
    if arrays is not None:
        vox_mod.write_materials(workdir, arrays)
        mat_path = os.path.join(workdir, "materials.npz")
    with open(job_path, "r", encoding="utf-8") as handle:
        written = json.load(handle)
    grid = written.get("grid", {})
    return {
        "workdir": workdir, "job": job_path, "materials": mat_path,
        "mode": written.get("mode", commands.MODE_FDTD),
        "steps": written.get("steps"),
        "grid": {k: grid.get(k) for k in ("Nx", "Ny", "Nz")},
        "backend": written.get("backend"),
        "warnings": [w for w in warnings if w],
    }


@api_function("run")
def load_results(doc=None, workdir=None):
    """Load a finished run's results into the document tree.

    Rebuilds the Simulation's *Results* group from ``<workdir>/results.npz``
    and ``summary.json`` (as *Run Simulation* does when its run finishes), so
    the user can double-click result nodes to plot them.

    :param workdir: the run folder holding results.npz + summary.json.
    :returns: ``{"results_group", "leaves": n, "summary": {...ok, steps,
        wall_time_s, Nx, Ny, Nz}}``.
    """
    from wavesim_gui import results as results_mod

    doc = c.get_doc(doc)
    sim = c.get_sim(doc)
    if not workdir or not os.path.isdir(workdir):
        raise ApiError("Run folder {!r} does not exist.".format(workdir))
    summary_path = os.path.join(workdir, "summary.json")
    try:
        with open(summary_path, "r", encoding="utf-8") as handle:
            summary = json.load(handle)
    except FileNotFoundError:
        raise ApiError("No summary.json in {} -- the run has not finished."
                       .format(workdir))
    if not summary.get("ok", True):
        raise ApiError("The run failed: {}".format(summary.get("error")))
    if not os.path.isfile(os.path.join(workdir, "results.npz")):
        raise ApiError("No results.npz in {}.".format(workdir))
    grp = results_mod.build_results(doc, sim, workdir, summary)
    if grp is None:
        raise ApiError("Results could not be loaded (see the FreeCAD report "
                       "view).")
    leaves = 0
    stack = list(grp.Group)
    while stack:
        obj = stack.pop()
        if hasattr(obj, "Group"):
            stack.extend(obj.Group)
        else:
            leaves += 1
    brief = {k: summary.get(k) for k in ("ok", "steps", "dt", "wall_time_s",
                                         "Nx", "Ny", "Nz", "backend")}
    return {"results_group": grp.Label, "leaves": leaves, "summary": brief}
