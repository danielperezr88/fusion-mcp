#!/usr/bin/env python3
"""R-10 (mesh-graph-pipeline plan todo 16): trimesh.facets cross-check
diagnostic tests.

DIAGNOSTIC ONLY: the helper lives in ``tests/trimesh_facets_diag.py`` (the
plan's "or in tests/ as a helper" option) — no production code is touched.
Discrepancies between our ``planar_faces`` group assignments and trimesh's
facet assignments are emitted via ``warnings.warn`` and the returned report
dict; the invariant checked is that our connectivity-constrained grouping
(R-2) yields AT LEAST as many planar faces as trimesh facets
(``trimesh_facet_count``), and discrepancies never fail the build.

Documented trimesh edge cases (verified against trimesh 5.0.0):
  - issue #347 — single-face facets: trimesh's ``facets`` builds connected
    components with ``min_len=2``, so a facet of one face is NEVER emitted.
    Our decompose DOES emit single-triangle planar faces; those have no
    trimesh counterpart by construction and are reported as
    ``single_face_analogues`` (expected, logged, never a failure).
  - issue #1745 — splitting with faces on a plane: facets sharing a plane
    are counted as ``plane_facet_groups`` (expected, logged).
  - degenerate input: ``mesh.facets`` failures are caught and reported via
    ``report["facets_error"]`` — the test never crashes.
"""

import os
import sys
import warnings

import numpy as np
import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from mcp_server.mesh_analysis import decompose_mesh_faces  # noqa: E402
from trimesh_facets_diag import _compare_with_trimesh_facets  # noqa: E402

# pytest may also import trimesh itself (the helper needs it to build the
# trimesh.Trimesh under comparison).
import trimesh  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic mesh fixtures (flat node/indices idiom from test_decompose_faces_fix.py)
# ---------------------------------------------------------------------------

def _unit_cube():
    nodes = [
        0, 0, 0,  1, 0, 0,  1, 1, 0,  0, 1, 0,
        0, 0, 1,  1, 0, 1,  1, 1, 1,  0, 1, 1,
    ]
    indices = [
        0, 2, 1,  0, 3, 2,
        4, 5, 6,  4, 6, 7,
        0, 1, 5,  0, 5, 4,
        3, 7, 6,  3, 6, 2,
        0, 4, 3,  3, 4, 7,
        1, 2, 6,  1, 6, 5,
    ]
    return nodes, indices


def _lone_triangle_plus_quad():
    """One isolated triangle next to a quad — the isolated triangle is a
    single-face facet analogue: trimesh's ``facets`` (min_len=2, issue
    #347) drops it, our decompose emits it as its own planar face."""
    nodes = [
        0, 0, 0,  1, 0, 0,  1, 1, 0,  0, 1, 0,
        5, 5, 0,  6, 5, 0,  5.5, 6, 0,
    ]
    indices = [
        0, 1, 2,  0, 2, 3,
        4, 5, 6,
    ]
    return nodes, indices


def _two_separated_coplanar_quads():
    """Two coplanar quads on the z=0 plane, edge-disconnected — both of
    their facets lie on one plane (trimesh issue #1745 territory)."""
    nodes = [
        0, 0, 0,  1, 0, 0,  1, 1, 0,  0, 1, 0,
        3, 0, 0,  4, 0, 0,  4, 1, 0,  3, 1, 0,
    ]
    indices = [
        0, 1, 2,  0, 2, 3,
        4, 5, 6,  4, 6, 7,
    ]
    return nodes, indices


def _degenerate_collinear():
    """All-collinear triangles — degenerate input for both decompose and
    trimesh facets."""
    nodes = [0, 0, 0,  1, 0, 0,  2, 0, 0]
    indices = [0, 1, 2]
    return nodes, indices


def _mesh_from(nodes, indices):
    """Build a raw trimesh with process=False so face indices stay in the
    input triangle space (same space decompose_mesh_faces receives)."""
    v = np.array(nodes, dtype=np.float64).reshape(-1, 3)
    f = np.array(indices, dtype=np.int64).reshape(-1, 3)
    return trimesh.Trimesh(vertices=v, faces=f, process=False)


def _run_cross_check(nodes, indices):
    """Decompose + build trimesh + run the cross-check, capturing warnings."""
    result = decompose_mesh_faces(nodes, indices)
    mesh = _mesh_from(nodes, indices)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        report = _compare_with_trimesh_facets(result, mesh)
    return result, report, [str(w.message) for w in caught]


# ---------------------------------------------------------------------------
# Main cross-check: unit cube
# ---------------------------------------------------------------------------

def test_trimesh_facets_cross_check():
    """Known mesh (unit cube): decompose -> 6 planar faces; trimesh facets
    -> 6 facets (one per cube face, each 2 triangles).  The invariant
    ``our_face_count >= trimesh_facet_count`` must hold and the cross-check
    must emit its diagnostic summary warning without any discrepancy."""
    nodes, indices = _unit_cube()
    result, report, messages = _run_cross_check(nodes, indices)

    assert len(result["planar_faces"]) == 6
    assert report["our_face_count"] == 6
    assert report["trimesh_facet_count"] == 6
    assert report["facet_sizes"] == [2] * 6
    # Connectivity constraint -> at least as many planar faces as facets.
    assert report["our_face_count"] >= report["trimesh_facet_count"]
    assert report["invariant_ok"] is True
    assert report["discrepancy_count"] == 0
    assert report["single_face_analogues"] == 0
    assert report["plane_facet_groups"] == 0
    assert report["facets_error"] is None

    # Diagnostic summary warning always emitted; no discrepancy warnings
    # (per-facet warnings carry the distinctive "discrepancy:" form).
    assert any("cross-check" in m for m in messages)
    assert not any("discrepancy:" in m for m in messages), messages


# ---------------------------------------------------------------------------
# trimesh issue #347 — single-face facets
# ---------------------------------------------------------------------------

def test_trimesh_facets_cross_check_single_face_facet_347():
    """An isolated triangle next to a quad is a single-face facet: trimesh
    ``facets`` uses ``min_len=2`` connected components so it NEVER emits it
    (issue #347).  Our decompose still emits it as one planar face, so
    ``single_face_analogues`` reports it — documented, logged, no failure."""
    nodes, indices = _lone_triangle_plus_quad()
    result, report, messages = _run_cross_check(nodes, indices)

    assert len(result["planar_faces"]) == 2
    assert report["our_face_count"] == 2
    assert report["trimesh_facet_count"] == 1   # quad only; lone tri dropped
    assert report["our_face_count"] >= report["trimesh_facet_count"]
    assert report["invariant_ok"] is True
    # The lone triangle face (triangle_count == 1) has no trimesh facet.
    assert report["single_face_analogues"] == 1
    assert any("#347" in m for m in messages), messages
    assert report["discrepancy_count"] == 0     # every trimesh facet covered
    assert report["facets_error"] is None


# ---------------------------------------------------------------------------
# trimesh issue #1745 — splitting with faces on a plane
# ---------------------------------------------------------------------------

def test_trimesh_facets_cross_check_faces_on_plane_1745():
    """Two coplanar quads both lying on the z=0 plane produce two trimesh
    facets that share a plane (issue #1745 territory).  They are counted as
    ``plane_facet_groups`` and logged as expected — never a failure."""
    nodes, indices = _two_separated_coplanar_quads()
    result, report, messages = _run_cross_check(nodes, indices)

    assert len(result["planar_faces"]) == 2
    assert report["our_face_count"] == 2
    assert report["trimesh_facet_count"] == 2
    assert report["our_face_count"] >= report["trimesh_facet_count"]
    assert report["invariant_ok"] is True
    assert report["plane_facet_groups"] >= 1
    assert any("#1745" in m for m in messages), messages
    assert report["discrepancy_count"] == 0
    assert report["facets_error"] is None


# ---------------------------------------------------------------------------
# Degenerate input — graceful handling, never a crash
# ---------------------------------------------------------------------------

def test_trimesh_facets_cross_check_degenerate_graceful():
    """All-collinear degenerate mesh: decompose emits zero planar faces and
    trimesh facets returns zero facets — the cross-check reports the empty
    comparison and logs it, without raising."""
    nodes, indices = _degenerate_collinear()
    result, report, messages = _run_cross_check(nodes, indices)

    assert result["planar_faces"] == []
    assert report["our_face_count"] == 0
    assert report["trimesh_facet_count"] == 0
    assert report["invariant_ok"] is True      # 0 >= 0 holds
    assert report["discrepancy_count"] == 0
    assert report["facets_error"] is None
    assert any("cross-check" in m for m in messages), messages


def test_trimesh_facets_cross_check_facets_error_graceful(monkeypatch):
    """If ``mesh.facets`` raises (older trimesh builds on degenerate
    input), the helper must catch it, report ``facets_error``, emit a
    warning, and return a report — never propagate the exception."""
    nodes, indices = _unit_cube()
    result = decompose_mesh_faces(nodes, indices)
    mesh = _mesh_from(nodes, indices)

    def _boom(_self):
        raise RuntimeError("facets failed on degenerate input")

    # `facets` is a property; replacing it with a plain function would make
    # `mesh.facets` evaluate to the bound method (never called), so patch
    # with a property whose getter raises.
    monkeypatch.setattr(type(mesh), "facets", property(_boom))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        report = _compare_with_trimesh_facets(result, mesh)

    assert report["facets_error"] is not None
    assert "facets failed on degenerate input" in report["facets_error"]
    assert report["trimesh_facet_count"] == 0
    assert report["invariant_ok"] is True      # our_faces >= 0 trivially
    assert any("unavailable" in str(w.message) for w in caught), [
        str(w.message) for w in caught]
