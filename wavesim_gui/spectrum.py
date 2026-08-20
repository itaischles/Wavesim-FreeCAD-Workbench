# -*- coding: utf-8 -*-
"""Fourier transforms of a run's recorded time series, and Z(f) (FreeCAD side).

What this is for
----------------
A result leaf holds ``<key>_times`` / ``<key>_values`` out of ``results.npz``.
This module turns such a pair into a one-sided spectrum, and a port's V/I pair
into the impedance ``Z(f) = V(f)/I(f)`` it presents -- the frequency-domain half
of the voltage/current plot windows in :mod:`wavesim_gui.results`.

This is a **numeric port of the subset of ``wavesim.spectrum`` the plots need**
(``Spectrum``, ``spectrum``, ``usable_band`` and the ``impedance`` ratio). It
lives here for the same reason :mod:`wavesim_gui.subpixel` does: FreeCAD's
bundled Python cannot import the solver package, and viewing a finished run must
not need the conda side. Keep the two in step -- they share the transform, not
the code. The one deliberate difference is the interface: the solver adapts its
*objects* (a monitor, a self-recording port) and infers the stagger from what
produced them; here there are only arrays out of an npz, so the caller passes
``times``/``values`` and names the stagger with :data:`STAGGER_E` /
:data:`STAGGER_H`.

The three things that make this more than ``np.fft.rfft``
---------------------------------------------------------
**The half-step stagger.** E and H leapfrog. When the solver's monitors run, E
has reached ``(n+1)·dt`` but H only ``(n+1/2)·dt``, and both samples are stamped
``n·dt``. A port's V is E-derived and its I is the Piket-May impressed current
half a step behind, so dividing the two spectra as recorded multiplies Z by
``exp(+j·pi·f·dt)`` -- which does not show up as a phase footnote but as a
*resistance*, ``|Z|·sin(pi·f·dt)``, in a structure that has none. Every series
therefore carries its stagger and the transform divides it back out.

**Dividing noise by noise.** V(f)/I(f) only means anything where the excitation
put energy. Outside that band both are round-off and the quotient is a garbage
number that nonetheless plots at full scale, so those bins are masked to NaN --
*both* parts of it, because ``np.nan + 0j`` keeps a perfectly good zero in the
imaginary part and a masked reactance would then draw as ``X = 0``, which reads
as a resonance rather than as no data.

**Truncation.** A low-loss structure rings long after the drive is over. If the
run ended mid-ring, the transform sees a rectangular-windowed sinusoid and every
sharp feature smears. :func:`tail_ratio` measures it so the plot can say so;
``window=`` trades resolution for leakage when a longer run is not an option.

On windows, for an FDTD port record specifically: these records are
*front-loaded* -- the drive lands in the first few percent and the rest is decay
-- so a symmetric taper ('hann', 'hamming') puts its steep rising edge right on
top of the excitation and reshapes it (the solver's tests measure 18% error in a
ratio taken through a Hann). ``'tukey'`` is flat across the middle and ramps
only over ``alpha/2`` of the record at each end -- 5% by default -- so it leaves
both a ratio and an amplitude essentially intact. That is why the plot windows
offer Tukey and not Hann as the first non-trivial choice.

**Its leading ramp is not free, though.** A Tukey tapers the *start* as well as
the tail, so a drive that lands inside the first 5% of the record is reshaped by
it exactly the way a Hann reshapes one in the first 30% --
``tools/check_portmatrix.py`` measured 4% error in an extracted Z-matrix from
a pulse sitting at sample 60 of 2048, and 5e-14 from the same pulse at sample
300. Run long enough that the drive clears the ramp, or leave the window off.

Qt-free and FreeCAD-free (numpy only), so it stays importable in console mode.
"""

import numpy as np


__all__ = [
    "Spectrum", "spectrum", "impedance", "usable_band", "tail_ratio",
    "STAGGER_E", "STAGGER_H", "WINDOWS",
]


#: Stagger, in units of dt, of a recorded quantity's true sample time relative
#: to its timestamp. E-derived quantities (a voltage, ``E``) define the
#: reference; H-derived ones (a current, ``H``) are half a step behind.
STAGGER_E = 0.0
STAGGER_H = -0.5

#: Window names accepted by :func:`spectrum`, in the order the GUI offers them.
WINDOWS = (None, "tukey", "exponential", "hann", "hamming")


class Spectrum(object):
    """A one-sided (real-input) spectrum: complex ``values`` on ``freqs``.

    The scaling is that of the continuous transform -- the DFT sum times ``dt``
    -- so ``values`` are per-hertz densities (V/Hz for a voltage), independent
    of the timestep and the record length. For a ratio like Z(f) it cancels.

    ``label`` and ``unit`` are separate: a current is labelled ``I`` and
    measured in ``A``.
    """

    __slots__ = ("freqs", "values", "dt", "label", "unit")

    def __init__(self, freqs, values, dt, label="", unit=""):
        self.freqs = freqs
        self.values = values
        self.dt = float(dt)
        self.label = label
        self.unit = unit

    def __len__(self):
        return len(self.freqs)

    @property
    def magnitude(self):
        """``|X(f)|``."""
        return np.abs(self.values)

    @property
    def db(self):
        """``20·log10|X(f)|``, with zeros floored rather than -inf."""
        mag = self.magnitude
        finite = mag[np.isfinite(mag)]
        peak = float(finite.max()) if finite.size else 1.0
        floor = max(peak * 1e-300, np.finfo(float).tiny)
        return 20.0 * np.log10(np.maximum(mag, floor))

    @property
    def real(self):
        """Real part -- resistance R(f) for an impedance."""
        return self.values.real

    @property
    def imag(self):
        """Imaginary part -- reactance X(f) for an impedance."""
        return self.values.imag

    def phase_deg(self, unwrap=True):
        """Phase in degrees, unwrapped within each run of finite bins.

        ``np.unwrap`` propagates a NaN forward through everything after it, so
        one masked out-of-band gap would erase the rest of the curve.
        Unwrapping each contiguous run on its own keeps every segment the data
        supports, and the step across a gap is honest -- the phase in between
        is genuinely unknown.
        """
        ang = np.angle(self.values)
        if not unwrap:
            return np.degrees(ang)
        out = np.full(ang.shape, np.nan)
        finite = np.isfinite(self.values)
        if not finite.any():
            return out
        edges = np.flatnonzero(np.diff(finite.astype(int)))
        starts = np.concatenate(([0], edges + 1))
        stops = np.concatenate((edges + 1, [finite.size]))
        for a, b in zip(starts, stops):
            if finite[a]:
                out[a:b] = np.degrees(np.unwrap(ang[a:b]))
        return out


# --------------------------------------------------------------------------- #
# The transform
# --------------------------------------------------------------------------- #

def _uniform_dt(times):
    """The sampling interval of *times*, checked for uniformity.

    Every recorder samples every step, so the intervals are identical to
    round-off. A non-uniform record would silently produce a wrong frequency
    axis, so it is rejected rather than averaged over.
    """
    t = np.asarray(times, dtype=float)
    if t.size < 2:
        raise ValueError("Need at least two samples to transform.")
    d = np.diff(t)
    dt = float(d.mean())
    if dt <= 0.0:
        raise ValueError("Timestamps must be strictly increasing.")
    if np.max(np.abs(d - dt)) > 1e-6 * dt:
        raise ValueError(
            "Sample times are not uniformly spaced -- the transform assumes a "
            "constant dt."
        )
    return dt


def _window(name, n, alpha, tau):
    """Window of length *n*. Implemented here to keep this module numpy-only."""
    if name is None or name in ("none", "boxcar"):
        return np.ones(n)
    k = np.arange(n)
    if name == "hann":
        return 0.5 * (1.0 - np.cos(2.0 * np.pi * k / max(n - 1, 1)))
    if name == "hamming":
        return 0.54 - 0.46 * np.cos(2.0 * np.pi * k / max(n - 1, 1))
    if name == "tukey":
        # Cosine-tapered box: flat over the middle, Hann lobes over a fraction
        # ``alpha`` of the length. alpha=0 is a box, alpha=1 a Hann.
        if alpha <= 0.0:
            return np.ones(n)
        if alpha >= 1.0:
            return _window("hann", n, alpha, tau)
        w = np.ones(n)
        edge = int(np.floor(alpha * (n - 1) / 2.0)) + 1
        ramp = 0.5 * (1.0 - np.cos(np.pi * np.arange(edge) / edge))
        w[:edge] = ramp
        w[n - edge:] = ramp[::-1]
        return w
    if name == "exponential":
        # exp(-t/tau), tau in units of the record length: forces the tail to
        # zero while leaving the early, high-amplitude part almost untouched --
        # the natural choice for a decaying resonator, at the cost of
        # broadening every resonance by 1/(2*pi*tau*T).
        return np.exp(-k / (tau * max(n - 1, 1)))
    raise ValueError(
        "Unknown window {!r}; expected one of {}.".format(name, WINDOWS)
    )


def tail_ratio(values, tail_frac=0.05):
    """Largest excursion in the last *tail_frac* of a record, over its peak.

    The truncation check: a response that has rung down leaves a tail of
    numerical dust, one that has not leaves a tail comparable to the peak -- and
    the transform of *that* is a rectangular-windowed sinusoid whose resonances
    are smeared across neighbouring bins. Returns 0.0 for an all-zero record.
    """
    v = np.asarray(values, dtype=float)
    peak = float(np.max(np.abs(v))) if v.size else 0.0
    if peak == 0.0:
        return 0.0
    n_tail = max(int(round(tail_frac * v.size)), 1)
    return float(np.max(np.abs(v[-n_tail:]))) / peak


def spectrum(times, values, *, window=None, alpha=0.1, tau=0.25, pad=1,
             stagger=STAGGER_E, subtract_mean=False, label="", unit=""):
    """Fourier transform of one recorded time series.

    Parameters
    ----------
    times, values : array-like
        The pair out of ``results.npz`` (seconds, SI).
    window : {None, 'tukey', 'exponential', 'hann', 'hamming'}
        Taper applied before transforming. ``None`` transforms the record
        as-is, which is right for a decayed signal and the only choice that
        preserves absolute amplitudes. See the module docstring for why Tukey
        rather than Hann on a front-loaded FDTD record.
    alpha : float
        Tapered fraction for ``'tukey'`` (0.1 tapers 5% at each end).
    tau : float
        Decay constant for ``'exponential'``, in units of the record length.
    pad : int
        Zero-pad to *pad* times the record length. Interpolates the frequency
        axis onto a finer grid; it adds no information and cannot resolve what
        the record length does not already separate, but it makes a peak easier
        to locate. Zeros are appended after windowing, so pad only a decayed or
        windowed record.
    stagger : float
        Sample-time offset in units of dt: :data:`STAGGER_E` for an E-derived
        quantity (a voltage), :data:`STAGGER_H` for an H-derived one (a
        current). Divided back out of the result.
    subtract_mean : bool
        Remove the record's mean first, killing the DC bin and the leakage a
        nonzero offset spreads into the lowest few bins.
    label, unit : str
        Carried onto the result for the plot's legend and axis.

    Returns
    -------
    Spectrum
    """
    v = np.asarray(values, dtype=float)
    if v.ndim != 1:
        raise ValueError("Expected a 1D time series, got shape {}.".format(v.shape))
    dt = _uniform_dt(times)
    if len(times) != v.size:
        # Caught here rather than downstream: a values array shorter than its
        # own time axis still transforms, onto a frequency axis silently scaled
        # by the length it actually had.
        raise ValueError(
            "Series has {} samples but {} timestamps.".format(v.size, len(times))
        )

    if subtract_mean:
        v = v - v.mean()
    v = v * _window(window, v.size, alpha, tau)

    n = int(v.size * pad)
    if n < v.size:
        raise ValueError("pad must be >= 1, got {}.".format(pad))
    freqs = np.fft.rfftfreq(n, dt)
    x = np.fft.rfft(v, n=n) * dt

    # Undo the sampling-time offset: a series sampled at t + stagger*dt but
    # stamped t transforms to X(f)*exp(+2j*pi*f*stagger*dt), so divide it out.
    if stagger:
        x = x * np.exp(-2j * np.pi * freqs * (stagger * dt))

    return Spectrum(freqs, x, dt, label=label, unit=unit)


# --------------------------------------------------------------------------- #
# Bands and ratios
# --------------------------------------------------------------------------- #

def usable_band(*spectra, floor=1e-3):
    """``(f_lo, f_hi)`` -- the span over which every given spectrum has signal.

    The same test :func:`impedance` uses to decide which bins are worth
    dividing, reported as a contiguous range instead of a mask: the frequencies
    where each spectrum stands at least *floor* times its own peak. This is what
    the plots set their x-limit from -- an rfft of an FDTD record runs to
    Nyquist, hundreds of GHz, and autoscaling to that draws the whole response
    in the leftmost pixel column.

    Returns ``(nan, nan)`` if no bin clears the floor in all of them.
    """
    if not spectra:
        raise ValueError("usable_band needs at least one Spectrum.")
    mask = np.ones(len(spectra[0]), dtype=bool)
    for s in spectra:
        if len(s) != len(mask):
            raise ValueError("All spectra must share one frequency axis.")
        mag = np.abs(s.values)
        finite = np.isfinite(mag)
        # A spectrum that is identically zero (or all NaN) has no band at all --
        # without this every bin would clear a floor of zero and the "usable"
        # range would be the whole axis.
        peak = float(np.max(mag[finite])) if finite.any() else 0.0
        if not peak > 0.0:
            return float("nan"), float("nan")
        mask &= finite & (mag >= floor * peak)
    if not mask.any():
        return float("nan"), float("nan")
    f = spectra[0].freqs
    return float(f[mask].min()), float(f[mask].max())


def _in_band(num, den, floor):
    """Bins where *both* spectra carry real signal, as a boolean mask.

    Each is measured against its own peak, so the test is scale-free.
    """
    a, b = np.abs(num.values), np.abs(den.values)
    return (a >= floor * np.nanmax(a)) & (b >= floor * np.nanmax(b))


def impedance(v, i, *, floor=1e-3, label="Z", unit="Ω"):
    """``Z(f) = V(f) / I(f)`` from two spectra sharing one frequency axis.

    Both must have been transformed with the same options (same length, dt and
    ``pad``), which is what makes a window safe for a ratio, and each must carry
    its own stagger -- see the module docstring for what happens when they do
    not.

    Bins where either side falls below *floor* times its own peak are masked to
    NaN in **both** parts, so an out-of-band reactance leaves a gap rather than
    drawing a zero crossing that reads as a resonance.
    """
    if v.freqs.shape != i.freqs.shape or not np.allclose(v.freqs, i.freqs):
        raise ValueError(
            "Numerator and denominator are on different frequency axes -- they "
            "must come from records of the same length and dt."
        )
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = v.values / i.values
    ratio = np.where(_in_band(v, i, floor), ratio, complex(np.nan, np.nan))
    return Spectrum(v.freqs, ratio, v.dt, label=label, unit=unit)
