# -*- coding: utf-8 -*-
"""Gate: the ``lumped_ports`` cross-process contract, end to end.

Run under the **solver conda Python** (it needs numpy + ``wavesim``), not
FreeCAD's::

    "%USERPROFILE%\\miniconda3\\envs\\wavesim\\python.exe" \\
        tools/check_lumped_port.py

Writes a ``job.json`` holding four lumped ports into a scratch workdir, runs
``runner.run_job`` on it, and checks the saved ``lumped_<i>v_*``/``_i_*`` series
against the element's **own terminal law**. That is what pins down the whole
chain: a branch value that arrived under the wrong keyword, a topology that was
dropped, or an excitation that never reached the waveform builder all show up
here as a residual of order one rather than of order 1e-16.

What the laws are, and why kappa is absent from them
----------------------------------------------------
The recorded ``V`` is the post-injection line voltage ``V_n`` and the recorded
``I`` is the impressed current at ``n+1/2``, so the voltage *across the element*
at that instant is the port mid-value ``(V_{n-1} + V_n)/2``. The element
contributes exactly its own admittance — there is no ``kappa/2`` in series, and
nothing about ``kappa`` enters the recorded pair. Hence:

* resistor (single branch, ``Z_eq = R``)::

      I_n = -(V_{n-1} + V_n) / (2R)

* capacitor (``Z = dt/2C``, trapezoidal history source ``h``)::

      h_n = (V_{n-1} + V_n)/2 + Z*I_n      (from the port law)
      h_{n+1} = h_n - 2*Z*I_n              (the trapezoidal update), h_0 = 0

* Thevenin drive with a resistor::

      I_n = (Vs(t_n) - (V_{n-1} + V_n)/2) / R

One port carries neither a branch nor a drive and must be **skipped** by both
sides (the solver refuses that combination, and it means a half-configured port
rather than a job worth killing at step 0).

The placement rule, checked as an identity
------------------------------------------
``kappa`` is not a series parasitic; it is the bridged cells' own gap
capacitance, ``C_cell = dt/kappa``. What makes it worth a gate is the path
quadrature: an edge's weight is the length of the line **inside that edge's own
cell**, and the injection divides by the *dual* Ampere face across the edge. So a
path running node to node puts the same current through every cell it crosses,
whatever the grading — that is what lets an element span uneven cells at all. Two
consequences, both asserted below against ``LineSource.self_coupling``:

* ends on **nodes** (a conductor surface the mesher put a grid line on, the
  normal case) give every crossed cell its full width, so
  ``kappa == dt*L/(eps*dA)`` exactly;
* ends **half a cell in** cover the two end cells only halfway. Since kappa sums
  ``w^2/h``, each half end contributes ``h/4`` rather than ``h``, so a span from
  one cell centre to another two cells away reads ``1.5x`` the whole-cell value
  — the reason the workbench no longer moves the endpoints it is given.
"""

import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))          # the workbench (runner.py)

import numpy as np

import runner


def _wavesim_repo():
    """The directory holding the ``wavesim`` package, or ``None``.

    ``WAVESIM_PATH`` first (which ``runner._ensure_wavesim_importable`` honours
    too), else the workbench's own settings file — the same precedence the GUI
    uses, so this script carries **no machine-specific path**.
    """
    repo = os.environ.get("WAVESIM_PATH")
    if repo and os.path.isdir(repo):
        return repo
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    import glob

    pattern = os.path.join(appdata, "FreeCAD", "**", "wavesim_settings.json")
    for path in glob.glob(pattern, recursive=True):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                repo = json.load(fh).get("wavesim_path")
        except Exception:
            continue
        if repo and os.path.isdir(repo):
            return repo
    return None


DX = 0.5e-3
N = 24
R_PASSIVE = 50.0
R_DRIVEN = 25.0
C = 2.0e-13
TOL = 1.0e-9
# wavesim.constants.EPS0's own (truncated) value, so the kappa identities below
# are not decided by the ninth digit of a constant.
EPS0 = 8.8541878e-12
# The kappa identities are analytic but reach through a quadrature that sub-steps
# the path, so they close to round-off rather than exactly.
KAPPA_TOL = 1.0e-6


def _job():
    """A small vacuum box, a point source, and the four ports under test."""
    return {
        "backend": "numba",
        "steps": 300,
        "grid": {"Nx": N, "Ny": N, "Nz": N, "dx": DX, "dy": DX, "dz": DX},
        "boundary": {"d_pml": 6,
                     "faces": ["x0", "x1", "y0", "y1", "z0", "z1"],
                     "pec_faces": []},
        "source": {"component": "Ez",
                   "x": 12 * DX, "y": 12 * DX, "z": 12 * DX,
                   "excitation": {"type": "gaussian", "fmax": 60.0e9,
                                  "amplitude": 1.0}},
        "lumped_ports": [
            {"name": "R port", "topology": "series", "drive": "none",
             "resistance": R_PASSIVE,
             "p0": [9 * DX, 12 * DX, 12 * DX],
             "p1": [9 * DX, 12 * DX, 13 * DX]},
            {"name": "C port", "topology": "series", "drive": "none",
             "capacitance": C,
             "p0": [15 * DX, 12 * DX, 12 * DX],
             "p1": [15 * DX, 12 * DX, 13 * DX]},
            {"name": "driven", "topology": "series", "drive": "voltage",
             "resistance": R_DRIVEN,
             "excitation": {"type": "gaussian", "fmax": 40.0e9,
                            "amplitude": 2.0},
             "p0": [12 * DX, 9 * DX, 12 * DX],
             "p1": [12 * DX, 9 * DX, 13 * DX]},
            # Ends half a cell in, covering the two end cells only halfway --
            # what the workbench used to produce, kept as the second identity.
            {"name": "half-cell R port", "topology": "series", "drive": "none",
             "resistance": R_PASSIVE,
             "p0": [16 * DX, 16 * DX, 12.5 * DX],
             "p1": [16 * DX, 16 * DX, 14.5 * DX]},
            {"name": "unconfigured", "topology": "series", "drive": "none",
             "p0": [12 * DX, 15 * DX, 12 * DX],
             "p1": [12 * DX, 15 * DX, 13 * DX]},
        ],
        "monitors": {},
    }


def _report(label, residual, extra="", tol=TOL):
    ok = residual < tol
    print("  {:14s} residual = {:9.3g}   {}{}".format(
        label, residual, "OK " if ok else "FAIL", extra))
    return ok


def main():
    repo = _wavesim_repo()
    if repo is None:
        print("Could not find the Wavesim solver repo. Set WAVESIM_PATH, or "
              "point Wavesim -> Settings at it first.")
        return 2

    workdir = os.path.join(tempfile.gettempdir(), "wavesim_lumped_gate")
    os.makedirs(workdir, exist_ok=True)
    job = _job()
    job["wavesim_path"] = repo
    with open(os.path.join(workdir, "job.json"), "w", encoding="utf-8") as fh:
        json.dump(job, fh, indent=2)

    summary = runner.run_job(workdir)
    data = np.load(os.path.join(workdir, "results.npz"))
    meta = summary.get("lumped_ports") or []

    print("\nlumped port gate ({} steps, dt = {:.4g} s)".format(
        summary["steps"], summary["dt"]))
    ok = True

    if len(meta) != 4:
        print("  FAIL: expected 4 built ports (the unconfigured one has neither "
              "a load nor a drive and must be skipped), got {}".format(len(meta)))
        return 1

    # -- kappa: the placement identities ------------------------------------ #
    dt = summary["dt"]
    whole = dt * DX / (EPS0 * DX * DX)      # one whole cell of weight, in vacuum
    print("  kappa = {:.4g} ohm node-to-node, {:.4g} ohm half-cell ends  "
          "(C_cell = {:.4g} / {:.4g} fF in parallel)".format(
              meta[0]["kappa"], meta[3]["kappa"],
              1.0e15 * meta[0]["c_cell"], 1.0e15 * meta[3]["c_cell"]))
    ok &= _report("kappa on nodes",
                  abs(meta[0]["kappa"] - whole) / whole,
                  "(dt*L/(eps*dA), L = 1 cell)", tol=KAPPA_TOL)
    # Half + whole + half: 0.25 + 1 + 0.25 cells of w^2/h over a 2-cell span.
    ok &= _report("kappa half-cell",
                  abs(meta[3]["kappa"] - 1.5 * whole) / (1.5 * whole),
                  "(two half-covered end cells)", tol=KAPPA_TOL)
    ok &= _report("C_cell",
                  abs(meta[3]["c_cell"] * meta[3]["kappa"] - dt) / dt,
                  "(C_cell = dt/kappa)", tol=KAPPA_TOL)

    # -- resistor ---------------------------------------------------------- #
    v = data["lumped_0v_values"]
    i = data["lumped_0i_values"]
    v_prev = np.concatenate(([0.0], v[:-1]))
    predicted = -(v_prev + v) / (2.0 * R_PASSIVE)
    scale = max(np.max(np.abs(i)), 1.0e-30)
    ok &= _report("resistor", np.max(np.abs(predicted - i)) / scale)

    # -- capacitor --------------------------------------------------------- #
    v = data["lumped_1v_values"]
    i = data["lumped_1i_values"]
    z = summary["dt"] / (2.0 * C)
    v_prev = np.concatenate(([0.0], v[:-1]))
    h = 0.5 * (v_prev + v) + i * z
    scale = max(np.max(np.abs(h)), 1.0e-30)
    residual = max(abs(h[0]), np.max(np.abs((h[:-1] - 2.0 * z * i[:-1]) - h[1:])))
    ok &= _report("capacitor", residual / scale,
                  "(Z_C companion = {:.4g} ohm)".format(z))

    # -- voltage-driven resistor ------------------------------------------- #
    import wavesim as ws

    t = data["lumped_2v_times"]
    v = data["lumped_2v_values"]
    i = data["lumped_2i_values"]
    waveform = runner._build_waveform(ws, _job()["lumped_ports"][2])
    vs = np.array([waveform(float(tn)) for tn in t])
    v_prev = np.concatenate(([0.0], v[:-1]))
    predicted = (vs - 0.5 * (v_prev + v)) / R_DRIVEN
    scale = max(np.max(np.abs(i)), 1.0e-30)
    ok &= _report("driven", np.max(np.abs(predicted - i)) / scale,
                  "(peak Vs = {:.3g} V)".format(np.max(np.abs(vs))))

    # -- the same resistor law, on the half-cell-ended placement ------------- #
    v = data["lumped_3v_values"]
    i = data["lumped_3i_values"]
    v_prev = np.concatenate(([0.0], v[:-1]))
    predicted = -(v_prev + v) / (2.0 * R_PASSIVE)
    scale = max(np.max(np.abs(i)), 1.0e-30)
    ok &= _report("half-cell R", np.max(np.abs(predicted - i)) / scale)

    # A reactive branch must not cost the run its stability.
    finite = all(np.all(np.isfinite(data["lumped_{}{}_values".format(n, s)]))
                 for n in range(4) for s in "vi")
    print("  {:14s} {}".format("finite", "OK" if finite else "FAIL"))
    ok &= finite

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
