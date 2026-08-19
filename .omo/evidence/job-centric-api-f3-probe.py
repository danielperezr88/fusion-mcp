#!/usr/bin/env python3
"""
F3 -- Hands-on live QA probe for the job-centric API
(.omo/plans/job-centric-api.md, Final Verification Wave).

Loads mcp_server/fusion_server.py HEADLESS (stubbed mcp.server.fastmcp,
importlib.util.spec_from_file_location, mcp_server on sys.path[0]) and drives
the REAL bridge at http://127.0.0.1:7432 via fresh-module dispatch of the repo
FusionMCP.py handler inside Fusion 360 (the established live-test pattern --
the running add-in copy may lag the repo). The server module's job lifecycle
(launch_job / worker pool / _job_status / _structure_graph_sync with the real
mesh_analysis + mesh_graph + DuckDB persist) runs for real; only the transport
(extract_mesh_data / create_new_document / import_mesh_data) is routed through
the fresh repo handler.

Exit code 0 = all 6 criteria PASS (verdict APPROVE); 1 = any FAIL (REJECT).
If the bridge is down the probe prints `BRIDGE_DOWN_SKIPPED` and exits 0 so the
verdict is SKIPPED, not a rejection.

Criteria:
  C1 structure_graph job lifecycle (launch -> poll -> complete; DuckDB queryable)
  C2 analyze_mesh job (launch -> poll -> complete)
  C3 not_found (job_id="deadbeef" on a job-enabled tool)
  C4 sync byte-identity (job_id="sync" summary == complete job result)
  C5 failure path (analyze_mesh mesh="999" -> launch -> poll -> failed)
  C6 pool saturation (MAX_CONCURRENT=1 + Event-gated _call -> queued+position)
"""

import importlib
import importlib.util
import json
import os
import queue
import sys
import threading
import time
import types

import requests

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
assert os.path.isdir(REPO_ROOT), f"repo root not found: {REPO_ROOT}"
MCP_SERVER_DIR = os.path.join(REPO_ROOT, "mcp_server")
FUSIONMCP_PATH = os.path.join(REPO_ROOT, "FusionMCP.py")
SERVER_PATH = os.path.join(MCP_SERVER_DIR, "fusion_server.py")
BASE_URL = "http://127.0.0.1:7432/command"

# Put mcp_server on sys.path so the lazy `from jobs import ...` resolves.
if MCP_SERVER_DIR not in sys.path:
    sys.path.insert(0, MCP_SERVER_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ---------------------------------------------------------------------------
# bridge transport helpers (mirror tests/test_review_reconstruction_live.py)
# ---------------------------------------------------------------------------

def call(command, params=None, timeout=300):
    params = params or {}
    r = requests.post(BASE_URL, json={"command": command, "params": params},
                     timeout=timeout)
    return r.json()


def run_code(code, timeout=300):
    resp = call("execute_script", {"code": code}, timeout=timeout)
    if isinstance(resp, dict) and "error" in resp:
        raise RuntimeError("execute_script failed:\n" + str(resp["error"])[:2000])
    return resp.get("output")


def fresh_dispatch(command, params, timeout=300):
    """Drive the repo FusionMCP.py handler in a fresh module inside Fusion."""
    inner = (
        "import importlib.util as _ilu\n"
        "import sys as _sys\n"
        "_spec = _ilu.spec_from_file_location('fusionmcp_f3', %s)\n"
        "_mod = _ilu.module_from_spec(_spec)\n"
        "_sys.modules['fusionmcp_f3'] = _mod\n"
        "_spec.loader.exec_module(_mod)\n"
        "_mod.app = app\n"
        "_out = _mod._process_command({'command': %s, 'params': %s})\n"
        "result['output'] = _out\n"
    ) % (repr(FUSIONMCP_PATH), repr(command), repr(params))
    return run_code(inner, timeout=timeout)


def open_doc_ids():
    try:
        out = run_code(
            "_ids = []\n"
            "for _i in range(app.documents.count):\n"
            "    _ids.append(app.documents.item(_i).creationId)\n"
            "result['output'] = _ids\n")
    except Exception:
        return None
    return out if isinstance(out, list) else None


def close_docs_except(pre_ids):
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


def unit_cube():
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


# ---------------------------------------------------------------------------
# headless load of mcp_server/fusion_server.py
# ---------------------------------------------------------------------------

def load_server():
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
        if name not in sys.modules:
            sys.modules[name] = mod

    spec = importlib.util.spec_from_file_location("fusionmcp_server_f3",
                                                  SERVER_PATH)
    fs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fs)

    # Route the server's transport through the fresh repo handler inside Fusion
    # (the running add-in copy may lag the repo). The heavy lifting
    # (decompose/build/persist/job lifecycle) still runs in the server module.
    def patched_call(command, params=None, timeout=300):
        params = params or {}
        out = fresh_dispatch(command, params, timeout=timeout)
        if isinstance(out, dict) and "error" in out:
            return f"Error: {out['error']}"
        return json.dumps(out, indent=2)

    fs._call = patched_call
    return fs


def poll(fs, tool_fn, job_id, deadline=180.0, sleep=0.2):
    """Poll tool_fn(job_id=job_id) until status leaves queued/running."""
    end = time.time() + deadline
    last = None
    while True:
        raw = tool_fn(job_id=job_id)
        status = json.loads(raw)
        last = status
        if status["status"] not in ("queued", "running"):
            return status
        if time.time() > end:
            raise AssertionError(
                f"job {job_id} still {status['status']} after {deadline:.1f}s: "
                f"{status}")
        time.sleep(sleep)


def wait_for_status(fs, job_id, statuses, deadline=60.0, sleep=0.02):
    end = time.time() + deadline
    while True:
        status = fs._job_status(job_id)
        if status["status"] in statuses:
            return status
        if time.time() > end:
            raise AssertionError(
                f"job {job_id} did not reach {statuses!r} within "
                f"{deadline:.1f}s: {status}")
        time.sleep(sleep)


# ---------------------------------------------------------------------------
# criteria
# ---------------------------------------------------------------------------

RESULTS = []


def record(criterion, ok, detail):
    RESULTS.append((criterion, bool(ok), detail))
    tag = "PASS" if ok else "FAIL"
    print(f"  [{criterion}] {tag}: {detail}")


def main():
    print("=== F3 hands-on live QA: job-centric API ===")
    print(f"python={sys.version.split()[0]} repo={REPO_ROOT}")

    # ---- probe bridge first ----
    try:
        r = requests.post(BASE_URL, json={"command": "get_info", "params": {}},
                          timeout=10)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"BRIDGE_DOWN_SKIPPED: {e}")
        print("VERDICT: SKIPPED (bridge unreachable)")
        sys.exit(0)
    print(f"[bridge] reachable (HTTP {r.status_code})")

    pre_ids = open_doc_ids()
    print(f"[bridge] pre-existing open docs: {len(pre_ids or [])}")

    fs = load_server()
    print(f"[server] loaded fusion_server headless; _call routed via fresh_dispatch")

    try:
        # ---- fresh doc + unit-cube mesh ----
        print("--- setup: fresh document + unit-cube mesh ---")
        fresh_dispatch("create_new_document", {"name": "F3_job_API_QA"})
        nodes, indices = unit_cube()
        resp = fresh_dispatch("import_mesh_data", {
            "coordinates": nodes, "triangle_indices": indices,
            "name": "f3_unit_cube"})
        assert "error" not in resp, f"import_mesh_data failed: {resp}"
        print(f"[setup] doc created; unit-cube mesh imported -> {resp}")

        # ===== C1 structure_graph job lifecycle (PRIME TARGET) =====
        print("=== C1 structure_graph job lifecycle ===")
        raw = fs.structure_graph(mesh="0")
        launch = json.loads(raw)
        print(f"  launch envelope: {launch}")
        assert "job_id" in launch and launch["job_id"], "missing job_id"
        assert launch["status"] in ("queued", "running"), \
            f"unexpected launch status: {launch['status']}"
        job1 = launch["job_id"]
        st = poll(fs, lambda **kw: fs.structure_graph(mesh="0", **kw), job1,
                  deadline=180.0)
        print(f"  poll terminal: status={st['status']}")
        if st["status"] != "complete":
            record("C1", False, f"structure_graph did not complete: {st}")
        else:
            result = st["result"]
            summ = json.loads(result) if isinstance(result, str) else result
            cc = summ.get("component_count")
            fc = summ.get("face_count")
            ok = isinstance(cc, int) and cc >= 1 and isinstance(fc, int) \
                and fc >= 1
            record("C1", ok,
                   f"complete; component_count={cc} face_count={fc}")

            # DuckDB queryable afterward
            print("  --- C1b query_structure_graph SELECT COUNT(*) FROM nodes ---")
            qraw = fs.query_structure_graph(mesh="0",
                                            sql="SELECT COUNT(*) FROM nodes")
            qdata = json.loads(qraw)
            rc = qdata.get("row_count")
            ok2 = rc is not None and rc > 0 and "error" not in qdata
            record("C1b", ok2,
                   f"query_structure_graph row_count={rc} (nodes present)")

        # ===== C2 analyze_mesh job =====
        print("=== C2 analyze_mesh job ===")
        raw = fs.analyze_mesh(mesh="0")
        launch2 = json.loads(raw)
        assert launch2["status"] in ("queued", "running"), launch2
        job2 = launch2["job_id"]
        st2 = poll(fs, lambda **kw: fs.analyze_mesh(mesh="0", **kw), job2,
                   deadline=120.0)
        if st2["status"] != "complete":
            record("C2", False, f"analyze_mesh did not complete: {st2}")
        else:
            rep = json.loads(st2["result"]) if isinstance(st2["result"], str) \
                else st2["result"]
            has_facts = ("vertex_count" in rep or "bbox" in rep
                        or "bounding_box" in rep or "triangle_count" in rep)
            record("C2", has_facts and "error" not in rep,
                   f"complete; report keys={list(rep)[:6]}")

        # ===== C3 not_found =====
        print("=== C3 not_found envelope ===")
        raw3 = fs.structure_graph(job_id="deadbeef")
        env3 = json.loads(raw3)
        ok3 = env3 == {"job_id": "deadbeef", "status": "not_found"}
        record("C3", ok3, f"envelope={env3}")

        # ===== C4 sync byte-identity =====
        print("=== C4 sync byte-identity ===")
        sync_raw = fs.structure_graph(mesh="0", job_id="sync")
        sync_summ = json.loads(sync_raw)
        if st["status"] == "complete":
            job_summ = json.loads(st["result"]) if isinstance(st["result"], str) \
                else st["result"]
            # Compare as dicts (same summary produced by the same sync body).
            same = sync_summ == job_summ
            record("C4", same,
                   f"sync summary == complete job result dict? {same}; "
                   f"keys={list(sync_summ)[:5]}")
        else:
            record("C4", False, "C1 never completed; cannot compare sync")

        # ===== C5 failure path =====
        print("=== C5 failure path (analyze_mesh mesh='999') ===")
        raw5 = fs.analyze_mesh(mesh="999")
        launch5 = json.loads(raw5)
        assert launch5["status"] in ("queued", "running"), launch5
        job5 = launch5["job_id"]
        st5 = poll(fs, lambda **kw: fs.analyze_mesh(mesh="999", **kw), job5,
                   deadline=120.0)
        ok5 = st5["status"] == "failed"
        record("C5", ok5,
               f"status={st5['status']}; error={st5.get('error', '')[:80]}")

        # ===== C6 pool saturation (queued observed) =====
        print("=== C6 pool saturation (queued + position) ===")
        jobs_mod = importlib.import_module("jobs")
        orig_max = jobs_mod.MAX_CONCURRENT
        orig_started = jobs_mod._WORKERS_STARTED
        orig_queue = jobs_mod._QUEUE
        jobs_mod.MAX_CONCURRENT = 1
        jobs_mod._WORKERS_STARTED = False
        jobs_mod._QUEUE = queue.Queue()
        gate = threading.Event()
        orig_call = fs._call

        def gated_call(command, params=None, timeout=300):
            gate.wait(timeout=120)
            return orig_call(command, params, timeout=timeout)

        fs._call = gated_call
        try:
            r1 = fs.structure_graph(mesh="0")
            l1 = json.loads(r1)
            j1 = l1["job_id"]
            assert l1["status"] in ("queued", "running"), l1
            wait_for_status(fs, j1, ("running",), deadline=30.0)
            print(f"  job1={j1} running")

            r2 = fs.structure_graph(mesh="0")
            l2 = json.loads(r2)
            j2 = l2["job_id"]
            assert l2["status"] in ("queued", "running"), l2
            env2 = fs._job_status(j2)
            print(f"  job2={j2} env={env2}")
            ok6a = env2["status"] == "queued"
            ok6b = env2.get("position") is not None and env2["position"] >= 0

            gate.set()
            f1 = wait_for_status(fs, j1, ("complete",), deadline=180.0)
            f2 = wait_for_status(fs, j2, ("complete",), deadline=180.0)
            ok6 = ok6a and ok6b and f1["status"] == "complete" \
                and f2["status"] == "complete"
            record("C6", ok6,
                   f"job2 queued(pos={env2.get('position')}); "
                   f"after release: job1={f1['status']} job2={f2['status']}")
        finally:
            fs._call = orig_call
            jobs_mod.MAX_CONCURRENT = orig_max
            jobs_mod._WORKERS_STARTED = orig_started
            # leave the saturation workers parked; do not restore the queue
            # object the workers are blocked on -- a fresh queue is harmless.
            jobs_mod._QUEUE = queue.Queue()
            jobs_mod._WORKERS_STARTED = True  # next real launch restarts 4

    finally:
        print("--- cleanup: closing probe-created Fusion documents ---")
        close_docs_except(pre_ids)

    # ---- verdict ----
    print("=== SUMMARY ===")
    all_ok = True
    for crit, ok, detail in RESULTS:
        tag = "PASS" if ok else "FAIL"
        print(f"  {crit}: {tag} -- {detail}")
        all_ok = all_ok and ok
    verdict = "APPROVE" if all_ok else "REJECT"
    print(f"VERDICT: {verdict}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()