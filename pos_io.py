"""pos_io — the POS integration's traffic controls (scale audit 10/19/26, A3).

What every process shares through the database, so the web process,
the scheduler thread and the queue worker see one picture:

  pos_request_ledger  every request a provider client sent (A3-19): one row
                      per UTC hour, provider, restaurant and endpoint with
                      counts by outcome, rows returned and a latency
                      histogram. Aggregated in memory and written at most
                      once per LEDGER_FLUSH_SECONDS per process (one upsert
                      batch), so the hot path costs a dict update. The demo
                      store (`demo-store:` tokens) never reaches it.
  pos_request_nights  date-scoped reads per business night, store and
                      endpoint (audit #23 A6-24): a night read twice shows.
  pos_breakers        one circuit breaker per provider (A3-10 / INF-11) and
                      one quarantine per store (`<provider>@<restaurant
                      id>`, audit #23 A1-07 / A4-01): a failure is one
                      request that failed after its retries; a store's own
                      run of failed requests parks that store; the
                      provider's row opens only when a share of distinct
                      stores is failing (fleet_should_open). Each is held
                      open for a cooldown that doubles on every failed probe
                      (to BREAKER_MAX_COOLDOWN_SECONDS); once it lapses ONE
                      caller, claimed by compare-and-swap, is let through as
                      the half-open probe and its answer closes or reopens
                      it. Every other caller is refused at once with the
                      seconds left (`retry_after`), which a queue row is
                      rescheduled by (job_queue.fail). A response that breaks
                      the provider's contract from several stores pauses the
                      provider and pages (schema_error).
  pos_auth_latch      a refused token or store, latched (auth_gate /
                      auth_latch / auth_clear; audit #23 A1-13 / A4-16): its
                      requests are refused before they are sent, one
                      re-probe every POS_AUTH_LATCH_SECONDS; a wave of
                      refusals pages once (auth_wave).
  pos_catalogs        a provider's configuration lists (menu, sales types,
                      jobs, employees, tables...) per consolidation group
                      (A3-07): one read serves every store in the group,
                      every process and a cold process after a deploy.
  pos_live_snapshot   the last read of a store's sales so far and clock-ins
                      during service (A3-02): one read per store per
                      LIVE_SNAPSHOT_SECONDS serves live labor, the coverage
                      check, the hourly capture and the /labor/live page.

Each request also carries a LANE (live, close, nightly, bulk — `lane` /
`current_lane` below): the request budget serves live reads ahead of the
nightly and bulk work (blind re-audit 10/20/26 R3-01).

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
    """CREATE TABLE IF NOT EXISTS pos_request_nights (
        night          TEXT NOT NULL,
        provider       TEXT NOT NULL,
        restaurant_id  INTEGER NOT NULL DEFAULT 0,
        endpoint       TEXT NOT NULL,
        reads          INTEGER NOT NULL DEFAULT 0,
        pages          INTEGER NOT NULL DEFAULT 0,
        rows_out       INTEGER NOT NULL DEFAULT 0,
        first_at       TEXT NOT NULL,
        last_at        TEXT NOT NULL,
        PRIMARY KEY (night, provider, restaurant_id, endpoint)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pos_request_nights_last ON pos_request_nights(last_at)",
    """CREATE TABLE IF NOT EXISTS pos_auth_latch (
        provider       TEXT NOT NULL,
        bucket         TEXT NOT NULL,
        store          TEXT NOT NULL DEFAULT '',
        restaurant_id  INTEGER NOT NULL DEFAULT 0,
        status         INTEGER,
        soft           INTEGER NOT NULL DEFAULT 0,
        first_at       REAL NOT NULL,
        failed_at      REAL NOT NULL,
        probe_at       REAL,
        updated_at     REAL NOT NULL,
        PRIMARY KEY (provider, bucket, store)
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
    """The tables — boot only (models.init_db)."""
    conn = get_conn(db_path)
    try:
        for stmt in _DDL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


# ── request lanes (blind re-audit 10/20/26 R3-01) ──────────────────────────
#
# Every RPOWER request shares one token's budget. A request carries the lane
# of the work that made it, and the budget (rpower._sleep_for_spacing) serves
# them in that order:
#   live     the store during service: live labor, the coverage check, the
#            hourly capture (pos._live_read), and any web request;
#   close    the night's report at the POS close (dsr.pipeline);
#   nightly  the nightly jobs (archive, labor sync, loss, depletion) — the
#            default off a request thread;
#   bulk     onboarding and history backfill.
# nightly and bulk are BACKGROUND: they never jump a live or close request
# waiting in this process, and while live/close requests are being made on
# a bucket (anywhere, LIVE_ACTIVE_SECONDS) background work may use only
# RPOWER_BACKGROUND_SHARE of it; bulk never more than that share at all.
LANES = ("live", "close", "nightly", "bulk")
BACKGROUND_LANES = ("nightly", "bulk")
_lane_ctx = threading.local()


class lane:
    """`with pos_io.lane("live"):` — the POS requests made inside, on this
    thread, are this lane's (an unknown name is ignored)."""

    def __init__(self, name):
        self.name = name if name in LANES else None

    def __enter__(self):
        self.prev = getattr(_lane_ctx, "name", None)
        if self.name:
            _lane_ctx.name = self.name
        return self

    def __exit__(self, *exc):
        _lane_ctx.name = self.prev
        return False


def current_lane() -> str:
    """This thread's lane: the one set by `lane`, else "live" on a web
    request thread (an owner is waiting), else "nightly"."""
    name = getattr(_lane_ctx, "name", None)
    if name:
        return name
    try:
        from flask import has_request_context
        if has_request_context():
            return "live"
    except Exception:
        pass
    return "nightly"


def reset_for_tests():
    """Forget every in-process memo (conftest, via rpower.clear_caches)."""
    with _ledger_lock:
        _ledger.clear()
        _ledger_state["flushed_at"] = time.monotonic()
    with _ledger_lock:
        _nights.clear()
    with _breaker_lock:
        _breaker_local.clear()
        _breaker_memo.clear()
        _store_runs.clear()
        _schema_seen.clear()
        _schema_captured.clear()
        _auth_memo.clear()
        _wave_memo.clear()


# ── the request ledger (A3-19) ──────────────────────────────────────────────

LEDGER_ENABLED = os.getenv("POS_REQUEST_LEDGER", "1").strip().lower() not in ("0", "false", "off", "no")
LEDGER_FLUSH_SECONDS = _float_env("POS_REQUEST_LEDGER_FLUSH_SECONDS", 30)
# Latency buckets (ms): the histogram the console's p50/p95 are read from.
_BUCKETS = ((250, "b250"), (1000, "b1000"), (3000, "b3000"), (10000, "b10000"))
_OUTCOMES = ("ok", "s4xx", "s429", "s5xx", "transport", "skipped")
_COUNTS = ("n", "ok", "rows_out", "s4xx", "s429", "s5xx", "transport", "skipped",
           "b250", "b1000", "b3000", "b10000", "bslow")
_ledger = {}
# Date-scoped reads per business night (audit #23 A6-24): one row per
# (night, provider, store, endpoint) with `reads` (first pages — one read of
# the night), `pages` and `rows_out`, so a night read three or four times
# (the R3-02 class) is measurable. Written with the hourly aggregate.
_nights = {}
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


def record(provider, restaurant_id, endpoint, outcome, ms=None, rows=None, night=None, first_page=True):
    """One request (or one refused before it was sent: outcome 'skipped')
    into this process's hourly aggregate — and, for a read of one business
    date (`night`, YYYY-MM-DD), into that night's read count (A6-24;
    `first_page` False for page 2 onward of the same read). Never raises,
    never blocks on the database for longer than the flush that falls due."""
    if not LEDGER_ENABLED:
        return
    try:
        key = (_hour_now(), str(provider), int(restaurant_id or 0), str(endpoint or "?")[:80])
        flush_due = False
        with _ledger_lock:
            if night and outcome != "skipped":
                nk = (str(night)[:10], key[1], key[2], key[3])
                na = _nights.get(nk)
                if na is None:
                    na = _nights[nk] = {"reads": 0, "pages": 0, "rows_out": 0,
                                        "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
                na["pages"] += 1
                na["reads"] += 1 if first_page else 0
                na["rows_out"] += int(rows or 0)
                na["last"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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
        nights = list(_nights.items())
        _nights.clear()
    if nights:
        _flush_nights(nights, db_path)
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


def _flush_nights(items, db_path=None):
    try:
        conn = get_conn(db_path)
        try:
            conn.executemany(
                "INSERT INTO pos_request_nights (night, provider, restaurant_id, endpoint, reads, pages, rows_out, "
                "first_at, last_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(night, provider, restaurant_id, endpoint) "
                "DO UPDATE SET reads=pos_request_nights.reads+excluded.reads, "
                "pages=pos_request_nights.pages+excluded.pages, "
                "rows_out=pos_request_nights.rows_out+excluded.rows_out, last_at=excluded.last_at",
                [(*k, int(v["reads"]), int(v["pages"]), int(v["rows_out"]), v["at"], v.get("last") or v["at"])
                 for k, v in items])
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        if not _ledger_state["warned"]:
            _ledger_state["warned"] = True
            log.warning("pos_request_nights write failed (counts dropped): %s", e)


def night_reads(hours=24, provider="rpower", db_path=None) -> dict:
    """{nights, read_again, extra_reads, worst: [{night, restaurant_id,
    endpoint, reads}]}: business nights whose date-scoped reads were made in
    the last `hours`, how many were read more than once per endpoint, and the
    reads past the first (A6-24)."""
    since = datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_conn(db_path)
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT night, restaurant_id, endpoint, reads FROM pos_request_nights WHERE provider=? "
                "AND last_at>=? ORDER BY reads DESC", (provider, since)).fetchall()]
        finally:
            conn.close()
    except Exception:
        rows = []
    again = [r for r in rows if int(r["reads"] or 0) > 1]
    return {"nights": len({(r["night"], r["restaurant_id"]) for r in rows}), "read_again": len(again),
            "extra_reads": sum(int(r["reads"]) - 1 for r in again), "worst": again[:10]}


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
            "night_reads": night_reads(hours, provider, db_path=db_path), "ledger_on": LEDGER_ENABLED}


# ── the circuit breaker (A3-10 / INF-11) and the store quarantine ──────────
#
# Audit #23 (10/21/26, A1-07 / A4-01): the breaker counted every retry
# ATTEMPT as a failure, opened the fleet-wide row on 12 failures in a row from
# ONE store, and any answer from any store reset the run. One store whose day
# read answered 5xx fast could open it for every store, and its own queue row
# (rescheduled by `retry_after`, never spending a try) re-tripped it after
# each lapse until its 22-hour deadline. Now:
#   * a failure is one REQUEST that failed after its retries (`_request`
#     reports once, at the end), never one per attempt;
#   * one store's failures park THAT store: STORE_QUARANTINE_FAILURES failed
#     requests in a row from it (its own successes reset the run, nobody
#     else's) open its own row, `<provider>@<restaurant id>` in pos_breakers,
#     with the same cooldown, doubling, half-open probe and compare-and-swap
#     claim as the fleet row; its requests raise with `retry_after` and every
#     other store goes on;
#   * the FLEET row opens only on a share of distinct stores failing in
#     BREAKER_WINDOW_SECONDS: at least BREAKER_MIN_STORES (or half the stores
#     this process is serving, when that is fewer — but never one store) AND
#     at least BREAKER_STORE_SHARE of them; or a failure-rate window —
#     BREAKER_FAILURE_RATE of at least BREAKER_MIN_REQUESTS requests failed,
#     from two or more stores. A store is "failing" while its newest request
#     in the window failed.
# Only a 5xx, a transport failure or a non-JSON 200 is a failure. A 429 is
# pressure (rpower's shared slow-down), not a failure and not a success; a
# 401/403 is the auth latch's (below), never the breaker's.

BREAKER_WINDOW_SECONDS = _float_env("POS_BREAKER_WINDOW_SECONDS", 300)
BREAKER_MIN_STORES = max(2, int(_float_env("POS_BREAKER_MIN_STORES", 5)))
BREAKER_STORE_SHARE = min(1.0, max(0.0, _float_env("POS_BREAKER_STORE_SHARE", 0.10)))
BREAKER_FAILURE_RATE = min(1.0, max(0.05, _float_env("POS_BREAKER_FAILURE_RATE", 0.5)))
BREAKER_MIN_REQUESTS = max(2, int(_float_env("POS_BREAKER_MIN_REQUESTS", 20)))
STORE_QUARANTINE_FAILURES = max(1, int(_float_env("POS_STORE_QUARANTINE_FAILURES", 3)))
BREAKER_COOLDOWN_SECONDS = _float_env("POS_BREAKER_COOLDOWN_SECONDS", 120)
BREAKER_MAX_COOLDOWN_SECONDS = _float_env("POS_BREAKER_MAX_COOLDOWN_SECONDS", 1800)
# A claimed probe that never reported (its thread died) frees the probe after this.
BREAKER_PROBE_HOLD_SECONDS = 90.0
# How long a process trusts its last read of the shared row.
BREAKER_SYNC_SECONDS = 5.0
# A breaker open this long pages the operator once for the opening (OPS-6).
BREAKER_PAGE_AFTER_SECONDS = _float_env("POS_BREAKER_PAGE_AFTER_SECONDS", 1800)
BREAKER_ENABLED = os.getenv("POS_BREAKER", "1").strip().lower() not in ("0", "false", "off", "no")
# A response that breaks the provider's contract from this many stores at
# once is the provider's change, not a store's (A1-09 / A4-17): the provider
# is paused this long (the fleet row, reason "schema: ...") and the operator
# is paged at once, once per opening.
SCHEMA_PAUSE_STORES = 2
SCHEMA_PAUSE_SECONDS = _float_env("POS_SCHEMA_PAUSE_SECONDS", 1800)
SCHEMA_REASON = "schema: "
_EVENTS_MAX = 5000
_breaker_lock = threading.Lock()
_breaker_local = {}       # provider -> deque of (time, store, ok): this process's requests in the window
_store_runs = {}          # (provider, store) -> failed requests in a row from that store (this process)
_schema_seen = {}         # provider -> {store: time of its last contract error}
_schema_captured = {}     # (provider, endpoint) -> time an ops event last said so
_breaker_memo = {}        # row key -> (monotonic read time, row dict or None)
import process_caches as _process_caches  # noqa: E402 (registered beside each cache, A04-13)
_process_caches.register("pos_io._breaker_memo", _breaker_memo)


def store_key(provider, restaurant_id) -> str:
    """The pos_breakers row that quarantines one store."""
    return f"{provider}@{int(restaurant_id or 0)}"


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
        if len(_breaker_memo) > 8192:
            _breaker_memo.clear()
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


def _gate(key, db_path=None):
    """('closed', 0) | ('probe', 0) | ('open', seconds) for one pos_breakers row."""
    if not BREAKER_ENABLED:
        return "closed", 0.0
    row = _breaker_row(key, db_path)
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
                               (now, now, key, now, now - BREAKER_PROBE_HOLD_SECONDS))
            conn.commit()
            won = cur.rowcount == 1
        finally:
            conn.close()
    except Exception:
        won = True                                      # can't arbitrate: let it through
    with _breaker_lock:
        _breaker_memo.pop(key, None)
    return ("probe", 0.0) if won else ("open", 30.0)


def breaker_allow(provider, db_path=None):
    """The fleet row: ('closed', 0) go ahead; ('probe', 0) go ahead as the
    half-open probe (report it with probe=True); ('open', seconds) do not
    send — try again in `seconds`."""
    return _gate(provider, db_path)


def store_allow(provider, restaurant_id, db_path=None):
    """One store's quarantine, read like breaker_allow (report a 'probe' with
    store_probe=True). A request for no store is never quarantined."""
    if not restaurant_id:
        return "closed", 0.0
    return _gate(store_key(provider, restaurant_id), db_path)


def _note_event(provider, store, ok, now):
    from collections import deque
    ev = _breaker_local.get(provider)
    if ev is None:
        ev = _breaker_local[provider] = deque(maxlen=_EVENTS_MAX)
    ev.append((now, int(store or 0), bool(ok)))
    while ev and now - ev[0][0] > BREAKER_WINDOW_SECONDS:
        ev.popleft()
    return ev


def _fleet_reading(ev):
    """(active stores, failing stores, requests, failed requests) of a window."""
    last, fails = {}, 0
    for _t, s, ok in ev:
        last[s] = ok
        fails += 0 if ok else 1
    failing = {s for s, ok in last.items() if not ok}
    return set(last), failing, len(ev), fails


def fleet_should_open(active, failing, n, fails) -> bool:
    """The fleet rule (A4-01): a share of distinct stores, or a failure rate
    from two or more stores — never one store on its own."""
    if len(failing) < 2:
        return False
    if len(failing) < BREAKER_STORE_SHARE * len(active):
        return False
    need = min(BREAKER_MIN_STORES, max(2, -(-len(active) // 2)))
    if len(failing) >= need:
        return True
    return n >= BREAKER_MIN_REQUESTS and fails >= BREAKER_FAILURE_RATE * n


def breaker_success(provider, probe=False, db_path=None, restaurant_id=None, store_probe=False):
    """The provider answered (an answer that is not a 5xx or a 429). Closes
    a breaker or quarantine this caller probed, and ends the store's run of
    failures."""
    now = time.time()
    with _breaker_lock:
        _note_event(provider, restaurant_id, True, now)
        _store_runs.pop((provider, int(restaurant_id or 0)), None)
    if not BREAKER_ENABLED:
        return
    if probe:
        _write_breaker(provider, db_path, open_until=0.0, probe_at=None, failures=0, cooldown=0.0,
                       reason="closed: the probe was answered")
        log.warning("pos breaker %s closed: the probe was answered", provider)
    if store_probe and restaurant_id:
        _write_breaker(store_key(provider, restaurant_id), db_path, open_until=0.0, probe_at=None, failures=0,
                       cooldown=0.0, reason="closed: the store answered")


def _reopen(key, reason, db_path=None):
    row = _breaker_row(key, db_path, fresh=True) or {}
    cool = min(BREAKER_MAX_COOLDOWN_SECONDS, max(BREAKER_COOLDOWN_SECONDS, float(row.get("cooldown") or 0) * 2))
    _write_breaker(key, db_path, open_until=time.time() + cool, cooldown=cool, probe_at=None,
                   failures=int(row.get("failures") or 0) + 1, reason=f"probe failed: {reason}"[:300])


def _quarantine(provider, restaurant_id, fails, reason, db_path=None) -> bool:
    key = store_key(provider, restaurant_id)
    now = time.time()
    row = _breaker_row(key, db_path, fresh=True) or {}
    if float(row.get("open_until") or 0) > now:
        return False
    _write_breaker(key, db_path, open_until=now + BREAKER_COOLDOWN_SECONDS, opened_at=now,
                   cooldown=BREAKER_COOLDOWN_SECONDS, probe_at=None, failures=fails,
                   reason=f"{fails} failed requests from this store: {reason}"[:300], paged_for=None)
    log.error("pos quarantine %s for %ss after %s failed requests (%s)", key, BREAKER_COOLDOWN_SECONDS, fails, reason)
    return True


def breaker_failure(provider, restaurant_id=None, reason="", probe=False, db_path=None, store_probe=False) -> bool:
    """One REQUEST that failed after its retries: a transport failure, a 5xx
    or a non-JSON answer. Quarantines its store after
    STORE_QUARANTINE_FAILURES in a row; opens the fleet row only by
    fleet_should_open. Returns True when this opened (or reopened) the fleet
    breaker."""
    if not BREAKER_ENABLED:
        return False
    if probe:
        _reopen(provider, reason, db_path)
        return True
    if store_probe and restaurant_id:
        _reopen(store_key(provider, restaurant_id), reason, db_path)
    now = time.time()
    with _breaker_lock:
        ev = _note_event(provider, restaurant_id, False, now)
        run_key = (provider, int(restaurant_id or 0))
        run = _store_runs[run_key] = _store_runs.get(run_key, 0) + 1
        if len(_store_runs) > 8192:
            _store_runs.clear()
        active, failing, n, fails = _fleet_reading(ev)
        trip = fleet_should_open(active, failing, n, fails)
        if trip:
            ev.clear()
    if restaurant_id and run >= STORE_QUARANTINE_FAILURES and not store_probe:
        if _quarantine(provider, restaurant_id, run, reason, db_path):
            with _breaker_lock:
                _store_runs.pop(run_key, None)
    if not trip:
        return False
    row = _breaker_row(provider, db_path, fresh=True) or {}
    if float(row.get("open_until") or 0) > now:
        return False                                     # another process opened it already
    why = (f"{len(failing)} of {len(active)} stores failing ({fails} of {n} requests in "
           f"{int(BREAKER_WINDOW_SECONDS)}s): {reason}")
    _write_breaker(provider, db_path, open_until=now + BREAKER_COOLDOWN_SECONDS, opened_at=now,
                   cooldown=BREAKER_COOLDOWN_SECONDS, probe_at=None, failures=fails,
                   reason=why[:300], paged_for=None)
    log.error("pos breaker %s OPEN for %ss: %s", provider, BREAKER_COOLDOWN_SECONDS, why)
    try:
        import ops
        ops.capture(RuntimeError(f"{provider} breaker opened: {why}"[:300]), job="pos_breaker", context=provider)
    except Exception:
        pass
    return True


def schema_error(provider, restaurant_id, endpoint, detail, db_path=None) -> bool:
    """A response that broke the provider's contract (rpower.RPowerSchemaError).
    Said once an hour per endpoint as a data-quality event; from
    SCHEMA_PAUSE_STORES stores at once the provider is paused
    (SCHEMA_PAUSE_SECONDS) and page_if_long_open pages at once. Returns True
    when this paused the provider."""
    now = time.time()
    with _breaker_lock:
        seen = _schema_seen.setdefault(provider, {})
        seen[int(restaurant_id or 0)] = now
        for s in [s for s, t in seen.items() if now - t > BREAKER_WINDOW_SECONDS]:
            seen.pop(s, None)
        stores = len(seen)
        say = now - _schema_captured.get((provider, endpoint), 0) > 3600
        if say:
            _schema_captured[(provider, endpoint)] = now
    if say:
        try:
            import ops
            ops.capture(RuntimeError(f"{provider} {endpoint} broke its response contract: {detail}"[:300]),
                        job="pos_schema", context=f"{provider} restaurant_id={int(restaurant_id or 0)}",
                        restaurant_id=int(restaurant_id or 0) or None, kind="data_quality", db_path=db_path)
        except Exception:
            pass
    if not BREAKER_ENABLED or stores < SCHEMA_PAUSE_STORES:
        return False
    row = _breaker_row(provider, db_path, fresh=True) or {}
    if float(row.get("open_until") or 0) > now and str(row.get("reason") or "").startswith(SCHEMA_REASON):
        return False
    _write_breaker(provider, db_path, open_until=now + SCHEMA_PAUSE_SECONDS, opened_at=now,
                   cooldown=SCHEMA_PAUSE_SECONDS, probe_at=None, failures=stores,
                   reason=f"{SCHEMA_REASON}{endpoint} from {stores} stores: {detail}"[:300], paged_for=None)
    log.error("pos breaker %s PAUSED for %ss: %s broke its contract from %s stores", provider,
              SCHEMA_PAUSE_SECONDS, endpoint, stores)
    with _breaker_lock:
        _schema_seen.pop(provider, None)
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
            "paged_for": row.get("paged_for"), "enabled": BREAKER_ENABLED,
            "quarantined": quarantined(provider, db_path=db_path)}


def quarantined(provider, db_path=None) -> list:
    """[{restaurant_id, open_until, reason, failures}] — the stores whose own
    quarantine is open or awaiting its probe (the console's reading)."""
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute("SELECT provider, open_until, reason, failures FROM pos_breakers "
                                "WHERE provider LIKE ? AND open_until > 0", (f"{provider}@%",)).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            rid = int(str(r["provider"]).split("@", 1)[1])
        except (IndexError, ValueError):
            continue
        out.append({"restaurant_id": rid, "open_until": float(r["open_until"]), "reason": r["reason"],
                    "failures": r["failures"]})
    return out


def page_if_long_open(provider, db_path=None) -> bool:
    """One page per opening once the breaker has been open (or failing its
    probes) for BREAKER_PAGE_AFTER_SECONDS — the provider's outage as ONE
    page, never one per store (OPS-6); a provider paused for a broken
    response contract pages at once (A1-09). Also pages a wave of refused
    tokens once (auth_wave, A4-16). Only where the scheduler runs."""
    paged = False
    try:
        paged = _page_auth_wave(provider, db_path)
    except Exception as e:
        log.warning("pos auth-wave page failed: %s", e)
    st = breaker_state(provider, db_path)
    schema = str(st.get("reason") or "").startswith(SCHEMA_REASON)
    if st["state"] == "closed" or not st.get("opened_at"):
        return paged
    if not schema and st["open_for_seconds"] < BREAKER_PAGE_AFTER_SECONDS:
        return paged
    if st.get("paged_for") and float(st["paged_for"]) == float(st["opened_at"]):
        return paged
    try:
        import scheduler
        if not scheduler.scheduling_allowed():
            return paged
        import ops
        if schema:
            res = ops.page_operator(f"pos_schema:{provider}", f"Cavnar AI: {provider.upper()} changed its answers — "
                                    "requests are paused",
                                    [f"{provider.upper()} answered in a shape Cavnar AI does not recognise, from "
                                     "more than one store. Nothing it sent was stored.",
                                     f"Detail: {st.get('reason') or 'unknown'}",
                                     f"Every {provider.upper()} request is paused for "
                                     f"{int(SCHEMA_PAUSE_SECONDS // 60)} minutes, then one probe tries again."])
        else:
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
    return paged


# ── the auth latch (audit #23 A1-13 / A4-16 / A6-09) ───────────────────────
#
# A refused token kept being used: every live slot, DSR poll and nightly
# read of a store whose token RPOWER had revoked sent another 401 — about
# 100 a day per store — and a partner token revoked looked like 1,500
# separate reconnect problems. Now a refusal latches: one pos_auth_latch row
# per (provider, token bucket — a hash, never the token — and store; '' for
# the token itself). While a row is younger than AUTH_LATCH_SECONDS that
# store's (or token's) requests are refused before they are sent; then ONE
# request, claimed by compare-and-swap, is the re-probe — an answer clears
# the latch, a refusal renews it. A token is latched whole when a call made
# with it alone (store/get) is refused, or AUTH_WAVE_STORES of its stores are. Many
# stores (AUTH_WAVE_STORES) or tokens (AUTH_WAVE_TOKENS) newly refused within
# an hour is a wave: the operator is paged once and the owners' "hasn't
# synced" notices are held (auth_wave). rpower writes the store's
# restaurants.rpower_auth_failed_at beside it.
AUTH_LATCH_SECONDS = _float_env("POS_AUTH_LATCH_SECONDS", 900)
AUTH_WAVE_WINDOW_SECONDS = 3600.0
AUTH_WAVE_STORES = 3
AUTH_WAVE_TOKENS = 2
_auth_memo = {}           # (provider, bucket, store) -> (monotonic read time, gate answer)
_process_caches.register("pos_io._auth_memo", _auth_memo)
_wave_memo = {}           # provider -> (monotonic read time, wave dict)
_process_caches.register("pos_io._wave_memo", _wave_memo)


def _auth_rows(conn, provider, bucket, store):
    return [dict(r) for r in conn.execute(
        "SELECT store, soft, failed_at, probe_at FROM pos_auth_latch WHERE provider=? AND bucket=? "
        "AND store IN (?, '') AND soft=0", (provider, bucket, str(store or ""))).fetchall()]


def auth_gate(provider, bucket, store=None, db_path=None):
    """('go', 0) send; ('probe', 0) send as the latch's re-probe (report the
    answer: auth_clear on any answer, auth_latch on a refusal); ('latched',
    seconds) do not send. Unreadable reads as 'go'."""
    key = (provider, bucket, str(store or ""))
    now_m = time.monotonic()
    with _breaker_lock:
        hit = _auth_memo.get(key)
    if hit and now_m - hit[0] < BREAKER_SYNC_SECONDS and hit[1][0] != "probe":
        if hit[1][0] == "latched":
            return "latched", max(1.0, hit[1][1] - (now_m - hit[0]))
        return hit[1]
    try:
        conn = get_conn(db_path)
        try:
            rows = _auth_rows(conn, provider, bucket, store)
            if not rows:
                answer = ("go", 0.0)
            else:
                now = time.time()
                newest = max(float(r["failed_at"]) for r in rows)
                left = newest + AUTH_LATCH_SECONDS - now
                if left > 0:
                    answer = ("latched", left)
                else:
                    won = 0
                    for r in rows:
                        cur = conn.execute(
                            "UPDATE pos_auth_latch SET probe_at=?, updated_at=? WHERE provider=? AND bucket=? "
                            "AND store=? AND soft=0 AND failed_at<=? AND (probe_at IS NULL OR probe_at<?)",
                            (now, now, provider, bucket, r["store"], now - AUTH_LATCH_SECONDS,
                             now - BREAKER_PROBE_HOLD_SECONDS))
                        won += cur.rowcount
                    conn.commit()
                    answer = ("probe", 0.0) if won else ("latched", 30.0)
        finally:
            conn.close()
    except Exception:
        return "go", 0.0
    with _breaker_lock:
        if len(_auth_memo) > 8192:
            _auth_memo.clear()
        _auth_memo[key] = (now_m, answer)
    return answer


def _auth_forget(provider, bucket):
    with _breaker_lock:
        for k in [k for k in _auth_memo if k[0] == provider and k[1] == bucket]:
            _auth_memo.pop(k, None)
        _wave_memo.pop(provider, None)


def auth_first_soft(provider, bucket, store=None, restaurant_id=None, db_path=None) -> bool:
    """A 403 on a token that answered within the hour may be a gateway or a
    rate limit wearing the wrong code: the FIRST such 403 (no soft mark in
    the last hour) is marked and answered True — the caller treats it as
    try-later; a second is a refusal."""
    now = time.time()
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT soft, first_at FROM pos_auth_latch WHERE provider=? AND bucket=? AND store=?",
                               (provider, bucket, str(store or ""))).fetchone()
            if row is not None and (not int(row["soft"] or 0) or now - float(row["first_at"]) < 3600):
                return False
            conn.execute("INSERT INTO pos_auth_latch (provider, bucket, store, restaurant_id, status, soft, first_at, "
                         "failed_at, probe_at, updated_at) VALUES (?,?,?,?,?,1,?,?,NULL,?) "
                         "ON CONFLICT(provider, bucket, store) DO UPDATE SET soft=1, first_at=excluded.first_at, "
                         "failed_at=excluded.failed_at, status=excluded.status, updated_at=excluded.updated_at",
                         (provider, bucket, str(store or ""), int(restaurant_id or 0), 403, now, now, now))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        return False
    return True


def auth_latch(provider, bucket, store=None, restaurant_id=None, status=None, db_path=None) -> dict:
    """Latch a refusal. {new: the store (or token) was not latched before,
    token: the token itself is now latched, wave: auth_wave(...)}."""
    now = time.time()
    store = str(store or "")
    out = {"new": False, "token": not store, "wave": None}
    try:
        conn = get_conn(db_path)
        try:
            prev = conn.execute("SELECT soft, first_at FROM pos_auth_latch WHERE provider=? AND bucket=? AND store=?",
                                (provider, bucket, store)).fetchone()
            out["new"] = prev is None or bool(int(prev["soft"] or 0))
            first = now if out["new"] else float(prev["first_at"])
            sql = ("INSERT INTO pos_auth_latch (provider, bucket, store, restaurant_id, status, soft, first_at, "
                   "failed_at, probe_at, updated_at) VALUES (?,?,?,?,?,0,?,?,NULL,?) "
                   "ON CONFLICT(provider, bucket, store) DO UPDATE SET soft=0, first_at=excluded.first_at, "
                   "failed_at=excluded.failed_at, probe_at=NULL, status=excluded.status, "
                   "restaurant_id=excluded.restaurant_id, updated_at=excluded.updated_at")
            conn.execute(sql, (provider, bucket, store, int(restaurant_id or 0), status, first, now, now))
            if store:
                n = conn.execute("SELECT COUNT(*) FROM pos_auth_latch WHERE provider=? AND bucket=? AND store<>'' "
                                 "AND soft=0 AND failed_at>=?", (provider, bucket, now - AUTH_WAVE_WINDOW_SECONDS)
                                 ).fetchone()[0]
                if int(n or 0) >= AUTH_WAVE_STORES:
                    tok = conn.execute("SELECT soft, first_at FROM pos_auth_latch WHERE provider=? AND bucket=? "
                                       "AND store=''", (provider, bucket)).fetchone()
                    conn.execute(sql, (provider, bucket, "", 0, status,
                                       float(tok["first_at"]) if tok is not None and not int(tok["soft"] or 0)
                                       else now, now, now))
                    out["token"] = True
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("pos auth latch not kept for %s: %s", provider, e)
        return out
    _auth_forget(provider, bucket)
    out["wave"] = auth_wave(provider, db_path=db_path, fresh=True)
    return out


def auth_clear(provider, bucket, store=None, db_path=None) -> int:
    """An answer on a latched (or soft-marked) store or token: its rows go —
    the store's and the token's (store None: every row of the token).
    Returns the rows removed."""
    try:
        conn = get_conn(db_path)
        try:
            if store is None:
                cur = conn.execute("DELETE FROM pos_auth_latch WHERE provider=? AND bucket=?", (provider, bucket))
            else:
                cur = conn.execute("DELETE FROM pos_auth_latch WHERE provider=? AND bucket=? AND store IN (?, '')",
                                   (provider, bucket, str(store or "")))
            conn.commit()
            n = cur.rowcount or 0
        finally:
            conn.close()
    except Exception:
        return 0
    _auth_forget(provider, bucket)
    return n


def auth_wave(provider, db_path=None, fresh=False) -> dict:
    """{active, stores, tokens}: stores and tokens newly refused (first
    refusal) within the last hour. Active from AUTH_WAVE_STORES stores or
    AUTH_WAVE_TOKENS tokens — RPOWER's auth, or a partner token, not one
    store's problem."""
    now_m = time.monotonic()
    with _breaker_lock:
        hit = _wave_memo.get(provider)
    if hit and not fresh and now_m - hit[0] < 60:
        return hit[1]
    out = {"active": False, "stores": 0, "tokens": 0}
    try:
        conn = get_conn(db_path)
        try:
            since = time.time() - AUTH_WAVE_WINDOW_SECONDS
            row = conn.execute("SELECT COUNT(DISTINCT restaurant_id) AS s, COUNT(DISTINCT bucket) AS b "
                               "FROM pos_auth_latch WHERE provider=? AND soft=0 AND first_at>=? AND store<>''",
                               (provider, since)).fetchone()
            tok = conn.execute("SELECT COUNT(DISTINCT bucket) FROM pos_auth_latch WHERE provider=? AND soft=0 "
                               "AND first_at>=?", (provider, since)).fetchone()
        finally:
            conn.close()
        out["stores"] = int(row["s"] or 0)
        out["tokens"] = int(tok[0] or 0)
        out["active"] = out["stores"] >= AUTH_WAVE_STORES or out["tokens"] >= AUTH_WAVE_TOKENS
    except Exception:
        pass
    with _breaker_lock:
        _wave_memo[provider] = (now_m, out)
    return out


def auth_wave_active(provider, db_path=None) -> bool:
    """Whether a wave of refusals is on (owners' per-store notices hold)."""
    return bool(auth_wave(provider, db_path=db_path).get("active"))


def _page_auth_wave(provider, db_path=None) -> bool:
    wave = auth_wave(provider, db_path=db_path, fresh=True)
    if not wave["active"]:
        return False
    import scheduler
    if not scheduler.scheduling_allowed():
        return False
    import ops
    res = ops.page_operator(f"pos_auth_wave:{provider}", f"Cavnar AI: {provider.upper()} is refusing tokens",
                            [f"{wave['stores']} store(s) and {wave['tokens']} token(s) newly refused in the last hour "
                             "— RPOWER's auth or a partner token, not one store's problem.",
                             "Their requests are held (one re-probe every "
                             f"{int(AUTH_LATCH_SECONDS // 60)} minutes); owners' \"hasn't synced\" notices are held "
                             "while it lasts."], cooldown_minutes=360)
    return bool(res.get("sent"))


def recent_ok(provider, restaurant_id, seconds=3600, db_path=None) -> bool:
    """Whether the store's requests were answered within `seconds`: this
    process's unflushed counts, then the ledger."""
    rid = int(restaurant_id or 0)
    cutoff = datetime.fromtimestamp(time.time() - seconds, timezone.utc).strftime("%Y-%m-%d %H:00:00")
    with _ledger_lock:
        for (hour, prov, r, _ep), agg in _ledger.items():
            if prov == provider and r == rid and hour >= cutoff and agg.get("ok"):
                return True
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT SUM(ok) FROM pos_request_ledger WHERE provider=? AND restaurant_id=? "
                               "AND hour>=?", (provider, rid, cutoff)).fetchone()
        finally:
            conn.close()
        return bool(row and int(row[0] or 0) > 0)
    except Exception:
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
    """Keep a list read afresh. A list that has not changed since it was kept
    has only its `fetched_at` moved (blind re-audit 10/20/26 R3-11): the
    whole JSON — 0.5 MB and more for a group's menu or employees — was
    rewritten on every refresh, all of it WAL on an archived Postgres."""
    try:
        payload = json.dumps(rows, default=str)
        now = time.time()
        conn = get_conn(db_path)
        try:
            same = conn.execute("UPDATE pos_catalogs SET fetched_at=? WHERE provider=? AND scope=? AND path=? "
                                "AND payload=?", (now, provider, str(scope), path, payload)).rowcount
            if not same:
                conn.execute("INSERT INTO pos_catalogs (provider, scope, path, payload, rows_n, fetched_at) "
                             "VALUES (?,?,?,?,?,?) ON CONFLICT(provider, scope, path) DO UPDATE SET "
                             "payload=excluded.payload, rows_n=excluded.rows_n, fetched_at=excluded.fetched_at",
                             (provider, str(scope), path, payload, len(rows or []), now))
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
