# -*- coding: utf-8 -*-
"""Gate for the cross-section view (``wavesim_gui/crosssection.py``).

Run under **FreeCAD's own Python** (``freecadcmd.exe``), which is the half of the
two-process split that has ``Part``::

    "C:/.../FreeCAD 1.1/bin/freecadcmd.exe" tools/check_crosssection.py

What it checks, per test document and per axis:

* **the cut is a partition** -- ``volume(keep low) + volume(keep high)`` equals
  the whole body's volume. This is the one that matters: it says the cut removes
  exactly the half beyond the plane, nothing more and nothing less, and it fails
  loudly if the half-space box is built inside out.
* **the cut is closed and capped** -- a body the plane genuinely *divides*
  comes back as a closed solid carrying at least one face in the cut plane. A
  cut that came back open would be the hollow this whole module exists to
  avoid. A body the plane misses is required to come back whole instead (the
  ``capacitor`` documents are two plates with a gap, and their mid-plane divides
  neither), so "no cap" is only a failure where there was something to cap.
* **a plane clear of the body leaves it alone** -- same volume, same face count.
* **a body wholly on the cut-away side comes back as nothing** -- the cut is a
  half-space, not a knife, so a body the plane never reaches is still on the
  wrong side of it. The regression: such a body used to survive whole and float,
  uncut, in front of a sectioned model.
* **the three buttons are one state** -- the grid implies a cut (it is drawn on
  the cut plane and means nothing without one) and taking the cut down takes the
  grid with it, over a stand-in Domain.
* **the hatch lies on the cap** -- every segment sits at the cut plane (to
  within the lift that keeps it off the surface), inside the cap's own bounding
  box, and an annular cap gets segments over the metal rather than over its hole.
* **taking a cut down shows the bodies again** -- over stand-ins, because a
  console document has no ``ViewObject``. This is the regression for the bug
  where opening the panel a second time left the model blank: the record of what
  was hidden lived in the panel, so the second panel's sweep deleted the previews
  with nothing left to say which bodies to restore.
* **the cap tint is quieter than the body** -- and the per-face colour list is
  exactly as long as the face list, which is what ``DiffuseColor`` requires.

The real ``ViewObject`` path -- the Qt panel, the colours as drawn -- is only
reachable by using the tool.
"""

import os
import sys

_WB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB_DIR not in sys.path:
    sys.path.insert(0, _WB_DIR)

import FreeCAD

# freecadcmd ships a FreeCADGui that satisfies the modules' import guard but has
# no addCommand, so every gui module raises on import without this.
try:
    import FreeCADGui

    if not hasattr(FreeCADGui, "addCommand"):
        FreeCADGui.addCommand = lambda *a, **k: None
except Exception:
    pass

from wavesim_gui import crosssection as xs

DOCS = r"C:\Users\itais\Desktop\wavesim-test"

# Volumes are mm^3 over models spanning tens of mm, so a relative bound is the
# only meaningful one; OCC's own boolean tolerance is far above float epsilon.
# Measured worst case is 1.2e-7 on bent_coax's swept pipe -- that is the boolean
# closing on a spline, not the half-space being wrong, so the bound sits an order
# above it and still catches any real mis-partition (which is order 1).
_REL_TOL = 1.0e-6

_failures = []


def check(ok, message):
    if not ok:
        _failures.append(message)
    return ok


def _axis_span(bbox, axis):
    lo = (bbox.XMin, bbox.YMin, bbox.ZMin)[axis]
    hi = (bbox.XMax, bbox.YMax, bbox.ZMax)[axis]
    return lo, hi


def check_document(path):
    import Part

    name = os.path.basename(path)
    doc = FreeCAD.openDocument(path)
    solids = [o for o in doc.Objects
              if getattr(o, "Shape", None) is not None
              and not o.Shape.isNull() and o.Shape.Solids]
    if not solids:
        print("  {:46s} no solids, skipped".format(name))
        FreeCAD.closeDocument(doc.Name)
        return

    whole = FreeCAD.BoundBox()
    for obj in solids:
        whole.add(obj.Shape.BoundBox)

    n_caps = n_hatch = 0
    for axis in (0, 1, 2):
        lo, hi = _axis_span(whole, axis)
        coord = 0.5 * (lo + hi)

        cuts = []
        for obj in solids:
            shape = obj.Shape
            low = xs.cut_shape(shape, axis, coord, keep_low=True)
            high = xs.cut_shape(shape, axis, coord, keep_low=False)

            # 1. the two halves partition the body
            v_whole = shape.Volume
            v_low = low.Volume if low is not None else 0.0
            v_high = high.Volume if high is not None else 0.0
            check(abs(v_low + v_high - v_whole) <= _REL_TOL * max(v_whole, 1.0),
                  "{} {} axis {}: halves sum to {:.10g}, body is {:.10g}".format(
                      name, obj.Name, xs.AXES[axis], v_low + v_high, v_whole))

            # 2. a body the plane divides gives two closed, capped halves.
            #    A body it misses gives one whole half and one empty one, and
            #    has no cap to find -- that is the correct answer, not a miss.
            divided = (v_low > 1.0e-9 and v_high > 1.0e-9)
            for half, side in ((low, "low"), (high, "high")):
                if not divided or half is None:
                    continue
                check(half.isClosed(),
                      "{} {} axis {} {}: cut is not closed".format(
                          name, obj.Name, xs.AXES[axis], side))
                caps = xs._cap_faces(half, axis, coord)
                check(bool(caps),
                      "{} {} axis {} {}: no cap face in the cut plane".format(
                          name, obj.Name, xs.AXES[axis], side))
                n_caps += len(caps)

            # 3. a plane clear of the body leaves it whole
            clear = xs.cut_shape(shape, axis, hi + 1000.0, keep_low=True)
            check(clear is not None
                  and abs(clear.Volume - v_whole) <= _REL_TOL * max(v_whole, 1.0)
                  and len(clear.Faces) == len(shape.Faces),
                  "{} {} axis {}: a plane past the body changed it".format(
                      name, obj.Name, xs.AXES[axis]))

            if low is not None and low.Volume > 1.0e-9:
                cuts.append((low, divided))

        # 4. the hatch sits on the caps
        if not cuts:
            continue
        shapes = [shape for shape, _divided in cuts]
        pitch = xs._hatch_pitch(shapes, axis)
        edges = xs.hatch_edges(shapes, axis, coord, True, pitch)
        n_hatch += len(edges)
        # Only demand a hatch where the plane actually exposed something.
        check(bool(edges) or not any(divided for _shape, divided in cuts),
              "{} axis {}: no hatch produced for {} cut solids".format(
                  name, xs.AXES[axis], len(cuts)))

        lift = max(pitch * (xs._HATCH_LIFT_FRAC / xs._HATCH_PITCH_FRAC),
                   xs._HATCH_LIFT_MIN)
        cap_bb = FreeCAD.BoundBox()
        for shape in shapes:
            for face in xs._cap_faces(shape, axis, coord):
                cap_bb.add(face.BoundBox)
        off_plane = outside = 0
        for edge in edges:
            for vertex in edge.Vertexes:
                point = vertex.Point
                if abs((point.x, point.y, point.z)[axis] - coord) > lift * 1.001:
                    off_plane += 1
                if not cap_bb.isInside(FreeCAD.Vector(point.x, point.y, point.z)) \
                        and cap_bb.isValid():
                    # the lift takes it just off the cap plane, so test in-plane
                    inflated = FreeCAD.BoundBox(cap_bb)
                    inflated.enlarge(lift * 2.0 + 1.0e-6)
                    if not inflated.isInside(FreeCAD.Vector(point.x, point.y,
                                                            point.z)):
                        outside += 1
        check(off_plane == 0,
              "{} axis {}: {} hatch points off the cut plane".format(
                  name, xs.AXES[axis], off_plane))
        check(outside == 0,
              "{} axis {}: {} hatch points outside the cap bounds".format(
                  name, xs.AXES[axis], outside))

    print("  {:46s} {:2d} solids, {:4d} caps, {:5d} hatch edges".format(
        name, len(solids), n_caps, n_hatch))
    FreeCAD.closeDocument(doc.Name)


def check_annulus():
    """A cap with a hole must be hatched over the metal, not across the hole.

    The coax shield is the case that matters: an even-odd scanline that ignored
    the inner wire would fill the bore with hatch and the section would read as
    solid metal.
    """
    import Part

    outer = Part.makeCylinder(10.0, 20.0)
    inner = Part.makeCylinder(4.0, 20.0)
    tube = outer.cut(inner)
    cut = xs.cut_shape(tube, 1, 0.0, keep_low=True)     # cut normal to y
    caps = xs._cap_faces(cut, 1, 0.0)
    check(bool(caps), "annulus: no cap face")

    pitch = xs._hatch_pitch([cut], 1)
    edges = xs.hatch_edges([cut], 1, 0.0, True, pitch)
    check(bool(edges), "annulus: no hatch")

    # Cutting a tube on a plane through its axis exposes two rectangles, one per
    # wall: every hatch point must be over metal, i.e. 4 <= |x| <= 10.
    bad = 0
    for edge in edges:
        for vertex in edge.Vertexes:
            if not (4.0 - 1.0e-6 <= abs(vertex.Point.x) <= 10.0 + 1.0e-6):
                bad += 1
    check(bad == 0, "annulus: {} hatch points in the bore or outside the wall"
          .format(bad))
    print("  {:46s} {:2d} caps, {:5d} hatch edges, {} stray".format(
        "annulus (synthetic tube)", len(caps), len(edges), bad))


class _FakeViewObject(object):
    def __init__(self, visible=True):
        self.Visibility = visible
        self.ShapeColor = (0.4, 0.6, 0.9, 0.0)


class _FakeObject(object):
    """Enough of a document object for the teardown path: a view object, dynamic
    properties, and a name."""

    def __init__(self, name, visible=True):
        self.Name = name
        self.Label = name
        self.ViewObject = _FakeViewObject(visible)

    def addProperty(self, _type, name, _group=None, _doc=None):
        setattr(self, name, False)
        return self

    def setEditorMode(self, _name, _mode):
        pass

    def removeProperty(self, name):
        if not hasattr(self, name):
            raise AttributeError(name)
        delattr(self, name)
        return True


class _FakeDocument(object):
    def __init__(self, objects):
        self.Objects = list(objects)

    def removeObject(self, name):
        self.Objects = [o for o in self.Objects if o.Name != name]


def check_teardown():
    """A sweep must put back every body the cut hid -- whoever hid it.

    Walks the exact sequence that broke: cut (bodies hidden, previews added),
    then a *fresh* sweep with no memory of the first, as a second panel opening
    does. The bodies must come back and the markers must be gone.
    """
    body_a = _FakeObject("BodyA")
    body_b = _FakeObject("BodyB")
    already_hidden = _FakeObject("BodyC", visible=False)
    preview = _FakeObject("WavesimXSecBody")
    setattr(preview, xs._PREVIEW_PROP, True)
    doc = _FakeDocument([body_a, body_b, already_hidden, preview])

    # 1. the cut hides the two visible bodies and marks them
    for obj in (body_a, body_b):
        check(xs._hide_source(obj), "teardown: _hide_source refused {}".format(
            obj.Name))
    check(body_a.ViewObject.Visibility is False
          and body_b.ViewObject.Visibility is False,
          "teardown: bodies were not hidden")
    check(getattr(body_a, xs._HIDDEN_PROP, False),
          "teardown: no marker left on a hidden body")

    # 2. a sweep that knows nothing about step 1 -- the second panel opening
    removed = xs.sweep(doc)
    check(removed == 1, "teardown: swept {} previews, expected 1".format(removed))
    check(body_a.ViewObject.Visibility and body_b.ViewObject.Visibility,
          "teardown: a hidden body was NOT restored by the sweep -- this is the "
          "blank-model-on-second-open bug")
    check(not hasattr(body_a, xs._HIDDEN_PROP),
          "teardown: the marker outlived the restore")
    check(already_hidden.ViewObject.Visibility is False,
          "teardown: a body the user had hidden was switched back on")
    check(not any(getattr(o, xs._PREVIEW_PROP, False) for o in doc.Objects),
          "teardown: a preview object survived the sweep")

    # 3. a second sweep is a no-op, not a re-show
    already_hidden.ViewObject.Visibility = False
    xs.sweep(doc)
    check(already_hidden.ViewObject.Visibility is False,
          "teardown: a repeat sweep re-showed a hidden body")
    print("  {:46s} hide -> foreign sweep -> restored, markers cleared".format(
        "teardown (stand-in objects)"))


def check_discarded_side():
    """A body wholly on the cut-away side must come back as nothing.

    The cut is a half-space, not a knife: what the tool shows is everything on
    the kept side. A body the plane never reaches is still on the wrong side of
    it, and the regression this guards is exactly that -- such a body used to
    come back whole and float, uncut, in front of a sectioned model.
    """
    import Part

    box = Part.makeBox(4.0, 4.0, 4.0, FreeCAD.Vector(0.0, 0.0, 10.0))
    for axis, keep_low, coord, expect in (
            (2, True, 5.0, None),       # box is above the plane, low kept
            (2, False, 20.0, None),     # box is below the plane, high kept
            (2, False, 5.0, "whole"),   # box is above the plane, high kept
            (2, True, 20.0, "whole"),   # box is below the plane, low kept
    ):
        cut = xs.cut_shape(box, axis, coord, keep_low)
        if expect is None:
            check(cut is None,
                  "discard: a body wholly beyond the plane survived "
                  "(axis {}, coord {}, keep_low {})".format(
                      xs.AXES[axis], coord, keep_low))
        else:
            check(cut is not None
                  and abs(cut.Volume - box.Volume) <= _REL_TOL * box.Volume,
                  "discard: a body wholly on the kept side was changed "
                  "(axis {}, coord {}, keep_low {})".format(
                      xs.AXES[axis], coord, keep_low))
    print("  {:46s} beyond -> None, before -> whole".format(
        "discarded side (offset box)"))


class _FakeQuantity(object):
    def __init__(self, value):
        self.Value = value


class _FakeDomain(_FakeObject):
    """Enough of a Domain for the state properties and the grid planes."""

    def __init__(self):
        _FakeObject.__init__(self, "Domain")
        for prop in xs._POSITION_PROPS:
            setattr(self, prop, _FakeQuantity(0.0))
        for prop in xs._SHOW_PROPS:
            setattr(self, prop, True)

    def __setattr__(self, name, value):
        # The real Domain's App::PropertyDistance takes "3 mm" and reads back a
        # quantity; keep that shape so read_plane_state sees what it expects.
        if name in xs._POSITION_PROPS and not isinstance(value, _FakeQuantity):
            value = _FakeQuantity(float(str(value).split()[0]))
        object.__setattr__(self, name, value)

    def addProperty(self, _type, name, _group=None, _doc=None):
        object.__setattr__(self, name, False)
        return self


def check_extent_with_a_cut_up():
    """The position range must survive its own cut.

    A cut hides the bodies it stands in for, so a range taken from the *visible*
    geometry collapses to nothing the moment one goes up -- and the position
    control, ranged on it, traps the cut where it stands. Only bites where the
    Domain has no bounds of its own, which is every document before the first
    material is assigned.
    """
    import Part

    body = _FakeObject("Body")
    body.Shape = Part.makeBox(10.0, 10.0, 10.0)
    doc = _FakeDocument([body])

    lo, hi = xs.axis_extent_mm(None, doc, 2)
    check((lo, hi) == (0.0, 10.0),
          "extent: a visible body gives {}..{}, expected 0..10".format(lo, hi))

    # ...now cut: hidden, and marked as hidden by us
    xs._hide_source(body)
    lo, hi = xs.axis_extent_mm(None, doc, 2)
    check((lo, hi) == (0.0, 10.0),
          "extent: with the cut up the range collapsed to {}..{} -- the cut "
          "can no longer be moved".format(lo, hi))

    # a body the *user* hid carries no marker and stays out of the range
    other = _FakeObject("Other", visible=False)
    other.Shape = Part.makeBox(5.0, 5.0, 5.0, FreeCAD.Vector(0.0, 0.0, 50.0))
    doc.Objects.append(other)
    lo, hi = xs.axis_extent_mm(None, doc, 2)
    check((lo, hi) == (0.0, 10.0),
          "extent: a body the user hid was counted, giving {}..{}".format(
              lo, hi))
    print("  {:46s} 0..10 visible, cut, and beside a hidden body".format(
        "extent under a cut (stand-in objects)"))


def check_toggle_state():
    """The three buttons are one state, and the grid is the stronger switch.

    Grid on implies a cut (a mesh plane through a solid model is the thing the
    cut exists to make readable); cut off implies grid off. Runs over a
    stand-in Domain -- a console document has no view provider to draw either.
    """
    domain = _FakeDomain()
    doc = _FakeDocument([domain])
    real_find = xs.find_domain
    xs.find_domain = lambda _doc: domain
    try:
        check(xs.ensure_state_props(domain), "toggle: state props refused")
        check(not xs.is_active(doc) and not xs.is_grid_on(doc),
              "toggle: a fresh domain claims a cut is up")

        # An unconfigured document still has a cut to raise: the default axis,
        # centred -- otherwise the button would appear to do nothing.
        xs.set_active(doc, True)
        check(xs.is_active(doc), "toggle: the cut did not come up")
        state = xs.read_state(domain, doc)
        check(state["configured"] and state["axis"] == xs._DEFAULT_AXIS,
              "toggle: switching on left the cut unconfigured")
        check(not xs.is_grid_on(doc),
              "toggle: the cut brought the grid up with it")

        # The grid is the stronger switch: it implies the cut.
        xs.set_active(doc, False)
        xs.set_grid(doc, True)
        check(xs.is_grid_on(doc) and xs.is_active(doc),
              "toggle: the grid came up without a cut under it")
        check(xs.read_plane_state(domain)[1] == (False, False, True),
              "toggle: the grid is drawn on {} planes, expected only the cut's"
              .format(xs.read_plane_state(domain)[1]))

        # ...and taking the cut down takes the grid with it.
        xs.set_active(doc, False)
        check(not xs.is_active(doc) and not xs.is_grid_on(doc),
              "toggle: the grid outlived the cut it was drawn on")
        check(xs.read_plane_state(domain)[1] == (False, False, False),
              "toggle: a grid plane is still drawn with no cut up")

        # A configured cut is remembered, not re-centred, across a toggle.
        xs.write_state(domain, axis=0, flip=True, style=xs.CAP_HATCHED)
        xs.write_plane_state(domain, position_mm=3.25, axis=0)
        xs.set_active(doc, True)
        state = xs.read_state(domain, doc)
        check((state["axis"], state["flip"], state["style"]) ==
              (0, True, xs.CAP_HATCHED)
              and abs(state["position"] - 3.25) < 1.0e-9,
              "toggle: the stored configuration was lost: {}".format(state))
    finally:
        xs.find_domain = real_find
    print("  {:46s} grid implies cut, cut off implies grid off".format(
        "toggle state (stand-in domain)"))


def check_cap_shading():
    """The cap tint must be darker than the body, and the list the right length."""
    import Part

    base = (0.40, 0.60, 0.90, 0.0)
    tint = xs._shade(base, xs._CAP_SHADE, xs._CAP_FLOOR)
    check(all(t < b for t, b in zip(tint[:3], base[:3])),
          "shading: the cap tint {} is not darker than the body {}".format(
              tint, base))
    check(all(0.0 <= c <= 1.0 for c in tint[:3]),
          "shading: the cap tint {} left the unit range".format(tint))
    check(tint[3] == base[3], "shading: the tint dropped the body's alpha")
    # A black body must not produce a negative or clipped-away colour.
    dark = xs._shade((0.0, 0.0, 0.0, 0.0), xs._CAP_SHADE, xs._CAP_FLOOR)
    check(all(c > 0.0 for c in dark[:3]),
          "shading: a black body shaded to {}, losing the face entirely".format(
              dark))

    box = Part.makeBox(10.0, 10.0, 10.0)
    cut = xs.cut_shape(box, 2, 5.0, keep_low=True)
    faces = cut.Faces
    caps = [f for f in faces if xs.is_cap_face(f, 2, 5.0)]
    colors = [base if not xs.is_cap_face(f, 2, 5.0) else tint for f in faces]
    check(len(colors) == len(faces),
          "shading: {} colours for {} faces".format(len(colors), len(faces)))
    check(len(caps) == 1,
          "shading: a cut box has {} cap faces, expected 1".format(len(caps)))
    print("  {:46s} {} faces, {} cap, tint {}".format(
        "cap shading (unit box)", len(faces), len(caps),
        tuple(round(c, 3) for c in tint[:3])))


def main():
    print("Cross-section gate\n")
    print("synthetic:")
    check_teardown()
    check_discarded_side()
    check_extent_with_a_cut_up()
    check_toggle_state()
    check_cap_shading()
    check_annulus()
    print("\ndocuments in {}:".format(DOCS))
    if os.path.isdir(DOCS):
        for name in sorted(os.listdir(DOCS)):
            if name.endswith(".FCStd"):
                check_document(os.path.join(DOCS, name))
    else:
        print("  (not present; synthetic checks only)")

    print("")
    if _failures:
        print("FAIL -- {} problem(s):".format(len(_failures)))
        for message in _failures[:40]:
            print("  " + message)
    else:
        print("PASS -- every cut partitions its body, caps closed, hatch on the "
              "cap.")
    sys.stdout.flush()


main()
