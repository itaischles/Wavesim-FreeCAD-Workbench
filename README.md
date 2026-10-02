# Wavesim FreeCAD Workbench

The Wavesim workbench turns FreeCAD solids into an electromagnetic simulation. You assign a
material to each body, place sources, ports and monitors, and then run the simulation. The
separate Wavesim solver performs the computation, and the results return to the FreeCAD
document tree as plots and tables.

Two solvers are currently available:
1. **Full wave (FDTD)** solves the full Maxwell's equations in the time domain.
2. **Electrostatic** solves for conductors held at fixed potentials or charges.

The workbench runs inside FreeCAD 1.1; the solver runs in its own Python environment,
with the only dependencies being `numpy`, `scipy` and `numba`. The  solver and the workbench
communicate through files on disk.

To learn all the features, and how to install the software read the [user manual](manual/wavesim-manual-v0.1.pdf).
It is written for physicists, and explains the numerical methods as far as is
needed using basic terms.
