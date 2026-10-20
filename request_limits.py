"""Request limits kept in memory (scale audit 10/19/26, SEC-07 and SEC-08).

Two jobs, both process-local by design — gunicorn runs one worker with four
request threads, and these guard THAT process's threads and its one writer
(CLAUDE.md's per-process class, like ASK_MAX_CONCURRENT):

1. **A fair share of the request threads per login and per restaurant**
   (SEC-07). docs/ops/SECURITY.md deferred request limits to a Cloudflare
   layer that does not front the app, so four slow requests from one tenant
   (a tablet left polling Home, a script) held every thread while every other
   tenant queued. `session_refusal(user)` takes from two token buckets — the
   session's and the restaurant's — at a cost per route: the measured
   10-second-class reads (`HEAVY_PATHS`) cost HEAVY_COST and also take from a
   small per-session heavy bucket, which bounds how often they start (how
   many run at once is the in-flight share below, R7-02).
   The buckets are sized well above a dashboard open (~23 reads at once).
   Internal logins (admin, support) have their own ceiling
   (`security.admin_request_allowed`) and are not counted here.
   `REQUEST_LIMITS=0` switches it off.

2. **Anonymous traffic is filtered before it writes** (SEC-08). Every allowed
   call of `ai_utils.ai_rate_limited` inserts a row, so a flood on a public
   route — the guest join form, the staff portal, a marketing link, a webhook
   with a bad signature — became write-lock contention for every tenant.
   `memory_limited(key, max_calls, window)` is a sliding window in memory;
   the public limits use it in place of the database, and the durable
   counters behind them (portal_attempts, guest_optin_requests,
   inbound_webhook_health) are only written for traffic it lets through.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

_lock = threading.Lock()
_windows: dict = {}          # key -> deque of monotonic stamps
_buckets: dict = {}          # key -> [credit, stamp]
_MAX_KEYS = 20000

# (capacity, refill per second)
SESSION_BUCKET = (90.0, 3.0)
RESTAURANT_BUCKET = (300.0, 10.0)
# Per session: six at once, then one a second. Was (3, 0.2) — a manager
# editing in the Studio re-runs the publish review (two heavy calls) each
# time the rows settle, and one every five seconds 429'd a person working
# quickly; these routes are stored or cached reads since the same round
# (FE-01 home_snapshots, F5 the Studio caches), so the cap is for a runaway
# client, never a person.
HEAVY_BUCKET = (6.0, 1.0)
HEAVY_COST = 5.0
# The routes production measured at ~10 s and more (A7 SEC-07): Home's
# morning brief and good news, the publish review — web and phone.
HEAVY_PATHS = ("/api/morning-brief", "/api/good-news", "/api/labor/publish-review",
               "/mobile/api/morning-brief", "/mobile/api/good-news", "/mobile/api/labor/publish-review")


def enabled() -> bool:
    return (os.environ.get("REQUEST_LIMITS") or "1").strip().lower() not in ("0", "false", "no", "off")


def _sweep(store):
    if len(store) > _MAX_KEYS:
        for k in list(store)[: len(store) // 2]:
            store.pop(k, None)


def memory_limited(key: str, max_calls: int, window_secs: float, now: float = None) -> bool:
    """True when `key` has made `max_calls` calls in the last `window_secs`
    (this call is then not counted); else counts it and answers False."""
    now = time.monotonic() if now is None else now
    with _lock:
        q = _windows.get(key)
        if q is None:
            _sweep(_windows)
            q = _windows[key] = deque()
        while q and now - q[0] >= window_secs:
            q.popleft()
        if len(q) >= max_calls:
            return True
        q.append(now)
        return False


def _take(key, cost, capacity, rate, now):
    b = _buckets.get(key)
    if b is None:
        _sweep(_buckets)
        b = _buckets[key] = [capacity, now]
    b[0] = min(capacity, b[0] + (now - b[1]) * rate)
    b[1] = now
    if b[0] < cost:
        return (cost - b[0]) / rate
    b[0] -= cost
    return 0.0


def _refund(key, cost, capacity):
    b = _buckets.get(key)
    if b is not None:
        b[0] = min(capacity, b[0] + cost)


def check(session_key: str, restaurant_id, path: str, now: float = None, rule: str = None):
    """0.0 when the request may run (its cost taken), else the seconds to
    wait. Takes nothing when refused. A route is heavy when it is in
    HEAVY_PATHS or measured slow (`measured_heavy_routes`, R7-02)."""
    now = time.monotonic() if now is None else now
    heavy = is_heavy(path, rule)
    cost = HEAVY_COST if heavy else 1.0
    taken = []
    with _lock:
        steps = [(f"s:{session_key}", cost, *SESSION_BUCKET)]
        if restaurant_id:
            steps.append((f"r:{restaurant_id}", cost, *RESTAURANT_BUCKET))
        if heavy:
            steps.append((f"h:{session_key}", 1.0, *HEAVY_BUCKET))
        for key, c, cap, rate in steps:
            wait = _take(key, c, cap, rate, now)
            if wait > 0:
                for k, c2, cap2 in taken:
                    _refund(k, c2, cap2)
                return wait
            taken.append((key, c, cap))
    return 0.0


# ── in flight: a share of the threads, not just of the starts (R7-02) ───────
#
# The buckets above bound how often a login STARTS requests, never how many
# run at once: one login at 3 a second on a 4-second route held all four
# threads for as long as it kept going, inside its "fair share" (blind
# re-audit 10/20/26 R7-02, reproduced). These count requests running right
# now — per login, per restaurant, per login's heavy routes, and in total —
# taken just before the view runs and given back when it returns
# (`run_counted`, from login_required, mobile_login_required and
# staff_login_required). Work-conserving: a login alone on an idle process
# still runs CAPACITY-1 at once; it is held to SESSION_INFLIGHT only once the
# process is down to its last free thread, so at least one thread is always
# left for someone else. A refusal is a cheap 429 {in_flight: true} with
# Retry-After 1, which both clients retry on their own. A streamed answer
# gives its slot back when the view returns, before the stream ends.
def _env_int(name, default):
    try:
        v = int((os.environ.get(name) or "").strip() or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def capacity() -> int:
    """The request threads this process serves (Procfile --threads 4)."""
    return _env_int("REQUEST_THREADS", 4)


def session_inflight() -> int:
    return _env_int("REQUEST_INFLIGHT_PER_LOGIN", 2)


def restaurant_inflight() -> int:
    return min(_env_int("REQUEST_INFLIGHT_PER_RESTAURANT", 3), max(1, capacity() - 1))


def heavy_inflight() -> int:
    return _env_int("REQUEST_INFLIGHT_HEAVY_PER_LOGIN", 2)


_inflight: dict = {}          # key -> requests running now
# Routes measured slow (request_rollups, the last 7 days), refreshed every 10
# minutes: a route that got slow joins HEAVY_PATHS without a deploy.
HEAVY_MEASURED_MS = 2000.0
_measured = {"at": 0.0, "routes": frozenset()}


def measured_heavy_routes(now: float = None) -> frozenset:
    """URL rules whose mean over the last 7 days is HEAVY_MEASURED_MS or more
    (at least 20 requests). Read at most every 10 minutes; any failure keeps
    the last answer (empty at first) — HEAVY_PATHS still hold."""
    now = time.monotonic() if now is None else now
    if _measured["at"] and now - _measured["at"] < 600:
        return _measured["routes"]
    _measured["at"] = now
    try:
        import models
        conn = models.get_conn()
        try:
            rows = conn.execute(
                "SELECT route FROM request_rollups WHERE minute_at >= datetime('now', '-7 days') AND route <> '*' "
                "GROUP BY route HAVING SUM(requests) >= 20 AND SUM(total_ms) / SUM(requests) >= ?",
                (HEAVY_MEASURED_MS,)).fetchall()
        finally:
            conn.close()
        _measured["routes"] = frozenset(str(r[0]) for r in rows if r[0])
    except Exception:
        pass
    return _measured["routes"]


def is_heavy(path: str, rule: str = None) -> bool:
    if any(path == p or path.startswith(p + "/") for p in HEAVY_PATHS):
        return True
    return bool(rule) and rule in measured_heavy_routes()


def inflight_enter(user, path: str = "", rule: str = None):
    """(refusal_seconds, slot). refusal_seconds > 0: refuse, nothing taken.
    Else `slot` (the keys taken, or None when not counted) goes to
    inflight_leave."""
    if not enabled() or not user:
        return 0, None
    try:
        from auth import is_internal_login
        if is_internal_login(user):
            return 0, None
    except Exception:
        pass
    uid = str(user.get("id") or "")
    if not uid:
        return 0, None
    rid = user.get("restaurant_id")
    heavy = is_heavy(path or "", rule)
    with _lock:
        total = _inflight.get("*", 0)
        mine = _inflight.get(f"s:{uid}", 0)
        if mine >= max(1, capacity() - 1):
            return 1, None
        if mine >= session_inflight() and total >= capacity() - 1:
            return 1, None
        if rid and _inflight.get(f"r:{rid}", 0) >= restaurant_inflight():
            return 1, None
        if heavy and _inflight.get(f"h:{uid}", 0) >= heavy_inflight():
            return 1, None
        keys = ["*", f"s:{uid}"] + ([f"r:{rid}"] if rid else []) + ([f"h:{uid}"] if heavy else [])
        for k in keys:
            _inflight[k] = _inflight.get(k, 0) + 1
    return 0, keys


def inflight_leave(slot):
    if not slot:
        return
    with _lock:
        for k in slot:
            n = _inflight.get(k, 0) - 1
            if n > 0:
                _inflight[k] = n
            else:
                _inflight.pop(k, None)


def _refusal(wait, **extra):
    from flask import jsonify
    retry = max(1, int(wait + 0.999))
    resp = jsonify(ok=False, rate_limited=True, retry_after=retry,
                   error="That's a lot of requests at once. Give it a moment and try again.", **extra)
    resp.status_code = 429
    resp.headers["Retry-After"] = str(retry)
    return resp


def run_counted(user, call):
    """Run `call()` (the view) inside this login's in-flight share: a 429
    when the share is spent, else the view's answer, the slot given back
    however it ends."""
    try:
        from flask import request
        rule = request.url_rule.rule if request.url_rule is not None else None
        path = request.path or ""
    except Exception:
        rule, path = None, ""
    wait, slot = inflight_enter(user, path, rule)
    if wait > 0:
        return _refusal(wait, in_flight=True)
    try:
        return call()
    finally:
        inflight_leave(slot)


def session_refusal(user):
    """A 429 response for a session or restaurant past its share, else None.
    Called by auth.login_required / mobile_login_required once the session
    is known."""
    if not enabled() or not user:
        return None
    try:
        from auth import is_internal_login
        if is_internal_login(user):
            return None
    except Exception:
        pass
    from flask import request, jsonify
    # Per login: a login's phone and its browser share one share.
    session_key = str(user.get("id") or "")
    if not session_key:
        return None
    wait = check(session_key, user.get("restaurant_id"), request.path or "",
                 rule=(request.url_rule.rule if request.url_rule is not None else None))
    if wait <= 0:
        return None
    return _refusal(wait)


def reset():
    """Tests only."""
    with _lock:
        _windows.clear()
        _buckets.clear()
        _inflight.clear()
    _measured["at"] = 0.0
    _measured["routes"] = frozenset()
