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
bar and cancel by killing the process, plus ``STATUS <text>`` lines for the
coarse non-numeric stages (loading the solver, factorising a TEM plane) so a
long-running step does not look like a frozen GUI.

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
      "source": {"component":"Ez", "x":.., "y":.., "z":..,
                 "excitation": {"type":"gaussian"|"sine"|"rectangular"|
                                "gaussian_sine", ...params (SI)...}} | null,
                 # legacy jobs may instead carry flat "fmax"/"amplitude" keys
                 # (a Gaussian pulse); see _build_waveform for the param set.
      "tem_sources": [{"name":.., "normal":"z", "position":..,
                       "direction": 1.0|-1.0,   # +/-normal launch (into domain)
                       "conductor_id": 0,       # which solved mode to launch:
                                                # a conductor label (see summary
                                                # "modes"), 0/absent = dominant
                       "excitation": {"type":.., ...}, "fields":"EH"|"E"}, ...],
                       # legacy entries may carry flat "fmax"/"amplitude" keys
                       # and omit "direction" (defaults to +normal / low face)
      "mode_only": false,                     # solve TEM modes only; no FDTD run
      "monitors": {
        "energy": true,
        "probes":    [{"name":.., "component":"Ez", "x":.., "y":.., "z":..}, ...],
        "snapshots": [{"name":.., "component":"Ez", "normal":"z",
                       "position":.., "every_N_steps":20}, ...],
        "voltages":  [{"name":.., "path": [[x,y,z], ...]}, ...],
        "currents":  [{"name":.., "path": [[x,y,z], ...]}, ...]
      }
    }

results.npz holds the recorded monitor series (e.g. ``energy_times`` /
``energy_values``); summary.json holds scalar run metadata (dt, steps, wall
time, grid dims, voxel counts, final energy).

TEM ports (Session 9)
---------------------
Each ``tem_sources`` entry names a grid plane (the ``normal`` axis and the
``position`` of the plane along it, in the solver frame). The runner calls
:func:`wavesim.mode_solver.solve_tem_modes` on that plane to find the TEM mode of
the PEC cross-section, launches it as a directional ``PlaneSource`` (built via
:meth:`TEMMode.to_source`) during the FDTD run, and saves each solved mode's 2D
field profiles into ``results.npz`` (keys ``mode_<si>_<mi>_phi`` / ``_pec`` /
``_E_<comp>``) with its per-unit-length parameters under ``summary["modes"]``.
With ``mode_only`` true the runner solves and saves the modes and skips the FDTD
time-stepping entirely (used by the workbench's "Compute Mode" button).
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


def _emit_status(message):
    """Print a ``STATUS <text>`` line for the FreeCAD side to show to the user.

    Used for the coarse, non-numeric stages (loading the solver, factorising a
    TEM plane, ...) where there is no step count to drive a progress bar but the
    work can still take long enough that the GUI looks frozen without feedback.
    Flushed immediately so each stage appears as it happens. Any embedded
    newlines are escaped so the whole message stays on one stdout line (the
    FreeCAD side splits stdout on newlines); it un-escapes them for display.
    """
    sys.stdout.write("STATUS {}\n".format(message.replace("\n", "\\n")))
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Excitation waveforms — build the point source's temporal profile
# --------------------------------------------------------------------------- #

def _build_waveform(ws, s):
    """Build the solver temporal waveform ``f(t)`` for a point-source spec *s*.

    Reads the ``excitation`` sub-dict (the job.json contract shared with the
    workbench's :mod:`wavesim_gui.excitation`). The maths is duplicated here on
    purpose rather than importing workbench code, so the solver side stays free
    to grow its own native waveform classes. Any callable ``f(t) -> float`` is a
    valid waveform (see ``wavesim.sources``). Falls back to the legacy flat
    ``fmax``/``amplitude`` Gaussian for jobs written before excitation types.
    """
    import math

    exc = s.get("excitation")
    if not exc:
        return ws.GaussianPulse.for_fmax(
            float(s["fmax"]), amplitude=float(s.get("amplitude", 1.0))
        )

    typ = exc.get("type", "gaussian")
    amp = float(exc.get("amplitude", 1.0))

    if typ == "gaussian":
        # Reuse the solver's own pulse so a plain Gaussian stays identical to
        # earlier runs (width = 1/(2*pi*fmax), t0 = 4*width).
        return ws.GaussianPulse.for_fmax(
            float(exc.get("fmax", 30.0e9)), amplitude=amp
        )

    if typ == "sine":
        freq = float(exc.get("frequency", 30.0e9))
        phase = math.radians(float(exc.get("phase_deg", 0.0)))
        return lambda t: amp * math.sin(2.0 * math.pi * freq * t + phase)

    if typ == "rectangular":
        start = float(exc.get("start_time", 0.0))
        rise = float(exc.get("rise_time", 0.0))
        flat = float(exc.get("flat_time", 0.0))
        fall = float(exc.get("fall_time", 0.0))
        end = start + rise + flat + fall

        def rect(t):
            up = (1.0 if t >= start else 0.0) if rise <= 0.0 \
                else min(max((t - start) / rise, 0.0), 1.0)
            down = (1.0 if t <= end else 0.0) if fall <= 0.0 \
                else min(max((end - t) / fall, 0.0), 1.0)
            return amp * min(up, down)

        return rect

    if typ == "gaussian_sine":
        fmax = max(float(exc.get("fmax", 10.0e9)), 1.0e-30)
        width = 1.0 / (2.0 * math.pi * fmax)
        t0 = 4.0 * width
        freq = float(exc.get("frequency", 30.0e9))
        phase = math.radians(float(exc.get("phase_deg", 0.0)))
        return lambda t: (amp
                          * math.exp(-0.5 * ((t - t0) / width) ** 2)
                          * math.sin(2.0 * math.pi * freq * (t - t0) + phase))

    # Unknown type: a unit Gaussian rather than failing the whole run.
    return ws.GaussianPulse.for_fmax(
        float(exc.get("fmax", 30.0e9)), amplitude=amp
    )


# --------------------------------------------------------------------------- #
# TEM ports — solve each plane's transverse-static mode (Session 9)
# --------------------------------------------------------------------------- #

def _f(value):
    """Coerce a possibly-``None`` solver parameter to a JSON-friendly float."""
    return None if value is None else float(value)


def _solve_tem_modes(ws, np, grid, job):
    """Solve the TEM modes of every ``tem_sources`` plane on *grid*.

    Returns ``(plane_sources, mode_arrays, mode_meta)``:

    * ``plane_sources`` — directional :class:`PlaneSource` launchers (one per
      port, built from the port's dominant mode) to add to the FDTD run; empty
      when ``mode_only`` is set.
    * ``mode_arrays`` — the 2D field profiles to drop into ``results.npz``
      (``mode_<si>_<mi>_phi`` / ``_pec`` / ``_E_<comp>``).
    * ``mode_meta`` — per-mode metadata (identity + per-unit-length parameters)
      for ``summary["modes"]`` and the workbench results tree.
    """
    tem_cfg = job.get("tem_sources") or []
    mode_only = bool(job.get("mode_only", False))
    plane_sources = []
    mode_arrays = {}
    mode_meta = []

    n_ports = len(tem_cfg)
    for si, t in enumerate(tem_cfg):
        normal = t.get("normal", "z")
        position = float(t.get("position", 0.0))
        fields = t.get("fields", "EH")
        name = t.get("name", "TEM")

        # A single characteristic frequency + amplitude for the results tree
        # (the launch waveform itself is built by _build_waveform below). Reads
        # the excitation spec, falling back to legacy flat fmax/amplitude keys.
        exc_spec = t.get("excitation") or {}
        etype = exc_spec.get("type", "gaussian")
        amplitude = float(exc_spec.get("amplitude", t.get("amplitude", 1.0)))
        if etype in ("sine", "gaussian_sine"):
            fmax = float(exc_spec.get("frequency", 0.0))
        else:  # gaussian (or legacy) uses fmax; rectangular has none
            fmax = float(exc_spec.get("fmax", t.get("fmax", 0.0)))

        prefix = "Port {}/{}: ".format(si + 1, n_ports) if n_ports > 1 else ""
        _emit_status(
            "{}solving TEM mode on the {}-plane of '{}'\n"
            "(factorising the {}x{}x{} cross-section; this scales with grid "
            "size)...".format(
                prefix, normal, name, grid.Nx, grid.Ny, grid.Nz
            )
        )
        modes = ws.solve_tem_modes(
            grid, normal=normal, position=position, compute_params=True
        )
        _emit_status(
            "{}found {} TEM mode(s); building field profiles...".format(
                prefix, len(modes)
            )
        )

        for mi, mode in enumerate(modes):
            key = "mode_{}_{}".format(si, mi)
            mode_arrays[key + "_phi"] = np.asarray(mode.phi, dtype=np.float64)
            mode_arrays[key + "_pec"] = np.asarray(mode.pec, dtype=np.uint8)
            for comp, arr in mode.E.items():
                mode_arrays["{}_E_{}".format(key, comp)] = np.asarray(
                    arr, dtype=np.float64
                )
            mode_meta.append({
                "source_index": si, "mode_index": mi, "name": name,
                "conductor_id": int(mode.conductor_id),
                "normal": mode.normal, "position": float(mode.position),
                "transverse_axes": list(mode.transverse_axes),
                "da": float(mode.da), "db": float(mode.db),
                "Ecomps": list(mode.E.keys()),
                "impedance": _f(mode.impedance), "eps_eff": _f(mode.eps_eff),
                "capacitance": _f(mode.capacitance),
                "inductance": _f(mode.inductance),
                "v_phase": _f(mode.v_phase),
                "fmax": fmax, "amplitude": amplitude, "fields": fields,
            })

        # Launch one mode as a directional plane source. By default that is the
        # dominant (first) mode; a non-zero ``conductor_id`` selects the mode whose
        # energized conductor carries that label (as shown in summary["modes"] /
        # the results tree), falling back to the dominant mode with a warning if no
        # such mode was found. The mode is normalised to a 1 V drive, so the
        # temporal waveform carries the amplitude and ``to_source`` is left at unit
        # scale. The waveform is built from the port's excitation spec (same
        # builder as the point source).
        if modes and not mode_only:
            wanted = int(t.get("conductor_id", 0))
            chosen = modes[0]
            if wanted > 0:
                match = next(
                    (m for m in modes if m.conductor_id == wanted), None
                )
                if match is None:
                    sys.stderr.write(
                        "wavesim: TEM port '{}' requested conductor {} but only "
                        "conductors {} were solved; launching conductor {} "
                        "instead.\n".format(
                            name, wanted,
                            [m.conductor_id for m in modes],
                            modes[0].conductor_id,
                        )
                    )
                else:
                    chosen = match
            waveform = _build_waveform(ws, t)
            src = chosen.to_source(waveform, amplitude=1.0, fields=fields)
            # ``to_source`` builds H = (n̂ × E)/η for +normal propagation, so the
            # wave always flows toward +normal. A port on a high face launches
            # *into* the domain along -normal (direction < 0): flip H to reverse
            # the Poynting vector S = E × H (E-only launches are bidirectional,
            # so the sign is moot there and there is no H to flip).
            direction = float(t.get("direction", 1.0))
            if direction < 0 and getattr(src, "profiles", None):
                for comp in list(src.profiles):
                    if comp.startswith("H"):
                        src.profiles[comp] = -src.profiles[comp]
            plane_sources.append(src)

    return plane_sources, mode_arrays, mode_meta


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

    # Importing the solver pulls in numba/scipy and, on a cold interpreter, can
    # take several seconds — tell the user so the GUI does not look hung.
    _emit_status("Loading solver (first run may compile, please wait)...")
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

    # TEM ports: solve each port plane's transverse mode. Done before the FDTD
    # setup so the solved modes can be launched as directional plane sources
    # (and so a mode-only request can return without building the time loop).
    plane_sources, mode_arrays, mode_meta = _solve_tem_modes(ws, np, grid, job)

    if job.get("mode_only", False):
        _emit_status("Saving mode results...")
        np.savez(os.path.join(workdir, "results.npz"), **mode_arrays)
        summary = {
            "ok": True, "mode_only": True,
            "dt": float(grid.dt), "steps": 0, "wall_time_s": 0.0,
            "Nx": int(grid.Nx), "Ny": int(grid.Ny), "Nz": int(grid.Nz),
        }
        summary.update(voxel_summary)
        if mode_meta:
            summary["modes"] = mode_meta
        with open(os.path.join(workdir, "summary.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)
        _emit_progress(1, 1)
        return summary

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

    # Sources: an optional soft point excitation plus any TEM port launchers.
    # ``source`` may be null when the excitation comes entirely from TEM ports.
    sources = []
    s = job.get("source")
    if s:
        waveform = _build_waveform(ws, s)
        sources.append(ws.PointSource(
            s["component"], float(s["x"]), float(s["y"]), float(s["z"]), waveform
        ))
    sources.extend(plane_sources)

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

    # Line-integral monitors: V = int E.dl / I = loop-int H.dl along a polyline
    # of solver-frame vertices (discretised from a sketch on the FreeCAD side).
    voltages = []  # (name, VoltageMonitor)
    for v in mon_cfg.get("voltages", []):
        voltages.append((v.get("name", "voltage"), ws.VoltageMonitor(v["path"])))

    currents = []  # (name, CurrentMonitor)
    for c in mon_cfg.get("currents", []):
        currents.append((c.get("name", "current"), ws.CurrentMonitor(c["path"])))

    all_monitors = []
    if energy is not None:
        all_monitors.append(energy)
    all_monitors.extend(m for _name, m in probes)
    all_monitors.extend(m for _name, m in snapshots)
    all_monitors.extend(m for _name, m in voltages)
    all_monitors.extend(m for _name, m in currents)

    sim = ws.Simulation(
        grid,
        cpml=cpml,
        sources=sources,
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

    # Replace the last setup STATUS (solver load / TEM mode build) so the dialog
    # label reflects what is actually happening while the bar advances.
    _emit_status("Running FDTD simulation ({} time steps)...".format(n_steps))
    t0 = time.perf_counter()
    sim.run(n_steps, callback=callback)
    wall_time = time.perf_counter() - t0

    # --- write results ---------------------------------------------------- #
    # Seed with the solved TEM-mode profiles so they ride along in the same
    # results.npz the monitors write into.
    result_arrays = dict(mode_arrays)
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

    # Voltage/current line integrals: one time series each, keyed by index.
    voltage_meta = []
    for idx, (name, mon) in enumerate(voltages):
        result_arrays["voltage_{}_times".format(idx)] = np.asarray(mon.times)
        result_arrays["voltage_{}_values".format(idx)] = np.asarray(mon.values)
        voltage_meta.append({"name": name})

    current_meta = []
    for idx, (name, mon) in enumerate(currents):
        result_arrays["current_{}_times".format(idx)] = np.asarray(mon.times)
        result_arrays["current_{}_values".format(idx)] = np.asarray(mon.values)
        current_meta.append({"name": name})

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
    if voltage_meta:
        summary["voltages"] = voltage_meta
    if current_meta:
        summary["currents"] = current_meta
    if mode_meta:
        summary["modes"] = mode_meta
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
