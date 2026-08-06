"""Thread-safe in-memory bounded job registry for long-running MCP tools.

Each job is a dict with keys ``status``, ``result``, ``error``, ``created``
and ``finished``.  Status is one of:

* ``"running"`` -- the job thread has been spawned but has not finished yet.
* ``"completed"`` -- the job body returned normally; ``result`` holds it.
* ``"error"`` -- the job body raised; ``error`` holds the message.

The registry is bounded at ``MAX_JOBS`` entries (oldest finished record is
evicted first; running jobs are NEVER evicted) and finished entries expire
``JOB_TTL`` seconds after they complete.

Deliberately minimal: no cancellation, no persistence across restarts, no
retry, no callbacks.  Stdlib only (``threading``, ``uuid``, ``time``) -- no
new dependencies.

Locking convention: every mutation of ``_JOBS`` happens under ``_LOCK``.
``prune()`` assumes the caller already holds ``_LOCK`` (``launch_job`` calls
it while holding the lock); call it directly only when no other thread is
mutating the registry concurrently.
"""

import threading
import time
import uuid

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
MAX_JOBS = 64
JOB_TTL = 3600.0  # seconds


def launch_job(fn, *args, **kwargs) -> str:
    """Run ``fn(*args, **kwargs)`` on a daemon thread and return a job id.

    The record is inserted with status ``"running"`` before the thread
    starts, so a caller can immediately ``get_job(job_id)`` and observe the
    job as running.
    """
    job_id = uuid.uuid4().hex
    with _LOCK:
        prune()
        _JOBS[job_id] = {
            "status": "running",
            "result": None,
            "error": None,
            "created": time.time(),
            "finished": None,
        }
        threading.Thread(
            target=_run, args=(job_id, fn, args, kwargs), daemon=True
        ).start()
    return job_id


def get_job(job_id) -> dict | None:
    """Return a shallow copy of the job record, or None if it is unknown."""
    with _LOCK:
        record = _JOBS.get(job_id)
        return None if record is None else dict(record)


def _run(job_id, fn, args, kwargs):
    """Execute the job body and record the outcome under the lock.

    Every exception becomes a status ``"error"`` record -- nothing is ever
    swallowed without being recorded.
    """
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # any failure must be recorded, never swallowed
        with _LOCK:
            record = _JOBS.get(job_id)
            if record is not None:
                record["status"] = "error"
                record["error"] = str(exc)
                record["finished"] = time.time()
        return
    with _LOCK:
        record = _JOBS.get(job_id)
        if record is not None:
            record["status"] = "completed"
            record["result"] = result
            record["finished"] = time.time()


def prune():
    """Drop expired and excess finished records.  Caller must hold _LOCK.

    (a) Records whose ``finished`` timestamp is older than ``JOB_TTL`` are
        dropped.
    (b) While the registry exceeds ``MAX_JOBS``, the oldest finished
        (completed/error) record is evicted.  Running records are never
        evicted; if every record is still running the registry may stay
        above the cap until some of them finish.
    """
    now = time.time()
    for job_id in list(_JOBS):
        record = _JOBS[job_id]
        if record["finished"] is not None and now - record["finished"] > JOB_TTL:
            del _JOBS[job_id]
    while len(_JOBS) > MAX_JOBS:
        finished = [
            (job_id, record)
            for job_id, record in _JOBS.items()
            if record["finished"] is not None
        ]
        if not finished:
            break
        oldest = min(finished, key=lambda item: item[1]["finished"])
        del _JOBS[oldest[0]]
