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
      "backend": "auto",                      # 'auto'|'cuda'|'numba'|'numpy'
                                              # 'auto' -> 'cuda' when a CUDA GPU
                                              # is present, else 'numba' (see
                                              # _resolve_backend). The GPU path
                                              # allocates the grid as float32.
      "steps": 1000,
      "grid":   {"Nx":.., "Ny":.., "Nz":.., "dx":.., "dy":.., "dz":..,
                 "x":[..], "y":[..], "z":[..]},  # optional node-coordinate
                 # arrays (metres, solver frame, strictly increasing, N+1 nodes
                 # per axis). When present the runner builds a non-uniform
                 # rectilinear grid via create_grid_rectilinear (dx/dy/dz then
                 # carry the *minimum* spacing per axis); absent -> uniform
                 # create_grid. The workbench sends them only for a genuinely
                 # graded grid: create_grid_rectilinear derives spacings via
                 # diff(coords), which rounds ~1 ULP off a uniform tick, so a
                 # uniform run stays on create_grid to keep dt/results exact.
      "boundary": {"d_pml": 10, "faces": ["x0",...], "pec_faces": ["z0",...]} | null,
      "source": {"component":"Ez", "x":.., "y":.., "z":..,
                 "excitation": {"type":"gaussian"|"sine"|"sinusoid"|
                                "rectangular"|"gaussian_sine",
                                ...params (SI)...}} | null,
                 # legacy jobs may instead carry flat "fmax"/"amplitude" keys
                 # (a Gaussian pulse); see _build_waveform for the param set.
                 # "sinusoid" is the ramped CW drive (raised-cosine turn-on over
                 # "ramp_cycles" periods), built from the solver's Sinusoid.
      "tem_sources": [{"name":.., "normal":"z", "position":..,
                       "direction": 1.0|-1.0,   # +/-normal launch (into domain)
                       "conductor_id": 0,       # which solved mode to launch:
                                                # a conductor label (see summary
                                                # "modes"), 0/absent = dominant
                       "bounds": [a0,a1,b0,b1], # optional in-plane subset (solver
                                                # metres, transverse slice order);
                                                # absent = the whole face
                       "mode_mesh": {           # optional connectivity-preserving
                         "key":"modemesh_0",    # fine transverse re-voxelisation
                         "normal":"z","position":.., # of this plane; arrays live in
                         "a_nodes":[..],"b_nodes":[..]}, # materials.npz (see below).
                                                # Absent = solve on the coarse slice.
                                                # A *convergence* mode mesh instead
                                                # carries "levels":[{"a_nodes","b_nodes"},
                                                # ...] (finest last) + "convergence":
                                                # {"max_iter","rel_tol"}: the runner
                                                # walks the ladder, re-solving Z0 until
                                                # it settles (arrays <key>_lvl<j>_*)
                       "excitation": {"type":.., ...}, "fields":"EH"|"E"}, ...],
                       # legacy entries may carry flat "fmax"/"amplitude" keys
                       # and omit "direction" (defaults to +normal / low face)
      "plane_waves": [{"face":"x0".."z1", "angle_deg":.., "directional":true,
                       "excitation": {"type":.., ...}}, ...],
                       # directional plane waves launched from a boundary face,
                       # one PML-depth in (d_pml taken from "boundary"). "angle_deg"
                       # is the E polarization measured in that face's right-handed
                       # transverse frame (see wavesim.sources._FACE_CFG). The face
                       # must be a PML face (the workbench forces it).
      "ngspice_dll": "<path to ngspice.dll>", # optional; library_path for all
                                              # SPICE ports (else PySpice search)
      "spice_ports": [                        # SPICE co-simulation ports
        {"kind":"line", "name":.., "netlist":"<path>", "nodes":["port1p","0"],
         "p0":[x,y,z], "p1":[x,y,z], "sign":1.0, "uic":false},
        {"kind":"tem",  "name":.., "netlist":"<path>", "nodes":["port1p","0"],
         "normal":"z", "position":.., "direction":1.0|-1.0, "conductor_id":0,
         "bounds":[a0,a1,b0,b1],  # optional; as in tem_sources (whole face if absent)
         "directional":true, "sign":1.0, "uic":false}, ...],
      "mode_only": false,                     # solve TEM modes only; no FDTD run
      "monitors": {
        "energy": true,
        "probes":    [{"name":.., "component":"Ez", "x":.., "y":.., "z":..}, ...],
        "snapshots": [{"name":.., "field":"E", "normal":"z",
                       "position":.., "every_N_steps":20}, ...],
                      # "field" ('E'/'H') records all three components; 'S'
                      # records the Poynting vector S = E x H (Sx/Sy/Sz); a legacy
                      # "component" ("Ez", "|E|") records only that one
        "voltages":  [{"name":.., "path": [[x,y,z], ...]}, ...],
        "currents":  [{"name":.., "path": [[x,y,z], ...]}, ...]
      }
    }

results.npz holds the recorded monitor series (e.g. ``energy_times`` /
``energy_values``); summary.json holds scalar run metadata (dt, steps, wall
time, grid dims, voxel counts, final energy). A snapshot stores one frame stack
per recorded component (``snapshot_<idx>_<comp>_data``, e.g. ``snapshot_0_Ex_data``)
plus the ``snapshot_<idx>_times`` and the two in-plane node/edge coordinate arrays
(``snapshot_<idx>_edges0`` / ``_edges1``, metres, solver frame) they share; its
summary entry lists the ``field`` and the ``components`` actually saved. The
magnitude |E|/|H| is *not* stored -- the workbench derives it from the three
components (the same sqrt(Fx²+Fy²+Fz²) the solver's own magnitude monitor takes,
over the same collocated slices), which keeps results.npz a third smaller.
Every frame is **collocated to cell centres** by the solver's SnapshotMonitor,
so all components share one coordinate grid and each frame is one cell shorter
than the grid per in-plane axis; the edge arrays stay node coordinates and so
remain one entry longer than the frame (pcolormesh's convention). H frames are
additionally averaged across the half timestep onto the E timebase, so an E and
an H snapshot sharing a ``times`` entry are simultaneous -- what a Poynting
vector needs. The saved frames and edges are **cropped to the domain
interior** -- the PML padding cells on both in-plane axes are stripped so the
animation/export shows only the physical region. Each TEM mode stores its two
transverse cell-centre
coordinate arrays (``mode_<si>_<mi>_ca`` / ``_cb``), so the workbench draws them
on the real grid (uniform or non-uniform) instead of assuming a constant cell
size.

TEM ports (Session 9)
---------------------
Each ``tem_sources`` entry names a grid plane (the ``normal`` axis and the
``position`` of the plane along it, in the solver frame). The runner calls
:func:`wavesim.mode_solver.solve_tem_modes` on that plane to find the TEM mode of
the PEC cross-section, launches it as an amplitude-calibrated directional modal
source (:meth:`TEMMode.to_source`, which impresses the modal current a matched
line turns into the requested forward voltage — so a 1 V mode launches ≈ 1 V on
any grid or fill) during the FDTD run, and saves each solved mode's 2D
field profiles into ``results.npz`` (keys ``mode_<si>_<mi>_phi`` / ``_pec`` /
``_E_<comp>``) with its per-unit-length parameters under ``summary["modes"]``.
A port sits on a domain face, which abuts the PML, so the runner clamps the
launched plane onto the **first interior cell** (:func:`_clamp_launch_into_interior`,
mirroring :class:`wavesim.sources.PlaneWave`'s ``d_pml`` / ``N-1-d_pml``): the
plane's boundary node is shared by the last interior cell and the first PML cell,
and the Mode source's nearest-node snap rounds a high-face launch into the
absorber otherwise — firing the forward wave into the PML.
With ``mode_only`` true the runner solves and saves the modes and skips the FDTD
time-stepping entirely. The workbench's "Compute Mode" button uses this, sending a
job that carries **only the one port** it wants previewed (it plots the modes and
throws the workdir away); a real run solves every port's mode and keeps them in
its own ``results.npz``/``summary.json``. An
optional ``bounds`` ``[a0,a1,b0,b1]`` (solver metres, transverse slice order)
confines the mode solve to a sub-rectangle of the face — e.g. one connector's
cross-section on a plane that cuts several — and is forwarded straight to
``solve_tem_modes(bounds=...)``; absent it solves on the whole face. The solver
embeds a bounded mode back into the full transverse plane (the modal launch needs
that shape), so the runner crops the *saved* ``mode_*`` profiles and
their ``_ca``/``_cb`` coords back to the solved sub-rect (:func:`_bounds_window`)
— the results plot then shows the bounded region, not a face of zeros around it.
The launched mode itself keeps its full shape.

At the FDTD cell size the voxeliser can shred a continuous PEC on the plane into
disconnected cells, so ``ndimage.label`` miscounts conductors. When that happens
the FreeCAD side ships a ``mode_mesh`` block: a finer transverse re-voxelisation
of *that plane only*, auto-refined until the PEC component count stabilises. Its
2D arrays travel in ``materials.npz`` as ``<key>_pec`` (uint8), ``<key>_eps`` and
``<key>_mu`` (float64), shape ``(Na, Nb)`` in ``(a, b)`` transverse slice order.
The runner (:func:`_mode_mesh_grid`) rebuilds them as a single-cell-thick fine
grid, solves the mode there (conductor count now correct), and — for a launch —
interpolates the mode back onto the coarse grid as a rebuilt coarse ``TEMMode``
(:func:`_coarse_mode_from_fine`) that :meth:`TEMMode.to_source` (or a SPICE port)
then launches. The FDTD grid is untouched. Absent ⇒ the coarse
slice is solved as before. ``mode_mesh`` and ``bounds`` are mutually exclusive
per port: the fine grid already spans exactly the (bounded) box.

When the workbench's **characteristic-impedance convergence study** is on, the
``mode_mesh`` carries a ``levels`` ladder (progressively finer transverse
re-voxelisations of the plane, finest last; each level's 2D arrays in
``materials.npz`` as ``<key>_lvl<j>_{pec,eps,mu}``) plus a ``convergence``
``{max_iter, rel_tol}`` criterion. :func:`_solve_mode_convergence` solves the mode
on each level in turn and stops once the chosen conductor's impedance changes by
less than ``rel_tol`` between levels (or ``max_iter`` levels are used). The finest
solved level is then used exactly like a single mode mesh (saved, launched,
interpolated back onto the coarse grid). The per-iteration Z0 history rides along
in ``summary["modes"][*]["convergence"]`` for the results plot.

SPICE co-simulation ports
-------------------------
Each ``spice_ports`` entry couples one FDTD lumped port to a user ngspice netlist
in lockstep (:class:`wavesim.sources.SpicePort`). A ``kind:"line"`` port is a
straight ``p0 -> p1`` line; a ``kind:"tem"`` port drives a solved TEM mode of the
named plane (solved alongside the ``tem_sources`` modes, so it is saved/plotted
like one and honours ``mode_only``). The ngspice shared library is taken from
``ngspice_dll`` (falling back to a per-port ``library_path`` / PySpice's own
search). Each port records its port V(t)/I(t) into ``results.npz`` (keys
``spice_<idx>_times`` / ``_voltages`` / ``_currents``) with names under
``summary["spice_ports"]``. One netlist drives one port; several ports run
independent ngspice instances. (The port series are stored as two
``_times``/``_values`` pairs — ``spice_<idx>v_*`` for voltage, ``spice_<idx>i_*``
for current.)
"""

import json
import math
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
# Backend selection — pick the fastest available update-kernel backend
# --------------------------------------------------------------------------- #

def _cuda_available():
    """Return ``True`` when a CUDA GPU usable by the solver is present.

    Probes numba's CUDA driver binding. ``wavesim.backend_cuda`` forces the
    legacy ctypes binding on import (the default native one is blocked by
    Windows Smart App Control on some machines); mirror that here so the probe
    uses the same binding the run will. Any import or driver error is swallowed
    and treated as "no GPU", so a machine without CUDA simply falls back to the
    CPU backend instead of failing the run.
    """
    os.environ.setdefault("NUMBA_CUDA_USE_NVIDIA_BINDING", "0")
    try:
        from numba import cuda
        return bool(cuda.is_available())
    except Exception:
        return False


def _resolve_backend(requested):
    """Resolve a job's requested backend string to a concrete backend name.

    ``'auto'`` (the workbench default) becomes ``'cuda'`` when a CUDA GPU is
    available, else ``'numba'`` (the multithreaded CPU backend). An explicit
    ``'numpy'``/``'numba'``/``'cuda'`` is honoured unchanged, so a user can force
    the CPU path on a GPU box, or demand the GPU and get a clear solver error if
    it is missing. FreeCAD's Python cannot make this choice (it cannot import
    numba), which is why the ``'auto'`` sentinel is resolved here on the solver
    side rather than when the job is written.
    """
    requested = (requested or "auto").lower()
    if requested != "auto":
        return requested
    return "cuda" if _cuda_available() else "numba"


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

    if typ == "sinusoid":
        # Ramped CW. Use the solver's native Sinusoid so the launch machinery can
        # read its ``center_frequency`` (it tunes a directional plane-wave / TEM
        # launch's H time shift to the numerical phase velocity). ``phase`` is in
        # radians solver-side; ``ramp_cycles`` is the raised-cosine turn-on length.
        return ws.Sinusoid(
            frequency=float(exc.get("frequency", 30.0e9)),
            amplitude=amp,
            phase=math.radians(float(exc.get("phase_deg", 0.0))),
            ramp_cycles=float(exc.get("ramp_cycles", 3.0)),
        )

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


# In-plane axes (in array-index order) of the slice perpendicular to a normal,
# mirroring ``wavesim.monitors.record_snapshot``'s plane extraction.
_INPLANE_AXES = {"z": ("x", "y"), "y": ("x", "z"), "x": ("y", "z")}


def _field_components(field):
    """The three component tokens of field ``'E'``/``'H'``/``'S'`` (Poynting)."""
    u = str(field).upper()
    f = "S" if u.startswith("S") else ("H" if u.startswith("H") else "E")
    return [f + axis for axis in ("x", "y", "z")]


def _field_of(component):
    """The field ('E'/'H'/'S') a component token belongs to ('Ez', '|H|', 'Sx')."""
    text = str(component).replace("|", "")
    head = text[:1].upper()
    return head if head in ("H", "S") else "E"


class _PoyntingComponentView:
    """Presents one Cartesian component of a ``PoyntingMonitor`` as a
    ``SnapshotMonitor``-shaped monitor so the snapshot save/crop loop can treat
    Poynting (S = E x H) exactly like an E or H field.

    A ``PoyntingMonitor`` records one ``(Na, Nb, 3)`` frame (Sx, Sy, Sz) per
    step; a ``SnapshotMonitor`` records one ``(Na, Nb)`` scalar frame. This view
    slices out component *comp_index* so its ``.snapshots`` is a list of 2D
    frames, its ``.snap_times`` and ``.normal`` mirror the shared monitor, and
    the results plot derives |S| and the in-plane power-flow quiver from the
    three views just as it does for a field's components.
    """

    def __init__(self, poynting, comp_index, normal):
        self._poynting = poynting
        self._i = int(comp_index)
        self.normal = normal
        self._cache = None

    @property
    def snapshots(self):
        # Recorded lazily during the run, so slice on first access afterwards.
        # (numpy is imported inside ``run_job``, not at module scope.)
        if self._cache is None:
            import numpy as np
            self._cache = [np.asarray(f)[..., self._i]
                           for f in self._poynting.snapshots]
        return self._cache

    @property
    def snap_times(self):
        return self._poynting.snap_times


def _axis_nodes(grid, axis):
    """The node (edge) coordinate array of *grid* along *axis* ('x'/'y'/'z')."""
    return {"x": grid.x, "y": grid.y, "z": grid.z}[axis]


def _axis_centers(grid, axis):
    """The cell-centre coordinate array of *grid* along *axis*."""
    return {"x": grid.xc, "y": grid.yc, "z": grid.zc}[axis]


def _bounds_window(grid, mode, bounds):
    """Index window ``(ia0, ia1, ib0, ib1)`` of a ``bounds`` rect on *mode*'s plane.

    Mirrors ``mode_solver.solve_tem_modes``'s own sub-rect indexing, so the saved
    profiles can be cropped back to exactly the cells it solved. ``None`` when the
    rect degenerates to nothing (⇒ save the whole plane, as before).
    """
    a0, a1, b0, b1 = bounds
    ta = mode.transverse_axes
    ia0, ia1 = grid.axis_index(ta[0], a0), grid.axis_index(ta[0], a1)
    ib0, ib1 = grid.axis_index(ta[1], b0), grid.axis_index(ta[1], b1)
    if ia1 <= ia0 or ib1 <= ib0:
        return None
    return ia0, ia1, ib0, ib1


def _crop_plane(arr, win):
    """Crop a full-plane 2D profile to a :func:`_bounds_window` (no-op if ``None``)."""
    if win is None:
        return arr
    ia0, ia1, ib0, ib1 = win
    return arr[ia0:ia1, ib0:ib1]


def _choose_mode(modes, wanted, name):
    """Pick the mode whose energized conductor is *wanted* (0 = dominant).

    Falls back to the dominant (first) mode with an stderr note when no mode
    carries the requested conductor label.
    """
    chosen = modes[0]
    if wanted > 0:
        match = next((m for m in modes if m.conductor_id == wanted), None)
        if match is None:
            sys.stderr.write(
                "wavesim: port '{}' requested conductor {} but only conductors "
                "{} were solved; using conductor {} instead.\n".format(
                    name, wanted, [m.conductor_id for m in modes],
                    modes[0].conductor_id,
                )
            )
        else:
            chosen = match
    return chosen


# In-array slice-to-3D reshaping for a mode mesh: a 2D ``(Na, Nb)`` transverse
# plane becomes a singleton-thick 3D block along the normal axis, matching how
# ``mode_solver._slice`` extracts the plane (z→[:,:,k], y→[:,k,:], x→[k,:,:]).
_MODEMESH_SHAPE = {"z": lambda Na, Nb: (Na, Nb, 1),
                   "y": lambda Na, Nb: (Na, 1, Nb),
                   "x": lambda Na, Nb: (1, Na, Nb)}


def _thin_mode_grid(ws, np, normal, position, a_nodes, b_nodes,
                    pec2d, eps2d, mu2d):
    """Build a single-cell-thick fine grid from one plane's 2D material arrays.

    Turns the ``(Na, Nb)`` transverse ``pec``/``eps``/``mu`` arrays (``(a, b)``
    slice order) into a grid one cell thick along *normal*, centred on *position*,
    so :func:`wavesim.solve_tem_modes` runs on that fine cross-section. Shared by
    the single mode mesh and each level of a convergence ladder.
    """
    a_nodes = np.asarray(a_nodes, dtype=np.float64)
    b_nodes = np.asarray(b_nodes, dtype=np.float64)
    pec2d = np.ascontiguousarray(pec2d).astype(bool)
    eps2d = np.ascontiguousarray(eps2d, dtype=np.float64)
    mu2d = np.ascontiguousarray(mu2d, dtype=np.float64)
    Na, Nb = pec2d.shape

    # One thin cell along the normal, centred on the plane; its thickness is
    # immaterial to the purely transverse (2D) mode solve, so use a representative
    # transverse spacing.
    h = float(min(np.diff(a_nodes).min(), np.diff(b_nodes).min()))
    norm_nodes = np.array([position - 0.5 * h, position + 0.5 * h], dtype=np.float64)
    axes = {"z": (a_nodes, b_nodes, norm_nodes),
            "y": (a_nodes, norm_nodes, b_nodes),
            "x": (norm_nodes, a_nodes, b_nodes)}[normal]
    grid_f = ws.set_vacuum(ws.create_grid_rectilinear(*axes))

    shape3 = _MODEMESH_SHAPE[normal](Na, Nb)
    eps3, mu3, pec3 = eps2d.reshape(shape3), mu2d.reshape(shape3), pec2d.reshape(shape3)
    grid_f = ws.set_material_arrays(grid_f, eps3, eps3, eps3, mu3, mu3, mu3,
                                    pec_mask=pec3)
    return grid_f


def _mode_mesh_grid(ws, np, mm, material_data):
    """Build a thin fine rectilinear grid carrying a mode mesh's cross-section.

    A single-level ``mode_mesh`` block (see the job schema) re-voxelises one port
    plane on a connectivity-preserving fine transverse grid. This turns its 2D
    ``(Na, Nb)`` ``pec``/``eps``/``mu`` arrays (shipped in ``materials.npz`` under
    ``<key>_pec``/``_eps``/``_mu``) into a single-cell-thick 3D grid normal to the
    port, so :func:`wavesim.solve_tem_modes` runs on the fine cross-section where
    the conductor count is correct. Returns the ``FDTDGrid``.
    """
    key = mm["key"]
    return _thin_mode_grid(
        ws, np, mm["normal"], float(mm["position"]),
        mm["a_nodes"], mm["b_nodes"],
        material_data[key + "_pec"], material_data[key + "_eps"],
        material_data[key + "_mu"],
    )


def _solve_mode_convergence(ws, np, mm, material_data, wanted_conductor,
                            name, prefix):
    """Walk a mode-mesh refinement ladder until the characteristic impedance settles.

    A convergence ``mode_mesh`` block carries ``levels`` (a list of
    ``{a_nodes, b_nodes}``, finest last) whose per-level 2D arrays live in
    ``materials.npz`` as ``<key>_lvl<j>_{pec,eps,mu}``, plus a ``convergence``
    ``{max_iter, rel_tol}`` criterion. Each level is solved on its own thin fine
    grid (:func:`_thin_mode_grid`); after each, the chosen conductor's impedance is
    compared with the previous level's and the walk stops once the relative change
    is within ``rel_tol`` (or ``max_iter`` levels are used). Coarse levels that
    fail to resolve the wanted conductor are skipped for the comparison but still
    advance the mesh.

    Returns ``(modes, solve_grid, history)`` for the finest level actually solved:
    *modes* are handed on exactly like a single mode mesh's (saved, launched,
    interpolated back onto the coarse grid), and *history* is a per-iteration list
    ``[{iteration, Na, Nb, impedance, delta_rel, converged}, ...]`` for the
    summary / results plot.
    """
    key = mm["key"]
    normal = mm["normal"]
    position = float(mm["position"])
    levels = mm.get("levels") or []
    conv = mm.get("convergence") or {}
    rel_tol = float(conv.get("rel_tol", 0.01))
    max_iter = int(conv.get("max_iter", len(levels)))

    modes = []
    solve_grid = None
    history = []
    prev_z = None
    for li, lvl in enumerate(levels[:max_iter]):
        lkey = "{}_lvl{}".format(key, li)
        pec2d = material_data[lkey + "_pec"]
        solve_grid = _thin_mode_grid(
            ws, np, normal, position, lvl["a_nodes"], lvl["b_nodes"],
            pec2d, material_data[lkey + "_eps"], material_data[lkey + "_mu"],
        )
        modes = ws.solve_tem_modes(
            solve_grid, normal=normal, position=position, compute_params=True,
        )
        Na, Nb = int(pec2d.shape[0]), int(pec2d.shape[1])
        chosen = _choose_mode(modes, wanted_conductor, name) if modes else None
        z = float(chosen.impedance) if (
            chosen is not None and chosen.impedance is not None) else None
        delta = None
        converged = False
        if z is not None and prev_z is not None and abs(prev_z) > 0.0:
            delta = abs(z - prev_z) / abs(prev_z)
            converged = delta <= rel_tol
        history.append({
            "iteration": li + 1, "Na": Na, "Nb": Nb,
            "impedance": _f(z), "delta_rel": _f(delta), "converged": converged,
        })
        _emit_status(
            "{}mode convergence iteration {}/{} on a {}x{} mesh: Z0 = {}{}".format(
                prefix, li + 1, min(len(levels), max_iter), Na, Nb,
                "{:.4g} ohm".format(z) if z is not None else "n/a",
                "" if delta is None else " (change {:.3g}%)".format(100.0 * delta),
            )
        )
        if z is not None:
            if converged:
                break
            prev_z = z
    return modes, solve_grid, history


def _interp_coarse_profiles(np, mode, grid, fields):
    """Resample a fine-grid mode's E/H profiles onto the coarse grid's plane.

    A mode solved on the fine mode mesh must be launched on the coarse FDTD grid.
    Each requested transverse field component is interpolated from the fine cell
    centres onto the coarse grid's plane cell centres with a
    :class:`RegularGridInterpolator` (zero outside the fine span), yielding a
    ``{component: 2D-array}`` shaped like the coarse ``mode.normal``-slice — the
    form a rebuilt coarse :class:`~wavesim.mode_solver.TEMMode`
    (:func:`_coarse_mode_from_fine`) expects.
    """
    from scipy.interpolate import RegularGridInterpolator

    ta = mode.transverse_axes
    a_nodes = np.asarray(mode.a_nodes, dtype=np.float64)
    b_nodes = np.asarray(mode.b_nodes, dtype=np.float64)
    a_c = 0.5 * (a_nodes[:-1] + a_nodes[1:])
    b_c = 0.5 * (b_nodes[:-1] + b_nodes[1:])
    a_coarse = _axis_centers(grid, ta[0])
    b_coarse = _axis_centers(grid, ta[1])
    CA, CB = np.meshgrid(a_coarse, b_coarse, indexing="ij")
    query = np.stack([CA.ravel(), CB.ravel()], axis=-1)
    out_shape = (a_coarse.size, b_coarse.size)

    def _one(arr2d):
        f = RegularGridInterpolator(
            (a_c, b_c), np.asarray(arr2d, dtype=np.float64),
            bounds_error=False, fill_value=0.0,
        )
        return f(query).reshape(out_shape)

    profiles = {}
    if "E" in fields:
        for comp, arr in mode.E.items():
            profiles[comp] = _one(arr)
    if "H" in fields:
        for comp, arr in mode.H.items():
            profiles[comp] = _one(arr)
    return profiles


def _coarse_mode_from_fine(ws, np, mode, grid):
    """Rebuild a fine mode mesh's mode as a coarse-grid :class:`TEMMode`.

    A :class:`SpicePort` compiles its mode into a lumped-port kernel against the
    *coarse* FDTD grid (:meth:`TEMMode.build_port_kernel`), so a fine-grid mode
    cannot be handed to it directly — its cell indices reference the fine grid.
    This produces an equivalent coarse-grid mode: the transverse E/H profiles are
    resampled onto the coarse plane and the per-unit-length parameters carried
    over unchanged (``phi``/``pec`` are unused by the port kernel, so left zero).
    """
    profs = _interp_coarse_profiles(np, mode, grid, "EH")
    E = {c: a for c, a in profs.items() if c.startswith("E")}
    H = {c: a for c, a in profs.items() if c.startswith("H")}
    ta = mode.transverse_axes
    slice_shape = (_axis_centers(grid, ta[0]).size, _axis_centers(grid, ta[1]).size)
    return ws.TEMMode(
        normal=mode.normal, position=mode.position,
        slice_index=grid.axis_index(mode.normal, mode.position),
        transverse_axes=ta, da=mode.da, db=mode.db,
        phi=np.zeros(slice_shape, dtype=np.float64), E=E, H=H,
        pec=np.zeros(slice_shape, dtype=bool), conductor_id=mode.conductor_id,
        a_nodes=np.asarray(_axis_nodes(grid, ta[0]), dtype=np.float64),
        b_nodes=np.asarray(_axis_nodes(grid, ta[1]), dtype=np.float64),
        capacitance=mode.capacitance, inductance=mode.inductance,
        impedance=mode.impedance, v_phase=mode.v_phase, eps_eff=mode.eps_eff,
    )


def _reverse_mode_h(mode):
    """Return a shallow copy of *mode* with its H profiles negated.

    Reverses the Poynting vector S = E × H so a directional ``to_source`` launch
    flows toward -normal (a port on a high face fires *into* the domain). The E
    profiles — which set the calibrated launch amplitude — and every per-unit
    parameter are shared unchanged; only the H sheet's sign flips.
    """
    import copy

    rev = copy.copy(mode)
    rev.H = {comp: -arr for comp, arr in mode.H.items()}
    return rev


def _clamp_launch_into_interior(mode, grid, d_pml):
    """Pull a boundary launch plane out of the PML onto the first interior cell.

    A face port's plane sits on the domain boundary node. The Mode source injects
    at ``grid.axis_index(normal, position)`` -- the *nearest node* used as a cell
    index -- which on a low face is cell ``d_pml`` (the first interior cell,
    correct) but on a high face is cell ``N-d_pml``: the boundary node is shared
    by the last interior cell and the first PML cell, and the nearest-node rule
    rounds toward the PML. The forward wave would then be launched one cell inside
    the absorber. Clamp the injection cell into ``[d_pml, N-1-d_pml]`` -- exactly
    :class:`wavesim.sources.PlaneWave`'s convention -- and move ``mode.position``
    onto that cell's node so both the launch and any coarse-grid rebuild
    (:func:`_coarse_mode_from_fine`, which reads ``mode.position``) follow.

    A no-op for an interior port (its cell is already well inside the range). The
    near bound is the PML pad on the port's own face, which a TEM/SPICE-TEM face
    always has (the workbench forces it to PML); the far bound never binds.
    """
    normal = mode.normal
    N = {"x": grid.Nx, "y": grid.Ny, "z": grid.Nz}[normal]
    coords = {"x": grid.x, "y": grid.y, "z": grid.z}[normal]
    k = grid.axis_index(normal, mode.position)
    k_interior = min(max(k, d_pml), N - 1 - d_pml)
    if k_interior != k:
        mode.position = float(coords[k_interior])
    return mode


def _solve_all_modes(ws, np, grid, job, material_data=None):
    """Solve the TEM modes of every TEM-source and SPICE-TEM-port plane.

    Returns ``(plane_sources, spice_modes, mode_arrays, mode_meta)``:

    * ``plane_sources`` — amplitude-calibrated modal launchers
      (:meth:`TEMMode.to_source`) for the ``tem_sources`` (one per port, the
      chosen mode); empty when ``mode_only``.
    * ``spice_modes`` — ``{job_spice_index: TEMMode}`` giving the chosen mode for
      each ``kind:"tem"`` SPICE port, consumed by :func:`_build_spice_ports`;
      empty when ``mode_only`` (no FDTD to drive).
    * ``mode_arrays`` — the 2D field profiles for ``results.npz``
      (``mode_<si>_<mi>_phi`` / ``_pec`` / ``_E_<comp>``).
    * ``mode_meta`` — per-mode metadata for ``summary["modes"]``.

    When an entry carries a ``mode_mesh`` block (and *material_data*, the loaded
    ``materials.npz``, holds its arrays) the mode is solved on a thin fine grid
    built from that connectivity-preserving re-voxelisation instead of the coarse
    slice, then interpolated back onto *grid* to launch; otherwise the historical
    coarse-slice solve (honouring any ``bounds``) runs.
    """
    mode_only = bool(job.get("mode_only", False))
    # PML depth (cells) used to keep a face-port launch plane just inside the
    # absorber; see _clamp_launch_into_interior.
    d_pml = int((job.get("boundary") or {}).get("d_pml", 10))

    # Every plane needing a mode solve: TEM sources first, then SPICE TEM ports.
    # ``spice_index`` is the entry's index in job["spice_ports"] (None for TEM
    # sources) so the chosen mode can be handed back to _build_spice_ports.
    planes = []  # (kind, cfg, spice_index)
    for t in job.get("tem_sources") or []:
        planes.append(("tem_source", t, None))
    for idx, p in enumerate(job.get("spice_ports") or []):
        if p.get("kind") == "tem":
            planes.append(("spice", p, idx))

    plane_sources = []
    spice_modes = {}
    mode_arrays = {}
    mode_meta = []

    n_ports = len(planes)
    for si, (kind, t, spice_index) in enumerate(planes):
        normal = t.get("normal", "z")
        position = float(t.get("position", 0.0))
        name = t.get("name", "TEM")

        # Characteristic frequency/amplitude/fields for the results tree. SPICE
        # ports have no waveform (the circuit drives them), so they carry none.
        if kind == "spice":
            fmax, amplitude = 0.0, 1.0
            fields = "EH" if t.get("directional", True) else "E"
        else:
            exc_spec = t.get("excitation") or {}
            etype = exc_spec.get("type", "gaussian")
            amplitude = float(exc_spec.get("amplitude", t.get("amplitude", 1.0)))
            if etype in ("sine", "gaussian_sine"):
                fmax = float(exc_spec.get("frequency", 0.0))
            else:  # gaussian (or legacy) uses fmax; rectangular has none
                fmax = float(exc_spec.get("fmax", t.get("fmax", 0.0)))
            fields = t.get("fields", "EH")

        # A connectivity-preserving mode mesh re-voxelises this plane on a fine
        # transverse grid so the conductor count is right (the coarse cell size
        # can shred one PEC into several cells). Solve there when present; the
        # solved mode is interpolated back onto the coarse grid to launch.
        mm = t.get("mode_mesh")
        use_mesh = mm is not None and material_data is not None
        # A convergence mode mesh carries a refinement ``levels`` ladder the runner
        # walks until Z0 settles; a plain one carries a single ``a_nodes``/
        # ``b_nodes`` grid.
        use_convergence = use_mesh and bool(mm.get("levels"))
        # Optional in-plane bounds (solver-frame metres, transverse slice order).
        # Mutually exclusive with a mode mesh, whose fine grid already spans
        # exactly the (bounded) box.
        bounds = None if use_mesh else t.get("bounds")
        conv_history = None

        prefix = "Port {}/{}: ".format(si + 1, n_ports) if n_ports > 1 else ""
        _emit_status(
            "{}solving TEM mode on the {}-plane of '{}'\n"
            "({}factorising the cross-section; this scales with grid "
            "size)...".format(
                prefix, normal, name,
                "impedance-convergence fine meshes; " if use_convergence else
                "connectivity-preserving fine mesh; " if use_mesh else "",
            )
        )
        if use_convergence:
            # Walk the refinement ladder until the chosen conductor's Z0 settles.
            # The finest solved level is used exactly like a single mode mesh.
            modes, solve_grid, conv_history = _solve_mode_convergence(
                ws, np, mm, material_data, int(t.get("conductor_id", 0)),
                name, prefix,
            )
        elif use_mesh:
            # The fine grid already spans exactly the (bounded) box, so no
            # ``bounds`` is passed — the whole fine plane is the solve region.
            solve_grid = _mode_mesh_grid(ws, np, mm, material_data)
            modes = ws.solve_tem_modes(
                solve_grid, normal=normal, position=position, compute_params=True,
            )
        else:
            # Confine the mode solve to a sub-rectangle of the face when the port
            # carries ``bounds``. Absent => whole face, the historical behaviour.
            solve_grid = grid
            modes = ws.solve_tem_modes(
                grid, normal=normal, position=position,
                bounds=tuple(bounds) if bounds else None,
                compute_params=True,
            )
        _emit_status(
            "{}found {} TEM mode(s); building field profiles...".format(
                prefix, len(modes)
            )
        )

        # ``solve_tem_modes`` embeds a bounded solve back into the *full* plane
        # (the modal launch needs the full transverse shape), padding it with
        # zeros. Crop the **saved** profiles back to the cells actually solved, so
        # the results plot draws the bounded region the user selected instead of a
        # face of zeros around it. The in-memory ``mode`` handed to ``to_source``
        # / ``SpicePort`` below keeps its full shape and is untouched.
        win = _bounds_window(grid, modes[0], bounds) if (bounds and modes) else None

        for mi, mode in enumerate(modes):
            key = "mode_{}_{}".format(si, mi)
            mode_arrays[key + "_phi"] = np.asarray(
                _crop_plane(mode.phi, win), dtype=np.float64
            )
            mode_arrays[key + "_pec"] = np.asarray(
                _crop_plane(mode.pec, win), dtype=np.uint8
            )
            for comp, arr in mode.E.items():
                mode_arrays["{}_E_{}".format(key, comp)] = np.asarray(
                    _crop_plane(arr, win), dtype=np.float64
                )
            # Transverse cell-centre coordinates (metres, solver frame) so the
            # results plot can draw the mode on the real (possibly non-uniform)
            # axes rather than assuming a constant da/db spacing.
            t_axes = list(getattr(mode, "transverse_axes", []))
            if len(t_axes) == 2:
                # From the grid the mode was solved on (the fine mesh when used),
                # so the results plot shows the true mode resolution; sliced to
                # the same window as the profiles above.
                ca = _axis_centers(solve_grid, t_axes[0])
                cb = _axis_centers(solve_grid, t_axes[1])
                if win is not None:
                    ca, cb = ca[win[0]:win[1]], cb[win[2]:win[3]]
                mode_arrays[key + "_ca"] = np.asarray(ca, dtype=np.float64)
                mode_arrays[key + "_cb"] = np.asarray(cb, dtype=np.float64)
            meta = {
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
                "spice": kind == "spice",
            }
            # Attach the port's impedance-convergence history (same for every mode
            # on this plane) so the results plot can show Z0 settling with mesh.
            if conv_history is not None:
                meta["convergence"] = conv_history
            mode_meta.append(meta)

        if not modes or mode_only:
            continue

        chosen = _choose_mode(modes, int(t.get("conductor_id", 0)), name)
        # A port on a domain face abuts the PML; keep its launch plane on the
        # first interior cell so the forward wave isn't fired into the absorber
        # (nearest-node snapping otherwise lands a high-face launch one cell
        # inside the PML). Applies to both the TEM and SPICE-TEM launch paths,
        # which each read the chosen mode's ``position``.
        _clamp_launch_into_interior(chosen, grid, d_pml)
        if kind == "spice":
            # Hand the chosen mode to _build_spice_ports; the circuit drives it.
            # A fine-mesh mode is rebuilt on the coarse grid first, since the
            # SpicePort compiles its kernel against the coarse FDTD grid.
            spice_modes[spice_index] = (
                _coarse_mode_from_fine(ws, np, chosen, grid) if use_mesh else chosen
            )
            continue

        # TEM source: launch the chosen mode as an amplitude-calibrated modal
        # source. ``to_source`` impresses the modal current that a matched line
        # turns into ``amplitude·waveform(t)`` volts forward, using the same
        # ``build_port_kernel`` current machinery a port uses — so the launched
        # voltage is correct on any grid or fill permittivity (the older additive
        # field write came out √ε_r / S_c too large). The waveform already carries
        # the excitation amplitude, so ``amplitude`` stays at unit scale.
        #
        # A fine-mesh mode lives on the fine grid; rebuild it on the coarse launch
        # grid first (exactly as the SPICE path does), since the port kernel
        # indexes the coarse cells.
        waveform = _build_waveform(ws, t)
        launch_mode = (
            _coarse_mode_from_fine(ws, np, chosen, grid) if use_mesh else chosen
        )
        # ``to_source`` builds H = (n̂ × E)/η for a +normal launch, so the wave
        # flows toward +normal. A port on a high face launches *into* the domain
        # along -normal (direction < 0): negate the mode's H profiles to reverse
        # the Poynting vector S = E × H (an E-only launch carries no H sheet and
        # is bidirectional, so the sign is moot there). Any residual backward lobe
        # is absorbed by the PML behind the launch face.
        direction = float(t.get("direction", 1.0))
        if direction < 0 and fields != "E":
            launch_mode = _reverse_mode_h(launch_mode)
        src = launch_mode.to_source(waveform, amplitude=1.0, fields=fields)
        plane_sources.append(src)

    return plane_sources, spice_modes, mode_arrays, mode_meta


# --------------------------------------------------------------------------- #
# SPICE co-simulation ports — build one SpicePort per spice_ports entry
# --------------------------------------------------------------------------- #

def _prepare_ngspice_library(job):
    """Make the configured ``ngspice.dll`` and its sibling DLLs loadable.

    PySpice loads ``ngspice.dll`` by full path via cffi, which on modern Windows
    does **not** add the DLL's own directory to the search path. ngspice ships a
    co-located dependency (``libomp140.x86_64.dll``, the OpenMP runtime), so the
    load otherwise fails with ``OSError`` 0x7e (``ERROR_MOD_NOT_FOUND``). Put the
    DLL's directory on the search path and pre-load its sibling DLLs so the later
    cffi load resolves them. A no-op when no ngspice path is configured or on
    platforms without ``os.add_dll_directory`` (non-Windows).
    """
    dll = job.get("ngspice_dll")
    if not dll or not os.path.isfile(dll):
        return
    d = os.path.dirname(os.path.abspath(dll))
    add_dll_dir = getattr(os, "add_dll_directory", None)
    if add_dll_dir is not None:  # Windows, Python 3.8+
        try:
            add_dll_dir(d)
        except OSError:
            pass
    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    # Pre-load every sibling DLL so ngspice finds each already in the process
    # (the loader resolves an import against modules already loaded by name).
    try:
        import ctypes

        for name in os.listdir(d):
            if name.lower().endswith(".dll") and name.lower() != "ngspice.dll":
                try:
                    ctypes.CDLL(os.path.join(d, name))
                except OSError:
                    pass
    except Exception:
        pass


def _build_spice_ports(ws, job, spice_modes):
    """Build a :class:`SpicePort` for each ``spice_ports`` entry.

    Returns a list of ``(name, SpicePort)``. Line ports come straight from their
    ``p0``/``p1``; TEM ports drive the mode chosen in :func:`_solve_all_modes`
    (passed in *spice_modes*, keyed by job-spice index). Ports whose netlist file
    is missing — or whose TEM mode failed to solve — are skipped with a note so
    the rest of the run still proceeds.
    """
    lib = job.get("ngspice_dll") or None
    if job.get("spice_ports"):
        _prepare_ngspice_library(job)
    ports = []
    for idx, e in enumerate(job.get("spice_ports") or []):
        name = e.get("name", "spice")
        netlist = e.get("netlist") or ""
        if not netlist or not os.path.isfile(netlist):
            _emit_status(
                "SPICE port '{}': netlist not found ({}); skipping.".format(
                    name, netlist or "unset"
                )
            )
            sys.stderr.write(
                "wavesim: SPICE port '{}' netlist not found: {!r}; skipping.\n"
                .format(name, netlist)
            )
            continue
        nodes = tuple(e.get("nodes", ("port1p", "0")))
        sign = float(e.get("sign", 1.0))
        uic = bool(e.get("uic", False))
        if e.get("kind") == "tem":
            mode = spice_modes.get(idx)
            if mode is None:
                sys.stderr.write(
                    "wavesim: SPICE TEM port '{}' has no solved mode "
                    "(needs >=2 PEC conductors on the plane); skipping.\n"
                    .format(name)
                )
                continue
            port = ws.SpicePort(
                mode=mode, netlist=netlist, nodes=nodes,
                directional=bool(e.get("directional", True)),
                library_path=lib, sign=sign, uic=uic,
            )
        else:
            port = ws.SpicePort(
                p0=tuple(e["p0"]), p1=tuple(e["p1"]),
                netlist=netlist, nodes=nodes,
                library_path=lib, sign=sign, uic=uic,
            )
        ports.append((name, port))
    return ports


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

    # Resolve the update-kernel backend before allocating the grid: the CUDA GPU
    # path wants float32 field/material arrays for good throughput on consumer
    # cards, so the choice drives the grid dtype. Mode-only jobs never run the
    # FDTD loop (the backend is unused) and their mode solve is more accurate in
    # double precision, so they stay float64 / numba regardless.
    mode_only = bool(job.get("mode_only", False))
    backend = "numba" if mode_only else _resolve_backend(job.get("backend", "auto"))
    field_dtype = np.float32 if backend == "cuda" else np.float64

    # Importing the solver pulls in numba/scipy and, on a cold interpreter, can
    # take several seconds — tell the user so the GUI does not look hung.
    _emit_status("Loading solver (first run may compile, please wait)...")
    import wavesim as ws

    g = job["grid"]
    dx = float(g["dx"])
    dy = float(g.get("dy", dx))
    dz = float(g.get("dz", dx))
    # Non-uniform (rectilinear) grid when the job carries per-axis node
    # coordinate arrays (solver frame, metres); else the uniform grid. On a
    # uniform node array the two paths are bit-for-bit identical by design (the
    # solver derives constant spacing/dual arrays from the coordinates).
    gx, gy, gz = g.get("x"), g.get("y"), g.get("z")
    if gx is not None and gy is not None and gz is not None:
        grid = ws.create_grid_rectilinear(
            np.asarray(gx, dtype=np.float64),
            np.asarray(gy, dtype=np.float64),
            np.asarray(gz, dtype=np.float64),
            dtype=field_dtype,
        )
    else:
        grid = ws.create_grid(
            int(g["Nx"]), int(g["Ny"]), int(g["Nz"]), dx, dy, dz, dtype=field_dtype
        )
    grid = ws.set_vacuum(grid)

    # Optional voxelised materials (Session 3+). Absent in the Session 2 slice.
    # ``material_data`` is kept for the mode solve: a port's ``mode_mesh`` block
    # loads its fine ``modemesh_*`` arrays from here.
    materials_path = os.path.join(workdir, "materials.npz")
    voxel_summary = {}
    material_data = None
    if os.path.isfile(materials_path):
        data = np.load(materials_path)
        material_data = data
        pec_mask = data["pec_mask"] if "pec_mask" in data.files else None
        # Cast to the grid's dtype so the field and material arrays stay
        # matched — the CUDA backend keys its per-cell arithmetic and scalar
        # coefficients off the field dtype, so a float32 grid needs float32
        # eps/mu to do genuine single-precision math (the arrays are written as
        # float64 by the FreeCAD-side voxeliser).
        grid = ws.set_material_arrays(
            grid,
            data["eps_x"].astype(field_dtype, copy=False),
            data["eps_y"].astype(field_dtype, copy=False),
            data["eps_z"].astype(field_dtype, copy=False),
            data["mu_x"].astype(field_dtype, copy=False),
            data["mu_y"].astype(field_dtype, copy=False),
            data["mu_z"].astype(field_dtype, copy=False),
            pec_mask=pec_mask,
        )
        if pec_mask is not None:
            voxel_summary["pec_cells"] = int(np.count_nonzero(pec_mask))
        voxel_summary["dielectric_cells"] = int(np.count_nonzero(data["eps_x"] != 1.0))

    # TEM ports: solve each port plane's transverse mode. Done before the FDTD
    # setup so the solved modes can be launched as directional plane sources
    # (and so a mode-only request can return without building the time loop).
    plane_sources, spice_modes, mode_arrays, mode_meta = _solve_all_modes(
        ws, np, grid, job, material_data
    )

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

    # Boundary plane waves: a one-way plane wave launched from a boundary face,
    # one PML-depth inside it. The E sheet is placed at cell ``d_pml`` (low face)
    # or ``N-1-d_pml`` (high face), so it sits on the first interior cell of the
    # forced-PML launch face and its backward lobe is absorbed. ``d_pml`` comes
    # from the run's boundary so the sheet matches the actual absorber thickness.
    for pw in job.get("plane_waves") or []:
        waveform = _build_waveform(ws, pw)
        sources.append(ws.PlaneWave(
            str(pw["face"]),
            math.radians(float(pw.get("angle_deg", 0.0))),
            waveform,
            d_pml=int(boundary.get("d_pml", 10)),
            directional=bool(pw.get("directional", True)),
        ))

    # SPICE co-simulation ports: one live ngspice instance each, driven in
    # lockstep with the FDTD loop. Kept aside so their port records can be saved
    # and their ngspice instances torn down after the run.
    spice_ports = _build_spice_ports(ws, job, spice_modes)
    sources.extend(port for _name, port in spice_ports)

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

    # A snapshot records a whole field: one solver monitor per component, so the
    # results window can offer Ex/Ey/Ez (and |E|, derived from them) from a single
    # user-placed monitor. Legacy jobs naming one ``component`` record just that.
    # A Poynting snapshot ("field": "S") is one ``PoyntingMonitor`` whose (Na,Nb,3)
    # S = E x H frames are split into Sx/Sy/Sz views, so the same save/crop loop
    # and results plot handle it like a field (|S| derived, in-plane quiver).
    snapshots = []  # (name, field, [(component, monitor-or-view), ...])
    snapshot_solver_monitors = []  # the actual solver monitors to register
    for s in mon_cfg.get("snapshots", []):
        position = float(s.get("position", s.get("at_z", 0.0)))
        every = max(1, int(s.get("every_N_steps", 20)))
        normal = s.get("normal", "z")
        field = s.get("field")
        name = s.get("name", "snapshot")
        if field and str(field).upper().startswith("S"):
            poy = ws.PoyntingMonitor(position, every, normal=normal)
            snapshot_solver_monitors.append(poy)
            mons = [(c, _PoyntingComponentView(poy, i, normal))
                    for i, c in enumerate(_field_components("S"))]
            snapshots.append((name, "S", mons))
            continue
        if field:
            comps = _field_components(field)
        else:
            comps = [s["component"]]
            field = _field_of(comps[0])
        mons = [(c, ws.SnapshotMonitor(c, position, every, normal=normal))
                for c in comps]
        snapshot_solver_monitors.extend(m for _c, m in mons)
        snapshots.append((name, field, mons))

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
    all_monitors.extend(snapshot_solver_monitors)
    all_monitors.extend(m for _name, m in voltages)
    all_monitors.extend(m for _name, m in currents)

    sim = ws.Simulation(
        grid,
        cpml=cpml,
        sources=sources,
        monitors=all_monitors,
        pec_faces=pec_faces,
        backend=backend,
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
    # label reflects what is actually happening while the bar advances — naming
    # the resolved backend so an 'auto' job makes clear whether the GPU is in use.
    backend_label = {
        "cuda": "CUDA GPU (float32)",
        "numba": "Numba (multicore CPU)",
        "numpy": "NumPy (reference)",
    }.get(backend, backend)
    _emit_status("Running FDTD simulation on {} ({} time steps)...".format(
        backend_label, n_steps))
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

    # Snapshots: per recorded component a stack of frames (n_frames, N_axis1,
    # N_axis2), plus the times and the two in-plane node (edge) coordinate arrays
    # (metres, solver frame) shared by the components, so the results plot honours
    # non-uniform spacing via pcolormesh.
    d_pml = int(boundary.get("d_pml", 10))
    pml_set = set(pml_faces)

    def _interior_pad(axis):
        """PML cell counts (lo, hi) to strip off *axis* ('x'/'y'/'z')."""
        return (
            d_pml if (axis + "0") in pml_set else 0,
            d_pml if (axis + "1") in pml_set else 0,
        )

    snapshot_meta = []
    for idx, (name, field, mons) in enumerate(snapshots):
        saved = []
        for comp, mon in mons:
            if not mon.snapshots:
                continue
            data = np.asarray(mon.snapshots)
            ax0, ax1 = _INPLANE_AXES.get(getattr(mon, "normal", "z"), ("x", "y"))
            edges0 = np.asarray(_axis_nodes(grid, ax0), dtype=np.float64)
            edges1 = np.asarray(_axis_nodes(grid, ax1), dtype=np.float64)
            # Crop the PML padding off both in-plane axes so the saved frames (and
            # the animation/export built from them) show only the domain interior.
            #
            # The frames are collocated to cell centres by the solver's
            # SnapshotMonitor and are therefore already one cell short per
            # in-plane axis (cells 0..N-2). So the window is expressed in *grid*
            # cells -- keep cells [lo, N-hi) -- and clamped to what the frame
            # actually carries. Deriving the high edge from ``data.shape``
            # instead would silently eat one extra interior cell per PML face,
            # since collocation has already consumed the last one.
            (lo0, hi0), (lo1, hi1) = _interior_pad(ax0), _interior_pad(ax1)
            n0, n1 = data.shape[1], data.shape[2]
            stop0 = min(len(edges0) - 1 - hi0, n0)
            stop1 = min(len(edges1) - 1 - hi1, n1)
            if stop0 > lo0 and stop1 > lo1:
                data = data[:, lo0:stop0, lo1:stop1]
                # Cell-centred values still sit inside their original cells, so
                # the node array remains the correct pcolormesh edge array --
                # one entry longer than the frame.
                edges0 = edges0[lo0:stop0 + 1]
                edges1 = edges1[lo1:stop1 + 1]
            result_arrays["snapshot_{}_{}_data".format(idx, comp)] = data
            if not saved:
                # Same plane and cadence for every component: save once.
                result_arrays["snapshot_{}_times".format(idx)] = \
                    np.asarray(mon.snap_times)
                result_arrays["snapshot_{}_edges0".format(idx)] = edges0
                result_arrays["snapshot_{}_edges1".format(idx)] = edges1
            saved.append(comp)
        # The two components lying *in* the slice plane, in array-index order —
        # the in-plane vector the results plot draws as a quiver overlay. Named
        # here because only this side knows the slice normal.
        normal = getattr(mons[0][1], "normal", "z") if mons else "z"
        pax0, pax1 = _INPLANE_AXES.get(normal, ("x", "y"))
        snapshot_meta.append({
            "name": name, "field": field, "components": saved,
            "inplane": [field + pax0, field + pax1],
            "frames": len(mons[0][1].snapshots) if mons else 0,
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

    # SPICE ports: the co-simulated port V(t)/I(t) recorded by each SpicePort,
    # saved as two ``_times``/``_values`` series (voltage 'v', current 'i') so the
    # results tree can reuse the shared 1-D plotter. Then tear down ngspice.
    spice_meta = []
    for idx, (name, port) in enumerate(spice_ports):
        times = np.asarray(port.times)
        result_arrays["spice_{}v_times".format(idx)] = times
        result_arrays["spice_{}v_values".format(idx)] = np.asarray(port.voltages)
        result_arrays["spice_{}i_times".format(idx)] = times
        result_arrays["spice_{}i_values".format(idx)] = np.asarray(port.currents)
        spice_meta.append({"name": name})
        try:
            port.close()
        except Exception:
            pass

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
        "subpixel": bool(job.get("subpixel", False)),
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
    if spice_meta:
        summary["spice_ports"] = spice_meta
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
