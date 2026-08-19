# Decisions — mesh-graph-pipeline

Architectural choices and rationales discovered during work on this plan.

_Auto-scaffolded by /start-work. Append new entries below - never overwrite._

---

## [2026-08-05] DECISION: async job scheme for timeout-prone MCP tools (user-mandated)
Source: user directive during F3 live QA (analyze_mesh timed out on cold start:
first call lazily imports numpy+trimesh via _load_mesh_analysis(), exceeding the
MCP request window).
Design (user-specified): a call with NO job id LAUNCHES a job and returns a job
id; a call WITH a job id returns status and/or result.
Scope: the mesh-pipeline tools that do heavy processing (analyze_mesh,
structure_graph, and the other numpy/trimesh/OpenSCAD-heavy tools); simple tools
stay synchronous.
Open implementation forks to resolve in the plan:
  - API shape: same-tool optional job_id param vs dedicated status tool
  - job store: module-level dict + threading.Lock; bounded LRU + expiry
  - error capture: job result carries exception message (status 'error')
  - backward compat: async-first changes the tool contract; README + tests updated
Status: to be planned (new workstream after mesh-graph-pipeline closes).
