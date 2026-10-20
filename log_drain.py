"""log_drain.py — the logs leave the platform (scale audit 10/19/26 OPS-8).

Every log line lived in two places: Railway's log view (its retention, its
search) and, for the rollups, the product's own database — so when the
database was the incident its telemetry was unreadable, and history ended
where Railway's retention did. With LOG_DRAIN_URL set, every process
(web, queue worker) also ships its structured JSON lines to a log service
over HTTPS — Better Stack, Axiom, Datadog's HTTP intake, or anything that
takes a JSON array POST with a bearer token:

  LOG_DRAIN_URL     https endpoint (off when unset — nothing changes)
  LOG_DRAIN_TOKEN   sent as `Authorization: Bearer <token>`
  LOG_DRAIN_LEVEL   the lowest level shipped (default INFO)

Never in the way of the request that logged: a line is put on a bounded
in-memory queue (LOG_DRAIN_QUEUE_MAX, oldest dropped and counted when it is
full) and a daemon thread sends batches of up to LOG_DRAIN_BATCH lines every
LOG_DRAIN_FLUSH_SECONDS, each POST with a timeout. A failed send drops that
batch (counted) — the stdout copy Railway keeps is unchanged. The shipper
never logs through the drain it feeds.
"""
import collections
import json
import logging
import os
import threading
import time

DRAIN_TIMEOUT = (3, 5)
QUEUE_MAX = int(os.getenv("LOG_DRAIN_QUEUE_MAX", "5000"))
BATCH = int(os.getenv("LOG_DRAIN_BATCH", "200"))
FLUSH_SECONDS = float(os.getenv("LOG_DRAIN_FLUSH_SECONDS", "2"))

_lock = threading.Lock()
_queue = collections.deque()
_stats = {"sent": 0, "dropped": 0, "failed_batches": 0, "last_error": None}
_state = {"handler": None, "thread": None, "stop": None}
_SELF = "log_drain"


def configured() -> bool:
    url = (os.getenv("LOG_DRAIN_URL") or "").strip()
    return url.startswith("https://")


def stats() -> dict:
    with _lock:
        return dict(_stats, queued=len(_queue), installed=_state["handler"] is not None)


class DrainHandler(logging.Handler):
    """Formats a record as the JSON line stdout gets and queues it."""

    def emit(self, record):
        if record.name == _SELF:
            return
        try:
            line = self.format(record)
        except Exception:
            return
        with _lock:
            if len(_queue) >= QUEUE_MAX:
                _queue.popleft()
                _stats["dropped"] += 1
            _queue.append(line)


def _take(n):
    with _lock:
        out = []
        while _queue and len(out) < n:
            out.append(_queue.popleft())
        return out


def flush() -> int:
    """Send everything queued now, in batches. Returns lines sent."""
    import requests
    url = (os.getenv("LOG_DRAIN_URL") or "").strip()
    token = (os.getenv("LOG_DRAIN_TOKEN") or "").strip()
    sent = 0
    while True:
        lines = _take(BATCH)
        if not lines:
            return sent
        body = []
        for ln in lines:
            try:
                body.append(json.loads(ln))
            except ValueError:
                body.append({"message": ln})
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            r = requests.post(url, data=json.dumps(body, default=str), headers=headers, timeout=DRAIN_TIMEOUT)
            ok = 200 <= r.status_code < 300
            err = None if ok else f"HTTP {r.status_code}"
        except Exception as e:
            ok, err = False, str(e)[:200]
        with _lock:
            if ok:
                _stats["sent"] += len(lines)
                sent += len(lines)
            else:
                _stats["failed_batches"] += 1
                _stats["dropped"] += len(lines)
                _stats["last_error"] = err
        if not ok:
            return sent


def _loop(stop):
    while not stop.wait(FLUSH_SECONDS):
        try:
            flush()
        except Exception:
            pass
    try:
        flush()
    except Exception:
        pass


def install(start_thread=True):
    """Add the drain to the root logger when LOG_DRAIN_URL is an https URL.
    Idempotent. Returns the handler, or None when the drain is off."""
    if not configured():
        return None
    if _state["handler"] is not None:
        return _state["handler"]
    import logging_setup
    handler = DrainHandler()
    handler._cavnar_drain = True
    handler.addFilter(logging_setup.ContextFilter())
    handler.setFormatter(logging_setup.JsonFormatter())
    handler.setLevel((os.getenv("LOG_DRAIN_LEVEL") or "INFO").upper())
    logging.getLogger().addHandler(handler)
    _state["handler"] = handler
    if start_thread:
        stop = threading.Event()
        t = threading.Thread(target=_loop, args=(stop,), daemon=True, name="log-drain")
        t.start()
        _state.update(thread=t, stop=stop)
        import atexit
        atexit.register(stop.set)
    return handler


def uninstall():
    """Remove the drain (tests)."""
    h = _state["handler"]
    if h is not None:
        logging.getLogger().removeHandler(h)
    if _state["stop"] is not None:
        _state["stop"].set()
    _state.update(handler=None, thread=None, stop=None)
    with _lock:
        _queue.clear()
