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
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import trimesh

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
# face decomposition (trimesh-based)
# --------------------------------------------------------------------------

def _compute_polygon_angles_3d(vertices_list: List[List[float]]) -> List[float]:
    """Compute interior angles of a 3D polygon projected to its best-fit 2D plane.

    Uses SVD to find the best-fit plane, projects 3D vertices to 2D, then
    computes the interior angle at each vertex via dot product.
    """
    pts = np.array(vertices_list, dtype=np.float64)
    centroid = pts.mean(axis=0)
    centered = pts - centroid
    U, S, Vt = np.linalg.svd(centered)
    basis = Vt[:2]
    proj_2d = centered @ basis.T

    n = len(proj_2d)
    angles = []
    for i in range(n):
        prev = proj_2d[(i - 1) % n]
        curr = proj_2d[i]
        nxt = proj_2d[(i + 1) % n]
        v1 = prev - curr
        v2 = nxt - curr
        v1_norm = float(np.linalg.norm(v1))
        v2_norm = float(np.linalg.norm(v2))
        if v1_norm < 1e-12 or v2_norm < 1e-12:
            angles.append(0.0)
            continue
        cos_ang = float(np.clip(np.dot(v1, v2) / (v1_norm * v2_norm), -1.0, 1.0))
        ang_rad = math.acos(cos_ang)
        angles.append(round(math.degrees(ang_rad), 2))
    return angles


# --------------------------------------------------------------------------
# curved-surface fitting (per-patch primitive classification)
# --------------------------------------------------------------------------

def _stride_sample(arr, max_samples=50):
    """Deterministic stride-based sampling: ~max_samples items in input order.

    No random module — identical input always yields identical output.
    """
    n = len(arr)
    if n <= max_samples:
        return arr
    step = max(1, n // max_samples)
    return arr[::step][:max_samples]


def _find_axis_from_cross_products(normals, cluster_deg=10.0):
    """Find the dominant axis direction from cross products of normal pairs.

    For a cylinder (normals perpendicular to axis), n_i x n_j is parallel to
    the axis.  Candidates are canonicalised (largest-abs component made
    positive), greedy-clustered by angular threshold, and the largest cluster
    mean is returned.

    Returns a unit-length numpy array, or None.
    """
    samples = _stride_sample(normals, 50)
    candidates = []
    n = len(samples)
    for i in range(n):
        for j in range(i + 1, n):
            cross = np.cross(samples[i], samples[j])
            cnorm = np.linalg.norm(cross)
            if cnorm > 1e-9:
                candidates.append(cross / cnorm)
    if not candidates:
        return None
    candidates = np.array(candidates)
    # Canonicalise so +v and -v map to the same direction.
    canonical = candidates.copy()
    for k in range(len(canonical)):
        idx = int(np.argmax(np.abs(canonical[k])))
        if canonical[k, idx] < 0:
            canonical[k] = -canonical[k]
    # Greedy clustering.
    cos_thr = math.cos(math.radians(cluster_deg))
    used = np.zeros(len(canonical), dtype=bool)
    best_dir = None
    best_size = 0
    for i in range(len(canonical)):
        if used[i]:
            continue
        members = [i]
        used[i] = True
        for j in range(i + 1, len(canonical)):
            if not used[j] and np.dot(canonical[i], canonical[j]) > cos_thr:
                members.append(j)
                used[j] = True
        if len(members) > best_size:
            best_size = len(members)
            best_dir = canonical[members].mean(axis=0)
    if best_dir is None:
        return None
    dnorm = np.linalg.norm(best_dir)
    if dnorm < 1e-12:
        return None
    return best_dir / dnorm


def _patch_centroids(verts, faces):
    """Compute per-face centroid for valid triangles (vertex indices in range)."""
    out = []
    for tri in faces:
        if len(tri) == 3 and np.all(np.asarray(tri) < len(verts)):
            out.append(verts[tri].mean(axis=0))
    return np.array(out) if out else np.empty((0, 3))


def _align_and_normalize(centroids, normals):
    """Trim to equal length, drop zero-length normals, unit-normalise.

    Returns (centroids, normals) aligned 1:1, or (empty, empty) on failure.
    """
    n = min(len(centroids), len(normals))
    centroids = centroids[:n]
    normals = np.asarray(normals[:n], dtype=np.float64)
    mags = np.linalg.norm(normals, axis=1)
    valid = mags > 1e-12
    return centroids[valid], normals[valid] / mags[valid, np.newaxis]


def _fit_cylinder(nodes_array, face_indices, face_normals):
    """Deterministic cylinder fit for a curved patch.

    Strategy:
      1. Cylinder side-face normals are perpendicular to the axis.
      2. Axis direction: cross products of stride-sampled normal pairs,
         clustered to find the dominant direction.
      3. Radius: median distance from side-face centroids to the axis line.
      4. Height: vertex extent projected onto the axis.
      5. Confidence: fraction of normals perpendicular to the axis.

    Returns dict with surface_type, axis, radius_cm, axis_point_cm,
    height_cm, confidence; or None if confidence < 0.3.
    """
    verts = np.asarray(nodes_array, dtype=np.float64)
    faces = np.asarray(face_indices, dtype=np.int64)
    centroids = _patch_centroids(verts, faces)
    if len(centroids) < 3:
        return None
    centroids, normals = _align_and_normalize(centroids, face_normals)
    if len(normals) < 3:
        return None

    axis = _find_axis_from_cross_products(normals)
    if axis is None:
        return None

    # Confidence: fraction of normals perpendicular to axis.
    dots = np.abs(normals @ axis)
    perp_mask = dots < 0.15
    confidence = float(np.count_nonzero(perp_mask) / len(normals))
    if confidence < 0.3:
        return None

    # Axis point: centroid of all vertices.
    axis_point = verts.mean(axis=0)

    # Radius: median distance from side-face centroids to axis line.
    side_centroids = centroids[perp_mask]
    if len(side_centroids) == 0:
        return None
    rel = side_centroids - axis_point
    dists = np.linalg.norm(np.cross(rel, axis), axis=1)
    radius = float(np.median(dists))
    if radius < 1e-9:
        return None

    # Height: vertex extent along axis.
    projs = verts @ axis
    height = float(projs.max() - projs.min())

    return {
        "surface_type": "cylinder",
        "axis": [round(float(c), 6) for c in axis],
        "radius_cm": round(radius, 6),
        "axis_point_cm": [round(float(c), 6) for c in axis_point],
        "height_cm": round(height, 6),
        "confidence": round(confidence, 4),
    }


def _fit_sphere(nodes_array, face_indices, face_normals):
    """Deterministic sphere fit for a curved patch.

    Strategy:
      1. Sphere normals all point radially outward from the centre.
      2. Centre: least-squares intersection of face-normal rays (each ray
         passes through the centre for a perfect sphere).
      3. Radius: median vertex distance from centre.
      4. Confidence: inverse of the radius coefficient of variation.

    Returns dict with surface_type, center_cm, radius_cm, confidence;
    or None if confidence < 0.3.
    """
    verts = np.asarray(nodes_array, dtype=np.float64)
    faces = np.asarray(face_indices, dtype=np.int64)
    centroids = _patch_centroids(verts, faces)
    if len(centroids) < 4:
        return None
    centroids, normals = _align_and_normalize(centroids, face_normals)
    if len(normals) < 4:
        return None

    # Least-squares ray intersection:  (Σ M_i) · centre = Σ M_i · c_i
    # where M_i = I − d_i d_iᵀ  (projection onto plane ⊥ to ray direction).
    samples_c = _stride_sample(centroids, 30)
    samples_n = _stride_sample(normals, 30)
    n_use = min(len(samples_c), len(samples_n))
    A = np.zeros((3, 3))
    b = np.zeros(3)
    for i in range(n_use):
        d = samples_n[i]
        M = np.eye(3) - np.outer(d, d)
        A += M
        b += M @ samples_c[i]
    try:
        center = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        return None

    # Radius from vertex distances.
    dists = np.linalg.norm(verts - center, axis=1)
    radius = float(np.median(dists))
    if radius < 1e-9:
        return None

    # Confidence: low coefficient of variation ⇒ high confidence.
    mean_r = float(np.mean(dists))
    if mean_r < 1e-9:
        return None
    cv = float(np.std(dists)) / mean_r
    confidence = max(0.0, 1.0 - cv * 5.0)
    if confidence < 0.3:
        return None

    return {
        "surface_type": "sphere",
        "center_cm": [round(float(c), 6) for c in center],
        "radius_cm": round(radius, 6),
        "confidence": round(confidence, 4),
    }


def _fit_cone(nodes_array, face_indices, face_normals):
    """Deterministic cone fit for a curved patch.

    Strategy:
      1. Cone normals make a constant angle with the axis.
      2. Axis: eigenvector with the smallest eigenvalue of the covariance
         of face normals (the normals vary least along the axis direction).
      3. Half-angle: 90° − median(acute angle between normal and axis).
      4. Apex: median of per-face apex-height estimates along the axis.
      5. Confidence: inverse of the half-angle coefficient of variation.

    Returns dict with surface_type, axis, half_angle_deg, apex_cm,
    confidence; or None if confidence < 0.3.
    """
    verts = np.asarray(nodes_array, dtype=np.float64)
    faces = np.asarray(face_indices, dtype=np.int64)
    centroids = _patch_centroids(verts, faces)
    if len(centroids) < 4:
        return None
    centroids, normals = _align_and_normalize(centroids, face_normals)
    if len(normals) < 4:
        return None

    # Axis from PCA: normals on a cone-of-revolution lie on a narrow cone
    # around the axis; the covariance's smallest-eigenvalue eigenvector
    # is the axis direction.
    mean_n = normals.mean(axis=0)
    centered = normals - mean_n
    cov = (centered.T @ centered) / len(normals)
    eigvals, eigvecs = np.linalg.eigh(cov)
    axis = eigvecs[:, 0]  # smallest eigenvalue
    anorm = np.linalg.norm(axis)
    if anorm < 1e-12:
        return None
    axis = axis / anorm

    # Half-angle: acute angle between each normal and the axis.
    dots = np.clip(normals @ axis, -1.0, 1.0)
    angles = np.degrees(np.arccos(dots))
    acute = np.minimum(angles, 180.0 - angles)
    median_angle = float(np.median(acute))

    # Reject cylinder-like (normals ⊥ axis ⇒ angle ≈ 90°) and flat
    # (normals ∥ axis ⇒ angle ≈ 0°).
    if median_angle > 80.0 or median_angle < 5.0:
        return None

    mean_a = float(np.mean(acute))
    std_a = float(np.std(acute))
    cv = std_a / mean_a if mean_a > 1e-9 else 1.0
    if cv > 0.3:
        return None
    confidence = max(0.0, 1.0 - cv * 3.0)
    if confidence < 0.3:
        return None

    half_angle = 90.0 - median_angle
    tan_ha = math.tan(math.radians(half_angle))
    if tan_ha < 1e-6:
        return None

    # Apex: for each face, h_apex = h_i ∓ r_i / tan(half_angle).
    p0 = verts.mean(axis=0)
    rel_c = centroids - p0
    h_vals = rel_c @ axis
    radial = rel_c - np.outer(h_vals, axis)
    r_vals = np.linalg.norm(radial, axis=1)
    h_minus = h_vals - r_vals / tan_ha
    h_plus = h_vals + r_vals / tan_ha
    var_minus = float(np.var(h_minus)) if len(h_minus) > 0 else float('inf')
    var_plus = float(np.var(h_plus)) if len(h_plus) > 0 else float('inf')
    h_apex_vals = h_minus if var_minus <= var_plus else h_plus
    h_apex = float(np.median(h_apex_vals))
    apex = p0 + h_apex * axis

    return {
        "surface_type": "cone",
        "axis": [round(float(c), 6) for c in axis],
        "half_angle_deg": round(float(half_angle), 4),
        "apex_cm": [round(float(c), 6) for c in apex],
        "confidence": round(float(confidence), 4),
    }


def _estimate_freeform_curvature(mesh):
    """Rough mean-curvature proxy for a freeform patch.

    Uses the area-weighted mean absolute dihedral angle across interior
    edges (radians × cm / cm ⇒ radians).  Returns 0.0 on failure.
    """
    try:
        angles = mesh.face_adjacency_angles
        edges = mesh.face_adjacency_edges
        if angles is None or edges is None or len(angles) == 0:
            return 0.0
        edge_vecs = mesh.vertices[edges[:, 1]] - mesh.vertices[edges[:, 0]]
        edge_lens = np.linalg.norm(edge_vecs, axis=1)
        total = float(np.sum(np.abs(angles) * edge_lens))
        weight = float(np.sum(edge_lens))
        if weight < 1e-12:
            return 0.0
        return round(total / weight, 6)
    except Exception:
        return 0.0


def _classify_curved_patch(patch):
    """Classify a single curved-patch trimesh and return the surface dict.

    Tries cylinder → sphere → cone → freeform, in that order.
    """
    verts = np.asarray(patch.vertices, dtype=np.float64)
    faces = np.asarray(patch.faces, dtype=np.int64)
    normals = np.asarray(patch.face_normals, dtype=np.float64)

    surface = _fit_cylinder(verts, faces, normals)
    if surface is None:
        surface = _fit_sphere(verts, faces, normals)
    if surface is None:
        surface = _fit_cone(verts, faces, normals)
    if surface is None:
        surface = {
            "surface_type": "freeform",
            "mean_curvature": _estimate_freeform_curvature(patch),
        }
    return surface


# --------------------------------------------------------------------------
# face decomposition (trimesh-based)
# --------------------------------------------------------------------------

# --- Bug A-E helpers (2026-08-04) -------------------------------------------

_MIN_FACE_AREA = 1e-4   # Bug C: degenerate-face area threshold


def _max_extent_nodes(node_list):
    """Max per-axis span of a list of (x, y, z) coordinate tuples."""
    if not node_list:
        return 0.0
    return max(max(p[k] for p in node_list) - min(p[k] for p in node_list)
               for k in range(3))


_QUANT_REL_CAP = 1e-3   # quantization steps are tiny relative to model extent


def _detect_quantization_step(node_list):
    """Most frequent small nonzero coordinate delta (exporter rounding step).

    STL / OBJ exporters round every coordinate to a fixed decimal count
    (6 decimals -> 1e-6, 4 decimals -> 1e-4).  Adjacent sorted unique
    coordinate values then differ by small integer multiples of that step,
    so the quantization step is the most frequent *small* delta, where small
    means at most ``max(1e-4, 1e-3 * extent)``.  A genuine global rounding
    grid recurs across many deltas (count > 1), while a one-off seam gap does
    not, so single-occurrence deltas are ignored.

    Deterministic: ``Counter`` over per-axis deltas rounded to 9 decimals,
    most-frequent-first with smallest-step tie-break (no set-iteration-order
    dependence).

    Returns ``0.0`` when no recurring small step exists (uniform coordinates,
    integer-coordinate fixtures) so callers fall back to their extent-based
    floors: ``eps >= max(1e-9, 1e-7 * extent)`` and ``snap_tol >= 1e-5``.
    """
    extent = _max_extent_nodes(node_list)
    cap = max(1e-4, _QUANT_REL_CAP * extent)
    steps = Counter()
    for k in range(3):
        vals = sorted({p[k] for p in node_list})
        for a, b in zip(vals, vals[1:]):
            d = round(abs(b - a), 9)
            if 0 < d <= cap:
                steps[d] += 1
    if not steps or max(steps.values()) < 2:
        return 0.0
    return max(steps, key=lambda d: (steps[d], -d))


def _detect_components(welded_tris):
    """Connected-component IDs for triangles via shared-edge union-find.

    Two triangles are in the same component iff they share at least one edge
    (after welding).  Returns a list of component IDs, one per triangle.
    Deterministic: greedy in input order, no *random*.
    """
    n = len(welded_tris)
    parent = list(range(n))

    def _find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    edge_owner: Dict[Tuple[int, int], int] = {}
    for ti, (a, b, c) in enumerate(welded_tris):
        for e in ((min(a, b), max(a, b)),
                  (min(b, c), max(b, c)),
                  (min(a, c), max(a, c))):
            prev = edge_owner.get(e)
            if prev is None:
                edge_owner[e] = ti
            else:
                ra, rb = _find(prev), _find(ti)
                if ra != rb:
                    parent[ra] = rb

    comp_map: Dict[int, int] = {}
    next_id = 0
    result: List[int] = []
    for ti in range(n):
        r = _find(ti)
        cid = comp_map.get(r)
        if cid is None:
            cid = next_id
            comp_map[r] = next_id
            next_id += 1
        result.append(cid)
    return result


def _coplanar_with_region(face_plane, region_plane, cos_tol, offset_tol):
    """Coplanarity predicate between a face's canonical plane and a region's.

    Each plane is ``(unit_normal, offset)``, canonicalised so the dominant
    normal component is positive.  Returns True iff the normals agree within
    *cos_tol* (absolute dot product) and the offsets differ by less than
    *offset_tol*.  This is exactly the predicate the global first-match path
    uses; R-2 keeps it as a clearly-isolated helper so R-7 can swap in a
    running plane-fit.
    """
    nu, offset = face_plane
    gn, goffset = region_plane
    dot = abs(float(nu[0] * gn[0] + nu[1] * gn[1] + nu[2] * gn[2]))
    return dot >= cos_tol and abs(offset - goffset) < offset_tol


def _cluster_near_vertices(v_arr, tol):
    """Union-find clusters of vertices nearer than *tol* to each other.

    Deterministic grid-based clustering: each vertex probes the 27 cells
    around its ``round(coord / tol)`` key (any two vertices within *tol* sit
    in the same or an adjacent cell) and unions with earlier vertices found
    closer than *tol*.  Returns ``{vertex_index: cluster_id}`` with cluster
    ids assigned in ascending first-member index order.

    R-2 uses this to recover edge-adjacency across exporter seams that
    welding (eps) cannot bridge but snapping (snap_tol) would: the seam
    fixtures duplicate a shared edge at ~1e-4 offsets, so a strict welded
    edge map would split a touching face pair the rest of the pipeline
    merges.
    """
    n = len(v_arr)
    parent = list(range(n))

    def _find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    tol2 = tol * tol
    cells: Dict[Tuple[int, int, int], List[int]] = defaultdict(list)
    for vi in range(n):
        p = v_arr[vi]
        cell = (round(p[0] / tol), round(p[1] / tol), round(p[2] / tol))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for u in cells.get((cell[0] + dx, cell[1] + dy,
                                        cell[2] + dz), ()):
                        q = v_arr[u]
                        d2 = ((p[0] - q[0]) * (p[0] - q[0])
                              + (p[1] - q[1]) * (p[1] - q[1])
                              + (p[2] - q[2]) * (p[2] - q[2]))
                        if d2 < tol2:
                            ru, rv = _find(u), _find(vi)
                            if ru != rv:
                                parent[ru] = rv
        cells[cell].append(vi)

    clusters: Dict[int, int] = {}
    out: Dict[int, int] = {}
    for vi in range(n):
        r = _find(vi)
        cid = clusters.get(r)
        if cid is None:
            cid = len(clusters)
            clusters[r] = cid
        out[vi] = cid
    return out


def _group_planar_triangles(v_arr, f_arr, tri_normals, angle_tol_deg, extent):
    """Greedy-group triangles by coplanarity (normal within *angle_tol_deg*,
    plane offset within tol).

    Returns a list of dicts: ``{"normal": np.ndarray(3), "offset": float,
    "tri_indices": List[int]}``.

    R-2: a connectivity-constrained region-growing FIRST PASS is the primary
    path.  Regions are grown from the largest-area unassigned triangle over
    edge-adjacent faces (edge map over the welded soup, with near-coincident
    seam vertices clustered first) using the same coplanarity predicate as
    the global path.  Any triangle the connectivity pass cannot place falls
    through to the unchanged global first-match path.  Deterministic: seeds
    by largest area (smallest index on ties), neighbors traversed in sorted
    index order, groups emitted in seed order.
    """
    cos_tol = math.cos(math.radians(angle_tol_deg))
    offset_tol = max(1e-6, 1e-4 * extent)
    # Edge-coincidence tolerance = extent-based floor of snap_tol: edges
    # closer than this are the same seam edge (duplicated at exporter
    # precision) and must merge here, or touching seam faces would split.
    edge_adj_tol = max(1e-5, 5e-4 * extent)

    n_faces = len(f_arr)

    # Canonical per-face plane + area (degenerate faces -> None plane, ~0 area)
    face_planes: List[Optional[Tuple[np.ndarray, float]]] = [None] * n_faces
    face_areas: List[float] = [0.0] * n_faces
    for ti in range(n_faces):
        n = tri_normals[ti]
        nn = float(np.linalg.norm(n))
        if nn < 1e-12:
            continue                       # degenerate normal -> skip
        nu = n / nn
        v0 = v_arr[f_arr[ti, 0]]
        offset = float(nu[0] * v0[0] + nu[1] * v0[1] + nu[2] * v0[2])
        # Canonicalise: flip so the dominant component is positive
        dom = int(np.argmax(np.abs(nu)))
        if nu[dom] < 0:
            nu = -nu
            offset = -offset
        face_planes[ti] = (nu, offset)
        a = v_arr[f_arr[ti, 0]]
        b = v_arr[f_arr[ti, 1]]
        c = v_arr[f_arr[ti, 2]]
        face_areas[ti] = 0.5 * float(np.linalg.norm(np.cross(b - a, c - a)))

    # Face adjacency via an O(F) edge -> face-set map over the welded soup,
    # keyed by cluster ids so seam-coincident edges count as shared.
    cluster = _cluster_near_vertices(v_arr, edge_adj_tol)
    edge_faces: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for ti in range(n_faces):
        a = cluster[int(f_arr[ti, 0])]
        b = cluster[int(f_arr[ti, 1])]
        c = cluster[int(f_arr[ti, 2])]
        for e in ((min(a, b), max(a, b)),
                  (min(b, c), max(b, c)),
                  (min(a, c), max(a, c))):
            if e[0] != e[1]:
                edge_faces[e].append(ti)
    adj: List[set] = [set() for _ in range(n_faces)]
    for faces in edge_faces.values():
        m = len(faces)
        for i in range(m):
            fi = faces[i]
            for j in range(i + 1, m):
                fj = faces[j]
                adj[fi].add(fj)
                adj[fj].add(fi)

    assigned = [False] * n_faces
    groups: List[Dict] = []

    # --- Connectivity-constrained first pass (primary) ---
    seed_order = sorted(
        (ti for ti in range(n_faces) if face_planes[ti] is not None),
        key=lambda ti: (-face_areas[ti], ti))
    for seed in seed_order:
        if assigned[seed]:
            continue
        gn, goffset = face_planes[seed]
        region: List[int] = []
        queue = [seed]
        seen = {seed}
        qi = 0
        while qi < len(queue):
            cur = queue[qi]
            qi += 1
            region.append(cur)
            for nbr in sorted(adj[cur]):
                if nbr in seen or face_planes[nbr] is None:
                    continue
                if not _coplanar_with_region(
                        face_planes[nbr], (gn, goffset),
                        cos_tol, offset_tol):
                    continue
                seen.add(nbr)
                queue.append(nbr)
        for ti in region:
            assigned[ti] = True
        groups.append({"normal": gn.copy(), "offset": goffset,
                       "tri_indices": sorted(region)})

    # --- Global fallback (unchanged first-match path) ---
    fallback_groups: List[Dict] = []
    for ti in range(n_faces):
        if assigned[ti]:
            continue
        plane = face_planes[ti]
        if plane is None:
            continue                       # degenerate normal -> skip
        nu, offset = plane
        placed = False
        for g in fallback_groups:
            gn = g["normal"]
            dot = abs(float(nu[0] * gn[0] + nu[1] * gn[1] + nu[2] * gn[2]))
            if dot >= cos_tol and abs(offset - g["offset"]) < offset_tol:
                g["tri_indices"].append(ti)
                placed = True
                break
        if not placed:
            fallback_groups.append({"normal": nu.copy(), "offset": offset,
                                    "tri_indices": [ti]})
    groups.extend(fallback_groups)
    return groups


def _chain_directed_edges(edges):
    """Chain directed ``(src, dst)`` edges into ordered vertex-index loops.

    Consumes edges greedily in sorted-vertex order (deterministic).
    """
    out_adj: Dict[int, List[int]] = defaultdict(list)
    for a, b in edges:
        out_adj[a].append(b)
    loops: List[List[int]] = []
    for start in sorted(out_adj):
        while out_adj[start]:
            loop = [start]
            current = start
            while True:
                nbrs = out_adj.get(current)
                if not nbrs:
                    break
                nxt = nbrs.pop()
                if nxt == start:
                    break
                loop.append(nxt)
                current = nxt
            if len(loop) >= 3:
                loops.append(loop)
    return loops


def _split_pinch_loops(loops):
    """Split loops at pinch points (vertices visited more than once).

    When two coplanar regions share only a vertex (not an edge),
    ``_chain_directed_edges`` may produce a single loop that visits the
    shared vertex twice — tracing both regions as one path.  This function
    splits such loops at the first repeated vertex, producing separate
    sub-loops for each region.
    """
    result: List[List[int]] = []
    for loop in loops:
        positions: Dict[int, int] = {}
        split_at = None
        for i, v in enumerate(loop):
            if v in positions:
                split_at = (positions[v], i)
                break
            positions[v] = i
        if split_at is None:
            result.append(loop)
        else:
            first, last = split_at
            inner = loop[first:last + 1]
            outer = loop[last:] + loop[:first + 1]
            if inner[0] == inner[-1] and len(inner) > 1:
                inner = inner[:-1]
            if outer[0] == outer[-1] and len(outer) > 1:
                outer = outer[:-1]
            if len(inner) >= 3:
                result.append(inner)
            if len(outer) >= 3:
                result.extend(_split_pinch_loops([outer]))
    return result


def _snap_group_vertices(welded_verts, tri_indices, welded_tris, snap_tol):
    """Union-find remapping of near-coincident vertices within a planar group.

    Returns ``{old_vertex_index: canonical_vertex_index}``.  Vertices within
    *snap_tol* of each other are merged — this fixes the seam-duplicate
    problem (Bug B residual) where displayMesh emits slightly-offset copies
    of the same corner for adjacent fragments, preventing edge cancellation.

    R-3: O(n) average grid-bucketed spatial hash instead of the former
    O(n^2) all-pairs scan.  Cell size = *snap_tol*, cell key =
    ``(floor(x / snap_tol), floor(y / snap_tol), floor(z / snap_tol))``.
    Two vertices closer than *snap_tol* have per-axis keys differing by at
    most 1 (|a - b| < tol  =>  |floor(a/tol) - floor(b/tol)| <= 1), so they
    always land in the same or an adjacent cell: probing the 27-cell
    neighbourhood finds every in-range pair, and the merge set is identical
    to the all-pairs scan.  The union rule (smaller root absorbs larger)
    keeps each cluster's canonical root equal to its smallest vertex index
    regardless of probe order, so the output remap is unchanged.
    """
    if snap_tol <= 0:
        return {}
    vi_set: set = set()
    for ti in tri_indices:
        a, b, c = welded_tris[ti]
        vi_set.update((a, b, c))
    vi_list = sorted(vi_set)
    n = len(vi_list)
    if n < 2:
        return {}

    coords = np.array([welded_verts[v] for v in vi_list], dtype=np.float64)

    parent = list(range(n))

    def _find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    snap_sq = snap_tol * snap_tol
    cells: Dict[Tuple[int, int, int], List[int]] = defaultdict(list)
    for i in range(n):
        p = coords[i]
        cell = (int(math.floor(p[0] / snap_tol)),
                int(math.floor(p[1] / snap_tol)),
                int(math.floor(p[2] / snap_tol)))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for u in cells.get((cell[0] + dx, cell[1] + dy,
                                        cell[2] + dz), ()):
                        diff = coords[i] - coords[u]
                        if float(diff @ diff) < snap_sq:
                            ri, rj = _find(u), _find(i)
                            if ri != rj:
                                if ri < rj:
                                    parent[rj] = ri
                                else:
                                    parent[ri] = rj
        cells[cell].append(i)

    remap: Dict[int, int] = {}
    for i, v in enumerate(vi_list):
        root = vi_list[_find(i)]
        if root != v:
            remap[v] = root
    return remap


def _split_edges_at_tjunctions(edges, welded_verts, tol):
    """Split directed edges where another vertex lies on the segment.

    For each edge ``(A, B)``, finds vertices *C* in the group that lie on
    segment AB (perpendicular distance < *tol*, projection parameter in
    ``(tol, len-tol)``).  Splits the edge into the chain
    ``A→C1→…→B``, sorted by position along the segment.  This ensures
    that when one fragment has a long boundary edge and the adjacent
    fragment has intermediate vertices on the same line, the edges match
    for proper cancellation.
    """
    vi_set: set = set()
    for a, b in edges:
        vi_set.update((a, b))
    vi_list = sorted(vi_set)
    coords = {v: np.array(welded_verts[v], dtype=np.float64) for v in vi_list}

    result: List[Tuple[int, int]] = []
    for a, b in edges:
        pa = coords[a]
        pb = coords[b]
        seg = pb - pa
        seg_len = float(np.linalg.norm(seg))
        if seg_len < 1e-12:
            continue
        dirv = seg / seg_len
        splits: List[Tuple[float, int]] = []
        for c in vi_list:
            if c == a or c == b:
                continue
            pc = coords[c]
            t = float((pc - pa) @ dirv)
            if tol < t < seg_len - tol:
                proj = pa + dirv * t
                if float(np.linalg.norm(pc - proj)) < tol:
                    splits.append((t, c))
        if not splits:
            result.append((a, b))
        else:
            splits.sort()
            chain = [a] + [c for _, c in splits] + [b]
            for i in range(len(chain) - 1):
                result.append((chain[i], chain[i + 1]))
    return result


def _loop_contains_centroid(loop_2d, centroids_2d):
    """Check if any 2D centroid falls inside the 2D loop polygon."""
    if not centroids_2d or len(loop_2d) < 3:
        return False
    for cx, cy in centroids_2d:
        if _point_in_polygon_2d(cx, cy, loop_2d):
            return True
    return False


def _merge_loop_into_outer(outer_vi, inner_vi, welded_verts):
    """Merge an inner loop's boundary into the outer loop via edge cancellation.

    Both loops are treated as CCW polygon boundaries. Shared edges (where
    the outer has a→b and the inner has b→a) cancel, producing a merged
    boundary that encompasses both regions.  Returns the new outer vertex
    index loop, or the original outer if merging fails.
    """
    def _loop_edges(loop):
        edges = []
        for i in range(len(loop)):
            edges.append((loop[i], loop[(i + 1) % len(loop)]))
        return edges

    outer_edges = _loop_edges(outer_vi)
    inner_edges = _loop_edges(inner_vi)

    edge_counts: Dict[Tuple[int, int], int] = defaultdict(int)
    for e in outer_edges:
        edge_counts[e] += 1
    for e in inner_edges:
        edge_counts[e] += 1

    merged: List[Tuple[int, int]] = []
    seen: set = set()
    for (a, b), fwd in edge_counts.items():
        if (a, b) in seen:
            continue
        rev = edge_counts.get((b, a), 0)
        seen.add((a, b))
        seen.add((b, a))
        net = fwd - rev
        if net > 0:
            merged.extend([(a, b)] * net)
        elif net < 0:
            merged.extend([(b, a)] * (-net))

    new_loops = _split_pinch_loops(_chain_directed_edges(merged))
    if not new_loops:
        return outer_vi
    new_loops.sort(key=len, reverse=True)
    return new_loops[0]


def _boundary_loops(tri_indices, welded_tris, remap=None,
                    tri_normals=None, group_normal=None,
                    welded_verts=None, tjunc_tol=0.0):
    """Boundary vertex-index loops of a triangle set via edge cancellation.

    Interior edges (shared by two triangles with opposite winding) cancel.
    Remaining boundary edges are chained into ordered loops.

    Optional corrections applied in order:
      * *remap* — canonicalise vertex indices (seam-duplicate merge).
      * *welded_verts* + *group_normal* — recompute winding from POST-SNAP
        vertex positions (pre-snap normals are stale for triangles that
        became degenerate after snapping, causing edge-cancellation
        imbalance at seams).
      * *welded_verts* + *tjunc_tol* — split long edges at T-junction
        vertices (where a vertex from one fragment lies on an edge of
        another), ensuring shared boundaries have matching segmentation.
    """
    gn = None
    if group_normal is not None:
        gn = (float(group_normal[0]), float(group_normal[1]),
              float(group_normal[2]))

    raw_edges: List[Tuple[int, int]] = []
    for ti in tri_indices:
        a, b, c = welded_tris[ti]
        if remap:
            a = remap.get(a, a)
            b = remap.get(b, b)
            c = remap.get(c, c)

        if a == b and b == c:
            continue

        if gn is not None and welded_verts is not None:
            pa = welded_verts[a] if not isinstance(welded_verts[a], np.ndarray) \
                else welded_verts[a]
            pb = welded_verts[b] if not isinstance(welded_verts[b], np.ndarray) \
                else welded_verts[b]
            pc = welded_verts[c] if not isinstance(welded_verts[c], np.ndarray) \
                else welded_verts[c]
            ux, uy, uz = (pb[0]-pa[0], pb[1]-pa[1], pb[2]-pa[2])
            vx, vy, vz = (pc[0]-pa[0], pc[1]-pa[1], pc[2]-pa[2])
            nx = uy*vz - uz*vy
            ny = uz*vx - ux*vz
            nz = ux*vy - uy*vx
            dot = nx*gn[0] + ny*gn[1] + nz*gn[2]
            if dot < 0:
                a, b, c = c, b, a
        elif tri_normals is not None and group_normal is not None:
            n = tri_normals[ti]
            dot = float(n[0]*gn[0] + n[1]*gn[1] + n[2]*gn[2])
            if dot < 0:
                a, b, c = c, b, a

        if a != b:
            raw_edges.append((a, b))
        if b != c:
            raw_edges.append((b, c))
        if a != c:
            raw_edges.append((c, a))

    if tjunc_tol > 0 and welded_verts:
        raw_edges = _split_edges_at_tjunctions(
            raw_edges, welded_verts, tjunc_tol)

    edge_counts: Dict[Tuple[int, int], int] = defaultdict(int)
    for e in raw_edges:
        edge_counts[e] += 1

    boundary: List[Tuple[int, int]] = []
    seen: set = set()
    for (a, b), fwd in edge_counts.items():
        if (a, b) in seen:
            continue
        rev = edge_counts.get((b, a), 0)
        seen.add((a, b))
        seen.add((b, a))
        net = fwd - rev
        if net > 0:
            boundary.extend([(a, b)] * net)
        elif net < 0:
            boundary.extend([(b, a)] * (-net))
    return _split_pinch_loops(_chain_directed_edges(boundary))


def _project_to_2d(points_3d, normal, origin=None):
    """Project coplanar 3D points to 2D using the plane *normal*.

    Returns a list of ``(x, y)`` tuples.  When *origin* is given, all loops
    in the same planar group share the same 2D coordinate system (needed for
    containment-based hole classification).
    """
    pts = np.asarray(points_3d, dtype=np.float64)
    n = np.asarray(normal, dtype=np.float64)
    ref = (np.array([1.0, 0.0, 0.0]) if abs(n[0]) <= abs(n[1])
           else np.array([0.0, 1.0, 0.0]))
    u = np.cross(n, ref)
    un = float(np.linalg.norm(u))
    if un < 1e-12:
        ref = np.array([0.0, 0.0, 1.0])
        u = np.cross(n, ref)
        un = float(np.linalg.norm(u))
    u = u / un
    v = np.cross(n, u)
    org = pts[0] if origin is None else np.asarray(origin, dtype=np.float64)
    rel = pts - org
    return [(float(rel[i] @ u), float(rel[i] @ v)) for i in range(len(pts))]


def _shoelace_area_2d(points_2d):
    """Signed area of a 2D polygon (positive = CCW, negative = CW)."""
    m = len(points_2d)
    if m < 3:
        return 0.0
    s = 0.0
    for i in range(m):
        x1, y1 = points_2d[i]
        x2, y2 = points_2d[(i + 1) % m]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _simplify_2d_keep(points_2d, tol):
    """Indices of points to keep after collinear-point collapse.

    A point is dropped when its perpendicular distance from the line through
    its two neighbours is below *tol*.  Always keeps >= 3 points.
    """
    m = len(points_2d)
    if m <= 3:
        return list(range(m))
    keep: List[int] = []
    for i in range(m):
        px, py = points_2d[i]
        ax, ay = points_2d[(i - 1) % m]
        bx, by = points_2d[(i + 1) % m]
        dx, dy = bx - ax, by - ay
        seg = math.hypot(dx, dy)
        if seg < 1e-12:
            keep.append(i)
            continue
        dist = abs(dy * px - dx * py + bx * ay - by * ax) / seg
        if dist > tol:
            keep.append(i)
    return keep if len(keep) >= 3 else list(range(m))


def _point_in_polygon_2d(px, py, poly):
    """Ray-casting point-in-polygon test for a 2D polygon *poly*."""
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > py) != (yj > py)) and \
           (px < (xj - xi) * (py - yi) / (yj - yi + 1e-30) + xi):
            inside = not inside
        j = i
    return inside


def _extract_curved_patches(welded_verts, welded_tris, planar_tri_set,
                            comp_ids, v_arr, f_arr):
    """Classify non-planar triangles into curved surface patches.

    Groups curved triangles by component, builds a trimesh per group, splits
    into connected patches, and classifies each (cylinder / sphere / cone /
    freeform).
    """
    curved_by_comp: Dict[int, List[int]] = defaultdict(list)
    for i in range(len(welded_tris)):
        if i not in planar_tri_set:
            curved_by_comp[comp_ids[i]].append(i)
    if not curved_by_comp:
        return []
    patches: List[Dict] = []
    patch_idx = 0
    for comp in sorted(curved_by_comp):
        tri_list = curved_by_comp[comp]
        try:
            f_sub = f_arr[np.array(tri_list, dtype=np.int64)]
            cm = trimesh.Trimesh(vertices=v_arr, faces=f_sub, process=False)
            parts = cm.split(only_watertight=False)
            if isinstance(parts, trimesh.Trimesh):
                parts = [parts]
        except Exception:
            continue
        for part in parts:
            if len(part.faces) == 0:
                continue
            surface = _classify_curved_patch(part)
            entry = {"component": comp, "patch_index": patch_idx,
                     "triangle_count": len(part.faces),
                     "area": round(float(part.area), 6)}
            entry.update(surface)
            patches.append(entry)
            patch_idx += 1
    return patches


def decompose_mesh_faces(nodes, indices, angle_tolerance_deg=0.5,
                         simplify_vertices=True):
    """Decompose mesh into planar faces and curved patches.

    Returns a dict with ``components_detected``, ``planar_faces``, and
    ``curved_patches``.  Each planar face includes ordered polygon vertices,
    normal, interior angles, area, and optional ``holes`` (list of inner-loop
    point lists).  Curved patches report triangle count, area, and a fitted
    surface classification (cylinder / sphere / cone / freeform).

    Bug fixes (2026-08-04):
      * **Bug A** – weld vertices FIRST (numpy-safe rounded-key dedupe), then
        ``Trimesh(process=False)`` — avoids the trimesh-5 / numpy-2
        ``hashable_rows`` crash that silently returned the empty fallback.
      * **Bug B** – coplanar faces that touch (share a boundary edge segment)
        across disconnected components are MERGED into one face per connected
        2D region, via directed-edge cancellation on the merged triangle set.
      * **Bug C** – degenerate faces (area < 1e-4, < 3 vertices, zero normal)
        are filtered out.
      * **Bug D** – outline simplification runs on the 2D projection
        (collinear-point collapse), not the 3D angle filter that left
        68–295 vertex outlines.  ``angle_tolerance_deg`` is wired through
        to the planar grouping (was dead: ``facet_threshold=None``).
      * **Bug E** – all outline loops are iterated; the largest loop = outer,
        the rest = holes (previously only ``entities[0]`` was kept).
    """
    try:
        node_list = _as_triples(nodes)
        raw_tris = [(int(a), int(b), int(c))
                    for a, b, c in _as_triples(indices)]
        if not raw_tris or not node_list:
            return {"components_detected": 0, "planar_faces": [],
                    "curved_patches": []}

        # --- Bug A: weld FIRST, then process=False ---
        extent = _max_extent_nodes(node_list)
        quant_step = _detect_quantization_step(node_list)
        eps = max(max(1e-9, 1e-7 * extent), 3 * quant_step)
        welded_verts, welded_tris = _weld_vertices(node_list, raw_tris, eps)

        v_arr = np.array(welded_verts, dtype=np.float64)
        f_arr = np.array([[a, b, c] for a, b, c in welded_tris], dtype=np.int64)
        if f_arr.ndim == 1 and len(f_arr) >= 3:
            f_arr = f_arr.reshape(-1, 3)

        # Per-triangle normals & areas (numpy-vectorised)
        v0v = v_arr[f_arr[:, 0]]
        v1v = v_arr[f_arr[:, 1]]
        v2v = v_arr[f_arr[:, 2]]
        crosses = np.cross(v1v - v0v, v2v - v0v)
        cross_norms = np.linalg.norm(crosses, axis=1)
        safe_norms = np.where(cross_norms > 1e-15, cross_norms, 1.0)
        tri_normals = crosses / safe_norms[:, None]

        # Component IDs (for the "component" field)
        comp_ids = _detect_components(welded_tris)
        n_components = (max(comp_ids) + 1) if comp_ids else 0

        # --- Planar grouping (angle_tolerance_deg wired through, Bug D) ---
        planar_groups = _group_planar_triangles(
            v_arr, f_arr, tri_normals, angle_tolerance_deg, extent)

        planar_tri_set: set = set()
        planar_faces: List[Dict] = []
        face_idx = 0
        simp_tol = max(1e-6, 1e-4 * extent)
        snap_tol = max(1e-5, max(5e-4 * extent, 2 * quant_step))

        for g in planar_groups:
            tri_indices = g["tri_indices"]
            for ti in tri_indices:
                planar_tri_set.add(ti)

            normal = g["normal"]

            remap = _snap_group_vertices(
                welded_verts, tri_indices, welded_tris, snap_tol)
            loops_vi = _boundary_loops(
                tri_indices, welded_tris, remap,
                tri_normals=tri_normals, group_normal=normal,
                welded_verts=welded_verts, tjunc_tol=snap_tol)
            if not loops_vi:
                continue

            origin_3d = welded_verts[loops_vi[0][0]]
            loop_info = []
            for loop in loops_vi:
                pts_3d = [welded_verts[vi] for vi in loop]
                pts_2d = _project_to_2d(pts_3d, normal, origin_3d)
                signed = _shoelace_area_2d(pts_2d)
                loop_info.append({"vi": loop, "pts_3d": pts_3d,
                                  "pts_2d": pts_2d, "signed": signed})
            if not loop_info:
                continue

            loop_info.sort(key=lambda d: abs(d["signed"]), reverse=True)

            tri_centroids_2d = []
            for ti in tri_indices:
                a, b, c = welded_tris[ti]
                ra = remap.get(a, a) if remap else a
                rb = remap.get(b, b) if remap else b
                rc = remap.get(c, c) if remap else c
                pa, pb, pc = welded_verts[ra], welded_verts[rb], welded_verts[rc]
                cx3 = (pa[0] + pb[0] + pc[0]) / 3.0
                cy3 = (pa[1] + pb[1] + pc[1]) / 3.0
                cz3 = (pa[2] + pb[2] + pc[2]) / 3.0
                pts_2d = _project_to_2d(
                    [[cx3, cy3, cz3]], normal, origin_3d)
                tri_centroids_2d.append(pts_2d[0])

            faces_in_group: List[Dict] = []
            for li in loop_info:
                if abs(li["signed"]) < _MIN_FACE_AREA:
                    continue
                cx = sum(p[0] for p in li["pts_2d"]) / len(li["pts_2d"])
                cy = sum(p[1] for p in li["pts_2d"]) / len(li["pts_2d"])
                assigned = False
                for fg in faces_in_group:
                    if _point_in_polygon_2d(
                            cx, cy, fg["outer"]["pts_2d"]):
                        if _loop_contains_centroid(
                                li["pts_2d"], tri_centroids_2d):
                            merged_vi = _merge_loop_into_outer(
                                fg["outer"]["vi"], li["vi"], welded_verts)
                            merged_3d = [welded_verts[vi] for vi in merged_vi]
                            merged_2d = _project_to_2d(
                                merged_3d, normal, origin_3d)
                            merged_signed = _shoelace_area_2d(merged_2d)
                            fg["outer"] = {
                                "vi": merged_vi, "pts_3d": merged_3d,
                                "pts_2d": merged_2d,
                                "signed": merged_signed}
                        else:
                            fg["holes"].append(li)
                        assigned = True
                        break
                if not assigned:
                    faces_in_group.append({"outer": li, "holes": []})

            comp = comp_ids[tri_indices[0]] if tri_indices else 0

            for fg in faces_in_group:
                outer = fg["outer"]
                hole_list = fg["holes"]
                total_area = (abs(outer["signed"])
                              - sum(abs(h["signed"]) for h in hole_list))
                if total_area < _MIN_FACE_AREA:
                    continue

                outer_3d = outer["pts_3d"]
                outer_2d = outer["pts_2d"]

                if simplify_vertices:
                    keep = _simplify_2d_keep(outer_2d, simp_tol)
                else:
                    keep = list(range(len(outer_3d)))
                simp_3d = [outer_3d[i] for i in keep]
                if len(simp_3d) < 3:
                    continue

                angles = _compute_polygon_angles_3d(
                    [[float(c) for c in p] for p in simp_3d])

                holes_out: List[List[List[float]]] = []
                for h in hole_list:
                    holes_out.append(
                        [[round(float(c), 6) for c in p]
                         for p in h["pts_3d"]])

                planar_faces.append({
                    "component": comp,
                    "face_index": face_idx,
                    "triangle_count": len(tri_indices),
                    "vertex_count": len(simp_3d),
                    "vertices": [[round(float(c), 6) for c in p]
                                 for p in simp_3d],
                    "normal": [round(float(c), 6) for c in normal],
                    "angles_deg": angles,
                    "area": round(total_area, 6),
                    "holes": holes_out,
                })
                face_idx += 1

        # Curved patches: triangles not in any planar group
        curved_patches = _extract_curved_patches(
            welded_verts, welded_tris, planar_tri_set, comp_ids, v_arr, f_arr)

        return {
            "components_detected": n_components,
            "planar_faces": planar_faces,
            "curved_patches": curved_patches,
        }
    except Exception as e:
        return {
            "components_detected": 0,
            "planar_faces": [],
            "curved_patches": [],
            "error": str(e),
        }


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
            "face_decomposition": {"components_detected": 0, "planar_faces": [],
                                   "curved_patches": []},
        }

    for (i0, i1, i2) in tri_list:
        for ix in (i0, i1, i2):
            if not 0 <= ix < vertex_count:
                raise ValueError(
                    f"triangle index {ix} out of range for {vertex_count} nodes")

    extent = max(max(p[k] for p in node_list) - min(p[k] for p in node_list)
                 for k in range(3))
    quant_step = _detect_quantization_step(node_list)
    node_list, tri_list = _weld_vertices(
        node_list, tri_list,
        max(max(1e-9, 1e-7 * extent), 3 * quant_step))
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

    face_decomp = decompose_mesh_faces(node_list, tri_list)

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
        "face_decomposition": face_decomp,
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
    faced = out.get("face_decomposition") or {}
    for face in faced.get("planar_faces", []):
        if "vertices" in face:
            face["vertices"] = [[round(c * f, 6) for c in v] for v in face["vertices"]]
        if "area" in face:
            face["area"] = round(face["area"] * f * f, 6)
        if "holes" in face:
            face["holes"] = [[[round(c * f, 6) for c in p] for p in hole]
                             for hole in face["holes"]]
    for patch in faced.get("curved_patches", []):
        if "area" in patch:
            patch["area"] = round(patch["area"] * f * f, 6)
        for key in ("radius_cm", "height_cm"):
            if key in patch:
                patch[key] = round(patch[key] * f, 6)
        for key in ("axis_point_cm", "center_cm", "apex_cm"):
            if key in patch:
                patch[key] = [round(c * f, 6) for c in patch[key]]
    return out
