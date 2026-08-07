#!/usr/bin/env python3
"""
Headless tests for the async job scheme on the job-enabled tools
(.omo/plans/async-job-scheme.md, Todo 3).

Fusion is NOT available, so the transport is mocked: ``fusion_server._call``
returns canned add-in JSON keyed on the command name, and (for the image
tools) ``fusion_server.requests.post`` is faked.  The ``mcp_server/``
directory is placed on ``sys.path`` so the lazy sibling imports
``from jobs import get_job`` / ``from jobs import launch_job`` inside
``_job_status`` / ``_launch_job`` resolve -- the same mechanism as production,
where the script directory is on ``sys.path``.

Coverage:
  * run_scad: launch -> poll -> completed (result equals the canned sync
    output string); ``job_id="sync"`` equals exactly what the mocked ``_call``
    would return; an unknown uuid polls ``not_found``; a mocked ``_call``
    raising ``requests.exceptions.ReadTimeout`` records a job ``error``.
  * review_reconstruction: the async job result is JSON-safe
    (``json.loads(json.dumps(result))`` round-trips; ``text`` plus
    ``views`` with ``image_base64`` strings, no Image objects).
"""

import base64
import importlib.util
import json
import os
import sys
import time
import types

import pytest
import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

MCP_SERVER_DIR = os.path.join(REPO_ROOT, "mcp_server")
if MCP_SERVER_DIR not in sys.path:
    # Resolves the lazy `from jobs import ...` in _job_status/_launch_job.
    sys.path.insert(0, MCP_SERVER_DIR)

SERVER_PATH = os.path.join(MCP_SERVER_DIR, "fusion_server.py")


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


@pytest.fixture(scope="module")
def fs():
    """Load fusion_server.py headless with a stubbed mcp.server.fastmcp."""
    stub_mcp = types.ModuleType("mcp")
    stub_server = types.ModuleType("mcp.server")
    stub_fastmcp = types.ModuleType("mcp.server.fastmcp")
    stub_fastmcp.FastMCP = _FastMCP
    stub_fastmcp.Image = _Image
    stub_server.fastmcp = stub_fastmcp
    for name, mod in (("mcp", stub_mcp), ("mcp.server", stub_server),
                      ("mcp.server.fastmcp", stub_fastmcp)):
        sys.modules[name] = mod

    spec = importlib.util.spec_from_file_location("fusionmcp_server_jobs",
                                                  SERVER_PATH)
    fs_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fs_mod)
    return fs_mod


# ---------------------------------------------------------------------------
# transport fakes (mimic _call's json.dumps(data, indent=2) envelope)
# ---------------------------------------------------------------------------

class _CommandCall:
    """Fake ``_call`` keyed on the add-in command name.

    ``responses`` maps command -> pre-serialized JSON string (exactly what the
    real ``_call`` returns).  ``raise_on`` maps command -> exception to raise
    (exercises the job error path).
    """

    def __init__(self, responses=None, raise_on=None):
        self.responses = responses or {}
        self.raise_on = raise_on or {}
        self.calls = []

    def __call__(self, command, params=None, timeout=30):
        self.calls.append((command, params, timeout))
        if command in self.raise_on:
            raise self.raise_on[command]
        if command not in self.responses:
            raise AssertionError(f"unexpected _call command {command!r}")
        return self.responses[command]


def _poll_tool(fs, tool_fn, job_id, deadline=10.0):
    """Poll ``tool_fn(job_id=job_id)`` until its status leaves 'running'."""
    end = time.time() + deadline
    while True:
        status = json.loads(tool_fn(job_id=job_id))
        if status["status"] != "running":
            return status
        if time.time() > end:
            raise AssertionError(
                f"job {job_id} still running after {deadline:.1f}s: {status}")
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# run_scad
# ---------------------------------------------------------------------------

CANNED_SCAD_DATA = {
    "mesh": "scad_mesh_1",
    "bodies": 1,
    "names": ["scad_mesh_1"],
}
CANNED_SCAD_SYNC = json.dumps(CANNED_SCAD_DATA, indent=2)


def test_run_scad_sync_equals_call_output(fs, monkeypatch):
    """job_id='sync' returns exactly the json.dumps(data, indent=2) string that
    the (mocked) _call produces for the canned dict."""
    fake = _CommandCall({"run_scad": CANNED_SCAD_SYNC})
    monkeypatch.setattr(fs, "_call", fake)
    expected = fake("run_scad", {"code": "cube([10,10,10]);"})
    assert expected == CANNED_SCAD_SYNC
    raw = fs.run_scad(code="cube([10,10,10]);", job_id="sync")
    assert raw == expected


def test_run_scad_launch_poll_completed(fs, monkeypatch):
    """job_id='' launches (status running + job_id); polling the id reaches
    completed with result == the canned sync output string."""
    fake = _CommandCall({"run_scad": CANNED_SCAD_SYNC})
    monkeypatch.setattr(fs, "_call", fake)

    raw = fs.run_scad(code="cube([10,10,10]);")
    launch = json.loads(raw)
    assert launch["status"] == "running"
    assert launch["job_id"]

    status = _poll_tool(
        fs, lambda **kw: fs.run_scad(code="cube([10,10,10]);", **kw),
        launch["job_id"])
    assert status["status"] == "completed"
    assert status["result"] == CANNED_SCAD_SYNC


def test_run_scad_unknown_uuid_not_found(fs):
    """An unknown job id polls a not_found envelope (no _call ever fires)."""
    job_id = "00000000000000000000000000000000"
    raw = fs.run_scad(code="cube([10,10,10]);", job_id=job_id)
    assert json.loads(raw) == {"job_id": job_id, "status": "not_found"}


def test_run_scad_transport_failure_records_job_error(fs, monkeypatch):
    """A mocked _call raising ReadTimeout surfaces as a job error whose message
    names the exception (the launch envelope still returns running)."""
    fake = _CommandCall(
        {}, raise_on={"run_scad": requests.exceptions.ReadTimeout(
            "ReadTimeout")})
    monkeypatch.setattr(fs, "_call", fake)

    raw = fs.run_scad(code="cube([10,10,10]);")
    launch = json.loads(raw)
    assert launch["status"] == "running"

    status = _poll_tool(
        fs, lambda **kw: fs.run_scad(code="cube([10,10,10]);", **kw),
        launch["job_id"])
    assert status["status"] == "error"
    assert "ReadTimeout" in status["error"]


# ---------------------------------------------------------------------------
# review_reconstruction (image tools: JSON-safe async result)
# ---------------------------------------------------------------------------

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _b64png():
    return base64.b64encode(_PNG).decode("ascii")


_VIEWS = ["isometric", "front", "top"]
_MESH_VIEWS = [{"view": v, "image_base64": _b64png()} for v in _VIEWS]
_BREP_VIEWS = [{"view": v, "image_base64": _b64png()} for v in _VIEWS]
CANNED_GEOMETRY = json.dumps({
    "method": "surface_evaluator",
    "volume_ratio": 1.0,
    "bbox_max_deviation_cm": 0.001,
    "sampled_deviation_cm": {"mean": 0.0005, "max": 0.002, "samples": 200},
}, indent=2)


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_review_reconstruction_async_result_is_json_safe(fs, monkeypatch):
    """review_reconstruction's job result round-trips json.loads and carries
    text + views with image_base64 strings (no Image objects)."""
    class _ReviewCall:
        def __call__(self, command, params=None, timeout=30):
            assert command == "compare_mesh_brep", command
            return CANNED_GEOMETRY

    monkeypatch.setattr(fs, "_call", _ReviewCall())

    def fake_post(url, json=None, timeout=None):
        cmd = json["command"]
        if cmd == "capture_mesh_views":
            return _Resp({"views": _MESH_VIEWS})
        if cmd == "capture_body_views":
            return _Resp({"views": _BREP_VIEWS})
        raise AssertionError(f"unexpected requests.post command {cmd!r}")

    monkeypatch.setattr(fs.requests, "post", fake_post)

    # The sync path still produces the current [text, Image x6] list.
    out = fs.review_reconstruction(mesh="0", body="0", views=_VIEWS,
                                   job_id="sync")
    assert isinstance(out, list) and len(out) == 7, len(out)
    for img in out[1:]:
        assert isinstance(img, _Image)
        assert img.data.startswith(b"\x89PNG")

    # The async path stores the JSON-safe dict as the job result.
    raw = fs.review_reconstruction(mesh="0", body="0", views=_VIEWS)
    launch = json.loads(raw)
    assert launch["status"] == "running"
    status = _poll_tool(
        fs, lambda **kw: fs.review_reconstruction(mesh="0", body="0",
                                                  views=_VIEWS, **kw),
        launch["job_id"])
    assert status["status"] == "completed"
    result = status["result"]
    assert json.loads(json.dumps(result)) == result
    assert isinstance(result["text"], str)
    assert len(result["views"]) == 2 * len(_VIEWS)
    for view in result["views"]:
        assert "view" in view and view["image_base64"]
