# Wavesim GUI → scripting API inventory

This is the inventory of every Wavesim workbench GUI command (plus the panels
that are opened by double-clicking objects) and of the GUI-free scripting API
call(s) that replace each one. It is written for workbench developers and for
whoever builds the AI agent, which drives FreeCAD through the
[Agent bridge](#4-agent-bridge). The MCP server lives in the
`wavesim-ai-harness` repository.

Contents

1. [The scripting API](#1-the-scripting-api): modules, conventions, the one
   creation path
2. [Master table: GUI command → object → API](#2-master-table-gui-command--object--api)
3. Per-area details:
   [3.1 Simulation](#31-simulation-container-solver-mode-run-settings) ·
   [3.2 Domain and mesh](#32-domain-and-mesh) ·
   [3.3 Materials](#33-materials) ·
   [3.4 Body refinement](#34-body-mesh-refinement) ·
   [3.5 Setup inspection](#35-setup-inspection-generic-edits-validation) ·
   [3.6 Parameters](#36-parameters-params-spreadsheet-bindings-fingerprint) ·
   [3.7 Excitation](#37-excitation-sources-ports-potentials-port-matrix) ·
   [3.8 Monitors](#38-monitors) ·
   [3.9 Geometry](#39-geometry) ·
   [3.10 Preview](#310-preview-screenshots-visibility-cross-section-voxels) ·
   [3.11 Run and results](#311-run-and-results) ·
   [3.12 Documents and settings](#312-documents-and-settings)
4. [Agent bridge](#4-agent-bridge)
5. [Alphabetical list of every registered API function](#5-alphabetical-list-of-every-registered-api-function)

---

## 1. The scripting API

### 1.1 Facade and modules

```python
from wavesim_gui import api
sim = api.new_simulation(App.ActiveDocument)
api.create_material(sim, label="FR4", eps=4.4)
```

[`wavesim_gui/api.py`](../wavesim_gui/api.py) is the single import. It imports
every `api_*` module listed below and re-exports each module's `__all__`, plus
`API_VERSION`, `ApiError`, `describe`, `registered_functions` and
`select_subshapes` from `api_common`. If one of the listed modules is missing
from an installation, the facade skips it. Any other import error is raised.

| Module | Categories (registered functions) | Scope |
|---|---|---|
| [`api_common.py`](../wavesim_gui/api_common.py) | none | Conventions, registry, units, quantities, transactions, `describe`, selectors |
| [`api_document.py`](../wavesim_gui/api_document.py) | `document` (8) | Create, open, save, recompute, activate, list and close documents |
| [`api_simulation.py`](../wavesim_gui/api_simulation.py) | `simulation` (8), `material` (5), `setup` (5) | Simulation container, solver mode, run settings, domain, mesh, refinement, materials, generic setup edits, validation |
| [`api_geometry.py`](../wavesim_gui/api_geometry.py) | `geometry` (20) | Part/Draft primitives, booleans, transforms, arrays, fillets, STEP import, checks |
| [`api_excitation.py`](../wavesim_gui/api_excitation.py) | `excitation` (7), `electrostatics` (1), `port_matrix` (3) | Sources, ports, beams, waveforms, conductor potentials, port matrix |
| [`api_monitors.py`](../wavesim_gui/api_monitors.py) | `monitor` (8) | Probes, snapshots, energy/dissipation, voltage/current, field along curve |
| [`api_params.py`](../wavesim_gui/api_params.py) | `params` (7) | `Params` spreadsheet, expression bindings, model fingerprint |
| [`api_preview.py`](../wavesim_gui/api_preview.py) | `preview` (4) | Screenshots, visibility, cross-section, voxel slice image |
| [`api_run.py`](../wavesim_gui/api_run.py) | `run` (3) | Export a solver job, locate the runner, load results |

There are **79** registered functions in total.

**Bridge whitelist.** `@api_function(category, name=None)`
([`api_common.py`](../wavesim_gui/api_common.py)) records a function in the
module-level registry. Registering a second function under the same name
raises a `RuntimeError`. `registered_functions()` returns
`{name: (function, category)}`. Registration is the *only* way a function
becomes callable over the Agent bridge. Helpers that are not decorated can be
used from Python but cannot be reached through the bridge. One example is
`api_excitation.port_matrix_plan`. `API_VERSION = 1` is incremented whenever a
registered signature or result changes incompatibly. The MCP server refuses
to talk to a bridge that reports a different version.

### 1.2 Conventions (`api_common` module docstring)

* **First argument.** Setup functions take `sim` first. It can be the
  Simulation object, any object in the same document, a `FreeCAD.Document`, a
  document name or label, or `None` for the active document's simulation
  (`get_sim`). When the document has no simulation, the call raises
  `ApiError("… Create one with new_simulation first.")`. Geometry, document,
  params, preview and run functions take `doc` first, with the same leniency
  (`get_doc`). Pass every other argument by keyword. The bridge only sends
  keywords.
* **Object references.** Anywhere an object is expected, you can pass its
  internal `Name`, its `Label` (which must be unique) or the object itself
  (`find_object`). An ambiguous label raises `ApiError` and lists the internal
  names. An unknown reference raises `ApiError` with a "Did you mean …?" hint.
* **Quantities.** A dimensioned argument accepts any of these forms:
  * a plain number, read in the argument's default unit;
  * `"<number> <unit>"`, where the unit comes from the case-sensitive
    whitelist `UNITS`: `m mm um nm cm`, `Hz kHz MHz GHz THz mHz`
    (`mHz` is millihertz), `s ms us ns ps fs`, `S/m`, `ohm kohm Mohm mohm`,
    `F mF uF nF pF fF`, `H mH uH nH pH`, `V mV kV`, `A mA uA`, `deg rad`, `-`;
  * `{"value": v, "unit": u}`;
  * a FreeCAD `Quantity`;
  * a **param expression**: a string that starts with `=` and is written over
    `UPPER_SNAKE_CASE` parameter names, numbers, `+ - * / ( ) .`, for example
    `"=CYLINDER_DIAMETER/2"`. It is rewritten to `Params.CYLINDER_DIAMETER / 2`
    and bound as a FreeCAD expression (`setExpression`), so the property
    follows the parameter on every recompute.

  Writing a plain value clears any expression already bound to that property.
  Some arguments accept numbers only and raise
  `"… cannot be bound to a parameter here"` for an expression. Examples are
  selector values, explicit points (`vector_mm`) and preview offsets.
* **Storage units.** Values are stored in FreeCAD properties and in the
  `Params` sheet as:

  | dimension | unit | dimension | unit |
  |---|---|---|---|
  | length | mm | resistance | ohm |
  | frequency | Hz | capacitance | F |
  | time | s | inductance | H |
  | angle | deg | voltage | V |
  | conductivity | S/m | current | A |
  | dimensionless | `-` | | |

  The dimension of a plain `Float` property comes from `register_dimensions`.
  `MaxTime` is a time, `MaxFrequency` a frequency, `Eps`/`Mu` are
  dimensionless and `Sigma` is a conductivity. Excitation parameters are
  registered with row suffixes `2`…`8`, and modules register their own
  properties such as `Resistance`. Where nothing is registered, the dimension
  comes from the property type: `Length`/`Distance`/`Vector`/`Position` are
  lengths, `Angle` is an angle, `ElectricPotential` is a voltage, and other
  plain numbers are dimensionless.
* **Transactions.** Each document change runs inside
  `transaction(doc, "Wavesim: …")`, so one API call is one undo step. The
  transaction is aborted if the body raises. It is re-entrant per document,
  so an API function called from inside another one joins the outer
  transaction. Headless documents have `UndoMode == 0`, where aborting a
  transaction does not undo anything. For that reason the creating functions
  in excitation and geometry also remove the objects they created when they
  fail.
* **Errors.** Every failure the caller can fix raises `ApiError` with an
  actionable sentence. The bridge reports it as `error_type: "ApiError"`.
* **No dialogs, no selection.** No `api_*` function opens a dialog or reads
  the GUI selection. Wherever the GUI relies on a selection, the API takes
  object references and declarative **selectors** instead (see below). Only
  `screenshot` and `cross_section` require the GUI to be up, because they
  draw in the 3D view.
* **Selectors** (`select_subshapes(shape, selector, kind)`). A selector is a
  dict, and every key it contains must match (logical AND):
  * `{"type": "all"}`;
  * `"parallel_to": "x"|"y"|"z"`: straight edges along that axis, or planar
    faces whose normal lies along it;
  * `"normal": "+z"|"-x"|…` (faces only, signed outward normal);
  * `"at_x"/"at_y"/"at_z": length`: the sub-shape lies entirely in that
    coordinate plane;
  * `"near_point": [x, y, z]`: the single closest sub-shape;
  * `"tolerance"`: default 1e-4 mm.

  Unknown keys raise `ApiError`. Raw `Edge3`/`Face7` indices are never
  accepted as input.
* **Results.** Functions return FreeCAD objects or plain data. The bridge
  serializes an object with `describe(obj)`, which gives
  `{name, label, type, properties, expressions}`. Hidden properties and
  `Proxy`, `Shape`, `Placement` and similar are left out. `to_jsonable`
  converts the values: vectors become `[x, y, z]` in mm, a placement becomes
  `{base, axis, angle_deg}`, and a quantity becomes its `Value`. Long
  operations collect the console warnings (`capture_warnings`) and return
  them.

### 1.3 One creation path

GUI commands and API functions build objects through the **same module-level
helpers**, so an object created by the API cannot be distinguished from one
created by the GUI. The command keeps its transaction, its recompute and its
task panel. The API supplies the values the panel would have collected.

| GUI path | Shared helper(s) |
|---|---|
| `Wavesim_NewSimulation` | `api_simulation.new_simulation` itself (the command calls the API), which uses `domain.create_domain` and `materials.create_default_materials` |
| Simulation panel OK | `commands.apply_simulation_settings` |
| New/Edit Material panel OK | `materials.create_material` |
| Tree drag & drop onto a material | `materials.assign_body` |
| Refine Body panel | `refine.set_body_cell_counts` |
| Add source / beam / ports | `source.create_source`, `gaussian_beam.create_gaussian_beam`, `lumped_port.create_lumped_port`, `modal_port.create_modal_port`, `spice_port.create_spice_line_port` / `create_spice_tem_port`, plus `auto_label` and the forced-BC helpers (see [3.7](#37-excitation-sources-ports-potentials-port-matrix)) |
| Set Potential panel OK | `potentials.write_conditions` |
| Port Matrix run | `portmatrix.build_sweep`, `prepare_sweep_dir`, `export_drive_jobs` |
| Add monitors / drop a curve onto a monitor | `monitors.new_probe`, `new_snapshot`, `new_energy_monitor`, `new_dissipation_monitor`, `new_path_monitor`, `new_field_line_monitor`, `attach_path` |
| Run Simulation | `voxelize.build_job_from_document`, `job.write_job`, `voxelize.write_materials`, `results.build_results` |

The **Domain panel** has no shared writer. It writes the Domain properties
directly, live, while you edit. `set_domain` and `set_mesh` write the same
properties and re-derive the cell size with the same function the Simulation
panel uses (`domain.suggested_cell_size_m`).

---

## 2. Master table: GUI command → object → API

Every command registered with `Gui.addCommand` (24 ids), plus the panels that
open on a double-click and the GUI-only buttons. "VP" means view provider.

| GUI command id (menu text) | Object type / proxy / VP | API function(s) | Source |
|---|---|---|---|
| `Wavesim_NewSimulation` (New Simulation) | `App::DocumentObjectGroupPython` "Simulation" / `SimulationContainer` / `SimulationViewProvider`; it also creates the Domain, the Materials/Sources/Monitors groups and the Vacuum and PEC materials | `new_simulation`, `get_simulation` | [`commands.py`](../wavesim_gui/commands.py), [`api_simulation.py`](../wavesim_gui/api_simulation.py) |
| Simulation panel (double-click Simulation) | `TaskSimulationPanel` editing the Simulation | `set_run_settings`, `set_solver_mode`, `mesh_info` (time steps) | [`commands.py`](../wavesim_gui/commands.py) |
| Domain panel (double-click Domain) | `App::FeaturePython` "Domain" / `DomainObject` / `DomainViewProvider`; `TaskDomainPanel` | `set_domain`, `set_mesh`, `mesh_info` | [`domain.py`](../wavesim_gui/domain.py) |
| `Wavesim_AssignMaterial` (New Material) | `App::FeaturePython` "Material" / `MaterialObject` / `MaterialViewProvider`; `TaskMaterialPanel` (also opened by double-clicking a material) | `create_material`, `set_material`, `list_materials` | [`materials.py`](../wavesim_gui/materials.py) |
| Tree drag & drop of a body onto / off a material | `Material.Bodies` (LinkList); body tint; PEC body gets potential properties | `assign_material`, `unassign_material` | [`materials.py`](../wavesim_gui/materials.py) |
| `Wavesim_RefineBody` (Refine Body Mesh) | `WavesimCellsX/Y/Z` integer properties on the **body**; `TaskRefinePanel` | `refine_body` | [`refine.py`](../wavesim_gui/refine.py) |
| `Wavesim_AddSource` (Add Point Source) | "Source" / `SourceObject` / `SourceViewProvider` | `add_source`, `set_excitation` | [`source.py`](../wavesim_gui/source.py) |
| `Wavesim_AddModalPort` (Add Modal Port) | "ModalPort" / `ModalPortObject` / `ModalPortViewProvider` | `add_modal_port`, `set_excitation` | [`modal_port.py`](../wavesim_gui/modal_port.py) |
| `Wavesim_AddTEMSource` (legacy id of the same command) | same as `Wavesim_AddModalPort`; `tem_source.TEMSourceObject` is kept as an unpickling alias | `add_modal_port` | [`modal_port.py`](../wavesim_gui/modal_port.py), [`tem_source.py`](../wavesim_gui/tem_source.py) |
| `Wavesim_AddGaussianBeam` (Add Gaussian Beam) | "GaussianBeam" / `GaussianBeamObject` / `GaussianBeamViewProvider` | `add_gaussian_beam`, `set_excitation` | [`gaussian_beam.py`](../wavesim_gui/gaussian_beam.py) |
| `Wavesim_AddLumpedPort` (Add Lumped Port) | "LumpedPort" / `LumpedPortObject` / `LumpedPortViewProvider` | `add_lumped_port`, `set_excitation` | [`lumped_port.py`](../wavesim_gui/lumped_port.py) |
| `Wavesim_AddSpiceLinePort` (Add SPICE Line Port) | "SpiceLinePort" / `SpiceLinePortObject` / `SpiceLinePortViewProvider` | `add_spice_line_port` | [`spice_port.py`](../wavesim_gui/spice_port.py) |
| `Wavesim_AddSpiceTEMPort` (Add SPICE TEM Port) | "SpiceTEMPort" / `SpiceTEMPortObject` / `SpiceTEMPortViewProvider` (legacy) | `add_spice_tem_port` (new work: `add_modal_port(drive_mode="spice")`) | [`spice_port.py`](../wavesim_gui/spice_port.py) |
| `Wavesim_SetPotential` (Set Potential) | no object: `WavesimConductorMode` / `WavesimPotential` / `WavesimFloatingCharge` on PEC bodies, `Simulation.UnsetConductorMode` | `set_potential` | [`potentials.py`](../wavesim_gui/potentials.py) |
| `Wavesim_AddProbe` (Add Probe) | "Probe" / `ProbeObject` / `ProbeViewProvider` | `add_probe` | [`monitors.py`](../wavesim_gui/monitors.py) |
| `Wavesim_AddSnapshot` (Add Snapshot) | "Snapshot" / `SnapshotObject` / `SnapshotViewProvider` | `add_snapshot` | [`monitors.py`](../wavesim_gui/monitors.py) |
| `Wavesim_AddEnergyMonitor` (Add Energy Monitor) | "EnergyMonitor" / `EnergyObject` / `EnergyViewProvider` | `add_energy_monitor` | [`monitors.py`](../wavesim_gui/monitors.py) |
| `Wavesim_AddDissipationMonitor` (Add Dissipation Monitor) | "DissipationMonitor" / `DissipationObject` / `DissipationViewProvider` | `add_dissipation_monitor` | [`monitors.py`](../wavesim_gui/monitors.py) |
| `Wavesim_AddVoltageMonitor` (Add Voltage Monitor) + curve drop | "VoltageMonitor" / `PathMonitorObject` / `PathMonitorViewProvider` | `add_voltage_monitor`, `set_monitor_path` | [`monitors.py`](../wavesim_gui/monitors.py) |
| `Wavesim_AddCurrentMonitor` (Add Current Monitor) + curve drop | "CurrentMonitor" / `PathMonitorObject` / `PathMonitorViewProvider` | `add_current_monitor`, `set_monitor_path` | [`monitors.py`](../wavesim_gui/monitors.py) |
| `Wavesim_AddFieldLineMonitor` (Add Field Along Curve) + curve drop | "FieldLineMonitor" / `FieldLineObject` / `FieldLineViewProvider` | `add_field_line_monitor`, `set_monitor_path` | [`monitors.py`](../wavesim_gui/monitors.py) |
| `Wavesim_Run` (Run Simulation) | voxelize → `job.json` + `materials.npz` → out-of-process `runner.py` → Results group | `export_job` + external `runner.py` (`runner_path`) + `load_results` | [`commands.py`](../wavesim_gui/commands.py), [`run.py`](../wavesim_gui/run.py), [`job.py`](../wavesim_gui/job.py), [`api_run.py`](../wavesim_gui/api_run.py) |
| `Wavesim_PortMatrix` (Port Matrix (Z / Y)) | modal picker → N runs → Port Matrix results group | `configure_port_matrix`, `export_port_matrix_jobs`, `load_port_matrix_results` | [`portmatrix.py`](../wavesim_gui/portmatrix.py) |
| `Wavesim_CrossSectionToggle` (Cross Section On/Off) | state on the Domain (`WavesimXSec*`, `GridPlaneX/Y/Z`) | `cross_section(on=…, plane=…, offset=…, flip=…, style=…)` | [`crosssection.py`](../wavesim_gui/crosssection.py) |
| `Wavesim_CrossSectionGrid` (Mesh Grid On/Off) | same state (`WavesimXSecGrid`) | `cross_section(grid=True/False)` | [`crosssection.py`](../wavesim_gui/crosssection.py) |
| `Wavesim_Settings` (Settings...) | `wavesim_settings.json` (no document object) | **not exposed**; `export_job(backend=, wavesim_path=)` override two values per job | [`wavesim_settings.py`](../wavesim_settings.py) |
| `Wavesim_AgentBridge` (Agent Bridge On/Off), new | XML-RPC listener; nothing in the document | **not exposed** (it is the transport, see [§4](#4-agent-bridge)) | [`bridge.py`](../wavesim_gui/bridge.py) |
| Modal-port / SPICE-TEM panel **Compute Mode** button | out-of-process mode solve (`modal_port.run_mode_solve`), modes plotted without touching the document | **not exposed** (`voxel_preview` is a different thing: an image of a voxelized material slice) | [`modal_port.py`](../wavesim_gui/modal_port.py), [`spice_port.py`](../wavesim_gui/spice_port.py) |
| Property editor edits, tree *Delete* on setup objects | any setup object | `get_object_props`, `set_object_props`, `delete_setup_object`, `list_setup`, `validate_setup` | [`api_simulation.py`](../wavesim_gui/api_simulation.py) |
| f(x) expression editor, Spreadsheet workbench | `Params` sheet, `ExpressionEngine` | `sync_params_to_spreadsheet`, `read_params_sheet`, `bind_param`, `unbind_param`, `list_bindings`, `property_dimension_of`, `model_fingerprint` | [`api_params.py`](../wavesim_gui/api_params.py) |
| Part / Draft workbench modelling (no Wavesim command) | `Part::Box`, `Part::Cylinder`, …, Draft arrays | `create_*`, `boolean`, `transform`, `mirror`, `*_array`, `fillet`, `chamfer`, `import_step`, `measure`, `check_geometry`, `delete_object`, `rename_object`, `list_objects` | [`api_geometry.py`](../wavesim_gui/api_geometry.py) |
| FreeCAD view menu: standard views, Save picture, Space bar / eye | 3D view | `screenshot`, `set_visibility` | [`api_preview.py`](../wavesim_gui/api_preview.py) |
| FreeCAD File menu | documents | `new_document`, `open_document`, `save_document`, `close_document`, `recompute`, `list_documents`, `active_document`, `set_active_document` | [`api_document.py`](../wavesim_gui/api_document.py) |
| Double-click a result node to plot it | result viewers | **not exposed** (the agent reads `results.npz` outside FreeCAD) | [`results.py`](../wavesim_gui/results.py) |

Command availability (`IsActive`) is enforced by the API in the same way:

* `Wavesim_NewSimulation` is enabled only while the document has no
  simulation. `new_simulation` raises `ApiError` if one exists.
* Every other command needs a simulation. The API raises `ApiError` when
  `get_sim` finds none.
* Sources, ports, beams and the port matrix are full-wave only
  (`commands.fdtd_only_active`).
* `Wavesim_SetPotential` and `Wavesim_AddFieldLineMonitor` work in
  electrostatic mode only.

---

## 3. Per-area details

### 3.1 Simulation container, solver mode, run settings

**Object.** `App::DocumentObjectGroupPython`, internal name `Simulation`. Proxy
`commands.SimulationContainer`, VP `commands.SimulationViewProvider` (tree icon
`simulation.svg`; double-click/`setEdit` opens `TaskSimulationPanel`). It is
identified by `WavesimType = "Simulation"`, a read-only string property, so
`active_simulation(doc)` finds it even before the proxy is restored. There is
one per document. It contains, in this order: `Domain`, then the
`App::DocumentObjectGroup`s `Materials`, `Sources`, `Monitors`
(`commands._CHILD_GROUPS`). Runs add result groups later.

| Property (group) | Type | Default | Notes |
|---|---|---|---|
| `WavesimType` (Wavesim) | String, read-only | `Simulation` | identity marker |
| `TimeUnit` (Units) | Enumeration `s ms us ns ps fs` | `ns` | display only |
| `FrequencyUnit` (Units) | Enumeration `Hz kHz MHz GHz THz` | `GHz` | display only |
| `MaxTime` (Run) | Float, **seconds** | 2e-9 | simulated time span; FDTD steps = MaxTime / dt; expressions allowed (evaluated in s) |
| `MaxFrequency` (Run) | Float, **Hz** | 1e9 | drives the default cell size `c / (fmax · CellsPerWavelength · n)`; expressions allowed |
| `SubpixelSmoothing` (Run) | Bool | False | anisotropic dielectric interface averaging; PEC unaffected |
| `ConformalPEC` (Run) | Bool | True for a new simulation, False for a document that predates the property | Dey-Mittra cut cells |
| `ConformalAreaThreshold` (Run) | Float | 0.4 (`CONFORMAL_AREA_THRESHOLD`) | starting clamp fraction, 0 ≤ x < 1; **no panel row** (property editor only) |
| `SolverMode` (Run) | Enumeration `Full wave (FDTD)` \| `Electrostatic` | `Full wave (FDTD)` | job token `fdtd` / `electrostatic` |
| `ExtractCapacitance` (Run) | Bool | True | electrostatic: one extra solve per conductor |
| `UnsetConductorMode` (Run) | Enumeration `Grounded (0 V)` \| `Floating (0 C)` | `Grounded (0 V)` | electrostatic: PEC bodies with no condition |

#### `Wavesim_NewSimulation` → `new_simulation(doc=None, label="Simulation", mode="fdtd")`

* **GUI.** If no document is open, the command creates one. If the document
  already has a simulation, it prints a warning and returns. Otherwise it
  calls `api_simulation.new_simulation(doc)`, which is the only creation path.
* **API.** The function does not create a document: call `new_document`
  first. It raises `ApiError` when a simulation already exists. In the
  transaction `Wavesim: New Simulation` it does the following:
  1. creates the container, its VP when a GUI is up, and
     `domain.create_domain(doc, sim)`;
  2. creates the three child groups;
  3. calls `materials.create_default_materials`, which creates **Vacuum**
     (eps 1, mu 1, colour (0.75, 0.90, 1.00)) and **PEC** (`Pec=True`,
     colour (0.78, 0.78, 0.82));
  4. sets `Domain.Background = Vacuum`;
  5. for a non-FDTD `mode`, calls
     `apply_simulation_settings(solver_mode_label=…)`.

  After the transaction it recomputes. Mode aliases: `fdtd`, `full wave`,
  `full-wave` / `electrostatic`, `es`, `electrostatics`.
* `get_simulation(sim=None)` returns the container, or raises.

#### Simulation panel (double-click Simulation) → `set_run_settings`, `set_solver_mode`

Panel rows:

* Solve (`SolverMode`)
* Time unit
* Frequency unit
* Max simulation time (spin box 0…1e12 in the display unit)
* Max frequency (spin box 0…1e12)
* Time steps (read-only, `domain.time_steps_for`)
* *Extract the capacitance matrix* (electrostatic only)
* *Subpixel smoothing*
* *Conformal (cut-cell) PEC*

In electrostatic mode the time and frequency rows are hidden. When `MaxTime` or
`MaxFrequency` is driven by an expression, its row is locked and shows the
expression (`expressions.lock_bound_widget`). OK opens the transaction
`Wavesim: Edit Simulation` and calls `commands.apply_simulation_settings(...)`,
followed by a recompute.

`apply_simulation_settings` is the single write path that both the panel and
the API use:

* `None` leaves a setting unchanged.
* A property driven by an expression is never written.
* When the max frequency changes by more than 1e-6 Hz, or the solver mode
  changes, it re-derives `Domain.Dx = Dy = Dz` from
  `domain.suggested_cell_size_m`.
* A mode change also touches the Domain, which then rebuilds its absorber
  shell.
* It removes the retired mode-convergence properties.
* It returns `(freq_changed, mode_changed)`.

API:

* `set_run_settings(sim=None, max_time=None, max_frequency=None, time_unit=None, frequency_unit=None, subpixel=None, conformal_pec=None, conformal_area_threshold=None)`
  runs in the transaction `Wavesim: Edit Simulation`.
  * `max_time` (s) and `max_frequency` (Hz) must be > 0 when given as plain
    values.
  * `"=PARAM"` binds an expression. A plain value clears an existing
    expression first.
  * Units are matched case-insensitively against the enumerations.
  * `conformal_area_threshold` must be in [0, 1). It is written after the
    shared helper runs, because the panel has no row for it.
  * The previous frequency is passed in, so binding a frequency expression
    re-derives the cell size just like a typed value does.
* `set_solver_mode(sim=None, mode="fdtd", extract_capacitance=None, unset_conductors=None)`
  runs in the transaction `Wavesim: Edit Simulation`. `unset_conductors` takes
  `grounded`/`ground`/`0v` or `floating`/`float`/`0c` and sets
  `UnsetConductorMode`. The GUI edits that property in the Set Potential panel
  (`set_potential(unset_default=…)` does the same).
* The panel's *Time steps* label → `mesh_info(sim)["steps"]`.

### 3.2 Domain and mesh

**Object.** `App::FeaturePython` "Domain", label `Domain`. Proxy
`domain.DomainObject`, VP `domain.DomainViewProvider`: a wireframe domain box,
a PML-interior box and cell-grid planes; double-click opens `TaskDomainPanel`.
`WavesimType = "Domain"`. It is created only by `domain.create_domain(doc, sim)`
(called from `new_simulation`). It is a singleton, and `delete_setup_object`
refuses to delete it.

How the box is sized:

* `execute` sizes the box to every material-assigned body plus the source
  positions and snapshot planes, and adds the per-face spacing.
* **The PML is inside the box**: the outer `PMLThickness` cells of each
  absorbing face.
* At export, the faces that carry a modal port, Gaussian beam or SPICE-TEM
  port are forced by `domain_grid_params`:
  * a modal-port face gets no PML, no PEC and no gap;
  * a beam or SPICE-TEM face is forced to PML.

  The panel shows those faces locked, labelled `Modal port` or
  `Port / beam (absorbing)`.

| Property (group) | Type | Default | Notes |
|---|---|---|---|
| `Dx`, `Dy`, `Dz` (Grid) | Length | 1 mm | cell size; with the non-uniform grid: the coarse (background) target |
| `Nx`, `Ny`, `Nz` (Grid) | Integer, read-only | derived | cell counts |
| `CellsPerWavelength` (Grid) | Integer | 20 | full-wave resolution rule |
| `CellsPerFeature` (Grid) | Integer | 10 | electrostatic: cells across the thinnest assigned feature |
| `UseNonuniformGrid` (Grid) | Bool | True | graded snapped grid (`gridbuild`) |
| `MaxGradingRatio` (Grid) | Float | 1.5 | neighbour cell-size ratio |
| `MinCellSize` (Grid) | Length | 0 mm (= no limit) | snapper floor; also clips body refinement |
| `CurvatureRefinement` (Grid) | Float | 1.0 (= off) | refinement where curved faces graze a grid plane |
| `NodesX/Y/Z` (Grid) | FloatList, hidden | derived | node coordinates (world mm) |
| `SnapsX/Y/Z` (Grid) | FloatList, hidden | derived | snapped lines (view data only) |
| `GridPlaneX/Y/Z` (Grid) | Distance | placed once on the min faces | cell-grid plane positions = cross-section position |
| `ShowGridPlaneX/Y/Z` (Grid) | Bool | False | grid plane drawn (normally set by the cross-section grid button) |
| `GridPlanesPlaced` (Grid) | Bool, hidden | False | bookkeeping |
| `SpacingXMin`…`SpacingZMax` (Domain) | Length | 0 mm | background gap per face (legacy scalar `Spacing` is migrated on restore) |
| `Background` (Domain) | Link | Vacuum (set by `new_simulation`); None = vacuum | material filling empty voxels |
| `PMLThickness` (Boundary) | Integer | 8 | PML depth in cells |
| `BoundaryXMin`…`BoundaryZMax` (Boundary) | Enumeration `PML` \| `PEC` | PML | full-wave BC per face |
| `ESBoundaryXMin`…`ESBoundaryZMax` (Boundary) | Enumeration `Ground` \| `Symmetry` | Ground | electrostatic BC: Ground = φ 0 (Dirichlet), Symmetry = no normal field (Neumann) |
| `DomainMin/Max`, `PmlMin/Max` (Box) | Vector, hidden | derived | whole grid / PML-free interior (mm) |
| `TrackedBodies` (Box) | LinkList, hidden | derived | bodies the box follows |
| `WavesimXSecAxis/Flip/Grid/On` (Grid) | Integer −1 / Bool, hidden | added by the cross-section tool | cut state |
| `WavesimXSecStyle` (Grid) | Enumeration `Shaded` \| `Hatched` \| `Plain` | Shaded | cap style (property editor only) |

**Domain panel** (`TaskDomainPanel`). Rows:

* cell size `Dx/Dy/Dz` (1e-6…1e6 mm, *cubic* tick box; disabled while the
  non-uniform grid is on);
* cells per wavelength (1…1000), or cells per feature in electrostatic mode
  (1…1000);
* a *Default from max frequency* / *Default from geometry* button;
* *non-uniform* tick box;
* max grading (1.0…5.0);
* min cell size (0…1e6 mm);
* curvature refinement (1…64);
* six spacings (0…1e6 mm, *same on all faces*);
* PML thickness (1…100);
* background combo (`Vacuum (eps=1, mu=1)` = None, then every material);
* six BC combos (full wave: PML/PEC; electrostatic: Ground/Symmetry; *same on
  all faces*; port faces locked);
* cell-count label and PML-shell notice.

Every edit is applied live and recomputed. Changing a resolution rule, the grid
type or the background re-derives the cell size. Cancel restores the values
captured when the panel opened. OK writes everything once more in the
transaction `Wavesim: Edit Domain`.

API (one transaction `Wavesim: Edit Domain` per call):

* `set_domain(sim=None, spacing=None, boundaries=None, pml_cells=None, background=None, es_boundaries=None)`
  * Faces are `x0 x1 y0 y1 z0 z1`. Aliases `xmin/xmax`, `-x/+x`, … are
    accepted.
  * A scalar applies to all six faces. A dict sets only the faces it names
    (this replaces the panel's *same on all faces* tick box).
  * `spacing` must be ≥ 0 (mm, expressions allowed).
  * `boundaries` takes `PML`/`PEC`; `es_boundaries` takes `Ground`/`Symmetry`.
    Both are case-insensitive.
  * `pml_cells` must be an integer in **1…100** (as in the panel).
  * `background` must be a Wavesim material. The panel's *Vacuum* entry
    (`None`) is not available here: use the Vacuum material. Setting a
    background clears expressions on `Dx/Dy/Dz` and re-derives the cell size.
  * The stored BC of a port-locked face is still written. The job builder
    overrides it at export.
  * Returns the Domain.
* `set_mesh(sim=None, cell_size=None, cells_per_wavelength=None, cells_per_feature=None, nonuniform=None, max_grading=None, min_cell_size=None, curvature_refinement=None)`
  * Validation (the panel's ranges): `cells_per_wavelength` and
    `cells_per_feature` are integers in 1…1000; `max_grading` is in
    **1.0…5.0**; `min_cell_size` ≥ 0; `curvature_refinement` in 1…64.
  * `cell_size` takes `"auto"`, one length (cubic) or `[dx, dy, dz]`, each
    > 0. Expressions are allowed. `"auto"` is the panel's *Default from …*
    button.
  * Like the panel, the cell size is re-derived when a resolution rule or the
    grid type changes and no `cell_size` is given.
  * When no size can be derived, it raises `ApiError`. That happens with no
    max frequency in full wave, or no assigned body in electrostatics.
  * Returns `mesh_info`.
* `mesh_info(sim=None)` recomputes and returns `solver_mode`, `cells`,
  `total_cells`, `cell_size_mm`, `min_spacing_mm`, `nonuniform`,
  `domain_min_mm`, `domain_max_mm`, `interior_min_mm`, `interior_max_mm`
  (inside the PML), `max_time_s`, `max_frequency_hz`, `dt_s` and `steps`.
  `dt_s` and `steps` are `None` in electrostatic mode. This replaces the
  panel's count label and the Simulation panel's *Time steps*. It does not
  voxelize.
* The grid-plane positions and visibility are ordinary properties:
  `cross_section` or `set_object_props(obj="Domain", …)`.

### 3.3 Materials

**Object.** `App::FeaturePython` "Material" in the `Materials` group. Proxy
`materials.MaterialObject`, VP `materials.MaterialViewProvider`. The VP is a
`LinkedVisibilityMixin`: the material's eye shows and hides its bodies. The VP
also handles drag & drop, and a double-click opens `TaskMaterialPanel`.
`WavesimType = "Material"`.

| Property (group Material) | Type | Default | Notes |
|---|---|---|---|
| `Eps` | Float | 1.0 | relative permittivity |
| `Mu` | Float | 1.0 | relative permeability |
| `Sigma` | Float, S/m | 0.0 | lossy dielectric (back-filled on restore); ignored for PEC |
| `Pec` | Bool | False | perfect electric conductor (overrides Eps/Mu/Sigma) |
| `Color` | Color | (0.30, 0.60, 0.90) | tint applied to every assigned body |
| `Bodies` | LinkList | empty | assigned solids; a body belongs to one material |

A body assigned to a PEC material gains the following properties (group
Wavesim, `materials.ensure_potential_prop`):

* `WavesimPotential` (Float, V, 0)
* `WavesimFloatingCharge` (Float, C, 0)
* `WavesimConductorMode` (Enumeration `Default`|`Potential`|`Floating`)

They are edited with `set_potential` (see
[3.7](#37-excitation-sources-ports-potentials-port-matrix)).

**`Wavesim_AssignMaterial` ("New Material").** `IsActive` requires a
simulation. The command opens `TaskMaterialPanel` with no object. **The
material is created only on OK**, so Cancel leaves nothing behind.

Panel fields:

* Name
* Colour button
* *PEC* tick box
* Eps (1…1e6)
* Mu (1e-6…1e6)
* Sigma (0…1e6 S/m)

On OK, inside the transaction `Wavesim: New Material`:

1. `materials.create_material(doc, sim, "Material")` creates the object.
2. It writes `Pec`, then `Eps`/`Mu`/`Sigma` (each skipped when an expression
   drives it), then `Color`.
3. It sets the label from the Name field. An empty name gives
   `Material (<eps=… | PEC>)`.
4. It calls `apply_material_color`.
5. For a PEC material it adds the potential properties to the bodies.

After the transaction: a recompute and `domain.notify_materials_changed`.
Double-clicking an existing material opens the same panel, with the transaction
`Wavesim: Edit Material`.

**Tree drag & drop.**

* `canDropObject` accepts only solids.
* `dropObject` calls `assign_body(mat, body)`, then
  `visibility.sync_from_children`, then `notify_materials_changed`. Assigning
  a body resizes the domain. In electrostatic mode it also re-derives the cell
  size from the thinnest feature and refreshes the snapshot planes.
* Dragging a body off a material removes it from `Bodies`, restores its grey
  colour (0.8, 0.8, 0.8) and notifies the domain.
* `assign_body` detaches the body from every other material, appends it, adds
  the potential properties for PEC, and tints the body.

API:

* `create_material(sim=None, label=None, eps=1.0, mu=1.0, sigma=0.0, pec=False, color=None, bodies=None)`
  runs in the transaction `Wavesim: New Material`.
  * `label` is **required** and must be unique (the GUI makes one up).
  * `eps` > 0 and `mu` > 0. This is looser than the panel's eps ≥ 1.
  * `sigma` ≥ 0 S/m (`"5.8e7 S/m"`). `eps`, `mu` and `sigma` all accept
    `"=PARAM"`.
  * `color` is `[r, g, b]` with values 0…1.
  * `bodies` are assigned inside the same transaction.
* `set_material(sim=None, material=None, label=None, eps=None, mu=None, sigma=None, pec=None, color=None)`
  is the edit panel, in the transaction `Wavesim: Edit Material`. It recolours
  the bodies and adds the potential properties when `pec` is turned on, then
  calls `notify_materials_changed`.
* `assign_material(sim=None, bodies=None, material=None)` is the drop.
  * Bodies are references, one or a list. Each must have a solid shape, and
    the target must be a Wavesim material.
  * Runs in the transaction `Wavesim: Assign Material`.
  * Calls `assign_body`, the visibility sync and `notify_materials_changed`.
* `unassign_material(sim=None, bodies=None)` is the drag-off, in the
  transaction `Wavesim: Unassign Material`. It returns the labels.
* `list_materials(sim=None)` returns
  `[{name, label, eps, mu, sigma_S_per_m, pec, bodies}]`.
* Deleting a material: `delete_setup_object`. It detaches the bodies, restores
  their colour, and refuses to delete the domain background.

### 3.4 Body mesh refinement

**`Wavesim_RefineBody` ("Refine Body Mesh").** `IsActive` requires a
simulation **and** a selection that contains solid bodies
(`refine.selected_bodies`; a face picked in 3D counts). If any selected body
is not assigned to a material, a message box refuses the whole selection.
`TaskRefinePanel` shows one spin box per axis (0…100000). It previews live
through `set_body_cell_counts` + `notify_domain_inputs_changed`, and prints the
resulting minimum cell and dt. OK commits in the transaction
`Wavesim: Refine Body Mesh`. Cancel restores the previous counts.

There is no Wavesim object for a refinement. Three properties are stored **on
the body** (group Wavesim, `refine.CELL_COUNT_PROPS`):

| Property | Type | Default | Meaning |
|---|---|---|---|
| `WavesimCellsX/Y/Z` | Integer | 0 (= no refinement on that axis) | minimum cells across the body's bounding box |

The count is a floor: interior forced lines can push the real count higher,
and the Domain's `MinCellSize` can clip it. It applies only with the
non-uniform grid on. Bodies that carry no properties produce an unchanged
mesh.

API: `refine_body(sim=None, body=None, cells=None)`

* Takes one body per call. A multi-selection in the GUI becomes a loop.
* `cells` is `[nx, ny, nz]` or a single integer, each an integer ≥ 0.
  `[0, 0, 0]` clears the request.
* The body must be assigned to a material.
* Runs in the transaction `Wavesim: Refine Body Mesh`, then
  `notify_domain_inputs_changed`.
* Returns `{body, cells, measured_cells, warnings}`.
  `measured_cells` comes from `refine.measured_cell_counts`. `warnings` says
  so when the non-uniform grid is off.

`list_setup()["refined_bodies"]` lists every request.

### 3.5 Setup inspection, generic edits, validation

These functions replace the property editor, tree deletion and a pre-run
check. None of them corresponds to a command.

* `list_setup(sim=None)` returns:
  * `simulation`: label, `solver_mode`, `max_time_s`, `max_frequency_hz`,
    `subpixel`, `conformal_pec`, `extract_capacitance`, `unset_conductors`,
    `expressions`;
  * `mesh` (= `mesh_info`);
  * `domain` (`describe`);
  * `materials` (= `list_materials`);
  * `sources` and `monitors` (`describe` of every object in those groups);
  * `refined_bodies`;
  * `results` (labels).
* `get_object_props(sim=None, obj=None)` returns `describe(obj)` plus
  `dimensions` `{prop: dimension}` and `storage_units`.
* `set_object_props(sim=None, obj=None, props=None)` runs in the transaction
  `Wavesim: Edit <label>`.
  * The target must be the Simulation itself or an object inside its tree.
    Geometry is refused.
  * Unknown properties raise `ApiError`, which lists the settable ones.
  * Refused properties: `WavesimType`, `Type`, `Proxy`, `ExpressionEngine`,
    `Label`, `Label2`, `Group`, `Placement`, `Shape`, and any read-only or
    hidden property.
  * Accepted values by property type:

    | Property type | Value |
    |---|---|
    | Enumeration | any listed choice, case-insensitive |
    | Bool | strict `true`/`false` |
    | String | text |
    | Link | reference or `null` |
    | LinkList | list of references |
    | Vector / VectorDistance / Position | `[x, y, z]` quantities |
    | Color | `[r, g, b]` |
    | any other | a quantity with the property's dimension, otherwise refused |

  * Afterwards it recomputes. For a material it also calls
    `apply_material_color` + `notify_materials_changed`; for anything else,
    `notify_domain_inputs_changed`.
  * It does **not** re-title auto labels or run panel-specific side effects
    (forced BCs, visibility sync, conductor scans). Use the dedicated function
    when one exists (`set_excitation`, `set_domain`, `set_material`, …).
* `delete_setup_object(sim=None, obj=None)` is the tree *Delete*, in the
  transaction `Wavesim: Delete <label>`.
  * It refuses the Simulation, the Domain, the groups, and the background
    material.
  * For a material it first detaches the bodies and restores their colour.
  * It then calls the matching domain notification.
* `validate_setup(sim=None, max_cells=None, throughput=None)` checks the setup
  without voxelizing and returns
  `{ok, errors, warnings, estimates: {cells, total_cells, memory_MB, dt_s, steps, runtime_s, runtime_note}}`.
  The GUI has no equivalent: its checks happen during *Run*.
  * Errors:
    * no assigned body, or an invalid body shape;
    * zero cells, or more than `max_cells`;
    * a source or monitor `Position` outside the domain;
    * electrostatic mode: no PEC body;
    * full wave: `MaxTime` ≤ 0, max frequency ≤ 0, or no source/port.
  * Warnings:
    * a visible solid with no material;
    * assigned bodies that overlap (the first 80 bodies are checked);
    * a position inside the PML (full wave);
    * electrostatic mode: no potential set;
    * full wave: no monitor, `materials.loss_warnings`,
      `lumped_port.colocation_warnings`;
    * `domain.shell_geometry_warnings`.
  * Estimates: 160 B/cell for full wave and 96 B/cell for electrostatics;
    runtime = cells × steps / throughput, with a default throughput of 1e8
    cell-updates/s.

### 3.6 Parameters: `Params` spreadsheet, bindings, fingerprint

No Wavesim command covers this area. By hand, you would use FreeCAD's f(x)
expression editor and the Spreadsheet workbench. The project's `PARAMS.md`
(outside FreeCAD) is the source of truth. The MCP server parses it and pushes
the resolved values into the sheet.

Sheet: `Spreadsheet::Sheet`, internal name and label `Params`. Row 1 holds the
headers `Name | Value | Unit | Description`. Each parameter row contains:

| Column | Content |
|---|---|
| A | name |
| B | value, with alias `NAME`, in the **storage unit** |
| C | unit |
| D | description |
| E | the last value synced from the file (column narrowed to width 1, used for drift detection) |

Names must be `UPPER_SNAKE_CASE`.

* `sync_params_to_spreadsheet(doc=None, params=None, remove_missing=True)`
  runs in the transaction `Wavesim: Sync Params`.
  * `params` is `{NAME: {"value": number | "text", "unit", "description"}}`.
  * The file wins.
  * A sheet value that differs from what the last sync wrote is reported as
    `drift`.
  * Rows whose name is missing from `params` are removed only when nothing is
    bound to them. Otherwise they are reported in `kept_missing_bound`.
  * Returns `{created, updated, drift, removed, kept_missing_bound,
    unknown_references, unused, recompute_errors}`.
* `read_params_sheet(doc=None)` returns `{NAME: value}`, or `{}` when there is
  no sheet.
* `list_bindings(doc=None)` returns
  `[{object, label, property, expression, params}]` for every expression that
  references `Params.<NAME>`.
* `property_dimension_of(doc=None, obj=None, prop=None)` returns
  `{object, property, dimension, storage_units}`. Use it to check a binding
  before making it.
* `bind_param(doc=None, obj=None, prop=None, expression=None)` runs in the
  transaction `Wavesim: Bind <label>.<prop>`.
  * It works on any object: geometry or setup.
  * `prop` can address a component: `Position.x`, `Placement.Base.z`,
    `Placement.Rotation.Angle`.
  * It refuses `Label`, `WavesimType`, `Type` and `Proxy`.
  * It recomputes and returns `{object, property, expression, value}`.
* `unbind_param(doc=None, obj=None, prop=None)` removes the expression. The
  current value stays.
* `model_fingerprint(doc=None)` returns
  `{model_hash, setup_hash, workbench_version, api_version, bodies, setup_objects}`.
  It does not voxelize.
  * `model_hash` covers every material-assigned body: topology counts,
    rounded vertices, volume, area, bounding box and face types, plus the
    material and the body's `Wavesim*` properties (cross-section state
    excluded).
  * `setup_hash` covers the Simulation's settings and every object in its
    tree. Result groups (`Results`, `Result`, `ResultPort`, `ResultMatrix`)
    are excluded, and so are derived or view-only properties: nodes, snaps,
    box corners, `Nx/Ny/Nz`, grid planes, display units, `Color`.
  * Floats are rounded to 10 significant digits, so the hashes are stable
    across save and reopen.

### 3.7 Excitation: sources, ports, potentials, port matrix

Source: [`api_excitation.py`](../wavesim_gui/api_excitation.py) (categories `excitation`, `electrostatics`, `port_matrix`): sources, ports,
beams, electrostatic potentials and the port matrix.

#### Common rules

- Signature `fn(sim=None, *, ...)`; `sim` = Simulation / Document / doc name /
  None (active document's simulation).
- Every `add_*` = the command's `Activated()` **plus OK in its task panel**:
  the object is created by the *same module-level helper* the command now
  calls (`source.create_source`, `gaussian_beam.create_gaussian_beam`,
  `lumped_port.create_lumped_port`, `modal_port.create_modal_port`,
  `spice_port.create_spice_line_port` / `create_spice_tem_port`), the settings
  are written, then the panel's commit side effects follow:
  `labels.retitle(old_auto, new_auto)` (or an explicit unique `label`),
  forced boundary conditions, `doc.recompute()` and
  `domain.notify_domain_inputs_changed(doc)`.
- One undo step per call (`Wavesim: Add …`). On any error the transaction is
  aborted **and** the objects the call created are removed (headless documents
  have `UndoMode == 0`, where an abort alone undoes nothing).
- Full-wave only: every `add_*` and `configure_port_matrix` /
  `export_port_matrix_jobs` raise `ApiError` in an electrostatic simulation
  (the commands' `IsActive` is `commands.fdtd_only_active`). `set_potential`
  is the reverse: electrostatic only (`CommandSetPotential.IsActive`).
- Quantities: number in the default unit (mm, Hz, s, deg, ohm, H, F, V) |
  `"2.4 GHz"` | `{"value", "unit"}` | `"=PARAM_EXPR"` (bound as an expression).
  Range checks apply to plain numbers only.
- All created objects go to the Simulation's **Sources** group.

##### Excitation dict (all driven objects)

`{"type": ..., <params>}` → the shared excitation property set
(`excitation.ensure_object_props`; row *i* > 0 of a modal port uses suffixed
names `Excitation2`, `Fmax2`, …).

| type (aliases) | `Excitation` enum label | params used |
|---|---|---|
| `gaussian` (`gauss`, `gaussian_pulse`) | Gaussian Pulse | fmax, amplitude |
| `sine` (`sin`, `sine_wave`) | Sine Wave | frequency, amplitude, phase |
| `sinusoid` (`cw`, `ramped_sine`) | Sinusoid (ramped CW) | frequency, amplitude, phase, ramp_cycles |
| `rect` (`rectangular`, `rectangle`, `rectangular_pulse`) | Rectangular Pulse | amplitude, start_time, rise_time, flat_time, fall_time |
| `gaussian_sine` (`gaussian+sine`, `modulated_gaussian`) | Gaussian + Sine | frequency, fmax, amplitude, phase |

The GUI labels are accepted as `type` too. Omitting `type` keeps the current
family (`set_excitation`) or uses Gaussian (creation).

| param | property | unit | default | plain-number range (panel spin box) |
|---|---|---|---|---|
| `fmax` (`bandwidth`) | `Fmax` (Float, SI) | Hz | 30 GHz | > 0 |
| `frequency` | `Frequency` | Hz | 30 GHz | > 0 |
| `amplitude` | `Amplitude` | — | 1 | ±1e9 |
| `phase` (`phase_deg`) | `PhaseDeg` | deg | 0 | ±360 |
| `ramp_cycles` | `RampCycles` | periods | 3 | 0 … 1e6 |
| `start_time` / `rise_time` / `flat_time` / `fall_time` | `StartTime` / `RiseTime` / `FlatTime` / `FallTime` | s | 0.1 / 0.1 / 0.5 / 0.1 ns | ≥ 0 |

A parameter the chosen type does not use, or an unknown key, is an `ApiError`.
After writing, `excitation.sync_visibility` re-hides unused parameters.

##### Sub-shape references (replace the 3D selection)

`{"object": ref, "vertex"|"edge"|"face": selector}`; selectors are
`api_common.select_subshapes` dicts (`normal`, `parallel_to`, `at_x/y/z`,
`near_point`, `tolerance`; vertices: `near_point`, `at_x/y/z`, `tolerance`),
evaluated on the object's **world-placed** shape (PartDesign features
included) and required to match **exactly one** sub-shape. A bare object
reference means the whole object (a sketch / line / curve). Raw `Face7`
names are never accepted. Explicit points: `points=[[x,y,z],[x,y,z]]` (mm)
create a hidden `Part::Line` helper (`Lumped Port Line` / `SPICE Port Line`)
linked as the terminal/curve.

#### Commands → objects → API

##### Wavesim_AddSource → `add_source(sim, *, label, component='Ez', position=None, excitation=None)`

`App::FeaturePython` "Source", proxy `source.SourceObject`, VP
`SourceViewProvider` (amber point marker). `WavesimType = "Source"`.

| property | type | default | notes |
|---|---|---|---|
| Component | Enumeration Ex\|Ey\|Ez\|Hx\|Hy\|Hz | Ez | |
| Position | Vector (mm) | domain centre (`default_position_mm`): DomainMin/Max midpoint, else material bbox centre, else origin | GUI spin ±1e6 mm |
| Excitation + params | see above | Gaussian, 30 GHz | |

Auto label `Source (<Component>, <excitation label>)`. Side effects:
`notify_domain_inputs_changed` (domain grows to include the source). GUI
Cancel on a new source removes it; the API creates only on success.

##### Wavesim_AddGaussianBeam → `add_gaussian_beam(sim, *, label, face='z0', angle=0, waist=0, directional=True, excitation=None)`

`App::FeaturePython` "GaussianBeam", proxy `GaussianBeamObject`, VP
`GaussianBeamViewProvider`. `WavesimType = "GaussianBeamSource"`.

| property | type | default | validation |
|---|---|---|---|
| Face | Enumeration x0\|x1\|y0\|y1\|z0\|z1 | z0 | |
| Angle | Angle (deg) | 0 | −360 … 360; measured in the face frame (x0: y→z, x1: z→y, y0: z→x, y1: x→z, z0: x→y, z1: y→x) |
| Waist | Length (mm) | 0 (= auto, ½ × smaller interior half-width) | 0 … 1e6 |
| Directional | Bool | True | |
| Corners | VectorList, hidden | from `execute` | |
| Excitation + params | | Gaussian, 30 GHz | |

Side effects (panel `_commit`, now `gaussian_beam.force_launch_bc`): launch
face BC set to **PML**; `notify_domain_inputs_changed`. Auto label
`Gaussian Beam (<face>, <angle>°, w0 <waist|auto>, <excitation>)`.

##### Wavesim_AddLumpedPort → `add_lumped_port(sim, *, label, plus, minus, points, reverse=False, resistance=50, inductance=None, capacitance=None, topology='series', drive='none', excitation=None)`

`App::FeaturePython` "LumpedPort", proxy `LumpedPortObject`, VP
`LumpedPortViewProvider`. `WavesimType = "LumpedPort"`.

| property | type | default | API |
|---|---|---|---|
| TerminalPlus / TerminalMinus | LinkSubGlobal | GUI: seeded from the selection at Activated (`selection_terminals`) | `plus` / `minus` refs + selectors, or `points` |
| ReversePolarity | Bool | False | `reverse` |
| UseResistance / Resistance | Bool / Float (ohm) | True / 50 | `resistance` (> 0, None = branch off) |
| UseInductance / Inductance | Bool / Float (H) | False / 1 nH | `inductance` |
| UseCapacitance / Capacitance | Bool / Float (F) | False / 1 pF | `capacitance` |
| Topology | Enumeration Series\|Parallel | Series | `series`/`parallel` |
| Drive | Enumeration None (passive load)\|Voltage source\|Current source | None | `none`/`voltage`/`current` (also `thevenin`/`norton`, labels) |
| Excitation + params | hidden while passive (`sync_drive_visibility`) | | `excitation` (refused with drive none) |
| P0 / P1 | Vector, hidden | resolved endpoints (`resolve_line`) | |

Terminal rules (`resolve_line`): two planar facing faces → line from the '+'
face centre projected normal onto the '−' plane; two vertices → joined; one
edge / curve object (plus only) → its ends, '+' at its start. Validation:
the terminals must resolve to a line (else `ApiError` with the resolver's
reason); a passive port needs a load; values > 0. Resolver warnings
(oblique line, projection off the face, curved edge) are printed to the
console. Dimensions registered: Resistance (resistance), Inductance
(inductance), Capacitance (capacitance) for `LumpedPort`. Side effects:
`notify_domain_inputs_changed`. Auto label `Lumped Port (<network>[, <mode>
drive])`, e.g. `Lumped Port (R 75 ohm + C 1 pF (parallel), voltage drive)`.

##### Wavesim_AddModalPort (+ legacy id Wavesim_AddTEMSource) → `add_modal_port(sim, *, label, face='z0', drive_mode='waveform', conductors=None, excitation=None, bounds=None, conductor_index=0, fields='EH', netlist=None, node_plus='port1p', node_minus='0', use_initial_conditions=False, invert_port_current=False)`

`App::FeaturePython` "ModalPort", proxy `ModalPortObject` (unpickling alias
`tem_source.TEMSourceObject`), VP `ModalPortViewProvider`.
`WavesimType = "ModalPort"` (legacy `TEMSource` accepted).

| property | type | default | API |
|---|---|---|---|
| Face | Enumeration x0…z1 | z0 | `face` |
| ExcitationMode | Enumeration Waveform\|SPICE | Waveform | `drive_mode` (`waveform`/`spice`, or the panel labels) |
| Conductors / ConductorFaces / Energized | LinkList / StringList / BoolList (read-only, the table) | empty (= legacy whole-face drive) | `conductors` |
| Conductor | Integer | 0 | `conductor_index` (0 … 999; written with no table, and in SPICE mode) |
| BoundsSel | LinkSub, hidden | None | `bounds` = `{"object", "edge"|"face": selector}` |
| Fields | Enumeration EH\|E | EH | `fields` (SPICE drive only) |
| Netlist / NodePlus / NodeMinus | File / String / String | '' / port1p / 0 | SPICE drive |
| UseInitialConditions / InvertPortCurrent | Bool | False | SPICE drive |
| Excitation[_n] + params[_n] | one set per table row | Gaussian, 30 GHz | `excitation` (all rows) + per-row `excitation` |
| Corners | VectorList, hidden | | |

Conductor table: `conductors=None` replicates what the panel opens a fresh
port with (`modal_port.scan_port_plane` + `default_drive_rows`, now shared
with the panel): every PEC region the plane cuts that does not touch the
solve region's edge (the grounded reference is excluded), largest first,
only row 0 energized; no PEC on the plane → no table. Explicit rows: body
reference or `{"body", "face": selector (a face of the body the plane
crosses; only needed when one body holds several conductors), "drive":
bool, "excitation"}` — what *Add selected face* does: the body must be PEC
(`materials.conductors`), its region must reach the plane
(`_region_on_plane`), the stored face is `storage_sub(region)` (empty for a
single-region body), duplicates (same region) refused. The plane is scanned
after the port exists and the domain is re-synced (a waveform port's face has
no background gap), exactly as the panel sees it after the command's commit.

Side effects: waveform drive → the face becomes a modal-port boundary
(`domain.modal_port_faces`, no PML/PEC/gap); SPICE drive →
`modal_port.force_spice_bc` (face → PML). `_sync_mode_visibility`,
`notify_domain_inputs_changed`. Validation: SPICE drive requires a netlist
and refuses waveform excitations; `netlist` with the waveform drive is
refused. Auto label `Modal Port (<face>, <conductor>, <excitation>)` /
`(<face>, N conductors driven)` / `(<face>, <netlist file>)`.

*Compute Mode* (out-of-process mode preview, `run_mode_solve`) is a GUI-only
viewer and has no API counterpart here.

##### Wavesim_AddSpiceLinePort → `add_spice_line_port(sim, *, label, curve=None, points=None, netlist, node_plus='port1p', node_minus='0', use_initial_conditions=False, invert_port_current=False)`

`App::FeaturePython` "SpiceLinePort", proxy `SpiceLinePortObject`, VP
`SpiceLinePortViewProvider`. Properties: `Sketch` (Link: curve whose chord
is the port, '+' at its start; GUI: panel combo or tree drag-and-drop),
`Netlist`, `NodePlus`, `NodeMinus`, `UseInitialConditions`,
`InvertPortCurrent`, hidden `P0`/`P1`. Exactly one of `curve` (an object with
edges and no solids) / `points` (hidden `Part::Line`); netlist required.
Side effects: retitle `SPICE Line Port (<netlist>, <+>/<−>)`,
`notify_domain_inputs_changed`.

##### Wavesim_AddSpiceTEMPort → `add_spice_tem_port(sim, *, label, face='z0', fields='EH', conductor_index=0, bounds=None, netlist, node_plus, node_minus, use_initial_conditions, invert_port_current)`

Legacy object kept because the command is still registered.
`App::FeaturePython` "SpiceTEMPort", proxy `SpiceTEMPortObject`, VP
`SpiceTEMPortViewProvider`. Properties: `Face`, `Fields` (EH\|E),
`Conductor`, hidden `BoundsSel`, SPICE props, hidden `Corners`. Side
effects: `spice_port.force_tem_bc` (face → PML), retitle `SPICE TEM Port
(<face>, <netlist>)`, `notify_domain_inputs_changed`. New work: use
`add_modal_port(drive_mode="spice")`.

##### (no command) `set_excitation(sim, *, obj, excitation, index=0)`

Rewrites the waveform of a Source / Gaussian beam / LumpedPort / ModalPort
(the panels' excitation block). `index` = modal-port table row
(0 … rows−1); 0 elsewhere. SPICE ports refused. Retitles the auto label,
re-syncs visibility (`sync_drive_visibility` / `_sync_mode_visibility`).

##### Wavesim_SetPotential → `set_potential(sim, *, body, mode='potential', volts=None, charge=None, unset_default=None)`

No document object: the panel (`potentials.TaskPotentialPanel`) writes onto
the PEC **bodies** (`materials.ensure_potential_prop`):

| property (on the body) | type | meaning |
|---|---|---|
| WavesimConductorMode | Enumeration Default\|Potential\|Floating | `mode` (`potential`/`floating`/`default`) |
| WavesimPotential | Float (V) | `volts` (quantity or `=PARAM`; registered dimension `voltage`) |
| WavesimFloatingCharge | Float (C) | `charge`: number in C or `"5 pC"` (C, mC, uC, nC, pC, fC) |
| Simulation.UnsetConductorMode | Enumeration Grounded (0 V)\|Floating (0 C) | `unset_default` (`ground`/`float`) |

Both numbers are stored whatever the mode (switching back restores them).
The panel's OK body is now `potentials.write_conditions(sim, rows, unset)`,
shared with the API. Validation: electrostatic simulation, body assigned to a
PEC material. Returns the resolved condition (`potentials.resolve`):
`{body, mode, volts, charge_C, held_at, origin (set/inherited/default),
unset_default, conflicts}` — conflicts are touching bodies asked for
different conditions (the solver would refuse the run).

##### Wavesim_PortMatrix → `configure_port_matrix(sim, *, ports=None)`, `export_port_matrix_jobs(sim, *, ports=None, root_dir=None, steps=None, progress=None)`, `load_port_matrix_results(sim, *, root_dir=None, ports=None)`

The GUI flow is a modal picker (`PortMatrixDialog`, all ports ticked, ≥ 2)
→ overwrite confirmation → voxelise once → N blocking runs → harvest →
`results.build_matrix_results`. **Nothing is persisted** on the document
before the results, and adding properties to the Simulation would change its
property editor, so `configure_port_matrix` stores nothing and returns a
validated plan (`port_matrix_plan` is the same function, unregistered):
`{ports, available, runs, matrix, excitation, sweep_dir, notes}`.
Port names are the spec names (`portmatrix.document_ports`): a lumped port's
label, a modal port's label, or `"<label> — <conductor>"` per table row (the
port label alone selects all its rows). Refusals: electrostatic mode, < 2
ports, unknown/duplicate names, SPICE ports in the document
(`portmatrix.document_spice_ports`, mirroring `spice_ports` in the job), no
waveform on any selected port.

`export_port_matrix_jobs` = the GUI's preparation without the runs, via the
shared GUI-free helpers now also used by `run_sweep`:
`portmatrix.build_sweep` (voxelise once, refuse no-structure / SPICE /
missing ports, bind names, common excitation) →
`prepare_sweep_dir(doc, n, root)` (clears `job.json`/`materials.npz`/
`results.npz`/`summary.json` and old `drive_*` folders) →
`export_drive_jobs`: `<root>/materials.npz` once, and per drive
`<root>/drive_<k>/job.json` (`drive_job`: everything silenced, port *k*
driven with the common excitation; passive lumped port promoted to a
voltage drive) + `materials.npz` (hard link, copy fallback). Drive folders
are named by `portmatrix.drive_dir_name` (`drive_0`, `drive_1`, … — what
`assemble` and `build_matrix_results` read). Default root =
`portmatrix.sweep_dir_for(doc)` (`<results>/<doc>/matrix`, the GUI's).

`load_port_matrix_results` → `results.build_matrix_results(doc, sim, root,
ports)` (Port Matrix group under the Simulation, replacing an old one) plus a
summary from `portmatrix.assemble`/`reciprocity_error`. Ports default to the
one live excitation in each `drive_<k>/job.json`.

#### GUI selections replaced

| GUI | API |
|---|---|
| Lumped port: selection at *Add* / *Set from selection* (vertex, planar face, edge, dropped curve) | `plus`/`minus` = `{"object", "vertex"|"edge"|"face": selector}` or object ref; `points` |
| Lumped port *Swap + / −* | `reverse=True` |
| Modal port *Add selected face* / *Refresh from geometry* | `conductors=[{"body", "face": selector}]` / `conductors=None` |
| Modal / SPICE TEM *Select bounding edge/face* | `bounds={"object", "edge"|"face": selector}` |
| SPICE line port curve combo / tree drop | `curve=ref` or `points` |
| Netlist *Browse…* | `netlist="path"` |
| Port matrix check boxes | `ports=[names]` |

#### GUI refactors (behaviour unchanged)

- Each `Command…Activated()` now calls the module-level `create_*` helper,
  which holds the exact former body (addObject, proxy, label, view provider
  when `_GUI_AVAILABLE and ViewObject`, Sources group); the command keeps its
  transaction, recompute and `created=True` panel.
- `auto_label(obj)` per module (`line_auto_label`/`tem_auto_label` for SPICE)
  returns the same string the panels format inline.
- Forced BCs moved into helpers the panels call: `gaussian_beam.force_launch_bc`,
  `modal_port.force_spice_bc`, `spice_port.force_tem_bc`.
- Modal panel `_face_conductors`/`_stored_rows` use
  `modal_port.scan_port_plane`/`default_drive_rows` (same scan, same order).
- Potential panel `accept` calls `potentials.write_conditions`.
- Port matrix `run_sweep` calls `portmatrix.build_sweep` (identical refusal
  texts); `prepare_sweep_dir` gained an optional `sweep` root;
  `document_ports` = `spec_ports(document_spec(sim))`.

### 3.8 Monitors

Module: [`api_monitors.py`](../wavesim_gui/api_monitors.py) (category `"monitor"`).
Shared creation helpers (used by both the commands and the API):
[`monitors.py`](../wavesim_gui/monitors.py) → `new_probe`, `new_snapshot`, `new_energy_monitor`,
`new_dissipation_monitor`, `new_path_monitor`, `new_field_line_monitor`,
`attach_path` (the tree drop), plus module-level `_is_curve_object`,
`_after_path_changed`, `_default_point_mm` (moved out of the GUI block).

All monitors are `App::FeaturePython` objects placed in the simulation's
**Monitors** group (`monitors_group(sim)`, falls back to the Simulation), with a
read-only `WavesimType` marker (group "Wavesim"). View providers are attached
only when a GUI and `ViewObject` exist. Labels are auto labels
([`labels.py`](../wavesim_gui/labels.py)): re-titled when settings change unless the user
renamed the object. Solver-mode gating mirrors `IsActive`: **only** the
field-along-curve command is mode-restricted (electrostatic only). Probes,
energy, dissipation, voltage and current monitors are enabled in both modes in
the GUI; the electrostatic runner ignores the time-series ones (the job builder
warns about probes). The API keeps that parity (no ApiError in ES mode).

| Command | Object (Name) / Proxy / ViewProvider | API |
|---|---|---|
| `Wavesim_AddProbe` | `Probe` / `ProbeObject` / `ProbeViewProvider` | `add_probe(sim=None, *, label=None, position=None, component="Ez")` |
| `Wavesim_AddSnapshot` | `Snapshot` / `SnapshotObject` / `SnapshotViewProvider` | `add_snapshot(sim=None, *, label=None, field="E", normal="z", offset=None, record_interval=None, every_n_steps=None)` |
| `Wavesim_AddEnergyMonitor` | `EnergyMonitor` / `EnergyObject` / `EnergyViewProvider` | `add_energy_monitor(sim=None, *, label=None, interior=True, full=False)` |
| `Wavesim_AddDissipationMonitor` | `DissipationMonitor` / `DissipationObject` / `DissipationViewProvider` | `add_dissipation_monitor(sim=None, *, label=None, interior=True, full=False)` |
| `Wavesim_AddVoltageMonitor` (+ drag sketch) | `VoltageMonitor` / `PathMonitorObject` / `PathMonitorViewProvider` | `add_voltage_monitor(sim=None, *, label=None, path=...)` |
| `Wavesim_AddCurrentMonitor` (+ drag sketch) | `CurrentMonitor` / `PathMonitorObject` / `PathMonitorViewProvider` | `add_current_monitor(sim=None, *, label=None, path=None, circle=None)` |
| `Wavesim_AddFieldLineMonitor` (+ drag sketch) | `FieldLineMonitor` / `FieldLineObject` / `FieldLineViewProvider` | `add_field_line_monitor(sim=None, *, label=None, path=None, circle=None, field="E", start_vertex=0, end_vertex=-1, reversed=False)` |
| tree drag & drop of a curve onto a path monitor | — | `set_monitor_path(sim=None, monitor=None, *, path=None, circle=None)` |

#### Probe (`Wavesim_AddProbe`)

- `WavesimType = "Probe"`.
- `Component` (Enumeration): `Ex Ey Ez Hx Hy Hz ∣E∣ ∣H∣` (magnitudes use U+2223
  so the property editor shows them flat; the job gets `|E|`/`|H|`). Default `Ez`.
- `Position` (Vector, mm, world): default domain centre / material-bbox centre /
  origin (`source.default_position_mm`). Snapped to the nearest cell by the solver.
- Label `Probe (<Component>)`.
- GUI: creates at the default point (transaction "Wavesim: Add Probe"),
  recomputes, opens `TaskProbePanel` (Cancel deletes the new probe; OK sets
  Component/Position in "Wavesim: Edit Probe" and re-titles). No domain update.
- API: `position` `[x, y, z]` lengths (mm default, units, `"=PARAM"` expressions
  bound on `Position.x/y/z`); `component` case-insensitive, `'|E|'`/`'|H|'`
  accepted. Errors: unknown component, bad vector, label clash.
- Job: `monitors.probes[] = {name, component, x, y, z}` (m, solver frame).

#### Snapshot (`Wavesim_AddSnapshot`)

- `WavesimType = "Snapshot"`.
- `Field` (Enumeration over `E H S phi D`): full-wave panel offers `E H S`
  (S = Poynting), electrostatic offers `phi E D`. Default `E`.
- `Plane` (Enumeration `XY YZ XZ`, default `XY`); normal axis z / x / y.
- `Offset` (Distance, mm): plane position along its normal. GUI default:
  `default_position_mm(sim).z` (XY plane through the centre).
- `RecordInterval` (Float, **seconds**; registered dimension `time`): cadence
  in simulated time; 0 = unset → `EveryNSteps`.
- `EveryNSteps` (Integer, default 20): derived from the interval and the CFL
  step (`snapshot_every_n`).
- Hidden `Corners` (VectorList) — plane corners for the view provider, from
  the domain box (`execute`).
- Label `Snapshot (<Field> <Plane> @ <axis>=<off> mm, every <interval|N steps>)`
  (no cadence in electrostatic mode).
- GUI: create (transaction "Wavesim: Add Snapshot") → `TaskSnapshotPanel`; OK
  stores Field/Plane/Offset, `RecordInterval` from the "Record every" box (pre-
  filled with `EveryNSteps·dt` when unset), `EveryNSteps` derived, re-titles,
  then `domain.notify_domain_inputs_changed` (domain grows to include the slice;
  refreshes snapshot planes). Cancel deletes the new snapshot.
- API: `field` validated against the **current solver mode**; `normal`
  `'x'|'y'|'z'` or plane name; `offset` length/expr (default: centre along the
  chosen normal — identical to the GUI for `z`); `record_interval` time (> 0,
  expr allowed) **or** `every_n_steps` (int ≥ 1, interval reset to 0); neither
  → panel default (`20·dt`). Same notify side effect.
- Job: `monitors.snapshots[] = {name, field, normal, position, every_N_steps}`;
  electrostatic jobs copy them into `electrostatic.slices`.

#### Energy / Dissipation monitors

- `WavesimType = "EnergyMonitor"` / `"DissipationMonitor"`; tree-only (no 3D).
- `RecordInterior` (Bool, default True), `RecordFullDomain` (Bool, default False);
  the panel keeps at least one ticked.
- Labels `Energy (excl. PML)` / `Dissipation (excl. PML)`, `(incl. PML)`,
  `(excl. + incl. PML)`.
- **Singletons**: the GUI warns "already exists" and returns; the API raises
  `ApiError`. Both False → `ApiError`.
- Job: `monitors.energy` / `monitors.dissipation = {full, interior}`.

#### Voltage / Current monitors

- `WavesimType = "VoltageMonitor"` / `"CurrentMonitor"`, both `PathMonitorObject`.
- `Sketch` (Link): the curve object (edges, no solids). Claimed as a tree child;
  the monitor's eye drives it (`visibility` owner `path_monitor`).
- Label `Voltage|Current Monitor (<curve label>)` / `(no curve)`.
- GUI: command creates an empty monitor (transactions "Wavesim: Add Voltage
  Monitor" / "… Current Monitor") and prints a hint; the user then **drags a
  sketch onto it**. `PathMonitorViewProvider.dropObject` → `attach_path`: link,
  re-title, `visibility.sync_from_children`, recompute,
  `notify_domain_inputs_changed` (domain grows to the curve bbox via
  `path_monitor_points_mm`).
- API replaces the drag: `path` is an existing curve object (name/label) or
  points; the API creates the curve and calls `attach_path` exactly as a drop:
  - 2 points → `Part::Line` (`X1..Z2`, each coordinate may be a param expr);
  - ≥ 3 points → `Part::Polygon` (`Nodes`; `Close` True for current loops,
    numbers only);
  - `circle={"center", "radius", "normal"}` (current/field line) →
    `Part::Circle` (Radius + Placement; center/radius accept exprs), CCW about
    the normal (I positive for current along +normal).
  - The created curve is labelled `"<monitor label> Path"` (or `Voltage Path` /
    `Current Path`) and stays at document root (shown under the monitor).
- Validation: voltage path open (first ≠ last), ≥ 2 points, no zero-length
  segments; current loop ≥ 3 distinct points or circle; exactly one of
  path/circle; referenced object must be a curve.
- Job: `monitors.voltages[] / currents[] = {name, path: [[x, y, z] m…]}` —
  the longest wire, discretised at ¼ of the smallest cell (`_path_spec`).

#### Field along curve (`Wavesim_AddFieldLineMonitor`, electrostatic only)

- `WavesimType = "FieldLineMonitor"`, `FieldLineObject`.
- `Sketch` (Link), `Field` (Enumeration `phi E D`, default `E`), `StartVertex`
  (Integer, 0), `EndVertex` (Integer, −1 = end of curve / full lap on a closed
  curve), `Reversed` (Bool, closed curves only), hidden `PathPoints` (preview).
- Label `Field Along Curve (<Field>, <curve label|no curve>)`.
- GUI: `IsActive` only in electrostatic mode. A single selected curve is linked
  at creation (before the label), then `sync_from_children` +
  `_after_path_changed`; otherwise drag a sketch later (drop resets the vertex
  range). The panel (`TaskFieldLinePanel`) picks Field/Start/End/Reversed and
  stores the curve's own end as −1; OK recomputes and notifies the domain.
- API: raises in full-wave mode; same path options as above; vertex indices
  validated against `curve_vertices_mm` (polyline vertices are its points);
  `reversed` only on closed curves; equal start/end on an open curve rejected.
- Job (electrostatic only): `electrostatic.lines[]` (points, tangents, s,
  vertex_s, normal); full-wave jobs ignore them with a warning.

### 3.9 Geometry

Source: [`api_geometry.py`](../wavesim_gui/api_geometry.py) (category `geometry`).

The workbench has **no GUI commands for geometry** — users model with the Part
(and Draft) workbench. The geometry API is therefore new code with no GUI path
to share; it creates exactly the parametric objects the Part/Draft GUI commands
create, with the same side effects (inputs of booleans, mirrors, fillets,
chamfers and arrays are hidden).

#### Common rules

- Signature `fn(doc=None, *, ...)`; `doc` = Document / name / None (active).
- Every created object needs a **unique label** (`c.check_new_label` +
  `c.set_label`); the internal Name is derived from the label.
- Dimensions: number (mm, deg) | `"3 mm"` | `{"value", "unit"}` | `"=EXPR"`
  over `Params` aliases. Expressions are bound with `setExpression` on the
  property (`Length`, `Radius`, `LengthFwd`, `NumberX`, ...) or the Placement
  component (`.Placement.Base.x`, `.Placement.Rotation.Angle`; when the angle
  is bound the axis is pinned with constant expressions on
  `.Placement.Rotation.Axis.x/y/z`, because FreeCAD may reset the axis to +Z
  when the angle evaluates to 0).
- Not bindable (documented in the docstrings): polygon vertices
  (`Part::Polygon.Nodes` is a vector list), fillet/chamfer sizes
  (`PropertyFilletEdges` stores per-edge radii; an expression is evaluated
  once), and `transform` arguments (relative, one-off change).
- One `c.transaction` per call, then `doc.recompute()`, then each created
  feature is checked (State not `Invalid`, shape not null, `isValid()`); on
  failure an `ApiError` names the object and FreeCAD's status message, and the
  call is rolled back: transaction aborted **and** created objects removed /
  hidden inputs re-shown explicitly (headless documents have `UndoMode == 0`,
  so an abort alone does not undo anything).
- Wavesim objects (`WavesimType` property, or members of the Simulation tree)
  are refused as geometry inputs.

#### Functions → FreeCAD objects

| Function | Object(s) | Properties set (units) | Validation / side effects |
|---|---|---|---|
| `create_box(label, length, width, height, position=[0,0,0], rotation=None)` | `Part::Box` | Length, Width, Height (mm); Placement.Base (mm); rotation `{"axis","angle"}` → Placement.Rotation (deg) | sizes > 0 |
| `create_cylinder(label, radius \| diameter, height, position, axis='z', angle=360)` | `Part::Cylinder` | Radius (diameter expr → `(expr) / 2`), Height, Angle (deg); Placement.Rotation maps local Z → axis | exactly one of radius/diameter; > 0 |
| `create_sphere(label, radius, position)` | `Part::Sphere` | Radius; Placement.Base = centre | radius > 0 |
| `create_cone(label, radius1, radius2, height, position, axis='z', angle=360)` | `Part::Cone` | Radius1, Radius2 (≥ 0, not both 0), Height, Angle | |
| `create_torus(label, radius1, radius2, position, axis='z')` | `Part::Torus` | Radius1 (major), Radius2 (tube) | radius2 < radius1 |
| `create_wedge(label, xmin=0, ymin=0, zmin=0, x2min=2, z2min=2, xmax=10, ymax=10, zmax=10, x2max=8, z2max=8, position, rotation)` | `Part::Wedge` | the ten bounds (mm), FreeCAD defaults | min < max (x2/z2: ≤) |
| `create_extruded_polygon(label, points, height, plane='xy', offset=0)` | hidden `Part::Polygon` "`<label> profile`" (Nodes, Close=True, Placement.Base.<normal> = offset) + `Part::Extrusion` (Base=profile, DirMode=Custom, Dir=normal, LengthFwd=height, Solid=True) | plane xy→(x,y) normal +z; xz→(x,z) normal +y; yz→(y,z) normal +x | ≥ 3 points, non-zero area, result must be a solid |
| `boolean(label, op, base, tools)` | fuse → `Part::MultiFuse` (Shapes); cut → `Part::Cut` (Base, Tool; several tools first fused into hidden `Part::MultiFuse` "`<label> tools`"); common → `Part::MultiCommon` | — | ≥ 1 tool, distinct inputs; inputs hidden |
| `transform(obj, translate, rotate_axis='z', angle=0, center=None)` | modifies `Placement` | new = T(translate) · R(axis, angle about center) · old; center default = bbox centre | refuses objects with Placement expressions |
| `mirror(label, obj, plane='xy'\|'yz'\|'xz'\|normal, base_point)` | `Part::Mirroring` | Source, Normal, Base (PropertyPosition, bindable per component) | source hidden |
| `linear_array(label, obj, direction, count, spacing)` | Draft ortho array (`Part::FeaturePython`, `use_link=False`, Fuse=False) | NumberX = count (bindable), IntervalX = direction·spacing (bindable per component as `(expr) * k`), NumberY = NumberZ = 1 | count ≥ 1; source hidden |
| `polar_array(label, obj, axis='z', center, count, angle=360)` | Draft polar array | NumberPolar (bindable), Angle (deg, bindable), Axis, Center (bindable) | count ≥ 2; source hidden |
| `fillet(label, obj, edges, radius)` | `Part::Fillet` | Base, Edges = [(i, r, r)] | edges from `c.select_subshapes` only; 0 matches → ApiError; returns `{"object": describe, "edges_matched", "edges"}`; base hidden |
| `chamfer(label, obj, edges, size)` | `Part::Chamfer` | same | same |
| `import_step(path, label_prefix=None)` | whatever `Import.insert` creates (fallback `Part.read` → `Part::Feature`) | labels `<prefix>` or `<prefix>_<n>` | file must exist, `.step`/`.stp`; returns list of created objects |
| `measure(obj)` | — | `bbox` {min,max,size} mm, `volume_mm3`, `area_mm2`, `center_of_mass`, `solids`, `valid` | |
| `check_geometry(objects=None, tolerance_mm3=1e-6)` | — | per object: valid, null, solids, issues; `overlaps` [{a, b, volume_mm3}] | default set: visible Part shapes, not Wavesim, not consumed by another feature (link-property inputs); bbox prefilter before `common()` |
| `delete_object(obj, force=False)` | removes object | returns {deleted, dependents, shown} | refuses with dependents (InList, excluding plain groups) unless force; refuses Wavesim objects; re-shows unused inputs like the Part GUI does |
| `rename_object(obj, new_label)` | Label | | unique; geometry only |
| `list_objects(include_wavesim=False)` | — | [{name, label, type, visible, bbox, volume, is_solid, consumed_by, material}] | material = label of the Wavesim material whose `Bodies` contains the object |

#### GUI selections replaced

- Part GUI booleans/mirror/fillet use the selection → API takes object refs
  (Name or unique Label).
- Fillet/chamfer edge picking → declarative selectors
  (`{"type":"all"}`, `{"parallel_to":"z"}`, `{"at_z":1.6}`,
  `{"near_point":[x,y,z]}`, AND-combined) — never raw edge indices.

#### Trade-offs / limitations

- Draft arrays work headless under FreeCADCmd (verified), so arrays are
  parametric Draft arrays; no MultiFuse fallback was needed. The document
  then needs the Draft module to recompute arrays (always shipped with
  FreeCAD).
- Fillet/chamfer sizes and polygon vertices are not parameter-bound.
- Imported STEP shapes are plain (non-parametric) `Part::Feature`s.

### 3.10 Preview: screenshots, visibility, cross-section, voxels

Source: [`api_preview.py`](../wavesim_gui/api_preview.py) (category `preview`).

**`Wavesim_CrossSectionToggle` ("Cross Section On/Off") and
`Wavesim_CrossSectionGrid` ("Mesh Grid On/Off").** These are two checkable
buttons on their own toolbar, next to raw widgets for the axis, the position
and the kept side. There is no task panel. `IsActive` requires a simulation.
The cut cuts each visible solid with a half-space box (`Shape.cut`), so the
section face is closed and capped. Bodies lying entirely beyond the plane are
hidden. **The cut plane is the Domain's grid plane** (`GridPlaneX/Y/Z`). The
grid button implies the cut, and turning the cut off removes the grid too.
The whole state lives on the Domain: `WavesimXSecAxis` (−1 means never set),
`WavesimXSecFlip`, `WavesimXSecGrid`, `WavesimXSecOn` (all hidden) and
`WavesimXSecStyle` (`Shaded`|`Hatched`|`Plain`, visible in the property editor
only). Runs are unaffected.

`cross_section(doc=None, on=True, grid=None, plane=None, offset=None, flip=None, style=None)`:

* **Needs the GUI.** It also needs a simulation domain, otherwise `ApiError`.
* `plane` takes `x|y|z`. The aliases `yz` (= x), `xz`/`zx` (= y) and
  `xy` (= z) are also accepted.
* `offset` is a length on that axis and must lie inside the domain extent,
  otherwise `ApiError`. Numbers only.
* `flip` keeps the high side of the cut.
* `style` takes `Shaded`, `Hatched` or `Plain`.
* `on=False` takes down both the cut and the grid. `grid=True` raises the cut
  with the grid on it, and `grid=False` keeps the cut with no grid.
* Returns the stored state: `axis` (`'x'|'y'|'z'`), position, flip, style,
  grid, on, plus `sectioned`.
* The toolbar's check marks are re-synchronized afterwards.

Other preview functions (these come from FreeCAD's own view menu, not from a
Wavesim command):

* `screenshot(doc=None, view="iso", path=None, width=1280, height=960, fit=True, background="White")`
  replaces the standard views and *Save picture*.
  * **Needs the GUI.**
  * `view` takes `iso|front|back|top|bottom|left|right|current`.
  * `path` must end in `.png`, and its folder must already exist.
  * `width` and `height` must be 64…4096 px.
  * `background` takes `White|Black|Transparent|Current`.
  * Returns `{path, view, width, height}`.
* `set_visibility(doc=None, objects=None, visible=True)` replaces the Space
  bar / eye toggle. `objects` is one reference, a list, or `"domain"`. Returns
  `{objects, visible}`.
* `voxel_preview(doc=None, plane="z", offset=None, path=None, quantity="material", show_grid=True)`
  has **no GUI counterpart** and **works headless**.
  * It voxelizes the document as an export would
    (`voxelize.build_job_from_document(doc, steps=1)`), but writes nothing to
    a run folder.
  * It renders one cell slice with matplotlib. PEC is drawn black; a
    conformal Yee face that is less than half open counts as PEC. Dielectrics
    are coloured by permittivity, or by sigma or PEC depending on `quantity`
    (`material|eps|sigma|pec`).
  * Cell lines are drawn only when there are at most 300 of them.
  * `offset` defaults to the domain centre.
  * Returns `{path, plane, offset_mm, index, cells, pec_cells_in_slice, materials_seen}`.

**Not exposed: the *Compute Mode* button** of the Modal Port panel and the
SPICE TEM Port panel. It runs an out-of-process 2D mode solve of that port
(`modal_port.run_mode_solve`, in a dedicated work folder) and plots the modes
without touching the document. No API function solves or plots port modes.
`voxel_preview` is unrelated: it renders an image of the voxelized
**materials** on a plane, as the solver will see them.

### 3.11 Run and results

**`Wavesim_Run` ("Run Simulation")** ([`commands.py`](../wavesim_gui/commands.py)).
`IsActive` requires a simulation. The command runs these steps in order:

1. It asks before overwriting the document's run folder
   (`job.workdir_for(doc, prefix="run")` under the results folder in
   Settings) with `run.confirm_overwrite`.
2. It voxelizes with a cancelable progress dialog
   (`voxelize.build_job_from_document(doc, progress)`). A `GridRequiredError`
   is shown in a message box.
3. If no material is assigned, it falls back to a vacuum demo box
   (`job.build_demo_job`) and prints a warning.
4. It calls `job.prepare_workdir`, `job.write_job` and
   `voxelize.write_materials`.
5. It runs `run.run_job` in a blocking progress dialog. That dialog counts
   steps, or shows a percentage (`ES_PROGRESS_STEPS = 1000`) for an
   electrostatic job.
6. It calls `results.build_results(doc, sim, workdir, summary)`.
7. It shows a summary dialog (grid, dt, steps, wall time; charges,
   potentials and the capacitance matrix in electrostatic mode).

The API **never runs the solver inside FreeCAD**. An external process runs it
instead:

1. `export_job(doc=None, workdir=None, steps=None, backend=None, wavesim_path=None, progress=None)`
   * Voxelizes and writes `job.json` + `materials.npz`.
   * **`workdir` is required.** There is no confirmation: only the previous
     job files (`job.JOB_ARTEFACTS`) are removed, and the GUI's own results
     folder is not touched.
   * `steps` is a positive integer. By default it is derived from `MaxTime`
     and the CFL step.
   * `backend` takes `auto|numba|numpy` and overrides the setting.
     `wavesim_path` is stamped into the job.
   * It refuses a document with no material assigned. The GUI runs the demo
     box in that case.
   * Errors: grid errors raise `ApiError`, and a cancellation raises
     `ApiError("Job export was cancelled.")`.
   * Progress goes through `progress(done, total)`, which the bridge supplies
     and uses to answer `status` and `cancel`.
   * Returns `{workdir, job, materials, mode, steps, grid: {Nx, Ny, Nz}, backend, warnings}`.
2. `runner_path()` returns `{runner, exporter, workbench}`, so the external
   launcher uses the `runner.py` that matches this workbench.
3. `load_results(doc=None, workdir=None)` needs `summary.json` (whose `ok`
   must not be false) and `results.npz` in the folder. It calls
   `results.build_results` exactly as *Run* does after its dialog. Returns
   `{results_group, leaves, summary: {ok, steps, dt, wall_time_s, Nx, Ny, Nz, backend}}`.

Plotting results (double-clicking result nodes) stays GUI-only. For the port
matrix sweep (`Wavesim_PortMatrix`), see
[3.7](#37-excitation-sources-ports-potentials-port-matrix).

### 3.12 Documents and settings

Documents use FreeCAD's File menu; there is no Wavesim command for them. Their
functions live in [`api_document.py`](../wavesim_gui/api_document.py)
(category `document`). Every function returns
`{name, label, path, modified, objects, active}` unless noted otherwise.

* `new_document(name="Unnamed")` creates the document and makes it active,
  including its 3D view.
* `open_document(path=None)` opens an `.FCStd` file. If that file is already
  open, it activates it instead.
* `save_document(doc=None, path=None)`. A `path` must end in `.FCStd` and its
  folder must exist (this is a *save as*). A document that was never saved
  needs a `path`.
* `recompute(doc=None)` returns `{document, failed: [{object, state}]}`.
* `active_document()` returns the active document, or `None`.
* `set_active_document(doc=None)` makes a document active.
* `list_documents()` lists every open document.
* `close_document(doc=None, discard_changes=False)` returns `{closed}`. It
  refuses a saved document with unsaved changes unless `discard_changes` is
  true.

**`Wavesim_Settings` ("Settings...")**
([`wavesim_settings.py`](../wavesim_settings.py)) opens a dialog that edits
`<FreeCAD user app data>/wavesim_settings.json`. The keys and the environment
variables that override them:

| Key | Environment override |
|---|---|
| `wavesim_python` | `WAVESIM_PYTHON` |
| `wavesim_path` | `WAVESIM_PATH` |
| `wavesim_results` | `WAVESIM_RESULTS` |
| `ngspice_dll` | `WAVESIM_NGSPICE_DLL` |
| `backend` | `WAVESIM_BACKEND` |
| `voxelize_workers` | `WAVESIM_VOXELIZE_WORKERS` |
| `colormap_bands` | `WAVESIM_COLORMAP_BANDS` |
| `bridge_autostart` | `WAVESIM_BRIDGE_AUTOSTART` |
| `bridge_port` | `WAVESIM_BRIDGE_PORT` |
| `bridge_token` | `WAVESIM_BRIDGE_TOKEN` |

The API cannot change these settings: there is **no API function for
settings**. As a result, the agent cannot change the interpreter, the results
folder or its own access. `export_job(backend=…, wavesim_path=…)` overrides
those two values for a single job.

---

## 4. Agent bridge

Source: [`wavesim_gui/bridge.py`](../wavesim_gui/bridge.py). An XML-RPC
listener lets the MCP server in `wavesim-ai-harness` drive this FreeCAD
session.

**Security.**

* The bridge binds **127.0.0.1 only**. `Bridge(host=…)` refuses any other
  address.
* Every call carries the **token** from the settings. It is compared in
  constant time (`hmac.compare_digest`). A wrong token gets
  `error_type: "AuthError"`.
* Dispatch is **whitelist only**: a call names one function registered with
  `@api_function`. There is no eval/exec and no attribute access.
* Arguments and results travel as **JSON strings** inside XML-RPC, which
  avoids XML-RPC's int and None limits. FreeCAD objects are returned through
  `describe()`.

**Endpoints.** XML-RPC path `/RPC2`. Each endpoint takes the token as its
first argument and returns a JSON string.

| Endpoint | Returns |
|---|---|
| `call(token, name, args_json[, timeout_s])` | `{"ok": true, "result": …}` or `{"ok": false, "error", "error_type", "traceback_summary"?}`; `error_type` ∈ `ApiError`, `AuthError`, `UsageError`, `Timeout`, `InternalError` |
| `bridge_info(token)` | `{protocol (=1), workbench_version, api_version, freecad_version, gui, port, functions}` |
| `status(token)` | `{busy, progress {function, done, total, t, percent}, queued, uptime_s, calls, last_error}` (answered from the RPC thread, also while a call runs) |
| `cancel(token)` | `{cancel_requested, busy}`: sets the cancel flag that the running export/voxelization polls through its progress callback |
| `list_functions(token)` | `[{name, category, signature, doc}]` for every registered function |

**`call` validation.** Each of the following returns a `UsageError` without
running anything:

* an unknown function name;
* arguments that are not valid JSON, or not a JSON object;
* an argument whose name starts with `_`, or is named `progress`;
* arguments that do not bind to the function's signature. The error message
  includes the signature.

When the function has a `progress` parameter, the bridge injects its own
callback. `ApiError` maps to `error_type: "ApiError"`. Any other exception
becomes `InternalError` with the last six traceback frames.

**Threading.**

* FreeCAD is not thread-safe. The server is a `ThreadingMixIn`
  `SimpleXMLRPCServer` running in a daemon thread named `WavesimAgentBridge`.
* Its handler threads only **queue** jobs. A `QTimer` (every 30 ms) on the Qt
  main thread drains the queue **one call at a time**, and does not re-enter
  while a call is running.
* The progress callback runs `QApplication.processEvents()` at most every
  0.05 s, so the GUI stays responsive and `status` keeps answering. The
  callback returns the cancel flag.
* `call` waits `timeout_s`, by default `DEFAULT_TIMEOUT_S = 900` s. When the
  wait times out, it returns `Timeout` while the job keeps running in FreeCAD
  (poll `status`).
* While a call runs, the status bar shows `Agent: <function> ...`.
* The executor `"direct"` (headless tests only) runs calls in the RPC thread
  under a lock instead.

**`Wavesim_AgentBridge` ("Agent Bridge On/Off").** A checkable toolbar toggle
that is always active. It calls `start_bridge()` / `stop_bridge()`. While the
bridge runs, a permanent status-bar label reads
`Agent bridge: ON (127.0.0.1:<port>)`. If the port is taken, the error is
printed ("Is another FreeCAD running it? Change the port in Wavesim ->
Settings."). `InitGui.py` registers the command and, once the event loop runs,
calls `bridge.autostart()`.

**Settings keys** in `wavesim_settings.json` (FreeCAD user app data dir),
edited in *Wavesim → Settings → Agent bridge (AI agent access)*:

| Key | Default | Meaning |
|---|---|---|
| `bridge_autostart` | `false` | start the bridge when FreeCAD starts (`bridge.autostart()`) |
| `bridge_port` | `9875` | TCP port on 127.0.0.1; values outside 1024…65535 fall back to 9875 |
| `bridge_token` | `""` | generated with `secrets.token_urlsafe(32)` on first use (`get_bridge_token(create=True)`); shown read-only with *Copy* and *Regenerate* (regenerating locks out an agent configured with the old token). It goes into the agent's config (`config.json` → `rpc_token`) |

The environment variables `WAVESIM_BRIDGE_AUTOSTART`, `WAVESIM_BRIDGE_PORT`
and `WAVESIM_BRIDGE_TOKEN` override absent stored values.

---

## 5. Alphabetical list of every registered API function

The list below was generated from `wavesim_agent_mcp/api_catalog.json` in the
`wavesim-ai-harness` repository (API version 1, workbench 0.2.0.dev0), which is
a dump of the bridge's `list_functions`. It contains 79 functions. The first
argument is `sim` or `doc` as described in [§1.2](#12-conventions-api_common-module-docstring).

| Function | Category | Parameters (defaults) | Purpose |
|---|---|---|---|
| `active_document` | document | — | The active document (or ``None``). |
| `add_current_monitor` | monitor | `sim=null, label=null, path=null, circle=null` | Add a current monitor I(t) = loop integral of H.dl and return it. |
| `add_dissipation_monitor` | monitor | `sim=null, label=null, interior=true, full=false` | Add the ohmic-dissipation monitor and return it (*Add Dissipation Monitor*). |
| `add_energy_monitor` | monitor | `sim=null, label=null, interior=true, full=false` | Add the total-energy monitor and return it (*Add Energy Monitor*). |
| `add_field_line_monitor` | monitor | `sim=null, label=null, path=null, circle=null, field="E", start_vertex=0, end_vertex=-1, reversed=false` | Add a field-along-curve monitor and return it (*Add Field Along Curve*). |
| `add_gaussian_beam` | excitation | `sim=null, label=null, face="z0", angle=0.0, waist=0.0, directional=true, excitation=null` | Add a Gaussian-beam boundary source on a domain face and return it. |
| `add_lumped_port` | excitation | `sim=null, label=null, plus=null, minus=null, points=null, reverse=false, resistance=50.0, inductance=null, capacitance=null, topology="series", drive="none", excitation=null` | Add a lumped R/L/C port (optionally a voltage/current source) across a gap. |
| `add_modal_port` | excitation | `sim=null, label=null, face="z0", drive_mode="waveform", conductors=null, excitation=null, bounds=null, conductor_index=0, fields="EH", netlist=null, node_plus="port1p", node_minus="0", use_initial_conditions=false, invert_port_current=false` | Add a modal (TEM) waveguide port on a domain face and return it. |
| `add_probe` | monitor | `sim=null, label=null, position=null, component="Ez"` | Add a point field probe and return it (*Wavesim -> Add Probe*). |
| `add_snapshot` | monitor | `sim=null, label=null, field="E", normal="z", offset=null, record_interval=null, every_n_steps=null` | Add a 2D field-slice monitor and return it (*Wavesim -> Add Snapshot*). |
| `add_source` | excitation | `sim=null, label=null, component="Ez", position=null, excitation=null` | Add a soft point source and return it. |
| `add_spice_line_port` | excitation | `sim=null, label=null, curve=null, points=null, netlist=null, node_plus="port1p", node_minus="0", use_initial_conditions=false, invert_port_current=false` | Add a straight lumped port coupled to an ngspice netlist (co-simulation). |
| `add_spice_tem_port` | excitation | `sim=null, label=null, face="z0", fields="EH", conductor_index=0, bounds=null, netlist=null, node_plus="port1p", node_minus="0", use_initial_conditions=false, invert_port_current=false` | Add a legacy SPICE TEM port (a TEM plane driven by an ngspice netlist). |
| `add_voltage_monitor` | monitor | `sim=null, label=null, path=null` | Add a voltage monitor V(t) = integral of E.dl and return it. |
| `assign_material` | material | `sim=null, bodies=null, material=null` | Assign one or more solid bodies to *material*. |
| `bind_param` | params | `doc=null, obj=null, prop=null, expression=null` | Drive ``obj.prop`` by an expression over PARAMS names. |
| `boolean` | geometry | `doc=null, label="__required__", op="fuse", base="__required__", tools="__required__"` | Combine solids with a parametric boolean and return the result. |
| `chamfer` | geometry | `doc=null, label="__required__", obj="__required__", edges="__required__", size="__required__"` | Bevel edges of a solid with a ``Part::Chamfer`` feature. |
| `check_geometry` | geometry | `doc=null, objects=null, tolerance_mm3=1e-06` | Check solids for validity and find overlapping pairs. |
| `close_document` | document | `doc=null, discard_changes=false` | Close a document. Refuses unsaved changes unless *discard_changes*. |
| `configure_port_matrix` | port_matrix | `sim=null, ports=null` | Validate a port-matrix (Z/Y) selection and return the sweep plan. |
| `create_box` | geometry | `doc=null, label="__required__", length="__required__", width="__required__", height="__required__", position=null, rotation=null` | Create a parametric box (``Part::Box``) and return it. |
| `create_cone` | geometry | `doc=null, label="__required__", radius1="__required__", radius2="__required__", height="__required__", position=null, axis="z", angle=360` | Create a parametric cone / frustum (``Part::Cone``) and return it. |
| `create_cylinder` | geometry | `doc=null, label="__required__", radius=null, diameter=null, height="__required__", position=null, axis="z", angle=360` | Create a parametric cylinder (``Part::Cylinder``) and return it. |
| `create_extruded_polygon` | geometry | `doc=null, label="__required__", points="__required__", height="__required__", plane="xy", offset=0` | Extrude a closed polygon along its plane normal and return the solid. |
| `create_material` | material | `sim=null, label=null, eps=1.0, mu=1.0, sigma=0.0, pec=false, color=null, bodies=null` | Create a Material and return it. |
| `create_sphere` | geometry | `doc=null, label="__required__", radius="__required__", position=null` | Create a parametric sphere (``Part::Sphere``) centred at *position*. |
| `create_torus` | geometry | `doc=null, label="__required__", radius1="__required__", radius2="__required__", position=null, axis="z"` | Create a parametric torus (``Part::Torus``) and return it. |
| `create_wedge` | geometry | `doc=null, label="__required__", xmin=0, ymin=0, zmin=0, x2min=2, z2min=2, xmax=10, ymax=10, zmax=10, x2max=8, z2max=8, position=null, rotation=null` | Create a parametric wedge (``Part::Wedge``) and return it. |
| `cross_section` | preview | `doc=null, on=true, grid=null, plane=null, offset=null, flip=null, style=null` | Cut the model open on a grid plane (view only; runs are unaffected). |
| `delete_object` | geometry | `doc=null, obj="__required__", force=false` | Delete a geometry object. |
| `delete_setup_object` | setup | `sim=null, obj=null` | Delete a source, port, monitor, material or result object. |
| `export_job` | run | `doc=null, workdir=null, steps=null, backend=null, wavesim_path=null, progress=null` | Voxelize the document and write ``job.json`` + ``materials.npz``. |
| `export_port_matrix_jobs` | port_matrix | `sim=null, ports=null, root_dir=null, steps=null, progress=null` | Voxelise once and write one solver job per port-matrix drive. |
| `fillet` | geometry | `doc=null, label="__required__", obj="__required__", edges="__required__", radius="__required__"` | Round edges of a solid with a ``Part::Fillet`` feature. |
| `get_object_props` | setup | `sim=null, obj=null` | All visible properties of a setup object, with dimensions and units. |
| `get_simulation` | simulation | `sim=null` | Return the Simulation container (raises if the document has none). |
| `import_step` | geometry | `doc=null, path="__required__", label_prefix=null` | Import a STEP file into the document and return the created objects. |
| `linear_array` | geometry | `doc=null, label="__required__", obj="__required__", direction="x", count="__required__", spacing="__required__"` | Create a parametric linear array (Draft ortho array) of *obj*. |
| `list_bindings` | params | `doc=null` | Every property driven by a ``Params`` expression. |
| `list_documents` | document | — | Every open document. |
| `list_materials` | material | `sim=null` | Every material: label, eps, mu, sigma (S/m), pec, assigned bodies. |
| `list_objects` | geometry | `doc=null, include_wavesim=false` | List the geometry objects of a document. |
| `list_setup` | setup | `sim=null` | Structured description of the whole simulation setup. |
| `load_port_matrix_results` | port_matrix | `sim=null, root_dir=null, ports=null` | Assemble a finished port-matrix sweep and add it to the Results tree. |
| `load_results` | run | `doc=null, workdir=null` | Load a finished run's results into the document tree. |
| `measure` | geometry | `doc=null, obj="__required__"` | Measure an object's shape. |
| `mesh_info` | simulation | `sim=null` | Current grid: cell counts, sizes, dt and step count (no voxelizing). |
| `mirror` | geometry | `doc=null, label="__required__", obj="__required__", plane="xy", base_point=null` | Create a parametric mirrored copy (``Part::Mirroring``) of *obj*. |
| `model_fingerprint` | params | `doc=null` | Stable hashes of the simulated model, computed without voxelizing. |
| `new_document` | document | `name="Unnamed"` | Create a new empty document and make it active. |
| `new_simulation` | simulation | `doc=null, label="Simulation", mode="fdtd"` | Create the Simulation container (one per document) and return it. |
| `open_document` | document | `path=null` | Open an ``.FCStd`` file (or activate it if it is already open). |
| `polar_array` | geometry | `doc=null, label="__required__", obj="__required__", axis="z", center=null, count="__required__", angle=360` | Create a parametric polar array (Draft polar array) of *obj*. |
| `property_dimension_of` | params | `doc=null, obj=null, prop=null` | Physical dimension a property expects (``"length"``, ``"frequency"``, ...), or ``None`` if unknown -- used to check a binding before making it. |
| `read_params_sheet` | params | `doc=null` | Current values in the ``Params`` sheet: ``{NAME: value}`` (storage units), or ``{}`` when there is no sheet. |
| `recompute` | document | `doc=null` | Recompute the document; report objects that failed. |
| `refine_body` | simulation | `sim=null, body=null, cells=null` | Ask for at least N grid cells across a body's bounding box per axis. |
| `rename_object` | geometry | `doc=null, obj="__required__", new_label="__required__"` | Give a geometry object a new unique label and return it. |
| `runner_path` | run | — | Absolute path of the workbench's ``runner.py`` (and ``exporter.py`` directory), so an external launcher uses the matching solver bridge. |
| `save_document` | document | `doc=null, path=null` | Save the document (to *path* when given, as ``.FCStd``). |
| `screenshot` | preview | `doc=null, view="iso", path=null, width=1280, height=960, fit=true, background="White"` | Save a PNG of the 3D view from a standard direction. |
| `set_active_document` | document | `doc=null` | Make an open document the active one. |
| `set_domain` | simulation | `sim=null, spacing=null, boundaries=null, pml_cells=null, background=null, es_boundaries=null` | Set the Domain box padding, boundary conditions and background medium. |
| `set_excitation` | excitation | `sim=null, obj=null, excitation=null, index=0` | Change the temporal waveform of a source, beam or port. |
| `set_material` | material | `sim=null, material=null, label=null, eps=null, mu=null, sigma=null, pec=null, color=null` | Edit an existing material (the New/Edit Material panel's fields). |
| `set_mesh` | simulation | `sim=null, cell_size=null, cells_per_wavelength=null, cells_per_feature=null, nonuniform=null, max_grading=null, min_cell_size=null, curvature_refinement=null` | Set the grid: cell size, resolution rules and non-uniform grading. |
| `set_monitor_path` | monitor | `sim=null, monitor=null, path=null, circle=null` | Give a voltage/current/field-along-curve monitor a new path. |
| `set_object_props` | setup | `sim=null, obj=null, props=null` | Set properties of a setup object (source, port, monitor, material, Domain, Simulation) by name, with validation. |
| `set_potential` | electrostatics | `sim=null, body=null, mode="potential", volts=null, charge=null, unset_default=null` | Set an electrostatic conductor condition on a PEC body. |
| `set_run_settings` | simulation | `sim=null, max_time=null, max_frequency=null, time_unit=null, frequency_unit=null, subpixel=null, conformal_pec=null, conformal_area_threshold=null` | Set the Simulation panel's run settings. ``None`` leaves a value as is. |
| `set_solver_mode` | simulation | `sim=null, mode="fdtd", extract_capacitance=null, unset_conductors=null` | Switch between full-wave FDTD and electrostatics. |
| `set_visibility` | preview | `doc=null, objects=null, visible=true` | Show or hide objects in the 3D view (no effect on the simulation). |
| `sync_params_to_spreadsheet` | params | `doc=null, params=null, remove_missing=true` | Write resolved parameters into the ``Params`` sheet and recompute. |
| `transform` | geometry | `doc=null, obj="__required__", translate=null, rotate_axis="z", angle=0, center=null` | Move and/or rotate an object relative to its current placement. |
| `unassign_material` | material | `sim=null, bodies=null` | Remove bodies from whatever material holds them. |
| `unbind_param` | params | `doc=null, obj=null, prop=null` | Remove the expression on ``obj.prop`` (its current value stays). |
| `validate_setup` | setup | `sim=null, max_cells=null, throughput=null` | Check the setup before running; nothing is voxelized or written. |
| `voxel_preview` | preview | `doc=null, plane="z", offset=null, path=null, quantity="material", show_grid=true` | Render what the solver will see: the voxelized materials on a plane. |
