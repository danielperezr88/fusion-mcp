#!/usr/bin/env python3
"""
Live test for the `capture_screenshot` -> `image_base64` fix
(.omo/plans/mesh-to-parametric.md, Todo 1 -- vision prerequisite).

The RUNNING add-in copy in Fusion 360 lags the repo and does NOT send
`image_base64`, so the repo copy of FusionMCP.py is driven INSIDE Fusion via
execute_script + the fresh-module pattern (importlib spec_from_file_location
-> mod.app = app -> _process_command), mirroring tests/test_openscad_live.py
(lines 128-148).

pytest behaviour: probes the bridge first; skips cleanly when Fusion is not
reachable (so the headless `py -m pytest tests -v` run stays green), and runs
the real assertions when Fusion is live.

Checks:
  * happy path  -- response contains `image_base64`; base64.b64decode() starts
                   with PNG magic b"\\x89PNG"; `screenshot` + `size` keys
                   unchanged (backward compatible)
  * failure path -- invalid path (nonexistent directory) -> graceful
                    {"error": ...}, never a crash
"""

import base64
import os
import sys

import pytest
import requests

BASE_URL = "http://127.0.0.1:7432/command"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUSIONMCP_PATH = os.path.join(REPO_ROOT, "FusionMCP.py")
SCREENSHOT_PATH = os.path.join(
    os.path.expanduser("~"), "Desktop", "fusion_capture_live_test.png")
# An existing directory used as the save path: saveAsImageFile raises there
# (unlike a nonexistent directory, which Fusion silently auto-creates), so the
# handler must surface a graceful {"error": ...} via _process_command.
BAD_SCREENSHOT_PATH = os.path.expanduser("~")


class BridgeError(Exception):
    """Raised when the Fusion HTTP bridge cannot be reached."""


def call(command, params=None, timeout=180):
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


def run_code(code, timeout=180):
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
    switching to the new document; a capture right afterwards can grab the
    PREVIOUS document's viewport. Activate the newest document explicitly and
    poll until its design owns the active product. Never raises: if the check
    cannot be satisfied the caller keeps going.
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


def fresh_capture_screenshot(params, timeout=180):
    """Drive the repo FusionMCP.py _capture_screenshot in a fresh module.

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
        "_out = _mod._process_command({'command': 'capture_screenshot', "
        "'params': %s})\n"
        "result['output'] = _out\n"
    ) % (repr(FUSIONMCP_PATH), repr(params))
    return run_code(inner, timeout=timeout)


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
    """Create a fresh document (capture_screenshot needs an active viewport /
    document) and close it -- plus any document the test creates --
    afterwards. Self-contained: never depends on documents left open by
    earlier tests."""
    pre_ids = _open_doc_ids()
    resp = call("create_new_document", {"name": "T1_screenshot_QA"})
    assert "error" not in resp, f"create_new_document failed: {resp}"
    _activate_last_doc()
    yield
    _close_docs_except(pre_ids)


def test_capture_screenshot_returns_image_base64(bridge):
    """FAILING-FIRST (post-fix target): the response must contain
    `image_base64` whose base64.b64decode() starts with PNG magic
    b"\\x89PNG", while `screenshot` + `size` stay backward compatible."""
    params = {"path": SCREENSHOT_PATH, "width": 1920, "height": 1080}
    resp = fresh_capture_screenshot(params)
    assert isinstance(resp, dict), f"expected dict response, got {resp!r}"
    assert "error" not in resp, f"handler errored: {resp['error']}"
    b64 = resp.get("image_base64")
    assert b64, f"image_base64 missing or empty: {resp}"
    png = base64.b64decode(b64)
    assert png.startswith(b"\x89PNG"), (
        f"decoded bytes do not start with PNG magic: {png[:8]!r}")
    assert resp.get("screenshot") == SCREENSHOT_PATH, resp
    assert resp.get("size") == "1920x1080", resp


def test_capture_screenshot_invalid_path_graceful_error(bridge):
    """Failure path: an existing directory used as the save path must yield
    a graceful {"error": ...} dict (via _process_command's handler wrapper),
    never a crash."""
    params = {"path": BAD_SCREENSHOT_PATH}
    resp = fresh_capture_screenshot(params)
    assert isinstance(resp, dict), f"expected dict response, got {resp!r}"
    assert "error" in resp, f"expected graceful error, got {resp!r}"
