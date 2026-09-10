"""
ops.py — make swallowed failures visible.

Nearly every background job wraps its work in `except Exception: log.error(...)`,
which keeps one bad restaurant from killing the loop — but Railway logs are the
only place the error lands, Sentry never hears about handled exceptions, and
nobody reads logs until a client complains. Every silent failure now flows
through capture(): recorded in a job_failures table, forwarded to Sentry when
configured, and rolled up into a daily 8am digest email if anything failed.
"""
import logging
import uuid
import os


def _html_doc(fragment, bg="#f7f4ef"):
    """Wrap a bare fragment in a real HTML document so its background fills
    the mail client's viewport instead of stopping at the content's height
    (the half-cut-off look). Imported lazily: emails.py reads RESEND_API_KEY
    at module scope, and a module-level import here could bind it before
    load_dotenv() runs. See emails._html_document."""
    from emails import html_document
    return html_document(fragment, bg)



log = logging.getLogger("ops")

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS job_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    error TEXT,
    context TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)
"""


def _ensure_table(conn):
    conn.execute(_TABLE_SQL)


def capture(exc, job="unknown", context=""):
    """Record a handled exception. Never raises — an error reporter that can
    take down the thing it's reporting on is worse than none."""
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(exc)
    except Exception:
        pass
    try:
        from models import get_conn
        conn = get_conn()
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO job_failures (job, error, context) VALUES (?,?,?)",
            (str(job)[:100], str(exc)[:500], str(context)[:200]),
        )
        conn.commit()
        conn.close()
    except Exception as db_err:
        log.error(f"ops.capture could not persist failure ({job}): {db_err}")


_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    started_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT,
    duration_ms INTEGER,
    ok INTEGER,
    error TEXT,
    context TEXT
)
"""


def _record_run_start(name, context=""):
    try:
        from models import get_conn
        conn = get_conn()
        conn.execute(_RUNS_SQL)
        cur = conn.execute("INSERT INTO job_runs (job, context) VALUES (?, ?)", (str(name)[:100], str(context)[:200]))
        run_id = cur.lastrowid
        conn.commit()
        conn.close()
        return run_id
    except Exception:
        return None


def _record_run_end(run_id, started, ok, error=None):
    if run_id is None:
        return
    try:
        import time as _time
        from models import get_conn
        conn = get_conn()
        conn.execute("UPDATE job_runs SET finished_at=datetime('now'), duration_ms=?, ok=?, error=? WHERE id=?",
                     (int((_time.time() - started) * 1000), 1 if ok else 0, (str(error)[:500] if error else None), run_id))
        conn.commit()
        conn.close()
    except Exception as e:
        # log, not capture(): capture writes to the same database this just
        # failed against, so calling it here is the recursion this module
        # exists to avoid. A job stuck with no finished_at shows up on the
        # admin console's Jobs page as "stuck" anyway.
        log.error(f"_record_run_end({run_id}) failed: {e}")


_PERIOD_CLAIM_SQL = """CREATE TABLE IF NOT EXISTS job_period_claims (
    job_key    TEXT PRIMARY KEY,
    claimed_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""


def claim_period(job: str, period: str) -> bool:
    """True the first time `job` is claimed for `period`, False afterwards.

    Replaces the module-level `_last_*_date` globals the scheduler used to
    gate its daily/weekly work. Those had two failure modes the audit caught:

    1. They lived in process memory, so every Railway redeploy reset them —
       a deploy inside a job's hour re-ran that job (re-emailing the whole
       database, re-sending client digests). Deploys are frequent.
    2. Several jobs shared one variable and compared it against values of a
       different type (`_last_fetch_date != today`, where the variable held
       a string like "2026-09-08-6" and `today` was a date), so the guard was
       never satisfied and the job re-ran on every tick — 12 times an hour at
       a 300s tick, including the weekly competitor analysis, which calls
       Google Places and Claude for every full-tier restaurant.

    The PRIMARY KEY insert is the claim, so it is atomic and survives
    restarts. Fails OPEN (returns True) if the bookkeeping table is
    unreachable — a scheduler that silently stops working is worse than one
    that occasionally repeats.
    """
    key = f"{job}:{period}"
    try:
        from models import get_conn
        conn = get_conn()
        conn.execute(_PERIOD_CLAIM_SQL)
        conn.commit()
        try:
            conn.execute("INSERT INTO job_period_claims (job_key) VALUES (?)", (key,))
            conn.commit()
            claimed = True
        except Exception:
            claimed = False
        # Keep the table from growing without bound.
        try:
            conn.execute("DELETE FROM job_period_claims WHERE claimed_at < datetime('now','-45 days')")
            conn.commit()
        except Exception:
            pass
        conn.close()
        return claimed
    except Exception as e:
        log.error(f"claim_period({key}) failed, allowing run: {e}")
        return True


# ── async request-scoped jobs (schedule generation, competitor intel) ───────
#
# These used to live in module-level dicts in client_api.py and
# admin_routes.py. Three problems the audit caught:
#
# 1. Process-local. Railway runs one gunicorn worker today, so it works — but
#    the moment a second worker is added (the obvious response to load), the
#    poll lands on a worker that never saw the job and returns "Job not
#    found" forever, throwing away a 30-second Claude call the client is
#    watching a spinner for. A redeploy mid-job does the same thing.
# 2. Unbounded. A job nobody polls (closed tab, phone locked) stayed in the
#    dict for the life of the process — schedule results are large.
# 3. Untenanted. The poll routes took the job id alone, on the reasoning that
#    a UUID is unguessable. True, but it meant the result could not be scoped
#    even where the caller was authenticated.

_ASYNC_JOB_SQL = """CREATE TABLE IF NOT EXISTS async_jobs (
    job_id        TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    restaurant_id INTEGER,
    status        TEXT NOT NULL,
    result_json   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
)"""

# Long enough for the slowest generation plus a client that backgrounds the
# app mid-poll; short enough that abandoned results don't accumulate.
_ASYNC_JOB_TTL_HOURS = 6


def _async_conn():
    from models import get_conn
    conn = get_conn()
    conn.execute(_ASYNC_JOB_SQL)
    conn.commit()
    return conn


def start_async_job(job_id, kind, restaurant_id):
    """Record a job as pending. Raises nothing — a job whose bookkeeping row
    can't be written still runs; its poll just reports it missing, which is
    the same outcome the old in-memory version gave after a restart."""
    try:
        conn = _async_conn()
        conn.execute(
            "INSERT OR REPLACE INTO async_jobs (job_id, kind, restaurant_id, status, result_json)"
            " VALUES (?,?,?, 'pending', NULL)",
            (str(job_id), str(kind), restaurant_id),
        )
        conn.execute(
            "DELETE FROM async_jobs WHERE created_at < datetime('now', ?)",
            (f"-{_ASYNC_JOB_TTL_HOURS} hours",),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"start_async_job({kind}/{job_id}) failed: {e}")


def finish_async_job(job_id, status, result):
    """Store a finished job's payload. `status` is 'done' or 'error'."""
    import json
    try:
        payload = json.dumps(result)
    except (TypeError, ValueError) as e:
        status, payload = "error", json.dumps({"ok": False, "error": f"Result could not be stored: {e}"})
    try:
        conn = _async_conn()
        conn.execute(
            "UPDATE async_jobs SET status=?, result_json=? WHERE job_id=?",
            (status, payload, str(job_id)),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"finish_async_job({job_id}) failed: {e}")


def read_async_job(job_id, restaurant_id=None):
    """The job's {"status", "result"}, or None if it doesn't exist (or belongs
    to another restaurant). A finished job is deleted as it is read — the
    poll consumes it, matching how the in-memory version popped its entry."""
    import json
    try:
        conn = _async_conn()
        row = conn.execute(
            "SELECT job_id, restaurant_id, status, result_json FROM async_jobs WHERE job_id=?",
            (str(job_id),),
        ).fetchone()
        if not row:
            conn.close()
            return None
        # Scoped where the caller is authenticated: a job belongs to the
        # restaurant that started it, whatever the id in the URL.
        if restaurant_id is not None and row["restaurant_id"] is not None \
                and int(row["restaurant_id"]) != int(restaurant_id):
            conn.close()
            return None
        status = row["status"]
        if status == "pending":
            conn.close()
            return {"status": "pending", "result": None}
        conn.execute("DELETE FROM async_jobs WHERE job_id=?", (str(job_id),))
        conn.commit()
        conn.close()
        try:
            result = json.loads(row["result_json"]) if row["result_json"] else None
        except (TypeError, ValueError):
            return {"status": "error", "result": {"ok": False, "error": "Result could not be read back"}}
        return {"status": status, "result": result}
    except Exception as e:
        log.error(f"read_async_job({job_id}) failed: {e}")
        return None


def inflight_async_jobs(limit=20):
    """What the admin console's Jobs page lists — now every worker's jobs,
    not only the one that happened to serve the request."""
    try:
        conn = _async_conn()
        rows = conn.execute(
            "SELECT job_id, kind, restaurant_id, status, created_at FROM async_jobs"
            " ORDER BY created_at DESC LIMIT ?", (int(limit),)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"inflight_async_jobs failed: {e}")
        return []


def run_job(name, fn, *args, context="", **kwargs):
    """Run a scheduled job with failure capture. Returns the job's result,
    or None if it raised. Every run — not only the failures — lands in
    job_runs (start, end, duration, ok), which is what the admin console's
    Jobs page reads; before this the only record of a job that worked was
    the scheduler's heartbeat."""
    import time as _time
    started = _time.time()
    run_id = _record_run_start(name, context)
    try:
        result = fn(*args, **kwargs)
        _record_run_end(run_id, started, True)
        return result
    except Exception as e:
        log.error(f"Job '{name}' crashed: {e}")
        capture(e, job=name)
        _record_run_end(run_id, started, False, e)
        return None


def failures_last_24h():
    try:
        from models import get_conn
        conn = get_conn()
        _ensure_table(conn)
        rows = conn.execute("""
            SELECT job, COUNT(*) as cnt, MAX(created_at) as last_at,
                   MAX(error) as sample_error
            FROM job_failures
            WHERE created_at >= datetime('now','-1 day')
            GROUP BY job ORDER BY cnt DESC
        """).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


_LEASE_SQL = """CREATE TABLE IF NOT EXISTS scheduler_lease (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    owner        TEXT,
    heartbeat_at TEXT
)"""

# How stale a heartbeat has to be before another process may take over. Must
# comfortably exceed the scheduler tick, or a slow pass loses its own lease
# mid-run and two processes end up holding it.
SCHEDULER_LEASE_STALE_SECONDS = int(os.getenv("SCHEDULER_LEASE_STALE_SECONDS", "1800"))

_LEASE_OWNER = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


def acquire_scheduler_lease(owner: str = None, stale_seconds: int = None) -> bool:
    """True if this process may run the scheduler right now.

    Everything in scheduler_loop is protected by claim_period(), so two
    schedulers mostly collide harmlessly — but "mostly" was doing real work
    there. run_due_posts has no claim of its own (it must publish within
    minutes of a slot, not once a day), and claim_period deliberately fails
    OPEN, so a database hiccup drops the guard for every job at once. The
    only reason none of that has bitten is `--workers 1` in railway.json:
    one character of deploy config standing between the current behaviour
    and every scheduled job running twice.

    So the guarantee moves into the database, where it can be reasoned about.
    The holder refreshes its heartbeat on every tick; if it dies, its lease
    goes stale and another process takes over rather than the scheduler
    simply stopping. Fails OPEN for the same reason claim_period does — a
    bookkeeping outage must not silently stop every scheduled job.
    """
    owner = owner or _LEASE_OWNER
    stale = SCHEDULER_LEASE_STALE_SECONDS if stale_seconds is None else stale_seconds
    try:
        from models import get_conn
        conn = get_conn()
        conn.execute(_LEASE_SQL)
        conn.execute("INSERT OR IGNORE INTO scheduler_lease (id, owner, heartbeat_at) VALUES (1, NULL, NULL)")
        conn.commit()
        cur = conn.execute(
            "UPDATE scheduler_lease SET owner=?, heartbeat_at=datetime('now') "
            "WHERE id=1 AND (owner IS NULL OR owner=? OR heartbeat_at IS NULL "
            "               OR heartbeat_at < datetime('now', ?))",
            (owner, owner, f"-{int(stale)} seconds"),
        )
        conn.commit()
        held = cur.rowcount == 1
        conn.close()
        return held
    except Exception as e:
        log.error(f"acquire_scheduler_lease failed, allowing run: {e}")
        return True


def scheduler_lease_holder():
    """Who currently owns the lease, for the admin console and for tests."""
    try:
        from models import get_conn
        conn = get_conn()
        conn.execute(_LEASE_SQL)
        conn.commit()
        row = conn.execute("SELECT owner, heartbeat_at FROM scheduler_lease WHERE id=1").fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def stuck_jobs(older_than_minutes: int = 90):
    """Jobs that started and never finished — the failure mode nothing reports.

    claim_period() claims BEFORE the work runs, which is exactly what stops a
    redeploy from re-emailing the whole client list. The cost is that a hard
    kill (SIGKILL, an OOM, a container replaced mid-run) leaves the claim
    standing with no code left to release it, so the job does not run again
    until its next slot. For daily_alerts that is a whole day with no alerts
    at all, and because nothing raised, capture() never fired and the digest
    below stayed empty. Silence looked identical to "nothing went wrong".

    A row with started_at and no finished_at is the evidence. The admin
    console already lists these; this is what puts them in the mail.
    """
    try:
        from models import get_conn
        conn = get_conn()
        conn.execute(_RUNS_SQL)
        conn.commit()
        rows = conn.execute(
            "SELECT job, started_at, context FROM job_runs "
            "WHERE finished_at IS NULL AND started_at < datetime('now', ?) "
            "AND started_at >= datetime('now', '-7 days') ORDER BY started_at DESC LIMIT 25",
            (f"-{int(older_than_minutes)} minutes",),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"stuck_jobs failed: {e}")
        return []


def send_failure_digest():
    """Daily 8am: one compact email to the operator if anything failed in the
    last 24h. No failures → no email (silence stays meaningful)."""
    failures = failures_last_24h()
    stuck = stuck_jobs()
    # A job that died without raising leaves no failure row, so "no failures"
    # was never the same thing as "nothing went wrong".
    if not failures and not stuck:
        return False
    resend_key = os.getenv("RESEND_API_KEY", "")
    if not resend_key:
        log.warning("send_failure_digest: RESEND_API_KEY not set — skipping")
        return False
    try:
        import html as _html
        import resend as _resend
        _resend.api_key = resend_key
        will = os.getenv("WILL_EMAIL", "will@cavnar.ai")
        total = sum(f["cnt"] for f in failures)
        stuck_html = ""
        if stuck:
            stuck_rows = "".join(
                f"""<tr>
              <td style="padding:8px 12px;border-bottom:1px solid #e0dbd0;font-weight:600">{_html.escape(j['job'])}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #e0dbd0;font-size:12px;color:#7a736a">started {_html.escape(str(j['started_at'] or ''))}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #e0dbd0;font-size:12px;color:#7a736a">{_html.escape((j['context'] or '')[:120])}</td>
            </tr>"""
                for j in stuck
            )
            stuck_html = f"""
  <p style="font-size:13px;font-weight:600;color:#0e0c0a;margin:22px 0 6px">Started and never finished</p>
  <p style="font-size:12px;color:#7a736a;margin:0 0 10px">These did not raise, so they are not counted above. The job holds its claim until its next slot, so whatever it does was skipped for that period.</p>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
    <tr>
      <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #b7791f">Job</th>
      <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #b7791f">Started</th>
      <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #b7791f">Context</th>
    </tr>
    {stuck_rows}
  </table>"""
        rows_html = "".join(
            f"""<tr>
              <td style="padding:8px 12px;border-bottom:1px solid #e0dbd0;font-weight:600">{_html.escape(f['job'])}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #e0dbd0;text-align:center">{f['cnt']}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #e0dbd0;font-size:12px;color:#7a736a">{_html.escape((f['sample_error'] or '')[:160])}</td>
            </tr>"""
            for f in failures
        )
        _resend.Emails.send({
            "from": f"Cavnar AI Ops <{os.getenv('FROM_EMAIL', 'will@cavnar.ai')}>",
            "to": [will],
            "subject": (f"⚠ {total} background job failure{'s' if total != 1 else ''} in the last 24h"
                        + (f" · {len(stuck)} stuck" if stuck else "")) if failures
                       else f"⚠ {len(stuck)} background job{'s' if len(stuck) != 1 else ''} started and never finished",
            "html": _html_doc(f"""
<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
<div style="font-family:-apple-system,sans-serif;max-width:640px;margin:0 auto;color:#1a1714;background:white;border-radius:12px;padding:28px 24px;box-sizing:border-box">
  <div style="border-top:3px solid #c84b2f;padding-top:20px;margin-bottom:16px">
    <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="150" height="26" alt="Cavnar AI" style="display:block;width:150px;height:26px;border:0;outline:none;margin-bottom:4px">
    <p style="font-size:13px;font-weight:600;color:#0e0c0a;margin:0">Job failures</p>
  </div>
  <table style="width:100%;border-collapse:collapse;font-size:13px;{'' if failures else 'display:none'}">
    <tr>
      <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #c84b2f">Job</th>
      <th style="padding:8px 12px;border-bottom:2px solid #c84b2f">Count</th>
      <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #c84b2f">Latest error</th>
    </tr>
    {rows_html}
  </table>{stuck_html}
  <p style="font-size:12px;color:#7a736a;margin-top:16px">Full stack traces are in Sentry (if configured) and Railway logs.</p>
</div>
</div>"""),
        })
        log.info(f"Failure digest sent to {will} ({total} failures)")
        return True
    except Exception as e:
        log.error(f"send_failure_digest failed: {e}")
        return False
