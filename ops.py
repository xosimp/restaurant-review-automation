"""
ops.py — make swallowed failures visible.

Nearly every background job wraps its work in `except Exception: log.error(...)`,
which keeps one bad restaurant from killing the loop — but Railway logs are the
only place the error lands, Sentry never hears about handled exceptions, and
nobody reads logs until a client complains. Every silent failure now flows
through capture(): recorded in a job_failures table, forwarded to Sentry when
configured, and rolled up into a daily 8am digest email if anything failed.

Also here: every job run (run_job → job_runs), the once-per-period claims
the scheduler gates on, the scheduler lease, retention, the platform SLA
watchdog and the operator's out-of-band pages (SMS, email, push, and an
external dead-man ping), and the backup ledger.
"""
import collections.abc
import logging
import re
import sqlite3
import threading
import time
import uuid
import os
import config
import jobs_registry



log = logging.getLogger("ops")

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS job_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    error TEXT,
    context TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    restaurant_id INTEGER,
    kind TEXT NOT NULL DEFAULT 'job'
)
"""
# Added to job_failures after it shipped (#67): the restaurant a failure is
# about, as a column the client page filters on exactly — it matched
# context LIKE '%rid=N%', which missed the 191 capture sites that write
# 'restaurant_id=N' and matched rid 50-59 when asked for rid 5 — and what
# kind of failure it is.
_FAILURE_COLUMNS = (("restaurant_id", "INTEGER"), ("kind", "TEXT NOT NULL DEFAULT 'job'"))
CAPTURE_KINDS = ("job", "request", "ai_quality", "audit")

# "restaurant_id=5" or "rid=5" in a capture's context, as a whole number:
# "rid=5" never matches "rid=50", and "grid=5" is not a rid.
_RID_RE = re.compile(r"(?<![A-Za-z0-9_])(?:restaurant_id|rid)\s*=\s*(\d+)(?!\d)")


def _ensure_table(conn):
    conn.execute(_TABLE_SQL)


# What a failure captured before job_failures.kind existed was, read from its
# job name and text (#58): AI output checks and console request errors were
# ops.capture calls like any failed job until fix rounds D and G. Used once,
# when the boot migration adds the column, and by the console on a database
# that has not had it yet — never for a row that carries its kind.
_AI_QUALITY_JOBS = frozenset(("safety_disagreement", "ai_quality"))
_AI_QUALITY_MARKERS = ("stated figures not present in its input", "rated normal urgency", "unsupported figure",
                       "validation refused", "refused by validation", "cause claim", "citation dropped",
                       "stated a cause no stored diagnosis supports", "attached figures to the wrong fact",
                       "bullet dropped", "dsr narrative truncated", "dsr narrative failed validation",
                       "dsr narrative lead refused", "dsr narrative dropped")
_REQUEST_JOBS = frozenset(("admin_console", "request"))


def infer_failure_kind(job, error):
    """job | request | ai_quality for a failure with no kind of its own."""
    job = str(job or "").lower()
    err = str(error or "").lower()
    if job in _AI_QUALITY_JOBS or any(m in err for m in _AI_QUALITY_MARKERS):
        return "ai_quality"
    if job in _REQUEST_JOBS:
        return "request"
    return "job"


def restaurant_id_from(context):
    """The restaurant a context string names ("restaurant_id=N" or "rid=N"),
    or None — so the capture sites that already say which restaurant they
    are about need no change to be filterable by it."""
    m = _RID_RE.search(str(context or ""))
    return int(m.group(1)) if m else None


def capture(exc, job="unknown", context="", db_path=None, restaurant_id=None, kind="job"):
    """Record a handled exception. Never raises — an error reporter that can
    take down the thing it's reporting on is worse than none.

    db_path defaults to None (models.DB_PATH, the one real database) rather
    than being silently forced there — a caller running against its own
    database (every isolated test fixture) can now say so, instead of this
    always writing into whichever database happens to be the default in
    that process.

    `restaurant_id` is the restaurant the failure is about; when it is not
    passed it is read from a "restaurant_id=N" / "rid=N" context (#67).
    `kind` is one of CAPTURE_KINDS (job, request, ai_quality, audit)."""
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(exc)
    except Exception:
        pass
    rid = restaurant_id
    if rid is None:
        rid = restaurant_id_from(context)
    try:
        rid = int(rid) if rid is not None else None
    except (TypeError, ValueError):
        rid = None
    kind = kind if kind in CAPTURE_KINDS else "job"
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            # Redacted before it is stored (MOD-REV-8): a requests error carries
            # the URL it failed on, and every Places URL carries key=. This table
            # is shown in the admin console and mailed in the failure digest.
            from ai_guard import redact_secrets
            row = (str(job)[:100], redact_secrets(str(exc))[:500], redact_secrets(str(context))[:200])
            try:
                conn.execute("INSERT INTO job_failures (job, error, context, restaurant_id, kind) VALUES (?,?,?,?,?)",
                             row + (rid, kind))
            except sqlite3.OperationalError:
                # A database booted before the columns existed (init_ops adds
                # them), or one no table was ever made in.
                _ensure_table(conn)
                try:
                    conn.execute("INSERT INTO job_failures (job, error, context, restaurant_id, kind) "
                                 "VALUES (?,?,?,?,?)", row + (rid, kind))
                except sqlite3.OperationalError:
                    conn.execute("INSERT INTO job_failures (job, error, context) VALUES (?,?,?)", row)
            conn.commit()
        finally:
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
    context TEXT,
    result_json TEXT,
    claim_job TEXT
)
"""
# Added to job_runs after it shipped: the sweep's counts (result_json) and
# the claim_period job key the run was claimed under (claim_job), so a dead
# run is reclaimed even where the claim key and the job name differ (DH2-16);
# the restaurant a one-restaurant run is about (#67); the process that ran
# it and the last time that process proved the run alive (owner, pulse_at —
# a run whose pulse stopped is an orphan a deploy killed, #150); and the
# console request a manual run came from (request_id, #153).
# ALTERed at boot by init_ops; a database that already has them skips.
_RUNS_COLUMNS = (("result_json", "TEXT"), ("claim_job", "TEXT"), ("restaurant_id", "INTEGER"),
                 ("owner", "TEXT"), ("pulse_at", "TEXT"), ("request_id", "INTEGER"))

# job_runs.ok: 1 ran clean, 0 failed (raised, or every restaurant it
# attempted failed), 2 PARTIAL — some restaurants failed or the time bound
# cut it short (DH2-1: a night on which every POS sync failed read ok=1).
RUN_OK, RUN_FAILED, RUN_PARTIAL = 1, 0, 2
_RESULT_KEYS = ("attempted", "ok", "failed", "skipped", "hit_bound", "held", "retried")
# Counters that are not a success when a job reports no `attempted` of its
# own: everything else it counts ({sent: 99}, {diagnosed: 3}) is.
_NOT_SUCCESS = frozenset(("attempted", "failed", "skipped", "held", "retried", "hit_bound", "closed",
                          "not_supported", "empty", "expired", "unsupported", "missed"))

# How often a running job proves it is alive (job_runs.pulse_at), and how
# long a silent run may go before it is taken to be dead — its process gone.
RUN_PULSE_SECONDS = int(os.getenv("RUN_PULSE_SECONDS", "60"))
RUN_DEAD_MINUTES = int(os.getenv("RUN_DEAD_MINUTES", "10"))


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def standard_counts(result):
    """{attempted, ok, failed, skipped, hit_bound} for what a job returned,
    or None when it returned nothing countable.

    The contract every scheduled function now meets returns these keys
    itself (#39). A job that counts in its own words is read here: its
    `failed`, its bound (`hit_bound`, or `complete: False`), and every other
    number as a success."""
    if not isinstance(result, dict):
        return None
    has_keys = any(k in result for k in _RESULT_KEYS)
    bound = bool(result.get("hit_bound")) or result.get("complete") is False
    if not has_keys and not bound:
        return None
    failed = _int(result.get("failed"))
    if "attempted" in result:
        attempted = _int(result.get("attempted"))
        ok = _int(result["ok"]) if "ok" in result and not isinstance(result["ok"], bool) \
            else max(0, attempted - failed)
    else:
        ok = sum(_int(v) for k, v in result.items()
                 if k not in _NOT_SUCCESS and isinstance(v, int) and not isinstance(v, bool))
        attempted = ok + failed
    return {"attempted": attempted, "ok": ok, "failed": failed,
            "skipped": _int(result.get("skipped")), "hit_bound": bound}


def run_outcome(result):
    """(job_runs.ok, result_json or None) for what a job returned.

    Judged by its counts (standard_counts): nothing failed and no bound hit
    is 1; every attempt failed is 0; anything between — or a pass the time
    bound cut short — is 2 (partial). One failure among a hundred sends is a
    PARTIAL run, never a failed one (#150): without an `attempted` of its
    own, every other number the job counted is a success. Any other return
    value is a clean run."""
    import json as _json
    counts = standard_counts(result)
    if counts is None:
        return RUN_OK, None
    failed, attempted = counts["failed"], counts["attempted"]
    state = RUN_OK
    if failed and attempted and failed >= attempted:
        state = RUN_FAILED
    elif failed or counts["hit_bound"]:
        state = RUN_PARTIAL
    blob_counts = {k: result.get(k) for k in _RESULT_KEYS if k in result}
    for k, v in counts.items():
        blob_counts.setdefault(k, v)
    try:
        blob = _json.dumps(blob_counts, default=str)[:500]
    except (TypeError, ValueError):
        blob = None
    return state, blob


def _record_run_start(name, context="", db_path=None, claim=None, restaurant_id=None, request_id=None):
    rid = restaurant_id if restaurant_id is not None else restaurant_id_from(context)
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
        conn.execute(_RUNS_SQL)
        try:
            cur = conn.execute(
                "INSERT INTO job_runs (job, context, claim_job, restaurant_id, owner, pulse_at, request_id) "
                "VALUES (?, ?, ?, ?, ?, datetime('now'), ?)",
                (str(name)[:100], str(context)[:200], (str(claim)[:100] if claim else None), rid,
                 _LEASE_OWNER, request_id))
        except sqlite3.OperationalError:
            # A database booted before these columns existed (init_ops adds them).
            cur = conn.execute("INSERT INTO job_runs (job, context) VALUES (?, ?)",
                               (str(name)[:100], str(context)[:200]))
        run_id = cur.lastrowid
        conn.commit()
        conn.close()
        return run_id
    except Exception:
        return None


def _record_run_end(run_id, started, ok, error=None, db_path=None, result_json=None):
    if run_id is None:
        return
    try:
        import time as _time
        from ai_guard import redact_secrets
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
        if isinstance(ok, bool) or ok not in (RUN_OK, RUN_FAILED, RUN_PARTIAL):
            ok = RUN_OK if ok else RUN_FAILED
        args = (int((_time.time() - started) * 1000), ok,
                (redact_secrets(str(error))[:500] if error else None))
        try:
            conn.execute("UPDATE job_runs SET finished_at=datetime('now'), duration_ms=?, ok=?, error=?, "
                         "result_json=? WHERE id=?", args + (result_json, run_id))
        except sqlite3.OperationalError:
            conn.execute("UPDATE job_runs SET finished_at=datetime('now'), duration_ms=?, ok=?, error=? "
                         "WHERE id=?", args + (run_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        # log, not capture(): capture writes to the same database this just
        # failed against, so calling it here is the recursion this module
        # exists to avoid. A job stuck with no finished_at shows up on the
        # admin console's Jobs page as "stuck" anyway.
        log.error(f"_record_run_end({run_id}) failed: {e}")


def _pulse_run(run_id, db_path, stop):
    """Stamp job_runs.pulse_at every RUN_PULSE_SECONDS until the run ends —
    the evidence that tells a live run in another process from one a deploy
    killed (#150). Never raises."""
    while not stop.wait(RUN_PULSE_SECONDS):
        try:
            from models import get_conn
            conn = get_conn(db_path) if db_path else get_conn()
            try:
                conn.execute("UPDATE job_runs SET pulse_at=datetime('now') WHERE id=? AND finished_at IS NULL",
                             (run_id,))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            log.warning(f"run pulse for job_runs {run_id} failed: {e}")


def _failures_during(names, run_id, db_path=None) -> int:
    """How many failures were captured under this run's job name (or claim)
    since the run started — a job that captured a failure and then returned
    normally is not a clean run (#40)."""
    if run_id is None:
        return 0
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            marks = ",".join("?" * len(names))
            row = conn.execute(
                f"SELECT COUNT(*) FROM job_failures WHERE job IN ({marks}) AND COALESCE(context, '') != 'time_bound' "
                "AND created_at >= (SELECT started_at FROM job_runs WHERE id=?)", tuple(names) + (run_id,)).fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


class _OrderedKeySet(collections.abc.MutableSet):
    """A set that remembers insertion order, so the ceiling below can evict
    the OLDEST key. The plain set this replaced popped an arbitrary one —
    often a period claimed a minute ago, re-opening that job's guard in the
    middle of the outage it exists for (DATA-45)."""

    def __init__(self, items=()):
        self._d = dict.fromkeys(items)

    def __contains__(self, key):
        return key in self._d

    def __iter__(self):
        return iter(self._d)

    def __len__(self):
        return len(self._d)

    def add(self, key):
        self._d[key] = None

    def discard(self, key):
        self._d.pop(key, None)

    def pop(self):
        """Remove and return the oldest key."""
        try:
            key = next(iter(self._d))
        except StopIteration:
            raise KeyError("pop from an empty set") from None
        del self._d[key]
        return key

    def clear(self):
        self._d.clear()


# Process-local backstop for claim_period when the database cannot be
# written. Deliberately in memory with a ceiling: it only has to survive the
# outage, not a restart.
_claim_fallback = _OrderedKeySet()
_CLAIM_FALLBACK_MAX = 500

_PERIOD_CLAIM_SQL = """CREATE TABLE IF NOT EXISTS job_period_claims (
    job_key    TEXT PRIMARY KEY,
    claimed_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""

# Once-ever markers ("told this owner about this competitor at this level",
# "first saw this source connected at …"). They lived in job_period_claims,
# which is pruned at 45 days, so each came back every 45 days (#157). This
# table has no retention.
_MARKERS_SQL = """CREATE TABLE IF NOT EXISTS ops_markers (
    key        TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""
# The claim_period keys that were used as permanent markers (GLOB
# patterns), carried into ops_markers once at boot so none of them fires
# again. The calibration marker is "quality_calibration:<rid>:<week>"; the
# loop's own daily "quality_calibration:<date>" claim is not one.
_MARKER_PATTERNS = ("rating_unreadable:*", "competitor_move_told:*", "data_source_seen:*",
                    "quality_calibration:[0-9]*:*")

# A claim whose run started and never finished is taken to be dead after
# this long, and the period may be claimed again (DATA-20). A run still
# alive in THIS process is never reclaimed whatever its age (_running_jobs),
# nor is one whose pulse (job_runs.pulse_at) shows another process still
# running it.
CLAIM_RECLAIM_MINUTES = int(os.getenv("CLAIM_RECLAIM_MINUTES", "120"))

# How many runs of each job name run_job is executing in this process.
_running_jobs = {}
_running_lock = threading.Lock()


# ── run-now requests: the console hands a job to the scheduler (#153) ──────
_RUN_REQUESTS_SQL = """CREATE TABLE IF NOT EXISTS job_run_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job           TEXT NOT NULL,
    requested_by  TEXT,
    requested_at  TEXT NOT NULL DEFAULT (datetime('now')),
    status        TEXT NOT NULL DEFAULT 'pending',
    taken_at      TEXT,
    taken_by      TEXT,
    finished_at   TEXT,
    ok            INTEGER,
    error         TEXT
)"""
# A request the scheduler has not taken within this long is not run late: a
# "Run now" pressed during an outage must not fire hours afterwards.
RUN_REQUEST_TTL_MINUTES = int(os.getenv("RUN_REQUEST_TTL_MINUTES", "30"))

# ── the scheduler's own liveness (status_manager's heartbeat functions) ────
# In its own table, written only by the loop (#4): the heartbeat was
# service_status.updated_at, which the liveness check and the console's
# status "Change" both reset.
_HEARTBEAT_SQL = """CREATE TABLE IF NOT EXISTS scheduler_heartbeat (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    beat_at           TEXT,
    loop_completed_at TEXT,
    running_job       TEXT,
    running_since     TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
)"""

# ── the backup ledger (#1, #2, #28) ─────────────────────────────────────────
_BACKUP_RUNS_SQL = """CREATE TABLE IF NOT EXISTS backup_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at    TEXT,
    local_ok       INTEGER,
    integrity_ok   INTEGER,
    size_bytes     INTEGER,
    local_path     TEXT,
    offsite_ok     INTEGER,
    offsite_target TEXT,
    offsite_error  TEXT,
    sha256         TEXT,
    db_bytes       INTEGER,
    wal_bytes      INTEGER,
    backups_bytes  INTEGER,
    free_bytes     INTEGER,
    detail_json    TEXT
)"""

# ── what the operator was last paged about (#27) ───────────────────────────
_OPERATOR_ALERTS_SQL = """CREATE TABLE IF NOT EXISTS operator_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL,
    subject     TEXT,
    sent        INTEGER NOT NULL DEFAULT 0,
    channels    TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
)"""

# ── windows that closed with nothing sent (#131) ───────────────────────────
_MISSED_WINDOWS_SQL = """CREATE TABLE IF NOT EXISTS missed_windows (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job           TEXT NOT NULL,
    restaurant_id INTEGER,
    local_date    TEXT NOT NULL,
    detail        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(job, restaurant_id, local_date)
)"""

# ── when this database first expected each job (#31) ───────────────────────
# jobs_overdue measured a job that has never succeeded from the OLDEST run
# of any job — 45 days back on a live database — so a job a release adds (the
# Monday operator digest, a new billing job) read as weeks overdue from the
# first /health after the deploy and paged Will every hour until it first
# ran. One row per registry job, written at boot, never updated.
_EXPECTED_SINCE_SQL = """CREATE TABLE IF NOT EXISTS job_expected_since (
    job    TEXT PRIMARY KEY,
    since  TEXT NOT NULL DEFAULT (datetime('now'))
)"""


def init_ops(db_path=None):
    """Create this module's tables, and their indexes, at boot.

    Called from models.init_db, so every database the app or a test builds
    has them. claim_period used to run CREATE TABLE IF NOT EXISTS and an
    unindexed full-table prune on every call (DATA-6 / MOD-PERF-3): two
    statements under SQLite's write lock for every restaurant a sweep
    claims."""
    from models import get_conn
    conn = get_conn(db_path) if db_path else get_conn()
    try:
        for sql in (_TABLE_SQL, _RUNS_SQL, _PERIOD_CLAIM_SQL, _ASYNC_JOB_SQL, _LEASE_SQL, _MARKERS_SQL,
                    _RUN_REQUESTS_SQL, _HEARTBEAT_SQL, _BACKUP_RUNS_SQL, _OPERATOR_ALERTS_SQL,
                    _MISSED_WINDOWS_SQL, _EXPECTED_SINCE_SQL):
            conn.execute(sql)
        conn.executemany("INSERT OR IGNORE INTO job_expected_since (job) VALUES (?)",
                         [(job,) for job in EXPECTED_JOBS])
        for table, columns in (("job_runs", _RUNS_COLUMNS), ("job_failures", _FAILURE_COLUMNS),
                               ("scheduler_lease", _LEASE_COLUMNS)):
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in columns:
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                    if (table, col) == ("job_failures", "kind"):
                        _classify_legacy_failures(conn)
        for sql in (
            # The retention deletes in prune_ledgers (DATA-40), and the
            # dead-run lookup in _reclaim_dead_run.
            "CREATE INDEX IF NOT EXISTS idx_job_failures_created ON job_failures(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_job_runs_started ON job_runs(started_at)",
            "CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs(job, started_at)",
            "CREATE INDEX IF NOT EXISTS idx_job_period_claims_at ON job_period_claims(claimed_at)",
            # The client page reads a restaurant's failures and runs by the
            # column, exactly (#67).
            "CREATE INDEX IF NOT EXISTS idx_job_failures_rid ON job_failures(restaurant_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_job_runs_rid ON job_runs(restaurant_id, started_at)",
            "CREATE INDEX IF NOT EXISTS idx_job_runs_open ON job_runs(finished_at, started_at)",
            "CREATE INDEX IF NOT EXISTS idx_job_run_requests_status ON job_run_requests(status, id)",
            "CREATE INDEX IF NOT EXISTS idx_job_run_requests_at ON job_run_requests(requested_at)",
            "CREATE INDEX IF NOT EXISTS idx_backup_runs_started ON backup_runs(started_at)",
            "CREATE INDEX IF NOT EXISTS idx_operator_alerts_created ON operator_alerts(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_missed_windows_created ON missed_windows(created_at)",
        ):
            conn.execute(sql)
        conn.execute("INSERT OR IGNORE INTO scheduler_heartbeat (id) VALUES (1)")
        # The permanent markers that lived in the pruned claims table (#157),
        # carried over once with the time each was first claimed.
        for pattern in _MARKER_PATTERNS:
            conn.execute("INSERT OR IGNORE INTO ops_markers (key, created_at) "
                         "SELECT job_key, claimed_at FROM job_period_claims WHERE job_key GLOB ?", (pattern,))
        _ensure_retention_indexes(conn)
        conn.commit()
    finally:
        conn.close()


def _classify_legacy_failures(conn):
    """Give the rows captured before job_failures.kind existed their kind,
    once, as the column is added: the column's default made every one a
    failed job, so the AI output checks and console request errors of the
    last weeks would have read as "Job X failed" until they aged out."""
    rows = conn.execute("SELECT id, job, error FROM job_failures").fetchall()
    for fid, job, error in rows:
        kind = infer_failure_kind(job, error)
        if kind != "job":
            conn.execute("UPDATE job_failures SET kind=? WHERE id=?", (kind, fid))


def _memo_claim(key, reason):
    """The in-memory answer while the claims table cannot be written: the
    first ask in this process runs the job, every later one refuses."""
    first_time = key not in _claim_fallback
    _claim_fallback.add(key)
    while len(_claim_fallback) > _CLAIM_FALLBACK_MAX:
        # Bounded: a long outage across many jobs must not grow this without
        # limit. The oldest key goes — the period least likely to be asked
        # about again.
        _claim_fallback.pop()
    log.error(f"claim_period({key}) failed, {'allowing' if first_time else 'refusing'} "
              f"run from process memory: {reason}")
    return first_time


def _run_alive_sql(alias=""):
    """SQL true for an unfinished job_runs row whose process is still
    running it: its pulse (stamped every RUN_PULSE_SECONDS) is fresh. A row
    from before pulses existed (pulse_at NULL) is judged by the claim age
    alone, as it always was."""
    p = f"{alias}." if alias else ""
    return (f"({p}finished_at IS NULL AND {p}pulse_at IS NOT NULL "
            f"AND {p}pulse_at >= datetime('now', '-{int(RUN_DEAD_MINUTES)} minutes'))")


def _close_runs(conn, where, args, reason):
    """Mark matching unfinished runs as interrupted. Returns how many."""
    try:
        cur = conn.execute(
            "UPDATE job_runs SET finished_at=datetime('now'), ok=0, "
            "duration_ms=CAST((julianday('now') - julianday(started_at)) * 86400000 AS INTEGER), "
            f"error=? WHERE finished_at IS NULL AND {where}", (reason,) + tuple(args))
        return max(0, cur.rowcount or 0)
    except sqlite3.OperationalError as e:
        log.warning(f"could not close interrupted runs: {e}")
        return 0


def _reclaim_dead_run(conn, job, key):
    """True if `key` was claimed by a run of `job` that started and never
    finished, long enough ago to be dead — the claim is then taken over
    (restamped) for this caller, and the dead run is closed as interrupted
    (#150). A deploy SIGKILLs mid-run: the claim row stands, job_runs holds
    a start and no finish, and without this the job did not run again until
    its next period — for a daily job, a whole day of alerts that never went
    out (DATA-20).

    Only a claim with that evidence is reclaimed. One with no run row at
    all, or whose run finished (even with an error), or whose run another
    process is still pulsing, is left alone: re-sending a digest is worse
    than missing one."""
    with _running_lock:
        if _running_jobs.get(job):
            return False                       # still running here, however long
    row = conn.execute(
        "SELECT claimed_at FROM job_period_claims WHERE job_key=? AND claimed_at < datetime('now', ?)",
        (key, f"-{int(CLAIM_RECLAIM_MINUTES)} minutes")).fetchone()
    if not row:
        return False
    claimed_at = row[0]
    # The run is found by its job name OR the claim it ran under: the
    # "ops_digest" claim runs "ops_failure_digest", "intraday" runs six jobs,
    # and a lookup by claim key alone never found them (DH2-16).
    try:
        runs = conn.execute(
            "SELECT SUM(finished_at IS NULL), SUM(finished_at IS NOT NULL), "
            f"SUM({_run_alive_sql()}) FROM job_runs "
            "WHERE (job=? OR claim_job=?) AND started_at >= datetime(?, '-5 minutes')",
            (job, job, claimed_at)).fetchone()
    except sqlite3.OperationalError:
        runs = conn.execute(
            "SELECT SUM(finished_at IS NULL), SUM(finished_at IS NOT NULL), 0 FROM job_runs "
            "WHERE job=? AND started_at >= datetime(?, '-5 minutes')", (job, claimed_at)).fetchone()
    if not runs or not runs[0] or runs[1] or runs[2]:
        return False
    cur = conn.execute("UPDATE job_period_claims SET claimed_at=datetime('now') "
                       "WHERE job_key=? AND claimed_at=?", (key, claimed_at))
    if cur.rowcount == 1:
        try:
            _close_runs(conn, "(job=? OR claim_job=?) AND started_at >= datetime(?, '-5 minutes')",
                        (job, job, claimed_at), "interrupted — the process running it ended; the period was reclaimed")
        except Exception:
            pass
    conn.commit()
    if cur.rowcount == 1:
        log.error(f"claim_period({key}): the run claimed at {claimed_at} never finished — reclaimed")
        return True
    return False


def _claimed_on_read(conn, key):
    """Whether `key` is in the claims table, read without the write lock (a
    WAL read never blocks). None when even the read fails."""
    try:
        return conn.execute("SELECT 1 FROM job_period_claims WHERE job_key=?", (key,)).fetchone() is not None
    except Exception:
        return None


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
    restarts. A key already there is read first, without the write lock —
    most calls ask about a period an earlier tick already claimed.

    A claim that cannot be written (a full, read-only or locked volume) is
    checked read-only first (#111): a period the table already holds is
    refused, whatever the lock. "database is locked" used to fall through to
    the memo below, which answered True the first time a process asked —
    re-running an already-claimed brief, digest or alert batch. Only a
    period the table does NOT hold runs from the process-local memo: the
    first ask in this process runs it, every later one refuses, and it is
    written to the table (and refused) once the database answers again
    (DATA-22). acquire_scheduler_lease fails open on the same dependency, so
    "allow every time" would re-run every due job on every tick.

    A claim whose run was killed before it finished is reclaimed after
    CLAIM_RECLAIM_MINUTES (_reclaim_dead_run). The table is created at boot
    (init_ops) and pruned by prune_ledgers, never here.
    """
    key = f"{job}:{period}"
    try:
        from models import get_conn
        conn = get_conn()
    except Exception as e:
        return _memo_claim(key, e)
    try:
        if key in _claim_fallback:
            # Already run from memory during an outage: record it durably
            # now the database answers, and refuse (DATA-22).
            conn.execute("INSERT OR IGNORE INTO job_period_claims (job_key) VALUES (?)", (key,))
            conn.commit()
            return False
        if _claimed_on_read(conn, key):
            try:
                return _reclaim_dead_run(conn, job, key)
            except Exception as e:
                log.error(f"claim_period({key}) could not check for a dead run: {e}")
                return False
        try:
            conn.execute("INSERT INTO job_period_claims (job_key) VALUES (?)", (key,))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
        try:
            return _reclaim_dead_run(conn, job, key)
        except Exception as e:
            log.error(f"claim_period({key}) could not check for a dead run: {e}")
            return False
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        if _claimed_on_read(conn, key):
            log.error(f"claim_period({key}) could not write ({e}); the period is already claimed — refused")
            return False
        return _memo_claim(key, e)
    finally:
        conn.close()


def claim_cooldown(key: str, minutes: float) -> bool:
    """True, and stamps `key`, unless it was stamped within the last
    `minutes` — one atomic upsert on job_period_claims, so two presses at
    the same instant cannot both pass. For actions that must not repeat on a
    double-click but may be repeated deliberately later (an admin resending
    a welcome email). Fails OPEN on a database error, like claim_period."""
    try:
        from models import get_conn
        conn = get_conn()
        try:
            cur = conn.execute(
                "INSERT INTO job_period_claims (job_key, claimed_at) VALUES (?, datetime('now')) "
                "ON CONFLICT(job_key) DO UPDATE SET claimed_at=datetime('now') "
                "WHERE job_period_claims.claimed_at <= datetime('now', ?)",
                (f"cooldown:{key}", f"-{float(minutes) * 60:.0f} seconds"))
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()
    except Exception as e:
        log.error(f"claim_cooldown({key}) failed, allowing: {e}")
        return True


def period_claimed(job: str, period: str) -> bool:
    """Whether `job` already holds `period`, without claiming it. For work
    that claims only once it has finished (notify's morning batch claims at
    flush), so a pass can skip what an earlier pass completed. Fails open
    (False): run again rather than silently skip."""
    key = f"{job}:{period}"
    if key in _claim_fallback:
        return True
    try:
        from models import get_conn
        conn = get_conn()
        try:
            return conn.execute("SELECT 1 FROM job_period_claims WHERE job_key=?",
                                (key,)).fetchone() is not None
        finally:
            conn.close()
    except Exception:
        return False


def release_period(job: str, period: str) -> None:
    """Give back a claim_period claim, so the next tick can try the work
    again. For jobs that claim BEFORE working (so two ticks cannot run it at
    once) but must not lose the period when the work then fails."""
    key = f"{job}:{period}"
    _claim_fallback.discard(key)
    try:
        from models import get_conn
        conn = get_conn()
        try:
            conn.execute("DELETE FROM job_period_claims WHERE job_key=?", (key,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.error(f"release_period({key}) failed: {e}")


# ── permanent markers (#157) ────────────────────────────────────────────────

def claim_marker(job: str, value: str) -> bool:
    """True the first time `job:value` is marked, False ever after — for
    "once, ever" facts (this competitor at this level was told; this reading
    was reported unreadable; this source was first seen at …). The same
    shape as claim_period, in a table that is never pruned. Falls back to
    the process memo, like claim_period, when the table cannot be written."""
    key = f"{job}:{value}"
    try:
        from models import get_conn
        conn = get_conn()
    except Exception as e:
        return _memo_claim(key, e)
    try:
        if key in _claim_fallback:
            conn.execute("INSERT OR IGNORE INTO ops_markers (key) VALUES (?)", (key,))
            conn.commit()
            return False
        try:
            conn.execute("INSERT INTO ops_markers (key) VALUES (?)", (key,))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            if conn.execute("SELECT 1 FROM ops_markers WHERE key=?", (key,)).fetchone():
                return False
        except Exception:
            pass
        return _memo_claim(key, e)
    finally:
        conn.close()


def release_marker(job: str, value: str) -> None:
    """Undo claim_marker — the send it guarded did not happen."""
    key = f"{job}:{value}"
    _claim_fallback.discard(key)
    try:
        from models import get_conn
        conn = get_conn()
        try:
            conn.execute("DELETE FROM ops_markers WHERE key=?", (key,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.error(f"release_marker({key}) failed: {e}")


def marker_created_at(job: str, value: str):
    """When `job:value` was first marked (UTC "YYYY-MM-DD HH:MM:SS"), or None."""
    try:
        from models import get_conn
        conn = get_conn()
        try:
            row = conn.execute("SELECT created_at FROM ops_markers WHERE key=?", (f"{job}:{value}",)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:
        return None


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


# The longest a generation can plausibly run: a very large roster is written
# in a dozen or more calls of a minute or two each. A job still pending past
# this is dead, whatever process owned it.
JOB_MAX_MINUTES = 45


def sweep_stale_jobs(older_than_minutes: int = 0) -> int:
    """Fail every job still pending from before this process started.

    Schedule generation runs on a daemon thread inside the web process, and
    a daemon thread is killed at interpreter exit without running its
    finally blocks — so a deploy, crash or restart mid-generation left the
    row pending forever, the client polling until it timed out, and a paid
    model call lost with no error anybody could see. Called at boot, which
    is exactly when the previous process was the one that died — so its
    jobs are dead whatever their age. Only jobs older than ten minutes used
    to be swept, and a press in the next ten minutes joined the dead one
    (SCHED-25 / DATA-9).
    """
    try:
        conn = _async_conn()
        cur = conn.execute(
            "UPDATE async_jobs SET status='error', result_json=? "
            "WHERE status='pending' AND created_at <= datetime('now', ?)",
            ('{"ok": false, "error": "Generation was interrupted — please try again."}',
             f"-{int(older_than_minutes)} minutes"))
        conn.commit()
        n = cur.rowcount or 0
        conn.close()
        if n:
            print(f"[ops] swept {n} stale pending job(s)")
        return n
    except Exception as e:
        print(f"sweep_stale_jobs failed: {e}")
        return 0


def active_job(kind, restaurant_id, max_age_minutes: int = JOB_MAX_MINUTES):
    """The job_id of a pending job of this kind for this restaurant, started
    within `max_age_minutes`, or None. Two owners pressing Generate at once
    used to produce two model calls and two history rows; the second press
    now joins the first job and polls it. The window is the longest a
    generation can run (a big roster outlived the old ten minutes, SCHED-24);
    a job a restart killed is failed at boot, so it is never joined."""
    try:
        conn = _async_conn()
        row = conn.execute(
            "SELECT job_id FROM async_jobs WHERE kind=? AND restaurant_id=? AND status='pending' "
            "AND created_at >= datetime('now', ?) ORDER BY created_at DESC LIMIT 1",
            (str(kind), restaurant_id, f"-{int(max_age_minutes)} minutes")).fetchone()
        conn.close()
        return row["job_id"] if row else None
    except Exception:
        return None


def claim_async_job(job_id, kind, restaurant_id, max_age_minutes: int = JOB_MAX_MINUTES):
    """(job_id, joined): start `job_id` as this restaurant's one pending job
    of `kind`, or join the one already running — checked and inserted in one
    write transaction. active_job then start_async_job was check-then-insert,
    so two presses at the same instant started two paid generations
    (SCHED-25 / DATA-23)."""
    conn = _async_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT job_id FROM async_jobs WHERE kind=? AND restaurant_id=? AND status='pending' "
            "AND created_at >= datetime('now', ?) ORDER BY created_at DESC LIMIT 1",
            (str(kind), restaurant_id, f"-{int(max_age_minutes)} minutes")).fetchone()
        if row:
            conn.rollback()
            return row["job_id"], True
        conn.execute("INSERT OR REPLACE INTO async_jobs (job_id, kind, restaurant_id, status, result_json)"
                     " VALUES (?,?,?, 'pending', NULL)", (str(job_id), str(kind), restaurant_id))
        conn.execute("DELETE FROM async_jobs WHERE created_at < datetime('now', ?)", (f"-{_ASYNC_JOB_TTL_HOURS} hours",))
        conn.commit()
        return str(job_id), False
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    to another restaurant).

    Reads are NON-destructive. This used to delete the row on the first read,
    "matching how the in-memory version popped its entry" — which meant a
    browser refresh, a dropped response, or a second device polling the same
    job got nothing, after the user had waited thirty seconds for a Claude
    call. Schedules are written to schedule_history before the result lands
    here so the work was usually recoverable, but "usually" was doing real
    work in that sentence.

    Rows are cleaned up by the TTL sweep in start_async_job instead, so a
    finished result stays readable for the rest of its window."""
    import json
    try:
        conn = _async_conn()
        row = conn.execute(
            "SELECT job_id, restaurant_id, status, result_json, "
            "created_at < datetime('now', ?) AS overdue FROM async_jobs WHERE job_id=?",
            (f"-{JOB_MAX_MINUTES} minutes", str(job_id)),
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
        if status == "pending" and row["overdue"]:
            # Past any real generation: its result was never stored (the
            # write failed, or the process died after the boot sweep ran).
            # Polling 'pending' forever helps nobody (DATA-9).
            conn.close()
            return {"status": "error", "result": {"ok": False, "error": "Generation didn't finish — please try again."}}
        if status == "pending":
            conn.close()
            return {"status": "pending", "result": None}
        conn.close()
        try:
            result = json.loads(row["result_json"]) if row["result_json"] else None
        except (TypeError, ValueError):
            return {"status": "error", "result": {"ok": False, "error": "Result could not be read back"}}
        return {"status": status, "result": result}
    except Exception as e:
        log.error(f"read_async_job({job_id}) failed: {e}")
        return None


# Admin actions that call a provider or a model run here, off the request
# thread, at most this many at once (#153): "Sync now" and "Fetch now"
# started one unbounded daemon thread per click, inside a web process with
# four request threads.
ADMIN_TASK_WORKERS = int(os.getenv("ADMIN_TASK_WORKERS", "2"))
_admin_pool = None
_admin_pool_lock = threading.Lock()


def run_admin_task(kind, restaurant_id, name, fn, *args, context="", **kwargs):
    """(job_id, joined): run `fn` for an admin action on the bounded admin
    pool, as an async job the console polls (read_async_job) and a job run
    (run_job, so the Jobs page and the client page see it). A second press
    for the same restaurant while one is pending joins it (claim_async_job)."""
    global _admin_pool
    import concurrent.futures
    job_id, joined = claim_async_job(uuid.uuid4().hex, kind, restaurant_id)
    if joined:
        return job_id, True
    with _admin_pool_lock:
        if _admin_pool is None:
            _admin_pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, ADMIN_TASK_WORKERS),
                                                                thread_name_prefix="admin-task")

    def _go():
        outcome = {}

        def body(*a, **k):
            outcome["result"] = fn(*a, **k)
            return outcome["result"]
        try:
            run_job(name, body, *args, context=context, restaurant_id=restaurant_id, **kwargs)
            res = outcome.get("result")
            state = run_outcome(res)[0] if "result" in outcome else RUN_FAILED
            finish_async_job(job_id, "done" if state != RUN_FAILED else "error",
                             {"ok": state != RUN_FAILED, "state": {1: "ok", 2: "partial", 0: "failed"}[state],
                              "result": res})
        except Exception as e:
            finish_async_job(job_id, "error", {"ok": False, "error": str(e)[:300]})
    _admin_pool.submit(_go)
    return job_id, False


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


def run_job(name, fn, *args, context="", db_path=None, claim=None, restaurant_id=None, request_id=None, **kwargs):
    """Run a scheduled job with failure capture. Returns the job's result,
    or None if it raised. Every run — not only the failures — lands in
    job_runs (start, end, duration, ok), which is what the admin console's
    Jobs page reads; before this the only record of a job that worked was
    the scheduler's heartbeat.

    A job that returns its sweep counts ({attempted, ok, failed, skipped,
    hit_bound}) is recorded by them (run_outcome): ok=2 (partial) when some
    restaurants failed or the bound cut the pass short, ok=0 when every one
    it attempted failed — never a green run over a night nothing synced
    (DH2-1). A run that returned normally but captured failures under its
    own name meanwhile is partial, not clean (#40). `claim` is the
    claim_period job key the run was claimed under, stored with it so a dead
    run is reclaimable by that key (DH2-16). While it runs, its row's
    pulse_at is stamped every RUN_PULSE_SECONDS, so another process can
    tell it from a run a deploy killed (#150)."""
    import time as _time
    started = _time.time()
    run_id = _record_run_start(name, context, db_path=db_path, claim=claim, restaurant_id=restaurant_id,
                               request_id=request_id)
    names = [name] + ([claim] if claim and claim != name else [])
    with _running_lock:
        for n in names:
            _running_jobs[n] = _running_jobs.get(n, 0) + 1
    stop = threading.Event()
    if run_id is not None:
        threading.Thread(target=_pulse_run, args=(run_id, db_path, stop), daemon=True,
                         name=f"run-pulse-{name}").start()
    try:
        result = fn(*args, **kwargs)
        state, blob = run_outcome(result)
        err = None
        if state != RUN_OK and isinstance(result, dict):
            counts = standard_counts(result) or {}
            err = (f"{counts.get('failed') or 0} of {counts.get('attempted') or 0} failed"
                   + (" · stopped at its time bound" if counts.get("hit_bound") else ""))
        if state == RUN_OK:
            captured = _failures_during(names, run_id, db_path=db_path)
            if captured:
                state = RUN_PARTIAL
                err = f"{captured} failure{'s' if captured != 1 else ''} captured during the run"
        _record_run_end(run_id, started, state, err, db_path=db_path, result_json=blob)
        return result
    except Exception as e:
        log.error(f"Job '{name}' crashed: {e}")
        capture(e, job=name, db_path=db_path, restaurant_id=restaurant_id)
        _record_run_end(run_id, started, RUN_FAILED, e, db_path=db_path)
        return None
    finally:
        stop.set()
        with _running_lock:
            for n in names:
                _running_jobs[n] -= 1
                if not _running_jobs[n]:
                    del _running_jobs[n]


def is_running(name) -> bool:
    """Whether run_job is executing `name` (a job name or the claim it ran
    under) in this process right now."""
    with _running_lock:
        return bool(_running_jobs.get(name))


def running_elsewhere(name, db_path=None):
    """The unfinished job_runs row of `name` (or its claim) that is still
    alive — its pulse fresh, or, for a row from before pulses, started
    within the job's own bound — in any process; None when there is none.
    What "the loop never starts a job that is already running" and "Run now
    refuses a running job" both ask (#64, #153)."""
    bound = jobs_registry.max_minutes(name)
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            try:
                row = conn.execute(
                    "SELECT id, job, started_at, owner, pulse_at FROM job_runs "
                    "WHERE (job=? OR claim_job=?) AND finished_at IS NULL "
                    f"AND ({_run_alive_sql()} OR (pulse_at IS NULL AND started_at >= datetime('now', ?))) "
                    "ORDER BY id DESC LIMIT 1", (name, name, f"-{int(bound)} minutes")).fetchone()
            except sqlite3.OperationalError:
                row = conn.execute(
                    "SELECT id, job, started_at FROM job_runs WHERE job=? AND finished_at IS NULL "
                    "AND started_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
                    (name, f"-{int(bound)} minutes")).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception:
        return None


def close_orphaned_runs(db_path=None) -> int:
    """Close every run whose process is gone: unfinished, not this
    process's, and silent for RUN_DEAD_MINUTES (or, from before pulses,
    started that long ago). A run a deploy killed stayed open for good —
    "Stuck" on the Jobs page for 45 days, the digest repeating it for a
    week, Run now blocked for four hours (#150). Called at boot
    (scheduler.start_scheduler, worker.main) and when this process takes
    the scheduler lease. Returns how many it closed."""
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            n = _close_runs(
                conn,
                "(owner IS NULL OR owner != ?) AND COALESCE(pulse_at, started_at) < datetime('now', ?)",
                (_LEASE_OWNER, f"-{int(RUN_DEAD_MINUTES)} minutes"),
                "interrupted — the process running it ended (a deploy or a crash)")
            conn.commit()
        finally:
            conn.close()
        if n:
            log.warning(f"closed {n} run(s) a previous process left open")
        return n
    except Exception as e:
        log.error(f"close_orphaned_runs failed: {e}")
        return 0


# ── run-now requests (#153, #64) ────────────────────────────────────────────

def request_job_run(job, requested_by):
    """Queue a console "Run now" for the scheduler process: (request_id,
    None), or (None, why) when it cannot be queued. A second press while one
    is pending joins it."""
    try:
        from models import get_conn
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id FROM job_run_requests WHERE job=? AND status IN ('pending','running') "
                               "AND requested_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
                               (job, f"-{RUN_REQUEST_TTL_MINUTES + jobs_registry.max_minutes(job)} minutes")
                               ).fetchone()
            if row:
                conn.rollback()
                return row[0], None
            cur = conn.execute("INSERT INTO job_run_requests (job, requested_by) VALUES (?, ?)",
                               (str(job)[:100], str(requested_by or "admin")[:100]))
            conn.commit()
            return cur.lastrowid, None
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as e:
        log.error(f"request_job_run({job}) failed: {e}")
        return None, f"The request could not be recorded: {e}"


def take_job_requests(limit=3):
    """The pending run-now requests this process now owns, oldest first —
    each taken with a compare-and-set, so two processes never both run one.
    Requests older than RUN_REQUEST_TTL_MINUTES are expired, not run."""
    out = []
    try:
        from models import get_conn
        conn = get_conn()
        try:
            conn.execute("UPDATE job_run_requests SET status='expired', finished_at=datetime('now'), "
                         "error='not started within the request window' "
                         "WHERE status='pending' AND requested_at < datetime('now', ?)",
                         (f"-{RUN_REQUEST_TTL_MINUTES} minutes",))
            rows = conn.execute("SELECT id, job, requested_by, requested_at FROM job_run_requests "
                                "WHERE status='pending' ORDER BY id LIMIT ?", (int(limit),)).fetchall()
            for r in rows:
                cur = conn.execute("UPDATE job_run_requests SET status='running', taken_at=datetime('now'), "
                                   "taken_by=? WHERE id=? AND status='pending'", (_LEASE_OWNER, r["id"]))
                if cur.rowcount == 1:
                    out.append(dict(r))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.error(f"take_job_requests failed: {e}")
    return out


def finish_job_request(request_id, ok, error=None):
    try:
        from models import get_conn
        conn = get_conn()
        try:
            conn.execute("UPDATE job_run_requests SET status=?, ok=?, error=?, finished_at=datetime('now') "
                         "WHERE id=?", ("done" if ok else "failed", 1 if ok else 0,
                                        (str(error)[:300] if error else None), int(request_id)))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.error(f"finish_job_request({request_id}) failed: {e}")


def job_requests(limit=20):
    """Recent run-now requests, newest first, for the console."""
    try:
        from models import get_conn
        conn = get_conn()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT id, job, requested_by, requested_at, status, taken_at, finished_at, ok, error "
                "FROM job_run_requests ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]
        finally:
            conn.close()
    except Exception:
        return []


# ── the jobs that should have run (DH2-2) ────────────────────────────────────
#
# Nothing compared "jobs that should have run by now" against job_runs: a
# loop that died after stamping its heartbeat, or a job whose gate never
# opened, left no failure row and no stuck row. This table is the SLA — job
# name → the most hours that may pass between SUCCESSFUL (ok 1 or partial 2)
# runs — and jobs_overdue() is read from a REQUEST thread (/health, the admin
# overview), never from the scheduler it is watching. It is built from the
# job registry, so every job with an SLA there is watched here: 14 of the
# loop's 55 jobs were (#31).
EXPECTED_JOBS = jobs_registry.expected_hours()
# Heartbeat older than this is a dead or wedged loop. The one threshold the
# status page, /health and the console share (#4).
HEARTBEAT_ALERT_MINUTES = jobs_registry.HEARTBEAT_STALE_MINUTES
# One out-of-band alert per this many minutes, however many requests see it.
PLATFORM_ALERT_COOLDOWN_MINUTES = 60


def jobs_overdue(now=None, db_path=None) -> list:
    """[{job, max_hours, last_ok_at, hours_since}] for every EXPECTED_JOBS
    entry whose last successful run is older than its SLA. A job that has
    never succeeded counts only once job_runs is older than its SLA (a fresh
    database is not "overdue") — and a job with no run at all under its name
    only once its SLA has passed since this database first expected it
    (job_expected_since): a job a release adds is not weeks overdue on the
    first /health after the deploy. `now` is a UTC "YYYY-MM-DD HH:MM:SS"
    (tests); None is SQLite's now. Never raises: [] when unreadable.

    One indexed lookup per job (the newest successful run, found through
    idx_job_runs_job), not a GROUP BY over every run ever kept: this is read
    on every /health request."""
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
    except Exception:
        return []
    out = []
    try:
        first = conn.execute("SELECT MIN(started_at) FROM job_runs").fetchone()[0]
        if not first:
            return []
        try:
            since = {r[0]: r[1] for r in conn.execute("SELECT job, since FROM job_expected_since")}
        except sqlite3.OperationalError:
            since = {}
        for job, hours in EXPECTED_JOBS.items():
            row = conn.execute("SELECT finished_at FROM job_runs WHERE job=? AND ok IN (1, 2) "
                               "AND finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 1", (job,)).fetchone()
            last = row[0] if row else None
            base = last or first
            if last is None and since.get(job) and str(since[job]) > str(first) and conn.execute(
                    "SELECT 1 FROM job_runs WHERE job=? LIMIT 1", (job,)).fetchone() is None:
                # Never run under this name: late from when it was first
                # expected, not from the oldest run of some other job. A job
                # with failed runs keeps the old base — it did run, and fails.
                base = since[job]
            late = conn.execute("SELECT (julianday(COALESCE(?, 'now')) - julianday(?)) * 24.0",
                                (now, base)).fetchone()[0]
            if late is not None and late > hours:
                out.append({"job": job, "max_hours": hours, "last_ok_at": last,
                            "hours_since": round(float(late), 1)})
    except Exception as e:
        log.error(f"jobs_overdue unreadable: {e}")
        return []
    finally:
        conn.close()
    return out


def write_probe(db_path=None, timeout=2.0):
    """(ok, error): can the database take a write right now? BEGIN
    IMMEDIATE takes the write lock without writing anything, then ROLLBACK.
    A full, locked or read-only database answered SELECT 1 perfectly well
    and passed /health (#105).

    One implementation: status_manager.db_write_probe, which /health and the
    system card use too. Its "busy" (the lock held past `timeout`) is not ok
    here — the SLA check reports a database that would not take a write
    when it looked."""
    try:
        import status_manager
        st = status_manager.db_write_probe(db_path=db_path, timeout_ms=int(float(timeout) * 1000))
    except Exception as e:
        return False, str(e)[:200]
    if st.get("state") == "ok":
        return True, None
    return False, (st.get("error") or st.get("state") or "write probe failed")[:200]


def _dsr_missing(db_path=None):
    """Restaurants whose last business night has no final or provisional
    Daily Sales Report an hour past its deadline (#17). [] when unreadable."""
    try:
        from dsr import pipeline
        return pipeline.nights_missing(db_path=db_path)
    except Exception as e:
        log.warning(f"DSR check unavailable: {e}")
        return []


def check_platform_sla(send=True, db_path=None, write_ok=None) -> dict:
    """The scheduler's watchdog, run from a REQUEST thread: {heartbeat_minutes,
    loop_minutes, running_job, jobs_overdue, disk, write_ok, backup,
    dsr_missing, problems, alerted}.

    Pages Will (page_operator: SMS, email and push — the cooldown claimed
    only after one of them went out) when:
      * the heartbeat is older than HEARTBEAT_ALERT_MINUTES (dead loop), or
        the loop has not COMPLETED a tick in that long with no job running
        to explain it, or a job has run past its own bound (#121);
      * an expected job is overdue (#31);
      * the volume is low or critical, or the database refuses a write —
        paged over channels that need no database write (#28, #105);
      * the newest backup is older than 26 hours, or has no off-site copy
        (#2);
      * a DSR night is missing past its deadline (#17).

    Only where the scheduler is meant to run (scheduler.scheduling_allowed):
    a laptop has no scheduler and must not page anyone. Owners are never
    contacted from here. Never raises. `write_ok` is the caller's own write
    probe (/health may already have run one)."""
    out = {"heartbeat_minutes": None, "loop_minutes": None, "running_job": None, "jobs_overdue": [],
           "disk": None, "write_ok": None, "backup": None, "dsr_missing": [], "problems": [], "alerted": False}
    try:
        import status_manager
        state = status_manager.scheduler_state(db_path)
        out["heartbeat_minutes"] = state.get("beat_age_minutes")
        out["loop_minutes"] = state.get("loop_completed_age_minutes")
        out["running_job"] = state.get("running_job")
    except Exception:
        state = {}
        try:
            from status_manager import scheduler_heartbeat_age_minutes
            out["heartbeat_minutes"] = scheduler_heartbeat_age_minutes()
        except Exception:
            pass
    out["jobs_overdue"] = jobs_overdue(db_path=db_path)
    try:
        import status_manager as _sm
        out["disk"] = _sm.disk_state(db_path)
    except Exception:
        out["disk"] = None
    if write_ok is None:
        write_ok, write_err = write_probe(db_path)
    else:
        write_err = None if write_ok else "write probe failed"
    out["write_ok"] = bool(write_ok)
    out["backup"] = backup_status(db_path=db_path)
    out["dsr_missing"] = _dsr_missing(db_path=db_path)

    problems = []
    hb = out["heartbeat_minutes"]
    if hb is not None and hb > HEARTBEAT_ALERT_MINUTES:
        problems.append(f"Scheduler heartbeat is {int(hb)} minutes old — nothing scheduled is running.")
    if state.get("wedged"):
        problems.append(f"{state.get('running_job')} has run {int(state.get('running_minutes') or 0)} minutes, "
                        f"past its {state.get('running_bound_minutes')}-minute bound — the loop is stuck in it.")
    elif state.get("loop_stalled"):
        problems.append(f"The scheduler has not completed a tick in {int(out['loop_minutes'] or 0)} minutes "
                        "and no job is running — a tick is failing part-way.")
    for j in out["jobs_overdue"]:
        problems.append(f"{j['job']}: no successful run in {j['hours_since']:g}h (expected within {j['max_hours']}h)")
    disk = out["disk"] or {}
    if disk.get("state") in ("low", "critical"):
        problems.append(f"Volume {'almost full' if disk['state'] == 'critical' else 'filling up'} — "
                        f"{disk.get('free_mb', '?')} MB free ({disk.get('pct_free', '?')}%).")
    if not out["write_ok"]:
        problems.append(f"The database refuses writes: {write_err or 'write probe failed'}.")
    b = out["backup"] or {}
    if b.get("state") in ("stale", "failed", "no_offsite"):
        problems.append(b.get("summary") or "The nightly backup is not healthy.")
    if out["dsr_missing"]:
        names = ", ".join(str(m.get("restaurant") or m.get("restaurant_id")) for m in out["dsr_missing"][:4])
        problems.append(f"Daily Sales Report missing past its deadline for {len(out['dsr_missing'])} "
                        f"restaurant{'s' if len(out['dsr_missing']) != 1 else ''}: {names}.")
    out["problems"] = problems
    if not problems or not send:
        return out
    try:
        import scheduler as _sched
        if not _sched.scheduling_allowed():
            return out
    except Exception:
        return out
    res = page_operator("platform_sla_alert", "Cavnar AI: the platform needs you", problems)
    out["alerted"] = bool(res.get("sent"))
    return out


# ── paging the operator (#27, decision 8) ────────────────────────────────────

# When the claims table cannot be written (the case the page is about),
# the cooldown lives here instead — one page per key per cooldown per process.
_page_memory = {}
_page_lock = threading.Lock()


def will_phone():
    """The operator's mobile for platform pages (WILL_PHONE), or ""."""
    return (os.getenv("WILL_PHONE") or "").strip()


def _cooldown_active(key, minutes):
    """True while `key` was paged successfully within `minutes`. Read-only;
    falls back to process memory when the database cannot be read."""
    mem = _page_memory.get(key)
    if mem is not None and time.monotonic() - mem < float(minutes) * 60:
        return True
    try:
        from models import get_conn
        conn = get_conn()
        try:
            row = conn.execute("SELECT 1 FROM job_period_claims WHERE job_key=? AND claimed_at > datetime('now', ?)",
                               (f"cooldown:{key}", f"-{float(minutes) * 60:.0f} seconds")).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return False


def _stamp_cooldown(key):
    _page_memory[key] = time.monotonic()
    try:
        from models import get_conn
        conn = get_conn()
        try:
            conn.execute("INSERT INTO job_period_claims (job_key, claimed_at) VALUES (?, datetime('now')) "
                         "ON CONFLICT(job_key) DO UPDATE SET claimed_at=datetime('now')", (f"cooldown:{key}",))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.error(f"page cooldown for {key} not written ({e}); held in process memory")


def _record_operator_alert(key, subject, res):
    import json as _json
    try:
        from models import get_conn
        conn = get_conn()
        try:
            conn.execute("INSERT INTO operator_alerts (key, subject, sent, channels, error) VALUES (?,?,?,?,?)",
                         (str(key)[:100], str(subject)[:200], 1 if res.get("sent") else 0,
                          _json.dumps(res.get("channels") or {})[:500], (res.get("error") or None)))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.error(f"operator alert not recorded ({key}): {e}")


def page_operator(key, subject, lines, cooldown_minutes=PLATFORM_ALERT_COOLDOWN_MINUTES) -> dict:
    """Page Will about `key` at most once per `cooldown_minutes`, over every
    channel alert_will has. The cooldown is claimed only AFTER a channel
    confirmed delivery (#27): it used to be spent first, so a page whose
    email and push both failed silenced the next hour of them. Returns
    alert_will's {sent, channels, error}, or {sent: False, reason:
    'cooldown'}."""
    with _page_lock:
        if _cooldown_active(key, cooldown_minutes):
            return {"sent": False, "reason": "cooldown", "channels": {}}
        got = alert_will(subject, lines)
        res = {"sent": bool(got), "channels": dict(getattr(got, "channels", None) or {}),
               "error": getattr(got, "error", None)}
        if res["sent"]:
            _stamp_cooldown(key)
        _record_operator_alert(key, subject, res)
        return res


class AlertResult:
    """What an operator page did, channel by channel. Truthy when a text or
    the email was accepted — like emails.SendResult, so `if alert_will(...)`
    keeps working."""
    __slots__ = ("sent", "channels", "error")

    def __init__(self, sent, channels=None, error=None):
        self.sent, self.channels, self.error = bool(sent), dict(channels or {}), error

    def __bool__(self):
        return self.sent

    def __repr__(self):
        return f"<AlertResult sent={self.sent} channels={self.channels} err={self.error!r}>"


def alert_will(subject, lines):
    """Tell Will now, outside the scheduler: a text to WILL_PHONE, an email
    to config.will_email() and a push to the admin logins' phones. Never an
    owner.

    The text is the independent channel (decision 8): the email rides the
    product's own Resend account and the push its own APNs, so an outage of
    either used to take the page down with it. A push is only queued here —
    it proves nothing — so only the text and the email count as delivered.

    Returns an AlertResult: truthy when a text or the email went out, with
    each channel's outcome in .channels."""
    import html as _html
    channels, errors = {}, []
    lines = [str(x) for x in (lines or [])]
    phone = will_phone()
    if phone:
        try:
            import notify as _notify
            body = (subject + ": " + " ".join(lines[:3]))[:320]
            channels["sms"] = bool(_notify.send_sms(phone, body, use_case="alert"))
            if not channels["sms"]:
                errors.append("sms not accepted")
        except Exception as e:
            channels["sms"] = False
            errors.append(f"sms: {e}")
            log.error(f"alert_will sms failed: {e}")
    try:
        import emails as _emails
        b = _emails.BRAND
        body = "".join(f'<li style="margin:0 0 6px">{_html.escape(x)}</li>' for x in lines[:12])
        res = _emails._send_branded(
            config.will_email(), subject, from_label="Cavnar AI Ops", email_type="ops_platform_alert",
            inner_html=(f'<p style="color:{b["strong"]};font-size:16px;font-weight:700;margin:0 0 12px">'
                        f'{_html.escape(subject)}</p><ul style="color:{b["body"]};font-size:14px;'
                        f'line-height:1.5;margin:0 0 0 18px">{body}</ul>'))
        channels["email"] = bool(getattr(res, "ok", False))
        if not channels["email"]:
            errors.append(f"email: {getattr(res, 'error', None) or 'not accepted'}")
    except Exception as e:
        channels["email"] = False
        errors.append(f"email: {e}")
        log.error(f"alert_will email failed: {e}")
    try:
        from models import get_conn
        import push as _push
        conn = get_conn()
        try:
            admins = conn.execute("SELECT id, restaurant_id FROM users WHERE is_admin=1 AND is_active=1").fetchall()
        finally:
            conn.close()
        by_rid = {}
        for a in admins:
            by_rid.setdefault(a["restaurant_id"], set()).add(a["id"])
        queued = 0
        for rid, ids in by_rid.items():
            queued += int(_push.fire_push(rid, "platform_alert", subject, lines[0][:180] if lines else subject,
                                          data={"tab": "home"}, user_ids=ids) or 0)
        channels["push"] = queued > 0
    except Exception as e:
        channels["push"] = False
        log.error(f"alert_will push failed: {e}")
    return AlertResult(bool(channels.get("sms") or channels.get("email")), channels,
                       "; ".join(errors)[:300] or None)


def last_operator_alert():
    """The newest operator page and whether it went out, for Engineering."""
    try:
        from models import get_conn
        conn = get_conn()
        try:
            row = conn.execute("SELECT key, subject, sent, channels, error, created_at FROM operator_alerts "
                               "ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception:
        return None


def ping_healthcheck(status="ok"):
    """Tell the external dead-man monitor (HEALTHCHECK_PING_URL:
    healthchecks.io, Better Stack, Cronitor) that the scheduler is alive —
    at the end of every tick and after the backup and the digest — so its
    silence pages Will from outside this process, which is the one thing
    nothing inside it can do (#3, #33). status="fail" pings `<url>/fail`,
    which marks the check down at once. Optional: a no-op when unset. Short
    timeouts, never raises; False when the ping did not land."""
    url = (os.getenv("HEALTHCHECK_PING_URL") or "").strip()
    if not url:
        return False
    if status == "fail":
        url = url.rstrip("/") + "/fail"
    try:
        import requests as _requests
        r = _requests.get(url, timeout=(3, 5))
        return 200 <= r.status_code < 300
    except Exception as e:
        log.warning(f"healthcheck ping failed: {e}")
        return False


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


def failures_since(since):
    """[{job, cnt, last_at, sample_error}] since `since` (UTC stamp), the
    sample being each job's NEWEST error — MAX(error) was the alphabetically
    last one."""
    try:
        from models import get_conn
        conn = get_conn()
        try:
            rows = conn.execute("""
                SELECT f.job, COUNT(*) AS cnt, MAX(f.created_at) AS last_at,
                       (SELECT g.error FROM job_failures g WHERE g.job=f.job AND g.created_at >= ?
                        ORDER BY g.id DESC LIMIT 1) AS sample_error
                FROM job_failures f WHERE f.created_at >= ? GROUP BY f.job ORDER BY cnt DESC
            """, (since, since)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


_LEASE_SQL = """CREATE TABLE IF NOT EXISTS scheduler_lease (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    owner        TEXT,
    heartbeat_at TEXT
)"""
# `kept`: the holder runs a lease keeper that renews every
# LEASE_RENEW_SECONDS, so its silence for LEASE_OWNER_GONE_SECONDS means its
# process is gone — the next one takes over in minutes instead of idling
# out the 30-minute window (#134).
_LEASE_COLUMNS = (("kept", "INTEGER NOT NULL DEFAULT 0"),)

# How stale a heartbeat has to be before another process may take over. Must
# comfortably exceed the scheduler tick, or a slow pass loses its own lease
# mid-run and two processes end up holding it.
SCHEDULER_LEASE_STALE_SECONDS = int(os.getenv("SCHEDULER_LEASE_STALE_SECONDS", "1800"))
LEASE_RENEW_SECONDS = int(os.getenv("LEASE_RENEW_SECONDS", "60"))
LEASE_OWNER_GONE_SECONDS = int(os.getenv("LEASE_OWNER_GONE_SECONDS", "240"))

_LEASE_OWNER = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

# Set once this process begins to exit: no path may take (or renew) the
# lease after its release, so the replacement is not locked out again by a
# daemon thread still running here (#161).
_shutting_down = threading.Event()
# Set while a lease keeper renews for this process (scheduler._LeaseKeeper).
_lease_kept = threading.Event()


def acquire_scheduler_lease(owner: str = None, stale_seconds: int = None) -> bool:
    """True if this process may run the scheduler right now.

    Everything in scheduler_loop is protected by claim_period(), so two
    schedulers mostly collide harmlessly — but "mostly" was doing real work
    there. claim_period deliberately fails OPEN, so a database hiccup drops
    the guard for every job at once. The only reason none of that has
    bitten is `--workers 1` in railway.json: one character of deploy config
    standing between the current behaviour and every scheduled job running
    twice.

    So the guarantee moves into the database, where it can be reasoned about.
    The holder refreshes its heartbeat on every tick (and a lease keeper
    every LEASE_RENEW_SECONDS); if it dies, its lease goes stale and another
    process takes over rather than the scheduler simply stopping — after
    LEASE_OWNER_GONE_SECONDS when the holder ran a keeper, else after
    SCHEDULER_LEASE_STALE_SECONDS. Fails OPEN for the same reason
    claim_period does — a bookkeeping outage must not silently stop every
    scheduled job. Never once this process is shutting down.
    """
    if _shutting_down.is_set():
        return False
    owner = owner or _LEASE_OWNER
    stale = SCHEDULER_LEASE_STALE_SECONDS if stale_seconds is None else stale_seconds
    kept = 1 if (_lease_kept.is_set() and owner == _LEASE_OWNER) else 0
    try:
        from models import get_conn
        conn = get_conn()
        conn.execute(_LEASE_SQL)
        conn.execute("INSERT OR IGNORE INTO scheduler_lease (id, owner, heartbeat_at) VALUES (1, NULL, NULL)")
        conn.commit()
        try:
            cur = conn.execute(
                "UPDATE scheduler_lease SET owner=?, heartbeat_at=datetime('now'), kept=? "
                "WHERE id=1 AND (owner IS NULL OR owner=? OR heartbeat_at IS NULL "
                "               OR heartbeat_at < datetime('now', ?) "
                "               OR (kept=1 AND heartbeat_at < datetime('now', ?)))",
                (owner, kept, owner, f"-{int(stale)} seconds",
                 f"-{int(min(stale, LEASE_OWNER_GONE_SECONDS))} seconds"),
            )
        except sqlite3.OperationalError:
            # A database booted before `kept` existed (init_ops adds it).
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


def renew_scheduler_lease(owner: str = None) -> bool:
    """Refresh the heartbeat of a lease this process already holds — the
    lease keeper's call. Never takes a lease it does not hold, and never
    once shutting down."""
    if _shutting_down.is_set():
        return False
    owner = owner or _LEASE_OWNER
    try:
        from models import get_conn
        conn = get_conn()
        try:
            try:
                cur = conn.execute("UPDATE scheduler_lease SET heartbeat_at=datetime('now'), kept=? "
                                   "WHERE id=1 AND owner=?", (1 if _lease_kept.is_set() else 0, owner))
            except sqlite3.OperationalError:
                cur = conn.execute("UPDATE scheduler_lease SET heartbeat_at=datetime('now') WHERE id=1 AND owner=?",
                                   (owner,))
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"renew_scheduler_lease failed: {e}")
        return False


def release_scheduler_lease(owner: str = None) -> bool:
    """Give the lease up on a clean exit, so the next process takes over on
    its next tick instead of waiting out SCHEDULER_LEASE_STALE_SECONDS.

    Nothing did this, so every redeploy left the new process idling for up
    to 30 minutes behind a dead holder's lease: no fetches, alerts or briefs
    in that window, repeated on every push. Only releases a lease this
    process actually holds. Process exit goes through shutdown_scheduler,
    which also stops this process taking it back.
    """
    owner = owner or _LEASE_OWNER
    try:
        from models import get_conn
        conn = get_conn()
        cur = conn.execute("UPDATE scheduler_lease SET owner=NULL, heartbeat_at=NULL "
                           "WHERE id=1 AND owner=?", (owner,))
        conn.commit()
        conn.close()
        return cur.rowcount == 1
    except Exception as e:
        log.error(f"release_scheduler_lease failed: {e}")
        return False


def shutdown_scheduler() -> bool:
    """The exit path (atexit in the web process, SIGTERM in worker.py): mark
    this process as shutting down FIRST — so the loop, the pulse and the
    lease keeper, daemon threads still running, can no longer re-acquire or
    renew — then release the lease (#161)."""
    _shutting_down.set()
    _lease_kept.clear()
    return release_scheduler_lease()


def scheduler_lease_holder():
    """Who currently owns the lease, for the admin console and for tests."""
    try:
        from models import get_conn
        conn = get_conn()
        conn.execute(_LEASE_SQL)
        conn.commit()
        row = conn.execute("SELECT * FROM scheduler_lease WHERE id=1").fetchone()
        conn.close()
        if not row:
            return None
        out = {"owner": row["owner"], "heartbeat_at": row["heartbeat_at"]}
        if "kept" in row.keys():
            out["kept"] = row["kept"]
        return out
    except Exception:
        return None


# ── retention (#72, #81) ─────────────────────────────────────────────────────
#
# Ledgers that only ever grew. Audit #7 found seven of them: ai_usage (read
# by the budget check on every AI call), job_runs, job_failures, alert_log,
# push_deliveries, webhook_deliveries and email_log. Nothing pruned any of
# them, on SQLite, on a volume whose only protection against filling up is
# the status check added in audit #6.
#
# Retention is per-table because they are not equally useful old: the budget
# only ever asks about this month, but a year of email history is worth
# keeping for a billing dispute.
#
# THE ONE REGISTRY (#72). models._LOG_RETENTION_DAYS was a second one that
# ran hourly and disagreed with this one; both ran, so the shorter window
# always won. It is merged here with each conflict resolved to the value
# production has actually been running — the shorter: job_runs 45 days
# (models said 90; nothing reads further back than the 8-day weekly SLA),
# push_deliveries 30 days (models said 90; the notifications page reads 30).
# ai_validation_log (120) and activity_log (180) came over unchanged.
_RETENTION_DAYS = {
    "ai_usage":            int(os.getenv("RETAIN_AI_USAGE_DAYS", "120")),
    "ai_validation_log":   int(os.getenv("RETAIN_AI_VALIDATION_DAYS", "120")),
    "activity_log":        int(os.getenv("RETAIN_ACTIVITY_LOG_DAYS", "180")),
    "job_runs":            int(os.getenv("RETAIN_JOB_RUNS_DAYS", "45")),
    "job_failures":        int(os.getenv("RETAIN_JOB_FAILURES_DAYS", "90")),
    "push_deliveries":     int(os.getenv("RETAIN_PUSH_DELIVERIES_DAYS", "30")),
    "webhook_deliveries":  int(os.getenv("RETAIN_WEBHOOK_DELIVERIES_DAYS", "60")),
    "alert_log":           int(os.getenv("RETAIN_ALERT_LOG_DAYS", "180")),
    "email_log":           int(os.getenv("RETAIN_EMAIL_LOG_DAYS", "365")),
    # Added by audits #11 and #12 and initially registered nowhere, which is
    # exactly the oversight audit #7 built this registry to make impossible.
    # Eight rows per visibility run, weekly per full-tier client plus every
    # manual check; one row per competitor per weekly analysis. Both are
    # kept long enough to show a real trend and no longer.
    "ai_visibility_query_runs": int(os.getenv("RETAIN_AIVIS_QUERIES_DAYS", "365")),
    "competitor_snapshots":     int(os.getenv("RETAIN_COMPETITOR_SNAPSHOTS_DAYS", "365")),
    "ai_visibility_runs":       int(os.getenv("RETAIN_AIVIS_RUNS_DAYS", "730")),
    # One row each time a schedule recommendation is shown, accepted or
    # dismissed; a year is plenty to know which kinds an owner ignores
    # (SCHED-27).
    "schedule_recommendation_events": int(os.getenv("RETAIN_SCHED_RECS_DAYS", "365")),
    # claim_period pruned this itself, on every call, with a full scan
    # (DATA-6). A claim older than any period that is still asked about.
    # Once-ever markers live in ops_markers, which is never pruned (#157).
    "job_period_claims": int(os.getenv("RETAIN_JOB_CLAIMS_DAYS", "45")),
    # A held alert keeps its whole email body (guest review excerpts
    # included) and is sent or dropped within a day; notification_opens
    # feeds a 30-day engagement read. Neither was ever pruned (MOD-NOT-14).
    "alert_holds":        int(os.getenv("RETAIN_ALERT_HOLDS_DAYS", "30")),
    # Tap de-duplication only needs the last half hour (marketing_links).
    "marketing_link_taps": int(os.getenv("RETAIN_LINK_TAPS_DAYS", "2")),
    "notification_opens": int(os.getenv("RETAIN_NOTIFICATION_OPENS_DAYS", "365")),
    # Registered by #72. The admin audit trail is long: a year and a month,
    # so last year's same month is still there to compare.
    "admin_events":       int(os.getenv("RETAIN_ADMIN_EVENTS_DAYS", "400")),
    # One row per restaurant per day; the trend reads months, a year covers
    # the same month last year.
    "data_health_daily":  int(os.getenv("RETAIN_DATA_HEALTH_DAILY_DAYS", "400")),
    # Webhook idempotency: Stripe retries an event for three days.
    "stripe_events_seen": int(os.getenv("RETAIN_STRIPE_EVENTS_SEEN_DAYS", "90")),
    # A session row a day past its own expiry can never authenticate again.
    "sessions":           int(os.getenv("RETAIN_EXPIRED_SESSIONS_DAYS", "1")),
    # The recommendation trail. The longest reader (confidence calibration)
    # looks back 730 days, so this keeps past it; rec_instances — the
    # identities outcomes reference — are never pruned.
    "rec_events":         int(os.getenv("RETAIN_REC_EVENTS_DAYS", "800")),
    # The operator's own ledgers from this round.
    "operator_alerts":    int(os.getenv("RETAIN_OPERATOR_ALERTS_DAYS", "180")),
    "backup_runs":        int(os.getenv("RETAIN_BACKUP_RUNS_DAYS", "400")),
    "job_run_requests":   int(os.getenv("RETAIN_JOB_RUN_REQUESTS_DAYS", "90")),
    "missed_windows":     int(os.getenv("RETAIN_MISSED_WINDOWS_DAYS", "90")),
    # The other workstreams' ledgers, registered by the integration wave.
    # business_metrics_daily is NOT here, on purpose: it is the history MRR
    # and account trends are drawn from, and nothing earlier is rebuilt.
    # The platform telemetry (request_rollups, http_5xx_log, boot_events,
    # provider_health) is pruned hourly by the web process's supervisor
    # (platform_monitor.TELEMETRY_RETENTION_DAYS) — one pruner, not two.
    # The AI-operations tables are ai_utils.prune_ai_ops', run by the
    # rollup below.
    "value_figures_daily": int(os.getenv("RETAIN_VALUE_FIGURES_DAYS", "120")),
    "admin_issue_resolution_history": int(os.getenv("RETAIN_RESOLUTION_HISTORY_DAYS", "400")),
    "sms_log":            int(os.getenv("RETAIN_SMS_LOG_DAYS", "90")),
    "push_outbox":        int(os.getenv("RETAIN_PUSH_OUTBOX_DAYS", "30")),
    "webhook_outbox":     int(os.getenv("RETAIN_WEBHOOK_OUTBOX_DAYS", "30")),
    "morning_brief_deliveries": int(os.getenv("RETAIN_BRIEF_DELIVERIES_DAYS", "90")),
    "alert_storm_caps":   int(os.getenv("RETAIN_ALERT_STORM_CAPS_DAYS", "365")),
    # login_history (90 days, auth.LOGIN_HISTORY_RETENTION_DAYS, pruned at
    # boot today) and view_as_sessions join once auth.init_auth indexes
    # their created_at: every delete here must use an index
    # (tests/test_edge_data_claims_and_lease.py), and both tables are made
    # after this module's boot init runs.
}

# Each table's own timestamp column — they do not agree on a name.
_RETENTION_COLUMN = {
    "ai_usage": "created_at", "ai_validation_log": "created_at", "activity_log": "created_at",
    "job_runs": "started_at", "job_failures": "created_at",
    "push_deliveries": "created_at", "webhook_deliveries": "created_at",
    "alert_log": "fired_at", "email_log": "sent_at",
    "ai_visibility_query_runs": "created_at", "competitor_snapshots": "captured_at",
    "ai_visibility_runs": "created_at", "job_period_claims": "claimed_at",
    "alert_holds": "created_at", "notification_opens": "opened_at",
    "marketing_link_taps": "tapped_at",
    "admin_events": "created_at", "data_health_daily": "created_at", "stripe_events_seen": "seen_at",
    "sessions": "expires_at", "rec_events": "at",
    "operator_alerts": "created_at", "backup_runs": "started_at", "job_run_requests": "requested_at",
    "missed_windows": "created_at",
    "value_figures_daily": "date", "admin_issue_resolution_history": "created_at", "sms_log": "created_at",
    "push_outbox": "created_at", "webhook_outbox": "created_at", "morning_brief_deliveries": "created_at",
    "alert_storm_caps": "started_at",
}
# Every table above has an index on its column, created where the table is
# or at boot here (_ensure_retention_indexes, DATA-40): these deletes run
# under the write lock, and a full scan of a year of email_log there stalls
# every request that writes. tests/test_edge_data_claims_and_lease.py pins it.

# Deleted in chunks of this many rows, one commit each, and the whole pass
# stops taking on tables after RETENTION_MAX_SECONDS (#72, #81): one DELETE
# per table held the write lock for as long as the table was old.
RETENTION_CHUNK_ROWS = int(os.getenv("RETENTION_CHUNK_ROWS", "5000"))
RETENTION_MAX_SECONDS = int(os.getenv("RETENTION_MAX_SECONDS", str(10 * 60)))

# Old schedule states, keeping every published week (#72): intermediate
# saves of a week this old, and drafts a later regeneration replaced that
# were never published or shared.
SCHEDULE_VERSIONS_KEEP_DAYS = int(os.getenv("RETAIN_SCHEDULE_VERSIONS_DAYS", "180"))
SUPERSEDED_DRAFTS_KEEP_DAYS = int(os.getenv("RETAIN_SUPERSEDED_DRAFTS_DAYS", "365"))


def _ensure_retention_indexes(conn):
    """The indexes the retention deletes (and the client page) need on tables
    created elsewhere, where the table exists. Idempotent; at boot."""
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, sql in (
        ("admin_events", "CREATE INDEX IF NOT EXISTS idx_admin_events_created_at ON admin_events(created_at)"),
        ("admin_events", "CREATE INDEX IF NOT EXISTS idx_admin_events_rid ON admin_events(restaurant_id, id)"),
        ("data_health_daily", "CREATE INDEX IF NOT EXISTS idx_data_health_daily_created ON data_health_daily(created_at)"),
        ("stripe_events_seen", "CREATE INDEX IF NOT EXISTS idx_stripe_events_seen_at ON stripe_events_seen(seen_at)"),
        ("rec_events", "CREATE INDEX IF NOT EXISTS idx_rec_events_at_prune ON rec_events(at)"),
        ("ai_validation_log", "CREATE INDEX IF NOT EXISTS idx_ai_validation_log_created ON ai_validation_log(created_at)"),
        ("activity_log", "CREATE INDEX IF NOT EXISTS idx_activity_log_created ON activity_log(created_at)"),
        ("schedule_versions", "CREATE INDEX IF NOT EXISTS idx_schedule_versions_created ON schedule_versions(created_at)"),
        # Messaging ledgers made in models.init_db before this runs (fix
        # round E). sms_log's, the outboxes' and the resolution history's
        # live with their own tables.
        ("morning_brief_deliveries",
         "CREATE INDEX IF NOT EXISTS idx_morning_brief_deliveries_created ON morning_brief_deliveries(created_at)"),
        ("alert_storm_caps", "CREATE INDEX IF NOT EXISTS idx_alert_storm_caps_started ON alert_storm_caps(started_at)"),
    ):
        if table in have:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError as e:
                log.warning(f"retention index on {table} skipped: {e}")


# inventory_history holds a snapshot per restaurant per DAY, each carrying the
# full item list — about 2.5 MB a day for a 5,000-item kitchen, and nothing
# pruned it (MOD-FC-18). Every reader buckets it to one row per ISO week, so
# past INVENTORY_DAILY_DAYS only each week's last snapshot is kept; past
# INVENTORY_HISTORY_DAYS nothing is. 395 days keeps the same four weeks last
# year that food_cost_intelligence.seasonal_baseline compares against (its
# oldest day is 392 days back).
INVENTORY_DAILY_DAYS = int(os.getenv("RETAIN_INVENTORY_DAILY_DAYS", "56"))
INVENTORY_HISTORY_DAYS = int(os.getenv("RETAIN_INVENTORY_HISTORY_DAYS", "395"))


def _prune_inventory_history(conn):
    """Thin inventory_history to weekly past INVENTORY_DAILY_DAYS and drop it
    past INVENTORY_HISTORY_DAYS. Dated by week_end — the day the snapshot
    describes — not saved_at, which a backfill sets to today. Returns rows
    deleted."""
    n = 0
    if INVENTORY_HISTORY_DAYS > 0:
        cur = conn.execute("DELETE FROM inventory_history WHERE week_end < date('now', ?)",
                           (f"-{INVENTORY_HISTORY_DAYS} days",))
        n += max(0, cur.rowcount or 0)
        conn.commit()
    if INVENTORY_DAILY_DAYS > 0:
        # The week's figure is its latest snapshot (waste_trend.load_waste_history).
        cur = conn.execute(
            "DELETE FROM inventory_history WHERE week_end < date('now', ?) AND id NOT IN ("
            "  SELECT (SELECT h2.id FROM inventory_history h2 WHERE h2.restaurant_id = h.restaurant_id"
            "          AND date(h2.week_end, 'weekday 0', '-6 days') = date(h.week_end, 'weekday 0', '-6 days')"
            "          ORDER BY h2.week_end DESC, h2.id DESC LIMIT 1)"
            "  FROM inventory_history h WHERE h.week_end < date('now', ?)"
            "  GROUP BY h.restaurant_id, date(h.week_end, 'weekday 0', '-6 days'))",
            (f"-{INVENTORY_DAILY_DAYS} days", f"-{INVENTORY_DAILY_DAYS} days"))
        n += max(0, cur.rowcount or 0)
    conn.commit()
    return n


def _chunked_delete(conn, table, where, args, deadline):
    """DELETE ... WHERE `where` in RETENTION_CHUNK_ROWS batches, committing
    each, until done or `deadline` (time.monotonic()). Returns rows deleted."""
    total = 0
    while True:
        cur = conn.execute(f"DELETE FROM {table} WHERE rowid IN "
                           f"(SELECT rowid FROM {table} WHERE {where} LIMIT {int(RETENTION_CHUNK_ROWS)})",
                           tuple(args))
        n = max(0, cur.rowcount or 0)
        conn.commit()
        total += n
        if n < RETENTION_CHUNK_ROWS or time.monotonic() > deadline:
            return total


def _prune_schedules(conn, deadline):
    """Old schedule states, never a published week (#72): intermediate saves
    of weeks past SCHEDULE_VERSIONS_KEEP_DAYS (each week keeps its newest
    version and every published one), and drafts a later regeneration
    superseded, never published or shared, past SUPERSEDED_DRAFTS_KEEP_DAYS.
    Returns {table: rows}."""
    out = {}
    if SCHEDULE_VERSIONS_KEEP_DAYS > 0:
        n = _chunked_delete(
            conn, "schedule_versions",
            "created_at < datetime('now', ?) AND reason != 'published' "
            "AND version < (SELECT MAX(v2.version) FROM schedule_versions v2 "
            "               WHERE v2.history_id = schedule_versions.history_id)",
            (f"-{SCHEDULE_VERSIONS_KEEP_DAYS} days",), deadline)
        if n:
            out["schedule_versions"] = n
    if SUPERSEDED_DRAFTS_KEEP_DAYS > 0 and time.monotonic() < deadline:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_history)")}
        if {"superseded_by", "published_at"} <= cols:
            ids = [r[0] for r in conn.execute(
                "SELECT h.id FROM schedule_history h WHERE h.superseded_by IS NOT NULL AND h.published_at IS NULL "
                "AND h.generated_at < datetime('now', ?) "
                "AND NOT EXISTS (SELECT 1 FROM schedule_shares s WHERE s.schedule_id = h.id) "
                "LIMIT ?", (f"-{SUPERSEDED_DRAFTS_KEEP_DAYS} days", int(RETENTION_CHUNK_ROWS))).fetchall()]
            dependents = [t for t in ("schedule_versions", "schedule_experiment_weeks",
                                      "shift_change_requests", "schedule_outcomes")
                          if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                          (t,)).fetchone()]
            deleted = 0
            for hid in ids:
                if time.monotonic() > deadline:
                    break
                # A draft with requests or outcomes was used after all: kept.
                if any(conn.execute(f"SELECT 1 FROM {t} WHERE history_id=? LIMIT 1", (hid,)).fetchone()
                       for t in ("shift_change_requests", "schedule_outcomes") if t in dependents):
                    continue
                for t in ("schedule_versions", "schedule_experiment_weeks"):
                    if t in dependents:
                        conn.execute(f"DELETE FROM {t} WHERE history_id=?", (hid,))
                deleted += max(0, conn.execute("DELETE FROM schedule_history WHERE id=?", (hid,)).rowcount or 0)
                conn.commit()
            if deleted:
                out["schedule_history"] = deleted
    return out


def _optimize(conn):
    """Planner statistics after the nightly prune (#72): no ANALYZE ran
    anywhere, and on a large copy it cut one admin query from 1,364 ms to
    10.5 ms. Bounded (analysis_limit) so it samples rather than reads every
    row; a first run with no statistics at all gets a bounded ANALYZE."""
    try:
        conn.execute("PRAGMA analysis_limit=400")
        has_stats = conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'").fetchone()
        if not has_stats:
            conn.execute("ANALYZE")
        conn.execute("PRAGMA optimize=0x10002")
        conn.commit()
        return True
    except Exception as e:
        log.warning(f"PRAGMA optimize skipped: {e}")
        return False


def prune_ledgers(db_path=None):
    """Delete rows past their retention window. Returns {table: rows_deleted}.

    Deliberately tolerant: a table that does not exist yet, or whose stamp
    column is named something else on an older database, is skipped rather
    than taking the whole sweep down with it. Nightly, straight after the
    backup (so the pruned rows are in it), chunked with a commit per chunk
    and a wall-clock bound, then planner statistics (#72, #81)."""
    from models import get_conn, DB_PATH
    deleted = {}
    counts = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    try:
        conn = get_conn(db_path or DB_PATH)
    except Exception as e:
        log.error(f"prune_ledgers could not open the database: {e}")
        raise
    deadline = time.monotonic() + RETENTION_MAX_SECONDS

    def _missing(err):
        text = str(err).lower()
        return "no such table" in text or "no such column" in text

    # The AI ledger's daily rollup first (fix round G, #70): ai_usage_daily
    # and ai_validation_daily are never pruned, so what the raw rows said
    # outlives them. models.prune_operational_logs ran it, and is no longer
    # scheduled. A rollup that failed keeps tonight's raw AI rows.
    rolled = True
    try:
        import ai_utils
        ai_utils.rollup_usage(db_path)
    except Exception as e:
        rolled = False
        log.error(f"prune_ledgers: the AI usage rollup failed, so the AI ledgers are kept tonight: {e}")
        capture(e, job="prune_ledgers", context="ai_utils.rollup_usage", db_path=db_path)

    try:
        for table, days in _RETENTION_DAYS.items():
            if days <= 0:
                continue            # 0 disables retention for that table
            if time.monotonic() > deadline:
                counts["hit_bound"] = True
                break
            if not rolled and table in ("ai_usage", "ai_validation_log"):
                counts["skipped"] += 1
                continue
            col = _RETENTION_COLUMN.get(table, "created_at")
            counts["attempted"] += 1
            try:
                n = _chunked_delete(conn, table, f"{col} < datetime('now', ?)", (f"-{days} days",), deadline)
                if n:
                    deleted[table] = n
                counts["ok"] += 1
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                if _missing(e):
                    # Missing table or renamed column — not worth failing the sweep.
                    counts["attempted"] -= 1
                    counts["skipped"] += 1
                    log.debug(f"prune_ledgers skipped {table}: {e}")
                else:
                    counts["failed"] += 1
                    log.error(f"prune_ledgers could not prune {table}: {e}")
        try:
            n = _prune_inventory_history(conn)
            if n:
                deleted["inventory_history"] = n
        except Exception as e:
            log.debug(f"prune_ledgers skipped inventory_history: {e}")
        try:
            deleted.update(_prune_schedules(conn, deadline))
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            log.debug(f"prune_ledgers skipped schedules: {e}")
        _optimize(conn)
    finally:
        conn.close()
    if deleted:
        log.info(f"Pruned old rows: {deleted}")
    deleted.update(counts)
    return deleted


def stuck_jobs(older_than_minutes: int = None):
    """Jobs that started and have not finished past their own bound — the
    failure mode nothing reports.

    claim_period() claims BEFORE the work runs, which is exactly what stops a
    redeploy from re-emailing the whole client list. The cost is that a hard
    kill (SIGKILL, an OOM, a container replaced mid-run) leaves the claim
    standing with no code left to release it, so the job does not run again
    until its next slot. For daily_alerts that is a whole day with no alerts
    at all, and because nothing raised, capture() never fired and the digest
    below stayed empty. Silence looked identical to "nothing went wrong".

    A row with started_at and no finished_at past the job's own bound
    (jobs_registry.max_minutes — "stuck" had four thresholds: 30, 90, 120
    and 240 minutes, #150) is the evidence. Runs a deploy killed are closed
    at boot (close_orphaned_runs), so what is left here is a run still
    going past its bound. `older_than_minutes` overrides every job's bound."""
    try:
        from models import get_conn
        conn = get_conn()
        conn.execute(_RUNS_SQL)
        conn.commit()
        rows = conn.execute(
            "SELECT job, started_at, context, "
            "(julianday('now') - julianday(started_at)) * 1440.0 AS minutes FROM job_runs "
            "WHERE finished_at IS NULL AND started_at >= datetime('now', '-7 days') "
            "ORDER BY started_at DESC LIMIT 200").fetchall()
        conn.close()
        out = []
        for r in rows:
            bound = older_than_minutes if older_than_minutes is not None else jobs_registry.max_minutes(r["job"])
            if (r["minutes"] or 0) > bound:
                d = dict(r)
                d["minutes"] = int(d["minutes"] or 0)
                d["bound_minutes"] = int(bound)
                out.append(d)
        return out[:25]
    except Exception as e:
        log.error(f"stuck_jobs failed: {e}")
        return []


# ── the failure digest (#33) ─────────────────────────────────────────────────

_DIGEST_CURSOR = "ops_failure_digest_sent_at"
# However long the digest has been failing, one email covers at most this.
DIGEST_MAX_LOOKBACK_DAYS = 7


def _digest_since():
    """UTC stamp the digest covers from: the last digest that actually went
    out, else 24 hours ago; never more than DIGEST_MAX_LOOKBACK_DAYS. A fixed
    24-hour window meant a day whose digest failed was never mailed."""
    try:
        from models import get_conn
        conn = get_conn()
        try:
            row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (_DIGEST_CURSOR,)).fetchone()
            floor = conn.execute("SELECT datetime('now', ?)", (f"-{DIGEST_MAX_LOOKBACK_DAYS} days",)).fetchone()[0]
            day = conn.execute("SELECT datetime('now', '-1 day')").fetchone()[0]
        finally:
            conn.close()
        since = row["value"] if row and row["value"] else day
        return max(str(since), floor)
    except Exception:
        from datetime import datetime, timedelta
        return (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")


def _mark_digest_sent(stamp):
    try:
        from models import get_conn
        conn = get_conn()
        try:
            conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                         (_DIGEST_CURSOR, stamp))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.error(f"failure digest cursor not written: {e}")


def _now_utc_stamp():
    from datetime import datetime
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


class DigestNotSent(RuntimeError):
    """The failure digest had something to say and did not get it out."""


def send_failure_digest():
    """Daily 8am: one compact email to the operator about what failed, what
    is stuck, what is overdue and what the backup looks like since the last
    digest that was actually sent (#33). Nothing to say → no email (silence
    stays meaningful), and the external monitor is pinged either way.

    Returns the standard counts. RAISES DigestNotSent when there was
    something to say and it did not go out — recorded as a failed run, and
    the loop gives the day's claim back so a later tick retries — instead of
    returning False into an ok=1 run nobody saw."""
    started = _now_utc_stamp()
    since = _digest_since()
    failures = failures_since(since)
    stuck = stuck_jobs()
    overdue = jobs_overdue()
    backup = backup_status()
    missed = missed_windows_since(since)
    try:
        import security as _security
        sec_lines = _security.digest_lines()
    except Exception:
        sec_lines = []
    # "never" is a fresh database; a backup that stopped running is the
    # backup_db SLA's to report (overdue), not this line's.
    backup_bad = backup.get("state") in ("stale", "failed", "no_offsite")
    # A job that died without raising leaves no failure row, so "no failures"
    # was never the same thing as "nothing went wrong".
    # (tests/test_security.py pins the first three conjuncts in this order.)
    if not failures and not stuck and not sec_lines and not overdue and not backup_bad and not missed:
        _mark_digest_sent(started)
        ping_healthcheck()
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 1, "hit_bound": False, "sent": False}
    import html as _html
    import emails as _emails_ops
    b = _emails_ops.BRAND
    will = config.will_email()
    total = sum(f["cnt"] for f in failures)

    def _section(title, items, note=None):
        if not items:
            return ""
        lis = "".join(f'<li style="margin:0 0 6px">{x}</li>' for x in items)
        return (f'<p style="font-size:13px;font-weight:700;color:{b["strong"]};margin:20px 0 6px">{_html.escape(title)}</p>'
                + (f'<p style="font-size:12px;color:{b["muted"]};margin:0 0 8px">{_html.escape(note)}</p>' if note else "")
                + f'<ul style="font-size:13px;color:{b["body"]};line-height:1.5;margin:0 0 0 18px">{lis}</ul>')

    fail_items = [f'<b>{_html.escape(f["job"])}</b> ×{int(f["cnt"])} — '
                  f'{_html.escape((f.get("sample_error") or "")[:160])}' for f in failures]
    stuck_items = [f'<b>{_html.escape(j["job"])}</b> running {int(j.get("minutes") or 0)} min '
                   f'(bound {int(j.get("bound_minutes") or 0)} min) — '
                   f'{_html.escape((j.get("context") or "")[:100])}' for j in stuck]
    overdue_items = [f'<b>{_html.escape(j["job"])}</b> — no successful run in {j["hours_since"]:g}h '
                     f'(expected within {j["max_hours"]}h)' for j in overdue]
    backup_items = [_html.escape(backup.get("summary") or "The nightly backup is not healthy.")] if backup_bad else []
    missed_items = [f'<b>{_html.escape(m["job"])}</b> — {int(m["n"])} restaurant-window'
                    f'{"s" if int(m["n"]) != 1 else ""} closed with nothing sent' for m in missed]
    inner = (
        f'<p style="font-size:16px;font-weight:700;color:{b["strong"]};margin:0 0 4px">Background jobs</p>'
        f'<p style="font-size:12px;color:{b["muted"]};margin:0">Since {_html.escape(since)} UTC</p>'
        + _section("Overdue", overdue_items, "No successful run inside the job's SLA.")
        + _section("Backup", backup_items)
        + _section("Failures", fail_items)
        + _section("Running past their bound", stuck_items,
                   "Still running past the job's own bound; a run a deploy killed is closed at boot.")
        + _section("Windows missed", missed_items, "Due in a restaurant's local window, which closed first.")
        + _section("Security", [_html.escape(x) for x in sec_lines])
        + f'<p style="font-size:12px;color:{b["muted"]};margin-top:18px">Full stack traces are in Sentry '
          f'(if configured) and Railway logs.</p>')
    parts = []
    if overdue:
        parts.append(f"{len(overdue)} overdue")
    if backup_bad:
        parts.append("backup")
    if failures:
        parts.append(f"{total} failure{'s' if total != 1 else ''}")
    if stuck:
        parts.append(f"{len(stuck)} started and never finished")
    if missed:
        parts.append("windows missed")
    if sec_lines and not parts:
        parts.append("security")
    subject = "⚠ Background jobs: " + " · ".join(parts)
    res = _emails_ops.deliver(email_type="ops_failure_digest", payload={
        "from": _emails_ops.sender("ops"), "to": [will], "subject": subject,
        "html": _emails_ops._branded_email(inner)})
    if not getattr(res, "ok", False):
        ping_healthcheck("fail")
        raise DigestNotSent(f"failure digest not sent: {getattr(res, 'error', None) or 'not accepted'}")
    _mark_digest_sent(started)
    ping_healthcheck()
    log.info(f"Failure digest sent to {will} ({total} failures)")
    return {"attempted": 1, "ok": 1, "failed": 0, "skipped": 0, "hit_bound": False, "sent": True,
            "failures": total, "overdue": len(overdue), "stuck": len(stuck)}


# ── the Monday operator digest (#35) ─────────────────────────────────────────

def send_operator_weekly_digest():
    """Monday 7am CT: one email to Will built from the console's own reads
    (admin_ops.overview and clients) — pipeline, churn risk, onboarding,
    failures and costs — instead of separate emails that each end "follow
    up by hand" (#35). Raises when it could not be sent."""
    import html as _html
    import emails as _emails_ops
    import admin_ops
    ov = admin_ops.overview()
    k = ov.get("kpis") or {}
    try:
        recs = admin_ops.clients().get("clients") or []
    except Exception:
        recs = []
    real = [r for r in recs if not r.get("is_demo") and not r.get("is_admin_home")]
    at_risk = sorted([r for r in real if (r.get("churn") or {}).get("level") in ("high", "medium")],
                     key=lambda r: -int((r.get("churn") or {}).get("score") or 0))[:8]
    onboarding = [r for r in real if (r.get("onboarding") or {}).get("complete") is False][:8]
    b = _emails_ops.BRAND

    def _row(label, value):
        return (f'<tr><td style="padding:4px 12px 4px 0;color:{b["muted"]};font-size:13px">{_html.escape(label)}</td>'
                f'<td style="padding:4px 0;color:{b["strong"]};font-size:13px;font-weight:700">{_html.escape(str(value))}</td></tr>')

    def _list(title, items):
        if not items:
            return ""
        return (f'<p style="font-size:13px;font-weight:700;color:{b["strong"]};margin:18px 0 6px">{_html.escape(title)}</p>'
                f'<ul style="font-size:13px;color:{b["body"]};margin:0 0 0 18px">'
                + "".join(f'<li style="margin:0 0 4px">{x}</li>' for x in items) + "</ul>")

    pipeline = "".join(_row(label, value) for label, value in (
        ("Clients", k.get("clients", 0)), ("Active", k.get("active", 0)), ("Trial", k.get("trial", 0)),
        ("New this month", k.get("new_this_month", 0)), ("Past due", k.get("past_due", 0)),
        ("MRR", f"${int(k.get('mrr') or 0):,}"), ("Needs attention", k.get("attention", 0)),
        ("Job failures (24h)", k.get("job_failures_24h", 0)), ("Jobs overdue", k.get("jobs_overdue", 0)),
        ("AI cost today", f"${float(k.get('ai_cost_today') or 0):,.2f}")))
    risk_items = [f'<b>{_html.escape(str(r.get("name")))}</b> — '
                  f'{_html.escape("; ".join((r.get("churn") or {}).get("reasons") or [])[:160])}' for r in at_risk]
    onb_items = [f'<b>{_html.escape(str(r.get("name")))}</b> — '
                 f'{_html.escape(str((r.get("onboarding") or {}).get("next_step") or "setup incomplete"))}'
                 for r in onboarding]
    issue_items = [f'{_html.escape(str(i.get("restaurant") or ""))}: {_html.escape(str(i.get("title") or ""))}'
                   for i in (ov.get("issues") or [])[:10]]
    inner = (f'<p style="font-size:16px;font-weight:700;color:{b["strong"]};margin:0 0 10px">Your week in Cavnar AI</p>'
             f'<table style="border-collapse:collapse">{pipeline}</table>'
             + _list("Churn risk", risk_items) + _list("Onboarding not finished", onb_items)
             + _list("Top issues", issue_items)
             + f'<p style="font-size:12px;color:{b["muted"]};margin-top:18px">Everything here is on the admin '
               f'console: dashboard.cavnar.ai/admin</p>')
    res = _emails_ops.deliver(email_type="ops_weekly_digest", payload={
        "from": _emails_ops.sender("ops"), "to": [config.will_email()],
        "subject": f"Your week: {k.get('clients', 0)} clients · {k.get('attention', 0)} need you",
        "html": _emails_ops._branded_email(inner)})
    if not getattr(res, "ok", False):
        raise RuntimeError(f"operator weekly digest not sent: {getattr(res, 'error', None) or 'not accepted'}")
    return {"attempted": 1, "ok": 1, "failed": 0, "skipped": 0, "hit_bound": False}


# ── missed windows (#131) ────────────────────────────────────────────────────

def record_missed_window(job, restaurant_id, local_date, detail=None) -> bool:
    """Record that `job` was due for this restaurant on its `local_date`
    and its local window closed with nothing sent — once per (job,
    restaurant, date). Nothing recorded "window closed, not sent", so a
    brief a long pass pushed past 2pm local looked like a quiet morning."""
    try:
        from models import get_conn
        conn = get_conn()
        try:
            cur = conn.execute("INSERT OR IGNORE INTO missed_windows (job, restaurant_id, local_date, detail) "
                               "VALUES (?,?,?,?)", (str(job)[:100], restaurant_id, str(local_date)[:10],
                                                   (str(detail)[:200] if detail else None)))
            conn.commit()
            return (cur.rowcount or 0) > 0
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"missed window not recorded ({job}, {restaurant_id}): {e}")
        return False


def missed_windows_since(since):
    """[{job, n}] of windows recorded missed since `since` (UTC)."""
    try:
        from models import get_conn
        conn = get_conn()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT job, COUNT(*) AS n FROM missed_windows WHERE created_at >= ? GROUP BY job ORDER BY n DESC",
                (since,)).fetchall()]
        finally:
            conn.close()
    except Exception:
        return []


# ── the backup ledger (#1, #2, #28) ─────────────────────────────────────────

# A backup older than this is stale — the nightly run missed at least once.
BACKUP_STALE_HOURS = 26


def offsite_configured() -> dict:
    """Which off-site copies this deployment can make: object storage
    (BACKUP_S3_*) and the encrypted email (BACKUP_ENCRYPTION_KEY + Resend).
    Both need the encryption key: nothing leaves the server in the clear."""
    key = bool((os.getenv("BACKUP_ENCRYPTION_KEY") or "").strip())
    s3 = all((os.getenv(v) or "").strip() for v in ("BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET",
                                                   "BACKUP_S3_ACCESS_KEY_ID", "BACKUP_S3_SECRET_ACCESS_KEY"))
    try:
        import emails as _e
        resend = bool(_e._resend_key())
    except Exception:
        resend = bool((os.getenv("RESEND_API_KEY") or "").strip())
    return {"encryption_key": key, "s3": s3 and key, "email": resend and key,
            "any": key and (s3 or resend)}


def record_backup_run(row: dict, db_path=None):
    """Insert one backup_runs row; returns its id or None. Never raises."""
    cols = ("started_at", "finished_at", "local_ok", "integrity_ok", "size_bytes", "local_path", "offsite_ok",
            "offsite_target", "offsite_error", "sha256", "db_bytes", "wal_bytes", "backups_bytes",
            "free_bytes", "detail_json")
    data = {c: row.get(c) for c in cols if row.get(c) is not None}
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            names = ", ".join(data)
            cur = conn.execute(f"INSERT INTO backup_runs ({names}) VALUES ({', '.join('?' * len(data))})",
                               tuple(data.values())) if data else conn.execute(
                "INSERT INTO backup_runs DEFAULT VALUES")
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        log.error(f"backup run not recorded: {e}")
        return None


def backup_status(db_path=None) -> dict:
    """The newest backup, for /health, the console and the SLA watchdog:
    {last_local_ok_at, last_offsite_ok_at, last_size_bytes, last_error,
    offsite_configured, age_hours, offsite_age_hours, state, summary}.

    state is ok | stale (newest good snapshot older than BACKUP_STALE_HOURS)
    | no_offsite (no off-site copy in that long) | failed (the last run
    failed) | never. Never raises."""
    cfg = offsite_configured()
    out = {"last_local_ok_at": None, "last_offsite_ok_at": None, "last_size_bytes": None, "last_error": None,
           "offsite_configured": bool(cfg.get("any")), "offsite_targets": [t for t in ("s3", "email") if cfg.get(t)],
           "age_hours": None, "offsite_age_hours": None, "state": "never", "summary": None, "last_run_at": None}
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            last = conn.execute("SELECT * FROM backup_runs ORDER BY id DESC LIMIT 1").fetchone()
            local = conn.execute("SELECT finished_at, size_bytes, (julianday('now') - julianday(finished_at)) * 24.0 "
                                 "AS age FROM backup_runs WHERE local_ok=1 ORDER BY id DESC LIMIT 1").fetchone()
            off = conn.execute("SELECT finished_at, (julianday('now') - julianday(finished_at)) * 24.0 AS age "
                               "FROM backup_runs WHERE offsite_ok=1 ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
    except Exception as e:
        out["state"], out["summary"] = "never", f"backup ledger unreadable: {e}"
        return out
    if last:
        out["last_run_at"] = last["finished_at"] or last["started_at"]
        errs = [e for e in (last["offsite_error"],) if e]
        if not last["local_ok"]:
            try:
                import json as _json
                errs.insert(0, (_json.loads(last["detail_json"] or "{}") or {}).get("error") or "snapshot failed")
            except Exception:
                errs.insert(0, "snapshot failed")
        out["last_error"] = "; ".join(str(e) for e in errs)[:300] or None
    if local:
        out["last_local_ok_at"], out["last_size_bytes"] = local["finished_at"], local["size_bytes"]
        out["age_hours"] = round(float(local["age"]), 1) if local["age"] is not None else None
    if off:
        out["last_offsite_ok_at"] = off["finished_at"]
        out["offsite_age_hours"] = round(float(off["age"]), 1) if off["age"] is not None else None
    if not last:
        out["state"], out["summary"] = "never", "No backup has run on this database yet."
    elif not last["local_ok"]:
        out["state"] = "failed"
        out["summary"] = f"The last backup failed: {out['last_error'] or 'snapshot failed'}."
    elif out["age_hours"] is None or out["age_hours"] > BACKUP_STALE_HOURS:
        out["state"] = "stale"
        out["summary"] = (f"The newest good backup is {out['age_hours']:g} hours old."
                          if out["age_hours"] is not None else "There is no good backup.")
    elif out["offsite_age_hours"] is None or out["offsite_age_hours"] > BACKUP_STALE_HOURS:
        out["state"] = "no_offsite"
        out["summary"] = ("No off-site copy of the database " +
                          (f"in {out['offsite_age_hours']:g} hours" if out["offsite_age_hours"] is not None else "exists")
                          + (" — nothing is configured (BACKUP_S3_* / BACKUP_ENCRYPTION_KEY)."
                             if not cfg.get("any") else f": {out['last_error'] or 'the copy failed'}."))
    else:
        out["state"] = "ok"
        out["summary"] = f"Backed up {out['age_hours']:g}h ago, off-site {out['offsite_age_hours']:g}h ago."
    return out


def storage_trend(days=30, db_path=None) -> dict:
    """The database, WAL and backup sizes recorded by each night's backup,
    for a days-to-full line on Engineering (#28): {rows, growth_bytes_per_day,
    days_to_full}. Never raises."""
    out = {"rows": [], "growth_bytes_per_day": None, "days_to_full": None}
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT date(started_at) AS date, db_bytes, wal_bytes, backups_bytes, free_bytes FROM backup_runs "
                "WHERE started_at >= datetime('now', ?) AND db_bytes IS NOT NULL ORDER BY id",
                (f"-{int(days)} days",)).fetchall()]
        finally:
            conn.close()
    except Exception:
        return out
    out["rows"] = rows
    if len(rows) >= 2:
        first, last = rows[0], rows[-1]
        from datetime import date as _date
        try:
            span = max(1, (_date.fromisoformat(last["date"]) - _date.fromisoformat(first["date"])).days)
        except Exception:
            span = max(1, len(rows) - 1)
        used = lambda r: (r.get("db_bytes") or 0) + (r.get("wal_bytes") or 0) + (r.get("backups_bytes") or 0)
        growth = (used(last) - used(first)) / span
        out["growth_bytes_per_day"] = int(growth)
        if growth > 0 and last.get("free_bytes"):
            out["days_to_full"] = int(last["free_bytes"] / growth)
    return out
