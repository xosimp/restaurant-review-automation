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
    {"key": "storage",          "name": "Data Storage",          "description": "Database volume capacity"},
]

# How long the scheduler's heartbeat may go unstamped before the thread is
# presumed dead. It ticks every SCHEDULER_TICK_SECONDS (300) and the pulse
# stamps it every tick's length while a long job runs, so three missed
# stamps. ONE threshold, shared with ops' platform page and the console
# (jobs_registry.HEARTBEAT_STALE_MINUTES) — they said 15 and 20 (#4).
import jobs_registry as _jobs_registry
SCHEDULER_STALE_MINUTES = _jobs_registry.HEARTBEAT_STALE_MINUTES


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


# ── the scheduler's own liveness (#4, #121) ─────────────────────────────────
#
# The heartbeat lives in its own table (scheduler_heartbeat, created at boot
# by ops.init_ops) and is written ONLY by the scheduler loop. It used to be
# service_status.updated_at, which every update_service_status call stamps —
# so check_scheduler_liveness, marking a dead scheduler "outage", reset the
# clock it had just read (a dead scheduler read as alive after one /health
# request), and so did the console's status "Change".
#
#   beat_at            the loop proved it is alive: the end of a tick, and the
#                      pulse while a job runs within its own bound
#   loop_completed_at  the end of a tick only — the proof every job in it got
#                      its chance; a tick that fails part-way never writes it
#   running_job/since  the job the loop is inside, for the runtime watchdog


def _heartbeat_row():
    conn = _conn()
    try:
        return conn.execute(
            "SELECT running_job, running_since, beat_at, loop_completed_at, "
            "(julianday('now') - julianday(COALESCE(beat_at, created_at))) * 1440.0 AS beat_age, "
            "(julianday('now') - julianday(COALESCE(loop_completed_at, created_at))) * 1440.0 AS loop_age, "
            "(julianday('now') - julianday(running_since)) * 1440.0 AS running_minutes "
            "FROM scheduler_heartbeat WHERE id=1").fetchone()
    finally:
        conn.close()


def record_scheduler_heartbeat(loop_completed=False):
    """Stamp the heartbeat. Called only by the scheduler: at the end of every
    tick (loop_completed=True, which also stamps loop_completed_at and
    clears the running job) and by the pulse while a job runs within its own
    bound. Also marks the public status page's scheduler row operational."""
    conn = _conn()
    try:
        if loop_completed:
            cur = conn.execute("UPDATE scheduler_heartbeat SET beat_at=datetime('now'), "
                               "loop_completed_at=datetime('now'), running_job=NULL, running_since=NULL WHERE id=1")
        else:
            cur = conn.execute("UPDATE scheduler_heartbeat SET beat_at=datetime('now') WHERE id=1")
        if cur.rowcount == 0:
            conn.execute("INSERT OR IGNORE INTO scheduler_heartbeat (id, beat_at, loop_completed_at) "
                         "VALUES (1, datetime('now'), CASE WHEN ? THEN datetime('now') END)", (1 if loop_completed else 0,))
        conn.commit()
    finally:
        conn.close()
    update_service_status("scheduler", "operational", None)


def record_running_job(name=None):
    """The job the loop is inside now (None when it left it), with the time
    it went in — what the per-job runtime watchdog measures (#121). Does not
    stamp the heartbeat: starting a job is not proof the loop completes."""
    conn = _conn()
    try:
        if name:
            conn.execute("UPDATE scheduler_heartbeat SET running_job=?, running_since=datetime('now') WHERE id=1",
                         (str(name)[:100],))
        else:
            conn.execute("UPDATE scheduler_heartbeat SET running_job=NULL, running_since=NULL WHERE id=1")
        conn.commit()
    finally:
        conn.close()


def scheduler_heartbeat_age_minutes():
    """Minutes since the scheduler loop last proved it was alive — from its
    own heartbeat, which nothing else writes. A loop that has never stamped
    counts from when the heartbeat row was created (boot), so a scheduler
    that never started goes stale instead of reading "unknown" forever.
    None only when the heartbeat table cannot be read.

    Read from a REQUEST thread (see hosted_dashboard's /health) — the
    scheduler cannot notice its own death, so nothing inside it can be the
    thing that checks. This matters because scheduled posts publish from
    that thread: if it stops, nothing throws and nothing 500s. Posts just
    quietly never go out.
    """
    try:
        row = _heartbeat_row()
    except Exception:
        return None
    if not row or row["beat_age"] is None:
        return None
    return max(0.0, float(row["beat_age"]))


def scheduler_state():
    """The scheduler's liveness in full, for the platform watchdog and the
    console: {beat_age_minutes, loop_completed_age_minutes, running_job,
    running_minutes, running_bound_minutes, wedged, loop_stalled, stale}.

    wedged: the loop has been inside one job past that job's own bound
    (jobs_registry.max_minutes) — the pulse stops vouching for it then, so
    the beat goes stale too. loop_stalled: no tick has COMPLETED within the
    threshold and no job is running to explain it — a tick failing
    part-way, which a fresh beat from the jobs before the failure hid (#121)."""
    beat = scheduler_heartbeat_age_minutes()
    out = {"beat_age_minutes": beat, "loop_completed_age_minutes": None, "running_job": None,
           "running_minutes": None, "running_bound_minutes": None, "wedged": False, "loop_stalled": False,
           "stale": beat is not None and beat > SCHEDULER_STALE_MINUTES}
    try:
        row = _heartbeat_row()
    except Exception:
        row = None
    if not row:
        return out
    out["loop_completed_age_minutes"] = max(0.0, float(row["loop_age"])) if row["loop_age"] is not None else None
    if row["running_job"]:
        out["running_job"] = row["running_job"]
        out["running_minutes"] = max(0.0, float(row["running_minutes"] or 0))
        out["running_bound_minutes"] = _jobs_registry.max_minutes(row["running_job"])
        out["wedged"] = out["running_minutes"] > out["running_bound_minutes"]
    loop_age = out["loop_completed_age_minutes"]
    out["loop_stalled"] = bool(loop_age is not None and loop_age > SCHEDULER_STALE_MINUTES
                               and not out["running_job"] and not out["stale"])
    return out


def check_scheduler_liveness():
    """Mark the scheduler down on the public status page when its heartbeat
    has gone stale. Returns the age in minutes (None if unreadable).

    Writes only service_status — never the heartbeat — so two checks in a
    row on a dead scheduler both report it stale (#4); and only when the row
    is not already saying so, rather than on every /health request."""
    age = scheduler_heartbeat_age_minutes()
    if age is None:
        return None
    if age > SCHEDULER_STALE_MINUTES:
        msg = f"No heartbeat for {int(age)} minutes — scheduled posts and nightly syncs are not running"
        try:
            conn = _conn()
            try:
                row = conn.execute("SELECT status, message FROM service_status WHERE service_key='scheduler'").fetchone()
            finally:
                conn.close()
        except Exception:
            row = None
        if not row or row["status"] != "outage" or row["message"] != msg:
            update_service_status("scheduler", "outage", msg)
    return age


def _check_storage():
    """Free space on the volume the database lives on.

    Audit #6 had no detector for this at all: a full volume is one of the few
    failures that takes writes down without taking reads down, so the app
    keeps serving, every gate keeps failing open, and the first symptom is
    data quietly not being saved. sqlite raises "database or disk is full" on
    write and every caller in this codebase catches broadly, so it looks like
    a hundred unrelated small failures rather than one cause.
    """
    d = disk_state()
    if d["state"] == "unknown":
        update_service_status("storage", "degraded",
                              f"Could not read volume capacity: {d.get('error', '')}")
        return
    free_mb, pct_free = d["free_mb"], d["pct_free"]

    detail = f"{free_mb:,.0f} MB free ({pct_free:.0f}%)"
    if d["state"] == "critical":
        update_service_status("storage", "outage", f"Volume almost full — {detail}. Writes will start failing.")
        _page_operator("storage_critical",
                       f"Volume almost full — {detail}. SQLite writes will start failing; "
                       f"reads will keep working, so this will look like a hundred "
                       f"unrelated small errors rather than one cause.")
    elif d["state"] == "low":
        update_service_status("storage", "degraded", f"Volume filling up — {detail}")
        _page_operator("storage_low", f"Volume filling up — {detail}")
    else:
        update_service_status("storage", "operational", None)


def _page_operator(key, message):
    """Put a status-page outage into the operator's failure digest.

    update_service_status writes the status page and stops there, so the two
    conditions this module detects that nobody is watching for — a filling
    volume and a dead scheduler — were visible only to someone who already
    suspected something and went looking. ops.capture routes them into
    job_failures, which the 8am digest reads.

    Claimed per day per key so a condition that persists for a week is one
    line in each morning's digest rather than one every scheduler tick.
    """
    try:
        import ops
        from datetime import date
        if ops.claim_period(f"status_page:{key}", date.today().isoformat()):
            ops.capture(RuntimeError(message), job=f"status_{key}", context="status_manager")
    except Exception as e:
        log.warning("could not page operator for %s: %s", key, e)


def disk_state(db_path=None):
    """Free space on the volume the database lives on, as a state plus the
    numbers behind it.

    Shared by _check_storage (which writes the status page) and the /health
    endpoint (which is what an uptime monitor actually polls), so the two
    cannot disagree about what "nearly full" means.
    """
    import shutil
    from models import DB_PATH
    try:
        target = os.path.dirname(os.path.abspath(db_path or DB_PATH)) or "."
        usage = shutil.disk_usage(target)
        free_mb = usage.free / (1024 * 1024)
        pct_free = (usage.free / usage.total * 100) if usage.total else 100.0
    except Exception as e:
        return {"state": "unknown", "error": str(e)[:80]}
    state = "ok"
    if free_mb < 50 or pct_free < 2:
        state = "critical"
    elif free_mb < 250 or pct_free < 10:
        state = "low"
    return {"state": state, "free_mb": round(free_mb), "pct_free": round(pct_free, 1)}


_reference_schema = {}


def _reference_columns():
    """{table: {columns}} of a database built by this code's own init_db and
    ensure_columns — the schema every deploy migrates to. Built once per
    process in a scratch file."""
    if "cols" in _reference_schema:
        return _reference_schema["cols"]
    import contextlib
    import io
    import tempfile
    import sqlite3
    from models import init_db, ensure_columns
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "reference.db")
        with contextlib.redirect_stdout(io.StringIO()):
            init_db(path)
            ensure_columns(db_path=path)
        conn = sqlite3.connect(path)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            cols = {t: {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')} for t in tables}
        finally:
            conn.close()
    _reference_schema["cols"] = cols
    return cols


def _schema_gaps(conn) -> list:
    """Tables or columns the code expects that this database lacks."""
    try:
        ref = _reference_columns()
    except Exception:
        return []          # never fail health on the checker itself
    have_tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    gaps = []
    for table, cols in ref.items():
        if table not in have_tables:
            gaps.append(table)
            continue
        have = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        gaps.extend(f"{table}.{c}" for c in sorted(cols - have))
    return gaps


def health_snapshot(db_path=None):
    """(payload, http_status) for the /health endpoint.

    Extracted from hosted_dashboard for the same reason http_layer was:
    importing that module boots the database, seeds demo data, starts the
    scheduler thread and re-runs csrf_protect on already-registered
    blueprints, so anything living there cannot be tested directly.

    Two conditions are a 500: a database that cannot be read, and one
    missing tables or columns this code expects (an empty restore, a skipped
    migration). Those are the cases where a new deployment genuinely should
    not be promoted over a working one. Everything else — a stale scheduler, a filling volume — is
    a 200 carrying the signal, because the web app is up and serving and
    failing the healthcheck would replace a real problem with a deploy
    problem on top of it.
    """
    from models import get_conn, DB_PATH
    try:
        conn = get_conn(db_path or DB_PATH)
        conn.execute("SELECT 1").fetchone()
        missing = _schema_gaps(conn)
        conn.close()
    except Exception as e:
        return {"status": "error", "db": str(e)}, 500
    if missing:
        # An empty database (a restore that went wrong) or one whose boot
        # migration did not run answers SELECT 1 perfectly well. Neither is
        # a platform that should be promoted (DATA-33).
        return {"status": "error", "db": "schema", "missing": missing[:10]}, 500

    disk = disk_state(db_path)

    scheduler_state, age = "ok", None
    try:
        age = check_scheduler_liveness()
        if age is None:
            scheduler_state = "unknown"
        elif age > SCHEDULER_STALE_MINUTES:
            scheduler_state = "stale"
    except Exception:
        scheduler_state = "unknown"

    # The scheduler's watchdog lives HERE, on the request path an uptime
    # monitor polls, never inside the loop it watches (DH2-2): expected jobs
    # past their SLA, and one out-of-band alert to Will an hour at most.
    overdue = []
    try:
        import ops
        overdue = ops.check_platform_sla(db_path=db_path).get("jobs_overdue") or []
    except Exception:
        overdue = []

    overall = "ok"
    if disk["state"] in ("critical", "low") or scheduler_state == "stale" or overdue:
        overall = "degraded"

    payload = {"status": overall, "db": "ok", "scheduler": scheduler_state, "disk": disk,
               "jobs_overdue": [j["job"] for j in overdue]}
    if age is not None:
        payload["scheduler_heartbeat_age_minutes"] = round(age, 1)
    return payload, 200


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
               _check_labor_analytics, _check_scheduled_posts, _check_storage):
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


# Scheduled review fetches (admin_ops.REVIEW_FETCH_SLOTS) a location may miss
# before the status page calls it stale.
REVIEW_SLOTS_MISSED_STALE = 2


def _check_review_sync():
    conn = _conn()
    # EXACTLY the predicate scheduler.run_daily_fetch selects on. Nothing else
    # is a defensible population to measure staleness against.
    #
    # It read `gmb_access_token IS NOT NULL` originally, which is not the
    # column the fetch branches on. Audit #6 widened it to include any
    # google_place_id, which over-corrected in the other direction: a
    # restaurant with a Place ID but reviews_live=0 is deliberately NOT
    # fetched, so it could never be anything but stale and reported a
    # permanent outage nobody could clear.
    _FETCH_POPULATION = "(r.reviews_live=1 OR r.gmb_refresh_token IS NOT NULL)"
    active_with_gmb = conn.execute(
        "SELECT COUNT(*) as cnt FROM restaurants r "
        "JOIN users u ON u.restaurant_id=r.id "
        f"WHERE u.is_active=1 AND {_FETCH_POPULATION}"
    ).fetchone()["cnt"]

    if active_with_gmb == 0:
        conn.close()
        update_service_status("review_sync", "operational", None)
        return

    # Staleness on the fetch schedule's own clock, through
    # admin_ops.fetch_slots_missed. This compared last_fetched_at — Chicago
    # local with a 'T' — as a STRING against a UTC cutoff with a space, and
    # 'T' sorts after ' ', so any fetch on the cutoff's date never counted as
    # stale: a check meant to fire at 25 hours took up to ~47 (CA3 F10). Two
    # missed slots is the same rule the admin console uses; one can be the
    # bounded pass's tail.
    rows = conn.execute(
        "SELECT DISTINCT r.id, r.last_fetched_at FROM restaurants r "
        "JOIN users u ON u.restaurant_id=r.id "
        f"WHERE u.is_active=1 AND {_FETCH_POPULATION}"
    ).fetchall()
    conn.close()
    from admin_ops import fetch_slots_missed
    stale = 0
    for row in rows:
        missed = fetch_slots_missed(row["last_fetched_at"])
        if missed is None or missed >= REVIEW_SLOTS_MISSED_STALE:
            stale += 1

    if stale == 0:
        update_service_status("review_sync", "operational", None)
    elif stale < active_with_gmb:
        update_service_status("review_sync", "degraded",
                              f"Review sync missed 2+ scheduled fetches at {_locations(stale)}")
    else:
        update_service_status("review_sync", "outage", "Review sync is stale at every location")


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
    """Every POS provider, RPOWER included, through the one provider-agnostic
    reading (pos_health.pos_sync_state). It checked Toast only, so a Square,
    Clover or RPOWER sync failing for days read operational (CA3 F6)."""
    import pos_health
    conn = _conn()
    rows = conn.execute(
        "SELECT DISTINCT r.* FROM restaurants r "
        "JOIN users u ON u.restaurant_id=r.id "
        "WHERE u.is_active=1"
    ).fetchall()
    conn.close()

    # The status page is PUBLIC (status_routes, status.html): it says how
    # many locations are affected, never which — a restaurant's name next to
    # "POS sync error" is its private operating state (re-audit B6#6).
    errored = stale = 0
    for r in rows:
        st = pos_health.pos_sync_state(dict(r))
        if st["state"] == "error":
            errored += 1
        elif st["state"] == "stale":
            stale += 1
    if errored:
        update_service_status("labor_analytics", "degraded", f"POS sync error at {_locations(errored)}")
    elif stale:
        update_service_status("labor_analytics", "degraded", f"POS data behind at {_locations(stale)}")
    else:
        update_service_status("labor_analytics", "operational", None)


def _locations(n):
    """Qualitative on purpose (Benchmarking audit #47, BM1-21): the status
    page is PUBLIC, and "1 of N locations" or "an error at 1 location on
    POS X" told a competitor the platform's size and could point at one
    known customer. Counts stay on the admin console."""
    return "one or more locations" if n else "no locations"


def overall_status(statuses):
    vals = [s["status"] for s in statuses]
    if "outage" in vals:
        return "outage"
    if "degraded" in vals:
        return "degraded"
    if "maintenance" in vals:
        return "maintenance"
    return "operational"
