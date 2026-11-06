# -*- coding: utf-8 -*-
"""Run driver for the Wavesim workbench bridge (FreeCAD side).

Spawns the conda-side ``runner.py`` on a serialised job directory with
``QProcess``, shows a ``QProgressDialog`` driven by the ``PROGRESS n/N`` lines
the runner prints to stdout, and lets the user cancel by killing the process.
On success it loads ``summary.json`` and returns it to the caller.

:func:`start_console_run` is the non-blocking alternative Run Simulation uses by
default on Windows: the runner goes into its own console window (through
``console_runner.py``, which draws the status and progress there and kills the
solver when the window is closed), this function returns at once, and a timer
hands the summary to a callback when ``exit.json`` says the run is over.

Kept separate from :mod:`wavesim_gui.commands` so the QProcess plumbing can be
reused (and tested) independently of the command wiring.
"""

import json
import os
import time

import FreeCAD

import wavesim_settings


# Path to the conda-executed runner at the workbench root. FreeCAD ``exec``s the
# init files without a stable ``__file__``, so derive it from the app-data dir.
_WB_DIR = os.path.join(
    FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench"
)
RUNNER_PATH = os.path.join(_WB_DIR, "runner.py")
# The console wrapper a non-blocking run starts runner.py through (it runs under
# the solver Python too, stdlib-only).
CONSOLE_RUNNER_PATH = os.path.join(_WB_DIR, "console_runner.py")

# Written by console_runner.py when runner.py exits by itself; its absence with
# the console gone means the window was closed, i.e. the run was cancelled.
EXIT_FILE = "exit.json"

# How often a console run is checked for having ended.
_POLL_MS = 500

_CREATE_NEW_CONSOLE = 0x00000010
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _format_duration(seconds):
    """Return a compact human-readable duration like ``1h 03m 05s``.

    Sub-minute times keep one decimal (``4.2s``) so short runs still show
    something meaningful; longer times drop to whole units.
    """
    seconds = max(0.0, float(seconds))
    if seconds < 60.0:
        return "{:.1f}s".format(seconds)
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "{}h {:02d}m {:02d}s".format(hours, minutes, secs)
    return "{}m {:02d}s".format(minutes, secs)


def _progress_label(state, done, n_steps, percent=False):
    """Compose the multi-line progress-dialog label with a live ETA.

    Keeps the latest coarse ``STATUS`` text (``state["status"]``) on the first
    line and appends step count, percent, throughput and an estimated time to
    completion built from the wall-clock rate since the first PROGRESS line.
    ``state`` carries the anchor (``step_t0``/``step_done0``) between calls.
    With *percent* the steps are only units of the bar (an electrostatic solve
    has none of its own), so the step count and the steps/s rate are left out.
    """
    now = time.perf_counter()
    if state["step_t0"] is None:
        state["step_t0"] = now
        state["step_done0"] = done

    n_steps = max(1, int(n_steps))
    done = max(0, min(int(done), n_steps))
    pct = 100.0 * done / n_steps

    if percent:
        lines = [state["status"], "{:.1f}% complete".format(pct)]
    else:
        lines = [state["status"], "Step {:,} / {:,}  ({:.1f}%)".format(
            done, n_steps, pct)]

    elapsed = now - state["step_t0"]
    stepped = done - state["step_done0"]
    if stepped > 0 and elapsed > 0.0:
        rate = stepped / elapsed
        remaining = n_steps - done
        eta = remaining / rate if rate > 0 else 0.0
        if percent:
            lines.append("Elapsed {} · ~{} remaining".format(
                _format_duration(elapsed), _format_duration(eta)))
        else:
            lines.append(
                "Elapsed {} · ~{} remaining · {:,.0f} steps/s".format(
                    _format_duration(elapsed), _format_duration(eta), rate)
            )
    else:
        lines.append("Elapsed {} · estimating time remaining...".format(
            _format_duration(elapsed)))

    return "\n".join(lines)


def _read_summary(workdir):
    """Return the parsed ``summary.json`` from *workdir*, or ``None``."""
    path = os.path.join(workdir, "summary.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def confirm_overwrite(workdir, parent=None, title="Wavesim"):
    """Ask before a run replaces the results already sitting in *workdir*.

    Results live in a stable per-document folder (see :mod:`wavesim_gui.job`), so
    every run after the first destroys its predecessor's output. Returns ``True``
    to proceed — including when the folder holds nothing to lose, in which case
    no dialog is shown.
    """
    from wavesim_gui import job as job_mod

    if not job_mod.existing_artefacts(workdir):
        return True

    try:
        from PySide import QtWidgets
    except ImportError:
        from PySide import QtGui as QtWidgets

    box = QtWidgets.QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QtWidgets.QMessageBox.Warning)
    box.setText("Previous results for this document will be overwritten.")
    box.setInformativeText(
        "The results in\n\n{}\n\nwill be replaced by this run. Move or rename "
        "the folder first if you want to keep them.".format(workdir)
    )
    box.setStandardButtons(
        QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel
    )
    box.button(QtWidgets.QMessageBox.Ok).setText("Overwrite and Run")
    box.setDefaultButton(QtWidgets.QMessageBox.Ok)
    return box.exec_() == QtWidgets.QMessageBox.Ok


def voxelization_progress(parent=None, title="Wavesim",
                          message="Voxelizing geometry..."):
    """Return ``(dialog, callback)`` for showing voxelisation progress.

    The voxeliser's section-and-sample sweep runs on the GUI thread, so without
    pumping events the window looks frozen. Pass *callback* as
    :func:`voxelize.build_job_from_document`'s ``progress``: it sizes the bar on
    the first call, advances it, processes events so the dialog paints and the
    Cancel button works, and returns ``dialog.wasCanceled()`` to abort the
    sweep. The caller must ``close()`` the dialog when done.
    """
    from PySide import QtCore
    try:
        from PySide import QtWidgets
    except ImportError:
        from PySide import QtGui as QtWidgets

    dialog = QtWidgets.QProgressDialog(message, "Cancel", 0, 0, parent)
    dialog.setWindowTitle(title)
    dialog.setWindowModality(QtCore.Qt.WindowModal)
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    dialog.setMinimumWidth(420)
    dialog.show()
    dialog.raise_()
    QtWidgets.QApplication.processEvents()

    state = {"sized": False}

    def callback(done, total):
        if not state["sized"]:
            dialog.setRange(0, max(1, int(total)))
            state["sized"] = True
        dialog.setValue(min(int(done), dialog.maximum()))
        QtWidgets.QApplication.processEvents()
        return dialog.wasCanceled()

    return dialog, callback


def run_job(workdir, n_steps, parent=None, message="Running FDTD simulation...",
            busy=False, percent=False):
    """Run the job in *workdir* out-of-process with a progress dialog.

    Returns the summary dict on success, or ``None`` if the run was cancelled or
    failed (a console message / dialog explains failures). ``n_steps`` sizes the
    progress bar and must match the job's ``steps``; *message* is the initial
    progress dialog label (e.g. the TEM mode-solve uses its own wording).

    The runner streams two kinds of feedback on stdout: ``PROGRESS n/N`` lines
    drive the bar, and ``STATUS <text>`` lines replace the dialog label for the
    coarse, non-numeric stages (loading the solver, factorising a TEM plane,
    ...). Pass ``busy=True`` for a job with no meaningful step count (the TEM
    mode-solve): the bar then runs as an animated indeterminate indicator so the
    window visibly is not frozen while ``STATUS`` lines report the live stage.
    Pass ``percent=True`` when the PROGRESS units are only a scale for the bar
    (the electrostatic solve): the label then shows a percentage and time
    remaining rather than a step count and rate.
    """
    from PySide import QtCore
    try:
        from PySide import QtWidgets
    except ImportError:  # older FreeCAD shims expose widgets under QtGui
        from PySide import QtGui as QtWidgets

    python_exe = wavesim_settings.get_wavesim_python()
    if not os.path.isfile(python_exe):
        QtWidgets.QMessageBox.critical(
            parent, "Wavesim Run",
            "Solver Python interpreter not found:\n{}\n\n"
            "Set it in Wavesim -> Settings.".format(python_exe),
        )
        return None
    if not os.path.isfile(RUNNER_PATH):
        QtWidgets.QMessageBox.critical(
            parent, "Wavesim Run",
            "runner.py not found at:\n{}".format(RUNNER_PATH),
        )
        return None

    process = QtCore.QProcess(parent)
    process.setProcessChannelMode(QtCore.QProcess.SeparateChannels)

    # Accumulated stderr, surfaced if the run fails. ``status`` holds the latest
    # coarse STATUS label so PROGRESS updates can re-append the live ETA line
    # under it; ``step_t0``/``step_done0`` anchor the throughput estimate to the
    # first PROGRESS line of the time-stepping loop (so solver load / TEM
    # factorisation time doesn't drag the rate down).
    state = {
        "stderr": "", "stdout_tail": "", "cancelled": False,
        "status": message, "step_t0": None, "step_done0": 0,
    }

    # A busy (indeterminate) job uses a 0..0 range so Qt animates the bar; a
    # normal run uses 0..n_steps and is driven by the PROGRESS lines.
    bar_max = 0 if busy else n_steps
    progress = QtWidgets.QProgressDialog(
        message, "Cancel", 0, bar_max, parent
    )
    progress.setWindowTitle("Wavesim Run")
    progress.setWindowModality(QtCore.Qt.WindowModal)
    progress.setMinimumDuration(0)
    progress.setAutoClose(False)
    progress.setAutoReset(False)
    # Roomier so multi-line STATUS messages are readable.
    progress.setMinimumWidth(420)
    # Show the dialog explicitly and paint it now. QProgressDialog only
    # auto-shows itself from setValue(), so a busy (0..0) job that never sets a
    # value would otherwise stay hidden while the solver blocks — the window
    # then looks frozen until the run ends. forceShow()/processEvents make it
    # appear immediately for both job kinds.
    if not busy:
        progress.setValue(0)
    progress.show()
    progress.raise_()
    QtWidgets.QApplication.processEvents()

    loop = QtCore.QEventLoop()

    def on_stdout():
        text = bytes(process.readAllStandardOutput()).decode(
            "utf-8", "replace"
        )
        # Lines may arrive split across reads; keep a small tail buffer.
        buf = state["stdout_tail"] + text
        lines = buf.split("\n")
        state["stdout_tail"] = lines.pop()  # incomplete trailing fragment
        for line in lines:
            line = line.strip()
            if line.startswith("STATUS "):
                text = line[len("STATUS "):].replace("\\n", "\n")
                state["status"] = text
                progress.setLabelText(text)
            elif line.startswith("PROGRESS ") and not busy:
                try:
                    done = int(line.split()[1].split("/")[0])
                except (ValueError, IndexError):
                    continue
                progress.setValue(min(done, n_steps))
                progress.setLabelText(
                    _progress_label(state, done, n_steps, percent))

    def on_stderr():
        state["stderr"] += bytes(process.readAllStandardError()).decode(
            "utf-8", "replace"
        )

    def on_cancel():
        state["cancelled"] = True
        process.kill()

    def on_finished(*_args):
        loop.quit()

    process.readyReadStandardOutput.connect(on_stdout)
    process.readyReadStandardError.connect(on_stderr)
    progress.canceled.connect(on_cancel)
    process.finished.connect(on_finished)

    process.start(python_exe, [RUNNER_PATH, workdir])
    if not process.waitForStarted(10000):
        progress.close()
        QtWidgets.QMessageBox.critical(
            parent, "Wavesim Run",
            "Failed to start the solver process:\n{} {}".format(
                python_exe, RUNNER_PATH
            ),
        )
        return None

    # Block on a local event loop so the dialog stays responsive (progress
    # updates, Cancel) without returning control to FreeCAD until the run ends.
    loop.exec_()
    on_stdout()  # drain any final buffered lines
    on_stderr()
    # close() invokes QProgressDialog.cancel(), which emits canceled() and would
    # otherwise re-fire on_cancel and mark a finished run as cancelled. A genuine
    # Cancel has already set the flag via the button, so dropping the connection
    # here only suppresses the spurious close-triggered signal.
    try:
        progress.canceled.disconnect(on_cancel)
    except (RuntimeError, TypeError):
        pass
    progress.close()

    if state["cancelled"]:
        FreeCAD.Console.PrintWarning("Wavesim: run cancelled.\n")
        return None

    if process.exitStatus() != QtCore.QProcess.NormalExit or process.exitCode() != 0:
        summary = _read_summary(workdir) or {}
        error = summary.get("error") or state["stderr"] or "unknown error"
        FreeCAD.Console.PrintError("Wavesim: run failed: {}\n".format(error))
        QtWidgets.QMessageBox.critical(
            parent, "Wavesim Run",
            "The solver run failed:\n\n{}".format(error),
        )
        return None

    summary = _read_summary(workdir)
    if summary is None or not summary.get("ok", False):
        FreeCAD.Console.PrintError(
            "Wavesim: run produced no valid summary in {}\n".format(workdir)
        )
        return None

    _report_complete(summary, workdir)
    return summary


def _report_complete(summary, workdir):
    FreeCAD.Console.PrintMessage(
        "Wavesim: run complete in {:.2f}s ({} steps, dt={:.3e}s). "
        "Output in {}\n".format(
            summary.get("wall_time_s", 0.0), summary.get("steps", "?"),
            summary.get("dt", float("nan")), workdir,
        )
    )


# --------------------------------------------------------------------------- #
# Non-blocking run in a console window
# --------------------------------------------------------------------------- #

# Console runs still going, by normalised workdir. Holds each run's process and
# poll timer, so neither is garbage collected while the solver works.
_CONSOLE_RUNS = {}


def _run_key(workdir):
    return os.path.normcase(os.path.abspath(workdir))


def console_runs_supported():
    """Whether this platform can run the solver in its own console window.

    The wrapper's cancel-on-close rests on a Windows Job Object; elsewhere a run
    takes the blocking :func:`run_job` path.
    """
    return os.name == "nt"


def console_run_active(workdir=None):
    """True while a console run is going -- in *workdir*, or anywhere if None."""
    if workdir is None:
        return bool(_CONSOLE_RUNS)
    return _run_key(workdir) in _CONSOLE_RUNS


def _read_exit(workdir):
    """Return the parsed ``exit.json`` from *workdir*, or ``None``."""
    try:
        with open(os.path.join(workdir, EXIT_FILE), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _discard_exit(workdir):
    """Delete ``exit.json`` (and any half-written temp of it) once it is read.

    It exists only to tell this side the run is over, so a finished run folder
    holds exactly what a blocking run leaves.
    """
    try:
        names = os.listdir(workdir)
    except OSError:
        return
    for name in names:
        if name == EXIT_FILE or (name.startswith(EXIT_FILE + ".")
                                 and name.endswith(".tmp")):
            try:
                os.remove(os.path.join(workdir, name))
            except OSError:
                pass


def _gui_busy():
    """Whether now is a bad moment to rebuild the Results tree.

    A task panel has a transaction of its own open and a modal dialog is in the
    middle of something; landing a ``Build Results`` transaction inside either
    would tangle the user's undo history with the run's. The run's outcome is
    kept and delivered on a later tick instead.
    """
    try:
        import FreeCADGui
        if FreeCADGui.Control.activeDialog():
            return True
    except Exception:
        pass
    try:
        from PySide import QtWidgets
    except ImportError:
        from PySide import QtGui as QtWidgets
    return QtWidgets.QApplication.activeModalWidget() is not None


def _console_outcome(workdir, process):
    """``(outcome, summary, error)`` once the console run is over, else None.

    *outcome* is ``'ok'``, ``'failed'`` or ``'cancelled'``. ``exit.json`` says
    the runner ended by itself; a console that is gone without one was closed.
    """
    info = _read_exit(workdir)
    if info is not None:
        summary = _read_summary(workdir)
        if info.get("returncode") == 0 and summary and summary.get("ok", False):
            return "ok", summary, None
        error = ((summary or {}).get("error") or info.get("error")
                 or "the solver exited with code {} -- see its console "
                    "window".format(info.get("returncode")))
        return "failed", None, error
    if process.poll() is not None:
        return "cancelled", None, None
    return None


def start_console_run(workdir, on_done, parent=None, title="Wavesim",
                      percent=False):
    """Start the job in *workdir* in its own console window and return at once.

    The non-blocking sibling of :func:`run_job`: ``console_runner.py`` runs
    ``runner.py`` in a new console that shows the status lines and a progress
    bar, and FreeCAD stays usable meanwhile. Closing that window cancels the
    run, as the dialog's Cancel button does. A timer polls for the end; on
    success *on_done(summary)* is called on the GUI thread (failure and cancel
    are reported here, as :func:`run_job` reports them). Delivery waits while a
    task panel or modal dialog is open -- see :func:`_gui_busy`.

    *percent* is :func:`run_job`'s: the PROGRESS units are only a bar scale.
    Returns ``True`` if the run started.
    """
    import subprocess

    from PySide import QtCore
    try:
        from PySide import QtWidgets
    except ImportError:
        from PySide import QtGui as QtWidgets

    key = _run_key(workdir)
    if key in _CONSOLE_RUNS:
        QtWidgets.QMessageBox.warning(
            parent, "Wavesim Run",
            "A run is already going in\n{}\n\nClose its console window to "
            "cancel it, or wait for it to finish.".format(workdir))
        return False

    python_exe = wavesim_settings.get_wavesim_python()
    for path, what in ((python_exe, "Solver Python interpreter"),
                       (RUNNER_PATH, "runner.py"),
                       (CONSOLE_RUNNER_PATH, "console_runner.py")):
        if not os.path.isfile(path):
            QtWidgets.QMessageBox.critical(
                parent, "Wavesim Run",
                "{} not found:\n{}\n\nSet the interpreter in Wavesim -> "
                "Settings.".format(what, path))
            return False

    _discard_exit(workdir)
    cmd = [python_exe, CONSOLE_RUNNER_PATH, "--runner", RUNNER_PATH,
           "--workdir", workdir, "--title", title]
    if percent:
        cmd.append("--percent")

    # Breakaway first: if FreeCAD itself sits in a kill-on-close job (started
    # from an IDE or a terminal), the run should not die with it. Not every job
    # allows it, hence the plain retry.
    process = None
    error = None
    for flags in (_CREATE_NEW_CONSOLE | _CREATE_BREAKAWAY_FROM_JOB,
                  _CREATE_NEW_CONSOLE):
        try:
            process = subprocess.Popen(cmd, cwd=workdir, creationflags=flags)
            break
        except OSError as exc:
            error = exc
    if process is None:
        QtWidgets.QMessageBox.critical(
            parent, "Wavesim Run",
            "Failed to start the solver console:\n{}\n\n{}".format(
                subprocess.list2cmdline(cmd), error))
        return False

    timer = QtCore.QTimer()
    timer.setInterval(_POLL_MS)
    run = {"process": process, "timer": timer, "outcome": None}

    def poll():
        if run["outcome"] is None:
            outcome = _console_outcome(workdir, process)
            if outcome is None:
                return
            run["outcome"] = outcome
            _discard_exit(workdir)
        if _gui_busy():
            return
        timer.stop()
        _CONSOLE_RUNS.pop(key, None)
        status, summary, error = run["outcome"]
        if status == "cancelled":
            FreeCAD.Console.PrintWarning("Wavesim: run cancelled.\n")
            return
        if status == "failed":
            FreeCAD.Console.PrintError("Wavesim: run failed: {}\n".format(error))
            QtWidgets.QMessageBox.critical(
                parent, "Wavesim Run",
                "The solver run failed:\n\n{}".format(error))
            return
        _report_complete(summary, workdir)
        try:
            on_done(summary)
        except Exception as exc:
            import traceback
            FreeCAD.Console.PrintError(
                "Wavesim: could not load the results: {}\n{}".format(
                    exc, traceback.format_exc()))

    timer.timeout.connect(poll)
    _CONSOLE_RUNS[key] = run
    timer.start()
    FreeCAD.Console.PrintMessage(
        "Wavesim: the solver is running in its own console window; FreeCAD "
        "stays usable. Close that window to cancel the run.\n")
    return True
