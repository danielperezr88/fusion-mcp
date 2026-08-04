#!/usr/bin/env python3
"""
Live test for `capture_body_views` (FusionMCP.py) + `review_reconstruction`
(mcp_server/fusion_server.py) -- .omo/plans/mesh-to-parametric.md, Todo 9
(vision QA loop).

The RUNNING add-in copy in Fusion 360 lags the repo, so the repo copy of
FusionMCP.py is driven INSIDE Fusion via execute_script + the fresh-module
pattern (importlib spec_from_file_location -> mod.app = app ->
_process_command), mirroring tests/test_annotate_mesh_parameters_live.py and
tests/test_mesh_to_brep_live.py.

pytest behaviour: probes the bridge first; skips cleanly when Fusion is not
reachable (so the headless `py -m pytest tests -v` run stays green), and runs
the real assertions when Fusion is live.

Checks:
  * happy path  -- fresh doc: unit cube mesh (import_mesh_data) + prismatic
                   BRep cube (extract_mesh_data -> build_csg_tree ->
                   scale_tree x10 -> create_from_csg_tree); capture_body_views
                   returns 3 views, every image_base64 decodes to PNG magic,
                   handler returns {"body": str, "views": [...]}
  * failure paths -- unknown view name -> EXACT error string (same as T5);
                     missing body -> graceful {"error": ...} (never a crash)
  * server path -- review_reconstruction returns
                   [text_envelope, Image x6] interleaved per view (mesh then
                   brep); envelope pairs have view/mesh_image_base64/
                   brep_image_base64 all PNG-magic, geometry summary present,
                   workflow.stage == "review"; validated headless with a
                   stubbed mcp.server.fastmcp and the REAL fresh-module
                   payloads injected at the _call and inline requests.post
                   boundaries. Failure path: mesh=999 -> {"error": ...}
                   string (images never sent).
"""

import base64
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

from mcp_server import mesh_csg  # noqa: E402  (pure-Python, importable headless)

VALID_VIEWS = {"isometric", "front", "top", "right"}


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


def _fresh_setup_mesh_and_brep(bridge):
    """Fresh doc with a unit-cube mesh + prismatically reconstructed BRep.

    Returns (mesh_ref, body_ref) -- "0" is the first mesh / first BRep.
    """
    call("create_new_document", {"name": "T9_review_QA"})
    nodes, indices = _unit_cube()
    resp = fresh_dispatch("import_mesh_data", {
        "coordinates": nodes,
        "triangle_indices": indices,
        "name": "t9_review_cube",
    })
    assert "error" not in resp, f"import_mesh_data failed: {resp.get('error')}"
    mesh = "0"
    data = fresh_dispatch("extract_mesh_data", {"mesh": mesh})
    assert "error" not in data, f"extract_mesh_data failed: {data.get('error')}"
    tree = mesh_csg.build_csg_tree(data, "prismatic", {})
    tree = mesh_csg.scale_tree(tree, 10.0)  # cm -> mm (reconstruct units=mm)
    resp = fresh_dispatch("create_from_csg_tree", {"csg_tree": tree,
                                                   "units": "mm"})
    assert "error" not in resp, \
        f"create_from_csg_tree failed: {resp.get('error')}"
    assert resp.get("bodies", 0) >= 1, resp
    return mesh, "0"


def _fresh_capture_body_views(params, timeout=240):
    """fresh_dispatch for the capture_body_views command."""
    return fresh_dispatch("capture_body_views", params, timeout=timeout)


def test_capture_body_views_returns_3_png_views(bridge):
    """FAILING-FIRST (pre-implementation): the fresh-module dispatch returns
    {'error': 'Unknown command 'capture_body_views''} because the repo lacks
    the handler. Post-implementation: 3 views, each image_base64 decodes with
    PNG magic, view names in {isometric, front, top}."""
    mesh, body = _fresh_setup_mesh_and_brep(bridge)
    resp = _fresh_capture_body_views({"body": body, "views": ["isometric",
                                                              "front", "top"]})
    assert isinstance(resp, dict), f"expected dict response, got {resp!r}"
    assert "error" not in resp, f"handler errored: {resp['error']}"
    assert isinstance(resp.get("body"), str) and resp["body"], resp
    views = resp.get("views", [])
    assert len(views) == 3, f"expected 3 views, got {len(views)}"
    assert [v["view"] for v in views] == ["isometric", "front", "top"], views
    for v in views:
        b64 = v.get("image_base64", "")
        assert b64, f"view {v['view']} missing image_base64"
        png = base64.b64decode(b64)
        assert png.startswith(b"\x89PNG"), (
            f"view {v['view']} decoded bytes do not start with PNG magic: "
            f"{png[:8]!r}")


def test_capture_body_views_unknown_view_error(bridge):
    """Failure path: an unknown view name must yield the EXACT error string
    (same as T5's capture_mesh_views) before any viewport churn."""
    _fresh_setup_mesh_and_brep(bridge)
    resp = _fresh_capture_body_views(
        {"body": "0", "views": ["isometric", "bogus"]})
    assert resp == {
        "error": "Unknown view 'bogus'. Use isometric, front, top, right"}


def test_capture_body_views_missing_body_error(bridge):
    """Failure path: a nonexistent BRep body reference must yield a graceful
    {'error': ...} (the handler resolves the body before viewport churn)."""
    _fresh_setup_mesh_and_brep(bridge)
    resp = _fresh_capture_body_views({"body": "999"})
    assert isinstance(resp, dict), f"expected dict response, got {resp!r}"
    assert "error" in resp, f"expected graceful error, got {resp!r}"
    assert "not found" in resp["error"].lower(), resp["error"]


def test_review_reconstruction_server_path_happy(bridge, monkeypatch):
    """Server-path validation with the REAL fresh-module payloads injected at
    the _call (compare_mesh_brep) and inline requests.post (capture_mesh_views
    + capture_body_views) boundaries.

    The tool must return [text_envelope, Image x6] interleaved per view (mesh
    then brep); the envelope pairs carry view/mesh_image_base64/
    brep_image_base64 (all PNG-magic), a geometry summary with the
    compare_mesh_to_brep keys, and workflow.stage == 'review'."""
    mesh, body = _fresh_setup_mesh_and_brep(bridge)
    views = ["isometric", "front", "top"]
    real_mesh_cap = fresh_dispatch("capture_mesh_views", {"mesh": mesh,
                                                          "views": views})
    assert "error" not in real_mesh_cap, real_mesh_cap.get("error")
    real_brep_cap = fresh_dispatch("capture_body_views", {"body": body,
                                                          "views": views})
    assert "error" not in real_brep_cap, real_brep_cap.get("error")
    real_compare = fresh_dispatch("compare_mesh_brep", {"mesh": mesh,
                                                        "body": body})
    assert "error" not in real_compare, real_compare.get("error")

    # ---- stub mcp.server.fastmcp so fusion_server.py imports headless ----
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

    spec = importlib.util.spec_from_file_location("fusionmcp_server_dev",
                                                  SERVER_PATH)
    fs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fs)

    # Real fresh-module compare payload at the _call boundary (the geometry
    # summary calls the LOCAL compare_mesh_to_brep, which uses _call).
    monkeypatch.setattr(
        fs, "_call",
        lambda command, params=None, timeout=30: json.dumps(real_compare,
                                                            indent=2))

    # Fake requests.post for the INLINE capture_mesh_views + capture_body_views
    # calls (fs.requests IS the global requests module).
    class _Resp:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    def fake_post(url, json=None, timeout=None):
        cmd = json["command"]
        if cmd == "capture_mesh_views":
            return _Resp(real_mesh_cap)
        if cmd == "capture_body_views":
            return _Resp(real_brep_cap)
        raise AssertionError(f"unexpected command {cmd!r}")

    monkeypatch.setattr(fs.requests, "post", fake_post)

    out = fs.review_reconstruction(mesh=mesh, body=body, views=views)

    assert isinstance(out, list), f"expected list, got {out!r}"
    assert len(out) == 7, f"expected [text, Image x6], got {len(out)} items"
    text = out[0]
    assert isinstance(text, str), f"envelope should be a JSON string, got {text!r}"
    env = json.loads(text)
    pairs = env["pairs"]
    assert len(pairs) == 3, len(pairs)
    for i, pair in enumerate(pairs):
        assert pair["view"] == views[i], (pair["view"], views[i])
        assert base64.b64decode(pair["mesh_image_base64"]).startswith(
            b"\x89PNG"), pair["view"]
        assert base64.b64decode(pair["brep_image_base64"]).startswith(
            b"\x89PNG"), pair["view"]
    geo = env["geometry"]
    assert isinstance(geo, dict) and "error" not in geo, geo
    assert "volume_ratio" in geo, geo
    assert "bbox_max_deviation_cm" in geo, geo
    assert env["workflow"]["stage"] == "review", env["workflow"]
    assert "reconstruct_mesh" in env["workflow"]["next"], env["workflow"]
    assert "select_parameter_schema" in env["workflow"]["next"], env["workflow"]
    # Interleave order: mesh then brep, per view, in views order.
    mesh_by_view = {v["view"]: base64.b64decode(v["image_base64"])
                    for v in real_mesh_cap["views"]}
    brep_by_view = {v["view"]: base64.b64decode(v["image_base64"])
                    for v in real_brep_cap["views"]}
    expected_order = []
    for v in views:
        expected_order.append(mesh_by_view[v])
        expected_order.append(brep_by_view[v])
    for idx, img in enumerate(out[1:], start=1):
        assert isinstance(img, _Image), type(img)
        assert img.data == expected_order[idx - 1], \
            f"image {idx} out of order"
        assert img.data.startswith(b"\x89PNG"), img.data[:8]


def test_review_reconstruction_server_path_failure(bridge, monkeypatch):
    """Failure path: a missing mesh (mesh=999) yields a string envelope
    {"error": ...} -- the tool fails before any images are produced."""
    _fresh_setup_mesh_and_brep(bridge)

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

    spec = importlib.util.spec_from_file_location("fusionmcp_server_dev2",
                                                  SERVER_PATH)
    fs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fs)

    monkeypatch.setattr(fs, "_call",
                        lambda command, params=None, timeout=30: json.dumps(
                            {"error": "Body '0' not found."}, indent=2))

    class _Resp:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    def fake_post(url, json=None, timeout=None):
        return _Resp({"error": "Mesh body '999' not found."})

    monkeypatch.setattr(fs.requests, "post", fake_post)

    out = fs.review_reconstruction(mesh="999", body="0", views=["isometric"])
    assert isinstance(out, str), f"expected error string, got {out!r}"
    data = json.loads(out)
    assert "error" in data, data
    assert "not found" in data["error"].lower(), data["error"]
