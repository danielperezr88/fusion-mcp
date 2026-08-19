# Issues — dismantle-one-shot-reconstruction

Problems and gotchas encountered during work on this plan.

_Auto-scaffolded by /start-work. Append new entries below - never overwrite._

---

## F3 — fresh-process MCP probe issues (2026-08-07)

- ENVIRONMENT TRAP (caused the earlier F3 failure): the repo has TWO Pythons with TWO mcp
  SDK versions. `py` (3.14.2) has mcp 2.0.0 which REMOVED `mcp.server.fastmcp` — importing
  `mcp_server/fusion_server.py` there fails with `ModuleNotFoundError: No module named
  'mcp.server.fastmcp'`. The deployed server runs on `py -3.13` (Store build, mcp 1.26.0).
  Any fresh-process probe MUST use `py -3.13` (or the same env as the deployed server).
- The stale long-running MCP server process still reports pre-dismantle behavior (it holds
  the OLD module image in memory); a fresh process is the only reliable evidence path for
  this plan. Must NOT use session `fusion360_*` MCP tools for QA of this change.
- Doc state had drifted from the earlier probe (no `fusionmcp_render_88c7d113` /
  `scad_linear_extrude_2` — replaced by `MeshBody1` + a freshly created `Cuerpo75`).
  Discovery-by-probe (analyze_mesh mesh=0 first) handled it without assuming body names.
- F3 created one extra BRep body (`Cuerpo75`) in the open Fusion doc — additive QA side
  effect, acceptable per plan; noted so nobody is surprised by it.
- First probe run logged `loop[0] points=0` because the loop key is `pts`; cosmetic only —
  assertion (loops > 0) already passed. Fixed the detail line and re-ran.
