# -*- coding: utf-8 -*-
"""Letting FreeCAD expressions (VarSet parameters, formulas) drive properties.

FreeCAD can bind any *editable* property to an expression -- the ``f(x)`` button
in the property editor -- so a sweep parameter kept in a ``VarSet`` can drive a
cell size, a max frequency or a pulse bandwidth. The binding is evaluated by the
expression engine on every recompute.

Two things follow, and this module owns both:

* **Read-only kills it.** A property put in editor mode 1 to steer users towards
  a task panel gets no editor widget in the property editor, and therefore no
  ``f(x)`` button. Several Wavesim properties were marked that way; they are now
  opened up with :func:`allow_expressions`, which also re-asserts the mode on a
  restored document (property status is stored in the file, so a document saved
  while the property was read-only would come back locked).
* **The expression wins.** Assigning to a bound property from Python succeeds
  but is discarded at the next recompute, when the engine re-evaluates. A task
  panel writing the same property would therefore look like it silently reverted
  the user's typing, so panels grey the corresponding widget out --
  :func:`lock_bound_widget` -- and say why.

Values are stored in SI base units (seconds, hertz, ...); an expression is
evaluated in those units too, not in the panel's display unit.

FreeCAD-side only; imports nothing but what it is handed.
"""

__all__ = [
    "BOUND_TOOLTIP",
    "expression_for",
    "is_bound",
    "allow_expressions",
    "lock_bound_widget",
]

#: Shown on a panel widget whose property is driven by an expression.
BOUND_TOOLTIP = (
    "Driven by the expression '{}'.\nEdit it in the property editor (the f(x) "
    "button); a value typed here would be overwritten on the next recompute."
)


def expression_for(obj, prop):
    """Return the expression bound to *obj*'s *prop*, or None.

    ``ExpressionEngine`` is a list of ``(path, expression)`` pairs. The path is
    the property name for a plain property and ``Name.sub`` for a component of a
    vector/placement, so a bound sub-component counts as bound here too -- the
    panel must not fight it either.
    """
    if obj is None or not prop:
        return None
    for entry in getattr(obj, "ExpressionEngine", None) or ():
        try:
            path, expr = entry[0], entry[1]
        except (TypeError, IndexError):
            continue
        path = str(path)
        if path == prop or path.startswith(prop + "."):
            return str(expr)
    return None


def is_bound(obj, prop):
    """True when *obj*'s *prop* is driven by an expression."""
    return expression_for(obj, prop) is not None


def allow_expressions(obj, *props):
    """Clear the read-only editor mode on *props* so they can take expressions.

    Idempotent, and silent about properties the object does not have -- callers
    run it from both ``__init__`` and ``onDocumentRestored``, where an older
    document may be missing some of them.
    """
    for prop in props:
        if obj is not None and hasattr(obj, prop):
            try:
                obj.setEditorMode(prop, 0)
            except Exception:
                pass


def lock_bound_widget(widget, obj, prop):
    """Disable *widget* and explain, if *obj*'s *prop* carries an expression.

    Returns True when the widget was locked. A no-op otherwise, so panels can
    call it unconditionally on every row they build.
    """
    expr = expression_for(obj, prop)
    if expr is None or widget is None:
        return False
    widget.setEnabled(False)
    widget.setToolTip(BOUND_TOOLTIP.format(expr))
    return True
