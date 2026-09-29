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
# All three are written off the request path (PlatformSupervisor) and pruned
# by prune_platform_telemetry.

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
            duration_ms   INTEGER
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
    )


def init_platform_tables(db_path=None):
    """Create the telemetry tables at boot (hosted_dashboard calls this beside
    the other init_* steps). Never on a request path."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        for sql in _platform_ddl():
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


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
                     s.get("klass"), s.get("duration_ms")))
    conn = _telemetry_conn(db_path)
    try:
        conn.executemany(
            "INSERT INTO http_5xx_log (created_at, method, route, status, error, exc_type, unhandled, "
            "request_id, restaurant_id, klass, duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


# Days each telemetry table is kept. Pruned here (the supervisor, hourly) so
# they are bounded even before ops.prune_ledgers' registry names them.
TELEMETRY_RETENTION_DAYS = {
    "request_rollups": ("minute_at", int(os.getenv("RETAIN_REQUEST_ROLLUPS_DAYS", "14"))),
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
      * the request rollups and 5xx samples http_layer buffers, flushed to
        the database every tick, kept for the next tick when the write
        fails;
      * hourly: telemetry retention and the volume marker.
    """

    def __init__(self, db_path=None, scheduler_thread=None, restart_scheduler=None,
                 sla_every=300, prune_every=3600, marker_every=3600, clock=None):
        import time as _time
        self.db_path = db_path
        self.scheduler_thread = scheduler_thread
        self.restart_scheduler = restart_scheduler
        self.sla_every, self.prune_every, self.marker_every = sla_every, prune_every, marker_every
        self.clock = clock or _time.monotonic
        now = self.clock()
        self._last = {"sla": now, "prune": now - prune_every, "marker": now}
        self.scheduler_restarts = 0
        self._next_restart_at = 0.0
        self._backoff = 30.0
        self._pending_rollups, self._pending_errors = [], []
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
        written = {"rollups": 0, "errors": 0}
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

    def tick(self, now=None):
        now = self.clock() if now is None else now
        self.flush()
        self._watch_scheduler(now)
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
    """Database size over time: ops.size_history (D's daily record) when it
    exists, else the size at each boot from boot_events."""
    try:
        import ops
        fn = getattr(ops, "size_history", None)
        if fn is not None:
            return {"source": "daily", "points": fn(days=30)}
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
    try:
        report["heartbeat_minutes"] = _sm.scheduler_heartbeat_age_minutes()
    except Exception:
        report["heartbeat_minutes"] = None
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
    return w
