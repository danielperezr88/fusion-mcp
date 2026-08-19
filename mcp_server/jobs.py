"""Thread-safe in-memory bounded job registry with a fixed worker pool.

Each job is a dict with keys ``status``, ``result``, ``error``, ``created``
and ``finished``.  Status is one of:

* ``"queued"``   -- the job is waiting for a worker thread (``position`` key
  is added in the ``get_job`` copy).
* ``"running"``  -- a worker has picked the job up and is executing it.
* ``"complete"`` -- the job body returned normally; ``result`` holds it.
* ``"failed"``   -- the job body raised; ``error`` holds the message.

The registry is bounded at ``MAX_JOBS`` entries (oldest finished record is
evicted first; queued/running jobs are NEVER evicted) and finished entries
expire ``JOB_TTL`` seconds after they finish.

Deliberately minimal: no cancellation, no persistence across restarts, no
retry, no callbacks.  Stdlib only (``threading``, ``queue``, ``uuid``,
``time``) -- no new dependencies.

Locking convention: every mutation of ``_JOBS`` happens under ``_LOCK``.
``prune()`` assumes the caller already holds ``_LOCK`` (``launch_job`` calls
it while holding the lock); call it directly only when no other thread is
mutating the registry concurrently.
"""

import queue
import threading
import time
import uuid

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
MAX_JOBS = 64
JOB_TTL = 3600.0  # seconds
MAX_CONCURRENT = 4

_QUEUE: queue.Queue = queue.Queue()
_WORKERS_STARTED = False


def _ensure_workers():
    """Start the daemon worker pool on first launch (guard against double start).

    Caller MUST already hold ``_LOCK``.
    """
    global _WORKERS_STARTED  # noqa: PLW0603 -- guarded by _LOCK
    if _WORKERS_STARTED:
        return
    for _ in range(MAX_CONCURRENT):
        threading.Thread(target=_worker_loop, daemon=True).start()
    _WORKERS_STARTED = True


def _worker_loop():
    """Pop jobs from the queue, set ``running``, execute, set ``complete``/``failed``."""
    while True:
        job_id, fn, args, kwargs = _QUEUE.get()
        with _LOCK:
            record = _JOBS.get(job_id)
            if record is not None:
                record["status"] = "running"
        _run(job_id, fn, args, kwargs)


def launch_job(fn, *args, **kwargs) -> str:
    """Enqueue ``fn(*args, **kwargs)`` for execution by the pool and return a job id.

    The record is inserted with status ``"queued"`` before being enqueued,
    so a caller can immediately ``get_job(job_id)`` and observe the job as
    queued (or already running if a worker grabbed it fast enough).
    """
    job_id = uuid.uuid4().hex
    with _LOCK:
        prune()
        _ensure_workers()
        _JOBS[job_id] = {
            "status": "queued",
            "result": None,
            "error": None,
            "created": time.time(),
            "finished": None,
        }
        _QUEUE.put((job_id, fn, args, kwargs))
    return job_id


def get_job(job_id) -> dict | None:
    """Return a shallow copy of the job record, or None if it is unknown.

    For a ``"queued"`` record the copy additionally includes a ``position``
    key: the 0-based index of the job_id in the pending queue, or None if
    the position could not be determined.
    """
    with _LOCK:
        record = _JOBS.get(job_id)
        if record is None:
            return None
        result = dict(record)
    if result["status"] == "queued":
        result["position"] = _compute_position(job_id)
    return result


def _compute_position(job_id) -> int | None:
    """Return the 0-based index of *job_id* in the pending queue, or None.

    Queue items are ``(job_id, fn, args, kwargs)`` tuples; we compare only the
    first element.  Best-effort — must never raise.
    """
    try:
        pending_ids = [item[0] for item in list(_QUEUE.queue)]
        return pending_ids.index(job_id)
    except (ValueError, RuntimeError, TypeError, IndexError):
        return None


def _run(job_id, fn, args, kwargs):
    """Execute the job body and record the outcome under the lock.

    Every exception becomes a status ``"failed"`` record -- nothing is ever
    swallowed without being recorded.
    """
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # any failure must be recorded, never swallowed
        with _LOCK:
            record = _JOBS.get(job_id)
            if record is not None:
                record["status"] = "failed"
                record["error"] = str(exc)
                record["finished"] = time.time()
        return
    with _LOCK:
        record = _JOBS.get(job_id)
        if record is not None:
            record["status"] = "complete"
            record["result"] = result
            record["finished"] = time.time()


def prune():
    """Drop expired and excess finished records.  Caller must hold _LOCK.

    (a) Records whose ``finished`` timestamp is older than ``JOB_TTL`` are
        dropped.
    (b) While the registry exceeds ``MAX_JOBS``, the oldest finished
        (complete/failed) record is evicted.  Queued/running records are
        never evicted; if every record is still queued or running the
        registry may stay above the cap until some of them finish.
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