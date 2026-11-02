# API inventory — geometry (`wavesim_gui/api_geometry.py`)

The workbench has **no GUI commands for geometry** — users model with the Part
(and Draft) workbench. The geometry API is therefore new code with no GUI path
to share; it creates exactly the parametric objects the Part/Draft GUI commands
create, with the same side effects (inputs of booleans, mirrors, fillets,
chamfers and arrays are hidden).

## Common rules

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

## Functions → FreeCAD objects

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

## GUI selections replaced

- Part GUI booleans/mirror/fillet use the selection → API takes object refs
  (Name or unique Label).
- Fillet/chamfer edge picking → declarative selectors
  (`{"type":"all"}`, `{"parallel_to":"z"}`, `{"at_z":1.6}`,
  `{"near_point":[x,y,z]}`, AND-combined) — never raw edge indices.

## Trade-offs / limitations

- Draft arrays work headless under FreeCADCmd (verified), so arrays are
  parametric Draft arrays; no MultiFuse fallback was needed. The document
  then needs the Draft module to recompute arrays (always shipped with
  FreeCAD).
- Fillet/chamfer sizes and polygon vertices are not parameter-bound.
- Imported STEP shapes are plain (non-parametric) `Part::Feature`s.
