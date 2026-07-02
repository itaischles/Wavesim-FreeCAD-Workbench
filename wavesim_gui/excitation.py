# -*- coding: utf-8 -*-
"""Temporal excitation waveforms for Wavesim sources (workbench side).

A source's *excitation* is the temporal profile ``f(t)`` that drives the chosen
field component. This module is the workbench-side single source of truth for:

* the **catalogue** of waveform families the user can choose (``ORDER`` /
  ``LABELS``) and, for each, the **parameters** it exposes with their kind and SI
  default (``PARAMS``) -- the source task panel builds its widgets from this and
  the property editor stores one FreeCAD property per parameter;
* the **maths** (:func:`evaluate`) used to *plot* an excitation in the panel's
  preview and to describe it in the tree label.

Everything here is in SI base units (seconds, hertz, radians-from-degrees) and
depends only on numpy, so it stays importable without FreeCAD/Qt (console mode)
and mirrors the SI frame the solver runs in.

Cross-process note
------------------
This is **workbench code only**. The conda-side :mod:`runner` builds the actual
solver waveform *independently* from the same ``excitation`` spec dict carried in
``job.json`` (the shared contract is the JSON, not this module) so that the
solver can later grow its own native waveform classes without depending on the
workbench. Keep the maths here in step with ``runner._build_waveform``.

Excitation spec dict (the job.json contract)
--------------------------------------------
    {"type": "gaussian"|"sine"|"rectangular"|"gaussian_sine",
     "amplitude": <peak>,
     "fmax": <Hz>,          # gaussian (bandwidth), gaussian_sine (envelope BW)
     "frequency": <Hz>,     # sine / gaussian_sine carrier
     "phase_deg": <deg>,    # sine / gaussian_sine carrier phase
     "start_time": <s>,     # rectangular: delay before the ramp up
     "rise_time":  <s>,     # rectangular: linear ramp-up duration
     "flat_time":  <s>,     # rectangular: flat-top duration
     "fall_time":  <s>}     # rectangular: linear ramp-down duration
"""

import math

import numpy as np

from wavesim_gui import units


# --------------------------------------------------------------------------- #
# Catalogue metadata
# --------------------------------------------------------------------------- #

# Parameter *kinds* -- tell the panel how to show/convert a value (frequency and
# time use the simulation's display unit; amplitude and phase are shown as-is).
KIND_FREQ = "freq"
KIND_TIME = "time"
KIND_AMP = "amp"
KIND_PHASE = "phase"

# Waveform type keys, in the order they appear in the panel's combo box.
GAUSSIAN = "gaussian"
SINE = "sine"
RECTANGULAR = "rectangular"
GAUSSIAN_SINE = "gaussian_sine"

ORDER = [GAUSSIAN, SINE, RECTANGULAR, GAUSSIAN_SINE]

# Human-readable names shown to the user (and stored in the Excitation enum).
LABELS = {
    GAUSSIAN: "Gaussian Pulse",
    SINE: "Sine Wave",
    RECTANGULAR: "Rectangular Pulse",
    GAUSSIAN_SINE: "Gaussian + Sine",
}

# Per-type ordered parameter list: (key, panel-label, kind, SI default).
PARAMS = {
    GAUSSIAN: [
        ("fmax", "Max frequency", KIND_FREQ, 30.0e9),
        ("amplitude", "Amplitude", KIND_AMP, 1.0),
    ],
    SINE: [
        ("frequency", "Frequency", KIND_FREQ, 30.0e9),
        ("amplitude", "Amplitude", KIND_AMP, 1.0),
        ("phase_deg", "Phase offset", KIND_PHASE, 0.0),
    ],
    RECTANGULAR: [
        ("amplitude", "Amplitude", KIND_AMP, 1.0),
        ("start_time", "Start time", KIND_TIME, 0.1e-9),
        ("rise_time", "Rise time", KIND_TIME, 0.1e-9),
        ("flat_time", "Flat-top time", KIND_TIME, 0.5e-9),
        ("fall_time", "Fall time", KIND_TIME, 0.1e-9),
    ],
    GAUSSIAN_SINE: [
        ("frequency", "Carrier frequency", KIND_FREQ, 30.0e9),
        ("fmax", "Envelope bandwidth", KIND_FREQ, 10.0e9),
        ("amplitude", "Amplitude", KIND_AMP, 1.0),
        ("phase_deg", "Phase offset", KIND_PHASE, 0.0),
    ],
}

# Every parameter key used across all types, with its SI default -- lets the
# document object add one property per parameter idempotently.
ALL_PARAMS = {}
for _typ in ORDER:
    for _key, _label, _kind, _default in PARAMS[_typ]:
        ALL_PARAMS.setdefault(_key, (_kind, _default))


def label_for_type(typ):
    """Human label for a waveform *type* key (falls back to the Gaussian one)."""
    return LABELS.get(typ, LABELS[GAUSSIAN])


def type_for_label(label):
    """Waveform *type* key for a human *label* (falls back to Gaussian)."""
    for typ, name in LABELS.items():
        if name == label:
            return typ
    return GAUSSIAN


def param_keys(typ):
    """Ordered parameter keys used by waveform *type*."""
    return [key for key, _label, _kind, _default in PARAMS.get(typ, [])]


# Human labels in panel order (stored in each source's Excitation enum).
EXCITATION_LABELS = [LABELS[t] for t in ORDER]


# --------------------------------------------------------------------------- #
# Document-object <-> spec glue (shared by point and TEM sources)
# --------------------------------------------------------------------------- #
#
# Every excitation parameter maps to one App::PropertyFloat (SI) on the source
# object. These helpers add/refresh those properties, keep the property editor
# tidy, and read the properties back into a spec dict. They only *call* methods
# on the passed FreeCAD object, so this module stays FreeCAD-free/importable in
# console mode.

# Excitation parameter key -> FreeCAD property name holding its SI value.
PROP_FOR_KEY = {
    "fmax": "Fmax",
    "amplitude": "Amplitude",
    "frequency": "Frequency",
    "phase_deg": "PhaseDeg",
    "start_time": "StartTime",
    "rise_time": "RiseTime",
    "flat_time": "FlatTime",
    "fall_time": "FallTime",
}

# Property-editor tooltips for each parameter property.
PARAM_DESCRIPTIONS = {
    "fmax": "Gaussian bandwidth (and Gaussian+Sine envelope BW), in hertz "
            "(edit via the source panel)",
    "amplitude": "Peak amplitude of the excitation waveform",
    "frequency": "Sine / Gaussian+Sine carrier frequency, in hertz "
                 "(edit via the source panel)",
    "phase_deg": "Sine / Gaussian+Sine carrier phase offset, in degrees",
    "start_time": "Rectangular pulse: delay before the ramp up, seconds",
    "rise_time": "Rectangular pulse: linear ramp-up duration, seconds",
    "flat_time": "Rectangular pulse: flat-top duration, seconds",
    "fall_time": "Rectangular pulse: linear ramp-down duration, seconds",
}


def ensure_object_props(obj):
    """Add/refresh the Excitation enum + one property per waveform parameter.

    Idempotent and safe on both freshly created and reloaded objects, so sources
    saved before the extra waveforms existed pick up the new selectable families
    and parameter properties. Parameters are stored in SI with the catalogue
    defaults and edited through the task panel in display units.
    """
    if not hasattr(obj, "Excitation"):
        obj.addProperty(
            "App::PropertyEnumeration", "Excitation", "Excitation",
            "Temporal waveform driving the source",
        )
        obj.Excitation = EXCITATION_LABELS
        obj.Excitation = EXCITATION_LABELS[0]
    else:
        # Refresh the allowed options (older docs only offered Gaussian Pulse),
        # preserving the current selection.
        current = str(obj.Excitation)
        obj.Excitation = EXCITATION_LABELS
        obj.Excitation = current if current in EXCITATION_LABELS \
            else EXCITATION_LABELS[0]

    for key, prop in PROP_FOR_KEY.items():
        if not hasattr(obj, prop):
            _kind, default = ALL_PARAMS[key]
            obj.addProperty(
                "App::PropertyFloat", prop, "Excitation",
                PARAM_DESCRIPTIONS.get(key, ""),
            )
            setattr(obj, prop, float(default))
    sync_visibility(obj)


def sync_visibility(obj):
    """Show (read-only) the parameters the active Excitation uses, hide the rest.

    All parameters are edited through the task panel in display units, so all are
    read-only (mode 1) in the property editor; those irrelevant to the current
    waveform are hidden (mode 2) to keep the editor uncluttered.
    """
    typ = type_for_label(str(getattr(obj, "Excitation", EXCITATION_LABELS[0])))
    used = set(param_keys(typ))
    for key, prop in PROP_FOR_KEY.items():
        if hasattr(obj, prop):
            obj.setEditorMode(prop, 1 if key in used else 2)


def spec_from_object(obj):
    """Return the excitation spec dict (SI) read from *obj*'s properties.

    Carries the waveform ``type`` and only the parameters that type uses -- the
    job.json contract the runner rebuilds the solver waveform from.
    """
    typ = type_for_label(str(getattr(obj, "Excitation", EXCITATION_LABELS[0])))
    spec = {"type": typ}
    for key in param_keys(typ):
        spec[key] = float(getattr(obj, PROP_FOR_KEY[key], ALL_PARAMS[key][1]))
    return spec


def representative_fmax(spec):
    """A single characteristic frequency (Hz) for *spec*, for labels/summaries.

    The Gaussian/Gaussian+Sine bandwidth, the sine/carrier frequency, or 0.0 for
    the rectangular pulse (which has no natural frequency).
    """
    typ = spec.get("type", GAUSSIAN)
    if typ == GAUSSIAN:
        return float(spec.get("fmax", 0.0))
    if typ in (SINE, GAUSSIAN_SINE):
        return float(spec.get("frequency", 0.0))
    return 0.0


def excitation_label(obj, sim):
    """Human excitation label for a source, e.g. ``Gaussian Pulse @ 30 GHz``.

    Uses *sim*'s frequency unit and appends the characteristic frequency for the
    waveforms that have one (the rectangular pulse has none).
    """
    spec = spec_from_object(obj)
    label = label_for_type(spec["type"])
    fmax = representative_fmax(spec)
    if fmax > 0.0:
        unit = units.get_frequency_unit(sim)
        return "{} @ {:g} {}".format(label, units.freq_from_si(fmax, unit), unit)
    return label


# --------------------------------------------------------------------------- #
# Maths (plotting / description)
# --------------------------------------------------------------------------- #

def _gaussian_t0_width(fmax):
    """Return ``(t0, width)`` of a baseband Gaussian whose -3 dB BW ~= *fmax*.

    Mirrors the solver's ``GaussianPulse.for_fmax``: ``width = 1/(2*pi*fmax)`` and
    ``t0 = 4*width`` so the pulse has fully risen by ``t = 0``.
    """
    fmax = max(float(fmax), 1.0e-30)
    width = 1.0 / (2.0 * math.pi * fmax)
    return 4.0 * width, width


def evaluate(spec, t):
    """Evaluate the excitation *spec* at time(s) *t* (seconds, SI).

    *t* may be a scalar or a numpy array; the return matches its shape. Used for
    the panel's preview plot and label, and kept in step with the independent
    conda-side builder in :func:`runner._build_waveform`.
    """
    t = np.asarray(t, dtype=float)
    typ = spec.get("type", GAUSSIAN)
    amp = float(spec.get("amplitude", 1.0))

    if typ == GAUSSIAN:
        t0, width = _gaussian_t0_width(spec.get("fmax", 30.0e9))
        return amp * np.exp(-0.5 * ((t - t0) / width) ** 2)

    if typ == SINE:
        freq = float(spec.get("frequency", 30.0e9))
        phase = math.radians(float(spec.get("phase_deg", 0.0)))
        return amp * np.sin(2.0 * math.pi * freq * t + phase)

    if typ == RECTANGULAR:
        start = float(spec.get("start_time", 0.0))
        rise = float(spec.get("rise_time", 0.0))
        flat = float(spec.get("flat_time", 0.0))
        fall = float(spec.get("fall_time", 0.0))
        end = start + rise + flat + fall
        # Ramp-up and ramp-down envelopes, each clipped to [0, 1]; their minimum
        # traces the trapezoid (flat top where both saturate at 1). A zero-length
        # ramp becomes an instantaneous step.
        up = np.where(t >= start, 1.0, 0.0) if rise <= 0.0 \
            else np.clip((t - start) / rise, 0.0, 1.0)
        down = np.where(t <= end, 1.0, 0.0) if fall <= 0.0 \
            else np.clip((end - t) / fall, 0.0, 1.0)
        return amp * np.minimum(up, down)

    if typ == GAUSSIAN_SINE:
        t0, width = _gaussian_t0_width(spec.get("fmax", 10.0e9))
        freq = float(spec.get("frequency", 30.0e9))
        phase = math.radians(float(spec.get("phase_deg", 0.0)))
        envelope = np.exp(-0.5 * ((t - t0) / width) ** 2)
        return amp * envelope * np.sin(2.0 * math.pi * freq * (t - t0) + phase)

    # Unknown type: silent zero rather than raising in a plotting/label path.
    return np.zeros_like(t)


def suggested_tmax(spec):
    """A reasonable plot span (s) for *spec* when the simulation MaxTime is unset."""
    typ = spec.get("type", GAUSSIAN)
    if typ in (GAUSSIAN, GAUSSIAN_SINE):
        default_bw = 30.0e9 if typ == GAUSSIAN else 10.0e9
        t0, width = _gaussian_t0_width(spec.get("fmax", default_bw))
        return t0 + 4.0 * width
    if typ == SINE:
        freq = max(float(spec.get("frequency", 30.0e9)), 1.0e-30)
        return 5.0 / freq  # a few periods
    if typ == RECTANGULAR:
        span = (float(spec.get("start_time", 0.0))
                + float(spec.get("rise_time", 0.0))
                + float(spec.get("flat_time", 0.0))
                + float(spec.get("fall_time", 0.0)))
        return span * 1.25 if span > 0.0 else 1.0e-9
    return 1.0e-9
