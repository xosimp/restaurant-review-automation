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
"""
import os

_done = {"process": None}


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
    )
    try:
        sentry_sdk.set_tag("process", process)
    except Exception:
        pass
    _done["process"] = process
    return True
