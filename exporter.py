# -*- coding: utf-8 -*-
"""Results exporter for Wavesim runs: CSV, Touchstone, PNG, GIF, summary.md.

Runs in the **Wavesim Python** (numpy/scipy/matplotlib/pillow + the ``wavesim``
repo on ``sys.path``), never in FreeCAD::

    <wavesim_python> exporter.py <run_dir> <out_dir> [--z0 50]
                                 [--project-slug NAME] [--mp4]

*run_dir* is either an ordinary run folder (``results.npz`` + ``summary.json`` +
``job.json``, as written by ``runner.py``) or a **port-matrix** folder holding
one ``drive_<k>`` subfolder per driven port (``wavesim_gui/portmatrix.py``).
Everything lands in *out_dir* under fixed sub-folders (``csv/``, ``plots/``,
``animations/``, ``sparams/``) plus ``summary.md`` and ``manifest.json``; the
formats are documented for agents in the harness repo's
``docs/OUTPUT_FORMATS.md``.

Why this lives beside ``runner.py`` rather than in the FreeCAD GUI: an export is
a pure function of the files a run left behind, so it must not need the document
(or Qt) that produced them -- the agent harness calls it on run folders long
after FreeCAD has moved on. ``wavesim_gui/results.py`` is the reference for
*what* is worth showing and how it is labelled; the maths is the solver's own
(:mod:`wavesim.spectrum`), reused rather than re-derived so an exported Z(f)
cannot disagree with the one the GUI draws.

Robustness contract: each item is exported inside its own ``try``; a failure is
recorded in ``summary.md`` and ``manifest.json`` and the export carries on. Exit
code 0 when ``summary.md`` was written, 2 for unusable input (no results and no
drive folders), and the last stdout line is ``EXPORTED <n> files``.

S-parameters: the solver has no native S-parameters. Every ``S`` here is derived
from the impedance ``Z(f) = V(f)/I(f)`` (a matrix ``Z = V·I⁻¹`` for a port
matrix) against a real reference ``Z0`` (``--z0``, default 50 Ω):
``S = (Z − Z0·1)(Z + Z0·1)⁻¹``.
"""

import argparse
import datetime
import glob
import json
import math
import os
import re
import shutil
import sys
import traceback
import warnings

import numpy as np

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


EXPORTER_VERSION = 1

#: Relative level below which a spectrum bin counts as out of band -- the same
#: floor the GUI and :func:`wavesim.spectrum.impedance` use.
BAND_FLOOR = 1.0e-3
#: Spectrum CSVs run to this multiple of the usable band's upper edge (or to
#: Nyquist, whichever is lower): an rfft axis to Nyquist is mostly round-off.
SPECTRUM_SPAN = 1.5
#: Matrix bins whose excitation conditioning exceeds this are not reported
#: (``wavesim_gui.portmatrix.COND_LIMIT``).
COND_LIMIT = 1.0e10
MAX_GIF_FRAMES = 120
GIF_FPS = 12
MAX_SLICE_CSV_ROWS = 250000

STAGGER_E = 0.0
STAGGER_H = -0.5

_INPLANE_AXES = {"z": ("x", "y"), "y": ("x", "z"), "x": ("y", "z")}

_SPEC_KEY = {"modal": "modal_ports", "lumped": "lumped_ports"}
_ARRAY_PREFIX = {"modal": "port_", "lumped": "lumped_"}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def slugify(text, default="item"):
    """Lowercase ``[a-z0-9_-]`` label; spaces and other runs become ``_``."""
    s = re.sub(r"[^a-z0-9_-]+", "_", str(text).strip().lower())
    s = re.sub(r"_+", "_", s).strip("_-")
    return s or default


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _field_unit(comp):
    """SI unit token (for a CSV header) of a field component name."""
    c = str(comp).replace("|", "").strip()
    if c.lower().startswith("phi"):
        return "V"
    head = c[:1].upper()
    return {"E": "V_per_m", "H": "A_per_m", "S": "W_per_m2",
            "D": "C_per_m2"}.get(head, "")


def _unit_pretty(token):
    return {"V_per_m": "V/m", "A_per_m": "A/m", "W_per_m2": "W/m²",
            "C_per_m2": "C/m²", "V": "V", "A": "A"}.get(token, token)


def _comp_token(comp):
    """Header-safe component name: ``|E|`` -> ``absE``."""
    c = str(comp)
    if c.startswith("|") and c.endswith("|"):
        return "abs" + c.strip("|")
    return re.sub(r"[^A-Za-z0-9_]", "", c) or "value"


def _stagger_of(comp):
    return STAGGER_H if str(comp).strip("|")[:1].upper() == "H" else STAGGER_E


def _wavesim_repo_candidates(job):
    """Where the ``wavesim`` package may live: job, env, workbench settings."""
    out = []
    for repo in ((job or {}).get("wavesim_path"),
                 os.environ.get("WAVESIM_PATH")):
        if repo:
            out.append(repo)
    appdata = os.environ.get("APPDATA")
    if appdata:
        pattern = os.path.join(appdata, "FreeCAD", "**",
                               "wavesim_settings.json")
        for path in glob.glob(pattern, recursive=True):
            data = _read_json(path) or {}
            if data.get("wavesim_path"):
                out.append(data["wavesim_path"])
    return out


def _ensure_wavesim_importable(job):
    """Same precedence as ``runner._ensure_wavesim_importable`` (+ settings)."""
    for repo in _wavesim_repo_candidates(job):
        if os.path.isdir(repo) and repo not in sys.path:
            sys.path.insert(0, repo)
            break


class _Series(object):
    """Light-weight stand-in for a solver monitor (``.times``/``.values``)."""

    def __init__(self, times, values, component="", x=0.0, y=0.0, z=0.0):
        self.times = times
        self.values = values
        self.component = component
        self.x, self.y, self.z = x, y, z


# --------------------------------------------------------------------------- #
# The exporter
# --------------------------------------------------------------------------- #

class Exporter(object):

    def __init__(self, run_dir, out_dir, z0=50.0, project_slug=None,
                 mp4=False):
        self.run_dir = os.path.abspath(run_dir)
        self.out_dir = os.path.abspath(out_dir)
        self.z0 = float(z0)
        self.want_mp4 = bool(mp4)
        self.files = []          # {"path", "kind", "description"}
        self.errors = []         # {"item", "error"}
        self.warnings = []       # strings
        self.notes = []          # strings
        self._labels = set()
        self.project_slug = slugify(project_slug) if project_slug \
            else self._derive_project_slug()
        self.ws = None
        self.spec = None
        self.viz = None
        self.kind = "run"

    # ---- bookkeeping ------------------------------------------------------ #

    def _derive_project_slug(self):
        parts = os.path.normpath(self.run_dir).split(os.sep)
        for i in range(len(parts) - 1, 0, -1):
            if parts[i].lower() in ("runs", "sweeps"):
                return slugify(parts[i - 1], "wavesim")
        return slugify(os.path.basename(self.run_dir), "wavesim")

    def label(self, *parts):
        """A unique slug label for one exported item."""
        base = slugify("_".join(str(p) for p in parts if p not in (None, "")))
        lab, n = base, 2
        while lab in self._labels:
            lab = "{}-{}".format(base, n)
            n += 1
        self._labels.add(lab)
        return lab

    def path(self, sub, name):
        d = os.path.join(self.out_dir, sub) if sub else self.out_dir
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, name)

    def add(self, abspath, kind, description):
        rel = os.path.relpath(abspath, self.out_dir).replace("\\", "/")
        self.files = [f for f in self.files if f["path"] != rel]
        self.files.append({"path": rel, "kind": kind,
                           "description": description})

    def guard(self, item, func, *args, **kwargs):
        """Run one export item; record (never raise) its failure."""
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = func(*args, **kwargs)
            for w in caught:
                msg = "{}: {}".format(item, w.message)
                if msg not in self.warnings:
                    self.warnings.append(msg)
            return result
        except Exception as exc:
            self.errors.append({
                "item": item,
                "error": "{}: {}".format(type(exc).__name__, exc),
            })
            sys.stderr.write("export of {} failed:\n".format(item))
            traceback.print_exc()
            return None
        finally:
            plt.close("all")

    def _clear_previous(self):
        """Delete files a previous export listed in its manifest (only those)."""
        old = _read_json(os.path.join(self.out_dir, "manifest.json"))
        if not isinstance(old, dict):
            return
        for entry in old.get("files") or []:
            rel = str(entry.get("path", ""))
            target = os.path.abspath(os.path.join(self.out_dir, rel))
            if not target.startswith(self.out_dir + os.sep):
                continue
            if os.path.isfile(target):
                try:
                    os.remove(target)
                except OSError:
                    pass

    # ---- writers ---------------------------------------------------------- #

    def write_csv(self, sub, name, header, columns, kind, description):
        cols = [np.asarray(c) for c in columns]
        data = np.column_stack(cols) if cols else np.zeros((0, 0))
        p = self.path(sub, name)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(",".join(header) + "\n")
            for row in data:
                fh.write(",".join(_fmt(v) for v in row) + "\n")
        self.add(p, kind, description)
        return p

    def write_rows(self, sub, name, header, rows, kind, description):
        p = self.path(sub, name)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(",".join(header) + "\n")
            for row in rows:
                fh.write(",".join(_fmt(v) for v in row) + "\n")
        self.add(p, kind, description)
        return p

    def save_fig(self, fig, name, description, kind="plot"):
        p = self.path("plots", name)
        fig.savefig(p, dpi=110)
        plt.close(fig)
        self.add(p, kind, description)
        return p

    # ---- input ------------------------------------------------------------ #

    def load_run(self, run_dir):
        npz_path = os.path.join(run_dir, "results.npz")
        data = {}
        with np.load(npz_path, allow_pickle=False) as npz:
            for key in npz.files:
                data[key] = np.array(npz[key])
        summary = _read_json(os.path.join(run_dir, "summary.json")) or {}
        job = _read_json(os.path.join(run_dir, "job.json")) or {}
        return data, summary, job

    def import_solver(self, job):
        _ensure_wavesim_importable(job)
        import importlib

        try:
            # ``wavesim.spectrum`` the *attribute* is the re-exported function;
            # the module itself is only reachable through the import system.
            self.spec = importlib.import_module("wavesim.spectrum")
        except Exception as exc:
            self.errors.append({"item": "import wavesim.spectrum",
                                "error": "{}: {} (spectra, impedances and "
                                         "S-parameters skipped; set "
                                         "WAVESIM_PATH)".format(
                                             type(exc).__name__, exc)})
            return
        try:
            self.viz = importlib.import_module("wavesim.viz")
        except Exception as exc:
            self.notes.append("wavesim.viz unavailable ({}: {}); plots drawn "
                              "with plain matplotlib".format(
                                  type(exc).__name__, exc))

    # ---- entry point ------------------------------------------------------ #

    def run(self):
        drives = _drive_dirs(self.run_dir)
        has_results = os.path.isfile(os.path.join(self.run_dir, "results.npz"))
        if not has_results and not drives:
            raise InputError("{} holds neither results.npz nor drive_<k> "
                             "port-matrix folders".format(self.run_dir))
        os.makedirs(self.out_dir, exist_ok=True)
        self._clear_previous()
        if drives and not has_results:
            self.kind = "port_matrix"
            self.export_matrix(drives)
        else:
            self.export_single(self.run_dir)
        return self.finish()

    # ======================================================================= #
    # Ordinary run
    # ======================================================================= #

    def export_single(self, run_dir):
        try:
            data, summary, job = self.load_run(run_dir)
        except Exception as exc:
            raise InputError("results.npz unreadable: {}: {}".format(
                type(exc).__name__, exc))
        self.data, self.summary, self.job = data, summary, job
        if summary.get("ok") is False:
            self.warnings.append("summary.json reports a failed run: {}".format(
                summary.get("error")))
        self.import_solver(job)
        self.meta_source = (run_dir, summary, job)

        if summary.get("mode") == "electrostatic":
            self.kind = "electrostatic"
        elif summary.get("mode_only"):
            self.kind = "mode_only"
        else:
            self.kind = "fdtd"

        self.guard("energy/dissipation", self.export_energy)
        self.guard("probes", self.export_probes)
        self.guard("voltage/current monitors", self.export_line_monitors)
        ports = self.guard("ports", self.export_ports) or []
        self.guard("S-parameters", self.export_single_sparams, ports)
        self.guard("port table", self.export_port_table)
        self.guard("modes", self.export_modes)
        self.guard("snapshots", self.export_snapshots)
        self.guard("field lines", self.export_field_lines)
        self.guard("electrostatics", self.export_electrostatic)

    # ---- generic time series ---------------------------------------------- #

    def _pair(self, key):
        t, v = self.data.get(key + "_times"), self.data.get(key + "_values")
        if t is None or v is None or len(t) == 0:
            return None, None
        return np.asarray(t, dtype=float), np.asarray(v, dtype=float)

    def _time_plot(self, label, times, series, ylabel, title, description):
        fig, ax = plt.subplots(figsize=(9, 4))
        for name, values, color in series:
            ax.plot(times * 1e9, values, lw=1.2, color=color, label=name)
        ax.set_xlabel("time (ns)")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(True, alpha=0.3)
        if len(series) > 1:
            ax.legend(fontsize=9)
        fig.tight_layout()
        self.save_fig(fig, label + ".png", description)

    def export_energy(self):
        specs = (
            ("energy", "Energy (incl. PML)", "energy_J", "total energy (J)",
             "#d65a00"),
            ("energy_interior", "Energy (excl. PML)", "energy_J",
             "total energy (J)", "#d65a00"),
            ("dissipation", "Dissipation (incl. PML)", "power_W",
             "dissipated power (W)", "#b03000"),
            ("dissipation_interior", "Dissipation (excl. PML)", "power_W",
             "dissipated power (W)", "#b03000"),
        )
        for key, title, col, ylabel, color in specs:
            t, v = self._pair(key)
            if t is None:
                continue
            lab = self.label(key)
            self.write_csv("csv", lab + ".csv", ["t_s", col], [t, v],
                           "timeseries", "{} vs time".format(title))
            self.guard("plot " + lab, self._time_plot, lab, t,
                       [(title, v, color)], ylabel, title,
                       "{} vs time".format(title))

    def export_probes(self):
        job_probes = ((self.job.get("monitors") or {}).get("probes") or [])
        for idx, meta in enumerate(self.summary.get("probes") or []):
            t, v = self._pair("probe_{}".format(idx))
            if t is None:
                continue
            name = meta.get("name") or "probe {}".format(idx)
            comp = meta.get("component", "")
            unit = _field_unit(comp)
            lab = self.label("probe", name)
            ctok = _comp_token(comp)
            self.write_csv(
                "csv", lab + ".csv",
                ["t_s", "{}_{}".format(ctok, unit) if unit else ctok], [t, v],
                "timeseries",
                "Probe '{}' {} ({}) vs time".format(name, comp,
                                                    _unit_pretty(unit)))
            jp = job_probes[idx] if idx < len(job_probes) else {}
            mon = _Series(t, v, comp, float(jp.get("x", 0.0)),
                          float(jp.get("y", 0.0)), float(jp.get("z", 0.0)))
            self.guard("plot " + lab, self._probe_plot, lab, mon, name)
            self.guard("spectrum " + lab, self.export_spectrum, lab, t, v,
                       ctok, unit, _stagger_of(comp),
                       "Probe '{}' {}".format(name, comp))

    def _probe_plot(self, lab, mon, name):
        if self.viz is not None and mon.component:
            fig, ax = self.viz.plot_monitor_time_series(mon, 0.0)
            ax.set_title("Probe '{}': {} vs. time".format(name, mon.component))
            fig.tight_layout()
        else:
            fig, ax = plt.subplots(figsize=(9, 4))
            ax.plot(mon.times * 1e9, mon.values, color="#1f77b4")
            ax.set_xlabel("time (ns)")
            ax.set_ylabel(mon.component)
            ax.grid(True, alpha=0.3)
        self.save_fig(fig, lab + ".png",
                      "Probe '{}' time series".format(name))

    def export_line_monitors(self):
        for prefix, qty, unit, ylabel, title, color in (
                ("voltage", "V", "V", "voltage (V)",
                 "Voltage: ∫E·dl vs. time", "#2ca02c"),
                ("current", "I", "A", "current (A)",
                 "Current: ∮H·dl vs. time", "#9467bd")):
            for idx, meta in enumerate(self.summary.get(prefix + "s") or []):
                t, v = self._pair("{}_{}".format(prefix, idx))
                if t is None:
                    continue
                name = meta.get("name") or "{} {}".format(prefix, idx)
                lab = self.label(prefix, name)
                self.write_csv("csv", lab + ".csv",
                               ["t_s", "{}_{}".format(qty, unit)], [t, v],
                               "timeseries",
                               "{} monitor '{}' vs time".format(
                                   prefix.title(), name))
                self.guard("plot " + lab, self._time_plot, lab, t,
                           [(name, v, color)], ylabel,
                           "{} ({})".format(title, name),
                           "{} monitor '{}' time series".format(
                               prefix.title(), name))
                self.guard("spectrum " + lab, self.export_spectrum, lab, t, v,
                           qty, unit,
                           STAGGER_H if qty == "I" else STAGGER_E,
                           "{} monitor '{}'".format(prefix.title(), name))

    # ---- spectra ---------------------------------------------------------- #

    def _spectrum(self, t, v, stagger, label, unit):
        return self.spec.spectrum((t, v), window=None, stagger=stagger,
                                  label=label, warn_undecayed=True)

    def _span_mask(self, *spectra):
        f = spectra[0].freqs
        _lo, hi = self.spec.usable_band(*spectra, floor=BAND_FLOOR)
        if np.isfinite(hi) and hi > 0:
            return f <= SPECTRUM_SPAN * hi
        return np.ones(f.shape, dtype=bool)

    def export_spectrum(self, lab, t, v, qty, unit, stagger, what):
        if self.spec is None or len(t) < 4:
            return None
        s = self._spectrum(t, v, stagger, qty, unit)
        keep = self._span_mask(s)
        u = (unit + "_per_Hz") if unit else "per_Hz"
        self.write_csv(
            "csv", lab + "_spectrum.csv",
            ["f_Hz", "mag_" + u, "phase_deg", "re_" + u, "im_" + u],
            [s.freqs[keep], s.magnitude[keep],
             np.degrees(np.angle(s.values[keep])), s.real[keep],
             s.imag[keep]],
            "spectrum",
            "{} spectrum (continuous-FT scaling, {}/Hz), DC to {}x the usable "
            "band edge".format(what, _unit_pretty(unit) or "1",
                               SPECTRUM_SPAN))
        self.guard("plot " + lab + "_spectrum", self._spectrum_plot, lab, [s],
                   what)
        return s

    def _spectrum_plot(self, lab, spectra, what):
        if self.viz is not None:
            fig, ax = self.viz.plot_spectrum(spectra)
            ax.set_title("Spectrum: {}".format(what))
        else:
            fig, ax = plt.subplots(figsize=(9, 4))
            for s in spectra:
                ax.semilogy(s.freqs / 1e9, s.magnitude, label=s.label)
            ax.set_xlabel("frequency (GHz)")
            ax.legend()
        fig.tight_layout()
        self.save_fig(fig, lab + "_spectrum.png",
                      "Magnitude spectrum of {} (shaded: usable band)".format(
                          what))

    # ---- ports ------------------------------------------------------------ #

    def _port_list(self):
        """``[(family, name, v_key, i_key, meta)]`` for every recorded port."""
        out = []
        for meta in self.summary.get("ports") or []:
            si = int(meta.get("source_index", 0))
            out.append(("modal", str(meta.get("name") or "port {}".format(si)),
                        "port_{}v".format(si), "port_{}i".format(si), meta))
        for idx, meta in enumerate(self.summary.get("lumped_ports") or []):
            out.append(("lumped",
                        str(meta.get("name") or "lumped port {}".format(idx)),
                        "lumped_{}v".format(idx), "lumped_{}i".format(idx),
                        meta))
        for idx, meta in enumerate(self.summary.get("spice_ports") or []):
            out.append(("spice",
                        str(meta.get("name") or "spice port {}".format(idx)),
                        "spice_{}v".format(idx), "spice_{}i".format(idx),
                        meta))
        return out

    def export_ports(self):
        """Port V/I CSV + plots, V/I spectra and Z(f). Returns the Z list."""
        results = []
        title = {"modal": "Modal port", "lumped": "Lumped port",
                 "spice": "SPICE port"}
        for family, name, vk, ik, meta in self._port_list():
            tv, v = self._pair(vk)
            ti, i = self._pair(ik)
            if tv is None and ti is None:
                continue
            lab = self.label(family, name)
            what = "{} '{}'".format(title[family], name)
            if tv is not None and ti is not None and len(tv) == len(ti):
                self.write_csv("csv", lab + ".csv", ["t_s", "V_V", "I_A"],
                               [tv, v, i], "timeseries",
                               "{} voltage and current (I positive into the "
                               "structure) vs time".format(what))
            else:
                for t, x, col in ((tv, v, "V_V"), (ti, i, "I_A")):
                    if t is not None:
                        q = col[0].lower()
                        self.write_csv("csv", "{}_{}.csv".format(lab, q),
                                       ["t_s", col], [t, x], "timeseries",
                                       "{} {} vs time".format(what, col[0]))
            series = []
            if tv is not None:
                series.append(("V", tv, v))
            if ti is not None:
                series.append(("I", ti, i))
            self.guard("plot " + lab, self._port_time_plot, lab, series, what)
            z = self.guard("impedance " + lab, self._port_impedance, lab, what,
                           tv, v, ti, i, meta, family)
            if z is not None:
                results.append({"family": family, "name": name, "label": lab,
                                "z": z, "meta": meta})
        return results

    def _port_time_plot(self, lab, series, what):
        fig, ax = plt.subplots(figsize=(9, 4))
        lines = []
        ax_i = ax
        for q, t, x in series:
            if q == "I" and len(series) > 1:
                ax_i = ax.twinx()
                lines += ax_i.plot(t * 1e9, x, color="#9467bd", ls="--",
                                   label="I(t)")
                ax_i.set_ylabel("current (A)")
            else:
                lines += ax.plot(t * 1e9, x,
                                 color="#2ca02c" if q == "V" else "#9467bd",
                                 label="{}(t)".format(q))
                ax.set_ylabel("voltage (V)" if q == "V" else "current (A)")
        ax.set_xlabel("time (ns)")
        ax.set_title("{}: V and I vs. time".format(what))
        ax.grid(True, alpha=0.3)
        ax.legend(lines, [l.get_label() for l in lines], fontsize=9)
        fig.tight_layout()
        self.save_fig(fig, lab + ".png",
                      "{} V(t) and I(t) (I on the right axis)".format(what))

    def _port_impedance(self, lab, what, tv, v, ti, i, meta, family):
        if self.spec is None or tv is None or ti is None or len(tv) < 4:
            return None
        sv = self._spectrum(tv, v, STAGGER_E, "V", "V")
        si = self._spectrum(ti, i, STAGGER_H, "I", "A")
        keep = self._span_mask(sv, si)
        for s, q, u in ((sv, "v", "V"), (si, "i", "A")):
            self.write_csv(
                "csv", "{}_{}_spectrum.csv".format(lab, q),
                ["f_Hz", "mag_{}_per_Hz".format(u), "phase_deg",
                 "re_{}_per_Hz".format(u), "im_{}_per_Hz".format(u)],
                [s.freqs[keep], s.magnitude[keep],
                 np.degrees(np.angle(s.values[keep])), s.real[keep],
                 s.imag[keep]],
                "spectrum", "{} {}(f) spectrum ({}/Hz)".format(
                    what, q.upper(), u))
        self.guard("plot " + lab + "_spectrum", self._spectrum_plot, lab,
                   [sv, si], what + " V and I")
        z = self.spec.impedance(sv, si, floor=BAND_FLOOR)
        ok = np.isfinite(z.values) & (z.freqs > 0)
        if not np.any(ok):
            self.warnings.append("{}: no in-band bins, Z(f) not written".format(
                what))
            return None
        zf, zv = z.freqs[ok], z.values[ok]
        self.write_csv(
            "csv", lab + "_impedance.csv",
            ["f_Hz", "re_Z_ohm", "im_Z_ohm", "mag_Z_ohm", "phase_deg"],
            [zf, zv.real, zv.imag, np.abs(zv), np.degrees(np.angle(zv))],
            "impedance",
            "{} input impedance Z(f) = V(f)/I(f), usable band only".format(
                what))
        self.guard("plot " + lab + "_impedance", self._impedance_plot, lab, z,
                   what, meta, family)
        return z

    def _impedance_plot(self, lab, z, what, meta, family):
        if self.viz is not None:
            fig, (ax_m, _ax_p) = self.viz.plot_bode(z)
            ax_m.set_title("{}: Z(f) = V(f)/I(f)".format(what))
            notes = []
            zr = meta.get("reference_impedance") if family == "modal" else None
            if zr:
                notes.append("Z_ref = {:.4g} Ω".format(zr))
                ax_m.axhline(zr, color="0.4", ls="--", lw=0.8)
            if meta.get("c_cell"):
                notes.append("cell gap C = {:.4g} fF in parallel".format(
                    1e15 * float(meta["c_cell"])))
            if notes:
                ax_m.text(0.02, 0.95, "\n".join(notes), transform=ax_m.transAxes,
                          va="top", fontsize=9,
                          bbox=dict(boxstyle="round", fc="white", ec="0.7"))
        else:
            fig, ax_m = plt.subplots(figsize=(9, 4))
            ax_m.semilogy(z.freqs / 1e9, np.abs(z.values))
        fig.tight_layout()
        self.save_fig(fig, lab + "_impedance.png",
                      "{} |Z| and phase vs frequency (Bode)".format(what))

    def export_single_sparams(self, ports):
        if not ports:
            return
        meta_lines = self._touchstone_header()
        for p in ports:
            stem = self.project_slug if len(ports) == 1 else \
                "{}_{}".format(self.project_slug, p["label"])
            z = p["z"]
            ok = np.isfinite(z.values) & (z.freqs > 0)
            f = z.freqs[ok]
            zz = z.values[ok].reshape(-1, 1, 1)
            s = _s_from_z(zz, self.z0)
            names = [p["name"]]
            lines = meta_lines + [
                "Port 1: {} ({} port)".format(p["name"], p["family"]),
            ]
            if p["family"] == "modal" and p["meta"].get("reference_impedance"):
                lines.append("Modal reference impedance Z_ref = {:.6g} ohm "
                             "(not used: S is referenced to Z0 = {:g} "
                             "ohm)".format(p["meta"]["reference_impedance"],
                                           self.z0))
            if len(ports) > 1:
                lines.append("This run holds {} ports; this file is the "
                             "one-port S11 of '{}' with every other port in "
                             "its own termination".format(len(ports),
                                                          p["name"]))
            self._write_touchstone(stem, f, s, lines)
            self._write_sparams_csv(stem + "_s11.csv", f, s, names,
                                    "S11 of '{}' derived from Z(f)".format(
                                        p["name"]))
            self.guard("plot sparams " + stem, self._sparams_plot, stem, f, s,
                       names)

    # ---- port metadata ---------------------------------------------------- #

    def export_port_table(self):
        rows = []
        for family, name, _vk, _ik, meta in self._port_list():
            rows.append([
                family, name,
                meta.get("source_index", ""), meta.get("topology", ""),
                meta.get("drive", ""), _num(meta.get("resistance")),
                _num(meta.get("inductance")), _num(meta.get("capacitance")),
                _num(meta.get("c_cell")),
                _num(meta.get("reference_impedance")),
                _num(meta.get("sheet_divergence")),
            ])
        if not rows:
            return
        self.write_rows(
            "csv", "ports.csv",
            ["family", "name", "source_index", "topology", "drive",
             "resistance_ohm", "inductance_H", "capacitance_F", "c_cell_F",
             "reference_impedance_ohm", "sheet_divergence"],
            rows, "table",
            "One row per port: family (modal/lumped/spice), lumped R/L/C, "
            "bridged-cell gap capacitance, modal reference impedance")

    def export_modes(self):
        modes = self.summary.get("modes") or []
        rows = []
        for meta in modes:
            si, mi = meta.get("source_index", 0), meta.get("mode_index", 0)
            rows.append([
                meta.get("name", ""), si, mi,
                meta.get("conductor", ""), meta.get("conductor_id", ""),
                bool(meta.get("driven")), meta.get("normal", ""),
                _num(meta.get("position")), _num(meta.get("impedance")),
                _num(meta.get("eps_eff")), _num(meta.get("capacitance")),
                _num(meta.get("inductance")), _num(meta.get("v_phase")),
            ])
            key = "mode_{}_{}".format(si, mi)
            if key + "_phi" in self.data:
                lab = self.label("mode", meta.get("name", "port"), mi)
                self.guard("plot " + lab, self._mode_plot, lab, key, meta)
        if rows:
            self.write_rows(
                "csv", "modes.csv",
                ["port", "source_index", "mode_index", "conductor",
                 "conductor_id", "driven", "normal", "position_m",
                 "impedance_ohm", "eps_eff", "capacitance_F_per_m",
                 "inductance_H_per_m", "v_phase_m_per_s"],
                rows, "table",
                "Solved TEM port modes and their per-unit-length parameters")

    def _mode_plot(self, lab, key, meta):
        phi = self.data[key + "_phi"]
        ca = self.data.get(key + "_ca")
        cb = self.data.get(key + "_cb")
        if ca is None or cb is None or len(ca) != phi.shape[0] \
                or len(cb) != phi.shape[1]:
            ca = np.arange(phi.shape[0]) * float(meta.get("da", 1.0))
            cb = np.arange(phi.shape[1]) * float(meta.get("db", 1.0))
        axes = meta.get("transverse_axes") or ["a", "b"]
        fig, ax = plt.subplots(figsize=(6.5, 5.5))
        mesh = ax.pcolormesh(ca * 1e3, cb * 1e3, phi.T, shading="nearest",
                             cmap="viridis")
        fig.colorbar(mesh, ax=ax, label="φ (normalised, V)")
        pec = self.data.get(key + "_pec")
        if pec is not None and np.any(pec):
            ax.contour(ca * 1e3, cb * 1e3, pec.T.astype(float), levels=[0.5],
                       colors="k", linewidths=1.0)
        ax.set_aspect("equal")
        ax.set_xlabel("{} (mm, solver frame)".format(axes[0]))
        ax.set_ylabel("{} (mm, solver frame)".format(axes[1]))
        z0 = meta.get("impedance")
        ax.set_title("{} mode {}{}".format(
            meta.get("name", "port"), meta.get("mode_index", 0),
            "  Z0 = {:.4g} Ω".format(z0) if z0 else ""))
        fig.tight_layout()
        self.save_fig(fig, lab + ".png",
                      "TEM mode potential of port '{}' (mode {}, black: "
                      "conductors)".format(meta.get("name", ""),
                                           meta.get("mode_index", 0)))

    # ---- snapshots -------------------------------------------------------- #

    def export_snapshots(self):
        for idx, meta in enumerate(self.summary.get("snapshots") or []):
            name = meta.get("name") or "snapshot {}".format(idx)
            self.guard("snapshot " + name, self._snapshot, idx, meta, name)

    def _snapshot(self, idx, meta, name):
        comps = [c for c in meta.get("components") or []
                 if "snapshot_{}_{}_data".format(idx, c) in self.data]
        stacks = {c: self.data["snapshot_{}_{}_data".format(idx, c)]
                  for c in comps}
        legacy = "snapshot_{}_data".format(idx)
        if not stacks and legacy in self.data:
            c = meta.get("component") or "field"
            stacks = {c: self.data[legacy]}
            comps = [c]
        if not stacks:
            return
        times = np.asarray(self.data.get("snapshot_{}_times".format(idx),
                                         np.zeros(1)), dtype=float)
        first = stacks[comps[0]]
        e0 = self.data.get("snapshot_{}_edges0".format(idx))
        e1 = self.data.get("snapshot_{}_edges1".format(idx))
        if e0 is None or len(e0) != first.shape[1] + 1:
            e0 = np.arange(first.shape[1] + 1, dtype=float)
        if e1 is None or len(e1) != first.shape[2] + 1:
            e1 = np.arange(first.shape[2] + 1, dtype=float)
        normal = self._snapshot_normal(idx, meta)
        ax0, ax1 = _INPLANE_AXES.get(normal, ("a", "b"))
        lab = self.label("snapshot", name)
        if first.shape[0] <= 1 or self.kind == "electrostatic":
            self._static_slice(lab, name, meta, comps, stacks, e0, e1,
                               (ax0, ax1))
            return
        # Animate the component that actually carries the field.
        peak = {c: float(np.nanmax(np.abs(stacks[c]))) for c in comps}
        comp = max(comps, key=lambda c: peak[c])
        stack = stacks[comp]
        interior = (meta.get("interior0"), meta.get("interior1"))
        self._animate(lab, name, comp, stack, times, e0, e1, (ax0, ax1),
                      interior)

    def _snapshot_normal(self, idx, meta):
        if self.kind == "electrostatic":
            slices = (self.job.get("electrostatic") or {}).get("slices") or []
        else:
            slices = (self.job.get("monitors") or {}).get("snapshots") or []
        if idx < len(slices):
            return str(slices[idx].get("normal", "z"))
        return "z"

    def _animate(self, lab, name, comp, stack, times, e0, e1, axes, interior):
        n = stack.shape[0]
        pick = np.unique(np.linspace(0, n - 1, min(n, MAX_GIF_FRAMES))
                         .round().astype(int))
        frames = stack[pick]
        absf = np.abs(frames[np.isfinite(frames)])
        vmax = float(np.percentile(absf, 99.5)) if absf.size else 1.0
        if not vmax > 0:
            vmax = float(absf.max()) if absf.size and absf.max() > 0 else 1.0
        unit = _unit_pretty(_field_unit(comp))
        x, y = e0 * 1e3, e1 * 1e3

        def setup():
            fig, ax = plt.subplots(figsize=(6.4, 5.2))
            mesh = ax.pcolormesh(x, y, frames[0].T, cmap="RdBu_r",
                                 vmin=-vmax, vmax=vmax, shading="flat")
            fig.colorbar(mesh, ax=ax, label="{} ({})".format(comp, unit))
            self._draw_interior(ax, interior)
            ax.set_aspect("equal")
            ax.set_xlabel("{} (mm, solver frame)".format(axes[0]))
            ax.set_ylabel("{} (mm, solver frame)".format(axes[1]))
            return fig, ax, mesh

        fig, ax, mesh = setup()
        title = ax.set_title("")

        def update(k):
            mesh.set_array(frames[k].T.ravel())
            t = times[pick[k]] if pick[k] < len(times) else float("nan")
            title.set_text("{}: {}  t = {:.4g} ns".format(name, comp, t * 1e9))
            return mesh, title

        from matplotlib import animation
        anim = animation.FuncAnimation(fig, update, frames=len(pick),
                                       blit=False)
        gif = self.path("animations", lab + ".gif")
        anim.save(gif, writer=animation.PillowWriter(fps=GIF_FPS), dpi=80)
        self.add(gif, "animation",
                 "Snapshot '{}': {} animation, {} of {} frames, symmetric "
                 "colour scale ±{:.3g} {} (dashed: PML boundary)".format(
                     name, comp, len(pick), n, vmax, unit))
        if self.want_mp4:
            if shutil.which("ffmpeg"):
                mp4 = self.path("animations", lab + ".mp4")
                anim.save(mp4, writer=animation.FFMpegWriter(fps=GIF_FPS),
                          dpi=100)
                self.add(mp4, "animation",
                         "Snapshot '{}': {} animation (MP4)".format(name, comp))
            else:
                msg = "--mp4 requested but ffmpeg is not on PATH; MP4 skipped"
                if msg not in self.notes:
                    self.notes.append(msg)
        plt.close(fig)

        # One still of the strongest frame, for readers that cannot play GIFs.
        kmax = int(np.argmax([np.nanmax(np.abs(f)) for f in frames]))
        fig, ax, mesh = setup()
        mesh.set_array(frames[kmax].T.ravel())
        t = times[pick[kmax]] if pick[kmax] < len(times) else float("nan")
        ax.set_title("{}: {}  t = {:.4g} ns (peak frame)".format(
            name, comp, t * 1e9))
        fig.tight_layout()
        self.save_fig(fig, lab + "_peak.png",
                      "Snapshot '{}': {} at its strongest recorded "
                      "frame".format(name, comp))

    @staticmethod
    def _draw_interior(ax, interior):
        i0, i1 = interior
        if not i0 and not i1:
            return
        xlim, ylim = ax.get_xlim(), ax.get_ylim()
        x0, x1 = (i0[0] * 1e3, i0[1] * 1e3) if i0 else xlim
        y0, y1 = (i1[0] * 1e3, i1[1] * 1e3) if i1 else ylim
        ax.plot([x0, x1, x1, x0, x0], [y0, y0, y1, y1, y0], ls="--",
                color="0.3", lw=0.8)

    def _static_slice(self, lab, name, meta, comps, stacks, e0, e1, axes):
        x, y = e0 * 1e3, e1 * 1e3
        planes = {c: stacks[c][-1] for c in comps}
        field = meta.get("field") or comps[0]
        fig, ax = plt.subplots(figsize=(6.4, 5.2))
        if len(comps) == 1:
            c = comps[0]
            data = planes[c]
            unit = _unit_pretty(_field_unit(c))
            cmap = "viridis" if c.lower().startswith("phi") else "RdBu_r"
            mesh = ax.pcolormesh(x, y, data.T, cmap=cmap, shading="flat")
            fig.colorbar(mesh, ax=ax, label="{} ({})".format(c, unit))
            shown = c
        else:
            mag = np.sqrt(sum(planes[c] ** 2 for c in comps))
            unit = _unit_pretty(_field_unit(comps[0]))
            mesh = ax.pcolormesh(x, y, mag.T, cmap="inferno", shading="flat")
            fig.colorbar(mesh, ax=ax, label="|{}| ({})".format(field, unit))
            inpl = [c for c in (meta.get("inplane") or []) if c in planes]
            if len(inpl) == 2:
                cx = 0.5 * (x[:-1] + x[1:])
                cy = 0.5 * (y[:-1] + y[1:])
                step = max(1, int(max(len(cx), len(cy)) / 25))
                u = planes[inpl[0]][::step, ::step].T
                v = planes[inpl[1]][::step, ::step].T
                norm = np.sqrt(u ** 2 + v ** 2)
                weak = norm < 0.02 * (np.nanmax(norm) if norm.size else 0.0)
                norm[norm == 0] = 1.0
                u, v = u / norm, v / norm
                u[weak] = np.nan
                v[weak] = np.nan
                ax.quiver(cx[::step], cy[::step], u, v,
                          color="w", scale=40, width=0.003)
            shown = "|{}|".format(field)
        ax.set_aspect("equal")
        ax.set_xlabel("{} (mm, solver frame)".format(axes[0]))
        ax.set_ylabel("{} (mm, solver frame)".format(axes[1]))
        ax.set_title("{}: {}".format(name, shown))
        fig.tight_layout()
        self.save_fig(fig, lab + ".png",
                      "Slice '{}': {} ({})".format(name, shown,
                                                   "static field"
                                                   if self.kind ==
                                                   "electrostatic"
                                                   else "single frame"))
        # The numbers themselves, long format, for a reader that wants values.
        cx = 0.5 * (e0[:-1] + e0[1:])
        cy = 0.5 * (e1[:-1] + e1[1:])
        if len(cx) * len(cy) <= MAX_SLICE_CSV_ROWS:
            aa, bb = np.meshgrid(cx, cy, indexing="ij")
            cols = [aa.ravel(), bb.ravel()] + [planes[c].ravel() for c in comps]
            header = ["{}_m".format(axes[0]), "{}_m".format(axes[1])] + [
                "{}_{}".format(_comp_token(c), _field_unit(c)) for c in comps]
            self.write_csv("csv", lab + "_slice.csv", header, cols,
                           "slice",
                           "Slice '{}' sample values on its in-plane grid "
                           "(sample centres, metres, solver frame)".format(name))
        else:
            self.notes.append("slice '{}' too large for CSV ({} samples); "
                              "PNG only".format(name, len(cx) * len(cy)))

    # ---- field lines (electrostatic curves) ------------------------------- #

    def export_field_lines(self):
        for idx, meta in enumerate(self.summary.get("field_lines") or []):
            key = "line_{}".format(idx)
            comps = [c for c in meta.get("components") or []
                     if "{}_{}".format(key, c) in self.data]
            if not comps:
                continue
            name = meta.get("name") or "line {}".format(idx)
            self.guard("field line " + name, self._field_line, key, comps,
                       name)

    def _field_line(self, key, comps, name):
        vals = [np.asarray(self.data["{}_{}".format(key, c)], dtype=float)
                for c in comps]
        n = len(vals[0])
        s = np.asarray(self.data.get(key + "_s", np.arange(n)), dtype=float)
        pts = np.asarray(self.data.get(key + "_points", np.zeros((n, 3))),
                         dtype=float).reshape(-1, 3)
        if len(s) != n:
            s = np.arange(n, dtype=float)
        if len(pts) != n:
            pts = np.full((n, 3), np.nan)
        cols = [s, pts[:, 0], pts[:, 1], pts[:, 2]] + vals
        header = ["s_m", "x_m", "y_m", "z_m"] + [
            "{}_{}".format(_comp_token(c), _field_unit(c)) for c in comps]
        tang = np.asarray(self.data.get(key + "_tangents", []),
                          dtype=float).reshape(-1, 3)
        if len(comps) == 3 and len(tang) == n:
            et = sum(vals[k] * tang[:, k] for k in range(3))
            cols.append(et)
            header.append("{}_tangential_{}".format(comps[0][0],
                                                    _field_unit(comps[0])))
        lab = self.label("line", name)
        self.write_csv("csv", lab + ".csv", header, cols, "field_line",
                       "Field along curve '{}' vs distance s along it (points "
                       "in solver-frame metres)".format(name))
        fig, ax = plt.subplots(figsize=(9, 4))
        for c, v in zip(comps, vals):
            ax.plot(s * 1e3, v, label=c)
        if len(cols) > 4 + len(comps):
            ax.plot(s * 1e3, cols[-1], "k--", label="tangential")
        ax.set_xlabel("distance along the curve (mm)")
        ax.set_ylabel(_unit_pretty(_field_unit(comps[0])))
        ax.set_title("Field along '{}'".format(name))
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=9)
        fig.tight_layout()
        self.save_fig(fig, lab + ".png",
                      "Field along curve '{}'".format(name))

    # ---- electrostatics --------------------------------------------------- #

    def export_electrostatic(self):
        es = self.summary.get("electrostatic")
        if not es:
            return
        pots = es.get("potentials") or {}
        floating = es.get("floating") or {}
        fpots = es.get("floating_potentials") or {}
        rows = []
        for name in sorted(es.get("charges") or {}):
            if name in pots:
                kind, volts = "applied", pots[name]
            elif name in floating:
                kind, volts = "floating", fpots.get(name)
            else:
                kind, volts = "grounded", 0.0
            rows.append([name, kind, _num(volts),
                         _num((es.get("charges") or {}).get(name))])
        if rows:
            self.write_rows("csv", "charges_C.csv",
                            ["conductor", "kind", "potential_V", "charge_C"],
                            rows, "table",
                            "Per-conductor potential (applied, floating "
                            "result, or grounded 0 V) and charge")
        cap = es.get("capacitance")
        if cap:
            names = list(cap.get("names") or [])
            out = []
            for mat in ("maxwell", "mutual"):
                for name, row in zip(names, cap.get(mat) or []):
                    out.append([mat, name] + [_num(v) for v in row])
            self.write_rows(
                "csv", "capacitance_matrix_F.csv",
                ["matrix", "conductor"] + names, out, "capacitance",
                "Capacitance matrix in farads: 'maxwell' rows (C_ij = "
                "dQ_i/dV_j, others grounded) then 'mutual' rows (two-terminal "
                "capacitance between conductors; diagonal = to ground)")
            self.guard("plot capacitance", self._cap_plot, names, cap)
        elif es.get("capacitance_skipped"):
            self.warnings.append("capacitance skipped: {}".format(
                es["capacitance_skipped"]))
        if cap and cap.get("fused"):
            self.warnings.append("capacitance: fused conductor groups {}".format(
                cap["fused"]))

    def _cap_plot(self, names, cap):
        m = np.asarray(cap.get("mutual") or [], dtype=float)
        if m.size == 0:
            return
        fig, ax = plt.subplots(figsize=(1.6 + 1.1 * len(names),
                                        1.2 + 1.0 * len(names)))
        im = ax.imshow(m * 1e12, cmap="Blues")
        for i in range(len(names)):
            for j in range(len(names)):
                ax.text(j, i, "{:.4g}".format(m[i, j] * 1e12), ha="center",
                        va="center", fontsize=9)
        ax.set_xticks(range(len(names)), names, rotation=30)
        ax.set_yticks(range(len(names)), names)
        fig.colorbar(im, ax=ax, label="pF")
        ax.set_title("Mutual capacitance (pF)")
        fig.tight_layout()
        self.save_fig(fig, "capacitance_mutual.png",
                      "Mutual capacitance matrix heat-map (pF)")

    # ======================================================================= #
    # Port matrix
    # ======================================================================= #

    def export_matrix(self, drives):
        runs = []
        for k, d in drives:
            data, summary, job = self.load_run(d)
            runs.append((k, d, data, summary, job))
        first_summary, first_job = runs[0][3], runs[0][4]
        self.data, self.summary, self.job = runs[0][2], first_summary, \
            first_job
        self.meta_source = (runs[0][1], first_summary, first_job)
        self.import_solver(first_job)
        ports = self._matrix_ports(runs)
        self.matrix_ports = ports

        # The per-drive port records, so the raw data behind Z is on disk too.
        for drive, (k, d, data, _s, _j) in enumerate(runs):
            driven = ports[drive][2] if drive < len(ports) else "?"
            for fam, idx, name in ports:
                pre = "{}{}".format(_ARRAY_PREFIX[fam], idx)
                t = data.get(pre + "v_times")
                v = data.get(pre + "v_values")
                i = data.get(pre + "i_values")
                if t is None or v is None or i is None:
                    continue
                lab = self.label("drive", k, fam, name)
                self.write_csv("csv", lab + ".csv", ["t_s", "V_V", "I_A"],
                               [t, v, i], "timeseries",
                               "Drive {} ('{}' driven): port '{}' V and I vs "
                               "time".format(k, driven, name))
        if self.spec is None:
            raise InputError("wavesim.spectrum is required to assemble a port "
                             "matrix")
        res = self.guard("Z matrix", self._assemble, runs, ports)
        if res is None:
            return
        self.guard("Z/S matrix files", self._write_matrix, res, ports)

    def _matrix_ports(self, runs):
        """``[(family, index, name)]`` -- the port each drive energised."""
        declared = _read_json(os.path.join(self.run_dir, "matrix.json"))
        if isinstance(declared, dict) and declared.get("ports"):
            return [(str(p["family"]), int(p["index"]), str(p.get("name", "")))
                    for p in declared["ports"]]
        ports = []
        for k, d, _data, _s, job in runs:
            live = []
            for fam in ("modal", "lumped"):
                for idx, entry in enumerate(job.get(_SPEC_KEY[fam]) or []):
                    exc = entry.get("excitation") or {}
                    if fam == "lumped" and entry.get("drive", "none") == "none":
                        continue
                    if float(exc.get("amplitude", 0.0) or 0.0) != 0.0:
                        live.append((fam, idx, str(entry.get("name") or
                                                   "{} {}".format(fam, idx))))
            if len(live) != 1:
                raise InputError(
                    "drive folder {} energises {} ports ({}); a port-matrix "
                    "drive must energise exactly one (or provide matrix.json "
                    "with a 'ports' list)".format(
                        os.path.basename(d), len(live),
                        ", ".join(p[2] for p in live) or "none"))
            ports.append(live[0])
        return ports

    def _assemble(self, runs, ports):
        """``wavesim_gui.portmatrix.assemble``'s maths, on wavesim.spectrum."""
        n = len(ports)
        if n < 2:
            raise InputError("a port matrix needs at least two drives")
        if len(runs) != n:
            raise InputError("{} drive folders for {} ports".format(len(runs),
                                                                     n))
        v_cols, i_cols, in_band, freqs = [], [], None, None
        for drive, (_k, d, data, _s, _j) in enumerate(runs):
            v_col, i_col = [], []
            for p, (fam, idx, name) in enumerate(ports):
                pre = "{}{}".format(_ARRAY_PREFIX[fam], idx)
                t = data.get(pre + "v_times")
                v = data.get(pre + "v_values")
                i = data.get(pre + "i_values")
                if t is None or v is None or i is None:
                    raise InputError("drive {} recorded nothing for port "
                                     "'{}'".format(drive, name))
                sv = self.spec.spectrum((t, v), window=None, stagger=STAGGER_E,
                                        warn_undecayed=(p == drive))
                si = self.spec.spectrum((t, i), window=None, stagger=STAGGER_H,
                                        warn_undecayed=False)
                v_col.append(sv.values)
                i_col.append(si.values)
                freqs = sv.freqs
                if p == drive:
                    mag_v, mag_i = np.abs(sv.values), np.abs(si.values)
                    band = (np.isfinite(sv.values) & np.isfinite(si.values)
                            & (mag_v >= BAND_FLOOR * np.nanmax(mag_v))
                            & (mag_i >= BAND_FLOOR * np.nanmax(mag_i)))
                    in_band = band if in_band is None else (in_band & band)
            v_cols.append(v_col)
            i_cols.append(i_col)

        def _matrix(cols):
            return np.stack([np.stack([cols[k][p] for k in range(n)], axis=-1)
                             for p in range(n)], axis=-2)

        v_mat, i_mat = _matrix(v_cols), _matrix(i_cols)
        nf = v_mat.shape[0]
        cond = np.full(nf, np.inf)
        probe = (in_band & np.all(np.isfinite(i_mat), axis=(1, 2))
                 & np.all(np.isfinite(v_mat), axis=(1, 2)))
        if np.any(probe):
            cond[probe] = np.linalg.cond(i_mat[probe])
        valid = probe & np.isfinite(cond) & (cond <= COND_LIMIT)
        z = np.full((nf, n, n), complex(np.nan, np.nan))
        if np.any(valid):
            z[valid] = np.linalg.solve(
                i_mat[valid].swapaxes(-1, -2), v_mat[valid].swapaxes(-1, -2)
            ).swapaxes(-1, -2)
        return {"freqs": freqs, "z": z, "cond": cond, "valid": valid}

    def _write_matrix(self, res, ports):
        valid = res["valid"] & (res["freqs"] > 0)
        if not np.any(valid):
            self.warnings.append("port matrix: no valid (in-band, "
                                 "well-conditioned) bins")
            return
        f = res["freqs"][valid]
        z = res["z"][valid]
        cond = res["cond"][valid]
        n = len(ports)
        names = [p[2] for p in ports]
        recip = _reciprocity_error(z)
        header, cols = ["f_Hz"], [f]
        for i in range(n):
            for j in range(n):
                header += ["re_Z{}{}_ohm".format(i + 1, j + 1),
                           "im_Z{}{}_ohm".format(i + 1, j + 1)]
                cols += [z[:, i, j].real, z[:, i, j].imag]
        header += ["cond_I", "reciprocity_err"]
        cols += [cond, recip]
        self.write_csv("sparams", "zmatrix.csv", header, cols, "zmatrix",
                       "{0}x{0} impedance matrix Z(f) = V·I⁻¹ (row-major), "
                       "valid bins only; cond_I = excitation conditioning, "
                       "reciprocity_err = max|Zij-Zji|/max|Z|".format(n))
        s = _s_from_z(z, self.z0)
        lines = self._touchstone_header() + [
            "Port matrix assembled from {} drive runs (one port driven per "
            "run, all others in their terminations)".format(n)]
        lines += ["Port {}: {} ({} port)".format(k + 1, p[2], p[0])
                  for k, p in enumerate(ports)]
        lines.append("Median reciprocity error max|Zij-Zji|/max|Z| = "
                     "{:.3g}".format(float(np.nanmedian(recip))))
        self._write_touchstone(self.project_slug, f, s, lines)
        self._write_sparams_csv("sparams.csv", f, s, names,
                                "{0}x{0} S-matrix derived from the Z "
                                "matrix".format(n))
        self.guard("plot sparams", self._sparams_plot, self.project_slug, f, s,
                   names)
        self.guard("plot zmatrix", self._zmatrix_plot, f, z, names)

    def _zmatrix_plot(self, f, z, names):
        n = len(names)
        fig, axes = plt.subplots(n, n, figsize=(3.2 * n, 2.6 * n),
                                 squeeze=False, sharex=True)
        for i in range(n):
            for j in range(n):
                ax = axes[i][j]
                ax.plot(f / 1e9, z[:, i, j].real, label="Re")
                ax.plot(f / 1e9, z[:, i, j].imag, label="Im")
                ax.set_title("Z{}{}".format(i + 1, j + 1), fontsize=9)
                ax.grid(True, alpha=0.3)
                if i == n - 1:
                    ax.set_xlabel("frequency (GHz)", fontsize=8)
                if j == 0:
                    ax.set_ylabel("Ω", fontsize=8)
        axes[0][0].legend(fontsize=7)
        fig.suptitle("Z matrix: " + ", ".join(
            "{}={}".format(k + 1, nm) for k, nm in enumerate(names)),
            fontsize=9)
        fig.tight_layout()
        self.save_fig(fig, "zmatrix.png",
                      "Real/imaginary parts of every Z-matrix entry vs "
                      "frequency")

    # ======================================================================= #
    # Touchstone / S-parameter helpers
    # ======================================================================= #

    def _touchstone_header(self):
        run_dir, summary, job = self.meta_source
        lines = [
            "Wavesim export (exporter v{})".format(EXPORTER_VERSION),
            "Project: {}".format(self.project_slug),
            "Run folder: {}".format(os.path.basename(self.run_dir)),
            "Exported: {}".format(_now_iso()),
            "Solver: Wavesim FDTD, grid {}x{}x{}, dt = {} s, {} steps".format(
                summary.get("Nx"), summary.get("Ny"), summary.get("Nz"),
                _fmt(summary.get("dt", float("nan"))), summary.get("steps")),
            "S-parameters are DERIVED from Z(f) = V(f)/I(f) (the solver has "
            "no native S-parameters): S = (Z - Z0*1)(Z + Z0*1)^-1, "
            "Z0 = {:g} ohm real, same for every port".format(self.z0),
            "Only in-band bins (V and I above {:g} of their peak) are "
            "listed".format(BAND_FLOOR),
        ]
        return lines

    def _write_touchstone(self, stem, f, s, comment_lines):
        n = s.shape[1]
        p = self.path("sparams", "{}.s{}p".format(stem, n))
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            for line in comment_lines:
                fh.write("! " + line + "\n")
            fh.write("# Hz S RI R {:g}\n".format(self.z0))
            for k in range(len(f)):
                # Touchstone v1: a 2-port lists S11 S21 S12 S22 (column-major);
                # every other size is row-major, one matrix row per line.
                if n == 2:
                    order = [(0, 0), (1, 0), (0, 1), (1, 1)]
                    pairs = ["{} {}".format(_fmt(s[k, i, j].real),
                                            _fmt(s[k, i, j].imag))
                             for i, j in order]
                    fh.write(_fmt(f[k]) + " " + " ".join(pairs) + "\n")
                else:
                    for i in range(n):
                        pairs = ["{} {}".format(_fmt(s[k, i, j].real),
                                                _fmt(s[k, i, j].imag))
                                 for j in range(n)]
                        lead = _fmt(f[k]) if i == 0 else "   "
                        for c in range(0, n, 4):
                            fh.write((lead if c == 0 else "   ") + " "
                                     + " ".join(pairs[c:c + 4]) + "\n")
        self.add(p, "touchstone",
                 "Touchstone v1 {}-port S-parameters (Hz, RI, Z0 = {:g} ohm), "
                 "derived from Z".format(n, self.z0))
        return p

    def _write_sparams_csv(self, name, f, s, names, description):
        n = s.shape[1]
        header, cols = ["f_Hz"], [f]
        for i in range(n):
            for j in range(n):
                sij = s[:, i, j]
                tag = "S{}{}".format(i + 1, j + 1)
                header += ["re_" + tag, "im_" + tag, "mag_dB_" + tag,
                           "phase_deg_" + tag]
                cols += [sij.real, sij.imag,
                         20.0 * np.log10(np.maximum(np.abs(sij), 1e-300)),
                         np.degrees(np.angle(sij))]
        self.write_csv("sparams", name, header, cols, "sparams_csv",
                       "{} (Z0 = {:g} ohm; ports: {})".format(
                           description, self.z0, ", ".join(names)))

    def _sparams_plot(self, stem, f, s, names):
        n = s.shape[1]
        fig, ax = plt.subplots(figsize=(9, 4.5))
        for i in range(n):
            for j in range(n):
                ax.plot(f / 1e9, 20 * np.log10(np.maximum(np.abs(s[:, i, j]),
                                                          1e-300)),
                        label="S{}{}".format(i + 1, j + 1))
        ax.set_xlabel("frequency (GHz)")
        ax.set_ylabel("|S| (dB)")
        ax.set_title("|S| derived from Z, Z0 = {:g} Ω ({})".format(
            self.z0, ", ".join(names)))
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=9)
        fig.tight_layout()
        self.save_fig(fig, "{}_s_db.png".format(stem),
                      "|S| in dB vs frequency (derived from Z, Z0 = {:g} "
                      "ohm)".format(self.z0))

    # ======================================================================= #
    # summary.md / manifest.json
    # ======================================================================= #

    def _metadata(self):
        run_dir, summary, job = getattr(self, "meta_source",
                                        (self.run_dir, {}, {}))
        state = _read_json(os.path.join(self.run_dir, "run_state.json")) or {}
        exit_info = _read_json(os.path.join(self.run_dir, "exit.json")) or {}
        g = job.get("grid") or {}
        graded = all(g.get(a) is not None for a in ("x", "y", "z"))
        md = {
            "run_folder": self.run_dir,
            "run_id": state.get("run_id"),
            "project_slug": self.project_slug,
            "kind": self.kind,
            "run_date": (state.get("finished_at") or exit_info.get(
                "finished_at") or state.get("created_at") or _mtime_iso(
                os.path.join(run_dir, "summary.json"))),
            "params_snapshot": ("PARAMS.snapshot.md" if os.path.isfile(
                os.path.join(self.run_dir, "PARAMS.snapshot.md")) else None),
            "solver_mode": ("port_matrix (fdtd)" if self.kind == "port_matrix"
                            else self.kind),
            "Nx": summary.get("Nx", g.get("Nx")),
            "Ny": summary.get("Ny", g.get("Ny")),
            "Nz": summary.get("Nz", g.get("Nz")),
            "dx_m": g.get("dx"), "dy_m": g.get("dy", g.get("dx")),
            "dz_m": g.get("dz", g.get("dx")),
            "graded_grid": graded,
            "dt_s": summary.get("dt"),
            "steps": summary.get("steps", job.get("steps")),
            "sim_time_s": summary.get("sim_time_s"),
            "wall_time_s": summary.get("wall_time_s"),
            "wrapper_wall_s": exit_info.get("wall_s"),
            "backend": summary.get("backend", job.get("backend")),
            "conformal_pec": summary.get("conformal_pec"),
            "lossy": summary.get("lossy"),
            "subpixel": summary.get("subpixel"),
            "pml_faces": summary.get("pml_faces"),
            "pec_faces": summary.get("pec_faces"),
        }
        for key in ("pec_cells", "dielectric_cells", "lossy_cells",
                    "cut_cells", "clamped_faces", "conformal_area_threshold",
                    "conformal_area_threshold_requested"):
            if key in summary:
                md[key] = summary[key]
        if self.kind == "port_matrix":
            md["drives"] = len(getattr(self, "matrix_ports", []))
            md["matrix_ports"] = [p[2] for p in getattr(self, "matrix_ports",
                                                        [])]
        if self.kind == "electrostatic":
            es = summary.get("electrostatic") or {}
            md["es_energy_J"] = es.get("energy")
            md["es_method"] = es.get("method")
            md["es_boundary"] = es.get("boundary")
        # Run-level warnings the summary carries.
        if "conformal_area_threshold_requested" in summary:
            self.warnings.append(
                "conformal clamp threshold raised from {} to {} ({} faces "
                "clamped)".format(summary["conformal_area_threshold_requested"],
                                  summary.get("conformal_area_threshold"),
                                  summary.get("clamped_faces")))
        for p in summary.get("ports") or []:
            div = p.get("sheet_divergence")
            if div is not None and div > 1e-8:
                self.warnings.append("modal port '{}': sheet divergence {:.3g} "
                                     "(should be ~1e-14)".format(p.get("name"),
                                                                 div))
        log = os.path.join(self.run_dir, "run.log")
        if os.path.isfile(log):
            try:
                with open(log, "r", encoding="utf-8", errors="replace") as fh:
                    hits = [ln.strip() for ln in fh
                            if re.search(r"warn", ln, re.I)]
                for ln in hits[:20]:
                    self.warnings.append("run.log: " + ln[:300])
            except OSError:
                pass
        return md

    def finish(self):
        md = self._metadata()
        summary_path = os.path.join(self.out_dir, "summary.md")
        manifest_path = os.path.join(self.out_dir, "manifest.json")
        self.add(summary_path, "summary", "This file: run metadata, warnings, "
                                          "errors and the list of exported "
                                          "files")
        self.add(manifest_path, "manifest", "Machine-readable list of every "
                                            "exported file (path, kind, "
                                            "description) plus errors")
        files = sorted(self.files, key=lambda f: (f["kind"] in ("summary",
                                                                "manifest"),
                                                  f["path"]))
        lines = ["# Wavesim results export — {}".format(
            os.path.basename(self.run_dir)), ""]
        lines.append("## Run metadata")
        lines.append("")
        for key, val in md.items():
            if val is None:
                continue
            if isinstance(val, float):
                val = _fmt(val)
            lines.append("- **{}**: {}".format(key, val))
        lines.append("- **exported_at**: {}".format(_now_iso()))
        lines.append("- **z0_ohm** (S-parameter reference): {:g}".format(
            self.z0))
        lines.append("")
        params = os.path.join(self.run_dir, "PARAMS.snapshot.md")
        if os.path.isfile(params):
            lines.append("## Parameters (from PARAMS.snapshot.md)")
            lines.append("")
            lines.append("```")
            try:
                with open(params, "r", encoding="utf-8",
                          errors="replace") as fh:
                    plines = fh.read().splitlines()
                lines.extend(plines[:80])
                if len(plines) > 80:
                    lines.append("... ({} more lines)".format(len(plines) - 80))
            except OSError as exc:
                lines.append("(unreadable: {})".format(exc))
            lines.append("```")
            lines.append("")
        lines.append("## Warnings")
        lines.append("")
        if self.warnings:
            lines.extend("- " + w for w in self.warnings)
        else:
            lines.append("- none")
        lines.append("")
        lines.append("## Errors")
        lines.append("")
        if self.errors:
            lines.extend("- **{}**: {}".format(e["item"], e["error"])
                         for e in self.errors)
        else:
            lines.append("- none")
        lines.append("")
        if self.notes:
            lines.append("## Notes")
            lines.append("")
            lines.extend("- " + n for n in self.notes)
            lines.append("")
        lines.append("## Files ({})".format(len(files)))
        lines.append("")
        lines.append("| path | kind | description |")
        lines.append("|---|---|---|")
        for f in files:
            lines.append("| `{}` | {} | {} |".format(
                f["path"], f["kind"], f["description"].replace("|", "\\|")))
        lines.append("")
        with open(summary_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines))
        manifest = {
            "version": EXPORTER_VERSION,
            "run_dir": self.run_dir,
            "project_slug": self.project_slug,
            "kind": self.kind,
            "exported_at": _now_iso(),
            "z0_ohm": self.z0,
            "metadata": _jsonable(md),
            "files": files,
            "warnings": self.warnings,
            "errors": self.errors,
            "notes": self.notes,
        }
        with open(manifest_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(manifest, fh, indent=2)
        return len(files)


# --------------------------------------------------------------------------- #
# Module-level helpers
# --------------------------------------------------------------------------- #

class InputError(Exception):
    """The run folder cannot be exported at all (exit code 2)."""


def _fmt(v):
    if isinstance(v, (bool, np.bool_)):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"{}"'.format(v.replace('"', '""')) if ("," in v or '"' in v) \
            else v
    if v is None:
        return ""
    try:
        return "{:.10g}".format(float(v))
    except (TypeError, ValueError):
        return str(v)


def _num(v):
    return "" if v is None else v


def _now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _mtime_iso(path):
    try:
        return datetime.datetime.fromtimestamp(
            os.path.getmtime(path)).astimezone().isoformat(timespec="seconds")
    except OSError:
        return None


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _drive_dirs(run_dir):
    """``[(k, path)]`` of ``drive_<k>`` folders holding results, sorted by k."""
    out = []
    try:
        names = os.listdir(run_dir)
    except OSError:
        return out
    for name in names:
        m = re.fullmatch(r"drive_(\d+)", name)
        path = os.path.join(run_dir, name)
        if m and os.path.isfile(os.path.join(path, "results.npz")):
            out.append((int(m.group(1)), path))
    return sorted(out)


def _s_from_z(z, z0):
    """``S = (Z − Z0·1)(Z + Z0·1)⁻¹`` per bin; *z* is ``(nf, N, N)``."""
    n = z.shape[-1]
    eye = np.eye(n)
    a = z - z0 * eye
    b = z + z0 * eye
    # S = A·B⁻¹  <=>  Bᵀ·Sᵀ = Aᵀ
    return np.linalg.solve(b.swapaxes(-1, -2), a.swapaxes(-1, -2)).swapaxes(
        -1, -2)


def _reciprocity_error(z):
    asym = np.max(np.abs(z - z.swapaxes(-1, -2)), axis=(1, 2))
    scale = np.max(np.abs(z), axis=(1, 2))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(scale > 0, asym / scale, np.nan)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export a Wavesim run folder to CSV/Touchstone/PNG/GIF.")
    parser.add_argument("run_dir")
    parser.add_argument("out_dir")
    parser.add_argument("--z0", type=float, default=50.0,
                        help="S-parameter reference impedance, ohm (default "
                             "50)")
    parser.add_argument("--project-slug", default=None,
                        help="Name used for sparams/<slug>.sNp (default: "
                             "derived from the run folder path)")
    parser.add_argument("--mp4", action="store_true",
                        help="Also write MP4 animations when ffmpeg is on PATH")
    args = parser.parse_args(argv)
    if not args.z0 > 0:
        sys.stderr.write("--z0 must be positive\n")
        return 2
    exp = Exporter(args.run_dir, args.out_dir, z0=args.z0,
                   project_slug=args.project_slug, mp4=args.mp4)
    try:
        n = exp.run()
    except InputError as exc:
        sys.stderr.write("unusable input: {}\n".format(exc))
        # Still leave a summary when we got far enough to have an out_dir.
        if os.path.isdir(exp.out_dir) and exp.kind == "port_matrix":
            exp.errors.append({"item": "port matrix", "error": str(exc)})
            n = exp.finish()
            print("EXPORTED {} files".format(n))
            return 0
        print("EXPORTED 0 files")
        return 2
    except Exception as exc:
        traceback.print_exc()
        exp.errors.append({"item": "export", "error": "{}: {}".format(
            type(exc).__name__, exc)})
        try:
            n = exp.finish()
        except Exception:
            print("EXPORTED 0 files")
            return 2
        print("EXPORTED {} files".format(n))
        return 0
    print("EXPORTED {} files".format(n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
