# Issues — mesh-graph-pipeline

Problems and gotchas encountered during work on this plan.

_Auto-scaffolded by /start-work. Append new entries below - never overwrite._

---

## [F1 Audit] Plan Compliance Audit — 2026-08-05

**Auditor: Sisyphus-Junior (F1 Gate)**
**Plan: `.omo/plans/mesh-graph-pipeline.md` (237 lines)**
**Evidence: `.omo/notepads/mesh-graph-pipeline/learnings.md` (821 lines)**

### T1 R-1: Quantization-step tolerances ✅
- Plan: `_detect_quantization_step(node_list)` in mesh_analysis.py
- Actual: L1003 in mesh_analysis.py — exactly named ✅
- Wired into `decompose_mesh_faces` (L2130-2131) and `analyze_mesh_data` (L2489-2492) ✅
- Formula: `eps = max(max(1e-9, 1e-7*extent), 3*quant_step)` ✅
- Formula: `snap_tol = max(1e-5, max(5e-4*extent, 2*quant_step))` ✅
- Small-step cap, recurrence guard, safe default (returns 0.0) — all per plan ✅

### T2 R-2: Connectivity-constrained grouping ✅
- Plan: `_group_planar_triangles` connectivity first pass, global fallback
- Actual: L1220 — connectivity-constrained region-growing FIRST PASS, global fallback ✅
- Cluster near vertices for seam bridging (L1275: `_cluster_near_vertices`) ✅
- Fallback avoids re-matching connectivity groups ✅
- Deterministic: seed order by (-area, ti), sorted neighbor expansion ✅

### T3 R-3: Spatial-hash snap ✅
- Plan: Replace O(n²) with grid-bucketed spatial hash in `_snap_group_vertices`
- Actual: L1450 — floor-key spatial hash, 27-cell neighborhood ✅
- Cell key: `floor(x/snap_tol)` per plan spec ✅
- "Smaller root absorbs larger" union rule preserved ✅
- Reference implementation kept in test file for equivalence verification ✅

### T4 R-4: Non-manifold edge warnings ✅
- Plan: `_count_residual_loop_edges`, `residual_out` kwarg on `_boundary_loops`
- Actual: `_count_residual_loop_edges` at L1622, `residual_out=None` at L1645 ✅
- Per-face `warnings: [{"type": "unpaired_boundary_edges", "count": N}]` ✅
- Top-level `"has_warnings": bool` ✅
- Post-pinch counting (true residual) per plan ✅

### T5: build_structure_graph ✅
- Plan: `build_structure_graph(decompose_result: dict) -> nx.Graph`
- Actual: L~300 in mesh_graph.py — exact signature ✅
- 4 node types (Face/Hole/CurvedPatch/Component) + all plan attrs ✅
- 10 edge types (EDGE_ADJACENT, VERTEX_TOUCH, COPLANAR, SAME_ORIENTATION, PARALLEL, PERPENDICULAR, CONTAINS, EXTRUSION_ALIGNED, COMPONENT_OF, HAS_BASE) ✅
- EDGE_ADJACENT consecutive-pair rule (not just shared vertices) ✅
- EXTRUSION_ALIGNED replaces geometric relations (not physical) per plan precedence ✅
- No numpy in mesh_graph.py — imports only networkx, json, math, collections ✅

### T6: DuckDB persistence ✅
- Plan: `_persist_to_duckdb(graph, mesh_key)` with nodes/edges tables
- Actual: L616 in mesh_graph.py ✅
- Parameterized SQL (executemany with ?) ✅
- LRU cache MAX_GRAPHS=16 (OrderedDict) ✅
- `_get_graph_db(mesh_key)` with KeyError contract ✅
- Dynamic schema (sorted union of present attrs) ✅
- Deviation: DuckDB 1.5.5 can't switch in-memory conn to read-only (documented, T8 enforces at statement level) ✅

### T7: structure_graph MCP tool ✅
- Plan: `structure_graph(mesh="0", units="cm")` → summary JSON
- Actual: `structure_graph(mesh="0", units="cm", angle_tolerance_deg=None, offset_tol=None, snap_tol=None, simplify_vertices=True, preset=None)` at L1529 ✅
- Summary keys: mesh, units, component_count, face_count, edge_type_counts, base_face_candidates, has_warnings, duckdb_table_schema, query_example, articulation_points, connected_component_count, workstream ✅
- All required keys present, extras are additive ✅
- `_load_mesh_graph()` lazy importer at L89 (mirrors `_load_mesh_analysis` pattern) ✅

### T8: query_structure_graph MCP tool ✅
- Plan: `query_structure_graph(mesh="0", sql="")` → JSON rows
- Actual: L1735 — exact signature ✅
- Statement-level read-only enforcement (`_assert_read_only_sql` at L1684) ✅
- Deterministic ordering (sort by first col, NULLs last) ✅
- Recursive CTE support documented in docstring ✅
- Deviation: `SET access_mode='read_only'` impossible on in-memory DuckDB 1.5.5 → statement-level gate (justified, documented in learnings T6/T7/T8) ✅

### T9: Workstream computation ✅
- Plan: `_score_base_faces`, `_classify_units`, `_build_dependency_order`
- Actual: L588, L691, L813 in mesh_graph.py — all three present ✅
- Score formula: 0.35*area_norm + 0.25*degree_norm + 0.15*floor_facing + 0.15*contains_holes + 0.10*axis_alignment ✅
- Unit types: base > hole > fillet/chamfer > protrusion/depression > freeform ✅
- Dependency ordering: topological sort, base first, fillets last, cycle breaking ✅
- `NetworkXUnfeasible` caught at L878 ✅

### T10: Workstream wiring ✅
- Plan: Enrich structure_graph summary with workstream key
- Actual: L1627-1650 in fusion_server.py — reads graph attrs set by T9 ✅
- Summary shape: `base_face_per_component`, `unit_types`, `rebuild_order`, `dag_has_cycles` ✅
- Primary `base_face_candidates` key preserved (backward compat) ✅
- Dictionary keys deterministically sorted ✅

### T11 R-5: Hardened hole classification ✅
- Plan: `_generalized_winding_number`, `_same_sign_2d`, `_centroid_near_boundary`
- Actual: L1786, L1839, L1848 — all present ✅
- Signed area pre-check (opposite sign + no centroid → hole) ✅
- Same sign → always merge ✅
- Ambiguous → winding number fallback (Jacobson 2013) ✅
- Guard: `not residual_out.get("residual_pairs")` (corrected from plan's `has_warnings` — documented in learnings) ✅
- `_compute_interior_probe_3d` exists at L1919 ✅

### T12 R-6+R-7: Unified tolerance + plane-fit ✅
- Plan: `_ToleranceConfig` dataclass with 7 fields
- Actual: L51 — `@dataclass(frozen=True)` class `_ToleranceConfig` ✅
- Fields: quant_step, extent, weld_eps, snap_tol, offset_tol, simp_tol, tjunc_tol ✅
- `from_()` classmethod: all formulas match R-1 ✅
- All inline tolerance computations replaced with `tol.*` references ✅
- R-7: `_accept_by_running_plane_fit` at L1165 ✅
- R-7: `_smallest_eigenvector_3x3` at L86 (closed-form 3×3) ✅
- cnt < 3 guard (rank-deficient covariance) ✅
- Order-independence test added ✅

### T14 R-8: Voxel/SDF fallback detection ✅
- Plan: >5% unpaired edges → `strategy_fallback_suggested: "organic"`
- Actual: L2369-2407 in decompose_mesh_faces ✅
- Ratio: `unpaired_total / (3 * len(welded_tris))`, threshold 0.05 ✅
- Conditional key: `strategy_fallback_suggested` + `unpaired_pct` ✅
- Propagation to structure_graph summary: L1671-1677 ✅
- No voxelization/SDF code written (detection only) ✅

### T15 R-9: Presets + wiring ✅
- Plan: `decompose_mesh_faces` with preset/override params, wired through tools
- Actual: `decompose_mesh_faces(nodes, indices, angle_tolerance_deg=None, simplify_vertices=True, offset_tol=None, snap_tol=None, simp_tol=None, preset=None)` at L2065 ✅
- `accurate`: angle=0.1, offset_tol=max(1e-8, 1e-6*extent), snap_tol=max(1e-8, 1e-6*extent) ✅
- `balanced`: defaults ✅
- `coarse`: angle=1.0, offset_tol=max(1e-6, 1e-3*extent), snap_tol=max(1e-5, 1e-3*extent) ✅
- Invalid preset → ValueError ✅
- `analyze_mesh` tool passes params through (L1431-1493) ✅
- `structure_graph` tool passes params through (L1529, L1597-1601) ✅
- Deviation: `angle_tolerance_deg=None` (sentinel) instead of `=0.5` — required for preset angle override (documented in learnings T15) ✅

### T16 R-10: trimesh.facets cross-check ✅
- Plan: Diagnostic function in `tests/` (not production code)
- Actual: `tests/trimesh_facets_diag.py` (293 lines) + `tests/test_trimesh_facets_cross_check.py` ✅
- `_compare_with_trimesh_facets` helper (faces cross-check) ✅
- Assertions via warnings (not failing) ✅
- Covers facet_threshold=5000, issue #347, issue #1745 ✅
- No production code touched ✅

### Cross-references
- **ZERO changes to scad_translator.py**: confirmed via `git diff 6c451cf..HEAD --stat -- mcp_server/scad_translator.py` (no output) and `git log --oneline -15 -- mcp_server/scad_translator.py` (last 5 commits predate this plan) ✅
- **requirements.txt**: `networkx>=3.6.1` (L9), `duckdb>=1.5.5` (L10) ✅
- **No numpy in mesh_graph.py**: grep confirms zero numpy/scipy imports ✅
- **All output dict keys additive**: `has_warnings`, `warnings`, `strategy_fallback_suggested`, `unpaired_pct` are new keys; no existing keys removed ✅
- **structure_graph + query_structure_graph**: both @mcp.tool() registered in fusion_server.py ✅
- **analyze_mesh no simp_tol**: grep confirms no `simp_tol` in fusion_server.py ✅

### Plan deviations (all justified)
1. `angle_tolerance_deg: Optional[float] = None` instead of `=0.5` — needed for preset angle override sentinel (learnings T15)
2. DuckDB read-only at statement level instead of connection — DuckDB 1.5.5 limitation (learnings T6/T7/T8)
3. T7 summary has extra keys (articulation_points, connected_component_count, workstream) — all additive
4. T11 guard uses `residual_out.get("residual_pairs")` not `has_warnings` — correct per-group signal (learnings T11, dead-guard bug found + fixed by orchestrator)

---

## F4 SCOPE FIDELITY AUDIT — 2026-08-05

### G1 — scad_translator.py untouched
**PASS.** `git diff 814324d~1..HEAD --stat -- mcp_server/scad_translator.py` → no output (empty diff). Plan commit range (814324d..b7d7c9d) introduced zero changes to that file. The scad_translator.py commits (8a7236a, 69b89ff, 32f98f3, 7b27e48, 94123c1) predate the plan entirely.

### G2 — No external graph database
**PASS.** `duckdb.connect(":memory:")` at line 1075 of `mesh_graph.py` — pure in-memory, no persistent `.db` file path. DuckDB graphs are ephemeral (lost on MCP server restart, by design).

### G3 — No server-side vision API calls
**PASS.** All 8 `requests.post/get` calls in `fusion_server.py` target `FUSION_URL` (local Fusion 360 bridge at 127.0.0.1:7432). No `openai`, `anthropic`, or `httpx` calls exist. No image payloads sent to external APIs. Classification is model-side per README.

### G4 — No full voxelization/SDF implementation
**PASS.** Grep for `voxel|sdf|repair|marching` in both `mesh_graph.py` and `mesh_analysis.py` returned zero matches. R-8 is detection-only: `strategy_fallback_suggested` flag + `unpaired_pct` metric. No marching cubes, no SDF reconstruction, no voxel repair pipeline.

### G5 — No breaking changes to decompose output keys
**PASS.** `decompose_mesh_faces` return dict base keys: `components_detected`, `planar_faces`, `curved_patches`, `has_warnings`. New R-8 keys (`strategy_fallback_suggested`, `unpaired_pct`) are conditionally ADDED (lines 2405-2407) — never overwrite base keys. Exception path adds `error` key only. `analyze_mesh_data` return dict is additive too (adds `face_decomposition` as sub-key). Zero keys removed or renamed.

### G6 — No numpy in mesh_graph.py
**PASS.** The only matches for `numpy`/`np.` in `mesh_graph.py` are docstring comments at lines 115 and 219: `"Pure Python (math only, no numpy)"` and `"tiny pure-math helpers (no numpy)"`. Zero numpy imports or usage.

### G7 — Only structure_graph + query_structure_graph added as graph MCP tools
**PASS.** `fusion_server.py` has exactly two graph-query function definitions: `structure_graph` (line 1529) and `query_structure_graph` (line 1735). Support functions (`_load_mesh_graph`, `_GRAPH_EDGE_RELATIONS`, `_base_face_candidates`) are private helpers. No additional `@mcp.tool()` graph functions exist.

### G8 — No multi-body segmentation
**PASS.** `structure_graph(mesh="0")` takes a single `mesh` body identifier and calls `extract_mesh_data({"mesh": mesh})` for that one body only. No loop over all bodies. `query_structure_graph` also scoped to one mesh per call.

### G9 — README.md mesh-reconstruction bullets present
**PASS.** README lines in the "Mesh Reconstruction" section include full bullets for both `structure_graph` (11-line description with tolerance params, presets, workstream, DuckDB schema, query example) and `query_structure_graph` (12-line description with read-only SQL enforcement, recursive CTE support, schema docs, examples). Classification-is-model-side note present.

### G10 — Headless pytest suite
**PASS.** `248 passed, 18 deselected` in 20.48s. Zero failures.

### Summary
All 10 scope-fidelity gates pass. The plan implemented exactly what it committed to: structure graph (+DuckDB persistence), query tool (read-only SQL), tolerance presets (R-9), pathological seam detection (R-8), scored base-face workstream (T9), and test cross-checks (R-10) — with zero scope creep into scad_translator, external DBs, server-side vision, voxel/SDF pipelines, numpy dependencies, multi-body segmentation, or breaking API changes.

---

## F2 CODE QUALITY REVIEW — 2026-08-05

**Auditor: Sisyphus-Junior (F2 Gate)**
**Plan: `.omo/plans/mesh-graph-pipeline.md`**
**Changed files: `mcp_server/mesh_analysis.py`, `mcp_server/mesh_graph.py`, `mcp_server/fusion_server.py`, `tests/test_*.py`**

### Gate 1 — No bare `except:` in new/changed code ✅ PASS
- Only bare `except:` in the workspace: `fusion_server.py:293` (pre-existing, commit c346404, 2026-03-08, by Anonimus124).
- Zero plan-commit changes to that line.
- All new code uses `except Exception`, `except ValueError`, `except KeyError`, `except ImportError`, `except BaseException` (transaction rollback), etc.

### Gate 2 — `_call`/dispatch/fusion-server HTTP paths wrapped in try/except ✅ PASS
- `_call()` (L269-282): catches `requests.exceptions.ConnectionError` → `"Cannot reach Fusion 360..."`, `Exception` → `"Unexpected error: {e}"` ✅
- `structure_graph()` (L1594-1680): entire pipeline wrapped in `except Exception as e` → `{"error": f"structure graph failed: {e}"}` ✅
- `query_structure_graph()` (L1818-1840): catches `KeyError` (no graph), `ValueError` (non-SELECT SQL), `Exception` (SQL error) — each with distinct `{"error": ...}` messages ✅

### Gate 3 — Consistent JSON envelope shapes ✅ PASS
- Error paths consistently return `json.dumps({"error": "..."})` across all three new code paths.
- Success: `structure_graph` returns `{mesh, units, component_count, ...}`, `query_structure_graph` returns `{mesh, columns, rows, row_count}`.
- No error path returns bare strings or inconsistent keys.

### Gate 4 — No hardcoded paths ✅ PASS
- `mesh_graph.py`: zero path references (pure computation + in-memory DuckDB).
- `fusion_server.py`: uses `os.path.dirname(os.path.abspath(__file__))` + `os.path.join` for relative resolution in `_load_mesh_graph()`.
- Zero absolute paths (`C:\...`, `/Users/...`, `/home/...`) in plan-committed changes.

### Gate 5 — Type hints on new functions ✅ PASS (with minor notes)
- `mesh_graph.py` public functions: ALL have full type hints: `build_structure_graph(...) -> nx.Graph`, `_persist_to_duckdb(...) -> duckdb.DuckDBPyConnection`, `_get_graph_db(...) -> duckdb.DuckDBPyConnection`, `_score_base_faces(...) -> Dict[str, float]`, `_classify_units(...) -> Dict[str, str]`, `_build_dependency_order(...) -> Tuple[List[str], bool]`.
- `_ToleranceConfig` dataclass: all 7 fields typed `float`, `from_()` classmethod fully typed.
- `structure_graph()` and `query_structure_graph()`: full parameter + return type annotations.
- `analyze_mesh_data()` new params: `angle_tolerance_deg: Optional[float]`, `offset_tol: Optional[float]`, `snap_tol: Optional[float]`, `simp_tol: Optional[float]`, `simplify_vertices: bool`, `preset: Optional[str]` ✅
- MINOR: `decompose_mesh_faces` new params (L2065) lack type hints — `nodes, indices, angle_tolerance_deg=None, offset_tol=None, snap_tol=None, simp_tol=None, preset=None` are all unannotated. Pre-existing: the `nodes, indices` params were already untyped in the original signature.
- MINOR: `_assert_read_only_sql(sql)` at L1684 lacks parameter and return type annotations.

### Gate 6 — mesh_graph.py is numpy-free ✅ PASS
- Imports: `json, logging, math, re, collections.OrderedDict/ defaultdict, typing, duckdb, networkx`.
- Zero `numpy`/`np.` usage outside docstring comments ("Pure Python (math only, no numpy)").
- `mesh_analysis.py` legitimately uses numpy (mesh triangle processing) — within scope.

### Gate 7 — py_compile clean ✅ PASS
- `mesh_analysis.py`, `mesh_graph.py`, `fusion_server.py` — all three compile cleanly with `py -3.14 -m py_compile`.

### Gate 8 — pytest tests/ -v passes headless ✅ PASS
- **265 passed, 1 xfailed, 0 failures** in 82.77s.
- XFAIL: `test_qa_failure_proof_machinery_live` (expected failure, machinery proof test).
- All new test files pass: `test_hole_classification_r5.py` (5), `test_presets_tolerances.py` (8), `test_r8_fallback_detection.py` (4), `test_tolerance_plane_fit.py` (7), `test_mesh_graph.py` (20), `test_mesh_graph_db.py` (6), `test_structure_graph_tool.py` (17), `test_workstream.py` (27), `test_trimesh_facets_cross_check.py` (5).

### Gate 9 — duckdb import succeeds on Python 3.14 ✅ PASS
- `duckdb.__version__` = `"1.5.5"` — meets `>=1.5.5` requirement.

### Verdict Summary
All 9 mandatory F2 gate criteria pass. Two MINOR findings (type hints on `decompose_mesh_faces` new params and `_assert_read_only_sql`) are non-blocking and consistent with the pre-existing partial-typing pattern in `mesh_analysis.py`. No regressions, no security issues, no bare-except violations, clean compilation, full test suite green.

**VERDICT: APPROVE**
