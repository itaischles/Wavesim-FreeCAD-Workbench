# -*- coding: utf-8 -*-
"""Port-matrix extraction: Z(f) / Y(f) over a chosen set of ports.

What it is
----------
A single run answers "what does this port present, with everything else
terminated as it is". That is one number per port, and it is not a property of
the structure -- change a termination and it changes. The **matrix** is the
property: ``V = Z·I`` relating every port's voltage to every port's current,
from which any termination's answer can be computed.

Getting it costs **one run per port**. Each run measures one current vector and
one voltage vector, so it contributes N scalar equations against the N² unknowns
of Z; N linearly independent excitation states are needed, and driving one port
per run with the rest passive is the way to guarantee independence. Reciprocity
(``Z_ij = Z_ji``) halves the unknowns but does not buy a run: for N = 2 it buys
nothing, and the schemes that exploit it for N ≥ 3 are badly conditioned.

What *does* buy runs is choosing the ports. A 2×2 sub-block of a six-port
structure is two runs, with the other four left sitting in their terminations --
and the answer is then the two-port Z of "structure *including* those
terminations", which is usually the wanted thing. That is why this takes a port
selection rather than always doing every port it can find.

Why it is cheap-ish
-------------------
The geometry never changes between drives, so the **voxelisation is paid once**
and only the solve repeats. Moving the drive is a pure ``job.json`` edit:
:func:`drive_job` copies the spec and sets one excitation's amplitude, because
the runner already treats an amplitude-0 port as a pure passive termination --
that is how an unenergized row of a multi-conductor modal port works (see
:func:`wavesim_gui.modal_port.modal_port_specs`). No geometry is re-cut, no
materials array is rewritten, and the N runs share one ``materials.npz``.

The one thing that must hold
----------------------------
``V = Z·I`` describes a **source-free** network. Any excitation left running in
the background makes the relation affine (``V = Z·I + V_oc``) and the matrix
solve silently returns nonsense. So :func:`drive_job` silences *everything* --
the point source, every Gaussian beam, every modal row and every lumped port,
enrolled or not -- and then re-energizes exactly one. A document holding a SPICE
port is refused rather than swept: its drive lives in a netlist this module
cannot zero, and a co-simulated source that keeps running would corrupt every
entry of the matrix without any visible symptom.

Silencing is exact for every port family here. The solver composes a lumped
port's voltage drive as a **Thévenin** source (load in series with the EMF) and
its current drive as a **Norton** one (load in parallel), so amplitude 0 leaves
precisely the load -- which also means a passive load-only lumped port can be
enrolled: give it a voltage drive, and in the runs where it is not driving it
behaves exactly as it does today. A modal port's sheet absorbs whether it is
driven or not. Every port therefore presents the *same* termination in all N
runs, which is what makes the N excitation states independent by construction.

What this module does not do
----------------------------
**De-embedding.** The matrix is reported at the port planes, so it includes
whatever line runs between the plane and the component, plus (for a lumped port)
the bridged cell's own ``C_cell = ε·dA/dl`` in parallel. Subtracting a "thru"
run is a separate measurement with its own UI.

**Committing to an equivalent circuit.** A T or Π is a *choice* of topology laid
over the matrix; the matrix itself is topology-free, and that is what the viewer
in :mod:`wavesim_gui.results` draws.

Qt-free above the GUI fence, so the spec surgery and the assembly can be
exercised headlessly (``tools/check_portmatrix.py``).
"""

import copy
import os

import FreeCAD


__all__ = [
    "MatrixPort", "FAMILY_MODAL", "FAMILY_LUMPED",
    "spec_ports", "document_ports", "select_ports", "common_excitation",
    "drive_job", "SweepError", "assemble", "drive_dir_name",
]


FAMILY_MODAL = "modal"
FAMILY_LUMPED = "lumped"

# family -> (job.json list key, results.npz key prefix). Both indices are the
# entry's position in the job spec list, which is exactly what the runner uses
# for its result keys (``port_{si}v`` counts modal_ports; ``lumped_{idx}v``
# counts lumped_ports), so a port's identity survives from the spec we write to
# the arrays we read back -- provided the lists keep their length and order,
# which they do because a drive only ever edits an amplitude in place.
_SPEC_KEY = {FAMILY_MODAL: "modal_ports", FAMILY_LUMPED: "lumped_ports"}
_ARRAY_PREFIX = {FAMILY_MODAL: "port_", FAMILY_LUMPED: "lumped_"}
_FAMILY_LABEL = {FAMILY_MODAL: "Modal", FAMILY_LUMPED: "Lumped"}


class SweepError(Exception):
    """The document cannot be swept as asked (reported to the user verbatim)."""


class MatrixPort(object):
    """One row/column of the matrix: a port that records its own V(t) and I(t).

    ``index`` is the entry's position in its ``job.json`` list, which is also
    what names its arrays in ``results.npz``. ``name`` is the spec's own name --
    the string the dialog shows, the results tree labels, and the only thing
    tying a port chosen before voxelisation to the spec entry built after it.
    """

    __slots__ = ("family", "index", "name")

    def __init__(self, family, index, name):
        self.family = family
        self.index = int(index)
        self.name = str(name)

    @property
    def key_v(self):
        return "{}{}v".format(_ARRAY_PREFIX[self.family], self.index)

    @property
    def key_i(self):
        return "{}{}i".format(_ARRAY_PREFIX[self.family], self.index)

    @property
    def kind_label(self):
        return _FAMILY_LABEL[self.family]

    def entry(self, spec):
        """This port's dict inside *spec* (raises if the spec has changed)."""
        return (spec.get(_SPEC_KEY[self.family]) or [])[self.index]

    def __repr__(self):
        return "MatrixPort({}, {}, {!r})".format(self.family, self.index,
                                                 self.name)


# --------------------------------------------------------------------------- #
# Enumerating the ports of a document / of a built job spec
# --------------------------------------------------------------------------- #

def spec_ports(spec):
    """Every matrix-capable port in a built job *spec*, in job.json order.

    Authoritative: the indices here are the ones the runner will use. Modal
    ports come first (matching the runner's own ``planes`` ordering), then
    lumped ports.
    """
    ports = []
    for family in (FAMILY_MODAL, FAMILY_LUMPED):
        for index, entry in enumerate(spec.get(_SPEC_KEY[family]) or []):
            ports.append(MatrixPort(
                family, index,
                entry.get("name") or "{} port {}".format(
                    _FAMILY_LABEL[family], index)))
    return ports


def document_ports(sim):
    """The same list, built from the document without voxelising it.

    The picker has to offer ports *before* the geometry sweep, which is the slow
    half of a run and would be absurd to pay before the user has chosen what to
    sweep. So the spec builders are called here directly -- the very functions
    :func:`wavesim_gui.voxelize.build_job_from_document` calls, so the list
    matches entry for entry, including the rows they drop (a conductor that does
    not reach its port plane, a lumped port whose terminals do not resolve).

    **Only the names and the families off this list are usable.** The specs are
    built against a zero origin because the real one is a product of the
    voxelisation, so every coordinate in them is wrong by the domain corner.
    Nothing here reaches a job file: :func:`select_ports` re-binds the choice to
    the authoritative :func:`spec_ports` list by name once the real spec exists.
    """
    from wavesim_gui import lumped_port as lumped_mod
    from wavesim_gui import modal_port as modal_mod

    if sim is None:
        return []
    origin = (0.0, 0.0, 0.0)
    modal_specs = []
    for obj in modal_mod.find_modal_ports(sim):
        if modal_mod.excitation_mode(obj) != modal_mod.MODE_WAVEFORM:
            # A SPICE-driven modal port is co-simulated, not amplitude-driven.
            continue
        modal_specs.extend(modal_mod.modal_port_specs(obj, origin))
    lumped_specs = [s for s in
                    (lumped_mod.lumped_port_spec(obj, origin)
                     for obj in lumped_mod.find_lumped_ports(sim)) if s]
    return spec_ports({"modal_ports": modal_specs,
                       "lumped_ports": lumped_specs})


def select_ports(spec, names):
    """Bind chosen port *names* to the authoritative spec entries, in order.

    Raises :class:`SweepError` naming what went missing, rather than sweeping a
    different set of ports than the user picked: between the picker and the
    voxelisation the user may have edited the document, and a port that quietly
    dropped out would shift every index after it and mislabel the whole matrix.
    """
    available = {port.name: port for port in spec_ports(spec)}
    chosen, missing = [], []
    for name in names:
        port = available.get(name)
        (chosen if port is not None else missing).append(port or name)
    if missing:
        raise SweepError(
            "These ports were selected but are not in the job the document "
            "just built:\n  {}\n\nThe document may have changed since the "
            "ports were chosen. Reopen the port matrix dialog and pick "
            "again.".format("\n  ".join(missing)))
    if len(chosen) < 2:
        raise SweepError(
            "A port matrix needs at least two ports; {} selected.".format(
                len(chosen)))
    return chosen


# --------------------------------------------------------------------------- #
# Spec surgery: one job per drive
# --------------------------------------------------------------------------- #

def _excitation_of(entry):
    """The ``excitation`` sub-dict of a spec entry, or None."""
    exc = entry.get("excitation")
    return exc if isinstance(exc, dict) else None


def common_excitation(spec, ports):
    """The one waveform every drive will use.

    All N runs must illuminate the same band or the matrix is only valid on the
    intersection, and a matrix whose entries are trustworthy over different
    spans is a trap. So one waveform is taken -- the first enrolled port that
    carries one -- and stamped onto every drive.
    """
    for port in ports:
        exc = _excitation_of(port.entry(spec))
        if exc is not None:
            return copy.deepcopy(exc)
    raise SweepError(
        "None of the selected ports carries an excitation waveform to drive "
        "the sweep with. Give at least one of them a waveform (any drive "
        "amplitude; the sweep sets it per run).")


def _silence(spec):
    """Zero every excitation in *spec*, in place. Returns the count silenced.

    ``V = Z·I`` describes a source-free network. One stray drive left running --
    a point source, a beam, a lumped port the user forgot -- makes it affine and
    the matrix solve returns a confident wrong answer, so this is a sweep over
    *everything* rather than over the enrolled ports.
    """
    n = 0
    targets = [spec.get("source")]
    for key in ("modal_ports", "lumped_ports", "gaussian_beams"):
        targets.extend(spec.get(key) or [])
    for entry in targets:
        if not isinstance(entry, dict):
            continue
        exc = _excitation_of(entry)
        if exc is not None and "amplitude" in exc:
            exc["amplitude"] = 0.0
            n += 1
    return n


def drive_job(spec, ports, driven, excitation):
    """A copy of *spec* in which only ``ports[driven]`` is excited.

    The driven port keeps its load and its termination; only the source
    amplitude moves. A lumped port with no drive at all is given a **voltage**
    drive, which composes as a Thévenin source in series with its load -- so at
    amplitude 0 it is electrically the passive element it already was, and the
    N runs still present one another the same network.
    """
    job = copy.deepcopy(spec)
    _silence(job)
    port = ports[driven]
    entry = port.entry(job)

    if port.family == FAMILY_LUMPED and entry.get("drive", "none") == "none":
        # Thévenin: the EMF sits in series with the load, so this adds a drive
        # without removing the element the user placed.
        entry["drive"] = "voltage"
    entry["excitation"] = copy.deepcopy(excitation)
    return job


def drive_dir_name(index):
    """Sub-directory holding drive *index*'s outputs, inside the sweep dir."""
    return "drive_{}".format(int(index))


# --------------------------------------------------------------------------- #
# Assembly: N runs -> Z(f)
# --------------------------------------------------------------------------- #

#: Bins whose excitation-matrix condition number exceeds this are not reported.
#: The solve is exact arithmetic on numbers that carry no information there --
#: it returns a matrix, and the matrix is noise amplified by the reciprocal of
#: the conditioning.
COND_LIMIT = 1.0e10

#: Out-of-band cutoff, matching :mod:`wavesim_gui.spectrum`'s own.
BAND_FLOOR = 1.0e-3


def _load_pair(workdir, key):
    """``(times, values)`` for one recorded series, or ``(None, None)``."""
    import numpy as np

    path = os.path.join(workdir, "results.npz")
    if not os.path.isfile(path):
        return None, None
    with np.load(path) as data:
        if key + "_values" not in data.files:
            return None, None
        return (np.array(data[key + "_times"]),
                np.array(data[key + "_values"]))


def assemble(sweep_dir, ports, window=None, cond_limit=COND_LIMIT,
             floor=BAND_FLOOR):
    """Build ``Z(f)`` from the N drive directories under *sweep_dir*.

    Every series is transformed with the same options (so a taper is safe for
    the ratios) and each with its own stagger -- V is E-derived, I is the
    impressed current half a step behind it. Then, per frequency bin, the two
    N×N matrices ``V[:, k]`` and ``I[:, k]`` (column k = drive k) give
    ``Z = V·I⁻¹``.

    Returns a dict with ``freqs``, ``z`` (nf, N, N complex, NaN where not
    reported), ``cond`` (nf), ``valid`` (nf bool) and ``names``.

    Two things are masked rather than reported. **Out of band**, where the
    excitation put no energy and every entry would be a quotient of round-off.
    And **ill-conditioned bins**, where the N excitation states have collapsed
    towards dependence -- which is not a numerical detail but the measurement
    failing: it means the drives did not actually probe N independent states of
    the structure, and inverting anyway turns that into a plausible-looking
    matrix.
    """
    import numpy as np

    from wavesim_gui import spectrum as spec_mod

    n = len(ports)
    if n < 2:
        raise SweepError("A port matrix needs at least two ports.")

    v_cols, i_cols, in_band, freqs = [], [], None, None
    for drive in range(n):
        workdir = os.path.join(sweep_dir, drive_dir_name(drive))
        v_col, i_col = [], []
        for port in ports:
            times, volts = _load_pair(workdir, port.key_v)
            _t, amps = _load_pair(workdir, port.key_i)
            if times is None or volts is None or amps is None:
                raise SweepError(
                    "Drive {} recorded nothing for port '{}'. The sweep's "
                    "output in\n  {}\nis incomplete -- re-run it."
                    .format(drive + 1, port.name, workdir))
            sv = spec_mod.spectrum(times, volts, window=window,
                                   stagger=spec_mod.STAGGER_E)
            si = spec_mod.spectrum(times, amps, window=window,
                                   stagger=spec_mod.STAGGER_H)
            v_col.append(sv.values)
            i_col.append(si.values)
            freqs = sv.freqs
            if port is ports[drive]:
                # The driving port's own pair defines this run's band; the
                # matrix is only reported where *every* run has signal.
                lo_hi = np.isfinite(sv.values) & np.isfinite(si.values)
                mag_v, mag_i = np.abs(sv.values), np.abs(si.values)
                run_band = (lo_hi
                            & (mag_v >= floor * np.nanmax(mag_v))
                            & (mag_i >= floor * np.nanmax(mag_i)))
                in_band = run_band if in_band is None else (in_band & run_band)
        v_cols.append(v_col)
        i_cols.append(i_col)

    # ``(bin, port, drive)``: the *column* index is the run. That is what makes
    # the per-bin system one matrix equation V = Z·I rather than N² ratios --
    # port i's voltage under drive k involves every port's current, not only
    # port k's.
    def _matrix(cols):
        return np.stack([np.stack([cols[k][p] for k in range(n)], axis=-1)
                         for p in range(n)], axis=-2)

    v_mat, i_mat = _matrix(v_cols), _matrix(i_cols)

    nf = v_mat.shape[0]
    cond = np.full(nf, np.inf)
    probe = (in_band
             & np.all(np.isfinite(i_mat), axis=(1, 2))
             & np.all(np.isfinite(v_mat), axis=(1, 2)))
    if np.any(probe):
        cond[probe] = np.linalg.cond(i_mat[probe])

    valid = probe & np.isfinite(cond) & (cond <= cond_limit)
    z = np.full((nf, n, n), complex(np.nan, np.nan))
    if np.any(valid):
        # Z·I = V  ⇒  Iᵀ·Zᵀ = Vᵀ, which is the form np.linalg.solve takes.
        z[valid] = np.linalg.solve(
            i_mat[valid].swapaxes(-1, -2), v_mat[valid].swapaxes(-1, -2)
        ).swapaxes(-1, -2)

    return {"freqs": freqs, "z": z, "cond": cond, "valid": valid,
            "names": [port.name for port in ports]}


def reciprocity_error(z):
    """Per-bin ``max|Z_ij − Z_ji| / max|Z|`` — a free check on the extraction.

    A passive reciprocal structure has a symmetric Z. Nothing in the assembly
    enforces that, so how far it misses is an independent measure of whether the
    N runs really did see one linear network: a band where it climbs is a band
    where something (an undecayed record, a drive that never settled, a
    conditioning cliff) has corrupted the solve.
    """
    import numpy as np

    out = np.full(z.shape[0], np.nan)
    # Only the reported bins: ``nanmax`` over an all-NaN row is a RuntimeWarning
    # per bin, and an unreported matrix is most of the axis -- enough of them to
    # bury anything else the console had to say.
    live = np.flatnonzero(np.any(np.isfinite(z), axis=(1, 2)))
    if not live.size:
        return out
    block = z[live]
    asym = np.nanmax(np.abs(block - block.swapaxes(-1, -2)), axis=(1, 2))
    scale = np.nanmax(np.abs(block), axis=(1, 2))
    good = np.isfinite(asym) & np.isfinite(scale) & (scale > 0)
    out[live[good]] = asym[good] / scale[good]
    return out


def admittance(z):
    """``Y = Z⁻¹``, bin by bin, NaN where Z is not reported or is singular."""
    import numpy as np

    y = np.full(z.shape, complex(np.nan, np.nan))
    good = np.all(np.isfinite(z), axis=(1, 2))
    if np.any(good):
        block = z[good]
        with np.errstate(invalid="ignore", divide="ignore"):
            ok = np.linalg.cond(block) <= COND_LIMIT
            idx = np.flatnonzero(good)[ok]
            if idx.size:
                y[idx] = np.linalg.inv(z[idx])
    return y


def sweep_dir_for(doc):
    """The document's port-matrix output directory.

    Its own folder beside ``run``, so a matrix sweep and an ordinary Run never
    overwrite one another -- they are different measurements of the document and
    a user who swept yesterday should not lose it by pressing Run today.
    """
    from wavesim_gui import job as job_mod

    return job_mod.workdir_for(doc, prefix="matrix")


def prepare_sweep_dir(doc, n_drives):
    """Create (and clear) the sweep directory and its per-drive subdirectories."""
    import shutil

    from wavesim_gui import job as job_mod

    sweep = sweep_dir_for(doc)
    os.makedirs(sweep, exist_ok=True)
    for name in job_mod.JOB_ARTEFACTS:
        path = os.path.join(sweep, name)
        if os.path.isfile(path):
            os.remove(path)
    for drive in range(n_drives):
        path = os.path.join(sweep, drive_dir_name(drive))
        shutil.rmtree(path, ignore_errors=True)
        os.makedirs(path, exist_ok=True)
    # Drive folders from a previous, wider sweep would otherwise sit there
    # looking like part of this one.
    for entry in sorted(os.listdir(sweep)):
        if entry.startswith("drive_") and entry not in {
                drive_dir_name(k) for k in range(n_drives)}:
            shutil.rmtree(os.path.join(sweep, entry), ignore_errors=True)
    return sweep


def harvest_drive(sweep_dir, drive):
    """Move one finished run's outputs into its drive subdirectory.

    The runner writes ``results.npz``/``summary.json`` beside the ``job.json``
    it read, and every drive reads the same ``materials.npz`` -- which is the
    point, since that array is the expensive one and is identical across the
    sweep. So each run happens in the sweep root and its two outputs are moved
    down afterwards.
    """
    import shutil

    target = os.path.join(sweep_dir, drive_dir_name(drive))
    os.makedirs(target, exist_ok=True)
    moved = []
    for name in ("results.npz", "summary.json", "job.json"):
        src = os.path.join(sweep_dir, name)
        if os.path.isfile(src):
            dst = os.path.join(target, name)
            if os.path.isfile(dst):
                os.remove(dst)
            shutil.move(src, dst)
            moved.append(name)
    return moved


def _log(message):
    FreeCAD.Console.PrintMessage("Wavesim: " + message + "\n")


# --------------------------------------------------------------------------- #
# GUI: the port picker and the sweep driver
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = hasattr(Gui, "addCommand")
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

    import os as _os

    from wavesim_gui.commands import active_simulation

    _WB_DIR = _os.path.join(FreeCAD.getUserAppDataDir(), "Mod",
                            "wavesim-workbench")
    _MATRIX_ICON = _os.path.join(_WB_DIR, "Resources", "icons", "matrix.svg")

    def _qt():
        try:
            from PySide import QtCore, QtWidgets
        except ImportError:
            from PySide import QtCore
            from PySide import QtGui as QtWidgets
        return QtCore, QtWidgets

    class PortMatrixDialog(object):
        """Modal port picker: which ports the matrix should span.

        Deliberately a plain dialog rather than a task panel: it configures one
        action and then that action runs, with nothing left on the document
        afterwards to edit. The list it offers is built straight from the
        document (see :func:`document_ports`), so it opens instantly instead of
        making the user pay the geometry sweep before choosing what to sweep.
        """

        def __init__(self, sim, parent=None):
            _QtCore, QtWidgets = _qt()
            self.ports = document_ports(sim)
            self.accepted = False
            self.chosen = []

            self.dialog = QtWidgets.QDialog(parent)
            self.dialog.setWindowTitle("Wavesim: Port Matrix")
            self.dialog.resize(560, 460)
            layout = QtWidgets.QVBoxLayout(self.dialog)

            header = QtWidgets.QLabel(
                "Choose the ports the Z / Y matrix should span. The solver runs "
                "<b>once per selected port</b>, driving that one while every "
                "other source in the document is silenced — the geometry is "
                "voxelised only once for the whole sweep.<br><br>"
                "Ports you leave out stay in their terminations and become part "
                "of the network the matrix describes."
            )
            header.setWordWrap(True)
            layout.addWidget(header)

            self.table = QtWidgets.QTableWidget(0, 3)
            self.table.setHorizontalHeaderLabels(["Include", "Port", "Kind"])
            self.table.verticalHeader().setVisible(False)
            self.table.setEditTriggers(
                QtWidgets.QAbstractItemView.NoEditTriggers)
            head = self.table.horizontalHeader()
            head.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
            head.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
            head.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
            layout.addWidget(self.table)

            self.checks = []
            for row, port in enumerate(self.ports):
                self.table.insertRow(row)
                check = QtWidgets.QCheckBox()
                check.setChecked(True)
                holder = QtWidgets.QWidget()
                hlay = QtWidgets.QHBoxLayout(holder)
                hlay.setContentsMargins(0, 0, 0, 0)
                hlay.setAlignment(_QtCore.Qt.AlignCenter)
                hlay.addWidget(check)
                self.table.setCellWidget(row, 0, holder)
                self.table.setItem(row, 1,
                                   QtWidgets.QTableWidgetItem(port.name))
                self.table.setItem(row, 2,
                                   QtWidgets.QTableWidgetItem(port.kind_label))
                check.toggled.connect(self._update_count)
                self.checks.append(check)

            self.count = QtWidgets.QLabel()
            layout.addWidget(self.count)

            self.note = QtWidgets.QLabel(
                "The matrix is reported at the port planes: it includes any "
                "line between a plane and the component, and a lumped port's "
                "own bridged-cell capacitance. Nothing here de-embeds those."
            )
            self.note.setWordWrap(True)
            self.note.setStyleSheet("color: gray;")
            layout.addWidget(self.note)

            buttons = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.Ok
                | QtWidgets.QDialogButtonBox.Cancel)
            self.ok_button = buttons.button(QtWidgets.QDialogButtonBox.Ok)
            self.ok_button.setText("Run sweep")
            buttons.accepted.connect(self._accept)
            buttons.rejected.connect(self.dialog.reject)
            layout.addWidget(buttons)

            self._update_count()

        def _selected(self):
            return [port for port, check in zip(self.ports, self.checks)
                    if check.isChecked()]

        def _update_count(self, *_args):
            n = len(self._selected())
            if n < 2:
                self.count.setText(
                    "<b>Select at least two ports.</b> A matrix relating one "
                    "port to itself is the impedance a single run already gives.")
            else:
                self.count.setText(
                    "<b>{n} ports &rarr; a {n}&times;{n} matrix, {n} solver "
                    "runs.</b>".format(n=n))
            self.ok_button.setEnabled(n >= 2)

        def _accept(self):
            self.chosen = self._selected()
            self.accepted = True
            self.dialog.accept()

        def exec_(self):
            self.dialog.exec_()
            return self.accepted

    def _refuse(parent, text):
        _QtCore, QtWidgets = _qt()
        QtWidgets.QMessageBox.warning(parent, "Wavesim Port Matrix", text)

    def run_sweep(doc, chosen_names, parent=None):
        """Voxelise once, then run the solver once per port. Returns the sweep
        directory and the bound port list, or ``(None, None)``.

        The order here is the whole economy of the feature: the geometry sweep
        and the materials array are produced a single time and every drive
        reuses them, so an N-port matrix costs one voxelisation and N solves
        rather than N of each.
        """
        from wavesim_gui import job as job_mod
        from wavesim_gui import run as run_mod
        from wavesim_gui import voxelize as vox_mod

        vox_dialog, vox_cb = run_mod.voxelization_progress(
            parent, "Wavesim Port Matrix", "Voxelizing geometry (once)...")
        try:
            spec, arrays = vox_mod.build_job_from_document(doc, progress=vox_cb)
        except vox_mod.VoxelizationCancelled:
            vox_dialog.close()
            FreeCAD.Console.PrintWarning("Wavesim: port matrix cancelled.\n")
            return None, None
        except vox_mod.GridRequiredError as exc:
            vox_dialog.close()
            _refuse(parent, str(exc))
            return None, None
        finally:
            vox_dialog.close()

        if spec is None:
            _refuse(parent,
                    "This document has no materials assigned, so there is no "
                    "structure to extract a matrix from.")
            return None, None
        if spec.get("spice_ports"):
            # Its drive lives in a netlist this module cannot zero, and a source
            # left running in every run corrupts every entry of the matrix
            # without any visible symptom.
            _refuse(parent,
                    "This document has SPICE co-simulation ports. The sweep "
                    "cannot silence a netlist-driven source, and one left "
                    "running would corrupt every entry of the matrix.\n\n"
                    "Remove the SPICE ports, or drive them from a Modal or "
                    "Lumped port instead.")
            return None, None

        try:
            ports = select_ports(spec, chosen_names)
            excitation = common_excitation(spec, ports)
        except SweepError as exc:
            _refuse(parent, str(exc))
            return None, None

        sweep_dir = prepare_sweep_dir(doc, len(ports))
        vox_mod.write_materials(sweep_dir, arrays)
        steps = int(spec.get("steps", 0))

        for drive, port in enumerate(ports):
            _log("port matrix drive {}/{}: {}".format(
                drive + 1, len(ports), port.name))
            job_mod.write_job(sweep_dir, drive_job(spec, ports, drive,
                                                   excitation))
            summary = run_mod.run_job(
                sweep_dir, steps, parent=parent,
                message="Drive {} of {}: {}".format(
                    drive + 1, len(ports), port.name),
            )
            if summary is None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: port matrix stopped at drive {} of {}; the "
                    "matrix needs all of them.\n".format(drive + 1, len(ports)))
                return None, None
            harvest_drive(sweep_dir, drive)

        return sweep_dir, ports

    class CommandPortMatrix:
        """Toolbar command: extract a Z / Y matrix over a chosen set of ports."""

        def GetResources(self):
            return {
                "Pixmap": _MATRIX_ICON,
                "MenuText": "Port Matrix (Z / Y)",
                "ToolTip": "Run the solver once per selected port and extract "
                           "the Z / Y matrix relating them",
            }

        def Activated(self):
            _QtCore, QtWidgets = _qt()
            doc = FreeCAD.ActiveDocument
            sim = active_simulation(doc)
            main = Gui.getMainWindow()

            picker = PortMatrixDialog(sim, main)
            if len(picker.ports) < 2:
                _refuse(main,
                        "A port matrix needs at least two ports that record "
                        "their own V(t) and I(t).\n\nThis document has {}. Add "
                        "Modal Ports or Lumped Ports and try again."
                        .format(len(picker.ports)))
                return
            if not picker.exec_():
                return

            # Asked *after* the picker (which is instant) and *before* the
            # voxelisation (which is not), so the user has committed to a sweep
            # before being warned, and can still rescue the last one before
            # anything is overwritten.
            from wavesim_gui import run as run_mod

            if not run_mod.confirm_overwrite(
                    sweep_dir_for(doc), parent=main,
                    title="Wavesim Port Matrix"):
                return

            names = [port.name for port in picker.chosen]
            sweep_dir, ports = run_sweep(doc, names, parent=main)
            if sweep_dir is None:
                return

            from wavesim_gui import results as results_mod

            try:
                results_mod.build_matrix_results(doc, sim, sweep_dir, ports)
            except SweepError as exc:
                _refuse(main, str(exc))
                return
            QtWidgets.QMessageBox.information(
                main, "Wavesim Port Matrix",
                "{n}x{n} matrix extracted from {n} runs.\n\n"
                "Double-click the Port Matrix node in the tree to view it.\n\n"
                "Output: {d}".format(n=len(ports), d=sweep_dir))

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    Gui.addCommand("Wavesim_PortMatrix", CommandPortMatrix())
