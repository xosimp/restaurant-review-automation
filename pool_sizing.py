"""
pool_sizing.py — how big one process's bounded pool is when gunicorn runs
more than one web worker (audit #25, WS10 — the P3 multi-worker readiness
item; docs/plans/POSTGRES_AND_WORKERS_PLAN.md, "What still blocks a second
gunicorn worker").

Every bounded pool here is one per PROCESS: the admin pools
(ops.run_admin_task, admin_routes._submit_admin_job), the webhook and push
delivery pools, the owner AI job pool (ai_async), the interactive lane
(ai_lane), Ask's read pool, staff translations and the one-thread
background pools (Ask's summary, the shadow review, the shadow scoring). Its
setting (ADMIN_TASK_WORKERS, PUSH_MAX_WORKERS, AI_LANE_WORKERS, ...) is the
bound for the whole CONTAINER — what the one gunicorn web worker has today.
With `--workers N` each worker builds its own copy, so the container would
run N times every bound on outbound work.

  per_worker(bound)   this process's share: bound // web_workers(), never
                      below `minimum` (1 — a pool with no thread is no
                      pool). With ONE web worker — production today — the
                      bound comes back unchanged, exactly as before.
  web_workers()       how many gunicorn web workers share the container's
                      bounds (below).
  sized(name, bound)  per_worker, remembered under `name` for the console
                      and the tests (snapshot()).

WHERE THE FLOOR OVERSHOOTS. A bound smaller than the worker count (a
one-thread pool across four workers) cannot be split: each worker keeps
one, and the container runs `web_workers()` of them. The pools where that
can happen hold their real ceiling elsewhere, in the database, shared by
every process: model spend by the gateway's tokens a minute
(model_gateway, `model_token_windows`) and the AI rate limits
(`ai_rate_events`); a generation's streams by the queue's claimed rows
(job_queue.max_running) and the gateway's stream places. Nothing those
pools run is dropped for having fewer threads, so their total work is the
same either way — only its pace changes. The plan doc's inventory says,
pool by pool, which applies.

HOW MANY WORKERS. gunicorn.conf.py's when_ready (in the master, before the
web workers fork) writes the master's real count — server.cfg.workers, the
start command's `--workers` — to CAVNAR_WEB_WORKERS, which every worker
inherits. Without it (a dev server, a test) WEB_CONCURRENCY, what gunicorn
itself reads as its default worker count; else 1. The master's own count
wins, so a stray WEB_CONCURRENCY can never shrink production's pools while
the start command still says `--workers 1`.

Only a WEB process divides. worker.py (the queue consumer, the scheduler
process) says so in CAVNAR_PROCESS_ROLE (`queue`, `scheduler`): it inherits
CAVNAR_WEB_WORKERS from the master that spawned it, but it is one process
of its kind, so its pools keep the whole bound.
"""
import os
import threading

WORKERS_ENV = "CAVNAR_WEB_WORKERS"
ROLE_ENV = "CAVNAR_PROCESS_ROLE"

_sized = {}
_sized_lock = threading.Lock()


def process_role() -> str:
    """`web` (the default — a gunicorn worker or the dev server), else what
    worker.py set: `queue` or `scheduler`."""
    return (os.getenv(ROLE_ENV) or "web").strip().lower() or "web"


def _positive_int(value):
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def web_workers() -> int:
    """How many gunicorn web workers divide the container's bounds: 1 in any
    process that is not a web worker."""
    if process_role() != "web":
        return 1
    for name in (WORKERS_ENV, "WEB_CONCURRENCY"):
        n = _positive_int(os.getenv(name))
        if n:
            return n
    return 1


def per_worker(bound, minimum=1, workers=None) -> int:
    """This process's share of a container-wide `bound`. One worker: `bound`
    itself, untouched (whatever it is — today's behaviour exactly). N
    workers: bound // N, at least `minimum`."""
    n = workers if workers is not None else web_workers()
    if n <= 1:
        return bound
    try:
        b = int(bound)
    except (TypeError, ValueError):
        return bound
    return max(int(minimum), b // n)


def sized(name, bound, minimum=1) -> int:
    """per_worker(bound), noted under `name` (snapshot)."""
    workers = web_workers()
    share = per_worker(bound, minimum=minimum, workers=workers)
    with _sized_lock:
        _sized[str(name)] = {"bound": bound, "per_process": share, "workers": workers}
    return share


def whole(name, bound, held_by) -> int:
    """`bound` unchanged in every process, noted under `name` with what holds
    its ceiling across processes (`held_by`) — a pool whose work is already
    counted in the database (the schedule call pool: a generation's streams
    exist only while its queue row is claimed, and claimed rows are counted
    across every process, job_queue.max_running)."""
    with _sized_lock:
        _sized[str(name)] = {"bound": bound, "per_process": bound, "workers": web_workers(),
                             "held_by": str(held_by)}
    return bound


def fleet_total(bound, minimum=1, workers=None) -> int:
    """What the container runs in all when every web worker takes its share:
    the bound itself, or more where the floor overshoots it."""
    n = workers if workers is not None else web_workers()
    return per_worker(bound, minimum=minimum, workers=n) * max(1, n)


def snapshot() -> dict:
    """{pool name: {bound, per_process, workers}} for every pool sized so
    far in this process, plus this process's worker count and role."""
    with _sized_lock:
        pools = {k: dict(v) for k, v in _sized.items()}
    return {"workers": web_workers(), "role": process_role(), "pools": pools}
