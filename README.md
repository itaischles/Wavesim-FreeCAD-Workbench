# Wavesim FreeCAD Workbench

The Wavesim workbench turns FreeCAD solids into an electromagnetic simulation. You assign a
material to each body, place sources, ports and monitors, and press **Run Simulation**. The
separate Wavesim solver performs the computation, and the results return to the FreeCAD
document tree as plots and tables.

Two solvers act on the same geometry, materials and mesh. **Full wave (FDTD)** solves
Maxwell's equations in the time domain, so a single pulsed run gives the response over a
broad band: port impedances, field animations, energy and probe records. **Electrostatic**
solves for conductors held at fixed potentials or charges, and returns the potential, the
field, the conductor charges and the capacitance matrix.

The workbench runs inside FreeCAD 1.1; the solver runs in its own Python environment, with
`numpy`, `scipy` and `numba`. The two communicate through files on disk.

The [user manual](manual/wavesim-manual-v0.1.pdf) covers installation, a worked example for
each solver, every command, the mesh, materials, ports and monitors, and how to read the
results. It is written for a physicist, and explains the numerical methods as far as is
needed to judge a result.
