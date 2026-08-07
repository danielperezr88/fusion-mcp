#!/usr/bin/env python3
"""
Live test for `capture_mesh_views` (FusionMCP.py) + `annotate_mesh_parameters`
(mcp_server/fusion_server.py) -- .omo/plans/mesh-to-parametric.md, Todo 5
(vision-guided annotation, Wave 3 first lane).

The RUNNING add-in copy in Fusion 360 lags the repo and has no
`capture_mesh_views` dispatcher entry, so the repo copy of FusionMCP.py is
driven INSIDE Fusion via execute_script + the fresh-module pattern
(importlib spec_from_file_location -> mod.app = app -> _process_command),
mirroring tests/test_capture_screenshot_live.py.

pytest behaviour: probes the bridge first; skips cleanly when Fusion is not
reachable (so the headless `py -m pytest tests -v` run stays green), and runs
the real assertions when Fusion is live.

Checks:
  * happy path  -- 4 views; every image_base64 decodes to PNG magic
                   b"\\x89PNG"; view names in {isometric, front, top, right};
                   handler returns {"mesh": str, "views": [...]}
  * failure path -- unknown view name -> EXACT error string; missing mesh ->
                    graceful {"error": ...} (never a crash)
  * server path -- annotate_mesh_parameters returns
                   [text_envelope, Image, Image, Image, Image]: envelope has
                   non-empty measured_facts and workflow.stage == "annotate";
                   validated headless with a stubbed mcp.server.fastmcp and
                   the REAL fresh-module payloads injected at the _call and
                   inline requests.post boundaries.
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


def _activate_last_doc():
    """Pin app.activeProduct to the newest document before the test body runs.

    create_new_document returns before Fusion's activeProduct finishes
    switching to the new document; an import right afterwards can land in the
    PREVIOUS document (intermittent 'Mesh body 0 not found' flake). Activate
    the newest document explicitly and poll until its design owns the active
    product. Never raises: if the check cannot be satisfied the caller keeps
    going (import may land in the wrong doc -- a pre-existing flake).
    """
    try:
        return run_code(
            "import time\n"
            "_d = app.documents.item(app.documents.count - 1)\n"
            "_d.activate()\n"
            "_ok = False\n"
            "for _t in range(15):\n"
            "    _p = app.activeProduct\n"
            "    if _p is not None and hasattr(_p, 'parentDocument'):\n"
            "        _pd = _p.parentDocument\n"
            "        if _pd is not None and _pd.creationId == _d.creationId:\n"
            "            _ok = True\n"
            "            break\n"
            "    time.sleep(0.2)\n"
            "result['output'] = {'ok': _ok, 'doc': _d.creationId}\n")
    except Exception:
        return None


def fresh_dispatch(command, params, timeout=240):
    """Drive the repo FusionMCP.py handler in a fresh module inside Fusion.

    Loads the LOCAL FusionMCP.py as 'fusionmcp_dev', sets mod.app = app
    (module-level 'app' is None outside run()), then dispatches through
    _process_command so the full handler path is exercised. Params are
    embedded with repr(). The handler dict is returned via result['output'].
    """
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


def fresh_capture_mesh_views(params, timeout=240):
    """fresh_dispatch for the capture_mesh_views command."""
    return fresh_dispatch("capture_mesh_views", params, timeout=timeout)


def _mesh_body_count():
    inner = (
        "import importlib.util as _ilu\n"
        "import sys as _sys\n"
        "_spec = _ilu.spec_from_file_location('fusionmcp_dev', %s)\n"
        "_mod = _ilu.module_from_spec(_spec)\n"
        "_sys.modules['fusionmcp_dev'] = _mod\n"
        "_spec.loader.exec_module(_mod)\n"
        "_mod.app = app\n"
        "result['output'] = "
        "{'mesh_bodies': _mod._design().rootComponent.meshBodies.count}\n"
    ) % (repr(FUSIONMCP_PATH),)
    return run_code(inner)


def _unit_cube():
    """Outward-wound unit cube [0,1]^3 as flat node/index/normal lists."""
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
    normals = [
        0, 0, -1,  0, 0, -1,
        0, 0, 1,   0, 0, 1,
        0, -1, 0,  0, -1, 0,
        0, 1, 0,   0, 1, 0,
        -1, 0, 0,  -1, 0, 0,
        1, 0, 0,   1, 0, 0,
    ]
    return nodes, indices, normals


def _ensure_mesh():
    """Make sure at least one mesh body exists; import a unit cube if empty.

    Reuses the T2/T3 fresh-module import_mesh_data approach (the running
    add-in's dispatcher lacks import_mesh_data too). Returns '0' (index of
    the first mesh body).
    """
    # _mesh_body_count() returns {'mesh_bodies': N} -- comparing the whole
    # dict to 0 is always False, so the import below silently never ran and
    # the tests depended on a leftover mesh being present in the active doc
    # (the historical flake). Read the count out of the dict instead.
    if _mesh_body_count().get("mesh_bodies", 0) == 0:
        nodes, indices, normals = _unit_cube()
        resp = fresh_dispatch("import_mesh_data", {
            "coordinates": nodes,
            "triangle_indices": indices,
            "normals": normals,
            "name": "t5_annotate_cube",
        })
        assert "error" not in resp, f"import_mesh_data failed: {resp.get('error')}"
    return "0"


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
def _fresh_doc_and_close(bridge):
    """Create a fresh document (_ensure_mesh needs an active document with a
    mesh body) and close it -- plus any document the test creates --
    afterwards. Self-contained: never depends on documents left open by
    earlier tests."""
    pre_ids = _open_doc_ids()
    resp = call("create_new_document", {"name": "T5_annotate_QA"})
    assert "error" not in resp, f"create_new_document failed: {resp}"
    _activate_last_doc()
    yield
    _close_docs_except(pre_ids)


def test_capture_mesh_views_returns_4_png_views(bridge):
    """FAILING-FIRST (pre-implementation): the fresh-module dispatch returns
    {'error': 'Unknown command 'capture_mesh_views''} because the repo lacks
    the handler. Post-implementation: 4 views, each image_base64 decodes with
    PNG magic, view names in {isometric, front, top, right}."""
    mesh = _ensure_mesh()
    resp = fresh_capture_mesh_views({"mesh": mesh})
    assert isinstance(resp, dict), f"expected dict response, got {resp!r}"
    assert "error" not in resp, f"handler errored: {resp['error']}"
    assert isinstance(resp.get("mesh"), str) and resp["mesh"], resp
    views = resp.get("views", [])
    assert len(views) == 4, f"expected 4 views, got {len(views)}"
    assert {v["view"] for v in views} == VALID_VIEWS, views
    for v in views:
        b64 = v.get("image_base64", "")
        assert b64, f"view {v['view']} missing image_base64"
        png = base64.b64decode(b64)
        assert png.startswith(b"\x89PNG"), (
            f"view {v['view']} decoded bytes do not start with PNG magic: "
            f"{png[:8]!r}")


def test_capture_mesh_views_unknown_view_error(bridge):
    """Failure path: an unknown view name must yield the EXACT error string
    before any viewport churn (views validated before capture)."""
    _ensure_mesh()
    resp = fresh_capture_mesh_views(
        {"mesh": "0", "views": ["isometric", "bogus"]})
    assert resp == {
        "error": "Unknown view 'bogus'. Use isometric, front, top, right"}


def test_capture_mesh_views_missing_mesh_error(bridge):
    """Failure path: a nonexistent mesh reference must yield a graceful
    {'error': ...} (the handler resolves the mesh before viewport churn)."""
    resp = fresh_capture_mesh_views({"mesh": "999"})
    assert isinstance(resp, dict), f"expected dict response, got {resp!r}"
    assert "error" in resp, f"expected graceful error, got {resp!r}"
    assert "not found" in resp["error"].lower(), resp["error"]


def test_annotate_mesh_parameters_server_path(bridge, monkeypatch):
    """Server-path validation with the REAL fresh-module payloads injected at
    the _call and inline requests.post boundaries (the running add-in cannot
    serve capture_mesh_views over HTTP -- dispatcher lags the repo).

    The tool must return [text_envelope, Image, Image, Image, Image] where the
    envelope has a non-empty measured_facts report, 4 views, and
    workflow.stage == 'annotate'."""
    mesh = _ensure_mesh()
    real_extract = fresh_dispatch("extract_mesh_data", {"mesh": mesh})
    assert "error" not in real_extract, real_extract.get("error")
    real_capture = fresh_capture_mesh_views({"mesh": mesh})
    assert "error" not in real_capture, real_capture.get("error")
    assert len(real_capture.get("views", [])) == 4

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

    # Real fresh-module extract payload at the _call boundary.
    monkeypatch.setattr(
        fs, "_call",
        lambda command, params=None, timeout=30: json.dumps(real_extract,
                                                            indent=2))

    # Fake requests.post for the INLINE capture_mesh_views call.
    class _Resp:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    def fake_post(url, json=None, timeout=None):
        assert json["command"] == "capture_mesh_views", json
        return _Resp(real_capture)

    monkeypatch.setattr(fs.requests, "post", fake_post)

    out = fs.annotate_mesh_parameters(
        mesh=mesh, views=["isometric", "front", "top", "right"], units="cm",
        job_id="sync")

    assert isinstance(out, list), f"expected list, got {out!r}"
    assert len(out) == 5, f"expected [text, Image x4], got {len(out)} items"
    text = out[0]
    assert isinstance(text, str), f"envelope should be a JSON string, got {text!r}"
    env = json.loads(text)
    assert env["mesh"] == real_extract.get("mesh", mesh), env["mesh"]
    assert len(env["views"]) == 4, len(env["views"])
    facts = env["measured_facts"]
    assert isinstance(facts, dict) and facts, facts
    assert "bounding_box_cm" in facts, facts
    for img in out[1:]:
        assert isinstance(img, _Image), type(img)
        assert img.data.startswith(b"\x89PNG"), img.data[:8]
