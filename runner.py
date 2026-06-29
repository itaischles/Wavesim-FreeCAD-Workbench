# -*- coding: utf-8 -*-
"""Conda-side solver runner for the Wavesim workbench.

This script is the *other end* of the workbench bridge. It is executed by the
external conda Python interpreter (the one that can ``import wavesim``), **not**
by FreeCAD's bundled Python. FreeCAD serialises a job into a working directory
and spawns::

    <wavesim_python> runner.py <workdir>

The runner reads ``job.json`` (and an optional ``materials.npz`` of voxelised
material arrays, used from Session 3 onward), runs the FDTD solver, and writes
``results.npz`` + ``summary.json`` back into the same directory. While running it
prints ``PROGRESS n/N`` lines to stdout so the FreeCAD side can drive a progress
bar and cancel by killing the process.

The job/result contract is intentionally small and JSON-based so a future
persistent-worker server can reuse :func:`run_job` without re-spawning a fresh
interpreter (and re-paying the numba JIT warmup) on every run.

job.json schema (Session 2)
---------------------------
    {
      "wavesim_path": "<repo dir>",          # optional; else WAVESIM_PATH env
      "backend": "numba",                     # 'numba' | 'numpy'
      "steps": 1000,
      "grid":   {"Nx":.., "Ny":.., "Nz":.., "dx":.., "dy":.., "dz":..},
      "boundary": {"d_pml": 10, "faces": ["x0",...], "pec_faces": ["z0",...]} | null,
      "source": {"component":"Ez", "x":.., "y":.., "z":.., "fmax":.., "amplitude":..},
      "monitors": {
        "energy": true,
        "probes":    [{"name":.., "component":"Ez", "x":.., "y":.., "z":..}, ...],
        "snapshots": [{"name":.., "component":"Ez", "normal":"z",
                       "position":.., "every_N_steps":20}, ...]
      }
    }

results.npz holds the recorded monitor series (e.g. ``energy_times`` /
``energy_values``); summary.json holds scalar run metadata (dt, steps, wall
time, grid dims, voxel counts, final energy).
"""

import json
import os
import sys
import time


# --------------------------------------------------------------------------- #
# Job I/O helpers
# --------------------------------------------------------------------------- #

def _load_job(workdir):
    """Read and return the ``job.json`` dict from *workdir*."""
    with open(os.path.join(workdir, "job.json"), "r", encoding="utf-8") as handle:
        return json.load(handle)


def _ensure_wavesim_importable(job):
    """Put the Wavesim repo on ``sys.path`` so ``import wavesim`` resolves.

    Precedence: an explicit ``wavesim_path`` in the job, else the ``WAVESIM_PATH``
    environment variable. Either must point at the repo *containing* the
    ``wavesim`` package.
    """
    repo = job.get("wavesim_path") or os.environ.get("WAVESIM_PATH")
    if repo and os.path.isdir(repo) and repo not in sys.path:
        sys.path.insert(0, repo)


def _emit_progress(done, total):
    """Print a single ``PROGRESS done/total`` line for the FreeCAD side.

    Flushed immediately so QProcess sees each update as it happens rather than
    in one buffered burst at the end.
    """
    sys.stdout.write("PROGRESS {}/{}\n".format(done, total))
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Core — callable so a future persistent worker can reuse it
# --------------------------------------------------------------------------- #

def run_job(workdir):
    """Run the simulation described by ``<workdir>/job.json``.

    Writes ``results.npz`` and ``summary.json`` into *workdir* and returns the
    summary dict. Designed to be importable and called directly by a long-lived
    worker process (which amortises the numba JIT warmup across many jobs).
    """
    import numpy as np

    job = _load_job(workdir)
    _ensure_wavesim_importable(job)

    import wavesim as ws

    g = job["grid"]
    dx = float(g["dx"])
    dy = float(g.get("dy", dx))
    dz = float(g.get("dz", dx))
    grid = ws.create_grid(
        int(g["Nx"]), int(g["Ny"]), int(g["Nz"]), dx, dy, dz
    )
    grid = ws.set_vacuum(grid)

    # Optional voxelised materials (Session 3+). Absent in the Session 2 slice.
    materials_path = os.path.join(workdir, "materials.npz")
    voxel_summary = {}
    if os.path.isfile(materials_path):
        data = np.load(materials_path)
        pec_mask = data["pec_mask"] if "pec_mask" in data.files else None
        grid = ws.set_material_arrays(
            grid,
            data["eps_x"], data["eps_y"], data["eps_z"],
            data["mu_x"], data["mu_y"], data["mu_z"],
            pec_mask=pec_mask,
        )
        if pec_mask is not None:
            voxel_summary["pec_cells"] = int(np.count_nonzero(pec_mask))
        voxel_summary["dielectric_cells"] = int(np.count_nonzero(data["eps_x"] != 1.0))

    # Boundary: absorbing CPML on the PML faces, PEC walls on the PEC faces.
    # An explicit empty PML face list means a closed (PEC-cavity) domain, so
    # only fall back to all-six when the key is absent entirely.
    cpml = None
    boundary = job.get("boundary") or {}
    pml_faces = boundary.get("faces", list(ws.ALL_FACES))
    if pml_faces:
        cpml = ws.init_cpml(
            grid, d_pml=int(boundary.get("d_pml", 10)), faces=tuple(pml_faces)
        )
    pec_faces = tuple(boundary.get("pec_faces") or ())

    # Source: a single soft point excitation driven by a Gaussian pulse.
    s = job["source"]
    pulse = ws.GaussianPulse.for_fmax(
        float(s["fmax"]), amplitude=float(s.get("amplitude", 1.0))
    )
    source = ws.PointSource(
        s["component"], float(s["x"]), float(s["y"]), float(s["z"]), pulse
    )

    # Monitors. The energy monitor is whole-domain; probes and snapshots
    # (Session 7) are point/plane recorders described in the job. All locations
    # are already in the solver frame (origin baked into the voxel arrays).
    mon_cfg = job.get("monitors", {})
    energy = ws.EnergyMonitor() if mon_cfg.get("energy", True) else None

    probes = []  # (name, FieldProbe)
    for p in mon_cfg.get("probes", []):
        probes.append((
            p.get("name", "probe"),
            ws.FieldProbe(p["component"], float(p["x"]), float(p["y"]), float(p["z"])),
        ))

    snapshots = []  # (name, SnapshotMonitor)
    for s in mon_cfg.get("snapshots", []):
        snapshots.append((
            s.get("name", "snapshot"),
            ws.SnapshotMonitor(
                s["component"],
                float(s.get("position", s.get("at_z", 0.0))),
                max(1, int(s.get("every_N_steps", 20))),
                normal=s.get("normal", "z"),
            ),
        ))

    all_monitors = []
    if energy is not None:
        all_monitors.append(energy)
    all_monitors.extend(m for _name, m in probes)
    all_monitors.extend(m for _name, m in snapshots)

    sim = ws.Simulation(
        grid,
        cpml=cpml,
        sources=[source],
        monitors=all_monitors,
        pec_faces=pec_faces,
        backend=job.get("backend", "numba"),
    )

    n_steps = int(job["steps"])

    # Throttle progress output: an update per ~1% of the run plus the final step
    # is plenty for a smooth bar without flooding the pipe on long runs.
    progress_every = max(1, n_steps // 100)
    _emit_progress(0, n_steps)

    def callback(_sim, n):
        done = n + 1
        if done % progress_every == 0 or done == n_steps:
            _emit_progress(done, n_steps)

    t0 = time.perf_counter()
    sim.run(n_steps, callback=callback)
    wall_time = time.perf_counter() - t0

    # --- write results ---------------------------------------------------- #
    result_arrays = {}
    if energy is not None:
        result_arrays["energy_times"] = np.asarray(energy.times)
        result_arrays["energy_values"] = np.asarray(energy.values)

    # Probes: one time series each, keyed by index (names kept in the summary).
    probe_meta = []
    for idx, (name, mon) in enumerate(probes):
        result_arrays["probe_{}_times".format(idx)] = np.asarray(mon.times)
        result_arrays["probe_{}_values".format(idx)] = np.asarray(mon.values)
        probe_meta.append({"name": name, "component": mon.component})

    # Snapshots: a stack of frames (n_frames, Nx, Ny) plus their times each.
    snapshot_meta = []
    for idx, (name, mon) in enumerate(snapshots):
        if mon.snapshots:
            result_arrays["snapshot_{}_data".format(idx)] = np.asarray(mon.snapshots)
            result_arrays["snapshot_{}_times".format(idx)] = np.asarray(mon.snap_times)
        snapshot_meta.append({
            "name": name, "component": mon.component,
            "frames": len(mon.snapshots),
        })

    np.savez(os.path.join(workdir, "results.npz"), **result_arrays)

    summary = {
        "ok": True,
        "dt": float(grid.dt),
        "steps": n_steps,
        "wall_time_s": wall_time,
        "Nx": int(grid.Nx), "Ny": int(grid.Ny), "Nz": int(grid.Nz),
        "backend": sim.backend,
        "sim_time_s": float(grid.time_step * grid.dt),
        "pml_faces": list(pml_faces),
        "pec_faces": list(pec_faces),
    }
    summary.update(voxel_summary)
    if energy is not None and energy.values:
        summary["energy_final"] = float(energy.values[-1])
        summary["energy_max"] = float(max(energy.values))
    if probe_meta:
        summary["probes"] = probe_meta
    if snapshot_meta:
        summary["snapshots"] = snapshot_meta
    with open(os.path.join(workdir, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)

    return summary


# --------------------------------------------------------------------------- #
# CLI entry point
# --------------------------------------------------------------------------- #

def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: runner.py <workdir>\n")
        return 2

    workdir = argv[1]
    try:
        summary = run_job(workdir)
    except Exception as exc:  # report the failure into the workdir, then exit non-zero
        import traceback
        message = "{}: {}".format(type(exc).__name__, exc)
        sys.stderr.write(message + "\n")
        traceback.print_exc()
        try:
            with open(os.path.join(workdir, "summary.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({"ok": False, "error": message}, handle, indent=2)
        except Exception:
            pass
        return 1

    sys.stdout.write(
        "DONE steps={steps} dt={dt:.3e}s wall={wall_time_s:.2f}s\n".format(**summary)
    )
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
