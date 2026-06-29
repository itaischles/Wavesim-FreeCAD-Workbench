# -*- coding: utf-8 -*-
"""TEM (waveguide-port) source for the Wavesim workbench (Session 9).

A *TEM Source* launches a transverse-electromagnetic port mode on a domain face.
Unlike the point :mod:`~wavesim_gui.source` (a single soft cell), it excites a
whole grid plane with the modal field profile the solver's TEM mode solver finds
for the PEC cross-section lying on that plane (e.g. a coax, stripline or
microstrip port). It is a scripted DocumentObject grouped under the simulation's
"Sources" child group.

Workflow
--------
* The user adds a TEM source, picks one of the six domain faces (the launch
  plane) in the task panel, and that face's boundary condition is set to **PML**
  automatically so the backward/reflected wave is absorbed -- the standard FDTD
  port setup.
* The mode itself is solved out-of-process by the conda-side ``runner.py`` (it
  needs scipy/numba, unavailable in FreeCAD's Python). The panel's **Compute
  Mode** button runs a *mode-only* job (no FDTD time-stepping); otherwise the
  main Run solves the mode just before stepping the simulation. Either way the
  solved mode lands in the Results tree as a clickable node showing the mode
  shape and the port's per-unit-length parameters (Z0, eps_eff, C, L, v).

Rendering
---------
The source draws as a translucent teal plane on the chosen face spanning the
domain box (mirroring the snapshot monitor's plane), so the launch plane is
visible and the standard "eye" toggle shows/hides it.

Units: FreeCAD geometry/properties are in millimetres; the solver works in
metres. :func:`tem_source_spec` converts the face plane's position to metres and
into the solver frame (measured from the domain origin) for the runner.

Importing this module registers ``Wavesim_AddTEMSource`` with ``Gui.addCommand``
when a GUI is available.
"""

import os

import FreeCAD

from wavesim_gui.commands import active_simulation
from wavesim_gui import units
from wavesim_gui import domain as domain_mod


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_RESOURCES_DIR = os.path.join(_WB_DIR, "Resources")
_TEM_ICON = os.path.join(_RESOURCES_DIR, "port.png")

_TYPE_PROP = "WavesimType"
_TEM_TYPE = "TEMSource"

# Name of the child group (created by CommandNewSimulation) holding sources.
_SOURCES_GROUP = "Sources"

# The six domain faces, in the solver's '<axis><0|1>' naming.
_FACES = ("x0", "x1", "y0", "y1", "z0", "z1")
# Human labels for the face dropdown.
_FACE_LABELS = {
    "x0": "X min (x0)", "x1": "X max (x1)",
    "y0": "Y min (y0)", "y1": "Y max (y1)",
    "z0": "Z min (z0)", "z1": "Z max (z1)",
}

# Excitation waveform families (only the Gaussian pulse is wired today).
_EXCITATIONS = ["Gaussian Pulse"]

# Which transverse fields to inject. Driving both E and H launches a directional
# (one-way) wave; E only is simpler but bidirectional. Display label -> token.
_FIELDS_LABELS = ["E and H (directional)", "E only (bidirectional)"]
_FIELDS_TOKEN = {"E and H (directional)": "EH", "E only (bidirectional)": "E"}
_FIELDS_FROM_TOKEN = {v: k for k, v in _FIELDS_TOKEN.items()}

# Boundary condition forced on the launch face (absorbing port).
_PORT_BC = "PML"

# Translucent teal plane, distinct from the orange monitor / green point source.
_TEM_COLOR = (0.0, 0.80, 0.80)
_TEM_TRANSPARENCY = 0.6

_MM_PER_M = 1000.0
_AXIS_IDX = {"x": 0, "y": 1, "z": 2}


# --------------------------------------------------------------------------- #
# Document-object model
# --------------------------------------------------------------------------- #

class TEMSourceObject:
    """``Proxy`` for a TEM port-source document object.

    Properties:
        ``Face``       -- domain face the port launches from ('x0'..'z1').
        ``Excitation`` -- temporal waveform family ('Gaussian Pulse').
        ``Fmax``       -- target maximum frequency of the pulse, in hertz (SI);
                          edited via the panel in the simulation's frequency unit.
        ``Amplitude``  -- peak amplitude of the waveform.
        ``Fields``     -- transverse fields injected ('EH' directional / 'E').

    Hidden ``Corners`` carries the launch plane's four world-mm corners for the
    view provider; ``execute`` keeps them in sync with the domain bounds + face.
    """

    def __init__(self, obj):
        self.Type = _TEM_TYPE
        obj.Proxy = self

        if not hasattr(obj, _TYPE_PROP):
            obj.addProperty(
                "App::PropertyString", _TYPE_PROP, "Wavesim",
                "Marks this object as a Wavesim TEM source",
            )
            setattr(obj, _TYPE_PROP, _TEM_TYPE)
            obj.setEditorMode(_TYPE_PROP, 1)  # read-only identity marker

        if not hasattr(obj, "Face"):
            obj.addProperty(
                "App::PropertyEnumeration", "Face", "Port",
                "Domain face the TEM port launches from (set to PML "
                "automatically)",
            )
            obj.Face = list(_FACES)
            obj.Face = "z0"
        if not hasattr(obj, "Fields"):
            obj.addProperty(
                "App::PropertyEnumeration", "Fields", "Port",
                "Transverse fields injected: E and H (directional) or E only",
            )
            obj.Fields = ["EH", "E"]
            obj.Fields = "EH"
        if not hasattr(obj, "Excitation"):
            obj.addProperty(
                "App::PropertyEnumeration", "Excitation", "Excitation",
                "Temporal waveform driving the port",
            )
            obj.Excitation = _EXCITATIONS
            obj.Excitation = _EXCITATIONS[0]
        if not hasattr(obj, "Fmax"):
            obj.addProperty(
                "App::PropertyFloat", "Fmax", "Excitation",
                "Target maximum frequency of the Gaussian pulse, in hertz "
                "(edit via the panel in the simulation's frequency unit)",
            )
            obj.Fmax = 30.0e9
            obj.setEditorMode("Fmax", 1)  # read-only; edit through the panel
        if not hasattr(obj, "Amplitude"):
            obj.addProperty(
                "App::PropertyFloat", "Amplitude", "Excitation",
                "Peak amplitude of the excitation waveform",
            )
            obj.Amplitude = 1.0

        # Plane corners (hidden, four world-mm points) for the view provider.
        if not hasattr(obj, "Corners"):
            obj.addProperty("App::PropertyVectorList", "Corners", "Plane", "")
            obj.setEditorMode("Corners", 2)  # hidden

    def onDocumentRestored(self, obj):
        obj.Proxy = self
        self.Type = getattr(self, "Type", _TEM_TYPE)
        if hasattr(obj, "Fmax"):
            obj.setEditorMode("Fmax", 1)

    def execute(self, obj):
        """Size/orient the drawn launch plane to the domain bounds and face."""
        sim = active_simulation(obj.Document)
        dom = domain_mod.find_domain(sim) if sim else None
        if dom is not None and (dom.DomainMax - dom.DomainMin).Length > 1.0e-9:
            mn, mx = dom.DomainMin, dom.DomainMax
        else:
            # No sized domain yet: a small default cube so the plane is visible.
            half = 5.0
            mn = FreeCAD.Vector(-half, -half, -half)
            mx = FreeCAD.Vector(half, half, half)
        obj.Corners = [FreeCAD.Vector(*p) for p in _face_corners(mn, mx, str(obj.Face))]

    def dumps(self):
        return {"Type": getattr(self, "Type", _TEM_TYPE)}

    def loads(self, state):
        if isinstance(state, dict):
            self.Type = state.get("Type", _TEM_TYPE)
        return None

    __getstate__ = dumps
    __setstate__ = loads


def _face_corners(mn, mx, face):
    """Four (x, y, z) corners of the *face* plane spanning the box *mn*..*mx*."""
    axis = face[0]
    hi = face.endswith("1")
    if axis == "x":
        x = mx.x if hi else mn.x
        return [(x, mn.y, mn.z), (x, mx.y, mn.z), (x, mx.y, mx.z), (x, mn.y, mx.z)]
    if axis == "y":
        y = mx.y if hi else mn.y
        return [(mn.x, y, mn.z), (mx.x, y, mn.z), (mx.x, y, mx.z), (mn.x, y, mx.z)]
    z = mx.z if hi else mn.z
    return [(mn.x, mn.y, z), (mx.x, mn.y, z), (mx.x, mx.y, z), (mn.x, mx.y, z)]


# --------------------------------------------------------------------------- #
# Lookup helpers & job serialisation
# --------------------------------------------------------------------------- #

def is_tem_source(obj):
    """Return True if *obj* is a Wavesim TEM Source object."""
    return getattr(obj, _TYPE_PROP, None) == _TEM_TYPE


def sources_group(sim):
    """Return the "Sources" child group of *sim* (or *sim* itself if missing)."""
    if sim is None:
        return None
    for child in sim.Group:
        if child.Name == _SOURCES_GROUP or child.Label == _SOURCES_GROUP:
            return child
    return sim


def find_tem_sources(sim):
    """Return all TEM Source objects under the Simulation container *sim*."""
    grp = sources_group(sim)
    if grp is None:
        return []
    return [obj for obj in grp.Group if is_tem_source(obj)]


def tem_source_spec(obj, origin_m):
    """Return the ``job.json`` ``tem_sources`` dict for *obj* in the solver frame.

    The launch plane sits on the chosen domain face; its position along the face
    normal is taken from the domain box and shifted into the solver frame (the
    domain origin is subtracted, mirroring :func:`source.source_spec`).
    """
    sim = active_simulation(obj.Document)
    dom = domain_mod.find_domain(sim) if sim else None
    face = str(obj.Face)
    axis = domain_mod.face_axis(face)
    world_mm = domain_mod.face_world_coord_mm(dom, face) if dom is not None else 0.0
    position = world_mm / _MM_PER_M - origin_m[_AXIS_IDX[axis]]
    return {
        "name": str(obj.Label or obj.Name),
        "normal": axis,
        "position": position,
        "fmax": float(getattr(obj, "Fmax", 0.0)),  # stored in Hz
        "amplitude": float(getattr(obj, "Amplitude", 1.0)),
        "fields": str(getattr(obj, "Fields", "EH")),
    }


def _describe(obj):
    """Short human label, e.g. ``z0 @ 30 GHz``, in the simulation's freq unit."""
    doc = getattr(obj, "Document", None)
    sim = active_simulation(doc) if doc is not None else None
    unit = units.get_frequency_unit(sim)
    value = units.freq_from_si(float(getattr(obj, "Fmax", 0.0)), unit)
    return "{} @ {:g} {}".format(getattr(obj, "Face", "z0"), value, unit)


# --------------------------------------------------------------------------- #
# GUI: view provider, task panel, command
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:  # console mode / no Qt
    _GUI_AVAILABLE = False


if _GUI_AVAILABLE:

    class TEMSourceViewProvider:
        """Coin view provider drawing the port as a translucent teal plane."""

        def __init__(self, vobj):
            vobj.Proxy = self

        def attach(self, vobj):
            from pivy import coin

            self.Object = vobj.Object
            root = coin.SoSeparator()

            # Two-sided lighting so the translucent plane shows from behind.
            hints = coin.SoShapeHints()
            hints.vertexOrdering = coin.SoShapeHints.COUNTERCLOCKWISE
            hints.shapeType = coin.SoShapeHints.UNKNOWN_SHAPE_TYPE
            root.addChild(hints)

            material = coin.SoMaterial()
            material.diffuseColor.setValue(*_TEM_COLOR)
            material.transparency.setValue(_TEM_TRANSPARENCY)
            root.addChild(material)

            self._coords = coin.SoCoordinate3()
            root.addChild(self._coords)
            self._face = coin.SoFaceSet()
            root.addChild(self._face)

            # Opaque border so the plane edges read clearly.
            border = coin.SoSeparator()
            bcolor = coin.SoBaseColor()
            bcolor.rgb.setValue(*_TEM_COLOR)
            border.addChild(bcolor)
            bstyle = coin.SoDrawStyle()
            bstyle.lineWidth = 2
            border.addChild(bstyle)
            self._border_coords = coin.SoCoordinate3()
            border.addChild(self._border_coords)
            self._border_lines = coin.SoIndexedLineSet()
            border.addChild(self._border_lines)
            root.addChild(border)

            self._root = root
            vobj.addDisplayMode(root, "Plane")
            self._rebuild()

        def _clear(self):
            if self._coords.point.getNum():
                self._coords.point.deleteValues(0)
            self._face.numVertices.setValue(0)
            if self._border_coords.point.getNum():
                self._border_coords.point.deleteValues(0)
            if self._border_lines.coordIndex.getNum():
                self._border_lines.coordIndex.deleteValues(0)

        def _rebuild(self):
            obj = getattr(self, "Object", None)
            if obj is None:
                return
            corners = list(getattr(obj, "Corners", []) or [])
            if len(corners) != 4:
                self._clear()
                return
            pts = [(v.x, v.y, v.z) for v in corners]

            self._coords.point.setValues(0, len(pts), pts)
            if self._coords.point.getNum() > len(pts):
                self._coords.point.deleteValues(len(pts))
            self._face.numVertices.setValue(len(pts))

            self._border_coords.point.setValues(0, len(pts), pts)
            if self._border_coords.point.getNum() > len(pts):
                self._border_coords.point.deleteValues(len(pts))
            edges = [0, 1, 2, 3, 0, -1]
            self._border_lines.coordIndex.setValues(0, len(edges), edges)
            if self._border_lines.coordIndex.getNum() > len(edges):
                self._border_lines.coordIndex.deleteValues(len(edges))

        def updateData(self, obj, prop):
            if prop == "Corners":
                self._rebuild()

        def getDisplayModes(self, vobj):
            return ["Plane"]

        def getDefaultDisplayMode(self):
            return "Plane"

        def setDisplayMode(self, mode):
            return mode

        def getIcon(self):
            return _TEM_ICON

        def setEdit(self, vobj, mode=0):
            _open_tem_panel(vobj.Object)
            return True

        def doubleClicked(self, vobj):
            _open_tem_panel(vobj.Object)
            return True

        def dumps(self):
            return None

        def loads(self, state):
            return None

        __getstate__ = dumps
        __setstate__ = loads

    class TaskTEMSourcePanel:
        """Task panel to edit a TEM port: face, fields and excitation.

        "Compute Mode" solves and visualises the port mode now (out of process,
        no FDTD); OK commits the source and leaves the mode for the main Run.
        Cancel removes a freshly-created source so it leaves no trace.
        """

        def __init__(self, obj, created=False):
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets

            self.obj = obj
            self.created = created
            self._orig_face = str(getattr(obj, "Face", "z0"))

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim TEM Source")
            layout = QtWidgets.QFormLayout(form)

            self._face = QtWidgets.QComboBox()
            for f in _FACES:
                self._face.addItem(_FACE_LABELS[f], f)
            self._face.setCurrentIndex(
                max(0, list(_FACES).index(str(getattr(obj, "Face", "z0"))))
            )

            self._fields = QtWidgets.QComboBox()
            self._fields.addItems(_FIELDS_LABELS)
            self._fields.setCurrentText(
                _FIELDS_FROM_TOKEN.get(str(getattr(obj, "Fields", "EH")),
                                       _FIELDS_LABELS[0])
            )

            self._excitation = QtWidgets.QComboBox()
            self._excitation.addItems(_EXCITATIONS)
            self._excitation.setCurrentText(
                str(getattr(obj, "Excitation", _EXCITATIONS[0]))
            )

            sim = active_simulation(obj.Document)
            self._freq_unit = units.get_frequency_unit(sim)
            self._fmax = QtWidgets.QDoubleSpinBox()
            self._fmax.setRange(1.0e-9, 1.0e15)
            self._fmax.setDecimals(6)
            self._fmax.setSuffix(" " + self._freq_unit)
            self._fmax.setSingleStep(1.0)
            self._fmax.setValue(
                units.freq_from_si(float(getattr(obj, "Fmax", 30.0e9)),
                                   self._freq_unit)
            )

            self._amplitude = QtWidgets.QDoubleSpinBox()
            self._amplitude.setRange(-1.0e9, 1.0e9)
            self._amplitude.setDecimals(4)
            self._amplitude.setSingleStep(0.1)
            self._amplitude.setValue(float(getattr(obj, "Amplitude", 1.0)))

            layout.addRow("Launch face:", self._face)
            layout.addRow("Inject fields:", self._fields)
            layout.addRow("Excitation:", self._excitation)
            layout.addRow("Max frequency:", self._fmax)
            layout.addRow("Amplitude:", self._amplitude)

            self._compute = QtWidgets.QPushButton("Compute Mode")
            layout.addRow(self._compute)

            info = QtWidgets.QLabel(
                "The port launches the TEM mode of the PEC cross-section on the "
                "chosen face (which is set to PML automatically). The face must "
                "cut at least two conductors. 'Compute Mode' solves and plots "
                "the mode now; otherwise it is solved when you Run the "
                "simulation. The frequency unit is set on the Simulation object."
            )
            info.setWordWrap(True)
            layout.addRow(info)

            # Live-update the drawn plane as the face changes.
            self._face.currentIndexChanged.connect(self._live_face)
            self._compute.clicked.connect(self._on_compute)

            self.form = form

        def _selected_face(self):
            return self._face.currentData() or _FACES[self._face.currentIndex()]

        def _live_face(self, *_):
            self.obj.Face = self._selected_face()
            self.obj.Document.recompute()

        def _commit(self, title):
            """Write the widget values onto the object and force PML on the face.

            Returns after committing + recomputing; the domain is re-synced so it
            re-sizes to the (possibly changed) port plane. Shared by Accept and
            Compute Mode so both see exactly the same persisted state.
            """
            doc = self.obj.Document
            # Restore the original face first so the transaction captures the full
            # change (the live edit already moved the object outside it).
            self.obj.Face = self._orig_face
            doc.openTransaction(title)
            face = self._selected_face()
            self.obj.Face = face
            self.obj.Fields = _FIELDS_TOKEN[self._fields.currentText()]
            self.obj.Excitation = self._excitation.currentText()
            self.obj.Fmax = units.freq_to_si(self._fmax.value(), self._freq_unit)
            self.obj.Amplitude = self._amplitude.value()
            self.obj.Label = "TEM Source ({})".format(_describe(self.obj))
            # Absorbing port: force the launch face to PML.
            domain_mod.set_face_bc(domain_mod.find_domain(active_simulation(doc)),
                                   face, _PORT_BC)
            doc.commitTransaction()
            doc.recompute()
            domain_mod.notify_domain_inputs_changed(doc)
            self._orig_face = face

        def _on_compute(self, *_):
            self._commit("Wavesim: Edit TEM Source")
            run_mode_solve(self.obj.Document, self.obj)

        def accept(self):
            self._commit("Wavesim: Edit TEM Source")
            Gui.Control.closeDialog()
            return True

        def reject(self):
            doc = self.obj.Document
            if self.created:
                doc.openTransaction("Wavesim: Cancel TEM Source")
                doc.removeObject(self.obj.Name)
                doc.commitTransaction()
                doc.recompute()
            else:
                self.obj.Face = self._orig_face
                doc.recompute()
            Gui.Control.closeDialog()
            return True

        def getStandardButtons(self):
            try:
                from PySide import QtWidgets as _w
            except ImportError:
                from PySide import QtGui as _w
            buttons = _w.QDialogButtonBox.Ok | _w.QDialogButtonBox.Cancel
            return int(getattr(buttons, "value", buttons))

    def _open_tem_panel(obj, created=False):
        """Open (or replace) the TEM source task panel bound to *obj*."""
        Gui.Control.closeDialog()
        Gui.Control.showDialog(TaskTEMSourcePanel(obj, created=created))

    def run_mode_solve(doc, focus_obj=None):
        """Solve the document's TEM port mode(s) out of process (no FDTD run).

        Builds the usual voxelised job, flags it ``mode_only``, runs the
        conda-side runner, then (re)builds the Results tree and opens the first
        solved mode. *focus_obj* is the TEM source that triggered the solve (used
        only to scope warnings); all defined ports are solved together.
        """
        try:
            from PySide import QtWidgets
        except ImportError:
            from PySide import QtGui as QtWidgets
        from wavesim_gui import job as job_mod
        from wavesim_gui import run as run_mod
        from wavesim_gui import voxelize as vox_mod
        from wavesim_gui import results as results_mod

        sim = active_simulation(doc)
        main = Gui.getMainWindow()
        # Voxelisation runs on the GUI thread and can be slow; show a cancelable
        # progress dialog while it sweeps the geometry.
        vox_dialog, vox_cb = run_mod.voxelization_progress(
            main, "Wavesim Mode Solve", "Voxelizing geometry..."
        )
        try:
            spec, arrays = vox_mod.build_job_from_document(doc, progress=vox_cb)
        except vox_mod.VoxelizationCancelled:
            vox_dialog.close()
            FreeCAD.Console.PrintWarning("Wavesim: mode solve cancelled.\n")
            return
        except vox_mod.GridRequiredError as exc:
            vox_dialog.close()
            QtWidgets.QMessageBox.warning(main, "Wavesim Mode Solve", str(exc))
            return
        vox_dialog.close()
        if spec is None or arrays is None:
            QtWidgets.QMessageBox.warning(
                main, "Wavesim Mode Solve",
                "Assign materials (with PEC conductors crossing the port plane) "
                "before computing a mode.",
            )
            return
        if not spec.get("tem_sources"):
            QtWidgets.QMessageBox.warning(
                main, "Wavesim Mode Solve", "No TEM source to solve.",
            )
            return

        spec["mode_only"] = True
        spec["steps"] = 1
        workdir = job_mod.new_workdir(prefix="mode")
        job_mod.write_job(workdir, spec)
        vox_mod.write_materials(workdir, arrays)

        FreeCAD.Console.PrintMessage(
            "Wavesim: solving TEM mode(s) in {}\n".format(workdir)
        )
        summary = run_mod.run_job(
            workdir, 1, parent=main,
            message="Preparing TEM mode solve...", busy=True,
        )
        if summary is None:
            return
        if not summary.get("modes"):
            QtWidgets.QMessageBox.information(
                main, "Wavesim Mode Solve",
                "No TEM mode was found. A TEM port plane needs at least two "
                "PEC conductors (e.g. a signal conductor and a ground/shield).",
            )
            return

        grp = results_mod.build_results(doc, sim, workdir, summary)
        results_mod.open_first_mode(grp)

    class CommandAddTEMSource:
        """Create a TEM port Source on a domain face and open its editor."""

        def GetResources(self):
            return {
                "Pixmap": _TEM_ICON,
                "MenuText": "Add TEM Source",
                "ToolTip": "Add a TEM waveguide-port source that launches the "
                "modal field of a PEC cross-section on a domain face",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = active_simulation(doc)
            if sim is None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: create a Simulation before adding a TEM source.\n"
                )
                return

            doc.openTransaction("Wavesim: Add TEM Source")
            try:
                tem = doc.addObject("App::FeaturePython", "TEMSource")
                TEMSourceObject(tem)
                tem.Label = "TEM Source ({})".format(_describe(tem))
                if tem.ViewObject is not None:
                    TEMSourceViewProvider(tem.ViewObject)
                sources_group(sim).addObject(tem)
            except Exception:
                doc.abortTransaction()
                raise
            doc.commitTransaction()
            doc.recompute()

            _open_tem_panel(tem, created=True)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    Gui.addCommand("Wavesim_AddTEMSource", CommandAddTEMSource())
