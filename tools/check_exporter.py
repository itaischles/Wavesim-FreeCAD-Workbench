# -*- coding: utf-8 -*-
"""Gate: ``exporter.py`` turns real runner output into the documented files.

Run under the **Wavesim Python** (numpy + matplotlib + pillow; the ``wavesim``
repo is found via ``WAVESIM_PATH`` or the workbench settings file, exactly like
``tools/check_lumped_port.py``)::

    "C:\\Program Files\\Python314\\python.exe" tools\\check_exporter.py [--keep]

Four tiny jobs are written by hand (following the ``runner.py`` docstring
schema), solved with ``runner.run_job`` and exported through the exporter's
**CLI** (a subprocess, so the exit code and the ``EXPORTED <n> files`` line are
checked too):

1. ``fdtd`` -- point source, E/H probes, an E snapshot, energy + dissipation,
   voltage and current monitors: time-series CSVs, spectra, plots, a GIF.
2. ``coax`` -- a square air coax terminated by a modal port on z0 and PML on z1:
   port V/I, Z(f), a mode plot and ``sparams/coax.s1p``.
3. ``electrostatic`` -- two cubes, capacitance matrix, phi/E slices and a field
   line.
4. ``matrix`` -- two lumped ports, one run per drive (``drive_000``,
   ``drive_001``): Z matrix, ``.s2p``, ``zmatrix.csv``.

Plus the unusable-input path (an empty folder must exit 2) and a run with one
corrupt item (exported anyway, error recorded). Scratch data goes
in ``./_check_exporter_work`` (removed unless ``--keep``). The last line is
``CHECK EXPORTER: PASS`` or ``CHECK EXPORTER: FAIL``.
"""

import copy
import csv
import glob
import json
import os
import shutil
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_WB = os.path.dirname(_HERE)
sys.path.insert(0, _WB)

import numpy as np  # noqa: E402

import runner  # noqa: E402

EXPORTER = os.path.join(_WB, "exporter.py")

_failures = []


def check(name, ok, detail=""):
    print("  [{}] {}{}".format("ok" if ok else "FAIL", name,
                               "" if ok or not detail else " -- " + detail))
    if not ok:
        _failures.append(name)
    return ok


def _wavesim_repo():
    """``WAVESIM_PATH`` first, else the workbench's settings file."""
    repo = os.environ.get("WAVESIM_PATH")
    if repo and os.path.isdir(repo):
        return repo
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
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


# --------------------------------------------------------------------------- #
# Job builders
# --------------------------------------------------------------------------- #

def _vacuum(n):
    ones = np.ones(n, dtype=np.float64)
    return {k: ones.copy() for k in
            ("eps_x", "eps_y", "eps_z", "mu_x", "mu_y", "mu_z")}


def _write(workdir, job, arrays):
    os.makedirs(workdir, exist_ok=True)
    with open(os.path.join(workdir, "job.json"), "w", encoding="utf-8") as fh:
        json.dump(job, fh, indent=2)
    np.savez(os.path.join(workdir, "materials.npz"), **arrays)


def job_fdtd(repo):
    n, dx = 30, 1.0e-3
    c = 15 * dx
    arrays = _vacuum((n, n, n))
    arrays["pec_mask"] = np.zeros((n, n, n), dtype=bool)
    job = {
        "wavesim_path": repo, "backend": "numba", "steps": 300,
        "grid": {"Nx": n, "Ny": n, "Nz": n, "dx": dx, "dy": dx, "dz": dx},
        "boundary": {"d_pml": 6, "faces": ["x0", "x1", "y0", "y1", "z0", "z1"],
                     "pec_faces": []},
        "source": {"component": "Ez", "x": c, "y": c, "z": c,
                   "excitation": {"type": "gaussian", "fmax": 30.0e9,
                                  "amplitude": 1.0}},
        "monitors": {
            "energy": {"full": True, "interior": True},
            "dissipation": {"full": False, "interior": True},
            "probes": [
                {"name": "Probe Ez", "component": "Ez",
                 "x": c + 4 * dx, "y": c, "z": c},
                {"name": "Probe Hy", "component": "Hy",
                 "x": c + 4 * dx, "y": c, "z": c},
            ],
            "snapshots": [{"name": "Mid plane", "field": "E", "normal": "z",
                           "position": c, "every_N_steps": 10}],
            "voltages": [{"name": "V gap",
                          "path": [[c + 3 * dx, c, c - 2 * dx],
                                   [c + 3 * dx, c, c + 2 * dx]]}],
            "currents": [{"name": "I loop",
                          "path": [[c - 2 * dx, c - 2 * dx, c],
                                   [c + 2 * dx, c - 2 * dx, c],
                                   [c + 2 * dx, c + 2 * dx, c],
                                   [c - 2 * dx, c + 2 * dx, c],
                                   [c - 2 * dx, c - 2 * dx, c]]}],
        },
    }
    return job, arrays


def job_coax(repo):
    nx, nz, dx = 20, 30, 1.0e-3
    arrays = _vacuum((nx, nx, nz))
    pec = np.zeros((nx, nx, nz), dtype=bool)
    pec[:3, :, :] = pec[-3:, :, :] = True
    pec[:, :3, :] = pec[:, -3:, :] = True
    pec[8:12, 8:12, :] = True
    arrays["pec_mask"] = pec
    job = {
        "wavesim_path": repo, "backend": "numba", "steps": 500,
        "grid": {"Nx": nx, "Ny": nx, "Nz": nz, "dx": dx, "dy": dx, "dz": dx},
        "boundary": {"d_pml": 8, "faces": ["z1"],
                     "pec_faces": ["x0", "x1", "y0", "y1"]},
        "source": None,
        "modal_ports": [{
            "name": "P1", "normal": "z", "position": 0.0, "face": "z0",
            "conductor_point": [10.0 * dx, 10.0 * dx],
            "excitation": {"type": "gaussian", "fmax": 20.0e9,
                           "amplitude": 1.0},
        }],
        "monitors": {"energy": {"full": False, "interior": True}},
    }
    return job, arrays


def job_electrostatic(repo):
    n, dx = 30, 1.0e-3
    arrays = _vacuum((n, n, n))
    pec = np.zeros((n, n, n), dtype=bool)
    pid = np.zeros((n, n, n), dtype=np.int32)
    pec[8:13, 12:19, 12:19] = True
    pid[8:13, 12:19, 12:19] = 1
    pec[17:22, 12:19, 12:19] = True
    pid[17:22, 12:19, 12:19] = 2
    arrays["pec_mask"] = pec
    arrays["pec_id"] = pid
    xs = np.linspace(13.0 * dx, 17.0 * dx, 21)
    pts = [[float(x), 15.0 * dx, 15.0 * dx] for x in xs]
    job = {
        "wavesim_path": repo, "mode": "electrostatic", "steps": 0,
        "pec_names": {"left": 1, "right": 2},
        "grid": {"Nx": n, "Ny": n, "Nz": n, "dx": dx, "dy": dx, "dz": dx},
        "electrostatic": {
            "potentials": {"left": 1.0, "right": 0.0},
            "boundary": "ground", "capacitance": True, "method": "auto",
            "slices": [
                {"name": "phi mid", "field": "phi", "normal": "z",
                 "position": 15.0 * dx},
                {"name": "E mid", "field": "E", "normal": "z",
                 "position": 15.0 * dx},
            ],
            "lines": [{
                "name": "gap axis", "field": "E", "points": pts,
                "tangents": [[1.0, 0.0, 0.0]] * len(pts),
                "s": [float(x - xs[0]) for x in xs],
                "vertex_s": [0.0, float(xs[-1] - xs[0])], "normal": None,
            }],
        },
    }
    return job, arrays


def job_matrix(repo):
    n, dx = 24, 0.5e-3
    arrays = _vacuum((n, n, n))
    exc = {"type": "gaussian", "fmax": 40.0e9, "amplitude": 1.0}
    job = {
        "wavesim_path": repo, "backend": "numba", "steps": 500,
        "grid": {"Nx": n, "Ny": n, "Nz": n, "dx": dx, "dy": dx, "dz": dx},
        "boundary": {"d_pml": 6, "faces": ["x0", "x1", "y0", "y1", "z0", "z1"],
                     "pec_faces": []},
        "source": None,
        "lumped_ports": [
            {"name": "Port A", "topology": "series", "drive": "voltage",
             "resistance": 50.0, "excitation": dict(exc),
             "p0": [9 * dx, 12 * dx, 11 * dx], "p1": [9 * dx, 12 * dx, 13 * dx]},
            {"name": "Port B", "topology": "series", "drive": "voltage",
             "resistance": 50.0, "excitation": dict(exc),
             "p0": [15 * dx, 12 * dx, 11 * dx],
             "p1": [15 * dx, 12 * dx, 13 * dx]},
        ],
        "monitors": {"energy": {"full": False, "interior": False}},
    }
    return job, arrays


# --------------------------------------------------------------------------- #
# Helpers for the assertions
# --------------------------------------------------------------------------- #

def run_exporter(run_dir, out_dir, *extra):
    cmd = [sys.executable, EXPORTER, run_dir, out_dir] + list(extra)
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    if proc.returncode not in (0, 2):
        print(proc.stdout[-3000:])
        print(proc.stderr[-3000:])
    return proc.returncode, last, proc.stderr


def header(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.readline().strip()


def read_csv(path):
    with open(path, "r", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    return rows[0], rows[1:]


def gif_frames(path):
    from PIL import Image

    with Image.open(path) as im:
        return getattr(im, "n_frames", 1)


def parse_touchstone(path):
    """``(option_line, freqs, rows)`` -- rows are numeric token lists."""
    option, tokens = None, []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.split("!", 1)[0].strip()
            if not line:
                continue
            if line.startswith("#"):
                option = line
                continue
            tokens.extend(float(t) for t in line.split())
    return option, tokens


def common_checks(out, tag):
    check(tag + ": summary.md", os.path.isfile(os.path.join(out, "summary.md")))
    mpath = os.path.join(out, "manifest.json")
    if not check(tag + ": manifest.json", os.path.isfile(mpath)):
        return {}
    with open(mpath, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    missing = [f["path"] for f in manifest["files"]
               if not os.path.isfile(os.path.join(out, f["path"]))]
    check(tag + ": every manifest path exists", not missing, str(missing))
    check(tag + ": no export errors", not manifest["errors"],
          json.dumps(manifest["errors"])[:800])
    return manifest


def ascending(values):
    return all(b > a for a, b in zip(values, values[1:]))


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #

def case_fdtd(root, repo):
    print("1. FDTD: probes, snapshot, energy, V/I monitors")
    rd = os.path.join(root, "fdtdproj", "runs", "20261102-1040_basic")
    _write(rd, *job_fdtd(repo))
    runner.run_job(rd)
    with open(os.path.join(rd, "PARAMS.snapshot.md"), "w",
              encoding="utf-8") as fh:
        fh.write("# PARAMS — fdtdproj\n\n## Model\nBOX = 30 [mm]  # box\n")
    out = os.path.join(root, "out_fdtd")
    code, last, _err = run_exporter(rd, out)
    check("exit code 0", code == 0, str(code))
    check("prints EXPORTED <n> files", last.startswith("EXPORTED "), last)
    m = common_checks(out, "fdtd")
    check("project slug derived from path",
          m.get("project_slug") == "fdtdproj", str(m.get("project_slug")))
    csvd = os.path.join(out, "csv")
    check("probe Ez header",
          header(os.path.join(csvd, "probe_probe_ez.csv"))
          == "t_s,Ez_V_per_m")
    check("probe Hy header",
          header(os.path.join(csvd, "probe_probe_hy.csv"))
          == "t_s,Hy_A_per_m")
    check("probe spectrum header",
          header(os.path.join(csvd, "probe_probe_ez_spectrum.csv"))
          == "f_Hz,mag_V_per_m_per_Hz,phase_deg,re_V_per_m_per_Hz,"
             "im_V_per_m_per_Hz")
    _h, rows = read_csv(os.path.join(csvd, "probe_probe_ez_spectrum.csv"))
    check("spectrum frequencies ascending",
          len(rows) > 5 and ascending([float(r[0]) for r in rows]))
    check("energy header",
          header(os.path.join(csvd, "energy.csv")) == "t_s,energy_J")
    check("energy interior csv",
          os.path.isfile(os.path.join(csvd, "energy_interior.csv")))
    check("dissipation header",
          header(os.path.join(csvd, "dissipation_interior.csv"))
          == "t_s,power_W")
    check("voltage header",
          header(os.path.join(csvd, "voltage_v_gap.csv")) == "t_s,V_V")
    check("current header",
          header(os.path.join(csvd, "current_i_loop.csv")) == "t_s,I_A")
    check("voltage spectrum",
          os.path.isfile(os.path.join(csvd, "voltage_v_gap_spectrum.csv")))
    _h, rows = read_csv(os.path.join(csvd, "probe_probe_ez.csv"))
    check("probe csv has every step", len(rows) == 300, str(len(rows)))
    for name in ("probe_probe_ez.png", "probe_probe_ez_spectrum.png",
                 "energy.png", "voltage_v_gap.png", "current_i_loop.png",
                 "snapshot_mid_plane_peak.png"):
        check("plot " + name, os.path.isfile(os.path.join(out, "plots", name)))
    gif = os.path.join(out, "animations", "snapshot_mid_plane.gif")
    if check("snapshot GIF", os.path.isfile(gif)):
        nfr = gif_frames(gif)
        check("GIF has >1 frame (30 expected)", nfr > 1, str(nfr))
    with open(os.path.join(out, "summary.md"), "r", encoding="utf-8") as fh:
        text = fh.read()
    check("summary.md has grid + params + files", "**Nx**: 30" in text
          and "BOX = 30" in text and "csv/probe_probe_ez.csv" in text)
    # --mp4 without ffmpeg must not fail.
    out2 = os.path.join(root, "out_fdtd_mp4")
    code, last, _ = run_exporter(rd, out2, "--mp4", "--project-slug", "My Box")
    m2 = common_checks(out2, "fdtd --mp4")
    check("--mp4 exit 0", code == 0, str(code))
    check("--project-slug slugified", m2.get("project_slug") == "my_box")
    if not shutil.which("ffmpeg"):
        check("--mp4 skipped with a note",
              any("ffmpeg" in n for n in m2.get("notes", [])))


def case_coax(root, repo):
    print("2. modal port (coax): Z(f), S11 Touchstone")
    rd = os.path.join(root, "coax", "runs", "20261102-1041_coax")
    _write(rd, *job_coax(repo))
    summary = runner.run_job(rd)
    z_ref = (summary.get("ports") or [{}])[0].get("reference_impedance")
    out = os.path.join(root, "out_coax")
    code, last, _ = run_exporter(rd, out, "--z0", "50")
    check("exit code 0", code == 0, str(code))
    common_checks(out, "coax")
    zcsv = os.path.join(out, "csv", "modal_p1_impedance.csv")
    if check("impedance csv", os.path.isfile(zcsv)):
        h, rows = read_csv(zcsv)
        check("impedance header",
              ",".join(h) == "f_Hz,re_Z_ohm,im_Z_ohm,mag_Z_ohm,phase_deg")
        f = [float(r[0]) for r in rows]
        check("Z rows in band and ascending", len(rows) > 3 and ascending(f))
        zmid = complex(float(rows[len(rows) // 2][1]),
                       float(rows[len(rows) // 2][2]))
        check("matched line: Z ~ Z_ref mid-band",
              z_ref and abs(zmid - z_ref) < 0.25 * z_ref,
              "Z={:.4g} Z_ref={}".format(zmid, z_ref))
    s1p = os.path.join(out, "sparams", "coax.s1p")
    if check("coax.s1p", os.path.isfile(s1p)):
        option, tok = parse_touchstone(s1p)
        check("option line", option == "# Hz S RI R 50", str(option))
        check("1+2*N^2 = 3 columns", len(tok) % 3 == 0 and len(tok) > 0)
        f = tok[0::3]
        check("frequencies ascending", ascending(f))
        mags = [abs(complex(a, b)) for a, b in zip(tok[1::3], tok[2::3])]
        check("|S11| <= 1 (passive)", max(mags) <= 1.0 + 1e-6,
              "{:.4g}".format(max(mags)))
    for name in ("modal_p1.png", "modal_p1_impedance.png",
                 "modal_p1_spectrum.png", "coax_s_db.png", "mode_p1_0.png"):
        check("plot " + name, os.path.isfile(os.path.join(out, "plots", name)))
    for name in ("modal_p1.csv", "modal_p1_v_spectrum.csv",
                 "modal_p1_i_spectrum.csv", "ports.csv", "modes.csv"):
        check("csv " + name, os.path.isfile(os.path.join(out, "csv", name)))
    check("port V/I header",
          header(os.path.join(out, "csv", "modal_p1.csv")) == "t_s,V_V,I_A")


def case_electrostatic(root, repo):
    print("3. electrostatic: capacitance, slices, field line")
    rd = os.path.join(root, "caps", "runs", "20261102-1042_es")
    _write(rd, *job_electrostatic(repo))
    runner.run_job(rd)
    out = os.path.join(root, "out_es")
    code, last, _ = run_exporter(rd, out)
    check("exit code 0", code == 0, str(code))
    m = common_checks(out, "es")
    check("kind electrostatic", m.get("kind") == "electrostatic")
    cap = os.path.join(out, "csv", "capacitance_matrix_F.csv")
    if check("capacitance_matrix_F.csv", os.path.isfile(cap)):
        h, rows = read_csv(cap)
        check("capacitance header", h == ["matrix", "conductor", "left",
                                          "right"], str(h))
        mx = [r for r in rows if r[0] == "maxwell"]
        mu = [r for r in rows if r[0] == "mutual"]
        check("2 maxwell + 2 mutual rows", len(mx) == 2 and len(mu) == 2)
        c11, c12 = float(mx[0][2]), float(mx[0][3])
        check("Maxwell C11 > 0 > C12", c11 > 0 > c12,
              "{} {}".format(c11, c12))
    check("charges csv header",
          header(os.path.join(out, "csv", "charges_C.csv"))
          == "conductor,kind,potential_V,charge_C")
    check("phi slice png",
          os.path.isfile(os.path.join(out, "plots", "snapshot_phi_mid.png")))
    check("E slice png",
          os.path.isfile(os.path.join(out, "plots", "snapshot_e_mid.png")))
    check("phi slice csv header",
          header(os.path.join(out, "csv", "snapshot_phi_mid_slice.csv"))
          == "x_m,y_m,phi_V")
    lcsv = os.path.join(out, "csv", "line_gap_axis.csv")
    if check("field line csv", os.path.isfile(lcsv)):
        check("field line header", header(lcsv) ==
              "s_m,x_m,y_m,z_m,Ex_V_per_m,Ey_V_per_m,Ez_V_per_m,"
              "E_tangential_V_per_m", header(lcsv))
    check("no GIF for a static slice",
          not glob.glob(os.path.join(out, "animations", "*.gif")))


def case_matrix(root, repo):
    print("4. port matrix: two lumped ports, two drives")
    base, arrays = job_matrix(repo)
    md = os.path.join(root, "twoport", "runs", "20261102-1043_matrix")
    os.makedirs(md, exist_ok=True)
    np.savez(os.path.join(md, "materials.npz"), **arrays)
    for k in range(2):
        job = copy.deepcopy(base)
        for idx, port in enumerate(job["lumped_ports"]):
            port["excitation"]["amplitude"] = 1.0 if idx == k else 0.0
        d = os.path.join(md, "drive_{:03d}".format(k))
        _write(d, job, arrays)
        runner.run_job(d)
    out = os.path.join(root, "out_matrix")
    code, last, _ = run_exporter(md, out)
    check("exit code 0", code == 0, str(code))
    m = common_checks(out, "matrix")
    check("kind port_matrix", m.get("kind") == "port_matrix")
    s2p = os.path.join(out, "sparams", "twoport.s2p")
    if check("twoport.s2p", os.path.isfile(s2p)):
        option, tok = parse_touchstone(s2p)
        check("option line", option == "# Hz S RI R 50", str(option))
        check("1+2*N^2 = 9 columns", len(tok) % 9 == 0 and len(tok) > 0)
        f = tok[0::9]
        check("frequencies ascending", ascending(f))
        s21 = [complex(a, b) for a, b in zip(tok[3::9], tok[4::9])]
        s12 = [complex(a, b) for a, b in zip(tok[5::9], tok[6::9])]
        err = max(abs(a - b) for a, b in zip(s21, s12))
        check("reciprocal: S21 ~ S12", err < 0.05, "{:.3g}".format(err))
    zc = os.path.join(out, "sparams", "zmatrix.csv")
    if check("zmatrix.csv", os.path.isfile(zc)):
        h, rows = read_csv(zc)
        check("zmatrix header", h[:3] == ["f_Hz", "re_Z11_ohm", "im_Z11_ohm"]
              and h[-2:] == ["cond_I", "reciprocity_err"] and len(h) == 11,
              str(h))
    check("sparams.csv",
          os.path.isfile(os.path.join(out, "sparams", "sparams.csv")))
    check("zmatrix.png", os.path.isfile(os.path.join(out, "plots",
                                                     "zmatrix.png")))
    check("per-drive V/I csv", len(glob.glob(os.path.join(
        out, "csv", "drive_*.csv"))) == 4)


def case_bad_input(root):
    print("5. unusable input")
    empty = os.path.join(root, "empty_run")
    os.makedirs(empty, exist_ok=True)
    code, last, _ = run_exporter(empty, os.path.join(root, "out_empty"))
    check("missing results.npz exits 2", code == 2, str(code))
    check("still prints EXPORTED 0 files", last == "EXPORTED 0 files", last)

    print("6. one broken item does not sink the export")
    broken = os.path.join(root, "broken_run")
    os.makedirs(broken, exist_ok=True)
    t = np.arange(200) * 1e-12
    np.savez(os.path.join(broken, "results.npz"),
             probe_0_times=t, probe_0_values=np.exp(-((t - 4e-11) / 1e-11) ** 2),
             snapshot_0_Ex_data=np.zeros(5), snapshot_0_times=np.zeros(5))
    with open(os.path.join(broken, "summary.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"ok": True, "dt": 1e-12, "steps": 200, "Nx": 4, "Ny": 4,
                   "Nz": 4,
                   "probes": [{"name": "p", "component": "Ex"},
                              {"name": "absent", "component": "Ey"}],
                   "snapshots": [{"name": "bad", "field": "E",
                                  "components": ["Ex"], "frames": 5}]}, fh)
    out = os.path.join(root, "out_broken")
    code, last, _ = run_exporter(broken, out)
    check("exit 0 despite a broken snapshot", code == 0, str(code))
    with open(os.path.join(out, "manifest.json"), "r", encoding="utf-8") as fh:
        man = json.load(fh)
    check("snapshot failure recorded",
          any("bad" in e["item"] for e in man["errors"]), str(man["errors"]))
    check("probe still exported",
          os.path.isfile(os.path.join(out, "csv", "probe_p.csv")))
    with open(os.path.join(out, "summary.md"), "r", encoding="utf-8") as fh:
        check("error listed in summary.md", "snapshot bad" in fh.read())


def main(argv):
    keep = "--keep" in argv
    repo = _wavesim_repo()
    if not repo:
        print("cannot locate the wavesim repo (set WAVESIM_PATH)")
        print("CHECK EXPORTER: FAIL")
        return 1
    os.environ["WAVESIM_PATH"] = repo
    root = os.path.join(os.getcwd(), "_check_exporter_work")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    t0 = time.time()
    try:
        for case in (case_fdtd, case_coax, case_electrostatic, case_matrix):
            try:
                case(root, repo)
            except Exception as exc:
                import traceback

                traceback.print_exc()
                check("{} ran".format(case.__name__), False,
                      "{}: {}".format(type(exc).__name__, exc))
        case_bad_input(root)
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
    print("elapsed {:.1f} s".format(time.time() - t0))
    if _failures:
        print("{} failure(s): {}".format(len(_failures), ", ".join(_failures)))
        print("CHECK EXPORTER: FAIL")
        return 1
    print("CHECK EXPORTER: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
