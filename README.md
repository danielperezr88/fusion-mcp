# FusionMCP

Control Autodesk Fusion 360 with natural language through Claude. FusionMCP is an [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that gives Claude direct access to Fusion 360's CAD engine -- sketching, modeling, assemblies, export, and more.

## How It Works

```
Claude Desktop  ──stdio──▶  MCP Server (Python)  ──HTTP──▶  Fusion 360 Add-in
                             fusion_server.py                 FusionMCP.py
                             (your machine)                   (inside Fusion 360)
```

1. **MCP Server** (`mcp_server/fusion_server.py`) — Runs locally and exposes tools to Claude via the Model Context Protocol over stdio.
2. **Fusion 360 Add-in** (`FusionMCP.py`) — Runs inside Fusion 360, listens on `http://127.0.0.1:7432`, receives commands from the MCP server, and executes them on Fusion's main thread using the Fusion 360 Python API.

## Features

### Sketching
- Primitives: rectangle, center rectangle, circle, line, arc, polygon, ellipse, spline, slot, text
- Operations: fillet, offset, mirror, rectangular pattern
- Constraints: coincident, tangent, perpendicular, parallel, horizontal, vertical, concentric, equal, midpoint, fix, collinear, smooth
- Dimensions: distance, angle, diameter, radius

### 3D Modeling
- Extrude, revolve, loft, sweep, helix, pipe
- Shell, fillet, chamfer, draft, thread
- Press/pull faces, thicken sketches
- Boolean operations (join, cut, intersect)
- Move, rotate, scale (uniform and non-uniform), copy, mirror
- Rectangular and circular body patterns
- Hole features (simple, counterbore, countersink)

### Assembly
- Create components, move bodies between components
- Joints: rigid, revolute, slider, cylindrical, pin-slot, planar, ball
- As-built joints

### Inspection
- Design info (components, sketches, bodies, parameters, joints)
- Face/edge/sketch info for targeted operations
- Body measurement (bounding box, volume, face/edge/vertex counts)
- Distance measurement between entities
- Feature timeline
- `get_physical_properties` — mass, density, volume, surface area, center of mass, moments of inertia, and principal axes
- `measure_angle` — angle between two faces or edges, in degrees
- `get_oriented_bounding_box` — oriented bounding box along configurable length/width axes
- `inspect_body` — full BRep report: faces/edges/vertices, cylindrical radii, and surface normals (summary or full detail)

### Parameters & Appearance
- Create and update user parameters
- Apply material appearances from Fusion's library
- Set custom RGB colors on bodies

### Export & Capture
- STL, STEP, 3MF, F3D (Fusion archive)
- Viewport screenshot capture

### Import
- `import_cad_file` — STEP, SAT, SMT, IGES, and F3D imports (format auto-detected from extension)
- `import_mesh_file` — STL/3MF mesh import as mesh bodies (mm/cm/in units)
- `import_sketch_file` — SVG/DXF 2D imports as sketches (DXF lands on a chosen construction plane)

### OpenSCAD Pipeline
- `run_scad` — render OpenSCAD code to a mesh body using the bundled OpenSCAD + BOSL2 (via `include <BOSL2/std.scad>`); supports `-D` param overrides and `$fn` quality; stores the source as a `scad_source` body attribute for later re-runs
- `update_scad_body` — re-run a stored .scad source on a mesh body with new parameters or replacement code; the new body is renamed to keep name references valid
- `import_mesh_data` — create a mesh body directly from raw triangle data (flat coordinate/index lists; auto-generates face normals if omitted)
- `create_from_scad` — translate `.scad`/BOSL2 source into native parametric Fusion features (sketch/extrude/revolve/combine) visible in the timeline, resolving the source via the CSG translator (mm/cm/in units); falls back to a mesh body when a construct is unsupported (hull, surface, text, import, arbitrary polyhedra) or `fallback_to_mesh` is enabled

### Mesh Reconstruction
- `get_workflow_guide` — the pipeline map and entry point: returns the 8-step mesh-to-parametric workflow as JSON — import -> analyze -> slice -> annotate -> select_parameter_schema -> reconstruct -> compare -> review — with each step's tool name, purpose, inputs/outputs, the model action to take, the reconstruct step's note (DISMANTLED — agent composes with primitives), and fallbacks; pass a step name (e.g. "annotate") to get a single step
- `analyze_mesh` — analyze a mesh body and report measured facts: watertightness, manifoldness, vertex/triangle counts, enclosed volume, bounding box, mirror symmetry, primitive hints (plane regions, box, cylinder)
- `slice_mesh` — slice a mesh body with an axis-aligned plane (X/Y/Z + signed height) and return the ordered 2D cross-section loops: outer contours (CCW) and holes (CW)
- `annotate_mesh_parameters` — capture 4 viewport screenshots (isometric/front/top/right) of a mesh plus its measured facts, returned as base64 PNG image blocks for model-side classification
- `select_parameter_schema` — pure-data tool: binds measured facts (bbox dims, slice loop diameter, fit params) to stable named parameters for a classified object (20 part classes + generic fallback) with confidence scores; vision-sourced roles return as placeholders for the model to fill
- `compare_mesh_to_brep` — vision-free fidelity QA between the original mesh and the reconstructed BRep: volume ratio, per-axis bounding-box deviation, and a sampled surface deviation (mean/max)
- `review_reconstruction` — capture side-by-side mesh vs reconstructed-BRep screenshots per view, paired with a local geometry summary, for the final QA pass
- `structure_graph` — build the structure graph of a mesh body and persist it to an in-memory DuckDB database: fetches the mesh triangle data from Fusion, decomposes it into planar faces and curved patches (mesh_analysis), builds the NetworkX property graph (face/hole/curved-patch/component nodes; 10 edge relation types), runs graph algorithms (articulation points, connected components), and persists the full graph to `nodes`/`edges` tables for later SQL queries; returns a JSON summary — not the full graph — with component_count, face_count, edge_type_counts, base_face_candidates, the persisted DuckDB table schema, and a ready-to-run query example, plus a workstream section (base_face_per_component, dag_has_cycles) from the scored base-face analysis; accepts optional tolerance params — angle_tolerance_deg, offset_tol, snap_tol, simplify_vertices, and a preset ("accurate" | "balanced" | "coarse")
- `query_structure_graph` — run arbitrary read-only SQL against the persisted structure graph: queries the in-memory DuckDB database that `structure_graph` persisted for the mesh (call `structure_graph(mesh=...)` first — graphs are ephemeral and an MCP server restart drops them); only a single SELECT/WITH statement is allowed (comments stripped, multi-statement SQL rejected) and rows sort by the first column when no ORDER BY is present; supports recursive CTE queries for graph traversal (e.g. `SELECT node_id, area_cm2 FROM nodes WHERE label='Face' ORDER BY area_cm2 DESC` or a `WITH RECURSIVE` query walking connected faces); returns the result set as JSON (`{mesh, columns, rows, row_count}`) — the full graph never leaves the server

Classification happens on the **model side** — no server-side vision API calls. `annotate_mesh_parameters` and `review_reconstruction` return screenshots as MCP Image blocks (base64 PNG) via `image_base64`, and `select_parameter_schema` maps the measured facts to parameters deterministically.

Example workflow:

```text
1. import_mesh_file("bracket.stl")
2. analyze_mesh(mesh="0")                    # measured facts, primitive_hints
3. structure_graph(mesh="0", preset="accurate")  # surface relationships
4. query_structure_graph(mesh="0", sql="SELECT node_id, area_cm2 FROM nodes WHERE label='Face' ORDER BY area_cm2 DESC")
5. # Agent composes with sketch/feature/boolean MCP tools from graph insights
```

### Long-running tools & job polling

Five long-running tools accept a `job_id` parameter so heavy work (OpenSCAD rendering, screenshot capture) never blocks the caller: `run_scad`, `update_scad_body`, `create_from_scad`, `annotate_mesh_parameters`, `review_reconstruction`.

The `job_id` parameter has three modes:

- `job_id=""` (default): launches the job in the background and returns `{"job_id": "<id>", "status": "running"}` immediately
- `job_id="<id>"`: polls the job, returning `running`, `completed` (with `result`), `error` (with `error`), or `not_found`
- `job_id="sync"`: runs in the foreground and returns the full result exactly as before

```text
run_scad(code="cube([10,10,10]);")            -> {"job_id": "...", "status": "running"}     # launch
run_scad(job_id="<the job id above>")         -> {"job_id": "...", "status": "running"}     # poll
run_scad(job_id="<the job id above>")         -> {"job_id": "...", "status": "completed", "result": "{...}"}
```

Notes:
- Jobs are in-memory only and are lost when the MCP server restarts, so keep polling until a job reaches `completed` or `error`.
- The Fusion add-in command cap is 300s: each polled job runs to completion or returns its own timeout error.
- The result of a polled `run_scad` is the same JSON string the tool normally returns; `annotate_mesh_parameters` and `review_reconstruction` return their screenshots as base64 image data.

### Other
- Execute arbitrary Python scripts inside Fusion 360
- Create/clear documents
- Undo/redo
- Save/save as

## Prerequisites

- [Autodesk Fusion 360](https://www.autodesk.com/products/fusion-360) (any license tier)
- [Python 3.11+](https://www.python.org/downloads/)
- [Claude Desktop](https://claude.ai/download)

**OpenSCAD & BOSL2 bundling:** On first use, FusionMCP automatically downloads a portable OpenSCAD build and the BOSL2 library to `~/.fusion-mcp/bundle/` — no PATH configuration needed. Common BOSL2 modules (`cuboid`, `cyl`, `prismoid`, `torus`, `diff`, `edge_profile`, `xcopies`) resolve to native Fusion features via the openscad-evaluator.

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/Anonimus124/fusion-mcp.git
cd fusion-mcp
```

### 2. Install Python Dependencies

```bash
pip install -r mcp_server/requirements.txt
```

This installs:
- `mcp` — Model Context Protocol SDK
- `requests` — HTTP client for communicating with the Fusion add-in

### 3. Install the Fusion 360 Add-in

Create a folder named `FusionMCP` inside Fusion 360's add-ins directory, then copy **both** `FusionMCP.py` and `FusionMCP.manifest` into it.

**Windows:**
```
%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\FusionMCP\
```

**macOS:**
```
~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/FusionMCP/
```

The folder must be named exactly `FusionMCP` and contain both files — Fusion 360 will not recognize the add-in without the manifest.

#### Start the Add-in in Fusion 360

1. Open Fusion 360
2. Go to the **Utilities** tab (or **Tools** depending on your version)
3. Click **Add-Ins** (or press `Shift+S`)
4. In the Add-Ins dialog, go to the **Add-Ins** tab
5. Click the green **+** icon next to "My Add-Ins" and browse to the `FusionMCP` folder
6. Select **FusionMCP** and click **Run**
7. A dialog will confirm the bridge is running on port 7432

To auto-start the add-in with Fusion 360, check **Run on Startup** in the Add-Ins dialog.

### 4. Configure Claude Desktop

Open your Claude Desktop configuration file:

**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

Add the FusionMCP server:

```json
{
  "mcpServers": {
    "fusion360": {
      "command": "python",
      "args": ["C:/path/to/fusion-mcp/mcp_server/fusion_server.py"]
    }
  }
}
```

Replace the path with the actual absolute path to `fusion_server.py` on your machine. On Windows, use forward slashes or escaped backslashes in the path.

Restart Claude Desktop after saving the config.

## Usage

Once everything is set up:

1. Open Fusion 360 and make sure the FusionMCP add-in is running (check for the startup dialog)
2. Open Claude Desktop — you should see a hammer icon indicating MCP tools are available
3. Start asking Claude to create things in Fusion 360

### Example Prompts

```
Create a box that's 10cm x 5cm x 3cm
```

```
Draw a circle with radius 2cm on the XY plane and extrude it 5cm
```

```
Add 2mm fillets to all edges of the first body
```

```
Create a hollow cylinder: outer radius 3cm, wall thickness 0.5cm, height 8cm
```

```
Export the current design as STL to my desktop
```

```
Create a gear-like shape: draw a 12-sided polygon and extrude it, then add
a center hole with diameter 1cm
```

### Tips

- All dimensions in FusionMCP use **centimeters** (Fusion 360's internal unit)
- Angles are in **degrees** in tool parameters (converted to radians internally)
- Use `get_design_info` to see what's currently in your design
- Use `get_face_info` or `get_edge_info` before operations that target specific faces/edges
- The `execute_script` tool lets Claude run arbitrary Fusion 360 Python API code for anything not covered by the built-in tools

## Troubleshooting

**"Cannot reach Fusion 360"**
- Make sure Fusion 360 is open
- Check that the FusionMCP add-in is running (Utilities > Add-Ins)
- Verify nothing else is using port 7432

**Claude Desktop doesn't show MCP tools**
- Verify your `claude_desktop_config.json` syntax is valid JSON
- Make sure the path to `fusion_server.py` is correct and absolute
- Restart Claude Desktop after editing the config
- Check that `python` is in your PATH (try `python --version` in a terminal)

**Add-in fails to start in Fusion 360**
- Check the Fusion 360 text commands window for error details (View > Text Commands)
- Make sure the file is named `FusionMCP.py` inside a folder named `FusionMCP`

## License

MIT
