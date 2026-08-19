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
  * run_scad: launch -> poll -> complete (result equals the canned sync
    output string); ``job_id="sync"`` equals exactly what the mocked ``_call``
    would return; an unknown uuid polls ``not_found``; a mocked ``_call``
    raising ``requests.exceptions.ReadTimeout`` records a job ``failed``.
  * review_reconstruction: the async job result is JSON-safe
    (``json.loads(json.dumps(result))`` round-trips; ``text`` plus
    ``views`` with ``image_base64`` strings, no Image objects).
  * structure_graph: launch -> poll -> complete (async result is the summary);
    a saturated pool reports the extra launch as ``queued`` with a ``position``
    key (new status vocabulary: queued/running/complete/failed/not_found).
"""

import base64
import importlib
import importlib.util
import json
import os
import queue
import sys
import threading
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
    """Poll ``tool_fn(job_id=job_id)`` until its status leaves
    'queued'/'running' (i.e. reaches a terminal complete/failed/not_found)."""
    end = time.time() + deadline
    while True:
        status = json.loads(tool_fn(job_id=job_id))
        if status["status"] not in ("queued", "running"):
            return status
        if time.time() > end:
            raise AssertionError(
                f"job {job_id} still queued/running after {deadline:.1f}s: "
                f"{status}")
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
    """job_id='' launches (status queued or running + job_id); polling the id
    reaches complete with result == the canned sync output string."""
    fake = _CommandCall({"run_scad": CANNED_SCAD_SYNC})
    monkeypatch.setattr(fs, "_call", fake)

    raw = fs.run_scad(code="cube([10,10,10]);")
    launch = json.loads(raw)
    # The pool may report 'queued' when saturated; accept either active status.
    assert launch["status"] in ("queued", "running")
    assert launch["job_id"]

    status = _poll_tool(
        fs, lambda **kw: fs.run_scad(code="cube([10,10,10]);", **kw),
        launch["job_id"])
    assert status["status"] == "complete"
    assert status["result"] == CANNED_SCAD_SYNC


def test_run_scad_unknown_uuid_not_found(fs):
    """An unknown job id polls a not_found envelope (no _call ever fires)."""
    job_id = "00000000000000000000000000000000"
    raw = fs.run_scad(code="cube([10,10,10]);", job_id=job_id)
    assert json.loads(raw) == {"job_id": job_id, "status": "not_found"}


def test_run_scad_transport_failure_records_job_error(fs, monkeypatch):
    """A mocked _call raising ReadTimeout surfaces as a job failure whose
    message names the exception (the launch envelope still returns an active
    status)."""
    fake = _CommandCall(
        {}, raise_on={"run_scad": requests.exceptions.ReadTimeout(
            "ReadTimeout")})
    monkeypatch.setattr(fs, "_call", fake)

    raw = fs.run_scad(code="cube([10,10,10]);")
    launch = json.loads(raw)
    assert launch["status"] in ("queued", "running")

    status = _poll_tool(
        fs, lambda **kw: fs.run_scad(code="cube([10,10,10]);", **kw),
        launch["job_id"])
    assert status["status"] == "failed"
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
    assert launch["status"] in ("queued", "running")
    status = _poll_tool(
        fs, lambda **kw: fs.review_reconstruction(mesh="0", body="0",
                                                  views=_VIEWS, **kw),
        launch["job_id"])
    assert status["status"] == "complete"
    result = status["result"]
    assert json.loads(json.dumps(result)) == result
    assert isinstance(result["text"], str)
    assert len(result["views"]) == 2 * len(_VIEWS)
    for view in result["views"]:
        assert "view" in view and view["image_base64"]


# ---------------------------------------------------------------------------
# structure_graph (job-enabled tool: launch -> poll -> complete)
# ---------------------------------------------------------------------------

# Canned extract_mesh_data payload: the canonical box from
# tests/test_mesh_graph.py (8 nodes / 12 triangles / 6 planar faces).
CANNED_MESH_DATA = json.dumps({
    "mesh": "0",
    "nodes": [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0),
              (0, 0, 2), (2, 0, 2), (2, 2, 2), (0, 2, 2)],
    "indices": [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
                (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
                (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)],
    "normals": [],
}, indent=2)


def test_structure_graph_launch_poll_complete(fs, monkeypatch):
    """structure_graph's async path: launching with no job_id returns an
    envelope with a job_id and an active status; polling the id reaches
    'complete' with the summary JSON as the job result."""
    fake = _CommandCall({"extract_mesh_data": CANNED_MESH_DATA})
    monkeypatch.setattr(fs, "_call", fake)

    raw = fs.structure_graph(mesh="0")
    launch = json.loads(raw)
    assert launch["status"] in ("queued", "running")
    assert launch["job_id"]

    status = _poll_tool(
        fs, lambda **kw: fs.structure_graph(mesh="0", **kw),
        launch["job_id"])
    assert status["status"] == "complete"
    parsed = json.loads(status["result"])
    assert parsed["mesh"] == "0"
    assert parsed["component_count"] == 1
    assert parsed["face_count"] == 6


# ---------------------------------------------------------------------------
# pool saturation (queued + position)
# ---------------------------------------------------------------------------

def _wait_for_status(fs, job_id, statuses, deadline=30.0):
    """Poll ``fs._job_status(job_id)`` until its status is one of *statuses*."""
    end = time.time() + deadline
    while True:
        status = fs._job_status(job_id)
        if status["status"] in statuses:
            return status
        if time.time() > end:
            raise AssertionError(
                f"job {job_id} did not reach {statuses!r} within "
                f"{deadline:.1f}s: {status}")
        time.sleep(0.02)


def test_pool_saturation_queued_with_position(fs, monkeypatch):
    """With the pool saturated, the extra launch reports 'queued' with a
    position key; releasing the gate drains both jobs to 'complete'."""
    # fusion_server's _launch_job imports the TOP-LEVEL `jobs` module
    # (mcp_server/jobs.py via MCP_SERVER_DIR on sys.path) -- not the
    # `mcp_server.jobs` package module -- so patch that exact instance.
    jobs = importlib.import_module("jobs")

    # Quarantine workers started by earlier tests: old workers are parked on
    # the OLD queue forever, so new launches flow only through the fresh
    # single-worker pool this test sets up.
    monkeypatch.setattr(jobs, "MAX_CONCURRENT", 1)
    monkeypatch.setattr(jobs, "_WORKERS_STARTED", False)
    monkeypatch.setattr(jobs, "_QUEUE", queue.Queue())

    gate = threading.Event()

    def gated_call(command, params=None, timeout=30):
        gate.wait(timeout=30)
        return CANNED_MESH_DATA

    monkeypatch.setattr(fs, "_call", gated_call)

    raw1 = fs.structure_graph(mesh="0")
    launch1 = json.loads(raw1)
    assert launch1["job_id"]
    assert launch1["status"] in ("queued", "running")
    job1 = launch1["job_id"]
    _wait_for_status(fs, job1, ("running",))

    raw2 = fs.structure_graph(mesh="0")
    launch2 = json.loads(raw2)
    assert launch2["job_id"]
    assert launch2["status"] in ("queued", "running")
    job2 = launch2["job_id"]
    env2 = fs._job_status(job2)
    assert env2["status"] == "queued"
    assert env2["position"] == 0

    gate.set()
    assert _wait_for_status(fs, job1, ("complete",))["status"] == "complete"
    assert _wait_for_status(fs, job2, ("complete",))["status"] == "complete"
