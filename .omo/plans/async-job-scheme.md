# async-job-scheme - Work Plan

## TL;DR (For humans)
<!-- Fill this LAST, after the detailed plan below is written, so it summarizes the REAL plan. -->
<!-- Plain English for a non-engineer: NO file paths, NO todo numbers, NO wave/agent/tool names. -->

**What you'll get:** Seven long-running Fusion tools (OpenSCAD rendering, mesh reconstruction, SCAD-to-features translation, multi-view screenshot capture) stop blocking the caller: a call with no job id now starts the work in the background and immediately returns a job id; calling again with that id reports the status and, once finished, the result. A `job_id="sync"` escape keeps the old blocking behavior for scripts that want it. The Fusion add-in's hidden 30-second cap is raised to 300 seconds so background jobs that legitimately take minutes no longer fail with a bogus timeout.

**Why this approach:** Exactly the contract you specified in the design notes — "a call with no job id launches a job and returns a job id, and a call with a job id returns a status and/or a result." It fixes the real failure mode discovered in the previous plan: the server already allowed 330s for heavy calls, but the add-in killed every command after 30s (`event.wait(timeout=30)`), so long operations always failed no matter what the server allowed.

**What it will NOT do:** It will not change any other tool's behavior (everything else stays byte-identical). It will not touch the OpenSCAD translator at all (hard guardrail carried over from the mesh plan). It will not add dependencies (stdlib threading only), will not persist jobs across server restarts, and will not add job cancellation (a future workstream).

**Effort:** Medium
**Risk:** Medium - changes the default return shape of 7 tools (launch-vs-result); existing tests get a mechanical `job_id="sync"` update; add-in change requires a Fusion add-in reload before live QA.
**Decisions to sanity-check:** (1) async-by-default for the 7 job-enabled tools per your literal directive; the `job_id="sync"` sentinel preserves the old blocking behavior; (2) job store is in-memory only (ephemeral, like the structure-graph DuckDB); (3) add-in cap raised 30s → 300s constant, matching the server's 330s ceiling.

Your next move: this implements your standing directive from the mesh plan's design notes. Execution starts immediately (wave 1 dispatches as soon as this plan lands). Flag any decision above you want changed and it is a small adjustment.

---

> TL;DR (machine): Medium effort, Medium risk. New stdlib `mcp_server/jobs.py` (threaded, bounded, TTL'd job registry) + `job_id` param (launch/poll/`"sync"`) on 7 timeout-prone tools in fusion_server.py + add-in `ADDIN_CMD_TIMEOUT=300` in FusionMCP.py + headless tests + live QA. Additive elsewhere; scad_translator.py untouched.

## Scope
### Must have
- `mcp_server/jobs.py`: thread-safe in-memory job registry. `launch_job(fn, *args, **kwargs) -> str` (spawns daemon thread, returns job id), `get_job(job_id) -> dict | None`, `prune()`. Bounded at `MAX_JOBS = 64` (LRU eviction, running jobs NEVER evicted), TTL `JOB_TTL = 3600.0` s. Statuses: `running`, `completed` (with `result`), `error` (with `error` message); unknown id → None (mapped to `not_found` at the tool layer). Stdlib only: `threading`, `uuid`, `time`.
- 7 job-enabled tools in `mcp_server/fusion_server.py`, each gains a trailing `job_id: str = ""` parameter with THREE modes:
  - `job_id == ""` (default): **launch** — run the existing body in a background thread, return immediately `{"job_id": "<hex>", "status": "running"}` (json.dumps, indent=2).
  - `job_id == "sync"`: **synchronous** — run the existing body inline, return exactly what the tool returned before this change (byte-identical output).
  - `job_id == "<uuid>"`: **poll** — return `{"job_id": ..., "status": "running"}` | `{"job_id": ..., "status": "completed", "result": <result>}` | `{"job_id": ..., "status": "error", "error": "<msg>"}` | `{"job_id": ..., "status": "not_found"}` (json.dumps, indent=2).
  - Tools: `run_scad`, `update_scad_body`, `reconstruct_mesh`, `reconstruct_from_faces`, `create_from_scad`, `annotate_mesh_parameters`, `review_reconstruction`.
- `FusionMCP.py`: new module constant `ADDIN_CMD_TIMEOUT = 300`; `event.wait(timeout=30)` (L67) → `event.wait(timeout=ADDIN_CMD_TIMEOUT)`; timeout message (L68) → f-string using the constant. Nothing else in the add-in changes (queue/event bridge and all command handlers untouched).
- Tests: `tests/test_jobs.py` (registry unit tests) + `tests/test_job_tools.py` (headless tool-level tests with a mocked `_call`/`requests.post`). All pre-existing tests that call the 7 tools synchronously get `job_id="sync"` added (mechanical).
- README: new "Long-running tools & job polling" section + docstring updates on the 7 tools.
- Additive contract: no removed keys, no signature changes to the other ~70 tools, `_call()` itself unchanged (default 30s stays; heavy sites keep their explicit timeouts).

### Must NOT have (guardrails, anti-slop, scope boundaries)
- `mcp_server/scad_translator.py` MUST stay untouched (zero diff — hard guardrail from the mesh plan).
- No new dependencies in `mcp_server/requirements.txt` (stdlib only). No changes to `bundle.py` download timeouts.
- Do NOT change `_call()`'s implementation, default timeout, or error strings.
- Do NOT job-enable `capture_screenshot` (standard 30s, not timeout-prone) or the default-30s mesh tools (`analyze_mesh`, `structure_graph`, `slice_mesh`, `compare_mesh_to_brep`) — out of scope; note as future work only.
- No job cancellation, no job persistence across restart, no job re-execution on collision (uuid4 hex).
- Do NOT restructure the add-in's queue/event/threading architecture (Fusion API is main-thread-bound) — only the wait constant + message change.
- Do NOT change the async result shape of the 2 image tools (`annotate_mesh_parameters`, `review_reconstruction`): their job `result` must be JSON-safe (no `Image` objects — base64 strings only) while their `sync` path keeps returning the current `list` with `Image` blocks.
- No subagent git commits (Atlas commits once after the Final Wave passes).

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: TDD for `jobs.py` (tests written with the module); tests-after for the tool wrappers (existing tests updated). Framework: pytest, run with `py -3.14 -m pytest`.
- Evidence: `.omo/evidence/async-job-scheme-task-<N>.log` (attemptDir = `.omo/evidence/` per repo convention).

## Execution strategy
### Parallel execution waves
- Wave 1 (parallel): Todo 1 (`jobs.py` + unit tests), Todo 2 (`FusionMCP.py` cap). Independent files.
- Wave 2 (sequential, depends on 1): Todo 3 (tool refactor + `tests/test_job_tools.py` + update existing tests). Needs `jobs.py` to exist (imports it) and edits `fusion_server.py` alone.
- Wave 3 (sequential, depends on 3): Todo 4 (README + docstrings). Docstrings live in the same functions Todo 3 refactors.
- Final wave (parallel): F1–F4.

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| 1 jobs.py + tests | — | 3 | 2 |
| 2 FusionMCP.py cap | — | — | 1 |
| 3 tool refactor + tests | 1 | 4 | — |
| 4 README + docstrings | 3 | Final wave | — |

## Todos
> Implementation + Test = ONE todo. Never separate.
<!-- APPEND TASK BATCHES BELOW THIS LINE WITH edit/apply_patch - never rewrite the headers above. -->
- [x] 1. `mcp_server/jobs.py`: thread-safe bounded job registry + `tests/test_jobs.py`
  What to do / Must NOT do: Create `mcp_server/jobs.py` with module-level `_JOBS: dict[str, dict]`, `_LOCK = threading.Lock()`, `MAX_JOBS = 64`, `JOB_TTL = 3600.0`. Functions: `launch_job(fn, *args, **kwargs) -> str` (job_id = `uuid.uuid4().hex`; under lock: call `prune()`, insert `{"status": "running", "result": None, "error": None, "created": time.time(), "finished": None}`, then spawn `threading.Thread(target=_run, args=(job_id, fn, args, kwargs), daemon=True).start()`; return job_id), `get_job(job_id) -> dict | None` (under lock, return a shallow copy of the record or None), `_run(job_id, fn, args, kwargs)` (try: result = fn(*args, **kwargs), under lock set status="completed", result=result, finished=time.time(); except Exception as e: under lock set status="error", error=str(e), finished=time.time()), `prune()` (under lock: drop records with `finished` set and `time.time() - finished > JOB_TTL`; while count of non-running records + running count > MAX_JOBS... eviction: drop oldest `finished` records (completed/error) first by `finished` timestamp; NEVER evict running). Do NOT add cancellation, persistence, or retry logic. No new imports beyond stdlib. Must NOT touch any other file.
  Parallelization: Wave 1 | Blocked by: — | Blocks: 3
  References (executor has NO interview context - be exhaustive): this todo's spec above; existing repo conventions: sibling imports in fusion_server.py are lazy (see `_load_mesh_analysis` pattern), tests live in `tests/`, evidence logs to `.omo/evidence/`. Run tests with `py -3.14 -m pytest`.
  Acceptance criteria (agent-executable): `py -3.14 -m pytest tests/test_jobs.py -q` green. `tests/test_jobs.py` covers: launch returns unique id and record is `running`; a slow fn (e.g. `time.sleep(0.05)`) transitions to `completed` with the correct result; fn raising → `error` with message; `get_job("deadbeef")` → None; TTL expiry (monkeypatch `jobs.time.time` or set `JOB_TTL` small and fabricate `finished` timestamps) → pruned; LRU: launch 70 jobs where 66 complete quickly and 4 stay running → `_JOBS` count ≤ 64 and all running ids still present; concurrency: 20 threads launch simultaneously → 20 unique ids, all eventually `completed`.
  QA scenarios (name the exact tool + invocation): happy — `launch_job(lambda: 42)` then poll `get_job` until completed, assert result 42; failure — `launch_job(lambda: 1/0)`, assert status "error" and "division by zero" in message; boundary — eviction/expiry/concurrency tests above. Evidence `.omo/evidence/async-job-scheme-task-1.log` (capture the pytest run output).
  Commit: N (Atlas commits after Final Wave)
- [x] 2. `FusionMCP.py`: raise add-in command cap 30s → `ADDIN_CMD_TIMEOUT = 300`
  What to do / Must NOT do: Add module constant `ADDIN_CMD_TIMEOUT = 300` next to `PORT = 7432` (L34). At L67 change `event.wait(timeout=30)` → `event.wait(timeout=ADDIN_CMD_TIMEOUT)`. At L68 change the timeout result to use the constant: `_results.pop(cmd_id, {"error": f"Timeout - Fusion did not respond in {ADDIN_CMD_TIMEOUT}s"})`. Change NOTHING else in FusionMCP.py — no queue/event/threading restructure, no handler edits, no signature changes. Rationale for 300 (not higher): server heavy calls use `timeout=330`, so the add-in now always responds (result or its own timeout error) before the server's socket timeout kills the connection.
  Parallelization: Wave 1 | Blocked by: — | Blocks: —
  References (executor has NO interview context - be exhaustive): `C:\Users\danie\fusion-mcp\FusionMCP.py` L33-34 (constants), L59-70 (`_dispatch`, the `event.wait(timeout=30)` at L67 and timeout fallback at L68).
  Acceptance criteria (agent-executable): the three lines above changed exactly as specified; `git diff -- FusionMCP.py` shows ONLY those changes; file still parses (`py -3.14 -m py_compile FusionMCP.py`).
  QA scenarios (name the exact tool + invocation): static — `git diff` inspection (only the constant + 2 lines); parse check — `py -3.14 -m py_compile FusionMCP.py`. Live proof is deferred to F3 (requires add-in reload in Fusion 360). Evidence `.omo/evidence/async-job-scheme-task-2.log`.
  Commit: N (Atlas commits after Final Wave)
- [x] 3. `fusion_server.py`: add `job_id` param (launch/poll/sync) to the 7 tools + `tests/test_job_tools.py` + update existing sync tests
  What to do / Must NOT do: (a) Add two private helpers near `_call`: `_job_status(job_id) -> dict` (returns `{"job_id": id, "status": "running"}` | `{"job_id": id, "status": "completed", "result": rec["result"]}` | `{"job_id": id, "status": "error", "error": rec["error"]}` | `{"job_id": id, "status": "not_found"}`) using `from jobs import get_job` (lazy import inside the helper, matching the file's existing lazy sibling-import pattern); and `_launch_job(fn, *args, **kwargs) -> str` (lazy `from jobs import launch_job`; returns `json.dumps({"job_id": launch_job(fn, *args, **kwargs), "status": "running"}, indent=2)`). (b) For EACH of the 7 tools, rename the current tool body to a private `_<name>_sync(<same params>)` and replace the `@mcp.tool()` function with a thin wrapper: signature = original params + trailing `job_id: str = ""`; body: `if job_id == "sync": return _<name>_sync(<args>)`, `if job_id: return json.dumps(_job_status(job_id), indent=2)`, `return _launch_job(_<name>_sync, <args>)`. Keep the original docstring and append one line: "`job_id`: \"\" (default) launches the job and returns a job id; \"sync\" runs synchronously and returns the full result; a job id polls status/result." (c) For `annotate_mesh_parameters` and `review_reconstruction` ONLY (the `-> list` tools): refactor so the private sync function returns a JSON-safe dict `{"text": <the current text-envelope str>, "views": [{"view": name, "image_base64": b64}]}` (no Image objects); the `@mcp.tool()` sync path converts that dict back into the current `list` return (text envelope str first, then `Image(data=base64.b64decode(b64), format="png")` per view) so the sync output is byte-identical to today; the async path stores the JSON-safe dict as the job result. Do NOT change `_call()`; do NOT touch the other ~70 tools; do NOT job-enable `capture_screenshot` or the default-30s mesh tools; do NOT modify `scad_translator.py`. (d) New `tests/test_job_tools.py` (headless, mocked transport): monkeypatch `fusion_server._call` (and `fusion_server.requests.post` for the 2 image tools) to return canned add-in JSON (e.g. for run_scad return `{"mesh": "body1", "triangles": 12}`; for review_reconstruction return `{"views": [{"view": "isometric", "image_base64": "<b64>"}]}`). Cover for run_scad: launch (`job_id=""`) → returns dict with `job_id` + `status == "running"`; poll the returned id (loop with small sleep) → eventually `status == "completed"` and `result` equals the canned sync output; `job_id="sync"` → returns exactly the current output format; unknown uuid → `status == "not_found"`; mocked `_call` raising `requests.exceptions.ReadTimeout` → job `status == "error"` with message containing "ReadTimeout". For reconstruct_mesh: launch + poll with mocked `get_mesh_data` + `create_from_csg_tree` → completed result matches sync. For review_reconstruction: launch + poll → `result` is JSON-safe (json.loads round-trip works; no Image objects; image_base64 strings present). (e) Grep `tests/` for every call to the 7 tools (`run_scad(`, `update_scad_body(`, `reconstruct_mesh(`, `reconstruct_from_faces(`, `create_from_scad(`, `annotate_mesh_parameters(`, `review_reconstruction(`) and add `job_id="sync"` to each call that expects a synchronous result (tests specifically exercising the new async behavior stay without it). Do not change assertions — only add the param.
  Parallelization: Wave 2 | Blocked by: 1 | Blocks: 4
  References (executor has NO interview context - be exhaustive): `mcp_server/fusion_server.py` — `_call` L269-282 (do not change), `run_scad` L~1370-1381 (timeout=330), `update_scad_body` L~1390-1398, `reconstruct_mesh` L~2000-2062 (three internal `_call`s: mesh_convert L2029-2032, revolve_cross_section L2043-2047, create_from_csg_tree L2057-2058 — all timeout=330), `reconstruct_from_faces` L2066+ (create_sketch_from_polygon L2199-2205), `annotate_mesh_parameters` L~2310-2369 (inline `requests.post` capture_mesh_views L2341-2344, timeout=60; returns `list`), `review_reconstruction` L2372-2429+ (inline POSTs L2402-2405 + L2414-2417, timeout=60; returns `list`), `create_from_scad` L~2515-2536 (timeout=330). Existing test conventions: `tests/test_scad_*.py`, `tests/test_reconstruct_*.py`, `tests/test_presets_tolerances.py` call the affected tools synchronously. Sibling import pattern: lazy `from bundle import get_openscad_path` at L263 / `_load_mesh_analysis` style. Evidence logs to `.omo/evidence/`.
  Acceptance criteria (agent-executable): `py -3.14 -m pytest tests/test_job_tools.py -q` green; `py -3.14 -m pytest -q` green (full suite after the `job_id="sync"` updates); `lsp_diagnostics` on `mcp_server/fusion_server.py` and `tests/` shows zero errors; `git diff --stat` shows only fusion_server.py + test files touched (plus jobs.py/tests from Todo 1); `git diff -- mcp_server/scad_translator.py` empty.
  QA scenarios (name the exact tool + invocation): happy — headless run_scad launch→poll→completed with canned result; sync path equality with pre-change output; failure — mocked ReadTimeout → job error status; boundary — unknown uuid → not_found; JSON-safe result for review_reconstruction (json.loads round-trip). Evidence `.omo/evidence/async-job-scheme-task-3.log`.
  Commit: N (Atlas commits after Final Wave)
- [x] 4. README + tool docstrings: document the job scheme
  What to do / Must NOT do: Add a "Long-running tools & job polling" section to `README.md` (after the OpenSCAD/mesh sections): explain the `job_id` parameter contract (launch when empty, poll by id, `"sync"` for blocking), show a 3-line example (launch `run_scad` → poll → use result), note jobs are in-memory and lost on server restart, and that the add-in cap is 300s. Update the 7 tools' docstrings only if Todo 3 did not already (check first — avoid double-edit conflicts; coordinate with the Todo 3 diff). Do NOT rewrite unrelated README sections; do NOT change any code.
  Parallelization: Wave 3 | Blocked by: 3 | Blocks: Final wave
  References (executor has NO interview context - be exhaustive): `README.md` (top-level; sections "OpenSCAD Pipeline", "Mesh Reconstruction", "Troubleshooting"); the docstrings live in `mcp_server/fusion_server.py` at the 7 tools (see Todo 3 references for line ranges).
  Acceptance criteria (agent-executable): README renders with the new section; the 7 tool docstrings mention `job_id` (either from Todo 3's appended line or this todo's update — exactly once, no duplication); `git diff -- README.md` contains only the new section.
  QA scenarios (name the exact tool + invocation): manual review of the diff; grep `job_id` in README.md and in the 7 tool docstrings → each appears ≥1×. Evidence `.omo/evidence/async-job-scheme-task-4.log`.
  Commit: N (Atlas commits after Final Wave)

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [x] F1. Plan compliance audit — Verify every todo implemented with exact function names, parameter signatures, return shapes, and error handling specified. Verify `jobs.py` API (`launch_job`/`get_job`/`prune`, MAX_JOBS=64, JOB_TTL=3600, running-never-evicted, statuses running/completed/error). Verify all 7 tools have the trailing `job_id` param and the launch/poll/sync contract; verify `job_id="sync"` output is byte-identical to pre-change output; verify the 2 image tools' async results are JSON-safe (no Image objects). Verify zero changes to `scad_translator.py` (git diff), zero changes to `_call()`, zero new requirements, no changes to non-job tools. Verify `ADDIN_CMD_TIMEOUT = 300` and the two-line FusionMCP.py diff only.
- [x] F2. Code quality review — Review `jobs.py` thread-safety (all mutations under `_LOCK`, no TOCTOU on record reads), LRU/TTL eviction correctness (running never evicted, bounded at 64), `_run` exception capture; review the 7 wrappers for param-order correctness (job_id last), docstring accuracy, and the image-tool two-phase refactor preserving sync output; review test quality (deterministic sleeps, no flaky timing asserts beyond generous bounds).
- [x] F3. Real manual QA — Agent-executed live verification against running Fusion 360 with a temp driver (pattern: `C:\Users\danie\AppData\Local\Temp\opencode\f3_live_driver.py` from the mesh plan). Add-in MUST be reloaded in Fusion (Add-Ins → Stop → Run) to pick up the 300s cap BEFORE this wave. Checks, all programmatic: (a) `execute_script` with `import time; time.sleep(35); result['output']='done'` returns success (proves the 30s→300s cap; would have failed with "did not respond in 30s" before); (b) `run_scad` with `job_id=""` returns `{"job_id", "status": "running"}`; poll until `status == "completed"` and the result parses as the expected mesh envelope; (c) `run_scad(..., job_id="sync")` returns the same result format synchronously; (d) poll a random uuid → `status == "not_found"`; (e) a normal fast tool (e.g. `draw_center_rectangle` + `extrude` via sync path) still works unchanged. Evidence `.omo/evidence/async-job-scheme-f3-live-qa.log`. NOTE: do NOT run this driver concurrently with pytest (live tests mutate the real Fusion document).
- [x] F4. Scope fidelity — Verify no out-of-scope changes: the ~70 non-job tools byte-identical (git diff), `capture_screenshot` untouched, `bundle.py` untouched, README-only diff is the new section, no job cancellation/persistence scope creep, evidence logs present for todos 1-4.

## Commit strategy
- No subagent commits (hard rule: commit only after verification). Atlas creates ONE atomic commit after the Final Wave passes: `feat(server): async job scheme for long-running tools (job_id launch/poll/sync, add-in 300s cap)` — staging only the plan's files (fusion_server.py, jobs.py, FusionMCP.py, tests, README.md).

## Success criteria
- `py -3.14 -m pytest -q` fully green (existing suite + new `test_jobs.py` + `test_job_tools.py`).
- `lsp_diagnostics` clean on `mcp_server/`, `tests/`, `FusionMCP.py`.
- `git diff -- mcp_server/scad_translator.py` empty; `git diff --stat` limited to the plan's files.
- `ADDIN_CMD_TIMEOUT = 300` in FusionMCP.py; live QA proves a 35s add-in command no longer times out at 30s.
- Live QA proves the full launch→poll→completed cycle for `run_scad`, and `job_id="sync"` equals the old blocking output.
- No new entries in `mcp_server/requirements.txt`.
