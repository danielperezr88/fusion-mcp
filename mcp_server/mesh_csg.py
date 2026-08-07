"""Geometry primitives salvaged from the mesh-reconstruction pipeline.
compute_revolved_profile (half-profile extraction), scale_tree (CSG-tree
scaling for unit conversion), euler-angle helpers, and low-level mesh
utilities.  No server-side reconstruction -- agents compose with these
primitives alongside sketch/feature/boolean MCP tools.
"""

from __future__ import annotations

import copy
import math
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from mcp_server.mesh_slicer import slice_mesh_at
except ImportError:
    from mesh_slicer import slice_mesh_at

Float3 = Tuple[float, float, float]

_CURVATURE_EPS = 1e-6
_COS_ORTHO = 0.05          # |dot| below this => directions are orthogonal
_DEFAULT_SIMILARITY_TOL = 0.05  # max normalized loop distance for prismatic
_MESH_EPSILON = 1e-9
_ROUND_PLACES = 6

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
