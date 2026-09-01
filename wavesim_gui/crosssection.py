# -*- coding: utf-8 -*-
"""Cross-section view: cut the model open on a grid plane, capped and hatched.

FreeCAD's own *View -> Clipping plane* clips the scene with an OpenGL clip
plane, which leaves every solid it cuts looking **hollow** -- there is no surface
where the plane passed, only the inside of the far shell. That is the wrong
picture for a model whose whole point is what is inside it, and it is awkward to
place and to move.

This does it with geometry instead. Each visible solid is cut by a half-space box
(``Shape.cut``), which returns a **closed** solid with a real planar face where
the plane passed -- so the section reads as material, not as a hole. Measured on
the test suite the whole per-move cost (cut + FreeCAD's own tessellation) runs
from 40 ms on ``coaxial_line`` to 630 ms on ``bent_coax``, which is why the panel
debounces (see :data:`_DEBOUNCE_MS`) rather than recutting on every keystroke.

The cut plane **is a grid plane**
--------------------------------
There is no second position property. The cut sits at the Domain's own
``GridPlaneX/Y/Z`` (:func:`domain.grid_plane_positions_mm`), so moving the cut
moves the voxel grid drawn on it, by construction rather than by syncing -- which
is the point of the tool: to read the mesh against the geometry it will
discretise. The panel also switches the other two grid planes off through the
Domain's ``ShowGridPlaneX/Y/Z``, because three grids at once over a sectioned
model is unreadable.

Denoting the cut face
---------------------
Three styles (:data:`CAP_STYLES`), and the marking is **derived from the body's
own colour** in each -- blended toward black rather than fixed. That is what
keeps it quieter than the grey voxel grid whatever the material is tinted, and
the hierarchy matters: the grid is the thing being read, the section marking is
context. A fixed black hatch inverts that and becomes the loudest thing on
screen.

* ``CAP_SHADED`` (the default) -- no extra geometry at all. The cap faces are
  found by :func:`is_cap_face` and tinted through a per-face ``DiffuseColor``
  built to the cut's own face count. Nothing to z-fight with, nothing to hatch.
* ``CAP_HATCHED`` -- cross-hatch lines at +/-45 degrees over the cap, one line
  set per body so each can carry its own shade. Built by a **plain scanline over
  the discretized wires** (:func:`_hatch_face`), not by an OCC boolean against a
  compound of lines: the two agree to the discretisation (1090 vs 1090 segments
  on the 22 caps of ``floating_shield_on_unshielded_coax_in_a_box``, 535 vs 537
  on ``resistor_cap``) and the scanline costs 1.9-3.5 ms against the boolean's
  148-325 ms. Every wire of a face goes into one crossing count, so an annular
  cap (a coax shield) hatches as a ring and not as a disc.
* ``CAP_PLAIN`` -- the cap in the body's own colour, unmarked.

What this is not
----------------
**View only, and it cannot reach a run.** The preview objects are ordinary
``Part::Feature`` objects that belong to no Material, and both the voxeliser
(``voxelize`` walks ``mat.Bodies``) and the Domain's auto-sizing
(``domain._material_bounds``) enumerate material bodies only -- so a cut model
voxelises exactly like an uncut one, and the preview cannot grow the domain box.
Keep it that way: anything here that starts being read by the mesher is a bug.

**Taking a cut down is one operation, and it lives in the document.** A cut is
two halves -- preview objects added, source bodies hidden -- and undoing one
without the other is what broke it once: the record of *which* bodies were hidden
sat in the panel instance, so opening the panel a second time swept the previous
previews away with nothing left to say what to show again, and the model went
blank while the tool reported nothing to cut. So the record is a property on the
body (:data:`_HIDDEN_PROP`, written by :func:`_hide_source`), and :func:`sweep`
always does both halves. Any entry point can then undo a cut it did not make --
which is exactly what a second panel, and a reload, need to do.

**It does not survive a reload.** :class:`_RestoreSweeper` runs that same sweep
as a document finishes loading, so a cut saved by accident comes back whole,
bodies and all. It is also not live: edit the geometry or the mesh while a cut is
up and the preview is stale until the panel is re-opened.

Importing this module registers ``Wavesim_CrossSection`` with ``Gui.addCommand``
when a GUI is available.
"""

import math
import os

import FreeCAD


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_ICONS_DIR = os.path.join(_WB_DIR, "Resources", "icons")
_XSEC_ICON = os.path.join(_ICONS_DIR, "cross_section.svg")

# Axis names in the x/y/z order every index in this module uses.
AXES = ("x", "y", "z")
# The two in-plane axes for a cut normal to each axis, same order.
_TRANSVERSE = ((1, 2), (0, 2), (0, 1))
# The Domain property holding each axis's plane position / visibility.
_POSITION_PROPS = ("GridPlaneX", "GridPlaneY", "GridPlaneZ")
_SHOW_PROPS = ("ShowGridPlaneX", "ShowGridPlaneY", "ShowGridPlaneZ")

# Marks a document object as one of ours, so a stale one -- left by a crash, or
# written into a saved file -- can be found and removed without guessing at
# names. See _RestoreSweeper.
_PREVIEW_PROP = "WavesimCrossSectionPreview"

# Marks a *source* body that we hid to put a cut copy in its place. This lives
# on the document object rather than in the panel that hid it, and that is the
# whole point: the record has to outlive the panel. It did not, once -- the
# second panel's opening sweep deleted the first panel's previews with nothing
# left to say which bodies to show again, so the model went blank and the tool
# reported nothing to cut. See restore_hidden.
_HIDDEN_PROP = "WavesimCrossSectionHidden"

# How the exposed section face is denoted, in panel order.
CAP_SHADED = "Shaded"          # the cap in a darker shade of the body colour
CAP_HATCHED = "Hatched"        # cross-hatch lines over the cap
CAP_PLAIN = "Plain"            # nothing; the cap in the body's own colour
CAP_STYLES = (CAP_SHADED, CAP_HATCHED, CAP_PLAIN)

# Both markings are **derived from the body's own colour**, blended toward black
# (multiply, then lift so a dark body does not go flat black). Deriving rather
# than fixing a colour is what keeps the contrast low whatever the material is
# tinted, which is the requirement: the section marking has to sit *under* the
# voxel grid in the visual hierarchy. A fixed black hatch does the opposite --
# it is the most prominent thing on screen, over a grid drawn in mid grey.
_CAP_SHADE, _CAP_FLOOR = 0.62, 0.06
_HATCH_SHADE, _HATCH_FLOOR = 0.60, 0.05

# How far the cutting box overhangs the body it cuts (mm). Only has to clear the
# shape; the box is thrown away immediately.
_CUT_PAD = 10.0

# A face counts as a cap when it is planar, normal to the cut axis, and lies in
# the cut plane to within this (mm).
_PLANE_TOL = 1.0e-6

# Hatch pitch as a fraction of the model's in-plane size, and the deflection the
# cap wires are discretized at as a fraction of that pitch. One pitch for the
# whole cross-section rather than per body, so the caps read as one cut surface.
_HATCH_PITCH_FRAC = 1.0 / 34.0
_HATCH_DEFLECTION_FRAC = 0.1

# The hatch is drawn this far off the cap, toward the side that was cut away, so
# it does not z-fight with the face it marks. A fraction of the model size, with
# an absolute floor for a very small one.
_HATCH_LIFT_FRAC = 2.0e-4
_HATCH_LIFT_MIN = 1.0e-5

_HATCH_WIDTH = 1

# Both hatch families, in radians: a cross-hatch, not a single-direction hatch.
_HATCH_ANGLES = (math.pi / 4.0, -math.pi / 4.0)

# Idle time after the last edit before the model is recut. Long enough that a
# held arrow key on the position spinner does not queue boolean work, short
# enough to feel like a direct manipulation.
_DEBOUNCE_MS = 80

_EPS = 1.0e-9


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def visible_solids(doc):
    """Every solid in *doc* that is currently drawn, as document objects.

    Filtering on ``ViewObject.Visibility`` is what keeps a PartDesign ``Body``
    from being cut twice: its own ``Pad``/``Pocket`` features carry solid shapes
    of their own, and in a normal document they are hidden while the Body is
    shown. Our own previews are skipped, so a re-apply cuts the originals rather
    than the last cut.
    """
    out = []
    for obj in getattr(doc, "Objects", []) or []:
        if getattr(obj, _PREVIEW_PROP, False):
            continue
        shape = getattr(obj, "Shape", None)
        if shape is None or shape.isNull() or not shape.Solids:
            continue
        vobj = getattr(obj, "ViewObject", None)
        if vobj is None or not bool(getattr(vobj, "Visibility", False)):
            continue
        out.append(obj)
    return out


def _cut_box(bbox, axis, coord, keep_low):
    """The box to subtract from a shape bounded by *bbox* to cut it open.

    Removes the half beyond *coord* on *axis* -- the high half when *keep_low*.
    ``None`` when the plane misses the shape on the discarded side, which means
    the shape is kept whole rather than cut.
    """
    import Part

    lo = [bbox.XMin - _CUT_PAD, bbox.YMin - _CUT_PAD, bbox.ZMin - _CUT_PAD]
    hi = [bbox.XMax + _CUT_PAD, bbox.YMax + _CUT_PAD, bbox.ZMax + _CUT_PAD]
    if keep_low:
        lo[axis] = coord
    else:
        hi[axis] = coord
    lengths = [hi[i] - lo[i] for i in range(3)]
    if min(lengths) <= _EPS:
        return None
    return Part.makeBox(lengths[0], lengths[1], lengths[2],
                        FreeCAD.Vector(lo[0], lo[1], lo[2]))


def cut_shape(shape, axis, coord, keep_low):
    """*shape* with the half beyond *coord* removed, or ``None`` if nothing left.

    The result is a closed solid carrying a real face in the cut plane -- that
    face is what makes the section read as material instead of as the hollow an
    OpenGL clip plane leaves. A shape the plane misses entirely comes back
    unchanged.
    """
    box = _cut_box(shape.BoundBox, axis, coord, keep_low)
    if box is None:
        return shape
    try:
        cut = shape.cut(box)
    except Exception:
        return shape          # a cut we cannot do is better shown uncut
    if cut is None or cut.isNull() or not cut.Faces:
        return None           # the plane took the whole body
    return cut


def is_cap_face(face, axis, coord):
    """Whether *face* lies in the cut plane -- i.e. is exposed section.

    Planar, normal to the cut axis, and flat against ``axis == coord``. A face
    the body already had in that plane qualifies too, which is right: it is a
    surface in the section whatever produced it.
    """
    import Part

    surface = getattr(face, "Surface", None)
    if not isinstance(surface, Part.Plane):
        return False
    normal = surface.Axis
    if abs(abs((normal.x, normal.y, normal.z)[axis]) - 1.0) > 1.0e-6:
        return False
    bbox = face.BoundBox
    lo = (bbox.XMin, bbox.YMin, bbox.ZMin)[axis]
    hi = (bbox.XMax, bbox.YMax, bbox.ZMax)[axis]
    return abs(lo - coord) < _PLANE_TOL and abs(hi - coord) < _PLANE_TOL


def _cap_faces(shape, axis, coord):
    """The faces of *shape* lying in the cut plane -- the exposed section."""
    return [face for face in (getattr(shape, "Faces", []) or [])
            if is_cap_face(face, axis, coord)]


def _shade(color, factor, floor):
    """*color* blended toward black, keeping its alpha.

    Multiply-then-lift rather than a straight multiply so a body that is already
    dark keeps some of its hue instead of collapsing to black.
    """
    try:
        rgb = [min(float(c) * factor + floor, 1.0) for c in color[:3]]
    except Exception:
        return (0.35, 0.35, 0.35, 0.0)
    alpha = float(color[3]) if len(color) > 3 else 0.0
    return (rgb[0], rgb[1], rgb[2], alpha)


def _hatch_face(face, axis, coord, pitch, angle, deflection):
    """Hatch segments across *face*, as ``((a0, b0), (a1, b1))`` in-plane pairs.

    An even-odd scanline in a frame rotated by *angle*, so the hatch lines are
    ``v = const`` and each one's inside/outside runs fall straight out of the
    sorted crossings. Every wire of the face is thrown into the same crossing
    count, which is what makes a hole a hole -- an annular cap (a coax shield)
    hatches as a ring rather than as a disc.

    Deliberately not an OCC boolean against a compound of lines. That is exact
    to the curved boundary, agrees with this to the discretisation, and costs
    two orders of magnitude more (148-325 ms against 1.9-3.5 ms on the heaviest
    test documents) -- which would make the hatch, not the cut, the reason the
    view lags.
    """
    ax_a, ax_b = _TRANSVERSE[axis]
    cos_t, sin_t = math.cos(angle), math.sin(angle)

    polygons = []
    for wire in getattr(face, "Wires", []) or []:
        try:
            points = wire.discretize(Deflection=deflection)
        except Exception:
            continue
        poly = []
        for point in points:
            a = (point.x, point.y, point.z)[ax_a]
            b = (point.x, point.y, point.z)[ax_b]
            poly.append((a * cos_t + b * sin_t, -a * sin_t + b * cos_t))
        if len(poly) >= 3:
            polygons.append(poly)
    if not polygons:
        return []

    vs = [v for poly in polygons for _u, v in poly]
    v_lo, v_hi = min(vs), max(vs)

    segments = []
    # Start at the first multiple of the pitch inside the face, in a frame
    # anchored at the origin rather than at this face -- so neighbouring bodies'
    # caps carry one continuous hatch instead of each starting its own.
    start = math.ceil(v_lo / pitch) * pitch
    steps = int((v_hi - start) / pitch) + 1
    for i in range(steps):
        v = start + i * pitch
        if v <= v_lo or v >= v_hi:
            continue
        crossings = []
        for poly in polygons:
            n = len(poly)
            for k in range(n):
                u0, v0 = poly[k]
                u1, v1 = poly[(k + 1) % n]
                if (v0 <= v < v1) or (v1 <= v < v0):
                    crossings.append(u0 + (v - v0) / (v1 - v0) * (u1 - u0))
        crossings.sort()
        for k in range(0, len(crossings) - 1, 2):
            u_a, u_b = crossings[k], crossings[k + 1]
            if u_b - u_a <= _EPS:
                continue
            segments.append((
                (u_a * cos_t - v * sin_t, u_a * sin_t + v * cos_t),
                (u_b * cos_t - v * sin_t, u_b * sin_t + v * cos_t),
            ))
    return segments


def hatch_edges(shapes, axis, coord, keep_low, pitch):
    """Hatch every cap face of *shapes*, as ``Part`` line edges in world space.

    The lines are lifted clear of the cap toward the side that was cut away --
    the side they are looked at from -- so they draw in front of the face they
    mark instead of z-fighting with it.
    """
    import Part

    if pitch <= _EPS:
        return []
    # The pitch is a fixed fraction of the model, so it is also the way back
    # to the model's own size -- which is what the lift should scale with.
    lift = max(pitch * (_HATCH_LIFT_FRAC / _HATCH_PITCH_FRAC), _HATCH_LIFT_MIN)
    plane = coord + (lift if keep_low else -lift)
    deflection = max(pitch * _HATCH_DEFLECTION_FRAC, _EPS)
    ax_a, ax_b = _TRANSVERSE[axis]

    edges = []
    for shape in shapes:
        for face in _cap_faces(shape, axis, coord):
            for angle in _HATCH_ANGLES:
                for (a0, b0), (a1, b1) in _hatch_face(
                        face, axis, coord, pitch, angle, deflection):
                    p0, p1 = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
                    p0[axis] = p1[axis] = plane
                    p0[ax_a], p0[ax_b] = a0, b0
                    p1[ax_a], p1[ax_b] = a1, b1
                    try:
                        edges.append(Part.makeLine(
                            FreeCAD.Vector(*p0), FreeCAD.Vector(*p1)))
                    except Exception:
                        pass
    return edges


# --------------------------------------------------------------------------- #
# The preview: cut copies standing in for the real bodies
# --------------------------------------------------------------------------- #

def _mark_preview(obj):
    """Tag *obj* as a cross-section preview and keep it out of the tree."""
    try:
        obj.addProperty("App::PropertyBool", _PREVIEW_PROP, "Wavesim",
                        "Transient cross-section preview; not part of the model")
        setattr(obj, _PREVIEW_PROP, True)
        obj.setEditorMode(_PREVIEW_PROP, 2)
    except Exception:
        pass
    vobj = getattr(obj, "ViewObject", None)
    if vobj is not None:
        try:
            vobj.ShowInTree = False
        except Exception:
            pass
    return obj


def _hide_source(obj):
    """Hide *obj*, recording **on the document** that we were the one who did.

    The marker is what lets any later sweep put the body back without having to
    be the same panel that hid it. Call inside :func:`visibility.suppressed`.
    """
    vobj = getattr(obj, "ViewObject", None)
    if vobj is None:
        return False
    try:
        if not hasattr(obj, _HIDDEN_PROP):
            obj.addProperty("App::PropertyBool", _HIDDEN_PROP, "Wavesim",
                            "Hidden by the Wavesim cross-section view")
            obj.setEditorMode(_HIDDEN_PROP, 2)
        setattr(obj, _HIDDEN_PROP, True)
        vobj.Visibility = False
    except Exception:
        return False
    return True


def restore_hidden(doc):
    """Show every body the cross-section hid, and forget that it did.

    Only bodies carrying :data:`_HIDDEN_PROP` -- which are exactly the ones that
    were visible when a cut was taken, since ``visible_solids`` never offers a
    hidden one. So a body the *user* had hidden stays hidden, and the marker is
    removed rather than set false, leaving the object as it was found.
    """
    from wavesim_gui import visibility

    if doc is None:
        return 0
    marked = [obj for obj in (getattr(doc, "Objects", []) or [])
              if getattr(obj, _HIDDEN_PROP, False)]
    with visibility.suppressed():
        for obj in marked:
            vobj = getattr(obj, "ViewObject", None)
            if vobj is not None:
                try:
                    vobj.Visibility = True
                except Exception:
                    pass
            try:
                obj.removeProperty(_HIDDEN_PROP)
            except Exception:
                try:
                    setattr(obj, _HIDDEN_PROP, False)
                except Exception:
                    pass
    return len(marked)


def sweep(doc):
    """Take down any cross-section in *doc*: previews out, bodies back.

    Both halves, always. Deleting the previews alone is what left the model
    blank on a second open -- the bodies stayed hidden with nothing left holding
    their state. Called before a fresh cut and again when a document finishes
    loading, so a cut that outlived its panel (a crash, or a document saved
    while one was up) cannot masquerade as geometry or eat the model.
    """
    if doc is None:
        return 0
    stale = [obj.Name for obj in (getattr(doc, "Objects", []) or [])
             if getattr(obj, _PREVIEW_PROP, False)]
    for name in stale:
        try:
            doc.removeObject(name)
        except Exception:
            pass
    restore_hidden(doc)
    return len(stale)


class CrossSectionPreview(object):
    """Cut copies of the visible solids, standing in for the bodies themselves.

    :meth:`apply` hides each source body and adds a ``Part::Feature`` holding its
    cut shape in its own colour; :meth:`clear` takes the whole thing down.

    **It keeps no record of what it hid.** That lives on the bodies themselves
    (:data:`_HIDDEN_PROP`), so :func:`sweep` can undo a cut this instance did not
    make -- which is what a second panel does when it opens.

    The hide/show runs inside :func:`visibility.suppressed`. Without it the
    Material eye-linking observer reads our hiding of a body as the user hiding
    it and switches the owning Material off -- so a preview taken down would
    leave the tree claiming a material is hidden while its bodies are back.
    """

    def __init__(self, doc):
        self.doc = doc
        self._names = []        # preview objects we created, in creation order

    # -- state ---------------------------------------------------------- #

    @property
    def active(self):
        return bool(self._names)

    def _alive(self):
        return (self.doc is not None
                and self.doc.Name in FreeCAD.listDocuments())

    # -- teardown ------------------------------------------------------- #

    def clear(self):
        """Remove the preview and show the bodies it stood in for again."""
        if not self._alive():
            self._names = []
            return
        for name in self._names:
            try:
                self.doc.removeObject(name)
            except Exception:
                pass
        self._names = []
        # Sweeps our own leftovers *and* anything another panel left, and shows
        # every marked body -- the single path by which a cut comes down.
        sweep(self.doc)

    # -- build ---------------------------------------------------------- #

    def apply(self, axis, coord, keep_low, style=CAP_SHADED):
        """Cut every visible solid at ``axis == coord``; returns the body count.

        Rebuilds from scratch each time -- the sources are read *before* anything
        is hidden, so re-applying cuts the real bodies rather than the last cut.
        """
        from wavesim_gui import visibility

        if not self._alive():
            return 0
        self.clear()
        sources = visible_solids(self.doc)
        if not sources:
            return 0

        cuts = []
        for obj in sources:
            shape = cut_shape(obj.Shape, axis, coord, keep_low)
            if shape is not None:
                cuts.append((obj, shape))
        if not cuts:
            # The plane took every body. Leave the model alone rather than show
            # an empty scene the user cannot tell from a broken one.
            return 0

        with visibility.suppressed():
            for obj, _shape in cuts:
                _hide_source(obj)

        pitch = _hatch_pitch([shape for _obj, shape in cuts], axis)
        for obj, shape in cuts:
            self._add_body(obj, shape, axis, coord, style)
            if style == CAP_HATCHED:
                self._add_hatch(obj, shape, axis, coord, keep_low, pitch)
        self._show()
        return len(cuts)

    def _show(self):
        """Bring the previews we just added onto the screen.

        Scoped to our own objects on purpose. A bare ``doc.recompute()`` would
        re-run the Domain's ``execute`` -- rebuilding the node arrays, on every
        step of the position spinner -- for a set of objects that have no
        ``execute`` of their own and only need their new ``Shape`` picked up.
        """
        objects = [self.doc.getObject(name) for name in self._names]
        objects = [obj for obj in objects if obj is not None]
        if not objects:
            return
        try:
            self.doc.recompute(objects)
        except Exception:
            pass

    def _add_body(self, source, shape, axis, coord, style):
        preview = self.doc.addObject("Part::Feature", "WavesimXSecBody")
        preview.Label = "{} (section)".format(source.Label)
        preview.Shape = shape
        _mark_preview(preview)
        self._copy_appearance(source, preview)
        if style == CAP_SHADED:
            self._shade_caps(source, preview, shape, axis, coord)
        try:
            preview.purgeTouched()
        except Exception:
            pass
        self._names.append(preview.Name)

    @staticmethod
    def _copy_appearance(source, preview):
        """Carry the body's own colour onto its cut copy.

        Without it a sectioned model loses the material tint
        (``materials.apply_material_color``) that says which lump is PEC and
        which is dielectric -- the reason for looking inside it in the first
        place. The source's own ``DiffuseColor`` is deliberately *not* copied: it
        is per-face and the cut has a different face count. :meth:`_shade_caps`
        builds a fresh one at the right length instead.
        """
        src = getattr(source, "ViewObject", None)
        dst = getattr(preview, "ViewObject", None)
        if src is None or dst is None:
            return
        for prop in ("ShapeColor", "Transparency", "LineColor", "PointColor"):
            try:
                setattr(dst, prop, getattr(src, prop))
            except Exception:
                pass

    @staticmethod
    def _shade_caps(source, preview, shape, axis, coord):
        """Tint just the cap faces, via a per-face ``DiffuseColor``.

        This is the quiet way to denote a section: no extra geometry, nothing to
        z-fight with, and nothing that competes with the voxel grid drawn on the
        same plane. Must run *after* ``ShapeColor`` is set -- assigning that
        resets ``DiffuseColor`` to a single entry.
        """
        vobj = getattr(preview, "ViewObject", None)
        if vobj is None:
            return
        base = getattr(getattr(source, "ViewObject", None), "ShapeColor", None)
        if base is None:
            base = getattr(vobj, "ShapeColor", (0.8, 0.8, 0.8, 0.0))
        tint = _shade(base, _CAP_SHADE, _CAP_FLOOR)
        colors = [tuple(base) if not is_cap_face(face, axis, coord) else tint
                  for face in (getattr(shape, "Faces", []) or [])]
        if not colors or all(c == colors[0] for c in colors):
            return          # nothing exposed here; leave the body's own colour
        try:
            vobj.DiffuseColor = colors
        except Exception:
            pass

    def _add_hatch(self, source, shape, axis, coord, keep_low, pitch):
        """Cross-hatch one body's caps, in a darker shade of its own colour.

        One hatch object per body rather than one for the model, because Coin
        carries a single line colour per set and the whole point of the shade is
        that it is *this body's*.
        """
        import Part

        edges = hatch_edges([shape], axis, coord, keep_low, pitch)
        if not edges:
            return
        preview = self.doc.addObject("Part::Feature", "WavesimXSecHatch")
        preview.Label = "{} (hatch)".format(source.Label)
        preview.Shape = Part.makeCompound(edges)
        _mark_preview(preview)
        base = getattr(getattr(source, "ViewObject", None), "ShapeColor",
                       (0.6, 0.6, 0.6, 0.0))
        vobj = getattr(preview, "ViewObject", None)
        if vobj is not None:
            for prop, value in (
                    ("LineColor", _shade(base, _HATCH_SHADE, _HATCH_FLOOR)),
                    ("LineWidth", _HATCH_WIDTH),
                    ("Selectable", False)):
                try:
                    setattr(vobj, prop, value)
                except Exception:
                    pass
        try:
            preview.purgeTouched()
        except Exception:
            pass
        self._names.append(preview.Name)


def _hatch_pitch(shapes, axis):
    """One hatch pitch for the whole section, from the model's in-plane size.

    Derived from every cut shape together rather than per face, so a small body
    beside a large one is hatched at the same pitch and the caps read as one cut
    surface instead of as unrelated textures.
    """
    bbox = FreeCAD.BoundBox()
    for shape in shapes:
        try:
            bbox.add(shape.BoundBox)
        except Exception:
            pass
    if not bbox.isValid():
        return 0.0
    lo = (bbox.XMin, bbox.YMin, bbox.ZMin)
    hi = (bbox.XMax, bbox.YMax, bbox.ZMax)
    ax_a, ax_b = _TRANSVERSE[axis]
    span = max(hi[ax_a] - lo[ax_a], hi[ax_b] - lo[ax_b])
    return span * _HATCH_PITCH_FRAC if span > _EPS else 0.0


# --------------------------------------------------------------------------- #
# The cut plane is a grid plane
# --------------------------------------------------------------------------- #

def axis_extent_mm(domain, doc, axis):
    """``(lo, hi)`` the cut may travel between on *axis*, in world mm.

    The domain box, which is what the grid plane is clamped to anyway
    (:func:`domain.grid_plane_positions_mm`). Falls back to the visible geometry
    for a document whose domain has not been sized yet, so the tool still works
    before any material is assigned.
    """
    mn = getattr(domain, "DomainMin", None) if domain is not None else None
    mx = getattr(domain, "DomainMax", None) if domain is not None else None
    if mn is not None and mx is not None and (mn - mx).Length > 1.0e-9:
        return (getattr(mn, AXES[axis]), getattr(mx, AXES[axis]))
    bbox = FreeCAD.BoundBox()
    for obj in visible_solids(doc):
        bbox.add(obj.Shape.BoundBox)
    if not bbox.isValid():
        return (0.0, 0.0)
    lo = (bbox.XMin, bbox.YMin, bbox.ZMin)[axis]
    hi = (bbox.XMax, bbox.YMax, bbox.ZMax)[axis]
    return (lo, hi)


def read_plane_state(domain):
    """The Domain's three plane positions (mm) and three visibility flags."""
    from wavesim_gui import domain as domain_mod

    positions = []
    for prop in _POSITION_PROPS:
        value = getattr(domain, prop, None)
        positions.append(float(value.Value) if value is not None else 0.0)
    return tuple(positions), domain_mod.grid_plane_visibility(domain)


def write_plane_state(domain, position_mm=None, axis=None, show=None):
    """Put the cut where the panel says, and switch the grid planes to match.

    Writing ``GridPlane*``/``ShowGridPlane*`` is all it takes to move the drawn
    mesh: ``DomainViewProvider.updateData`` rebuilds the grid line sets straight
    off the property change, with no recompute -- which is what makes following
    the cut cost nothing next to the boolean.
    """
    if domain is None:
        return
    if axis is not None and position_mm is not None:
        try:
            setattr(domain, _POSITION_PROPS[axis], "{} mm".format(position_mm))
        except Exception:
            pass
    if show is not None:
        for prop, state in zip(_SHOW_PROPS, show):
            try:
                setattr(domain, prop, bool(state))
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# GUI: task panel + command
# --------------------------------------------------------------------------- #

try:
    import FreeCADGui as Gui

    _GUI_AVAILABLE = True
except Exception:      # console mode / no Qt
    _GUI_AVAILABLE = False


class _RestoreSweeper(object):
    """Deletes cross-section previews as a document finishes loading.

    The previews are real document objects, so a document saved while a cut was
    up carries them into the file. This is what makes the promise that a cut
    never survives a reload true without hooking the save itself: whatever came
    back is swept before the user ever sees it, and the bodies it was hiding are
    stored with their own visibility, which the sweep does not touch.
    """

    def slotFinishRestoreDocument(self, doc):
        try:
            removed = sweep(doc)
        except Exception:
            return
        if removed:
            FreeCAD.Console.PrintMessage(
                "Wavesim: removed {} stale cross-section preview object(s) "
                "from {}.\n".format(removed, getattr(doc, "Name", "?")))


_sweeper = None


def install_sweeper():
    """Register the reload sweep; idempotent, so a re-import cannot double it."""
    global _sweeper
    if _sweeper is None:
        _sweeper = _RestoreSweeper()
        FreeCAD.addDocumentObserver(_sweeper)
    return _sweeper


if _GUI_AVAILABLE:

    from wavesim_gui.commands import active_simulation

    class TaskCrossSectionPanel:
        """Task-tab panel: which axis to cut on, and where.

        Live, like the Domain panel, but **debounced**: the axis, the position,
        the side and the grid checkbox all schedule a recut rather than doing
        one, so holding an arrow key on the position spinner cannot queue a
        boolean per keystroke (the cut costs 40-630 ms depending on the model).

        OK leaves the cut standing -- the panel is exclusive, so a tool that
        restored the model on close could only ever be looked at through itself.
        Cancel puts back the bodies, the plane positions and the grid flags
        exactly as they were on open.
        """

        def __init__(self, sim, doc):
            try:
                from PySide import QtWidgets, QtCore
            except ImportError:
                from PySide import QtGui as QtWidgets
                from PySide import QtCore

            from wavesim_gui import domain as domain_mod

            self.sim = sim
            self.doc = doc
            self.domain = domain_mod.find_domain(sim)
            self.preview = CrossSectionPreview(doc)
            self._initializing = True
            # Pre-edit plane state, for Cancel.
            self._orig_positions, self._orig_show = (
                read_plane_state(self.domain) if self.domain is not None
                else ((0.0, 0.0, 0.0), (True, True, True)))

            form = QtWidgets.QWidget()
            form.setWindowTitle("Wavesim Cross Section")
            layout = QtWidgets.QFormLayout(form)

            # -- axis, one at a time (None is also the off switch) --------- #
            self._axis_group = QtWidgets.QButtonGroup(form)
            axis_row = QtWidgets.QWidget()
            axis_layout = QtWidgets.QHBoxLayout(axis_row)
            axis_layout.setContentsMargins(0, 0, 0, 0)
            self._axis_buttons = []
            for index, label in enumerate(("X", "Y", "Z", "None")):
                button = QtWidgets.QRadioButton(label)
                self._axis_group.addButton(button, index)
                axis_layout.addWidget(button)
                self._axis_buttons.append(button)
            self._axis_buttons[3].setToolTip(
                "Remove the cross-section and show the whole model again")
            self._axis_buttons[2].setChecked(True)      # z: the usual cut
            self._axis_group.buttonClicked.connect(self._on_axis_changed)
            layout.addRow("Cut normal to:", axis_row)

            # -- position -------------------------------------------------- #
            self._position = QtWidgets.QDoubleSpinBox()
            self._position.setDecimals(4)
            self._position.setSuffix(" mm")
            self._position.valueChanged.connect(self._schedule)
            layout.addRow("Position:", self._position)

            self._flip = QtWidgets.QCheckBox("Keep the far side instead")
            self._flip.setToolTip(
                "Which half of the model is kept; the cut plane does not move")
            self._flip.toggled.connect(self._schedule)
            layout.addRow("", self._flip)

            self._show_grid = QtWidgets.QCheckBox("Show the voxel grid on the cut")
            self._show_grid.setChecked(True)
            self._show_grid.setToolTip(
                "Draws the Domain's cell grid on the cut plane, and switches "
                "the other two grid planes off")
            self._show_grid.toggled.connect(self._schedule)
            layout.addRow("", self._show_grid)

            self._style = QtWidgets.QComboBox()
            self._style.addItems(list(CAP_STYLES))
            self._style.setToolTip(
                "How the exposed section face is marked. Shaded tints the cut "
                "face a darker shade of the body's own colour; Hatched draws "
                "cross-hatch lines over it; Plain leaves it in the body colour. "
                "Both markings are derived from the body colour so they stay "
                "quieter than the voxel grid.")
            self._style.currentIndexChanged.connect(self._schedule)
            layout.addRow("Section face:", self._style)

            self._info = QtWidgets.QLabel()
            self._info.setWordWrap(True)
            layout.addRow("", self._info)

            self._warning = QtWidgets.QLabel()
            self._warning.setWordWrap(True)
            self._warning.setStyleSheet("color: #b06000;")
            layout.addRow("", self._warning)

            # One shot, restarted on every edit: the last edit in a burst is the
            # only one that costs a cut.
            self._timer = QtCore.QTimer()
            self._timer.setSingleShot(True)
            self._timer.setInterval(_DEBOUNCE_MS)
            self._timer.timeout.connect(self._apply)

            self.form = form
            self._initializing = False
            self._reset_position()          # centres on the default axis...
            self._apply()                   # ...and cuts there straight away

        # -- reads --------------------------------------------------------- #

        def _axis(self):
            """0/1/2 for x/y/z, or ``None`` when the cross-section is off."""
            index = self._axis_group.checkedId()
            return None if index == 3 else index

        def _extent(self, axis):
            return axis_extent_mm(self.domain, self.doc, axis)

        # -- writes -------------------------------------------------------- #

        def _reset_position(self):
            """Centre the position on the current axis and re-range the spinner.

            Half way along the axis is the default the tool opens on, and the
            default it returns to whenever the axis changes -- the centre of a
            new axis is a useful place to be, and the old axis's number never is.
            """
            axis = self._axis()
            enabled = axis is not None
            for widget in (self._position, self._flip, self._show_grid,
                           self._style):
                widget.setEnabled(enabled)
            if not enabled:
                return
            lo, hi = self._extent(axis)
            self._position.blockSignals(True)
            self._position.setRange(min(lo, hi), max(lo, hi))
            self._position.setSingleStep(max((hi - lo) / 100.0, 1.0e-4))
            self._position.setValue(0.5 * (lo + hi))
            self._position.blockSignals(False)

        def _on_axis_changed(self, *_):
            if self._initializing:
                return
            self._reset_position()
            self._schedule()

        def _schedule(self, *_):
            if self._initializing:
                return
            self._timer.start()

        def _apply(self):
            """Recut the model and move the grid plane under it."""
            self._timer.stop()
            axis = self._axis()
            if axis is None:
                self.preview.clear()
                write_plane_state(self.domain, show=self._orig_show)
                self._refresh_labels(0)
                return

            position = float(self._position.value())
            show = [False, False, False]
            if self._show_grid.isChecked():
                show[axis] = True
            write_plane_state(self.domain, position_mm=position, axis=axis,
                              show=show)
            try:
                count = self.preview.apply(
                    axis, position, keep_low=not self._flip.isChecked(),
                    style=self._style.currentText())
            except Exception as exc:
                self.preview.clear()
                FreeCAD.Console.PrintError(
                    "Wavesim: could not build the cross-section ({}: {})\n"
                    .format(type(exc).__name__, exc))
                count = 0
            self._refresh_labels(count)

        # -- readout ------------------------------------------------------- #

        def _refresh_labels(self, count):
            axis = self._axis()
            if axis is None:
                self._info.setText("No cross-section; the whole model is shown.")
                self._warning.setText("")
                return
            lo, hi = self._extent(axis)
            span = hi - lo
            fraction = (self._position.value() - lo) / span * 100.0 if span else 0.0
            self._info.setText(
                "{} at {:.4g} mm ({:.0f}% of {:.4g}..{:.4g} mm), "
                "{} bod{} sectioned.".format(
                    AXES[axis].upper(), self._position.value(), fraction, lo, hi,
                    count, "y" if count == 1 else "ies"))
            notes = []
            if count == 0:
                notes.append(
                    "Nothing to cut here -- either no solid body is visible, or "
                    "the plane is past all of them.")
            if self.domain is None:
                notes.append(
                    "No Domain in this document, so there is no voxel grid to "
                    "draw and the range is the geometry's own.")
            self._warning.setText("\n".join(notes))

        # -- task panel protocol ------------------------------------------- #

        def accept(self):
            self._timer.stop()
            self._apply()
            Gui.Control.closeDialog()
            return True

        def reject(self):
            self._timer.stop()
            self.preview.clear()
            if self.domain is not None:
                for prop, value in zip(_POSITION_PROPS, self._orig_positions):
                    try:
                        setattr(self.domain, prop, "{} mm".format(value))
                    except Exception:
                        pass
                write_plane_state(self.domain, show=self._orig_show)
            Gui.Control.closeDialog()
            return True

        def getStandardButtons(self):
            try:
                from PySide import QtWidgets as _w
            except ImportError:
                from PySide import QtGui as _w
            buttons = _w.QDialogButtonBox.Ok | _w.QDialogButtonBox.Cancel
            return int(getattr(buttons, "value", buttons))

    class CommandCrossSection:
        """Cut the model open on a grid plane, capped and hatched."""

        def GetResources(self):
            return {
                "Pixmap": _XSEC_ICON,
                "MenuText": "Cross Section",
                "ToolTip": "Cut the model open on a grid plane -- capped and "
                           "hatched, not hollow -- and draw the voxel grid on "
                           "the cut",
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sim = active_simulation(doc)
            if sim is None:
                FreeCAD.Console.PrintWarning(
                    "Wavesim: create a Simulation before cross-sectioning.\n")
                return
            sweep(doc)          # anything an earlier panel or a crash left
            Gui.Control.closeDialog()
            Gui.Control.showDialog(TaskCrossSectionPanel(sim, doc))

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    Gui.addCommand("Wavesim_CrossSection", CommandCrossSection())

install_sweeper()
