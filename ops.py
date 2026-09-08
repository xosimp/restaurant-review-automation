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
    except Exception:
        pass


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


def send_failure_digest():
    """Daily 8am: one compact email to the operator if anything failed in the
    last 24h. No failures → no email (silence stays meaningful)."""
    failures = failures_last_24h()
    if not failures:
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
            "subject": f"⚠ {total} background job failure{'s' if total != 1 else ''} in the last 24h",
            "html": _html_doc(f"""
<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
<div style="font-family:-apple-system,sans-serif;max-width:640px;margin:0 auto;color:#1a1714;background:white;border-radius:12px;padding:28px 24px;box-sizing:border-box">
  <div style="border-top:3px solid #c84b2f;padding-top:20px;margin-bottom:16px">
    <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="150" height="26" alt="Cavnar AI" style="display:block;width:150px;height:26px;border:0;outline:none;margin-bottom:4px">
    <p style="font-size:13px;font-weight:600;color:#0e0c0a;margin:0">Job failures</p>
  </div>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
    <tr>
      <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #c84b2f">Job</th>
      <th style="padding:8px 12px;border-bottom:2px solid #c84b2f">Count</th>
      <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #c84b2f">Latest error</th>
    </tr>
    {rows_html}
  </table>
  <p style="font-size:12px;color:#7a736a;margin-top:16px">Full stack traces are in Sentry (if configured) and Railway logs.</p>
</div>
</div>"""),
        })
        log.info(f"Failure digest sent to {will} ({total} failures)")
        return True
    except Exception as e:
        log.error(f"send_failure_digest failed: {e}")
        return False
