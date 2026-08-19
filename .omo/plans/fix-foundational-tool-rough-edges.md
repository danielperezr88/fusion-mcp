# fix-foundational-tool-rough-edges - Work Plan

## TL;DR (For humans)

**What you'll get.** Six rough edges discovered during the live MeshBody1/MeshBody2 reconstruction demo are surgically fixed across `FusionMCP.py` and `mcp_server/fusion_server.py`. The `compare_mesh_to_brep` QA tool's `vertex_fallback` (which reported a phantom 2.94 cm max deviation against a true 0.933 cm) is replaced with BRep surface UV-grid point sampling. `execute_script` is fixed so nested function definitions resolve sibling names (the exec-scope quirk that wasted two probes in the demo). `_call` default timeout is bumped 30→60s and heavy tools get explicit 120s timeouts (structure_graph and execute_script both timed out at 30s during the demo). The vision-returning tools (`annotate_mesh_parameters`, `review_reconstruction`) and `compare_mesh_to_brep` get docstring reminders that non-multimodal models should dispatch to vision-capable subagents (or call compare after cuts land, respectively). The `slice_mesh` thin-band reliability issue is researched first, then fixed.

**Why this approach.** Four of the six issues are low-risk surgical code edits with known locations and clear fix strategies. Two are pure documentation additions (docstrings). One (`slice_mesh`) needs root-cause investigation before the fix is decided — so it's the last implementation wave. The demonstrate validated the dismantle-thesis: the surviving foundational toolset works when an agent composes it iteratively, but only if every tool metric is trustworthy. This plan makes the metrics trustworthy.

**What it will NOT do.** No new MCP tools introduced. No changes to tool function signatures, parameters, or return-type contracts (only enriched docstrings and improved internal fallback implementations). No changes to `scad_translator.py`, `bundle.py`, the OpenSCAD pipeline, or the add-in's event handlers outside `_compare_mesh_brep` and `_execute_script`. No `git rebase` or force push. No changes to `parameter_schemas.py` or `parameter_schemas.json`.

**Effort.** ~4 implementation waves (scope+timeout, compare sampling, exec-scope+docs, slice research+fix). ~7 implementation todos + 3 final-wave verifications. Single atomic commit on `master`.

**Risk.** Highest risk: replacing the `vertex_fallback` distance computation with surface sampling — if the BRep face `evaluator` API behaves unexpectedly on certain face types, the new path could be slower or produce errors. Mitigated by keeping the vertex-to-vertex computation as a catch-all fallback WITHIN the vertex_fallback branch and returning the method name transparently. Second risk: the `slice_mesh` investigation may reveal the issue is in the triangle-plane intersection math (numeric precision at near-coplanar input) requiring a targeted epsilon fix rather than a broad rewrite — the investigation todo pins this before any edit.

**Decisions.**

- **D1 — UV-grid sampling density for BRep surface:** ~5×5 = 25 samples per face, total capped at 2000 to bound response time. Chosen to give ~500–1000 total samples for typical parts (10–40 faces), well above the current 200-vertex sample count. The 5×5 grid uses `evaluator.getPointAtParameter` with evenly spaced UV params (verified available on this Fusion build — see `FusionMCP.py` L2392 note about `getParameterAtPoint` / `getPointAtParameter` / `getNormalAtPoint` being the available evaluator methods).
- **D2 — Timeout values:** `_call` default 30→60s (the minimum that prevented demo timeouts); `execute_script` explicit 120s; `structure_graph`, `analyze_mesh`, `slice_mesh` explicit 120s; `compare_mesh_to_brep` retains the new default 60s. Values chosen from observed demo behavior: fastest heavy tool ~30s, worst-case ~90s.
- **D3 — Vision documentation placement:** Primary: the three affected tool docstrings (`annotate_mesh_parameters`, `review_reconstruction`, `compare_mesh_to_brep`). Secondary: the `workflow_guide.py` step entries for annotate (step 4) and review (step 8). No standalone docs file — the README already lists these tools; the reminder belongs where the agent reads it (the docstring).
- **D4 — slice_mesh research scope:** Audit the triangle-plane intersection code in `mcp_server/mesh_slicer.py` (functions `_triangle_plane_segment` at L200, `_chain_loops` at L250, `slice_mesh_at` at L401; eps constants `_ONPLANE_EPS_BASE = 1e-6` at L70 and `_DEDUP_EPS_BASE = 1e-9` at L69) for numeric precision issues when triangle faces are near-coplanar with the slice plane (the "thin band" case). Investigate the interplay between `_ONPLANE_EPS_BASE` (vertex-on-plane tolerance) and `_DEDUP_EPS_BASE` (endpoint dedup/chaining tolerance) for near-coplanar thin bands. Then decide between a precision fix (adjusting the onplane epsilon or adding a near-coplanar guard before intersection) or a chaining-tolerance fix (widening `_DEDUP_EPS_BASE` for thin bands). Log the root cause and decision in `.omo/evidence/fix-T7-slice-research.log` BEFORE making the edit. Then apply the fix.
- **D5 — Tests-after, not TDD:** these are fixes/refactors to existing behavior, not new features. Agent-executed QA verifies the fixes produce correct values via live Fusion probes and headless py_compile/import checks.
- **D6 — Single atomic commit on `master`:** same discipline as the dismantle plan. No intermediate commits.

## Scope

### In scope

Fix six foundational-tool rough edges discovered during the live reconstruction demo. Two Python files are edited (`FusionMCP.py`, `mcp_server/fusion_server.py`), one optionally (`mcp_server/workflow_guide.py` docstring-only), one investigated+fixed (`mcp_server/mesh_slicer.py` slice intersection). Single atomic commit on `master`.

### Must NOT have (Out of scope)

- No NEW MCP tools introduced.
- No changes to tool function signatures, parameters, or return-type contracts — only enriched docstrings and improved internal fallback implementations.
- No edits to `mcp_server/scad_translator.py`, `bundle.py`, the OpenSCAD pipeline, or the add-in's event handlers outside `_compare_mesh_brep` and `_execute_script`.
- No `git rebase` / `force push` / history rewrite — single atomic commit on `master`.
- No changes to `parameter_schemas.py` / `parameter_schemas.json`.
- No deletion or signature change of any foundational tool.

## Verification strategy

- **Headless `pytest`**: the surviving ~282 pytest tests + standalone `check_*` suites must remain green. No new test symbols are removed; fixtures must keep working. Evidence: `.omo/evidence/fix-F-pytest.log` — verify no `FAILED` or `ERROR`.
- **Live Fusion 360 manual probe**: with the add-in running on port 7432, call `compare_mesh_to_brep` on the demo bodies (MeshBody2/Cuerpo78) and confirm the `sampled_deviation_cm.max` drops from ~2.94 to ≤1.5 (matching the analytically-proven true max of 0.933 with surface-sampling upper-bound slack). Call `execute_script` with a nested-function test payload and confirm no NameError. Call `structure_graph` and confirm no timeout. Evidence: `.omo/evidence/fix-F-live-probe.log`.
- **Fresh-process MCP probe**: launch `python mcp_server/fusion_server.py` fresh and confirm all tools import without error; the docstring changes don't break registration. Evidence: `.omo/evidence/fix-F-mcp-probe.log`.

## Execution strategy

Sequential waves:

1. **Scope + timeout** — bump `_call` default timeout; add per-tool explicit timeouts for heavy tools; add `compare_mesh_to_brep` docstring warning about sequential ordering (vs parallel cuts). [
2. **Compare surface sampling** — replace vertex-to-vertex fallback in `_compare_mesh_brep` with BRep face UV-grid point sampling; keep vertex-to-vertex as an inner catch-all within the fallback branch; add vision-subagent reminder to docstrings. 
3. **Exec-scope fix + vision docstrings** — fix `exec()` call in `_execute_script` to pass `local_vars` as both globals and locals; enrich `annotate_mesh_parameters`, `review_reconstruction`, `get_workflow_guide` step entries with vision-subagent reminders.
4. **slice_mesh research + fix** — audit triangle-plane intersection code in `mcp_server/mesh_slicer.py`; diagnose root cause of thin-band unreliability; apply targeted fix (precision epsilon or near-coplanar guard).
5. **Final verification wave** runs in parallel (F1–F3 all must APPROVE before the atomic commit).

Per-todo evidence under `.omo/evidence/fix-T<n>-*.log`. No intermediate commits — accumulate to a single atomic commit landed by F3.

## Todos

- [x] 1. Bump `_call` default timeout 30→60s and add explicit heavy-tool timeouts in `mcp_server/fusion_server.py`.
  - **References**: `mcp_server/fusion_server.py` line 297 (`def _call(command: str, params: dict = None, timeout: int = 30)`) — change default from 30 to 60. IMPORTANT: Currently NO `_call` invocation carries an explicit `timeout=` kwarg — ALL use the default 30. The fix is to ADD `timeout=120` to each heavy-tool call site, not to "change" a non-existent kwarg. The heavy-tool call sites are: (a) `analyze_mesh` at **L1576** (`raw = _call("extract_mesh_data", {"mesh": mesh})` → add `timeout=120`); (b) `structure_graph` at **L1688** (same `_call("extract_mesh_data", {"mesh": mesh})` → add `timeout=120`); (c) `slice_mesh` at **L1974** (same `_call("extract_mesh_data", {"mesh": mesh})` → add `timeout=120`); (d) `execute_script` at **L489** (`return _call("execute_script", {"code": code})` → add `timeout=120`). compare_mesh_to_brep at **L2017** (`_call("compare_mesh_brep", {"mesh": mesh, "body": body})`) — uses the new default 60, confirm that's sufficient (the compare is bounded by surface sampling; 60s should suffice for typical meshes — if it times out, add `timeout=120`). annotate_mesh_parameters and review_reconstruction already use the job-polling mechanism (330s) — no change. CRITICAL: Do NOT touch L1350 (`capture_screenshot`'s inline `requests.post(..., timeout=30)`) — that is a DIRECT requests.post, NOT a `_call` site, and is out of scope.
  - **Acceptance**: `grep -n "def _call" mcp_server/fusion_server.py` shows `timeout: int = 60`. `grep -n "timeout=120" mcp_server/fusion_server.py` returns ≥4 matches (at lines containing `_call("extract_mesh_data"` for analyze_mesh L1576, structure_graph L1688, slice_mesh L1974, AND `_call("execute_script"` at L489). Verify each heavy-tool line literally contains `timeout=120` by `grep -n "extract_mesh_data" mcp_server/fusion_server.py` — each matching line that is an analyze/structure_graph/slice_mesh call must also contain `timeout=120`. `grep -n "timeout=30" mcp_server/fusion_server.py` returns exactly ONE match at L1350 (capture_screenshot — out of scope, unchanged). `py -m py_compile mcp_server/fusion_server.py` exit 0. Fresh `python -c "import sys; sys.path.insert(0,'mcp_server'); import fusion_server; assert hasattr(fusion_server, 'structure_graph')"` exits 0.
  - **Happy QA**: grep shows the new timeout values; py_compile passes; import check passes. Evidence: `.omo/evidence/fix-T1-grep.log` + `fix-T1-pycompile.log`.
  - **Failure QA**: if a call site is missed (e.g. slice_mesh still uses default 60 without explicit `timeout=120`), the `grep -n "timeout=120"` count will be < 4 — fix by adding the explicit kwarg. If py_compile fails (typo in kwargs), fix. Verify L1350 is NOT modified: `git diff mcp_server/fusion_server.py | grep "1350"` returns empty. Evidence: `.omo/evidence/fix-T1-fresh-launch.log`.
  - **Commit**: no commit yet — accumulate for atomic commit in T7/F3.

- [x] 2. Add `compare_mesh_to_brep` docstring warning about sequential dependency (run after cuts, not in parallel).
  - **References**: `mcp_server/fusion_server.py` lines 2000–2016 (`compare_mesh_to_brep` docstring, starting after the `def` at L1999). Add a Note paragraph after the existing docstring text: "Note: compare_mesh_to_brep reads the BRep body's CURRENT state. If called in parallel with a cut/extrude operation on the same body, it may return metrics for the PRE-cut geometry. Always call this tool AFTER the preceding feature has completed, not in the same parallel batch." Do NOT change the function signature, return type, or tool registration — only the docstring text.
  - **Acceptance**: `grep -n "parallel" mcp_server/fusion_server.py` returns a match in the compare_mesh_to_brep docstring region (L1999–2016). `py -m py_compile mcp_server/fusion_server.py` exit 0. `python -c "import sys; sys.path.insert(0,'mcp_server'); import fusion_server; assert 'parallel' in fusion_server.compare_mesh_to_brep.__doc__"` exits 0.
  - **Happy QA**: grep matches; docstring contains the warning; py_compile passes. Evidence: `.omo/evidence/fix-T2-docstring.log`.
  - **Failure QA**: if the docstring is malformed (indentation breaks the tool registration), py_compile or import fails. Evidence: same file.
  - **Commit**: no commit yet — accumulate.

- [x] 3. Replace vertex-to-vertex fallback in `FusionMCP.py:_compare_mesh_brep` with BRep surface UV-grid point sampling.
  - **References**: `FusionMCP.py` lines 2433–2466 (the `vertex_fallback` branch of `_compare_mesh_brep`). Currently when `face_evaluators` is empty (no `getClosestPointTo` API), it builds `brep_verts` from `body.vertices` and measures each mesh vertex to the nearest BRep VERTEX (L2441–2462). Replace with: for each BRep face, get the `evaluator` (already available — L2436 shows `ev = body.faces.item(i).evaluator`), call `evaluator.getPointAtParameter(u, v)` on a UV grid. Use ADAPTIVE grid density: compute `grid_n = max(3, ceil(sqrt(face_area_cm2 / 0.5)))` per face so large faces get proportionally more samples (target ~1 sample per 0.5 cm² of face area). Cap total surface points at 2000 (if total > 2000, reduce per-face grid density proportionally). Collect ALL surface points into `brep_surface_points` (list of (x,y,z) tuples). Then for each sampled mesh vertex, compute `min(distance to any brep_surface_point)` — same min-distance loop, but against surface samples instead of vertices. NOTE: distance-to-nearest-sample is an UPPER BOUND on the true closest-point distance; the approximation gap scales with face_extent / (2 × grid_n). The adaptive grid keeps this gap small for large faces. Keep the `method` field reporting `"vertex_fallback"` (it's still a fallback — just a better one; alternatively report `"surface_sampled"` for clarity). Keep the `face_evaluators` detection logic (L2434–2439) unchanged — if `getClosestPointTo` exists on a future build, it's still preferred. The `evaluator.getPointAtParameter` return signature: a tuple `(ret_val, point3d)` where `ret_val` is a bool — handle it. Guard with try/except per face (skip faces whose evaluator fails). UV param bounds: use `evaluator.paramBounds` (if available) or default [0,1]×[0,1]. IMPORTANT: keep the original vertex-to-vertex computation as an inner catch-all WITHIN the fallback branch — if ALL faces' surface sampling fails, fall back to the vertex-to-vertex path so there's always a valid result.
  - **Acceptance**: After the change, `compare_mesh_to_brep(mesh=1, body=1)` on the live MeshBody2/Cuerpo78 scene returns `sampled_deviation_cm.max` ≤ 1.5 (the analytically-proven true max is 0.933; surface-sampled distance is an upper bound, so ≤1.5 cm is the acceptance threshold — adaptive grid density keeps the gap small for large faces). The `method` field reports `"vertex_fallback"` or `"surface_sampled"`. `py -m py_compile FusionMCP.py` exit 0. The `FusionMCP.py` add-in loads without error. Live probe: `_call("compare_mesh_brep", {"mesh": "1", "body": "1"})` returns valid JSON with no error.
  - **Happy QA**: py_compile passes; live compare returns max ≤ 1.5; surface point count is in expected range (500–2000). Evidence: `.omo/evidence/fix-T3-compare-live.log`.
  - **Failure QA**: if the compare max is > 1.5 (the adaptive grid wasn't dense enough for large faces), increase the target density constant from 0.5 to 0.25 (doubling samples per face) and retry. If `getPointAtParameter` is unavailable on some face types (e.g. planar vs cylindrical), the per-face try/except skips that face — verify the method still returns valid distances from remaining faces. If ALL faces fail, the catch-all vertex path must still run (keeping the original vertex-to-vertex as an inner fallback). Evidence: `.omo/evidence/fix-T3-compare-fallback.log`.
  - **Commit**: no commit yet — accumulate.

- [x] 4. Add vision-subagent reminder docstrings to `annotate_mesh_parameters` and `review_reconstruction` in `mcp_server/fusion_server.py`.
  - **References**: `mcp_server/fusion_server.py` lines 2155–2171 (`annotate_mesh_parameters` docstring) — add: "Note: This tool returns PNG image blocks for model-side classification. If the calling model cannot process images, dispatch a vision-capable subagent to inspect the returned views and extract the object class, then call `select_parameter_schema` with the result." Lines 2286–2300 (`review_reconstruction` docstring) — add: "Note: This tool returns side-by-side PNG image pairs for model-side visual QA. If the calling model cannot process images, dispatch a vision-capable subagent to compare each mesh/brep pair and report whether features are missing." Also add to `compare_mesh_to_brep` docstring (L2000–2009): "Note: this tool is vision-free and works for any model. For visual QA, see `review_reconstruction`." Do NOT change signatures, return types, or registrations.
  - **Acceptance**: `grep -n "vision-capable subagent" mcp_server/fusion_server.py` returns matches in the annotate and review docstrings. `grep -n "vision-free" mcp_server/fusion_server.py` returns a match in the compare docstring. `py -m py_compile mcp_server/fusion_server.py` exit 0. `python -c "import sys; sys.path.insert(0,'mcp_server'); import fusion_server; assert 'vision-capable' in fusion_server.annotate_mesh_parameters.__doc__; assert 'vision-capable' in fusion_server.review_reconstruction.__doc__"` exits 0.
  - **Happy QA**: grep matches; docstrings contain the reminders; py_compile passes. Evidence: `.omo/evidence/fix-T4-docstrings.log`.
  - **Failure QA**: if the docstring indentation breaks the tool registration, py_compile or import fails. Evidence: same file.
  - **Commit**: no commit yet — accumulate.

- [x] 5. Fix `exec()` scope in `FusionMCP.py:_execute_script` so nested function definitions can resolve sibling names.
  - **References**: `FusionMCP.py` line 591 (`exec(code, {"__builtins__": __builtins__}, local_vars)`). Currently the globals dict is `{"__builtins__": __builtins__}` and the locals dict is `local_vars`. Functions defined in `code` land in `local_vars` (the locals dict), but when called, their body resolves names against the GLOBALS dict (which lacks the sibling functions). Fix: pass `local_vars` as BOTH the globals and locals dict — `exec(code, local_vars, local_vars)` — but FIRST merge a clean `__builtins__` into `local_vars`: add `local_vars["__builtins__"] = __builtins__` before the exec call. This makes sibling names visible in function bodies. The `local_vars` dict already contains `app`, `ui`, `design`, `root`, `result`, `adsk`, `math`, `json` (L584–589) — these remain accessible. After the fix, test with a script that defines two functions where one calls the other (the exact pattern that failed in the demo).
  - **Acceptance**: After the fix, calling `execute_script` with code that defines `def helper(): return 42` and `def caller(): return helper()` and sets `result['output'] = str(caller())` returns `{"success": True, "output": "42"}` instead of `NameError: name 'helper' is not defined`. `py -m py_compile FusionMCP.py` exit 0. The add-in runs without error. Live probe: `_call("execute_script", {"code": "def f():\n    return 7\ndef g():\n    return f()\nresult['output'] = str(g())"})` returns `{"success": True, "output": "7"}`.
  - **Happy QA**: py_compile passes; the nested-function test script returns "7" without NameError. Evidence: `.omo/evidence/fix-T5-exec-live.log`.
  - **Failure QA**: if the `__builtins__` merge is wrong, builtins like `print`, `len`, `range` become unavailable — test with `result['output'] = str(len([1,2,3]))`. Evidence: `.omo/evidence/fix-T5-exec-builtins.log`.
  - **Commit**: no commit yet — accumulate.

- [x] 6. Add vision-subagent reminder to `mcp_server/workflow_guide.py` step entries for annotate (step 4) and review (step 8).
  - **References**: `mcp_server/workflow_guide.py` GUIDE list (lines ~26–143). Find the entry where `"tool": "annotate_mesh_parameters"` (step 4) — add to its `"model_action"` or a new `"note"` field: "If the calling model cannot process images, dispatch a vision-capable subagent to inspect the returned views and extract the object class." Find the entry where `"tool": "review_reconstruction"` (step 8) — add the same kind of reminder. Do NOT change the `"tool"` string names or the `get_step` lookup logic. Do NOT change the GUIDE entry count (still 8).
  - **Acceptance**: `python -c "import sys; sys.path.insert(0,'mcp_server'); import workflow_guide, json; data = json.loads(workflow_guide.GUIDE_JSON); assert len(data) == 8; ann = [s for s in data if s['tool'] == 'annotate_mesh_parameters'][0]; assert 'vision-capable' in ann.get('model_action', '') or 'vision-capable' in ann.get('note', ''), ann; rev = [s for s in data if s['tool'] == 'review_reconstruction'][0]; assert 'vision-capable' in rev.get('model_action', '') or 'vision-capable' in rev.get('note', ''), rev"` exits 0. `py -m py_compile mcp_server/workflow_guide.py` exit 0.
  - **Happy QA**: the GUIDE_JSON probe passes; py_compile passes. Evidence: `.omo/evidence/fix-T6-guide.log`.
  - **Failure QA**: if the GUIDE entry count changes or get_step breaks, the probe fails. Evidence: same file.
  - **Commit**: no commit yet — accumulate.

- [x] 7. Research and fix `slice_mesh` thin-band reliability in `mcp_server/mesh_slicer.py`.
  - **References**: `mcp_server/mesh_slicer.py` — the triangle-plane intersection code. Key functions: `_triangle_plane_segment` at L200 (computes the intersection segment of a triangle with the slice plane), `_chain_loops` at L250 (chains raw segments into ordered closed loops), `slice_mesh_at` at L401 (the entry point called by the `slice_mesh` MCP tool in `fusion_server.py` L1987). Epsilon constants: `_ONPLANE_EPS_BASE = 1e-6` at L70 (distance-to-plane "on plane" tolerance, used to detect vertices ON the plane), `_DEDUP_EPS_BASE = 1e-9` at L69 (point dedup / collinear simplification tolerance, used in loop chaining). The `slice_mesh` MCP tool is at `fusion_server.py` L1944–1995; it calls `_call("extract_mesh_data")` at L1974 (timeout will be bumped by T1) then runs `mesh_slicer.slice_mesh_at(...)` LOCALLY at L1986–1987 (pure Python, not a `_call`). The reported symptom: thin-band slices (e.g. a Z-slice at z=0.25 when the mesh has vertices at z=0 and z=0.5) produce false loops or miss real loops. Investigation steps: (a) Read `_triangle_plane_segment` (L200) — find where the segment-vs-plane test computes intersection points; check for numeric precision when a triangle vertex is ON or NEAR the slice plane (within `_ONPLANE_EPS_BASE` = 1e-6). (b) Read `_chain_loops` (L250) — check endpoint dedup tolerance `_DEDUP_EPS_BASE` = 1e-9: near-coplanar triangles may produce segments whose endpoints differ by >1e-9 but < the mesh's own vertex precision, causing chain failures. (c) Root-cause decision: if the issue is near-coplanar precision (vertices within ~1e-6 cm of the plane), the onplane epsilon may need to be scale-adaptive (multiplied by the mesh's bounding-box scale, which the code at L179/L183 already computes: `_ONPLANE_EPS_BASE * scale`). If the issue is endpoint chaining tolerance, `_DEDUP_EPS_BASE` may need to be wider for thin-band slices (or made scale-adaptive too). Log the root cause and decision in `.omo/evidence/fix-T7-slice-research.log` BEFORE making the edit. Then apply the targeted fix.
  - **Acceptance**: After the fix, `py -m py_compile mcp_server/mesh_slicer.py` exit 0. A regression test: create a known mesh with vertices at z=0 and z=0.5, slice at z=0.25, and confirm the returned loops match expected geometry (no false loops, no missed loops). The existing `slice_mesh` MCP tool still works on solid-height slices (no regression). `py -m pytest tests/test_mesh_slicer.py -q` exits 0 (no FAILED, no ERROR). If a test exercises the slice path and must be updated, update it. Evidence: `.omo/evidence/fix-T7-slice-fix.log`.
  - **Happy QA**: py_compile passes; the regression slice test produces correct loops; existing `tests/test_mesh_slicer.py` suite green. Evidence: `.omo/evidence/fix-T7-slice-fix.log`.
  - **Failure QA**: if the fix introduces a regression in solid-height slices (the reliable case), the existing `tests/test_mesh_slicer.py` tests would fail — rollback the edit and use a narrower fix. Evidence: `.omo/evidence/fix-T7-slice-regression.log`.
  - **Commit**: no commit yet — accumulate.

## Final verification wave

- [x] F1. Plan compliance audit — verify all 6 fixes are applied exactly as specified and no out-of-scope changes leaked in.
  - **References**: Compare the actual diffs against the plan. Verify: (a) `_call` default is 60 and heavy tools have 120s timeouts; (b) `compare_mesh_to_brep` docstring has the parallel warning; (c) `_compare_mesh_brep` in `FusionMCP.py` uses surface UV-grid sampling (not bare vertex-to-vertex); (d) `annotate_mesh_parameters`, `review_reconstruction`, `compare_mesh_to_brep` docstrings have vision-subagent reminders; (e) `_execute_script` uses the scope-fixed exec call; (f) `workflow_guide.py` step 4 and 8 entries have vision reminders; (g) `mcp_server/mesh_slicer.py` slice fix is present and research log exists.
  - **Acceptance**: `grep -rn "timeout=30" mcp_server/fusion_server.py` returns exactly ONE match at L1350 (capture_screenshot — out of scope, unchanged). `grep -n "vision-capable" mcp_server/fusion_server.py mcp_server/workflow_guide.py` returns ≥3 matches. `grep -n "getPointAtParameter" FusionMCP.py` returns matches in the `_compare_mesh_brep` region. `FusionMCP.py` L591 shows `exec(code, local_vars, local_vars)` (or equivalent scope-fix). `.omo/evidence/fix-T7-slice-research.log` exists and documents the root cause. `git diff --stat` shows only the expected files modified (`FusionMCP.py`, `mcp_server/fusion_server.py`, `mcp_server/workflow_guide.py`, `mcp_server/mesh_slicer.py`). Evidence: `.omo/evidence/fix-F1-audit.log`.
  - **Commit**: this verifier only PRODUCES evidence — does not commit.

- [x] F2. Code quality + import probe — verify all edited files compile, import, and tools register.
  - **References**: `py -m py_compile FusionMCP.py mcp_server/fusion_server.py mcp_server/workflow_guide.py mcp_server/mesh_slicer.py` exit 0. Fresh `python -c "import sys; sys.path.insert(0,'mcp_server'); import fusion_server; assert hasattr(fusion_server, 'compare_mesh_to_brep'); assert hasattr(fusion_server, 'annotate_mesh_parameters'); assert hasattr(fusion_server, 'review_reconstruction'); assert hasattr(fusion_server, 'execute_script'); assert hasattr(fusion_server, 'structure_graph'); assert hasattr(fusion_server, 'slice_mesh')"` exits 0. `python -c "import sys; sys.path.insert(0,'mcp_server'); import workflow_guide; import json; assert len(json.loads(workflow_guide.GUIDE_JSON)) == 8"` exits 0.
  - **Acceptance**: py_compile exits 0 for all 4 files; import assertions pass; GUIDE has 8 entries; no NameError at import time.
  - **Happy QA**: all compiles and imports pass. Evidence: `.omo/evidence/fix-F2-pycompile.log` + `fix-F2-import-probe.log`.
  - **Failure QA**: if the exec-scope fix in `FusionMCP.py` or the surface-sampling edit breaks an import path, the import probe fails. Evidence: same file.
  - **Commit**: this verifier only PRODUCES evidence — does not commit.

- [x] F3. Live Fusion 360 manual QA + atomic commit on `master`.
  - **References**: Running Fusion 360 (port 7432). The demo bodies (MeshBody2 index 1, Cuerpo78 index 1) in the open document. Issue: `compare_mesh_to_brep(mesh="1", body="1")` — verify `sampled_deviation_cm.max` is ≤ 1.5 (down from ~2.94; the analytically-proven true max is 0.933 cm, and surface-sampled distance is an upper bound with slack for large-face approximation). `execute_script` with the nested-function test payload — verify no NameError. `structure_graph(mesh="1")` — verify no timeout. Then atomic commit: `git add FusionMCP.py mcp_server/fusion_server.py mcp_server/workflow_guide.py mcp_server/mesh_slicer.py` (only the files this plan touches) + `.omo/evidence/fix-*` + `git commit` with message "fix: compare surface sampling, exec-scope, timeout bumps, vision docstrings, slice thin-band fix". `git status` reports clean tree after.
  - **Acceptance**: compare returns max ≤ 1.5; execute_script nested functions work; structure_graph completes without timeout; `git log --oneline -1` shows the atomic commit; `git status` clean (excluding `.omo/notepads`).
  - **Happy QA**: all three live probes pass; git commit lands. Evidence: `.omo/evidence/fix-F3-live-probe.log` + `fix-F3-commit.log`.
  - **Failure QA**: if the compare max is still > 1.5 (the surface sampling didn't improve fidelity enough), increase the target density constant from 0.5 to 0.25 (doubling samples per face) and retry. If structure_graph still times out, consider a higher timeout or async job mode. Evidence: same file.
  - **Commit**: ATOMIC SINGLE COMMIT — `git add` only the touched files, then `git commit -m "..."`. Run `git status` WORKTREE check before commit: any unrelated modified file should be cleanly separated.

## Commit strategy

Single atomic commit on `master` after F1–F3 all APPROVE. Commit message:

```
fix: foundational tool rough edges from live reconstruction demo

- compare_mesh_to_brep: replace vertex_to_vertex fallback with BRep surface
  UV-grid point sampling (5x5/face, cap 2000) — fixes phantom 2.94 cm max
  deviation (true max 0.933)
- execute_script: fix exec() scope so nested function definitions resolve
  sibling names (pass local_vars as both globals and locals)
- _call: bump default timeout 30→60s; analyze_mesh/structure_graph/slice_mesh/
  execute_script get explicit 120s timeouts
- annotate_mesh_parameters/review_reconstruction/compare_mesh_to_brep: add
  docstring reminders about dispatching to vision-capable subagents for
  non-multimodal models
- workflow_guide: add vision-subagent notes to step 4 (annotate) and step 8
  (review)
- compare_mesh_to_brep: add docstring warning about sequential dependency
  (run after cuts, not in parallel)
- slice_mesh: fix thin-band reliability via [root cause from T7 research log]
```

Files staged: `FusionMCP.py`, `mcp_server/fusion_server.py`, `mcp_server/workflow_guide.py`, `mcp_server/mesh_slicer.py`, `.omo/evidence/fix-*`.

## Success criteria

1. `compare_mesh_to_brep` on the demo bodies returns `sampled_deviation_cm.max` ≤ 1.5 (matching the analytically-proven true max of 0.933 cm, with upper-bound slack for surface-sampled approximation).
2. `execute_script` with nested function definitions no longer raises `NameError` (sibling functions resolve correctly).
3. `structure_graph` and `execute_script` no longer time out under the new timeout values.
4. `annotate_mesh_parameters`, `review_reconstruction`, and `compare_mesh_to_brep` docstrings contain vision-subagent reminders for non-multimodal models.
5. `compare_mesh_to_brep` docstring warns about sequential dependency.
6. `slice_mesh` produces correct loops on thin-band slices (root cause researched and fixed).
7. All existing pytest tests remain green; all edited files compile and import cleanly.
8. Single atomic commit on `master` with a clean worktree after.