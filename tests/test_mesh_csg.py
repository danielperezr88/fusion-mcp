#!/usr/bin/env python3
"""Headless tests for mcp_server.mesh_csg (mesh-to-parametric plan, Todo 6).

`build_csg_tree(mesh_data, strategy, params)` turns a triangle mesh into a
CSG tree consumable DIRECTLY by `scad_translator.translate_to_fusion_commands`:

  * prismatic  -- slice the mesh at N heights along an axis, verify the
    cross-section is constant (loop-shape similarity), emit ONE
    `linear_extrude` whose `polygon` child carries the slice-loop pts
    (holes -> `paths`), wrapped in a `translate` when the mesh is offset
    along the axis.
  * csg_decompose -- planar region growing over the welded topology, then
    box fitting (3 orthogonal plane-pair extents) / cylinder fitting per
    cluster; emits a `union` tree with per-primitive `translate`+`rotate`.

`compute_revolved_profile(mesh_data, params)` produces the HALF cross-section
profile (x>=0 half) for the `revolved` strategy -- it is NOT a CSG tree; it
feeds the `revolve_cross_section` Fusion handler.

NOTE on the plan's prismatic tree sketch (line 114): the plan's literal
`{"kind": "polygon", ..., "children": [{"kind": "linear_extrude", ...}]}`
nesting is NOT a valid input to `translate_to_fusion_commands` -- the
translator's validation rejects top-level 2D primitives ("2D primitive
requires linear_extrude or rotate_extrude parent", pinned by
test_top_level_2d_polygon_requires_extrude_parent).  The functional contract
(a polygon profile + a linear_extrude, pts matching the slice loops) is
emitted in the translator-accepted shape: `linear_extrude` root whose child is
the `polygon`.  The tree still validates headlessly via
`translate_to_fusion_commands(tree, TrapRoot(), ...)` (see
test_prismatic_tree_passes_translator_validation).

All math is stdlib-only and deterministic; mesh data is in cm.
"""

import math
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from conftest import TrapRoot

from mcp_server.mesh_csg import (  # noqa: E402  (import fails pre-implementation)
    NotPrismaticError,
    UnsupportedStrategyError,
    build_csg_tree,
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


def _unit_cube():
    return _box((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))


def _hollow_box():
    """Outer unit cube + inner 0.5 square tube spanning the FULL height (a
    through-hole: every interior cross-section is a square ring -> prismatic
    with a hole)."""
    on, oi = _box((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
    in_nodes, in_idx = _box((0.25, 0.25, 0.0), (0.5, 0.5, 1.0))
    in_idx = [i + 8 for i in in_idx]
    return on + in_nodes, oi + in_idx


def _two_boxes():
    """Two DISJOINT axis-aligned boxes (csg_decompose target)."""
    n1, i1 = _box((0.0, 0.0, 0.0), (2.0, 2.0, 2.0))
    n2, i2 = _box((2.5, 0.0, 0.0), (2.0, 2.0, 2.0))
    i2 = [i + 8 for i in i2]
    return n1 + n2, i1 + i2


def _pyramid():
    """Square pyramid: base [0,1]^2 at z=0, apex (0.5,0.5,1) (varying slice)."""
    nodes = [0, 0, 0,  1, 0, 0,  1, 1, 0,  0, 1, 0,  0.5, 0.5, 1]
    indices = [
        0, 2, 1,  0, 3, 2,   # base (-z)
        0, 1, 4,  1, 2, 4,   # front/right
        2, 3, 4,  3, 0, 4,   # back/left
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


def _pt_set(pts, tol_round=6):
    """Order-invariant point set: the slicer's CCW-forced loops may start at
    any corner, so shape is asserted as a set (plus orientation separately)."""
    return {(round(p[0], tol_round), round(p[1], tol_round)) for p in pts}


def _loop_area(pts):
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return 0.5 * s


# ---------------------------------------------------------------------------
# prismatic
# ---------------------------------------------------------------------------

def test_prismatic_cube_tree_polygon_with_linear_extrude():
    """A cube mesh -> linear_extrude root whose polygon child pts EXACTLY match
    the T3 slice loop (4-pt CCW square; loop start is slicer-deterministic so
    the shape is asserted order-invariantly), height = z-extent."""
    nodes, indices = _unit_cube()
    tree = build_csg_tree(_mesh_data(nodes, indices), "prismatic", {})
    assert len(tree) == 1, tree
    root = tree[0]
    assert root["kind"] == "linear_extrude", root
    assert root["params"]["height"] == pytest.approx(1.0, abs=1e-6)
    assert len(root["children"]) == 1
    poly = root["children"][0]
    assert poly["kind"] == "polygon", poly
    pts = poly["params"]["pts"]
    assert len(pts) == 4, pts
    assert _pt_set(pts) == {(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)}
    assert _loop_area(pts) > 0.0  # outer loop forced CCW
    assert "paths" not in poly["params"]  # no holes -> no paths


def test_prismatic_constant_cross_section_at_multiple_heights():
    """A taller box (1x1x2) slices to the same square at 3 interior heights ->
    prismatic succeeds with height 2.0 (loop-shape similarity is verified)."""
    nodes, indices = _box((0.0, 0.0, 0.0), (1.0, 1.0, 2.0))
    tree = build_csg_tree(_mesh_data(nodes, indices), "prismatic",
                          {"num_slices": 3})
    root = tree[0]
    assert root["kind"] == "linear_extrude"
    assert root["params"]["height"] == pytest.approx(2.0, abs=1e-6)
    poly = root["children"][0]
    assert poly["kind"] == "polygon"
    assert _pt_set(poly["params"]["pts"]) == {
        (0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)}


def test_prismatic_offset_box_is_translated():
    """A box whose base is NOT at z=0 is wrapped in translate[0,0,zmin] so the
    extruded body lands on the mesh (position fidelity).  The polygon pts stay
    ABSOLUTE in x,y (slice loops are absolute), so the translate only carries
    the axis coordinate."""
    nodes, indices = _box((1.0, 2.0, 3.0), (1.0, 1.0, 2.0))
    tree = build_csg_tree(_mesh_data(nodes, indices), "prismatic", {})
    assert len(tree) == 1
    root = tree[0]
    assert root["kind"] == "translate", root
    assert root["params"]["0"] == [0.0, 0.0, 3.0]
    child = root["children"][0]
    assert child["kind"] == "linear_extrude"
    assert child["params"]["height"] == pytest.approx(2.0, abs=1e-6)
    poly = child["children"][0]
    assert poly["kind"] == "polygon"
    assert _pt_set(poly["params"]["pts"]) == {
        (1.0, 2.0), (2.0, 2.0), (2.0, 3.0), (1.0, 3.0)}


def test_prismatic_hollow_box_holes_become_paths():
    """A hollow box (outer + inner cube) -> polygon with pts = outer+inner
    loops and paths = [outer ring, inner ring] (holes -> paths)."""
    nodes, indices = _hollow_box()
    tree = build_csg_tree(_mesh_data(nodes, indices), "prismatic", {})
    root = tree[0]
    assert root["kind"] == "linear_extrude"
    poly = root["children"][0]
    assert poly["kind"] == "polygon"
    params = poly["params"]
    assert len(params["pts"]) == 8, params["pts"]  # 4 outer + 4 inner
    assert params["paths"] == [[0, 1, 2, 3], [4, 5, 6, 7]], params
    # outer ring is CCW (positive area), hole ring CW (negative area)
    def area(loop):
        s = 0.0
        n = len(loop)
        for i in range(n):
            x1, y1 = loop[i]
            x2, y2 = loop[(i + 1) % n]
            s += x1 * y2 - x2 * y1
        return 0.5 * s
    outer = [params["pts"][i] for i in params["paths"][0]]
    inner = [params["pts"][i] for i in params["paths"][1]]
    assert area(outer) > 0
    assert area(inner) < 0


def test_prismatic_varying_cross_section_raises():
    """A pyramid has shrinking square slices -> NOT prismatic -> raises."""
    nodes, indices = _pyramid()
    with pytest.raises(NotPrismaticError):
        build_csg_tree(_mesh_data(nodes, indices), "prismatic", {})


def test_prismatic_tree_passes_translator_validation():
    """The emitted tree is DIRECTLY consumable: translate_to_fusion_commands
    runs its pure validation phase headlessly (TrapRoot is never touched) and
    only fails at the adsk-import phase -> NOT UnsupportedSCADNodeError."""
    from mcp_server import scad_translator as st
    nodes, indices = _unit_cube()
    tree = build_csg_tree(_mesh_data(nodes, indices), "prismatic", {})
    with pytest.raises(RuntimeError) as excinfo:
        st.translate_to_fusion_commands(tree, TrapRoot(), "cm")
    assert "adsk is not available" in str(excinfo.value)
    assert not isinstance(excinfo.value, st.UnsupportedSCADNodeError)


# ---------------------------------------------------------------------------
# csg_decompose
# ---------------------------------------------------------------------------

def test_csg_decompose_two_boxes_union_tree():
    """Two disjoint boxes -> union tree with per-primitive translate+rotate
    wrapping cube primitives at the correct centers/sizes."""
    nodes, indices = _two_boxes()
    tree = build_csg_tree(_mesh_data(nodes, indices), "csg_decompose", {})
    assert len(tree) == 1, tree
    root = tree[0]
    assert root["kind"] == "union", root
    children = root["children"]
    assert len(children) == 2, children
    centers = []
    for child in children:
        assert child["kind"] == "translate", child
        centers.append(child["params"]["0"])
        assert len(child["children"]) == 1
        rot = child["children"][0]
        assert rot["kind"] == "rotate", rot
        assert len(rot["children"]) == 1
        cube = rot["children"][0]
        assert cube["kind"] == "cube", cube
        assert cube["params"]["center"] is True
        assert [round(v, 6) for v in cube["params"]["size"]] == [2.0, 2.0, 2.0]
    centers = [[round(v, 6) for v in c] for c in centers]
    assert [1.0, 1.0, 1.0] in centers, centers
    assert [3.5, 1.0, 1.0] in centers, centers


# ---------------------------------------------------------------------------
# strategy validation
# ---------------------------------------------------------------------------

def test_unknown_strategy_raises():
    nodes, indices = _unit_cube()
    with pytest.raises(UnsupportedStrategyError):
        build_csg_tree(_mesh_data(nodes, indices), "bogus", {})


def test_organic_strategy_raises_until_t7():
    """'organic' is NOT a csg strategy yet (T7 adds it) -> UnsupportedStrategy."""
    nodes, indices = _unit_cube()
    with pytest.raises(UnsupportedStrategyError):
        build_csg_tree(_mesh_data(nodes, indices), "organic", {})


def test_revolved_strategy_is_profile_not_csg():
    """'revolved' emits NO CSG tree: build_csg_tree raises
    UnsupportedStrategyError (the profile path is compute_revolved_profile,
    covered by the revolved half-profile tests below)."""
    nodes, indices = _unit_cube()
    with pytest.raises(UnsupportedStrategyError) as excinfo:
        build_csg_tree(_mesh_data(nodes, indices), "revolved", {})
    assert "revolve profile" in str(excinfo.value)
    # and the profile path is the supported route: it produces pts, not a tree
    profile = compute_revolved_profile(_mesh_data(nodes, indices), {})
    assert isinstance(profile, list) and len(profile) >= 3


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
    nodes, indices = _unit_cube()
    tree = build_csg_tree(_mesh_data(nodes, indices), "prismatic", {})
    scaled = scale_tree(tree, 10.0)  # cm -> mm
    assert scaled is not tree
    root = scaled[0]
    assert root["params"]["height"] == pytest.approx(10.0, abs=1e-6)
    poly = root["children"][0]
    assert _pt_set(poly["params"]["pts"]) == {
        (0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)}

    dnodes, dindices = _two_boxes()
    dtree = build_csg_tree(_mesh_data(dnodes, dindices), "csg_decompose", {})
    dscaled = scale_tree(dtree, 2.0)
    trans = dscaled[0]["children"][0]
    assert trans["params"]["0"] == [2.0, 2.0, 2.0]
    cube = trans["children"][0]["children"][0]
    assert [round(v, 6) for v in cube["params"]["size"]] == [4.0, 4.0, 4.0]
