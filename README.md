# Wavesim FreeCAD Workbench

The Wavesim workbench turns FreeCAD solids into electromagnetic simulations.
Assign materials, configure the mesh, place sources, ports and monitors, and run
the simulation. Results appear in the FreeCAD document tree as plots and tables.

Two solvers use the same geometry, materials and mesh:

- **Full wave (FDTD)** solves Maxwell's equations in the time domain, providing
  field animations, probe records, energy measurements and port impedances.
- **Electrostatic** solves for conductors held at fixed potentials or charges,
  providing potential and field distributions, conductor charges, capacitance
  matrices and field measurements along curves.

Models can be built interactively, through a parametric
[scripting API](docs/API_INVENTORY.md), or with the
[Wavesim AI Agent](https://github.com/itaischles/Wavesim-AI-Agent) through an
optional local [Agent bridge](docs/AGENT_BRIDGE.md). Saved results can be exported
to CSV, PNG, GIF and Touchstone files.

The workbench runs inside **FreeCAD 1.1**; the separate Wavesim solver runs in its
own Python environment with `numpy`, `scipy` and `numba`. On Windows, the solver
can run in its own console while FreeCAD remains usable, with partial results
available during time-domain runs.

The [user manual](manual/wavesim-manual-v0.1.pdf) explains installation, numerical
methods and worked examples. It currently covers v0.1; see the guides above and
the [changelog](CHANGELOG.md) for later additions.
