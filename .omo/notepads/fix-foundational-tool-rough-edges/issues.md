# Issues — fix-foundational-tool-rough-edges

Problems and gotchas encountered during work on this plan.

_Auto-scaffolded by /start-work. Append new entries below - never overwrite._

---

## [2026-08-10 01:47:43] Confirm: F3 still blocked, state stable
- AddIns installed copy == repo copy (SHA match True), backup present.
- Fusion 360 process still PID 13532 (started 31/07/2026 20:59:19, pre-fix) => stale add-in confirmed again.
- Plan checkboxes: 9/10 [x], F3 [~] (blocked). No new probes run against stale add-in (wasteful; staleness already proven twice).
- Next step ONLY possible by user: Utilities -> Add-Ins -> FusionMCP -> Stop -> Run (or restart Fusion 360).
- After reload: verify execute_script nested-def returns "7", then run F3 live probes (compare max<=1.5, structure_graph w/ 150s cap), F3 verdict, atomic commit on master, mark F3 [x].
## [2026-08-10] T3-live-QA bug: _sample_brep_surface_points getPointAtParameter(u, v) -> Point2D; fixed
## [2026-08-10] T3-live-QA bug #2: _evaluator_uv_bounds must call parametricRange() method (paramBounds property absent on build); fixed

## 2026-08-10 F3 BLOCKER: MCP server process staleness (ACTION NEEDED)
- The fusion360 MCP server process (PID 26648, child of opencode session
  ses_046722e53fferGQ5c1ZKxNJ377) started 2026-08-07 19:45:45 - BEFORE the mesh_graph.py
  persist fix (mtime 2026-08-10 03:04:42). It caches the old executemany module in memory.
- structure_graph live probe WILL keep timing out until that process restarts.
- Fix is proven on identical real data (58s chain, persist 0.7s) - only the process is stale.
- To unblock: restart the MCP server (restart opencode session OR /mcp reconnect OR kill
  PID 26648 + wrapper 34664 so the host respawns with fresh code), then re-run
  fusion360_structure_graph(mesh="1") and expect success (not -32001).
