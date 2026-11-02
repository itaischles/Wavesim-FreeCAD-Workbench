# -*- coding: utf-8 -*-
"""Scripting API: simulation container, solver mode, domain, mesh, materials.

GUI-free counterparts of the New Simulation, Simulation panel, Domain panel,
New Material and Refine Body commands. See :mod:`wavesim_gui.api_common` for
the argument conventions (``sim`` first, references by name/label, quantities
as number/"3 mm"/"=PARAM" expressions, one undo step per call).
"""

import FreeCAD

from wavesim_gui import api_common as c
from wavesim_gui.api_common import ApiError, api_function

__all__ = [
    "new_simulation", "get_simulation", "set_solver_mode", "set_run_settings",
    "set_domain", "set_mesh", "refine_body", "mesh_info",
    "create_material", "assign_material", "unassign_material",
    "list_materials", "set_material", "list_setup", "get_object_props",
    "set_object_props", "delete_setup_object", "validate_setup",
]

_MODE_ALIASES = {
    "fdtd": "fdtd", "full wave": "fdtd", "full-wave": "fdtd",
    "electrostatic": "electrostatic", "es": "electrostatic",
    "electrostatics": "electrostatic",
}


def _mode_label(mode):
    from wavesim_gui import commands
    token = _MODE_ALIASES.get(str(mode).strip().lower())
    if token is None:
        raise ApiError("Invalid solver mode {!r}; use 'fdtd' or "
                       "'electrostatic'.".format(mode))
    return commands.MODE_LABELS[0 if token == "fdtd" else 1]


# --------------------------------------------------------------------------- #
# Simulation container
# --------------------------------------------------------------------------- #

@api_function("simulation")
def new_simulation(doc=None, label="Simulation", mode="fdtd"):
    """Create the Simulation container (one per document) and return it.

    Creates, exactly like *Wavesim -> New Simulation*: the Simulation group,
    its Domain, the Materials/Sources/Monitors groups, and the default Vacuum
    (domain background) and PEC materials.

    :param doc: document (None = active; a new document is NOT created here).
    :param label: label of the container.
    :param mode: ``'fdtd'`` (full wave, default) or ``'electrostatic'``.
    """
    from wavesim_gui import commands
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import materials as materials_mod

    doc = c.get_doc(doc)
    if commands.active_simulation(doc) is not None:
        raise ApiError("Document {!r} already has a Simulation.".format(
            doc.Label))
    mode_label = _mode_label(mode)
    with c.transaction(doc, "Wavesim: New Simulation"):
        sim = doc.addObject("App::DocumentObjectGroupPython", "Simulation")
        commands.SimulationContainer(sim)
        sim.Label = label or "Simulation"
        if sim.ViewObject is not None and hasattr(commands,
                                                  "SimulationViewProvider"):
            commands.SimulationViewProvider(sim.ViewObject)
        # The domain sits first, directly under the Simulation in the tree.
        domain = domain_mod.create_domain(doc, sim)
        for name in commands._CHILD_GROUPS:
            grp = doc.addObject("App::DocumentObjectGroup", name)
            grp.Label = name
            sim.addObject(grp)
        vacuum, _pec = materials_mod.create_default_materials(doc, sim)
        if hasattr(domain, "Background"):
            domain.Background = vacuum
        if mode_label != commands.MODE_LABELS[0]:
            commands.apply_simulation_settings(sim,
                                               solver_mode_label=mode_label)
    doc.recompute()
    return sim


@api_function("simulation")
def get_simulation(sim=None):
    """Return the Simulation container (raises if the document has none)."""
    return c.get_sim(sim)


_UNSET_ALIASES = {"grounded": 0, "ground": 0, "0v": 0,
                  "floating": 1, "float": 1, "0c": 1}


@api_function("simulation")
def set_solver_mode(sim=None, mode="fdtd", extract_capacitance=None,
                    unset_conductors=None):
    """Switch between full-wave FDTD and electrostatics.

    Switching re-derives the Domain cell size from the new driver (max
    frequency in FDTD, thinnest assigned feature in electrostatics), exactly
    like the Simulation panel.

    :param mode: ``'fdtd'`` or ``'electrostatic'``.
    :param extract_capacitance: electrostatic only -- also extract the
        capacitance matrix (one extra solve per conductor). Default unchanged.
    :param unset_conductors: electrostatic only -- what a PEC body with no
        potential set (and touching none that has one) does: ``'grounded'``
        (0 V) or ``'floating'`` (0 C).
    """
    from wavesim_gui import commands

    sim = c.get_sim(sim)
    label = _mode_label(mode)
    unset_label = None
    if unset_conductors is not None:
        idx = _UNSET_ALIASES.get(str(unset_conductors).strip().lower())
        if idx is None:
            raise ApiError("unset_conductors must be 'grounded' or "
                           "'floating'.")
        unset_label = commands.UNSET_LABELS[idx]
    with c.transaction(sim.Document, "Wavesim: Edit Simulation"):
        commands.apply_simulation_settings(
            sim, solver_mode_label=label,
            extract_cap=None if extract_capacitance is None
            else bool(extract_capacitance))
        if unset_label is not None:
            sim.UnsetConductorMode = unset_label
    sim.Document.recompute()
    return sim


@api_function("simulation")
def set_run_settings(sim=None, max_time=None, max_frequency=None,
                     time_unit=None, frequency_unit=None, subpixel=None,
                     conformal_pec=None, conformal_area_threshold=None):
    """Set the Simulation panel's run settings. ``None`` leaves a value as is.

    :param max_time: simulated time span (default unit s; e.g. ``"5 ns"``,
        or a param expression ``"=T_MAX"``). FDTD steps = max_time / dt.
    :param max_frequency: highest frequency of interest (default unit Hz;
        e.g. ``"10 GHz"``). Drives the default cell size: changing it
        re-derives the Domain's Dx/Dy/Dz like the Simulation panel does.
    :param time_unit: display unit for times in the GUI (s ms us ns ps fs).
    :param frequency_unit: display unit for frequencies (Hz kHz MHz GHz THz).
    :param subpixel: subpixel smoothing of dielectric interfaces (bool).
    :param conformal_pec: conformal (cut-cell) PEC conductors (bool).
    :param conformal_area_threshold: starting clamp fraction, 0 <= x < 1.
    """
    from wavesim_gui import commands
    from wavesim_gui import units

    sim = c.get_sim(sim)
    doc = sim.Document
    if time_unit is not None:
        time_unit = c.enum_choice(time_unit, units.time_unit_labels(),
                                  "time unit")
    if frequency_unit is not None:
        frequency_unit = c.enum_choice(frequency_unit,
                                       units.freq_unit_labels(),
                                       "frequency unit")
    if conformal_area_threshold is not None:
        thr = float(conformal_area_threshold)
        if not 0.0 <= thr < 1.0:
            raise ApiError("conformal_area_threshold must be in [0, 1).")
    prev_freq = float(getattr(sim, "MaxFrequency", 0.0))
    with c.transaction(doc, "Wavesim: Edit Simulation"):
        time_s = freq_hz = None
        for prop, value, dim in (("MaxTime", max_time, "time"),
                                 ("MaxFrequency", max_frequency, "frequency")):
            if value is None:
                continue
            kind, number = c.parse_quantity(value, dim)
            if kind == "value" and number <= 0:
                raise ApiError("{} must be positive.".format(prop))
            if kind == "expr":
                c.set_quantity(sim, prop, value, dim)
                doc.recompute()
            else:
                try:
                    sim.setExpression(prop, None)
                except Exception:
                    pass
                if prop == "MaxTime":
                    time_s = number
                else:
                    freq_hz = number
        commands.apply_simulation_settings(
            sim, time_unit=time_unit, frequency_unit=frequency_unit,
            max_time_s=time_s, max_frequency_hz=freq_hz, subpixel=subpixel,
            conformal_pec=conformal_pec,
            previous_max_frequency_hz=prev_freq)
        if conformal_area_threshold is not None:
            sim.ConformalAreaThreshold = float(conformal_area_threshold)
    doc.recompute()
    return sim


_FACE_ALIASES = {
    "x0": "x0", "xmin": "x0", "-x": "x0", "x1": "x1", "xmax": "x1", "+x": "x1",
    "y0": "y0", "ymin": "y0", "-y": "y0", "y1": "y1", "ymax": "y1", "+y": "y1",
    "z0": "z0", "zmin": "z0", "-z": "z0", "z1": "z1", "zmax": "z1", "+z": "z1",
}


def _face(name):
    face = _FACE_ALIASES.get(str(name).strip().lower())
    if face is None:
        raise ApiError("Invalid face {!r}; use x0 x1 y0 y1 z0 z1 (or xmin, "
                       "xmax, ...).".format(name))
    return face


def _per_face(value, what):
    """Scalar -> all six faces; dict -> its faces only."""
    if isinstance(value, dict):
        return {_face(k): v for k, v in value.items()}
    return {f: value for f in ("x0", "x1", "y0", "y1", "z0", "z1")}


def _domain(sim):
    from wavesim_gui import domain as domain_mod
    domain = domain_mod.find_domain(sim)
    if domain is None:
        raise ApiError("The simulation has no Domain object; re-create it.")
    return domain


def _auto_cell_size(sim, domain):
    from wavesim_gui import domain as domain_mod
    size_m = domain_mod.suggested_cell_size_m(sim, domain=domain)
    if size_m is not None:
        for prop in ("Dx", "Dy", "Dz"):
            try:
                domain.setExpression(prop, None)
            except Exception:
                pass
            setattr(domain, prop, "{} mm".format(size_m * 1000.0))
    return size_m


@api_function("simulation")
def set_domain(sim=None, spacing=None, boundaries=None, pml_cells=None,
               background=None, es_boundaries=None):
    """Set the Domain box padding, boundary conditions and background medium.

    The domain auto-sizes to every material-assigned body (plus sources and
    snapshot planes); ``spacing`` adds background medium around that bound.
    The PML sits *inside* the box (its outer ``pml_cells`` cells).

    :param spacing: background gap per face (default unit mm). A single value
        for all faces, or ``{"x0": 5, "z1": "10 mm", ...}`` (faces x0 x1 y0
        y1 z0 z1 = low/high x, y, z). Param expressions allowed.
    :param boundaries: full-wave boundary per face, ``'PML'`` or ``'PEC'``;
        single value or dict. Faces carrying a modal port / beam are forced
        by the port at export regardless.
    :param pml_cells: PML depth in cells (integer 1..100, default 8).
    :param background: material filling empty space (name/label), e.g.
        ``"Vacuum"``. Re-derives the cell size, as the Domain panel does.
    :param es_boundaries: electrostatic boundary per face, ``'ground'``
        (phi = 0) or ``'symmetry'`` (no normal field).
    """
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import materials as materials_mod

    sim = c.get_sim(sim)
    doc = sim.Document
    domain = _domain(sim)
    face_prop = {f: p for f, p, _d in domain_mod._FACE_PROPS}
    es_prop = {f: p for f, p, _d in domain_mod._ES_FACE_PROPS}
    spacing_prop = {f: p for f, p, _d in domain_mod._SPACING_PROPS}
    with c.transaction(doc, "Wavesim: Edit Domain"):
        if spacing is not None:
            for face, value in _per_face(spacing, "spacing").items():
                kind, number = c.parse_quantity(value, "length")
                if kind == "value" and number < 0:
                    raise ApiError("Spacing must be >= 0 mm.")
                c.set_quantity(domain, spacing_prop[face], value, "length")
        if boundaries is not None:
            for face, value in _per_face(boundaries, "boundaries").items():
                setattr(domain, face_prop[face],
                        c.enum_choice(value, domain_mod._BC_CHOICES,
                                      "boundary"))
        if es_boundaries is not None:
            for face, value in _per_face(es_boundaries,
                                         "es_boundaries").items():
                setattr(domain, es_prop[face],
                        c.enum_choice(value, domain_mod._ES_BC_CHOICES,
                                      "electrostatic boundary"))
        if pml_cells is not None:
            n = int(pml_cells)
            if n != pml_cells or not 1 <= n <= 100:
                raise ApiError("pml_cells must be an integer in 1..100.")
            domain.PMLThickness = n
        if background is not None:
            mat = c.find_object(doc, background, "material")
            if not materials_mod.is_material(mat):
                raise ApiError("{!r} is not a Wavesim material.".format(
                    mat.Label))
            domain.Background = mat
            doc.recompute()
            _auto_cell_size(sim, domain)
    doc.recompute()
    return domain


@api_function("simulation")
def set_mesh(sim=None, cell_size=None, cells_per_wavelength=None,
             cells_per_feature=None, nonuniform=None, max_grading=None,
             min_cell_size=None, curvature_refinement=None):
    """Set the grid: cell size, resolution rules and non-uniform grading.

    In full wave the default cell size is ``c / (fmax * cells_per_wavelength
    * n)``; in electrostatics it is the thinnest assigned feature divided by
    ``cells_per_feature``. Like the Domain panel, changing a resolution rule
    (or the grid type) without giving ``cell_size`` re-derives it.

    :param cell_size: ``"auto"`` (derive), one length for cubic cells, or
        ``[dx, dy, dz]`` (default unit mm; param expressions allowed). With a
        non-uniform grid it is the coarse (background) target.
    :param cells_per_wavelength: integer 1..1000 (full wave), default 20.
    :param cells_per_feature: integer 1..1000 (electrostatic), default 10.
    :param nonuniform: graded non-uniform (snapped) grid on/off.
    :param max_grading: largest neighbour cell-size ratio, 1.0..5.0
        (solver guidance 1.5-2).
    :param min_cell_size: smallest cell the snapper may create (mm, 0 = no
        limit).
    :param curvature_refinement: refinement factor on curved surfaces
        (1..64, 1 = off).
    :returns: :func:`mesh_info` after the change.
    """
    sim = c.get_sim(sim)
    doc = sim.Document
    domain = _domain(sim)
    rederive = cell_size is None and any(
        v is not None for v in (cells_per_wavelength, cells_per_feature,
                                nonuniform))
    with c.transaction(doc, "Wavesim: Edit Domain"):
        if cells_per_wavelength is not None:
            n = int(cells_per_wavelength)
            if n != cells_per_wavelength or not 1 <= n <= 1000:
                raise ApiError("cells_per_wavelength must be an integer "
                               "in 1..1000.")
            domain.CellsPerWavelength = n
        if cells_per_feature is not None:
            n = int(cells_per_feature)
            if n != cells_per_feature or not 1 <= n <= 1000:
                raise ApiError("cells_per_feature must be an integer in "
                               "1..1000.")
            domain.CellsPerFeature = n
        if nonuniform is not None:
            domain.UseNonuniformGrid = bool(nonuniform)
        if max_grading is not None:
            ratio = float(max_grading)
            if not 1.0 <= ratio <= 5.0:
                raise ApiError("max_grading must be in 1.0..5.0.")
            domain.MaxGradingRatio = ratio
        if min_cell_size is not None:
            kind, number = c.parse_quantity(min_cell_size, "length")
            if kind == "value" and number < 0:
                raise ApiError("min_cell_size must be >= 0.")
            c.set_quantity(domain, "MinCellSize", min_cell_size, "length")
        if curvature_refinement is not None:
            factor = float(curvature_refinement)
            if not 1.0 <= factor <= 64.0:
                raise ApiError("curvature_refinement must be in 1..64.")
            domain.CurvatureRefinement = factor
        if isinstance(cell_size, str) and cell_size.strip().lower() == "auto":
            rederive = True
        elif cell_size is not None:
            sizes = cell_size if isinstance(cell_size, (list, tuple)) \
                else [cell_size] * 3
            if len(sizes) != 3:
                raise ApiError("cell_size needs one value or [dx, dy, dz].")
            for prop, value in zip(("Dx", "Dy", "Dz"), sizes):
                kind, number = c.parse_quantity(value, "length")
                if kind == "value" and number <= 0:
                    raise ApiError("Cell sizes must be positive.")
                c.set_quantity(domain, prop, value, "length")
        if rederive:
            doc.recompute()
            if _auto_cell_size(sim, domain) is None:
                raise ApiError(
                    "Cannot derive a cell size: set a positive max frequency "
                    "(full wave) or assign a body to a material "
                    "(electrostatic) first, or give cell_size explicitly.")
    doc.recompute()
    return mesh_info(sim)


@api_function("simulation")
def mesh_info(sim=None):
    """Current grid: cell counts, sizes, dt and step count (no voxelizing).

    :returns: ``{"cells": [Nx, Ny, Nz], "total_cells", "cell_size_mm",
        "min_spacing_mm", "nonuniform", "dt_s", "steps", "max_time_s",
        "domain_min_mm", "domain_max_mm", "interior_min_mm",
        "interior_max_mm"}`` (interior = inside the PML).
    """
    from wavesim_gui import commands
    from wavesim_gui import domain as domain_mod

    sim = c.get_sim(sim)
    domain = _domain(sim)
    sim.Document.recompute()
    counts = [int(domain.Nx), int(domain.Ny), int(domain.Nz)]
    info = {
        "solver_mode": commands.solver_mode(sim),
        "cells": counts,
        "total_cells": counts[0] * counts[1] * counts[2],
        "cell_size_mm": [float(domain.Dx.Value), float(domain.Dy.Value),
                         float(domain.Dz.Value)],
        "nonuniform": bool(getattr(domain, "UseNonuniformGrid", False)),
        "domain_min_mm": c.to_jsonable(domain.DomainMin),
        "domain_max_mm": c.to_jsonable(domain.DomainMax),
        "interior_min_mm": c.to_jsonable(domain.PmlMin),
        "interior_max_mm": c.to_jsonable(domain.PmlMax),
        "max_time_s": float(getattr(sim, "MaxTime", 0.0)),
        "max_frequency_hz": float(getattr(sim, "MaxFrequency", 0.0)),
    }
    try:
        info["min_spacing_mm"] = [s * 1000.0 for s in
                                  domain_mod.min_spacings_m(domain)]
    except Exception:
        info["min_spacing_mm"] = info["cell_size_mm"]
    if commands.is_electrostatic(sim):
        info["dt_s"] = None
        info["steps"] = None
    else:
        info["dt_s"] = domain_mod.cfl_dt(domain)
        info["steps"] = domain_mod.time_steps_for(domain, info["max_time_s"])
    return info


@api_function("simulation")
def refine_body(sim=None, body=None, cells=None):
    """Ask for at least N grid cells across a body's bounding box per axis.

    Needs the non-uniform grid (see :func:`set_mesh`) and a body assigned to
    a material. ``cells=[0, 0, 0]`` clears the request.

    :param body: the body (name or label).
    :param cells: ``[nx, ny, nz]`` integers >= 0 (0 = no refinement on that
        axis), or one integer for all three.
    :returns: ``{"body", "cells", "measured_cells", "warnings"}``.
    """
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import refine

    sim = c.get_sim(sim)
    doc = sim.Document
    obj = c.find_object(doc, body, "body")
    if cells is None:
        raise ApiError("Give cells=[nx, ny, nz].")
    counts = cells if isinstance(cells, (list, tuple)) else [cells] * 3
    if len(counts) != 3 or any(int(n) != n or n < 0 for n in counts):
        raise ApiError("cells must be three integers >= 0.")
    if obj.Name not in refine._material_body_names(sim):
        raise ApiError("{!r} is not assigned to a material, so it is not "
                       "part of the simulation and cannot be refined.".format(
                           obj.Label))
    with c.transaction(doc, "Wavesim: Refine Body Mesh"):
        refine.set_body_cell_counts(obj, [int(n) for n in counts])
    domain_mod.notify_domain_inputs_changed(doc)
    domain = _domain(sim)
    warnings = []
    if not getattr(domain, "UseNonuniformGrid", False):
        warnings.append("The non-uniform grid is off, so this request is "
                        "ignored; enable it with set_mesh(nonuniform=True).")
    try:
        measured = list(refine.measured_cell_counts(domain, obj))
    except Exception:
        measured = None
    return {"body": obj.Label, "cells": [int(n) for n in counts],
            "measured_cells": measured, "warnings": warnings}


# --------------------------------------------------------------------------- #
# Materials
# --------------------------------------------------------------------------- #

@api_function("material")
def create_material(sim=None, label=None, eps=1.0, mu=1.0, sigma=0.0,
                    pec=False, color=None, bodies=None):
    """Create a Material and return it.

    :param label: unique label, e.g. ``"FR4"``. Required.
    :param eps: relative permittivity (dimensionless, >= 1 is typical; > 0).
    :param mu: relative permeability (dimensionless, > 0).
    :param sigma: conductivity in S/m (>= 0); a quantity like ``"5.8e7 S/m"``.
    :param pec: perfect electric conductor (eps/mu/sigma then ignored).
    :param color: ``[r, g, b]`` floats 0..1 (optional).
    :param bodies: optional bodies to assign right away (see
        :func:`assign_material`).

    ``eps``/``mu``/``sigma`` also accept param expressions (``"=EPS_SUB"``).
    """
    from wavesim_gui import materials as materials_mod

    sim = c.get_sim(sim)
    doc = sim.Document
    label = c.check_new_label(doc, label, "material")
    with c.transaction(doc, "Wavesim: New Material"):
        mat = materials_mod.create_material(
            doc, sim, label, pec=bool(pec),
            color=tuple(float(x) for x in color) if color else None)
        _set_material_params(mat, eps=eps, mu=mu, sigma=sigma)
        c.set_label(mat, label)
        if bodies:
            assign_material(sim, bodies, mat)
    doc.recompute()
    return mat


def _set_material_params(mat, eps=None, mu=None, sigma=None):
    for prop, value, dim in (("Eps", eps, "dimensionless"),
                             ("Mu", mu, "dimensionless"),
                             ("Sigma", sigma, "conductivity")):
        if value is None:
            continue
        kind, number = c.parse_quantity(value, dim)
        if kind == "value":
            if prop in ("Eps", "Mu") and number <= 0:
                raise ApiError("{} must be > 0 (got {}).".format(prop, number))
            if prop == "Sigma" and number < 0:
                raise ApiError("Sigma must be >= 0 S/m (got {}).".format(
                    number))
        c.set_quantity(mat, prop, value, dim)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


@api_function("material")
def assign_material(sim=None, bodies=None, material=None):
    """Assign one or more solid bodies to *material*.

    A body belongs to exactly one material: it is removed from any other one.
    Assigning to a PEC material also adds the body's conductor (potential)
    properties. The domain re-sizes to the assigned bodies, as in the GUI.

    :param bodies: a body reference or a list of them (name or label).
    :param material: the material (name or label), e.g. ``"PEC"``.
    """
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import materials as materials_mod

    sim = c.get_sim(sim)
    doc = sim.Document
    mat = c.find_object(doc, material, "material")
    if not materials_mod.is_material(mat):
        raise ApiError("{!r} is not a Wavesim material.".format(mat.Label))
    objs = [c.find_object(doc, b, "body") for b in _as_list(bodies)]
    if not objs:
        raise ApiError("No bodies given to assign.")
    for body in objs:
        if not materials_mod._is_solid_body(body):
            raise ApiError("{!r} has no solid shape; only solids can carry a "
                           "material.".format(body.Label))
    with c.transaction(doc, "Wavesim: Assign Material"):
        for body in objs:
            materials_mod.assign_body(mat, body)
        _sync_visibility(mat)
        domain_mod.notify_materials_changed(doc)
    return mat


@api_function("material")
def unassign_material(sim=None, bodies=None):
    """Remove bodies from whatever material holds them."""
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import materials as materials_mod

    sim = c.get_sim(sim)
    doc = sim.Document
    objs = [c.find_object(doc, b, "body") for b in _as_list(bodies)]
    with c.transaction(doc, "Wavesim: Unassign Material"):
        for body in objs:
            materials_mod._detach_body(body, keep=None)
            materials_mod._restore_body_color(body)
        domain_mod.notify_materials_changed(doc)
    return [o.Label for o in objs]


def _sync_visibility(mat):
    try:
        from wavesim_gui import visibility
        visibility.sync_from_children(mat)
    except Exception:
        pass


@api_function("material")
def list_materials(sim=None):
    """Every material: label, eps, mu, sigma (S/m), pec, assigned bodies."""
    from wavesim_gui import materials as materials_mod

    sim = c.get_sim(sim)
    out = []
    for mat in materials_mod.find_materials(sim):
        out.append({
            "name": mat.Name, "label": mat.Label,
            "eps": float(mat.Eps), "mu": float(mat.Mu),
            "sigma_S_per_m": float(materials_mod.material_sigma(mat)),
            "pec": bool(mat.Pec),
            "bodies": [b.Label for b in (mat.Bodies or [])],
        })
    return out


@api_function("material")
def set_material(sim=None, material=None, label=None, eps=None, mu=None,
                 sigma=None, pec=None, color=None):
    """Edit an existing material (the New/Edit Material panel's fields).

    ``None`` leaves a field unchanged. Units as in :func:`create_material`.
    Turning ``pec`` on gives its bodies conductor (potential) properties.
    """
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import materials as materials_mod

    sim = c.get_sim(sim)
    doc = sim.Document
    mat = c.find_object(doc, material, "material")
    if not materials_mod.is_material(mat):
        raise ApiError("{!r} is not a Wavesim material.".format(mat.Label))
    if label is not None and label != mat.Label:
        label = c.check_new_label(doc, label, "material")
    with c.transaction(doc, "Wavesim: Edit Material"):
        if pec is not None:
            mat.Pec = bool(pec)
        _set_material_params(mat, eps=eps, mu=mu, sigma=sigma)
        if color is not None:
            mat.Color = tuple(float(x) for x in color)
        if label is not None and label != mat.Label:
            c.set_label(mat, label)
        materials_mod.apply_material_color(mat)
        if mat.Pec:
            for body in getattr(mat, "Bodies", []) or []:
                materials_mod.ensure_potential_prop(body)
    doc.recompute()
    domain_mod.notify_materials_changed(doc)
    return mat


# --------------------------------------------------------------------------- #
# Generic setup objects
# --------------------------------------------------------------------------- #

def _walk(group):
    for obj in getattr(group, "Group", []) or []:
        yield obj
        if obj.TypeId.startswith("App::DocumentObjectGroup"):
            for sub in _walk(obj):
                yield sub


def _setup_objects(sim):
    """Every object in the simulation tree (not the groups themselves)."""
    return [o for o in _walk(sim)
            if not o.TypeId.startswith("App::DocumentObjectGroup")]


def _require_setup_object(sim, obj):
    if obj is sim or obj not in _setup_objects(sim):
        raise ApiError("{!r} is not a Wavesim setup object of this "
                       "simulation. Geometry is edited with the geometry "
                       "tools.".format(obj.Label))


@api_function("setup")
def list_setup(sim=None):
    """Structured description of the whole simulation setup.

    :returns: ``{"simulation": {...settings}, "mesh": mesh_info,
        "domain": {...}, "materials": [...], "sources": [...],
        "monitors": [...], "refined_bodies": [...], "results": [...]}``
        where sources/monitors are ``{name, label, type, properties,
        expressions}`` (lengths in mm, frequencies in Hz, times in s).
    """
    from wavesim_gui import commands
    from wavesim_gui import refine

    sim = c.get_sim(sim)
    domain = _domain(sim)
    groups = {g.Label: g for g in sim.Group
              if g.TypeId.startswith("App::DocumentObjectGroup")}
    out = {
        "simulation": {
            "label": sim.Label,
            "solver_mode": commands.solver_mode(sim),
            "max_time_s": float(getattr(sim, "MaxTime", 0.0)),
            "max_frequency_hz": float(getattr(sim, "MaxFrequency", 0.0)),
            "subpixel": bool(getattr(sim, "SubpixelSmoothing", False)),
            "conformal_pec": bool(getattr(sim, "ConformalPEC", False)),
            "extract_capacitance": commands.extract_capacitance(sim),
            "unset_conductors": commands.unset_conductor_mode(sim),
            "expressions": c.describe(sim).get("expressions", {}),
        },
        "mesh": mesh_info(sim),
        "domain": c.describe(domain),
        "materials": list_materials(sim),
        "sources": [c.describe(o) for o in
                    (_walk(groups["Sources"]) if "Sources" in groups else [])],
        "monitors": [c.describe(o) for o in
                     (_walk(groups["Monitors"]) if "Monitors" in groups
                      else [])],
        "refined_bodies": [{"body": b.Label, "cells": list(n)}
                           for b, n in refine.refined_bodies(sim)],
        "results": [o.Label for o in (_walk(groups["Results"])
                                      if "Results" in groups else [])],
    }
    return out


@api_function("setup")
def get_object_props(sim=None, obj=None):
    """All visible properties of a setup object, with dimensions and units.

    :returns: ``describe(obj)`` plus ``"dimensions": {prop: dimension}`` and
        ``"storage_units": {dimension: unit}`` (mm, Hz, s, deg, ...).
    """
    sim = c.get_sim(sim)
    target = c.find_object(sim.Document, obj)
    out = c.describe(target)
    dims = {}
    for prop in out["properties"]:
        try:
            dim = c.property_dimension(target, prop)
        except ApiError:
            dim = None
        if dim:
            dims[prop] = dim
    out["dimensions"] = dims
    out["storage_units"] = dict(c.STORAGE_UNIT)
    return out


_PROTECTED_PROPS = {"WavesimType", "Type", "Proxy", "ExpressionEngine",
                    "Label", "Label2", "Group", "Placement", "Shape"}


def _assign_prop(doc, obj, prop, value):
    type_id = obj.getTypeIdOfProperty(prop)
    mode = obj.getEditorMode(prop)
    if prop in _PROTECTED_PROPS or "ReadOnly" in mode or "Hidden" in mode:
        raise ApiError("Property {!r} of {!r} cannot be set directly.".format(
            prop, obj.Label))
    if type_id == "App::PropertyEnumeration":
        setattr(obj, prop, c.enum_choice(
            value, obj.getEnumerationsOfProperty(prop), prop))
    elif type_id == "App::PropertyBool":
        if not isinstance(value, bool):
            raise ApiError("{} expects true/false.".format(prop))
        setattr(obj, prop, value)
    elif type_id in ("App::PropertyString",):
        setattr(obj, prop, str(value))
    elif type_id in ("App::PropertyLink", "App::PropertyLinkGlobal"):
        setattr(obj, prop, None if value is None
                else c.find_object(doc, value))
    elif type_id in ("App::PropertyLinkList", "App::PropertyLinkListGlobal"):
        setattr(obj, prop, [c.find_object(doc, v) for v in value])
    elif type_id in ("App::PropertyVector", "App::PropertyVectorDistance",
                     "App::PropertyPosition"):
        c.set_vector(obj, prop, value)
    elif type_id == "App::PropertyColor":
        setattr(obj, prop, tuple(float(x) for x in value))
    else:
        dim = c.property_dimension(obj, prop)
        if dim is None:
            raise ApiError("Property {!r} ({}) is not settable through this "
                           "tool.".format(prop, type_id))
        c.set_quantity(obj, prop, value, dim)


@api_function("setup")
def set_object_props(sim=None, obj=None, props=None):
    """Set properties of a setup object (source, port, monitor, material,
    Domain, Simulation) by name, with validation.

    Use :func:`get_object_props` first to see names, dimensions and units.
    Values follow the quantity rules (number in the storage unit, ``"2 GHz"``,
    or ``"=PARAM"`` to bind a parameter); enumerations accept any listed
    choice (case-insensitive); links take object names/labels; vectors take
    ``[x, y, z]``. Read-only/derived properties are refused.

    :param props: ``{"Frequency": "2.4 GHz", "Position": [0, 0, "=H/2"]}``.
    """
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import materials as materials_mod

    sim = c.get_sim(sim)
    doc = sim.Document
    target = c.find_object(doc, obj)
    if target is not sim:
        _require_setup_object(sim, target)
    if not isinstance(props, dict) or not props:
        raise ApiError("props must be a non-empty {name: value} dict.")
    for prop in props:
        if prop not in target.PropertiesList:
            raise ApiError("{!r} has no property {!r}. Available: {}".format(
                target.Label, prop, ", ".join(sorted(
                    p for p in target.PropertiesList
                    if p not in _PROTECTED_PROPS))))
    with c.transaction(doc, "Wavesim: Edit {}".format(target.Label)):
        for prop, value in props.items():
            _assign_prop(doc, target, prop, value)
    doc.recompute()
    if materials_mod.is_material(target):
        materials_mod.apply_material_color(target)
        domain_mod.notify_materials_changed(doc)
    else:
        domain_mod.notify_domain_inputs_changed(doc)
    return target


@api_function("setup")
def delete_setup_object(sim=None, obj=None):
    """Delete a source, port, monitor, material or result object.

    Materials: their bodies are detached first (the bodies themselves stay);
    the domain's background material cannot be deleted. The Simulation,
    Domain and the Materials/Sources/Monitors groups cannot be deleted.
    """
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import materials as materials_mod

    sim = c.get_sim(sim)
    doc = sim.Document
    target = c.find_object(doc, obj)
    _require_setup_object(sim, target)
    domain = _domain(sim)
    if target is domain:
        raise ApiError("The Domain cannot be deleted.")
    is_mat = materials_mod.is_material(target)
    if is_mat and getattr(domain, "Background", None) is target:
        raise ApiError("{!r} is the domain background; choose another "
                       "background first (set_domain).".format(target.Label))
    label = target.Label
    with c.transaction(doc, "Wavesim: Delete {}".format(label)):
        if is_mat:
            for body in list(getattr(target, "Bodies", []) or []):
                materials_mod._restore_body_color(body)
            target.Bodies = []
        doc.removeObject(target.Name)
    doc.recompute()
    if is_mat:
        domain_mod.notify_materials_changed(doc)
    else:
        domain_mod.notify_domain_inputs_changed(doc)
    return {"deleted": label}


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

#: Rough FDTD memory per cell (fields, coefficients, CPML auxiliaries; float64).
_BYTES_PER_CELL_FDTD = 160
_BYTES_PER_CELL_ES = 96
#: Rough numba throughput used for the runtime estimate (cell-updates / s).
_DEFAULT_THROUGHPUT = 1.0e8


def _solid_geometry(doc):
    """Visible top-level solids that are not Wavesim objects."""
    out = []
    for obj in doc.Objects:
        if c.wavesim_type(obj) or not hasattr(obj, "Shape"):
            continue
        if obj.TypeId.startswith(("App::", "Spreadsheet::", "Sketcher::")):
            continue
        try:
            if not obj.Shape.Solids:
                continue
        except Exception:
            continue
        consumers = [o for o in obj.InList
                     if not c.wavesim_type(o) and hasattr(o, "Shape")
                     and not o.TypeId.startswith("App::")]
        if consumers:
            continue
        out.append(obj)
    return out


def _inside(point, lo, hi, tol=1e-6):
    return all(lo[i] - tol <= point[i] <= hi[i] + tol for i in range(3))


@api_function("setup")
def validate_setup(sim=None, max_cells=None, throughput=None):
    """Check the setup before running; nothing is voxelized or written.

    :param max_cells: hard cap on the total cell count (e.g. the PARAMS
        ``MAX_NUM_CELLS``); exceeding it is an error.
    :param throughput: assumed solver speed in cell-updates per second for
        the runtime estimate (default 1e8, a rough multicore numba figure).
    :returns: ``{"ok": bool, "errors": [...], "warnings": [...],
        "estimates": {"cells", "total_cells", "memory_MB", "dt_s", "steps",
        "runtime_s"}}``. ``ok`` is false when there is any error.
    """
    from wavesim_gui import commands
    from wavesim_gui import domain as domain_mod
    from wavesim_gui import materials as materials_mod

    sim = c.get_sim(sim)
    doc = sim.Document
    doc.recompute()
    errors, warnings = [], []
    domain = _domain(sim)
    electrostatic = commands.is_electrostatic(sim)
    mats = materials_mod.find_materials(sim)
    assigned = []
    for mat in mats:
        for body in getattr(mat, "Bodies", []) or []:
            assigned.append((body, mat))
    if not assigned:
        errors.append("No body is assigned to a material, so there is "
                      "nothing to simulate.")
    for body, mat in assigned:
        try:
            if body.Shape.isNull() or not body.Shape.isValid():
                errors.append("Body {!r} ({}) has an invalid shape.".format(
                    body.Label, mat.Label))
        except Exception:
            errors.append("Body {!r} has no usable shape.".format(body.Label))
    assigned_names = {b.Name for b, _m in assigned}
    for obj in _solid_geometry(doc):
        if obj.Name not in assigned_names:
            try:
                visible = obj.Visibility
            except Exception:
                visible = True
            if visible:
                warnings.append(
                    "Solid {!r} has no material, so it is not part of the "
                    "simulation (assign it, or delete/hide it).".format(
                        obj.Label))
    # Overlapping assigned solids: which one wins is order-dependent.
    bodies = [b for b, _m in assigned][:80]
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            a, b = bodies[i], bodies[j]
            try:
                if not a.Shape.BoundBox.intersect(b.Shape.BoundBox):
                    continue
                vol = a.Shape.common(b.Shape).Volume
            except Exception:
                continue
            if vol > 1e-6:
                warnings.append("Bodies {!r} and {!r} overlap ({:.4g} mm^3); "
                                "the overlap gets one material only.".format(
                                    a.Label, b.Label, vol))

    info = mesh_info(sim)
    total = info["total_cells"]
    if total <= 0:
        errors.append("The grid has no cells (assign geometry and set a "
                      "cell size).")
    if max_cells is not None and total > float(max_cells):
        errors.append("The grid has {:,} cells, over the cap of {:,} "
                      "(MAX_NUM_CELLS). Coarsen the mesh (set_mesh) or "
                      "shrink the domain.".format(total, int(float(max_cells))))
    lo = info["domain_min_mm"]
    hi = info["domain_max_mm"]
    ilo = info["interior_min_mm"]
    ihi = info["interior_max_mm"]

    groups = {g.Label: g for g in sim.Group
              if g.TypeId.startswith("App::DocumentObjectGroup")}
    sources = list(_walk(groups["Sources"])) if "Sources" in groups else []
    monitors = list(_walk(groups["Monitors"])) if "Monitors" in groups else []
    for obj in sources + monitors:
        pos = getattr(obj, "Position", None)
        if not isinstance(pos, FreeCAD.Vector) or total <= 0:
            continue
        point = [pos.x, pos.y, pos.z]
        if not _inside(point, lo, hi):
            errors.append("{!r} at {} mm is outside the domain {}..{} "
                          "mm.".format(obj.Label, _fmt(point), _fmt(lo),
                                       _fmt(hi)))
        elif not electrostatic and not _inside(point, ilo, ihi):
            warnings.append("{!r} at {} mm lies inside the PML; move it into "
                            "the interior {}..{} mm or add spacing.".format(
                                obj.Label, _fmt(point), _fmt(ilo),
                                _fmt(ihi)))

    if electrostatic:
        if not any(materials_mod.is_pec(m) and getattr(m, "Bodies", None)
                   for m in mats):
            errors.append("Electrostatic mode needs at least one PEC body "
                          "(conductor).")
        elif not materials_mod.conductor_potentials(sim):
            warnings.append("No conductor has a potential set; every "
                            "conductor is at the default condition "
                            "(set_potential).")
    else:
        if float(getattr(sim, "MaxTime", 0.0)) <= 0:
            errors.append("Max time must be positive (set_run_settings).")
        if commands.max_frequency_hz(sim) <= 0:
            errors.append("Max frequency must be positive "
                          "(set_run_settings).")
        driven = [o for o in sources if c.wavesim_type(o)]
        if not driven:
            errors.append("No source or port: a full-wave run needs an "
                          "excitation.")
        if not monitors:
            warnings.append("No monitor or probe: the run would record "
                            "nothing but port data.")
        try:
            warnings.extend(materials_mod.loss_warnings(
                sim, dt=domain_mod.cfl_dt(domain)))
        except Exception:
            pass
        try:
            from wavesim_gui import lumped_port
            warnings.extend(lumped_port.colocation_warnings(sim))
        except Exception:
            pass
    try:
        warnings.extend(domain_mod.shell_geometry_warnings(sim, domain))
    except Exception:
        pass

    per_cell = _BYTES_PER_CELL_ES if electrostatic else _BYTES_PER_CELL_FDTD
    estimates = {
        "cells": info["cells"], "total_cells": total,
        "memory_MB": round(total * per_cell / 1e6, 1),
        "dt_s": info["dt_s"], "steps": info["steps"],
    }
    rate = float(throughput or _DEFAULT_THROUGHPUT)
    if not electrostatic and info["steps"]:
        estimates["runtime_s"] = round(total * info["steps"] / rate, 1)
    else:
        estimates["runtime_s"] = None
    estimates["runtime_note"] = ("rough: assumes {:.3g} cell-updates/s; "
                                 "calibrate from a previous run's "
                                 "wall time".format(rate))
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "estimates": estimates}


def _fmt(point):
    return "[" + ", ".join("{:.4g}".format(v) for v in point) + "]"
