# job-centric-api Learnings

Inherited wisdom for this plan. Append new entries below - never overwrite.

---

## [2026-08-10] Pre-work: inherited conventions (from async-job-scheme + mesh-graph-pipeline notepads)

- **jobs.py contract stability**: keep `launch_job(fn, *args, **kwargs) -> str` and
  `get_job(job_id) -> dict | None` names; keep record keys
  `status/result/error/created/finished`. `prune()` runs under `_LOCK` (caller holds it).
  Running records NEVER evicted; cap may transiently exceed MAX_JOBS.
- **Wrapper pattern (per tool)**: original body becomes `def _<name>_sync(<same sig>)`;
  new `@mcp.tool()` wrapper takes original args + trailing `job_id: str = ""` and does
  `if job_id == "sync": return _<name>_sync(...)` / `if job_id: return json.dumps(_job_status(job_id), indent=2)` /
  `return _launch_job(_<name>_sync, ...)`. Docstring appends the exact line:
  "`job_id`: \"\" (default) launches the job and returns a job id; \"sync\" runs
  synchronously and returns the full result; a job id polls status/result."
- **Lazy sibling imports**: `from jobs import get_job` / `from jobs import launch_job`
  INSIDE `_job_status`/`_launch_job` (script-dir import in prod, sys.path in tests).
- **Test determinism (critical)**: for LRU/cap/saturation tests, gate fast jobs on a
  `threading.Event` so they can't finish mid-launch; release after all launches;
  then `_wait_for` each. Blocked-on-Event jobs stay `running` deterministically.
- **Headless fusion_server loading in tests**: stub `mcp.server.fastmcp`
  (`_FastMCP`, `_Image`) in sys.modules, `importlib.util.spec_from_file_location`,
  exec with MCP_SERVER_DIR at sys.path[0] so lazy `from jobs import ...` resolves.
- **Command**: tests run with `py -3.14 -m pytest`; py_compile via `py -3.14 -m py_compile`.
- **Add-in cap 300s**: server heavy calls use timeout=330 (add-in responds first).
  `_call(command, params, timeout=60)` default; `_call` is L297-310, DO NOT modify.
- **query_structure_graph**: `_get_graph_db(mesh)` raises KeyError when no graph built
  (L1922-1926) -> current guard returns "no graph built for mesh 'X'. Call structure_graph first."
- **Structure graph measured times** (real mesh): extract 0.3s + decompose 15.7s +
  build 41.3s + persist 0.7s ~ 58s total. analyze_mesh: extract 0.3s + decompose 15.7s.
- **Live tests are collected and run headless** (mock `_call`/requests.post) - NOT skipped.
  Any test calling a converted tool directly must pass `job_id="sync"`.

---

## [2026-08-10] T2 findings: fusion_server.py job-centric conversion

- **Helpers reworked**: `_job_status` now maps `queued`->`{job_id,status,position}`
  (`record.get("position")`, int|None), `running`->`{job_id,status}`,
  `complete`->`{job_id,status,result}`, else->`{job_id,status:"failed",error}`;
  `not_found` envelope byte-identical. `_launch_job` reads back the ACTUAL status
  via `get_job(job_id)` after launch (returns `queued` when the pool is saturated,
  `running` otherwise) instead of hardcoding "running". Lazy sibling imports kept
  inside both helpers (`from jobs import launch_job, get_job`).
- **10 tools converted** to sync-helper + wrapper (mirror run_scad exactly):
  structure_graph, analyze_mesh, slice_mesh, compare_mesh_to_brep, import_cad_file,
  import_mesh_file, export_as_stl, export_as_step, export_as_3mf, export_as_f3d.
  Sync bodies byte-identical to pre-change EXCEPT import/export bump `_call(...)`
  timeout to 330; structure_graph/analyze_mesh keep extract timeout=120.
  Wrappers: original args + trailing `job_id: str = ""`, dispatch
  `"sync"`/truthy/empty, verbatim docstring line appended.
- **query_structure_graph** stays sync; guard message now says structure_graph is
  asynchronous: launch it, poll job_id until 'complete', then query.
- **VERIFY**: py_compile exit 0; grep `job_id: str = ""` = 15; grep docstring line = 15.
- **Test wave** (tests/test_structure_graph_tool.py + test_job_tools.py): 3 passed
  (sync path, not_found, empty-sql), 21 failed -- ALL failures are T3 scope:
  (a) direct structure_graph/analyze_mesh calls lack `job_id="sync"` so they get the
  launch envelope instead of results (cascade: all query_structure_graph tests that
  depend on `_run(fs)` fail with the new "structure_graph is asynchronous" guard);
  (b) test_job_tools asserts old launch status `"running"` but the pool now reports
  `"queued"` (observed in output -- T1 pool is live), and asserts `"completed"`/
  `"error"` poll vocab that is now `"complete"`/`"failed"`;
  (c) test_query_structure_graph_no_graph pins the OLD guard message and must be
  updated for the mandated async-flow message. No failure traces to a broken wrapper.
- **Headless smoke (mock _call, real pipeline)**: all 10 wrappers registered with
  `_<name>_sync`; export_as_stl/import_cad_file sync + timeout=330 confirmed;
  structure_graph sync runs the real decompose/build/persist and returns a summary
  (extract timeout=120 confirmed); analyze_mesh launch -> poll -> complete returns the
  real report as `result`; not_found envelope exact; job body raising yields
  `failed` + error message; pool saturation (MAX_CONCURRENT=1 + Event-gated jobs)
  yields `queued` with `position` key. Cleanup: smoke script deleted from temp.
- **jobs.py note**: T1's pool landed mid-T2 (git status shows jobs.py modified) --
  confirms `queued`/position contract consumed by the new helpers.

## [2026-08-10] Orchestrator verification: T1+T2 VERIFIED, defect fixed
- T1 jobs.py + test_jobs.py: full diff reviewed; bounded pool (queue.Queue, MAX_CONCURRENT=4, lazily-started daemon workers under _LOCK), vocab queued/running/complete/failed, position for queued, prune unchanged. test_jobs.py green in full suite. py_compile exit 0. Cosmetic only: missing trailing newline at EOF of jobs.py.
- T2 fusion_server.py: all 10 conversions verified in diff (_<name>_sync + wrapper, dispatch sync/truthy/empty, verbatim docstring line); _job_status/_launch_job reworked; query_structure_graph guard message updated. grep job_id = 15, docstring = 15. py_compile exit 0.
- DEFECT FOUND+FIXED: review_reconstruction sync body called the compare_mesh_to_brep TOOL (L2555) -> post-conversion that LAUNCHES a job (and in headless tests trips lazy 'from jobs import' -> No module named 'jobs'). Fixed to call _compare_mesh_to_brep_sync. test_review_reconstruction_server_path_happy now passes (real bridge). Cross-tool call audit: L2555 was the ONLY internal bare call to a converted tool.
- SUITE STATUS after T1+T2+fix: 229 passed, 1 xfailed, 22 failed -- ALL 22 are T3 scope (3 test_job_tools old-vocab asserts, 1 test_mesh_to_brep_live direct call, 18 test_structure_graph_tool direct calls + old guard pin). Count note: 252 tests collected vs plan's stale '281 baseline' (historical figure from async-job-scheme evidence; no test files deleted per git status). Gate = fully green suite after T3.

## [2026-08-10] T4: README job-polling section rewritten (task 4 complete)
- README "### Long-running tools & job polling" section (lines 92-121) rewritten; only that section touched.
- Opening now lists all 15 job-enabled tools; three job_id modes kept with new vocab.
- New status list documented: queued (position), running (job_id+status), complete (result), failed (error), not_found.
- Bounded queue documented: MAX_CONCURRENT = 4, excess jobs wait as queued; in-memory only, poll until complete/failed.
- Example block: queued(position 2) -> running -> complete; 300s add-in cap and base64 screenshot notes unchanged.
- Added note: query_structure_graph stays sync but needs a finished structure_graph job (launch -> poll -> complete -> query).
- VERIFY: Select-String queued = 4 hits, MAX_CONCURRENT = 1 hit, "completed" = 0, 'status": "error"' = 0. PASS.
- Evidence: .omo/evidence/job-centric-api-t4.log (appended).

## [2026-08-10] T3 findings: test-suite conversion to job-centric API (22 failures -> 0)
- 18x tests/test_structure_graph_tool.py: direct structure_graph/analyze_mesh calls now need
  job_id='sync' (no-arg default launches). Fixed in _run(fs) helper + 2 inline preset tests.
  test_query_structure_graph_no_graph guard message updated to the exact async-flow text
  (structure_graph is asynchronous: launch ... then poll ... status 'complete').
- 3x tests/test_job_tools.py: _poll_tool now polls until status not in {queued,running};
  launch asserts accept {queued,running}; poll vocab completed->complete, error->failed.
- 1x tests/test_mesh_to_brep_live.py: test_compare_mesh_to_brep_server_path needs job_id='sync'
  at BOTH direct call sites (happy + error payload).
- test_import_live.py: NO change -- standalone __main__ script, not pytest-collected, drives
  FusionMCP.py handlers, never touches converted server tools.
- CRITICAL module-instance gotcha for pool tests: fusion_server._launch_job imports TOP-LEVEL
  'jobs' (mcp_server/jobs.py via MCP_SERVER_DIR on sys.path), which is a DIFFERENT module object
  from 'mcp_server.jobs' used by tests/test_jobs.py. To patch the real pool state use
  importlib.import_module('jobs'). Quarantine workers from earlier tests by replacing jobs._QUEUE
  with a fresh queue.Queue() + _WORKERS_STARTED=False + MAX_CONCURRENT=1 -> deterministic
  single-worker pool even after prior tests started 4 workers.
- New tests: test_structure_graph_launch_poll_complete (mocked _call, box payload -> summary in
  result: component_count=1, face_count=6); test_pool_saturation_queued_with_position
  (gate-wait in _call, launch1 running, launch2 queued position=0, gate.set -> both complete).
- SUITE: 31 passed on 3 target files; full suite 253 passed, 1 xfailed, 0 failures
  (252 baseline + 2 new tests = 254 collected). Gate = ZERO failures achieved.
## [2026-08-10] T4 follow-up: README example block accuracy fix
- Bug: launch envelope never carries position; _launch_job returns exactly {job_id, status} (reads back record["status"] only).
  position appears only in the poll envelope (_job_status queued -> {job_id, status, position: <int|null>}).
- Fixed 3-line example to 4 lines: launch -> {"job_id", "status": "queued"} (no position) | poll -> queued + position | poll -> running | poll -> complete.
- VERIFY: 'status": "queued", "position"' hits exactly the poll line; launch line has no position. Nothing else in README changed.

## [2026-08-10] F1 verdict: APPROVE (plan compliance audit)
- All 4 TODOs verified against ACTUAL code (not subagent claims). Per-claim evidence in .omo/evidence/job-centric-api-f1.log.
- T1 jobs.py: MAX_CONCURRENT=4 (L35), queue.Queue (L37), lazy daemon workers under _LOCK (L41-51, called from launch_job within with _LOCK L73-83), launch='queued'(L77) -> worker 'running'(L61) -> _run 'complete'/'failed'(L136/L129); MAX_JOBS=64, JOB_TTL=3600, keys status/result/error/created/finished retained; launch_job/get_job stable; docstring updated.
- T2 fusion_server.py: _job_status envelopes exact (queued->position via record.get, running, complete->result, else->failed->error, not_found unchanged); _launch_job reads back ACTUAL status (may be queued), returns {job_id,status} ONLY. 15/15 wrappers + verbatim docstring line. 6 import/export sync bodies timeout=330; structure_graph/analyze_mesh (and slice_mesh) extract timeout=120. query_structure_graph guard message byte-exact. review_reconstruction calls _compare_mesh_to_brep_sync (L2557).
- T3: _run helper + 2 inline preset tests use job_id='sync'; test_mesh_to_brep_live both call sites job_id='sync'; test_job_tools new vocab + 2 new tests (launch_poll_complete, pool_saturation_queued_with_position w/ MAX_CONCURRENT=1 + Event gate); test_jobs 3 new Event-gated pool tests.
- T4 README: single hunk @@ -91,24 +91,35 @@; 15 tools; 4 statuses+not_found; MAX_CONCURRENT=4; run_scad example launch={job_id,status} no position, position only on poll line; query_structure_graph note.
- Greps: job_id=15, docstring=15, status.*queued present, no "status":"completed"/"status":"error" leftovers ('completed' only as prose + one test NAME, not a status value). py_compile exit 0. Scope: only 8 permitted files (mesh-graph-pipeline.md pre-existing, unrelated).

## F4 Scope fidelity (final verification wave) - 2026-08-10
- VERDICT: APPROVE. All 8 modified files inside allowed set; mesh-graph-pipeline.md
  diff confirmed pre-existing and job-vocabulary-free (tolerance/plane-fit content).
- 15/15 job_id tools verified by multi-line-aware scan (10 new conversions + 5 pre-existing);
  capture_screenshot + query_structure_graph correctly stay sync.
- _call L297-310 byte-identical (first diff hunk at L316). FusionMCP.py + requirements.txt: no diff.
- _review_reconstruction_sync had to switch its internal compare call to
  _compare_mesh_to_brep_sync -- a REQUIRED follow-through of the conversion, not scope creep.
- Cosmetic only: jobs.py and test_jobs.py lost trailing newline at EOF.

## [2026-08-10] FINAL VERIFICATION WAVE COMPLETE - 4/4 APPROVE - PLAN CLOSED
- F1 APPROVE (compliance): 15/15 wrappers+docstrings, envelopes byte-exact, timeouts 330/120, guard message exact, review_reconstruction calls sync helper, scope clean, suite 253/1/0. Log: f1.
- F2 APPROVE (code quality): _JOBS mutations all under _LOCK; record inserted before _QUEUE.put -> no launch race; workers run user code outside lock; prune never evicts queued/running; envelopes JSON-safe; sync bodies byte-identical; Event-gated tests deterministic. Log: f2.
- F3 APPROVE (live QA): C1 structure_graph launch->poll->complete + DuckDB queryable; C2 analyze_mesh; C3 not_found exact; C4 sync byte-identical to job result; C5 extra probe RESOLVED - analyze_mesh(mesh=999) is complete-with-error-envelope (normal return), NOT failed (failed = raised exception only, proven by transport-failure unit test); C6 queued(pos=0)->drain. Probe: f3-probe.py. Log: f3.
- F4 APPROVE (scope): only 8 allowed files; _call/FusionMCP.py untouched; README matches reality. Log: f4.
- GOTCHA: F3 subagent's chat claim (APPROVE) initially contradicted its own f3.log (REJECT from first probe run) - orchestrator reconciled by appending the post-resolution verdict to f3.log, preserving raw observations.
- GOTCHA: earlier plan-checkbox edits did NOT persist to disk (edit tool writes are authoritative; bash Set-Content in a prior pass was lost) - re-marked via Edit tool, verified OPEN:0 CLOSED:14 (4 TODOs + 6 AC + 4 F-wave).
