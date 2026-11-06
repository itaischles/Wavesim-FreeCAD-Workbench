# -*- coding: utf-8 -*-
"""Run one ``runner.py`` job in its own console window, with a live status view.

A **standalone script**, executed by the *solver* Python (the conda
interpreter), never by FreeCAD's. Stdlib-only, so it cannot break on whatever
the solver environment does or does not have::

    <wavesim_python> console_runner.py --runner <runner.py> --workdir <run dir>
                                       [--title TEXT] [--percent]

The FreeCAD side starts it with ``CREATE_NEW_CONSOLE`` and returns at once, so
FreeCAD stays usable while the solver runs (``wavesim_gui.run.start_console_run``).
This script is what the user watches:

* The runner's ``STATUS`` lines are printed as time-stamped lines, and its
  ``PROGRESS n/N`` lines drive a one-line progress bar with the elapsed time,
  the time remaining and the step rate -- the text the progress dialog shows,
  laid out for a console. Anything else the runner prints (warnings, a
  traceback) is passed through as is, above the bar.
* The runner is placed in a Windows **Job Object** created with
  ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, and this script holds the only handle
  to it. Whatever ends this script -- the user closing the window, Ctrl+C, a
  crash -- closes that handle, and the OS then kills the solver too. Closing the
  window is therefore the Cancel button. The runner is started suspended and
  resumed only once it is inside the job, so none of it runs outside.
* ``exit.json`` is written (atomically) **only when the runner exits by
  itself**, so FreeCAD reads "the run is over" from its presence and
  "cancelled" from a dead console with no ``exit.json``. FreeCAD deletes it once
  read, so a finished run folder holds exactly what it always did.

The Job Object handling follows the AI agent's own console wrapper
(``Wavesim-AI-Agent/wavesim_agent_mcp/console_wrapper.py``), which is where it was
proven out.
"""

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes

CREATE_SUSPENDED = 0x00000004
# The runner gets a hidden console of its own (its output is piped here anyway).
# Sharing this window's, it would receive the close event too and die on its own
# -- before this script's handler runs, so the run would read as finished-and-
# failed rather than cancelled.
CREATE_NO_WINDOW = 0x08000000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

CTRL_EVENT_NAMES = {0: "Ctrl+C", 1: "Ctrl+Break", 2: "window closed",
                    5: "logoff", 6: "shutdown"}

# Shortest interval between two redraws of the bar. The runner prints ~100
# PROGRESS lines a run, but an electrostatic solve can print many more.
_REDRAW_S = 0.1


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")

kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateJobObject.restype = wintypes.BOOL
kernel32.SetConsoleTitleW.argtypes = [wintypes.LPCWSTR]
kernel32.SetConsoleTitleW.restype = wintypes.BOOL
kernel32.GetConsoleProcessList.argtypes = [
    ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
kernel32.GetConsoleProcessList.restype = wintypes.DWORD
ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
ntdll.NtResumeProcess.restype = ctypes.c_long

HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
kernel32.SetConsoleCtrlHandler.argtypes = [HANDLER_ROUTINE, wintypes.BOOL]
kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL


def _win_error(what):
    err = ctypes.get_last_error()
    return OSError("{} failed: {}".format(what, ctypes.FormatError(err).strip()))


def _console_attached():
    pids = (wintypes.DWORD * 16)()
    return kernel32.GetConsoleProcessList(pids, 16) > 0


def create_kill_on_close_job():
    """A fresh job handle that kills every process in it when it is closed."""
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise _win_error("CreateJobObjectW")
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
            job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info), ctypes.sizeof(info)):
        raise _win_error("SetInformationJobObject")
    return job


def _write_json_atomic(path, data):
    tmp = "{}.{}.tmp".format(path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _format_duration(seconds):
    """``4.2s`` / ``3m 05s`` / ``1h 03m 05s`` -- as the progress dialog writes it."""
    seconds = max(0.0, float(seconds))
    if seconds < 60.0:
        return "{:.1f}s".format(seconds)
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "{}h {:02d}m {:02d}s".format(hours, minutes, secs)
    return "{}m {:02d}s".format(minutes, secs)


def _wait_key():
    """Block until a key is pressed in this console (no-op without one)."""
    try:
        import msvcrt
    except ImportError:
        return
    while True:
        if msvcrt.kbhit():
            msvcrt.getwch()
            return
        time.sleep(0.1)


class ConsoleView:
    """The status lines plus a progress bar that redraws in place below them.

    The bar is always the last line on screen: any other line clears it, is
    printed, and the bar is drawn again under it. Shared by the reader loop and
    the control handler thread, hence the lock.
    """

    def __init__(self, title, percent=False):
        self._title = title or "Wavesim"
        self._percent = percent
        self._lock = threading.Lock()
        self._bar = ""            # the bar text currently on screen
        self._done = 0
        self._total = 0
        self._t0 = None           # anchored on the first PROGRESS line,
        self._done0 = 0           # as in the dialog, so setup time is not
        self._last_draw = 0.0     # counted against the step rate
        self._set_title("")

    def _set_title(self, suffix):
        text = self._title + (" - " + suffix if suffix else "")
        kernel32.SetConsoleTitleW(text)

    def _width(self):
        return max(40, shutil.get_terminal_size((100, 25)).columns - 1)

    def _write(self, text):
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except Exception:
            pass

    def _clear_bar(self):
        if self._bar:
            self._write("\r" + " " * len(self._bar) + "\r")
            self._bar = ""

    def _compose_bar(self):
        total = max(1, self._total)
        done = max(0, min(self._done, total))
        frac = done / total
        parts = ["{:5.1f}%".format(100.0 * frac)]
        if not self._percent:
            parts.append("step {:,}/{:,}".format(done, total))
        elapsed = time.perf_counter() - self._t0
        stepped = done - self._done0
        if stepped > 0 and elapsed > 0.0:
            rate = stepped / elapsed
            parts.append("{} elapsed".format(_format_duration(elapsed)))
            parts.append("~{} left".format(
                _format_duration((total - done) / rate)))
            if not self._percent:
                parts.append("{:,.0f} steps/s".format(rate))
        else:
            parts.append("{} elapsed".format(_format_duration(elapsed)))
        text = "  ".join(parts)
        width = self._width()
        cells = max(10, min(40, width - len(text) - 4))
        filled = int(round(cells * frac))
        line = "[{}{}]  {}".format("#" * filled, "-" * (cells - filled), text)
        return line[:width], frac

    def _draw_bar(self):
        if self._t0 is None:
            return
        line, frac = self._compose_bar()
        self._write("\r" + line + " " * max(0, len(self._bar) - len(line)))
        self._bar = line
        self._set_title("{:.0f}%".format(100.0 * frac))
        self._last_draw = time.perf_counter()

    def line(self, text):
        """Print *text* on its own line above the bar."""
        with self._lock:
            self._clear_bar()
            self._write(text + "\n")
            self._draw_bar()

    def status(self, text):
        stamp = time.strftime("%H:%M:%S")
        for i, part in enumerate(text.split("\n")):
            self.line("{}  {}".format(stamp if i == 0 else " " * len(stamp),
                                      part))

    def progress(self, done, total):
        with self._lock:
            if self._t0 is None:
                self._t0 = time.perf_counter()
                self._done0 = done
            self._done, self._total = done, total
            final = done >= total
            if final or time.perf_counter() - self._last_draw >= _REDRAW_S:
                self._draw_bar()

    def finish(self, suffix):
        """Leave the last bar on screen and stop drawing it.

        Also called from the control handler's thread, so it gives up on the
        lock rather than wait on a reader that may never let go of it.
        """
        locked = self._lock.acquire(timeout=1.0)
        try:
            if self._bar:
                self._write("\n")
                self._bar = ""
            self._t0 = None
            self._set_title(suffix)
        finally:
            if locked:
                self._lock.release()


def _handle_line(view, raw):
    line = raw.rstrip("\r\n")
    stripped = line.strip()
    if stripped.startswith("STATUS "):
        view.status(stripped[len("STATUS "):].replace("\\n", "\n"))
        return
    if stripped.startswith("PROGRESS "):
        try:
            done, total = stripped.split()[1].split("/")
            view.progress(int(done), int(total))
            return
        except (ValueError, IndexError):
            pass
    if stripped:
        view.line(line)


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runner", required=True, help="path to the workbench runner.py")
    ap.add_argument("--workdir", required=True, help="run folder holding job.json")
    ap.add_argument("--title", default="Wavesim", help="console window title")
    ap.add_argument("--percent", action="store_true",
                    help="PROGRESS units are only a bar scale (electrostatics): "
                         "show a percentage, not a step count")
    ap.add_argument("--no-wait", action="store_true",
                    help="close straight away after a failure (tests)")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    workdir = os.path.abspath(args.workdir)
    runner = os.path.abspath(args.runner)
    exit_path = os.path.join(workdir, "exit.json")
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    view = ConsoleView(args.title, percent=args.percent)
    state = {"job": None, "finished": False, "cancelling": False}
    # Ending the job closes the runner's stdout, which sends the main thread on
    # to write exit.json -- and an exit.json saying "failed" is exactly what a
    # cancel must not leave behind. The two sides take this lock: a cancel that
    # wins means no exit.json at all, and a write that wins means the run was
    # already over and the close is just closing the window.
    exit_lock = threading.Lock()

    def abort(reason):
        exit_lock.acquire(timeout=2.0)
        if state["finished"]:
            os._exit(0)
        state["cancelling"] = True
        # The job would take the runner down with this process anyway; ending it
        # here first just makes the stop immediate.
        if state["job"]:
            kernel32.TerminateJobObject(state["job"], 1)
        try:
            view.finish("cancelled ({})".format(reason))
        except Exception:
            pass
        os._exit(1)

    # Runs on a system thread. Closing the window leaves ~5 s before the OS
    # kills this process; that is plenty to end the job.
    def on_ctrl(event):
        abort(CTRL_EVENT_NAMES.get(event, "console event {}".format(event)))
        return True

    ctrl_handler = HANDLER_ROUTINE(on_ctrl)
    kernel32.SetConsoleCtrlHandler(ctrl_handler, True)

    # If the console host itself vanishes (Windows Terminal crashed, conhost
    # killed) no control event arrives and the solver would run on headless.
    def console_watchdog():
        while True:
            time.sleep(1.0)
            if not _console_attached():
                abort("console lost")

    if _console_attached():
        threading.Thread(target=console_watchdog, daemon=True).start()

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, "-u", runner, workdir]

    view.line("Wavesim solver run")
    view.line("  folder: {}".format(workdir))
    view.line("  Closing this window cancels the run.")
    view.line("")
    t0 = time.monotonic()
    try:
        job = create_kill_on_close_job()
        state["job"] = job
        proc = subprocess.Popen(cmd, cwd=workdir, env=env,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                creationflags=CREATE_SUSPENDED | CREATE_NO_WINDOW)
        handle = int(proc._handle)
        if not kernel32.AssignProcessToJobObject(job, handle):
            err = _win_error("AssignProcessToJobObject")
            proc.kill()
            raise err
        status = ntdll.NtResumeProcess(handle)
        if status != 0:
            proc.kill()
            raise OSError("NtResumeProcess failed (NTSTATUS 0x{:08X})".format(
                status & 0xFFFFFFFF))
    except Exception as exc:
        view.line("Could not start the solver: {}: {}".format(
            type(exc).__name__, exc))
        _write_json_atomic(exit_path, {
            "returncode": -1,
            "error": "could not start the solver: {}".format(exc),
            "wall_s": round(time.monotonic() - t0, 3)})
        state["finished"] = True
        if not args.no_wait:
            view.line("Press any key to close this window.")
            _wait_key()
        return 1

    for raw in proc.stdout:
        _handle_line(view, raw.decode("utf-8", "replace"))
    returncode = proc.wait()
    wall = time.monotonic() - t0

    with exit_lock:
        if state["cancelling"]:
            # The control thread is ending this process; let it.
            while True:
                time.sleep(1.0)
        _write_json_atomic(exit_path, {"returncode": returncode,
                                       "wall_s": round(wall, 3)})
        state["finished"] = True
    if returncode == 0:
        view.finish("done")
        view.line("Finished in {}. The results are loading in FreeCAD.".format(
            _format_duration(wall)))
        return 0
    view.finish("failed")
    view.line("")
    view.line("The solver failed (exit code {}) after {}.".format(
        returncode, _format_duration(wall)))
    if not args.no_wait:
        view.line("Press any key to close this window.")
        _wait_key()
    return 1


if __name__ == "__main__":
    sys.exit(main())
