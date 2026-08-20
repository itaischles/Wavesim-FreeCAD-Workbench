# -*- coding: utf-8 -*-
"""Gate: the workbench's spectrum port must answer exactly as the solver's.

:mod:`wavesim_gui.spectrum` is a numeric port of the subset of
``wavesim.spectrum`` the result plots need, for the same reason
:mod:`wavesim_gui.subpixel` is one: FreeCAD's bundled Python cannot import the
solver package, and viewing a finished run must not need the conda side. Two
copies of one transform drift, and the way this one would drift is not visible
in a picture -- a stagger convention flipped on one side puts a *resistance* on
a lossless structure's Z, and the curve still looks like an impedance. So this
transforms the same records through both modules and demands bit-identical
arrays.

Run under the **solver's** Python, the only one that can import both::

    C:\\Users\\itais\\miniconda3\\envs\\wavesim\\python.exe tools\\check_spectrum.py

It needs the solver repo's parent on ``sys.path`` (``WAVESIM_REPO`` overrides the
default location). Nothing here touches FreeCAD, so it runs with no document and
no GUI. Exit status is meaningful; read the PASS/FAIL line either way.

What it covers, and why each part earns its place:

* **every window** in ``_WINDOW_CHOICES`` (the GUI offers all five), on V, on I,
  and on the Z(f) they divide to -- a window is only safe for a ratio if both
  series get the same one, and that has to hold in both modules;
* ``Spectrum.db`` and the **per-run phase unwrap**, which lives on the class
  here but in ``wavesim.viz._phase_deg`` there -- an easy pair to let drift,
  and one masked bin unwrapped naively blanks every sample after it;
* ``usable_band``, which sets both the plotted x-limit and which bins a ratio
  is taken over at all;
* the **stagger physics** itself, on a synthetic lossless series inductance:
  matched staggers must read R = 0 to round-off, and the mismatch must be
  caught reading percent-level R -- so a future edit that quietly drops the
  correction fails here rather than in someone's extracted circuit model.
"""

import importlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

# The repo root -- the directory *containing* the ``wavesim/`` package. The
# conda env has the solver's dependencies but not the package itself installed,
# so this has to go on sys.path or ``import wavesim`` finds nothing.
_REPO = os.environ.get("WAVESIM_REPO", r"C:\Users\itais\Desktop\Wavesim")
sys.path.insert(0, _REPO)

import numpy as np  # noqa: E402

try:
    # By module, not ``from wavesim import spectrum``: the package re-exports
    # the *function* of that name, which would shadow the module.
    ref = importlib.import_module("wavesim.spectrum")
    ref_viz = importlib.import_module("wavesim.viz")
except ImportError as exc:  # pragma: no cover - environment problem
    print("SKIP: cannot import the solver package ({}).".format(exc))
    print("      Run this under the solver Python, or set WAVESIM_REPO.")
    raise SystemExit(2)

from wavesim_gui import spectrum as wb  # noqa: E402


# --------------------------------------------------------------------------- #
# A record shaped like the ones the plots see
# --------------------------------------------------------------------------- #

DT = 1.0e-12
N = 4000
TIMES = np.arange(N) * DT

# Front-loaded and decayed, like an FDTD port record: a differentiated gaussian
# for V, and an I that is neither proportional to it nor in phase with it, so a
# wrong stagger cannot cancel out of the ratio.
_T0, _TAU = 300 * DT, 60 * DT
VOLTS = -(TIMES - _T0) / _TAU * np.exp(-((TIMES - _T0) / _TAU) ** 2)
AMPS = np.gradient(VOLTS, DT) * 1e-11 + 0.3 * VOLTS


class _Port(object):
    """What the solver's adapters take: a self-recording port."""

    def __init__(self, times, voltages, currents):
        self.times = times
        self.voltages = voltages
        self.currents = currents


_failures = []


def _same(name, got, want, tol=0.0):
    """Compare two arrays, treating NaN as a value that must land identically."""
    got, want = np.asarray(got), np.asarray(want)
    if got.shape != want.shape:
        _failures.append(name)
        print("  FAIL {:38s} shape {} vs {}".format(name, got.shape, want.shape))
        return
    nan_got, nan_want = np.isnan(got), np.isnan(want)
    if not np.array_equal(nan_got, nan_want):
        _failures.append(name)
        print("  FAIL {:38s} NaN masks differ ({} vs {} bins)".format(
            name, int(nan_got.sum()), int(nan_want.sum())))
        return
    err = np.abs(got - want)
    err[nan_got] = 0.0
    scale = np.nanmax(np.abs(want)) or 1.0
    rel = float(np.max(err)) / scale
    ok = rel <= tol
    if not ok:
        _failures.append(name)
    print("  {} {:38s} rel {:.3e}".format("ok  " if ok else "FAIL", name, rel))


def main():
    port = _Port(TIMES, VOLTS, AMPS)

    for window in wb.WINDOWS:
        print("window={!r}".format(window))
        rv = ref.spectrum(port, "V", window=window, warn_undecayed=False)
        ri = ref.spectrum(port, "I", window=window, warn_undecayed=False)
        rz = ref.impedance(port, window=window, warn_undecayed=False)

        wv = wb.spectrum(TIMES, VOLTS, window=window, stagger=wb.STAGGER_E,
                         label="V", unit="V")
        wi = wb.spectrum(TIMES, AMPS, window=window, stagger=wb.STAGGER_H,
                         label="I", unit="A")
        wz = wb.impedance(wv, wi)

        _same("freqs", wv.freqs, rv.freqs)
        _same("V(f)", wv.values, rv.values)
        _same("I(f) (stagger applied)", wi.values, ri.values)
        _same("Z(f)", wz.values, rz.values)
        _same("Z real / imag", np.stack([wz.real, wz.imag]),
              np.stack([rz.real, rz.imag]))
        _same("|Z| and dB", np.stack([wz.magnitude, wz.db]),
              np.stack([rz.magnitude, rz.db]))
        # The GUI's phase lives on the Spectrum; the solver's lives in viz.
        _same("phase, unwrapped per run", wz.phase_deg(),
              ref_viz._phase_deg(rz, True))
        _same("phase, principal", wz.phase_deg(False),
              ref_viz._phase_deg(rz, False))
        _same("usable_band(V, I)", wb.usable_band(wv, wi),
              ref.usable_band(rv, ri))

    # ---------------------------------------------------------------- #
    # The stagger is the reason this module exists rather than an rfft.
    # ---------------------------------------------------------------- #
    print("stagger physics (synthetic lossless series L)")
    volts_l = np.gradient(AMPS, DT) * 1.0e-9      # v = L di/dt, E-derived
    right = wb.impedance(wb.spectrum(TIMES, volts_l, stagger=wb.STAGGER_E),
                         wb.spectrum(TIMES, AMPS, stagger=wb.STAGGER_E))
    wrong = wb.impedance(wb.spectrum(TIMES, volts_l, stagger=wb.STAGGER_E),
                         wb.spectrum(TIMES, AMPS, stagger=wb.STAGGER_H))
    band = np.isfinite(right.values)
    r_right = float(np.max(np.abs(right.real[band] / np.abs(right.values[band]))))
    r_wrong = float(np.max(np.abs(wrong.real[band] / np.abs(wrong.values[band]))))
    print("  matched staggers : |R|/|Z| <= {:.3e}   (want < 1e-6)".format(r_right))
    print("  mismatched       : |R|/|Z| <= {:.3e}   (want > 1e-3)".format(r_wrong))
    if not r_right < 1e-6:
        _failures.append("lossless L reads a resistance")
    if not r_wrong > 1e-3:
        # Not pedantry: if a mismatch stopped mattering, the correction stopped
        # being applied, and every check above would still pass.
        _failures.append("a mismatched stagger no longer shows up")

    print("truncation check")
    decayed = wb.tail_ratio(VOLTS)
    ringing = wb.tail_ratio(np.sin(2 * np.pi * 3e9 * TIMES))
    print("  decayed pulse: {:.3e}   undecayed ring: {:.3f}".format(
        decayed, ringing))
    if not (decayed < 0.01 < ringing):
        _failures.append("tail_ratio does not separate decayed from ringing")

    print()
    if _failures:
        print("FAIL: {} check(s) -- {}".format(len(_failures), _failures))
        return 1
    print("PASS: wavesim_gui.spectrum matches wavesim.spectrum exactly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
