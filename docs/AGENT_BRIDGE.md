# Agent bridge and scripting API — manual addendum

This addendum to the Wavesim manual (v0.1) covers two additions on the
`agent-bridge` branch: a **scripting API** for the Python console, and the
**Agent bridge** that lets an AI agent use that API to drive FreeCAD.

## Scripting API

Open *View → Panels → Python console* and:

```python
from wavesim_gui import api
doc = App.newDocument("coax")
sim = api.new_simulation(doc)                       # Simulation, Domain, Vacuum + PEC
api.create_cylinder(doc, label="Inner", radius=3, height=60)
api.create_cylinder(doc, label="ShieldOut", radius=8, height=60)
api.create_cylinder(doc, label="ShieldIn", radius=7, height=60)
api.boolean(doc, label="Outer", op="cut", base="ShieldOut", tools=["ShieldIn"])
api.assign_material(sim, ["Inner", "Outer"], "PEC")
api.set_run_settings(sim, max_frequency="8 GHz", max_time="2 ns")
api.set_domain(sim, boundaries={"x0": "PEC", "x1": "PEC", "y0": "PEC", "y1": "PEC"})
api.add_modal_port(sim, face="z0", conductors=["Inner"],
                   excitation={"type": "gaussian", "fmax": "8 GHz"})
api.add_probe(sim, position=[0, 5, 30], component="Ey")
print(api.validate_setup(sim))
```

* Objects are referenced by label or name; every function returns the object it
  created (or a small report) and is **one undo step**.
* Quantities: a plain number uses the documented default unit (mm, Hz, s, deg,
  ...); a string like `"2.4 GHz"` uses a case-sensitive unit; a string starting
  with `=` (`"=SUBSTRATE_H/2"`) binds the property to the `Params` spreadsheet.
* Edges and faces are picked with selectors such as `{"parallel_to": "z"}`,
  `{"at_z": 1.6}`, `{"near_point": [x, y, z]}` — never by index.
* Errors are raised as `api.ApiError` with a readable message; nothing opens a
  dialog.
* `docs/API_INVENTORY.md` lists every toolbar command and the matching function;
  `help(api.add_lumped_port)` shows units and examples.

## Agent bridge

The bridge lets the Wavesim AI agent (VS Code + the [Wavesim-AI-Agent](https://github.com/itaischles/Wavesim-AI-Agent) MCP
server) work in this FreeCAD window. You see every object appear as the agent
builds the model, and you can undo its steps.

**Switching it on.** Press *Agent Bridge On/Off* (chain-link icon) in the Wavesim toolbar (next to
*Settings*). The status bar shows **Agent bridge: ON (127.0.0.1:9875)**. Press
again to switch it off.

**Settings** (*Wavesim → Settings → Agent bridge*):

* *Start the Agent bridge when FreeCAD starts* — auto-start.
* *Port* — default 9875; change it if another program uses that port (and set
  the same `rpc_port` in the agent's config).
* *Token* — generated the first time the bridge starts. The agent on the same
  machine reads it automatically; *Copy* puts it on the clipboard, *Regenerate*
  replaces it (an agent with the old token is refused).

**What the agent can and cannot do.** The bridge accepts connections from this
computer only, and every request must carry the token. It can call only the
scripting API functions and a few document operations (new, open, save,
recompute). It cannot run arbitrary Python code or read and write files of its
own choosing.

**While the agent works.** Calls run one at a time in FreeCAD's main window.
While a job is being voxelized for a run, FreeCAD keeps responding, and the
solver itself runs in a separate console window that the agent opens. Closing
that window stops the solver.
