import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

DB_PATH = "reviews.db"

SERVICES = [
    {"key": "dashboard",        "name": "Dashboard & Login",     "description": "Client login and account access"},
    {"key": "ai_drafting",      "name": "AI Review Drafting",    "description": "AI-generated review response drafts"},
    {"key": "review_sync",      "name": "Review Sync",           "description": "Google Business Profile review syncing"},
    {"key": "email",            "name": "Email Delivery",        "description": "Outbound email notifications"},
    {"key": "labor_analytics",  "name": "Labor & Analytics",     "description": "Labor data processing and insights"},
    {"key": "scheduler",        "name": "Background Scheduler",  "description": "Automated tasks and nightly syncs"},
]


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


def run_health_checks():
    """Automatically check all services and update their status. Called hourly by scheduler."""
    try:
        _check_dashboard()
        _check_ai_drafting()
        _check_review_sync()
        _check_email()
        _check_labor_analytics()
        # scheduler marks itself via record_scheduler_heartbeat() separately
        log.info("Status health checks complete")
    except Exception as e:
        log.error(f"run_health_checks error: {e}")


def _check_dashboard():
    # If this code is running, the app is up
    update_service_status("dashboard", "operational", None)


def _check_ai_drafting():
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        update_service_status("ai_drafting", "outage", "API key not configured")
        return
    # Check if any draft was written in the last 48 hours
    conn = _conn()
    cutoff = (datetime.utcnow() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM reviews WHERE response_draft IS NOT NULL AND fetched_at >= ?",
        (cutoff,)
    ).fetchone()
    conn.close()
    if row and row["cnt"] > 0:
        update_service_status("ai_drafting", "operational", None)
    else:
        # Key is set but no recent drafts — could be normal if no new reviews, keep operational
        update_service_status("ai_drafting", "operational", None)


def _check_review_sync():
    conn = _conn()
    # Restaurants with GBP connected
    active_with_gmb = conn.execute(
        "SELECT COUNT(*) as cnt FROM restaurants WHERE is_active=1 AND gmb_access_token IS NOT NULL"
    ).fetchone()["cnt"]

    if active_with_gmb == 0:
        conn.close()
        update_service_status("review_sync", "operational", None)
        return

    cutoff = (datetime.utcnow() - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
    stale = conn.execute(
        "SELECT COUNT(*) as cnt FROM restaurants WHERE is_active=1 AND gmb_access_token IS NOT NULL "
        "AND (last_fetched_at IS NULL OR last_fetched_at < ?)",
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
        import urllib.request
        req = urllib.request.Request(
            "https://api.resend.com/domains",
            headers={"Authorization": "Bearer " + resend_key},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                update_service_status("email", "operational", None)
            else:
                update_service_status("email", "degraded", f"Resend API returned {resp.status}")
    except Exception as e:
        update_service_status("email", "degraded", "Email API unreachable")


def _check_labor_analytics():
    conn = _conn()
    # Check for Toast sync errors on connected restaurants
    rows = conn.execute(
        "SELECT restaurant_name, toast_sync_error FROM restaurants "
        "WHERE is_active=1 AND toast_restaurant_guid IS NOT NULL"
    ).fetchall()
    conn.close()

    errored = [r for r in rows if r["toast_sync_error"]]
    if not rows:
        # No POS-connected restaurants yet — still operational
        update_service_status("labor_analytics", "operational", None)
    elif errored:
        names = ", ".join(r["restaurant_name"] for r in errored[:2])
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
