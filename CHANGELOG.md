# Changelog

All notable changes to the Wavesim FreeCAD workbench are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
workbench uses [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.
While the version is `0.x`, a MINOR bump may change behaviour or document formats.

Add each change under **Unreleased** as you commit it, in one of these groups:
**Added**, **Changed**, **Fixed**, **Removed**. At release time the Unreleased
section is renamed to the new version and date, and a fresh empty Unreleased
section is started above it.

## [Unreleased]

### Added

- An **Agent bridge** for AI-agent access (new *Agent Bridge On/Off* button,
  a chain-link icon next to *Settings*). When switched on, an AI agent in VS Code (through the
  MCP server of the [Wavesim-AI-Agent](https://github.com/itaischles/Wavesim-AI-Agent) repository) can build and run
  simulations in this FreeCAD window while you watch. It listens on
  127.0.0.1 only, needs a token, and can call only the scripting API below —
  it cannot run arbitrary code. The status bar shows *Agent bridge: ON* while
  it runs. *Wavesim → Settings* has a new *Agent bridge* section: start it
  with FreeCAD, change its port (default 9875), copy or regenerate the token.
- A **scripting API** (`from wavesim_gui import api`) usable from the Python
  console: create and edit simulations, materials, domain and mesh,
  geometry (Part primitives, booleans, arrays, fillets and chamfers with edge
  selectors, STEP import), sources and ports, monitors, parameter bindings to a
  `Params` spreadsheet, setup validation, previews, job export and results
  loading. Every function is one undo step. See `docs/API_INVENTORY.md` and
  `docs/AGENT_BRIDGE.md`.
- `exporter.py` next to `runner.py`: exports a finished run's results to CSV,
  PNG, GIF and Touchstone files with a `summary.md`
  (`<solver python> exporter.py <run dir> <out dir>`).
- Headless API tests: `freecadcmd tests\run_api_tests.py`.
- *Run Simulation* now runs the solver in **its own console window**, and
  FreeCAD stays usable while it runs. The window shows the solver's status
  lines and a progress bar with elapsed time, time remaining and steps per
  second. **Closing the window cancels the run**, as *Cancel* did. The
  Results tree is built when the run finishes. *Wavesim → Settings → Solver
  run* switches back to the progress dialog. Windows only; elsewhere the
  dialog is always used. Port Matrix and Compute Mode still use the dialog.
- **Refresh Results** (new button beside *Run*, and on the *Results* group's
  right-click menu) shows a running simulation's results so far: the solver
  writes what its monitors have recorded, and the Results tree is rebuilt from
  it, labelled e.g. *Results (partial: 23%, t = 1.12 of 4.8 ns)*. Snapshots
  animate up to the current time; probes, voltages, currents, energy and port
  V/I plot up to it. Impedance leaves and spectrum views need the whole record
  and appear only when the run finishes. A plot window opened from a partial
  tree says so in a banner and keeps showing what it loaded. The finished run
  replaces the partial tree and deletes the temporary files, so its folder and
  results are exactly what they would have been without a refresh. Console
  runs of time-domain simulations only.

### Changed

- The toolbar commands now create their objects through the same functions as
  the scripting API, so a scripted object and a clicked one are identical. What
  you see and click is unchanged.
- A job's `job.json` now also records `origin_m`, the world position of the
  grid origin (ignored by the solver).

- A **Field Along Curve** monitor for electrostatic runs (new button in the
  monitors group). Drag a sketch onto it, then double-click it to pick phi, E
  or D and the start and end vertices on the curve. After the run its result
  plots the field against the distance along the curve, from 0 mm at the start.
  For E or D a dropdown picks |F|, Fx/Fy/Fz, the component along the curve, and
  the perpendicular component (signed in the sketch plane, and as a magnitude).
  The plot also shows ∫E·dl for E along the curve, and ∫D·n dl for D in the
  plane.
- A **Banding** checkbox on snapshot plots cuts the colour map into a fixed
  number of flat bands. Set the number of bands under *Wavesim → Settings →
  Colour map bands* (default 12).
- Potential (φ) snapshots from an electrostatic run now have white electric
  field lines (along E = −∇φ) drawn over them, which you can turn off with the
  **Field lines** checkbox. They also get the **Vectors** arrow overlay, with
  arrows for E = −∇φ, turned on by default.
- Running a model again no longer voxelises it again when nothing that affects
  the voxels has changed: geometry, materials, mesh, background, subpixel and
  conformal settings. Changing monitors (inside the domain), run time or port
  drives now starts the solver straight away (measured: 64.5 s down to 0.4 s
  on `tori_and_sphere_ES`, 12.5 s down to 0.04 s on the reference coax). This
  also works across Run, Port Matrix and Compute
  Mode, and after closing and reopening the document. The console says when a
  voxelisation is reused. The last full-wave and the last electrostatic
  voxelisation are kept separately, in a `voxel_cache` folder next to the
  document's `run` folder. Turn it off under *Wavesim → Settings →
  Voxelisation reuse*.

### Changed

- The **Cross Section On/Off** and **Mesh Grid On/Off** buttons now stay
  usable while a task panel (Domain, a port, a monitor, ...) is open, instead
  of greying out until the panel is closed.
- The progress dialog for an electrostatic run now shows a percentage and an
  estimate of the time left, instead of an animated "busy" bar.
- Better visibility for vector overlays (on snapshots and on TEM modes).
- The `materials.npz` file a run writes is now compressed, which makes it
  roughly 100 times smaller on disk (239 MB down to 2 MB on a 2.4-million-cell
  model). Runs read it exactly as before.
- Voxelising is faster on models whose sections are slow to cut, with the
  same results to the last bit: about 20% faster on a conformal spark gap
  (160 s down to 126 s) and on the reference coax (14.4 s down to 11.0 s).
  The parallel section workers now start after timing a few sample planes
  instead of a whole serial pass, and conformal PEC no longer cuts each of its
  node planes twice.

### Fixed

- Changing a checkbox or dropdown on a snapshot or mode plot no longer resets
  the zoom: the view you zoomed or panned to is kept.
- On a snapshot plot, switching between a magnitude (|E|) and a component (Ex)
  after toggling **Smooth** no longer makes the colour bar disappear and leaves
  the plot unchanged.

## [0.1.0]

First public version.

