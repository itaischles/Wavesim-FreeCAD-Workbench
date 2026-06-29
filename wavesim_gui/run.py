# -*- coding: utf-8 -*-
"""Run driver for the Wavesim workbench bridge (FreeCAD side).

Spawns the conda-side ``runner.py`` on a serialised job directory with
``QProcess``, shows a ``QProgressDialog`` driven by the ``PROGRESS n/N`` lines
the runner prints to stdout, and lets the user cancel by killing the process.
On success it loads ``summary.json`` and returns it to the caller.

Kept separate from :mod:`wavesim_gui.commands` so the QProcess plumbing can be
reused (and tested) independently of the command wiring.
"""

import json
import os

import FreeCAD

import wavesim_settings


# Path to the conda-executed runner at the workbench root. FreeCAD ``exec``s the
# init files without a stable ``__file__``, so derive it from the app-data dir.
_WB_DIR = os.path.join(
    FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench"
)
RUNNER_PATH = os.path.join(_WB_DIR, "runner.py")


def _read_summary(workdir):
    """Return the parsed ``summary.json`` from *workdir*, or ``None``."""
    path = os.path.join(workdir, "summary.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def voxelization_progress(parent=None, title="Wavesim",
                          message="Voxelizing geometry..."):
    """Return ``(dialog, callback)`` for showing voxelisation progress.

    The voxeliser's ``isInside`` sweep runs on the GUI thread, so without
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
            busy=False):
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

    # Accumulated stderr, surfaced if the run fails.
    state = {"stderr": "", "stdout_tail": "", "cancelled": False}

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
                progress.setLabelText(text)
            elif line.startswith("PROGRESS ") and not busy:
                try:
                    done = int(line.split()[1].split("/")[0])
                    progress.setValue(min(done, n_steps))
                except (ValueError, IndexError):
                    pass

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

    FreeCAD.Console.PrintMessage(
        "Wavesim: run complete in {:.2f}s ({} steps, dt={:.3e}s). "
        "Output in {}\n".format(
            summary.get("wall_time_s", 0.0), summary.get("steps", "?"),
            summary.get("dt", float("nan")), workdir,
        )
    )
    return summary
