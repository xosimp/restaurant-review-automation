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
"""
import concurrent.futures
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

# The job store's kind is "ai:<kind>:<hash of the request and the login>",
# so the same request pressed twice joins one job while two different
# invoices — or two logins' — run side by side.
KIND_PREFIX = "ai:"

# A job the job store itself failed (swept at boot after a deploy, or read
# past its deadline) carries ops' own words, which are about schedules.
DEAD_JOB_MESSAGE = "That took longer than it should and was stopped — nothing was saved. Try again."
GONE_MESSAGE = "That request isn't here any more — try again."
FAILED_MESSAGE = "Cavnar AI couldn't finish that right now — try again in a moment."

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


def _executor():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = concurrent.futures.ThreadPoolExecutor(max_workers=POOL_WORKERS, thread_name_prefix="owner-ai")
    return _pool


def _json_safe(payload):
    """The payload as the job store keeps it: a str subclass carrying
    attributes (a draft's run_id) comes back a plain str, anything else
    that is not JSON its text — never a job lost to a serialisation error."""
    return json.loads(json.dumps(payload, default=str))


def _run(job_id, kind, restaurant_id, user_id, fn, args, kwargs):
    import ops
    started = time.time()
    # The clock starts when a worker takes the job, not when it queued for
    # one (re-audit 10/4/26 PIPE-9, the schedule's rule).
    ops.set_async_job_deadline(job_id, started + JOB_SECONDS.get(kind, DEFAULT_JOB_SECONDS), started_ts=started)
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
    # called dead keeps that verdict (P-22).
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
    job_id, joined = ops.claim_async_job(uuid.uuid4().hex, job_kind(kind, request_key, user_id), restaurant_id)
    if joined:
        return job_id, True
    try:
        _executor().submit(ai_utils.attributed(_run), job_id, kind, restaurant_id, user_id, fn, args, kwargs)
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
            "wait_seconds": JOB_SECONDS.get(kind, DEFAULT_JOB_SECONDS)}


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
