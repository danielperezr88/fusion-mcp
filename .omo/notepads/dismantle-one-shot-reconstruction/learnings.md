# Learnings — dismantle-one-shot-reconstruction

Conventions, patterns, and successful approaches discovered during work on this plan.

_Auto-scaffolded by /start-work. Append new entries below - never overwrite._

---

## 2026-08-07 — F1 docstring audit folded into dismantle commit

- Stale docstring refs in `mcp_server/mesh_graph.py` (`_classify_units`, `_build_dependency_order`, `rebuild_order`) were removed — docstring-only (5 insertions, 36 deletions), no behavior change.
- Folded into the atomic dismantle commit via `git commit --amend --no-edit` (message preserved verbatim). Pre-flight `git branch -r --contains` was empty → amend safe (commit never pushed).
- Amending replaces the hash (`2a4761c` → `e7fb28b`); the original message is unchanged. Only `mcp_server/mesh_graph.py` was staged — unrelated `.omo/` files left untracked, `mcp_server/*.py` (other than mesh_graph.py) untouched.


## F3 — fresh-process MCP probe (2026-08-07)

- The previous F3 FAIL was caused by probing the STALE long-running MCP server process
  (pre-dismantle code). The fix that worked: run a dedicated `py -3.13` process that
  imports the working-tree `mcp_server/fusion_server.py` directly and calls the tool
  functions in-process — no MCP transport at all.
- Import trick: `sys.path.insert(0, <repo root>); sys.path.insert(0, <repo root>/mcp_server);
  import fusion_server`. Assert `os.path.normcase(os.path.abspath(fs.__file__))` equals the
  working-tree path to guarantee no stale copy was picked up.
- Tool registry introspection for mcp SDK 1.26 (installed on `py -3.13`, NOT `py` default
  which has mcp 2.0.0 without `mcp.server.fastmcp`):
  `fs.mcp._tool_manager._tools` is a plain dict {name: Tool}. Choose the interpreter that
  matches the deployed server env (1.26.0).
- The add-in (FusionMCP.py) is unchanged and was NOT the source of the removed
  fields: `recommended_strategy`/`unit_types`/`rebuild_order`/`workflow` dict are all
  computed SERVER-side (mesh_analysis/mesh_graph/fusion_server), so a fresh-process probe
  against the unchanged add-in correctly shows the dismantled outputs.
- `get_bodies_info` lists ONLY BRep bodies (`root.bRepBodies`) — it cannot discover mesh
  bodies. Mesh discovery must probe `analyze_mesh(mesh=...)` (it 404s `extract_mesh_data`
  errors as `{"error": "Mesh body '...' not found."}`).
- `slice_mesh` loop dicts use key `pts` (not `points`) — assert on `loops` length / `pts`.
- `annotate_mesh_parameters` / `review_reconstruction` with `job_id="sync"` return a list:
  `[text_envelope_str, Image(...), ...]` where `Image` is `mcp.server.fastmcp.Image`.
  Envelope = `json.loads(result[0])`.
- Discovery on this run: doc had NO BRep and a mesh at index 0 (MeshBody1); probe created
  a 10cm BRep via sketch/extrude (`Cuerpo75`, index 0) — the prescribed fallback path
  (create_sketch → draw_center_rectangle → finish_sketch → extrude_sketch) works end-to-end.
- F3 VERDICT: OVERALL PASS — all 11 probe checks green (registry + 10 foundational tools).

## [2026-08-07] MeshBody2 acceptance analysis (atlas)
- Analytical point-to-BRep distance over ALL 4882 MeshBody2 verts (exact AABB math on the 9-cut box union): GLOBAL MAX = 0.933 cm at (24.432, 19.269, 3.5), i.e. ONLY the documented high-X fin exclusion. 4580/4882 (93.8%) verts within 0.1 cm; ZERO verts > 1.0 cm.
- compare_mesh_to_brep 'vertex_fallback' sampled max 2.942485 is an ARTIFACT: this Fusion build lacks SurfaceEvaluator.getClosestPointTo, so the fallback approximates surface distance and inflates it. Same artifact inflated MeshBody1's accepted sampled max 2.38. Trust ratio + bbox + analytical vertex distance, not sampled_deviation in this build.
- Volume deficit 4.07 cm3 (ratio 1.033387) fully accounted: high-X fin material = front wall ext (0.933x0.5x3.5) + back ext + floor-level fin (270 verts, y 13.27-14.41, z 0-0.5) + mid-band fragment (22 verts, y 14.41-19.27, z 0-3.5) ~= 4.0 cm3. Hangers negligible (6 verts, z=0 only).
- Fin zone inventory (x>23.499): 318 verts total. y 12.77-13.27: 16 @ z0-3.5; y 13.27-14.41: 270 @ z0-0.5; y 14.41-19.27: 22 @ z0-3.5; y 19.27-19.77: 10 @ z0-3.5.
- Earlier 'no high-X at Z=0.25 slice' finding CONTRADICTS direct vertex probe (270 fin verts at z 0-0.5). Direct displayMesh.nodeCoordinates read is authoritative; treat the slice-tool/agent slice conclusion as erroneous (slice tool documented unreliable).
- Bridge script scope note: functions defined inside execute_script cannot see sibling names (exec scope quirk) - write FLAT inline scripts (no helper defs) to avoid NameError.


## [2026-08-11] analyze_mesh face-decomposition parameter investigation (atlas)

- Scope change from user: STOP mesh?BRep reconstruction of MeshBody2. Investigate the face-decomposition implementation and find params that reproduce MeshBody2's TRUE analytic counts (27 planar + 20 curved) instead of default output (992 planar + 127 freeform curved).
- Data source: _extract_mesh_data (FusionMCP.py:2842) uses ody.displayMesh, coords rounded to 6dp. Dumped MeshBody2 displayMesh to C:\Users\danie\AppData\Local\Temp\opencode\meshb2_data.json (4882 nodes / 4938 tris; 2309/4938 after welding) � dump reproduces the MCP default baseline EXACTLY (992 planar / 127 curved freeform, 14.8s/call).
- Preset resolution (mesh_analysis.py:2120-2152): default angle 0.5deg; accurate?0.1 + offset/snap=max(1e-8,1e-6*extent); coarse?1.0 + offset/snap=max(1e-6,1e-3*extent). Explicit angle/offset/snap always override. edge_adj_tol in _group_planar_triangles is HARDCODED max(1e-5, 5e-4*extent) � 0.0107 (NOT driven by snap_tol).
- SWEEP RESULTS (all 16 targeted combos): angle 1-5deg = NO effect (992); only 10deg?554, 30deg+offset0.5?192 planar. offset_tol 0.01-0.2 = NO effect. Presets 991-997. curved = 127 in EVERY run, identical tri-hist (97x2, 27x1, 3x3), all area 0.0, all freeform. NO parameter combo reaches 27/20.
- ROOT CAUSE: the 20 hole cylinder walls (r=0.25, z 0-0.5, ~96-108 tris each, ONE connected mesh component, 49 distinct azimuth buckets spanning 356deg) are absorbed into the PLANAR grouping as 2-triangle quad faces (974 of 992 planar faces = hole-wall quads, normals radial like (0.657,0.754)). The 20 true curved surfaces NEVER reach _extract_curved_patches; the 127 "curved" patches are 230 leftover slivers (area 0.0).
- WHY params can't fix it: region growing is connectivity-constrained AND planarity-gated (running plane fit, offset_tol=2.147e-3). Micro-test on hole[0] wall alone: even at 60deg angle, max group = 4 tris (2 quads). Sagitta math: 2-quad arc (15deg) on r=0.25 has sagitta 0.002139 � offset_tol 0.002147 � the offset gate stalls growth after ~2 quads; larger angle can't help because the gate is offset-based, and a cylinder is genuinely non-planar so no plane fits it within tolerance.
- Also: mesh IS watertight-false/manifold-false (some non-2 edges) but that is NOT the fragmentation cause; it is a single connected component.
- CONCLUSION: 27/20 is UNREACHABLE with current implementation params � it's an algorithmic gap (curvature surfaces eaten as planar quads; no curvature gate before planar grouping), not a tuning problem. Fix would require implementation change (e.g., classify curved patches first, or add curvature detection to reject hole walls from planar grouping).

## [2026-08-11] joint planar+curved prototype (atlas)

PROTOTYPE: C:\Users\danie\AppData\Local\Temp\opencode\joint_experiment.py (+ joint_results.jsonl). Pure-Python, fully offline on meshb2_data.json dump. Imports mcp_server.mesh_analysis internals directly.

ALGORITHM (connected-components-of-small-groups + dual-model competition):
1. Call existing _group_planar_triangles at given (angle, offset) -> groups with per-triangle indices.
2. Build group adjacency from shared edges.
3. Find connected components of SMALL groups (<=30 tris each). KEY INSIGHT: wall quads at default (2-4 tris each, ~48 per wall) are edge-adjacent within each wall but NOT across walls (separated by large plate-face groups). So connected-components of small groups recovers each wall as ONE cluster -- 20 wall clusters + 1 false-positive (side-wall fragments spanning the mesh).
4. Fit cylinder on each cluster via _fit_cylinder. The 20 wall clusters each yield axis=[0,0,1], r=0.2498, confidence=1.0, residual=0.000475 cm -- PERFECT fits.
5. Cylinder merge: same axis direction + radius + **axis-line distance** < 0.1 cm. Must check LINE coincidence, not just direction/radius -- the 20 walls all have axis=[0,0,1] r=0.25 but distinct centers spaced >=1cm apart, so they correctly do NOT merge.
6. Post-hoc plane merge: adjacent plane regions with matching canonical normal/offset (5deg / 0.05cm).
7. Model competition on large groups (>30 tris): fit both plane and cylinder, assign lower residual. All large groups stay planes (cylinder fit has huge residual on flat faces).

RESULTS (all 10 joint configs give identical output -- the algorithm is CONFIG-INVARIANT):
- **Baseline: 992 planar / 127 curved (all freeform, area=0.0). Coverage 95.3%, but 974 of 992 planar faces are wall-quad fragments (2-4 tris each).**
- **Joint: 31 planar / 20 curved (ALL cylinders, r=0.25). Coverage 95.0%. Distance to target (27,20) = 4.**
- Radius histogram: `{"0.25": 20}` -- all 20 cylinders recover the true r=0.25 cm hole radius.
- Runtime: 1.2-1.3s per joint config (vs 15s for full decompose_mesh_faces baseline -- _group_planar_triangles alone is fast; the 15s is outline extraction).

WHY 31 NOT 27: the 4 extra planar faces are from the high-X fin/extension area -- tilted wall fragments (normals 7-18deg off the main axes, e.g., [-0.124,0.992,0]) representing chamfered/angled edges. These are genuinely distinct faces in the mesh topology (different normals AND offsets from any large group), not algorithmic failures. The BRep target of 27 likely merges these via topological face splitting rules. A fin-area Z=0.5 surface is also coplanar with the plate top but not edge-connected to it (separate mesh island), adding 1 more.

TWO BUGS FOUND AND FIXED:
1. _fit_cylinder computes xis_point = verts.mean(axis=0) over the FULL vertex array passed as 
odes_array. The original caller (_classify_curved_patch) passes patch.vertices (compact local array). Passing global _arr computes the GLOBAL mesh centroid as axis_point -> wrong radius (10cm instead of 0.25cm) and wrong residual. FIX: build compact local vertex array per cluster via 
p.unique(faces_sub.ravel(), return_inverse=True).
2. Cylinder merge must check axis-LINE coincidence, not just axis direction + radius. Without it, all 20 walls (same axis [0,0,1], same r=0.25) merge into 1 cylinder. FIX: compute perpendicular distance between axis lines -- etween = ap_j - ap_i; perp = between - dot(between, axis) * axis; line_dist = norm(perp) -- and require < 0.1 cm.

VERDICT: the joint dual-model approach reaches **(31, 20)** -- distance 4 from the BRep target (27, 20), with all 20 cylinders at the correct r=0.25 cm. The approach is CONFIG-INVARIANT (works identically at angle 0.5-30deg and offset 0.002-0.5), runs in ~1.3s (vs 15s baseline), and produces zero degenerate curved patches (baseline had 127 area-0.0 freeform slivers). The 4-planar gap is from fin-area topology, not algorithm failure. To close the gap: either (a) accept 31 as correct for the mesh topology, or (b) add a tolerance for merging coplanar-but-disconnected faces (risky -- could merge genuinely separate faces). The algorithmic fix for the production pipeline is clear: after _group_planar_triangles, run connected-components on small groups, fit cylinders, and reassign -- BEFORE calling _extract_curved_patches.

## [2026-08-11] universal delta estimator calibration (atlas)

SCRIPT: `C:\Users\danie\AppData\Local\Temp\opencode\delta_calibration.py` (+ `delta_results.jsonl`). Pure-Python, fully offline. Validates a surface-type-agnostic chordal-error estimator that replaces the fixed epsilon_accept=0.01 with a derived tolerance.

THE ESTIMATOR (local planarity residual + Otsu gap detection):
1. For each triangle, build 1-ring neighborhood (triangle + edge-adjacent triangles). Filter neighbors by normal consistency (|dot| >= cos(45deg)) to exclude sharp dihedral edges.
2. Fit plane via PCA on the filtered neighborhood's vertices (smallest-eigenvector normal). Residual = mean abs vertex-to-plane distance.
3. Accumulate residuals over all triangles (stride sample for >5k tris). Histogram on log10(residual).
4. Otsu threshold on log-residuals maximizes between-class variance -> split point between noise mode (flat areas) and delta mode (curved areas).
5. delta_est = **median of curved-mode residuals** (residuals ABOVE the Otsu threshold), NOT the threshold value itself. This was a critical fix: the Otsu threshold lands in the middle of the gap between modes (orders of magnitude wide), not at the curved mode's center. Using the curved-mode median recovers the actual tessellation error.
6. eps = clamp(3*delta_est, floor, ceiling). floor = min(10*quant_step, 0.1*ceiling). ceiling = max(1e-3*extent, 4*delta_est) -- the `4*delta_est` term ensures the ceiling never blocks 3*delta_est on small meshes where delta is a significant fraction of extent.

SYNTHETIC VALIDATION (9 curved meshes + 1 planar, 3 delta targets x 3 curved primitives + planar bracket):
- Cylinder (r=1, h=3): trimesh.creation.cylinder at n sections. Analytic delta_true = r*(1-cos(pi/n)).
- Sphere (r=2): trimesh.creation.uv_sphere at count=[n, n]. Analytic delta_true = r*(1-cos(pi/n)).
- Torus (R=2, r=0.8): manual parametric. Analytic delta_true = r_minor*(1-cos(pi/n_minor)).
- Planar bracket: manual box (4x3x1 cm) with grid-subdivided faces. NO curved surfaces, NO jitter. delta_true = 0.

RESULTS TABLE (all ratios in [0.3, 3.0]):

| Mesh             | delta_true | delta_est | ratio | eps     |
|------------------|-----------|-----------|-------|---------|
| cylinder_d0.001  | 0.000979  | 0.001623  | 1.66  | 4.9e-3  |
| sphere_d0.001    | 0.000987  | 0.000474  | 0.48  | 1.4e-3  |
| torus_d0.001     | 0.000994  | 0.002213  | 2.23  | 6.6e-3  |
| cylinder_d0.01   | 0.009314  | 0.015333  | 1.65  | 4.6e-2  |
| sphere_d0.01     | 0.009631  | 0.004749  | 0.49  | 1.4e-2  |
| torus_d0.01      | 0.009849  | 0.021099  | 2.14  | 6.3e-2  |
| cylinder_d0.05   | 0.048943  | 0.077718  | 1.59  | 2.3e-1  |
| sphere_d0.05     | 0.043705  | 0.019863  | 0.45  | 6.0e-2  |
| torus_d0.05      | 0.048246  | 0.096575  | 2.00  | 2.9e-1  |
| planar_bracket   | 0.0       | 1e-15     | -     | 1.0e-6  |

PATTERN: cylinder and torus overestimate delta by ~1.6-2.2x (the 1-ring PCA residual spans 2-3 facets, so the sagitta is larger than a single-facet chordal error). Sphere underestimates by ~0.45-0.49x (the UV sphere has triangles with varying size/curvature; the curved-mode median is pulled down by the many small triangles near the poles). Both biases are CONSISTENT and within the [0.3, 3.0] order-of-magnitude band. The estimator is NOT a precision measurement -- it is a gap detector that correctly separates flat from curved at the right scale.

PLANAR BRACKET (no-curvature stress case): delta_est = 1e-15 (machine zero). eps = 1e-6 (floor). Segmentation: 6 planar, 0 curved, coverage=1.0. The estimator correctly identifies no curved mode and the derived eps stays at the noise floor, preventing false curved classifications.

REAL MESH RESULTS:
- **meshb2 (MeshBody2)**: delta_est=7.6e-4, eps=2.3e-3. Segmentation: **31 planar, 20 curved, all r=0.25**, coverage=0.95. Identical to the prototype's fixed-eps=0.01 result. The derived eps=2.3e-3 sits in the gap between perfect-fit wall residual (0.000475) and wrong-model residual (0.05-0.3). The estimator correctly identified the cylinder walls as the curved mode.
- **cuerpo79 (real CAD tray)**: delta_est=9.0e-3, eps=2.7e-2. Segmentation: 268 planar, 15 curved (all r~2.49), coverage=0.64. The delta is in the expected 1e-3 to 1e-2 range for MeshRefinementHigh. Planar-heavy as expected. The 15 cylinders at r~2.49 are likely filleted features (consistent across all detections).

COARSE delta STRESS CASE (cylinder at delta=0.05): fixed eps=0.01 < delta_true=0.0489 -> REJECTS curved surfaces (false planar). Derived eps=0.233 > delta_true -> ACCEPTS curved. The universal estimator correctly scales eps with the mesh's actual tessellation error, avoiding the fixed-eps failure mode on coarse meshes.

KNOWN MODEL-SET LIMITATION: the joint segmentation uses plane+cylinder only. Sphere and torus curved regions do NOT classify as curved (no sphere/torus fitter) -- they produce planar_count>0 and curved_count=0. This is ORTHOGONAL to the delta estimator (which correctly recovers delta on sphere/torus meshes); it is a limitation of the model competition set, not the tolerance derivation. A production system would need a sphere/cone/general-freeform fitter to classify those surfaces.

FIVE VERDICT CHECKS: ALL PASS.
(a) delta_est tracks delta_true across sphere/torus/cylinder: ratio in [0.3, 3.0] for all 9 meshes.
(b) Planar bracket: curved_count=0, eps at noise floor.
(c) meshb2: 31 planar, 20 cylinders at r=0.25 (matches prototype).
(d) cuerpo79: delta=9e-3 in [1e-4, 1e-1], planar-heavy (268P / 15C).
(e) Coarse delta=0.05: derived eps accepts curved where fixed eps=0.01 rejects.

RECOMMENDATION: the universal delta estimator is READY TO SHIP into the production segmentation pipeline. It replaces the fixed epsilon_accept=0.01 with a derived eps that correctly scales with the mesh's tessellation error, handles planar-only meshes (no false positives), and works across different surface types. The ceiling formula `max(1e-3*extent, 4*delta_est)` ensures eps is never blocked on small meshes. The main caveat: it is an order-of-magnitude estimator (ratio 0.45-2.23), not a precision measurement -- the 1-ring PCA residual overestimates delta by ~2x for cylinder/torus and underestimates by ~2x for sphere, but this is acceptable because eps=3*delta_est still lands in the right gap for all tested meshes.

### Re-verification addendum (2026-08-11)

Re-ran `py -3.13 -u delta_calibration.py` — reproduces identically (12.5s total). JSONL now uses task-specified column names: `shape`, `target_delta_cm`, `true_delta_cm`, `est_delta_cm`, `log10_err`, `planar_count`, `curved_count`, `radius_hist`, `coverage`, `secs`.

LOG10_ERR: all 9 curved synthetics have |log10(est)−log10(true)| ≤ 0.35 (task threshold: 0.5). Cylinder consistently at 0.20–0.22, sphere at 0.31–0.34, torus at 0.30–0.35. Estimator is a reliable order-of-magnitude gap detector.

CUERPO79 TOTAL-vs-40 ANALYSIS: mesh decomposition gives 268 planar + 15 curved = **283 total faces vs BRep ground-truth 40** (gap=243). Root cause: `_merge_planes` only merges **edge-adjacent** coplanar regions — disconnected coplanar faces separated by curved features (fillets, holes) cannot merge even with identical normal/offset. cuerpo79 extent is 215.5×150.3×70.0 cm (likely mm dumped as cm: a 215mm×150mm×70mm tray), 5078 tris, quant_step=0.145cm. The 268 fragments average ~19 tris each, consistent with planar-grower fragmentation at curved-to-planar transitions. The DELTA ESTIMATOR is unaffected (delta=9e-3 is correct for this mesh density); this is a segmentation-merger topology limitation.

CYLINDER d0.01 NOTE: the joint segmentation produced 0 cylinders (25 planar, coverage 0.54) for the 92-tri cylinder at delta=0.01. Root cause: 23-section cylinder has cap fans of 23 tris each (≤30 small_group_max), so caps+walls merge into one cluster — the cap apex vertices at the cylinder center inflate the fit residual, rejecting the cylinder model. At d0.001 (71 sections: caps >30 tris → large groups, walls cluster alone) and d0.05 (10 sections: cluster is small enough for robust fit despite caps) the cylinder IS recovered. The critical stress case (d0.05) passes as required.

FINAL VERDICT UNCHANGED: delta estimator is production-safe. The segmentation gap on cuerpo79 (283 vs 40) is a known limitation of the edge-adjacency-constrained plane merger on large/complex meshes — orthogonal to the delta estimator's correctness.
## [2026-08-11] v1 BASELINE FROZEN (pre-fix snapshot for before/after comparison) (atlas)

Frozen for regression comparison after fixes (a/d/b/e):
- delta_calibration_v1.py   SHA256=AB1F754F268A780A3EF58BFF77A199537338ABDC6DD35806B3EB95F913EE4FE9
- delta_results_v1.jsonl    SHA256=67049BCAF2586FE83ADEF1648A6D1600966FDDA9C9A569513AEDE0CFA95DA04C

v1 SUMMARY (re-verified by atlas re-run 2026-08-11):
- delta estimator: PASS. ratio 0.45-2.23 across delta 0.001-0.05 (cylinder/sphere/torus); planar floor 1e-15, eps 1e-6; meshb2 regression exact (31P/20C r=0.25 cov 0.95, eps 2.3e-3).
- segmentation gaps recorded for v2:
  1) cylinder_d0.01: 25P 0C cov 0.543 (regression; d0.001 and d0.05 both C=1)
  2) sphere (all delta): 0C, 426-8671P, cov 0.08-0.31 (no sphere fitter)
  3) torus (all delta): 0C, 135-5116P, cov 0.32-0.79 (no torus fitter)
  4) cuerpo79: 268P+15C = 283 vs BRep 40; units suspect (extent 215.5 vs meshb2 21.47; radii 2.49 -> likely mm)
NEXT: (a) fix cylinder_d0.01, (d) resolve cuerpo79 units, (b) add sphere/cone/torus fitters, (e) ship estimator+cylinder recovery into mesh_analysis.py, retry full set -> delta_results_v2.jsonl, compare vs this baseline.
## [2026-08-11] v1.5 PROGRESS: fix-a (cylinder d0.01) + unit-d (cuerpo79 mm->cm) VERIFIED (atlas)

(a) cylinder_d0.01 regression FIXED + verified by atlas re-run:
- ROOT CAUSE: n=23 cylinder -> caps (23 tris each, <=small_group_max=30) merge with walls into ONE 92-tri cluster via cap<->wall edge adjacency. Cylinder fit on the full cluster succeeds (confidence 0.5, r=0.9917, axis [0,0,1]) but the 46 cap fan apex vertices sit ON the axis (radial dist 0), inflating mean radial err to 0.172 >> eps=0.046 -> REJECTED. Wall-only refit control: res=0.008 << eps -> would pass perfectly. Coverage 0.5435 puzzle: 2-tri wall quads have degenerate PCA (rank-1 covariance -> arbitrary normal -> res 0.03-0.18); only 2 cap groups + 1 wall group pass the res<=eps plane gate (50/92 tris).
- WHY d0.001/d0.05 unaffected: d0.001 caps = 71 tris > small_group_max=30 -> excluded from clusters; d0.05 caps (10 tris) barely pass full-cluster fit (res 0.195 < eps 0.233).
- FIX: cap-contamination retry in joint_segmentation (delta_calibration.py lines 345-387). On full-fit FAILURE with valid axis: split cluster groups by majority |dot(tri_normal, axis)|<0.15 -> side (wall) vs non-side (cap); if BOTH exist and side-only fit passes (res<=eps), emit side tris as cylinder + non-side groups as planes. ONLY triggers on full-fit failure (d0.05, meshb2 walls untouched).
- RESULT (atlas re-run, identical to agent claim): cylinder_d0.01 P=2 C=1 cov=1.0 r=0.992. All other rows byte-identical to v1. All 5 verdict checks PASS. Code reviewed line-by-line: guards correct, deterministic, estimator formulas untouched.

(d) cuerpo79_data.json UNITS RESOLVED: the dump is in MILLIMETERS; scale to cm = exactly 10.0.
- Evidence (5 lines, documented in unit_d_findings.md): Fusion BRep min matches dump min at exactly 10x (2.882 cm -> 28.82); Z extent 70.0/7.0 = 10.0; Z unique values are exactly 10x meshb2's verified-cm values; cylinder radii 2.49 mm -> 0.249 cm = meshb2's r=0.25; design params authored in mm (d271="5.00 mm" -> r 2.5mm = 0.25cm hole).
- Atlas cross-verification: live fusion360_measure_body("Cuerpo79") = 21.458 x 7.002 x 7.0 cm, 40 faces. Corrected dump extent 21.55 x 15.03 x 7.0, min coords exactly match BRep min at cm scale. Original dump DOES have a normals key (prior note was wrong); corrected dump same schema, indices identical, nodes x0.1 (verified).
- METRICS SHIFT: quant_step 0.14499 -> 0.01450 cm; delta_est 9.01e-3 -> 9.38e-4 cm (now same order as meshb2's 7.61e-4); eps 2.70e-2 -> 2.81e-3 cm; cyl radii ~2.49 -> ~0.25 cm (matches meshb2); segmentation 268P/15C cov 0.636 -> 141P/18C cov 0.678 (rhist {0.25:10, 0.249:7, 0.248:1}).
- CAVEAT: dump is an OLDER snapshot than the live body (Y extent 15.03 cm vs current BRep 7.002 cm; body trimmed in Y after export). The 40-face BRep ground truth is for the CURRENT body; dump-time face count unknown. Use cuerpo79_cm_data.json for segmentation runs, but treat the 40-face target as approximate.
- verify_cuerpo79_cm.py: 6/6 checks PASS (atlas re-run). Deliverables in temp: cuerpo79_cm_data.json, unit_d_findings.md, verify_cuerpo79_cm.py.

NEXT: (b) add sphere/cone/torus fitters + model competition to delta_calibration.py + wire cuerpo79_cm_data.json; (e) ship estimator + cylinder recovery into mesh_analysis.py; v2 retry + compare vs v1.
## [2026-08-11] v2 PROGRESS: fix-b (5-model competition) VERIFIED (atlas)

All rows verified by atlas re-run (identical to agent claims; delta_results.jsonl regenerated with v2 schema incl. per-type counts):
- sphere d0.001/d0.01/d0.05: 0P 1S cov=1.0 (was 8671/1679/426P 0C cov 0.078-0.311)
- torus d0.001/d0.01/d0.05: 0P 1T cov=1.0 (was 5116/634/135P 0C cov 0.316-0.793)
- cylinders unchanged: d0.001 2P1C r=0.999, d0.01 2P1C r=0.992, d0.05 0P1C r=0.957, all cov=1.0
- planar_bracket: 6P 0C cov=1.0 eps=1e-6 (no false positives)
- meshb2: 31P 20C r=0.25 cov=0.9498 -- EXACT v1 match, ZERO regression
- cuerpo79_cm: 66P 20C (18 cyl + 2 sph) r~0.25 cov=0.706 (mm-scaled baseline had been 268P/15C cov 0.64)
- Verdicts: (a),(b),(b2-sphere),(b2-torus),(c),(d),(e) ALL PASS. (d2) still FAR (86 vs 40, gap 46) -- expected; dump is an older snapshot than the 40-face live body.
- New code reviewed: _fit_torus(46), _fit_sphere_local(122), _fit_cone_local(191); residuals sphere/cone/torus(508/523/546); _best_curved_fit(567) with cylinder-preference (conf>=0.5, structural normal-axis test), sphere normal-radial alignment gate (median<0.85 reject), torus min-verts 16, sphere radius sanity (r > 100x extent reject); per-cluster flow (1) full model competition -> (2) cap-retry LAST RESORT -> (3) planes; merges per type (cyl 834, sph 892, cone 940, tor 994). No TODO/FIXME markers. Estimator formulas untouched.
- FLAG: cone fitter NOT exercised by the synthetic suite (no synthetic cones; the probe cone mesh was co-spherically degenerate so sphere won). Cone residual participates in competition but has no synthetic pass -- needs a real-cone validation before trust.
- Bodies of evidence: fix_b_findings.md (187 lines, full design decisions + v1-vs-v2 table).

NEXT: (e) ship delta estimator + cylinder recovery (+cap-retry) into mcp_server/mesh_analysis.py, pytest green; then final v2-vs-v1 comparison documented.
## [2026-08-12] v2 FINAL: all fixes verified + production shipped + retry PASSES (atlas)

FROZEN v2 ARTIFACTS: delta_calibration_v2.py, delta_results_v2.jsonl (SHA of results in git-less temp; v1 copies untouched).

RETRY OF THE WHOLE SET (atlas final run, delta_calibration_v2.py): 10 synthetic + 2 real meshes, all verdicts PASS except (d2)=FAR (expected snapshot caveat).

v1 (baseline) vs v2 (final) comparison:
| case | v1 P/C cov | v2 P/C cov |
| cylinder_d0.001 | 2/1 cov1.0 r=0.999 | SAME |
| sphere_d0.001 | 8671/0 cov0.078 | 0/1 cov1.0 (sphere) |
| torus_d0.001 | 5116/0 cov0.317 | 0/1 cov1.0 (torus) |
| cylinder_d0.01 | 25/0 cov0.543 | 2/1 cov1.0 r=0.992 |
| sphere_d0.01 | 1679/0 cov0.311 | 0/1 cov1.0 |
| torus_d0.01 | 634/0 cov0.316 | 0/1 cov1.0 |
| cylinder_d0.05 | 0/1 cov1.0 r=0.957 | SAME |
| sphere_d0.05 | 426/0 cov0.290 | 0/1 cov1.0 |
| torus_d0.05 | 135/0 cov0.793 | 0/1 cov1.0 |
| planar_bracket | 6/0 cov1.0 eps=1e-6 | SAME (no false positives) |
| meshb2 | 31/20 cov0.950 r=0.25 | SAME (exact, no regression) |
| cuerpo79 | 268/15 cov0.636 (mm-as-cm) | 66/20 cov0.706 r~0.25 (cm-corrected) |

PRODUCTION SHIP (fix-e, mcp_server/mesh_analysis.py): _otsu_threshold(1028), _estimate_delta(1065), _cylinder_fit_residual(942), _sphere_fit_residual(956), _cone_fit_residual(964), _cylinder_residual_on_tris(1201), _cluster_tri_area(1190), _recover_cylinders_from_small_groups(1230); _classify_curved_patch(980) gained epsilon gate (None=backward compat); _extract_curved_patches passes epsilon; decompose_mesh_faces(2495) wires estimate->recover->exclude. Reuses _fit_cylinder/_ToleranceConfig/_detect_quantization_step. Pure numpy, no deps. Output schema unchanged (additive residual field only).
- Tests: 9 new in tests/test_cylinder_recovery.py; FULL SUITE 262 passed, 1 xfailed (atlas re-run, 51.5s) -- zero regressions.
- HANDS-ON PRODUCTION QA on real MeshBody2 dump: 32 planar + 20 cylinders ALL r=0.2498 cm (was 992 planar / 0 cylinders pre-fix). 127 freeform remain = the SAME pre-existing area-0.0 degenerate slivers as baseline (127/127, median area 0.0) -- preserved per spec (freeform behavior unchanged); the prototype dropped unclassified tris (coverage 0.9498), production's schema requires the freeform fallback (documented design difference, not a regression).
- Scope: only mesh_analysis.py + tests/test_cylinder_recovery.py touched by fix-e (other repo diffs pre-date this session).

REMAINING CAVEATS (documented follow-ups):
1. Cone fitter has NO synthetic validation (no synthetic cones; probe cone was co-spherically degenerate) -- needs a real-cone test before trust.
2. Production port includes cylinder recovery + estimator only; sphere/cone/torus model competition stays prototype-only (fix-b validated, ready to port).
3. cuerpo79 (d2) FAR: 86 vs 40 faces -- the dump is an OLDER snapshot than the live 40-face body (Y trimmed after export); treat 40 as approximate.
4. Cuerpo79 ground truth comparison would need a FRESH dump of the current body.
5. Uncommitted repo changes: earlier-boulder work (fusion_server.py, jobs.py, etc.) + this fix-e. Commit decision left to user.


## [2026-08-12] cone validation: synthetic cone cases + fitter bug fixes (atlas)

TASK: add real-taper synthetic cone cases to delta_calibration.py and prove the cone fitter + model competition classify them as cone.

SYNTHETIC CONE DESIGN:
- Frustum r1=1.0, r2=0.3, h=3.0 -> half-angle=atan(0.7/3)=13.13 deg. Steeper than the task's e.g. r2=0.5 (half-angle 9.46 deg) because the cone fitter median-angle gate rejects >80 deg (half-angle <10 deg). r2=0.5 gives median_angle 80.54 >80 -> REJECTED.
- Open surface (no caps): avoids cap-fan apex contamination. Cone wall normals have |dot(axis)|=sin(13.13)=0.227 > cap-retry threshold 0.15, so cap-retry cannot separate walls from caps. The open frustum sidesteps this entirely.
- 4 height rings (not 2): breaks co-spherical degeneracy. Any frustum with 2 parallel rings is co-spherical (2 circles always lie on one sphere). With >=3 rings the vertex set is NOT co-spherical (cone-sphere intersection <=2 circles), forcing sphere residual >>0 while cone residual stays ~0 -> cone unambiguously wins.
- delta_true = r_mean*(1-cos(pi/n)), r_mean=(r1+r2)/2=0.65. Section counts: n=57/18/8 for d=0.001/0.01/0.05.

TWO BUGS FOUND AND FIXED (prototype delta_calibration.py only, production untouched):
1. _fit_cone_local APEX SIGN BUG (CRITICAL): the apex formula t_vals=h0-rho0/tan_ha is only correct for cones with apex below (radius increasing). For apex above (radius decreasing, the common case), the correct sign is h0+rho0/tan_ha. The PCA eigenvector direction is arbitrary, so for n=57/n=18 the axis came out +Z (wrong sign -> non-constant t_vals -> near-zero confidence -> None), while n=8 happened to get -Z (right sign by luck -> fit succeeded). FIX: try both signs, pick lower variance (matching production mesh_analysis.py lines 903-907 which already does this).
2. _best_curved_fit CYLINDER PREFERENCE OVERRIDING CONE: cyl_conf>=0.5 always returns cylinder, even when cone has 13x lower residual. For cone_d0.05, cyl confidence was exactly 0.5 on the cone surface. FIX: added cone preference BEFORE cylinder preference — a high-confidence cone fit (>=0.5) with valid half-angle (>=10 deg, guaranteed by the median-angle gate at 80 deg) is structurally a cone. Real cylinders have half-angle=0 (median_angle=90>80 -> cone rejected), spheres/tori have cv>0.3 -> cone rejected, so no existing case is affected.

RESULTS (all 3 cone cases PASS verdict b3-cone):
| Case | n | d_true | d_est | ratio | cone | cov | ha_fit | ha_true | ha_err% |
| cone_d0.001 | 57 | 0.000987 | 0.001916 | 1.94 | 1 | 1.0 | 13.1 | 13.134 | 0.3% |
| cone_d0.01 | 18 | 0.009875 | 0.018853 | 1.91 | 1 | 1.0 | 12.9 | 13.134 | 1.8% |
| cone_d0.05 | 8 | 0.049478 | 0.071851 | 1.45 | 1 | 1.0 | 12.2 | 13.134 | 7.1% |
All ratios in [0.3,3.0], all half-angle errors <10%, all coverage=1.0, all cone_count=1.

REGRESSION: all 12 original rows byte-identical to delta_results_v2.jsonl (only secs timing field varies). The cone preference in _best_curved_fit does not affect any existing case because cone fit returns None for cylinders (median_angle=90>80), spheres (cv>0.3), tori (cv>0.3).

DELIVERABLES: cone_findings.md (full design decisions + results), delta_calibration.py (3 cone cases + b3 verdict + 2 bug fixes), delta_results.jsonl (15 rows: 12 original + 3 cone).

TRUST FOR PRODUCTION PORT: the cone fitter (_fit_cone_local with sign fix) and the cone preference in model competition (_best_curved_fit) are validated. The production _fit_cone already has the sign fix (lines 903-907). The production _classify_curved_patch tries cyl->sph->cone sequentially (no competition); the port task should add the 5-model competition with cone preference as validated here.

## [2026-08-12] 5-model competition production port (atlas)

TASK: port the validated 5-model competition (cylinder/sphere/cone/torus) from delta_calibration_v3.py into production mcp_server/mesh_analysis.py so _classify_curved_patch runs the full model competition instead of sequential cylinder->sphere->cone->freeform.

CHANGES (mcp_server/mesh_analysis.py only):
1. _fit_sphere: added two validated gates (additive rejection criteria, no change to the ray-intersection fitting method):
   - Radius sanity: reject if radius > 100x patch extent (catches near-planar overfit).
   - Normal-radial alignment: reject if median |cos(normal, radial)| < 0.85 (catches cylindrical patches being misclassified as spheres; on a real sphere normals point radially, on a cylinder they point perpendicular to the axis).  Also scales confidence by the alignment factor.
2. _fit_torus: NEW function ported verbatim from prototype (vertex covariance for axis, linearised torus constraint for R/r, min 16 verts, confidence from CV of geometric residual).  Returns major_radius_cm, minor_radius_cm, axis, center_cm, confidence.
3. _torus_fit_residual: NEW — mean tube-distance residual on (verts, faces, fit).
4. _classify_curved_patch: REPLACED with full 5-model competition when epsilon is provided:
   - Tries cylinder, sphere, cone, torus independently.
   - Collects candidates with residual <= epsilon.
   - Preference: cone (conf>=0.5) FIRST, then cylinder (conf>=0.5), then lowest residual.
   - Winning dict gets additive residual_cm field.
   - epsilon=None path: preserved as sequential first-fit-wins (backward compat, no competition).
5. Cone fitter already had the both-signs apex fix (verified lines ~903-907 in pre-edit source).

KEY DESIGN DECISION: epsilon=None backward compat is implemented as a separate sequential path (old behavior: first non-None fit wins). The competition only runs when epsilon is provided.  In the production flow, decompose_mesh_faces always passes epsilon=derived_eps from _estimate_delta, so the competition always activates.  Direct callers without epsilon get the old behavior.

ARCHITECTURE NOTE: the production decompose_mesh_faces routes curved surfaces through _recover_cylinders_from_small_groups (cylinders only, untouched per spec) and _extract_curved_patches (triangles NOT in any planar group -> _classify_curved_patch).  On synthetic sphere/cone/torus meshes, the planar grouping at 0.5deg absorbs ALL triangles into small planar groups, leaving none for _extract_curved_patches.  This is the same architecture that produces 127 freeform slivers on meshb2.  The competition now correctly classifies any curved triangles that DO reach _extract_curved_patches (e.g. meshb2 slivers, non-planar fragments).  Testing the competition directly via _classify_curved_patch(patch, epsilon=X) is the validated approach — it tests the exact ported logic on synthetic sphere/cone/torus trimesh patches.

TESTS: 14 new tests in tests/test_curved_competition.py:
- Sphere: classified as sphere, correct radius ~2.0, not cylinder.
- Torus: classified as torus, correct R~2.0 r~0.8.
- Cone: classified as cone, correct half-angle ~13.13deg, not sphere.
- Cylinder preference: tall cylinder (h>>r) classifies as cylinder not sphere (normal-radial gate rejects sphere).
- Freeform fallback: random mesh -> freeform.
- residual_cm additive field present on winning model.
- epsilon=None: sequential first-fit-wins still works.
- Cylinder end-to-end regression via decompose_mesh_faces (23-section cylinder -> 1 cylinder).
- Determinism: repeated classification identical.

FULL SUITE: 276 passed, 1 xfailed (57.75s). Was 262 passed + 1 xfailed before this port (14 new tests). ZERO regressions.

SCOPE: only mcp_server/mesh_analysis.py + tests/test_curved_competition.py. No other files touched. No new dependencies (pure numpy). No TODO/FIXME markers, no dead code, no print debugging.


## [2026-08-12] production pipeline port: curved recovery routes sphere/cone/torus through competition (atlas)

TASK: wire the validated 5-model competition into the production pipeline so whole-mesh sphere/cone/torus surfaces are recovered via `decompose_mesh_faces`, not just isolated patches.

ROOT CAUSE (why the 14 isolated tests passed but the pipeline didn't): the planar grouping at 0.5° absorbs ALL triangles of a sphere/torus/cone into small planar groups (2-4 tris each). `_recover_cylinders_from_small_groups` then clustered these small groups and ran ONLY a cylinder fit — which fails for sphere/torus/cone surfaces. The clusters never reached `_classify_curved_patch`, so the competition was bypassed.

CHANGES (mcp_server/mesh_analysis.py):
1. Renamed `_recover_cylinders_from_small_groups` → `_recover_curved_from_small_groups` (call site at ~2838 updated).
2. Inserted step (1) Full model competition BEFORE the existing cylinder fit (now step 2) and cap-retry (now step 3): for each cluster with >= 6 tris, build a COMPACT local trimesh (np.unique remap, same as `_cylinder_residual_on_tris`), call `_classify_curved_patch(part, epsilon=epsilon)`. If surface_type != "freeform", emit entry and continue. If freeform (or competition skipped), fall through to cylinder fit + cap-retry.
3. Added sharp-edge gate (step 0): compute mean |dot(adjacent normals)| for interior edges in the cluster. If < 0.707 (≈ avg dihedral > 45°), skip the competition — the cluster has sharp edges (box, capped cylinder) and is not a smooth curved surface. This prevents the sphere fit false positive on boxes (box vertices are cospherical → sphere residual 0, CV 0, alignment 1.0 → perfect sphere fit that is WRONG).

CRITICAL GOTCHA — box cospherical degeneracy: a rectangular box's 8 corner vertices are equidistant from the box center (circumscribed sphere). The sphere fit produces center=box center, radius=circumscribed radius, residual=0, CV=0 (confidence 1.0), normal-radial alignment=1.0 (face normals point radially from center). ALL sphere gates pass. Without the sharp-edge gate, `decompose_mesh_faces` on a box returns 1 sphere patch instead of 0 curved patches. The sharp-edge gate catches this because box face-adjacency edges have 90° dihedral → mean |dot| = 0.333 < 0.707 → competition skipped.

CRITICAL GOTCHA — compact vertex array: the competition functions (`_fit_cylinder`, `_fit_sphere`, etc.) compute `axis_point = verts.mean(axis=0)` — the centroid of ALL vertices passed. If the trimesh is built with `process=False` on the full `v_arr`, the centroid is the GLOBAL mesh centroid, producing wrong fit parameters. MUST compact: `np.unique(f_sub.ravel(), return_inverse=True)` → local vertex array with only the cluster's vertices. Same pattern as `_cylinder_residual_on_tris` (line ~1382).

CRITICAL GOTCHA — capped cylinder competition: a 23-section cylinder with caps (92 tris, 1 cluster) has wall-cap edges at 90° → mean |dot| ≈ 0.66 < 0.707 → competition skipped. The cap-retry (step 3) handles this correctly (splits wall/cap, refits wall-only). The sphere fit on a capped cylinder also produces a false positive (ring vertices at constant distance from center, cap apex at different distance → CV low enough to pass), so skipping the competition on sharp-edged clusters is essential.

GATE THRESHOLD JUSTIFICATION (0.707 = cos 45°):
- Box (4×3×1): mean |dot| = 0.333 → skip ✓
- 23-section cyl with caps: mean |dot| = 0.66 → skip ✓ (cap-retry handles)
- Sphere (12×16): mean |dot| = 0.966 → run ✓
- Torus (24×16): mean |dot| = 0.951 → run ✓
- Cone (18×4): mean |dot| = 0.966 → run ✓
- meshb2 walls: mean |dot| ≈ 0.99 → run ✓

TESTS: 6 new in tests/test_curved_recovery_pipeline.py:
- sphere via decompose_mesh_faces → surface_type "sphere" ✓
- torus via decompose_mesh_faces → surface_type "torus" ✓
- cone via decompose_mesh_faces → surface_type "cone" ✓
- flat bracket → 0 curved patches (no false sphere) ✓
- determinism (sphere ×2, torus ×2) ✓

FULL SUITE: 282 passed, 1 xfailed (53.81s). Was 276 passed + 1 xfailed (6 new tests). ZERO regressions. meshb2 and planar_bracket regression tests unchanged. Function pure LOC: 198 (under 250 ceiling).

PROBE RESULTS (matching task spec):
- synthetic_sphere: classified as "sphere" ✓ (was: 192 planar, 32 freeform)
- synthetic_torus: classified as "torus" ✓ (was: 384 planar, 0 curved)
- synthetic_cone: classified as "cone" ✓ (was: 18 planar, 0 curved)
- meshb2 regression: 32 planar + 20 cylinder + 127 freeform (UNCHANGED)
- planar_bracket: 6 planar, 0 curved (UNCHANGED)

SCOPE: mcp_server/mesh_analysis.py + tests/test_curved_recovery_pipeline.py. No other files touched.


## [2026-08-12] FRESH cuerpo79 dump: display mesh vs STL export density (atlas)

TASK: produce a FRESH mesh dump of the current Cuerpo79 BRep body (40 faces) and re-run the delta-calibration harness to test the (d2) verdict gap.

FUSION STATE: Cuerpo79 was OPEN as a BRep body (document "Sin título"), 40 faces, 110 edges, extent 21.458 x 7.002 x 7.0 cm. Parameters authored in mm (d271 = 0.5 mm, etc.).

DUMP EXTRACTION — TWO APPROACHES ATTEMPTED:

**Approach 1 (succeeded): meshManager.displayMeshes** — the rendering display mesh. `body.meshManager.displayMeshes` returns a collection of `TriangleMesh` objects with `nodeCoordinates` as a list of `Point3D` objects (NOT flat floats — must extract `.x`, `.y`, `.z`). Only 1 mesh in the collection. Result: **220 vertices, 140 triangles**, extent 21.458 x 7.002 x 7.0 cm (exact match to BRep bbox). This is the DEFAULT rendering tessellation — extremely coarse (~3.5 tris per BRep face).

**Approach 2 (CRASHED FUSION): MeshCalculator** — attempted `meshMgr.createMeshCalculator(); meshCalc.surfaceTolerance = 0.001; meshCalc.calculate()`. This caused a FATAL Fusion 360 crash — bridge unreachable for 35+ minutes, likely requires manual restart. LESSON: do NOT use MeshCalculator with tight tolerances on complex BRep bodies via execute_script; it crashes Fusion. Use `export_as_stl` MCP tool (stable exporter code path) or import the STL as a mesh body instead.

FRESH DUMP STATS (approach 1, 140-tri display mesh):
- Vertices: 220, Triangles: 140 (welded: 140), Normals: 220 vecs
- Extent: (21.4580, 7.0020, 7.0000) cm — matches current BRep EXACTLY
- quant_step: 2.000e-03 cm (vs old dump 1.450e-02 cm — finer quantization)
- delta_est: 3.003e-04 cm (vs old dump 9.38e-04 cm — same order as meshb2's 7.6e-4)
- eps: 2.146e-03 cm
- n_residuals: 49, n_curved: 6
- OLD DUMP: 5085 vertices, 5078 tris, extent 21.55 x 15.03 x 7.0 cm — DIFFERENT geometry (Y trimmed from ~15 to ~7 cm between dump and current body)

DIFFERENCES FROM OLD DUMP: counts are VERY DIFFERENT (140 vs 5078 tris; 220 vs 5085 verts) AND geometry is different (Y extent 7.0 vs 15.03 cm). The old dump IS confirmed stale — it was from the pre-Y-trim body. The fresh dump is from the current 40-face body.

HARNESS RESULTS (full run, delta_calibration.py v3 frozen, 12.4s):
- All synthetic cases (12) + planar bracket: PASS (identical to prior runs)
- meshb2: PASS — 31P, 20C (all r=0.25), cov=0.9498. Harness integrity confirmed.
- **(d) cuerpo79: PASS** — delta_est=3.00e-4 in [1e-4, 1e-1], P=36, C=0, curved<=planar ✓
- **(d2) cuerpo79: CLOSE** — P=36, C=0, P+C=36 vs BRep=40, **gap=4** (<=20)
- (e) coarse delta: PASS

CRITICAL CAVEAT — DEGENERATE CLOSE: the (d2) CLOSE verdict is **not meaningful** because the 140-tri display mesh is too coarse to resolve ANY curved features. 0 curved faces means the cylindrical holes, fillets, and rounds in the BRep are invisible at this tessellation density. The gap=4 (36 vs 40) is coincidental — the 36 planar groups happen to be close to 40 because each BRep face maps to ~3-4 triangles, each forming a single planar group. A finer mesh (~5000 tris from an STL export) would likely detect the curved features and produce a different (possibly still FAR) gap.

GOTCHAS:
1. `meshManager.displayMeshes` returns `Point3D` objects, not flat floats — must extract `.x/.y/.z` explicitly.
2. `MeshManager.displayMesh` does NOT exist (singular) — the correct property is `displayMeshes` (plural).
3. `MeshCalculator.calculate()` with tight tolerance CRASHES Fusion 360 — use `export_as_stl` instead.
4. The old `cuerpo79_cm_data.json` was overwritten with the 140-tri dump; old `cuerpo79_data.json` (mm) is intact for recovery.

NEXT STEP: when Fusion is restarted, export the body as STL via `export_as_stl` MCP tool, parse the STL to get a ~5000-tri mesh, write as cm JSON, and re-run the harness for a non-degenerate (d2) verdict.


## [2026-08-12] FINE cuerpo79 dump via 3MF re-import: (d2) CLOSE, gap=11 (atlas)

TASK: re-import the "Plataforma tortus 4.3mf" (user request after MeshCalculator crash lost the doc), extract the fine mesh (~5000 tris) as cuerpo79, and re-run the harness for a NON-DEGENERATE (d2) verdict.

3MF IMPORT: `fusion360_import_mesh_file` with `path=Plataforma tortus 4.3mf`, `units="mm"`. Imported 2 mesh bodies: MeshBody1 (1842 nodes, 1780 tris) and MeshBody2 (4882 nodes, 4938 tris). Both extent 214.6×70×70 mm → 21.467×7.0×7.0 cm.

BODY IDENTIFICATION: MeshBody2 (4938 tris) matches object_3 (4939 tris from 3MF peek) and the old cuerpo79 dump's ~5078-tri density. Used as the cuerpo79 source per task direction.

DUMP EXTRACTION (STABLE PATH): extracted MeshBody2's `displayMesh` directly via `execute_script` — same technique as prior session but on a MESH body (not BRep). For imported mesh bodies, the displayMesh IS the source mesh data (no separate tessellation). `nodeCoordinates` = list of `Point3D` objects (extract `.x/.y/.z`); `nodeIndices` = flat int list; `normalVectors` = list of `Vector3D`. All coordinates already in cm (Fusion internal units). Wrote JSON directly from Fusion Python (full FS access). No MeshCalculator, no STL export needed.

FRESH DUMP STATS (4938-tri fine mesh):
- Vertices: 4882, Triangles: 4938 (welded: 4938), Normals: 4882 vecs
- Extent: (21.4667, 7.0000, 7.0000) cm
- quant_step: 2.100e-03 cm, delta_est: 7.608e-04 cm, eps: 2.282e-03 cm
- n_residuals: 4641, n_curved: 1997
- OLD DEGENERATE DUMP: 220 verts / 140 tris (display mesh of BRep body) — completely different
- OLD MM DUMP: 5085 verts / 5078 tris, extent 21.55×15.03×7.0 — different geometry (pre-Y-trim)

CRITICAL FINDING — IDENTICAL TO MESHB2: the fresh cuerpo79 dump produces **byte-identical metrics** to meshb2_data.json: same delta_est (7.6075e-04), same eps (2.2823e-03), same segmentation (P=31, C=20, rhist={0.25: 20}), same extent (21.467), same quant_step (2.10e-03), same coverage (0.9498). This means **MeshBody2 in the 3MF IS the meshb2 reference mesh** — they are the same geometry. The 3MF "Plataforma tortus 4" contains the original design's two mesh bodies (MeshBody1 + MeshBody2), and object_3/MeshBody2 is the same mesh that was already analyzed as the meshb2 harness case.

HARNESS RESULTS (full run, delta_calibration.py v3 frozen, 10.8s):
- All 12 synthetic cases + planar bracket: PASS (identical to all prior runs)
- meshb2: PASS — 31P, 20C (all r=0.25), cov=0.9498. Harness integrity confirmed.
- **(d) cuerpo79: PASS** — delta_est=7.6075e-04 in [1e-4, 1e-1], P=31, C=20, curved<=planar ✓
- **(d2) cuerpo79: CLOSE** — P=31, C=20, P+C=51 vs BRep=40, **gap=11** (<=20)
- (e) coarse delta: PASS

VERDICT INTERPRETATION: gap=11 (51 vs 40) is a GENUINE CLOSE — not degenerate (unlike the prior 140-tri run). The fine mesh resolves 20 curved features (cylinders r=0.25) and 31 planar regions. The 11-face overcount vs the 40-face BRep truth is explained by: (a) the plane merger fragments coplanar-but-disconnected regions (~4 extra planar faces from topology separation), and (b) the 40-face BRep truth was for a DIFFERENT body (Cuerpo79 BRep, Y-trimmed) that is NOT the same geometry as this mesh.

IMPORTANT — BODY IDENTITY MISMATCH: the task directed using object_3 (4939 tris) as cuerpo79. However, object_3 = MeshBody2 = meshb2 (the harness reference case). The actual Cuerpo79 BRep body (40 faces, min Y=4.738) matches MeshBody1's position (min Y=4.738, 1780 tris), NOT MeshBody2 (min Y=12.769). The 3MF contains two parts of a "Plataforma tortus" platform at different Y positions. To get a genuine cuerpo79-vs-40-faces comparison, one would need to either: (a) reconstruct the 40-face BRep and dump its fine mesh, or (b) use MeshBody1 (the body at Y=4.738 matching the BRep position) as the cuerpo79 source.

GOTCHAS:
1. For imported mesh bodies, `displayMesh` IS the source mesh — no MeshCalculator or STL export needed. This is the STABLE extraction path.
2. `import_mesh_file` with `.3mf` works perfectly — 2 mesh bodies imported, correct units conversion (mm→cm).
3. MeshBody naming in Fusion is sequential (MeshBody1, MeshBody2) — may not correspond to 3MF object IDs.
4. Identical segmentation results across two "different" dump files is a red flag — always cross-check vertex coordinates / min-max positions, not just tri counts.


## [2026-08-12] CORRECTED cuerpo79 dump: MeshBody1 = object_2 = real cuerpo79 (atlas)

TASK: correct the body identity error from the prior run. MeshBody2/object_3 = meshb2 (4882v/4938t/minY 12.769). The REAL cuerpo79 = MeshBody1/object_2 (1842v/1780t/minY 4.738 — positionally matches the 40-face BRep). Extract MeshBody1, overwrite cuerpo79_cm_data.json, re-run harness.

BODY IDENTIFICATION CORRECTION:
- object_2 / MeshBody1 = cuerpo79: 1842 nodes, 1780 tris, min Y=4.738, extent (21.457, 7.0, 7.0) cm. Positionally matches the 40-face Cuerpo79 BRep (min Y=4.738, extent 21.458×7.002×7.0).
- object_3 / MeshBody2 = meshb2: 4882 nodes, 4938 tris, min Y=12.769. Identical to meshb2_data.json (the harness reference case).

DUMP EXTRACTION: same stable path as prior run — `execute_script` on `root.meshBodies` to find the body with <3000 nodes (= MeshBody1), extract `displayMesh` (Point3D coords, int indices, Vector3D normals), write JSON in cm directly from Fusion Python.

FRESH DUMP STATS (1780-tri MeshBody1 = real cuerpo79):
- Vertices: 1842, Triangles: 1780 (welded: 1780), Normals: 1842 vecs
- Extent: (21.4567, 7.0000, 7.0000) cm, min Y = 4.738
- quant_step: 1.830e-04 cm (much finer than meshb2's 2.10e-03 — this mesh has very fine coordinate resolution)
- delta_est: 1.505e-07 cm (MACHINE ZERO — all residuals at noise floor)
- eps: 1.830e-03 cm (floor = 10×quant_step, since 3×delta_est is below floor)
- n_residuals: 1431, n_curved: 240 (Otsu found a split, but both modes are near machine-zero)
- NOT meshb2: 1842v/1780t/minY 4.738 vs meshb2's 4882v/4938t/minY 12.769. Identity CONFIRMED different.

WHY delta_est IS MACHINE ZERO: the MeshBody1 mesh from the 3MF is a COARSE TESSELLATION of a part that has NO curved features resolvable at this density. The 1780 triangles approximate the BRep's 40 faces with ~44 tris/face — enough to represent planar regions but not enough to resolve any cylinders/fillets/rounds. All 1-ring PCA residuals are at machine-zero because the mesh is essentially piecewise-planar (each triangle's neighborhood is coplanar within rounding). The Otsu threshold splits noise from noise; n_curved=240 but those "curved" residuals are ~1e-7, not real curvature.

HARNESS RESULTS (full run, delta_calibration.py v3 frozen, 9.9s):
- All 12 synthetic cases + planar bracket: PASS (identical to all prior runs)
- meshb2: PASS — 31P, 20C (all r=0.25), cov=0.9498. Harness integrity confirmed.
- **(d) cuerpo79: FAIL** — delta_est=1.5046e-07 NOT in [1e-4, 1e-1] (it is at machine zero). P=36, C=0, cov=0.8472.
  ```
  (d) cuerpo79: delta in [1e-4, 1e-1], reasonable counts:
      delta_est=1.5046e-07  eps=1.8300e-03  P=36 C=0
      rhist={}  cov=0.8472
      => FAIL (delta in range: False, curved<=planar: True)
  ```
- **(d2) cuerpo79: CLOSE** — P=36, C=0, P+C=36 vs BRep=40, gap=4 (<=20).
  ```
  (d2) cuerpo79: total faces vs BRep ground-truth 40:
      planar(36) + curved(0) = 36  vs BRep=40  gap=4
      => CLOSE (gap <=20)
  ```
- (e) coarse delta: PASS

VERDICT INTERPRETATION: the (d2) CLOSE (gap=4) is technically correct but SEMANTICALLY EMPTY. The mesh has 0 curved features (C=0) — the 1780-tri tessellation is too coarse to resolve any cylinders or fillets. The 36 planar groups approximate the 40 BRep faces because each face maps to roughly 1 planar group (the segmentation finds 36 of the 40 faces as planar regions; the missing 4 are likely curved faces that have no curved detection). The (d) FAIL is expected and HONEST: delta_est at machine zero means the mesh cannot inform the delta estimator about the BRep's actual tessellation error.

ROOT CAUSE — COARSE MESH: this MeshBody1 (1780 tris, from the 3MF) is a SLICER-GRADE export, not a fine CAD tessellation. The 3MF was produced for 3D printing (simplified mesh), not for face-decomposition analysis. Compare: meshb2 (MeshBody2, 4938 tris) has 2.8× more triangles for a similar-extent part and correctly resolves 20 cylinders. The cuerpo79 part needs a finer mesh (ideally the original BRep's MeshCalculator output or an STL export at high refinement) to produce a meaningful (d) verdict.

GOTCHAS:
1. 3MF mesh bodies are SLICER-GRADE (coarse) — NOT suitable for face decomposition. They are optimized for 3D printing, not CAD analysis.
2. delta_est at machine zero (1e-7) means the mesh is piecewise-planar — no curvature information whatsoever. The estimator correctly reports "no curved mode" but the (d) verdict FAILS because the range check [1e-4, 1e-1] assumes a real mesh with actual curvature.
3. The (d2) gap=4 CLOSE is coincidental: 36 planar groups happen to be near the 40 BRep faces, but C=0 means no curved validation at all.
4. To get a REAL cuerpo79 (d2) verdict, need: (a) re-open the 40-face BRep body, (b) export as STL at high refinement (not MeshCalculator — crashes Fusion), or (c) find a finer-tessellated mesh source.


## [2026-08-19] port divergence note: production _fit_cone confidence lacks prototype's t_std/h_range term (sisyphus)

FLAGGED-BUT-NEVER-APPENDED note from the 2026-08-12 competition port (the final compaction summary listed it as "flagged for notepad" — appending now to close the loop):

- Prototype (`delta_calibration_v3.py`, `_fit_cone_local` L260-264): `confidence = max(0.0, 1.0 - cv*3.0 - t_std/h_range)` — the extra `t_std/h_range` term penalizes fits whose per-vertex apex distances (`t_vals`) don't cluster relative to the fitted height range (`h_range = max(h0) - min(h0)`, guarded for h_range < 1e-9).
- Production (`mcp_server/mesh_analysis.py`, `_fit_cone` L914): `confidence = max(0.0, 1.0 - cv*3.0)` — NO t_std/h_range term.

EFFECT: production cone confidence is >= prototype confidence for the same fit (slightly more permissive). Accepted at port time because:
1. All 3 validated synthetic cone cases (half-angle 13.134° frustums, d0.001/0.01/0.05) classify identically under both formulas — conf=1.0 either way (cv ≈ 0 dominates on clean frustums).
2. The competition ordering (cone preference at conf >= 0.5, then cylinder at conf >= 0.5, then lowest residual) is unaffected on the validated corpus; only borderline fits near the 0.5 threshold could flip cone-vs-fallthrough.

RISK: an unvalidated edge case — a poor cone fit with low cv but scattered apex distances would score higher in production than the prototype intended, potentially winning the cone-preference slot where the prototype would have demoted it below 0.5. If such a misclassification shows up, the fix is one line at mesh_analysis.py L914 (port the `t_std/h_range` term; compute `t_std = np.std(t_vals)` and `h_range = max(h0) - min(h0)` with the < 1e-9 guard from the prototype).

All other competition math (residual functions, gates 5-80° half-angle + cv <= 0.3, both-signs apex fix, sphere/torus fitters) was verified equivalent between prototype and production at port time.


## [2026-08-19] cuerpo79 CLOSED: project recovered, full parametric reconstruction, and the definitive (d) root cause (sisyphus)

TASK: recover the "Plataforma tortus" project (import `C:\Users\danie\Documents\3D Prints\Plataforma tortus\Plataforma tortus 4.3mf`, units="mm"), then run the remaining work: the BRep-based cuerpo79 (d) confirmation.

PROJECT RECOVERY:
- Original 40-face BRep document is UNRECOVERABLE: not open in Fusion, no tortus-named file in any of the 4 cloud projects (Assets/Default/Housing_new/My First Project), no f3d/step on disk. Other STLs in the folder (grande 4176t, prototipo 6552t, tortus-3 12808t) are all DIFFERENT variants (no 214.6x70x70mm footprint match) — no finer cuerpo79 tessellation exists anywhere.
- 3mf re-import via import_mesh_file: MeshBody1 (1842v/1780t, min Y 4.738) = cuerpo79 + MeshBody2 (4882v/4938t, min Y 12.769) = meshb2, byte-consistent with the Aug-12 session.

RECONSTRUCTION (full-circle mesh->parametric, saved as "Plataforma tortus recon v1"):
- Structure derived from authoritative slices (mesh_slicer.slice_mesh_at locally on the dump, 6 heights) + decompose face map + targeted triangle probes. 5-layer prismatic model:
  1. Slab Z[0,0.5]: rect X[2.882,20.839]xY[4.738,11.738] minus front-right open channel X[16.339,20.339]xY[4.738,8.738] (full-height notch, NOT a floor pocket — the Z=0.25 outer loop traces the detour) minus crescent slot (flat edge X=2.882 Y[5.843,10.633], straight right edge X=3.806, curved top/bottom, area 3.93 — the "tortus" cutout; f25 flat wall 2.394=4.79x0.5 proves it is an internal slot, not a left-edge bite).
  2. Wall ring Z[0.5,3.5]: front band X[3.806,16.339]xY[4.738,5.238] (ends at divider rib), back band X[3.806,20.839]xY[11.238,11.738], right column X[20.339,20.839] full depth, rib X[15.917,16.339]xY[5.238,8.738], bar X[15.917,20.339]xY[8.738,9.138]. LEFT side open (no wall) except band end caps.
  3. Inner posts Z[3.5,6.5]: X[20.339,20.839] front+back.
  4. Outer posts Z[5,6.5]: X[23.839,24.339] front+back (free-standing until caps).
  5. Cap strips Z[6.5,7]: X[20.339,24.339] front+back (bridge/handle over the arm).
- Built with native MCP features: 2 rect extrudes + 1 cut (notch) + line+SketchFittedSpline sketch via execute_script -> cut (crescent, 15 spline pts from the slice loop) + 5 rect extrudes (ring) + 2+2 posts + 2 caps, all join.
- RESULT: 34 faces, volume 120.79 cm3 (mesh 121.66, ratio 1.0073), bbox deviation 6.7e-5 cm. compare_mesh_to_brep: mean sampled dev 0.26 cm (vertex_fallback method — low-fidelity sampler, volume/bbox are the authoritative gates).

THE DEFINITIVE (d) ROOT CAUSE (two runs):
- Unfilleted recon, VeryHigh-quality STL (2134 tris after Y-filtering the mixed export): delta_est=4.88e-08 MACHINE ZERO, P=72 C=0, cov=0.58 => (d) FAIL. NEW KNOWLEDGE: an extruded fitted-spline wall tessellates PIECEWISE-PLANAR BY CONSTRUCTION — Fusion extrudes the sketch curve's polyline approximation, so the crescent walls have literally zero smooth curvature in the tessellation, at any refinement. No available cuerpo79 source contains recoverable curvature (all exports are slicer-grade; the original BRep is gone).
- CONCLUSION: the original (d) FAILs were NEVER an estimator bug and never fixable from available data — cuerpo79's recoverable geometry is curvature-free. The only curvature in the design family is the meshb2-informed r=0.25 fillets.

FILLETED VARIANT EXPERIMENT (explicitly labeled — NOT a cuerpo79 verdict):
- Added 3 r=0.25 fillets on the wall-top outer perimeter (edges 45/37/50 = front/back/right, geometrically selected at Z=3.5) => 37 faces, volume 120.32.
- Fine STL (2436 tris): delta_est=1.9333e-04 IN [1e-4,1e-1] => **(d) PASS** (first (d) pass in the workstream), cov=0.945. Proves the delta estimator + competition pipeline work correctly when real curvature exists.
- (d2) got WORSE: P=91 C=0 = 91 vs 40 => FAR. VeryHigh tessellation shatters fillet bands into narrow planar strips that fragment in the planar grouping and never cluster into cylinders (C=0 despite 3 real r=0.25 cylinders). Same fragmentation class as meshb2's 51-vs-40, amplified. Open issue: cylinder recovery does not fire on finely-tessellated narrow fillet bands at eps~5.8e-4.

GOTCHAS:
1. STLExportOptions exports the whole COMPONENT (mesh bodies included) even when created with a single body — filter by geometry (the 10mm Y-gap made this trivial) or expect contaminated dumps.
2. opts.meshRefinement = TriangleMeshQualityOptions.VeryHighQualityTriangleMesh (NOT MeshRefinementOptions); refinement barely changes tri count on prismatic geometry.
3. Fusion body name is locale-dependent ("Cuerpo1"); rename immediately after first feature.
4. SketchFittedSplines.add() takes an ObjectCollection, not a list; a failed execute_script attempt leaves a stale named sketch — delete before retry (name collision gives "name (1)").
5. The mixed-charge crescent: decimation of a chained slice loop can silently jump collinear runs (the 3.8cm right edge was ONE segment) — always inspect the raw loop order before fitting splines.
6. Volume arithmetic is the cheapest structure validator: predicted 121.72 vs mesh 121.66 before building (0.05%) caught every misread notch/pocket during slicing interpretation.

HARNESS FILES (Temp\opencode): cuerpo79_cm_data.json RESTORED to the frozen 1780-tri dump (backup was cuerpo79_cm_data.slicer-grade.bak.json); new artifacts cuerpo79_recon_data.json (2134t unfilleted) + filtered filleted variant inside harness run harness_recon_run.txt / harness_filleted_run.txt; STLs cuerpo79_recon.stl / _fine.stl / _filleted.stl.

## 2026-08-19 — Extruded-wall sweep recovery: 3 fixes, 1 fundamental limitation

### What was built
Three-layer fix in `mcp_server/mesh_analysis.py` `_recover_curved_from_small_groups` for finely-tessellated extruded curved walls:
1. **Chain gate** (`_MAX_CHAIN_ANGLE_DEG=30°`): union-find on adjacent small planar groups only chains when mean normals agree within 30°. Kills sliver-bridged mega-clusters.
2. **Axis-partitioned chaining** (`_group_axis_partition`, `_CHAIN_AXIS_PERP_DOT=0.15`): groups in different perpendicular-axis partitions never chain — prevents fillet bands (⊥X/Y) from merging into wall bands (⊥Z) through smooth <30° normal transitions at fillet-wall junctions.
3. **Sweep-run recovery** (`_grow_sweep_runs`, `_fit_sweep_cylinder`, `_sweep_recover_cluster`): detects extrusion axis via cross-products + canonical candidates, grows monotonic normal-rotation runs, fits each as a cylinder via Kasa circle fit (axis-aware, handles open <360° arcs that `_fit_cylinder` can't).

### Bug found and fixed during verification
`_grow_sweep_runs` computed projected-normal angles as `atan2(perp[1], perp[0])` (always XY plane) regardless of extrusion axis. For X-axis extrusions (rotation in YZ), all angles collapsed to 90° → 0 runs. Fixed: pick the two non-dominant axis components based on `argmax(|axis|)`.

### What works
- 7/7 synthetic tests pass (annular sector, egg profile, meshb2 regression, etc.)
- Full suite: 289 passed, 1 xfailed, 0 failures (baseline 282 + 7 new)
- meshb2 regression fully preserved: 32P + 20C r=0.25, coverage 0.9498
- Axis partition successfully splits the 187-tri mega-cluster into clean single-axis clusters (141⊥X, 91⊥Y, 96⊥X, 114⊥Z)
- Sweep runs correctly detected on all large clusters (82.5° and 90° sweeps)

### The fundamental limitation (cuerpo79)
cuerpo79's crescent wall is an **extruded fitted spline**, not an extruded arc. The spline has monotonically rotating normals (looks like a cylinder to the run-grower) but **non-constant radius** (r varies 0.07–0.25 cm across the arc). The Kasa fit correctly rejects these because the radial residual (0.02–0.08 cm) far exceeds epsilon (5.8e-4 cm). This is the correct behavior — the sweep recovers true cylinders, not spline lookalikes.

Probe result on filleted cuerpo79: 119P + 4 freeform + 1 cylinder (r=0.37, sweep-recovered) = 124 total faces (baseline 126). The modest improvement (126→124) is real: the chain gate + axis partition broke the mega-cluster into smaller, cleaner clusters, and the sweep recovered one small cylinder. The remaining 4 freeform patches are the spline walls — correctly classified as non-cylindrical.

### Key insight
The (d2) "P=91 C=0" fragmentation issue from the filleted experiment is now understood: it was never a clustering bug — it's that the curved walls are splines, not cylinders. No amount of clustering improvement can make a spline surface pass a cylinder epsilon gate. Future work to recover spline surfaces would need a different approach (e.g., fit a 2D spline to the projected normal-angle vs radius curve).

## 2026-08-20 — Phase 2: spline-extrusion band recovery (124 → 52 faces)

### What was built
The "different approach" from the key insight above: recover prismatic walls as `surface_type: "extrusion"` faces with adaptive B-spline profiles. Implementation in `mesh_analysis.py`:

1. **Band finding via shared-edge direction votes** (not normal-based partitioning): two small groups band-chain when their shared mesh edge is ≥0.95-parallel to a canonical axis. This is the geometric signature of an extrusion (consecutive faces share axis-parallel edges), chains corners AND smooth junctions, never crosses into caps, and is immune to the normal-direction ambiguity that breaks per-group axis partitioning (groups whose normals are ⊥ to two canonical axes).

2. **PCA axis estimation**: smallest eigenvector of the normal scatter matrix Σnᵢnᵢᵀ replaces cross-product in `_find_extrusion_axis`; canonical candidates win ties (degenerate near-flat bands get arbitrary PCA directions). Out-of-plane eigenvalue gate (λ_min/trace ≥ 0.02) rejects taper/helix.

3. **Twist tripwire**: correlation between projected-normal azimuth and axial coordinate per group; high correlation + axially short strips = helical twist → reject.

4. **Greedy monotonic walk with multi-start**: the band graph (often branched by T-junctions from coplanar-merged strips or non-manifold tessellation) is traversed by smallest normal-turn at each step; trying all degree-1 endpoints and picking best coverage; emits the covered subset (≥50%) as the extrusion face, leftovers stay planar.

5. **Corner-pinned adaptive cubic B-spline ladder**: k=4 start, insert knots at worst-residual midpoints, refit each k; accept at first k with mean residual ≤ ε. Overfit guard: k≥n (underdetermined) or adaptive k−pin_ctrl0 ≥ 0.5·n → reject. Corners detected at >60° normal turns; pin multiplicity 1 (concentrates curvature without forcing C⁰, which would underdetermine fits on bands with many junctions). Lyche–Mørken knot removal post-pass: drop interior knots whose removal bound ≤ ε, one final refit, revert if residual regresses.

6. **Acceptance ladder per band**: closed + ≥8 points → Kasa 360° cylinder first (generalizes meshb2 path); else spline. This means circular arcs still extract as cylinders (by design — cylinder-first), and only non-circular smooth profiles reach the spline rung.

### Key deviations from the locked design (forced by real-mesh behavior)
- **Edge-vote banding replaces normal partitioning** — the locked "partition by `_group_axis_partition`" breaks on axis-aligned normals (ellipse top chord ⊥ to 2 axes → mispartitioned → band splits). Edge-direction votes are the principled fix.
- **Corner threshold 60° (not 45°)** — 45° over-detects fillet junctions as corners (8 corners in 19 points → k-guard fires). 60° keeps fillet junctions smooth while pinning real 90° profile corners.
- **Pin multiplicity 1 (not 3/2)** — multiplicity 3/2 causes underdetermination on real bands with many corners (8×3+4 = 28 ≥ 19 pts). Multiplicity 1 = just a knot at the corner; the spline stays determined and smoothly approximates the corner within ε.
- **Partial band emission (≥50% coverage)** — real bands are branched (T-junctions); emitting the covered subset (e.g. 66/83 groups = 79%) as one extrusion face, with leftovers staying planar, is a massive win vs all-or-nothing bail.

### Results
- **cuerpo79 filleted: 124 → 52 total faces** (58% reduction): 44 planar + 3 extrusion + 1 cylinder + 4 freeform. The 3 extrusion patches collapsed ~80 wall/fillet strips. The 4 remaining freeform patches are the X-axis band (35 groups, walk coverage <50% — too branched) and fragments.
- **meshb2: 32P + 20C r=0.25 UNCHANGED** — band pass never intercepts step-1-consumed clusters.
- **Full suite: 300 passed, 1 xfailed, 0 failures** (289 baseline + 11 new spline-band tests).
- **mesh_graph.py: generic `surface_type` handling** — no whitelist, "extrusion" flows through as `curve_type="extrusion"` automatically; docstring updated.

### What would push cuerpo79 below 50
The 4 remaining freeform patches + the 44 planar groups: the X-band (35 groups, 391 tris) walk-fails at <50% coverage due to extreme branching. A graph-simplification pre-pass (collapse coplanar-merged groups, prune T-junction edges by azimuth consistency) could make that band walkable → 1 more extrusion → ~46 faces. The 44 planar groups include ~22 horizontal caps (truth) + ~22 wall fragments (the T-junction leftovers from the Z-band's 17 uncovered groups). Phase 3: graph simplification + leftover absorption.

## 2026-08-20 (later) — Phase 2b: outer repeat loop + bidirectional all-seed walk (52 → 42)

Two changes hit the ≤50 target without needing edge pruning:

1. **Bidirectional all-seed walk**: every group is tried as a walk seed (not just degree-1 endpoints — a mid-path seed on a branched band often beats endpoints sitting on short side branches); each seed runs a forward greedy pass plus a backward pass from the seed through unblocked neighbours, so one seed covers both profile directions.

2. **Outer repeat loop over the whole 3-axis sweep**: the crucial ordering insight — the X-axis mega-band (35 groups) fails its walk on pass 1, but AFTER the Y/Z passes consume bridging groups, the X leftovers fragment into walkable sub-bands. Re-running all three axes on the shrunken leftover set (until no progress) picks those up. A per-axis repeat loop is NOT enough: axis X runs first, fails, and is never revisited unless the loop wraps all axes.

Also fixed a latent cross-axis double-consumption risk (a group emitted by axis X could be re-emitted by axis Y): `remaining` per pass now subtracts the global `used_groups`.

### Results (cuerpo79 filleted)
- **52 → 42 total faces** (target ≤50 HIT): 32 planar + 5 extrusion + 1 cylinder + 4 freeform
- 5 extrusion patches: 165 tris (k=16, 9 corners), 74 (k=11, 5 corners), 94 (k=16), 86 (k=5), 127 (k=5) — the last two are the X-band fragments from the repeat loop; all residuals 1.3e-4..5.7e-4 ≤ epsilon 5.8e-4
- Coverage tightened 1.0316 → 1.0103 (double-consumption fix)
- 300 passed, 1 xfailed, 0 failures; meshb2 unchanged (32P + 20C, coverage 1.0425)
- Edge pruning (op 3) and the Y-band 75° threshold were NOT needed — kept as future options
