#!/usr/bin/env python3
"""
Unit tests for mcp_server/jobs.py -- the bounded worker pool + thread-safe
in-memory job registry.

Covers: launch returns unique active records; a slow fn completes with the
right result; a raising fn records a failure; unknown ids return None; TTL
expiry pruning; LRU eviction (bounded at MAX_JOBS, queued/running jobs never
evicted); fresh finished records surviving a prune; concurrent launches all
reaching "complete".  New pool tests: saturation produces "queued" with a
position; multiple queued reports report ordered positions 0,1,2; a raising fn
drained through the pool still records "failed".

All timing assertions use deterministic ``threading.Event`` gates -- no
sleep-based flakiness.
"""

import threading
import time

import mcp_server.jobs as jobs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait_for(job_id, status="complete", timeout=30.0):
    """Poll get_job until the record reaches *status* (generous bound)."""
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


def _wait_for_status(job_id, statuses, timeout=30.0):
    """Poll get_job until the record reaches any of *statuses*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = jobs.get_job(job_id)
        if record is not None and record["status"] in statuses:
            return record
        time.sleep(0.01)
    raise AssertionError(
        "job %s did not reach any of %r within %.1fs (last=%r)"
        % (job_id, statuses, timeout, jobs.get_job(job_id))
    )


# ---------------------------------------------------------------------------
# Existing tests -- intent preserved, vocabulary renamed
# ---------------------------------------------------------------------------

def test_launch_returns_unique_active_record():
    """launch_job returns unique active records (queued or running)."""

    def slow():
        time.sleep(0.2)
        return 42

    job_ids = [jobs.launch_job(slow) for _ in range(3)]
    assert len(set(job_ids)) == 3  # unique ids
    for job_id in job_ids:
        record = _wait_for_status(job_id, ("queued", "running"))
        assert record["status"] in ("queued", "running")
        assert record["result"] is None
        assert record["error"] is None
        assert record["finished"] is None
        assert record["created"] is not None
    # Clean up: let all three finish.
    for job_id in job_ids:
        _wait_for(job_id)


def test_slow_fn_completes_with_result():
    def slow_42():
        time.sleep(0.05)
        return 42

    job_id = jobs.launch_job(slow_42)
    _wait_for_status(job_id, ("queued", "running"))
    record = _wait_for(job_id)
    assert record["status"] == "complete"
    assert record["result"] == 42
    assert record["error"] is None
    assert record["finished"] is not None


def test_failing_fn_records_failed():
    def raise_div_zero():
        return 1 / 0

    job_id = jobs.launch_job(raise_div_zero)
    record = _wait_for(job_id, status="failed")
    assert record["status"] == "failed"
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


def test_prune_keeps_recently_finished():
    job_id = jobs.launch_job(lambda: None)
    _wait_for(job_id)
    jobs.prune()
    assert job_id in jobs._JOBS
    assert jobs.get_job(job_id)["status"] == "complete"


def test_lru_cap_evicts_finished_keeps_running():
    """With a 4-worker pool: 1 slow job holds a slot (stays *running*), 69 fast
    gated jobs finish after their gate is set, prune trims to MAX_JOBS while
    the running job survives.
    """
    slow_release = threading.Event()
    fast_gate = threading.Event()

    # One long-running job occupies a pool slot until slow_release is set.
    running_ids = [jobs.launch_job(lambda: slow_release.wait(timeout=30))]
    # 69 fast jobs gate on fast_gate; once set they complete quickly through
    # the remaining pool slots.
    finished_ids = [
        jobs.launch_job(lambda: fast_gate.wait(timeout=30)) for _ in range(69)
    ]
    # Release the fast gate so the gated jobs drain through the pool.
    fast_gate.set()
    for job_id in finished_ids:
        _wait_for(job_id)
    # The slow job is still blocked and therefore still running.
    assert _wait_for_status(running_ids[0], ("running",))["status"] == "running"
    jobs.prune()
    assert len(jobs._JOBS) <= jobs.MAX_JOBS
    assert running_ids[0] in jobs._JOBS
    assert jobs.get_job(running_ids[0])["status"] == "running"
    slow_release.set()
    _wait_for(running_ids[0])


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
        assert record["status"] == "complete"
        assert record["result"] == 42


# ---------------------------------------------------------------------------
# New pool tests (deterministic, Event-gated)
# ---------------------------------------------------------------------------

def test_pool_saturation_queued_observed_with_position():
    """When all MAX_CONCURRENT workers are busy, an extra job stays *queued*
    with ``position == 0``; after releasing the gate all jobs drain to
    *complete*.
    """
    gate = threading.Event()
    # Fill all pool worker slots with gated jobs.
    running_ids = [
        jobs.launch_job(lambda: gate.wait(timeout=30))
        for _ in range(jobs.MAX_CONCURRENT)
    ]
    # Wait until the pool has picked up every gated job and set it running.
    for rid in running_ids:
        _wait_for_status(rid, ("running",))
    # This extra job cannot be picked up -- all workers are blocked.
    queued_id = jobs.launch_job(lambda: 42)
    rec = jobs.get_job(queued_id)
    assert rec["status"] == "queued"
    assert rec["position"] == 0
    gate.set()
    for rid in running_ids:
        _wait_for(rid)
    _wait_for(queued_id)


def test_pool_saturation_position_ordering():
    """With the pool saturated, three extra queued jobs report positions
    0, 1, 2 respectively.
    """
    gate = threading.Event()
    running_ids = [
        jobs.launch_job(lambda: gate.wait(timeout=30))
        for _ in range(jobs.MAX_CONCURRENT)
    ]
    for rid in running_ids:
        _wait_for_status(rid, ("running",))
    extra_ids = [jobs.launch_job(lambda: 42) for _ in range(3)]
    for i, eid in enumerate(extra_ids):
        rec = jobs.get_job(eid)
        assert rec["status"] == "queued", rec
        assert rec["position"] == i, rec
    gate.set()
    for rid in running_ids:
        _wait_for(rid)
    for eid in extra_ids:
        _wait_for(eid)


def test_pool_failure_drains_to_failed():
    """A raising fn enqueued while the pool is saturated still reaches *failed*
    with the error message after the pool drains.
    """

    def raise_boom():
        raise ValueError("boom")

    gate = threading.Event()
    # Saturate the pool with gated long-running jobs.
    running_ids = [
        jobs.launch_job(lambda: gate.wait(timeout=30))
        for _ in range(jobs.MAX_CONCURRENT)
    ]
    for rid in running_ids:
        _wait_for_status(rid, ("running",))
    bad_id = jobs.launch_job(raise_boom)
    rec = jobs.get_job(bad_id)
    assert rec["status"] == "queued"
    gate.set()
    record = _wait_for(bad_id, status="failed")
    assert record["status"] == "failed"
    assert "boom" in record["error"]
    for rid in running_ids:
        _wait_for(rid)