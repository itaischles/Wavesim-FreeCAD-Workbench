# API inventory — Monitors

Module: `wavesim_gui/api_monitors.py` (category `"monitor"`).
Shared creation helpers (used by both the commands and the API):
`wavesim_gui/monitors.py` → `new_probe`, `new_snapshot`, `new_energy_monitor`,
`new_dissipation_monitor`, `new_path_monitor`, `new_field_line_monitor`,
`attach_path` (the tree drop), plus module-level `_is_curve_object`,
`_after_path_changed`, `_default_point_mm` (moved out of the GUI block).

All monitors are `App::FeaturePython` objects placed in the simulation's
**Monitors** group (`monitors_group(sim)`, falls back to the Simulation), with a
read-only `WavesimType` marker (group "Wavesim"). View providers are attached
only when a GUI and `ViewObject` exist. Labels are auto labels
(`wavesim_gui/labels.py`): re-titled when settings change unless the user
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

## Probe (`Wavesim_AddProbe`)

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

## Snapshot (`Wavesim_AddSnapshot`)

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

## Energy / Dissipation monitors

- `WavesimType = "EnergyMonitor"` / `"DissipationMonitor"`; tree-only (no 3D).
- `RecordInterior` (Bool, default True), `RecordFullDomain` (Bool, default False);
  the panel keeps at least one ticked.
- Labels `Energy (excl. PML)` / `Dissipation (excl. PML)`, `(incl. PML)`,
  `(excl. + incl. PML)`.
- **Singletons**: the GUI warns "already exists" and returns; the API raises
  `ApiError`. Both False → `ApiError`.
- Job: `monitors.energy` / `monitors.dissipation = {full, interior}`.

## Voltage / Current monitors

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

## Field along curve (`Wavesim_AddFieldLineMonitor`, electrostatic only)

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
