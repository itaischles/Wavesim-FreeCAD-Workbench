# -*- coding: utf-8 -*-
"""Scripting API: document operations exposed to the Agent bridge.

Only these document-level operations are reachable over the bridge (besides
the setup/geometry API): create, open, save, recompute, activate, list, close.
"""

import os

import FreeCAD

from wavesim_gui import api_common as c
from wavesim_gui.api_common import ApiError, api_function

__all__ = ["new_document", "open_document", "save_document", "recompute",
           "active_document", "set_active_document", "list_documents",
           "close_document"]


def _doc_info(doc):
    try:
        modified = bool(doc.isTouched())
    except Exception:
        modified = None
    return {"name": doc.Name, "label": doc.Label,
            "path": doc.FileName or None, "modified": modified,
            "objects": len(doc.Objects),
            "active": FreeCAD.ActiveDocument is doc}


@api_function("document")
def new_document(name="Unnamed"):
    """Create a new empty document and make it active."""
    doc = FreeCAD.newDocument(str(name))
    FreeCAD.setActiveDocument(doc.Name)
    _activate_view(doc)
    return _doc_info(doc)


def _activate_view(doc):
    if getattr(FreeCAD, "GuiUp", False):
        try:
            import FreeCADGui
            FreeCADGui.ActiveDocument = FreeCADGui.getDocument(doc.Name)
        except Exception:
            pass


@api_function("document")
def open_document(path=None):
    """Open an ``.FCStd`` file (or activate it if it is already open)."""
    if not path or not str(path).lower().endswith(".fcstd"):
        raise ApiError("Give the path of an .FCStd file.")
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise ApiError("File not found: {}".format(path))
    for doc in FreeCAD.listDocuments().values():
        if doc.FileName and os.path.normcase(os.path.abspath(doc.FileName)) \
                == os.path.normcase(path):
            FreeCAD.setActiveDocument(doc.Name)
            _activate_view(doc)
            return _doc_info(doc)
    doc = FreeCAD.openDocument(path)
    FreeCAD.setActiveDocument(doc.Name)
    _activate_view(doc)
    return _doc_info(doc)


@api_function("document")
def save_document(doc=None, path=None):
    """Save the document (to *path* when given, as ``.FCStd``)."""
    doc = c.get_doc(doc)
    if path:
        if not str(path).lower().endswith(".fcstd"):
            raise ApiError("The file name must end in .FCStd.")
        path = os.path.abspath(path)
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            raise ApiError("Folder {} does not exist.".format(folder))
        doc.saveAs(path)
    else:
        if not doc.FileName:
            raise ApiError("The document has never been saved; give a path.")
        doc.save()
    return _doc_info(doc)


@api_function("document")
def recompute(doc=None):
    """Recompute the document; report objects that failed."""
    doc = c.get_doc(doc)
    doc.recompute()
    failed = []
    for obj in doc.Objects:
        try:
            state = list(obj.State)
        except Exception:
            continue
        if "Invalid" in state or "Error" in state:
            failed.append({"object": obj.Label, "state": state})
    return {"document": doc.Name, "failed": failed}


@api_function("document")
def active_document():
    """The active document (or ``None``)."""
    doc = FreeCAD.ActiveDocument
    return _doc_info(doc) if doc is not None else None


@api_function("document")
def set_active_document(doc=None):
    """Make an open document the active one."""
    doc = c.get_doc(doc)
    FreeCAD.setActiveDocument(doc.Name)
    _activate_view(doc)
    return _doc_info(doc)


@api_function("document")
def list_documents():
    """Every open document."""
    return [_doc_info(d) for d in FreeCAD.listDocuments().values()]


@api_function("document")
def close_document(doc=None, discard_changes=False):
    """Close a document. Refuses unsaved changes unless *discard_changes*."""
    doc = c.get_doc(doc)
    touched = False
    try:
        touched = bool(doc.isTouched()) or any(
            o.isTouched() for o in doc.Objects)
    except Exception:
        pass
    if touched and not discard_changes and doc.FileName:
        raise ApiError("Document {!r} has unsaved changes; save it first or "
                       "pass discard_changes=true.".format(doc.Label))
    name = doc.Name
    FreeCAD.closeDocument(name)
    return {"closed": name}
