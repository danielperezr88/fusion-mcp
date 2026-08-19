# Decisions — fusion-mcp-enhancements

Architectural choices and rationales discovered during work on this plan.

_Auto-scaffolded by /start-work. Append new entries below - never overwrite._

---

## 2026-08-03 A11-impl: Translator process split
- Fusion embedded Python = 3.14.0 but lacks openscad_evaluator (verified via execute_script).
- MCP server Python 3.14.2 HAS openscad-lalr-parser, openscad-evaluator, manifold3d installed.
- DECISION: resolve_scad() runs in mcp_server (server-side, has packages). translate_to_fusion_commands() runs inside Fusion add-in (has Fusion API objects). scad_translator.py MUST use guarded/lazy imports so it can be imported from BOTH processes: openscad_* imports only inside resolve_scad(), adsk imports only inside translate_to_fusion_commands().
- CSG tree crossing the HTTP bridge must be JSON-serializable (params are concrete floats/lists - verified by research).
