# -*- coding: utf-8 -*-
"""Scripting API: Part-based geometry (primitives, booleans, transforms, fillets, STEP import, measurement).

Everything here builds *parametric* Part workbench features (``Part::Box``,
``Part::Cylinder``, ``Part::Extrusion``, ``Part::Cut``, ``Part::Fillet``, Draft
arrays, ...) so a model stays editable in the GUI and follows the ``Params``
spreadsheet: every dimension accepts a param expression such as
``"=CYL_D/2"``, which is bound as a FreeCAD expression on the property (or on
the Placement component, e.g. ``.Placement.Base.x``).

The workbench has no GUI commands of its own for geometry (users use the Part
workbench), so there is no GUI path to share; the objects created are exactly
those the Part / Draft GUI commands create, including hiding the inputs of a
boolean, mirror, fillet or array.

Every created object needs a unique label. A feature that fails to compute
(e.g. a fillet radius too large for the edge) raises :class:`ApiError` and
leaves the document as it was. See :mod:`wavesim_gui.api_common` for the
general conventions.
"""

import contextlib
import math
import os
import re

import FreeCAD

from wavesim_gui import api_common as c
from wavesim_gui.api_common import ApiError, api_function

__all__ = [
    "create_box", "create_cylinder", "create_sphere", "create_cone",
    "create_torus", "create_wedge", "create_extruded_polygon", "boolean",
    "transform", "mirror", "linear_array", "polar_array", "fillet",
    "chamfer", "import_step", "measure", "check_geometry", "delete_object",
    "rename_object", "list_objects",
]

_L = "length"
_A = "angle"

_PLANES = {
    # plane -> (index of u, index of v, normal axis letter)
    "xy": (0, 1, "z"),
    "xz": (0, 2, "y"),
    "yz": (1, 2, "x"),
}
_MIRROR_PLANES = {"xy": "z", "yz": "x", "xz": "y"}


# --------------------------------------------------------------------------- #
# Private helpers
# --------------------------------------------------------------------------- #

def _is_wavesim(obj):
    return "WavesimType" in obj.PropertiesList or bool(c.wavesim_type(obj))


def _sim_members(doc):
    """Objects inside the Simulation container tree (groups included)."""
    from wavesim_gui import commands
    sim = commands.active_simulation(doc)
    out = set()
    if sim is None:
        return out
    stack = [sim]
    while stack:
        obj = stack.pop()
        if obj.Name in out:
            continue
        out.add(obj.Name)
        stack.extend(getattr(obj, "Group", None) or ())
    return out


def _has_shape(obj):
    try:
        return obj.isDerivedFrom("Part::Feature")
    except Exception:
        return False


def _geometry(doc, ref, what="object"):
    """Resolve *ref* to a Part geometry object (not a Wavesim object)."""
    obj = c.find_object(doc, ref, what)
    if _is_wavesim(obj) or obj.Name in _sim_members(obj.Document):
        raise ApiError(
            "{!r} is a Wavesim simulation object, not geometry; use the "
            "Wavesim setup functions for it.".format(obj.Label))
    if not _has_shape(obj):
        raise ApiError("{!r} ({}) is not a Part shape object.".format(
            obj.Label, obj.TypeId))
    return obj


def _solid_input(doc, ref, what):
    obj = _geometry(doc, ref, what)
    shape = obj.Shape
    if shape.isNull():
        raise ApiError("{!r} has an empty shape; recompute or fix it "
                       "first.".format(obj.Label))
    return obj


def _name_hint(label):
    name = re.sub(r"[^A-Za-z0-9_]", "_", label)
    if not name or name[0].isdigit():
        name = "_" + name
    return name


def _vec3(value, what):
    if value is None:
        value = [0, 0, 0]
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ApiError("{} needs three coordinates [x, y, z].".format(what))
    return list(value)


def _set_qty(obj, prop, value, dim=_L, what=None, minimum=None,
             strict=True):
    """``c.set_quantity`` plus a range check on plain numbers.

    *minimum* with *strict* means ``value > minimum``; otherwise ``>=``.
    Expressions are checked after recompute (the shape check catches them).
    """
    what = what or prop
    kind, result = c.parse_quantity(value, dim)
    if kind == "value" and minimum is not None:
        bad = result <= minimum if strict else result < minimum
        if bad:
            raise ApiError("{} must be {} {} {} (got {:g}).".format(
                what, ">" if strict else ">=", minimum, c.STORAGE_UNIT[dim],
                result))
    try:
        return c.set_quantity(obj, prop, value, dim)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError("Cannot set {} to {!r}: {}. A param expression needs "
                       "the alias to exist in the Params spreadsheet.".format(
                           what, value, exc))


def _set_expr(obj, path, expr, what):
    try:
        obj.setExpression(path, expr)
    except Exception as exc:
        raise ApiError("Cannot bind {} to {!r}: {}. A param expression needs "
                       "the alias to exist in the Params spreadsheet.".format(
                           what, expr, exc))


def _eval_snapshot(obj, value, dim, what):
    """Number for *value*; a param expression is evaluated once (not bound)."""
    kind, result = c.parse_quantity(value, dim)
    if kind == "value":
        return result
    try:
        out = obj.evalExpression(result)
    except Exception as exc:
        raise ApiError("Cannot evaluate {} {!r}: {}".format(what, value, exc))
    return float(getattr(out, "Value", out))


def _set_placement(obj, position=None, axis=None, angle=0,
                   rotation_from_z=None):
    """Set ``obj.Placement`` from quantity arguments, binding expressions.

    *position* ``[x, y, z]`` (mm). Rotation either *rotation_from_z* (a
    Vector: rotate the local Z axis onto it -- for axisymmetric primitives)
    or *axis* + *angle* (deg, may be a param expression).
    """
    position = _vec3(position, "position")
    base = FreeCAD.Vector()
    exprs = []
    for comp, value in zip("xyz", position):
        kind, result = c.parse_quantity(value, _L)
        if kind == "expr":
            exprs.append((".Placement.Base." + comp, result,
                          "position " + comp))
        else:
            setattr(base, comp, result)
    if rotation_from_z is not None:
        rot = FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), rotation_from_z)
    else:
        axis_vec = c.axis_vector(axis if axis is not None else "z",
                                 "rotation axis")
        kind, ang = c.parse_quantity(angle if angle is not None else 0, _A)
        if kind == "expr":
            # Pin the axis with constant expressions: FreeCAD may otherwise
            # reset it to +Z whenever the bound angle evaluates to 0.
            for comp in "xyz":
                exprs.append((".Placement.Rotation.Axis." + comp,
                              repr(float(getattr(axis_vec, comp))),
                              "rotation axis"))
            exprs.append((".Placement.Rotation.Angle", ang,
                          "rotation angle"))
            ang = 0.0
        rot = FreeCAD.Rotation(axis_vec, ang)
    obj.Placement = FreeCAD.Placement(base, rot)
    for path, expr, what in exprs:
        _set_expr(obj, path, expr, what)


def _rotation_args(rotation):
    """``None | {"axis": ..., "angle": ...}`` -> (axis, angle)."""
    if rotation is None:
        return "z", 0
    if not isinstance(rotation, dict) or "angle" not in rotation:
        raise ApiError("rotation must be a dict {'axis': 'z' or [x, y, z], "
                       "'angle': degrees}.")
    unknown = set(rotation) - {"axis", "angle"}
    if unknown:
        raise ApiError("Unknown rotation key(s): {}".format(
            ", ".join(sorted(unknown))))
    return rotation.get("axis", "z"), rotation["angle"]


def _shape_problem(obj):
    """A sentence describing why *obj* did not compute, or ''."""
    try:
        state = list(obj.State)
    except Exception:
        state = []
    if "Invalid" in state or "Error" in state:
        try:
            msg = obj.getStatusString()
        except Exception:
            msg = ""
        return msg or "recompute failed"
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        return "the result is an empty shape"
    if not shape.isValid():
        return "the result is not a valid shape"
    return ""


class _Build(object):
    """Tracks objects created/hidden by one API call so a failure undoes them.

    FreeCAD's undo is off in headless documents (``UndoMode == 0``), so an
    aborted transaction alone would leave half-built features behind.
    """

    def __init__(self, doc):
        self.doc = doc
        self.created = []
        self.hidden = []

    def add(self, type_id, label):
        obj = self.doc.addObject(type_id, _name_hint(label))
        self.created.append(obj.Name)
        c.set_label(obj, label)
        return obj

    def track(self, obj):
        self.created.append(obj.Name)
        return obj

    def hide(self, obj):
        if obj.Visibility:
            self.hidden.append(obj.Name)
        _set_visible(obj, False)

    def finish(self, *objs, hint=""):
        self.doc.recompute()
        for obj in objs:
            problem = _shape_problem(obj)
            if problem:
                raise ApiError("{!r} ({}) failed to compute: {}.{}".format(
                    obj.Label, obj.TypeId, problem,
                    " " + hint if hint else ""))

    def rollback(self):
        for name in reversed(self.created):
            if self.doc.getObject(name) is not None:
                try:
                    self.doc.removeObject(name)
                except Exception:
                    pass
        for name in self.hidden:
            obj = self.doc.getObject(name)
            if obj is not None:
                _set_visible(obj, True)
        try:
            self.doc.recompute()
        except Exception:
            pass


@contextlib.contextmanager
def _building(doc, title):
    build = _Build(doc)
    try:
        with c.transaction(doc, title):
            yield build
    except BaseException:
        build.rollback()
        raise


def _set_visible(obj, visible):
    obj.Visibility = visible
    vobj = getattr(obj, "ViewObject", None)
    if vobj is not None:
        try:
            vobj.Visibility = visible
        except Exception:
            pass


def _link_targets(obj):
    """Objects referenced by *obj*'s link properties (not expressions)."""
    out = []
    for prop in obj.PropertiesList:
        try:
            type_id = obj.getTypeIdOfProperty(prop)
        except Exception:
            continue
        if not type_id.startswith("App::PropertyLink") \
                and type_id != "App::PropertyXLink":
            continue
        value = getattr(obj, prop, None)
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            if isinstance(item, tuple) and item:
                item = item[0]
            if hasattr(item, "Name") and hasattr(item, "Document"):
                out.append(item)
    return out


def _consumers(obj):
    """Geometry features that use *obj*'s shape as an input."""
    out = []
    for parent in obj.InList:
        if parent is obj or _is_wavesim(parent) or not _has_shape(parent):
            continue
        if any(t is obj for t in _link_targets(parent)):
            out.append(parent)
    return out


def _check_new_labels(doc, *labels):
    out = [c.check_new_label(doc, label) for label in labels]
    if len(set(out)) != len(out):
        raise ApiError("Labels must be distinct: {}".format(", ".join(out)))
    return out


def _bbox_dict(bb):
    return {"min": [bb.XMin, bb.YMin, bb.ZMin],
            "max": [bb.XMax, bb.YMax, bb.ZMax],
            "size": [bb.XLength, bb.YLength, bb.ZLength]}


def _center_of_mass(shape):
    try:
        if shape.ShapeType == "Solid":
            return shape.CenterOfMass
        solids = shape.Solids
        if solids:
            total = sum(s.Volume for s in solids)
            if total > 0:
                acc = FreeCAD.Vector()
                for s in solids:
                    acc += s.CenterOfMass * s.Volume
                return acc * (1.0 / total)
        return shape.CenterOfGravity
    except Exception:
        return shape.BoundBox.Center


def _int_count(obj, prop, value, what, minimum):
    kind, result = c.parse_quantity(value, "dimensionless")
    if kind == "value":
        if abs(result - round(result)) > 1e-9 or result < minimum:
            raise ApiError("{} must be an integer >= {} (got {!r}).".format(
                what, minimum, value))
    c.set_quantity(obj, prop, value, "dimensionless")


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #

@api_function("geometry")
def create_box(doc=None, *, label, length, width, height, position=None,
               rotation=None):
    """Create a parametric box (``Part::Box``) and return it.

    Use for substrates, ground planes, blocks, PCB traces. The box spans
    ``[0, length] x [0, width] x [0, height]`` in its local frame, whose
    origin is *position*.

    :param label: unique label (required).
    :param length: size along local X (mm, or "3 mm", or "=PARAM").
    :param width: size along local Y (mm / quantity / param expression).
    :param height: size along local Z (mm / quantity / param expression).
    :param position: ``[x, y, z]`` of the local origin (mm, each component
        may be a param expression bound to ``.Placement.Base.x`` ...);
        default ``[0, 0, 0]``.
    :param rotation: optional ``{"axis": "z" | [x, y, z], "angle": deg}``
        about *position*; the angle may be a param expression.
    :returns: the ``Part::Box`` object.

    Example: ``create_box(label="Substrate", length="=SUB_L", width=40,
    height="1.6 mm", position=["=-SUB_L/2", -20, 0])``
    """
    doc = c.get_doc(doc)
    label = c.check_new_label(doc, label)
    axis, angle = _rotation_args(rotation)
    with _building(doc, "Wavesim: Create Box") as b:
        obj = b.add("Part::Box", label)
        _set_qty(obj, "Length", length, what="length", minimum=0)
        _set_qty(obj, "Width", width, what="width", minimum=0)
        _set_qty(obj, "Height", height, what="height", minimum=0)
        _set_placement(obj, position, axis=axis, angle=angle)
        b.finish(obj, hint="Check that all sizes are positive.")
    return obj


def _set_radius_or_diameter(obj, radius, diameter):
    if (radius is None) == (diameter is None):
        raise ApiError("Give exactly one of radius or diameter.")
    if radius is not None:
        _set_qty(obj, "Radius", radius, what="radius", minimum=0)
        return
    kind, result = c.parse_quantity(diameter, _L)
    if kind == "expr":
        _set_expr(obj, "Radius", "({}) / 2".format(result), "diameter")
    else:
        if result <= 0:
            raise ApiError("diameter must be > 0 mm (got {:g}).".format(
                result))
        obj.Radius = result / 2.0


@api_function("geometry")
def create_cylinder(doc=None, *, label, radius=None, diameter=None, height,
                    position=None, axis="z", angle=360):
    """Create a parametric cylinder (``Part::Cylinder``) and return it.

    Use for coax conductors, vias, rods, wires. The cylinder starts at
    *position* (centre of its base disc) and extends *height* along *axis*.

    :param label: unique label (required).
    :param radius: radius (mm / quantity / "=PARAM"); or give *diameter*
        (a diameter param expression is bound as ``(expr) / 2``).
    :param height: length along the axis (mm / quantity / "=PARAM").
    :param position: ``[x, y, z]`` base centre (mm, components bindable).
    :param axis: ``"x"|"y"|"z"`` (optionally ``"-z"``) or a vector.
    :param angle: sweep angle in degrees (360 = full cylinder; bindable).
    :returns: the ``Part::Cylinder`` object.

    Example: ``create_cylinder(label="Inner", diameter="=D_IN", height=20,
    axis="x")``
    """
    doc = c.get_doc(doc)
    label = c.check_new_label(doc, label)
    direction = c.axis_vector(axis)
    with _building(doc, "Wavesim: Create Cylinder") as b:
        obj = b.add("Part::Cylinder", label)
        _set_radius_or_diameter(obj, radius, diameter)
        _set_qty(obj, "Height", height, what="height", minimum=0)
        _set_qty(obj, "Angle", angle, _A, what="angle", minimum=0)
        _set_placement(obj, position, rotation_from_z=direction)
        b.finish(obj, hint="Check radius, height and angle (0 < angle <= "
                           "360).")
    return obj


@api_function("geometry")
def create_sphere(doc=None, *, label, radius, position=None):
    """Create a parametric sphere (``Part::Sphere``) centred at *position*.

    :param label: unique label (required).
    :param radius: radius (mm / quantity / "=PARAM").
    :param position: ``[x, y, z]`` centre (mm, components bindable).
    :returns: the ``Part::Sphere`` object.

    Example: ``create_sphere(label="Ball", radius="=R", position=[0, 0, 5])``
    """
    doc = c.get_doc(doc)
    label = c.check_new_label(doc, label)
    with _building(doc, "Wavesim: Create Sphere") as b:
        obj = b.add("Part::Sphere", label)
        _set_qty(obj, "Radius", radius, what="radius", minimum=0)
        _set_placement(obj, position)
        b.finish(obj)
    return obj


@api_function("geometry")
def create_cone(doc=None, *, label, radius1, radius2, height, position=None,
                axis="z", angle=360):
    """Create a parametric cone / frustum (``Part::Cone``) and return it.

    Use for tapers and horn transitions. The base disc (*radius1*) is centred
    at *position*; the top disc (*radius2*) lies *height* along *axis*.

    :param label: unique label (required).
    :param radius1: base radius (mm / quantity / "=PARAM", >= 0).
    :param radius2: top radius (>= 0; not both radii 0).
    :param height: height along the axis (mm, > 0).
    :param position: ``[x, y, z]`` base centre (mm, components bindable).
    :param axis: ``"x"|"y"|"z"`` (optionally signed) or a vector.
    :param angle: sweep angle in degrees (default 360, bindable).
    :returns: the ``Part::Cone`` object.

    Example: ``create_cone(label="Taper", radius1=2, radius2=0.5, height=5)``
    """
    doc = c.get_doc(doc)
    label = c.check_new_label(doc, label)
    direction = c.axis_vector(axis)
    k1, r1 = c.parse_quantity(radius1, _L)
    k2, r2 = c.parse_quantity(radius2, _L)
    if k1 == k2 == "value" and r1 == 0 and r2 == 0:
        raise ApiError("A cone needs at least one non-zero radius.")
    with _building(doc, "Wavesim: Create Cone") as b:
        obj = b.add("Part::Cone", label)
        _set_qty(obj, "Radius1", radius1, what="radius1", minimum=0,
                 strict=False)
        _set_qty(obj, "Radius2", radius2, what="radius2", minimum=0,
                 strict=False)
        _set_qty(obj, "Height", height, what="height", minimum=0)
        _set_qty(obj, "Angle", angle, _A, what="angle", minimum=0)
        _set_placement(obj, position, rotation_from_z=direction)
        b.finish(obj, hint="Radii must not both be 0 and must differ from "
                           "each other or use a cylinder.")
    return obj


@api_function("geometry")
def create_torus(doc=None, *, label, radius1, radius2, position=None,
                 axis="z"):
    """Create a parametric torus (``Part::Torus``) and return it.

    Use for loops and rings. The ring lies in the plane normal to *axis*
    through *position*.

    :param label: unique label (required).
    :param radius1: major radius, centre to tube centre (mm, bindable).
    :param radius2: tube (minor) radius (mm, bindable; must be < radius1).
    :param position: ``[x, y, z]`` centre (mm, components bindable).
    :param axis: ring axis ``"x"|"y"|"z"`` or a vector.
    :returns: the ``Part::Torus`` object.

    Example: ``create_torus(label="Loop", radius1=10, radius2=0.5)``
    """
    doc = c.get_doc(doc)
    label = c.check_new_label(doc, label)
    direction = c.axis_vector(axis)
    k1, r1 = c.parse_quantity(radius1, _L)
    k2, r2 = c.parse_quantity(radius2, _L)
    if k1 == k2 == "value" and r2 >= r1:
        raise ApiError("radius2 (tube) must be smaller than radius1.")
    with _building(doc, "Wavesim: Create Torus") as b:
        obj = b.add("Part::Torus", label)
        _set_qty(obj, "Radius1", radius1, what="radius1", minimum=0)
        _set_qty(obj, "Radius2", radius2, what="radius2", minimum=0)
        _set_placement(obj, position, rotation_from_z=direction)
        b.finish(obj, hint="radius2 must be smaller than radius1.")
    return obj


@api_function("geometry")
def create_wedge(doc=None, *, label, xmin=0, ymin=0, zmin=0, x2min=2,
                 z2min=2, xmax=10, ymax=10, zmax=10, x2max=8, z2max=8,
                 position=None, rotation=None):
    """Create a parametric wedge (``Part::Wedge``) and return it.

    Use for ramps, tapers and trapezoidal prisms. The base face at
    ``y = ymin`` spans ``[xmin, xmax] x [zmin, zmax]``; the top face at
    ``y = ymax`` spans ``[x2min, x2max] x [z2min, z2max]`` (local frame).
    Defaults equal FreeCAD's *Part -> Primitives -> Wedge*.

    :param label: unique label (required).
    :param xmin..z2max: the ten Part::Wedge bounds (mm / quantity /
        "=PARAM"); need ``xmin < xmax``, ``ymin < ymax``, ``zmin < zmax``,
        ``x2min <= x2max``, ``z2min <= z2max``.
    :param position: ``[x, y, z]`` of the local origin (mm, bindable).
    :param rotation: optional ``{"axis": ..., "angle": deg}``.
    :returns: the ``Part::Wedge`` object.

    Example: ``create_wedge(label="Ramp", x2min=0, x2max=10, z2min=0,
    z2max=0)`` (a triangular prism).
    """
    doc = c.get_doc(doc)
    label = c.check_new_label(doc, label)
    axis, angle = _rotation_args(rotation)
    values = {"Xmin": xmin, "Ymin": ymin, "Zmin": zmin, "X2min": x2min,
              "Z2min": z2min, "Xmax": xmax, "Ymax": ymax, "Zmax": zmax,
              "X2max": x2max, "Z2max": z2max}
    nums = {}
    for prop, value in values.items():
        kind, result = c.parse_quantity(value, _L)
        if kind == "value":
            nums[prop] = result
    for lo, hi, strict in (("Xmin", "Xmax", True), ("Ymin", "Ymax", True),
                           ("Zmin", "Zmax", True), ("X2min", "X2max", False),
                           ("Z2min", "Z2max", False)):
        if lo in nums and hi in nums and (
                nums[lo] >= nums[hi] if strict else nums[lo] > nums[hi]):
            raise ApiError("Wedge needs {} {} {}.".format(
                lo.lower(), "<" if strict else "<=", hi.lower()))
    with _building(doc, "Wavesim: Create Wedge") as b:
        obj = b.add("Part::Wedge", label)
        for prop, value in values.items():
            _set_qty(obj, prop, value, what=prop.lower())
        _set_placement(obj, position, axis=axis, angle=angle)
        b.finish(obj)
    return obj


@api_function("geometry")
def create_extruded_polygon(doc=None, *, label, points, height, plane="xy",
                            offset=0):
    """Extrude a closed polygon along its plane normal and return the solid.

    Use for arbitrary prismatic shapes: patch antennas, tapered traces,
    polygonal pads. Creates a hidden ``Part::Polygon`` profile (label
    ``"<label> profile"``) and a ``Part::Extrusion`` (*label*, Solid=True).

    :param label: unique label (required).
    :param points: ``[[u, v], ...]`` at least 3 vertices (mm or "3 mm"
        quantities -- *not* param expressions) in the plane's 2D coordinates:
        ``xy`` -> (x, y), ``xz`` -> (x, z), ``yz`` -> (y, z). The polygon is
        closed automatically and must not self-intersect.
    :param height: extrusion length along the normal (+z for ``xy``, +y for
        ``xz``, +x for ``yz``); mm / quantity / "=PARAM", > 0.
    :param plane: ``"xy"`` (default), ``"xz"`` or ``"yz"``.
    :param offset: plane position along its normal (mm, bindable), e.g. the
        substrate top ``"=SUB_H"``.
    :returns: the ``Part::Extrusion`` object.

    Example: ``create_extruded_polygon(label="Patch", points=[[0,0],[10,0],
    [10,8],[0,8]], height=0.035, offset="=SUB_H")``
    """
    doc = c.get_doc(doc)
    label, profile_label = _check_new_labels(doc, label, "{} profile".format(
        str(label).strip()))
    key = c.enum_choice(plane, list(_PLANES), "plane")
    iu, iv, normal = _PLANES[key]
    if not isinstance(points, (list, tuple)) or len(points) < 3:
        raise ApiError("points needs at least 3 [u, v] vertices.")
    uv = []
    for i, pt in enumerate(points):
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise ApiError("Point {} must be [u, v], got {!r}.".format(i, pt))
        uv.append([c.quantity_value(v, _L, what="polygon point")
                   for v in pt])
    if len(uv) > 3 and all(abs(a - b) < 1e-9 for a, b in zip(uv[0], uv[-1])):
        uv = uv[:-1]
    area = 0.5 * sum(uv[i][0] * uv[(i + 1) % len(uv)][1]
                     - uv[(i + 1) % len(uv)][0] * uv[i][1]
                     for i in range(len(uv)))
    if abs(area) < 1e-9:
        raise ApiError("The polygon has zero area (collinear or repeated "
                       "points).")
    nodes = []
    for u, v in uv:
        xyz = [0.0, 0.0, 0.0]
        xyz[iu], xyz[iv] = u, v
        nodes.append(FreeCAD.Vector(*xyz))
    with _building(doc, "Wavesim: Create Extruded Polygon") as b:
        profile = b.add("Part::Polygon", profile_label)
        profile.Nodes = nodes
        profile.Close = True
        _set_offset(profile, normal, offset)
        ext = b.add("Part::Extrusion", label)
        ext.Base = profile
        ext.DirMode = "Custom"
        ext.Dir = c.AXES[normal]
        ext.Solid = True
        _set_qty(ext, "LengthFwd", height, what="height", minimum=0)
        b.hide(profile)
        b.finish(profile, ext, hint="Make sure the polygon does not "
                                    "self-intersect and height > 0.")
        if not ext.Shape.Solids:
            raise ApiError("The extrusion of {!r} is not a solid; check that "
                           "the polygon does not self-intersect.".format(
                               label))
    return ext


def _set_offset(obj, axis_letter, offset):
    kind, result = c.parse_quantity(offset, _L)
    path = ".Placement.Base." + axis_letter
    if kind == "expr":
        _set_expr(obj, path, result, "offset")
    else:
        pl = FreeCAD.Placement(obj.Placement)
        setattr(pl.Base, axis_letter, result)
        obj.Placement = pl


# --------------------------------------------------------------------------- #
# Booleans, transforms, patterns
# --------------------------------------------------------------------------- #

@api_function("geometry")
def boolean(doc=None, *, label, op="fuse", base, tools):
    """Combine solids with a parametric boolean and return the result.

    Use to drill holes (cut), merge conductors (fuse) or intersect (common).
    Like the Part workbench, the inputs are hidden; they stay in the
    document and the result follows any later change to them.

    :param label: unique label of the result (required).
    :param op: ``"fuse"`` (``Part::MultiFuse``), ``"cut"`` (``Part::Cut``;
        several tools are first fused into a hidden ``"<label> tools"``
        ``Part::MultiFuse``) or ``"common"`` (``Part::MultiCommon``).
    :param base: the object to start from (name or label).
    :param tools: list of objects (names/labels), at least one.
    :returns: the boolean feature.

    Example: coax dielectric ``boolean(label="Dielectric", op="cut",
    base="Outer", tools=["Inner"])``
    """
    doc = c.get_doc(doc)
    label = c.check_new_label(doc, label)
    op_key = c.enum_choice(op, ["fuse", "cut", "common"], "boolean op")
    base_obj = _solid_input(doc, base, "base object")
    if isinstance(tools, (str, bytes)) or hasattr(tools, "Name"):
        tools = [tools]
    if not tools:
        raise ApiError("boolean needs at least one tool object.")
    tool_objs = [_solid_input(doc, t, "tool object") for t in tools]
    names = [base_obj.Name] + [t.Name for t in tool_objs]
    if len(set(names)) != len(names):
        raise ApiError("base and tools must be different objects.")
    tools_label = None
    if op_key == "cut" and len(tool_objs) > 1:
        tools_label = c.check_new_label(doc, "{} tools".format(label))
    type_ids = {"fuse": "Part::MultiFuse", "cut": "Part::Cut",
                "common": "Part::MultiCommon"}
    with _building(doc, "Wavesim: Boolean {}".format(op_key.capitalize())) \
            as b:
        obj = b.add(type_ids[op_key], label)
        if op_key == "cut":
            tool = tool_objs[0]
            if tools_label:
                tool = b.add("Part::MultiFuse", tools_label)
                tool.Shapes = tool_objs
                for t in tool_objs:
                    b.hide(t)
            obj.Base = base_obj
            obj.Tool = tool
            b.hide(tool)
        else:
            obj.Shapes = [base_obj] + tool_objs
            for t in tool_objs:
                b.hide(t)
        b.hide(base_obj)
        b.finish(obj, hint="Check that the inputs are valid solids.")
    return obj


@api_function("geometry")
def transform(doc=None, *, obj, translate=None, rotate_axis="z", angle=0,
              center=None):
    """Move and/or rotate an object relative to its current placement.

    Use to reposition existing geometry (also imported STEP parts). The
    rotation (if *angle* != 0) is applied first, about *center*, then the
    translation. One undo step. Objects whose Placement is bound to
    parameters are refused -- change the parameters instead.

    :param obj: the object (name or label).
    :param translate: ``[dx, dy, dz]`` (mm / quantities; no param
        expressions -- this is a relative, one-off change).
    :param rotate_axis: ``"x"|"y"|"z"`` or ``[x, y, z]``.
    :param angle: rotation angle in degrees (number or "90 deg"/"1.57 rad").
    :param center: ``[x, y, z]`` rotation centre (mm); default the centre of
        the object's bounding box.
    :returns: the modified object.

    Example: ``transform(obj="Patch", translate=[0, 0, 1.6])``
    """
    doc = c.get_doc(doc)
    target = _geometry(doc, obj)
    bound = [str(e[0]) for e in (target.ExpressionEngine or ())
             if str(e[0]).lstrip(".").startswith("Placement")]
    if bound:
        raise ApiError("{!r} has its Placement bound to parameters ({}); "
                       "change those parameters instead.".format(
                           target.Label, ", ".join(bound)))
    move = c.vector_mm(_vec3(translate, "translate"), "translate")
    ang = c.quantity_value(angle, _A, what="angle")
    axis_vec = c.axis_vector(rotate_axis, "rotate_axis")
    if center is None:
        shape = target.Shape
        pivot = shape.BoundBox.Center if not shape.isNull() \
            else target.Placement.Base
    else:
        pivot = c.vector_mm(center, "center")
    rot = FreeCAD.Placement(FreeCAD.Vector(), FreeCAD.Rotation(axis_vec, ang),
                            pivot)
    with c.transaction(doc, "Wavesim: Transform"):
        target.Placement = FreeCAD.Placement(move, FreeCAD.Rotation()) \
            .multiply(rot).multiply(target.Placement)
        doc.recompute()
    return target


@api_function("geometry")
def mirror(doc=None, *, label, obj, plane="xy", base_point=None):
    """Create a parametric mirrored copy (``Part::Mirroring``) of *obj*.

    Use for symmetric structures (dipole arms, differential pairs). The
    source is hidden as the Part GUI does.

    :param label: unique label of the copy (required).
    :param obj: source object (name or label).
    :param plane: mirror plane ``"xy"|"yz"|"xz"`` (through *base_point*) or
        the plane normal ``[x, y, z]``.
    :param base_point: ``[x, y, z]`` point on the plane (mm, components
        bindable as param expressions); default origin.
    :returns: the ``Part::Mirroring`` object.

    Example: ``mirror(label="Arm2", obj="Arm1", plane="yz")``
    """
    doc = c.get_doc(doc)
    label = c.check_new_label(doc, label)
    source = _solid_input(doc, obj, "source object")
    if isinstance(plane, str):
        key = c.enum_choice(plane, list(_MIRROR_PLANES), "mirror plane")
        normal = c.AXES[_MIRROR_PLANES[key]]
    else:
        normal = c.axis_vector(plane, "mirror plane normal")
    point = _vec3(base_point, "base_point")
    with _building(doc, "Wavesim: Mirror") as b:
        out = b.add("Part::Mirroring", label)
        out.Source = source
        out.Normal = normal
        for comp, value in zip("xyz", point):
            _set_qty(out, "Base." + comp, value, what="base_point " + comp)
        b.hide(source)
        b.finish(out)
    return out


def _draft():
    try:
        import Draft
    except Exception as exc:
        raise ApiError("The Draft workbench is not available ({}); arrays "
                       "need it.".format(exc))
    return Draft


@api_function("geometry")
def linear_array(doc=None, *, label, obj, direction="x", count, spacing):
    """Create a parametric linear array (Draft ortho array) of *obj*.

    Use for periodic structures (via fences, slot arrays, antenna elements).
    The copies are fused into one shape only if they touch (Draft ``Fuse``
    is off); the source is hidden, as the Draft GUI does.

    :param label: unique label (required).
    :param obj: source object (name or label).
    :param direction: ``"x"|"y"|"z"`` (optionally signed) or a vector.
    :param count: number of copies including the original (integer >= 1,
        or "=PARAM" bound to ``NumberX``).
    :param spacing: centre-to-centre distance along *direction* (mm /
        quantity / "=PARAM"; bound as ``IntervalX`` components).
    :returns: the Draft Array object (``Part::FeaturePython``).

    Example: ``linear_array(label="Vias", obj="Via", direction="x",
    count="=N_VIAS", spacing="=VIA_PITCH")``
    """
    doc = c.get_doc(doc)
    label = c.check_new_label(doc, label)
    source = _solid_input(doc, obj, "source object")
    dir_vec = c.axis_vector(direction, "direction")
    kind, gap = c.parse_quantity(spacing, _L)
    draft = _draft()
    with _building(doc, "Wavesim: Linear Array") as b:
        arr = b.track(draft.make_ortho_array(
            source, v_x=dir_vec * (gap if kind == "value" else 1.0),
            v_y=FreeCAD.Vector(0, 1, 0), v_z=FreeCAD.Vector(0, 0, 1),
            n_x=1, n_y=1, n_z=1, use_link=False))
        c.set_label(arr, label)
        _int_count(arr, "NumberX", count, "count", 1)
        if kind == "expr":
            for comp in "xyz":
                k = getattr(dir_vec, comp)
                if abs(k) > 1e-12:
                    _set_expr(arr, ".IntervalX." + comp,
                              "({}) * {!r}".format(gap, k), "spacing")
                else:
                    arr.IntervalX = _with_comp(arr.IntervalX, comp, 0.0)
        b.hide(source)
        b.finish(arr)
    return arr


def _with_comp(vec, comp, value):
    vec = FreeCAD.Vector(vec)
    setattr(vec, comp, value)
    return vec


@api_function("geometry")
def polar_array(doc=None, *, label, obj, axis="z", center=None, count,
                angle=360):
    """Create a parametric polar array (Draft polar array) of *obj*.

    Use for rotationally periodic structures (radial stubs, slot rings).
    *count* copies (including the original) are spread over *angle*; with
    360 deg they are evenly spaced without a duplicate. Source hidden.

    :param label: unique label (required).
    :param obj: source object (name or label).
    :param axis: rotation axis ``"x"|"y"|"z"`` or a vector.
    :param center: ``[x, y, z]`` point on the axis (mm, bindable); default
        origin.
    :param count: number of copies (integer >= 2, or "=PARAM").
    :param angle: total angle in degrees (default 360, bindable).
    :returns: the Draft Array object (``Part::FeaturePython``).

    Example: ``polar_array(label="Spokes", obj="Spoke", count=6)``
    """
    doc = c.get_doc(doc)
    label = c.check_new_label(doc, label)
    source = _solid_input(doc, obj, "source object")
    axis_vec = c.axis_vector(axis, "axis")
    point = _vec3(center, "center")
    draft = _draft()
    with _building(doc, "Wavesim: Polar Array") as b:
        arr = b.track(draft.make_polar_array(
            source, number=2, angle=360, center=FreeCAD.Vector(),
            use_link=False))
        c.set_label(arr, label)
        arr.Axis = axis_vec
        _int_count(arr, "NumberPolar", count, "count", 2)
        _set_qty(arr, "Angle", angle, _A, what="angle", minimum=0)
        for comp, value in zip("xyz", point):
            _set_qty(arr, "Center." + comp, value, what="center " + comp)
        b.hide(source)
        b.finish(arr)
    return arr


# --------------------------------------------------------------------------- #
# Fillets and chamfers
# --------------------------------------------------------------------------- #

def _edge_feature(doc, type_id, title, label, obj, edges, size, what):
    doc = c.get_doc(doc)
    label = c.check_new_label(doc, label)
    base = _solid_input(doc, obj, "object")
    if not base.Shape.Solids:
        raise ApiError("{!r} has no solid to {}.".format(
            base.Label, what))
    matched = c.select_subshapes(base.Shape, edges, "edge")
    if not matched:
        raise ApiError("The edge selector {!r} matched no edge of {!r}. Try "
                       "{{'type': 'all'}}, {{'parallel_to': 'z'}}, "
                       "{{'at_z': ...}} or {{'near_point': [x, y, z]}}."
                       .format(edges, base.Label))
    value = _eval_snapshot(base, size, _L, what)
    if value <= 0:
        raise ApiError("The {} size must be > 0 mm (got {:g}).".format(
            what, value))
    with _building(doc, title) as b:
        feat = b.add(type_id, label)
        feat.Base = base
        feat.Edges = [(int(name[4:]), value, value) for name, _ in matched]
        b.hide(base)
        b.finish(feat, hint="The {} size {:g} mm is probably too large for "
                            "the selected edges.".format(what, value))
    return {"object": c.describe(feat), "edges_matched": len(matched),
            "edges": [name for name, _ in matched]}


@api_function("geometry")
def fillet(doc=None, *, label, obj, edges, radius):
    """Round edges of a solid with a ``Part::Fillet`` feature.

    Use to model rounded corners. Edges are chosen ONLY with a selector
    (see :func:`wavesim_gui.api_common.select_subshapes`), never raw indices:
    ``{"type": "all"}``, ``{"parallel_to": "z"}``, ``{"at_z": 1.6}``,
    ``{"near_point": [x, y, z]}``, combined keys = AND. The base is hidden.

    :param label: unique label (required).
    :param obj: the solid to fillet (name or label).
    :param edges: selector dict (evaluated on the object's current shape).
    :param radius: fillet radius (mm / quantity). A param expression is
        evaluated once at creation (``Part::Fillet`` stores radii per edge,
        so they cannot be bound).
    :returns: ``{"object": <description>, "edges_matched": n, "edges":
        ["Edge3", ...]}``. Raises if no edge matches or the fillet fails.

    Example: ``fillet(label="Rounded", obj="Block", edges={"parallel_to":
    "z"}, radius=1)``
    """
    return _edge_feature(doc, "Part::Fillet", "Wavesim: Fillet", label, obj,
                         edges, radius, "fillet")


@api_function("geometry")
def chamfer(doc=None, *, label, obj, edges, size):
    """Bevel edges of a solid with a ``Part::Chamfer`` feature.

    Same edge selectors and return value as :func:`fillet`.

    :param label: unique label (required).
    :param obj: the solid (name or label).
    :param edges: selector dict, e.g. ``{"at_z": 10}``.
    :param size: chamfer distance (mm / quantity; a param expression is
        evaluated once, not bound).
    :returns: ``{"object": <description>, "edges_matched": n, "edges":
        [...]}``.

    Example: ``chamfer(label="Bevel", obj="Block", edges={"at_z": 10},
    size="0.5 mm")``
    """
    return _edge_feature(doc, "Part::Chamfer", "Wavesim: Chamfer", label,
                         obj, edges, size, "chamfer")


# --------------------------------------------------------------------------- #
# STEP import
# --------------------------------------------------------------------------- #

@api_function("geometry")
def import_step(doc=None, *, path, label_prefix=None):
    """Import a STEP file into the document and return the created objects.

    Use to bring in CAD parts (connectors, enclosures). Uses FreeCAD's
    STEP importer (``Import.insert``; falls back to ``Part.read`` as one
    ``Part::Feature``). Imported shapes are not parametric.

    :param path: absolute path of an existing ``.step``/``.stp`` file.
    :param label_prefix: optional; when given, the imported shape objects
        are labelled ``<prefix>`` (single shape) or ``<prefix>_1``,
        ``<prefix>_2``, ... (several). Labels must be unused.
    :returns: list of created objects.

    Example: ``import_step(path="C:/cad/sma.step", label_prefix="SMA")``
    """
    doc = c.get_doc(doc)
    if not isinstance(path, str) or not path.strip():
        raise ApiError("path must be the file path of a STEP file.")
    path = os.path.abspath(os.path.expanduser(path.strip()))
    if os.path.splitext(path)[1].lower() not in (".step", ".stp"):
        raise ApiError("{!r} is not a STEP file (.step / .stp).".format(path))
    if not os.path.isfile(path):
        raise ApiError("File {!r} does not exist.".format(path))
    if label_prefix is not None:
        label_prefix = c.check_new_label(doc, label_prefix)
    before = set(o.Name for o in doc.Objects)
    with _building(doc, "Wavesim: Import STEP") as b:
        try:
            import Import
            Import.insert(path, doc.Name)
        except Exception:
            import Part
            for name in [o.Name for o in doc.Objects
                         if o.Name not in before]:
                doc.removeObject(name)
            try:
                shape = Part.read(path)
            except Exception as exc:
                raise ApiError("Cannot read STEP file {!r}: {}".format(
                    path, exc))
            feat = doc.addObject("Part::Feature", "Import")
            feat.Shape = shape
            feat.Label = os.path.splitext(os.path.basename(path))[0]
        created = [o for o in doc.Objects if o.Name not in before]
        for o in created:
            b.track(o)
        if not created:
            raise ApiError("STEP file {!r} contained no shapes.".format(path))
        doc.recompute()
        if label_prefix is not None:
            shapes = [o for o in created if _has_shape(o)
                      and not any(p in created for p in o.InList)]
            shapes = shapes or [o for o in created if _has_shape(o)]
            if len(shapes) == 1:
                c.set_label(shapes[0], label_prefix)
            else:
                for i, o in enumerate(shapes, 1):
                    c.set_label(o, c.check_new_label(
                        doc, "{}_{}".format(label_prefix, i)))
    return created


# --------------------------------------------------------------------------- #
# Inspection
# --------------------------------------------------------------------------- #

@api_function("geometry")
def measure(doc=None, *, obj):
    """Measure an object's shape.

    :param obj: the object (name or label).
    :returns: ``{"bbox": {"min": [..], "max": [..], "size": [..]}`` (mm),
        ``"volume_mm3"``, ``"area_mm2"``, ``"center_of_mass": [x, y, z]``
        (mm, of the solids; geometric centre otherwise), ``"solids"``
        (count), ``"valid"`` (``shape.isValid()``)}.

    Example: ``measure(obj="Dielectric")["volume_mm3"]``
    """
    doc = c.get_doc(doc)
    target = c.find_object(doc, obj)
    shape = getattr(target, "Shape", None)
    if shape is None or not _has_shape(target):
        raise ApiError("{!r} has no shape to measure.".format(target.Label))
    if shape.isNull():
        raise ApiError("{!r} has an empty shape.".format(target.Label))
    return {"label": target.Label, "name": target.Name,
            "bbox": _bbox_dict(shape.BoundBox),
            "volume_mm3": float(shape.Volume),
            "area_mm2": float(shape.Area),
            "center_of_mass": c.to_jsonable(_center_of_mass(shape)),
            "solids": len(shape.Solids),
            "valid": bool(shape.isValid())}


def _final_geometry(doc):
    """Visible, non-Wavesim shape objects not consumed by another feature."""
    members = _sim_members(doc)
    out = []
    for obj in doc.Objects:
        if not _has_shape(obj) or _is_wavesim(obj) or obj.Name in members:
            continue
        if not obj.Visibility or _consumers(obj):
            continue
        out.append(obj)
    return out


@api_function("geometry")
def check_geometry(doc=None, objects=None, tolerance_mm3=1e-6):
    """Check solids for validity and find overlapping pairs.

    Use before meshing: overlapping solids with different materials make
    the material assignment ambiguous. By default checks every visible
    geometry object that is not an input of another feature (boolean inputs,
    mirror/array sources) and not a Wavesim object.

    :param objects: optional list of objects (names/labels) to check.
    :param tolerance_mm3: overlap volumes at or below this are ignored
        (touching faces have zero common volume).
    :returns: ``{"ok": bool, "objects": [{"name", "label", "valid",
        "null", "solids", "issues": [...]}], "overlaps": [{"a", "b",
        "volume_mm3"}]}`` (labels in ``a``/``b``).

    Example: ``check_geometry()["overlaps"]``
    """
    doc = c.get_doc(doc)
    if objects is None:
        objs = _final_geometry(doc)
    else:
        if isinstance(objects, str) or hasattr(objects, "Name"):
            objects = [objects]
        objs = [_geometry(doc, o) for o in objects]
    report, solids = [], []
    for obj in objs:
        shape = obj.Shape
        issues = []
        null = shape.isNull()
        valid = (not null) and bool(shape.isValid())
        n_solids = 0 if null else len(shape.Solids)
        if null:
            issues.append("empty (null) shape")
        elif not valid:
            issues.append("shape is not valid (shape.isValid() is False)")
        if not null and n_solids == 0:
            issues.append("not a solid ({})".format(shape.ShapeType))
        if "Invalid" in list(obj.State):
            issues.append("recompute error: {}".format(
                obj.getStatusString()))
        report.append({"name": obj.Name, "label": obj.Label, "valid": valid,
                       "null": null, "solids": n_solids, "issues": issues})
        if valid and n_solids:
            solids.append(obj)
    overlaps = []
    tol = float(tolerance_mm3)
    for i, a in enumerate(solids):
        bb_a = a.Shape.BoundBox
        for bobj in solids[i + 1:]:
            if not bb_a.intersect(bobj.Shape.BoundBox):
                continue
            try:
                vol = a.Shape.common(bobj.Shape).Volume
            except Exception:
                continue
            if vol > tol:
                overlaps.append({"a": a.Label, "b": bobj.Label,
                                 "volume_mm3": float(vol)})
    ok = not overlaps and all(not r["issues"] for r in report)
    return {"ok": ok, "objects": report, "overlaps": overlaps}


# --------------------------------------------------------------------------- #
# Document housekeeping
# --------------------------------------------------------------------------- #

@api_function("geometry")
def delete_object(doc=None, *, obj, force=False):
    """Delete a geometry object.

    Refuses when other objects depend on it (boolean results, arrays,
    fillets, material assignments...) unless *force* is true. Like the Part
    GUI, inputs of a deleted feature that are no longer used elsewhere are
    shown again. Never deletes Wavesim simulation objects (sources,
    monitors, materials, domain...) -- use the Wavesim setup functions.

    :param obj: the object (name or label).
    :param force: delete even if others depend on it (they will break).
    :returns: ``{"deleted": label, "dependents": [labels], "shown":
        [labels made visible again]}``.

    Example: ``delete_object(obj="Old box")``
    """
    doc = c.get_doc(doc)
    target = c.find_object(doc, obj)
    if _is_wavesim(target) or target.Name in _sim_members(doc):
        raise ApiError(
            "{!r} is a Wavesim simulation object; delete_object only removes "
            "geometry. Use delete_setup_object for "
            "it.".format(target.Label))
    dependents = [o for o in target.InList if o is not target
                  and not (o.isDerivedFrom("App::DocumentObjectGroup")
                           or o.isDerivedFrom("App::Part"))]
    if dependents and not force:
        raise ApiError(
            "{!r} is used by: {}. Delete those first or pass force=True."
            .format(target.Label, ", ".join(o.Label for o in dependents)))
    inputs = [o for o in _link_targets(target)
              if _has_shape(o) and not _is_wavesim(o)]
    label = target.Label
    shown = []
    with c.transaction(doc, "Wavesim: Delete"):
        doc.removeObject(target.Name)
        for o in inputs:
            if doc.getObject(o.Name) is None or o.Visibility:
                continue
            if not _consumers(o):
                _set_visible(o, True)
                shown.append(o.Label)
        doc.recompute()
    return {"deleted": label, "dependents": [o.Label for o in dependents],
            "shown": shown}


@api_function("geometry")
def rename_object(doc=None, *, obj, new_label):
    """Give a geometry object a new unique label and return it.

    :param obj: the object (name or current label).
    :param new_label: the new label (must be unused).
    :returns: the object.

    Example: ``rename_object(obj="Box", new_label="Substrate")``
    """
    doc = c.get_doc(doc)
    target = _geometry(doc, obj)
    if str(new_label).strip() == target.Label:
        return target
    new_label = c.check_new_label(doc, new_label)
    with c.transaction(doc, "Wavesim: Rename"):
        c.set_label(target, new_label)
    return target


@api_function("geometry")
def list_objects(doc=None, include_wavesim=False):
    """List the geometry objects of a document.

    :param include_wavesim: also list Wavesim objects that have a shape.
    :returns: list of ``{"name", "label", "type", "visible", "bbox"
        (min/max/size mm or None), "volume" (mm^3), "is_solid",
        "consumed_by" (labels of features using it as input), "material"
        (label of the Wavesim material whose Bodies contain it, or None)}``.

    Example: ``[o["label"] for o in list_objects() if o["visible"]]``
    """
    from wavesim_gui import commands
    doc = c.get_doc(doc)
    material_of = {}
    sim = commands.active_simulation(doc)
    if sim is not None:
        from wavesim_gui import materials
        for mat in materials.find_materials(sim):
            for body in getattr(mat, "Bodies", None) or ():
                material_of.setdefault(body.Name, mat.Label)
    members = _sim_members(doc)
    out = []
    for obj in doc.Objects:
        if not _has_shape(obj):
            continue
        wav = _is_wavesim(obj) or obj.Name in members
        if wav and not include_wavesim:
            continue
        shape = obj.Shape
        null = shape.isNull()
        out.append({
            "name": obj.Name, "label": obj.Label,
            "type": c.wavesim_type(obj) or obj.TypeId,
            "visible": bool(obj.Visibility),
            "bbox": None if null else _bbox_dict(shape.BoundBox),
            "volume": 0.0 if null else float(shape.Volume),
            "is_solid": (not null) and bool(shape.Solids),
            "consumed_by": [p.Label for p in _consumers(obj)],
            "material": material_of.get(obj.Name),
        })
    return out
