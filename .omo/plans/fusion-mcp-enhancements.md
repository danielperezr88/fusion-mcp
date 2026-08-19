# fusion-mcp-enhancements - Work Plan

## TL;DR (For humans)

**What you'll get:** Three major capability sets added to your Fusion 360 MCP server: (1) full physical properties and advanced dimensioning, (2) file import for CAD formats (STEP/F3D/SAT/IGES) + mesh formats (STL/3MF) + 2D formats (SVG/DXF), and (3) a complete OpenSCAD pipeline with bundled OpenSCAD+BOSL2 that renders `.scad` code to mesh bodies AND can translate CSG operations into native parametric Fusion timeline features — **including BOSL2 user-defined modules** (cuboid, prismoid, cyl, torus, diff, edge_profile, etc.). Plus MIT license and test infrastructure.

**Why this approach:** All API capabilities were verified live against your running Fusion 360 instance. The OpenSCAD pipeline has two tiers — mesh import (fast, covers any `.scad` including complex BOSL2 designs like the Kumiko projects) and CSG-to-timeline (creates editable parametric features from the resolved CSG tree). The CSG-to-timeline uses two MIT-licensed libraries by the BOSL2 author himself: `openscad-lalr-parser` (AST parsing) and `openscad-evaluator` (which resolves BOSL2 module calls in-process using `manifold3d`, producing a clean CSG tree with concrete float params and no `group()` noise). This was empirically verified: `cuboid([10,10,10])` resolves to native `cube()`, `cyl(l=30,d=10)` resolves to native `cylinder()`, `diff()+edge_profile+mask2d_roundover` resolves to `difference() { cube; 4× linear_extrude(polygon) }`. An OpenSCAD-binary CSG export path (`openscad -o out.csg`) is kept as a fallback when the evaluator is unavailable.

**What it will NOT do:** No 2D drawing generation (Fusion API classes exist but methods are unimplemented — `RuntimeError: API Function not yet implemented` confirmed on builds through 2703.x Insider). No mock mode. No feature recognition on imported parts. No Streamable HTTP transport migration.

**Effort:** ~26 todos across 6 waves. Architecture-scale.

**Risk:** CSG-to-timeline translation handles all BOSL2 modules via `openscad-evaluator` (which resolves them to primitives in-process), but some primitives remain untranslatable to parametric features: `hull` (no Fusion API equivalent for arbitrary hull), `surface` (image-based), `text` (typography), `import` (external mesh), and `minkowski` with non-standard operand pairs — those fall back to mesh import via `run_scad`. `polyhedron` with arbitrary vertex topology (Bezier surfaces, VNF from BOSL2's `rounded_prism`/`spheroid`/`dovetail`) falls back to mesh import via `meshBodies.addByTriangleMeshData`. `importToTarget` may require a saved document for some formats — handled with save-first logic. OpenSCAD subprocess render may exceed the 30s MCP HTTP timeout for complex BOSL2 models — handled with configurable timeout + async fallback. Python 3.11+ required (openscad-lalr-parser + openscad-evaluator dependency).

**Decisions I made for you:**
- A1: Centimeters remain the default unit (matches existing 78 tools)
- A2: Bundle OpenSCAD + BOSL2 binaries/libs with the MCP package (no PATH dependency)
- A3: FreeCAD NOT required (documented as optional for STEP-via-CSG path)
- A4: Imports go into root component by default; optional `as_component` param
- A5: Two-tier parametric: mesh-path stores `.scad` source as body metadata; CSG-to-timeline creates native Fusion features
- A6: Add MIT LICENSE file to project
- A7: Study MIT-licensed code only (fscad, openscad-parser, FusionMCPSample, bberrevoets)
- A8: pytest suite for pure-logic + live QA for integration
- A9: Drawing generation excluded (API not implemented)
- A10: SVG added as 2D input path
- A11: Use `openscad-evaluator` (MIT, by BOSL2 author Revar Desmera) as the primary CSG resolution path — it resolves BOSL2 modules in-process via `manifold3d`, producing a clean CSG tree (`Evaluator.csg_tree`) with concrete float params and no `group()` noise. Keep OpenSCAD-binary CSG export (`openscad -o out.csg`) as fallback when evaluator is unavailable. Do NOT implement a BOSL2 interpreter ourselves.
- A12: Add `polyhedron`, `minkowski`, `mirror`, and `resize` to the CSG translator's supported primitive set (empirically required by BOSL2 expansions — `prismoid` → `polyhedron`, rounded cuboid → `minkowski`, masks → `mirror`)

## Scope

### In scope
- **Component 1 — Dimensioning & Inspection:** `get_physical_properties` (mass, CoM, moments of inertia, principal axes, density, area), `measure_angle`, `get_oriented_bounding_box`, `inspect_body` (full geometry report with face/edge/vertex details, cylindrical radii, surface normals)
- **Component 2 — File Import Tools:** `import_cad_file` (STEP/SAT/SMT/IGES/F3D via `app.importManager`), `import_mesh_file` (STL/OBJ/3MF via `meshBodies.add`), `import_sketch_file` (SVG/DXF via `importManager`)
- **Component 3 — OpenSCAD Pipeline (Mesh Path):** `run_scad` (bundled OpenSCAD+BOSL2 → render to STL/3MF → `meshBodies.add`), `update_scad_body` (re-run with changed parameters), `import_mesh_data` (raw triangle data via `addByTriangleMeshData`)
- **Component 4 — CSG-to-Timeline Translation:** `create_from_scad` (resolve `.scad` via `openscad-evaluator` which inlines all BOSL2 modules in-process → walk resolved CSG tree → emit native Fusion parametric features: extrude, combine, move, rotate, scale, loft, revolve, fillet). Handles primitives (cube, cylinder, sphere, polyhedron), transforms (translate, rotate, scale, mirror, resize, multmatrix), booleans (union, difference, intersection, minkowski-for-rounding), 2D→3D (linear_extrude, rotate_extrude). **BOSL2 modules are fully supported** — `cuboid()` → Box, `cyl()` → Cylinder, `prismoid()` → Loft, `torus()` → Revolve, `diff()+edge_profile+mask2d_roundover()` → Box + Chamfer/Fillet, `xcopies()` → multiple instances. Falls back to mesh import only for: `hull` (no Fusion API equivalent), `surface` (image-based), `text` (typography), `import` (external mesh), `minkowski` with non-standard operands, and `polyhedron` with arbitrary VNF topology (Bezier surfaces from `rounded_prism`/`spheroid`/`dovetail`).
- **Component 5 — Project Hygiene:** MIT LICENSE file, OpenSCAD+BOSL2 bundling system (auto-download/extract portable build), pytest test infrastructure

### Must-NOT-Have (scope out)
- 2D Drawing generation (DrawingDocument/DrawingSheet/DrawingView/DrawingDimension API methods are unimplemented)
- Feature recognition on imported parts (no API exists)
- Mock mode / test doubles for Fusion API
- Streamable HTTP transport migration
- CAM/manufacturing workspace integration
- OpenSCAD → parametric features for constructs with no Fusion API equivalent (hull, surface, text, import — these fall back to mesh)
- Writing a BOSL2 interpreter ourselves (we use `openscad-evaluator` instead)
- AuraFriday/Autodesk built-in code reuse (proprietary)

## Verification strategy

Every todo carries agent-executable acceptance criteria with:
- **Happy path:** Exact tool invocation + expected JSON output shape, verified against running Fusion 360
- **Failure path:** Expected error message/behavior for invalid inputs
- **Evidence:** Screenshots, `get_design_info` dumps, or exported file existence checks

Integration tests run live against Fusion 360 (bridge verified working on port 7432). Pure-logic tests (AST parsing, unit conversion, mesh data formatting, bundle path resolution) use `pytest` without Fusion.

**Kumiko validation:** The Kumiko `.scad` files at `C:\Users\danie\Downloads\KumikoPatreon\Source Code\Source Code\` serve as integration test cases for the OpenSCAD pipeline — they exercise BOSL2 includes, SVG import, complex parametric modules, and multi-mode output.

## Execution strategy

**Wave 1 (Foundation):** MIT license + bundling infrastructure + dimensioning tools
**Wave 2 (Import):** File import tools (CAD + mesh + 2D)
**Wave 3 (OpenSCAD Mesh Pipeline):** `run_scad` + `update_scad_body` + `import_mesh_data`
**Wave 4 (CSG-to-Timeline):** CSG tree resolution + Fusion feature emission. The primary resolution path uses `openscad-evaluator` (Todo 13), which inlines all BOSL2 modules in-process via `manifold3d` and exposes `Evaluator.csg_tree` — a clean tree of builtin primitives with concrete float params. The fallback resolution path uses OpenSCAD's own CSG export (`openscad -o out.csg`) when the evaluator is unavailable. Both paths depend on `openscad-lalr-parser` + `openscad-evaluator` pip packages (Todo 21). Mesh fallback in Todo 14 depends on Todo 10 (`run_scad`), which depends on Todo 2 (bundling).
**Wave 5 (Integration):** End-to-end Kumiko validation + pytest suite
**Wave 6 (Final Verification):** F1-F4 verification wave

Dependencies: Wave 2 depends on Wave 1 (import tools use dimensioning for post-import inspection). Wave 3 depends on Wave 2 (mesh import) and Wave 1 (bundling). Wave 4 translator (Todo 13) depends on Todo 21 (requirements: `openscad-lalr-parser` + `openscad-evaluator` + `manifold3d`) and Todo 2 (bundling — for CSG-export fallback path only). Wave 4 tool (Todo 14) depends on Todo 13 (translator) and Todo 10 (`run_scad` mesh fallback). Wave 4 2D primitives (Todo 15) depends on Todo 13. Wave 4 transforms (Todo 16) depends on Todo 13. Wave 5 depends on all prior waves.

**License note (E2):** Reference repos (fscad, openscad-parser, KeithSloan/OpenSCAD_Workbench) are studied for ALGORITHMIC PATTERNS ONLY — no code is copied. KeithSloan/OpenSCAD_Workbench is GPL-2 (incompatible with direct code reuse); fscad, openscad-lalr-parser, and openscad-evaluator are all MIT. The translator is implemented from scratch. `openscad-evaluator` and `openscad-lalr-parser` are both by Revar Desmera (the BOSL2 author) — using them is the canonical, author-endorsed path for BOSL2 resolution. `manifold3d` is Apache-2.0.

## Todos

### Wave 1 — Foundation: License, Bundling, Dimensioning

- [x] 1. Add MIT LICENSE file and update project metadata
  **References:** `C:\Users\danie\fusion-mcp\README.md` (claims MIT), `.gitignore`
  **Acceptance:** `LICENSE` file exists at repo root with MIT text (Copyright (c) 2026 Anonimus124). `git status` shows new untracked file. README "License" section already says MIT — no change needed there.
  **QA happy:** Read LICENSE file, confirm it contains "Permission is hereby granted, free of charge" and "MIT License" in first 5 lines.
  **QA failure:** N/A (static file).
  **Commit:** `docs: add MIT LICENSE file`

- [x] 2. Create OpenSCAD+BOSL2 bundling system in `mcp_server/bundle.py`
  **References:** `mcp_server/fusion_server.py` (add import), BOSL2 repo: `https://github.com/BelfrySCAD/BOSL2/archive/refs/heads/main.zip`. Platform-specific OpenSCAD downloads: Windows `.zip` from `https://files.openscad.org/` (latest x86-64 zip), macOS `.dmg` from `https://files.openscad.org/` (latest arm64/intel dmg), Linux AppImage from `https://files.openscad.org/`. Use `sys.platform` for platform detection (`win32`→Windows zip, `darwin`→macOS dmg mount+copy `OpenSCAD.app/Contents/MacOS/OpenSCAD`, `linux`→AppImage). FusionMCP.manifest declares `"supportedOS": "windows|mac"` so both must be supported.
  **Acceptance:** New file `mcp_server/bundle.py` exports `get_openscad_path() -> str` that returns absolute path to bundled OpenSCAD executable (`openscad.com` on Windows, `OpenSCAD` binary inside `.app` on macOS, AppImage path on Linux), auto-downloading on first run to `~/.fusion-mcp/bundle/openscad/`. Also exports `get_bosl2_path() -> str` returning path to BOSL2 library directory at `~/.fusion-mcp/bundle/BOSL2/`. Platform detection via `sys.platform`. macOS extraction: mount DMG via `hdiutil attach`, copy `OpenSCAD.app` to bundle dir, `hdiutil detach`. `get_openscad_path()` raises `FileNotFoundError` with platform-specific install instructions if download fails.
  **QA happy:** On Windows: call `get_openscad_path()`, verify it returns a string ending in `openscad.com`. Call `get_bosl2_path()`, verify it returns a string ending in `BOSL2`. Verify `BOSL2/std.scad` exists at that path.
  **QA failure:** Delete the bundle dir, call `get_openscad_path()`, verify it auto-downloads. Simulate network failure (disconnect), verify clear error message with manual install instructions.
  **Commit:** `feat: add OpenSCAD+BOSL2 bundling system`

- [x] 3. Add `get_physical_properties` tool — full mass/CoM/inertia/density/area extraction
  **References:** `FusionMCP.py` lines 302-322 (`_measure_body` existing implementation for reference), `mcp_server/fusion_server.py` (add new `@mcp.tool()` function). API: `body.physicalProperties` (`.volume`, `.area`, `.mass`, `.density`, `.centerOfMass`, `.getXYZMomentsOfInertia()`, `.getPrincipalAxes()`)
  **Acceptance:** New tool `get_physical_properties(body="0")` registered in both `FusionMCP.py` dispatcher (`_get_physical_properties`) and `mcp_server/fusion_server.py` (`@mcp.tool()`). Returns JSON: `{"body": str, "volume_cm3": float, "area_cm2": float, "mass_kg": float, "density_kg_cm3": float, "center_of_mass": [x,y,z], "moments_of_inertia": {"Ixx": float, "Iyy": float, "Izz": float, "Ixy": float, "Iyz": float, "Ixz": float}, "principal_axes": {"I1": float, "I2": float, "I3": float, "axis1": [x,y,z], "axis2": [x,y,z], "axis3": [x,y,z]}}`. Uses `_find_body()` helper. All numeric values rounded to 6 decimal places.
  **QA happy:** Create a 5×3×2cm box (extrude a 5×3 rectangle by 2cm). Call `get_physical_properties(body="0")`. Verify volume ≈ 30.0 cm³, area ≈ 62.0 cm², mass > 0, center_of_mass ≈ [2.5, 1.5, 1.0].
  **QA failure:** Call `get_physical_properties(body="999")`. Verify error: "Body '999' not found" or similar.
  **Commit:** `feat: add get_physical_properties tool with mass/CoM/inertia`

- [x] 4. Add `measure_angle` tool — angle between faces/edges
  **References:** `FusionMCP.py` (new `_measure_angle` function), `mcp_server/fusion_server.py` (new tool). API: `app.measureManager.measureAngle(entity1, entity2)` returns `MeasureResults` with `.value` in radians.
  **Acceptance:** New tool `measure_angle(entity1: str = "", entity2: str = "")` using the same entity specifier format as existing `measure_between` (`body:0:face:1`). Returns `{"angle_deg": float, "angle_rad": float}`. Converts radians to degrees for display.
  **QA happy:** Create a box. Call `measure_angle("body:0:face:0", "body:0:face:1")`. Verify angle is 90.0 degrees (adjacent faces of a box).
  **QA failure:** Call `measure_angle("body:0", "body:0")`. Verify graceful error (body-level entities may not work for angle measurement — return error message suggesting faces/edges).
  **Commit:** `feat: add measure_angle tool`

- [x] 5. Add `get_oriented_bounding_box` tool
  **References:** API: `app.measureManager.getOrientedBoundingBox(geometry, lengthVector, widthVector)` → `OrientedBoundingBox3D` with `.centerPoint`, `.length`, `.width`, `.height`. **Live verification evidence:** During planning research, calling `app.measureManager.getOrientedBoundingBox(body)` returned `RuntimeError: missing 2 required positional arguments: 'lengthVector' and 'widthVector'` — confirming the method EXISTS and requires 3 arguments (geometry + 2 vectors). Length/width vectors derived from axis name (X/Y/Z → `adsk.core.Vector3D.create(1,0,0)` etc.).
  **Acceptance:** New tool `get_oriented_bounding_box(body: str = "0", length_axis: str = "X", width_axis: str = "Y")` returns `{"body": str, "center": [x,y,z], "length_cm": float, "width_cm": float, "height_cm": float}`. Calls `app.measureManager.getOrientedBoundingBox(body, lengthVec, widthVec)` with vectors constructed from axis names.
  **QA happy:** Create a box. Call `get_oriented_bounding_box("0", "X", "Y")`. Verify length, width, height match the box dimensions.
  **QA failure:** Call with invalid axis string "W". Verify error: "Axis must be X, Y, or Z".
  **Commit:** `feat: add get_oriented_bounding_box tool`

- [x] 6. Add `inspect_body` tool — comprehensive geometry report with BRep traversal
  **References:** `FusionMCP.py` existing `_get_face_info` and `_get_edge_info` for pattern. API: `face.geometry` (`.classType()` for surface type, Cylinder `.radius`), `face.evaluator.getNormalAtParameter()`, `edge.evaluator`, `vertex.geometry`
  **Acceptance:** New tool `inspect_body(body: str = "0", detail: str = "summary", max_items: int = 100)` returns a JSON object with: `{"body": str, "bounding_box": {...}, "physical_properties": {...}, "faces": [...], "edges": [...], "vertices": [...]}`. For cylindrical faces, `geometry_params` includes `radius_cm`. For circular edges, includes `radius_cm`. `detail="summary"` returns counts + bounding box + physical properties only; `detail="full"` returns faces/edges/vertices up to `max_items` (default 100) with a `truncated: bool` flag if counts exceeded limit. If `max_items` is exceeded, response includes `total_faces`, `total_edges`, `total_vertices` counts.
  **QA happy:** Create a cylinder (extrude a circle). Call `inspect_body(body="0", detail="full")`. Verify faces include cylindrical faces with `radius_cm` matching the circle radius. Verify edge types include `Circle3D`.
  **QA failure:** Call `inspect_body(body="nonexistent")`. Verify error. Call `inspect_body(body="0", detail="full", max_items=1)` on a body with >1 face — verify `"truncated": true` in response.
  **Commit:** `feat: add inspect_body tool with full BRep geometry traversal`

### Wave 2 — File Import Tools

- [x] 7. Add `import_cad_file` tool — STEP/SAT/SMT/IGES/F3D import via ImportManager
  **References:** `FusionMCP.py` (new `_import_cad_file` function). API: `app.importManager.createSTEPImportOptions(path)`, `.createSATImportOptions(path)`, `.createSMTImportOptions(path)`, `.createIGESImportOptions(path)`, `.createFusionArchiveImportOptions(path)`, `.importToTarget(options, rootComponent)`. Pattern reference: `MapleLeafMakers/VoronConstruct360/plugin/util.py` (MIT-adjacent, create_import_options function).
  **Acceptance:** New tool `import_cad_file(path: str, format: str = "", as_component: bool = False)` in both files. Auto-detects format from file extension if `format` is empty. Maps: step/stp→createSTEPImportOptions, sat→createSATImportOptions, smt→createSMTImportOptions, igs/iges→createIGESImportOptions, f3d→createFusionArchiveImportOptions. If document has never been saved and importToTarget fails with save-related error, attempt `app.activeDocument.saveAs()` first, then retry. Returns `{"imported": str, "format": str, "bodies_added": int, "path": str}`.
  **QA happy:** Export current design to STEP (via existing `export_as_step`). Clear design. Import the STEP file back. Verify bodies count > 0.
  **QA failure:** Call `import_cad_file(path="/nonexistent/file.step")`. Verify error: file not found.
  **Commit:** `feat: add import_cad_file tool for STEP/SAT/SMT/IGES/F3D`

- [x] 8. Add `import_mesh_file` tool — STL/3MF mesh import
  **References:** API: `root.meshBodies.add(filepath, units, baseFeature)`. `adsk.fusion.MeshUnits.MillimeterMeshUnit` / `CentimeterMeshUnit` / `InchMeshUnit`. In parametric mode, must wrap in `baseFeatures.add()` → `.startEdit()` → `.add()` → `.finishEdit()`. NOTE: OBJ format is NOT supported by `meshBodies.add()` (existing `export_obj` in `FusionMCP.py` line 1577 already returns error for OBJ; import is the same API surface). Only STL and 3MF are supported.
  **Acceptance:** New tool `import_mesh_file(path: str, units: str = "mm", as_component: bool = False)`. Units param: mm/cm/in → corresponding MeshUnits enum. Detects parametric vs direct mode. In parametric: creates baseFeature, starts edit, adds mesh, finishes edit. Returns `{"imported_mesh": str, "mesh_bodies": int, "path": str}`. Supported extensions: `.stl`, `.3mf`. Unsupported extensions (`.obj`, `.amf`, etc.) return clear error.
  **QA happy:** Create a test STL file (write a simple ASCII STL with known geometry). Call `import_mesh_file(path=...)`. Verify `meshBodies.count` increases by 1.
  **QA failure:** Call with `.obj` file. Verify error: "OBJ format is not supported. Use STL or 3MF." Call with invalid path. Verify error.
  **Commit:** `feat: add import_mesh_file tool for STL/3MF`

- [x] 9. Add `import_sketch_file` tool — SVG/DXF 2D import
  **References:** API: `app.importManager.createSVGImportOptions(path)` and `.createDXF2DImportOptions(path, plane)`. SVG imports as sketch curves; DXF imports as sketch with dimensions.
  **Acceptance:** New tool `import_sketch_file(path: str, format: str = "", plane: str = "XY")`. Auto-detects format from extension. For DXF, the `plane` param selects which construction plane to import onto. Returns `{"imported": str, "format": str, "sketches_added": int, "path": str}`.
  **QA happy:** Create a simple SVG file with a rectangle path. Call `import_sketch_file(path=...)`. Verify a new sketch appears in `get_design_info`.
  **QA failure:** Call with nonexistent file. Verify error. Call with `.pdf` extension. Verify "Unsupported format" error.
  **Commit:** `feat: add import_sketch_file tool for SVG/DXF`

### Wave 3 — OpenSCAD Pipeline (Mesh Path)

- [x] 10. Add `run_scad` tool — render .scad code to mesh body via bundled OpenSCAD
  **References:** `mcp_server/bundle.py` (from todo 2), `FusionMCP.py` (new `_run_scad`). Pipeline: (1) Write user's `.scad` code string to temp file with BOSL2 include path set via `OPENSCADPATH` env var. (2) Call `subprocess.run([openscad_path, "-o", output_stl, input_scad], env={...OPENSCADPATH: bosl2_path}, timeout=300, capture_output=True)`. Timeout is 300 seconds (5 min) for complex BOSL2 renders. If subprocess times out, kills process and returns error. (3) Import resulting STL via `meshBodies.add()`. (4) Store `.scad` source as body attribute via `body.attributes.add("scad_source", code)`. **HTTP timeout fix (B4):** The MCP server's `_call()` in `fusion_server.py` line 23 has `timeout=30`. Add an optional `timeout` parameter: `def _call(command: str, params: dict = None, timeout: int = 30) -> str:` and pass `timeout=30` to the existing `requests.post(...)` call. For `run_scad` and `update_scad_body`, the `fusion_server.py` tool wrappers must call `_call("run_scad", {...}, timeout=330)`. All other tools keep `timeout=30` (default). This is a per-call override, NOT a blanket increase — connection failures for normal tools still time out at 30s.
  **Acceptance:** New tool `run_scad(code: str, params: str = "", quality: int = 100, units: str = "mm")`. `code` is raw OpenSCAD source. `params` is optional `-D` variable overrides (e.g., `"Grid_Pitch=50;Frame_Depth=12"`). `quality` sets `$fn`. Writes to temp dir, renders via bundled OpenSCAD CLI with BOSL2 path in `OPENSCADPATH`. `subprocess.run(timeout=300)`. On timeout: kills process, cleans temp files, returns `{"error": "OpenSCAD render timed out after 300s. Model may be too complex."}`. Imports STL result as mesh body. Stores source code as body attribute `scad_source`. Returns `{"rendered": True, "mesh_body": str, "scad_source_stored": True, "render_time_s": float}`.
  **QA happy:** Call `run_scad(code="cube([10, 10, 10]);")`. Verify a mesh body appears with bounding box approximately 10×10×10mm. Call `run_scad` with a BOSL2 snippet `code="include <BOSL2/std.scad>\ncuboid([10,10,10]);"`. Verify it renders (BOSL2 include resolves).
  **QA failure:** Call `run_scad(code="invalid syntax {{{")`. Verify error includes OpenSCAD's parse error message from stderr. Call with no OpenSCAD bundled and network down — verify clear error with install instructions. Call with a model that takes >300s — verify timeout error.
  **Commit:** `feat: add run_scad tool with bundled OpenSCAD+BOSL2`

- [x] 11. Add `update_scad_body` tool — re-run stored .scad with new parameters
  **References:** Depends on todo 10 (`run_scad`). Body attribute `scad_source` stored on the mesh body. API: `body.attributes.itemByName("scad_source")`.
  **Acceptance:** New tool `update_scad_body(body: str = "0", params: str = "", code: str = "")`. Reads stored `.scad` from body attribute. If `code` is provided, overrides the stored source. Deletes old mesh body, re-renders with new params, creates new mesh body. **Body index shift:** After deletion, the new body is appended as the last index. The tool returns the NEW body's name (not index) and the new index, with a note: `"index_shift_warning": "Body indices may have shifted after update. Use body name for future references."` Returns `{"updated": True, "mesh_body": str, "mesh_body_name": str, "params_used": str, "index_shift_warning": str}`.
  **QA happy:** Create body via `run_scad(code="cube([10,10,10]);")`. Note the body name. Call `update_scad_body(body="0", code="cube([20,20,20]);")`. Verify new bounding box is approximately 20×20×20mm. Verify `mesh_body_name` in response matches the original body name.
  **QA failure:** Call `update_scad_body(body="0")` on a body that has no `scad_source` attribute. Verify error: "Body does not have stored SCAD source."
  **Commit:** `feat: add update_scad_body tool for parametric re-run`

- [x] 12. Add `import_mesh_data` tool — direct mesh creation from triangle data
  **References:** `FusionMCP.py` (new `_import_mesh_data`). API: `root.meshBodies.addByTriangleMeshData(coordinates: list[float], coordinateIndexList: list[int], normalVectors: list[float], normalIndexList: list[int])`. Verified working in live testing.
  **Acceptance:** New tool `import_mesh_data(coordinates: list, triangle_indices: list, normals: list = [], normal_indices: list = [], name: str = "")`. `coordinates` is flat list `[x0,y0,z0,x1,y1,z1,...]`. `triangle_indices` is flat list of vertex indices `[v0,v1,v2,v0,v1,v2,...]`. If normals omitted, generates face normals. Creates mesh body. Returns `{"created_mesh": str, "vertex_count": int, "triangle_count": int}`.
  **QA happy:** Call `import_mesh_data(coordinates=[0,0,0, 2,0,0, 0,2,0, 1,1,2], triangle_indices=[0,1,2, 0,1,3, 1,2,3, 0,2,3])`. Verify a tetrahedron mesh body appears with 4 triangles.
  **QA failure:** Call with mismatched index count (not divisible by 3). Verify error. Call with empty coordinates. Verify error.
  **Commit:** `feat: add import_mesh_data tool for direct mesh creation`

### Wave 4 — CSG-to-Timeline Translation

- [x] 13. Add `openscad-lalr-parser` + `openscad-evaluator` dependencies and create CSG translator module in `mcp_server/scad_translator.py`
  **References:** `openscad-lalr-parser` on PyPI (MIT, by Revar Desmera — the BOSL2 author, requires Python ≥3.11). `openscad-evaluator` on PyPI (MIT, same author, depends on `manifold3d`). Imports: `from openscad_lalr_parser import getASTfromFile, getASTfromString, build_scopes` and `from openscad_evaluator import Evaluator`. **BOSL2 resolution (primary path):** Write user's `.scad` code to a temp file, call `getASTfromFile(tempfile)` (which processes `include`/`use` statements), then `Evaluator().evaluate(ast, build_scopes(ast))` — this inlines ALL BOSL2 modules in-process via manifold3d, producing `Evaluator.csg_tree`: a clean tree of builtin CSG nodes (no `group()` noise, no identity-multmatrix, all params resolved to concrete floats). Walk `csg_tree` to emit Fusion features. **BOSL2 resolution (fallback path):** If `openscad-evaluator` import fails or evaluation raises, fall back to OpenSCAD-binary CSG export: `subprocess.run([openscad_path, "-o", out.csg, in.scad], env={OPENSCADPATH: bosl2_path})` → parse `.csg` with `getASTfromFile(out.csg)` → strip `group()` nodes + collapse identity `multmatrix` → walk remaining tree. **CSG tree node types (from evaluator, empirically verified):** `cube`, `cylinder`, `sphere`, `polyhedron`, `circle`, `square`, `polygon`, `text`, `translate`, `rotate`, `scale`, `mirror`, `resize`, `multmatrix`, `color`, `hull`, `minkowski`, `offset`, `projection`, `union`, `difference`, `intersection`, `linear_extrude`, `rotate_extrude`, `render`, `surface`, `import`. Each node has `.kind` (builtin name), `.params` (dict of concrete floats/lists), `.children` (list of child nodes). **Unit conversion:** OpenSCAD is unitless (users think in mm). Fusion's internal unit is cm. The translator MUST apply a conversion factor of 0.1 (mm→cm) to ALL dimension values extracted from the CSG tree before passing to Fusion APIs. Add a `units: str = "mm"` parameter to `translate_to_fusion_commands()` that controls the scale factor (mm→0.1, cm→1.0, in→2.54). **Coordinate mapping:** OpenSCAD and Fusion are both Z-up, so no axis swap needed. `linear_extrude` extrudes a 2D shape (XY plane) along +Z → sketch on XY + extrude. `rotate_extrude` rotates a 2D shape (XZ plane) around Z → sketch on XZ + revolve around Z construction axis. **`$fn`/`$fa`/`fs` handling:** The evaluator pre-resolves `$fn` into concrete side counts via `segs()`. Drop `$fn` for primitives (Fusion cylinders/spheres are exact — better geometry than faceted). For 2D `circle()`/`polygon` approximation, replicate `segs(r)` in Python to match BOSL2 vertex counts, or convert to native Fusion circles/arcs (preferred — exact geometry). **`segs(r)` formula:** `min(ceil(360/$fa), max(3, ceil(2*pi*r/$fs)))` when `$fn=0`, else `$fn`.
  **Acceptance:** New file `mcp_server/scad_translator.py` exports `resolve_scad(code: str, openscad_path: str = None, bosl2_path: str = None) -> list` (returns CSG tree nodes from evaluator or CSG-export fallback) and `translate_to_fusion_commands(csg_nodes, root, design, units="mm")`. Supported primitive handlers: `cube` → creates sketch rectangle + `extrudeFeatures`, `cylinder` → creates sketch circle + `extrudeFeatures`, `sphere` → creates sketch circle + `revolveFeatures` (half-circle profile revolved 360°), `polyhedron` → if 2-path loftable structure → `loftFeatures`; else `meshBodies.addByTriangleMeshData` from triangulated verts (creates base feature with timeline entry), `translate` → `moveFeatures.createInput2()` + `.defineAsFreeMove()` with translation matrix, `rotate` → `moveFeatures.createInput2()` + `.defineAsFreeMove()` with rotation matrix via `Matrix3D.setToRotation()` (same pattern as existing `_rotate_body` at FusionMCP.py:1050 — there is NO `rotateFeatures` in the API), `scale` → `scaleFeatures`, `mirror` → `mirrorFeatures` (or `moveFeatures` with mirror matrix), `resize` → `scaleFeatures` with non-uniform scale, `multmatrix` → decompose 4×4 into translation+rotation+scale, apply via `moveFeatures` + `scaleFeatures`, `union` → `combineFeatures(JoinBooleanType)`, `difference` → `combineFeatures(CutBooleanType)` (children[0] is target, children[1:] are tools), `intersection` → `combineFeatures(IntersectBooleanType)`, `minkowski` → special-case: `cube⊕sphere` → Box + `filletEdges` (radius=sphere r), `cylinder⊕sphere` → Cylinder + `filletEdges` on end edges; else raise `UnsupportedSCADNodeError("minkowski")`, `hull` → raise `UnsupportedSCADNodeError("hull")` (no Fusion API for arbitrary hull). 2D primitives (`polygon`, `circle`, `square`) are handled in Todo 15, NOT this todo. **Body tracking:** The translator MUST maintain a list of all body names it creates, returned as `created_body_names`, for cleanup-on-fallback (see Todo 14).
  **QA happy:** Resolve `"cube([10, 10, 10]);"` — verify CSG tree contains a `cube` node with `params={'size': [10.0, 10.0, 10.0], 'center': False}`. Resolve `"include <BOSL2/std.scad>\ncuboid([10,10,10]);"` — verify CSG tree contains a `cube` node (BOSL2 `cuboid()` inlined to native `cube()` by the evaluator). Resolve `"cyl(l=30,d=10);"` → verify `cylinder` node with resolved params. Resolve `"diff() cuboid(30) { edge_profile(TOP) mask2d_roundover(r=5); }"` → verify `difference` node with `cube` child and 4 `linear_extrude` children containing `mirror` + `polygon`.
  **QA failure:** Resolve invalid SCAD `"{{{"` — verify parse error with line number. Translate a `hull` node — verify `UnsupportedSCADNodeError("hull")` raised. Translate a `polyhedron` with 1000+ arbitrary verts (BOSL2 `spheroid` style) — verify mesh-body fallback creates a base feature.
  **Commit:** `feat: add CSG translator with openscad-evaluator BOSL2 resolution`

- [x] 14. Add `create_from_scad` tool — translate .scad to native parametric Fusion features
  **References:** Depends on todo 13 (translator), todo 2 (bundling), todo 10 (mesh fallback path). `FusionMCP.py` (new `_create_from_scad`).
  **Acceptance:** New tool `create_from_scad(code: str, units: str = "mm", fallback_to_mesh: bool = True)`. Resolves code via `scad_translator.resolve_scad()` (evaluator primary path, CSG-export fallback). Translates via `scad_translator.translate_to_fusion_commands(csg_nodes, root, design, units=units)`. If any node raises `UnsupportedSCADNodeError` and `fallback_to_mesh=True`: **first delete all bodies created during the partial translation** (using the `created_body_names` list from the translator), then fall back to `run_scad` (todo 10) for the entire model. If `fallback_to_mesh=False`, delete partial bodies and return error listing unsupported nodes. Result bodies are real BRep bodies created via sketch+extrude (showing in timeline as extrude and combine features). Returns `{"created": True, "method": "csg_translation" | "mesh_fallback", "bodies": int, "features": int, "unsupported_nodes": [str], "fallback_reason": str | null}`. When `method` is `"mesh_fallback"`, `fallback_reason` explains which nodes triggered the fallback.
  **QA happy:** Call `create_from_scad(code="difference() { cube([20,20,20]); cylinder(r=5, h=30); }", units="mm")`. Verify a box with a cylindrical hole appears as a BRep body (not mesh) with dimensions in mm (2cm × 2cm × 2cm box). Call `get_timeline_info()` — verify extrude features (for cube and cylinder shapes) and combine feature (for the difference) appear in the timeline. Call `create_from_scad(code="include <BOSL2/std.scad>\ncuboid([20,20,20]);")` — verify BRep box created (BOSL2 `cuboid()` inlined to `cube()` by evaluator). Call `create_from_scad(code="include <BOSL2/std.scad>\ntorus(d_maj=40, d_min=10);")` — verify BRep torus (revolve of circle).
  **QA failure:** Call `create_from_scad(code="hull() { cube(5); sphere(3); }", fallback_to_mesh=False)`. Verify error listing `hull` as unsupported AND verify no partial bodies remain in design. Call with `fallback_to_mesh=True` — verify it falls back to mesh import with `"fallback_reason": "Unsupported node: hull"` AND verify no partial BRep bodies remain from a failed translation attempt.
  **Commit:** `feat: add create_from_scad tool for CSG-to-timeline translation`

- [x] 15. Add 2D primitive support to CSG translator — `polygon`, `circle`, `square` → sketch profiles
  **References:** `mcp_server/scad_translator.py` (extend from todo 13). API: `sketch.sketchCurves.sketchLines.addByTwoPoints()`, `sketch.sketchCurves.sketchCircles.addByCenterRadius()`, `root.features.extrudeFeatures`. **Multi-child extrude:** When a 2D-extrude node has multiple 2D children (e.g., `linear_extrude(h=10) { circle(5); square([2,2]); }`), ALL children are drawn in a SINGLE sketch; the extrude uses all closed profiles in that sketch (iterate `sketch.profiles` and extrude the profile that represents the union, or use `extrudeFeatures` with all profiles in an `ObjectCollection`). **Todo 13 vs 15 demarcation:** Todo 13 handles 3D primitives + transforms + booleans ONLY. Todo 15 handles ALL 2D primitives and their extrusion. There is no overlap.
  **Acceptance:** Extend `scad_translator.translate_to_fusion_commands()` to handle: `circle(r=X)` → creates sketch circle, returns the sketch and profile; `square([w,h])` → creates sketch rectangle, returns profile; `polygon(points=[...])` → creates sketch lines forming closed profile, returns profile; `polygon(points=[...], paths=[outer_idx, inner_idx])` → creates sketch lines forming closed profile AND inner loop(s) as hole(s) (BOSL2 `ring()` and `region()` produce this — verified empirically). When these appear inside `linear_extrude(height=H)` or `rotate_extrude(angle=A)`, the 2D profile is extruded via `extrudeFeatures` (sketch on XY plane) or `revolveFeatures` (sketch on XZ plane, revolve around Z construction axis). For multi-child 2D nodes inside an extrude, all shapes drawn in one sketch; extrude targets the combined profile set. **BOSL2 2D modules supported via evaluator resolution:** `rect()` → `square()`, `circle()` → native `circle()`, `regular_ngon()`/`hexagon()`/`pentagon()` → `polygon()`, `ellipse()` → `polygon()`, `ring()` → `polygon(points=, paths=[outer,inner])`, `mask2d_roundover()`/`mask2d_chamfer()` → `polygon()` with computed points. All verified in BOSL2 research to resolve to these primitives in the evaluator CSG tree.
  **QA happy:** Call `create_from_scad(code="linear_extrude(height=10) circle(r=5);")`. Verify a cylinder body appears as BRep with an extrude feature in timeline.
  **QA failure:** Call `create_from_scad(code="polygon(points=[[0,0],[10,0],[5,10]]);")` without extrude. Verify error: "2D primitive requires linear_extrude or rotate_extrude parent."
  **Commit:** `feat: add 2D primitive and extrude support to CSG translator`

- [x] 16. Add transform chain support — nested translate/rotate/scale on CSG operations
  **References:** `mcp_server/scad_translator.py`. API: `adsk.core.Matrix3D` for transform composition, `root.features.moveFeatures.createInput2()` + `.defineAsFreeMove()`, `root.features.scaleFeatures`.
  **Acceptance:** Extend translator to handle nested transforms: `translate([x,y,z]) rotate([a,b,c]) cube([...])` → creates cube body, applies rotation via `moveFeatures` with rotation matrix, then translation. Transform matrices compose correctly (parent transforms apply to children). Verify via body position after creation matches expected translated/rotated position.
  **QA happy:** Call `create_from_scad(code="translate([10,0,0]) cube([5,5,5]);")`. Verify body bounding box min is at approximately [10, 0, 0] (in mm). Call with nested `translate([5,0,0]) rotate([0,0,90]) cube([5,5,5])` — verify combined transform applied.
  **QA failure:** Call with invalid rotation values (non-numeric). Verify error.
  **Commit:** `feat: add transform chain support to CSG translator`

### Wave 5 — Integration Testing

- [x] 17. Create pytest test suite for pure-logic modules
  **References:** `mcp_server/scad_translator.py`, `mcp_server/bundle.py`. Create `tests/` directory with `test_scad_translator.py`, `test_bundle.py`.
  **Acceptance:** New directory `tests/` with `conftest.py`, `test_scad_translator.py` (tests: resolve valid SCAD via evaluator, resolve invalid SCAD, identify supported/unsupported CSG nodes, transform matrix composition, **BOSL2 module resolution** — resolve `cuboid([10,10,10])` and verify CSG tree contains `cube` node, resolve `cyl(l=30,d=10)` and verify `cylinder` node, resolve `torus(d_maj=40,d_min=10)` and verify `rotate_extrude`+`circle` nodes), `test_bundle.py` (tests: path resolution logic, unit conversion mm↔cm↔in). Run `pytest tests/ -v` — all tests pass without Fusion running. BOSL2 resolution tests require `openscad-evaluator` + `manifold3d` installed but NOT Fusion running — the evaluator works in pure Python.
  **QA happy:** Run `pytest tests/ -v` from repo root. All tests pass.
  **QA failure:** Break a parser test (e.g., change expected AST node type) — verify test fails with clear assertion error.
  **Commit:** `test: add pytest suite for scad_translator and bundle modules`

- [x] 18. Create live integration test script for dimensioning tools
  **References:** All dimensioning tools from Wave 1 (todos 3-6). Script at `tests/test_dimensioning_live.py` — requires Fusion running.
  **Acceptance:** Python script that connects to Fusion via HTTP (port 7432) and exercises: create box → `get_physical_properties` → verify volume/area/CoM. Create box → `measure_angle` between two faces → verify 90°. Create box → `get_oriented_bounding_box` → verify dimensions. Create cylinder → `inspect_body` → verify cylindrical face with radius. Script prints PASS/FAIL for each check.
  **QA happy:** Run with Fusion open and bridge running. All checks print PASS.
  **QA failure:** Run with Fusion closed. Verify connection error message.
  **Commit:** `test: add live dimensioning integration tests`

- [x] 19. Create live integration test for import tools
  **References:** Import tools from Wave 2 (todos 7-9). Script at `tests/test_import_live.py`.
  **Acceptance:** Script exercises: export STEP → clear → import STEP back → verify body exists. Create test STL → import mesh → verify mesh body exists. Create test SVG → import sketch → verify sketch exists. Prints PASS/FAIL.
  **QA happy:** Run with Fusion open. All checks PASS.
  **QA failure:** Run with nonexistent file paths. Verify each import returns appropriate error.
  **Commit:** `test: add live import integration tests`

- [x] 20. Create live integration test for OpenSCAD pipeline with Kumiko validation
  **References:** Kumiko files at `C:\Users\danie\Downloads\KumikoPatreon\Source Code\Source Code\` (or configurable via `KUMIKO_SCAD_PATH` env var — skip Kumiko checks if path doesn't exist). `run_scad`, `update_scad_body`, `create_from_scad` from Waves 3-4. Script at `tests/test_openscad_live.py`.
  **Acceptance:** Script exercises: (1) `run_scad(code="cube([10,10,10]);")` → verify mesh body. (2) `run_scad` with BOSL2 code → verify BOSL2 include resolves. (3) `update_scad_body` with changed param → verify geometry changes. (4) If `KUMIKO_SCAD_PATH` env var or default path exists: read `individual_insert_generator.scad`, override `Insert_Pattern=1`, render via `run_scad` → verify mesh body created. (5) `create_from_scad(code="difference(){cube([20,20,20]);cylinder(r=5,h=30);}")` → verify BRep body with timeline features. (6) `create_from_scad(code="union(){cube([10,10,10]);cylinder(r=5,h=10);}")` → verify union body. (7) `create_from_scad(code="translate([20,0,0])cube([10,10,10]);")` → verify translated body position. (8) `create_from_scad(code="linear_extrude(height=10)circle(r=5);")` → verify extruded cylinder. (9) `create_from_scad(code="include <BOSL2/std.scad>\ncuboid([20,20,20]);")` → verify BRep box (BOSL2 `cuboid()` inlined to `cube()` by evaluator). (10) `create_from_scad(code="include <BOSL2/std.scad>\ntorus(d_maj=40,d_min=10);")` → verify BRep torus (revolve of circle). (11) `create_from_scad(code="include <BOSL2/std.scad>\nxcopies(n=3,spacing=12)cuboid([5,5,5]);")` → verify 3 BRep bodies (for-loop unrolled). (12) `create_from_scad(code="include <BOSL2/std.scad>\ndiff() cuboid(30) { edge_profile(TOP) mask2d_roundover(r=5); }")` → verify BRep box with filleted/chamfered edges (difference of cube and 4 extruded mask polygons). Checks 4 is skipped (with printed SKIP message) if Kumiko files not found.
  **QA happy:** All non-skipped checks PASS. Kumiko insert renders successfully if path exists. BOSL2 CSG translation checks (9-12) all produce BRep bodies with timeline features.
  **QA failure:** `create_from_scad` with unsupported `hull()` → verify graceful fallback to mesh with `"fallback_reason"` in response. `create_from_scad` with BOSL2 `prismoid()` → verify Loft feature (2 sketch profiles + loft). `create_from_scad` with BOSL2 `spheroid(style="icosa")` → verify polyhedron mesh-body fallback (arbitrary VNF topology).
  **Commit:** `test: add OpenSCAD pipeline integration tests with Kumiko validation`

- [x] 21. Update `mcp_server/requirements.txt` with new dependencies and bump Python requirement
  **References:** `mcp_server/requirements.txt` (currently has `mcp` and `requests`). Add: `openscad-lalr-parser>=1.1.0` (requires Python ≥3.11), `openscad-evaluator>=1.1.0` (MIT, depends on `manifold3d`), `manifold3d>=3.5.2`. `README.md` line: "Python 3.10+" → "Python 3.11+". Do NOT add `solidpython2` — no todo uses it (removed as scope creep).
  **Acceptance:** `mcp_server/requirements.txt` includes `openscad-lalr-parser>=1.1.0`, `openscad-evaluator>=1.1.0`, and `manifold3d>=3.5.2`. README prerequisites updated to "Python 3.11+". `pip install -r mcp_server/requirements.txt` succeeds on Python 3.11+.
  **QA happy:** Fresh `pip install -r mcp_server/requirements.txt` in a Python 3.11+ venv — all packages install. Verify `from openscad_evaluator import Evaluator` imports successfully.
  **QA failure:** Run on Python 3.10 — verify `openscad-lalr-parser` fails to install with clear version requirement message.
  **Commit:** `deps: add openscad-lalr-parser, openscad-evaluator, manifold3d; bump Python to 3.11+`

- [x] 22. Update README.md with new tools documentation
  **References:** `C:\Users\danie\fusion-mcp\README.md`. Add new tool sections.
  **Acceptance:** README "Features" section includes: Inspection subsection with `get_physical_properties`, `measure_angle`, `get_oriented_bounding_box`, `inspect_body`. New "Import" section with `import_cad_file`, `import_mesh_file`, `import_sketch_file`. New "OpenSCAD Pipeline" section with `run_scad`, `update_scad_body`, `create_from_scad`, `import_mesh_data`. New "Bundling" note explaining auto-download of OpenSCAD+BOSL2.
  **QA happy:** Read README, verify all new tools listed with descriptions.
  **QA failure:** N/A.
  **Commit:** `docs: update README with new tools and OpenSCAD pipeline`

## Final verification wave

- [~] F1. Plan compliance audit — Verify every todo in this plan has been implemented with the exact function signatures, return shapes, and error handling specified. Cross-reference each `@mcp.tool()` in `fusion_server.py` with corresponding handler in `FusionMCP.py` dispatcher.
- [~] F2. Code quality review — Verify: no bare `except:` blocks in new code; all Fusion API calls wrapped in try/except with meaningful error messages; consistent JSON response shapes across all new tools; no hardcoded paths (all use `os.path` or `pathlib`); type hints on all new Python functions.
- [~] F3. Real manual QA — Agent-executed live verification against running Fusion 360: (a) `create_sketch` + `draw_center_rectangle` + `extrude_sketch` to create a 10cm box, then `get_physical_properties`, capture screenshot via `capture_screenshot` tool and verify body exists via `get_design_info`. (b) `run_scad` with `code="cube([10,10,10]);"`, capture screenshot, verify mesh body via `get_bodies_info`. (c) `create_from_scad` with `code="difference(){cube([20,20,20]);cylinder(r=5,h=30);}"`, capture screenshot, verify BRep body via `get_timeline_info` showing parametric features. (d) `create_from_scad` with `code="include <BOSL2/std.scad>\ncuboid([20,20,20]);"`, verify BRep box via `get_timeline_info` (BOSL2 module resolved to native features). (e) `run_scad` with Kumiko `individual_insert_generator.scad` content (read from path, or skip if path doesn't exist), verify mesh body created. All assertions are programmatic (JSON field checks on tool return values), not human visual inspection.
- [~] F4. Scope fidelity — Confirm: NO drawing generation code was written. NO proprietary code was copied. MIT LICENSE exists. OpenSCAD+BOSL2 bundling works without PATH. All 5 components are implemented. Tests pass.

## Commit strategy

Each todo is a separate atomic commit with the prefix specified in its Commit line. Final commits are squashed into logical groups if the worker prefers, but the prefixes must be preserved. All commits go to the current branch.

## Success criteria

1. All 22 implementation todos are complete with acceptance criteria met
2. All 4 final verification items (F1-F4) pass
3. `pytest tests/ -v` passes without Fusion running (requires Python 3.11+)
4. Live integration tests pass with Fusion running
5. Kumiko `.scad` file renders successfully via `run_scad` (if Kumiko files present)
6. CSG operations (`cube`, `cylinder`, `sphere`, `polyhedron`, `difference`, `union`, `intersection`, `translate`, `rotate`, `scale`, `mirror`, `multmatrix`, `linear_extrude`, `rotate_extrude`, `minkowski-for-rounding`) create native parametric Fusion features via `create_from_scad`
7. MIT LICENSE file exists
8. OpenSCAD+BOSL2 bundle auto-downloads on first run without PATH configuration (Windows + macOS)
9. BOSL2 modules (`cuboid`, `cyl`, `prismoid`, `torus`, `sphere`, `diff`, `edge_profile`, `xcopies`) create native parametric Fusion features via `create_from_scad` using `openscad-evaluator` resolution — NOT mesh fallback
10. CSG-to-timeline documented limitation: only `hull`, `surface`, `text`, `import`, `minkowski` with non-standard operands, and `polyhedron` with arbitrary VNF topology fall back to mesh; all other constructs including BOSL2 modules produce native parametric features
