#!/usr/bin/env python3
"""Headless tests for mcp_server.mesh_slicer (mesh-to-parametric plan, Todo 3).

`slice_mesh_at()` intersects a triangle mesh with an axis-aligned plane and
returns ordered, closed 2D loops (outer = CCW / positive shoelace area, hole =
CW / negative), with the plane definition (axis + height + origin + basis).

The math is stdlib-only (no numpy) and fully deterministic (no random module),
so identical input always yields identical loops.  Fusion's displayMesh is
NON-INDEXED (every triangle corner a distinct node) and triangulates each
quad face into 2 triangles, so a cube slice starts as 8 raw points (4 corners
+ 4 collinear edge midpoints) and must be simplified back down to the 4
corners.
"""

import math
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mcp_server.mesh_slicer import scale_slice, slice_mesh_at


# ---------------------------------------------------------------------------
# Synthetic fixtures (all in cm)
# ---------------------------------------------------------------------------

def _cube(corner, size):
    """Outward-wound cube [corner, corner+size]^3 as flat node/index lists."""
    x, y, z = corner
    nodes = [
        x, y, z,      x + size, y, z,      x + size, y + size, z,      x, y + size, z,
        x, y, z + size, x + size, y, z + size, x + size, y + size, z + size,
        x, y + size, z + size,
    ]
    indices = [
        # -z
        0, 2, 1,  0, 3, 2,
        # +z
        4, 5, 6,  4, 6, 7,
        # -y
        0, 1, 5,  0, 5, 4,
        # +y
        3, 7, 6,  3, 6, 2,
        # -x
        0, 4, 3,  3, 4, 7,
        # +x
        1, 2, 6,  1, 6, 5,
    ]
    return nodes, indices


def _unit_cube():
    return _cube((0.0, 0.0, 0.0), 1.0)


def _hollow_box():
    """Outer unit cube + inner 0.5 cube at [0.25, 0.75]^3, both outward-wound."""
    on, oi = _cube((0.0, 0.0, 0.0), 1.0)
    in_nodes, in_idx = _cube((0.25, 0.25, 0.25), 0.5)
    in_idx = [i + 8 for i in in_idx]  # inner cube nodes are indices 8..15
    return on + in_nodes, oi + in_idx


def _signed_area(pts):
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return 0.5 * s


def _cyclic_rotation_ok(pts, expected, tol=1e-6):
    """True if pts is a cyclic rotation of expected or its reverse (either
    walk direction).  Orientation is asserted separately via signed area."""
    if len(pts) != len(expected):
        return False
    n = len(pts)
    variants = [expected, list(reversed(expected))]
    for base in variants:
        for shift in range(n):
            cand = base[shift:] + base[:shift]
            if all(abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol
                   for a, b in zip(cand, pts)):
                return True
    return False


# ---------------------------------------------------------------------------
# Happy path: unit cube at mid-height
# ---------------------------------------------------------------------------

def test_cube_midheight_z_single_ccw_loop():
    nodes, indices = _unit_cube()
    res = slice_mesh_at(nodes, indices, {"axis": "Z", "height_cm": 0.5})
    assert len(res["loops"]) == 1
    loop = res["loops"][0]
    assert loop["is_hole"] is False
    # exactly the 4 corners - the collinear edge midpoints must be simplified
    assert len(loop["pts"]) == 4
    expected = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    assert _cyclic_rotation_ok(loop["pts"], expected)
    # closed chain without repeating the first point
    assert loop["pts"][0] != loop["pts"][-1]
    # CCW outer = positive shoelace area
    assert _signed_area(loop["pts"]) > 0
    # plane definition is returned
    assert res["plane"]["axis"] == "Z"
    assert res["plane"]["height_cm"] == pytest.approx(0.5)
    assert res["plane"]["origin"] == pytest.approx([0.0, 0.0, 0.5])
    assert [u for u in res["plane"]["basis"][0]] == pytest.approx([1.0, 0.0, 0.0])
    assert [u for u in res["plane"]["basis"][1]] == pytest.approx([0.0, 1.0, 0.0])


def test_cube_numeric_height_defaults_to_z():
    nodes, indices = _unit_cube()
    res = slice_mesh_at(nodes, indices, 0.5)
    assert len(res["loops"]) == 1
    assert len(res["loops"][0]["pts"]) == 4
    assert res["plane"]["axis"] == "Z"


def test_cube_slice_along_x_and_y():
    nodes, indices = _unit_cube()
    rx = slice_mesh_at(nodes, indices, {"axis": "X", "height_cm": 0.5})
    assert len(rx["loops"]) == 1 and len(rx["loops"][0]["pts"]) == 4
    assert rx["plane"]["origin"] == pytest.approx([0.5, 0.0, 0.0])
    ry = slice_mesh_at(nodes, indices, {"axis": "Y", "height_cm": 0.5})
    assert len(ry["loops"]) == 1 and len(ry["loops"][0]["pts"]) == 4
    assert ry["plane"]["origin"] == pytest.approx([0.0, 0.5, 0.0])


# ---------------------------------------------------------------------------
# Hollow box: outer loop + inner hole
# ---------------------------------------------------------------------------

def test_hollow_box_outer_loop_and_hole():
    nodes, indices = _hollow_box()
    res = slice_mesh_at(nodes, indices, {"axis": "Z", "height_cm": 0.5})
    assert len(res["loops"]) == 2
    holes = [l for l in res["loops"] if l["is_hole"]]
    outers = [l for l in res["loops"] if not l["is_hole"]]
    assert len(holes) == 1
    assert len(outers) == 1
    assert _cyclic_rotation_ok(outers[0]["pts"],
                               [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    assert _cyclic_rotation_ok(holes[0]["pts"],
                               [[0.25, 0.25], [0.75, 0.25],
                                [0.75, 0.75], [0.25, 0.75]])
    assert _signed_area(outers[0]["pts"]) > 0   # CCW outer
    assert _signed_area(holes[0]["pts"]) < 0    # CW hole
    assert outers[0]["pts"][0] != outers[0]["pts"][-1]
    assert holes[0]["pts"][0] != holes[0]["pts"][-1]


# ---------------------------------------------------------------------------
# Above the mesh / degenerate / empty
# ---------------------------------------------------------------------------

def test_slice_above_mesh_empty():
    nodes, indices = _unit_cube()
    res = slice_mesh_at(nodes, indices, {"axis": "Z", "height_cm": 2.0})
    assert res["loops"] == []
    assert res["plane"]["axis"] == "Z"


def test_coplanar_triangle_skipped():
    """A triangle lying entirely in the slice plane is skipped."""
    nodes = [0, 0, 0.5,  1, 0, 0.5,  0, 1, 0.5]
    indices = [0, 1, 2]
    res = slice_mesh_at(nodes, indices, 0.5)
    assert res["loops"] == []


def test_touch_vertex_triangle_skipped():
    """Triangle touching the plane at exactly one vertex (others same side)
    yields no spurious points."""
    nodes = [0, 0, 0.5,  1, 0, 0.7,  0, 1, 0.7]
    indices = [0, 1, 2]
    res = slice_mesh_at(nodes, indices, 0.5)
    assert res["loops"] == []


def test_degenerate_zero_area_triangle_skipped():
    """A zero-area (degenerate) triangle -- two coincident vertices -- is
    skipped without crashing: its two plane crossings collapse to ONE point,
    the zero-length segment is deduped away, and no spurious loop appears.
    Mixed into a real mesh it must leave the slice byte-identical."""
    # degenerate: vertices 0 and 1 coincide; the plane at z=0.5 cuts both
    # remaining edges at the same point (0.5, 0, 0.5).
    nodes = [0, 0, 0,  0, 0, 0,  1, 0, 1]
    indices = [0, 1, 2]
    res = slice_mesh_at(nodes, indices, 0.5)
    assert res["loops"] == []

    cube_nodes, cube_indices = _unit_cube()
    mixed_nodes = cube_nodes + [0, 0, 0,  0, 0, 0,  1, 0, 1]
    mixed_indices = list(cube_indices) + [8, 9, 10]
    clean = slice_mesh_at(cube_nodes, cube_indices, 0.5)
    mixed = slice_mesh_at(mixed_nodes, mixed_indices, 0.5)
    assert mixed == clean


def test_empty_mesh_no_crash():
    res = slice_mesh_at([], [], {"axis": "Z", "height_cm": 0.5})
    assert res["loops"] == []
    assert res["plane"]["axis"] == "Z"
    assert res["plane"]["height_cm"] == pytest.approx(0.5)


def test_out_of_range_index_raises_value_error():
    nodes, indices = _unit_cube()
    bad = list(indices)
    bad[0] = 999
    with pytest.raises(ValueError):
        slice_mesh_at(nodes, bad, 0.5)


def test_invalid_plane_raises_value_error():
    with pytest.raises(ValueError):
        slice_mesh_at([0, 0, 0,  1, 0, 0,  1, 1, 0], [0, 1, 2], "not-a-plane")


# ---------------------------------------------------------------------------
# Determinism / input forms / scaling
# ---------------------------------------------------------------------------

def test_deterministic_repeatable():
    nodes, indices = _hollow_box()
    a = slice_mesh_at(nodes, indices, {"axis": "Z", "height_cm": 0.5})
    b = slice_mesh_at(nodes, indices, {"axis": "Z", "height_cm": 0.5})
    assert a == b


def test_flat_and_nested_inputs_match():
    nodes, indices = _unit_cube()
    node_t = [[nodes[i], nodes[i + 1], nodes[i + 2]]
              for i in range(0, len(nodes), 3)]
    idx_t = [[indices[i], indices[i + 1], indices[i + 2]]
             for i in range(0, len(indices), 3)]
    a = slice_mesh_at(nodes, indices, 0.5)
    b = slice_mesh_at(node_t, idx_t, 0.5)
    assert a == b


def test_scale_slice_mm():
    nodes, indices = _unit_cube()
    res = slice_mesh_at(nodes, indices, {"axis": "Z", "height_cm": 0.5})
    scaled = scale_slice(res, 10.0)
    assert _cyclic_rotation_ok(scaled["loops"][0]["pts"],
                               [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]])
    assert scaled["plane"]["height_cm"] == pytest.approx(5.0)
    assert scaled["plane"]["origin"] == pytest.approx([0.0, 0.0, 5.0])
    # is_hole / structure untouched
    assert scaled["loops"][0]["is_hole"] == res["loops"][0]["is_hole"]
