# -*- coding: utf-8 -*-
"""Per-simulation display units for time and frequency.

The solver works exclusively in SI base units (seconds, hertz). The workbench,
however, lets each simulation pick the units the user *sees and types* -- e.g.
nanoseconds and gigahertz for a microwave problem. This module is the single
source of truth for:

* the available unit labels (``s/ms/us/ns/ps/fs`` and ``Hz/kHz/MHz/GHz/THz``);
* converting between a displayed value and the SI base used by the runner.

The chosen units live as ``TimeUnit`` / ``FrequencyUnit`` enumeration properties
on the Simulation container (see :mod:`wavesim_gui.commands`). Every place that
enters a time or frequency value reads the active simulation's unit via
:func:`get_time_unit` / :func:`get_frequency_unit`, shows it as a fixed
(display-only) suffix, and converts to/from SI at the property boundary.

Qt-free and FreeCAD-free, so it stays importable in console mode.
"""

# Ordered ``(label, factor-to-SI-base)`` tables. Base units: seconds and hertz.
_TIME_UNITS = [
    ("s", 1.0),
    ("ms", 1.0e-3),
    ("us", 1.0e-6),
    ("ns", 1.0e-9),
    ("ps", 1.0e-12),
    ("fs", 1.0e-15),
]
_FREQ_UNITS = [
    ("Hz", 1.0),
    ("kHz", 1.0e3),
    ("MHz", 1.0e6),
    ("GHz", 1.0e9),
    ("THz", 1.0e12),
]

_TIME_FACTORS = dict(_TIME_UNITS)
_FREQ_FACTORS = dict(_FREQ_UNITS)

# Defaults match the workbench's microwave-leaning examples (and the previous
# hardcoded 30 GHz source pulse).
DEFAULT_TIME_UNIT = "ns"
DEFAULT_FREQ_UNIT = "GHz"


def time_unit_labels():
    """Return the ordered list of selectable time-unit labels."""
    return [label for label, _factor in _TIME_UNITS]


def freq_unit_labels():
    """Return the ordered list of selectable frequency-unit labels."""
    return [label for label, _factor in _FREQ_UNITS]


def time_to_si(value, unit):
    """Convert *value* expressed in *unit* to seconds."""
    return float(value) * _TIME_FACTORS.get(unit, 1.0)


def time_from_si(value_s, unit):
    """Convert *value_s* (seconds) to a value expressed in *unit*."""
    return float(value_s) / _TIME_FACTORS.get(unit, 1.0)


def freq_to_si(value, unit):
    """Convert *value* expressed in *unit* to hertz."""
    return float(value) * _FREQ_FACTORS.get(unit, 1.0)


def freq_from_si(value_hz, unit):
    """Convert *value_hz* (hertz) to a value expressed in *unit*."""
    return float(value_hz) / _FREQ_FACTORS.get(unit, 1.0)


def get_time_unit(sim):
    """Return the time unit of Simulation container *sim* (default if unset)."""
    unit = getattr(sim, "TimeUnit", None) if sim is not None else None
    return unit if unit in _TIME_FACTORS else DEFAULT_TIME_UNIT


def get_frequency_unit(sim):
    """Return the frequency unit of Simulation container *sim* (default if unset)."""
    unit = getattr(sim, "FrequencyUnit", None) if sim is not None else None
    return unit if unit in _FREQ_FACTORS else DEFAULT_FREQ_UNIT
