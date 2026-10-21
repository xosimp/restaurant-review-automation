"""
rpower_health.py — one precomputed health row per RPOWER store, and the
fleet's reading of them (Audit #23, 10/21/26: A6-01, A4-10 and the rules of
A6 section 3.6).

Each store had one stamp, `rpower_last_synced`, and it moved only on the
nightly LABOR pull: the ticket archive, the change read, the catalogs and
prices, the live reads, the loss and DSR reads and the queue rows each failed
in their own place, so the console read "RPOWER connected, fresh" while Food
Cost, Bar and the DSR backfill read holes. Nothing here is computed on a
request:

  * `rpower_store_health` — one row per store bound to RPOWER (created at
    boot, `init_rpower_health`): the last good time of each data type
    (labor pull, archive night, change read, catalog, prices, live, intraday,
    loss, DSR), settled coverage over 90 days, gaps in the last 14, the
    backfill slot, failures and the next retry, the store's queue rows and
    request ledger over 24 hours, the anomalies of the last 7 nights (recon
    gap, unknown-type dollars, suspect nights, pay gap, first-read suspect,
    open punches, zero-labor trading days, DSR/POS mismatch, data-quality
    findings), the integrity checks (one store bound twice, a night archived
    from another store, a simulated token on a non-demo account), and the
    health state with its severity rank and the reasons.
  * `rpower_fleet_health_daily` — one row per day for the fleet (counts by
    health, failing and stale per data type, coverage p10/p50, requests,
    429 / skipped, queue failed and expired by kind, auth failures and the
    token buckets they span), rewritten by every pass; kept 400 days
    (ops._RETENTION_DAYS).

Both are written by the `rpower_health_rollup` job every 15 minutes
(jobs_registry; bounded by ROLLUP_BOUND_SECONDS, resumable through the
job_cursors row CURSOR_KEY, and each batch of BATCH stores read with one
grouped query per source table — never a query per store). The console's
RPOWER view (`page`, `store_detail`), the fleet alert rules (`fleet_rules`,
read by ops.check_platform_sla and admin_ops), pos_health.fleet_state and the
public status page read only these tables.

The auth latch (`restaurants.rpower_auth_failed_at`, set by the request
layer on any 401/403) is surfaced here: the store's health is `auth`, the
console raises it as a critical issue, and the owner is told once — email and
push through notify.raise_alert, the Google reconnect pattern — on the
transition into it (`_notify_auth`, only where scheduler.scheduling_allowed),
unless the token is shared by other failing stores (a partner token the
owner does not hold: that is the operator's, one platform issue).

Imports nothing from the app at module scope.
"""
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("rpower_health")

PROVIDER = "rpower"
CURSOR_KEY = "rpower_health_cursor"
BATCH = 200
ROLLUP_BOUND_SECONDS = 120
WINDOW_DAYS = 90
GAP_DAYS = 14
ANOMALY_DAYS = 7
# A store's queue kinds (pos_sync and live_labor are queue rows from F5 of
# this round; counting a kind that never appears costs nothing).
QUEUE_KINDS = ("pos_archive", "pos_onboard", "pos_sync", "pos_schedule_push", "live_labor")
# The kinds whose expired rows are recorded as missed windows (F5).
MISSED_JOBS = ("pos_archive", "pos_onboard", "pos_sync")
DATA_TYPES = ("labor", "archive", "change", "catalog", "prices", "live", "intraday", "loss", "dsr")
SOURCE_OF = {"pos_intraday": "intraday", "loss": "loss", "dsr": "dsr"}

# How old a data type's last good read may be before it is stale.
STALE_HOURS = {"labor": 48, "archive": 48, "change": 72, "catalog": 72, "prices": 72,
               "intraday": 48, "loss": 48, "dsr": 48}
# A store is onboarding until its backfill slot finishes, or for this long
# after it was bound with no archived night yet.
ONBOARDING_DAYS = 3
# A night counts as owed once it is this long past its business date's end.
GAP_GRACE_HOURS = 24
# A recon or pay gap matters past max(this, NIGHT_GAP_SHARE of the night's net).
NIGHT_GAP_DOLLARS = 25.0
NIGHT_GAP_SHARE = 0.005
UNKNOWN_SALES_SHARE = 0.01

HEALTH_RANK = {"auth": 5, "failing": 4, "stale": 3, "watch": 2, "onboarding": 1, "ok": 0}
HEALTH_STATES = tuple(sorted(HEALTH_RANK, key=lambda h: -HEALTH_RANK[h]))

# ── the fleet rules (A6 section 3.6) ────────────────────────────────────────
FLEET_FAILING_SHARE = 0.05          # > 5% of connected stores failing a data type ...
FLEET_FAILING_MIN = 5               # ... and at least this many ...
FLEET_FAILING_SUSTAIN_MINUTES = 30  # ... each failing this long
FLEET_FAILING_URGENT_SHARE = 0.20
FLEET_STALE_SHARE = 0.10            # > 10% stale past 48 hours on archive or labor
FLEET_MIN_STORES = 2                # one store's own trouble stays that store's issue
FLEET_GAP_SHARE = 0.02              # > 2% of stores with an archive gap
AUTH_WAVE_STORES = 3                # newly auth-failed within AUTH_WAVE_MINUTES ...
AUTH_WAVE_BUCKETS = 2               # ... or spanning this many token buckets
AUTH_WAVE_MINUTES = 60
BUDGET_SKIPPED_SHARE = 0.05         # skipped / requests over the last hour
BUDGET_429_SHARE = 0.01             # 429 / requests over the last hour
BUDGET_MIN_REQUESTS = 50
VOLUME_CONSOLE_X = 1.5              # requests a day against the 7-day median
VOLUME_PAGE_X = 2.0
VOLUME_STORE_X = 10.0
QUEUE_FAIL_SHARE = 0.02             # pos_archive failed + expired / rows in 24 hours
QUEUE_FAIL_MIN = 2
STORE_FAILING_CRITICAL_DAYS = 4
FRESH_ROLLUP_MINUTES = 60           # a reader trusts the table this long after a pass

_TABLE_SQL = (
    """CREATE TABLE IF NOT EXISTS rpower_store_health (
        restaurant_id        INTEGER PRIMARY KEY,
        name                 TEXT,
        is_demo              INTEGER NOT NULL DEFAULT 0,
        in_service           INTEGER NOT NULL DEFAULT 1,
        billing_status       TEXT,
        partner_id           INTEGER,
        cg                   TEXT,
        store_mid            TEXT,
        token_bucket         TEXT,
        tz                   TEXT,
        connection           TEXT,
        token_state          TEXT,
        auth_failed_at       TEXT,
        last_labor_ok        TEXT,
        last_archive_night   TEXT,
        last_archived_at     TEXT,
        last_change_read     TEXT,
        last_catalog_ok      TEXT,
        last_prices_ok       TEXT,
        last_live_ok         TEXT,
        live_state           TEXT,
        last_intraday_ok     TEXT,
        last_loss_ok         TEXT,
        last_dsr_ok          TEXT,
        type_states          TEXT,
        failing_types        TEXT,
        stale_types          TEXT,
        expected_90d         INTEGER,
        archived_90d         INTEGER,
        coverage_90d         REAL,
        gaps_14d             INTEGER,
        gap_dates            TEXT,
        backfill_state       TEXT,
        backfill_reason      TEXT,
        consecutive_failures INTEGER,
        first_failed_at      TEXT,
        last_error           TEXT,
        error_class          TEXT,
        next_retry_at        TEXT,
        queue_queued         INTEGER,
        queue_rows_24h       INTEGER,
        queue_failed_24h     INTEGER,
        queue_expired_24h    INTEGER,
        req_24h              INTEGER,
        err_24h              INTEGER,
        s429_24h             INTEGER,
        skipped_24h          INTEGER,
        p95_ms_24h           INTEGER,
        req_median_7d        REAL,
        recon_nights_7d      INTEGER,
        unknown_sales_7d     REAL,
        net_sales_7d         REAL,
        suspect_nights_7d    INTEGER,
        pay_gap_nights_7d    INTEGER,
        first_read_suspect_7d INTEGER,
        open_punches_7d      INTEGER,
        zero_labor_days_7d   INTEGER,
        dsr_mismatch_7d      INTEGER,
        data_issues_7d       INTEGER,
        duplicate_binding    INTEGER NOT NULL DEFAULT 0,
        store_key_mismatch   INTEGER NOT NULL DEFAULT 0,
        simulated_not_demo   INTEGER NOT NULL DEFAULT 0,
        has_anomaly          INTEGER NOT NULL DEFAULT 0,
        recipe_coverage_pct  REAL,
        missed_runs_7d       INTEGER,
        recipe_coverage_at   TEXT,
        health               TEXT NOT NULL DEFAULT 'ok',
        health_since         TEXT,
        severity_rank        INTEGER NOT NULL DEFAULT 0,
        reasons              TEXT,
        updated_at           TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_rpower_store_health_rank ON rpower_store_health(severity_rank, restaurant_id)",
    "CREATE INDEX IF NOT EXISTS idx_rpower_store_health_health ON rpower_store_health(health, severity_rank, restaurant_id)",
    "CREATE INDEX IF NOT EXISTS idx_rpower_store_health_bucket ON rpower_store_health(token_bucket)",
    "CREATE INDEX IF NOT EXISTS idx_rpower_store_health_partner ON rpower_store_health(partner_id)",
    "CREATE INDEX IF NOT EXISTS idx_rpower_store_health_cg ON rpower_store_health(cg)",
    """CREATE TABLE IF NOT EXISTS rpower_fleet_health_daily (
        date                 TEXT PRIMARY KEY,
        stores               INTEGER NOT NULL DEFAULT 0,
        by_health            TEXT,
        failing_by_type      TEXT,
        stale_by_type        TEXT,
        coverage_p10         REAL,
        coverage_p50         REAL,
        nights_expected      INTEGER,
        nights_archived      INTEGER,
        recon_nights         INTEGER,
        pay_gap_nights       INTEGER,
        auth_failed          INTEGER,
        auth_buckets         INTEGER,
        requests             INTEGER,
        s429                 INTEGER,
        skipped              INTEGER,
        errors               INTEGER,
        queue_failed         INTEGER,
        queue_expired        INTEGER,
        duplicate_bindings   INTEGER,
        detail_json          TEXT,
        updated_at           TEXT
    )""",
)


def _conn(db_path=None):
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def init_rpower_health(db_path=None):
    """The two tables and their indexes — boot only (models.init_db)."""
    conn = _conn(db_path)
    try:
        for stmt in _TABLE_SQL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


# ── small readers ──────────────────────────────────────────────────────────

def _utcnow():
    return datetime.now(timezone.utc)


def _stamp(dt):
    """A UTC datetime as the tables' 'YYYY-MM-DD HH:MM:SS', or None."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse(value):
    """Any stored stamp (text, or epoch seconds) as an aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        from time_utils import parse_stamp
        return parse_stamp(str(value), naive_tz="UTC")
    except Exception:
        return None


def _hours_since(value, now):
    dt = _parse(value)
    return None if dt is None else (now - dt).total_seconds() / 3600.0


def _ids_sql(ids):
    return ",".join("?" for _ in ids)


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def _safe_rows(conn, label, sql, args=(), fallback_sql=None):
    """Rows, or [] (a table a fresh database has not got yet): logged, never
    raised — one unreadable source must not stop the other columns."""
    try:
        return _rows(conn, sql, args)
    except Exception as e:
        if fallback_sql:
            try:
                return _rows(conn, fallback_sql, args)
            except Exception as e2:
                e = e2
        log.warning("rpower_health: %s unreadable: %s", label, e)
        return []


def _local_now(tz, now):
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo(tz or "America/Chicago"))
    except Exception:
        return now.astimezone(timezone(timedelta(hours=-5)))


def _closures(compliance_json):
    try:
        data = json.loads(compliance_json or "{}") or {}
    except (TypeError, ValueError):
        data = {}
    names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    days = {names.index(d) for d in (data.get("closed_weekdays") or []) if d in names}
    dates = {str(d)[:10] for d in (data.get("closed_dates") or [])}
    return days, dates


def _token_bucket(raw_token):
    """The shared-budget bucket a store's token is in (rpower._bucket_key: a
    hash, never the token), so a shared token's failures group together."""
    if not raw_token:
        return None
    try:
        import credentials
        tok = credentials.decrypt(raw_token)
    except Exception:
        tok = None
    if not tok:
        return None
    try:
        import rpower
        return rpower._bucket_key(tok)
    except Exception:
        import hashlib
        return "rpower:" + hashlib.sha256(str(tok).encode()).hexdigest()[:24]


def _is_simulated_token(raw_token):
    try:
        import credentials
        tok = credentials.decrypt(raw_token) if raw_token else None
        import demo_store
        return bool(tok) and str(tok).startswith(demo_store.TOKEN_PREFIX)
    except Exception:
        return False


def _first_read_suspect(raw):
    """Whether a night's first-read verdict (pos_archive_days.first_read,
    JSON {verdict, reasons, at}, F2's A4-04 / A6-07) says the read looked
    short. Any verdict other than an all-clear counts; NULL is never judged."""
    if not raw:
        return False
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return False
    verdict = str((v or {}).get("verdict") or "").strip().lower() if isinstance(v, dict) else ""
    return bool(verdict) and verdict not in ("ok", "plausible", "pass", "passed", "normal", "clear")


def _night_gap(gap, net):
    try:
        g = abs(float(gap))
    except (TypeError, ValueError):
        return False
    return g > max(NIGHT_GAP_DOLLARS, NIGHT_GAP_SHARE * abs(float(net or 0)))


def _archived(row):
    """Whether a pos_archive_days row is a stored night. A row the archive
    wrote only to record a failed attempt (attempts/last_error with nothing
    stored, F2's A4-06) is not."""
    if not row.get("last_error"):
        return True
    return (int(row.get("tickets") or 0) + int(row.get("lines") or 0) + int(row.get("punches") or 0)) > 0


# ── the rollup ─────────────────────────────────────────────────────────────

_STORE_COLS = ("id", "name", "location_name", "is_demo", "billing_status", "timezone", "rpower_token", "rpower_cg",
               "rpower_store_mid", "rpower_last_synced", "rpower_sync_error", "rpower_verified_at",
               "rpower_auth_failed_at", "compliance_json", "created_at")


def _stores(conn):
    """Every restaurant bound to RPOWER (a token or a store id), narrow."""
    sql = (f"SELECT {', '.join(_STORE_COLS)} FROM restaurants WHERE id > 0 AND "
           "(COALESCE(rpower_token,'') <> '' OR COALESCE(rpower_store_mid,'') <> '') ORDER BY id")
    fallback = sql.replace(", rpower_auth_failed_at", "")
    return _safe_rows(conn, "restaurants", sql, (), fallback_sql=fallback)


def _cursor(conn):
    try:
        row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (CURSOR_KEY,)).fetchone()
        v = row[0] if row else None
        return int(v) if v is not None and str(v).isdigit() else None
    except Exception:
        return None


def _save_cursor(conn, rid):
    conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                 (CURSOR_KEY, str(int(rid))))
    conn.commit()


def _fleet_reads(conn, now):
    """The reads that are one query for the whole fleet per pass: the request
    ledger (24 hours per store with latency buckets, 8 days per store per
    day, the last hour fleet-wide), duplicate bindings, the catalogs per
    consolidation group, the partners, and the queue's rows by kind."""
    out = {"ledger": {}, "ledger_days": {}, "ledger_1h": {}, "dupes": set(), "catalogs": {}, "partners": {},
           "queue_kinds": {}}
    try:
        import pos_io
        pos_io.flush()
    except Exception as e:
        log.warning("rpower_health: ledger flush failed: %s", e)
    since24 = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:00:00")
    since8d = (now - timedelta(days=8)).strftime("%Y-%m-%d 00:00:00")
    since1h = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:00:00")
    counts = ("n", "ok", "s4xx", "s429", "s5xx", "transport", "skipped", "b250", "b1000", "b3000", "b10000", "bslow")
    sums = ", ".join(f"SUM({c}) AS {c}" for c in counts)
    for r in _safe_rows(conn, "pos_request_ledger", f"SELECT restaurant_id, {sums} FROM pos_request_ledger "
                        "WHERE provider=? AND hour >= ? GROUP BY restaurant_id", (PROVIDER, since24)):
        out["ledger"][int(r["restaurant_id"] or 0)] = r
    for r in _safe_rows(conn, "pos_request_ledger", "SELECT restaurant_id, substr(hour, 1, 10) AS day, SUM(n) AS n "
                        "FROM pos_request_ledger WHERE provider=? AND hour >= ? "
                        "GROUP BY restaurant_id, substr(hour, 1, 10)", (PROVIDER, since8d)):
        out["ledger_days"].setdefault(int(r["restaurant_id"] or 0), {})[r["day"]] = int(r["n"] or 0)
    one = _safe_rows(conn, "pos_request_ledger", "SELECT SUM(n) AS n, SUM(s429) AS s429, SUM(skipped) AS skipped "
                     "FROM pos_request_ledger WHERE provider=? AND hour >= ?", (PROVIDER, since1h))
    out["ledger_1h"] = {k: int((one[0] if one else {}).get(k) or 0) for k in ("n", "s429", "skipped")}
    for r in _safe_rows(conn, "restaurants", "SELECT rpower_cg, rpower_store_mid, COUNT(*) AS n FROM restaurants "
                        "WHERE COALESCE(is_demo,0)=0 AND COALESCE(rpower_store_mid,'') <> '' "
                        "GROUP BY rpower_cg, rpower_store_mid HAVING COUNT(*) > 1"):
        out["dupes"].add((str(r["rpower_cg"] or ""), str(r["rpower_store_mid"] or "")))
    for r in _safe_rows(conn, "pos_catalogs", "SELECT scope, path, fetched_at FROM pos_catalogs WHERE provider=?",
                        (PROVIDER,)):
        scope = str(r["scope"] or "")
        if scope.startswith("demo:"):
            scope = scope[5:]
        cg = scope.split(":", 1)[0]
        at = float(r["fetched_at"] or 0)
        if at and at > out["catalogs"].get(cg, 0):
            out["catalogs"][cg] = at
    for r in _safe_rows(conn, "partner_stores", "SELECT restaurant_id, partner_id FROM partner_stores"):
        out["partners"][int(r["restaurant_id"])] = r["partner_id"]
    since_q = _stamp(now - timedelta(hours=24))
    cutoff = time.time() - 86400
    # Every kind and its group (the queue groups pos_io, pos_bulk, live, ...).
    for r in _safe_rows(conn, "jobs", "SELECT kind, grp, COUNT(*) AS n, "
                        "SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed, "
                        "SUM(CASE WHEN state='expired' THEN 1 ELSE 0 END) AS expired "
                        "FROM jobs WHERE created_at >= ? GROUP BY kind, grp", (since_q,)):
        k = out["queue_kinds"].setdefault(r["kind"], {"grp": r["grp"], "rows_24h": 0, "failed_24h": 0,
                                                      "expired_24h": 0})
        k["rows_24h"] += int(r["n"] or 0)
        k["failed_24h"] += int(r["failed"] or 0)
        k["expired_24h"] += int(r["expired"] or 0)
    for r in _safe_rows(conn, "jobs", "SELECT kind, grp, state, COUNT(*) AS n FROM jobs "
                        "WHERE state IN ('queued','running') GROUP BY kind, grp, state"):
        k = out["queue_kinds"].setdefault(r["kind"], {"grp": r["grp"], "rows_24h": 0, "failed_24h": 0,
                                                      "expired_24h": 0})
        k[r["state"]] = k.get(r["state"], 0) + int(r["n"] or 0)
    groups = {}
    for kind, k in out["queue_kinds"].items():
        g = groups.setdefault(k.get("grp") or "?", {"queued": 0, "running": 0, "rows_24h": 0, "failed_24h": 0,
                                                    "expired_24h": 0})
        for c in g:
            g[c] += int(k.get(c) or 0)
    out["queue_groups"] = groups
    out["queue_cutoff"] = cutoff
    # The request budget's lanes and its backpressure verdict (pos_capacity,
    # F5 of this round — read only where the module exists), and the nightly
    # budget check's findings (its ops captures, job "pos_budget_check").
    out["capacity"] = _capacity()
    out["budget_findings"] = [{"error": r["error"], "at": r["created_at"]} for r in _safe_rows(
        conn, "job_failures", "SELECT error, created_at FROM job_failures WHERE job=? AND created_at >= ? "
        "ORDER BY id DESC LIMIT 3", (BUDGET_CHECK_JOB, since_q))]
    return out


BUDGET_CHECK_JOB = "pos_budget_check"


def _capacity():
    """{utilisation, state} from pos_capacity when it is deployed, else {}.
    Never raises."""
    try:
        import pos_capacity
    except ImportError:
        return {}
    out = {}
    try:
        out["utilisation"] = pos_capacity.utilisation(24)
    except Exception as e:
        log.warning("rpower_health: lane utilisation unreadable: %s", e)
    try:
        st = pos_capacity.state() or {}
        out["state"] = {k: st.get(k) for k in ("shed_bulk", "stretch_live", "demand", "budget", "ratio", "at",
                                              "over_nights")}
    except Exception as e:
        log.warning("rpower_health: budget state unreadable: %s", e)
    return out


def _batch_reads(conn, ids, now):
    """Every per-store source for one batch, one grouped query each."""
    ph = _ids_sql(ids)
    since90 = (now.date() - timedelta(days=WINDOW_DAYS + 2)).isoformat()
    week = _stamp(now - timedelta(days=ANOMALY_DAYS))
    day_ago = time.time() - 86400
    out = {"sh": {}, "days": {}, "state": {}, "slots": {}, "live": {}, "queue": {}, "failures": {}}
    for r in _safe_rows(conn, "source_health",
                        "SELECT restaurant_id, source, last_ok_at, last_attempt_at, first_failed_at, last_error, "
                        "error_class, consecutive_failures, next_retry_at FROM source_health "
                        f"WHERE restaurant_id IN ({ph}) AND source IN ('pos','pos_intraday','loss','dsr')", ids):
        out["sh"].setdefault(int(r["restaurant_id"]), {})[r["source"]] = r
    base = ("restaurant_id, business_date, tickets, lines, punches, net_sales, archived_at, recon_gap, "
            "unknown_sales, suspect")
    full = base + ", pay_gap, open_punches, store_key, attempts, last_error, first_read"
    where = f"FROM pos_archive_days WHERE provider=? AND restaurant_id IN ({ph}) AND business_date >= ?"
    for r in _safe_rows(conn, "pos_archive_days", f"SELECT {full} {where}", [PROVIDER] + list(ids) + [since90],
                        fallback_sql=f"SELECT {base} {where}"):
        out["days"].setdefault(int(r["restaurant_id"]), []).append(r)
    for r in _safe_rows(conn, "pos_archive_state",
                        "SELECT restaurant_id, changes_checked_to, prices_read_at, store_key FROM pos_archive_state "
                        f"WHERE provider=? AND restaurant_id IN ({ph})", [PROVIDER] + list(ids),
                        fallback_sql="SELECT restaurant_id, changes_checked_to FROM pos_archive_state "
                                     f"WHERE provider=? AND restaurant_id IN ({ph})"):
        out["state"][int(r["restaurant_id"])] = r
    for r in _safe_rows(conn, "pos_backfill_slots",
                        "SELECT restaurant_id, admitted_at, finished_at, held_reason FROM pos_backfill_slots "
                        f"WHERE provider=? AND restaurant_id IN ({ph})", [PROVIDER] + list(ids)):
        out["slots"][int(r["restaurant_id"])] = r
    for r in _safe_rows(conn, "pos_live_snapshot",
                        f"SELECT restaurant_id, kind, state, read_at FROM pos_live_snapshot WHERE restaurant_id IN ({ph})",
                        ids):
        out["live"].setdefault(int(r["restaurant_id"]), []).append(r)
    kinds = ",".join(f"'{k}'" for k in QUEUE_KINDS)
    for r in _safe_rows(conn, "jobs",
                        "SELECT restaurant_id, "
                        "SUM(CASE WHEN state IN ('queued','running') THEN 1 ELSE 0 END) AS queued, "
                        "SUM(CASE WHEN state='failed' AND finished_at >= ? THEN 1 ELSE 0 END) AS failed, "
                        "SUM(CASE WHEN state='expired' AND finished_at >= ? THEN 1 ELSE 0 END) AS expired, "
                        "SUM(CASE WHEN enqueued_at >= ? THEN 1 ELSE 0 END) AS rows_24h, "
                        "MIN(CASE WHEN state='queued' THEN not_before END) AS next_at "
                        f"FROM jobs WHERE restaurant_id IN ({ph}) AND kind IN ({kinds}) GROUP BY restaurant_id",
                        [day_ago, day_ago, day_ago] + list(ids)):
        out["queue"][int(r["restaurant_id"])] = r
    for r in _safe_rows(conn, "job_failures",
                        "SELECT restaurant_id, job, kind, COUNT(*) AS n, MAX(created_at) AS last_at FROM job_failures "
                        f"WHERE restaurant_id IN ({ph}) AND created_at >= ? GROUP BY restaurant_id, job, kind",
                        list(ids) + [week]):
        out["failures"].setdefault(int(r["restaurant_id"]), []).append(r)
    # Missed POS runs: a night's or an onboarding's queue row that expired
    # unstarted is recorded as a missed window (F5's EXPIRE_HOOKS of this
    # round, strategy_jobs.on_pos_row_expired); none before it is deployed.
    jobs = ",".join(f"'{j}'" for j in MISSED_JOBS)
    out["missed"] = {int(r["restaurant_id"]): r for r in _safe_rows(
        conn, "missed_windows", "SELECT restaurant_id, COUNT(*) AS n, MAX(local_date) AS last_date FROM missed_windows "
        f"WHERE restaurant_id IN ({ph}) AND job IN ({jobs}) AND created_at >= ? GROUP BY restaurant_id",
        list(ids) + [week])}
    return out


def _expected_nights(days, local_now, closed_weekdays, closed_dates, first_night):
    """(expected trading dates in the 90-day window up to the last owed night,
    the last owed night). A weekday is a trading weekday when at least two
    stored nights in the window fell on it (one odd night is not a pattern)
    and the owner has not said it is closed; an owner's closed date is never
    owed. Nights before the store's first stored night are not owed — that
    is the backfill's to fill."""
    owed_through = (local_now - timedelta(hours=GAP_GRACE_HOURS + 5)).date() - timedelta(days=1)
    start = max(local_now.date() - timedelta(days=WINDOW_DAYS), first_night) if first_night else None
    if start is None or start > owed_through:
        return [], owed_through
    seen = {}
    for d in days:
        try:
            wd = date.fromisoformat(str(d)[:10]).weekday()
        except ValueError:
            continue
        seen[wd] = seen.get(wd, 0) + 1
    trading = {wd for wd, n in seen.items() if n >= 2} - set(closed_weekdays)
    if not trading:
        return [], owed_through
    out, d = [], start
    while d <= owed_through:
        if d.weekday() in trading and d.isoformat() not in closed_dates:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out, owed_through


def _type_state(failing=False, retrying=False, stale=False):
    return "failing" if failing else "retrying" if retrying else "stale" if stale else "ok"


def compute_row(store, src, fleet, now=None, previous=None):
    """One store's health row from its batch reads (`src`), the fleet reads
    and the row it replaces (for `health_since`). Pure."""
    now = now or _utcnow()
    rid = int(store["id"])
    sh = src["sh"].get(rid, {})
    days = [d for d in src["days"].get(rid, [])]
    stored = [d for d in days if _archived(d)]
    st = src["state"].get(rid) or {}
    slot = src["slots"].get(rid)
    queue = src["queue"].get(rid) or {}
    fails = src["failures"].get(rid, [])
    cg, mid = str(store.get("rpower_cg") or ""), str(store.get("rpower_store_mid") or "")
    tz = store.get("timezone") or "America/Chicago"
    local_now = _local_now(tz, now)
    is_demo = int(store.get("is_demo") or 0)
    bs = str(store.get("billing_status") or "").strip().lower()
    try:
        import models
        in_service = int(models.in_service({"billing_status": bs}))
    except Exception:
        in_service = 1
    reasons, types = [], {}

    # The connection and the token.
    connection = "bound" if (store.get("rpower_token") and cg and mid) else (
        "needs_store" if store.get("rpower_token") else "not_configured")
    pos_sh = sh.get("pos") or {}
    auth_at = store.get("rpower_auth_failed_at") or (
        pos_sh.get("first_failed_at") if pos_sh.get("error_class") == "auth" and int(
            pos_sh.get("consecutive_failures") or 0) > 0 else None)
    token_state = "auth_failed" if auth_at else ("ok" if store.get("rpower_verified_at") else "unverified")
    if auth_at:
        reasons.append("RPOWER refused this store's token — every call waits for a reconnect")

    # Labor (the nightly pull).
    err = (store.get("rpower_sync_error") or "").strip()
    cf = int(pos_sh.get("consecutive_failures") or 0)
    retry_pending = bool(pos_sh.get("next_retry_at")) and pos_sh.get("error_class") != "auth"
    labor_age = _hours_since(store.get("rpower_last_synced"), now)
    labor_failing = bool((err or cf) and not auth_at and not retry_pending)
    types["labor"] = _type_state(labor_failing, bool((err or cf) and retry_pending and not auth_at),
                                 labor_age is None or labor_age > STALE_HOURS["labor"])
    if types["labor"] == "failing":
        reasons.append(f"Labor pull failing{(' ' + str(cf) + ' in a row') if cf else ''}: {(err or pos_sh.get('last_error') or '')[:120]}")
    elif types["labor"] == "retrying":
        reasons.append("Labor pull failed — a retry is scheduled")

    # The archive: coverage, gaps, the newest stored night.
    first = None
    for d in stored:
        try:
            dd = date.fromisoformat(str(d["business_date"])[:10])
        except ValueError:
            continue
        first = dd if first is None or dd < first else first
    stored_dates = {str(d["business_date"])[:10] for d in stored}
    closed_weekdays, closed_dates = _closures(store.get("compliance_json"))
    expected, owed_through = _expected_nights(stored_dates, local_now, closed_weekdays, closed_dates, first)
    expected_set = set(expected)
    archived_90 = len(expected_set & stored_dates)
    coverage = round(archived_90 / float(len(expected)), 4) if expected else None
    gap_from = (local_now.date() - timedelta(days=GAP_DAYS)).isoformat()
    gaps = sorted(d for d in expected if d >= gap_from and d not in stored_dates)
    last_night = max(stored_dates) if stored_dates else None
    last_archived_at = max((str(d.get("archived_at") or "") for d in stored), default="") or None
    recent_owed = sorted(expected)[-2:]
    two_missing = len(recent_owed) == 2 and not (set(recent_owed) & stored_dates)
    failed_nights = [d for d in days if not _archived(d) and str(d["business_date"])[:10] in set(recent_owed)]
    q_failed = int(queue.get("failed") or 0) + int(queue.get("expired") or 0)
    archive_failing = two_missing and (bool(failed_nights) or q_failed > 0 or any(
        f.get("job") in ("queue:pos_archive", "pos_archive") for f in fails))
    types["archive"] = ("failing" if archive_failing else "stale" if two_missing else
                        "gap" if gaps else "ok" if stored_dates else "none")
    if archive_failing:
        why = (failed_nights[0].get("last_error") if failed_nights else None) or "the archive rows failed"
        reasons.append(f"No archived night for the last 2 trading nights — {str(why)[:120]}")
    elif two_missing:
        reasons.append("No archived night for the last 2 trading nights, and nothing failed — the run did not reach it")
    elif gaps:
        reasons.append(f"{len(gaps)} trading night{'s' if len(gaps) != 1 else ''} missing from the archive in 14 days")

    # The change read, the catalog, the prices.
    change_age = _hours_since(st.get("changes_checked_to"), now)
    types["change"] = "none" if change_age is None else ("stale" if change_age > STALE_HOURS["change"] else "ok")
    cat_at = (fleet.get("catalogs") or {}).get(cg)
    cat_age = (time.time() - cat_at) / 3600.0 if cat_at else None
    types["catalog"] = "none" if cat_age is None else ("stale" if cat_age > STALE_HOURS["catalog"] else "ok")
    prices_age = _hours_since(st.get("prices_read_at"), now)
    types["prices"] = "none" if prices_age is None else ("stale" if prices_age > STALE_HOURS["prices"] else "ok")

    # Live reads.
    live = src["live"].get(rid, [])
    live_ok = max((float(x["read_at"] or 0) for x in live if (x.get("state") or "ok") == "ok"), default=0) or None
    newest_live = max(live, key=lambda x: float(x.get("read_at") or 0), default=None)
    live_state = (newest_live or {}).get("state") if newest_live else None
    types["live"] = "none" if not live else ("ok" if (live_state or "ok") == "ok" else "failing")

    # Intraday, loss, the DSR (source_health).
    for source, key in SOURCE_OF.items():
        s = sh.get(source)
        if not s:
            types[key] = "none"
            continue
        n = int(s.get("consecutive_failures") or 0)
        age = _hours_since(s.get("last_ok_at"), now)
        types[key] = _type_state(n >= 2 and not s.get("next_retry_at"), n >= 1,
                                 age is not None and age > STALE_HOURS[key])
        if types[key] == "failing":
            reasons.append(f"{key.title()} read failing {n} in a row: {(s.get('last_error') or '')[:100]}")

    # The queue and the ledger.
    led = (fleet.get("ledger") or {}).get(rid) or {}
    req = int(led.get("n") or 0)
    errs = sum(int(led.get(c) or 0) for c in ("s4xx", "s429", "s5xx", "transport"))
    p95 = None
    try:
        import pos_io
        p95 = pos_io._pct_from_buckets(led, 95) if led else None
    except Exception:
        p95 = None
    by_day = (fleet.get("ledger_days") or {}).get(rid) or {}
    today_key = now.strftime("%Y-%m-%d")
    prior = sorted(v for k, v in by_day.items() if k != today_key)
    median = None
    if prior:
        mid_i = len(prior) // 2
        median = float(prior[mid_i]) if len(prior) % 2 else (prior[mid_i - 1] + prior[mid_i]) / 2.0

    # The anomalies of the last 7 nights.
    since7 = (local_now.date() - timedelta(days=ANOMALY_DAYS)).isoformat()
    week = [d for d in stored if str(d["business_date"])[:10] >= since7]
    recon_n = sum(1 for d in week if d.get("recon_gap") is not None and _night_gap(d["recon_gap"], d.get("net_sales")))
    unknown = round(sum(float(d.get("unknown_sales") or 0) for d in week), 2)
    net7 = round(sum(float(d.get("net_sales") or 0) for d in week), 2)
    suspect_n = sum(1 for d in week if d.get("suspect"))
    pay_n = sum(1 for d in week if d.get("pay_gap") is not None and _night_gap(d["pay_gap"], d.get("net_sales")))
    first_read_n = sum(1 for d in week if _first_read_suspect(d.get("first_read")))
    open_p = sum(int(d.get("open_punches") or 0) for d in week)
    zero_labor = sum(1 for d in week if int(d.get("tickets") or 0) > 0 and float(d.get("net_sales") or 0) > 0
                     and int(d.get("punches") or 0) == 0)
    dsr_mm = sum(int(f.get("n") or 0) for f in fails if f.get("job") == "dsr_pos_consistency")
    data_n = sum(int(f.get("n") or 0) for f in fails if (f.get("kind") or "") == "data_quality")
    anomalies = []
    if recon_n >= 2:
        anomalies.append(f"Lines and RPOWER's ticket totals disagree on {recon_n} nights")
    if net7 > 0 and unknown > UNKNOWN_SALES_SHARE * net7:
        anomalies.append(f"${unknown:,.0f} on sales types RPOWER's list does not name ({unknown / net7 * 100:.1f}% of net)")
    if pay_n >= 2:
        anomalies.append(f"Payments and ticket totals disagree on {pay_n} nights")
    if suspect_n:
        anomalies.append(f"{suspect_n} night{'s' if suspect_n != 1 else ''} with a short re-read refused")
    if first_read_n:
        anomalies.append(f"{first_read_n} night{'s' if first_read_n != 1 else ''} whose first read looked short")
    if open_p:
        anomalies.append(f"{open_p} punch{'es' if open_p != 1 else ''} still open past close")
    if zero_labor:
        anomalies.append(f"{zero_labor} trading night{'s' if zero_labor != 1 else ''} with sales and no punches")
    if dsr_mm:
        anomalies.append("The nightly report's net and the archive disagree")
    missed = (src.get("missed") or {}).get(rid) or {}
    missed_n = int(missed.get("n") or 0)
    if missed_n:
        anomalies.append(f"{missed_n} POS run{'s' if missed_n != 1 else ''} missed — queue rows expired unstarted "
                         f"(last for {missed.get('last_date')})")
    if median and req > VOLUME_STORE_X * median:
        anomalies.append(f"{req:,} RPOWER requests in 24 hours, {req / median:.0f}x this store's median")

    # Integrity.
    dup = int((cg, mid) in (fleet.get("dupes") or set()) and not is_demo)
    key_now = f"{cg}:{mid}" if cg and mid else None
    keys = {str(d.get("store_key")) for d in days if d.get("store_key")}
    if st.get("store_key"):
        keys.add(str(st["store_key"]))
    key_mm = int(bool(key_now) and any(k != key_now for k in keys))
    sim = int(_is_simulated_token(store.get("rpower_token")) and not is_demo)
    if dup:
        reasons.append("This RPOWER store is bound to another restaurant too")
    if key_mm:
        reasons.append("Archived nights came from a different RPOWER store than the one bound now")
    if sim:
        reasons.append("A simulated (demo-store) token on an account not marked demo")

    # The backfill slot, onboarding.
    backfill = "none"
    if slot:
        backfill = "done" if slot.get("finished_at") else ("held" if slot.get("held_reason") else "admitted")
    bound_age = _hours_since(store.get("rpower_verified_at") or store.get("created_at"), now)
    onboarding = backfill in ("admitted", "held") or (not stored_dates and (bound_age is None or bound_age < ONBOARDING_DAYS * 24))

    failing_types = sorted(k for k, v in types.items() if v == "failing" and k != "live")
    stale_types = sorted(k for k, v in types.items() if v == "stale")
    if auth_at:
        health = "auth"
    elif failing_types or dup or key_mm or sim:
        health = "failing"
    elif onboarding:
        health = "onboarding"
    elif "labor" in stale_types or "archive" in stale_types:
        health = "stale"
    elif (anomalies or gaps or stale_types or any(v in ("retrying", "gap") for v in types.values())
          or types.get("live") == "failing"):
        health = "watch"
    else:
        health = "ok"
    if onboarding and health == "onboarding" and slot and slot.get("held_reason"):
        reasons.append(f"Backfill held: {slot['held_reason']}")
    reasons.extend(anomalies)
    for k in stale_types:
        if k not in ("labor", "archive"):
            reasons.append(f"{k.title()} not read in over {STALE_HOURS.get(k, 48)} hours")
    if health == "stale" and "labor" in stale_types:
        reasons.append("No labor pull in over 48 hours")
    since = (previous or {}).get("health_since") if (previous or {}).get("health") == health else None
    rank = HEALTH_RANK[health] * 100 + min(99, len(gaps) * 5 + len(failing_types) * 10 + len(anomalies) * 3)
    return {
        "restaurant_id": rid, "name": store.get("location_name") or store.get("name"),
        "is_demo": is_demo, "in_service": in_service, "billing_status": bs or None,
        "partner_id": (fleet.get("partners") or {}).get(rid), "cg": cg or None, "store_mid": mid or None,
        "token_bucket": _token_bucket(store.get("rpower_token")), "tz": tz,
        "connection": connection, "token_state": token_state, "auth_failed_at": auth_at,
        "last_labor_ok": store.get("rpower_last_synced"), "last_archive_night": last_night,
        "last_archived_at": last_archived_at, "last_change_read": st.get("changes_checked_to"),
        "last_catalog_ok": _stamp(datetime.fromtimestamp(cat_at, timezone.utc)) if cat_at else None,
        "last_prices_ok": st.get("prices_read_at"),
        "last_live_ok": _stamp(datetime.fromtimestamp(live_ok, timezone.utc)) if live_ok else None,
        "live_state": live_state,
        "last_intraday_ok": (sh.get("pos_intraday") or {}).get("last_ok_at"),
        "last_loss_ok": (sh.get("loss") or {}).get("last_ok_at"),
        "last_dsr_ok": (sh.get("dsr") or {}).get("last_ok_at"),
        "type_states": json.dumps(types, sort_keys=True),
        "failing_types": ",".join(failing_types) or None, "stale_types": ",".join(stale_types) or None,
        "expected_90d": len(expected), "archived_90d": archived_90, "coverage_90d": coverage,
        "gaps_14d": len(gaps), "gap_dates": json.dumps(gaps),
        "backfill_state": backfill, "backfill_reason": (slot or {}).get("held_reason"),
        "consecutive_failures": cf, "first_failed_at": pos_sh.get("first_failed_at"),
        "last_error": (err or pos_sh.get("last_error") or None),
        "error_class": pos_sh.get("error_class"), "next_retry_at": pos_sh.get("next_retry_at"),
        "queue_queued": int(queue.get("queued") or 0), "queue_rows_24h": int(queue.get("rows_24h") or 0),
        "queue_failed_24h": int(queue.get("failed") or 0), "queue_expired_24h": int(queue.get("expired") or 0),
        "req_24h": req, "err_24h": errs, "s429_24h": int(led.get("s429") or 0),
        "skipped_24h": int(led.get("skipped") or 0), "p95_ms_24h": p95, "req_median_7d": median,
        "recon_nights_7d": recon_n, "unknown_sales_7d": unknown, "net_sales_7d": net7,
        "suspect_nights_7d": suspect_n, "pay_gap_nights_7d": pay_n, "first_read_suspect_7d": first_read_n,
        "open_punches_7d": open_p, "zero_labor_days_7d": zero_labor, "dsr_mismatch_7d": dsr_mm,
        "data_issues_7d": data_n, "missed_runs_7d": missed_n, "duplicate_binding": dup, "store_key_mismatch": key_mm,
        "simulated_not_demo": sim, "has_anomaly": int(bool(anomalies)),
        "health": health, "health_since": since or _stamp(now), "severity_rank": rank,
        "reasons": json.dumps(reasons[:12]), "updated_at": _stamp(now),
    }


_ROW_COLS = ("restaurant_id", "name", "is_demo", "in_service", "billing_status", "partner_id", "cg", "store_mid",
             "token_bucket", "tz", "connection", "token_state", "auth_failed_at", "last_labor_ok",
             "last_archive_night", "last_archived_at", "last_change_read", "last_catalog_ok", "last_prices_ok",
             "last_live_ok", "live_state", "last_intraday_ok", "last_loss_ok", "last_dsr_ok", "type_states",
             "failing_types", "stale_types", "expected_90d", "archived_90d", "coverage_90d", "gaps_14d", "gap_dates",
             "backfill_state", "backfill_reason", "consecutive_failures", "first_failed_at", "last_error",
             "error_class", "next_retry_at", "queue_queued", "queue_rows_24h", "queue_failed_24h",
             "queue_expired_24h", "req_24h", "err_24h", "s429_24h", "skipped_24h", "p95_ms_24h", "req_median_7d",
             "recon_nights_7d", "unknown_sales_7d", "net_sales_7d", "suspect_nights_7d", "pay_gap_nights_7d",
             "first_read_suspect_7d", "open_punches_7d", "zero_labor_days_7d", "dsr_mismatch_7d",
             "data_issues_7d", "duplicate_binding", "store_key_mismatch", "simulated_not_demo", "has_anomaly",
             "recipe_coverage_pct", "recipe_coverage_at", "missed_runs_7d",
             "health", "health_since", "severity_rank", "reasons", "updated_at")
_UPSERT = (f"INSERT INTO rpower_store_health ({', '.join(_ROW_COLS)}) VALUES ({', '.join('?' for _ in _ROW_COLS)}) "
           "ON CONFLICT(restaurant_id) DO UPDATE SET "
           + ", ".join(f"{c}=excluded.{c}" for c in _ROW_COLS if c != "restaurant_id"))


def _write_rows(conn, rows):
    if rows:
        conn.executemany(_UPSERT, [tuple(r.get(c) for c in _ROW_COLS) for r in rows])
        conn.commit()


# Recipe coverage (A6-21) is the owner's own figure (inventory_ledger.
# recipe_coverage: Back Office recipes and the bar's drink categories count),
# a few queries a store — so it is refreshed once a day per store, at most
# RECIPE_COVERAGE_PER_PASS stores a pass, and carried between refreshes.
RECIPE_COVERAGE_PER_PASS = 25
RECIPE_COVERAGE_HOURS = 24


def _recipe_coverage(row, previous, now, budget):
    """Carry the store's recipe coverage forward, refreshing it when a day
    old and the pass's budget allows. Returns the budget left."""
    previous = previous or {}
    row["recipe_coverage_pct"] = previous.get("recipe_coverage_pct")
    row["recipe_coverage_at"] = previous.get("recipe_coverage_at")
    age = _hours_since(row["recipe_coverage_at"], now)
    if budget <= 0 or row.get("is_demo") or not row.get("in_service") or (
            age is not None and age < RECIPE_COVERAGE_HOURS):
        return budget
    try:
        import inventory_ledger
        cov = inventory_ledger.recipe_coverage(int(row["restaurant_id"])) or {}
        row["recipe_coverage_pct"] = cov.get("coverage_pct")
        row["recipe_coverage_at"] = _stamp(now)
    except Exception as e:
        log.warning("rpower_health: recipe coverage unreadable for %s: %s", row["restaurant_id"], e)
    return budget - 1


def _previous(conn, ids):
    if not ids:
        return {}
    return {int(r["restaurant_id"]): r for r in _safe_rows(
        conn, "rpower_store_health", "SELECT restaurant_id, health, health_since, auth_failed_at, "
        "recipe_coverage_pct, recipe_coverage_at FROM "
        f"rpower_store_health WHERE restaurant_id IN ({_ids_sql(ids)})", ids)}


def run_rollup(db_path=None, max_seconds=None, now=None, restaurant_ids=None) -> dict:
    """The `rpower_health_rollup` job: every RPOWER store's row, in batches
    of BATCH from the job_cursors cursor, until ROLLUP_BOUND_SECONDS; then
    the stores no longer bound are dropped (on a pass that covered them all),
    today's fleet row is rewritten from the table, and the owners whose token
    just failed are told (`_notify_auth`). Returns the standard counts."""
    t0 = _clock()
    bound = float(max_seconds if max_seconds is not None else ROLLUP_BOUND_SECONDS)
    now = now or _utcnow()
    conn = _conn(db_path)
    attempted = ok = failed = 0
    hit_bound = False
    transitions, order = [], []
    try:
        stores = _stores(conn)
        if restaurant_ids is not None:
            want = {int(x) for x in restaurant_ids}
            stores = [s for s in stores if int(s["id"]) in want]
        by_id = {int(s["id"]): s for s in stores}
        order = sorted(by_id)
        budget = RECIPE_COVERAGE_PER_PASS
        last = _cursor(conn) if restaurant_ids is None else None
        if last is not None and order:
            cut = next((i for i, x in enumerate(order) if x > last), len(order))
            order = order[cut:] + order[:cut]
        fleet = _fleet_reads(conn, now)
        done = 0
        for i in range(0, len(order), BATCH):
            if i and _clock() - t0 > bound:
                hit_bound = True
                break
            ids = order[i:i + BATCH]
            attempted += len(ids)
            try:
                src = _batch_reads(conn, ids, now)
                prev = _previous(conn, ids)
                rows = []
                for rid in ids:
                    try:
                        row = compute_row(by_id[rid], src, fleet, now=now, previous=prev.get(rid))
                        budget = _recipe_coverage(row, prev.get(rid), now, budget)
                        rows.append(row)
                        if row["auth_failed_at"] and not (prev.get(rid) or {}).get("auth_failed_at"):
                            transitions.append(row)
                        ok += 1
                    except Exception as e:
                        failed += 1
                        import ops
                        ops.capture(e, job="rpower_health_rollup", context=f"restaurant_id={rid}", db_path=db_path)
                _write_rows(conn, rows)
            except Exception as e:
                failed += len(ids)
                import ops
                ops.capture(e, job="rpower_health_rollup", context=f"batch from {ids[0]}", db_path=db_path)
            done = i + len(ids)
            if restaurant_ids is None:
                _save_cursor(conn, ids[-1])
        if restaurant_ids is None and not hit_bound and done >= len(order):
            gone = _safe_rows(conn, "rpower_store_health", "SELECT restaurant_id FROM rpower_store_health")
            stale_ids = [int(r["restaurant_id"]) for r in gone if int(r["restaurant_id"]) not in by_id]
            for j in range(0, len(stale_ids), 500):
                part = stale_ids[j:j + 500]
                conn.execute(f"DELETE FROM rpower_store_health WHERE restaurant_id IN ({_ids_sql(part)})", part)
            conn.commit()
        _write_fleet_day(conn, fleet, now)
    finally:
        conn.close()
    notified = _notify_auth(transitions, db_path=db_path) if transitions else 0
    return {"attempted": attempted, "ok": ok, "failed": failed, "skipped": 0, "hit_bound": hit_bound,
            "eligible": len(order), "auth_notified": notified}


def _clock():
    """The rollup's own clock (its bound), apart from time.monotonic so a
    test can drive it without stopping every other timer."""
    return time.monotonic()


def refresh_store(restaurant_id, db_path=None) -> dict:
    """One store's row now — the event hook a writer of the auth latch or an
    archive result may call so the console does not wait for the next pass.
    Never raises."""
    try:
        return run_rollup(db_path=db_path, restaurant_ids=[int(restaurant_id)], max_seconds=30)
    except Exception as e:
        log.warning("rpower_health.refresh_store(%s) failed: %s", restaurant_id, e)
        return {"attempted": 1, "ok": 0, "failed": 1, "skipped": 0, "hit_bound": False}


def _pctl(vals, q):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    import math
    return round(vals[max(0, min(len(vals) - 1, int(math.ceil(q / 100.0 * len(vals))) - 1))], 4)


def _write_fleet_day(conn, fleet, now):
    """Today's rpower_fleet_health_daily row, from the table (the live,
    non-demo stores) and this pass's fleet reads."""
    rows = _safe_rows(conn, "rpower_store_health", "SELECT health, type_states, coverage_90d, expected_90d, "
                      "archived_90d, recon_nights_7d, pay_gap_nights_7d, token_bucket, auth_failed_at, req_24h, "
                      "s429_24h, skipped_24h, err_24h, duplicate_binding FROM rpower_store_health "
                      "WHERE is_demo=0 AND in_service=1")
    by_health, failing, stale = {}, {}, {}
    buckets = set()
    for r in rows:
        by_health[r["health"]] = by_health.get(r["health"], 0) + 1
        try:
            types = json.loads(r.get("type_states") or "{}")
        except (TypeError, ValueError):
            types = {}
        for k, v in types.items():
            if v == "failing":
                failing[k] = failing.get(k, 0) + 1
            elif v == "stale":
                stale[k] = stale.get(k, 0) + 1
        if r.get("auth_failed_at") and r.get("token_bucket"):
            buckets.add(r["token_bucket"])
    days = {}
    for per in (fleet.get("ledger_days") or {}).values():
        for d, n in per.items():
            days[d] = days.get(d, 0) + int(n or 0)
    qk = fleet.get("queue_kinds") or {}
    detail = {"ledger_1h": fleet.get("ledger_1h") or {}, "requests_by_day": dict(sorted(days.items())),
              "queue_kinds": qk, "queue_groups": fleet.get("queue_groups") or {},
              "capacity": fleet.get("capacity") or {}, "budget_findings": fleet.get("budget_findings") or []}
    vals = (now.date().isoformat(), len(rows), json.dumps(by_health, sort_keys=True), json.dumps(failing, sort_keys=True),
            json.dumps(stale, sort_keys=True), _pctl([r.get("coverage_90d") for r in rows], 10),
            _pctl([r.get("coverage_90d") for r in rows], 50),
            sum(int(r.get("expected_90d") or 0) for r in rows), sum(int(r.get("archived_90d") or 0) for r in rows),
            sum(int(r.get("recon_nights_7d") or 0) for r in rows), sum(int(r.get("pay_gap_nights_7d") or 0) for r in rows),
            sum(1 for r in rows if r.get("auth_failed_at")), len(buckets),
            sum(int(r.get("req_24h") or 0) for r in rows), sum(int(r.get("s429_24h") or 0) for r in rows),
            sum(int(r.get("skipped_24h") or 0) for r in rows), sum(int(r.get("err_24h") or 0) for r in rows),
            sum(int(v.get("failed_24h") or 0) for k, v in qk.items() if k in QUEUE_KINDS),
            sum(int(v.get("expired_24h") or 0) for k, v in qk.items() if k in QUEUE_KINDS),
            sum(1 for r in rows if r.get("duplicate_binding")), json.dumps(detail, sort_keys=True), _stamp(now))
    cols = ("date", "stores", "by_health", "failing_by_type", "stale_by_type", "coverage_p10", "coverage_p50",
            "nights_expected", "nights_archived", "recon_nights", "pay_gap_nights", "auth_failed", "auth_buckets",
            "requests", "s429", "skipped", "errors", "queue_failed", "queue_expired", "duplicate_bindings",
            "detail_json", "updated_at")
    conn.execute(f"INSERT INTO rpower_fleet_health_daily ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
                 "ON CONFLICT(date) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "date"),
                 vals)
    conn.commit()


# ── the owner told once (A6-10) ────────────────────────────────────────────

def _notify_auth(rows, db_path=None) -> int:
    """Tell each owner whose store just went into auth failure, once per
    failure (claimed on the latch's own stamp before the send): one alert,
    email and push, through notify.raise_alert — the Google reconnect
    pattern (scheduler's connection_lost). Only where the scheduler may send
    (scheduler.scheduling_allowed); never for a demo store, a store out of
    service, or a token other failing stores share — a partner token the
    owner does not hold is the operator's (one platform issue, A4-16)."""
    try:
        import scheduler
        if not scheduler.scheduling_allowed():
            return 0
    except Exception:
        return 0
    shared = {}
    conn = _conn(db_path)
    try:
        for r in _safe_rows(conn, "rpower_store_health", "SELECT token_bucket, COUNT(*) AS n FROM rpower_store_health "
                            "WHERE auth_failed_at IS NOT NULL AND token_bucket IS NOT NULL GROUP BY token_bucket"):
            shared[r["token_bucket"]] = int(r["n"] or 0)
    finally:
        conn.close()
    sent = 0
    import ops
    for row in rows:
        if row.get("is_demo") or not row.get("in_service"):
            continue
        if row.get("token_bucket") and shared.get(row["token_bucket"], 0) >= 2:
            continue
        rid = int(row["restaurant_id"])
        try:
            if not ops.claim_period(f"rpower_auth_notice:{rid}", str(row["auth_failed_at"])[:19]):
                continue
            import notify
            name = row.get("name") or "your restaurant"
            kw = {"db_path": db_path} if db_path else {}
            notify.raise_alert(
                rid, "connection_lost",
                f"Cavnar AI: RPOWER stopped accepting this store's connection — sales and labor stopped syncing "
                f"for {name}. Reconnect it in Account -> Connections.",
                f"RPOWER needs reconnecting — {name}",
                lines=["RPOWER refused the token Cavnar AI uses to read this store's sales and labor, so nothing "
                       "new is coming in: the nightly report, labor and food cost are using the last data read.",
                       "Nothing already stored is lost. Open Account -> Connections and reconnect RPOWER, or ask "
                       "your RPOWER contact for a new API token."], **kw)
            sent += 1
        except Exception as e:
            log.warning("rpower auth notice failed for %s: %s", rid, e)
    return sent


# ── readers: the console, the alert rules, the status page ─────────────────

_JSON_COLS = ("type_states", "gap_dates", "reasons")


def _shape(row):
    out = dict(row)
    for c in _JSON_COLS:
        try:
            out[c] = json.loads(out.get(c) or ("{}" if c == "type_states" else "[]"))
        except (TypeError, ValueError):
            out[c] = {} if c == "type_states" else []
    for c in ("failing_types", "stale_types"):
        out[c] = [x for x in str(out.get(c) or "").split(",") if x]
    return out


def store_rows(db_path=None) -> dict:
    """{restaurant_id: shaped row} for every store in the table — the admin
    console's fleet build reads this once, never a query per store."""
    conn = _conn(db_path)
    try:
        return {int(r["restaurant_id"]): _shape(r) for r in _safe_rows(conn, "rpower_store_health",
                                                                         "SELECT * FROM rpower_store_health")}
    finally:
        conn.close()


def _live_rows(conn, cols):
    return _safe_rows(conn, "rpower_store_health", f"SELECT {cols} FROM rpower_store_health "
                      "WHERE is_demo=0 AND in_service=1")


def _today_row(conn):
    rows = _safe_rows(conn, "rpower_fleet_health_daily",
                      "SELECT * FROM rpower_fleet_health_daily ORDER BY date DESC LIMIT 1")
    if not rows:
        return {}
    r = rows[0]
    for c in ("by_health", "failing_by_type", "stale_by_type", "detail_json"):
        try:
            r[c] = json.loads(r.get(c) or "{}")
        except (TypeError, ValueError):
            r[c] = {}
    return r


def fleet_trend(days=30, db_path=None) -> list:
    """The last `days` daily fleet rows, oldest first — the console's trend
    lines (rpower_fleet_health_daily's binding reader, 30 days)."""
    since = (_utcnow().date() - timedelta(days=int(days))).isoformat()
    conn = _conn(db_path)
    try:
        rows = _safe_rows(conn, "rpower_fleet_health_daily", "SELECT date, stores, by_health, coverage_p50, "
                          "requests, s429, skipped, errors, queue_failed, queue_expired, recon_nights, "
                          "pay_gap_nights, auth_failed FROM rpower_fleet_health_daily WHERE date >= ? ORDER BY date",
                          (since,))
    finally:
        conn.close()
    for r in rows:
        try:
            r["by_health"] = json.loads(r.get("by_health") or "{}")
        except (TypeError, ValueError):
            r["by_health"] = {}
    return rows


def fleet_counts(db_path=None) -> dict:
    """{connected, by_health, as_of, fresh} over the live, non-demo stores —
    one GROUP BY (A6-16), never the restaurants hydrated."""
    conn = _conn(db_path)
    try:
        rows = _safe_rows(conn, "rpower_store_health", "SELECT health, COUNT(*) AS n, MAX(updated_at) AS at "
                          "FROM rpower_store_health WHERE is_demo=0 AND in_service=1 GROUP BY health")
    finally:
        conn.close()
    by = {r["health"]: int(r["n"] or 0) for r in rows}
    at = max((r["at"] for r in rows if r.get("at")), default=None)
    age = _hours_since(at, _utcnow())
    return {"connected": sum(by.values()), "by_health": by, "as_of": at,
            "fresh": age is not None and age * 60 <= FRESH_ROLLUP_MINUTES}


def fleet_failing(db_path=None) -> dict:
    """{"fresh", "connected", "failing": [{id, name}], "stale": [{id, name}]}
    for the live, non-demo RPOWER stores, from the rollup: failing is a data
    type failing or the token refused, stale is the archive or the labor
    pull past 48 hours (A6-12) — what pos_health.fleet_state and the status
    page read in place of every restaurant row (A6-14, A6-16)."""
    conn = _conn(db_path)
    try:
        rows = _live_rows(conn, "restaurant_id, name, health, updated_at")
    finally:
        conn.close()
    at = max((r["updated_at"] for r in rows if r.get("updated_at")), default=None)
    age = _hours_since(at, _utcnow())
    return {"fresh": bool(rows) and age is not None and age * 60 <= FRESH_ROLLUP_MINUTES, "connected": len(rows),
            "failing": [{"id": r["restaurant_id"], "name": r["name"]} for r in rows if r["health"] in ("failing", "auth")],
            "stale": [{"id": r["restaurant_id"], "name": r["name"]} for r in rows if r["health"] == "stale"]}


def fleet(db_path=None) -> dict:
    """The KPI strip: connected, failing share, auth failures (and the token
    buckets they span), stale, coverage p50, stores with a gap, recon and pay
    gap nights, requests, 429 and skipped shares, the breaker, the queue's
    failed and expired rows, the backfill slots."""
    conn = _conn(db_path)
    try:
        rows = _live_rows(conn, "health, token_bucket, auth_failed_at, stale_types, gaps_14d, recon_nights_7d, "
                                "pay_gap_nights_7d, req_24h, s429_24h, skipped_24h, err_24h, queue_failed_24h, "
                                "queue_expired_24h, backfill_state, coverage_90d, unknown_sales_7d, net_sales_7d, "
                                "recipe_coverage_pct, updated_at")
        day = _today_row(conn)
    finally:
        conn.close()
    n = len(rows)
    by = {}
    for r in rows:
        by[r["health"]] = by.get(r["health"], 0) + 1
    req = sum(int(r.get("req_24h") or 0) for r in rows)
    auth = [r for r in rows if r.get("auth_failed_at")]
    try:
        import pos_io
        breaker = pos_io.breaker_state(PROVIDER)
    except Exception:
        breaker = {"state": "unknown"}
    try:
        import onboarding
        cap = int(getattr(onboarding, "POS_BACKFILL_MAX_STORES", 0) or 0) or None
    except Exception:
        cap = None
    at = max((r["updated_at"] for r in rows if r.get("updated_at")), default=None)
    age = _hours_since(at, _utcnow())
    return {
        "connected": n, "by_health": by,
        "failing": by.get("failing", 0), "failing_share": round(by.get("failing", 0) / float(n), 4) if n else 0.0,
        "auth_failed": len(auth), "auth_buckets": len({r["token_bucket"] for r in auth if r.get("token_bucket")}),
        "stale_48h": sum(1 for r in rows if set(str(r.get("stale_types") or "").split(",")) & {"labor", "archive"}),
        "coverage_p50": _pctl([r.get("coverage_90d") for r in rows], 50),
        "coverage_p10": _pctl([r.get("coverage_90d") for r in rows], 10),
        "stores_with_gaps": sum(1 for r in rows if int(r.get("gaps_14d") or 0) > 0),
        # A6-21: what the fleet sells on unnamed sales types, and how much of
        # what it sells a recipe accounts for (each store's own figure).
        "unknown_sales_7d": round(sum(float(r.get("unknown_sales_7d") or 0) for r in rows), 2),
        "unknown_sales_share_7d": (round(sum(float(r.get("unknown_sales_7d") or 0) for r in rows)
                                         / sum(float(r.get("net_sales_7d") or 0) for r in rows), 4)
                                   if sum(float(r.get("net_sales_7d") or 0) for r in rows) > 0 else None),
        "recipe_coverage_p50": _pctl([r.get("recipe_coverage_pct") for r in rows], 50),
        "recipe_coverage_stores": sum(1 for r in rows if r.get("recipe_coverage_pct") is not None),
        "recon_nights_7d": sum(int(r.get("recon_nights_7d") or 0) for r in rows),
        "pay_gap_nights_7d": sum(int(r.get("pay_gap_nights_7d") or 0) for r in rows),
        "requests_24h": req, "errors_24h": sum(int(r.get("err_24h") or 0) for r in rows),
        "s429_share": round(sum(int(r.get("s429_24h") or 0) for r in rows) / float(req), 4) if req else 0.0,
        "skipped_share": round(sum(int(r.get("skipped_24h") or 0) for r in rows) / float(req), 4) if req else 0.0,
        "breaker": breaker,
        "queue_failed_24h": sum(int(r.get("queue_failed_24h") or 0) for r in rows),
        "queue_expired_24h": sum(int(r.get("queue_expired_24h") or 0) for r in rows),
        "queue_kinds": (day.get("detail_json") or {}).get("queue_kinds") or {},
        "queue_groups": (day.get("detail_json") or {}).get("queue_groups") or {},
        # The request budget by lane and its backpressure verdict (pos_capacity;
        # {} where it is not deployed) and the nightly budget check's findings.
        "capacity": (day.get("detail_json") or {}).get("capacity") or {},
        "budget_findings": (day.get("detail_json") or {}).get("budget_findings") or [],
        "backfill_used": sum(1 for r in rows if r.get("backfill_state") in ("admitted", "held")),
        "backfill_cap": cap, "as_of": at, "fresh": age is not None and age * 60 <= FRESH_ROLLUP_MINUTES,
    }


def _rule(key, title, severity, detail, page=None, fold=None):
    return {"key": key, "title": title, "severity": severity, "detail": detail, "page": page, "fold": fold or []}


def _median(vals):
    vals = sorted(vals)
    if not vals:
        return None
    m = len(vals) // 2
    return float(vals[m]) if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2.0


def fleet_rules(db_path=None, now=None) -> list:
    """The fleet alert rules (A6 section 3.6) over the rollup only: each
    {key, title, severity, detail, page: None | "warn" | "urgent", fold:
    [restaurant ids whose own POS row the issue stands for]}. Read by
    ops.check_platform_sla (pages) and admin_ops (platform issues). Never
    raises: [] unreadable."""
    now = now or _utcnow()
    try:
        conn = _conn(db_path)
        try:
            rows = _live_rows(conn, "restaurant_id, name, health, health_since, type_states, auth_failed_at, "
                                    "token_bucket, gaps_14d")
            day = _today_row(conn)
        finally:
            conn.close()
    except Exception as e:
        log.warning("rpower fleet rules unreadable: %s", e)
        return []
    n = len(rows)
    if not n:
        return []
    out = []
    types = {}
    for r in rows:
        try:
            types[r["restaurant_id"]] = json.loads(r.get("type_states") or "{}")
        except (TypeError, ValueError):
            types[r["restaurant_id"]] = {}

    def names(rs):
        return ", ".join(str(r.get("name") or "#%s" % r["restaurant_id"]) for r in rs[:4]) + ("…" if len(rs) > 4 else "")

    # 1. A data type failing across the fleet, each store failing 30 minutes or more.
    sustained = now - timedelta(minutes=FLEET_FAILING_SUSTAIN_MINUTES)
    for t in DATA_TYPES:
        bad = [r for r in rows if types.get(r["restaurant_id"], {}).get(t) == "failing"
               and (_parse(r.get("health_since")) or now) <= sustained]
        share = len(bad) / float(n)
        if len(bad) >= FLEET_FAILING_MIN and share > FLEET_FAILING_SHARE:
            out.append(_rule(f"rpower_failing:{t}", f"RPOWER {t} failing for {len(bad)} of {n} stores ({share * 100:.0f}%)",
                             "critical", f"{names(bad)}. One fleet problem, not each store's own.",
                             page="urgent" if share > FLEET_FAILING_URGENT_SHARE else "warn",
                             fold=[r["restaurant_id"] for r in bad]))
    # 2. Stale past 48 hours on the archive or the labor pull (A6-12).
    stale = [r for r in rows if {types.get(r["restaurant_id"], {}).get("labor"),
                                 types.get(r["restaurant_id"], {}).get("archive")} & {"stale"}]
    if len(stale) >= FLEET_MIN_STORES and len(stale) / float(n) > FLEET_STALE_SHARE:
        out.append(_rule("rpower_stale", f"RPOWER data over 48 hours old at {len(stale)} of {n} stores",
                         "warning", f"{names(stale)}. Nothing failed — the nightly runs are not reaching them.",
                         page="warn"))
    # 3. The auth wave (A4-16): a partner token revoked looks like N reconnects.
    auth = [r for r in rows if r.get("auth_failed_at")]
    recent = [r for r in auth if (_parse(r["auth_failed_at"]) or now - timedelta(days=1))
              >= now - timedelta(minutes=AUTH_WAVE_MINUTES)]
    buckets = {}
    for r in auth:
        if r.get("token_bucket"):
            buckets.setdefault(r["token_bucket"], []).append(r)
    shared = [r for b in buckets.values() if len(b) >= 2 for r in b]
    rb = {r["token_bucket"] for r in recent if r.get("token_bucket")}
    if len(recent) >= AUTH_WAVE_STORES or len(rb) >= AUTH_WAVE_BUCKETS:
        out.append(_rule("rpower_auth_wave", f"RPOWER refused {len(recent)} stores' tokens in the last hour",
                         "critical", f"{names(recent)} — across {len(rb)} token{'s' if len(rb) != 1 else ''}. "
                         "The partner token is likely revoked or RPOWER's sign-in is down: one problem, not "
                         "a reconnect per store.", page="urgent",
                         fold=sorted({r["restaurant_id"] for r in recent + shared})))
    elif len(shared) >= 2:
        out.append(_rule("rpower_auth_wave", f"RPOWER refused a shared token for {len(shared)} stores", "critical",
                         f"{names(shared)}. The owners were not asked to reconnect a token they do not hold.",
                         fold=[r["restaurant_id"] for r in shared]))
    # 4. Archive gaps across the fleet (A6-03).
    gaps = [r for r in rows if int(r.get("gaps_14d") or 0) > 0]
    if len(gaps) >= FLEET_MIN_STORES and len(gaps) / float(n) > FLEET_GAP_SHARE:
        out.append(_rule("rpower_gaps", f"{len(gaps)} of {n} RPOWER stores are missing archived nights",
                         "warning", f"{names(gaps)}. Each trading night in the last 14 days owed for over a day.",
                         page="warn"))
    detail = day.get("detail_json") or {}
    # 5. The queue: failed and expired rows by kind (A6-02).
    for kind, k in sorted((detail.get("queue_kinds") or {}).items()):
        bad = int(k.get("failed_24h") or 0) + int(k.get("expired_24h") or 0)
        total = int(k.get("rows_24h") or 0)
        if bad >= QUEUE_FAIL_MIN and total and bad / float(total) > QUEUE_FAIL_SHARE:
            out.append(_rule(f"queue_failed:{kind}", f"{bad} of {total} `{kind}` queue rows failed or expired in 24h",
                             "warning", f"{int(k.get('failed_24h') or 0)} failed, {int(k.get('expired_24h') or 0)} "
                             "expired past their deadline — each is a night or a job that did not happen.",
                             page="warn"))
    # 6. The request budget over the last hour (A6-17).
    one = detail.get("ledger_1h") or {}
    req1 = int(one.get("n") or 0)
    if req1 >= BUDGET_MIN_REQUESTS:
        sk, s4 = int(one.get("skipped") or 0) / float(req1), int(one.get("s429") or 0) / float(req1)
        if sk > BUDGET_SKIPPED_SHARE:
            out.append(_rule("rpower_budget_skipped", f"{sk * 100:.0f}% of RPOWER requests paused by the budget (1h)",
                             "warning", f"{one.get('skipped')} of {req1} requests were not sent: live labor and "
                             "intraday go blank first. Raise RPOWER_FLEET_RPS only if RPOWER allows it.", page="warn"))
        if s4 > BUDGET_429_SHARE:
            out.append(_rule("rpower_budget_429", f"RPOWER answered 429 to {s4 * 100:.1f}% of requests (1h)",
                             "warning", f"{one.get('s429')} of {req1} requests were refused as too many.",
                             page="warn"))
    # 6b. The budget's backpressure and the nightly budget check (pos_capacity,
    # job pos_budget_check): one rule naming what was held and why.
    cap = detail.get("capacity") or {}
    st = cap.get("state") or {}
    findings = detail.get("budget_findings") or []
    if st.get("shed_bulk") or findings:
        bits = []
        if st.get("shed_bulk"):
            bits.append("onboarding and backfill are held tonight"
                        + (f" (demand {st['ratio']:.1f}x the budget)" if isinstance(st.get("ratio"), (int, float)) else ""))
        if st.get("stretch_live"):
            bits.append("live labor reads are stretched")
        bits += [str(f.get("error") or "")[:200] for f in findings[:2]]
        out.append(_rule("rpower_capacity", "The RPOWER request budget is short of the fleet's nightly need",
                         "warning", "; ".join(b for b in bits if b) + ".", page="warn"))
    # 7. Request volume against the 7-day median (A6-17).
    by_day = detail.get("requests_by_day") or {}
    today = now.strftime("%Y-%m-%d")
    full = [(d, v) for d, v in sorted(by_day.items()) if d < today]
    if len(full) >= 4:
        last_d, last_v = full[-1]
        mid = _median([v for _d, v in full[:-1][-7:]])
        if mid and last_v > VOLUME_CONSOLE_X * mid:
            out.append(_rule("rpower_volume", f"RPOWER requests {last_v / float(mid):.1f}x the 7-day median",
                             "warning", f"{last_v:,} requests on {last_d} against a median of {int(mid):,} — "
                             "a night read several times, or a loop.",
                             page="warn" if last_v > VOLUME_PAGE_X * mid else None))
    return out


def page(health=None, q=None, data_type=None, bucket=None, partner=None, anomaly=False, coverage_lt=None,
         include_demo=False, after=None, limit=50, db_path=None) -> dict:
    """The RPOWER table, server-side: filters are indexed WHEREs, the order
    is severity then restaurant, and `after` ("<rank>:<restaurant_id>", the
    last row's `cursor`) is the keyset — no OFFSET scan at 10,000 stores.
    {rows, next, total, counts}."""
    limit = max(1, min(200, int(limit or 50)))
    where, args = [], []
    if not include_demo:
        where.append("is_demo=0")
    hs = [h for h in (health if isinstance(health, (list, tuple)) else str(health or "").split(",")) if h in HEALTH_RANK]
    if hs:
        where.append(f"health IN ({_ids_sql(hs)})")
        args += hs
    if data_type and data_type in DATA_TYPES:
        where.append("(',' || COALESCE(failing_types,'') || ',' || COALESCE(stale_types,'') || ',') LIKE ?")
        args.append(f"%,{data_type},%")
    if bucket:
        where.append("token_bucket=?")
        args.append(str(bucket))
    if partner not in (None, ""):
        try:
            args.append(int(partner))
            where.append("partner_id=?")
        except (TypeError, ValueError):
            pass
    if anomaly:
        where.append("has_anomaly=1")
    if coverage_lt not in (None, ""):
        try:
            args.append(float(coverage_lt))
            where.append("coverage_90d < ?")
        except (TypeError, ValueError):
            pass
    if q:
        s = str(q).strip()
        if s.isdigit():
            where.append("(restaurant_id=? OR store_mid=? OR cg=?)")
            args += [int(s), s, s]
        elif s:
            where.append("LOWER(COALESCE(name,'')) LIKE ?")
            args.append(f"%{s.lower()}%")
    base = (" WHERE " + " AND ".join(where)) if where else ""
    conn = _conn(db_path)
    try:
        total = int((_safe_rows(conn, "rpower_store_health", f"SELECT COUNT(*) AS n FROM rpower_store_health{base}",
                                args) or [{"n": 0}])[0]["n"] or 0)
        kw, kargs = list(where), list(args)
        if after:
            try:
                rank, rid = (int(x) for x in str(after).split(":", 1))
                kw.append("(severity_rank < ? OR (severity_rank = ? AND restaurant_id > ?))")
                kargs += [rank, rank, rid]
            except (TypeError, ValueError):
                pass
        sql = ("SELECT * FROM rpower_store_health" + ((" WHERE " + " AND ".join(kw)) if kw else "")
               + " ORDER BY severity_rank DESC, restaurant_id LIMIT ?")
        rows = [_shape(r) for r in _safe_rows(conn, "rpower_store_health", sql, kargs + [limit + 1])]
        counts = {r["health"]: int(r["n"] or 0) for r in _safe_rows(
            conn, "rpower_store_health", "SELECT health, COUNT(*) AS n FROM rpower_store_health"
            + ("" if include_demo else " WHERE is_demo=0") + " GROUP BY health")}
    finally:
        conn.close()
    more = len(rows) > limit
    rows = rows[:limit]
    for r in rows:
        r["cursor"] = f"{r['severity_rank']}:{r['restaurant_id']}"
    return {"rows": rows, "next": rows[-1]["cursor"] if more and rows else None, "total": total, "counts": counts}


_LAST_OK_COL = {"labor": "last_labor_ok", "archive": "last_archived_at", "change": "last_change_read",
                "catalog": "last_catalog_ok", "prices": "last_prices_ok", "live": "last_live_ok",
                "intraday": "last_intraday_ok", "loss": "last_loss_ok", "dsr": "last_dsr_ok"}


def store_detail(restaurant_id, db_path=None) -> dict:
    """The drill-down drawer for one store: its row, a 90-day calendar from
    pos_archive_days (each night archived / missing / failed / suspect /
    recon or pay gap / restated, with tickets and net against the same
    weekday's median), the data-type timeline, its queue rows, its request
    ledger for 7 days by endpoint, punch issues, the data findings and the
    binding facts."""
    rid = int(restaurant_id)
    now = _utcnow()
    conn = _conn(db_path)
    try:
        row = _safe_rows(conn, "rpower_store_health", "SELECT * FROM rpower_store_health WHERE restaurant_id=?",
                         (rid,))
        since = (now.date() - timedelta(days=WINDOW_DAYS)).isoformat()
        base = "business_date, tickets, lines, punches, net_sales, restated, archived_at, recon_gap, unknown_sales, suspect"
        nights = _safe_rows(conn, "pos_archive_days",
                            f"SELECT {base}, pay_gap, open_punches, store_key, attempts, last_error, first_read "
                            "FROM pos_archive_days WHERE restaurant_id=? AND provider=? AND business_date >= ? "
                            "ORDER BY business_date", (rid, PROVIDER, since),
                            fallback_sql=f"SELECT {base} FROM pos_archive_days WHERE restaurant_id=? AND provider=? "
                                         "AND business_date >= ? ORDER BY business_date")
        kinds = ",".join(f"'{k}'" for k in QUEUE_KINDS)
        queue = _safe_rows(conn, "jobs", "SELECT id, kind, state, attempts, max_attempts, error, not_before, deadline, "
                           f"enqueued_at, finished_at FROM jobs WHERE restaurant_id=? AND kind IN ({kinds}) "
                           "ORDER BY id DESC LIMIT 25", (rid,))
        ledger = _safe_rows(conn, "pos_request_ledger", "SELECT endpoint, SUM(n) AS n, SUM(ok) AS ok, "
                            "SUM(s4xx) AS s4xx, SUM(s429) AS s429, SUM(s5xx) AS s5xx, SUM(transport) AS transport, "
                            "SUM(skipped) AS skipped FROM pos_request_ledger WHERE provider=? AND restaurant_id=? "
                            "AND hour >= ? GROUP BY endpoint ORDER BY SUM(n) DESC",
                            (PROVIDER, rid, (now - timedelta(days=7)).strftime("%Y-%m-%d %H:00:00")))
        findings = _safe_rows(conn, "job_failures", "SELECT job, kind, error, created_at FROM job_failures "
                              "WHERE restaurant_id=? AND created_at >= ? ORDER BY id DESC LIMIT 25",
                              (rid, _stamp(now - timedelta(days=ANOMALY_DAYS))))
        keys = _safe_rows(conn, "pos_archive_days", "SELECT store_key, MIN(business_date) AS first_night, "
                          "MAX(business_date) AS last_night, COUNT(*) AS nights FROM pos_archive_days "
                          "WHERE restaurant_id=? AND provider=? AND store_key IS NOT NULL GROUP BY store_key",
                          (rid, PROVIDER))
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "No RPOWER health row for this restaurant yet — the rollup runs every 15 minutes."}
    r = _shape(row[0])
    gap_set = set(r.get("gap_dates") or [])
    by_wd = {}
    for x in nights:
        if _archived(x):
            try:
                by_wd.setdefault(date.fromisoformat(str(x["business_date"])[:10]).weekday(), []).append(
                    float(x.get("net_sales") or 0))
            except ValueError:
                pass
    med = {wd: _median(v) for wd, v in by_wd.items() if v}
    by_date = {str(x["business_date"])[:10]: x for x in nights}
    cal = []
    start = now.date() - timedelta(days=WINDOW_DAYS - 1)
    for i in range(WINDOW_DAYS):
        d = (start + timedelta(days=i)).isoformat()
        x = by_date.get(d)
        if x and not _archived(x):
            state = "failed"
        elif x and (x.get("suspect") or _first_read_suspect(x.get("first_read"))):
            state = "suspect"
        elif x and x.get("recon_gap") is not None and _night_gap(x["recon_gap"], x.get("net_sales")):
            state = "recon_gap"
        elif x and x.get("pay_gap") is not None and _night_gap(x["pay_gap"], x.get("net_sales")):
            state = "pay_gap"
        elif x and int(x.get("restated") or 0):
            state = "restated"
        elif x:
            state = "archived"
        elif d in gap_set:
            state = "missing"
        else:
            state = "none"
        wd = date.fromisoformat(d).weekday()
        cal.append({"date": d, "state": state,
                    "tickets": int(x.get("tickets") or 0) if x else None,
                    "net_sales": round(float(x.get("net_sales") or 0), 2) if x else None,
                    "weekday_median": round(med[wd], 2) if med.get(wd) is not None else None,
                    "open_punches": x.get("open_punches") if x else None,
                    "last_error": x.get("last_error") if x else None})
    timeline = [{"type": t, "state": (r.get("type_states") or {}).get(t, "none"), "last_ok": r.get(_LAST_OK_COL[t])}
                for t in DATA_TYPES]
    for q_ in queue:
        for c in ("not_before", "deadline", "enqueued_at", "finished_at"):
            if q_.get(c):
                q_[c] = _stamp(_parse(q_[c]))
    for lr in ledger:
        lr["errors"] = sum(int(lr.get(c) or 0) for c in ("s4xx", "s429", "s5xx", "transport"))
    return {"ok": True, "row": r, "calendar": cal, "timeline": timeline, "queue": queue, "ledger_7d": ledger,
            "findings": findings,
            "punches": {"open_7d": r.get("open_punches_7d"), "zero_labor_days_7d": r.get("zero_labor_days_7d")},
            "binding": {"cg": r.get("cg"), "store_mid": r.get("store_mid"), "token_bucket": r.get("token_bucket"),
                        "store_keys": keys, "duplicate_binding": r.get("duplicate_binding"),
                        "store_key_mismatch": r.get("store_key_mismatch"),
                        "simulated_not_demo": r.get("simulated_not_demo")}}


def store_issues(row, now=None) -> list:
    """The console's per-store issues from one shaped row: [{key (after the
    restaurant id), title, severity, detail, since}]. The labor pull and the
    auth latch are the store's RPOWER integration issue (admin_ops); these
    are the rest — the archive, the integrity checks and the data checks
    (never a job failure, A6-04)."""
    if not row or row.get("is_demo"):
        return []
    now = now or _utcnow()
    out = []
    types = row.get("type_states") or {}
    since = row.get("health_since")
    s = _parse(since)
    days = (now - s).total_seconds() / 86400.0 if s is not None else None
    if types.get("archive") in ("failing", "stale"):
        crit = days is not None and days >= STORE_FAILING_CRITICAL_DAYS
        out.append({"key": "rpower:archive", "title": "RPOWER archive: no night stored for 2 trading nights",
                    "severity": "critical" if crit else "warning", "since": since,
                    "detail": "; ".join((row.get("reasons") or [])[:2]) or None})
    elif int(row.get("gaps_14d") or 0) > 0:
        n = int(row["gaps_14d"])
        out.append({"key": "rpower:gap", "title": f"RPOWER archive missing {n} night{'s' if n != 1 else ''} (14 days)",
                    "severity": "warning", "since": None,
                    "detail": "Nights: " + ", ".join(str(d) for d in (row.get("gap_dates") or [])[:6])})
    for col, title in (("duplicate_binding", "This RPOWER store is bound to another restaurant too"),
                       ("store_key_mismatch", "Archived nights from a different RPOWER store than the one bound"),
                       ("simulated_not_demo", "Simulated demo-store token on an account not marked demo")):
        if int(row.get(col) or 0):
            out.append({"key": f"rpower:integrity:{col}", "title": title, "severity": "critical", "since": None,
                        "detail": f"Store {row.get('cg') or '?'}:{row.get('store_mid') or '?'}."})
    if int(row.get("missed_runs_7d") or 0):
        n = int(row["missed_runs_7d"])
        out.append({"key": "rpower:missed", "title": f"{n} RPOWER run{'s' if n != 1 else ''} missed this week",
                    "severity": "warning", "since": None,
                    "detail": "Its queue rows expired unstarted — the POS queue or the request budget was behind. "
                              "The next night's row reads what is still missing."})
    checks = []
    if int(row.get("recon_nights_7d") or 0) >= 2:
        checks.append(f"lines vs ticket totals on {row['recon_nights_7d']} nights")
    if int(row.get("pay_gap_nights_7d") or 0) >= 2:
        checks.append(f"payments vs tickets on {row['pay_gap_nights_7d']} nights")
    net, unk = float(row.get("net_sales_7d") or 0), float(row.get("unknown_sales_7d") or 0)
    if net > 0 and unk > UNKNOWN_SALES_SHARE * net:
        checks.append(f"${unk:,.0f} on unnamed sales types")
    if int(row.get("first_read_suspect_7d") or 0):
        checks.append(f"{row['first_read_suspect_7d']} short first read(s)")
    if int(row.get("suspect_nights_7d") or 0):
        checks.append(f"{row['suspect_nights_7d']} short re-read(s) refused")
    if int(row.get("zero_labor_days_7d") or 0):
        checks.append(f"{row['zero_labor_days_7d']} trading night(s) with no punches")
    if int(row.get("dsr_mismatch_7d") or 0):
        checks.append("the nightly report's net vs the archive")
    if int(row.get("data_issues_7d") or 0):
        checks.append(f"{row['data_issues_7d']} data-check finding(s)")
    if checks:
        out.append({"key": "rpower:data", "title": "RPOWER data checks disagree (7 days)", "severity": "warning",
                    "since": None, "detail": "; ".join(checks) + "."})
    return out
