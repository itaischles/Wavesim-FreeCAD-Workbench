# -*- coding: utf-8 -*-
"""Backwards-compatibility shim for the pre-rename "TEM Source".

The TEM port was replaced by the **Modal Port** (:mod:`wavesim_gui.modal_port`),
an impedance-sheet boundary that terminates its own face. Nothing in the
workbench imports this module any more -- it exists for one reason:

FreeCAD stores a scripted object's ``Proxy`` as a reference to
``<module>.<ClassName>``. A document saved with the old workbench names
``wavesim_gui.tem_source.TEMSourceObject`` (and ``...TEMSourceViewProvider``), so
deleting this module would leave those objects proxy-less on load -- properties
intact but dead, with no ``execute`` and no task panel. The aliases below let the
unpickler resolve them to the new classes; :meth:`ModalPortObject.
onDocumentRestored` then rewrites the object's ``WavesimType`` marker from
``"TEMSource"`` to ``"ModalPort"``, so the document is migrated the first time it
is opened and needs no further special-casing.

The view-provider alias is defined only when a GUI is available, mirroring
:mod:`wavesim_gui.modal_port`'s own guard (in console mode the old document's
view provider is not restored either).

Write new code against :mod:`wavesim_gui.modal_port`.
"""

from wavesim_gui.modal_port import ModalPortObject as TEMSourceObject  # noqa: F401

try:
    from wavesim_gui.modal_port import (  # noqa: F401
        ModalPortViewProvider as TEMSourceViewProvider,
    )
except ImportError:  # console mode: modal_port defines no view provider
    pass
