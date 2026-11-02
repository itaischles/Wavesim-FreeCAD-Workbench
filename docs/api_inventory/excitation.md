# API inventory — excitation (`wavesim_gui/api_excitation.py`)

Sources, ports, beams, electrostatic potentials and the port matrix.

## Common rules

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

### Excitation dict (all driven objects)

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

### Sub-shape references (replace the 3D selection)

`{"object": ref, "vertex"|"edge"|"face": selector}`; selectors are
`api_common.select_subshapes` dicts (`normal`, `parallel_to`, `at_x/y/z`,
`near_point`, `tolerance`; vertices: `near_point`, `at_x/y/z`, `tolerance`),
evaluated on the object's **world-placed** shape (PartDesign features
included) and required to match **exactly one** sub-shape. A bare object
reference means the whole object (a sketch / line / curve). Raw `Face7`
names are never accepted. Explicit points: `points=[[x,y,z],[x,y,z]]` (mm)
create a hidden `Part::Line` helper (`Lumped Port Line` / `SPICE Port Line`)
linked as the terminal/curve.

## Commands → objects → API

### Wavesim_AddSource → `add_source(sim, *, label, component='Ez', position=None, excitation=None)`

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

### Wavesim_AddGaussianBeam → `add_gaussian_beam(sim, *, label, face='z0', angle=0, waist=0, directional=True, excitation=None)`

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

### Wavesim_AddLumpedPort → `add_lumped_port(sim, *, label, plus, minus, points, reverse=False, resistance=50, inductance=None, capacitance=None, topology='series', drive='none', excitation=None)`

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

### Wavesim_AddModalPort (+ legacy id Wavesim_AddTEMSource) → `add_modal_port(sim, *, label, face='z0', drive_mode='waveform', conductors=None, excitation=None, bounds=None, conductor_index=0, fields='EH', netlist=None, node_plus='port1p', node_minus='0', use_initial_conditions=False, invert_port_current=False)`

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

### Wavesim_AddSpiceLinePort → `add_spice_line_port(sim, *, label, curve=None, points=None, netlist, node_plus='port1p', node_minus='0', use_initial_conditions=False, invert_port_current=False)`

`App::FeaturePython` "SpiceLinePort", proxy `SpiceLinePortObject`, VP
`SpiceLinePortViewProvider`. Properties: `Sketch` (Link: curve whose chord
is the port, '+' at its start; GUI: panel combo or tree drag-and-drop),
`Netlist`, `NodePlus`, `NodeMinus`, `UseInitialConditions`,
`InvertPortCurrent`, hidden `P0`/`P1`. Exactly one of `curve` (an object with
edges and no solids) / `points` (hidden `Part::Line`); netlist required.
Side effects: retitle `SPICE Line Port (<netlist>, <+>/<−>)`,
`notify_domain_inputs_changed`.

### Wavesim_AddSpiceTEMPort → `add_spice_tem_port(sim, *, label, face='z0', fields='EH', conductor_index=0, bounds=None, netlist, node_plus, node_minus, use_initial_conditions, invert_port_current)`

Legacy object kept because the command is still registered.
`App::FeaturePython` "SpiceTEMPort", proxy `SpiceTEMPortObject`, VP
`SpiceTEMPortViewProvider`. Properties: `Face`, `Fields` (EH\|E),
`Conductor`, hidden `BoundsSel`, SPICE props, hidden `Corners`. Side
effects: `spice_port.force_tem_bc` (face → PML), retitle `SPICE TEM Port
(<face>, <netlist>)`, `notify_domain_inputs_changed`. New work: use
`add_modal_port(drive_mode="spice")`.

### (no command) `set_excitation(sim, *, obj, excitation, index=0)`

Rewrites the waveform of a Source / Gaussian beam / LumpedPort / ModalPort
(the panels' excitation block). `index` = modal-port table row
(0 … rows−1); 0 elsewhere. SPICE ports refused. Retitles the auto label,
re-syncs visibility (`sync_drive_visibility` / `_sync_mode_visibility`).

### Wavesim_SetPotential → `set_potential(sim, *, body, mode='potential', volts=None, charge=None, unset_default=None)`

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

### Wavesim_PortMatrix → `configure_port_matrix(sim, *, ports=None)`, `export_port_matrix_jobs(sim, *, ports=None, root_dir=None, steps=None, progress=None)`, `load_port_matrix_results(sim, *, root_dir=None, ports=None)`

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

## GUI selections replaced

| GUI | API |
|---|---|
| Lumped port: selection at *Add* / *Set from selection* (vertex, planar face, edge, dropped curve) | `plus`/`minus` = `{"object", "vertex"|"edge"|"face": selector}` or object ref; `points` |
| Lumped port *Swap + / −* | `reverse=True` |
| Modal port *Add selected face* / *Refresh from geometry* | `conductors=[{"body", "face": selector}]` / `conductors=None` |
| Modal / SPICE TEM *Select bounding edge/face* | `bounds={"object", "edge"|"face": selector}` |
| SPICE line port curve combo / tree drop | `curve=ref` or `points` |
| Netlist *Browse…* | `netlist="path"` |
| Port matrix check boxes | `ports=[names]` |

## GUI refactors (behaviour unchanged)

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
