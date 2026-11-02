# -*- coding: utf-8 -*-
"""Agent bridge: lets an AI agent drive this FreeCAD session over XML-RPC.

The MCP server in the ``wavesim-ai-harness`` repository talks to FreeCAD
through this listener. Security and safety rules, all enforced here:

* bound to **127.0.0.1 only**; every call carries the **token** stored in the
  workbench settings (compared in constant time);
* **whitelist dispatch only** -- a call names one function registered with
  :func:`wavesim_gui.api_common.api_function` (the scripting API plus the
  document operations of :mod:`wavesim_gui.api_document`). There is no
  eval/exec, no attribute access, no way to name anything else;
* arguments and results are **JSON** (sent as strings inside XML-RPC, which
  avoids XML-RPC's int/None limits); FreeCAD objects come back described by
  :func:`wavesim_gui.api_common.describe`;
* FreeCAD is not thread-safe, so every call runs on the **Qt main thread**:
  the RPC thread queues it and a ``QTimer`` drains the queue, one call at a
  time. Long calls (job export / voxelization) report progress through a
  callback that also pumps the Qt event loop, so the GUI stays responsive and
  :meth:`Bridge.status` answers (from the RPC thread) while they run.

Endpoints (all take the token first):

``call(token, name, args_json[, timeout_s])`` -> JSON
    ``{"ok": true, "result": ...}`` or ``{"ok": false, "error": "...",
    "error_type": "ApiError"|"AuthError"|"UsageError"|"Timeout"|"InternalError",
    "traceback_summary": [...]}``.
``bridge_info(token)`` -> JSON: protocol, workbench and API versions.
``status(token)`` -> JSON: busy function, progress, queue length.
``cancel(token)`` -> JSON: asks a running export/voxelization to stop.
``list_functions(token)`` -> JSON: name, category, signature, docstring.
"""

import hmac
import inspect
import json
import queue
import socketserver
import threading
import time
import traceback
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

import FreeCAD

#: Version of the RPC protocol itself (endpoints and envelope).
PROTOCOL_VERSION = 1
DEFAULT_TIMEOUT_S = 900.0
_PUMP_INTERVAL_MS = 30
_PROCESS_EVENTS_EVERY_S = 0.05


class _Handler(SimpleXMLRPCRequestHandler):
    rpc_paths = ("/RPC2",)

    def log_message(self, fmt, *args):  # keep the report view quiet
        pass


class _Server(socketserver.ThreadingMixIn, SimpleXMLRPCServer):
    daemon_threads = True
    allow_reuse_address = False


class _Job(object):
    __slots__ = ("name", "func", "kwargs", "done", "result", "abandoned")

    def __init__(self, name, func, kwargs):
        self.name = name
        self.func = func
        self.kwargs = kwargs
        self.done = threading.Event()
        self.result = None
        self.abandoned = False


def _summarize_traceback(exc):
    frames = traceback.extract_tb(exc.__traceback__)[-6:]
    return ["{}:{} in {}".format(f.filename.replace("\\", "/").split("/")[-1],
                                 f.lineno, f.name) for f in frames]


class Bridge(object):
    """The XML-RPC listener plus its main-thread executor.

    *executor* ``"qt"`` (the GUI) runs calls on the Qt main thread;
    ``"direct"`` runs them in the RPC thread under a lock -- for headless
    tests only, where nothing else touches FreeCAD meanwhile.
    """

    def __init__(self, port, token, host="127.0.0.1", executor="qt"):
        if host != "127.0.0.1":
            raise ValueError("The Agent bridge only binds 127.0.0.1.")
        if not token:
            raise ValueError("The Agent bridge needs a token.")
        self.host = host
        self.port = int(port)
        self._token = str(token)
        self._executor = executor
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self._timer = None
        self._busy = None
        self._cancel = threading.Event()
        self._progress = None
        self._last_pump = 0.0
        self._started = None
        self._calls = 0
        self._last_error = None

    # -- lifecycle ---------------------------------------------------- #

    def start(self):
        self._server = _Server((self.host, self.port), requestHandler=_Handler,
                               allow_none=True, logRequests=False)
        for name in ("call", "bridge_info", "status", "cancel",
                     "list_functions"):
            self._server.register_function(getattr(self, "_rpc_" + name),
                                           name)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="WavesimAgentBridge",
                                        daemon=True)
        self._thread.start()
        if self._executor == "qt":
            from PySide import QtCore
            self._timer = QtCore.QTimer()
            self._timer.setInterval(_PUMP_INTERVAL_MS)
            self._timer.timeout.connect(self._pump)
            self._timer.start()
        self._started = time.time()

    def stop(self):
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        while not self._queue.empty():
            job = self._queue.get_nowait()
            job.result = self._error("Bridge stopped before the call ran.",
                                     "InternalError")
            job.done.set()

    @property
    def running(self):
        return self._server is not None

    # -- RPC endpoints (run in the server's threads) ------------------ #

    def _authorized(self, token):
        return isinstance(token, str) and hmac.compare_digest(
            token.encode("utf-8"), self._token.encode("utf-8"))

    @staticmethod
    def _error(message, kind, tb=None):
        out = {"ok": False, "error": message, "error_type": kind}
        if tb:
            out["traceback_summary"] = tb
        return out

    def _rpc_call(self, token, name, args_json="{}", timeout_s=None):
        if not self._authorized(token):
            return json.dumps(self._error("Invalid bridge token.",
                                          "AuthError"))
        return json.dumps(self.call(name, args_json, timeout_s))

    def _rpc_bridge_info(self, token):
        if not self._authorized(token):
            return json.dumps(self._error("Invalid bridge token.",
                                          "AuthError"))
        return json.dumps({"ok": True, "result": self.info()})

    def _rpc_status(self, token):
        if not self._authorized(token):
            return json.dumps(self._error("Invalid bridge token.",
                                          "AuthError"))
        return json.dumps({"ok": True, "result": self.status()})

    def _rpc_cancel(self, token):
        if not self._authorized(token):
            return json.dumps(self._error("Invalid bridge token.",
                                          "AuthError"))
        self._cancel.set()
        return json.dumps({"ok": True, "result": {"cancel_requested": True,
                                                  "busy": self._busy}})

    def _rpc_list_functions(self, token):
        if not self._authorized(token):
            return json.dumps(self._error("Invalid bridge token.",
                                          "AuthError"))
        return json.dumps({"ok": True, "result": list_functions()})

    # -- dispatch ----------------------------------------------------- #

    def info(self):
        import wavesim_gui
        from wavesim_gui import api_common
        version = FreeCAD.Version()
        return {
            "protocol": PROTOCOL_VERSION,
            "workbench_version": getattr(wavesim_gui, "__version__", "?"),
            "api_version": api_common.API_VERSION,
            "freecad_version": ".".join(version[:3]),
            "gui": bool(getattr(FreeCAD, "GuiUp", False)),
            "port": self.port,
            "functions": len(_registry()),
        }

    def status(self):
        progress = dict(self._progress) if self._progress else None
        if progress and progress.get("total"):
            progress["percent"] = round(
                100.0 * progress["done"] / max(progress["total"], 1), 1)
        return {"busy": self._busy, "progress": progress,
                "queued": self._queue.qsize(),
                "uptime_s": round(time.time() - (self._started or time.time()),
                                  1),
                "calls": self._calls, "last_error": self._last_error}

    def call(self, name, args_json="{}", timeout_s=None):
        """Validate and run one whitelisted call; returns the envelope."""
        registry = _registry()
        if not isinstance(name, str) or name not in registry:
            return self._error(
                "Unknown function {!r}. Only registered API functions can be "
                "called (see list_functions).".format(name), "UsageError")
        try:
            kwargs = json.loads(args_json) if args_json else {}
        except ValueError as exc:
            return self._error("Arguments are not valid JSON: {}".format(exc),
                               "UsageError")
        if not isinstance(kwargs, dict):
            return self._error("Arguments must be a JSON object.",
                               "UsageError")
        func = registry[name][0]
        sig = inspect.signature(func)
        for key in kwargs:
            if key.startswith("_") or key == "progress":
                return self._error("Argument {!r} is not allowed.".format(key),
                                   "UsageError")
        try:
            sig.bind(**kwargs)
        except TypeError as exc:
            return self._error("{} -- signature: {}{}".format(exc, name, sig),
                               "UsageError")
        if "progress" in sig.parameters:
            kwargs["progress"] = self._progress_callback
        job = _Job(name, func, kwargs)
        timeout = float(timeout_s) if timeout_s else DEFAULT_TIMEOUT_S
        if self._executor == "direct":
            with self._lock:
                self._run(job)
        else:
            self._queue.put(job)
            if not job.done.wait(timeout):
                job.abandoned = True
                return self._error(
                    "{} did not finish within {:.0f} s; it keeps running in "
                    "FreeCAD (poll status).".format(name, timeout), "Timeout")
        return job.result

    def _pump(self):
        """QTimer slot on the main thread: run the next queued call."""
        if self._busy is not None:
            return  # re-entered from processEvents inside a running call
        try:
            job = self._queue.get_nowait()
        except queue.Empty:
            return
        self._run(job)

    def _run(self, job):
        self._busy = job.name
        self._cancel.clear()
        self._progress = None
        self._calls += 1
        _status_message("Agent: {} ...".format(job.name))
        try:
            from wavesim_gui import api_common
            result = job.func(**job.kwargs)
            job.result = {"ok": True, "result": _serialize(result)}
        except Exception as exc:  # noqa: BLE001 -- every error is reported
            from wavesim_gui.api_common import ApiError
            if isinstance(exc, ApiError):
                job.result = self._error(str(exc), "ApiError")
            else:
                job.result = self._error(
                    "{}: {}".format(type(exc).__name__, exc), "InternalError",
                    _summarize_traceback(exc))
            self._last_error = {"function": job.name,
                                "error": job.result["error"]}
        finally:
            self._busy = None
            self._progress = None
            _status_message("")
            job.done.set()

    def _progress_callback(self, done, total):
        """Progress hook handed to long API calls (main thread)."""
        self._progress = {"function": self._busy, "done": int(done),
                          "total": int(total), "t": time.time()}
        now = time.time()
        if self._executor == "qt" and now - self._last_pump \
                > _PROCESS_EVENTS_EVERY_S:
            self._last_pump = now
            try:
                from PySide import QtWidgets
                QtWidgets.QApplication.processEvents()
            except Exception:
                pass
        return self._cancel.is_set()


def _registry():
    from wavesim_gui import api  # noqa: F401  (imports every api_* module)
    from wavesim_gui.api_common import registered_functions
    return registered_functions()


def _serialize(result):
    from wavesim_gui import api_common as c
    if hasattr(result, "Name") and hasattr(result, "Document") \
            and hasattr(result, "TypeId"):
        return c.describe(result)
    if isinstance(result, list) and result and all(
            hasattr(r, "TypeId") and hasattr(r, "Document") for r in result):
        return [c.describe(r) for r in result]
    return c.to_jsonable(result)


def list_functions():
    """Name, category, signature and docstring of every callable function."""
    out = []
    for name, (func, category) in sorted(_registry().items()):
        out.append({"name": name, "category": category,
                    "signature": "{}{}".format(name, inspect.signature(func)),
                    "doc": inspect.getdoc(func) or ""})
    return out


def _status_message(text):
    if not getattr(FreeCAD, "GuiUp", False):
        return
    try:
        import FreeCADGui
        bar = FreeCADGui.getMainWindow().statusBar()
        if text:
            bar.showMessage(text)
        else:
            bar.clearMessage()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Singleton control (used by the toggle command and auto-start)
# --------------------------------------------------------------------------- #

_bridge = None
_indicator = None


def is_running():
    return _bridge is not None and _bridge.running


def start_bridge(port=None, token=None, executor="qt"):
    """Start the bridge (no-op if running). Returns the Bridge."""
    global _bridge
    import wavesim_settings
    if is_running():
        return _bridge
    port = int(port or wavesim_settings.get_bridge_port())
    token = token or wavesim_settings.get_bridge_token(create=True)
    bridge = Bridge(port, token, executor=executor)
    try:
        bridge.start()
    except OSError as exc:
        FreeCAD.Console.PrintError(
            "Wavesim: Agent bridge could not listen on 127.0.0.1:{} ({}). "
            "Is another FreeCAD running it? Change the port in Wavesim -> "
            "Settings.\n".format(port, exc))
        raise
    _bridge = bridge
    FreeCAD.Console.PrintMessage(
        "Wavesim: Agent bridge listening on 127.0.0.1:{}.\n".format(port))
    _update_indicator()
    return bridge


def stop_bridge():
    global _bridge
    if _bridge is not None:
        _bridge.stop()
        _bridge = None
        FreeCAD.Console.PrintMessage("Wavesim: Agent bridge stopped.\n")
    _update_indicator()


def _update_indicator():
    """Permanent status-bar label + the toolbar button's checkmark."""
    global _indicator
    if not getattr(FreeCAD, "GuiUp", False):
        return
    try:
        import FreeCADGui
        from PySide import QtWidgets
        window = FreeCADGui.getMainWindow()
        if _indicator is None:
            _indicator = QtWidgets.QLabel()
            window.statusBar().addPermanentWidget(_indicator)
        if is_running():
            _indicator.setText("Agent bridge: ON (127.0.0.1:{})".format(
                _bridge.port))
            _indicator.setStyleSheet("color: #1a7f37; font-weight: bold;")
            _indicator.show()
        else:
            _indicator.hide()
        for action in window.findChildren(QtWidgets.QAction):
            if action.objectName() == "Wavesim_AgentBridge" \
                    and action.isCheckable():
                blocked = action.blockSignals(True)
                action.setChecked(is_running())
                action.blockSignals(blocked)
    except Exception:
        pass


def autostart():
    """Called once from InitGui after the GUI is up."""
    import wavesim_settings
    if wavesim_settings.get_bridge_autostart() and not is_running():
        try:
            start_bridge()
        except Exception:
            pass


try:
    import FreeCADGui as Gui
    _GUI_AVAILABLE = bool(getattr(FreeCAD, "GuiUp", False)) \
        and hasattr(Gui, "addCommand")
except Exception:
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:
    import os

    _ICON = os.path.join(FreeCAD.getUserAppDataDir(), "Mod",
                         "wavesim-workbench", "Resources", "icons",
                         "settings.svg")

    class CommandAgentBridge(object):
        """Toggle the Agent bridge (opt-in AI agent access)."""

        def GetResources(self):
            return {
                "Pixmap": _ICON,
                "MenuText": "Agent Bridge On/Off",
                "Checkable": is_running(),
                "CmdType": "ForEdit",
                "ToolTip": "Let the Wavesim AI agent (VS Code, via MCP) drive "
                           "this FreeCAD session. Listens on 127.0.0.1 only "
                           "and needs the token from Wavesim -> Settings.",
            }

        def Activated(self, checked=None):
            if checked is None:
                checked = not is_running()
            if checked:
                try:
                    start_bridge()
                except Exception:
                    pass
            else:
                stop_bridge()
            _update_indicator()

        def IsActive(self):
            return True

    Gui.addCommand("Wavesim_AgentBridge", CommandAgentBridge())
