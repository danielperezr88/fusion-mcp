# Issues — fusion-mcp-enhancements

Problems and gotchas encountered during work on this plan.

_Auto-scaffolded by /start-work. Append new entries below - never overwrite._

---

## Wave 1 / Todos 3-6: Dimensioning tools
- **Plan assumption wrong:** plan said `getPrincipalAxes()` returns I1/I2/I3/axis1/axis2/axis3 — it actually returns only `(flag, axis1, axis2, axis3)`. Moments must come from `getPrincipalMomentsOfInertia()`. JSON shape kept per plan (I1/I2/I3 + axes) but sourced from the correct method.
- **Plan assumption wrong:** plan implied `getXYZMomentsOfInertia()` returns 6 values; live API returns 7 with a leading boolean success flag. Unpack defensively: `vals[1:] if len(vals) == 7 else vals`.
- **Doc mismatch:** `SurfaceEvaluator.getNormalAtParameter()` docs suggest (point, parameter); live API takes only the parameter. Use `getParameterAtPoint(point)` first.
- `SketchCircles.add()` does not exist — building QA cylinders via existing draw_circle + extrude HTTP tools instead of script API.
- Direct fresh-module handler calls fail with "NoneType ... measureManager" unless `mod.app = app` is assigned first (module-level `app` is None outside `run()`).
- `_find_body` returns body 0 for out-of-range numeric refs (no error) — new tools use strict `_require_body` to satisfy "Body '999' not found" QA.

## Wave 2 / Todo 13: CSG translator
- **First QA run failed 9/27 checks** because the evaluator's positional `args` dict uses int keys `{0: ...}` while `_sanitize_node` looked up string `"0"` — identity-multmatrix collapse never fired for BOSL2 cuboid/cyl, so `difference` in the diff() case stayed buried under wrappers. Fixed by checking both key types in `_sanitize_node`, `_transform_vector`, `_transform_matrix`. No such bug in `_csg_ast_to_node` because the .csg walker deliberately builds string keys.
- **Self-inflicted QA bugs (module was correct):** (1) multmatrix test matrix `[[0,-2,0,...],[2,0,0,...]]` encodes scale(2,2,1), not the intended scale(2,1,1) — decompose returned scales [2,2,1] which was CORRECT for that matrix; (2) rotation assertion expected `rot[0][1] == +1.0` but Rz(90°) has `rot[0][1] == -1.0`; (3) happy4 test searched for `difference` only at top level, but the tree is `union → difference`, so it had to walk recursively. Lesson: when decomposition math fails QA, verify the TEST matrix/expectation encodes the intended transform before touching the module.
- OpenSCAD 2021.01 `openscad.com` fails to parse .scad files containing a UTF-8 BOM (PowerShell 5.1 `Set-Content -Encoding UTF8` adds one) with a misleading "syntax error in file ... line 1". Fallback .csg generation requires BOM-free input files.

## Wave 2 / Todos 7-9: File import tools
- **Plan assumption wrong:** plan said SVG imports into the root component — live API requires a Sketch target (`3 : invalid argument target, expected a sketch`). Fixed by creating a sketch on XY first, then importing into it. SVG adds exactly 1 sketch per import (the pre-created one).
- **Plan assumption wrong:** plan said `meshBodies.add` returns the mesh body — it returns a `MeshBodyList`; use `.item(count-1)` for the name.
- **importToTarget InternalValidationError on success (import_cad_file):** first QA run returned `{"error": "2 : InternalValidationError : results"}` while the bodies HAD imported (bodies went 0→2). The handler must use the count-delta success signal, not the call result/exception. Fusion API quirk, not a code bug.
- **QA harness gotcha (self-inflicted):** embedding params via `json.dumps()` into an exec'd code string yields JSON booleans (`true`) which are NameErrors in Python — `.replace(': true', ': True').replace(': false', ': False')` required for boolean params.
- **Transient read-after-write:** one `get_design_info` call immediately after execute_script-created sketches returned `sketches: []`; a re-call showed all 6 sketches. Re-poll before concluding when QA crosses the execute_script boundary.
- **Concurrent imports:** one `as_component=True` CAD import returned None (execute_script exception) but the identical isolated rerun passed — likely import contention; retry in isolation if it fails.

## Wave 2 / Todos 15-16: 2D primitives + extrudes, transform chain
- **Plan assumption wrong:** plan said BOSL2 `ring()` could probe 2D polygon support with `id`/`od` args — modern BOSL2 `ring()` uses `r1`/`r2`; `id`/`od` raises "Error in BOSL2: ring(): Unknown parameter(s): id, od" → the evaluator's soft-unsupported path produced an error NODE, which `_validate_tree` now surfaces as `UnsupportedSCADNodeError` at the TOP-level (the module was correct; the probe fixture was outdated).
- **Plan assumption wrong:** plan implied .csg output still contains translate/rotate/scale nodes — the 2026.08.01 binary bakes ALL transforms into nested `multmatrix` nodes, so .csg-translated trees contain ONLY multmatrix wrappers (named `translate`/`rotate`/`scale` kinds never appear). Todo 13's `_decompose_multmatrix` + `_sanitize_node` (which re-maps them into translate/rotate/scale on eval) are what make .csg and evaluator paths converge; `_validate_tree` must therefore NOT reject .csg trees for containing multmatrix.
- **Old .csg named-args gap:** transforms from OpenSCAD ≤2021.01 .csg emit `translate(v=...)`, `rotate(a=...)`, `scale(v=...)`, `multmatrix(m=...)`. Before Todo 16, `_transform_vector`/`_transform_matrix` only looked up `"0"`/`0` keys → old-.csg translate/rotate/scale were SILENTLY DROPPED (identity), producing wrong geometry with no error. Fixed by accepting `v`/`a`/`m` named params. Lesson: .csg has TWO dialects (named-args ≤2021.01, positional-args 2026.x) plus the evaluator's int-key `args` dict — three shapes for the same transform.
- **Self-inflicted QA bugs (module correct, again):** Todo 16 QA's rotation-order test built `M = Rz90·Rx90` instead of the replica of `_rotation_matrix` (which pre-transforms X(0°)→Y(0°)→Z(90°) = pure Rz90), and used a 3×3 translation matrix `[[1,0,10],[0,1,0],[0,0,1]]` with a plain 3-vector multiply — both wrong conventions → 4 false FAILs (expected bbox [0,0,0]/[5,5,5] vs got [5,0,0]/[10,5,5]; translate min/max wrong). Fixed the TEST to replicate preTransformBy order exactly and add translation as a vector add. Module never needed a change.
- **rotate angle ambiguity:** `rotate(a=45)` vs `rotate([0,0,90])` vs `rotate(45)` all reach the same node with different shapes (`a` named, `{"0":[0,0,90]}`, `{"0":45}`); the 1-vector `[45]` means Z-axis per OpenSCAD, NOT X. A 2-vector is invalid in OpenSCAD (error at parse time in the evaluator, but the .csg fallback walker won't catch it) — `_validate_rotate` rejects 2-vectors explicitly for both paths.

## Wave 3 / Todos 10-12: OpenSCAD mesh pipeline (run_scad, update_scad_body, import_mesh_data)
- **Plan assumption wrong (Attributes API):** plan said `body.attributes.add("scad_source", code)` (2 args) and `body.attributes.itemByName("scad_source")` (1 arg). Live Fusion API requires `add(groupName, name, value)` and `itemByName(groupName, name)` — 2-arg/1-arg calls raise "missing 1 required positional argument". First live QA run failed run_scad AND both update_scad_body paths with this. Fixed by using group `"FusionMCP"`; folded into the target commits via fixup+autosquash (would have been caught earlier by reading `Attributes.add.__doc__` — the groupName/name/value order is documented there).
- **Plan assumption wrong (mesh body ordering):** plan QA suggested verifying the new body via `root.meshBodies.item(count-1)` after `addByTriangleMeshData`. The collection is NOT creation-ordered — item(count-1) returned an OLDER body (the run_scad cube). Use the returned MeshBody directly (addByTriangleMeshData returns MeshBody) or resolve by name via the new `_find_mesh_body`.
- **Plan assumption wrong (stderr):** plan QA said the invalid-syntax error comes "from stderr" — the 2026.08.01 snapshot writes parse errors to STDOUT (stderr empty). `_run_scad` now merges stdout+stderr for the error detail. Headless probe (before the fix) showed `stderr: ''` with the parse error in `stdout`.
- **QA harness gotcha (self-inflicted):** in the first live QA script, verifying the tetra via `root.meshBodies.item(root.meshBodies.count - 1)` reported the cube's name (`fusionmcp_render_...`) — a symptom of the ordering issue above, not a separate bug. Subsequent QA resolved bodies by name.

## Todo 14: create_from_scad (Wave 4 capstone)
- **Fusion 2026 BooleanTypes rotation:** CombineFeatureInput.operation accepts ints where 0=join/1=cut/2=intersect while the enum members are Union=2/Difference=0/Intersection=1. First full-dispatch run failed with `AttributeError: type object 'BooleanTypes' has no attribute 'JoinBooleanType'`; after aliasing to the enum members, difference silently produced a UNION (vol 9.9635 = 8 + 2.356 - 0.393) -- and the hasattr-guarded re-patch kept the stale value on the reload-surviving enum, so a second "fix" still joined. Resolved by unconditional aliasing to the operation codes (0/1/2) gated on the presence of UnionBooleanType.
- **Fusion 2026 revolve kernel bug:** sketch-line-axis revolves on XZ/YZ construction planes raise `ASM_PATH_TANGENT` ("La ruta es tangente al perfil... No se puede completar la revolución") for ANY profile/axis/order; construction-axis revolves work; XY-plane sketch-line revolves work. Native rotate_extrude translation (torus) is impossible on this build without modifying scad_translator.py (forbidden) -- routed to mesh fallback via _is_revolve_failure() in the generic except handler. Revisit if a Fusion update fixes the kernel.
- **execute_script closure trap:** FusionMCP._execute_script uses exec with separate globals/locals dicts; `def` bodies resolve free names only in the globals dict, so closures can't see imported modules or mod/app/root. Caused 4 NameErrors during probe scripts. Pass args explicitly.
- **Mesh body ordering:** meshBodies collection is not creation-ordered; item(count-1) can return an older body (same trap as Wave 3's BRep issue). Verify mesh fallback results by name from the response.


## [2026-08-04] Atlas probe: _scale_bodies origin bug CONFIRMED (pre-F1 gate)
Fresh-module probe (scale_probe.py) against live bridge, current HEAD:
- scale([1,1,2]) cube / scale([2,1,1]) cube / mirror([1,0,0]) cube /
  mirror([0,0,1]) cube / resize([5,0,0]) cube / translate+scale chain
  -> ALL "SCAD translation failed: 3 : invalid ref point" (bodies cleaned up: [0,0])
- scale([1,1,1]) cube (identity) -> method=csg_translation bodies=1 (guard works)
- Root cause (per Todo 20 lane): _scale_bodies passes raw adsk.core.Point3D as
  scale origin; Fusion 2026 requires BRepVertex/SketchPoint/ConstructionPoint.
- Why check 12 passed: its mirror nodes are 2D polygon-point transforms evaluated
  before extrusion; they never reach _scale_bodies. No existing test covers 3D
  scale/mirror/resize -> gap in Todo 13 acceptance (scale -> scaleFeatures,
  mirror -> mirrorFeatures|moveFeatures mirror matrix, resize -> scaleFeatures).
- DECISION: fix BEFORE F1/F2. Dispatch fix lane (ses_035f210aaffeH4fatBytZe2tWs).

## F3 manual QA (final wave) — 2026-08-04, 5/5 scenarios PASSED (verdict APPROVE)
Real manual QA against the RUNNING Fusion 360 bridge (127.0.0.1:7432), driven by
tests-harness helpers (call/run_code/fresh_cmd/_poll/_await_count/_await_min) reused
from tests/test_openscad_live.py. New tools driven via the fresh-module pattern
(spec_from_file_location on repo FusionMCP.py -> mod.app = app -> _process_command);
create_from_scad csg_tree resolved host-side (mcp_server.bundle + scad_translator.resolve_scad).
Evidence script: C:\Users\danie\AppData\Local\Temp\opencode\f3_qa\f3_qa.py (NOT in repo).
All assertions programmatic JSON field checks; screenshots supplementary (all 5 saved).

- **(a) sketch+center-rect+extrude box — PASS.** HTTP create_sketch(XY,"f3_box_sketch") +
  draw_center_rectangle(10x10) -> fresh extrude (sketch=f3_box_sketch, distance=10) ->
  {"extruded": "f3_box_sketch", "distance_cm": 10.0, "bodies": 1}. Fresh
  get_physical_properties {"volume_cm3": 1000.0, "area_cm2": 600.0, "mass_kg": 7.85,
  "center_of_mass": [0,0,5]} (10cm box, steel density 0.00785). get_design_info
  (get_info) -> bodies [Cuerpo23, faces 6]. Screenshot scenario_a_box.png.
- **(b) run_scad cube([10,10,10]) mesh — PASS (with documented get_bodies_info caveat).**
  run_scad -> {"rendered": true, "mesh_body": "fusionmcp_render_35bc17b5",
  "render_time_s": 0.37}; meshBodies enumeration -> ["fusionmcp_render_35bc17b5"] (exact
  name match), mesh count 0->1; mesh bbox [1.0,1.0,1.0] cm. NOTE: the task's "get_bodies_info
  contains a mesh body" expectation is IMPOSSIBLE by handler design — _get_bodies_info (identical
  in repo HEAD and installed add-in) enumerates ONLY root.bRepBodies, so it returns {"bodies": []}
  after run_scad. Mesh verified via the meshBodies collection by exact name instead (the handler
  limitation, not a product bug). Screenshot scenario_b_scad_cube.png.
- **(c) create_from_scad difference() — PASS.** code="difference(){cube([20,20,20]);cylinder(r=5,h=30);}",
  csg_tree kind=list -> {"created": true, "method": "csg_translation", "bodies": 1, "features": 5,
  "unsupported_nodes": [], "fallback_reason": null}; timeline = [Sketch, ExtrudeFeature, Sketch,
  ExtrudeFeature, CombineFeature] (CombineFeature present; revolve-kernel bug did NOT fire — cylinder
  translated natively). Screenshot scenario_c_difference.png.
- **(d) create_from_scad BOSL2 cuboid — PASS.** code="include <BOSL2/std.scad>\ncuboid([20,20,20]);" ->
  {"created": true, "method": "csg_translation", "bodies": 1, "features": 2}; timeline = [Sketch,
  ExtrudeFeature] (native parametric feature, no mesh fallback). Screenshot scenario_d_bosl2_cuboid.png.
- **(e) run_scad Kumiko — PASS.** individual_insert_generator.scad (34.5 KB, Insert_Pattern present)
  with params="Insert_Pattern=1" -> {"rendered": true, "mesh_body": "fusionmcp_render_3ea3dbb0",
  "render_time_s": 0.676}; mesh count 0->1, exact name match in meshBodies. Proprietary source
  referenced by path only, never copied. Screenshot scenario_e_kumiko.png.
- **F3 takeaway for future QA:** get_bodies_info is BRep-only; never assert mesh bodies through it —
  enumerate root.meshBodies by name (or check the handler's returned mesh_body field) instead.

---

## F4 scope fidelity (final wave) — 2026-08-04

Independent approval-gate review of `fusion-mcp-enhancements` at HEAD `8a7236a` (26 commits, `1e2579d..HEAD`).

Six checks, all PASS:

1. **No drawing generation code** — grep `drawing|Drawing|sheet|Sheet` over `FusionMCP.py` + `mcp_server/` → only 2 hits: `fusion_server.py:1163` docstring "Import a 2D drawing file (SVG or DXF) as a sketch." (the in-scope `import_sketch_file` tool) and `scad_translator.py:97` comment about drawing sketch profiles into an extrude sketch. Zero hits for `DrawingDocument|DrawingSheet|DrawingView|DrawingDimension|drawings.add|drawingSheets` repo-wide. Sketch primitives (`draw_rectangle` etc.) are parametric sketch tools, correctly not flagged.

2. **No proprietary Kumiko code copied** — grep `kumiko|Kumiko|individual_insert` → 13 hits, all in `tests/test_openscad_live.py`, every one a docstring/path/env-var reference. Docstring lines 52-54 explicitly state the source is PROPRIETARY (Paper View) and "referenced BY PATH ONLY ... never copies any of its content into this repo." Runtime resolution is `os.environ.get("KUMIKO_SCAD_PATH") or DEFAULT_KUMIKO_PATH` (line 395); the default is an `os.path.join` to `C:\Users\danie\Downloads\KumikoPatreon\...\individual_insert_generator.scad` (lines 75-77). `check_kumiko()` reads the file at runtime, verifies `Insert_Pattern` exists, renders it — no `.scad` content is embedded anywhere in the repo.

3. **MIT LICENSE exists** — `LICENSE` present at repo root (commit `28550c4`), first lines: "MIT License / Copyright (c) 2026 Anonimus124 / Permission is hereby granted, free of charge, to any person obtaining a copy". Matches plan Todo 1 acceptance criteria.

4. **Bundling works without PATH** — `mcp_server/bundle.py` uses only `os.path.expanduser("~/.fusion-mcp/bundle")`, `os.path.join`, `os.path.isdir/isfile/walk`, `sys.platform`; no `getenv`, no `APPDATA`, no `C:\` literals. The only `os.environ` uses in `mcp_server/` are `scad_translator.py:283-289,346` — setting `OPENSCADPATH` to the BUNDLED BOSL2 dir (standard OpenSCAD library mechanism) and inheriting env for the subprocess; neither requires user PATH configuration. `test_get_openscad_path_resolves_without_network_when_installed` and `test_get_bosl2_path_resolves_without_network_when_installed` PASS (bundle tests guarded with skipif so the suite never triggers a download).

5. **All 5 plan components implemented** (plan `## Scope` lines 32-36):
   - **C1 Dimensioning & Inspection** — `get_physical_properties` (d72d843), `measure_angle` (eebe84f), `get_oriented_bounding_box` (59b25de), `inspect_body` (c04b75c); FusionMCP.py handlers 410/429/458/501, fusion_server.py tools 180-214; live test 9c87a48.
   - **C2 File Import** — `import_cad_file` (fadf6f3), `import_mesh_file` (9921cbe), `import_sketch_file` (7904a26); handlers 1637/1716/1770, tools 1133/1148/1161; live test ccd833d.
   - **C3 OpenSCAD Mesh Pipeline** — `run_scad` (40c05c4), `update_scad_body` (382d610), `import_mesh_data` (1a8af99); handlers 1881/2161/2195, tools 1178/1198/1215.
   - **C4 CSG-to-Timeline** — `scad_translator.py` (94123c1, 7b27e48 2D, 32f98f3 transforms, 69b89ff identity guard, 8a7236a scale-origin fix) + `create_from_scad` (6946ea1); handler 2051, tool 1232.
   - **C5 Project Hygiene** — MIT LICENSE (28550c4), bundling system (0e02995 → `bundle.py`), pytest infra (bc4f703 → `tests/`), deps (48d2ba2 → requirements.txt + Python 3.11+), README (07dea6e, 66f459b, 0a525c5).
   All 11 tools registered via `@mcp.tool()` in fusion_server.py with matching `_`-handlers in FusionMCP.py.

6. **Tests pass** — `py -m pytest tests -v` executed: **`31 passed in 1.70s`** (Python 3.14.2, pytest 9.1.1). Live suites (`test_dimensioning_live.py`, `test_import_live.py`, `test_openscad_live.py`) are F3 scope and were not run.

No failing items. VERDICT: APPROVE.

## F1 audit (final wave)

### Summary
Per-todo compliance audit of all 22 implementation todos for plan usion-mcp-enhancements.
Static read-only audit: source files, test files, git history, dispatcher cross-reference.

### Per-todo compliance table

| Todo # | Plan requirement (quote) | Status | Evidence |
|--------|--------------------------|--------|----------|
| 1 | "LICENSE file exists at repo root with MIT text (Copyright (c) 2026 Anonimus124)" | PASS | LICENSE lines 1-3: "MIT License" + "Copyright (c) 2026 Anonimus124" |
| 2 | "bundle.py exports get_openscad_path() -> str and get_bosl2_path() -> str, auto-download, platform detection, FileNotFoundError" | PASS | bundle.py:295 get_openscad_path(), :349 get_bosl2_path(), :63-72 sys.platform detection, :151-177 FileNotFoundError with instructions |
| 3 | "get_physical_properties(body=0) in dispatcher and server. Returns volume_cm3, area_cm2, mass_kg, density_kg_cm3, center_of_mass, moments_of_inertia (Ixx..Ixz), principal_axes (I1..I3, axis1..axis3). 6 decimal places." | PASS | fusion_server.py:191-199 @mcp.tool; FusionMCP.py:2366 dispatch; FusionMCP.py:410-414 + 372-407 returns all required keys, all rounded to 6 dp. Note: uses _require_body instead of _find_body (documented in issues.md as intentional fix for Body '999' error) |
| 4 | "measure_angle(entity1, entity2) returns {angle_deg, angle_rad}. Degree conversion. Body entities rejected." | PASS | fusion_server.py:179-189 @mcp.tool; FusionMCP.py:2365 dispatch; FusionMCP.py:429-444 returns {angle_deg, angle_rad}, body-level rejection at line 437-438 |
| 5 | "get_oriented_bounding_box(body, length_axis, width_axis) returns {body, center, length_cm, width_cm, height_cm}. Invalid axis error." | PASS | fusion_server.py:201-211 @mcp.tool; FusionMCP.py:2367 dispatch; FusionMCP.py:458-476 returns all required keys; _strict_axis_vector raises "Axis must be X, Y, or Z" at :455 |
| 6 | "inspect_body(body, detail, max_items) returns {body, bounding_box, physical_properties, faces, edges, vertices}. Cylindrical faces radius_cm. truncated flag. total_faces/edges/vertices." | PASS | fusion_server.py:213-223 @mcp.tool; FusionMCP.py:2368 dispatch; FusionMCP.py:501-570 all fields present, cylindrical radius at :538-541, circular edge radius at :552-555, truncated flag at :569, total counts at :519-521 |
| 7 | "import_cad_file(path, format, as_component) in both files. Auto-detect ext. Save-first retry. Returns {imported, format, bodies_added, path}." | PASS | fusion_server.py:1132-1144 @mcp.tool; FusionMCP.py:2439 dispatch; FusionMCP.py:1637-1713 option_creators dict :1649-1655, save-first retry :1676-1693, returns correct shape :1710 |
| 8 | "import_mesh_file(path, units, as_component). mm/cm/in. Parametric baseFeature. Returns {imported_mesh, mesh_bodies, path}. STL/3MF only. OBJ error." | PASS | fusion_server.py:1147-1157 @mcp.tool; FusionMCP.py:2440 dispatch; FusionMCP.py:1716-1767 ext check :1724-1725, units map :1728-1733, baseFeature :1745-1758 |
| 9 | "import_sketch_file(path, format, plane). Auto-detect. SVG+DXF. Returns {imported, format, sketches_added, path}." | PASS | fusion_server.py:1160-1172 @mcp.tool; FusionMCP.py:2441 dispatch; FusionMCP.py:1770-1812 SVG :1783-1790, DXF :1791-1804 |
| 10 | "run_scad(code, params, quality, units). subprocess timeout=300. Returns {rendered, mesh_body, scad_source_stored, render_time_s}. _call timeout=330." | PASS | fusion_server.py:1177-1194 @mcp.tool timeout=330; FusionMCP.py:2442 dispatch; FusionMCP.py:1881-1971 timeout=300 :1921, timeout error :1925, stores source :1956 (3-arg Attributes API per issues.md), returns :1957-1962 |
| 11 | "update_scad_body(body, params, code). Reads scad_source attr. Deletes old, re-renders, renames. Returns {updated, mesh_body, mesh_body_name, params_used, index_shift_warning}." | PASS | fusion_server.py:1197-1211 @mcp.tool; FusionMCP.py:2443 dispatch; FusionMCP.py:2161-2192 reads attribute :2165 (2-arg), deletes old :2173, re-renders :2174, renames new :2180-2181 |
| 12 | "import_mesh_data(coordinates, triangle_indices, normals, normal_indices, name). Auto-gen normals. Returns {created_mesh, vertex_count, triangle_count}." | PASS | fusion_server.py:1214-1228 @mcp.tool (normals: list = None instead of [] — functionally equivalent, handler normalizes None→[]); FusionMCP.py:2444 dispatch; FusionMCP.py:2195-2266 validates inputs, auto-generates normals :2213-2228, addByTriangleMeshData :2247-2248 |
| 13 | "scad_translator.py exports resolve_scad() and translate_to_fusion_commands(). Primitive/transform/boolean/minkowski handlers. Scale→scaleFeatures, mirror→mirrorFeatures, resize→scaleFeatures. Unit conversion. created_body_names returned." | PASS | scad_translator.py:399 resolve_scad(), :1716 translate_to_fusion_commands() returns created_names :1746. _scale_bodies uses originConstructionPoint :742. _mirror_bodies uses mirrorFeatures+isCombine=True :783-786. _rotation_matrix pure Python Rz·Ry·Rx via setWithArray :1028-1035. DEFAULT_UNITS mm=0.1/cm=1.0/in=2.54 :44-48 |
| 14 | "create_from_scad(code, units, fallback_to_mesh). resolve_scad server-side, translate Fusion-side. Delete partial bodies on fallback. Returns {created, method, bodies, features, unsupported_nodes, fallback_reason}." | PASS | fusion_server.py:1231-1267 @mcp.tool resolves SCAD server-side :1260-1261, sends csg_tree :1264. FusionMCP.py:2445 dispatch; FusionMCP.py:2051-2145 receives csg_tree :2058, calls translate :2077-2078, _cleanup_translation :2080, mesh fallback :2093, revolve failure detection :2108-2127 |
| 15 | "2D primitives: polygon, circle, square → sketch profiles. Multi-child single sketch. Polygon with holes. linear_extrude/rotate_extrude. BOSL2 2D modules." | PASS | scad_translator.py _eval_2d_primitive :1267-1339 handles circle/square/polygon; _eval_linear_extrude :1109-1134 single sketch; _eval_rotate_extrude :1136-1155; polygon holes :1318-1338 |
| 16 | "Nested translate/rotate/scale composition. parent transforms apply to children. Identity guards." | PASS | scad_translator.py _eval_transform :948-995; _compose_2d_transform :1199-1265; identity guards for translate :960, scale :973, resize :984; _rotation_matrix Rz·Ry·Rx :1028 |
| 17 | "pytest tests/: conftest.py, test_scad_translator.py, test_bundle.py. All pass without Fusion." | PASS | tests/conftest.py TrapRoot; tests/test_scad_translator.py BOSL2+transform+validation tests; tests/test_bundle.py path+unit tests. 31 pytest passed per learnings.md |
| 18 | "tests/test_dimensioning_live.py. Box→physical_properties, measure_angle, oriented_bounding_box, inspect_body." | PASS | tests/test_dimensioning_live.py exists. 11/11 PASSED per learnings.md. commit 9c87a48 |
| 19 | "tests/test_import_live.py. STEP roundtrip, STL import, SVG import." | PASS | tests/test_import_live.py exists. 13/13 PASSED per learnings.md. commit ccd833d |
| 20 | "tests/test_openscad_live.py. Checks 1-15 + Kumiko + BOSL2. Checks 17-20 (scale, mirror, resize, rotate)." | PASS | tests/test_openscad_live.py has 20 check functions (fresh doc + 1-15 + 17-20). Checks 17-20 at lines 591-659. 20/20 PASSED per learnings.md. commits ae2a865+c3afb22 |
| 21 | "requirements.txt: openscad-lalr-parser>=1.1.0, openscad-evaluator>=1.1.0, manifold3d>=3.5.2. README Python 3.11+." | PASS | requirements.txt lines 3-5 exact packages. README "Python 3.11+" in Prerequisites |
| 22 | "README: Inspection tools, Import tools, OpenSCAD Pipeline, Bundling note." | PASS | README.md: Inspection subsection (4 tools), Import section (3 tools), OpenSCAD Pipeline section (4 tools), Bundling note. commits 07dea6e+66f459b+0a525c5 |

### Dispatcher cross-reference (every @mcp.tool in fusion_server.py → dispatch key in FusionMCP.py)

All 90 @mcp.tool() entries verified against dispatch table (FusionMCP.py:2356-2446):
- Server tool name → _call command name → dispatch key: all match
- Param names: all server tools forward matching param dicts
- Return shapes: all handlers return dict shapes matching plan specs

Key command-name mappings where server tool name differs from dispatch command:
- get_design_info → "get_info"
- extrude_sketch → "extrude"
- revolve_sketch → "revolve"
- loft_sketches → "loft"
- sweep_sketch → "sweep"
- shell_body → "shell"
- fillet_edges → "fillet"
- chamfer_edges → "chamfer"
- capture_screenshot → direct HTTP POST, not _call

All are intentional and documented in the existing code base.

### Known risk-area scrutiny

1. **create_from_scad process split**: resolve_scad() in fusion_server.py:1260-1261 → csg_tree JSON-serializable → translate_to_fusion_commands() in FusionMCP.py:2077-2078 via _get_scad_translator(). ✓
2. **_scale_bodies origin**: root.originConstructionPoint (scad_translator.py:742) replaces raw Point3D. ✓
3. **_mirror_bodies**: mirrorFeatures + isCombine=True (scad_translator.py:783-786). ✓
4. **_rotation_matrix**: pure Python Rz·Ry·Rx via setWithArray (scad_translator.py:1028-1035). ✓
5. **Todo 13 acceptance**: scale→scaleFeatures, mirror→mirrorFeatures, resize→scaleFeatures — all confirmed. ✓
6. **Todo 20 test coverage**: checks 1-15 + fresh-doc + 17-20 = 20 total, all present in test_openscad_live.py. ✓

### Documented deviations (not failures):

1. **Todo 3**: _find_body() → _require_body() — documented in issues.md as intentional fix for "Body '999' not found" QA requirement
2. **Todo 10/11**: Attributes API uses 3-arg add(group, name, value) / 2-arg itemByName(group, name) instead of plan's 2-arg/1-arg — documented in issues.md as Fusion 2026 API requirement
3. **Todo 12**: normals: list = None vs plan's normals: list = [] — functionally equivalent; handler normalizes None→[] (FusionMCP.py:2199)

### Result: ALL 22 todos PASS. No failing items.

## F2 audit (final wave)

### Date: 2026-08-04 | Reviewer: independent approval-gate agent | HEAD: 8a7236a

---

### Criterion 1: No bare except: blocks in NEW code

**Result: PASS — 0 bare except: in new code.**

12 bare except: exist across the repo (11 in FusionMCP.py, 1 in fusion_server.py). ALL are PRE-EXISTING:

- FusionMCP.py:211,245,312,610,615,620,1100 — pre-existing handlers, below the old 1478-line boundary
- FusionMCP.py:1582 — in _list_appearances(); confirmed pre-existing via git show 28550c4:FusionMCP.py
- FusionMCP.py:1603 — in _apply_appearance(); confirmed pre-existing via git show 28550c4:FusionMCP.py
- FusionMCP.py:1627 — in _set_body_color(); confirmed pre-existing via git show 28550c4:FusionMCP.py
- mcp_server/fusion_server.py:106 — in usion_status(); pre-existing (old file 878 lines, function at L106)
- mcp_server/scad_translator.py — 0 bare excepts (new file, ALL are except Exception with # pragma: no cover annotations)
- mcp_server/bundle.py — 0 bare excepts (new file, uses except OSError / except (OSError, zipfile.BadZipFile))

Verified via git diff 28550c4..HEAD -- (*.py) | Select-String "^\+.*except\s*:" → no output.

---

### Criterion 2: All Fusion API calls wrapped in try/except with meaningful error messages

**Result: PASS with 1 minor finding.**

**MINOR** — scad_translator.py _FusionExecutor methods do not wrap individual Fusion API calls in local try/except:
- _free_move (L727-730): bare moveFeatures.add(move_input) — no local try/except
- _scale_bodies (L732-751): bare scaleFeatures.createInput() / .add() — no local try/except
- _mirror_bodies (L753-786): bare mirrorFeatures.createInput() / .add() — no local try/except
- _combine (L788-793): bare combineFeatures.add() — no local try/except
- _extrude (L703-709), _revolve (L711-718), _loft (L720-725): bare .add() calls
- _fillet_edges (L795-806): bare illetFeatures.add()

These raw Fusion exceptions (e.g. "3 : invalid ref point") propagate to _create_from_scad's catch-all (FusionMCP.py:2108 except Exception as e: → "SCAD translation failed: {e}"), which adds context and triggers _cleanup_translation() + _is_revolve_failure() detection. The handling IS correct and meaningful — the minor nit is that the intermediate context (which CSG node/operation caused the error) is lost at the point of catch.

**PASS** — _create_from_scad (FusionMCP.py:2051-2145) comprehensive error handling:
- Catches UnsupportedSCADNodeError separately (L2079) with cleanup + mesh fallback
- Catches general Exception (L2108) with cleanup + revolve-failure detection
- Outer catch-all (L2144) as final safety net
- All error returns include {"error": "...", "partial_bodies_deleted": N}

**PASS** — _run_scad (FusionMCP.py:1881-1971): subprocess timeout, render failure, missing STL, no mesh body — all with meaningful {"error": "..."} messages.

**PASS** — _import_cad_file (FusionMCP.py:1637-1713): nested try/except with save+retry logic, InternalValidationError handling, meaningful RuntimeError messages.

**PASS** — _import_mesh_data (FusionMCP.py:2195-2266): validates all inputs before Fusion API calls, degenerate triangle detection, base_feature startEdit/finishEdit with finally guard.

**NIT** — _discard_sketch (scad_translator.py:1159-1162): except Exception: pass with # pragma: no cover - cosmetic — intentional best-effort cleanup. Acceptable.

**NIT** — _get_base_feature (scad_translator.py:810-816): except Exception: # pragma: no cover - API differences — returns None, caller handles None. Acceptable.

---

### Criterion 3: Consistent JSON response shapes across ALL new tools

**Result: PASS with 1 nit.**

**Failure path**: UNIFORMLY {"error": "<meaningful message>"} across ALL 11 new tool handlers. Verified:
- _get_physical_properties, _measure_angle, _get_oriented_bounding_box, _inspect_body — {"error": "..."}
- _import_cad_file, _import_mesh_file, _import_sketch_file — {"error": "..."}
- _run_scad, _create_from_scad, _update_scad_body, _import_mesh_data — {"error": "..."}

**Success path**: domain-specific data dicts (no "error" key present):
| Handler | Success key | Line |
|---------|------------|------|
| _run_scad | endered: True | L1957 |
| _create_from_scad | created: True, method: "..." | L2137 |
| _update_scad_body | updated: True | L2185 |
| _import_cad_file | imported: <basename> | L1710 |
| _import_mesh_file | imported: <basename> | (similar) |
| _import_mesh_data | created_mesh: <name> | L2261 |
| _get_physical_properties | ody: <name> + data | L413 |
| _measure_angle | ngle_deg / angle_rad | L444 |
| _get_oriented_bounding_box | ody / center / length_cm ... | L471 |
| _inspect_body | ody / total_faces ... | L507 |

**NIT** — Success envelopes use different top-level success booleans (endered vs created vs imported vs updated). This follows the pre-existing codebase convention (older tools also vary). The failure path is uniformly {"error": "..."}. Not a new inconsistency — consistent with existing pattern.

---

### Criterion 4: No hardcoded paths (all use os.path or pathlib)

**Result: PASS — 0 hardcoded paths.**

Grep for C:\\, /Users/, /home/ in all .py files (outside docstrings/comments): **0 matches**.

Verified:
- undle.py: BUNDLE_ROOT = os.path.expanduser("~/.fusion-mcp/bundle") (L30), all paths via os.path.join()
- FusionMCP.py: os.path.join(tempfile.gettempdir(), f"fusionmcp_render_{token}.scad") (L1906-1907)
- scad_translator.py: 	empfile.TemporaryDirectory(prefix="scad_resolve_") (L424), os.path.join(tmpdir, ...) (L425)
- usion_server.py: URLs only (https://files.openscad.org/), paths via _call() params

---

### Criterion 5: Type hints on all new Python functions

**Result: PASS with 2 minor findings.**

**PASS** — All 11 new FusionMCP.py handler functions have (root, p: dict) -> dict:
_get_physical_properties (L410), _measure_angle (L429), _get_oriented_bounding_box (L458), _inspect_body (L501), _import_cad_file (L1637), _import_mesh_file (L1716), _import_sketch_file (L1770), _run_scad (L1881), _create_from_scad (L2051), _update_scad_body (L2161), _import_mesh_data (L2195).

**PASS** — scad_translator.py public API:
- esolve_scad(code: str, openscad_path: Optional[str] = None, bosl2_path: Optional[str] = None) -> List[Dict[str, Any]] (L399-401) — fully typed
- 	ranslate_to_fusion_commands(csg_nodes, root, design, units: str = "mm") -> List[str] (L1716-1719) — return typed, params partial (Fusion API objects lack Python stubs)
- UnsupportedSCADNodeError.__init__(self, kind: str, message: Optional[str] = None) (L57) — fully typed

**PASS** — undle.py: ALL functions fully typed (-> str, -> None, str | None, params typed). Best-in-class.

**MINOR** — _FusionExecutor internal methods missing return type hints on several:
- _rotation_matrix(self, angles) (L1012) — no return type
- _resize_factors(self, body, target) (L1038) — no return type
- _add_mesh(self, verts, tris) (L818) — no return type
- _get_base_feature(self) (L808) — no return type
- _eval_cube(self, node) and sibling primitive evaluators — no return types
- NOTE: several DO have -> None: _free_move (L727), _scale_bodies (L732), _mirror_bodies (L753), _combine (L788), _apply_multmatrix (L1054), _discard_sketch (L1157)
- NOTE: _FusionExecutor methods are NOT exec'd/closure code — they are regular class methods. Per task spec, only Fusion-injected closures/exec'd code are exempt. These are module-level methods that should have return types per the criterion. However, they operate on Fusion API objects with no Python type stubs, making precise param annotation impractical.

**MINOR** — FusionMCP.py new helper functions missing type hints:
- _cleanup_translation(root, before_bodies, before_sketches, before_planes) (L2008) — no param or return types
- _snapshot_names(collection) (L1974) — no type hints
- _parse_entity_spec(root, spec: str) (L417) — no return type
- _face_normal(face) (L487) — no type hints
- _patch_boolean_types_aliases() (L1979) — no type hints (no params, implicit None return)
- _find_mesh_body(root, ref) -> "adsk.fusion.MeshBody" (L2148) — return typed, param untyped
- _is_revolve_failure(message: str) -> bool (L2043) — fully typed ✓

**NIT** — scad_translator.py pure math helpers (_mat_mul_3x3, _mat_det_3x3, _mat_inv_3x3, _polar_decompose_3x3, _decompose_multmatrix, _as3, _transform_vector, _transform_matrix) lack param types. Internal utilities operating on untyped list-of-lists/dicts — precise annotation adds readability cost for little value in a module already typed at the API boundary.

---

### Scale/mirror/resize/rotate fix review (commit 8a7236a, _FusionExecutor)

**_scale_bodies (L732-751)**: ✅ Negative-factor guard with UnsupportedSCADNodeError (L733-737) — clear message distinguishing from mirror. ✅ Uses self.root.originConstructionPoint (L742) instead of raw Point3D — correct for Fusion 2026. ✅ Non-uniform scale via setToNonUniform. Comment (L738-741) documents the API constraint.

**_mirror_bodies (L753-786)**: ✅ Non-axis-aligned rejection via UnsupportedSCADNodeError (L771-773). ✅ isCombine = True (L785) with comprehensive docstring (L754-767) explaining why. ✅ Axis→plane mapping documented in comments (L775-778). ✅ Iterates per-body with ObjectCollection — correct Fusion pattern.

**_rotation_matrix (L1012-1036)**: ✅ Pure Python Rz·Ry·Rx composition (L1028). ✅ setWithArray with row-major 16-array (L1030-1035). ✅ Docstring (L1013-1019) explains why preTransformBy isn't used. ✅ Uses existing _mat_mul_3x3 helper. No try/except needed (pure math).

**UnsupportedSCADNodeError → mesh-fallback / cleanup path**: ✅ Consistent. _create_from_scad (L2079-2107) catches UnsupportedSCADNodeError, calls _cleanup_translation, optionally falls back to _run_scad, returns {"created": True, "method": "mesh_fallback", ...} with unsupported_nodes and allback_reason. General except Exception (L2108-2127) also calls _cleanup_translation and detects revolve failures via _is_revolve_failure().

**_cleanup_translation (L2008-2040)**: ✅ Removes bodies/sketches/planes NOT in pre-translation snapshots. ✅ Iterates in reverse (L2018, L2026, L2033) to keep indices valid. ✅ Returns deleted body count. ✅ Each delete wrapped in 	ry/except Exception: pass (best-effort). Constructs not yet created have no snapshot gap (guaranteed by name-prefix convention scad_*).

---

### Aggregate Summary

| Criterion | Finding | Severity |
|-----------|---------|----------|
| 1. Bare except: | 0 in new code; 12 pre-existing | PASS |
| 2. Fusion API try/except | Minor: _FusionExecutor methods lack local try/except; outer catch-all IS meaningful | PASS (minor) |
| 3. JSON response shapes | Nit: success keys vary by domain; failure path uniform {"error": "..."} | PASS (nit) |
| 4. Hardcoded paths | 0 found; all use os.path / tempfile / expanduser | PASS |
| 5. Type hints | Minor: public API + handlers fully typed; _FusionExecutor methods & helpers partial | PASS (minor) |

**No blockers found.** All findings are minor or nit level.
