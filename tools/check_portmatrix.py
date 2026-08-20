# -*- coding: utf-8 -*-
"""Gate: the port-matrix sweep must recover a known Z and silence everything else.

Two properties, and the whole feature rests on them.

**The algebra.** :func:`wavesim_gui.portmatrix.assemble` claims that N runs give
``Z = V·I⁻¹`` per frequency bin. That is checked here by running it backwards: a
known frequency-dependent Z is chosen, arbitrary independent current records are
invented for each drive, the voltage records that Z implies are synthesised by
inverse transform, and the assembly must return the Z it started from -- to
round-off, over every in-band bin. This also pins the **half-step stagger**,
because the synthesis goes through the same :mod:`wavesim_gui.spectrum` the
assembly does: if one side ever stopped correcting the current's -0.5·dt offset,
the recovered matrix would pick up a frequency-proportional phase and this would
fail rather than quietly reporting a resistance the network does not have.

**The silence.** ``V = Z·I`` describes a source-free network. A stray excitation
anywhere -- the point source, a beam, a port the user forgot, a port not even
enrolled in the matrix -- makes the relation affine and the solve returns a
confident wrong answer with no visible symptom. So :func:`drive_job` is checked
to leave *exactly one* live amplitude in a spec deliberately littered with
drives, and to promote a passive lumped port to a Thévenin drive (which at
amplitude 0 is the passive element again, so the other runs are unchanged).

A third check walks the index contract: a port's position in its ``job.json``
list is what names its arrays in ``results.npz``, and the two must not drift.

Run under ``freecadcmd`` (needs FreeCAD's bundled numpy)::

    freecadcmd.exe tools\\check_portmatrix.py

Exit status is not usable under freecadcmd (it swallows ``sys.exit``); read the
PASS/FAIL line.
"""

import copy
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

try:  # freecadcmd supplies a stub FreeCADGui that the gui modules probe for
    import FreeCADGui

    if not hasattr(FreeCADGui, "addCommand"):
        FreeCADGui.addCommand = lambda *a, **k: None
except ImportError:
    pass

import numpy as np  # noqa: E402

from wavesim_gui import portmatrix as pm  # noqa: E402
from wavesim_gui import spectrum as spec_mod  # noqa: E402


_failures = []


def check(name, ok, detail=""):
    _failures.append(name) if not ok else None
    print("  {} {}{}".format("ok  " if ok else "FAIL", name,
                             ("   " + detail) if detail else ""))


# --------------------------------------------------------------------------- #
# 1. The algebra: synthesise records from a known Z and get it back
# --------------------------------------------------------------------------- #

DT = 1.0e-12
NSAMP = 2048


def _pulse(t, t0, tau):
    """A differentiated gaussian -- broadband, decayed, front-loaded."""
    return -(t - t0) / tau * np.exp(-((t - t0) / tau) ** 2)


def _known_z(freqs, n):
    """A smooth, symmetric, invertible Z(f). No topology implied -- the point is
    that the assembly reproduces whatever matrix produced the records."""
    w = 2.0 * np.pi * freqs
    z = np.zeros((freqs.size, n, n), dtype=complex)
    for i in range(n):
        for j in range(n):
            # Symmetric by construction, and diagonally dominant so it inverts.
            base = 50.0 * (1.0 + 0.5 * (i == j) * (i + 1))
            react = 1.0e-10 * w * (1.0 + 0.3 * (i + j))
            z[:, i, j] = (base / (1 + abs(i - j))) + 1j * react / (1 + abs(i - j))
    return z


def synth_sweep(root, n, window=None):
    """Write N drive directories whose records realise ``_known_z`` exactly."""
    times = np.arange(NSAMP) * DT
    ports = [pm.MatrixPort(pm.FAMILY_MODAL, k, "P{}".format(k))
             for k in range(n)]

    # One arbitrary, independent current record per (port, drive). Different
    # widths and delays so the N excitation states are genuinely independent.
    i_time = np.empty((n, n, NSAMP))
    for k in range(n):
        for p in range(n):
            # Narrow pulses on purpose: the band a bin is reported over is the
            # excitation's, so a narrow one in time gives the checks below
            # hundreds of bins to compare rather than a dozen near the peak.
            # Placed at 15% of the record, not at its very start: a Tukey with
            # alpha=0.1 ramps over the first *and* last 5%, so a drive inside
            # the first hundred samples gets reshaped by the leading taper --
            # which is the Hann failure mode, reproduced with a Tukey, and it
            # cost this gate a spurious 4% until the pulse was moved.
            t0 = (300 + 11 * p + 5 * k) * DT
            tau = (6 + 1.5 * p + 0.7 * k) * DT
            amp = 1.0 if p == k else 0.35 / (1 + abs(p - k))
            i_time[k, p] = amp * _pulse(times, t0, tau)

    # Transform each current the way the assembly will, apply Z, and bring the
    # resulting voltage back to a time series a monitor could have recorded.
    i_spec = np.empty((n, n, NSAMP // 2 + 1), dtype=complex)
    for k in range(n):
        for p in range(n):
            i_spec[k, p] = spec_mod.spectrum(
                times, i_time[k, p], window=window,
                stagger=spec_mod.STAGGER_H).values
    freqs = np.fft.rfftfreq(NSAMP, DT)
    z_true = _known_z(freqs, n)

    v_time = np.empty((n, n, NSAMP))
    for k in range(n):
        # V(:, k) = Z · I(:, k), one matrix product per bin.
        v_spec = np.einsum("fij,jf->if", z_true, i_spec[k])
        for p in range(n):
            # spectrum() with stagger 0 is rfft(v)·dt, so undo exactly that.
            v_time[k, p] = np.fft.irfft(v_spec[p] / DT, n=NSAMP)

    for k in range(n):
        d = os.path.join(root, pm.drive_dir_name(k))
        os.makedirs(d, exist_ok=True)
        arrays = {}
        for p, port in enumerate(ports):
            arrays[port.key_v + "_times"] = times
            arrays[port.key_v + "_values"] = v_time[k, p]
            arrays[port.key_i + "_times"] = times
            arrays[port.key_i + "_values"] = i_time[k, p]
        np.savez(os.path.join(d, "results.npz"), **arrays)
    return ports, freqs, z_true


def test_algebra():
    print("1. assemble() recovers a known Z")
    for n in (2, 3, 4):
        root = tempfile.mkdtemp(prefix="wsmat_")
        try:
            ports, freqs, z_true = synth_sweep(root, n)
            out = pm.assemble(root, ports)
            valid = out["valid"]
            check("  {}x{}: a wide band is reported".format(n, n),
                  int(valid.sum()) > 100,
                  "{} of {} bins".format(int(valid.sum()), valid.size))
            if not valid.any():
                continue
            err = np.abs(out["z"][valid] - z_true[valid])
            rel = float(np.max(err) / np.max(np.abs(z_true[valid])))
            check("  {}x{}: Z matches to round-off".format(n, n), rel < 1e-9,
                  "rel {:.3e}".format(rel))
            check("  {}x{}: masked outside the band".format(n, n),
                  bool(np.all(np.isnan(out["z"][~valid]))))
            recip = pm.reciprocity_error(out["z"])
            check("  {}x{}: reciprocity error ~0 for a symmetric Z".format(n, n),
                  float(np.nanmax(recip[valid])) < 1e-9,
                  "max {:.3e}".format(float(np.nanmax(recip[valid]))))
            y = pm.admittance(out["z"])
            ident = np.einsum("fij,fjk->fik", out["z"][valid], y[valid])
            eye = np.broadcast_to(np.eye(n), ident.shape)
            check("  {}x{}: Y = Z inverse".format(n, n),
                  float(np.max(np.abs(ident - eye))) < 1e-9)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    # A window must leave the ratio essentially alone. Not *exactly*: a taper is
    # a multiplication in time, so a convolution in frequency, and it commutes
    # with multiplying by Z(f) only where Z is flat across the window's own
    # spectral width. So this is a tolerance, not an identity -- what it pins is
    # that turning the Window box on cannot move the answer by anything a reader
    # would act on.
    root = tempfile.mkdtemp(prefix="wsmat_")
    try:
        ports, freqs, z_true = synth_sweep(root, 2)
        out = pm.assemble(root, ports, window="tukey")
        valid = out["valid"]
        rel = float(np.nanmax(np.abs(out["z"][valid] - z_true[valid]))
                    / np.nanmax(np.abs(z_true[valid])))
        check("  2x2 through a Tukey window", rel < 1e-3,
              "rel {:.3e} (want < 1e-3)".format(rel))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_stagger_matters():
    """The correction is load-bearing: drop it and the answer must move."""
    print("2. the half-step stagger is load-bearing")
    root = tempfile.mkdtemp(prefix="wsmat_")
    try:
        ports, freqs, z_true = synth_sweep(root, 2)
        good = pm.assemble(root, ports)

        real_spectrum = spec_mod.spectrum

        def unstaggered(times, values, **kw):
            kw["stagger"] = spec_mod.STAGGER_E
            return real_spectrum(times, values, **kw)

        spec_mod.spectrum = unstaggered
        try:
            bad = pm.assemble(root, ports)
        finally:
            spec_mod.spectrum = real_spectrum

        valid = good["valid"] & bad["valid"]
        rel = float(np.nanmax(np.abs(bad["z"][valid] - z_true[valid]))
                    / np.nanmax(np.abs(z_true[valid])))
        check("  dropping it visibly breaks the answer", rel > 1e-3,
              "rel error {:.3e} (want > 1e-3)".format(rel))
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 3. Spec surgery
# --------------------------------------------------------------------------- #

def _littered_spec():
    """A job spec with a drive on everything, enrolled or not."""
    def exc(amp):
        return {"type": "gaussian", "fmax": 10.0e9, "amplitude": amp}

    return {
        "steps": 100,
        "source": {"component": "Ez", "x": 0.0, "y": 0.0, "z": 0.0,
                   "excitation": exc(3.0)},
        "modal_ports": [
            {"name": "MP a", "normal": "z", "excitation": exc(1.0)},
            {"name": "MP b", "normal": "z", "excitation": exc(0.0)},
            {"name": "MP c", "normal": "z", "excitation": exc(2.5)},
        ],
        "lumped_ports": [
            {"name": "LP passive", "drive": "none", "resistance": 50.0},
            {"name": "LP driven", "drive": "current", "resistance": 50.0,
             "excitation": exc(0.7)},
        ],
        "gaussian_beams": [{"face": "x0", "excitation": exc(4.0)}],
    }


def _live_amplitudes(spec):
    """Every nonzero excitation amplitude in *spec*, as (owner, value)."""
    live = []
    entries = [("source", spec.get("source"))]
    for key in ("modal_ports", "lumped_ports", "gaussian_beams"):
        for e in spec.get(key) or []:
            entries.append((e.get("name", key), e))
    for owner, entry in entries:
        if not isinstance(entry, dict):
            continue
        amp = (entry.get("excitation") or {}).get("amplitude")
        if amp:
            live.append((owner, amp))
    return live


def test_spec_surgery():
    print("3. drive_job() leaves exactly one source alive")
    spec = _littered_spec()
    ports = pm.spec_ports(spec)
    check("  spec_ports finds every port", len(ports) == 5,
          "{} found: {}".format(len(ports), [p.name for p in ports]))
    check("  modal ports come first, in order",
          [p.name for p in ports[:3]] == ["MP a", "MP b", "MP c"])
    check("  array keys follow the spec index",
          [p.key_v for p in ports] ==
          ["port_0v", "port_1v", "port_2v", "lumped_0v", "lumped_1v"])
    check("  and the current keys match",
          [p.key_i for p in ports] ==
          ["port_0i", "port_1i", "port_2i", "lumped_0i", "lumped_1i"])

    # Enrol a subset -- MP a, MP c and the *passive* lumped port -- so the
    # unenrolled ones (MP b, LP driven) and the beam and the point source all
    # have to be silenced too.
    chosen = pm.select_ports(spec, ["MP a", "MP c", "LP passive"])
    excitation = pm.common_excitation(spec, chosen)
    check("  common_excitation takes one waveform",
          excitation.get("amplitude") == 1.0 and excitation.get("type") ==
          "gaussian")

    for k, port in enumerate(chosen):
        job = pm.drive_job(spec, chosen, k, excitation)
        live = _live_amplitudes(job)
        check("  drive {}: exactly one live source".format(k + 1),
              len(live) == 1, "live: {}".format(live))
        if len(live) == 1:
            check("  drive {}: it is '{}'".format(k + 1, port.name),
                  live[0][0] == port.name)
        # The passive lumped port must have become a Thevenin drive, keeping
        # its load -- and it is the load that makes the other runs unchanged.
        lp = job["lumped_ports"][0]
        check("  drive {}: passive lumped port keeps its load".format(k + 1),
              lp.get("resistance") == 50.0)
        if port.name == "LP passive":
            check("  drive {}: promoted to a voltage (Thevenin) drive"
                  .format(k + 1), lp.get("drive") == "voltage")

    check("  the original spec is untouched",
          _live_amplitudes(spec) and len(_live_amplitudes(spec)) == 5,
          "{} live".format(len(_live_amplitudes(spec))))

    # A selection that no longer matches the spec must be refused, loudly.
    try:
        pm.select_ports(spec, ["MP a", "ghost port"])
        check("  a vanished port is refused", False)
    except pm.SweepError as exc:
        check("  a vanished port is refused", "ghost port" in str(exc))
    try:
        pm.select_ports(spec, ["MP a"])
        check("  a single port is refused", False)
    except pm.SweepError:
        check("  a single port is refused", True)


def test_conditioning():
    """Dependent excitation states must be masked, not inverted anyway."""
    print("4. an ill-conditioned bin is not reported")
    root = tempfile.mkdtemp(prefix="wsmat_")
    try:
        ports, freqs, _z = synth_sweep(root, 2)
        # Make drive 1's currents a copy of drive 0's: the two runs then probe
        # one state, not two, and there is nothing to invert.
        d0 = os.path.join(root, pm.drive_dir_name(0), "results.npz")
        d1 = os.path.join(root, pm.drive_dir_name(1), "results.npz")
        with np.load(d0) as a:
            np.savez(d1, **{k: a[k] for k in a.files})
        out = pm.assemble(root, ports)
        check("  no bin survives a degenerate sweep",
              not bool(out["valid"].any()),
              "{} bins reported".format(int(out["valid"].sum())))
        check("  Z is all NaN", bool(np.all(np.isnan(out["z"]))))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    test_algebra()
    test_stagger_matters()
    test_spec_surgery()
    test_conditioning()
    print()
    if _failures:
        print("FAIL: {} check(s) -- {}".format(len(_failures), _failures))
    else:
        print("PASS: port-matrix algebra and spec surgery are sound.")
    sys.stdout.flush()


main()
