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

A cut is a half-space, not a knife
---------------------------------
What the tool shows is **everything on the kept side of the plane**, which is
not the same thing as everything the plane cut. A body lying wholly beyond the
plane is cut by nothing and still has to go -- otherwise it floats, whole and
uncut, in front of a sectioned model and the picture is a lie about where the
plane is. So :func:`cut_shape` returns ``None`` for such a body and
:meth:`CrossSectionPreview.apply` hides it with the rest, preview or no preview.

The cut plane **is a grid plane**
--------------------------------
There is no second position property. The cut sits at the Domain's own
``GridPlaneX/Y/Z`` (:func:`domain.grid_plane_positions_mm`), so moving the cut
moves the voxel grid drawn on it, by construction rather than by syncing -- which
is the point of the tool: to read the mesh against the geometry it will
discretise. The other two grid planes are switched off through the Domain's
``ShowGridPlaneX/Y/Z``, because three grids at once over a sectioned model is
unreadable.

A toolbar, not a dialog
-----------------------
Looking inside a model is something you do repeatedly *while* building it, so
none of this is behind a task panel (there used to be one; it is gone). The
whole tool is one toolbar of its own: two checkable buttons -- the cut
(``Wavesim_CrossSectionToggle``) and the mesh grid on it
(``Wavesim_CrossSectionGrid``) -- and three raw widgets for the axis, the
position and which half is kept. Pick the axis, drag the position, press the
button; nothing to open, nothing to confirm, and nothing exclusive locking the
rest of the GUI while a cut is up.

The two switches are not independent. **The grid is the stronger one**: it
implies the cut, because a mesh plane through an unsectioned solid model is
exactly the picture the cut exists to replace. So switching the grid on raises a
cut under it, and switching the cut off takes the grid down with it.

All of it is one state, and that state is **on the Domain**
(:data:`_STATE_PROPS`), hidden, beside the plane positions that already hold the
cut's *position*. It has to be: the widgets set up a cut before it is switched
on, so their edits have to land somewhere a live cut is not. Everything on
screen comes back out of there through the one function :func:`apply_state`,
so the widgets and the buttons cannot drift apart -- and the buttons' checkmarks
are pushed back *from* the state (``_sync_toggle_actions``) rather than tracking
their own clicks, which is what makes the grid-implies-cut rule visible on the
other button.

The one setting with no control is the cap style, which is a taste you set once:
it stays as an ordinary Domain property, and the property editor is its UI.

Denoting the cut face
---------------------
Three styles (:data:`CAP_STYLES`), and the marking is **derived from the body's
own colour** in each -- blended toward black rather than fixed. That is what
keeps it quieter than the grey voxel grid whatever the material is tinted, and
the hierarchy matters: the grid is the thing being read, the section marking is
context. A fixed black hatch inverts that and becomes the loudest thing on
screen.

* ``CAP_SHADED`` (the default, and what a document gets unless somebody edits
  the Domain's ``WavesimXSecStyle``) -- no extra geometry at all. The cap faces are
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
as a document finishes loading -- and clears the state that says a cut is up, so
the toggle button agrees with the model -- so a cut saved by accident comes back
whole, bodies and all. It is also not live: edit the geometry or the mesh while
a cut is up and the preview is stale until it is re-applied.

Importing this module registers ``Wavesim_CrossSectionToggle`` and
``Wavesim_CrossSectionGrid`` with ``Gui.addCommand`` when a GUI is available.
The three widgets are not commands and cannot be registered: the workbench's
``Activated`` hook calls :func:`install_toolbar_controls` to push them into the
toolbar, every time, because FreeCAD tears that toolbar down on a workbench
switch.
"""

import math
import os

import FreeCAD


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_WB_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "wavesim-workbench")
_ICONS_DIR = os.path.join(_WB_DIR, "Resources", "icons")
# The cut's own icon is the on/off button: with the task panel gone there is
# nothing else for it to be.
_XSEC_ICON = os.path.join(_ICONS_DIR, "cross_section.svg")
_XSEC_GRID_ICON = os.path.join(_ICONS_DIR, "cross_section_grid.svg")

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
    OpenGL clip plane leaves. A shape wholly on the kept side comes back
    unchanged; one wholly on the discarded side comes back as ``None``, and the
    caller has to hide it -- see :meth:`CrossSectionPreview.apply`.
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
    """Tag *obj* as a cross-section preview and keep it out of the tree.

    ``ShowInTree = False`` alone does not hold: FreeCAD's tree re-inserts a
    hidden item as soon as it is *selected*, so one click on a cut face in the
    3D view put the whole section into the tree, where it can be renamed,
    hidden, or deleted out from under the panel. ``Selectable = False`` closes
    that door -- there is nothing to click, so nothing to sync into the tree,
    and picking still falls through to whatever the user actually meant to hit.
    A preview is a picture of the model, never a handle on it.
    """
    try:
        obj.addProperty("App::PropertyBool", _PREVIEW_PROP, "Wavesim",
                        "Transient cross-section preview; not part of the model")
        setattr(obj, _PREVIEW_PROP, True)
        obj.setEditorMode(_PREVIEW_PROP, 2)
    except Exception:
        pass
    vobj = getattr(obj, "ViewObject", None)
    if vobj is not None:
        for prop, value in (("ShowInTree", False), ("Selectable", False)):
            try:
                setattr(vobj, prop, value)
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
                and getattr(self.doc, "Name", None) in FreeCAD.listDocuments())

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
        """Cut every visible solid at ``axis == coord``.

        Returns ``(sectioned, removed)`` -- how many bodies came back with
        something left to draw, and how many lay wholly on the discarded side.

        Rebuilds from scratch each time -- the sources are read *before* anything
        is hidden, so re-applying cuts the real bodies rather than the last cut.

        **A body the plane does not reach is still on the wrong side of it.**
        The cut is a half-space, not a knife: what the tool shows is everything
        on the kept side, so a body lying entirely beyond the plane has to go
        even though there was nothing to cut. It gets no preview and is hidden
        like any other source -- which is the one thing that used to be missed,
        leaving un-cut bodies floating in front of a sectioned model.
        """
        from wavesim_gui import visibility

        if not self._alive():
            return (0, 0)
        self.clear()
        sources = visible_solids(self.doc)
        if not sources:
            return (0, 0)

        cuts, removed = [], []
        for obj in sources:
            shape = cut_shape(obj.Shape, axis, coord, keep_low)
            if shape is None:
                removed.append(obj)     # wholly beyond the plane
            else:
                cuts.append((obj, shape))

        with visibility.suppressed():
            for obj, _shape in cuts:
                _hide_source(obj)
            for obj in removed:
                _hide_source(obj)
        if not cuts:
            return (0, len(removed))

        pitch = _hatch_pitch([shape for _obj, shape in cuts], axis)
        for obj, shape in cuts:
            self._add_body(obj, shape, axis, coord, style)
            if style == CAP_HATCHED:
                self._add_hatch(obj, shape, axis, coord, keep_low, pitch)
        self._show()
        return (len(cuts), len(removed))

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
                    ("LineWidth", _HATCH_WIDTH)):
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
    (:func:`domain.grid_plane_positions_mm`). Falls back to the geometry itself
    for a document whose domain has not been sized yet -- a domain is sized from
    the bodies that have a *material*, so this is the whole range before the
    first material is assigned.

    That fallback counts the bodies **the cut has hidden** as well as the
    visible ones. It has to: hiding them is what a cut does, so a fallback that
    looked only at what is on screen would collapse to nothing the moment a cut
    went up -- and the position control, ranged on it, would trap the cut where
    it stood.
    """
    mn = getattr(domain, "DomainMin", None) if domain is not None else None
    mx = getattr(domain, "DomainMax", None) if domain is not None else None
    if mn is not None and mx is not None and (mn - mx).Length > 1.0e-9:
        return (getattr(mn, AXES[axis]), getattr(mx, AXES[axis]))
    bbox = FreeCAD.BoundBox()
    sources = list(visible_solids(doc))
    for obj in (getattr(doc, "Objects", []) or []):
        shape = getattr(obj, "Shape", None)
        if (getattr(obj, _HIDDEN_PROP, False) and shape is not None
                and not shape.isNull() and shape.Solids):
            sources.append(obj)
    for obj in sources:
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
# The cut as state: what the toggles switch
# --------------------------------------------------------------------------- #
#
# The cut has three buttons, not one: *configure* opens the panel, *toggle* puts
# the last configuration up and takes it down, and *grid* does the same for the
# mesh drawn on the cut plane. Two of those have to raise a cut nobody is
# looking at a panel for, so the configuration cannot live in the panel -- it
# lives on the Domain, hidden, beside the plane positions it already owns.
#
# The position is **not** duplicated here. It is ``GridPlaneX/Y/Z``, which is
# the whole premise of the tool (see the module docstring); only which axis,
# which side, how the cap is marked, and whether each switch is on are stored.
#
# The grid is the stronger switch: it implies the cut. Grid on with no cut would
# be a mesh plane floating through a solid model -- the thing the cut exists to
# make readable -- so :func:`set_grid` turns the cut on with it, and turning the
# cut off takes the grid down too.

_STATE_AXIS = "WavesimXSecAxis"      # 0/1/2, or -1 for "never configured"
_STATE_FLIP = "WavesimXSecFlip"      # keep the far side instead
_STATE_STYLE = "WavesimXSecStyle"    # one of CAP_STYLES
_STATE_GRID = "WavesimXSecGrid"      # draw the voxel grid on the cut plane
_STATE_ON = "WavesimXSecOn"          # is a cut up right now

# What the property editor says about them. Only the cap style is shown there
# (see ensure_state_props), so it is the only one that needs to read well.
_STATE_DOC = {
    _STATE_STYLE: ("How the exposed section face is marked: Shaded tints it a "
                   "darker shade of the body's own colour, Hatched draws "
                   "cross-hatch lines over it, Plain leaves it in the body "
                   "colour. Both markings derive from the body colour, so "
                   "they stay quieter than the voxel grid drawn on the cut."),
}

_STATE_PROPS = (
    (_STATE_AXIS, "App::PropertyInteger", -1),
    (_STATE_FLIP, "App::PropertyBool", False),
    (_STATE_STYLE, "App::PropertyEnumeration", CAP_SHADED),
    (_STATE_GRID, "App::PropertyBool", False),
    (_STATE_ON, "App::PropertyBool", False),
)

# The axis a cut opens on when the tool has never been configured in this
# document: z, the usual one to look down on a layered model.
_DEFAULT_AXIS = 2


def ensure_state_props(domain):
    """Add the cross-section state properties to *domain* if it lacks them.

    Hidden from the property editor: they are the tool's own bookkeeping, and
    the two that mean anything on their own (position, grid visibility) are
    already editable as ``GridPlane*``/``ShowGridPlane*``.
    """
    if domain is None:
        return False
    for prop, kind, default in _STATE_PROPS:
        if hasattr(domain, prop):
            continue
        try:
            domain.addProperty(kind, prop, "Grid", _STATE_DOC.get(prop, ""))
            if kind == "App::PropertyEnumeration":
                setattr(domain, prop, list(CAP_STYLES))
            setattr(domain, prop, default)
            # All but the cap style: those four are switch positions the two
            # buttons own, and a second way to set them from the property
            # editor could only ever disagree with the buttons.
            if prop != _STATE_STYLE:
                domain.setEditorMode(prop, 2)
        except Exception:
            return False
    return True


def find_domain(doc):
    """The Domain of *doc*'s simulation, or ``None``."""
    from wavesim_gui import commands, domain as domain_mod

    sim = commands.active_simulation(doc)
    return None if sim is None else domain_mod.find_domain(sim)


def read_state(domain, doc=None):
    """The cut's stored configuration as a dict, defaults filled in.

    ``axis`` is never ``None`` here -- an unconfigured document reads back as
    the default axis centred in the domain, which is what a toggle needs to be
    able to raise a cut with nothing configured yet.
    """
    axis = int(getattr(domain, _STATE_AXIS, -1)) if domain is not None else -1
    configured = 0 <= axis <= 2
    if not configured:
        axis = _DEFAULT_AXIS
    positions, _show = (read_plane_state(domain) if domain is not None
                        else ((0.0, 0.0, 0.0), (True, True, True)))
    position = positions[axis]
    if not configured:
        lo, hi = axis_extent_mm(domain, doc, axis)
        position = 0.5 * (lo + hi)
    style = str(getattr(domain, _STATE_STYLE, CAP_SHADED) or CAP_SHADED)
    return {
        "axis": axis,
        "configured": configured,
        "position": position,
        "flip": bool(getattr(domain, _STATE_FLIP, False)),
        "style": style if style in CAP_STYLES else CAP_SHADED,
        "grid": bool(getattr(domain, _STATE_GRID, False)),
        "on": bool(getattr(domain, _STATE_ON, False)),
    }


def write_state(domain, **values):
    """Store any of ``axis``/``flip``/``style``/``grid``/``on`` on *domain*."""
    if domain is None:
        return
    ensure_state_props(domain)
    for key, prop in (("axis", _STATE_AXIS), ("flip", _STATE_FLIP),
                      ("style", _STATE_STYLE), ("grid", _STATE_GRID),
                      ("on", _STATE_ON)):
        if key not in values:
            continue
        try:
            setattr(domain, prop, values[key])
        except Exception:
            pass


def apply_state(doc, domain=None):
    """Put *doc* into the state its Domain describes; returns ``(kept, removed)``.

    The single path from stored state to what is on screen, so the toggles, the
    panel and a re-apply after an edit all raise exactly the same picture.
    ``(0, 0)`` when the cut is off -- which also takes the grid planes down,
    because the grid is only ever drawn on a cut.
    """
    if domain is None:
        domain = find_domain(doc)
    if domain is None:
        sweep(doc)
        return (0, 0)
    state = read_state(domain, doc)
    if not state["on"]:
        sweep(doc)
        write_plane_state(domain, show=(False, False, False))
        return (0, 0)

    axis = state["axis"]
    show = [False, False, False]
    show[axis] = state["grid"]
    write_plane_state(domain, position_mm=state["position"], axis=axis,
                      show=show)
    try:
        return CrossSectionPreview(doc).apply(
            axis, state["position"], keep_low=not state["flip"],
            style=state["style"])
    except Exception as exc:
        sweep(doc)
        FreeCAD.Console.PrintError(
            "Wavesim: could not build the cross-section ({}: {})\n"
            .format(type(exc).__name__, exc))
        return (0, 0)


def _adopt_defaults(domain, doc):
    """Write the opening configuration for a document that has never had one."""
    state = read_state(domain, doc)
    if state["configured"]:
        return state
    write_state(domain, axis=state["axis"])
    write_plane_state(domain, position_mm=state["position"], axis=state["axis"])
    return read_state(domain, doc)


def is_active(doc):
    """Whether a cut is up in *doc*."""
    domain = find_domain(doc)
    return domain is not None and bool(getattr(domain, _STATE_ON, False))


def is_grid_on(doc):
    """Whether the voxel grid is drawn on the cut in *doc*."""
    domain = find_domain(doc)
    return (domain is not None and bool(getattr(domain, _STATE_ON, False))
            and bool(getattr(domain, _STATE_GRID, False)))


def set_active(doc, on):
    """Raise or take down the cut, keeping the grid switch consistent.

    Turning the cut off turns the grid off with it: the grid is drawn on the
    cut plane and means nothing without one. Returns what :func:`apply_state`
    did, as ``(sectioned, removed)``.
    """
    domain = find_domain(doc)
    if domain is None:
        return False
    ensure_state_props(domain)
    if on:
        _adopt_defaults(domain, doc)
        write_state(domain, on=True)
    else:
        write_state(domain, on=False, grid=False)
    return apply_state(doc, domain)


def set_grid(doc, on):
    """Switch the mesh grid on the cut plane -- turning the cut on if needed.

    Returns what :func:`apply_state` did, as ``(sectioned, removed)``.
    """
    domain = find_domain(doc)
    if domain is None:
        return False
    ensure_state_props(domain)
    if on:
        _adopt_defaults(domain, doc)
        write_state(domain, grid=True, on=True)
    else:
        write_state(domain, grid=False)
    return apply_state(doc, domain)


# --------------------------------------------------------------------------- #
# GUI: task panel + commands
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

    def slotCreatedObject(self, obj):
        """A Domain appearing gives the toolbar controls a range to read.

        Creating the Simulation does not change which document is active, so
        the active-document watcher never hears about it -- and the controls
        would go on showing the 0..0 range of a document that had no Domain
        when they were built.
        """
        if not (hasattr(obj, _POSITION_PROPS[0])
                or getattr(obj, "WavesimType", None) == "Simulation"):
            return
        try:
            refresh_toolbar_controls()
        except Exception:      # console mode: no controls to refresh
            pass

    def slotFinishRestoreDocument(self, doc):
        try:
            removed = sweep(doc)
            # ...and the state that says a cut is up, so the toggle button
            # agrees with the model the user is looking at. The grid planes go
            # down with it: a grid is drawn on a cut, so a document saved with
            # one showing must not come back with a mesh plane hanging in an
            # uncut model. This is also the migration for a document written
            # before the grid had its own button, when all three defaulted on.
            domain = find_domain(doc)
            if domain is not None:
                write_state(domain, on=False, grid=False)
                write_plane_state(domain, show=(False, False, False))
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

    # The two checkable commands, so any path that changes the state can put
    # their buttons back in step -- the panel, and each other (switching the
    # grid on switches the cut on under it).
    _TOGGLE_COMMAND = "Wavesim_CrossSectionToggle"
    _GRID_COMMAND = "Wavesim_CrossSectionGrid"

    def _qaction_class():
        """``QAction``, which moved from QtWidgets to QtGui in Qt 6."""
        try:
            from PySide import QtGui

            if hasattr(QtGui, "QAction"):
                return QtGui.QAction
        except ImportError:
            pass
        from PySide import QtWidgets

        return QtWidgets.QAction

    def _sync_toggle_actions(doc):
        """Put the two toolbar buttons' checkmarks back in step with the state.

        FreeCAD names a command's ``QAction`` after the command, so the buttons
        can be found and set without holding a reference to them -- which the
        commands cannot do anyway, since FreeCAD builds one action per toolbar
        and per menu from the same command. Signals are blocked around the set:
        this is a readout of the state, not a click.
        """
        try:
            window = Gui.getMainWindow()
            if window is None:
                return
            wanted = {_TOGGLE_COMMAND: is_active(doc),
                      _GRID_COMMAND: is_grid_on(doc)}
            for action in window.findChildren(_qaction_class()):
                state = wanted.get(action.objectName())
                if state is None or not action.isCheckable():
                    continue
                blocked = action.blockSignals(True)
                action.setChecked(bool(state))
                action.blockSignals(blocked)
        except Exception:
            pass
        try:
            refresh_toolbar_controls()
        except Exception:
            pass

    # ----------------------------------------------------------------- #
    # The settings, on the toolbar
    # ----------------------------------------------------------------- #
    #
    # There is no task panel. Which axis, where, and which half is a setting
    # you change *while looking at the model* -- often several times a minute
    # -- and a modal task panel is the wrong shape for that: it has to be
    # opened, it is exclusive (nothing else can be edited while it is up), and
    # it puts an OK button between the user and a cut they can already see.
    # So the three settings are toolbar widgets sitting next to the two buttons
    # they feed, and the sequence the tool is used in -- pick the axis, drag the
    # position, press the toggle -- is one row of controls with no dialog in it.
    #
    # The price is that a toolbar widget is not a FreeCAD command: it is a raw
    # ``QWidget`` pushed into the ``QToolBar`` behind FreeCAD's back, so it has
    # no menu entry, no shortcut, and FreeCAD's Customize dialog cannot see it.
    # It also has to be rebuilt on every workbench activation, because FreeCAD
    # tears the toolbar down when the user switches away and back.

    _TOOLBAR = "Wavesim Cross Section"

    # Object names for the widgets we push into that toolbar. They are how a
    # re-activation finds the ones it left behind, so it can take them out
    # instead of adding a second set beside them.
    _AXIS_WIDGET = "WavesimXSecAxisBox"
    _POSITION_WIDGET = "WavesimXSecPositionBox"
    _FLIP_WIDGET = "WavesimXSecFlipBox"
    _OUR_WIDGETS = (_AXIS_WIDGET, _POSITION_WIDGET, _FLIP_WIDGET)

    # A cut sitting on the domain wall shows nothing at all, so a plane that has
    # never been moved off it is centred when its axis is picked instead. This
    # is how far off the wall (as a fraction of the axis span) still counts as
    # "on it".
    _WALL_FRAC = 1.0e-6


    class _CrossSectionControls(object):
        """Axis, position and flip, as widgets on the cross-section toolbar.

        Edits go **into the stored state** whether or not a cut is up: setting
        the controls with the cut off and then pressing the toggle is the way
        the tool is meant to be used, so the settings cannot be something only
        a live cut remembers. When a cut *is* up the edit also recuts, on the
        same debounce the panel used (:data:`_DEBOUNCE_MS`) -- holding an arrow
        key on the position spinner must not queue one boolean per keystroke.
        """

        def __init__(self):
            self._axis = None
            self._position = None
            self._flip = None
            self._timer = None
            self._filter = None
            self._loading = False

        # -- installation ---------------------------------------------- #

        @staticmethod
        def _toolbar():
            try:
                from PySide import QtWidgets
            except ImportError:
                from PySide import QtGui as QtWidgets
            window = Gui.getMainWindow()
            if window is None:
                return None
            return window.findChild(QtWidgets.QToolBar, _TOOLBAR)

        def install(self):
            """Build the widgets into the toolbar, replacing any we left there.

            Idempotent by demolition rather than by detection: FreeCAD may have
            destroyed the widgets with the toolbar, in which case the Python
            references we hold point at deleted C++ objects and cannot be told
            apart from live ones without touching them. Taking out anything
            carrying our names and building fresh is the version with no
            question in it.
            """
            toolbar = self._toolbar()
            if toolbar is None:
                return False
            self._remove_ours(toolbar)
            self._build(toolbar)
            _force_new_row(toolbar)
            self.refresh()
            # FreeCAD builds a checkable command's action **checked**, so
            # without this both buttons come up pressed over a model with no
            # cut in it. The state is the truth; the actions are a readout.
            _sync_toggle_actions(FreeCAD.ActiveDocument)
            return True

        @staticmethod
        def _remove_ours(toolbar):
            for action in list(toolbar.actions()):
                try:
                    widget = action.defaultWidget()
                except Exception:
                    continue
                if widget is not None and widget.objectName() in _OUR_WIDGETS:
                    toolbar.removeAction(action)

        def _build(self, toolbar):
            try:
                from PySide import QtWidgets, QtCore
            except ImportError:
                from PySide import QtGui as QtWidgets
                from PySide import QtCore

            self._axis = QtWidgets.QComboBox()
            self._axis.setObjectName(_AXIS_WIDGET)
            self._axis.addItems([name.upper() for name in AXES])
            self._axis.setToolTip("Which axis the cut plane is normal to")
            self._axis.currentIndexChanged.connect(self._on_axis_changed)

            self._position = QtWidgets.QDoubleSpinBox()
            self._position.setObjectName(_POSITION_WIDGET)
            self._position.setDecimals(4)
            self._position.setSuffix(" mm")
            self._position.setKeyboardTracking(False)
            self._position.setToolTip(
                "Where the cut plane sits on that axis. This is the Domain's "
                "own grid plane, so the drawn mesh moves with it")
            self._position.valueChanged.connect(self._schedule)

            self._flip = QtWidgets.QCheckBox("Flip")
            self._flip.setObjectName(_FLIP_WIDGET)
            self._flip.setToolTip(
                "Which half of the model is kept; the cut plane does not move. "
                "A body lying entirely on the discarded side is hidden, cut "
                "or not")
            self._flip.toggled.connect(self._schedule)

            self._filter = _reach_filter()(self)
            for widget in (self._axis, self._position, self._flip):
                widget.installEventFilter(self._filter)
                toolbar.addWidget(widget)

            self._timer = QtCore.QTimer()
            self._timer.setSingleShot(True)
            self._timer.setInterval(_DEBOUNCE_MS)
            self._timer.timeout.connect(self._apply)

        def _alive(self):
            """Whether our widgets still exist on the C++ side."""
            try:
                return (self._axis is not None
                        and self._axis.objectName() == _AXIS_WIDGET)
            except Exception:
                return False

        # -- state in, state out --------------------------------------- #

        def refresh(self):
            """Show what the Domain says, without setting anything off.

            The one direction the controls are ever written from. Called after
            every toggle too, because the first switch-on adopts a default axis
            -- and a control that did not follow that would be lying about the
            cut on screen.
            """
            if not self._alive():
                return
            doc = FreeCAD.ActiveDocument
            domain = find_domain(doc)
            for widget in (self._axis, self._position, self._flip):
                widget.setEnabled(domain is not None)
            if domain is None:
                return
            state = read_state(domain, doc)
            self._loading = True
            try:
                self._axis.setCurrentIndex(state["axis"])
                self._range(state["axis"])
                self._position.setValue(state["position"])
                self._flip.setChecked(state["flip"])
            finally:
                self._loading = False

        def refresh_if_idle(self):
            """:meth:`refresh`, unless the position box is being typed into.

            Re-reading the state writes the spinner, so doing it under a
            half-typed number would eat the number.
            """
            try:
                if self._position is not None and self._position.hasFocus():
                    return
            except Exception:
                return
            self.refresh()

        def _range(self, axis):
            """Re-range the spinner over the domain's extent on *axis*."""
            lo, hi = axis_extent_mm(find_domain(FreeCAD.ActiveDocument),
                                    FreeCAD.ActiveDocument, axis)
            self._position.setRange(min(lo, hi), max(lo, hi))
            self._position.setSingleStep(max(abs(hi - lo) / 100.0, 1.0e-4))

        def _on_axis_changed(self, index):
            """Move to the new axis's own plane -- or to its middle.

            The position of each axis's cut is that axis's grid plane, so
            switching axis is a jump to wherever that plane was left. The
            exception is a plane that has never been moved: it sits on the
            domain wall, where a cut shows either everything or nothing, so it
            is centred instead. That is the one case where remembering is
            worse than defaulting.
            """
            if self._loading or not 0 <= index <= 2:
                return
            doc = FreeCAD.ActiveDocument
            domain = find_domain(doc)
            if domain is None:
                return
            lo, hi = axis_extent_mm(domain, doc, index)
            span = abs(hi - lo)
            position = read_plane_state(domain)[0][index]
            position = min(max(position, min(lo, hi)), max(lo, hi))
            if span > 0.0 and min(abs(position - lo),
                                  abs(position - hi)) <= span * _WALL_FRAC:
                position = 0.5 * (lo + hi)
            self._loading = True
            try:
                self._range(index)
                self._position.setValue(position)
            finally:
                self._loading = False
            self._schedule()

        def _schedule(self, *_):
            if not self._loading:
                self._timer.start()

        def _apply(self):
            """Store the settings, and recut if a cut is up.

            Storing happens either way. The controls are how a cut is set up
            *before* it is switched on, so an edit with the cut down has to
            land somewhere -- and the only somewhere is the Domain.
            """
            self._timer.stop()
            if not self._alive():
                return
            doc = FreeCAD.ActiveDocument
            domain = find_domain(doc)
            if domain is None:
                return
            axis = self._axis.currentIndex()
            write_plane_state(domain, position_mm=float(self._position.value()),
                              axis=axis)
            write_state(domain, axis=axis, flip=self._flip.isChecked())
            if bool(getattr(domain, _STATE_ON, False)):
                _report(apply_state(doc, domain))


    _controls = None
    _reach_filter_class = None

    # How long to wait before asking for the toolbar break (see _force_new_row).
    _BREAK_DELAY_MS = 500


    def _reach_filter():
        """The event filter that refreshes a control as it is reached for.

        The observers cover the events that *change* which state the controls
        should show (a document activated, a Domain created). This covers the
        rest: everything else that can stale them -- a body added, a domain
        resized, an undo -- happens without a signal we watch, and the cheapest
        honest answer is to re-read the state when the pointer arrives on the
        control, just before it is used.

        Built on first use, never at import: this module is imported by
        headless checks under ``freecadcmd``, where there is no PySide to
        subclass ``QObject`` from.
        """
        global _reach_filter_class
        if _reach_filter_class is None:
            from PySide import QtCore

            class _RefreshOnReach(QtCore.QObject):

                def __init__(self, controls):
                    QtCore.QObject.__init__(self)
                    self._controls = controls

                def eventFilter(self, _obj, event):
                    try:
                        if event.type() in (QtCore.QEvent.Enter,
                                            QtCore.QEvent.FocusIn):
                            self._controls.refresh_if_idle()
                    except Exception:
                        pass
                    return False

            _reach_filter_class = _RefreshOnReach
        return _reach_filter_class


    def _force_new_row(toolbar):
        """Put the cross-section toolbar on a line of its own, once.

        Qt packs toolbars onto the same row while there is space, and the point
        of this one is that it is a second row of controls under the workbench's
        buttons.

        **Deferred through the event loop**, because FreeCAD restores its saved
        window layout *after* it activates a workbench: a break inserted inline
        here is registered and then overwritten, which is exactly what the
        first version did -- ``toolBarBreak()`` answered True while the two
        toolbars sat on one row. Once per session either way: the break becomes
        part of the layout, which the user is free to rearrange and FreeCAD
        saves.
        """
        global _row_forced
        if _row_forced:
            return
        _row_forced = True
        try:
            from PySide import QtCore

            QtCore.QTimer.singleShot(_BREAK_DELAY_MS, lambda: _insert_break(toolbar))
        except Exception:
            pass


    def _insert_break(toolbar):
        try:
            window = Gui.getMainWindow()
            if window is not None and not window.toolBarBreak(toolbar):
                window.insertToolBarBreak(toolbar)
        except Exception:
            pass


    _row_forced = False


    def _report(counts):
        """Say what the cut did, in the status bar rather than in a panel.

        The count used to be a label in the task panel; with no panel it goes
        where a transient readout belongs. The one case worth more than that --
        a plane past every body, which empties the screen and looks like a
        broken tool -- also goes to the report view.
        """
        count, removed = counts
        message = "Wavesim: {} bod{} sectioned{}.".format(
            count, "y" if count == 1 else "ies",
            "" if not removed else
            ", {} hidden past the plane".format(removed))
        try:
            window = Gui.getMainWindow()
            if window is not None:
                window.statusBar().showMessage(message, 4000)
        except Exception:
            pass
        if count == 0 and removed:
            FreeCAD.Console.PrintWarning(
                "Wavesim: the cut plane is past every body -- the whole model "
                "is on the discarded side, so nothing is drawn.\n")


    class _ActiveDocumentWatcher(object):
        """Refreshes the toolbar controls when the active document changes.

        Without it the controls keep showing the document they were built over
        -- and, since they are built when the workbench is activated, that is
        usually *no* document at all: an axis of X and a 0..0 position over a
        model that has a Domain and a cut. They are a readout of one document's
        state, so they have to be told when that document is no longer the one
        being looked at.
        """

        def slotActivateDocument(self, _doc):
            try:
                refresh_toolbar_controls()
                _sync_toggle_actions(FreeCAD.ActiveDocument)
            except Exception:
                pass

    _doc_watcher = None


    def _install_document_watcher():
        """Register the active-document refresh; idempotent."""
        global _doc_watcher
        if _doc_watcher is None:
            _doc_watcher = _ActiveDocumentWatcher()
            try:
                Gui.addDocumentObserver(_doc_watcher)
            except Exception:
                _doc_watcher = None
        return _doc_watcher


    def install_toolbar_controls(retry=True):
        """Build (or rebuild) the toolbar widgets. Called on workbench activation.

        FreeCAD builds the toolbars around the activation hook rather than
        strictly before it, so a first activation can arrive before the
        toolbar exists. One deferred retry through the event loop covers that
        without a poll.
        """
        global _controls
        if _controls is None:
            _controls = _CrossSectionControls()
        _install_document_watcher()
        if _controls.install():
            return True
        if retry:
            try:
                from PySide import QtCore

                QtCore.QTimer.singleShot(
                    0, lambda: install_toolbar_controls(retry=False))
            except Exception:
                pass
        return False


    def refresh_toolbar_controls():
        """Make the widgets show what the state says."""
        if _controls is not None:
            _controls.refresh()


    class _CommandToggle(object):
        """Shared behaviour for the two checkable buttons.

        Both do the same three things: read what the user asked for off the
        action, ask the module to make it so, and then set every copy of both
        buttons to what the state actually *is*. That last step is not a
        formality -- switching the grid on switches the cut on under it, and the
        cut's own button has to show that.
        """

        _setter = None          # set_active / set_grid

        def _toggle(self, index):
            doc = FreeCAD.ActiveDocument
            if index is None:
                index = not self._current(doc)
            counts = type(self)._setter(doc, bool(index))
            _sync_toggle_actions(doc)
            if index:
                _report(counts)

        def Activated(self, index=None):
            self._toggle(index)

        def IsActive(self):
            return active_simulation(FreeCAD.ActiveDocument) is not None

    class CommandCrossSectionToggle(_CommandToggle):
        """Put the configured cut up, or take it down."""

        _setter = staticmethod(set_active)

        @staticmethod
        def _current(doc):
            return is_active(doc)

        def GetResources(self):
            return {
                "Pixmap": _XSEC_ICON,
                "MenuText": "Cross Section On/Off",
                "Checkable": True,
                "ToolTip": "Show or hide the cross-section, using the settings "
                           "the Cross Section panel last configured. Anything "
                           "on the cut-away side goes with it, cut or not",
            }

    class CommandCrossSectionGrid(_CommandToggle):
        """Draw the voxel grid on the cut plane, or stop drawing it."""

        _setter = staticmethod(set_grid)

        @staticmethod
        def _current(doc):
            return is_grid_on(doc)

        def GetResources(self):
            return {
                "Pixmap": _XSEC_GRID_ICON,
                "MenuText": "Mesh Grid On/Off",
                "Checkable": True,
                "ToolTip": "Draw the Domain's cell grid on the cut plane. The "
                           "grid is only ever drawn on a cut, so switching it "
                           "on cuts the model as well",
            }

    Gui.addCommand(_TOGGLE_COMMAND, CommandCrossSectionToggle())
    Gui.addCommand(_GRID_COMMAND, CommandCrossSectionGrid())

install_sweeper()
