#!/usr/bin/env python3
"""
Live test for `compare_mesh_brep` (FusionMCP.py) + `compare_mesh_to_brep`
(mcp_server/fusion_server.py) -- .omo/plans/mesh-to-parametric.md, Todo 8
(vision-free fidelity QA).

The RUNNING add-in copy in Fusion 360 lags the repo, so the repo copy of
FusionMCP.py is driven INSIDE Fusion via execute_script + the fresh-module
pattern (importlib spec_from_file_location -> mod.app = app ->
_process_command), mirroring tests/test_annotate_mesh_parameters_live.py.

pytest behaviour: probes the bridge first; skips cleanly when Fusion is not
reachable (so the headless `py -m pytest tests -v` run stays green), and runs
the real assertions when Fusion is live.

Checks:
  * happy path  -- unit cube mesh (import_mesh_data) + prismatic-reconstructed
                   BRep cube (create_from_csg_tree) -> volume_ratio ~= 1.0
                   (within 5%), bbox_max_deviation_cm < 0.1,
                   sampled_deviation_cm.samples > 0, method == "vertex_fallback"
                   (SurfaceEvaluator.getClosestPointTo is absent on Metis F6)
  * failure paths -- missing mesh / missing BRep body / mesh ref used as the
                     BRep body -> graceful {"error": ...} (never a crash)
  * server path -- compare_mesh_to_brep returns the dict envelope with the
                   EXACT keys {"mesh", "brep", "volume_ratio",
                   "bbox_max_deviation_cm", "sampled_deviation_cm", "method"};
                   validated headless with a stubbed mcp.server.fastmcp and
                   the REAL fresh-module payload injected at the _call boundary.
"""

import importlib.util
import json
import os
import sys
import types

import pytest
import requests

BASE_URL = "http://127.0.0.1:7432/command"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUSIONMCP_PATH = os.path.join(REPO_ROOT, "FusionMCP.py")
SERVER_PATH = os.path.join(REPO_ROOT, "mcp_server", "fusion_server.py")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

EXPECTED_KEYS = {"mesh", "brep", "volume_ratio", "bbox_max_deviation_cm",
                 "sampled_deviation_cm", "method"}


class BridgeError(Exception):
    """Raised when the Fusion HTTP bridge cannot be reached."""


def call(command, params=None, timeout=240):
    """POST a command to the Fusion bridge and return the parsed JSON dict."""
    params = params or {}
    try:
        r = requests.post(BASE_URL, json={"command": command, "params": params},
                          timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise BridgeError(
            f"Cannot reach Fusion 360 on http://127.0.0.1:7432 - is Fusion "
            f"open and the FusionMCP add-in running? ({e})")
    try:
        return r.json()
    except ValueError as e:
        raise BridgeError(f"Bridge returned invalid JSON: {e}")


def run_code(code, timeout=240):
    """Run Python inside Fusion via execute_script; returns parsed output."""
    resp = call("execute_script", {"code": code}, timeout=timeout)
    if isinstance(resp, dict) and "error" in resp:
        raise RuntimeError(
            "execute_script failed inside Fusion:\n" + str(resp["error"])[:2000])
    return resp.get("output")


# ---- document lifecycle helpers (close test-created docs, never leak) ----

def _open_doc_ids():
    """Snapshot currently-open Fusion document ids via execute_script.

    Returns None when the snapshot cannot be taken (bridge hiccup) so the
    caller skips cleanup instead of ever closing pre-existing documents.
    """
    try:
        out = run_code(
            "_ids = []\n"
            "for _i in range(app.documents.count):\n"
            "    _ids.append(app.documents.item(_i).creationId)\n"
            "result['output'] = _ids\n")
    except Exception:
        return None
    return out if isinstance(out, list) else None


def _close_docs_except(pre_ids):
    """Close every open Fusion document whose id is NOT in pre_ids.

    Collects the ids to close FIRST, then re-finds each document before
    closing (indices shift as documents close -- never close while iterating
    by index). Each close is guarded so an already-closed document or an API
    hiccup never fails the test; cleanup never raises. Returns how many
    documents were closed.
    """
    if pre_ids is None:
        return 0
    try:
        n = run_code(
            "_pre = %s\n"
            "_ids = []\n"
            "for _i in range(app.documents.count):\n"
            "    _d = app.documents.item(_i)\n"
            "    if _d.creationId not in _pre:\n"
            "        _ids.append(_d.creationId)\n"
            "_closed = 0\n"
            "for _did in _ids:\n"
            "    for _i in range(app.documents.count):\n"
            "        _d = app.documents.item(_i)\n"
            "        if _d.creationId == _did:\n"
            "            try:\n"
            "                _d.close(False)\n"
            "                _closed += 1\n"
            "            except Exception:\n"
            "                pass\n"
            "            break\n"
            "result['output'] = _closed\n" % repr(pre_ids))
    except Exception as e:
        print(f"[cleanup] document close skipped: {e}")
        return 0
    print(f"[cleanup] closed {n or 0} test document(s)")
    return n or 0


def fresh_dispatch(command, params, timeout=240):
    """Drive the repo FusionMCP.py handler in a fresh module inside Fusion."""
    inner = (
        "import importlib.util as _ilu\n"
        "import sys as _sys\n"
        "_spec = _ilu.spec_from_file_location('fusionmcp_dev', %s)\n"
        "_mod = _ilu.module_from_spec(_spec)\n"
        "_sys.modules['fusionmcp_dev'] = _mod\n"
        "_spec.loader.exec_module(_mod)\n"
        "_mod.app = app\n"
        "_out = _mod._process_command({'command': %s, 'params': %s})\n"
        "result['output'] = _out\n"
    ) % (repr(FUSIONMCP_PATH), repr(command), repr(params))
    return run_code(inner, timeout=timeout)


def _unit_cube():
    """Outward-wound unit cube [0,1]^3 as flat node/index lists."""
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


def _probe_bridge():
    try:
        r = requests.post(BASE_URL, json={"command": "get_info", "params": {}},
                          timeout=10)
        r.raise_for_status()
        return True
    except requests.exceptions.RequestException:
        return False


@pytest.fixture(scope="module")
def bridge():
    """Skip every test when Fusion is not reachable (headless CI runs)."""
    if not _probe_bridge():
        pytest.skip(
            "Fusion bridge unreachable on http://127.0.0.1:7432 - is Fusion "
            "open and the FusionMCP add-in running?")
    return True


@pytest.fixture(autouse=True)
def _close_test_docs(bridge):
    """Close every Fusion document a test creates (id-snapshot diff) after
    the test, on success AND failure, so pytest runs stop leaving documents
    open in Fusion 360."""
    pre_ids = _open_doc_ids()
    yield
    _close_docs_except(pre_ids)


def _fresh_import_cube(bridge):
    """Import a 1x1x1 cm cube mesh via fresh-module import_mesh_data."""
    call("create_new_document", {"name": "T8_compare_QA"})
    nodes, indices = _unit_cube()
    resp = fresh_dispatch("import_mesh_data", {
        "coordinates": nodes,
        "triangle_indices": indices,
        "name": "cube_mesh",
    })
    assert "error" not in resp, f"import_mesh_data failed: {resp.get('error')}"
    return "cube_mesh"


def _fresh_reconstruct_prismatic(bridge, mesh_name):
    """Reconstruct the mesh as a prismatic BRep cube via fresh dispatch.

    Returns the BRep body ref ("0" = first BRep).
    """
    tree = [{"kind": "cube", "params": {"size": [10, 10, 10]}}]
    resp = fresh_dispatch("create_from_csg_tree", {"csg_tree": tree,
                                                    "units": "mm"})
    assert "error" not in resp, f"create_from_csg_tree failed: {resp.get('error')}"
    assert resp.get("bodies", 0) >= 1, resp
    return "0"


def test_compare_mesh_to_brep_happy_path(bridge):
    """Cube mesh vs reconstructed BRep cube: ratio ~1.0, tiny bbox deviation,
    >0 sampled points, vertex_fallback method (Metis F6 verdict)."""
    mesh = _fresh_import_cube(bridge)
    body = _fresh_reconstruct_prismatic(bridge, mesh)
    resp = fresh_dispatch("compare_mesh_brep", {"mesh": mesh, "body": body})
    assert isinstance(resp, dict), f"expected dict, got {resp!r}"
    assert "error" not in resp, f"handler errored: {resp['error']}"
    assert set(resp) == EXPECTED_KEYS, set(resp) ^ EXPECTED_KEYS
    assert resp["mesh"]["volume_cm3"] == pytest.approx(1.0, abs=0.01), resp["mesh"]
    assert resp["brep"]["volume_cm3"] == pytest.approx(1.0, abs=0.01), resp["brep"]
    assert resp["volume_ratio"] == pytest.approx(1.0, rel=0.05), resp["volume_ratio"]
    assert resp["bbox_max_deviation_cm"] < 0.1, resp["bbox_max_deviation_cm"]
    sd = resp["sampled_deviation_cm"]
    assert sd["samples"] > 0, sd
    assert sd["mean"] >= 0.0 and sd["max"] >= sd["mean"], sd
    assert resp["method"] == "vertex_fallback", resp["method"]


def test_compare_mesh_to_brep_missing_mesh_error(bridge):
    """Failure: a nonexistent mesh ref yields a graceful {'error': ...}."""
    _fresh_import_cube(bridge)
    resp = fresh_dispatch("compare_mesh_brep", {"mesh": "999", "body": "0"})
    assert isinstance(resp, dict), f"expected dict, got {resp!r}"
    assert "error" in resp, f"expected graceful error, got {resp!r}"
    assert "not found" in resp["error"].lower(), resp["error"]


def test_compare_mesh_to_brep_missing_body_error(bridge):
    """Failure: a nonexistent BRep body ref yields a graceful error."""
    mesh = _fresh_import_cube(bridge)
    resp = fresh_dispatch("compare_mesh_brep", {"mesh": mesh, "body": "999"})
    assert isinstance(resp, dict), f"expected dict, got {resp!r}"
    assert "error" in resp, f"expected graceful error, got {resp!r}"
    assert "not found" in resp["error"].lower(), resp["error"]


def test_compare_mesh_to_brep_mesh_ref_as_body_error(bridge):
    """Failure: a mesh body used as the BRep body ref errors (not a BRep)."""
    mesh = _fresh_import_cube(bridge)
    resp = fresh_dispatch("compare_mesh_brep", {"mesh": mesh, "body": mesh})
    assert isinstance(resp, dict), f"expected dict, got {resp!r}"
    assert "error" in resp, f"expected graceful error, got {resp!r}"
    assert "not found" in resp["error"].lower(), resp["error"]


def test_compare_mesh_to_brep_server_path(bridge, monkeypatch):
    """Server-path validation with the REAL fresh-module payload injected at
    the _call boundary (the running add-in cannot serve compare_mesh_brep over
    HTTP -- dispatcher lags the repo)."""
    mesh = _fresh_import_cube(bridge)
    body = _fresh_reconstruct_prismatic(bridge, mesh)
    real = fresh_dispatch("compare_mesh_brep", {"mesh": mesh, "body": body})
    assert "error" not in real, real.get("error")

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
        monkeypatch.setitem(sys.modules, name, mod)

    spec = importlib.util.spec_from_file_location("fusionmcp_server_dev_t8",
                                                  SERVER_PATH)
    fs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fs)

    monkeypatch.setattr(
        fs, "_call",
        lambda command, params=None, timeout=30: json.dumps(real, indent=2))

    out = fs.compare_mesh_to_brep(mesh=mesh, body=body, job_id="sync")
    parsed = json.loads(out)
    assert set(parsed) == EXPECTED_KEYS, set(parsed) ^ EXPECTED_KEYS
    assert parsed["mesh"]["volume_cm3"] == pytest.approx(1.0, abs=0.01)
    assert parsed["brep"]["volume_cm3"] == pytest.approx(1.0, abs=0.01)
    assert parsed["sampled_deviation_cm"]["samples"] > 0

    # Error payloads pass through verbatim.
    monkeypatch.setattr(
        fs, "_call",
        lambda command, params=None, timeout=30: json.dumps(
            {"error": "Body '999' not found."}, indent=2))
    err_out = json.loads(fs.compare_mesh_to_brep(mesh="999", body="0",
                                                 job_id="sync"))
    assert err_out == {"error": "Body '999' not found."}
