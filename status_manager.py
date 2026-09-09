import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from models import DB_PATH

log = logging.getLogger(__name__)

SERVICES = [
    {"key": "dashboard",        "name": "Dashboard & Login",     "description": "Client login and account access"},
    {"key": "ai_drafting",      "name": "AI Review Drafting",    "description": "AI-generated review response drafts"},
    {"key": "review_sync",      "name": "Review Sync",           "description": "Google Business Profile review syncing"},
    {"key": "email",            "name": "Email Delivery",        "description": "Outbound email notifications"},
    {"key": "labor_analytics",  "name": "Labor & Analytics",     "description": "Labor data processing and insights"},
    {"key": "scheduler",        "name": "Background Scheduler",  "description": "Automated tasks and nightly syncs"},
    {"key": "scheduled_posts",  "name": "Scheduled Posting",     "description": "Publishing queued marketing posts"},
]

# How long the scheduler's heartbeat may go unstamped before the thread is
# presumed dead. It ticks every SCHEDULER_TICK_SECONDS (300), so three missed
# ticks plus slack — long enough that a slow nightly job can't trip it, short
# enough that a scheduled post is not silently hours late.
SCHEDULER_STALE_MINUTES = 20


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def seed_default_services():
    conn = _conn()
    for svc in SERVICES:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO service_status (service_key, name, description, status) VALUES (?,?,?,?)",
                (svc["key"], svc["name"], svc["description"], "operational"),
            )
        except Exception:
            pass
    conn.commit()
    conn.close()


def get_all_statuses():
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM service_status ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_service_status(service_key, status, message=None):
    conn = _conn()
    conn.execute(
        "UPDATE service_status SET status=?, message=?, updated_at=datetime('now') WHERE service_key=?",
        (status, message, service_key),
    )
    conn.commit()
    conn.close()


def get_open_incidents():
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM status_incidents WHERE status != 'resolved' ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_incidents(limit=10):
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM status_incidents ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_incident_updates(incident_id):
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM status_incident_updates WHERE incident_id=? ORDER BY created_at DESC",
        (incident_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_incident(title, body, affected_keys, severity, status="investigating"):
    conn = _conn()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    keys_json = json.dumps(affected_keys) if isinstance(affected_keys, list) else affected_keys
    cur = conn.execute(
        "INSERT INTO status_incidents (title, body, affected_keys, severity, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (title, body, keys_json, severity, status, now, now),
    )
    incident_id = cur.lastrowid
    if body:
        conn.execute(
            "INSERT INTO status_incident_updates (incident_id, message, status, created_at) VALUES (?,?,?,?)",
            (incident_id, body, status, now),
        )
    conn.commit()
    conn.close()
    return incident_id


def update_incident(incident_id, message, status):
    conn = _conn()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    resolved_at = now if status == "resolved" else None
    conn.execute(
        "UPDATE status_incidents SET status=?, updated_at=?, resolved_at=COALESCE(resolved_at, ?) WHERE id=?",
        (status, now, resolved_at, incident_id),
    )
    conn.execute(
        "INSERT INTO status_incident_updates (incident_id, message, status, created_at) VALUES (?,?,?,?)",
        (incident_id, message, status, now),
    )
    if status == "resolved":
        conn.execute(
            "UPDATE status_incidents SET resolved_at=? WHERE id=? AND resolved_at IS NULL",
            (now, incident_id),
        )
    conn.commit()
    conn.close()


def record_scheduler_heartbeat():
    update_service_status("scheduler", "operational", None)


def scheduler_heartbeat_age_minutes():
    """Minutes since the scheduler last stamped itself, or None if it never
    has. Read from a REQUEST thread (see hosted_dashboard's /health) — the
    scheduler cannot notice its own death, so nothing inside it can be the
    thing that checks.

    This matters because scheduled posts publish from that thread: if it
    stops, nothing throws and nothing 500s. Posts just quietly never go out.
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT updated_at FROM service_status WHERE service_key='scheduler'"
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["updated_at"]:
        return None
    try:
        stamped = datetime.strptime(str(row["updated_at"])[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    # service_status stamps with SQLite's datetime('now'), which is UTC.
    return max(0.0, (datetime.utcnow() - stamped).total_seconds() / 60.0)


def check_scheduler_liveness():
    """Mark the scheduler down when its heartbeat has gone stale. Returns the
    age in minutes (None if it has never run)."""
    age = scheduler_heartbeat_age_minutes()
    if age is None:
        return None
    if age > SCHEDULER_STALE_MINUTES:
        update_service_status(
            "scheduler", "outage",
            f"No heartbeat for {int(age)} minutes — scheduled posts and nightly syncs are not running")
    return age


def run_health_checks():
    """Check every service and update its status. Called from the scheduler.

    Seeds first. update_service_status is a bare UPDATE, so a service added to
    SERVICES after a database was created has no row to update and every check
    for it is a silent no-op — the status page just never mentions it. Seeding
    here rather than only on a /status visit means a new service starts
    reporting as soon as the scheduler ticks, which is well before anyone
    thinks to look at the page.
    """
    try:
        seed_default_services()
    except Exception as e:
        log.error(f"Seeding default services failed: {e}")
    for fn in (_check_dashboard, _check_ai_drafting, _check_review_sync, _check_email,
               _check_labor_analytics, _check_scheduled_posts):
        try:
            fn()
        except Exception as e:
            log.error(f"Health check {fn.__name__} error: {e}")
    log.info("Status health checks complete")


def _check_scheduled_posts():
    """Degrade when queued posts are failing, not when one did.

    A single failure is the owner's to fix and they are emailed about it
    (marketing_publish._alert_failed_post). Several across the last day is
    usually one cause — an expired Meta token, a Google outage — and that is
    what belongs on a status page.
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
            "  SUM(CASE WHEN status='posted' THEN 1 ELSE 0 END) AS posted "
            "FROM marketing_scheduled_posts "
            "WHERE COALESCE(posted_at, created_at) >= datetime('now','-1 day')"
        ).fetchone()
        overdue = conn.execute(
            "SELECT COUNT(*) AS cnt FROM marketing_scheduled_posts "
            "WHERE status='scheduled' AND scheduled_for < datetime('now','-2 hours')"
        ).fetchone()["cnt"]
    except Exception:
        # The table only exists once the marketing migration has run.
        update_service_status("scheduled_posts", "operational", None)
        return
    finally:
        conn.close()

    failed = (row["failed"] if row else 0) or 0
    posted = (row["posted"] if row else 0) or 0

    if overdue:
        # Queued, due, and still sitting there — the shape a stopped
        # scheduler makes, which is exactly the failure nothing else notices.
        update_service_status("scheduled_posts", "outage",
                              f"{overdue} post(s) past their scheduled time and unpublished")
    elif failed and not posted:
        update_service_status("scheduled_posts", "outage",
                              f"{failed} post(s) failed to publish in the last 24h")
    elif failed >= 3:
        update_service_status("scheduled_posts", "degraded",
                              f"{failed} of {failed + posted} posts failed in the last 24h")
    else:
        update_service_status("scheduled_posts", "operational", None)


def _check_dashboard():
    # If this code is running, the app is up
    update_service_status("dashboard", "operational", None)


def _check_ai_drafting():
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        update_service_status("ai_drafting", "outage", "API key not configured")
        return
    # Key present = operational (drafts only happen when new reviews arrive)
    update_service_status("ai_drafting", "operational", None)


def _check_review_sync():
    conn = _conn()
    # Restaurants with GBP connected (join users to get is_active)
    active_with_gmb = conn.execute(
        "SELECT COUNT(*) as cnt FROM restaurants r "
        "JOIN users u ON u.restaurant_id=r.id "
        "WHERE u.is_active=1 AND r.gmb_access_token IS NOT NULL"
    ).fetchone()["cnt"]

    if active_with_gmb == 0:
        conn.close()
        update_service_status("review_sync", "operational", None)
        return

    cutoff = (datetime.utcnow() - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
    stale = conn.execute(
        "SELECT COUNT(*) as cnt FROM restaurants r "
        "JOIN users u ON u.restaurant_id=r.id "
        "WHERE u.is_active=1 AND r.gmb_access_token IS NOT NULL "
        "AND (r.last_fetched_at IS NULL OR r.last_fetched_at < ?)",
        (cutoff,)
    ).fetchone()["cnt"]
    conn.close()

    if stale == 0:
        update_service_status("review_sync", "operational", None)
    elif stale < active_with_gmb:
        update_service_status("review_sync", "degraded", f"{stale} of {active_with_gmb} location(s) not synced in 25h")
    else:
        update_service_status("review_sync", "outage", f"Review sync stale on all {stale} location(s)")


def _check_email():
    resend_key = os.getenv("RESEND_API_KEY", "").strip()
    if not resend_key:
        update_service_status("email", "outage", "Email API key not configured")
        return
    # Optionally ping Resend's API to verify the key is valid
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(
            "https://api.resend.com/domains",
            headers={"Authorization": "Bearer " + resend_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                update_service_status("email", "operational", None)
        except urllib.error.HTTPError as e:
            # 403 = key is valid, just no domain permission — still operational
            if e.code in (200, 403):
                update_service_status("email", "operational", None)
            elif e.code == 401:
                update_service_status("email", "outage", "Email API key invalid")
            else:
                update_service_status("email", "degraded", f"Resend API returned {e.code}")
    except Exception:
        update_service_status("email", "degraded", "Email API unreachable")


def _check_labor_analytics():
    conn = _conn()
    rows = conn.execute(
        "SELECT r.name, r.toast_sync_error FROM restaurants r "
        "JOIN users u ON u.restaurant_id=r.id "
        "WHERE u.is_active=1 AND r.toast_restaurant_guid IS NOT NULL"
    ).fetchall()
    conn.close()

    errored = [r for r in rows if r["toast_sync_error"]]
    if errored:
        names = ", ".join(r["name"] for r in errored[:2])
        update_service_status("labor_analytics", "degraded", f"POS sync error: {names}")
    else:
        update_service_status("labor_analytics", "operational", None)


def overall_status(statuses):
    vals = [s["status"] for s in statuses]
    if "outage" in vals:
        return "outage"
    if "degraded" in vals:
        return "degraded"
    if "maintenance" in vals:
        return "maintenance"
    return "operational"
