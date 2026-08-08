# -*- coding: utf-8 -*-
"""Tree visibility linking: an object's eye drives the geometry under it.

A Material and a path monitor own geometry they did not create -- bodies
dragged onto the material, a sketch dropped on the monitor -- and the tree
already nests that geometry under them (``claimChildren``). This module makes
the nesting mean something, in *both* directions:

* toggling the parent's eye shows or hides every child, so "hide all the PEC" is
  one click instead of one per body;
* hiding the children by hand settles the parent's own eye to match, so the
  parent never reads as visible when nothing under it is.

The parent has no geometry of its own, so without this its ``Visibility`` is a
flag that does nothing -- which is what made a Material sit permanently greyed
out in the tree.

**One flag, two directions.** Every write this module makes sets ``_busy``, and
both hooks ignore changes while it is set, so the push down and the pull up
cannot ping-pong. A third state matters as much: while a document is being
restored, the children already carry their own saved visibilities, and pushing
the parent's saved state over them would silently re-show a body the user had
hidden by hand. ``slotStartRestoreDocument``/``slotFinishRestoreDocument`` mark
that window and :func:`is_restoring` is what the view providers check.

**Owners register themselves** (:func:`register_owner`) rather than being listed
here, so this module needs to know how to find an owner's children -- not what a
Material or a monitor is.
"""

import FreeCAD


# [(key, is_owner(obj) -> bool, children(obj) -> [obj])]
_owners = []
# True while this module is writing a Visibility, so its own writes do not come
# back through the hooks as though the user had made them.
_busy = False
# Number of documents currently being restored (nesting is possible).
_restoring = 0
_observer = None


def register_owner(key, is_owner, children):
    """Declare a kind of object whose eye drives the geometry under it.

    *key* is a short unique name; re-registering it is a no-op, so a module
    re-import cannot install the same owner twice.
    """
    if any(existing == key for existing, _pred, _kids in _owners):
        return
    _owners.append((key, is_owner, children))


def owner_children(owner):
    """The geometry *owner* owns, or ``[]`` if it is not a registered owner."""
    for _key, is_owner, children in _owners:
        try:
            if is_owner(owner):
                return [c for c in (children(owner) or []) if c is not None]
        except Exception:
            continue
    return []


def owners_of(child):
    """Registered owners holding *child*.

    Walks ``child.InList`` -- the objects that link to it -- rather than
    scanning the document: an owner holds its children through a link property,
    so it is always in that list.
    """
    return [obj for obj in (getattr(child, "InList", []) or [])
            if child in owner_children(obj)]


def is_restoring():
    """True while a document is being loaded (see the module docstring)."""
    return _restoring > 0


def apply_to_children(owner, state):
    """Show/hide every child of *owner*; returns True if it had any."""
    global _busy
    children = owner_children(owner)
    if not children or _busy:
        return False
    _busy = True
    try:
        for child in children:
            if bool(getattr(child, "Visibility", True)) != bool(state):
                child.Visibility = bool(state)
    finally:
        _busy = False
    return True


def sync_from_children(owner):
    """Set *owner*'s own eye from its children: on when **any** of them is.

    Any, not all: a material with one body left showing is not hidden, and the
    click that follows should hide that one rather than re-show the rest.
    """
    global _busy
    children = owner_children(owner)
    if not children or _busy:
        return
    state = any(bool(getattr(c, "Visibility", False)) for c in children)
    if bool(getattr(owner, "Visibility", False)) == state:
        return
    _busy = True
    try:
        owner.Visibility = state
    finally:
        _busy = False


def on_owner_toggled(owner, state):
    """Hook for an owner's view provider ``onChanged('Visibility')``."""
    if is_restoring():
        return
    apply_to_children(owner, state)


class _VisibilityObserver:
    """Pulls an owner's eye up when a child's own eye is toggled."""

    def slotChangedObject(self, obj, prop):
        if prop != "Visibility" or _busy or is_restoring():
            return
        for owner in owners_of(obj):
            sync_from_children(owner)

    def slotStartRestoreDocument(self, doc):
        global _restoring
        _restoring += 1

    def slotFinishRestoreDocument(self, doc):
        global _restoring
        _restoring = max(0, _restoring - 1)
        # Settle every owner's eye now that its children carry their saved
        # state. Only the owner's own flag can move here -- the children keep
        # exactly what they were saved with -- which is what brings a document
        # written before any of this existed (every Material greyed, its bodies
        # visible) into agreement on load. It does mark such a document
        # modified once; a document already in agreement is untouched.
        for obj in list(getattr(doc, "Objects", []) or []):
            if owner_children(obj):
                sync_from_children(obj)


def install():
    """Register the document observer; idempotent, so callers need not check."""
    global _observer
    if _observer is None:
        _observer = _VisibilityObserver()
        FreeCAD.addDocumentObserver(_observer)
    return _observer


# --------------------------------------------------------------------------- #
# View-provider mixin
# --------------------------------------------------------------------------- #

class DisplayModeMixin:
    """Gives a geometry-less view provider a display mode, so it can be *shown*.

    The tree greys a row by ``ViewProvider::isShow()``, which asks the coin
    mode-switch which child is on -- **not** the ``Visibility`` property. A
    scripted provider that never calls ``addDisplayMode`` leaves that switch at
    -1, so its row stays greyed however often ``Visibility`` is toggled, which
    is exactly how a Material could drive its bodies and still look switched
    off. Naming a mode in ``getDisplayModes`` is not enough on its own: there
    has to be a node registered under that name for the switch to select.

    The node is an empty ``SoGroup`` -- these objects genuinely draw nothing;
    the point is only to give the switch something to land on. Providers call
    :meth:`attach_display_mode` from their own ``attach``.
    """

    def attach_display_mode(self, vobj):
        try:
            from pivy import coin
            node = coin.SoGroup()
            vobj.addDisplayMode(node, "Default")
            self._display_node = node       # keep it alive with the provider
        except Exception as exc:            # never let this break attach
            FreeCAD.Console.PrintWarning(
                "Wavesim: could not attach a display mode ({})\n".format(exc)
            )

    def getDisplayModes(self, vobj):
        return ["Default"]

    def getDefaultDisplayMode(self):
        return "Default"

    def setDisplayMode(self, mode):
        return mode


class LinkedVisibilityMixin(DisplayModeMixin):
    """Adds the parent-eye behaviour to a view provider that claims children.

    Mix into a view provider whose object is a registered owner; it turns the
    object's ``Visibility`` into a switch over the geometry underneath.
    """

    def onChanged(self, vobj, prop):
        if prop != "Visibility":
            return
        owner = getattr(vobj, "Object", None) or getattr(self, "Object", None)
        if owner is not None:
            on_owner_toggled(owner, bool(getattr(vobj, "Visibility", True)))
