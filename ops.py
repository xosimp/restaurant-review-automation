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


def run_job(name, fn, *args, **kwargs):
    """Run a scheduled job with failure capture. Returns the job's result,
    or None if it raised."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        log.error(f"Job '{name}' crashed: {e}")
        capture(e, job=name)
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
            "html": f"""
<div style="font-family:-apple-system,sans-serif;max-width:640px;margin:0 auto;color:#1a1714">
  <div style="border-top:3px solid #c84b2f;padding-top:20px;margin-bottom:16px">
    <h2 style="font-family:Georgia,serif;font-size:20px;font-weight:400;margin:0">Cavnar <span style="color:#c84b2f;font-style:italic">AI</span> — job failures</h2>
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
</div>""",
        })
        log.info(f"Failure digest sent to {will} ({total} failures)")
        return True
    except Exception as e:
        log.error(f"send_failure_digest failed: {e}")
        return False
