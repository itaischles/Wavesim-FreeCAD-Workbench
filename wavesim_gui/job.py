# -*- coding: utf-8 -*-
"""Job serialisation for the Wavesim workbench bridge (FreeCAD side).

A "job" is a self-contained working directory the conda-side ``runner.py``
consumes: a ``job.json`` describing the simulation plus, from Session 3 onward,
a ``materials.npz`` of voxelised materials. The runner writes ``results.npz`` and
``summary.json`` back into the same directory, so a run's inputs and outputs live
together and persist for later inspection.

The working directory is **stable per document**: ``<results path>/<document
name>/run`` for a simulation. Re-running a document therefore overwrites its
previous results instead of piling up timestamped folders — callers must warn the
user first (see :func:`wavesim_gui.run.confirm_overwrite`). A TEM "Compute Mode"
preview is throwaway (its modes are re-solved by the next real run and saved
alongside the run's results), so it uses a :func:`temp_workdir` that
:func:`discard_workdir` deletes once the mode has been plotted — nothing of a
preview reaches the results path.

This module is FreeCAD-side and deliberately Qt-free and solver-free: it only
writes JSON, so it stays importable in console mode and never touches the
incompatible solver Python.
"""

import json
import os
import re
import shutil
import tempfile

import wavesim_settings


# Every file the runner or the workbench writes into a workdir. Cleared before a
# new run so a stale artefact (e.g. a materials.npz from a previous geometry, or
# the results of a run that this one is about to replace) can never be picked up
# as if it belonged to the new job.
JOB_ARTEFACTS = ("job.json", "materials.npz", "results.npz", "summary.json")

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def document_slug(doc):
    """Return a filesystem-safe folder name identifying *doc*.

    The saved ``.FCStd`` file's stem when there is one, else the document label
    (an unsaved document shares the ``Unnamed`` folder — saving it moves its
    results to a folder of their own).
    """
    name = ""
    if doc is not None:
        path = str(getattr(doc, "FileName", "") or "")
        if path:
            name = os.path.splitext(os.path.basename(path))[0]
        else:
            name = str(getattr(doc, "Label", "") or getattr(doc, "Name", "") or "")
    name = _UNSAFE_CHARS.sub("_", name).strip("._")
    return name or "Unnamed"


def workdir_for(doc, prefix="run"):
    """Return *doc*'s working directory for the *prefix* ('run'/'mode') stage.

    Pure path arithmetic — nothing is created or deleted, so callers can probe
    it with :func:`existing_artefacts` before asking the user to overwrite.
    """
    root = wavesim_settings.get_results_path()
    return os.path.join(root, document_slug(doc), prefix)


def existing_artefacts(workdir):
    """Return the :data:`JOB_ARTEFACTS` that already exist in *workdir*."""
    return [name for name in JOB_ARTEFACTS
            if os.path.isfile(os.path.join(workdir, name))]


def prepare_workdir(doc, prefix="run"):
    """Create *doc*'s working directory, clearing any previous job artefacts.

    Only the files this workbench writes are removed; anything else the user
    keeps in the folder is left alone. Returns the directory path.
    """
    workdir = workdir_for(doc, prefix)
    os.makedirs(workdir, exist_ok=True)
    for name in JOB_ARTEFACTS:
        path = os.path.join(workdir, name)
        if os.path.isfile(path):
            os.remove(path)
    return workdir


def temp_workdir():
    """Create a throwaway working directory for a preview job.

    Used by the TEM "Compute Mode" solve, whose output is only ever plotted: the
    modes it finds are re-solved (and saved) by the next real run, so writing
    them into the document's results path would be misleading clutter. The caller
    must pass the directory to :func:`discard_workdir` when done.
    """
    return tempfile.mkdtemp(prefix="wavesim_mode_")


def discard_workdir(workdir):
    """Delete a :func:`temp_workdir` and everything in it (never raises)."""
    shutil.rmtree(workdir, ignore_errors=True)


def write_job(workdir, job):
    """Serialise the *job* dict to ``<workdir>/job.json`` and return its path.

    The repo path is stamped into the job (from settings) unless the caller
    already supplied one, so the runner can put ``wavesim`` on ``sys.path``
    without depending on an environment variable being inherited.
    """
    job = dict(job)
    job.setdefault("wavesim_path", wavesim_settings.get_wavesim_path())
    # Path to the ngspice shared library for SPICE co-simulation ports (empty
    # when unset -> the solver falls back to PySpice's own library search).
    job.setdefault("ngspice_dll", wavesim_settings.get_ngspice_dll())
    # Solver backend ('auto' -> the runner picks the CUDA GPU when present, else
    # numba). Stamped here so every job route (real runs, mode solves, the demo)
    # honours the configured backend without each builder repeating it.
    job.setdefault("backend", wavesim_settings.get_backend())
    job_path = os.path.join(workdir, "job.json")
    with open(job_path, "w", encoding="utf-8") as handle:
        json.dump(job, handle, indent=2)
    return job_path


def build_demo_job(steps=800):
    """Return the hardcoded Session-2 vertical-slice job.

    A small vacuum box with CPML on all faces and one Gaussian-pulse point source
    at the centre, with an energy monitor. No real geometry yet — this exists to
    prove the bridge round-trip end to end.
    """
    Nx, Ny, Nz = 60, 60, 1
    dx = 1.0e-3  # 1 mm cells
    # Centre of the domain, in metres.
    cx = (Nx // 2) * dx
    cy = (Ny // 2) * dx
    return {
        # backend is stamped by write_job from settings (default 'auto').
        "steps": int(steps),
        "grid": {"Nx": Nx, "Ny": Ny, "Nz": Nz, "dx": dx, "dy": dx, "dz": dx},
        "boundary": {"d_pml": 10, "faces": ["x0", "x1", "y0", "y1"]},
        "source": {
            "component": "Ez",
            "x": cx, "y": cy, "z": 0.0,
            "fmax": 30.0e9,        # 30 GHz Gaussian pulse
            "amplitude": 1.0,
        },
        "monitors": {"energy": True},
    }
