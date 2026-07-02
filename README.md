# Wavesim FreeCAD Workbench

A FreeCAD workbench for setting up and running FDTD electromagnetic simulations
directly from CAD geometry. It lets you assign materials to solid bodies, define
the simulation domain and boundaries, place sources and monitors, and run the
simulation through the external Wavesim solver — which executes out-of-process so
the heavy numerics never block FreeCAD.
Results (energy, field probes, and animated field snapshots) are loaded back into
the document tree for inspection. More detailed documentation will follow as the
project matures.
