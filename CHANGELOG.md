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

### Changed

- The **Cross Section On/Off** and **Mesh Grid On/Off** buttons now stay
  usable while a task panel (Domain, a port, a monitor, ...) is open, instead
  of greying out until the panel is closed.
- The progress dialog for an electrostatic run now shows a percentage and an
  estimate of the time left, instead of an animated "busy" bar.
- Better visibility for vector overlays (on snapshots and on TEM modes).

### Fixed

- Changing a checkbox or dropdown on a snapshot or mode plot no longer resets
  the zoom: the view you zoomed or panned to is kept.
- On a snapshot plot, switching between a magnitude (|E|) and a component (Ex)
  after toggling **Smooth** no longer makes the colour bar disappear and leaves
  the plot unchanged.

## [0.1.0]

First public version.

