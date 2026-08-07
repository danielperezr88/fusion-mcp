#!/usr/bin/env python3
"""
Headless tests for the T10 workflow guide (.omo/plans/mesh-to-parametric.md,
Todo 10 -- get_workflow_guide tool + chainable output envelopes).

Two layers are covered:

  1. `mcp_server/workflow_guide.py` -- the pure-data GUIDE module: 8 ordered
     steps with the exact key set, MODEL ACTION markers on annotate/review,
     reconstruct's strategy branch + fallback, and the `get_step` lookup.
  2. `mcp_server/fusion_server.py` -- the `get_workflow_guide` server tool
     (served locally, NO `_call`, NO bridge) via the stubbed
     mcp.server.fastmcp pattern, plus the docstring stage cross-references
     and the pinned T5/T9 workflow envelopes.
"""

import importlib.util
import inspect
import json
import os
import re
import sys
import types

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SERVER_PATH = os.path.join(REPO_ROOT, "mcp_server", "fusion_server.py")

from mcp_server.workflow_guide import GUIDE, GUIDE_JSON, get_step  # noqa: E402

_STEP_TOOLS = [
    "import_mesh_file", "analyze_mesh", "slice_mesh",
    "annotate_mesh_parameters", "select_parameter_schema",
    "reconstruct_mesh", "compare_mesh_to_brep", "review_reconstruction",
]
_KEYS = {"tool", "purpose", "inputs", "outputs", "model_action", "branch",
         "fallback", "note"}


# ---------------------------------------------------------------------------
# GUIDE module shape
# ---------------------------------------------------------------------------

def test_guide_has_8_steps_in_exact_order():
    assert len(GUIDE) >= 8
    assert [s["tool"] for s in GUIDE] == _STEP_TOOLS


def test_every_step_has_exactly_the_required_keys():
    assert all(set(step) - {"note"} <= _KEYS for step in GUIDE)
    for step in GUIDE:
        assert isinstance(step["tool"], str) and step["tool"]
        assert isinstance(step["purpose"], str) and step["purpose"]
        assert isinstance(step["inputs"], list) and step["inputs"]
        assert isinstance(step["outputs"], list) and step["outputs"]
        assert step["model_action"] is None or isinstance(
            step["model_action"], str)
        assert step["branch"] is None or isinstance(step["branch"], (dict, type(None)))
        assert step["fallback"] is None or isinstance(step["fallback"], (str, type(None)))


def test_model_action_markers_on_annotate_and_review():
    by_tool = {s["tool"]: s for s in GUIDE}
    annotate = by_tool["annotate_mesh_parameters"]
    review = by_tool["review_reconstruction"]
    assert "classify" in annotate["model_action"].lower()
    assert "compare" in review["model_action"].lower()
    assert "accept" in review["model_action"].lower()


def test_get_step_resolves_short_and_tool_names():
    assert get_step("reconstruct")["tool"] == "reconstruct_mesh"
    assert get_step("reconstruct_mesh")["tool"] == "reconstruct_mesh"
    assert get_step("import")["tool"] == "import_mesh_file"
    assert get_step("annotate")["tool"] == "annotate_mesh_parameters"
    assert get_step("select_parameter_schema")["tool"] == "select_parameter_schema"
    assert get_step("bogus") is None
    assert get_step("") is None
    assert get_step(None) is None


def test_guide_json_round_trips():
    assert json.loads(GUIDE_JSON) == GUIDE


# ---------------------------------------------------------------------------
# Server tool (stubbed mcp.server.fastmcp, no bridge)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def fs():
    """Load fusion_server.py headless with a stubbed mcp.server.fastmcp.

    The `mcp` SDK is not installed in this environment, so the module-level
    `from mcp.server.fastmcp import FastMCP, Image` is satisfied with a stub
    (same pattern as tests/test_mesh_convert.py).
    """
    class _Image:
        def __init__(self, data=None, format=None):
            self.data = data
            self.format = format

    class _FastMCP:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            def deco(fn):
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

    spec = importlib.util.spec_from_file_location("fusionmcp_server_dev_t10",
                                                  SERVER_PATH)
    fs_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fs_mod)
    return fs_mod


def test_server_full_guide(fs):
    raw = fs.get_workflow_guide()
    data = json.loads(raw)
    assert isinstance(data, list)
    assert len(data) >= 8
    assert [s["tool"] for s in data] == _STEP_TOOLS
    # MODEL ACTION markers survive the full dump
    by_tool = {s["tool"]: s for s in data}
    assert "classify" in by_tool["annotate_mesh_parameters"]["model_action"]
    assert "compare" in by_tool["review_reconstruction"]["model_action"]


def test_server_single_step_by_short_name(fs):
    data = json.loads(fs.get_workflow_guide(step="reconstruct"))
    assert isinstance(data, dict)
    assert data["tool"] == "reconstruct_mesh"
    assert data["inputs"] and data["outputs"]
    assert "DISMANTLED" in data.get("note", "")


def test_server_single_step_by_tool_name(fs):
    data = json.loads(fs.get_workflow_guide(step="select_parameter_schema"))
    assert isinstance(data, dict)
    assert data["tool"] == "select_parameter_schema"
    assert data["inputs"] and data["outputs"]


def test_server_unknown_step_exact_error(fs):
    raw = fs.get_workflow_guide(step="bogus")
    assert raw == json.dumps({"error": "Unknown workflow step 'bogus'"},
                             indent=2)
    assert json.loads(raw) == {"error": "Unknown workflow step 'bogus'"}


def test_server_empty_and_none_step_return_full_guide(fs):
    for value in ("", None):
        data = json.loads(fs.get_workflow_guide(step=value))
        assert isinstance(data, list) and len(data) >= 8


def test_server_never_touches_bridge(fs):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(fs, "_call", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("get_workflow_guide is pure-data: no _call")))
    try:
        raw = fs.get_workflow_guide()
        data = json.loads(raw)
        assert len(data) >= 8
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# Docstring stage cross-references (acceptance c) + T5/T9 envelopes (d)
# ---------------------------------------------------------------------------

def test_docstring_stage_cross_references(fs):
    expected = {
        "analyze_mesh": "Stage 2",
        "slice_mesh": "Stage 3",
        "annotate_mesh_parameters": "Stage 4",
        "select_parameter_schema": "Stage 5",
        "compare_mesh_to_brep": "Stage 7",
        "review_reconstruction": "Stage 8",
    }
    for tool_name, stage in expected.items():
        doc = inspect.getdoc(getattr(fs, tool_name)) or ""
        assert stage in doc, f"{tool_name} docstring missing {stage}"
        assert ("of the mesh-to-parametric workflow (see get_workflow_guide)"
                in doc), f"{tool_name} docstring missing workflow cross-reference"


