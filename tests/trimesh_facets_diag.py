#!/usr/bin/env python3
"""Test-only diagnostic: cross-check ``decompose_mesh_faces`` planar faces
against trimesh's ``.facets`` (mesh-graph-pipeline plan todo 16 / R-10).
Lives under ``tests/`` per the plan's L210 tests/-helper option; no
production code is touched.

trimesh facets are the ``min_len=2`` connected components of the
parallel-adjacency graph, where adjacent faces are parallel when
``(radius/span)**2 > 5000`` (trimesh/graph.py, ``tol.facet_threshold``).
DIAGNOSTIC ONLY: discrepancies are returned in the report dict and emitted
via ``warnings.warn``; ``mesh.facets`` failures on degenerate input are
caught and reported via ``report["facets_error"]``.
"""

from __future__ import annotations

import warnings
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import trimesh

# trimesh's parallel-adjacency ratio predicate (trimesh/graph.py facets).
FACET_THRESHOLD = 5000.0

# Min |normal dot| for a facet triangle to lie in a face's plane (cos 60 deg:
# coplanar faces match ~1.0, cube faces differ by 0.0, 0.5 separates them).
_NORMAL_MATCH_COS = 0.5

# Cap on per-facet discrepancy warnings emitted per comparison.
_WARN_CAP = 10

# decompose_mesh_faces emits face polygon vertices rounded to 6 decimals.
_VERTEX_ROUND = 6

_V3 = Tuple[float, float, float]


def _drop_axis(normal: np.ndarray) -> int:
    return int(np.argmax(np.abs(normal)))


def _project_drop(points_3d: np.ndarray, drop: int) -> np.ndarray:
    keep = [k for k in range(3) if k != drop]
    return np.asarray(points_3d, dtype=np.float64)[:, keep]


def _polygon_area_2d(poly: np.ndarray) -> float:
    x = poly[:, 0]
    y = poly[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - y * np.roll(x, -1)))


def _point_in_polygon_2d(pt: np.ndarray, poly: np.ndarray) -> bool:
    """Even-odd ray cast; degenerate (zero-area) polygons contain nothing."""
    if len(poly) < 3 or abs(_polygon_area_2d(poly)) < 1e-12:
        return False
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > pt[1]) != (y2 > pt[1]):   # strict => y1 != y2, no div-by-0
            x_cross = x1 + (pt[1] - y1) * (x2 - x1) / (y2 - y1)
            if pt[0] < x_cross:
                inside = not inside
    return inside


def _face_covering_indices(centroid: np.ndarray,
                           facet_normal: np.ndarray,
                           planar_faces: Sequence[dict]) -> List[int]:
    """Our-face indices whose polygon contains *centroid*; emission order."""
    hits: List[int] = []
    for fi, face in enumerate(planar_faces):
        fn = np.asarray(face.get("normal"), dtype=np.float64)
        if fn.shape != (3,) or not np.isfinite(fn).all():
            continue
        fnn = float(np.linalg.norm(fn))
        if fnn < 1e-12:
            continue
        if abs(float(np.dot(fn / fnn, facet_normal))) < _NORMAL_MATCH_COS:
            continue
        poly_3d = np.asarray(face.get("vertices", []), dtype=np.float64)
        if len(poly_3d) < 3:
            continue
        # 2D projection collapses the plane offset, so parallel faces at
        # different offsets project identically — also require the centroid
        # to lie ON this face's plane (rounding-aware tolerance).
        offset = float(np.dot(fn / fnn, poly_3d[0]))
        plane_tol = max(1e-4, 10.0 ** (-_VERTEX_ROUND) * abs(offset))
        if abs(float(np.dot(fn / fnn, centroid)) - offset) > plane_tol:
            continue
        drop = _drop_axis(fn)
        poly_2d = _project_drop(poly_3d, drop)
        pt_2d = np.array([centroid[k] for k in range(3) if k != drop])
        if _point_in_polygon_2d(pt_2d, poly_2d):
            hits.append(fi)
    return hits


def _tri_normals(mesh: trimesh.Trimesh) -> Optional[np.ndarray]:
    """Per-face unit normals, computed directly (no trimesh processing side
    effects; process=False meshes keep raw indexing), or None if unusable."""
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    if len(faces) == 0 or vertices.shape[1] != 3:
        return None
    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]
    crosses = np.cross(v1 - v0, v2 - v0)
    norms = np.linalg.norm(crosses, axis=1)
    safe = np.where(norms > 1e-12, norms, 1.0)
    return crosses / safe[:, None]


def _facet_normal(tri_normals: np.ndarray, facet: np.ndarray) -> Optional[np.ndarray]:
    vals = tri_normals[np.asarray(facet, dtype=np.int64)]
    vals = vals[np.isfinite(vals).all(axis=1)]
    if len(vals) == 0:
        return None
    mean = np.mean(vals, axis=0)
    norm = float(np.linalg.norm(mean))
    return mean / norm if norm > 1e-12 else None


def _emit_report_warnings(report: dict) -> None:
    extra = ""
    if report["facets_error"]:
        extra = " facets_error=%r" % report["facets_error"]
    warnings.warn(
        "trimesh facets cross-check: our_faces=%d trimesh_facets=%d "
        "invariant_ok=%s discrepancies=%d%s"
        % (report["our_face_count"], report["trimesh_facet_count"],
           report["invariant_ok"], report["discrepancy_count"], extra),
        stacklevel=2)
    if report["facets_error"]:
        warnings.warn(
            "trimesh facets cross-check: facets unavailable, skipped: %s"
            % report["facets_error"], stacklevel=2)
    if not report["invariant_ok"]:
        warnings.warn(
            "trimesh facets cross-check: INVARIANT VIOLATED - our %d planar "
            "faces < trimesh %d facets (diagnostic only)"
            % (report["our_face_count"], report["trimesh_facet_count"]),
            stacklevel=2)
    if report["single_face_analogues"]:
        warnings.warn(
            "trimesh facets cross-check: %d single-face facet analogue(s) "
            "with no trimesh facet - issue #347 (min_len=2 drops them), "
            "expected" % report["single_face_analogues"], stacklevel=2)
    if report["plane_facet_groups"]:
        warnings.warn(
            "trimesh facets cross-check: %d plane(s) split into multiple "
            "trimesh facets - issue #1745, expected"
            % report["plane_facet_groups"], stacklevel=2)
    for d in report["discrepancies"][:_WARN_CAP]:
        warnings.warn(
            "trimesh facets cross-check: facet %d (size %d) discrepancy: %s"
            % (d["facet_index"], d["size"], d["note"]), stacklevel=2)
    remaining = report["discrepancy_count"] - _WARN_CAP
    if remaining > 0:
        warnings.warn(
            "trimesh facets cross-check: %d more discrepancy(ies) not shown"
            % remaining, stacklevel=2)


def _compare_with_trimesh_facets(decompose_result: dict,
                                 mesh: trimesh.Trimesh) -> Dict[str, object]:
    """Compare our planar-face group assignments with trimesh's facets.

    Runs ``mesh.facets`` (the ``(radius/span)**2 > 5000`` parallel-adjacency
    components), maps each facet's triangles onto our planar faces by plane
    + centroid-in-polygon containment, and reports discrepancies (a facet
    not covered by exactly one of our faces).  Never raises; ``mesh.facets``
    failures are caught and surfaced as ``report["facets_error"]``.

    Returns a report dict: our_face_count, trimesh_facet_count,
    invariant_ok, facets_error, facet_sizes, our_face_triangle_counts,
    single_face_analogues, plane_facet_groups, discrepancies (list of
    {facet_index, size, matched_triangles, covering_faces, note}),
    discrepancy_count — and emits warnings.warn diagnostics.
    """
    planar_faces: List[dict] = list(decompose_result.get("planar_faces") or [])
    our_face_count = len(planar_faces)
    report: Dict[str, object] = {
        "our_face_count": our_face_count,
        "trimesh_facet_count": 0,
        "invariant_ok": True,
        "facets_error": None,
        "facet_sizes": [],
        "our_face_triangle_counts":
            [int(f.get("triangle_count", 0)) for f in planar_faces],
        "single_face_analogues": 0,
        "plane_facet_groups": 0,
        "discrepancies": [],
        "discrepancy_count": 0,
    }

    try:
        facets_list = list(mesh.facets)
    except Exception as e:                      # noqa: BLE001 - 3rd-party
        report["facets_error"] = "%s: %s" % (type(e).__name__, e)
        _emit_report_warnings(report)
        return report

    report["facet_sizes"] = [int(len(f)) for f in facets_list]
    report["trimesh_facet_count"] = len(facets_list)
    report["invariant_ok"] = our_face_count >= len(facets_list)

    if not facets_list:
        report["discrepancies"] = []
        report["discrepancy_count"] = 0
        report["single_face_analogues"] = _single_face_analogue_count(
            planar_faces, frozenset())
        _emit_report_warnings(report)
        return report

    try:
        tri_normals = _tri_normals(mesh)
        vertices = np.asarray(mesh.vertices, dtype=np.float64)
        faces_arr = np.asarray(mesh.faces, dtype=np.int64)
    except Exception as e:                      # noqa: BLE001 - 3rd-party
        report["facets_error"] = "%s: %s" % (type(e).__name__, e)
        _emit_report_warnings(report)
        return report

    discrepancies: List[dict] = []
    matched_face_idxs: set = set()
    planes: Dict[_V3, int] = {}

    for i, facet in enumerate(facets_list):
        tri_idx = [int(t) for t in facet]
        fn = _facet_normal(tri_normals, facet)
        if fn is None:
            discrepancies.append({
                "facet_index": i, "size": len(tri_idx),
                "matched_triangles": 0, "covering_faces": [],
                "note": "facet has no usable triangle normal",
            })
            continue
        key = tuple(round(float(c), 3) for c in fn)
        planes[key] = planes.get(key, 0) + 1

        covering: set = set()
        matched = 0
        for ti in tri_idx:
            if ti >= len(faces_arr):
                continue
            a, b, c = faces_arr[ti]
            centroid = (vertices[a] + vertices[b] + vertices[c]) / 3.0
            hits = _face_covering_indices(centroid, fn, planar_faces)
            if hits:
                matched += 1
                covering.update(hits)
        matched_face_idxs.update(covering)

        note_parts: List[str] = []
        if matched < len(tri_idx):
            note_parts.append("%d of %d triangles unmatched"
                              % (len(tri_idx) - matched, len(tri_idx)))
        if len(covering) != 1:
            note_parts.append("covered by %d our-face(s): %s (expected exactly 1)"
                              % (len(covering), sorted(covering)))
        if note_parts:
            discrepancies.append({
                "facet_index": i, "size": len(tri_idx),
                "matched_triangles": matched,
                "covering_faces": sorted(covering),
                "note": "; ".join(note_parts),
            })

    report["discrepancies"] = discrepancies
    report["discrepancy_count"] = len(discrepancies)
    report["single_face_analogues"] = _single_face_analogue_count(
        planar_faces, matched_face_idxs)
    # trimesh issue #1745: faces lying on one common plane can split into
    # several facets — counted as expected, never a failure.
    report["plane_facet_groups"] = sum(
        1 for count in planes.values() if count >= 2)
    _emit_report_warnings(report)
    return report


def _single_face_analogue_count(planar_faces: Sequence[dict],
                                matched_face_idxs: set) -> int:
    # trimesh issue #347: facets are min_len=2 components, so a single-face
    # facet is never emitted — our single-triangle faces (triangle_count==1)
    # with no trimesh counterpart are expected, reported, never a failure.
    return sum(
        1 for fi, f in enumerate(planar_faces)
        if int(f.get("triangle_count", 0)) == 1 and fi not in matched_face_idxs)
