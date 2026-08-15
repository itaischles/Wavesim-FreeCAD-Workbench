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
at that instant is the port mid-value ``(V_{n-1} + V_n)/2``. ``kappa/2`` sits
between the element and the field, **not** inside the recorded pair — which is
exactly the solver's claim that "the recorded V(t)/I(t) are exact regardless, so
port extraction is unaffected". Hence:

* resistor (single branch, ``Z_eq = R``)::

      I_n = -(V_{n-1} + V_n) / (2R)

* capacitor (``Z = dt/2C``, trapezoidal history source ``h``)::

      h_n = (V_{n-1} + V_n)/2 + Z*I_n      (from the port law)
      h_{n+1} = h_n - 2*Z*I_n              (the trapezoidal update), h_0 = 0

* Thevenin drive with a resistor::

      I_n = (Vs(t_n) - (V_{n-1} + V_n)/2) / R

The fourth port carries neither a branch nor a drive and must be **skipped** by
both sides (the solver refuses that combination, and it means a half-configured
port rather than a job worth killing at step 0).

There is a second, two-process check this file deliberately does not automate:
the workbench's own ``lumped_port.self_coupling_ohms`` estimate against
``LineSource.self_coupling`` on the same grid. They agree to round-off, and the
reason it is worth re-checking after any change to either side is that the
solver's path quadrature puts a **half** weight on an edge at each end of a
node-aligned line — so the obvious "the line covers whole edges" formula
overstates kappa by exactly 2x for a one-cell gap, the commonest case there is.
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
            {"name": "unconfigured", "topology": "series", "drive": "none",
             "p0": [12 * DX, 15 * DX, 12 * DX],
             "p1": [12 * DX, 15 * DX, 13 * DX]},
        ],
        "monitors": {},
    }


def _report(label, residual, extra=""):
    ok = residual < TOL
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

    if len(meta) != 3:
        print("  FAIL: expected 3 built ports (the fourth has neither a load "
              "nor a drive and must be skipped), got {}".format(len(meta)))
        return 1
    print("  kappa = {:.4g} ohm  (the field sees Z_eq + kappa/2 = "
          "Z_eq + {:.4g} ohm)".format(meta[0]["kappa"], 0.5 * meta[0]["kappa"]))

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

    # A reactive branch must not cost the run its stability.
    finite = all(np.all(np.isfinite(data["lumped_{}{}_values".format(n, s)]))
                 for n in range(3) for s in "vi")
    print("  {:14s} {}".format("finite", "OK" if finite else "FAIL"))
    ok &= finite

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
