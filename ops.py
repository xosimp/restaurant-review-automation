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
import contextlib
import json
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
# "data_quality" (Audit #23, 10/21/26, A6-04): a check of the data itself
# found something — a reconcile gap, a payments gap, a DSR/POS mismatch, a
# short first read. The run that found it worked: the console lists it per
# store as a data issue (admin_ops) and never as "Job X failed", which at
# 1,500 stores was a permanent critical.
CAPTURE_KINDS = ("job", "request", "ai_quality", "audit", "data_quality")

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
    `kind` is one of CAPTURE_KINDS (job, request, ai_quality, audit).

    Sentry hears the first of each (job, exception type) per
    SENTRY_CAPTURE_WINDOW_SECONDS, within an hourly ceiling
    (sentry_setup.allow — blind re-audit 10/20/26 R1-06): every
    per-restaurant failure of a provider outage used to be its own event,
    and the month's quota went in hours. The job_failures row below is
    written for every one, as before."""
    try:
        import sentry_setup
        if sentry_setup.allow(f"{job}|{type(exc).__name__}"):
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
    tb = getattr(exc, "__traceback__", None) if isinstance(exc, BaseException) else None
    if tb is not None:
        # A raised exception's traceback reaches the log (#38): it used to
        # stop at this table and Sentry. Redacted like the row — a traceback
        # ends with the exception's own text, URL and key= included.
        try:
            import traceback as _traceback
            from ai_guard import redact_secrets
            text = redact_secrets("".join(_traceback.format_exception(type(exc), exc, tb)))
            log.warning("captured %s failure: %s", job, redact_secrets(str(exc))[:300],
                        extra={"traceback": text[-6000:], "restaurant_id": rid})
        except Exception:
            pass
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
_NOT_SUCCESS = frozenset(("attempted", "failed", "skipped", "held", "retried", "hit_bound", "closed", "not_yet",
                          "not_supported", "empty", "expired", "unsupported", "missed", "eligible"))
# Kept beside the counts when a job reports it (scale audit 10/19/26 INF-7):
# how many restaurants the pass was FOR, so a run its bound cut short says
# how far it got (jobs_bound_capped).
_COVERAGE_KEYS = ("eligible", "queued", "rows_settled", "pending")

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
    own, every other number the job counted is a success.

    Every scheduled job returns its counts or raises (#39), so a result
    without counts is no longer taken for a clean run: False is a failed
    run, True a clean one (an explicit yes), and anything else — a bare
    None, a dict counting nothing recognisable — partial, so the Jobs page
    shows amber instead of a green run that proved nothing."""
    import json as _json
    counts = standard_counts(result)
    if counts is None:
        if result is True:
            return RUN_OK, None
        return (RUN_FAILED if result is False else RUN_PARTIAL), None
    failed, attempted = counts["failed"], counts["attempted"]
    state = RUN_OK
    if failed and attempted and failed >= attempted:
        state = RUN_FAILED
    elif failed or counts["hit_bound"]:
        state = RUN_PARTIAL
    blob_counts = {k: result.get(k) for k in _RESULT_KEYS + _COVERAGE_KEYS if k in result}
    for k, v in counts.items():
        blob_counts.setdefault(k, v)
    try:
        blob = _json.dumps(blob_counts, default=str)[:500]
    except (TypeError, ValueError):
        blob = None
    return state, _with_held_back(blob, blob_counts, result)


# What a run held back, kept beside its counts (memory audit 9/29/26):
# prune_ledgers returns `refused` (windows under their floor — nothing
# deleted) and `capped` (tables the per-pass cap stopped). The Jobs page
# reads a run from its stored counts alone, so without them it could not
# say which tables a partial prune left.
_HELD_BACK_KEYS = ("refused", "capped")


def _with_held_back(blob, blob_counts, result):
    """`blob` with each non-empty _HELD_BACK_KEYS list added, as valid JSON
    within the 500-character column budget: a list that would not fit is
    cut, and `<key>_n` says how many there were."""
    import json as _json
    held = {k: result.get(k) for k in _HELD_BACK_KEYS if isinstance(result.get(k), list) and result.get(k)}
    if not held or blob is None or len(blob) >= 500:
        return blob
    body = dict(blob_counts)
    for k, items in held.items():
        body[k + "_n"] = len(items)
        body[k] = []
        for it in items:
            body[k].append(it)
            try:
                if len(_json.dumps(body, default=str)) > 500:
                    body[k].pop()
                    break
            except (TypeError, ValueError):
                body[k].pop()
                break
    try:
        out = _json.dumps(body, default=str)
    except (TypeError, ValueError):
        return blob
    return out if len(out) <= 500 else blob


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
_MARKERS_CARRIED = "boot:claims_markers_carried_157"

# A claim whose run started and never finished may be claimed again once
# the run is DEAD (DATA-20): its pulse silent for RUN_DEAD_MINUTES, or — a
# run with no pulse at all — started longer ago than the job's own bound
# (jobs_registry.max_minutes), never more than CLAIM_RECLAIM_MINUTES. It
# was a flat 120 minutes after the claim, so a deploy at 3:10 re-ran the
# POS sync at 5:10, after the morning chain had read stale data; and a run
# close_orphaned_runs had already closed as interrupted (a restart that
# took over 10 minutes) was never reclaimed at all, so the job missed its
# day (blind re-audit 10/20/26 R1-10). A run still alive in THIS process is
# never reclaimed whatever its age (_running_jobs), nor is one whose pulse
# (job_runs.pulse_at) shows another process still running it.
CLAIM_RECLAIM_MINUTES = int(os.getenv("CLAIM_RECLAIM_MINUTES", "120"))
# A job killed this many times in a day is not reclaimed again that day: a
# job that takes its process down with it (an out-of-memory export) must
# not become a crash loop (R1-01 / R1-10). The SLA and the digest say so.
CLAIM_RECLAIMS_PER_DAY = int(os.getenv("CLAIM_RECLAIMS_PER_DAY", "3"))
_INTERRUPTED = "interrupted"

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
        # The lease's one row, here at boot: acquire_scheduler_lease ran this
        # and the CREATE TABLE on every tick, pulse and lane renew (scale
        # audit 10/19/26 INF-18 — no DDL on a per-call path).
        conn.execute("INSERT OR IGNORE INTO scheduler_lease (id, owner, heartbeat_at) VALUES (1, NULL, NULL)")
        for table, columns in (("job_runs", _RUNS_COLUMNS), ("job_failures", _FAILURE_COLUMNS),
                               ("scheduler_lease", _LEASE_COLUMNS), ("async_jobs", _ASYNC_JOB_COLUMNS)):
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
            # A press joins a pending job of its own restaurant (SCALE-30).
            "CREATE INDEX IF NOT EXISTS idx_async_jobs_rid ON async_jobs(restaurant_id, kind, status)",
        ):
            conn.execute(sql)
        conn.execute("INSERT OR IGNORE INTO scheduler_heartbeat (id) VALUES (1)")
        # The permanent markers that lived in the pruned claims table (#157),
        # carried over once with the time each was first claimed — once:
        # every boot scanned the whole claims table four times (scale audit
        # 10/19/26 DB-14), for rows nothing has written there since.
        if not conn.execute("SELECT 1 FROM ops_markers WHERE key=?", (_MARKERS_CARRIED,)).fetchone():
            for pattern in _MARKER_PATTERNS:
                # The pattern written into the statement (a module constant):
                # db_dialect carries a literal GLOB to Postgres as a regex
                # with its [0-9] class; a bound one only as * and ?, and the
                # calibration markers were never carried there (R9-02).
                conn.execute("INSERT OR IGNORE INTO ops_markers (key, created_at) "
                             f"SELECT job_key, claimed_at FROM job_period_claims WHERE job_key GLOB '{pattern}'")
            conn.execute("INSERT OR IGNORE INTO ops_markers (key) VALUES (?)", (_MARKERS_CARRIED,))
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


def _claims_fail_closed(error=None) -> bool:
    """Whether a claim that cannot be checked is refused (blind re-audit
    10/20/26 R1-09). On Postgres — production — always: a checkout that
    timed out on the bounded pool ("connection pool exhausted", a normal
    peak there, not an outage) or a connect breaker that is open says
    nothing about whether an earlier tick claimed the period, and the
    process memo then answered yes and re-sent a digest or a summary. The
    next tick asks again, so a refusal delays the work by a tick; it never
    loses the period. The memo below stays only for SQLite, whose failure
    is a locked or read-only volume (DATA-22)."""
    try:
        import db_dialect
        if db_dialect.backend() == "postgres":
            return True
    except Exception:
        pass
    text = str(error or "").lower()
    return "pool exhausted" in text or "database unreachable" in text


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
    than missing one. A run closed as interrupted (close_orphaned_runs, at
    a boot or a takeover) is a run that never finished, not a finished one
    (R1-10). Dead means its pulse has been silent RUN_DEAD_MINUTES — or,
    with no pulse, it started more than the job's own bound ago (at most
    CLAIM_RECLAIM_MINUTES) — never a flat two hours. A job killed
    CLAIM_RECLAIMS_PER_DAY times in a day is left for its SLA to report."""
    with _running_lock:
        if _running_jobs.get(job):
            return False                       # still running here, however long
    row = conn.execute(
        "SELECT claimed_at FROM job_period_claims WHERE job_key=? AND claimed_at < datetime('now', ?)",
        (key, f"-{int(RUN_DEAD_MINUTES)} minutes")).fetchone()
    if not row:
        return False
    claimed_at = row[0]
    bound = max(int(RUN_DEAD_MINUTES), min(int(CLAIM_RECLAIM_MINUTES), int(jobs_registry.max_minutes(job))))
    interrupted = f"{_INTERRUPTED}%"
    # The run is found by its job name OR the claim it ran under: the
    # "ops_digest" claim runs "ops_failure_digest", "intraday" runs six jobs,
    # and a lookup by claim key alone never found them (DH2-16).
    # CASE WHEN, not SUM(<boolean>): Postgres has no sum(boolean), and this
    # raised on every call there — every period a dead process held was
    # never reclaimed, so a job a deploy killed lost its day (blind re-audit
    # 10/20/26 R9-02: the recorded-statement PREPARE found it).
    try:
        runs = conn.execute(
            "SELECT SUM(CASE WHEN finished_at IS NULL OR COALESCE(error, '') LIKE ? THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN finished_at IS NOT NULL AND COALESCE(error, '') NOT LIKE ? THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN {_run_alive_sql()} THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN finished_at IS NULL AND pulse_at IS NULL AND started_at >= datetime('now', ?) "
            "THEN 1 ELSE 0 END) FROM job_runs "
            "WHERE (job=? OR claim_job=?) AND started_at >= datetime(?, '-5 minutes')",
            (interrupted, interrupted, f"-{bound} minutes", job, job, claimed_at)).fetchone()
        killed = conn.execute(
            "SELECT COUNT(*) FROM job_runs WHERE (job=? OR claim_job=?) AND error LIKE ? "
            "AND started_at >= datetime('now', '-1 day')", (job, job, interrupted)).fetchone()[0]
    except sqlite3.OperationalError:
        runs = conn.execute(
            "SELECT SUM(CASE WHEN finished_at IS NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN finished_at IS NOT NULL THEN 1 ELSE 0 END), 0, "
            "SUM(CASE WHEN finished_at IS NULL AND started_at >= datetime('now', ?) THEN 1 ELSE 0 END) "
            "FROM job_runs WHERE job=? AND started_at >= datetime(?, '-5 minutes')",
            (f"-{bound} minutes", job, claimed_at)).fetchone()
        killed = 0
    if not runs or not runs[0] or runs[1] or runs[2] or runs[3]:
        return False
    if int(killed or 0) >= CLAIM_RECLAIMS_PER_DAY:
        log.error(f"claim_period({key}): {job} was interrupted {killed} times in a day — not reclaimed again "
                  "today (a job that takes its process down must not loop)")
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

    On Postgres a claim that cannot be checked is REFUSED, never memoised
    (_claims_fail_closed, R1-09): the next tick asks again.
    """
    key = f"{job}:{period}"
    try:
        from models import get_conn
        conn = get_conn()
    except Exception as e:
        if _claims_fail_closed(e):
            log.error(f"claim_period({key}) could not be checked ({e}) — refused; the next tick asks again")
            return False
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
        if _claims_fail_closed(e):
            log.error(f"claim_period({key}) could not write ({e}) — refused; the next tick asks again")
            return False
        return _memo_claim(key, e)
    finally:
        conn.close()


def claim_cooldown(key: str, minutes: float) -> bool:
    """True, and stamps `key`, unless it was stamped within the last
    `minutes` — one atomic upsert on job_period_claims, so two presses at
    the same instant cannot both pass. For actions that must not repeat on a
    double-click but may be repeated deliberately later (an admin resending
    a welcome email). Fails OPEN on a database error on SQLite; on
    Postgres it fails CLOSED (R1-09: the supplier-order email's cooldown
    re-sent on a pool timeout) — the person presses again."""
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
        if _claims_fail_closed(e):
            log.error(f"claim_cooldown({key}) failed, refusing: {e}")
            return False
        log.error(f"claim_cooldown({key}) failed, allowing: {e}")
        return True


def period_claimed(job: str, period: str) -> bool:
    """Whether `job` already holds `period`, without claiming it. For work
    that claims only once it has finished (notify's morning batch claims at
    flush), so a pass can skip what an earlier pass completed. Fails open
    (False) on SQLite: run again rather than silently skip. On Postgres it
    fails closed (True, R1-09): the morning batch claims at flush, so "not
    claimed" on a pool timeout re-sent what an earlier pass had sent; the
    next pass asks again."""
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
    except Exception as e:
        return _claims_fail_closed(e)


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


def renew_cooldown(key: str) -> bool:
    """Stamp a held claim_cooldown claim again (a heartbeat), so work that
    outlasts the cooldown keeps it while it runs and a killed process's
    claim still lapses one cooldown after its last beat (pos_archive's
    single-flight day read, blind re-audit 10/20/26 R3-15). True when the
    claim was there to renew. Never raises."""
    try:
        from models import get_conn
        conn = get_conn()
        try:
            n = conn.execute("UPDATE job_period_claims SET claimed_at=datetime('now') WHERE job_key=?",
                             (f"cooldown:{key}",)).rowcount
            conn.commit()
            return bool(n)
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"renew_cooldown({key}) failed: {e}")
        return False


# ── a failed job's retry, kept across restarts (blind re-audit 10/20/26 R1-10)
#
# The loop's retry of a failed non-sending job (scheduler._PulsedOps._after:
# the period back after RETRY_BACKOFF_MINUTES) lived in process memory, so a
# deploy between the failure and its retry lost the retry and the job its
# day. Each pending retry is a job_cursors row (key "retry:<claim>|<period>",
# value {"attempt", "at": epoch seconds}); the runner reads them when it
# takes the lease.
_RETRY_KEY = "retry:"
RETRY_KEEP_HOURS = 30


def _retry_key(claim, period):
    return f"{_RETRY_KEY}{claim}|{period}"


def record_retry(claim, period, attempt, at_epoch, only=None) -> None:
    """Keep a pending retry durably — a partial retry with the restaurant
    ids it re-runs (`only`). Never raises."""
    body = {"attempt": int(attempt), "at": float(at_epoch)}
    if only is not None:
        body["only"] = [int(x) for x in only][:5000]
    try:
        from models import get_conn
        conn = get_conn()
        try:
            conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                         (_retry_key(claim, period), json.dumps(body)))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"retry of {claim}:{period} not kept durably: {e}")


def clear_retry(claim, period) -> None:
    """Forget a retry (the job succeeded, or gave up). Never raises."""
    try:
        from models import get_conn
        conn = get_conn()
        try:
            conn.execute("DELETE FROM job_cursors WHERE key=?", (_retry_key(claim, period),))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"retry of {claim}:{period} not cleared: {e}")


def pending_retries() -> list:
    """[(claim, period, attempt, at_epoch)] of the retries kept in the last
    RETRY_KEEP_HOURS; older rows are dropped (their period is over)."""
    out = []
    try:
        from models import get_conn
        conn = get_conn()
        try:
            conn.execute("DELETE FROM job_cursors WHERE key LIKE ? AND updated_at < datetime('now', ?)",
                         (f"{_RETRY_KEY}%", f"-{int(RETRY_KEEP_HOURS)} hours"))
            conn.commit()
            rows = conn.execute("SELECT key, value FROM job_cursors WHERE key LIKE ?", (f"{_RETRY_KEY}%",)).fetchall()
        finally:
            conn.close()
        for key, value in ((r[0], r[1]) for r in rows):
            claim, sep, period = str(key)[len(_RETRY_KEY):].rpartition("|")
            try:
                v = json.loads(value or "{}")
                if sep and claim:
                    out.append((claim, period, int(v.get("attempt") or 1), float(v.get("at") or 0)))
            except (TypeError, ValueError):
                continue
    except Exception as e:
        log.warning(f"pending retries not read: {e}")
    return out


def pending_retry_scopes() -> dict:
    """{(claim, period): [restaurant ids]} of the pending PARTIAL retries
    (record_retry `only`): what each re-runs. Never raises."""
    out = {}
    try:
        from models import get_conn
        conn = get_conn()
        try:
            rows = conn.execute("SELECT key, value FROM job_cursors WHERE key LIKE ?", (f"{_RETRY_KEY}%",)).fetchall()
        finally:
            conn.close()
        for key, value in ((r[0], r[1]) for r in rows):
            claim, sep, period = str(key)[len(_RETRY_KEY):].rpartition("|")
            try:
                v = json.loads(value or "{}")
            except (TypeError, ValueError):
                continue
            if sep and claim and isinstance(v.get("only"), list):
                out[(claim, period)] = [int(x) for x in v["only"]]
    except Exception as e:
        log.warning(f"pending partial retries not read: {e}")
    return out


def failed_restaurants(jobs, since) -> set:
    """The restaurant ids whose failures `jobs` (names) captured since
    `since` (a UTC "YYYY-MM-DD HH:MM:SS"): what a partial retry re-runs
    (scheduler._PulsedOps._after). job_failures carries the restaurant each
    per-restaurant capture names (capture's context, restaurant_id=N). Empty
    when unreadable."""
    names = sorted({str(j) for j in (jobs or ()) if j})
    if not names or not since:
        return set()
    try:
        from models import get_conn
        conn = get_conn()
        try:
            rows = conn.execute("SELECT DISTINCT restaurant_id FROM job_failures WHERE job IN ("
                                + ",".join("?" * len(names)) + ") AND created_at >= ? AND restaurant_id IS NOT NULL "
                                "AND COALESCE(kind, 'job') = 'job'", tuple(names) + (str(since),)).fetchall()
        finally:
            conn.close()
        return {int(r[0]) for r in rows}
    except Exception as e:
        log.warning(f"failed restaurants of {names} unreadable: {e}")
        return set()


# ── permanent markers (#157) ────────────────────────────────────────────────

def claim_marker(job: str, value: str) -> bool:
    """True the first time `job:value` is marked, False ever after — for
    "once, ever" facts (this competitor at this level was told; this reading
    was reported unreadable; this source was first seen at …). The same
    shape as claim_period, in a table that is never pruned. Falls back to
    the process memo, like claim_period, when the table cannot be written —
    on SQLite only; on Postgres an unwritable marker is refused (R1-09)."""
    key = f"{job}:{value}"
    try:
        from models import get_conn
        conn = get_conn()
    except Exception as e:
        if _claims_fail_closed(e):
            log.error(f"claim_marker({key}) could not be checked ({e}) — refused")
            return False
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
        if _claims_fail_closed(e):
            return False
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
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    deadline_at   TEXT,
    started_at    TEXT,
    request_json  TEXT,
    progress_json TEXT,
    stop_requested_at TEXT,
    saved_ref     INTEGER
)"""
# Added after the table shipped; init_ops adds them to an older database.
# deadline_at is the job's own wall-clock limit (UTC, SQLite datetime text),
# when the job set one (set_async_job_deadline; schedule audit 10/3/26 P-22).
# started_at is when it began running — a generation waits for a pool slot
# first, and its age is counted from here (re-audit 10/4/26 PIPE-9).
# request_json is what was asked (a schedule's week, days, draft and
# instruction): a press joins only the same request (UI-8).
# progress_json is how far a pending job has got, in its own terms — a
# schedule generation's days drafted so far (AI cost audit 10/7/26 #36,
# set_async_job_progress) — read back on a pending poll.
# stop_requested_at is the owner's Stop on a job that had already saved
# something (stop_async_job); the job reads it and says what happened.
# saved_ref is the first durable thing the job saved (a schedule's history
# id, written in the same transaction as the draft — models.
# save_schedule_history): with it set, Stop never says "nothing was saved"
# (schedule audit 10/10/26 ARCH-2, BUGSLIFE-26).
_ASYNC_JOB_COLUMNS = (("deadline_at", "TEXT"), ("started_at", "TEXT"), ("request_json", "TEXT"),
                      ("progress_json", "TEXT"), ("stop_requested_at", "TEXT"), ("saved_ref", "INTEGER"))

# Long enough for the slowest generation plus a client that backgrounds the
# app mid-poll; short enough that abandoned results don't accumulate.
_ASYNC_JOB_TTL_HOURS = 6


def _async_conn():
    """A connection for the async job store. No DDL here (memory audit
    9/29/26, "outside"): it ran CREATE TABLE IF NOT EXISTS async_jobs and a
    commit on every job read and write — a write-lock take on every poll —
    while init_ops has created the table at boot all along (CLAUDE.md:
    never schema DDL on a request or per-call path)."""
    from models import get_conn
    return get_conn()


# The longest any job can plausibly run. A schedule generation now sets its
# own, shorter deadline as soon as its calls are planned (at most
# schedule_engine.SCHEDULE_JOB_MAX_SECONDS, which a test holds under this;
# schedule audit 10/3/26 P-22), and is read dead past that; this ceiling is
# for a job that set none. A job still pending past it is dead, whatever
# process owned it.
JOB_MAX_MINUTES = 45
# A job the queue will resume after a restart (sweep_stale_jobs) has this
# long from the boot before a poll calls it dead.
RESUME_DEADLINE_SECONDS = 20 * 60
# An owner's generation waiting past this to start (the queue's interactive
# p95 over the last hour) warns the operator (check_platform_sla).
QUEUE_WAIT_ALERT_SECONDS = int(os.getenv("QUEUE_WAIT_ALERT_SECONDS", "120"))
# The queue worker process silent this long while it is meant to run
# (gunicorn.conf.py starts it) warns the operator (scale audit 10/19/26
# INF-4): the web process then consumes the whole queue itself, and nothing
# said so.
QUEUE_WORKER_ALERT_SECONDS = int(os.getenv("QUEUE_WORKER_ALERT_SECONDS", "300"))
# A row that may run (past its not_before) still queued this long in a
# non-generation group warns (INF-6): the bulk or POS rows are not draining
# before the morning that reads them.
QUEUE_BACKLOG_ALERT_SECONDS = int(float(os.getenv("QUEUE_BACKLOG_ALERT_HOURS", "6")) * 3600)
# A job whose last this-many runs ALL hit their time bound warns (INF-7): it
# is no longer reaching every restaurant each period, and a bounded run was
# counted a success (job_runs.ok 2), so no SLA saw it.
BOUND_ALERT_RUNS = max(2, int(os.getenv("BOUND_ALERT_RUNS", "3")))
# A job whose last FAILING_ALERT_RUNS finished runs each failed at least
# FAILING_ALERT_SHARE of what it attempted (or crashed), or attempted nothing
# while restaurants were eligible, warns (Audit #25 A07-03): a sweep failing
# for 49 of 50 stores was a partial run — counted a success, never paged.
FAILING_ALERT_RUNS = max(2, int(os.getenv("JOB_FAILING_ALERT_RUNS", "2")))
FAILING_ALERT_SHARE = float(os.getenv("JOB_FAILING_ALERT_SHARE", "0.5"))
# ...judged only on a run that attempted at least this many (re-audit B7
# P2, 10/27/26): one restaurant of two with a revoked credential made a
# nightly job "overdue" — an urgent page a day for a single tenant's problem
# its reconnect flow already handles. Below it a partial run counts toward
# the SLA and the per-restaurant rules speak; a run whose every attempt
# failed (or that raised) is still a failed run at any size.
FAILING_MIN_ATTEMPTED = max(1, int(os.getenv("JOB_FAILING_MIN_ATTEMPTED", "3")))
# Failed job_queue rows (dead letters) per kind in the last hour that warn
# (A07-07): QUEUE_FAILED_ALERT_ROWS of one kind, or QUEUE_FAILED_ALERT_SHARE
# of the kind's finished rows with at least QUEUE_FAILED_ALERT_MIN failed.
# A failed owner press or auto-draft (QUEUE_FAILED_URGENT_KINDS) is urgent.
QUEUE_FAILED_ALERT_ROWS = int(os.getenv("QUEUE_FAILED_ALERT_ROWS", "5"))
QUEUE_FAILED_ALERT_SHARE = float(os.getenv("QUEUE_FAILED_ALERT_SHARE", "0.2"))
QUEUE_FAILED_ALERT_MIN = 2
# A rate, never a bare count (re-audit B7 P2: 5 dead letters of a kind an
# hour paged whatever the kind's volume — at 1,500 stores a routine 0.3%):
# a kind warns at QUEUE_FAILED_ALERT_MIN failed and QUEUE_FAILED_ALERT_SHARE
# of its finished rows, or at QUEUE_FAILED_ALERT_ROWS failed and this
# smaller share of a busy kind's.
QUEUE_FAILED_ALERT_VOLUME_SHARE = float(os.getenv("QUEUE_FAILED_ALERT_VOLUME_SHARE", "0.05"))
# A queued row of a kind this build has no handler for, this long after the
# boot and this old, is named (re-audit B7 P2; job_queue.expire_unknown_kinds
# expires it after days).
QUEUE_UNKNOWN_KIND_ALERT_SECONDS = 30 * 60
QUEUE_FAILED_URGENT_KINDS = ("schedule_generate", "auto_draft")

# How long past its own deadline (set_async_job_deadline) a job may still be
# writing its result before a poll calls it dead.
ASYNC_DEADLINE_GRACE_SECONDS = 90
# A job that has SAVED its draft (async_jobs.saved_ref) and is past its
# deadline is finishing its after-save steps, not dead: a poll waits this
# much longer before it says so, and even then says the draft is saved —
# provisionally, so the job's own finish still replaces it (blind re-audit
# 10/11/26 BUGSGEN-13).
SAVED_FINISH_GRACE_SECONDS = 5 * 60
# The verdict a poll or the boot sweep writes over a job that saved a draft
# but did not finish: the draft is in force, and the job's own finish (or a
# resumed try's) may still replace this (finish_async_job).
SAVED_UNFINISHED = {"ok": False, "saved": True, "provisional": True,
                    "error": "Cavnar AI saved a draft for this week but didn't finish its last steps — open it "
                             "from Schedule History and read it through before you publish."}
_PROVISIONAL_MARK = '"provisional": true'


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

    A job the durable queue will run again (job_queue — scheduler audit
    10/10/26 SCALE-2, RM-7: a queued row, or a running one with tries left)
    is NOT dead: the next consumer resumes it from its checkpoints, reading
    the model answers its earlier try paid for. It stays pending, its
    deadline pushed RESUME_DEADLINE_SECONDS out so a poll does not call it
    dead while it restarts.
    """
    try:
        resumable = []
        try:
            import job_queue as _jq_sweep
            resumable = [str(x) for x in _jq_sweep.resumable_external_ids()]
        except Exception as e:
            log.warning(f"sweep_stale_jobs: the queue could not be read ({e}); every pending job is failed")
            resumable = []
        conn = _async_conn()
        keep = (" AND job_id NOT IN (" + ",".join("?" * len(resumable)) + ")") if resumable else ""
        # A dead job that had saved its draft is never "interrupted — try
        # again": the draft is in force (BUGSGEN-13).
        import json as _json_sweep
        conn.execute(
            "UPDATE async_jobs SET status='error', result_json=? "
            "WHERE status='pending' AND saved_ref IS NOT NULL AND created_at <= datetime('now', ?)" + keep,
            [_json_sweep.dumps(SAVED_UNFINISHED), f"-{int(older_than_minutes)} minutes"] + resumable)
        cur = conn.execute(
            "UPDATE async_jobs SET status='error', result_json=? "
            "WHERE status='pending' AND created_at <= datetime('now', ?)" + keep,
            ['{"ok": false, "error": "Generation was interrupted — please try again."}',
             f"-{int(older_than_minutes)} minutes"] + resumable)
        if resumable:
            conn.execute("UPDATE async_jobs SET deadline_at=datetime('now', ?) WHERE status='pending' "
                         "AND job_id IN (" + ",".join("?" * len(resumable)) + ")",
                         [f"+{int(RESUME_DEADLINE_SECONDS)} seconds"] + resumable)
        conn.commit()
        n = cur.rowcount or 0
        conn.close()
        if n:
            print(f"[ops] swept {n} stale pending job(s)")
        return n
    except Exception as e:
        print(f"sweep_stale_jobs failed: {e}")
        return 0


# A pending job past its own deadline (set_async_job_deadline, P-22) is dead
# whatever its age: never joined, never counted as the one running.
_NOT_PAST_DEADLINE = ("AND (deadline_at IS NULL OR deadline_at >= datetime('now', '-%d seconds'))"
                      % ASYNC_DEADLINE_GRACE_SECONDS)
# A job is judged by its own deadline once it has one, and only without one
# by its age — from when it started running, not from when it queued for a
# slot (re-audit 10/4/26 PIPE-9: a generation that waited ten minutes for a
# slot was called dead at 45 minutes from its press, four minutes inside its
# own deadline, and its paid draft thrown away). A job still queued is aged
# from its press.
_YOUNG = "AND (deadline_at IS NOT NULL OR COALESCE(started_at, created_at) >= datetime('now', ?))"


class JobBusy(Exception):
    """A pending job of this kind for this restaurant is doing something
    else than what was asked (claim_async_job `request` — re-audit 10/4/26
    UI-8): it is never joined. `request` is what the running job was asked
    (None when it did not say)."""

    def __init__(self, job_id, request):
        super().__init__(f"job {job_id} is running another request")
        self.job_id = job_id
        self.request = request


def _request_text(request):
    import json
    return None if request is None else json.dumps(request, sort_keys=True, default=str)


def running_job(kind, restaurant_id, max_age_minutes: int = JOB_MAX_MINUTES):
    """(job_id, request) of this restaurant's pending job of `kind`, or
    (None, None) — what it was asked (claim_async_job `request`) included."""
    import json
    try:
        conn = _async_conn()
        row = conn.execute(
            "SELECT job_id, request_json FROM async_jobs WHERE kind=? AND restaurant_id=? AND status='pending' "
            + _YOUNG + " " + _NOT_PAST_DEADLINE + " ORDER BY created_at DESC LIMIT 1",
            (str(kind), restaurant_id, f"-{int(max_age_minutes)} minutes")).fetchone()
        conn.close()
        if not row:
            return None, None
        try:
            req = json.loads(row["request_json"]) if row["request_json"] else None
        except (TypeError, ValueError):
            req = None
        return row["job_id"], req
    except Exception:
        return None, None


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
            + _YOUNG + " " + _NOT_PAST_DEADLINE + " ORDER BY created_at DESC LIMIT 1",
            (str(kind), restaurant_id, f"-{int(max_age_minutes)} minutes")).fetchone()
        conn.close()
        return row["job_id"] if row else None
    except Exception:
        return None


def claim_async_job(job_id, kind, restaurant_id, max_age_minutes: int = JOB_MAX_MINUTES, request=None):
    """(job_id, joined): start `job_id` as this restaurant's one pending job
    of `kind`, or join the one already running — checked and inserted in one
    write transaction. active_job then start_async_job was check-then-insert,
    so two presses at the same instant started two paid generations
    (SCHED-25 / DATA-23).

    With `request` (what is asked, JSON-able) only a job asked exactly that
    is joined; one asked anything else raises JobBusy and nothing starts
    (re-audit 10/4/26 UI-8: Generate for one week silently joined a redo of
    another, and the owner's instruction was lost)."""
    import json
    conn = _async_conn()
    try:
        # One restaurant's jobs of one kind serialise (check-then-insert),
        # no one else's (SCALE-12).
        import db_dialect
        db_dialect.begin_write(conn, "async_jobs", kind, restaurant_id)
        row = conn.execute(
            "SELECT job_id, request_json FROM async_jobs WHERE kind=? AND restaurant_id=? AND status='pending' "
            + _YOUNG + " " + _NOT_PAST_DEADLINE + " ORDER BY created_at DESC LIMIT 1",
            (str(kind), restaurant_id, f"-{int(max_age_minutes)} minutes")).fetchone()
        if row:
            conn.rollback()
            if request is not None and row["request_json"] != _request_text(request):
                try:
                    running = json.loads(row["request_json"]) if row["request_json"] else None
                except (TypeError, ValueError):
                    running = None
                raise JobBusy(row["job_id"], running)
            return row["job_id"], True
        conn.execute("INSERT OR REPLACE INTO async_jobs (job_id, kind, restaurant_id, status, result_json, request_json)"
                     " VALUES (?,?,?, 'pending', NULL, ?)", (str(job_id), str(kind), restaurant_id,
                                                              _request_text(request)))
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
    """Store a finished job's payload. `status` is 'done' or 'error'.

    Only a job still pending is written: one already failed — swept at boot,
    or read past its own deadline (read_async_job) — keeps that verdict. A
    generation that ran on past it used to overwrite "didn't finish" with a
    draft the owner had been told never came (schedule audit 10/3/26 P-22).
    The one exception: a job that saved its draft and was given the
    provisional "saved but didn't finish" verdict (SAVED_UNFINISHED) is
    finished by its own finish — the draft it names is the one in force
    (blind re-audit 10/11/26 BUGSGEN-13)."""
    import json
    try:
        payload = json.dumps(result)
    except (TypeError, ValueError) as e:
        status, payload = "error", json.dumps({"ok": False, "error": f"Result could not be stored: {e}"})
    try:
        conn = _async_conn()
        cur = conn.execute(
            "UPDATE async_jobs SET status=?, result_json=? WHERE job_id=? AND (status='pending' OR "
            "(status='error' AND saved_ref IS NOT NULL AND result_json LIKE ?))",
            (status, payload, str(job_id), f"%{_PROVISIONAL_MARK}%"),
        )
        conn.commit()
        conn.close()
        finished = cur.rowcount == 1
    except Exception as e:
        log.error(f"finish_async_job({job_id}) failed: {e}")
        return
    if finished:
        # Its Live Activity, if a phone is showing one, ends with how it
        # went (#38). Never raises.
        import live_activities
        live_activities.job_finished(job_id, status, result)


def rewrite_async_result(job_id, result) -> None:
    """Replace a FINISHED job's stored result, keeping its status — the
    admin console removing a read-once value (the review account's new
    password) after its first read. finish_async_job writes only a pending
    job now (P-22), so this rewrite has its own door."""
    import json
    try:
        payload = json.dumps(result)
        conn = _async_conn()
        conn.execute("UPDATE async_jobs SET result_json=? WHERE job_id=? AND status<>'pending'",
                     (payload, str(job_id)))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"rewrite_async_result({job_id}) failed: {e}")


def set_async_job_deadline(job_id, deadline_ts, started_ts=None) -> None:
    """Record the wall-clock time (a time.time() value) by which a job will
    have finished or failed (schedule audit 10/3/26 P-22). Past it, plus
    ASYNC_DEADLINE_GRACE_SECONDS, a poll reports the job dead — judged by
    that deadline alone, never by JOB_MAX_MINUTES (re-audit 10/4/26 PIPE-9).
    `started_ts` is when the job began running (kept from its first record).
    Raises nothing: a job whose deadline cannot be written still runs, and
    is judged by JOB_MAX_MINUTES as before."""
    from datetime import datetime as _dt, timezone as _tz
    try:
        at = _dt.fromtimestamp(float(deadline_ts), tz=_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
        began = (_dt.fromtimestamp(float(started_ts), tz=_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
                 if started_ts is not None else None)
        conn = _async_conn()
        conn.execute("UPDATE async_jobs SET deadline_at=?, started_at=COALESCE(started_at, ?) WHERE job_id=? "
                     "AND status='pending'", (at, began, str(job_id)))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"set_async_job_deadline({job_id}) failed: {e}")


def set_async_job_progress(job_id, progress) -> None:
    """Record how far a pending job has got (`progress`, JSON-able) — a
    schedule generation's days drafted so far (AI cost audit 10/7/26 #36):
    a pending poll returns it. Written only while the job is pending, and
    only as often as the caller has news (the generation writes once per
    newly finished day). Raises nothing: a progress line is never a reason
    for a job to fail."""
    import json
    try:
        payload = json.dumps(progress)
        conn = _async_conn()
        conn.execute("UPDATE async_jobs SET progress_json=? WHERE job_id=? AND status='pending'",
                     (payload, str(job_id)))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"set_async_job_progress({job_id}) failed: {e}")
        return
    # A phone showing this generation's Live Activity hears of the new day
    # (iOS parity audit 10/7/26 #38). Never raises.
    import live_activities
    live_activities.job_progress(job_id, progress)


def stop_async_job(job_id, result) -> str:
    """The owner's Stop (schedule audit 10/10/26 ARCH-2, BUGSLIFE-26): what
    it did, as one word.

    "stopped" — the job had saved nothing, so its row is finished now with
    `result` (the restaurant may start another at once). The job's own save
    checks the row in the same transaction as its write (models.
    save_schedule_history `job_id`), so nothing can be saved after this.
    "stopping" — the job had already saved a draft (`saved_ref`): the row
    stays pending with `stop_requested_at` set and the job, which knows what
    it saved, finishes it and says what happened — the route never writes
    "nothing was saved" over a draft that replaced the owner's.
    "finished" — it had already finished. One UPDATE each: SQLite orders
    them against the job's save, so the two can never cross."""
    import json
    try:
        payload = json.dumps(result)
        conn = _async_conn()
        try:
            cur = conn.execute("UPDATE async_jobs SET status='error', result_json=? WHERE job_id=? "
                               "AND status='pending' AND saved_ref IS NULL", (payload, str(job_id)))
            if cur.rowcount == 1:
                conn.commit()
                verdict = "stopped"
            else:
                cur = conn.execute("UPDATE async_jobs SET stop_requested_at=datetime('now') WHERE job_id=? "
                                   "AND status='pending'", (str(job_id),))
                conn.commit()
                verdict = "stopping" if cur.rowcount == 1 else "finished"
        finally:
            conn.close()
    except Exception as e:
        log.error(f"stop_async_job({job_id}) failed: {e}")
        return "finished"
    if verdict == "stopped":
        import live_activities
        live_activities.job_finished(job_id, "error", result)
    return verdict


def job_stop_state(job_id) -> str:
    """Where a running job stands against a Stop: "running", "stop_requested"
    (the owner stopped it after it saved — stop_async_job), "stopped" (its
    row was finished as stopped) or "gone" (finished any other way — a poll
    past its deadline, the boot sweep). "running" when the row cannot be
    read (never stop a job over a bookkeeping failure)."""
    try:
        conn = _async_conn()
        try:
            row = conn.execute("SELECT status, stop_requested_at, result_json FROM async_jobs WHERE job_id=?",
                               (str(job_id),)).fetchone()
        finally:
            conn.close()
    except Exception:
        return "running"
    if row is None:
        return "running"
    if row["status"] == "pending":
        return "stop_requested" if row["stop_requested_at"] else "running"
    if row["status"] == "error" and '"stopped": true' in (row["result_json"] or ""):
        return "stopped"
    return "gone"


def job_still_pending(job_id) -> bool:
    """Whether the job's row still reads pending — False once a poll has
    called it dead (read_async_job) or the boot sweep failed it. A job that
    finds itself failed does not save what it was building. True when the
    row cannot be read (never stop a job over a bookkeeping failure)."""
    try:
        conn = _async_conn()
        row = conn.execute("SELECT status FROM async_jobs WHERE job_id=?", (str(job_id),)).fetchone()
        conn.close()
        return True if row is None else row["status"] == "pending"
    except Exception:
        return True


_QUEUE_ENDED_MESSAGES = {
    "expired": "The schedule couldn't be started in time — try again.",
    "failed": "The schedule couldn't be finished, so nothing was saved. Try again.",
    "cancelled": "Stopped — nothing was saved.",
    "done": "The schedule ended without a result, so nothing was saved. Try again.",
}


def _queue_state(job_id):
    """The durable queue's word on an async job it runs (job_queue.row_for):
    "live" (queued, or running on a lease not yet run out), the row's end
    state, or None (the queue does not run this job). Never raises."""
    try:
        import job_queue as _jq_state
        row = _jq_state.row_for(job_id)
    except Exception:
        return None
    if not row:
        return None
    st = row.get("state")
    if st == "queued" or (st == "running" and float(row.get("lease_until") or 0) > time.time()):
        return "live"
    if st == "running":
        return None                       # its process died: the next consumer resumes it
    return st


def read_async_job(job_id, restaurant_id=None, kinds=None):
    """The job's {"status", "result"}, or None if it doesn't exist (or belongs
    to another restaurant, or — with `kinds` — is another kind of job: a
    route that polls schedule generations answers only those, so an invoice
    scan's costs never come back through it; blind re-audit 10/20/26 R7-21).

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
            "SELECT job_id, kind, restaurant_id, status, result_json, progress_json, "
            "(deadline_at IS NULL AND COALESCE(started_at, created_at) < datetime('now', ?)) AS overdue, "
            "(deadline_at IS NOT NULL AND deadline_at < datetime('now', ?)) AS past_deadline, "
            "((deadline_at IS NULL AND COALESCE(started_at, created_at) < datetime('now', ?)) OR "
            " (deadline_at IS NOT NULL AND deadline_at < datetime('now', ?))) AS long_past, "
            "saved_ref, "
            "CAST(strftime('%s', deadline_at) AS INTEGER) - CAST(strftime('%s', 'now') AS INTEGER) AS seconds_left "
            "FROM async_jobs WHERE job_id=?",
            (f"-{JOB_MAX_MINUTES} minutes", f"-{ASYNC_DEADLINE_GRACE_SECONDS} seconds",
             f"-{JOB_MAX_MINUTES * 60 + SAVED_FINISH_GRACE_SECONDS} seconds",
             f"-{ASYNC_DEADLINE_GRACE_SECONDS + SAVED_FINISH_GRACE_SECONDS} seconds", str(job_id)),
        ).fetchone()
        if not row:
            conn.close()
            return None
        # Scoped where the caller is authenticated: a job belongs to the
        # restaurant that started it, whatever the id in the URL.
        # A row with no restaurant is nobody's to read through a scoped
        # call (scale audit 10/19/26, SEC-18: it failed open).
        if restaurant_id is not None and (row["restaurant_id"] is None
                                          or int(row["restaurant_id"]) != int(restaurant_id)):
            conn.close()
            return None
        if kinds is not None and str(row["kind"] or "") not in tuple(kinds):
            conn.close()
            return None
        status = row["status"]
        # One source of truth for a job the durable queue runs (blind
        # re-audit 10/11/26 ARCH-10): the queue row says whether it can still
        # finish. A row that expired unstarted, failed, was cancelled or
        # ended without writing a result will never write one — the owner
        # is told now rather than polling to a timeout (unless the job saved
        # its draft: that is BUGSGEN-13's case below); a row still queued
        # (waiting its turn) or running on a live lease is never called dead
        # by its age, only by its own deadline.
        queue_state = _queue_state(job_id) if status == "pending" else None
        if queue_state in ("expired", "failed", "cancelled", "done") and row["saved_ref"] is None:
            failed = {"ok": False, "error": _QUEUE_ENDED_MESSAGES[queue_state]}
            try:
                conn.execute("UPDATE async_jobs SET status='error', result_json=? WHERE job_id=? "
                             "AND status='pending'", (json.dumps(failed), str(job_id)))
                conn.commit()
            except Exception as e:
                log.error(f"read_async_job({job_id}) could not record the ended queue job: {e}")
            conn.close()
            return {"status": "error", "result": failed}
        overdue = row["past_deadline"] or (row["overdue"] and queue_state != "live")
        if status == "pending" and overdue and row["saved_ref"] is not None:
            # The job saved its draft and is finishing (its after-save
            # steps): not dead. The poll waits; well past that it says the
            # draft is saved, provisionally — the job's own finish still
            # replaces it (blind re-audit 10/11/26 BUGSGEN-13).
            if not row["long_past"]:
                conn.close()
                return {"status": "pending", "result": None, "seconds_left": 0, "saved": True}
            saved = dict(SAVED_UNFINISHED, history_id=int(row["saved_ref"]))
            try:
                conn.execute("UPDATE async_jobs SET status='error', result_json=? WHERE job_id=? "
                             "AND status='pending'", (json.dumps(saved), str(job_id)))
                conn.commit()
            except Exception as e:
                log.error(f"read_async_job({job_id}) could not record the unfinished saved job: {e}")
            conn.close()
            return {"status": "error", "result": saved}
        if status == "pending" and overdue:
            # Past any real generation (or past the job's own deadline): its
            # result was never stored (the write failed, or the process died
            # after the boot sweep ran). Polling 'pending' forever helps
            # nobody (DATA-9). Written, once, so a job still running cannot
            # later overwrite this verdict with a draft (P-22): it sees the
            # row failed and saves nothing (job_still_pending).
            failed = {"ok": False, "error": "The schedule didn't finish in the time a generation is given, so "
                                            "nothing was saved. Try again."}
            try:
                conn.execute("UPDATE async_jobs SET status='error', result_json=? WHERE job_id=? "
                             "AND status='pending'", (json.dumps(failed), str(job_id)))
                conn.commit()
            except Exception as e:
                log.error(f"read_async_job({job_id}) could not record the overdue job: {e}")
            conn.close()
            return {"status": "error", "result": failed}
        if status == "pending":
            conn.close()
            out = {"status": "pending", "result": None}
            if row["seconds_left"] is not None:
                # How long the job can still run, when it set its deadline:
                # a client waits that long rather than a guessed 15 minutes.
                out["seconds_left"] = max(0, int(row["seconds_left"]))
            if row["progress_json"]:
                # How far it has got (set_async_job_progress, #36).
                try:
                    out["progress"] = json.loads(row["progress_json"])
                except (TypeError, ValueError):
                    pass
            return out
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
            # ADMIN_TASK_WORKERS is the container's bound: each gunicorn
            # web worker takes its share (pool_sizing; one worker: all of it).
            import pool_sizing
            _admin_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=pool_sizing.sized("admin_task", max(1, ADMIN_TASK_WORKERS)),
                thread_name_prefix="admin-task")
    # Who asked, taken here on the request thread: in the pool thread the
    # request is gone, and every model call would read as 'system' (#148).
    try:
        import ai_utils as _ai
        attribution = _ai.attribution_for_thread()
        attribution["trigger"] = attribution.get("trigger") if attribution.get("trigger") not in (None, "system") \
            else "admin"
        attribution["correlation_id"] = attribution.get("correlation_id") or f"admin:{kind}:{job_id}"
    except Exception:
        _ai, attribution = None, {}

    def _go():
        outcome = {}

        def body(*a, **k):
            outcome["result"] = fn(*a, **k)
            return outcome["result"]
        try:
            with (_ai.ai_context(**attribution) if _ai is not None else contextlib.nullcontext()):
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
    job_ctx = _job_context(name, run_id)
    job_ctx.__enter__()
    try:
        result = fn(*args, **kwargs)
        state, blob = run_outcome(result)
        err = None
        counts = standard_counts(result)
        if counts is not None and state != RUN_OK:
            err = (f"{counts.get('failed') or 0} of {counts.get('attempted') or 0} failed"
                   + (" · stopped at its time bound" if counts.get("hit_bound") else ""))
        elif counts is None and state != RUN_OK:
            err = "the job returned False" if result is False else "the job returned no counts (#39)"
        if state == RUN_OK or counts is None:
            captured = _failures_during(names, run_id, db_path=db_path)
            if captured:
                state = RUN_PARTIAL if state == RUN_OK else state
                err = f"{captured} failure{'s' if captured != 1 else ''} captured during the run" + (
                    f"; {err}" if err else "")
        _record_run_end(run_id, started, state, err, db_path=db_path, result_json=blob)
        return result
    except Exception as e:
        # Inside the job's log context, so the line carries job=<name>;
        # capture() logs the traceback (redacted) beside it (#38).
        log.error("Job '%s' crashed: %s", name, _redacted(e))
        capture(e, job=name, db_path=db_path, restaurant_id=restaurant_id)
        _record_run_end(run_id, started, RUN_FAILED, e, db_path=db_path)
        return None
    finally:
        job_ctx.__exit__(None, None, None)
        stop.set()
        with _running_lock:
            for n in names:
                _running_jobs[n] -= 1
                if not _running_jobs[n]:
                    del _running_jobs[n]


@contextlib.contextmanager
def _job_context(name, run_id):
    """What one job run carries while it runs: `job=<name>` on every log
    line (logging_setup.context, #38), and the AI attribution of every model
    and Places call it makes (ai_utils.ai_context, #148) — trigger
    "scheduler" and correlation id "job:<name>:<run id>", unless the caller
    already said otherwise (an admin's Run now, an owner's background task:
    their trigger and correlation id stand). Neither can fail the job."""
    with contextlib.ExitStack() as stack:
        try:
            import logging_setup
            stack.enter_context(logging_setup.context(job=name))
        except Exception as e:
            log.debug(f"job log context unavailable: {e}")
        try:
            import ai_utils
            outer = ai_utils.current_ai_context()
            stack.enter_context(ai_utils.ai_context(
                trigger=None if outer.get("trigger") else "scheduler",
                correlation_id=None if outer.get("correlation_id") else f"job:{name}:{run_id}"))
        except Exception as e:
            log.debug(f"job AI context unavailable: {e}")
        yield


def _redacted(exc):
    """An exception's text with secrets taken out (a requests error carries
    the URL it failed on, and a Places URL carries key=) — for log lines."""
    try:
        from ai_guard import redact_secrets
        return redact_secrets(str(exc))[:500]
    except Exception:
        return type(exc).__name__


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


def return_job_request(request_id):
    """Hand a taken request back (running -> pending) when it could not be
    started now — its job's lane is busy (platform re-audit 10/7/26 #11) —
    so a later tick takes it again; take_job_requests still expires it
    after RUN_REQUEST_TTL_MINUTES."""
    try:
        from models import get_conn
        conn = get_conn()
        try:
            conn.execute("UPDATE job_run_requests SET status='pending', taken_at=NULL, taken_by=NULL "
                         "WHERE id=? AND status='running'", (int(request_id),))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.error(f"return_job_request({request_id}) failed: {e}")


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
PLATFORM_WARN_COOLDOWN_MINUTES = 24 * 60


def run_failed_share(ok, result_json):
    """The share of what one finished run attempted that failed: 1.0 for a
    failed run (ok 0 — it raised, or every attempt failed), failed/attempted
    from its stored counts, and None when it counted nothing it attempted (a
    clean run without counts, a bound-only partial, a run with no blob)."""
    import json as _json
    try:
        if int(ok) == RUN_FAILED:
            return 1.0
    except (TypeError, ValueError):
        return None
    try:
        b = _json.loads(result_json) if result_json else None
    except (TypeError, ValueError):
        b = None
    if not isinstance(b, dict):
        return None
    attempted, failed = _int(b.get("attempted")), _int(b.get("failed"))
    if attempted <= 0:
        return None
    return min(1.0, failed / float(attempted))


def _run_attempted(result_json) -> int:
    """What one finished run counted as attempted (0: no counts)."""
    import json as _json
    try:
        b = _json.loads(result_json) if result_json else None
    except (TypeError, ValueError):
        return 0
    return _int(b.get("attempted")) if isinstance(b, dict) else 0


def _healthy_run(ok, result_json) -> bool:
    """Whether a finished run counts as a success for the SLA (A07-03): a
    clean run (ok 1), or a partial one (ok 2) that failed under
    FAILING_ALERT_SHARE of what it attempted — a run that failed 9 of 10
    restaurants is not a success however it is scored — or that attempted
    fewer than FAILING_MIN_ATTEMPTED (one tenant of two is not the job
    failing; re-audit B7)."""
    try:
        ok = int(ok)
    except (TypeError, ValueError):
        return False
    if ok == RUN_OK:
        return True
    if ok != RUN_PARTIAL:
        return False
    if 0 < _run_attempted(result_json) < FAILING_MIN_ATTEMPTED:
        return True
    share = run_failed_share(ok, result_json)
    return share is None or share < FAILING_ALERT_SHARE


def _failing_run(ok, result_json) -> bool:
    """Whether one finished run counts toward jobs_failing: it raised or
    every attempt failed (ok 0), or it failed at least FAILING_ALERT_SHARE
    of at least FAILING_MIN_ATTEMPTED attempts."""
    share = run_failed_share(ok, result_json)
    if share is None:
        return False
    try:
        if int(ok) == RUN_FAILED:
            return True
    except (TypeError, ValueError):
        return False
    return share >= FAILING_ALERT_SHARE and _run_attempted(result_json) >= FAILING_MIN_ATTEMPTED


def _last_healthy_finish(conn, job, scan=20):
    """finished_at of `job`'s newest healthy run (_healthy_run), or None.
    The newest `scan` successful-or-partial runs are read through
    idx_job_runs_job and judged here (their counts are JSON); past them,
    the newest clean run — any healthy partial older than the scanned ones
    is older than all of them, so at worst this reads a little early."""
    rows = conn.execute("SELECT finished_at, ok, result_json FROM job_runs WHERE job=? AND ok IN (1, 2) "
                        "AND finished_at IS NOT NULL ORDER BY started_at DESC LIMIT ?", (job, int(scan))).fetchall()
    for r in rows:
        if _healthy_run(r[1], r[2]):
            return r[0]
    if len(rows) < scan:
        return None
    row = conn.execute("SELECT finished_at FROM job_runs WHERE job=? AND ok=1 AND finished_at IS NOT NULL "
                       "ORDER BY started_at DESC LIMIT 1", (job,)).fetchone()
    return row[0] if row else None


def jobs_overdue(now=None, db_path=None) -> list:
    """[{job, max_hours, last_ok_at, hours_since}] for every EXPECTED_JOBS
    entry whose last successful run is older than its SLA — a clean run, or
    a partial one that failed under FAILING_ALERT_SHARE of what it attempted
    (_healthy_run; A07-03: a job whose every run failed 49 of 50 stores was
    never overdue). A job that has
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
            last = _last_healthy_finish(conn, job)
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


_bound_cache = {}
import process_caches as _process_caches  # noqa: E402 (registered beside each cache, A04-13)
_process_caches.register("ops._bound_cache", _bound_cache)
_BOUND_TTL_SECONDS = 600


def jobs_bound_capped(db_path=None, runs=None, use_cache=True) -> list:
    """[{job, runs, eligible, attempted}] for every registered job whose last
    `runs` (BOUND_ALERT_RUNS) finished runs ALL hit their time bound (scale
    audit 10/19/26 INF-7): a resumable sweep that is cut short every pass
    rotates through the fleet instead of covering it — pos_sync reaching
    each store every second night at 1,500 — while every one of those runs
    counts as a success and no SLA fires. One indexed read per job (the
    jobs_overdue pattern), kept ten minutes. Never raises: [] unreadable."""
    import json as _json
    n = int(runs or BOUND_ALERT_RUNS)
    key = (db_path or "", n)
    hit = _bound_cache.get(key)
    if use_cache and hit and time.monotonic() - hit[0] < _BOUND_TTL_SECONDS:
        return list(hit[1])
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
    except Exception:
        return []
    out = []
    try:
        for job in jobs_registry.JOBS:
            rows = conn.execute("SELECT result_json FROM job_runs WHERE job=? AND finished_at IS NOT NULL "
                                "ORDER BY started_at DESC LIMIT ?", (job, n)).fetchall()
            if len(rows) < n:
                continue
            blobs = []
            for r in rows:
                try:
                    blobs.append(_json.loads(r[0]) if r[0] else {})
                except (TypeError, ValueError):
                    blobs.append({})
            if all(isinstance(b, dict) and b.get("hit_bound") for b in blobs):
                last = blobs[0]
                out.append({"job": job, "runs": n, "eligible": last.get("eligible"),
                            "attempted": last.get("attempted")})
    except Exception as e:
        log.warning(f"jobs_bound_capped unreadable: {e}")
        return []
    finally:
        conn.close()
    _bound_cache[key] = (time.monotonic(), list(out))
    return out


_failing_cache = {}
_process_caches.register("ops._failing_cache", _failing_cache)


def _run_idle(result_json) -> bool:
    """A run that attempted nothing and skipped nothing while restaurants
    were eligible for it (its own `eligible` count), and was not cut short:
    the eligibility query or the per-restaurant body broke, and the run read
    as clean (A07-03)."""
    import json as _json
    try:
        b = _json.loads(result_json) if result_json else None
    except (TypeError, ValueError):
        return False
    if not isinstance(b, dict) or _int(b.get("eligible")) <= 0:
        return False
    return _int(b.get("attempted")) == 0 and _int(b.get("skipped")) == 0 and not b.get("hit_bound")


def jobs_failing(db_path=None, runs=None, use_cache=True) -> list:
    """[{job, runs, why: "failing"|"idle", share, attempted, failed,
    eligible}] for every registered job whose last `runs`
    (FAILING_ALERT_RUNS) finished runs ALL failed at least
    FAILING_ALERT_SHARE of what they attempted (a crashed run counts as all
    failed; a run of fewer than FAILING_MIN_ATTEMPTED attempts only when all
    failed — _failing_run), or ALL attempted nothing while restaurants were
    eligible (_run_idle) — Audit #25 A07-03. One indexed read per job, kept ten
    minutes like jobs_bound_capped. Never raises: [] unreadable."""
    import json as _json
    n = int(runs or FAILING_ALERT_RUNS)
    key = (db_path or "", n)
    hit = _failing_cache.get(key)
    if use_cache and hit and time.monotonic() - hit[0] < _BOUND_TTL_SECONDS:
        return list(hit[1])
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
    except Exception as e:
        log.warning(f"jobs_failing unreadable: {e}")
        return []
    out = []
    try:
        for job in jobs_registry.JOBS:
            rows = conn.execute("SELECT ok, result_json FROM job_runs WHERE job=? AND finished_at IS NOT NULL "
                                "AND ok IS NOT NULL ORDER BY started_at DESC LIMIT ?", (job, n)).fetchall()
            if len(rows) < n:
                continue
            shares = [run_failed_share(r[0], r[1]) for r in rows]
            try:
                last = _json.loads(rows[0][1]) if rows[0][1] else {}
            except (TypeError, ValueError):
                last = {}
            last = last if isinstance(last, dict) else {}
            common = {"job": job, "runs": n, "attempted": last.get("attempted"), "failed": last.get("failed"),
                      "eligible": last.get("eligible")}
            if all(_failing_run(r[0], r[1]) for r in rows):
                out.append(dict(common, why="failing", share=round(shares[0], 2)))
            elif all(_run_idle(r[1]) for r in rows):
                out.append(dict(common, why="idle", share=None))
    except Exception as e:
        log.warning(f"jobs_failing unreadable: {e}")
        return []
    finally:
        conn.close()
    _failing_cache[key] = (time.monotonic(), list(out))
    return out


# A job that fans out as queue rows (pos_sync in queue mode, POS_SYNC_QUEUE)
# finishes when it has QUEUED them: its counts were rows enqueued, `failed`
# always 0, so the failure-share, idle and partial-retry rules never saw a
# store's sync fail (re-audit B7 P2, 10/27/26). settle_queue_fanout rewrites
# such a run's counts from its rows' real outcomes once they have run.
FANOUT_SETTLE_MAX_HOURS = 36


def _row_outcome(state, result_json) -> str:
    """"ok", "failed", "skipped", "missed" (expired unstarted) or "pending"
    for one queue row a fan-out queued."""
    import json as _json
    if state in ("queued", "running"):
        return "pending"
    if state == "expired":
        return "missed"
    if state != "done":
        return "failed"
    try:
        r = _json.loads(result_json) if result_json else {}
    except (TypeError, ValueError):
        r = {}
    if not isinstance(r, dict):
        return "ok"
    if isinstance(r.get("ok"), bool):
        return "ok" if r["ok"] else "failed"
    if r.get("skipped") and not r.get("attempted"):
        return "skipped"
    counts = standard_counts(r)
    if counts and counts["failed"] and counts["failed"] >= max(1, counts["attempted"]):
        return "failed"
    return "ok"


def settle_queue_fanout(job, kind, db_path=None, max_runs=5) -> int:
    """Rewrite the counts of `job`'s recent fan-out runs (their stored
    counts carry `queued`, not yet `rows_settled`) from the outcomes of the
    `kind` rows each queued (created while it ran): attempted the rows that
    ran (and those that expired unstarted), ok / failed / skipped as each
    row ended, `eligible` the rows queued — and its ok column re-judged
    (run_outcome) — so jobs_failing, jobs_overdue and the idle rule read the
    night as it went. A run is settled once none of its rows is still
    queued or running, or FANOUT_SETTLE_MAX_HOURS after it. A run none of
    whose rows ran reads idle (attempted 0, eligible N). Returns runs
    settled; never raises."""
    import json as _json
    settled = 0
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
    except Exception as e:
        log.warning(f"fan-out runs of {job} not settled: {e}")
        return 0
    try:
        runs = conn.execute("SELECT id, started_at, finished_at, result_json FROM job_runs WHERE job=? "
                            "AND finished_at IS NOT NULL AND started_at >= datetime('now', ?) "
                            "ORDER BY started_at DESC LIMIT ?",
                            (job, f"-{FANOUT_SETTLE_MAX_HOURS + 24} hours", int(max_runs))).fetchall()
        for run_id, started, finished, blob in runs:
            try:
                b = _json.loads(blob) if blob else None
            except (TypeError, ValueError):
                b = None
            if not isinstance(b, dict) or not _int(b.get("queued")) or b.get("rows_settled"):
                continue
            tally = {"ok": 0, "failed": 0, "skipped": 0, "missed": 0, "pending": 0}
            for state, res in conn.execute("SELECT state, result_json FROM jobs WHERE kind=? AND created_at >= ? "
                                           "AND created_at <= ?", (kind, started, finished)).fetchall():
                tally[_row_outcome(state, res)] += 1
            old = conn.execute("SELECT (julianday('now') - julianday(?)) * 24.0", (finished,)).fetchone()[0]
            if tally["pending"] and (old or 0) < FANOUT_SETTLE_MAX_HOURS:
                continue
            ran = tally["ok"] + tally["failed"]
            counts = {"attempted": ran + tally["missed"] if ran else 0, "ok": tally["ok"],
                      "failed": tally["failed"] + tally["missed"] if ran else 0,
                      # Only rows that skipped: the stores the pass never queued (no
                      # POS) are not what it was for (`eligible` is the rows).
                      "skipped": tally["skipped"], "hit_bound": False,
                      "eligible": _int(b.get("queued")), "queued": _int(b.get("queued")), "rows_settled": True,
                      "pending": tally["pending"]}
            ok, new_blob = run_outcome(counts)
            conn.execute("UPDATE job_runs SET ok=?, result_json=? WHERE id=?", (ok, new_blob, run_id))
            conn.commit()
            settled += 1
        if settled:
            _failing_cache.clear()
    except Exception as e:
        log.warning(f"fan-out runs of {job} not settled: {e}")
    finally:
        conn.close()
    return settled


def queue_failed_kinds(q) -> list:
    """[{kind, failed, finished, share, urgent}] for every job_queue kind
    whose failed rows (dead letters: failed on their last try, or a
    handler's job_queue.dead_letter — a failed generation) in the last hour
    reach QUEUE_FAILED_ALERT_SHARE of the kind's finished rows with at least
    QUEUE_FAILED_ALERT_MIN failed, or QUEUE_FAILED_ALERT_VOLUME_SHARE with
    at least QUEUE_FAILED_ALERT_ROWS (A07-07; a rate with absolute minimums,
    re-audit B7). From job_queue.stats()'s `kinds`: failed_1h, finished_1h."""
    out = []
    for kind, k in sorted(((q or {}).get("kinds") or {}).items()):
        failed = _int((k or {}).get("failed_1h"))
        finished = _int((k or {}).get("finished_1h"))
        if failed <= 0:
            continue
        share = failed / float(max(finished, failed))
        if (failed >= QUEUE_FAILED_ALERT_MIN and share >= QUEUE_FAILED_ALERT_SHARE) or \
                (failed >= QUEUE_FAILED_ALERT_ROWS and share >= QUEUE_FAILED_ALERT_VOLUME_SHARE):
            out.append({"kind": kind, "failed": failed, "finished": max(finished, failed),
                        "share": round(share, 2), "urgent": kind in QUEUE_FAILED_URGENT_KINDS})
    return out


def queue_problems(q, worker_expected=None) -> list:
    """[(warn kind, sentence)] about the job queue from job_queue.stats():
      * the queue worker process silent past QUEUE_WORKER_ALERT_SECONDS while
        it is meant to run (INF-4) — the web process has the queue;
      * a non-generation group's oldest runnable row waiting past
        QUEUE_BACKLOG_ALERT_SECONDS (INF-6);
      * a kind's failed rows in the last hour past its threshold
        (queue_failed_kinds, A07-07): "queue_failed:<kind>", or
        "queue_failed_urgent:<kind>" for an owner's press or the auto-draft
        — check_platform_sla pages those as urgent;
      * queued rows of a kind this build has no handler for, half an hour
        after the boot and that old: "queue_unknown_kind:<kind>" (re-audit
        B7 — a renamed kind's rows sat forever).
    `worker_expected`: whether gunicorn's master supervises a queue worker
    here (QUEUE_WORKER_SUPERVISED, which it sets before the web worker
    forks); None reads it."""
    out = []
    q = q or {}
    if worker_expected is None:
        worker_expected = (os.getenv("QUEUE_WORKER_SUPERVISED") or "").strip() == "1"
    if worker_expected:
        age = q.get("worker_heartbeat_age_seconds")
        booted = q.get("web_started_age_seconds")
        silent = (age is None and booted is not None and booted > QUEUE_WORKER_ALERT_SECONDS) or \
            (age is not None and age > QUEUE_WORKER_ALERT_SECONDS
             and (booted is None or booted > QUEUE_WORKER_ALERT_SECONDS))
        if silent:
            when = "since this process booted" if age is None else f"for {int(age // 60)} minutes"
            out.append(("queue_worker", f"The queue worker process has not beaten its heartbeat {when} — the web "
                        "process is running every generation, learner and archive row itself. Check the logs "
                        "for 'queue worker exited' or 'no web process booted' (worker.py --queue)."))
    for grp, g in sorted((q.get("groups") or {}).items()):
        if grp == "generation":
            continue
        age = (g or {}).get("oldest_ready_seconds")
        if age is not None and age > QUEUE_BACKLOG_ALERT_SECONDS:
            out.append(("queue_backlog", f"The {grp} queue is behind: its oldest runnable row has waited "
                        f"{age / 3600:.1f} hours (alert past {QUEUE_BACKLOG_ALERT_SECONDS // 3600}) with "
                        f"{int(g.get('queued') or 0)} queued — raise QUEUE_{grp.upper()}_CONCURRENCY or see "
                        "docs/plans/POSTGRES_AND_WORKERS_PLAN.md."))
    booted = q.get("web_started_age_seconds")
    for kind, u in sorted((q.get("unknown_kinds") or {}).items()):
        if (booted is None or booted > QUEUE_UNKNOWN_KIND_ALERT_SECONDS) and \
                float(u.get("oldest_seconds") or 0) > QUEUE_UNKNOWN_KIND_ALERT_SECONDS:
            out.append((f"queue_unknown_kind:{kind}",
                        f"{int(u.get('queued') or 0)} queued {kind} row{'s' if int(u.get('queued') or 0) != 1 else ''} "
                        f"of a kind this build has no handler for (the oldest {float(u['oldest_seconds']) / 3600:.1f} "
                        "hours) — renamed or removed? Nothing will run them; they expire after "
                        "QUEUE_UNKNOWN_KIND_EXPIRE_DAYS (a removed kind's expire hook belongs in "
                        "job_queue.RETIRED_KINDS)."))
    for f in queue_failed_kinds(q):
        what = "queue_failed_urgent" if f["urgent"] else "queue_failed"
        out.append((f"{what}:{f['kind']}",
                    f"{f['failed']} {f['kind']} queue row{'s' if f['failed'] != 1 else ''} failed on their last try "
                    f"in the last hour ({int(round(f['share'] * 100))}% of {f['finished']} finished) — each "
                    f"try's error is in the job failures (queue:{f['kind']})."))
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


# A missing DSR night pages the operator only when more than this share of
# the stores with the report on are missing one (R8-14) — the POS fleet
# outage's rule (pos_health.FLEET_OUTAGE_SHARE). With fewer than 1/share DSR
# stores (today's fleet) any one missing still pages.
DSR_MISSING_PAGE_SHARE = float(os.getenv("DSR_MISSING_PAGE_SHARE", "0.05"))


def _dsr_missing_pages(missing, db_path=None) -> bool:
    """Whether `missing` (dsr.pipeline.nights_missing) is a fleet problem
    worth a page: more than DSR_MISSING_PAGE_SHARE of the DSR fleet — the
    stores that had a report in the last week, and the missing ones. An
    unreadable count pages (fail loud)."""
    from datetime import datetime as _dt, timedelta as _td
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            seen = {int(r[0]) for r in conn.execute(
                "SELECT DISTINCT restaurant_id FROM dsr_reports WHERE business_date >= ?",
                ((_dt.utcnow().date() - _td(days=8)).isoformat(),)).fetchall()}
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"DSR fleet count unavailable: {e}")
        return True
    fleet = len(seen | {int(m.get("restaurant_id") or 0) for m in missing})
    return len(missing) > DSR_MISSING_PAGE_SHARE * fleet


def _dsr_missing(db_path=None):
    """Restaurants whose last business night has no final or provisional
    Daily Sales Report an hour past its deadline (#17). [] when unreadable."""
    try:
        from dsr import pipeline
        return pipeline.nights_missing(db_path=db_path)
    except Exception as e:
        log.warning(f"DSR check unavailable: {e}")
        return []


# Schedule generation end to end (schedule audit 10/3/26 P-24): each week
# saves its stage timings and total (schedule_history.total_seconds, written
# with the draft by models.save_schedule_history). The p95 of the last
# SCHEDULE_P95_SAMPLE generations in SCHEDULE_P95_DAYS past
# SCHEDULE_P95_ALERT_SECONDS is a warning the platform check pages once a
# day — the owner sits waiting on every one, and the clients' wait is 15
# minutes. Fewer than SCHEDULE_P95_MIN generations say nothing.
SCHEDULE_P95_ALERT_SECONDS = 600
SCHEDULE_P95_SAMPLE = 20
SCHEDULE_P95_MIN = 5
SCHEDULE_P95_DAYS = 14


def schedule_generation_latency(db_path=None) -> dict:
    """{"n", "p50", "p95", "max", "slow", "alert_seconds"}: end-to-end
    seconds of the recent schedule generations (nearest-rank percentiles);
    {"n": 0, ...} with none on file."""
    from models import get_conn
    conn = get_conn(db_path) if db_path else get_conn()
    try:
        rows = conn.execute("SELECT total_seconds FROM schedule_history WHERE total_seconds IS NOT NULL "
                            "AND generated_at >= datetime('now', ?) ORDER BY id DESC LIMIT ?",
                            (f"-{int(SCHEDULE_P95_DAYS)} days", int(SCHEDULE_P95_SAMPLE))).fetchall()
    finally:
        conn.close()
    vals = sorted(float(r[0]) for r in rows if r[0] is not None)
    out = {"n": len(vals), "p50": None, "p95": None, "max": None, "slow": False,
           "alert_seconds": SCHEDULE_P95_ALERT_SECONDS}
    if not vals:
        return out

    def rank(p):
        import math
        return vals[max(0, min(len(vals) - 1, int(math.ceil(p / 100.0 * len(vals))) - 1))]
    out.update(p50=round(rank(50), 1), p95=round(rank(95), 1), max=round(vals[-1], 1))
    out["slow"] = len(vals) >= SCHEDULE_P95_MIN and out["p95"] > SCHEDULE_P95_ALERT_SECONDS
    return out


# Each input a schedule generation can go without, as an SLO (schedule audit
# 10/10/26 ARCH-25): the share of the last SCHEDULE_INPUT_SAMPLE generations
# in SCHEDULE_P95_DAYS made without it (the review's inputs_missing, saved
# with each draft). Past SCHEDULE_INPUT_FAIL_ALERT for any one input — with
# at least SCHEDULE_P95_MIN generations — the platform check warns once a
# day: an input that fails that often is a broken read, not bad luck.
SCHEDULE_INPUT_SAMPLE = 40
SCHEDULE_INPUT_FAIL_ALERT = 0.10


def schedule_input_failures(db_path=None) -> dict:
    """{"n", "rates": {input: share of generations without it}, "over":
    [inputs past SCHEDULE_INPUT_FAIL_ALERT], "alert_rate"} over the recent
    generations; {"n": 0, ...} with none on file."""
    import json as _json
    from models import get_conn
    conn = get_conn(db_path) if db_path else get_conn()
    try:
        rows = conn.execute("SELECT review_json FROM schedule_history WHERE total_seconds IS NOT NULL "
                            "AND generated_at >= datetime('now', ?) ORDER BY id DESC LIMIT ?",
                            (f"-{int(SCHEDULE_P95_DAYS)} days", int(SCHEDULE_INPUT_SAMPLE))).fetchall()
    finally:
        conn.close()
    counts, n = {}, 0
    for r in rows:
        n += 1
        try:
            missing = (_json.loads(r[0] or "null") or {}).get("inputs_missing") or []
        except Exception:
            missing = []
        for name in set(str(x) for x in missing if x):
            counts[name] = counts.get(name, 0) + 1
    rates = {k: round(v / float(n), 3) for k, v in sorted(counts.items())} if n else {}
    over = sorted(k for k, v in rates.items() if v > SCHEDULE_INPUT_FAIL_ALERT) if n >= SCHEDULE_P95_MIN else []
    return {"n": n, "rates": rates, "over": over, "alert_rate": SCHEDULE_INPUT_FAIL_ALERT}


# Solves an emergency cap ended (blind re-audit 10/11/26 SCALE-15, SCALE-16):
# each schedule generation names on its review the solves the wall clock or
# CP-SAT's memory bound ended instead of their deterministic work budget
# (schedule_engine.solve_capped). Such a week may differ from the one the
# same inputs give on an idle box. Past SCHEDULE_CAPPED_ALERT of the last
# SCHEDULE_INPUT_SAMPLE generations — with at least SCHEDULE_P95_MIN — the
# platform check warns: the box is too busy for its generation concurrency
# (job_queue.group_concurrency) or a tenant outgrew SCHEDULE_CPSAT_MEMORY_MB.
SCHEDULE_CAPPED_ALERT = 0.05


def schedule_solve_capped(db_path=None) -> dict:
    """{"n", "capped" (generations with any capped solve), "rate", "kinds":
    {"wall:solver": count, ...}, "over", "alert_rate"} over the recent
    generations; {"n": 0, ...} with none on file."""
    import json as _json
    from models import get_conn
    conn = get_conn(db_path) if db_path else get_conn()
    try:
        rows = conn.execute("SELECT review_json FROM schedule_history WHERE total_seconds IS NOT NULL "
                            "AND generated_at >= datetime('now', ?) ORDER BY id DESC LIMIT ?",
                            (f"-{int(SCHEDULE_P95_DAYS)} days", int(SCHEDULE_INPUT_SAMPLE))).fetchall()
    finally:
        conn.close()
    kinds, n, capped = {}, 0, 0
    for r in rows:
        n += 1
        try:
            got = (_json.loads(r[0] or "null") or {}).get("solve_capped") or []
        except Exception:
            got = []
        if got:
            capped += 1
        for k in set(str(x) for x in got if x):
            kinds[k] = kinds.get(k, 0) + 1
    rate = round(capped / float(n), 3) if n else 0.0
    return {"n": n, "capped": capped, "rate": rate, "kinds": dict(sorted(kinds.items())),
            "over": bool(n >= SCHEDULE_P95_MIN and rate > SCHEDULE_CAPPED_ALERT),
            "alert_rate": SCHEDULE_CAPPED_ALERT}


def _has_urgent(out, state, write_ok=True) -> bool:
    """Whether check_platform_sla's problems include one that cannot wait a
    day: the scheduler dead or stuck, a job past its SLA, the volume almost
    full, the database refusing writes."""
    hb = out.get("heartbeat_minutes")
    disk = out.get("disk") or {}
    return bool((hb is not None and hb > HEARTBEAT_ALERT_MINUTES)
                or state.get("wedged") or state.get("loop_stalled")
                or out.get("jobs_overdue")
                or disk.get("state") == "critical"
                or (out.get("pg_volume") or {}).get("state") == "critical"
                or (out.get("requests") or {}).get("error_rate_over")
                or (out.get("requests") or {}).get("error_floor_over")
                or any(r.get("page") == "urgent" for r in out.get("rpower_rules") or [])
                or out.get("queue_urgent")
                or not write_ok)


def _sla_conditions(out, state, warn_kinds) -> dict:
    """{"sla:<condition>": "critical" | "warning"} — what check_platform_sla
    found, one key per condition (the pager's dedup key, resolved when it
    is gone): the urgent ones (_has_urgent's) named, every warning kind as
    it is."""
    c = {}
    hb = out.get("heartbeat_minutes")
    if hb is not None and hb > HEARTBEAT_ALERT_MINUTES:
        c["heartbeat"] = "critical"
    if state.get("wedged"):
        c["loop_wedged"] = "critical"
    elif state.get("loop_stalled"):
        c["loop_stalled"] = "critical"
    for j in out.get("jobs_overdue") or []:
        c[f"job_overdue:{j.get('job')}"] = "critical"
    if (out.get("disk") or {}).get("state") == "critical":
        c["disk_critical"] = "critical"
    if (out.get("pg_volume") or {}).get("state") == "critical":
        c["pg_volume_critical"] = "critical"
    if out.get("write_ok") is False:
        c["write_refused"] = "critical"
    rq = out.get("requests") or {}
    if rq.get("error_rate_over"):
        c["http_5xx_rate"] = "critical"
    elif rq.get("error_floor_over"):
        c["http_5xx_floor"] = "critical"
    for r in out.get("rpower_rules") or []:
        if r.get("page") == "urgent":
            c[str(r.get("key"))] = "critical"
    for k in out.get("queue_urgent_kinds") or []:
        c[k] = "critical"
    for k in warn_kinds:
        c.setdefault(str(k), "warning")
    return {PAGER_CONDITION_PREFIX + k: v for k, v in c.items()}


def check_platform_sla(send=True, db_path=None, write_ok=None) -> dict:
    """The scheduler's watchdog: {heartbeat_minutes, loop_minutes,
    running_job, jobs_overdue, disk, write_ok, backup, dsr_missing, problems,
    alerted, ...}. Run with send=True every five minutes by the web process's
    supervisor thread (platform_monitor.PlatformSupervisor), never from the
    scheduler it watches; /health calls it read-only (send=False), which
    pages nothing and skips the reads only paging needs.

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
      * a DSR night is missing past its deadline (#17);
      * a messaging channel is broken (notify.messaging_problems, fix round
        E): an inbound webhook refusing every request, no verified Resend
        event while mail goes out, Twilio account errors, an operator
        address suppressed. Read only when paging (send=True) — /health's
        read-only call does not publish or need them;
      * Audit #25 (read only when paging): a job whose last runs failed at
        least half of what they attempted, or attempted nothing while
        restaurants were eligible (jobs_failing, A07-03); a queue kind's
        failed rows (queue_problems, A07-07 — an owner's press or the
        auto-draft urgent); an AI surface's refusals and fallbacks over
        twice its 14-day rate, the schedule generations' fallback share
        over twice its baseline, a DSR night still provisional at noon, the
        weekly smoke eval off its baseline (ai_quality_watch, A07-04,
        A07-11, A07-06);
      * the iPhone app could not decode the server's answers on more than
        CLIENT_DECODE_ALERT_DEVICES devices in an hour (client_compat).

    Only where the scheduler is meant to run (scheduler.scheduling_allowed):
    a laptop has no scheduler and must not page anyone. Owners are never
    contacted from here. Never raises. `write_ok` is the caller's own write
    probe (/health may already have run one).

    The pager gets each condition as its own incident ("sla:<condition>",
    _sla_conditions) and a RESOLVE for each one that has cleared
    (pager_resolve_cleared), on every send=True pass where the scheduler may
    page — re-audit B7 P2.

    What /health's read-only call (send=False) reads — each bounded, none
    paging-only (re-audit B7 P3): the scheduler's state (one row), the jobs
    overdue (one indexed lookup per registered job), the volume and the
    Postgres volume, the write probe (unless given), the backup's state, the
    DSR nights missing, the schedule generations' samples (the last
    GEN/INPUT/CAPPED sample rows), the queue's live rows (queued and running,
    grouped — not its finished rows, job_queue.stats dead_letters=False),
    the request rollups of the last 5, 10 and 15 minutes and the signed-in
    5xx of the last 10 (not the post-boot 7-day route scan, request_slo
    fresh=False) and the iPhone decode reports of the last hour (indexed on
    created_at). Read only when paging: messaging health, the failing and
    bound-capped jobs, the AI quality watch, the RPOWER rules and the POS
    fleet, the queue's dead letters and the fresh-5xx routes."""
    out = {"heartbeat_minutes": None, "loop_minutes": None, "running_job": None, "jobs_overdue": [],
           "disk": None, "pg_volume": None, "write_ok": None, "backup": None, "dsr_missing": [],
           "dsr_missing_paged": False, "messaging": [], "problems": [],
           "schedule_generation": None, "schedule_inputs": None, "schedule_capped": None, "queue": None,
           "bound_capped": [], "requests": None, "alerted": False,
           "jobs_failing": [], "queue_urgent": False, "ai_watch": []}
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
        # The Postgres service's own volume (scale re-audit 10/20/26 R8-06):
        # nothing watched it, so the first sign of it filling was every
        # write failing. "n/a" on SQLite.
        out["pg_volume"] = _sm.pg_volume_state(db_path)
    except Exception:
        out["disk"] = out.get("disk")
    if write_ok is None:
        write_ok, write_err = write_probe(db_path)
    else:
        write_err = None if write_ok else "write probe failed"
    out["write_ok"] = bool(write_ok)
    out["backup"] = backup_status(db_path=db_path)
    out["dsr_missing"] = _dsr_missing(db_path=db_path)
    if send:
        try:
            import notify
            out["messaging"] = list(notify.messaging_problems(db_path=db_path) or [])
        except Exception as e:
            log.warning(f"messaging health unavailable to the platform check: {e}")

    problems = []
    # Kinds that can wait a day: a volume filling (not full), a stale
    # backup, a late DSR, a messaging channel. Anything else is urgent.
    # 10/3/26: "Volume filling up - 239 MB free" texted Will every hour
    # for a day because every problem shared one 60-minute cooldown.
    warn_kinds = set()
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
        if disk["state"] == "low":
            warn_kinds.add("disk_low")
        what = "App volume (backups)" if disk.get("backend") == "postgres" else "Volume"
        problems.append(f"{what} {'almost full' if disk['state'] == 'critical' else 'filling up'} — "
                        f"{disk.get('free_mb', '?')} MB free ({disk.get('pct_free', '?')}%).")
    pv = out.get("pg_volume") or {}
    if pv.get("state") in ("low", "critical"):
        if pv["state"] == "low":
            warn_kinds.add("pg_volume_low")
        problems.append(f"Postgres volume {'almost full' if pv['state'] == 'critical' else 'filling up'} — "
                        f"{pv.get('used_mb')} MB of {pv.get('max_mb')} MB used ({pv.get('pct_used')}%: the "
                        f"database {pv.get('db_mb')} MB, its write-ahead log {pv.get('wal_mb')} MB). A full "
                        "volume refuses every write: grow it in Railway, then raise PG_VOLUME_MAX_BYTES.")
    elif pv.get("state") == "unset":
        warn_kinds.add("pg_volume_unset")
        problems.append(f"The Postgres database is {pv.get('used_mb')} MB and PG_VOLUME_MAX_BYTES is not set, so "
                        "nothing can warn before its volume fills — set it to the Postgres volume's size "
                        "(Railway -> Postgres -> Volume; docs/ops/ENVIRONMENT.md).")
    if not out["write_ok"]:
        problems.append(f"The database refuses writes: {write_err or 'write probe failed'}.")
    b = out["backup"] or {}
    if b.get("state") in ("stale", "failed", "no_offsite"):
        warn_kinds.add("backup")
        problems.append(b.get("summary") or "The nightly backup is not healthy.")
    # A missing DSR pages only above a share of the fleet's DSR stores (scale
    # re-audit 10/20/26 R8-14): at hundreds of stores some store always has
    # a closed night or a revoked token, and a daily page about one store's
    # night is not the operator's to fix — it stays on the console's list
    # (out["dsr_missing"]). Below DSR_MISSING_PAGE_SHARE stores it is any.
    if out["dsr_missing"] and _dsr_missing_pages(out["dsr_missing"], db_path):
        out["dsr_missing_paged"] = True
        warn_kinds.add("dsr_missing")
        names = ", ".join(str(m.get("restaurant") or m.get("restaurant_id")) for m in out["dsr_missing"][:4])
        # A night still provisional (it went out without its sales and never
        # became final — Audit #23 A6-20) is said as such, apart from a
        # missing one (Audit #25 A07-11: the line read "missing" for both).
        prov = sum(1 for m in out["dsr_missing"] if m.get("status") == "provisional")
        split = (f" ({len(out['dsr_missing']) - prov} missing, {prov} still provisional — sent without its sales "
                 "and never final)") if prov else ""
        problems.append(f"Daily Sales Report missing past its deadline for {len(out['dsr_missing'])} "
                        f"restaurant{'s' if len(out['dsr_missing']) != 1 else ''}{split}: {names}.")
    if out["messaging"]:
        warn_kinds.add("messaging")
    problems.extend(f"Messaging: {m}" for m in out["messaging"])
    try:
        out["schedule_generation"] = schedule_generation_latency(db_path)
    except Exception as e:
        log.warning(f"schedule generation timings unavailable to the platform check: {e}")
    gen = out["schedule_generation"] or {}
    if gen.get("slow"):
        warn_kinds.add("schedule_slow")
        problems.append(f"Schedule generations are slow: the p95 of the last {gen['n']} is "
                        f"{gen['p95'] / 60:.1f} minutes (alert past {SCHEDULE_P95_ALERT_SECONDS // 60}), the "
                        f"slowest {gen['max'] / 60:.1f} — the stage timings are on each week "
                        "(schedule_history.stage_seconds_json).")
    try:
        out["schedule_inputs"] = schedule_input_failures(db_path)
    except Exception as e:
        log.warning(f"schedule input failures unavailable to the platform check: {e}")
    gin = out["schedule_inputs"] or {}
    if gin.get("over"):
        warn_kinds.add("schedule_inputs")
        problems.append("Schedule generations are being made without inputs they read: "
                        + ", ".join(f"{k} ({int(round(gin['rates'][k] * 100))}% of the last {gin['n']})"
                                    for k in gin["over"][:4])
                        + " — each failure is in the job failures (schedule_inputs).")
    try:
        out["schedule_capped"] = schedule_solve_capped(db_path)
    except Exception as e:
        log.warning(f"schedule solve caps unavailable to the platform check: {e}")
    gcap = out.get("schedule_capped") or {}
    if gcap.get("over"):
        warn_kinds.add("schedule_capped")
        # What the slots are sized from (scale audit 10/19/26 A5-F6): solver
        # threads past the cores is the usual cause of a wall-clock cut.
        sizing = ""
        try:
            import job_queue as _jq_cap
            cap = _jq_cap.generation_capacity()
            sizing = (f" Sizing: {cap['slots']} generation slots x {cap['solve_threads']} solver threads on "
                      f"{cap['cores']} cores" + (" — more threads than cores." if cap["oversubscribed"] else "."))
        except Exception as e:
            log.warning(f"generation capacity unavailable to the platform check: {e}")
        problems.append(f"Schedule solves are being cut short: {gcap['capped']} of the last {gcap['n']} generations "
                        f"({int(round(gcap['rate'] * 100))}%) had a solve ended by the wall clock or the memory "
                        "bound instead of its work budget ("
                        + ", ".join(f"{k} {v}" for k, v in list(gcap["kinds"].items())[:4])
                        + ") — lower QUEUE_GENERATION_CONCURRENCY or raise SCHEDULE_CPSAT_MEMORY_MB." + sizing)
    # The durable job queue's wait (scheduler audit 10/10/26 SCALE-21): an
    # owner's press waiting longer than QUEUE_WAIT_ALERT_SECONDS to start
    # (p95 over the last hour) is the trigger to move the queue to its own
    # worker and add slots — no request-latency figure sees it.
    try:
        import job_queue as _jq_sla
        out["queue"] = _jq_sla.stats(dead_letters=send)
    except Exception as e:
        log.warning(f"the job queue's stats unavailable to the platform check: {e}")
    q = out["queue"] or {}
    if (q.get("wait_p95_seconds") or 0) > QUEUE_WAIT_ALERT_SECONDS and (q.get("wait_samples") or 0) >= 5:
        warn_kinds.add("queue_wait")
        problems.append(f"Owners' schedule generations wait {q['wait_p95_seconds'] / 60:.1f} minutes to start "
                        f"(p95, last hour; alert past {QUEUE_WAIT_ALERT_SECONDS // 60}) — check the queue worker "
                        "process is running (QUEUE_WORKER=1, the default) or raise QUEUE_GENERATION_CONCURRENCY "
                        "(docs/plans/POSTGRES_AND_WORKERS_PLAN.md, When).")
    # The queue worker alive and the bulk / POS backlog (scale audit 10/19/26
    # INF-4, INF-6).
    for kind, sentence in queue_problems(q):
        if kind.startswith("queue_failed_urgent:"):
            out["queue_urgent"] = True      # an owner's press or the auto-draft: _has_urgent
            out.setdefault("queue_urgent_kinds", []).append(kind)
        else:
            warn_kinds.add(kind)
        problems.append(sentence)
    # Sweeps their bound cuts short every pass (INF-7): coverage, not
    # success. Read where the supervisor pages (send) — kept ten minutes.
    if send:
        out["bound_capped"] = jobs_bound_capped(db_path=db_path)
    for b in out["bound_capped"]:
        warn_kinds.add("bound_capped")
        reach = (f" (the last reached {b['attempted']} of {b['eligible']})"
                 if b.get("eligible") and b.get("attempted") is not None else "")
        problems.append(f"{b['job']} hit its time bound on each of its last {b['runs']} runs{reach} — it no "
                        "longer covers every restaurant each period.")
    # Jobs failing most of what they attempt, or attempting nothing while
    # restaurants are eligible (Audit #25 A07-03): partial runs read as
    # successes, so neither the SLA nor any other rule saw them.
    if send:
        out["jobs_failing"] = jobs_failing(db_path=db_path)
    for f in out["jobs_failing"]:
        warn_kinds.add(f"job_failing:{f['job']}")
        if f["why"] == "idle":
            problems.append(f"{f['job']} attempted nothing on each of its last {f['runs']} runs while "
                            f"{f.get('eligible')} restaurants were eligible — its per-restaurant work is not running.")
        else:
            counts = (f" (the last: {f['failed']} of {f['attempted']})"
                      if f.get("attempted") not in (None, "") and f.get("failed") is not None else "")
            problems.append(f"{f['job']} failed at least {int(FAILING_ALERT_SHARE * 100)}% of what it attempted on "
                            f"each of its last {f['runs']} runs{counts} — the errors are in the job failures.")
    # The AI's output quality, the schedule's fallbacks, DSR nights stuck
    # provisional and the weekly smoke eval (Audit #25 A07-04, A07-11,
    # A07-06): each a warning, read only where the supervisor pages.
    if send:
        try:
            import ai_quality_watch as _aqw
            out["ai_watch"] = _aqw.platform_problems(db_path=db_path)
        except Exception as e:
            log.warning(f"the AI quality watch unavailable to the platform check: {e}")
    for w in out["ai_watch"]:
        warn_kinds.add(w["key"])
        problems.append(w["line"])
    # Customer requests failing or slow (scale audit 10/19/26 OPS-7), from the
    # persisted per-minute rollups: a 5xx rate over the last minutes, and a
    # p95 over the SLO held for a quarter of an hour.
    try:
        import platform_monitor as _pm_sla
        out["requests"] = _pm_sla.request_slo(db_path=db_path, fresh=send)
    except Exception as e:
        log.warning(f"request rollups unavailable to the platform check: {e}")
    rq = out["requests"] or {}
    if rq.get("error_rate_over"):
        problems.append(f"{rq['errors_5xx']} of {rq['requests']} customer requests failed with a 5xx in the last "
                        f"{rq['error_minutes']} minutes ({rq['error_rate'] * 100:.1f}%, alert past "
                        f"{rq['error_rate_alert'] * 100:g}%) — the routes are in the console's 5xx samples.")
    if rq.get("error_floor_over") and not rq.get("error_rate_over"):
        # The absolute floor (release-safety audit 10/26/26 A07-10): the rate
        # rule's request floor is never reached at quiet hours.
        problems.append(f"{rq['floor_errors']} customer requests failed with a 5xx on "
                        f"{len(rq['floor_routes'])} routes in the last {rq['floor_minutes']} minutes "
                        f"({', '.join(rq['floor_routes'][:4])}) — the 5xx samples are in the console.")
    if rq.get("fresh_5xx_routes"):
        warn_kinds.add("new_5xx_route")
        problems.append(f"A 5xx since the last boot on route(s) with none in the 7 days before it: "
                        f"{', '.join(rq['fresh_5xx_routes'][:4])} — likely the release; hold the next deploy.")
    if rq.get("latency_over"):
        warn_kinds.add("latency")
        problems.append(f"Customer requests are slow: p95 within {rq['p95_le_ms']} ms over the last "
                        f"{rq['latency_minutes']} minutes (SLO {rq['p95_slo_ms']} ms, {rq['latency_requests']} "
                        "requests) — the slow routes are in the console's request rollups.")
    # The iPhone app failing to read the server's answers (client_compat,
    # release-safety audit #25 A06-04): past CLIENT_DECODE_ALERT_DEVICES
    # distinct devices in an hour, a response changed shape under a shipped
    # build. A warning keyed on the routes, so a new route breaking pages again.
    out["client_decode"] = None
    try:
        import client_compat as _cc_sla
        out["client_decode"] = _cc_sla.decode_failure_surge(db_path=db_path)
        decode_line = _cc_sla.surge_problem(out["client_decode"])
        if decode_line:
            warn_kinds.add("client_decode:" + ",".join(r["route"] for r in out["client_decode"]["routes"][:4]))
            problems.append(decode_line)
    except Exception as e:
        log.warning(f"client decode reports unavailable to the platform check: {e}")
    # The fleet's POS (scale audit 10/19/26 OPS-6): a provider failing for
    # more than pos_health.FLEET_OUTAGE_SHARE of the connected stores is one
    # outage and one page (a warning: once a day for the set), never one
    # console issue per store; a breaker held open past its page threshold
    # pages once per opening (pos_io.page_if_long_open).
    # The RPOWER fleet rules (Audit #23, A6 section 3.6), evaluated here from
    # the rollup only (rpower_health.fleet_rules): a data type failing or
    # stale across the fleet, an auth wave (urgent: a partner token revoked
    # is one problem, not N reconnects — A4-16), archive gaps, the queue's
    # failed and expired rows per kind (A6-02), the request budget and volume
    # (A6-17). A rule without a page is the console's alone.
    out["rpower_rules"] = []
    if send:
        try:
            import rpower_health as _rh_sla
            out["rpower_rules"] = [r for r in _rh_sla.fleet_rules(db_path=db_path) if r.get("page")]
        except Exception as e:
            log.warning(f"the RPOWER fleet rules unavailable to the platform check: {e}")
    for rule in out["rpower_rules"]:
        if rule["page"] != "urgent":
            warn_kinds.add(rule["key"])
        problems.append(f"{rule['title']} — {rule['detail']}")
    rpower_fleet_said = any(r["key"].startswith(("rpower_failing:", "rpower_stale", "rpower_auth_wave"))
                            for r in out["rpower_rules"])
    try:
        import pos_health as _ph_sla
        # Read only when paging: /health's read-only call stays lean. The
        # restaurants' sync columns narrowly, RPOWER from its rollup (A6-16).
        fleet = (_ph_sla.fleet_state(db_path=db_path) if db_path else _ph_sla.fleet_state()) if send else {}
        out["pos_fleet"] = {p: {"connected": f["connected"], "failing": len(f["failing"]), "share": f["share"]}
                            for p, f in fleet.items()}
        for prov, f in fleet.items():
            if prov == "rpower" and rpower_fleet_said:
                continue        # the rollup's own rule says it, by data type
            if f["outage"]:
                warn_kinds.add(f"pos_fleet_{prov}")
                problems.append(f"{prov.upper()} sync failing for {len(f['failing'])} of {f['connected']} connected "
                                f"stores ({int(round(f['share'] * 100))}%) — one outage, not a store's own problem.")
        if send:
            import pos_io as _pio_sla
            _pio_sla.page_if_long_open("rpower")
    except Exception as e:
        log.warning(f"the fleet POS reading unavailable to the platform check: {e}")
    out["problems"] = problems
    if not send:
        return out
    try:
        import scheduler as _sched
        if not _sched.scheduling_allowed():
            return out
    except Exception:
        return out
    # Each condition its own pager incident, and the ones that cleared
    # resolved — before the "nothing wrong" return, so the last one clearing
    # resolves too (re-audit B7 P2).
    conds = _sla_conditions(out, state, warn_kinds)
    out["conditions"] = sorted(conds)
    try:
        out["pager_resolved"] = pager_resolve_cleared(set(conds))
    except Exception as e:
        log.warning(f"cleared pager conditions not resolved: {e}")
    if not problems:
        return out
    events = [(k, f"Cavnar AI: {k[len(PAGER_CONDITION_PREFIX):]}", sev) for k, sev in sorted(conds.items())]
    # An urgent problem pages hourly. Only warnings: once a day for the same
    # set, sooner when a new kind joins it (the key names the set). Jobs
    # overdue, and nothing worse: once per the shortest overdue job's own
    # interval, between an hour and a day, keyed on the set - a weekly job
    # that missed its Sunday texted, emailed and pushed every hour (owner,
    # 10/19/26); a new job joining the set pages at once.
    urgent = _has_urgent(out, state, write_ok=out["write_ok"])
    core = _has_urgent(dict(out, jobs_overdue=[]), state, write_ok=out["write_ok"])
    _page_local.sla_events = events
    try:
        if urgent and not core:
            overdue = out["jobs_overdue"]
            cd = int(max(60, min(24 * 60, min(float(j.get("max_hours") or 1) for j in overdue) * 60)))
            res = page_operator("platform_sla_overdue:" + ",".join(sorted(str(j["job"]) for j in overdue)),
                                "Cavnar AI: the platform needs you", problems, cooldown_minutes=cd)
        elif urgent:
            # Keyed on the urgent conditions, never one constant (re-audit
            # B7): a new urgent condition pages at once; the same set hourly.
            crit = sorted(k[len(PAGER_CONDITION_PREFIX):] for k, sev in conds.items() if sev == "critical")
            res = page_operator("platform_sla_alert:" + ",".join(crit), "Cavnar AI: the platform needs you",
                                problems)
        else:
            res = page_operator("platform_sla_warn:" + ",".join(sorted(warn_kinds)),
                                "Cavnar AI: the platform needs you", problems,
                                cooldown_minutes=PLATFORM_WARN_COOLDOWN_MINUTES)
    finally:
        _page_local.sla_events = None
    out["alerted"] = bool(res.get("sent"))
    return out


# ── paging the operator (#27, decision 8) ────────────────────────────────────

# When the claims table cannot be written (the case the page is about),
# the cooldown lives here instead — one page per key per cooldown per process.
_page_memory = {}
_page_lock = threading.Lock()
_page_lock_open = threading.Lock()
_page_local = threading.local()


def will_phone():
    """The operator's mobile for platform pages (WILL_PHONE), or ""."""
    return (os.getenv("WILL_PHONE") or "").strip()


def _env_list(name):
    return [x.strip() for x in (os.getenv(name) or "").split(",") if x.strip()]


def oncall_phones():
    """Every phone a platform page is texted to: WILL_PHONE, then each of
    OPS_ONCALL_PHONES (comma-separated) — a second responder (scale audit
    10/19/26 OPS-4: one person was the whole on-call). Deduplicated, in
    order."""
    out = []
    for p in [will_phone()] + _env_list("OPS_ONCALL_PHONES"):
        if p and p not in out:
            out.append(p)
    return out


def oncall_emails():
    """Every address a platform page is emailed to: config.will_email(),
    then each of OPS_ONCALL_EMAILS."""
    out = []
    for e in [config.will_email()] + _env_list("OPS_ONCALL_EMAILS"):
        e = (e or "").strip()
        if e and e.lower() not in [x.lower() for x in out]:
            out.append(e)
    return out


# A paging service (OPS-4): phone escalation, acknowledgement and a second
# on-call belong to a service built for them, not to a text that nobody
# acknowledges. Off until configured. OPS_PAGER_KIND=pagerduty sends a
# PagerDuty Events v2 trigger (OPS_PAGER_ROUTING_KEY, deduplicated on the
# page's key); OPS_PAGER_KIND=webhook (or just OPS_PAGER_WEBHOOK_URL) POSTs
# {key, subject, lines, source} to any service's incoming webhook (Better
# Stack, Opsgenie, ...).
PAGER_TIMEOUT = (5, 10)
PAGERDUTY_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"


def pager_configured():
    kind = (os.getenv("OPS_PAGER_KIND") or "").strip().lower()
    if kind == "pagerduty":
        return bool((os.getenv("OPS_PAGER_ROUTING_KEY") or "").strip())
    return bool((os.getenv("OPS_PAGER_WEBHOOK_URL") or "").strip())


def pager_wanted() -> bool:
    """Whether a paging service is MEANT to get every page: OPS_PAGER_KIND
    is set, or a webhook URL is. A page then always tries it (Audit #25
    A07-09) — set but missing its key, it is reported as a failed channel,
    never silently skipped."""
    return bool((os.getenv("OPS_PAGER_KIND") or "").strip() or (os.getenv("OPS_PAGER_WEBHOOK_URL") or "").strip())


def pager_misconfigured() -> str:
    """Why the paging service OPS_PAGER_KIND names cannot be reached, or ""."""
    if not pager_wanted() or pager_configured():
        return ""
    kind = (os.getenv("OPS_PAGER_KIND") or "").strip().lower()
    if kind == "pagerduty":
        return "OPS_PAGER_KIND=pagerduty but OPS_PAGER_ROUTING_KEY is not set"
    return f"OPS_PAGER_KIND={kind or '(unset)'} but OPS_PAGER_WEBHOOK_URL is not set"


def send_to_pager(key, subject, lines, action="trigger", severity="critical") -> bool:
    """One event to the configured paging service — `action` "trigger" or
    "resolve" (PagerDuty Events v2; a webhook gets it as `action`), deduped
    on `key`. True when it took it; never raises. A trigger of a platform
    condition (a "sla:" key) is remembered as open (pager_open), so the
    supervisor resolves it when the condition clears."""
    if not pager_configured():
        return False
    import requests
    kind = (os.getenv("OPS_PAGER_KIND") or "").strip().lower()
    try:
        if kind == "pagerduty":
            body = {"routing_key": os.getenv("OPS_PAGER_ROUTING_KEY").strip(), "event_action": action,
                    "dedup_key": f"cavnar:{key}"}
            if action == "trigger":
                body["payload"] = {"summary": str(subject)[:1000], "source": "cavnar-ai", "severity": severity,
                                   "custom_details": {"lines": [str(x)[:300] for x in (lines or [])[:12]]}}
            r = requests.post(PAGERDUTY_EVENTS_URL, json=body, timeout=PAGER_TIMEOUT)
        else:
            r = requests.post(os.getenv("OPS_PAGER_WEBHOOK_URL").strip(), json={
                "key": str(key), "subject": str(subject), "lines": [str(x) for x in (lines or [])[:12]],
                "source": "cavnar-ai", "action": action, "severity": severity}, timeout=PAGER_TIMEOUT)
        ok = 200 <= r.status_code < 300
    except Exception as e:
        log.error(f"pager send failed: {e}")
        return False
    if ok and str(key).startswith(PAGER_CONDITION_PREFIX):
        _pager_open_note(str(key), opened=(action == "trigger"))
    return ok


# The platform check's conditions reach the pager one dedup key each
# ("sla:<condition>"), triggered when a page goes out and RESOLVED when the
# condition clears (re-audit B7 P2, 10/27/26): every urgent page shared one
# constant key and nothing was ever resolved, so an incident left
# acknowledged swallowed every later urgent page. The open keys are kept in
# job_cursors (PAGER_OPEN_CURSOR), so a deploy does not forget them.
PAGER_CONDITION_PREFIX = "sla:"
PAGER_OPEN_CURSOR = "pager_open_conditions"


def _pager_open_read() -> dict:
    try:
        from models import get_conn
        conn = get_conn()
        try:
            row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (PAGER_OPEN_CURSOR,)).fetchone()
        finally:
            conn.close()
        v = json.loads(row[0]) if row and row[0] else {}
        return v if isinstance(v, dict) else {}
    except Exception as e:
        log.warning(f"open pager conditions unreadable: {e}")
        return {}


def _pager_open_note(key, opened=True) -> None:
    """Remember `key` as an open pager incident (or forget it)."""
    with _page_lock_open:
        cur = _pager_open_read()
        if opened:
            cur[key] = cur.get(key) or time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        else:
            cur.pop(key, None)
        try:
            from models import get_conn
            conn = get_conn()
            try:
                conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                             (PAGER_OPEN_CURSOR, json.dumps(cur)))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            log.warning(f"open pager conditions not kept: {e}")


def pager_resolve_cleared(active) -> list:
    """Resolve every open platform-condition incident whose condition is not
    in `active` (a set of "sla:" keys). Returns the keys resolved. Only
    where the scheduler may page (the caller checks); never raises."""
    if not pager_configured():
        return []
    done = []
    for key in sorted(set(_pager_open_read()) - set(active or ())):
        if send_to_pager(key, "Cleared", [], action="resolve"):
            done.append(key)
    return done


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


def page_operator(key, subject, lines, cooldown_minutes=PLATFORM_ALERT_COOLDOWN_MINUTES, pager_events=None) -> dict:
    """Page Will about `key` at most once per `cooldown_minutes`, over every
    channel alert_will has. The cooldown is claimed only AFTER a channel
    confirmed delivery (#27): it used to be spent first, so a page whose
    email and push both failed silenced the next hour of them. Returns
    alert_will's {sent, channels, error}, or {sent: False, reason:
    'cooldown'}. `pager_events` [(key, summary, severity)]: the paging
    service gets one event per condition instead of one for `key` (the
    platform check's "sla:" conditions, each resolved when it clears)."""
    if pager_events is None:
        # check_platform_sla hands its conditions over through the thread
        # (sla_events), so page_operator's signature — which tests and
        # callers stand in for — is unchanged for them.
        pager_events = getattr(_page_local, "sla_events", None)
    with _page_lock:
        if _cooldown_active(key, cooldown_minutes):
            return {"sent": False, "reason": "cooldown", "channels": {}}
        # The page's key reaches the pager's dedup through a thread-local, so
        # alert_will's (subject, lines) signature — which tests and callers
        # stand in for — is unchanged.
        _page_local.key = key
        _page_local.pager_events = pager_events
        try:
            got = alert_will(subject, lines)
        finally:
            _page_local.key = None
            _page_local.pager_events = None
        res = {"sent": bool(got), "channels": dict(getattr(got, "channels", None) or {}),
               "error": getattr(got, "error", None)}
        # Which channels were tried and did not take it (A07-09): a page that
        # went out by push alone, or with the pager refusing, says so.
        res["failed"] = sorted(k for k, v in res["channels"].items() if v is False)
        if res["failed"]:
            log.warning(f"operator page {key}: channels failed: {', '.join(res['failed'])}"
                        + (f" ({res['error']})" if res.get("error") else ""))
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


def alert_will(subject, lines, key=None):
    """Tell the on-call now, outside the scheduler: a text to every
    oncall_phones() number (WILL_PHONE first), an email to every
    oncall_emails() address, an event to the paging service when one is
    configured (send_to_pager, keyed on `key`), and a push to the admin
    logins' phones. Never an owner.

    The text is the independent channel (decision 8): the email rides the
    product's own Resend account and the push its own APNs, so an outage of
    either used to take the page down with it. A push is only queued here —
    it proves nothing — so only the text and the email count as delivered.

    Returns an AlertResult: truthy when a text, the email or the pager
    went out, with each channel's outcome in .channels.

    The paging service goes FIRST and is always tried when OPS_PAGER_KIND
    (or a webhook URL) is set (Audit #25 A07-09): it is the channel that
    needs none of the app's own keys, so neither a Twilio or Resend key
    revoked mid-rotation nor a failure building the text or the email can
    stand between a page and it. Set but missing its key, it is a failed
    channel with the reason, never silently skipped."""
    import html as _html
    channels, errors = {}, []
    lines = [str(x) for x in (lines or [])]
    if pager_wanted():
        why_not = pager_misconfigured()
        if why_not:
            channels["pager"] = False
            errors.append(f"pager: {why_not}")
            log.error(f"alert_will pager not reachable: {why_not}")
        else:
            try:
                events = getattr(_page_local, "pager_events", None) if key is None else None
                if events:
                    # One event per condition (its own incident, resolved
                    # when it clears): taken when every one was accepted.
                    channels["pager"] = all([bool(send_to_pager(k, summary, lines, severity=sev))
                                             for k, summary, sev in events])
                else:
                    channels["pager"] = bool(send_to_pager(key or getattr(_page_local, "key", None) or subject,
                                                           subject, lines))
            except Exception as e:          # send_to_pager never raises; belt and braces for the one channel
                channels["pager"] = False
                log.error(f"alert_will pager failed: {e}")
            if not channels["pager"]:
                errors.append("pager not accepted")
    phones = oncall_phones()
    if phones:
        body = (subject + ": " + " ".join(lines[:3]))[:320]
        texted = 0
        try:
            import notify as _notify
            for phone in phones:
                try:
                    if _notify.send_sms(phone, body, use_case="alert"):
                        texted += 1
                    else:
                        errors.append("sms not accepted")
                except Exception as e:
                    errors.append(f"sms: {e}")
                    log.error(f"alert_will sms failed: {e}")
        except Exception as e:
            errors.append(f"sms: {e}")
            log.error(f"alert_will sms unavailable: {e}")
        channels["sms"] = texted > 0
        if len(phones) > 1:
            channels["sms_reached"] = texted
    try:
        import emails as _emails
        b = _emails.BRAND
        body = "".join(f'<li style="margin:0 0 6px">{_html.escape(x)}</li>' for x in lines[:12])
        inner = (f'<p style="color:{b["strong"]};font-size:16px;font-weight:700;margin:0 0 12px">'
                 f'{_html.escape(subject)}</p><ul style="color:{b["body"]};font-size:14px;'
                 f'line-height:1.5;margin:0 0 0 18px">{body}</ul>')
        mailed = 0
        for addr in oncall_emails():
            res = _emails._send_branded(addr, subject, from_label="Cavnar AI Ops", email_type="ops_platform_alert",
                                        inner_html=inner)
            if getattr(res, "ok", False):
                mailed += 1
            else:
                errors.append(f"email: {getattr(res, 'error', None) or 'not accepted'}")
        channels["email"] = mailed > 0
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
        # The app opens its "Platform needs you" sheet on it (nav
        # admin/platform, iOS parity follow-up 10/7/26): the alert's own
        # lines and when it went out ride the payload, so the sheet says
        # what the page said. It landed on the owner Home ({"tab": "home"}).
        data = platform_alert_payload(subject, lines)
        queued = 0
        for rid, ids in by_rid.items():
            queued += int(_push.fire_push(rid, "platform_alert", subject, lines[0][:180] if lines else subject,
                                          data=dict(data), user_ids=ids) or 0)
        channels["push"] = queued > 0
    except Exception as e:
        channels["push"] = False
        log.error(f"alert_will push failed: {e}")
    return AlertResult(bool(channels.get("sms") or channels.get("email") or channels.get("pager")), channels,
                       "; ".join(errors)[:300] or None)


# What the "Platform needs you" sheet shows: a few lines, each clipped, so
# the payload stays far under APNs' 4 KB whatever a page's lines say.
PLATFORM_ALERT_PUSH_LINES = 6
PLATFORM_ALERT_PUSH_LINE_CHARS = 200


def platform_alert_payload(subject, lines, now=None) -> dict:
    """The push data of an operator page (alert_will): where it opens
    (`nav` admin/platform — the app's admin sheet), the subject, the first
    PLATFORM_ALERT_PUSH_LINES lines clipped, and when it went out
    (`alert_at`, UTC ISO with Z)."""
    import nav as _nav
    from datetime import datetime, timezone
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    clipped = [str(x)[:PLATFORM_ALERT_PUSH_LINE_CHARS] for x in (lines or []) if str(x).strip()]
    return {"nav": _nav.path("admin", "platform"), "subject": str(subject or "")[:160],
            "lines": clipped[:PLATFORM_ALERT_PUSH_LINES],
            "alert_at": when.strftime("%Y-%m-%dT%H:%M:%SZ")}


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


def failures_since(since, data_checks=None):
    """[{job, cnt, last_at, sample_error}] since `since` (UTC stamp), the
    sample being each job's NEWEST error — MAX(error) was the alphabetically
    last one. `data_checks`: None every kind; True only the data findings
    (kind 'data_quality'); False everything else (Audit #23 A6-04 — the
    digest lists the data checks apart from the failures)."""
    try:
        from models import get_conn
        conn = get_conn()
        try:
            cond = "" if data_checks is None else (
                " AND COALESCE({t}.kind,'job') {op} 'data_quality'".replace("{op}", "=" if data_checks else "<>"))
            rows = conn.execute(f"""
                SELECT f.job, COUNT(*) AS cnt, MAX(f.created_at) AS last_at,
                       (SELECT g.error FROM job_failures g WHERE g.job=f.job AND g.created_at >= ?{cond.format(t='g')}
                        ORDER BY g.id DESC LIMIT 1) AS sample_error
                FROM job_failures f WHERE f.created_at >= ?{cond.format(t='f')} GROUP BY f.job ORDER BY cnt DESC
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
        # The table and its one row are made at boot (init_ops — INF-18);
        # one UPDATE here.
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
        if not held and conn.execute("SELECT 1 FROM scheduler_lease WHERE id=1").fetchone() is None:
            # The row went missing (a hand-edited or restored database):
            # made once, then taken by the next call.
            conn.execute("INSERT OR IGNORE INTO scheduler_lease (id, owner, heartbeat_at) VALUES (1, NULL, NULL)")
            conn.commit()
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
    # The owner's redos and discarded drafts, and the one-tap "why" for a
    # big edit in the first weeks (schedule audit 10/3/26 L-26, L-35): the
    # learners read 24 weeks (schedule_versions.LEARN_DAYS); a year and a
    # month keeps last year's same weeks.
    "schedule_rejections": int(os.getenv("RETAIN_SCHED_REJECTIONS_DAYS", "400")),
    "schedule_edit_answers": int(os.getenv("RETAIN_SCHED_EDIT_ANSWERS_DAYS", "400")),
    # The scheduling memory's observation log (schedule_memory, schedule
    # audit 10/3/26 L-29): raw and temporary — the compact memory
    # (schedule_memory, one row per learned fact, kept) is what outlives it,
    # rebuilt every night while the log still holds its evidence.
    "schedule_observations": int(os.getenv("RETAIN_SCHED_OBSERVATIONS_DAYS", "400")),
    # Each week as the edit learners read it, kept (schedule audit 10/10/26
    # SCALE-33): a cache rewritten whenever its week changes, so a row older
    # than the learners' window is a week they no longer read.
    "schedule_learning_records": int(os.getenv("RETAIN_SCHED_LEARNING_RECORDS_DAYS", "168")),
    # The publish warnings a send went out past, by key (TRUST-28): the
    # override rate per kind reads 180 days of them.
    "schedule_publish_overrides": int(os.getenv("RETAIN_SCHED_PUBLISH_OVERRIDES_DAYS", "400")),
    # The owner's answers to the pre-publish suggestions (schedule re-audit
    # 10/11/26 TRUST-17): read for DISMISS_MAX_DAYS at most.
    "schedule_review_dismissals": int(os.getenv("RETAIN_SCHED_REVIEW_DISMISSALS_DAYS", "200")),
    # What made each draft (RM-4, AI-29): its commit, arms, row origins and
    # the learning ablation's two drafts — read until the week's scorecard
    # row is frozen; the frozen row keeps what was measured from it.
    "schedule_generation_tags": int(os.getenv("RETAIN_SCHED_GENERATION_TAGS_DAYS", "400")),
    # Each staff offer answer's client and minutes to answer (staff_pwa,
    # scheduler audit 10/10/26 MARKET-18): the admin's read is 90 days.
    "offer_response_platforms": int(os.getenv("RETAIN_OFFER_PLATFORMS_DAYS", "400")),
    # The Shift Quality weights' weekly shadow runs and promotions (LEARNING-9):
    # the newest promotion is what the owner can undo, and an undo holds for
    # eight weeks.
    "schedule_calibration_runs": int(os.getenv("RETAIN_SCHED_CALIBRATION_RUNS_DAYS", "400")),
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
    # The phone's Live Activities (push.py, iOS parity audit 10/7/26 #38, #61,
    # #94): one row per send, generation or night it was about, read only
    # while the thing is live; a token not re-registered in 60 days belongs
    # to a session that has long ended.
    "live_activity_runs": int(os.getenv("RETAIN_LIVE_ACTIVITY_RUNS_DAYS", "60")),
    "live_activity_tokens": int(os.getenv("RETAIN_LIVE_ACTIVITY_TOKENS_DAYS", "60")),
    "notification_opens": int(os.getenv("RETAIN_NOTIFICATION_OPENS_DAYS", "365")),
    # Registered by #72. The admin audit trail is long: a year and a month,
    # so last year's same month is still there to compare.
    "admin_events":       int(os.getenv("RETAIN_ADMIN_EVENTS_DAYS", "400")),
    # One row per restaurant per day; the trend reads months, a year covers
    # the same month last year.
    "data_health_daily":  int(os.getenv("RETAIN_DATA_HEALTH_DAILY_DAYS", "400")),
    # The RPOWER fleet's daily health row (rpower_health, Audit #23 A6-01):
    # one row a day, a year and a bit of trend.
    "rpower_fleet_health_daily": 400,
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
    # Tock's webhook deliveries kept raw (guest_hub): reprocessed once the
    # payload format is confirmed; the bookings they made are kept apart.
    "tock_events":        int(os.getenv("RETAIN_TOCK_EVENTS_DAYS", "90")),
    "push_outbox":        int(os.getenv("RETAIN_PUSH_OUTBOX_DAYS", "30")),
    # The schedule publish outbox (schedule_publish): who each Send told and
    # how; the share-status list reads a week's own rows.
    "schedule_notices":   int(os.getenv("RETAIN_SCHEDULE_NOTICES_DAYS", "180")),
    "webhook_outbox":     int(os.getenv("RETAIN_WEBHOOK_OUTBOX_DAYS", "30")),
    "morning_brief_deliveries": int(os.getenv("RETAIN_BRIEF_DELIVERIES_DAYS", "90")),
    "alert_storm_caps":   int(os.getenv("RETAIN_ALERT_STORM_CAPS_DAYS", "365")),
    # The sign-in record: 90 days, auth.LOGIN_HISTORY_RETENTION_DAYS, which
    # auth.prune_login_history also applies at boot. Its created_at index is
    # auth.AUTH_INDEXES' idx_login_history_created (INT-2), made at boot with
    # the table — after this module's own boot init, so it is not made here.
    "login_history":      int(os.getenv("RETAIN_LOGIN_HISTORY_DAYS", "90")),
    # People memory (memory audit 9/29/26). Every shift every person worked
    # for three years, then their per-quarter summary in person_quarters,
    # kept forever — shift_facts.rollup_quarters writes each quarter while
    # all of it is still here, and never rewrites one from what is left.
    # Attendance outcomes and the signals about a person (covers taken,
    # guests naming them) for two years, their quarterly counts the same
    # way (shift_facts.roll_all_quarters, the rollup declared below).
    # people, person_aliases, person_questions, person_merges, person_roles,
    # person_quarters and schedule_standing_patterns are kept forever.
    "shift_facts":        int(os.getenv("RETAIN_SHIFT_FACTS_DAYS", "1095")),
    # The ticket-level POS archive (pos_archive, 9/29/26): three years, the
    # history every people, table and price learner reads.
    "pos_tickets":        int(os.getenv("RETAIN_POS_ARCHIVE_DAYS", "1095")),
    # The line detail may be kept shorter than the rest of the archive
    # (scale audit 10/19/26 DB-02: ~0.2 GB a store-year of the ~0.3):
    # RETAIN_POS_LINES_DAYS, the archive's window unless set. Its rollup
    # (pos_item_daily, below) rolls every day before a line of it goes.
    "pos_ticket_lines":   int(os.getenv("RETAIN_POS_LINES_DAYS", os.getenv("RETAIN_POS_ARCHIVE_DAYS", "1095"))),
    "pos_punches":        int(os.getenv("RETAIN_POS_ARCHIVE_DAYS", "1095")),
    "pos_archive_days":   int(os.getenv("RETAIN_POS_ARCHIVE_DAYS", "1095")),
    "pos_payments":       int(os.getenv("RETAIN_POS_ARCHIVE_DAYS", "1095")),
    "pos_payouts":        int(os.getenv("RETAIN_POS_ARCHIVE_DAYS", "1095")),
    # Each archived night's sales by POS item category net of its check
    # discounts (bar.sales.derive_nights, 10/15/26): kept as long as the
    # archive it is derived from.
    "bar_sales_nights":   int(os.getenv("RETAIN_POS_ARCHIVE_DAYS", "1095")),
    # Audit #23 F7 (A7-13 / A7-07): the night's comps and voids by approver
    # (names) kept as long as the archive they come from; the RPOWER checks
    # matched to guests two years (the guest's usual order reads them).
    "pos_loss_daily":     int(os.getenv("RETAIN_POS_ARCHIVE_DAYS", "1095")),
    "guest_pos_visits":   int(os.getenv("RETAIN_GUEST_POS_VISITS_DAYS", "730")),
    # The lines by item and day (pos_archive.roll_item_days, DB-11 / DB-02):
    # about a tenth of the lines' rows, kept at least as long as the archive
    # so it outlives a shortened line window.
    "pos_item_daily":     int(os.getenv("RETAIN_POS_ITEM_DAILY_DAYS", os.getenv("RETAIN_POS_ARCHIVE_DAYS", "1095"))),
    # The POS request ledger (pos_io, scale audit 10/19/26 A3-19): hourly
    # counts per endpoint and store; the console reads a day, a quarter
    # keeps the trend RPOWER's limits will be argued from.
    "pos_request_ledger": int(os.getenv("RETAIN_POS_REQUEST_LEDGER_DAYS", "90")),
    # RPOWER audit #23 (A5-09 / A5-13, F5): the growing per-store tables that
    # had no entry. The nightly report's versions and its figures are kept
    # three years, as long as the archive they are built from (an owner's
    # report history older than that is the stated rule's to drop); the hourly
    # sales captures 400 days (their readers compare the last same weekdays);
    # the shared closeday answers 400 days (the close lag reads 14 nights).
    "dsr_reports":        int(os.getenv("RETAIN_DSR_REPORTS_DAYS", "1095")),
    "pos_intraday":       int(os.getenv("RETAIN_POS_INTRADAY_DAYS", "400")),
    "pos_closed_days":    int(os.getenv("RETAIN_POS_CLOSED_DAYS", "400")),
    # Requests per hour per lane (pos_capacity's budget utilisation): the
    # request ledger's quarter.
    "pos_lane_ledger":    int(os.getenv("RETAIN_POS_REQUEST_LEDGER_DAYS", "90")),
    # ...and its reads per business night (audit #23 A6-24), by when the
    # night was last read: a backfilled old night is kept as long as a new one.
    "pos_request_nights": int(os.getenv("RETAIN_POS_REQUEST_LEDGER_DAYS", "90")),
    # Task sheets (task_sheets.py): two years of who did which duty; proof
    # photos 90 days (the day view shows a photo only while it is kept).
    "task_assignments":      int(os.getenv("RETAIN_TASK_SHEET_DAYS", "730")),
    "task_line_completions": int(os.getenv("RETAIN_TASK_SHEET_DAYS", "730")),
    "task_signoffs":         int(os.getenv("RETAIN_TASK_SHEET_DAYS", "730")),
    "task_proof_media":      int(os.getenv("RETAIN_TASK_PHOTO_DAYS", "90")),
    "attendance_events":  int(os.getenv("RETAIN_ATTENDANCE_DAYS", "730")),
    "person_signals":     int(os.getenv("RETAIN_PERSON_SIGNALS_DAYS", "730")),
    # Cavnar AI's own reads and their claims (ai_reads, memory audit
    # 9/29/26): the raw text 13 months. Each closed quarter is summarised
    # into ai_read_summaries (never pruned) by the nightly learning job
    # long before its rows reach this — "what we said, what was done, what
    # happened" outlives the words. The claims' Historical Accuracy reads a
    # year (ai_reads.claims_record), inside this.
    "ai_reads":           int(os.getenv("RETAIN_AI_READS_DAYS", "400")),
    "ai_claims":          int(os.getenv("RETAIN_AI_CLAIMS_DAYS", "400")),
    # view_as_sessions is not here: auth.record_view_as_session deletes its
    # rows past two days whenever a view-as opens, and one pruner per table.
    # What left the owner's memory without the owner asking (a lane's budget,
    # a date passed, a retraction — models.ask_memory_archive, memory audit
    # 9/29/26 owner_lanes): shown in Account and restorable for a year and a
    # month. The live facts themselves are bounded by their lanes, not dates.
    "ask_memory_archive": int(os.getenv("RETAIN_ASK_MEMORY_ARCHIVE_DAYS", "400")),
    # The memory audit's recommendation ledgers (9/29/26, M1): one compact
    # row per restaurant, surface and day of what a ranking did (a year and
    # a month, so a model version can be compared with last year's); a
    # delegate's per-login silence, a month after it ended.
    "rec_rank_builds":    int(os.getenv("RETAIN_REC_RANK_BUILDS_DAYS", "400")),
    "rec_silences":       int(os.getenv("RETAIN_REC_SILENCES_DAYS", "30")),
    # Memory audit 9/29/26, workstream M6: a reply draft the owner turned
    # down, as a hash and signals (never its words) — the drafter's edit
    # note reads the last 90 days (models.REJECTIONS_KEEP_DAYS).
    "reply_draft_rejections": int(os.getenv("RETAIN_REPLY_REJECTIONS_DAYS", "90")),
    # Marketing copy a model drafted (marketing_voice.DRAFTS_KEEP_DAYS): what
    # went out keeps its original on marketing_edits; the rest is only
    # needed to see which drafts were regenerated rather than used.
    "marketing_model_drafts": int(os.getenv("RETAIN_MKT_MODEL_DRAFTS_DAYS", "90")),
    # Memory re-audit 9/29/26 (FORGET-15, INVENTORY-7): what an evicted Ask
    # chat was about (its title — the owner's own first question — and the
    # model's notes) and each answer's rating with its free-text note were
    # kept forever, though nothing reads either past 400 days
    # (read_past_conversations) or 365 (the console's AI-quality range). The
    # monthly helpful rate outlives the ratings in learning_scorecards (kept
    # forever, frozen a week after each month closes).
    "ask_topics":         int(os.getenv("RETAIN_ASK_TOPICS_DAYS", "400")),
    "ask_feedback":       int(os.getenv("RETAIN_ASK_FEEDBACK_DAYS", "400")),
    # The staff app's ledgers (employee audit fix round, 10/2/26). What staff
    # and managers said to each other — announcements, who got and read each,
    # the message threads' messages, running-late reports (they mirror
    # attendance_events), offers of an open shift — two years. A recipient
    # row goes before its announcement, and an announcement only once no
    # recipient row is left (_RETENTION_ONLY), so the FK never refuses a
    # prune. The threads themselves (one per login) are bounded, not pruned.
    # Operational records a year: the after-shift pulse (its notes are the
    # person's own words; the owner's summary reads 90 days at most), each
    # day's staff brief, revoked calendar-feed links (a live one has no
    # revoked_at, so it never matches). Floor sections per shift two years
    # (the shift history they sit beside is kept longer). The translation
    # cache 90 days: a miss only costs one model call. staff_notices,
    # staff_otp_sends and staff_pin_resets prune themselves
    # (staff_reminders._prune, auth) — one pruner per table.
    "staff_announcement_recipients": int(os.getenv("RETAIN_STAFF_COMMS_DAYS", "730")),
    "staff_announcements":   int(os.getenv("RETAIN_STAFF_COMMS_DAYS", "730")),
    "staff_thread_messages": int(os.getenv("RETAIN_STAFF_COMMS_DAYS", "730")),
    "staff_running_late":    int(os.getenv("RETAIN_STAFF_COMMS_DAYS", "730")),
    "shift_offers":          int(os.getenv("RETAIN_STAFF_COMMS_DAYS", "730")),
    # An employee's availability cut and its answer (availability_changes,
    # 10/10/26 MARKET-15): what they asked and when it took effect.
    "staff_availability_changes": int(os.getenv("RETAIN_STAFF_COMMS_DAYS", "730")),
    # A shift at another location offered to someone who travels (cross_site,
    # 10/10/26 MARKET-16): kept like the offers here.
    "cross_site_offers": int(os.getenv("RETAIN_STAFF_COMMS_DAYS", "730")),
    # The schedule on the floor (J2, 10/10/26): each push of a week to the POS
    # clock and its shifts' statuses; the live labor moves and their answers
    # (autopilot earns on 60 days of them); the kept live read (a cache of
    # today's — a miss reads again).
    "pos_schedule_pushes": int(os.getenv("RETAIN_POS_PUSH_DAYS", "400")),
    "pos_schedule_push_shifts": int(os.getenv("RETAIN_POS_PUSH_DAYS", "400")),
    "live_labor_moves": int(os.getenv("RETAIN_LIVE_LABOR_DAYS", "400")),
    "live_labor_reads": int(os.getenv("RETAIN_LIVE_LABOR_READS_DAYS", "30")),
    "staff_shift_pulse":     int(os.getenv("RETAIN_STAFF_OPS_DAYS", "365")),
    "staff_briefs":          int(os.getenv("RETAIN_STAFF_OPS_DAYS", "365")),
    "staff_calendar_links":  int(os.getenv("RETAIN_STAFF_OPS_DAYS", "365")),
    "shift_sections":        int(os.getenv("RETAIN_SHIFT_SECTIONS_DAYS", "730")),
    "staff_translations":    int(os.getenv("RETAIN_STAFF_TRANSLATIONS_DAYS", "90")),
    # The website's daily figures (web_analytics): two years, so a year-on-year
    # comparison stays possible.
    "web_analytics_daily": int(os.getenv("RETAIN_WEB_ANALYTICS_DAYS", "800")),
    # Every schedule call's full input and answer (schedule_output, schedule
    # audit 10/3/26 PR-31): a quarter of real weeks to replay a model, effort
    # or prompt change against (scripts/schedule_model_eval.py). 90 days, not
    # 180 (AI cost audit 10/7/26 #94): its readers read 60, a replay wants a
    # quarter (the floor), and each row carries a whole compressed request —
    # the second half-year was volume nothing read.
    "schedule_model_calls": int(os.getenv("RETAIN_SCHEDULE_MODEL_CALLS_DAYS", "90")),
    # One row a schedule generation (schedule_metrics — scheduler audit
    # 10/10/26 AI-2, AI-23, RM-1): its cost, the model rows' survival, the
    # stage attribution and the post-model context. A year, so cost per
    # accepted week reads across seasons; the eval copies what it freezes.
    "schedule_generation_metrics": int(os.getenv("RETAIN_SCHEDULE_METRICS_DAYS", "365")),
    # What each saved week was checked and scored against (schedule_output.
    # record_check_snapshot, schedule audit 10/10/26 ARCH-35): the other
    # half of a replay, kept as long as the calls it replays with.
    "schedule_check_snapshots": int(os.getenv("RETAIN_SCHEDULE_MODEL_CALLS_DAYS", "90")),
    # The durable job queue (job_queue — scheduler audit 10/10/26 RM-7):
    # a finished row is read by the Jobs page and the queue-wait p95 (an hour);
    # two weeks reads what a week of passes did. The generation watchers are
    # taken when their draft lands; a row left is a job that never finished.
    "jobs": int(os.getenv("RETAIN_JOBS_DAYS", "14")),
    # The Postgres shadow's outbox (pg_shadow, RM-13): a sent row is done.
    "pg_shadow_outbox": int(os.getenv("RETAIN_PG_SHADOW_OUTBOX_DAYS", "30")),
    "generation_watchers": int(os.getenv("RETAIN_GENERATION_WATCHERS_DAYS", "7")),
    # The Message Batches queue (ai_batches, AI cost audit 10/7/26 #19): a
    # batch answers within 24 hours and every answer is in ai_usage and
    # ai_calls by then — a month is enough to read what a batch did.
    "ai_batch_jobs":      int(os.getenv("RETAIN_AI_BATCHES_DAYS", "30")),
    "ai_batch_items":     int(os.getenv("RETAIN_AI_BATCHES_DAYS", "30")),
    # The Places Details cache (ai_utils.places_request, AI cost audit
    # 10/7/26 #13): only today's rows are ever read; Google allows Places
    # content to be cached for 30 days at most.
    "places_details_cache": int(os.getenv("RETAIN_PLACES_CACHE_DAYS", "3")),
    # The orchestration ledgers (ai_orchestrator, AI orchestration design
    # 10/7/26): a run row per workflow output, its outcome filed days later
    # and the learner reading 28 days; the kept requests a shadow replay
    # reads soon or never (redacted, compressed); the route recommendations
    # the console decides on. ai_route_overrides is configuration, never pruned.
    "ai_runs":            int(os.getenv("RETAIN_AI_RUNS_DAYS", "180")),
    "ai_run_requests":    int(os.getenv("RETAIN_AI_RUN_REQUESTS_DAYS", "30")),
    "ai_route_recommendations": int(os.getenv("RETAIN_AI_ROUTE_RECS_DAYS", "365")),
    # Each scored run of an AI evaluation set (ai_eval.record_run, Audit #25
    # R13): aggregate scores, a few rows a week from the smoke eval. The
    # sets themselves (ai_eval_items) are never pruned — a baseline is only
    # comparable on the set it was measured on.
    "ai_eval_runs":       int(os.getenv("RETAIN_AI_EVAL_RUNS_DAYS", "400")),
    # The send gate's verdicts on exact guest texts (ai_reviewer.gate_send,
    # re-audit 10/7/26 #8): a verdict stands GATE_VERDICT_DAYS (30), after
    # which the text is read again; a row past that is never read.
    "ai_gate_verdicts":   int(os.getenv("RETAIN_AI_GATE_VERDICTS_DAYS", "45")),
    # The Restaurant Context Manager's stored sections (restaurant_context,
    # Phase 2, 10/7/26): a cache, one row per restaurant, section and viewer
    # scope, rewritten on every new version — a row not rebuilt in a month
    # is a scope nobody reads any more (a login that left, a changed role).
    "context_sections":   int(os.getenv("RETAIN_CONTEXT_SECTIONS_DAYS", "30")),
    # The compliance records (schedule audit 10/10/26 RM-15, MARKET-4):
    # staff's written consents, the predictability-pay and rest-premium
    # ledger, good-faith estimates, the written answers to change requests
    # and the access-to-hours offers — three years, what Chicago, New York
    # City, Seattle, San Francisco, Oregon, Evanston, Berkeley and
    # Emeryville require scheduling records be kept (compliance_packs
    # records_years). The floor is the same: never shorter.
    "staff_consents":        int(os.getenv("RETAIN_COMPLIANCE_RECORDS_DAYS", "1095")),
    "compliance_pay_ledger": int(os.getenv("RETAIN_COMPLIANCE_RECORDS_DAYS", "1095")),
    "good_faith_estimates":  int(os.getenv("RETAIN_COMPLIANCE_RECORDS_DAYS", "1095")),
    "fww_requests":          int(os.getenv("RETAIN_COMPLIANCE_RECORDS_DAYS", "1095")),
    "fww_access_offers":     int(os.getenv("RETAIN_COMPLIANCE_RECORDS_DAYS", "1095")),
    # Received mail (email_intake, bar fix round 10/15/26 R24): one row per
    # email to the receiving domain — spam to no address included, with its
    # sender and subject. 180 days; `no_route` rows go at 30 (the special
    # "email_intake_unrouted" below).
    "email_intake":          int(os.getenv("RETAIN_EMAIL_INTAKE_DAYS", "180")),
    # The iPhone app's decode-failure reports (client_compat, audit #25
    # A06-04): the platform check reads the last hour, the console a week.
    "client_error_reports":  int(os.getenv("RETAIN_CLIENT_ERROR_REPORTS_DAYS", "30")),
}

# Three years: how long a restaurant a predictive-scheduling law covers keeps
# its scheduling records (compliance_packs records_years). The staff
# communications, offers and attendance below are kept two years for
# everyone; a restaurant under such a law keeps them three (`_records_held`,
# MARKET-4) — a chain on Cavnar AI used to lose them a year early.
RECORDS_HOLD_DAYS = 1095
_RETENTION_HOLD_TABLES = frozenset({"attendance_events", "shift_offers", "staff_announcements",
                                    "staff_announcement_recipients", "staff_thread_messages", "staff_running_late"})

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
    "live_activity_runs": "created_at", "live_activity_tokens": "updated_at",
    "admin_events": "created_at", "data_health_daily": "created_at", "stripe_events_seen": "seen_at",
    "rpower_fleet_health_daily": "date",
    "sessions": "expires_at", "rec_events": "at",
    "operator_alerts": "created_at", "backup_runs": "started_at", "job_run_requests": "requested_at",
    "missed_windows": "created_at",
    "value_figures_daily": "date", "admin_issue_resolution_history": "created_at", "sms_log": "created_at",
    "tock_events": "received_at",
    "push_outbox": "created_at", "webhook_outbox": "created_at", "morning_brief_deliveries": "created_at",
    "schedule_notices": "created_at",
    "places_details_cache": "created_at",
    "alert_storm_caps": "started_at", "login_history": "created_at",
    "shift_facts": "business_date", "attendance_events": "business_date", "person_signals": "signal_date",
    "pos_tickets": "business_date", "pos_ticket_lines": "business_date", "pos_punches": "business_date",
    "pos_request_ledger": "hour",
    "dsr_reports": "business_date", "pos_intraday": "business_date",
    "pos_closed_days": "business_date", "pos_lane_ledger": "hour", "pos_request_nights": "last_at",
    "pos_archive_days": "business_date", "pos_payments": "business_date", "pos_payouts": "business_date",
    "bar_sales_nights": "business_date", "pos_item_daily": "business_date",
    "pos_loss_daily": "business_date", "guest_pos_visits": "business_date",
    "task_assignments": "task_date", "task_line_completions": "completed_at", "task_signoffs": "task_date",
    "task_proof_media": "created_at",
    "ai_reads": "created_at", "ai_claims": "created_at",
    "ask_memory_archive": "archived_at",
    "rec_rank_builds": "built_at", "rec_silences": "until",
    "reply_draft_rejections": "created_at", "marketing_model_drafts": "created_at",
    "ask_topics": "created_at", "ask_feedback": "created_at",
    "staff_announcement_recipients": "created_at", "staff_announcements": "created_at",
    "staff_thread_messages": "created_at", "staff_running_late": "business_date", "shift_offers": "created_at",
    "staff_availability_changes": "created_at", "cross_site_offers": "created_at",
    "pos_schedule_pushes": "created_at", "pos_schedule_push_shifts": "created_at",
    "live_labor_moves": "business_date", "live_labor_reads": "business_date",
    "staff_shift_pulse": "business_date", "staff_briefs": "business_date", "staff_calendar_links": "revoked_at",
    "shift_sections": "date", "staff_translations": "created_at",
    "web_analytics_daily": "day",
    "schedule_model_calls": "created_at", "schedule_generation_metrics": "created_at",
    "schedule_check_snapshots": "created_at", "jobs": "created_at", "generation_watchers": "created_at",
    "pg_shadow_outbox": "created_at",
    "ai_batch_jobs": "submitted_at", "ai_batch_items": "created_at",
    "ai_runs": "created_at", "ai_run_requests": "created_at", "ai_route_recommendations": "created_at",
    "ai_gate_verdicts": "created_at",
    "context_sections": "built_at",
    "schedule_rejections": "created_at", "schedule_edit_answers": "asked_at",
    "schedule_observations": "created_at",
    "staff_consents": "created_at", "compliance_pay_ledger": "created_at", "good_faith_estimates": "created_at",
    "fww_requests": "created_at", "fww_access_offers": "created_at",
    "schedule_publish_overrides": "shown_at",
    "schedule_review_dismissals": "dismissed_at",
    "schedule_calibration_runs": "run_at",
    "offer_response_platforms": "answered_at",
    "email_intake": "received_at",
    "client_error_reports": "created_at",
}
# Rows a table's retention never deletes, whatever their age: the owner's
# ANSWERS to recommendations are kept for good (memory audit 9/29/26,
# "silences": "keep every answer forever; only the silence changes") —
# rec_events' showings, opens and lifecycle rows go at their age.
_RETENTION_ONLY = {
    "rec_events": "event IN ('shown', 'opened', 'evidence_viewed', 'superseded', 'expired')",
    # An account holder's rule that a full lane pushed out is kept until
    # someone dismisses it in Account (owner_memory.dismiss) — never deleted
    # for age alone (memory re-audit 9/29/26, R3 lane_eviction).
    "ask_memory_archive": "NOT (reason='evicted' AND kind='constraint' "
                          "AND COALESCE(authority, '') NOT IN ('delegate', 'admin') "
                          "AND COALESCE(audience, 'team')!='author')",
    # An announcement goes once its recipient rows have (they are pruned on
    # the same window, first): the recipients' FK would refuse it otherwise.
    "staff_announcements": "NOT EXISTS (SELECT 1 FROM staff_announcement_recipients r "
                           "WHERE r.announcement_id = staff_announcements.id)",
}
# Every table above has an index on its column, created where the table is
# or at boot here (_ensure_retention_indexes, DATA-40): these deletes run
# under the write lock, and a full scan of a year of email_log there stalls
# every request that writes. tests/test_edge_data_claims_and_lease.py pins it.

# ── the rest of each table's declaration (memory audit 9/29/26) ────────────
#
# The days and the stamp column above are two of five fields. A window was
# whatever a RETAIN_* variable said, so RETAIN_ALERT_LOG_DAYS=18 — one typo
# — would have deleted every restaurant's alert history overnight, gone for
# good once the 14-day backups rolled over; and two readers reached past
# their table's window (inventory_history under the seasonal food-cost
# re-check, alert_log under "since you started"). So each table also has:
#
#   _RETENTION_FLOOR_DAYS  the least its window may be set to. A RETAIN_*
#                          value below it is REFUSED: the table is not
#                          pruned that night, the run is partial, and the
#                          operator is paged (retention_state / prune_ledgers).
#                          0 still disables a table's prune (keep forever).
#   _RETENTION_ROLLUP      the summary written before its rows are deleted
#                          ("module:function"); a rollup that fails keeps
#                          that night's rows.
#   _RETENTION_READERS     its readers and the longest window each reads —
#                          days, "module.CONSTANT", or None for a lifetime
#                          reader, which must read the rollup table named
#                          beside it. tests/test_mem_m7_retention.py fails
#                          when a window does not fit inside the floor, or
#                          a registered table has no floor.
#
# and every pass deletes at most RETENTION_PASS_MAX_ROWS rows per table: a
# table that has more past its window is finished on the following nights
# (reported, not paged) — a wrong clock or a mis-stamped column cannot
# empty a table in one night.
RETENTION_FLOOR_DEFAULT_DAYS = 30
# Each floor is at least the longest window any declared reader reads
# (_RETENTION_READERS, checked by the test); where the default IS the
# floor, the window may be raised, never lowered.
_RETENTION_FLOOR_DAYS = {
    # The website (web_analytics): a year and a bit, so a year-on-year read stays possible.
    "web_analytics_daily": 400,
    "ai_usage": 90, "ai_validation_log": 90, "activity_log": 90, "job_runs": 45, "job_failures": 30,
    "push_deliveries": 30, "webhook_deliveries": 14, "alert_log": 90, "email_log": 180,
    "ai_visibility_query_runs": 90, "competitor_snapshots": 365, "ai_visibility_runs": 180,
    "schedule_recommendation_events": 180, "job_period_claims": 35, "alert_holds": 7,
    # The schedule learners read 24 weeks of redos and answers (L-26, L-35);
    # the scheduling memory reads 24 weeks of its log and the weeks scored as
    # they ran for recommendations accepted up to 180 days ago (L-29, L-34).
    "schedule_rejections": 168, "schedule_edit_answers": 168, "schedule_observations": 200,
    # The kept week records (SCALE-33): the learners' own 24-week window.
    "schedule_learning_records": 168,
    "schedule_publish_overrides": 180,
    "schedule_review_dismissals": 180,
    "schedule_generation_tags": 120,
    "schedule_calibration_runs": 90,
    "offer_response_platforms": 90,
    "marketing_link_taps": 1, "notification_opens": 60, "admin_events": 400, "data_health_daily": 30,
    "stripe_events_seen": 7, "sessions": 1, "rec_events": 731, "operator_alerts": 30, "backup_runs": 60,
    "job_run_requests": 7, "missed_windows": 30, "value_figures_daily": 7,
    # The Places Details cache is read for its own day only (#13).
    "places_details_cache": 1,
    "admin_issue_resolution_history": 90, "sms_log": 30, "push_outbox": 7, "webhook_outbox": 7,
    "tock_events": 30,
    "rpower_fleet_health_daily": 30,
    # A sent week's delivery list (schedule_publish.notice_status) is read
    # while the week is current — a few weeks, never more than two months.
    "schedule_notices": 60,
    "morning_brief_deliveries": 30, "alert_storm_caps": 60, "login_history": 60,
    # Account -> Memory offers a year to restore a forgotten fact (M2's archive).
    "ask_memory_archive": 365,
    # The admin's rank-learning read compares up to a year (M1, rank_log); a
    # delegate's per-login silence is read only while it holds (M1,
    # who_answered).
    "rec_rank_builds": 365, "rec_silences": 1,
    # The people memory (M3): the overtime metric's seasonal re-check reads
    # the same weeks last year (392 days), mentoring a year, reliability
    # 360 days, a person's record its guest mentions a year; tenure reads
    # person_quarters for the rest.
    "shift_facts": 400, "attendance_events": 400, "person_signals": 400,
    "pos_tickets": 400, "pos_ticket_lines": 400, "pos_punches": 400, "pos_archive_days": 400,
    "pos_payments": 400, "pos_payouts": 400,
    # The console reads a day of the request ledger (pos_io.summary).
    "pos_request_ledger": 30,
    # The DSR's history and figures (canonical facts, trends, the seasonal
    # reads) reach a year and a month; the intraday baselines the same;
    # the close lag 14 nights.
    "dsr_reports": 400, "pos_intraday": 400, "pos_closed_days": 30, "pos_lane_ledger": 30,
    "pos_request_nights": 30,
    # The Bar's months table and trend (bar.pour_cost) read a year back.
    "bar_sales_nights": 400, "pos_item_daily": 400, "pos_loss_daily": 400, "guest_pos_visits": 365,
    # The consistency report reads up to 90 days (task_sheets.REPORT_MAX_DAYS).
    "task_assignments": 90, "task_line_completions": 90, "task_signoffs": 90, "task_proof_media": 30,
    # M6: the drafter's edit note reads 90 days of turned-down reply drafts
    # (models.REJECTIONS_KEEP_DAYS); the marketing voice reads 90 days of
    # model drafts to see which were regenerated (marketing_voice.DRAFTS_KEEP_DAYS).
    "reply_draft_rejections": 90, "marketing_model_drafts": 90,
    # Ask's past chats read up to 400 days (read_past_conversations); the
    # console's AI quality reads up to a year of ratings.
    "ask_topics": 400, "ask_feedback": 365,
    # The staff app (10/2/26): the inbox and threads read the newest 50/200
    # rows, running late and offers today's and the live ones, the pulse
    # summary 90 days at most, the brief one day, the translation cache a
    # lookup (a miss re-translates).
    "staff_announcement_recipients": 90, "staff_announcements": 90, "staff_thread_messages": 90,
    "staff_running_late": 30, "shift_offers": 30, "staff_shift_pulse": 90, "staff_briefs": 30,
    # An availability change is read while pending or approved and not yet
    # in force (the first week nobody has built, a few weeks ahead) and 30
    # days after.
    "staff_availability_changes": 90,
    # Offers at another location read while live; the POS pushes read per
    # week (the newest); live moves' autopilot trust reads EARN_WINDOW_DAYS
    # (60) of them; the live read is today's.
    "cross_site_offers": 30, "pos_schedule_pushes": 30, "pos_schedule_push_shifts": 30,
    "live_labor_moves": 120, "live_labor_reads": 7,
    # Floor sections: the scheduling memory learns each server's usual
    # section from 24 weeks of them (schedule audit 10/3/26 L-22).
    "staff_calendar_links": 30, "shift_sections": 168, "staff_translations": 7,


    # Cavnar AI's own reads and claims (M4, ai_reads): the claims' record and
    # the model-confidence check read a year; Ask reads up to 180 days of
    # reads. Every closed quarter is summarised first (the rollup below).
    "ai_reads": 365, "ai_claims": 365,
    # The schedule-call record: the measured tokens a row reads 60 days, and
    # a replay wants a quarter of real weeks at the least.
    "schedule_model_calls": 90, "schedule_check_snapshots": 90,
    # The queue's rows: job_queue.stats reads an hour; a week of passes.
    "jobs": 7, "generation_watchers": 2, "pg_shadow_outbox": 7,
    # schedule_metrics.summary reads 90 days of generations.
    "schedule_generation_metrics": 90,
    # Every answer is in ai_usage / ai_calls within a day; a week keeps a
    # batch's own record for the console's "what did the last pass do".
    "ai_batch_jobs": 7, "ai_batch_items": 7,
    # The learner reads 28 days of runs and the revert compares the 28 before
    # an override; a replay needs a fortnight of kept requests to sample.
    "ai_runs": 60, "ai_run_requests": 14, "ai_route_recommendations": 90, "ai_eval_runs": 90,
    # A send-gate verdict is read GATE_VERDICT_DAYS (30) after it was given.
    "ai_gate_verdicts": 30,
    # A cache: a pruned row is rebuilt on the next packet that needs it.
    "context_sections": 1,
    # A Live Activity lasts a night at most (a service) — a few days covers
    # the longest one; a live session re-registers its tokens on every open.
    "live_activity_runs": 7, "live_activity_tokens": 31,
    # The compliance records: three years, never less (RM-15, MARKET-4).
    "staff_consents": 1095, "compliance_pay_ledger": 1095, "good_faith_estimates": 1095,
    "fww_requests": 1095, "fww_access_offers": 1095,
    # The admin's Invoice intake card reads the newest 20 rows, whatever
    # their age; a quarter keeps a feed's history for a question about it.
    "email_intake": 90,
    # The platform check reads the last hour of decode reports.
    "client_error_reports": 7,
}
_RETENTION_ROLLUP = {
    "ai_usage": "ai_utils:rollup_usage",
    "ai_validation_log": "ai_utils:rollup_usage",
    "alert_log": "history_rollups:roll_alerts",
    "notification_opens": "history_rollups:roll_engagement",
    "login_history": "history_rollups:roll_engagement",
    "email_log": "history_rollups:stamp_newsletter_results",
    # Each person's quarter (shifts, hours, roles, dayparts, attendance
    # outcomes, covers taken, guest mentions) into person_quarters, kept
    # forever, while the quarter's rows are all still here (M3).
    "shift_facts": "shift_facts:roll_all_quarters",
    "attendance_events": "shift_facts:roll_all_quarters",
    "person_signals": "shift_facts:roll_all_quarters",
    # "What we said, what was done, what happened" per closed quarter
    # (ai_read_summaries, kept forever) before any raw read or claim goes.
    "ai_reads": "ai_reads:rollup_quarters",
    "ai_claims": "ai_reads:rollup_quarters",
    # Each archived day's lines by item (pos_item_daily) before any line of
    # the day goes (scale audit 10/19/26 DB-02); raises while a day past the
    # line window is not rolled yet.
    "pos_ticket_lines": "pos_archive:roll_item_days",
    # The request ledger's hourly rows become one row a day after a week
    # (pos_capacity.roll_ledger_daily; RPOWER audit #23 A5-13).
    "pos_request_ledger": "pos_capacity:roll_ledger_daily",
}
# (reader, window, what a lifetime reader reads instead). A window is days,
# or "module.CONSTANT" (the reader's own constant, read by the test), or
# None for a lifetime reader, which must name what it calls for the history
# the raw rows no longer hold (the name is checked in its source). The binding readers only — the
# longest windows and every lifetime reader (a mapped sweep, 9/29/26).
_RETENTION_READERS = {
    # Marketing's website card: a window and the one before it (Ask asks for up to 90 days).
    "web_analytics_daily": (("web_analytics.summary", 180, None),),
    "pos_tickets": (("service_performance.summary", "service_performance.MAX_DAYS", None),),
    "pos_ticket_lines": (("service_performance.summary", "service_performance.MAX_DAYS", None),),
    "pos_punches": (("service_performance.pay_and_tips", "service_performance.MAX_DAYS", None),),
    "pos_payments": (("service_performance.payments", "service_performance.MAX_DAYS", None),),
    "pos_payouts": (("service_performance.payments", "service_performance.MAX_DAYS", None),),
    "pos_intraday": (("intraday.pulse", "intraday.PROFILE_MAX_DAYS", None), ("labor_live._sales", 56, None)),
    "pos_closed_days": (("pos_closed.close_lag_minutes", "pos_closed.LAG_SAMPLE_NIGHTS", None),),
    "task_assignments": (("task_sheets.report", "task_sheets.REPORT_MAX_DAYS", None),),
    "task_line_completions": (("task_sheets.report", "task_sheets.REPORT_MAX_DAYS", None),),
    "task_signoffs": (("task_sheets.report", "task_sheets.REPORT_MAX_DAYS", None),),
    "ai_usage": (("ai_utils._spend_since", 31, None), ("admin_ops.ai_ops", 90, None)),
    "ai_validation_log": (("admin_ops.validation_rates", 90, None),),
    "activity_log": (("rec_trust.owner_changes", "rec_trust.CHANGES_LOOKBACK_DAYS", None),
                     ("admin_ops.adoption", 90, None), ("home_brief._build", 90, None)),
    "job_runs": (("admin_ops._job_rows", 45, None), ("ops._reclaim_dead_run", 31, None)),
    "job_failures": (("admin_ops.activity", 14, None), ("ops.failures_since", "ops.DIGEST_MAX_LOOKBACK_DAYS", None)),
    "push_deliveries": (("admin_ops.notifications", 30, None),),
    "alert_log": (("value_delivered.ledger", None, "alerts_lifetime"),
                  ("ask_cavnar_tools._read_alerts", 90, None),
                  ("notify.engagement_report", "notify.ENGAGEMENT_WINDOW_DAYS", None),
                  ("models.money_surfaced", 30, None)),
    "email_log": (("guest_email.newsletter_history", None, "results_stamped_at"),
                  ("models.onboarding_started_at", 180, None), ("guest_email.opens_tracked", 90, None)),
    "ai_visibility_query_runs": (("models.ai_visibility_query_history", 56, None),),
    "competitor_snapshots": (("models.competitor_movement", 365, None),),
    "ai_visibility_runs": (("models.get_ai_visibility_history", 70, None),),
    "schedule_recommendation_events": (
        ("schedule_intel.measure_accepted_recommendations", "schedule_intel.REC_ACCEPTED_LOOKBACK_DAYS", None),
        ("rec_ledger.sync_existing", 120, None),
        # A suppression is durable state now (rec_kind_states, memory audit
        # quiet_kinds): the log is read only for the re-test window.
        ("schedule_intel.suppression_state", "schedule_intel.SUPPRESS_REVIEW_DAYS", None)),
    "job_period_claims": (("ops.claim_period", 31, None),),
    "notification_opens": (("notify.engagement_report", "notify.ENGAGEMENT_WINDOW_DAYS", None),),
    "admin_events": (("admin_ops._load_with", 400, None),),
    "data_health_daily": (("admin_ops._load_with", 14, None),),
    "rpower_fleet_health_daily": (("rpower_health.fleet_trend", 30, None),),
    "rec_events": (("admin_ops.confidence_calibration", 730, None),
                   ("rec_learning.effectiveness", "rec_learning.EFFECT_WINDOW_DAYS", None)),
    "backup_runs": (("ops.storage_trend", 60, None),),
    "sms_log": (("admin_ops._load_with", 30, None),),
    "login_history": (("home_brief._build", 60, None),),
    "inventory_history": (("food_cost_intelligence.seasonal_baseline", 392, None),
                          ("cogs.build_food_cost_pct", 375, None),
                          # every food-cost reader's loader — the seasonal re-check
                          # (outcomes.expected_for → metrics → cogs) included
                          ("waste_trend.load_waste_history", None, "inventory_summary_weeks")),
    # Every schedule learner reads a week through schedule_versions.
    # learning_weeks: 24 weeks, weighted by half-life (schedule audit
    # 10/3/26 L-27), inside the intermediate versions' floor.
    "schedule_versions": (("schedule_versions.learning_weeks", "schedule_versions.LEARN_DAYS", None),
                          ("schedule_learning.edited_weeks", "schedule_versions.LEARN_DAYS", None),
                          ("schedule_learning.prediction_weeks", "schedule_versions.LEARN_DAYS", None)),
    "schedule_rejections": (("schedule_versions.learning_weeks", "schedule_versions.LEARN_DAYS", None),),
    "schedule_learning_records": (("schedule_versions.learning_weeks", "schedule_versions.LEARN_DAYS", None),),
    "schedule_publish_overrides": (("schedule_overrides.stats", "schedule_overrides.WINDOW_DAYS", None),),
    "schedule_review_dismissals": (("schedule_overrides.dismissals", "schedule_overrides.DISMISS_MAX_DAYS", None),),
    "schedule_generation_tags": (("schedule_scorecard.compute_week", "schedule_scorecard.TAGS_READ_DAYS", None),),
    "offer_response_platforms": (("staff_pwa.response_times", "staff_pwa.RESPONSE_DAYS", None),),
    "schedule_calibration_runs": (("schedule_calibration._quiet", "schedule_calibration.REVERT_QUIET_DAYS", None),),
    "schedule_edit_answers": (("schedule_versions.learning_weeks", "schedule_versions.LEARN_DAYS", None),),
    # The scheduling memory (L-29, L-22, L-34): the owner's redos over its
    # window; each recommendation's week as it ran, for one accepted as far
    # back as the measurement looks; a server's usual section.
    "schedule_observations": (("schedule_memory._learn_redos", "schedule_memory.WINDOW_DAYS", None),
                              ("schedule_intel.measure_recommendations_as_run",
                               "schedule_intel.AS_RUN_LOOKBACK_DAYS", None)),
    "shift_sections": (("schedule_memory._learn_sections", "schedule_memory.WINDOW_DAYS", None),),
    "ask_memory_archive": (("models.get_ask_memory_archive", 365, None),),
    "rec_rank_builds": (("admin_ops.rank_learning", 365, None),),
    "rec_silences": (("rec_ledger.silenced_keys", 1, None), ("rec_ledger.login_silences", 1, None)),
    # The people memory (M3). Every per-person reader names its window; the
    # one lifetime reader, tenure, reads the quarterly summaries.
    "shift_facts": (("metrics._overtime_hours", 392, None),              # same weeks last year (outcomes)
                    ("schedule_intel.mentoring", "schedule_intel.MENTOR_WINDOW_DAYS", None),
                    ("staff_settings.reliability", "staff_settings.RELIABILITY_WINDOW_DAYS", None),
                    ("shift_facts.tenure", None, "person_quarters")),
    # One weighted reader for every attendance consumer (schedule audit
    # 10/3/26 L-17): reliability, by-weekday, standby and the prompt's
    # no-show block all read staff_settings.attendance_events.
    "attendance_events": (("staff_settings.reliability", "staff_settings.RELIABILITY_WINDOW_DAYS", None),
                          ("staff_settings.attendance_events", "staff_settings.RELIABILITY_WINDOW_DAYS", None),
                          ("attendance.summary_lines", 84, None)),
    "person_signals": (("people.cover_record", 180, None),
                       ("people.get_person", "people.PERSON_MENTION_DAYS", None),
                       ("people.memory_lines", "people.MEMORY_MENTION_DAYS", None)),
    "reply_draft_rejections": (("models.get_reply_rejection_signals", "models.REJECTIONS_KEEP_DAYS", None),),
    "marketing_model_drafts": (("marketing_voice.regenerated", "marketing_voice.DRAFTS_KEEP_DAYS", None),
                               ("marketing_voice._match_draft", 1, None)),


    "ask_topics": (("ask_conversations.past_conversations", 400, None),
                   ("ask_conversations.often_asks", "ask_conversations.OFTEN_ASKS_DAYS", None)),
    "ask_feedback": (("models.ask_feedback_rows", 180, None), ("models.ask_feedback_summary", 90, None),
                     ("admin_ops.ai_quality", 365, None), ("learning_scorecard.compute_month", 60, None)),
    "ai_reads": (("ask_cavnar_tools._read_recent_reads", 180, None),
                 ("ai_reads.recent_reads", 30, None)),
    "ai_claims": (("ai_reads.claims_record", 365, None), ("ai_reads.confidence_calibration", 365, None),
                  ("ai_reads.claim_lines", "ai_reads.CLAIM_LOOKBACK_DAYS", None)),
    # The after-shift pulse: the owner's summary reads 90 days at most.
    "staff_shift_pulse": (("staff_insights.pulse_summary", 90, None),),
    # What a schedule row really costs in output tokens (P-35), from the
    # last 60 days of calls.
    "schedule_model_calls": (("schedule_output.measured_tokens_per_row", 60, None),
                             ("schedule_output.call_costs", 60, None)),
    "schedule_generation_metrics": (("schedule_metrics.summary", 90, None),),
    "client_error_reports": (("client_compat.decode_failure_surge", 1, None),),
}
# Readers that reach past their table's window today, each with the reason
# it is left for now — found by the mapped sweep (9/29/26) and listed so
# they are seen, not silently wrong. The test fails on any other; this list
# may only shrink.
_RETENTION_KNOWN_GAPS = {
    ("ai_usage", "admin_ops.ai_quality"): "the 365-day console range reads raw rows kept 120 days; "
                                          "ai_usage_daily holds the counts it needs",
    ("ai_usage", "admin_ops.vendor_costs"): "months past 120 days read raw AI cost; ai_usage_daily holds it",
    ("ai_usage", "ai_utils.usage_summary"): "/admin/ai-usage takes an uncapped ?days=",
    ("ai_validation_log", "admin_ops._client_validation"): "the 365-day range reads raw rows kept 120 days",
    ("sms_log", "admin_ops.vendor_costs"): "SMS cost past sms_log's 90 days is not on file",
    ("job_runs", "platform_monitor._last_run"): "the quarterly restore drill's last run is pruned at 45 days",
    ("alert_log", "scheduler.send_while_away_nudges"): "reads everything since the owner's last sign-in, uncapped",
    ("email_log", "client_api._do_upload_data"): "the 'first upload ever' check re-fires after 365 days",
    ("login_history", "admin_ops._load_with"): "team last-seen reads the staff sign-ins kept 90 days (the owners' "
                                               "last console sign-in falls back to engagement_monthly's months)",
}
# Past this many rows in one table in one pass, the rest waits a night —
# or past RETENTION_INFLOW_FACTOR times the rows that aged out in the last
# day, when that is more (_inflow_cap, scale audit 10/19/26 DB-03: a fixed
# cap under a table's daily inflow meant the table only ever grew).
RETENTION_PASS_MAX_ROWS = int(os.getenv("RETENTION_PASS_MAX_ROWS", "200000"))
RETENTION_INFLOW_FACTOR = int(os.getenv("RETENTION_INFLOW_FACTOR", "2"))

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
# Draft detail (memory audit 9/29/26, "draft_thinning"): a superseded,
# never-published draft keeps its whole Shift Quality evaluation, review and
# what-if (about 130 KB for a 55-person week) though nobody reads them once
# a later draft replaced it — and that growth is what would push the backup
# past the emailed copy's 25 MB. Past these many days the detail is thinned
# to its headline (score, band, confidence, the top reasons); the CSV, the
# economics and the row stay. An intermediate version's evaluation is
# thinned the same way; the generated, published and final ones never are.
DRAFT_DETAIL_KEEP_DAYS = int(os.getenv("RETAIN_DRAFT_DETAIL_DAYS", "30"))
# 30 days (scheduler audit 10/10/26, SCALE-4; it was 90): a version's detail
# is read by the as-run measurement within days of its week ending, and the
# generated, published and newest versions are never thinned.
VERSION_DETAIL_KEEP_DAYS = int(os.getenv("RETAIN_VERSION_DETAIL_DAYS", "30"))
# Removed reviews keep their words this long, then they are erased.
REVIEW_ERASE_DAYS = int(os.getenv("RETAIN_REVIEW_ERASE_DAYS", "30"))
# Mail to no intake address of ours (email_intake `no_route` — mostly spam),
# and a received invoice file's kept text (fintech_files.content, and its
# object-store copy): the invoice lines hold the history, so past a year the
# file is only its row (bar fix round 10/15/26, R5, R24).
# Claims of a period shorter than a day (scale audit 10/19/26 DB-14 /
# INF-16): live labor every quarter hour and the intraday capture every hour,
# per open restaurant, plus the loop's own 5-20 minute ticks — ~60 of a
# restaurant's ~70-150 claims a day, each asked about only inside its own
# period, kept 45 days with the daily ones. They go after
# RETAIN_SUBDAILY_CLAIMS_DAYS (floor 1: a period is asked about within it).
SUBDAILY_CLAIM_JOBS = ("live_labor", "intraday", "ai_batch_collect", "dsr_sweep", "pg_shadow", "dsr_delivery",
                       "staff_reminders", "pos_schedule_push", "pos_retry", "ai_fleet_runaway")
SUBDAILY_CLAIMS_KEEP_DAYS = int(os.getenv("RETAIN_SUBDAILY_CLAIMS_DAYS", "2"))
# The band each night's pass reads (claimed_at, indexed): the days just past
# the window, so a missed night or two is still caught; anything older goes
# at the table's own 45 days.
SUBDAILY_CLAIMS_BAND_DAYS = 7
EMAIL_INTAKE_UNROUTED_DAYS = int(os.getenv("RETAIN_EMAIL_INTAKE_UNROUTED_DAYS", "30"))
FINTECH_FILE_CONTENT_DAYS = int(os.getenv("RETAIN_FINTECH_FILE_CONTENT_DAYS", "365"))

# The prunes that are not one table's rows by one stamp, with the same
# floor rule: {name: (current days, floor)}. The schedule learners read the
# last 24 weeks of edits (schedule_versions.LEARN_DAYS); the waste trend,
# price trends and food cost % bucket inventory by ISO week; the seasonal
# baseline reads 392 days back (food_cost_intelligence.seasonal_baseline)
# and the seasonal re-check further, from the weekly summary.
def _pos_cold_days():
    try:
        import pos_cold
        return int(pos_cold.HOT_DAYS)
    except Exception:
        return 0


def _special_retention():
    return {
        "inventory_history": (INVENTORY_HISTORY_DAYS, 395),
        "inventory_daily_detail": (INVENTORY_DAILY_DAYS, 28),
        # The learners read 24 weeks of a week's saves (schedule_versions.
        # LEARN_DAYS, 168 — schedule audit 10/3/26 L-27); they used to read 8.
        "schedule_versions": (SCHEDULE_VERSIONS_KEEP_DAYS, 168),
        "superseded_drafts": (SUPERSEDED_DRAFTS_KEEP_DAYS, 90),
        "draft_detail": (DRAFT_DETAIL_KEEP_DAYS, 14),
        "version_detail": (VERSION_DETAIL_KEEP_DAYS, 30),
        # A removed review's words (memory re-audit 9/29/26, FORGET-1): the
        # soft delete is a month's undo window, then the guest text is
        # erased (history_rollups.erase_removed_reviews). Floor 7 days.
        "reviews_erase": (REVIEW_ERASE_DAYS, 7),
        # Received mail to no address (floor 7) and a received file's text
        # (floor 90: a re-read of a file is asked for within weeks).
        "email_intake_unrouted": (EMAIL_INTAKE_UNROUTED_DAYS, 7),
        "fintech_file_content": (FINTECH_FILE_CONTENT_DAYS, 90),
        # The sub-daily claims (DB-14, above), floor 1 day.
        "subdaily_claims": (SUBDAILY_CLAIMS_KEEP_DAYS, 1),
        # The sale lines' cold tier (pos_cold; RPOWER audit #23 A5-09):
        # months past RETAIN_POS_LINES_HOT_DAYS exported to object storage
        # before their lines go; floor 400 (service_performance.MAX_DAYS
        # and a year-over-year month). 0 turns the tier off.
        "pos_lines_cold": (_pos_cold_days(), 400),
    }


def retention_floor(table) -> int:
    """The least `table`'s window may be set to (see above). A table no one
    has given a floor keeps whatever it is set to (never refused) until it
    is given one — the test fails first."""
    if table in _RETENTION_FLOOR_DAYS:
        return int(_RETENTION_FLOOR_DAYS[table])
    special = _special_retention().get(table)
    if special:
        return int(special[1])
    days = _RETENTION_DAYS.get(table)
    return min(RETENTION_FLOOR_DEFAULT_DAYS, int(days)) if days else 0


def retention_state(table) -> dict:
    """{table, days, floor, column, rollup, state}: state 'ok' (pruned at
    `days`), 'disabled' (0 — kept forever) or 'below_floor' (refused: the
    configured window is under the floor, so nothing is pruned)."""
    special = _special_retention().get(table)
    days = int(special[0]) if special else int(_RETENTION_DAYS.get(table, 0) or 0)
    floor = retention_floor(table)
    state = "disabled" if days <= 0 else ("below_floor" if days < floor else "ok")
    return {"table": table, "days": days, "floor": floor, "state": state,
            "column": _RETENTION_COLUMN.get(table, "created_at") if not special else None,
            "rollup": _RETENTION_ROLLUP.get(table)}


def retention_days(table):
    """The window a pruner applies to `table`: its days when its state is
    'ok', else None (disabled or refused — prune nothing)."""
    st = retention_state(table)
    return st["days"] if st["state"] == "ok" else None


def retention_refusals() -> list:
    """Every table whose configured window is under its floor — the
    console's and the pass's list of what is not being pruned, and why."""
    names = list(_RETENTION_DAYS) + list(_special_retention())
    return [st for st in (retention_state(t) for t in names) if st["state"] == "below_floor"]


def _page_refusals(refused, db_path=None):
    """One page (hourly cooldown) and one captured failure for windows set
    under their floor. Never raises."""
    if not refused:
        return
    lines = [f"{r['table']}: RETAIN window {r['days']} days is under its floor of {r['floor']} — not pruned"
             for r in refused]
    capture(RuntimeError("retention refused: " + "; ".join(lines)),
            job="prune_ledgers", context="retention window under its floor", db_path=db_path)
    try:
        page_operator("retention_floor", "Retention refused a window under its floor",
                      lines + ["Fix the RETAIN_* variable in Railway; nothing was deleted from these tables."])
    except Exception as e:
        log.error(f"retention floor page failed: {e}")


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
        ("ask_topics", "CREATE INDEX IF NOT EXISTS idx_ask_topics_created ON ask_topics(created_at)"),
        ("ask_feedback", "CREATE INDEX IF NOT EXISTS idx_ask_feedback_created ON ask_feedback(created_at)"),
        # A received file's text past its year (ops "fintech_file_content").
        ("fintech_files", "CREATE INDEX IF NOT EXISTS idx_fintech_files_created ON fintech_files(created_at)"),
        # RPOWER audit #23 (A5-09, F5): the business-date retention deletes.
        ("dsr_reports", "CREATE INDEX IF NOT EXISTS idx_dsr_reports_bdate ON dsr_reports(business_date)"),
        ("pos_intraday", "CREATE INDEX IF NOT EXISTS idx_pos_intraday_bdate ON pos_intraday(business_date)"),
        # Audit #23 F7: the loss lines by approver and the guests' matched
        # checks, now pruned at their age.
        ("pos_loss_daily", "CREATE INDEX IF NOT EXISTS idx_pos_loss_daily_date ON pos_loss_daily(business_date)"),
        ("guest_pos_visits", "CREATE INDEX IF NOT EXISTS idx_guest_pos_visits_date ON guest_pos_visits(business_date)"),
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


def _prune_inventory_history(conn, deadline=None, refused=None):
    """Thin inventory_history to weekly past INVENTORY_DAILY_DAYS and drop it
    past INVENTORY_HISTORY_DAYS. Dated by week_end — the day the snapshot
    describes — not saved_at, which a backfill sets to today. Before any row
    is dropped its week is summarised (history_rollups.summarise_inventory:
    the week's value, waste, COGS and food cost %, and the month's unit cost
    per ingredient, kept forever), and waste_trend's loader reads the
    summary for the weeks that are gone — the seasonal food-cost re-check
    reaches past 395 days (memory audit 9/29/26, "seasonal_food"). A window
    under its floor is refused (appended to `refused`). Returns rows
    deleted."""
    n = 0
    history_days = retention_days("inventory_history")
    if history_days:
        cutoff = conn.execute("SELECT date('now', ?)", (f"-{history_days} days",)).fetchone()[0]
        import history_rollups
        history_rollups.summarise_inventory(conn, cutoff, deadline=deadline)
        # Only weeks that now have their summary go: a summary cut short by
        # the time bound leaves the rest for tomorrow.
        cur = conn.execute(
            "DELETE FROM inventory_history WHERE week_end < ? AND EXISTS (SELECT 1 FROM inventory_weekly_summary s "
            "WHERE s.restaurant_id = inventory_history.restaurant_id "
            "AND s.week = date(inventory_history.week_end, 'weekday 0', '-6 days'))", (cutoff,))
        n += max(0, cur.rowcount or 0)
        conn.commit()
    elif refused is not None and retention_state("inventory_history")["state"] == "below_floor":
        refused.append(retention_state("inventory_history"))
    daily_days = retention_days("inventory_daily_detail")
    if daily_days:
        # The week's figure is its latest snapshot (waste_trend.load_waste_history).
        # An old row goes when a later snapshot of its week exists (the
        # same rows the per-week "latest id" subquery kept, in SQL Postgres
        # takes too — it refused that GROUP BY's ungrouped week_end).
        # A snapshot taken on a real count stays (food & bar audit 10/16/26,
        # FC-CALC-02): food cost % is anchored only on counted snapshots,
        # dated by the count day, and the week's latest one is often not it.
        # Counted is read from the items (waste_trend._counted), so the
        # candidates are read one at a time and only the rest deleted.
        import waste_trend as _wt
        from datetime import date as _ih_date
        cand = conn.execute(
            "SELECT id, week_end FROM inventory_history WHERE week_end < date('now', ?) AND EXISTS ("
            "  SELECT 1 FROM inventory_history h2 WHERE h2.restaurant_id = inventory_history.restaurant_id"
            "  AND date(h2.week_end, 'weekday 0', '-6 days') = date(inventory_history.week_end, 'weekday 0', '-6 days')"
            "  AND (h2.week_end > inventory_history.week_end OR (h2.week_end = inventory_history.week_end"
            "       AND h2.id > inventory_history.id)))",
            (f"-{daily_days} days",)).fetchall()
        drop = []
        for row in cand:
            try:
                got = conn.execute("SELECT items_json FROM inventory_history WHERE id=?", (row["id"],)).fetchone()
                items = json.loads(got["items_json"]) if got and got["items_json"] else None
                kept = bool(items) and _wt._counted(items, _ih_date.fromisoformat(str(row["week_end"])[:10]))[0]
            except Exception:
                kept = False
            if not kept:
                drop.append(int(row["id"]))
        for i in range(0, len(drop), 500):
            part = drop[i:i + 500]
            dc = conn.execute(f"DELETE FROM inventory_history WHERE id IN ({','.join('?' * len(part))})", tuple(part))
            n += max(0, dc.rowcount or 0)
    elif refused is not None and retention_state("inventory_daily_detail")["state"] == "below_floor":
        refused.append(retention_state("inventory_daily_detail"))
    conn.commit()
    return n


def _chunked_delete(conn, table, where, args, deadline, max_rows=None):
    """DELETE ... WHERE `where` in RETENTION_CHUNK_ROWS batches, committing
    each, until done, `deadline` (time.monotonic()) or `max_rows` (the
    per-pass cap). Returns rows deleted — `max_rows` or more means the cap
    stopped it."""
    total = 0
    while True:
        chunk = int(RETENTION_CHUNK_ROWS)
        if max_rows is not None:
            chunk = max(0, min(chunk, int(max_rows) - total))
            if chunk == 0:
                return total
        cur = conn.execute(f"DELETE FROM {table} WHERE rowid IN "
                           f"(SELECT rowid FROM {table} WHERE {where} LIMIT {chunk})",
                           tuple(args))
        n = max(0, cur.rowcount or 0)
        conn.commit()
        total += n
        if n < chunk or time.monotonic() > deadline:
            return total


def _inflow_cap(conn, table, col, days, only=None) -> int:
    """This pass's row cap for `table` (scale audit 10/19/26 DB-03): the
    fixed RETENTION_PASS_MAX_ROWS, or RETENTION_INFLOW_FACTOR times the rows
    that aged out in the last day (stamped in the one day just past the
    window) when that is more. A table whose daily inflow outgrew the fixed
    cap only ever grew; this keeps up with it — and catches up a backlog —
    while a wrong clock or a mis-stamped column still cannot empty a table
    in one night: the band is one day of rows, so the cap stays a couple of
    days of them (none at all when the clock has jumped past the data)."""
    base = int(RETENTION_PASS_MAX_ROWS)
    try:
        where = f"{col} >= datetime('now', ?) AND {col} < datetime('now', ?)" + (f" AND {only}" if only else "")
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}",
                         (f"-{int(days) + 1} days", f"-{int(days)} days")).fetchone()[0]
    except Exception as e:
        log.warning(f"retention inflow of {table} unreadable: {e}")
        return base
    return max(base, int(RETENTION_INFLOW_FACTOR) * int(n or 0))


def _drop_expired_months(conn, table, days, held=None) -> list:
    """On Postgres, the month partitions of `table` wholly past its window,
    dropped (pg_partitions.drop_expired, at most PARTITION_DROPS_PER_PASS a
    night) before the chunked delete takes what is left — the partial month
    and the DEFAULT partition. A restaurant under a records hold keeps the
    table RECORDS_HOLD_DAYS, so with one held a month goes only past that.
    [] on SQLite, for an unpartitioned table, or one whose retention keeps
    some rows whatever their age (_RETENTION_ONLY)."""
    import db_dialect
    if table not in db_dialect.PARTITIONED or table in _RETENTION_ONLY or not db_dialect.is_postgres(conn):
        return []
    keep = int(days)
    if table in _RETENTION_HOLD_TABLES and held and keep < RECORDS_HOLD_DAYS:
        keep = RECORDS_HOLD_DAYS
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    cutoff = (_dt.now(_tz.utc) - _td(days=keep)).strftime("%Y-%m-%d %H:%M:%S")
    import pg_partitions
    conn.commit()                    # the drop runs in a transaction of its own
    return pg_partitions.drop_expired(conn, table, cutoff)


def _prune_subdaily_claims(conn, deadline, refused=None) -> int:
    """The claims of a period shorter than a day, after
    SUBDAILY_CLAIMS_KEEP_DAYS (DB-14 / INF-16), under the floor rule — from
    the band of SUBDAILY_CLAIMS_BAND_DAYS just past it, so the night's read
    is a few days of claims, never the table's whole 45. Returns rows."""
    st = retention_state("subdaily_claims")
    if st["state"] == "disabled":
        return 0
    if st["state"] == "below_floor":
        if refused is not None:
            refused.append(st)
        return 0
    days = int(st["days"])
    # The job names are this module's own constants, never input.
    keys = "(" + " OR ".join(f"job_key LIKE '{j}:%'" for j in SUBDAILY_CLAIM_JOBS) + ")"
    where = f"claimed_at >= datetime('now', ?) AND claimed_at < datetime('now', ?) AND {keys}"
    args = (f"-{days + SUBDAILY_CLAIMS_BAND_DAYS} days", f"-{days} days")
    cap = _inflow_cap(conn, "job_period_claims", "claimed_at", days, only=keys)
    return _chunked_delete(conn, "job_period_claims", where, args, deadline, max_rows=cap)


# What thinned detail keeps of a Shift Quality evaluation: the headline the
# history list and its one-line summary read (models.get_schedule_history,
# _history_summary_line), and when it was thinned.
_THIN_QUALITY_KEYS = ("checked", "score", "band")


def _thin_quality(raw, today):
    try:
        q = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        q = None
    if not isinstance(q, dict):
        return None
    out = {k: q.get(k) for k in _THIN_QUALITY_KEYS if k in q}
    conf = q.get("confidence")
    if isinstance(conf, dict) and conf.get("level"):
        out["confidence"] = {"level": conf.get("level")}
    for k in ("weaknesses", "strengths"):
        if isinstance(q.get(k), list):
            out[k] = [str(x)[:200] for x in q[k][:2]]
    out["thinned_at"] = today
    return json.dumps(out)


def _thin_review(raw, today):
    try:
        r = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        r = None
    if not isinstance(r, dict):
        return None
    out = {k: r[k] for k in ("hard", "soft", "ok") if k in r}
    out["thinned_at"] = today
    return json.dumps(out)


def _thin_drafts(conn, deadline):
    """Superseded, never-published, never-shared drafts past
    DRAFT_DETAIL_KEEP_DAYS whose week is over: their Shift Quality
    evaluation, review and what-if are thinned to the headline (score,
    band, confidence, the top reasons, the breach counts); the CSV, the
    economics and the row stay, and detail_thinned_at says when. A draft
    with change requests or outcomes was used after all: kept whole. Then
    the intermediate versions past VERSION_DETAIL_KEEP_DAYS — never the
    generated, the published or a week's newest — keep only their headline
    too (memory audit 9/29/26, "draft_thinning"). Returns {name: rows}."""
    out = {}
    today = conn.execute("SELECT date('now')").fetchone()[0]
    draft_days = retention_days("draft_detail")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_history)")}
    if draft_days and {"superseded_by", "published_at", "detail_thinned_at", "quality_json"} <= cols:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        used = " ".join(f"AND NOT EXISTS (SELECT 1 FROM {t} x WHERE x.history_id = h.id)"
                        for t in ("shift_change_requests", "schedule_outcomes") if t in have)
        shared = ("AND NOT EXISTS (SELECT 1 FROM schedule_shares sh WHERE sh.schedule_id = h.id)"
                  if "schedule_shares" in have else "")
        rows = conn.execute(
            "SELECT h.id, h.quality_json, h.review_json FROM schedule_history h "
            "WHERE h.superseded_by IS NOT NULL AND h.published_at IS NULL AND h.detail_thinned_at IS NULL "
            "AND h.generated_at < datetime('now', ?) AND COALESCE(h.week_end, h.week_start) < date('now') "
            f"{shared} {used} LIMIT ?", (f"-{draft_days} days", int(RETENTION_CHUNK_ROWS))).fetchall()
        n = 0
        for r in rows:
            if time.monotonic() > deadline:
                break
            # The headlines stay; the pointers to the whole go, and the
            # stored detail with them once nothing names it (SCALE-13).
            ref_cols = ", quality_ref=NULL, review_ref=NULL" if {"quality_ref", "review_ref"} <= cols else ""
            conn.execute("UPDATE schedule_history SET quality_json=?, review_json=?, what_if_json=NULL, "
                         f"detail_thinned_at=datetime('now'){ref_cols} WHERE id=?",
                         (_thin_quality(r["quality_json"], today), _thin_review(r["review_json"], today), r["id"]))
            n += 1
            if n % 200 == 0:
                conn.commit()
        conn.commit()
        if n:
            out["schedule_history_thinned"] = n
        # The shift projection of a thinned draft (superseded, never sent or
        # shared or used, its week over) goes too: its text stays on the row,
        # and every reader falls back to it (schedule_shifts.rows_by_week).
        # It was kept until the draft itself was pruned a year on — ~0.46 MB a
        # restaurant-week (blind re-audit 10/20/26 R5-10). Drafts thinned
        # before this are swept the same way, a chunk a pass.
        if {"schedule_shifts", "schedule_shift_weeks"} <= have and time.monotonic() < deadline:
            gone = 0
            for (hid,) in conn.execute(
                    "SELECT h.id FROM schedule_history h WHERE h.detail_thinned_at IS NOT NULL "
                    "AND h.superseded_by IS NOT NULL AND h.published_at IS NULL "
                    "AND EXISTS (SELECT 1 FROM schedule_shift_weeks w WHERE w.history_id = h.id) "
                    f"{shared} {used} LIMIT ?", (int(RETENTION_CHUNK_ROWS),)).fetchall():
                if time.monotonic() > deadline:
                    break
                gone += max(0, conn.execute("DELETE FROM schedule_shifts WHERE history_id=?", (hid,)).rowcount or 0)
                conn.execute("DELETE FROM schedule_shift_weeks WHERE history_id=?", (hid,))
                conn.commit()
            if gone:
                out["schedule_shifts_of_thinned_drafts"] = gone
    version_days = retention_days("version_detail")
    vcols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_versions)")}
    if version_days and time.monotonic() < deadline and "quality_json" in vcols:
        has_ref = "quality_ref" in vcols
        rows = conn.execute(
            "SELECT id, quality_json" + (", quality_ref" if has_ref else "") + " FROM schedule_versions v "
            "WHERE created_at < datetime('now', ?) "
            "AND COALESCE(reason, '') NOT IN ('generated', 'published') AND quality_json IS NOT NULL "
            + ("AND (quality_ref IS NOT NULL OR (quality_json NOT LIKE '%\"thinned_at\"%' "
               "AND quality_json NOT LIKE '%\"headline\": true%')) " if has_ref else
               "AND quality_json NOT LIKE '%\"thinned_at\"%' ")
            + "AND version < (SELECT MAX(v2.version) FROM schedule_versions v2 WHERE v2.history_id = v.history_id) "
            "LIMIT ?", (f"-{version_days} days", int(RETENTION_CHUNK_ROWS))).fetchall()
        n = 0
        for r in rows:
            if time.monotonic() > deadline:
                break
            if has_ref and r["quality_ref"]:
                # The headline stays inline; the pointer to the whole
                # evaluation goes (the blob goes once nothing points at it).
                conn.execute("UPDATE schedule_versions SET quality_ref=NULL WHERE id=?", (r["id"],))
            else:
                conn.execute("UPDATE schedule_versions SET quality_json=? WHERE id=?",
                             (_thin_quality(r["quality_json"], today), r["id"]))
            n += 1
        conn.commit()
        if n:
            out["schedule_versions_thinned"] = n
        if has_ref and time.monotonic() < deadline:
            moved = _compact_version_quality(conn, deadline)
            if moved:
                out["schedule_versions_compacted"] = moved
    if time.monotonic() < deadline:
        moved = _compact_history_detail(conn, deadline)
        if moved:
            out["schedule_history_compacted"] = moved
    if time.monotonic() < deadline:
        moved = _compact_shift_exports(conn, deadline)
        if moved:
            out["shift_exports_compacted"] = moved
        if has_ref and time.monotonic() < deadline:
            gone = _gc_quality_blobs(conn, deadline)
            if gone:
                out["quality_blobs"] = gone
    return out


def _records_held(conn) -> list:
    """The restaurants a predictive-scheduling law covers today, whose
    scheduling records are kept three years (compliance_packs
    records_hold_days ≥ RECORDS_HOLD_DAYS). Read once per pass; a failure
    holds nobody back from the usual windows (logged)."""
    try:
        from types import SimpleNamespace
        import compliance_packs
        cols = {r[1] for r in conn.execute("PRAGMA table_info(restaurants)")}
        want = [c for c in ("id", "jurisdiction", "compliance_profile_json", "aivis_city_json", "mailing_address")
                if c in cols]
        if "compliance_profile_json" not in want:
            return []
        out = []
        for row in conn.execute(f"SELECT {', '.join(want)} FROM restaurants WHERE compliance_profile_json IS NOT NULL"):
            r = SimpleNamespace(**{k: row[k] for k in want})
            if compliance_packs.records_hold_days(r) >= RECORDS_HOLD_DAYS:
                out.append(int(row["id"]))
        return out
    except Exception as e:
        log.error(f"records hold not read: {e}")
        return []


def _compact_version_quality(conn, deadline) -> int:
    """Versions written before 10/10/26 that still hold their whole
    evaluation inline (SCALE-4): the whole moved to quality_blobs under its
    hash, the headline left inline — the same shape a new version is
    written in. Bounded by RETENTION_CHUNK_ROWS and the pass's deadline;
    the rest are compacted on the following nights."""
    import schedule_versions as _sv_c
    rows = conn.execute(
        "SELECT id, restaurant_id, quality_json FROM schedule_versions WHERE quality_ref IS NULL "
        "AND quality_json IS NOT NULL AND quality_json NOT LIKE '%\"thinned_at\"%' "
        "AND quality_json NOT LIKE '%\"headline\": true%' LIMIT ?", (int(RETENTION_CHUNK_ROWS),)).fetchall()
    n = 0
    for r in rows:
        if time.monotonic() > deadline:
            break
        try:
            q = json.loads(r["quality_json"] or "null")
        except (TypeError, ValueError):
            q = None
        if not isinstance(q, dict) or not q:
            # Not an evaluation at all: marked so it is not read again.
            conn.execute("UPDATE schedule_versions SET quality_json=? WHERE id=?",
                         (json.dumps({"headline": True, "unreadable": True}), r["id"]))
        else:
            head, ref = _sv_c.store_quality(conn, r["restaurant_id"], q)
            conn.execute("UPDATE schedule_versions SET quality_json=?, quality_ref=? WHERE id=?", (head, ref, r["id"]))
        n += 1
        if n % 200 == 0:
            conn.commit()
    conn.commit()
    return n


def _gc_quality_blobs(conn, deadline) -> int:
    """Stored evaluations no version points at any more (thinned, pruned, or
    their week deleted), in chunks. An hour's grace: a blob is written in
    the same transaction as the version that names it, so nothing newer
    can be waiting on it. One kept in object storage (blob_store, RM-14) is
    deleted there first."""
    try:
        import blob_store
        if blob_store.active() != "db":
            for r in conn.execute(
                    "SELECT body FROM quality_blobs WHERE created_at < datetime('now', '-1 hour') AND "
                    "substr(body, 1, ?) = ? AND NOT EXISTS (SELECT 1 FROM schedule_versions v WHERE "
                    "v.quality_ref = quality_blobs.hash) AND " + _HISTORY_UNREF + " LIMIT ?",
                    (len(blob_store.POINTER), blob_store.POINTER, int(RETENTION_CHUNK_ROWS))).fetchall():
                blob_store.delete(r["body"])
    except Exception as e:
        log.warning(f"stored quality objects not deleted: {e}")
    return _chunked_delete(
        conn, "quality_blobs",
        "created_at < datetime('now', '-1 hour') AND NOT EXISTS "
        "(SELECT 1 FROM schedule_versions v WHERE v.quality_ref = quality_blobs.hash) AND " + _HISTORY_UNREF,
        (), deadline)


# A stored detail a draft still names — its evaluation or its review
# (schedule_history.quality_ref / review_ref, SCALE-13) — is never collected.
_HISTORY_UNREF = ("NOT EXISTS (SELECT 1 FROM schedule_history h WHERE h.quality_ref = quality_blobs.hash) "
                  "AND NOT EXISTS (SELECT 1 FROM schedule_history h2 WHERE h2.review_ref = quality_blobs.hash)")


def _compact_shift_exports(conn, deadline) -> int:
    """Restaurants on shift_facts whose shift file export is still stored
    plain (written before 10/14/26): compressed (models.store_shift_file —
    SCALE-14). Bounded; resumable (a compacted row no longer matches)."""
    import models as _m_x
    cols = {r[1] for r in conn.execute("PRAGMA table_info(client_data)")}
    if not {"shifts_store", "shifts_csv_z"} <= cols:
        return 0
    rids = [r[0] for r in conn.execute(
        "SELECT restaurant_id FROM client_data WHERE shifts_store = 'facts' AND COALESCE(shifts_csv, '') <> '' "
        "ORDER BY restaurant_id LIMIT ?", (int(RETENTION_CHUNK_ROWS),)).fetchall()]
    n = 0
    for rid in rids:
        if time.monotonic() > deadline:
            break
        _m_x.store_shift_file(conn, rid, _m_x.shift_file_text(conn, rid))
        conn.commit()
        n += 1
    return n


def _compact_history_detail(conn, deadline) -> int:
    """Drafts written before 10/14/26 that still hold their whole Shift
    Quality evaluation and review inline (SCALE-13: 0.3-0.7 MB a week):
    the whole of each moved to quality_blobs under its hash, the headline
    left inline — the shape a new draft is written in. Bounded by
    RETENTION_CHUNK_ROWS and the pass's deadline; resumable without a
    cursor because a compacted row no longer matches (its ref is set, or
    its JSON is a headline), so the next night starts at the first row
    still to do and nothing is starved."""
    import models as _m_c
    import schedule_versions as _sv_c
    cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_history)")}
    if not {"quality_ref", "review_ref"} <= cols:
        return 0
    rows = conn.execute(
        "SELECT id, restaurant_id, quality_json, quality_ref, review_json, review_ref FROM schedule_history WHERE "
        "(quality_ref IS NULL AND quality_json IS NOT NULL AND quality_json NOT LIKE '%\"headline\": true%' "
        " AND quality_json NOT LIKE '%\"thinned_at\"%') OR "
        "(review_ref IS NULL AND review_json IS NOT NULL AND review_json NOT LIKE '%\"headline\": true%' "
        " AND review_json NOT LIKE '%\"thinned_at\"%') "
        "ORDER BY id LIMIT ?", (int(RETENTION_CHUNK_ROWS),)).fetchall()
    n = 0
    for r in rows:
        if time.monotonic() > deadline:
            break
        sets, args = [], []
        if r["quality_ref"] is None and r["quality_json"]:
            q = _m_c.history_quality(conn, r)
            if isinstance(q, dict) and q:
                head, ref = _sv_c.store_quality(conn, r["restaurant_id"], q)
                sets += ["quality_json=?", "quality_ref=?"]
                args += [head, ref]
            else:
                sets.append("quality_json=?")
                args.append(json.dumps({"headline": True, "unreadable": True}))
        if r["review_ref"] is None and r["review_json"]:
            rv = _m_c.history_review(conn, r)
            if rv:
                head, ref = _m_c.store_history_review(conn, r["restaurant_id"], rv)
                sets += ["review_json=?", "review_ref=?"]
                args += [head, ref]
            else:
                sets.append("review_json=?")
                args.append(json.dumps({"headline": True, "unreadable": True}))
        if sets:
            conn.execute(f"UPDATE schedule_history SET {', '.join(sets)} WHERE id=?", (*args, r["id"]))
        n += 1
        if n % 100 == 0:
            conn.commit()
    conn.commit()
    return n


def _prune_schedules(conn, deadline, refused=None, held=None):
    """Old schedule states, never a published week (#72): intermediate saves
    of weeks past SCHEDULE_VERSIONS_KEEP_DAYS (each week keeps its newest
    version, every published one and its generated draft — the diff the
    schedule learner reads), and drafts a later regeneration superseded,
    never published or shared, past SUPERSEDED_DRAFTS_KEEP_DAYS. First, the
    detail of superseded drafts and old intermediate versions is thinned
    (_thin_drafts). A window under its floor is refused (appended to
    `refused`). `held`: restaurants under a predictive-scheduling law, whose
    intermediate versions (a cover, a swap, an edit sent to staff) are kept
    RECORDS_HOLD_DAYS (MARKET-4). Returns {table: rows}."""
    out = {}
    try:
        out.update(_thin_drafts(conn, deadline))
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning(f"draft thinning skipped: {e}")
    for name in ("schedule_versions", "superseded_drafts", "draft_detail", "version_detail"):
        st = retention_state(name)
        if refused is not None and st["state"] == "below_floor":
            refused.append(st)
    versions_days = retention_days("schedule_versions")
    if versions_days:
        # Never a snapshot a kept version is stored against (SCALE-34:
        # schedule_versions keeps later versions as differences from it).
        where = ("created_at < datetime('now', ?) AND reason NOT IN ('published', 'generated') "
                 "AND version < (SELECT MAX(v2.version) FROM schedule_versions v2 "
                 "               WHERE v2.history_id = schedule_versions.history_id) "
                 "AND NOT EXISTS (SELECT 1 FROM schedule_versions d WHERE d.history_id = schedule_versions.history_id "
                 "                AND d.base_version = schedule_versions.version)")
        args = [f"-{versions_days} days"]
        if held and versions_days < RECORDS_HOLD_DAYS:
            where += (f" AND NOT (restaurant_id IN ({','.join('?' * len(held))}) "
                      "AND created_at >= datetime('now', ?))")
            args += list(held) + [f"-{RECORDS_HOLD_DAYS} days"]
        n = _chunked_delete(conn, "schedule_versions", where, tuple(args), deadline)
        if n:
            out["schedule_versions"] = n
    drafts_days = retention_days("superseded_drafts")
    if drafts_days and time.monotonic() < deadline:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_history)")}
        if {"superseded_by", "published_at"} <= cols:
            ids = [r[0] for r in conn.execute(
                "SELECT h.id FROM schedule_history h WHERE h.superseded_by IS NOT NULL AND h.published_at IS NULL "
                "AND h.generated_at < datetime('now', ?) "
                "AND NOT EXISTS (SELECT 1 FROM schedule_shares s WHERE s.schedule_id = h.id) "
                "LIMIT ?", (f"-{drafts_days} days", int(RETENTION_CHUNK_ROWS))).fetchall()]
            dependents = [t for t in ("schedule_versions", "schedule_experiment_weeks",
                                      "shift_change_requests", "schedule_outcomes",
                                      "schedule_shifts", "schedule_shift_weeks")
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
                for t in ("schedule_versions", "schedule_experiment_weeks", "schedule_shifts",
                          "schedule_shift_weeks"):
                    if t in dependents:
                        conn.execute(f"DELETE FROM {t} WHERE history_id=?", (hid,))
                deleted += max(0, conn.execute("DELETE FROM schedule_history WHERE id=?", (hid,)).rowcount or 0)
                conn.commit()
            if deleted:
                out["schedule_history"] = deleted
    return out


def _erase_removed_reviews(conn, deadline=None, refused=None):
    """Blank the guest text of reviews removed past REVIEW_ERASE_DAYS
    (memory re-audit 9/29/26, FORGET-1) — the soft delete alone kept every
    removed review's words in the database and every backup for good. Under
    the same floor rule as every prune: a window under its floor erases
    nothing and is refused."""
    st = retention_state("reviews_erase")
    if st["state"] == "disabled":
        return 0
    if st["state"] == "below_floor":
        if refused is not None:
            refused.append(st)
        return 0
    import history_rollups
    return history_rollups.erase_removed_reviews(conn, st["days"], deadline=deadline,
                                                 max_rows=RETENTION_PASS_MAX_ROWS, chunk=RETENTION_CHUNK_ROWS)


def _prune_intake(conn, deadline=None, refused=None) -> dict:
    """The intake's two special prunes (email_intake.prune_unrouted,
    email_intake.expire_file_content), each under the floor rule. Returns
    {name: rows} for the ones that did something."""
    import email_intake
    out = {}
    for name, fn in (("email_intake_unrouted", email_intake.prune_unrouted),
                     ("fintech_file_content", email_intake.expire_file_content)):
        st = retention_state(name)
        if st["state"] == "disabled":
            continue
        if st["state"] == "below_floor":
            if refused is not None:
                refused.append(st)
            continue
        n = fn(conn, st["days"], deadline=deadline, max_rows=RETENTION_PASS_MAX_ROWS)
        if n:
            out[name] = n
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


def _run_rollups(db_path):
    """Every summary a table declares (_RETENTION_ROLLUP), once each, in
    declaration order — the AI usage rollup first. Returns {"module:function":
    ok}. A rollup that raised is captured; its tables keep tonight's rows."""
    import importlib
    done = {}
    for target in dict.fromkeys(_RETENTION_ROLLUP.values()):
        mod, fn = target.split(":", 1)
        try:
            getattr(importlib.import_module(mod), fn)(db_path)
            done[target] = True
        except Exception as e:
            done[target] = False
            log.error(f"prune_ledgers: the rollup {target} failed, so its tables are kept tonight: {e}")
            capture(e, job="prune_ledgers", context=target, db_path=db_path)
    return done


def prune_ledgers(db_path=None):
    """Delete rows past their retention window. Returns {table: rows_deleted}
    with the standard counts, `refused` (windows under their floor) and
    `capped` (tables the per-pass cap stopped).

    Deliberately tolerant: a table that does not exist yet, or whose stamp
    column is named something else on an older database, is skipped rather
    than taking the whole sweep down with it. Nightly, straight after the
    backup (so the pruned rows are in it): first every table's rollup
    (_RETENTION_ROLLUP — a failed one keeps its tables' rows tonight), then
    each table chunked with a commit per chunk, at most
    RETENTION_PASS_MAX_ROWS rows per table and a wall-clock bound, then
    planner statistics (#72, #81). A window set under its floor is refused,
    counted failed and paged (memory audit 9/29/26)."""
    from models import get_conn, DB_PATH
    deleted = {}
    counts = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    refused, capped, dropped_parts = [], [], []
    try:
        conn = get_conn(db_path or DB_PATH)
    except Exception as e:
        log.error(f"prune_ledgers could not open the database: {e}")
        raise
    deadline = time.monotonic() + RETENTION_MAX_SECONDS

    def _missing(err):
        text = str(err).lower()
        return "no such table" in text or "no such column" in text

    # The rollups first (fix round G, #70; memory audit 9/29/26): the AI
    # daily rollup, the monthly alert and engagement summaries, the
    # newsletter results — never pruned, so what the raw rows said
    # outlives them.
    rolled = _run_rollups(db_path)
    held = _records_held(conn)

    try:
        for table in list(_RETENTION_DAYS):
            st = retention_state(table)
            if st["state"] == "disabled":
                continue            # 0 disables retention for that table
            if st["state"] == "below_floor":
                refused.append(st)
                counts["attempted"] += 1
                counts["failed"] += 1
                continue
            if time.monotonic() > deadline:
                counts["hit_bound"] = True
                break
            target = _RETENTION_ROLLUP.get(table)
            if target and not rolled.get(target, True):
                counts["skipped"] += 1
                continue
            days, col = st["days"], st["column"]
            counts["attempted"] += 1
            try:
                # A table may keep some rows for good whatever their age
                # (_RETENTION_ONLY: rec_events keeps every answer).
                only = _RETENTION_ONLY.get(table)
                where, args = f"{col} < datetime('now', ?)" + (f" AND {only}" if only else ""), [f"-{days} days"]
                # A restaurant a predictive-scheduling law covers keeps these
                # three years (MARKET-4).
                if table in _RETENTION_HOLD_TABLES and held and days < RECORDS_HOLD_DAYS:
                    where += (f" AND NOT (restaurant_id IN ({','.join('?' * len(held))}) "
                              f"AND {col} >= datetime('now', ?))")
                    args += list(held) + [f"-{RECORDS_HOLD_DAYS} days"]
                # Postgres: whole months past the window are dropped first
                # (DB-03); a failed drop leaves them to the delete below.
                try:
                    gone = _drop_expired_months(conn, table, days, held)
                    if gone:
                        dropped_parts.extend(gone)
                except Exception as e:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    log.warning(f"prune_ledgers could not drop {table}'s old months: {e}")
                    capture(e, job="prune_ledgers", context=f"partition drop {table}", db_path=db_path)
                # The cap follows the table's inflow (DB-03): read before
                # the delete, which may take the newest aged rows first.
                cap = _inflow_cap(conn, table, col, days, only)
                n = _chunked_delete(conn, table, where, tuple(args), deadline, max_rows=cap)
                if n:
                    deleted[table] = n
                if n >= cap:
                    capped.append(table)
                    counts["hit_bound"] = True
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
            n = _prune_inventory_history(conn, deadline=deadline, refused=refused)
            if n:
                deleted["inventory_history"] = n
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            log.debug(f"prune_ledgers skipped inventory_history: {e}")
        try:
            deleted.update(_prune_schedules(conn, deadline, refused=refused, held=held))
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            log.debug(f"prune_ledgers skipped schedules: {e}")
        try:
            n = _erase_removed_reviews(conn, deadline, refused=refused)
            if n:
                deleted["reviews_erased"] = n
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            if _missing(e):
                log.debug(f"prune_ledgers skipped the review erase: {e}")
            else:
                counts["attempted"] += 1
                counts["failed"] += 1
                log.error(f"prune_ledgers could not erase removed reviews: {e}")
                capture(e, job="prune_ledgers", context="reviews_erase", db_path=db_path)
        try:
            n = _prune_subdaily_claims(conn, deadline, refused=refused)
            if n:
                deleted["subdaily_claims"] = n
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            log.warning(f"prune_ledgers skipped the sub-daily claims: {e}")
        try:
            st_cold = retention_state("pos_lines_cold")
            if st_cold["state"] == "below_floor":
                refused.append(st_cold)
            elif st_cold["state"] == "ok" and rolled.get("pos_archive:roll_item_days", True):
                import pos_cold
                cold = pos_cold.tier(deadline=deadline, db_path=db_path)
                if cold.get("deleted") or cold.get("exported"):
                    deleted["pos_lines_cold"] = int(cold.get("deleted") or 0)
                if cold.get("failed"):
                    counts["attempted"] += 1
                    counts["failed"] += 1
                if cold.get("hit_bound"):
                    counts["hit_bound"] = True
        except Exception as e:
            log.error(f"prune_ledgers: the sale lines' cold tier failed (their lines are kept): {e}")
            capture(e, job="prune_ledgers", context="pos_lines_cold", db_path=db_path)
        try:
            deleted.update(_prune_intake(conn, deadline, refused=refused))
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            if _missing(e):
                log.debug(f"prune_ledgers skipped the intake prunes: {e}")
            else:
                counts["attempted"] += 1
                counts["failed"] += 1
                log.error(f"prune_ledgers could not prune the intake: {e}")
                capture(e, job="prune_ledgers", context="intake", db_path=db_path)
        # POS data after a disconnect or cancel (Audit #23 F7, A7-13), and
        # the RPOWER lists no bound store reads any more (A7-03) — each on
        # its own connection, inside this pass's wall-clock bound.
        try:
            import offboarding
            res = offboarding.purge_disconnected_pos(deadline=deadline, db_path=db_path)
            if res.get("purged"):
                deleted["pos_after_disconnect"] = sum(res["purged"].values())
            if res.get("unfinished"):
                counts["hit_bound"] = True
        except Exception as e:
            counts["attempted"] += 1
            counts["failed"] += 1
            log.error(f"prune_ledgers could not purge disconnected POS data: {e}")
            capture(e, job="prune_ledgers", context="pos_after_disconnect", db_path=db_path)
        try:
            import rpower
            n = rpower.purge_catalogs(db_path)
            if n:
                deleted["pos_catalogs"] = n
        except Exception as e:
            log.warning(f"prune_ledgers skipped the RPOWER list purge: {e}")
        _optimize(conn)
    finally:
        conn.close()
    if deleted:
        log.info(f"Pruned old rows: {deleted}")
    special_refused = [r for r in refused if r["table"] not in _RETENTION_DAYS]
    if special_refused:
        counts["attempted"] += len(special_refused)
        counts["failed"] += len(special_refused)
    if refused:
        _page_refusals(refused, db_path=db_path)
    if capped:
        capture(RuntimeError(f"retention cap: {', '.join(capped)} had more than {RETENTION_PASS_MAX_ROWS} rows "
                             f"past their window; the rest go on the next nights"),
                job="prune_ledgers", context="per-pass cap", db_path=db_path)
    deleted.update(counts)
    deleted["refused"] = [{"table": r["table"], "days": r["days"], "floor": r["floor"]} for r in refused]
    deleted["capped"] = capped
    if dropped_parts:
        deleted["partitions_dropped"] = dropped_parts
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
    # The data checks' findings apart from the failures (Audit #23 A6-04): a
    # reconcile gap is the data's news, not a job that broke.
    failures = failures_since(since, data_checks=False)
    checks = failures_since(since, data_checks=True)
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
    if not failures and not stuck and not sec_lines and not overdue and not backup_bad and not missed and not checks:
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
    check_items = [f'<b>{_html.escape(f["job"])}</b> ×{int(f["cnt"])} — '
                   f'{_html.escape((f.get("sample_error") or "")[:160])}' for f in checks]
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
        f'<p style="font-size:12px;color:{b["muted"]};margin:0">Since {_html.escape(_since_ct(since))}</p>'
        + _section("Overdue", overdue_items, "No successful run inside the job's SLA.")
        + _section("Backup", backup_items)
        + _section("Failures", fail_items)
        + _section("Data checks", check_items, "The data disagreed with itself; each store's findings are on "
                   "its console page. Nothing failed.")
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
    if checks:
        parts.append("data checks")
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
    # The console's records: customer accounts only (C's segment), their
    # churn read under `churn_risk` (level, reasons, points) — this read
    # `churn`/`score`, keys the records never carried, so the at-risk list
    # was always empty — and the first onboarding step not yet done.
    real = [r for r in recs if r.get("segment", "customer") == "customer"
            and not r.get("is_demo") and not r.get("is_admin_home")]

    def _churn(r):
        return r.get("churn_risk") or r.get("churn") or {}
    at_risk = sorted([r for r in real if _churn(r).get("level") in ("high", "medium")],
                     key=lambda r: -int(_churn(r).get("points") or _churn(r).get("score") or 0))[:8]
    onboarding = [r for r in real if (r.get("onboarding") or {}).get("complete") is False][:8]
    for r in onboarding:
        ob = r.get("onboarding") or {}
        if not ob.get("next_step"):
            nxt = next((st for st in ob.get("steps") or [] if not st.get("done")), None)
            ob["next_step"] = (nxt or {}).get("label")
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
                  f'{_html.escape("; ".join(_churn(r).get("reasons") or [])[:160])}' for r in at_risk]
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

def _existed_for_window(restaurant_id, local_date, window_open_hour=None) -> bool:
    """Whether the restaurant existed when `local_date`'s window opened. A
    window that opened before the restaurant was created was never owed
    (10/16/26: the showcase, created at 5:55pm Chicago, was first seen at
    6pm, after every window of its first day had closed - five "missed"
    windows in the ops digest). `restaurants.created_at` is Chicago time;
    it is read in the restaurant's own zone. With no opening hour, the
    restaurant's first day is never a miss. True when unreadable."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from models import get_conn
        conn = get_conn()
        try:
            row = conn.execute("SELECT created_at, timezone FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        finally:
            conn.close()
        if not row or not row["created_at"]:
            return True
        made = datetime.fromisoformat(str(row["created_at"]).replace("Z", "")[:19])
        if made.tzinfo is None:
            made = made.replace(tzinfo=ZoneInfo("America/Chicago"))
        made = made.astimezone(ZoneInfo(row["timezone"] or "America/Chicago")).replace(tzinfo=None)
        day = str(local_date)[:10]
        if window_open_hour is None:
            return made.date().isoformat() < day
        return made <= datetime.fromisoformat(day).replace(hour=int(window_open_hour))
    except Exception:
        return True


def _since_ct(since) -> str:
    """The digest's window start in Chicago time, M/D/YY (10/17/26: it read
    "2026-10-16 13:00:43 UTC")."""
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        from time_utils import mdy
        t = datetime.fromisoformat(str(since).replace("Z", "")[:19]).replace(tzinfo=timezone.utc)
        t = t.astimezone(ZoneInfo("America/Chicago"))
        return f"{mdy(t.date().isoformat())} {t.strftime('%I:%M%p').lstrip('0').lower()} CT"
    except Exception:
        return f"{since} UTC"


def record_missed_window(job, restaurant_id, local_date, detail=None, window_open_hour=None) -> bool:
    """Record that `job` was due for this restaurant on its `local_date`
    and its local window closed with nothing sent — once per (job,
    restaurant, date). Nothing recorded "window closed, not sent", so a
    brief a long pass pushed past 2pm local looked like a quiet morning.
    A window that opened before the restaurant existed is not recorded
    (_existed_for_window)."""
    if restaurant_id is not None and not _existed_for_window(restaurant_id, local_date, window_open_hour):
        return False
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


# How recent the snapshot a prune relies on must be (memory re-audit
# 9/29/26, FORGET-10): the prune runs straight after the 2am backup, so the
# pruned rows are in last night's snapshot — but only if that snapshot was
# taken.
PRUNE_NEEDS_BACKUP_WITHIN_HOURS = 26


def prune_backup_gate(db_path=None) -> tuple:
    """(ok, why) — whether a retention prune may delete tonight: the newest
    backup_runs row must have written its local snapshot (local_ok=1) and
    have started within PRUNE_NEEDS_BACKUP_WITHIN_HOURS. A failed snapshot,
    no backup at all, or an unreadable ledger holds the prune (fail closed:
    a row deleted with no copy anywhere cannot come back). Never raises."""
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            last = conn.execute(
                "SELECT local_ok, started_at, (julianday('now') - julianday(started_at)) * 24.0 AS age "
                "FROM backup_runs ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
    except Exception as e:
        return False, f"the backup ledger is unreadable: {e}"
    if not last:
        return False, "no backup has run"
    if not last["local_ok"]:
        return False, f"the newest backup ({last['started_at']}) did not write its snapshot"
    if last["age"] is None or float(last["age"]) > PRUNE_NEEDS_BACKUP_WITHIN_HOURS:
        return False, f"the newest good snapshot ({last['started_at']}) is over {PRUNE_NEEDS_BACKUP_WITHIN_HOURS} hours old"
    return True, None


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


# The newest drafts a week's footprint is measured over (storage_per_week).
STORAGE_SAMPLE_WEEKS = 200


def storage_per_week(db_path=None) -> dict:
    """What one generated week costs the database, measured over the newest
    STORAGE_SAMPLE_WEEKS drafts (blind re-audit 10/11/26 SCALE-14: the
    volume's ceiling is bytes per restaurant-week times the fleet, and
    nothing measured it): {weeks, history_bytes, versions_bytes,
    detail_bytes, total_bytes} — the draft row's stored text, its versions'
    and its stored detail (quality_blobs), each an average per week.
    {weeks: 0} with no drafts. Never raises."""
    out = {"weeks": 0, "history_bytes": None, "versions_bytes": None, "detail_bytes": None, "total_bytes": None}
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
    except Exception:
        return out
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_history)")}
        text = [c for c in ("schedule_csv", "published_csv", "quality_json", "review_json", "summary_json",
                            "what_if_json", "economics_json", "weather_json", "stage_seconds_json") if c in cols]
        if not text:
            return out
        width = " + ".join(f"COALESCE(length({c}), 0)" for c in text)
        refs = ", quality_ref, review_ref" if {"quality_ref", "review_ref"} <= cols else ""
        rows = conn.execute(f"SELECT id, {width} AS b{refs} FROM schedule_history ORDER BY id DESC LIMIT ?",
                            (STORAGE_SAMPLE_WEEKS,)).fetchall()
        if not rows:
            return out
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        vers = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(length(schedule_csv), 0) + COALESCE(length(quality_json), 0) "
            "+ COALESCE(length(diff_json), 0) + COALESCE(length(delta_json), 0)), 0) FROM schedule_versions "
            f"WHERE history_id IN ({marks})", ids).fetchone()[0] or 0
        hashes = {h for r in rows for h in ((r["quality_ref"], r["review_ref"]) if refs else ()) if h}
        detail = 0
        if hashes:
            hl = list(hashes)
            detail = conn.execute(f"SELECT COALESCE(SUM(length(body)), 0) FROM quality_blobs "
                                  f"WHERE hash IN ({','.join('?' * len(hl))})", hl).fetchone()[0] or 0
        n = len(rows)
        out.update(weeks=n, history_bytes=int(sum(int(r["b"] or 0) for r in rows) / n),
                   versions_bytes=int(vers / n), detail_bytes=int(detail / n))
        out["total_bytes"] = out["history_bytes"] + out["versions_bytes"] + out["detail_bytes"]
    except Exception as e:
        log.warning(f"storage per week not measured: {e}")
    finally:
        conn.close()
    return out


def storage_trend(days=30, db_path=None) -> dict:
    """The database, WAL and backup sizes recorded by each night's backup,
    for a days-to-full line on Engineering (#28): {rows, growth_bytes_per_day,
    days_to_full, per_week} — per_week what one generated week costs
    (storage_per_week, SCALE-14). Never raises."""
    out = {"rows": [], "growth_bytes_per_day": None, "days_to_full": None,
           "per_week": storage_per_week(db_path)}
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
