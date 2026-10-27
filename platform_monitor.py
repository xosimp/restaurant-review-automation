"""
platform_monitor.py — what the web process records about itself, and the
thread that watches it.

  * request rollups (per minute, per route, per traffic class) and every
    5xx, persisted from http_layer's in-memory buffers (#30, #77);
  * boot_events: one row per boot, how the one before it ended, and a page
    when the process is crash-looping (#76);
  * PlatformSupervisor: the web process's own watchdog thread — restarts a
    dead scheduler thread, runs the platform SLA off the request path,
    flushes the telemetry, prunes it (#3, #134);
  * the admin console's system card: every required key, the disk, the
    database, backups, the lease, the drill, the breaker (#28, #102, #125).

Split from status_manager, which keeps the public status page and /health.
"""
import logging
import os
import sqlite3
from datetime import datetime

import status_manager as _sm

log = logging.getLogger(__name__)


# ── platform telemetry tables (created at boot) ─────────────────────────────
#
# boot_events       one row per process boot: when, which build and deployment,
#                   how it ended (#76 — restarts and crash loops left no record).
# http_5xx_log      every 5xx, handled or not: route, status, short error,
#                   request id (#30 — 50 of 73 route error handlers left no trace).
# request_rollups   per minute, per route, per traffic class: counts, errors,
#                   latency buckets, peak concurrency (#77 — the only latency
#                   signal was a five-minute window that reset on deploy).
# tenant_request_rollups  per hour, per restaurant: the busiest restaurants'
#                   customer requests, 5xx and time (scale audit 10/19/26
#                   OPS-9 — the route rollups have no restaurant).
# All of them are written off the request path (PlatformSupervisor) and
# pruned by prune_platform_telemetry.

def _bucket_columns():
    import http_layer
    edges = http_layer.LATENCY_BUCKETS_MS
    return [f"le{e}" for e in edges] + [f"gt{edges[-1]}"]


def _platform_ddl():
    buckets = ", ".join(f"{c} INTEGER NOT NULL DEFAULT 0" for c in _bucket_columns())
    return (
        """CREATE TABLE IF NOT EXISTS boot_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at    TEXT NOT NULL DEFAULT (datetime('now')),
            ready_at      TEXT,
            stopped_at    TEXT,
            pid           INTEGER,
            host          TEXT,
            deployment_id TEXT,
            replica_id    TEXT,
            build         TEXT,
            python        TEXT,
            sqlite        TEXT,
            volume        TEXT,
            db_bytes      INTEGER,
            boot_ms       INTEGER,
            ok            INTEGER,
            error         TEXT,
            previous_exit TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_boot_events_started ON boot_events(started_at)",
        """CREATE TABLE IF NOT EXISTS http_5xx_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            method        TEXT,
            route         TEXT,
            status        INTEGER,
            error         TEXT,
            exc_type      TEXT,
            unhandled     INTEGER NOT NULL DEFAULT 0,
            request_id    TEXT,
            restaurant_id INTEGER,
            klass         TEXT,
            duration_ms   INTEGER,
            authed        INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_http_5xx_created ON http_5xx_log(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_http_5xx_route ON http_5xx_log(route, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_http_5xx_restaurant ON http_5xx_log(restaurant_id)",
        f"""CREATE TABLE IF NOT EXISTS request_rollups (
            minute_at     TEXT NOT NULL,
            route         TEXT NOT NULL,
            method        TEXT NOT NULL,
            klass         TEXT NOT NULL,
            requests      INTEGER NOT NULL DEFAULT 0,
            errors_4xx    INTEGER NOT NULL DEFAULT 0,
            errors_5xx    INTEGER NOT NULL DEFAULT 0,
            total_ms      REAL NOT NULL DEFAULT 0,
            max_ms        REAL NOT NULL DEFAULT 0,
            {buckets},
            max_inflight  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (minute_at, route, method, klass)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_request_rollups_minute ON request_rollups(minute_at)",
        """CREATE TABLE IF NOT EXISTS tenant_request_rollups (
            hour_at       TEXT NOT NULL,
            restaurant_id INTEGER NOT NULL,
            requests      INTEGER NOT NULL DEFAULT 0,
            errors_5xx    INTEGER NOT NULL DEFAULT 0,
            total_ms      REAL NOT NULL DEFAULT 0,
            max_ms        REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (hour_at, restaurant_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_tenant_request_rollups_hour ON tenant_request_rollups(hour_at)",
        # The rehearsals the runbook asks for each quarter (release-safety
        # audit 10/26/26 A05-05 / A05-10): a Railway rollback and a Postgres
        # point-in-time restore into a scratch service, each with the minutes
        # it took. A handful of rows a year; never pruned.
        """CREATE TABLE IF NOT EXISTS release_drills (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            kind         TEXT NOT NULL,
            done_at      TEXT NOT NULL DEFAULT (datetime('now')),
            minutes      REAL,
            ok           INTEGER NOT NULL DEFAULT 1,
            note         TEXT,
            recorded_by  TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_release_drills_kind ON release_drills(kind, done_at)",
    )


# Columns added to a telemetry table after it shipped (boot, add_missing_columns).
# http_5xx_log.authed: the 5xx came from a signed-in customer request (re-audit
# B8 P2, 10/27/26 — the absolute 5xx floor counts only those).
_PLATFORM_ALTERS = ("ALTER TABLE http_5xx_log ADD COLUMN authed INTEGER NOT NULL DEFAULT 0",)


def init_platform_tables(db_path=None):
    """Create the telemetry tables at boot (hosted_dashboard calls this beside
    the other init_* steps). Never on a request path."""
    import models
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        for sql in _platform_ddl():
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()
    models.add_missing_columns(db_path or DB_PATH, _PLATFORM_ALTERS)


def _telemetry_conn(db_path=None):
    """A connection for a telemetry write: a 5-second busy timeout, not the
    usual 30, so a long write lock costs the supervisor a tick, not half a
    minute."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def persist_request_rollups(rows, db_path=None) -> int:
    """Upsert http_layer.drain_rollups() rows, adding to any row already
    there for the same minute and route. Returns rows written; raises on a
    database error so the caller can keep the rows for the next flush."""
    if not rows:
        return 0
    cols = _bucket_columns()
    names = ["minute_at", "route", "method", "klass", "requests", "errors_4xx", "errors_5xx",
             "total_ms", "max_ms"] + cols + ["max_inflight"]
    sums = ["requests", "errors_4xx", "errors_5xx", "total_ms"] + cols
    update = ", ".join([f"{c} = {c} + excluded.{c}" for c in sums]
                       + ["max_ms = MAX(max_ms, excluded.max_ms)",
                          "max_inflight = MAX(max_inflight, excluded.max_inflight)"])
    sql = (f"INSERT INTO request_rollups ({', '.join(names)}) VALUES ({', '.join('?' * len(names))}) "
           f"ON CONFLICT(minute_at, route, method, klass) DO UPDATE SET {update}")
    values = []
    for r in rows:
        b = list(r.get("buckets") or [])
        b = (b + [0] * len(cols))[:len(cols)]
        values.append([r["minute_at"], str(r["route"])[:200], str(r["method"])[:10], str(r["klass"])[:16],
                       int(r["requests"]), int(r["errors_4xx"]), int(r["errors_5xx"]),
                       float(r["total_ms"]), float(r["max_ms"])] + b + [int(r.get("max_inflight") or 0)])
    conn = _telemetry_conn(db_path)
    try:
        conn.executemany(sql, values)
        conn.commit()
    finally:
        conn.close()
    return len(values)


def persist_tenant_rollups(rows, db_path=None) -> int:
    """Upsert http_layer.drain_tenant_rollups() rows, adding to a row
    already there for the hour (a shutdown flush, then the next process).
    Raises on a database error so the caller can keep them."""
    if not rows:
        return 0
    conn = _telemetry_conn(db_path)
    try:
        conn.executemany(
            "INSERT INTO tenant_request_rollups (hour_at, restaurant_id, requests, errors_5xx, total_ms, max_ms) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(hour_at, restaurant_id) DO UPDATE SET "
            "requests = requests + excluded.requests, errors_5xx = errors_5xx + excluded.errors_5xx, "
            "total_ms = total_ms + excluded.total_ms, max_ms = MAX(max_ms, excluded.max_ms)",
            [(r["hour_at"], int(r["restaurant_id"]), int(r["requests"]), int(r["errors_5xx"]),
              float(r["total_ms"]), float(r["max_ms"])) for r in rows])
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def tenant_load(hours=24, limit=10, db_path=None) -> dict:
    """The busiest restaurants over the last `hours`, from the per-tenant
    rollups (OPS-9): {by_requests[], by_time[]} — each {restaurant_id, name,
    requests, errors_5xx, total_seconds, avg_ms, max_ms}. A sample: each hour
    kept its busiest TENANT_ROLLUP_TOP."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        rows = conn.execute(
            "SELECT t.restaurant_id, r.name, SUM(t.requests) AS n, SUM(t.errors_5xx) AS e5, SUM(t.total_ms) AS ms, "
            "MAX(t.max_ms) AS mx FROM tenant_request_rollups t LEFT JOIN restaurants r ON r.id = t.restaurant_id "
            "WHERE t.hour_at >= datetime('now', ?) GROUP BY t.restaurant_id, r.name",
            (f"-{int(hours)} hours",)).fetchall()
    except sqlite3.OperationalError as e:
        log.warning("tenant rollups unreadable: %s", e)
        return {"by_requests": [], "by_time": [], "hours": hours}
    finally:
        conn.close()
    items = [{"restaurant_id": r[0], "name": r[1], "requests": int(r[2] or 0), "errors_5xx": int(r[3] or 0),
              "total_seconds": round(float(r[4] or 0) / 1000.0, 1),
              "avg_ms": round(float(r[4] or 0) / r[2], 1) if r[2] else None, "max_ms": round(float(r[5] or 0), 1)}
             for r in rows]
    return {"hours": hours, "by_requests": sorted(items, key=lambda x: -x["requests"])[:limit],
            "by_time": sorted(items, key=lambda x: -x["total_seconds"])[:limit]}


# Customer requests paged on (scale audit 10/19/26 OPS-7): a 5xx rate over
# the last HTTP_5XX_ALERT_MINUTES (urgent), and a p95 over the SLO held for
# HTTP_P95_ALERT_MINUTES (a warning). Each needs a floor of requests, so a
# quiet night's one failure is not 100%.
HTTP_5XX_ALERT_RATE = float(os.getenv("HTTP_5XX_ALERT_RATE", "0.02"))
HTTP_5XX_ALERT_MIN_REQUESTS = int(os.getenv("HTTP_5XX_ALERT_MIN_REQUESTS", "50"))
HTTP_5XX_ALERT_MINUTES = 5
HTTP_P95_SLO_MS = int(os.getenv("HTTP_P95_SLO_MS", "5000"))
HTTP_P95_ALERT_MIN_REQUESTS = int(os.getenv("HTTP_P95_ALERT_MIN_REQUESTS", "200"))
HTTP_P95_ALERT_MINUTES = 15


# The absolute 5xx floor (release-safety audit 10/26/26 A07-10): the rate
# rule above needs HTTP_5XX_ALERT_MIN_REQUESTS customer requests in five
# minutes, which a quiet hour never has — a deploy that broke a route for
# the ten owners who hit it paged nobody. So also: at least
# HTTP_5XX_FLOOR_ERRORS customer 5xx on at least HTTP_5XX_FLOOR_ROUTES routes
# in HTTP_5XX_FLOOR_MINUTES (urgent), and any 5xx within
# HTTP_5XX_FRESH_BOOT_MINUTES of a boot on a route with none in the
# HTTP_5XX_CLEAN_DAYS before it (a warning: a new failure a release brought).
# The floor counts only SIGNED-IN customer requests (http_5xx_log.authed),
# from at least HTTP_5XX_FLOOR_TENANTS restaurants — a crawler posting
# malformed bodies to two routes paged urgently every hour (re-audit B8
# P2) — and is armed only while the rate rule is not (fewer than
# HTTP_5XX_ALERT_MIN_REQUESTS requests): at peak, five 5xx in 20,000
# requests is the rate rule's call, not a page (re-audit B7 P2).
HTTP_5XX_FLOOR_ERRORS = int(os.getenv("HTTP_5XX_FLOOR_ERRORS", "5"))
HTTP_5XX_FLOOR_ROUTES = int(os.getenv("HTTP_5XX_FLOOR_ROUTES", "2"))
HTTP_5XX_FLOOR_TENANTS = max(1, int(os.getenv("HTTP_5XX_FLOOR_TENANTS", "2")))
HTTP_5XX_FLOOR_MINUTES = 10
HTTP_5XX_FRESH_BOOT_MINUTES = 30
HTTP_5XX_CLEAN_DAYS = 7


def _utc(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_utc(text):
    try:
        return datetime.strptime(str(text)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def latest_boot_at(db_path=None):
    """When the newest boot that came up started (UTC datetime), or None."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        row = conn.execute("SELECT MAX(started_at) FROM boot_events WHERE ok = 1").fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return _parse_utc(row[0]) if row and row[0] else None


def _route_5xx(conn, start, end, classes):
    """{route: 5xx} for customer traffic in [start, end)."""
    marks = ",".join("?" * len(classes))
    return {r[0]: int(r[1] or 0) for r in conn.execute(
        f"SELECT route, SUM(errors_5xx) FROM request_rollups WHERE minute_at >= ? AND minute_at < ? "
        f"AND route <> '*' AND klass IN ({marks}) GROUP BY route HAVING SUM(errors_5xx) > 0",
        (_utc(start), _utc(end)) + tuple(classes)).fetchall()}


def _authed_5xx(conn, start, end, classes):
    """({route: 5xx}, distinct restaurants) of SIGNED-IN customer requests'
    5xx in [start, end) — the absolute floor's count (http_5xx_log.authed)."""
    marks = ",".join("?" * len(classes))
    args = (_utc(start), _utc(end)) + tuple(classes)
    where = (f"FROM http_5xx_log WHERE created_at >= ? AND created_at < ? AND authed = 1 AND klass IN ({marks}) "
             "AND route <> '*'")
    routes = {r[0]: int(r[1] or 0) for r in conn.execute(f"SELECT route, COUNT(*) {where} GROUP BY route",
                                                         args).fetchall()}
    tenants = int(conn.execute(f"SELECT COUNT(DISTINCT restaurant_id) {where} AND restaurant_id IS NOT NULL",
                               args).fetchone()[0] or 0)
    return routes, tenants


def request_slo(db_path=None, now=None, fresh=True) -> dict:
    """{requests, errors_5xx, error_rate, error_rate_over, latency_requests,
    p95_le_ms, latency_over, error_floor_over, fresh_5xx_routes, ...} for
    customer traffic (http_layer.CUSTOMER_CLASSES) from the persisted
    per-minute rollups: the last HTTP_5XX_ALERT_MINUTES whole minutes for the
    5xx rate, the last HTTP_P95_ALERT_MINUTES for the p95 (a bucket edge, as
    the console reads it), the last HTTP_5XX_FLOOR_MINUTES for the absolute
    floor (signed-in requests' 5xx from http_5xx_log, armed only below the
    rate rule's request floor). The minute still filling is not persisted
    yet, so each window ends a minute back. `now` (UTC) for a test; the
    clock otherwise. `fresh` False (the /health read-only call) skips the
    post-boot scan of each failing route's previous HTTP_5XX_CLEAN_DAYS —
    the one read here that is not bounded by minutes (re-audit B7 P3)."""
    import http_layer
    cols = _bucket_columns()
    classes = tuple(http_layer.CUSTOMER_CLASSES)
    marks = ",".join("?" * len(classes))
    bsum = ", ".join(f"SUM({c})" for c in cols)
    from datetime import timedelta
    from models import get_conn, DB_PATH
    ref = now or datetime.utcnow()
    end = ref + timedelta(minutes=1)              # every persisted minute up to now
    boot = latest_boot_at(db_path)
    conn = get_conn(db_path or DB_PATH)
    try:
        err = conn.execute(
            f"SELECT SUM(requests), SUM(errors_5xx) FROM request_rollups WHERE minute_at >= ? "
            f"AND route <> '*' AND klass IN ({marks})",
            (_utc(ref - timedelta(minutes=HTTP_5XX_ALERT_MINUTES + 1)),) + classes).fetchone()
        lat = conn.execute(
            f"SELECT SUM(requests), {bsum} FROM request_rollups WHERE minute_at >= ? "
            f"AND route <> '*' AND klass IN ({marks})",
            (_utc(ref - timedelta(minutes=HTTP_P95_ALERT_MINUTES + 1)),) + classes).fetchone()
        try:
            floor, floor_tenants = _authed_5xx(conn, ref - timedelta(minutes=HTTP_5XX_FLOOR_MINUTES + 1), end,
                                               classes)
        except sqlite3.OperationalError as e:       # a database not migrated yet (authed): no floor
            log.warning("the 5xx floor's log unreadable: %s", e)
            floor, floor_tenants = {}, 0
        fresh_routes = []
        if fresh and boot is not None and ref - boot <= timedelta(minutes=HTTP_5XX_FRESH_BOOT_MINUTES):
            since_boot = _route_5xx(conn, boot, end, classes)
            if since_boot:
                before = _route_5xx(conn, boot - timedelta(days=HTTP_5XX_CLEAN_DAYS), boot, classes)
                fresh_routes = sorted(r for r in since_boot if r not in before)
    finally:
        conn.close()
    floor_errors = sum(floor.values())
    n, e5 = int(err[0] or 0), int(err[1] or 0)
    rate = (e5 / n) if n else 0.0
    ln = int(lat[0] or 0)
    p95 = _bucket_percentile([int(x or 0) for x in lat[1:]], 0.95) if ln else None
    return {"requests": n, "errors_5xx": e5, "error_rate": round(rate, 4), "error_minutes": HTTP_5XX_ALERT_MINUTES,
            "error_rate_alert": HTTP_5XX_ALERT_RATE,
            "error_rate_over": bool(n >= HTTP_5XX_ALERT_MIN_REQUESTS and rate > HTTP_5XX_ALERT_RATE),
            "latency_requests": ln, "latency_minutes": HTTP_P95_ALERT_MINUTES, "p95_le_ms": p95,
            "p95_slo_ms": HTTP_P95_SLO_MS,
            "latency_over": bool(ln >= HTTP_P95_ALERT_MIN_REQUESTS and p95 is not None and p95 > HTTP_P95_SLO_MS),
            "floor_errors": floor_errors, "floor_routes": sorted(floor), "floor_minutes": HTTP_5XX_FLOOR_MINUTES,
            "floor_tenants": floor_tenants,
            "error_floor_over": bool(n < HTTP_5XX_ALERT_MIN_REQUESTS and floor_errors >= HTTP_5XX_FLOOR_ERRORS
                                     and len(floor) >= HTTP_5XX_FLOOR_ROUTES
                                     and floor_tenants >= HTTP_5XX_FLOOR_TENANTS),
            "fresh_5xx_routes": fresh_routes}


def persist_server_errors(samples, db_path=None) -> int:
    """Insert http_layer.drain_server_errors() samples. Raises on a database
    error so the caller can keep them."""
    if not samples:
        return 0
    rows = []
    for s in samples:
        at = datetime.utcfromtimestamp(float(s.get("at") or 0)).strftime("%Y-%m-%d %H:%M:%S") \
            if s.get("at") else datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        rows.append((at, s.get("method"), str(s.get("route") or "")[:200], int(s.get("status") or 500),
                     (s.get("error") or None) and str(s["error"])[:300], s.get("exc_type"),
                     1 if s.get("unhandled") else 0, s.get("request_id"), s.get("restaurant_id"),
                     s.get("klass"), s.get("duration_ms"), 1 if s.get("authed") else 0))
    conn = _telemetry_conn(db_path)
    try:
        conn.executemany(
            "INSERT INTO http_5xx_log (created_at, method, route, status, error, exc_type, unhandled, "
            "request_id, restaurant_id, klass, duration_ms, authed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


# Days each telemetry table is kept. Pruned here (the supervisor, hourly) so
# they are bounded even before ops.prune_ledgers' registry names them.
TELEMETRY_RETENTION_DAYS = {
    "request_rollups": ("minute_at", int(os.getenv("RETAIN_REQUEST_ROLLUPS_DAYS", "14"))),
    "tenant_request_rollups": ("hour_at", int(os.getenv("RETAIN_REQUEST_ROLLUPS_DAYS", "14"))),
    "http_5xx_log":    ("created_at", int(os.getenv("RETAIN_HTTP_5XX_DAYS", "30"))),
    "boot_events":     ("started_at", int(os.getenv("RETAIN_BOOT_EVENTS_DAYS", "180"))),
    "provider_health": ("checked_at", int(os.getenv("RETAIN_PROVIDER_HEALTH_DAYS", "30"))),
}


def prune_platform_telemetry(db_path=None) -> dict:
    """Delete telemetry past its retention. {table: rows deleted}."""
    out = {}
    try:
        conn = _telemetry_conn(db_path)
    except Exception as e:
        log.warning("telemetry prune: could not open the database: %s", e)
        return out
    try:
        for table, (col, days) in TELEMETRY_RETENTION_DAYS.items():
            if days <= 0:
                continue
            try:
                cur = conn.execute(f"DELETE FROM {table} WHERE {col} < datetime('now', ?)",
                                   (f"-{int(days)} days",))
                conn.commit()
                if cur.rowcount and cur.rowcount > 0:
                    out[table] = cur.rowcount
            except sqlite3.OperationalError as e:
                log.debug("telemetry prune skipped %s: %s", table, e)
    finally:
        conn.close()
    return out


def _bucket_percentile(buckets, p):
    """The upper edge (ms) of the latency bucket holding the p-th request,
    from rollup bucket counts; None for no requests. The top bucket has no
    edge: it reads as the last edge, marked by `over`."""
    import http_layer
    edges = list(http_layer.LATENCY_BUCKETS_MS)
    total = sum(buckets)
    if not total:
        return None
    need = total * p
    run = 0
    for i, n in enumerate(buckets):
        run += n
        if run >= need:
            return edges[i] if i < len(edges) else edges[-1]
    return edges[-1]


def request_rollup_summary(hours=24, db_path=None) -> dict:
    """Latency, errors and saturation over the last `hours`, from the
    persisted per-minute rollups: {routes[], classes{}, hourly[],
    saturated_minutes, peak_inflight}. Percentiles are bucket edges
    ("95% within 500 ms"), which is what a rollup can honestly say."""
    import http_layer
    cols = _bucket_columns()
    threads = http_layer.worker_threads() or 4
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        since = f"-{int(hours)} hours"
        bsum = ", ".join(f"SUM({c})" for c in cols)
        routes = conn.execute(
            f"SELECT route, method, klass, SUM(requests), SUM(errors_4xx), SUM(errors_5xx), SUM(total_ms), "
            f"MAX(max_ms), {bsum} FROM request_rollups WHERE minute_at >= datetime('now', ?) AND route <> '*' "
            f"GROUP BY route, method, klass", (since,)).fetchall()
        hourly = conn.execute(
            f"SELECT substr(minute_at, 1, 13) AS hour, klass, SUM(requests), SUM(errors_5xx), {bsum} "
            f"FROM request_rollups WHERE minute_at >= datetime('now', ?) AND route <> '*' "
            f"GROUP BY hour, klass ORDER BY hour", (since,)).fetchall()
        peaks = conn.execute(
            "SELECT substr(minute_at, 1, 13) AS hour, MAX(max_inflight) FROM request_rollups "
            "WHERE minute_at >= datetime('now', ?) AND route = '*' GROUP BY hour", (since,)).fetchall()
        saturated = conn.execute(
            "SELECT COUNT(*) FROM request_rollups WHERE minute_at >= datetime('now', ?) AND route = '*' "
            "AND max_inflight >= ?", (since, threads)).fetchone()[0]
    except sqlite3.OperationalError as e:
        log.warning("request rollups unreadable: %s", e)
        return {"routes": [], "classes": {}, "hourly": [], "saturated_minutes": 0, "peak_inflight": None,
                "hours": hours}
    finally:
        conn.close()

    def _row(n, e4, e5, total, mx, b):
        return {"requests": n, "errors_4xx": e4, "errors_5xx": e5,
                "avg_ms": round(total / n, 1) if n else None, "max_ms": round(mx or 0, 1),
                "p50_le_ms": _bucket_percentile(b, 0.50), "p95_le_ms": _bucket_percentile(b, 0.95)}
    out_routes, classes = [], {}
    for r in routes:
        b = list(r[8:8 + len(cols)])
        out_routes.append({"route": r[0], "method": r[1], "klass": r[2], **_row(r[3], r[4], r[5], r[6], r[7], b)})
        c = classes.setdefault(r[2], [0, 0, 0, 0.0, 0.0, [0] * len(cols)])
        c[0] += r[3]; c[1] += r[4]; c[2] += r[5]; c[3] += r[6]; c[4] = max(c[4], r[7] or 0)
        c[5] = [x + y for x, y in zip(c[5], b)]
    out_routes.sort(key=lambda x: (-x["errors_5xx"], -x["requests"]))
    peak_by_hour = {h: m for h, m in peaks}
    hours_out = {}
    for h, klass, n, e5, *b in hourly:
        slot = hours_out.setdefault(h, {"hour": h + ":00", "customer": [0, 0, [0] * len(cols)],
                                         "admin": [0, 0, [0] * len(cols)], "other": [0, 0, [0] * len(cols)]})
        grp = "customer" if klass in http_layer.CUSTOMER_CLASSES else ("admin" if klass == "admin" else "other")
        s = slot[grp]
        s[0] += n; s[1] += e5; s[2] = [x + y for x, y in zip(s[2], b)]
    hourly_out = []
    for h in sorted(hours_out):
        slot = hours_out[h]
        entry = {"hour": slot["hour"], "peak_inflight": peak_by_hour.get(h)}
        for grp in ("customer", "admin", "other"):
            n, e5, b = slot[grp]
            entry[grp] = {"requests": n, "errors_5xx": e5, "p95_le_ms": _bucket_percentile(b, 0.95)}
        hourly_out.append(entry)
    return {"hours": hours, "routes": out_routes[:100],
            "classes": {k: _row(v[0], v[1], v[2], v[3], v[4], v[5]) for k, v in sorted(classes.items())},
            "hourly": hourly_out, "saturated_minutes": saturated, "threads": threads,
            "peak_inflight": max([m for m in peak_by_hour.values() if m is not None] or [0])}


def recent_server_errors(hours=24, limit=50, db_path=None) -> dict:
    """5xx by route over the last `hours`, and the newest samples."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        since = f"-{int(hours)} hours"
        by_route = [dict(r) for r in conn.execute(
            "SELECT route, method, status, COUNT(*) AS n, MAX(created_at) AS last_at, "
            "SUM(unhandled) AS unhandled FROM http_5xx_log WHERE created_at >= datetime('now', ?) "
            "GROUP BY route, method, status ORDER BY n DESC LIMIT 50", (since,)).fetchall()]
        latest = [dict(r) for r in conn.execute(
            "SELECT created_at, method, route, status, error, exc_type, unhandled, request_id, "
            "restaurant_id, klass, duration_ms FROM http_5xx_log WHERE created_at >= datetime('now', ?) "
            "ORDER BY id DESC LIMIT ?", (since, int(limit))).fetchall()]
        total = conn.execute("SELECT COUNT(*) FROM http_5xx_log WHERE created_at >= datetime('now', ?)",
                             (since,)).fetchone()[0]
    except sqlite3.OperationalError as e:
        log.warning("5xx log unreadable: %s", e)
        return {"hours": hours, "total": 0, "by_route": [], "latest": []}
    finally:
        conn.close()
    return {"hours": hours, "total": total, "by_route": by_route, "latest": latest}


# ── boot events (#76) ───────────────────────────────────────────────────────
#
# A restart used to leave nothing behind but a gap. Each boot now records
# itself, the one before it says whether it ended cleanly (an atexit stamp
# means a clean shutdown; none means it was killed or crashed), and more
# than CRASH_LOOP_BOOTS in an hour pages Will.
CRASH_LOOP_BOOTS = 3


def record_boot_start(db_path=None):
    """Insert this boot's row and return its id (None when the database
    cannot take it — the boot goes on; it will fail on its own if the
    database is really gone). Creates boot_events if needed: this runs
    before init_db, at boot, never on a request."""
    import platform
    import socket
    from models import get_conn, DB_PATH
    path = db_path or DB_PATH
    try:
        conn = get_conn(path)
    except Exception as e:
        log.warning("boot record: could not open the database: %s", e)
        return None
    try:
        conn.execute(_platform_ddl()[0])
        conn.execute(_platform_ddl()[1])
        prev = conn.execute("SELECT stopped_at, ok FROM boot_events ORDER BY id DESC LIMIT 1").fetchone()
        previous_exit = None
        if prev is not None:
            previous_exit = "clean" if prev["stopped_at"] else ("boot_failed" if prev["ok"] == 0 else "unclean")
        try:
            db_bytes = os.path.getsize(path)
        except OSError:
            db_bytes = None
        cur = conn.execute(
            "INSERT INTO boot_events (pid, host, deployment_id, replica_id, build, python, sqlite, volume, "
            "db_bytes, previous_exit) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (os.getpid(), socket.gethostname()[:100], (os.getenv("RAILWAY_DEPLOYMENT_ID") or None),
             (os.getenv("RAILWAY_REPLICA_ID") or None),
             (os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT") or "")[:12] or None,
             platform.python_version(), sqlite3.sqlite_version,
             (os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or None), db_bytes, previous_exit))
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        log.warning("boot record: could not write: %s", e)
        return None
    finally:
        conn.close()


def _update_boot(boot_id, sql, params, db_path=None):
    if not boot_id:
        return
    try:
        conn = _telemetry_conn(db_path)
        try:
            conn.execute(sql, tuple(params) + (boot_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("boot record: could not update boot %s: %s", boot_id, e)


def record_boot_ready(boot_id, boot_ms, db_path=None):
    _update_boot(boot_id, "UPDATE boot_events SET ready_at=datetime('now'), boot_ms=?, ok=1 WHERE id=?",
                 (int(boot_ms),), db_path)


def record_boot_failed(boot_id, error, db_path=None):
    try:
        from ai_guard import redact_secrets
        error = redact_secrets(str(error))
    except Exception:
        error = str(error)[:80]
    _update_boot(boot_id, "UPDATE boot_events SET ok=0, error=? WHERE id=?", (str(error)[:500],), db_path)


def record_boot_stop(boot_id, db_path=None):
    """atexit: a clean shutdown. A boot whose row never gets this was killed."""
    _update_boot(boot_id, "UPDATE boot_events SET stopped_at=datetime('now') WHERE id=?", (), db_path)


def recent_boots(limit=10, db_path=None) -> list:
    from models import get_conn, DB_PATH
    try:
        conn = get_conn(db_path or DB_PATH)
    except Exception:
        return []
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, started_at, ready_at, stopped_at, pid, deployment_id, build, python, sqlite, "
            "boot_ms, ok, error, previous_exit, db_bytes FROM boot_events ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def crash_loop_state(boot_id=None, db_path=None) -> dict:
    """{boots_last_hour, same_deployment, unclean, looping}. A deploy is one
    boot, so four deploys in an hour are not a loop: looping means more
    than CRASH_LOOP_BOOTS boots of THIS deployment, or more than that many
    boots that followed a crash, in the last hour."""
    from models import get_conn, DB_PATH
    out = {"boots_last_hour": 0, "same_deployment": 0, "unclean": 0, "looping": False}
    try:
        conn = get_conn(db_path or DB_PATH)
    except Exception:
        return out
    try:
        dep = None
        if boot_id:
            row = conn.execute("SELECT deployment_id FROM boot_events WHERE id=?", (boot_id,)).fetchone()
            dep = row["deployment_id"] if row else None
        out["boots_last_hour"] = conn.execute(
            "SELECT COUNT(*) FROM boot_events WHERE started_at >= datetime('now', '-1 hour')").fetchone()[0]
        if dep:
            out["same_deployment"] = conn.execute(
                "SELECT COUNT(*) FROM boot_events WHERE started_at >= datetime('now', '-1 hour') "
                "AND deployment_id = ?", (dep,)).fetchone()[0]
        out["unclean"] = conn.execute(
            "SELECT COUNT(*) FROM boot_events WHERE started_at >= datetime('now', '-1 hour') "
            "AND previous_exit IN ('unclean', 'boot_failed')").fetchone()[0]
    except sqlite3.OperationalError:
        return out
    finally:
        conn.close()
    out["looping"] = out["same_deployment"] > CRASH_LOOP_BOOTS or out["unclean"] > CRASH_LOOP_BOOTS
    return out


def page_if_crash_looping(boot_id, db_path=None) -> bool:
    """Tell Will when the process keeps restarting: one alert an hour at
    most, and only where the scheduler would run (a laptop pages nobody).
    Runs on a background thread at boot — never holds the boot up on a
    send. True when an alert went out."""
    state = crash_loop_state(boot_id, db_path)
    if not state["looping"]:
        return False
    try:
        import scheduler
        if not scheduler.scheduling_allowed():
            return False
        import ops
        if not ops.claim_cooldown("crash_loop_alert", 60):
            return False
        boots = recent_boots(6, db_path)
        lines = [f"{state['boots_last_hour']} boots in the last hour "
                 f"({state['same_deployment']} of this deployment, {state['unclean']} after a crash)."]
        for b in boots[:5]:
            lines.append(f"{b['started_at']} UTC · build {b.get('build') or '?'} · "
                         f"{'ready' if b.get('ok') == 1 else 'failed' if b.get('ok') == 0 else 'did not finish booting'}"
                         + (f" · {b['error'][:120]}" if b.get("error") else ""))
        return bool(ops.alert_will("Cavnar AI: the web process keeps restarting", lines))
    except Exception as e:
        log.error("crash-loop page failed: %s", e)
        return False


# ── the quarterly release drills (release-safety audit 10/26/26, A05-05 /
# A05-10) ───────────────────────────────────────────────────────────────────
# Recorded by scripts/record_drill.py after each rehearsal (docs/ops/
# RECOVERY.md → Release drills); the Engineering system card shows the last
# of each and warns past DRILL_STALE_DAYS.
DRILL_KINDS = {"rollback": "Rollback drill", "pg_restore": "Postgres restore drill"}
DRILL_STALE_DAYS = 100


def record_drill(kind, minutes=None, ok=True, note=None, recorded_by=None, db_path=None, done_at=None) -> int:
    if kind not in DRILL_KINDS:
        raise ValueError(f"unknown drill {kind!r} (one of {', '.join(DRILL_KINDS)})")
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        cur = conn.execute("INSERT INTO release_drills (kind, done_at, minutes, ok, note, recorded_by) "
                           "VALUES (?, COALESCE(?, datetime('now')), ?, ?, ?, ?)",
                           (kind, done_at, None if minutes is None else float(minutes), 1 if ok else 0,
                            (note or None) and str(note)[:500], (recorded_by or None) and str(recorded_by)[:100]))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def drill_status(db_path=None, now=None) -> dict:
    """{kind: {label, done_at, age_days, minutes, ok, state}} — state "ok",
    "stale" (over DRILL_STALE_DAYS, or the last one failed) or "never"."""
    from models import get_conn, DB_PATH
    now = now or datetime.utcnow()
    out = {k: {"label": label, "done_at": None, "age_days": None, "minutes": None, "ok": None, "state": "never"}
           for k, label in DRILL_KINDS.items()}
    try:
        conn = get_conn(db_path or DB_PATH)
    except Exception as e:
        return {"error": str(e)[:120], **out}
    try:
        for kind in DRILL_KINDS:
            row = conn.execute("SELECT done_at, minutes, ok FROM release_drills WHERE kind = ? "
                               "ORDER BY done_at DESC, id DESC LIMIT 1", (kind,)).fetchone()
            if not row:
                continue
            done = _parse_utc(row[0])
            age = (now - done).days if done else None
            out[kind].update(done_at=row[0], age_days=age, minutes=row[1], ok=bool(row[2]),
                             state="stale" if (not row[2] or age is None or age > DRILL_STALE_DAYS) else "ok")
    except sqlite3.OperationalError as e:
        return {"error": str(e)[:120], **out}
    finally:
        conn.close()
    return out


# ── a slow boot pages (release-safety audit 10/26/26, A05-09) ───────────────
#
# Every deploy is an outage of old-container-stop plus boot_ms, and a boot
# past the 120-second healthcheck is a failed deploy. Nothing watched the
# trend: a boot over SLOW_BOOT_MS, or SLOW_BOOT_GROWTH over the median of the
# last SLOW_BOOT_BASELINE boots, pages once per build.
SLOW_BOOT_MS = int(os.getenv("SLOW_BOOT_MS", "60000"))
SLOW_BOOT_GROWTH = 1.5
SLOW_BOOT_BASELINE = 10


def slow_boot_state(boot_id, db_path=None) -> dict:
    """{boot_ms, median_ms, slow, reason} for boot `boot_id` against the
    ready boots before it."""
    from models import get_conn, DB_PATH
    out = {"boot_ms": None, "median_ms": None, "slow": False, "reason": None}
    try:
        conn = get_conn(db_path or DB_PATH)
    except Exception:
        return out
    try:
        row = conn.execute("SELECT boot_ms, build FROM boot_events WHERE id=?", (boot_id,)).fetchone()
        if not row or row[0] is None:
            return out
        prior = [int(r[0]) for r in conn.execute(
            "SELECT boot_ms FROM boot_events WHERE id < ? AND ok = 1 AND boot_ms IS NOT NULL "
            "ORDER BY id DESC LIMIT ?", (boot_id, SLOW_BOOT_BASELINE)).fetchall()]
    except sqlite3.OperationalError:
        return out
    finally:
        conn.close()
    ms = int(row[0])
    out["boot_ms"], out["build"] = ms, row[1]
    if prior:
        s = sorted(prior)
        mid = len(s) // 2
        out["median_ms"] = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) // 2
    if ms > SLOW_BOOT_MS:
        out["slow"], out["reason"] = True, f"boot took {ms / 1000:.0f} s (alert past {SLOW_BOOT_MS / 1000:.0f} s)"
    elif out["median_ms"] and len(prior) >= 3 and ms > SLOW_BOOT_GROWTH * out["median_ms"]:
        out["slow"], out["reason"] = True, (f"boot took {ms / 1000:.0f} s, {ms / out['median_ms']:.1f}x the median "
                                            f"of the last {len(prior)} ({out['median_ms'] / 1000:.0f} s)")
    return out


def page_if_slow_boot(boot_id, db_path=None) -> bool:
    """Tell the operator once per build when this boot was slow. Only where
    the scheduler would run (a laptop pages nobody); off the boot path."""
    state = slow_boot_state(boot_id, db_path)
    if not state["slow"]:
        return False
    try:
        import config
        import scheduler
        if not scheduler.scheduling_allowed():
            return False
        import ops
        build = state.get("build") or config.build_label()
        res = ops.page_operator(f"slow_boot:{build}", "Cavnar AI: the last boot was slow",
                                [state["reason"] + f" — build {build}.",
                                 "Each deploy is an outage of the old container's stop plus the boot; past 120 s "
                                 "the healthcheck fails the deploy. The boot's steps are in the deploy log; "
                                 "docs/ops/RECOVERY.md → Deploys."], cooldown_minutes=7 * 24 * 60)
        return bool(res.get("sent"))
    except Exception as e:
        log.error("slow-boot page failed: %s", e)
        return False


# ── release health (release-safety audit 10/26/26, A07-01 / R15) ────────────
#
# The first RELEASE_WINDOW_MINUTES after a DEPLOYMENT's first boot against the
# deployment before it, its last RELEASE_BASELINE_MINUTES. Keyed on Railway's
# deployment id (RAILWAY_DEPLOYMENT_ID), the build SHA alongside (re-audit B7
# P2, 10/27/26): keyed on the SHA, a redeploy for an env-var change and a
# rollback to an earlier SHA were judged on a window from days before and
# never checked. A process with no deployment id (a laptop, a test) falls
# back to the build. Urgent rows mean roll back; warn rows mean hold the next
# deploy. A rollback is advised only for a regression that STARTS with the
# deployment — against the deployment before it, as a rate, with absolute
# minimums — and never for a provider's outage (provider_health), which a
# rollback cannot fix and which costs another downtime window. The supervisor
# runs it once per process, pages once with the deployment, the SHA and the
# rollback step.
RELEASE_WINDOW_MINUTES = 15
RELEASE_BASELINE_MINUTES = 60
RELEASE_SCHEDULER_TICK_MINUTES = 10
# A kind's failed rows in the window that count as a regression: at least
# this many, this share of the kind's finished rows, and this many times its
# share before the deployment.
RELEASE_QUEUE_FAILED_MIN = 3
RELEASE_QUEUE_FAILED_SHARE = 0.2
RELEASE_REGRESSION_RATIO = 3.0
# A provider seen failing this long before the window's end (the probes run
# hourly) explains what fails through it.
RELEASE_PROVIDER_LOOKBACK_MINUTES = 120
# The queue kinds and the AI calls that go through one provider.
_KIND_PROVIDERS = {"pos_archive": ("rpower",), "pos_onboard": ("rpower",), "pos_sync": ("rpower",),
                   "live_labor": ("rpower",), "pos_schedule_push": ("rpower",), "silent_push": ("apns",),
                   "schedule_generate": ("anthropic",), "auto_draft": ("anthropic",)}
ROLLBACK_STEP = ("Roll back: Railway → web service → Deployments → the previous deployment → Redeploy; "
                 "docs/ops/RECOVERY.md → Rolling back a release.")


def current_deployment():
    """This process's Railway deployment id, or None (not on Railway)."""
    return (os.getenv("RAILWAY_DEPLOYMENT_ID") or "").strip() or None


def _release_windows(conn, build, now, deployment=None):
    """(window start, previous build, previous deployment, scope SQL, scope
    args) or None: the deployment's first boot and the deployment (or, with
    no deployment id, the build) that booted before it."""
    if deployment:
        row = conn.execute("SELECT MIN(started_at) FROM boot_events WHERE deployment_id = ?", (deployment,)).fetchone()
        start = _parse_utc(row[0]) if row and row[0] else None
        if start is not None:
            prev = conn.execute("SELECT build, deployment_id FROM boot_events WHERE started_at < ? "
                                "AND deployment_id IS NOT NULL AND deployment_id <> ? "
                                "ORDER BY started_at DESC, id DESC LIMIT 1", (_utc(start), deployment)).fetchone()
            return (start, (prev[0] if prev else None), (prev[1] if prev else None), "deployment_id = ?",
                    (deployment,))
    row = conn.execute("SELECT MIN(started_at) FROM boot_events WHERE build = ?", (build,)).fetchone()
    start = _parse_utc(row[0]) if row and row[0] else None
    if start is None:
        return None
    prev = conn.execute("SELECT build, deployment_id FROM boot_events WHERE started_at < ? AND build IS NOT NULL "
                        "AND build <> ? ORDER BY started_at DESC, id DESC LIMIT 1", (_utc(start), build)).fetchone()
    return start, (prev[0] if prev else None), (prev[1] if prev else None), "build = ?", (build,)


def _requests_window(conn, start, end, classes, cols):
    marks = ",".join("?" * len(classes))
    bsum = ", ".join(f"SUM({c})" for c in cols)
    row = conn.execute(
        f"SELECT SUM(requests), SUM(errors_5xx), {bsum} FROM request_rollups WHERE minute_at >= ? "
        f"AND minute_at < ? AND route <> '*' AND klass IN ({marks})",
        (_utc(start), _utc(end)) + tuple(classes)).fetchone()
    n, e5 = int(row[0] or 0), int(row[1] or 0)
    buckets = [int(x or 0) for x in row[2:]]
    return n, e5, (_bucket_percentile(buckets, 0.95) if n else None)


def _providers_down(conn, start, end) -> set:
    """Providers provider_health saw failing or unreachable in [start, end)."""
    try:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT provider FROM provider_health WHERE checked_at >= ? AND checked_at < ? "
            "AND state IN ('failing', 'error')", (_utc(start), _utc(end))).fetchall()}
    except sqlite3.OperationalError as e:
        log.warning("release health: provider_health unreadable: %s", e)
        return set()


def _regressed(rate, prev_rate):
    """Whether `rate` is a regression on `prev_rate` (RELEASE_REGRESSION_RATIO
    times it, from a floor of 1%)."""
    return rate >= RELEASE_REGRESSION_RATIO * max(float(prev_rate or 0.0), 0.01)


def _queue_window(conn, start, end):
    """{kind: (failed, finished)} of queue rows that finished in [start, end)."""
    import calendar
    a, b = float(calendar.timegm(start.timetuple())), float(calendar.timegm(end.timetuple()))
    out = {}
    for kind, state, n in conn.execute(
            "SELECT kind, state, COUNT(*) FROM jobs WHERE state IN ('failed', 'done') AND finished_at >= ? "
            "AND finished_at < ? GROUP BY kind, state", (a, b)).fetchall():
        f, t = out.get(kind, (0, 0))
        out[kind] = (f + (int(n) if state == "failed" else 0), t + int(n))
    return out


def release_health(build=None, db_path=None, now=None, deployment=None) -> dict:
    """{build, deployment_id, previous_build, previous_deployment,
    window_start, verdict, urgent, warn, ...}. verdict: "pending" (inside the
    window), "regressed" (an urgent row — roll back), "hold" (a warn row —
    hold the next deploy), "ok", or "unknown" (no build named, or no boot of
    it recorded). `deployment` defaults to this process's
    (RAILWAY_DEPLOYMENT_ID)."""
    from datetime import timedelta
    import config
    import http_layer
    from models import get_conn, DB_PATH
    build = build or config.build_label()
    deployment = deployment or current_deployment()
    now = now or datetime.utcnow()
    out = {"build": build, "deployment_id": deployment, "previous_build": None, "previous_deployment": None,
           "window_start": None, "verdict": "unknown", "urgent": [], "warn": []}
    if (not build or build == "unknown") and not deployment:
        return out
    conn = get_conn(db_path or DB_PATH)
    try:
        got = _release_windows(conn, build, now, deployment=deployment)
        if got is None:
            return out
        start, prev, prev_dep, scope_sql, scope_args = got
        out["previous_build"], out["previous_deployment"], out["window_start"] = prev, prev_dep, _utc(start)
        end = start + timedelta(minutes=RELEASE_WINDOW_MINUTES)
        if now < end:
            out["verdict"] = "pending"
            return out
        base_start = start - timedelta(minutes=RELEASE_BASELINE_MINUTES)
        urgent, warn = out["urgent"], out["warn"]
        down = _providers_down(conn, end - timedelta(minutes=RELEASE_PROVIDER_LOOKBACK_MINUTES), end)
        out["providers_down"] = sorted(down)

        # Boots of this deployment that failed (a crash loop on the new code):
        # each failed boot once — ok = 0, or a boot that never came up and was
        # replaced (re-audit B8 P2: the boot after a failed one was counted
        # again through its previous_exit, so one transient failure and a
        # clean restart read "2 failed boots — roll back").
        failed = int(conn.execute(
            f"SELECT COUNT(*) FROM boot_events WHERE {scope_sql} AND (ok = 0 OR (ok IS NULL AND id < "
            f"(SELECT MAX(id) FROM boot_events WHERE {scope_sql})))", scope_args + scope_args).fetchone()[0] or 0)
        if failed >= 2:
            urgent.append(f"{failed} failed boots of this deployment")
        elif failed == 1:
            warn.append("1 failed boot of this deployment (it came up on a restart)")

        # Customer 5xx and latency, against the deployment before it.
        classes, cols = tuple(http_layer.CUSTOMER_CLASSES), _bucket_columns()
        n, e5, p95 = _requests_window(conn, start, end, classes, cols)
        pn, pe5, pp95 = _requests_window(conn, base_start, start, classes, cols)
        routes = _route_5xx(conn, start, end, classes)
        rate, prate = (e5 / n if n else 0.0), (pe5 / pn if pn else 0.0)
        try:
            authed, tenants = _authed_5xx(conn, start, end, classes)
        except sqlite3.OperationalError as e:
            log.warning("release health: the 5xx log unreadable: %s", e)
            authed, tenants = {}, 0
        floor_hit = (sum(authed.values()) >= HTTP_5XX_FLOOR_ERRORS and len(authed) >= HTTP_5XX_FLOOR_ROUTES
                     and tenants >= HTTP_5XX_FLOOR_TENANTS)
        if floor_hit and _regressed(rate, prate):
            urgent.append(f"{sum(authed.values())} signed-in customer 5xx on {len(authed)} routes from {tenants} "
                          f"restaurants in its first {RELEASE_WINDOW_MINUTES} min ({rate * 100:.1f}% against "
                          f"{prate * 100:.1f}% before)")
        elif n >= 20 and e5 >= 2 and _regressed(rate, prate) and rate >= 0.005:
            urgent.append(f"customer 5xx rate {rate * 100:.1f}% against {prate * 100:.1f}% before")
        if routes:
            clean = _route_5xx(conn, start - timedelta(days=HTTP_5XX_CLEAN_DAYS), start, classes)
            fresh = sorted(r for r in routes if r not in clean)
            if fresh:
                warn.append(f"a 5xx on route(s) clean for {HTTP_5XX_CLEAN_DAYS} days: {', '.join(fresh[:4])}")
        if p95 is not None and pp95 and n >= 30:
            if p95 >= 2 * pp95:
                urgent.append(f"p95 within {p95} ms against {pp95} ms before ({n} requests)")
            elif p95 >= 1.5 * pp95:
                warn.append(f"p95 within {p95} ms against {pp95} ms before")
        out["requests"] = {"n": n, "errors_5xx": e5, "p95_le_ms": p95, "previous_n": pn,
                           "previous_errors_5xx": pe5, "previous_p95_le_ms": pp95,
                           "authed_5xx": sum(authed.values()), "authed_tenants": tenants}

        # Jobs that crashed on this deployment after a clean run before it.
        try:
            crashed = [r[0] for r in conn.execute(
                "SELECT DISTINCT job FROM job_runs WHERE started_at >= ? AND started_at < ? AND ok = 0",
                (_utc(start), _utc(end))).fetchall()]
            for job in crashed:
                last = conn.execute("SELECT ok FROM job_runs WHERE job = ? AND started_at < ? "
                                    "ORDER BY started_at DESC LIMIT 1", (job, _utc(start))).fetchone()
                if last is not None and last[0] in (1, 2):
                    urgent.append(f"job {job} crashed; its last run before this deployment was clean")
            for job, runs, bad in conn.execute(
                    "SELECT job, COUNT(*), SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) FROM job_runs "
                    "WHERE started_at >= ? AND started_at < ? GROUP BY job", (_utc(start), _utc(end))).fetchall():
                if int(runs or 0) >= 2 and int(bad or 0) * 2 >= int(runs) and job not in crashed:
                    warn.append(f"job {job}: {int(bad)} of {int(runs)} runs failed")
        except sqlite3.OperationalError as e:
            log.warning("release health: job_runs unreadable: %s", e)

        # Queue rows that failed in the window, per kind, as a share of the
        # kind's finished rows against its share before the deployment (a
        # rate: 3 failed pos_archive rows of 1,500 is no regression).
        try:
            now_q, before_q = _queue_window(conn, start, end), _queue_window(conn, base_start, start)
            for kind, (bad, done) in sorted(now_q.items()):
                if bad <= 0:
                    continue
                share = bad / float(max(done, bad))
                pb, pd = before_q.get(kind, (0, 0))
                pshare = pb / float(pd) if pd else 0.0
                what = f"{bad} of {done} {kind} queue rows failed ({share * 100:.0f}% against {pshare * 100:.0f}% before)"
                if bad >= RELEASE_QUEUE_FAILED_MIN and share >= RELEASE_QUEUE_FAILED_SHARE:
                    blamed = sorted(set(_KIND_PROVIDERS.get(kind, ())) & down)
                    if blamed:
                        warn.append(f"{what} — {', '.join(blamed)} failing at the same time: the provider, not "
                                    "the release")
                    elif _regressed(share, pshare):
                        urgent.append(what)
                    else:
                        warn.append(f"{what} — as before the deployment")
                elif kind == "schedule_generate":
                    warn.append(f"{bad} failed schedule generation(s)")
        except sqlite3.OperationalError as e:
            log.warning("release health: jobs unreadable: %s", e)

        # AI provider errors.
        try:
            def _ai(a, b):
                row = conn.execute("SELECT COUNT(*), SUM(CASE WHEN outcome = 'error' THEN 1 ELSE 0 END) FROM ai_usage "
                                   "WHERE created_at >= ? AND created_at < ?", (_utc(a), _utc(b))).fetchone()
                return int(row[0] or 0), int(row[1] or 0)
            calls, errs = _ai(start, end)
            pcalls, perrs = _ai(base_start, start)
            share, pshare = (errs / calls if calls else 0.0), (perrs / pcalls if pcalls else 0.0)
            if calls >= 10 and share >= 0.2:
                if "anthropic" in down:
                    warn.append(f"AI provider errors on {errs} of {calls} calls — Anthropic failing at the same time "
                                "(provider_health): the provider, not the release")
                elif _regressed(share, pshare):
                    urgent.append(f"AI provider errors on {errs} of {calls} calls ({perrs}/{pcalls} before)")
                else:
                    warn.append(f"AI provider errors on {errs} of {calls} calls — as before the deployment "
                                f"({perrs}/{pcalls})")
            elif calls >= 10 and pcalls and share >= 2 * max(pshare, 0.01):
                warn.append(f"AI provider errors {errs}/{calls} against {perrs}/{pcalls} before")
        except sqlite3.OperationalError as e:
            log.warning("release health: ai_usage unreadable: %s", e)

        # The scheduler completed a tick after the boot (only where it runs).
        if config.scheduling_allowed():
            try:
                row = conn.execute("SELECT loop_completed_at FROM scheduler_heartbeat WHERE id = 1").fetchone()
                done = _parse_utc(row[0]) if row and row[0] else None
                if now >= start + timedelta(minutes=RELEASE_SCHEDULER_TICK_MINUTES) and (done is None or done < start):
                    urgent.append(f"no completed scheduler tick in the {RELEASE_SCHEDULER_TICK_MINUTES} min "
                                  "after the boot")
            except sqlite3.OperationalError as e:
                log.warning("release health: scheduler heartbeat unreadable: %s", e)
    finally:
        conn.close()
    out["verdict"] = "regressed" if urgent else "hold" if warn else "ok"
    return out


def page_release_health(result) -> bool:
    """Page once per deployment when release_health found a regression
    (urgent) or a hold (warn). The page carries the deployment, the SHA and
    the rollback step."""
    if result.get("verdict") not in ("regressed", "hold"):
        return False
    try:
        import scheduler
        if not scheduler.scheduling_allowed():
            return False
        import ops
        build, dep = result.get("build"), result.get("deployment_id")
        what = f"deployment {dep} (build {build or '?'})" if dep else f"build {build}"
        before = (f"deployment {result.get('previous_deployment')} (build {result.get('previous_build') or '?'})"
                  if result.get("previous_deployment") else (result.get("previous_build") or "unknown"))
        lines = [f"{what[0].upper() + what[1:]} (after {before}), first {RELEASE_WINDOW_MINUTES} min from "
                 f"{result.get('window_start')} UTC:"]
        lines += [f"ROLL BACK: {u}" for u in result["urgent"]] + [f"hold: {w}" for w in result["warn"]]
        if result["verdict"] == "regressed":
            lines.append(ROLLBACK_STEP)
            subject = f"Cavnar AI: release {build} regressed — roll back"
        else:
            lines.append("Hold the next deploy until this is understood.")
            subject = f"Cavnar AI: release {build} — hold the next deploy"
        res = ops.page_operator(f"release_health:{dep or build}", subject, lines, cooldown_minutes=7 * 24 * 60)
        return bool(res.get("sent"))
    except Exception as e:
        log.error("release-health page failed: %s", e)
        return False


# ── the web process's supervisor thread ─────────────────────────────────────

class PlatformSupervisor:
    """One background thread in the web process that watches what nothing
    else did:

      * the scheduler thread — started once and never restarted; worker.py
        restarted its loop, the web path only printed (#134). A dead thread
        is restarted, with backoff. A thread that is alive but wedged is
        left alone: starting a second loop in this process would run every
        job twice under the same lease owner. The heartbeat and the
        external dead-man ping cover that case;
      * the platform SLA (ops.check_platform_sla) every five minutes — it
        paged only when something requested /health, and nothing did (#3).
        It runs here, off the request path and outside the scheduler it is
        watching;
      * every tick, the public status page's scheduler row
        (status_manager.check_scheduler_liveness): outage while the loop's
        heartbeat is stale. /health only reads;
      * the request rollups and 5xx samples http_layer buffers, flushed to
        the database every tick, kept for the next tick when the write
        fails;
      * hourly: telemetry retention and the volume marker;
      * once per build, RELEASE_WINDOW_MINUTES after its first boot: the
        release-health comparison with the build before it (release_health),
        paged once with the SHA and the rollback step (release-safety audit
        10/26/26, A07-01).
    """

    def __init__(self, db_path=None, scheduler_thread=None, restart_scheduler=None,
                 sla_every=300, prune_every=3600, marker_every=3600, clock=None, release_every=60):
        import time as _time
        self.db_path = db_path
        self.scheduler_thread = scheduler_thread
        self.restart_scheduler = restart_scheduler
        self.sla_every, self.prune_every, self.marker_every = sla_every, prune_every, marker_every
        self.clock = clock or _time.monotonic
        now = self.clock()
        self._last = {"sla": now, "prune": now - prune_every, "marker": now, "release": now}
        self.release_every = release_every
        self.release_done = False
        self.last_release = None
        self.scheduler_restarts = 0
        self._next_restart_at = 0.0
        self._backoff = 30.0
        self._pending_rollups, self._pending_errors, self._pending_tenants = [], [], []
        import threading
        self._flush_lock = threading.Lock()
        self.last_tick_at = None
        self.last_sla = None
        self.stopping = False

    # -- scheduler thread --------------------------------------------------
    def scheduler_alive(self):
        t = self.scheduler_thread
        return None if t is None else bool(t.is_alive())

    def _watch_scheduler(self, now):
        if self.stopping or self.scheduler_thread is None or self.restart_scheduler is None:
            return
        if self.scheduler_thread.is_alive():
            # An hour alive since the last restart: the next death starts
            # the backoff from the beginning again.
            if now - self._next_restart_at > 3600:
                self._backoff = 30.0
            return
        if now < self._next_restart_at:
            return
        self.scheduler_restarts += 1
        log.error("The scheduler thread has died — restarting it (restart %d)", self.scheduler_restarts)
        try:
            import ops
            ops.capture(RuntimeError("scheduler thread died in the web process; restarted"),
                        job="scheduler_supervisor", context=f"restart {self.scheduler_restarts}")
        except Exception as e:
            log.warning("could not record the scheduler restart: %s", e)
        try:
            self.scheduler_thread = self.restart_scheduler()
        except Exception as e:
            log.error("scheduler restart failed: %s", e)
        # Back off so a loop that dies at once does not spin: 30s, doubling
        # to ten minutes.
        self._next_restart_at = now + self._backoff
        self._backoff = min(600.0, self._backoff * 2)

    # -- telemetry ---------------------------------------------------------
    def flush(self, include_current=False):
        """Persist what http_layer buffered. Called by each tick and once at
        exit (include_current: the minute still filling)."""
        with self._flush_lock:
            return self._flush(include_current)

    def _flush(self, include_current):
        import http_layer
        self._pending_rollups += http_layer.drain_rollups(include_current=include_current)
        self._pending_errors += http_layer.drain_server_errors()
        self._pending_tenants += http_layer.drain_tenant_rollups(include_current=include_current)
        written = {"rollups": 0, "errors": 0, "tenants": 0}
        if self._pending_tenants:
            try:
                written["tenants"] = persist_tenant_rollups(self._pending_tenants, self.db_path)
                self._pending_tenants = []
            except Exception as e:
                log.warning("tenant rollups not persisted (kept for the next tick): %s", e)
                self._pending_tenants = self._pending_tenants[-5000:]
        if self._pending_rollups:
            try:
                written["rollups"] = persist_request_rollups(self._pending_rollups, self.db_path)
                self._pending_rollups = []
            except Exception as e:
                log.warning("request rollups not persisted (kept for the next tick): %s", e)
                self._pending_rollups = self._pending_rollups[-20000:]
        if self._pending_errors:
            try:
                written["errors"] = persist_server_errors(self._pending_errors, self.db_path)
                self._pending_errors = []
            except Exception as e:
                log.warning("5xx samples not persisted (kept for the next tick): %s", e)
                self._pending_errors = self._pending_errors[-2000:]
        return written

    # -- the SLA watchdog ----------------------------------------------------
    def _run_sla(self):
        try:
            import ops
            self.last_sla = ops.check_platform_sla(send=True)
        except Exception as e:
            log.error("platform SLA check failed: %s", e)

    def _check_liveness(self):
        """Flip the public status page's scheduler row to outage when the
        loop's heartbeat has gone stale. Safe here now that the heartbeat is
        its own table (#4): check_scheduler_liveness writes service_status
        only, never the heartbeat it measures. /health stays read-only."""
        try:
            _sm.check_scheduler_liveness(self.db_path)
        except Exception as e:
            log.warning("scheduler liveness check failed: %s", e)

    def _watch_lanes(self):
        """The scheduler's worker lanes (blind re-audit 10/20/26 R1-12): one
        stuck past twice its job's bound is paged once, past three times it
        is replaced (scheduler.watch_lanes). Only where the scheduler runs
        in this process."""
        if self.scheduler_thread is None or self.stopping:
            return
        try:
            import scheduler
            scheduler.watch_lanes()
        except Exception as e:
            log.warning("lane watchdog failed: %s", e)

    def _watch_release(self):
        """This build's release health, once its first window has passed."""
        if self.stopping or self.release_done:
            return
        try:
            res = release_health(db_path=self.db_path)
        except Exception as e:
            log.warning("release health check failed: %s", e)
            return
        if res.get("verdict") == "pending":
            return
        self.last_release = res
        self.release_done = True
        if res.get("verdict") in ("regressed", "hold"):
            log.error("release %s: %s — urgent %s, warn %s", res.get("build"), res.get("verdict"),
                      res.get("urgent"), res.get("warn"))
            page_release_health(res)

    def tick(self, now=None):
        now = self.clock() if now is None else now
        self.flush()
        if now - self._last["release"] >= self.release_every:
            self._last["release"] = now
            self._watch_release()
        self._watch_scheduler(now)
        self._watch_lanes()
        self._check_liveness()
        if now - self._last["sla"] >= self.sla_every:
            self._last["sla"] = now
            self._run_sla()
        if now - self._last["prune"] >= self.prune_every:
            self._last["prune"] = now
            prune_platform_telemetry(self.db_path)
        if now - self._last["marker"] >= self.marker_every:
            self._last["marker"] = now
            _sm.record_volume_marker(self.db_path)
        self.last_tick_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    def run(self, stop_event, interval=30):
        while not stop_event.wait(interval):
            try:
                self.tick()
            except Exception:
                log.exception("platform supervisor tick failed")

    def state(self):
        return {"scheduler_thread": ("none" if self.scheduler_thread is None
                                     else "alive" if self.scheduler_thread.is_alive() else "dead"),
                "scheduler_restarts": self.scheduler_restarts, "last_tick_at": self.last_tick_at,
                "pending_rollups": len(self._pending_rollups), "pending_errors": len(self._pending_errors),
                "release": ({k: (self.last_release or {}).get(k) for k in ("build", "verdict", "urgent", "warn")}
                            if self.last_release is not None else None),
                "last_sla": ({"heartbeat_minutes": (self.last_sla or {}).get("heartbeat_minutes"),
                              "jobs_overdue": len((self.last_sla or {}).get("jobs_overdue") or []),
                              "alerted": (self.last_sla or {}).get("alerted")}
                             if self.last_sla is not None else None)}


# The running process's supervisor (hosted_dashboard sets it at boot), so
# the admin system card can report on it.
SUPERVISOR = None


# ── the admin console's system card (/admin/api/system, #125) ────────────────
#
# The Engineering page checked one variable per provider and said "Every key
# is configured" while BACKUP_ENCRYPTION_KEY and CREDENTIAL_KEY were unset in
# production, and showed no disk, database, backup, lease, drill or breaker
# state at all. key_report() checks every variable docs/ops/SECURITY.md
# lists as required (and validates the ones whose check is cheap);
# system_report() is the physical state of the platform.

def _fernet_ok(value):
    try:
        from cryptography.fernet import Fernet
        Fernet(value.encode() if isinstance(value, str) else value)
        return True
    except Exception:
        return False


# (label, variables — all must be set, group, required, what breaks without it)
KEY_CHECKS = (
    ("Anthropic (Claude)", ("ANTHROPIC_API_KEY",), "provider", True, "AI drafting, narratives and Ask stop"),
    ("Perplexity", ("PERPLEXITY_API_KEY",), "provider", True, "AI-visibility checks stop"),
    ("Resend (email)", ("RESEND_API_KEY",), "provider", True, "every email stops"),
    ("Stripe", ("STRIPE_SECRET_KEY",), "provider", True, "checkout and billing reads fail"),
    ("Stripe webhook", ("STRIPE_WEBHOOK_SECRET",), "security", True, "Stripe events are rejected"),
    ("DocuSign", ("DOCUSIGN_INTEGRATION_KEY",), "provider", True, "contracts cannot be sent"),
    ("DocuSign webhook", ("DOCUSIGN_WEBHOOK_SECRET",), "security", True, "signed-contract events are rejected"),
    ("Twilio (SMS)", ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"), "provider", True, "texts and SMS codes stop"),
    ("Google OAuth", ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"), "provider", True, "Google Business connect fails"),
    ("Google Places", ("GOOGLE_PLACES_API_KEY|GOOGLE_API_KEY",), "provider", True, "review fetch and Intel stop"),
    ("Meta (Instagram)", ("META_APP_ID", "META_APP_SECRET"), "provider", True, "Instagram/Facebook connect fails"),
    ("APNs (push)", ("APNS_KEY_ID", "APNS_TEAM_ID", "APNS_PRIVATE_KEY"), "provider", True, "push notifications stop"),
    ("Sentry", ("SENTRY_DSN",), "provider", True, "errors are not reported"),
    ("Session secret", ("SECRET_KEY",), "security", True,
     "a random key per boot: signed links (QR joins, pay links, connect state) break on every restart"),
    ("Credential key", ("CREDENTIAL_KEY",), "security", True, "client POS/OAuth tokens are stored as plain text"),
    ("Backup encryption key", ("BACKUP_ENCRYPTION_KEY",), "security", True,
     "no encrypted off-site email copy of the database"),
    ("Staff PIN pepper", ("CAVNAR_PIN_PEPPER",), "security", True, "staff PINs cannot be set"),
    ("Admin 2FA enforced", ("ADMIN_REQUIRE_2FA",), "security", True,
     "an admin without two-factor can open the console"),
    ("Off-site backup bucket", ("BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET", "BACKUP_S3_ACCESS_KEY_ID",
                                "BACKUP_S3_SECRET_ACCESS_KEY"), "backup", False,
     "no object-storage copy of the database"),
    ("Operator SMS", ("WILL_PHONE",), "alerting", False, "operator pages go by email and push only"),
    ("Dead-man ping", ("HEALTHCHECK_PING_URL",), "alerting", False,
     "nothing outside Railway notices a stopped scheduler"),
)


def key_report(environ=None) -> list:
    """[{label, vars, group, required, present, state, breaks}] — state is
    ok, missing or invalid. Presence only, never a value."""
    env = os.environ if environ is None else environ

    def _get(spec):
        for n in spec.split("|"):
            v = (env.get(n) or "").strip()
            if v:
                return v
        return ""
    out = []
    for label, names, group, required, breaks in KEY_CHECKS:
        values = [_get(n) for n in names]
        present = all(values)
        state = "ok" if present else "missing"
        if present and label == "Session secret" and (env.get("CAVNAR_SECRET_KEY_EPHEMERAL") or "") == "1":
            # hosted_dashboard generates a per-boot key when SECRET_KEY is
            # unset and writes it back into the environment, so presence
            # alone always read as set.
            present, state = False, "missing"
        if present and label == "Admin 2FA enforced":
            present = values[0].lower() in ("1", "true", "yes")
            state = "ok" if present else "missing"
        if present and label in ("Credential key", "Backup encryption key") and not _fernet_ok(values[0]):
            present, state = False, "invalid"
        out.append({"label": label, "vars": [n.replace("|", " or ") for n in names], "group": group,
                    "required": required, "present": present, "state": state, "breaks": breaks})
    return out


def _last_run(job, db_path=None):
    from models import get_conn, DB_PATH
    try:
        conn = get_conn(db_path or DB_PATH)
    except Exception:
        return None
    try:
        row = conn.execute("SELECT started_at, finished_at, ok, error FROM job_runs WHERE job=? "
                           "ORDER BY id DESC LIMIT 1", (job,)).fetchone()
        return dict(row) if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def _ai_health():
    """G's ai_utils.ai_health() when it exists (breaker, budget, error rate,
    last auth error), else the breaker alone."""
    try:
        import ai_utils
        fn = getattr(ai_utils, "ai_health", None)
        if fn is not None:
            return fn()
        state, remaining = ai_utils.breaker_state("anthropic")
        return {"breaker": {"anthropic": {"state": state, "seconds_remaining": remaining}}}
    except Exception as e:
        return {"error": str(e)[:120]}


def _size_trend(db_path=None):
    """Database size over time: the nightly backup's record of the database,
    WAL, backups and free space (ops.storage_trend, fix round D #28) when it
    has any, with growth per day and days to full; else the size at each
    boot from boot_events."""
    def _mb(v):
        return round(v / (1024 * 1024), 2) if isinstance(v, (int, float)) else None
    try:
        import ops
        st = ops.storage_trend(days=30, db_path=db_path)
        if st.get("rows"):
            return {"source": "daily",
                    "points": [{"at": r.get("date"), "db_mb": _mb(r.get("db_bytes")), "wal_mb": _mb(r.get("wal_bytes")),
                                "backups_mb": _mb(r.get("backups_bytes")), "free_mb": _mb(r.get("free_bytes"))}
                               for r in st["rows"]],
                    "growth_mb_per_day": _mb(st.get("growth_bytes_per_day")),
                    "days_to_full": st.get("days_to_full")}
    except Exception as e:
        log.warning("size history unavailable: %s", e)
    from models import get_conn, DB_PATH
    try:
        conn = get_conn(db_path or DB_PATH)
    except Exception:
        return {"source": "boots", "points": []}
    try:
        rows = conn.execute("SELECT started_at, db_bytes FROM boot_events WHERE db_bytes IS NOT NULL "
                            "AND started_at >= datetime('now', '-30 days') ORDER BY id").fetchall()
        return {"source": "boots", "points": [{"at": r["started_at"],
                                               "db_mb": round(r["db_bytes"] / (1024 * 1024), 2)} for r in rows]}
    except sqlite3.OperationalError:
        return {"source": "boots", "points": []}
    finally:
        conn.close()


def system_report(db_path=None) -> dict:
    """Everything the Engineering system card shows. Admin-only (it names
    backup errors and lease owners); every section degrades to a marker of
    its own failure rather than failing the page."""
    from models import get_conn, DB_PATH
    path = db_path or DB_PATH
    report = {"keys": key_report(), "disk": _sm.disk_state(path)}

    database = {"file": os.path.basename(str(path)), "journal": None, "write": None, "clients": None}
    try:
        conn = get_conn(path)
        try:
            database["journal"] = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
            database["clients"] = _sm.client_restaurant_count(conn)
            if getattr(conn, "in_transaction", False):
                # On Postgres the reads above opened a transaction and the
                # probe refuses to run inside one: the Engineering card read
                # "unknown" (blind re-audit 10/20/26 R9-02; /health's fix,
                # status_manager). Only reads happened — ending it loses nothing.
                conn.rollback()
            database["write"] = _sm.db_write_probe(conn=conn)
        finally:
            conn.close()
    except Exception as e:
        database["error"] = str(e)[:160]
    report["database"] = database
    report["volume"] = {"mount": os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or None,
                        "marker": _sm.read_volume_marker(path)}
    report["backup"] = _sm.backup_health(db_path)
    report["drill"] = _last_run("restore_drill", db_path)
    report["release_drills"] = drill_status(db_path)

    lease = None
    try:
        import ops
        lease = ops.scheduler_lease_holder()
        if lease and lease.get("heartbeat_at"):
            h = _sm._hours_since(lease["heartbeat_at"])
            lease["age_seconds"] = round(h * 3600) if h is not None else None
    except Exception as e:
        lease = {"error": str(e)[:120]}
    report["lease"] = lease
    # The last time the operator was paged, and whether it reached him (#27).
    try:
        import ops as _ops_alert
        report["operator_alert"] = _ops_alert.last_operator_alert()
    except Exception as e:
        report["operator_alert"] = {"error": str(e)[:120]}
    try:
        report["scheduler"] = _sm.scheduler_state(path)
        report["heartbeat_minutes"] = report["scheduler"].get("beat_age_minutes")
    except Exception:
        report["scheduler"], report["heartbeat_minutes"] = None, None
    report["ai"] = _ai_health()
    report["supervisor"] = SUPERVISOR.state() if SUPERVISOR is not None else None

    try:
        import http_layer
        now = http_layer.request_metrics(classes=None)
        report["http"] = {"now": {"by_class": now["by_class"], "inflight": now["inflight"],
                                  "slowest": now["slowest"][-10:]},
                          "dropped": http_layer.dropped_counts()}
    except Exception as e:
        report["http"] = {"error": str(e)[:120]}
    report["rollups_24h"] = request_rollup_summary(24, db_path)
    report["rollups_24h"]["tenants"] = tenant_load(24, db_path=db_path)
    report["server_errors_24h"] = recent_server_errors(24, 50, db_path)
    report["boots"] = {"recent": recent_boots(10, db_path), "crash_loop": crash_loop_state(None, db_path)}
    report["size_trend"] = _size_trend(db_path)
    try:
        import provider_health
        report["providers"] = provider_health.latest(db_path)
    except Exception as e:
        report["providers"] = {"error": str(e)[:120]}
    try:
        import credentials
        report["credentials"] = credentials.status(db_path)
    except Exception as e:
        report["credentials"] = {"error": str(e)[:120]}
    # Which iPhone app builds are installed (live sessions by X-Cavnar-App)
    # and what they could not read in the last day (client_compat, audit #25
    # A05-14 / A06-04).
    try:
        import client_compat
        report["mobile_clients"] = {"installed": client_compat.installed_builds(db_path),
                                    "decode_failures_24h": client_compat.decode_failure_surge(hours=24,
                                                                                               db_path=db_path)}
    except Exception as e:
        report["mobile_clients"] = {"error": str(e)[:120]}
    report["warnings"] = system_warnings(report)
    return report


def system_warnings(report) -> list:
    """Plain sentences for what needs doing, most urgent first."""
    w = []
    cred = report.get("credentials") or {}
    if cred.get("key") == "missing":
        n = cred.get("plaintext") or 0
        w.append(f"CREDENTIAL_KEY is not set: {n} client credential{'s are' if n != 1 else ' is'} "
                 "stored as plain text. Set it (keep a copy off Railway); existing values are "
                 "encrypted at the next boot.")
    elif cred.get("key") == "invalid":
        w.append("CREDENTIAL_KEY is set but is not a valid Fernet key: saving any POS or OAuth "
                 "credential will fail.")
    elif cred.get("plaintext"):
        w.append(f"{cred['plaintext']} client credential(s) are still plain text; they are "
                 "encrypted at the next boot.")
    for k in report.get("keys") or []:
        if k["required"] and k["state"] != "ok" and k["label"] != "Credential key":
            w.append(f"{k['label']} ({', '.join(k['vars'])}) is {k['state']}: {k['breaks']}.")
    disk = report.get("disk") or {}
    if disk.get("state") in ("low", "critical"):
        w.append(f"Volume {disk['state']}: {disk.get('free_mb')} MB free "
                 f"(low below {disk.get('low_below_mb')} MB).")
    b = report.get("backup") or {}
    if b.get("state") == "stale":
        w.append(f"The newest good local backup is {b.get('age_hours')} hours old.")
    if b.get("offsite") == "unconfigured":
        w.append("No off-site backup is configured: losing the volume loses every customer's data.")
    elif b.get("offsite") == "stale":
        w.append(f"The newest off-site backup is {b.get('offsite_age_hours')} hours old.")
    sup = report.get("supervisor") or {}
    if sup.get("scheduler_thread") == "dead":
        w.append("The scheduler thread in the web process is dead.")
    if ((report.get("boots") or {}).get("crash_loop") or {}).get("looping"):
        w.append("The web process is restarting repeatedly (see boots).")
    db = report.get("database") or {}
    if isinstance(db.get("write"), dict) and db["write"].get("state") == "failed":
        w.append("The database cannot take a write.")
    installed = (report.get("mobile_clients") or {}).get("installed") or {}
    if installed.get("below_minimum"):
        n = installed["below_minimum"]
        w.append(f"{n} signed-in iPhone app session{'s run' if n != 1 else ' runs'} a build below "
                 f"MOBILE_MIN_BUILD ({installed.get('min_build')}) and {'are' if n != 1 else 'is'} "
                 "shown the update sheet.")
    return w
