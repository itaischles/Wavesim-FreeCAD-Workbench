# -*- coding: utf-8 -*-
"""Scripting API: the ``Params`` spreadsheet, parameter bindings, fingerprint.

The project's ``PARAMS.md`` (outside FreeCAD) is the source of truth. The MCP
server parses it and pushes the resolved values here with
:func:`sync_params_to_spreadsheet`; FreeCAD properties then follow the values
through expressions on ``Params.<NAME>`` (see :func:`bind_param`).

Spreadsheet layout (sheet object ``Params``)::

    A: name   B: value (alias = NAME)   C: storage unit   D: description

Values are stored in the *storage unit* of their dimension (mm, Hz, s, deg,
ohm, F, H, V, A, S/m; ``-`` for numbers without dimension), which is what an
unitless expression yields in a FreeCAD property. Strings are text cells.

:func:`model_fingerprint` hashes the simulated geometry and the Wavesim setup
without voxelizing, so the MCP server can tell whether a run's results still
describe the current model.
"""

import hashlib
import json
import math
import re

import FreeCAD

from wavesim_gui import api_common as c
from wavesim_gui.api_common import ApiError, api_function

__all__ = [
    "sync_params_to_spreadsheet", "read_params_sheet", "bind_param",
    "unbind_param", "list_bindings", "property_dimension_of",
    "model_fingerprint",
]

SHEET = c.PARAMS_SHEET
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_REF_RE = re.compile(r"\b" + SHEET + r"\.([A-Z][A-Z0-9_]*)\b")
_HEADER = ("Name", "Value", "Unit", "Description")
_MAX_ROWS = 2000


def _sheet(doc, create=False):
    sheet = doc.getObject(SHEET)
    if sheet is not None and sheet.TypeId != "Spreadsheet::Sheet":
        raise ApiError("Object {!r} exists but is not a spreadsheet.".format(
            SHEET))
    if sheet is None and create:
        if doc.getObjectsByLabel(SHEET):
            raise ApiError("Another object is labelled {!r}; rename it so the "
                           "parameter sheet can use that name.".format(SHEET))
        sheet = doc.addObject("Spreadsheet::Sheet", SHEET)
        sheet.Label = SHEET
        for col, text in zip("ABCD", _HEADER):
            sheet.set("{}1".format(col), text)
    return sheet


def _rows(sheet):
    """``{alias: row}`` for every aliased value cell in column B."""
    out = {}
    empty = 0
    for row in range(2, _MAX_ROWS):
        alias = sheet.getAlias("B{}".format(row))
        if alias:
            out[alias] = row
            empty = 0
        elif not sheet.getContents("A{}".format(row)):
            empty += 1
            if empty > 50:
                break
    return out


def _cell_value(sheet, cell):
    try:
        value = sheet.get(cell)
    except Exception:
        return None
    if hasattr(value, "Value"):
        return float(value.Value)
    return value


def _same(a, b):
    if isinstance(a, str) or isinstance(b, str):
        return str(a) == str(b)
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-15)


def _number_text(value):
    text = repr(float(value))
    return text[:-2] if text.endswith(".0") else text


@api_function("params")
def sync_params_to_spreadsheet(doc=None, params=None, remove_missing=True):
    """Write resolved parameters into the ``Params`` sheet and recompute.

    Called by the MCP server after it has parsed and validated PARAMS.md --
    the file wins: any value edited by hand in FreeCAD is overwritten and
    reported as drift.

    :param params: ``{NAME: {"value": number_in_storage_unit | "text",
        "unit": storage_unit, "description": "..."}}``.
    :param remove_missing: drop sheet rows whose name is no longer in
        *params* (only when nothing is bound to them; otherwise kept and
        reported).
    :returns: ``{"created": [...], "updated": [...], "drift": [{"name",
        "sheet_value", "file_value"}], "removed": [...],
        "kept_missing_bound": [...], "unknown_references": [{"object",
        "property", "name"}], "unused": [...], "recompute_errors": [...]}``.
    """
    doc = c.get_doc(doc)
    if not isinstance(params, dict):
        raise ApiError("params must be a {NAME: {value, unit}} dict.")
    for name, entry in params.items():
        if not _NAME_RE.match(name):
            raise ApiError("Invalid parameter name {!r} (UPPER_SNAKE_CASE)."
                           .format(name))
        if not isinstance(entry, dict) or "value" not in entry:
            raise ApiError("Parameter {} needs a 'value'.".format(name))
    report = {"created": [], "updated": [], "drift": [], "removed": [],
              "kept_missing_bound": [], "unknown_references": [],
              "unused": [], "recompute_errors": []}
    with c.transaction(doc, "Wavesim: Sync Params"):
        sheet = _sheet(doc, create=True)
        rows = _rows(sheet)
        bound = _referenced_names(doc)
        next_row = max(list(rows.values()) + [1]) + 1
        for name, entry in params.items():
            value = entry["value"]
            is_text = isinstance(value, str)
            if name in rows:
                row = rows[name]
                old = _cell_value(sheet, "B{}".format(row))
                if not _same(old, value):
                    report["updated"].append(name)
                    if _was_hand_edited(sheet, row, old):
                        report["drift"].append({"name": name,
                                                "sheet_value": old,
                                                "file_value": value})
            else:
                row = next_row
                next_row += 1
                report["created"].append(name)
            sheet.set("A{}".format(row), name)
            sheet.set("B{}".format(row),
                      "'" + value if is_text else _number_text(value))
            sheet.set("C{}".format(row), str(entry.get("unit", "-")))
            sheet.set("D{}".format(row), "'" + str(
                entry.get("description", "")))
            # Remember what the file said, to tell drift from a stale sheet.
            sheet.set("E{}".format(row), "'" + (value if is_text else
                                                _number_text(value)))
            if sheet.getAlias("B{}".format(row)) != name:
                sheet.setAlias("B{}".format(row), name)
        for name, row in sorted(rows.items(), key=lambda kv: -kv[1]):
            if name in params:
                continue
            if name in bound:
                report["kept_missing_bound"].append(name)
                continue
            if remove_missing:
                sheet.setAlias("B{}".format(row), "")
                for col in "ABCDE":
                    sheet.clear("{}{}".format(col, row))
                report["removed"].append(name)
        _hide_column_e(sheet)
    doc.recompute()
    known = set(params)
    for ref in list_bindings(doc):
        for name in ref["params"]:
            if name not in known:
                report["unknown_references"].append(
                    {"object": ref["label"], "property": ref["property"],
                     "name": name})
    used = {n for ref in list_bindings(doc) for n in ref["params"]}
    report["unused"] = sorted(n for n in known if n not in used)
    for obj in doc.Objects:
        try:
            if "Invalid" in obj.State or "Error" in obj.State:
                report["recompute_errors"].append(
                    {"object": obj.Label, "state": list(obj.State)})
        except Exception:
            pass
    return report


def _was_hand_edited(sheet, row, current):
    """True when the B cell differs from what the last sync wrote (column E).

    A sheet value that still equals the last synced file value is merely out
    of date (the file changed); one that differs from it was edited in
    FreeCAD -- that is drift. A row with no record counts as edited.
    """
    last = sheet.getContents("E{}".format(row)).lstrip("'")
    if last == "":
        return True
    if isinstance(current, str):
        return current != last
    try:
        return not _same(current, float(last))
    except ValueError:
        return True


def _hide_column_e(sheet):
    try:
        sheet.setColumnWidth("E", 1)
    except Exception:
        pass


@api_function("params")
def read_params_sheet(doc=None):
    """Current values in the ``Params`` sheet: ``{NAME: value}`` (storage
    units), or ``{}`` when there is no sheet."""
    doc = c.get_doc(doc)
    sheet = _sheet(doc)
    if sheet is None:
        return {}
    return {name: _cell_value(sheet, "B{}".format(row))
            for name, row in _rows(sheet).items()}


def _referenced_names(doc):
    names = set()
    for ref in list_bindings(doc):
        names.update(ref["params"])
    return names


@api_function("params")
def list_bindings(doc=None):
    """Every property driven by a ``Params`` expression.

    :returns: ``[{"object", "label", "property", "expression", "params":
        [NAME, ...]}]`` (``property`` may be ``"Position.x"``).
    """
    doc = c.get_doc(doc)
    out = []
    for obj in doc.Objects:
        for entry in getattr(obj, "ExpressionEngine", None) or ():
            expr = str(entry[1])
            names = sorted(set(_REF_RE.findall(expr)))
            if names:
                out.append({"object": obj.Name, "label": obj.Label,
                            "property": str(entry[0]).lstrip("."),
                            "expression": expr, "params": names})
    return out


@api_function("params")
def property_dimension_of(doc=None, obj=None, prop=None):
    """Physical dimension a property expects (``"length"``, ``"frequency"``,
    ...), or ``None`` if unknown -- used to check a binding before making it.
    """
    doc = c.get_doc(doc)
    target = c.find_object(doc, obj)
    return {"object": target.Label, "property": prop,
            "dimension": c.property_dimension(target, prop),
            "storage_units": dict(c.STORAGE_UNIT)}


@api_function("params")
def bind_param(doc=None, obj=None, prop=None, expression=None):
    """Drive ``obj.prop`` by an expression over PARAMS names.

    Works for geometry (``Radius``, ``Height``, ``Placement.Base.z``) and
    Wavesim setup objects (``Frequency``, ``Position.x``, ``MaxFrequency``,
    ``Dx`` ...). The value is evaluated in the property's storage unit (mm,
    Hz, s, deg, ...), which is how the ``Params`` sheet stores values.

    :param prop: property name; a vector component as ``"Position.x"``;
        placement as ``"Placement.Base.x"`` or ``"Placement.Rotation.Angle"``.
    :param expression: e.g. ``"=CYLINDER_DIAMETER/2"`` (leading ``=``
        optional). Names must be UPPER_SNAKE_CASE parameters.
    :returns: ``{"object", "property", "expression", "value"}`` after a
        recompute.
    """
    doc = c.get_doc(doc)
    target = c.find_object(doc, obj)
    if not prop:
        raise ApiError("Give the property to bind.")
    base = prop.split(".")[0]
    if base not in target.PropertiesList:
        raise ApiError("{!r} has no property {!r}.".format(target.Label, base))
    if base in ("Label", "WavesimType", "Type", "Proxy"):
        raise ApiError("{!r} cannot be bound.".format(base))
    if not expression:
        raise ApiError("Give an expression, e.g. '=SUBSTRATE_H/2'.")
    fc_expr = c.param_expression(str(expression).strip())
    path = "." + prop if "." in prop else prop
    with c.transaction(doc, "Wavesim: Bind {}.{}".format(target.Label, prop)):
        try:
            target.setExpression(path, fc_expr)
        except Exception as exc:
            raise ApiError("FreeCAD rejected the expression {!r} on {}.{}: {}"
                           .format(fc_expr, target.Label, prop, exc))
    doc.recompute()
    value = _read_path(target, prop)
    return {"object": target.Label, "property": prop,
            "expression": fc_expr, "value": c.to_jsonable(value)}


def _read_path(obj, prop):
    value = obj
    for part in prop.split("."):
        value = getattr(value, part, None)
    return value


@api_function("params")
def unbind_param(doc=None, obj=None, prop=None):
    """Remove the expression on ``obj.prop`` (its current value stays)."""
    doc = c.get_doc(doc)
    target = c.find_object(doc, obj)
    path = "." + prop if "." in prop else prop
    with c.transaction(doc, "Wavesim: Unbind {}.{}".format(target.Label,
                                                           prop)):
        target.setExpression(path, None)
    doc.recompute()
    return {"object": target.Label, "property": prop}


# --------------------------------------------------------------------------- #
# Fingerprint
# --------------------------------------------------------------------------- #

_SIG = 10  # significant digits kept when hashing floats


def _canon(value):
    """JSON-ready canonical form, rounding floats so noise does not count."""
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        if value == 0.0:
            return 0.0
        return float("{:.{}g}".format(value, _SIG))
    if isinstance(value, str):
        return value
    if isinstance(value, FreeCAD.Vector):
        return [_canon(value.x), _canon(value.y), _canon(value.z)]
    if isinstance(value, FreeCAD.Placement):
        q = value.Rotation.Q
        return {"base": _canon(value.Base), "rot": [_canon(x) for x in q]}
    if hasattr(value, "Value") and hasattr(value, "Unit"):
        return _canon(float(value.Value))
    if hasattr(value, "Name") and hasattr(value, "TypeId"):
        return "@" + value.Name
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    return str(value)


def _shape_digest(shape):
    """Geometry digest: topology counts, measures and rounded vertices.

    Used instead of a raw BREP string because a BREP text carries
    tolerance/serialisation noise that changes across a save/reopen or an OCC
    update while the geometry itself does not.
    """
    verts = sorted(_canon(v.Point) for v in shape.Vertexes)
    bb = shape.BoundBox
    return {
        "solids": len(shape.Solids), "faces": len(shape.Faces),
        "edges": len(shape.Edges), "vertices": verts,
        "volume": _canon(float(shape.Volume)),
        "area": _canon(float(shape.Area)),
        "bbox": [_canon(x) for x in (bb.XMin, bb.YMin, bb.ZMin,
                                     bb.XMax, bb.YMax, bb.ZMax)],
        "face_types": sorted(f.Surface.TypeId for f in shape.Faces),
    }


_RESULT_TYPES = {"Results", "Result", "ResultPort", "ResultMatrix"}

_SETUP_SKIP = {"Proxy", "ExpressionEngine", "Label2", "Visibility", "Shape",
               "_GroupTouched", "NodesX", "NodesY", "NodesZ", "SnapsX",
               "SnapsY", "SnapsZ", "DomainMin", "DomainMax", "PmlMin",
               "PmlMax", "Nx", "Ny", "Nz", "TrackedBodies", "Corners",
               "GridPlaneX", "GridPlaneY", "GridPlaneZ", "ShowGridPlaneX",
               "ShowGridPlaneY", "ShowGridPlaneZ", "TimeUnit",
               "FrequencyUnit", "Color", "Content", "Placement"}


def _object_props(obj, skip):
    out = {}
    for prop in sorted(obj.PropertiesList):
        if prop in skip or prop in ("Label",):
            continue
        try:
            group = obj.getGroupOfProperty(prop)
        except Exception:
            group = ""
        if group in ("Base",) and prop != "Group":
            continue
        try:
            out[prop] = _canon(getattr(obj, prop))
        except Exception:
            continue
    return out


def _hash(data):
    text = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@api_function("params")
def model_fingerprint(doc=None):
    """Stable hashes of the simulated model, computed without voxelizing.

    ``model_hash`` covers every material-assigned body (geometry digest plus
    its Wavesim body properties: potentials, refinement); ``setup_hash``
    covers every object in the Simulation tree (materials, domain, sources,
    ports, monitors; results excluded) and the Simulation's own settings.
    Same model -> same hashes, across save/reopen.

    :returns: ``{"model_hash", "setup_hash", "workbench_version",
        "api_version", "bodies": n, "setup_objects": n}``.
    """
    from wavesim_gui import commands
    from wavesim_gui import materials as materials_mod
    import wavesim_gui

    doc = c.get_doc(doc)
    sim = commands.active_simulation(doc)
    if sim is None:
        raise ApiError("The document has no simulation.")
    bodies = {}
    for mat in materials_mod.find_materials(sim):
        for body in getattr(mat, "Bodies", []) or []:
            try:
                digest = _shape_digest(body.Shape)
            except Exception as exc:
                digest = {"error": str(exc)}
            digest["material"] = mat.Name
            digest["wavesim_props"] = {
                p: _canon(getattr(body, p)) for p in sorted(
                    body.PropertiesList) if p.startswith("Wavesim")}
            bodies[body.Name] = digest
    setup = {"simulation": _object_props(sim, _SETUP_SKIP | {"Group"})}
    stack = list(sim.Group)
    count = 0
    while stack:
        obj = stack.pop(0)
        # Loaded results (Results / port groups / Port Matrix) describe a
        # run, not the model, so loading them must not change the hash.
        if c.wavesim_type(obj) in _RESULT_TYPES:
            continue
        if obj.TypeId.startswith("App::DocumentObjectGroup"):
            stack.extend(obj.Group)
            continue
        setup[obj.Name] = {"type": c.wavesim_type(obj) or obj.TypeId,
                           "props": _object_props(obj, _SETUP_SKIP)}
        count += 1
    return {
        "model_hash": _hash(bodies),
        "setup_hash": _hash(setup),
        "workbench_version": getattr(wavesim_gui, "__version__", "unknown"),
        "api_version": c.API_VERSION,
        "bodies": len(bodies),
        "setup_objects": count,
    }
