# -*- coding: utf-8 -*-
"""Parallel OCC sectioning across FreeCAD subprocesses.

Why this exists
---------------
Profiling the voxeliser on the reference coax (26x26x101, conformal PEC on)
puts **81% of the wall time in ``Shape.slice``** -- 15.6 s of 19.3 s, over 2329
section planes -- against 10% in the scanline point-in-polygon and 3% in
``Wire.discretize``. Nothing about that is numpy, so a JIT (numba and friends)
has nothing to compile; and OCC holds the GIL, so threads buy nothing either.
Processes buy all of it: the sections of different planes are independent.

What makes it safe
------------------
The workbench promises its arrays are reproducible, so a parallel section must
be the *same* section, not a near one:

* the shape crosses the process boundary as a **binary BRep** (``exportBinary``
  / ``importBinary``), which round-trips bit-identically -- verified by
  sectioning both copies at the conformal sampler's own chord tolerance and
  comparing discretized vertices exactly. (``Part.Shape`` cannot be pickled, so
  BRep is not merely the fast option, it is the only one.)
* the worker calls :func:`wavesim_gui.voxelize._section_polygons`, the same
  function the serial path calls, so the closed-wire rule and the
  degenerate-plane nudge retry cannot drift between the two paths.
* results are reassembled **by request index**, never by completion order, so
  the arrays do not depend on how the work happened to be scheduled.
* anything the pool cannot do -- it fails to start, a worker dies, one plane
  raises -- falls back to sectioning that plane in-process. The pool is an
  accelerator, never a second source of truth.

``tools/check_sectionpool.py`` is the gate: it voxelises a document with the
pool on and off and asserts every array is bit-identical.

Turning it off
--------------
``voxelize_workers`` in Wavesim -> Settings (or ``WAVESIM_VOXELIZE_WORKERS``):
``0`` or ``1`` restores the fully serial path, which is what shipped before.
"""

import os
import pickle
import queue
import struct
import subprocess
import tempfile
import threading
import time

import FreeCAD

# Below this many planes a batch is never worth dispatching whatever it costs.
_MIN_BATCH = 48

# Starting the workers means N processes each doing ``import FreeCAD``, which is
# seconds, not milliseconds -- easily more than a whole small voxelisation takes.
# Left ungoverned that is a straight regression: a 0.76 s dielectric slab became
# 2.75 s and a 6.2 s sphere pair 8.4 s, entirely in start-up. So a batch is
# dispatched only when the time it is predicted to *save* beats that cost by a
# margin. The margin is asymmetric on purpose -- a sweep wrongly kept serial
# loses a fraction of itself, one wrongly dispatched loses the whole start-up --
# so the bar sits well above the ~1.8 s actually measured for eleven workers. At
# 2.0 x 1.5 a 3.2 s SPICE coax still dispatched and came back 4.3 s.
_STARTUP_ESTIMATE_S = 2.5     # deliberately above the ~1.8 s measured
_DISPATCH_MARGIN = 2.0        # predicted saving must beat start-up by this
_BATCH_MIN_S = 0.15           # once running, the per-batch round trip floor

# Upper bound on workers however many cores are present. Past this the OCC
# sections are no longer the constraint and the parent's own reassembly is.
_MAX_WORKERS = 16

# How long to wait on a reader thread once its worker should have finished.
_JOIN_TIMEOUT_S = 60.0

# Main-thread poll interval while the pool works. Small enough that the progress
# dialog stays responsive, large enough not to spin.
_POLL_S = 0.02


class SectionPoolCancelled(Exception):
    """The caller's progress callback asked to stop mid-batch."""


def resolve_workers(setting, cpu_count=None):
    """Worker count from the ``voxelize_workers`` setting.

    ``'auto'`` (or empty/unparseable) leaves one core for the rest of the
    machine and caps at :data:`_MAX_WORKERS`. ``0``/``1`` mean "serial" and are
    returned as-is, which is what disables the pool.
    """
    cpus = cpu_count or os.cpu_count() or 2
    text = str(setting or "auto").strip().lower()
    if text and text != "auto":
        try:
            return max(0, int(text))
        except ValueError:
            pass
    return max(1, min(cpus - 1, _MAX_WORKERS))


def _worker_command(workers_script):
    """``bin/python.exe`` + the worker script.

    Deliberately *not* ``sys.executable``: inside the FreeCAD GUI that is
    ``FreeCAD.exe``, and spawning it would start whole new GUI instances.
    """
    home = FreeCAD.getHomePath()
    exe = os.path.join(home, "bin", "python.exe" if os.name == "nt" else "python")
    if not os.path.exists(exe):
        alt = os.path.join(home, "bin", "python3")
        exe = alt if os.path.exists(alt) else None
    return None if exe is None else [exe, "-E", "-s", workers_script]


class SectionPool(object):
    """A set of FreeCAD subprocesses that cut section planes on request.

    Start-up is **lazy**: the processes are spawned on the first batch that has
    been *shown* to pay for them, so a small model never spawns one at all.

    The pool never commits on a guess. Section cost tracks topological
    complexity rather than grid size, and the obvious cheap proxy for it -- face
    count -- was tried and is wrong in both directions: a sphere is one face and
    not remotely cheap (it talked the pool out of a 6 s sweep), while a filleted
    resistor is many faces and sections fast (it talked the pool into a 3.2 s
    sweep that came back 5.0 s). So the first batch of every run is cut serially
    and timed by the caller (:meth:`observe`), and only that measurement decides.
    The rule costs one pass at serial speed and cannot mis-fire on geometry
    nobody has profiled.

    Use as a context manager, or call :meth:`close`.
    """

    def __init__(self, workers):
        self.workers = int(workers)
        self._procs = []
        self._tmpdir = None
        self._shapes = {}          # id(shape) -> (token, shape ref, brep path)
        self._started = False
        self._broken = False
        self._plane_cost_s = None  # measured from batches cut serially
        self._planned_planes = 0   # the whole sweep's plane count (see plan())
        self._seen_planes = 0      # ...and how many of them are already done

    # ---------------------------------------------------------------- setup #

    @property
    def enabled(self):
        return self.workers > 1 and not self._broken

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def plan(self, planes):
        """Tell the pool how many section planes the whole sweep will cut.

        Judged on its own size a batch can only ever start the pool late: a long
        voxelisation often opens with a small pass (on the reference coax, each
        body's node-plane pass) and would cut several of those serially before
        committing. The caller counts the total up front anyway, for the progress
        bar, and that total times the *measured* per-plane cost is what says
        whether this run as a whole is worth the workers.
        """
        self._planned_planes = int(planes)

    def worth_dispatching(self, n_planes):
        """Whether *n_planes* repay what dispatching them costs.

        Before the workers exist that cost is their start-up -- seconds, and
        wasted outright on a model that voxelises in under one -- so the run's
        predicted saving has to beat it by :data:`_DISPATCH_MARGIN`. Once they
        are up the cost is only the round trip, and the bar drops accordingly.

        Returns ``False`` until a batch has been timed, which is what makes the
        first pass of every run a measurement rather than a bet.
        """
        if n_planes < _MIN_BATCH:
            return False
        cost = self._plane_cost_s
        if cost is None:
            return False
        serial_s = n_planes * cost
        if self._started:
            return serial_s >= _BATCH_MIN_S
        # What the run has *left*, not what it started with: a sweep should
        # commit on its first sizeable batch rather than its largest, but by the
        # time three of four bodies are measured there may be nothing left worth
        # starting for. Judging the tail against the whole made a 3.1 s model
        # spawn workers for its last 56 planes and take 5.3 s.
        remaining = max(0, self._planned_planes - self._seen_planes)
        planned_s = max(serial_s, remaining * cost)
        # Perfect scaling is not claimed: the saving is what stays serial
        # subtracted, which for W workers is a (1 - 1/W) fraction.
        saving = planned_s * (1.0 - 1.0 / max(self.workers, 2))
        return (saving >= _STARTUP_ESTIMATE_S * _DISPATCH_MARGIN
                and serial_s >= _BATCH_MIN_S)

    def observe(self, n_planes, seconds):
        """Record what *n_planes* actually cost when cut serially.

        The samplers time their own fallback and report it here, so the first
        batches of a run measure the geometry instead of guessing at it -- face
        count is a poor proxy for section cost, and this replaces it as soon as
        there is anything better.
        """
        if n_planes <= 0 or seconds <= 0.0:
            return
        self._plane_cost_s = seconds / float(n_planes)
        self._seen_planes += n_planes

    def _start(self):
        if self._started or self._broken:
            return self._started
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "_section_worker.py")
        command = _worker_command(script)
        if command is None:
            self._broken = True
            return False

        # No console windows, and stderr discarded: the protocol lives on the
        # worker's private dup of fd 1 (see _section_worker), but a blocked
        # stderr pipe nobody drains would deadlock it.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._tmpdir = tempfile.mkdtemp(prefix="wavesim_sect_")
        try:
            for _ in range(self.workers):
                self._procs.append(subprocess.Popen(
                    command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, creationflags=flags,
                ))
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Wavesim: could not start section workers ({}); "
                "voxelising serially.\n".format(exc))
            self._broken = True
            self.close()
            return False
        self._started = True
        return True

    # ------------------------------------------------------------- protocol #

    @staticmethod
    def _send(proc, obj):
        payload = pickle.dumps(obj, protocol=4)
        proc.stdin.write(struct.pack("<I", len(payload)))
        proc.stdin.write(payload)
        proc.stdin.flush()

    @staticmethod
    def _recv(proc):
        head = proc.stdout.read(4)
        if not head or len(head) < 4:
            raise IOError("section worker closed its pipe")
        (size,) = struct.unpack("<I", head)
        body = b""
        while len(body) < size:
            chunk = proc.stdout.read(size - len(body))
            if not chunk:
                raise IOError("section worker truncated a reply")
            body += chunk
        return pickle.loads(body)

    def _upload(self, shape):
        """Export *shape* once and have every worker load it. Returns a token."""
        key = id(shape)
        cached = self._shapes.get(key)
        if cached is not None:
            return cached[0]
        token = len(self._shapes)
        path = os.path.join(self._tmpdir, "shape_{}.bin".format(token))
        shape.exportBinary(path)
        for proc in self._procs:
            self._send(proc, ("load", token, path))
        for proc in self._procs:
            reply = self._recv(proc)
            if reply[0] != "ok":
                raise IOError("section worker refused a shape")
        # The shape reference is kept so its id() cannot be recycled onto a
        # different shape while this pool is alive.
        self._shapes[key] = (token, shape, path)
        return token

    # -------------------------------------------------------------- the map #

    def sections(self, shape, requests, on_progress=None):
        """Section *shape* at every ``(z, deflection, nudge)`` in *requests*.

        Returns a list aligned with *requests* (each entry the polygon list
        ``_section_polygons`` gave, or ``None`` for a plane that misses), or
        ``None`` when the pool declined -- too small a batch, not started,
        broken -- and the caller should do it serially.

        *on_progress* is called once per completed plane **on the calling
        thread** (Qt work must not happen on the reader threads); a truthy
        return raises :class:`SectionPoolCancelled`.
        """
        if not self.enabled or not self.worth_dispatching(len(requests)):
            return None
        if not self._start():
            return None
        try:
            token = self._upload(shape)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Wavesim: section workers unusable ({}); voxelising "
                "serially.\n".format(exc))
            self._broken = True
            return None

        self._seen_planes += len(requests)
        pending = queue.Queue()
        for idx in range(len(requests)):
            pending.put(idx)
        results = [None] * len(requests)
        done = [0]
        lock = threading.Lock()
        stop = threading.Event()
        failed = []

        def reader(proc):
            while not stop.is_set():
                try:
                    idx = pending.get_nowait()
                except queue.Empty:
                    return
                z, deflection, nudge = requests[idx]
                try:
                    self._send(proc, ("sec", idx, token, float(z),
                                      float(deflection), float(nudge)))
                    reply = self._recv(proc)
                except Exception as exc:
                    failed.append(exc)
                    stop.set()
                    return
                with lock:
                    results[reply[1]] = reply[2]
                    done[0] += 1

        threads = [threading.Thread(target=reader, args=(proc,), daemon=True)
                   for proc in self._procs]
        for thread in threads:
            thread.start()

        # Main thread: tick progress for each finished plane and watch for a
        # cancel. Never touches `results`, so no lock is needed for the count.
        ticked = 0
        try:
            while True:
                with lock:
                    finished = done[0]
                while ticked < finished:
                    ticked += 1
                    if on_progress is not None and on_progress():
                        stop.set()
                        raise SectionPoolCancelled()
                if finished >= len(requests) or (stop.is_set() and not failed):
                    break
                if failed:
                    break
                if not any(thread.is_alive() for thread in threads):
                    break
                time.sleep(_POLL_S)
        finally:
            if stop.is_set():
                # A cancelled batch leaves workers mid-section; they cannot be
                # interrupted, so the pool is retired rather than reused.
                self._broken = True
                for thread in threads:
                    thread.join(timeout=0.05)
                self.close()

        for thread in threads:
            thread.join(timeout=_JOIN_TIMEOUT_S)

        if failed:
            FreeCAD.Console.PrintWarning(
                "Wavesim: a section worker failed ({}); the remaining planes "
                "are cut in-process.\n".format(failed[0]))
            self._broken = True
            self.close()
        return results

    # ------------------------------------------------------------- teardown #

    def close(self):
        for proc in self._procs:
            try:
                self._send(proc, ("bye",))
            except Exception:
                pass
        for proc in self._procs:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._procs = []
        self._started = False
        self._shapes = {}
        if self._tmpdir:
            for name in os.listdir(self._tmpdir) if os.path.isdir(self._tmpdir) else []:
                try:
                    os.remove(os.path.join(self._tmpdir, name))
                except Exception:
                    pass
            try:
                os.rmdir(self._tmpdir)
            except Exception:
                pass
            self._tmpdir = None
