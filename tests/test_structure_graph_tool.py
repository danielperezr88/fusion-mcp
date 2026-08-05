#!/usr/bin/env python3
"""Headless tests for the T7 `structure_graph` server tool
(mcp_server/fusion_server.py -- .omo/plans/mesh-to-parametric.md, Todo 7).

The tool fetches mesh triangle data via ``_call("extract_mesh_data", ...)``
and runs the real decompose -> build_structure_graph -> DuckDB persistence
pipeline.  Fusion is NOT available headless, so ``_call`` is mocked with the
established pattern from tests/test_mesh_convert.py: load fusion_server.py
with a stubbed ``mcp.server.fastmcp``, then monkeypatch ``fs._call`` with a
recorder that returns a canned box payload (the ``_box()`` fixture from
tests/test_mesh_graph.py: 8 nodes / 12 triangles / 6 planar faces).

The real pipeline modules (mesh_analysis, mesh_graph) resolve through the
lazy ``_load_mesh_analysis`` / ``_load_mesh_graph`` importers, so the
counts asserted here are genuine end-to-end results.
"""

import importlib.util
import json
import os
import sys
import types

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SERVER_PATH = os.path.join(REPO_ROOT, "mcp_server", "fusion_server.py")

from mcp_server import mesh_graph  # noqa: E402  (for LRU assertions)


@pytest.fixture(scope="module")
def fs():
    """Load fusion_server.py headless with a stubbed mcp.server.fastmcp.

    Same pattern as tests/test_mesh_convert.py; the stub additionally
    records every @mcp.tool() registration so tests can assert the tool
    appears in the MCP tool list.
    """
    class _Image:
        def __init__(self, data=None, format=None):
            self.data = data
            self.format = format

    class _FastMCP:
        def __init__(self, *a, **k):
            self._tools = {}

        def tool(self, *a, **k):
            def deco(fn):
                self._tools[fn.__name__] = fn
                return fn
            return deco

    stub_mcp = types.ModuleType("mcp")
    stub_server = types.ModuleType("mcp.server")
    stub_fastmcp = types.ModuleType("mcp.server.fastmcp")
    stub_fastmcp.FastMCP = _FastMCP
    stub_fastmcp.Image = _Image
    stub_server.fastmcp = stub_fastmcp
    for name, mod in (("mcp", stub_mcp), ("mcp.server", stub_server),
                      ("mcp.server.fastmcp", stub_fastmcp)):
        sys.modules[name] = mod

    spec = importlib.util.spec_from_file_location("fusionmcp_server_dev_t7",
                                                  SERVER_PATH)
    fs_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fs_mod)
    return fs_mod


@pytest.fixture(autouse=True)
def _clear_graph_db_cache():
    """Each test starts from an empty LRU so key/eviction state is isolated
    (same fixture as tests/test_mesh_graph_db.py)."""
    mesh_graph._GRAPH_DBS.clear()
    yield
    mesh_graph._GRAPH_DBS.clear()


# ---------------------------------------------------------------------------
# fixtures (canonical box from tests/test_mesh_graph.py)
# ---------------------------------------------------------------------------

def _box_payload():
    nodes = [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0),
             (0, 0, 2), (2, 0, 2), (2, 2, 2), (0, 2, 2)]
    tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
            (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
            (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return json.dumps({"mesh": "0", "nodes": nodes, "indices": tris,
                       "normals": []})


class _FakeCall:
    """Records (command, params) pairs and returns canned mesh payloads."""

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else _box_payload()
        self.calls = []

    def __call__(self, command, params=None, timeout=30):
        self.calls.append((command, params))
        return self.payload


def _run(fs, mesh="0", units="cm", payload=None, fake=None):
    """Run structure_graph with a mocked _call; returns (summary_dict, fake)."""
    fake = fake or _FakeCall(payload)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(fs, "_call", fake)
    try:
        raw = fs.structure_graph(mesh=mesh, units=units)
    finally:
        monkeypatch.undo()
    return json.loads(raw), fake


# ---------------------------------------------------------------------------
# tool behaviour
# ---------------------------------------------------------------------------

def test_structure_graph_summary_keys(fs):
    summary, fake = _run(fs)
    # tool appears in the MCP tool list
    assert "structure_graph" in fs.mcp._tools
    # _call fired exactly once, with the mesh body passed through
    assert fake.calls == [("extract_mesh_data", {"mesh": "0"})]
    # all summary keys present
    assert set(summary) == {
        "mesh", "units", "component_count", "face_count", "edge_type_counts",
        "base_face_candidates", "has_warnings", "duckdb_table_schema",
        "query_example", "articulation_points", "connected_component_count",
    }
    assert summary["mesh"] == "0"
    assert summary["units"] == "cm"
    assert summary["face_count"] == 6
    assert summary["component_count"] == 1
    assert summary["has_warnings"] is False
    # all 10 edge relations present (zeros included), box carries 12 adjacent
    assert set(summary["edge_type_counts"]) == set(fs._GRAPH_EDGE_RELATIONS)
    assert summary["edge_type_counts"]["EDGE_ADJACENT"] == 12
    # preliminary base-face candidate: the 6 faces tie at 4.0 cm2 -> one per
    # component
    assert summary["base_face_candidates"] == [
        {"component_id": 0, "face_id": "face:0", "area_cm2": 4.0}]
    # persisted schema section reflects the real tables
    assert summary["duckdb_table_schema"]["nodes"][0] == "node_id"
    assert summary["duckdb_table_schema"]["edges"][:3] == \
        ["source", "target", "relation"]
    assert summary["query_example"].startswith(
        "SELECT node_id, area_cm2 FROM nodes")
    assert summary["connected_component_count"] == 1


def test_structure_graph_persists_duckdb(fs):
    summary, _ = _run(fs)
    assert "error" not in summary
    # the connection is cached under the mesh key "0"
    conn = mesh_graph._get_graph_db("0")
    assert conn is mesh_graph._GRAPH_DBS["0"]
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 7
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 21
    # persisted rows match the summary's counts
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE label = 'Face'"
                        ).fetchone()[0] == summary["face_count"]


def test_structure_graph_error_envelope(fs):
    summary, _ = _run(fs, fake=_FakeCall("Error: mesh not found"))
    assert summary == {"error": "mesh not found"}
    # invalid units fail BEFORE _call fires
    fake = _FakeCall()
    summary, _ = _run(fs, units="furlongs", fake=fake)
    assert summary == {"error": "Unsupported units 'furlongs'. "
                                "Use 'mm', 'cm', or 'in'."}
    assert fake.calls == []


def test_structure_graph_articulation_points(fs):
    summary, _ = _run(fs)
    # key present with list type (a closed box has no articulation points)
    assert "articulation_points" in summary
    assert isinstance(summary["articulation_points"], list)
    assert summary["connected_component_count"] == 1
    # Face nodes flagged is_articulation_point match the computed list
    conn = mesh_graph._get_graph_db("0")
    flagged = [r[0] for r in conn.execute(
        "SELECT node_id FROM nodes WHERE label = 'Face' "
        "AND is_articulation_point").fetchall()]
    assert flagged == summary["articulation_points"]


def test_structure_graph_rebuild_overwrites(fs):
    summary1, _ = _run(fs)
    conn1 = mesh_graph._get_graph_db("0")
    summary2, _ = _run(fs)
    conn2 = mesh_graph._get_graph_db("0")
    # second call replaces the cached connection for the same mesh key
    assert conn2 is not conn1
    assert mesh_graph._GRAPH_DBS["0"] is conn2
    # no KeyError, counts still correct after the rebuild
    assert "error" not in summary1 and "error" not in summary2
    assert summary2["face_count"] == 6
    assert conn2.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 7
