"""
ai_async.py — an owner's slow AI request, run off the request thread as a
job the client polls (AI cost audit 10/7/26 #57).

An invoice read (Opus, many seconds), a recipe-card read and the Campaign
Studio's three drafts (the text, the email, the post) used to hold one of
the web process's four request threads for the whole model call
(gunicorn --workers 1 --threads 4): two invoices scanned at once were half
the platform. They now run the way the schedule's Generate always has —
start, a job id back at once, poll — on the schedule's job store
(ops.async_jobs: claim_async_job, finish_async_job, read_async_job), on a
small pool of their own:

  start(kind, rid, request_key, fn, *a)   (job_id, joined) — fn runs on the
                                          pool; the same request pressed
                                          again by the same login joins
                                          the job already running
                                          (claim_async_job)
  result(job_id, rid, user)               (payload, http status) for a poll:
                                          pending, or exactly what the
                                          synchronous route would have
                                          answered, status code included
  wants_async()                           did this request ask for a job?

THE SYNCHRONOUS ROUTE STAYS. A route answers with a job only when the
client sends `async` (a JSON body field, a form field or a query arg, any of
1/true/yes): iOS builds already in the field call these routes and wait on
the answer, and they keep getting it. Every client in this repository sends
the flag (dashboard.html's aiJobAwait, iOS APIClient.awaitAIJob).

WHO ASKED is taken on the request thread (ai_utils.attributed): the pool
thread has no request, and every model call would read as 'system' (#148).
The interactive slot (ai_utils, #4) is not taken: it bounds request
threads, and this work is off them — the pool's size bounds it instead
(OWNER_AI_JOB_WORKERS, one process: the CLAUDE.md caveat on gunicorn
--workers applies to it as to the other pools).

A JOB'S RESULT is stored with the login that started it: an invoice's
prices are read by whoever may scan one (FOOD_COST_VIEW, by the route's own
path), and a poll by another login of the restaurant is answered 404, as
an unknown job is.

FAIR, BOUNDED AND PRIORITISED (blind re-audit 10/20/26 R4-03). The pool was
a bare ThreadPoolExecutor: an unbounded FIFO with no notion of who asked,
so a morning's insight refreshes queued an owner's invoice scan behind
hundreds of other restaurants', and a job left waiting 45 minutes was
called dead by its poll yet still ran (and was paid for) — while the
owner's re-press, no longer joining it, queued a second. Now (_FairPool):
  * an owner-pressed job (an invoice or recipe read, a campaign draft, a
    live visibility check) is taken before a background insight refresh
    (priority_of), and within a priority the restaurant with the fewest
    running, then the one served longest ago, then the oldest job;
  * at most OWNER_AI_JOB_QUEUE_MAX jobs wait, and
    OWNER_AI_JOB_TENANT_QUEUE_MAX of one restaurant's — past either the job
    is answered "busy" at once, and nothing is paid;
  * a queued job carries a deadline from the moment it queues (its wait
    plus its run), so a poll never calls it dead while it waits and a
    re-press joins it; one that waited past OWNER_AI_JOB_QUEUE_WAIT_SECONDS
    is answered "busy" instead of run;
  * _run re-reads its row before the model call: a job the store already
    failed is never run;
  * a run that finished after a poll had called it dead keeps its answer
    (it was paid for): the store's dead verdict is replaced by the result,
    marked late, and the same login pressing the same request again within
    LATE_REUSE_MINUTES is handed it rather than paying twice.
"""
import contextvars
import hashlib
import json
import logging
import os
import threading
import time
import uuid

log = logging.getLogger("ai_async")

# The pool every owner AI job runs on. Small on purpose: each worker waits
# on the model, not the CPU, and the provider's own limits and the per-
# restaurant route limiters bound the rest.
POOL_WORKERS = max(1, int(os.getenv("OWNER_AI_JOB_WORKERS", "3")))


def _env_int(name, default, lo=0):
    try:
        return max(lo, int(os.getenv(name, str(default)) or default))
    except ValueError:
        return default


# The bounds (R4-03): jobs waiting in all, one restaurant's waiting, one
# restaurant's running at once (0: no ceiling — a lone restaurant may use
# every worker; under contention the pick is fair anyway), and the longest a
# job may wait for a worker before it is answered "busy" instead of run.
QUEUE_MAX = _env_int("OWNER_AI_JOB_QUEUE_MAX", 200, 1)
TENANT_QUEUE_MAX = _env_int("OWNER_AI_JOB_TENANT_QUEUE_MAX", 12, 1)
PER_TENANT_RUNNING = _env_int("OWNER_AI_JOB_PER_TENANT", 0)
QUEUE_WAIT_SECONDS = _env_int("OWNER_AI_JOB_QUEUE_WAIT_SECONDS", 900, 30)
# A late answer (finished after its poll called it dead) is handed to the
# same login pressing the same request again within this many minutes.
LATE_REUSE_MINUTES = 30

# Which jobs go first: an owner waiting on what they pressed (0) before a
# background refresh of a stored read (1, insight_refresh's "insight_*").
PRIORITY_PRESSED, PRIORITY_BACKGROUND = 0, 1
BACKGROUND_PREFIXES = ("insight_",)


def priority_of(kind) -> int:
    return PRIORITY_BACKGROUND if str(kind or "").startswith(BACKGROUND_PREFIXES) else PRIORITY_PRESSED

# How long each kind of job may run before a poll calls it dead
# (ops.set_async_job_deadline): the longest the synchronous call could have
# taken — create_with_retry's attempts and backoff, the orchestrator's one
# escalation where the policy has one — with room to spare.
JOB_SECONDS = {"invoice_scan": 300, "recipe_scan": 240, "campaign_text": 180, "campaign_email": 180,
               "campaign_post": 180,
               # A live AI-visibility check: eight paced Perplexity queries,
               # bounded by client_api.AIVIS_RUN_MAX_SECS (75 s), plus the
               # Places lookups around them (parity audit 10/7/26 #75).
               "ai_visibility": 150}
DEFAULT_JOB_SECONDS = 240


def job_seconds(kind) -> int:
    """How long a job of `kind` may run (an insight refresh: the default)."""
    return JOB_SECONDS.get(kind, DEFAULT_JOB_SECONDS)

# The job store's kind is "ai:<kind>:<hash of the request and the login>",
# so the same request pressed twice joins one job while two different
# invoices — or two logins' — run side by side.
KIND_PREFIX = "ai:"

# A job the job store itself failed (swept at boot after a deploy, or read
# past its deadline) carries ops' own words, which are about schedules.
DEAD_JOB_MESSAGE = "That took longer than it should and was stopped — nothing was saved. Try again."
GONE_MESSAGE = "That request isn't here any more — try again."
FAILED_MESSAGE = "Cavnar AI couldn't finish that right now — try again in a moment."
BUSY_MESSAGE = "Cavnar AI is busy with a lot of requests right now — try again in a few minutes."

_pool = None
_pool_lock = threading.Lock()


def wants_async(req=None) -> bool:
    """Whether the request asked to be answered with a job: `async` in the
    query string, a form field (a multipart upload) or the JSON body."""
    if req is None:
        from flask import request as req
    vals = [req.args.get("async")]
    try:
        vals.append(req.form.get("async"))
    except Exception:
        pass
    try:
        body = req.get_json(silent=True)
        if isinstance(body, dict):
            vals.append(body.get("async"))
    except Exception:
        pass
    for v in vals:
        if v is True or str(v or "").strip().lower() in ("1", "true", "yes"):
            return True
    return False


def job_kind(kind, request_key, user_id=None) -> str:
    """The job store's kind for this request BY THIS LOGIN (platform
    re-audit 10/7/26 #5): a result is readable only by the login that
    started it (result()), so a second login pressing the same request
    joined a job it could not read — a 404, then a second paid call. Each
    login gets its own job; the same login pressing twice still joins."""
    raw = json.dumps({"key": request_key, "user": user_id}, sort_keys=True, default=str)
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{KIND_PREFIX}{kind}:{key}"


class _FairPool:
    """The owner AI pool (R4-03): POOL_WORKERS daemon threads taking the
    fairest waiting job — priority first, then the restaurant with the
    fewest running, then the one served longest ago, then the oldest.
    Bounded: submit_fair answers False when the queue (or this restaurant's
    share of it) is full. Like the ai_lane, per process."""

    def __init__(self, workers, queue_max, tenant_queue_max, per_tenant_running):
        self.workers = workers
        self.queue_max = queue_max
        self.tenant_queue_max = tenant_queue_max
        self.per_tenant_running = per_tenant_running
        self._pending = []            # [priority, tenant, seq, queued_at, fn]
        self._running = {}
        self._served = {}
        self._seq = 0
        self._cv = threading.Condition()
        self._threads = []
        self._stopping = False

    def _ensure_threads(self):
        self._threads = [t for t in self._threads if t.is_alive()]
        while len(self._threads) < self.workers:
            t = threading.Thread(target=self._loop, daemon=True, name=f"owner-ai-{len(self._threads) + 1}")
            t.start()
            self._threads.append(t)

    def submit_fair(self, tenant, priority, fn) -> bool:
        """Queue fn(waited_seconds); False (nothing queued) when full."""
        with self._cv:
            if self._stopping or len(self._pending) >= self.queue_max:
                return False
            if sum(1 for p in self._pending if p[1] == tenant) >= self.tenant_queue_max:
                return False
            self._seq += 1
            self._pending.append([int(priority), tenant, self._seq, time.time(), fn])
            self._ensure_threads()
            self._cv.notify()
            return True

    def _pick(self):
        cands = [p for p in self._pending
                 if not self.per_tenant_running or self._running.get(p[1], 0) < self.per_tenant_running]
        if not cands:
            return None
        p = min(cands, key=lambda c: (c[0], self._running.get(c[1], 0), self._served.get(c[1], 0.0), c[2]))
        self._pending.remove(p)
        return p

    def _loop(self):
        while True:
            with self._cv:
                item = self._pick()
                while item is None:
                    self._cv.wait(1.0)
                    item = self._pick()
                tenant = item[1]
                self._running[tenant] = self._running.get(tenant, 0) + 1
                self._served[tenant] = time.monotonic()
                if len(self._served) > 5000:
                    self._served.clear()
            try:
                # A fresh context for every job: nothing one job set leaks
                # into the next on this thread (as the ai_lane does).
                contextvars.Context().run(item[4], time.time() - item[3])
            except Exception as e:                    # the job captures its own; never kill the worker
                log.warning("ai_async: a job failed outside its capture: %s", e)
            finally:
                with self._cv:
                    n = self._running.get(tenant, 1) - 1
                    if n > 0:
                        self._running[tenant] = n
                    else:
                        self._running.pop(tenant, None)
                    self._cv.notify_all()

    def stats(self):
        with self._cv:
            return {"workers": self.workers, "waiting": len(self._pending),
                    "running": sum(self._running.values()), "restaurants_running": len(self._running),
                    "queue_max": self.queue_max}

    def drain(self, seconds):
        """At exit: take nothing new, and give the running jobs up to
        `seconds` to finish (their answers are paid for)."""
        end = time.time() + max(0.0, seconds)
        with self._cv:
            self._stopping = True
            while self._running and time.time() < end:
                self._cv.wait(max(0.05, min(1.0, end - time.time())))


def _executor():
    global _pool
    with _pool_lock:
        if _pool is None:
            # OWNER_AI_JOB_WORKERS and OWNER_AI_JOB_PER_TENANT are the
            # container's bounds: each gunicorn web worker takes its share
            # (pool_sizing; one worker: all of it). 0 per tenant stays "no
            # ceiling". The queue bounds are this process's memory: kept.
            import pool_sizing
            per_tenant = pool_sizing.per_worker(PER_TENANT_RUNNING) if PER_TENANT_RUNNING else 0
            _pool = _FairPool(pool_sizing.sized("owner_ai", POOL_WORKERS), QUEUE_MAX, TENANT_QUEUE_MAX, per_tenant)
            import atexit
            atexit.register(_pool.drain, 30)
    return _pool


def stats() -> dict:
    """The pool's state for the console (waiting, running)."""
    if isinstance(_pool, _FairPool):
        return _pool.stats()
    import pool_sizing
    return {"workers": pool_sizing.per_worker(POOL_WORKERS), "waiting": 0, "running": 0, "restaurants_running": 0,
            "queue_max": QUEUE_MAX}


def _json_safe(payload):
    """The payload as the job store keeps it: a str subclass carrying
    attributes (a draft's run_id) comes back a plain str, anything else
    that is not JSON its text — never a job lost to a serialisation error."""
    return json.loads(json.dumps(payload, default=str))


def _still_pending(job_id) -> bool:
    """Whether the job's row is still waiting for its answer (R4-03): one
    the store failed meanwhile (a poll past its deadline, the boot sweep) is
    never run. True when the row cannot be read — the run decides then."""
    import ops
    try:
        conn = ops._async_conn()
        try:
            row = conn.execute("SELECT status FROM async_jobs WHERE job_id=?", (str(job_id),)).fetchone()
        finally:
            conn.close()
    except Exception:
        return True
    return row is None or row["status"] == "pending"


def _keep_late(job_id, stored) -> bool:
    """A run that finished after the store called its job dead (R4-03): the
    answer was paid for, so it replaces the store's own dead verdict —
    marked `late` — never a verdict a job wrote itself. True when kept."""
    import ops
    try:
        payload = json.dumps(dict(stored, late=True))
        conn = ops._async_conn()
        try:
            cur = conn.execute("UPDATE async_jobs SET status='done', result_json=? WHERE job_id=? AND status='error' "
                               "AND (result_json IS NULL OR result_json NOT LIKE ?)",
                               (payload, str(job_id), '%"payload"%'))
            conn.commit()
        finally:
            conn.close()
        return cur.rowcount == 1
    except Exception as e:
        log.warning("ai_async: a late answer for %s was not kept: %s", job_id, e)
        return False


def _late_answer(kind_key, restaurant_id):
    """The job id of a late answer (_keep_late) to this very request by this
    login within LATE_REUSE_MINUTES, or None."""
    import ops
    try:
        conn = ops._async_conn()
        try:
            row = conn.execute("SELECT job_id FROM async_jobs WHERE kind=? AND restaurant_id=? AND status='done' "
                               "AND result_json LIKE ? AND created_at >= datetime('now', ?) "
                               "ORDER BY created_at DESC LIMIT 1",
                               (kind_key, restaurant_id, '%"late": true%',
                                f"-{int(LATE_REUSE_MINUTES)} minutes")).fetchone()
        finally:
            conn.close()
        return row["job_id"] if row else None
    except Exception:
        return None


def _run(job_id, kind, restaurant_id, user_id, fn, args, kwargs, waited=None):
    import ops
    # A job the store already failed — read dead by a poll, swept at boot —
    # is never run: its model call would be paid for and thrown away (R4-03).
    if not _still_pending(job_id):
        return
    if waited is not None and waited > QUEUE_WAIT_SECONDS:
        # Waited past the queue's bound: answered "busy", nothing is sent.
        ops.finish_async_job(job_id, "done", {"payload": {"ok": False, "error": BUSY_MESSAGE, "busy": True},
                                              "http_status": 503, "user_id": user_id})
        return
    started = time.time()
    # The clock starts when a worker takes the job, not when it queued for
    # one (re-audit 10/4/26 PIPE-9, the schedule's rule).
    ops.set_async_job_deadline(job_id, started + job_seconds(kind), started_ts=started)
    try:
        payload, status = fn(*args, **kwargs)
    except Exception as e:
        ops.capture(e, job=f"ai_async_{kind}", context=f"restaurant_id={restaurant_id} job={job_id}")
        payload, status = {"ok": False, "error": FAILED_MESSAGE}, 500
    try:
        stored = {"payload": _json_safe(payload), "http_status": int(status or 200), "user_id": user_id}
    except Exception as e:
        ops.capture(e, job=f"ai_async_{kind}", context=f"restaurant_id={restaurant_id} job={job_id} (result)")
        stored = {"payload": {"ok": False, "error": FAILED_MESSAGE}, "http_status": 500, "user_id": user_id}
    # finish_async_job writes only a job still pending: one a poll already
    # called dead keeps that verdict (P-22) — unless this run has a real
    # answer, which was paid for and is kept, marked late (R4-03).
    if not _still_pending(job_id):
        if int(stored.get("http_status") or 200) < 400:
            _keep_late(job_id, stored)
        return
    ops.finish_async_job(job_id, "done", stored)


def start(kind, restaurant_id, request_key, fn, *args, by_user=None, **kwargs):
    """(job_id, joined): run fn(*args, **kwargs) — which returns (payload,
    http status), the synchronous route's answer — on the pool as a job the
    client polls. The same `request_key` for this restaurant, by the same
    login, while that job is pending joins it. Who asked rides with the work (ai_utils.attributed);
    `by_user` is the login whose job it is (result() answers only them, or
    an admin)."""
    user_id = by_user
    import ops
    import ai_utils
    kind_key = job_kind(kind, request_key, user_id)
    # This very request's answer that came back after its poll had given
    # up on it (R4-03): handed over, never bought twice.
    late = _late_answer(kind_key, restaurant_id)
    if late:
        return late, True
    job_id, joined = ops.claim_async_job(uuid.uuid4().hex, kind_key, restaurant_id)
    if joined:
        return job_id, True
    # Its deadline from the moment it queues — the longest wait plus its run
    # — so a poll never calls a job dead while it waits for a worker, and
    # pressing again joins it (R4-03). _run resets it when the run starts.
    ops.set_async_job_deadline(job_id, time.time() + QUEUE_WAIT_SECONDS + job_seconds(kind))
    run = ai_utils.attributed(_run)
    try:
        ex = _executor()
        if isinstance(ex, _FairPool):
            def queued(waited):
                run(job_id, kind, restaurant_id, user_id, fn, args, kwargs, waited=waited)
            if not ex.submit_fair(restaurant_id, priority_of(kind), queued):
                # The queue (or this restaurant's share of it) is full:
                # "busy" now, nothing sent, nothing paid.
                ops.finish_async_job(job_id, "done", {"payload": {"ok": False, "error": BUSY_MESSAGE, "busy": True},
                                                      "http_status": 503, "user_id": user_id})
        else:
            ex.submit(run, job_id, kind, restaurant_id, user_id, fn, args, kwargs)
    except Exception as e:
        # The pool refused it (shutting down): said, and the job closed, so
        # the client's poll never waits on a job nothing will run.
        ops.capture(e, job=f"ai_async_{kind}", context=f"restaurant_id={restaurant_id} (submit)")
        ops.finish_async_job(job_id, "done", {"payload": {"ok": False, "error": FAILED_MESSAGE},
                                              "http_status": 503, "user_id": user_id})
    return job_id, False


def started_answer(job_id, joined, kind) -> dict:
    """What a route answers when it started (or joined) a job."""
    return {"ok": True, "async": True, "status": "pending", "job_id": job_id, "joined": bool(joined),
            "wait_seconds": job_seconds(kind)}


def result(job_id, restaurant_id, user=None):
    """(payload, http status) for a poll of `job_id`: {"ok", "status":
    "pending", "seconds_left"?} while it runs; then the payload the
    synchronous route would have answered, with its status code, plus
    "status": "done" and "job_id". A job of another restaurant, or another
    login's, is not found."""
    import ops
    job = ops.read_async_job(job_id, restaurant_id=restaurant_id)
    if not job:
        return {"ok": False, "status": "error", "error": GONE_MESSAGE}, 404
    if job.get("status") == "pending":
        out = {"ok": True, "status": "pending", "job_id": job_id}
        if job.get("seconds_left") is not None:
            out["seconds_left"] = job["seconds_left"]
        return out, 200
    stored = job.get("result")
    if not isinstance(stored, dict) or "payload" not in stored:
        # Failed by the job store itself (swept at boot, read past its
        # deadline): its words are the schedule's, never shown here.
        return {"ok": False, "status": "error", "job_id": job_id, "error": DEAD_JOB_MESSAGE}, 503
    owner = stored.get("user_id")
    me = (user or {}).get("id")
    if owner is not None and me is not None and int(owner) != int(me) and not (user or {}).get("is_admin"):
        return {"ok": False, "status": "error", "error": GONE_MESSAGE}, 404
    payload = stored.get("payload")
    payload = dict(payload) if isinstance(payload, dict) else {"ok": False, "error": FAILED_MESSAGE}
    payload.setdefault("status", "done")
    payload["job_id"] = job_id
    return payload, int(stored.get("http_status") or 200)
