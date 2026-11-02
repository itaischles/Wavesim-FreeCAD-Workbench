# -*- coding: utf-8 -*-
"""Scripting API: previews -- screenshots, visibility, cross-section, voxels.

Screenshots and the cross-section need the FreeCAD GUI (they draw in the 3D
view); :func:`voxel_preview` works headless (it voxelizes and renders the
material arrays with matplotlib). Output paths are chosen by the caller (the
MCP server always passes a path inside the project's ``previews/`` folder).
"""

import os

import FreeCAD

from wavesim_gui import api_common as c
from wavesim_gui.api_common import ApiError, api_function

__all__ = ["screenshot", "set_visibility", "cross_section", "voxel_preview"]

_VIEWS = {
    "iso": "viewIsometric", "isometric": "viewIsometric",
    "front": "viewFront", "back": "viewRear", "rear": "viewRear",
    "top": "viewTop", "bottom": "viewBottom",
    "left": "viewLeft", "right": "viewRight",
    "current": None,
}


def _gui():
    if not getattr(FreeCAD, "GuiUp", False):
        raise ApiError("This needs the FreeCAD GUI (it draws in the 3D view).")
    import FreeCADGui
    return FreeCADGui


def _check_png_path(path):
    if not path or not str(path).lower().endswith(".png"):
        raise ApiError("The output path must end in .png.")
    folder = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(folder):
        raise ApiError("Output folder {} does not exist.".format(folder))
    return os.path.abspath(path)


@api_function("preview")
def screenshot(doc=None, view="iso", path=None, width=1280, height=960,
               fit=True, background="White"):
    """Save a PNG of the 3D view from a standard direction.

    :param view: ``iso | front | back | top | bottom | left | right |
        current``.
    :param path: output ``.png`` (folder must exist).
    :param width, height: pixels (64..4096).
    :param fit: fit the whole model in view first.
    :param background: ``"White"``, ``"Black"``, ``"Transparent"`` or
        ``"Current"`` (the view's own background).
    :returns: ``{"path", "view", "width", "height"}``.
    """
    gui = _gui()
    doc = c.get_doc(doc)
    key = str(view).strip().lower()
    if key not in _VIEWS:
        raise ApiError("Unknown view {!r}; use one of: {}".format(
            view, ", ".join(k for k in _VIEWS if k not in ("isometric",
                                                         "rear"))))
    path = _check_png_path(path)
    width, height = int(width), int(height)
    if not (64 <= width <= 4096 and 64 <= height <= 4096):
        raise ApiError("width/height must be within 64..4096 pixels.")
    gdoc = gui.getDocument(doc.Name)
    if gdoc is None:
        raise ApiError("Document {!r} has no 3D view.".format(doc.Label))
    if gui.ActiveDocument is None or gui.ActiveDocument.Document is not doc:
        FreeCAD.setActiveDocument(doc.Name)
    view_obj = gdoc.activeView()
    if view_obj is None or not hasattr(view_obj, "saveImage"):
        raise ApiError("Document {!r} has no 3D view open.".format(doc.Label))
    method = _VIEWS[key]
    if method:
        getattr(view_obj, method)()
    if fit:
        view_obj.fitAll()
    try:
        gui.updateGui()
    except Exception:
        pass
    view_obj.saveImage(path, width, height, background)
    if not os.path.isfile(path):
        raise ApiError("FreeCAD did not write the image.")
    return {"path": path, "view": key, "width": width, "height": height}


@api_function("preview")
def set_visibility(doc=None, objects=None, visible=True):
    """Show or hide objects in the 3D view (no effect on the simulation).

    :param objects: an object reference, a list of them, or ``"domain"`` for
        the Domain box/grid.
    :param visible: true to show, false to hide.
    """
    doc = c.get_doc(doc)
    refs = objects if isinstance(objects, (list, tuple)) else [objects]
    out = []
    for ref in refs:
        if isinstance(ref, str) and ref.lower() == "domain":
            from wavesim_gui import domain as domain_mod
            obj = domain_mod.find_domain(c.get_sim(doc))
        else:
            obj = c.find_object(doc, ref)
        vobj = getattr(obj, "ViewObject", None)
        if vobj is not None:
            vobj.Visibility = bool(visible)
        else:
            obj.Visibility = bool(visible)
        out.append(obj.Label)
    return {"objects": out, "visible": bool(visible)}


@api_function("preview")
def cross_section(doc=None, on=True, grid=None, plane=None, offset=None,
                  flip=None, style=None):
    """Cut the model open on a grid plane (view only; runs are unaffected).

    The same tool as the Cross Section toolbar: everything on the kept side of
    the plane is shown with a capped section face; ``grid`` draws the Domain's
    cell grid on the cut (implies the cut).

    :param on: raise (true) or take down (false) the cut.
    :param grid: draw the mesh grid on the cut plane (bool).
    :param plane: normal axis of the cut, ``'x'|'y'|'z'`` (``'xy'`` = z,
        ``'xz'`` = y, ``'yz'`` = x also accepted).
    :param offset: plane position along that axis (default unit mm).
    :param flip: keep the high side instead of the low side.
    :param style: cap marking ``'Shaded'|'Hatched'|'Plain'``.
    :returns: the stored state ``{axis, position, flip, style, grid, on}``.
    """
    from wavesim_gui import crosssection as xs

    doc = c.get_doc(doc)
    _gui()
    domain = xs.find_domain(doc)
    if domain is None:
        raise ApiError("The document has no simulation domain.")
    xs.ensure_state_props(domain)
    planes = {"x": 0, "y": 1, "z": 2, "yz": 0, "xz": 1, "zx": 1, "xy": 2}
    values = {}
    axis = None
    if plane is not None:
        axis = planes.get(str(plane).strip().lower())
        if axis is None:
            raise ApiError("plane must be x, y or z (or yz, xz, xy).")
        values["axis"] = axis
    if flip is not None:
        values["flip"] = bool(flip)
    if style is not None:
        values["style"] = c.enum_choice(style, xs.CAP_STYLES, "style")
    if values:
        xs.write_state(domain, **values)
    if offset is not None:
        state = xs.read_state(domain, doc)
        pos = c.quantity_value(offset, "length", what="offset")
        lo, hi = xs.axis_extent_mm(domain, doc, state["axis"])
        if not lo - 1e-9 <= pos <= hi + 1e-9:
            raise ApiError("offset {:.4g} mm is outside the domain on that "
                           "axis ({:.4g}..{:.4g} mm).".format(pos, lo, hi))
        xs.write_state(domain, axis=state["axis"])
        xs.write_plane_state(domain, position_mm=pos, axis=state["axis"])
    if not on:
        counts = xs.set_active(doc, False)
    elif grid is not None:
        counts = xs.set_grid(doc, bool(grid))
        if not grid:
            counts = xs.set_active(doc, True)
    else:
        counts = xs.set_active(doc, True)
    sync = getattr(xs, "_sync_toggle_actions", None)
    if sync is not None:
        sync(doc)
    state = xs.read_state(domain, doc)
    state["axis"] = "xyz"[state["axis"]]
    state["sectioned"] = list(counts) if isinstance(counts, tuple) else counts
    return state


@api_function("preview")
def voxel_preview(doc=None, plane="z", offset=None, path=None,
                  quantity="material", show_grid=True):
    """Render what the solver will see: the voxelized materials on a plane.

    Voxelizes the document (like a run's export, but nothing is written to a
    run folder) and draws one grid slice: PEC black, dielectrics coloured by
    permittivity (or conductivity), with the cell grid. Useful to check that
    thin features survive the mesh. Can take a while on fine grids.

    :param plane: slice normal ``'x'|'y'|'z'``.
    :param offset: slice position along that axis (default: domain centre;
        default unit mm).
    :param path: output ``.png``.
    :param quantity: ``'material'`` (eps + PEC), ``'eps'``, ``'sigma'`` or
        ``'pec'``.
    :param show_grid: draw cell boundaries (skipped above 300 lines).
    :returns: ``{"path", "plane", "offset_mm", "index", "cells", "pec_cells",
        "materials_seen": [eps values]}``.
    """
    import numpy as np

    from wavesim_gui import voxelize as vox_mod

    doc = c.get_doc(doc)
    c.get_sim(doc)
    path = _check_png_path(path)
    axis = {"x": 0, "y": 1, "z": 2, "yz": 0, "xz": 1, "xy": 2}.get(
        str(plane).strip().lower())
    if axis is None:
        raise ApiError("plane must be x, y or z.")
    if quantity not in ("material", "eps", "sigma", "pec"):
        raise ApiError("quantity must be material, eps, sigma or pec.")
    doc.recompute()
    with c.capture_warnings():
        spec, arrays = vox_mod.build_job_from_document(doc, steps=1)
    if spec is None or arrays is None:
        raise ApiError("Nothing to voxelize: assign a body to a material.")
    grid = spec["grid"]
    counts = [int(grid["Nx"]), int(grid["Ny"]), int(grid["Nz"])]
    nodes = []
    for i, key in enumerate("xyz"):
        if grid.get(key):
            nodes.append(np.asarray(grid[key], dtype=float) * 1000.0)
        else:
            step = float(grid["d" + key]) * 1000.0
            nodes.append(np.arange(counts[i] + 1) * step)
    # Solver frame is offset from world coordinates by the grid origin.
    origin = [float(v) * 1000.0 for v in spec["origin_m"]]
    nodes = [n - n[0] + float(o) for n, o in zip(nodes, origin)]
    centres = [0.5 * (n[:-1] + n[1:]) for n in nodes]
    if offset is None:
        pos = 0.5 * (nodes[axis][0] + nodes[axis][-1])
    else:
        pos = c.quantity_value(offset, "length", what="offset")
    index = int(np.clip(np.searchsorted(nodes[axis], pos) - 1, 0,
                        counts[axis] - 1))
    comp = "xyz"[axis]
    eps = np.asarray(arrays.get("eps_" + comp, arrays.get("eps_z")))
    pec = np.asarray(arrays.get("pec_mask", np.zeros_like(eps, dtype=bool)))
    # Conformal PEC describes conductors by the open fraction of each Yee
    # face rather than by the mask; a mostly closed cell reads as metal here.
    open_key = "pec_face_open_" + comp
    if open_key in arrays:
        pec = pec | (np.asarray(arrays[open_key]) < 0.5)
    sigma = arrays.get("sigma_" + comp)

    def cut(a):
        a = np.asarray(a)
        sl = [slice(None)] * 3
        sl[axis] = min(index, a.shape[axis] - 1)
        return a[tuple(sl)]

    eps2, pec2 = cut(eps), cut(pec).astype(bool)
    others = [i for i in range(3) if i != axis]
    ua, ub = nodes[others[0]], nodes[others[1]]

    import matplotlib
    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap

    fig, ax = plt.subplots(figsize=(7, 6), dpi=110)
    if quantity == "sigma" and sigma is not None:
        data = cut(sigma)
        label = "sigma (S/m)"
    elif quantity == "pec":
        data = pec2.astype(float)
        label = "PEC"
    else:
        data = eps2
        label = "relative permittivity"
    data = np.asarray(data, dtype=float)[:len(ua) - 1, :len(ub) - 1]
    mesh = ax.pcolormesh(ua, ub, data.T, cmap="viridis", shading="flat")
    fig.colorbar(mesh, ax=ax, label=label)
    if quantity in ("material", "eps"):
        mask = np.ma.masked_where(~pec2[:len(ua) - 1, :len(ub) - 1].T,
                                  np.ones_like(data.T))
        ax.pcolormesh(ua, ub, mask, cmap=ListedColormap(["black"]),
                      shading="flat")
    if show_grid and len(ua) + len(ub) <= 300:
        for x in ua:
            ax.axvline(x, color="0.6", lw=0.3)
        for y in ub:
            ax.axhline(y, color="0.6", lw=0.3)
    names = "xyz"
    ax.set_xlabel("{} (mm)".format(names[others[0]]))
    ax.set_ylabel("{} (mm)".format(names[others[1]]))
    ax.set_aspect("equal")
    ax.set_title("Voxels on {} = {:.4g} mm (cell {}), PEC black".format(
        names[axis], centres[axis][index], index))
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return {"path": path, "plane": names[axis],
            "offset_mm": float(centres[axis][index]), "index": index,
            "cells": counts, "pec_cells_in_slice": int(pec2.sum()),
            "materials_seen": sorted(set(round(float(v), 4)
                                         for v in np.unique(eps2)))}
