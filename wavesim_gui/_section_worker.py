# -*- coding: utf-8 -*-
"""Worker process for :mod:`wavesim_gui.sectionpool`: OCC sections on demand.

Run by FreeCAD's *bundled* ``bin/python.exe`` (not ``freecadcmd``), one process
per core, and spoken to over stdin/stdout by the parent. It exists because
``Shape.slice`` is 80% of a conformal voxelisation's wall time and holds the GIL,
so threads buy nothing and processes buy everything.

It deliberately calls :func:`wavesim_gui.voxelize._section_polygons` -- the very
function the serial path calls -- rather than reimplementing the section rules
(closed-wire filtering, the degenerate-plane nudge retry). There is one
definition of "the cross-section here", and both paths run it.

Protocol
--------
Length-prefixed pickles both ways: ``struct.pack("<I", n)`` then ``n`` bytes.

    ("load", token, brep_path)      -> ("ok", token)
    ("sec", idx, token, z, defl, nudge)
                                    -> ("sec", idx, polygons | None)
    ("bye",)                        -> process exits

``polygons`` is what ``_section_polygons`` returned: a list of ``(V, 2)`` float
arrays, or ``None`` for a plane that misses the solid.

**The protocol does not run on fd 1.** Anything in this process -- FreeCAD, OCC,
a stray ``print`` in a module we import -- may write to stdout, and one such byte
would desynchronise the stream and hang the parent. So fd 1 is duplicated to a
private fd first and then pointed at the null device: the parent's pipe survives
as that private fd, and every later write to "stdout" is discarded.
"""

import os
import pickle
import struct
import sys

# --------------------------------------------------------------------------- #
# Take the pipe private *before* importing anything that might print.
# --------------------------------------------------------------------------- #
_PIPE_FD = os.dup(1)
_devnull = os.open(os.devnull, os.O_WRONLY)
os.dup2(_devnull, 1)
os.close(_devnull)

_IN = sys.stdin.buffer


def _send(obj):
    payload = pickle.dumps(obj, protocol=4)
    os.write(_PIPE_FD, struct.pack("<I", len(payload)))
    written = 0
    while written < len(payload):
        written += os.write(_PIPE_FD, payload[written:])


def _recv():
    head = _read_exactly(4)
    if head is None:
        return None
    (size,) = struct.unpack("<I", head)
    body = _read_exactly(size)
    return None if body is None else pickle.loads(body)


def _read_exactly(n):
    chunks = []
    got = 0
    while got < n:
        chunk = _IN.read(n - got)
        if not chunk:
            return None
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def main():
    # The workbench dir (parent of this package) must be importable.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    import FreeCAD
    import Part

    from wavesim_gui.voxelize import _section_polygons

    z_axis = FreeCAD.Vector(0.0, 0.0, 1.0)
    shapes = {}

    while True:
        msg = _recv()
        if msg is None or msg[0] == "bye":
            return
        kind = msg[0]
        if kind == "load":
            _, token, path = msg
            shape = Part.Shape()
            shape.importBinary(path)
            shapes[token] = shape
            _send(("ok", token))
        elif kind == "sec":
            _, idx, token, z, deflection, nudge = msg
            try:
                polys = _section_polygons(shapes[token], z_axis, z, deflection,
                                          nudge)
            except Exception:
                # A plane the parent can redo serially; never kill the pool for
                # one bad section.
                polys = "error"
            _send(("sec", idx, polys))


main()
