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
from dataclasses import dataclass, replace as _dataclasses_replace
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import trimesh

Float3 = Tuple[float, float, float]


# ==========================================================================
# R-6: Unified tolerance model
# ==========================================================================

@dataclass(frozen=True)
class _ToleranceConfig:
    """Unified tolerance model computed once from (quant_step, extent).

    All values derived per the R-1/R-6 formulas.  ``tjunc_tol`` equals
    ``snap_tol`` by construction.  R-9 presets can override individual
    fields (offset_tol / snap_tol / simp_tol) by constructing an instance
    directly rather than via ``from_()``.
    """
    quant_step: float
    extent: float
    weld_eps: float
    snap_tol: float
    offset_tol: float
    simp_tol: float
    tjunc_tol: float

    @classmethod
    def from_(cls, quant_step: float, extent: float) -> "_ToleranceConfig":
        """Build config from the quantization step and model extent.

        Uses the max-form formulas from R-1 (identical weld_eps, snap_tol).
        """
        weld = max(max(1e-9, 1e-7 * extent), 3 * quant_step)
        snap = max(1e-5, max(5e-4 * extent, 2 * quant_step))
        return cls(
            quant_step=quant_step,
            extent=extent,
            weld_eps=weld,
            snap_tol=snap,
            offset_tol=max(1e-6, 1e-4 * extent),
            simp_tol=max(1e-6, 1e-4 * extent),
            tjunc_tol=snap,
        )


def _smallest_eigenvector_3x3(cov):
    """Closed-form smallest eigenvector of a 3×3 symmetric covariance matrix.

    Derivation — analytic cubic formula + adjugate nullspace:

    1. Characteristic polynomial  λ³ + a₁λ² + a₂λ + a₃ = 0  where
       a₁=−tr(C), a₂=sum of principal 2×2 minors, a₃=−det(C).
    2. Depress: let  λ = t − a₁/3  →  t³ + pt + q = 0.
    3. For a *symmetric* matrix all eigenvalues are real → p ≤ 0,
       three real roots via the trigonometric solution:
         r = √(−p/3),  φ = ⅓·acos(−q/(2r³)),
         t₀,₁,₂ = 2r·cos(φ + 2πk/3)  (k=0,1,2).
    4. Smallest eigenvalue  λ_min = min(tₖ) − a₁/3.
    5. Eigenvector = any nonzero row of  adj(C − λ_min·I).
       Use row-0 cofactors; fall back to rows 1, 2 if zero.

    O(1), no numpy linear algebra.  Returns a **unit-length**
    ``np.ndarray(3, dtype=np.float64)``.  Degenerate covariances
    (all points coincident) return the Z-axis unit vector.
    """
    # ── unpack symmetric 3×3 elements (row-major) ──
    a, b, c = float(cov[0, 0]), float(cov[0, 1]), float(cov[0, 2])
    d, e, f = float(cov[1, 1]), float(cov[1, 2]), float(cov[2, 2])
    # (cov[1,0]=b, cov[2,0]=c, cov[2,1]=e — symmetry assumed)

    # ── characteristic polynomial coefficients ──
    a1 = -(a + d + f)
    a2 = (a * d - b * b) + (a * f - c * c) + (d * f - e * e)
    a3 = -(a * (d * f - e * e) - b * (b * f - c * e) + c * (b * e - c * d))

    # ── depressed cubic: t³ + p·t + q = 0 ──
    p = a2 - a1 * a1 / 3.0
    q = 2.0 * a1 * a1 * a1 / 27.0 - a1 * a2 / 3.0 + a3

    eps_c = 1e-15
    if abs(p) < eps_c:
        # p ≈ 0 → t³ = −q → three equal real roots
        t_root = -math.copysign(abs(q) ** (1.0 / 3.0), q) if abs(q) > eps_c else 0.0
        lam = t_root - a1 / 3.0
        eigs = [lam, lam, lam]
    else:
        r = math.sqrt(-p / 3.0)
        arg = -q / (2.0 * r * r * r)
        arg = max(-1.0, min(1.0, arg))            # clamp for float safety
        theta = math.acos(arg) / 3.0
        pi23 = 2.0 * math.pi / 3.0
        eigs = [2.0 * r * math.cos(theta + k * pi23) - a1 / 3.0
                for k in (0, 1, 2)]

    lam_min = min(eigs)

    # ── eigenvector via adjugate row cofactors ──
    m00, m01, m02 = a - lam_min, b, c
    m10, m11, m12 = b, d - lam_min, e
    m20, m21, m22 = c, e, f - lam_min

    z = np.array([0.0, 0.0, 1.0], dtype=np.float64)

    def _cofactor_row(r):
        # Row-0,1,2 cofactor triples of M = C − λ_min·I
        if r == 0:
            return (m11 * m22 - m12 * m21,
                    m12 * m20 - m10 * m22,
                    m10 * m21 - m11 * m20)
        if r == 1:
            return (m02 * m21 - m01 * m22,
                    m00 * m22 - m02 * m20,
                    m01 * m20 - m00 * m21)
        return (m01 * m12 - m02 * m11,
                m02 * m10 - m00 * m12,
                m00 * m11 - m01 * m10)

    for row in (0, 1, 2):
        vx, vy, vz = _cofactor_row(row)
        vn = vx * vx + vy * vy + vz * vz
        if vn > 1e-20:
            inv = 1.0 / math.sqrt(vn)
            return np.array([vx * inv, vy * inv, vz * inv], dtype=np.float64)

    return z  # fully degenerate covariance

_CLUSTER_ANGLE_DEG = 15.0      # normal clustering angular threshold
_CYL_SIDE_ANGLE_DEG = 85.0     # normals within 5 deg of perpendicular = "side"
_CYL_SIDE_FRACTION_MIN = 0.5   # min fraction of side normals for a cylinder
_CYL_COVERAGE_MIN = 0.85       # min azimuthal bucket coverage for a cylinder
_CYL_RADIUS_CV_MAX = 0.20      # max relative std of side-face centroid radius
_ORTHO_DOT_MAX = 0.05          # |dot| below this => directions are orthogonal
_PARALLEL_DOT_MIN = 0.95       # |dot| above this => directions are parallel
_BOX_FITTED_CONF = 0.40        # box "fitted" when >= this fraction matches


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

    cluster_extent = float(np.max(verts.max(axis=0) - verts.min(axis=0)))
    if cluster_extent > 1e-9 and radius > 100.0 * cluster_extent:
        return None

    # Confidence: low coefficient of variation ⇒ high confidence.
    mean_r = float(np.mean(dists))
    if mean_r < 1e-9:
        return None
    cv = float(np.std(dists)) / mean_r
    confidence = max(0.0, 1.0 - cv * 5.0)
    if confidence < 0.3:
        return None

    # Normal-radial alignment gate (validated: median |cos| < 0.85 rejects).
    radial = centroids - center
    radial_norm = np.linalg.norm(radial, axis=1)
    valid_r = radial_norm > 1e-9
    if np.any(valid_r):
        radial_unit = radial[valid_r] / radial_norm[valid_r, np.newaxis]
        n = min(len(normals), len(radial_unit))
        if n > 0:
            nm = np.linalg.norm(normals[:n], axis=1)
            ok = nm > 1e-12
            if np.any(ok):
                dots = np.abs(np.sum(
                    normals[:n][ok] / nm[ok, np.newaxis]
                    * radial_unit[:n][ok], axis=1))
                if len(dots) > 0:
                    median_align = float(np.median(dots))
                    if median_align < 0.85:
                        return None
                    confidence *= median_align
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


def _fit_torus(nodes_array, face_indices, face_normals):
    """Deterministic torus fit for a curved patch.

    Strategy:
      1. Torus centre: vertex mean (torus is point-symmetric about centre).
      2. Axis: smallest-eigenvector of vertex covariance.  For a ring
         torus (R > r), spread along axis = 2r while spread perpendicular
         = 2(R+r), so the axis is unambiguously the least-spread dir.
      3. For each vertex: rho = radial distance from axis, z = signed
         height along axis from the equatorial plane.
      4. Torus constraint: (rho-R)^2 + z^2 = r^2, linearised to
         rho^2 + z^2 = 2R*rho + (r^2-R^2).  Least-squares gives R, r.
      5. Confidence: inverse of the geometric residual coefficient of
         variation.

    Returns dict with surface_type, axis, centre_cm, major_radius_cm,
    minor_radius_cm, confidence; or None if confidence < 0.3.
    """
    verts = np.asarray(nodes_array, dtype=np.float64)
    if len(verts) < 16:
        return None

    center = verts.mean(axis=0)
    centered = verts - center
    cov = centered.T @ centered
    eigvals, eigvecs = np.linalg.eigh(cov)
    axis = eigvecs[:, 0]
    anorm = np.linalg.norm(axis)
    if anorm < 1e-12:
        return None
    axis = axis / anorm

    projections = centered @ axis
    perp = centered - np.outer(projections, axis)
    rho = np.linalg.norm(perp, axis=1)

    if np.min(rho) < 1e-9:
        return None

    z = projections
    x = rho
    y = rho ** 2 + z ** 2
    A = np.column_stack([x, np.ones_like(x)])
    try:
        coeffs, _, _, _ = np.linalg.lstsq(A, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    slope = float(coeffs[0])
    intercept = float(coeffs[1])
    R = slope / 2.0
    r_sq = intercept + R * R
    if R < 1e-9 or r_sq < 1e-12:
        return None
    r = math.sqrt(r_sq)
    if r < 1e-9 or r >= R:
        return None

    tube_dist = np.sqrt((rho - R) ** 2 + z ** 2)
    geom_err = np.abs(tube_dist - r)
    std_err = float(np.std(geom_err))
    cv = std_err / r
    confidence = max(0.0, 1.0 - cv * 5.0)
    if confidence < 0.3:
        return None

    return {
        "surface_type": "torus",
        "axis": [round(float(c), 6) for c in axis],
        "center_cm": [round(float(c), 6) for c in center],
        "major_radius_cm": round(R, 6),
        "minor_radius_cm": round(r, 6),
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


def _cylinder_fit_residual(verts, faces, fit):
    """Mean radial residual of a cylinder fit on (verts, faces).

    For each face vertex: |distance_to_axis_line − radius|, averaged.
    """
    axis = np.array(fit["axis"], dtype=np.float64)
    axis_point = np.array(fit["axis_point_cm"], dtype=np.float64)
    radius = float(fit["radius_cm"])
    face_verts = verts[faces]
    rel = face_verts - axis_point
    cross_dists = np.linalg.norm(np.cross(rel, axis), axis=2)
    return float(np.mean(np.abs(cross_dists - radius)))


def _sphere_fit_residual(verts, fit):
    """Mean vertex distance residual of a sphere fit."""
    center = np.array(fit["center_cm"], dtype=np.float64)
    radius = float(fit["radius_cm"])
    dists = np.linalg.norm(verts - center, axis=1)
    return float(np.mean(np.abs(dists - radius)))


def _cone_fit_residual(verts, faces, fit):
    """Mean surface residual of a cone fit."""
    apex = np.array(fit["apex_cm"], dtype=np.float64)
    axis = np.array(fit["axis"], dtype=np.float64)
    half_angle = math.radians(float(fit["half_angle_deg"]))
    cos_a = math.cos(half_angle)
    sin_a = math.sin(half_angle)
    face_verts = verts[faces]
    rel = face_verts - apex
    h = rel @ axis
    perp = rel - h[:, :, None] * axis
    rho = np.linalg.norm(perp, axis=2)
    surf_err = np.abs(rho * cos_a - np.abs(h) * sin_a)
    return float(np.mean(surf_err))


def _torus_fit_residual(verts, faces, fit):
    """Mean tube-distance residual of a torus fit on (verts, faces)."""
    center = np.array(fit["center_cm"], dtype=np.float64)
    axis = np.array(fit["axis"], dtype=np.float64)
    R = float(fit["major_radius_cm"])
    r = float(fit["minor_radius_cm"])
    face_verts = verts[faces]
    rel = face_verts - center
    proj = rel @ axis
    perp = rel - proj[:, :, None] * axis
    rho = np.linalg.norm(perp, axis=2)
    tube_dist = np.sqrt((rho - R) ** 2 + proj ** 2)
    return float(np.mean(np.abs(tube_dist - r)))


def _classify_curved_patch(patch, epsilon=None):
    """Classify a single curved-patch trimesh and return the surface dict.

    When *epsilon* is provided, runs the full 5-model competition
    (cylinder, sphere, cone, torus): each model is fit independently,
    candidates with mean geometric residual <= epsilon are collected,
    and the winner is chosen by validated preference rules:

      1. Cone preference (conf >= 0.5): a high-confidence cone fit is
         structurally a cone — the median-angle gate ensures half-angle
         >= 10 deg, which cylinders (half-angle 0) cannot produce.
      2. Cylinder preference (conf >= 0.5): the normal-axis perpendicularity
         structural test beats algebraic sphere overfitting on short
         cylinders (h ~ 2r).
      3. Lowest residual among remaining candidates.

    Patches that win no model (all fits fail or exceed epsilon) remain
    ``surface_type: freeform``.

    When *epsilon* is None, the original sequential first-fit-wins logic
    (cylinder -> sphere -> cone -> freeform) is preserved for backward
    compatibility.
    """
    verts = np.asarray(patch.vertices, dtype=np.float64)
    faces = np.asarray(patch.faces, dtype=np.int64)
    normals = np.asarray(patch.face_normals, dtype=np.float64)

    if epsilon is None:
        surface = _fit_cylinder(verts, faces, normals)
        if surface is not None:
            return surface
        surface = _fit_sphere(verts, faces, normals)
        if surface is not None:
            return surface
        surface = _fit_cone(verts, faces, normals)
        if surface is not None:
            return surface
        return {
            "surface_type": "freeform",
            "mean_curvature": _estimate_freeform_curvature(patch),
        }

    candidates = []

    cyl_fit = _fit_cylinder(verts, faces, normals)
    if cyl_fit is not None:
        cyl_res = _cylinder_fit_residual(verts, faces, cyl_fit)
        if cyl_res <= epsilon:
            candidates.append(("cylinder", cyl_fit, cyl_res))

    sph_fit = _fit_sphere(verts, faces, normals)
    if sph_fit is not None:
        sph_res = _sphere_fit_residual(verts, sph_fit)
        if sph_res <= epsilon:
            candidates.append(("sphere", sph_fit, sph_res))

    cone_fit = _fit_cone(verts, faces, normals)
    if cone_fit is not None:
        cone_res = _cone_fit_residual(verts, faces, cone_fit)
        if cone_res <= epsilon:
            candidates.append(("cone", cone_fit, cone_res))

    tor_fit = _fit_torus(verts, faces, normals)
    if tor_fit is not None:
        tor_res = _torus_fit_residual(verts, faces, tor_fit)
        if tor_res <= epsilon:
            candidates.append(("torus", tor_fit, tor_res))

    if not candidates:
        return {
            "surface_type": "freeform",
            "mean_curvature": _estimate_freeform_curvature(patch),
        }

    cone_entry = next((c for c in candidates if c[0] == "cone"), None)
    if cone_entry is not None and cone_entry[1].get("confidence", 0) >= 0.5:
        result = dict(cone_entry[1])
        result["residual_cm"] = round(cone_entry[2], 6)
        return result

    cyl_entry = next((c for c in candidates if c[0] == "cylinder"), None)
    if cyl_entry is not None and cyl_entry[1].get("confidence", 0) >= 0.5:
        result = dict(cyl_entry[1])
        result["residual_cm"] = round(cyl_entry[2], 6)
        return result

    best = min(candidates, key=lambda c: c[2])
    result = dict(best[1])
    result["residual_cm"] = round(best[2], 6)
    return result


# --------------------------------------------------------------------------
# universal delta estimator + cylinder recovery (fix-e, 2026-08-12)
# --------------------------------------------------------------------------

_SMALL_GROUP_MAX = 30   # planar groups with <= this many triangles are
                        # candidates for cylinder-wall recovery

# --- extruded-wall sweep recovery (2026-08-19) ------------------------------
_MAX_CHAIN_ANGLE_DEG = 30.0   # small-group chaining gate: adjacent groups
                              # union into a cluster only when their mean
                              # normals agree within 30 deg — kills the
                              # sliver-bridged mega-clusters that chained
                              # unrelated wall bands through degenerate
                              # seam triangles
_SWEEP_AXIS_PERP_DOT = 0.15   # |n . axis| < this => normal is perpendicular
                              # to the extrusion axis (side-wall normal)
_SWEEP_AXIS_MIN_FRACTION = 0.8   # >= 80% of cluster normals perpendicular
                                 # to a candidate axis => extrusion band
_SWEEP_STEP_MIN_DEG = 0.5      # per-step normal turn below this = straight
                               # (constant-direction) run — stop there
_SWEEP_STEP_MAX_DEG = 45.0     # per-step normal turn above this = sharp
                               # junction — stop there
_SWEEP_MIN_TOTAL_DEG = 10.0    # a run must sweep >= this many degrees of
                               # normal rotation to be an arc worth fitting

# --- axis-partitioned chaining (2026-08-19) ---------------------------------
_CANON_AXES: Tuple[np.ndarray, np.ndarray, np.ndarray] = (
    np.array([1.0, 0.0, 0.0]),
    np.array([0.0, 1.0, 0.0]),
    np.array([0.0, 0.0, 1.0]),
)
_CHAIN_AXIS_PERP_DOT = 0.15   # |mean_normal . axis| < this => the group's
                              # normals are perpendicular to that axis
                              # (i.e. the group is a wall band extruded
                              # along it).  Two groups in different axis
                              # partitions never chain, even when their
                              # normals agree within 30° — preventing
                              # fillet bands (⊥ X/Y) from chaining into
                              # wall bands (⊥ Z) through smooth junctions.

# --- extrusion-band recovery, phase 2: spline profiles (2026-08-20) ---------
_BAND_EDGE_PARALLEL_DOT = 0.95   # shared mesh edge |d_norm . axis| above this
                                 # => the edge is parallel to the extrusion
                                 # axis => band-adjacent (chains corners AND
                                 # smooth junctions; never chains into caps,
                                 # whose shared edges are perpendicular)
_BAND_OUT_OF_PLANE_MAX = 0.02    # lambda_min/trace of the normal scatter
                                 # matrix at or above this => the normals
                                 # have systematic out-of-plane tilt (taper
                                 # or helix lead) — not a prismatic band
_BAND_TWIST_CORR_MAX = 0.8       # correlation between projected-normal
                                 # azimuth and axial coordinate above this
                                 # (with axially short strips) = twist
_BAND_TWIST_SPAN_FRAC = 0.6      # every group spanning less than this
                                 # fraction of the band's axial extent is
                                 # the "short strips" half of the twist test
_SPLINE_OVERFIT_RATIO = 0.5      # distinct control points at or above this
                                 # ratio of profile points => the profile is
                                 # not smooth — reject (overfit guard)
_BAND_CORNER_DEG = 60.0          # projected-normal turn between consecutive
                                 # band groups above this = profile corner —
                                 # pinned as a reduced-continuity knot (60
                                 # deg keeps fillet junctions at 45 deg smooth
                                 # while pinning real 90 deg profile corners)


def _otsu_threshold(log_values, n_bins=128):
    """Otsu's method: threshold maximizing between-class variance.

    Returns the bin-edge value that best separates the log10-residual
    histogram into two classes (flat noise mode vs curved tessellation
    mode).  Deterministic: pure numpy histogram + cumsum.
    """
    lo, hi = float(np.min(log_values)), float(np.max(log_values))
    if hi - lo < 0.1:
        return float(np.median(log_values))

    hist, edges = np.histogram(log_values, bins=n_bins, range=(lo, hi))
    total = len(log_values)
    prob = hist / total
    bin_centers = (edges[:-1] + edges[1:]) / 2

    cumsum_w = np.cumsum(prob)
    cumsum_m = np.cumsum(prob * bin_centers)
    total_mean = cumsum_m[-1]

    max_var = -1.0
    best_t = 0
    for t in range(1, n_bins):
        w0 = cumsum_w[t - 1]
        w1 = 1.0 - w0
        if w0 < 1e-10 or w1 < 1e-10:
            continue
        m0 = cumsum_m[t - 1] / w0
        m1 = (total_mean - cumsum_m[t - 1]) / w1
        var_between = w0 * w1 * (m0 - m1) ** 2
        if var_between > max_var:
            max_var = var_between
            best_t = t

    return float(edges[best_t])


def _estimate_delta(v_arr, f_arr, tri_normals, quant_step, extent,
                    normal_filter_deg=45.0, max_tris=5000):
    """Universal delta estimator: per-triangle 1-ring plane-fit residual +
    Otsu gap detection on log10-histogram.

    Estimates the mesh's own tessellation error (chordal deviation between
    the tessellated surface and the underlying smooth surface) without
    knowing the surface type.  Works by measuring how non-planar each
    triangle's 1-ring neighborhood is: on a planar face the residual is
    machine-zero, on a tessellated curved surface it is the sagitta
    (chordal error) of the tessellation.

    Algorithm:
      1. For each sampled triangle, collect its edge-adjacent 1-ring.
      2. Filter the ring by normal consistency (|dot| >= cos(45°)) to
         exclude sharp dihedral edges (e.g. the seam between a cylinder
         wall and its cap).
      3. Fit a plane (PCA smallest eigenvector) to the filtered ring's
         vertices; residual = mean abs vertex-to-plane distance.
      4. Histogram on log10(residual).  Otsu threshold splits the flat-
         noise mode from the curved-tessellation mode.
      5. delta_est = median of residuals ABOVE the Otsu threshold (the
         curved mode's center, not the split point).
      6. eps = clamp(3*delta_est, floor, ceiling) where
         floor = min(max(10*quant_step, 1e-6), 0.1*ceiling) and
         ceiling = max(1e-3*extent, 4*delta_est).

    On planar-only meshes all residuals are machine-zero → delta_est ≈ 0
    → eps stays at floor → no false curved patches.

    Returns dict with keys: delta_est, epsilon, floor, ceiling,
    quant_step, extent, n_residuals, n_curved.
    """
    n_tris = len(f_arr)
    if n_tris == 0:
        return {"delta_est": 1e-15, "epsilon": max(1e-6, 10 * quant_step),
                "floor": max(1e-6, 10 * quant_step),
                "ceiling": max(1e-3 * extent, 4e-15),
                "quant_step": quant_step, "extent": extent,
                "n_residuals": 0, "n_curved": 0}

    # --- edge-to-face adjacency ---
    edge_faces: Dict[Tuple[int, int], set] = defaultdict(set)
    for ti in range(n_tris):
        a, b, c = int(f_arr[ti, 0]), int(f_arr[ti, 1]), int(f_arr[ti, 2])
        for e in ((min(a, b), max(a, b)),
                  (min(b, c), max(b, c)),
                  (min(a, c), max(a, c))):
            if e[0] != e[1]:
                edge_faces[e].add(ti)

    stride = max(1, n_tris // max_tris)
    sample_indices = list(range(0, n_tris, stride))
    cos_threshold = math.cos(math.radians(normal_filter_deg))

    residuals: List[float] = []
    for ti in sample_indices:
        # 1-ring via shared edges
        ring: set = set()
        a, b, c = int(f_arr[ti, 0]), int(f_arr[ti, 1]), int(f_arr[ti, 2])
        for e in ((min(a, b), max(a, b)),
                  (min(b, c), max(b, c)),
                  (min(a, c), max(a, c))):
            ring.update(edge_faces.get(e, ()))

        # Filter by normal consistency
        cn = tri_normals[ti]
        filtered = [tj for tj in ring
                    if abs(float(np.dot(tri_normals[tj], cn))) >= cos_threshold]

        if len(filtered) < 3:
            continue

        # Collect all vertices in the filtered ring
        verts = v_arr[f_arr[filtered].ravel()]
        if len(verts) < 4:
            continue

        # PCA plane fit (smallest eigenvector = normal)
        center = verts.mean(axis=0)
        centered = verts - center
        cov = centered.T @ centered
        eigvals, eigvecs = np.linalg.eigh(cov)
        normal = eigvecs[:, 0]

        # Residual = mean abs distance to fitted plane
        dists = np.abs((verts - center) @ normal)
        res = float(np.mean(dists))
        if res < 1e-15:
            res = 1e-15
        residuals.append(res)

    if not residuals:
        delta_est = 1e-15
    else:
        residuals_arr = np.array(residuals)
        log_res = np.log10(residuals_arr)
        threshold = _otsu_threshold(log_res)

        curved_mask = log_res > threshold
        n_curved = int(np.sum(curved_mask))
        if n_curved >= max(3, len(residuals) * 0.02):
            delta_est = float(np.median(residuals_arr[curved_mask]))
        else:
            delta_est = 10.0 ** threshold

    # eps = clamp(3*delta_est, floor, ceiling)
    ceiling = max(1e-3 * extent, 4.0 * delta_est)
    raw_floor = max(10.0 * quant_step, 1e-6) if quant_step > 0 else 1e-6
    floor = min(raw_floor, 0.1 * ceiling)
    epsilon = max(floor, min(3.0 * delta_est, ceiling))

    return {
        "delta_est": delta_est,
        "epsilon": epsilon,
        "floor": floor,
        "ceiling": ceiling,
        "quant_step": quant_step,
        "extent": extent,
        "n_residuals": len(residuals),
        "n_curved": int(np.sum(np.log10(np.array(residuals)) > threshold))
                    if residuals else 0,
    }


def _cluster_tri_area(v_arr, f_arr, tri_indices):
    """Total area of a set of triangles."""
    total = 0.0
    for ti in tri_indices:
        a = v_arr[f_arr[ti, 0]]
        b = v_arr[f_arr[ti, 1]]
        c = v_arr[f_arr[ti, 2]]
        total += 0.5 * float(np.linalg.norm(np.cross(b - a, c - a)))
    return total


def _cylinder_residual_on_tris(v_arr, f_arr, tri_normals, tri_indices):
    """Cylinder fit + mean radial residual for a set of triangle indices.

    Remaps to local vertex indices, calls ``_fit_cylinder``, then computes
    the mean absolute radial error of all face vertices.

    Returns ``(fit_dict, residual)`` or ``(None, inf)`` on failure.
    """
    ti_arr = np.asarray(tri_indices, dtype=np.int64)
    if len(ti_arr) < 6:
        return None, float("inf")
    faces_sub = f_arr[ti_arr]
    normals_sub = tri_normals[ti_arr]
    unique_idx, inverse = np.unique(faces_sub.ravel(), return_inverse=True)
    local_verts = v_arr[unique_idx]
    local_faces = inverse.reshape(-1, 3)
    fit = _fit_cylinder(local_verts, local_faces, normals_sub)
    if fit is None:
        return None, float("inf")
    axis = np.array(fit["axis"], dtype=np.float64)
    axis_point = np.array(fit["axis_point_cm"], dtype=np.float64)
    radius = float(fit["radius_cm"])
    face_verts = local_verts[local_faces]
    rel = face_verts - axis_point
    cross_dists = np.linalg.norm(np.cross(rel, axis), axis=2)
    radial_err = np.abs(cross_dists - radius)
    return fit, float(np.mean(radial_err))


def _group_mean_normals(planar_groups, tri_normals):
    """Area-free unit mean normal per planar group (None when degenerate).

    The mean of a group's per-triangle normals, normalized.  Groups whose
    normals cancel to near-zero length (numerically degenerate slivers)
    return None — callers treat that as "fails any angular gate".
    """
    out: List[Optional[np.ndarray]] = []
    for g in planar_groups:
        tris = np.asarray(g["tri_indices"], dtype=np.int64)
        if len(tris) == 0:
            out.append(None)
            continue
        mean_n = tri_normals[tris].mean(axis=0)
        norm = float(np.linalg.norm(mean_n))
        out.append(mean_n / norm if norm > 1e-12 else None)
    return out


def _group_axis_partition(mean_normal):
    """Index (0=X, 1=Y, 2=Z) of the canonical axis most perpendicular to
    *mean_normal*, or -1 when no axis is clearly perpendicular.

    Groups in different partitions never chain together — this stops
    fillet bands (⊥ X/Y) from merging into wall bands (⊥ Z) through
    smooth (< 30°) normal transitions at fillet-wall junctions.
    """
    if mean_normal is None:
        return -1
    dots = (abs(float(mean_normal @ _CANON_AXES[0])),
            abs(float(mean_normal @ _CANON_AXES[1])),
            abs(float(mean_normal @ _CANON_AXES[2])))
    best = min(range(3), key=lambda i: dots[i])
    if dots[best] < _CHAIN_AXIS_PERP_DOT:
        return best
    return -1


def _find_extrusion_axis(tri_normals, tri_indices):
    """Authoritative extrusion axis for a triangle set, or None.

    A band extruded along axis ``a`` has (nearly) all face normals
    perpendicular to ``a``.  Candidate axes are the three canonical
    world axes plus the smallest eigenvector of the normal scatter
    matrix ``sum(n n^T)`` — the least-squares plane through the
    offsetted strip normals, which sits "in between" their alternating
    deviations.  Canonical axes are listed first so they win ties
    (degenerate near-flat bands get arbitrary PCA directions).  Every
    candidate is scored by its perpendicular fraction; the highest wins
    and is returned only when at least ``_SWEEP_AXIS_MIN_FRACTION``.
    A separate out-of-plane gate rejects triangle sets whose smallest
    scatter eigenvalue holds at or above ``_BAND_OUT_OF_PLANE_MAX`` of
    the trace — systematic normal tilt out of the perpendicular plane
    (taper, helix lead) is not a prismatic band.
    """
    normals = tri_normals[np.asarray(tri_indices, dtype=np.int64)]
    if len(normals) < 4:
        return None

    scatter = normals.T @ normals
    eigvals, eigvecs = np.linalg.eigh(scatter)
    trace = float(eigvals.sum())
    if trace <= 1e-18:
        return None
    if float(eigvals[0] / trace) >= _BAND_OUT_OF_PLANE_MAX:
        return None
    pca = np.array(eigvecs[:, 0], dtype=np.float64)
    big = int(np.argmax(np.abs(pca)))
    if pca[big] < 0.0:
        pca = -pca
    pca = pca / float(np.linalg.norm(pca))

    candidates: List[np.ndarray] = []
    for c in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)):
        candidates.append(np.array(c, dtype=np.float64))
    candidates.append(pca)

    best_axis = None
    best_frac = 0.0
    for cand in candidates:
        frac = float(np.count_nonzero(
            np.abs(normals @ cand) < _SWEEP_AXIS_PERP_DOT) / len(normals))
        if frac > best_frac:
            best_frac = frac
            best_axis = cand
    if best_axis is None or best_frac < _SWEEP_AXIS_MIN_FRACTION:
        return None
    return best_axis


def _grow_sweep_runs(group_ids, group_mean_normals, group_adj, axis):
    """Monotonic arc runs over a cluster's planar groups (deterministic).

    Region-grows runs of groups whose mean normals rotate monotonically
    around *axis* — the tessellation rings of an extruded circular-arc
    wall form exactly such a chain.  Seeds are visited in ascending group
    index; neighbors grow in ascending index order.  Each seed grows
    BIDIRECTIONALLY: a forward pass (append, direction = sign of its
    first step) and a backward pass (prepend, direction = sign of its
    first step) extend the run from both sides of the seed, so a single
    monotonic chain is covered by one run regardless of where the seed
    sits along it.

    A pass extends from its current group *g_cur* to an adjacent
    unvisited group *g_next* only when the projected normal angle step
    ``theta(g_next) - theta(g_cur)``:

      * keeps the pass's rotation direction (sign of its first step),
      * has magnitude in ``[_SWEEP_STEP_MIN_DEG, _SWEEP_STEP_MAX_DEG]``
        — steps below the minimum are a straight (constant-normal) edge,
        steps above the maximum are a sharp junction; both stop the pass.

    Returns a list of ``(run_groups, total_sweep_deg)`` for every run
    with at least 2 groups; total sweep is the accumulated |delta
    theta| of both passes' accepted steps.
    """
    thetas = np.full(len(group_mean_normals), np.nan, dtype=np.float64)
    ax_abs = np.abs(axis)
    dominant = int(np.argmax(ax_abs))
    if dominant == 0:
        c1, c2 = 1, 2
    elif dominant == 1:
        c1, c2 = 0, 2
    else:
        c1, c2 = 0, 1
    for gi in group_ids:
        mn = group_mean_normals[gi]
        if mn is None:
            continue
        perp = mn - float(mn @ axis) * axis
        if float(np.hypot(perp[c1], perp[c2])) > 1e-9:
            thetas[gi] = math.degrees(math.atan2(perp[c2], perp[c1]))

    def _step(a, b):
        return (thetas[b] - thetas[a] + 180.0) % 360.0 - 180.0

    def _pass(start, visited, cluster):
        """Greedy one-direction path from *start*; returns (groups, sweep)."""
        out: List[int] = []
        sweep = 0.0
        direction = 0.0
        cur = start
        while True:
            nxt = None
            for cand in sorted(group_adj.get(cur, ())):
                if cand not in cluster or cand in visited \
                        or np.isnan(thetas[cand]):
                    continue
                step = _step(cur, cand)
                if abs(step) < _SWEEP_STEP_MIN_DEG \
                        or abs(step) > _SWEEP_STEP_MAX_DEG:
                    continue
                if direction == 0.0 \
                        or math.copysign(1.0, step) == direction:
                    nxt = cand
                    if direction == 0.0:
                        direction = math.copysign(1.0, step)
                    break
            if nxt is None:
                break
            step = _step(cur, nxt)
            visited.add(nxt)
            out.append(nxt)
            sweep += abs(step)
            cur = nxt
        return out, sweep

    visited: set = set()
    cluster = set(group_ids)
    runs: List[Tuple[List[int], float]] = []
    for seed in sorted(group_ids):
        if seed in visited:
            continue
        visited.add(seed)
        forward, sweep_f = _pass(seed, visited, cluster)
        backward, sweep_b = _pass(seed, visited, cluster)
        run = list(reversed(backward)) + [seed] + forward
        if len(run) >= 2:
            runs.append((run, sweep_f + sweep_b))
    return runs


def _fit_sweep_cylinder(v_arr, f_arr, tri_normals, tri_indices, axis,
                        epsilon):
    """Cylinder fit for a sweep run against a KNOWN extrusion axis.

    ``_fit_cylinder`` places its axis line through the patch vertex mean,
    which is chord-biased on open (< 360 deg) arcs and inflates the radial
    residual far above epsilon — an arc of a true r=2.0 cylinder fits as
    r=0.77 with residual 0.33.  A sweep run already knows the extrusion
    axis, so only the axis line's perpendicular position is unknown: a
    Kasa algebraic circle fit (linear least squares on
    ``x^2 + y^2 = 2*cx*x + 2*cy*y + c``) on the run's compacted vertices
    projected to the axis-perpendicular plane recovers the exact center
    for vertices lying on a circular arc.  Tessellated cylinders put
    their vertices exactly ON the surface, so the mean radial residual of
    a true arc run is machine-zero and the epsilon gate stays honest.

    Returns ``(fit_dict, residual)`` with the standard cylinder schema
    (surface_type, axis, radius_cm, axis_point_cm, height_cm,
    confidence, residual_cm), or ``(None, inf)`` when the run is not a
    cylinder within epsilon.
    """
    ti_arr = np.asarray(tri_indices, dtype=np.int64)
    if len(ti_arr) < 6:
        return None, float("inf")

    faces_sub = f_arr[ti_arr]
    unique_idx, inverse = np.unique(faces_sub.ravel(), return_inverse=True)
    local_verts = v_arr[unique_idx]
    local_faces = inverse.reshape(-1, 3)

    ref = (np.array([1.0, 0.0, 0.0]) if abs(float(axis[0])) < 0.9
           else np.array([0.0, 1.0, 0.0]))
    u = np.cross(axis, ref)
    u = u / np.linalg.norm(u)
    w = np.cross(axis, u)

    rel = local_verts - local_verts.mean(axis=0)
    x = rel @ u
    y = rel @ w
    A = np.column_stack([2.0 * x, 2.0 * y, np.ones_like(x)])
    b = x * x + y * y
    try:
        coeffs, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
    except np.linalg.LinAlgError:
        return None, float("inf")
    cx, cy, c = float(coeffs[0]), float(coeffs[1]), float(coeffs[2])
    r_sq = cx * cx + cy * cy + c
    if r_sq <= 1e-12:
        return None, float("inf")
    axis_point = local_verts.mean(axis=0) + cx * u + cy * w

    face_verts = local_verts[local_faces]
    cross_dists = np.linalg.norm(
        np.cross(face_verts - axis_point, axis), axis=2)
    radius = float(np.median(cross_dists))
    if radius < 1e-9:
        return None, float("inf")
    residual = float(np.mean(np.abs(cross_dists - radius)))
    if residual > epsilon:
        return None, float("inf")

    normals_sub = tri_normals[ti_arr]
    perp = float(np.count_nonzero(
        np.abs(normals_sub @ axis) < _SWEEP_AXIS_PERP_DOT)
        / len(normals_sub))
    if perp < 0.3:
        return None, float("inf")

    projs = local_verts @ axis
    height = float(projs.max() - projs.min())

    fit = {
        "surface_type": "cylinder",
        "axis": [round(float(cc), 6) for cc in axis],
        "radius_cm": round(radius, 6),
        "axis_point_cm": [round(float(cc), 6) for cc in axis_point],
        "height_cm": round(height, 6),
        "confidence": round(perp, 4),
        "residual_cm": round(residual, 6),
    }
    return fit, residual


def _sweep_recover_cluster(v_arr, f_arr, tri_normals, planar_groups,
                          group_list, mean_normals, group_adj, axis,
                          epsilon, comp_ids):
    """Recover sweep cylinders from one cluster's monotonic arc runs.

    Grows monotonic rotation runs over the cluster's groups (``_grow_sweep_runs``),
    fits each run with at least ``_SWEEP_MIN_TOTAL_DEG`` of normal sweep
    first via the validated ``_classify_curved_patch`` competition and, on
    failure, via the axis-aware ``_fit_sweep_cylinder`` (which alone can
    fit open < 360 deg arcs).  Accepts only cylinders whose residual is
    within *epsilon*.

    Returns ``(entries, consumed_groups, consumed_tris)`` where each entry
    is a curved-patch dict (``recovered_via: "sweep"``) WITHOUT
    ``patch_index`` (the caller assigns it sequentially).
    """
    runs = _grow_sweep_runs(group_list, mean_normals, group_adj, axis)
    entries: List[Dict] = []
    consumed: set = set()
    consumed_tris: set = set()
    for run_groups, sweep_deg in runs:
        if sweep_deg < _SWEEP_MIN_TOTAL_DEG:
            continue
        run_tris = sorted(set(
            ti for gi in run_groups
            for ti in planar_groups[gi]["tri_indices"]))
        if len(run_tris) < 6:
            continue
        f_run = f_arr[np.array(run_tris, dtype=np.int64)]
        unique_idx, inverse = np.unique(
            f_run.ravel(), return_inverse=True)
        local_verts = v_arr[unique_idx]
        local_faces = inverse.reshape(-1, 3)
        try:
            part = trimesh.Trimesh(
                vertices=local_verts, faces=local_faces,
                process=False)
            surface = _classify_curved_patch(part, epsilon=epsilon)
        except Exception:
            surface = None
        if surface is None or \
                surface.get("surface_type") != "cylinder" or \
                surface.get("residual_cm", float("inf")) > epsilon:
            surface, _ = _fit_sweep_cylinder(
                v_arr, f_arr, tri_normals, run_tris, axis, epsilon)
        if surface is None or \
                surface.get("surface_type") != "cylinder" or \
                surface.get("residual_cm", float("inf")) > epsilon:
            continue
        comp = comp_ids[run_tris[0]] if run_tris else 0
        entry = {
            "component": comp,
            "triangle_count": len(run_tris),
            "area": round(_cluster_tri_area(
                v_arr, f_arr, run_tris), 6),
            "recovered_via": "sweep",
        }
        entry.update(surface)
        entries.append(entry)
        consumed.update(run_groups)
        consumed_tris.update(run_tris)
    return entries, consumed, consumed_tris


# ---------------------------------------------------------------------------
# Phase 2: extrusion bands with spline profiles (2026-08-20)
#
# A prismatic wall — any profile (arcs, corners, fitted splines) extruded
# along an axis — tessellates into planar chord strips whose shared edges
# are parallel to the extrusion axis.  The sweep layer above recovers only
# the constant-radius subsets (cylinder runs); these functions recover the
# WHOLE band as a single extrusion face by fitting the projected profile
# with an adaptive corner-pinned cubic B-spline.  Tessellation vertices
# lie exactly on the design profile (Fusion evaluates the true curve
# before extruding the polyline approximation), so a genuine band fits
# within epsilon at a small control count regardless of tessellation
# coarseness — the k/n overfit ratio, not epsilon, guards against fitting
# non-smooth garbage.
# ---------------------------------------------------------------------------


def _bspline_basis_matrix(params, knots, degree):
    """Cox-de Boor basis matrix (n_params, n_ctrl) for a knot vector.

    Vectorized triangular recursion: level 0 marks the half-open knot
    span [t_i, t_{i+1}) containing each parameter (parameters equal to
    the final knot are clipped into the last non-empty span so clamped
    end evaluation works), and each level applies the standard
    two-neighbour blend.  Repeated knots (zero-width spans) contribute
    zero through the safe-division guards.
    """
    t = np.asarray(knots, dtype=np.float64)
    u = np.asarray(params, dtype=np.float64).ravel()
    n_knots = len(t)
    n_ctrl = n_knots - degree - 1
    spans = np.searchsorted(t, u, side="right") - 1
    spans = np.clip(spans, 0, n_ctrl - 1)
    B = np.zeros((len(u), n_knots - 1), dtype=np.float64)
    B[np.arange(len(u)), spans] = 1.0
    for j in range(1, degree + 1):
        cols = n_knots - 1 - j
        Bnew = np.zeros((len(u), cols), dtype=np.float64)
        for k in range(cols):
            lo = t[k + j] - t[k]
            if lo > 0.0:
                Bnew[:, k] += ((u - t[k]) / lo) * B[:, k]
            hi = t[k + j + 1] - t[k + 1]
            if hi > 0.0:
                Bnew[:, k] += ((t[k + j + 1] - u) / hi) * B[:, k + 1]
        B = Bnew
    return B


def _periodic_profile_knots(knot_list, degree):
    """Extended evaluation knot vector for a per-period knot list.

    *knot_list* holds the (sorted, possibly repeated) knot parameters of
    one period, in [0, 1).  Tiling over offsets {-1, 0, +1} and slicing
    out the window that starts ``degree`` knots before the first 0.0
    yields the standard periodic window: ``n_c + degree`` extended
    control points covering the domain [0, 1], where ``n_c`` is the
    per-period knot count.  Extended control point ``i`` folds to
    distinct control point ``i mod n_c``.
    """
    K = sorted(knot_list)
    n_c = len(K)
    tiled = sorted([k - 1.0 for k in K] + K + [k + 1.0 for k in K])
    i0 = n_c
    return tiled[i0 - degree: i0 + n_c + degree + 1]


def _spline_curve_residual(points, params, knots, degree, ctrl, closed):
    """Per-point geometric distances from *points* to a B-spline curve.

    Each data parameter is refined by two Newton steps along the local
    tangent (central finite differences of the evaluated curve); closed
    profiles wrap refined parameters modulo the period.  Returns
    ``(mean_distance, per_point_distances)``.
    """
    pts = np.asarray(points, dtype=np.float64)
    s = np.asarray(params, dtype=np.float64).copy()
    delta = 1e-5

    def _eval(q):
        return _bspline_basis_matrix(q, knots, degree) @ ctrl

    for _ in range(2):
        c_s = _eval(s)
        if closed:
            sp, sm = s + delta, s - delta
        else:
            sp = np.clip(s + delta, 0.0, 1.0)
            sm = np.clip(s - delta, 0.0, 1.0)
        tangents = (_eval(sp) - _eval(sm))
        widths = (sp - sm)[:, None]
        tangents = tangents / np.where(np.abs(widths) > 0.0, widths, 1.0)
        denom = (tangents * tangents).sum(axis=1)
        safe = denom > 1e-18
        step = np.zeros_like(s)
        step[safe] = ((pts - c_s)[safe] * tangents[safe]).sum(axis=1) \
            / denom[safe]
        s = s + step
        s = np.mod(s, 1.0) if closed else np.clip(s, 0.0, 1.0)
    c_fin = _eval(s)
    dists = np.hypot(pts[:, 0] - c_fin[:, 0], pts[:, 1] - c_fin[:, 1])
    return float(dists.mean()), dists


def _lm_removal_bound(knots, ctrl, degree, knot_index):
    """Lyche-Morken one-step bound for removing a simple interior knot.

    Inverts Boehm insertion over the affected control window: a forward
    pass from the left window edge and a backward pass from the right
    edge each reconstruct the pre-insertion control points; when the knot
    is redundant the two reconstructions agree.  The bound is the summed
    absolute disagreement over the window (the B-spline basis is bounded
    by 1, so no extra scaling is needed).  Returns ``(bound, ok)``.
    """
    t = knots
    P = ctrl
    r = knot_index
    p = degree
    lo, hi = r - p, r
    if lo < 0 or hi >= len(P):
        return float("inf"), False
    u = float(t[r])
    fwd = P.copy()
    bwd = P.copy()
    for j in range(lo + 1, hi + 1):
        span = float(t[j + p] - t[j])
        if span <= 0.0:
            return float("inf"), False
        a = (u - float(t[j])) / span
        if not (0.0 < a < 1.0):
            return float("inf"), False
        fwd[j] = (P[j] - (1.0 - a) * fwd[j - 1]) / a
    for j in range(hi - 1, lo - 1, -1):
        span = float(t[j + p] - t[j])
        if span <= 0.0:
            return float("inf"), False
        a = (u - float(t[j])) / span
        if not (0.0 < a < 1.0):
            return float("inf"), False
        bwd[j] = (P[j + 1] - a * bwd[j + 1]) / (1.0 - a)
    diff = np.abs(fwd[lo + 1:hi] - bwd[lo + 1:hi])
    return float(diff.sum()), True


def _fit_bspline_profile(points, closed, corner_params, epsilon):
    """Corner-pinned adaptive cubic B-spline fit of a 2D profile.

    Ladder: fit by linear least squares at the minimum non-trivial
    control count (4 distinct), then insert a knot at the midpoint of
    the largest knot interval containing the worst-residual parameter
    and refit, until the mean geometric residual is within *epsilon*.
    Corner parameters place a knot at each detected corner (multiplicity
    1 — concentrating curvature without forcing C0, which would
    underdetermine fits on real meshes with many junctions); the corner
    positions are recorded in the output for the reconstruction
    consumer.  Fires the overfit guard (control points at or above n,
    or adaptive insertions at or above ``_SPLINE_OVERFIT_RATIO`` x n) by
    returning None — the profile is not smooth.  Accepted fits run a
    Lyche-Morken removal pass: knots whose removal bound stays within
    *epsilon* are dropped (least bound first), the reduced knot set is
    refitted once, and reverted if the residual regresses.

    Returns a dict (profile_closed, profile_degree,
    profile_control_points_cm, profile_knots, profile_corner_params,
    residual_cm) or None.
    """
    pts = np.asarray(points, dtype=np.float64)
    n = len(pts)
    if n < 4:
        return None
    seg = np.hypot(pts[1:, 0] - pts[:-1, 0], pts[1:, 1] - pts[:-1, 1])
    total = float(seg.sum())
    if closed:
        total += float(np.hypot(pts[0, 0] - pts[-1, 0],
                                pts[0, 1] - pts[-1, 1]))
    if total <= 1e-12:
        return None
    params = np.concatenate([[0.0], np.cumsum(seg)]) / total
    degree = 3
    corner_vals = sorted(float(c) % 1.0 for c in corner_params)

    def _intervals(bounds):
        out = []
        for a, b in zip(bounds[:-1], bounds[1:]):
            if b > a + 1e-12:
                out.append((a, b))
        return out

    def _insert_position(bounds, u_worst):
        cands = [(a, b) for (a, b) in _intervals(bounds)
                 if a <= u_worst <= b]
        if not cands:
            cands = _intervals(bounds)
        a, b = max(cands, key=lambda iv: iv[1] - iv[0])
        return 0.5 * (a + b)

    def _fit_once():
        if closed:
            t_ext = _periodic_profile_knots(K, degree)
            m_ext = len(t_ext) - degree - 1
            a_ext = _bspline_basis_matrix(params, t_ext, degree)
            design = np.zeros((n, len(K)))
            np.add.at(design.T, np.arange(m_ext) % len(K), a_ext.T)
        else:
            t_ext = np.array(
                [0.0] * (degree + 1) + interior + [1.0] * (degree + 1))
            design = _bspline_basis_matrix(params, t_ext, degree)
        q, *_ = np.linalg.lstsq(design, pts, rcond=None)
        if closed:
            ctrl_ext = np.vstack([q, q[:degree]])
        else:
            ctrl_ext = q
        mean_res, dists = _spline_curve_residual(
            pts, params, t_ext, degree, ctrl_ext, closed)
        return q, t_ext, ctrl_ext, mean_res, dists

    if closed:
        K = [0.0, 0.25, 0.5, 0.75]
        for c in corner_vals:
            K.append(c)
        K.sort()
        interior = None
        pin_ctrl0 = 4 + len(corner_vals)
    else:
        interior = []
        for c in corner_vals:
            interior.append(c)
        interior.sort()
        K = None
        pin_ctrl0 = 4 + len(corner_vals)

    while True:
        q, t_ext, ctrl_ext, mean_res, dists = _fit_once()
        n_ctrl = len(q)
        if n_ctrl >= n:
            return None
        if mean_res <= epsilon:
            break
        if n_ctrl - pin_ctrl0 >= _SPLINE_OVERFIT_RATIO * n:
            return None
        u_worst = float(params[int(np.argmax(dists))])
        if closed:
            bounds = K + [1.0 + K[0]]
            pos = _insert_position(bounds, u_worst) % 1.0
            K.append(pos)
            K.sort()
        else:
            bounds = [0.0] + interior + [1.0]
            interior.append(_insert_position(bounds, u_worst))
            interior.sort()

    # --- Lyche-Morken removal pass on the accepted fit ---
    while True:
        if closed:
            removable = [v for v in sorted(set(K))
                         if K.count(v) == 1 and v != 0.0
                         and v not in corner_vals]
        else:
            removable = [v for v in sorted(set(interior))
                         if interior.count(v) == 1
                         and v not in corner_vals]
        if not removable:
            break
        best = None
        for v in removable:
            r_idx = int(np.searchsorted(
                np.asarray(t_ext), v, side="left"))
            bound, ok = _lm_removal_bound(t_ext, ctrl_ext, degree, r_idx)
            if ok and bound <= epsilon:
                if best is None or bound < best[0]:
                    best = (bound, v)
        if best is None:
            break
        if closed:
            K.remove(best[1])
        else:
            interior.remove(best[1])
        q_red, t_red, ctrl_red, res_red, _ = _fit_once()
        if res_red <= epsilon:
            q, t_ext, ctrl_ext, mean_res = q_red, t_red, ctrl_red, res_red
        else:
            if closed:
                K.append(best[1])
                K.sort()
            else:
                interior.append(best[1])
                interior.sort()
            break

    out_knots = sorted(K) if closed else sorted(interior)
    return {
        "profile_closed": bool(closed),
        "profile_degree": degree,
        "profile_control_points_cm": [
            [round(float(a), 6), round(float(b), 6)]
            for a, b in (q if not closed else q)],
        "profile_knots": [round(float(v), 6) for v in out_knots],
        "profile_corner_params": [round(float(c), 6)
                                  for c in corner_vals],
        "residual_cm": round(float(mean_res), 8),
    }


def _band_walk_profile(band_groups, pair_junctions, planar_groups,
                       mean_normals, axis, proj, f_arr):
    """Ordered 2D profile walk over a band's group adjacency graph.

    The band graph is traversed greedily by smallest projected-normal
    azimuth turn at each step (the geometric continuation of the
    profile, in the spirit of the sweep run-grower) — this rides
    through spurious T-junction edges (coplanar-merged strips,
    non-manifold tessellation) whose azimuth turns are large, instead
    of bailing on degree >= 3 nodes.  Every group is tried as a
    bidirectional seed (forward + backward passes from the seed); the
    longest walk wins.  Junction points come from the shared
    axis-parallel mesh-edge vertex indices (exact, not
    proximity-matched); open ends take the group's projected vertex
    farthest from its single junction.  Corners are junctions where
    consecutive groups' projected mean-normal azimuths turn more than
    ``_BAND_CORNER_DEG``.

    Returns ``(points, corner_point_indices, closed, visited_groups)`` or
    None when no walk covers at least 50%% of the band's groups.
    """
    bset = set(band_groups)
    adj: Dict[int, set] = defaultdict(set)
    for (a, b) in pair_junctions:
        if a in bset and b in bset:
            adj[a].add(b)
            adj[b].add(a)
    degs = {g: len(adj[g]) for g in band_groups}
    endpoints = sorted(g for g in band_groups if degs[g] == 1)
    closed = len(endpoints) == 0

    def _azimuth(gi):
        n_vec = mean_normals[gi]
        if n_vec is None:
            return None
        perp = n_vec - float(n_vec @ axis) * axis
        ax_abs = np.abs(axis)
        dom = int(np.argmax(ax_abs))
        c1, c2 = (1, 2) if dom == 0 else ((0, 2) if dom == 1 else (0, 1))
        if float(np.hypot(perp[c1], perp[c2])) <= 1e-9:
            return None
        return math.atan2(perp[c2], perp[c1])

    def _turn(ga, gb):
        aa, ab = _azimuth(ga), _azimuth(gb)
        if aa is None or ab is None:
            return 0.0
        return abs((math.degrees(ab - aa) + 180.0) % 360.0 - 180.0)

    def _greedy(start, blocked):
        cur = start
        vis = {start}
        out = [start]
        while True:
            opts = [n for n in adj[cur]
                    if n not in vis and n not in blocked]
            if not opts:
                break
            nxt = min(opts, key=lambda n: (_turn(cur, n), n))
            vis.add(nxt)
            out.append(nxt)
            cur = nxt
        return out

    # Bidirectional multi-start: every group is tried as a seed (a
    # mid-path seed on a branched band often beats the degree-1
    # endpoints, which may sit on short side branches); each seed's
    # forward pass takes the smallest-turn neighbour and the backward
    # pass continues from the seed through the remaining neighbours, so
    # one seed covers both directions of the profile.
    best_order: Optional[List[int]] = None
    for seed in sorted(band_groups):
        forward = _greedy(seed, set())
        backward = _greedy(seed, set(forward) - {seed})
        order = list(reversed(backward)) + forward[1:]
        if best_order is None or len(order) > len(best_order):
            best_order = order
        if len(order) == len(band_groups):
            break
    if best_order is None or len(best_order) < 0.5 * len(band_groups):
        return None
    order = best_order
    visited = set(order)
    covers_all = len(visited) == len(band_groups)
    closed = closed and covers_all
    if closed and order[0] not in adj[order[-1]]:
        return None

    def _junction_2d(ga, gb):
        key = (min(ga, gb), max(ga, gb))
        va, vb = pair_junctions[key]
        return 0.5 * (proj(va) + proj(vb))

    seq: List[np.ndarray] = []
    corner_idx: List[int] = []
    if closed:
        for i in range(len(order)):
            ga, gb = order[i], order[(i + 1) % len(order)]
            seq.append(_junction_2d(ga, gb))
            if _turn(ga, gb) > _BAND_CORNER_DEG:
                corner_idx.append(len(seq) - 1)
    else:
        j_first = _junction_2d(order[0], order[1])
        pts0 = [proj(v) for v in _group_vertex_ids(
            planar_groups, order[0], f_arr)]
        outer0 = max(pts0, key=lambda p: float(
            np.hypot(p[0] - j_first[0], p[1] - j_first[1])))
        seq.append(outer0)
        for i in range(len(order) - 1):
            ga, gb = order[i], order[i + 1]
            seq.append(_junction_2d(ga, gb))
            if _turn(ga, gb) > _BAND_CORNER_DEG:
                corner_idx.append(len(seq) - 1)
        j_last = _junction_2d(order[-2], order[-1])
        pts_e = [proj(v) for v in _group_vertex_ids(
            planar_groups, order[-1], f_arr)]
        outer_e = max(pts_e, key=lambda p: float(
            np.hypot(p[0] - j_last[0], p[1] - j_last[1])))
        seq.append(outer_e)

    dedup: List[np.ndarray] = []
    remap: List[int] = []
    for i, p in enumerate(seq):
        if dedup and float(np.hypot(
                p[0] - dedup[-1][0], p[1] - dedup[-1][1])) <= 1e-9:
            remap.append(len(dedup) - 1)
        else:
            dedup.append(p)
            remap.append(len(dedup) - 1)
    if len(dedup) < 4:
        return None
    corners = sorted({remap[i] for i in corner_idx
                      if remap[i] < len(dedup)})
    return np.array(dedup), corners, closed, order


def _group_vertex_ids(planar_groups, gi, f_arr):
    """Sorted unique welded vertex ids used by one planar group."""
    tri_idx = np.asarray(planar_groups[gi]["tri_indices"], dtype=np.int64)
    if len(tri_idx) == 0:
        return []
    return sorted({int(v) for v in np.unique(f_arr[tri_idx].ravel())})


def _accept_band(band_groups, pair_junctions, v_arr, f_arr, tri_normals,
                 planar_groups, mean_normals, epsilon, comp_ids):
    """Acceptance ladder for one prismatic band (axis gates → walk → fit).

    Runs the authoritative axis estimation (out-of-plane eigenvalue
    gate), the twist tripwire, the bidirectional profile walk, and the
    ladder — full-circle Kasa cylinder first (closed bands with >= 8
    profile points), then the corner-pinned adaptive B-spline.  The
    walk may cover only part of a branched band; only the covered
    groups are accepted.

    Returns ``(surface_fields, visited_groups)`` for the caller to
    stamp into a patch entry, or None.
    """
    band_tris = sorted(set(
        ti for gi in band_groups
        for ti in planar_groups[gi]["tri_indices"]))
    if len(band_tris) < 6:
        return None

    axis_est = _find_extrusion_axis(tri_normals, band_tris)
    if axis_est is None:
        return None
    if _band_twist_rejected(
            planar_groups, band_groups, mean_normals, axis_est,
            v_arr, f_arr):
        return None

    band_verts = v_arr[np.unique(
        f_arr[np.asarray(band_tris, dtype=np.int64)].ravel())]
    v_mean = band_verts.mean(axis=0)
    ref = (np.array([1.0, 0.0, 0.0])
           if abs(float(axis_est[0])) < 0.9
           else np.array([0.0, 1.0, 0.0]))
    u_ax = np.cross(axis_est, ref)
    u_ax = u_ax / np.linalg.norm(u_ax)
    w_ax = np.cross(axis_est, u_ax)

    def _proj(vi):
        rel = v_arr[int(vi)] - v_mean
        return np.array([float(rel @ u_ax), float(rel @ w_ax)])

    walk = _band_walk_profile(
        band_groups, pair_junctions, planar_groups,
        mean_normals, axis_est, _proj, f_arr)
    if walk is None:
        return None
    points, corner_pts, closed, visited_groups = walk
    band_tris = sorted(set(
        ti for gi in visited_groups
        for ti in planar_groups[gi]["tri_indices"]))
    if len(band_tris) < 6:
        return None

    comp = comp_ids[band_tris[0]] if band_tris else 0
    base_entry = {
        "component": comp,
        "triangle_count": len(band_tris),
        "area": round(_cluster_tri_area(
            v_arr, f_arr, band_tris), 6),
    }
    axis_heights = band_verts @ axis_est
    height = float(axis_heights.max() - axis_heights.min())

    accepted = None
    if closed and len(points) >= 8:
        # below 8 profile points a circle claim is unfalsifiable
        # (any 3-4 convex points are cocircular — e.g. a square
        # tube's corners), so the Kasa rung only fires on bands
        # coarse-enough-tessellated to test roundness at all
        fit, res = _fit_sweep_cylinder(
            v_arr, f_arr, tri_normals, band_tris, axis_est, epsilon)
        if fit is not None:
            accepted = dict(fit)
            accepted["axis"] = [round(float(c), 6) for c in axis_est]
            accepted["residual_cm"] = round(float(res), 8)
            accepted["recovered_via"] = "band"
    if accepted is None:
        corner_params = []
        if len(points) > 1:
            seg = np.hypot(
                points[1:, 0] - points[:-1, 0],
                points[1:, 1] - points[:-1, 1])
            if closed:
                seg = np.concatenate([seg, [float(np.hypot(
                    points[0, 0] - points[-1, 0],
                    points[0, 1] - points[-1, 1]))]])
            csum = np.concatenate([[0.0], np.cumsum(seg)])
            denom = float(csum[-1])
            if denom > 1e-12:
                corner_params = [
                    float(csum[min(i, len(csum) - 2)])
                    / float(denom) for i in corner_pts]
        profile = _fit_bspline_profile(
            points, closed, corner_params, epsilon)
        if profile is not None:
            accepted = {
                "surface_type": "extrusion",
                "axis": [round(float(c), 6) for c in axis_est],
                "axis_point_cm": [round(float(c), 6) for c in v_mean],
                "u_axis": [round(float(c), 6) for c in u_ax],
                "w_axis": [round(float(c), 6) for c in w_ax],
                "height_cm": round(height, 6),
                "recovered_via": "spline_band",
            }
            accepted.update(profile)
    if accepted is None:
        return None
    entry = dict(base_entry)
    entry.update(accepted)
    return entry, visited_groups


def _recover_extrusion_bands(v_arr, f_arr, tri_normals, planar_groups,
                             edge_faces, tri_to_group, is_small,
                             mean_normals, consumed_groups, epsilon,
                             comp_ids, patch_idx):
    """Prismatic-band recovery pass over remaining small planar groups.

    Bands are chained DIRECTLY from shared-edge direction votes: a mesh
    edge shared by two small groups whose direction is at least
    ``_BAND_EDGE_PARALLEL_DOT``-parallel to a canonical axis unions the
    groups into that axis's band set.  This is the geometric signature
    of an extrusion (all its faces contain the axis direction, and
    consecutive faces share axis-parallel edges) — it chains corners
    and smooth junctions alike, never crosses into caps through
    perpendicular edges, and is immune to the normal-direction
    ambiguity that mis-partitions groups whose normals are
    perpendicular to two canonical axes at once.  Per band the
    acceptance ladder (``_accept_band``) runs; after each accepted band
    the consumed groups are removed and band-finding REPEATS on the
    leftovers — the unconsumed fragments of a branched band often form
    simple path sub-bands that walk cleanly on the second pass.

    Returns ``(patches, consumed_tris, consumed_groups)`` with patch
    indices continuing from *patch_idx*.
    """
    eligible = [gi for gi in range(len(planar_groups))
                if is_small[gi] and gi not in consumed_groups]
    if len(eligible) < 2:
        return [], set(), set()
    gset = set(eligible)

    edge_info: List[Tuple[int, int, np.ndarray, int, int]] = []
    for edge, tris in edge_faces.items():
        ga = gb = None
        for t in tris:
            g = tri_to_group.get(t)
            if g in gset:
                if ga is None:
                    ga = g
                elif g != ga:
                    gb = g
                    break
        if ga is None or gb is None:
            continue
        va, vb = int(edge[0]), int(edge[1])
        d = v_arr[vb] - v_arr[va]
        dn = float(np.linalg.norm(d))
        if dn <= 1e-12:
            continue
        edge_info.append((min(ga, gb), max(ga, gb), d / dn, va, vb))

    patches: List[Dict] = []
    used_tris: set = set()
    used_groups: set = set()

    # Outer repeat loop over the whole 3-axis sweep: a band that fails
    # on one pass (e.g. an over-branched mega-band whose walk covers
    # too little) often fragments into walkable sub-bands once a LATER
    # axis pass consumes the bridging groups — re-running earlier axes
    # on the shrunken leftover set picks those up.
    made_progress = True
    while made_progress:
        made_progress = False
        for axis_i in range(3):
            axis = _CANON_AXES[axis_i]
            axis_edges = [
                (ga, gb, va, vb) for ga, gb, d_hat, va, vb in edge_info
                if abs(float(d_hat @ axis)) > _BAND_EDGE_PARALLEL_DOT]
            remaining = set(eligible) - used_groups
            if len(remaining) < 2:
                continue

            parent = {gi: gi for gi in remaining}

            def _find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            pair_junctions: Dict[Tuple[int, int], Tuple[int, int]] = {}
            for ga, gb, va, vb in axis_edges:
                if ga not in remaining or gb not in remaining:
                    continue
                pair_junctions[(ga, gb)] = (va, vb)
                ra, rb = _find(ga), _find(gb)
                if ra != rb:
                    parent[ra] = rb

            comps: Dict[int, List[int]] = defaultdict(list)
            for gi in remaining:
                comps[_find(gi)].append(gi)

            for root in sorted(comps, key=lambda r: min(comps[r])):
                band_groups = sorted(comps[root])
                if len(band_groups) < 2:
                    continue
                outcome = _accept_band(
                    band_groups, pair_junctions, v_arr, f_arr,
                    tri_normals, planar_groups, mean_normals, epsilon,
                    comp_ids)
                if outcome is None:
                    continue
                entry, visited_groups = outcome
                entry["patch_index"] = patch_idx + len(patches)
                patches.append(entry)
                band_tris = sorted(set(
                    ti for gi in visited_groups
                    for ti in planar_groups[gi]["tri_indices"]))
                used_tris.update(band_tris)
                used_groups.update(visited_groups)
                made_progress = True

    return patches, used_tris, used_groups


def _band_twist_rejected(planar_groups, band_groups, mean_normals, axis,
                         v_arr, f_arr):
    """Azimuth-vs-axial-coordinate twist tripwire for one band.

    A prismatic band's strips each span (nearly) the full axial extent,
    so their projected-normal azimuths are uncorrelated with their mean
    axial position.  A helical band's strips are axially short and their
    azimuths drift monotonically with height — max correlation above
    ``_BAND_TWIST_CORR_MAX`` with every strip spanning less than
    ``_BAND_TWIST_SPAN_FRAC`` of the band's axial extent is twist, and
    the band is rejected (it is not an extrusion of any 2D profile).
    """
    thetas: List[float] = []
    hs: List[float] = []
    spans: List[float] = []
    lo, hi = float("inf"), float("-inf")
    ax_abs = np.abs(axis)
    dom = int(np.argmax(ax_abs))
    c1, c2 = (1, 2) if dom == 0 else ((0, 2) if dom == 1 else (0, 1))
    for gi in band_groups:
        n_vec = mean_normals[gi]
        if n_vec is None:
            return False
        perp = n_vec - float(n_vec @ axis) * axis
        if float(np.hypot(perp[c1], perp[c2])) <= 1e-9:
            return False
        thetas.append(math.atan2(perp[c2], perp[c1]))
        g_tris = planar_groups[gi]["tri_indices"]
        verts = v_arr[np.unique(
            f_arr[np.asarray(g_tris, dtype=np.int64)].ravel())]
        proj_ax = verts @ axis
        hs.append(float(proj_ax.mean()))
        spans.append(float(proj_ax.max() - proj_ax.min()))
        lo = min(lo, float(proj_ax.min()))
        hi = max(hi, float(proj_ax.max()))
    extent = hi - lo
    if extent <= 1e-12 or not spans:
        return False
    if max(spans) >= _BAND_TWIST_SPAN_FRAC * extent:
        return False
    th = np.asarray(thetas, dtype=np.float64)
    h = np.asarray(hs, dtype=np.float64)

    def _corr(a):
        sa, sb = float(a.std()), float(h.std())
        if sa <= 1e-12 or sb <= 1e-12:
            return 0.0
        return float(np.corrcoef(a, h)[0, 1])

    return max(abs(_corr(np.cos(th))), abs(_corr(np.sin(th)))) \
        > _BAND_TWIST_CORR_MAX


def _recover_curved_from_small_groups(v_arr, f_arr, tri_normals,
                                      planar_groups, epsilon, comp_ids):
    """Recover curved patches from small planar groups (fix-e + competition).

    After planar grouping, small groups (<= ``_SMALL_GROUP_MAX`` tris)
    that are actually tessellated curved surfaces get absorbed as planar
    fragments.  This function:

      1. Builds group adjacency over shared edges.
      2. Union-finds connected components of small groups into clusters.
      3. For each cluster: runs the FULL model competition (cylinder /
         sphere / cone / torus) via ``_classify_curved_patch`` on a
         compact trimesh built from the cluster's triangles.  If a
         non-freeform surface wins, emits it as a recovered curved patch.
      4. Sweep-run recovery: when the competition fails or is skipped by
         the sharp gate, detect an extrusion axis (>= 80% of cluster
         normals perpendicular to a dominant cross-product axis) and
         region-grow monotonic arc runs over the cluster's groups.  Each
         run sweeping >= _SWEEP_MIN_TOTAL_DEG is fitted as a compact
         trimesh; accepted cylinders become sweep-recovered patches
         (``recovered_via: "sweep"``).  Unconsumed groups (straight runs,
         leftover slivers) stay in the cluster.
      5. Cap-contamination retry: when the competition and the sweep both
          fail to explain the cluster, falls back to a cylinder-only fit.
          If the fit fails but produced a valid axis, splits the cluster
          by |dot(tri_normal, axis)| < 0.15 (side=wall vs non-side=cap).
          If both exist and the side-only fit passes, emits side tris as
          one cylinder patch and leaves non-side groups as planar.
      6. Extrusion-band recovery: remaining small groups chained through
          shared mesh edges parallel to a canonical axis form prismatic
          bands; closed bands fitting a circle emit a 360 deg cylinder,
          and the rest emit ``extrusion`` faces whose profiles are
          corner-pinned adaptive B-splines (``recovered_via:
          "spline_band"``).

    The retry only fires when the competition AND the direct cylinder fit
    both fail — clusters that pass on the first attempt (fine-tessellation
    cylinders, meshb2 hole walls, spheres, tori, cones) are recovered
    immediately and the retry is never reached.

    Args:
        v_arr, f_arr, tri_normals: welded mesh arrays + per-tri normals.
        planar_groups: output of ``_group_planar_triangles``.
        epsilon: derived acceptance tolerance from ``_estimate_delta``.
        comp_ids: per-triangle component IDs.

    Returns:
        ``(recovered_patches, recovered_tri_indices, remaining_groups)``
        where recovered_patches is a list of curved-patch dicts matching
        the existing curved-patch schema, recovered_tri_indices is the set
        of triangle indices now assigned to curved patches, and
        remaining_groups is the planar_groups list with fully-recovered
        groups removed.
    """
    n_faces = len(f_arr)

    # --- edge-to-face adjacency ---
    edge_faces: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for ti in range(n_faces):
        a, b, c = int(f_arr[ti, 0]), int(f_arr[ti, 1]), int(f_arr[ti, 2])
        for e in ((min(a, b), max(a, b)),
                  (min(b, c), max(b, c)),
                  (min(a, c), max(a, c))):
            if e[0] != e[1]:
                edge_faces[e].append(ti)

    # --- group adjacency via shared edges ---
    tri_to_group: Dict[int, int] = {}
    for gi, g in enumerate(planar_groups):
        for ti in g["tri_indices"]:
            tri_to_group[ti] = gi

    group_adj: Dict[int, set] = defaultdict(set)
    for tris in edge_faces.values():
        tl = list(tris)
        for i in range(len(tl)):
            for j in range(i + 1, len(tl)):
                ga = tri_to_group.get(tl[i])
                gb = tri_to_group.get(tl[j])
                if ga is not None and gb is not None and ga != gb:
                    group_adj[ga].add(gb)
                    group_adj[gb].add(ga)

    # --- identify small groups ---
    is_small = [len(g["tri_indices"]) <= _SMALL_GROUP_MAX
                for g in planar_groups]

    # --- union-find on adjacent small groups ---
    # Normal-continuity + axis-partition gate (2026-08-19): two adjacent
    # small groups chain only when (a) their mean normals agree within
    # _MAX_CHAIN_ANGLE_DEG AND (b) they share the same dominant
    # perpendicular canonical axis (or either lacks one).  Without the
    # axis partition, fillet bands (⊥ X/Y) chain into wall bands (⊥ Z)
    # through the smooth < 30° normal transitions at fillet-wall
    # junctions, producing mixed-axis clusters that no single extrusion
    # axis can explain.
    n_groups = len(planar_groups)
    parent = list(range(n_groups))
    mean_normals = _group_mean_normals(planar_groups, tri_normals)
    axis_parts = [_group_axis_partition(mn) for mn in mean_normals]
    chain_cos = math.cos(math.radians(_MAX_CHAIN_ANGLE_DEG))

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a, b):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    for gi in range(n_groups):
        if not is_small[gi] or mean_normals[gi] is None:
            continue
        for nbr in group_adj.get(gi, ()):
            if nbr < n_groups and is_small[nbr] \
                    and mean_normals[nbr] is not None \
                    and abs(float(mean_normals[gi] @ mean_normals[nbr])) \
                    >= chain_cos \
                    and (axis_parts[gi] == -1 or axis_parts[nbr] == -1
                         or axis_parts[gi] == axis_parts[nbr]):
                _union(gi, nbr)

    clusters: Dict[int, List[int]] = defaultdict(list)
    for gi in range(n_groups):
        if is_small[gi]:
            clusters[_find(gi)].append(gi)

    recovered_patches: List[Dict] = []
    recovered_tri_indices: set = set()
    recovered_groups: set = set()
    patch_idx = 0

    for root, group_list in clusters.items():
        cluster_tris = sorted(set(
            ti for gi in group_list
            for ti in planar_groups[gi]["tri_indices"]))

        if len(cluster_tris) < 6:
            continue

        # --- (0) Sharp-edge gate ---
        # Clusters with sharp dihedral edges (boxes, capped cylinders) are
        # not smooth curved surfaces.  On such clusters the sphere fit can
        # produce a false positive (box vertices are cospherical → residual
        # 0).  Gate the competition on mean |dot(adjacent normals)| >= 0.707
        # (≈ average dihedral <= 45°) so only smooth clusters enter it.
        tri_set_c = frozenset(cluster_tris)
        dot_sum = 0.0
        dot_n = 0
        for ti in cluster_tris:
            a, b, c = (int(f_arr[ti, 0]),
                       int(f_arr[ti, 1]),
                       int(f_arr[ti, 2]))
            for e in ((min(a, b), max(a, b)),
                      (min(b, c), max(b, c)),
                      (min(a, c), max(a, c))):
                if e[0] == e[1]:
                    continue
                for nbr in edge_faces.get(e, ()):
                    if nbr != ti and nbr in tri_set_c:
                        dot_sum += abs(
                            float(tri_normals[ti] @ tri_normals[nbr]))
                        dot_n += 1
        mean_normal_dot = dot_sum / dot_n if dot_n > 0 else 1.0

        # --- (1) Full model competition (cylinder / sphere / cone / torus) ---
        if mean_normal_dot >= 0.707:
            f_sub = f_arr[np.array(cluster_tris, dtype=np.int64)]
            unique_idx, inverse = np.unique(
                f_sub.ravel(), return_inverse=True)
            local_verts = v_arr[unique_idx]
            local_faces = inverse.reshape(-1, 3)
            try:
                part = trimesh.Trimesh(
                    vertices=local_verts, faces=local_faces,
                    process=False)
                surface = _classify_curved_patch(part, epsilon=epsilon)
            except Exception:
                surface = None

            if surface is not None and \
                    surface.get("surface_type") != "freeform":
                comp = comp_ids[cluster_tris[0]] if cluster_tris else 0
                entry = {
                    "component": comp,
                    "patch_index": patch_idx,
                    "triangle_count": len(cluster_tris),
                    "area": round(_cluster_tri_area(
                        v_arr, f_arr, cluster_tris), 6),
                }
                entry.update(surface)
                recovered_patches.append(entry)
                patch_idx += 1
                recovered_tri_indices.update(cluster_tris)
                for gi in group_list:
                    recovered_groups.add(gi)
                continue

        # --- (1.5) Sweep-run recovery (extruded-wall arcs) ---
        # A finely tessellated wall EXTRUDED from a curved profile (arcs
        # of cylinders sweeping around the extrusion axis) fragments into
        # dozens of small planar groups; when several arcs with different
        # radii chain into one cluster, the whole-cluster competition
        # correctly returns freeform and the arcs are lost.  Split the
        # cluster into monotonic rotation runs and fit each run as its
        # own cylinder.  Runs that stop (straight edges, sharp junctions,
        # direction reversal) are left for steps 2/3.
        axis = _find_extrusion_axis(tri_normals, cluster_tris)
        if axis is not None:
            sweep_entries, consumed_groups, consumed_tris = \
                _sweep_recover_cluster(
                    v_arr, f_arr, tri_normals, planar_groups,
                    group_list, mean_normals, group_adj,
                    axis, epsilon, comp_ids)
            for e in sweep_entries:
                e["patch_index"] = patch_idx
                recovered_patches.append(e)
                patch_idx += 1
            if consumed_groups:
                recovered_groups.update(consumed_groups)
                recovered_tri_indices.update(consumed_tris)
                group_list = [gi for gi in group_list
                              if gi not in consumed_groups]
                cluster_tris = sorted(set(
                    ti for gi in group_list
                    for ti in planar_groups[gi]["tri_indices"]))
                if len(cluster_tris) < 6:
                    continue

        # --- (2) Cylinder-only fit (fallback + cap-retry trigger) ---
        fit, res = _cylinder_residual_on_tris(
            v_arr, f_arr, tri_normals, cluster_tris)

        if fit is not None and res <= epsilon:
            comp = comp_ids[cluster_tris[0]] if cluster_tris else 0
            entry = {
                "component": comp,
                "patch_index": patch_idx,
                "triangle_count": len(cluster_tris),
                "area": round(_cluster_tri_area(
                    v_arr, f_arr, cluster_tris), 6),
            }
            entry.update(fit)
            entry["residual"] = round(res, 6)
            recovered_patches.append(entry)
            patch_idx += 1
            recovered_tri_indices.update(cluster_tris)
            for gi in group_list:
                recovered_groups.add(gi)
            continue

        # --- (3) Cap-contamination retry ---
        # Fires ONLY on full-fit failure.  Split by normal-vs-axis
        # direction: side groups (normals ⊥ axis) are wall candidates,
        # non-side groups (normals ∥ axis) are cap candidates.
        if fit is not None:
            axis = np.array(fit["axis"], dtype=np.float64)
            side_groups = []
            non_side_groups = []
            for gi in group_list:
                g_tris = planar_groups[gi]["tri_indices"]
                normals_g = tri_normals[np.asarray(g_tris)]
                dots = np.abs(normals_g @ axis)
                if float(np.mean(dots < 0.15)) > 0.5:
                    side_groups.append(gi)
                else:
                    non_side_groups.append(gi)

            if non_side_groups and side_groups:
                side_tris = sorted(set(
                    ti for gi in side_groups
                    for ti in planar_groups[gi]["tri_indices"]))
                if len(side_tris) >= 6:
                    side_fit, side_res = _cylinder_residual_on_tris(
                        v_arr, f_arr, tri_normals, side_tris)
                    if side_fit is not None and side_res <= epsilon:
                        comp = comp_ids[side_tris[0]] if side_tris else 0
                        entry = {
                            "component": comp,
                            "patch_index": patch_idx,
                            "triangle_count": len(side_tris),
                            "area": round(_cluster_tri_area(
                                v_arr, f_arr, side_tris), 6),
                        }
                        entry.update(side_fit)
                        entry["residual"] = round(side_res, 6)
                        recovered_patches.append(entry)
                        patch_idx += 1
                        recovered_tri_indices.update(side_tris)
                        for gi in side_groups:
                            recovered_groups.add(gi)
                        # non-side groups stay as planar
                        continue

    # --- (4) Extrusion-band recovery: spline profiles (2026-08-20) ---
    # Runs over small groups the per-cluster ladder above did NOT
    # consume; bands chained through axis-parallel shared edges accept
    # as whole extrusion faces (cylinder for full-circle profiles,
    # corner-pinned B-spline profiles otherwise).
    band_patches, band_tris, band_groups_used = _recover_extrusion_bands(
        v_arr, f_arr, tri_normals, planar_groups, edge_faces,
        tri_to_group, is_small, mean_normals, recovered_groups,
        epsilon, comp_ids, patch_idx)
    if band_patches:
        recovered_patches.extend(band_patches)
        recovered_tri_indices.update(band_tris)
        recovered_groups.update(band_groups_used)

    remaining_groups = [g for gi, g in enumerate(planar_groups)
                        if gi not in recovered_groups]

    return recovered_patches, recovered_tri_indices, remaining_groups


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


def _triangle_centroid(v_arr, f_arr, ti):
    """Centroid of triangle *ti* as a (float, float, float) tuple."""
    a = v_arr[f_arr[ti, 0]]
    b = v_arr[f_arr[ti, 1]]
    c = v_arr[f_arr[ti, 2]]
    return ((a[0] + b[0] + c[0]) / 3.0,
            (a[1] + b[1] + c[1]) / 3.0,
            (a[2] + b[2] + c[2]) / 3.0)


def _accept_by_running_plane_fit(face_centroid, face_normal,
                                  centroid_sum, cov_sum, cnt,
                                  seed_normal, offset_tol, cos_tol):
    """R-7 running-plane-fit acceptance predicate.

    Reconstructs the refit plane normal from the ACCUMULATED centroid
    covariance of already-accepted triangles (O(1), 3x3 analytic
    eigenvector), then tests whether *face_centroid* lies within
    *offset_tol* of that plane AND *face_normal* agrees with the refit
    normal within *cos_tol*.

    When *cnt* < 3 (fewer than 3 centroids) the covariance is rank-
    deficient (2 points → line, 1 point → point) and the smallest
    eigenvector is underdetermined.  In that regime the seed's own plane
    normal is used — this is correct for truly coplanar triangles and
    avoids rejecting valid neighbours during the initial expansion.
    """
    if cnt == 0:
        return True
    if cnt < 3:
        refit_n = seed_normal
        mx = centroid_sum[0] / cnt
        my = centroid_sum[1] / cnt
        mz = centroid_sum[2] / cnt
    else:
        inv = 1.0 / cnt
        mx = centroid_sum[0] * inv
        my = centroid_sum[1] * inv
        mz = centroid_sum[2] * inv
        cov = np.array([
            [cov_sum[0][0] * inv - mx * mx,
             cov_sum[0][1] * inv - mx * my,
             cov_sum[0][2] * inv - mx * mz],
            [cov_sum[0][1] * inv - mx * my,
             cov_sum[1][1] * inv - my * my,
             cov_sum[1][2] * inv - my * mz],
            [cov_sum[0][2] * inv - mx * mz,
             cov_sum[1][2] * inv - my * mz,
             cov_sum[2][2] * inv - mz * mz],
        ], dtype=np.float64)
        refit_n = _smallest_eigenvector_3x3(cov)

    nu = face_normal
    dot = abs(float(nu[0] * refit_n[0] + nu[1] * refit_n[1] + nu[2] * refit_n[2]))
    if dot < cos_tol:
        return False
    cx, cy, cz = face_centroid
    d = abs(refit_n[0] * (cx - mx)
            + refit_n[1] * (cy - my)
            + refit_n[2] * (cz - mz))
    return d < offset_tol


def _group_planar_triangles(v_arr, f_arr, tri_normals, angle_tol_deg, extent,
                            tol=None):
    """Greedy-group triangles by coplanarity (normal within *angle_tol_deg*,
    plane offset within tol).

    Returns a list of dicts: ``{"normal": np.ndarray(3), "offset": float,
    "tri_indices": List[int]}``.

    R-2: a connectivity-constrained region-growing FIRST PASS is the primary
    path.  Regions are grown from the largest-area unassigned triangle over
    edge-adjacent faces (edge map over the welded soup, with near-coincident
    seam vertices clustered first) using the coplanarity predicate.  Any
    triangle the connectivity pass cannot place falls through to the
    unchanged global first-match path.  Deterministic: seeds by largest area
    (smallest index on ties), neighbors traversed in sorted index order,
    groups emitted in seed order.

    R-7: when *tol* is a ``_ToleranceConfig``, the connectivity pass uses a
    RUNNING plane-fit (accumulated centroid covariance → smallest eigenvector
    → refit plane) instead of the static first-match predicate, removing
    seed-order bias.  When *tol* is None the original static predicate
    ``_coplanar_with_region`` is used (backward-compat).
    """
    cos_tol = math.cos(math.radians(angle_tol_deg))
    use_running_fit = tol is not None
    if use_running_fit:
        offset_tol = tol.offset_tol
    else:
        offset_tol = max(1e-6, 1e-4 * extent)
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
    # R-7: when *tol* is provided, uses a running plane-fit residual
    # (accumulated centroid covariance → smallest eigenvector → refit plane)
    # instead of the static seed-plane predicate.  This removes seed-order
    # bias while preserving connectivity semantics (region still grows from
    # edge-adjacent seeds).
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
        if use_running_fit:
            c0 = _triangle_centroid(v_arr, f_arr, seed)
            centroid_sum = list(c0)
            cov_sum = [[c0[0] * c0[0], c0[0] * c0[1], c0[0] * c0[2]],
                       [c0[1] * c0[0], c0[1] * c0[1], c0[1] * c0[2]],
                       [c0[2] * c0[0], c0[2] * c0[1], c0[2] * c0[2]]]
            cnt = 1
        while qi < len(queue):
            cur = queue[qi]
            qi += 1
            region.append(cur)
            for nbr in sorted(adj[cur]):
                if nbr in seen or assigned[nbr] or face_planes[nbr] is None:
                    continue
                fp_nu, fp_off = face_planes[nbr]
                accepted = False
                if use_running_fit:
                    accepted = _accept_by_running_plane_fit(
                        _triangle_centroid(v_arr, f_arr, nbr),
                        fp_nu, centroid_sum, cov_sum, cnt,
                        gn, offset_tol, cos_tol)
                else:
                    accepted = _coplanar_with_region(
                        (fp_nu, fp_off), (gn, goffset), cos_tol, offset_tol)
                if not accepted:
                    continue
                seen.add(nbr)
                queue.append(nbr)
                if use_running_fit:
                    cn = _triangle_centroid(v_arr, f_arr, nbr)
                    centroid_sum[0] += cn[0]
                    centroid_sum[1] += cn[1]
                    centroid_sum[2] += cn[2]
                    cov_sum[0][0] += cn[0] * cn[0]
                    cov_sum[0][1] += cn[0] * cn[1]
                    cov_sum[0][2] += cn[0] * cn[2]
                    cov_sum[1][1] += cn[1] * cn[1]
                    cov_sum[1][2] += cn[1] * cn[2]
                    cov_sum[2][2] += cn[2] * cn[2]
                    cov_sum[1][0] = cov_sum[0][1]
                    cov_sum[2][0] = cov_sum[0][2]
                    cov_sum[2][1] = cov_sum[1][2]
                    cnt += 1
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


def _count_residual_loop_edges(loops, residual_pairs):
    """Count occurrences of residual non-manifold edges in vertex-index loops.

    A *residual edge* is an undirected vertex pair whose directed-edge
    cancellation count ``|net|`` is not 1 — the seam was too wide for
    snapping to close, leaving an unbalanced boundary edge (R-4).  Each
    occurrence of such an edge among the consecutive vertex pairs of *loops*
    is counted once.  Returns 0 when no residual pairs are given.
    """
    if not residual_pairs or not loops:
        return 0
    count = 0
    for loop in loops:
        m = len(loop)
        for i in range(m):
            if frozenset((loop[i], loop[(i + 1) % m])) in residual_pairs:
                count += 1
    return count


def _boundary_loops(tri_indices, welded_tris, remap=None,
                    tri_normals=None, group_normal=None,
                    welded_verts=None, tjunc_tol=0.0,
                    residual_out=None):
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
    residual_pairs: set = set()
    pre_pinch_residual_count = 0
    for (a, b), fwd in edge_counts.items():
        if (a, b) in seen:
            continue
        rev = edge_counts.get((b, a), 0)
        seen.add((a, b))
        seen.add((b, a))
        net = fwd - rev
        if net != 1 and net != -1:
            # |net| >= 2: the edge survived cancellation unbalanced — a
            # residual non-manifold boundary edge (seam too wide to close).
            residual_pairs.add(frozenset((a, b)))
            pre_pinch_residual_count += abs(net)
        if net > 0:
            boundary.extend([(a, b)] * net)
        elif net < 0:
            boundary.extend([(b, a)] * (-net))
    loops = _split_pinch_loops(_chain_directed_edges(boundary))
    if residual_out is not None:
        residual_out.update({
            "residual_pairs": residual_pairs,
            "pre_pinch_residual_count": pre_pinch_residual_count,
            "post_pinch_residual_count":
                _count_residual_loop_edges(loops, residual_pairs),
        })
    return loops


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


# ---------------------------------------------------------------------------
# R-5: generalized winding number (Jacobson et al. 2013) and hole-classification
# helpers
# ---------------------------------------------------------------------------

def _generalized_winding_number(point, welded_verts, welded_tris):
    """Generalized winding number (Jacobson, Kavan, Sorkine-Hornung 2013).

    Computes the solid-angle sum of all triangles subtended at *point*,
    divided by 4π.  For a watertight mesh the result is 0 outside, 1 inside.
    The winding number is ill-defined for non-manifold meshes; callers must
    guard with ``has_warnings is False`` before invoking this function.

    Args:
        point: 3D probe point ``(x, y, z)``.
        welded_verts: list of vertex coordinate triples.
        welded_tris: list of ``(a, b, c)`` index triples into *welded_verts*.

    Returns:
        float (typically near 0 or 1 for a clean mesh).

    Complexity: O(n) in number of triangles; < 100 ms for 2000-tris on a
    modern CPU (numpy-vectorised, no per-element Python loop).
    """
    if len(welded_tris) == 0:
        return 0.0

    p = np.asarray(point, dtype=np.float64)
    verts = np.asarray(welded_verts, dtype=np.float64)
    tri_arr = np.array(welded_tris, dtype=np.int64)

    a = verts[tri_arr[:, 0]] - p      # (N, 3)
    b = verts[tri_arr[:, 1]] - p
    c = verts[tri_arr[:, 2]] - p

    la = np.linalg.norm(a, axis=1)    # (N,)
    lb = np.linalg.norm(b, axis=1)
    lc = np.linalg.norm(c, axis=1)

    valid = (la > 1e-15) & (lb > 1e-15) & (lc > 1e-15)
    if not np.any(valid):
        return 0.0

    a = a[valid]; b = b[valid]; c = c[valid]
    la = la[valid]; lb = lb[valid]; lc = lc[valid]

    numerator = np.sum(a * np.cross(b, c), axis=1)
    denominator = (
        la * lb * lc
        + la * np.sum(b * c, axis=1)
        + lb * np.sum(c * a, axis=1)
        + lc * np.sum(a * b, axis=1)
    )

    omega = 2.0 * np.arctan2(numerator, denominator)
    return float(np.sum(omega) / (4.0 * math.pi))


def _same_sign_2d(a, b):
    """True if *a* and *b* are both positive or both negative.

    Zero-area inputs (== 0) return False — a degenerate loop cannot be a
    meaningful hole candidate.
    """
    return (a > 0.0 and b > 0.0) or (a < 0.0 and b < 0.0)


def _centroid_near_boundary(loop_2d, centroids_2d):
    """Check whether any triangle centroid is within 1 % of the loop's
    effective diameter from the loop boundary.

    When a centroid is very close to a loop edge, ray-casting
    (``_point_in_polygon_2d``) may misclassify it due to floating-point
    edge cases.  The calling loop is considered *ambiguous* and should
    fall back to the generalized winding number (R-5).

    Returns:
        bool — True if at least one centroid is within 1 % of the loop
        edge-length scale from the nearest edge of *loop_2d*.
    """
    m = len(loop_2d)
    if m < 3 or not centroids_2d:
        return False

    # Loop diameter: maximum vertex-to-vertex distance in 2D.
    max_sq = 0.0
    for i in range(m):
        xi, yi = loop_2d[i]
        for j in range(i + 1, m):
            dx = xi - loop_2d[j][0]
            dy = yi - loop_2d[j][1]
            d2 = dx * dx + dy * dy
            if d2 > max_sq:
                max_sq = d2
    diam = math.sqrt(max_sq)
    if diam < 1e-15:
        return False
    threshold = 0.01 * diam

    # Pre-compute edge segments for distance tests.
    edges = []
    for i in range(m):
        x1, y1 = loop_2d[i]
        x2, y2 = loop_2d[(i + 1) % m]
        dx = x2 - x1
        dy = y2 - y1
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq < 1e-30:
            edges.append((x1, y1, x1, y1, 0.0, 0.0))
        else:
            inv_len = 1.0 / math.sqrt(seg_len_sq)
            edges.append((x1, y1, x2, y2, dx * inv_len, dy * inv_len))

    for cx, cy in centroids_2d:
        min_dist = float("inf")
        for (x1, y1, x2, y2, ex, ey) in edges:
            # Project centroid onto the infinite line through the edge.
            wx = cx - x1
            wy = cy - y1
            t = wx * ex + wy * ey
            if t <= 0.0:
                # Closest to start vertex.
                dist = math.hypot(cx - x1, cy - y1)
            elif t * t >= (x2 - x1)*(x2 - x1) + (y2 - y1)*(y2 - y1):
                # Closest to end vertex.
                dist = math.hypot(cx - x2, cy - y2)
            else:
                # Perpendicular distance.
                proj_x = x1 + t * ex
                proj_y = y1 + t * ey
                dist = math.hypot(cx - proj_x, cy - proj_y)
            if dist < min_dist:
                min_dist = dist
        if min_dist < threshold:
            return True
    return False


def _compute_interior_probe_3d(loop_pts_2d, loop_pts_3d, normal,
                               origin_3d):
    """Compute a 3D probe point guaranteed to lie inside the 2D loop.

    Takes the loop's 2D centroid and offsets it inward (toward a vertex
    direction) by 1 % of the loop diameter.  Then converts back to 3D
    using the same projection parameters that ``_project_to_2d`` used.

    Returns:
        ``(x, y, z)`` tuple in 3D.
    """
    m = len(loop_pts_2d)
    if m < 3:
        return tuple(origin_3d)

    cx = sum(p[0] for p in loop_pts_2d) / m
    cy = sum(p[1] for p in loop_pts_2d) / m

    # Diameter from max pairwise distance.
    max_sq = 0.0
    for i in range(m):
        xi, yi = loop_pts_2d[i]
        for j in range(i + 1, m):
            dx = xi - loop_pts_2d[j][0]
            dy = yi - loop_pts_2d[j][1]
            d2 = dx * dx + dy * dy
            if d2 > max_sq:
                max_sq = d2
    diam = math.sqrt(max_sq)
    if diam < 1e-15:
        return tuple(origin_3d)

    # Direction: centroid → farthest vertex.
    farthest_dist = 0.0
    farthest_dx = 1.0
    farthest_dy = 0.0
    for x, y in loop_pts_2d:
        d2 = (x - cx)**2 + (y - cy)**2
        if d2 > farthest_dist:
            farthest_dist = d2
            farthest_dx = x - cx
            farthest_dy = y - cy
    norm = math.hypot(farthest_dx, farthest_dy)
    if norm < 1e-15:
        return tuple(origin_3d)

    offset = 0.01 * diam
    probe_x = cx + (farthest_dx / norm) * offset
    probe_y = cy + (farthest_dy / norm) * offset

    # Project 2D back to 3D using the same u,v basis as _project_to_2d.
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
    org = np.asarray(origin_3d, dtype=np.float64)
    result = org + probe_x * u + probe_y * v
    return (float(result[0]), float(result[1]), float(result[2]))


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
                            comp_ids, v_arr, f_arr, epsilon=None):
    """Classify non-planar triangles into curved surface patches.

    Groups curved triangles by component, builds a trimesh per group, splits
    into connected patches, and classifies each (cylinder / sphere / cone /
    freeform).

    When *epsilon* is provided (fix-e), each patch classification is gated
    by the derived tessellation-error tolerance: a fit whose mean geometric
    residual exceeds epsilon falls through to freeform, preventing false
    curved patches on planar-only meshes.
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
            surface = _classify_curved_patch(part, epsilon=epsilon)
            entry = {"component": comp, "patch_index": patch_idx,
                     "triangle_count": len(part.faces),
                     "area": round(float(part.area), 6)}
            entry.update(surface)
            patches.append(entry)
            patch_idx += 1
    return patches


def decompose_mesh_faces(nodes, indices, angle_tolerance_deg=None,
                         simplify_vertices=True, offset_tol=None,
                         snap_tol=None, simp_tol=None, preset=None):
    """Decompose mesh into planar faces and curved patches.

    Returns a dict with ``components_detected``, ``planar_faces``, and
    ``curved_patches`` (plus a top-level ``has_warnings`` flag, R-4).  Each
    planar face includes ordered polygon vertices, normal, interior angles,
    area, optional ``holes`` (list of inner-loop point lists), and a
    ``warnings`` list of per-face residual non-manifold edge warnings.
    Curved patches report triangle count, area, and a fitted surface
    classification (cylinder / sphere / cone / freeform).

    Args:
        nodes: Vertex coordinates (flat list or list of triples).
        indices: Triangle corner indices (flat list or list of triples).
        angle_tolerance_deg:
            Dihedral angle threshold for planar grouping in degrees.
            Default (``None``) resolves to ``0.5``, or the preset value
            when a preset is active.  An explicit value always wins.
        simplify_vertices:
            If True (default), collapse nearly-collinear polygon vertices
            in the 2D outline via ``simp_tol``.
        offset_tol:
            Per-plane offset tolerance for coplanarity grouping (cm).
            Overrides both defaults and preset values when provided.
        snap_tol:
            Vertex-snap tolerance for seam closure (cm).  Overrides
            both defaults and preset values when provided.
        simp_tol:
            Polygon simplification tolerance (cm).  Overrides both
            defaults and preset values when provided.
        preset:
            Optional tolerance preset — one of ``"accurate"``, ``"balanced"``,
            ``"coarse"``.  ``"accurate"`` uses extent-relative tolerances
            an order of magnitude tighter than defaults; ``"coarse"``
            uses looser tolerances; ``"balanced"`` keeps the defaults.
            Invalid preset names raise ``ValueError``.

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
                    "curved_patches": [], "has_warnings": False}

        # --- Bug A: weld FIRST, then process=False ---
        extent = _max_extent_nodes(node_list)
        quant_step = _detect_quantization_step(node_list)
        tol = _ToleranceConfig.from_(quant_step, extent)

        # R-9: resolve preset + individual tolerance overrides
        # angle_tolerance_deg=None means "use default or preset" at every
        # layer (decompose_mesh_faces, analyze_mesh_data, and the MCP tools
        # all default to None and forward it), so the sentinel always
        # reaches this resolution point unless the caller passed an explicit
        # value.
        if angle_tolerance_deg is None:
            effective_angle = 0.5  # default
        else:
            effective_angle = float(angle_tolerance_deg)

        if preset is not None:
            preset = preset.lower()
            if preset == "accurate":
                if angle_tolerance_deg is None:
                    effective_angle = 0.1
                tol = _dataclasses_replace(tol,
                    offset_tol=max(1e-8, 1e-6 * extent),
                    snap_tol=max(1e-8, 1e-6 * extent))
            elif preset == "balanced":
                # defaults — effective_angle already resolved above
                pass
            elif preset == "coarse":
                if angle_tolerance_deg is None:
                    effective_angle = 1.0
                tol = _dataclasses_replace(tol,
                    offset_tol=max(1e-6, 1e-3 * extent),
                    snap_tol=max(1e-5, 1e-3 * extent))
            else:
                raise ValueError(
                    f"unknown preset '{preset}' — "
                    f"expected one of: accurate, balanced, coarse")

        if offset_tol is not None:
            tol = _dataclasses_replace(tol, offset_tol=float(offset_tol))
        if snap_tol is not None:
            tol = _dataclasses_replace(tol, snap_tol=float(snap_tol))
        if simp_tol is not None:
            tol = _dataclasses_replace(tol, simp_tol=float(simp_tol))

        welded_verts, welded_tris = _weld_vertices(node_list, raw_tris,
                                                    tol.weld_eps)

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
            v_arr, f_arr, tri_normals, effective_angle, extent, tol=tol)

        # --- Delta estimation + cylinder recovery (fix-e) ---
        delta_info = _estimate_delta(
            v_arr, f_arr, tri_normals, quant_step, extent)
        derived_eps = delta_info["epsilon"]

        recovered_patches, recovered_tris, planar_groups = (
            _recover_curved_from_small_groups(
                v_arr, f_arr, tri_normals, planar_groups,
                derived_eps, comp_ids))

        planar_tri_set: set = set(recovered_tris)
        planar_faces: List[Dict] = []
        face_idx = 0
        has_warnings = False

        for g in planar_groups:
            tri_indices = g["tri_indices"]
            for ti in tri_indices:
                planar_tri_set.add(ti)

            normal = g["normal"]

            remap = _snap_group_vertices(
                welded_verts, tri_indices, welded_tris, tol.snap_tol)
            residual_out: Dict = {}
            loops_vi = _boundary_loops(
                tri_indices, welded_tris, remap,
                tri_normals=tri_normals, group_normal=normal,
                welded_verts=welded_verts, tjunc_tol=tol.tjunc_tol,
                residual_out=residual_out)
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
                        should_merge = False
                        if _same_sign_2d(li["signed"], fg["outer"]["signed"]):
                            # R-5: same signed-area sign as outer → always
                            # merge (cannot be a hole regardless of centroids).
                            should_merge = True
                        else:
                            contains = _loop_contains_centroid(
                                li["pts_2d"], tri_centroids_2d)
                            if not contains:
                                # Opposite sign AND no triangle centroid
                                # inside → definitely a hole.
                                fg["holes"].append(li)
                            else:
                                # Opposite sign BUT centroids lie inside —
                                # could still be a true hole whose interior
                                # overlaps geometry from another face group.
                                ambiguous = _centroid_near_boundary(
                                    li["pts_2d"], tri_centroids_2d)
                                if (ambiguous
                                        and not residual_out.get(
                                            "residual_pairs")):
                                    # R-5: centroid-containment test is
                                    # unreliable — fall back to the
                                    # generalized winding number.
                                    # NOTE: winding number is ill-defined on
                                    # non-manifold meshes; residual_pairs (R-4)
                                    # is the per-group non-manifold signal
                                    # populated by _boundary_loops before
                                    # classification. When set → skip winding
                                    # number, keep centroid result (merge).
                                    probe = _compute_interior_probe_3d(
                                        li["pts_2d"], li["pts_3d"],
                                        normal, origin_3d)
                                    wn = _generalized_winding_number(
                                        probe, welded_verts, welded_tris)
                                    if wn > 0.5:
                                        should_merge = True
                                    else:
                                        fg["holes"].append(li)
                                else:
                                    should_merge = True
                        if should_merge:
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
                    keep = _simplify_2d_keep(outer_2d, tol.simp_tol)
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

                face_loops_vi = [outer["vi"]] + [h["vi"] for h in hole_list]
                unpaired_edges = _count_residual_loop_edges(
                    face_loops_vi, residual_out.get("residual_pairs", set()))
                warnings = ([{"type": "unpaired_boundary_edges",
                              "count": unpaired_edges}]
                            if unpaired_edges > 0 else [])
                if warnings:
                    has_warnings = True

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
                    "warnings": warnings,
                })
                face_idx += 1

        # R-8: Voxel/SDF fallback detection — total unpaired edge count
        # across all faces vs 5% of total triangle edges.
        if planar_faces:
            unpaired_total = sum(
                face["warnings"][0]["count"]
                for face in planar_faces
                if face.get("warnings")
            )
            n_welded_tris = len(welded_tris)
            total_tri_edges = 3 * n_welded_tris
            if total_tri_edges > 0:
                ratio = unpaired_total / total_tri_edges
                if ratio > 0.05:
                    unpaired_pct = round(
                        100.0 * unpaired_total / total_tri_edges, 1)
                    strategy_fallback_suggested = "organic"
                else:
                    strategy_fallback_suggested = None
                    unpaired_pct = None
            else:
                strategy_fallback_suggested = None
                unpaired_pct = None
        else:
            strategy_fallback_suggested = None
            unpaired_pct = None

        # Curved patches: triangles not in any planar group
        curved_patches = _extract_curved_patches(
            welded_verts, welded_tris, planar_tri_set, comp_ids, v_arr, f_arr,
            epsilon=derived_eps)

        # Append recovered cylinder patches (fix-e)
        if recovered_patches:
            patch_offset = len(curved_patches)
            for i, rp in enumerate(recovered_patches):
                rp["patch_index"] = patch_offset + i
            curved_patches.extend(recovered_patches)

        result = {
            "components_detected": n_components,
            "planar_faces": planar_faces,
            "curved_patches": curved_patches,
            "has_warnings": has_warnings,
        }
        if strategy_fallback_suggested is not None:
            result["strategy_fallback_suggested"] = strategy_fallback_suggested
            result["unpaired_pct"] = unpaired_pct
        return result
    except Exception as e:
        return {
            "components_detected": 0,
            "planar_faces": [],
            "curved_patches": [],
            "has_warnings": False,
            "error": str(e),
        }


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def analyze_mesh_data(nodes: Sequence, indices: Sequence,
                      normals: Optional[Sequence] = None,
                      angle_tolerance_deg: Optional[float] = None,
                      offset_tol: Optional[float] = None,
                      snap_tol: Optional[float] = None,
                      simp_tol: Optional[float] = None,
                      simplify_vertices: bool = True,
                      preset: Optional[str] = None) -> Dict:
    """Analyze a triangle mesh and return the measured-facts report.

    Returns a dict with keys: watertight, manifold, vertex_count,
    triangle_count, volume_cm3, bounding_box_cm, symmetry, primitive_hints,
    face_decomposition.  Never raises on empty/degenerate input; raises
    ValueError only for structurally invalid input (flat list lengths not
    divisible by 3, or triangle indices out of range).

    Args:
        angle_tolerance_deg:
            Dihedral angle threshold for planar grouping (degrees, default
            ``None`` → resolves to ``0.5`` or the preset's angle).  An
            explicit value overrides the preset.
        offset_tol:  Per-plane offset tolerance (cm, overrides defaults/presets).
        snap_tol:    Vertex-snap tolerance (cm, overrides defaults/presets).
        simp_tol:    Polygon simplification tolerance (cm, overrides defaults/presets).
        simplify_vertices:  If True, collapse nearly-collinear polygon vertices.
        preset:      Tolerance preset: ``"accurate"``, ``"balanced"``, ``"coarse"``.
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
    tol = _ToleranceConfig.from_(quant_step, extent)
    node_list, tri_list = _weld_vertices(
        node_list, tri_list, tol.weld_eps)
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

    face_decomp = decompose_mesh_faces(
        node_list, tri_list,
        angle_tolerance_deg=angle_tolerance_deg,
        simplify_vertices=simplify_vertices,
        offset_tol=offset_tol, snap_tol=snap_tol,
        simp_tol=simp_tol, preset=preset)

    return {
        "watertight": watertight,
        "manifold": manifold,
        "vertex_count": vertex_count,
        "triangle_count": triangle_count,
        "volume_cm3": abs(signed_sum),
        "bounding_box_cm": [_round3(bbox_min), _round3(bbox_max)],
        "symmetry": symmetry,
        "primitive_hints": hints,
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
