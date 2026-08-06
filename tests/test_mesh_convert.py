#!/usr/bin/env python3
"""
Headless tests for the T7 organic strategy of `reconstruct_mesh`
(.omo/plans/mesh-to-parametric.md, Todo 7 -- MeshConvertFeature PREVIEW).

The MeshConvertFeature API lives inside Fusion (FusionMCP.py `_mesh_convert`),
which cannot be imported headless, so these tests cover the SERVER half at
the `_call` boundary with real handler-shaped payloads:

  * `strategy="organic"` is accepted by the whitelist and routes to
    `_call("mesh_convert", {"mesh", "method": "organic", "operation"})`.
  * The envelope is EXACTLY {"strategy", "method", "preview_api",
    "parametric", "note"} with the PREVIEW caveat note; `parametric` is True
    for operation=parametric and False for operation=base.
  * Handler error envelopes pass through verbatim (the exact not-available
    string is asserted).
  * `auto` routing to recommended_strategy == "organic" also calls
    mesh_convert.
  * The unknown-strategy error now lists organic in Supported.
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


@pytest.fixture(scope="module")
def fs():
    """Load fusion_server.py headless with a stubbed mcp.server.fastmcp.

    The `mcp` SDK is not installed in this environment, so the module-level
    `from mcp.server.fastmcp import FastMCP, Image` is satisfied with a stub
    (same pattern as tests/test_annotate_mesh_parameters_live.py).
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

    spec = importlib.util.spec_from_file_location("fusionmcp_server_dev_t7",
                                                  SERVER_PATH)
    fs_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fs_mod)
    return fs_mod

NOT_AVAILABLE = "MeshConvertFeature not available on this Fusion build (PREVIEW API)"


def _convert_success(method="organic", operation="parametric"):
    return json.dumps({
        "converted": True,
        "bodies": 1,
        "names": ["converted_1"],
        "method": method,
        "operation": operation,
        "preview_api": True,
    })


def _mesh_extract_payload():
    return json.dumps({"mesh": "0", "nodes": [], "indices": [], "normals": []})


class _FakeCall:
    """Records (command, params) pairs and returns canned payloads."""

    def __init__(self, convert_payload):
        self.convert_payload = convert_payload
        self.calls = []

    def __call__(self, command, params, timeout=None):
        self.calls.append((command, params))
        if command == "extract_mesh_data":
            return _mesh_extract_payload()
        return self.convert_payload


def _run_organic(fs, strategy, params=None, convert_payload=None, fake=None):
    fake = fake or _FakeCall(convert_payload or _convert_success())
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(fs, "_call", fake)
    try:
        raw = fs.reconstruct_mesh(mesh="0", strategy=strategy, units="mm",
                                  params=params, job_id="sync")
    finally:
        monkeypatch.undo()
    return json.loads(raw), fake


def test_organic_whitelisted_and_exact_envelope(fs):
    envelope, fake = _run_organic(fs, "organic")
    assert set(envelope) == {"strategy", "method", "preview_api", "parametric", "note"}
    assert envelope["strategy"] == "organic"
    assert envelope["method"] == "mesh_convert"
    assert envelope["preview_api"] is True
    assert envelope["parametric"] is True
    assert envelope["note"] == ("Organic conversion is a PREVIEW API result, "
                                "not a parameterized solid.")
    cmd, params = fake.calls[-1]
    assert cmd == "mesh_convert"
    assert params["mesh"] == "0"
    assert params["method"] == "organic"
    assert params["operation"] == "parametric"


def test_organic_base_operation_reports_parametric_false(fs):
    envelope, fake = _run_organic(fs, "organic", params={"operation": "base"})
    assert envelope["parametric"] is False
    cmd, params = fake.calls[-1]
    assert params["operation"] == "base"


def test_organic_not_available_error_passes_through_verbatim(fs):
    envelope, fake = _run_organic(
        fs, "organic", convert_payload=json.dumps({"error": NOT_AVAILABLE}))
    assert envelope == {"error": NOT_AVAILABLE}
    cmd, params = fake.calls[-1]
    assert cmd == "mesh_convert"


def test_auto_routing_to_organic_calls_mesh_convert(fs):
    class FakeAnalysis:
        def analyze_mesh_data(self, nodes, indices, normals):
            return {"recommended_strategy": "organic"}

    fake = _FakeCall(_convert_success())
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(fs, "_call", fake)
    monkeypatch.setattr(fs, "_load_mesh_analysis", lambda: FakeAnalysis())
    try:
        raw = fs.reconstruct_mesh(mesh="0", strategy="auto", units="mm",
                                  job_id="sync")
    finally:
        monkeypatch.undo()
    envelope = json.loads(raw)
    assert envelope["strategy"] == "organic"
    assert envelope["method"] == "mesh_convert"
    commands = [c for c, _ in fake.calls]
    assert "mesh_convert" in commands
    params = dict(fake.calls[commands.index("mesh_convert")][1])
    assert params["method"] == "organic"


def test_unknown_strategy_error_lists_organic(fs):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(fs, "_call", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("_call must not fire for an unknown strategy")))
    try:
        raw = fs.reconstruct_mesh(mesh="0", strategy="bogus", units="mm",
                                  job_id="sync")
    finally:
        monkeypatch.undo()
    envelope = json.loads(raw)
    assert "Unknown strategy 'bogus'." in envelope["error"]
    assert envelope["error"].endswith(
        "Supported: auto, prismatic, revolved, csg_decompose, organic")


def test_organic_skips_csg_building(fs):
    fake = _FakeCall(_convert_success())
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(fs, "_call", fake)
    monkeypatch.setattr(fs, "_load_mesh_csg",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("organic must not build a CSG tree")))
    try:
        raw = fs.reconstruct_mesh(mesh="0", strategy="organic", units="mm",
                                  job_id="sync")
    finally:
        monkeypatch.undo()
    envelope = json.loads(raw)
    assert envelope["strategy"] == "organic"
