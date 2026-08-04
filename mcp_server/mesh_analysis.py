"""Pure-Python mesh analysis for the Fusion 360 MCP server (mesh-to-parametric
plan, Todo 2).

`analyze_mesh_data()` computes topology facts (watertight / manifold / vertex
and triangle counts), the enclosed volume via the divergence theorem, the
axis-aligned bounding box, mirror-symmetry plane candidates (paired bbox
extremes + normal-vector mirror agreement), primitive hints (plane regions via
normal clustering, a cylinder fit via deterministic normal-line sampling, and
a box fit via 3 orthogonal plane-pair fits), and a recommended reconstruction
strategy.

Design constraints:
  * stdlib ONLY (math, collections) - no numpy (Fusion's embedded Python does
    not guarantee it).
  * fully DETERMINISTIC - clustering and the RANSAC-style fitting use fixed,
    input-order-based sampling; there is no `random` module anywhere, so
    identical input always yields an identical report.
  * robust - empty / degenerate input returns a valid dict, never crashes.

Input conventions (flat lists or lists of (x,y,z) triples, both accepted):
  nodes:    vertex coordinates.  Flat:  [x0,y0,z0, x1,y1,z1, ...]
  indices:  triangle corner indices. Flat: [i0,i1,i2, i0,i1,i2, ...]
  normals:  per-face (one per triangle), per-corner (3 per triangle; the three
            are averaged), or empty/None (derived from geometry via normalized
            cross products).

Units: node coordinates are in centimeters (Fusion's internal unit).  The MCP
tool scales the report to the requested units via `scale_report()`; the
analysis itself is always in cm.
"""

from __future__ import annotations

import copy
import math
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

Float3 = Tuple[float, float, float]

_CLUSTER_ANGLE_DEG = 15.0      # normal clustering angular threshold
_CYL_SIDE_ANGLE_DEG = 85.0     # normals within 5 deg of perpendicular = "side"
_CYL_SIDE_FRACTION_MIN = 0.5   # min fraction of side normals for a cylinder
_CYL_COVERAGE_MIN = 0.85       # min azimuthal bucket coverage for a cylinder
_CYL_RADIUS_CV_MAX = 0.20      # max relative std of side-face centroid radius
_ORTHO_DOT_MAX = 0.05          # |dot| below this => directions are orthogonal
_PARALLEL_DOT_MIN = 0.95       # |dot| above this => directions are parallel
_BOX_FITTED_CONF = 0.40        # box "fitted" when >= this fraction matches
_BOX_CONF_PRISMATIC = 0.65     # box confidence for the prismatic strategy
_CYL_CONF_REVOLVED = 0.55      # cylinder confidence for the revolved strategy
_PLATE_FRACTION = 0.80         # dominant plane-region fraction => prismatic


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


def _norm(a: Sequence[float]) -> float:
    return math.sqrt(_dot(a, a))


def _normalize(a: Sequence[float]) -> Optional[Float3]:
    n = _norm(a)
    if n <= 1e-12:
        return None
    return (a[0] / n, a[1] / n, a[2] / n)


def _mean(points: Sequence[Sequence[float]]) -> Float3:
    if not points:
        return (0.0, 0.0, 0.0)
    n = len(points)
    return tuple(sum(p[k] for p in points) / n for k in range(3))


def _weld_vertices(node_list: Sequence[Float3],
                   tri_list: Sequence[Tuple[int, int, int]],
                   eps: float) -> Tuple[List[Float3], List[Tuple[int, int, int]]]:
    """Merge geometrically-identical vertices and remap triangle indices.

    Fusion's ``displayMesh`` is NON-indexed: every triangle corner is a
    distinct node, so watertightness via node-index adjacency would always
    report False on a closed body.  Welding recovers the shared-vertex
    topology that the divergence-theorem / edge-map analysis expects.
    """
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


def _round3(p: Sequence[float]) -> List[float]:
    return [round(float(v), 6) for v in p]


# --------------------------------------------------------------------------
# per-face normals / areas
# --------------------------------------------------------------------------

def _face_normals(node_list: Sequence[Float3],
                  tri_list: Sequence[Tuple[int, int, int]],
                  normals: Optional[Sequence]) -> List[Optional[Float3]]:
    """Per-face unit normals; prefers provided normals, falls back to geometry.

    Provided normals may be per-face (len == T) or per-corner (len == 3*T, in
    which case the three corners of each triangle are averaged).  Any other
    count is ignored and geometry is used.
    """
    t_count = len(tri_list)
    provided = _as_triples(normals) if normals else []
    if len(provided) == 3 * t_count and t_count:
        provided = [tuple(sum(provided[3 * t + k][c] for k in range(3)) / 3.0
                          for c in range(3))
                    for t in range(t_count)]
    if len(provided) == t_count and t_count:
        return [_normalize(n) for n in provided]
    out = []
    for (i0, i1, i2) in tri_list:
        a, b, c = node_list[i0], node_list[i1], node_list[i2]
        u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        out.append(_normalize(_cross(u, v)))
    return out


def _face_areas(node_list: Sequence[Float3],
                tri_list: Sequence[Tuple[int, int, int]]) -> List[float]:
    areas = []
    for (i0, i1, i2) in tri_list:
        a, b, c = node_list[i0], node_list[i1], node_list[i2]
        u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        areas.append(0.5 * _norm(_cross(u, v)))
    return areas


def _face_centroids(node_list: Sequence[Float3],
                    tri_list: Sequence[Tuple[int, int, int]]) -> List[Float3]:
    out = []
    for (i0, i1, i2) in tri_list:
        out.append(tuple((node_list[i0][k] + node_list[i1][k] + node_list[i2][k]) / 3.0
                         for k in range(3)))
    return out


# --------------------------------------------------------------------------
# symmetry
# --------------------------------------------------------------------------

def _symmetry_analysis(node_list: Sequence[Float3],
                       face_normals: Sequence[Optional[Float3]],
                       bbox_min: Sequence[float],
                       bbox_max: Sequence[float]) -> Dict:
    """Mirror-plane candidates from paired bbox extremes + normal agreement.

    A plane at the bbox midpoint along each axis is a candidate; it is scored
    by (a) normal-vector mirror agreement -- every face normal's reflection is
    also present -- and (b) vertex mirror agreement -- every vertex reflects
    onto another vertex (triangulation-independent, unlike face centroids).
    """
    max_extent = max(bmax - bmin for bmin, bmax in zip(bbox_min, bbox_max))
    if max_extent <= 1e-9:
        return {"candidates": [], "dominant_axis": None}
    normal_quant = 1e-3
    vertex_eps = max(1e-4, 1e-3 * max_extent)
    candidates = []
    for axis in range(3):
        lo, hi = bbox_min[axis], bbox_max[axis]
        if hi - lo <= 1e-9:
            continue
        mid = (lo + hi) / 2.0
        valid = [n for n in face_normals if n is not None]
        if not valid:
            continue
        keys = {tuple(round(c / normal_quant) for c in n) for n in valid}
        matched_n = 0
        for n in valid:
            refl = [n[0], n[1], n[2]]
            refl[axis] = -refl[axis]
            if tuple(round(c / normal_quant) for c in refl) in keys:
                matched_n += 1
        normal_agreement = matched_n / len(valid)
        vkeys = {tuple(round(p[k] / vertex_eps) for k in range(3)) for p in node_list}
        matched_v = 0
        for p in node_list:
            rp = [p[0], p[1], p[2]]
            rp[axis] = 2.0 * mid - rp[axis]
            if tuple(round(rp[k] / vertex_eps) for k in range(3)) in vkeys:
                matched_v += 1
        vertex_agreement = matched_v / len(node_list) if node_list else 0.0
        candidates.append({
            "axis": "XYZ"[axis],
            "plane_cm": round(mid, 6),
            "normal_agreement": round(normal_agreement, 4),
            "vertex_agreement": round(vertex_agreement, 4),
            "symmetric": bool(normal_agreement >= 0.9 and vertex_agreement >= 0.9),
        })
    symmetric = [c for c in candidates if c["symmetric"]]
    return {
        "candidates": candidates,
        "dominant_axis": symmetric[0]["axis"] if symmetric else None,
    }


# --------------------------------------------------------------------------
# primitive hints
# --------------------------------------------------------------------------

def _cluster_normals(face_normals: Sequence[Optional[Float3]],
                     face_areas: Sequence[float]) -> List[Dict]:
    """Deterministic greedy clustering of face normals by angular threshold."""
    cos_thr = math.cos(math.radians(_CLUSTER_ANGLE_DEG))
    clusters = []  # list of {"normal": List[float], "members": List[int]}
    for idx, n in enumerate(face_normals):
        if n is None:
            continue
        placed = False
        for cl in clusters:
            if _dot(n, cl["normal"]) >= cos_thr:
                cl["members"].append(idx)
                s = [0.0, 0.0, 0.0]
                for m in cl["members"]:
                    mn = face_normals[m]
                    for k in range(3):
                        s[k] += mn[k]
                u = _normalize(tuple(s))
                if u is not None:
                    cl["normal"] = list(u)
                placed = True
                break
        if not placed:
            clusters.append({"normal": list(n), "members": [idx]})
    out = []
    for cl in clusters:
        out.append({
            "normal": tuple(cl["normal"]),
            "face_count": len(cl["members"]),
            "area": sum(face_areas[m] for m in cl["members"]),
        })
    out.sort(key=lambda c: (c["area"], c["face_count"]), reverse=True)
    return out


def _box_fit(regions: Sequence[Dict],
             face_normals: Sequence[Optional[Float3]],
             face_areas: Sequence[float],
             total_area: float,
             bbox_min: Sequence[float],
             bbox_max: Sequence[float]) -> Dict:
    """Box fit: 3 mutually-orthogonal plane-pair fits from dominant clusters."""
    unfitted = {"fitted": False, "confidence": 0.0, "axes": None, "dims_cm": None}
    picked = []  # mutually orthogonal normal tuples
    for r in regions:
        n = tuple(r["normal"])
        if all(abs(_dot(n, p)) < _ORTHO_DOT_MAX for p in picked):
            picked.append(n)
            if len(picked) == 3:
                break
    if len(picked) < 3:
        return unfitted
    matched_area = 0.0
    for i, n in enumerate(face_normals):
        if n is None:
            continue
        if any(abs(_dot(n, p)) > _PARALLEL_DOT_MIN for p in picked):
            matched_area += face_areas[i]
    confidence = matched_area / total_area if total_area > 0 else 0.0
    return {
        "fitted": confidence >= _BOX_FITTED_CONF,
        "confidence": round(confidence, 4),
        "axes": [_round3(p) for p in picked],
        "dims_cm": [round(bmax - bmin, 6) for bmin, bmax in zip(bbox_min, bbox_max)],
    }


def _cylinder_unfitted() -> Dict:
    return {"fitted": False, "confidence": 0.0, "axis": None,
            "axis_point_cm": None, "radius_cm": None, "height_cm": None,
            "side_fraction": 0.0, "coverage": 0.0}


def _azimuthal_coverage(v: Float3,
                        face_normals: Sequence[Optional[Float3]],
                        side_indices: Sequence[int]) -> float:
    """Fraction of 16 azimuthal buckets covered by side-face normals around v."""
    if abs(v[0]) < 0.9:
        u = _normalize(_cross(v, (1.0, 0.0, 0.0)))
    else:
        u = _normalize(_cross(v, (0.0, 1.0, 0.0)))
    if u is None:
        return 0.0
    w = _cross(v, u)
    buckets = set()
    for i in side_indices:
        n = face_normals[i]
        ang = math.atan2(_dot(n, w), _dot(n, u))
        # floor (not round): round() aliases normals sitting on bucket
        # boundaries, e.g. the 22.5-deg-spaced facets of a 16-gon prism.
        b = int(math.floor(ang / (2.0 * math.pi) * 16.0)) % 16
        buckets.add(b)
    return len(buckets) / 16.0


def _radius_stats(v: Float3,
                  centroids: Sequence[Float3],
                  side_indices: Sequence[int]) -> Tuple[Optional[float], Optional[float]]:
    """Mean radius of side-face centroids around axis (p0, v) and its CV."""
    p0 = _mean(centroids)
    radii = []
    for i in side_indices:
        c = centroids[i]
        d = _cross((c[0] - p0[0], c[1] - p0[1], c[2] - p0[2]), v)
        radii.append(_norm(d))
    if not radii:
        return None, None
    mean_r = sum(radii) / len(radii)
    if mean_r <= 1e-9:
        return None, None
    var = sum((r - mean_r) ** 2 for r in radii) / len(radii)
    return mean_r, math.sqrt(var) / mean_r


def _extent_along(v: Float3, node_list: Sequence[Float3]) -> float:
    proj = [_dot(p, v) for p in node_list]
    return max(proj) - min(proj)


def _cylinder_fit(node_list: Sequence[Float3],
                  centroids: Sequence[Float3],
                  face_normals: Sequence[Optional[Float3]],
                  face_areas: Sequence[float],
                  regions: Sequence[Dict]) -> Dict:
    """Cylinder fit via deterministic normal-line sampling (RANSAC-style).

    Candidate axis directions come from cross products of (a) the dominant
    normal-cluster representatives and (b) an input-order-based sample of the
    face normals -- fixed and reproducible, no random module.
    """
    valid = [n for n in face_normals if n is not None]
    if not valid:
        return _cylinder_unfitted()
    total_area = sum(face_areas) or 0.0
    candidates = []
    reps = [tuple(r["normal"]) for r in regions[:8]]
    for i in range(len(reps)):
        for j in range(i + 1, len(reps)):
            u = _normalize(_cross(reps[i], reps[j]))
            if u is not None:
                candidates.append(u)
    step = max(1, len(valid) // 12)
    sample = [valid[i] for i in range(0, len(valid), step)][:12]
    for i in range(len(sample)):
        for j in range(i + 1, len(sample)):
            u = _normalize(_cross(sample[i], sample[j]))
            if u is not None:
                candidates.append(u)
    if not candidates:
        return _cylinder_unfitted()
    side_cos = math.cos(math.radians(_CYL_SIDE_ANGLE_DEG))
    best = None
    for v in candidates:
        side = [i for i, n in enumerate(face_normals)
                if n is not None and abs(_dot(n, v)) < side_cos]
        if not side:
            continue
        side_area = sum(face_areas[i] for i in side)
        side_fraction = side_area / total_area if total_area > 0 else 0.0
        if side_fraction < _CYL_SIDE_FRACTION_MIN:
            continue
        coverage = _azimuthal_coverage(v, face_normals, side)
        if coverage < _CYL_COVERAGE_MIN:
            continue
        radius, cv = _radius_stats(v, centroids, side)
        if radius is None or cv > _CYL_RADIUS_CV_MAX:
            continue
        score = side_fraction * coverage
        if best is None or score > best[0]:
            best = (score, v, side_fraction, coverage, radius, cv)
    if best is None:
        return _cylinder_unfitted()
    score, v, side_fraction, coverage, radius, _cv = best
    return {
        "fitted": True,
        "confidence": round(score, 4),
        "axis": _round3(v),
        "axis_point_cm": _round3(_mean(centroids)),
        "radius_cm": round(radius, 6),
        "height_cm": round(_extent_along(v, node_list), 6),
        "side_fraction": round(side_fraction, 4),
        "coverage": round(coverage, 4),
    }


def _primitive_hints(node_list: Sequence[Float3],
                     centroids: Sequence[Float3],
                     face_normals: Sequence[Optional[Float3]],
                     face_areas: Sequence[float],
                     bbox_min: Sequence[float],
                     bbox_max: Sequence[float]) -> Dict:
    total_area = sum(face_areas)
    regions = _cluster_normals(face_normals, face_areas)
    plane_regions = [
        {
            "normal": _round3(r["normal"]),
            "face_count": r["face_count"],
            "area_fraction": round(r["area"] / total_area, 4) if total_area > 0 else 0.0,
        }
        for r in regions
    ]
    return {
        "plane_regions": plane_regions,
        "box": _box_fit(regions, face_normals, face_areas, total_area,
                        bbox_min, bbox_max),
        "cylinder": _cylinder_fit(node_list, centroids, face_normals,
                                  face_areas, regions),
    }


# --------------------------------------------------------------------------
# strategy
# --------------------------------------------------------------------------

def _recommended_strategy(watertight: bool, hints: Dict) -> str:
    box = hints.get("box") or {}
    cyl = hints.get("cylinder") or {}
    if box.get("fitted") and box.get("confidence", 0.0) >= _BOX_CONF_PRISMATIC:
        return "prismatic"
    if cyl.get("fitted") and cyl.get("confidence", 0.0) >= _CYL_CONF_REVOLVED:
        return "revolved"
    regions = hints.get("plane_regions") or []
    if regions and regions[0]["area_fraction"] >= _PLATE_FRACTION:
        return "prismatic"
    if box.get("fitted"):
        return "csg_decompose"
    if watertight and len(regions) >= 3:
        return "csg_decompose"
    return "organic"


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def analyze_mesh_data(nodes: Sequence, indices: Sequence,
                      normals: Optional[Sequence] = None) -> Dict:
    """Analyze a triangle mesh and return the measured-facts report.

    Returns a dict with keys: watertight, manifold, vertex_count,
    triangle_count, volume_cm3, bounding_box_cm, symmetry, primitive_hints,
    recommended_strategy.  Never raises on empty/degenerate input; raises
    ValueError only for structurally invalid input (flat list lengths not
    divisible by 3, or triangle indices out of range).
    """
    node_list = _as_triples(nodes)
    tri_list = [(int(a), int(b), int(c)) for a, b, c in _as_triples(indices)]
    vertex_count = len(node_list)
    triangle_count = len(tri_list)

    empty_hints = {
        "plane_regions": [],
        "box": {"fitted": False, "confidence": 0.0, "axes": None, "dims_cm": None},
        "cylinder": _cylinder_unfitted(),
    }
    if triangle_count == 0:
        if node_list:
            bbox_min = [min(p[k] for p in node_list) for k in range(3)]
            bbox_max = [max(p[k] for p in node_list) for k in range(3)]
            bbox = [_round3(bbox_min), _round3(bbox_max)]
        else:
            bbox = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
        return {
            "watertight": False,
            "manifold": False,
            "vertex_count": vertex_count,
            "triangle_count": 0,
            "volume_cm3": 0.0,
            "bounding_box_cm": bbox,
            "symmetry": {"candidates": [], "dominant_axis": None},
            "primitive_hints": empty_hints,
            "recommended_strategy": "organic",
        }

    for (i0, i1, i2) in tri_list:
        for ix in (i0, i1, i2):
            if not 0 <= ix < vertex_count:
                raise ValueError(
                    f"triangle index {ix} out of range for {vertex_count} nodes")

    extent = max(max(p[k] for p in node_list) - min(p[k] for p in node_list)
                 for k in range(3))
    node_list, tri_list = _weld_vertices(
        node_list, tri_list, max(1e-9, 1e-7 * extent))
    vertex_count = len(node_list)

    edge_counts = defaultdict(int)
    degenerate = False
    for (i0, i1, i2) in tri_list:
        if i0 == i1 or i1 == i2 or i0 == i2:
            degenerate = True
            continue
        for a, b in ((i0, i1), (i1, i2), (i2, i0)):
            key = (a, b) if a < b else (b, a)
            edge_counts[key] += 1
    max_edge_incidence = max(edge_counts.values(), default=0)
    watertight = (not degenerate) and all(c == 2 for c in edge_counts.values())
    manifold = (not degenerate) and max_edge_incidence <= 2

    signed_sum = 0.0
    for (i0, i1, i2) in tri_list:
        ax, ay, az = node_list[i0]
        bx, by, bz = node_list[i1]
        cx, cy, cz = node_list[i2]
        signed_sum += (ax * (by * cz - bz * cy)
                       - ay * (bx * cz - bz * cx)
                       + az * (bx * cy - by * cx)) / 6.0

    bbox_min = [min(p[k] for p in node_list) for k in range(3)]
    bbox_max = [max(p[k] for p in node_list) for k in range(3)]

    face_normals = _face_normals(node_list, tri_list, normals)
    face_areas = _face_areas(node_list, tri_list)
    centroids = _face_centroids(node_list, tri_list)
    symmetry = _symmetry_analysis(node_list, face_normals, bbox_min, bbox_max)
    hints = _primitive_hints(node_list, centroids, face_normals, face_areas,
                             bbox_min, bbox_max)

    return {
        "watertight": watertight,
        "manifold": manifold,
        "vertex_count": vertex_count,
        "triangle_count": triangle_count,
        "volume_cm3": abs(signed_sum),
        "bounding_box_cm": [_round3(bbox_min), _round3(bbox_max)],
        "symmetry": symmetry,
        "primitive_hints": hints,
        "recommended_strategy": _recommended_strategy(watertight, hints),
    }


def scale_report(report: Dict, factor: float) -> Dict:
    """Deep-copy `report` scaling lengths by `factor` and volume by factor**3.

    Used by the MCP tool to convert cm-based measurements to mm / in.  Counts,
    booleans, the strategy, and unit-less agreement scores are not scaled.
    """
    out = copy.deepcopy(report)
    f = float(factor)
    if "volume_cm3" in out:
        out["volume_cm3"] = round(out["volume_cm3"] * f ** 3, 6)
    if "bounding_box_cm" in out:
        out["bounding_box_cm"] = [
            [round(c * f, 6) for c in p] for p in out["bounding_box_cm"]]
    sym = out.get("symmetry") or {}
    for cand in sym.get("candidates", []):
        if "plane_cm" in cand:
            cand["plane_cm"] = round(cand["plane_cm"] * f, 6)
    hints = out.get("primitive_hints") or {}
    box = hints.get("box")
    if box and box.get("dims_cm") is not None:
        box["dims_cm"] = [round(d * f, 6) for d in box["dims_cm"]]
    cyl = hints.get("cylinder")
    if cyl and cyl.get("fitted"):
        if cyl.get("radius_cm") is not None:
            cyl["radius_cm"] = round(cyl["radius_cm"] * f, 6)
        if cyl.get("height_cm") is not None:
            cyl["height_cm"] = round(cyl["height_cm"] * f, 6)
        if cyl.get("axis_point_cm") is not None:
            cyl["axis_point_cm"] = [round(c * f, 6) for c in cyl["axis_point_cm"]]
    return out
