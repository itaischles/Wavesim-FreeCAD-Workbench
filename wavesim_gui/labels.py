# -*- coding: utf-8 -*-
"""Auto-generated tree labels that stop being auto once the user renames one.

Most Wavesim objects carry a descriptive label built from their own properties
-- ``Probe (Ex @ 10, 0, 0)``, ``Modal Port (z0)``, ``Snapshot (E, XY)``. The
label is regenerated whenever the object's task panel is accepted, which is what
keeps the tree readable as settings change.

That is also what used to throw away a name the user had typed in the tree: the
panel rewrote ``Label`` unconditionally, and when the regenerated label collided
with a sibling's, FreeCAD appended its uniquifying ``001``. Renaming a monitor
and then editing it therefore renamed it back *and* numbered it.

The rule here is the usual one: **a label is auto only until the user changes
it.** A caller computes the label the object would have carried with its old
settings, applies its edits, and asks :func:`retitle` for the new one; the write
only happens when the current label still matches the old auto label (possibly
with FreeCAD's three-digit suffix, which a collision at creation time may have
added). Anything else is a name the user chose, and is left alone.

Qt-free and FreeCAD-free, so it stays importable in console mode.
"""

__all__ = ["is_auto", "retitle"]


def is_auto(label, auto):
    """True if *label* is still the generated *auto* label (or a numbered copy).

    FreeCAD keeps labels unique within a document by appending a three-digit
    counter, so two monitors created with identical settings come out as
    ``Snapshot (E, XY)`` and ``Snapshot (E, XY)001``. Both are auto labels: the
    second was never typed by anyone either.
    """
    label = str(label or "")
    auto = str(auto or "")
    if label == auto:
        return True
    tail = label[len(auto):]
    return label.startswith(auto) and len(tail) == 3 and tail.isdigit()


def retitle(obj, old_auto, new_auto):
    """Move *obj*'s label from *old_auto* to *new_auto*, unless it was renamed.

    *old_auto* is what :func:`is_auto` compares against and must be computed
    **before** the caller writes its new property values -- it is the label the
    object would still be carrying if nobody had touched it. Returns True when
    the label was rewritten.
    """
    if obj is None:
        return False
    current = str(getattr(obj, "Label", "") or "")
    if not is_auto(current, old_auto):
        return False        # a name the user chose; not ours to overwrite
    if current == new_auto:
        return False        # already right, and re-setting it is a doc change
    obj.Label = new_auto
    return True
