# -*- coding: utf-8 -*-
"""Wavesim workbench settings.

These settings live *inside the workbench* rather than in FreeCAD's global
preferences dialog. They are persisted to a small JSON file under the user's
FreeCAD app-data directory, fully decoupled from FreeCAD's own configuration so
a FreeCAD reset never wipes them and they never clutter Edit -> Preferences.

The settings answer one question: which external Python interpreter and which
Wavesim repository should the workbench use to run the solver? The solver runs
out-of-process in a separate conda environment (see the workbench bridge
design), so FreeCAD needs to know where that interpreter and repository live.

This module is split in two halves:

* A small, Qt-free storage API (:func:`load`, :func:`save`, :func:`get`,
  :func:`get_wavesim_python`, :func:`get_wavesim_path`) that any FreeCAD-side
  code can call to read the configured paths.
* A GUI half (the settings dialog and the ``Wavesim_Settings`` command) that is
  only wired up when ``FreeCADGui`` and a Qt binding are available.
"""

import json
import os

import FreeCAD


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

# Stored alongside FreeCAD's user data but outside user.cfg, so this is the
# workbench's own file rather than a global FreeCAD preference.
CONFIG_PATH = os.path.join(FreeCAD.getUserAppDataDir(), "wavesim_settings.json")

# The interpreter and repository paths have no built-in default (they are
# machine-specific): point the workbench at them via Wavesim -> Settings, or the
# WAVESIM_PYTHON / WAVESIM_PATH environment variables, which override an empty
# value. The results folder defaults to a subdirectory of FreeCAD's app data.
DEFAULTS = {
    "wavesim_python": "",
    "wavesim_path": "",
    # Where run output (job working dirs with results.npz/summary.json) lands.
    "wavesim_results": os.path.join(
        FreeCAD.getUserAppDataDir(), "wavesim_results"
    ),
    # Optional path to the ngspice shared library (ngspice.dll / libngspice.so)
    # used for SPICE co-simulation ports. Empty means "let PySpice find it"
    # (its own search / NGSPICE_LIBRARY_PATH / bundled DLL).
    "ngspice_dll": "",
}

# Environment variable that overrides each key when the stored value is absent.
_ENV_OVERRIDES = {
    "wavesim_python": "WAVESIM_PYTHON",
    "wavesim_path": "WAVESIM_PATH",
    "wavesim_results": "WAVESIM_RESULTS",
    "ngspice_dll": "WAVESIM_NGSPICE_DLL",
}


def load():
    """Return the current settings as a dict.

    Precedence per key: a value saved in :data:`CONFIG_PATH` wins; otherwise the
    matching environment variable; otherwise the built-in default. Unknown keys
    in the file are ignored, and a corrupt file falls back to defaults with a
    warning rather than raising.
    """
    settings = dict(DEFAULTS)

    stored = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            stored = loaded
    except FileNotFoundError:
        pass
    except Exception as exc:  # pragma: no cover - diagnostic path
        FreeCAD.Console.PrintWarning(
            "Wavesim: could not read settings from {}: {}\n".format(
                CONFIG_PATH, exc
            )
        )

    for key in DEFAULTS:
        value = stored.get(key)
        if value:
            settings[key] = value
        elif os.environ.get(_ENV_OVERRIDES[key]):
            settings[key] = os.environ[_ENV_OVERRIDES[key]]

    return settings


def save(settings):
    """Persist *settings* (a dict) to :data:`CONFIG_PATH`.

    Only known keys are written. Returns ``True`` on success, ``False`` (with a
    console warning) if the file could not be written.
    """
    to_store = {key: settings[key] for key in DEFAULTS if key in settings}
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(to_store, handle, indent=2)
        return True
    except Exception as exc:  # pragma: no cover - diagnostic path
        FreeCAD.Console.PrintError(
            "Wavesim: could not write settings to {}: {}\n".format(
                CONFIG_PATH, exc
            )
        )
        return False


def get(key):
    """Return a single setting value by key."""
    return load()[key]


def get_wavesim_python():
    """Path to the external Python interpreter that runs the solver."""
    return get("wavesim_python")


def get_wavesim_path():
    """Path to the repository that contains the ``wavesim`` package."""
    return get("wavesim_path")


def get_results_path():
    """Folder where solver run output (working dirs + results) is written."""
    return get("wavesim_results")


def get_ngspice_dll():
    """Path to the ngspice shared library for SPICE co-simulation ports.

    Empty when unset — the solver then falls back to PySpice's own library
    search (``NGSPICE_LIBRARY_PATH`` / the bundled DLL).
    """
    return get("ngspice_dll")


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #

# The storage API above must stay importable in console mode, so the Qt/Gui
# layer is optional. When it is unavailable we simply skip registering the
# command and dialog.
try:
    import FreeCADGui as Gui

    try:
        from PySide import QtWidgets, QtCore  # noqa: F401
    except ImportError:  # older FreeCAD shims expose widgets under QtGui
        from PySide import QtGui as QtWidgets  # noqa: F401
        from PySide import QtCore  # noqa: F401

    _GUI_AVAILABLE = True
except Exception as exc:  # console mode / no Qt — log so it is diagnosable
    FreeCAD.Console.PrintWarning(
        "Wavesim: settings GUI not registered ({}: {})\n".format(
            type(exc).__name__, exc
        )
    )
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

    _WB_DIR = os.path.join(
        FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench"
    )
    # FreeCAD ships this themed icon; falls back gracefully if unavailable.
    _SETTINGS_ICON = "preferences-general"

    class SettingsDialog(QtWidgets.QDialog):
        """Modal dialog to view and edit the workbench solver paths."""

        def __init__(self, parent=None):
            super(SettingsDialog, self).__init__(parent)
            self.setWindowTitle("Wavesim Settings")
            self.setMinimumWidth(560)

            current = load()

            layout = QtWidgets.QVBoxLayout(self)

            form = QtWidgets.QFormLayout()
            self._python_edit = QtWidgets.QLineEdit(current["wavesim_python"])
            self._path_edit = QtWidgets.QLineEdit(current["wavesim_path"])
            self._results_edit = QtWidgets.QLineEdit(current["wavesim_results"])
            self._ngspice_edit = QtWidgets.QLineEdit(current["ngspice_dll"])

            form.addRow(
                "Solver Python interpreter:",
                self._row(
                    self._python_edit,
                    self._browse_python,
                ),
            )
            form.addRow(
                "Wavesim repository:",
                self._row(
                    self._path_edit,
                    self._browse_path,
                ),
            )
            form.addRow(
                "Results output folder:",
                self._row(
                    self._results_edit,
                    self._browse_results,
                ),
            )
            form.addRow(
                "ngspice.dll (optional):",
                self._row(
                    self._ngspice_edit,
                    self._browse_ngspice,
                ),
            )
            layout.addLayout(form)

            hint = QtWidgets.QLabel(
                "These point the workbench at the external conda environment "
                "and the Wavesim solver source. Stored at:\n{}".format(
                    CONFIG_PATH
                )
            )
            hint.setWordWrap(True)
            hint.setStyleSheet("color: gray;")
            layout.addWidget(hint)

            buttons = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.Save
                | QtWidgets.QDialogButtonBox.Cancel
            )
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

        def _row(self, line_edit, on_browse):
            """Wrap *line_edit* and a Browse button in a horizontal layout."""
            container = QtWidgets.QWidget()
            row = QtWidgets.QHBoxLayout(container)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(line_edit)
            browse = QtWidgets.QPushButton("Browse...")
            browse.clicked.connect(on_browse)
            row.addWidget(browse)
            return container

        def _browse_python(self):
            start = self._python_edit.text() or os.path.expanduser("~")
            chosen, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                "Select the solver Python interpreter",
                os.path.dirname(start),
                "Python interpreter (python.exe python);;All files (*)",
            )
            if chosen:
                self._python_edit.setText(chosen)

        def _browse_path(self):
            start = self._path_edit.text() or os.path.expanduser("~")
            chosen = QtWidgets.QFileDialog.getExistingDirectory(
                self,
                "Select the Wavesim repository",
                start,
            )
            if chosen:
                self._path_edit.setText(chosen)

        def _browse_results(self):
            start = self._results_edit.text() or os.path.expanduser("~")
            chosen = QtWidgets.QFileDialog.getExistingDirectory(
                self,
                "Select the results output folder",
                start,
            )
            if chosen:
                self._results_edit.setText(chosen)

        def _browse_ngspice(self):
            start = self._ngspice_edit.text() or os.path.expanduser("~")
            chosen, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                "Select the ngspice shared library",
                os.path.dirname(start),
                "ngspice library (ngspice*.dll libngspice*.so *.dll *.so);;"
                "All files (*)",
            )
            if chosen:
                self._ngspice_edit.setText(chosen)

        def accept(self):
            """Validate and persist before closing."""
            python_path = self._python_edit.text().strip()
            repo_path = self._path_edit.text().strip()
            results_path = self._results_edit.text().strip()
            ngspice_dll = self._ngspice_edit.text().strip()

            warnings = []
            if not os.path.isfile(python_path):
                warnings.append(
                    "The Python interpreter path does not exist:\n{}".format(
                        python_path
                    )
                )
            if not os.path.isdir(repo_path):
                warnings.append(
                    "The Wavesim repository path does not exist:\n{}".format(
                        repo_path
                    )
                )
            if not os.path.isdir(os.path.join(repo_path, "wavesim")):
                warnings.append(
                    "No 'wavesim' package found under:\n{}".format(repo_path)
                )
            if not results_path:
                warnings.append("The results output folder is empty.")
            # ngspice.dll is optional (only needed for SPICE ports); warn only
            # when a path is given but does not point at a file.
            if ngspice_dll and not os.path.isfile(ngspice_dll):
                warnings.append(
                    "The ngspice library path does not exist:\n{}".format(
                        ngspice_dll
                    )
                )

            if warnings:
                answer = QtWidgets.QMessageBox.warning(
                    self,
                    "Wavesim Settings",
                    "\n\n".join(warnings)
                    + "\n\nSave these settings anyway?",
                    QtWidgets.QMessageBox.Save
                    | QtWidgets.QMessageBox.Cancel,
                    QtWidgets.QMessageBox.Cancel,
                )
                if answer != QtWidgets.QMessageBox.Save:
                    return

            if save(
                {
                    "wavesim_python": python_path,
                    "wavesim_path": repo_path,
                    "wavesim_results": results_path,
                    "ngspice_dll": ngspice_dll,
                }
            ):
                FreeCAD.Console.PrintMessage(
                    "Wavesim: settings saved to {}\n".format(CONFIG_PATH)
                )
                super(SettingsDialog, self).accept()

    class CommandWavesimSettings:
        """Workbench command that opens the settings dialog."""

        def GetResources(self):
            return {
                "Pixmap": _SETTINGS_ICON,
                "MenuText": "Settings...",
                "ToolTip": "Configure the Wavesim solver interpreter and "
                "repository paths",
            }

        def Activated(self):
            dialog = SettingsDialog(Gui.getMainWindow())
            dialog.exec_()

        def IsActive(self):
            return True

    Gui.addCommand("Wavesim_Settings", CommandWavesimSettings())
