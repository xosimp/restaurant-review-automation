"""
ops.py — make swallowed failures visible.

Nearly every background job wraps its work in `except Exception: log.error(...)`,
which keeps one bad restaurant from killing the loop — but Railway logs are the
only place the error lands, Sentry never hears about handled exceptions, and
nobody reads logs until a client complains. Every silent failure now flows
through capture(): recorded in a job_failures table, forwarded to Sentry when
configured, and rolled up into a daily 8am digest email if anything failed.
"""
import collections.abc
import logging
import sqlite3
import threading
import uuid
import os
import config


from emails import html_document as _html_doc  # one definition; emails reads its env lazily

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


def capture(exc, job="unknown", context="", db_path=None):
    """Record a handled exception. Never raises — an error reporter that can
    take down the thing it's reporting on is worse than none.

    db_path defaults to None (models.DB_PATH, the one real database) rather
    than being silently forced there — a caller running against its own
    database (every isolated test fixture) can now say so, instead of this
    always writing into whichever database happens to be the default in
    that process."""
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(exc)
    except Exception:
        pass
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
        _ensure_table(conn)
        # Redacted before it is stored (MOD-REV-8): a requests error carries
        # the URL it failed on, and every Places URL carries key=. This table
        # is shown in the admin console and mailed in the failure digest.
        from ai_guard import redact_secrets
        conn.execute(
            "INSERT INTO job_failures (job, error, context) VALUES (?,?,?)",
            (str(job)[:100], redact_secrets(str(exc))[:500], redact_secrets(str(context))[:200]),
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
    context TEXT,
    result_json TEXT,
    claim_job TEXT
)
"""
# Added to job_runs after it shipped: the sweep's counts (result_json) and
# the claim_period job key the run was claimed under (claim_job), so a dead
# run is reclaimed even where the claim key and the job name differ (DH2-16).
# ALTERed at boot by init_ops; a database that already has them skips.
_RUNS_COLUMNS = (("result_json", "TEXT"), ("claim_job", "TEXT"))

# job_runs.ok: 1 ran clean, 0 failed (raised, or every restaurant it
# attempted failed), 2 PARTIAL — some restaurants failed or the time bound
# cut it short (DH2-1: a night on which every POS sync failed read ok=1).
RUN_OK, RUN_FAILED, RUN_PARTIAL = 1, 0, 2
_RESULT_KEYS = ("attempted", "ok", "failed", "skipped", "hit_bound", "held", "retried")
_SUCCESS_KEYS = ("ok", "analysed", "checked", "synced", "written")


def run_outcome(result):
    """(job_runs.ok, result_json or None) for what a job returned. A dict
    carrying the sweep counts (_RESULT_KEYS) is judged by them: nothing
    failed and no bound hit is 1; every attempt failed is 0; anything
    between — or a pass the time bound cut short — is 2 (partial). Any
    other return value is a clean run."""
    import json as _json
    if not isinstance(result, dict) or not any(k in result for k in _RESULT_KEYS):
        return RUN_OK, None
    counts = {k: result.get(k) for k in _RESULT_KEYS if k in result}
    try:
        failed = int(result.get("failed") or 0)
        attempted = int(result.get("attempted") or 0)
        if "attempted" not in result and failed:
            # Sweeps that count their successes by their own name
            # ({"analysed": 2, "failed": 1}): attempted is the two together.
            attempted = failed + sum(int(v) for k, v in result.items()
                                     if k in _SUCCESS_KEYS and isinstance(v, int) and not isinstance(v, bool))
    except (TypeError, ValueError):
        failed, attempted = 0, 0
    state = RUN_OK
    if failed and attempted and failed >= attempted:
        state = RUN_FAILED
    elif failed or result.get("hit_bound"):
        state = RUN_PARTIAL
    try:
        blob = _json.dumps(counts, default=str)[:500]
    except (TypeError, ValueError):
        blob = None
    return state, blob


def _record_run_start(name, context="", db_path=None, claim=None):
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
        conn.execute(_RUNS_SQL)
        try:
            cur = conn.execute("INSERT INTO job_runs (job, context, claim_job) VALUES (?, ?, ?)",
                               (str(name)[:100], str(context)[:200], (str(claim)[:100] if claim else None)))
        except sqlite3.OperationalError:
            # A database booted before claim_job existed (init_ops adds it).
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

# A claim whose run started and never finished is taken to be dead after
# this long, and the period may be claimed again (DATA-20). A run still
# alive in THIS process is never reclaimed whatever its age (_running_jobs),
# and one in another process can only be reclaimed after that process has
# lost the scheduler lease, which a live runner renews all through a long
# pass (scheduler._TickPulse).
CLAIM_RECLAIM_MINUTES = int(os.getenv("CLAIM_RECLAIM_MINUTES", "120"))

# How many runs of each job name run_job is executing in this process.
_running_jobs = {}
_running_lock = threading.Lock()


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
        for sql in (_TABLE_SQL, _RUNS_SQL, _PERIOD_CLAIM_SQL, _ASYNC_JOB_SQL, _LEASE_SQL):
            conn.execute(sql)
        have = {r[1] for r in conn.execute("PRAGMA table_info(job_runs)")}
        for col, typ in _RUNS_COLUMNS:
            if col not in have:
                conn.execute(f"ALTER TABLE job_runs ADD COLUMN {col} {typ}")
        for sql in (
            # The retention deletes in prune_ledgers (DATA-40), and the
            # dead-run lookup in _reclaim_dead_run.
            "CREATE INDEX IF NOT EXISTS idx_job_failures_created ON job_failures(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_job_runs_started ON job_runs(started_at)",
            "CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs(job, started_at)",
            "CREATE INDEX IF NOT EXISTS idx_job_period_claims_at ON job_period_claims(claimed_at)",
        ):
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


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


def _reclaim_dead_run(conn, job, key):
    """True if `key` was claimed by a run of `job` that started and never
    finished, long enough ago to be dead — the claim is then taken over
    (restamped) for this caller. A deploy SIGKILLs mid-run: the claim row
    stands, job_runs holds a start and no finish, and without this the job
    did not run again until its next period — for a daily job, a whole day
    of alerts that never went out (DATA-20).

    Only a claim with that evidence is reclaimed. One with no run row at
    all, or whose run finished (even with an error), is left alone:
    re-sending a digest is worse than missing one."""
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
            "SELECT SUM(finished_at IS NULL), SUM(finished_at IS NOT NULL) FROM job_runs "
            "WHERE (job=? OR claim_job=?) AND started_at >= datetime(?, '-5 minutes')",
            (job, job, claimed_at)).fetchone()
    except sqlite3.OperationalError:
        runs = conn.execute(
            "SELECT SUM(finished_at IS NULL), SUM(finished_at IS NOT NULL) FROM job_runs "
            "WHERE job=? AND started_at >= datetime(?, '-5 minutes')", (job, claimed_at)).fetchone()
    if not runs or not runs[0] or runs[1]:
        return False
    cur = conn.execute("UPDATE job_period_claims SET claimed_at=datetime('now') "
                       "WHERE job_key=? AND claimed_at=?", (key, claimed_at))
    conn.commit()
    if cur.rowcount == 1:
        log.error(f"claim_period({key}): the run claimed at {claimed_at} never finished — reclaimed")
        return True
    return False


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
    restarts. Only a duplicate key means "already claimed". Any other
    failure to write it (a full, read-only or locked volume) used to be read
    the same way, so every scheduled job silently stopped for as long as it
    lasted, with nothing logged (DATA-22).

    The fall-back for a claim that cannot be written is a process-local memo
    rather than an open door. acquire_scheduler_lease fails open on the same
    dependency, so "allow every time" would re-run every due job on every
    tick — twelve identical digests an hour. The memo bounds a bookkeeping
    outage to one run per job per period per process while still letting
    the work happen, and a period run from it is written to the table (and
    refused) once the database answers again.

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


def run_job(name, fn, *args, context="", db_path=None, claim=None, **kwargs):
    """Run a scheduled job with failure capture. Returns the job's result,
    or None if it raised. Every run — not only the failures — lands in
    job_runs (start, end, duration, ok), which is what the admin console's
    Jobs page reads; before this the only record of a job that worked was
    the scheduler's heartbeat.

    A job that returns its sweep counts ({attempted, ok, failed, skipped,
    hit_bound}) is recorded by them (run_outcome): ok=2 (partial) when some
    restaurants failed or the bound cut the pass short, ok=0 when every one
    it attempted failed — never a green run over a night nothing synced
    (DH2-1). `claim` is the claim_period job key the run was claimed under,
    stored with it so a dead run is reclaimable by that key (DH2-16)."""
    import time as _time
    started = _time.time()
    run_id = _record_run_start(name, context, db_path=db_path, claim=claim)
    names = [name] + ([claim] if claim and claim != name else [])
    with _running_lock:
        for n in names:
            _running_jobs[n] = _running_jobs.get(n, 0) + 1
    try:
        result = fn(*args, **kwargs)
        state, blob = run_outcome(result)
        err = None
        if state != RUN_OK and isinstance(result, dict):
            err = (f"{result.get('failed') or 0} of {result.get('attempted') or 0} failed"
                   + (" · stopped at its time bound" if result.get("hit_bound") else ""))
        _record_run_end(run_id, started, state, err, db_path=db_path, result_json=blob)
        return result
    except Exception as e:
        log.error(f"Job '{name}' crashed: {e}")
        capture(e, job=name, db_path=db_path)
        _record_run_end(run_id, started, RUN_FAILED, e, db_path=db_path)
        return None
    finally:
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


# ── the jobs that should have run (DH2-2) ────────────────────────────────────
#
# Nothing compared "jobs that should have run by now" against job_runs: a
# loop that died after stamping its heartbeat, or a job whose gate never
# opened, left no failure row and no stuck row. This table is the SLA — job
# name → the most hours that may pass between SUCCESSFUL (ok 1 or partial 2)
# runs — and jobs_overdue() is read from a REQUEST thread (/health, the admin
# overview), never from the scheduler it is watching.
EXPECTED_JOBS = {
    "pos_sync": 26, "loss_sync": 26, "inventory_depletion": 26, "food_cost_snapshots": 26,
    "marketing_metrics_sync": 26, "backup_db": 26, "data_health_daily": 26,
    # 8am, 12pm, 4pm, 8pm Chicago: the overnight gap is 12 hours, plus a
    # bounded pass of up to three.
    "review_fetch": 16,
    "daily_alerts": 3, "dsr_sweep": 1, "intraday_capture": 2, "pos_retry": 3,
    "competitor_analysis": 8 * 24, "ai_visibility": 8 * 24,
}
# Heartbeat older than this is a dead or wedged loop (the pulse stamps it
# during long jobs, so a live runner is never this old).
HEARTBEAT_ALERT_MINUTES = 15
# One out-of-band alert per this many minutes, however many requests see it.
PLATFORM_ALERT_COOLDOWN_MINUTES = 60


def jobs_overdue(now=None, db_path=None) -> list:
    """[{job, max_hours, last_ok_at, hours_since}] for every EXPECTED_JOBS
    entry whose last successful run is older than its SLA. A job that has
    never succeeded counts only once job_runs is older than its SLA (a fresh
    database is not "overdue"). `now` is a UTC "YYYY-MM-DD HH:MM:SS" (tests);
    None is SQLite's now. Never raises: [] when unreadable."""
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
        rows = {r[0]: r[1] for r in conn.execute(
            "SELECT job, MAX(finished_at) FROM job_runs WHERE ok IN (1, 2) AND finished_at IS NOT NULL "
            "GROUP BY job").fetchall()}
        for job, hours in EXPECTED_JOBS.items():
            last = rows.get(job)
            base = last or first
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


def check_platform_sla(send=True, db_path=None) -> dict:
    """The scheduler's watchdog, run from a REQUEST thread: {heartbeat_minutes,
    jobs_overdue, alerted}. When the heartbeat is older than
    HEARTBEAT_ALERT_MINUTES or any expected job is overdue, Will gets one
    email and push per PLATFORM_ALERT_COOLDOWN_MINUTES (claim_cooldown) —
    out of band, because the failure digest is itself a job inside the loop
    that has stopped. Only where the scheduler is meant to run
    (scheduler.scheduling_allowed): a laptop has no scheduler and must not
    page anyone. Owners are never contacted from here. Never raises."""
    out = {"heartbeat_minutes": None, "jobs_overdue": [], "alerted": False}
    try:
        from status_manager import scheduler_heartbeat_age_minutes
        out["heartbeat_minutes"] = scheduler_heartbeat_age_minutes()
    except Exception:
        pass
    out["jobs_overdue"] = jobs_overdue(db_path=db_path)
    hb = out["heartbeat_minutes"]
    problems = []
    if hb is not None and hb > HEARTBEAT_ALERT_MINUTES:
        problems.append(f"Scheduler heartbeat is {int(hb)} minutes old — nothing scheduled is running.")
    for j in out["jobs_overdue"]:
        problems.append(f"{j['job']}: no successful run in {j['hours_since']:g}h (expected within {j['max_hours']}h)")
    if not problems or not send:
        return out
    try:
        import scheduler as _sched
        if not _sched.scheduling_allowed():
            return out
    except Exception:
        return out
    if not claim_cooldown("platform_sla_alert", PLATFORM_ALERT_COOLDOWN_MINUTES):
        return out
    out["alerted"] = alert_will("Cavnar AI: scheduled jobs are not running", problems)
    return out


def alert_will(subject, lines) -> bool:
    """Tell Will now, outside the scheduler: an email to config.will_email()
    and a push to the admin logins' phones. Never an owner, never an SMS.
    True when either went out."""
    import html as _html
    sent = False
    try:
        import emails as _emails
        b = _emails.BRAND
        body = "".join(f'<li style="margin:0 0 6px">{_html.escape(str(x))}</li>' for x in lines[:12])
        res = _emails._send_branded(
            config.will_email(), subject, from_label="Cavnar AI Ops", email_type="ops_platform_alert",
            inner_html=(f'<p style="color:{b["strong"]};font-size:16px;font-weight:700;margin:0 0 12px">'
                        f'{_html.escape(subject)}</p><ul style="color:{b["body"]};font-size:14px;'
                        f'line-height:1.5;margin:0 0 0 18px">{body}</ul>'))
        sent = bool(getattr(res, "ok", res))
    except Exception as e:
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
        for rid, ids in by_rid.items():
            if _push.fire_push(rid, "platform_alert", subject, str(lines[0])[:180] if lines else subject,
                               data={"tab": "home"}, user_ids=ids):
                sent = True
    except Exception as e:
        log.error(f"alert_will push failed: {e}")
    return sent


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


def release_scheduler_lease(owner: str = None) -> bool:
    """Give the lease up on a clean exit, so the next process takes over on
    its next tick instead of waiting out SCHEDULER_LEASE_STALE_SECONDS.

    Nothing did this, so every redeploy — Railway starts the new container,
    then SIGTERMs the old one — left the new process idling for up to 30
    minutes behind a dead holder's lease: no fetches, alerts or briefs in
    that window, repeated on every push. Only releases a lease this process
    actually holds.
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


# Ledgers that only ever grew. Audit #7 found seven of them: ai_usage (read
# by the budget check on every AI call), job_runs, job_failures, alert_log,
# push_deliveries, webhook_deliveries and email_log. Nothing pruned any of
# them, on SQLite, on a volume whose only protection against filling up is
# the status check added in audit #6.
#
# Retention is per-table because they are not equally useful old: the budget
# only ever asks about this month, but a year of email history is worth
# keeping for a billing dispute.
_RETENTION_DAYS = {
    "ai_usage":            int(os.getenv("RETAIN_AI_USAGE_DAYS", "120")),
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
    "job_period_claims": int(os.getenv("RETAIN_JOB_CLAIMS_DAYS", "45")),
    # A held alert keeps its whole email body (guest review excerpts
    # included) and is sent or dropped within a day; notification_opens
    # feeds a 30-day engagement read. Neither was ever pruned (MOD-NOT-14).
    "alert_holds":        int(os.getenv("RETAIN_ALERT_HOLDS_DAYS", "30")),
    # Tap de-duplication only needs the last half hour (marketing_links).
    "marketing_link_taps": int(os.getenv("RETAIN_LINK_TAPS_DAYS", "2")),
    "notification_opens": int(os.getenv("RETAIN_NOTIFICATION_OPENS_DAYS", "365")),
}

# Each table's own timestamp column — they do not agree on a name.
_RETENTION_COLUMN = {
    "ai_usage": "created_at", "job_runs": "started_at", "job_failures": "created_at",
    "push_deliveries": "created_at", "webhook_deliveries": "created_at",
    "alert_log": "fired_at", "email_log": "sent_at",
    "ai_visibility_query_runs": "created_at", "competitor_snapshots": "captured_at",
    "ai_visibility_runs": "created_at", "job_period_claims": "claimed_at",
    "alert_holds": "created_at", "notification_opens": "opened_at",
    "marketing_link_taps": "tapped_at",
}
# Every table above has an index on its column, created where the table is
# (DATA-40): these deletes run under the write lock, and a full scan of a
# year of email_log there stalls every request that writes.


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


def prune_ledgers(db_path=None):
    """Delete rows past their retention window. Returns {table: rows_deleted}.

    Deliberately tolerant: a table that does not exist yet, or whose stamp
    column is named something else on an older database, is skipped rather
    than taking the whole sweep down with it.
    """
    from models import get_conn, DB_PATH
    deleted = {}
    try:
        conn = get_conn(db_path or DB_PATH)
    except Exception as e:
        log.error(f"prune_ledgers could not open the database: {e}")
        return deleted
    try:
        for table, days in _RETENTION_DAYS.items():
            if days <= 0:
                continue            # 0 disables retention for that table
            col = _RETENTION_COLUMN.get(table, "created_at")
            try:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE {col} < datetime('now', ?)", (f"-{days} days",)
                )
                conn.commit()
                if cur.rowcount and cur.rowcount > 0:
                    deleted[table] = cur.rowcount
            except Exception as e:
                # Missing table or renamed column — not worth failing the sweep.
                log.debug(f"prune_ledgers skipped {table}: {e}")
        try:
            n = _prune_inventory_history(conn)
            if n:
                deleted["inventory_history"] = n
        except Exception as e:
            log.debug(f"prune_ledgers skipped inventory_history: {e}")
    finally:
        conn.close()
    if deleted:
        log.info(f"Pruned old rows: {deleted}")
    return deleted


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
    console already lists these; this is what puts them in the mail. Since
    DATA-20, claim_period also acts on it: the period is reclaimed, and the
    job re-run, once the claim is CLAIM_RECLAIM_MINUTES old.
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
    try:
        import security as _security
        sec_lines = _security.digest_lines()
    except Exception:
        sec_lines = []
    # A job that died without raising leaves no failure row, so "no failures"
    # was never the same thing as "nothing went wrong".
    if not failures and not stuck and not sec_lines:
        return False
    resend_key = os.getenv("RESEND_API_KEY", "")
    if not resend_key:
        log.warning("send_failure_digest: RESEND_API_KEY not set — skipping")
        return False
    try:
        import html as _html
        import emails as _emails_ops
        will = config.will_email()
        total = sum(f["cnt"] for f in failures)
        sec_html = ""
        if sec_lines:
            sec_html = ("<p style=\"font-size:13px;font-weight:600;color:#0e0c0a;margin:22px 0 6px\">Security, last 24 hours</p><ul style=\"font-size:13px;color:#3a3530;margin:0 0 10px 18px\">"
                        + "".join(f"<li>{_html.escape(x)}</li>" for x in sec_lines) + "</ul>")
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
  <p style="font-size:12px;color:#7a736a;margin:0 0 10px">These did not raise, so they are not counted above. Each is re-run once its claim is {CLAIM_RECLAIM_MINUTES} minutes old; until then its work for that period has not happened.</p>
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
        _emails_ops.deliver_or_raise(email_type="ops_failure_digest", payload={
            "from": _emails_ops.sender("ops"),
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
  </table>{sec_html}{stuck_html}
  <p style="font-size:12px;color:#7a736a;margin-top:16px">Full stack traces are in Sentry (if configured) and Railway logs.</p>
</div>
</div>"""),
        })
        log.info(f"Failure digest sent to {will} ({total} failures)")
        return True
    except Exception as e:
        log.error(f"send_failure_digest failed: {e}")
        return False
