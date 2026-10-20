"""pos_io — the POS integration's traffic controls (scale audit 10/19/26, A3).

Four things every process shares through the database, so the web process,
the scheduler thread and the queue worker see one picture:

  pos_request_ledger  every request a provider client sent (A3-19): one row
                      per UTC hour, provider, restaurant and endpoint with
                      counts by outcome, rows returned and a latency
                      histogram. Aggregated in memory and written at most
                      once per LEDGER_FLUSH_SECONDS per process (one upsert
                      batch), so the hot path costs a dict update. The demo
                      store (`demo-store:` tokens) never reaches it.
  pos_breakers        one circuit breaker per provider (A3-10 / INF-11):
                      opened after BREAKER_FAILURES consecutive transport
                      failures or 5xx answers (from two restaurants, or
                      BREAKER_FAILURES_ONE_STORE from one), held open for a
                      cooldown that doubles on every failed probe (to
                      BREAKER_MAX_COOLDOWN_SECONDS); once it lapses ONE
                      caller, claimed by compare-and-swap, is let through as
                      the half-open probe and its answer closes or reopens
                      it. Every other caller is refused at once with the
                      seconds left (`retry_after`), which a queue row is
                      rescheduled by (job_queue.fail).
  pos_catalogs        a provider's configuration lists (menu, sales types,
                      jobs, employees, tables...) per consolidation group
                      (A3-07): one read serves every store in the group,
                      every process and a cold process after a deploy.
  pos_live_snapshot   the last read of a store's sales so far and clock-ins
                      during service (A3-02): one read per store per
                      LIVE_SNAPSHOT_SECONDS serves live labor, the coverage
                      check, the hourly capture and the /labor/live page.

Every table is created at boot (models.init_db -> init_pos_io) and none of
these functions raises into a caller: a control that cannot reach the
database falls back to "allow" (the breaker) or "not kept" (the caches) and
the request goes ahead as it did before this module.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import models as _models_mod

log = logging.getLogger("pos_io")


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md's bound-import
    hazard: a test's database, never the one imported first)."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()


def _float_env(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


_DDL = (
    """CREATE TABLE IF NOT EXISTS pos_request_ledger (
        hour           TEXT NOT NULL,
        provider       TEXT NOT NULL,
        restaurant_id  INTEGER NOT NULL DEFAULT 0,
        endpoint       TEXT NOT NULL,
        n              INTEGER NOT NULL DEFAULT 0,
        ok             INTEGER NOT NULL DEFAULT 0,
        rows_out       INTEGER NOT NULL DEFAULT 0,
        s4xx           INTEGER NOT NULL DEFAULT 0,
        s429           INTEGER NOT NULL DEFAULT 0,
        s5xx           INTEGER NOT NULL DEFAULT 0,
        transport      INTEGER NOT NULL DEFAULT 0,
        skipped        INTEGER NOT NULL DEFAULT 0,
        ms_total       REAL NOT NULL DEFAULT 0,
        ms_max         REAL NOT NULL DEFAULT 0,
        b250           INTEGER NOT NULL DEFAULT 0,
        b1000          INTEGER NOT NULL DEFAULT 0,
        b3000          INTEGER NOT NULL DEFAULT 0,
        b10000         INTEGER NOT NULL DEFAULT 0,
        bslow          INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (hour, provider, restaurant_id, endpoint)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pos_request_ledger_hour ON pos_request_ledger(hour)",
    """CREATE TABLE IF NOT EXISTS pos_breakers (
        provider       TEXT PRIMARY KEY,
        open_until     REAL NOT NULL DEFAULT 0,
        opened_at      REAL,
        cooldown       REAL NOT NULL DEFAULT 0,
        probe_at       REAL,
        failures       INTEGER NOT NULL DEFAULT 0,
        reason         TEXT,
        paged_for      REAL,
        updated_at     REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS pos_catalogs (
        provider       TEXT NOT NULL,
        scope          TEXT NOT NULL,
        path           TEXT NOT NULL,
        payload        TEXT NOT NULL,
        rows_n         INTEGER NOT NULL DEFAULT 0,
        fetched_at     REAL NOT NULL,
        PRIMARY KEY (provider, scope, path)
    )""",
    """CREATE TABLE IF NOT EXISTS pos_live_snapshot (
        restaurant_id  INTEGER NOT NULL,
        kind           TEXT NOT NULL,
        business_date  TEXT NOT NULL,
        provider       TEXT,
        value_json     TEXT,
        state          TEXT NOT NULL DEFAULT 'ok',
        read_at        REAL NOT NULL,
        PRIMARY KEY (restaurant_id, kind)
    )""",
)


def init_pos_io(db_path=None):
    """The four tables — boot only (models.init_db)."""
    conn = get_conn(db_path)
    try:
        for stmt in _DDL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def reset_for_tests():
    """Forget every in-process memo (conftest, via rpower.clear_caches)."""
    with _ledger_lock:
        _ledger.clear()
        _ledger_state["flushed_at"] = time.monotonic()
    with _breaker_lock:
        _breaker_local.clear()
        _breaker_memo.clear()


# ── the request ledger (A3-19) ──────────────────────────────────────────────

LEDGER_ENABLED = os.getenv("POS_REQUEST_LEDGER", "1").strip().lower() not in ("0", "false", "off", "no")
LEDGER_FLUSH_SECONDS = _float_env("POS_REQUEST_LEDGER_FLUSH_SECONDS", 30)
# Latency buckets (ms): the histogram the console's p50/p95 are read from.
_BUCKETS = ((250, "b250"), (1000, "b1000"), (3000, "b3000"), (10000, "b10000"))
_OUTCOMES = ("ok", "s4xx", "s429", "s5xx", "transport", "skipped")
_COUNTS = ("n", "ok", "rows_out", "s4xx", "s429", "s5xx", "transport", "skipped",
           "b250", "b1000", "b3000", "b10000", "bslow")
_ledger = {}
_ledger_lock = threading.Lock()
_ledger_state = {"flushed_at": time.monotonic(), "warned": False}


def _hour_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:00:00")


def _flush_at_exit():
    try:
        flush()
    except Exception:
        pass


# A deploy or a recycled worker writes what it counted (never more than one
# flush interval is lost to a crash).
import atexit as _atexit
_atexit.register(_flush_at_exit)


def record(provider, restaurant_id, endpoint, outcome, ms=None, rows=None):
    """One request (or one refused before it was sent: outcome 'skipped')
    into this process's hourly aggregate. Never raises, never blocks on the
    database for longer than the flush that falls due."""
    if not LEDGER_ENABLED:
        return
    try:
        key = (_hour_now(), str(provider), int(restaurant_id or 0), str(endpoint or "?")[:80])
        flush_due = False
        with _ledger_lock:
            agg = _ledger.get(key)
            if agg is None:
                agg = _ledger[key] = dict.fromkeys(_COUNTS, 0)
                agg["ms_total"] = 0.0
                agg["ms_max"] = 0.0
            if outcome != "skipped":
                agg["n"] += 1
            agg[outcome if outcome in _OUTCOMES else "s4xx"] += 1
            if rows:
                agg["rows_out"] += int(rows)
            if ms is not None and outcome != "skipped":
                agg["ms_total"] += float(ms)
                agg["ms_max"] = max(agg["ms_max"], float(ms))
                for limit, col in _BUCKETS:
                    if ms < limit:
                        agg[col] += 1
                        break
                else:
                    agg["bslow"] += 1
            if time.monotonic() - _ledger_state["flushed_at"] >= LEDGER_FLUSH_SECONDS:
                _ledger_state["flushed_at"] = time.monotonic()
                flush_due = True
        if flush_due:
            flush()
    except Exception as e:                                   # accounting never fails a request
        log.debug("ledger record skipped: %s", e)


def flush(db_path=None) -> int:
    """Write this process's aggregate to pos_request_ledger (one batch of
    upserts). Returns the rows written. A failed write keeps nothing: the
    counts are an operator's reading, not a record anything depends on."""
    with _ledger_lock:
        items = list(_ledger.items())
        _ledger.clear()
    if not items:
        return 0
    cols = list(_COUNTS)
    adds = ", ".join(f"{c}=pos_request_ledger.{c}+excluded.{c}" for c in cols + ["ms_total"])
    sql = (f"INSERT INTO pos_request_ledger (hour, provider, restaurant_id, endpoint, {', '.join(cols)}, ms_total, "
           f"ms_max) VALUES ({', '.join('?' for _ in range(len(cols) + 6))}) "
           f"ON CONFLICT(hour, provider, restaurant_id, endpoint) DO UPDATE SET {adds}, "
           "ms_max=CASE WHEN excluded.ms_max > pos_request_ledger.ms_max THEN excluded.ms_max "
           "ELSE pos_request_ledger.ms_max END")
    try:
        conn = get_conn(db_path)
        try:
            conn.executemany(sql, [(*k, *[int(v[c]) for c in cols], round(float(v["ms_total"]), 1),
                                    round(float(v["ms_max"]), 1)) for k, v in items])
            conn.commit()
        finally:
            conn.close()
        return len(items)
    except Exception as e:
        if not _ledger_state["warned"]:
            _ledger_state["warned"] = True
            log.warning("pos_request_ledger write failed (counts dropped): %s", e)
        return 0


def _pct_from_buckets(row, q):
    """The latency bucket's upper bound holding the q-th percentile (ms), or
    None with no timed requests."""
    total = sum(int(row.get(c) or 0) for _l, c in _BUCKETS) + int(row.get("bslow") or 0)
    if not total:
        return None
    want, seen = q / 100.0 * total, 0
    for limit, col in _BUCKETS:
        seen += int(row.get(col) or 0)
        if seen >= want:
            return limit
    return int(row.get("ms_max") or 10000)


def summary(hours=24, provider="rpower", db_path=None) -> dict:
    """The console's reading of the ledger: totals, by endpoint, by store
    and by hour over the last `hours`, plus the breaker. p50/p95 are bucket
    upper bounds (<=250 ms, <=1 s, <=3 s, <=10 s, slower)."""
    flush(db_path)
    since = datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).strftime("%Y-%m-%d %H:00:00")
    sums = ", ".join(f"SUM({c}) AS {c}" for c in _COUNTS + ("ms_total",))
    conn = get_conn(db_path)
    try:
        def q(group):
            sel = f"{group}, " if group else ""
            grp = f" GROUP BY {group} ORDER BY n DESC" if group else ""
            return [dict(r) for r in conn.execute(
                f"SELECT {sel}{sums}, MAX(ms_max) AS ms_max FROM pos_request_ledger "
                f"WHERE provider=? AND hour >= ?{grp}", (provider, since)).fetchall()]
        total = (q("") or [{}])[0]
        by_endpoint, by_store = q("endpoint"), q("restaurant_id")
        by_hour = sorted(q("hour"), key=lambda r: r["hour"])
        names = {}
        ids = [int(r["restaurant_id"]) for r in by_store if r.get("restaurant_id")]
        if ids:
            names = {int(r[0]): r[1] for r in conn.execute(
                f"SELECT id, name FROM restaurants WHERE id IN ({','.join('?' for _ in ids)})", ids).fetchall()}
    finally:
        conn.close()

    def shape(r):
        out = {k: v for k, v in r.items() if k not in _COUNTS and k not in ("ms_total", "ms_max")}
        out.update({c: int(r.get(c) or 0) for c in _COUNTS})
        n = out["n"]
        out.update(errors=sum(out[c] for c in ("s4xx", "s429", "s5xx", "transport")),
                   avg_ms=round(float(r.get("ms_total") or 0) / n) if n else None,
                   max_ms=round(float(r.get("ms_max") or 0)) if n else None,
                   p50_ms=_pct_from_buckets(r, 50), p95_ms=_pct_from_buckets(r, 95))
        return out
    for r in by_store:
        r["restaurant"] = names.get(int(r.get("restaurant_id") or 0)) or (
            "no store (connect / probe)" if not r.get("restaurant_id") else f"#{r['restaurant_id']}")
    return {"hours": hours, "provider": provider, "total": shape(total) if total.get("n") is not None else {},
            "by_endpoint": [shape(r) for r in by_endpoint], "by_store": [shape(r) for r in by_store],
            "by_hour": [shape(r) for r in by_hour], "breaker": breaker_state(provider, db_path=db_path),
            "ledger_on": LEDGER_ENABLED}


# ── the circuit breaker (A3-10 / INF-11) ────────────────────────────────────

BREAKER_FAILURES = int(_float_env("POS_BREAKER_FAILURES", 5))
BREAKER_FAILURES_ONE_STORE = int(_float_env("POS_BREAKER_FAILURES_ONE_STORE", 12))
BREAKER_WINDOW_SECONDS = _float_env("POS_BREAKER_WINDOW_SECONDS", 300)
BREAKER_COOLDOWN_SECONDS = _float_env("POS_BREAKER_COOLDOWN_SECONDS", 120)
BREAKER_MAX_COOLDOWN_SECONDS = _float_env("POS_BREAKER_MAX_COOLDOWN_SECONDS", 1800)
# A claimed probe that never reported (its thread died) frees the probe after this.
BREAKER_PROBE_HOLD_SECONDS = 90.0
# How long a process trusts its last read of the shared row.
BREAKER_SYNC_SECONDS = 5.0
# A breaker open this long pages the operator once for the opening (OPS-6).
BREAKER_PAGE_AFTER_SECONDS = _float_env("POS_BREAKER_PAGE_AFTER_SECONDS", 1800)
BREAKER_ENABLED = os.getenv("POS_BREAKER", "1").strip().lower() not in ("0", "false", "off", "no")
_breaker_lock = threading.Lock()
_breaker_local = {}       # provider -> {"fails", "rids", "first_at"}: this process's run of failures
_breaker_memo = {}        # provider -> (monotonic read time, row dict or None)


def _breaker_row(provider, db_path=None, fresh=False):
    now = time.monotonic()
    with _breaker_lock:
        hit = _breaker_memo.get(provider)
        if hit and not fresh and now - hit[0] < BREAKER_SYNC_SECONDS:
            return hit[1]
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT * FROM pos_breakers WHERE provider=?", (provider,)).fetchone()
        finally:
            conn.close()
        row = dict(row) if row else None
    except Exception:
        row = None
    with _breaker_lock:
        _breaker_memo[provider] = (now, row)
    return row


def _write_breaker(provider, db_path=None, **fields):
    fields["updated_at"] = time.time()
    cols = list(fields)
    try:
        conn = get_conn(db_path)
        try:
            conn.execute(f"INSERT INTO pos_breakers (provider, {', '.join(cols)}) VALUES (?{', ?' * len(cols)}) "
                         f"ON CONFLICT(provider) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols)}",
                         (provider, *[fields[c] for c in cols]))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("pos breaker %s not shared: %s", provider, e)
    with _breaker_lock:
        _breaker_memo.pop(provider, None)


def breaker_allow(provider, db_path=None):
    """('closed', 0) go ahead; ('probe', 0) go ahead as the half-open probe
    (report it with probe=True); ('open', seconds) do not send — try again
    in `seconds`."""
    if not BREAKER_ENABLED:
        return "closed", 0.0
    row = _breaker_row(provider, db_path)
    if not row or not float(row.get("open_until") or 0):
        return "closed", 0.0
    now = time.time()
    open_until = float(row["open_until"])
    if open_until > now:
        return "open", open_until - now
    # The cooldown is over: one caller (any process) is the probe.
    try:
        conn = get_conn(db_path)
        try:
            cur = conn.execute("UPDATE pos_breakers SET probe_at=?, updated_at=? WHERE provider=? AND open_until>0 "
                               "AND open_until<=? AND (probe_at IS NULL OR probe_at<?)",
                               (now, now, provider, now, now - BREAKER_PROBE_HOLD_SECONDS))
            conn.commit()
            won = cur.rowcount == 1
        finally:
            conn.close()
    except Exception:
        won = True                                      # can't arbitrate: let it through
    with _breaker_lock:
        _breaker_memo.pop(provider, None)
    return ("probe", 0.0) if won else ("open", 30.0)


def breaker_success(provider, probe=False, db_path=None):
    """The provider answered (any HTTP answer that is not a 5xx). Closes a
    breaker this caller probed, or one it finds open-but-lapsed."""
    with _breaker_lock:
        _breaker_local.pop(provider, None)
    if not BREAKER_ENABLED:
        return
    if probe:
        _write_breaker(provider, db_path, open_until=0.0, probe_at=None, failures=0, cooldown=0.0,
                       reason="closed: the probe was answered")
        log.warning("pos breaker %s closed: the probe was answered", provider)


def breaker_failure(provider, restaurant_id=None, reason="", probe=False, db_path=None) -> bool:
    """A transport failure or a 5xx. Returns True when this opened (or
    reopened) the breaker."""
    if not BREAKER_ENABLED:
        return False
    now = time.time()
    if probe:
        row = _breaker_row(provider, db_path, fresh=True) or {}
        cool = min(BREAKER_MAX_COOLDOWN_SECONDS, max(BREAKER_COOLDOWN_SECONDS, float(row.get("cooldown") or 0) * 2))
        _write_breaker(provider, db_path, open_until=now + cool, cooldown=cool, probe_at=None,
                       failures=int(row.get("failures") or 0) + 1, reason=f"probe failed: {reason}"[:300])
        return True
    with _breaker_lock:
        st = _breaker_local.get(provider)
        if not st or now - st["first_at"] > BREAKER_WINDOW_SECONDS:
            st = _breaker_local[provider] = {"fails": 0, "rids": set(), "first_at": now}
        st["fails"] += 1
        st["rids"].add(int(restaurant_id or 0))
        trip = (st["fails"] >= BREAKER_FAILURES and len(st["rids"]) >= 2) or st["fails"] >= BREAKER_FAILURES_ONE_STORE
        fails = st["fails"]
        if trip:
            _breaker_local.pop(provider, None)
    if not trip:
        return False
    row = _breaker_row(provider, db_path, fresh=True) or {}
    if float(row.get("open_until") or 0) > now:
        return False                                     # another process opened it already
    _write_breaker(provider, db_path, open_until=now + BREAKER_COOLDOWN_SECONDS, opened_at=now,
                   cooldown=BREAKER_COOLDOWN_SECONDS, probe_at=None, failures=fails,
                   reason=f"{fails} failures running: {reason}"[:300], paged_for=None)
    log.error("pos breaker %s OPEN for %ss after %s failures (%s)", provider, BREAKER_COOLDOWN_SECONDS, fails, reason)
    try:
        import ops
        ops.capture(RuntimeError(f"{provider} breaker opened after {fails} failures running: {reason}"[:300]),
                    job="pos_breaker", context=provider)
    except Exception:
        pass
    return True


def breaker_state(provider, db_path=None) -> dict:
    """{state: closed | open | half_open, open_until, opened_at, cooldown,
    failures, reason, open_for_seconds} — the console and the SLA check."""
    row = _breaker_row(provider, db_path, fresh=True) or {}
    now = time.time()
    open_until = float(row.get("open_until") or 0)
    state = "closed" if not open_until else ("open" if open_until > now else "half_open")
    opened = row.get("opened_at")
    return {"state": state, "open_until": open_until or None, "opened_at": opened,
            "cooldown": row.get("cooldown"), "failures": row.get("failures"), "reason": row.get("reason"),
            "open_for_seconds": (round(now - float(opened)) if state != "closed" and opened else 0),
            "paged_for": row.get("paged_for"), "enabled": BREAKER_ENABLED}


def page_if_long_open(provider, db_path=None) -> bool:
    """One page per opening once the breaker has been open (or failing its
    probes) for BREAKER_PAGE_AFTER_SECONDS — the provider's outage as ONE
    page, never one per store (OPS-6). Only where the scheduler runs."""
    st = breaker_state(provider, db_path)
    if st["state"] == "closed" or not st.get("opened_at") or st["open_for_seconds"] < BREAKER_PAGE_AFTER_SECONDS:
        return False
    if st.get("paged_for") and float(st["paged_for"]) == float(st["opened_at"]):
        return False
    try:
        import scheduler
        if not scheduler.scheduling_allowed():
            return False
        import ops
        res = ops.page_operator(f"pos_breaker:{provider}", f"Cavnar AI: {provider.upper()} has been failing for "
                                f"{st['open_for_seconds'] // 60} minutes",
                                [f"Requests to {provider.upper()} are paused by the breaker (every store).",
                                 f"Last reason: {st.get('reason') or 'unknown'}",
                                 "The probe retries on its own; nightly POS jobs reschedule themselves."])
        if res.get("sent"):
            _write_breaker(provider, db_path, paged_for=float(st["opened_at"]))
            return True
    except Exception as e:
        log.warning("pos breaker page failed: %s", e)
    return False


# ── catalogs per consolidation group (A3-07) ────────────────────────────────

def catalog_get(provider, scope, path, db_path=None):
    """(fetched_at epoch, rows) of a kept list, or None."""
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT payload, fetched_at FROM pos_catalogs WHERE provider=? AND scope=? AND path=?",
                               (provider, str(scope), path)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row:
        return None
    try:
        return float(row["fetched_at"]), json.loads(row["payload"])
    except (TypeError, ValueError):
        return None


def catalog_put(provider, scope, path, rows, db_path=None) -> None:
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("INSERT INTO pos_catalogs (provider, scope, path, payload, rows_n, fetched_at) "
                         "VALUES (?,?,?,?,?,?) ON CONFLICT(provider, scope, path) DO UPDATE SET "
                         "payload=excluded.payload, rows_n=excluded.rows_n, fetched_at=excluded.fetched_at",
                         (provider, str(scope), path, json.dumps(rows, default=str), len(rows or []), time.time()))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("pos catalog %s %s not kept: %s", provider, path, e)


# ── the live snapshot (A3-02 / A3-09) ───────────────────────────────────────

# A read during service is shared for this long. Under the 15-minute live
# labor cadence, so that job always reads afresh and the coverage check
# (20 min), the hourly capture and the page reuse its read.
LIVE_SNAPSHOT_SECONDS = _float_env("POS_LIVE_SNAPSHOT_SECONDS", 840)


def snapshot_get(restaurant_id, kind, business_date, db_path=None):
    """{"value", "provider", "state", "read_at", "age"} of the store's last
    live read of `kind` for `business_date`, or None."""
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT business_date, provider, value_json, state, read_at FROM pos_live_snapshot "
                               "WHERE restaurant_id=? AND kind=?", (int(restaurant_id), kind)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row or str(row["business_date"]) != str(business_date)[:10]:
        return None
    try:
        value = json.loads(row["value_json"]) if row["value_json"] is not None else None
    except (TypeError, ValueError):
        return None
    return {"value": value, "provider": row["provider"], "state": row["state"], "read_at": float(row["read_at"]),
            "age": max(0.0, time.time() - float(row["read_at"]))}


def snapshot_put(restaurant_id, kind, business_date, value, provider=None, state="ok", db_path=None) -> None:
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("INSERT INTO pos_live_snapshot (restaurant_id, kind, business_date, provider, value_json, "
                         "state, read_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(restaurant_id, kind) DO UPDATE SET "
                         "business_date=excluded.business_date, provider=excluded.provider, "
                         "value_json=excluded.value_json, state=excluded.state, read_at=excluded.read_at",
                         (int(restaurant_id), kind, str(business_date)[:10], provider,
                          json.dumps(value, default=str), state, time.time()))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("pos live snapshot %s not kept for %s: %s", kind, restaurant_id, e)


_snapshot_locks = {}
_snapshot_locks_guard = threading.Lock()


def snapshot_lock(restaurant_id, kind):
    """This process's single-flight lock for one store's live read."""
    key = (int(restaurant_id), kind)
    with _snapshot_locks_guard:
        lock = _snapshot_locks.get(key)
        if lock is None:
            if len(_snapshot_locks) > 4096:
                _snapshot_locks.clear()
            lock = _snapshot_locks[key] = threading.Lock()
        return lock
