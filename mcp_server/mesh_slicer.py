"""Pure-Python mesh slicer for the Fusion 360 MCP server (mesh-to-parametric
plan, Todo 3).

`slice_mesh_at()` intersects a triangle mesh with an axis-aligned plane and
returns the ordered, closed 2D cross-section loops with hole detection:

    slice_mesh_at(nodes, indices, height_or_plane)
        -> {"loops": [{"pts": [[x, y], ...], "is_hole": bool}, ...],
            "plane": {"axis": str, "height_cm": float, "origin": [x,y,z],
                      "basis": [[x,y,z], [x,y,z]]}}

Per triangle the plane-vs-triangle intersection segment is computed, all
segments are deduped and chained into closed loops via an adjacency graph,
collinear intermediate points are simplified away (Fusion's displayMesh
triangulates every quad face into 2 triangles, which would otherwise add an
extra vertex per edge), and loops are classified as outer (CCW, positive
shoelace area) vs hole (CW, negative) by containment depth.

Design constraints:
  * stdlib ONLY (math, collections) - no numpy (Fusion's embedded Python does
    not guarantee it).
  * fully DETERMINISTIC - there is no `random` module anywhere; adjacency
    lists and loop starts are visited in sorted order, so identical input
    always yields identical loops.
  * robust - empty / degenerate input returns a valid dict, never crashes;
    structurally invalid input (bad flat lengths, out-of-range triangle
    indices, unknown plane) raises ValueError with a message.

Input conventions (flat lists or lists of (x,y,z) triples, both accepted;
identical to mcp_server.mesh_analysis):
  nodes:    vertex coordinates.  Flat:  [x0,y0,z0, x1,y1,z1, ...]
  indices:  triangle corner indices. Flat: [i0,i1,i2, i0,i1,i2, ...]

The mesh may be NON-INDEXED (every triangle corner a distinct node, as in
Fusion's displayMesh): each triangle's corner triple is processed
independently and loop chaining relies purely on intersection-point dedup
(epsilon 1e-9, scaled with mesh extent).

Plane definition: axis ("X"/"Y"/"Z") + signed height (cm) along that axis, or
a bare number (height along Z).  The returned plane dict carries the origin (a
point on the plane) and two orthonormal basis vectors used to project the 3D
intersection points to the 2D loop coordinates.

Units: node coordinates are in centimeters (Fusion's internal unit).  The MCP
tool scales the loops to the requested units via `scale_slice()`.
"""

from __future__ import annotations

import copy
import math
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple, Union

Float3 = Tuple[float, float, float]

_AXIS_VECTOR = {
    "X": (1.0, 0.0, 0.0),
    "Y": (0.0, 1.0, 0.0),
    "Z": (0.0, 0.0, 1.0),
}
# Orthonormal basis spanning each axis-aligned plane; 2D loop coords are
# ((p - origin) . basis[0], (p - origin) . basis[1]).
_BASIS = {
    "X": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),   # 2D coords (y, z)
    "Y": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),   # 2D coords (x, z)
    "Z": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),   # 2D coords (x, y)
}
_DEDUP_EPS_BASE = 1e-9     # point dedup / collinear simplification tolerance
_ONPLANE_EPS_BASE = 1e-6   # distance-to-plane "on plane" tolerance (6dp data)


# --------------------------------------------------------------------------
# input normalization helpers
# --------------------------------------------------------------------------

def _as_triples(data: Sequence) -> List[Tuple[float, ...]]:
    """Return `data` as a list of (a, b, c) triples.

    Accepts a flat sequence (length % 3 == 0) or a sequence of triples.
    """
    if not data:
        return []
    first = data[0]
    if isinstance(first, (list, tuple)):
        return [tuple(float(v) for v in t) for t in data]
    if len(data) % 3 != 0:
        raise ValueError("flat input length must be divisible by 3")
    return [tuple(float(v) for v in data[i:i + 3])
            for i in range(0, len(data), 3)]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Sequence[float], b: Sequence[float]) -> Float3:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _normalize(a: Sequence[float]) -> Optional[Float3]:
    n = math.sqrt(_dot(a, a))
    if n <= 1e-12:
        return None
    return (a[0] / n, a[1] / n, a[2] / n)


def _round2(p: Sequence[float]) -> List[float]:
    return [round(float(p[0]), 6), round(float(p[1]), 6)]


def _round3(p: Sequence[float]) -> List[float]:
    return [round(float(v), 6) for v in p]


# --------------------------------------------------------------------------
# plane resolution
# --------------------------------------------------------------------------

def _build_plane(axis: str, height: float) -> Dict:
    axis = str(axis).strip().upper()
    if axis not in _AXIS_VECTOR:
        raise ValueError("Axis must be X, Y, or Z")
    origin = [0.0, 0.0, 0.0]
    origin["XYZ".index(axis)] = float(height)
    return {
        "axis": axis,
        "height_cm": round(float(height), 6),
        "origin": _round3(origin),
        "basis": [_round3(u) for u in _BASIS[axis]],
        "_normal": _AXIS_VECTOR[axis],
    }


def _resolve_plane(height_or_plane) -> Dict:
    """Normalize `height_or_plane` into an internal plane dict.

    Accepts a bare number (height along Z, cm) or a dict with "axis" and
    "height_cm" (an explicitly given "origin" + "basis" pair is honored too).
    The internal dict carries a "_normal" key used by the math, stripped from
    the public result.
    """
    if isinstance(height_or_plane, (int, float)) and not isinstance(height_or_plane, bool):
        return _build_plane("Z", float(height_or_plane))
    if isinstance(height_or_plane, dict):
        if "origin" in height_or_plane and "basis" in height_or_plane:
            origin = [float(v) for v in height_or_plane["origin"]]
            basis = [[float(v) for v in u] for u in height_or_plane["basis"]]
            normal = _normalize(_cross(basis[0], basis[1]))
            if normal is None:
                raise ValueError("plane basis vectors must be non-degenerate")
            axis = str(height_or_plane.get("axis", "Z")).strip().upper()
            if axis not in _AXIS_VECTOR:
                raise ValueError("Axis must be X, Y, or Z")
            return {
                "axis": axis,
                "height_cm": round(float(height_or_plane.get("height_cm", 0.0)), 6),
                "origin": _round3(origin),
                "basis": [_round3(u) for u in basis],
                "_normal": normal,
            }
        axis = str(height_or_plane.get("axis", "Z")).strip().upper()
        height = float(height_or_plane.get("height_cm", 0.0))
        return _build_plane(axis, height)
    raise ValueError(
        "height_or_plane must be a number (height along Z, cm) or a dict with "
        "'axis' and 'height_cm'")


def _public_plane(plane: Dict) -> Dict:
    return {k: v for k, v in plane.items() if not k.startswith("_")}


def _epsilons(node_list: Sequence[Float3]) -> Tuple[float, float]:
    """(dedup_eps, onplane_eps) scaled with mesh extent for stability."""
    if not node_list:
        return _DEDUP_EPS_BASE, _ONPLANE_EPS_BASE
    extent = max(max(p[k] for p in node_list) - min(p[k] for p in node_list)
                 for k in range(3))
    scale = max(1.0, extent)
    return _DEDUP_EPS_BASE * scale, _ONPLANE_EPS_BASE * scale


# --------------------------------------------------------------------------
# triangle-plane intersection
# --------------------------------------------------------------------------

def _project(p: Sequence[float], origin: Sequence[float],
             basis: Sequence[Sequence[float]]) -> Tuple[float, float]:
    u, v = basis
    dx = p[0] - origin[0]
    dy = p[1] - origin[1]
    dz = p[2] - origin[2]
    return (dx * u[0] + dy * u[1] + dz * u[2],
            dx * v[0] + dy * v[1] + dz * v[2])


def _triangle_plane_segment(a: Float3, b: Float3, c: Float3,
                            normal: Float3, origin: Sequence[float],
                            basis: Sequence[Sequence[float]],
                            onplane_eps: float) -> Optional[Tuple[Tuple[float, float],
                                                                  Tuple[float, float]]]:
    """2D intersection segment of triangle (a,b,c) with the plane, or None.

    Coplanar triangles (all 3 corners on the plane) and single-vertex touches
    (one corner on the plane, the other two on the same side) are skipped so
    they never emit spurious points.  An edge lying in the plane (two corners
    on it) contributes that edge's two endpoints.
    """
    corners = (a, b, c)
    raw_dists = tuple(_dot((p[0] - origin[0], p[1] - origin[1], p[2] - origin[2]),
                           normal) for p in corners)
    on = [abs(d) <= onplane_eps for d in raw_dists]
    if all(on):
        return None  # triangle lies entirely in the plane -> skip
    # Clamp near-plane (on) vertices to exactly 0 so the edge-crossing test
    # below never fires for edges touching an on-plane vertex.  Without this,
    # a vertex at distance ~1e-7 (within onplane_eps but nonzero) causes both
    # a spurious interpolated crossing AND the on-plane-corner point,
    # producing segment endpoints that differ between adjacent triangles and
    # break loop chaining (thin-band reliability bug, T7).
    dists = tuple(0.0 if on[k] else raw_dists[k] for k in range(3))
    pts = []
    # proper edge crossings (endpoints strictly on opposite sides)
    for (i, j) in ((0, 1), (1, 2), (2, 0)):
        di, dj = dists[i], dists[j]
        if di * dj < 0.0:
            t = di / (di - dj)
            pi, pj = corners[i], corners[j]
            pts.append(_project(
                (pi[0] + t * (pj[0] - pi[0]),
                 pi[1] + t * (pj[1] - pi[1]),
                 pi[2] + t * (pj[2] - pi[2])), origin, basis))
    # on-plane corners participate only when they are genuine segment endpoints
    for k in range(3):
        if not on[k]:
            continue
        o1, o2 = (k + 1) % 3, (k + 2) % 3
        d1, d2 = dists[o1], dists[o2]
        if (d1 > onplane_eps and d2 < -onplane_eps) or (d1 < -onplane_eps and d2 > onplane_eps):
            pts.append(_project(corners[k], origin, basis))
        elif on[o1] and not on[o2]:
            pts.append(_project(corners[k], origin, basis))
        elif on[o2] and not on[o1]:
            pts.append(_project(corners[k], origin, basis))
    if len(pts) >= 2:
        return (pts[0], pts[1])
    return None


# --------------------------------------------------------------------------
# chaining
# --------------------------------------------------------------------------

def _chain_loops(raw_segments: Sequence[Tuple[Tuple[float, float],
                                              Tuple[float, float]]],
                 dedup_eps: float) -> List[List[Tuple[float, float]]]:
    """Chain 2D segments into closed loops via an adjacency graph.

    Coincident points are deduped by grid quantization (epsilon dedup_eps);
    each deduped point keeps the first-seen representative.  Segments are
    deduped too.  Loops are walked deterministically (sorted adjacency lists,
    first unused edge) and closed loops are returned with the first point NOT
    repeated at the end; open chains (non-closed slices) are discarded.
    """
    scale = 1.0 / dedup_eps
    reps: Dict[Tuple[int, int], Tuple[float, float]] = {}

    def canon(p: Sequence[float]) -> Tuple[int, int]:
        key = (round(p[0] * scale), round(p[1] * scale))
        if key not in reps:
            reps[key] = (float(p[0]), float(p[1]))
        return key

    segs = set()
    for a, b in raw_segments:
        ka, kb = canon(a), canon(b)
        if ka == kb:
            continue  # zero-length segment after dedup
        segs.add((ka, kb) if ka < kb else (kb, ka))
    adj: Dict[Tuple[int, int], List[Tuple[int, int]]] = defaultdict(list)
    for ka, kb in segs:
        adj[ka].append(kb)
        adj[kb].append(ka)
    for key in adj:
        adj[key].sort()

    def _edge(a, b):
        return (a, b) if a < b else (b, a)

    used = set()
    loops = []
    for start in sorted(adj):
        for nxt in adj[start]:
            e = _edge(start, nxt)
            if e in used:
                continue
            used.add(e)
            loop = [reps[start]]
            cur, prev = nxt, start
            while cur != start:
                loop.append(reps[cur])
                cands = [v for v in adj[cur]
                         if v != prev and _edge(cur, v) not in used]
                if not cands:
                    loop = None  # open chain -> discard
                    break
                nxt2 = cands[0]
                used.add(_edge(cur, nxt2))
                if nxt2 == start:
                    break  # loop closed; first point is not repeated
                prev, cur = cur, nxt2
            if loop is not None and len(loop) >= 3:
                loops.append(loop)
    return loops


# --------------------------------------------------------------------------
# loop polishing: simplification, holes, orientation
# --------------------------------------------------------------------------

def _point_on_segment(a: Sequence[float], b: Sequence[float],
                      c: Sequence[float], eps: float) -> bool:
    """True if c lies on the closed segment ab (within eps)."""
    abx = b[0] - a[0]
    aby = b[1] - a[1]
    ab2 = abx * abx + aby * aby
    acx = c[0] - a[0]
    acy = c[1] - a[1]
    if ab2 <= eps * eps:
        return math.hypot(acx, acy) <= eps
    t = (acx * abx + acy * aby) / ab2
    if t < 0.0 or t > 1.0:
        return False
    dist = abs(acx * aby - acy * abx) / math.sqrt(ab2)
    return dist <= eps


def _simplify_loop(pts: Sequence[Sequence[float]],
                   eps: float) -> List[Tuple[float, float]]:
    """Drop points that are collinear with their two neighbours (e.g. the
    extra midpoint Fusion's 2-triangles-per-face triangulation creates on
    every edge of a cube slice).  Iterates until stable; deterministic."""
    pts = [tuple(float(v) for v in p) for p in pts]
    changed = True
    while changed:
        changed = False
        n = len(pts)
        if n < 4:
            break
        out = []
        for i in range(n):
            b = pts[i]
            c = pts[(i + 1) % n]
            a = out[-1] if out else pts[-1]
            if _point_on_segment(a, c, b, eps):
                changed = True
            else:
                out.append(b)
        pts = out
    return pts


def _signed_area(pts: Sequence[Sequence[float]]) -> float:
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return 0.5 * s


def _point_in_polygon(pt: Sequence[float],
                      poly: Sequence[Sequence[float]]) -> bool:
    """Even-odd ray-casting test; True if pt is inside the (closed) polygon."""
    x, y = pt[0], pt[1]
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xint:
                inside = not inside
    return inside


def _containment_depths(loops: Sequence[Sequence[Sequence[float]]]) -> List[int]:
    """Number of other loops strictly containing each loop (any-vertex test)."""
    depths = []
    for i in range(len(loops)):
        d = 0
        for j in range(len(loops)):
            if i != j and any(_point_in_polygon(pt, loops[j]) for pt in loops[i]):
                d += 1
        depths.append(d)
    return depths


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def slice_mesh_at(nodes: Sequence, indices: Sequence,
                  height_or_plane: Union[float, Dict]) -> Dict:
    """Slice a triangle mesh with an axis-aligned plane.

    Returns {"loops": [{"pts": [[x, y], ...], "is_hole": bool}, ...],
             "plane": {"axis", "height_cm", "origin", "basis"}}.  Never
    raises on empty/degenerate input; raises ValueError only for structurally
    invalid input (flat lengths not divisible by 3, out-of-range triangle
    indices, unknown plane spec).  Deterministic: identical input yields
    identical output.
    """
    node_list = _as_triples(nodes)
    tri_list = [(int(a), int(b), int(c)) for a, b, c in _as_triples(indices)]
    plane = _resolve_plane(height_or_plane)
    if not node_list or not tri_list:
        return {"loops": [], "plane": _public_plane(plane)}
    for (i0, i1, i2) in tri_list:
        for ix in (i0, i1, i2):
            if not 0 <= ix < len(node_list):
                raise ValueError(
                    f"triangle index {ix} out of range for {len(node_list)} nodes")
    dedup_eps, onplane_eps = _epsilons(node_list)
    raw_segments = []
    for (i0, i1, i2) in tri_list:
        seg = _triangle_plane_segment(
            node_list[i0], node_list[i1], node_list[i2],
            plane["_normal"], plane["origin"], plane["basis"], onplane_eps)
        if seg is not None:
            raw_segments.append(seg)
    chains = _chain_loops(raw_segments, dedup_eps)
    polished = []
    for pts in chains:
        pts = _simplify_loop(pts, dedup_eps)
        if len(pts) >= 3:
            polished.append([_round2(p) for p in pts])
    depths = _containment_depths(polished)
    loops = []
    for pts, depth in zip(polished, depths):
        area = _signed_area(pts)
        is_hole = depth % 2 == 1
        # force orientation: outer = CCW (positive), hole = CW (negative)
        if (is_hole and area > 0.0) or (not is_hole and area < 0.0):
            pts.reverse()
        loops.append({"pts": pts, "is_hole": is_hole})
    loops.sort(key=lambda lp: (lp["is_hole"],
                               min(p[0] for p in lp["pts"]),
                               min(p[1] for p in lp["pts"])))
    return {"loops": loops, "plane": _public_plane(plane)}


def scale_slice(result: Dict, factor: float) -> Dict:
    """Deep-copy `result` scaling loop points and the plane by `factor`.

    Used by the MCP tool to convert cm-based slices to mm / in.  `is_hole`
    flags and the plane basis (unit vectors) are left untouched.
    """
    out = copy.deepcopy(result)
    f = float(factor)
    for loop in out.get("loops", []):
        loop["pts"] = [[round(x * f, 6), round(y * f, 6)] for x, y in loop["pts"]]
    plane = out.get("plane")
    if plane:
        if plane.get("origin") is not None:
            plane["origin"] = _round3([c * f for c in plane["origin"]])
        if plane.get("height_cm") is not None:
            plane["height_cm"] = round(plane["height_cm"] * f, 6)
    return out
