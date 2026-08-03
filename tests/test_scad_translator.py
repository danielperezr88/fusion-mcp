"""Headless unit tests for mcp_server/scad_translator.py.

Covers (plan Todo 17 acceptance):
  * resolve valid / invalid SCAD via the openscad-evaluator pipeline
  * BOSL2 module resolution (cuboid -> cube, cyl -> cylinder, torus ->
    rotate_extrude + circle) using the already-bundled BOSL2 library
  * pure transform-matrix decomposition (_decompose_multmatrix) and
    shape-agnostic transform extraction (_transform_vector)
  * two-phase validation: unsupported nodes fail during the PURE phase
    (TrapRoot is never touched); a valid tree fails only on adsk import
  * linear_extrude + circle resolution (2D -> 3D)

No Fusion process and no network are required -- resolve_scad() uses the
openscad-lalr-parser / openscad-evaluator packages, and every translator
test passes a TrapRoot that would fail on any adsk access.
"""

import os

import pytest

from conftest import TrapRoot
from mcp_server import scad_translator as st
from mcp_server.bundle import BOSL2_DIR


# ---------------------------------------------------------------------------
# tree helpers
# ---------------------------------------------------------------------------

def _walk_kinds(nodes):
    kinds = []
    for node in nodes:
        kinds.append(node["kind"])
        kinds.extend(_walk_kinds(node.get("children", [])))
    return kinds


def _find_first(nodes, kind):
    for node in nodes:
        if node["kind"] == kind:
            return node
        found = _find_first(node.get("children", []), kind)
        if found is not None:
            return found
    return None


@pytest.fixture(scope="module")
def bosl2_path():
    """Path to the bundled BOSL2 library, or SKIP (never downloads)."""
    std_scad = os.path.join(BOSL2_DIR, "std.scad")
    if not os.path.isfile(std_scad):
        pytest.skip("BOSL2 bundle not installed at %r -- would need network"
                    % BOSL2_DIR)
    return BOSL2_DIR


# ---------------------------------------------------------------------------
# resolve_scad(): valid / invalid source
# ---------------------------------------------------------------------------

def test_resolve_cube():
    tree = st.resolve_scad("cube([10,10,10]);")
    assert len(tree) == 1
    node = tree[0]
    assert node["kind"] == "cube"
    assert node["params"]["size"] == [10.0, 10.0, 10.0]
    assert node["params"]["center"] is False


def test_resolve_invalid_scad_raises_parse_error():
    with pytest.raises(st.SCADParseError) as excinfo:
        st.resolve_scad("{{{")
    message = str(excinfo.value)
    assert "Syntax error" in message or "parse error" in message.lower()


# ---------------------------------------------------------------------------
# resolve_scad(): BOSL2 module resolution (bundled library, no network)
# ---------------------------------------------------------------------------

def test_resolve_bosl2_cuboid_is_cube(bosl2_path):
    tree = st.resolve_scad(
        "include <BOSL2/std.scad>\ncuboid([10,10,10]);",
        bosl2_path=bosl2_path)
    node = _find_first(tree, "cube")
    assert node is not None, "cuboid() must resolve to a cube node"
    assert node["params"]["size"] == [10.0, 10.0, 10.0]


def test_resolve_bosl2_cyl_is_cylinder(bosl2_path):
    tree = st.resolve_scad(
        "include <BOSL2/std.scad>\ncyl(l=30,d=10);",
        bosl2_path=bosl2_path)
    node = _find_first(tree, "cylinder")
    assert node is not None, "cyl() must resolve to a cylinder node"
    assert node["params"]["h"] == 30.0
    assert node["params"]["r1"] == 5.0   # d=10 -> r=5
    assert node["params"]["r2"] == 5.0


def test_resolve_bosl2_torus_is_revolve_of_circle(bosl2_path):
    tree = st.resolve_scad(
        "include <BOSL2/std.scad>\ntorus(d_maj=40,d_min=10);",
        bosl2_path=bosl2_path)
    kinds = _walk_kinds(tree)
    assert "rotate_extrude" in kinds, "torus() must revolve a 2D profile"
    assert "circle" in kinds, "torus() cross-section must be a circle"
    circle = _find_first(tree, "circle")
    assert circle["params"]["r"] == 5.0  # d_min=10 -> r=5


# ---------------------------------------------------------------------------
# pure transform helpers (no adsk)
# ---------------------------------------------------------------------------

def test_decompose_multmatrix_translate_rotate_scale():
    # M = T(10,20,30) . Rz(90deg) . S(2,1,1)  (verified empirically in
    # learnings.md: Rz(90) is [[0,-1,0],[1,0,0],[0,0,1]] with rot[0][1] = -1).
    matrix = [[0.0, -1.0, 0.0, 10.0],
              [2.0, 0.0, 0.0, 20.0],
              [0.0, 0.0, 1.0, 30.0],
              [0.0, 0.0, 0.0, 1.0]]
    translation, scales, rotation = st._decompose_multmatrix(matrix)
    assert translation == [10.0, 20.0, 30.0]
    assert [round(s, 6) for s in scales] == [2.0, 1.0, 1.0]
    # Rz(90): x-axis must map onto the y-axis.
    assert abs(rotation[0][1] + 1.0) < 1e-9   # rot[0][1] == -1
    assert abs(rotation[1][0] - 1.0) < 1e-9   # rot[1][0] == +1
    assert abs(rotation[0][0]) < 1e-9
    assert abs(rotation[1][1]) < 1e-9
    assert abs(rotation[2][2] - 1.0) < 1e-9


def test_decompose_multmatrix_identity():
    matrix = [[1.0, 0.0, 0.0, 0.0],
              [0.0, 1.0, 0.0, 0.0],
              [0.0, 0.0, 1.0, 0.0],
              [0.0, 0.0, 0.0, 1.0]]
    translation, scales, rotation = st._decompose_multmatrix(matrix)
    assert translation == [0.0, 0.0, 0.0]
    assert [round(s, 6) for s in scales] == [1.0, 1.0, 1.0]
    assert st._matrix_near_identity(rotation)


def test_transform_vector_accepts_all_param_shapes():
    default = [0.0, 0.0, 0.0]
    # evaluator: positional args under int keys
    assert st._transform_vector({"args": {0: [1, 2, 3]}}, default) == [1, 2, 3]
    # .csg walker: string keys inside args
    assert st._transform_vector({"args": {"0": [1, 2, 3]}}, default) == [1, 2, 3]
    # .csg walker: positional string key directly on params
    assert st._transform_vector({"0": [1, 2, 3]}, default) == [1, 2, 3]
    # old .csg (<=2021.01): named argument shapes
    assert st._transform_vector({"v": [1, 2, 3]}, default) == [1, 2, 3]
    assert st._transform_vector({"a": 45}, default) == 45
    # absent transform -> the default vector is returned
    assert st._transform_vector({}, default) == [0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# two-phase validation (pure phase vs adsk-import phase)
# ---------------------------------------------------------------------------

def test_hull_node_raises_unsupported_during_validation():
    tree = [{"kind": "hull", "params": {}, "children": [
        {"kind": "cube", "params": {"size": [1.0, 1.0, 1.0]},
         "children": []}]}]
    with pytest.raises(st.UnsupportedSCADNodeError) as excinfo:
        st.translate_to_fusion_commands(tree, TrapRoot(), None)
    assert "hull" in str(excinfo.value)


def test_top_level_2d_polygon_requires_extrude_parent():
    tree = [{"kind": "polygon",
             "params": {"pts": [[0, 0], [10, 0], [5, 10]]},
             "children": []}]
    with pytest.raises(st.UnsupportedSCADNodeError) as excinfo:
        st.translate_to_fusion_commands(tree, TrapRoot(), None)
    assert "linear_extrude or rotate_extrude parent" in str(excinfo.value)


def test_valid_cube_tree_fails_only_on_adsk_import():
    tree = [{"kind": "cube",
             "params": {"size": [10.0, 10.0, 10.0], "center": False},
             "children": []}]
    with pytest.raises(RuntimeError) as excinfo:
        st.translate_to_fusion_commands(tree, TrapRoot(), None)
    assert "adsk is not available" in str(excinfo.value)
    # NOT UnsupportedSCADNodeError -> the pure validation phase passed.
    assert not isinstance(excinfo.value, st.UnsupportedSCADNodeError)


def test_validate_tree_accepts_supported_transform_chain():
    tree = [{"kind": "translate",
             "params": {"args": {0: [1.0, 2.0, 3.0]}},
             "children": [
                 {"kind": "cube",
                  "params": {"size": [1.0, 1.0, 1.0]},
                  "children": []}]}]
    st._validate_tree(tree)  # must not raise


def test_validate_tree_rejects_unknown_kind():
    tree = [{"kind": "not_a_scad_kind", "params": {}, "children": []}]
    with pytest.raises(st.UnsupportedSCADNodeError) as excinfo:
        st._validate_tree(tree)
    assert "not_a_scad_kind" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 2D + extrude resolution
# ---------------------------------------------------------------------------

def test_resolve_linear_extrude_circle():
    tree = st.resolve_scad("linear_extrude(height=10) circle(r=5);")
    assert len(tree) == 1
    node = tree[0]
    assert node["kind"] == "linear_extrude"
    assert node["params"]["height"] == 10.0
    assert len(node["children"]) == 1
    child = node["children"][0]
    assert child["kind"] == "circle"
    assert child["params"]["r"] == 5.0


# ---------------------------------------------------------------------------
# polyhedron param normalization (pure helpers)
# ---------------------------------------------------------------------------

def test_polyhedron_mesh_evaluator_shape():
    verts, tris = st._polyhedron_mesh({
        "verts": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 0, 1]],
        "tri_arr": [[0, 1, 2], [0, 1, 3]],
    })
    assert verts == [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0],
                     [1.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    assert tris == [[0, 1, 2], [0, 1, 3]]


def test_polyhedron_mesh_csg_shape_fan_triangulates():
    verts, tris = st._polyhedron_mesh({
        "points": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]],
        "faces": [[0, 1, 2, 3]],
    })
    assert tris == [[0, 1, 2], [0, 2, 3]]


def test_is_loftable_two_equal_rings():
    verts = [[0, 0, 0], [1, 0, 0], [0.5, 1, 0],
             [0, 0, 2], [1, 0, 2], [0.5, 1, 2]]
    assert st._is_loftable(verts) is True


def test_is_loftable_rejects_non_two_ring():
    verts = [[0, 0, 0], [1, 0, 0], [0.5, 1, 0], [0.5, 0.5, 0.5]]
    assert st._is_loftable(verts) is False
