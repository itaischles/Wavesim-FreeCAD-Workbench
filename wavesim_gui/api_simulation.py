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
    "new_simulation", "get_simulation", "create_material", "assign_material",
    "unassign_material", "list_materials",
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
        sim.SolverMode = mode_label
    doc.recompute()
    return sim


@api_function("simulation")
def get_simulation(sim=None):
    """Return the Simulation container (raises if the document has none)."""
    return c.get_sim(sim)


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
