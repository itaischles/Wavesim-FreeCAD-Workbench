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
                                              # 'auto' never picks CUDA for a
                                              # conformal or a lossy job: that
                                              # backend refuses cut-cell
                                              # geometry and has no lossy E
                                              # update.
      "mode": "fdtd",               # optional; "electrostatic" solves once for the
                                    # potential instead of time-stepping (see
                                    # "Electrostatics" below). Absent == "fdtd",
                                    # so every earlier job is unchanged.
      "pec_names": {"trace": 1, ..},# optional; part label -> the value that
                                    # labels its cells in materials.npz's
                                    # ``pec_id``. Both together or neither; only
                                    # an electrostatic job writes them, and the
                                    # FDTD path reads neither.
      "steps": 1000,
      "conformal_pec": false,       # did this job's voxelisation produce the six
                                    # pec_*_open_* arrays in materials.npz? What
                                    # actually ran, not what the user asked for
                                    # (the workbench falls back to staircase when
                                    # nothing is cut or the background is PEC).
                                    # Read *before* materials.npz is opened,
                                    # because it steers the backend and so the
                                    # grid dtype.
      "conformal_area_threshold": 0.4,  # optional; smallest open area fraction an
                                    # H face may have before it is clamped, which
                                    # is what keeps a sliver cut cell stable at
                                    # fixed dt. Ignored without the arrays.
      "lossy": false,               # does materials.npz carry the three sigma_*
                                    # arrays? Same rule as conformal_pec: what
                                    # actually got voxelised, and read before the
                                    # arrays are opened, because a lossy grid is
                                    # CPU-only and that sets the grid dtype.
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
      "modal_ports": [{"name":.., "normal":"z", "position":..,
                       "face": "z0".."z1",      # the domain face this port
                                                # terminates; ModalPort derives its
                                                # ghost-H plane and sign from it
                       "conductor_point": [a,b],# optional: a point inside the
                                                # conductor whose mode to launch
                                                # (solver metres, transverse slice
                                                # order). The mode with phi ~ 1 V
                                                # there is the one; takes priority
                                                # over "conductor_id"
                       "conductor_id": 0,       # legacy: which solved mode to
                                                # launch by conductor label (see
                                                # summary "modes"), 0/absent =
                                                # dominant. Read only when no
                                                # "conductor_point" is given
                       "port": "<label>",       # optional: the owning port object;
                                                # every drive row of one multi-
                                                # conductor port shares it
                       "conductor": "<label>",  # optional: that row's conductor,
                                                # echoed into summary["modes"]
                       "bounds": [a0,a1,b0,b1], # optional in-plane subset (solver
                                                # metres, transverse slice order);
                                                # absent = the whole face
                       "excitation": {"type":.., ...}}, ...],
                       # A modal port face carries NO PML and NO PEC: the port is
                       # the boundary (see "Modal ports" below). SEVERAL entries may
                       # share one face -- that is how a multi-conductor port
                       # launches a superposition of modes; their sheets superpose
                       # on the ghost H plane and the plane is factorised once.
                       # Legacy jobs name this list "tem_sources" and may carry
                       # "direction"/"fields" and a "mode_mesh" block; all three are
                       # ignored now.
      "gaussian_beams": [{"face":"x0".."z1", "angle_deg":.., "waist":<metres>,
                       "directional":true,
                       "excitation": {"type":.., ...}}, ...],
                       # directional Gaussian beams launched from a boundary face,
                       # one PML-depth in (d_pml taken from "boundary"). "angle_deg"
                       # is the E polarization measured in that face's right-handed
                       # transverse frame (see wavesim.sources._FACE_CFG). The face
                       # must be a PML face (the workbench forces it). "waist" is
                       # w0 in metres -- the 1/e E-amplitude radius, sitting at the
                       # launch plane (flat phase front) -- and must be positive;
                       # the workbench resolves its "auto" waist before writing.
                       # The solver zeroes the sheet over the transverse PML slabs,
                       # which is what keeps a DC-containing waveform from growing
                       # without bound there.
      "ngspice_dll": "<path to ngspice.dll>", # optional; library_path for all
                                              # SPICE ports (else PySpice search)
      "spice_ports": [                        # SPICE co-simulation ports
        {"kind":"line", "name":.., "netlist":"<path>", "nodes":["port1p","0"],
         "p0":[x,y,z], "p1":[x,y,z], "sign":1.0, "uic":false},
        {"kind":"tem",  "name":.., "netlist":"<path>", "nodes":["port1p","0"],
         "normal":"z", "position":.., "direction":1.0|-1.0, "conductor_id":0,
         "bounds":[a0,a1,b0,b1],  # optional; as in modal_ports (whole face if absent)
         "directional":true, "sign":1.0, "uic":false}, ...],
                      # A SPICE TEM port is still a *lumped* launch on an interior
                      # plane, so unlike a modal port its face IS forced to PML.
      "lumped_ports": [                       # lumped R/L/C elements on a line
        {"name":.., "p0":[x,y,z], "p1":[x,y,z],
         "resistance":50.0, "inductance":.., "capacitance":..,  # each optional
         "topology":"series"|"parallel",
         "drive":"none"|"voltage"|"current",
         "excitation": {"type":.., ...}}, ...],  # only when drive != "none"
                      # One wavesim.sources.LineSource each: the given branches
                      # wired between the two terminals, optionally in company
                      # with an ideal voltage (Thevenin) or current (Norton)
                      # source. An omitted branch is ABSENT, not zero (a short in
                      # series, an open in parallel); at least one branch or a
                      # drive is required. p0 is the "+" terminal.
      "electrostatic": {                      # required when mode == "electrostatic"
        "potentials": {"trace": 5.0, "gnd": 0.0},  # volts, keyed by part name
        "boundary": "ground" | "neumann" | {"xmin": "ground", ...},
        "capacitance": true,                  # also extract the C matrix
        "method": "auto",                     # 'auto'|'direct'|'cg'
        "slices": [{"name":.., "field":"phi"|"E"|"D",
                    "normal":"z", "position":..}, ...]
      },
      "mode_only": false,                     # solve TEM modes only; no FDTD run
      "monitors": {
        "energy": {"full": false, "interior": true},
                      # one whole-domain energy monitor per true region:
                      # "full" sums the entire grid, "interior" only the physical
                      # domain (PML cells dropped). Both false records no energy;
                      # a legacy bool true == {"full": true}
        "dissipation": {"full": false, "interior": true},
                      # ohmic power P = sum(sigma*|E|^2*dV), same regions and the
                      # same both-false-means-nothing rule. 'interior' is usually
                      # what is wanted: the PML dissipates by design and would
                      # swamp the material's own loss. Records 0.0 on a lossless
                      # grid rather than refusing.
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

materials.npz holds ``eps_x/y/z``, ``mu_x/y/z`` and an optional ``pec_mask``,
plus — for a conformal PEC run — the six ``pec_edge_open_x/y/z`` and
``pec_face_open_x/y/z`` arrays: the dimensionless fraction of each Yee E edge /
H face **not** inside a conductor. Fractions rather than metres, so the solver
multiplies by its own spacing arrays and the geometry keeps a single owner. All
six or none (a partial set is refused): with them the solver integrates the cut
contour, without them it staircases exactly as before. ``pec_mask`` stays in
either case, but is only *read* on the staircase path.

It may also carry the three ``sigma_x/y/z`` arrays — the electric conductivity
in S/m seen by Ex/Ey/Ez — which switch the solver onto its two-coefficient
(lossy-dielectric) E update. All three or none. **Lossy dielectrics only:** a
good conductor belongs in ``pec_mask``, and where the two overlap PEC wins,
since the mask zeroes E after the update regardless. Absent ⇒ the one-coefficient
update, bit-identical to a pre-loss run.

results.npz holds the recorded monitor series (e.g. ``energy_times`` /
``energy_values`` for the whole grid, ``energy_interior_*`` for the PML-free
interior, and ``dissipation_*``/``dissipation_interior_*`` for the ohmic power);
summary.json holds scalar run metadata (dt, steps, wall time, grid
dims, voxel counts, and ``<key>_final``/``<key>_max`` per recorded energy
region — a dissipation region adds ``<key>_energy``, the time integral of the
power, which is the number to weigh against ``U(0) - U(t)``). It also echoes
``lossy`` and ``conformal_pec`` — both read off the *grid*, so they record what
the materials actually were rather than what the job asked for — and, for
a conformal run, ``cut_cells``, ``clamped_faces`` and the
``conformal_area_threshold`` in force. That last one is likewise what *ran*:
building the Simulation measures this grid's small-cut stability and raises the
threshold when it has to (solver S7), since 0.4 is not safe on real geometry and
the failure is not monotone in resolution. When it was raised, the job's own
value is kept alongside as ``conformal_area_threshold_requested`` and
``clamped_faces`` counts the faces clamped at the threshold actually used. A snapshot stores one frame stack per recorded component (``snapshot_<idx>_<comp>_data``, e.g. ``snapshot_0_Ex_data``)
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
transverse sample-coordinate arrays (``mode_<si>_<mi>_ca`` / ``_cb``), so the
workbench draws them on the real grid (uniform or non-uniform) instead of
assuming a constant cell size. Those are **node** coordinates, one per saved
sample: a mode's profiles are node-indexed, not cell-centred (see
:func:`_solve_modes`).

Modal ports
-----------
Each ``modal_ports`` entry names a grid plane (the ``normal`` axis and the
``position`` of the plane along it, in the solver frame) plus the ``face`` it
terminates. The runner calls :func:`wavesim.mode_solver.solve_tem_modes` on that
plane to find the TEM mode of the PEC cross-section, hands it to a
:class:`wavesim.sources.ModalPort` registered with
``Simulation.add_boundary`` (:func:`_build_modal_port`), and saves each solved
mode's 2D field profiles into ``results.npz`` (keys ``mode_<si>_<mi>_phi`` /
``_pec`` / ``_E_<comp>``) with its per-unit-length parameters under
``summary["modes"]``.

**Which mode an entry drives** is set by ``conductor_point`` when present: a
point inside the wanted conductor's cross-section, in solver metres on the two
transverse axes. ``mode_solver._solve_one`` pins φ to exactly 1.0 on the
energized conductor and 0.0 on the others, so the mode with φ ~ 1 there is that
conductor's -- an assignment fixed by the geometry, not by ``ndimage.label``'s
raster ordering, which is what the legacy integer ``conductor_id`` (still read
when no point is given) indexes into. A point matching no mode falls back to the
dominant one with an stderr note.

**Several entries may name one plane.** A multi-conductor port emits one entry
per energized conductor, all on the same face; their sheets superpose on the
shared ghost H plane (``sources._GhostPlaneGroup``), which is how the port
launches ``Σ aᵢ·fᵢ(t)·modeᵢ``. The cross-section is factorised **once per
distinct plane** and the solve reused across those entries. Each entry still
saves the mode it drives as ``mode_<si>_0``, keeping a port's series and its mode
shapes on one ``si``; modes no entry drives are saved after the ones of the
plane's first entry, so nothing solved goes unreported. A mode entry carries
``driven`` (True for the one its port launches) and, when the workbench named
it, the ``conductor`` it energizes.

Each port also **records its own V(t) and I(t)**, saved as ``port_<si>v_times`` /
``_values`` and ``port_<si>i_*`` with the port named under ``summary["ports"]``
(``si`` is the same plane index the ``mode_<si>_<mi>`` arrays carry, which is
what ties a port's series to its mode shapes). Both come from
:class:`wavesim.sources.ModalPort`'s own record and are co-timed at step ``n``:
``V`` is the eps-weighted modal projection of the plane E, ``I`` the Poynting-
paired modal projection of the H plane one cell inside the sheet, signed
**positive into the domain** so ``V*I`` is the power the port delivers inward.
The summary entry also carries the port's ``reference_impedance``
``Z_ref = 1/(s*G)`` -- the impedance that ``(V, I)`` pair is self-consistent
against, so ``a = (V + Z_ref*I)/2`` and ``b = (V - Z_ref*I)/2`` are the incident
and outgoing wave amplitudes (it equals Z0 whenever the admittance scale was
derived rather than overridden) -- and ``sheet_divergence``, the port's own
health check: the transverse divergence of the injected sheet at the *free*
nodes of its plane, which must be at round-off (~1e-14) because the ghost plane
runs open loop and anything left over integrates into a static field on both
port planes. Above the solver's threshold the runner also says so as a status
line, since the solver's own ``warnings.warn`` goes to stderr and the workbench
only surfaces that when a run fails.

A ``ModalPort`` is an **impedance sheet on the face**, not a source on an
interior plane: each step it writes the ghost tangential H just outside the face
to ``±s·(V̄ − 2a)·(n̂ × ê)``, which in one expression radiates a forward wave of
``a`` volts inward *and* absorbs whatever returns. Three consequences here:

* **The face carries no PML and no PEC.** The sheet terminates the mode with no
  reflection and — unlike the propagating-only CPML — no DC error, so a
  DC-containing drive (a Gaussian pulse) drains straight back out through it
  instead of stranding static charge. The workbench strips both the absorber pad
  and the PEC wall from a modal-port face (``domain.modal_port_faces``); the
  termination is local to each port, no far-end load is assumed. The one thing
  the sheet does *not* absorb is field its modal pattern does not describe —
  higher-order modes, or radiation off an open cross-section such as microstrip —
  which now reflects instead of being eaten by a pad. Closed cross-sections
  (coax, stripline) are what this is validated on.
* **The conductors must reach the face.** With no pad and no background gap the
  domain face lands on the geometry's own end, which is exactly what the mode
  solve needs. The runner still nudges the plane one cell inside
  (:func:`_interior_position`): ``grid.axis_index`` returns node ``N`` for a
  top-face plane (out of range for an ``N``-cell axis), and a low-face
  ``ModalPort`` writes its ghost H at ``k-1``, so it needs ``k >= 1``.
* **The mode is solved on the run's own grid.** No mode-mesh refinement, by
  design. The sheet's ``ê`` is a forward difference of φ landed on the Yee edges,
  which is an exact null vector of *this* grid's transverse divergence (the
  property that keeps the launch from depositing charge), and its admittance
  scale ``s = 1/(Z₀·G)`` relies on Z₀ and the discrete modal conductance G
  sharing that same discretisation. Solving on a finer mesh and interpolating
  back destroys both — and the Z₀ so found is not the Z₀ the FDTD grid presents,
  which showed up as artefacts. Refine the simulation grid instead.

With ``mode_only`` true the runner solves and saves the modes and skips the FDTD
time-stepping entirely. The workbench's "Compute Mode" button uses this, sending a
job that carries **only the one port** it wants previewed (it plots the modes and
throws the workdir away); a real run solves every port's mode and keeps them in
its own ``results.npz``/``summary.json``. An
optional ``bounds`` ``[a0,a1,b0,b1]`` (solver metres, transverse slice order)
confines the mode solve to a sub-rectangle of the face — e.g. one connector's
cross-section on a plane that cuts several — and is forwarded straight to
``solve_tem_modes(bounds=...)``; absent it solves on the whole face. The solver
embeds a bounded mode back into the full transverse plane (the port needs that
shape), so the runner crops the *saved* ``mode_*`` profiles and
their ``_ca``/``_cb`` coords back to the solved sub-rect (:func:`_bounds_window`)
— the results plot then shows the bounded region, not a face of zeros around it.
The port's own mode keeps its full shape.

Electrostatics
--------------
With ``mode: "electrostatic"`` the runner solves ``div(eps grad phi) = 0`` on the
same grid, with each named PEC part held at its assigned potential
(:func:`_run_electrostatic`), and never builds a CPML, a source, a port or a time
loop. It reuses the grid because the geometry *is* the same geometry: eps, the
conductors and the cut-cell open fractions all mean here what they mean to the
field solver, so a conformal or a graded run stays conformal or graded. Nothing
in this path reads ``dt``, ``Ex``..``Hz`` or the CFL condition.

Conductors are addressed by name, which ``pec_mask`` alone cannot express (it
answers "is this cell metal?", and a boundary condition has no name). The
workbench labels every PEC body: ``materials.npz`` gains ``pec_id`` and job.json
the matching ``pec_names``.

phi lives on the Yee **nodes**, which is what makes it fit -- the difference of
phi across an edge is that edge's E component. Slices are saved under the same
``snapshot_<i>_<comp>_data`` keys as a time-domain snapshot, as a stack of one
frame, with ``E``/``D`` collocated **to the nodes** (not to cell centres) so that
every quantity shares phi's coordinate grid. Their ``_edges0``/``_edges1`` are
therefore the **dual** cells (:func:`_node_dual_edges`), not ``grid.x``: that
array holds the N+1 boundaries of N cells while these are N samples sitting at
nodes, and drawing one against the other shifts the picture half a cell.

``summary["electrostatic"]`` carries the applied potentials, the per-conductor
charge, the field energy, and -- when asked for -- the capacitance matrix in both
conventions. ``maxwell[i][j]`` is dQ_i/dV_j with every other conductor grounded,
which is what a field solve measures; ``mutual`` is the two-terminal capacitance
people draw between pins. They are routinely confused, so both are named rather
than one being "the" capacitance matrix. Extraction drives one *body* at a time:
two named parts that are one lump of metal cannot be driven independently, so a
representative is chosen and the fusion reported under ``capacitance.fused``.

SPICE co-simulation ports
-------------------------
Each ``spice_ports`` entry couples one FDTD lumped port to a user ngspice netlist
in lockstep (:class:`wavesim.sources.SpicePort`). A ``kind:"line"`` port is a
straight ``p0 -> p1`` line; a ``kind:"tem"`` port drives a solved TEM mode of the
named plane (solved alongside the ``modal_ports`` modes, so it is saved/plotted
like one and honours ``mode_only``). It is a *lumped* drive on an interior plane,
so — unlike a modal port — its face is forced to PML and the plane is clamped one
PML depth in. The ngspice shared library is taken from
``ngspice_dll`` (falling back to a per-port ``library_path`` / PySpice's own
search). Each port records its port V(t)/I(t) into ``results.npz`` (keys
``spice_<idx>_times`` / ``_voltages`` / ``_currents``) with names under
``summary["spice_ports"]``. One netlist drives one port; several ports run
independent ngspice instances. (The port series are stored as two
``_times``/``_values`` pairs — ``spice_<idx>v_*`` for voltage, ``spice_<idx>i_*``
for current.)

Lumped R/L/C ports
------------------
Each ``lumped_ports`` entry is one :class:`wavesim.sources.LineSource` across a
straight ``p0 -> p1`` gap: up to three R/L/C branches wired ``series`` or
``parallel``, with an optional ideal voltage (Thevenin) or current (Norton)
source in company. The reactive branches are integrated with the trapezoidal
companion model (:mod:`wavesim.lumped`), whose every branch resistance is
positive and finite, so **no value of L or C imposes a timestep limit** — a job
that was stable without the element stays stable with it. This is the analytic
sibling of a ``spice_ports`` entry and needs no ngspice; the port series land in
``results.npz`` as ``lumped_<idx>v_*`` / ``lumped_<idx>i_*`` with names and
branch values under ``summary["lumped_ports"]``, exactly like the SPICE ones.

The element contributes **exactly** its own admittance — measured spectrally
solver-side to four figures over 4–30 GHz — so there is nothing for the caller to
pre-compensate. Two discretisation facts the caller does own: the Yee cells the
line bridges keep their own gap capacitance ``C_cell = eps*dA/dl`` (``= dt/kappa``)
in **parallel** with it, which moves with the mesh and is only fixable by meshing;
and two elements sharing a line inject sequentially rather than as one solved
circuit, each then adding its own ``kappa/2`` in series — a single entry with a
``topology`` network is the way to put two branches on one gap.
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

# The conformal (Dey-Mittra) PEC open-fraction arrays in materials.npz, in the
# order wavesim.set_material_arrays takes them. Written by the FreeCAD-side
# voxeliser; all six or none.
_CONFORMAL_KEYS = (
    "pec_edge_open_x", "pec_edge_open_y", "pec_edge_open_z",
    "pec_face_open_x", "pec_face_open_y", "pec_face_open_z",
)

# The electric-conductivity arrays in materials.npz (S/m), in the order
# wavesim.set_material_arrays takes them. All three or none, like the conformal
# set: the solver refuses a partial one, since it would leave one field
# component undamped while the other two decayed.
_SIGMA_KEYS = ("sigma_x", "sigma_y", "sigma_z")


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


def _resolve_backend(requested, conformal=False, lossy=False):
    """Resolve a job's requested backend string to a concrete backend name.

    ``'auto'`` (the workbench default) becomes ``'cuda'`` when a CUDA GPU is
    available, else ``'numba'`` (the multithreaded CPU backend). An explicit
    ``'numpy'``/``'numba'``/``'cuda'`` is honoured unchanged, so a user can force
    the CPU path on a GPU box, or demand the GPU and get a clear solver error if
    it is missing. FreeCAD's Python cannot make this choice (it cannot import
    numba), which is why the ``'auto'`` sentinel is resolved here on the solver
    side rather than when the job is written.

    **Conformal PEC is not implemented on CUDA and the backend refuses it**
    rather than silently staircasing (its H update would integrate the full face
    area while E is masked by the cut geometry — a wrong answer that looks like a
    working run). Since the workbench ships ``'auto'``, every conformal run on a
    GPU box would otherwise die at the first H update, so ``'auto'`` resolves to
    ``'numba'`` for a conformal job. An explicit ``'cuda'`` is left alone: the
    user named the GPU, and the solver's own ``NotImplementedError`` says far
    more than a silent downgrade would.

    **A lossy dielectric is CPU-only** for the same reason: the CUDA E update has
    no ``Ca``/``Cb`` pair, so a grid carrying conductivity would step as though it
    were lossless. The solver refuses it rather than doing that, so ``'auto'``
    resolves to ``'numba'`` here too, and an explicit ``'cuda'`` is again left to
    the solver's own error.
    """
    requested = (requested or "auto").lower()
    if requested != "auto":
        return requested
    if conformal or lossy:
        return "numba"
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
        # read its ``center_frequency`` (it tunes a directional beam / TEM
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


# Energy monitors: solver region -> the base key its series takes in results.npz.
# 'full' keeps the bare "energy" prefix the whole-domain series has always used,
# so older runs and their result leaves still read.
_ENERGY_KEY = {"full": "energy", "interior": "energy_interior"}

# Dissipation monitors, same scheme. No legacy bare-prefix case to preserve —
# this monitor postdates the region split — but the naming mirrors energy's so
# the two series read as the pair they are (U(0) - U(t) = integral of P dt).
_DISSIPATION_KEY = {"full": "dissipation", "interior": "dissipation_interior"}


def _monitor_regions(cfg):
    """The ``region`` values requested by a ``monitors.<name>`` entry.

    *cfg* is either the per-region dict the GUI emits (``{"full": .., "interior":
    ..}``) or a legacy bool, where true meant the one whole-domain monitor.
    Shared by the energy and dissipation monitors, which carry the same
    region/d_pml/faces trio solver-side and are auto-filled by the same hook.
    """
    if isinstance(cfg, dict):
        return [r for r in ("full", "interior") if cfg.get(r)]
    return ["full"] if cfg else []


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

    That includes the solver's one-node reach past ``axis_index``: the profiles
    are node-indexed, and a rect covering cells ``ia0..ia1-1`` stands on nodes
    ``ia0..ia1``. Crop one node short and the saved picture is not the region
    that was solved -- and, worse, the two would disagree only on the far edge,
    which is exactly where the interesting boundary usually is.
    """
    a0, a1, b0, b1 = bounds
    ta = mode.transverse_axes
    shape = mode.phi.shape          # numpy is not imported at module scope here
    ia0, ia1 = grid.axis_index(ta[0], a0), grid.axis_index(ta[0], a1)
    ib0, ib1 = grid.axis_index(ta[1], b0), grid.axis_index(ta[1], b1)
    if ia1 <= ia0 or ib1 <= ib0:
        return None
    return ia0, min(ia1 + 1, shape[0]), ib0, min(ib1 + 1, shape[1])


def _crop_plane(arr, win):
    """Crop a full-plane 2D profile to a :func:`_bounds_window` (no-op if ``None``)."""
    if win is None:
        return arr
    ia0, ia1, ib0, ib1 = win
    return arr[ia0:ia1, ib0:ib1]


def _mode_at_point(np, modes, grid, point, name):
    """Pick the mode that energizes the conductor containing *point*.

    *point* is ``(a, b)`` in solver metres on the mode's two transverse axes --
    the workbench's ``conductor_point``, derived from a point inside that
    conductor's CAD cross-section. ``mode_solver._solve_one`` pins φ to **exactly
    1.0** on the energized conductor and 0.0 on every other one, so the mode whose
    φ is ~1 there *is* the one driving that conductor. That makes the assignment a
    property of the geometry rather than of ``ndimage.label``'s raster ordering,
    which is what the integer ``conductor_id`` had to be read against and why it
    could only be chosen after looking at a solve.

    ``None`` (with an stderr note) when the point lands on no conductor -- a
    conductor that moved off the plane, or a table built against different
    geometry -- so the caller can fall back rather than drive the wrong mode.
    """
    axes = list(getattr(modes[0], "transverse_axes", []))
    if len(axes) != 2:
        return None
    shape = np.asarray(modes[0].phi).shape
    ia = min(max(grid.axis_index(axes[0], float(point[0])), 0), shape[0] - 1)
    ib = min(max(grid.axis_index(axes[1], float(point[1])), 0), shape[1] - 1)
    best, best_phi = None, 0.5   # φ is 1.0 on the energized conductor
    for mode in modes:
        value = float(np.asarray(mode.phi)[ia, ib])
        if value > best_phi:
            best, best_phi = mode, value
    if best is None:
        sys.stderr.write(
            "wavesim: port '{}' points at ({:.6g}, {:.6g}) m on the plane, which "
            "is not inside any solved signal conductor (it may be the grounded "
            "reference, or the geometry may have moved); falling back to the "
            "dominant mode.\n".format(name, point[0], point[1])
        )
    return best


def _choose_mode(np, modes, grid, entry, name):
    """Pick the mode an entry drives: by conductor point, else by label.

    The point (``conductor_point``, written by a port's conductor table) wins
    when present; the integer ``conductor_id`` is the legacy path for a port
    with no table. Either way an unmatched request falls back to the dominant
    (first) mode with an stderr note.
    """
    point = entry.get("conductor_point")
    if point:
        match = _mode_at_point(np, modes, grid, point, name)
        if match is not None:
            return match
        return modes[0]

    wanted = int(entry.get("conductor_id", 0))
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


def _build_modal_port(ws, mode, waveform, grid, face, name):
    """Build the :class:`wavesim.sources.ModalPort` boundary for *mode*.

    A modal port is an **impedance sheet on the domain face**, registered with
    ``Simulation.add_boundary`` so it runs between the H and E updates (it writes
    the ghost tangential H the very next E update consumes; a source hook, which
    runs after the E update, would be clobbered by the following H update). The
    same sheet launches the mode inward and terminates the plane -- with no
    reflection and no DC error, which is why the face needs no PML behind it.

    *face* (``'x0'``..``'z1'``) is passed straight through: ``ModalPort`` derives
    the ghost-H plane index and its sign from it, so nothing here has to encode a
    propagation direction (the old TEM port needed H-sheet surgery for a high
    face; this does not).

    **No amplitude correction.** ``_build_waveform`` already folds the
    excitation's amplitude into ``f(t)``, and ``ModalPort``'s ``amplitude`` is the
    launched *forward-wave* voltage, calibrated by the solver to land one forward
    volt per unit on any grid or fill. So ``amplitude=1.0`` with the amplitude-
    carrying waveform is exactly the contract the workbench documents; scaling
    here would double-count.

    The port is compiled eagerly rather than on the first step, so a plane it
    cannot be built on -- no transverse E energy, a mode solved without ``Z₀``
    (its admittance scale is derived from Z₀ and has no fallback), or a low-face
    port whose ghost-H plane would fall off the grid -- is reported against the
    port's name before time-stepping starts.
    """
    port = ws.ModalPort(mode, amplitude=1.0, waveform=waveform, face=face)
    try:
        port._setup(grid)
    except ValueError as exc:
        raise RuntimeError(
            "Modal port '{}' cannot be built on this plane: {}".format(name, exc)
        )
    return port


def _interior_position(normal, position, grid, d_pml):
    """Nudge a face port's plane onto a cell the solver can actually use.

    A face port's plane sits on the domain boundary node, and the cell it lands
    on is ``grid.axis_index(normal, position)`` -- the *nearest node* used as a
    cell index. That needs clamping at both ends:

    * On a **high** face it returns node ``N``, one past the last cell of an
      ``N``-cell axis, which the mode solver's plane slice cannot index.
    * A low-face :class:`wavesim.sources.ModalPort` writes its ghost H at
      ``k-1``, so its plane must sit at least one cell in.

    *d_pml* is the absorber depth **on this port's own face**, i.e. ``0`` for a
    modal port (its face carries no PML) and the run's ``d_pml`` for a SPICE-TEM
    port, whose lumped launch must not fire into the absorber and whose plane
    must land where the geometry actually is. The clamp is therefore
    ``[max(d_pml, 1), N-1-d_pml]``; for a SPICE port that reproduces
    :class:`wavesim.sources.GaussianBeam`'s ``d_pml`` / ``N-1-d_pml`` convention
    exactly. Returns the clamped cell's node coordinate (or *position* unchanged
    when nothing moved).

    Applying it to *position* up front keeps the solve, the saved profiles and
    the port itself on one populated plane. A no-op for a genuinely interior
    port, whose cell is already well inside the range.
    """
    N = {"x": grid.Nx, "y": grid.Ny, "z": grid.Nz}[normal]
    coords = {"x": grid.x, "y": grid.y, "z": grid.z}[normal]
    k = grid.axis_index(normal, position)
    k_interior = min(max(k, max(d_pml, 1)), N - 1 - d_pml)
    return float(coords[k_interior]) if k_interior != k else position


def _solve_all_modes(ws, np, grid, job):
    """Solve the TEM modes of every modal-port and SPICE-TEM-port plane.

    Returns ``(modal_ports, spice_modes, mode_arrays, mode_meta)``:

    * ``modal_ports`` — one ``(plane_index, name, port)`` triple per
      ``modal_ports`` entry, the port a :class:`wavesim.sources.ModalPort`
      boundary driving the chosen mode (:func:`_build_modal_port`). The plane
      index is the one the ``mode_<si>_<mi>`` arrays are keyed by, so the port's
      recorded V(t)/I(t) can be saved against the same ``si`` as its mode
      shapes;
      empty when ``mode_only``.
    * ``spice_modes`` — ``{job_spice_index: TEMMode}`` giving the chosen mode for
      each ``kind:"tem"`` SPICE port, consumed by :func:`_build_spice_ports`;
      empty when ``mode_only`` (no FDTD to drive).
    * ``mode_arrays`` — the 2D field profiles for ``results.npz``
      (``mode_<si>_<mi>_phi`` / ``_pec`` / ``_E_<comp>``).
    * ``mode_meta`` — per-mode metadata for ``summary["modes"]``.

    Every mode is solved on *grid* itself -- the run's own FDTD grid, honouring
    any ``bounds``. There is deliberately no finer mode mesh: a ``ModalPort``'s
    profile must be a discrete null vector of *this* grid, and a Z₀ measured on a
    different mesh is not the Z₀ the run presents (see the module docstring).

    **Several entries may share one plane.** A multi-conductor modal port emits
    one entry per energized conductor, all on the same face, whose sheets
    superpose there. The cross-section is therefore factorised **once per
    distinct plane** (``normal``/``position``/``bounds``) and the solve reused --
    it is the expensive step and it does not depend on which conductor is being
    driven. Each entry still saves the mode *it* drives under its own ``si``, so
    the invariant that a port's V(t)/I(t) and its mode shapes share one index
    holds per drive row; modes no entry claims are saved against the plane's
    first entry so nothing solved goes unreported.
    """
    mode_only = bool(job.get("mode_only", False))
    # PML depth (cells) of the run. Only a SPICE-TEM port's plane is held that far
    # in: a modal port's own face carries no absorber, so it passes 0 (see
    # _interior_position).
    d_pml = int((job.get("boundary") or {}).get("d_pml", 10))

    # Every plane needing a mode solve: modal ports first, then SPICE TEM ports.
    # ``spice_index`` is the entry's index in job["spice_ports"] (None for modal
    # ports) so the chosen mode can be handed back to _build_spice_ports.
    # ``tem_sources`` is the pre-rename name of ``modal_ports``, still read so an
    # older job.json on disk runs.
    planes = []  # (kind, cfg, spice_index)
    for t in (job.get("modal_ports") or job.get("tem_sources") or []):
        planes.append(("modal", t, None))
    for idx, p in enumerate(job.get("spice_ports") or []):
        if p.get("kind") == "tem":
            planes.append(("spice", p, idx))

    modal_ports = []
    spice_modes = {}
    mode_arrays = {}
    mode_meta = []

    n_ports = len(planes)

    # --- pass 1: describe every entry, and solve each distinct plane once ---- #
    entries = []          # per si: the resolved parameters of that entry
    solved = {}           # plane key -> (modes, win)
    for si, (kind, t, spice_index) in enumerate(planes):
        normal = t.get("normal", "z")
        name = t.get("name", "port")
        # Nudge the plane onto a usable cell *before* solving, so the solve, the
        # saved profiles and the port all sit on the same populated plane. A modal
        # port's face has no absorber, so this only moves it off the very edge
        # (nearest-node lands a high-face plane on node N, past the last cell, and
        # a low-face ModalPort needs a cell at k-1); a SPICE-TEM port is held one
        # full PML depth in, where its lumped launch will not fire into the pad.
        position = _interior_position(
            normal, float(t.get("position", 0.0)), grid,
            d_pml if kind == "spice" else 0)

        # Characteristic frequency/amplitude for the results tree. SPICE ports
        # have no waveform (the circuit drives them), so they carry none, and only
        # they still choose which fields to inject.
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
            fields = ""     # a modal impedance sheet is one-way by construction

        # Optional in-plane bounds (solver-frame metres, transverse slice order).
        bounds = t.get("bounds")
        # Two entries share a solve only if they name the same cell of the same
        # axis with the same sub-rect. ``position`` is already snapped to a node
        # by _interior_position, so equality here is exact, not approximate.
        key = (normal, float(position), tuple(bounds) if bounds else None)
        entries.append({
            "si": si, "kind": kind, "cfg": t, "spice_index": spice_index,
            "normal": normal, "name": name, "position": position,
            "fmax": fmax, "amplitude": amplitude, "fields": fields,
            "bounds": bounds, "key": key,
        })

        if key in solved:
            continue
        prefix = "Port {}/{}: ".format(si + 1, n_ports) if n_ports > 1 else ""
        _emit_status(
            "{}solving TEM mode on the {}-plane of '{}'\n"
            "(factorising the cross-section; this scales with grid "
            "size)...".format(prefix, normal, name)
        )
        # Confine the mode solve to a sub-rectangle of the face when the port
        # carries ``bounds``. Absent => the whole face.
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
        # (the port needs the full transverse shape), padding it with zeros. Crop
        # the **saved** profiles back to the cells actually solved, so the results
        # plot draws the bounded region the user selected instead of a face of
        # zeros around it. The in-memory ``mode`` handed to ``ModalPort`` /
        # ``SpicePort`` below keeps its full shape and is untouched.
        win = _bounds_window(grid, modes[0], bounds) if (bounds and modes) else None
        solved[key] = (modes, win)

    # --- pass 2: which mode does each entry drive? -------------------------- #
    # Done before anything is saved, so the plane's first entry knows which of
    # its modes nobody claimed and can report those too.
    for entry in entries:
        modes, _win = solved[entry["key"]]
        entry["chosen"] = None if (not modes or mode_only) else _choose_mode(
            np, modes, grid, entry["cfg"], entry["name"])

    first_of_plane = {}
    for entry in entries:
        first_of_plane.setdefault(entry["key"], entry["si"])
    claimed = {}   # plane key -> ids of the modes some entry drives
    for entry in entries:
        if entry["chosen"] is not None:
            claimed.setdefault(entry["key"], set()).add(id(entry["chosen"]))

    # --- pass 3: save profiles, and build the ports ------------------------- #
    for entry in entries:
        si, kind, t = entry["si"], entry["kind"], entry["cfg"]
        modes, win = solved[entry["key"]]
        if not modes:
            continue

        # What this entry reports. ``mode_only`` has no chosen mode (there is no
        # FDTD to drive), so the plane's first entry reports every mode -- which
        # is what the panel's "Compute Mode" preview scrolls through. Otherwise
        # each entry leads with the mode it drives, and the plane's first entry
        # also carries the ones no entry took.
        if entry["chosen"] is None:
            reported = list(modes) if si == first_of_plane[entry["key"]] else []
        else:
            reported = [entry["chosen"]]
            if si == first_of_plane[entry["key"]]:
                taken = claimed.get(entry["key"], set())
                reported += [m for m in modes if id(m) not in taken]

        for mi, mode in enumerate(reported):
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
            # Transverse sample coordinates (metres, solver frame) so the results
            # plot can draw the mode on the real (possibly non-uniform) axes
            # rather than assuming a constant da/db spacing.
            #
            # These are the **node** coordinates, not the cell centres: a mode's
            # profiles are node-indexed. ``phi[i, j]`` is the potential *at* node
            # ``(a[i], b[j])`` -- see ``mode_solver.TEMMode.E``, "φ sits on nodes,
            # so consecutive φ are one primary [width] apart", and the
            # ``a_nodes``/``b_nodes`` the solver carries for exactly this. The
            # arrays are still shaped (Na, Nb) like cells, with the plane's last
            # node simply unrepresented, which is what makes the confusion easy:
            # handing back cell centres is one constant half-cell shift on a
            # uniform mesh and so invisible, but on a graded mesh the two rulers
            # are not a rigid shift at all. Drawn on centres, this run's coax pin
            # -- centred on the axis, on the nodes either side of it -- came out
            # 1.25 mm off centre with the outer bore bursting through one corner
            # of the window and a cell clear of the other: a symmetric port that
            # plots skewed.
            t_axes = list(getattr(mode, "transverse_axes", []))
            if len(t_axes) == 2:
                # From the grid the mode was solved on -- the run's own grid --
                # sliced to the same window as the profiles above.
                ca = _axis_nodes(grid, t_axes[0])
                cb = _axis_nodes(grid, t_axes[1])
                if win is not None:
                    ca, cb = ca[win[0]:win[1]], cb[win[2]:win[3]]
                # One coordinate per saved sample. A node array is one longer
                # than the profiles it rules (N+1 nodes, N samples), and the
                # bounded slices above already drop the window's last node; the
                # unbounded case has to drop the plane's. Length is not cosmetic
                # here -- the plot only believes these coordinates when they
                # match the profile shape, and falls back to a uniform da/db
                # ruler when they do not, which is the very thing they exist to
                # replace.
                na, nb = mode_arrays[key + "_phi"].shape
                ca, cb = ca[:na], cb[:nb]
                mode_arrays[key + "_ca"] = np.asarray(ca, dtype=np.float64)
                mode_arrays[key + "_cb"] = np.asarray(cb, dtype=np.float64)
            meta = {
                "source_index": si, "mode_index": mi, "name": entry["name"],
                "conductor_id": int(mode.conductor_id),
                "normal": mode.normal, "position": float(mode.position),
                "transverse_axes": list(mode.transverse_axes),
                "da": float(mode.da), "db": float(mode.db),
                "Ecomps": list(mode.E.keys()),
                "impedance": _f(mode.impedance), "eps_eff": _f(mode.eps_eff),
                "capacitance": _f(mode.capacitance),
                "inductance": _f(mode.inductance),
                "v_phase": _f(mode.v_phase),
                "fmax": entry["fmax"], "amplitude": entry["amplitude"],
                "fields": entry["fields"], "spice": kind == "spice",
                "driven": entry["chosen"] is not None and mi == 0,
            }
            # The entry's conductor names the mode it *drives*, which is the one
            # at mi == 0. The modes after it are the plane's unclaimed ones and
            # belong to other conductors entirely -- carrying this name onto them
            # would label three different modes with one conductor.
            if meta["driven"] and t.get("conductor"):
                meta["conductor"] = str(t["conductor"])
            mode_meta.append(meta)

        chosen = entry["chosen"]
        if chosen is None:
            continue

        # ``position`` was already nudged onto a usable cell before the solve
        # (:func:`_interior_position`), and both launch paths read the chosen
        # mode's own ``position``, so nothing needs moving here.
        if kind == "spice":
            # Hand the chosen mode to _build_spice_ports; the circuit drives it.
            spice_modes[entry["spice_index"]] = chosen
            continue

        # Modal port: an impedance sheet on the face that launches the chosen
        # mode inward and terminates the plane at the same time (no reflection,
        # exact at DC), which is why that face carries no PML. Registered as a
        # boundary, not a source -- see :func:`_build_modal_port`. Several such
        # sheets may share one face: they superpose on the ghost H plane, which
        # is how a multi-conductor port launches a superposition of modes.
        waveform = _build_waveform(ws, t)
        # Legacy jobs carry ``direction`` instead of ``face``; derive the face
        # name from the normal and that sign so an old job.json still runs.
        face = t.get("face") or "{}{}".format(
            entry["normal"],
            "0" if float(t.get("direction", 1.0)) >= 0 else "1")
        modal_ports.append(
            (si, entry["name"],
             _build_modal_port(ws, chosen, waveform, grid, str(face),
                               entry["name"])))

    return modal_ports, spice_modes, mode_arrays, mode_meta


# --------------------------------------------------------------------------- #
# SPICE co-simulation ports — build one SpicePort per spice_ports entry
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Electrostatics
# --------------------------------------------------------------------------- #

# The axis a slice normal indexes into, matching ``_INPLANE_AXES`` above and the
# solver's own ``viz._ES_PLANES``.
_NORMAL_AXIS = {"x": 0, "y": 1, "z": 2}


def _node_dual_edges(np, nodes, centres, n):
    """The ``n+1`` drawing boundaries for *n* node-centred samples.

    Electrostatic quantities live on the Yee **nodes**, not at cell centres, so
    ``grid.x`` is *not* their pcolormesh edge array: that holds the ``n+1``
    boundaries of ``n`` cells, while these are ``n`` samples sitting *at* nodes.
    Drawing one against the other shifts the picture half a cell. Node ``i`` owns
    the half cell either side of it, so the boundaries are the cell centres,
    closed by the two end nodes -- exactly the dual cell the operator integrates
    over, which is what puts a symmetry plane on the edge of the picture rather
    than half a cell inside it. Mirrors ``wavesim.viz._node_edges``.
    """
    nodes = np.asarray(nodes, dtype=np.float64)
    if n < 2:
        return nodes[:2]
    return np.concatenate([nodes[:1], np.asarray(centres, dtype=np.float64)[:n - 1],
                           nodes[n - 1:n]])


def _es_quantities(np, sol, field):
    """``(components, arrays)`` for one electrostatic slice quantity.

    ``phi`` is the potential itself -- the only exact thing in the picture, being
    what was solved for -- and has no components. ``E`` and ``D`` are taken at the
    **nodes** rather than on their Yee edges, so every quantity shares phi's
    coordinate grid and two of these pictures can be compared point for point.
    """
    text = str(field)
    if text.lower().startswith("phi"):
        return ["phi"], [sol.phi]
    if text.upper().startswith("D"):
        return ["Dx", "Dy", "Dz"], list(sol.D_nodes)
    return ["Ex", "Ey", "Ez"], list(sol.E_nodes)


def _run_electrostatic(ws, np, grid, job, workdir, voxel_summary):
    """Solve the electrostatic problem described by *job* and write the results.

    Reuses everything the FDTD path built -- the same grid, the same eps/mu, the
    same conductors, cut cells included -- because the geometry is the same
    geometry. Nothing here reads ``dt``, the field arrays or the CFL condition.
    """
    cfg = job.get("electrostatic") or {}
    potentials = dict(cfg.get("potentials") or {})
    boundary = cfg.get("boundary") or "ground"
    method = str(cfg.get("method", "auto"))

    known = set(getattr(grid, "pec_names", None) or {})
    missing = sorted(set(potentials) - known)
    if missing:
        # Names come from job.json and the labels from materials.npz; they are
        # written together, so a mismatch means the two files are from different
        # builds rather than a user error worth guessing around.
        raise ValueError(
            "job.json assigns a potential to {} but materials.npz labels no such "
            "conductor (it has: {}). Re-run the job, or check that the PEC body "
            "still exists.".format(
                ", ".join(repr(m) for m in missing),
                ", ".join(sorted(known)) or "none"))

    _emit_status("Solving electrostatics ({:,} nodes, {} conductor(s))...".format(
        grid.Nx * grid.Ny * grid.Nz, len(known)))
    es = ws.Electrostatics(grid)
    for name, volts in potentials.items():
        es.set_potential(name, float(volts))
    t0 = time.perf_counter()
    sol = es.solve(boundary=boundary, method=method)
    wall_time = time.perf_counter() - t0

    result_arrays = {}

    # --- slices ---------------------------------------------------------- #
    # Saved under the same ``snapshot_*`` keys the time-domain path uses, as a
    # stack of exactly one frame: the results window already knows how to draw a
    # plane with a component selector and an in-plane quiver, and a static
    # solution is that with the time axis removed rather than a different thing.
    boundary_cfg = job.get("boundary") or {}
    d_pml = int(boundary_cfg.get("d_pml", 0))
    pml_set = set(boundary_cfg.get("faces") or ())

    def _pad(axis):
        return (d_pml if (axis + "0") in pml_set else 0,
                d_pml if (axis + "1") in pml_set else 0)

    snapshot_meta = []
    for idx, spec in enumerate(cfg.get("slices") or []):
        normal = str(spec.get("normal", "z"))
        n_axis = _NORMAL_AXIS.get(normal, 2)
        comps, fields = _es_quantities(np, sol, spec.get("field", "phi"))
        shape = sol.phi.shape
        # ``axis_index`` can return the N-th node, which the solution does not
        # carry (phi has one sample per cell, not per grid line).
        k = min(int(grid.axis_index(normal, float(spec.get("position", 0.0)))),
                shape[n_axis] - 1)
        ax0, ax1 = _INPLANE_AXES.get(normal, ("x", "y"))
        edges0 = _node_dual_edges(np, _axis_nodes(grid, ax0),
                                  _axis_centers(grid, ax0),
                                  shape[_NORMAL_AXIS[ax0]])
        edges1 = _node_dual_edges(np, _axis_nodes(grid, ax1),
                                  _axis_centers(grid, ax1),
                                  shape[_NORMAL_AXIS[ax1]])
        # Strip the absorber padding, as the time-domain path does: those cells
        # are plain background here, but they are still outside the box the user
        # drew, and showing them would put the grounded wall in the wrong place.
        (lo0, hi0), (lo1, hi1) = _pad(ax0), _pad(ax1)
        n0, n1 = shape[_NORMAL_AXIS[ax0]], shape[_NORMAL_AXIS[ax1]]
        stop0, stop1 = n0 - hi0, n1 - hi1
        crop = stop0 > lo0 and stop1 > lo1
        saved = []
        for comp, arr in zip(comps, fields):
            plane = np.take(np.asarray(arr, dtype=np.float64), k, axis=n_axis)
            if crop:
                plane = plane[lo0:stop0, lo1:stop1]
            result_arrays["snapshot_{}_{}_data".format(idx, comp)] = \
                plane[np.newaxis, ...]
            saved.append(comp)
        if crop:
            edges0 = edges0[lo0:stop0 + 1]
            edges1 = edges1[lo1:stop1 + 1]
        result_arrays["snapshot_{}_times".format(idx)] = np.zeros(1)
        result_arrays["snapshot_{}_edges0".format(idx)] = edges0
        result_arrays["snapshot_{}_edges1".format(idx)] = edges1
        field = "phi" if len(saved) == 1 else saved[0][0]
        snapshot_meta.append({
            "name": spec.get("name", "slice"),
            "field": field,
            "components": saved,
            # A scalar has no in-plane vector to overlay.
            "inplane": [] if len(saved) == 1 else [field + ax0, field + ax1],
            "frames": 1,
        })

    # --- integrals ------------------------------------------------------- #
    # Charges come from the same face coefficients the operator was assembled
    # from, so the reported charge is exactly the flux the solved equations
    # balanced -- not a second discretisation of the same integral.
    charges = {}
    for name in sorted(known):
        try:
            charges[name] = float(sol.charge(name))
        except KeyError:
            # Named, but occupying no conductor body: every cell it would have
            # owned was overwritten by a body placed after it.
            charges[name] = 0.0

    es_summary = {
        "potentials": potentials,
        "charges": charges,
        "energy": float(sol.energy),
        "method": sol.method,
        "iterations": int(sol.iterations),
        "unknowns": int(sol.n_unknowns),
        "grounded_bodies": int(sol.grounded_bodies),
        "boundary": sol.boundary if isinstance(sol.boundary, dict) else boundary,
    }

    # --- capacitance matrix ---------------------------------------------- #
    if cfg.get("capacitance"):
        # One conductor per *body*: two named parts that turn out to be the same
        # lump of metal cannot be driven independently, and the solver refuses
        # the pair rather than reporting a capacitance between them. Picking a
        # representative here turns that refusal into a run that still produces
        # the matrix, and names the fusion in the summary.
        bodies = ws.body_parts(grid)
        names, fused = [], []
        for group in bodies:
            group = sorted(group)
            if not group:
                continue
            names.append(group[0])
            if len(group) > 1:
                fused.append(group)
        if len(names) >= 2:
            _emit_status(
                "Extracting the capacitance matrix ({} conductors, {} solves)..."
                .format(len(names), len(names)))
            cap = ws.capacitance_matrix(grid, names, boundary=boundary,
                                        method=method)
            result_arrays["capacitance_maxwell"] = np.asarray(cap.maxwell)
            es_summary["capacitance"] = {
                "names": list(cap.names),
                "maxwell": [[float(v) for v in row] for row in cap.maxwell],
                "mutual": [[float(v) for v in row] for row in cap.mutual()],
            }
            if fused:
                es_summary["capacitance"]["fused"] = fused
        else:
            es_summary["capacitance_skipped"] = (
                "fewer than two independent conductors: {} named part(s) form "
                "{} conductor body(s)".format(len(known), len(names)))

    _emit_status("Saving electrostatic results...")
    np.savez(os.path.join(workdir, "results.npz"), **result_arrays)

    summary = {
        "ok": True,
        "mode": "electrostatic",
        "steps": 0,
        "dt": float(grid.dt),
        "sim_time_s": 0.0,
        "wall_time_s": wall_time,
        "Nx": int(grid.Nx), "Ny": int(grid.Ny), "Nz": int(grid.Nz),
        "subpixel": bool(job.get("subpixel", False)),
        "conformal_pec": bool(grid.is_conformal),
        "electrostatic": es_summary,
    }
    summary.update(voxel_summary)
    if snapshot_meta:
        summary["snapshots"] = snapshot_meta
    with open(os.path.join(workdir, "summary.json"), "w",
              encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
    _emit_progress(1, 1)
    return summary


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
# Lumped R/L/C ports — one LineSource per lumped_ports entry
# --------------------------------------------------------------------------- #

def _build_lumped_ports(ws, job):
    """Build a :class:`wavesim.sources.LineSource` for each ``lumped_ports`` entry.

    Returns a list of ``(name, LineSource, entry)`` — the job entry travels with
    the port so the summary can report the network without reaching into the
    solver object's internals. An entry with neither a branch nor
    a drive is skipped with a note rather than allowed to raise out of the run:
    the solver refuses that combination, and it means a port the user has not
    finished configuring, not a job that should die at step 0. The branches are
    passed through as keywords, so an omitted one stays *absent* (a short in
    series, an open in parallel) instead of becoming a zero the solver would
    reject.
    """
    ports = []
    for e in job.get("lumped_ports") or []:
        name = e.get("name", "lumped")
        load = {key: float(e[key])
                for key in ("resistance", "inductance", "capacitance")
                if e.get(key) is not None}
        drive = str(e.get("drive", "none"))
        if not load and drive == "none":
            sys.stderr.write(
                "wavesim: lumped port '{}' has neither a load nor a drive; "
                "skipping.\n".format(name))
            continue
        kwargs = dict(load)
        kwargs["topology"] = str(e.get("topology", "series"))
        if drive in ("voltage", "current"):
            kwargs[drive] = _build_waveform(ws, e)
        try:
            port = ws.LineSource(p0=tuple(e["p0"]), p1=tuple(e["p1"]), **kwargs)
        except Exception as err:
            sys.stderr.write(
                "wavesim: lumped port '{}' could not be built ({}); "
                "skipping.\n".format(name, err))
            continue
        ports.append((name, port, e))
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
    # The conformal and lossy flags have to come from the job, not from
    # materials.npz: this runs before the arrays are opened, because the backend
    # choice is what sets the grid dtype. The workbench writes both as what
    # actually got voxelised, not as what the checkbox said, so they are safe to
    # key on. Both rule out the GPU (its kernels implement neither).
    mode_only = bool(job.get("mode_only", False))
    electrostatic = str(job.get("mode", "fdtd")) == "electrostatic"
    conformal = bool(job.get("conformal_pec", False))
    lossy = bool(job.get("lossy", False))
    # An electrostatic job never enters the time loop either: it assembles a
    # sparse operator and hands it to scipy, which wants float64 and has no
    # backend to choose. Treated like ``mode_only`` for exactly that reason.
    backend = ("numba" if mode_only or electrostatic
               else _resolve_backend(job.get("backend", "auto"), conformal,
                                     lossy))
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
    materials_path = os.path.join(workdir, "materials.npz")
    voxel_summary = {}
    if os.path.isfile(materials_path):
        data = np.load(materials_path)
        pec_mask = data["pec_mask"] if "pec_mask" in data.files else None
        # Conformal (Dey-Mittra) PEC open fractions: all six or none, which the
        # solver enforces (a partial set would mix conformal edges with staircase
        # faces, and E and H would see different conductors). Absent -> every
        # existing path is untouched and bit-identical. Left in float64 whatever
        # the field dtype is: they are geometry, not field data, and the solver
        # multiplies them by its own float64 spacing arrays.
        conformal_arrays = {}
        if all(key in data.files for key in _CONFORMAL_KEYS):
            conformal_arrays = {key: data[key] for key in _CONFORMAL_KEYS}
        elif any(key in data.files for key in _CONFORMAL_KEYS):
            raise ValueError(
                "materials.npz carries an incomplete set of conformal PEC "
                "arrays: {}. All six or none.".format(
                    sorted(set(_CONFORMAL_KEYS) & set(data.files))))
        # Electric conductivity (lossy dielectrics). Cast like eps/mu so the
        # coefficient build stays in the field dtype. Absent -> the solver's
        # one-coefficient E update, bit-identical to a pre-loss run; present ->
        # the Ca/Cb pair. All three or none, checked here so a truncated npz is
        # named rather than surfacing as a solver-side shape error.
        sigma_arrays = {}
        if all(key in data.files for key in _SIGMA_KEYS):
            sigma_arrays = {
                key: data[key].astype(field_dtype, copy=False)
                for key in _SIGMA_KEYS
            }
        elif any(key in data.files for key in _SIGMA_KEYS):
            raise ValueError(
                "materials.npz carries an incomplete set of conductivity "
                "arrays: {}. All three or none.".format(
                    sorted(set(_SIGMA_KEYS) & set(data.files))))
        # Cast to the grid's dtype so the field and material arrays stay
        # matched — the CUDA backend keys its per-cell arithmetic and scalar
        # coefficients off the field dtype, so a float32 grid needs float32
        # eps/mu to do genuine single-precision math (the arrays are written as
        # float64 by the FreeCAD-side voxeliser).
        # Named PEC parts: the label array pairs with the ``pec_names`` map in
        # job.json (the array is bulk and belongs in the npz, the names are
        # metadata and belong in the job). Both together or neither, which the
        # solver enforces -- a label outside the mask would describe metal the
        # field solver cannot see. Written only by an electrostatic job; the FDTD
        # path reads neither.
        part_arrays = {}
        pec_names = job.get("pec_names") or {}
        if "pec_id" in data.files and pec_names:
            part_arrays = {"pec_id": data["pec_id"],
                           "pec_names": {str(k): int(v)
                                         for k, v in pec_names.items()}}
        grid = ws.set_material_arrays(
            grid,
            data["eps_x"].astype(field_dtype, copy=False),
            data["eps_y"].astype(field_dtype, copy=False),
            data["eps_z"].astype(field_dtype, copy=False),
            data["mu_x"].astype(field_dtype, copy=False),
            data["mu_y"].astype(field_dtype, copy=False),
            data["mu_z"].astype(field_dtype, copy=False),
            pec_mask=pec_mask,
            conformal_area_threshold=(
                float(job["conformal_area_threshold"])
                if conformal_arrays and "conformal_area_threshold" in job
                else None),
            **part_arrays,
            **conformal_arrays,
            **sigma_arrays
        )
        if pec_mask is not None:
            voxel_summary["pec_cells"] = int(np.count_nonzero(pec_mask))
        # Conductor cells are excluded: on a conformal run the voxeliser fills
        # them with the material of the medium beside them (so the live edges
        # hugging the surface read the right eps), and counting those as
        # dielectric would inflate this by the whole volume of every conductor.
        dielectric = data["eps_x"] != 1.0
        if pec_mask is not None:
            dielectric = dielectric & ~pec_mask
        voxel_summary["dielectric_cells"] = int(np.count_nonzero(dielectric))
        if sigma_arrays:
            voxel_summary["lossy_cells"] = int(
                np.count_nonzero(data["sigma_x"] > 0.0))
            voxel_summary["max_sigma"] = float(data["sigma_x"].max())
        if grid.is_conformal and backend == "cuda":
            # The solver's own guard (backend_cuda._refuse_conformal) sits in the
            # per-call update_H wrapper, but Simulation.run('cuda') dispatches to
            # the resident-memory path (CudaResident) and never reaches it — so a
            # cut-cell grid runs on the GPU staircased, with H integrating the
            # full face area while E is masked by the cut geometry. That is a
            # silently wrong answer, which is the one outcome the guard exists to
            # prevent, so refuse here too rather than trust it. 'auto' never gets
            # this far (see _resolve_backend); reaching it means the backend was
            # asked for by name.
            raise NotImplementedError(
                "backend='cuda' cannot run conformal (Dey-Mittra) PEC: the GPU "
                "kernels only implement the staircase H update, so the run "
                "would be silently wrong rather than merely slow. Use "
                "backend='numba' (or 'numpy'), or turn off the Simulation's "
                "ConformalPEC to run staircased.")
        if grid.is_lossy and backend == "cuda":
            # Unlike the conformal case above, the solver's own guard does cover
            # the resident path (backend_cuda._refuse_lossy is called from
            # CudaResident.__init__), so this is not closing a hole — it is
            # raising before the grid is uploaded, and saying what to do about it
            # in the workbench's own terms. 'auto' never reaches here.
            raise NotImplementedError(
                "backend='cuda' cannot run lossy dielectrics: the GPU E update "
                "has no Ca/Cb coefficient pair, so a grid carrying conductivity "
                "would step as though it were lossless. Pick 'numba' (or "
                "'numpy') in Wavesim -> Settings, or clear the Sigma of every "
                "material to run the model lossless deliberately.")
        if grid.is_conformal:
            # Provisional: building the Simulation below measures this grid's
            # stability and may raise the threshold (solver S7), which moves
            # both the threshold and the clamped-face count. They are re-recorded
            # there. Recorded here as well so a mode-only request — which returns
            # before any time loop exists, and so has no stability question to
            # answer — still reports them.
            voxel_summary["cut_cells"] = int(ws.count_cut_cells(grid))
            voxel_summary["conformal_area_threshold"] = float(
                grid.conformal_area_threshold)
            # Every clamped face is a cut the run only partly resolved, and a
            # large share of them is the signature of the small-cut instability.
            voxel_summary["clamped_faces"] = int(
                ws.conformal_geometry(grid).n_clamped)
            _emit_status(
                "Conformal PEC: {:,} cut cells, {:,} clamped faces "
                "(threshold {:.2f})".format(
                    voxel_summary["cut_cells"], voxel_summary["clamped_faces"],
                    grid.conformal_area_threshold))

    # Electrostatics: a different question about the same geometry. Taken here,
    # after the materials are on the grid (the solve reads eps, the conductors
    # and the cut-cell fractions) and before anything that assumes a time axis --
    # there are no ports to solve, no absorber to build and no loop to step.
    if electrostatic:
        return _run_electrostatic(ws, np, grid, job, workdir, voxel_summary)

    # Ports: solve each port plane's transverse mode. Done after the materials are
    # loaded (the mode solve reads the grid's own eps/mu/PEC) and before the FDTD
    # setup, so the modal-port boundaries exist for the Simulation below and a
    # mode-only request can return without building the time loop.
    modal_ports, spice_modes, mode_arrays, mode_meta = _solve_all_modes(
        ws, np, grid, job
    )
    modal_boundaries = [port for _si, _name, port in modal_ports]

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
    # only fall back to all-six when the key is absent entirely. A modal-port
    # face appears in neither list -- the port terminates it itself, so the
    # workbench strips it from both (see domain.domain_grid_params).
    cpml = None
    boundary = job.get("boundary") or {}
    pml_faces = boundary.get("faces", list(ws.ALL_FACES))
    if pml_faces:
        cpml = ws.init_cpml(
            grid, d_pml=int(boundary.get("d_pml", 10)), faces=tuple(pml_faces)
        )
    pec_faces = tuple(boundary.get("pec_faces") or ())

    # Sources: an optional soft point excitation. ``source`` may be null when the
    # excitation comes entirely from the ports. Modal ports are *not* sources --
    # they go in ``boundaries=`` below, so they run between the H and E updates.
    sources = []
    s = job.get("source")
    if s:
        waveform = _build_waveform(ws, s)
        sources.append(ws.PointSource(
            s["component"], float(s["x"]), float(s["y"]), float(s["z"]), waveform
        ))

    # Boundary Gaussian beams: a one-way beam launched from a boundary face, one
    # PML-depth inside it. The E sheet is placed at cell ``d_pml`` (low face) or
    # ``N-1-d_pml`` (high face), so it sits on the first interior cell of the
    # forced-PML launch face and its backward lobe is absorbed. ``d_pml`` comes
    # from the run's boundary so the sheet matches the actual absorber thickness,
    # and the solver zeroes the sheet that same depth off every transverse edge.
    # ``waist`` is w0 in metres, at the launch plane; the workbench has already
    # resolved it to a positive number (the solver rejects anything else).
    for beam in job.get("gaussian_beams") or []:
        waveform = _build_waveform(ws, beam)
        sources.append(ws.GaussianBeam(
            str(beam["face"]),
            math.radians(float(beam.get("angle_deg", 0.0))),
            waveform,
            float(beam["waist"]),
            d_pml=int(boundary.get("d_pml", 10)),
            directional=bool(beam.get("directional", True)),
        ))

    # SPICE co-simulation ports: one live ngspice instance each, driven in
    # lockstep with the FDTD loop. Kept aside so their port records can be saved
    # and their ngspice instances torn down after the run.
    spice_ports = _build_spice_ports(ws, job, spice_modes)
    sources.extend(port for _name, port in spice_ports)

    # Lumped R/L/C ports: the same kind of element, with the circuit solved
    # analytically here instead of by ngspice. Kept aside for the same reason --
    # each records the port V(t)/I(t) saved after the run.
    lumped_ports = _build_lumped_ports(ws, job)
    sources.extend(port for _name, port, _entry in lumped_ports)

    # Monitors. Probes and snapshots (Session 7) are point/plane recorders
    # described in the job. All locations are already in the solver frame
    # (origin baked into the voxel arrays).
    mon_cfg = job.get("monitors", {})

    # Energy: one solver monitor per requested region -- 'full' sums the entire
    # grid, 'interior' only the physical domain, with the PML cells dropped. The
    # solver takes the PML geometry off the CPML this run is built with, so the
    # interior monitor needs nothing from us but the region name.
    energy = [(region, ws.EnergyMonitor(region=region))
              for region in _monitor_regions(mon_cfg.get("energy", True))]

    # Dissipation: P = sum(sigma*|E|^2*dV), the ohmic power a lossy dielectric
    # absorbs -- the term that makes the energy series legitimately decay.
    # Region-selected exactly like energy ('interior' is usually what is wanted:
    # the PML shell dissipates by design and would swamp the material's own
    # loss). The solver records 0.0 on a lossless grid rather than refusing.
    dissipation = [(region, ws.DissipationMonitor(region=region))
                   for region in _monitor_regions(mon_cfg.get("dissipation",
                                                              False))]

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

    all_monitors = [m for _region, m in energy]
    all_monitors.extend(m for _region, m in dissipation)
    all_monitors.extend(m for _name, m in probes)
    all_monitors.extend(snapshot_solver_monitors)
    all_monitors.extend(m for _name, m in voltages)
    all_monitors.extend(m for _name, m in currents)

    # ``boundaries`` run between the H and E updates, unlike sources (after E).
    # A modal port sets the ghost tangential H that the very next E update
    # consumes, so a source hook would be clobbered before it was ever read.
    sim = ws.Simulation(
        grid,
        cpml=cpml,
        sources=sources,
        boundaries=modal_boundaries,
        monitors=all_monitors,
        pec_faces=pec_faces,
        backend=backend,
    )

    if grid.is_conformal:
        # Only now is the clamp threshold settled. Constructing the Simulation
        # probes this exact scheme for the small-cut instability and raises the
        # threshold if it has to (solver S7), because 0.4 is not safe on real
        # geometry and stability is not monotone in resolution — the reference
        # coax diverges at a 0.25 mm cell and runs at 0.1875 mm. What the run
        # used is what belongs in the summary; what the job asked for is exactly
        # the thing that can go unhonoured.
        requested = voxel_summary["conformal_area_threshold"]
        used = float(grid.conformal_area_threshold)
        if used != requested:
            voxel_summary["conformal_area_threshold"] = used
            voxel_summary["conformal_area_threshold_requested"] = requested
            voxel_summary["clamped_faces"] = int(
                ws.conformal_geometry(grid).n_clamped)
            _emit_status(
                "Conformal PEC: clamp threshold raised {:.2f} -> {:.2f} - the "
                "run diverges at {:.2f}. {:,} faces are now clamped, which "
                "costs accuracy.".format(requested, used, requested,
                                         voxel_summary["clamped_faces"]))

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
    for region, mon in energy:
        result_arrays[_ENERGY_KEY[region] + "_times"] = np.asarray(mon.times)
        result_arrays[_ENERGY_KEY[region] + "_values"] = np.asarray(mon.values)
    for region, mon in dissipation:
        key = _DISSIPATION_KEY[region]
        result_arrays[key + "_times"] = np.asarray(mon.times)
        result_arrays[key + "_values"] = np.asarray(mon.values)

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

    # Modal ports: the V(t)/I(t) each ModalPort recorded at its own sheet, saved
    # in the same two-``_times``/``_values``-pairs shape as a SPICE port so the
    # results tree reuses the shared 1-D plotter. Keyed by the port's *plane*
    # index, which is what ``mode_<si>_<mi>`` uses -- that is the only link
    # between a port's series and its mode shapes.
    #
    # Both series are the port's own record and are co-timed at step n: V is the
    # eps-weighted modal projection of the plane E, I the Poynting-paired
    # projection of the H plane one cell inside the sheet, positive *into* the
    # domain. ``reference_impedance`` (= 1/(s*G), the impedance that pair is
    # self-consistent against, so a = (V + Z_ref*I)/2 is the incident wave) is
    # set when the port compiles, hence always present here.
    #
    # ``sheet_divergence`` is the port's own health check, and the one number
    # that says whether the run behind it can be trusted at all: the injected
    # sheet must have zero transverse divergence at the plane's *free* nodes,
    # because the ghost plane runs open loop and anything left over integrates
    # into a static field on both port planes rather than radiating away. The
    # solver measures it when the port compiles and warns on its own — but a
    # Python warning goes to stderr, which the workbench only surfaces when a run
    # fails, so it is echoed here as a status line and stored in the summary.
    # Round-off is ~1e-14 and a real failure ~1e-1, so the threshold is not a
    # tuning knob; it comes from the solver so there is one owner of it.
    try:
        from wavesim.mode_solver import _SHEET_DIVERGENCE_TOL as _DIV_TOL
    except ImportError:          # a solver predating the measurement
        _DIV_TOL = None
    port_meta = []
    for si, name, port in modal_ports:
        times = np.asarray(port.times)
        result_arrays["port_{}v_times".format(si)] = times
        result_arrays["port_{}v_values".format(si)] = np.asarray(port.voltages)
        result_arrays["port_{}i_times".format(si)] = times
        result_arrays["port_{}i_values".format(si)] = np.asarray(port.currents)
        divergence = getattr(port, "sheet_divergence", None)
        port_meta.append({
            "name": name, "source_index": si, "kind": "modal",
            "reference_impedance": _f(getattr(port, "reference_impedance", None)),
            "modal_conductance": _f(getattr(port, "modal_conductance", None)),
            "sheet_divergence": _f(divergence),
        })
        if _DIV_TOL is not None and divergence is not None \
                and divergence > _DIV_TOL:
            _emit_status(
                "Port '{}': the injected modal sheet has a transverse "
                "divergence of {:.3g} (round-off is ~1e-14). It will pile a "
                "static field onto both port planes instead of radiating. The "
                "usual cause is a permittivity map that disagrees with its own "
                "cut-cell geometry - check eps_x/eps_y/eps_z against "
                "pec_edge_open_*.".format(name, divergence))

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

    # Lumped R/L/C ports: the same two series, under their own key prefix. The
    # branch values ride in the summary so a results window can say what the
    # element *was* without reopening job.json.
    lumped_meta = []
    for idx, (name, port, cfg) in enumerate(lumped_ports):
        times = np.asarray(port.times)
        result_arrays["lumped_{}v_times".format(idx)] = times
        result_arrays["lumped_{}v_values".format(idx)] = np.asarray(port.voltages)
        result_arrays["lumped_{}i_times".format(idx)] = times
        result_arrays["lumped_{}i_values".format(idx)] = np.asarray(port.currents)
        meta = {"name": name,
                "topology": str(cfg.get("topology", "series")),
                "drive": str(cfg.get("drive", "none"))}
        for key in ("resistance", "inductance", "capacitance"):
            if cfg.get(key) is not None:
                meta[key] = float(cfg[key])
        # kappa is the element's own model of the grid it landed on. It is *not*
        # a series parasitic (the element delivers exactly its own admittance);
        # what it buys is the gap capacitance dt/kappa = eps*dA/dl the bridged
        # cells keep in parallel, which is what a user comparing the recorded Z
        # against a nominal R/L/C needs, and which moves with the mesh.
        try:
            kappa = float(port.self_coupling(grid))
        except Exception:
            kappa = None
        if kappa:
            meta["kappa"] = kappa
            meta["c_cell"] = float(grid.dt) / kappa
        lumped_meta.append(meta)

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
        # Read off the grid, not the job: this is what the run's conductors
        # actually were. The job's request can go unhonoured (an incomplete or
        # absent array set falls back to staircase), and a stored result that
        # claims conformal when it staircased is worse than no field at all.
        "conformal_pec": bool(grid.is_conformal),
        # Likewise off the grid: whether this run stepped the lossy E update.
        "lossy": bool(grid.is_lossy),
    }
    summary.update(voxel_summary)
    # Per recorded region: <key>_final / <key>_max, so a run recording both the
    # whole grid and the interior reports each ("energy_*" is the whole grid).
    for region, mon in energy:
        if mon.values:
            summary[_ENERGY_KEY[region] + "_final"] = float(mon.values[-1])
            summary[_ENERGY_KEY[region] + "_max"] = float(max(mon.values))
    # Dissipation reports a third number the energy series has no analogue for:
    # the time integral of the power, i.e. the joules the lossy material
    # actually absorbed over the run. That is the quantity to compare against
    # U(0) - U(t), and it is the monitor's own trapezoid so the two agree.
    for region, mon in dissipation:
        if mon.values:
            key = _DISSIPATION_KEY[region]
            summary[key + "_final"] = float(mon.values[-1])
            summary[key + "_max"] = float(max(mon.values))
            summary[key + "_energy"] = float(mon.energy())
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
    if lumped_meta:
        summary["lumped_ports"] = lumped_meta
    if port_meta:
        summary["ports"] = port_meta
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
