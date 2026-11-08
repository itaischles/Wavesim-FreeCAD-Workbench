# Changelog

All notable changes to the Wavesim FreeCAD workbench are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and version numbers follow [Semantic Versioning](https://semver.org/):
MAJOR.MINOR.PATCH. While the version is 0.x, a MINOR bump may change
behaviour or document formats.

Add changes under Unreleased in the Added, Changed, Fixed or Removed groups.
At release time, move them into a dated version section and leave a fresh
Unreleased section above it.

GitHub release tags use the short form: v0.2 corresponds to version 0.2.0.

## [Unreleased]

## [0.2.0] - 2026-11-08

### Added

- A scripting API (`from wavesim_gui import api`) for simulation setup,
  materials, domain and mesh, parametric geometry, sources and ports, monitors,
  Params spreadsheet bindings, validation, previews, job export and results
  loading. Document-changing calls are undoable. See docs/API_INVENTORY.md.
- An optional Agent bridge for the Wavesim AI Agent, with an
  Agent Bridge On/Off toolbar button beside Settings. The bridge listens on
  127.0.0.1, requires a token and exposes registered scripting API operations
  rather than arbitrary Python execution. Settings include automatic startup,
  port selection and token management. See docs/AGENT_BRIDGE.md.
- A standalone results exporter for CSV, PNG, GIF and Touchstone output,
  including a summary and manifest. It supports ordinary runs and port-matrix
  results:
  `<solver-python> exporter.py <run-directory> <output-directory>`.
- Headless scripting API tests:
  `freecadcmd tests\run_api_tests.py`.
- Console-window solver execution for Run Simulation on Windows. FreeCAD
  remains usable, and the console displays status, progress, elapsed time,
  estimated remaining time and steps per second. Closing the window cancels
  the run. The progress dialog remains selectable in Settings and continues
  to be used on other platforms, for Port Matrix and for Compute Mode.
- Refresh Results, available beside the run commands and from the Results
  group's context menu, to inspect results recorded so far during console-based
  time-domain runs. Partial results and plot windows are clearly labelled.
  Impedance leaves and spectrum views are available after completion.
  Completed results replace the partial tree, and temporary files are removed.
- An electrostatic Field Along Curve monitor. Assign a sketch, select phi,
  E or D, and choose start and end vertices. Results plot the field against
  distance from the start, with Cartesian, tangential and perpendicular
  vector components and relevant line integrals.
- A Banding checkbox on snapshot plots. The number of colour-map bands is
  configurable in Settings; the default is 12.
- Electric-field lines and vector arrows on electrostatic potential snapshots,
  derived from E = -grad(phi), with controls to disable the overlays.
- Persistent voxelisation reuse when geometry, materials, mesh, background,
  subpixel and conformal settings are unchanged. The last full-wave and
  electrostatic voxelisations are cached separately and can be reused across
  Run Simulation, Port Matrix, Compute Mode and document sessions.
  Reuse can be disabled in Settings.

### Changed

- Toolbar commands create objects through the same functions as the scripting
  API, keeping scripted and interactively created objects consistent.
- Exported job.json files include origin_m, the world position of the grid
  origin. The solver ignores this metadata.
- Cross Section On/Off and Mesh Grid On/Off remain usable while task panels
  are open.
- Electrostatic progress dialogs show a percentage and estimated time
  remaining instead of an indeterminate busy indicator.
- Vector overlays on snapshots and TEM modes are easier to see.
- Material arrays in materials.npz are compressed, substantially reducing disk
  usage without changing how runs read them.
- Voxelisation is faster on models with expensive section cuts. Parallel
  workers start after a small set of sample planes is timed, and conformal
  PEC processing avoids cutting the same node planes twice. Results remain
  bit-identical to the previous sectioning path.

### Fixed

- Snapshot and mode plot checkboxes and dropdowns preserve zoom and pan.
- Switching between magnitude and component views after toggling Smooth no
  longer makes the snapshot colour bar disappear or leaves the plot unchanged.

## [0.1.0]

First public version.

[Unreleased]: https://github.com/itaischles/Wavesim-FreeCAD-Workbench/compare/v0.2...master
[0.2.0]: https://github.com/itaischles/Wavesim-FreeCAD-Workbench/compare/v0.1...v0.2
[0.1.0]: https://github.com/itaischles/Wavesim-FreeCAD-Workbench/releases/tag/v0.1
