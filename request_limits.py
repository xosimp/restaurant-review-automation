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
   small per-session heavy bucket, which bounds how many can run at once.
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
HEAVY_BUCKET = (3.0, 0.2)     # per session: three at once, then one every five seconds
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


def check(session_key: str, restaurant_id, path: str, now: float = None):
    """0.0 when the request may run (its cost taken), else the seconds to
    wait. Takes nothing when refused."""
    now = time.monotonic() if now is None else now
    heavy = any(path == p or path.startswith(p + "/") for p in HEAVY_PATHS)
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
    wait = check(session_key, user.get("restaurant_id"), request.path or "")
    if wait <= 0:
        return None
    retry = max(1, int(wait + 0.999))
    resp = jsonify(ok=False, rate_limited=True, retry_after=retry,
                   error="That's a lot of requests at once. Give it a moment and try again.")
    resp.status_code = 429
    resp.headers["Retry-After"] = str(retry)
    return resp


def reset():
    """Tests only."""
    with _lock:
        _windows.clear()
        _buckets.clear()
