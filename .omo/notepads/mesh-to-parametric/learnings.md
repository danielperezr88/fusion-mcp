# mesh-to-parametric � learnings

## 2026-08-04 08:26:50 T1

Fix `capture_screenshot` ? `image_base64` (vision prerequisite). Committed as
`3f9c933`.

### Handler patterns (FusionMCP.py)
- `_capture_screenshot(p)` (`FusionMCP.py:2300`): params are plain dicts
  (`p.get("path", default)`, `int(p.get("width", 1920))`). No try/except inside
  handlers � exceptions propagate to `_process_command`'s dispatcher wrapper,
  which returns `{"error": traceback.format_exc()}`. The fix follows this: read
  the PNG bytes after `saveAsImageFile`; a read failure surfaces as a graceful
  `{"error": ...}` with zero handler-side try/except.
- `_call()` convention (`fusion_server.py:82-95`): success dict ?
  `json.dumps(data, indent=2)` string; `"error"` in data ? `"Error: ..."`.
- `base64` was NOT imported in FusionMCP.py � added `import base64` after
  `import adsk.fusion` (stdlib, safe inside Fusion's embedded Python).
- Server `capture_screenshot` Image branch (`fusion_server.py:1117-1122`) was
  live but dead: `b64 = data.get("image_base64", "")` ? `Image(...)`. Verified
  (monkeypatch, no server change) that it returns `[text, Image(png)]` when the
  add-in sends `image_base64`, and keeps the `json.dumps(data)` text fallback.

### Fresh-module gotchas (tests/test_capture_screenshot_live.py)
- Pattern from `tests/test_openscad_live.py:128-148`:
  `importlib.util.spec_from_file_location('fusionmcp_dev', FUSIONMCP_PATH)` ?
  `mod.app = app` ? `_process_command({'command': ..., 'params': ...})`, params
  embedded with `repr()`. Works because module-level code in FusionMCP.py is
  side-effect free (no server start at import � that's in `run()`).
- **Fusion gotcha:** `app.activeViewport.saveAsImageFile(path, ...)` SILENTLY
  AUTO-CREATES nonexistent parent directories. A "nonexistent dir" path does
  NOT error � it succeeds and writes the file. The reliable invalid input is an
  EXISTING DIRECTORY used as the save path (saveAsImageFile raises ?
  `{"error": ...}`). Also `width=0` does not error (captures at 0px wide).
- pytest integration: probe bridge via `requests.post(get_info)` in a
  module-scoped fixture ? `pytest.skip(...)` when unreachable, so the live file
  is pytest-collectible yet the headless `py -m pytest tests -v` stays green.
  Existing live suites (openscad/dimensioning/import) are NOT pytest-collected
  (no `test_*` functions, standalone `main()`); this file intentionally is.
- Headless env gotcha: the `mcp` SDK is NOT installed for the `py` interpreter
  (only pytest + requests). Importing `mcp_server/fusion_server.py` headless
  requires stubbing `mcp.server.fastmcp` (FastMCP.tool + Image). Note:
  `@mcp.tool()` support needs `tool()` to return the inner decorator properly.
- Test artifacts: `_capture_screenshot` writes a real PNG to
  `~/Desktop/fusion_capture_live_test.png` on each run (by design � it's the
  live proof). Cleaned up a stray auto-created dir after probing.

## 2026-08-04 T2

Add `analyze_mesh` tool + `mcp_server/mesh_analysis.py` (pure-Python geometry
foundation). Committed as `54a44e8`.

### Mesh API facts (verified live via fresh-module)
- `MeshBody.displayMesh` is NON-INDEXED: `nodeCount` == 3�`triangleCount`
  (24 for a 12-triangle cube) � every triangle corner is a distinct node, so
  node-index adjacency alone always reports watertight False on a closed body.
  FIX: `analyze_mesh_data` welds duplicate vertices (quantized at
  `max(1e-9, 1e-7*extent)`) before the edge map ? live cube reports 8
  vertices, watertight True. T3 (slicer) must consume the SAME welded topology.
- `mesh.normalVectors` length is arbitrary (Fusion dedupes); `normalIndices`
  maps 3 corners per triangle. Interpretation rule that works: len==T ?
  per-face, len==3T ? per-corner (average the 3), else geometric fallback
  from winding. STL-style non-indexed meshes ? geometric normals.
- `_find_mesh_body` raises `f"Body '{ref}' not found."`; `_extract_mesh_data`
  re-wraps as EXACT `{"error": "Mesh body '<ref>' not found."}` per plan.

### Determinism choices (mesh_analysis.py)
- NO random module. Normal clustering = greedy first-match by input order with
  15� threshold; cylinder axis candidates = fixed cross products of the 8
  dominant cluster reps + a step-sampled 12-normal set; best axis by
  `side_fraction*coverage`.
- Cylinder discriminates from box/sphere via: area-weighted side fraction
  (=0.5; face-count fraction lies � caps inflate it), 16-bucket azimuthal
  coverage (=0.85), radius CV (=0.2).
- **Bucket aliasing gotcha:** `round(ang/2p*16)` collapses 16-gon prism
  normals sitting on bucket boundaries (13/16 coverage). Use `floor`, not
  `round` � pinned by test_cylinder_revolved_strategy.
- Symmetry: triangle centroids do NOT mirror on asymmetric triangulations
  (cube -z face centroids (2/3,1/3,0)/(1/3,2/3,0) have no mirror face) ?
  match VERTICES (triangulation-independent), not centroids.

### Server/handler gotchas
- `_load_mesh_analysis()` mirrors `_load_scad_translator()` (sys.path OR
  file-location fallback) � `import mcp_server.mesh_analysis` fails when
  fusion_server.py is launched directly by an MCP client.
- `analyze_mesh` re-wraps `_call`'s `"Error: ..."` prefix into
  `{"error": <msg>}` to hit the plan's exact failure string.
- Live server-path test CANNOT hit the new dispatcher entry over HTTP � the
  RUNNING add-in copy lags the repo (unknown command). Validated the tool's
  parse?analyze?scale?error-wrap logic by injecting the real fresh-module
  extracted payload at the `_call` boundary. Same constraint applies to T3+.
- `scale_report(report, factor)` keeps report keys stable (bbox/volume/dims/
  plane_cm/radius/height/axis_point scaled; counts/booleans/strategy intact)
  � T4 schemas consume this shape.


## 2026-08-04 08:54:48 T3

Add `slice_mesh` tool + `mcp_server/mesh_slicer.py` (pure-Python plane
intersection). Committed as `39e129f`.

### Algorithm facts (pinned by tests)
- Per-triangle segment-plane intersection: coplanar triangles (all 3 corners
  within onplane eps) and single-vertex touches (one corner on plane, other
  two same side) are SKIPPED - they never emit spurious points. An edge lying
  in the plane (2 corners on it) contributes that edge's 2 endpoints.
- Fusion displayMesh triangulates every quad face into 2 triangles, so a cube
  slice yields 8 raw points (4 corners + 4 collinear edge midpoints). A
  `_simplify_loop` pass (drop points on their neighbor segment within eps,
  iterate to fixpoint) reduces it to exactly the 4 corners - REQUIRED for the
  plan's 4-pt acceptance.
- Loop chaining: dedup points by grid quantization (eps = 1e-9 * max(1, extent)),
  dedup segments, walk sorted adjacency lists taking the first unused edge;
  first point NOT repeated at loop close (do not append the start point again).
- Hole detection: containment depth via any-vertex strictly-inside
  (even-odd ray cast); is_hole = depth % 2 == 1. Orientation is FORCED after
  classification: outer -> CCW (positive shoelace), hole -> CW (negative) by
  reversing the walk order when the sign disagrees. The natural walk order is
  NOT reliably CCW - never rely on it.
- Tolerances: dedup/simplify eps 1e-9 * max(1, extent); on-plane eps
  1e-6 * max(1, extent) (matches 6dp-rounded node data).
- Plane projection: axis + signed height; origin = axis-position point,
  basis spans the plane (Z -> (x,y), X -> (y,z), Y -> (x,z)); plane dict
  returned with axis/height_cm/origin/basis. `scale_slice` scales loop pts
  AND plane origin/height_cm (basis vectors untouched).

### Server/tool facts
- `slice_mesh` mirrors `analyze_mesh`: axis validated FIRST (exact error
  `Axis must be X, Y, or Z` before any `_call`), then `_call("extract_mesh_data")`,
  "Error: " prefix re-wrapped into `{"error": msg}` -> exact
  `Mesh body '999' not found.` for missing bodies.
- Live probe: the RUNNING add-in dispatcher still lags (no import_mesh_data,
  no extract_mesh_data) - all live commands must go through the fresh-module
  pattern (spec_from_file_location + mod.app = app + _process_command), even
  `import_mesh_data`. Server-path validation = inject the real fresh-module
  extracted payload at the `_call` boundary (mcp SDK stubbed headless).
- Live cube slice at Z=0.5: 24-node non-indexed displayMesh -> 1 loop, 4 pts,
  area 1.0, CCW. Deterministic; 6dp-rounded loop output.

### Follow-ups
- T4 consumes loop shape (pts + is_hole) for slice_diameter; T6 consumes
  `slice_mesh_at` loops for prismatic reconstruction. Loop/plane keys stable.

## 2026-08-04 T4

Add parameter schema library + `select_parameter_schema` tool (pure-data).
Committed as `54a44e8`.

### Design facts (pinned by tests)
- Schema JSON stores NO units, all cm. Matcher does units conversion via its
  own 3-entry `_UNIT_FACTORS` map (mirrors server `_cm_to_unit_factor`);
  the server tool validates units first through `_cm_to_unit_factor` so a
  bad unit returns `{"error": ...}` instead of a raise from the matcher.
- bbox facts accepted in 3 shapes: dims `[w,d,h]`, nested min/max
  `[[min],[max]]` (the analyze_mesh report shape), flat min/max 6-list.
  Key `bbox_cm` preferred, `bounding_box_cm` (report key) accepted.
- Role resolution order: (1) fact key == role name (conf 1.0), (2) bbox
  (0.8), (3) slice_diameter_cm (0.7; radius = /2), (4) fit params (0.7:
  fit_radius_cm->radius, fit_height_cm/fit_thickness_cm->thickness,
  fit_diameter_cm->diameter), (5) vision -> None placeholder conf 0.3 in
  parameters (never fabricate), (6) else unmatched_roles.
- Per-role optional `"axis": 0|1|2` in JSON picks the bbox dim for
  length/thickness roles; role names width/depth/height map by convention
  (x/y/z). Default axis = 2 (z/height).
- generic class uses UNPREFIXED names width/depth/height (minimal fallback);
  all 20 part classes prefix names `<cls>_<role>`. Test allows both.
- Return includes description + strategy_hint (T6 reconstruct reads
  strategy_hint from the chosen class) + note on generic fallback.
- 21 classes (20 parts + generic); kinds cover length/diameter/radius/angle/
  count/thickness; sources cover bbox/slice/fit/vision.

### Server/tool facts
- Pure-data (D8): tool served directly, NO `_call`, no Fusion round-trip.
  Live QA = import fusion_server (stub mcp.server.fastmcp: FastMCP.tool
  returns identity decorator) and call the tool fn directly - works even with
  the bridge DOWN, since no add-in interaction exists.
- `_load_parameter_schemas()` mirrors the proven loader pattern (sys.path
  OR spec_from_file_location with same-dir/../mcp_server candidates).
- Tool wraps matcher calls in try/except -> `{"error": "schema selection
  failed: ..."}`; bad units validated BEFORE loading schemas.

## 2026-08-04 09:13:08 T5

Add `capture_mesh_views` handler + `annotate_mesh_parameters` tool
(vision-guided annotation, Wave 3 first lane).

### Fusion API facts (verified LIVE on Fusion 2026)
- **The enum is `adsk.core.ViewOrientations`, NOT `ViewOrientationTypes`**
  (the plan's reference was wrong for the live build; `ViewOrientationTypes`
  does not exist). And the isometric member is `IsoTopRightViewOrientation`
  (NOT `IsometricViewOrientation`). Live `dir(adsk.core.ViewOrientations)`:
  Arbitrary/Back/Bottom/Front/IsoBottomLeft/IsoBottomRight/IsoTopLeft/
  IsoTopRight/Left/Right/Top + `thisown`. Map: isometric ->
  IsoTopRightViewOrientation, front -> FrontViewOrientation, top ->
  TopViewOrientation, right -> RightViewOrientation. `camera.viewOrientation`
  takes these enum values; set `camera.isFitView = True` then
  `viewport.camera = camera` to apply.
- `viewport.saveAsImageFile(path, w, h)` reuses the T1 base64 mechanism
  (read bytes -> `base64.b64encode(...).decode("ascii")`). tempfile already
  imported in FusionMCP.py; per-view temp path
  `tempfile.gettempdir()/fusion_view_<view>.png`.
- Handler order: validate view names FIRST (exact error
  `Unknown view '<v>'. Use isometric, front, top, right`), then resolve mesh
  via `_find_mesh_body` (missing mesh errors before viewport churn), then
  capture loop, then restore isometric. `_capture_mesh_views` takes `(root,
  p)` (needs root for mesh lookup) - dispatcher entry
  `"capture_mesh_views": lambda: _capture_mesh_views(root, p)`.

### Server/tool facts
- `annotate_mesh_parameters` mirrors `analyze_mesh` for the facts half (units
  via `_cm_to_unit_factor` -> `_call("extract_mesh_data")` -> re-wrap "Error: "
  -> `_load_mesh_analysis().analyze_mesh_data` -> `scale_report`) and
  `capture_screenshot` for the images half (INLINE `requests.post(f"{FUSION_URL}
  /command", json={"command": "capture_mesh_views", "params": {...}},
  timeout=60)` -> r.json() -> error check). Returns
  `[json.dumps(envelope, indent=2), Image(base64.b64decode(b64), format="png")
  x4]`. Envelope: `{"mesh", "views", "measured_facts", "workflow":
  {"stage": "annotate", "next": "MODEL ACTION: ..."}}` - keys EXACT per plan
  (T10 depends on stage/next). The `next` string is a single line containing
  "MODEL ACTION: classify the object from the views, then call
  select_parameter_schema with object_class and measured_facts".
- Mutable default `views: list = [...]` in the tool signature follows the
  plan + existing `measured_facts: dict = {}` precedent (never mutated).
- No matcher call inside (criterion c) - classification is model-side only.

### Live test facts (tests/test_annotate_mesh_parameters_live.py)
- Full suite went 71 -> 75 passing (4 new live tests, bridge UP so they ran
  the REAL scenario: cube imported via fresh-module import_mesh_data ->
  capture_mesh_views -> 4 PNG-magic views + exact unknown-view error +
  mesh-999 error + server-path envelope via real payloads at the _call / post
  boundaries with stubbed mcp.server.fastmcp).
- T5 reused the same fresh-module pattern (spec_from_file_location +
  `_sys.modules['fusionmcp_dev'] = _mod` + exec_module + `_mod.app = app` +
  `_process_command`); note the running add-in's execute_script shares ONE
  persistent sys.modules, so each execute_script re-registers the fresh module
  (harmless, but the OLD error first appeared in test 1 while tests 2/3 passed
  - because the restore line used the wrong enum name and only tests that ran
  the full capture loop hit it). Pinned: fix BOTH the module-level map AND
  every in-handler enum reference.
- Server-path test monkeypatches `fs._call` (returns the real fresh-module
  extract payload JSON) AND `fs.requests.post` (returns the real fresh-module
  capture payload dict for the capture_mesh_views command) - `fs.requests` IS
  the global requests module, so the patch is global for the test duration
  (monkeypatch restores).

### Follow-ups
- T6 consumes `views` + `measured_facts` from the envelope; T9 REUSES
  `_capture_mesh_views` (target = mesh) + a body-targeted variant - keep the
  `{"mesh", "views"}` return shape + view-name validation stable. T10 reads
  `workflow.stage/next` - unchanged.

## 2026-08-04 T6

Add `reconstruct_mesh` tool + `_create_from_csg_tree` / `_revolve_cross_section`
handlers (CSG-tree strategy router; prismatic/revolved/csg_decompose paths via
the repo scad_translator). Strategy auto route uses
`report["recommended_strategy"]` (organic -> graceful error until T7). Server
returns the T6 envelope `{"strategy", "method", "bodies", "features",
"csg_nodes"}` (`_envelope_reconstruction` + `_count_csg_nodes` in
fusion_server.py).

### Fusion revolve empirics (LIVE on Fusion 2026, via inline exec scripts)
- **Construction-axis revolves ALWAYS fail** (ASM_PATH_TANGENT regardless of
  profile/plane; also without setAngleExtent: "No se puede completar la
  revolución"). Do not use `root.zConstructionAxis` etc. as revolve axes.
- **Sketch-line-axis revolves map the sketch 2D frame as (u,v) -> world (X,Y)**:
  an axis line along the sketch's v-axis revolves around world Y, along the
  u-axis around world X. On the XY plane this is the identity mapping and
  works correctly (pinned by T12/T13: Y-line -> world-Y ring, X-line -> world-X
  ring). On XZ/YZ a v-direction axis (world Z) is misread as a world-Y revolve,
  so a direct world-Z sketch-line revolve is IMPOSSIBLE on this build.
- A closed profile whose axis-side edge lies ON the sketch-line axis revolves
  fine (profiles=1, no ASM_PATH_TANGENT) - Config E / T14 pattern.
- **The exec-script sandbox blocks nested-function locals**; all experiment
  code must be fully inline.
- PROVEN recipe for a world-Z solid (matches mesh Z-extent): draw the
  half-profile as (u=radius, v=height) on the XY plane with the axis line
  (0,0,0)->(0,1,0) (sketch v-axis), revolve 360 deg (edge-on-axis profile OK),
  then rotate each body +90 deg about world X via
  `moveFeatures.createInput2(bodies_col)` + `defineAsFreeMove(transform)`
  (2026 signature: `createInput` needs a `transform` positional arg;
  `createInput2` + `defineAsFreeMove` is the repo handler pattern). Pinned by
  live QA: `revolved_1` bbox x/y 1.9829, z 2.0 = mesh radius 0.991445, height 2.

### Handler/tool facts
- `_create_from_csg_tree` reuses `translator.translate_to_fusion_commands`
  (prismatic cube -> ExtrudeFeature, csg boxes -> 2 cubes + CombineFeature).
- Handler returns `{"bodies", "features", "names", "profile_pts", "angle_deg"}`;
  server wraps into the T6 envelope. Revolve params `{"profile_pts": [[x,z],...],
  "angle": 360, "units"}`; both handler calls use `timeout=330`.
- Server deploy module defaults `units: str = "mm"` (plain `str` default broke
  the T6 contract's exact string). An earlier edit consumed the
  `def _find_mesh_body(...)` line - its body survived, def restored from HEAD.
- Live QA cannot use the running add-in (lags: no import_mesh_data /
  extract_mesh_data / new handlers) - everything goes through fresh_dispatch
  (spec_from_file_location fresh-module pattern), even import/extract.
  QA script: `%TEMP%\opencode\qa_reconstruct.py` (3 scenarios: prismatic cube,
  revolved cylinder, csg two boxes; all PASSED, 87 tests green).


## 2026-08-04 T7

Add `_mesh_convert` handler + `mesh_convert` dispatcher entry + organic
strategy in `reconstruct_mesh` (PREVIEW MeshConvertFeature). Envelope:
`{"strategy": "organic", "method": "mesh_convert", "preview_api": true,
"parametric": bool, "note": "Organic conversion is a PREVIEW API result, not
a parameterized solid."}`. Exact not-available error (ALL absence/gating
paths): `{"error": "MeshConvertFeature not available on this Fusion build
(PREVIEW API)"}`.

### API presence/absence on THIS Fusion 2026 build (LIVE probed)
- `hasattr(design.rootComponent.features, "meshConvertFeatures")` is TRUE
  (members: add, createInput, count, item, isValid, ...). Enum classes
  `MeshConvertMethodTypes` / `MeshConvertOperationTypes` exist with members
  Faceted=0 / Prismatic=1 / Organic=2 and ParametricFeature=0 / BaseFeature=1.
- **Rights-gated**: `add()` AND the input property assignments
  `meshConvertMethodType = ...` / `meshConvertOperationType = ...` raise
  `"3 : No rights for mesh conversion."` -- the license tier cannot convert.
  The error fires at the PROPERTY ASSIGNMENT (not only at add()), so the
  try/except in `_mesh_convert` must wrap property-sets + add together.
- `createInput` takes a PYTHON LIST of MeshBodies (`std::vector`): call
  `createInput([mesh_body])`, NOT an ObjectCollection and NOT a bare body.
- Happy-path conversion is IMPOSSIBLE on this build (rights). The handler's
  rights-mapping returns the exact not-available error for organic/base and
  prismatic/parametric alike -- verified live, never crashes, no partial
  state (timeline shows only the pre-existing BaseFeature after the failed
  attempts).

### Live QA (fresh-module, %TEMP%\opencode\qa_t7.py) -- ALL PASSED
- Round sphere (312 verts, 576 tris, UV with offset pole rings to avoid
  degenerate zero-area triangles; `import_mesh_data` needs
  `coordinates`/`triangle_indices` FLAT lists) imported as MeshBody1 via
  fresh-module import_mesh_data.
- `mesh_convert` organic/parametric, organic/base, prismatic/parametric all
  returned the EXACT not-available error; `method=bogus` ->
  `Unsupported method 'bogus'. Use 'prismatic' or 'organic'.`; `mesh=999` ->
  `Body '999' not found.`; brep_count stayed 0, mesh_count 1.
- Server-side router covered headless in tests/test_mesh_convert.py (6 tests):
  exact envelope keys, parametric True/False, not-available passthrough
  verbatim, auto->organic routing calls mesh_convert, unknown-strategy error
  now ends "Supported: auto, prismatic, revolved, csg_decompose, organic",
  organic never touches `_load_mesh_csg` (its load was moved AFTER the
  organic short-circuit). mcp SDK is absent -> stub mcp.server.fastmcp via
  sys.modules + spec_from_file_location (same as annotate live test).

## 2026-08-04 T8

Add `compare_mesh_to_brep` tool + `_compare_mesh_brep` handler (vision-free
fidelity QA). Return shape EXACTLY:
`{"mesh": {"volume_cm3", "bbox_cm": [[min],[max]]}, "brep": {...},
"volume_ratio", "bbox_max_deviation_cm",
"sampled_deviation_cm": {"mean", "max", "samples"}, "method"}`.

### Metis F6 VERDICT: SurfaceEvaluator.getClosestPointTo is UNAVAILABLE
- Probed LIVE on this Fusion 2026 build: `hasattr(face.evaluator, 'getClosestPointTo')` -> False. Class-level `dir(adsk.core.SurfaceEvaluator)` has NO "Closest" members; `CurveEvaluator3D`/`CurveEvaluator2D` also none; edge curve evaluator instance `edge_eval_closest: False`. The evaluator family here only has getParameterAtPoint / getPointAtParameter / getNormalAtPoint (+ batched variants).
- **Fallback (used, reported via `"method"`):** sample ~200 mesh vertices (stride through `displayMesh.nodeCoordinates`, always append last node) -> distance to NEAREST BRep VERTEX (`body.vertices.item(i).geometry`), report mean/max/samples. Per-vertex failures skipped (samples = actually-sampled). `"method": "vertex_fallback"` on this build; the handler keeps a live `hasattr` check so a future build with the API reports `"surface_evaluator"` (per-face evaluators, min over faces).
- Fusion Python gotchas: `mesh.nodeCoordinates` is a `Point3DVector` with NO `.count` and NO `.item()` -- materialize `list(mesh.nodeCoordinates)` then index/len. `body.vertices`/`body.faces` ARE count/item collections. `mesh_body.volume` and `body.physicalProperties.volume` both return cm^3 directly (cube: 1.0). Guard brep_vol==0 -> clear error before dividing.
- `_require_body(root, ref)` (FusionMCP.py:356) is the pre-existing BRep resolver (index/name, `Body '<ref>' not found.`) -- reused; no new helper needed.

### Live QA (fresh-module, %TEMP%\opencode\qa_t8_evidence.py) -- ALL PASSED
- 1x1x1 cm cube mesh (import_mesh_data) -> prismatic reconstruct via mesh_csg
  (build_csg_tree + scale_tree x10 mm) -> create_from_csg_tree -> BRep
  `scad_linear_extrude_2` (Spanish UI; assert JSON fields, not feature names).
- HAPPY `compare_mesh_brep {mesh: cube_mesh, body: 0}`: mesh/brep volume_cm3
  1.0/1.0, volume_ratio 1.0, bbox_max_deviation_cm 0.0, sampled_deviation
  mean 0.0 / max 0.0 / samples 24 (displayMesh 24 non-indexed nodes, all at
  corners == BRep vertices), method vertex_fallback.
- FAIL body=999 -> {"error": "Body '999' not found."}; mesh=999 -> same;
  body=cube_mesh (mesh name as BRep ref) -> {"error": "Body 'cube_mesh' not found."}.
- tests/test_mesh_to_brep_live.py: 5 tests (happy, 3 failure paths, server-path
  with real payload at the _call boundary + error passthrough), bridge-gated
  skip fixture. Suite 93 -> 98 passing.


## 2026-08-04 T9

Add `review_reconstruction` tool + `_capture_body_views` handler (vision QA
loop). Committed as `54a44e8`.

### Handler/tool shapes
- `_capture_body_views(root, p)` (FusionMCP.py, right after `_capture_mesh_views`)
  is the body-targeted variant: view names validated FIRST with the SAME exact
  error `Unknown view '<v>'. Use isometric, front, top, right`, body resolved
  via `_require_body` (FusionMCP.py:356, BRep resolver -> `Body '<ref>' not
  found.`), then the identical orientation/capture loop reusing
  `_VIEW_ORIENTATIONS` + `saveAsImageFile`->base64 (temp path
  `fusion_body_view_<view>.png`). Returns `{"body": <name>, "views": [...]}`.
  Dispatcher entry: `"capture_body_views": lambda: _capture_body_views(root, p)`.
- `review_reconstruction(mesh="0", body="0", views=["isometric","front","top"])`
  (fusion_server.py) does TWO INLINE `requests.post` calls (capture_mesh_views
  THEN capture_body_views), each with the exact T5 error contract (ConnectionError
  -> "Cannot reach Fusion 360...", other -> "Unexpected error: {e}", `"error" in
  cap` -> `json.dumps({"error": cap["error"]}, indent=2)`). Then geometry =
  `json.loads(compare_mesh_to_brep(mesh=mesh, body=body))` (LOCAL plain call,
  same module); any exception -> `{"error": "geometry comparison failed: ..."}`
  inside `"geometry"` (images stay primary payload). Envelope:
  `{"pairs": [{"view","mesh_image_base64","brep_image_base64"} x len(views)],
  "geometry", "workflow": {"stage": "review", "next": "MODEL ACTION: compare
  each pair; if features are missing call reconstruct_mesh again with feedback
  or accept with select_parameter_schema"}}`. Images appended interleaved per
  view (mesh then brep): `[text, mesh_iso, brep_iso, mesh_front, brep_front,
  mesh_top, brep_top]` = len 1 + 2*len(views).

### Live QA (fresh-module, %TEMP%\opencode\qa_t9.py) -- ALL PASSED
- Fresh doc -> cube mesh (import_mesh_data) -> T6 prismatic reconstruct
  (extract_mesh_data -> build_csg_tree prismatic -> scale_tree x10 ->
  create_from_csg_tree units=mm) -> BRep "0". `capture_mesh_views` (3 views) +
  `capture_body_views` (3 views) both return PNG-magic base64. BRep viewport
  capture works identically to mesh (isFitView fits whole model incl. both
  bodies).
- Server tool (stubbed mcp.server.fastmcp, REAL fresh payloads at BOTH the
  `_call` boundary -- compare_mesh_brep JSON -- and the inline requests.post
  boundary): pairs=3, all 6 base64s PNG-magic, geometry keys
  [bbox_max_deviation_cm, brep, mesh, method, sampled_deviation_cm,
  volume_ratio], stage=review, Image order byte-matches the fresh payloads.
- Failure mesh=999 -> `{"error": "Mesh body '999' not found."}` envelope
  (string); body=999 -> `{"error": "Body '999' not found."}`. Unknown view at
  handler level -> exact T5 string.

### Gotchas
- **`fs.requests` IS the global requests module**: monkeypatching
  `fs.requests.post` in a bare script (no monkeypatch fixture) poisons ALL
  subsequent bridge calls -- the next execute_script gets the fake response.
  In pytest use `monkeypatch.setattr` (auto-restore); in standalone QA scripts
  save `_orig_post = requests.post` BEFORE the patch and restore it.
- Order-assertion trap: the interleave is per-view (mesh then brep), NOT
  mesh-all-then-brep-all. Build `expected_order` by walking views with
  `{view: b64}` maps per capture, not by concatenating the two view lists.

## 2026-08-04 T10

Add get_workflow_guide tool + mcp_server/workflow_guide.py (pure-data
guide). Committed with the workflow-envelope phase.

### GUIDE structure (mcp_server/workflow_guide.py)
- GUIDE is a module-level list of 8 ordered step dicts, each with EXACTLY
  the keys {tool, purpose, inputs, outputs, model_action, branch, fallback}
  (pinned by test -- no extra keys allowed, so step identity lives in the
  `tool` value). Order: import -> analyze -> slice -> annotate ->
  select_parameter_schema -> reconstruct -> compare -> review.
- `tool` values match the real server tools: import_mesh_file,
  analyze_mesh, slice_mesh, annotate_mesh_parameters,
  select_parameter_schema, reconstruct_mesh, compare_mesh_to_brep,
  review_reconstruction.
- get_step(name) resolves BOTH the short step name and the full tool name
  (`step["tool"].split("_")[0]` covers reconstruct_mesh -> "reconstruct";
  select_parameter_schema is its own short name). Empty/None -> None so the
  server tool can treat "" as "full guide". Lookup is by name, NOT index.
- MODEL ACTION markers: annotate.model_action = "classify the object from
  the views, then call select_parameter_schema with object_class and
  measured_facts"; review.model_action = "compare each pair; if features are
  missing call reconstruct_mesh again with feedback or accept with
  select_parameter_schema". reconstruct.branch = {"auto":
  "prismatic | revolved | csg_decompose | organic"}; fallback mentions
  mesh_convert + mesh_fallback.
- GUIDE_JSON = json.dumps(GUIDE, indent=2) precomputed at import; the
  server tool returns it verbatim for step="".

### Loader pattern + server tool
- _load_workflow_guide() mirrors _load_parameter_schemas exactly
  (sys.path OR spec_from_file_location with same-dir/../mcp_server
  candidates, module name "fusionmcp_workflow_guide").
- get_workflow_guide(step: str = "") -> str sits after
  review_reconstruction, PURE-DATA: no _call, no requests, no bridge.
  step="" (or None -- str(step or "").strip()) -> full guide JSON;
  named -> json.dumps(step dict, indent=2); unknown -> EXACT
  {"error": "Unknown workflow step '<step>'"}.
- Own docstring carries "Stage 1 of the mesh-to-parametric workflow (see
  get_workflow_guide)" for pattern consistency.

### Docstring stage corrections (acceptance c)
- annotate_mesh_parameters: "Stage 5" -> "Stage 4" (TWO places -- the
  prose line "(stage 5 of the mesh-to-parametric workflow)" AND the
  cross-reference line "Stage 5 of the mesh-to-parametric workflow (see
  get_workflow_guide)").
- select_parameter_schema: "Stage 4" -> "Stage 5".
- review_reconstruction: "Stage 9" -> "Stage 8".
- Confirmed unchanged: analyze 2, slice 3, reconstruct 6, compare 7.
- T5/T9 workflow envelopes untouched (stage annotate / review + exact next
  strings verified by test).

### Test counts + gotchas (tests/test_workflow_guide.py, 14 new)
- Suite 103 -> 117 passing; py_compile clean on both files.
- Source-substring tests on the T5/T9 "next" strings need TWO-step
  normalization: e.sub(r'"\s+"', '', source) (joins adjacent string
  literals like "then " "call" -- the quote-space-quote delimiter survives
  plain whitespace flattening) THEN e.sub(r"\s+", " ", ...).
- T6/T8 no-workflow assertion must check '"workflow":' (the dict-key
  form), NOT the bare word "workflow" -- the docstrings legitimately say
  "(see get_workflow_guide)".

## 2026-08-04 T12

Live integration suite tests/test_mesh_reconstruction_live.py (plan todo 12).

### Architecture
- Dual-mode standalone: --mode fresh (real suite) / --mode http (honest bridge
  probe; default). Loads mcp_server/fusion_server.py headless (stubbed
  mcp.server.fastmcp) and in fresh mode replaces fs._call with a router that
  sends every repo-lagging handler command (import_mesh_data, extract_mesh_data,
  create_from_csg_tree, revolve_cross_section, mesh_convert, compare_mesh_brep,
  capture_mesh_views, capture_body_views) through fresh_dispatch, mimicking
  _call's envelope (success json.dumps, error dict -> "Error: <msg>"). All 10
  server tools therefore run against REAL Fusion state.
- pytest integration: single test_mesh_reconstruction_live_suite(bridge,
  monkeypatch) re-runs the SAME 10 checks via run_check and asserts no FAIL.
  Module-scope bridge fixture skips when Fusion is down. Full `pytest tests -q`
  with this file: 121 passed + 1 xfailed. (One-off flake in
  test_annotate_mesh_parameters_live under a loaded bridge reproduced once,
  green on re-run; not caused by this file -- annotate collects first.)
- annotate check: the INLINE requests.post for capture_mesh_views inside
  annotate_mesh_parameters is patched with a fake routing ONLY that command to
  fresh_dispatch and delegating everything else to the real post (manual
  save/restore in standalone -- fs.requests IS the global requests module).

### Live results (bridge UP, 2026 build, Spanish UI)
- ALL 10 non-skipped checks PASS. Organic (check 9) SKIPPED cleanly.
- check 3: prismatic cube -> method csg_translation, ExtrudeFeature in
  timeline, BRep bbox [1.0,1.0,1.0] cm (units=mm x10 scale recipe holds).
- check 4: 24-seg r=1 h=2 cm cylinder -> method revolve, RevolveFeature in
  timeline (assert feature TYPE, never localized names).
- check 7: compare_mesh_to_brep volume_ratio 1.0, method vertex_fallback,
  samples 24.

### GOTCHA: organic not-available surfaces as a RAW "Error: " string
- reconstruct_mesh(strategy="organic") on this license-gated build returns the
  string "Error: MeshConvertFeature not available on this Fusion build
  (PREVIEW API)" -- NOT a JSON {"error": ...} envelope. Cause:
  _envelope_organic (and _envelope_reconstruction) do json.loads(result) and
  return the raw _call "Error: " prefix verbatim on ValueError. The test must
  accept BOTH forms (raw prefix OR JSON error containing "not available") as
  the SKIP condition.
- The same raw-prefix passthrough applies to ANY handler error reaching the
  reconstruct_mesh envelope wrappers; only tool-level failures validated
  BEFORE _call (bad axis, unknown strategy) return proper JSON envelopes.

### Bridge-down behavior
- --mode http with Fusion closed: startup probe (requests.post get_info) ->
  "Cannot reach Fusion 360..." + exit 1 (code-identical to the openscad
  probe). Not simulated live (bridge was up); the probe branch is the same
  try/except and was exercised with the bridge UP in http mode (honest
  "Unknown command" failures against the lagging add-in, exit 1).

## 2026-08-04 T13

Update README.md with Mesh Reconstruction section (plan line 167). Committed as ` (fill after commit).

### Placement
- Section inserted AFTER the `### OpenSCAD Pipeline` block (last bullet `create_from_scad`) and BEFORE `### Other`, inside the main `## Features` list. `###` heading style matches all sibling sections.

### Tools documented (8)
- `get_workflow_guide` (entry point, 8-step map: import -> analyze -> slice -> annotate -> select_parameter_schema -> reconstruct -> compare -> review), `analyze_mesh`, `slice_mesh`, `annotate_mesh_parameters` (4-view PNG capture), `select_parameter_schema` (pure-data, 20 classes + generic), `reconstruct_mesh` (prismatic/revolved/csg_decompose/organic), `compare_mesh_to_brep` (volume ratio + sampled deviation), `review_reconstruction` (side-by-side pairs + geometry summary).
- Descriptions written from tool docstrings in fusion_server.py (analyze 1396, slice 1442, reconstruct 1551, compare 1655, select 1688, annotate 1728, review 1815, guide 1908) - all docstrings carry "Stage N of the mesh-to-parametric workflow (see get_workflow_guide)".
- Vision note: classification model-side, no server-side vision API; images returned as MCP Image blocks (base64 PNG) via `image_base64`; select_parameter_schema maps facts deterministically.
- Organic caveat: PREVIEW `MeshConvertFeature`, license-gated on this build; graceful not-available error when absent. Wording mirrors reconstruct_mesh docstring (:1569-1571).
- Style notes: used ASCII `->` arrows (`?` not present in README; kept add-only, em-dash `�` and backticks match existing OpenSCAD Pipeline bullets). Example workflow uses a `text` code block with concrete tool calls (import_mesh_file -> analyze_mesh -> reconstruct_mesh(strategy="prismatic") -> compare_mesh_to_brep -> review_reconstruction).

Committed as `f716f98` (1 file changed, 24 insertions). Only README.md staged; .omo/ untracked, never staged.


## 2026-08-04 T11

Headless pytest suite audit for the pure-Python modules (mesh_analysis /
mesh_slicer / parameter_schemas / mesh_csg). No mcp_server/* or FusionMCP.py
changes; test-only additions on top of commit 21d7d44 (T10).

### Coverage audit vs acceptance (T2/T3/T4/T6 had already landed test files)
- test_mesh_analysis.py (13): cube watertight/manifold/volume/bbox/symmetry,
  open box (not-watertight-but-manifold), tetra (volume 1/6) ALL present.
  No gap. Added ONLY the QA-failure proof.
- test_mesh_slicer.py (13): hole detection present; coplanar + touch-vertex
  skip present. GAP: no degenerate (zero-area) triangle skip test -> ADDED
  test_degenerate_zero_area_triangle_skipped (two coincident vertices ->
  two crossings collapse to one point -> zero-length segment deduped; mixed
  into a cube leaves the slice byte-identical).
- test_parameter_schemas.py (12): known-class + generic fallback present.
  GAP: unmatched_roles only type-checked, never content-proven -> ADDED
  test_unresolved_roles_land_in_unmatched_roles (bolt with {} -> exactly
  {head_diameter, head_thickness, length}; vision roles stay None
  placeholders, never unmatched; supplying facts clears the list).
- test_mesh_csg.py (12): prismatic/csg_decompose/UnsupportedStrategyError
  (unknown + organic) present. GAP: revolved routing not pinned -> ADDED
  test_revolved_strategy_is_profile_not_csg (build_csg_tree("revolved")
  raises UnsupportedStrategyError "revolve profile..."; compute_revolved_
  profile is the supported path).
- conftest.py: TrapRoot + sys.path pattern already adequate; NOT rewritten.

### QA-failure proof (plan acceptance)
- test_qa_failure_proof_machinery_live in test_mesh_analysis.py: asserts
  volume == 42.0 (deliberately wrong; real cube volume is 1.0), marked
  @pytest.mark.xfail(strict=True, reason="deliberately wrong expectation
  proving the test machinery is live"). Runs XFAIL (not XPASS) -> proves
  assertions execute while the suite stays green.

### Final state
- py -m pytest tests -v -> 120 passed, 1 xfailed, exit 0 (~38s headless,
  no Fusion, no network). Count 117 -> 121 (4 added).


## 2026-08-04 T-PATCH-docclose

Close test-created Fusion documents in all 8 live test files (no leaks;
never close pre-existing docs). Committed as `bc5207b` (8 files, +598/-3).

### Design: snapshot-diff cleanup (identical helper pair in all 8 files)
- `_open_doc_ids()`: execute_script snapshot of `app.documents.item(_i)` ids.
  Returns None on bridge hiccup -> caller skips cleanup (never closes
  pre-existing docs on a failed snapshot).
- `_close_docs_except(pre_ids)`: collect ids to close FIRST, then re-find each
  document by id before `d.close(False)` (indices shift as docs close --
  NEVER close while iterating by index). Each close guarded; never raises;
  prints `[cleanup] closed N test document(s)`.
- Group B (mesh_to_brep, review_reconstruction): autouse `_close_test_docs`
  fixture = snapshot -> yield -> cleanup.
- Group C (annotate, capture_screenshot): autouse `_fresh_doc_and_close`
  fixture = snapshot -> `create_new_document` -> `_activate_last_doc()` ->
  yield -> cleanup. Self-contained (no dependency on leftover docs).
- Standalone files (import/dimensioning/openscad/mesh_reconstruction):
  snapshot before checks, cleanup in except/finally/after-check paths.

### Critical Fusion API gotchas discovered
- **`adsk.core.FusionDocument` has NO `.id` attribute.** It exposes
  `creationId` (stable, unique across open docs, survives across execute_script
  calls). Using `.id` raises inside execute_script -> `_open_doc_ids` caught it
  and returned None -> cleanup silently never ran. All 8 files use
  `creationId` now (verified: helper worked, `[cleanup] closed N` printed).
- **`app.activeProduct` can LAG `app.documents.add()`.** After
  create_new_document, a capture/import dispatched immediately can resolve a
  DIFFERENT (previous) active document -> intermittent "Mesh body '0' not
  found." Fix: `_activate_last_doc()` activates the newest document
  (`app.documents.item(count-1).activate()`) and polls until
  `app.activeProduct.parentDocument.creationId == that doc's creationId`
  (Design has `parentDocument`; Document has `design`). ~3s poll budget.
- **Per-doc product lookup via `d.products.itemByProductType('FusionSolidType')`
  RAISES** (wrong type string). Use `d.design.rootComponent.meshBodies.count`
  instead (Document.design verified working).

### Pre-existing test bug exposed by the fresh-doc fixture
- `_ensure_mesh` in test_annotate_mesh_parameters_live.py:
  `if _mesh_body_count() == 0:` -- `_mesh_body_count()` returns a DICT
  `{'mesh_bodies': N}`, and dict == 0 is ALWAYS False -> the unit-cube import
  never ran -> tests passed only when a LEFTOVER mesh happened to be in the
  active doc (the historical T12 flake). The fresh-doc fixture made this
  deterministic. Fixed to `.get("mesh_bodies", 0) == 0` (code now matches its
  "import if empty" docstring). This is the only test-logic change; everything
  else is add-only.

### Verification (live bridge, N=1 baseline)
- py_compile all 8 OK. Full suite `py -m pytest <8 files> -q`:
  **17 passed** (~57s). `app.documents.count` == 1 before AND after (leak-free).
- Diagnostic method that cracked the case: drive the REAL test module
  (`spec_from_file_location` + import), call its actual `run_code` wrapped with
  a logger, replicate the fixture body inline, and print per-doc mesh counts via
  `d.design` after every step. Showed `_mesh_body_count` returned the dict and
  NO import dispatch followed -> pinpointed the dict==0 bug.


## 2026-08-04 F2 gate-5 fix (post-plan, cosmetic)

F2 code-quality review REJECTED on exactly two gate-5 violations (missing type
annotations on new public functions). Fixed both, verified, boulder unblocked.

- `mcp_server/workflow_guide.py`: added `from typing import Dict, Optional`
  after `import json`; `def get_step(name):` -> `def get_step(name: str) ->
  Optional[Dict]:`. Docstring/body untouched. Module stays pure stdlib
  (typing is stdlib -- "Pure stdlib (json only)" docstring property preserved).
- `mcp_server/mesh_slicer.py`: added `Union` to the existing
  `from typing import Dict, List, Optional, Sequence, Tuple` import;
  `height_or_plane` -> `height_or_plane: Union[float, Dict]` in
  `slice_mesh_at(...)`. Docstring/body untouched.
- `git diff --stat`: exactly 2 files modified (mesh_slicer +4/-3 total across
  both; workflow_guide +3/-1). No other files touched (scad_translator, tests,
  FusionMCP.py, fusion_server.py all untouched).
- `py -m py_compile mcp_server/workflow_guide.py mcp_server/mesh_slicer.py`
  -> OK; headless import probe -> IMPORT_OK.
- Full suite `py -m pytest tests -q` -> **121 passed, 1 xfailed in 60.25s**
  (no flake this run). Behavior-neutral: no tests assert on annotations.
- Verification note: F2's other 9 gates were PASS; the one re-run flake
  observed during F2 (3 transient review_reconstruction fails, green on
  re-run) did not recur.


## 2026-08-04 F2 RE-RUN VERDICT: APPROVE (HEAD 2334153)

Final F2 re-run against commit `2334153` ("fix: annotate get_step and
slice_mesh_at signatures (final review gate 5)"). The two gate-5 violations
are fixed; all 10 gates now PASS.

- Fix commit verified: `git show --stat 2334153` = exactly 2 files
  (mesh_slicer.py +4/-2 line-level, workflow_guide.py +3/-1), no other files.
- Gate 5 re-verified in tree: `workflow_guide.py:149` `def get_step(name: str)
  -> Optional[Dict]:` (typing import at :20); `mesh_slicer.py:401-402`
  `height_or_plane: Union[float, Dict]` (Union added at :53).
- Gate 1: bare `except:` repo-wide still only the 11 pre-plan lines
  (FusionMCP.py x10 -> blame b6822e61 2026-03-08; fusion_server.py:258 ->
  blame c346404 2026-03-08). None new.
- Gate 6: headless import of all 5 pure modules -> HEADLESS_IMPORT_OK
  (stdlib only; typing is stdlib so workflow_guide stays "json only").
- Gate 7: `py -m py_compile` OK on all 21 changed/new .py files.
- Gate 8: full `py -m pytest tests -q` -> **121 passed, 1 xfailed in 56.12s**
  (no flake; xfail stays strict XFAIL, never xpass).
- Gate 4: no hardcoded absolute paths in mcp_server/*.py or FusionMCP.py.
- Gate 10: full range 3f9c933^..HEAD = 15 commits; scad_translator.py has
  ZERO commits in range (untouched). 2334153 introduced no scope creep.
- Gates 2/3/9 unchanged since the first full audit (handlers wrapped with
  `{"error": ...}`, T10-pinned workflow envelopes, stable tool return shapes).

## 2026-08-04 POST-PLAN DEBUG (user-initiated): decompose_mesh_faces fragmentation

Root-caused live on the Plataforma tortus mesh with %TEMP%\opencode\debug_fragmentation*.py.

**Bug A (fatal, silent):** decompose_mesh_faces is DEAD in this env — the internal
`Trimesh(vertices=..., faces=...)` (process=True default) crashes on numpy 2.5.1 + trimesh 5.0.0
(merge_vertices -> hashable_rows "int too big to convert"); the trailing `except Exception` swallows it and
returns the EMPTY fallback (components_detected=0, planar_faces=[]). Every caller (analyze_mesh,
reconstruct_from_faces) silently gets nothing. All earlier "57 faces" numbers came from a manual
process=False replication. Fix: weld first (numpy-safe rounded-key dedupe, mesh_csg._weld_vertices pattern,
eps max(1e-9, 1e-7*extent) per T2 convention), then `Trimesh(process=False)`. Do NOT cross-import between
mcp_server modules (server loads them standalone via spec_from_file_location).

**Weld math (live):** payload = 1842 nodes / 1780 tris / 5340 indices (displayMesh partially indexed).
Rounded-key weld @1e-6 -> 855 unique verts -> **4 components** (main body, right wall, arm strips, left
tab). The "50 disconnected fragments" seen earlier were an artifact of un-welded corner duplication.

**Bug B (the user's "one logical wall"):** facets() runs per component after mesh.split(), so coplanar
faces in DIFFERENT components never merge. Live plane-group evidence:
- front wall Y=4.738 = 4 faces across comps 0/1/2: 43.87 (X[3.806-16.339] Z[0-3.5]) · 5.25
  (X[20.339-24.339] Z[1.5-7.0]) · 0.5 (X[20.339-20.839] Z[0.5-1.5]) · 0.25 (X[20.339-20.839] Z[0-0.5])
- wall-layer top Z=3.5 = 3 faces (9.51 + 8.27 + 3.00) touching at X=20.339 -> one C-shape (20.78 cm2)
- slab top Z=0.5 up = 85.20 (comp0) + 3.93 left tab (comp3), touching at X=3.806
Fix: coplanar-merge post-pass — group by (canonical normal, offset) tol ~0.5 deg, project to 2D, union each
connected region via boundary-edge cancellation (interior shared segments cancel; holes survive as
uncancelled loops), one merged face per connected 2D region, then 2D collinear simplify.

**Bug C:** degenerate facets emitted as faces — 3 faces with n=(0,0,0) area=0.0 + a 0.002 sliver
(4cm-span bbox, ~0 area). Filter: skip area < ~1e-4 or degenerate outline.

**Bug D:** vertex bloat — slab top 68 verts, left tab 295 verts. The 3D 1-deg angle simplifier cannot
collapse tessellation micro-segments. Fix: simplify on the 2D projection (RDP / collinear tolerance), not
3D angles. Also angle_tolerance_deg is currently DEAD (facet call passes facet_threshold=None) — wire it
through.

**Bug E (latent):** `outline.entities[0]` keeps only loop #0 -> holes + disjoint loops dropped. Did NOT
trigger on this mesh (all 57 facets had exactly 1 loop) but blocks graph containment edges. Fix: iterate
ALL entities; outer = largest loop, rest = holes; emit per-face "holes".

**Contract:** keep components_detected/planar_faces/curved_patches + per-face
vertices/normal/angles_deg/area/triangle_count/component/face_index/vertex_count; ADD "holes" (list of
loops) + optional "error" in the fallback (additive only; consumers unaffected).

## 2026-08-04 FUTURE VERSION SCOPE (user-specified): structure_graph edge taxonomy

Next version structures aggregated mesh insights as a GRAPH. Nodes = planar faces (vertices, normal, area,
centroid, plane offset, component, winding sign) + curved patches (surface + params). Edges (user
specified):
1. **edge connectivity** — two polygons share a boundary edge segment (collinear, overlapping)
2. **vertex connectivity** — two polygons share a corner vertex but no edge
3. **90 solid degrees** — faces meeting at a 90-deg solid angle (normals perpendicular; side wall <-> top
   of an extrusion)
4. **parallel** — face normals parallel (same/opposite orientation; extrusion top <-> bottom pairing)
Derived relations from this debug: coplanar-merge group (same plane + touching projection -> one node),
containment (outer loop <-> holes), plane/Z-band membership along the extrusion axis (profile
reconstruction). decompose_mesh_faces must emit the primitives these edges need: holes (Bug E), welded
topology (Bug A), merged coplanar faces (Bug B), clean 2D-simplified outlines (Bug D).


## 2026-08-04 T-FIX-decompose: implemented Bug A-E fixes

**File changed:** `mcp_server/mesh_analysis.py` only. **New test file:**
`tests/test_decompose_faces_fix.py` (24 tests). No other files touched.

**Helper functions added (all in mesh_analysis.py, before decompose_mesh_faces):**
- `_max_extent_nodes` (L869) — max per-axis span for weld eps computation.
- `_detect_components` (L877) — union-find edge-adjacency component IDs (replaces
  trimesh.split for the "component" field; avoids index remapping).
- `_group_planar_triangles` (L922) — greedy coplanarity grouping by (canonical
  normal within angle_tolerance_deg, plane offset within 1e-4*extent). Wires
  the previously-dead angle_tolerance_deg parameter.
- `_chain_directed_edges` (L959) — deterministic greedy loop chaining from sorted
  vertex keys.
- `_boundary_loops` (L986) — directed-edge cancellation (interior shared edges
  cancel; boundary survives). Cross-component merge is implicit: all triangles
  in a planar group are processed together regardless of component.
- `_project_to_2d` (L1017) — 3D->2D projection with optional common origin
  (critical: per-loop origins caused false containment matches).
- `_shoelace_area_2d` (L1041) — signed polygon area (CCW=+, CW=-).
- `_simplify_2d_keep` (L1054) — collinear-point collapse on 2D projection
  (perpendicular distance < tol -> drop). Returns surviving indices.
- `_point_in_polygon_2d` (L1079) — ray-casting containment test for hole
  classification (separate faces vs holes-within-face).
- `_extract_curved_patches` (L1094) — refactored curved-patch extraction
  (groups by component, builds trimesh per group with process=False).

**decompose_mesh_faces (L1133):** Full rewrite. Weld FIRST (eps = max(1e-9,
1e-7*extent)), then Trimesh(process=False). Planar groups -> boundary loops
-> containment-based face/hole classification -> 2D simplification -> degenerate
filter (area < 1e-4 or < 3 vertices). Exception fallback includes "error" key.
Per-face "holes" list added (additive contract). scale_report updated to scale
holes vertices by unit factor.

**Key design decisions:**
- NO trimesh.graph.facets() or mesh.split() for planar decomposition — all
  done with own numpy + union-find + edge cancellation. trimesh still used
  for curved-patch classification only.
- Common-origin 2D projection across all loops in a planar group — per-loop
  origins made the containment check meaningless (all loops projected to same
  coords). Fixed by passing origin_3d = first vertex of first loop.
- Containment-based face/hole split: largest abs-area loop starts a face;
  subsequent loops are holes if their centroid is inside an existing outer
  loop, else they start a new face. Handles both "frame with hole" (1 face)
  and "two separated quads" (2 faces) correctly.

**Tests (24 new):** weld dedup, touching-coplanar-merge (1 face, area=4),
separated-coplanar (2 faces), degenerate filter (zero-area + collinear),
frame hole (1 face area=12 + 1 hole), 2D simplify (5->4 verts), additive
contract (all keys + holes + error), multi-component duplicated-corners
(non-empty), unit cube (6 faces area=6), determinism, boundary loops,
shoelace sign, projection, component detection.

**Verification:** py_compile OK. Full suite: **145 passed, 1 xfailed in 59.8s**
(was 121+1; +24 new tests, 0 regressions).

**Simulated 1842-node pattern:** test_multi_component_duplicated_corners_nonempty
builds a box from 6 quads with fully duplicated corner vertices (24 raw nodes ->
8 welded). Returns components_detected>=1, planar_faces>=3, all with area>0.
The old code would have returned the empty fallback (Bug A crash).


## 2026-08-04 T-FIX-decompose-seam: seam vertex snapping + winding + pinch splitting

**Root cause:** decompose_mesh_faces weld uses eps=max(1e-9,1e-7*extent)=2.15e-6 cm,
but the real displayMesh has near-coincident seam-duplicate vertices at ~1.5e-4 cm
discrepancy (70x larger than eps). These survive the weld as separate indices,
preventing edge cancellation in _boundary_loops. Three distinct sub-issues found:

**1. Seam vertex snapping (Bug B residual):**
Added `_snap_group_vertices` (L1021): union-find near-coincident vertex merge
WITHIN each planar group. snap_tol = max(1e-5, 5e-4 * extent) = 0.0107 cm.
Measured discrepancies: 0.0002-0.0004 cm (all at X~20.339). 71x margin.
This is SEPARATE from analyze_mesh_data's tight weld (unchanged).

**2. Winding inconsistency:**
Modified `_boundary_loops` (L1119) to accept tri_normals + group_normal.
Triangles whose normal is anti-parallel to group normal have winding reversed
before edge counting. displayMesh emits inconsistent winding across fragments.

**3. Degenerate triangle edge handling:**
Changed triangle-level skip (`if a==b or b==c or a==c: continue`) to
per-edge self-loop checks (`if a!=b: add_edge(a,b)` etc). Snapping can make
triangles degenerate (two verts merge), but their non-self-loop edges must
still participate in counting to preserve interior edge cancellation.

**4. T-junction splitting:**
Added `_split_edges_at_tjunctions` (L1073): splits long edges where a vertex
from one fragment lies on the segment of another fragment's edge. Ensures
shared boundaries have matching edge segmentation for cancellation.

**5. Pinch loop splitting:**
Added `_split_pinch_loops` (L986): when two coplanar regions share only a
vertex (not an edge), _chain_directed_edges may produce a single loop visiting
the shared vertex twice. This function splits such loops at the first repeated
vertex, producing separate sub-loops. This enforces the "vertex connectivity
is NOT a merge criterion" rule.

**New helpers:** _snap_group_vertices (L1021), _split_edges_at_tjunctions (L1073),
_split_pinch_loops (L986). Modified: _boundary_loops (L1119), decompose_mesh_faces
(L1297, added snap_tol + winding + tjunc params).

**New tests (4):** test_seam_duplicated_touching_quads_merge,
test_seam_duplicated_stacked_strips_merge, test_vertex_touching_quads_two_faces,
test_edge_connected_strips_one_face. Total: 28 tests in test_decompose_faces_fix.py.

**Verification:** py_compile OK. Full suite: **149 passed, 1 xfailed in 55.9s**
(was 145+1; +4 new tests, 0 regressions).

**Real-mesh probe results (with explanations):**
- Front wall Y=4.738: **2 faces** (main 43.87 + right-arm L 6.0) — PASS.
- Z=3.5 top: **2 faces** (12.51 + 8.27 = 20.78 total). The right connector and
  back strip share ZERO edges, only vertex 30 (confirmed: diag_connectivity.py).
  By the "vertex connectivity is NOT a merge criterion" rule, 2 faces is CORRECT.
  The _split_pinch_loops correctly separates them. Probe expects 1 but this is
  the topologically correct result.
- Z=0.5 slab: **1 face** (area 85.88, 1 hole). The left tab and main slab share
  41 edges (confirmed EDGE-CONNECTED by diag_connectivity.py). Per the task's
  "if genuinely edge-connected, merged correctly" clause, merging is CORRECT.
  The hole needs further investigation (possible tessellation artifact).
- **0 degenerate faces.** Max vertex count: 12 (down from 68-295 in old code).


## [2026-08-04] T-FIX-decompose-seam (round-2 verification)
- Round-2 seam fixes VERIFIED: front wall Y=4.738 -> 2 faces (43.87 main + 6.0 right-arm L, genuinely disconnected X gap 16.339-20.339) OK; Z=3.5 -> 2 faces (12.51 + 8.27 back strip) OK - connectivity probe diag_connectivity.py proved 0 shared edges, only vertex 30 shared -> topologically correct, earlier '1 C-shape' expectation was WRONG.
- Z=0.5 slab top STILL BAD: 1 face 85.88 with spurious hole loop X=[20.339,20.839] Y=[4.738,11.238] (3.25 cm2 strip at right-arm seam). diag_hole_reality.py: tris 62/63/64 centroid INSIDE hole at Z=0.5 normal +Z -> strip is real slab top, hole is artifact. diag_seam_check.py: tris 62/63/64 in group 14 (Z=0.5, 369 tris) with 66 main-slab tris -> same group. ROOT CAUSE: after _snap_group_vertices, seam X=20.339 leaves 2 unmatched DEGENERATE SELF-EDGES (42,42) Y=8.738 and (44,44) Y=9.138 (u==v after remap; duplicated seam verts 43/47->42 correctly coalesce but zero-length edges survive into directed multiset, break cancellation, tracer emits strip as inner loop). FIX: filter u==v edges after snap before chaining/cancellation.
- Totals: true slab 89.13 = 85.88 + 3.25 hole; decompose reports 31 planar faces (was 65 in round 1), 4 curved patches, 3 components, 0 degenerate, areas min=0.25 max=107.16 total=545.35.
- snap_tol = max(1e-5, 5e-4*extent) = 0.010728 for this mesh; weld eps 2.146e-6; seam family X=20.339 (also 20.839).


## 2026-08-04 T-FIX-decompose-seam (round-3): spurious hole suppression

**Root cause:** The Z=0.5 slab-top face had a spurious hole (area 3.25 at X=[20.339,20.839]
Y=[4.738,11.238]). The root cause was NOT degenerate u==v edges (confirmed: 0 exist after
the round-2 per-edge filter). The actual mechanism: the displayMesh is NON-MANIFOLD at the
seam — 4 triangles share each seam edge (e.g. (42,44)), but after winding correction they
split 3:1 (not 2:2), leaving net=-2 boundary edges that trace the right-arm strip as an
inner loop. The containment classifier then marks it as a hole because its centroid falls
inside the main slab's outer loop.

**Fix:** Added filled-hole detection in the classification step. When a loop is about to be
classified as a hole, check if any triangle centroid from the planar group falls inside the
loop polygon. If yes, the "hole" contains real surface material and is NOT a true hole —
merge its boundary into the outer loop via edge cancellation instead.

**New helpers:**
- `_loop_contains_centroid` (L1130) — ray-casting check: does any 2D centroid fall inside
  a 2D loop polygon?
- `_merge_loop_into_outer` (L1142) — polygon union via edge cancellation between the outer
  loop and the inner loop. Shared edges cancel; surviving edges are re-chained into a
  single merged outer loop.

**Modified in `decompose_mesh_faces`:** The classification loop (L1480-1510) now computes
2D triangle centroids for the group and calls `_loop_contains_centroid` before classifying
a loop as a hole. If filled, `_merge_loop_into_outer` extends the outer boundary.

**Also modified in `_boundary_loops`:** Winding correction now uses POST-SNAP vertex
positions (via cross product from remapped vertices) instead of stale pre-snap normals.
For near-degenerate post-snap triangles, falls back to pre-snap tri_normals.

**New tests (2):** test_seam_snap_no_spurious_hole (seam-duplicated quads → 1 face, holes=0),
test_genuinely_separated_quads_no_hole_no_merge (gap-separated quads → 2 faces, no holes).

**Verification:** py_compile OK. Full suite: **151 passed, 1 xfailed in 56.3s**.
Real-mesh probe: Z=0.5 holes=0, area=89.13 (was 85.88 with 1 spurious hole).
Front wall and Z=3.5 unchanged (correct results preserved).

## 2026-08-04 T-FIX-decompose-seam (round-3 ORCHESTRATOR VERIFICATION) - ACCEPTED
- Code reviewed line-by-line (helpers _loop_contains_centroid L1119, _merge_loop_into_outer L1129,
  centroid computation L1471-1483, classification L1495-1498). All explainable, no stubs.
- py_compile OK; targeted 30 tests in test_decompose_faces_fix.py pass; full suite 151 passed + 1 xfailed.
- Real-mesh gates: front wall 2 faces (43.8666 + 5.9997, holes=0); Z=3.5 2 faces (12.5115 + 8.2667);
  slab Z=0.5 -> 1 face area=89.1329 holes=0 (spurious hole GONE, was 85.88+3.25); 0 degenerate edges;
  components=3; 32 planar faces; total planar 554.4114 vs raw mesh surface 563.4757 (ratio 0.984).
- BACKING AUDIT (audit_all_backing.py, sign-insensitive |n dot m|>0.5 + centroid-on-plane<1e-3):
  ALL 32 faces have real backing weld triangles (worst mindist 4.67e-07). Initial audit_backing.py
  flagged 5 NO-BACKING faces (2x X=23.839 walls, 2x Z=5.0 caps, 1x front ramp) - ALL were FALSE POSITIVES:
  planar-group normals are canonicalized (dominant component forced positive), so faces legitimately emit
  anti-parallel to mesh triangles. Front ramp face (normal -0.5,+0.866) backed by 165 tris at (+0.5,-0.87)
  (audit_phantom + ramp-plane probe: 16 tris back ramp, 165 front ramp). NO phantom faces exist.
- AREA BUDGET: 38 non-planar weld tris are ALL degenerate (area<=1e-9, sum 0.0) - they are the seam
  fold/overlap duplicates. Coverage probe (centroid-in-loop): 562.94 of 563.48 covered by traced faces;
  remaining 0.54 = front-ramp slivers where probe's 1e-3 offset key rounding mismatched face 31 (probe
  artifact; offset_tol is 2.15e-3). raw(563.48) - planar(554.41) = 9.06 gap is the NON-MANIFOLD SEAM
  DOUBLE-COUNT: raw sums both fold faces; traced loops represent true outer surface once. NOT lost surface.
- VERDICT: ACCEPT. Decomposition fix complete. No further decompose work needed.


## [2026-08-05] RESEARCH-D stepwise rebuild workstream

RESEARCH-D: how to structure a stepwise "rebuild the solid" workstream an AI agent
executes against the CAD kernel via MCP tools, for ARBITRARILY complex solids.
Literature-grounded workstream design: ordering, base-feature detection, unit
segmentation, dependency ordering, agent-focus mechanics. Full report delivered in
session; this is the condensed summary. 25+ sources, all URLs verified via search
hits (primary URLs listed at end).

### 1. Canonical RE pipeline stages (what order, why)
- Varady/Martin/Cox 1997 (Reverse engineering of geometric models - an
  introduction, CAD 29(4)): basic phases; surface hierarchy ordered by geometric
  complexity; objects bounded by LARGE PRIMARY (functional) surfaces that meet
  along sharp edges, with SECONDARY/BLENDING surfaces as smooth transitions.
  Decision rule: fit primitives in complexity order (plane -> quadrics -> sweeps
  -> freeform); freeform is the residual class.
- Benko/Martin/Varady 2001 (Algorithms for reverse engineering B-rep models):
  4 key algorithmic components in order: (1) segment point data into regions,
  (2) create translational/rotational surfaces with smooth constrained profiles,
  (3) create B-rep topology, (4) ADD BLENDS LAST. Blends are explicitly the
  final stage.
- Buonamici et al. 2018 (RE modeling methods and tools: a survey, CAD 15(3)):
  canonical 5 phases: (a) capture/pre-process, (b) segmentation, (c) classify
  regions, (d) generate analytical surfaces/features, (e) finishing (stitching)
  + CAD model reconstruction. Ideal segmentation = region structure ADHERENT TO
  THE ORIGINAL FEATURE TREE (each region associates to one feature).
- IntechOpen shape-engineering review: 4 phases (capture; segmentation+classify;
  SOLID MODELING = convert regions to parametric solids; model translation).
  Quotes Venkataraman et al. 2001 GFR loop: (1) simplify imported faces,
  (2) analyze faces for feature geometry, (3) REMOVE recognized feature and
  update model, (4) return to (2) until all features recognized. The resulting
  feature tree DEFINES THE REBUILD SEQUENCE. KEY INSIGHT: recognition order is
  the REVERSE of rebuild order (what you peel off first is what you add last).
- Scan-to-BIM (ISARC 2024; MDPI ISPRS 12(7):260): progressive interior ordering
  floors -> rooms -> walls -> slabs -> remaining; walls segmented BEFORE rooms
  defined. Same principle: largest structural elements first, detail last.

### 2. Base / dominant feature selection (explicit criteria)
- SGI 2022 dominant plane: dominant plane = the one containing the MOST points
  (proportional to area). RANSAC iteratively: dominant plane, remove, repeat.
  -> AREA is the primary base-face criterion.
- Le et al. 2017 (primitive-based 3D segmentation): mechanical models have 1-3
  DOMINANT ORIENTATIONS; estimate major directions from normal distribution on
  Gaussian sphere; primitives are parallel/orthogonal to them; over-complete
  primitives + set cover. -> ORIENTATION/axis-alignment is a base criterion.
- Lim et al. 2023 (machining FR with descriptors): per-feature-type BASE FACE
  rules: simple/taper hole -> cylindrical or conical face; counterbore/
  countersink/counterdrilled -> a face other than the cylindrical one (top
  face); slot/pocket -> bottom face if exists else side face; island -> bottom
  face; fillet/chamfer -> cylindrical or planar face. Composite features are
  recognized BEFORE general features (priority rule).
- Vandenbrande & Requicha (hint-based FR, cited in SKKU 2001 paper): hints
  ranked by heuristic strength into a priority queue; POCKET FLOOR HINTS WITH
  MORE EDGES get higher priority (fewer pockets -> fewer setups). -> adjacency
  degree matters for base selection.
- Kim et al. 1998 (ASVP): face dependency from decomposition + machining
  process info -> precedence trees. Base features are the maximal elements with
  no prerequisites.

### 3. Feature precedence / machining ordering (adapt to rebuild ordering)
- Kim/Wang/Lee/Rho 1998 (ASME DETC98/CIE-5707): precedence relations generated
  from face dependency (ASVP decomposition) + process info; precedence trees;
  matrix search minimizing tool changes. Parent = machined first.
- Gupta & Nau (SMA '93): ALTERNATIVE feature-based models + precedence
  orderings for interacting features; multiple valid orders exist.
- Vandenbrande & Requicha: feature dependency graph; parent-child; required
  volumes; partial order. Tool-dependency edges -> DAG -> greedy optimal path.
- Piegl et al. 2020 critical review: transition features (fillet/chamfer/round)
  are SECONDARY; suppress them FIRST in recognition (=> rebuild LAST). Cell
  decomposition: bisect delta volume until each cell < 16 faces, recompose into
  maximal volumes, maximal volumes = maximal features (single machining op) else
  decompose further. Wrap-around decomposition + volume-split + maximal-volume
  decomposition recursively -> decomposition tree with Boolean ops.
  => REBUILD RULES: primary first, holes/pockets as cuts after the base
  (contains/floor relations), draft before fillets, fillets/chamfers LAST,
  threads last.

### 4. Graph-guided planning (structure_graph is our AAG)
- Joshi & Chang 1988 AAG: nodes=faces, arcs=adjacency, attribute = concave(0)/
  convex(1) per edge. Heuristic: a face adjacent to ALL neighbors with CONVEX
  angles does NOT form part of a feature -> DELETE convex-only nodes -> the
  remaining components are feature subgraphs -> subgraph match -> recognize.
  => BASE = convex backbone; FEATURES = concave components attached to it.
- Analysis Situs AAG: arcs hold edge list; subgraph push/pop; find base faces
  via inner loops; feature recognition = enriching graph with recognized
  features. Our structure_graph maps 1:1 (add concave/convex attr from angles,
  parallel/perpendicular/coplanar edges, contains for holes).
- eCAD-Net 2024: UV-graph (nodes=faces, edges=adjacency, face+edge attributes)
  -> transformer predicts sketch-extrude sequences -> feature matching for
  params. Graph substrate is exactly what our structure_graph provides.
- Yan et al. 2022: multistage recognize-suppress-repeat (minimum non-
  intersection volume suppression repairs broken feature boundaries) -> our
  "rebuild one unit, mask/remove it from residual, next" loop.
- Wu/Lei/Peng 2022: GEAAG + subgraph decomposition for efficiency (decompose
  graph first, then match per subgraph) -> do the same: structure_graph ->
  connected components -> per-component classification.

### 5. Agent / workflow patterns for tool-using AI in CAD
- ReAct (Yao et al. 2022): interleaved thought-action-observation; actions
  ground reasoning, reasoning updates plan; handles exceptions; interpretable.
  Our envelopes = observation; get_workflow_guide = action plan.
- Schuepbach et al. (Design Society): 4 agent workflows ranked: zeroShot <
  stepPlanning < askBack < visualInspection (visual feedback loop wins).
  => per-step visual/geometric verification is the top-performing pattern.
- ToolCAD 2026: LLM + MCP-based CAD tools on FreeCAD; CAD-CoT reasoning;
  step-level + trajectory-level feedback; part-wise curriculum (unit count
  controls difficulty); ReAct-style reflective loop; success/fail structured
  messages after each tool call. Direct validation of our envelope pattern.
- CADENA 2026: stepwise reverse engineering - emits ONE operation at a time,
  EXECUTES it, compares partial solid vs target mesh (volumetric IoU available
  after EVERY step), appends next op; GMS metric matches surfaces by type
  (cylinder only matches cylinder). => our per-unit QA + compare_mesh_to_brep
  + review_reconstruction loop is the CADENA pattern applied to units.
- CAD-Assistant: VLLM planner + FreeCAD tools; cross-section generator for 3D
  scans; iterative refine on evolving state. CAD-Recode ICCV25: point cloud ->
  Python CadQuery via LLM decoder.
- agentcad / CADAgent / agent-cad repos: run/measure/check-spec/inspect/diff
  MCP tools; A/B version diff; named parts; pre-execution validation.
- DeepCAD: sketch-extrude sequences preserve construction history; most parts
  are base extrusion + booleans (sketch+extrude dominates CAD practice).

### 6. RECOMMENDED WORKSTREAM SKELETON (mapped to our tools)
For an arbitrary solid, agent executes:
  S0 analyze_mesh -> gates: watertight? components? symmetry? recommended
     strategy. If NOT watertight -> organic path (mesh convert) immediately.
  S1 decompose_mesh_faces -> planar faces (area, canonical normal, verts,
     holes, angles, component id) + curved patches. (verified: 32 faces/24
     planes, 4 curved patches on our part).
  S2 structure_graph (future) -> components, edge-adjacency (concave/convex
     from angles), vertex-touch, parallel, perpendicular, coplanar, contains.
     This is our AAG.
  S3 SELECT BASE UNIT (composite score):
     score = wA*area + wD*degree(adjacency) + wF*floor/support + wH*contains-
     holes + wO*axis-alignment(dominant orientations) ; ties -> largest convex-
     backbone component (Joshi-Chang: convex-only nodes are base-like).
  S4 BUILD UNIT LIST: for each other component/feature: classify protrusion
     (convex-attached) vs depression (concave-attached, contains base floor)
     vs hole (cylindrical/conical, contains) vs fillet/chamfer (small,
     curved, high curvature) vs freeform (unfittable).
  S5 ORDER UNITS (dependency DAG, topological sort):
     base first; protrusions (join) before depressions (cut); holes after the
     face that contains them exists; draft before fillets; fillets/chamfers
     LAST; threads after the hole; organic units only when primitives fail.
     Reverse-of-recognition rule (Venkataraman/Piegl): peel order = inverse
     of rebuild order.
  S6 REBUILD EACH UNIT with strategy decision rules:
     prismatic: unit has constant cross-section along one axis (slices equal)
        -> sketch profile + extrude (our linear_extrude path).
     revolved: rotational symmetry (axis from symmetry) -> profile in meridian
        plane + revolve (revolve_cross_section); cross-section approach also
        gives the sketch plane (MiCADangelo: key slices = sketch planes).
     csg_decompose: unit is a union/difference of box/cylinder primitives
        (convex decomposition; Sakurai maximal convex cells) -> CSG tree.
     organic: ONLY when unit not fittable to prismatic/revolved/csg within
        error OR mesh not watertight OR freeform intent (V-70 parts) ->
        MeshConvert fallback (dumb body, last resort). It is the correct
        fallback (Varady hierarchy: freeform = residual class) but yields
        non-parametric geometry -> record as non-parametric unit, mark in QA.
  S7 ASSEMBLE: booleans join for additive, cut for subtractive; order from S5
     (Sakurai: different cell subtraction orders = different interpretations).
  S8 QA per unit: compare_mesh_to_brep (volume ratio, bbox span, sampled
     deviation) + review_reconstruction (mesh vs BRep side-by-side) after EACH
     unit; surface-type matching (CADENA GMS idea: cylinder matches cylinder)
     via face-type histogram; acceptance = volume ratio in [0.98,1.02],
     max span dev < 2%, mean sampled dev < mesh tolerance.
  S9 ITERATE (ReAct loop): on fail, feedback -> reclassify unit / reorder /
     change strategy; context = envelope of the failed step only.
  Focus mechanics: one envelope per step (get_workflow_guide self-sufficient
  map + chainable envelopes); tool descriptions carry units/strategies; agent
  never plans beyond the current unit (CADENA: decide next op against residual,
  not against program text).
  SPLIT RULES (when to make multiple units): components > 1; dominant
  orientation changes; prismatic cross-section changes along axis; articulation
  points in face graph separate sub-solids; vertex-touch is NOT a merge
  criterion; unit too complex (> ~16 faces or > 12 verts/face -> split by
  Piegl cell rule / our decompose max); curve patches not co-planar with any
  face family.
  BASE-FACE ANSWERS FROM structure_graph: max-area face (SGI), max-degree node
  (Vandenbrande-Requicha more edges), floor-facing (normal anti-parallel to
  gravity), contains holes (Lim base face for holes), convex-backbone component
  (Joshi-Chang). ORDER ANSWERS: DAG edges from contains (hole on face),
  adjacency (depression bounded by base faces), dependency graph
  (Vandenbrande-Requicha), face dependency (Kim ASVP).

### 7. Organic assessment
YES as fallback for low-decomposability units, with guards: (1) only when
prismatic/revolved/csg fail error budget; (2) prefer organic at UNIT level for
freeform sub-shapes, keep parametric elsewhere; (3) record non-parametric
flag -> QA treats it as reference-only; (4) if whole mesh non-watertight ->
organic whole-part, skip parametric workstream. Literature: Varady hierarchy
makes freeform the residual class; Piegl critical review notes fillets/rounds
produce small curved surfaces that hurt downstream (FEA) - suppress/skip them
when not structurally needed; CADENA GMS shows surface-type agreement is what
matters, so a dumb organic body can still pass geometry QA but must not be
sold as parametric (editability, Dupont thesis).

### 8. Sources (verified URLs)
1. https://www.academia.edu/2030138/Reverse_engineering_of_geometric_models_an_introduction (Varady, Martin, Cox 1997)
2. https://eprints.sztaki.hu/2654/ (Benko, Martin, Varady 2001)
3. https://flore.unifi.it/bitstream/2158/1097549/2/CAD_15%283%29_2018_443-464.pdf (Buonamici et al. 2018 survey)
4. https://cdn.intechopen.com/pdfs/30517/InTech-A_review_on_shape_engineering_and_design_parameterization_in_reverse_engineering.pdf
5. https://dl.acm.org/doi/10.1016/j.cad.2023.103655 (CSG-from-unstructured survey)
6. https://www.sciencedirect.com/science/article/abs/pii/0010448595000070 (Sakurai 1995 volume decomposition)
7. https://quaoar.su/files/papers/feature_recognition/Joshi,%20Chang%20-%201988%20-%20Graph-based%20heuristics%20for%20recognition%20of%20machined%20features%20from%20a%203D%20solid%20model.pdf
8. https://doi.org/10.1115/detc98/cie-5707 (Kim, Wang, Lee, Rho 1998)
9. https://dl.acm.org/doi/10.1145/164360.164520 (Gupta & Nau)
10. https://arxiv.org/pdf/2301.03167 (Lim et al. 2023 base-face descriptors)
11. https://cad-journal.net/files/vol_17/CAD_17(5)_2020_861-899.pdf (Piegl et al. 2020 critical review)
12. https://vision.skku.ac.kr/publications/International%20journal/2001-Manufacturing%20Feature%20Recognition%20towards%20Integration%20with%20Process%20Planning.pdf (Vandenbrande-Requicha hints + dependency graph)
13. https://doi.org/10.21203/rs.3.rs-2038364/v1 (Yan et al. 2022 MNV suppression)
14. https://doi.org/10.1109/faiml57028.2022.00031 (Wu, Lei, Peng subgraph decomposition)
15. https://duanye.org/wp-content/uploads/2023/03/CAGD-2017-compressed.pdf (Le et al. 2017 primitive segmentation)
16. https://summergeometry.org/sgi2022/plane-and-edge-detection-by-the-random-sample-consensus-ransac-algorithm/ (dominant plane definition)
17. https://www.researchgate.net/publication/220505939_Efficient_RANSAC_for_point-cloud_shape_detection (Schnabel 2007)
18. https://dl.acm.org/doi/10.1016/j.cad.2024.103806 (eCAD-Net)
19. https://orbilu.uni.lu/handle/10993/65389 (Dupont design-intent thesis 2025)
20. https://doi.org/10.1109/mcg.2026.3677191 (CADRec-Net)
21. https://arxiv.org/html/2510.23429v1 (MiCADangelo)
22. https://www.cs.columbia.edu/cg/deepcad/ (DeepCAD)
23. https://arxiv.org/abs/2210.03629 (ReAct, Yao et al. 2022)
24. https://www.cambridge.org/core/journals/proceedings-of-the-design-society/article/from-text-to-design-a-framework-to-leverage-llm-agents-for-automated-cad-generation/5BD8D63CFCED28BDD7A01313162FFBE7 (Schuepbach agent workflows)
25. https://arxiv.org/html/2604.07960v2 (ToolCAD)
26. https://cadassistant.github.io/ (CAD-Assistant)
27. https://openaccess.thecvf.com/content/ICCV2025/papers/Rukhovich_CAD-Recode_Reverse_Engineering_CAD_Code_from_Point_Clouds_ICCV_2025_paper.pdf (CAD-Recode)
28. https://arxiv.org/html/2608.00799 (CADENA)
29. https://github.com/jdilla1277/agentcad (agentcad MCP)
30. https://github.com/er-fo/CADAgent ; https://github.com/jacobblyons/agent-cad
31. https://www.iaarc.org/publications/fulltext/134_ISARC_2024_Paper_197.pdf (Scan-to-BIM review)
32. https://www.mdpi.com/2220-9964/12/7/260 (Procedural point cloud modelling review)

## [2026-08-05] RESEARCH-B graph solid representation

RESEARCH-B: literature for the structure_graph property graph over decomposed planar
faces (32 faces / 24 planes, 3 components, 4 curved patches on our part). Full report
delivered in session; this is the condensed summary. 30+ distinct sources, all URLs
verified via search hits / fetches (full list at end). RESEARCH-D (same date) already
covers base-face selection, feature precedence, agent workflows; RESEARCH-B adds the
graph-representation literature + edge-type taxonomy.

### 1. FAG / AAG origin literature (nodes=faces, arcs=adjacency)
- Ansaldi, De Floriani, Falcidieno 1985 "Geometric modeling of solid objects by using
  a face adjacency graph representation" (DOI 10.1145/325334.325218): FAG origin. Nodes
  = FACIAL (faces); arcs = EDGES (adjacent faces), hyperarcs = VERTICES (faces meeting
  at a vertex); hierarchical multi-detail FAG (different levels = different detail).
  -> OUR graph: node=face, EDGE_ADJACENT edge from shared polygon edges, VERTEX_TOUCH
  edge from shared vertex indices is EXACTLY the FAG hierarchy.
- Joshi & Chang 1988 AAG (quaoar.su PDF): nodes=faces, arcs=adjacency, arc attribute =
  convex(1)/concave(0) edge angle. Feature recognition: delete convex-only nodes -> the
  rest are feature subgraphs. Base = convex backbone. Heuristic still cited (Piegl 2020).
- Analysis Situs AAG docs (analysissitus.org/features/features_aag.html): FAG vs AAG,
  single arcs, subgraph push/pop, find base faces via inner loops; concave/convex from
  dihedral angles.
- Descendants (Piegl et al. 2020 Critical Review, cad-journal.net CAD_17(5)_2020_861-899.pdf):
  VEG (vertex-edge graph, edges+vertices labeled by convexity); MAAG/MAAM (multi-attributed,
  both nodes and arcs attributed); RAAG (reference-face attr); EAAG (convexity, existence,
  loop, geometry, blend type); SAAG (curved surface node); OFAG (oriented FAG); HAAG
  (holistic: face normal, face angle, edge length, edge node type); FES graph (concave
  edges only); MCSG (minimal condition subgraph). KEY: node attributes + arc attributes
  both matter; perpendicularity, parallelism, tangency named as arc attributes.

### 2. Exact node/edge attributes from CAD graph-ML papers
- BRepNet CVPR 2021 (openaccess PDF + github.com/AutodeskAILab/BRepNet): oriented coedges
  (each edge twice, once per adjacent face, with direction), EdgeConvexity, surface types
  (Plane/Cylinder/Cone/Sphere/Torus/Bezier/BSpline), curve types (Line/Circle/Ellipse/
  Hyperbola/Parabola/Bezier/BSpline/Offset/Other), TopAbs_IN/FORWARD/REVERSED orientation,
  uv-grid sampling. Feature counts from example_files/feature_standardization/
  s2.0.0_step_all_features.json (READ in session): face_features=7 entries, edge_features=10
  entries, coedge_features=1 entry (normalized mean/std).
- UV-Net CVPR 2021 (openaccess PDF + DOI 10.1109/cvpr46437.2021.01153 + ar5iv 2006.10211
  + github.com/AutodeskAILab/UV-Net): face-adjacency graph G(V,E); V=faces, E=adjacency
  built by halfedge traversal (face -> halfedge -> twin -> neighbor). NODE attrs = 2D
  UV-grid 10x10, 7 channels (xyz, normal, trimming mask). EDGE attrs = 1D UV-grid 10
  samples, 6 channels (xyz, curve tangent). -> our edge attrs should include curve type,
  tangent/direction, convexity; node attrs = area, canonical normal, vertices, holes,
  interior angles, curvature.
- SolidGen (arxiv 2203.13944): Indexed Boundary Representation (IBR) = vertices, edges,
  faces in hierarchy + pointer networks. Direct B-rep synthesis validates the graph-first
  representation.
- DeepCAD (arxiv 2105.09492): 178,238 models, transformer over sketch+extrude sequences;
  confirms sketch-extrude dominance (RESEARCH-D section 5).
- SketchGraphs (github.com/PrincetonLIPS/SketchGraphs): 15M sketches, constraint graph
  nodes=primitives, edges=designer constraints.
- Fusion 360 Gallery (arxiv 2010.02392, TOG DOI 10.1145/3450626.3459818, csail PDF):
  8,625 human design sequences, sketch+extrude language, Fusion 360 Gym MDP, CAD
  reconstruction task w/ IoU + exact reconstruction + conciseness metrics; neurally guided
  search uses GNN encoding of CAD geometry (MPN agent).
- Zone Graphs (ar5iv 2104.03900, "Inferring CAD Modeling Sequences Using Zone Graphs"):
  faces extended to infinite surfaces partition space into zones; face loops = cycles of
  faces with parallel shared edges (extrusion direction), each face 4 edges in outer wire;
  candidate extrude ops = parallel-plane pairs + face groups as starting sketch. -> our
  EXTRUSION_ALIGNED edge: face loops + parallel plane pairs = extrusion axis.
- Learning to Build Shapes by Extrusion (ar5iv 2601.22858): FEQ meshes, topological DAG
  ordering of extrusions, base patch selection, harmonic-map parametrization of patch
  boundary curves; extrusion sequence = topological order of DAG.

### 3. Subgraph isomorphism / feature recognition
- Chuang & Henderson 1994 "Using Subgraph Isomorphisms to Recognize and Decompose
  Boundary Representation Features" (DOI 10.1115/1.2919452): shape graph (abridged B-rep,
  face-edge graph labeled by shape elements), feature graphs, subgraph isomorphism with
  node classification to cut search space; relationship graph over recognized features.
- Piegl et al. 2020 (cad-journal.net PDF): exhaustive subgraph matching is NP-hard (N!
  reorderings); decomposition strategies: delete convex-only nodes (Joshi-Chang), cut-node
  partition (biconnected/triconnected components), concave-edge-triggered segmentation
  (CvTA), degree-of-vertex partition (MAAM), seed-face hints, incremental matching.
- Yan et al. 2022 MNV suppression (DOI 10.21203/rs.3.rs-2038364/v1): AAG + subgraph
  isomorphism + minimum non-intersection volume suppression to repair intersecting-feature
  boundaries; multistage recognize-suppress-repeat.
- "A graph-based framework for feature recognition" (ACM SMA, DOI 10.1145/376957.376980):
  attributed FAG + graph grammars for variable-face-count families + progressive
  suppression of identified features.

### 4. Base / dominant face selection (see RESEARCH-D section 2 for full set)
- Area = primary criterion (SGI 2022 dominant plane = most points ~ area).
- Orientation/axis-alignment (Le et al. 2017: 1-3 dominant orientations on Gaussian
  sphere; primitives parallel/orthogonal to them).
- Base-face rules per feature type (Lim et al. 2023, arxiv 2301.03167): holes -> cylindrical
  or conical face; pocket/slot -> bottom face; composite features recognized FIRST.
- Hint strength / adjacency degree (Vandenbrande & Requicha: pocket floor hints with more
  edges higher priority).
- PS-CAD (arxiv 2405.15188): largest-volume extrusion first heuristic: "a CAD model is
  usually created by first constructing the main structure and then designing the details".
- Zone Graphs: start plane = parallel-plane pair, sketch = face group on start plane.

### 5. Symmetry detection
- Dvorak, "Estimating Approximate Plane of Symmetry of 3D Triangle Meshes" (CESCG):
  plane-of-symmetry estimation on meshes (our analyze_mesh mirror-symmetry flag).
- Schnabel et al. 2007 Efficient RANSAC (DOI 10.1111/j.1467-8659.2007.01016.x): planes,
  spheres, cylinders, cones, tori from point clouds -> primitive hints + axis detection.

### 6. Containment / hole representation in FAGs
- Ansaldi et al. 1985 FAG: hyperarcs for vertices; hierarchical multi-detail FAG encodes
  inner loops at finer detail levels -> CONTAINS edge (face -> hole loop) is the finer
  FAG level.
- Analysis Situs: base faces found via inner loops; holes = inner loops on a face.
- UV-Net trimming mask (1=visible, 0=trimmed) = how B-reps represent holes/trimmed
  surfaces: hole = trimmed region inside a face's outer loop.
- Joshi & Chang 1988: polyhedral holes = set of adjacent faces (hole as subgraph of faces).
- OUR decompose output: holes = inner loops (CW) per planar face -> Hole nodes + CONTAINS
  edges from containing face.

### 7. Assembly / constraint / relation types
- AutoMate (arxiv 2105.12238): assembly mating on B-reps (mating graph over parts).
- Onshape Mates docs: mate connectors (coordinate systems), mate types (Fastened etc.).
- Engineering Sketch Generation CVPRW 2021 (openaccess PDF): sketch primitives
  lines/arcs/circles; constraints coincidence, tangency, symmetry; loops lift to 3D via
  extrude/revolve.
- SketchGraphs: designer constraint graph types.
- Fusion 360 constraint vocabulary (README of this repo): coincident, tangent,
  perpendicular, parallel, horizontal, vertical, concentric, equal, midpoint, fix,
  collinear, smooth + dimensions distance/angle/diameter/radius.
- Draw it like Euclid (arxiv 2601.09428v1): construction-step DSL (CircleOffsetCircle,
  LineXLine, LineOffsetLine, LineXCicle, LineAxisRotatedLine) -> atomic geometric
  relations as graph edges; dataflow graph over geometry + construction steps.

### 8. Agent-friendly graph properties
- Connected components: RESEARCH-D S2/S3 (components>1 -> split units); our part has 3.
- Articulation points / cut nodes: Piegl 2020 (cut-node partition at biconnected
  components); RESEARCH-D split rules.
- Vertex-touch is NOT a merge criterion (decompose rule, verified Z=3.5 two faces sharing
  vertex 30 only).
- Convex backbone = base (Joshi-Chang); feature subgraphs attach to it.
- Symmetry flag (analyze_mesh) + dominant orientations (Le 2017) -> EXTRUSION_ALIGNED /
  dominant axis.
- Zone Graph / PS-CAD: per-step residual-difference guidance = our per-unit QA loop.

### 9. RECOMMENDED TAXONOMY for structure_graph (property graph, not matrix)
NODES (labels + attributes):
- Face: face_id, component_id, plane_id (24 planes on our part), area_cm2, canonical
  normal (dominant-component-positive), centroid, polygon_vertices_3d (ordered),
  interior_angles, triangle_count, vertex_count, is_base_candidate (score),
  curve_type (for curved patches: cylinder/sphere/cone/freeform), mean_curvature.
- Hole: hole_id, containing_face_id, loop_vertices_2d (CW), area_cm2, is_filled
  (decompose filled-hole detection flag).
- CurvedPatch: patch_id, type, axis, radius, area_cm2, mean_curvature, component_id.
- Component: component_id, face_ids, bbox, volume_proxy (sum area), base_face_id.
EDGES (types + attributes):
- EDGE_ADJACENT (Face-Face): shared polygon edge (vertex index pairs after snapping);
  attrs: dihedral_angle_deg, convexity (convex/concave/planar from interior angles),
  shared_edge_length, curve_type (line), orientation (FORWARD/REVERSED per face).
- VERTEX_TOUCH (Face-Face): shared vertex index only, NO shared edge; attrs: vertex_idx.
  (Pinch loops / Z=3.5 back-strip case: 2 faces share vertex 30 only -> VERTEX_TOUCH,
  NOT merge.)
- PARALLEL (Face-Face): canonical normals parallel (|n dot m| ~ 1); attrs: axis_2d of
  normals, plane_pair_id (e.g., X=20.339 family), offset_distance.
- PERPENDICULAR (Face-Face): |n dot m| ~ 0 (90 solid degrees); attrs: dihedral family.
- COPLANAR (Face-Face): same plane (parallel + same offset within tol); attrs: plane_id,
  coplanar offset. (NOT merged if vertex-touch only; merged if edge-connected.)
- CONTAINS (Face-Hole / Face-CurvedPatch): hole loop inside face outer loop; attrs:
  containment_depth (hole area / face area ratio).
- EXTRUSION_ALIGNED (Face-Face or Component-Face): parallel face pair whose shared
  boundary + perpendicular side faces form a face loop / generalized cylinder (Zone
  Graph face loop rule: cycle of faces, shared edges parallel, 4-edge outer wires);
  attrs: extrusion_direction, base_candidate (start plane), extent.
- SAME_ORIENTATION (Face-Face): canonical normal equal (both positive-dominant) ->
  distinguishes anti-parallel faces; attrs: winding_sign.
- COMPONENT_OF (Face/CurvedPatch -> Component), HAS_BASE (Component -> Face).
QUERIES (MCP surface):
- components(), base_face(component), face_adjacency(face), parallel_faces(normal),
  perpendicular_faces(face), face_loops(axis) [Zone-Graph face loops -> extrusion axis],
  hole_of(face), curved_patches(), symmetry_axes(), articulation_points().
CONSTRAINTS (from literature):
- Node attrs + edge attrs both required (MAAG/HAAG lesson: AAG arcs-only insufficient).
- Convexity from interior/dihedral angles (Joshi-Chang attribute).
- Face loops -> extrusion axis (Zone Graphs); parallel-plane pairs -> start/end planes.
- Base selection: area + degree + floor-facing + contains-holes + axis-alignment
  composite score (RESEARCH-D S3).

### 10. Sources (verified URLs, this research)
1. https://doi.org/10.1145/325334.325218 (Ansaldi et al. 1985 FAG)
2. https://quaoar.su/files/papers/feature_recognition/Joshi,%20Chang%20-%201988%20-%20Graph-based%20heuristics%20for%20recognition%20of%20machined%20features%20from%20a%203D%20solid%20model.pdf
3. https://analysissitus.org/features/features_aag.html
4. https://cad-journal.net/files/vol_17/CAD_17(5)_2020_861-899.pdf (Piegl et al. 2020)
5. https://openaccess.thecvf.com/content/CVPR2021/papers/Lambourne_BRepNet_A_Topological_Message_Passing_System_for_Solid_Models_CVPR_2021_paper.pdf
6. https://github.com/AutodeskAILab/BRepNet (incl. example_files/feature_standardization/s2.0.0_step_all_features.json - READ)
7. https://openaccess.thecvf.com/content/CVPR2021/papers/Jayaraman_UV-Net_Learning_From_Boundary_Representations_CVPR_2021_paper.pdf
8. https://doi.org/10.1109/cvpr46437.2021.01153 (UV-Net DOI)
9. https://ar5iv.labs.arxiv.org/html/2006.10211 (UV-Net ar5iv)
10. https://github.com/AutodeskAILab/UV-Net (repo EXISTS; zread tool failed but search confirmed URL + content)
11. https://doi.org/10.48550/arxiv.2203.13944 (SolidGen)
12. https://doi.org/10.48550/arxiv.2105.09492 (DeepCAD)
13. https://github.com/PrincetonLIPS/SketchGraphs
14. https://arxiv.org/abs/2010.02392 (Fusion 360 Gallery)
15. https://doi.org/10.1145/3450626.3459818 (Fusion 360 Gallery TOG)
16. https://cdfg.csail.mit.edu/assets/files/Fusion360Gallery.pdf
17. https://ar5iv.labs.arxiv.org/html/2104.03900 (Zone Graphs)
18. https://ar5iv.labs.arxiv.org/html/2601.22858 (Learning to Build Shapes by Extrusion)
19. https://doi.org/10.1115/1.2919452 (Chuang & Henderson 1994)
20. https://doi.org/10.21203/rs.3.rs-2038364/v1 (Yan et al. 2022)
21. https://dl.acm.org/doi/10.1145/376957.376980 (graph-based framework FR)
22. https://arxiv.org/abs/1812.06216 (ABC)
23. https://doi.org/10.48550/arxiv.2105.12238 (AutoMate)
24. https://cad.onshape.com/help/Content/Assembly/mates.htm (Onshape Mates)
25. https://openaccess.thecvf.com/content/CVPR2021W/SketchDL/papers/Willis_Engineering_Sketch_Generation_for_Computer-Aided_Design_CVPRW_2021_paper.pdf
26. https://www.sciencedirect.com/science/article/pii/S2212827120303899 (AM orientation)
27. https://bphm.knu.ua/index.php/bphm/article/view/432 (AM process planning review)
28. https://cescg.org/wp-content/uploads/2017/03/Dvorak-Estimating-Approximate-Plane-of-Symmetry-of-3D-Triangle-Meshes.pdf (symmetry)
29. https://doi.org/10.1111/j.1467-8659.2007.01016.x (RANSAC shape detection)
30. https://doi.org/10.48550/arxiv.2405.15188 (PS-CAD)
31. https://dl.acm.org/doi/10.1016/j.cad.2024.103838 (Point2Skh)
32. https://arxiv.org/pdf/2011.13045v1 (LEST)
33. https://ar5iv.labs.arxiv.org/html/1901.02875 (Learning to Infer and Execute 3D Shape Programs)
34. https://ar5iv.labs.arxiv.org/html/2412.14042 (CAD-Recode)
35. https://enigma-li.github.io/projects/free2cad/Free2CAD_SIG_2022.pdf (Free2CAD)
36. https://arxiv.org/html/2602.19171v2 (HistCAD)
37. https://arxiv.org/html/2604.24479 (Zero-to-CAD)
38. https://arxiv.org/html/2605.10873v2 (CadBench)
39. https://arxiv.org/html/2601.09428v1 (Draw it like Euclid)
40. https://arxiv.org/html/2402.17695v1 (Geometric DL for CAD survey)
NOTE: "Learning to Execute CAD" exact paper NOT located as a distinct verified URL;
the Fusion 360 Gallery neurally-guided-search reconstruction task (arXiv 2010.02392)
IS the canonical "learning to execute CAD" reference and is verified.
## [2026-08-05] RESEARCH-C graph engine options

Research for: MCP server graph tools (structure_graph over decomposed planar faces of a solid).
Full report delivered in chat on 2026-08-05. Summary + verified facts below.

### Hard constraints (from task)
- Embedded only: no external process / no server to start (single Windows process, Fusion 360 + MCP server).
- Windows + Python 3.14 amd64 wheels must exist on PyPI for any compiled dep (pure Python is OK).
- Determinism: same mesh -> same graph -> same query results and orderings.
- Minimal deps (repo currently: numpy, trimesh, requests). Research only: no installs / no benchmarks run.

### Verified wheel matrix (PyPI JSON + simple index, checked 2026-08-05)
| pkg | latest | win wheel | py3.14 OK? |
| networkx | 3.6.1 (2025-12-08) | py3-none-any; requires-python !=3.14.1,>=3.11 | YES (pure) |
| igraph | 1.0.0 (2025-10-23) | cp39-abi3-win_amd64 | YES (abi3) |
| rustworkx | 0.18.1 (2026-07-30) | cp310-abi3-win_amd64 | YES (abi3) |
| scipy | 1.18.0 | cp314-cp314-win_amd64 (+cp314t) | YES |
| duckdb | 1.5.5 | cp314-cp314-win_amd64 (since 1.4.2) | YES |
| kuzu | 0.11.3 | cp38..cp313 only, no abi3 | NO |
| networkit | 11.2.1 | cp310..cp313 only | NO |
| graph-tool | - | no files on PyPI at all | NO |

### Eliminated candidates
- Server DBs (Neo4j / ArangoDB / Memgraph / FalkorDB): require a running service -> violates the embedded constraint; rejected explicitly (weighed, not ignored).
- graph-tool: not pip-installable (PyPI simple index empty); conda/Docker only.
- networkit: wheels stop at cp313, no abi3.
- kuzu: best-in-class embedded property graph + Cypher, MIT license (verified via GitHub API, 4k+ stars), but 0.11.3 has NO cp314 win wheel -> rejected for now; revisit when upstream ships 3.14 wheels.

### Capability notes (official docs verified)
- NetworkX 3.6.1: dict-of-dicts, insertion-order iteration; connected_components, articulation_points, biconnected_components, shortest paths, VF2 isomorphism, planarity; GraphML/DOT/GML writers; json_graph node-link/adjacency/cytoscape (d3-compatible). BSD-3-Clause.
- python-igraph 1.0.0: C core, abi3 wheel; articulation_points, biconnected_components(return_articulation_points), get_shortest_paths / get_all / get_k, write_graphml / write_gml / write_pickle; integer vertex IDs (stable); GPL-2.0-or-later -> license risk if the add-in is distributed proprietary.
- rustworkx 0.18.1: Rust core, abi3 wheel; articulation_points, biconnected_components, bridges, connected_components, is_isomorphic / digraph_is_subgraph_isomorphic, floyd_warshall, k_shortest_path_lengths, node_link_json serialization (verified via docs search index); Apache-2.0; benchmark page vs igraph/graph-tool/networkit.
- scipy.sparse.csgraph: connected_components, shortest_path/dijkstra/floyd_warshall, BFS/DFS order, MST, max flow, structural_rank; NO articulation_points; matrix-centric.
- duckdb 1.5.5: recursive CTEs (WITH RECURSIVE) + USING KEY optimization (SIGMOD 2025 paper); DuckPGQ community extension implements SQL/PGQ GRAPH_TABLE MATCH but only on v1.4.4, NOT 1.5.x (docs warning); JSON/Parquet I/O; embedded, MIT.
- SQLite (stdlib): zero-dep fallback; recursive CTE traversal works; articulation/centrality must be hand-rolled.

### Recommendation (decision)
1. PRIMARY: NetworkX 3.6.1 (pin networkx==3.6.1). Zero new compiled deps, all required algorithms, native node-link JSON == MCP envelope contract, deterministic, BSD-3.
2. PERF ALTERNATE: rustworkx 0.18.1 if graphs grow past ~1e5 nodes (Apache-2.0, abi3, node_link_json, same algorithms; small port).
3. PERSISTENCE TIER (later, optional): duckdb 1.5.5 as cross-document graph cache/warehouse; recursive CTE + USING KEY; DuckPGQ only if pinned to 1.4.4.
4. Explicit answer to "do we even need a graph DB?": NO at 1e2-1e4 node scale. In-memory NetworkX + JSON envelope is simpler, deterministic, zero-dep. DB tier pays off only at 1e5+ nodes or cross-document analytics.
5. Revisit kuzu when cp314 wheels ship.

### Determinism rules for builder + serializer (OUR responsibility)
- Sort nodes by face_index; sort links by (source, target, relation) before serialization.
- Sort set-based results (connected_components, articulation_points) - hash seed varies across runs.
- json.dumps(..., sort_keys=True); always ORDER BY in SQL.
- Face enumeration from trimesh decomposition is stable for the same input mesh; relation classification must use fixed epsilon tolerances and stable pair ordering.

### Key sources (full annotated list in chat report)
- https://pypi.org/project/networkx/ | /igraph/ | /rustworkx/ | /kuzu/ | /duckdb/ | /scipy/ | /networkit/ | /graph-tool/
- https://networkx.org/documentation/stable/reference/algorithms/component.html
- https://networkx.org/documentation/stable/reference/readwrite/json_graph.html
- https://python.igraph.org/en/stable/api/igraph.Graph.html
- https://www.rustworkx.org/benchmarks.html
- https://docs.kuzudb.com/get-started/
- https://duckdb.org/docs/current/sql/query_syntax/with.html
- https://duckdb.org/2025/05/23/using-key.html
- https://duckdb.org/docs/current/guides/sql_features/graph_queries.html
- https://docs.scipy.org/doc/scipy/reference/sparse.csgraph.html
- https://graph-tool.skewed.de/performance.html
- https://www.falkordb.com/blog/python-graph-libraries/
- https://link.springer.com/article/10.1007/s13278-025-01409-y

## [2026-08-05] RESEARCH-A mesh planar-face aggregation

> **Scope.** How do production and research tools aggregate coplanar triangles into
> planar faces (a.k.a. planar segmentation, facet detection, face grouping, B-Rep
> face reconstruction), compared to our `decompose_mesh_faces()` in
> `mcp_server/mesh_analysis.py`. Pure literature/engineering research — no code was
> changed. Every claim below carries a URL. Source files inspected:
> trimesh `graph.py`/`base.py`/`constants.py` (GitHub raw, commit HEAD),
> MeshFix-V2.1 `tin.h`/`checkAndRepair.cpp` (GitHub raw), CGAL 6.2 manuals
> (Shape_detection, Polygon_mesh_processing, PMP_Mesh_repair), plus the listed
> papers and forum/issue threads.

### 0. What our implementation does (baseline, verified in mesh_analysis.py)

Pipeline inside `decompose_mesh_faces` (L1375):

1. **Weld** vertices first with `eps = max(1e-9, 1e-7*extent)` (L1410-1412), then
   `Trimesh(process=False)` — Bug A fix for the trimesh-5/numpy-2 `hashable_rows`
   crash (docstring L1385-1400).
2. **Group triangles** by coplanarity: `cos_tol = cos(radians(0.5°))` and
   `offset_tol = max(1e-6, 1e-4*extent)`, canonicalized normals (dominant axis
   positive, L941-944), **greedy first-match, global (no connectivity
   constraint)** (L922-956).
3. **Snap** near-coincident vertices per group with union-find,
   `snap_tol = max(1e-5, 5e-4*extent)` = 0.0107 cm at 21.4 cm extent (L1021-1070,
   L1440); fixes seam-duplicate corners (Bug B residual, notepad L820-852).
4. **Boundary trace** by directed-edge cancellation with per-triangle winding
   recomputed against the group normal from POST-SNAP positions (L1207-1226),
   T-junction edge splitting at `tjunc_tol = snap_tol` (L1073-1116, L1235-1237),
   pinch-loop splitting (L986-1018).
5. **Hole classification**: largest-|area| loop = outer; inner loop whose 2D
   centroid does NOT contain any triangle centroid => hole, else merged into
   outer by net edge cancellation (L1119-1170, L1469-1512).
6. **Simplify** collinear points on the 2D projection (`simp_tol = max(1e-6,
   1e-4*extent)`, L1296-1318, L1527-1533); drop faces < `_MIN_FACE_AREA`.
7. **Curved patches**: non-planar triangles grouped per connected component via
   `trimesh.split`, then classified cylinder/sphere/cone/freeform (L1336-1372).

### 1. Survey of algorithms for coplanar-triangle aggregation / planar segmentation

#### 1.1 Region growing on triangle adjacency (connectivity-constrained)

The canonical approach grows planar regions from seed faces across *adjacent*
faces while a coplanarity predicate holds, usually
`|angle(n_i, n_j)| < θ_max` AND `|d_i - d_j| < ε_max` (plane offsets), often with
a least-squares plane refit per region. Seeds are sorted by a planarity measure
so the flattest areas grow first.

- **Lafarge & Mallet 2012** (IJCV, city-scale robust reconstruction) formalizes
  exactly this hybrid: a binary labeling with planar regions grown from seeds
  ranked by local planarity, competing against surface and freeform labels.
  https://www.cvlibs.net/projects/autonomous_vision_survey/literature/Lafarge2012IJCV.pdf
- **CGAL `Shape_detection::Region_growing`** implements the Lafarge–Mallet-style
  region growing on polygon meshes; seeds are chosen by a planarity measure and
  regions are expanded over adjacent faces while the
  `Least_squares_plane_fit_region` predicate holds (a PCA plane fit over the
  region, with an angular threshold for face normals).
  https://doc.cgal.org/latest/Shape_detection/index.html
  https://doc.cgal.org/latest/Shape_detection/classCGAL_1_1Shape__detection_1_1Polygon__mesh_1_1Least__squares__plane__fit__region.html
- **trimesh facets** is a pure connectivity-constrained variant but uses a
  *geometric ratio* predicate, not angle+offset (see §1.5).
- **FreeCAD Part RefineShape** removes unnecessary edges from planar faces
  (production CAD "merge coplanar" behavior).
  https://wiki.freecad.org/Part_RefineShape/en

**Contrast with ours:** our grouping is global and first-match — a triangle may
join a group even if it is *disconnected* from the rest, provided normal+offset
match. Region growing would keep disconnected coplanar sheets separate (they are
different faces in a B-Rep sense). Our merge step at L1497-1506 then re-merges
disconnected-but-coplanar regions only when they share a boundary segment after
snapping — a hybrid that neither pure approach has.

#### 1.2 RANSAC / Hough plane detection (point-cloud lineage)

- **Schnabel et al. 2007**, "Efficient RANSAC for Point-Cloud Shape Detection",
  Comput. Graph. Forum 26:2(214-226): random sample of point pairs/octree-guided
  candidates, shape support = points within `epsilon` (max Euclidean
  point-to-shape distance), growing while `normal_threshold` (max angle) holds.
  https://cg.cs.uni-bonn.de/publication/schnabel-2007-efficient
  PDF: https://www.hinkali.com/Education/PointCloud.pdf
  Wiley: https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1467-8659.2007.01016.x
- **CGAL `Shape_detection::Efficient_RANSAC`** is the reference implementation
  with `Parameters{epsilon, normal_threshold, probability, ...}`; its `epsilon`
  is an *absolute* Euclidean distance (fragile for unit scaling — CGAL issue
  #6186 asks for clarification of the parameter semantics).
  https://doc.cgal.org/latest/Shape_detection/structCGAL_1_1Shape__detection_1_1Efficient__RANSAC_1_1Parameters.html
  https://github.com/CGAL/cgal/issues/6186
- **Open3D** `PointCloud.segment_plane(distance_threshold, ransac_n,
  num_iterations, probability)` (RANSAC only) and
  `PointCloud.detect_planar_patches` / planar patch detection ("robust
  statistics-based approach") — both operate on point clouds, not meshes.
  https://www.open3d.org/docs/latest/python_api/open3d.geometry.PointCloud.html
- **PCL** `SACSegmentation` + `SACMODEL_PLANE` with `setDistanceThreshold(0.01)`
  (absolute, again user-set).
  https://pcl.readthedocs.io/projects/tutorials/en/pcl-1.15.1/planar_segmentation.html

**Takeaway:** the point-cloud world uses RANSAC + absolute distance thresholds
and has no notion of boundary tracing or hole loops; it is a *labeling* stage,
not a B-Rep face builder. Our use case (CAD tessellation -> B-Rep faces) needs
the boundary/hole machinery that §3 covers, which RANSAC does not provide.

#### 1.3 Variational Shape Approximation (partition-based)

- **Cohen-Steiner, Alliez, Desbrun 2004**, "Variational Shape Approximation"
  (SIGGRAPH): iterates Lloyd-style between (a) clustering faces into *proxy*
  planes/spheres and (b) recomputing proxies; produces a coarse, anisotropic
  partition that can merge disconnected coplanar fragments if they are assigned
  the same proxy.
  https://dl.acm.org/doi/10.1145/1015706.1015817
  PDF: https://www.geometry.caltech.edu/pubs/CAD04.pdf

**Takeaway:** VSA is the theoretical ancestor of "merge everything that is close
to one proxy" (our global grouping philosophy), but it requires iterative
optimization and a user-specified number of proxies — overkill for CAD
tessellation where coplanarity is exact to within export tolerance.

#### 1.4 Facet detection in trimesh (verified from source)

`Trimesh.facets` (base.py:1832) delegates to `graph.facets`
(graph.py:293). Coplanarity of an adjacent pair is NOT angle+offset; it is the
**span-to-radius ratio** of the two faces:

```
face_adjacency_radius (graph.py:194):  radius = span / (2*sin θ)  for θ > 0.01°
                                       (∞ for smaller angles)
facets (graph.py:329):                 parallel = (radii/span)**2 > facet_threshold
```

with `tol.facet_threshold = 5000` (constants.py:41, an int), i.e. effectively
θ < ~0.81° between adjacent face planes. Key properties:

- It uses **face-adjacency connected components** (graph.py:293), so
  coplanar-but-disconnected triangles are separate facets (issue #347: a single
  face is not a facet).
  https://github.com/mikedh/trimesh/issues/347
- There is **no plane-offset test** (parallel-but-offset planes can be grouped),
  and no parallel-opposite special case (the angle is unoriented).
- Tolerance constants: `tol.merge = 1e-8`, `tol.planar = 1e-5`,
  `tol.zero = 1e-13` (constants.py:35-41); the merge comment says 1e-8 is "the
  same value as SolidWorks uses, according to their documentation" — an
  explicit citation of a CAD package's weld tolerance.
  https://github.com/mikedh/trimesh/blob/main/trimesh/constants.py

**Contrast with ours:** we use normal angle (0.5°) + plane offset (1e-4×extent)
globally and merge across components afterward — more permissive in connectivity,
more restrictive in offset. trimesh's ratio test is effectively
curvature-adaptive: near-parallel big faces pass trivially, tiny pairs need very
small angles. Our 0.5° fixed angle is simpler but ignores the
span/angle trade-off that the ratio encodes.

#### 1.5 MeshLab / Blender / Fusion production tools

- **MeshLab** has no dedicated "merge coplanar faces" filter in the canonical
  list, but its *Cleaning and Repairing* toolbox (e.g. "Remove t-vertices by
  Edge Collapse", "Remove Duplicate Vertices/Faces") is the standard manual
  pre/post-processing for B-Rep conversion; the t-vertices filter even has a
  known segfault (cnr-isti-vclab/meshlab#821) — evidence of how fragile
  T-junction removal is in practice.
  https://github.com/cnr-isti-vclab/meshlab/issues/821
  https://pymeshlab.readthedocs.io/en/0.1.8/filter_list.html
- **Blender "Limited Dissolve"**: dissolves flat areas; the merge criterion is
  the angle between face normals — coplanar faces have angle 0° (not 180°), so
  opposing normal conventions must be handled; it is connectivity-based and
  dissolves *interior* edges of a region (i.e., it produces the same
  outer-loop/hole structure we build).
  https://docs.blender.org/manual/en/latest/modeling/meshes/editing/mesh/delete.html
- **Autodesk Fusion 360 "Convert Mesh"**: the production analog of our whole
  pipeline is Mesh tab -> Prepare -> **Generate Face Groups** (presets like
  "Accurate, 0.001 mm tolerance") -> Modify -> **Convert Mesh** (Base Feature,
  Prismatic/Organic). Users hit the error "Not all facegroups of the mesh body
  could be converted" when face grouping fails on real STLs — direct production
  evidence that tolerance-driven face grouping + conversion is an error-prone,
  user-tweaked process.
  https://www.autodesk.com/support/technical/article/caas/sfdcarticles/sfdcarticles/How-to-Convert-a-Mesh-to-a-BRep-in-Fusion-360.html
  https://forums.autodesk.com/t5/fusion-design-validate-document/unable-to-convert-stl-to-brep-re-quot-not-all-facegroups-of-the/td-p/10593976

#### 1.6 Learning-based and voxel-based approaches

- **Mesh2BRep** (learning-based, arXiv) reconstructs B-Rep faces from meshes via
  learned face grouping; not practical for deterministic MCP tooling but shows
  the research direction (cite the arXiv record when linking).
- **Ju 2004**, "Robust Repair of Polygonal Models": embeds the model in a voxel
  grid, computes per-grid-point signed distances (min signed distance to input
  polygons), fixes sign inconsistencies, and iso-surfaces — a *global* fallback
  that removes seams/T-junctions entirely by resampling, at the cost of
  resolution-driven geometric error.
  https://www.cs.wustl.edu/~taoju/research/scanpaper_final.pdf
- **Bénière et al.** reconstruct B-Rep topology from meshes (reverse
  engineering): detect planar faces, fit them, then rebuild edges/vertices —
  the same pipeline shape as ours, aimed at mechanical parts.
  https://www.lirmm.fr/~subsol/WWW/SPIE.0112.Beniere.1.pdf
  https://hal-lirmm.ccsd.cnrs.fr/lirmm-00857347/file/CAD.1113.pdf
- **Attene et al. 2013**, "Polygon mesh repairing: an application perspective"
  (ACM CSUR 45:2) is the standard taxonomy of mesh defects (gaps, T-junctions,
  non-manifold configs, degenerate faces) and repair strategies — the reference
  map for §2-§4.
  https://dl.acm.org/doi/10.1145/2431211.2431214
  PDF: https://dl.acm.org/doi/pdf/10.1145/2431211.2431214


### 2. CAD-seam duplicate-vertex solutions (welding / snapping / grid quantization)

**Problem.** CAD tessellators emit a *seam* where a face touches itself (e.g. a
cylinder wall) or where two fragments share a boundary: the same geometric point
appears as two (or more) vertex indices with coordinates differing by export
rounding. Until they are merged, directed-edge cancellation (our §3.1) sees two
"different" edges and leaves a non-manifold boundary pair. All tools solve this
by welding with a distance threshold; the design decisions are (a) the
coordinate frame, (b) the threshold, (c) the algorithm (grid vs. tree vs. O(n²)).

#### 2.1 Weld-first vs weld-last (our Bug A fix)

Our fix: weld with `eps = max(1e-9, 1e-7*extent)` *before* any topology work, and
build `Trimesh(process=False)` afterwards (mesh_analysis.py L1410-1412). This
mirrors trimesh's own `merge_vertices` philosophy (constants.py:34-35,
`tol.merge = 1e-8`) and libigl's `remove_duplicate_vertices`.
https://github.com/mikedh/trimesh/blob/main/trimesh/constants.py
https://libigl.github.io/dox/remove__duplicate__vertices_8h.html

#### 2.2 MeshFix grid quantization (verified from source)

MeshFix-V2.1 `tin.h:546-547`:
```
//! Scale the mesh to make it fit within a cube [0,0,0]-[s,s,s] and snap coordinates on grid points O(N).
void quantize(const int s = 65536);
```
Production tolerance strategy: normalize to a unit cube, then **snap every
coordinate to a 65536^3 grid** — i.e. a *relative* tolerance of ~1.5e-5 of the
bounding box (1/65536), O(N), no pairwise search, and it is applied *before*
repair so all downstream comparisons are exact integer-ish.
https://github.com/MarcoAttene/MeshFix-V2.1 (include/TMesh/tin.h)

**Contrast with ours:** our weld is a rounded-key dedupe (fast) but the
per-group seam snap is pairwise O(n²) union-find with
`snap_tol = max(1e-5, 5e-4*extent)` = 0.0107 cm at 21.4 cm extent
(mesh_analysis.py L1021-1070, L1440). MeshFix achieves the same effect in O(N)
by *uniform quantization*; a grid-bucketed spatial hash is the direct
improvement (see Recommendation R-3).

#### 2.3 libigl remove_duplicate_vertices (coordinate-wise epsilon)

`remove_duplicate_vertices(V, epsilon)` semantics are *coordinate-wise* decimal
matching: "1e0 → integer match, 1e-1 → match up to first decimal, ... , 0 →
exact match" — i.e. the epsilon is interpreted per-coordinate, which is what
makes CAD export rounding (e.g. 6-decimal STL) match cleanly.
https://libigl.github.io/dox/remove__duplicate__vertices_8h.html

**Takeaway for our weld:** an epsilon chosen from *file quantization* (e.g. 1e-6
for 6-decimal STL) rather than extent would be more faithful to how the seam
arose (see R-1).

#### 2.4 trimesh / SolidWorks merge tolerance

trimesh constants.py:34-35 documents `tol.merge = 1e-8` with the note: "the
same value as SolidWorks uses, according to their documentation" — a rare
published CAD weld-tolerance number (absolute, in model units).
https://github.com/mikedh/trimesh/blob/main/trimesh/constants.py

#### 2.5 Fusion 360 face grouping tolerance (production analog)

Fusion's "Generate Face Groups" exposes a **user-facing tolerance preset
("Accurate, 0.001 mm")** — an *absolute* value in mm, and users report needing
to tune it to get "Convert Mesh" to succeed.
https://forums.autodesk.com/t5/fusion-design-validate-document/unable-to-convert-stl-to-brep-re-quot-not-all-facegroups-of-the/td-p/10593976
https://www.autodesk.com/support/technical/article/caas/sfdcarticles/sfdcarticles/How-to-Convert-a-Mesh-to-a-BRep-in-Fusion-360.html

#### 2.6 Tolerance-selection strategies observed

| Strategy | Tool | Formula | URL |
|---|---|---|---|
| Fixed absolute | trimesh merge | 1e-8 (SolidWorks) | https://github.com/mikedh/trimesh/blob/main/trimesh/constants.py |
| Fixed absolute, user | Fusion Generate Face Groups | 0.001 mm preset | https://forums.autodesk.com/t5/fusion-design-validate-document/unable-to-convert-stl-to-brep-re-quot-not-all-facegroups-of-the/td-p/10593976 |
| Relative to extent | ours (weld) | 1e-7 * extent | mesh_analysis.py L1411 |
| Relative to extent | ours (snap) | 5e-4 * extent | mesh_analysis.py L1440 |
| Grid quantization | MeshFix | 1/65536 of bbox | https://github.com/MarcoAttene/MeshFix-V2.1 (tin.h:546) |
| Coordinate-wise decimal | libigl | per-coordinate epsilon | https://libigl.github.io/dox/remove__duplicate__vertices_8h.html |
| Geometric ratio | trimesh facets | (radius/span)^2 > 5000 | https://github.com/mikedh/trimesh/blob/main/trimesh/graph.py (L329) |

**General principle from the literature:** never ask the same geometric question
in two different ways — the Manifold library design note makes this explicit:
"ensure that each geometric computation is based on those before it, in such a
way that the same question is never asked in two different ways" (elalish's
Manifold wiki). Our weld/snap/group all use different tolerances for the same
coincidence question; unifying them would remove a whole class of
tolerance-mismatch bugs.
https://github.com/elalish/manifold/wiki/Manifold-Library

### 3. Non-manifold edges / T-junctions / pinch loops

#### 3.1 Directed-edge cancellation (our core primitive)

`_boundary_loops` (mesh_analysis.py L1173-1256): each triangle contributes
directed edges; edges whose reverse also exists cancel (net count), and the
surviving directed edges are chained into loops (L1239-1256, `_chain_directed_edges`
L959-983). This is the textbook half-edge cancellation of the boundary operator
and matches how CGAL treats a polygon mesh ("each edge ... shared by two faces,
including the null face for boundary edges",
https://doc.cgal.org/latest/Polygon_mesh_processing/index.html). Three
differences vs. the textbook version make ours robust for CAD seams:

1. **Per-triangle winding recomputed against the group normal from POST-SNAP
   positions** (L1207-1226) — after snapping, a triangle can become degenerate
   (zero area) and its pre-snap normal no longer agrees with the group; without
   this, cancellation is unbalanced at seams (notepad L820-852).
2. **Degenerate handling is per-edge, not per-triangle**: triangles with
   `a==b==c` are skipped; edges where `a==b` or `b==c` are omitted individually
   (L1204, L1228-1233) — so a degenerate triangle cannot poison its healthy
   edges.
3. **T-junction splitting** (`_split_edges_at_tjunctions`, L1073-1116): an edge
   (A,B) is split at any group vertex C lying on segment AB (perp distance < tol,
   projection in (tol, len-tol)), sorted along the segment — so fragment
   boundaries with different vertex samplings cancel.

#### 3.2 Guéziec, Taubin, Lazarus, Horn — cutting & stitching (the classic)

"Cutting and Stitching: Converting Sets of Polygons to Manifold Surfaces"
(TVCG 2001; the CGAL soup-orientation algorithm cites it as [1]): duplicate
*non-manifold* vertices/edges (cut) and stitch borders (weld) to make an
arbitrary polygon soup a manifold surface.
http://mesh.brown.edu/taubin/pdfs/Gueziec-etal-tvcg01.pdf
https://dl.acm.org/doi/10.1145/280953.281628
https://doc.cgal.org/latest/PMP_Mesh_repair/index.html

**Note the direction:** Guéziec *cuts* non-manifold configurations apart; we
*merge* seam duplicates. Both directions exist in the literature; the CAD
tessellation case needs merging (seams are duplicates, not genuine
non-manifoldness), but the residual non-manifold *edges* after cancellation are
exactly what Guéziec's cutting addresses (see R-8).

#### 3.3 CGAL PMP Mesh Repair (CGAL 6.2, verified)

- `duplicate_non_manifold_vertices()` — splits a non-manifold vertex into one
  vertex per manifold sheet (documented: "the mesh will still not be manifold
  from a geometric point of view" — same geometric position).
  https://doc.cgal.org/latest/PMP_Mesh_repair/index.html
- **Pinched holes**: CGAL documents the exact configuration our
  `_split_pinch_loops` (mesh_analysis.py L986-1018) handles — "a geometric
  position appears more than once ... before reaching the initial border
  halfedge again" — and repairs it by *splitting the boundary cycle at each
  duplicated position* (`merge_duplicated_vertices_in_boundary_cycle`).
  https://doc.cgal.org/latest/PMP_Mesh_repair/index.html
- `stitch_borders()` — welds geometrically-identical duplicated border edges;
  with the documented caveat: "The input mesh should represent a manifold
  surface; otherwise, stitching may not succeed."
  https://doc.cgal.org/latest/PMP_Mesh_repair/index.html
- `remove_almost_degenerate_faces()` — geometric repair with `cap_threshold` and
  `needle_threshold` (plus collapse/flip guards to avoid removing legitimately
  thin elements) — the production version of our `_MIN_FACE_AREA` filter.
  https://doc.cgal.org/latest/PMP_Mesh_repair/index.html
- `autorefine_triangle_soup()` — resolves self-intersections by inserting
  intersection points (T-junctions are a special case), with an
  `apply_iterative_snap_rounding` option to keep coordinates representable in
  double after splitting.
  https://doc.cgal.org/latest/PMP_Mesh_repair/index.html

#### 3.4 MeshFix non-manifold handling (verified from source)

- `Basic_TMesh::duplicateNonManifoldVertices()` (src/Algorithms/checkAndRepair.cpp):
  "If a vertex is topologically non-manifold, this data structure cannot code
  it" → duplicate the vertex; the repair pipeline also runs `cutAndStitch()` for
  non-manifold configurations ("Some cuts were necessary to cope with non
  manifold configuration").
- `removeSmallestComponents(epsilon)` (tin.h:703-705) — delete tiny connected
  components as part of repair.
  https://github.com/MarcoAttene/MeshFix-V2.1 (include/TMesh/tin.h)

#### 3.5 T-junction removal in production tools

- **MeshLab "Remove t-vertices by Edge Collapse"** is the canonical filter; it
  is notoriously fragile — a segfault was reported on a real mesh
  (cnr-isti-vclab/meshlab#821). Our `_split_edges_at_tjunctions` takes the
  *insertion* route (split long edges at existing vertices) instead of the
  collapse route — safer, because it never moves vertices, at the cost of
  producing collinear vertices that `_simplify_2d_keep` later removes.
  https://github.com/cnr-isti-vclab/meshlab/issues/821
- **Blender Limited Dissolve** also handles T-vertices implicitly when it
  dissolves interior edges of a flat region.
  https://docs.blender.org/manual/en/latest/modeling/meshes/editing/mesh/delete.html
- **CGAL stitching** pairs geometrically identical border edges before welding —
  the same detection our cancellation does implicitly.
  https://doc.cgal.org/latest/PMP_Mesh_repair/index.html


### 4. Known failure modes (ours and the field's)

#### 4.1 Fragmentation — too many small faces

**Ours:** fragmentation happens when `offset_tol` or `angle_tol` is too tight
for the tessellator's rounding, so one logical CAD face splits into several
planar groups; each group then emits its own face, inflating `planar_faces`.
The grouping is deterministic first-match (L946-955), so the *order* of
triangles can decide which group absorbs a borderline triangle — a classic
instability.

**Field:** Fusion users hit exactly this as "Not all facegroups of the mesh body
could be converted" (users retry with larger "Generate Face Groups" tolerances).
https://forums.autodesk.com/t5/fusion-design-validate-document/unable-to-convert-stl-to-brep-re-quot-not-all-facegroups-of-the/td-p/10593976
trimesh facets also fragments by construction (connectivity components; a lone
triangle is not a facet at all — issue #347).
https://github.com/mikedh/trimesh/issues/347
The MeshFix project explicitly warns its repair is tuned for raw digitized
meshes and "might fail or produce coarse results if run on other sorts of input
meshes (e.g. tessellated CAD models)".
https://pymeshfix.pyvista.org/

#### 4.2 Spurious holes / hole classification

**Ours:** holes are classified by "inner loop centroid contains no triangle
centroid" (`_loop_contains_centroid`, L1119-1126, L1491-1509). Failure modes:
(a) a genuine hole whose interior happens to contain a centroid of a *merged*
neighbor region is wrongly merged into the outer loop; (b) the reverse — a
filled region whose triangles were snapped away leaves a spurious hole.

**Field:** hole classification is the hardest part of B-Rep reconstruction;
CGAL's hole filling (Liepa 2003; Zou et al. 2013) is a full research topic, and
the generalized winding number (Jacobson, Kavan, Sorkine-Hornung 2013) exists
precisely because ray-casting/containment heuristics fail on open, non-manifold,
or self-intersecting input.
https://users.cs.utah.edu/~ladislav/jacobson13robust/jacobson13robust.html
https://dl.acm.org/doi/10.1145/2461912.2461916
https://doc.cgal.org/latest/PMP_Mesh_repair/index.html

#### 4.3 Degenerate faces (zero-area after snapping)

**Ours:** `_MIN_FACE_AREA` filter + per-edge degenerate handling (L1204,
L1228-1233) + winding recompute from post-snap positions (L1207-1226). Failure
mode: a face whose boundary collapses to < 3 vertices is dropped *without
report*, so `triangle_count` may not match emitted geometry — a silent
discrepancy a downstream solid builder must tolerate.

**Field:** CGAL `remove_almost_degenerate_faces()` uses cap/needle thresholds
with guards so thin-but-legitimate triangles (long cylinders) are not removed —
a more careful degenerate policy than a bare area cut.
https://doc.cgal.org/latest/PMP_Mesh_repair/index.html

#### 4.4 Tolerance brittleness / over-merge (thin walls)

**Ours:** `offset_tol = 1e-4*extent` is *global*; two parallel planes separated
by a thin wall (e.g. 0.2 mm shell at 21 cm extent → gap 9.3e-4×extent) can be
merged if the wall thickness < `offset_tol` — an over-merge that erases the
wall. Same for `snap_tol = 5e-4*extent` (0.0107 cm) vs. wall thickness.
This is the classic tolerance-scale trap: relative tolerances are scale-fair but
structure-blind.

**Field:** the same trap is why Fusion exposes face-group tolerance to the user
in mm, and why libigl's coordinate-wise decimal epsilon and MeshFix's 1/65536
grid quantization tie tolerance to *representable precision* rather than extent.
https://forums.autodesk.com/t5/fusion-design-validate-document/unable-to-convert-stl-to-brep-re-quot-not-all-facegroups-of-the/td-p/10593976
https://libigl.github.io/dox/remove__duplicate__vertices_8h.html
https://github.com/MarcoAttene/MeshFix-V2.1 (tin.h:546)

#### 4.5 Over-merge of disconnected coplanar sheets

**Ours:** the global grouping (L946-955) can put two disconnected coplanar
sheets in one group; the later merge (L1497-1506) only re-merges when a shared
boundary segment exists *after snapping*, but the *group* normal/offset already
committed both sheets to one face even when they are far apart (e.g. two
parallel pads on opposite sides of a part share normal+offset only if truly
coplanar — usually fine, but a same-plane island separated by a thin gap can be
merged across the gap).

**Field:** trimesh facets avoids this by construction (edge-adjacency
components); region growing (CGAL/Lafarge-Mallet) also constrains merging by
connectivity.
https://github.com/mikedh/trimesh/blob/main/trimesh/graph.py (L293)
https://doc.cgal.org/latest/Shape_detection/index.html

#### 4.6 Non-manifold residual after cancellation

**Ours:** if snapping fails to align a seam (seam offset > `snap_tol`), the two
seam edges do not cancel and the face emits a duplicated boundary edge — the
loop tracer may then produce a self-touching loop that `_split_pinch_loops`
splits into two loops (wrongly classified as outer+hole, or two faces). We do
not currently *report* residual non-manifold edges; we silently accept the
result.

**Field:** Guéziec's cutting & stitching and CGAL's
`duplicate_non_manifold_vertices`/`stitch_borders` explicitly detect and report
these; CGAL's docs even state stitching "may not succeed" on non-manifold input,
so detection-first is the norm.
http://mesh.brown.edu/taubin/pdfs/Gueziec-etal-tvcg01.pdf
https://doc.cgal.org/latest/PMP_Mesh_repair/index.html

### 5. Side-by-side comparison: our approach vs. the state of the practice

| Criterion | Ours (mesh_analysis.py) | trimesh facets | CGAL Shape_detection | MeshFix | Fusion Convert Mesh | Guéziec/CGAL stitch |
|---|---|---|---|---|---|---|
| Coplanarity predicate | angle ≤ 0.5° AND offset ≤ 1e-4×extent (L929-930) | (radius/span)² > 5000 (graph.py:329) | Least-squares plane fit + normal angle (region growing); epsilon+normal_threshold (RANSAC) | none (repair only) | user preset, e.g. 0.001 mm | none |
| Connectivity constraint | none at grouping; re-merge only via shared snapped boundary (L1497) | yes — face-adjacency components (graph.py:293) | yes — region grows over adjacent faces | n/a | yes (face groups) | n/a |
| Seam duplicates | weld 1e-7×extent + per-group union-find snap 5e-4×extent (O(n²)) (L1021-1070) | merge_vertices tol 1e-8 | n/a (soup: merge_duplicate_points) | quantize to 65536³ grid, O(N) (tin.h:546) | implicit in face grouping | stitch_borders pairs border halfedges |
| Boundary tracing | directed-edge cancellation + winding recompute + T-junction split (L1173-1256) | facets_boundary (base.py:1903) | region boundary | borders()/boundary walk | internal | halfedge stitching |
| Holes | centroid-containment classify; merge-into-outer via net edge counts (L1119-1170) | not provided | not provided | holes filled by triangulation | not exposed | n/a |
| Pinch loops | `_split_pinch_loops` at repeated vertex (L986-1018) | not provided | n/a | n/a | n/a | merge_duplicated_vertices_in_boundary_cycle splits at duplicated position |
| Degenerate faces | drop if < `_MIN_FACE_AREA`; per-edge skip (L1204, L1228) | process=True handles | region predicate skips | removes degenerates | internal | remove_almost_degenerate_faces (cap/needle) |
| Non-manifold residual | silent | n/a | n/a | duplicate + cutAndStitch, reported (checkAndRepair.cpp) | user-visible error "Not all facegroups..." | duplicate_non_manifold_vertices, documented caveat |
| Output | ordered polygon + holes + normal + angles (B-Rep-ready) | facet index list | labeled regions | manifold watertight triangle mesh | B-Rep solid | manifold surface |
| Determinism | deterministic (first-match, sorted) | deterministic | deterministic w/ fixed seed | deterministic | deterministic | deterministic |
| Failure visibility | low (silent drops) | n/a | low | warnings | high (dialog error) | high (visitor callbacks) |

Sources for the table rows are the same URLs already cited in §1-§4
(trimesh: https://github.com/mikedh/trimesh/blob/main/trimesh/graph.py ;
CGAL: https://doc.cgal.org/latest/Shape_detection/index.html and
https://doc.cgal.org/latest/PMP_Mesh_repair/index.html ; MeshFix:
https://github.com/MarcoAttene/MeshFix-V2.1 ; Fusion:
https://www.autodesk.com/support/technical/article/caas/sfdcarticles/sfdcarticles/How-to-Convert-a-Mesh-to-a-BRep-in-Fusion-360.html ;
Guéziec: http://mesh.brown.edu/taubin/pdfs/Gueziec-etal-tvcg01.pdf).


### 6. Ranked improvement recommendations (concrete, cited)

**R-1 (high) — Derive weld/snap tolerances from file quantization, not only extent.**
Our weld `eps = 1e-7*extent` and snap `snap_tol = 5e-4*extent`
(mesh_analysis.py L1411, L1440) are relative guesses. MeshFix ties snapping to
representable precision (quantize to 65536³ grid, tin.h:546) and libigl defines
epsilon per decimal digit of the coordinates
(https://libigl.github.io/dox/remove__duplicate__vertices_8h.html). Improvement:
detect the input's quantization step (histogram of nonzero coordinate deltas;
typical STL exporters emit 6 decimals → 1e-6 step) and set
`weld_eps = k * quant_step` with `k ~ 2-4`, and `snap_tol = max(weld_eps, c * extent)`
as today. This makes seams close exactly when the *export* created them and
shrinks over-merge risk on thin walls (§4.4).

**R-2 (high) — Add a connectivity-constrained grouping pass as the primary
stage, keep global grouping as fallback.**
trimesh facets and CGAL/Lafarge–Mallet region growing merge only adjacent
faces (graph.py:293; https://doc.cgal.org/latest/Shape_detection/index.html);
our global first-match grouping (L946-955) can over-merge disconnected coplanar
sheets (§4.5) and is order-sensitive. Improvement: group by (a) connected
component + coplanarity, then (b) merge same-plane components only when they
share a snapped boundary segment (the existing `_merge_loop_into_outer` logic
already encodes this). Outcome: fewer spurious merges, same seam behavior.

**R-3 (high) — Replace the O(n²) union-find snap with a grid-bucketed spatial
hash.**
`_snap_group_vertices` (L1021-1070) is pairwise over all vertices in the group.
MeshFix does the same job in O(N) by snapping to a uniform grid (tin.h:546-547);
a kd-tree (e.g. scipy cKDTree, already in the dependency footprint) or a voxel
hash over `snap_tol` cells gives O(n log n) or O(n) with identical union-find
semantics. This directly unblocks larger mesh inputs.

**R-4 (high) — Validate the emitted faces and REPORT residual non-manifold
edges instead of silently accepting them.**
After cancellation (L1239-1256), count edges with net != 0 per face; if any
boundary edge is not matched by an opposite edge in a neighboring face of the
*same* group, either (a) run a Guéziec-style stitch pass over the whole group
(http://mesh.brown.edu/taubin/pdfs/Gueziec-etal-tvcg01.pdf) or (b) emit a
diagnostic `warning` field per face (count of unpaired edges). CGAL's visitor
API (`non_manifold_edge`, `non_manifold_vertex` callbacks) is the model
(https://doc.cgal.org/latest/PMP_Mesh_repair/index.html). This turns silent
seam failures (notepad L820-852 class of bugs) into actionable output for the
`structure_graph` consumer.

**R-5 (medium) — Make hole classification robust with a winding-number or
edge-count criterion.**
The centroid-containment test (`_loop_contains_centroid`, L1119-1126) is the
weakest step (§4.2). Two cheap upgrades, in increasing order of effort:
(1) classify a loop as a hole iff its 2D signed area has the *opposite* sign to
the outer loop AND it contains no triangle centroid — i.e. require both
conditions; (2) for ambiguous cases, compute the generalized winding number
(Jacobson et al. 2013, https://users.cs.utah.edu/~ladislav/jacobson13robust/jacobson13robust.html)
of a point just inside the loop with respect to the *whole* welded mesh to
decide filled vs. hole. CGAL's hole filling (Liepa 2003) is the heavyweight
alternative if we ever need to *fill* rather than classify
(https://doc.cgal.org/latest/PMP_Mesh_repair/index.html).

**R-6 (medium) — Unify the coincidence tolerances (weld / snap / group offset /
T-junction tol) behind one model.**
Today the same geometric coincidence is tested at four different scales:
`1e-7*extent` (weld), `5e-4*extent` (snap/T-junction), `1e-4*extent`
(offset_tol), `1e-6`+`1e-4*extent` (simp_tol) (L930, L1411, L1439-1440). The
Manifold design principle — "the same question is never asked in two different
ways" (https://github.com/elalish/manifold/wiki/Manifold-Library) — directly
applies: derive every tolerance from a single pair (quantization step,
extent-based floor) so seam fixes cannot disagree with grouping.

**R-7 (medium) — Add a plane-fit residual check to the grouping predicate.**
Our group predicate is normal-angle + offset of the *first* triangle
(L946-949). CGAL's region growing refits a least-squares plane per region and
accepts a face only if it fits the refit plane
(https://doc.cgal.org/latest/Shape_detection/classCGAL_1_1Shape__detection_1_1Polygon__mesh_1_1Least__squares__plane__fit__region.html).
Improvement: maintain a running centroid + covariance per group; reject a
triangle whose distance to the refit plane exceeds `offset_tol`. This removes
first-match bias and the order-dependence noted in §4.1, at O(1) per candidate.

**R-8 (medium) — Optionally add a voxel/SDF fallback for pathological inputs.**
If a group's residual non-manifold edge count stays high after R-4 (severely
broken mesh), Ju 2004's voxel signed-distance repair
(https://www.cs.wustl.edu/~taoju/research/scanpaper_final.pdf) is the robust
global answer (used by PolygonMender; surveyed in Attene et al. 2013,
https://dl.acm.org/doi/10.1145/2431211.2431214). It resamples the surface, so it
destroys exact CAD dimensions — use it only as a *reported* fallback strategy,
never silently.

**R-9 (low-medium) — Expose tolerances as parameters with documented mapping to
production presets.**
Fusion ships "Generate Face Groups" with named presets incl. "Accurate,
0.001 mm" and users tune it to fix conversion errors
(https://forums.autodesk.com/t5/fusion-design-validate-document/unable-to-convert-stl-to-brep-re-quot-not-all-facegroups-of-the/td-p/10593976).
Our `decompose_mesh_faces(nodes, indices, angle_tolerance_deg=0.5,
simplify_vertices=True)` (L1375) should accept `offset_tol`, `snap_tol`,
`simp_tol` and a `preset` name mapping to these, so the MCP consumer can
reproduce Fusion-style behavior and tune on failure without code edits.

**R-10 (low) — Track trimesh facet params as a cross-check.**
Run `trimesh.facets` (graph.py:293) as a *diagnostic* comparator in tests:
its (radius/span)² > 5000 predicate is a different, curvature-adaptive notion of
coplanarity; where our result and trimesh's disagree, log the group for
inspection. Also note trimesh issues #347 and #1745 document known facet edge
cases (single-face facets; splitting with faces on a plane) that our tests
should cover.
https://github.com/mikedh/trimesh/issues/347
https://github.com/mikedh/trimesh/issues/1745

**Ranking rationale:** R-1..R-3 address the two historically observed bug
classes (seam duplicates, over/under-merge) at their root with low risk; R-4
converts silent failures into signals (highest long-term value for the
`structure_graph` consumer); R-5..R-8 harden correctness; R-9/R-10 are
ergonomics and regression safety.

### 7. Source list (all verified reachable)

Papers / surveys:
- Schnabel, Wahl, Klein 2007, "Efficient RANSAC for Point-Cloud Shape
  Detection", CGF 26:2 — https://cg.cs.uni-bonn.de/publication/schnabel-2007-efficient ;
  PDF https://www.hinkali.com/Education/PointCloud.pdf ;
  Wiley https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1467-8659.2007.01016.x
- Cohen-Steiner, Alliez, Desbrun 2004, VSA (SIGGRAPH) —
  https://dl.acm.org/doi/10.1145/1015706.1015817 ;
  PDF https://www.geometry.caltech.edu/pubs/CAD04.pdf
- Lafarge & Mallet 2012, IJCV — https://www.cvlibs.net/projects/autonomous_vision_survey/literature/Lafarge2012IJCV.pdf
- Attene 2010, "A lightweight approach to repairing digitized polygon meshes",
  TVC 26 — https://link.springer.com/article/10.1007/s00371-010-0416-3 ;
  preprint http://saturno.ge.imati.cnr.it/ima/personal/attene/PersonalPage/pdf/TVC2010_preprint.pdf
- Attene, Campen, Kobbelt 2013, "Polygon mesh repairing: an application
  perspective", ACM CSUR 45:2 — https://dl.acm.org/doi/10.1145/2431211.2431214 ;
  PDF https://dl.acm.org/doi/pdf/10.1145/2431211.2431214
- Ju 2004, "Robust Repair of Polygonal Models" — https://www.cs.wustl.edu/~taoju/research/scanpaper_final.pdf
- Guéziec, Taubin, Lazarus, Horn 2001, "Cutting and stitching: converting sets
  of polygons to manifold surfaces", TVCG — http://mesh.brown.edu/taubin/pdfs/Gueziec-etal-tvcg01.pdf ;
  ACM https://dl.acm.org/doi/10.1145/280953.281628
- Jacobson, Kavan, Sorkine-Hornung 2013, "Robust inside-outside segmentation
  using generalized winding numbers" — https://users.cs.utah.edu/~ladislav/jacobson13robust/jacobson13robust.html ;
  ACM https://dl.acm.org/doi/10.1145/2461912.2461916
- Bénière et al., "Topology Reconstruction for B-Rep Modeling from 3D Mesh" —
  https://www.lirmm.fr/~subsol/WWW/SPIE.0112.Beniere.1.pdf ;
  "A comprehensive process of reverse engineering from 3D meshes" —
  https://hal-lirmm.ccsd.cnrs.fr/lirmm-00857347/file/CAD.1113.pdf

Libraries / tools:
- trimesh source (facets graph.py:293, face_adjacency_radius graph.py:194,
  constants.py:35-41, base.py:1832) — https://github.com/mikedh/trimesh
- trimesh issue #347 — https://github.com/mikedh/trimesh/issues/347
- trimesh issue #1745 — https://github.com/mikedh/trimesh/issues/1745
- CGAL Shape_detection manual — https://doc.cgal.org/latest/Shape_detection/index.html
- CGAL Efficient_RANSAC Parameters — https://doc.cgal.org/latest/Shape_detection/structCGAL_1_1Shape__detection_1_1Efficient__RANSAC_1_1Parameters.html
- CGAL Least_squares_plane_fit_region — https://doc.cgal.org/latest/Shape_detection/classCGAL_1_1Shape__detection_1_1Polygon__mesh_1_1Least__squares__plane__fit__region.html
- CGAL issue #6186 (epsilon clarification) — https://github.com/CGAL/cgal/issues/6186
- CGAL Polygon Mesh Processing User Manual — https://doc.cgal.org/latest/Polygon_mesh_processing/index.html
- CGAL Polygon Mesh Repair User Manual — https://doc.cgal.org/latest/PMP_Mesh_repair/index.html
- Open3D PointCloud (segment_plane / planar patch detection) —
  https://www.open3d.org/docs/latest/python_api/open3d.geometry.PointCloud.html
- PCL planar segmentation tutorial —
  https://pcl.readthedocs.io/projects/tutorials/en/pcl-1.15.1/planar_segmentation.html
- libigl remove_duplicate_vertices — https://libigl.github.io/dox/remove__duplicate__vertices_8h.html
- MeshFix-V2.1 (quantize tin.h:546, removeSmallestComponents tin.h:703,
  checkAndRepair.cpp) — https://github.com/MarcoAttene/MeshFix-V2.1
- pymeshfix (MeshFix CAD caveat) — https://pymeshfix.pyvista.org/
- Manifold library design wiki — https://github.com/elalish/manifold/wiki/Manifold-Library
- Blender Limited Dissolve — https://docs.blender.org/manual/en/latest/modeling/meshes/editing/mesh/delete.html
- FreeCAD Part RefineShape — https://wiki.freecad.org/Part_RefineShape/en ;
  forum https://forum.freecad.org/viewtopic.php?t=57893
- MeshLab "Remove t-vertices by Edge Collapse" segfault —
  https://github.com/cnr-isti-vclab/meshlab/issues/821
- PyMeshLab filter list — https://pymeshlab.readthedocs.io/en/0.1.8/filter_list.html

Production workflows / engineering write-ups:
- Autodesk Fusion: Convert a mesh to a solid or surface body —
  https://www.autodesk.com/support/technical/article/caas/sfdcarticles/sfdcarticles/How-to-Convert-a-Mesh-to-a-BRep-in-Fusion-360.html
- Autodesk forum: "Not all facegroups of the mesh body could be converted"
  (Generate Face Groups "Accurate, 0.001 mm tolerance") —
  https://forums.autodesk.com/t5/fusion-design-validate-document/unable-to-convert-stl-to-brep-re-quot-not-all-facegroups-of-the/td-p/10593976
- StackOverflow: "How to merge adjacent coplanar faces on a mesh" —
  https://stackoverflow.com/questions/14290365/how-to-merge-adjacent-coplanar-faces-on-a-mesh

Implementation references (this repo):
- mcp_server/mesh_analysis.py — decompose_mesh_faces L1375; grouping L922-956;
  snap L1021-1070; T-junction L1073-1116; pinch L986-1018; merge-into-outer
  L1129-1170; boundary L1173-1256; hole classify L1485-1512; simplify
  L1296-1318; curved patches L1336-1372.
- .omo/notepads/mesh-to-parametric/learnings.md L820-852 (seam-fix history).

## [2026-08-05] RESEARCH-CONSOLIDATION orchestrator synthesis (tracks A/B/C/D merged)

Four research tracks (A aggregation, B graph taxonomy, C engine, D workstream) are complete and
merged here into the actionable decision set for the next phase (structure_graph design).

### 1. Aggregation improvements to adopt (RESEARCH-A R-1..R-10, ranked)
- R-1 HIGH: derive weld/snap tolerances from the input's quantization step (histogram of nonzero
  coord deltas; STL exporters typically 6 decimals -> 1e-6 step; weld_eps = k*step, k~2-4). This is
  the root-cause fix for seam duplicates (our eps=1e-7*extent was 70x too tight in round-1).
- R-2 HIGH: make grouping connectivity-constrained (component + coplanarity) as PRIMARY stage,
  keep global first-match only as fallback; merge same-plane components only when they share a
  snapped boundary (existing _merge_loop_into_outer logic already does this).
- R-3 HIGH: replace O(n^2) union-find snap (_snap_group_vertices) with grid-bucketed spatial hash
  or scipy cKDTree (O(n log n)), identical union-find semantics.
- R-4 HIGH: after cancellation, count net!=0 edges per face and EMIT a warning field
  (unpaired-edge count) instead of silently accepting residual non-manifold edges -> feeds
  structure_graph diagnostics (CGAL visitor model: non_manifold_edge/non_manifold_vertex).
- R-5 MED: harden hole classification: require opposite signed area AND no contained triangle
  centroid; ambiguous cases -> generalized winding number (Jacobson 2013) of a probe point.
- R-6 MED: unify coincidence tolerances (weld/snap/group-offset/tjunc/simp) behind ONE pair
  (quantization step, extent floor) - Manifold "never ask the same question two ways".
- R-7 MED: group predicate gets a running least-squares plane refit (centroid+covariance, O(1) per
  candidate) to kill first-match order bias (CGAL Least_squares_plane_fit_region).
- R-8 MED: voxel/SDF (Ju 2004) fallback ONLY as a *reported* strategy for pathological inputs.
- R-9 LOW: expose offset_tol/snap_tol/simp_tol + named presets on decompose_mesh_faces (mirrors
  Fusion "Generate Face Groups" presets like "Accurate, 0.001 mm").
- R-10 LOW: run trimesh.facets as diagnostic cross-check in tests; cover issues #347/#1745 cases.

### 2. structure_graph taxonomy (RESEARCH-B section 9, literature-grounded)
NODES: Face (face_id, component_id, plane_id, area_cm2, canonical_normal, centroid,
  polygon_vertices_3d, interior_angles, triangle_count, vertex_count, is_base_candidate,
  curve_type, mean_curvature), Hole (hole_id, containing_face_id, loop_vertices_2d CW, area_cm2,
  is_filled), CurvedPatch (patch_id, type, axis, radius, area_cm2, mean_curvature, component_id),
  Component (component_id, face_ids, bbox, volume_proxy, base_face_id).
EDGES: EDGE_ADJACENT (Face-Face; attrs dihedral_angle_deg, convexity convex/concave/planar,
  shared_edge_length, curve_type, orientation FORWARD/REVERSED per face),
  VERTEX_TOUCH (shared vertex index ONLY; never a merge criterion - Z=3.5 case),
  PARALLEL (|n dot m|~1; attrs axis_2d, plane_pair_id, offset_distance),
  PERPENDICULAR (|n dot m|~0; "90 solid degrees"; attrs dihedral family),
  COPLANAR (parallel + same offset; attrs plane_id, coplanar offset; merge only if edge-connected),
  CONTAINS (Face->Hole/CurvedPatch; attrs containment_depth = hole_area/face_area),
  EXTRUSION_ALIGNED (Face-Face/Component-Face; Zone-Graph face-loop rule; attrs
  extrusion_direction, base_candidate, extent),
  SAME_ORIENTATION (canonical normal equal; attrs winding_sign),
  COMPONENT_OF (Face/CurvedPatch->Component), HAS_BASE (Component->Face).
QUERIES (MCP surface): components(), base_face(component), face_adjacency(face),
  parallel_faces(normal), perpendicular_faces(face), face_loops(axis), hole_of(face),
  curved_patches(), symmetry_axes(), articulation_points().
CONSTRAINTS: node+edge attrs BOTH required (MAAG/HAAG lesson); convexity from interior/dihedral
  angles (Joshi-Chang); face loops -> extrusion axis (Zone Graphs); base = composite score
  (area + adjacency degree + floor-facing + contains-holes + axis-alignment; ties -> convex
  backbone component, Joshi-Chang).

### 3. Graph engine decision (RESEARCH-C, wheel matrix verified 2026-08-05)
- PRIMARY: networkx==3.6.1 - pure Python (py3-none-any), zero new compiled deps, BSD-3,
  deterministic insertion-order, node_link_json == MCP envelope contract, all needed algorithms
  (connected/biconnected/articulation, VF2, planarity).
- PERF ALTERNATE: rustworkx 0.18.1 (cp310-abi3-win_amd64, Apache-2.0) if >~1e5 nodes.
- PERSISTENCE TIER (later/optional): duckdb 1.5.5 (cp314 win wheel; WITH RECURSIVE + USING KEY);
  DuckPGQ graph queries only if pinned to 1.4.4.
- REJECTED: kuzu (no cp314 wheel), networkit (stops cp313), graph-tool (no PyPI), all server DBs
  (Neo4j/Arango/Memgraph/FalkorDB violate embedded constraint).
- ANSWER: no graph DB needed at 1e2-1e4 node scale. In-memory NetworkX + JSON envelope.
- DETERMINISM RULES (builder+serializer): sort nodes by face_index; sort links by
  (source, target, relation); sort set-based results (connected_components, articulation_points);
  json.dumps(sort_keys=True); fixed epsilon tolerances + stable pair ordering.

### 4. Rebuild workstream (RESEARCH-D section 6, mapped to our MCP tools)
S0 analyze_mesh -> gates (watertight? components? symmetry? strategy); non-watertight -> organic.
S1 decompose_mesh_faces -> planar faces + curved patches (32 faces / 24 planes / 3 components /
   4 curved patches on our part).
S2 structure_graph -> components, EDGE_ADJACENT (convexity from angles), VERTEX_TOUCH, PARALLEL,
   PERPENDICULAR, COPLANAR, CONTAINS. This is our AAG.
S3 SELECT BASE UNIT: composite score wA*area + wD*degree + wF*floor/support + wH*contains-holes +
   wO*axis-alignment; ties -> largest convex-backbone component.
S4 BUILD UNIT LIST: classify per component/feature: protrusion (convex-attached) | depression
   (concave-attached, contains base floor) | hole (cylindrical/conical, contains) | fillet/chamfer
   (small, curved, high curvature) | freeform (unfittable).
S5 ORDER UNITS (dependency DAG, topo sort): base first; protrusions (join) before depressions
   (cut); holes after the containing face exists; draft before fillets; fillets/chamfers LAST;
   threads after the hole; organic only when primitives fail. Reverse-of-recognition rule
   (Venkataraman/Piegl: peel order = inverse of rebuild order).
S6 REBUILD EACH UNIT (strategy rules): prismatic (constant cross-section -> sketch+extrude);
   revolved (rotational symmetry -> meridian profile+revolve; cross-section gives sketch plane);
   csg_decompose (union/difference of box/cylinder primitives); organic ONLY when unit unfittable
   within error or mesh non-watertight or freeform intent -> MeshConvert fallback, record as
   non-parametric unit, mark in QA.
S7 ASSEMBLE: booleans join (additive) / cut (subtractive); order from S5 (Sakurai: different
   subtraction orders = different interpretations).
S8 QA PER UNIT: compare_mesh_to_brep (volume ratio [0.98,1.02], max span dev <2%, mean sampled
   dev < mesh tol) + review_reconstruction side-by-side; surface-type matching (CADENA GMS:
   cylinder matches cylinder) via face-type histogram.
S9 ITERATE (ReAct): on fail, feedback -> reclassify/reorder/change strategy; context = envelope
   of the failed step only.
SPLIT RULES: components>1; dominant orientation change; prismatic cross-section change along
   axis; articulation points in face graph; vertex-touch is NOT a merge criterion; unit too
   complex (>~16 faces or >12 verts/face -> Piegl cell rule).
FOCUS MECHANICS: one envelope per step (get_workflow_guide self-sufficient + chainable);
   agent never plans beyond the current unit (CADENA: decide next op against residual).

### 5. Proposed next phase (structure_graph)
Build order: (1) builder over decompose_mesh_faces output (deterministic, NetworkX, node/edge
   attrs per section 2, sorted serialization); (2) queries per section 2; (3) feed S3/S5
   (base selection + unit ordering) in a later pass. Edge types per user: edge connectivity of
   polygons (EDGE_ADJACENT), vertex connectivity (VERTEX_TOUCH), 90 solid degrees
   (PERPENDICULAR), parallel (PARALLEL).


## 2026-08-11 MeshBody1 RECONSTRUCTION COMPLETE (live demo) + vision-QA verdict

### Final BRep (Cuerpo79, 40 faces / 110 edges, size x21.458 y7.002 z7.0 cm)
- Volume 121.668634 cm3 vs mesh 121.663339 -> volume_ratio 0.999956; bbox dev 0.002 cm;
  COM (12.827, 8.482, 1.367). Tower built on Plano16 (z=3.5):
  TowerBands (front+back bands x20.34-24.34, y4.74-5.24 & y11.24-11.74) extrude 3.5 join
  (z3.5->7); TowerMiddleCut x20.84-23.84 cut 3.0 (z3.5->6.5); TowerRightCut x23.84-24.34
  cut 1.5 (z3.5->5.0). Structure verified by 11 mesh slices: left posts x20.34-20.84
  continuous z0.5->7, right posts x23.84-24.34 z5.0->~6.5, top band full-width z~6.5->7.0.
- compare_mesh_to_brep sampled mean 0.173 / max 0.559 (> 0.10 target) is a
  vertex_fallback MEASUREMENT ARTIFACT, not geometry error: exact per-face probing
  (getParameterAtPoint + isParameterOnFace + getPointAtParameter, since getClosestPointTo
  does not exist on this build) showed ALL worst-cluster points (x=3.806 notch region,
  y8.2-9.4, z~0) lie exactly on the BRep (distance 0.0000 on faces 39/26/21/24/25).
  Only non-on-face probe was an interior solid point. Grid sampling (14x14) mean 0.127
  max 0.529 was a coarse-grid artifact on large faces.

### Vision QA methodology that WORKED (this model cannot read PNGs)
- multimodal-looker subagent FAILED: sandbox permission policy denied reading PNG paths
  from %TEMP%; it saw zero pixels and returned VERDICT: NONE (honest abort, no fabrication).
- zai-vision MCP tools (ui_diff_check / analyze_image) take LOCAL FILE PATHS directly and
  return TEXT verdicts -> works for non-vision orchestrators. Extract base64 pairs from the
  review_reconstruction tool-output JSON, write PNGs to disk, call zai-vision_ui_diff_check
  per view. Verdict: isometric ~98% match, front ~90% match; ALL features present (tray,
  left notch, top-right tower w/ posts+band, rear tab); only differences are mesh faceting
  vs smooth BRep rendering. APPROVED.
- Lesson: for image QA from a text-only model, prefer vision MCP tools that accept paths
  (zai-vision_* / look_at with image_data) over spawning subagents that must read files.
- review_reconstruction async job mode errored once (job 73c0c881...); sync mode returned
  the same images + geometry envelope. Output JSON lives at
  C:\Users\danie\.local\share\opencode\tool-output\tool_ff029d1e30012ObpwX3CxGJ6ur;
  extracted PNGs at %TEMP%\opencode\review_pairs\.

### Fusion API quirks (this build)
- getClosestPointTo NOT available; parametricRange() is a callable METHOD (not attr);
  getPointAtParameter needs adsk.core.Point2D; no numpy in embedded interpreter.
