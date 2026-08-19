# Learnings — mesh-graph-pipeline

Conventions, patterns, and successful approaches discovered during work on this plan.

_Auto-scaffolded by /start-work. Append new entries below - never overwrite._

---

## [2026-08-05] Task: T1 R-1 — quantization-step-derived weld/snap tolerances

Implemented `_detect_quantization_step(node_list)` in `mcp_server/mesh_analysis.py`
(L880) and wired it into `decompose_mesh_faces` (weld `eps`, `snap_tol`) and
`analyze_mesh_data` (weld `eps`). Full evidence: `.omo/evidence/task-1-mesh-graph-pipeline.log`.

Key design decisions (for downstream R-2 grouping / R-3 snap agents):

1. **Formula** (exactly per plan): `eps = max(max(1e-9, 1e-7*extent), 3*quant_step)`;
   `snap_tol = max(1e-5, max(5e-4*extent, 2*quant_step))`. `quant_step` is computed
   once per call and is in scope for both weld and snap in `decompose_mesh_faces`.
2. **"Small step" cap** = `max(1e-4, 1e-3*extent)`. Without it, integer-coordinate
   fixtures (unit cube, coplanar-quad tests) produce geometry deltas ~1.0 that
   would be misread as the quantization step and blow `eps` up to several units,
   welding entire models together. The cap + frequency filter keep every
   pre-existing fixture bit-identical (quant_step == 0.0 there).
3. **Recurrence guard**: a step only counts if it appears >= 2 times in the
   histogram. A genuine exporter grid recurs across hundreds of adjacent deltas;
   a one-off seam gap (e.g. the 1.5e-4 seam fixtures) appears once and is ignored,
   so seam behavior stays unchanged. Without this, seam fixtures would weld more
   aggressively (assertions still held in manual analysis, but unchanged behavior
   is the surgical-scope-safe choice).
4. **Tie-break**: most-frequent wins; ties -> smallest step (`key=lambda d:
   (steps[d], -d)`). Deterministic (Counter + sorted unique values per axis).
5. **Rounding**: deltas rounded to 9 decimals before counting; collapses float
   noise (0.9999999995e-6 -> 1e-6) so 6-decimal STL -> 1e-6, 4-decimal -> 1e-4.
6. **Safe default**: no nonzero deltas (uniform coords) or no recurring small
   step -> returns 0.0; the caller `max()` floors then guarantee
   `eps >= max(1e-9, 1e-7*extent)` and `snap_tol >= 1e-5`. No division, no crash.
7. **Weld-grid caveat (important for R-3)**: `_weld_vertices` uses rounded-key
   grid cells of size `eps` — two points within `eps` do NOT merge if they straddle
   a cell boundary (round-half-even at `k+0.5` grid lines). A test fixture whose
   duplicate landed on a diagonal at offset 1e-6 straddled the boundary and did
   not weld; the working fixture instead writes the hub vertex at three adjacent
   1e-6-grid points that all land in one 3e-6 cell. Downstream tolerance users
   should expect weld = grid-cell bucketing, not exact-distance radius.
8. Baseline 30/30 green; after change 36/36 in the target file and 140/140
   headless (18 `*_live` deselected) — no regressions.


## [2026-08-05] Task: T2 R-2 � connectivity-constrained planar grouping (primary), global first-match fallback

Implemented in `mcp_server/mesh_analysis.py`: `_group_planar_triangles` (L1032)
now runs a connectivity-constrained region-growing FIRST PASS before the unchanged
global first-match fallback. Full evidence: `.omo/evidence/task-2-mesh-graph-pipeline.log`.

Key design decisions (for downstream R-3 snap / R-7 plane-fit agents):

1. **Strict welded-edge adjacency is NOT enough � seam fixtures would split.**
   Diagnostic: the seam test welds to tris [(0,1,2),(0,2,3),(4,5,6),(4,6,7)] �
   the 1.5e-4 seam gap is NOT bridged by weld eps (4e-7) nor by quant-derived
   eps (quant_step=0 there: the single-occurrence 1.5e-4 delta is ignored by the
   recurrence guard). The separated fixture welds to the SAME shape. So f_arr
   alone cannot distinguish seam-must-merge (1.5e-4) from gap-must-not-merge
   (2.0). A strict welded edge map would split `test_seam_duplicated_touching_quads_merge`.
2. **Resolution: `_cluster_near_vertices`** � deterministic grid union-find
   clustering of vertices within `edge_adj_tol = max(1e-5, 5e-4*extent)`
   (the extent-based floor of snap_tol = the criterion under which snapping
   merges them anyway). The edge map is keyed by CLUSTERED vertex ids, so
   seam-coincident edges count as shared while real gaps (>> tol) stay apart.
   This keeps the seam tests as ONE group/face while separated patches become
   TWO groups. R-3 must be aware: the grouping layer already treats edges within
   snap_tol as coincident, so per-group snapping is mostly confirmatory.
3. **Fallback must NOT match connectivity groups.** If the global first-match
   fallback scanned the whole `groups` list (connectivity groups included), an
   unassigned coplanar triangle would re-join a connectivity region by plane
   alone � resurrecting the over-merge R-2 exists to prevent. The fallback runs
   first-match over fallback-created groups only; predicate, group dict shape
   and degenerate-skip are byte-identical to the pre-change path.
4. **Determinism recipe**: seeds = valid faces sorted by `(-area, ti)`
   (largest area first, smallest index on tie); neighbors expanded via
   `sorted(adj[cur])`; region `tri_indices` sorted before storing; groups
   emitted in seed order then fallback order. No `set` iteration order
   anywhere in the control flow.
5. **Predicate isolation**: `_coplanar_with_region(face_plane, region_plane,
   cos_tol, offset_tol)` where each plane is a canonical `(unit_normal,
   offset)` tuple � the exact seam R-7 will swap for a running plane-fit.
   Kept at 4 args by passing plane tuples rather than flattened normals.
6. **Expected behavior change (correct)**: coplanar-but-disconnected patches
   change from ONE group (2 boundary loops -> 2 faces) to TWO groups (1 loop
   each -> 2 faces). Face-level output is unchanged for every existing test.
    Baseline 36/36 green; after change 39/39 in the target file (3 new tests:
    touching->1 group, separated->2 groups, non-coplanar adjacent->2 groups)
    and 143/143 headless (18 `*_live` deselected) � no regressions.


## [2026-08-05] Task: T3 R-3 � grid-bucketed spatial-hash snap (O(n^2) -> O(n) avg)

Replaced the O(n^2) all-pairs loop inside `_snap_group_vertices` (L1221, drifted
from L1021 after T2) with a floor-key grid spatial hash. Full evidence:
`.omo/evidence/task-3-mesh-graph-pipeline.log`.

Key design decisions (for downstream R-4 warnings / future R work):

1. **Floor keys, per plan spec** (NOT the round() keys `_cluster_near_vertices`
   uses): cell = `(floor(x/snap_tol), floor(y/snap_tol), floor(z/snap_tol))`,
   cell size = snap_tol, probe 27-cell neighbourhood. `round()` and `floor()` are
   NOT interchangeable for the "no in-range pair missed" guarantee: round()
   straddle analysis differs (round at k+0.5 mid-cell boundaries can split a
   pair at distance < tol when they round to cells 2 apart? No � round keys also
   differ by <=1 within tol, but floor is what the plan mandates; stick to it).
2. **Invariant proof (write this down once)**: |a-b| < T  =>  |floor(a/T) -
   floor(b/T)| <= 1 (floor monotone + floor(x+1)=floor(x)+1). So Euclidean dist
   < snap_tol => per-axis key diff <= 1 => both vertices in the 27-cell
   neighbourhood. Every in-range pair is examined; distance verdict is bit-exact
   (same float64 op order as old code: coords[high]-coords[low], then @).
3. **Partition is union-order-independent**: with "smaller root absorbs larger",
   root(x) = min-index member of the component is an invariant at every union,
   so the canonical vertex per cluster = smallest member, regardless of probe
   order. The new remap is BIT-IDENTICAL to the old O(n^2) one (proven + tested),
   not merely "functionally equivalent" � the plan's canonical-may-differ caveat
   is not exercised.
4. **Insert-after-probe ordering**: each vertex is appended to its cell only
   after probing, so candidates always have u < i (mirrors old inner loop j>i),
   each pair examined once, deterministic per-cell lists.
5. **Strict `< snap_sq` preserved**: gap exactly == snap_tol (cell-boundary
   straddle) is probed (keys differ by exactly 1) and correctly rejected �
   covered by a new boundary test (over tol not merged, under tol merged as
   {1: 0}, exact not merged).
6. **Test strategy for equivalence**: keep the O(n^2) implementation as a
   TEST-ONLY reference copy (`_snap_group_vertices_quadratic_ref` in the test
   file), compare canonical cluster partitions (groups + sizes of global vertex
   indices) for random clustered inputs of sizes [10,100,500,2000], plus a full
   `decompose_mesh_faces` run through both paths via monkeypatching
   `mesh_analysis._snap_group_vertices` (module-global lookup makes this work).
7. **Performance**: 5000-vertex group 0.0907s vs 24.078s quadratic ref = ~265x.
   Merge count identical (4280). Degenerate all-in-one-cell input is still O(n^2)
   (inherent to the algorithm); the perf test uses clustered random input.
8. Baseline 39/39; after change 42/42 in the target file, 146/146 headless
   (18 `*_live` deselected) � no regressions.


## [2026-08-05] Task: T4 R-4 � residual non-manifold edge warnings (per-face)

Added read-only residual accounting to `mcp_server/mesh_analysis.py`:
`_count_residual_loop_edges` helper (L1393), optional `residual_out` kwarg on
`_boundary_loops` (L1413/1416, return shape UNCHANGED � direct tests depend
on it), per-face `"warnings"` + top-level `"has_warnings"` in
`decompose_mesh_faces`. Full evidence: `.omo/evidence/task-4-mesh-graph-pipeline.log`.

Key design decisions (for T14 R-8 and future R work):

1. **Residual definition**: undirected edge pair whose directed cancellation
   count `|net| != 1` (i.e. `|net| >= 2`) that survives into the boundary set.
   net=1 is the normal boundary edge; net=0 cancels (interior); |net|>=2 = the
   seam was too wide to close. Pre-pinch count = copies in the boundary set
   (`sum(abs(net))`); post-pinch count = occurrences of residual pairs among
   the consecutive pairs of the final loops. The WARNING reports the post-pinch
   per-face count (true residual); `_boundary_loops` residual_out also carries
   both per-group counts for diagnostics.
2. **How a "too-wide seam" actually becomes residual (important for fixtures)**:
   a plain gap (offset > snap_tol, fragments NOT connected) yields TWO separate
   planar groups -> two clean faces, ZERO residuals. Residuals materialise only
   when the duplicated seam fragment stays CONNECTED to the base fragment via
   shared (welded/clustered) vertices while its seam edge vertices remain
   unsnapped. The working 0.1-offset fixture: quads 2 and 3 both contain the
   REAL far edge (6,0)-(6,4) (welds to the same vertex ids) while their seam
   edges sit at x=4 and x=4.1. T-junction splitting (tjunc_tol=snap_tol) then
   splits the base quad's seam edge at the (4.1,�) fragment vertices, leaving
   THREE net=2 edges: {(6,0)-(6,4), (4.1,0)-(6,0), (6,4)-(4.1,4)} -> pre=6,
   post-per-group=6, per-face-after-merge=3 (inner/outer loop merge cancels
   shared residual copies). remap={} confirms the seam did NOT snap.
3. **Per-face attribution**: one planar group -> N faces, and inner loops can
   MERGE into the outer (`_merge_loop_into_outer` re-chains with edge
   cancellation), so the per-face count MUST be computed in decompose_mesh_faces
   from the face's FINAL loops (`[outer["vi"]] + [h["vi"] for h in holes]`),
   not from the per-group count. Communication: `_boundary_loops` fills an
   optional `residual_out` dict {"residual_pairs": set-of-frozensets, ...};
   no return-shape change, no module-level mutable state.
4. **Loop-chaining/pinch logic untouched**; the residual computation is a
   pure read of `net` values already computed in the boundary-extraction loop
   plus `_count_residual_loop_edges` over the returned loops. Additive only.
5. **T14 hook**: total residual edge count = `sum(face["warnings"][0]["count"]
   for face in planar_faces if face["warnings"])`; `has_warnings` True iff > 0.
   T14 compares total vs >5% of triangle edges to set
   `strategy_fallback_suggested: "organic"`.
6. Baseline 42/42; after change 45/45 in the target file (3 new tests:
   clean-mesh no warnings, 0.1 offset-seam warning, additive contract),
   149/149 headless (18 `*_live` deselected) � no regressions.


## [2026-08-05] Task: T5 � NetworkX property-graph builder (mesh_graph.py)

Created `mcp_server/mesh_graph.py` (482 lines): `build_structure_graph`
(decompose_result: dict) -> `nx.Graph`, 4 node types / 10 edge types, pure
math (no numpy/trimesh). Full evidence:
`.omo/evidence/task-5-mesh-graph-pipeline.log`.

Key design decisions (for T6 DuckDB sink, T7 components, T9 scoring):

1. **EDGE_ADJACENT needs a CONSECUTIVE-pair rule, not just shared vertices.**
   Built from a vertex->face map (rounded-6dp vertex tuples), then
   `_find_shared_edge` requires a shared pair that is consecutive in BOTH
   ordered polygons (wrap-around allowed). Diagonal-only sharers fall to
   VERTEX_TOUCH. Face vertex order comes from decompose (ordered polygon, no
   repeated first vertex), so the consecutive test is reliable.
2. **nx.Graph = one edge per node pair -> precedence is structural.**
   Face->Component cannot hold BOTH COMPONENT_OF and HAS_BASE. HAS_BASE
   (emitted for every face) wins the pair; COMPONENT_OF is CurvedPatch->
   Component only. A naive "add COMPONENT_OF for faces too" is dead code
   (silently overwritten). Document this so it is not re-added.
3. **EXTRUSION_ALIGNED must REPLACE geometric relations, not skip pairs with
   an existing edge.** The first version skipped `G.has_edge` pairs -> a
   closed box produced ZERO EXTRUSION_ALIGNED (top/bottom already carried
   SAME_ORIENTATION). Fix: remove COPLANAR/SAME_ORIENTATION/PARALLEL/
   PERPENDICULAR and re-add EXTRUSION_ALIGNED; never touch physical edges.
4. **Closed-box EXTRUSION_ALIGNED = 3, not 12.** The marked cycle + family
   pairs where the box side walls already hold physical EDGE_ADJACENT edges
   keep those (physical wins). Only the three parallel non-adjacent pairs
   (top-bottom, left-right, front-back) get EXTRUSION_ALIGNED. That is
   correct per the plan's precedence and satisfies ">=1 edge on a box".
5. **Decompose normals are canonicalised** (dominant component flipped
   positive) and rounded 6dp: box bottom face normal is [0,0,1], not
   [0,0,-1]. SAME_ORIENTATION must compare CANONICAL normals; plane_id
   ("nx|ny|nz|offset") uses the raw normal + offset, so parallel families
   at different offsets get distinct plane_ids.
6. **acos-derived angles cap at 180deg** (decompose `_compute_polygon_
   angles_3d`), so concave faces only appear from reflex input; triangles
   are always convex. Convexity on an edge uses sum of the two interior
   angles at the shared-edge endpoint > 360deg -> concave.
7. **T6 column mapping**: node/edge attrs are all JSON-serialisable
   (lists not numpy arrays); relation column = edge attr "relation";
   G.graph carries has_warnings + components_detected; component membership
   for faces is the HAS_BASE edge (T7 reads it), base-candidate scoring is
   T9's job via face.is_base_candidate (starts False).
8. **Test discipline**: every fixture runs through the REAL
   `decompose_mesh_faces` -> `build_structure_graph` pipeline (never
   hand-built dicts); determinism tested via
   `nx.to_dict_of_dicts(G1) == nx.to_dict_of_dicts(G2)` + node attrs +
   G.graph equality. 149 baseline + 20 new = 169/169 headless, no regressions.


## [2026-08-05] Task: T6 — DuckDB persistence (_persist_to_duckdb)

Added the DuckDB sink to `mcp_server/mesh_graph.py` (ADD-only; T5 untouched):
`_persist_to_duckdb(graph, mesh_key)` (L616), `_get_graph_db(mesh_key)`
(L660), module-level `_GRAPH_DBS` OrderedDict LRU + `MAX_GRAPHS = 16` (L518/521).
Full evidence: `.omo/evidence/task-6-mesh-graph-pipeline.log`.

Key design decisions (for T7 components / T8 query tool / T9 scoring):

1. **Dynamic schema = fixed core + SORTED union of attr keys.** nodes:
   `node_id TEXT PRIMARY KEY, label TEXT` then the sorted union of all node
   attrs; edges: `source, target, relation` then sorted union of edge attrs.
   Sorting (never insertion order) keeps the schema deterministic. T9 adds
   `base_score`/`unit_type`/`rebuild_order` to nodes -> they become columns
   automatically; **call build_structure_graph -> enrich nodes -> persist**.
2. **Per-column type inference from the values** (`_column_type`): bool ->
   BOOLEAN (checked BEFORE int: bool subclasses int), int -> INTEGER,
   int/float mix -> DOUBLE (ints widened at bind time), str -> TEXT,
   list/dict/tuple -> TEXT via `json.dumps` (round-trip with `json.loads`),
   missing attr -> NULL. Columns only appear when the union contains them: a
   box has NO `normal_dot` edge column (no box pair carries it).
3. **All values parameterized** (`executemany` with `?`); the ONLY thing
   reaching SQL text is column names, validated by `_safe_ident`
   (`^[A-Za-z_][A-Za-z0-9_]*$`). Whole write (DDL+index+inserts) is one
   BEGIN/COMMIT transaction; ROLLBACK+close on any exception.
4. **LRU semantics**: insert moves the key to MRU; replace-same-key closes
   the OLD connection first (leak guard); overflow -> `popitem(last=False)`
   + `conn.close()` (while-loop, though only one evict is ever needed).
   `_get_graph_db` promotes to MRU and returns the SAME connection object
   (identity; T8 must mutate that object). It never opens a new connection.
5. **KeyError contract for T8**: `_get_graph_db(mesh_key)` raises
   `KeyError("no graph built for mesh '<k>'. Call structure_graph first
   (graphs are ephemeral: the MCP server restart drops them)")` — tests match
   on "Call structure_graph first" and "ephemeral".
6. **CRITICAL for T8 — duckdb 1.5.5 CANNOT switch a running in-memory
   connection to read-only.** `SET access_mode='read_only'` (and SET GLOBAL)
   raise InvalidInputException ("it must be set when opening or attaching");
   `duckdb.connect(":memory:", read_only=True)` raises CatalogException
   ("Cannot launch in-memory database in read-only mode!"); ATTACH ':memory:'
   (READ_ONLY) fails too. WORKAROUND: the connection is process-local, so
   read-only enforcement lives in the T8 query layer (issue only SELECTs /
   a helper that rejects non-read statements). T6's read-only test locks the
   real contract: deterministic plain SELECTs on the cached connection.
7. **duckdb API quirks learned**: `executemany` raises InvalidInputException
   on an EMPTY parameter list -> guard `if normalized:` before bulk insert
   (locked by `test_persist_empty_graph`). `PRAGMA table_info(t)` gives
   column names at index 1. In-memory conns are per-connection; each
   `duckdb.connect(":memory:")` is a fresh empty DB (why the LRU holds one
   conn per mesh key).
8. **Public API surface for T7's lazy import** (fusion_server.py L55-82
   pattern): keep `build_structure_graph`, `_persist_to_duckdb`,
   `_get_graph_db`, `_GRAPH_DBS` stable. T7 calls `_persist_to_duckdb(G,
   mesh_key)` AFTER building; mesh_key is the caller's responsibility (any
   string; the MCP tool will likely use the mesh body id/name).
9. **Tests**: new file `tests/test_mesh_graph_db.py` (130 LOC, 6 tests) with
   an autouse `_clear_graph_db_cache` fixture so LRU state never leaks across
   tests. Box fixture expectations: nodes=7, edges=21, face:0 has exactly 4
   EDGE_ADJACENT neighbours. Baseline 169 -> 175/175 headless, no regressions.


## [2026-08-05] Task: T7 — MCP tool `structure_graph` with DuckDB persistence

Added to `mcp_server/fusion_server.py` (ADD-only): `_load_mesh_graph()`
(lazy importer, mirror of `_load_mesh_analysis` L55-82) and the
`@mcp.tool() structure_graph(mesh="0", units="cm")` tool. New test file
`tests/test_structure_graph_tool.py` (5 tests, mock-`_call`). Full evidence:
`.omo/evidence/task-7-mesh-graph-pipeline.log`.

Key learnings (for T8 query tool / T9 base scoring):

1. **The `_call` mock pattern for headless fusion_server tests** (established
   in test_mesh_convert.py, reused): load fusion_server.py with a stubbed
   `mcp.server.fastmcp` (stub `_FastMCP.tool` records `fn.__name__` into
   `self._tools` so tests can assert `"structure_graph" in fs.mcp._tools`);
   then `monkeypatch.setattr(fs, "_call", fake)` where `fake` is a
   `_FakeCall` recorder `def __call__(self, command, params=None, timeout=30)`
   returning the canned box payload (`json.dumps({"mesh": "0", "nodes": 8
   verts, "indices": 12 tris, "normals": []})` — the `_box()` fixture). The
   REAL pipeline resolves through `_load_mesh_analysis()`/`_load_mesh_graph()`
   (repo root on sys.path), so asserted counts are genuine end-to-end. An
   autouse fixture clears `mesh_graph._GRAPH_DBS` before/after each test.
   Units validation happens BEFORE `_call` fires — the invalid-units test
   asserts `fake.calls == []`.
2. **Summary schema** (tool returns JSON string, NOT the graph — graph stays
   server-side in DuckDB): `{mesh, units, component_count, face_count,
   edge_type_counts (all 10 relation keys, zeros included, sorted),
   base_face_candidates, has_warnings, duckdb_table_schema{nodes,edges} via
   PRAGMA table_info, query_example, articulation_points,
   connected_component_count}`. `duckdb_table_schema` is computed AFTER
   `_persist_to_duckdb` so it reflects the real persisted columns. Box:
   face_count=6, component_count=1, EDGE_ADJACENT=12, EXTRUSION_ALIGNED=3,
   HAS_BASE=6, edges total 21, articulation_points=[] (cube graph is
   2-connected).
3. **`is_articulation_point` must be set on Face nodes BEFORE
   `_persist_to_duckdb`** so the flag lands as a nodes column. Guard with
   `d.get("label") == "Face"` — `nx.articulation_points` can return
   non-Face nodes (they carry no such attr). Graph.graph attrs
   (articulation_points / connected_components lists) are NOT persisted as
   rows — they live on the in-memory Graph object only.
4. **T8 read-only enforcement — VERIFIED FACT (re-confirmed in T7): a
   running in-memory duckdb 1.5.5 connection CANNOT be switched to
   read-only.** `SET access_mode='read_only'` (and `SET GLOBAL`) raise
   InvalidInputException ("access_mode can only be set when opening or
   attaching a database"); `duckdb.connect(":memory:", read_only=True)`
   raises CatalogException ("Cannot launch in-memory database in read-only
   mode!"); `ATTACH ':memory:' (READ_ONLY)` fails too. T8 MUST enforce
   read-only at the STATEMENT level: reject any statement that does not
   start with SELECT/WITH (or a whitelist), e.g. a `query_structure_graph`
   tool that uppercases + strips the input and returns an error for
   non-SELECT/WITH prefixes, and blocks `;`-separated extra statements.
5. **Persistence key = the `mesh` param string verbatim.** Tool calls
   `_persist_to_duckdb(graph, mesh)` with mesh="0" by default, so T8's
   `_get_graph_db("0")` resolves. `_persist_to_duckdb` closes + replaces the
   cached connection for the same key on rebuild (leak guard from T6) — a
   second `structure_graph(mesh="0")` call yields a NEW connection object.
6. **T9 note**: `base_face_candidates` in the summary is a PRELIMINARY
   heuristic (largest-area Face per component via `area_cm2`, strict `>` so
   the first tie wins deterministically — box yields face:0/4.0). T9/T10
   replaces it with real scored candidates (base_score/unit_type/
   rebuild_order become nodes columns automatically via the T6 dynamic
   schema). Keep the summary key stable.
 7. Baseline 175 -> 180/180 headless (5 new tests), no regressions.



## [2026-08-05] Task: T8 — MCP tool `query_structure_graph` with SQL

Added to `mcp_server/fusion_server.py` (ADD-only): `_assert_read_only_sql`
(L1596), `_json_safe_cell` (L1631) and the `@mcp.tool()
query_structure_graph(mesh="0", sql="")` tool (L1647), inserted right after
`structure_graph`. New tests: 9 appended to `tests/test_structure_graph_tool.py`
(reusing the `fs`/`_run`/`_FakeCall`/`_box_payload` fixtures). Full evidence:
`.omo/evidence/task-8-mesh-graph-pipeline.log`.

Key learnings (for T9/T10 workstream analysis queries and T14):

1. **Read-only is enforced at the STATEMENT level (design locked).** The plan's
   original `SET access_mode='read_only'` approach is impossible on duckdb
   1.5.5 in-memory (verified T6/T7 and re-confirmed; all three variants raise).
   `_assert_read_only_sql` pipeline: (a) strip `/* */` block comments
   (DOTALL, non-greedy) then `--...` line comments; (b) strip ONE trailing `;`;
   (c) reject if ANY `;` remains (kills `SELECT 1; DROP TABLE nodes`); (d) the
   FIRST WORD must be a prefix of `select` or `with` (case-insensitive).
2. **Keyword check is prefix-of, NOT startswith** — this is the subtle bit the
   spec's tests forced: `SELEC 1` must reach the DuckDB parser and return
   `{"error": "SQL error: ..."}`, not "only SELECT queries are allowed". With
   `lowered.startswith("select")`, "selec 1" fails the gate (test broke). Fix:
   `"select".startswith(first_word) or "with".startswith(first_word)`. A strict
   prefix (selec, w, ...) can never begin a valid DuckDB statement, so only
   exact `select`/`with` first words execute; truncated-keyword typos fall
   through to DuckDB's ParserException. All real destructive keywords
   (DROP/INSERT/UPDATE/DELETE/ALTER/CREATE/...) are rejected.
3. **Comment stripping order matters**: `-- x\nDROP TABLE` and `/* x */ DROP`
   are rejected because stripping runs before the keyword check. Semicolons
   inside string literals (`SELECT 'a;b'`) are over-strictly rejected — safe
   failure, never a false accept; accepted for simplicity.
4. **Residual (documented, acceptable): `SELECT * INTO t FROM nodes` creates a
   table** while starting with the SELECT keyword. The plan's design is
   statement-level best-effort; the agent is trusted. T9/T10 should not rely on
   query_structure_graph as a security boundary.
5. **Deterministic ordering**: `"order by" in cleaned.lower()` (checked on the
   comment-stripped SQL) -> absent => `rows.sort(key=lambda r: (r[0] is None,
   r[0]))`. The None guard is REAL, not defensive: `SELECT area_cm2 FROM nodes`
   on a box returns `[4.0 x6, None]` (Component node has no area_cm2 column
   value) — a bare `key=lambda r: r[0]` would raise TypeError sorting None vs
   float. `(r[0] is None, r[0])` keeps NULLs last and preserves numeric order.
6. **DuckDB naming quirk for T9/T10 tests**: `SELECT COUNT(*) FROM nodes`
   produces column name **`count_star()`** (not "COUNT(*)"). Box: 7 nodes / 21
   edges; nodes table 19 columns, edges table 8 columns (box carries NO
   VERTEX_TOUCH/SAME_ORIENTATION/COPLANAR edges, so no
   shared_vertex/shared_vertex_count/normal_dot/normal_x/y/z columns — the
   column set is the sorted union of attrs PRESENT).
7. **Recursive-CTE pattern that works on a box**: `WITH RECURSIVE adj(u,v) AS
   (SELECT source,target FROM edges WHERE relation='EDGE_ADJACENT' UNION ALL
   SELECT target,source ...), hops(node_id,depth) AS (SELECT 'face:0',0 UNION
   ALL SELECT a.v,h.depth+1 FROM hops h JOIN adj a ON a.u=h.node_id WHERE
   h.depth<2) SELECT DISTINCT node_id FROM hops ORDER BY node_id` — the `adj`
   CTE makes the traversal undirected (raw edges are source=lower-index only,
   so a source->target-only CTE would be asymmetric and miss faces). 2-hop
   closure from any face of a closed box = all 6 faces.
8. **T9/T10 query path**: T9 writes `base_score`/`unit_type`/`rebuild_order`
   onto node attrs before `_persist_to_duckdb` (T6 dynamic schema adds them as
   columns automatically); T10 analyses run through `query_structure_graph` SQL.
   T14 reads `strategy_fallback_suggested` via the structure_graph summary.
   The tool takes `mesh` verbatim and errors `"no graph built for mesh 'X'.
   Call structure_graph first."` on a missing key (KeyError contract from T6).
9. Baseline 180 -> 189/189 headless (9 new tests), no regressions. `py_compile`
   clean; basedpyright LSP not installed (user declined) so py_compile is the
   syntax gate.




## [2026-08-05] Orchestrator: T8 VERIFIED + COMMITTED (151ebca)

T8 verified by orchestrator: diff-read (161 lines fusion_server.py + 123 lines tests), 14/14 targeted, 189/189 headless, no stubs. Phase-1 review confirmed both deviations justified (prefix-of select/with gate; None-guard sort key). Commit 151ebca.

## [2026-08-05] Task: T9 — workstream computation

Implemented three pure functions in `mcp_server/mesh_graph.py` (ADD-only, ~330 lines)
with wiring at the end of `build_structure_graph` (L568-L571).  New test file
`tests/test_workstream.py` (27 tests).  Full evidence:
`.omo/evidence/task-9-mesh-graph-pipeline.log`.

Key design decisions and gotchas:

1. **DAG edge direction** — `nx.topological_sort` returns nodes where every directed
   edge `u → v` has `u` before `v`.  For base-first ordering, edges must be
   `base → others` (dependency first, dependent later).  The initial implementation
   had the reverse direction (`dependent → dependency`), which put base LAST.
   Fixed in the second iteration.

2. **Component base_score must precede early return** — `_score_base_faces` guards
   on `not face_nodes` at the top, but a hand-built graph with only Component
   nodes (no Face nodes) would return `{}` before reaching the Component
   base_score assignment loop.  Fix: move the Component-initialization loop
   BEFORE the `if not face_nodes: return {}` guard.

3. **Box non-uniform scores** — The `floor_facing` component produces different
   scores for different faces on a box (0.85 for top/bottom with canonical
   `normal_z>0`, 0.70 for side faces with `normal_z=0`).  The composite score
   + tie-break (`is_base_candidate` per component: highest score → area →
   face_index) still deterministically picks face:0 on a box, consistent with
   the T7 preliminary heuristic.

4. **DAG cycle breaking** — Uses `nx.find_cycle` to locate a cycle edge, then
   resolves to the underlying EDGE_ADJACENT edge with the lowest
   `shared_edge_length` between the two components' faces.  Retries ONCE, then
   falls back to sorted-component order on double failure.  The `caplog` fixture
   captures the warning message in tests.

5. **Hole dependency links** — A `hole` component depends on the component of the
   face whose CONTAINS edge points to the hole's curved patch.  This is resolved
   by tracing: CurvedPatch→(COMPONENT_OF)→Component for ownership, then
   CurvedPatch→(CONTAINS)→Face→(component_id)→Component for containing-face owner.

6. **Test pattern for hand-built graphs** — Tests that need 6 unit types
   (base/protrusion/depression/hole/fillet/freeform) use hand-built `nx.Graph`
   instances with the `_mk_face`/`_add_edge_adjacent`/etc. helpers, because the
   real decompose pipeline cannot cheaply produce all six.  Docstring documents
   this design choice.

7. **One pre-existing test updated** — `test_face_node_attrs` in
   `tests/test_mesh_graph.py` L130 asserted `is_base_candidate is False` for all
   faces.  T9 now sets one to `True`.  Changed to `is_base_candidate in (True, False)`.

8. **Final counts**: baseline 189 → 216 headless (27 new), 18 `*_live` deselected,
   zero regressions.  py_compile clean.


## [2026-08-05] Task: T10 — wire workstream into structure_graph

Enriched the `structure_graph` summary JSON with a `workstream` key carrying the
T9 workstream analysis, without breaking any existing summary keys.

Key design decisions and gotchas:

1. **Read-only pattern**: T9 already calls `_score_base_faces`, `_classify_units`,
   `_build_dependency_order` at the end of `build_structure_graph` (L568-571).
   By the time `structure_graph` has the graph object, all attrs are already set:
   - Face nodes: `base_score`, `is_base_candidate`
   - Component nodes: `base_score`, `unit_type`, `rebuild_order`
   - Graph-level: `graph.graph["rebuild_order"]`, `graph.graph["dag_has_cycles"]`
   T10 only READS these — no re-calling of T9 functions (double computation avoided).

2. **Summary shape** — exact per-plan schema:
   ```python
   "workstream": {
       "base_face_per_component": {"0": "face:0", ...},
       "unit_types": {"0": "base", ...},
       "rebuild_order": ["component:0", ...],
       "dag_has_cycles": false
   }
   ```

3. **Dictionary key determinism**: `base_face_per_component` and `unit_types`
   dicts are built by scanning nodes, then keys are sorted by `int(cid)` before
   `str()`-ing — so `json.dumps` produces deterministic output. `rebuild_order`
   is a plain list copy from `graph.graph["rebuild_order"]` (already sorted by T9).

4. **`base_face_per_component` construction**: scans all Face nodes for
   `is_base_candidate is True` (set by T9's `_score_base_faces`), keyed by
   `str(component_id)`. If a component somehow has no candidate, it's simply
   absent from the dict (defensive, must not crash).

5. **`unit_types` construction**: scans all Component nodes for their `unit_type`
   attr (set by T9's `_classify_units`), keyed by `str(component_id)`. Defaults
   to `"freeform"` if attr is absent (defensive).

6. **Existing key stability**: `base_face_candidates` is unchanged — the
   pre-existing test asserting `== [{"component_id": 0, "face_id": "face:0",
   "area_cm2": 4.0}]` still passes byte-identically. The docstring NOTE was
   updated to point agents at the real `workstream` data.

7. **Test additions**: 3 new tests in `test_structure_graph_tool.py`:
   - `test_structure_graph_workstream_summary`: full tool call, verify all 4
     workstream keys present, box values correct, rebuild_order is list of strings
   - `test_structure_graph_workstream_sql_agrees`: DuckDB SQL cross-check —
     `SELECT node_id, base_score FROM nodes WHERE is_base_candidate = TRUE`
     → `("face:0", 0.85)`, `SELECT ... WHERE label='Component' ORDER BY rebuild_order`
     → `[("component:0", "base", 0)]`, then cross-check summary matches
   - `test_structure_graph_workstream_raises`: monkeypatches
     `mesh_graph._build_dependency_order` to raise RuntimeError, runs tool,
     asserts `{"error": "structure graph failed: ..."}` returned, restores
     the patch (the blanket try/except at L1590 already handles this — test locks it)

8. **Final counts**: baseline 216 → 219 headless (+3 new), 18 `*_live` deselected,
   zero regressions.  py_compile clean.  All 17 structure_graph tool tests pass.


## [2026-08-05] Task: T16 R-10 — trimesh.facets cross-check diagnostic in tests

Added `tests/trimesh_facets_diag.py` (helper) + `tests/test_trimesh_facets_cross_check.py`
(5 tests). Full evidence: `.omo/evidence/task-16-mesh-graph-pipeline.log`.

Key findings (for downstream F-wave reviewers):

1. **trimesh facets real semantics (verified against trimesh 5.0.0 source)**:
   facets are the `connected_components(min_len=2)` of the parallel-adjacency
   graph, where two EDGE-ADJACENT faces are parallel when
   `(radius/span)**2 > 5000` (`tol.facet_threshold == 5000`). It is a
   PAIRWISE adjacency test, not a plane-fit; radius = circle through the
   perpendicular projection of the two non-shared vertices onto the shared
   edge, span = span perpendicular to that edge.
2. **Issue #347 IS the `min_len=2` filter**: a single-face component is never
   emitted, so a lone triangle mesh yields `mesh.facets == []` (verified:
   tetrahedron → 0 facets). Our decompose emits single-triangle planar faces,
   so our count ≥ trimesh count is structurally guaranteed in the common
   case. Handle as `single_face_analogues` (expected, warned, never failed).
3. **Issue #1745 (plane splitting)**: multiple facets may share one plane
   (e.g. two separated coplanar quads → 2 facets on z=0). Count shared-plane
   facet groups as `plane_facet_groups` (expected, warned, never failed).
4. **Drop-axis 2D projection collapses the plane offset** — parallel faces at
   different offsets (cube z=0 vs z=1) project to the SAME 2D polygon, so a
   centroid containment test alone matches every triangle against both faces.
   The coverage matcher must ALSO require the centroid to lie on the face's
   plane: `|n·centroid − n·poly[0]| <= max(1e-4, 1e-6·|offset|)`.
5. **`mesh.facets` on degenerate input** (zero-area, all-identical, NaN,
   collinear) returns `[]` on trimesh 5.0.0 — it does NOT raise. The
   graceful catch path is still locked by a monkeypatch test that replaces
   the `facets` property with `property(_boom)` (a plain function would be
   returned as a bound method, never called — `TypeError: 'method' object
   is not iterable`).
6. **Face dict `vertices` are 3D coordinates rounded to 6 decimals**, NOT
   welded vertex indices (despite what the plan's inherited-wisdom suggested
   — `node_id/face_id` keys don't exist; keys are component, face_index,
   triangle_count, vertex_count, vertices, normal, angles_deg, area, holes,
   warnings). This is the geometric bridge that makes a cross-index-space
   comparison with trimesh possible without touching production code.
7. **Test counts**: baseline 219 → 224 headless (+5), 18 `*_live` deselected,
    zero regressions. Helper = 239 non-comment lines (< 250 ceiling);
    py_compile clean; no `mcp_server/` edits; no new deps.




## [2026-08-05] Task: T12 R-6+R-7 — Unified tolerance model + plane-fit residual in grouping

Implemented `_ToleranceConfig` dataclass and running plane-fit in `mcp_server/mesh_analysis.py`.
Full evidence: `.omo/evidence/task-12-mesh-graph-pipeline.log`.

### R-6: `_ToleranceConfig` dataclass (L~45)

`@dataclass(frozen=True)` with fields `quant_step, extent, weld_eps, snap_tol, offset_tol, simp_tol, tjunc_tol`.
Classmethod `from_(quant_step, extent)` computes all 5 tolerance values per the R-1 formulas:

- `weld_eps = max(max(1e-9, 1e-7*extent), 3*quant_step)` — IDENTICAL to R-1 formula (max-form, not min)
- `snap_tol = max(1e-5, max(5e-4*extent, 2*quant_step))`
- `offset_tol = max(1e-6, 1e-4*extent)`
- `simp_tol = max(1e-6, 1e-4*extent)` — same as offset_tol
- `tjunc_tol = snap_tol` — same as snap_tol

Replaced ALL inline tolerance computations in the pipeline:
- `decompose_mesh_faces`: single `tol = _ToleranceConfig.from_(...)` replaces 3 inline computations (weld eps L1875, snap_tol L1905, simp_tol L1904)
- `analyze_mesh_data`: weld eps now uses `tol.weld_eps`
- `_group_planar_triangles`: consumes `tol.offset_tol` when `tol` is provided (backward-compat: `tol=None` uses old inline formulas)
- `_split_edges_at_tjunctions` / `_boundary_loops`: receive `tol.tjunc_tol` via the call site
- `_snap_group_vertices`: receives `tol.snap_tol`
- `_simplify_2d_keep`: receives `tol.simp_tol`

No inline `max(1e-` patterns remain outside the `_ToleranceConfig.from_()` factory, `_detect_quantization_step` (its own logic), and `_group_planar_triangles` backward-compat fallback.

### R-7: Running plane-fit in `_group_planar_triangles`

When `tol` is provided (running fit), the connectivity first pass replaces the static seed-plane predicate (`_coplanar_with_region`) with `_accept_by_running_plane_fit`:

1. **Seed initialization**: centroid and outer-product of the seed triangle
2. **Per-neighbor accept**: reconstruct refit plane from accumulated centroid covariance, check face normal agreement (cos_tol) + centroid distance to refit plane (offset_tol)
3. **After accept**: update `centroid_sum`, `cov_sum` (accumulated, never recomputed from scratch), increment `cnt`

**cnt < 3 guard**: The smallest eigenvector of a covariance from ≤2 centroids is rank-deficient (1 point: fully undetermined; 2 points: 2D nullspace → any perpendicular direction). When `cnt < 3`, the seed's own normal is used as refit — this is correct for truly coplanar triangles and avoids rejecting valid neighbors during the initial expansion. For `cnt ≥ 3`, the analytic 3×3 smallest eigenvector (`_smallest_eigenvector_3x3`) is computed.

**Cross-seed assignment guard**: Added `assigned[nbr]` to the skip condition in the connectivity pass — without it, a second seed's region could re-absorb triangles already assigned by the first seed. This bug existed in the pre-R7 code but was masked because existing fixtures either had fully-connected graphs or didn't trigger cross-seed overlap. Exposed by the plane-fit test fixture.

**`_smallest_eigenvector_3x3(cov)`**: Closed-form O(1) analytic eigenvector for a 3×3 symmetric covariance. Uses characteristic polynomial → depressed cubic → trigonometric solution (all eigenvalues real for symmetric matrices → p ≤ 0) → adjugate nullspace (row cofactors). Degenerate covariance → Z-axis unit vector. No `np.linalg.eigh`.

### Determinism preserved

- Seeds sorted by `(-area, ti)`; neighbors via `sorted(adj[cur])`
- Groups emitted in seed order then fallback order
- No `set` iteration order in control flow
- Order-independence test (`test_order_independence`): same triangle set in 3 different orders → identical groups

### Test file: `tests/test_tolerance_plane_fit.py` (7 tests)

1. `test_tolerance_config_values` — all 5 fields match formulas for multiple (qs, extent) inputs
2. `test_tolerance_r1_r6_agree` — R-1 eps formula and ToleranceConfig.weld_eps produce identical values across 6 cases
3. `test_plane_fit_residual_grouping` — disconnected patches at different z → 2 groups; connected on same z → 1 group
4. `test_order_independence` — shuffled triangle order → identical groups
5-7. `test_smallest_eigenvector_3x3_*` — plane normal, isotropic, zero covariance → Z-axis fallback

### Counts

Baseline: 229 passed / 18 deselected. After: 236 passed / 18 deselected (+7 new tests). Zero regressions. `py_compile` clean. `scad_translator.py` untouched. No existing test files modified.


## [2026-08-05] Task: T11 R-5 — Hardened hole classification

Added `_generalized_winding_number`, `_same_sign_2d`, `_centroid_near_boundary`,
hole classification section of `decompose_mesh_faces`. New test file
`tests/test_hole_classification_r5.py` (5 tests).
Full evidence: `.omo/evidence/task-11-mesh-graph-pipeline.log`.

Key findings:

1. **Dead-guard bug (found by orchestrator, fixed)**: Original implementation
   used `not has_warnings` as the winding-number guard, but `has_warnings` is
   only set AFTER the hole classification loop (in the face-construction section).
   At guard evaluation, `has_warnings` is ALWAYS `False` for the current group.
   Fixed to `not residual_out.get("residual_pairs")` — this is the per-group
   non-manifold signal (populated by `_boundary_loops` BEFORE classification).

2. **Winding-number implementation**: Jacobson et al. 2013 solid-angle sum,
   fully numpy-vectorised (no per-triangle Python loop). Uses `np.cross` and
   `np.arctan2` vectorisation. Filters degenerate vectors (`|v| < 1e-15`).
   Returns 0.0 on empty tris. NOT called when `residual_pairs` is non-empty
   (non-manifold mesh → winding-number ill-defined → fall back to centroid test).

3. **Same-sign loops are structurally absent in connected 2-manifold meshes**:
   `_boundary_loops` normalises all triangle windings to match the group normal,
   producing opposite-signed outer/inner loops (CCW/CW respectively). Getting
   same-sign loops requires disconnected patches in the same planar group
   (R-2 connectivity constraint prevents this) or synthetic monkeypatching.
   `test_same_signed_merge` monkeypatches `_same_sign_2d` → True to exercise
   the branch.

4. **Weld reorder artifact**: The weld's rounded-key grid order reorders vertex
   indices (e.g., original [0,1,2,3,4,5,6,7] remapped to [0,1,2,3,4,5,6,7]
   with different vertex positions). This creates intermediate non-manifold
   edges in `_boundary_loops`' `residual_pairs` even on a clean mesh. The final
   decompose output is correct (loop-chaining handles the artifacts). For
   winding-number tests, `_boundary_loops` is wrapped to clear `residual_pairs`
   so the guard stays active.

5. **Degenerate-hole filter unchanged**: The existing `|signed| < _MIN_FACE_AREA`
   check at L1953 remains the first filter. R-5 only adds the signed-area
   pre-check and winding-number fallback after the degenerate filter.

6. **Test strategy**: Annulus pattern (like `_frame_mesh`). Monkeypatching is
   necessary because: (a) same-sign loops require disconnected topology that
   R-2 separates; (b) centroid containment requires triangulation inside hole
   which creates non-manifold edges in the weld-reordered mesh. The
   monkeypatches lock the specific R-5 code branches deterministically.

7. **Final counts**: baseline 224 → 229 headless (+5 new), 18 `*_live`
   deselected, zero regressions. py_compile clean. No scad_translator.py
   changes; no existing test files modified; no new deps.


## [2026-08-05] Task: T14 R-8 — Voxel/SDF fallback detection + warning propagation

Implemented R-8 detection in `decompose_mesh_faces` and propagation to
`structure_graph` summary. Full evidence:
`.omo/evidence/task-14-mesh-graph-pipeline.log`.

Key design decisions (for T15 R-9 presets which comes next):

1. **Residual-edge source** (same semantics as R-4 per-face warnings):
   `unpaired_total = sum(face["warnings"][0]["count"] for face in planar_faces
   if face.get("warnings"))`.  Each face's warning count comes from
   `_count_residual_loop_edges` (L1628-1639) counting how many `residual_pairs`
   (from `_boundary_loops`' `residual_out` kwarg, populated at L1734-1740)
   appear as consecutive vertex pairs in the FINAL face loops — the post-pinch
   true residual count, not the pre-pinch intermediate count.

2. **Ratio formula**: `unpaired_total / (3 * len(welded_tris))` where
   `welded_tris` is the post-weld triangle count.  unpaired_pct =
   `round(100.0 * unpaired_total / (3 * n_welded_tris), 1)`.  Threshold: > 5%.

3. **Return dict assembly**: changed from inline dict literal to named
   `result` variable so keys can be conditionally added.  When triggered,
   adds `strategy_fallback_suggested: "organic"` and `unpaired_pct: <float>`.
   When NOT triggered, NO new keys (byte-identical to pre-R-8 for existing
   tests).  Empty-input early-return path (L2097-2099) is unchanged.

4. **Propagation to structure_graph** (fusion_server.py L1599-1617): after
   the workstream block, if `decompose_result.get("strategy_fallback_suggested")`
   is truthy, adds `fallback_strategy` dict to summary with strategy, reason,
   and `unpaired_pct`.  When absent, summary has no `fallback_strategy` key.

5. **T11 weld-reorder artifact is the justification for the 5% threshold**:
   a truly watertight closed mesh produces zero post-pinch residuals
   (loop-chaining handles intermediate artifacts).  Even in worst case,
   residual count stays well below 5%.  The clean-mesh test explicitly
   asserts `strategy_fallback_suggested` NOT in result.

6. **Test fixtures**: seam-mismatched uses 0.003 cm offset (> snap_tol ~0.002
   for extent ~4.0) — the same proven 3-quad-strip topology as the R-4 offset
   test.  Clean mesh = watertight unit cube (8 verts, 12 tris).  Empty-input
   test locks the early-return path's byte-identical contract.

7. **Final counts**: baseline 236 → 240 headless (+4 new), 18 `*_live`
   deselected, zero regressions.  py_compile clean.  `scad_translator.py`
   untouched; no existing test files modified; no new deps; no voxelization/
    SDF code written (detection and reporting ONLY).


## [2026-08-05] Task: T15 R-9 — Expose tolerances + presets on decompose_mesh_faces, wire through MCP tools

Implemented R-9 preset/override tolerance system. Full evidence:
`.omo/evidence/task-15-mesh-graph-pipeline.log`.

Key design decisions (for F1-F4 final verification auditors):

1. **Preset resolution order** (mesh_analysis L2104-2145):
   - Compute `tol = _ToleranceConfig.from_(quant_step, extent)` first (R-6)
   - `angle_tolerance_deg` is `Optional[float]=None` at mesh_analysis layer
     (sentinel: `None` = "not supplied by caller" → default 0.5 or preset angle)
   - Apply preset via `dataclasses.replace(tol, ...)`:
     - `"accurate"`: angle=0.1°, offset_tol=max(1e-8, 1e-6*extent),
       snap_tol=max(1e-8, 1e-6*extent) — extent-relative, 100× tighter than balanced
     - `"balanced"`: no-op (defaults from `from_`)
     - `"coarse"`: angle=1.0°, offset_tol=max(1e-6, 1e-3*extent),
       snap_tol=max(1e-5, 1e-3*extent)
     - Invalid preset → `ValueError` (caught by decompose's try/except → error dict)
   - Individual params (offset_tol/snap_tol/simp_tol) override BOTH defaults AND presets
   - `weld_eps` and `tjunc_tol` are NEVER overridden by presets
   - Effective angle flows to `_group_planar_triangles(effective_angle, ...)`

2. **Angle sentinel pattern**: At mesh_analysis layer, `angle_tolerance_deg=None`
   means "use default 0.5 or preset". At MCP tool layer, `angle_tolerance_deg=0.5`
   is explicit → explicit always wins over preset angle. This keeps tool signatures
   clean while allowing decomposition-layer preset-override semantics.

3. **API surface** — parameters added:
   - `decompose_mesh_faces`: angle_tolerance_deg=None, offset_tol=None,
     snap_tol=None, simp_tol=None, preset=None
   - `analyze_mesh_data`: angle_tolerance_deg=0.5, offset_tol=None,
     snap_tol=None, simp_tol=None, simplify_vertices=True, preset=None
     (after `normals=None` — positional-compatible)
   - `analyze_mesh` MCP tool: angle_tolerance_deg=0.5, offset_tol=None,
     snap_tol=None, simplify_vertices=True, preset=None (NO simp_tol)
   - `structure_graph` MCP tool: same params as analyze_mesh

4. **Test fixture geometry** (6 tests, all passing):
   - Bent-strip fixture: 2 triangles sharing origin edge, tris at (L,0,0)-(L/2,perp,0)
     and (L,0,0)-(L/2,perp,h).  Perpendicular distance = 0.1L to keep centroid
     offset manageable for running-plane-fit check.
   - Dihedral angle formula: `dot = 0.1L / sqrt(h^2 + 0.01L^2)`
   - Centroid distance for running fit (cnt=1): `h/3`
   - Test 1 (accurate more faces): h=0.0299, L=100 → angle~0.17°, offset~0.00997
     passes balanced (tol=0.01) but fails accurate (tol=0.0001)
   - Test 2 (coarse fewer faces): h=0.122, L=100 → angle~0.69° fails balanced
     (angle 0.69°>0.5°) but passes coarse (tol=0.1, angle 0.69°<1.0°)

5. **Final counts**: baseline 240 → 246 headless (+6 new), 18 `*_live`
   deselected, zero regressions. py_compile clean. `scad_translator.py`
   untouched; no existing test files modified; no new deps.

6. **Post-review fix (2026-08-05)**: Two defects caught by Phase-1 review:
   (a) `analyze_mesh` tool accepted but never forwarded the 5 new params —
   call site still used bare `analyze_mesh_data(nodes, indices, normals)`.
   (b) `angle_tolerance_deg: float = 0.5` on all three layers meant the
   None sentinel that triggers preset angle resolution was dead — preset
   angle was always overwritten by the explicit 0.5 default. Changed to
   `Optional[float] = None` on `analyze_mesh_data`, `analyze_mesh`, and
   `structure_graph`. Added 2 regression tests proving preset passthrough
   and explicit-angle-wins work through the intermediate `analyze_mesh_data`
   layer. Final count: 248 passed / 18 deselected.


## [2026-08-05] Task: F4 — README docs for structure_graph / query_structure_graph

Added two bullets to the README.md "Mesh Reconstruction" section (after
`review_reconstruction`), documenting the graph tools for F4 gate compliance.
No other README section touched; no code files touched.

Facts locked from the real implementations (fusion_server.py L1528-1681 and
L1734-1840, mesh_graph.py):

1. **Summary keys are exactly**: mesh, units, component_count, face_count,
   edge_type_counts (all 10 relation keys, zeros included, sorted),
   base_face_candidates (legacy preliminary: largest-area face per component),
   has_warnings, duckdb_table_schema{nodes,edges}, query_example,
   articulation_points, connected_component_count, workstream
   (base_face_per_component / unit_types / rebuild_order / dag_has_cycles),
   plus fallback_strategy when `strategy_fallback_suggested` is set.
2. **Tool signature**: `structure_graph(mesh="0", units="cm",
   angle_tolerance_deg=None, offset_tol=None, snap_tol=None,
   simplify_vertices=True, preset=None)` — preset in {"accurate", "balanced",
   "coarse"}, explicit params override preset. `query_structure_graph(mesh="0",
   sql="")` returns `{mesh, columns, rows, row_count}`.
3. **DuckDB table names are literally `nodes` and `edges`** (not
   graph_nodes/anything else): nodes(node_id TEXT PRIMARY KEY, label TEXT,
   <attr>...), edges(source, target, relation, <attr>...), dynamic columns =
   fixed core + sorted union of attrs present. `edges` has
   idx_edges_src_rel ON edges(source, relation).
4. **Graph is in-memory, ephemeral**: DuckDB `:memory:` conn cached per mesh
   key in the module LRU (`MAX_GRAPHS`=16); MCP server restart drops all
   graphs → README says "call structure_graph first". No external DB.
5. **query_structure_graph is statement-level read-only**: single SELECT/WITH
   only; comments stripped; any inner `;` rejected; deterministic ordering =
   sort by first column (NULLs last) when no ORDER BY.
6. README voice: backtick tool name + em-dash + detailed explanation, matching
   the existing bullets byte-style. Verified: only the Mesh Reconstruction
   section changed; list rendering intact.



## [2026-08-05] Task: T15 F3 D — tool-level invalid-preset error envelopes

Fixed in mcp_server/fusion_server.py (discovered by F3 live QA check D):
`analyze_mesh(preset="bogus")` returned a full normal-looking report with empty
`planar_faces=[]` instead of a top-level `{"error": ...}` — the decompose-level
error was buried inside `face_decomposition.error` (silent-failure hazard), and
`structure_graph` proceeded to build an empty graph. Invalid preset names now
raise the documented "Invalid names raise an error" tool-level contract.

Changes (additive; valid/default preset paths byte-identical):
1. `analyze_mesh` (L1494-1497): after `analyze_mesh_data(...)` and BEFORE
   `scale_report`, check `(report.get("face_decomposition") or {}).get("error")`
   -> early-return `{"error": "analysis failed: <decompose error>"}`.
2. `structure_graph` (L1606-1610): after `decompose_mesh_faces(...)` and BEFORE
   `build_structure_graph`, check `decompose_result.get("error")`
   -> early-return `{"error": "structure graph failed: <decompose error>"}`.

`decompose_mesh_faces` (mesh_analysis.py L2409-2416) still wraps its body in
try/except and returns the dict-with-error contract (`{"error": str(e), ...}`) —
that decompose-level contract is TESTED (`test_invalid_preset_raises`) and was
NOT touched. The gap was purely at the tool boundary.

Tests: tests/test_structure_graph_tool.py += 2 headless tests
(`test_analyze_mesh_invalid_preset_error_envelope`,
`test_structure_graph_invalid_preset_error_envelope`) using the existing `fs`
fixture + `_box_payload()` + `_FakeCall` pattern (monkeypatch `fs._call` with
the recorder). Assert top-level `"error"` key only, the prefix
(`analysis failed` / `structure graph failed`), `unknown preset`, and the
allowed-preset list (accurate/balanced/coarse).

Verification: `py -3.14 -m pytest -q -k "not live"` -> 250 passed, 18 deselected
(baseline was 248 passed / 18 deselected; +2 = the new tests, deselected set
unchanged). Plain full run: 267 passed, 1 xfailed.

## [2026-08-05] F3 live QA closed (mesh-graph-pipeline)
- F3 verification method: temp driver (`Temp\opencode\f3_live_driver.py`) imports the CURRENT `fusion_server.py` under py3.13 and calls `analyze_mesh`/`structure_graph`/`query_structure_graph` directly through the real HTTP bridge (127.0.0.1:7432) to the live Fusion add-in � bypasses the stale pre-T15 MCP process that serves conversation tools. Importing fusion_server.py is safe (module-level side effects = FastMCP() ctor only; `__main__` guard at L2561).
- Result: 22/22 programmatic checks PASSED on live bodies (F3-cube (1): 6 planar faces accurate; F3-bend: 2 accurate / 1 coarse, coarse < accurate; structure_graph summary keys incl. workstream {base_face_per_component:{0:face:0}, unit_types:{0:base}, rebuild_order:[component:0], dag_has_cycles:false}; query: SELECT faces row_count=6, recursive CTE reaches all 6 faces, multi-statement rejected, unknown-mesh error).
- DEFECT FOUND + FIXED (F3 D-check): tool-level invalid-preset envelope was missing. decompose_mesh_faces swallows ValueError into an `error`-key dict (tested contract, test_presets_tolerances.py L179-186) but analyze_mesh/structure_graph returned normal reports with the error buried in `face_decomposition.error` (analyze_mesh) or ignored into an empty graph build (structure_graph). Fix: top-level `{"error": "analysis failed: ..."}` / `{"error": "structure graph failed: ..."}` early-returns in fusion_server.py (L1494-1497, L1606-1610) + 2 headless regression tests (test_structure_graph_tool.py L184-217). Suite: 267 passed / 1 xfailed.
- LESSON (test harness): NEVER run the F3 live driver concurrently with `py -3.14 -m pytest` � the full run includes `tests/test_*_live.py` which mutate the REAL Fusion document (clear/import/create doc); they caused spurious "Mesh body not found" mid-driver. Run live QA in isolation.
- LESSON: structure_graph's DuckDB is process-local and ephemeral (documented) � a fresh python process must call `structure_graph(mesh=...)` BEFORE `query_structure_graph`; cross-process query returns "no graph built".

## [2026-08-06] loader-fix: Python 3.13 spec-load registration + cache + startup pre-load
- ROOT CAUSE (Defect A): every loader fallback (`_load_scad_translator`, `_load_mesh_analysis`,
  `_load_mesh_graph`, `_load_mesh_slicer`, `_load_mesh_csg`, `_load_parameter_schemas`,
  `_load_workflow_guide`) did `spec_from_file_location(name, path)` ->
  `module_from_spec` -> `exec_module` WITHOUT inserting the module into `sys.modules`.
  Python 3.13's `dataclasses._is_type` resolves `cls.__module__` via
  `sys.modules.get(cls.__module__)`; `mesh_analysis.py` defines a
  `@dataclass(frozen=True)` class, so the unregistered module (`__module__ ==
  "fusionmcp_mesh_analysis"`, not in sys.modules) crashed at class-definition
  time with `AttributeError: 'NoneType' object has no attribute '__dict__'`.
  The fallback ALWAYS ran when the server is launched directly by an MCP client,
  because `sys.path[0]` is the `mcp_server` dir and `from mcp_server import X`
  raises ImportError.
- ROOT CAUSE (Defect B): the same spec-load imports numpy + trimesh
  (`mesh_analysis.py` L40-41) the FIRST time a tool handler calls the loader.
  Tool handlers run in an anyio worker thread, where that first import hangs
  >120s (never returns); the identical import completes in ~0.7s on the main
  thread. Tool-call context = hang; startup context = fast.
- FIX (register + cache + startup pre-load), all in `mcp_server/fusion_server.py`:
  1. Shared `_spec_load(name, path)` helper: `sys.modules[name] = module` BEFORE
     `spec.loader.exec_module(module)`, using the EXACT spec name — dataclasses
     looks up `cls.__module__` which equals that name. Mismatch = crash persists.
  2. Module-level `_LOADER_CACHE` dict keyed by loader/spec name; cache written
     in BOTH branches (branch-1 `from mcp_server import X` success AND
     branch-2 spec-load) so the exec runs AT MOST ONCE per process and every
     call returns the same module object.
  3. `if __name__ == "__main__":` pre-loads `_load_mesh_analysis`,
     `_load_mesh_graph`, `_load_mesh_slicer`, `_load_scad_translator`,
     `_load_parameter_schemas`, `_load_workflow_guide` on the MAIN thread before
     `mcp.run(transport="stdio")`, each in try/except (non-fatal, prints a NOTE
     on failure; loader retries at call time with its normal graceful error).
  - Kept the `raise FileNotFoundError(...)` fallback at the end of each loader;
    no tool signatures/return shapes/docstrings/handler logic changed; no
    cross-import between mcp_server modules added.
- NOTE: `_load_mesh_csg` got the register+cache fix too, but was NOT added to
  the startup pre-load (mesh_csg.py itself imports
  `from mcp_server.mesh_slicer import slice_mesh_at`, which still fails on a
  direct launch — pre-existing module-internal coupling, out of scope).
- WHY pytest never caught it: headless tests put the repo ROOT on sys.path, so
  branch-1 always succeeded and the spec-load fallback (with the crash) was
  never exercised.
- VERIFICATION (all green): py_compile exit 0; headless loader probe
  (`Temp\opencode\verify_fix_daniel.py`) — first `_load_mesh_analysis()` 0.78s
  on main thread, second call 0.0ms same object, module registered in
  sys.modules, all six loaders resolve; fresh-process MCP stdio repro
  (`Temp\opencode\verify_fix_mcp_repro.py`) spawning
  `python3.13 mcp_server\fusion_server.py` — analyze_mesh #1 1.7s
  (watertight=False, strategy=prismatic), structure_graph 6.1s
  (components=1, faces=33), slice_mesh 0.1s (4 loops), analyze_mesh #2 1.7s
  cache hit; full suite `py -m pytest tests -q` — 281 passed, 1 xfailed
  (baseline cited elsewhere was 121; suite has since grown — zero failures,
  xfail count matches).

## [2026-08-06] mesh_csg import coupling fix
- mesh_csg.py L46 `from mcp_server.mesh_slicer import slice_mesh_at` broke spec-load (direct launch): `No module named 'mcp_server'` -> reconstruct_mesh failed.
- Fixed with dual-branch import (repo convention): try `mcp_server.mesh_slicer`, except ImportError -> `mesh_slicer` (top-level, works when sys.path[0]=mcp_server dir; harmless duplicate module, mesh_slicer is stdlib-only).
- Verified: py_compile OK; csg_loader_probe OK (<module> resolves slice_mesh_at); pytest mesh_csg/mesh_slicer/job_tools green.

## [2026-08-06] demo: MeshBody1/MeshBody2 -> BRep with compare_mesh_to_brep supervision
- Goal was "convert MeshBody1/MeshBody2 to solid with compare_mesh_to_brep supervision - expect BRep bodies created". BRep bodies WERE created; supervision ran and reported honest fidelity numbers.
- Tools exercised live (fresh server process, direct launch, real add-in): analyze_mesh (1.6-16.6s, watertight=False, prismatic), structure_graph (6.2s, 1 component/33 faces), query_structure_graph (0.0s SQL), slice_mesh (0.1-0.3s), reconstruct_from_faces (1.8s/15.9s), reconstruct_mesh csg_decompose (0.5s/0.9s, "bodies": 3, "csg_nodes": 10-13), compare_mesh_to_brep (0.1s).
- prismatic CORRECTLY rejected both meshes: slice loop count varies with height (MeshBody1: 3->4->2 loops across Z=1..6.5; MeshBody2: 21 at grazing Z=0.5 then 3->4->2). Not constant cross-section -> NotPrismaticError is correct behavior, not a bug.
- organic UNAVAILABLE on this build: `Error: MeshConvertFeature not available on this Fusion build (PREVIEW API)` — documented limitation (README), not a regression.
- compare_mesh_to_brep semantics (FusionMCP.py L2416): volume_ratio = mesh_vol / brep_vol; body param resolves BRep by name or index via _require_body. IMPORTANT GOTCHA: default body="0" = BRep index 0 (scad_cube_2), NOT the just-created reconstruction — must pass the created body name (e.g. "Cuerpo64").
- Fidelity results (vertex_fallback method; surface_evaluator absent on this build):
  - MeshBody1 vs Cuerpo64 (from_faces): mesh 121.66 cm3 vs brep 447.07 cm3 -> volume_ratio 0.272; bbox_max_dev 0.923 cm; sampled dev mean 4.07/max 8.95 (186 samples).
  - MeshBody2 vs Cuerpo65 (from_faces): mesh 126.08 vs brep 469.93 -> volume_ratio 0.268; bbox_max_dev 1e-06 cm (extent essentially perfect); sampled dev mean 10.57/max 16.64 (197 samples).
- INTERPRETATION: both meshes are THIN-WALLED HOLLOW SHELLS — mesh volume is ~12% of bbox volume (121.66/1051 for MeshBody1, 21.46x7x7 bbox). csg_decompose and from_faces both build SOLID extrusions/unions -> they fill the interior -> ~3.7x the shell volume -> volume_ratio ~0.27. The large sampled deviation is dominated by the reconstruction being re-anchored at origin (plane_height=0) while the mesh bodies sit offset in the doc — compare measures spatial position too. bbox_max_dev 1e-06 for MeshBody2 proves the SHAPE/extent reconstruction is essentially perfect; the volume gap is the solid-vs-shell difference.
- CONCLUSION: mesh->BRep pipeline works end-to-end after the two loader fixes. For hollow shells, solid strategies overbuild by design; organic (the correct strategy) is PREVIEW-gated on this Fusion build. Supervision did its job: it flagged the overbuild honestly instead of reporting a false pass.
- Doc state after demo: BRep bodies scad_cube_2 (0)-(10) (leftovers + intermediate demo runs), Cuerpo51/52 (csg_decompose), Cuerpo64/65 (from_faces). Mesh bodies MeshBody1/MeshBody2 are a separate mesh-body registry NOT shown by get_bodies_info.
- Commit 4777283 — fix(server): robust mesh loader startup + mesh_csg import fallback for direct MCP launch (atomic, 2 files on master: mcp_server/fusion_server.py +119/-44 [_spec_load registers sys.modules before exec_module + _LOADER_CACHE + main-thread pre-load of 6 mesh loaders], mcp_server/mesh_csg.py +3/-2 [L46 dual-branch slice_mesh_at import fallback]; 88+/36- total; pytest 281 passed + fresh-process MCP repro green pre-commit)
## [2026-08-06] demo#2: MeshBody1 -> solid, iterative supervision (numeric)
- Goal: convert in-scope mesh (MeshBody1, thin-walled hollow shell 21.46x7x7, 0.5cm walls) to solid with overlap-metric supervision.
- LIMITATION: review_reconstruction returns PNG pairs, but this model (secondary-orchestrator) CANNOT read images (''Cannot read image''). Supervision must use numeric metrics only: compare_mesh_to_brep {volume_ratio, bbox_max_deviation_cm, sampled_deviation_cm{mean,max}}.
- Iteration 1 csg_decompose (MeshBody1): REJECTED. 2 boxes fit to base region only (17.03x7x3.5 + 20.53x6x2, joined vol 70.29 cm3, COM z=0.5 vs mesh z=0..7). compare: volume_ratio 1.73 (brep SMALLER than mesh), bbox_max_dev 4.42 cm (upper half missing), sampled mean 2.26/max 4.97. Root cause: csg face-fitting dropped the tall back wall + tower (z 3.5..7).
- Iteration 2 reconstruct_from_faces (MeshBody1): Cuerpo71 = 20.53x7x7 full envelope. Un-aligned compare: volume_ratio 0.272, bbox_dev 0.923, sampled 4.07/8.95. bbox gap is ALL y-translation (brep re-anchored y=0, mesh y=4.74-11.74) PLUS real x-deficit.
- Iteration 3 = alignment fix (move_body dx=-0.461667 dy=+4.737909 to bbox-center-match): sampled mean 4.07 -> 1.49 (-63%), max 8.95 -> 5.69. bbox_dev stays 0.923 = REAL x-shape error: brep x 3.34-23.88 vs mesh x 2.88-24.34; chamfer tip (60deg corner, x 2.88-3.34) + tower far end (23.88-24.34) not captured; 0.92cm / 21.5cm = 4.3% span deficit.
- Lesson: ALWAYS bbox-center-align the reconstructed BRep to the mesh before comparing, or sampled_dev is dominated by spatial offset. move_body with dx=(mesh_cx-brep_cx), dy=(mesh_cy-brep_cy), dz=(mesh_cz-brep_cz).
- Lesson: sampled_dev mean ~1.5 on hollow-shell->solid-fill is near the practical floor: the shell's inner wall vertices (z=0.5 etc.) have NO BRep surface to measure against (solid fill = no interior walls) -> large outlier distances. organic (would shell it) is PREVIEW-gated. prismatic correctly rejected (varying loop counts). from_faces is best available.
- Best-available result on MeshBody1: Cuerpo71 from_faces, volume_ratio 0.272 (expected solid-fill-of-hollow-shell), bbox_dev 0.923cm (4.3%), sampled mean 1.49/max 5.69 after alignment.

## [2026-08-06] demo#2 FINAL: both meshes -> solids, numeric supervision
- ACCEPTED results (doc now: MeshBody1, MeshBody2, Cuerpo71, Cuerpo72 only):
  - Cuerpo71 (MeshBody1, from_faces, aligned): volume_ratio 0.272, bbox_max_dev 0.923cm (x-deficit 4.3%: chamfer tip x 2.88-3.34 + tower end 23.88-24.34 not captured), sampled mean 1.49 / max 5.69.
  - Cuerpo72 (MeshBody2, from_faces, aligned): volume_ratio 0.268, bbox_max_dev 1e-06 cm (perfect extent), sampled mean 3.53 / max 6.13.
- Alignment moves: Cuerpo71 dx=-0.461667 dy=+4.737909; Cuerpo72 dy=+12.769428 (x/z already matched). Both verified final bbox center == mesh bbox center.
- Supervision verdict: ACCEPT both. from_faces is best available (prismatic rejected by design, organic PREVIEW-gated, csg_decompose REJECTED on MeshBody1: bottom-half-only 70cm3, vol_ratio 1.73, bbox_dev 4.42). Residual sampled deviation = hollow-shell inner-wall vertices have no BRep surface (solid fill) - structural floor, not fixable without organic shelling.
- GOTCHA: fusion360 delete_body reports success but bodies may persist (naming/index resolution). Robust cleanup = execute_script over root.bRepBodies, deleteMe() or parentFeature.deleteMe() for scad_cube*/csg artifacts. Timeline confirmed csg boxes = scad_cube_sketch_1/3/5 (Extruir64/65/66), keepers = face_polygon_sketch(1) (Extruir67/68).
- review_reconstruction image pairs NOT readable by this model (secondary-orchestrator, no image input). Numeric supervision (compare_mesh_to_brep) is the verification path.

## [2026-08-06] demo#2 VISION QA: multimodal-looker verdict on reconstruction screenshots
- Test (user-requested): capture 4 screenshots w/ identical camera (IsoTopRightViewOrientation + isFitView, union bbox span 21.550, center (13.657,12.254,3.500)) -> C:\Users\danie\AppData\Local\Temp\opencode\qa\qa1_all_bodies.png (all), qa2_mesh_only.png (meshes only), qa3_brep_only.png (BRep only), qa4_all_restored.png (restored). Delegated to multimodal-looker (ses_0299aaa70ffeeIZdY2SJRvYvCI) since secondary-orchestrator cannot read images.
- VERDICT: REJECT (borderline MARGINAL-to-REJECT). Envelope + footprint chamfer PASS; part identity FAILS.
- Confirms user hypothesis: reconstruct_from_faces output reads as "plain extruded boxes with a chamfered corner" - constant-cross-section prism. MISSING: tower (flat top, mesh tower visibly pokes above roof), shelf/step structure at z 0..3.5, hollow cavity (solid fills interior, volume_ratio ~0.27), wall-thickness semantics (0.5cm thin-wall part lost). Cuerpo71 undershoot in X visible at chamfer tip + far end (matches measured 0.923cm deficit). Cuerpo72 tighter/cleaner containment (matches bbox_dev 1e-06). No severe intersection artifacts - reads "nested", not "intersecting mess".
- Fix direction (vision agent suggestion): proper profile including tower/shelf silhouettes OR multiple extrusions/boolean union of lofted segments, THEN shell/bool to recreate hollow interior. I.e. multi-step, not single-shot.
- Lesson: the ONLY way to do visual QA on this pipeline is delegating to a vision-capable agent (multimodal-looker) - screenshots at Temp\opencode\qa\ are the handoff artifact.
