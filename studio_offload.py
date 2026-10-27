"""
studio_offload.py — the Schedule Studio's heavy reads run in the queue
worker process, not on the web process's four request threads (blind
re-audit 10/20/26 R5-03).

The pre-publish review (GET/POST labor/publish-review, 10.3 s average in
production), the rows-on-screen rule check (POST labor/schedule/violations,
3.5 s), Send's preview (GET labor/publish-check, 1.3 s) and a reopened week
(GET labor/schedule-history/<id>, 3.9 s) are Python CPU and database round
trips on gunicorn's one worker x 4 threads — one GIL. At a few hundred
actively scheduling restaurants their Sunday/Monday peak is more than the
four threads carry, and every other request waits behind them.

  * An OFFLOADABLE endpoint (ENDPOINTS) whose client says it can poll
    (OFFLOAD_HEADER: 1 — the dashboard's fetch and the iOS APIClient send it
    on every request) and whose restaurant's recent calls of it cost at
    least INLINE_SECONDS (an in-process moving average per endpoint and
    restaurant: a small restaurant's 200 ms check stays inline, never paying
    a poll) is answered at once: HTTP 202, header TICKET_HEADER, body
    {ok, pending, ticket, poll, retry_after_ms}. The work is one
    `studio_compute` job_queue row (its own `studio` group, so it never
    waits behind a generation or a learner) the queue worker runs.
  * The client polls `poll` (GET labor/offload/<ticket>, both prefixes):
    202 while it waits (retry_after_ms grows with the rows ahead of it);
    then EXACTLY what the synchronous route would have answered — body and
    status code. A row that failed or expired is run inline by the poll.
  * No header (an iOS build already in the field), no live queue worker
    (the web process would run the row itself — nothing gained), a cheap
    restaurant, or STUDIO_OFFLOAD=0: the route answers synchronously, as
    before.

The row runs the route's own body in a bare Flask request context rebuilt
from the request (method, query, JSON body, path arguments) with the login
the route saw (credential-looking fields dropped — schedule_engine.
_row_safe_user), its salary view (models.salary_view(permissions.sees_salaries)) and its
AI attribution. A ticket is answered only to the login that started it, at
the restaurant it was started at.
"""
import hashlib
import json
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

KIND = "studio_compute"
OFFLOAD_HEADER = "X-Cavnar-Offload"
TICKET_HEADER = "X-Cavnar-Ticket"
# The endpoints whose bodies may run in the worker: read-only checks (what
# they write is a view or a "shown" record, never the week).
ENDPOINTS = ("publish_review", "schedule_violations", "publish_check", "schedule_history_detail")
# A restaurant's call of an endpoint is offloaded once its recent cost
# (exponential moving average, this process) reaches this many seconds.
INLINE_SECONDS = float(os.getenv("STUDIO_INLINE_SECONDS", "") or 1.0)
_EWMA_WEIGHT = 0.3
# A row no slot took in this long expires; the poll then runs it inline.
ROW_DEADLINE_SECONDS = 120
# How long a client keeps polling before it says so.
WAIT_SECONDS = 150
# A finished row (its answer is a whole review, tens of KB) is kept this long
# for its polls, then pruned by the next submit — never the jobs table's 14
# days: at 1,500 restaurants that was gigabytes a week.
RESULT_KEEP_SECONDS = 600

_COST = {}
_COST_LOCK = threading.Lock()
_REGISTRY = {}
_APP = []


def enabled() -> bool:
    return (os.getenv("STUDIO_OFFLOAD") or "1").strip().lower() not in ("0", "off", "false", "no")


def register(name, fn) -> None:
    """The body `name` runs in the worker: fn(current_user, **path_args)
    returning (dict, status), a Response, or (Response, status)."""
    _REGISTRY[name] = fn


def record(endpoint, restaurant_id, seconds) -> None:
    """One measured call of `endpoint` at a restaurant, into its average."""
    try:
        key = (endpoint, int(restaurant_id))
    except (TypeError, ValueError):
        return
    with _COST_LOCK:
        old = _COST.get(key)
        _COST[key] = float(seconds) if old is None else old + _EWMA_WEIGHT * (float(seconds) - old)


def cost(endpoint, restaurant_id):
    try:
        return _COST.get((endpoint, int(restaurant_id)))
    except (TypeError, ValueError):
        return None


def wanted(endpoint, restaurant_id, req=None) -> bool:
    """Whether this call goes to the worker (the module doc)."""
    if endpoint not in ENDPOINTS or not enabled() or endpoint not in _REGISTRY:
        return False
    try:
        from flask import request as _rq
        req = req or _rq
        if str(req.headers.get(OFFLOAD_HEADER) or "").strip() != "1":
            return False
    except Exception:
        return False
    c = cost(endpoint, restaurant_id)
    if c is None or c < INLINE_SECONDS:
        return False
    try:
        import job_queue
        return job_queue.mode() != "inprocess" and job_queue.worker_alive()
    except Exception:
        return False


def _user_id(user):
    try:
        return int((user or {}).get("id"))
    except (TypeError, ValueError):
        return None


def submit(endpoint, user, restaurant_id, path_args=None, req=None):
    """The call as a `studio_compute` row: (Response, 202). The same call by
    the same login, still queued or running, is the same row."""
    from flask import jsonify, request as _rq
    import job_queue
    import schedule_engine as _se
    req = req or _rq
    body = req.get_json(silent=True) if req.method != "GET" else None
    query = [(k, v) for k, vs in req.args.lists() for v in vs]
    attribution = {}
    try:
        import ai_utils
        attribution = ai_utils.attribution_for_thread() or {}
    except Exception:
        attribution = {}
    payload = {"endpoint": endpoint, "method": req.method, "path": req.path, "query": query, "json": body,
               "path_args": dict(path_args or {}), "user": _se._row_safe_user(user),
               "restaurant_id": int(restaurant_id), "attribution": attribution}
    sig = hashlib.sha1(json.dumps([endpoint, req.method, req.path, query, body, path_args or {}, _user_id(user),
                                   int(restaurant_id)], sort_keys=True, default=str).encode()).hexdigest()
    qid, _created = job_queue.enqueue(KIND, payload, restaurant_id=int(restaurant_id),
                                      idempotency_key=f"{KIND}:{sig}",
                                      requeue_states=("done", "failed", "expired", "cancelled"),
                                      deadline=time.time() + ROW_DEADLINE_SECONDS)
    _prune()
    resp = jsonify(**pending_answer(qid, req.path))
    resp.headers[TICKET_HEADER] = str(qid)
    resp.headers["Cache-Control"] = "no-store"
    return resp, 202


def _prune(limit=200) -> int:
    """Finished studio rows past RESULT_KEEP_SECONDS, a bounded chunk. Never
    raises."""
    try:
        from models import get_conn
        conn = get_conn()
        try:
            n = conn.execute("DELETE FROM jobs WHERE id IN (SELECT id FROM jobs WHERE grp='studio' "
                             "AND state IN ('done', 'failed', 'expired', 'cancelled') AND finished_at < ? LIMIT ?)",
                             (time.time() - RESULT_KEEP_SECONDS, int(limit))).rowcount
            conn.commit()
            return int(n or 0)
        finally:
            conn.close()
    except Exception as e:
        log.warning("studio rows not pruned: %s", e)
        return 0


def _poll_path(path, ticket) -> str:
    prefix = "/mobile/api" if str(path or "").startswith("/mobile/") else "/api"
    return f"{prefix}/labor/offload/{int(ticket)}"


def pending_answer(ticket, path, ahead=None) -> dict:
    """The 202 body: where to poll and how soon. The wait grows with the
    rows ahead of it (never a 2-second storm behind a backlog)."""
    wait = 400 if not ahead else min(5000, 400 + 250 * int(ahead))
    out = {"ok": True, "pending": True, "status": "pending", "ticket": int(ticket),
           "poll": _poll_path(path, ticket), "retry_after_ms": wait, "wait_seconds": WAIT_SECONDS}
    if ahead is not None:
        out["queue_ahead"] = int(ahead)
    return out


def _app():
    if not _APP:
        from flask import Flask
        _APP.append(Flask("studio_offload"))
    return _APP[0]


def _normalise(out):
    """(dict, status) from a body's (dict, status), Response or (Response, status)."""
    from flask import Response
    status = None
    if isinstance(out, tuple):
        out, status = out[0], (out[1] if len(out) > 1 else None)
    if isinstance(out, Response):
        status = status if status is not None else out.status_code
        data = out.get_json(silent=True)
        return (data if isinstance(data, dict) else {"ok": False, "error": "unreadable answer"}), int(status)
    return (out if isinstance(out, dict) else {"ok": False, "error": "unreadable answer"}), int(status or 200)


def run_body(p) -> tuple:
    """(payload, status) of the route's own body for a row's payload `p`,
    in a request context rebuilt from it, under the login's salary view."""
    import models
    from permissions import sees_salaries
    if p.get("endpoint") not in _REGISTRY:
        # The queue worker: the route modules register their bodies when
        # imported (strategy_routes' table, mobile_api's decorator).
        import strategy_routes  # noqa: F401
        import mobile_api  # noqa: F401
    fn = _REGISTRY.get(p.get("endpoint"))
    if fn is None:
        return {"ok": False, "error": "That check isn't available here."}, 404
    user = dict(p.get("user") or {})
    from flask import g
    from urllib.parse import urlencode
    query = [(str(k), str(v)) for k, v in (p.get("query") or [])]
    kw = {"method": p.get("method") or "GET", "query_string": urlencode(query)}
    if p.get("json") is not None:
        kw["json"] = p["json"]
    # Its own context even inside a request (a poll running a failed row):
    # the body reads exactly the request it was asked with.
    with _app().test_request_context(p.get("path") or "/", **kw):
        g.cavnar_current_user = user
        with models.salary_view(bool(sees_salaries(user))):
            out = fn(user, **(p.get("path_args") or {}))
    return _normalise(out)


def run_queued(job):
    """job_queue's handler for `studio_compute`: the body, timed; its answer
    and status code are the row's result."""
    p = job.payload if hasattr(job, "payload") else (job or {}).get("payload") or {}
    t0 = time.monotonic()
    payload, status = run_body(p)
    return {"body": json.loads(json.dumps(payload, default=str)), "status": int(status),
            "seconds": round(time.monotonic() - t0, 3), "user_id": _user_id(p.get("user")),
            "restaurant_id": p.get("restaurant_id")}


def _ahead(conn, row) -> int:
    """Rows of the studio group queued before this one (at most 200 counted)."""
    try:
        return int(conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM jobs WHERE grp=? AND state='queued' AND id<? "
                                "LIMIT 200) q", (row["grp"], int(row["id"]))).fetchone()[0] or 0)
    except Exception:
        return 0


def result(ticket, user, restaurant_id, path=None):
    """(Response, status) for a poll of `ticket`: 202 + pending while it
    waits; the stored answer and status when it ran; the body run inline
    when the row failed or expired. Another login's or restaurant's ticket
    is not found."""
    from flask import jsonify
    from models import get_conn
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, grp, state, restaurant_id, payload_json, result_json FROM jobs "
                           "WHERE id=? AND kind=? AND restaurant_id=?",
                           (int(ticket), KIND, int(restaurant_id))).fetchone()
        row = dict(row) if row else None
        ahead = _ahead(conn, row) if row and row["state"] == "queued" else None
    finally:
        conn.close()
    try:
        p = json.loads((row or {}).get("payload_json") or "{}")
    except (TypeError, ValueError):
        p = {}
    if row is None or _user_id(p.get("user")) != _user_id(user):
        return jsonify(ok=False, error="That check isn't here any more — reload."), 404
    if row["state"] in ("queued", "running"):
        resp = jsonify(**pending_answer(row["id"], path or p.get("path"), ahead=ahead))
        resp.headers[TICKET_HEADER] = str(row["id"])
        resp.headers["Cache-Control"] = "no-store"
        return resp, 202
    stored = None
    if row["state"] == "done":
        try:
            stored = json.loads(row.get("result_json") or "null")
        except (TypeError, ValueError):
            stored = None
    if not isinstance(stored, dict) or not isinstance(stored.get("body"), dict):
        # Failed, expired or cancelled: answered here, now, as the route would.
        t0 = time.monotonic()
        payload, status = run_body(p)
        record(p.get("endpoint"), restaurant_id, time.monotonic() - t0)
        return jsonify(**payload), status
    record(p.get("endpoint"), restaurant_id, stored.get("seconds") or 0)
    resp = jsonify(**stored["body"])
    resp.headers["Cache-Control"] = "no-store"
    return resp, int(stored.get("status") or 200)


def offloadable(endpoint):
    """Decorator for a route body fn(current_user, **path_args) (or a view
    fn(*path_args, current_user)): registered for the worker, answered with
    a ticket when `wanted`, else run here and its time recorded."""
    def deco(fn):
        import functools
        import inspect
        params = list(inspect.signature(fn).parameters)
        positional_user_last = bool(params) and params[-1] == "current_user" and params[0] != "current_user"

        def body(current_user, **path_args):
            if positional_user_last:
                return fn(*path_args.values(), current_user)
            return fn(current_user, **path_args)
        register(endpoint, body)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if positional_user_last:
                user = kwargs.get("current_user", args[-1] if args else None)
                names = [n for n in params if n != "current_user"]
                path_args = dict(zip(names, args[:len(names)]))
                path_args.update({k: v for k, v in kwargs.items() if k != "current_user"})
            else:
                user = kwargs.get("current_user", args[0] if args else None)
                path_args = {k: v for k, v in kwargs.items() if k != "current_user"}
            rid = _restaurant_of(user)
            if rid is not None and wanted(endpoint, rid):
                return submit(endpoint, user, rid, path_args)
            t0 = time.monotonic()
            out = fn(*args, **kwargs)
            if rid is not None:
                record(endpoint, rid, time.monotonic() - t0)
            return out
        return wrapper
    return deco


def _restaurant_of(user):
    """The restaurant a route body acts for (strategy_routes._rid's rule)."""
    try:
        return int((user or {}).get("restaurant_id"))
    except (TypeError, ValueError, AttributeError):
        return None
