"""
insight_refresh.py — the module AI reads served stale-while-refresh
(iOS parity audit 10/7/26 #37).

The Labor, Reviews and Marketing reads (/api/labor-insight, /api/review-
insight, /api/mkt-insight and their /mobile/api twins) each called a model
on the request thread whenever the stored read did not match the data: a
Sonnet read of 10–20 seconds holding one of the web process's four request
threads (gunicorn --workers 1 --threads 4), on the first open after any
figure moved and on every deploy (the 300-second process cache went with
the process).

Every read is already kept in the database (insight_store.insight_cache,
keyed on the data it was written from), and every model call already runs
through the AI orchestration (ai_orchestrator.generate: the workflow's
policy, ladder, canary, caps and budgets). What changes is WHERE a miss is
paid for:

  1. The request thread renders the read with model calls refused
     (ai_utils.stored_reads_only). A stored read for this data — or a
     fixed copy that needs no model (no data, a held refusal) — is served
     as it always was.
  2. A render that would have called a model stops before the call
     (ai_utils.StoredReadMiss). The route starts the read on the owner AI
     pool (ai_async.start — one job per restaurant, read and variant: a
     second open, from any login or device, joins it) and answers at once:
       * the last stored read, whatever its data, with its age —
         `stale: true`, `as_of`, `stale_note`, `refreshing: true`; or
       * with nothing stored yet, `pending: true` / `status: "pending"`,
         the read's own fields carrying a one-line placeholder so a build
         that knows nothing of this still shows a sentence.
     Both carry `refresh_job`, the job to poll by.
  3. The client re-reads the route with `refresh_job=<id>` (1.5 → 3 → 5 s).
     While the job runs it gets step 2's answer again without a render;
     once the job has written the read, step 1 serves it. A job that could
     not write one (a budget stop, an outage, a refused read) is answered
     with the last read marked as such, or the route's own error — never
     another job, so a failing read is not retried on every poll.

THE SYNCHRONOUS ROUTE STAYS, as with ai_async: only a request that sends
`async` (query arg, any of 1/true/yes) is answered this way. iOS builds in
the field, and every test that calls a route plainly, get the read as
before. Every client in this repository sends it.

The background work is the route's own body, run off the request: the same
orchestrated workflow, budgets, canary, readiness gate and validation — the
pool thread takes no interactive slot (it is not a request thread) and is
attributed to the login that opened the read (ai_async uses
ai_utils.attributed).
"""
import logging

log = logging.getLogger("insight_refresh")

# The ai_async job kind prefix: "insight_labor", "insight_reviews", ...
JOB_PREFIX = "insight_"

PENDING_MESSAGE = "Cavnar AI is writing this read — it'll be here in a moment."


def refreshing_note(as_of=None) -> str:
    """The line under a read served while a newer one is written."""
    if as_of:
        return f"From a read on {as_of} — Cavnar AI is writing a newer one."
    return "From an earlier read — Cavnar AI is writing a newer one."


def failed_note(as_of=None) -> str:
    """The line under a read served because a newer one couldn't be written
    (the wording the routes' own stale fallbacks use)."""
    if as_of:
        return f"From a read on {as_of} — the latest one couldn't be written."
    return "From an earlier read — the latest one couldn't be written."


def wants_background(req=None) -> bool:
    """Whether the request asked for the stale-while-refresh answer."""
    import ai_async
    return ai_async.wants_async(req)


def refresh_job_arg(req=None):
    """The `refresh_job` a polling client sends back, or None."""
    if req is None:
        from flask import request as req
    v = (req.args.get("refresh_job") or "").strip()
    return v[:64] or None


def probe(render):
    """(True, render()) when the read renders from what is stored; (False,
    None) when the render reached a model call (nothing was sent)."""
    import ai_utils
    try:
        with ai_utils.stored_reads_only():
            return True, render()
    except ai_utils.StoredReadMiss:
        return False, None


def is_failure(payload, status) -> bool:
    """Whether a finished refresh failed to write a read: an error status,
    an error payload, or the body's own stale fallback."""
    if int(status or 200) >= 400:
        return True
    if not isinstance(payload, dict):
        return True
    return bool(payload.get("error") or payload.get("ok") is False or payload.get("stale"))


def _settle(refresh):
    """The job body: run the route's refresh and keep only what a poll needs
    — whether it wrote a read, and the error the route would have answered."""
    def run():
        payload, status = refresh()
        status = int(status or 200)
        if is_failure(payload, status):
            p = payload if isinstance(payload, dict) else {}
            msg = p.get("error") or (p.get("insight") if not p.get("stale") else None)
            return {"ok": False, "error": msg, "stale": bool(p.get("stale"))}, status
        return {"ok": True}, status
    return run


def start(kind, restaurant_id, key, refresh):
    """(job_id, joined): the refresh on the owner AI pool, one per
    restaurant, read and `key` — whoever opened it (by_user=None), so a
    second login or device joins the read already being written."""
    import ai_async
    return ai_async.start(JOB_PREFIX + kind, restaurant_id, {"kind": kind, "key": key}, _settle(refresh),
                          by_user=None)


def job_state(job_id, restaurant_id):
    """("pending" | "done" | "failed" | None, stored payload, status) for a
    refresh job of this restaurant. None: unknown here (another
    restaurant's, or swept)."""
    try:
        import ops
        job = ops.read_async_job(job_id, restaurant_id=restaurant_id)
    except Exception:
        job = None
    if not job:
        return None, None, None
    if job.get("status") == "pending":
        return "pending", None, None
    stored = job.get("result")
    if not isinstance(stored, dict) or "payload" not in stored:
        # Failed by the job store itself (swept, past its deadline).
        return "failed", {"ok": False, "error": None}, 503
    payload, status = stored.get("payload"), int(stored.get("http_status") or 200)
    return ("failed" if is_failure(payload, status) else "done"), payload, status


def _waiting(job_id, stale, pending):
    s = stale(refreshing_note) if stale else None
    if s is not None:
        payload, status = s
        payload = dict(payload)
        payload.update(stale=True, refreshing=True, pending=False, refresh_job=job_id)
        return payload, status
    payload, status = pending(job_id)
    payload = dict(payload)
    payload.update(pending=True, refreshing=True, status="pending", refresh_job=job_id)
    return payload, status


def serve(kind, restaurant_id, *, render, refresh, stale, pending, failed, key=None, refresh_job=None):
    """(payload, http status) for a read asked with `async`.

    render()        the route's answer — run with model calls refused
    refresh()       the route's body that writes the read (calls the
                    model); run on the owner AI pool
    stale(note_fn)  the last stored read shaped as this route answers it,
                    marked stale with note_fn(as_of) as its stale_note, or
                    None when nothing is stored
    pending(job_id) the route's answer while there is no read at all
    failed(error, status)  the route's error answer, for a refresh that
                    wrote nothing and left no read to fall back on
    """
    state = None
    if refresh_job:
        state, job_payload, job_status = job_state(refresh_job, restaurant_id)
        if state == "pending":
            return _waiting(refresh_job, stale, pending)
    hit, answer = probe(render)
    if hit:
        return answer
    if state == "failed":
        # The job ran and wrote nothing: the last read, said so, else the
        # route's error. No new job — the client stops polling here.
        s = stale(failed_note)
        if s is not None:
            payload, status = s
            payload = dict(payload)
            payload.update(stale=True, refreshing=False)
            return payload, status
        err = (job_payload or {}).get("error") if isinstance(job_payload, dict) else None
        return failed(err, job_status or 500)
    try:
        job_id, joined = start(kind, restaurant_id, key, refresh)
    except Exception as e:
        # The job store refused (a locked database): the read is rendered
        # on this thread as the synchronous route would.
        log.warning("insight refresh not started (%s rid=%s): %s", kind, restaurant_id, e)
        return render()
    return _waiting(job_id, stale, pending)
