# -*- coding: utf-8 -*-
"""Reuse a document's last voxelisation when nothing it depends on has changed.

Voxelising is the slow half of a run (two minutes on a conformal spark gap) and
is repeated on every Run, Port Matrix and Compute Mode, though most re-runs
change a monitor, the run time or a port's drive and leave the material arrays
exactly as they were. So the last voxelisation is kept on disk, next to the
document's ``run`` folder, together with a **fingerprint** of everything that
produced it, and reused when the next fingerprint comes out the same.

The fingerprint is taken at the boundary of
:func:`wavesim_gui.voxelize.voxelize_materials`, never from the document: the
function is pure in its arguments, its bodies' shapes and its own code, so
hashing exactly those is complete by construction. There is no list of "edits
that matter" to keep up to date -- a monitor that enlarges the domain changes
the node arrays and so the fingerprint, and one that does not, does not. What
goes in:

* every body, in order (a later body overwrites an earlier one): its shape as
  BREP text, its material's eps / mu / PEC / sigma, and its object name (an
  electrostatic run labels conductors by it);
* every argument of the call -- node arrays, cell size, spacing, PML depth,
  extra points, background medium, subpixel / conformal switches, conductor
  names -- whatever the caller passes, so a new argument is covered the day it
  is added;
* the source of the voxeliser modules (:data:`_CODE_MODULES`), so a fix to the
  voxeliser invalidates every cache made before it, and the FreeCAD/OCC version.

Three quirks of the shape text, all measured on the test documents. OCC writes
a line of seven state flags per sub-shape (``0111000``) that differs between a
shape read from the file and the same shape recomputed, so those lines are
stripped. The ``.FCStd`` file writes placements to 15 decimal places, so a
coordinate computed as ``0.0999999999999997`` reloads as ``0.1``; every number
is rounded to 15 decimals (1e-15 mm) before hashing, or no cache would survive
closing the document. And the shape is copied without its mesh first, so
whether the 3D view has tessellated it yet cannot change the answer. Hashing
takes ~2 ms a document.

One slot per solver mode (``fdtd``, ``electrostatic``): the two never share a
voxelisation -- electrostatics drops the PML shell, which moves grid lines, and
adds a conductor-label array -- so a single slot would be overwritten every time
a document alternates between them.

A cache problem never stops a run: anything unreadable is a miss, anything
unwritable is a warning, and the ``voxel_cache`` setting (Wavesim -> Settings)
turns reuse off altogether.
"""

import hashlib
import json
import os
import re

import FreeCAD

import wavesim_settings

# Bump to orphan every existing cache when the stored layout changes.
_FORMAT = 1

# Modules whose source decides what a voxelisation comes out as. The section
# pool is here although it is meant to change only the speed, because the one
# time it did not (an unanswered plane read as empty geometry) it changed the
# arrays without a word.
_CODE_MODULES = ("voxelize.py", "scanline.py", "subpixel.py",
                 "sectionpool.py", "_section_worker.py")

_FLAG_LINES = re.compile(r"^[01]{7}$", re.M)
_NUMBER = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")

_HERE = os.path.dirname(os.path.abspath(__file__))
_CODE_HASH = None

# The folder next to ``run`` that holds the slots; not one of the job
# artefacts, so starting a run never clears it.
CACHE_DIR = "voxel_cache"


class CachedArrays(dict):
    """The material arrays, plus the cache file that holds exactly them.

    :func:`wavesim_gui.voxelize.write_materials` copies :attr:`npz_path`
    instead of compressing the arrays again (a second or more on a big model).
    """

    npz_path = None


def enabled():
    """Whether the ``voxel_cache`` setting allows reuse (on unless ``'off'``)."""
    try:
        return str(wavesim_settings.get_voxel_cache()).lower() != "off"
    except Exception:
        return True


def cache_dir_for(doc):
    """``<results>/<document>/voxel_cache``, beside the document's ``run``."""
    from wavesim_gui import job as job_mod

    return os.path.join(os.path.dirname(job_mod.workdir_for(doc, "run")),
                        CACHE_DIR)


def _code_hash():
    global _CODE_HASH
    if _CODE_HASH is None:
        digest = hashlib.sha256()
        for name in _CODE_MODULES:
            with open(os.path.join(_HERE, name), "rb") as handle:
                digest.update(name.encode() + b"\0" + handle.read())
        _CODE_HASH = digest.hexdigest()
    return _CODE_HASH


def _shape_text(shape):
    """*shape* as BREP text, as the document file would hold it.

    No mesh, no OCC state-flag lines, and every number rounded to 15 decimal
    places (1e-15 mm) with no negative zeros. A saved ``.FCStd`` writes its
    placements to 15 decimals and its geometry to 17, so without the rounding a
    shape and its reloaded copy (``0.0999999999999997`` live, ``0.1`` read
    back; ``0`` live, ``-0`` read back) would fingerprint apart.
    """
    try:
        shape = shape.copy(True, False)
    except Exception:
        pass
    text = _FLAG_LINES.sub("", shape.exportBrepToString())
    # ``+ 0.0`` folds -0 into 0: a reloaded placement matrix reads ``-0`` where
    # the recomputed one has ``0``.
    return _NUMBER.sub(lambda m: repr(round(float(m.group()), 15) + 0.0), text)


def fingerprint(materials, kwargs):
    """Hex digest of everything ``voxelize_materials(materials, **kwargs)`` reads.

    *kwargs* is the call's keyword arguments minus ``progress``. Values are
    serialised through ``json`` (floats by ``repr``, which round-trips), with
    ``repr`` as the fallback for anything else, so a changed value can only
    change the digest.
    """
    from wavesim_gui import voxelize as vox

    digest = hashlib.sha256()

    def feed(value):
        digest.update(json.dumps(value, sort_keys=True, default=repr).encode())
        digest.update(b"\0")

    feed([_FORMAT, _code_hash(), list(FreeCAD.Version()[:4])])
    for shape, eps, mu, pec, sigma, body in vox._gather(materials):
        feed([float(eps), float(mu), bool(pec), float(sigma),
              str(getattr(body, "Name", ""))])
        digest.update(_shape_text(shape).encode())
        digest.update(b"\0")
    feed(kwargs)
    return digest.hexdigest()


def _plain(value):
    """*value* with numpy scalars and tuples turned into JSON-able types."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        return value.item()
    return value


def _paths(folder, mode):
    return (os.path.join(folder, mode + ".npz"),
            os.path.join(folder, mode + ".json"))


def load(folder, mode, key):
    """The stored voxelisation for *mode* if its fingerprint is *key*, else None."""
    import numpy as np

    npz_path, meta_path = _paths(folder, mode)
    try:
        with open(meta_path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError):
        return None
    if meta.get("key") != key:
        return None
    try:
        arrays = CachedArrays()
        with np.load(npz_path) as stored:
            for name in stored.files:
                arrays[name] = stored[name]
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            "Wavesim: could not read the stored voxelisation ({}); "
            "voxelising again.\n".format(exc))
        return None
    arrays.npz_path = npz_path
    return {
        "arrays": arrays,
        "grid": meta["grid"],
        "origin_m": tuple(meta["origin_m"]),
        "counts": meta["counts"],
        "pec_names": meta.get("pec_names") or {},
    }


def store(folder, mode, key, vox):
    """Save *vox* as *mode*'s slot under fingerprint *key*.

    Returns *vox* with its arrays wrapped as :class:`CachedArrays` on success,
    or *vox* unchanged when the slot could not be written. The metadata file is
    removed first and written last, so a slot interrupted half way is never
    read back as a hit.
    """
    import numpy as np

    npz_path, meta_path = _paths(folder, mode)
    try:
        os.makedirs(folder, exist_ok=True)
        if os.path.exists(meta_path):
            os.remove(meta_path)
        tmp = npz_path + ".tmp"
        with open(tmp, "wb") as handle:
            np.savez_compressed(handle, **vox["arrays"])
        os.replace(tmp, npz_path)
        meta = {
            "key": key,
            "grid": _plain(vox["grid"]),
            "origin_m": _plain(vox["origin_m"]),
            "counts": _plain(vox["counts"]),
            "pec_names": _plain(vox.get("pec_names") or {}),
        }
        tmp = meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(meta, handle)
        os.replace(tmp, meta_path)
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            "Wavesim: could not store the voxelisation for reuse ({}).\n"
            .format(exc))
        return vox
    arrays = CachedArrays(vox["arrays"])
    arrays.npz_path = npz_path
    out = dict(vox)
    out["arrays"] = arrays
    return out


def voxelize(doc, mode, materials, cell_size_m, **kwargs):
    """:func:`~wavesim_gui.voxelize.voxelize_materials`, reusing the last result.

    Same arguments and return value; *doc* picks the cache folder and *mode*
    the slot. A hit says so on the console, so a run that skipped the sweep is
    never mistaken for one that did it.
    """
    from wavesim_gui import voxelize as vox

    if not enabled():
        return vox.voxelize_materials(materials, cell_size_m, **kwargs)

    keyed = dict(kwargs)
    keyed.pop("progress", None)
    keyed["cell_size_m"] = cell_size_m
    folder = cache_dir_for(doc)
    try:
        key = fingerprint(materials, keyed)
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            "Wavesim: could not fingerprint the model for voxelisation reuse "
            "({}); voxelising.\n".format(exc))
        return vox.voxelize_materials(materials, cell_size_m, **kwargs)

    cached = load(folder, mode, key)
    if cached is not None:
        FreeCAD.Console.PrintMessage(
            "Wavesim: model unchanged since the last voxelisation; reusing it "
            "({}).\n".format(folder))
        return cached
    result = vox.voxelize_materials(materials, cell_size_m, **kwargs)
    return store(folder, mode, key, result)
