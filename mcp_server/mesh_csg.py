"""Pure-Python CSG-tree builder for the Fusion 360 MCP server
(mesh-to-parametric plan, Todo 6).

`build_csg_tree(mesh_data, strategy, params)` turns a triangle mesh into a CSG
tree that is consumable DIRECTLY by
``mcp_server.scad_translator.translate_to_fusion_commands``:

  * ``prismatic`` -- slice the mesh at N interior heights along an axis
    (reusing ``mcp_server.mesh_slicer.slice_mesh_at``), verify the
    cross-section is constant (loop-shape similarity), and emit ONE
    ``linear_extrude`` whose ``polygon`` child carries the first slice's loop
    pts (holes -> ``paths``), wrapped in a ``translate`` when the mesh is
    offset along the axis.
  * ``csg_decompose`` -- planar region growing over the welded topology, then
    box fitting (3 orthogonal plane-pair extents) and a cylinder fit for
    leftover clusters; emits a ``union`` tree with per-primitive
    ``translate`` + ``rotate``.

``compute_revolved_profile(mesh_data, params)`` produces the HALF cross-section
profile (the x>=0 half of the mesh's intersection with the Z-containing plane)
for the ``revolved`` strategy -- it is NOT a CSG tree; it feeds the
``revolve_cross_section`` Fusion handler.

Design constraints (same as mesh_analysis / mesh_slicer):
  * stdlib ONLY (math, collections) - no numpy (Fusion's embedded Python does
    not guarantee it).
  * fully DETERMINISTIC - no ``random`` module; region seeds, adjacency lists
    and candidate scans all follow input order.
  * robust - empty / degenerate input returns an empty tree or raises a
    specific error subclass, never crashes.

Input conventions: ``mesh_data`` is the ``extract_mesh_data`` payload shape
``{"nodes": [...], "indices": [...]}`` (6dp-rounded vertices, cm).  ``nodes``
may be flat [x,y,z,...] or a list of triples; ``indices`` flat or triples.
All internal math is in cm; the MCP tool scales trees via ``scale_tree`` and
profiles via ``_cm_to_unit_factor`` before passing them to the handlers.
"""

from __future__ import annotations

import copy
import math
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from mcp_server.mesh_slicer import slice_mesh_at
except ImportError:
    from mesh_slicer import slice_mesh_at

Float3 = Tuple[float, float, float]

#: strategies whose CSG emission build_csg_tree implements.
_CSG_STRATEGIES = frozenset({"prismatic", "csg_decompose"})

_COS_ORTHO = 0.05          # |dot| below this => directions are orthogonal
_COS_PARALLEL = math.cos(math.radians(10.0))   # region growing / box faces
_COS_SIDE = math.cos(math.radians(85.0))       # cylinder side normals (5 deg)
_CYL_COVERAGE_MIN = 0.5    # min azimuthal bucket coverage for a cylinder fit
_CYL_RADIUS_CV_MAX = 0.25  # max relative std of side-face radius
_DEFAULT_SIMILARITY_TOL = 0.05  # max normalized loop distance for prismatic


class UnsupportedStrategyError(ValueError):
    """Raised when ``build_csg_tree`` gets a strategy it cannot emit as CSG
    (unknown strategies; ``organic`` arrives with T7; ``revolved`` uses
    ``compute_revolved_profile`` instead)."""


class NotPrismaticError(ValueError):
    """Raised when the ``prismatic`` strategy finds a varying cross-section
    (the mesh is not an extrusion of a constant profile)."""


# --------------------------------------------------------------------------
# input normalization helpers (mirror mesh_analysis / mesh_slicer)
# --------------------------------------------------------------------------

def _as_triples(data: Sequence) -> List[Tuple[float, ...]]:
    """Return `data` as a list of (a, b, c) triples (flat or nested)."""
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


def _norm(a: Sequence[float]) -> float:
    return math.sqrt(_dot(a, a))


def _normalize(a: Sequence[float]) -> Optional[Float3]:
    n = _norm(a)
    if n <= 1e-12:
        return None
    return (a[0] / n, a[1] / n, a[2] / n)


def _round2(p: Sequence[float]) -> List[float]:
    return [round(float(p[0]), 6), round(float(p[1]), 6)]


def _round3(p: Sequence[float]) -> List[float]:
    return [round(float(v), 6) for v in p]


def _mesh_extent(node_list: Sequence[Float3]) -> float:
    if not node_list:
        return 0.0
    return max(max(p[k] for p in node_list) - min(p[k] for p in node_list)
               for k in range(3))


def _weld_vertices(node_list: Sequence[Float3],
                   tri_list: Sequence[Tuple[int, int, int]],
                   eps: float) -> Tuple[List[Float3], List[Tuple[int, int, int]]]:
    """Merge geometrically-identical vertices and remap triangle indices."""
    if eps <= 0:
        return list(node_list), list(tri_list)
    scale = 1.0 / eps
    remap: Dict[Tuple[int, int, int], int] = {}
    welded: List[Float3] = []
    out: List[Tuple[int, int, int]] = []
    for (i0, i1, i2) in tri_list:
        t = []
        for ix in (i0, i1, i2):
            p = node_list[ix]
            key = (round(p[0] * scale), round(p[1] * scale), round(p[2] * scale))
            if key not in remap:
                remap[key] = len(welded)
                welded.append(p)
            t.append(remap[key])
        out.append(tuple(t))
    return welded, out


def _face_normals(node_list: Sequence[Float3],
                  tri_list: Sequence[Tuple[int, int, int]]) -> List[Float3]:
    """Geometric per-face unit normals (outward winding required)."""
    out = []
    for (i0, i1, i2) in tri_list:
        a, b, c = node_list[i0], node_list[i1], node_list[i2]
        u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        n = _normalize(_cross(u, v))
        out.append(n if n is not None else (0.0, 0.0, 1.0))
    return out


def _face_centroids(node_list: Sequence[Float3],
                    tri_list: Sequence[Tuple[int, int, int]]) -> List[Float3]:
    return [tuple((node_list[i0][k] + node_list[i1][k] + node_list[i2][k]) / 3.0
                  for k in range(3))
            for (i0, i1, i2) in tri_list]


def _face_areas(node_list: Sequence[Float3],
                tri_list: Sequence[Tuple[int, int, int]]) -> List[float]:
    out = []
    for (i0, i1, i2) in tri_list:
        a, b, c = node_list[i0], node_list[i1], node_list[i2]
        u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        out.append(0.5 * _norm(_cross(u, v)))
    return out


# --------------------------------------------------------------------------
# prismatic
# --------------------------------------------------------------------------

def _loop_distance(a: Sequence[Sequence[float]],
                   b: Sequence[Sequence[float]]) -> float:
    """Normalized cyclic-min RMS distance between two loops of equal length.

    Both loops are centered; the distance is normalized by the LARGER extent
    (so two concentric squares of different size do NOT look similar).
    """
    n = len(a)
    if n == 0 or n != len(b):
        return float("inf")
    ca = (sum(p[0] for p in a) / n, sum(p[1] for p in a) / n)
    cb = (sum(p[0] for p in b) / n, sum(p[1] for p in b) / n)
    A = [(p[0] - ca[0], p[1] - ca[1]) for p in a]
    B = [(p[0] - cb[0], p[1] - cb[1]) for p in b]
    scale = max(max(abs(p[0]), abs(p[1])) for p in A + B) or 1.0
    best = float("inf")
    for shift in range(n):
        s = 0.0
        for i in range(n):
            p, q = A[i], B[(i + shift) % n]
            s += (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2
        best = min(best, math.sqrt(s / n) / scale)
    return best


def _prismatic_tree(slices: List[Dict], axis: str, lo: float, hi: float,
                    tol: float) -> List[Dict]:
    """Verify constant cross-section across slices and emit the extrude tree.

    The slice loop points are ABSOLUTE in the two basis coordinates (for axis
    Z they are the mesh's own x,y -- mesh_slicer projects relative to the
    plane origin (0,0,h) along basis (x,y)), so only the coordinate along the
    slice axis needs a ``translate`` to preserve position fidelity.
    """
    if not slices or not slices[0]["loops"]:
        raise NotPrismaticError(
            "mesh has no cross-section loops; cannot build a prismatic tree")
    first_loops = slices[0]["loops"]
    # pairwise loop-shape similarity
    for sl in slices[1:]:
        loops = sl["loops"]
        if len(loops) != len(first_loops):
            raise NotPrismaticError(
                "cross-section loop count changes between slices")
        for l1, l2 in zip(first_loops, loops):
            if l1["is_hole"] != l2["is_hole"]:
                raise NotPrismaticError(
                    "cross-section hole structure changes between slices")
            d = _loop_distance(l1["pts"], l2["pts"])
            if d > tol:
                raise NotPrismaticError(
                    f"cross-section varies between slices (loop distance "
                    f"{d:.4f} > {tol}); the mesh is not an extrusion")
    outer = [lp for lp in first_loops if not lp["is_hole"]]
    holes = [lp for lp in first_loops if lp["is_hole"]]
    pts = [p for lp in (outer + holes) for p in lp["pts"]]
    params: Dict = {"pts": pts}
    if holes:
        offset = 0
        paths = []
        for lp in (outer + holes):
            paths.append(list(range(offset, offset + len(lp["pts"]))))
            offset += len(lp["pts"])
        params["paths"] = paths
    span = hi - lo
    if span <= 1e-9:
        raise NotPrismaticError("prismatic extent is zero along the slice axis")
    extrude = {
        "kind": "linear_extrude",
        "params": {"height": round(span, 6)},
        "children": [{"kind": "polygon", "params": params, "children": []}],
    }
    # position fidelity: the polygon carries the two in-plane offsets (its pts
    # are absolute); translate only the coordinate along the slice axis when
    # the mesh base is offset from the origin.
    axis_idx = "XYZ".index(axis)
    if abs(lo) > 1e-9:
        t = [0.0, 0.0, 0.0]
        t[axis_idx] = round(lo, 6)
        return [{
            "kind": "translate",
            "params": {"0": t},
            "children": [extrude],
        }]
    return [extrude]


def _prismatic(mesh_data: Dict, params: Dict) -> List[Dict]:
    nodes = _as_triples(mesh_data.get("nodes", []))
    indices = _as_triples(mesh_data.get("indices", []))
    axis = str(params.get("axis", "Z") or "Z").strip().upper()
    if axis not in ("X", "Y", "Z"):
        raise ValueError("prismatic axis must be X, Y, or Z")
    n_slices = max(2, int(params.get("num_slices", 3) or 3))
    tol = float(params.get("similarity_tol", _DEFAULT_SIMILARITY_TOL) or
                _DEFAULT_SIMILARITY_TOL)
    if not nodes or not indices:
        raise NotPrismaticError("empty mesh data")
    axis_idx = "XYZ".index(axis)
    lo = min(p[axis_idx] for p in nodes)
    hi = max(p[axis_idx] for p in nodes)
    span = hi - lo
    if span <= 1e-9:
        raise NotPrismaticError("mesh has zero extent along the slice axis")
    # interior heights only: slicing exactly at the caps finds no loops (the
    # slicer skips coplanar triangles).  A height coinciding with an internal
    # face (e.g. the cap of a hole) degenerates its slice (loop points
    # collapse), so a half-shifted set of heights is retried before giving up.
    heights_sets = (
        [lo + span * (i + 1) / (n_slices + 1) for i in range(n_slices)],
        [lo + span * (i + 1.5) / (n_slices + 1) for i in range(n_slices)],
    )
    last_err: Optional[BaseException] = None
    for heights in heights_sets:
        slices = [
            slice_mesh_at(nodes, indices, {"axis": axis, "height_cm": h})
            for h in heights
        ]
        try:
            return _prismatic_tree(slices, axis, lo, hi, tol)
        except NotPrismaticError as err:
            last_err = err
    assert last_err is not None
    raise last_err


# --------------------------------------------------------------------------
# csg_decompose: planar region growing + box/cylinder fitting
# --------------------------------------------------------------------------

def _vertex_face_map(tri_list: Sequence[Tuple[int, int, int]]) -> Dict[int, List[int]]:
    vf: Dict[int, List[int]] = defaultdict(list)
    for t, (i0, i1, i2) in enumerate(tri_list):
        vf[i0].append(t)
        vf[i1].append(t)
        vf[i2].append(t)
    return vf


def _grow_regions(tri_list, normals, centroids, areas):
    """Deterministic planar region growing over welded-edge adjacency.

    A region starts at the lowest-index unvisited triangle and BFS-merges
    edge-connected triangles whose normal stays within 10 deg of the region's
    representative (the start triangle's normal).  For a box this keeps each
    planar face one region; a curved surface grows into a patch (leftover
    regions are handed to the cylinder fit).
    """
    vf = _vertex_face_map(tri_list)
    cos_thr = _COS_PARALLEL
    visited = [False] * len(tri_list)
    regions: List[Dict] = []
    for start in range(len(tri_list)):
        if visited[start]:
            continue
        stack = [start]
        visited[start] = True
        faces = []
        rep = normals[start]
        while stack:
            f = stack.pop()
            faces.append(f)
            for v in tri_list[f]:
                for g in vf[v]:
                    if not visited[g] and _dot(normals[g], rep) >= cos_thr:
                        visited[g] = True
                        stack.append(g)
        s = [0.0, 0.0, 0.0]
        for f in faces:
            n = normals[f]
            for k in range(3):
                s[k] += n[k]
        n_rep = _normalize(tuple(s))
        regions.append({
            "faces": faces,
            "normal": n_rep if n_rep is not None else (0.0, 0.0, 1.0),
            "area": sum(areas[f] for f in faces),
            "min_face": min(faces),
            "centroid": tuple(sum(centroids[f][k] for f in faces) / len(faces)
                              for k in range(3)),
        })
    return regions


def _build_adjacency(regions: List[Dict],
                     tri_list: Sequence[Tuple[int, int, int]]) -> List[List[int]]:
    """Region adjacency: two regions are neighbors when any of their triangles
    share a welded vertex."""
    vert_regions: Dict[int, set] = defaultdict(set)
    for r, reg in enumerate(regions):
        for f in reg["faces"]:
            for v in tri_list[f]:
                vert_regions[v].add(r)
    adj: List[set] = [set() for _ in regions]
    for v, rs in vert_regions.items():
        rs = list(rs)
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                adj[rs[i]].add(rs[j])
                adj[rs[j]].add(rs[i])
    return [sorted(s) for s in adj]


def _pos(reg: Dict, d: Sequence[float]) -> float:
    """Plane position of a region along direction `d`: mean centroid dot d."""
    return _dot(reg["centroid"], d)


def _euler_angles_from_rotation(r: Sequence[Sequence[float]]) -> List[float]:
    """OpenSCAD-order (Rz.Ry.Rx) Euler angles reproducing rotation `r`.

    A deterministic 90-deg-step search covers the signed-permutation matrices
    that box/cylinder fitting produces (handling the gimbal-lock ambiguity
    exactly); general rotations fall back to the standard atan2 extraction.
    """
    target = [[float(v) for v in row] for row in r]

    def compose(ax, ay, az):
        rx = [[1, 0, 0], [0, math.cos(ax), -math.sin(ax)],
              [0, math.sin(ax), math.cos(ax)]]
        ry = [[math.cos(ay), 0, math.sin(ay)], [0, 1, 0],
              [-math.sin(ay), 0, math.cos(ay)]]
        rz = [[math.cos(az), -math.sin(az), 0],
              [math.sin(az), math.cos(az), 0], [0, 0, 1]]
        m = [[0.0] * 3 for _ in range(3)]
        t = [[0.0] * 3 for _ in range(3)]
        for i in range(3):
            for j in range(3):
                for k in range(3):
                    t[i][j] += ry[i][k] * rx[k][j]
        for i in range(3):
            for j in range(3):
                for k in range(3):
                    m[i][j] += rz[i][k] * t[k][j]
        return m

    def close(a, b):
        return all(abs(a[i][j] - b[i][j]) <= 1e-6 for i in range(3)
                   for j in range(3))

    # 90-deg-step search: exact for signed-permutation rotations.
    for az in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2):
        for ay in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2):
            for ax in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2):
                if close(compose(ax, ay, az), target):
                    return [math.degrees(ax), math.degrees(ay),
                            math.degrees(az)]
    # general fallback: R = Rz.Ry.Rx
    ay = math.asin(max(-1.0, min(1.0, -target[2][0])))
    if abs(abs(target[2][0]) - 1.0) < 1e-9:
        ax = math.atan2(-target[0][1], target[1][1])
        az = 0.0
    else:
        ax = math.atan2(target[2][1], target[2][2])
        az = math.atan2(target[1][0], target[0][0])
    return [math.degrees(ax), math.degrees(ay), math.degrees(az)]


def _primitive_node(kind: str, dims, frame, position) -> Dict:
    """A translate -> rotate -> primitive node pair for a fitted primitive.

    `dims` is (dx, dy, dz) for a cube and (r, h) for a cylinder; `frame` is
    the orthonormal (n1, n2, n3) local axes (columns of the rotation R);
    `position` is the world center.  Angles from the 90-deg search are
    identity for axis-aligned primitives.
    """
    rot = [[frame[0][0], frame[1][0], frame[2][0]],
           [frame[0][1], frame[1][1], frame[2][1]],
           [frame[0][2], frame[1][2], frame[2][2]]]
    angles = [round(a, 6) for a in _euler_angles_from_rotation(rot)]
    if kind == "cube":
        prim = {"kind": "cube",
                "params": {"size": [round(v, 6) for v in dims],
                           "center": True}}
    else:
        prim = {"kind": "cylinder",
                "params": {"r": round(float(dims[0]), 6),
                           "h": round(float(dims[1]), 6),
                           "center": True}}
    return {
        "kind": "translate",
        "params": {"0": [round(float(c), 6) for c in position]},
        "children": [{
            "kind": "rotate",
            "params": {"0": angles},
            "children": [prim],
        }],
    }


def _assemble_boxes(regions, adj) -> List[Dict]:
    """Greedy box fitting: seed region + 4 perpendicular edge-neighbors +
    a matching opposite face (shared-neighbor count) define one box."""
    n_regions = len(regions)
    order = sorted(range(n_regions),
                   key=lambda r: (-regions[r]["area"], regions[r]["min_face"]))
    used = [False] * n_regions
    boxes: List[Dict] = []

    def first_unused():
        for r in order:
            if not used[r]:
                return r
        return None

    while True:
        seed = first_unused()
        if seed is None:
            break
        n1 = regions[seed]["normal"]
        n2 = None
        for r in order:
            if used[r] or r == seed:
                continue
            if abs(_dot(regions[r]["normal"], n1)) < _COS_ORTHO:
                n2 = regions[r]["normal"]
                break
        if n2 is None:
            break
        n2 = _normalize(n2)
        n3 = _normalize(_cross(n1, n2))
        if n3 is None:
            break
        axes = [n1, n2, n3]
        seed_nbrs = set(adj[seed])
        extents: List[float] = [0.0, 0.0, 0.0]
        centers: List[float] = [0.0, 0.0, 0.0]
        ok = True
        used_regions = {seed}
        for i in (1, 2):
            a = axes[i]
            plus = [r for r in seed_nbrs if _dot(regions[r]["normal"], a) >= _COS_PARALLEL]
            minus = [r for r in seed_nbrs if _dot(regions[r]["normal"], a) <= -_COS_PARALLEL]
            if not plus or not minus:
                ok = False
                break
            o_plus = max(_pos(regions[r], a) for r in plus)
            o_minus = min(_pos(regions[r], a) for r in minus)
            extents[i] = o_plus - o_minus
            centers[i] = (o_plus + o_minus) / 2.0
            used_regions.update(plus)
            used_regions.update(minus)
        if not ok:
            break
        a0 = axes[0]
        best = None
        for r in order:
            if used[r] or r == seed:
                continue
            if _dot(regions[r]["normal"], a0) <= -_COS_PARALLEL:
                shared = len(seed_nbrs & set(adj[r]))
                if best is None or shared > best[0]:
                    best = (shared, r)
        if best is None or best[0] < 2:
            break
        opp = best[1]
        o_seed = _pos(regions[seed], a0)
        o_opp = _pos(regions[opp], a0)
        extents[0] = abs(o_seed - o_opp)
        centers[0] = (o_seed + o_opp) / 2.0
        used_regions.add(opp)
        for r in used_regions:
            used[r] = True
        center = tuple(sum(centers[i] * axes[i][k] for i in range(3))
                       for k in range(3))
        boxes.append({
            "center": center,
            "size": tuple(extents),
            "axes": tuple(axes),
        })
    return boxes


def _cylinder_fit_leftovers(regions, used, order) -> Optional[Dict]:
    """Fit a Z-friendly cylinder from leftover regions (port of the
    mesh_analysis normal-line sampling, leaner).  Returns a box-like dict with
    'center' / 'size'(radius,height) / 'axes'."""
    leftover = [r for r in order if not used[r]]
    if len(leftover) < 3:
        return None
    reps = [regions[r]["normal"] for r in leftover]
    candidates = []
    for i in range(len(reps)):
        for j in range(i + 1, len(reps)):
            u = _normalize(_cross(reps[i], reps[j]))
            if u is not None:
                candidates.append(u)
    if not candidates:
        return None
    side_cos = _COS_SIDE
    best = None
    for v in candidates:
        side = [r for r in leftover if abs(_dot(regions[r]["normal"], v)) < side_cos]
        caps = [r for r in leftover
                if abs(_dot(regions[r]["normal"], v)) >= _COS_PARALLEL]
        if not side or len(caps) < 1:
            continue
        # azimuthal coverage over 16 buckets
        if abs(v[0]) < 0.9:
            u0 = _normalize(_cross(v, (1.0, 0.0, 0.0)))
        else:
            u0 = _normalize(_cross(v, (0.0, 1.0, 0.0)))
        if u0 is None:
            continue
        w = _cross(v, u0)
        buckets = set()
        radii = []
        for r in side:
            c = regions[r]["centroid"]
            ang = math.atan2(_dot(c, w), _dot(c, u0))
            buckets.add(int(math.floor(ang / (2.0 * math.pi) * 16.0)) % 16)
            radii.append(_norm(_cross(c, v)))
        coverage = len(buckets) / 16.0
        if coverage < _CYL_COVERAGE_MIN or not radii:
            continue
        mean_r = sum(radii) / len(radii)
        if mean_r <= 1e-9:
            continue
        var = sum((rad - mean_r) ** 2 for rad in radii) / len(radii)
        cv = math.sqrt(var) / mean_r
        if cv > _CYL_RADIUS_CV_MAX:
            continue
        score = coverage * (len(side) / len(leftover))
        if best is None or score > best[0]:
            best = (score, v, mean_r)
    if best is None:
        return None
    _, axis, radius = best
    lo = min(_pos(regions[r], axis) for r in leftover)
    hi = max(_pos(regions[r], axis) for r in leftover)
    h = hi - lo
    center = tuple((lo + (hi - lo) / 2.0) * axis[k] for k in range(3))
    u0 = _normalize(_cross(axis, (0.0, 0.0, 1.0)))
    if u0 is None:
        u0 = _normalize(_cross(axis, (1.0, 0.0, 0.0)))
    if u0 is None:
        return None
    v0 = _cross(axis, u0)
    return {
        "center": center,
        "size": (radius, h),
        "axes": (u0, v0, axis),
        "is_cylinder": True,
    }


def _csg_decompose(mesh_data: Dict, params: Dict) -> List[Dict]:
    nodes = _as_triples(mesh_data.get("nodes", []))
    indices = _as_triples(mesh_data.get("indices", []))
    if not nodes or not indices:
        return []
    tri_list = [(int(a), int(b), int(c)) for a, b, c in indices]
    for (i0, i1, i2) in tri_list:
        for ix in (i0, i1, i2):
            if not 0 <= ix < len(nodes):
                raise ValueError(
                    f"triangle index {ix} out of range for {len(nodes)} nodes")
    extent = _mesh_extent(nodes)
    eps = max(1e-9, 1e-7 * extent) if extent > 0 else 1e-9
    welded, tris = _weld_vertices(nodes, tri_list, eps)
    normals = _face_normals(welded, tris)
    centroids = _face_centroids(welded, tris)
    areas = _face_areas(welded, tris)
    regions = _grow_regions(tris, normals, centroids, areas)
    adj = _build_adjacency(regions, tris)
    boxes = _assemble_boxes(regions, adj)
    order = sorted(range(len(regions)),
                   key=lambda r: (-regions[r]["area"], regions[r]["min_face"]))
    used_flags = _mark_used(regions, boxes, adj, order)
    cyl = None
    if not all(used_flags):
        cyl = _cylinder_fit_leftovers(regions, used_flags, order)

    children = []
    for box in boxes:
        frame = box["axes"]
        node = _primitive_node("cube", box["size"], frame, box["center"])
        children.append(node)
    if cyl is not None:
        node = _primitive_node("cylinder", cyl["size"], cyl["axes"],
                               cyl["center"])
        children.append(node)
    if not children:
        return []
    return [{"kind": "union", "params": {}, "children": children}]


def _mark_used(regions, boxes, adj, order):
    """Which regions did the box assembly consume (seed + 4 nbrs + opposite
    for each box)?  Re-derives from the boxes' centers/dims for the leftover
    pass."""
    used = [False] * len(regions)
    for box in boxes:
        for r in order:
            c = regions[r]["centroid"]
            proj = tuple(_dot(c, ax) for ax in box["axes"])
            half = tuple(e / 2.0 for e in box["size"])
            center_proj = tuple(_dot(box["center"], ax) for ax in box["axes"])
            if all(abs(proj[i] - center_proj[i]) <= half[i] + 1e-4
                   for i in range(3)):
                used[r] = True
    return used


# --------------------------------------------------------------------------
# revolved half-profile
# --------------------------------------------------------------------------

def compute_revolved_profile(mesh_data: Dict,
                             params: Optional[Dict] = None) -> List[List[float]]:
    """Half cross-section profile (x >= 0 half) for the revolved strategy.

    The mesh is sliced with the Z-containing plane (the Y=0 plane, whose 2D
    basis is (x, z) -- see mesh_slicer._BASIS), the loop points are filtered to
    the x >= 0 half, and the open chain is closed along the Z axis into a
    closed half-profile polygon [[x, z], ...] in cm.
    """
    nodes = _as_triples(mesh_data.get("nodes", []))
    indices = _as_triples(mesh_data.get("indices", []))
    if not nodes or not indices:
        raise ValueError("empty mesh data; cannot compute a revolve profile")
    result = slice_mesh_at(nodes, indices, {"axis": "Y", "height_cm": 0.0})
    loops = result.get("loops", [])
    if not loops:
        raise ValueError("mesh does not intersect the Z-containing plane; "
                         "cannot compute a revolve profile")
    extent = _mesh_extent(nodes)
    eps = max(1e-9, 1e-6 * extent) if extent > 0 else 1e-9
    # take the FIRST (outermost) loop; its (x, z) pts in the Y-plane basis
    pts = loops[0]["pts"]
    right = sorted({(round(x, 6), round(z, 6))
                    for x, z in pts if x >= -eps}, key=lambda p: (p[1], p[0]))
    if not right:
        raise ValueError("no profile points on the x >= 0 half")
    zmin = right[0][1]
    zmax = right[-1][1]
    profile = [(0.0, zmin)]
    for x, z in right:
        profile.append((x, z))
    profile.append((0.0, zmax))
    profile = _simplify_profile(profile, eps)
    if len(profile) < 3:
        raise ValueError("revolve profile collapsed to fewer than 3 points")
    return [[round(float(x), 6), round(float(z), 6)] for x, z in profile]


def _simplify_profile(pts, eps):
    """Drop duplicate and collinear points; keep closure order."""
    out = []
    for p in pts:
        if not out or abs(p[0] - out[-1][0]) > eps or abs(p[1] - out[-1][1]) > eps:
            out.append(p)
    changed = True
    while changed:
        changed = False
        n = len(out)
        if n < 4:
            break
        simplified = []
        for i in range(n):
            a = simplified[-1] if simplified else out[-1]
            b = out[i]
            c = out[(i + 1) % n]
            if _point_on_segment2d(a, c, b, eps):
                changed = True
            else:
                simplified.append(b)
        out = simplified
    return out


def _point_on_segment2d(a, b, c, eps):
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


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def build_csg_tree(mesh_data: Dict, strategy: str,
                   params: Optional[Dict] = None) -> List[Dict]:
    """Build a CSG tree for `mesh_data` under `strategy`.

    ``prismatic`` and ``csg_decompose`` emit CSG trees consumable directly by
    ``scad_translator.translate_to_fusion_commands``.  ``revolved`` is NOT a
    CSG strategy -- it uses ``compute_revolved_profile`` for the revolve
    handler (and raises here).  Unknown strategies raise
    ``UnsupportedStrategyError``.  Never crashes on empty input: an empty tree
    (or a specific error subclass) is returned.
    """
    params = dict(params or {})
    strategy = str(strategy or "").strip().lower()
    if strategy not in _CSG_STRATEGIES:
        if strategy == "revolved":
            raise UnsupportedStrategyError(
                "strategy 'revolved' produces a revolve profile, not a CSG "
                "tree; use compute_revolved_profile()")
        raise UnsupportedStrategyError(f"Unsupported strategy '{strategy}'")
    if strategy == "prismatic":
        return _prismatic(mesh_data, params)
    return _csg_decompose(mesh_data, params)


def scale_tree(tree: Sequence[Dict], factor: float) -> List[Dict]:
    """Deep-copy `tree` scaling dimensional params by `factor` (cm -> units).

    The translator applies its own unit scale on top, so the server tool
    scales a cm-built tree to the requested units before the handler call.
    Angles (rotate) and booleans (center) are untouched.
    """
    out = copy.deepcopy(tree)
    f = float(factor)
    for node in out:
        _scale_node(node, f)
    return out


def _scale_node(node: Dict, f: float) -> None:
    kind = node["kind"]
    params = node.get("params", {})
    if kind == "polygon":
        if isinstance(params.get("pts"), list):
            params["pts"] = [[round(x * f, 6), round(y * f, 6)]
                             for x, y in params["pts"]]
    elif kind == "linear_extrude":
        for key in ("height", "h"):
            if key in params:
                params[key] = round(float(params[key]) * f, 6)
    elif kind == "cube":
        if isinstance(params.get("size"), list):
            params["size"] = [round(float(v) * f, 6) for v in params["size"]]
    elif kind == "cylinder":
        for key in ("r", "r1", "r2", "h"):
            if key in params:
                params[key] = round(float(params[key]) * f, 6)
    elif kind == "translate":
        vec = _translate_vec(params)
        if vec is not None:
            scaled = [round(float(v) * f, 6) for v in vec]
            if "args" in params:
                params["args"]["0"] = scaled
            else:
                params["0"] = scaled
    for child in node.get("children", []):
        _scale_node(child, f)


def _translate_vec(params: Dict) -> Optional[List[float]]:
    args = params.get("args")
    if isinstance(args, dict):
        if "0" in args:
            return list(args["0"])
        if 0 in args:
            return list(args[0])
    if "0" in params:
        return list(params["0"])
    if "v" in params:
        return list(params["v"])
    return None
