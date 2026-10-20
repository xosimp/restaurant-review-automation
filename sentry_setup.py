"""
sentry_setup.py — one Sentry initialisation for every process.

Sentry was initialised only in the web process (hosted_dashboard), so the
queue worker process — schedule generations, auto-drafts, the POS archive
and the nightly learners — and a scheduler run as `worker.py` reported
nothing: `ops.capture` there called `sentry_sdk.capture_exception` on an
SDK that was never initialised, a silent no-op (scale audit 10/19/26 OPS-5,
INF-7). `init(process)` is called by each entry point; every event carries
the process it came from as the `process` tag.

No SENTRY_DSN, no Sentry: a local run and the test suite send nothing.
Layer 0: the SDK and the environment only.

Sampling (blind re-audit 10/20/26 R1-06). Every handled per-restaurant
failure went to Sentry unsampled: a provider outage at 1,500 stores (the
DSR's late-data probes, the POS sync, intraday) is thousands of events an
hour, and the month's quota — the operator's main error channel — went in
hours, exactly after the incident. Now:
  * allow(key): the first event of a key per SENTRY_CAPTURE_WINDOW_SECONDS
    (ops.capture keys on job and exception type — the same failure across
    a fleet is one event a window, the job_failures rows keep every one);
  * before_send: at most SENTRY_MAX_EVENTS_PER_HOUR events an hour from
    this process, whatever raised them, and a logged error (the logging
    integration) also once per window per logger and message.
"""
import os
import threading
import time

_done = {"process": None}

WINDOW_SECONDS = max(0, int(os.getenv("SENTRY_CAPTURE_WINDOW_SECONDS", "600") or 600))
MAX_EVENTS_PER_HOUR = max(1, int(os.getenv("SENTRY_MAX_EVENTS_PER_HOUR", "120") or 120))
_SEEN_MAX = 2000                  # keys remembered; the oldest go first
_lock = threading.Lock()
_seen = {}                        # key -> time.monotonic() its last event was let through
_hour = {"start": None, "n": 0, "dropped": 0}


def allow(key, now=None) -> bool:
    """Whether an event keyed `key` may go to Sentry now: the first per
    WINDOW_SECONDS. Never raises."""
    if WINDOW_SECONDS <= 0:
        return True
    now = time.monotonic() if now is None else now
    try:
        with _lock:
            last = _seen.get(key)
            if last is not None and now - last < WINDOW_SECONDS:
                return False
            _seen[key] = now
            if len(_seen) > _SEEN_MAX:
                for k, _t in sorted(_seen.items(), key=lambda kv: kv[1])[:len(_seen) - _SEEN_MAX]:
                    _seen.pop(k, None)
        return True
    except Exception:
        return True


def _within_ceiling(now=None) -> bool:
    """One more event this hour, under MAX_EVENTS_PER_HOUR."""
    now = time.monotonic() if now is None else now
    with _lock:
        if _hour["start"] is None or now - _hour["start"] >= 3600:
            _hour.update(start=now, n=0, dropped=0)
        if _hour["n"] >= MAX_EVENTS_PER_HOUR:
            _hour["dropped"] += 1
            return False
        _hour["n"] += 1
        return True


def before_send(event, hint=None):
    """Sentry's before_send: the hourly ceiling for every event, and the
    per-window dedupe for a logged error (no exception of its own)."""
    try:
        if not (event or {}).get("exception") and (event or {}).get("logger"):
            msg = (event.get("logentry") or {}).get("message") or event.get("message") or ""
            if not allow(f"log|{event.get('logger')}|{str(msg)[:120]}"):
                return None
        if not _within_ceiling():
            return None
    except Exception:
        return event
    return event


def reset_for_tests():
    with _lock:
        _seen.clear()
        _hour.update(start=None, n=0, dropped=0)


def init(process, integrations=None, traces_sample_rate=0.1) -> bool:
    """Initialise Sentry for this process (`process`: "web", "queue-worker",
    "worker"). Returns whether it is on. Idempotent per process."""
    dsn = (os.getenv("SENTRY_DSN") or "").strip()
    if not dsn:
        return False
    if _done["process"]:
        return True
    import sentry_sdk
    sentry_sdk.init(
        dsn=dsn,
        integrations=list(integrations or []),
        traces_sample_rate=traces_sample_rate,   # 10% of requests for performance tracing (web)
        profiles_sample_rate=0.0,                # off — not needed yet
        environment=os.getenv("RAILWAY_ENVIRONMENT", "production"),
        # The deployed commit, so an error is tied to the build that raised
        # it and a regression shows as new in a release (#38, #77).
        release=(os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT") or None),
        send_default_pii=False,                  # never send PII to Sentry
        before_send=before_send,                 # the hourly ceiling and log dedupe (R1-06)
    )
    try:
        sentry_sdk.set_tag("process", process)
    except Exception:
        pass
    _done["process"] = process
    return True
