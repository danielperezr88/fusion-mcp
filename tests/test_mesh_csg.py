#!/usr/bin/env python3
"""Headless tests for mcp_server.mesh_csg (salvaged primitives post-dismantle).

`compute_revolved_profile(mesh_data, params)` produces the HALF cross-section
profile (x>=0 half) for revolve operations.

`scale_tree(tree, factor)` scales a CSG tree's dimensional params by a factor.

All math is stdlib-only and deterministic; mesh data is in cm.
"""

import math
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mcp_server.mesh_csg import (
    compute_revolved_profile,
    scale_tree,
)


# ---------------------------------------------------------------------------
# Synthetic fixtures (all in cm)
# ---------------------------------------------------------------------------

def _box(corner, dims):
    """Outward-wound box [corner, corner+dims] as flat node/index lists."""
    x, y, z = corner
    dx, dy, dz = dims
    nodes = [
        x, y, z,  x + dx, y, z,  x + dx, y + dy, z,  x, y + dy, z,
        x, y, z + dz,  x + dx, y, z + dz,  x + dx, y + dy, z + dz,
        x, y + dy, z + dz,
    ]
    indices = [
        0, 2, 1,  0, 3, 2,        # -z
        4, 5, 6,  4, 6, 7,        # +z
        0, 1, 5,  0, 5, 4,        # -y
        3, 7, 6,  3, 6, 2,        # +y
        0, 4, 3,  3, 4, 7,        # -x
        1, 2, 6,  1, 6, 5,        # +x
    ]
    return nodes, indices


def _cylinder(r=1.0, h=2.0, n=24):
    """Faceted cylinder centered on Z, base at z=0, height h.

    Vertices are rotated by half a facet (pi/n) so NO vertex lies exactly on
    the Y=0 plane -- the revolve profile slice then cuts through the two
    opposite side faces instead of grazing a shared edge (the slicer skips
    coplanar/single-vertex-touch triangles)."""
    nodes = []
    for i in range(n):
        a = 2.0 * math.pi * i / n + math.pi / n
        nodes += [r * math.cos(a), r * math.sin(a), 0.0]
    for i in range(n):
        a = 2.0 * math.pi * i / n + math.pi / n
        nodes += [r * math.cos(a), r * math.sin(a), h]
    nodes += [0.0, 0.0, 0.0, 0.0, 0.0, h]
    indices = []
    for i in range(n):
        j = (i + 1) % n
        indices += [i, j, n + j,  i, n + j, n + i]
    for i in range(n):
        j = (i + 1) % n
        indices += [2 * n, j, i]
        indices += [2 * n + 1, n + i, n + j]
    return nodes, indices


def _mesh_data(nodes, indices):
    return {"nodes": nodes, "indices": indices}


# ---------------------------------------------------------------------------
# revolved half-profile
# ---------------------------------------------------------------------------

def test_revolved_profile_cylinder_half_plane():
    """Cylinder mesh -> half cross-section profile: 4 pts, all x >= 0,
    spanning z in [0, 2] (the Z-containing plane is the Y=0 plane).  The
    fixture's vertices are rotated half a facet, so the plane cuts the side
    faces at x = cos(pi/24) ~= 0.991445."""
    nodes, indices = _cylinder(r=1.0, h=2.0)
    profile = compute_revolved_profile(_mesh_data(nodes, indices), {})
    assert isinstance(profile, list) and len(profile) >= 3, profile
    for x, z in profile:
        assert x >= -1e-6, profile  # half-plane constraint (x >= 0 half)
    assert max(z for _, z in profile) == pytest.approx(2.0, abs=1e-6)
    assert min(z for _, z in profile) == pytest.approx(0.0, abs=1e-6)
    half_x = round(math.cos(math.pi / 24), 6)
    expected = {(0.0, 0.0), (half_x, 0.0), (half_x, 2.0), (0.0, 2.0)}
    got = {(round(x, 6), round(z, 6)) for x, z in profile}
    assert got == expected, profile


def test_revolved_profile_requires_intersection():
    """A mesh that never crosses the Z-containing plane -> graceful ValueError."""
    nodes, indices = _box((5.0, 5.0, 0.0), (1.0, 1.0, 1.0))
    with pytest.raises(ValueError):
        compute_revolved_profile(_mesh_data(nodes, indices), {})


# ---------------------------------------------------------------------------
# scale_tree (server tool: cm tree -> requested units)
# ---------------------------------------------------------------------------

def test_scale_tree_scales_dimensional_params():
    """scale_tree scales dimensional params across tree kinds:
    linear_extrude height, polygon pts, translate offset, cube size."""
    # Hand-constructed prismatic tree (was build_csg_tree, now built inline)
    prismatic = [{
        "kind": "linear_extrude",
        "params": {"height": 1.0},
        "children": [{
            "kind": "polygon",
            "params": {"pts": [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]},
        }],
    }]
    scaled = scale_tree(prismatic, 10.0)  # cm -> mm
    assert scaled is not prismatic
    root = scaled[0]
    assert root["params"]["height"] == pytest.approx(10.0, abs=1e-6)
    poly = root["children"][0]
    pts_set = {(round(p[0], 6), round(p[1], 6)) for p in poly["params"]["pts"]}
    assert pts_set == {(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)}

    # Hand-constructed csg_decompose tree (two-box union)
    csg_tree = [{
        "kind": "union",
        "children": [
            {
                "kind": "translate",
                "params": {"0": [1.0, 1.0, 1.0]},
                "children": [{
                    "kind": "rotate",
                    "params": {"deg": [0, 0, 0]},
                    "children": [{
                        "kind": "cube",
                        "params": {"center": True, "size": [2.0, 2.0, 2.0]},
                    }],
                }],
            },
        ],
    }]
    dscaled = scale_tree(csg_tree, 2.0)
    trans = dscaled[0]["children"][0]
    assert trans["params"]["0"] == [2.0, 2.0, 2.0]
    cube = trans["children"][0]["children"][0]
    assert [round(v, 6) for v in cube["params"]["size"]] == [4.0, 4.0, 4.0]
