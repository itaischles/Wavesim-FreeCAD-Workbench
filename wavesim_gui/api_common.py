# -*- coding: utf-8 -*-
"""Shared conventions for the GUI-free Wavesim scripting API.

Every ``wavesim_gui.api_*`` module builds on the helpers here, so the whole API
behaves the same way from the FreeCAD Python console, from ``FreeCADCmd`` tests
and from the Agent bridge (which exposes exactly the functions registered with
:func:`api_function`, nothing else).

Conventions
-----------
* **First argument.** Setup functions take ``sim`` first; it may be the
  Simulation object, a ``FreeCAD.Document``, a document name, or ``None`` (the
  active document's simulation) -- see :func:`get_sim`. Geometry and document
  functions take ``doc`` first with the same leniency (:func:`get_doc`). All
  other arguments are keyword arguments.
* **Object references.** Anywhere an object is expected, its internal
  ``Name``, its ``Label`` (must be unique) or the object itself is accepted
  (:func:`find_object`).
* **Quantities.** A dimensioned argument accepts

  - a plain number, read in the argument's documented default unit (mm for
    lengths, Hz, s, deg, ohm, F, H, V, A, S/m);
  - a string ``"<number> <unit>"`` with a case-sensitive unit from
    :data:`UNITS` (``"2.4 GHz"``, ``"50 mHz"`` is millihertz);
  - a ``{"value": v, "unit": u}`` dict;
  - a **param expression**: a string starting with ``=`` written over PARAMS
    names, e.g. ``"=CYLINDER_DIAMETER/2"``. It is bound as a FreeCAD
    expression on ``Params.<NAME>`` (the ``Params`` spreadsheet holds every
    parameter in the *storage unit* of its dimension, see
    :data:`STORAGE_UNIT`), so the property follows the parameter on recompute.

* **Errors.** Every failure the caller can fix raises :class:`ApiError` with a
  sentence a user can act on.
* **Undo.** Document changes run inside :func:`transaction`, named after the
  action, so one API call is one undo step. Nested calls share the outermost
  transaction.
* **No dialogs.** Nothing here (or in any ``api_*`` module) opens a dialog or
  depends on the GUI selection.

FreeCAD-side only.
"""

import contextlib
import functools
import math
import re

import FreeCAD

#: Bumped whenever a registered function's signature or result changes
#: incompatibly; the MCP server refuses a bridge with a different value.
API_VERSION = 1


class ApiError(Exception):
    """A user-readable error raised by the scripting API."""


# --------------------------------------------------------------------------- #
# Registry (the bridge's whitelist)
# --------------------------------------------------------------------------- #

_REGISTRY = {}


def api_function(category, name=None):
    """Register the decorated function as a public, bridge-callable API call.

    *category* groups functions for documentation (``"simulation"``,
    ``"geometry"``, ``"excitation"``, ``"monitor"``, ...). Registration is the
    *only* way a function becomes reachable over the Agent bridge.
    """

    def decorator(func):
        key = name or func.__name__
        if key in _REGISTRY and _REGISTRY[key][0] is not func:
            raise RuntimeError("API function {!r} registered twice".format(key))
        _REGISTRY[key] = (func, category)
        func.api_category = category
        return func

    return decorator


def registered_functions():
    """``{name: (function, category)}`` of every registered API function."""
    return dict(_REGISTRY)


# --------------------------------------------------------------------------- #
# Documents, simulation, objects
# --------------------------------------------------------------------------- #

def get_doc(doc=None):
    """Resolve *doc* (Document, document name/label, or None=active)."""
    if doc is None:
        doc = FreeCAD.ActiveDocument
        if doc is None:
            raise ApiError("No document is open. Open or create one first.")
        return doc
    if isinstance(doc, str):
        docs = FreeCAD.listDocuments()
        if doc in docs:
            return docs[doc]
        for candidate in docs.values():
            if candidate.Label == doc:
                return candidate
        raise ApiError("No open document named {!r}. Open documents: {}".format(
            doc, ", ".join(sorted(docs)) or "none"))
    if hasattr(doc, "Objects") and hasattr(doc, "recompute"):
        return doc
    if hasattr(doc, "Document"):
        return doc.Document
    raise ApiError("Expected a document, got {!r}".format(doc))


def get_sim(sim=None, required=True):
    """Resolve *sim* to the Simulation container.

    Accepts the container itself, any object inside its document, a Document,
    a document name, or None (active document). Raises :class:`ApiError` when
    the document has no simulation and *required* is true.
    """
    from wavesim_gui import commands

    if sim is not None and not isinstance(sim, str) \
            and getattr(sim, commands._TYPE_PROP, None) == commands._SIM_TYPE:
        return sim
    doc = get_doc(sim if sim is None or isinstance(sim, str)
                  or hasattr(sim, "Objects") else sim.Document)
    found = commands.active_simulation(doc)
    if found is None and required:
        raise ApiError(
            "Document {!r} has no Wavesim simulation. Create one with "
            "new_simulation first.".format(doc.Label))
    return found


def find_object(doc, ref, what="object"):
    """Return the object *ref* (object, internal Name, or unique Label)."""
    doc = get_doc(doc)
    if ref is None or ref == "":
        raise ApiError("No {} given.".format(what))
    if not isinstance(ref, str):
        if hasattr(ref, "Name") and hasattr(ref, "Document"):
            return ref
        raise ApiError("Expected the name or label of a {}, got {!r}".format(
            what, ref))
    obj = doc.getObject(ref)
    if obj is not None:
        return obj
    matches = doc.getObjectsByLabel(ref)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ApiError("Label {!r} is ambiguous ({} objects: {}). Use the "
                       "internal name instead.".format(
                           ref, len(matches),
                           ", ".join(o.Name for o in matches)))
    close = [o.Label for o in doc.Objects
             if ref.lower() in o.Label.lower() or ref.lower() in o.Name.lower()]
    hint = " Did you mean: {}?".format(", ".join(close[:5])) if close else ""
    raise ApiError("No {} named {!r} in document {!r}.{}".format(
        what, ref, doc.Label, hint))


def wavesim_type(obj):
    """The Wavesim type marker of *obj* (``WavesimType``/``Type``) or ''."""
    for prop in ("WavesimType", "Type"):
        try:
            if prop in obj.PropertiesList:
                value = getattr(obj, prop)
                if isinstance(value, str) and value:
                    return value
        except Exception:
            pass
    return ""


def check_new_label(doc, label, what="object"):
    """Raise unless *label* is a non-empty label no other object uses."""
    doc = get_doc(doc)
    if not isinstance(label, str) or not label.strip():
        raise ApiError("A {} needs a non-empty label.".format(what))
    label = label.strip()
    if doc.getObjectsByLabel(label):
        raise ApiError("Label {!r} is already used in document {!r}; choose a "
                       "unique label.".format(label, doc.Label))
    return label


def set_label(obj, label):
    """Set *label* on *obj* and fail if FreeCAD had to rename it."""
    obj.Label = label
    if obj.Label != label:
        raise ApiError("FreeCAD renamed {!r} to {!r} (label clash).".format(
            label, obj.Label))
    return obj


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #

_TX_DEPTH = {}


@contextlib.contextmanager
def transaction(doc, title):
    """One undoable step named *title*; aborted if the body raises.

    Re-entrant: an API function called from inside another one joins the
    outer transaction instead of committing half of it.
    """
    doc = get_doc(doc)
    key = doc.Name
    depth = _TX_DEPTH.get(key, 0)
    if depth == 0:
        doc.openTransaction(title)
    _TX_DEPTH[key] = depth + 1
    try:
        yield doc
    except BaseException:
        _TX_DEPTH[key] = depth
        if depth == 0:
            try:
                doc.abortTransaction()
            except Exception:
                pass
        raise
    _TX_DEPTH[key] = depth
    if depth == 0:
        doc.commitTransaction()


@contextlib.contextmanager
def capture_warnings():
    """Collect what the workbench prints as warnings/errors while running.

    Long operations (voxelization, results loading) report problems through
    ``FreeCAD.Console``; an API caller should get them back too. The messages
    still reach the report view. Yields the list being filled.
    """
    console = FreeCAD.Console
    collected = []
    originals = {}
    for name in ("PrintWarning", "PrintError"):
        original = getattr(console, name)
        originals[name] = original

        def wrapper(message, _orig=original):
            collected.append(str(message).strip())
            return _orig(message)
        try:
            setattr(console, name, wrapper)
        except Exception:
            originals.pop(name, None)
    try:
        yield collected
    finally:
        for name, original in originals.items():
            try:
                setattr(console, name, original)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Units and quantities
# --------------------------------------------------------------------------- #

#: Case-sensitive unit whitelist: unit -> (dimension, factor to SI).
#: Kept identical to the harness's ``units.py``.
UNITS = {
    "m": ("length", 1.0), "mm": ("length", 1e-3), "um": ("length", 1e-6),
    "nm": ("length", 1e-9), "cm": ("length", 1e-2),
    "Hz": ("frequency", 1.0), "kHz": ("frequency", 1e3),
    "MHz": ("frequency", 1e6), "GHz": ("frequency", 1e9),
    "THz": ("frequency", 1e12), "mHz": ("frequency", 1e-3),
    "s": ("time", 1.0), "ms": ("time", 1e-3), "us": ("time", 1e-6),
    "ns": ("time", 1e-9), "ps": ("time", 1e-12), "fs": ("time", 1e-15),
    "S/m": ("conductivity", 1.0),
    "ohm": ("resistance", 1.0), "kohm": ("resistance", 1e3),
    "Mohm": ("resistance", 1e6), "mohm": ("resistance", 1e-3),
    "F": ("capacitance", 1.0), "mF": ("capacitance", 1e-3),
    "uF": ("capacitance", 1e-6), "nF": ("capacitance", 1e-9),
    "pF": ("capacitance", 1e-12), "fF": ("capacitance", 1e-15),
    "H": ("inductance", 1.0), "mH": ("inductance", 1e-3),
    "uH": ("inductance", 1e-6), "nH": ("inductance", 1e-9),
    "pH": ("inductance", 1e-12),
    "V": ("voltage", 1.0), "mV": ("voltage", 1e-3), "kV": ("voltage", 1e3),
    "A": ("current", 1.0), "mA": ("current", 1e-3), "uA": ("current", 1e-6),
    "deg": ("angle", 1.0), "rad": ("angle", 180.0 / math.pi),
    "-": ("dimensionless", 1.0),
}

#: The unit a value of each dimension is stored in -- in FreeCAD properties and
#: in the ``Params`` spreadsheet. Angles are "SI" in degrees here (FreeCAD's
#: angle unit), so the ``rad`` factor above converts to degrees.
STORAGE_UNIT = {
    "length": "mm", "frequency": "Hz", "time": "s", "conductivity": "S/m",
    "resistance": "ohm", "capacitance": "F", "inductance": "H",
    "voltage": "V", "current": "A", "angle": "deg", "dimensionless": "-",
}

_QTY_RE = re.compile(
    r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*\[?\s*([A-Za-z/\-]*)\s*\]?\s*$")
_EXPR_ALLOWED = re.compile(r"^[A-Z0-9_+\-*/(). eE]+$")
_NAME_RE = re.compile(r"\b([A-Z][A-Z0-9_]*)\b")

PARAMS_SHEET = "Params"


def to_storage(number, unit, dimension=None):
    """Convert *number* in *unit* to the storage unit of its dimension."""
    if unit not in UNITS:
        raise ApiError("Unknown unit {!r}. Units are case-sensitive; allowed: "
                       "{}".format(unit, " ".join(UNITS)))
    dim, factor = UNITS[unit]
    if dimension is not None and dim != dimension:
        raise ApiError("Unit {!r} is a {} but a {} is expected here.".format(
            unit, dim, dimension))
    if dim == "length":
        return float(number) * factor * 1e3
    return float(number) * factor


def param_expression(text):
    """Turn ``"=A/2 + B"`` into the FreeCAD expression ``Params.A / 2 + Params.B``."""
    body = text[1:].strip() if text.startswith("=") else text.strip()
    if not body or not _EXPR_ALLOWED.match(body):
        raise ApiError(
            "Invalid param expression {!r}: only UPPER_SNAKE_CASE parameter "
            "names, numbers, + - * / and parentheses are allowed.".format(text))

    def repl(match):
        word = match.group(1)
        # Bare exponent markers (1e6) never start a word boundary before a digit.
        return "{}.{}".format(PARAMS_SHEET, word)

    return _NAME_RE.sub(repl, body)


def parse_quantity(value, dimension, default_unit=None):
    """Parse a quantity argument.

    Returns ``("value", number_in_storage_unit)`` or ``("expr", freecad_expr)``.
    """
    if default_unit is None:
        default_unit = STORAGE_UNIT[dimension]
    if isinstance(value, bool):
        raise ApiError("Expected a {} but got a boolean.".format(dimension))
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ApiError("{} must be finite.".format(dimension.capitalize()))
        return "value", to_storage(value, default_unit, dimension)
    if isinstance(value, dict):
        if "value" not in value:
            raise ApiError("Quantity dict needs 'value' (and 'unit').")
        return parse_quantity(value["value"] if "unit" not in value else
                              "{} {}".format(value["value"], value["unit"]),
                              dimension, default_unit)
    if hasattr(value, "Value") and hasattr(value, "Unit"):  # FreeCAD Quantity
        return "value", float(value.Value)
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("="):
            return "expr", param_expression(text)
        match = _QTY_RE.match(text)
        if not match:
            raise ApiError(
                "Cannot read {!r} as a {}. Write e.g. '3 mm', a plain number "
                "(in {}), or a param expression like '=SUBSTRATE_H/2'.".format(
                    value, dimension, default_unit))
        number, unit = match.groups()
        return "value", to_storage(float(number), unit or default_unit,
                                   dimension)
    raise ApiError("Cannot read {!r} as a {}.".format(value, dimension))


def quantity_value(value, dimension, default_unit=None, what="value"):
    """Like :func:`parse_quantity` but numbers only (no param expressions)."""
    kind, result = parse_quantity(value, dimension, default_unit)
    if kind == "expr":
        raise ApiError("{} cannot be bound to a parameter here; give a "
                       "number.".format(what))
    return result


def set_quantity(obj, prop, value, dimension, default_unit=None):
    """Set *obj.prop* from a quantity argument, binding expressions.

    *prop* may address a vector component as ``"Position.x"``. A plain value
    clears any expression previously bound to the property.
    """
    kind, result = parse_quantity(value, dimension, default_unit)
    base, _, comp = prop.partition(".")
    path = ".{}.{}".format(base, comp) if comp else base
    if kind == "expr":
        obj.setExpression(path, result)
        return result
    try:
        obj.setExpression(path, None)
    except Exception:
        pass
    if comp:
        vec = FreeCAD.Vector(getattr(obj, base))
        setattr(vec, comp, result)
        setattr(obj, base, vec)
    else:
        type_id = obj.getTypeIdOfProperty(base)
        if type_id in ("App::PropertyInteger", "App::PropertyIntegerConstraint"):
            setattr(obj, base, int(round(result)))
        else:
            setattr(obj, base, result)
    return result


def set_vector(obj, prop, xyz, dimension="length", default_unit=None):
    """Set a vector property from ``[x, y, z]`` quantity arguments."""
    if not isinstance(xyz, (list, tuple)) or len(xyz) != 3:
        raise ApiError("{} needs three coordinates [x, y, z].".format(prop))
    for comp, value in zip("xyz", xyz):
        set_quantity(obj, "{}.{}".format(prop, comp), value, dimension,
                     default_unit)


def vector_mm(xyz, what="point"):
    """``FreeCAD.Vector`` (mm) from ``[x, y, z]`` quantity arguments."""
    if not isinstance(xyz, (list, tuple)) or len(xyz) != 3:
        raise ApiError("{} needs three coordinates [x, y, z].".format(what))
    return FreeCAD.Vector(*[quantity_value(v, "length", what=what)
                            for v in xyz])


def enum_choice(value, choices, what):
    """Match *value* case-insensitively against *choices*; return the choice."""
    for choice in choices:
        if str(value).strip().lower() == str(choice).lower():
            return choice
    raise ApiError("Invalid {} {!r}; choose one of: {}".format(
        what, value, ", ".join(map(str, choices))))


AXES = {"x": FreeCAD.Vector(1, 0, 0), "y": FreeCAD.Vector(0, 1, 0),
        "z": FreeCAD.Vector(0, 0, 1)}


def axis_vector(value, what="axis"):
    """``'x'|'y'|'z'`` (optionally signed) or ``[x, y, z]`` -> unit Vector."""
    if isinstance(value, str):
        text = value.strip().lower()
        sign = -1.0 if text.startswith("-") else 1.0
        text = text.lstrip("+-")
        if text not in AXES:
            raise ApiError("Invalid {} {!r}; use x, y, z or [x, y, z].".format(
                what, value))
        return AXES[text] * sign
    if isinstance(value, (list, tuple)) and len(value) == 3:
        vec = FreeCAD.Vector(*map(float, value))
        if vec.Length == 0:
            raise ApiError("{} must not be the zero vector.".format(what))
        return vec.normalize()
    raise ApiError("Invalid {} {!r}.".format(what, value))


# --------------------------------------------------------------------------- #
# Property dimensions (for parameter binding checks)
# --------------------------------------------------------------------------- #

_PROP_DIMENSIONS = {}

_TYPE_DIMENSIONS = {
    "App::PropertyLength": "length",
    "App::PropertyDistance": "length",
    "App::PropertyVector": "length",
    "App::PropertyVectorDistance": "length",
    "App::PropertyPosition": "length",
    "App::PropertyAngle": "angle",
    "App::PropertyFrequency": "frequency",
    "App::PropertyElectricPotential": "voltage",
    "App::PropertyCurrentDensity": None,
}


def register_dimensions(mapping, wavesim_type_name=None):
    """Declare the physical dimension of plain-float properties.

    ``mapping`` is ``{property_name: dimension}``. With *wavesim_type_name*
    the entry applies only to objects of that Wavesim type; otherwise to any
    object with that property name.
    """
    for prop, dim in mapping.items():
        if dim not in STORAGE_UNIT:
            raise ValueError("unknown dimension {!r}".format(dim))
        _PROP_DIMENSIONS[(wavesim_type_name, prop)] = dim


def property_dimension(obj, prop):
    """Dimension of ``obj.prop`` (``"Position.x"`` allowed) or ``None``."""
    base = prop.split(".")[0]
    if base not in obj.PropertiesList:
        raise ApiError("{} has no property {!r}.".format(obj.Label, base))
    wtype = wavesim_type(obj)
    for key in ((wtype, base), (None, base)):
        if key in _PROP_DIMENSIONS:
            return _PROP_DIMENSIONS[key]
    type_id = obj.getTypeIdOfProperty(base)
    if type_id in _TYPE_DIMENSIONS:
        return _TYPE_DIMENSIONS[type_id]
    if type_id in ("App::PropertyFloat", "App::PropertyInteger",
                   "App::PropertyFloatConstraint",
                   "App::PropertyIntegerConstraint", "App::PropertyPercent"):
        return "dimensionless"
    return None


# Dimensions shared by several Wavesim objects (excitation rows carry numbered
# suffixes; :func:`property_dimension` strips nothing, so register them too).
register_dimensions({
    "MaxTime": "time", "MaxFrequency": "frequency",
    "Eps": "dimensionless", "Mu": "dimensionless", "Sigma": "conductivity",
})
for _suffix in ("",) + tuple(str(i) for i in range(2, 9)):
    register_dimensions({
        "Fmax" + _suffix: "frequency", "Frequency" + _suffix: "frequency",
        "StartTime" + _suffix: "time", "RiseTime" + _suffix: "time",
        "FlatTime" + _suffix: "time", "FallTime" + _suffix: "time",
        "PhaseDeg" + _suffix: "angle", "Amplitude" + _suffix: "dimensionless",
        "RampCycles" + _suffix: "dimensionless",
    })


# --------------------------------------------------------------------------- #
# JSON conversion
# --------------------------------------------------------------------------- #

def ref(obj):
    """Compact JSON reference to a document object."""
    if obj is None:
        return None
    return {"name": obj.Name, "label": obj.Label,
            "type": wavesim_type(obj) or obj.TypeId}


def to_jsonable(value):
    """Convert FreeCAD values to JSON-serializable data (lengths in mm)."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, FreeCAD.Vector):
        return [value.x, value.y, value.z]
    if isinstance(value, FreeCAD.Placement):
        return {"base": to_jsonable(value.Base),
                "axis": to_jsonable(value.Rotation.Axis),
                "angle_deg": math.degrees(value.Rotation.Angle)}
    if hasattr(value, "Value") and hasattr(value, "Unit"):
        return float(value.Value)
    if hasattr(value, "Name") and hasattr(value, "Document") \
            and hasattr(value, "TypeId"):
        return ref(value)
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if hasattr(value, "tolist"):
        return value.tolist()
    return str(value)


_SKIP_PROPS = {"Proxy", "ExpressionEngine", "Label2", "Visibility",
               "Shape", "Group", "_GroupTouched", "Content", "Placement"}


def describe(obj, include_hidden=False):
    """Structured description of *obj*: identity, properties, expressions."""
    props = {}
    for prop in obj.PropertiesList:
        if prop in _SKIP_PROPS or prop == "Label":
            continue
        if not include_hidden:
            try:
                if "Hidden" in obj.getEditorMode(prop):
                    continue
            except Exception:
                pass
        try:
            props[prop] = to_jsonable(getattr(obj, prop))
        except Exception:
            continue
    exprs = {}
    for entry in getattr(obj, "ExpressionEngine", None) or ():
        exprs[str(entry[0]).lstrip(".")] = str(entry[1])
    out = ref(obj)
    out["properties"] = props
    if exprs:
        out["expressions"] = exprs
    return out


# --------------------------------------------------------------------------- #
# Sub-shape selectors (never raw indices)
# --------------------------------------------------------------------------- #

def _bbox_on(bb, axis, value, tol):
    lo = getattr(bb, axis.upper() + "Min")
    hi = getattr(bb, axis.upper() + "Max")
    return abs(lo - value) <= tol and abs(hi - value) <= tol


def _edge_dir(edge):
    try:
        if edge.Curve.TypeId == "Part::GeomLine":
            vec = edge.Vertexes[-1].Point - edge.Vertexes[0].Point
            return vec.normalize() if vec.Length > 0 else None
    except Exception:
        pass
    return None


def _face_normal(face):
    try:
        u0, u1, v0, v1 = face.ParameterRange
        return face.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
    except Exception:
        return None


def select_subshapes(shape, selector, kind="edge"):
    """Pick edges/faces of *shape* with a declarative *selector*.

    Returns ``[(sub_name, subshape), ...]`` (``"Edge3"``, ``"Face1"``).
    Selector keys (all given keys must match -- logical AND):

    * ``{"type": "all"}`` -- everything;
    * ``"parallel_to": "x"|"y"|"z"`` -- straight edges along the axis, or
      planar faces whose normal is along it;
    * ``"normal": "+z"|"-x"|...`` -- faces only, signed outward normal;
    * ``"at_x"/"at_y"/"at_z": value`` -- the sub-shape lies entirely in that
      coordinate plane (value is a length, mm by default);
    * ``"near_point": [x, y, z]`` -- the single sub-shape closest to a point;
    * ``"tolerance": length`` -- matching tolerance (default 1e-4 mm).
    """
    if not isinstance(selector, dict) or not selector:
        raise ApiError(
            "A selector is a dict such as {'type': 'all'}, {'parallel_to': "
            "'z'}, {'at_z': 1.6} or {'near_point': [x, y, z]}.")
    allowed = {"type", "parallel_to", "normal", "at_x", "at_y", "at_z",
               "near_point", "tolerance"}
    unknown = set(selector) - allowed
    if unknown:
        raise ApiError("Unknown selector key(s): {}. Allowed: {}".format(
            ", ".join(sorted(unknown)), ", ".join(sorted(allowed))))
    if selector.get("type", "all") != "all":
        raise ApiError("Selector 'type' only supports 'all'.")
    tol = quantity_value(selector.get("tolerance", 1e-4), "length")
    subs = shape.Edges if kind == "edge" else shape.Faces
    prefix = "Edge" if kind == "edge" else "Face"
    picked = [("{}{}".format(prefix, i + 1), s) for i, s in enumerate(subs)]

    if "parallel_to" in selector:
        axis = axis_vector(selector["parallel_to"], "parallel_to")
        out = []
        for name, sub in picked:
            vec = _edge_dir(sub) if kind == "edge" else _face_normal(sub)
            if vec is not None and abs(abs(vec.dot(axis)) - 1.0) < 1e-6:
                out.append((name, sub))
        picked = out
    if "normal" in selector:
        if kind != "face":
            raise ApiError("'normal' only applies to faces.")
        axis = axis_vector(selector["normal"], "normal")
        out = []
        for name, sub in picked:
            vec = _face_normal(sub)
            if vec is not None and vec.dot(axis) > 1 - 1e-6:
                out.append((name, sub))
        picked = out
    for axis in "xyz":
        key = "at_" + axis
        if key in selector:
            value = quantity_value(selector[key], "length", what=key)
            picked = [(n, s) for n, s in picked
                      if _bbox_on(s.BoundBox, axis, value, tol)]
    if "near_point" in selector:
        point = vector_mm(selector["near_point"], "near_point")
        if picked:
            import Part
            vertex = Part.Vertex(point)
            best = min(picked, key=lambda ns: ns[1].distToShape(vertex)[0])
            picked = [best]
    return picked
