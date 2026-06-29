# -*- coding: utf-8 -*-
"""Job serialisation for the Wavesim workbench bridge (FreeCAD side).

A "job" is a self-contained working directory the conda-side ``runner.py``
consumes: a ``job.json`` describing the simulation plus, from Session 3 onward,
a ``materials.npz`` of voxelised materials. The runner writes ``results.npz`` and
``summary.json`` back into the same directory, so keeping each run in its own
timestamped folder under the configured results path means a run's inputs and
outputs live together and persist for later inspection.

This module is FreeCAD-side and deliberately Qt-free and solver-free: it only
writes JSON, so it stays importable in console mode and never touches the
incompatible solver Python.
"""

import datetime
import json
import os

import wavesim_settings


def new_workdir(prefix="run"):
    """Create and return a fresh timestamped working directory.

    Lives under the configured results folder (created if missing). The
    timestamp includes microseconds so two runs started in the same second do
    not collide.
    """
    root = wavesim_settings.get_results_path()
    os.makedirs(root, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    workdir = os.path.join(root, "{}_{}".format(prefix, stamp))
    os.makedirs(workdir, exist_ok=True)
    return workdir


def write_job(workdir, job):
    """Serialise the *job* dict to ``<workdir>/job.json`` and return its path.

    The repo path is stamped into the job (from settings) unless the caller
    already supplied one, so the runner can put ``wavesim`` on ``sys.path``
    without depending on an environment variable being inherited.
    """
    job = dict(job)
    job.setdefault("wavesim_path", wavesim_settings.get_wavesim_path())
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
        "backend": "numba",
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
