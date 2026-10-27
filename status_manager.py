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


def _conn(db_path=None):
    import models
    path = db_path or DB_PATH
    if models._pg_url_for(path):
        # The platform's database on Postgres: the status, the heartbeat
        # and the incidents live there, never in the volume's idle SQLite
        # file (blind re-audit 10/11/26, SCALE-10).
        return models.get_conn(path)
    c = sqlite3.connect(path)
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


def update_service_status(service_key, status, message=None, db_path=None):
    conn = _conn(db_path)
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
#
# The readers take the database they are judging (`db_path`), so /health,
# the SLA check and the console read the heartbeat from the same file as
# everything else they report; None is this module's DB_PATH.


def _heartbeat_row(db_path=None):
    conn = _conn(db_path)
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


def scheduler_heartbeat_age_minutes(db_path=None):
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
        row = _heartbeat_row(db_path)
    except Exception:
        return None
    if not row or row["beat_age"] is None:
        return None
    return max(0.0, float(row["beat_age"]))


def scheduler_state(db_path=None):
    """The scheduler's liveness in full, for /health, the platform watchdog,
    the status page and the console — the ONE reading of the heartbeat:
    {beat_age_minutes, loop_completed_age_minutes, running_job,
    running_minutes, running_bound_minutes, wedged, loop_stalled, stale,
    state}.

    wedged: the loop has been inside one job past that job's own bound
    (jobs_registry.max_minutes) — the pulse stops vouching for it then, so
    the beat goes stale too. loop_stalled: no tick has COMPLETED within the
    threshold and no job is running to explain it — a tick failing
    part-way, which a fresh beat from the jobs before the failure hid (#121).
    state: "unknown" (heartbeat unreadable), "stale", "wedged", "stalled"
    or "ok" — in that order of precedence."""
    beat = scheduler_heartbeat_age_minutes(db_path) if db_path else scheduler_heartbeat_age_minutes()
    out = {"beat_age_minutes": beat, "loop_completed_age_minutes": None, "running_job": None,
           "running_minutes": None, "running_bound_minutes": None, "wedged": False, "loop_stalled": False,
           "stale": beat is not None and beat > SCHEDULER_STALE_MINUTES, "state": "unknown"}
    try:
        row = _heartbeat_row(db_path)
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
    if beat is not None:
        out["state"] = ("stale" if out["stale"] else "wedged" if out["wedged"]
                        else "stalled" if out["loop_stalled"] else "ok")
    return out


def check_scheduler_liveness(db_path=None):
    """Mark the scheduler down on the public status page when its heartbeat
    has gone stale. Returns the age in minutes (None if unreadable).

    Run by the web process's supervisor (platform_monitor.PlatformSupervisor
    .tick), never by /health, which is read-only. Writes only
    service_status — never the heartbeat — so two checks in a row on a dead
    scheduler both report it stale (#4); and only when the row is not
    already saying so."""
    age = scheduler_heartbeat_age_minutes(db_path) if db_path else scheduler_heartbeat_age_minutes()
    if age is None:
        return None
    if age > SCHEDULER_STALE_MINUTES:
        msg = f"No heartbeat for {int(age)} minutes — scheduled posts and nightly syncs are not running"
        try:
            conn = _conn(db_path)
            try:
                row = conn.execute("SELECT status, message FROM service_status WHERE service_key='scheduler'").fetchone()
            finally:
                conn.close()
        except Exception:
            row = None
        if not row or row["status"] != "outage" or row["message"] != msg:
            update_service_status("scheduler", "outage", msg, db_path=db_path)
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
    if d["state"] == "critical" and d.get("backend") == "postgres":
        # The database is on the Postgres service (R8-06): this volume holds
        # the backups, the logs and the cutover's frozen file.
        update_service_status("storage", "outage", f"App volume almost full — {detail}. The nightly backup no "
                                                   "longer fits.")
        _page_operator("storage_critical",
                       f"App volume almost full — {detail}. The nightly backup's snapshot no longer fits; the "
                       f"database itself is on the Postgres service (its volume is watched separately).")
    elif d["state"] == "critical":
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


# Disk thresholds, as multiples of the live database with an absolute floor
# under each. The fixed 50 MB / 250 MB lines were right for a 7 MB database
# and meaningless for a 2 GB one (#28). "low" is the point where the next
# nightly backup no longer fits: one run needs about
# scheduler.BACKUP_PEAK_SPACE_FACTOR (2.75) times the database free at its
# peak — the work copy, tonight's gzip, the encrypted off-site copy — so 4x
# leaves a night's margin; "critical" the point where even the local
# snapshot no longer fits (scheduler.BACKUP_FREE_SPACE_FACTOR, 1.5x).
#
# On Postgres (scale re-audit 10/20/26 R8-06) "the database" here is the LIVE
# one (pg_database_size), never the SQLite file the cutover left on the app
# volume, which never grows: the backup's export of it is what lands on this
# volume. The Postgres service's own volume is pg_volume_state's.
DISK_LOW_FLOOR_MB = 250
DISK_CRITICAL_FLOOR_MB = 50
DISK_LOW_DB_MULTIPLE = 4.0
DISK_CRITICAL_DB_MULTIPLE = 1.5


def _file_mb(path):
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0.0


def _pg_live_mb(db_path=None):
    """pg_database_size in MB when the database is Postgres, else None. Never
    raises (None)."""
    try:
        import onboarding
        b = onboarding._pg_bytes(db_path)
        return None if b is None else b / (1024 * 1024)
    except Exception as e:
        log.warning("disk: Postgres size unreadable: %s", e)
        return None


def disk_state(db_path=None, live_mb=None):
    """Free space on the APP volume (the one models.DB_PATH lives on), as a
    state plus the numbers behind it: free and total, the database and WAL
    sizes, and the two thresholds this reading was judged against.

    `live_mb` is the live database's size when the caller already has it (on
    Postgres, pg_database_size); otherwise it is read — the Postgres size on
    Postgres, the SQLite file and its WAL on SQLite.

    Shared by _check_storage (which writes the status page), /health (which
    an uptime monitor polls) and the admin console's system card, so none
    of them can disagree about what "nearly full" means.
    """
    import shutil
    from models import DB_PATH
    path = os.path.abspath(db_path or DB_PATH)
    try:
        target = os.path.dirname(path) or "."
        usage = shutil.disk_usage(target)
        free_mb = usage.free / (1024 * 1024)
        pct_free = (usage.free / usage.total * 100) if usage.total else 100.0
    except Exception as e:
        return {"state": "unknown", "error": str(e)[:80]}
    if live_mb is None:
        live_mb = _pg_live_mb(db_path)
    backend = "postgres" if live_mb is not None else "sqlite"
    if live_mb is None:
        db_mb, wal_mb = _file_mb(path), _file_mb(path + "-wal")
        live_mb = db_mb + wal_mb
    else:
        db_mb, wal_mb = float(live_mb), 0.0
    critical_below = max(DISK_CRITICAL_FLOOR_MB, live_mb * DISK_CRITICAL_DB_MULTIPLE)
    low_below = max(DISK_LOW_FLOOR_MB, live_mb * DISK_LOW_DB_MULTIPLE)
    state = "ok"
    if free_mb < critical_below or pct_free < 2:
        state = "critical"
    elif free_mb < low_below or pct_free < 10:
        state = "low"
    return {"state": state, "free_mb": round(free_mb), "pct_free": round(pct_free, 1),
            "total_mb": round(usage.total / (1024 * 1024)),
            "db_mb": round(db_mb, 1), "wal_mb": round(wal_mb, 1), "backend": backend,
            "low_below_mb": round(low_below), "critical_below_mb": round(critical_below)}


# The Postgres service's own volume (scale re-audit 10/20/26 R8-06). The app
# cannot see another Railway service's disk, so its size is configured
# (PG_VOLUME_MAX_BYTES — onboarding reads the same variable for the
# backfill's room) and the database's own size plus its write-ahead log is
# measured against it: past PG_VOLUME_LOW_SHARE a warning, past
# PG_VOLUME_CRITICAL_SHARE a page that cannot wait — a full Postgres volume
# refuses every write on the platform, and /health's db_not_writable was the
# first anyone heard of it. Unset on Postgres is itself a warning: there is
# no early signal without it.
PG_VOLUME_LOW_SHARE = 0.80
PG_VOLUME_CRITICAL_SHARE = 0.90


def pg_volume_max_bytes() -> int:
    try:
        return int(float(os.getenv("PG_VOLUME_MAX_BYTES", "0") or 0))
    except ValueError:
        return 0


def pg_volume_state(db_path=None) -> dict:
    """{state: n/a | unset | ok | low | critical | unknown, db_mb, wal_mb,
    used_mb, max_mb, pct_used, wal_error?}. "n/a" on SQLite. Never raises."""
    try:
        import db_dialect
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
    except Exception as e:
        return {"state": "unknown", "error": str(e)[:120]}
    try:
        if not db_dialect.is_postgres(conn):
            return {"state": "n/a"}
        size = int(conn.execute_native("SELECT pg_database_size(current_database())").fetchone()[0] or 0)
        wal, wal_err = None, None
        try:
            wal = int(conn.execute_native("SELECT COALESCE(SUM(size), 0) FROM pg_ls_waldir()").fetchone()[0] or 0)
        except Exception as e:                 # needs pg_monitor or a superuser; the size alone still counts
            wal_err = str(e)[:120]
            try:
                conn.rollback()
            except Exception:
                pass
    except Exception as e:
        return {"state": "unknown", "error": str(e)[:120]}
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
    mb = 1024 * 1024
    used = size + (wal or 0)
    out = {"db_mb": round(size / mb, 1), "wal_mb": round(wal / mb, 1) if wal is not None else None,
           "used_mb": round(used / mb, 1), "max_mb": None, "pct_used": None}
    if wal_err:
        out["wal_error"] = wal_err
    cap = pg_volume_max_bytes()
    if not cap:
        out["state"] = "unset"
        return out
    pct = used / float(cap)
    out.update(max_mb=round(cap / mb), pct_used=round(pct * 100, 1),
               state="critical" if pct >= PG_VOLUME_CRITICAL_SHARE else "low" if pct >= PG_VOLUME_LOW_SHARE
               else "ok")
    return out


_reference_schema = {}


def _reference_columns():
    """{table: {columns}} of a database built by this code's own boot schema
    (models.boot_schema: init_db, ensure_columns AND the init_* chain — auth,
    webhooks, guest marketing, push, telemetry; release-safety audit
    10/26/26 A05-07) — the schema every deploy migrates to. Read from the
    image's reference_schema.json when the build wrote one for THIS code
    (A05-09: the build phase pays for the scratch boot, not every deploy's
    boot); otherwise built once per process in a scratch file."""
    if "cols" in _reference_schema:
        return _reference_schema["cols"]
    cols = load_reference_artifact()
    if cols is not None:
        _reference_schema["cols"] = cols
        _reference_schema["source"] = "artifact"
        return cols
    cols = build_reference_columns()
    _reference_schema["cols"] = cols
    _reference_schema["source"] = "built"
    return cols


def build_reference_columns():
    """{table: {columns}} of a scratch database built by models.boot_schema."""
    import contextlib
    import io
    import tempfile
    import sqlite3
    import models
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "reference.db")
        with contextlib.redirect_stdout(io.StringIO()):
            models.boot_schema(path)
        conn = sqlite3.connect(path)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            cols = {t: {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')} for t in tables}
        finally:
            conn.close()
    return cols


# The reference schema built at image-build time (release-safety audit
# 10/26/26 A05-09): scripts/build_reference_schema.py, run by the Nixpacks
# build phase (nixpacks.toml), writes it beside the code with a hash of the
# code it was built from. Every boot used to pay a full scratch boot (~4 s
# locally, more on Railway) inside the deploy's outage for it.
REFERENCE_SCHEMA_FILE = "reference_schema.json"
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_NOT_APP_CODE = {"tests", "scripts", "ios", "docs", "static", "templates", "public", "brand", "node_modules",
                 "migrations", "__pycache__", ".git", ".claude", ".github", "demo_store"}


def reference_schema_path() -> str:
    return os.getenv("REFERENCE_SCHEMA_PATH") or os.path.join(_APP_DIR, REFERENCE_SCHEMA_FILE)


def schema_code_hash(root=None) -> str:
    """sha256 over the app's Python source (names and bytes, sorted), so an
    artifact built from other code than this process runs is never used.
    ~20-50 ms, once per process."""
    import hashlib
    root = root or _APP_DIR
    h = hashlib.sha256()
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _NOT_APP_CODE and not d.startswith("."))
        for name in filenames:
            if name.endswith(".py"):
                files.append(os.path.relpath(os.path.join(dirpath, name), root))
    for rel in sorted(files):
        h.update(rel.encode())
        with open(os.path.join(root, rel), "rb") as f:
            h.update(f.read())
    return h.hexdigest()


def write_reference_artifact(path=None, cols=None) -> str:
    """Build the reference schema (or take `cols`) and write it with this
    code's hash. Returns the path written."""
    import json
    import time as _time
    cols = cols if cols is not None else build_reference_columns()
    path = path or reference_schema_path()
    body = {"code_hash": schema_code_hash(), "built_at": _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime()),
            "tables": {t: sorted(c) for t, c in sorted(cols.items())}}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, separators=(",", ":"))
    os.replace(tmp, path)
    return path


def load_reference_artifact(path=None):
    """{table: {columns}} from the build's artifact, or None when there is
    none, it cannot be read, or it was built from other code."""
    import json
    path = path or reference_schema_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            body = json.load(f)
        if body.get("code_hash") != schema_code_hash():
            log.warning("reference schema artifact %s was built from other code; building it here", path)
            return None
        return {t: set(c) for t, c in (body.get("tables") or {}).items()}
    except Exception as e:
        log.warning("reference schema artifact %s unreadable (%s); building it here", path, e)
        return None


def warm_reference_schema():
    """Build the reference schema now, at boot, and return the seconds it
    took. The first /health used to build it — a full init_db on a scratch
    file — inside Railway's deploy-time healthcheck window, straight after
    a boot that had already run twenty init steps (#134)."""
    import time as _time
    started = _time.monotonic()
    _reference_columns()
    return round(_time.monotonic() - started, 3)


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


# A clean schema is re-checked at most this often. The check is ~170
# PRAGMA table_info reads, and /health is public and polled every minute
# (RELIABILITY-25). The cache is keyed by the database's schema_version,
# which SQLite bumps on every CREATE, ALTER and DROP, so a schema change or
# a different file is checked on its next /health regardless.
SCHEMA_RECHECK_SECONDS = 600
_schema_ok = {}


def _schema_gaps_cached(conn, path):
    import time as _time
    try:
        version = conn.execute("PRAGMA schema_version").fetchone()[0]
    except Exception:
        version = None
    key = (os.path.abspath(path), version)
    seen = _schema_ok.get(key)
    if seen is not None and _time.monotonic() - seen < SCHEMA_RECHECK_SECONDS:
        return []
    gaps = _schema_gaps(conn)
    if not gaps:
        if len(_schema_ok) > 64:
            _schema_ok.clear()
        _schema_ok[key] = _time.monotonic()
    return gaps


# A write probe waits this long for the write lock, then reads as busy.
WRITE_PROBE_TIMEOUT_MS = 2000
# Hours without a good snapshot (local, or off-site when configured) before
# /health calls the backup stale: the nightly run plus slack, the same 26
# hours ops.EXPECTED_JOBS gives backup_db.
BACKUP_STALE_HOURS = 26


def health_snapshot(db_path=None):
    """(payload, http_status) for the /health endpoint. READ-ONLY.

    Extracted from hosted_dashboard for the same reason http_layer was:
    importing that module boots the database, seeds demo data, starts the
    scheduler thread and re-runs csrf_protect on already-registered
    blueprints, so anything living there cannot be tested directly.

    Three answers, for two readers:

      * 200 `"status":"ok"` — everything below checked out.
      * 200 `"status":"degraded"` with `problems` — the app is serving but
        something needs a person: a stale scheduler heartbeat, a low or
        critical disk, a stale backup, overdue jobs, a write lock held past
        two seconds, a database not in WAL mode.
      * 500 `"status":"error"` with a generic `error` code — this is not a
        platform that should take traffic: the database cannot be opened or
        read, it lacks tables or columns this code expects (an empty
        restore, a skipped migration — DATA-33), it cannot take a write
        (read-only or failing storage), or it is empty on a volume that has
        held client data (#108).

    Railway's deploy-time healthcheck needs a 200 and only fails a deploy on
    the 500s, which are exactly the cases where a new deployment should not
    be promoted; failing it for a degraded platform would put a deploy
    problem on top of a real one, and block the deploy that fixes it. An
    uptime monitor asserts the keyword `"status":"ok"` in the body (#3), so
    degraded and error both alert it; no nested key is named "status", so
    that keyword cannot match anywhere else.

    Nothing here writes to the database or sends anything (#94). It used to
    stamp the scheduler's status row (resetting the very heartbeat it read)
    and page Will from the request path; the paging now runs on the web
    process's supervisor thread (platform_monitor.PlatformSupervisor), and
    the write probe is BEGIN IMMEDIATE followed by ROLLBACK (#105). Error
    bodies carry a code, never exception text; the detail goes to the log.
    """
    from models import get_conn, DB_PATH
    path = db_path or DB_PATH
    try:
        conn = get_conn(path)
    except Exception:
        log.exception("health: could not open the database")
        return {"status": "error", "error": "db_unavailable"}, 500
    try:
        conn.execute("SELECT 1").fetchone()
        missing = _schema_gaps_cached(conn, path)
        if missing:
            log.error("health: the database lacks %d table(s)/column(s) this code expects: %s",
                      len(missing), ", ".join(missing[:20]))
            return {"status": "error", "error": "schema_mismatch", "missing_count": len(missing)}, 500
        journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0] or "").lower()
        emptied = platform_emptied(conn, path)
        pg_mb = None
        if getattr(conn, "in_transaction", False):
            # On Postgres the reads above opened a transaction (SQLite's
            # SELECTs do not), and the probe refuses to run inside one: it
            # read "unknown" on every poll after the 10/19/26 cutover, so
            # /health could no longer see a database that takes no writes.
            # Only reads happened here — ending it loses nothing.
            import db_dialect
            if db_dialect.is_postgres(conn):
                pg_mb = conn.execute_native("SELECT pg_database_size(current_database())").fetchone()[0] / 1048576
            conn.rollback()
        write = db_write_probe(conn=conn)
    except Exception:
        log.exception("health: database check failed")
        return {"status": "error", "error": "db_unreadable"}, 500
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if write["state"] == "failed":
        log.error("health: write probe failed: %s", write.get("error"))
        return {"status": "error", "error": "db_not_writable"}, 500
    if emptied:
        log.error("health: the database holds no client restaurants, but the volume marker "
                  "says it held some — refusing to report a healthy platform")
        return {"status": "error", "error": "data_missing"}, 500

    problems = []
    if write["state"] == "busy":
        problems.append("db_busy")
    if journal != "wal":
        problems.append("db_not_wal")

    disk = disk_state(path, live_mb=pg_mb) if pg_mb is not None else disk_state(path)
    if disk["state"] in ("low", "critical"):
        problems.append(f"disk_{disk['state']}")

    # Read only: scheduler_state never writes, and reads the loop's own
    # heartbeat table in THIS database. This used to call
    # check_scheduler_liveness, whose "outage" write stamped the very column
    # the age was read from (RELIABILITY-4); the supervisor runs that now.
    try:
        sched = scheduler_state(path)
    except Exception:
        sched = {"state": "unknown", "beat_age_minutes": None, "loop_completed_age_minutes": None}
    code = {"stale": "scheduler_stale", "wedged": "scheduler_wedged",
            "stalled": "scheduler_stalled"}.get(sched["state"])
    if code:
        problems.append(code)

    overdue = []
    try:
        import ops
        # This probe's answer, so the SLA check does not take the write lock a
        # second time; "unknown" (no answer) lets it probe for itself.
        write_ok = None if write["state"] == "unknown" else write["state"] == "ok"
        overdue = ops.check_platform_sla(send=False, db_path=db_path, write_ok=write_ok).get("jobs_overdue") or []
    except Exception:
        overdue = []
    if overdue:
        problems.append("jobs_overdue")

    backup = backup_health(db_path)
    if backup["state"] == "stale":
        problems.append("backup_stale")
    if backup["offsite"] == "stale":
        problems.append("offsite_backup_stale")

    # An index this process's boot could not build (models.note_missing_index
    # — release-safety audit 10/26/26 A05-15 / A05-04): a UNIQUE one over
    # duplicate rows is a guarantee the data does not have; any other ran
    # past its build limit. Degraded, never a 500: the boot went on on
    # purpose, and failing the deploy check would not fix the data.
    try:
        import models as _models_ix
        missing_ix = dict(_models_ix.BOOT_MISSING_INDEXES)
    except Exception:
        missing_ix = {}
    if any(v.get("unique") for v in missing_ix.values()):
        problems.append("constraint_missing")
    if any(not v.get("unique") for v in missing_ix.values()):
        problems.append("index_missing")

    payload = {"status": "degraded" if problems else "ok", "problems": problems,
               "db": "ok",
               "database": {"write": write["state"], "write_ms": write["ms"], "journal": journal,
                            "size_mb": round(pg_mb, 1) if pg_mb is not None else disk.get("db_mb"),
                            "wal_mb": disk.get("wal_mb"),
                            **({"backend": "postgres"} if pg_mb is not None else {})},
               "scheduler": sched["state"], "disk": disk,
               "backup": {**{k: backup[k] for k in ("state", "age_hours", "offsite", "offsite_age_hours")},
                          # ops.backup_status's own verdict on the newest run:
                          # ok | stale | no_offsite | failed | never.
                          "last_run": (backup.get("raw") or {}).get("state")},
               "jobs_overdue": [j["job"] for j in overdue]}
    if missing_ix:
        payload["indexes_missing"] = sorted(missing_ix)
    if sched.get("beat_age_minutes") is not None:
        payload["scheduler_heartbeat_age_minutes"] = round(sched["beat_age_minutes"], 1)
    if sched.get("loop_completed_age_minutes") is not None:
        payload["scheduler_loop_age_minutes"] = round(sched["loop_completed_age_minutes"], 1)
    return payload, 200


def db_write_probe(conn=None, db_path=None, timeout_ms=None) -> dict:
    """Can the database take a write right now? {state, ms, error}.

    state is "ok", "busy" (the write lock was held past timeout_ms — a long
    job, the backup; worth watching, not an outage) or "failed" (read-only
    or failing storage: nothing the app saves will stick). BEGIN IMMEDIATE
    takes the write lock and ROLLBACK gives it straight back, so nothing is
    written. SELECT 1 was the only probe, and a full, locked or read-only
    database passed it (#105). Never raises.

    With `conn`, that connection's busy timeout is restored afterwards (the
    probe shortens it to timeout_ms); without, one is opened and closed."""
    import time as _time
    if timeout_ms is None:
        timeout_ms = WRITE_PROBE_TIMEOUT_MS
    own = conn is None
    if own:
        try:
            from models import get_conn, DB_PATH
            conn = get_conn(db_path or DB_PATH)
        except Exception as e:
            return {"state": "failed", "ms": None, "error": str(e)[:160]}
    elif conn.in_transaction:
        # Never probe inside a caller's transaction: BEGIN would fail, and a
        # ROLLBACK would throw away their work.
        return {"state": "unknown", "ms": None, "error": "connection already in a transaction"}
    started = _time.monotonic()
    began = False

    def _ms():
        return round((_time.monotonic() - started) * 1000, 1)
    try:
        import db_dialect
        if db_dialect.is_postgres(conn):
            # Postgres (blind re-audit 10/20/26 R2-05): BEGIN IMMEDIATE is
            # the platform's EXCLUSIVE advisory lock, bounded only by the
            # 30 s lock_timeout (busy_timeout is a no-op there) — a waiting
            # probe queued every tenant writer behind it, then read the lock
            # timeout as "failed" (500). The probe now waits timeout_ms for
            # the lock SHARED (as a tenant writer does: it never holds the
            # platform up) and asks the server whether it takes writes.
            conn.execute("BEGIN")
            began = True
            conn.execute_native(f"SET LOCAL lock_timeout = '{int(timeout_ms)}ms'")
            conn.execute_native(f"SET LOCAL statement_timeout = '{int(timeout_ms) + 1000}ms'")
            row = conn.execute_native("SELECT pg_is_in_recovery(), current_setting('transaction_read_only')").fetchone()
            if row[0] or str(row[1]).lower() == "on":
                return {"state": "failed", "ms": _ms(), "error": "the database is read-only"}
            conn.execute_native("SELECT pg_advisory_xact_lock_shared(%s)", (db_dialect.WRITE_LOCK_KEY,))
            # An id assigned: refused by a server that takes no writes
            # (wraparound protection, a standby).
            conn.execute_native("SELECT pg_current_xact_id()")
            conn.execute("ROLLBACK")
            began = False
            return {"state": "ok", "ms": _ms(), "error": None}
        conn.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
        conn.execute("BEGIN IMMEDIATE")
        began = True
        conn.execute("ROLLBACK")
        began = False
        return {"state": "ok", "ms": _ms(), "error": None}
    except sqlite3.OperationalError as e:
        msg = str(e)
        busy = "locked" in msg.lower() or "busy" in msg.lower()
        return {"state": "busy" if busy else "failed", "ms": _ms(), "error": msg[:160]}
    except Exception as e:
        return {"state": "failed", "ms": _ms(), "error": str(e)[:160]}
    finally:
        if began:
            try:
                conn.rollback()
            except Exception as e:
                log.warning("write probe could not roll back: %s", e)
        if own:
            try:
                conn.close()
            except Exception as e:
                log.warning("write probe could not close its connection: %s", e)
        else:
            try:
                conn.execute("PRAGMA busy_timeout=30000")
            except Exception as e:
                log.warning("write probe could not restore the busy timeout: %s", e)


def _hours_since(stamp, now=None):
    """Hours from a stored UTC stamp to now, or None."""
    from datetime import timezone
    from time_utils import parse_stamp
    when = parse_stamp(stamp, naive_tz="UTC")
    if when is None:
        return None
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return max(0.0, (ref - when).total_seconds() / 3600.0)


def backup_health(db_path=None, now=None) -> dict:
    """The backup block of /health and of the admin system card, read from
    ops.backup_status (the backup ledger workstream D keeps: last good local
    snapshot, last good off-site copy, size, last error).

    {state: ok|stale|none|unknown, age_hours, size_mb, offsite:
    ok|stale|none|unconfigured|unknown, offsite_age_hours, error, raw}.
    "none" means no good copy is on record yet; whether that is late is
    ops.jobs_overdue's call (backup_db's SLA), so it is not a problem here.
    `error` and `raw` are for the admin card only — /health publishes
    neither."""
    unknown = {"state": "unknown", "age_hours": None, "size_mb": None, "offsite": "unknown",
               "offsite_age_hours": None, "error": None, "raw": None}
    try:
        import ops
        fn = getattr(ops, "backup_status", None)
        if fn is None:
            return unknown
        st = fn(db_path=db_path) or {}
    except Exception as e:
        log.warning("backup status unreadable: %s", e)
        return unknown
    local_age = _hours_since(st.get("last_local_ok_at"), now)
    off_age = _hours_since(st.get("last_offsite_ok_at"), now)

    def _state(age):
        if age is None:
            return "none"
        return "stale" if age > BACKUP_STALE_HOURS else "ok"
    size = st.get("last_size_bytes")
    return {
        "state": _state(local_age),
        "age_hours": round(local_age, 1) if local_age is not None else None,
        "size_mb": round(size / (1024 * 1024), 1) if isinstance(size, (int, float)) else None,
        "offsite": _state(off_age) if st.get("offsite_configured") else "unconfigured",
        "offsite_age_hours": round(off_age, 1) if off_age is not None else None,
        "error": st.get("last_error"),
        "raw": st,
    }


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
    """The public "AI Review Drafting" row, from what the AI layer is doing
    (ai_utils.ai_service_status): the last hour's error rate for calls that
    reached the provider, the breaker (this process's, and the last one any
    process recorded), a recent credential or credit failure, and the
    global budget. It used to read "operational" whenever ANTHROPIC_API_KEY
    was set — through a revoked key, a dead balance and a provider outage
    alike (#104)."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        update_service_status("ai_drafting", "outage", "API key not configured")
        return
    try:
        import ai_utils
        state, message = ai_utils.ai_service_status()
    except Exception as e:
        log.error(f"AI status unavailable: {e}")
        state, message = "operational", None
    update_service_status("ai_drafting", state, message)
    # A breaker that has stayed open pages again (cooled down), from this
    # periodic check rather than from a call that may never come (#48).
    try:
        import ai_utils
        ai_utils.check_ai_alerts()
    except Exception as e:
        log.error(f"AI alert check failed: {e}")


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


# A recorded provider probe younger than this answers for the email row;
# older (or none: the hourly probe not scheduled yet) and the row probes live.
EMAIL_PROBE_FRESH_MINUTES = 90


def _check_email():
    """The email row, from the one definition of "is Resend taking our key"
    (provider_health.probe_resend). This used to ping Resend itself every
    tick and read the answer backwards against Resend's documented codes:
    a 403 (invalid_api_key) counted as operational and a 401 — which is
    also what a valid sending-only key gets — as "key invalid". Nor could
    it see a quota that was refusing every send (#29)."""
    import provider_health
    row = provider_health.latest().get("resend")
    age = _hours_since(row.get("checked_at")) if row else None
    if row is None or age is None or age * 60 > EMAIL_PROBE_FRESH_MINUTES:
        row = provider_health.probe("resend")
    state, detail = row.get("state"), (row.get("detail") or "")
    if state == "unconfigured":
        update_service_status("email", "outage", "Email API key not configured")
    elif state == "ok":
        update_service_status("email", "operational", None)
    elif state == "failing":
        update_service_status("email", "outage",
                              "Email sending limit reached" if "quota" in detail else "Email provider is refusing sends")
    else:
        update_service_status("email", "degraded", "Email provider unreachable")


def _check_labor_analytics():
    """Every POS provider, RPOWER included, through the one provider-agnostic
    reading (pos_health.pos_sync_state). It checked Toast only, so a Square,
    Clover or RPOWER sync failing for days read operational (CA3 F6)."""
    import pos_health
    # Only the POS columns of in-service restaurants with an active login
    # (Audit #23 A6-14: it was SELECT DISTINCT r.* every tick), and RPOWER's
    # stores from their health rollup while it is fresh.
    conn = _conn()
    try:
        cols = ", ".join(f"r.{c}" for c in pos_health.SYNC_COLUMNS)
        import models as _m_sa
        rows = conn.execute(
            f"SELECT DISTINCT {cols} FROM restaurants r JOIN users u ON u.restaurant_id=r.id "
            f"WHERE u.is_active=1 AND COALESCE(r.is_demo,0)=0 AND {_m_sa.in_service_sql('r.billing_status')}"
        ).fetchall()
    finally:
        conn.close()
    roll = pos_health._rpower_rollup()

    # The status page is PUBLIC (status_routes, status.html): it says how
    # many locations are affected, never which — a restaurant's name next to
    # "POS sync error" is its private operating state (re-audit B6#6).
    connected = errored = stale = 0
    for r in rows:
        st = pos_health.pos_sync_state(dict(r))
        if not st.get("connected") or (roll is not None and st["provider"] == "rpower"):
            continue
        connected += 1
        if st["state"] == "error":
            errored += 1
        elif st["state"] == "stale":
            stale += 1
    if roll is not None:
        connected += int(roll["connected"])
        errored += len(roll["failing"])
        stale += len(roll["stale"])
    # Degraded only past a share of the connected locations (A6-14): at 1,500
    # stores one store's revoked token or closed week is that store's own
    # issue (on the console), and the public row read "degraded" forever.
    # Below 1/share locations (today's fleet) any one still degrades it.
    if errored and errored > STATUS_POS_ERROR_SHARE * connected:
        update_service_status("labor_analytics", "degraded", f"POS sync error at {_locations(errored)}")
    elif stale and stale > STATUS_POS_STALE_SHARE * connected:
        update_service_status("labor_analytics", "degraded", f"POS data behind at {_locations(stale)}")
    else:
        update_service_status("labor_analytics", "operational", None)


# The share of connected locations whose POS must be erroring (or stale) before
# the public status page calls POS data degraded (Audit #23 A6-14).
STATUS_POS_ERROR_SHARE = 0.05
STATUS_POS_STALE_SHARE = 0.10


def _locations(n):
    """Qualitative on purpose (Benchmarking audit #47, BM1-21): the status
    page is PUBLIC, and "1 of N locations" or "an error at 1 location on
    POS X" told a competitor the platform's size and could point at one
    known customer. Counts stay on the admin console."""
    return "one or more locations" if n else "no locations"


# Worst first, so an open incident can only make a service look worse.
_STATUS_RANK = {"operational": 0, "maintenance": 1, "degraded": 2, "outage": 3}
# What the status tables accept (their CHECK constraints, models.SCHEMA).
INCIDENT_STATUSES = ("investigating", "monitoring", "resolved")
INCIDENT_SEVERITIES = ("degraded", "outage", "maintenance")
SERVICE_STATUSES = ("operational", "degraded", "outage", "maintenance")


def _incident_keys(inc):
    keys = inc.get("affected_keys")
    if isinstance(keys, str):
        try:
            keys = json.loads(keys or "[]")
        except ValueError:
            keys = []
    return [k for k in (keys or []) if isinstance(k, str)]


def effective_statuses(statuses, incidents=None):
    """The service rows as the public page shows them: each at least as bad
    as any OPEN incident that names it.

    The scheduler's checks rewrite every row each tick, so an operator who
    set Email to "outage" during a Resend incident saw it flip back to
    operational within five minutes, under an incident the banner ignored
    (RELIABILITY-13). An open incident now holds its services at its
    severity until it is resolved. Returns new dicts; the rows are not
    changed."""
    open_incs = [i for i in (incidents or []) if i.get("status") != "resolved"]
    out = []
    for s in statuses:
        row = dict(s)
        for inc in open_incs:
            sev = inc.get("severity") or "degraded"
            if s.get("service_key") in _incident_keys(inc) and \
                    _STATUS_RANK.get(sev, 0) > _STATUS_RANK.get(row.get("status"), 0):
                row["status"] = sev
                row["message"] = inc.get("title") or row.get("message")
                row["incident_id"] = inc.get("id")
        out.append(row)
    return out


def overall_status(statuses, incidents=None):
    """The banner: the worst service, or the worst open incident — an
    incident naming no service still sets the banner."""
    vals = [s["status"] for s in statuses]
    vals += [i.get("severity") or "degraded" for i in (incidents or []) if i.get("status") != "resolved"]
    if "outage" in vals:
        return "outage"
    if "degraded" in vals:
        return "degraded"
    if "maintenance" in vals:
        return "maintenance"
    return "operational"


def get_incident(incident_id):
    """One incident with its updates (newest first), or None."""
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM status_incidents WHERE id=?", (incident_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    inc = dict(row)
    inc["affected_keys"] = _incident_keys(inc)
    inc["updates"] = get_incident_updates(incident_id)
    return inc


# ── the volume marker: "this volume has held client data" (#108) ──────────────
#
# A boot without the volume came up on a fresh ./reviews.db, built a full
# schema, seeded an admin and the demos, and /health said ok — models.py
# records it happening once already. models.require_volume refuses that boot
# on Railway. This marker covers the other half: the volume is there but the
# database on it is empty (deleted, truncated, a restore of the wrong file).
# It is written beside the database once the platform has a client
# restaurant, and from then on an empty database there is refused at boot
# and fails /health. ALLOW_EMPTY_DATABASE=1 (or deleting the file) is the
# deliberate way past it.
VOLUME_MARKER = ".cavnar-volume.json"


def volume_marker_path(db_path=None):
    from models import DB_PATH
    return os.path.join(os.path.dirname(os.path.abspath(db_path or DB_PATH)), VOLUME_MARKER)


def _on_volume(db_path=None):
    """Whether the database lives on the Railway volume — or, with no volume
    on purpose (VOLUME_OPTIONAL), is the platform's own database, whose
    marker lives in object storage (marker_store). Only then is a marker
    written: locally it would be litter in the checkout."""
    from models import DB_PATH
    if _marker_in_object_store(db_path):
        return True
    mount = (os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    if not mount:
        return False
    return os.path.dirname(os.path.abspath(db_path or DB_PATH)) == os.path.abspath(mount)


def _marker_in_object_store(db_path=None):
    """The platform's database on a volume-less service (release-safety audit
    10/26/26, P3): its marker is marker_store's, in object storage."""
    from models import DB_PATH
    if db_path and os.path.abspath(db_path) != os.path.abspath(DB_PATH):
        return False
    try:
        import marker_store
        return marker_store.backend() == "object"
    except Exception as e:
        log.warning("volume marker: backend unknown, using the volume path: %s", e)
        return False


def _allow_empty():
    return (os.getenv("ALLOW_EMPTY_DATABASE") or "").strip().lower() in ("1", "true", "yes")


def client_restaurant_count(conn) -> int:
    """Restaurants that are neither a demo nor Cavnar AI's own (the boot seed
    makes an 'internal' admin restaurant and the demo seed its demos, which
    is exactly what made an empty platform look populated)."""
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM restaurants WHERE id > 0 AND COALESCE(is_demo, 0) = 0 "
            "AND COALESCE(billing_status, '') <> 'internal'").fetchone()[0]
    except sqlite3.OperationalError:
        return conn.execute("SELECT COUNT(*) FROM restaurants WHERE id > 0").fetchone()[0]


def read_volume_marker(db_path=None):
    if _marker_in_object_store(db_path):
        import marker_store
        return marker_store.read(VOLUME_MARKER)
    try:
        with open(volume_marker_path(db_path), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def record_volume_marker(db_path=None, conn=None):
    """Write or refresh the marker when the database on the volume holds a
    client restaurant. Atomic (temp file, then rename). Returns the marker
    written, or None when there is nothing to record. Never raises."""
    if not _on_volume(db_path):
        return None
    own = conn is None
    try:
        if own:
            from models import get_conn, DB_PATH
            conn = get_conn(db_path or DB_PATH)
        n = client_restaurant_count(conn)
    except Exception as e:
        log.warning("volume marker: could not count restaurants: %s", e)
        return None
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if n < 1:
        return None
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    prev = read_volume_marker(db_path) or {}
    marker = {"version": 1, "db": os.path.basename(os.path.abspath(db_path or _db_path())),
              "first_seen_at": prev.get("first_seen_at") or now, "updated_at": now,
              "clients_seen": max(int(prev.get("clients_seen") or 0), int(n))}
    if _marker_in_object_store(db_path):
        try:
            import marker_store
            marker_store.write(VOLUME_MARKER, marker)
        except Exception as e:
            log.warning("volume marker: could not write it to object storage: %s", e)
            return None
        return marker
    path = volume_marker_path(db_path)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(marker, f)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("volume marker: could not write %s: %s", path, e)
        return None
    return marker


def _db_path():
    from models import DB_PATH
    return DB_PATH


def platform_emptied(conn, db_path=None) -> bool:
    """True when the marker says this volume has held client restaurants
    and the database now holds none. False without a marker, or with
    ALLOW_EMPTY_DATABASE=1."""
    if _allow_empty():
        return False
    marker = read_volume_marker(db_path)
    if not marker or int(marker.get("clients_seen") or 0) < 1:
        return False
    try:
        return client_restaurant_count(conn) == 0
    except sqlite3.OperationalError:
        return True        # no restaurants table at all on a volume that had clients


def assert_platform_not_emptied(db_path=None):
    """Boot guard, run BEFORE init_db and the seeds (which would otherwise
    build a fresh schema and an admin over the evidence). Raises
    RuntimeError when the marker says client data existed and the database
    file is missing, empty or holds no client restaurant. Opens the file
    read-only, so a missing database is not created by the check."""
    if _allow_empty():
        return
    marker = read_volume_marker(db_path)
    if not marker or int(marker.get("clients_seen") or 0) < 1:
        return
    from urllib.request import pathname2url
    path = os.path.abspath(db_path or _db_path())
    reason = None
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        reason = "the database file is missing or empty"
    else:
        try:
            conn = sqlite3.connect(f"file:{pathname2url(path)}?mode=ro", uri=True, timeout=10)
            try:
                if client_restaurant_count(conn) == 0:
                    reason = "the database holds no client restaurants"
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                # Unreadable for some other reason (locked, I/O): not proof
                # the data is gone, and init_db will fail on it loudly anyway.
                log.warning("boot: could not check the database for client data: %s", e)
                return
            reason = "the database has no restaurants table"
    if reason:
        raise RuntimeError(
            f"Refusing to boot on an empty platform: {reason}, but {VOLUME_MARKER} on this volume "
            f"says it held {marker.get('clients_seen')} client restaurant(s) as of "
            f"{marker.get('updated_at')}. Restore it (RESTORE_FROM, docs/ops/RECOVERY.md), or set "
            f"ALLOW_EMPTY_DATABASE=1 if emptying it was deliberate.")
