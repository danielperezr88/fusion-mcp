# Learnings — fix-foundational-tool-rough-edges

Conventions, patterns, and successful approaches discovered during work on this plan.

_Auto-scaffolded by /start-work. Append new entries below - never overwrite._

---

## T3 — BRep surface UV-grid sampling in `_compare_mesh_brep` (2026-08-10)

### What changed
Replaced the bare vertex-to-vertex fallback measurement with adaptive BRep
surface UV-grid sampling in `FusionMCP.py`. When `face_evaluators` is empty
(no `getClosestPointTo` on this Metis F6 build), each BRep face is sampled on
a `grid_n x grid_n` UV grid via `evaluator.getPointAtParameter`, where
`grid_n = max(3, ceil(sqrt(area_cm2 / 0.5)))` (~1 sample per 0.5 cm^2, default
5 when `face.area` can't be read), total capped at 2000 by scaling each face's
grid proportionally (`sqrt(2000/naive_total)`, floor 2). The original
vertex-to-vertex computation is retained as an inner catch-all that only runs
when surface sampling yields zero points (`if not face_evaluators and not
brep_surface_points`). `method` field stays `"vertex_fallback"`.

### API surprises (validated against this build's evaluator contract)
- `SurfaceEvaluator.getPointAtParameter(u, v)` returns a **tuple `(ret_val,
  point3d)`** where `ret_val` is a bool — must unpack and check it. A `False`
  ret_val means the parameter point is outside the face; those samples are
  skipped.
- `SurfaceEvaluator.paramBounds` returns a **`BoundingBox2D`** (`.minPoint.x`,
  `.minPoint.y`, `.maxPoint.x`, `.maxPoint.y`), NOT a plain tuple. Code must
  tolerate both shapes (defensive `_evaluator_uv_bounds` helper) and default
  to `[0,1] x [0,1]` when it's unavailable or degenerate.
- `BRepFace.area` returns the surface area in cm^2 (float) — usable directly.
  On this build it did not raise, but it is wrapped in try/except anyway.

### Decision: keep `method` = `"vertex_fallback"`
The plan allows `"vertex_fallback"` or `"surface_sampled"`. Kept
`"vertex_fallback"` because `tests/test_mesh_to_brep_live.py` L242 hard-asserts
`resp["method"] == "vertex_fallback"` (live test, skips headless) — changing it
would break that test on the next live run, and the plan's F3 acceptance
accepts either value.

### Exact new code location (FusionMCP.py)
- `_surface_grid_n(area_cm2, area_per_sample=0.5)` — L2382
- `_cap_grid_ns(grid_ns, max_points=2000)` — L2394
- `_evaluator_uv_bounds(ev, default=...)` — L2408
- `_sample_brep_surface_points(body, max_points=2000, area_per_sample=0.5)` — L2431
- `_compare_mesh_brep` fallback branch: surface sampling at L2542-2548, inner
  vertex-to-vertex catch-all guard at L2550-2556, measurement loop
  `elif brep_surface_points` branch at L2570-2573. `face_evaluators` detection
  loop (L2534-2539) and the output dict keys are unchanged.

### Headless evidence
`.omo/evidence/fix-T3-headless.log` — py_compile exit 0 + 20/20 harness checks:
grid_n(100 cm^2)=15, cap-2000 scales 11250->1800 samples with per-face floor 2,
surface-sample min-distance (1.112 cm) <= vertex min-distance (6.624 cm) and
well under the phantom 2.94 cm, failing/paramBounds-less/area-less faces handled,
zero-face body returns [] (catch-all path).


## 2026-08-10 — T6: vision-subagent reminders in workflow_guide.py

- **Changed**: `mcp_server/workflow_guide.py` (the only code file touched; evidence at `.omo/evidence/fix-T6-guide.log`).
- **Where**: used the `"model_action"` field (not a new `"note"` key) for BOTH step 4 (`annotate_mesh_parameters`) and step 8 (`review_reconstruction`). Both entries already had non-None `model_action` strings, so the reminder appended naturally. `note` is reserved in this file for the reconstruct step's DISMANTLED marker — keeping the new reminder in `model_action` avoids diluting that convention.
- **Structure detail**: `GUIDE` is a Python list of 8 dicts, each with exactly `tool/purpose/inputs/outputs/model_action/branch/fallback` (reconstruct adds a `note`). `GUIDE_JSON = json.dumps(GUIDE, indent=2)` is generated at import time, so a text edit that keeps Python syntax valid automatically keeps the JSON valid — no manual JSON editing needed. `get_step()` looks up by tool name or first-underscore-segment, never by index.
- **Windows note**: `python` is NOT on PATH in this environment; `py` is the launcher that works (`py -c ...`, `py -m py_compile`).
- Probe/compile/grep all green: len==8 preserved, 'vision-capable' present in both entries.

## T1 — _call default timeout 30->60s + heavy-tool timeout=120 (2026-08-10)

- **Changed**: `mcp_server/fusion_server.py` ONLY (5 edits, evidence at `.omo/evidence/fix-T1-grep.log` + `fix-T1-pycompile.log`).
- **Line drift**: plan refs (L297/L489/L1576/L1688/L1974) matched EXACTLY — zero drift for all 5 sites.
- **Unexpected finding**: a FOURTH `_call("extract_mesh_data", {"mesh": mesh})` exists at L2103 inside `_annotate_mesh_parameters_sync` (private sync helper backing the `annotate_mesh_parameters` job-polling tool). The plan's annotate/review "no change" rule applies to it — left UNTOUCHED (keeps new 60s default). Plan reviewers missed this 4th site; grep is authoritative.
- **Final line numbers**:
  - `def _call` default 60s: L297
  - `timeout=120` sites: L489 (execute_script), L1576 (analyze_mesh), L1688 (structure_graph), L1974 (slice_mesh) — exactly 4
  - `timeout=30` (capture_screenshot requests.post): L1350 — the file's only `timeout=30`, unchanged
  - Untouched `_call` sites: L2017 (compare_mesh_brep, 60s default per F3), L2103 (annotate sync helper, 60s default)
- **Import probe gotcha (pre-existing)**: `import fusion_server` fails at L20 `from mcp.server.fastmcp import FastMCP, Image` — installed SDK is mcp==2.0.0 which dropped `mcp.server.fastmcp`, but requirements.txt allows `mcp>=1.0.0`. PROVEN pre-existing: git HEAD (unmodified) fails identically at L20. T1's diff touches no imports. F3 should pin `mcp<2.0.0` or migrate to the 2.x API before the live-QA probe.
- **Windows note (confirmed again)**: `python`/`grep` are NOT on PATH; use `py` and `Select-String`.

## T2 — compare_mesh_to_brep docstring parallel-order warning (2026-08-10)

- **Changed**: `mcp_server/fusion_server.py` ONLY (docstring text only; evidence at `.omo/evidence/fix-T2-docstring.log`).
- **Line refs**: `def compare_mesh_to_brep` still at L1999, docstring closed at L2016 (zero drift from plan). Note paragraph appended after the "Stage 7" line, before `Args:`.
- **Final line numbers**: Note paragraph = L2013–L2016; final line of the Note ("feature has completed, not in the same parallel batch.") = **L2016**. Args block shifted to L2018+.
- **Import probe**: hit the SAME pre-existing mcp==2.0.0 issue — `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` at L20 (identical to T1's finding). Not caused by the edit; not fixed.
- **py_compile**: exit 0. `grep "parallel batch"` matches L2016 (used grep tool; plain `grep` still not on PATH).

## T5 — exec() scope fix in `_execute_script` (2026-08-10)

- **Changed**: `FusionMCP.py` ONLY (2-line change, evidence at `.omo/evidence/fix-T5-exec-headless.log`).
- **Where**: `_execute_script` — final exec call at **L592**, with the new `local_vars["__builtins__"] = __builtins__` merge at **L591**. Grep confirmed the plan ref L591 was unchanged by T3 (T3's +104 lines landed at ~L2379, below this function).
- **What**: `exec(code, {"__builtins__": __builtins__}, local_vars)` → `local_vars["__builtins__"] = __builtins__` + `exec(code, local_vars, local_vars)`. Functions defined in `code` land in `local_vars`; when called they resolve names against the GLOBALS dict — previously the bare `{"__builtins__": ...}` lacked sibling functions → `NameError: name 'helper' is not defined`. Passing `local_vars` as BOTH globals and locals fixes it; merging `__builtins__` into `local_vars` keeps builtins available (since that dict is now the real globals).
- **Harness result**: `py ...\t5_harness.py` → `T5 HEADLESS PASS` (sibling `def g()` calling `def f()` → "7"; builtins `len` → "3"). Old pattern re-run → `NameError: name 'f' is not defined` (proves the fix is what changed behavior).
- **Windows note (T1/T6 confirmed again)**: `py` is the launcher; `python`/`grep` not on PATH. PowerShell `py -c "..."` mangles embedded `\n`/quotes — for multi-line code use a temp `.py` file instead of `-c`.

## T4 — vision-subagent reminder docstrings (2026-08-10)

- **Changed**: `mcp_server/fusion_server.py` ONLY (3 docstring-only additions; evidence at `.omo/evidence/fix-T4-docstrings.log`).
- **Final line numbers of the three Note insertions** (all placed after the "Stage N" line, before `Args:` — matching T2's note position in compare_mesh_to_brep):
  - `annotate_mesh_parameters` Note = **L2182–2185** ("dispatch a vision-capable subagent" at **L2183**; `def annotate_mesh_parameters` at L2157)
  - `review_reconstruction` Note = **L2314–2317** ("dispatch a vision-capable subagent" at **L2316**; `def review_reconstruction` at L2287)
  - `compare_mesh_to_brep` vision-free Note = **L2018–2019** ("Note: this tool is vision-free..." at **L2018**), added AFTER T2's parallel-batch Note (L2013–2016, untouched); `def compare_mesh_to_brep` still at L1999.
- **Line-wrap gotcha**: grep matches line-by-line. My FIRST version of the annotate note wrapped "vision-capable" / "subagent" across two lines, so `grep "vision-capable subagent"` found only 1 match (the review note). Requirement is >=2 matches for the exact string. Reflowed the annotate note so "vision-capable subagent" sits on ONE line (L2183) — then 2/2. Keep reminder phrases on a single line when a grep-count gate depends on them.
- **Import probe**: hit the SAME pre-existing mcp==2.0.0 issue — `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` at L20 (identical to T1/T2). Not caused by this edit; not fixed.
- **py_compile**: exit 0. `grep "vision-free"` = 2 matches (L2001 pre-existing header + L2018 new note).

## T7 — slice_mesh thin-band reliability fix (2026-08-10)

- **Changed**: `mcp_server/mesh_slicer.py` (2 targeted fixes) + `tests/test_mesh_slicer.py` (+4 regression tests). Evidence: `.omo/evidence/fix-T7-slice-research.log` + `fix-T7-slice-fix.log`.
- **Root cause was NOT the epsilon interplay the plan expected** — it was TWO separate algorithmic bugs:
  1. **`_chain_loops` first-hop vertex drop** (PRIMARY): the loop-walking code appended `reps[start]` and `reps[nxt2]` but NEVER `reps[cur]` (the first-hop vertex). In normal meshes, the dropped vertex is a collinear midpoint from quad-face triangulation that `_simplify_loop` removes anyway. In thin-band meshes (near-plane face producing full-edge segments with no midpoints), the dropped vertex is a REAL corner. A 4-segment rectangle → 3 pts (1 corner lost); a 3-segment triangle → 0 chains (dropped entirely, len < 3).
  2. **`_triangle_plane_segment` near-plane double-counting** (LATENT): when a vertex distance is within `onplane_eps` but nonzero (e.g. 9e-7 from 6-dp rounding in FusionMCP.py L2842), both the edge-crossing loop AND the on-plane-corner loop fire, producing spurious interpolated crossings near (but not at) the vertex. Masked by collinearity in axis-aligned meshes; could corrupt loops on diagonal/curved meshes.
- **Fix A (L212-224)**: clamp on-plane vertex distances to exactly 0.0 after computing `on` array → edge-crossing test `di*dj < 0` becomes `0*dj = 0`, never fires → no spurious crossings.
- **Fix B (L303-314)**: append `reps[cur]` at top of while loop instead of `reps[nxt2]` at bottom → all vertices included.
- **Key insight**: Fusion's `displayMesh` (FusionMCP.py L2839-2843) IS indexed (`nodeIndices` reference `nodeCoordinates`), and coordinates are rounded to 6 dp (`round(pt.x, 6)`). `_ONPLANE_EPS_BASE = 1e-6` sits exactly at this 6-dp precision boundary — a vertex at z=0.249999 has computed distance `0.249999 - 0.25 = -9.999999999747044e-07` whose abs() < 1e-6 → "on" the plane (for scale=1). For scale=10, even z=0.24999 (distance ~1e-5) is "on".
- **Why widening `_DEDUP_EPS_BASE` would NOT fix it**: the first-hop drop is algorithmic, not epsilon-related. Widening dedup could merge DISTINCT vertices in normal meshes, corrupting valid loops. The clamp+chain fixes are the correct approach.
- **Plan D4 asked the wrong question**: it assumed the bug was in the epsilon interplay. The actual bug was in loop-walking + double-counting. The research-first mandate (write log before editing) was critical — it prevented a wrong fix (epsilon widening).
- **Windows note (T1/T5/T6 confirmed again)**: `py` works; `python`/`grep` not on PATH. PowerShell `py -c "..."` mangles embedded quotes/newlines — use temp `.py` files for multi-line code.

## F2 — INDEPENDENT VERIFIER verdict: code quality + import probe (2026-08-10)

**VERDICT: APPROVE** — all gates green. Evidence logs:
`.omo/evidence/fix-F2-pycompile.log` + `.omo/evidence/fix-F2-import-probe.log`.

- **Compile**: `py -m py_compile FusionMCP.py mcp_server/fusion_server.py
  mcp_server/workflow_guide.py mcp_server/mesh_slicer.py` → **EXIT 0**. Note
  `py` here is 3.14.2; compile is interpreter-agnostic, exit code is what F2 gates on.
- **Imports (python3.13 = 3.13.14, the server's real interpreter)**: all 4
  probes exit 0 — `IMPORT_OK` (all 6 tools register: compare_mesh_to_brep,
  annotate_mesh_parameters, review_reconstruction, execute_script,
  structure_graph, slice_mesh), `GUIDE_OK` (exactly 8 entries), `DOCSTRINGS_OK`
  ('parallel' in compare docstring, 'vision-capable' in annotate + review),
  `SLICER_OK` (mesh_slicer.slice_mesh_at standalone import).
- **pytest**: `py -m pytest tests/ -q` → **248 passed, 1 xfailed, 0 failed,
  0 error, exit 0** (82.06s). No SKIPs in this run — live-dependent files
  (test_mesh_to_brep_live.py etc.) were collected as pass/xfail, so the
  "may SKIP" carve-out was not even exercised. `tests/test_mesh_slicer.py` alone:
  **18 passed** (matches plan gate). F2's "must have 0 FAILED/0 ERROR" satisfied.
- **No product files touched**; only the two evidence logs + this notepad entry.
- **F3 dependency**: F2's green checks say nothing about live Fusion behavior —
  the mcp==2.0.0 incompatibility note from T1/T2/T4 still stands for F3's live
  QA probe (pin `mcp<2.0.0` or migrate to the 2.x API before F3).

## F1 — Plan compliance audit: APPROVE (2026-08-10)

Independent read-only audit of all 6 fixes + scope. Evidence: `.omo/evidence/fix-F1-audit.log`.

**Verdict: APPROVE — 12/12 checks PASS.** All acceptance greps verified with exact line
numbers (a) `_call` default=60 at L297; (b) timeout=120 exactly 4: L489/1576/1688/1974;
(c) timeout=30 exactly 1 at L1350 (capture_screenshot, untouched); (d) parallel warning at
L2014/2016 + vision-free note at L2018; (e) vision-capable 4 matches (fusion_server L2183/
L2316, workflow_guide L81/L139); (f) getPointAtParameter 5 matches all in _compare_mesh_brep
region (helpers L2452/2473, fallback branch L2545); (g) exec scope-fix at FusionMCP.py
L591-592 (`local_vars["__builtins__"] = __builtins__` + `exec(code, local_vars, local_vars)`);
(h) fix-T7-slice-research.log present + diff shows Fix A (clamp on-plane dists to 0.0) and
Fix B (append reps[cur] first-hop) landed; (i) git diff --stat = only 5 in-scope files +
pre-existing mesh-graph-pipeline.md (flagged, excluded); (j) py_compile exit 0; (k) "1350"
count in fusion_server.py diff = 0; (l) diff def/@mcp/add_tool review = only 4 helper
additions (_surface_grid_n/_cap_grid_ns/_evaluator_uv_bounds/_sample_brep_surface_points) +
_call default change; no public tool signature changes, no new tools.

**Auditor's line-number corrections vs plan** (for future plans): all plan refs held
(L297/489/1576/1688/1974, L2013-2016, L2183, L2316, L2018) — T1-T7 zero drift.
Notepad T1's earlier note about a 4th extract_mesh_data site (L2103, annotate sync helper)
confirmed untouched (still 60s default). tests/test_mesh_slicer.py confirmed at 18 tests.

**F3 action item**: exclude `.omo/plans/mesh-graph-pipeline.md` from the atomic commit
(`git add` only FusionMCP.py + mcp_server/*.py × 3 + tests/test_mesh_slicer.py +
.omo/evidence/fix-*). Do NOT commit .omo/plans/mesh-graph-pipeline.md or any other
untracked plan files.
## F1/F2 — Orchestrator manual confirmation + F3 blocker discovered (2026-08-10)

- F1: boulder reviewer session (opencode:ses_01747764cffe5l7xvjU6j00oZK) hit poll
  inactivity timeout (1800000ms) on the re-run. Orchestrator MANUALLY confirmed the
  verdict per boulder option 1: audit log .omo/evidence/fix-F1-audit.log = 12/12 PASS,
  APPROVE. Independent re-grep by orchestrator confirmed: _call default=60 (L297),
  timeout=120 exactly 4, timeout=30 exactly 1, vision-capable 4 matches,
  FusionMCP.py L591-592 scope fix. Checkbox F1 marked [x].
- F2: orchestrator ran py -m pytest tests/ -q myself -> 248 passed, 1 xfailed,
  0 FAILED, 0 ERROR (58.70s, exit 0). Matches F2 verifier claim. Checkbox F2 marked [x].
- Plan state: 9/10 checkboxes [x]. Only F3 remains (L122).
- **F3 BLOCKER (PROVEN)**: live execute_script probe via the running bridge
  (fusion360_execute_script with nested def g()->f() payload) returned:
  NameError: name 'f' is not defined, traceback pointing at FusionMCP.py line 591
  exec(code, {"__builtins__": __builtins__}, local_vars) -- i.e. the OLD code.
  The add-in process inside Fusion 360 still has the pre-T5 (and pre-T3) FusionMCP.py
  loaded. The fix exists on disk (L591-592) but is NOT live in the running add-in.
  => USER ACTION REQUIRED before F3: reload the FusionMCP add-in in Fusion 360
  (Utilities -> Add-Ins -> FusionMCP -> Stop, then Run; or restart Fusion 360).
  Also the MCP server process serving the opencode fusion360 tools runs the OLD
  fusion_server.py (started before the edits) -- for F3 live probes, launch a fresh
  python3.13 fusion_server.py (or import fresh + POST to :7432) so timeout changes
  are active server-side too. The add-in reload is the critical one (it hosts the
  compare/exec logic).
- Scene verified still intact: Cuerpo76 (idx 0, 36 faces) + Cuerpo78 (idx 1, 46 faces)
  visible; bridge running on :7432.

## F3 — BLOCKED (user must reload Fusion add-in) (2026-08-10)

**Blocker (proven, not assumed):** F3's three live probes (compare_mesh_to_brep
max <= 1.5, execute_script nested functions -> "7", structure_graph no timeout)
all run inside the Fusion 360 add-in process on port 7432. That process is
executing the OLD in-memory FusionMCP.py:

- Fusion360.exe PID 13532 started 31/07/2026 20:59:19 -- days BEFORE the T1-T7
  edits and BEFORE the AddIns file sync (10/08/2026 0:25:04). The add-in module
  was loaded at process start and never reloaded.
- execute_script probe (nested def g()->f()) STILL returns
  "NameError: name 'f' is not defined".
- Decisive detail: the traceback reports the exec frame at LINE 591. In the OLD
  code object the exec(...) call is at L591; in the NEW code it moved to L592
  (L591 is now the __builtins__ merge). Python's traceback module reads the
  source TEXT from disk at format time (so the displayed line shows the new
  file's L591 text), but the LINE NUMBER comes from the running code object.
  Line 591 in the traceback == OLD code object still executing.
- importlib.reload is NOT a viable self-unblock: introspection probe returned []
  (add-in module not reachable via sys.modules from the exec sandbox), and
  re-running run() would not re-import the file from disk anyway.

**What was done before hitting the blocker:**
- Synced the fixed repo FusionMCP.py into the AddIns deployment folder:
  %APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\FusionMCP\FusionMCP.py
  (SHA256 now MATCHES repo: 77C6BE2E...; backup of the old deployed file saved
  as FusionMCP.py.bak-pre-fix in the same folder).
- So the file on disk is correct; the RUNNING process is stale.

**User action required to unblock (one of):**
  A) Fusion 360: Utilities -> Add-Ins -> FusionMCP -> Stop, then Run (keep doc open).
  B) Restart Fusion 360 and reopen the demo document (MeshBody2 idx 1 / Cuerpo78 idx 1).
After reload, orchestrator will verify with a quick execute_script probe (nested
functions must return "7"), then dispatch F3: live probes + atomic commit on master
(5 in-scope files + .omo/evidence/fix-*, EXCLUDING .omo/plans/mesh-graph-pipeline.md).

**Plan checkbox:** F3 marked - [~] (blocked) per boulder directive.

## F3 probe data point — structure_graph via fresh server vs stale add-in (2026-08-10)

- Fresh python3.13 process importing the NEW fusion_server.py (timeout=120
  confirmed in structure_graph source). duckdb 1.5.5 present (DUCKDB_OK).
- PING (_call get_bodies_info) -> 0.1s OK, scene intact (Cuerpo76/78).
- structure_graph(mesh="1") -> HUNG >300s with NO result/exception printed.
  requests timeout=120 did NOT fire, so either extract_mesh_data never
  completed on the stale add-in or the graph-build phase blocked silently.
- Add-in run() uses single-threaded HTTPServer (FusionMCP.py L3196) + custom
  event on the Fusion main thread: any long/blocked command serializes the
  bridge. The stale in-memory add-in is the prime suspect; the mesh itself
  was fine in the demo (compare + structure_graph ran on it then).
- ACTION for F3: AFTER the add-in reload, re-run structure_graph(mesh="1")
  with a hard wall-clock cap (e.g. 150s). If it STILL hangs with the NEW
  add-in, that is a NEW finding (not covered by T1) -> investigate the
  DuckDB persist step / extract_mesh_data payload size, per plan F3
  Failure QA (async job mode is the documented escalation).

## F3 � DuckDB persist bottleneck fixed: executemany -> CSV read_csv round-trip (2026-08-10)

**Changed**: `mcp_server/mesh_graph.py` `_create_table` ONLY (L772-797 region; local imports of csv/os/tempfile inside the function so no other code changed).

**The slow thing**: `conn.executemany("INSERT INTO ... VALUES (?,...)")`. DuckDB's per-parameter Python->C++ binding costs ~0.35ms; the real graph has ~770K params (55,080 edges x 14 cols + 1,160 nodes x 22 cols) -> ~270s no matter the batching. Measured: nodes 16.5s + edges 219.1s = 237s persist.

**Benchmark (same box, duckdb 1.5.5, synthetic 55,080-edge/14-col + 1,160-node/22-col rows, values pre-normalized exactly like `_sql_value`):**
- OLD executemany N=2000 baseline: 7.7-7.9 ms/row -> extrapolates to ~424-432s @ 55K (matches real 219s)
- literal VALUES chunk=500: 4.65s (0.084 ms/row, 90x faster)
- literal VALUES chunk=2000/5000/11000/single: 4.9-5.5s (0.09-0.10 ms/row) � chunk size barely matters
- **CSV read_csv round-trip: 282ms (0.005 ms/row) � 16x faster than literal VALUES, ~1500x faster than old**

**Chosen strategy**: temp-CSV + `read_csv`. `INSERT INTO t SELECT * FROM read_csv('path', columns={name: 'VARCHAR'|'BOOLEAN'|'INTEGER'|'DOUBLE'}, header=false, nullstr=E'\x01')`. NULL sentinel is `\x01` (control char; impossible in real attr strings � ids are ASCII, json.dumps escapes control chars). `E'\x01'` escape-string syntax is REQUIRED: duckdb does not process `\u` escapes in standard SQL string literals (passing `nullstr='\u0001'` literally broke INTEGER conversion). Empty string '' survives as '' (not NULL) with the explicit nullstr. File written with `encoding="utf-8"` (Windows default cp1252 would corrupt non-ASCII). Temp file removed in `finally`. `if normalized:` guard + transaction + LRU all unchanged.

**Final live timing proof (real mesh "1", 14,646 nodes / 14,814 indices):**
extract 0.3s + decompose 15.1s + build 39.3s + **persist 0.6s** = 55.3s total vs previous 294s. F3 gate (150s cap) passes with ~95s margin. Row counts verified: 1,160 nodes / 55,080 edges in DB == graph.

**Verification**: py_compile exit 0; tests/test_mesh_graph_db.py + tests/test_mesh_graph.py = 26 passed; tests/test_structure_graph_tool.py = 19 passed; full suite = 248 passed, 1 xfailed (identical to pre-change baseline).

## 2026-08-10 F3: structure_graph timeout root cause + fix (critical)
- Live structure_graph(mesh="1") probe timed out (MCP -32001) while compare + exec probes PASSED.
- ROOT CAUSE (server-side, in mcp_server/mesh_graph.py _create_table): duckdb conn.executemany
  does per-row parameterized INSERTs; measured real-data cost: persist = 237.3s of the 294s
  total chain (nodes 1160 rows 16.5s + edges 55080 rows 219.1s). Exceeds 150s F3 cap.
- Why probes 1+2 passed: those fixes are ADD-IN side (FusionMCP.py hot-patched live); only
  structure_graph persist is SERVER-side.
- FIX (landed): temp-CSV + read_csv bulk insert, zero-parameter. NULL sentinel \x01 +
  nullstr=E'\x01'; TEXT->VARCHAR in read_csv columns struct; forward-slash path; temp file
  removed in finally; empty guard retained. py_compile OK, 45/45 pytest pass.
- REAL-DATA PROOF: persist 237.3s -> 0.7s; total chain 58.0s < 150s; row counts exact
  (nodes 1160/1160, edges 55080/55080). cmd: py ...\Temp\opencode\live_persist_probe.py
- LIVE BLOCKER: MCP server PID 26648 (fusion_server.py, created 2026-08-07 19:45:45, child of
  this opencode session) started BEFORE the fix (mesh_graph.py mtime 2026-08-10 03:04) and
  caches the old module. Server process must restart to load the fix; live probe re-run after.
- GOTCHA: multiple stale fusion_server.py processes may exist (Aug 6-7); the serving one is
  the child of the current opencode host (cmd 34664 <- opencode 9220, ses_046722e53fferGQ5c1ZKxNJ377).
- duckdb 1.5.5: pyarrow/pandas MISSING on this box -> no read_csv auto-inference fallback; the
  explicit columns= struct is required. executemany is a trap on this data size.

## 2026-08-10 F3 COMPLETE: all probes PASS + atomic commit landed
- MCP server restarted by user (PID 23360, created 08-10 09:13:59, after fix).
- structure_graph(mesh="1") live probe PASSED: component_count 1, face_count 992,
  10 edge relation types, base face:0->face:4, articulation [face:4, face:5],
  dag_has_cycles false, DuckDB schema persisted (nodes 22 cols / edges 14 cols).
- Atomic commit landed: d7acf5a "fix: foundational tool rough edges from live
  reconstruction demo" - exactly 19 paths (6 code/test + 13 fix-* logs),
  1031 insertions / 20 deletions, on master, no amend/force/branch.
- Plan fix-foundational-tool-rough-edges: 10/10 [x]. F1 12/12, F2 7/7, F3 3/3.
- git status clean for plan scope (only pre-existing .omo/plans/mesh-graph-
  pipeline.md mod from a different plan + untracked .omo tooling remain).
- KEY LESSON: server-side Python module fixes require an MCP server restart to
  go live; add-in-side fixes can be hot-patched (FusionMCP.py). Diagnose by
  comparing fusion_server.py process CreationDate vs file mtime.
