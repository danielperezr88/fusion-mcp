#!/usr/bin/env python3
"""
Unit tests for mcp_server/jobs.py -- the thread-safe in-memory job registry.

Covers: launch returns unique running records; a slow fn completes with the
right result; a raising fn records an error; unknown ids return None; TTL
expiry pruning; LRU eviction (bounded at MAX_JOBS, running jobs never
evicted); fresh finished records surviving a prune; and concurrent launches
all reaching "completed".

All timing assertions use generous bounds (polling with a deadline, small
sleeps) -- no flaky wall-clock asserts.
"""

import threading
import time

import mcp_server.jobs as jobs


def _wait_for(job_id, status="completed", timeout=5.0):
    """Poll get_job until the record reaches `status` (generous bound)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = jobs.get_job(job_id)
        if record is not None and record["status"] == status:
            return record
        time.sleep(0.01)
    raise AssertionError(
        "job %s did not reach status %r within %.1fs (last=%r)"
        % (job_id, status, timeout, jobs.get_job(job_id))
    )


def test_launch_returns_unique_running_record():
    def slow():
        time.sleep(0.2)
        return 42

    job_ids = [jobs.launch_job(slow) for _ in range(3)]
    assert len(set(job_ids)) == 3  # unique ids
    for job_id in job_ids:
        record = jobs.get_job(job_id)
        assert record is not None
        assert record["status"] == "running"
        assert record["result"] is None
        assert record["error"] is None
        assert record["finished"] is None
        assert record["created"] is not None


def test_slow_fn_completes_with_result():
    def slow_42():
        time.sleep(0.05)
        return 42

    job_id = jobs.launch_job(slow_42)
    assert jobs.get_job(job_id)["status"] == "running"
    record = _wait_for(job_id)
    assert record["status"] == "completed"
    assert record["result"] == 42
    assert record["error"] is None
    assert record["finished"] is not None


def test_failing_fn_records_error():
    job_id = jobs.launch_job(lambda: 1 / 0)
    record = _wait_for(job_id, status="error")
    assert record["status"] == "error"
    assert "division by zero" in record["error"]
    assert record["result"] is None
    assert record["finished"] is not None


def test_unknown_id_returns_none():
    assert jobs.get_job("deadbeef" * 4) is None


def test_prune_removes_expired_records():
    job_id = jobs.launch_job(lambda: None)
    _wait_for(job_id)
    # Fabricate a stale finish timestamp, well beyond the TTL.
    jobs._JOBS[job_id]["finished"] = time.time() - jobs.JOB_TTL - 5.0
    jobs.prune()
    assert job_id not in jobs._JOBS


def test_prune_keeps_recently_finished_and_running():
    job_id = jobs.launch_job(lambda: None)
    _wait_for(job_id)
    jobs.prune()
    assert job_id in jobs._JOBS
    assert jobs.get_job(job_id)["status"] == "completed"


def test_lru_cap_evicts_finished_keeps_running():
    slow_release = threading.Event()
    fast_gate = threading.Event()
    running_ids = [
        jobs.launch_job(lambda: slow_release.wait(timeout=30)) for _ in range(4)
    ]
    # The 66 fast jobs block on fast_gate so NONE of them finish during the
    # launch loop (launch_job calls prune() per launch; a fast job completing
    # mid-launch would be evicted as the oldest finished record before the
    # test could observe it).  All 66 complete only after fast_gate is set.
    finished_ids = [
        jobs.launch_job(lambda: fast_gate.wait(timeout=30)) for _ in range(66)
    ]
    fast_gate.set()
    for job_id in finished_ids:
        _wait_for(job_id)
    # The 4 long-running jobs are still blocked and therefore still running.
    for job_id in running_ids:
        assert jobs.get_job(job_id)["status"] == "running"
    jobs.prune()
    assert len(jobs._JOBS) <= jobs.MAX_JOBS
    for job_id in running_ids:
        assert job_id in jobs._JOBS
        assert jobs.get_job(job_id)["status"] == "running"
    slow_release.set()
    for job_id in running_ids:
        _wait_for(job_id, status="completed")


def test_concurrent_launches_unique_and_complete():
    barrier = threading.Barrier(21)
    ids = []
    ids_lock = threading.Lock()

    def worker():
        barrier.wait()
        job_id = jobs.launch_job(lambda: 42)
        with ids_lock:
            ids.append(job_id)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert len(ids) == 20
    assert len(set(ids)) == 20  # all unique
    for job_id in ids:
        record = _wait_for(job_id)
        assert record["status"] == "completed"
        assert record["result"] == 42
