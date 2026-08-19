# mesh-to-parametric - Work Plan

## TL;DR (For humans)

**What you'll get:** A complete STL/mesh → fully-parameterized-solid workflow for the Fusion 360 MCP server. Import any STL/3MF mesh (existing `import_mesh_file`/`import_mesh_data`), then run four new capability layers: (1) **mesh analysis** — `analyze_mesh` reports watertightness, manifoldness, volume, bbox, symmetry, and primitive hints (plane/cylinder detection via pure-Python normal clustering + RANSAC), plus a recommended reconstruction strategy; (2) **mesh slicing** — `slice_mesh` computes plane-vs-mesh intersections into closed 2D polygon loops (the bridge to sketch→extrude parameterization); (3) **vision-guided parameter annotation** — `annotate_mesh_parameters` captures 4 viewport views, the calling model classifies the object from the images, and `select_parameter_schema` deterministically maps measured facts to a named parameter schema (stable names, ~20 mechanical part classes in `parameter_schemas.json`); (4) **mesh reconstruction** — `reconstruct_mesh` routes by strategy (prismatic→polygon+linear_extrude, revolved→rotate_extrude around construction axis, csg-decompose→boolean tree, organic→Fusion's `MeshConvertFeature` PREVIEW fallback), emits a JSON CSG tree that a new thin `create_from_csg_tree` dispatcher entry feeds into the **existing, verified `translate_to_fusion_commands` executor** (zero changes to `scad_translator.py`); the revolved strategy instead calls a dedicated construction-axis revolve handler (the executor's sketch-line revolve crashes on Fusion 2026). Plus `compare_mesh_to_brep` (vision-free geometric fidelity QA), `review_reconstruction` (vision QA loop), and `get_workflow_guide` (structured pipeline JSON so any client model can execute the whole workflow from the MCP surface alone).

**Why this approach:** All API facts were verified in planning research. The CSG executor (`scad_translator.py`) is reused **verbatim** — the reconstruction pipeline's only job is to EMIT `{"kind","params","children"}` CSG trees it already consumes. Fusion has **no native mesh sectioning or primitive-fitting API** — both are implemented in pure Python (no numpy: Fusion's embedded interpreter doesn't guarantee it) in new server-side modules (`mesh_analysis.py`, `mesh_slicer.py`) that are fully pytest-testable headless. Vision is **model-side**: the orchestration model sees screenshot PNGs (after fixing the broken `capture_screenshot` — the add-in currently omits `image_base64`, and the server's `Image(...)` branch is dead code); the server never calls an external vision API. `MeshConvertFeature` (July 2025, PREVIEW) is used only for the organic fallback, with the PREVIEW caveat documented.

**What it will NOT do:** No server-side vision API. No changes to `scad_translator.py` executor logic. No drawing/sheet generation. No multi-body mesh segmentation (single mesh body per reconstruction). No new dependencies.

**Effort:** 13 todos across 6 waves. Architecture-scale but fully testable: pure-Python geometry is headless-pytestable; the Fusion side is thin handlers.

**Risk:** `MeshConvertFeature` is a PREVIEW API (Autodesk: "never deliver programs using preview capabilities") — used only as the organic fallback and clearly labeled. The revolve path must use a **construction axis** (Fusion 2026 kernel bug: sketch-line revolve axes on XZ/YZ fail with ASM_PATH_TANGENT — verified in the prior plan). The `capture_screenshot` fix is the one prerequisite everything vision-related depends on.

**Decisions I made for you (from the approved draft, all reversible):**
- D1: Fix `capture_screenshot` to return `image_base64` (add-in handler + server Image branch) — vision prerequisite
- D2: Reconstruction EMITS CSG trees consumed by the existing executor via a NEW thin `create_from_csg_tree` dispatcher entry in `FusionMCP.py` (required: `_create_from_scad` guard-returns on empty `code`) — zero changes to `scad_translator.py`
- D3: CSG pipeline is the default reconstruction strategy; `MeshConvertFeature` Prismatic is an optional strategy
- D4: `MeshConvertFeature` Organic is the fallback-only path, with PREVIEW caveat documented
- D5: Triangle-plane intersection in pure Python for slicing (`displayMesh.nodeCoordinates` + `nodeIndices`)
- D6: Normal clustering + RANSAC in pure Python for primitive fitting
- D7: The orchestration model IS the vision — zero server-side vision API
- D8: Pure-data tools (`select_parameter_schema`, `get_workflow_guide`) are served locally by `fusion_server.py` — no Fusion HTTP round-trip needed

## Scope

### In scope
- **Component 1 — Mesh analysis:** `analyze_mesh` tool (topology: watertight/manifold/counts; volume via divergence theorem; bbox; symmetry; primitive hints via normal clustering + RANSAC; recommended strategy). New server module `mcp_server/mesh_analysis.py` (pure Python, numpy-free).
- **Component 2 — Mesh slicing:** `slice_mesh` tool (plane-vs-mesh intersection → ordered closed 2D polygon loops with hole detection). New server module `mcp_server/mesh_slicer.py` (pure Python, numpy-free).
- **Component 3 — Parameter schema library + matcher:** `mcp_server/parameter_schemas.json` (~20 mechanical part classes + generic fallback) + `select_parameter_schema` tool (deterministic role↔measured-fact matching, stable names). Server-local tool.
- **Component 4 — Vision-guided annotation:** `annotate_mesh_parameters` tool (4-view capture: isometric/front/top/right; returns `image_base64` list + measured facts; model classifies; matcher completes). Depends on the T1 screenshot fix.
- **Component 5 — Mesh reconstruction:** `reconstruct_mesh` tool — strategy router (prismatic / revolved / csg-decompose / organic), emits CSG tree → new `create_from_csg_tree` dispatcher entry → existing `translate_to_fusion_commands` executor → parametric timeline features; revolved uses a dedicated construction-axis revolve handler. Organic strategy via `MeshConvertFeature` (PREVIEW, labeled).
- **Component 6 — Fidelity QA:** `compare_mesh_to_brep` (volume/bbox/surface-deviation — vision-free) + `review_reconstruction` (screenshot pair → model flags missing features → iteration driver).
- **Component 7 — MCP workflow surface:** `get_workflow_guide` (structured pipeline JSON: ordered steps, MODEL ACTION markers, decision branches, fallbacks) + chainable output envelopes (`workflow` metadata: stage, next) + tool-description cross-references.
- **Component 8 — Tests + docs:** headless pytest suite (`test_mesh_analysis.py`, `test_mesh_slicer.py`, `test_parameter_schemas.py`), live suite (`test_mesh_reconstruction_live.py` following `test_openscad_live.py` pattern), README "Mesh Reconstruction" section.

### Must-NOT-Have (scope out)
- Server-side vision API integration (no external vision keys/dependencies) — vision is model-side only
- Changes to `mcp_server/scad_translator.py` executor logic (reuse as-is; reconstruction EMITS CSG trees)
- 2D drawing/sheet/draft generation
- Multi-body mesh segmentation (single mesh body per reconstruction; segmentation is future work)
- External CAD/CAE solver or geometry kernel dependency (Fusion API + pure Python only)
- numpy dependency (Fusion embedded Python does not guarantee it — pure Python math only)
- Automatic PR creation or upstream push
- Changes to the running add-in's installed copy (fresh-module pattern for live QA, same as prior plan)
- New pip dependencies (server-side stdlib only)

## Verification strategy

Every todo carries agent-executable acceptance criteria with:
- **Happy path:** Exact tool invocation + expected JSON output shape, verified against running Fusion 360 (live) or pytest (headless)
- **Failure path:** Expected error message/behavior for invalid inputs
- **Evidence:** Screenshots, `get_design_info` dumps, test PASS output

Pure-Python modules (`mesh_analysis.py`, `mesh_slicer.py`, `parameter_schemas.json` matcher, CSG emitter) are pytest-tested headless. Fusion-side handlers (mesh data extraction, view capture, MeshConvertFeature, compare) are tested live with the **fresh-module pattern** (`importlib.util.spec_from_file_location` → `mod.app = app` → `_process_command`, per `tests/test_openscad_live.py:128-148`) since the running add-in copy lags the repo.

**Baseline/characterization rule (start-work skill):** any todo touching existing behavior (T1 capture_screenshot fix) writes a characterization test FIRST that pins current behavior (no `image_base64` in response) and passes on unchanged code, then the failing-first proof for the new behavior before the fix.

## Execution strategy

Dependencies: T1 (screenshot fix) is the vision prerequisite (T5, T9 depend on it). T2 (analyze) and T3 (slice) are the geometry foundation (T6 depends on both). T4 (schemas) depends on T2's measured-facts shape. T6 (reconstruct) depends on T2+T3+T4. T7 (organic) extends T6's router. T8/T9 (QA) depend on T6. T10 (workflow guide) depends on all tool shapes. T11/T12/T13 (tests+docs) depend on the tools landing.

**File-conflict serialization:** `FusionMCP.py` and `mcp_server/fusion_server.py` are shared by most integration lanes → those lanes run SEQUENTIALLY (same-file writes serialize). Pure new files (`mesh_analysis.py`, `mesh_slicer.py`, `parameter_schemas.json`) are created by their owning lane. Wave 5 (test files + README, all disjoint files) runs fully PARALLEL.

**Architecture note (server-side computation):** new computational modules live in `mcp_server/` (system Python, headless-testable). Fusion-side additions are THIN handlers: `_extract_mesh_data` (dumps `displayMesh` triangles), `_capture_mesh_views` (4-viewport capture), `_create_from_csg_tree` (T6 — NEW dispatcher entry mirroring `_create_from_scad`'s translation flow but taking `csg_tree` directly; REQUIRED because `_create_from_scad` guard-returns on empty `code` at `FusionMCP.py:2053-2055`), `_revolve_cross_section` (T6 — construction-axis revolve; the executor's `rotate_extrude` sketch-line axis crashes with ASM_PATH_TANGENT on Fusion 2026), `_mesh_convert` (T7 — MeshConvertFeature), `_compare_mesh_brep` (T8), plus the T1 `_capture_screenshot` fix. `reconstruct_mesh` in `fusion_server.py`: extract mesh data → pure-Python analyze/slice/fit → emit CSG tree → `_call("create_from_csg_tree", {"csg_tree": tree, "units": units}, timeout=330)`; revolved → `_call("revolve_cross_section", {...})`. **NO mesh fallback from CSG trees** (there is no `.scad` source to render — `_create_from_scad`'s fallback needs `code`); unsupported nodes are hard errors; organic (T7) is the separate fallback.

## Todos

### Wave 1 — Vision prerequisite + mesh analysis

- [x] 1. Fix `capture_screenshot` to return `image_base64` (vision prerequisite)
  **References:** Add-in handler `_capture_screenshot` at `FusionMCP.py:2300-2305` returns only `{"screenshot": path, "size": ...}` — NO pixels. Server tool `capture_screenshot` in `mcp_server/fusion_server.py` (~line 1100) with a dead `Image(...)` branch at `fusion_server.py:1117-1122`. The `_call()` return convention: success dict → `json.dumps(data, indent=2)` string (`fusion_server.py:82-95`).
  **Acceptance:** (a) `FusionMCP.py` `_capture_screenshot` appends `image_base64` (base64.b64encode of the PNG file bytes read after `saveAsImageFile`/equivalent) alongside existing `screenshot`/`size` keys. (b) The server `capture_screenshot` tool's Image-block branch is ALREADY wired (`fusion_server.py:1117-1122`: `if b64: return [text, Image(data=base64.b64decode(b64), format="png")]`) — NO server change required beyond verifying it activates once the add-in sends `image_base64`; keep the `json.dumps(data)` text fallback for clients without image support. (c) Backward compatible: `screenshot` path + `size` keys unchanged. (d) Characterization test FIRST (per Verification strategy) — a headless test asserting current response lacks `image_base64`, then the fixed handler passes the new assertion.
  **QA happy (live, fresh-module):** call the fixed handler; assert `image_base64` is non-empty and `base64.b64decode(image_base64)` starts with PNG magic `\x89PNG`.
  **QA failure:** call with an invalid/closed document; assert graceful `{"error": ...}`.
  **Commit:** `fix: return image_base64 from capture_screenshot for vision clients`

- [x] 2. Add `analyze_mesh` tool + `mcp_server/mesh_analysis.py` pure-Python module
  **References:** MeshBody API: `.displayMesh` (always TriangleMesh), `.nodeCoordinates` (Point3D[]), `.nodeIndices`, `.normalVectors` (Vector3D[]), `.triangleCount`, `.nodeCount`, `.boundingBox`, `.isClosed` (F4 findings). Tool pattern `@mcp.tool()` + `_call("<key>", {...})` + dispatcher lambda (`FusionMCP.py:2347-2453`) (F1 findings). Parameter convention: cm (existing server convention).
  **Acceptance:** (a) New `mcp_server/mesh_analysis.py` exporting pure functions: `analyze_mesh_data(nodes, indices, normals)` → dict with `watertight` (every edge shared by exactly 2 triangles via node-index adjacency), `manifold`, `vertex_count`, `triangle_count`, `volume_cm3` (divergence theorem), `bounding_box_cm` [min,max], `symmetry` (plane candidates from paired bbox extremes + normal-vector mirror agreement), `primitive_hints` (plane regions via normal clustering with angle threshold; cylinder via RANSAC on normal lines; box via 3 orthogonal plane-pair fits), `recommended_strategy` (`prismatic` | `revolved` | `csg_decompose` | `organic` from hint confidence). All math pure-Python (no numpy). (b) `FusionMCP.py` new `_extract_mesh_data` handler: resolves mesh by name-or-index (`_find_mesh_body`-style), dumps `displayMesh` node coords/indices/normals as JSON (round to 6dp). (c) `fusion_server.py` tool `analyze_mesh(mesh: str = "0", units: str = "cm")`: calls `_call("extract_mesh_data", ...)`, runs pure analysis, scales to requested units, returns the full report dict incl. `recommended_strategy`. Error: mesh not found → `{"error": "Mesh body '<ref>' not found."}`.
  **QA happy (headless):** pytest: feed a synthetic unit cube triangle mesh → watertight True, volume 1.0, bbox [0,0,0]..[1,1,1], `recommended_strategy` prismatic.
  **QA happy (live, fresh-module):** `import_mesh_data` tetra/cube fixture (as in prior Todo 12), then `analyze_mesh` → report with counts > 0, `watertight` bool present.
  **QA failure (live):** `analyze_mesh(mesh="999")` → `{"error": ... not found ...}`.
  **Commit:** `feat: add analyze_mesh tool with pure-python mesh topology analysis`

### Wave 2 — Slicing + schema library

- [x] 3. Add `slice_mesh` tool + `mcp_server/mesh_slicer.py` pure-Python module
  **References:** No native mesh sectioning API (F4) — manual triangle-plane intersection on `displayMesh` data. 2D polygon bridge: CSG polygon `params["pts"]`/`["points"]` + `["paths"]` holes (`scad_translator.py:1267-1339`; F2). Units cm.
  **Acceptance:** (a) New `mcp_server/mesh_slicer.py` exporting pure functions: `slice_mesh_at(nodes, indices, height_or_plane)` → ordered closed 2D loops: for each triangle compute segment-plane intersections (Möller–Trumbore or segment clipping), collect points, chain into loops via adjacency graph, detect holes (winding/containment), return `{"loops": [{"pts": [[x,y],...], "is_hole": bool}], "plane": {...}}`. Robust: degenerate triangles (coplanar/on-plane) skipped; duplicate points deduped (epsilon 1e-9). (b) `fusion_server.py` tool `slice_mesh(mesh: str = "0", axis: str = "Z", height_cm: float = 0.0, units: str = "cm")`: extract via `_call("extract_mesh_data", ...)`, compute, return loops (scaled). Plane def: axis (X/Y/Z) + signed height along axis (cm). Error: axis invalid → `{"error": "Axis must be X, Y, or Z"}`.
  **QA happy (headless):** unit cube sliced at mid-height along Z → 1 square loop `[0,0]-[1,0]-[1,1]-[0,1]` (oriented), no holes. Hollow box (outer cube minus inner cube as triangle soup with consistent winding) sliced → outer loop + inner hole loop.
  **QA happy (live, fresh-module):** `import_mesh_data` cube fixture → `slice_mesh(axis="Z", height_cm=0.5)` → 1 loop with 4 pts.
  **QA failure (live):** `slice_mesh(axis="W", ...)` → axis error; mesh 999 → not-found error.
  **Commit:** `feat: add slice_mesh tool with pure-python plane intersection`

- [x] 4. Create `parameter_schemas.json` + `select_parameter_schema` tool (server-local)
  **References:** `add_parameter`/`update_parameter`/`list_parameters` handlers at `FusionMCP.py:1544-1568` (F1) — the annotation pipeline applies schemas through these existing tools. Pure-data tool: served directly by `fusion_server.py`, no Fusion round-trip (D8).
  **Acceptance:** (a) New `mcp_server/parameter_schemas.json`: ~20 classes — knob, bracket, housing, flange, pulley, gear, bearing_holder, handle, latch, mount, cover, plate, shaft, bushing, spacer, washer, bolt, nut, ring, link — each `{"class": str, "description": str, "strategy_hint": str, "parameters": [{"role": str, "name": str, "kind": "length|diameter|radius|angle|count|thickness", "source": "bbox|slice|fit|vision"}]}` plus `generic` fallback class. (b) Matcher logic (pure Python, e.g. `mcp_server/parameter_schemas.py` loader+matcher): `select_schema(class_name, measured_facts)` → resolves class, assigns each role from measured facts (bbox dims → width/depth/height, slice loop diameter → diameter, fit params → radius/thickness), returns `{"class": str, "parameters": [{"name": str, "value": float, "unit": str, "confidence": float}], "unmatched_roles": [str]}`. Unknown class → falls back to `generic` with a `note`. (c) `fusion_server.py` tool `select_parameter_schema(object_class: str = "generic", measured_facts: dict = {}, units: str = "cm")` returning the match result.
  **QA happy (headless):** measured_facts `{"bbox_cm": [4,4,3], "slice_diameter_cm": 4.0}` + class `bolt` → parameters include named `bolt_head_diameter`/`bolt_length` (or equivalent role-based stable names) with values from facts, confidence > 0.
  **QA happy (live):** call tool via server; assert response JSON has `class`, `parameters` non-empty for a known class.
  **QA failure (headless):** `select_schema("nonexistent_class", {})` → returns `generic` fallback, `note` present, never raises.
  **Commit:** `feat: add parameter schema library and deterministic matcher`

### Wave 3 — Vision annotation + reconstruction (lanes T5→T6→T7 SEQUENTIAL — each writes `FusionMCP.py` + `mcp_server/fusion_server.py`)

- [x] 5. Add `annotate_mesh_parameters` tool (vision-guided annotation)
  **References:** Depends on T1 (image_base64) and T2 (measured facts) and T4 (matcher). Viewport capture exists as `capture_screenshot`; camera/view control via `app.activeViewport.camera` (`viewOrientation` / `viewOrbit`-style manipulation). 4 views default: isometric, front (XZ), top (XY), right (YZ).
  **Acceptance:** (a) `FusionMCP.py` new `_capture_mesh_views` handler: for each of 4 view names, set the viewport orientation (isometric/front/top/right), capture PNG via the fixed screenshot path, collect `{"view": str, "image_base64": str}`; restore isometric. Return the list + target mesh name. (b) `fusion_server.py` tool `annotate_mesh_parameters(mesh: str = "0", views: list = ["isometric","front","top","right"], units: str = "cm")`: measured facts via `_call("extract_mesh_data", ...)` → `analyze_mesh_data`; the 4 captured views via `_call("capture_mesh_views", ...)` using INLINE `requests.post` (same pattern as `capture_screenshot` at `fusion_server.py:1110-1113` — NOT `_call`, whose `json.dumps` text would bury the images so MCP clients could not display them; Metis F4). Returns `[text_envelope, Image(v1), Image(v2), Image(v3), Image(v4)]` where `text_envelope` is `{"mesh": str, "views": [{"view": str, "image_base64": str}], "measured_facts": {...}, "workflow": {"stage": "annotate", "next": "MODEL ACTION: classify the object from the views, then call select_parameter_schema with object_class and measured_facts"}}` and each `Image` is a decoded PNG block. (c) Model-side classification is NOT in the server — the tool returns images + facts; the orchestration model supplies `object_class` to `select_parameter_schema`.
  **QA happy (live, fresh-module):** after T1, call `annotate_mesh_parameters` on a mesh → 4 views, each `image_base64` decodes to PNG; `measured_facts` non-empty; envelope `workflow.stage == "annotate"`.
  **QA failure (live):** mesh not found → error envelope.
  **Commit:** `feat: add annotate_mesh_parameters tool with 4-view capture`

- [x] 6. Add `reconstruct_mesh` tool — strategy router emitting CSG trees (prismatic/revolved/csg-decompose)
  **References:** Depends on T2 (analysis) + T3 (slicing) + T4 (schemas). Executor reuse: `translate_to_fusion_commands(csg_nodes, root, design, units)` (`scad_translator.py:1716-1746`); node schema `{"kind","params","children"}` (`:167-168`); polygon pts/paths (`:1267-1339`); linear_extrude (`:1109-1134`); `_create_from_scad` accepts a pre-resolved `csg_tree` param BUT guard-returns on empty `code` (`FusionMCP.py:2053-2055`) → T6 adds a CSG-only dispatcher entry `create_from_csg_tree` (Metis F1 BLOCKER fix). **Revolve MUST use construction axis** (`root.zConstructionAxis` — XZ/YZ sketch-line axes hit ASM_PATH_TANGENT on Fusion 2026; verified prior plan). `_patch_boolean_types_aliases()` (`FusionMCP.py:1979-2005`) required before any combine.
  **Acceptance:** (a) New server module `mcp_server/mesh_csg.py` (pure Python): `build_csg_tree(mesh_data, strategy, params)` → `List[Dict]` CSG tree. `prismatic` — slice at N heights, verify constant cross-section (loop shape similarity), emit `{"kind":"polygon","params":{"pts":[...]}, "children":[{"kind":"linear_extrude","params":{"height":...}}]}` (holes → `paths`); `csg_decompose` — region-grow plane clusters, fit boxes/cylinders per cluster, emit `union` tree with per-primitive translate/rotate; else raise `UnsupportedStrategyError`. The `revolved` strategy is NOT emitted as CSG — it produces a half cross-section profile (pts from the mesh's intersection with a plane containing the Z axis) for the revolve handler (3b). (b) `FusionMCP.py` NEW dispatcher entry `create_from_csg_tree` + handler `_create_from_csg_tree(root, p)`: mirrors `_create_from_scad` (`FusionMCP.py:2051-2127`) — snapshot names/counts, `_patch_boolean_types_aliases()`, `translate_to_fusion_commands(csg_tree, root, design, units)`, `_cleanup_translation` on failure — but takes `csg_tree` DIRECTLY (no `code`, no empty-code guard; Metis F1) and returns a HARD error on `UnsupportedSCADNodeError` with NO mesh fallback (no `.scad` source exists; Metis F2). If translation succeeds but creates 0 bodies → `{"error": "translation produced no bodies"}`. (c) `FusionMCP.py` NEW dispatcher entry `revolve_cross_section` + handler `_revolve_cross_section(root, p)`: draws the provided profile pts as sketch lines on `xZConstructionPlane`, revolves via `revolveFeatures.createInput(profile, root.zConstructionAxis)` (CONSTRUCTION AXIS — the executor's sketch-line revolve crashes with ASM_PATH_TANGENT on Fusion 2026; Metis F3), returns `{"bodies": int, "names": [str], "features": int}`. (d) `fusion_server.py` tool `reconstruct_mesh(mesh: str = "0", strategy: str = "auto", units: str = "mm")`: `auto` → `analyze_mesh_data` recommendation; prismatic/csg_decompose → extract → build tree → `_call("create_from_csg_tree", {"csg_tree": tree, "units": units}, timeout=330)`; revolved → extract → compute profile → `_call("revolve_cross_section", {"profile_pts": [...], "angle": 360, "units": units})`. Return `{"strategy": str, "method": "csg_translation"|"revolve", "bodies": int, "features": int, "csg_nodes": int}`. Unknown strategy → `{"error": "Unknown strategy '<s>'. Supported: auto, prismatic, revolved, csg_decompose"}` (organic added by T7 — Metis F5).
  **QA happy (headless):** build_csg_tree on a synthetic prismatic mesh → tree kind `polygon` with `linear_extrude` child, pts match slice loops.
  **QA happy (live, fresh-module):** cube mesh → `reconstruct_mesh(strategy="prismatic")` → method `csg_translation`, BRep body appears, `get_timeline_info` shows ExtrudeFeature. Cylinder mesh → `strategy="revolved"` → BRep with RevolveFeature (construction axis). Bracket-like union of 2 boxes → `csg_decompose` → CombineFeature.
  **QA failure (live):** `strategy="organic"` before T7 → unknown-strategy error (list excludes organic until T7; Metis F5); `strategy="bogus"` → unknown-strategy error; degenerate mesh producing 0 bodies → `{"error": "translation produced no bodies"}` hard error with no partial bodies left (cleanup verified via `get_design_info`).
  **Commit:** `feat: add reconstruct_mesh tool with CSG-tree strategy router`

- [x] 7. Add organic strategy to `reconstruct_mesh` via `MeshConvertFeature` (PREVIEW)
  **References:** MeshConvertFeature (July 2025 PREVIEW, F4): `design.features.meshConvertFeatures.createInput(meshBodies)` → `MeshConvertFeatureInput`; `.method` = Faceted(0)/Prismatic(1)/Organic(2); `.operation` = ParametricFeature(0, timeline)/BaseFeature(1, dumb); `add(input)` → feature, `.bodies` → BRepBodies. PREVIEW warning documented. Prismatic method also exposed as `reconstruct_mesh(strategy="prismatic_fusion")` optional alternate.
  **Acceptance:** (a) `FusionMCP.py` new `_mesh_convert` handler: `params {mesh, method (prismatic|organic), operation ("parametric"|"base")}` → resolve MeshBody, create input, set method/operation, `add()`, return `{"converted": True, "bodies": int, "names": [str], "method": str, "preview_api": True}`. Detect absence via BOTH `hasattr(design.features, "meshConvertFeatures")` guard AND try/except around `createInput` — either path → `{"error": "MeshConvertFeature not available on this Fusion build (PREVIEW API)"}` (never crash; Metis missing-criterion fix). (b) `reconstruct_mesh` router gains `strategy="organic"` → `_call("mesh_convert", {method:"organic"})` → envelope `{"strategy": "organic", "method": "mesh_convert", "preview_api": true, "parametric": bool, "note": "Organic conversion is a PREVIEW API result, not a parameterized solid."}`. (c) Tool description documents the PREVIEW caveat.
  **QA happy (live, fresh-module):** organic strategy on a rounded/irregular mesh → `converted: True`, body appears (may be BaseFeature/dumb — report `parametric` accurately).
  **QA failure (live):** strategy `organic` on a build without the API → graceful not-available error (never crash).
  **Commit:** `feat: add organic MeshConvertFeature strategy with PREVIEW caveat`

### Wave 4 — Fidelity QA + workflow surface (lanes T8→T9→T10 SEQUENTIAL — each writes `mcp_server/fusion_server.py`)

- [x] 8. Add `compare_mesh_to_brep` tool (vision-free fidelity QA)
  **References:** MeshBody `.volume`/`.boundingBox`; BRep `body.physicalProperties.volume`/`body.boundingBox`; closest-point via `surfaceEvaluator.getClosestPointTo` (learned in prior plan Wave 1). Depends on T6.
  **Acceptance:** (a) `FusionMCP.py` new `_compare_mesh_brep` handler: given mesh ref + BRep body ref, compute `{"mesh": {"volume_cm3", "bbox_cm"}, "brep": {"volume_cm3", "bbox_cm"}, "volume_ratio": float, "bbox_max_deviation_cm": float, "sampled_deviation_cm": {"mean": float, "max": float, "samples": int}}` — sample ~200 mesh vertices, `getClosestPointTo` on the BRep surface, report mean/max distance. (b) `fusion_server.py` tool `compare_mesh_to_brep(mesh: str = "0", body: str = "0")` returning the dict. Missing refs → clear errors. (c) FIRST live smoke-test `SurfaceEvaluator.getClosestPointTo(point)` on this build (Metis F6 — unverified in prior plan); if unavailable, fall back to sampled mesh-vertex → nearest BRep vertex / bbox-based deviation and report the method used in the response.
  **QA happy (live, fresh-module):** cube mesh (import_mesh_data) + reconstructed BRep cube → `volume_ratio` ≈ 1.0 (within 5%), `bbox_max_deviation_cm` < 0.1.
  **QA failure (live):** body ref not a BRep body → error.
  **Commit:** `feat: add compare_mesh_to_brep geometric fidelity QA`

- [x] 9. Add `review_reconstruction` tool (vision QA loop)
  **References:** Depends on T1 (screenshots) + T6 (reconstruction). Same viewport capture machinery as T5.
  **Acceptance:** (a) `fusion_server.py` tool `review_reconstruction(mesh: str = "0", body: str = "0", views: list = ["isometric","front","top"])`: captures ORIGINAL mesh + RECONSTRUCTED body views via INLINE `requests.post` (NOT `_call` — images must be MCP `Image` blocks to be visible; Metis F4, same pattern as T5). Returns `[text_envelope, Image(mesh_iso), Image(brep_iso), Image(mesh_front), Image(brep_front), Image(mesh_top), Image(brep_top)]` where `text_envelope` is `{"pairs": [{"view": str, "mesh_image_base64": str, "brep_image_base64": str}], "geometry": <compare_mesh_to_brep summary>, "workflow": {"stage": "review", "next": "MODEL ACTION: compare each pair; if features are missing call reconstruct_mesh again with feedback or accept with select_parameter_schema"}}`. (b) Reuses `_capture_mesh_views` (target = mesh) + a body-targeted variant (BRep body — the same viewport capture works on any visible geometry).
  **QA happy (live, fresh-module):** after a successful T6 reconstruction → pairs has 3 entries, both image_base64s decode PNG, geometry summary present.
  **QA failure (live):** mesh or body missing → error envelope.
  **Commit:** `feat: add review_reconstruction vision QA loop`

- [x] 10. Add `get_workflow_guide` tool + chainable output envelopes + tool-description cross-references
  **References:** Tool pattern (`fusion_server.py:114-117`, F1). Pure-data tool served locally (D8).
  **Acceptance:** (a) `mcp_server/workflow_guide.py` (or embedded dict) with `GUIDE`: ordered steps `[import → analyze → slice → annotate → select_parameter_schema → reconstruct → compare → review]`, each `{"tool": str, "purpose": str, "inputs": [str], "outputs": [str], "model_action": str|None, "branch": {…}|None, "fallback": str|None}`. MODEL ACTION markers on annotate (classify) and review (compare/accept). Branches: strategy auto-decision; fallback: organic→mesh_convert, unsupported→mesh_fallback. (b) `fusion_server.py` tool `get_workflow_guide(step: str = "")` → FULL guide when `step` is empty/default, single step when named (explicit empty-step behavior; Metis missing-criterion fix). (c) New tool docstrings cross-reference their pipeline slot ("Stage 2 of the mesh-to-parametric workflow; see get_workflow_guide"). (d) New tools' envelopes carry `workflow: {stage, next}` metadata (retrofit onto T5/T6/T8/T9 outputs where already specified).
  **QA happy (live):** `get_workflow_guide()` → JSON with ≥8 steps, MODEL ACTION markers present; `get_workflow_guide(step="reconstruct")` → single step with inputs/outputs/fallback.
  **QA failure (live):** `get_workflow_guide(step="bogus")` → `{"error": "Unknown workflow step 'bogus'"}`.
  **Commit:** `feat: add get_workflow_guide tool and workflow envelopes`

### Wave 5 — Tests + docs (PARALLEL lanes — disjoint files)

- [x] 11. Headless pytest suite for pure-Python modules
  **References:** `tests/conftest.py` TrapRoot + sys.path pattern (prior-plan learning, Todo 17); pytest without Fusion. Modules under test: `mesh_analysis.py`, `mesh_slicer.py`, `parameter_schemas.py`, `mesh_csg.py`.
  **Acceptance:** New `tests/test_mesh_analysis.py`, `tests/test_mesh_slicer.py`, `tests/test_parameter_schemas.py`, `tests/test_mesh_csg.py`. Coverage: watertight/manifold/volume/bbox/symmetry on synthetic meshes (cube, open box, tetra); slice loops incl. hole detection + degenerate-triangle skip; schema matcher known-class + generic fallback + unmatched roles; CSG emission prismatic/revolved/csg-decompose + `UnsupportedStrategyError`. `py -m pytest tests -v` → all pass, no Fusion, no network.
  **QA happy:** `py -m pytest tests -v` exit 0.
  **QA failure:** deliberately wrong expectation in one assertion → AssertionError (proves machinery live).
  **Commit:** `test: add headless pytest suite for mesh analysis/slicing/schemas/csg`

- [x] 12. Live integration test suite `tests/test_mesh_reconstruction_live.py`
  **References:** `tests/test_openscad_live.py` harness: `_poll`/`_await_count`/`_await_min` family (`:177-221`), fresh-module pattern (`:128-148`), `clear_design` before each check, `--mode http|fresh` with http as bridge-probe, `run_check` + SkipCheck/BridgeError handling (prior-plan learnings, Todo 20).
  **Acceptance:** Script exercises (each check prints PASS/FAIL): (1) `import_mesh_data` cube fixture → `analyze_mesh` → watertight true, strategy prismatic; (2) `slice_mesh` mid-height → 1 loop 4 pts; (3) `reconstruct_mesh(strategy="prismatic")` → csg_translation, ExtrudeFeature in timeline, bbox matches; (4) cylinder mesh → `strategy="revolved"` → BRep, RevolveFeature (construction axis); (5) `select_parameter_schema` with facts → named params; (6) `annotate_mesh_parameters` → 4 views with decodable PNGs; (7) `compare_mesh_to_brep` original vs reconstructed → volume_ratio within tolerance; (8) `get_workflow_guide()` → 8+ steps; (9) organic strategy → converted or graceful not-available (SKIP if API absent); (10) failure paths: `analyze_mesh(mesh="999")`, `slice_mesh(axis="W")`, unknown strategy. Fresh-mode via execute_script embedding (repr-safe, linear code — prior-plan gotchas). Retry-poll all cross-boundary reads.
  **QA happy:** run with Fusion + bridge → all non-skipped checks PASS, exit 0.
  **QA failure:** Fusion closed → `--mode http` probe reports bridge error, exit 1 (honest failure).
  **Commit:** `test: add live mesh reconstruction integration tests`

- [x] 13. Update README.md with Mesh Reconstruction section
  **References:** `C:\Users\danie\fusion-mcp\README.md` — new "Mesh Reconstruction" section after "OpenSCAD Pipeline".
  **Acceptance:** README documents: the workflow (import → analyze → slice → annotate → reconstruct → compare/review), all new tools (`analyze_mesh`, `slice_mesh`, `select_parameter_schema`, `annotate_mesh_parameters`, `reconstruct_mesh`, `compare_mesh_to_brep`, `review_reconstruction`, `get_workflow_guide`), the vision note (model-side classification; `capture_screenshot` returns `image_base64`), and the `MeshConvertFeature` PREVIEW caveat on the organic strategy.
  **QA happy:** read README, verify all new tools listed with descriptions.
  **QA failure:** N/A.
  **Commit:** `docs: document mesh-to-parametric workflow in README`

## Final verification wave

- [x] F1. Plan compliance audit — Verify every todo implemented with the exact tool signatures, return shapes, error handling, and envelope fields specified. Cross-reference each `@mcp.tool()` in `fusion_server.py` with its handler in `FusionMCP.py` dispatcher (including the 16 tool-name→dispatcher-key mismatches convention: e.g. tool `analyze_mesh` → key `analyze_mesh` or aliased consistently). Verify `capture_screenshot` returns `image_base64` + ImageContent. Verify zero changes to `scad_translator.py`.
- [x] F2. Code quality review — Verify: no bare `except:` in new code; all Fusion API calls wrapped in try/except with meaningful errors; consistent JSON shapes; no hardcoded paths; type hints on new functions; pure-Python modules are numpy-free (stdlib only) and headless-importable; `py_compile` clean on all changed files; `pytest tests -v` passes headless.
- [x] F3. Real manual QA — Agent-executed live verification against running Fusion 360: (a) import a cube mesh via `import_mesh_data`, run `analyze_mesh` → report, capture screenshots as evidence; (b) `reconstruct_mesh(strategy="prismatic")` → verify BRep with timeline ExtrudeFeature, then `compare_mesh_to_brep` volume_ratio ≈ 1; (c) `annotate_mesh_parameters` → verify 4 PNG views decode; (d) `get_workflow_guide` → verify full JSON; (e) `reconstruct_mesh(strategy="organic")` → verify converted body OR graceful not-available. All assertions programmatic (JSON field checks), evidence screenshots in a QA dir.
- [x] F4. Scope fidelity — Confirm: NO server-side vision API calls; NO `scad_translator.py` changes; NO drawing generation code; NO multi-body segmentation; NO numpy dependency; NO new pip deps; README section exists; all 8 components implemented; tests pass.

## Commit strategy

Each todo is a separate atomic commit with the prefix specified in its Commit line (`fix:`/`feat:`/`test:`/`docs:`). Stage ONLY the files named in the todo by explicit path — never stage `.omo/` or `.sisyphus/`. All commits go to the current branch (`master`, fork `danielperezr88/fusion-mcp`). No PRs, no upstream push.

## Success criteria

1. All 13 implementation todos complete with acceptance criteria met
2. All 4 final verification items (F1-F4) pass
3. `py -m pytest tests -v` passes without Fusion running (existing 31 + new mesh suites)
4. `tests/test_mesh_reconstruction_live.py` passes with Fusion running (all non-skipped checks)
5. `capture_screenshot` returns `image_base64` (decodable PNG) — vision prerequisite verified live
6. `analyze_mesh` correctly reports topology/volume/bbox/symmetry/primitives on synthetic + live meshes
7. `slice_mesh` produces correct closed loops incl. holes on synthetic + live meshes
8. `reconstruct_mesh` produces native parametric timeline features (Extrude/Revolve/Combine) for prismatic, revolved, and csg-decompose strategies via the UNCHANGED `scad_translator.py` executor (prismatic/csg-decompose through the new `create_from_csg_tree` entry; revolved through the construction-axis `revolve_cross_section` handler)
9. `select_parameter_schema` returns stable named parameters from measured facts (no model-invented names)
10. Organic strategy works via `MeshConvertFeature` or fails gracefully with the PREVIEW caveat (never crashes)
11. `get_workflow_guide` lets a fresh client model execute the full workflow from the MCP surface alone (tool descriptions + envelopes + guide are self-sufficient)
12. README documents the mesh-to-parametric workflow
