"""NetworkX property-graph builder over ``decompose_mesh_faces`` output.

``build_structure_graph(decompose_result)`` consumes the decompose dict
(``components_detected``, ``planar_faces``, ``curved_patches``,
``has_warnings``) and returns a deterministic ``networkx.Graph``.

Nodes
-----
``face:N``         label "Face"  — one per planar face.  Attrs: node_id,
                   label, component_id, face_index, plane_id (unique per
                   plane normal+offset, format ``nx|ny|nz|offset`` rounded
                   6dp), area_cm2, normal_x/y/z, centroid (3-float list),
                   vertex_count, triangle_count, interior_angles (list),
                   convexity, is_base_candidate, is_articulation_point,
                   mean_curvature, curve_type.  ``convexity``: "planar" for
                   triangles (always convex), "concave" if any interior
                   angle > 180deg, else "convex".  NOTE decompose's
                   ``angles_deg`` come from ``acos`` (SVD best-fit plane) so
                   they are capped at 180deg — "concave" only appears for
                   input that carries reflex angles.  ``is_base_candidate``
                   and ``is_articulation_point`` start False; T9 / T7 set
                   them.
``hole:N``         label "Hole"  — one per face hole (global index, face
                   order then hole order).  Attrs: node_id, label,
                   containing_face_id, area_cm2 (Newell 3D polygon area),
                   is_filled (always False; decompose reports holes as open
                   point loops).
``curved:N``       label "CurvedPatch" — one per curved patch
                   (``patch_index``).  Attrs: node_id, label, component_id,
                   triangle_count, curve_type (the patch ``surface_type``:
                   cylinder/sphere/cone/freeform), area_cm2.
``component:N``    label "Component" — one per detected component plus any
                   component id referenced by a face/patch.  Attrs: node_id,
                   label, component_id, face_count.

Edges (each carries ``relation``; every node pair has at most one edge)
-----------------------------------------------------------------------
``EDGE_ADJACENT``  Face–Face sharing a real polygon edge.  Built from a
                   vertex→face map (rounded-6dp vertex tuple → faces) and
                   verified: at least two shared vertices that are
                   CONSECUTIVE (wrap-around allowed) in BOTH ordered
                   polygons.  Pairs sharing only non-consecutive vertices
                   (e.g. diagonal-only) become ``VERTEX_TOUCH``.  Attrs:
                   dihedral_angle_deg (acos of the clamped normal dot, 0 =
                   coplanar same direction, 180 = coplanar opposite),
                   convexity ("coplanar" when |n1.n2| >= 1-1e-6, else
                   "concave" when the sum of the two interior angles at the
                   shared edge exceeds 360deg — requires a reflex angle in
                   the input — else "convex"; angles taken at the first
                   shared-edge endpoint, equal to the endpoint average for a
                   straight shared edge), shared_edge_length (span between
                   the first and last shared vertices along face1's polygon
                   = the full shared edge when the shared region is
                   contiguous), orientation ("FORWARD" when face2 traverses
                   the edge in face1's direction, else "REVERSED").
``VERTEX_TOUCH``   Face–Face sharing >=1 vertex but no consecutive pair.
                   Attrs: shared_vertex (lexicographically first shared
                   coord), shared_vertex_count.
``COPLANAR``       Face–Face in the same plane: |n1.n2| >= cos(1deg) AND
                   |offset1-offset2| <= 1e-4*extent.  Attr: normal_dot.
``SAME_ORIENTATION`` Face–Face whose canonical normals (flipped so the
                   dominant component is positive, matching decompose) are
                   equal after rounding, and NOT coplanar — covers same-way
                   and opposite-way parallel planes (box top/bottom).  Attr:
                   normal_x/y/z.
``PARALLEL``       Face–Face nearly parallel: |n1.n2| >= cos(1deg), offsets
                   differ by > 1e-4*extent, canonical normals NOT equal.
                   (Exact-parallel pairs are claimed by SAME_ORIENTATION.)
                   Attr: normal_dot.
``PERPENDICULAR``  Face–Face with |n1.n2| <= cos(89deg).  Attr: normal_dot.
``CONTAINS``       Face→Hole and Face→CurvedPatch.  Patches are attributed
                   to the same-component face with the smallest face_index
                   (best-effort; decompose does not link patches to faces).
``EXTRUSION_ALIGNED`` Face–Face: both faces are part of one detected
                   extrusion structure.  Best-effort: per component, plane
                   families are paired when parallel (|n1.n2| >= cos(1deg))
                   with distinct offsets; both families must be internally
                   connected via EDGE_ADJACENT; side-wall faces (EDGE_
                   ADJACENT-perpendicular to a face of each family) must
                   contain a simple cycle (nx.cycle_basis, longest cycle
                   wins).  Emits EXTRUSION_ALIGNED for every pair of marked
                   faces (cycle + both family faces); attrs:
                   extrusion_direction (normal of the first family, rounded
                   6dp).  Because a simple Graph holds one edge per pair,
                   EXTRUSION_ALIGNED REPLACES a geometric relation
                   (COPLANAR/SAME_ORIENTATION/PARALLEL/PERPENDICULAR) on a
                   marked pair but never a physical one
                   (EDGE_ADJACENT/VERTEX_TOUCH).  Zero edges on fixtures
                   without a valid loop.
``COMPONENT_OF``   CurvedPatch → Component.  Face→Component membership is
                   carried by the HAS_BASE edge instead: in an undirected
                   Graph each face–component pair can hold only ONE edge,
                   and the spec's "emit HAS_BASE for every face" takes the
                   pair, so the face–component edge is relation HAS_BASE
                   (which also serves as the membership relation for T7's
                   component analysis).
``HAS_BASE``       Component → Face, emitted for EVERY face.  The "base"
                   designation is T9's job (via is_base_candidate); the edge
                   type is created now so the schema is stable.

Precedence
----------
Each face pair carries exactly ONE edge: the physical relations
(EDGE_ADJACENT, VERTEX_TOUCH) win over the geometric ones; among geometric
relations COPLANAR > SAME_ORIENTATION > PARALLEL > PERPENDICULAR.
EXTRUSION_ALIGNED replaces geometric relations (never physical ones) on
marked extrusion pairs, keeping the direction set by the lexicographically
first family pair for a given node pair.

Determinism
-----------
All iteration is over sorted keys/node ids; ``G.graph`` carries
``has_warnings`` and ``components_detected``.  Same input dict →
bit-identical graph.  Pure Python (math only, no numpy).  Attr values are
JSON-serialisable (lists/tuples round-trip to TEXT in the T6 DuckDB sink).

DuckDB persistence (T6)
-----------------------
``_persist_to_duckdb(G, mesh_key)`` writes the graph into an in-memory
DuckDB database and returns the connection, also caching it in the
module-level LRU ``_GRAPH_DBS`` (``MAX_GRAPHS`` = 16; on overflow the
least-recently-used connection is closed and evicted).  ``_get_graph_db(
mesh_key)`` returns the cached connection (promoting it to most-recent) or
raises ``KeyError`` — the databases are ephemeral and die with the MCP
server process.

Tables — columns beyond the fixed core are the SORTED union of attribute
keys across all nodes (resp. all edges), so the schema is derived per
persist: attributes added later (T9: base_score/unit_type/rebuild_order)
become columns with no change here:

  nodes(node_id TEXT PRIMARY KEY, label TEXT, <attr> <TYPE>, ...)
  edges(source TEXT, target TEXT, relation TEXT, <attr> <TYPE>, ...)
  CREATE INDEX IF NOT EXISTS idx_edges_src_rel ON edges(source, relation)

Per-column type mapping: bool → BOOLEAN, int → INTEGER, float → DOUBLE,
str → TEXT, list/dict/tuple → TEXT via ``json.dumps`` (round-trip with
``json.loads`` in queries), missing attribute → NULL.  Every value is bound
with a parameterized insert; the whole write (DDL + inserts) runs inside
one transaction.  Column names are validated as SQL identifiers; values are
never interpolated into SQL.
"""

import json
import math
import re
from collections import OrderedDict, defaultdict
from typing import Dict, List, Optional, Set, Tuple

import duckdb
import networkx as nx

_COS_1_DEG = math.cos(math.radians(1.0))
_COS_89_DEG = math.cos(math.radians(89.0))


# --------------------------------------------------------------------------
# tiny pure-math helpers (no numpy)
# --------------------------------------------------------------------------

def _r6(x: float) -> float:
    return round(float(x), 6)


def _dot3(a: List[float], b: List[float]) -> float:
    return float(a[0] * b[0] + a[1] * b[1] + a[2] * b[2])


def _norm3(a: List[float]) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def _dist3(p: Tuple[float, float, float], q: Tuple[float, float, float]) -> float:
    return math.sqrt((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2)


def _canonical_normal(normal: List[float]) -> Tuple[float, float, float]:
    """Flip so the dominant component is positive (decompose's convention)."""
    n = (float(normal[0]), float(normal[1]), float(normal[2]))
    dom = max(range(3), key=lambda k: abs(n[k]))
    return tuple(-c for c in n) if n[dom] < 0.0 else n


def _plane_offset(normal: List[float], vertex: Tuple[float, float, float]) -> float:
    return float(normal[0] * vertex[0] + normal[1] * vertex[1] + normal[2] * vertex[2])


def _polygon_area_3d(pts: List[Tuple[float, float, float]]) -> float:
    """Newell's method: area of a planar 3D polygon (sign-free)."""
    m = len(pts)
    if m < 3:
        return 0.0
    nxx = ny = nz = 0.0
    for i in range(m):
        x1, y1, z1 = pts[i]
        x2, y2, z2 = pts[(i + 1) % m]
        nxx += (y1 - y2) * (z1 + z2)
        ny += (z1 - z2) * (x1 + x2)
        nz += (x1 - x2) * (y1 + y2)
    return round(0.5 * math.sqrt(nxx * nxx + ny * ny + nz * nz), 6)


def _face_convexity(vertex_count: int, interior_angles: List[float]) -> str:
    if vertex_count == 3:
        return "planar"
    if any(a > 180.0 for a in interior_angles):
        return "concave"
    return "convex"


def _edge_convexity(dot_normals: float, sum_interior_angles: float) -> str:
    if abs(dot_normals) >= 1.0 - 1e-6:
        return "coplanar"
    if sum_interior_angles > 360.0:
        return "concave"
    return "convex"


def _find_shared_edge(poly_a: List[Tuple[float, float, float]],
                      poly_b: List[Tuple[float, float, float]]):
    """First consecutive shared vertex pair of poly_a that is also an edge of
    poly_b (either direction).  Returns (u, w, i_a, j_b, orientation) or None."""
    nb = len(poly_b)
    pos_b = {v: i for i, v in enumerate(poly_b)}
    na = len(poly_a)
    for i in range(na):
        u = poly_a[i]
        w = poly_a[(i + 1) % na]
        if u == w or u not in pos_b or w not in pos_b:
            continue
        j = pos_b[u]
        k = pos_b[w]
        if k == (j + 1) % nb:
            return (u, w, i, j, "FORWARD")
        if j == (k + 1) % nb:
            return (u, w, i, j, "REVERSED")
    return None


def _bounding_extent(faces: List[dict]) -> float:
    xs: List[float] = []
    ys: List[float] = []
    zs: List[float] = []
    for f in faces:
        for p in f.get("vertices", []):
            xs.append(float(p[0]))
            ys.append(float(p[1]))
            zs.append(float(p[2]))
        for hole in f.get("holes", []):
            for p in hole:
                xs.append(float(p[0]))
                ys.append(float(p[1]))
                zs.append(float(p[2]))
    if not xs:
        return 0.0
    return max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))


def _is_perpendicular(n1: List[float], n2: List[float]) -> bool:
    return abs(_dot3(n1, n2)) <= _COS_89_DEG


def _adjacency_edges(G: nx.Graph, faces: List[str]) -> List[Tuple[str, str]]:
    """EDGE_ADJACENT edges induced by a face set, sorted for determinism."""
    return sorted((a, b) for a in faces for b in faces
                  if a < b and G.has_edge(a, b)
                  and G[a][b].get("relation") == "EDGE_ADJACENT")


def _connected_via_adjacency(G: nx.Graph, faces: List[str]) -> bool:
    if len(faces) <= 1:
        return True
    sub = nx.Graph()
    sub.add_nodes_from(faces)
    sub.add_edges_from(_adjacency_edges(G, faces))
    return nx.is_connected(sub)


def _add_extrusion_aligned(G: nx.Graph, face_ids: List[str],
                           face_normal: Dict[str, List[float]],
                           face_offset: Dict[str, float],
                           face_comp: Dict[str, int],
                           plane_id: Dict[str, str],
                           tol_plane: float) -> None:
    """Best-effort EXTRUSION_ALIGNED edges (see module docstring)."""
    comp_faces: Dict[int, List[str]] = defaultdict(list)
    for fid in face_ids:
        comp_faces[face_comp[fid]].append(fid)
    for comp in sorted(comp_faces):
        families: Dict[str, List[str]] = defaultdict(list)
        for fid in comp_faces[comp]:
            families[plane_id[fid]].append(fid)
        fam_ids = sorted(families)
        for i in range(len(fam_ids)):
            for j in range(i + 1, len(fam_ids)):
                faces_a = families[fam_ids[i]]
                faces_b = families[fam_ids[j]]
                na = face_normal[faces_a[0]]
                if abs(_dot3(na, face_normal[faces_b[0]])) < _COS_1_DEG:
                    continue
                if abs(face_offset[faces_a[0]] - face_offset[faces_b[0]]) <= tol_plane:
                    continue  # same plane, not a parallel-family pair
                if not _connected_via_adjacency(G, faces_a) \
                        or not _connected_via_adjacency(G, faces_b):
                    continue
                side_walls: List[str] = []
                blocked = set(faces_a) | set(faces_b)
                for fid in comp_faces[comp]:
                    if fid in blocked:
                        continue
                    nf = face_normal[fid]
                    adj_a = any(G.has_edge(fid, g) and _is_perpendicular(nf, face_normal[g])
                                for g in faces_a)
                    adj_b = any(G.has_edge(fid, g) and _is_perpendicular(nf, face_normal[g])
                                for g in faces_b)
                    if adj_a and adj_b:
                        side_walls.append(fid)
                if len(side_walls) < 3:
                    continue
                sub = nx.Graph()
                sub.add_nodes_from(side_walls)
                sub.add_edges_from(_adjacency_edges(G, side_walls))
                cycles = nx.cycle_basis(sub)
                if not cycles:
                    continue
                best = max(cycles, key=lambda c: (len(c), sorted(c)))
                marked = sorted(set(best) | set(faces_a) | set(faces_b))
                direction = [float(c) for c in na]
                for x in range(len(marked)):
                    for y in range(x + 1, len(marked)):
                        if G.has_edge(marked[x], marked[y]):
                            rel = G[marked[x]][marked[y]].get("relation")
                            if rel in ("EDGE_ADJACENT", "VERTEX_TOUCH",
                                       "EXTRUSION_ALIGNED"):
                                continue  # never replace a physical relation
                            G.remove_edge(marked[x], marked[y])
                        G.add_edge(marked[x], marked[y], relation="EXTRUSION_ALIGNED",
                                   extrusion_direction=direction)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def build_structure_graph(decompose_result: dict) -> nx.Graph:
    """Build the deterministic property graph for a decompose output dict."""
    G = nx.Graph()
    faces = sorted(decompose_result.get("planar_faces") or [],
                   key=lambda f: int(f.get("face_index", 0)))
    patches = sorted(decompose_result.get("curved_patches") or [],
                     key=lambda p: int(p.get("patch_index", 0)))
    n_components = int(decompose_result.get("components_detected") or 0)
    G.graph["has_warnings"] = bool(decompose_result.get("has_warnings", False))
    G.graph["components_detected"] = n_components

    tol_plane = 1e-4 * _bounding_extent(faces)

    face_ids: List[str] = []
    face_normal: Dict[str, List[float]] = {}
    face_offset: Dict[str, float] = {}
    face_poly: Dict[str, List[Tuple[float, float, float]]] = {}
    face_angles: Dict[str, List[float]] = {}
    face_comp: Dict[str, int] = {}
    min_face_by_comp: Dict[int, str] = {}

    for f in faces:
        fid = f"face:{int(f['face_index'])}"
        normal = [float(c) for c in f.get("normal", [0.0, 0.0, 0.0])]
        verts = [tuple(float(c) for c in p) for p in f.get("vertices", [])]
        angles = [float(a) for a in f.get("angles_deg", [])]
        offset = _plane_offset(normal, verts[0]) if verts else 0.0
        centroid = ([sum(p[k] for p in verts) / len(verts) for k in range(3)]
                    if verts else [0.0, 0.0, 0.0])
        comp = int(f.get("component", 0))
        face_ids.append(fid)
        face_normal[fid] = normal
        face_offset[fid] = offset
        face_poly[fid] = verts
        face_angles[fid] = angles
        face_comp[fid] = comp
        min_face_by_comp.setdefault(comp, fid)
        G.add_node(fid, node_id=fid, label="Face", component_id=comp,
                   face_index=int(f.get("face_index", 0)),
                   plane_id=_plane_id(normal, offset),
                   area_cm2=_r6(f.get("area", 0.0)),
                   normal_x=normal[0], normal_y=normal[1], normal_z=normal[2],
                   centroid=[_r6(c) for c in centroid],
                   vertex_count=int(f.get("vertex_count", len(verts))),
                   triangle_count=int(f.get("triangle_count", 0)),
                   interior_angles=angles,
                   convexity=_face_convexity(len(verts), angles),
                   is_base_candidate=False, is_articulation_point=False,
                   mean_curvature=0.0, curve_type="planar")

    hole_nodes: List[Tuple[str, str]] = []  # (face_id, hole_id)
    hole_idx = 0
    for f in faces:
        fid = f"face:{int(f['face_index'])}"
        for hole in f.get("holes", []):
            hid = f"hole:{hole_idx}"
            hole_nodes.append((fid, hid))
            G.add_node(hid, node_id=hid, label="Hole",
                       containing_face_id=fid,
                       area_cm2=_polygon_area_3d(
                           [tuple(float(c) for c in p) for p in hole]),
                       is_filled=False)
            hole_idx += 1

    for p in patches:
        cid = f"curved:{int(p['patch_index'])}"
        G.add_node(cid, node_id=cid, label="CurvedPatch",
                   component_id=int(p.get("component", 0)),
                   triangle_count=int(p.get("triangle_count", 0)),
                   curve_type=str(p.get("surface_type", "freeform")),
                   area_cm2=_r6(p.get("area", 0.0)))

    face_count_by_comp: Dict[int, int] = defaultdict(int)
    for fid in face_ids:
        face_count_by_comp[face_comp[fid]] += 1
    referenced = set(face_count_by_comp)
    for p in patches:
        referenced.add(int(p.get("component", 0)))
    for comp in sorted(set(range(n_components)) | referenced):
        G.add_node(f"component:{comp}", node_id=f"component:{comp}",
                   label="Component", component_id=comp,
                   face_count=face_count_by_comp.get(comp, 0))

    # ---- physical edges: EDGE_ADJACENT / VERTEX_TOUCH ----
    vertex_to_faces: Dict[Tuple[float, float, float], Set[str]] = defaultdict(set)
    for fid in face_ids:
        for v in face_poly[fid]:
            vertex_to_faces[v].add(fid)
    candidates: Set[Tuple[str, str]] = set()
    for fid in face_ids:
        for v in face_poly[fid]:
            for other in vertex_to_faces[v]:
                if other != fid:
                    candidates.add(tuple(sorted((fid, other))))

    for a, b in sorted(candidates):
        poly_a = face_poly[a]
        shared = set(poly_a) & set(face_poly[b])
        hit = _find_shared_edge(poly_a, face_poly[b])
        if hit is None:
            G.add_edge(a, b, relation="VERTEX_TOUCH",
                       shared_vertex=sorted(shared)[0],
                       shared_vertex_count=len(shared))
            continue
        u, w, i_a, j_b, orientation = hit
        dot_n = _dot3(face_normal[a], face_normal[b])
        span = [i for i, v in enumerate(poly_a) if v in shared]
        G.add_edge(a, b, relation="EDGE_ADJACENT",
                   dihedral_angle_deg=_r6(math.degrees(
                       math.acos(max(-1.0, min(1.0, dot_n))))),
                   convexity=_edge_convexity(dot_n,
                                             face_angles[a][i_a] + face_angles[b][j_b]),
                   shared_edge_length=_r6(_dist3(poly_a[span[0]], poly_a[span[-1]])),
                   orientation=orientation)

    # ---- geometric relations (one per non-physical pair) ----
    for i in range(len(face_ids)):
        a = face_ids[i]
        for b in face_ids[i + 1:]:
            if G.has_edge(a, b):
                continue
            na = face_normal[a]
            nb = face_normal[b]
            if _norm3(na) < 1e-12 or _norm3(nb) < 1e-12:
                continue
            dot = _dot3(na, nb)
            adot = abs(dot)
            if adot >= _COS_1_DEG and abs(face_offset[a] - face_offset[b]) <= tol_plane:
                G.add_edge(a, b, relation="COPLANAR", normal_dot=_r6(dot))
            elif _canonical_normal(na) == _canonical_normal(nb):
                cn = _canonical_normal(nb)
                G.add_edge(a, b, relation="SAME_ORIENTATION",
                           normal_x=cn[0], normal_y=cn[1], normal_z=cn[2])
            elif adot >= _COS_1_DEG:
                G.add_edge(a, b, relation="PARALLEL", normal_dot=_r6(dot))
            elif adot <= _COS_89_DEG:
                G.add_edge(a, b, relation="PERPENDICULAR", normal_dot=_r6(dot))

    # ---- CONTAINS: Face->Hole, Face->CurvedPatch ----
    for fid, hid in hole_nodes:
        G.add_edge(fid, hid, relation="CONTAINS")
    for p in patches:
        cid = f"curved:{int(p['patch_index'])}"
        comp = int(p.get("component", 0))
        if comp in min_face_by_comp:
            G.add_edge(min_face_by_comp[comp], cid, relation="CONTAINS")

    # ---- EXTRUSION_ALIGNED (best-effort) ----
    plane_id = {fid: G.nodes[fid]["plane_id"] for fid in face_ids}
    _add_extrusion_aligned(G, face_ids, face_normal, face_offset, face_comp,
                           plane_id, tol_plane)

    # ---- COMPONENT_OF / HAS_BASE ----
    # Face->Component membership is carried by HAS_BASE (one edge per pair
    # in an undirected Graph); COMPONENT_OF is CurvedPatch->Component only.
    for p in patches:
        G.add_edge(f"curved:{int(p['patch_index'])}",
                   f"component:{int(p.get('component', 0))}",
                   relation="COMPONENT_OF")
    for fid in face_ids:
        G.add_edge(f"component:{face_comp[fid]}", fid, relation="HAS_BASE")
    return G


def _plane_id(normal: List[float], offset: float) -> str:
    return "{}|{}|{}|{}".format(_r6(normal[0]), _r6(normal[1]),
                                _r6(normal[2]), _r6(offset))


# --------------------------------------------------------------------------
# DuckDB persistence — in-memory sink for the structure graph (T6)
# --------------------------------------------------------------------------

MAX_GRAPHS = 16
"""Maximum number of persisted graphs kept in ``_GRAPH_DBS``."""

_GRAPH_DBS: "OrderedDict[str, duckdb.DuckDBPyConnection]" = OrderedDict()
"""LRU cache: mesh_key -> open in-memory DuckDB connection (see module
docstring).  Access order is the recency order: ``move_to_end`` on insert
and on ``_get_graph_db``; ``popitem(last=False)`` evicts the oldest."""

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str) -> str:
    """Reject attr keys that cannot be a SQL identifier (values are always
    parameterized; column names are the only thing reaching SQL text)."""
    if _IDENT_RE.match(name) is None:
        raise ValueError(f"unsafe DuckDB column name: {name!r}")
    return name


def _column_type(values: List[object]) -> str:
    """DuckDB type for one column, inferred from its non-None values.

    Priority: a JSON container (list/dict/tuple) forces TEXT (stored as a
    JSON string); bool → BOOLEAN; int → INTEGER; int/float mix → DOUBLE;
    str → TEXT; anything else falls back to TEXT.  ``bool`` is checked
    before ``int`` because ``bool`` subclasses ``int``.
    """
    present = [v for v in values if v is not None]
    if not present:
        return "TEXT"
    if any(isinstance(v, (list, dict, tuple)) for v in present):
        return "TEXT"
    if all(isinstance(v, bool) for v in present):
        return "BOOLEAN"
    if all(isinstance(v, int) for v in present):
        return "INTEGER"
    if all(isinstance(v, (int, float)) for v in present):
        return "DOUBLE"
    return "TEXT"


def _sql_value(value: object, col_type: str) -> object:
    """Normalise one attr value for a typed DuckDB parameter.

    None stays NULL; list/dict/tuple become a JSON string; an int bound to
    a DOUBLE column is widened to float.  Everything else binds natively.
    """
    if value is None:
        return None
    if isinstance(value, (list, dict, tuple)):
        return json.dumps(value)
    if col_type == "DOUBLE" and isinstance(value, int):
        return float(value)
    return value


def _tabulate(rows: List[Tuple[str, dict]],
              core: Tuple[str, ...]) -> Tuple[List[str], List[Tuple[object, ...]]]:
    """Flatten ``(id, attrs)`` pairs into a stable table layout.

    Returns ``(columns, aligned_rows)``: ``columns`` is the fixed ``core``
    followed by the SORTED union of all attr keys minus the core; each row
    is a tuple aligned to ``columns`` with ``None`` for a missing attr.
    Sorted (never insertion-ordered) so the schema is deterministic.
    """
    columns = list(core) + sorted({k for _, attrs in rows for k in attrs}
                                  - set(core))
    return columns, [tuple(attrs.get(c) for c in columns) for _, attrs in rows]


def _create_table(conn: duckdb.DuckDBPyConnection, table: str,
                  rows: List[Tuple[str, dict]], core: Tuple[str, ...],
                  primary_key: Optional[str] = None) -> None:
    """Create ``table`` from ``rows`` and bulk-insert them (parameterized).

    Column names are validated identifiers; values go through
    ``_sql_value``.  The caller owns the surrounding transaction.
    """
    columns, aligned = _tabulate(rows, core)
    col_types = {c: _column_type([r[i] for r in aligned])
                 for i, c in enumerate(columns)}
    defs = []
    for c in columns:
        _safe_ident(c)
        d = f'"{c}" {col_types[c]}'
        if c == primary_key:
            d += " PRIMARY KEY"
        defs.append(d)
    conn.execute(f"CREATE TABLE {table} ({', '.join(defs)})")
    placeholders = ", ".join("?" for _ in columns)
    normalized = [tuple(_sql_value(v, col_types[c])
                        for c, v in zip(columns, row))
                  for row in aligned]
    if normalized:
        conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})",
                         normalized)


def _persist_to_duckdb(graph: nx.Graph,
                       mesh_key: str) -> duckdb.DuckDBPyConnection:
    """Persist ``graph`` into an in-memory DuckDB database and cache it.

    Returns the connection.  The nodes/edges tables carry the graph's attrs
    as dynamically-derived columns (see the module docstring); the whole
    write is one transaction (rolls back atomically on failure).  The
    connection replaces any previously cached one for ``mesh_key`` (the old
    connection is closed first) and is moved to the most-recent end of the
    LRU; on overflow the least-recently-used connection is closed+evicted.
    """
    node_rows = [(n, dict(d)) for n, d in graph.nodes(data=True)]
    edge_rows = []
    for u, v, d in graph.edges(data=True):
        attrs = dict(d)
        attrs["source"] = u
        attrs["target"] = v
        edge_rows.append((u, attrs))

    conn = duckdb.connect(":memory:")
    conn.execute("BEGIN")
    try:
        _create_table(conn, "nodes", node_rows, ("node_id", "label"),
                      primary_key="node_id")
        _create_table(conn, "edges", edge_rows,
                      ("source", "target", "relation"))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_src_rel "
                     "ON edges(source, relation)")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        conn.close()
        raise

    if mesh_key in _GRAPH_DBS:
        _GRAPH_DBS[mesh_key].close()
    _GRAPH_DBS[mesh_key] = conn
    _GRAPH_DBS.move_to_end(mesh_key)
    while len(_GRAPH_DBS) > MAX_GRAPHS:
        _evicted_key, evicted_conn = _GRAPH_DBS.popitem(last=False)
        evicted_conn.close()
    return conn


def _get_graph_db(mesh_key: str) -> duckdb.DuckDBPyConnection:
    """Return the cached in-memory DuckDB connection for ``mesh_key``.

    Promotes the key to most-recently-used.  Raises ``KeyError`` when no
    graph has been persisted for ``mesh_key`` — graphs are ephemeral: the
    MCP server restart drops them.
    """
    if mesh_key not in _GRAPH_DBS:
        raise KeyError(
            f"no graph built for mesh '{mesh_key}'. Call structure_graph "
            "first (graphs are ephemeral: the MCP server restart drops "
            "them)")
    _GRAPH_DBS.move_to_end(mesh_key)
    return _GRAPH_DBS[mesh_key]
