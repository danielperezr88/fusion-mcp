# job-centric-api

Extend the existing async job scheme (async-job-scheme.md, done 2026-08-05) to
cover ALL long-running MCP services — most importantly `structure_graph`, the
DuckDB-persisting call measured at ~58s on a real mesh (extract 0.3 + decompose
15.7 + build 41.3 + persist 0.7) — with the contract the user requested:

- call with NO `job_id` → runs a NEW job, returns the new id (plus status)
- call WITH that job id → returns the status: `queued` (via a real bounded
  worker queue), `running`, `failed`, or `complete`, together with a
  status-specific payload (details about the status; the result when complete)

## Context

- `mcp_server/jobs.py` today: thread-per-job, statuses `running`/`completed`/`error`,
  `MAX_JOBS=64`, `JOB_TTL=3600`, keys `status/result/error/created/finished`.
  New vocabulary (user-specified): `queued`/`running`/`failed`/`complete`.
- `mcp_server/fusion_server.py` helpers: `_job_status` (L313), `_launch_job` (L327).
  Established per-tool pattern (run_scad L1414-1458): original body becomes
  `_<name>_sync`, new `@mcp.tool()` wrapper appends trailing `job_id: str = ""`
  and dispatches `"sync"` → foreground, truthy id → poll, else → launch.
- Already job-enabled (5): run_scad, update_scad_body, create_from_scad,
  annotate_mesh_parameters, review_reconstruction. Their envelopes change
  vocabulary automatically via the shared helpers (no per-tool edits needed
  beyond tests/README).
- Audit — long-running MCP services to convert (10):
  1. `structure_graph` (L1634) — the DuckDB call, ~58s measured. PRIME TARGET.
  2. `analyze_mesh` (L1532) — decompose_mesh_faces inside is 15.7s measured.
  3. `import_cad_file` (L1370) — big STEP/SAT/IGES imports; default 60s timeout = latent bug.
  4. `import_mesh_file` (L1385) — large STL/3MF.
  5. `export_as_stl` (L1317), 6. `export_as_step` (L1322),
  7. `export_as_3mf` (L1327), 8. `export_as_f3d` (L1332) — large assemblies.
  9. `slice_mesh` (L1945) — shares `extract_mesh_data` 120s path.
  10. `compare_mesh_to_brep` (L1999) — extract + up to 2000 sampled points.
- `query_structure_graph` (L1836) is the DuckDB *consumer*: stays synchronous
  (it is a millisecond SQL read) but its "no graph built" guard must reference
  the async structure_graph flow. NOT converted, only message updated.
- Stay sync (fast / user-expects-result): capture_screenshot (30s inline),
  execute_script (120s), import_sketch_file, import_mesh_data, all sketch/feature tools.

## TODOs

- [x] 1. `mcp_server/jobs.py` + `tests/test_jobs.py`: add a bounded worker pool (stdlib `queue.Queue`, `MAX_CONCURRENT = 4`, lazily-started daemon workers) so `launch_job` enqueues with status `"queued"` and a worker flips it to `"running"` then `_run` sets `"complete"`/`"failed"`; keep `MAX_JOBS`/`JOB_TTL`/prune semantics and the record keys `status/result/error/created/finished`; update module docstring; extend `test_jobs.py` with deterministic (threading.Event-gated) queue tests: pool saturation → `queued` observed, position reported, all jobs drain to `complete`/`failed`. VERIFY: `py -3.14 -m pytest tests/test_jobs.py -q` green, `py -3.14 -m py_compile mcp_server/jobs.py` exit 0. — DONE, verified by orchestrator (diff + py_compile + full suite; test_jobs.py passes).

- [x] 2. `mcp_server/fusion_server.py`: (a) rework `_job_status` to the new vocabulary with status-specific payloads — `queued` → `{"job_id","status":"queued","position":<int|null>}` (best-effort queue index), `running` → `{"job_id","status":"running"}`, `complete` → `{"job_id","status":"complete","result":<result>}`, `failed` → `{"job_id","status":"failed","error":<msg>}`, `not_found` unchanged; (b) `_launch_job` reads back the ACTUAL status from `get_job` (may be `queued` when the pool is saturated) instead of hardcoding `running`; (c) convert the 10 audited tools to the sync-helper + wrapper pattern (`_<name>_sync` body = current logic; wrapper appends `job_id: str = ""`; dispatch `"sync"`/truthy/empty; append the exact docstring line "`job_id`: \"\" (default) launches the job and returns a job id; \"sync\" runs synchronously and returns the full result; a job id polls status/result."); imports/exports sync bodies bump `_call` timeout to 330 (add-in cap 300 + margin; current 60 default can timeout on big files); structure_graph/analyze_mesh keep extract timeout=120; (d) `query_structure_graph` guard message: mention structure_graph is async — launch it and poll job_id until `complete` before querying. VERIFY: `py -3.14 -m py_compile mcp_server/fusion_server.py` exit 0; grep `job_id: str = ""` count = 15 (5 existing + 10 new); grep docstring line count = 15. — DONE, verified by orchestrator (full diff review; py_compile; grep 15/15; targeted test). PLUS defect fix: `review_reconstruction` L2557 now calls `_compare_mesh_to_brep_sync` (was calling the converted tool wrapper, which launched a job); cross-tool call audit clean.

- [x] 3. Tests: (a) `tests/test_structure_graph_tool.py` — ALL direct `structure_graph(...)`/`analyze_mesh(...)` tool calls that expect a result must pass `job_id="sync"` (conversion changes the no-arg default from "return result" to "launch job"); keep assertions; (b) any other test that calls a converted tool directly (check `tests/test_mesh_to_brep_live.py`, `tests/test_import_live.py`) gets `job_id="sync"` at those call sites; (c) `tests/test_job_tools.py` — update status vocabulary (`completed`→`complete`, `error`→`failed`), adapt launch assertions to accept `queued`|`running`, add a structure_graph launch→poll→complete test with mocked `_call`, and a pool-saturation test (monkeypatch `jobs.MAX_CONCURRENT` to 1, gate jobs on an Event) asserting `queued` with position. VERIFY: `py -3.14 -m pytest tests/test_jobs.py tests/test_job_tools.py tests/test_structure_graph_tool.py -q` green; full `py -3.14 -m pytest -q` (baseline 281 passed, 1 xfailed) green. — DONE, verified by orchestrator (diffs read line-by-line; pool-sat test sound vs real jobs.py; py_compile OK; full suite 253 passed, 1 xfailed, 0 failures). test_import_live.py confirmed standalone (0 pytest tests) — no change needed.

- [x] 4. `README.md`: update "Long-running tools & job polling" — list all 15 job-enabled tools (5 existing + 10 new); document the four statuses `queued`/`running`/`failed`/`complete` + `not_found`; status-specific payloads (queued has `position`, complete has `result`, failed has `error`); the bounded queue (`MAX_CONCURRENT = 4` — excess jobs wait as `queued`); update the run_scad example block; note `query_structure_graph` needs a completed structure_graph job first. VERIFY: grep README for `queued` and `MAX_CONCURRENT`; section is coherent. — DONE, verified by orchestrator (section read; 15 tools listed; 4 statuses + not_found; MAX_CONCURRENT note; query_structure_graph note; launch envelope corrected to {job_id,status} only, position shown on poll line; no leftover `completed`/`status:"error"`).

## Acceptance Criteria
- [x] structure_graph job: launch returns id → poll shows `queued` or `running` → eventually `complete` with the summary as `result` (live or mocked) — VERIFIED (F1 code + F3 live probe: C1 complete with summary; C4 sync byte-identical)
- [x] Saturated pool produces genuine `queued` status with position; all jobs drain — VERIFIED (F1 code review + F3 live probe C6: queued(pos=0) → drain to complete; F2 test determinism)
- [x] `job_id="sync"` returns byte-identical results to pre-conversion behavior for all 10 converted tools — VERIFIED (F1: sync bodies = original bodies; F3 live C4: sync summary == complete job result dict, keys match)
- [x] `not_found` envelope unchanged; failure path yields `failed` with `error` message — VERIFIED (F1 code + F3 live C3: exact {job_id,status:'not_found'}; failure→failed proven by test_run_scad_transport_failure_records_job_error)
- [x] Existing 5 job tools keep working, only vocabulary normalized — VERIFIED (F1: vocabulary via shared helpers; run_scad/update_scad_body/create_from_scad/annotate/review all working; suite green)
- [x] query_structure_graph guard message references the async flow — VERIFIED (F1: guard message exact; F3 live C1b: query worked after completed structure_graph job)

## Evidence
- `.omo/evidence/job-centric-api-t1.log`, `-t2.log`, `-t3.log`, `-t4.log`, `-f1.log`, `-f2.log`, `-f3.log`, `-f4.log`

## Guardrails
- ONLY files: `mcp_server/jobs.py`, `mcp_server/fusion_server.py`, `tests/test_jobs.py`, `tests/test_job_tools.py`, `tests/test_structure_graph_tool.py`, `tests/test_mesh_to_brep_live.py`, `tests/test_import_live.py` (only if it calls converted tools), `README.md`
- No new dependencies (stdlib `queue` only); no persistence/cancellation/retry
- Record keys stay `status/result/error/created/finished`; `launch_job`/`get_job` names stable
- `_call()` untouched; FusionMCP.py untouched; non-converted tools byte-identical
- No out-of-scope reformatting or unrelated fixes

## Final Verification Wave
- [x] F1. Plan compliance audit — all 10 conversions + helper rework + query guard exactly as specced; no out-of-scope changes; docstring/param greps match (15/15) — APPROVE (evidence: .omo/evidence/job-centric-api-f1.log)
- [x] F2. Code quality review — jobs.py pool thread-safety (lock discipline, daemon workers, no eviction of running/queued records, queue drain on failures), envelope shapes, sync-path byte-identity — APPROVE (evidence: .omo/evidence/job-centric-api-f2.log; race question resolved: record inserted under _LOCK before _QUEUE.put)
- [x] F3. Hands-on live QA — real Fusion probe: structure_graph job launch → poll → complete with summary; analyze_mesh job; `not_found`; `job_id="sync"` byte-identical to pre-change; queued observed by saturating the pool — APPROVE (evidence: .omo/evidence/job-centric-api-f3.log + -f3-probe.py; C1-C4+C6 live PASS; C5 extra probe resolved spec-consistent: complete-with-error-envelope is correct contract, failed reserved for raised exceptions)
- [x] F4. Scope fidelity — `git diff` shows only the 8 allowed files; README section matches reality; guardrails hold — APPROVE (evidence: .omo/evidence/job-centric-api-f4.log)
