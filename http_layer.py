"""
http_layer.py — what every response gets on the way out.

Extracted from hosted_dashboard.py so it can be exercised without booting the
app. Importing hosted_dashboard initialises the database, seeds demo data and
starts the scheduler thread, and re-importing it inside a test re-runs
csrf_protect() on blueprints Flask has already registered — so the response
layer had no way to be tested directly. It is now a module you can attach to
a throwaway Flask app.

Two concerns, both measured in audit #17:

  COMPRESSION. dashboard.html renders to ~989 KB of HTML, CSS and JS in one
  document and shipped uncompressed to every client on every load — /login
  returned byte-identical responses with and without Accept-Encoding: gzip.

  CACHE HEADERS. Nothing set Cache-Control at all, which leaves every
  intermediary free to apply its own heuristic freshness to authenticated
  JSON carrying one restaurant's labor cost and revenue.

And, since the admin-console audit (#30, #38, #77), what every request
leaves behind: a request id (logged on every line the request writes and
returned as X-Request-ID), a traffic class that keeps admin and probe
traffic out of the customer latency figures, an in-flight count (the
saturation signal — four gunicorn threads are the whole platform), a
per-minute rollup by route that outlives a deploy, and a record of every
5xx, handled or not. The rollups and 5xx samples are buffered here in
memory; platform_monitor persists them off the request path.
"""
import gzip
import os
import re
import sys
import threading
import time
import uuid
from collections import deque

from flask import g, request

import logging_setup

_COMPRESSIBLE = ("text/html", "text/css", "text/plain", "text/xml",
                 "application/json", "application/javascript",
                 "text/javascript", "image/svg+xml")

# Below this the gzip header costs more than the body saves, and the CPU is
# wasted on every small JSON reply.
COMPRESS_MIN_BYTES = 1024
COMPRESS_LEVEL = 6      # the knee of the size/CPU curve for text


def compress_response(response):
    """gzip text responses that are worth compressing.

    Never touches: a streamed response (direct_passthrough — the body has not
    been produced yet and reading it here would defeat streaming), anything
    already encoded, a non-text type, or a body small enough that the header
    outweighs the saving.

    The streaming exclusion is explicit rather than trusted to a library
    default because there is exactly one SSE endpoint in the product — Ask
    Cavnar's progress stream — and buffering it to compress it would turn
    live tool progress back into the silent spinner the streaming exists to
    replace.
    """
    try:
        if response.direct_passthrough:
            return response
        if response.mimetype == "text/event-stream":
            return response
        if response.headers.get("Content-Encoding"):
            return response
        if response.status_code < 200 or response.status_code >= 300:
            return response
        if response.mimetype not in _COMPRESSIBLE:
            return response

        accept = (request.headers.get("Accept-Encoding") or "").lower()
        if "gzip" not in accept:
            # Still tell caches the response varies by encoding, or a proxy
            # can serve a gzipped body to a client that never asked for one.
            _add_vary(response)
            return response

        body = response.get_data()
        if len(body) < COMPRESS_MIN_BYTES:
            _add_vary(response)
            return response

        packed = gzip.compress(body, compresslevel=COMPRESS_LEVEL)
        # A body that grew is a body that should not have been compressed.
        if len(packed) >= len(body):
            _add_vary(response)
            return response

        response.set_data(packed)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(packed))
        _add_vary(response)
    except Exception:
        # Compression must never be the reason a page fails to render.
        return response
    return response


def _add_vary(response):
    vary = response.headers.get("Vary")
    if not vary:
        response.headers["Vary"] = "Accept-Encoding"
    elif "accept-encoding" not in vary.lower():
        response.headers["Vary"] = f"{vary}, Accept-Encoding"


def add_cache_headers(response):
    """Say explicitly what may be cached, because nothing did.

    One rule, and deliberately only one: anything under /api or /mobile/api is
    per-tenant and may change on the next write, so it is no-store. That is a
    correctness and privacy statement, not a performance one.

    Static assets are NOT given a long max-age, even though they are the
    obvious candidate. They are referenced by bare path — /static/cavnar-orb.js,
    no hash and no version query — so a far-future expiry would strand a
    JavaScript fix in browser caches with no way to bust it. Flask's own
    `no-cache` keeps the revalidation round trip but returns a bodiless 304,
    which is most of the saving without the hazard. Worth revisiting only
    alongside cache-busted asset URLs.

    HTML pages are left alone too: caching them would risk serving a stale
    dashboard after an action, and the win there came from compression.

    The admin console is the exception, JSON and HTML alike (#94): its
    payloads carry every owner's email and phone, login IPs and user agents
    across the whole fleet, which is the docstring's own privacy reason at
    its strongest, and they sat in browser caches on shared machines. The
    same goes for /health, which a monitor must never be answered from a
    cache.
    """
    try:
        path = request.path or ""
        if response.headers.get("Cache-Control"):
            return response
        if path.startswith(_NO_STORE_PREFIXES) or path == "/health":
            response.headers["Cache-Control"] = "no-store"
    except Exception:
        return response
    return response


# Per-tenant or per-operator content: never stored by a browser or a proxy.
# /audit/r/ is a revocable share link to one prospect's figures: a cached
# copy would outlive the revocation. /staff/api/ is one employee's shifts,
# colleagues, requests and sheets (PERF-11) — it carried no Cache-Control.
_NO_STORE_PREFIXES = ("/api/", "/mobile/api/", "/admin", "/audit/r/", "/staff/api/")


# ── request metrics ─────────────────────────────────────────────────────────
#
# The resiliency audit found no latency or error-rate signal anywhere: "is
# the site slow?" had no answer but a customer complaint, and the platform
# serves every request through gunicorn's four threads, so saturation is the
# failure mode most likely to arrive first and the one nothing could see.
#
# Two layers. The rolling in-process window below answers "how are we doing
# right now" for the console, and costs a deque append. Nothing here writes
# to SQLite on the request path — a row per request to the file the requests
# contend for would make the thing it measures worse — but per-minute
# rollups by route and every 5xx are buffered for platform_monitor's
# background flush, so a deploy no longer wipes the evidence (#77, #30).
_WINDOW_SECONDS = 300
_SLOW_MS = 2000
_MAX_SAMPLES = 5000
_SLOW_KEEP = 25

_samples = deque(maxlen=_MAX_SAMPLES)       # (at, ms, status, rule, klass)
_slowest = deque(maxlen=_SLOW_KEEP)
_metrics_lock = threading.Lock()

# ── traffic classes ─────────────────────────────────────────────────────────
#
# The console's p95 mixed an admin opening a 7-second fleet page, the
# monitor's /health and the static files in with what owners wait on (#77).
# Customer traffic is the dashboard, its JSON and the iOS app.
CUSTOMER_CLASSES = ("web", "web_api", "mobile")
_STATIC_PATHS = ("/robots.txt", "/sitemap.xml", "/favicon.ico", "/og-image-v2.png",
                 "/apple-app-site-association")
_WEBHOOK_PREFIXES = ("/stripe-webhook", "/docusign/webhook", "/docusign/callback", "/webhooks/")


def traffic_class(path) -> str:
    """admin | mobile | probe | static | webhook | web_api | web."""
    p = path or ""
    if p.startswith("/admin"):
        return "admin"
    if p.startswith("/mobile/api/"):
        return "mobile"
    if p in ("/health", "/api/status") or p == "/status" or p.startswith("/status/"):
        return "probe"
    if p.startswith("/static/") or p.startswith("/.well-known/") or p in _STATIC_PATHS:
        return "static"
    if p.startswith(_WEBHOOK_PREFIXES):
        return "webhook"
    if p.startswith("/api/"):
        return "web_api"
    return "web"


# ── request ids ─────────────────────────────────────────────────────────────
#
# One id per request, on every log line it writes and returned to the client
# as X-Request-ID, so "it failed at 2:14" can be found. An id the edge or the
# client already assigned is kept (Railway's own lets its HTTP log and ours
# be joined); anything that is not a plain token is replaced, so a header
# cannot inject into a log line.
_INBOUND_ID_HEADERS = ("X-Request-ID", "X-Railway-Request-Id")
_ID_OK = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def _new_request_id():
    for header in _INBOUND_ID_HEADERS:
        v = (request.headers.get(header) or "").strip()
        if v and _ID_OK.match(v):
            return v
    return uuid.uuid4().hex


def request_id():
    """This request's id, or None outside a request."""
    try:
        return getattr(g, "request_id", None)
    except RuntimeError:
        return None


# ── in flight ───────────────────────────────────────────────────────────────
#
# Only completed requests were ever counted, so four threads stuck on a
# third party read as a quiet platform. What is running now, how long the
# oldest has run, and the most that ran at once in each minute.
_inflight = {}                      # key -> (started, rule, klass)
_peak_by_minute = {}                # "YYYY-MM-DD HH:MM:00" -> max concurrent


def _minute(ts):
    return time.strftime("%Y-%m-%d %H:%M:00", time.gmtime(ts))


def worker_threads():
    """gunicorn's --threads for this process, read from its command line
    (the worker is forked from the master and keeps its argv); None when
    not under gunicorn."""
    argv = list(getattr(sys, "argv", []) or [])
    for i, a in enumerate(argv):
        if a.startswith("--threads="):
            v = a.split("=", 1)[1]
        elif a == "--threads" and i + 1 < len(argv):
            v = argv[i + 1]
        else:
            continue
        try:
            return int(v)
        except ValueError:
            return None
    return None


def inflight():
    """{now, oldest_ms, oldest_route, peak_5m, threads} for the console."""
    now = time.time()
    with _metrics_lock:
        running = list(_inflight.values())
        cutoff = _minute(now - _WINDOW_SECONDS)
        peak = max([v for k, v in _peak_by_minute.items() if k >= cutoff] or [0])
    oldest = min(running, key=lambda r: r[0]) if running else None
    return {"now": len(running),
            "oldest_ms": round((now - oldest[0]) * 1000) if oldest else None,
            "oldest_route": oldest[1] if oldest else None,
            "peak_5m": max(peak, len(running)),
            "threads": worker_threads()}


# ── per-minute rollups and 5xx samples (buffered; persisted elsewhere) ──────
LATENCY_BUCKETS_MS = (50, 100, 250, 500, 1000, 2500, 5000, 10000)   # + one overflow bucket
_MAX_PENDING_MINUTES = 180          # a flush outage keeps three hours, then drops the oldest
_rollups = {}                       # (minute, rule, method, klass) -> [n, e4, e5, total_ms, max_ms, *buckets]
_pending_5xx = deque(maxlen=500)
_dropped = {"rollup_minutes": 0, "server_errors": 0}
_sentry_last = {}                   # (rule, status) -> last capture time
_SENTRY_EVERY_SECONDS = 60


def _bucket(ms):
    for i, edge in enumerate(LATENCY_BUCKETS_MS):
        if ms <= edge:
            return i
    return len(LATENCY_BUCKETS_MS)


def _aggregate(now, elapsed_ms, status, rule, method, klass):
    """Add one request to its minute's rollup. Caller holds _metrics_lock."""
    minute = _minute(now)
    key = (minute, rule, method, klass)
    row = _rollups.get(key)
    if row is None:
        minutes = {k[0] for k in _rollups}
        if minute not in minutes and len(minutes) >= _MAX_PENDING_MINUTES:
            oldest = min(minutes)
            for k in [k for k in _rollups if k[0] == oldest]:
                del _rollups[k]
            _peak_by_minute.pop(oldest, None)
            _dropped["rollup_minutes"] += 1
        row = _rollups[key] = [0, 0, 0, 0.0, 0.0] + [0] * (len(LATENCY_BUCKETS_MS) + 1)
    row[0] += 1
    if 400 <= status < 500:
        row[1] += 1
    elif status >= 500:
        row[2] += 1
    row[3] += elapsed_ms
    row[4] = max(row[4], elapsed_ms)
    row[5 + _bucket(elapsed_ms)] += 1


def drain_rollups(now=None, include_current=False):
    """Take every completed minute's rollups out of the buffer, as dicts for
    platform_monitor.persist_request_rollups. The current minute stays (it is
    still filling) unless include_current (a shutdown flush). A synthetic
    route '*' row per minute carries that minute's peak concurrency."""
    now = time.time() if now is None else now
    current = _minute(now)
    out = []
    with _metrics_lock:
        keys = [k for k in _rollups if include_current or k[0] < current]
        minutes = set()
        for k in keys:
            row = _rollups.pop(k)
            minutes.add(k[0])
            out.append({"minute_at": k[0], "route": k[1], "method": k[2], "klass": k[3],
                        "requests": row[0], "errors_4xx": row[1], "errors_5xx": row[2],
                        "total_ms": round(row[3], 1), "max_ms": round(row[4], 1),
                        "buckets": list(row[5:]), "max_inflight": 0})
        for m in sorted(minutes):
            peak = _peak_by_minute.pop(m, 0)
            out.append({"minute_at": m, "route": "*", "method": "*", "klass": "*",
                        "requests": sum(r["requests"] for r in out if r["minute_at"] == m and r["route"] != "*"),
                        "errors_4xx": 0, "errors_5xx": 0, "total_ms": 0.0, "max_ms": 0.0,
                        "buckets": [0] * (len(LATENCY_BUCKETS_MS) + 1), "max_inflight": peak})
        for m in [m for m in _peak_by_minute if m < current and m not in minutes]:
            _peak_by_minute.pop(m, None)
    return out


def drain_server_errors():
    """Take the buffered 5xx samples, oldest first."""
    with _metrics_lock:
        out = list(_pending_5xx)
        _pending_5xx.clear()
    return out


def dropped_counts():
    with _metrics_lock:
        return dict(_dropped)


def _short_error(response):
    """What a 5xx said, for its log row: the JSON body's `error`, or the
    exception an unhandled one raised. Redacted — a provider error often
    carries the URL it failed on, and some URLs carry a key."""
    text = None
    exc = getattr(g, "_unhandled_exc", None)
    if exc is not None:
        text = f"{type(exc).__name__}: {exc}"
    elif not response.direct_passthrough and (response.mimetype or "") == "application/json":
        try:
            body = response.get_json(silent=True)
            if isinstance(body, dict) and body.get("error"):
                text = str(body.get("error"))
        except Exception:
            text = None
    if not text:
        return None
    try:
        from ai_guard import redact_secrets
        text = redact_secrets(text)
    except Exception:
        text = text[:80]
    return text[:300]


def _restaurant_of_request():
    """The restaurant a request was about, when anything in reach says so:
    a restaurant bound to the log context (by code that resolved the
    session), else a <restaurant_id>/<rid> in the URL rule."""
    rid = logging_setup.current().get("restaurant_id")
    if rid is None:
        va = getattr(request, "view_args", None) or {}
        rid = va.get("restaurant_id", va.get("rid"))
    try:
        return int(rid) if rid is not None else None
    except (TypeError, ValueError):
        return None


def _report_to_sentry(sample):
    """A HANDLED 5xx never reached Sentry: 50 of 73 route error handlers
    return a 500 without logging or capturing anything (#30). Unhandled
    ones are already captured by the Flask integration, so only handled
    ones are sent, at most one per route and status a minute."""
    if sample.get("unhandled") or not os.getenv("SENTRY_DSN"):
        return
    key = (sample["route"], sample["status"])
    now = time.time()
    with _metrics_lock:
        if now - _sentry_last.get(key, 0) < _SENTRY_EVERY_SECONDS:
            return
        _sentry_last[key] = now
    try:
        import sentry_sdk
        sentry_sdk.capture_message(
            f"HTTP {sample['status']} on {sample['method']} {sample['route']}"
            + (f": {sample['error']}" if sample.get("error") else ""),
            level="error",
            tags={"request_id": sample.get("request_id"), "route": sample["route"],
                  "status": str(sample["status"]), "traffic_class": sample.get("klass")},
            fingerprint=["http-5xx", sample["route"], str(sample["status"])])
    except Exception as e:
        import logging
        logging.getLogger("http").warning("could not forward a 5xx to Sentry: %s", e)


def _record_request(response):
    started = getattr(g, "_req_started", None)
    if started is None:
        return response
    elapsed_ms = (time.time() - started) * 1000.0
    try:
        # The RULE, not the path: /api/reviews/1234 and /api/reviews/5678 are
        # one endpoint, and keying on the raw path makes every id its own
        # row and the aggregate meaningless. It also keeps tokens that ride
        # in a path (/i/<token>) out of every record made here.
        rule = request.url_rule.rule if request.url_rule else "(unmatched)"
    except Exception:
        rule = "(unknown)"
    klass = getattr(g, "_req_class", None) or traffic_class(request.path)
    method = request.method
    status = response.status_code
    # The admin console's own "busy — try again" refusal (admin_ops' single-
    # flight fleet build answers 503 with X-Admin-Busy: 1) is a refusal, not a
    # server error: counted as a 429, never sampled as a 5xx or sent to
    # Sentry, so the console can't raise platform:error_rate on itself.
    if status == 503 and response.headers.get("X-Admin-Busy") == "1":
        status = 429
    now = time.time()
    with _metrics_lock:
        _samples.append((now, elapsed_ms, status, rule, klass))
        if elapsed_ms >= _SLOW_MS:
            _slowest.append({"at": now, "ms": round(elapsed_ms), "rule": rule,
                             "status": status, "klass": klass})
        _aggregate(now, elapsed_ms, status, rule, method, klass)
    rid = getattr(g, "request_id", None)
    if rid:
        response.headers["X-Request-ID"] = rid
    if status >= 500:
        try:
            sample = {"at": now, "method": method, "route": rule, "status": status,
                      "error": _short_error(response), "request_id": rid, "klass": klass,
                      "restaurant_id": _restaurant_of_request(), "duration_ms": round(elapsed_ms),
                      "unhandled": getattr(g, "_unhandled_exc", None) is not None,
                      "exc_type": type(g._unhandled_exc).__name__ if getattr(g, "_unhandled_exc", None) is not None else None}
            with _metrics_lock:
                if len(_pending_5xx) == _pending_5xx.maxlen:
                    _dropped["server_errors"] += 1
                _pending_5xx.append(sample)
            _report_to_sentry(sample)
        except Exception as e:
            import logging
            logging.getLogger("http").warning("could not record a %s: %s", status, e)
    return response


def _start_timer():
    now = time.time()
    g._req_started = now
    g.request_id = rid = _new_request_id()
    g._req_class = klass = traffic_class(request.path)
    try:
        rule = request.url_rule.rule if request.url_rule else "(unmatched)"
    except Exception:
        rule = "(unknown)"
    logging_setup.bind(request_id=rid, route=rule, method=request.method)
    # For gunicorn's access log, which reads WSGI environ keys as
    # %({name}e)s: the route RULE instead of the path, so tokens that ride
    # in a path (/reset-password/<token>, /i/<token>) never reach a log line.
    try:
        request.environ["cavnar.route"] = rule
        request.environ["cavnar.request_id"] = rid
        request.environ["cavnar.class"] = klass
    except Exception:
        pass
    g._inflight_key = key = object()
    minute = _minute(now)
    with _metrics_lock:
        _inflight[key] = (now, rule, klass)
        _peak_by_minute[minute] = max(_peak_by_minute.get(minute, 0), len(_inflight))
    if os.getenv("SENTRY_DSN"):
        try:
            import sentry_sdk
            sentry_sdk.set_tag("request_id", rid)
        except Exception:
            pass


def _end_request(exc=None):
    """teardown_request: runs for every request, whatever happened in it —
    after_request does not when an after-request hook itself fails."""
    try:
        key = getattr(g, "_inflight_key", None)
        if key is not None:
            with _metrics_lock:
                _inflight.pop(key, None)
    finally:
        logging_setup.clear()


def _on_exception(sender, exception, **extra):
    """got_request_exception: remember the unhandled exception, so the 5xx
    record names it and Sentry is not told twice."""
    try:
        g._unhandled_exc = exception
    except Exception:
        pass


def request_metrics(window_seconds=_WINDOW_SECONDS, classes=CUSTOMER_CLASSES):
    """{requests, rpm, error_rate, p50_ms, p95_ms, slowest[], by_class{},
    inflight{}} over the window.

    The headline figures cover customer traffic only (`classes`; None for
    everything): an admin loading a fleet-wide page, the uptime monitor and
    the static files no longer move the p95 owners are measured by (#77).
    by_class carries each class on its own, admin included."""
    cutoff = time.time() - window_seconds
    with _metrics_lock:
        window = [r for r in _samples if r[0] >= cutoff]
        slow = list(_slowest)
    rows = [r for r in window if classes is None or (r[4] if len(r) > 4 else "web") in classes]
    by_class = {}
    for r in window:
        k = r[4] if len(r) > 4 else "web"
        by_class.setdefault(k, []).append(r)
    out = _summary(rows, window_seconds)
    out.update({"window_seconds": window_seconds, "slowest": slow[-_SLOW_KEEP:],
                "classes": list(classes) if classes is not None else None,
                "by_class": {k: _summary(v, window_seconds) for k, v in sorted(by_class.items())},
                "inflight": inflight()})
    return out


def _summary(rows, window_seconds):
    if not rows:
        return {"requests": 0, "rpm": 0.0, "error_rate": 0.0, "server_error_rate": 0.0,
                "p50_ms": None, "p95_ms": None}
    times = sorted(r[1] for r in rows)

    def pct(p):
        return round(times[min(len(times) - 1, int(len(times) * p))], 1)
    errors = sum(1 for r in rows if r[2] >= 400)
    server = sum(1 for r in rows if r[2] >= 500)
    return {
        "requests": len(rows),
        "rpm": round(len(rows) / (window_seconds / 60.0), 1),
        "error_rate": round(errors / len(rows) * 100, 1),
        "server_error_rate": round(server / len(rows) * 100, 1),
        "p50_ms": pct(0.50), "p95_ms": pct(0.95),
    }


def reset_metrics():
    """Test hook."""
    with _metrics_lock:
        _samples.clear()
        _slowest.clear()
        _rollups.clear()
        _pending_5xx.clear()
        _inflight.clear()
        _peak_by_minute.clear()
        _sentry_last.clear()
        for k in _dropped:
            _dropped[k] = 0


def register(app):
    """Attach the handlers. Order matters, and Flask runs after_request
    functions in REVERSE registration order — verified, not assumed — so
    the execution order here is: compression, cache headers, then the
    metric.

    That means the recorded latency INCLUDES this module's own compression,
    which is correct: it is time the client spends waiting on the server.
    Measuring only the view function would report a number nobody
    experiences.

    Compression still runs before cache headers so it sees the final
    headers. The timer is the first before_request the app has (register
    is called before any blueprint), so the request id is bound before any
    other handler logs."""
    app.before_request(_start_timer)
    app.after_request(_record_request)
    app.after_request(add_cache_headers)
    app.after_request(compress_response)
    app.teardown_request(_end_request)
    try:
        from flask import got_request_exception
        got_request_exception.connect(_on_exception, app)
    except Exception as e:
        import logging
        logging.getLogger("http").warning("got_request_exception unavailable: %s", e)
    return app
