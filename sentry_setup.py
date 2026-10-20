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
Every event is scrubbed of secrets before it leaves, frame locals off
(`scrub_event`, re-audit 10/20/26 R7-14).
Layer 0: the SDK, the environment and ai_guard's redaction only.
"""
import os

_done = {"process": None}


def _scrub(value):
    """Strings through ai_guard.redact_secrets (key=, token=, secret=, bearer
    tokens, a JSON access_token), recursively; anything else as it is."""
    from ai_guard import redact_secrets
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub(v) for v in value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def scrub_event(event, hint=None):
    """Sentry's before_send: exception values, the message, the request's URL
    and query string and the extra data scrubbed of secrets; any frame
    locals dropped (include_local_variables is off as well). Never raises —
    an event that cannot be scrubbed is dropped, not sent raw."""
    try:
        for exc in ((event.get("exception") or {}).get("values") or []):
            if exc.get("value"):
                exc["value"] = _scrub(exc["value"])
            for frame in ((exc.get("stacktrace") or {}).get("frames") or []):
                frame.pop("vars", None)
        for key in ("message", "extra", "logentry"):
            if key in event:
                event[key] = _scrub(event[key])
        req = event.get("request")
        if isinstance(req, dict):
            for key in ("url", "data"):
                if key in req:
                    req[key] = _scrub(req[key])
            qs = req.get("query_string")
            if isinstance(qs, str) and qs:
                # The redaction reads a parameter after ? or &; a bare query
                # string has neither in front of its first one.
                req["query_string"] = _scrub("?" + qs)[1:]
            elif qs:
                req["query_string"] = _scrub(qs)
        return event
    except Exception:
        return None


def scrub_breadcrumb(crumb, hint=None):
    try:
        for key in ("message", "data"):
            if key in crumb:
                crumb[key] = _scrub(crumb[key])
        return crumb
    except Exception:
        return None


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
        # Frame locals are off and every event is scrubbed before it leaves
        # (blind re-audit 10/20/26 R7-14): ops.capture hands Sentry the raw
        # exception, whose message can carry a Places URL with key=, and the
        # SDK's own denylist does not match locals named access_token,
        # client_secret or refresh_token.
        include_local_variables=False,
        before_send=scrub_event,
        before_breadcrumb=scrub_breadcrumb,
    )
    try:
        sentry_sdk.set_tag("process", process)
    except Exception:
        pass
    _done["process"] = process
    return True
