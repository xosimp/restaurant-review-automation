"""
admin_ops.py — the data layer behind the admin console.

Everything the console shows is computed here, once, from tables that already
exist (restaurants, users, ai_usage, email_log, push_deliveries, device_tokens,
job_failures, job_runs, activity_log, login_history, alert_log, webhooks…) so
every page — Overview, Clients, Integrations, Billing, Jobs — reads the same
health record for a location and can never disagree about whether it is fine.

The model is OWNER (a users row) → BRAND (restaurants.location_group, or the
single restaurant itself) → LOCATION (a restaurants row). Health only ever
rolls UP: a brand is as healthy as its worst location.

What every figure here promises (fix round C, 9/29/26):

* TIME. Every stamp is parsed in the zone its column is written in
  (_parse_utc): SQLite's space form is UTC, a naive 'T' stamp is Chicago wall
  time (restaurants.created_at / last_fetched_at / last_activity,
  users.last_login, reviews.fetched_at, activity_log), an offset is itself.
  "Today" is midnight Central and every payload names its windows
  (WINDOW_LABELS). Two stamps are compared as instants, never as strings.
* FAILURE. A query that fails is recorded (_collecting) and the payload
  carries `errors`, so a locked database no longer reads as "nothing needs
  you". A table another part of the platform has not created yet is listed
  under `unavailable`, and the figure says what it fell back to.
* ACCOUNTS. One account filter (_segment): a demo, test
  (exclude_from_learning), internal-billing or admin-home account is
  'internal' — shown, but never counted in attention, critical or any
  business figure. Every issue carries its segment; counts come per segment.
* COST. The fleet build is memoised for a short TTL on request threads,
  single-flight, and dropped by every admin write; a request that would
  queue behind a build past the waiter limit gets 503 + Retry-After
  (AdminBusy) instead of holding one of gunicorn's four threads.
* RESOLVE. A resolution covers the occurrence it saw (occurrence_at); a newer
  occurrence reopens the issue and says so; a condition that clears retires
  its resolution; every step is written to admin_issue_resolution_history.
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import models as _models_mod
from models import get_restaurant

log = logging.getLogger(__name__)

# A database a caller names for the whole of one call (snapshot_business_metrics
# on a scratch copy), read by get_conn() below. Thread-local: a scheduler
# thread's override is never seen by a request thread.
_db_override = threading.local()


def get_conn(db_path=None):
    """models.get_conn, resolved at call time — CLAUDE.md's bound-import
    hazard. `from models import get_conn` bound the function object at
    import, so a patch of models.get_conn never reached this module's bare
    get_conn() calls and they opened ./reviews.db."""
    if db_path is None:
        db_path = getattr(_db_override, "path", None)
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()


def _current_db_path():
    """The database get_conn() opens right now: the caller's override, else
    models.DB_PATH at call time — for readers that take a path, not a
    connection (status_manager's heartbeat)."""
    return getattr(_db_override, "path", None) or _models_mod.DB_PATH


@contextmanager
def _using_db(db_path):
    prev = getattr(_db_override, "path", None)
    _db_override.path = db_path
    try:
        yield
    finally:
        _db_override.path = prev


# MRR by module count. This used to be its own {1: 349, 2: 649, ...} literal,
# which is how the 2- and 3-module prices ended up living in three places and
# disagreeing in one of them: this table and pricing.html both said $649/$899
# while pricing.py — the one that actually bills — said $698/$1,047. Read it
# from pricing.py so there is nothing left to drift.
from pricing import TIERS as _TIERS
MONTHLY_BY_MODULES = {n: t["monthly"] for n, t in _TIERS.items()}
ANNUAL_BY_MODULES = {n: t["annual"] for n, t in _TIERS.items()}

INTEGRATIONS = ("google_business", "toast", "square", "clover", "rpower", "instagram", "webhook")

# A paying subscription (MRR, churn risk, the inactivity alarm). 'trial' is
# every new row's default and means "never paid"; 'internal' is ours.
PAYING_STATES = ("active", "past_due")
ENDED_STATES = ("churned", "canceled", "cancelled")
# Who counts as the owner using the product (#49): the primary login, a
# multi-location owner, and a manager — never an admin, a support login, a
# legacy teammate or a staff PIN identity.
OWNER_ROLES = ("owner", "client", "manager")
_OWNER_ROLE_SQL = "LOWER(COALESCE(NULLIF(TRIM(u.role),''),'client')) IN ('owner','client','manager')"
_GRANTABLE_MODULES = ("reviews", "labor", "inventory", "marketing")


# ── time helpers ─────────────────────────────────────────────────────────────
#
# Four stamp conventions live in this database, and which zone an offset-less
# stamp means depends on the column that holds it (#89, #129):
#   "2026-09-06 23:17:24"        SQLite datetime('now'), utc_stamp — UTC
#   "2026-09-06T18:17:24"        Chicago wall time: restaurants.created_at,
#                                last_fetched_at, last_activity, users.last_login,
#                                reviews.fetched_at, activity_log (log_activity)
#   "2026-09-06T23:17:24+00:00"  an instant, whatever the zone
#   "2026-09-06"                 a date — midnight in the column's zone
# Production runs on UTC, so reading a Chicago 'T' stamp as server-local made
# every one of them 5-6 hours too old — a false critical "not fetched in 12h"
# every night. The default zone for a naive 'T' stamp is therefore Chicago;
# a column written as naive UTC with a T (rpower_last_synced) names "UTC".

OPERATOR_TZ = "America/Chicago"
WINDOW_LABELS = {"today": "since midnight Central", "day": "last 24 hours", "week": "last 7 days",
                 "month": "last 30 days"}
_ZFMT = "%Y-%m-%dT%H:%M:%SZ"


def _utcnow():
    return datetime.now(timezone.utc)


def _parse_utc(ts, zone=OPERATOR_TZ):
    """A stored stamp as an AWARE UTC datetime, through the one parser
    (time_utils.parse_stamp); `zone` is what a naive 'T' stamp or a bare
    date means for this column. None when missing or unreadable."""
    if ts is None or ts == "":
        return None
    from time_utils import parse_stamp
    try:
        return parse_stamp(ts, naive_tz=zone)
    except Exception:
        return None


def _parse(ts, zone=OPERATOR_TZ):
    """_parse_utc as a NAIVE UTC datetime, for arithmetic between two stamps
    of the same column."""
    d = _parse_utc(ts, zone)
    return None if d is None else d.replace(tzinfo=None)


def _age_hours(ts, zone=OPERATOR_TZ):
    d = _parse_utc(ts, zone)
    return None if d is None else max(0.0, (_utcnow() - d).total_seconds() / 3600)


def _age_days(ts, zone=OPERATOR_TZ):
    h = _age_hours(ts, zone)
    return None if h is None else h / 24


def _since(ts, zone=OPERATOR_TZ):
    """'3h', '2d', '5w' — how long ago, for the attention list."""
    h = _age_hours(ts, zone)
    if h is None:
        return "—"
    if h < 1:
        return f"{int(h * 60)}m"
    if h < 48:
        return f"{int(h)}h"
    d = h / 24
    if d < 14:
        return f"{int(d)}d"
    return f"{int(d / 7)}w"


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _utc_stamp(dt=None):
    """`dt` (aware, or naive UTC) as the UTC space form the ledgers store."""
    dt = dt or _utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _iso_z(ts, zone=OPERATOR_TZ):
    """A stored stamp as 'YYYY-MM-DDTHH:MM:SSZ' — the one form the console
    parses without guessing a zone. None when unreadable."""
    d = _parse_utc(ts, zone)
    return None if d is None else d.strftime(_ZFMT)


def _stamp_any(v):
    """A Stripe unix time (int or digit string) or any stored stamp, as Z."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
            return datetime.fromtimestamp(int(float(v)), tz=timezone.utc).strftime(_ZFMT)
    except (TypeError, ValueError, OSError):
        return None
    return _iso_z(v, "UTC")


def _newest(*stamps, zone="UTC"):
    """The newest of several stamps of one zone, as the raw stamp, or None."""
    best, best_raw = None, None
    for s in stamps:
        d = _parse_utc(s, zone)
        if d is not None and (best is None or d > best):
            best, best_raw = d, s
    return best_raw


def _ct(dt):
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo(OPERATOR_TZ))


def _mdy(ts, zone="UTC"):
    """M/D/YY of a stamp's CHICAGO day (DESIGN_SYSTEM → Dates)."""
    from time_utils import mdy
    d = _parse_utc(ts, zone)
    return mdy(_ct(d).date()) if d else ""


def _today_start():
    """Midnight today in the operator's zone — the start of every 'today'."""
    n = _now_ct()
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


def _windows():
    """The console's windows, each bounded in UTC for the UTC ledgers and in
    Chicago wall time for the Chicago columns."""
    now = _utcnow()
    today = _today_start()
    return {"now": now, "today": _utc_stamp(today), "today_ct": today.strftime("%Y-%m-%dT%H:%M:%S"),
            "day": _utc_stamp(now - timedelta(days=1)), "week": _utc_stamp(now - timedelta(days=7)),
            "prev_week": _utc_stamp(now - timedelta(days=14)), "month": _utc_stamp(now - timedelta(days=30)),
            "month_start_ct": today.replace(day=1).strftime("%Y-%m-%dT%H:%M:%S"),
            "labels": dict(WINDOW_LABELS), "tz": OPERATOR_TZ}


def _window_meta():
    return {"tz": OPERATOR_TZ, "labels": dict(WINDOW_LABELS)}


# ── query failures (#47) ────────────────────────────────────────────────────
#
# _rows_dict and _one_dict used to swallow every exception into [] / None, so
# a locked database or a schema drift read as zero failures, zero alerts and
# "Nothing needs you". They still never raise — one broken panel must not
# blank the page — but every failure is recorded in the collection scope of
# the payload being built (`errors`), captured to job_failures at most once
# per query per ten minutes, and a table that simply does not exist yet (one
# another part of the platform adds) is `unavailable`, not an error.

_err_local = threading.local()
_capture_last = {}
_CAPTURE_EVERY_S = 600
_FROM_RE = re.compile(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)


@contextmanager
def _collecting():
    stack = getattr(_err_local, "stack", None)
    if stack is None:
        stack = _err_local.stack = []
    bucket = {"errors": [], "unavailable": []}
    stack.append(bucket)
    try:
        yield bucket
    finally:
        # By identity: two buckets with the same contents are equal, and
        # list.remove() would take the outer one and leave this one behind.
        for i in range(len(stack) - 1, -1, -1):
            if stack[i] is bucket:
                del stack[i]
                break


def _current_errors():
    stack = getattr(_err_local, "stack", None) or []
    return [e for b in stack for e in b["errors"]]


def _missing_schema(exc):
    s = str(exc).lower()
    return "no such table" in s or "no such column" in s


def _label_of(sql):
    m = _FROM_RE.search(sql or "")
    return m.group(1) if m else "query"


def _note_failure(label, exc, optional=False):
    unavailable = bool(optional) and _missing_schema(exc)
    entry = {"query": label, "error": str(exc)[:200]}
    for bucket in list(getattr(_err_local, "stack", None) or []):
        (bucket["unavailable"] if unavailable else bucket["errors"]).append(entry)
    if unavailable:
        return
    log.warning("admin_ops query %s failed: %s", label, exc)
    now = time.monotonic()
    last = _capture_last.get(label)
    if last is not None and now - last < _CAPTURE_EVERY_S:
        return
    _capture_last[label] = now
    try:
        import ops
        try:
            # kind='request' (job_failures.kind, workstream D): a console
            # query failing is not a failed background job.
            ops.capture(exc, job="admin_console", context=f"query={label}", kind="request")
        except TypeError:
            ops.capture(exc, job="admin_console", context=f"query={label}")
    except Exception:
        pass


def _rows_dict(conn, sql, args=(), label=None, optional=False):
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception as e:
        _note_failure(label or _label_of(sql), e, optional)
        return []


def _one_dict(conn, sql, args=(), label=None, optional=False):
    try:
        r = conn.execute(sql, args).fetchone()
        return dict(r) if r else None
    except Exception as e:
        _note_failure(label or _label_of(sql), e, optional)
        return None


def _rows_strict(conn, sql, args=()):
    """For a caller that tries column sets in turn: raises, so the next set
    is tried (the fallback loops never ran while _rows_dict swallowed)."""
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _columns(conn, table):
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _merge_problems(*buckets):
    """{errors, query_errors, unavailable} from several scopes, each entry
    once. `errors` and `query_errors` are the same list: `query_errors` is
    the name on every payload, `errors` too wherever that key was free (the
    client page's `errors` has always been the client's own failures)."""
    out = {"errors": [], "unavailable": []}
    for kind in out:
        seen = set()
        for b in buckets:
            for e in (b or {}).get(kind) or []:
                k = (e.get("query"), e.get("error"))
                if k not in seen:
                    seen.add(k)
                    out[kind].append(e)
    out["query_errors"] = out["errors"]
    return out


# ── boot-time schema (never on a request path) ──────────────────────────────

_BOOT_SQL = (
    # One row per operator (Chicago) day, never pruned (#18): the figures a
    # trend needs, written by snapshot_business_metrics. History only accrues
    # from the day it is first scheduled. state_json keeps the day's paying /
    # trial / past-due ids so the next day's activations and churns are a
    # difference of two snapshots, not a back-cast from today's accounts.
    """CREATE TABLE IF NOT EXISTS business_metrics_daily (
        date                  TEXT PRIMARY KEY,
        generated_at          TEXT NOT NULL,
        list_mrr              REAL,
        billed_mrr            REAL,
        committed_mrr         REAL,
        mrr                   REAL,
        mrr_source            TEXT,
        subscriptions         INTEGER,
        paying_accounts       INTEGER,
        trial_accounts        INTEGER,
        past_due_accounts     INTEGER,
        paused_accounts       INTEGER,
        canceled_accounts     INTEGER,
        internal_accounts     INTEGER,
        signups               INTEGER,
        activations           INTEGER,
        churns                INTEGER,
        active_accounts_7d    INTEGER,
        active_users_1d       INTEGER,
        active_users_7d       INTEGER,
        ai_cost_usd           REAL,
        cost_by_vendor_json   TEXT,
        vendor_costs_month_json TEXT,
        state_json            TEXT
    )""",
    # Monthly costs the ledgers cannot see (#90): Railway, Resend, Twilio
    # numbers, Stripe fees, DocuSign — entered by the operator, one row per
    # vendor per month, audited in admin_events.
    """CREATE TABLE IF NOT EXISTS vendor_costs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        vendor      TEXT NOT NULL,
        month       TEXT NOT NULL,
        amount_usd  REAL NOT NULL,
        kind        TEXT NOT NULL DEFAULT 'fixed',
        source      TEXT NOT NULL DEFAULT 'manual',
        note        TEXT,
        updated_by  TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(vendor, month)
    )""",
    # Every resolve, reopen and automatic clear (#24) — admin_issue_resolutions
    # holds only the resolutions in force.
    """CREATE TABLE IF NOT EXISTS admin_issue_resolution_history (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        key           TEXT NOT NULL,
        action        TEXT NOT NULL,
        occurrence_at TEXT,
        note          TEXT,
        actor         TEXT,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_issue_res_history_key ON admin_issue_resolution_history(key, id)",
    # The nightly retention delete (ops._RETENTION_DAYS, about 400 days).
    "CREATE INDEX IF NOT EXISTS idx_issue_res_history_created ON admin_issue_resolution_history(created_at)",
    # Each paying account's churn-risk level and since when (#83), written by
    # the nightly snapshot, so "at risk for 7+ days" is a fact, not a guess.
    """CREATE TABLE IF NOT EXISTS account_risk_state (
        restaurant_id INTEGER PRIMARY KEY,
        level         TEXT NOT NULL,
        since         TEXT NOT NULL,
        points        INTEGER,
        reasons_json  TEXT,
        updated_at    TEXT NOT NULL
    )""",
    # The four value figures per restaurant per day (#57), written by
    # intelligence.dashboard.snapshot_value_figures (a bounded, resumable
    # sweep) so the admin Intelligence page sums stored rows instead of
    # running every restaurant's labor and inventory analysis per request.
    """CREATE TABLE IF NOT EXISTS value_figures_daily (
        restaurant_id        INTEGER NOT NULL,
        date                 TEXT    NOT NULL,
        delivered_monthly    REAL,
        avoided_to_date      REAL,
        surfaced_30d         REAL,
        opportunity_monthly  REAL,
        computed_at          TEXT    NOT NULL,
        PRIMARY KEY (restaurant_id, date)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_value_figures_daily_date ON value_figures_daily(date)",
)


def init_admin_ops(db_path=None):
    """The console's own tables and columns, at boot (models.init_db calls
    this). admin_issue_resolutions gains occurrence_at: the occurrence a
    resolution covered, so a newer one reopens the issue (#24)."""
    conn = get_conn(db_path)
    try:
        for sql in _BOOT_SQL:
            conn.execute(sql)
        cols = _columns(conn, "admin_issue_resolutions")
        if cols and "occurrence_at" not in cols:
            conn.execute("ALTER TABLE admin_issue_resolutions ADD COLUMN occurrence_at TEXT")
        conn.commit()
    finally:
        conn.close()


# ── the fleet build: memoised, single-flight, bounded (#32, #36) ────────────
#
# Every fleet page rebuilt every restaurant per request, and the first
# Overview load ran three builds at once (19.6 s at 1,000 restaurants). On a
# request thread the build is now shared: a memo of FLEET_TTL_SECONDS,
# dropped by every admin write (invalidate_fleet_cache, wired as an
# after-request hook on /admin writes), built by ONE thread while at most
# FLEET_MAX_WAITERS others wait for it; anything past that is refused with
# AdminBusy (503 + Retry-After) rather than queueing on gunicorn's four
# threads. Off a request thread (the scheduler, scripts, tests) every call
# builds fresh — the same rule as models.get_restaurant's request memo.

FLEET_TTL_SECONDS = float(os.getenv("ADMIN_FLEET_TTL_SECONDS", "45"))
FLEET_WAIT_SECONDS = float(os.getenv("ADMIN_FLEET_WAIT_SECONDS", "30"))
FLEET_MAX_WAITERS = int(os.getenv("ADMIN_FLEET_MAX_WAITERS", "1"))
HEAVY_CONCURRENCY = int(os.getenv("ADMIN_HEAVY_CONCURRENCY", "1"))
HEAVY_WAIT_SECONDS = float(os.getenv("ADMIN_HEAVY_WAIT_SECONDS", "2"))
BUSY_RETRY_AFTER = int(os.getenv("ADMIN_BUSY_RETRY_AFTER", "5"))
# The rail badges accept a memo up to this old rather than start a build.
BADGES_MAX_AGE_SECONDS = float(os.getenv("ADMIN_BADGES_MAX_AGE_SECONDS", "300"))
# ?fresh=1 accepts a memo no older than this.
FRESH_MAX_AGE_SECONDS = 5.0


class AdminBusy(Exception):
    """Refused because the console is already computing: the route answers
    503 with Retry-After (admin_routes registers the handler)."""

    def __init__(self, what="the console", retry_after=None):
        self.retry_after = int(retry_after or BUSY_RETRY_AFTER)
        self.what = what
        super().__init__(f"The admin console is busy building {what}. Try again in {self.retry_after} seconds.")


_fleet_cv = threading.Condition()
_fleet_state = {"memo": None, "building": False, "waiters": 0, "gen": 0}
_heavy_slots = threading.BoundedSemaphore(max(1, HEAVY_CONCURRENCY))


def _in_request():
    try:
        from flask import has_request_context
        return has_request_context()
    except Exception:
        return False


def _memo_key():
    """Which database a memo belongs to: a test that points get_conn at its
    own file must never be served the previous test's fleet."""
    return (id(globals().get("get_conn")), id(getattr(_models_mod, "get_conn", None)),
            str(getattr(_models_mod, "DB_PATH", "")), getattr(_db_override, "path", None))


def invalidate_fleet_cache():
    """Drop the memo — every admin write calls this (directly, or through the
    after-request hook on /admin writes)."""
    with _fleet_cv:
        _fleet_state["gen"] += 1
        _fleet_state["memo"] = None


def _fleet_memo(max_age=None):
    m = _fleet_state["memo"]
    if not m or m["key"] != _memo_key() or m["gen"] != _fleet_state["gen"]:
        return None
    age = time.monotonic() - m["at"]
    ttl = FLEET_TTL_SECONDS if not m["meta"]["errors"] else min(FLEET_TTL_SECONDS, 10.0)
    return m if age <= (ttl if max_age is None else max_age) else None


def _served(m):
    return m["recs"], m["d"], dict(m["meta"], cached=True, age_seconds=round(time.monotonic() - m["at"], 1))


def _records_cached():
    """(recs, d, meta) for a payload. meta = {generated_at, errors,
    unavailable, cached, age_seconds}."""
    if not _in_request() or FLEET_TTL_SECONDS <= 0:
        with _collecting() as bucket:
            recs, d = _records()
        return recs, d, _build_meta(bucket)
    deadline = time.monotonic() + FLEET_WAIT_SECONDS
    # ?fresh=1 (the console's Refresh) accepts a memo only seconds old —
    # still single-flight, so a burst of refreshes shares one build.
    try:
        from flask import request as _rq
        fresh = _rq.args.get("fresh") == "1"
    except Exception:
        fresh = False
    with _fleet_cv:
        while True:
            m = _fleet_memo(max_age=FRESH_MAX_AGE_SECONDS if fresh else None)
            if m:
                return _served(m)
            if not _fleet_state["building"]:
                _fleet_state["building"] = True
                gen = _fleet_state["gen"]
                break
            remaining = deadline - time.monotonic()
            if _fleet_state["waiters"] >= FLEET_MAX_WAITERS or remaining <= 0:
                raise AdminBusy("the fleet view")
            _fleet_state["waiters"] += 1
            try:
                _fleet_cv.wait(remaining)
            finally:
                _fleet_state["waiters"] -= 1
    try:
        with _collecting() as bucket:
            recs, d = _records()
        meta = _build_meta(bucket)
    except BaseException:
        with _fleet_cv:
            _fleet_state["building"] = False
            _fleet_cv.notify_all()
        raise
    with _fleet_cv:
        _fleet_state["building"] = False
        if gen == _fleet_state["gen"]:
            _fleet_state["memo"] = {"recs": recs, "d": d, "meta": meta, "at": time.monotonic(), "gen": gen,
                                    "key": _memo_key()}
        _fleet_cv.notify_all()
    return recs, d, meta


def _build_meta(bucket):
    return {"generated_at": _utcnow().strftime(_ZFMT), "errors": list(bucket["errors"]),
            "unavailable": list(bucket["unavailable"]), "cached": False, "age_seconds": 0.0}


@contextmanager
def heavy_slot(what):
    """At most HEAVY_CONCURRENCY heavy, non-fleet computations at once on
    request threads (recommendation analytics, calibration, Intelligence);
    past a short wait the request is refused with AdminBusy."""
    if not _in_request():
        yield
        return
    if not _heavy_slots.acquire(timeout=HEAVY_WAIT_SECONDS):
        raise AdminBusy(what)
    try:
        yield
    finally:
        _heavy_slots.release()


def _payload_meta(meta, *buckets):
    """What every fleet payload carries about itself."""
    probs = _merge_problems(meta, *buckets)
    return {"generated_at": meta.get("generated_at"), "cached": bool(meta.get("cached")),
            "age_seconds": meta.get("age_seconds") or 0.0, "errors": probs["errors"],
            "query_errors": probs["errors"], "unavailable": probs["unavailable"], "windows": _window_meta()}


# ── raw loads ────────────────────────────────────────────────────────────────

def _is_admin_home(users):
    return bool(users) and all(u.get("is_admin") for u in users)


def _segment(r, users):
    """'internal' for a demo, a test account (exclude_from_learning — "out of
    every cross-restaurant figure"), internal billing or the admin's own home
    restaurant; 'customer' otherwise (#141)."""
    if (r.get("is_demo") or _is_admin_home(users) or (r.get("billing_status") or "").lower() == "internal"
            or r.get("exclude_from_learning")):
        return "internal"
    return "customer"


def _load_everything():
    """One connection, one pass: every per-restaurant signal the pages need.
    Grouped queries only — no per-restaurant query anywhere in the build —
    and every scan of a growing ledger is windowed (SCALE-10)."""
    conn = get_conn()
    try:
        return _load_with(conn)
    finally:
        conn.close()


def _load_with(conn):
    w = _windows()
    ct_now = _now_ct()
    rests = _rows_dict(conn, "SELECT * FROM restaurants ORDER BY id", label="restaurants")
    users = _rows_dict(conn, "SELECT id, restaurant_id, username, email, role, is_admin, is_active, created_at, "
                             "last_login FROM users", label="users")
    by_rid = {}
    for u in users:
        by_rid.setdefault(u["restaurant_id"], []).append(u)

    def per_rid(sql, args=(), key="restaurant_id", label=None, optional=False):
        out = {}
        for r in _rows_dict(conn, sql, args, label=label, optional=optional):
            out[r[key]] = r
        return out

    max_ai = int(getattr(_models_mod, "MAX_AI_ATTEMPTS", 5) or 5)
    # reviews.fetched_at is Chicago wall time with a T: the 48-hour cut is
    # written on the same clock and julianday() reads both as one naive clock.
    urgent_cut = (ct_now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    _open = "COALESCE(response_status,'pending') NOT IN ('approved','posted','skipped')"
    _urgent = f"urgency='high' AND {_open} AND julianday(fetched_at) < julianday(?)"
    _stalled = ("((processed=0 AND COALESCE(analysis_attempts,0) >= ?) OR "
                "(processed=1 AND response_status='pending' AND COALESCE(draft_attempts,0) >= ?))")
    # `unanswered` is what "Reviews awaiting" means — drafted or not (#160,
    # FIGURES-25); draft_ready and no_draft split it. The stalled counts are
    # the reviews the AI gave up on after MAX_AI_ATTEMPTS (#124).
    reviews = per_rid(f"""SELECT restaurant_id, COUNT(*) AS total,
            SUM(CASE WHEN {_open} THEN 1 ELSE 0 END) AS unanswered,
            SUM(CASE WHEN response_status='drafted' THEN 1 ELSE 0 END) AS draft_ready,
            SUM(CASE WHEN COALESCE(response_status,'pending') NOT IN ('drafted','approved','posted','skipped')
                     THEN 1 ELSE 0 END) AS no_draft,
            SUM(CASE WHEN response_status IN ('approved','posted') THEN 1 ELSE 0 END) AS responded,
            SUM(CASE WHEN {_urgent} THEN 1 ELSE 0 END) AS urgent_stale,
            MAX(CASE WHEN {_urgent} THEN fetched_at END) AS urgent_stale_last,
            SUM(CASE WHEN processed=0 AND COALESCE(analysis_attempts,0) >= ? THEN 1 ELSE 0 END) AS stalled_analysis,
            SUM(CASE WHEN processed=1 AND response_status='pending' AND COALESCE(draft_attempts,0) >= ?
                     THEN 1 ELSE 0 END) AS stalled_drafts,
            MAX(CASE WHEN {_stalled} THEN fetched_at END) AS stalled_last,
            MAX(fetched_at) AS last_review_at
        FROM reviews WHERE deleted_at IS NULL GROUP BY restaurant_id""",
                      (urgent_cut, urgent_cut, max_ai, max_ai, max_ai, max_ai), label="reviews")

    # ai_usage is created at boot (ai_utils.init_ai_ops, fix round G); the
    # console never creates it (SCALE-19), so a database without it reads
    # `unavailable`. A call refused before it reached the provider (outcome
    # 'blocked': a budget, a breaker, the readiness gate) is not a call — it
    # counts in none of these aggregates (#48).
    ai_cols = _columns(conn, "ai_usage")
    llm_only = " AND COALESCE(vendor,'anthropic')='anthropic'" if "vendor" in ai_cols else ""
    sent_only = f" AND {_OUTCOME_SQL} <> 'blocked'" if "outcome" in ai_cols else ""
    owner_sum = (', SUM(CASE WHEN "trigger"=\'owner\' THEN 1 ELSE 0 END) AS owner_calls'
                 if "trigger" in ai_cols else "")
    ai_month = per_rid(f"""SELECT restaurant_id, COUNT(*) AS calls, ROUND(SUM(cost_usd),4) AS cost,
                                 SUM(input_tokens)+SUM(output_tokens) AS tokens, MAX(created_at) AS last_at,
                                 SUM(CASE WHEN COALESCE(status,'ok')='error' THEN 1 ELSE 0 END) AS failed
                          FROM ai_usage WHERE created_at >= ?{sent_only} GROUP BY restaurant_id""", (w["month"],),
                       label="ai_usage", optional=True)
    ai_failed_week = {}
    for row in _rows_dict(conn, "SELECT restaurant_id, created_at, error FROM ai_usage WHERE created_at >= ? "
                                f"AND COALESCE(status,'ok')='error'{llm_only}{sent_only} ORDER BY id", (w["week"],),
                          label="ai_usage", optional=True):
        f = ai_failed_week.setdefault(row["restaurant_id"], {"n": 0, "last_at": None, "sample": None})
        f["n"] += 1
        f["last_at"] = row["created_at"]
        f["sample"] = row["error"]              # the LATEST error, not MAX(error)
    ai_today = per_rid("SELECT restaurant_id, COUNT(*) AS calls, ROUND(SUM(cost_usd),4) AS cost FROM ai_usage "
                       f"WHERE created_at >= ?{sent_only} GROUP BY restaurant_id", (w["today"],), label="ai_usage",
                       optional=True)
    ai_prev = per_rid(f"SELECT restaurant_id, COUNT(*) AS calls{owner_sum} FROM ai_usage WHERE created_at >= ? "
                      f"AND created_at < ?{sent_only} GROUP BY restaurant_id", (w["prev_week"], w["week"]),
                      label="ai_usage", optional=True)
    ai_week = per_rid(f"SELECT restaurant_id, COUNT(*) AS calls{owner_sum} FROM ai_usage WHERE created_at >= ?"
                      f"{sent_only} GROUP BY restaurant_id", (w["week"],), label="ai_usage", optional=True)
    # Who is near or past one of their own ceilings (G's budget_watch, #122)
    # and the anomalies of the last two days (#140), each one grouped read —
    # the client issues read them instead of the old "$25 in 30 days" rule.
    try:
        budget_rows = budget_watch(conn) if "vendor" in ai_cols else []
    except Exception as e:
        budget_rows = []
        _note_failure("ai_budget_watch", e)
    try:
        anomaly_rows = ai_anomalies(days=2, conn=conn) if "outcome" in ai_cols else []
    except Exception as e:
        anomaly_rows = []
        _note_failure("ai_anomalies", e)
    # email_log.sent_at is UTC (workstream E's migration). Bounced and
    # complained are failures here too — a bounce is not a delivery (#59).
    emails = per_rid("""SELECT restaurant_id,
                              SUM(CASE WHEN sent_at >= ? THEN 1 ELSE 0 END) AS sent_7d,
                              SUM(CASE WHEN sent_at >= ? AND status='failed' THEN 1 ELSE 0 END) AS failed_7d,
                              SUM(CASE WHEN sent_at >= ? AND status='bounced' THEN 1 ELSE 0 END) AS bounced_7d,
                              SUM(CASE WHEN sent_at >= ? AND status='complained' THEN 1 ELSE 0 END) AS complained_7d,
                              MAX(CASE WHEN status IN ('failed','bounced','complained') THEN sent_at END)
                                  AS last_failed_at,
                              MAX(sent_at) AS last_sent_at
                       FROM email_log WHERE sent_at >= ? GROUP BY restaurant_id""",
                     (w["week"], w["week"], w["week"], w["week"], w["month"]), label="email_log")
    pushes = per_rid("""SELECT restaurant_id,
                              SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS sent_7d,
                              SUM(CASE WHEN created_at >= ? AND ok=0 THEN 1 ELSE 0 END) AS failed_7d,
                              MAX(CASE WHEN ok=0 THEN created_at END) AS last_failed_at
                       FROM push_deliveries WHERE created_at >= ? GROUP BY restaurant_id""",
                     (w["week"], w["week"], w["month"]), label="push_deliveries")
    tokens = per_rid("""SELECT restaurant_id, COUNT(*) AS devices,
                              SUM(CASE WHEN disabled_reason IS NOT NULL AND disabled_reason != '' THEN 1 ELSE 0 END) AS disabled
                       FROM device_tokens GROUP BY restaurant_id""", label="device_tokens")
    alerts = per_rid("SELECT restaurant_id, COUNT(*) AS fired_7d, MAX(fired_at) AS last_at FROM alert_log "
                     "WHERE fired_at >= ? GROUP BY restaurant_id", (w["week"],), label="alert_log")
    webhooks = per_rid("SELECT restaurant_id, url, is_active, consecutive_failures, last_status, last_fired_at, "
                       "disabled_reason FROM webhooks", label="webhooks")
    client_data = per_rid("SELECT restaurant_id, shifts_csv IS NOT NULL AND shifts_csv != '' AS has_shifts, "
                          "inventory_csv IS NOT NULL AND inventory_csv != '' AS has_inventory, updated_at "
                          "FROM client_data", label="client_data")
    # last_count_at: the newest physical count (the column ordering reads).
    # restaurants.inventory_updated_at is never written by anything, so
    # inventory read "fresh" forever off it (CA3 F9).
    ingredients = per_rid("SELECT restaurant_id, COUNT(*) AS n, MAX(updated_at) AS cost_updated_at, "
                          "MAX(last_recount_at) AS last_count_at "
                          "FROM ingredients WHERE COALESCE(is_active,1)=1 GROUP BY restaurant_id", label="ingredients")
    recipes = per_rid("""SELECT mi.restaurant_id AS restaurant_id, COUNT(DISTINCT mi.id) AS items,
                                COUNT(DISTINCT ri.menu_item_id) AS mapped
                         FROM menu_items mi LEFT JOIN recipe_ingredients ri ON ri.menu_item_id=mi.id
                         GROUP BY mi.restaurant_id""", label="menu_items")
    labor_days = per_rid("SELECT restaurant_id, COUNT(DISTINCT date) AS n, MAX(date) AS last FROM labor_daily_history "
                         "WHERE date >= ? AND sales > 0 GROUP BY restaurant_id",
                         ((ct_now.date() - timedelta(days=30)).isoformat(),), label="labor_daily_history")
    # The last day the labor data COVERS (a day with sales), not when a file
    # was last written — a sync that keeps landing shifts with no sales is
    # not current data (CA3 F2/F3).
    labor_last = per_rid("SELECT restaurant_id, MAX(date) AS last FROM labor_daily_history "
                         "WHERE sales > 0 GROUP BY restaurant_id", label="labor_daily_history")
    contacts = per_rid("SELECT restaurant_id, COUNT(*) AS n, SUM(COALESCE(sms_consent,0)) AS consented "
                       "FROM alert_contacts GROUP BY restaurant_id", label="alert_contacts")
    routing = per_rid("SELECT restaurant_id, COUNT(*) AS n FROM issue_routing GROUP BY restaurant_id",
                      label="issue_routing")
    stale_issues = per_rid("SELECT restaurant_id, COUNT(*) AS n FROM ops_issues WHERE status='open' "
                           "AND julianday(created_at) <= julianday('now','-24 hours') GROUP BY restaurant_id",
                           label="ops_issues")
    marketing = per_rid("SELECT restaurant_id, COUNT(*) AS pieces, MAX(created_at) AS last_at "
                        "FROM marketing_content_log GROUP BY restaurant_id", label="marketing_content_log")
    # Posts are stored 'scheduled' (marketing_publish), never 'pending': the
    # old filter counted none of them (#160).
    mp_cols = _columns(conn, "marketing_scheduled_posts")
    failed_at = "COALESCE(claimed_at, created_at)" if "claimed_at" in mp_cols else "created_at"
    sched_posts = per_rid(f"""SELECT restaurant_id,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN status IN ('scheduled','pending','publishing') THEN 1 ELSE 0 END) AS pending,
            MAX(CASE WHEN status='failed' THEN {failed_at} END) AS last_failed_at
        FROM marketing_scheduled_posts GROUP BY restaurant_id""", label="marketing_scheduled_posts")
    schedules = per_rid("SELECT restaurant_id, MAX(generated_at) AS last_at, COUNT(*) AS n FROM schedule_history "
                        "GROUP BY restaurant_id", label="schedule_history")
    # Owner activity (#49): an owner or manager console sign-in (never a
    # staff PIN, a PIN failure or an admin's view-as), and the newest request
    # any of their sessions made — web or iOS. julianday() orders the mixed
    # stamp formats as instants.
    owner_logins = per_rid(f"""SELECT l.restaurant_id AS restaurant_id,
                                      datetime(MAX(julianday(l.created_at))) AS last_at, COUNT(*) AS n
                               FROM login_history l JOIN users u ON u.id = l.user_id
                               WHERE l.event = 'login'
                                 AND COALESCE(l.device_type,'') NOT IN ('staff_pin','admin-view-as')
                                 AND COALESCE(u.is_admin,0) = 0 AND {_OWNER_ROLE_SQL}
                               GROUP BY l.restaurant_id""", label="login_history")
    team_seen = per_rid("SELECT restaurant_id, datetime(MAX(julianday(created_at))) AS last_at FROM login_history "
                        "WHERE event='staff_login' GROUP BY restaurant_id", label="login_history")
    s_cols = _columns(conn, "sessions")
    active_col = "s.active_restaurant_id" if "active_restaurant_id" in s_cols else "NULL"
    owner_sessions = {}
    for row in _rows_dict(conn, f"""SELECT u.restaurant_id AS home, {active_col} AS active, s.device_type AS device,
                                           datetime(julianday(s.last_active)) AS last_active
                                    FROM sessions s JOIN users u ON u.id = s.user_id
                                    WHERE COALESCE(u.is_admin,0) = 0 AND {_OWNER_ROLE_SQL}
                                      AND COALESCE(s.device_type,'') NOT IN ('staff_pin','admin-view-as')""",
                          label="sessions"):
        for rid in {row["home"], row["active"]} - {None}:
            cur = owner_sessions.setdefault(rid, {"last_active": None, "ios": False, "n": 0})
            cur["n"] += 1
            cur["ios"] = cur["ios"] or (row["device"] == "ios")
            if row["last_active"] and (cur["last_active"] is None or row["last_active"] > cur["last_active"]):
                cur["last_active"] = row["last_active"]
    sessions = per_rid("""SELECT u.restaurant_id AS restaurant_id, COUNT(*) AS n FROM sessions s JOIN users u ON u.id=s.user_id
                          WHERE julianday(s.expires_at) > julianday('now') GROUP BY u.restaurant_id""",
                       label="sessions")
    guests = per_rid("SELECT restaurant_id, COUNT(*) AS n FROM guest_contacts WHERE consent=1 AND unsubscribed=0 "
                     "GROUP BY restaurant_id", label="guest_contacts")
    # The Data Health rollup (DH5-13): the newest daily snapshot per
    # restaurant (data_health_daily, written by scheduler.run_data_health_daily)
    # and the live sync ledger (source_health) — read, never recomputed per
    # restaurant on every page load. Only the last 14 days are scanned; a
    # snapshot older than DH_SNAPSHOT_MAX_AGE_DAYS is reported, not trusted.
    data_health_daily = per_rid("""SELECT d.restaurant_id, d.date, d.overall, d.sources_json FROM data_health_daily d
        JOIN (SELECT restaurant_id, MAX(date) AS date FROM data_health_daily WHERE date >= ? GROUP BY restaurant_id) m
          ON m.restaurant_id = d.restaurant_id AND m.date = d.date""",
                                ((ct_now.date() - timedelta(days=14)).isoformat(),), label="data_health_daily")
    source_health = {}
    for row in _rows_dict(conn, "SELECT restaurant_id, source, provider, last_attempt_at, last_ok_at, first_failed_at, "
                                "last_error, error_class, consecutive_failures, next_retry_at, data_through "
                                "FROM source_health", label="source_health"):
        source_health.setdefault(row["restaurant_id"], []).append(row)
    # job_failures.kind / restaurant_id arrive with workstream D; without
    # them a failure's kind is read from its job name and text.
    jf_cols = _columns(conn, "job_failures")
    jf_extra = "".join(f", {c}" for c in ("kind", "restaurant_id") if c in jf_cols)
    job_failures = _rows_dict(conn, f"SELECT id, job, error, context, created_at{jf_extra} FROM job_failures "
                                    "WHERE created_at >= ? ORDER BY id DESC", (w["week"],), label="job_failures")
    res_cols = _columns(conn, "admin_issue_resolutions")
    occ_col = ", occurrence_at" if "occurrence_at" in res_cols else ""
    resolved = {r["key"]: r for r in _rows_dict(conn, f"SELECT key, resolved_at, note, actor{occ_col} "
                                                      "FROM admin_issue_resolutions",
                                                label="admin_issue_resolutions")}
    # The Stripe subscription mirror (workstream H): one row per paying
    # restaurant. Absent columns or table → list price, and the payload says so.
    mirror = {}
    mirror_cols = _columns(conn, "stripe_subscriptions")
    for row in _rows_dict(conn, "SELECT * FROM stripe_subscriptions", label="stripe_subscriptions", optional=True):
        mirror[row["restaurant_id"]] = row
    # When each billing state began: billing_status_history (workstream H)
    # when it exists, else the Stripe and DocuSign events already kept.
    status_changes = {}
    for row in _rows_dict(conn, "SELECT restaurant_id, new_value, MAX(created_at) AS at FROM billing_status_history "
                                "WHERE field='billing_status' GROUP BY restaurant_id, new_value",
                          label="billing_status_history", optional=True):
        status_changes.setdefault(row["restaurant_id"], {})[row["new_value"]] = row["at"]
    ev_types = ("invoice.payment_failed", "customer.subscription.deleted", "charge.dispute.created",
                "charge.refunded", "checkout.session.completed", "invoice.paid")
    events = {}
    for row in _rows_dict(conn, "SELECT restaurant_id, event_type, MAX(created_at) AS at FROM admin_events "
                                f"WHERE restaurant_id IS NOT NULL AND (event_type IN ({','.join('?' * len(ev_types))}) "
                                "OR event_type LIKE 'contract.signed%') AND created_at >= ? "
                                "GROUP BY restaurant_id, event_type",
                          (*ev_types, _utc_stamp(w["now"] - timedelta(days=400))), label="admin_events"):
        events.setdefault(row["restaurant_id"], {})[row["event_type"]] = row["at"]
    # Suppressed owner and login addresses (#45) — scope is 'all' (NULL) or a
    # narrower list ('guest', 'marketing').
    sup_cols = _columns(conn, "email_suppressions")
    scope_col = ", scope" if "scope" in sup_cols else ""
    suppressed = {}
    for row in _rows_dict(conn, f"""SELECT email, reason, detail, created_at{scope_col} FROM email_suppressions
                                    WHERE email IN (SELECT LOWER(TRIM(owner_email)) FROM restaurants
                                                    WHERE COALESCE(owner_email,'') != ''
                                                    UNION SELECT LOWER(TRIM(email)) FROM users
                                                    WHERE COALESCE(email,'') != '')""",
                          label="email_suppressions"):
        suppressed[(row["email"] or "").strip().lower()] = row
    risk_state = per_rid("SELECT restaurant_id, level, since, updated_at FROM account_risk_state",
                         label="account_risk_state", optional=True)
    sms_cost = per_rid("SELECT restaurant_id, ROUND(SUM(COALESCE(cost_usd,0)),4) AS cost, COUNT(*) AS n "
                       "FROM sms_log WHERE created_at >= ? GROUP BY restaurant_id", (w["month"],),
                       label="sms_log", optional=True)
    # Workstream H's billing ledgers, one grouped read each: owed billing mail
    # that gave up (#12), the nightly reconcile's findings (#115), each open
    # invoice's failure and retries (#6, #25), the current contract's fate
    # (#144) and the pay-link chase (#26).
    owed_failed = per_rid("""SELECT o.restaurant_id, COUNT(*) AS n, MIN(o.updated_at) AS first_at,
                                    MAX(o.updated_at) AS last_at,
                                    (SELECT o2.kind || ': ' || COALESCE(o2.last_error, 'not delivered') FROM owed_sends o2
                                     WHERE o2.restaurant_id = o.restaurant_id AND o2.status = 'failed'
                                     ORDER BY o2.id DESC LIMIT 1) AS sample
                             FROM owed_sends o WHERE o.status = 'failed' AND o.restaurant_id IS NOT NULL
                             GROUP BY o.restaurant_id""", label="owed_sends", optional=True)
    reconcile = per_rid("SELECT restaurant_id, checked_at, local_status, stripe_status, mismatches, first_seen_at "
                        "FROM billing_reconcile WHERE mismatches IS NOT NULL", label="billing_reconcile", optional=True)
    open_invoices = per_rid("SELECT restaurant_id, MAX(last_failed_at) AS last_failed_at, MAX(attempt_count) AS attempts, "
                            "SUM(COALESCE(amount_remaining_cents, amount_due_cents, 0)) AS remaining_cents, "
                            "MAX(next_payment_attempt) AS next_attempt FROM stripe_invoices "
                            "WHERE status = 'open' AND restaurant_id IS NOT NULL GROUP BY restaurant_id",
                            label="stripe_invoices", optional=True)
    envelope_fate = per_rid("SELECT restaurant_id, envelope_id, status, status_at, status_reason FROM docusign_envelopes "
                            "WHERE status IN ('declined', 'voided') ORDER BY COALESCE(status_at, sent_at)",
                            label="docusign_envelopes", optional=True)
    pay_reminders = per_rid("SELECT restaurant_id, GROUP_CONCAT(dedupe_key || '=' || status) AS sent FROM owed_sends "
                            "WHERE kind = 'pay_reminder' GROUP BY restaurant_id", label="owed_sends", optional=True)
    # The automatic alert-storm cap in force (fix round E, #92): one per
    # restaurant per local day, until its next local midnight (UTC stamp),
    # unless an admin lifted it. The newest wins.
    storm_caps = per_rid("SELECT restaurant_id, local_day, started_at, until_at, alerts_in_window, threshold, "
                         "suppressed FROM alert_storm_caps WHERE lifted_at IS NULL "
                         "AND julianday(until_at) > julianday('now') ORDER BY id",
                         label="alert_storm_caps", optional=True)

    d = dict(now=w["now"], windows=w, rests=rests, users=by_rid, reviews=reviews, ai_month=ai_month,
             ai_today=ai_today, ai_prev=ai_prev, ai_week=ai_week, ai_failed_week=ai_failed_week,
             ai_owner_trigger=bool(owner_sum), emails=emails, pushes=pushes, tokens=tokens, alerts=alerts,
             webhooks=webhooks, client_data=client_data, ingredients=ingredients, marketing=marketing,
             recipes=recipes, labor_days=labor_days, labor_last=labor_last, contacts=contacts, routing=routing,
             stale_issues=stale_issues, sched_posts=sched_posts, schedules=schedules,
             owner_logins=owner_logins, owner_sessions=owner_sessions, team_seen=team_seen,
             sessions=sessions, guests=guests, job_failures=job_failures,
             job_failures_has_kind="kind" in jf_cols, job_failures_has_rid="restaurant_id" in jf_cols,
             resolved=resolved, data_health_daily=data_health_daily, source_health=source_health,
             mirror=mirror, mirror_available=bool(mirror_cols), mirror_cols=mirror_cols,
             status_changes=status_changes, events=events, suppressed=suppressed, risk_state=risk_state,
             sms_cost=sms_cost, storm_caps=storm_caps, budget_watch=budget_rows, ai_anomalies=anomaly_rows,
             owed_failed=owed_failed, reconcile=reconcile, open_invoices=open_invoices, envelope_fate=envelope_fate,
             pay_reminders=pay_reminders,
             rest_names={r["id"]: r.get("name") for r in rests},
             has_converted_at=bool(rests) and "converted_at" in rests[0],
             pos_states={}, loaded_at=_utc_stamp(w["now"]))
    d["billing_groups"] = _billing_groups(rests, mirror)
    # Churn risk's "nothing measured in 90+ days" signal, for every eligible
    # account in one query (#43) — it used to open two connections per
    # restaurant (outcomes.total_value), ~96% of the build.
    eligible = [r["id"] for r in rests
                if (r.get("billing_status") or "trial") in PAYING_STATES
                and _segment(r, by_rid.get(r["id"], [])) == "customer"
                and (_age_days(r.get("created_at")) or 0) >= 90]
    d["outcome_counts"] = _outcome_counts(conn, eligible)
    return d


def _outcome_counts(conn, rids):
    """{rid: {"wins", "in_flight"}} for many restaurants from ONE query —
    outcomes.total_value's selection (list_outcomes' newest 500 evaluated and
    tracking trackers, untaken comparisons left out, informational rows
    dropped, the distinct/sales rules) without its two connections per
    restaurant (#43). tests/test_fix_c_* holds it equal to total_value."""
    if not rids:
        return {}
    import outcomes
    by = {}
    rids = sorted(set(rids))
    for n in range(0, len(rids), 400):
        chunk = rids[n:n + 400]
        marks = ",".join("?" for _ in chunk)
        for row in _rows_dict(conn, f"SELECT * FROM recommendation_outcomes WHERE restaurant_id IN ({marks}) "
                                    "AND status IN ('evaluated','tracking') AND source_key NOT LIKE ? "
                                    "ORDER BY id DESC", (*chunk, outcomes.UNTAKEN_PREFIX + "%"),
                              label="recommendation_outcomes", optional=True):
            by.setdefault((row["restaurant_id"], row["status"]), []).append(row)
    out = {}
    for rid in rids:
        try:
            ev = [x for x in (outcomes._row(r) for r in by.get((rid, "evaluated"), [])[:500])
                  if not x.get("informational")]
            tr = [x for x in (outcomes._row(r) for r in by.get((rid, "tracking"), [])[:500])
                  if not x.get("informational")]
            kept = outcomes._drop_sales_artifacts(outcomes.distinct(
                [x for x in ev if x.get("verdict") in outcomes._MOVED and x.get("counts")]))
            wins = [x for x in kept if outcomes.metrics.family(x.get("metric")) != "sales"
                    and x.get("verdict") == "improved" and x.get("dollars_monthly")]
            out[rid] = {"wins": len(wins), "in_flight": len(tr)}
        except Exception as e:
            _note_failure("recommendation_outcomes", e)
    return out


# ── billing groups and MRR (#15, #116) ──────────────────────────────────────
#
# Owner decision 1: ONE subscription per multi-location group. The location
# that pays carries the group's MRR; its siblings are "billed by" it and add
# nothing. MRR comes from the Stripe subscription mirror (workstream H):
# amount ÷ interval, once per subscription, after discount; a trialing
# subscription is COMMITTED, not billed. An account with no mirror row falls
# back to its list price — once per billing group — and the payload says so.

_STRIPE_BILLED = ("active", "past_due")
_STRIPE_COMMITTED = ("trialing",)
_STRIPE_ENDED = ("canceled", "incomplete_expired", "unpaid", "ended")


def _billing_roots(rests):
    """{rid: group root}: union of the three ways locations share a bill —
    organization_id, location_group + owner email (webhook_routes.
    _sibling_restaurant_ids' rule) and Stripe customer id."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first = {}
    for r in rests:
        rid = r["id"]
        find(rid)
        keys = []
        if r.get("organization_id"):
            keys.append(("org", r["organization_id"]))
        grp = (r.get("location_group") or "").strip().lower()
        if grp:
            keys.append(("grp", grp, (r.get("owner_email") or "").strip().lower()))
        cus = (r.get("stripe_customer_id") or "").strip()
        if cus:
            keys.append(("cus", cus))
        for k in keys:
            if k in first:
                a, b = find(first[k]), find(rid)
                if a != b:
                    parent[max(a, b)] = min(a, b)
            else:
                first[k] = rid
    return {rid: find(rid) for rid in list(parent)}


def _billing_groups(rests, mirror):
    """{rid: {"group": [ids], "payers": [ids], "billed_by": rid|None}}."""
    roots = _billing_roots(rests)
    members = {}
    for r in rests:
        members.setdefault(roots.get(r["id"], r["id"]), []).append(r)
    out = {}
    for group in members.values():
        group.sort(key=lambda x: x["id"])
        payers, seen_subs = [], set()
        for r in group:
            sub = mirror.get(r["id"]) or {}
            st = (sub.get("status") or "").lower()
            sid = sub.get("subscription_id")
            if sid and st in _STRIPE_BILLED + _STRIPE_COMMITTED and sid not in seen_subs:
                seen_subs.add(sid)
                payers.append(r)
        cus_paid = {(p.get("stripe_customer_id") or "").strip() for p in payers} - {""}
        for r in group:
            if r in payers or mirror.get(r["id"]) or (r.get("billing_status") or "trial") not in PAYING_STATES:
                continue
            cus = (r.get("stripe_customer_id") or "").strip()
            if cus and cus not in cus_paid:
                # Its own Stripe customer, not mirrored yet: its own list price.
                payers.append(r)
                cus_paid.add(cus)
        if not payers:
            lead = next((r for r in group if (r.get("billing_status") or "trial") in PAYING_STATES), None)
            if lead:
                payers.append(lead)
        payer_ids = [p["id"] for p in payers]
        ids = [x["id"] for x in group]
        for r in group:
            info = {"group": ids, "payers": payer_ids, "billed_by": None}
            if (len(group) > 1 and payer_ids and r["id"] not in payer_ids
                    and (r.get("billing_status") or "trial").lower() not in ENDED_STATES):
                cus = (r.get("stripe_customer_id") or "").strip()
                match = next((p for p in payers if cus and (p.get("stripe_customer_id") or "").strip() == cus), None)
                info["billed_by"] = (match or payers[0])["id"]
            out[r["id"]] = info
    return out


def _json_or_none(v):
    """A stored JSON value, parsed; None for empty or unreadable."""
    if not v:
        return None
    try:
        return json.loads(v) if isinstance(v, str) else v
    except (TypeError, ValueError):
        return None


def _sub_monthly(sub):
    """(monthly before discount, monthly after discount) of a mirrored
    subscription — amount × quantity ÷ its interval — or (None, None)."""
    amt = sub.get("amount_cents")
    if amt is None:
        return None, None
    try:
        qty = int(sub.get("quantity") or 1)
        n = int(sub.get("interval_count") or 1) or 1
        per_month = {"month": 1.0 / n, "year": 1.0 / (12 * n), "week": 52.0 / 12 / n,
                     "day": 365.0 / 12 / n}.get((sub.get("interval") or "month").lower())
        if per_month is None:
            return None, None
        gross = float(amt) / 100.0 * qty * per_month
        net = gross * (1 - float(sub.get("discount_pct") or 0) / 100.0)
        if sub.get("discount_amount_cents"):
            net = max(0.0, net - float(sub["discount_amount_cents"]) / 100.0 * per_month)
        return round(gross, 2), round(net, 2)
    except (TypeError, ValueError):
        return None, None


def _list_for(mod_count, interval="month"):
    """The list price per month for a module count on a billing interval —
    an annual plan lists at annual ÷ 12, not the monthly price."""
    n = min(max(int(mod_count or 0), 0), 4)
    if not n:
        return 0.0
    if (interval or "month").lower() == "year":
        return round(ANNUAL_BY_MODULES[n] / 12.0, 2)
    return float(MONTHLY_BY_MODULES[n])


def _billing_for(r, d, mod_count):
    """The location's billing record: status, the MRR it contributes and why,
    the mirrored subscription, who pays for it, pause reason, trial and
    contract ages, deletion request."""
    rid = r["id"]
    bs = (r.get("billing_status") or "trial").lower()
    sub = (d.get("mirror") or {}).get(rid)
    grp = (d.get("billing_groups") or {}).get(rid) or {}
    ev = (d.get("events") or {}).get(rid, {})
    hist = (d.get("status_changes") or {}).get(rid, {})
    paying = bs in PAYING_STATES
    out = {"status": bs, "tier": r.get("service_tier"), "stripe_customer_id": r.get("stripe_customer_id"),
           "contract_status": r.get("contract_status") or "pending", "envelope_id": r.get("docusign_envelope_id"),
           "modules": mod_count, "monthly": 0, "list_monthly": 0, "billed_monthly": None, "committed_monthly": 0,
           "mrr_source": None, "billed_by": None, "covers": [], "subscription": None, "list_mismatch": None,
           "status_mismatch": None, "pause_reason": None, "pause_reason_inferred": False,
           "paused_until": r.get("paused_until"),
           "converted_at": _iso_z(r.get("converted_at"), "UTC") if r.get("converted_at") else None}
    list_monthly = _list_for(mod_count) if paying else 0.0
    if grp.get("billed_by"):
        payer = grp["billed_by"]
        out.update(mrr_source="covered", billed_by={"restaurant_id": payer,
                                                    "name": (d.get("rest_names") or {}).get(payer)})
    elif sub and sub.get("subscription_id"):
        st = (sub.get("status") or "").lower()
        gross, net = _sub_monthly(sub)
        interval = (sub.get("interval") or "month").lower()
        out["subscription"] = {
            "id": sub.get("subscription_id"), "status": st or None, "interval": interval,
            "interval_count": sub.get("interval_count") or 1,
            "amount": (float(sub["amount_cents"]) / 100.0 if sub.get("amount_cents") is not None else None),
            "currency": sub.get("currency"), "quantity": sub.get("quantity"),
            "discount_pct": sub.get("discount_pct"), "monthly_gross": gross, "monthly_net": net,
            "trial_end": _stamp_any(sub.get("trial_end")),
            "current_period_end": _stamp_any(sub.get("current_period_end")),
            "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
            "canceled_at": _stamp_any(sub.get("canceled_at")), "ended_at": _stamp_any(sub.get("ended_at")),
            "cancellation_reason": sub.get("cancellation_reason"),
            "module_keys": sub.get("module_keys"), "module_mismatch": _json_or_none(sub.get("module_mismatch")),
            "updated_at": _stamp_any(sub.get("updated_at"))}
        out["list_monthly"] = _list_for(mod_count, interval) if st in _STRIPE_BILLED + _STRIPE_COMMITTED else 0
        if bs == "paused" and st in _STRIPE_BILLED:
            # Stripe keeps a subscription 'active' while collection is
            # paused; nothing is invoiced, so it is not MRR while it lasts.
            out.update(billed_monthly=0, paused_monthly=net, mrr_source="mirror", list_monthly=0)
        elif st in _STRIPE_BILLED and net is not None:
            out.update(monthly=net, billed_monthly=net, mrr_source="mirror")
        elif st in _STRIPE_COMMITTED and net is not None:
            out.update(committed_monthly=net, mrr_source="mirror")
        elif st in _STRIPE_BILLED + _STRIPE_COMMITTED:
            # A mirror row with no amount yet: the list price, labelled.
            out.update(monthly=out["list_monthly"] if st in _STRIPE_BILLED else 0,
                       committed_monthly=out["list_monthly"] if st in _STRIPE_COMMITTED else 0,
                       mrr_source="list")
        else:
            out["mrr_source"] = "mirror"
        expected = _list_for(mod_count, interval)
        if gross is not None and expected and abs(gross - expected) > 1.0:
            out["list_mismatch"] = {"billed_gross": gross, "list": expected, "interval": interval,
                                    "difference": round(gross - expected, 2)}
        if paying and st in _STRIPE_ENDED:
            out["status_mismatch"] = {"stripe": st, "local": bs}
        elif bs in ("trial", "pending", "") and st in _STRIPE_BILLED:
            out["status_mismatch"] = {"stripe": st, "local": bs}
    elif paying:
        # No mirror row: the list price, once per billing group (_billing_groups).
        out.update(monthly=list_monthly, list_monthly=list_monthly, mrr_source="list")
    groups = d.get("billing_groups") or {}
    out["covers"] = [m for m in grp.get("group", []) if m != rid and (groups.get(m) or {}).get("billed_by") == rid]
    # The pause reason (#138): workstream H's column, else read off the
    # Stripe events of the pause itself (a dispute or a refund).
    if bs == "paused":
        reason = r.get("pause_reason")
        if not reason:
            began = _parse_utc(hist.get("paused"), "UTC")
            floor = began - timedelta(days=2) if began else _utcnow() - timedelta(days=60)
            for etype, why in (("charge.dispute.created", "dispute"), ("charge.refunded", "refund")):
                at = _parse_utc(ev.get(etype), "UTC")
                if at and at >= floor:
                    reason = why
                    out["pause_reason_inferred"] = True
                    break
        out["pause_reason"] = reason
    # A hold only an admin lifts (H, #114): a dispute, a full refund or an
    # admin's own — also one kept on an account that has since churned.
    try:
        out["hold"] = _models_mod.billing_hold(r)
    except Exception:
        out["hold"] = None
    # Contract and trial ages (#26): when it was signed (H's column, else the
    # DocuSign completion in admin_events), days in trial since signup.
    signed_raw = r.get("contract_signed_at") or _newest(*[v for k, v in ev.items() if k.startswith("contract.signed")])
    out["signed_at"] = _iso_z(signed_raw, "UTC") if signed_raw else None
    out["days_since_signed"] = int(_age_days(signed_raw, "UTC")) if signed_raw else None
    created = _age_days(r.get("created_at"))
    out["days_in_trial"] = int(created) if (bs in ("trial", "pending", "") and created is not None) else None
    return out


# ── the per-location health record ──────────────────────────────────────────

def _pos_integration(r, name, label, configured, auth):
    """One POS provider's row, judged through pos_health.provider_state —
    the same provider-agnostic reading every other surface uses (CA3 F6) —
    so RPOWER is listed and a sync that stopped days ago without writing an
    error is not "connected"."""
    import pos_health
    st = pos_health.provider_state(r, name)
    err = r.get(f"{name}_sync_error") or None
    if not err and configured and st.get("state") == "stale":
        err = f"No successful sync in {int(st.get('age_days') or 0)} days"
    # *_last_synced is an instant with an offset, except rpower_last_synced
    # (naive UTC with a T) — read all four as UTC.
    return {"key": name, "label": label,
            "connected": bool(configured) and st.get("state") in ("current", "aging"),
            "configured": bool(configured),
            "last_success": _iso_z(r.get(f"{name}_last_synced"), "UTC"), "error": err,
            "sync_state": st.get("state"), "age_days": st.get("age_days"),
            "auth": auth}


def review_source(r):
    """(source, label) for how this location's Google reviews arrive.
    Places — a place id with reviews_live and no Business Profile — returns
    at most five reviews a fetch, so it is a SAMPLE of the listing and never
    "Google Business connected" (CA3 F13)."""
    if (r.get("gmb_refresh_token") if isinstance(r, dict) else getattr(r, "gmb_refresh_token", None)):
        return "gbp", "Google Business"
    live = r.get("reviews_live") if isinstance(r, dict) else getattr(r, "reviews_live", 0)
    place = r.get("google_place_id") if isinstance(r, dict) else getattr(r, "google_place_id", None)
    if live and place:
        return "places_sampled", PLACES_SAMPLED_LABEL
    return "none", "Google Business"


PLACES_SAMPLED_LABEL = "Google reviews (sampled — Places returns 5 at a time)"
_POS_LABELS = {"toast": "Toast", "square": "Square", "clover": "Clover", "rpower": "RPOWER"}
# One day of review fetches: a Business Profile that has not answered for
# this many in a row is an error, as scheduler.GBP_FALLBACK_ALERT_SLOTS
# tells the owner.
GBP_FAILING_SLOTS = 4


def _gbp_state(r, d):
    """(error, since, revoked_at) for the Business Profile connection (#44).
    A revoked refresh token is cleared by gmb.get_valid_token, which now
    stamps restaurants.gmb_revoked_at; the location then quietly reads as
    'Google reviews (sampled)'. That — or a Business Profile the fetch keeps
    falling back from (source_health 'gbp') — is an error, not a pass."""
    sh = {s["source"]: s for s in ((d or {}).get("source_health") or {}).get(r["id"], [])}
    gbp = sh.get("gbp") or {}
    fails = int(gbp.get("consecutive_failures") or 0)
    revoked_at = r.get("gmb_revoked_at")
    if not r.get("gmb_refresh_token"):
        if revoked_at:
            return ("Google access was revoked — reviews now arrive five at a time from Places, and replies "
                    "can't post. The owner has to reconnect Google.", revoked_at, revoked_at)
        if fails and (gbp.get("error_class") == "auth"):
            return (f"Google connection lost: {(gbp.get('last_error') or 'revoked')[:120]}",
                    gbp.get("first_failed_at"), gbp.get("last_attempt_at"))
        return None, None, None
    if fails >= GBP_FAILING_SLOTS:
        return (f"Business Profile hasn't answered for {fails} fetches in a row — reviews are coming from "
                f"Places, five at a time: {(gbp.get('last_error') or '')[:100]}", gbp.get("first_failed_at"), None)
    return None, None, None


def _integrations_for(r, hooks, d=None):
    """Every external connection, in one shape: state, last success (Z), the
    error, and when the error began (error_since, UTC) where it is known."""
    ig_exp = _parse_utc(r.get("ig_token_expires"), "UTC")
    ig_days_left = None if not ig_exp else (ig_exp - _utcnow()).days
    source, g_label = review_source(r)
    g_err, g_since, revoked_at = _gbp_state(r, d)
    out = [
        {"key": "google_business", "label": g_label, "source": source,
         "connected": bool(r.get("gmb_refresh_token")),
         "configured": bool(r.get("google_place_id") or r.get("gmb_location_id") or revoked_at),
         "last_success": _iso_z(r.get("last_fetched_at")), "error": g_err,
         "error_since": _iso_z(g_since, "UTC") if g_since else None,
         "revoked_at": _iso_z(revoked_at, "UTC") if revoked_at else None,
         "auth": "oauth" if r.get("gmb_refresh_token") else ("place id only" if r.get("google_place_id") else "none")},
        _pos_integration(r, "toast", "Toast POS", bool(r.get("toast_restaurant_guid")),
                         "credentials" if r.get("toast_client_id") else "none"),
        _pos_integration(r, "square", "Square POS", bool(r.get("square_access_token")),
                         "token" if r.get("square_access_token") else "none"),
        _pos_integration(r, "clover", "Clover POS", bool(r.get("clover_api_token")),
                         "token" if r.get("clover_api_token") else "none"),
        _pos_integration(r, "rpower", "RPOWER POS", bool(r.get("rpower_token") or r.get("rpower_store_mid")),
                         "token" if r.get("rpower_token") else "none"),
        {"key": "instagram", "label": "Instagram & Facebook",
         "connected": bool(r.get("ig_token")) and (ig_days_left is None or ig_days_left >= 0),
         "configured": bool(r.get("ig_token")),
         "last_success": None,
         "error": (f"Token expired {abs(ig_days_left)}d ago" if (r.get("ig_token") and ig_days_left is not None and ig_days_left < 0)
                   else (f"Token expires in {ig_days_left}d" if (r.get("ig_token") and ig_days_left is not None and ig_days_left <= 7) else None)),
         "error_since": ig_exp.strftime(_ZFMT) if (ig_exp and ig_days_left is not None and ig_days_left < 0) else None,
         "auth": "oauth" if r.get("ig_token") else "none"},
    ]
    pos_sh = {s.get("provider"): s for s in ((d or {}).get("source_health") or {}).get(r["id"], [])
              if s.get("source") == "pos"}
    for i in out[1:5]:
        s = pos_sh.get(i["key"]) or {}
        if i["error"] and int(s.get("consecutive_failures") or 0) > 0:
            i["error_since"] = _iso_z(s.get("first_failed_at"), "UTC")
        else:
            i.setdefault("error_since", None)
    hook = hooks.get(r["id"])
    out.append({"key": "webhook", "label": "Outbound webhook",
                "connected": bool(hook and hook.get("is_active")),
                "configured": bool(hook),
                "last_success": _iso_z(hook.get("last_fired_at"), "UTC") if hook else None,
                "error": (hook.get("disabled_reason") or (f"{hook['consecutive_failures']} consecutive failures" if hook.get("consecutive_failures") else None)) if hook else None,
                "error_since": None,
                "auth": "secret" if hook else "none"})
    # Only integrations the location actually uses count toward health.
    for i in out:
        i["in_use"] = i["configured"]
        i["state"] = ("error" if (i["configured"] and i["error"]) else "connected" if i["connected"] else "off")
    return out


# The freshness registry source each admin module row is dated by.
_ADMIN_MODULE_SOURCE = {"reviews": "reviews", "labor": "labor", "inventory": "inventory",
                        "marketing": "marketing", "intel": "competitor"}
# A Data Health snapshot older than this is not a reading of today (#46):
# its module states are not used, and the record says it is stale.
DH_SNAPSHOT_MAX_AGE_DAYS = 2


def _modules_for(r, d):
    rid = r["id"]
    rv = d["reviews"].get(rid, {})
    cd = d["client_data"].get(rid, {})
    ing = d["ingredients"].get(rid, {})
    mk = d["marketing"].get(rid, {})
    sc = d["schedules"].get(rid, {})
    pos_state = _pos_state_for(r, d)
    pos = bool(pos_state.get("connected"))
    labor_last = (d.get("labor_last") or {}).get(rid, {}).get("last")
    mods = [
        {"key": "reviews", "label": "Reviews", "enabled": bool(r.get("module_reviews")),
         "configured": bool(r.get("gmb_refresh_token") or r.get("google_place_id") or r.get("yelp_business_id")),
         "receiving": bool(rv.get("total")), "last_data": rv.get("last_review_at") or r.get("last_fetched_at")},
        # POS credentials alone are not labor data (FIGURES-16): receiving
        # needs a day with sales, a schedule, a shifts file or a sync that landed.
        {"key": "labor", "label": "Labor", "enabled": bool(r.get("module_labor")),
         "configured": pos or bool(cd.get("has_shifts")),
         "receiving": bool(labor_last) or bool(sc.get("n")) or bool(cd.get("has_shifts"))
                      or bool(pos_state.get("last_synced")),
         "last_data": labor_last or (pos_state.get("last_synced") if pos else None) or cd.get("updated_at")},
        {"key": "inventory", "label": "Food Cost", "enabled": bool(r.get("module_inventory")),
         "configured": bool(ing.get("n")) or bool(cd.get("has_inventory")),
         "receiving": bool(ing.get("last_count_at")) or bool(cd.get("has_inventory")),
         "last_data": ing.get("last_count_at") or cd.get("updated_at")},
        {"key": "marketing", "label": "Marketing", "enabled": bool(r.get("module_marketing")),
         "configured": bool(r.get("ig_token") or r.get("gmb_refresh_token") or r.get("voice_notes")),
         "receiving": bool(mk.get("pieces")), "last_data": mk.get("last_at")},
        {"key": "intel", "label": "Intel", "enabled": bool(r.get("module_reviews") and r.get("module_labor") and r.get("module_inventory") and r.get("module_marketing")),
         "configured": bool(r.get("google_place_id")), "receiving": bool(r.get("competitor_intel")), "last_data": r.get("competitor_updated_at")},
    ]
    # "Stale" by the registry's own reading where the daily Data Health
    # snapshot has one for the module's source (the owner's cards read the
    # same rule), else the old age cut. A snapshot that is itself stale is
    # not used (#46).
    dh_states = _dh_source_states(d, rid)
    dh_key = {"reviews": "reviews", "labor": "labor", "inventory": "inventory", "marketing": "marketing",
              "intel": "competitor"}
    for m in mods:
        if not m["enabled"]:
            m["state"] = "off"
        elif not m["configured"]:
            m["state"] = "unconfigured"
        elif not m["receiving"]:
            m["state"] = "no_data"
        elif dh_key.get(m["key"]) in dh_states:
            s = dh_states[dh_key[m["key"]]]
            st = s.get("state")
            # 'pending' (a first sync that never landed) is no data and
            # 'not_connected' is unconfigured — both read healthy before.
            if s.get("error") or st in ("stale", "unknown", "disconnected"):
                m["state"] = "stale"
            elif st == "pending":
                m["state"] = "no_data"
            elif st == "not_connected":
                m["state"] = "unconfigured"
            else:
                m["state"] = "healthy"
        else:
            age = _age_days(m["last_data"])
            # The registry's one stale rule (DH5-3), not a 3/14-day cut of its
            # own: admin and the owner's cards call the same data stale.
            import data_freshness as _df_mods
            m["state"] = "stale" if (age is not None and _df_mods.is_stale(
                _ADMIN_MODULE_SOURCE.get(m["key"], m["key"]), age)) else "healthy"
    return mods


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else round((vals[mid - 1] + vals[mid]) / 2.0, 1)


def _dh_snapshot_age(row):
    try:
        from datetime import date as _date
        return (_now_ct().date() - _date.fromisoformat(str(row.get("date"))[:10])).days
    except (TypeError, ValueError):
        return None


def _dh_source_states(d, rid) -> dict:
    """{source key: {state, pct, error}} from the restaurant's newest
    data_health_daily row — {} when there is none, or when that row is older
    than DH_SNAPSHOT_MAX_AGE_DAYS (a stopped snapshot job must not freeze
    every module at its last reading)."""
    row = (d.get("data_health_daily") or {}).get(rid) or {}
    age = _dh_snapshot_age(row) if row else None
    if not row or age is None or age > DH_SNAPSHOT_MAX_AGE_DAYS:
        return {}
    try:
        return {s["key"]: s for s in json.loads(row.get("sources_json") or "[]") if s.get("key")}
    except (ValueError, TypeError):
        return {}


def _data_health_for(r, d) -> dict:
    """The location's Data Health for the console: the newest daily overall
    % and its date (and whether that snapshot is stale), and every source
    failing in the live ledger right now (consecutive failures, since when,
    the error, the next retry)."""
    rid = r["id"]
    row = (d.get("data_health_daily") or {}).get(rid) or {}
    age = _dh_snapshot_age(row) if row else None
    failing = [{"source": s["source"], "provider": s.get("provider"), "failures": s.get("consecutive_failures"),
                "since": _iso_z(s.get("first_failed_at"), "UTC"), "error": (s.get("last_error") or "")[:160],
                "error_class": s.get("error_class"),
                "next_retry_at": _iso_z(s.get("next_retry_at"), "UTC"), "last_ok_at": _iso_z(s.get("last_ok_at"), "UTC")}
               for s in (d.get("source_health") or {}).get(rid, []) if int(s.get("consecutive_failures") or 0) > 0]
    return {"overall": row.get("overall"), "as_of": row.get("date"), "age_days": age,
            "stale": bool(row) and (age is None or age > DH_SNAPSHOT_MAX_AGE_DAYS), "failing": failing}


def _onboarding_for(r, d, owner):
    rid = r["id"]
    rv = d["reviews"].get(rid, {})
    mods = {m["key"]: m for m in _modules_for(r, d)}
    steps = [
        {"key": "contract", "label": "Contract signed", "done": (r.get("contract_status") or "pending") == "signed"},
        {"key": "login", "label": "First sign-in", "done": bool(owner and owner.get("last_login"))},
        {"key": "reviews", "label": "Google reviews connected", "done": bool(r.get("gmb_refresh_token") or r.get("reviews_live"))},
        {"key": "voice", "label": "Brand voice set", "done": bool(r.get("voice_notes"))},
        {"key": "respond", "label": "First reply approved", "done": (rv.get("responded") or 0) > 0},
    ]
    if r.get("module_labor"):
        steps.append({"key": "labor", "label": "Labor data flowing", "done": mods["labor"]["receiving"]})
    if r.get("module_inventory"):
        steps.append({"key": "inventory", "label": "Inventory loaded", "done": mods["inventory"]["configured"]})
    if r.get("module_marketing"):
        steps.append({"key": "marketing", "label": "First post generated", "done": mods["marketing"]["receiving"]})
    if r.get("billing_status") not in ("internal",):
        steps.append({"key": "billing", "label": "Billing active", "done": r.get("billing_status") == "active"})
    done = sum(1 for s in steps if s["done"])
    # `dismissed` is the OWNER hiding their setup card; it no longer mutes
    # the operator's "Onboarding stuck" issue (#152).
    return {"steps": steps, "done": done, "total": len(steps),
            "complete": done == len(steps), "dismissed": bool(r.get("onboarding_dismissed"))}


# ── data completeness & churn risk ──────────────────────────────────────────
# Every insight the product sells is only as good as the data under it, and a
# client whose data quietly stopped arriving is the client who decides the
# product "doesn't do much". Both reads here are rule-based and show their
# reasons — an admin must be able to say WHY a location scored what it did.

def _data_completeness(r, d):
    """{"score": 0-100 | None, "checks": [{key, label, ok, detail}]} over the
    checks that apply to this location's modules. None when none apply."""
    rid = r["id"]
    checks = []

    def add(key, label, ok, detail):
        checks.append({"key": key, "label": label, "ok": bool(ok), "detail": detail})

    if r.get("module_reviews"):
        _src, _label = review_source(r)
        add("reviews", "Google reviews connected", r.get("gmb_refresh_token") or r.get("reviews_live"),
            "sampled — Places returns 5 reviews a fetch; connect Business Profile to read them all"
            if _src == "places_sampled" else
            ("reviews fetch automatically" if (r.get("gmb_refresh_token") or r.get("reviews_live"))
             else "no live review source"))
    _pos = _pos_state_for(r, d)
    pos_fresh = _pos.get("last_synced")
    if r.get("module_labor") or r.get("module_inventory"):
        add("pos", "POS syncing", pos_fresh and not _pos.get("error") and _pos.get("state") in ("current", "aging"),
            f"{_POS_LABELS.get(_pos.get('provider'), 'POS')} sync error: {_pos.get('error')}" if _pos.get("error")
            else (f"last sync {_since(pos_fresh, 'UTC')}" if pos_fresh else "no POS sync on record"))
    if r.get("module_labor"):
        n = (d["labor_days"].get(rid) or {}).get("n") or 0
        add("labor", "Daily sales & labor, last 30 days", n >= 14, f"{n} of 30 days on file")
    if r.get("module_inventory"):
        rc = d["recipes"].get(rid) or {}
        items, mapped = rc.get("items") or 0, rc.get("mapped") or 0
        add("recipes", "Menu mapped to recipes", items and mapped / items >= 0.6,
            f"{mapped} of {items} menu items have recipes" if items else "no menu items")
        ing = d["ingredients"].get(rid) or {}
        age = _age_days(ing.get("cost_updated_at"))
        add("costs", "Ingredient costs current", ing.get("n") and age is not None and age <= 30,
            (f"{ing.get('n')} ingredients, costs last touched {_since(ing.get('cost_updated_at'))}"
             if ing.get("n") else "no ingredients"))
    ct = d["contacts"].get(rid) or {}
    add("contacts", "Someone consented to alert texts", (ct.get("consented") or 0) > 0,
        f"{ct.get('consented') or 0} of {ct.get('n') or 0} contacts consented")
    add("routing", "Issues routed to a manager", (d["routing"].get(rid) or {}).get("n"),
        "set" if (d["routing"].get(rid) or {}).get("n") else "bad reviews don't reach anyone on the floor")
    add("app", "Owner has the app", (d["tokens"].get(rid) or {}).get("devices"),
        "push-enabled device on file" if (d["tokens"].get(rid) or {}).get("devices") else "no device — briefs go by email")
    # "Setup completeness", not "data completeness" (G14): these are setup
    # checks, and the intelligence layer's feature completeness (the share of
    # features a restaurant can measure) is a different number that had the
    # same name. The label travels with the payload so no screen names it.
    if not checks:
        return {"score": None, "checks": [], "label": SETUP_COMPLETENESS_LABEL}
    return {"score": round(100 * sum(c["ok"] for c in checks) / len(checks)), "checks": checks,
            "label": SETUP_COMPLETENESS_LABEL}


SETUP_COMPLETENESS_LABEL = "Setup completeness"


# A live reviews restaurant should be fetched every four hours. This is
# generous against that: past it, the pass is genuinely not reaching them.
# Kept for the fleet check's wording; the check itself counts missed fetch
# slots on the schedule's own Chicago clock (#129).
FETCH_STALE_HOURS = int(os.getenv("FETCH_STALE_HOURS", "12"))
FLEET_FETCH_MISSED_SLOTS = 2
# "At risk for 7+ days" (#83): a high level held this long raises an issue.
CHURN_ISSUE_DAYS = int(os.getenv("ADMIN_CHURN_ISSUE_DAYS", "7"))


def _churn_risk(r, d, last_active, completeness):
    """{"level": low|medium|high|n/a, "reasons": [...], "scored": bool,
    "since", "days_at_level"}. Signals, not a model: each reason is a fact an
    admin can check and act on. Only a PAYING account is scored (#83) — a
    trial that goes quiet is a sales question — and only what the OWNER
    asked the AI counts as engagement: a scheduled job's calls are not use."""
    users = d["users"].get(r["id"], [])
    bs = (r.get("billing_status") or "trial").lower()
    if _segment(r, users) == "internal" or bs not in PAYING_STATES:
        why = ("internal account" if _segment(r, users) == "internal"
               else ("not paying yet" if bs in ("trial", "pending", "") else f"billing {bs}"))
        return {"level": "n/a", "reasons": [], "scored": False, "why_not": why, "since": None,
                "days_at_level": None}
    rid, reasons, points = r["id"], [], 0
    idle = _age_days(last_active, "UTC")
    joined = _age_days(r.get("created_at"))
    if joined is not None and joined < 14 and (idle is None or idle >= joined):
        pass    # still onboarding: no activity yet is not disengagement
    elif idle is None or idle >= 30:
        reasons.append("no owner activity in 30+ days" if idle is not None else "never active"); points += 3
    elif idle >= 14:
        reasons.append(f"no owner activity in {int(idle)} days"); points += 2
    if d.get("ai_owner_trigger"):
        wk = (d["ai_week"].get(rid) or {}).get("owner_calls") or 0
        prev = (d["ai_prev"].get(rid) or {}).get("owner_calls") or 0
        if prev >= 5 and wk < prev * 0.5:
            reasons.append(f"owner's AI use fell from {prev} to {wk} week over week"); points += 1
    score = (completeness or {}).get("score")
    if score is not None and score < 50:
        reasons.append(f"setup completeness {score}% — insights are thin"); points += 1
    if bs == "past_due":
        reasons.append("billing past due"); points += 2
    if (d["reviews"].get(rid) or {}).get("urgent_stale"):
        reasons.append("urgent reviews unanswered 2+ days"); points += 1
    if (d["stale_issues"].get(rid) or {}).get("n"):
        reasons.append("issues open 24h+ with nobody acknowledging"); points += 1

    # VALUE DELIVERED, not just activity. Every signal above measures whether
    # the owner is USING the product; none measured whether it had been worth
    # anything to them. A fully engaged client who has been shown nothing
    # they can point at scored "low risk" right up to the renewal call, and
    # that is the client who actually leaves. Only counted once the account
    # is old enough to have closed a tracker — a three-week-old restaurant
    # with no measured results is normal, not a warning. Read from the one
    # grouped load (_outcome_counts), never a query per restaurant.
    if joined is not None and joined >= 90:
        v = (d.get("outcome_counts") or {}).get(rid)
        if v is not None:
            if not v["wins"] and not v["in_flight"]:
                reasons.append("nothing measured in %d days — no result to show at renewal"
                               % int(joined)); points += 2
            elif not v["wins"]:
                reasons.append("%d change%s being measured, none has landed yet"
                               % (v["in_flight"], "" if v["in_flight"] == 1 else "s")); points += 1

    level = "high" if points >= 4 else "medium" if points >= 2 else "low"
    state = (d.get("risk_state") or {}).get(rid) or {}
    since = state.get("since") if state.get("level") == level else None
    days = _age_days(since, "UTC")
    return {"level": level, "reasons": reasons, "scored": True, "points": points,
            "since": _iso_z(since, "UTC") if since else None,
            "days_at_level": int(days) if days is not None else None}


def _pos_state_for(r, d=None):
    """pos_health.pos_sync_state, read ONCE per location per page load
    (DH5-13: it was evaluated four times per record)."""
    import pos_health
    cache = (d or {}).get("pos_states")
    if cache is None:
        return pos_health.pos_sync_state(r)
    if r["id"] not in cache:
        cache[r["id"]] = pos_health.pos_sync_state(r)
    return cache[r["id"]]


def _activity_for(r, d, users):
    """When the OWNER last used the product (#49), from four sources read as
    instants: an owner/manager sign-in (users.last_login, Chicago), a console
    sign-in event (login_history, UTC), any request their sessions made —
    web or iOS (sessions.last_active, UTC) — and the web tab ping
    (restaurants.last_activity, Chicago). Staff PIN sign-ins, PIN failures
    and an admin's view-as never count."""
    rid = r["id"]
    owners = [u for u in users if not u.get("is_admin")
              and (u.get("role") or "client").strip().lower() in OWNER_ROLES]
    sign_in = max((x for x in (_parse_utc(u.get("last_login")) for u in owners) if x), default=None)
    sess = (d.get("owner_sessions") or {}).get(rid) or {}
    sources = {"sign_in": sign_in,
               "console_login": _parse_utc(((d.get("owner_logins") or {}).get(rid) or {}).get("last_at"), "UTC"),
               "session": _parse_utc(sess.get("last_active"), "UTC"),
               "web_tab": _parse_utc(r.get("last_activity"))}
    best = max((v for v in sources.values() if v), default=None)
    team = _parse_utc(((d.get("team_seen") or {}).get(rid) or {}).get("last_at"), "UTC")
    return {"last_active": best.strftime(_ZFMT) if best else None,
            "last_login": max((v for v in (sources["sign_in"], sources["console_login"]) if v),
                              default=None),
            "sources": {k: (v.strftime(_ZFMT) if v else None) for k, v in sources.items()},
            "on_ios": bool(sess.get("ios")), "team_last_seen": team.strftime(_ZFMT) if team else None}


def location_record(r, d):
    """The one health record for a location. Everything else is a view of it."""
    rid = r["id"]
    users = d["users"].get(rid, [])
    owner = next((u for u in users if u.get("role") in ("client", "owner") and not u.get("is_admin")), users[0] if users else None)
    rv = d["reviews"].get(rid, {})
    ai = d["ai_month"].get(rid, {})
    em = d["emails"].get(rid, {})
    pu = d["pushes"].get(rid, {})
    tk = d["tokens"].get(rid, {})
    al = d["alerts"].get(rid, {})
    segment = _segment(r, users)
    integrations = _integrations_for(r, d["webhooks"], d)
    modules = _modules_for(r, d)
    onboarding = _onboarding_for(r, d, owner)
    act = _activity_for(r, d, users)
    last_active = act["last_active"]
    last_login = act["last_login"].strftime(_ZFMT) if act["last_login"] else None
    mod_count = sum(1 for m in modules if m["enabled"] and m["key"] != "intel")
    billing = _billing_for(r, d, mod_count)
    sp = d["sched_posts"].get(rid, {})
    completeness = _data_completeness(r, d)
    churn = _churn_risk(r, d, last_active, completeness)
    deletion = _deletion_for(r)
    raw = _issues_for(r, d, owner, integrations, modules, onboarding, last_active, billing=billing, users=users,
                      churn=churn, segment=segment, deletion=deletion)
    d.setdefault("raw_issue_keys", set()).update(i["key"] for i in raw)
    issues, muted = _apply_resolutions(raw, d)
    worst = max([i["severity_rank"] for i in issues] + [0])
    health = {0: "healthy", 1: "warning", 2: "critical"}[worst]
    if (r.get("billing_status") or "").lower() in ENDED_STATES or (owner and not owner.get("is_active")):
        health = "inactive"
    sms = (d.get("sms_cost") or {}).get(rid) or {}

    return {
        "id": rid,
        "name": r.get("name"),
        # Will's own login lives on a restaurant row too — it is never a
        # client, never MRR.
        "is_admin_home": _is_admin_home(users),
        "segment": segment,
        "exclude_from_learning": bool(r.get("exclude_from_learning")),
        "brand": r.get("location_group") or r.get("name"),
        "location_name": r.get("location_name"),
        "city": r.get("neighborhood"),
        "is_demo": bool(r.get("is_demo")),
        "created_at": _iso_z(r.get("created_at")),
        "timezone": r.get("timezone"),
        "owner": ({"id": owner["id"], "username": owner["username"], "email": owner["email"], "role": owner.get("role"),
                   "is_active": bool(owner.get("is_active")), "last_login": _iso_z(owner.get("last_login"))} if owner else None),
        "owner_name": r.get("owner_name"), "owner_email": r.get("owner_email"), "owner_phone": r.get("owner_phone"),
        "logins": [{"id": u["id"], "username": u["username"], "email": u["email"], "role": u.get("role"),
                    "is_active": bool(u.get("is_active")), "last_login": _iso_z(u.get("last_login"))} for u in users],
        "pos_system": r.get("pos_system"),
        "billing": billing,
        "modules": modules,
        "integrations": integrations,
        "integration_health": ("error" if any(i["state"] == "error" for i in integrations)
                               else "ok" if any(i["state"] == "connected" for i in integrations) else "none"),
        "onboarding": onboarding,
        "data_health": _data_health_for(r, d),
        "freshness": {"reviews": _iso_z(r.get("last_fetched_at")), "pos": _pos_state_for(r, d).get("last_synced"),
                      "pos_state": _pos_state_for(r, d),
                      "inventory": (d["ingredients"].get(rid) or {}).get("last_count_at"),
                      "intel": r.get("competitor_updated_at")},
        # `awaiting` is every unanswered review (FIGURES-25); draft_ready and
        # no_draft split it, and `stalled` is what the AI gave up on (#124).
        "reviews": {"total": rv.get("total") or 0, "awaiting": rv.get("unanswered") or 0,
                    "draft_ready": rv.get("draft_ready") or 0, "no_draft": rv.get("no_draft") or 0,
                    "responded": rv.get("responded") or 0, "urgent_stale": rv.get("urgent_stale") or 0,
                    "stalled": (rv.get("stalled_analysis") or 0) + (rv.get("stalled_drafts") or 0),
                    "stalled_analysis": rv.get("stalled_analysis") or 0,
                    "stalled_drafts": rv.get("stalled_drafts") or 0},
        "ai": {"calls_30d": ai.get("calls") or 0, "cost_30d": float(ai.get("cost") or 0), "tokens_30d": ai.get("tokens") or 0,
               "calls_today": (d["ai_today"].get(rid) or {}).get("calls") or 0,
               "calls_7d": (d["ai_week"].get(rid) or {}).get("calls") or 0,
               "calls_prev_7d": (d["ai_prev"].get(rid) or {}).get("calls") or 0,
               "owner_calls_7d": ((d["ai_week"].get(rid) or {}).get("owner_calls") if d.get("ai_owner_trigger") else None),
               "last_at": _iso_z(ai.get("last_at"), "UTC"),
               "failed_30d": ai.get("failed") or 0, "failed_7d": (d["ai_failed_week"].get(rid) or {}).get("n") or 0},
        "email": {"sent_7d": em.get("sent_7d") or 0, "failed_7d": em.get("failed_7d") or 0,
                  "bounced_7d": em.get("bounced_7d") or 0, "complained_7d": em.get("complained_7d") or 0,
                  "last_failed_at": _iso_z(em.get("last_failed_at"), "UTC"),
                  "last_sent_at": _iso_z(em.get("last_sent_at"), "UTC")},
        "push": {"sent_7d": pu.get("sent_7d") or 0, "failed_7d": pu.get("failed_7d") or 0, "devices": tk.get("devices") or 0, "disabled": tk.get("disabled") or 0},
        "sms": {"sent_30d": sms.get("n") or 0, "cost_30d": float(sms.get("cost") or 0)} if sms else None,
        "alerts_7d": al.get("fired_7d") or 0,
        "alert_cap": r.get("alert_max_per_day") or 0,
        "storm_cap": _storm_cap_view((d.get("storm_caps") or {}).get(rid)),
        "scheduled_posts": {"failed": sp.get("failed") or 0, "pending": sp.get("pending") or 0},
        "guests": (d["guests"].get(rid) or {}).get("n") or 0,
        "sessions": (d["sessions"].get(rid) or {}).get("n") or 0,
        "last_login": last_login,
        "last_active": last_active,
        "activity": act["sources"], "on_ios": act["on_ios"], "team_last_seen": act["team_last_seen"],
        "deletion": deletion,
        "internal_notes": r.get("internal_notes"),
        "issues": issues,
        "issues_resolved": muted,
        "health": health,
        # setup_completeness is the name; data_completeness stays as an alias
        # until admin.html reads the new key (group J).
        "setup_completeness": completeness,
        "data_completeness": completeness,
        "churn_risk": churn,
    }


def _storm_cap_view(row):
    """The automatic alert-storm cap on this restaurant now (fix round E,
    #92), or None: {cap, until, reason} plus the ledger's own fields.
    Only health and safety alerts go out until `until` (UTC, the
    restaurant's next local midnight); an admin lifts it with POST
    /admin/api/client/<rid>/storm-cap/lift."""
    if not row:
        return None
    n, limit = row.get("alerts_in_window"), row.get("threshold")
    reason = (f"{n} alerts in an hour" + (f" (limit {limit})" if limit else "")) if n is not None else "alert storm"
    return {"cap": "health and safety alerts only", "until": _iso_z(row.get("until_at"), "UTC"),
            "reason": reason, "until_at": row.get("until_at"), "started_at": _iso_z(row.get("started_at"), "UTC"),
            "local_day": row.get("local_day"), "alerts_in_window": n, "threshold": limit,
            "suppressed": row.get("suppressed") or 0}


DELETION_DUE_DAYS = 30


def _deletion_for(r):
    """An open account-deletion request (#34): when, when it is due (30
    days), days left. None when there is none."""
    req = r.get("deletion_requested_at")
    at = _parse_utc(req, "UTC") if req else None
    if not at:
        return None
    due = at + timedelta(days=DELETION_DUE_DAYS)
    return {"requested_at": at.strftime(_ZFMT), "due_at": due.strftime(_ZFMT),
            "days_left": (due - _utcnow()).days, "due_label": _mdy(due.strftime(_ZFMT), "UTC")}


# The review fetch runs at these Chicago hours (scheduler.py, _latest_slot).
REVIEW_FETCH_SLOTS = (8, 12, 16, 20)
# How long after a slot starts its pass may still be working through the
# list (run_daily_fetch is bounded and resumes from a cursor).
FETCH_SLOT_GRACE = timedelta(hours=1)


def _now_ct():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Chicago"))


def fetched_at_ct(raw):
    """restaurants.last_fetched_at as an aware Chicago time. models writes
    Chicago local with a 'T'; SQLite's datetime('now') (older rows, tests,
    hand fixes) is UTC with a space."""
    from zoneinfo import ZoneInfo
    from time_utils import parse_stamp
    d = parse_stamp(raw, naive_tz="America/Chicago")
    return None if d is None else d.astimezone(ZoneInfo("America/Chicago"))


def fetch_slots_missed(last_fetched_at, now=None) -> int:
    """How many review-fetch slots have come and gone (each given
    FETCH_SLOT_GRACE to finish) since this restaurant was last fetched.
    0 when it is current; None when it has never been fetched."""
    last = fetched_at_ct(last_fetched_at)
    if last is None:
        return None
    now = now or _now_ct()
    cutoff = now - FETCH_SLOT_GRACE
    missed, day = 0, cutoff.date()
    # Walk back over slot starts until one is at or before the last fetch.
    for back in range(0, 8):
        d = day - timedelta(days=back)
        for h in sorted(REVIEW_FETCH_SLOTS, reverse=True):
            start = datetime(d.year, d.month, d.day, h, tzinfo=cutoff.tzinfo)
            if start > cutoff:
                continue
            if start <= last:
                return missed
            missed += 1
    return missed


# ── issues ───────────────────────────────────────────────────────────────────
#
# An issue is a CONDITION (its key: "{rid}:billing", "job:review_fetch") and
# an OCCURRENCE of it (occurrence_at, UTC): when this instance began, or the
# newest evidence of it — the payment failure, the newest failed email, the
# last fetch before the stall, the moment the level escalated. Resolve
# stores the occurrence it saw; a newer one reopens the issue, and the
# payload says why (#24). An issue with no knowable occurrence is resolved
# for as long as its condition lasts: the first build that finds the
# condition gone retires the resolution, so a recurrence raises it again,
# and it lapses after RESOLUTION_MAX_DAYS regardless.

_SEV_RANK = {"warning": 1, "critical": 2}
_SEG_RANK = {"platform": 0, "customer": 1, "internal": 2}
RESOLUTION_MAX_DAYS = int(os.getenv("ADMIN_RESOLUTION_MAX_DAYS", "30"))
# Keys Resolve refuses: they clear themselves, or are closed another way.
UNRESOLVABLE = {
    "scheduler": "The scheduler heartbeat clears itself the moment the loop beats again, so it can't be marked "
                 "resolved.",
    "platform:error_rate": "The 5xx rate is a live five-minute reading that clears itself, so it can't be "
                           "marked resolved.",
}
_UNRESOLVABLE_KINDS = {
    "deletion": "An account-deletion request is closed by withdrawing it or completing the offboarding, not by "
                "muting it.",
}
SIGNED_UNPAID_WARN_DAYS = int(os.getenv("ADMIN_SIGNED_UNPAID_WARN_DAYS", "7"))
SIGNED_UNPAID_CRIT_DAYS = int(os.getenv("ADMIN_SIGNED_UNPAID_CRIT_DAYS", "30"))
# Consecutive failures of a data source before it is an issue (#46).
SOURCE_FAIL_ISSUE = int(os.getenv("ADMIN_SOURCE_FAIL_ISSUE", "3"))


def _issue_category(key):
    parts = (key or "").split(":")
    if parts and parts[0].isdigit():
        parts = parts[1:]
    return parts[0] if parts else ""


_SINCE = object()


def _make_issue(key, rid, title, severity, *, since=None, zone=OPERATOR_TZ, occurrence=_SINCE, occurrence_zone=None,
                action=None, action_route=None, action_kind=None, action_href=None, action_payload=None,
                detail=None, resolvable=True, segment="customer"):
    """One issue in the shape every list shows. `since` is when it began
    (shown as an age); `occurrence` defaults to it, and None makes the issue
    condition-level (a resolution holds while the condition lasts).
    action_kind says what the console should do with the action: 'post'
    action_route, go to action_href ('link'), open a templated email
    ('mailto'), or nothing."""
    since_dt = _parse_utc(since, zone) if since else None
    occ_raw = since if occurrence is _SINCE else occurrence
    occ_dt = _parse_utc(occ_raw, occurrence_zone or zone) if occ_raw else None
    if action_kind is None:
        if action_route:
            action_kind = ("link" if action_route.startswith(("/admin/view-as/", "/admin/client-settings/"))
                           else "post")
        elif action_href:
            action_kind = "mailto" if action_href.startswith("mailto:") else "link"
    return {"key": key, "restaurant_id": rid, "title": title, "detail": detail, "severity": severity,
            "severity_rank": _SEV_RANK[severity], "since": _since(since_dt) if since_dt else "—",
            "since_at": since_dt.strftime(_ZFMT) if since_dt else None,
            "occurrence_at": occ_dt.strftime("%Y-%m-%d %H:%M:%S") if occ_dt else None,
            "category": _issue_category(key), "segment": segment, "resolvable": resolvable,
            "action": action, "action_route": action_route, "action_kind": action_kind,
            "action_href": action_href, "action_payload": action_payload}


def _reach_out_href(r, owner):
    """'Reach out' as a templated email to the owner (#62)."""
    from urllib.parse import quote
    to = (r.get("owner_email") or (owner or {}).get("email") or "").strip()
    if not to:
        return None
    first = ((r.get("owner_name") or "").split() or ["there"])[0]
    name = r.get("name") or "your restaurant"
    subject = f"Checking in on Cavnar AI at {name}"
    body = (f"Hi {first},\n\nI noticed nobody has been in Cavnar AI for {name} lately. Is anything getting in "
            f"the way, or is there something you'd like set up differently? Happy to jump on a quick call.\n\nWill")
    return f"mailto:{to}?subject={quote(subject)}&body={quote(body)}"


def _integration_action(rid, i):
    """(label, POST route, kind, href) for an integration in error (#62)."""
    data = f"#client/{rid}?tab=data"
    if i["key"] == "instagram":
        return "Refresh token", f"/admin/refresh-ig-token/{rid}", "post", data
    if i["key"] == "google_business":
        return "Reconnect", None, "link", data
    if i["key"] == "webhook":
        return "Open webhook", None, "link", data
    return "Reconnect", None, "link", data


def _suppressed_for(r, users, d):
    """{email: (suppression row, 'owner'|'login')} for this location's owner
    address and its logins' addresses."""
    sup = d.get("suppressed") or {}
    out = {}
    owner_email = (r.get("owner_email") or "").strip().lower()
    if owner_email and owner_email in sup:
        out[owner_email] = (sup[owner_email], "owner")
    for u in users:
        e = (u.get("email") or "").strip().lower()
        if e and e in sup and e not in out and not u.get("is_admin"):
            out[e] = (sup[e], "login")
    return out


def _issues_for(r, d, owner, integrations, modules, onboarding, last_active, billing=None, users=None,
                churn=None, segment=None, deletion=None):
    """Actionable problems for one location, each with severity, age, the
    occurrence it is, and the action that fixes it."""
    rid = r["id"]
    users = users if users is not None else d["users"].get(rid, [])
    segment = segment or _segment(r, users)
    billing = billing or {}
    out = []
    client = f"#client/{rid}"
    billing_tab = f"{client}?tab=billing"
    data_tab = f"{client}?tab=data"

    def add(key, title, severity, since=None, action=None, action_route=None, detail=None, **kw):
        out.append(_make_issue(f"{rid}:{key}", rid, title, severity, since=since, action=action,
                               action_route=action_route, detail=detail, segment=segment, **kw))

    bs = (r.get("billing_status") or "trial").lower()
    ev = (d.get("events") or {}).get(rid, {})
    hist = (d.get("status_changes") or {}).get(rid, {})
    if bs == "past_due":
        # Aged from the payment failure itself — the open invoice's own
        # (H's stripe_invoices), else the event or the status change — not
        # the owner's last click. The fix is a new card: the action sends
        # the Billing Portal link (H, #6).
        inv = (d.get("open_invoices") or {}).get(rid) or {}
        failed_at = inv.get("last_failed_at") or _newest(ev.get("invoice.payment_failed"), hist.get("past_due"))
        bits = []
        if inv.get("remaining_cents"):
            bits.append(f"${int(inv['remaining_cents']) / 100.0:,.2f} due")
        if inv.get("attempts"):
            bits.append(f"{int(inv['attempts'])} attempt{'s' if int(inv['attempts']) != 1 else ''}")
        if inv.get("next_attempt"):
            bits.append(f"Stripe retries {_mdy(inv['next_attempt'])}")
        detail = ("; ".join(bits) + ".") if bits else (None if failed_at else
                                                       "When it failed isn't on record — open billing.")
        add("billing", "Stripe payment failed", "critical", failed_at, "Send card-update link",
            f"/admin/api/billing/{rid}/card-update-link", detail, zone="UTC", action_kind="post",
            action_href=billing_tab)
    if bs in ENDED_STATES:
        at = _newest(ev.get("customer.subscription.deleted"), hist.get("churned"), hist.get("canceled"))
        add("canceled", "Subscription canceled", "warning", at, "Open billing", None, zone="UTC",
            action_kind="link", action_href=billing_tab)
    # A hold only an admin lifts (H, #114): a chargeback, a full refund, or
    # an admin's own. The action is H's lift-hold (a note is required).
    hold = billing.get("pause_reason") if billing.get("pause_reason") in ("dispute", "refund") else None
    hold = hold or (billing.get("hold") if bs == "paused" else None)
    if bs == "paused" and hold in ("dispute", "refund", "admin"):
        at = _newest(ev.get({"dispute": "charge.dispute.created", "refund": "charge.refunded"}.get(hold, "")),
                     hist.get("paused"))
        add(f"paused:{hold}",
            {"dispute": "Chargeback — account on hold", "refund": "Full refund — account on hold",
             "admin": "Account on hold (set by an admin)"}[hold],
            "critical" if hold == "dispute" else "warning", at, "Lift hold", f"/admin/api/billing/{rid}/lift-hold",
            "Only an admin can lift this hold." + (" The reason is read from the Stripe events."
                                                  if billing.get("pause_reason_inferred") else ""),
            zone="UTC", action_kind="post", action_payload={"note": ""}, action_href=billing_tab)
    cs = (r.get("contract_status") or "pending").lower()
    if cs in ("declined", "voided") and not r.get("is_demo") and bs not in ("internal",) + ENDED_STATES:
        # The owner declined it, or it was voided (H, #144): nothing more
        # will come of that envelope. Resend contract sends a new one.
        env = (d.get("envelope_fate") or {}).get(rid) or {}
        add(f"contract:{cs}", f"Contract {cs}", "critical" if cs == "declined" else "warning",
            env.get("status_at") or r.get("created_at"), "Send a new contract", f"/admin/resend-contract/{rid}",
            (f"DocuSign: {env['status_reason']}" if env.get("status_reason") else None), zone="UTC",
            action_href=billing_tab)
    elif cs != "signed" and not r.get("is_demo") and bs not in ("internal",) + ENDED_STATES:
        age = _age_days(r.get("created_at"))
        if age is not None and age > 3:
            add("contract", "Contract still unsigned", "warning", r.get("created_at"), "Resend contract",
                f"/admin/resend-contract/{rid}", action_href=billing_tab)
    # Signed but never paid (#26): warning at 7 days, critical at 30. The
    # occurrence is when the current severity began, so an escalation to
    # critical reopens a warning someone resolved.
    if ((r.get("contract_status") or "") == "signed" and bs in ("trial", "pending", "")
            and not billing.get("billed_by") and not r.get("converted_at") and not r.get("is_demo")):
        signed = billing.get("signed_at")
        basis = signed or _iso_z(r.get("created_at"))
        days = _age_days(basis, "UTC")
        base = _parse_utc(basis, "UTC")
        if days is not None and base is not None and days >= SIGNED_UNPAID_WARN_DAYS:
            crit = days >= SIGNED_UNPAID_CRIT_DAYS
            occ = _utc_stamp(base + timedelta(days=SIGNED_UNPAID_CRIT_DAYS if crit else SIGNED_UNPAID_WARN_DAYS))
            title = (f"Signed {int(days)} days ago, never paid" if signed
                     else f"Contract signed, never paid — joined {int(days)} days ago (signing date not on record)")
            # Where the pay-link chase stands (H's run_contract_chase, #26).
            chase = [x for x in (((d.get("pay_reminders") or {}).get(rid) or {}).get("sent") or "").split(",") if x]
            sent_days = []
            for item in chase:                      # "pay_reminder:<rid>:<day>=<status>"
                key, _sep, status = item.partition("=")
                day = key.rsplit(":", 1)[-1]
                if status.startswith("sent") and day.isdigit():
                    sent_days.append(int(day))
            chase_note = None
            if chase:
                chase_note = (f"Pay link re-sent on day{'s' if len(sent_days) != 1 else ''} "
                              f"{', '.join(str(x) for x in sorted(sent_days))} after signing." if sent_days
                              else "The pay-link reminders have not gone out.")
            add("signed_unpaid", title, "critical" if crit else "warning", basis, "Resend payment link",
                f"/admin/resend-payment/{rid}", chase_note, zone="UTC", occurrence=occ, occurrence_zone="UTC",
                action_href=billing_tab)
    # Billing email the outbox gave up on (H, #12): a welcome, a receipt,
    # a dunning notice or a pay reminder that did not go.
    of = (d.get("owed_failed") or {}).get(rid)
    if of and of.get("n"):
        add("owed_sends", f"{of['n']} billing email{'s' if of['n'] != 1 else ''} not delivered", "warning",
            of.get("first_at"), "Open billing", None, (of.get("sample") or "")[:200] or None, zone="UTC",
            occurrence=of.get("last_at"), occurrence_zone="UTC", action_kind="link", action_href=billing_tab)
    # What the nightly reconcile found (H, #115): recorded, never repaired.
    rc = (d.get("reconcile") or {}).get(rid)
    rc_findings = (_json_or_none(rc.get("mismatches")) or []) if rc else []
    if rc_findings:
        add("reconcile", f"Stripe and this account disagree ({len(rc_findings)} "
                         f"finding{'s' if len(rc_findings) != 1 else ''})", "warning", rc.get("first_seen_at"),
            "Open billing", None, "; ".join(str(m.get("detail") or m.get("kind")) for m in rc_findings[:3])[:300],
            zone="UTC", occurrence=rc.get("first_seen_at"), occurrence_zone="UTC", action_kind="link",
            action_href=billing_tab)
    for i in integrations:
        if i["state"] == "error":
            sev = "critical" if i["key"] in ("toast", "square", "clover", "rpower", "google_business") else "warning"
            label, route, kind, href = _integration_action(rid, i)
            add(f"int:{i['key']}", f"{i['label']} needs attention", sev, i.get("error_since") or i.get("last_success"),
                label, route, i["error"], zone="UTC", occurrence=i.get("error_since") or i.get("last_success"),
                occurrence_zone="UTC", action_kind=kind, action_href=href)
    for m in modules:
        if m["state"] == "stale":
            add(f"stale:{m['key']}", f"{m['label']} data is stale", "warning", m["last_data"],
                "Sync now" if m["key"] in ("reviews", "labor") else "Check data",
                f"/admin/fetch-reviews/{rid}" if m["key"] == "reviews" else None,
                f"Last data {_since(m['last_data'])} ago", action_href=data_tab)
        elif m["state"] == "no_data":
            add(f"nodata:{m['key']}", f"{m['label']} is on but has never received data", "warning",
                r.get("created_at"), "Check setup", None, action_kind="link", action_href=data_tab)
        elif m["state"] == "unconfigured":
            add(f"unconf:{m['key']}", f"{m['label']} is on but not configured", "warning", r.get("created_at"),
                "Open settings", f"/admin/client-settings/{rid}")
    # Fetch coverage on the schedule's own clock (DATA-7). "Stale" above is
    # keyed on 3 days of review DATA, which a quiet restaurant produces with
    # every fetch working; this says the fetch itself stopped reaching it.
    # One missed slot can be the bounded pass's tail; two is not.
    if (r.get("module_reviews") and not r.get("is_demo") and bs in ("active", "trial")
            and r.get("last_fetched_at")):
        missed = fetch_slots_missed(r.get("last_fetched_at"))
        if missed and missed >= FLEET_FETCH_MISSED_SLOTS:
            add("fetch_behind", f"Review fetch has missed {missed} scheduled runs", "warning",
                r.get("last_fetched_at"), "Sync now", f"/admin/fetch-reviews/{rid}",
                "Fetches run at 8am, noon, 4pm and 8pm Central.")
    rv = d["reviews"].get(rid, {})
    if (rv.get("urgent_stale") or 0) > 0:
        add("urgent", f"{rv['urgent_stale']} urgent review{'s' if rv['urgent_stale'] > 1 else ''} unanswered 48h+",
            "critical", rv.get("urgent_stale_last"), "View as client", f"/admin/view-as/{rid}")
    stalled = (rv.get("stalled_analysis") or 0) + (rv.get("stalled_drafts") or 0)
    if stalled:
        max_ai = int(getattr(_models_mod, "MAX_AI_ATTEMPTS", 5) or 5)
        add("stalled_reviews", f"{stalled} review{'s' if stalled != 1 else ''} stuck after {max_ai} failed AI attempts",
            "critical" if stalled >= 10 else "warning", rv.get("stalled_last"), "Retry AI",
            f"/admin/api/client/{rid}/retry-ai",
            f"{rv.get('stalled_analysis') or 0} never analysed, {rv.get('stalled_drafts') or 0} never drafted. "
            "Nothing retries them on its own.", action_href=f"{client}?tab=messages")
    em = d["emails"].get(rid, {})
    bad = (em.get("failed_7d") or 0) + (em.get("bounced_7d") or 0) + (em.get("complained_7d") or 0)
    if bad > 0:
        parts = [f"{em[k]} {w}" for k, w in (("failed_7d", "failed"), ("bounced_7d", "bounced"),
                                             ("complained_7d", "marked as spam")) if em.get(k)]
        add("email", f"{bad} email{'s' if bad > 1 else ''} failed this week", "warning", em.get("last_failed_at"),
            "Open emails", None, ", ".join(parts), zone="UTC", action_kind="link",
            action_href=f"{client}?tab=messages")
    for addr, (sup, who) in sorted(_suppressed_for(r, users, d).items()):
        scope = (sup.get("scope") or "all").strip().lower()
        if scope == "guest":
            continue            # a restaurant's guest list, not the owner's own mail
        all_mail = scope in ("all", "")
        tag = hashlib.sha1(addr.encode()).hexdigest()[:10]
        whom = "Owner" if who == "owner" else "A login's"
        add(f"suppressed:{tag}", f"{whom} email address is suppressed ({sup.get('reason') or 'bounce'})",
            "critical" if all_mail else "warning", sup.get("created_at"), "Reinstate address",
            "/admin/api/suppressions/reinstate",
            (f"{addr} gets no {'' if all_mail else 'marketing '}email from Cavnar AI"
             + (" — alerts, briefs and reports included." if all_mail else ".")),
            zone="UTC", action_payload={"email": addr})
    pu = d["pushes"].get(rid, {})
    tk = d["tokens"].get(rid, {})
    if (pu.get("failed_7d") or 0) > 0 or (tk.get("disabled") or 0) > 0:
        add("push", f"Push delivery failing ({pu.get('failed_7d') or 0} failed, {tk.get('disabled') or 0} dead device{'s' if (tk.get('disabled') or 0) != 1 else ''})",
            "warning", pu.get("last_failed_at"), "Open notifications", None, zone="UTC", action_kind="link",
            action_href=f"{client}?tab=messages")
    sp = d["sched_posts"].get(rid, {})
    if (sp.get("failed") or 0) > 0:
        add("posts", f"{sp['failed']} scheduled post{'s' if sp['failed'] > 1 else ''} failed to publish", "warning",
            sp.get("last_failed_at"), "Open marketing", None, zone="UTC", action_kind="link",
            action_href=f"{client}?tab=messages")
    # The operator's onboarding issue no longer depends on the OWNER hiding
    # their setup card (#152).
    if not r.get("is_demo") and bs in ("active", "trial") and not onboarding["complete"]:
        age = _age_days(r.get("created_at"))
        if age is not None and age > 7 and onboarding["done"] < max(2, onboarding["total"] // 2):
            add("onboarding", f"Onboarding stuck at {onboarding['done']}/{onboarding['total']}", "warning",
                r.get("created_at"), "Open onboarding", None,
                "The owner hid their setup card; this stays open until the steps are done."
                if onboarding.get("dismissed") else None, action_kind="link", action_href="#customers/onboarding")
    # Inactivity is a PAYING account's alarm, read off real owner activity
    # (#49); a covered sibling's owner is active on the paying location.
    if bs in PAYING_STATES and not r.get("is_demo") and not billing.get("billed_by"):
        age = _age_days(last_active, "UTC")
        base = _parse_utc(last_active, "UTC")
        href = _reach_out_href(r, owner)
        if age is None or age > 30:
            add("inactive", "No activity in 30+ days", "critical", last_active, "Reach out", None, zone="UTC",
                occurrence=_utc_stamp(base + timedelta(days=30)) if base else None, occurrence_zone="UTC",
                action_kind="mailto" if href else None, action_href=href)
        elif age > 14:
            add("inactive", "No activity in 14+ days", "warning", last_active, "Reach out", None, zone="UTC",
                occurrence=_utc_stamp(base + timedelta(days=14)), occurrence_zone="UTC",
                action_kind="mailto" if href else None, action_href=href)
    if churn and churn.get("scored") and churn.get("level") == "high" \
            and (churn.get("days_at_level") or 0) >= CHURN_ISSUE_DAYS:
        add("churn_risk", f"High churn risk for {churn['days_at_level']} days", "warning", churn.get("since"),
            "Send value recap", f"/admin/api/client/{rid}/value-recap", "; ".join(churn.get("reasons")[:3]),
            zone="UTC")
    ai = d["ai_month"].get(rid, {})
    wk = (d["ai_week"].get(rid) or {}).get("calls") or 0
    prev = (d["ai_prev"].get(rid) or {}).get("calls") or 0
    ai_href = "#operations/ai"
    if wk >= 40 and prev and wk > 4 * prev:
        # A rolling-window condition: resolved, it holds while the spike
        # lasts and a later spike (after it clears) raises again.
        add("ai_spike", f"AI usage {wk // max(prev, 1)}× last week's ({wk} calls)", "warning", ai.get("last_at"),
            "Open AI ops", None, zone="UTC", occurrence=None, action_kind="link", action_href=ai_href)
    # Near or past one of this restaurant's own ceilings (G's budget_watch,
    # #122) — it replaced a flat "$25 in 30 days", which said nothing about
    # a trial's $5 a day or a Places ceiling. The occurrence is the ceiling's
    # own window: a warning resolved today comes back tomorrow, a month's
    # next month.
    watch = [b for b in d.get("budget_watch") or [] if b["restaurant_id"] == rid]
    for b, issue in zip(watch, ai_budget_issues(watch)):
        window = (_utc_today() + " 00:00:00") if b["scope"].endswith("_day") else (_utc_month() + " 00:00:00")
        add(f"ai_budget:{b['scope']}", issue["title"], issue["severity"], window, "Open AI ops", None,
            issue["detail"], zone="UTC", occurrence=window, occurrence_zone="UTC", action_kind="link",
            action_href=ai_href)
    # Cost and rate anomalies and loops over the last two days (#140), one
    # issue per kind, action and day.
    mine = [a for a in d.get("ai_anomalies") or [] if a.get("restaurant_id") == rid]
    for a, issue in zip([a for a in mine if a["kind"] in ("cost", "rate", "loop")], ai_anomaly_issues(mine)):
        day = (a.get("day") or _utc_today()) + " 00:00:00"
        add(f"ai_anomaly:{a['kind']}:{a.get('action') or ''}:{a.get('day') or ''}", issue["title"], issue["severity"],
            day, "Open AI ops", None, issue["detail"], zone="UTC", occurrence=day, occurrence_zone="UTC",
            action_kind="link", action_href=ai_href)
    af = d["ai_failed_week"].get(rid) or {}
    if (af.get("n") or 0) >= 3:
        add("ai_failures", f"{af['n']} AI calls failed this week", "critical" if af["n"] >= 10 else "warning",
            af.get("last_at"), "Open AI ops", detail=(af.get("sample") or "")[:160], zone="UTC",
            action_kind="link", action_href=ai_href)
    if deletion:
        overdue = deletion["days_left"] < 0
        add("deletion",
            (f"Account deletion overdue by {-deletion['days_left']} days (was due {deletion['due_label']})" if overdue
             else f"Account deletion requested — due {deletion['due_label']}"),
            "critical", deletion["requested_at"], "Open offboarding", None,
            f"Requested {_mdy(deletion['requested_at'])} in the app. Withdraw the request or finish the "
            f"offboarding by {deletion['due_label']} (App Store guideline 5.1.1(v)).",
            zone="UTC", resolvable=False, action_kind="link", action_href=f"{client}?tab=access")
    sub = billing.get("subscription") or {}
    stored_mm = sub.get("module_mismatch") if isinstance(sub.get("module_mismatch"), dict) else None
    if stored_mm and bs in PAYING_STATES:
        # H records the disagreement when a Stripe plan change was not
        # re-applied to the flags (stripe_subscriptions.module_mismatch).
        add("modules_mismatch", "Stripe plan and module access disagree", "warning",
            stored_mm.get("at") or sub.get("updated_at"), "Open billing", None,
            f"Stripe: {', '.join(stored_mm.get('stripe') or []) or 'none'}. "
            f"Here: {', '.join(stored_mm.get('local') or []) or 'none'}. Change plan, or set the modules to match.",
            zone="UTC", action_kind="link", action_href=billing_tab)
    elif sub.get("module_keys") is not None and bs in PAYING_STATES:
        stripe_set = {k.strip().lower() for k in str(sub["module_keys"]).split(",") if k.strip()} & set(_GRANTABLE_MODULES)
        local = {k for k in _GRANTABLE_MODULES if r.get(f"module_{k}")}
        if stripe_set and stripe_set != local:
            add("modules_mismatch", "Stripe plan and module access disagree", "warning", sub.get("updated_at"),
                "Open billing", None,
                f"Stripe: {', '.join(sorted(stripe_set))}. Here: {', '.join(sorted(local)) or 'none'}. The next "
                "subscription update resets the modules to Stripe's list.", zone="UTC", action_kind="link",
                action_href=billing_tab)
    if billing.get("status_mismatch") and not rc_findings:
        # The reconcile's own finding (above) says it, verified against Stripe.
        mm = billing["status_mismatch"]
        add("stripe_mismatch", f"Stripe says {mm['stripe']}; the account says {mm['local']}", "warning",
            sub.get("updated_at"), "Open billing", None, zone="UTC", action_kind="link", action_href=billing_tab)
    try:
        import data_health as _dh
        labels = getattr(_dh, "OWNER_LABEL", {}) or {}
    except Exception:
        labels = {}
    for s in (d.get("source_health") or {}).get(rid, []):
        n = int(s.get("consecutive_failures") or 0)
        if n < SOURCE_FAIL_ISSUE or s.get("source") in ("pos", "gbp"):
            continue        # POS and Business Profile are the integration issues above
        auth = s.get("error_class") == "auth"
        add(f"source:{s['source']}", f"{labels.get(s['source'], s['source'])} sync failing ({n} in a row)",
            "critical" if auth else "warning", s.get("first_failed_at"), "Open data", None,
            (s.get("last_error") or "")[:160], zone="UTC", action_kind="link", action_href=data_tab)
    return out


def _resolution_covers(issue, res):
    """(covered, reason). A resolution covers an occurrence at or before the
    one it stored (or, for a row written before occurrences were stored, at
    or before when it was resolved). An issue with no knowable occurrence is
    covered until the condition clears or RESOLUTION_MAX_DAYS pass."""
    if not issue.get("resolvable", True):
        return False, None
    occ = _parse_utc(issue.get("occurrence_at"), "UTC")
    if occ is None:
        age = _age_days(res.get("resolved_at"), "UTC")
        if age is not None and age > RESOLUTION_MAX_DAYS:
            return False, "expired"
        return True, None
    base = _parse_utc(res.get("occurrence_at") or res.get("resolved_at"), "UTC")
    if base is None or occ <= base + timedelta(seconds=1):
        return True, None
    return False, "newer_occurrence"


_REOPEN_LABEL = {"newer_occurrence": "It happened again after it was resolved.",
                 "expired": f"Resolved over {RESOLUTION_MAX_DAYS} days ago and still happening."}


def _apply_resolutions(issues, d):
    """(open, resolved) — the open ones carry `reopened` when a resolution
    exists but no longer covers them."""
    resolved = d.get("resolved") or {}
    open_, muted = [], []
    for i in issues:
        res = resolved.get(i["key"])
        if not res:
            open_.append(i)
            continue
        covered, reason = _resolution_covers(i, res)
        info = {"resolved_at": _iso_z(res.get("resolved_at"), "UTC"), "resolved_by": res.get("actor"),
                "note": res.get("note"), "occurrence_at": _iso_z(res.get("occurrence_at"), "UTC")}
        if covered:
            muted.append(dict(i, resolution=info))
        else:
            if reason:
                i["reopened"] = dict(info, reason=reason, label=_REOPEN_LABEL.get(reason))
            open_.append(i)
    return open_, muted


def _retire_resolutions(keys, loaded_at, note):
    """A resolution whose condition was gone at a complete build is retired
    (#24): the next time it happens it is a new issue. Only rows resolved
    before that build loaded are touched."""
    if not keys:
        return
    conn = get_conn()
    try:
        for k in keys:
            cur = conn.execute("DELETE FROM admin_issue_resolutions WHERE key=? AND "
                               "julianday(resolved_at) <= julianday(?)", (k, loaded_at))
            if cur.rowcount:
                conn.execute("INSERT INTO admin_issue_resolution_history (key, action, note, actor) "
                             "VALUES (?, 'cleared', ?, 'system')", (k, note))
        conn.commit()
    except Exception as e:
        _note_failure("admin_issue_resolutions", e)
    finally:
        conn.close()


_RID_KEY = re.compile(r"^\d+:")


# ── page payloads ────────────────────────────────────────────────────────────

def _annotate(issue, rec):
    issue["restaurant"] = rec["name"]
    issue["brand"] = rec["brand"]
    issue["location_name"] = rec["location_name"]
    issue["owner"] = (rec["owner"] or {}).get("username")
    return issue


def _records(d=None):
    """(recs, d), built fresh — every payload reads it through
    _records_cached. Issues are annotated here, once, so no payload looks a
    record up per issue (the quadratic scan, SCALE-17)."""
    d = d or _load_everything()
    d["raw_issue_keys"] = set()
    recs = [location_record(r, d) for r in d["rests"]]
    # Duplicate brand names across separate accounts (not multi-location groups).
    seen = {}
    for rec in recs:
        if rec["is_demo"] or rec["brand"] != rec["name"]:
            continue
        key = re.sub(r"[^a-z0-9]", "", (rec["name"] or "").lower())
        seen.setdefault(key, []).append(rec)
    for key, group in seen.items():
        if len(group) > 1 and key:
            for rec in group:
                dup = _make_issue(f"{rec['id']}:dup", rec["id"],
                                  f"Possible duplicate: {len(group)} accounts named \"{rec['name']}\"", "warning",
                                  action="Review", action_kind="link",
                                  action_href=f"#customers/restaurants?q={rec['name']}", segment=rec["segment"])
                d["raw_issue_keys"].add(dup["key"])
                opened, muted = _apply_resolutions([dup], d)
                rec["issues"].extend(opened)
                rec["issues_resolved"].extend(muted)
                if opened and rec["health"] == "healthy":
                    rec["health"] = "warning"
    for rec in recs:
        for i in rec["issues"]:
            _annotate(i, rec)
        for i in rec["issues_resolved"]:
            _annotate(i, rec)
    if not _current_errors():
        gone = [k for k in (d.get("resolved") or {}) if _RID_KEY.match(k) and k not in d["raw_issue_keys"]]
        _retire_resolutions(gone, d.get("loaded_at") or _utc_stamp(), "the condition cleared, so a recurrence raises it again")
    return recs, d


def _brands(recs):
    """OWNER → BRAND → LOCATION. A brand is a location_group; a lone restaurant
    is its own brand. Brand health is the worst location's, never an average.
    `monthly` sums locations whose MRR is already counted once per
    subscription, so a group is never billed twice here (#116)."""
    groups = {}
    for rec in recs:
        groups.setdefault(rec["brand"], []).append(rec)
    rank = {"inactive": -1, "healthy": 0, "warning": 1, "critical": 2}
    out = []
    for brand, locs in groups.items():
        worst = max(locs, key=lambda x: rank[x["health"]])
        owners = {}
        for l in locs:
            if l["owner"]:
                owners[l["owner"]["id"]] = l["owner"]
        out.append({"brand": brand, "locations": sorted(locs, key=lambda x: x["id"]), "count": len(locs),
                    "health": worst["health"], "owners": list(owners.values()),
                    "issues": sum(len(l["issues"]) for l in locs),
                    "monthly": round(sum(l["billing"]["monthly"] for l in locs), 2),
                    "segment": "internal" if all(l["segment"] == "internal" for l in locs) else "customer",
                    "is_demo": all(l["is_demo"] for l in locs)})
    out.sort(key=lambda b: (-rank[b["health"]], b["brand"].lower()))
    return out


def _brand_rows(recs):
    return [{**{k: v for k, v in b.items() if k != "locations"}, "location_ids": [l["id"] for l in b["locations"]]}
            for b in _brands(recs)]


def _job_failure_kind(f, has_kind):
    """job | request | ai_quality | audit. From job_failures.kind (fix round
    D) — rows captured before that column existed were given theirs once,
    when the boot migration added it. Only a database without the column is
    judged by job name and text, the same inference (ops.infer_failure_kind,
    #58). AI-quality findings themselves are ai_quality_events rows now
    (fix round G), never job_failures."""
    if has_kind:
        return (f.get("kind") or "job").lower()
    import ops as _ops_kind
    return _ops_kind.infer_failure_kind(f.get("job"), f.get("error"))


def _platform_issues(recs, d):
    """(issues, facts) for problems that belong to no one restaurant: failed
    jobs (kind='job' only), fleet fetch coverage, jobs past their SLA, the
    scheduler heartbeat, the 5xx rate, inbound webhook verification, email
    bounce and complaint rates, stalled reviews fleet-wide, a missing daily
    snapshot. `facts` carries the readings overview's KPIs reuse."""
    out, facts = [], {}
    resolved = d.get("resolved") or {}
    raw_keys = set()

    def add(key, title, severity, **kw):
        issue = _make_issue(key, None, title, severity, segment="platform", **kw)
        issue.update(restaurant="Platform", brand="Platform", location_name=None, owner=None)
        raw_keys.add(key)
        out.append(issue)

    # Failed jobs — kind='job' only: AI-quality findings and console request
    # errors are counted apart and never become "Job X failed" (#58).
    has_kind = d.get("job_failures_has_kind")
    by_job, kinds = {}, {"job": 0, "request": 0, "ai_quality": 0, "audit": 0}
    for f in d.get("job_failures") or []:
        age = _age_hours(f["created_at"], "UTC")
        if age is None or age > 24:
            continue
        kind = _job_failure_kind(f, has_kind)
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind == "job":
            by_job.setdefault(f["job"], []).append(f)
    facts["failures_24h"] = kinds
    facts["jobs_failing"] = len(by_job)
    facts["job_kind_basis"] = "job_failures.kind" if has_kind else "inferred from the job name and error text"
    for job, fs in by_job.items():
        add(f"job:{job}", f"Job `{job}` failed {len(fs)}× in 24h", "critical" if len(fs) >= 5 else "warning",
            since=fs[-1]["created_at"], zone="UTC", occurrence=fs[0]["created_at"], occurrence_zone="UTC",
            detail=(fs[0]["error"] or "")[:160], action="Open jobs", action_kind="link",
            action_href="#operations/jobs")
    # FLEET COVERAGE, on the fetch schedule's own Chicago clock (#129): a
    # restaurant is behind when two or more scheduled slots have passed
    # since its last fetch. Reading a Chicago stamp as UTC raised a false
    # critical every night.
    stale_fetch = []
    for rec in recs:
        if rec["segment"] != "customer" or rec["billing"]["status"] in ENDED_STATES + ("paused",):
            continue
        if not any(m["key"] == "reviews" and m["enabled"] for m in rec.get("modules") or []):
            continue
        last = (rec.get("freshness") or {}).get("reviews")
        if not last:
            joined = _age_days(rec.get("created_at"), "UTC")
            if joined is not None and joined <= 3:
                continue
            stale_fetch.append((rec, None, None))
            continue
        missed = fetch_slots_missed(last)
        if missed and missed >= FLEET_FETCH_MISSED_SLOTS:
            stale_fetch.append((rec, missed, last))
    customers = [r for r in recs if r["segment"] == "customer"]
    if stale_fetch:
        n = len(stale_fetch)
        names = ", ".join(r["name"] for r, _m, _l in stale_fetch[:4])
        lasts = [l for _r, _m, l in stale_fetch if l]
        crit = n > max(3, len(customers) // 10)
        add("fleet:fetch_coverage", f"{n} restaurant{'' if n == 1 else 's'} missed {FLEET_FETCH_MISSED_SLOTS}+ "
                                    "scheduled review fetches", "critical" if crit else "warning",
            since=min(lasts, key=lambda s: _parse_utc(s, "UTC")) if lasts else None, zone="UTC",
            occurrence=max(lasts, key=lambda s: _parse_utc(s, "UTC")) if lasts else None, occurrence_zone="UTC",
            detail=(f"{names}{'…' if n > 4 else ''}. Nothing failed — the pass did not reach them. "
                    "Fetches run at 8am, noon, 4pm and 8pm Central."),
            action="Open jobs", action_kind="link", action_href="#operations/jobs")
    facts["fetch_behind"] = len(stale_fetch)
    # Expected jobs past their SLA (ops.EXPECTED_JOBS, DH2-2).
    overdue_ok = True
    try:
        import ops as _ops_sla
        overdue = _ops_sla.jobs_overdue()
    except Exception as e:
        overdue, overdue_ok = [], False
        _note_failure("jobs_overdue", e)
    facts["jobs_overdue"] = len(overdue)
    for j in overdue:
        add(f"job_overdue:{j['job']}", f"Job `{j['job']}` has not run successfully in {j['hours_since']:g}h",
            "critical", since=j["last_ok_at"], zone="UTC", occurrence=j["last_ok_at"], occurrence_zone="UTC",
            detail=f"Expected within {j['max_hours']}h. Last success: {j['last_ok_at'] or 'never'}.",
            action="Open jobs", action_kind="link", action_href="#operations/jobs")
    # The loop's own heartbeat (status_manager.scheduler_state, the one
    # reading /health, the SLA page and the status page share), from the
    # database this build reads: stale, wedged past a job's own bound, or
    # ticks failing part-way (#4, #121).
    import status_manager as _sm_hb
    try:
        sched = _sm_hb.scheduler_state(_current_db_path())
    except Exception as e:
        sched = {"state": "unknown", "beat_age_minutes": None}
        _note_failure("scheduler_heartbeat", e)
    hb = sched.get("beat_age_minutes")
    facts["heartbeat"] = hb
    facts["scheduler_state"] = sched.get("state")
    if sched.get("state") in ("unknown", "stale", "wedged", "stalled"):
        if sched["state"] == "wedged":
            title = (f"Scheduler stuck in `{sched.get('running_job')}` for {int(sched.get('running_minutes') or 0)} "
                     f"minutes (bound {sched.get('running_bound_minutes')})")
        elif sched["state"] == "stalled":
            title = (f"Scheduler has not completed a tick in {int(sched.get('loop_completed_age_minutes') or 0)} "
                     "minutes")
        elif hb is not None:
            title = "Scheduler heartbeat is stale"
        else:
            title = "Scheduler heartbeat is unreadable"
        add("scheduler", title, "critical", detail=f"{int(hb)} minutes since the last beat" if hb is not None else None,
            resolvable=False, action="Check Railway", action_kind="link", action_href="#engineering")
        out[-1]["since"] = f"{int(hb)}m" if hb is not None else "—"
    # Latency and error rate. Rolling, in-process, reset on deploy — see
    # http_layer.request_metrics for why it is not a table.
    try:
        from http_layer import request_metrics
        rm = request_metrics()
        facts["request_metrics"] = rm
        if rm["requests"] >= 20 and rm["server_error_rate"] >= 5.0:
            add("platform:error_rate", f"{rm['server_error_rate']:.0f}% of requests are 5xx", "critical",
                detail=f"{rm['requests']} requests in the last {rm['window_seconds'] // 60} minutes.",
                resolvable=False, action="Open engineering", action_kind="link", action_href="#engineering")
            out[-1]["since"] = "now"
    except Exception as e:
        log.warning("request metrics unavailable: %s", e)
    conn = get_conn()
    try:
        wh = _webhook_health(conn)
        rates = _email_rates(conn, d.get("windows") or _windows())
        snap = _one_dict(conn, "SELECT MAX(date) AS last FROM business_metrics_daily", label="business_metrics_daily",
                         optional=True) or {}
    finally:
        conn.close()
    facts["webhooks"] = wh
    facts["email_rates"] = rates
    # Inbound webhooks (#74): signature failures with no verified event in
    # 24 hours mean a rotated secret is silently dropping every delivery.
    for p in wh["providers"]:
        if p["problem"]:
            add(f"webhook:{p['provider']}", f"{p['label']} webhook is failing verification", "critical",
                since=p.get("first_failure_at"), zone="UTC", occurrence=p.get("first_failure_at"),
                occurrence_zone="UTC",
                detail=(f"{p['failures_24h']} signature failure{'s' if p['failures_24h'] != 1 else ''} in 24h and "
                        f"no verified event since {_mdy(p['last_verified_at']) if p.get('last_verified_at') else 'ever'}. "
                        f"Check {p['secret_env']} against the provider's dashboard."),
                action="Open integrations", action_kind="link", action_href="#operations/integrations")
        elif p.get("stale"):
            add(f"webhook_stale:{p['provider']}", f"No {p['label']} webhook event in {int(p['age_hours'])}h",
                "warning", since=p.get("last_verified_at"), zone="UTC", occurrence=p.get("last_verified_at"),
                occurrence_zone="UTC",
                detail="Mail went out, but no delivery event came back: check the webhook's signing secret.",
                action="Open email", action_kind="link", action_href="#operations/email")
    # Email bounce and complaint rates (#59), over the last 7 days.
    wk = rates.get("7d") or {}
    if wk.get("enough"):
        for kind, label, warn, crit in (("bounce", "bounce", EMAIL_BOUNCE_WARN_PCT, EMAIL_BOUNCE_CRIT_PCT),
                                        ("complaint", "spam-complaint", EMAIL_COMPLAINT_WARN_PCT,
                                         EMAIL_COMPLAINT_CRIT_PCT)):
            pct = wk.get(f"{kind}_rate")
            if pct is not None and pct >= warn:
                add(f"email:{kind}_rate", f"Email {label} rate {pct:g}% this week", "critical" if pct >= crit else "warning",
                    since=wk.get(f"last_{kind}_at"), zone="UTC", occurrence=wk.get(f"last_{kind}_at"),
                    occurrence_zone="UTC",
                    detail=f"{wk.get(kind + 's') or 0} of {wk['accepted']} accepted sends. Warning at {warn:g}%, "
                           f"critical at {crit:g}%.",
                    action="Open email", action_kind="link", action_href="#operations/email")
    # Stalled reviews across the fleet (#124): the occurrence is the newest
    # stalled review, so a rising count reopens a resolved issue.
    stalled = [(r, r["reviews"]["stalled"]) for r in customers if r["reviews"].get("stalled")]
    facts["stalled_reviews"] = sum(n for _r, n in stalled)
    if stalled:
        newest = max((d["reviews"].get(r["id"], {}).get("stalled_last") for r, _n in stalled),
                     key=lambda s: _parse_utc(s) or datetime.min.replace(tzinfo=timezone.utc), default=None)
        total = facts["stalled_reviews"]
        add("stalled:fleet", f"{total} review{'s' if total != 1 else ''} stuck after failed AI attempts at "
                             f"{len(stalled)} restaurant{'s' if len(stalled) != 1 else ''}",
            "critical" if total >= 25 else "warning", since=newest, occurrence=newest,
            detail="Each restaurant's own issue carries the Retry AI action.", action="Open AI ops",
            action_kind="link", action_href="#operations/ai")
    # The daily business snapshot (#18) — only once one has ever been written.
    last_snap = snap.get("last")
    facts["metrics_snapshot_last"] = last_snap
    if last_snap:
        try:
            from datetime import date as _date
            gap = (_now_ct().date() - _date.fromisoformat(last_snap)).days
        except (TypeError, ValueError):
            gap = None
        if gap is not None and gap >= 2:
            add("metrics:snapshot", f"The daily business snapshot hasn't run in {gap} days", "warning",
                since=last_snap, zone=OPERATOR_TZ, detail="MRR and account history have a gap for those days.",
                action="Open jobs", action_kind="link", action_href="#operations/jobs")
    opened, _muted = _apply_resolutions(out, d)
    facts["platform_resolved"] = _muted
    if not _current_errors():
        gone = [k for k in resolved if not _RID_KEY.match(k) and k not in raw_keys and k not in UNRESOLVABLE
                and (overdue_ok or not k.startswith("job_overdue:"))]
        _retire_resolutions(gone, _utc_stamp(), "the condition cleared, so a recurrence raises it again")
    return opened, facts


def _sort_issues(issues):
    return sorted(issues, key=lambda i: (-i["severity_rank"], _SEG_RANK.get(i.get("segment"), 1), i["title"]))


def _issue_counts(issues):
    out = {seg: {"total": 0, "critical": 0, "warning": 0} for seg in ("customer", "platform", "internal")}
    for i in issues:
        c = out.setdefault(i.get("segment") or "customer", {"total": 0, "critical": 0, "warning": 0})
        c["total"] += 1
        c[i["severity"]] = c.get(i["severity"], 0) + 1
    out["attention"] = out["customer"]["total"] + out["platform"]["total"]
    out["critical"] = out["customer"]["critical"] + out["platform"]["critical"]
    return out


def _all_issues(recs, d):
    """(every open issue sorted, platform facts)."""
    platform, facts = _platform_issues(recs, d)
    return _sort_issues([i for r in recs for i in r["issues"]] + platform), facts


ISSUES_IN_OVERVIEW = 60


def _real(recs):
    return [r for r in recs if r["segment"] == "customer"]


def _mrr_totals(recs):
    """Billed MRR once per subscription (mirror), list MRR, committed
    (trialing) and how much still falls back to list price (#15, #116)."""
    real = _real(recs)
    payers = [r for r in real if r["billing"]["mrr_source"] in ("mirror", "list")]
    fallback = [r for r in real if r["billing"]["mrr_source"] == "list" and r["billing"]["monthly"]]
    mirrored = [r for r in real if r["billing"]["mrr_source"] == "mirror"]
    mrr = round(sum(r["billing"]["monthly"] for r in real), 2)
    return {"mrr": mrr,
            "mrr_list": round(sum(r["billing"]["list_monthly"] or 0 for r in payers), 2),
            "mrr_billed": round(sum(r["billing"]["billed_monthly"] or 0 for r in mirrored), 2),
            "mrr_committed": round(sum(r["billing"]["committed_monthly"] or 0 for r in real), 2),
            "mrr_past_due": round(sum(r["billing"]["monthly"] for r in real if r["billing"]["status"] == "past_due"), 2),
            "mrr_fallback_accounts": len(fallback),
            "mrr_source": ("mirror" if mirrored and not fallback else "list" if fallback and not mirrored
                           else "mixed" if fallback else ("mirror" if mirrored else None)),
            "mrr_mismatches": sum(1 for r in real if r["billing"].get("list_mismatch")),
            "subscriptions": len([r for r in real if r["billing"]["monthly"] or r["billing"]["committed_monthly"]]),
            "covered_locations": sum(1 for r in real if r["billing"]["mrr_source"] == "covered"),
            "mrr_basis": ("Billed MRR from the Stripe subscription mirror — amount ÷ interval, once per subscription, "
                          "after discount; trialing subscriptions are committed, not billed. Accounts with no mirror "
                          "row count at list price, once per billing group.")}


def overview():
    with _collecting() as bucket:
        recs, d, meta = _records_cached()
        w = _windows()
        real = _real(recs)
        issues, facts = _all_issues(recs, d)
        conn = get_conn()
        try:
            ai_cols = _columns(conn, "ai_usage")
            llm = " AND COALESCE(vendor,'anthropic')='anthropic'" if "vendor" in ai_cols else ""
            sent = f" AND {_OUTCOME_SQL} <> 'blocked'" if "outcome" in ai_cols else ""
            ai_today = _one_dict(conn, "SELECT COUNT(*) AS n, ROUND(COALESCE(SUM(cost_usd),0),2) AS cost FROM ai_usage "
                                       f"WHERE created_at >= ?{sent}", (w["today"],), optional=True) or {}
            ai_failed_24h = _one_dict(conn, "SELECT COUNT(*) AS n FROM ai_usage WHERE created_at >= ? "
                                            f"AND COALESCE(status,'ok')='error'{llm}{sent}", (w["day"],),
                                      optional=True) or {}
            # A bounce or a complaint is not a delivery (#59); "today" is
            # midnight Central against email_log's UTC stamps (#89).
            emails_today = _one_dict(conn, "SELECT COUNT(*) AS n, "
                                           "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
                                           "SUM(CASE WHEN status='bounced' THEN 1 ELSE 0 END) AS bounced, "
                                           "SUM(CASE WHEN status='complained' THEN 1 ELSE 0 END) AS complained, "
                                           "SUM(CASE WHEN status='delivered' THEN 1 ELSE 0 END) AS delivered "
                                           "FROM email_log WHERE sent_at >= ?", (w["today"],)) or {}
            push_today = _one_dict(conn, "SELECT SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS sent, "
                                         "SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failed FROM push_deliveries "
                                         "WHERE created_at >= ?", (w["today"],)) or {}
            alerts_today = _one_dict(conn, "SELECT COUNT(*) AS n FROM alert_log WHERE fired_at >= ?", (w["today"],)) or {}
            # Findings, not rows: one row can carry several (ai_utils
            # .record_quality_event's n), as G's own rates count them.
            ai_quality = _one_dict(conn, "SELECT COALESCE(SUM(n), 0) AS n FROM ai_quality_events WHERE created_at >= ?",
                                   (w["day"],), optional=True)
            series = _series(recs, d, conn)
        finally:
            conn.close()
        counts = _issue_counts(issues)
        visible = [i for i in issues if i.get("segment") != "internal"]
        hb = facts.get("heartbeat")
        failures = facts.get("failures_24h") or {}
        mrr = _mrr_totals(recs)
        undeliverable = sum(int(emails_today.get(k) or 0) for k in ("failed", "bounced", "complained"))
        month_start = _parse_utc(w["month_start_ct"])
        kpis = {
            "clients": len(real), "active": sum(1 for r in real if r["billing"]["status"] == "active"),
            "trial": sum(1 for r in real if r["billing"]["status"] == "trial"),
            "locations": len(recs), "brands": len(_brands(real)),
            "internal_accounts": sum(1 for r in recs if r["segment"] == "internal"),
            "new_this_month": sum(1 for r in real if (_parse_utc(r["created_at"], "UTC") is not None
                                                      and _parse_utc(r["created_at"], "UTC") >= month_start)),
            "subscriptions": mrr["subscriptions"],
            "mrr": mrr["mrr"], "mrr_list": mrr["mrr_list"], "mrr_billed": mrr["mrr_billed"],
            "mrr_committed": mrr["mrr_committed"], "mrr_past_due": mrr["mrr_past_due"],
            "mrr_source": mrr["mrr_source"], "mrr_fallback_accounts": mrr["mrr_fallback_accounts"],
            "mrr_mismatches": mrr["mrr_mismatches"], "covered_locations": mrr["covered_locations"],
            "past_due": sum(1 for r in real if r["billing"]["status"] == "past_due"),
            "paused": sum(1 for r in real if r["billing"]["status"] == "paused"),
            "canceled": sum(1 for r in real if r["billing"]["status"] in ENDED_STATES),
            "integrations_active": sum(1 for r in real for i in r["integrations"] if i["state"] == "connected"),
            "integrations_failing": sum(1 for r in real for i in r["integrations"] if i["state"] == "error"),
            "ai_calls_today": ai_today.get("n") or 0, "ai_cost_today": float(ai_today.get("cost") or 0),
            "ai_failures_24h": ai_failed_24h.get("n") or 0,
            # AI-quality findings are reported apart from failed jobs (#58).
            "ai_quality_24h": ((ai_quality or {}).get("n") or 0) + failures.get("ai_quality", 0),
            "emails_today": int(emails_today.get("n") or 0) - undeliverable,
            "email_failures_today": undeliverable,
            "email_bounced_today": int(emails_today.get("bounced") or 0),
            "email_complained_today": int(emails_today.get("complained") or 0),
            "email_delivered_today": int(emails_today.get("delivered") or 0),
            "push_today": push_today.get("sent") or 0, "push_failures_today": push_today.get("failed") or 0,
            "alerts_today": alerts_today.get("n") or 0,
            "job_failures_24h": failures.get("job", 0), "jobs_failing": facts.get("jobs_failing", 0),
            "request_errors_24h": failures.get("request", 0), "job_kind_basis": facts.get("job_kind_basis"),
            "attention": counts["attention"], "critical": counts["critical"],
            "scheduler_heartbeat_minutes": hb, "jobs_overdue": facts.get("jobs_overdue", 0),
            # The platform's data health, from the daily snapshots (admin only —
            # never an owner or the public status page: intelligence privacy).
            "data_health_median": _median([r["data_health"]["overall"] for r in real
                                           if r.get("data_health", {}).get("overall") is not None
                                           and not r["data_health"].get("stale")]),
            "sources_failing": sum(len(r.get("data_health", {}).get("failing") or []) for r in real),
            "stalled_reviews": facts.get("stalled_reviews", 0),
            "deletion_requests": sum(1 for r in recs if r.get("deletion")),
            "fetch_behind": facts.get("fetch_behind", 0),
            "trial_conversion": _trial_conversion(recs, d),
            "email_rates": facts.get("email_rates"),
        }
        rm = facts.get("request_metrics")
        if rm:
            # A rolling five-minute window, not "since the last deploy" (#160).
            kpis.update({"rpm": rm["rpm"], "error_rate": rm["error_rate"],
                         "server_error_rate": rm["server_error_rate"],
                         "p50_ms": rm["p50_ms"], "p95_ms": rm["p95_ms"],
                         "api_window_seconds": rm["window_seconds"],
                         "api_window_label": f"last {max(1, rm['window_seconds'] // 60)} minutes"})
        activity_events = activity(limit=30, d=d)["events"]
    return {"ok": True, "kpis": kpis, "issues": visible[:ISSUES_IN_OVERVIEW],
            "issues_total": len(visible), "issues_truncated": len(visible) > ISSUES_IN_OVERVIEW,
            "issue_counts": counts, "internal_issues": [i for i in issues if i.get("segment") == "internal"][:20],
            "activity": activity_events, "brands": _brand_rows(recs), "series": series,
            "webhooks": facts.get("webhooks"), **_payload_meta(meta, bucket)}


def clients():
    with _collecting() as bucket:
        recs, d, meta = _records_cached()
    return {"ok": True, "clients": recs, "brands": _brand_rows(recs), **_payload_meta(meta, bucket)}


def client_detail(rid):
    with _collecting() as bucket:
        recs, d, meta = _records_cached()
        by_id = {r["id"]: r for r in recs}
        rec = by_id.get(rid)
        if not rec:
            return {"ok": False, "error": "Not found"}
        siblings = [r for r in recs if r["brand"] == rec["brand"] and r["id"] != rid]
        conn = get_conn()
        try:
            month = _utc_stamp(_utcnow() - timedelta(days=30))
            ai_by_action = _rows_dict(conn, "SELECT action, model, COUNT(*) AS calls, ROUND(SUM(cost_usd),4) AS cost, SUM(input_tokens)+SUM(output_tokens) AS tokens, MAX(created_at) AS last_at FROM ai_usage WHERE restaurant_id=? AND created_at >= ? GROUP BY action, model ORDER BY cost DESC", (rid, month), optional=True)
            ai_recent = _rows_dict(conn, "SELECT action, model, input_tokens, output_tokens, cost_usd, created_at, COALESCE(status,'ok') AS status, error FROM ai_usage WHERE restaurant_id=? ORDER BY id DESC LIMIT 40", (rid,), optional=True)
            ai_failed = _rows_dict(conn, "SELECT action, model, error, created_at FROM ai_usage WHERE restaurant_id=? AND COALESCE(status,'ok')='error' ORDER BY id DESC LIMIT 20", (rid,), optional=True)
            # Not the per-request 'audit' rows: they carry the restaurant id now and
            # would bury this client's own events (#53) — /admin/api/client/<id>/audit
            # is their view.
            events = _rows_dict(conn, "SELECT id, source, event_type, amount, summary, created_at FROM admin_events WHERE restaurant_id=? AND source <> 'audit' ORDER BY id DESC LIMIT 40", (rid,))
            ai_daily = _fill_days(_rows_dict(conn, "SELECT created_at, cost_usd FROM ai_usage WHERE restaurant_id=? "
                                                   "AND created_at >= ?", (rid, month), optional=True),
                                  "created_at", 30, value_key="cost_usd", count_key="calls", sum_key="cost")
            emails = _rows_dict(conn, "SELECT email_type, to_email, subject, sent_at, status, error FROM email_log WHERE restaurant_id=? ORDER BY id DESC LIMIT 60", (rid,))
            pushes = _rows_dict(conn, "SELECT alert_type, status, ok, attempts, error, created_at FROM push_deliveries WHERE restaurant_id=? ORDER BY id DESC LIMIT 40", (rid,))
            devices = _rows_dict(conn, "SELECT id, user_id, environment, created_at, last_success_at, consecutive_failures, disabled_reason FROM device_tokens WHERE restaurant_id=?", (rid,))
            alerts = _rows_dict(conn, "SELECT alert_type, review_id, fired_at FROM alert_log WHERE restaurant_id=? ORDER BY id DESC LIMIT 40", (rid,))
            acts = _rows_dict(conn, "SELECT event_type, event_data, created_at FROM activity_log WHERE restaurant_id=? ORDER BY id DESC LIMIT 60", (rid,))
            logins = _rows_dict(conn, "SELECT event, ip_address, user_agent, device_type, created_at FROM login_history WHERE restaurant_id=? ORDER BY id DESC LIMIT 30", (rid,))
            for l in logins:
                l["label"] = _login_label(l.get("event"), l.get("device_type"))
            sessions = _rows_dict(conn, "SELECT s.created_at, s.last_active, s.device_type, s.ip_address, u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE u.restaurant_id=? AND julianday(s.expires_at) > julianday('now') ORDER BY julianday(s.last_active) DESC", (rid,))
            jobs, runs, quality = _client_job_rows(conn, rid, d)
            posts = _rows_dict(conn, "SELECT platform, content_type, topic, scheduled_for, status, error, attempts, posted_at FROM marketing_scheduled_posts WHERE restaurant_id=? ORDER BY id DESC LIMIT 20", (rid,))
            hooks = _rows_dict(conn, "SELECT event_type, status, ok, attempts, error, created_at FROM webhook_deliveries WHERE restaurant_id=? ORDER BY id DESC LIMIT 20", (rid,))
            sh_cols = _columns(conn, "schedule_history")
            extra = ", ".join(c for c in ("generation_seconds", "published_at", "published_by", "review_json") if c in sh_cols)
            schedules = _rows_dict(conn, "SELECT id, generated_at, week_start, week_end, hours_scheduled, hours_budget"
                                         + (", " + extra if extra else "") + " FROM schedule_history "
                                         "WHERE restaurant_id=? ORDER BY id DESC LIMIT 10", (rid,))
            for sch in schedules:
                try:
                    rv = json.loads(sch.pop("review_json", None) or "null") or {}
                except (TypeError, ValueError):
                    rv = {}
                sch["hard_breaches"] = rv.get("hard") or 0
            notes = _rows_dict(conn, "SELECT id, employee_name, notes, created_at FROM staff_notes WHERE restaurant_id=? ORDER BY id DESC", (rid,))
            sources = _rows_dict(conn, "SELECT source, provider, last_attempt_at, last_ok_at, first_failed_at, last_error, "
                                       "error_class, consecutive_failures, next_retry_at, data_through FROM source_health "
                                       "WHERE restaurant_id=? ORDER BY source", (rid,))
        finally:
            conn.close()
        r = get_restaurant(rid)
    bad_email = ("failed", "bounced", "complained")
    errors = ([{"kind": "integration", "label": i["label"], "error": i["error"], "at": i.get("error_since") or i.get("last_success")} for i in rec["integrations"] if i["error"]]
              + [{"kind": "email", "label": f"{e['email_type']} · {e['status']}", "error": e["error"], "at": _iso_z(e["sent_at"], "UTC")} for e in emails if e["status"] in bad_email]
              + [{"kind": "push", "label": p["alert_type"], "error": p["error"], "at": _iso_z(p["created_at"], "UTC")} for p in pushes if not p["ok"]]
              + [{"kind": "post", "label": f"{p['platform']} · {p['topic'] or p['content_type']}", "error": p["error"], "at": _iso_z(p["scheduled_for"])} for p in posts if p["status"] == "failed"]
              + [{"kind": "job", "label": j["job"], "error": j["error"], "at": _iso_z(j["created_at"], "UTC")} for j in jobs]
              + [{"kind": "ai", "label": f"{a['action']} · {a['model']}", "error": a["error"], "at": _iso_z(a["created_at"], "UTC")} for a in ai_failed]
              + [{"kind": "webhook", "label": h["event_type"], "error": h["error"], "at": _iso_z(h["created_at"], "UTC")} for h in hooks if not h["ok"]])
    errors.sort(key=lambda e: e.get("at") or "", reverse=True)
    profile = ({"neighborhood": r.neighborhood, "vibe": r.vibe, "known_for": r.known_for, "timezone": r.timezone,
                "voice_notes": r.voice_notes, "pos_system": r.pos_system, "google_place_id": r.google_place_id,
                "yelp_business_id": r.yelp_business_id, "labor_target_pct": r.labor_target_pct,
                "week_start_day": r.week_start_day,
                "food_cost_target": r.food_cost_target, "digest_day": r.digest_day,
                "internal_notes": r.internal_notes} if r else {})
    # The client's own failures stay `errors` (what the console reads); the
    # queries that failed ride as `query_errors`.
    return {**_payload_meta(meta, bucket), "ok": True, "client": rec, "siblings": siblings, "profile": profile,
            "ai": {"by_action": ai_by_action, "recent": ai_recent, "daily": ai_daily, "failed": ai_failed},
            "events": events, "emails": emails, "pushes": pushes, "devices": devices, "alerts": alerts,
            "activity": [{"type": a["event_type"], "data": a["event_data"], "at": _iso_z(a["created_at"])} for a in acts],
            "logins": logins, "sessions": sessions, "jobs": jobs, "job_runs": runs, "ai_quality": quality,
            "scheduled_posts": posts, "webhook_deliveries": hooks, "schedules": schedules, "staff_notes": notes,
            "data_sources": sources, "errors": errors[:60], "issues_resolved": rec.get("issues_resolved") or [],
            # The automatic alert-storm cap in force, or None (E's #92) —
            # the client page read the fleet's messaging health for it.
            "storm_cap": rec.get("storm_cap"),
            **_client_messaging(rid),
            "timeline_url": f"/admin/api/client/{rid}/timeline"}


def _client_messaging(rid):
    """E's per-restaurant messaging reads for the client page: every
    suppressed address its mail goes to, with the role it plays there and
    whether it is an operator address (#45, #101); its texts (last four
    digits only) and their outcomes over the week (#14)."""
    out = {"suppressions": [], "sms": [], "sms_stats": None}
    path = _current_db_path()
    try:
        import models as _m_msg
        out["suppressions"] = _m_msg.suppressions_for_restaurant(rid, db_path=path)
        for row in out["suppressions"]:
            row["operator"] = _m_msg.is_operator_address(row.get("email"))
    except Exception as e:
        _note_failure("suppressions_for_restaurant", e)
    try:
        import notify as _n_msg
        out["sms"] = _n_msg.sms_log_rows(restaurant_id=rid, limit=40, db_path=path)
        out["sms_stats"] = _n_msg.sms_stats(hours=24 * 7, restaurant_id=rid, db_path=path)
    except Exception as e:
        _note_failure("sms_log", e)
    return out


def _client_job_rows(conn, rid, d):
    """(job failures, job runs, AI-quality findings) for one restaurant — by
    job_failures.restaurant_id / job_runs.restaurant_id (workstream D) when
    the columns exist; before that, the context text matched exactly
    ('restaurant_id=5' or 'rid=5', never 'rid=50'), never by name (#67)."""
    rx = re.compile(r"\b(?:restaurant_id|rid)=%d\b" % int(rid))
    jf_cols = _columns(conn, "job_failures")
    kind_col = ", kind" if "kind" in jf_cols else ""
    if "restaurant_id" in jf_cols:
        rows = _rows_dict(conn, f"SELECT job, error, context, created_at{kind_col} FROM job_failures "
                                "WHERE restaurant_id=? ORDER BY id DESC LIMIT 80", (rid,))
    else:
        rows = [j for j in _rows_dict(conn, "SELECT job, error, context, created_at FROM job_failures "
                                            "WHERE context LIKE ? OR context LIKE ? ORDER BY id DESC LIMIT 400",
                                      (f"%restaurant_id={rid}%", f"%rid={rid}%"))
                if rx.search(j.get("context") or "")][:80]
    has_kind = "kind" in jf_cols
    jobs = [j for j in rows if _job_failure_kind(j, has_kind) == "job"][:40]
    quality = [dict(j, source="job_failures") for j in rows if _job_failure_kind(j, has_kind) == "ai_quality"]
    # Guard and validation findings, dropped lines and served fallbacks are
    # ai_quality_events rows since fix round G (#58) — never job_failures.
    # Shaped like a failure row (job = the surface, error = the detail) so
    # every reader of this list keeps working.
    for e in _rows_dict(conn, "SELECT surface, kind, action, detail, n, call_id, created_at FROM ai_quality_events "
                              "WHERE restaurant_id=? ORDER BY id DESC LIMIT 40", (rid,), optional=True):
        quality.append({"job": e["surface"], "error": e["detail"], "created_at": e["created_at"], "kind": "ai_quality",
                        "finding": e["kind"], "action": e["action"], "n": e["n"], "call_id": e["call_id"],
                        "source": "ai_quality_events"})
    quality.sort(key=lambda q: str(q.get("created_at") or ""), reverse=True)
    quality = quality[:40]
    jr_cols = _columns(conn, "job_runs")
    if "restaurant_id" in jr_cols:
        runs = _rows_dict(conn, "SELECT job, started_at, finished_at, duration_ms, ok, error FROM job_runs "
                                "WHERE restaurant_id=? ORDER BY id DESC LIMIT 20", (rid,))
    else:
        runs = [x for x in _rows_dict(conn, "SELECT job, started_at, finished_at, duration_ms, ok, error, context "
                                            "FROM job_runs WHERE context LIKE ? OR context LIKE ? "
                                            "ORDER BY id DESC LIMIT 200", (f"%restaurant_id={rid}%", f"%rid={rid}%"))
                if rx.search(x.get("context") or "")][:20]
    return jobs, runs, quality


def _fill_days(rows, key, days, value_key=None, count_key="n", sum_key=None, zone="UTC", pred=None):
    """[{day, count_key, sum_key?}] for every Chicago calendar day in the
    window, zero-filled — an empty day is a bar of zero, not a missing bar
    (FIGURES-23)."""
    today = _now_ct().date()
    buckets = {(today - timedelta(days=i)).isoformat(): {"day": (today - timedelta(days=i)).isoformat(),
                                                          count_key: 0, **({sum_key: 0.0} if sum_key else {})}
               for i in range(days - 1, -1, -1)}
    for row in rows:
        at = _parse_utc(row.get(key), zone)
        if at is None:
            continue
        b = buckets.get(_ct(at).date().isoformat())
        if b is None or (pred and not pred(row)):
            continue
        b[count_key] += 1
        if sum_key:
            b[sum_key] = round(b[sum_key] + float(row.get(value_key) or 0), 4)
    return list(buckets.values())


def integrations():
    with _collecting() as bucket:
        recs, _d, meta = _records_cached()
        rows = []
        for r in recs:
            for i in r["integrations"]:
                if not i["in_use"] and i["key"] != "google_business":
                    continue
                age = _age_days(i.get("last_success"), "UTC")
                rows.append({"restaurant_id": r["id"], "restaurant": r["name"], "brand": r["brand"], "location_name": r["location_name"],
                             "segment": r["segment"],
                             "integration": i["key"], "label": i["label"], "state": i["state"], "auth": i["auth"],
                             "last_success": i.get("last_success"), "error": i["error"],
                             "error_since": i.get("error_since"), "revoked_at": i.get("revoked_at"),
                             # The per-location list's POS reading (CA3 F6), carried to the
                             # fleet list too so both say the same thing.
                             "sync_state": i.get("sync_state"), "age_days": i.get("age_days"),
                             # A POS row reads pos_health's state — the registry's one
                             # POS rule (re-audit B6#5) — not its own 3-day cut.
                             "freshness": (("never" if i["state"] == "connected" and age is None else
                                            ("stale" if (i.get("sync_state") == "stale" if i.get("sync_state")
                                                         else (age is not None and age > 3)) else "fresh"))
                                           if i["state"] != "off" else "—"),
                             "health": r["health"]})
        systemic = {}
        for row in rows:
            if row["state"] == "error" and row["segment"] == "customer":
                systemic.setdefault(row["label"], 0)
                systemic[row["label"]] += 1
        conn = get_conn()
        try:
            inbound = _webhook_health(conn)
        finally:
            conn.close()
    return {"ok": True, "rows": rows, "systemic": [{"label": k, "failing": v} for k, v in systemic.items() if v >= 2],
            "webhooks_inbound": inbound, **_payload_meta(meta, bucket)}


def _utc_since(days):
    """A UTC 'YYYY-MM-DD HH:MM:SS' `days` back — the zone ai_usage and every
    AI table stamp in (SQLite's datetime('now')). ai_ops read these windows
    in server-local time, so "today" on the AI page and the budget's own UTC
    day disagreed wherever the server was not on UTC (AIOPS-20)."""
    from datetime import timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _utc_today():
    from datetime import timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_month():
    from datetime import timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-01")


# A row's outcome, for rows written before the column existed (their status
# was ok or error).
_OUTCOME_SQL = ("COALESCE(outcome, CASE WHEN COALESCE(status,'ok')='ok' THEN 'ok' "
                "WHEN status='blocked' THEN 'blocked' ELSE 'error' END)")
_VENDOR_SQL = ("COALESCE(vendor, CASE WHEN COALESCE(model,'') LIKE 'google-places%' THEN 'google_places' "
               "WHEN COALESCE(model,'') LIKE 'perplexity%' OR COALESCE(model,'') LIKE 'sonar%' THEN 'perplexity' "
               "ELSE 'anthropic' END)")
# A blocked row stands for every refusal coalesced into it (attempts).
_WEIGHT_SQL = f"(CASE WHEN {_OUTCOME_SQL}='blocked' THEN COALESCE(attempts,1) ELSE 1 END)"
_PROVIDER_LABELS = {"anthropic": "Claude", "perplexity": "Perplexity", "google_places": "Google Places"}
_OUTCOME_KEYS = ("ok", "error", "refused", "truncated", "unparseable", "blocked")


def _latency_by(conn, since, restaurant_id=None):
    """{(action, model): (p50, p95)} over calls that reached a provider."""
    from ai_utils import _percentile
    sql = (f"SELECT action, model, latency_ms FROM ai_usage WHERE created_at >= ? AND latency_ms IS NOT NULL "
           f"AND {_OUTCOME_SQL} <> 'blocked'")
    args = [since]
    if restaurant_id is not None:
        sql += " AND restaurant_id=?"
        args.append(restaurant_id)
    by = {}
    for r in _rows_dict(conn, sql, args):
        by.setdefault((r["action"], r["model"]), []).append(r["latency_ms"])
    return {k: (_percentile(v, 50), _percentile(v, 95)) for k, v in by.items()}


def _by_action(conn, since, restaurant_id=None):
    """Spend, calls, outcomes and p50/p95 latency by action and model —
    calls are calls that reached a provider; refused-before-sending ones are
    `blocked`, counted apart."""
    where, args = "WHERE created_at >= ?", [since]
    if restaurant_id is not None:
        where += " AND restaurant_id=?"
        args.append(restaurant_id)
    outcome_cols = ", ".join(
        "SUM(CASE WHEN %s='%s' THEN %s ELSE 0 END) AS n_%s"
        % (_OUTCOME_SQL, o, "COALESCE(attempts,1)" if o == "blocked" else "1", o) for o in _OUTCOME_KEYS)
    rows = _rows_dict(conn, f"""
        SELECT action, model, {_VENDOR_SQL} AS vendor,
               SUM(CASE WHEN {_OUTCOME_SQL} <> 'blocked' THEN 1 ELSE 0 END) AS calls,
               {outcome_cols},
               ROUND(COALESCE(SUM(cost_usd),0), 4) AS cost,
               ROUND(COALESCE(SUM(CASE WHEN "trigger"='admin' THEN cost_usd ELSE 0 END),0), 4) AS admin_cost,
               ROUND(AVG(CASE WHEN {_OUTCOME_SQL} <> 'blocked' THEN input_tokens + output_tokens END)) AS avg_tokens,
               MAX(created_at) AS last_at
        FROM ai_usage {where} GROUP BY action, model ORDER BY cost DESC""", args)
    lat = _latency_by(conn, since, restaurant_id)
    for r in rows:
        r["outcomes"] = {o: int(r.pop(f"n_{o}") or 0) for o in _OUTCOME_KEYS}
        r["p50_ms"], r["p95_ms"] = lat.get((r["action"], r["model"]), (None, None))
    return rows


def _budget_limits():
    import ai_utils as _ai
    return {"paid": {"day": _ai.AI_DAILY_BUDGET_USD, "month": _ai.AI_MONTHLY_BUDGET_USD},
            "trial": {"day": _ai.AI_TRIAL_DAILY_BUDGET_USD, "month": _ai.AI_TRIAL_MONTHLY_BUDGET_USD},
            "unpaid": {"day": _ai.AI_UNPAID_DAILY_BUDGET_USD, "month": _ai.AI_UNPAID_MONTHLY_BUDGET_USD},
            "places": {"day": _ai.AI_PLACES_DAILY_BUDGET_USD, "month": _ai.AI_PLACES_MONTHLY_BUDGET_USD},
            "global_month": _ai.global_monthly_budget(), "warn_pct": _ai.AI_BUDGET_WARN_PCT}


def budget_watch(conn=None):
    """Every restaurant at or past AI_BUDGET_WARN_PCT of one of its own
    ceilings today or this month — AI (by its tier) or Google Places — in
    one grouped read, never a per-restaurant loop (#122). Each row: {restaurant_id,
    name, tier, scope (ai_day | ai_month | places_day | places_month),
    spend, budget, pct, over}. The console's "warn at 80%"."""
    import ai_utils as _ai
    own = conn is None
    conn = conn or get_conn()
    try:
        today, month = _utc_today() + " 00:00:00", _utc_month() + " 00:00:00"
        rows = _rows_dict(conn, f"""
            SELECT a.restaurant_id, r.name, r.billing_status, COALESCE(r.is_demo, 0) AS is_demo,
                   SUM(CASE WHEN a.created_at >= ? AND {_ai._AI_ROW_SQL.replace('vendor', 'a.vendor').replace('model', 'a.model')}
                            AND COALESCE(a."trigger",'') <> 'admin' THEN a.cost_usd ELSE 0 END) AS ai_day,
                   SUM(CASE WHEN {_ai._AI_ROW_SQL.replace('vendor', 'a.vendor').replace('model', 'a.model')}
                            AND COALESCE(a."trigger",'') <> 'admin' THEN a.cost_usd ELSE 0 END) AS ai_month,
                   SUM(CASE WHEN a.created_at >= ? AND {_ai._PLACES_ROW_SQL.replace('vendor', 'a.vendor').replace('model', 'a.model')}
                            THEN a.cost_usd ELSE 0 END) AS places_day,
                   SUM(CASE WHEN {_ai._PLACES_ROW_SQL.replace('vendor', 'a.vendor').replace('model', 'a.model')}
                            THEN a.cost_usd ELSE 0 END) AS places_month
            FROM ai_usage a JOIN restaurants r ON r.id = a.restaurant_id
            WHERE a.created_at >= ? GROUP BY a.restaurant_id""", (today, today, month))
    finally:
        if own:
            conn.close()
    out = []
    for r in rows:
        status = (r.get("billing_status") or "").strip().lower()
        tier = ("paid" if status in _ai._PAID_BILLING_STATES else
                "trial" if status in _ai._TRIAL_BILLING_STATES and not r.get("is_demo") else "unpaid")
        daily, monthly = _ai._tier_budgets(tier)
        for scope, spend, budget in (("ai_day", r["ai_day"], daily), ("ai_month", r["ai_month"], monthly),
                                     ("places_day", r["places_day"], _ai.AI_PLACES_DAILY_BUDGET_USD),
                                     ("places_month", r["places_month"], _ai.AI_PLACES_MONTHLY_BUDGET_USD)):
            if not budget:
                continue
            pct = round(100.0 * float(spend or 0) / budget, 1)
            if pct >= _ai.AI_BUDGET_WARN_PCT:
                out.append({"restaurant_id": r["restaurant_id"], "name": r["name"], "tier": tier, "scope": scope,
                            "spend": round(float(spend or 0), 4), "budget": budget, "pct": pct,
                            "over": pct >= 100})
    out.sort(key=lambda x: -x["pct"])
    return out


# Anomaly thresholds (#140). A cost anomaly: a restaurant's day against the
# median of its own previous 14 days. A rate anomaly: one action's calls in a
# day against that action's own 14-day median for the restaurant. The 570
# geocodes a month (about 19 a day, from a baseline of zero) tripped neither
# of the old count rules.
ANOMALY_BASELINE_DAYS = 14
ANOMALY_COST_MULTIPLE = 3.0
ANOMALY_COST_FLOOR_USD = 1.0
ANOMALY_RATE_MULTIPLE = 4.0
ANOMALY_RATE_FLOOR = 15


def ai_anomalies(days=7, conn=None):
    """[{kind, restaurant_id, restaurant, detail, day, ...}] over the last
    `days` UTC days: `spike` (a week 4x the one before), `loop` (200+ calls
    of one action in a day), `cost` (a day over ANOMALY_COST_MULTIPLE x the
    restaurant's own trailing median and over the floor) and `rate` (an
    action's day over ANOMALY_RATE_MULTIPLE x its own trailing median and
    over the floor)."""
    from statistics import median
    own = conn is None
    conn = conn or get_conn()
    try:
        since = _utc_since(days + ANOMALY_BASELINE_DAYS)
        daily_cost = _rows_dict(conn, "SELECT restaurant_id, substr(created_at,1,10) AS day, SUM(cost_usd) AS cost "
                                      "FROM ai_usage WHERE created_at >= ? AND restaurant_id IS NOT NULL "
                                      "GROUP BY restaurant_id, day", (since,))
        daily_rate = _rows_dict(conn, f"SELECT restaurant_id, action, substr(created_at,1,10) AS day, COUNT(*) AS n "
                                      f"FROM ai_usage WHERE created_at >= ? AND restaurant_id IS NOT NULL "
                                      f"AND {_OUTCOME_SQL} <> 'blocked' GROUP BY restaurant_id, action, day", (since,))
        week, prev = _utc_since(7), _utc_since(14)
        wk = {r["restaurant_id"]: r["n"] for r in _rows_dict(conn, "SELECT restaurant_id, COUNT(*) AS n FROM ai_usage WHERE created_at >= ? GROUP BY restaurant_id", (week,))}
        pv = {r["restaurant_id"]: r["n"] for r in _rows_dict(conn, "SELECT restaurant_id, COUNT(*) AS n FROM ai_usage WHERE created_at >= ? AND created_at < ? GROUP BY restaurant_id", (prev, week))}
        names = {r["id"]: r["name"] for r in _rows_dict(conn, "SELECT id, name FROM restaurants")}
    finally:
        if own:
            conn.close()
    from datetime import timezone
    first_day = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    out = [{"kind": "spike", "restaurant_id": rid, "restaurant": names.get(rid),
            "detail": f"{n} calls this week vs {pv.get(rid, 0)} the week before"}
           for rid, n in wk.items() if rid is not None and n >= 40 and pv.get(rid, 0) and n > 4 * pv.get(rid, 0)]

    def _series(rows, key, value):
        by = {}
        for r in rows:
            by.setdefault(key(r), {})[r["day"]] = float(r[value] or 0)
        return by

    for rid, series in _series(daily_cost, lambda r: r["restaurant_id"], "cost").items():
        days_sorted = sorted(series)
        for day in [d for d in days_sorted if d >= first_day]:
            base_days = [d for d in days_sorted if d < day][-ANOMALY_BASELINE_DAYS:]
            base = median([series[d] for d in base_days]) if base_days else 0.0
            cost = series[day]
            if cost > ANOMALY_COST_FLOOR_USD and cost > ANOMALY_COST_MULTIPLE * base:
                out.append({"kind": "cost", "restaurant_id": rid, "restaurant": names.get(rid), "day": day,
                            "cost": round(cost, 2), "baseline": round(base, 2),
                            "detail": f"${cost:.2f} of AI and data-API spend on {day} against a "
                                      f"${base:.2f}/day median"})
    for (rid, action), series in _series(daily_rate, lambda r: (r["restaurant_id"], r["action"]), "n").items():
        days_sorted = sorted(series)
        for day in [d for d in days_sorted if d >= first_day]:
            base_days = [d for d in days_sorted if d < day][-ANOMALY_BASELINE_DAYS:]
            # A day with no calls is a zero in the baseline, not a gap.
            span = max(1, min(ANOMALY_BASELINE_DAYS, len(base_days)))
            base = median([series[d] for d in base_days] + [0.0] * (span - len(base_days))) if base_days else 0.0
            n = series[day]
            if n >= 200:
                out.append({"kind": "loop", "restaurant_id": rid, "restaurant": names.get(rid), "day": day,
                            "action": action, "calls": int(n), "detail": f"{action} ran {int(n)}× on {day}"})
            elif n >= ANOMALY_RATE_FLOOR and n > ANOMALY_RATE_MULTIPLE * base:
                out.append({"kind": "rate", "restaurant_id": rid, "restaurant": names.get(rid), "day": day,
                            "action": action, "calls": int(n), "baseline": base,
                            "detail": f"{action} ran {int(n)}× on {day} against a median of {base:g} a day"})
    return out


_BUDGET_SCOPE_WORDS = {"ai_day": "today's AI budget", "ai_month": "this month's AI budget",
                       "places_day": "today's Google Places budget",
                       "places_month": "this month's Google Places budget"}


def ai_budget_issues(watch=None):
    """budget_watch as issue rows in _issues_for's shape (key, title,
    severity, detail, action), for the client page and Overview (#122): a
    warning at AI_BUDGET_WARN_PCT of a ceiling, critical once it is reached
    (the client's AI or Google lookups are paused until it resets). Keys are
    "<rid>:ai_budget:<scope>". The integration wave splices these into
    _issues_for in place of the old "$25 in 30 days" rule."""
    out = []
    for w in (budget_watch() if watch is None else watch):
        over = w["over"]
        words = _BUDGET_SCOPE_WORDS.get(w["scope"], w["scope"])
        out.append({"key": f"{w['restaurant_id']}:ai_budget:{w['scope']}", "restaurant_id": w["restaurant_id"],
                    "title": (f"Paused: {words} is spent" if over else f"{int(w['pct'])}% of {words} used"),
                    "detail": f"${w['spend']:.2f} of ${w['budget']:.2f} ({w['tier']} ceiling)",
                    "severity": "critical" if over else "warning", "severity_rank": 2 if over else 1,
                    "action": "Open AI ops", "action_route": None})
    return out


def ai_anomaly_issues(anomalies=None):
    """ai_anomalies as platform issue rows for Overview (#140): cost and
    rate anomalies and loops are warnings, keyed by kind, restaurant, action
    and day so a new occurrence is a new issue."""
    out = []
    for a in (ai_anomalies(days=2) if anomalies is None else anomalies):
        if a["kind"] not in ("cost", "rate", "loop"):
            continue
        out.append({"key": f"ai_anomaly:{a['kind']}:{a['restaurant_id']}:{a.get('action') or ''}:{a.get('day') or ''}",
                    "restaurant_id": a["restaurant_id"], "restaurant": a.get("restaurant"),
                    "title": {"cost": "AI spend well above its usual day", "rate": "An AI action running far more "
                              "often than usual", "loop": "An AI action is looping"}[a["kind"]],
                    "detail": a["detail"], "severity": "critical" if a["kind"] == "loop" else "warning",
                    "severity_rank": 2 if a["kind"] == "loop" else 1, "action": "Open AI ops", "action_route": None})
    return out


def ai_ops(days=30):
    """The AI page (Operations → AI): spend, calls and every outcome by
    vendor, action and client; blocked calls by reason; p50/p95 latency by
    workflow; the ceilings (AI by tier, Places, global) and who is near
    one; the breakers and the last hour's error rate; anomalies; the
    quality summary. Windows are UTC, the zone the ledger and the budget
    ceilings use (AIOPS-20)."""
    import ai_utils as _ai
    conn = get_conn()
    since = _utc_since(days)
    today = _utc_today()
    month = _utc_month()
    calls_sql = f"SUM(CASE WHEN {_OUTCOME_SQL} <> 'blocked' THEN 1 ELSE 0 END)"
    totals = _one_dict(conn, f"SELECT {calls_sql} AS calls, ROUND(COALESCE(SUM(cost_usd),0),2) AS cost, COALESCE(SUM(input_tokens),0) AS tin, COALESCE(SUM(output_tokens),0) AS tout, "
                             f"ROUND(COALESCE(SUM(CASE WHEN {_VENDOR_SQL}='google_places' THEN cost_usd ELSE 0 END),0),2) AS data_api_cost, "
                             f"ROUND(COALESCE(SUM(CASE WHEN {_VENDOR_SQL}<>'google_places' THEN cost_usd ELSE 0 END),0),2) AS ai_cost, "
                             f"SUM(CASE WHEN {_OUTCOME_SQL}='blocked' THEN COALESCE(attempts,1) ELSE 0 END) AS blocked "
                             f"FROM ai_usage WHERE created_at >= ?", (since,)) or {}
    t_today = _one_dict(conn, f"SELECT {calls_sql} AS calls, ROUND(COALESCE(SUM(cost_usd),0),2) AS cost FROM ai_usage WHERE created_at >= ?", (today,)) or {}
    t_month = _one_dict(conn, f"SELECT {calls_sql} AS calls, ROUND(COALESCE(SUM(cost_usd),0),2) AS cost FROM ai_usage WHERE created_at >= ?", (month,)) or {}
    by_action = _by_action(conn, since)
    by_client = _rows_dict(conn, f"SELECT a.restaurant_id, r.name, r.location_group, SUM(CASE WHEN {_OUTCOME_SQL} <> 'blocked' THEN 1 ELSE 0 END) AS calls, "
                                 f"ROUND(SUM(a.cost_usd),4) AS cost, "
                                 f"ROUND(SUM(CASE WHEN {_VENDOR_SQL}='google_places' THEN a.cost_usd ELSE 0 END),4) AS data_api_cost, "
                                 f"SUM(CASE WHEN {_OUTCOME_SQL} NOT IN ('ok','blocked') THEN 1 ELSE 0 END) AS not_ok "
                                 f"FROM ai_usage a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE a.created_at >= ? GROUP BY a.restaurant_id ORDER BY cost DESC", (since,))
    daily = _rows_dict(conn, f"SELECT substr(created_at,1,10) AS day, {calls_sql} AS calls, ROUND(SUM(cost_usd),4) AS cost, "
                             f"ROUND(SUM(CASE WHEN {_VENDOR_SQL}='google_places' THEN cost_usd ELSE 0 END),4) AS data_api_cost "
                             f"FROM ai_usage WHERE created_at >= ? GROUP BY day ORDER BY day", (since,))
    by_vendor = _rows_dict(conn, f"SELECT {_VENDOR_SQL} AS vendor, {calls_sql} AS calls, ROUND(COALESCE(SUM(cost_usd),0),4) AS cost, "
                                 f"SUM(CASE WHEN {_OUTCOME_SQL}='error' THEN 1 ELSE 0 END) AS errors, "
                                 f"SUM(CASE WHEN {_OUTCOME_SQL}='blocked' THEN COALESCE(attempts,1) ELSE 0 END) AS blocked "
                                 f"FROM ai_usage WHERE created_at >= ? GROUP BY 1 ORDER BY cost DESC", (since,))
    outcomes = {o: 0 for o in _OUTCOME_KEYS}
    for r in _rows_dict(conn, f"SELECT {_OUTCOME_SQL} AS outcome, SUM({_WEIGHT_SQL}) AS n FROM ai_usage WHERE created_at >= ? GROUP BY 1", (since,)):
        outcomes[r["outcome"] if r["outcome"] in outcomes else "error"] += int(r["n"] or 0)
    blocked = _rows_dict(conn, f"SELECT COALESCE(reason,'unknown') AS reason, {_VENDOR_SQL} AS vendor, SUM(COALESCE(attempts,1)) AS n, "
                               f"MAX(created_at) AS last_at FROM ai_usage WHERE created_at >= ? AND {_OUTCOME_SQL}='blocked' "
                               f"GROUP BY 1, 2 ORDER BY n DESC", (since,))
    rate_hits = _rows_dict(conn, "SELECT bucket, SUM(hits) AS hits FROM ai_rate_hits WHERE day >= date(?) GROUP BY bucket ORDER BY hits DESC", (since,))
    # The latest error per group, not the alphabetically greatest (AIOPS-20).
    failures = _rows_dict(conn, f"""SELECT g.job, g.model, g.outcome, g.n, g.last_at, b.error AS sample FROM (
                                       SELECT action AS job, model, {_OUTCOME_SQL} AS outcome, COUNT(*) AS n,
                                              MAX(created_at) AS last_at, MAX(id) AS last_id
                                       FROM ai_usage WHERE created_at >= ? AND {_OUTCOME_SQL} NOT IN ('ok','blocked')
                                       GROUP BY action, model, outcome) g
                                   JOIN ai_usage b ON b.id = g.last_id ORDER BY g.n DESC""", (since,))
    failed_total = _one_dict(conn, f"SELECT COUNT(*) AS n, SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS n_24h FROM ai_usage WHERE created_at >= ? AND {_OUTCOME_SQL}='error'", (_utc_since(1), since)) or {}
    recent = _rows_dict(conn, f"SELECT a.id, a.restaurant_id, r.name, a.action, a.model, {_VENDOR_SQL} AS vendor, a.input_tokens, a.output_tokens, a.cost_usd, a.created_at, "
                              f"COALESCE(a.status,'ok') AS status, {_OUTCOME_SQL} AS outcome, a.stop_reason, a.reason, a.attempts, a.latency_ms, a.\"trigger\" AS \"trigger\", a.call_id, a.correlation_id, a.error "
                              f"FROM ai_usage a LEFT JOIN restaurants r ON r.id=a.restaurant_id ORDER BY a.id DESC LIMIT 80")
    recent_failed = _rows_dict(conn, f"SELECT a.id, a.restaurant_id, r.name, a.action, a.model, a.created_at, {_OUTCOME_SQL} AS outcome, a.reason, a.error, a.call_id "
                                     f"FROM ai_usage a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE {_OUTCOME_SQL} NOT IN ('ok','blocked') ORDER BY a.id DESC LIMIT 40")
    watch = budget_watch(conn)
    anomalies = ai_anomalies(days=min(days, 14), conn=conn)
    quality = _rows_dict(conn, "SELECT surface, kind, SUM(n) AS n, MAX(created_at) AS last_at FROM ai_quality_events "
                               "WHERE created_at >= ? GROUP BY surface, kind ORDER BY n DESC", (since,))
    conn.close()
    by_provider = [{"provider": _PROVIDER_LABELS.get(v["vendor"], v["vendor"]), "calls": v["calls"], "cost": v["cost"]}
                   for v in by_vendor]
    for v in by_vendor:
        v["label"] = _ai.VENDOR_LABELS.get(v["vendor"], v["vendor"])
        v["kind"] = "data" if v["vendor"] == "google_places" else "ai"
    # Spend against the ceilings that actually stop calls (ai_utils), so the
    # page shows how close the account is rather than only what it has spent.
    try:
        budget = _ai.ai_budget_status()
        budget["places_month"] = _ai.places_budget_status()["month"]
        budget["limits"] = _budget_limits()
    except Exception:
        budget = {}
    try:
        health = _ai.ai_health()
    except Exception:
        health = {}
    # Every UTC day of the window, zero where nothing ran — a gap was a
    # missing bar the chart joined across (UI-2 request 5).
    by_day = {r["day"]: r for r in daily}
    from datetime import timezone as _tz_daily
    first = (datetime.now(_tz_daily.utc) - timedelta(days=days - 1)).date()
    daily = [by_day.get(day) or {"day": day, "calls": 0, "cost": 0.0, "data_api_cost": 0.0}
             for day in ((first + timedelta(days=i)).isoformat() for i in range(days))]
    return {"ok": True, "days": days, "window_tz": "UTC", "generated_at": _utcnow().strftime(_ZFMT),
            "labels": {"cost_card": "AI & data APIs"},
            "totals": {**totals, "today": t_today, "month": t_month}, "by_action": by_action,
            "by_client": by_client, "daily": daily, "by_provider": by_provider, "by_vendor": by_vendor,
            "outcomes": outcomes, "blocked": blocked, "rate_limit_hits": rate_hits,
            "failures": failures, "recent": recent, "anomalies": anomalies,
            "budget": budget, "budget_watch": watch, "health": health, "quality": quality,
            "failed": {"n": failed_total.get("n") or 0, "n_24h": failed_total.get("n_24h") or 0},
            "recent_failed": recent_failed}


def validation_rates(days=30):
    """Response Validation Layer catch rates (internal only): per surface
    the outputs validated and their verdicts, and per surface × rule how
    many outputs the rule fired on and that share of the surface's outputs.
    Reads ai_validation_log only — no answer text is stored to read. Also
    each surface's mode NOW, including surfaces with no rows (a surface
    left in shadow is visible, #69), and a daily verdict trend (from the
    rollup past the raw rows' 120 days)."""
    import response_validation as _rv
    conn = get_conn()
    since = _utc_since(days)
    try:
        rows = _rows_dict(conn, "SELECT surface, verdict, rules, mode FROM ai_validation_log WHERE created_at >= ?",
                          (since,))
        trend = _rows_dict(conn, "SELECT substr(created_at,1,10) AS day, surface, verdict, COUNT(*) AS n "
                                 "FROM ai_validation_log WHERE created_at >= ? GROUP BY day, surface, verdict "
                                 "ORDER BY day", (since,))
        if days > 120:
            first_raw = (_one_dict(conn, "SELECT MIN(substr(created_at,1,10)) AS d FROM ai_validation_log") or {}).get("d")
            for r in _rows_dict(conn, "SELECT day, surface, SUM(n_pass) AS p, SUM(n_caveat) AS c, SUM(n_withhold) AS w, "
                                      "SUM(n_refuse) AS x FROM ai_validation_daily WHERE day >= date(?) AND (? IS NULL OR day < ?) "
                                      "GROUP BY day, surface", (since, first_raw, first_raw)):
                for verdict, key in (("pass", "p"), ("caveat", "c"), ("withhold", "w"), ("refuse", "x")):
                    if r[key]:
                        trend.append({"day": r["day"], "surface": r["surface"], "verdict": verdict, "n": r[key]})
    finally:
        conn.close()
    surfaces = {}
    for r in rows:
        s = surfaces.setdefault(r["surface"], {"surface": r["surface"], "n": 0,
                                               "verdicts": {v: 0 for v in _rv.VERDICTS}, "rules": {}, "modes": {}})
        s["n"] += 1
        if r["verdict"] in s["verdicts"]:
            s["verdicts"][r["verdict"]] += 1
        s["modes"][r["mode"] or "?"] = s["modes"].get(r["mode"] or "?", 0) + 1
        try:
            codes = set(json.loads(r["rules"] or "[]"))
        except (TypeError, ValueError):
            codes = set()
        for code in codes:
            s["rules"][code] = s["rules"].get(code, 0) + 1
    out = []
    for s in sorted(surfaces.values(), key=lambda x: -x["n"]):
        n = s["n"] or 1
        out.append({**s, "caught_pct": round(100.0 * (s["n"] - s["verdicts"].get("pass", 0)) / n, 1),
                    "rules": [{"rule": k, "label": _rv.RULES.get(k, k), "n": v, "pct": round(100.0 * v / n, 1)}
                              for k, v in sorted(s["rules"].items(), key=lambda kv: -kv[1])],
                    "mode_now": _rv.mode_for(s["surface"])})
    modes = [{"surface": sf, "mode": _rv.mode_for(sf)} for sf in _rv.SURFACES]
    return {"ok": True, "days": days, "total": len(rows), "surfaces": out, "version": _rv.VERSION,
            "modes": modes, "shadow": [m["surface"] for m in modes if m["mode"] == "shadow"], "trend": trend}


def ai_quality(days=30, restaurant_id=None):
    """The AI Quality panel (#69): the validation verdict mix and top rules
    by surface with a trend and each surface's mode; the model in force per
    purpose; reply drafts flagged for review and how owners edit them; Ask's
    helpful rate; safety disagreements as a share of reviews analysed; the
    guard and validation findings and fallbacks (ai_quality_events) by
    surface and kind, with a trend; refusals, truncations and unparseable
    replies by action. No answer, draft or guest text is returned."""
    import ai_utils as _ai
    since = _utc_since(days)
    rid_sql, rid_args = ("", []) if restaurant_id is None else (" AND restaurant_id=?", [restaurant_id])
    conn = get_conn()
    try:
        events = _rows_dict(conn, f"SELECT surface, kind, SUM(n) AS n, COUNT(*) AS events, MAX(created_at) AS last_at "
                                  f"FROM ai_quality_events WHERE created_at >= ?{rid_sql} GROUP BY surface, kind "
                                  f"ORDER BY n DESC", [since] + rid_args)
        event_trend = _rows_dict(conn, f"SELECT substr(created_at,1,10) AS day, surface, SUM(n) AS n FROM ai_quality_events "
                                       f"WHERE created_at >= ?{rid_sql} GROUP BY day, surface ORDER BY day",
                                 [since] + rid_args)
        recent_events = _rows_dict(conn, f"SELECT e.id, e.created_at, e.restaurant_id, r.name AS restaurant, e.surface, e.kind, "
                                         f"e.action, e.detail, e.codes, e.n, e.call_id FROM ai_quality_events e "
                                         f"LEFT JOIN restaurants r ON r.id=e.restaurant_id WHERE e.created_at >= ?"
                                         f"{rid_sql.replace('restaurant_id', 'e.restaurant_id')} ORDER BY e.id DESC LIMIT 50",
                                   [since] + rid_args)
        unusable = _rows_dict(conn, f"SELECT action, {_OUTCOME_SQL} AS outcome, COUNT(*) AS n, "
                                    f"SUM(CASE WHEN {_OUTCOME_SQL} <> 'blocked' THEN 1 ELSE 0 END) AS provider_calls "
                                    f"FROM ai_usage WHERE created_at >= ?{rid_sql} "
                                    f"AND {_OUTCOME_SQL} IN ('refused','truncated','unparseable') GROUP BY 1, 2 ORDER BY n DESC",
                              [since] + rid_args)
        calls_by_action = {r["action"]: r["n"] for r in _rows_dict(
            conn, f"SELECT action, COUNT(*) AS n FROM ai_usage WHERE created_at >= ?{rid_sql} AND {_OUTCOME_SQL} <> 'blocked' "
                  f"GROUP BY action", [since] + rid_args)}
        drafts = _one_dict(conn, f"SELECT COUNT(*) AS drafted, SUM(COALESCE(draft_needs_review,0)) AS needs_review, "
                                 f"SUM(COALESCE(regenerate_count,0)) AS regenerations FROM reviews "
                                 f"WHERE deleted_at IS NULL AND draft_response IS NOT NULL AND fetched_at >= ?{rid_sql}",
                           [since] + rid_args) or {}
        reasons = _rows_dict(conn, f"SELECT draft_review_reason AS reason, COUNT(*) AS n FROM reviews WHERE deleted_at IS NULL "
                                   f"AND COALESCE(draft_needs_review,0)=1 AND fetched_at >= ?{rid_sql} "
                                   f"GROUP BY draft_review_reason ORDER BY n DESC LIMIT 10", [since] + rid_args)
        edits = _rows_dict(conn, f"SELECT COALESCE(edit_category,'unrecorded') AS category, COUNT(*) AS n, "
                                 f"ROUND(AVG(edit_distance), 3) AS avg_distance FROM reviews WHERE deleted_at IS NULL "
                                 f"AND approved_at >= ?{rid_sql} GROUP BY 1", [since] + rid_args)
        draft_clients = [] if restaurant_id is not None else _rows_dict(conn, """
            SELECT r.restaurant_id, x.name, COUNT(*) AS drafted,
                   SUM(COALESCE(r.draft_needs_review,0)) AS needs_review,
                   SUM(CASE WHEN r.approved_at >= ? AND r.edit_category IN ('light','heavy','rewrite') THEN 1 ELSE 0 END) AS edited,
                   SUM(CASE WHEN r.approved_at >= ? AND r.edit_category IN ('heavy','rewrite') THEN 1 ELSE 0 END) AS heavy
            FROM reviews r LEFT JOIN restaurants x ON x.id = r.restaurant_id
            WHERE r.deleted_at IS NULL AND r.draft_response IS NOT NULL AND r.fetched_at >= ?
            GROUP BY r.restaurant_id ORDER BY drafted DESC LIMIT 50""", (since, since, since))
        ask = _one_dict(conn, f"SELECT COUNT(*) AS rated, SUM(helpful) AS helpful, "
                              f"SUM(CASE WHEN COALESCE(note,'') <> '' THEN 1 ELSE 0 END) AS notes "
                              f"FROM ask_feedback WHERE created_at >= ?{rid_sql}", [since] + rid_args) or {}
        analysed = (_one_dict(conn, f"SELECT COUNT(*) AS n FROM ai_usage WHERE created_at >= ? AND action='review_analysis' "
                                    f"AND {_OUTCOME_SQL}='ok'{rid_sql}", [since] + rid_args) or {}).get("n") or 0
    finally:
        conn.close()
    disagreements = sum(int(e["n"] or 0) for e in events if e["kind"] == "safety_disagreement")
    # Findings as a RATE (#58): per 100 provider calls of the surface's own
    # ledger action, where the surface has one.
    for e in events:
        actions = {e["surface"]} | set(_ai._SURFACE_ACTIONS.get(e["surface"], ()))
        if e["surface"] == "safety_disagreement":
            actions = {"review_analysis"}
        calls = sum(int(calls_by_action.get(a) or 0) for a in actions)
        e["calls"] = calls
        e["per_100_calls"] = round(100.0 * int(e["n"] or 0) / calls, 1) if calls else None
    drafted = int(drafts.get("drafted") or 0)
    rated = int(ask.get("rated") or 0)
    for u in unusable:
        total = calls_by_action.get(u["action"]) or 0
        u["pct_of_calls"] = round(100.0 * u["n"] / total, 1) if total else None
    models = [{"purpose": p, "env": env, "default": default, "model": _ai.model_for(p),
               "overridden": bool(os.getenv(env)) and os.getenv(env) != default}
              for p, (env, default) in _ai.MODELS.items()]
    val = validation_rates(days) if restaurant_id is None else _client_validation(restaurant_id, since)
    return {"ok": True, "days": days, "window_tz": "UTC", "restaurant_id": restaurant_id,
            "validation": val, "models": models,
            "drafts": {"drafted": drafted, "needs_review": int(drafts.get("needs_review") or 0),
                       "needs_review_pct": round(100.0 * int(drafts.get("needs_review") or 0) / drafted, 1) if drafted else None,
                       "regenerations": int(drafts.get("regenerations") or 0), "review_reasons": reasons,
                       "edit_categories": edits, "by_restaurant": draft_clients},
            "ask": {"rated": rated, "helpful": int(ask.get("helpful") or 0), "notes": int(ask.get("notes") or 0),
                    "helpful_pct": round(100.0 * int(ask.get("helpful") or 0) / rated, 1) if rated else None},
            "safety": {"disagreements": disagreements, "reviews_analysed": int(analysed),
                       "rate_pct": round(100.0 * disagreements / analysed, 2) if analysed else None},
            "events": events, "event_trend": event_trend, "recent_events": recent_events,
            "unusable_outputs": unusable}


def _client_validation(restaurant_id, since):
    conn = get_conn()
    try:
        rows = _rows_dict(conn, "SELECT surface, verdict, COUNT(*) AS n FROM ai_validation_log "
                                "WHERE created_at >= ? AND restaurant_id=? GROUP BY surface, verdict", (since, restaurant_id))
    finally:
        conn.close()
    out = {}
    for r in rows:
        out.setdefault(r["surface"], {})[r["verdict"]] = r["n"]
    return {"by_surface": out}


def ai_client(rid, days=30):
    """One client's AI (#48, #122, #124): spend against its own ceilings
    (AI by tier, and Google Places) with pct, warn and when a pause lifts;
    usage by action with every outcome and p50/p95 latency; blocked calls
    by reason; its recent traced calls; its AI-quality findings; stalled
    reviews and whether the Retry AI action has anything to do."""
    import ai_utils as _ai
    import models as _m
    since = _utc_since(days)
    try:
        ai_budget = _ai.ai_budget_status(rid)
        places_budget = _ai.places_budget_status(rid)
    except Exception:
        ai_budget, places_budget = {}, {}
    conn = get_conn()
    try:
        by_action = _by_action(conn, since, rid)
        blocked = _rows_dict(conn, f"SELECT COALESCE(reason,'unknown') AS reason, {_VENDOR_SQL} AS vendor, "
                                   f"SUM(COALESCE(attempts,1)) AS n, MAX(created_at) AS last_at, MAX(error) AS detail "
                                   f"FROM ai_usage WHERE restaurant_id=? AND created_at >= ? AND {_OUTCOME_SQL}='blocked' "
                                   f"GROUP BY 1, 2 ORDER BY n DESC", (rid, since))
        daily = _rows_dict(conn, f"SELECT substr(created_at,1,10) AS day, SUM(CASE WHEN {_OUTCOME_SQL} <> 'blocked' THEN 1 ELSE 0 END) AS calls, "
                                 f"ROUND(SUM(cost_usd),4) AS cost, "
                                 f"ROUND(SUM(CASE WHEN {_VENDOR_SQL}='google_places' THEN cost_usd ELSE 0 END),4) AS data_api_cost "
                                 f"FROM ai_usage WHERE restaurant_id=? AND created_at >= ? GROUP BY day ORDER BY day", (rid, since))
        calls = _rows_dict(conn, "SELECT call_id, created_at, action, model, \"trigger\" AS \"trigger\", correlation_id, "
                                 "outcome, stop_reason, latency_ms, attempts, input_tokens, output_tokens, "
                                 "prompt_z IS NOT NULL AS text_kept FROM ai_calls WHERE restaurant_id=? "
                                 "ORDER BY created_at DESC, rowid DESC LIMIT 25", (rid,))
    finally:
        conn.close()
    try:
        stalled = _m.count_stalled_reviews(rid)
    except Exception:
        stalled = {"unanalysed": 0, "undrafted": 0}
    warn = [dict(scope=f"{kind}_{scope}", **{k: v for k, v in (b.get(scope) or {}).items()})
            for kind, b in (("ai", ai_budget), ("places", places_budget)) for scope in ("day", "month")
            if (b.get(scope) or {}).get("warn")]
    return {"ok": True, "restaurant_id": rid, "days": days, "window_tz": "UTC",
            "budget": {"ai": ai_budget, "places": places_budget, "tier": ai_budget.get("tier"), "warnings": warn},
            "by_action": by_action, "blocked": blocked, "daily": daily, "recent_calls": calls,
            "quality": ai_quality(days, restaurant_id=rid),
            "stalled_reviews": stalled, "stalled_total": int(stalled.get("unanalysed") or 0) + int(stalled.get("undrafted") or 0)}


def ai_calls(restaurant_id=None, action=None, correlation_id=None, limit=50):
    """Traced calls, newest first (#117) — ids, hashes, outcome and timing;
    no prompt or output text (ai_call_detail has that)."""
    where, args = [], []
    for col, val in (("restaurant_id", restaurant_id), ("action", action), ("correlation_id", correlation_id)):
        if val is not None and val != "":
            where.append(f"c.{col}=?")
            args.append(val)
    sql = ("SELECT c.call_id, c.created_at, c.restaurant_id, r.name AS restaurant, c.action, c.vendor, c.model, "
           "c.\"trigger\" AS \"trigger\", c.actor_user_id, c.correlation_id, c.caller, c.template_hash, c.prompt_hash, "
           "c.request_id, c.stop_reason, c.outcome, c.output_hash, c.input_tokens, c.output_tokens, c.latency_ms, "
           "c.attempts, c.prompt_z IS NOT NULL AS text_kept FROM ai_calls c LEFT JOIN restaurants r ON r.id=c.restaurant_id"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY c.created_at DESC, c.rowid DESC LIMIT ?")
    conn = get_conn()
    try:
        rows = _rows_dict(conn, sql, args + [max(1, min(int(limit or 50), 500))])
    finally:
        conn.close()
    return {"ok": True, "calls": rows}


def ai_call_detail(call_id):
    """One traced call: its prompt and output (guest contact details and
    names redacted, kept for the newest calls only), the ledger row, the
    validation verdicts and quality findings linked to it, and the other
    calls in the same unit of work (an Ask turn's rounds, a run's queries)."""
    import ai_utils as _ai
    call = _ai.read_call(call_id)
    if not call:
        return {"ok": False, "error": "Not found"}
    conn = get_conn()
    try:
        usage = _rows_dict(conn, "SELECT id, created_at, restaurant_id, action, model, vendor, outcome, status, stop_reason, "
                                 "reason, attempts, latency_ms, input_tokens, output_tokens, cache_write_tokens, "
                                 "cache_read_tokens, cost_usd, price_version, request_id, \"trigger\" AS \"trigger\", "
                                 "actor_user_id, correlation_id, error FROM ai_usage WHERE call_id=?", (call_id,))
        validation = _rows_dict(conn, "SELECT id, created_at, surface, verdict, rules, tokens, n_rewrites, n_drops, n_caveats, "
                                      "mode, version FROM ai_validation_log WHERE call_id=?", (call_id,))
        quality = _rows_dict(conn, "SELECT id, created_at, surface, kind, detail, codes, n FROM ai_quality_events "
                                   "WHERE call_id=?", (call_id,))
        related = _rows_dict(conn, "SELECT call_id, created_at, action, outcome, stop_reason, latency_ms FROM ai_calls "
                                   "WHERE correlation_id=? AND call_id<>? ORDER BY created_at", (call.get("correlation_id"), call_id)) \
            if call.get("correlation_id") else []
    finally:
        conn.close()
    return {"ok": True, "call": call, "usage": usage, "validation": validation, "quality": quality, "related": related}


def emails(limit=200):
    """The Email page. email_log.sent_at is UTC; "today" is midnight Central
    (#89). A bounce or a complaint counts against delivery, with 7- and
    30-day rates and their thresholds, and when the last Resend event
    arrived (#59). Every count is over the full set — the suppression list
    and its total are no longer capped at 50 (#160)."""
    with _collecting() as bucket:
        w = _windows()
        conn = get_conn()
        try:
            rows = _rows_dict(conn, "SELECT e.id, e.restaurant_id, r.name AS restaurant, r.location_group AS brand, e.email_type, e.to_email, e.subject, e.sent_at, e.status, e.error FROM email_log e LEFT JOIN restaurants r ON r.id=e.restaurant_id ORDER BY e.id DESC LIMIT ?", (limit,))
            by_type = _rows_dict(conn, "SELECT email_type, COUNT(*) AS n, "
                                       "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
                                       "SUM(CASE WHEN status='bounced' THEN 1 ELSE 0 END) AS bounced, "
                                       "SUM(CASE WHEN status='complained' THEN 1 ELSE 0 END) AS complained "
                                       "FROM email_log WHERE sent_at >= ? GROUP BY email_type ORDER BY n DESC",
                                 (w["week"],))
            month_rows = _rows_dict(conn, "SELECT sent_at, status FROM email_log WHERE sent_at >= ?", (w["month"],))
            storms = _rows_dict(conn, "SELECT to_email, COUNT(*) AS n FROM email_log WHERE sent_at >= ? "
                                      "GROUP BY to_email HAVING n >= 8 ORDER BY n DESC", (w["today"],))
            sup_cols = _columns(conn, "email_suppressions")
            suppressed = _rows_dict(conn, "SELECT * FROM email_suppressions ORDER BY rowid DESC LIMIT 50")
            sup_total = (_one_dict(conn, "SELECT COUNT(*) AS n FROM email_suppressions") or {}).get("n") or 0
            sup_by = _rows_dict(conn, "SELECT reason, " + ("COALESCE(scope,'all')" if "scope" in sup_cols else "'all'")
                                      + " AS scope, COUNT(*) AS n FROM email_suppressions GROUP BY 1, 2 ORDER BY n DESC")
            # Whether any of it was worth sending. Everything above counts what went
            # OUT. Read as a floor, not a rate — Apple Mail pre-fetches images (an
            # open nobody performed) and a reader with images off never registers.
            # A bounced or complained send never reached anyone to open it.
            engagement = _rows_dict(conn,
                "SELECT email_type, COUNT(*) AS sent, "
                "SUM(CASE WHEN opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opened, "
                "SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicked "
                "FROM email_log WHERE sent_at >= ? AND status NOT IN ('failed','bounced','complained') "
                "GROUP BY email_type ORDER BY sent DESC", (w["month"],))
            for row in engagement:
                row["open_rate"] = (round(100.0 * (row["opened"] or 0) / row["sent"], 1)
                                    if row["sent"] else None)
            totals = _one_dict(conn, "SELECT COUNT(*) AS n, "
                                     "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
                                     "SUM(CASE WHEN status='bounced' THEN 1 ELSE 0 END) AS bounced, "
                                     "SUM(CASE WHEN status='complained' THEN 1 ELSE 0 END) AS complained, "
                                     "SUM(CASE WHEN status='delivered' THEN 1 ELSE 0 END) AS delivered "
                                     "FROM email_log WHERE sent_at >= ?", (w["today"],)) or {}
            rates = _email_rates(conn, w)
            hooks = _webhook_health(conn)
        finally:
            conn.close()
    today = {k: int(totals.get(k) or 0) for k in ("n", "failed", "bounced", "complained", "delivered")}
    # "Sent today" is what left without an error at our end or Resend's.
    today["sent"] = today["n"] - today["failed"] - today["bounced"] - today["complained"]
    bad = ("failed", "bounced", "complained")
    daily = _fill_days(month_rows, "sent_at", 30)
    fails = _fill_days(month_rows, "sent_at", 30, pred=lambda r: r.get("status") in bad)
    for day, f in zip(daily, fails):
        day["failed"] = f["n"]
    resend = next((p for p in hooks["providers"] if p["provider"] == "resend"), None)
    # An operator address is never suppressed (E, #101): one on the list
    # bounced, and is flagged rather than shown as a live suppression.
    try:
        import models as _m_sup
        for row in suppressed:
            row["operator"] = _m_sup.is_operator_address(row.get("email"))
    except Exception as e:
        _note_failure("operator_addresses", e)
    return {"ok": True, "generated_at": _utcnow().strftime(_ZFMT), "rows": rows, "by_type": by_type, "daily": daily,
            "storms": storms, "suppressed": suppressed, "suppressed_total": sup_total, "suppressed_by": sup_by,
            "today": today, "engagement": engagement, "rates": rates,
            "resend_webhook": resend, "reinstate_route": "/admin/api/suppressions/reinstate",
            "windows": _window_meta(), **_merge_problems(bucket)}


def notifications(limit=200):
    """The Push & alerts page. The opened rate divides opens by pushes that
    were DELIVERED — not by every alert raised on any channel (#160). Storms
    are counted today (Central) and over 7 days, and automatic temporary
    caps are listed beside the manual ones (#92)."""
    with _collecting() as bucket:
        w = _windows()
        conn = get_conn()
        try:
            pushes = _rows_dict(conn, "SELECT p.id, p.restaurant_id, r.name AS restaurant, p.device_token_id, d.user_id, u.username, p.alert_type, p.status, p.ok, p.attempts, p.error, p.created_at FROM push_deliveries p LEFT JOIN restaurants r ON r.id=p.restaurant_id LEFT JOIN device_tokens d ON d.id=p.device_token_id LEFT JOIN users u ON u.id=d.user_id ORDER BY p.id DESC LIMIT ?", (limit,))
            devices = _rows_dict(conn, "SELECT d.id, d.restaurant_id, r.name AS restaurant, u.username, d.environment, d.created_at, d.last_success_at, d.consecutive_failures, d.disabled_reason FROM device_tokens d LEFT JOIN restaurants r ON r.id=d.restaurant_id LEFT JOIN users u ON u.id=d.user_id ORDER BY d.id DESC LIMIT 500")
            devices_total = (_one_dict(conn, "SELECT COUNT(*) AS n FROM device_tokens") or {}).get("n") or 0
            alerts = _rows_dict(conn, "SELECT a.id, a.restaurant_id, r.name AS restaurant, a.alert_type, a.review_id, a.fired_at FROM alert_log a LEFT JOIN restaurants r ON r.id=a.restaurant_id ORDER BY a.id DESC LIMIT ?", (limit,))
            by_type = _rows_dict(conn, "SELECT alert_type, COUNT(*) AS n FROM alert_log WHERE fired_at >= ? GROUP BY alert_type ORDER BY n DESC", (w["week"],))
            storms = _rows_dict(conn, "SELECT a.restaurant_id, r.name AS restaurant, COALESCE(r.alert_max_per_day,0) AS cap, COUNT(*) AS n FROM alert_log a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE a.fired_at >= ? GROUP BY a.restaurant_id HAVING n >= 10 ORDER BY n DESC", (w["today"],))
            # Storm days over a week, bucketed on Central days (the offset of
            # today is used for the whole week — an hour off across a DST
            # change, never a day).
            off = int(_now_ct().utcoffset().total_seconds() // 3600)
            storm_days = _rows_dict(conn, "SELECT restaurant_id, date(fired_at, ?) AS day, COUNT(*) AS n FROM alert_log "
                                          "WHERE fired_at >= ? GROUP BY restaurant_id, day HAVING n >= 10",
                                    (f"{off} hours", w["week"]))
            caps = _rows_dict(conn, "SELECT id AS restaurant_id, name AS restaurant, alert_max_per_day AS cap FROM restaurants WHERE COALESCE(alert_max_per_day,0) > 0 ORDER BY name")
            auto_caps, auto_supported = _auto_caps(conn)
            # E's outboxes (#75): what is queued, in flight, failed.
            try:
                import push as _push_ob
                import webhooks as _wh_ob
                outboxes = {"push": _push_ob.outbox_counts(db_path=_current_db_path()),
                            "webhooks": _wh_ob.outbox_counts(db_path=_current_db_path())}
            except Exception as e:
                outboxes = None
                _note_failure("outboxes", e)
            scheduled = _rows_dict(conn, "SELECT p.id, p.restaurant_id, r.name AS restaurant, p.platform, p.content_type, p.topic, p.scheduled_for, p.status, p.error, p.attempts FROM marketing_scheduled_posts p LEFT JOIN restaurants r ON r.id=p.restaurant_id ORDER BY p.scheduled_for DESC LIMIT 60")
            post_totals = _one_dict(conn, "SELECT SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
                                          "SUM(CASE WHEN status IN ('scheduled','pending','publishing') THEN 1 ELSE 0 END) AS scheduled, "
                                          "COUNT(*) AS n FROM marketing_scheduled_posts") or {}
            today_push = _one_dict(conn, "SELECT SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS sent, SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failed FROM push_deliveries WHERE created_at >= ?", (w["today"],)) or {}
            # Whether any of it was worth sending. Thirty days rather than
            # seven: several of these types fire weekly. The denominator is
            # pushes DELIVERED of that type (a push per device), the numerator
            # the opens recorded for that type — both per device, both push.
            engagement = _rows_dict(conn,
                "SELECT a.alert_type, COUNT(*) AS alerts, "
                "(SELECT COUNT(*) FROM push_deliveries p WHERE p.alert_type=a.alert_type AND p.ok=1 "
                " AND p.created_at >= ?) AS delivered, "
                "(SELECT COUNT(*) FROM notification_opens o WHERE o.alert_type=a.alert_type "
                " AND o.opened_at >= ?) AS opened "
                "FROM alert_log a WHERE a.fired_at >= ? GROUP BY a.alert_type ORDER BY alerts DESC",
                (w["month"], w["month"], w["month"]))
        finally:
            conn.close()
    for row in engagement:
        row["open_rate"] = (round(100.0 * row["opened"] / row["delivered"], 1) if row["delivered"] else None)
    ignored = [r for r in engagement if (r["delivered"] or 0) >= 20 and not r["opened"]]
    return {"ok": True, "generated_at": _utcnow().strftime(_ZFMT), "pushes": pushes, "devices": devices,
            "devices_total": devices_total, "alerts": alerts,
            "by_type": by_type, "storms": storms, "caps": caps,
            "storm_counts": {"today": len(storms), "storm_days_7d": len(storm_days),
                             "restaurants_7d": len({s["restaurant_id"] for s in storm_days})},
            "auto_caps": auto_caps, "auto_caps_supported": auto_supported, "outboxes": outboxes,
            "scheduled_posts": scheduled,
            "posts_failed_total": post_totals.get("failed") or 0,
            "posts_scheduled_total": post_totals.get("scheduled") or 0,
            "today_push": today_push, "engagement": engagement, "ignored": ignored,
            "engagement_basis": "opens ÷ pushes delivered, per device, last 30 days",
            "windows": _window_meta(), **_merge_problems(bucket)}


def billing():
    """The Billing tab — from the Stripe subscription mirror, never a Stripe
    call per customer on a request thread (#37). A single client's live
    Stripe state is billing_live(rid), on its own page."""
    with _collecting() as bucket:
        recs, d, meta = _records_cached()
        rows = []
        for r in recs:
            b = r["billing"]
            sub = b.get("subscription") or {}
            costs = round(float(r["ai"]["cost_30d"] or 0) + float((r.get("sms") or {}).get("cost_30d") or 0), 2)
            revenue = b["monthly"] or 0
            rows.append({"restaurant_id": r["id"], "restaurant": r["name"], "brand": r["brand"],
                         "owner": (r["owner"] or {}).get("username"), "owner_email": r["owner_email"],
                         "stripe_customer_id": b["stripe_customer_id"], "status": b["status"], "tier": b["tier"],
                         "modules": b["modules"], "monthly": b["monthly"], "list_monthly": b["list_monthly"],
                         "billed_monthly": b["billed_monthly"], "committed_monthly": b["committed_monthly"],
                         "mrr_source": b["mrr_source"], "billed_by": b["billed_by"], "covers": b["covers"],
                         "list_mismatch": b["list_mismatch"], "status_mismatch": b["status_mismatch"],
                         "contract_status": b["contract_status"], "envelope_id": b["envelope_id"],
                         "signed_at": b.get("signed_at"), "days_since_signed": b.get("days_since_signed"),
                         "days_in_trial": b.get("days_in_trial"), "converted_at": b.get("converted_at"),
                         "pause_reason": b.get("pause_reason"), "pause_reason_inferred": b.get("pause_reason_inferred"),
                         "paused_until": b.get("paused_until"), "deletion": r.get("deletion"),
                         "created_at": r["created_at"], "is_demo": r["is_demo"], "is_admin_home": r["is_admin_home"],
                         "segment": r["segment"],
                         # The per-account margin (#90): what the account pays a
                         # month against what it cost in AI and SMS over 30 days.
                         "margin": {"revenue_monthly": revenue, "ai_cost_30d": r["ai"]["cost_30d"],
                                    "sms_cost_30d": (r.get("sms") or {}).get("cost_30d"),
                                    "variable_cost_30d": costs, "margin": round(revenue - costs, 2),
                                    "margin_pct": (round(100.0 * (revenue - costs) / revenue, 1) if revenue else None)},
                         # `live` keeps its shape for the console, filled from the mirror.
                         "live": ({"status": sub.get("status"), "amount": sub.get("amount"),
                                   "interval": sub.get("interval"), "trial_end": sub.get("trial_end"),
                                   "current_period_end": sub.get("current_period_end"),
                                   "cancel_at_period_end": sub.get("cancel_at_period_end"),
                                   "canceled_at": sub.get("canceled_at"), "source": "mirror",
                                   "updated_at": sub.get("updated_at")} if sub else None)})
        order = {"past_due": 0, "churned": 1, "canceled": 1, "paused": 1, "trial": 2, "active": 3, "internal": 4}
        rows.sort(key=lambda x: (order.get(x["status"], 2), (x["restaurant"] or "").lower()))
        try:
            import admin_events
            # Payment and contract history only: every admin write is an
            # admin_events row too, and unfiltered they pushed the Stripe and
            # DocuSign events off this list (#53, LIFECYCLE-12).
            events = admin_events.recent(limit=120, sources=("stripe", "docusign"))
        except Exception as e:
            _note_failure("admin_events", e)
            events = []
        totals = _mrr_totals(recs)
    customers = [x for x in rows if x["segment"] == "customer"]
    fixed = vendor_costs_month(_now_ct().strftime("%Y-%m"))
    variable = round(sum(x["margin"]["variable_cost_30d"] for x in rows), 2)
    payers = [x for x in customers if x["monthly"]]
    return {"ok": True, "rows": rows, "events": events, "stripe_live": bool(os.getenv("STRIPE_SECRET_KEY", "")),
            "stripe_error": None, "live_source": "mirror", "mirror_available": bool(d.get("mirror_available")),
            "live_route": "/admin/api/client/<id>/billing/live",
            "mrr": totals["mrr"], "totals": totals,
            "margin": {"mrr": totals["mrr"], "variable_cost_30d": variable, "fixed_cost_month": fixed["total"],
                       "gross_margin": round(totals["mrr"] - variable - fixed["total"], 2),
                       "gross_margin_pct": (round(100.0 * (totals["mrr"] - variable - fixed["total"]) / totals["mrr"], 1)
                                            if totals["mrr"] else None),
                       "arpa": round(totals["mrr"] / len(payers), 2) if payers else None,
                       "fixed_by_vendor": fixed["by_vendor"],
                       "basis": "MRR less 30 days of AI and SMS cost less this month's entered vendor costs"},
            **_payload_meta(meta, bucket)}


# The scheduler's jobs, by the name ops.run_job records them under, so the
# console can show the same rows the scheduler writes and run one on demand.
# Built from the ONE registry (jobs_registry.JOBS): this listed 27 of the
# loop's 55 jobs, and the Jobs page could not show or re-run the rest (#40,
# #56). Every entry keeps the shape the console reads: cadence, what,
# target, sends, plus the job's SLA and bound.
import jobs_registry as _jobs_registry
RUNNABLE_JOBS = {
    name: {"cadence": s["cadence"], "what": s["description"], "label": s.get("label") or name,
           "target": s["target"], "sends": bool(s.get("sends")), "sla_minutes": s.get("sla_minutes"),
           "max_minutes": _jobs_registry.max_minutes(name)}
    for name, s in _jobs_registry.JOBS.items() if s.get("runnable") and s.get("target")
}


# The longest any job may run (a weekly Intel sweep: three hours plus its
# margin). Kept for readers of the old name; "already running" is now each
# job's own bound, and a run a deploy killed is closed at boot rather than
# blocking Run now for four hours (#150).
RUN_NOW_BLOCK_MINUTES = max(_jobs_registry.max_minutes(n) for n in _jobs_registry.JOBS)

LOCAL_SENDS_REFUSED = ("This server is not the production scheduler, so jobs that email, text or push "
                       "people are refused here: it has production's Resend and Twilio keys and a stale "
                       "copy of what has already been sent. Run it from the production console.")


def _audit_run_now(name, actor, outcome):
    """Every Run now lands in admin_events (JOBS-5), not only in job_runs'
    context, which is pruned at 45 days."""
    try:
        import admin_events
        rec = getattr(admin_events, "record_admin_action", None)
        if rec is not None:
            rec(actor, "job.run_now", target=name, result=outcome.get("result", "ok"),
                summary=outcome.get("summary"))
        else:
            admin_events.record("admin", "job.run_now", summary=f"{outcome.get('summary')} (by {actor})")
    except Exception:
        pass


def run_job_now(name, actor):
    """Run one scheduled job on demand, recorded in job_runs exactly like a
    scheduled run — context says who asked.

    * A job that emails, texts or pushes people is refused unless this
      process may schedule (scheduler.scheduling_allowed, #9): a laptop has
      production's Resend and Twilio keys and a stale copy of the claims, so
      Run now on a local backend sent real owners a second digest.
    * It is HANDED to the scheduler process (ops.request_job_run, #153):
      the loop takes the request at its next tick and runs it under the
      lease with the pulse, never beside a live run of the same job (#64).
      It used to run on a thread of whichever web process served the
      click, with no claim, racing the scheduled run. Where no scheduler
      runs here (a laptop), a non-sending job still runs on a thread here.
    * Refused while a live run of it exists (its pulse fresh, in any
      process) — each job's own bound, not a flat four hours.

    The dict carries `status` (409 when refused), so the route can answer
    with a sentence the console shows."""
    import threading
    spec = RUNNABLE_JOBS.get(name)
    if not spec:
        return {"ok": False, "error": "Unknown job"}
    import ops as _ops_now
    import scheduler as _sched_now
    if _ops_now.is_running(name):
        return {"ok": False, "status": 409, "error": f"{name} is already running in this process"}
    running = _ops_now.running_elsewhere(name)
    if running:
        return {"ok": False, "status": 409, "error": f"{name} is already running (started {running['started_at']})"}
    allowed = _sched_now.scheduling_allowed()
    if spec.get("sends") and not allowed:
        out = {"ok": False, "status": 409, "error": LOCAL_SENDS_REFUSED}
        _audit_run_now(name, actor, {"result": "refused", "summary": f"Run now {name}: refused on a local backend"})
        return out
    ctx = f"manual by {actor}"
    if allowed:
        req_id, why = _ops_now.request_job_run(name, actor)
        if req_id is None:
            return {"ok": False, "status": 409, "error": why}
        _audit_run_now(name, actor, {"summary": f"Run now {name} queued for the scheduler (request {req_id})"})
        return {"ok": True, "job": name, "context": ctx, "queued": True, "request_id": req_id,
                "message": "Queued — the scheduler starts it on its next tick (within five minutes)."}
    # No scheduler in this process's deployment (a laptop): a job that sends
    # nothing runs here, on a thread, through the same run_job.
    import importlib
    mod, fn_name = spec["target"]
    try:
        fn = getattr(importlib.import_module(mod), fn_name)
    except Exception as e:
        return {"ok": False, "error": f"Could not load {mod}.{fn_name}: {e}"}
    kwargs = _jobs_registry.run_kwargs(name)

    try:
        import ai_utils as _ai_now
        attribution = dict(_ai_now.attribution_for_thread(), trigger="admin")
    except Exception:
        _ai_now, attribution = None, {}

    def _go():
        try:
            # An admin's run: its model calls are the admin's, not the
            # scheduler's or the client's ceiling (#148).
            if _ai_now is not None:
                with _ai_now.ai_context(**attribution):
                    _ops_now.run_job(name, fn, context=ctx, **kwargs)
            else:
                _ops_now.run_job(name, fn, context=ctx, **kwargs)
        except Exception:
            pass  # run_job already recorded the failure
    threading.Thread(target=_go, name=f"admin-run-{name}", daemon=True).start()
    _audit_run_now(name, actor, {"summary": f"Run now {name} started on this server"})
    return {"ok": True, "job": name, "context": ctx, "queued": False}


def set_alert_cap(rid, max_per_day, actor):
    """The storm brake: at most N alerts a day for one restaurant, 0 = off.
    Enforced in notify._check_dnd; this only sets the number."""
    try:
        n = int(max_per_day)
    except (TypeError, ValueError):
        return {"ok": False, "error": "max_per_day must be a number"}
    if n < 0 or n > 500:
        return {"ok": False, "error": "max_per_day must be between 0 and 500"}
    from models import update_restaurant
    if not get_restaurant(rid):
        return {"ok": False, "error": "Not found"}
    update_restaurant(rid, {"alert_max_per_day": n})
    invalidate_fleet_cache()
    try:
        import admin_events
        admin_events.record("admin", "alert_cap.set", restaurant_id=rid, amount=n,
                            summary=f"Alert cap set to {n or 'off'} by {actor}")
    except Exception:
        pass
    return {"ok": True, "restaurant_id": rid, "max_per_day": n}


# Runs kept per job on the Jobs page (a window function, per job): the page
# read the last 120 runs of EVERY job together, about two and a half hours
# at ~45 runs an hour, so a daily job showed empty squares (#40).
JOB_HISTORY_RUNS = 14
_RUN_STATES = {1: "ok", 2: "partial", 0: "failed"}


def _job_rows(conn, overdue):
    """One row per registry job: its history (last JOB_HISTORY_RUNS runs),
    its last success (ok 1 or partial 2 — "never ran" used to mean "never
    ran clean"), a running or stuck run against the job's OWN bound (four
    thresholds — 30, 90, 120, 240 minutes — used to disagree, #150), and a
    state of ok | partial | failed | running | stuck | overdue | never:
    partial is its own state, not green (#40)."""
    import ops as _ops_j
    hist = {}
    try:
        for r in _rows_dict(conn,
                            "SELECT id, job, started_at, finished_at, duration_ms, ok, error, context, result_json, "
                            "request_id, pulse_at FROM ("
                            "  SELECT *, ROW_NUMBER() OVER (PARTITION BY job ORDER BY started_at DESC, id DESC) AS rn "
                            "  FROM job_runs WHERE started_at >= datetime('now', '-45 days')"
                            ") WHERE rn <= ? ORDER BY job, started_at DESC", (JOB_HISTORY_RUNS,)):
            r["state"] = ("running" if r["finished_at"] is None else _RUN_STATES.get(r["ok"], "failed"))
            hist.setdefault(r["job"], []).append(r)
    except Exception as e:
        log.warning("job history unreadable: %s", e)
    stats = {r["job"]: r for r in _rows_dict(
        conn, "SELECT job, MAX(CASE WHEN ok IN (1,2) THEN finished_at END) AS last_ok, "
              "ROUND(AVG(CASE WHEN ok IN (1,2) THEN duration_ms END)) AS avg_ms, COUNT(*) AS runs, "
              "SUM(ok=0) AS failed, SUM(ok=2) AS partial FROM job_runs "
              "WHERE started_at >= datetime('now', '-7 days') GROUP BY job")}
    late = {j["job"]: j for j in overdue}
    out = []
    for name, spec in _jobs_registry.JOBS.items():
        runs = hist.get(name, [])
        bound = _jobs_registry.max_minutes(name)
        open_run = next((r for r in runs if r["finished_at"] is None), None)
        stuck = False
        if open_run:
            age = _age_hours(open_run["started_at"])
            stuck = age is not None and age * 60 > bound
        last_done = next((r for r in runs if r["finished_at"] is not None), None)
        if open_run:
            state = "stuck" if stuck else "running"
        elif name in late:
            state = "overdue"
        elif last_done:
            state = last_done["state"]
        else:
            state = "never"
        st = stats.get(name) or {}
        out.append({
            "job": name, "label": spec.get("label") or name, "cadence": spec["cadence"],
            "what": spec["description"], "sends": bool(spec.get("sends")), "runnable": bool(spec.get("runnable")),
            "sla_minutes": spec.get("sla_minutes"), "stuck_after_minutes": bound, "lane": spec.get("lane"),
            "retry": bool(spec.get("retry")), "state": state, "last_ok_at": st.get("last_ok"),
            "avg_ms": st.get("avg_ms"), "runs_7d": st.get("runs") or 0, "failed_7d": st.get("failed") or 0,
            "partial_7d": st.get("partial") or 0, "running_since": open_run["started_at"] if open_run else None,
            "overdue": late.get(name), "history": runs,
        })
    return out


def jobs():
    import ops as _ops
    conn = get_conn()
    # Windows in SQL, against the UTC stamps job_runs and job_failures carry:
    # Python's local now() was right only on a UTC host (JOBS-5).
    failures = _rows_dict(conn, "SELECT id, job, error, context, created_at, restaurant_id, kind FROM job_failures "
                                "ORDER BY id DESC LIMIT 100") if _has_cols(conn, "job_failures", ("restaurant_id", "kind")) \
        else _rows_dict(conn, "SELECT id, job, error, context, created_at FROM job_failures ORDER BY id DESC LIMIT 100")
    # One group per job AND kind (#58): an AI output finding or a console
    # request error is never folded into a job's failure count — the page
    # guessed each group's kind from the newest hundred rows.
    if _has_cols(conn, "job_failures", ("kind",)):
        grouped = _rows_dict(conn, "SELECT f.job, COALESCE(f.kind,'job') AS kind, COUNT(*) AS n, MAX(f.created_at) AS last_at, "
                                   "MIN(f.created_at) AS first_at, (SELECT g.error FROM job_failures g WHERE g.job=f.job "
                                   "AND COALESCE(g.kind,'job')=COALESCE(f.kind,'job') ORDER BY g.id DESC LIMIT 1) AS sample "
                                   "FROM job_failures f WHERE f.created_at >= datetime('now', '-7 days') "
                                   "GROUP BY f.job, COALESCE(f.kind,'job') ORDER BY n DESC")
    else:
        grouped = _rows_dict(conn, "SELECT f.job, COUNT(*) AS n, MAX(f.created_at) AS last_at, MIN(f.created_at) AS first_at, "
                                   "(SELECT g.error FROM job_failures g WHERE g.job=f.job ORDER BY g.id DESC LIMIT 1) AS sample "
                                   "FROM job_failures f WHERE f.created_at >= datetime('now', '-7 days') GROUP BY f.job ORDER BY n DESC")
        for g in grouped:
            g["kind"] = _job_failure_kind({"job": g["job"], "error": g["sample"]}, False)
    runs = _rows_dict(conn, "SELECT id, job, started_at, finished_at, duration_ms, ok, error, context FROM job_runs ORDER BY id DESC LIMIT 120")
    last_ok = _rows_dict(conn, "SELECT job, MAX(finished_at) AS last_ok, ROUND(AVG(duration_ms)) AS avg_ms, COUNT(*) AS runs "
                               "FROM job_runs WHERE ok IN (1, 2) AND started_at >= datetime('now', '-7 days') GROUP BY job")
    overdue = _ops.jobs_overdue()
    job_rows = _job_rows(conn, overdue)
    # "Stuck" is each job's own bound now, not a flat 30 minutes.
    stuck = [{"id": None, "job": j["job"], "started_at": j["running_since"], "context": None,
              "bound_minutes": j["stuck_after_minutes"]} for j in job_rows if j["state"] == "stuck"]
    try:
        posts = _rows_dict(conn, "SELECT p.id, r.name AS restaurant, p.platform, p.topic, p.scheduled_for, p.status, "
                                 "p.attempts, p.error FROM marketing_scheduled_posts p LEFT JOIN restaurants r ON r.id=p.restaurant_id "
                                 "WHERE p.status IN ('scheduled','publishing','failed') ORDER BY p.scheduled_for LIMIT 40")
    except Exception:
        posts = []
    try:
        missed = _rows_dict(conn, "SELECT m.job, m.restaurant_id, r.name AS restaurant, m.local_date, m.detail, m.created_at "
                                  "FROM missed_windows m LEFT JOIN restaurants r ON r.id=m.restaurant_id "
                                  "WHERE m.created_at >= datetime('now', '-7 days') ORDER BY m.id DESC LIMIT 50")
    except Exception:
        missed = []
    conn.close()
    import status_manager
    try:
        heartbeat = status_manager.scheduler_state(_current_db_path())
    except Exception:
        heartbeat = {"beat_age_minutes": None, "state": "unknown"}
    hb = heartbeat.get("beat_age_minutes")
    heartbeat["stale_after_minutes"] = _jobs_registry.HEARTBEAT_STALE_MINUTES
    # In-flight async jobs. These used to be read out of two module-level
    # dicts, so the page only ever showed the jobs belonging to whichever
    # worker served the request; ops.async_jobs is one table every worker
    # writes to.
    inflight = []
    try:
        for j in _ops.inflight_async_jobs(limit=20):
            inflight.append({"kind": j["kind"], "id": j["job_id"], "status": j["status"],
                             "restaurant_id": j.get("restaurant_id"), "started": j.get("created_at")})
    except Exception:
        pass
    try:
        import scheduler as _sched
        local_refused = not _sched.scheduling_allowed()
    except Exception:
        local_refused = True
    try:
        from dsr import pipeline as _dsr_pipeline
        dsr_missing = _dsr_pipeline.nights_missing()
    except Exception:
        dsr_missing = []
    schedule = [{"job": j["job"], "cadence": j["cadence"], "runnable": j["runnable"], "sends": j["sends"],
                 "what": j["what"]} for j in job_rows]
    return {"ok": True, "generated_at": _utcnow().strftime(_ZFMT), "heartbeat_minutes": hb, "failures": failures,
            "grouped": grouped, "runs": runs, "last_ok": last_ok,
            "stuck": stuck, "scheduled_posts": posts, "inflight": inflight, "schedule": schedule,
            # Fix round D (#40, #121, #1, #27, #153, #131, #17): one row per
            # registry job with its own history and state, the scheduler's
            # full liveness, the lease, the backup, run-now requests, the
            # last operator page, windows missed, DSR nights missing.
            "jobs": job_rows, "heartbeat": heartbeat, "lease": _ops.scheduler_lease_holder(),
            "backup": _ops.backup_status(), "storage": _ops.storage_trend(), "requests": _ops.job_requests(),
            "operator_alert": _ops.last_operator_alert(), "missed_windows": missed, "dsr_missing": dsr_missing,
            "jobs_overdue": overdue, "local_sends_refused": local_refused,
            "local_sends_refused_reason": LOCAL_SENDS_REFUSED if local_refused else None,
            "history_runs": JOB_HISTORY_RUNS}


def _has_cols(conn, table, cols):
    try:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        return all(c in have for c in cols)
    except Exception:
        return False


def issues():
    """The Support queue: the open customer and platform issues (the first
    ISSUES_IN_OVERVIEW, as the console has always read them) with the counts
    of the FULL set per segment. issues_page() pages and filters them all."""
    ov = overview()
    # The fleet payload meta every other read carries (C's #36, #47, #89).
    meta = {k: ov.get(k) for k in ("generated_at", "cached", "age_seconds", "errors", "query_errors",
                                   "unavailable", "windows")}
    meta["errors"], meta["unavailable"] = meta["errors"] or [], meta["unavailable"] or []
    meta["query_errors"] = meta["query_errors"] or meta["errors"]
    return {"ok": True, "issues": ov["issues"], "issues_total": ov["issues_total"],
            "issue_counts": ov["issue_counts"], **meta}


def _current_issue(key):
    """The open issue with this key as the console last saw it (the memo, if
    there is one, else a build), or None."""
    m = _fleet_state["memo"] if _fleet_state["memo"] and _fleet_state["memo"]["key"] == _memo_key() else None
    if m:
        recs, d = m["recs"], m["d"]
    else:
        recs, d, _meta = _records_cached()
    if _RID_KEY.match(key):
        rid = int(key.split(":", 1)[0])
        rec = next((r for r in recs if r["id"] == rid), None)
        pool = (rec["issues"] + rec["issues_resolved"]) if rec else []
    else:
        pool, _facts = _platform_issues(recs, d)
        pool = pool + list(_facts.get("platform_resolved") or [])
    return next((i for i in pool if i["key"] == key), None)


def resolve_issue(key, note, actor, occurrence_at=None):
    """Mark ONE occurrence of an issue resolved (#24). The occurrence is the
    one the console sent (`occurrence_at`) or, failing that, the one open
    now; a newer occurrence reopens the issue. Keys that clear themselves
    (the scheduler heartbeat, the 5xx rate) or that are closed another way
    (a deletion request) are refused, and say why."""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "Missing key"}
    if key in UNRESOLVABLE:
        return {"ok": False, "resolvable": False, "error": UNRESOLVABLE[key]}
    kind = _issue_category(key)
    if kind in _UNRESOLVABLE_KINDS:
        return {"ok": False, "resolvable": False, "error": _UNRESOLVABLE_KINDS[kind]}
    occ = _parse_utc(occurrence_at, "UTC") if occurrence_at else None
    if occ is None:
        try:
            cur = _current_issue(key)
        except AdminBusy:
            cur = None
        if cur is not None and not cur.get("resolvable", True):
            return {"ok": False, "resolvable": False, "error": "This issue clears itself; it can't be resolved."}
        occ = _parse_utc((cur or {}).get("occurrence_at"), "UTC")
    occ_s = _utc_stamp(occ) if occ else None
    conn = get_conn()
    try:
        if "occurrence_at" in _columns(conn, "admin_issue_resolutions"):
            conn.execute("INSERT OR REPLACE INTO admin_issue_resolutions (key, resolved_at, note, actor, occurrence_at) "
                         "VALUES (?, datetime('now'), ?, ?, ?)", (key, note, actor, occ_s))
        else:
            conn.execute("INSERT OR REPLACE INTO admin_issue_resolutions (key, resolved_at, note, actor) "
                         "VALUES (?, datetime('now'), ?, ?)", (key, note, actor))
        try:
            conn.execute("INSERT INTO admin_issue_resolution_history (key, action, occurrence_at, note, actor) "
                         "VALUES (?, 'resolved', ?, ?, ?)", (key, occ_s, note, actor))
        except Exception as e:
            _note_failure("admin_issue_resolution_history", e, optional=True)
        conn.commit()
    finally:
        conn.close()
    invalidate_fleet_cache()
    return {"ok": True, "key": key, "occurrence_at": _iso_z(occ_s, "UTC") if occ_s else None,
            "scope": "occurrence" if occ_s else "condition",
            "note": ("Resolved for this occurrence — it reopens if it happens again." if occ_s
                     else "Resolved while this condition lasts — it reopens once it clears and comes back.")}


def unresolve_issue(key, actor=None):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM admin_issue_resolutions WHERE key=?", (key,))
        try:
            conn.execute("INSERT INTO admin_issue_resolution_history (key, action, actor) VALUES (?, 'reopened', ?)",
                         (key, actor))
        except Exception as e:
            _note_failure("admin_issue_resolution_history", e, optional=True)
        conn.commit()
    finally:
        conn.close()
    invalidate_fleet_cache()
    return {"ok": True}


def resolved_issues(limit=500):
    """Every resolution in force (a cleared condition retires its own, so
    this no longer grows without bound) and the newest history."""
    conn = get_conn()
    try:
        cols = _columns(conn, "admin_issue_resolutions")
        occ = ", occurrence_at" if "occurrence_at" in cols else ""
        rows = _rows_dict(conn, f"SELECT key, resolved_at, note, actor{occ} FROM admin_issue_resolutions "
                                "ORDER BY resolved_at DESC LIMIT ?", (int(limit),))
        total = (_one_dict(conn, "SELECT COUNT(*) AS n FROM admin_issue_resolutions") or {}).get("n") or 0
        history = _rows_dict(conn, "SELECT key, action, occurrence_at, note, actor, created_at FROM "
                                   "admin_issue_resolution_history ORDER BY id DESC LIMIT 100", optional=True)
    finally:
        conn.close()
    for r in rows:
        r["occurrence_at"] = _iso_z(r.get("occurrence_at"), "UTC") if r.get("occurrence_at") else None
    return {"ok": True, "resolved": rows, "total": total, "history": history}


_ACTIVITY_LABELS = {
    "review_approved": "Approved a reply", "review_undo": "Undid an approval", "reviews_bulk_approved": "Published replies in bulk",
    "auto_approve_changed": "Changed auto-approve", "admin_settings_update": "Admin updated settings", "sessions_revoked_others": "Signed out other devices",
    "marketing_emails_changed": "Changed email preferences", "profile_updated": "Updated profile", "two_fa_enabled": "Turned on 2FA",
    "two_fa_disabled": "Turned off 2FA", "team_member_invited": "Invited a teammate", "team_member_revoked": "Removed a teammate",
    "data_exported": "Exported data", "recovery_email_set": "Set a recovery email", "password_changed": "Changed password",
    "email_changed": "Changed email", "backup_codes_regenerated": "Regenerated backup codes", "tab_view": None,
}
# login_history events say what KIND of sign-in it was — a staff PIN sign-in,
# a failed PIN or a lockout is not "Signed in" (#54).
_LOGIN_LABELS = {"login": "Signed in", "staff_login": "Staff signed in with a PIN", "pin_failed": "Staff PIN failed",
                 "pin_locked": "Staff PIN locked out", "login_failed": "Sign-in failed"}
_LOGIN_TONE = {"pin_failed": "warn", "pin_locked": "bad", "login_failed": "warn"}


def _login_label(event, device_type=None):
    base = _LOGIN_LABELS.get(event or "login", (event or "login").replace("_", " ").capitalize())
    if (event or "login") == "login":
        return f"{base} ({'view-as' if device_type == 'admin-view-as' else (device_type or 'web')})"
    return base


def activity(limit=60, d=None):
    """What changed in the last 14 days, newest first — every stamp read in
    its own column's zone and sorted as an instant (the list was sorted as
    strings across UTC and Chicago stamps). Audit rows are the audit trail's,
    not this list's."""
    with _collecting() as bucket:
        now = _utcnow()
        since = _utc_stamp(now - timedelta(days=14))
        since_ct = (_now_ct() - timedelta(days=15)).strftime("%Y-%m-%d %H:%M:%S")
        conn = get_conn()
        ev = []
        try:
            for a in _rows_dict(conn, "SELECT a.restaurant_id, r.name, a.event_type, a.event_data, a.created_at FROM activity_log a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE julianday(a.created_at) >= julianday(?) ORDER BY a.id DESC LIMIT 120", (since_ct,)):
                label = _ACTIVITY_LABELS.get(a["event_type"], a["event_type"].replace("_", " ").capitalize() if a["event_type"] else None)
                if label is None:
                    continue
                ev.append({"at": _iso_z(a["created_at"]), "restaurant_id": a["restaurant_id"], "restaurant": a["name"], "kind": "account", "label": label, "tone": "neutral"})
            for l in _rows_dict(conn, "SELECT l.restaurant_id, r.name, l.event, l.device_type, l.created_at FROM login_history l LEFT JOIN restaurants r ON r.id=l.restaurant_id WHERE l.created_at >= ? ORDER BY l.id DESC LIMIT 60", (since,)):
                ev.append({"at": _iso_z(l["created_at"], "UTC"), "restaurant_id": l["restaurant_id"], "restaurant": l["name"], "kind": "login",
                           "event": l.get("event") or "login", "label": _login_label(l.get("event"), l.get("device_type")),
                           "tone": _LOGIN_TONE.get(l.get("event"), "neutral")})
            for r in _rows_dict(conn, "SELECT id, name, created_at FROM restaurants WHERE julianday(created_at) >= julianday(?) ORDER BY id DESC", (since_ct,)):
                ev.append({"at": _iso_z(r["created_at"]), "restaurant_id": r["id"], "restaurant": r["name"], "kind": "signup", "label": "Restaurant created", "tone": "good"})
            for e in _rows_dict(conn, "SELECT e.restaurant_id, r.name, e.email_type, e.sent_at, e.status, e.error FROM email_log e LEFT JOIN restaurants r ON r.id=e.restaurant_id WHERE e.status IN ('failed','bounced','complained') AND e.sent_at >= ? ORDER BY e.id DESC LIMIT 40", (since,)):
                verb = {"bounced": "Email bounced", "complained": "Email marked as spam"}.get(e["status"], "Email failed")
                ev.append({"at": _iso_z(e["sent_at"], "UTC"), "restaurant_id": e["restaurant_id"], "restaurant": e["name"], "kind": "email", "label": f"{verb} · {e['email_type']}", "tone": "bad", "detail": e["error"]})
            for p in _rows_dict(conn, "SELECT p.restaurant_id, r.name, p.alert_type, p.created_at, p.error FROM push_deliveries p LEFT JOIN restaurants r ON r.id=p.restaurant_id WHERE p.ok=0 AND p.created_at >= ? ORDER BY p.id DESC LIMIT 40", (since,)):
                ev.append({"at": _iso_z(p["created_at"], "UTC"), "restaurant_id": p["restaurant_id"], "restaurant": p["name"], "kind": "push", "label": f"Push failed · {p['alert_type']}", "tone": "bad", "detail": p["error"]})
            jf_cols = _columns(conn, "job_failures")
            jf_extra = "".join(f", {c}" for c in ("kind", "restaurant_id") if c in jf_cols)
            for j in _rows_dict(conn, f"SELECT job, error, created_at{jf_extra} FROM job_failures WHERE created_at >= ? ORDER BY id DESC LIMIT 60", (since,)):
                if _job_failure_kind(j, "kind" in jf_cols) != "job":
                    continue
                ev.append({"at": _iso_z(j["created_at"], "UTC"), "restaurant_id": j.get("restaurant_id"), "restaurant": "Platform", "kind": "job", "label": f"Job failed · {j['job']}", "tone": "bad", "detail": (j["error"] or "")[:140]})
            for e in _rows_dict(conn, "SELECT e.restaurant_id, r.name, e.source, e.event_type, e.summary, e.created_at FROM admin_events e LEFT JOIN restaurants r ON r.id=e.restaurant_id WHERE e.created_at >= ? AND e.source <> 'audit' ORDER BY e.id DESC LIMIT 40", (since,)):
                bad = any(x in e["event_type"] for x in ("failed", "deleted", "canceled", "past_due"))
                ev.append({"at": _iso_z(e["created_at"], "UTC"), "restaurant_id": e["restaurant_id"], "restaurant": e["name"] or "Unmatched customer", "kind": e["source"],
                           "label": f"{e['source'].capitalize()} · {e['summary'] or e['event_type']}", "tone": "bad" if bad else ("good" if e["event_type"] in ("invoice.paid", "contract.signed") else "neutral")})
            for a in _rows_dict(conn, "SELECT a.restaurant_id, r.name, a.alert_type, a.fired_at FROM alert_log a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE a.fired_at >= ? AND a.alert_type IN ('1star','health','labor_over') ORDER BY a.id DESC LIMIT 30", (since,)):
                ev.append({"at": _iso_z(a["fired_at"], "UTC"), "restaurant_id": a["restaurant_id"], "restaurant": a["name"], "kind": "alert", "label": f"Alert fired · {a['alert_type']}", "tone": "warn"})
        finally:
            conn.close()
    floor = now - timedelta(days=14)
    ev = [e for e in ev if e["at"] and (_parse_utc(e["at"], "UTC") or floor) >= floor]
    ev.sort(key=lambda e: e["at"] or "", reverse=True)
    return {"ok": True, "events": ev[:limit], **_merge_problems(bucket)}


_DIGITS = re.compile(r"\D+")


def _digits_sql(col):
    """SQL for a column with phone punctuation removed."""
    expr = col
    for ch in ("-", " ", "(", ")", "+", ".", "/"):
        expr = f"REPLACE({expr}, '{ch}', '')"
    return expr


def search(q):
    """Restaurants and logins by name, email, phone (digits only, the last
    ten), Google Place ID, DocuSign envelope, Stripe customer or subscription,
    and POS identifiers — each result says what matched (#63). A number is
    matched EXACTLY against restaurant and login ids, labelled apart."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"ok": True, "results": []}
    like = f"%{q}%"
    digits = _DIGITS.sub("", q)
    res = []
    with _collecting() as bucket:
        conn = get_conn()
        try:
            rcols = _columns(conn, "restaurants")
            exact_fields = [("google_place_id", "Google Place ID"), ("docusign_envelope_id", "DocuSign envelope"),
                            ("stripe_customer_id", "Stripe customer"), ("toast_restaurant_guid", "Toast restaurant"),
                            ("square_location_id", "Square location"), ("clover_merchant_id", "Clover merchant"),
                            ("rpower_store_mid", "RPOWER store"), ("gmb_location_id", "Business Profile location")]
            exact_fields = [(c, l) for c, l in exact_fields if c in rcols]
            seen = set()

            def add_loc(r, match, how):
                if r["id"] in seen:         # one row per restaurant: its strongest match
                    return
                seen.add(r["id"])
                res.append({"type": "location", "id": r["id"], "title": r["name"], "match": match, "match_how": how,
                            "sub": " · ".join(x for x in [r.get("location_group"), r.get("location_name"), r.get("neighborhood")] if x) or r.get("owner_email")})

            base = "SELECT id, name, location_group, location_name, neighborhood, owner_email FROM restaurants"
            if digits and digits == q.strip() and digits.isdigit():
                for r in _rows_dict(conn, f"{base} WHERE id = ?", (int(digits),)):
                    add_loc(r, "restaurant id", "exact")
            for c, label in exact_fields:
                for r in _rows_dict(conn, f"{base} WHERE {c} = ? OR ({c} LIKE ? AND LENGTH(?) >= 6) LIMIT 10",
                                    (q, f"{q}%", q)):
                    add_loc(r, label, "exact")
            if len(digits) >= 7:
                tail = digits[-10:]
                for r in _rows_dict(conn, f"{base} WHERE {_digits_sql('owner_phone')} LIKE ? LIMIT 10", (f"%{tail}",)):
                    add_loc(r, "owner phone", "digits")
                for r in _rows_dict(conn, "SELECT r.id, r.name, r.location_group, r.location_name, r.neighborhood, "
                                          f"r.owner_email FROM alert_contacts c JOIN restaurants r ON r.id=c.restaurant_id "
                                          f"WHERE {_digits_sql('c.phone')} LIKE ? LIMIT 10", (f"%{tail}",)):
                    add_loc(r, "alert contact phone", "digits")
            for r in _rows_dict(conn, f"{base} WHERE name LIKE ? OR location_group LIKE ? OR location_name LIKE ? "
                                      "OR neighborhood LIKE ? OR owner_email LIKE ? OR owner_name LIKE ? LIMIT 20",
                                (like, like, like, like, like, like)):
                add_loc(r, "name or owner", "contains")
            subs = _rows_dict(conn, "SELECT s.restaurant_id AS id, r.name, r.location_group, r.location_name, "
                                    "r.neighborhood, r.owner_email FROM stripe_subscriptions s JOIN restaurants r "
                                    "ON r.id=s.restaurant_id WHERE s.subscription_id = ? LIMIT 5", (q,),
                              optional=True)
            for r in subs:
                add_loc(r, "Stripe subscription", "exact")
            if digits and digits == q.strip() and digits.isdigit():
                for u in _rows_dict(conn, "SELECT u.id, u.username, u.email, u.role, u.restaurant_id, r.name FROM users u LEFT JOIN restaurants r ON r.id=u.restaurant_id WHERE u.id = ?", (int(digits),)):
                    res.append({"type": "owner" if u["role"] in ("client", "owner") else "login", "id": u["restaurant_id"], "user_id": u["id"], "title": u["username"], "match": "login id", "match_how": "exact", "sub": f"{u['email']} · {u['name'] or 'no restaurant'} · {u['role']}"})
            for u in _rows_dict(conn, "SELECT u.id, u.username, u.email, u.role, u.restaurant_id, r.name FROM users u LEFT JOIN restaurants r ON r.id=u.restaurant_id WHERE u.username LIKE ? OR u.email LIKE ? LIMIT 20", (like, like)):
                res.append({"type": "owner" if u["role"] in ("client", "owner") else "login", "id": u["restaurant_id"], "user_id": u["id"], "title": u["username"], "match": "login name or email", "match_how": "contains", "sub": f"{u['email']} · {u['name'] or 'no restaurant'} · {u['role']}"})
            for g in _rows_dict(conn, "SELECT location_group AS g, COUNT(*) AS n FROM restaurants WHERE location_group LIKE ? GROUP BY location_group LIMIT 10", (like,)):
                res.append({"type": "brand", "id": None, "title": g["g"], "match": "brand", "match_how": "contains", "sub": f"{g['n']} locations"})
        finally:
            conn.close()
    return {"ok": True, "results": res[:40], **_merge_problems(bucket)}


# ── recommendation acceptance (internal only) ────────────────────────────────
#
# Read from rec_ledger (rec_instances + rec_events): one row per episode of a
# recommendation. Admin-only — no owner ever sees these rates. An episode
# nobody answered counts in every denominator as ignored: dropping the
# ignored ones would report what people did with the recommendations they
# chose to touch, and every rate would read high.

RAS_WEIGHTS = {"opened": 0.15, "accepted": 0.40, "completed": 0.25, "outcome": 0.20}
RAS_MIN_N = 20            # below this, a score is noise: rates only, no RAS
_Z90 = 1.645


def _wilson(k, n, z=_Z90):
    """90% Wilson interval for k of n, as (low, high) shares; None when n=0."""
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return (round(max(0.0, (c - m) / d), 3), round(min(1.0, (c + m) / d), 3))


def _ras_block(eps):
    """Rates and the Recommendation Acceptance Score for a set of episodes."""
    n = len(eps)
    if not n:
        return {"n": 0, "ras": None}
    k = {x: sum(1 for e in eps if e.get(x)) for x in ("opened", "evidence", "accepted", "completed", "implemented",
                                                       "dismissed", "snoozed", "ignored", "outcome")}
    took = sum(1 for e in eps if e["accepted"] or e["completed"] or e.get("implemented"))
    # "Improved" only among what was taken: a verdict on an episode nobody
    # took is not the recommendation working, and counting it let the
    # outcome rate pass 100%. Verdicts are read through
    # rec_learning.learned_verdict (a disowned, conditions-changed,
    # informational, faded or reversed result is never a win), and the rate
    # is improved ÷ MEASURED — taken with a clear verdict — not ÷ taken
    # (confidence audit E13, CA2 #10).
    _taken = [e for e in eps if e["accepted"] or e["completed"] or e.get("implemented")]
    k["improved"] = sum(1 for e in _taken if e["improved"])
    k["measured"] = sum(1 for e in _taken if e.get("measured"))
    rates = {"opened": k["opened"] / n, "accepted": took / n, "completed": k["completed"] / n,
             # Of what was taken and measured, how much improved — None, not
             # 0%, when nothing taken has been measured yet (#141): "improved
             # 0%" read as a failure that never happened.
             "outcome": (k["improved"] / k["measured"]) if k["measured"] else None}
    acts = sorted(e["hours_to_act"] for e in eps if e["hours_to_act"] is not None)
    out = {"n": n, "shown": sum(1 for e in eps if e["shown"]), "opened": k["opened"], "evidence": k["evidence"],
           "accepted": took, "completed": k["completed"], "implemented": k["implemented"],
           "dismissed": k["dismissed"], "snoozed": k["snoozed"],
           "ignored": k["ignored"], "outcomes": k["outcome"], "improved": k["improved"],
           "measured": k["measured"],
           "open_rate": round(rates["opened"], 3), "accept_rate": round(rates["accepted"], 3),
           "complete_rate": round(rates["completed"], 3),
           "outcome_rate": round(rates["outcome"], 3) if rates["outcome"] is not None else None,
           "dismiss_rate": round(k["dismissed"] / n, 3), "ignore_rate": round(k["ignored"] / n, 3),
           "accept_ci90": _wilson(took, n),
           "median_hours_to_act": acts[len(acts) // 2] if acts else None,
           "ras": None, "ras_partial": None, "ras_note": None}
    if n >= RAS_MIN_N:
        if rates["outcome"] is not None:
            out["ras"] = round(100 * sum(RAS_WEIGHTS[x] * rates[x] for x in RAS_WEIGHTS), 1)
        else:
            # Withheld: a fifth of the score is the measured outcome, and
            # scoring it as zero penalised a recommendation nobody had been
            # able to measure yet. The three behaviour rates, re-weighted to
            # their own sum, stand beside it, labelled.
            part = [x for x in RAS_WEIGHTS if x != "outcome"]
            out["ras_partial"] = round(100 * sum(RAS_WEIGHTS[x] * rates[x] for x in part)
                                       / sum(RAS_WEIGHTS[x] for x in part), 1)
            out["ras_note"] = "withheld until something taken has been measured; ras_partial scores behaviour only"
    return out


def _internal_restaurants_sql(conn):
    """Accounts whose owners' behaviour is not a customer's (#141): demo and
    test accounts (exclude_from_learning), internal billing, the admin's own
    home — built from the columns this database has."""
    cols = _columns(conn, "restaurants")
    parts = ["COALESCE(is_demo,0)=1"] if "is_demo" in cols else []
    if "exclude_from_learning" in cols:
        parts.append("COALESCE(exclude_from_learning,0)=1")
    if "billing_status" in cols:
        parts.append("LOWER(COALESCE(billing_status,''))='internal'")
    if _columns(conn, "users") >= {"restaurant_id", "is_admin"}:
        parts.append("id IN (SELECT restaurant_id FROM users GROUP BY restaurant_id "
                     "HAVING MIN(COALESCE(is_admin,0))=1)")
    return "SELECT id FROM restaurants WHERE " + (" OR ".join(parts) if parts else "0")


def _episodes(conn, since, restaurant_id=None, include_internal=False):
    """Every counted episode since `since`. Across the fleet, internal
    accounts are left out — their demo episodes were most of what the
    Recommendations page read (#141); asking for one restaurant by id
    always includes it."""
    where, args = "i.created_at >= ?", [since]
    if restaurant_id:
        where += " AND i.restaurant_id=?"
        args.append(restaurant_id)
    elif not include_internal:
        where += f" AND i.restaurant_id NOT IN ({_internal_restaurants_sql(conn)})"
    inst = _rows_dict(conn, "SELECT i.*, r.name AS restaurant FROM rec_instances i LEFT JOIN restaurants r ON r.id=i.restaurant_id "
                            f"WHERE {where}", tuple(args))
    if not inst:
        return []
    evs = {}
    for e in _rows_dict(conn, "SELECT e.rec_id, e.event, e.surface, e.meta, e.at, e.role FROM rec_events e JOIN rec_instances i "
                              f"ON i.rec_id=e.rec_id WHERE {where} ORDER BY e.id", tuple(args)):
        evs.setdefault(e["rec_id"], []).append(e)
    import rec_ledger
    trackers = _trackers(conn, sorted({i["tracker_id"] for i in inst if i.get("tracker_id")}))
    out = []
    for i in inst:
        es = evs.get(i["rec_id"], [])
        names = {e["event"] for e in es}
        # Only what an owner was shown is a recommendation they could take
        # or ignore. Bookkeeping keys (a kind restored, weights applied, an
        # on-call ask) and episodes no surface ever showed — an answer or a
        # verdict arriving with nothing shown behind it — stay out of every
        # rate.
        if "shown" not in names or not rec_ledger.counts_in_acceptance(i["key"]):
            continue
        # A superseded episode is the same recommendation carried on under
        # new content by a newer one: counting both would count one
        # recommendation twice, and neither was answered nor ignored.
        if i["status"] == "superseded":
            continue
        # The episode's measured result, read ONLY through
        # rec_learning.learned_verdict (confidence audit E13): the ledger's
        # first outcome event was taken as the verdict, so a result the
        # owner disowned or that reversed at its re-check counted as a win.
        verdict = learned_episode_verdict(es, trackers.get(i.get("tracker_id")))
        answered = names & {"accepted", "completed", "dismissed", "implemented"}
        first_act = next((e["at"] for e in es if e["event"] in ("accepted", "completed", "dismissed",
                                                                 "implemented")), None)
        hours = None
        if first_act:
            a, c = _parse(first_act), _parse(i["created_at"])
            if a and c:
                hours = round(max(0.0, (a - c).total_seconds() / 3600), 1)
        # The ledger's own expiry rule (created 14+ days ago, no answer, not
        # snoozed) — the same one expire_stale closes episodes by.
        ignored = (not answered) and (i["status"] == "expired" or rec_ledger.is_stale(i))
        shown_surfaces = sorted({e["surface"] for e in es if e["event"] == "shown" and e["surface"]})
        try:
            sources = json.loads(i["evidence_sources"]) if i["evidence_sources"] else []
        except (TypeError, ValueError):
            sources = []
        out.append({"rec_id": i["rec_id"], "restaurant_id": i["restaurant_id"], "restaurant": i["restaurant"],
                    "key": i["key"], "kind": i["kind"] or (i["key"] or "").split(":", 1)[0], "module": i["module"] or "—",
                    "title": i["title"], "status": i["status"],
                    "surface": i["first_surface"] or (shown_surfaces[0] if shown_surfaces else "unknown"),
                    "has_dollars": bool(i["dollar_value"]), "dollar_value": i["dollar_value"],
                    "confidence_band": i["confidence_band"] or "none", "cross_module": bool(i["cross_module"]),
                    "model_written": bool(i["model_written"]), "cavnar_completes": bool(i["cavnar_completes"]),
                    "sources": sources, "position": i["first_position"],
                    "shown": "shown" in names, "opened": bool(names & {"opened", "evidence_viewed"}) or bool(answered),
                    "evidence": "evidence_viewed" in names, "accepted": "accepted" in names,
                    "completed": "completed" in names, "implemented": "implemented" in names,
                    "dismissed": "dismissed" in names, "snoozed": "snoozed" in names,
                    # "Could not be measured" is not an outcome (ROI #2).
                    "ignored": ignored, "outcome": verdict in _CLEAR, "improved": verdict == "improved",
                    "measured": verdict in _CLEAR, "verdict": verdict,
                    # The confidence it was shown with (K3 snapshot).
                    **{c: _col_or_none(i, c) for c in _SNAPSHOT_COLS},
                    # ...and the one it carried when the owner TOOK it: the
                    # latest showing at or before the first acceptance (each
                    # `shown` meta carries the snapshot) — what calibration
                    # scores when there is one (group P item 8, B2 #9).
                    **_at_acceptance(es, first_act),
                    "tracker": trackers.get(i.get("tracker_id")) or {},
                    "reason_codes": sorted({(_meta_of(e) or {}).get("reason_code") for e in es
                                            if (_meta_of(e) or {}).get("reason_code")}),
                    "hours_to_act": hours, "responder_role": next((e["role"] for e in es if e["event"] in (
                        "accepted", "completed", "dismissed") and e["role"]), None)})
    return out


_CLEAR = ("improved", "worsened", "no_clear_change")
_SNAPSHOT_COLS = ("confidence_pct", "evidence_pct", "accuracy_pct", "accuracy_n", "freshness_pct",
                  "freshness_as_of", "trust_version")
# The tracker columns learned_verdict reads — concurrent and
# baseline_overlaps_trigger included, so an admin result is counted by the
# rule learning counts it by (outcomes.result_counts; group P item 8: the
# calibration pairs could include confounded results).
_TRACKER_COLS = ("id, status, verdict, dollars_monthly, evaluate_on, started_on, metric, after_start, "
                 "after_end, recheck_verdict, owner_checkin, source_key, concurrent, baseline_overlaps_trigger")
_TRACKER_COLS_OLD = ("id, status, verdict, dollars_monthly, evaluate_on, started_on, metric, after_start, "
                     "after_end, recheck_verdict, owner_checkin, source_key")


def _at_acceptance(events, first_act):
    """{confidence_at_accept, evidence_at_accept, accuracy_at_accept,
    freshness_at_accept, trust_version_at_accept} from the latest `shown`
    event at or before `first_act` whose meta carries a snapshot — all None
    when there is none (the episode's first snapshot is used instead)."""
    out = {"confidence_at_accept": None, "evidence_at_accept": None, "accuracy_at_accept": None,
           "freshness_at_accept": None, "trust_version_at_accept": None}
    if not first_act:
        return out
    for e in events or []:
        if e.get("event") != "shown" or str(e.get("at") or "") > str(first_act):
            continue
        m = _meta_of(e) or {}
        if m.get("confidence_pct") is None:
            continue
        out.update(confidence_at_accept=m.get("confidence_pct"), evidence_at_accept=m.get("evidence_pct"),
                   accuracy_at_accept=m.get("accuracy_pct"), freshness_at_accept=m.get("freshness_pct"),
                   trust_version_at_accept=m.get("trust_version"))
    return out


def _col_or_none(row, name):
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def _meta_of(e):
    try:
        return json.loads(e.get("meta") or "{}") or {}
    except (TypeError, ValueError):
        return {}


def _trackers(conn, tids):
    """{tracker id: recommendation_outcomes row} for the episodes' trackers."""
    out = {}
    for n in range(0, len(tids), 400):
        chunk = tids[n:n + 400]
        marks = ",".join("?" for _ in chunk)
        rows = None
        # _rows_strict: _rows_dict never raises, so with it the older column
        # sets were never tried and an older database read no trackers.
        for cols in (_TRACKER_COLS, _TRACKER_COLS_OLD, "id, status, verdict, dollars_monthly, evaluate_on"):
            try:
                rows = _rows_strict(conn, f"SELECT {cols} FROM recommendation_outcomes WHERE id IN ({marks})",
                                    tuple(chunk))
                break
            except Exception:
                continue
        for r in rows or []:
            out[r["id"]] = r
    return out


def learned_episode_verdict(events, tracker=None):
    """An episode's measured result through rec_learning.learned_verdict —
    the ledger's latest outcome event, else its tracker's current verdict,
    with the owner's latest check-in — or None when nothing was measured.
    The same reading rec_learning gives its own record."""
    import rec_learning
    verdict = None
    for e in events or []:
        if e.get("event") == "outcome":
            v = _meta_of(e).get("verdict")
            if v:
                verdict = v
    if tracker and tracker.get("status") == "evaluated":
        verdict = tracker.get("verdict") or verdict or "unknown"
    if verdict is None:
        return None
    checkins = [_meta_of(e) for e in (events or []) if e.get("event") == "checkin"]
    return rec_learning.learned_verdict(verdict, tracker, checkins[-1] if checkins else None)


def _pct_band(pct):
    import confidence_engine
    return confidence_engine.band(pct) if pct is not None else "not snapshotted"


def _group(eps, key_fn, label_fn=None, min_n=1):
    groups = {}
    for e in eps:
        groups.setdefault(key_fn(e), []).append(e)
    rows = []
    for g, members in groups.items():
        b = _ras_block(members)
        if b["n"] < min_n:
            continue
        b["group"] = label_fn(g) if label_fn else g
        rows.append(b)
    rows.sort(key=lambda r: -r["n"])
    return rows


def _surface_label(surface):
    import rec_ledger
    return rec_ledger.surface_label(surface)


def recommendation_acceptance(days=30, restaurant_id=None, include_internal=False):
    """The internal Recommendation Acceptance dashboard: the funnel, the
    score, and what moves it — dollars or none, cross-module, model-written,
    Cavnar-prepared, confidence band, surface — plus the most ignored kinds
    and restaurants showing fatigue. Internal accounts are left out of the
    fleet view (#141)."""
    import models
    days = max(1, min(int(days or 30), 365))
    since = _stamp(datetime.utcnow() - timedelta(days=days))
    with heavy_slot("recommendation analytics"):
        conn = models.get_conn()
        try:
            try:
                eps = _episodes(conn, since, restaurant_id, include_internal=include_internal)
            except Exception as e:           # the ledger table predates this database
                log.warning("recommendation_acceptance unavailable: %s", e)
                eps = []
        finally:
            conn.close()
    total = _ras_block(eps)
    funnel = [{"step": s, "n": total.get(k) or 0} for s, k in (
        ("Shown", "shown"), ("Opened", "opened"), ("Evidence viewed", "evidence"), ("Accepted", "accepted"),
        ("Completed", "completed"), ("Implemented", "implemented"), ("Outcome measured", "outcomes"),
        ("Improved", "improved"))]
    by_kind = _group(eps, lambda e: e["kind"] or "unknown")
    most_ignored = sorted((r for r in by_kind if r["n"] >= 5), key=lambda r: (-r["ignore_rate"], -r["n"]))[:10]
    by_rest = _group(eps, lambda e: (e["restaurant_id"], e["restaurant"] or f"#{e['restaurant_id']}"))
    for r in by_rest:
        r["restaurant_id"], r["group"] = r["group"]
    # Fatigue: plenty shown, little taken — the pattern that ends in an owner
    # tuning the product out.
    fatigue = [r for r in by_rest if r["n"] >= RAS_MIN_N and (r["dismiss_rate"] + r["ignore_rate"]) >= 0.75]
    return {"ok": True, "days": days, "restaurant_id": restaurant_id, "min_n": RAS_MIN_N, "weights": RAS_WEIGHTS,
            "internal_excluded": not (restaurant_id or include_internal),
            "total": total, "funnel": funnel,
            "by_module": _group(eps, lambda e: e["module"]),
            "by_kind": by_kind,
            "by_surface": _group(eps, lambda e: e["surface"], label_fn=_surface_label),
            "by_dollars": _group(eps, lambda e: "has a $ figure" if e["has_dollars"] else "no $ figure"),
            "by_cross_module": _group(eps, lambda e: "cross-module" if e["cross_module"] else "one module"),
            "by_model_written": _group(eps, lambda e: "model-written" if e["model_written"] else "rule-written"),
            "by_cavnar_completes": _group(eps, lambda e: "Cavnar AI prepares it" if e["cavnar_completes"] else "owner does it"),
            # By the confidence the owner was SHOWN (the K3 snapshot's
            # percentage → band) — not the legacy band column, which mixed
            # card bands, model self-ratings and the review trend's slope
            # (CA1 X5). Episodes from before the snapshot group apart.
            "by_confidence": _group(eps, lambda e: _pct_band(e.get("confidence_pct"))),
            "by_role": _group([e for e in eps if e["responder_role"]], lambda e: e["responder_role"]),
            "by_restaurant": by_rest,
            "most_ignored": most_ignored,
            "fatigue": fatigue}


# ── schedule generation experiments (internal only) ─────────────────────────
#
# The live A/B of schedule generation variants (schedule_experiments, audit
# #50): each generated week's arm, measured on how much of the draft went
# out unedited and on what the week then did. Admin-only — no owner ever
# sees an arm.

def schedule_experiments():
    """Per experiment and arm: weeks, acceptance with a 90% interval,
    outcomes, the draft's Shift Quality and the verdict under the minimum
    sample rule; plus every pin in force."""
    import schedule_experiments as sx
    try:
        return sx.readout()
    except Exception as e:           # the tables predate this database
        log.warning("schedule_experiments unavailable: %s", e)
        return {"ok": True, "experiments": [], "pins": [], "env_pin": None, "rule": sx.RULE,
                "min_weeks": sx.MIN_WEEKS_PER_ARM, "min_restaurants": sx.MIN_RESTAURANTS_PER_ARM,
                "error": "The experiment tables are not on this database yet."}


def set_schedule_experiment_pin(restaurant_id, experiment, arm, by="admin"):
    """Pin one restaurant to an arm ('off' = the control), or unpin (arm
    None) — the per-restaurant kill switch."""
    import schedule_experiments as sx
    import models
    if not models.get_restaurant(int(restaurant_id)):
        return {"ok": False, "error": "No such restaurant."}
    return sx.set_pin(int(restaurant_id), experiment, arm, pinned_by=by)


def promote_schedule_experiment(experiment, arm, by="admin", note=None):
    """The reviewed step that makes an experiment's winning arm the default
    (ROI #46): only the arm the readout's verdict calls, recorded with who
    promoted it; every restaurant then gets that arm from a stored setting —
    no code edit. Undone by revert_schedule_experiment."""
    import schedule_experiments as sx
    return sx.promote(experiment, arm, promoted_by=by, note=note)


def revert_schedule_experiment(experiment, by="admin"):
    import schedule_experiments as sx
    return sx.revert(experiment, reverted_by=by)


# ── recommendation calibration and missed detections (internal only) ────────
#
# ROI audit #43 and #44. Admin-only until the figures have enough behind
# them to be shown to an owner.

CALIBRATION_MIN_N = 5      # pairs per kind before a ratio is called


def recommendation_calibration(days=365, restaurant_id=None):
    """Each recommendation's predicted dollars (what it was shown with,
    rec_instances.dollar_value) against what its tracker measured
    (recommendation_outcomes.dollars_monthly; a no-clear-change result is $0
    realised), by kind. Only episodes taken and measured with a clear
    verdict; a result on a metric with no dollar reading is counted as
    `unpriced` and left out of the ratio. ratio = realised ÷ predicted over
    the kind; within_half = share of pairs whose realised figure landed
    within ±50% of the prediction."""
    import models
    days = max(1, min(int(days or 365), 730))
    since = _stamp(datetime.utcnow() - timedelta(days=days))
    where, args = "i.created_at >= ? AND i.dollar_value IS NOT NULL AND i.dollar_value > 0", [since]
    if restaurant_id:
        where += " AND i.restaurant_id=?"
        args.append(int(restaurant_id))
    conn = models.get_conn()
    try:
        try:
            rows = None
            # concurrent / baseline_overlaps_trigger: the result rule learning
            # counts by (learned_verdict ≡ outcomes.result_counts, group P
            # item 8); an older database without them reads as before.
            for extra in (", o.concurrent, o.baseline_overlaps_trigger", ""):
                try:
                    rows = _rows_strict(conn, "SELECT i.rec_id, i.kind, i.key, i.status, i.dollar_value, o.verdict, "
                                            "o.dollars_monthly, o.id AS tracker_id, o.status AS tracker_status, "
                                            "o.recheck_verdict, o.owner_checkin, o.source_key" + extra + " "
                                            "FROM rec_instances i JOIN recommendation_outcomes o ON o.id=i.tracker_id "
                                            f"WHERE {where} AND o.status='evaluated'", tuple(args))
                    break
                except Exception:
                    if not extra:
                        raise
            _ck = {}
            for n in range(0, len(rows), 400):
                chunk = [r["rec_id"] for r in rows[n:n + 400]]
                marks = ",".join("?" for _ in chunk)
                for e in _rows_dict(conn, f"SELECT rec_id, meta FROM rec_events WHERE event='checkin' "
                                          f"AND rec_id IN ({marks}) ORDER BY at, id", tuple(chunk)):
                    _ck[e["rec_id"]] = _meta_of(e)
        except Exception as e:           # the columns predate this database
            log.warning("recommendation_calibration unavailable: %s", e)
            rows, _ck = [], {}
    finally:
        conn.close()
    by = {}
    import rec_learning
    for r in rows:
        if r["status"] not in ("accepted", "completed", "implemented"):
            continue
        # The verdict through learned_verdict (confidence audit E13): a
        # disowned or conditions-changed result is not a pair at all, and a
        # faded or reversed one realised nothing.
        r["verdict"] = rec_learning.learned_verdict(
            r["verdict"], {"recheck_verdict": r.get("recheck_verdict"), "owner_checkin": r.get("owner_checkin"),
                           "source_key": r.get("source_key"), "concurrent": r.get("concurrent"),
                           "baseline_overlaps_trigger": r.get("baseline_overlaps_trigger")},
            _ck.get(r.get("rec_id")))
        k = by.setdefault(r["kind"] or (r["key"] or "").split(":", 1)[0], {"pairs": [], "unpriced": 0})
        if r["verdict"] == "no_clear_change":
            k["pairs"].append((float(r["dollar_value"]), 0.0))
        elif r["verdict"] in ("improved", "worsened") and r["dollars_monthly"] is not None:
            k["pairs"].append((float(r["dollar_value"]), float(r["dollars_monthly"])))
        elif r["verdict"] in ("improved", "worsened"):
            k["unpriced"] += 1
    out = []
    for kind, v in by.items():
        pairs = v["pairs"]
        pred = sum(p for p, _ in pairs)
        real = sum(a for _, a in pairs)
        ratios = sorted(a / p for p, a in pairs if p)
        out.append({"kind": kind, "n": len(pairs), "unpriced": v["unpriced"], "predicted": round(pred, 2),
                    "realised": round(real, 2), "ratio": round(real / pred, 3) if pred else None,
                    "median_ratio": round(ratios[len(ratios) // 2], 3) if ratios else None,
                    "within_half": (round(sum(1 for x in ratios if 0.5 <= x <= 1.5) / len(ratios), 3)
                                    if ratios else None),
                    "enough": len(pairs) >= CALIBRATION_MIN_N})
    out.sort(key=lambda r: (-r["n"], r["kind"]))
    pred = sum(r["predicted"] for r in out)
    real = sum(r["realised"] for r in out)
    return {"ok": True, "days": days, "restaurant_id": restaurant_id, "min_n": CALIBRATION_MIN_N, "by_kind": out,
            "total": {"n": sum(r["n"] for r in out), "predicted": round(pred, 2), "realised": round(real, 2),
                      "ratio": round(real / pred, 3) if pred else None}}


# ── confidence calibration (K7, internal only) ─────────────────────────────
#
# What the owner was told (the Recommendation Confidence % snapshotted at
# delivery, K3) against what happened: taken episodes with a clear learned
# verdict, improved = 1. A reliability table by decile, a Brier score, and
# the same per dimension — each withheld below its floor.

CALIBRATION_FLOOR_N = RAS_MIN_N     # a band's observed rate below this is noise


def _calibration_scored(eps):
    """The taken, measured episodes calibration scores, counted by the rule
    learning counts by: the verdict already read through learned_verdict
    (confounded and baseline-overlap results unknown — the tracker carries
    concurrent / baseline_overlaps_trigger), and ONE result per overlapping
    after-window on a number per restaurant and kind (rec_learning.
    _one_per_window, kind_record's rule — two recommendations read over the
    same weeks on the same number are one change). Each carries the figure
    it is scored on: the confidence at ACCEPTANCE when a showing before it
    carried one, else the episode's first snapshot (`scored_on`)."""
    import rec_learning
    taken = [e for e in eps if (e["accepted"] or e["completed"] or e.get("implemented")) and e.get("measured")]
    groups = {}
    for e in taken:
        groups.setdefault((e["restaurant_id"], e["kind"]), []).append(e)
    kept = []
    for items in groups.values():
        kept.extend(rec_learning._one_per_window(items))
    for e in kept:
        at_accept = e.get("confidence_at_accept") is not None
        e["scored_on"] = "acceptance" if at_accept else "first_shown"
        for dim in ("confidence", "evidence", "accuracy", "freshness"):
            e[f"score_{dim}"] = e.get(f"{dim}_at_accept") if at_accept else e.get(f"{dim}_pct")
        e["score_version"] = (e.get("trust_version_at_accept") if at_accept else e.get("trust_version"))
    return kept


def confidence_calibration(days=365, restaurant_id=None):
    """Will's view of whether a higher support score really goes with better
    results (the owner's decision, 9/24/26: the % is how well SUPPORTED the
    advice is — never a probability — so it is checked for ORDER, not for
    "72% comes true 72% of the time"):
      {ordering: {bands, ordered, violations, spearman, n} (confidence_engine.
       ordering: the observed improved rate by support band, with its 90%
       Wilson range, must not decrease — a higher band whose range sits
       wholly below a lower one's is a violation),
       ordering_by_kind: [{kind, ...ordering}], ordering_by_dimension:
       {evidence, accuracy, freshness}, alerts: [{severity, title, detail}]
       (an admin row, never an SMS), meaning, scored_on {acceptance,
       first_shown}, versions {version: n},
       bands, brier (decile reliability and the Brier score, kept for the
       record — NOT the meaning of the %), by_kind, by_dimension, floor_n,
       kind_floor_n, distrust:{n, by_kind}}.
    Only snapshots of the support-score meaning (trust_version ≥ 2) are
    judged for order; older ones measured something else and are counted
    apart. Owners never see this."""
    import models
    import confidence_engine as ce
    days = max(1, min(int(days or 365), 730))
    since = _stamp(datetime.utcnow() - timedelta(days=days))
    conn = models.get_conn()
    try:
        try:
            eps = _episodes(conn, since, restaurant_id)
        except Exception as e:           # the ledger / snapshot columns predate this database
            log.warning("confidence_calibration unavailable: %s", e)
            eps = []
    finally:
        conn.close()
    scored = _calibration_scored(eps)

    def current(e):
        try:
            return int(e.get("score_version") or 0) >= ce.VERSION
        except (TypeError, ValueError):
            return False

    def pairs(field, only_current=False, rows=None):
        return [(e.get(field), 1 if e["improved"] else 0) for e in (scored if rows is None else rows)
                if e.get(field) is not None and (current(e) or not only_current)]

    overall = pairs("score_confidence")
    support = pairs("score_confidence", only_current=True)
    by_kind, order_by_kind = [], []
    groups = {}
    for e in scored:
        if e.get("score_confidence") is not None:
            groups.setdefault(e["kind"], []).append(e)
    for kind, es in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        ps = pairs("score_confidence", rows=es)
        enough = len(ps) >= CALIBRATION_MIN_N
        by_kind.append({"kind": kind, "n": len(ps), "predicted_mean": round(sum(p for p, _ in ps) / len(ps), 1),
                        "observed_rate": round(100.0 * sum(y for _, y in ps) / len(ps), 1) if enough else None,
                        "enough": enough})
        o = ce.ordering(pairs("score_confidence", only_current=True, rows=es), CALIBRATION_MIN_N)
        order_by_kind.append(dict(o, kind=kind))
    ordering = ce.ordering(support, CALIBRATION_FLOOR_N)
    order_dims = {dim: ce.ordering(pairs(f"score_{dim}", only_current=True), CALIBRATION_FLOOR_N)
                  for dim in ("evidence", "accuracy", "freshness")}
    # The flag: a higher band doing worse than a lower one beyond its
    # interval. An admin row on this view, never an SMS.
    alerts = []

    def flag(where, o):
        for v in o.get("violations") or []:
            alerts.append({"severity": "warning", "where": where,
                           "title": f"Higher support is doing worse{'' if where == 'overall' else ' — ' + where}",
                           "detail": (f"Recommendations shown at {v['higher']}% improved at most "
                                      f"{v['higher_high']:g}% of the time (90% range), below the "
                                      f"{v['lower_low']:g}% floor of those shown at {v['lower']}%.")})
    flag("overall", ordering)
    for o in order_by_kind:
        flag(o["kind"], o)
    for dim, o in order_dims.items():
        flag(f"{dim} dimension", o)
    versions = {}
    for e in scored:
        v = str(e.get("score_version") or "none")
        versions[v] = versions.get(v, 0) + 1
    # The owner's "don't trust the data" answers, counted (E14).
    distrust = [e for e in eps if "dont_trust_data" in (e.get("reason_codes") or [])]
    dk = {}
    for e in distrust:
        dk[e["kind"]] = dk.get(e["kind"], 0) + 1
    return {"ok": True, "days": days, "restaurant_id": restaurant_id, "floor_n": CALIBRATION_FLOOR_N,
            "kind_floor_n": CALIBRATION_MIN_N, "n": len(overall), "meaning": ce.MEANING,
            "ordering": ordering, "ordering_by_kind": order_by_kind, "ordering_by_dimension": order_dims,
            "alerts": alerts,
            "scored_on": {"acceptance": sum(1 for e in scored if e["scored_on"] == "acceptance"),
                          "first_shown": sum(1 for e in scored if e["scored_on"] == "first_shown")},
            "versions": versions, "support_n": len(support),
            "bands": ce.reliability(overall, CALIBRATION_FLOOR_N),
            "brier": ce.brier(overall, CALIBRATION_FLOOR_N),
            "brier_note": "Kept for the record: the support score is not a probability, so this is not its test.",
            "by_kind": by_kind,
            "by_dimension": {dim: {"bands": ce.reliability(pairs(f"score_{dim}"), CALIBRATION_FLOOR_N),
                                   "brier": ce.brier(pairs(f"score_{dim}"), CALIBRATION_FLOOR_N),
                                   "n": len(pairs(f"score_{dim}"))}
                             for dim in ("evidence", "accuracy", "freshness")},
            "distrust": {"n": len(distrust),
                         "by_kind": sorted(({"kind": k, "n": v} for k, v in dk.items()), key=lambda x: -x["n"])},
            "rule": ("taken recommendations with a clear verdict read through learned_verdict (confounded and "
                     "baseline-overlap results unknown), one result per window per restaurant and kind, scored "
                     "on the confidence at acceptance when a showing before it carried one; improved = 1; an "
                     "observed rate only at the floor; order judged on support-score snapshots only")}


def missed_detections(days=30, restaurant_id=None, limit=200):
    """Problems that surfaced — an alert, an issue, a close-out 86 — with no
    recommendation covering their subject shown in the days before
    (rec_ledger.note_problem): by source, by subject kind, and the latest
    rows."""
    import models
    import rec_ledger
    days = max(1, min(int(days or 30), 365))
    since = _stamp(datetime.utcnow() - timedelta(days=days))
    where, args = "m.detected_at >= ?", [since]
    if restaurant_id:
        where += " AND m.restaurant_id=?"
        args.append(int(restaurant_id))
    conn = models.get_conn()
    try:
        try:
            rows = _rows_dict(conn, "SELECT m.*, r.name AS restaurant FROM rec_missed_detections m "
                                    "LEFT JOIN restaurants r ON r.id=m.restaurant_id "
                                    f"WHERE {where} ORDER BY m.detected_at DESC, m.id DESC", tuple(args))
        except Exception as e:           # the table predates this database
            log.warning("missed_detections unavailable: %s", e)
            rows = []
    finally:
        conn.close()
    by_source, by_kind = {}, {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        k = rec_ledger.kind_of(r["subject_key"])
        by_kind[k] = by_kind.get(k, 0) + 1
    return {"ok": True, "days": days, "restaurant_id": restaurant_id, "total": len(rows),
            "lookback_days": rec_ledger.MISSED_LOOKBACK_DAYS,
            "by_source": sorted(({"source": s, "n": n} for s, n in by_source.items()), key=lambda x: -x["n"]),
            "by_kind": sorted(({"kind": s, "n": n, "mapped": s in rec_ledger.PROBLEM_COVERAGE}
                               for s, n in by_kind.items()), key=lambda x: -x["n"]),
            "rows": rows[:int(limit)]}


# ── fix round C: the console's newer reads ───────────────────────────────────

# Email delivery health (#59): bounce and complaint rates over what Resend
# ACCEPTED, with the thresholds that make them an issue. A rate is only
# called at EMAIL_RATE_MIN_SENDS accepted sends.
EMAIL_BOUNCE_WARN_PCT = float(os.getenv("EMAIL_BOUNCE_WARN_PCT", "4"))
EMAIL_BOUNCE_CRIT_PCT = float(os.getenv("EMAIL_BOUNCE_CRIT_PCT", "8"))
EMAIL_COMPLAINT_WARN_PCT = float(os.getenv("EMAIL_COMPLAINT_WARN_PCT", "0.1"))
EMAIL_COMPLAINT_CRIT_PCT = float(os.getenv("EMAIL_COMPLAINT_CRIT_PCT", "0.3"))
EMAIL_RATE_MIN_SENDS = int(os.getenv("EMAIL_RATE_MIN_SENDS", "50"))


def _email_rates(conn, w):
    out = {"thresholds": {"bounce_warn_pct": EMAIL_BOUNCE_WARN_PCT, "bounce_crit_pct": EMAIL_BOUNCE_CRIT_PCT,
                          "complaint_warn_pct": EMAIL_COMPLAINT_WARN_PCT,
                          "complaint_crit_pct": EMAIL_COMPLAINT_CRIT_PCT, "min_sends": EMAIL_RATE_MIN_SENDS}}
    for label, since in (("7d", w["week"]), ("30d", w["month"])):
        row = _one_dict(conn, "SELECT COUNT(*) AS n, "
                              "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
                              "SUM(CASE WHEN status='bounced' THEN 1 ELSE 0 END) AS bounces, "
                              "SUM(CASE WHEN status='complained' THEN 1 ELSE 0 END) AS complaints, "
                              "SUM(CASE WHEN status='delivered' THEN 1 ELSE 0 END) AS delivered, "
                              "MAX(CASE WHEN status='bounced' THEN sent_at END) AS last_bounce_at, "
                              "MAX(CASE WHEN status='complained' THEN sent_at END) AS last_complaint_at "
                              "FROM email_log WHERE sent_at >= ?", (since,), label="email_log") or {}
        accepted = int(row.get("n") or 0) - int(row.get("failed") or 0)
        b, c = int(row.get("bounces") or 0), int(row.get("complaints") or 0)
        out[label] = {"sent": int(row.get("n") or 0), "accepted": accepted, "failed": int(row.get("failed") or 0),
                      "bounces": b, "complaints": c, "delivered": int(row.get("delivered") or 0),
                      "bounce_rate": round(100.0 * b / accepted, 2) if accepted else None,
                      "complaint_rate": round(100.0 * c / accepted, 2) if accepted else None,
                      "enough": accepted >= EMAIL_RATE_MIN_SENDS,
                      "last_bounce_at": row.get("last_bounce_at"), "last_complaint_at": row.get("last_complaint_at")}
    return out


# Inbound webhooks (#74). Each provider's last VERIFIED event and the
# requests refused since. Two ledgers record them, one per workstream, and
# the console reads both as one: E's inbound_webhook_health (Resend, Twilio
# inbound texts, Twilio delivery reports) and H's webhook_verifications
# (Stripe, DocuSign). A provider neither has a row for yet falls back to
# what a verified delivery already writes, and to its signature captures in
# job_failures over the last day.
WEBHOOK_PROVIDERS = (
    {"provider": "stripe", "label": "Stripe", "secret_env": "STRIPE_WEBHOOK_SECRET", "job": "stripe_webhook"},
    {"provider": "resend", "label": "Resend", "secret_env": "RESEND_WEBHOOK_SECRET", "job": "resend_webhook"},
    {"provider": "twilio", "label": "Twilio", "secret_env": "TWILIO_AUTH_TOKEN", "job": "twilio_webhook"},
    {"provider": "twilio_status", "label": "Twilio delivery reports", "secret_env": "TWILIO_AUTH_TOKEN",
     "job": "twilio_status_webhook"},
    {"provider": "docusign", "label": "DocuSign", "secret_env": "DOCUSIGN_WEBHOOK_SECRET", "job": "docusign_webhook"},
)
# (table, {normalized field: that table's column}) — each ledger in its own
# words (E's and H's), read into one shape.
_WEBHOOK_LEDGERS = (
    ("inbound_webhook_health", {"last_verified_at": "last_verified_at", "last_event": "last_event_type",
                                "failed_since_verified": "failed_since_verified", "failures_total": "failed_count",
                                "last_failure_at": "last_failed_at", "last_failure": "last_failure_reason"}),
    ("webhook_verifications", {"last_verified_at": "last_verified_at", "last_event": "last_verified_event",
                               "failed_since_verified": "failures_since_verified",
                               "failures_total": "failures_total", "last_failure_at": "last_failure_at",
                               "last_failure": "last_failure_error"}),
)
# Stale after this long with no verified event while sends went out — only
# judged from a recorded ledger (a derived "last event" is too sparse).
WEBHOOK_STALE_HOURS = int(os.getenv("ADMIN_WEBHOOK_STALE_HOURS", "24"))
_SIGNATURE_WORDS = ("signature", "unauthori", "verif", "hmac", "svix", "bad secret", "403", "401")


def _webhook_ledger(conn):
    """({provider: normalized row}, [ledger names read])."""
    ledger, names = {}, []
    for table, cols in _WEBHOOK_LEDGERS:
        have = _columns(conn, table)
        if "provider" not in have:
            continue
        names.append(table)
        for row in _rows_dict(conn, f"SELECT * FROM {table}", label=table, optional=True):
            p = (row.get("provider") or "").lower()
            if p and p not in ledger:
                ledger[p] = dict({k: row.get(c) for k, c in cols.items() if c in have}, ledger=table)
    return ledger, names


def _webhook_health(conn):
    ledger, ledger_names = _webhook_ledger(conn)
    day = _utc_stamp(_utcnow() - timedelta(days=1))
    eng = _one_dict(conn, "SELECT MAX(opened_at) AS o, MAX(clicked_at) AS c FROM email_log", label="email_log") or {}
    derived = {
        "stripe": _newest((_one_dict(conn, "SELECT MAX(created_at) AS at FROM admin_events WHERE source='stripe'",
                                     label="admin_events") or {}).get("at"),
                          (_one_dict(conn, "SELECT MAX(seen_at) AS at FROM stripe_events_seen", label="stripe_events_seen",
                                     optional=True) or {}).get("at")),
        "docusign": _newest((_one_dict(conn, "SELECT MAX(seen_at) AS at FROM docusign_events_seen",
                                       label="docusign_events_seen", optional=True) or {}).get("at"),
                            (_one_dict(conn, "SELECT MAX(created_at) AS at FROM admin_events WHERE source='docusign'",
                                       label="admin_events") or {}).get("at")),
        "resend": _newest(eng.get("o"), eng.get("c"),
                          (_one_dict(conn, "SELECT MAX(created_at) AS at FROM email_suppressions WHERE reason IN "
                                           "('bounced','complained','bounce','complaint','hard_bounce')",
                                     label="email_suppressions") or {}).get("at")),
        "twilio": (_one_dict(conn, "SELECT MAX(updated_at) AS at FROM sms_log", label="sms_log",
                             optional=True) or {}).get("at"),
    }
    sends = {"resend": (_one_dict(conn, "SELECT COUNT(*) AS n FROM email_log WHERE sent_at >= ? AND status != 'failed'",
                                  (day,), label="email_log") or {}).get("n") or 0}
    out = []
    for spec in WEBHOOK_PROVIDERS:
        p = spec["provider"]
        row = ledger.get(p)
        if row:
            # Refused since the last verified request: a rotated or missing
            # secret refuses every delivery, and no verified one follows.
            failures = int(row.get("failed_since_verified") or 0)
            last_verified = row.get("last_verified_at")
            first_failure = row.get("last_failure_at") if failures else None
        else:
            fails = _rows_dict(conn, "SELECT created_at, error FROM job_failures WHERE job=? AND created_at >= ? "
                                     "ORDER BY id", (spec["job"], day), label="job_failures")
            sig = [f for f in fails if any(wd in (f.get("error") or "").lower() for wd in _SIGNATURE_WORDS)]
            failures = len(sig)
            last_verified = derived.get(p)
            first_failure = sig[0]["created_at"] if sig else None
        age = _age_hours(last_verified, "UTC")
        problem = failures > 0 and (age is None or age > 24)
        stale = (bool(row) and p == "resend" and sends.get("resend", 0) >= 5
                 and (age is None or age > WEBHOOK_STALE_HOURS))
        out.append({**spec, "last_verified_at": _iso_z(last_verified, "UTC"),
                    "age_hours": round(age, 1) if age is not None else None,
                    # With a ledger: refused since the last verified request;
                    # without one: signature captures in the last 24 hours.
                    "failures_24h": failures, "failures_since_verified": failures if row else None,
                    "failures_total": int(row.get("failures_total") or 0) if row else None,
                    "last_failure": (row or {}).get("last_failure"), "last_event": (row or {}).get("last_event"),
                    "first_failure_at": _iso_z(first_failure, "UTC"), "problem": problem, "stale": stale,
                    "configured": bool(os.getenv(spec["secret_env"], "")),
                    "source": row["ledger"] if row else "derived"})
    return {"providers": out, "ledger": ", ".join(ledger_names) or None,
            "basis": ("the providers' own ledgers (inbound_webhook_health for Resend and Twilio, "
                      "webhook_verifications for Stripe and DocuSign); a provider with no row yet is derived"
                      if ledger_names else
                      "derived: Stripe and DocuSign from the events they record, Resend from opens, clicks and "
                      "bounce suppressions, Twilio from sms_log; failures from signature captures in job_failures")}


def _auto_caps(conn):
    """([{restaurant_id, restaurant, cap, until, reason}], supported) — the
    temporary storm caps applied automatically (workstream E). E stores them
    in alert_storm_caps (one per restaurant per local day, until its next
    local midnight, unless lifted); the other shapes are kept for a database
    that has not had E's migration."""
    if _columns(conn, "alert_storm_caps"):
        rows = _rows_dict(conn, "SELECT c.restaurant_id, r.name AS restaurant, c.local_day, c.started_at, c.until_at, "
                                "c.alerts_in_window, c.threshold, c.suppressed FROM alert_storm_caps c "
                                "LEFT JOIN restaurants r ON r.id=c.restaurant_id WHERE c.lifted_at IS NULL "
                                "AND julianday(c.until_at) > julianday('now') ORDER BY c.restaurant_id",
                          label="alert_storm_caps")
        return [dict(_storm_cap_view(r), restaurant_id=r["restaurant_id"], restaurant=r["restaurant"])
                for r in rows], True
    cols = _columns(conn, "alert_auto_caps")
    if cols:
        until = "until" if "until" in cols else ("expires_at" if "expires_at" in cols else None)
        where = f"WHERE julianday({until}) > julianday('now')" if until else ""
        rows = _rows_dict(conn, f"SELECT c.*, r.name AS restaurant FROM alert_auto_caps c LEFT JOIN restaurants r "
                                f"ON r.id=c.restaurant_id {where} ORDER BY c.restaurant_id", label="alert_auto_caps")
        return rows, True
    rcols = _columns(conn, "restaurants")
    for until in ("alert_auto_cap_until", "alert_cap_auto_until"):
        if until in rcols:
            cap = next((c for c in ("alert_auto_cap", "alert_cap_auto") if c in rcols), None)
            rows = _rows_dict(conn, f"SELECT id AS restaurant_id, name AS restaurant, {until} AS until"
                                    + (f", {cap} AS cap" if cap else "") +
                                    f" FROM restaurants WHERE julianday({until}) > julianday('now') ORDER BY name",
                              label="restaurants")
            return rows, True
    return [], False


def _week_start(dt_ct):
    d0 = dt_ct.replace(hour=0, minute=0, second=0, microsecond=0)
    return d0 - timedelta(days=d0.weekday())


def _series(recs, d, conn):
    """The Overview charts, computed here rather than back-cast in the
    browser from today's accounts (#36, #18): MRR by month and accounts by
    week from business_metrics_daily where snapshots exist, else a
    reconstruction that is labelled as one; signups by Chicago week counted
    from every real account created, churned ones included."""
    real = _real(recs)
    now_ct = _now_ct()
    weeks = [_week_start(now_ct) - timedelta(weeks=i) for i in range(11, -1, -1)]
    created = [(_parse_utc(r["created_at"], "UTC"), r) for r in real]
    signups = []
    for ws in weeks:
        we = ws + timedelta(days=7)
        signups.append({"week_start": ws.date().isoformat(),
                        "n": sum(1 for c, _r in created if c and ws <= _ct(c) < we)})
    snaps = _rows_dict(conn, "SELECT date, mrr, list_mrr, billed_mrr, committed_mrr, paying_accounts, "
                             "trial_accounts FROM business_metrics_daily WHERE date >= ? ORDER BY date",
                       ((now_ct.date() - timedelta(days=400)).isoformat(),), label="business_metrics_daily",
                       optional=True)
    if snaps:
        by_month = {}
        for s in snaps:
            by_month[s["date"][:7]] = s               # the last snapshot of each month
        mrr = [{"month": m, "mrr": s["mrr"], "billed": s["billed_mrr"], "list": s["list_mrr"],
                "committed": s["committed_mrr"], "as_of": s["date"]} for m, s in sorted(by_month.items())][-12:]
        by_week = {}
        for s in snaps:
            from datetime import date as _date
            ws = _date.fromisoformat(s["date"])
            by_week[(ws - timedelta(days=ws.weekday())).isoformat()] = s
        accounts = [{"week_start": ws.date().isoformat(),
                     "paying": (by_week.get(ws.date().isoformat()) or {}).get("paying_accounts"),
                     "trial": (by_week.get(ws.date().isoformat()) or {}).get("trial_accounts")} for ws in weeks]
        source, note = "snapshots", f"From daily snapshots since {_mdy(snaps[0]['date'], OPERATOR_TZ)}."
    else:
        mrr = []
        for i in range(5, -1, -1):
            y, m = now_ct.year, now_ct.month - i
            while m <= 0:
                m, y = m + 12, y - 1
            end = datetime(y + (m == 12), (m % 12) + 1, 1, tzinfo=now_ct.tzinfo)
            mrr.append({"month": f"{y:04d}-{m:02d}",
                        "mrr": round(sum(r["billing"]["monthly"] for c, r in created
                                         if c and _ct(c) < end and r["billing"]["status"] in PAYING_STATES), 2)})
        accounts = [{"week_start": ws.date().isoformat(),
                     "paying": sum(1 for c, r in created if c and _ct(c) < ws + timedelta(days=7)
                                   and r["billing"]["status"] in PAYING_STATES), "trial": None} for ws in weeks]
        source = "reconstructed"
        note = ("Reconstructed from today's accounts: churned and downgraded accounts are missing from the past. "
                "Real history starts when the daily snapshot runs.")
    return {"mrr": mrr, "signups": signups, "accounts": accounts, "source": source, "note": note,
            "week_starts_on": "Monday", "tz": OPERATOR_TZ}


def _trial_conversion(recs, d, window_days=90):
    """Trial → paid from restaurants.converted_at (workstream H) (#51): of
    the real accounts that signed up in the window, how many converted, and
    how fast. Unavailable (and says so) until the column exists."""
    real = _real(recs)
    if not d.get("has_converted_at"):
        return {"available": False, "reason": "restaurants.converted_at is not recorded yet", "window_days": window_days}
    floor = _utcnow() - timedelta(days=window_days)
    cohort, converted, days = [], [], []
    for r in real:
        c = _parse_utc(r["created_at"], "UTC")
        if not c or c < floor:
            continue
        cohort.append(r)
        conv = _parse_utc(r["billing"].get("converted_at"), "UTC")
        if conv:
            converted.append(r)
            days.append(max(0.0, (conv - c).total_seconds() / 86400))
    ripe = [r for r in real if (_age_days(r["created_at"], "UTC") or 0) >= 30]
    ripe_conv = [r for r in ripe if (lambda c, v: c and v and (v - c).days <= 30)(
        _parse_utc(r["created_at"], "UTC"), _parse_utc(r["billing"].get("converted_at"), "UTC"))]
    days.sort()
    return {"available": True, "window_days": window_days, "started": len(cohort), "converted": len(converted),
            "rate_pct": round(100.0 * len(converted) / len(cohort), 1) if cohort else None,
            "median_days_to_convert": round(days[len(days) // 2], 1) if days else None,
            "within_30d": {"eligible": len(ripe), "converted": len(ripe_conv),
                           "rate_pct": round(100.0 * len(ripe_conv) / len(ripe), 1) if ripe else None}}


# ── vendor costs (#90) ──────────────────────────────────────────────────────

VENDORS = ("railway", "resend", "twilio", "stripe_fees", "docusign", "anthropic", "google_places", "perplexity",
           "apple", "domain", "other")


def vendor_costs_month(month):
    """{total, by_vendor} of the costs entered for one 'YYYY-MM'."""
    conn = get_conn()
    try:
        rows = _rows_dict(conn, "SELECT vendor, amount_usd FROM vendor_costs WHERE month=?", (month,),
                          label="vendor_costs", optional=True)
    finally:
        conn.close()
    by = {r["vendor"]: round(float(r["amount_usd"] or 0), 2) for r in rows}
    return {"month": month, "total": round(sum(by.values()), 2), "by_vendor": by}


def _ai_cost_by_vendor(conn, since, until=None):
    """{vendor: $} from ai_usage — its vendor column once it exists
    (workstream G), else by model and action name."""
    cols = _columns(conn, "ai_usage")
    vendor = ("COALESCE(vendor,'anthropic')" if "vendor" in cols else
              "CASE WHEN model LIKE 'google-places%' OR action LIKE 'places%' THEN 'google_places' "
              "WHEN model LIKE '%perplexity%' OR model LIKE 'sonar%' OR action LIKE '%visibility%' THEN 'perplexity' "
              "ELSE 'anthropic' END")
    args = [since] + ([until] if until else [])
    rows = _rows_dict(conn, f"SELECT {vendor} AS vendor, ROUND(SUM(cost_usd),4) AS cost FROM ai_usage "
                            f"WHERE created_at >= ?{' AND created_at < ?' if until else ''} GROUP BY 1",
                      tuple(args), label="ai_usage", optional=True)
    return {r["vendor"]: float(r["cost"] or 0) for r in rows}


def vendor_costs(months=6):
    """The cost panel: every entered vendor cost by month beside the usage
    the ledgers already know (AI and paid APIs by vendor; SMS once sms_log
    carries a cost), so gross margin has a denominator (#90)."""
    months = max(1, min(int(months or 6), 24))
    now = _now_ct()
    keys = []
    y, m = now.year, now.month
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    keys.reverse()
    with _collecting() as bucket:
        conn = get_conn()
        try:
            rows = _rows_dict(conn, "SELECT id, vendor, month, amount_usd, kind, source, note, updated_by, updated_at "
                                    f"FROM vendor_costs WHERE month >= ? ORDER BY month, vendor", (keys[0],),
                              label="vendor_costs", optional=True)
            usage = {}
            for k in keys:
                start = datetime(int(k[:4]), int(k[5:]), 1, tzinfo=now.tzinfo)
                end = datetime(start.year + (start.month == 12), (start.month % 12) + 1, 1, tzinfo=now.tzinfo)
                ai = _ai_cost_by_vendor(conn, _utc_stamp(start), _utc_stamp(end))
                sms = (_one_dict(conn, "SELECT ROUND(SUM(COALESCE(cost_usd,0)),4) AS c FROM sms_log WHERE created_at >= ? "
                                       "AND created_at < ?", (_utc_stamp(start), _utc_stamp(end)), label="sms_log",
                                 optional=True) or {}).get("c")
                usage[k] = {**{f"usage:{v}": round(c, 2) for v, c in ai.items()},
                            **({"usage:twilio_sms": round(float(sms), 2)} if sms else {})}
        finally:
            conn.close()
    by_month = []
    for k in keys:
        fixed = {r["vendor"]: float(r["amount_usd"] or 0) for r in rows if r["month"] == k}
        use = usage.get(k) or {}
        by_month.append({"month": k, "entered": fixed, "usage": use,
                         "entered_total": round(sum(fixed.values()), 2), "usage_total": round(sum(use.values()), 2),
                         "total": round(sum(fixed.values()) + sum(use.values()), 2)})
    return {"ok": True, "vendors": list(VENDORS), "rows": rows, "by_month": by_month,
            "note": "Entered costs are the operator's; usage is metered by the platform. Neither is summed into MRR.",
            **_merge_problems(bucket)}


def set_vendor_cost(vendor, month, amount_usd, note=None, actor="admin"):
    """Enter or change one vendor's cost for one month — audited."""
    vendor = (vendor or "").strip().lower()
    if vendor not in VENDORS:
        return {"ok": False, "error": f"Unknown vendor. One of: {', '.join(VENDORS)}."}
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(month or "")):
        return {"ok": False, "error": "month must be YYYY-MM"}
    try:
        amount = round(float(amount_usd), 2)
    except (TypeError, ValueError):
        return {"ok": False, "error": "amount_usd must be a number"}
    if amount < 0 or amount > 1_000_000:
        return {"ok": False, "error": "amount_usd must be between 0 and 1,000,000"}
    conn = get_conn()
    try:
        before = _one_dict(conn, "SELECT amount_usd FROM vendor_costs WHERE vendor=? AND month=?", (vendor, month))
        conn.execute("INSERT INTO vendor_costs (vendor, month, amount_usd, note, updated_by) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(vendor, month) DO UPDATE SET amount_usd=excluded.amount_usd, note=excluded.note, "
                     "updated_by=excluded.updated_by, updated_at=datetime('now')",
                     (vendor, month, amount, (note or "")[:300] or None, actor))
        conn.commit()
    finally:
        conn.close()
    try:
        import admin_events
        admin_events.record("admin", "vendor_cost.set", amount=amount,
                            summary=f"{vendor} {month} set to ${amount:,.2f} by {actor}"
                                    + (f" (was ${float(before['amount_usd']):,.2f})" if before else ""))
    except Exception:
        pass
    return {"ok": True, "vendor": vendor, "month": month, "amount_usd": amount}


# ── the daily business snapshot (#18) ──────────────────────────────────────

def snapshot_business_metrics(db_path=None, day=None):
    """Write the operator day's business_metrics_daily row: list, billed and
    committed MRR, paying accounts, trials, past due, signups, activations,
    churns, active users and cost by vendor — plus each paying account's
    churn-risk level and since when (account_risk_state). Idempotent: a
    second run replaces the day's row. Set-based (one fleet build, no query
    per restaurant). Refuses to write on a build with query errors, so a
    locked database never becomes a day of zeros. Returns the sweep counts
    ops.run_outcome reads. Scheduled by the integration wave (daily, late
    evening Central)."""
    with _using_db(db_path):
        with _collecting() as bucket:
            recs, d = _records()
        if bucket["errors"]:
            return {"attempted": 1, "ok": 0, "failed": 1, "skipped": 0, "hit_bound": False,
                    "error": "; ".join(f"{e['query']}: {e['error']}" for e in bucket["errors"][:3])}
        now = _utcnow()
        today_ct = _now_ct().date()
        day = day or today_ct.isoformat()
        real = _real(recs)
        mrr = _mrr_totals(recs)

        def ids(pred):
            return sorted(r["id"] for r in real if pred(r))
        state = {"paying": ids(lambda r: r["billing"]["status"] in PAYING_STATES and not r["billing"]["billed_by"]),
                 "trial": ids(lambda r: r["billing"]["status"] in ("trial", "pending", "")),
                 "past_due": ids(lambda r: r["billing"]["status"] == "past_due"),
                 "ended": ids(lambda r: r["billing"]["status"] in ENDED_STATES)}
        conn = get_conn()
        try:
            prev = _one_dict(conn, "SELECT state_json FROM business_metrics_daily WHERE date < ? ORDER BY date DESC LIMIT 1",
                             (day,), label="business_metrics_daily") or {}
            try:
                prev_state = json.loads(prev.get("state_json") or "{}") or {}
            except (TypeError, ValueError):
                prev_state = {}
            day_start = datetime.combine(datetime.fromisoformat(day).date(), datetime.min.time(),
                                         tzinfo=_now_ct().tzinfo)
            day_end = day_start + timedelta(days=1)
            in_day = lambda s: (lambda x: x is not None and day_start <= x < day_end)(_parse_utc(s, "UTC"))
            signups = sum(1 for r in real if in_day(r["created_at"]))
            if d.get("has_converted_at"):
                activations = sum(1 for r in real if in_day(r["billing"].get("converted_at")))
            else:
                activations = len(set(state["paying"]) - set(prev_state.get("paying") or [])) if prev_state else None
            churns = (len(set(prev_state.get("paying") or []) - set(state["paying"]) - set(state["past_due"]))
                      if prev_state else None)
            users_1d = (_one_dict(conn, f"""SELECT COUNT(DISTINCT u.id) AS n FROM users u
                    WHERE COALESCE(u.is_admin,0)=0 AND {_OWNER_ROLE_SQL} AND (
                      EXISTS (SELECT 1 FROM sessions s WHERE s.user_id=u.id
                              AND COALESCE(s.device_type,'') NOT IN ('staff_pin','admin-view-as')
                              AND julianday(s.last_active) >= julianday(?))
                      OR EXISTS (SELECT 1 FROM login_history l WHERE l.user_id=u.id AND l.event='login'
                              AND COALESCE(l.device_type,'') NOT IN ('staff_pin','admin-view-as')
                              AND julianday(l.created_at) >= julianday(?)))""",
                                  (_utc_stamp(now - timedelta(days=1)),) * 2, label="users") or {}).get("n")
            users_7d = (_one_dict(conn, f"""SELECT COUNT(DISTINCT u.id) AS n FROM users u
                    WHERE COALESCE(u.is_admin,0)=0 AND {_OWNER_ROLE_SQL} AND (
                      EXISTS (SELECT 1 FROM sessions s WHERE s.user_id=u.id
                              AND COALESCE(s.device_type,'') NOT IN ('staff_pin','admin-view-as')
                              AND julianday(s.last_active) >= julianday(?))
                      OR EXISTS (SELECT 1 FROM login_history l WHERE l.user_id=u.id AND l.event='login'
                              AND COALESCE(l.device_type,'') NOT IN ('staff_pin','admin-view-as')
                              AND julianday(l.created_at) >= julianday(?)))""",
                                  (_utc_stamp(now - timedelta(days=7)),) * 2, label="users") or {}).get("n")
            costs = _ai_cost_by_vendor(conn, _utc_stamp(day_start), _utc_stamp(day_end))
            sms = (_one_dict(conn, "SELECT ROUND(SUM(COALESCE(cost_usd,0)),4) AS c FROM sms_log WHERE created_at >= ? "
                                   "AND created_at < ?", (_utc_stamp(day_start), _utc_stamp(day_end)), label="sms_log",
                             optional=True) or {}).get("c")
            if sms:
                costs["twilio_sms"] = float(sms)
            fixed = vendor_costs_month(day[:7])
            active_7d = sum(1 for r in real if (_age_days(r["last_active"], "UTC") or 999) <= 7)
            conn.execute("""INSERT OR REPLACE INTO business_metrics_daily
                (date, generated_at, list_mrr, billed_mrr, committed_mrr, mrr, mrr_source, subscriptions,
                 paying_accounts, trial_accounts, past_due_accounts, paused_accounts, canceled_accounts,
                 internal_accounts, signups, activations, churns, active_accounts_7d, active_users_1d, active_users_7d,
                 ai_cost_usd, cost_by_vendor_json, vendor_costs_month_json, state_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (day, _utc_stamp(now), mrr["mrr_list"], mrr["mrr_billed"], mrr["mrr_committed"], mrr["mrr"],
                          mrr["mrr_source"], mrr["subscriptions"], len(state["paying"]), len(state["trial"]),
                          len(state["past_due"]), sum(1 for r in real if r["billing"]["status"] == "paused"),
                          len(state["ended"]), sum(1 for r in recs if r["segment"] == "internal"),
                          signups, activations, churns, active_7d, users_1d, users_7d,
                          round(sum(v for k, v in costs.items() if k != "twilio_sms"), 4),
                          json.dumps({k: round(v, 4) for k, v in costs.items()}), json.dumps(fixed["by_vendor"]),
                          json.dumps(state)))
            # Churn risk level and since when (#83): unchanged level keeps its
            # 'since'; a new level starts now.
            stamp = _utc_stamp(now)
            for r in real:
                c = r["churn_risk"]
                if not c.get("scored"):
                    conn.execute("DELETE FROM account_risk_state WHERE restaurant_id=?", (r["id"],))
                    continue
                prior = (d.get("risk_state") or {}).get(r["id"]) or {}
                since = prior.get("since") if prior.get("level") == c["level"] else stamp
                conn.execute("INSERT OR REPLACE INTO account_risk_state (restaurant_id, level, since, points, "
                             "reasons_json, updated_at) VALUES (?,?,?,?,?,?)",
                             (r["id"], c["level"], since, c.get("points"), json.dumps(c.get("reasons") or []), stamp))
            conn.commit()
        finally:
            conn.close()
    return {"attempted": 1, "ok": 1, "failed": 0, "skipped": 0, "hit_bound": False, "date": day,
            "mrr": mrr["mrr"], "paying": len(state["paying"]), "signups": signups}


def business_metrics(days=90):
    """The daily snapshots (#18), oldest first — the history MRR, account and
    cost trends are drawn from. Nothing before the first snapshot exists."""
    days = max(1, min(int(days or 90), 1000))
    floor = (_now_ct().date() - timedelta(days=days)).isoformat()
    with _collecting() as bucket:
        conn = get_conn()
        try:
            rows = _rows_dict(conn, "SELECT * FROM business_metrics_daily WHERE date >= ? ORDER BY date", (floor,),
                              label="business_metrics_daily")
        finally:
            conn.close()
    for r in rows:
        r.pop("state_json", None)
        for k in ("cost_by_vendor_json", "vendor_costs_month_json"):
            try:
                r[k[:-5]] = json.loads(r.pop(k) or "{}")
            except (TypeError, ValueError):
                r[k[:-5]] = {}
    return {"ok": True, "days": days, "rows": rows,
            "first_date": rows[0]["date"] if rows else None,
            "note": "History accrues from the first day the snapshot ran; nothing before it is reconstructed.",
            **_merge_problems(bucket)}


# ── the slim reads the console polls and pages (#36, #79) ──────────────────

def badges():
    """The rail's counts. Served from the fleet memo — up to
    BADGES_MAX_AGE_SECONDS old — without starting a build of its own when
    one exists; `generated_at` says how old the counts are (#32, #36)."""
    with _collecting() as bucket:
        m = _fleet_memo(max_age=BADGES_MAX_AGE_SECONDS) if _in_request() else None
        recs, d, meta = _served(m) if m else _records_cached()
        issues, facts = _all_issues(recs, d)
    counts = _issue_counts(issues)
    real = _real(recs)
    return {"ok": True, "counts": counts, "attention": counts["attention"], "critical": counts["critical"],
            "kpis": {"scheduler_heartbeat_minutes": facts.get("heartbeat"), "jobs_overdue": facts.get("jobs_overdue", 0),
                     "job_failures_24h": (facts.get("failures_24h") or {}).get("job", 0),
                     "past_due": sum(1 for r in real if r["billing"]["status"] == "past_due"),
                     "deletion_requests": sum(1 for r in recs if r.get("deletion")),
                     "clients": len(real)},
            **_payload_meta(meta, bucket)}


def _page(items, page, per_page):
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = max(1, min(int(per_page or 50), 200))
    except (TypeError, ValueError):
        per_page = 50
    total = len(items)
    return items[(page - 1) * per_page: page * per_page], page, per_page, total, max(1, -(-total // per_page))


def issues_page(segment="attention", severity=None, q=None, restaurant_id=None, category=None, page=1,
                per_page=50, sort="severity"):
    """Every open issue, filtered, sorted and paged on the server, with the
    counts of the FULL set per segment (#79, #141). segment: attention
    (customer + platform, the default) | customer | platform | internal | all."""
    with _collecting() as bucket:
        recs, d, meta = _records_cached()
        issues, _facts = _all_issues(recs, d)
    counts = _issue_counts(issues)
    seg = (segment or "attention").lower()
    items = [i for i in issues if seg == "all" or (seg == "attention" and i.get("segment") != "internal")
             or i.get("segment") == seg]
    if severity in ("critical", "warning"):
        items = [i for i in items if i["severity"] == severity]
    if restaurant_id:
        items = [i for i in items if i.get("restaurant_id") == int(restaurant_id)]
    if category:
        items = [i for i in items if i.get("category") == category]
    if q:
        ql = q.strip().lower()
        items = [i for i in items if ql in " ".join(str(i.get(k) or "") for k in
                                                     ("title", "detail", "restaurant", "brand", "owner", "key")).lower()]
    if sort == "age":
        items.sort(key=lambda i: i.get("since_at") or "9999")
    elif sort == "restaurant":
        items.sort(key=lambda i: ((i.get("restaurant") or "").lower(), -i["severity_rank"]))
    rows, page, per_page, total, pages = _page(items, page, per_page)
    return {"ok": True, "items": rows, "page": page, "per_page": per_page, "total": total, "pages": pages,
            "counts": counts, "filters": {"segment": seg, "severity": severity, "q": q, "restaurant_id": restaurant_id,
                                          "category": category, "sort": sort},
            **_payload_meta(meta, bucket)}


def _slim(r):
    b = r["billing"]
    return {"id": r["id"], "name": r["name"], "brand": r["brand"], "location_name": r["location_name"],
            "city": r["city"], "segment": r["segment"], "is_demo": r["is_demo"], "health": r["health"],
            "status": b["status"], "monthly": b["monthly"], "mrr_source": b["mrr_source"],
            "billed_by": b["billed_by"], "owner": (r["owner"] or {}).get("username"), "owner_email": r["owner_email"],
            "last_active": r["last_active"], "on_ios": r.get("on_ios"), "created_at": r["created_at"],
            "issues": len(r["issues"]), "critical": sum(1 for i in r["issues"] if i["severity"] == "critical"),
            "churn": (r["churn_risk"] or {}).get("level"), "setup": (r["setup_completeness"] or {}).get("score"),
            "integration_health": r["integration_health"], "onboarding": f"{r['onboarding']['done']}/{r['onboarding']['total']}",
            "deletion_due": (r.get("deletion") or {}).get("due_at")}


def clients_page(q=None, health=None, segment="customer", status=None, sort="name", page=1, per_page=50,
                 churn=None, has_issues=None, joined_days=None, inactive_days=None):
    """The fleet list, filtered, sorted and paged on the server as slim rows
    (#79) — the full records are ~8 KB each. Counts come from the full set.

    The filters the console had in the browser before it paged on the
    server (UI-1 request 4): `churn` (a churn-risk level, e.g. "high"),
    `has_issues` (open issues), `joined_days` (created within that many
    days) and `inactive_days` (no owner activity in that many days, or
    never)."""
    with _collecting() as bucket:
        recs, d, meta = _records_cached()
    seg = (segment or "customer").lower()
    base = [r for r in recs if seg == "all" or r["segment"] == seg]
    counts = {"health": {}, "status": {}, "segment": {}}
    for r in recs:
        counts["segment"][r["segment"]] = counts["segment"].get(r["segment"], 0) + 1
    for r in base:
        counts["health"][r["health"]] = counts["health"].get(r["health"], 0) + 1
        counts["status"][r["billing"]["status"]] = counts["status"].get(r["billing"]["status"], 0) + 1
    items = base
    if health:
        items = [r for r in items if r["health"] == health]
    if status:
        items = [r for r in items if r["billing"]["status"] == status]
    if churn:
        items = [r for r in items if ((r.get("churn_risk") or {}).get("level") or "") == str(churn).lower()]
    if has_issues not in (None, "", "0", 0, False, "false"):
        items = [r for r in items if r["issues"]]
    if joined_days:
        items = [r for r in items if (_age_days(r["created_at"], "UTC") is not None
                                      and _age_days(r["created_at"], "UTC") <= int(joined_days))]
    if inactive_days:
        items = [r for r in items if (_age_days(r["last_active"], "UTC") is None
                                      or _age_days(r["last_active"], "UTC") > int(inactive_days))]
    if q:
        ql, qd = q.strip().lower(), _DIGITS.sub("", q)
        items = [r for r in items if ql in " ".join(str(x or "") for x in (
            r["name"], r["brand"], r["location_name"], r["city"], r["owner_email"], r["owner_name"],
            (r["owner"] or {}).get("username"), r["billing"].get("stripe_customer_id"))).lower()
            or (len(qd) >= 7 and qd[-10:] in _DIGITS.sub("", r.get("owner_phone") or ""))
            or ql == str(r["id"])]
    rank = {"critical": 0, "warning": 1, "healthy": 2, "inactive": 3}
    keyf = {"health": lambda r: (rank.get(r["health"], 4), (r["name"] or "").lower()),
            "mrr": lambda r: (-(r["billing"]["monthly"] or 0), (r["name"] or "").lower()),
            "last_active": lambda r: (r["last_active"] or ""),
            "created": lambda r: (r["created_at"] or "")}.get(sort, lambda r: ((r["name"] or "").lower(), r["id"]))
    items = sorted(items, key=keyf, reverse=(sort in ("last_active", "created")))
    rows, page, per_page, total, pages = _page(items, page, per_page)
    return {"ok": True, "items": [_slim(r) for r in rows], "page": page, "per_page": per_page, "total": total,
            "pages": pages, "counts": counts, **_payload_meta(meta, bucket)}


def onboarding_list():
    """The Onboarding tab: in-service real accounts that are not done (#152)
    — never a churned, paused, internal or admin-home account."""
    with _collecting() as bucket:
        recs, d, meta = _records_cached()
    blocked = set(ENDED_STATES) | {"paused"}
    rows = []
    for r in recs:
        if r["segment"] != "customer" or r["billing"]["status"] in blocked or r["onboarding"]["complete"]:
            continue
        rows.append({**_slim(r), "steps": r["onboarding"]["steps"], "done": r["onboarding"]["done"],
                     "total": r["onboarding"]["total"], "owner_hid_card": r["onboarding"]["dismissed"],
                     "days_since_signup": int(_age_days(r["created_at"], "UTC") or 0),
                     # The contract is the first onboarding step (UI-1 request 3).
                     "contract_status": r["billing"].get("contract_status"),
                     "signed_at": r["billing"].get("signed_at")})
    rows.sort(key=lambda x: (x["done"] / max(1, x["total"]), -x["days_since_signup"]))
    return {"ok": True, "rows": rows, **_payload_meta(meta, bucket)}


# Module adoption (#71): owner USE of a module in the last 28 days — a
# screen view on the web, or an action the module exists for (an approved
# reply, a published schedule, a count or an applied invoice, a real post),
# which is how iOS use shows. Entitlement flags default to on and say
# nothing about use.
ADOPTION_DAYS = 28
_MODULE_TABS = {"reviews": ("reviews",), "labor": ("labor",), "inventory": ("inventory",),
                "marketing": ("marketing",), "intel": ("competitor",)}


def adoption(days=ADOPTION_DAYS):
    days = max(1, min(int(days or ADOPTION_DAYS), 90))
    with _collecting() as bucket:
        recs, d, meta = _records_cached()
        since = _utc_stamp(_utcnow() - timedelta(days=days))
        since_ct = (_now_ct() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        used = {m: set() for m in _MODULE_TABS}
        conn = get_conn()
        try:
            tab_of = {t: m for m, ts in _MODULE_TABS.items() for t in ts}
            for row in _rows_dict(conn, "SELECT restaurant_id, json_extract(event_data,'$.tab') AS tab FROM activity_log "
                                        "WHERE event_type='tab_view' AND julianday(created_at) >= julianday(?) "
                                        "GROUP BY restaurant_id, tab", (since_ct,), label="activity_log"):
                m = tab_of.get(row.get("tab"))
                if m:
                    used[m].add(row["restaurant_id"])
            for row in _rows_dict(conn, "SELECT DISTINCT restaurant_id FROM activity_log WHERE event_type IN "
                                        "('review_approved','reviews_bulk_approved','review_undo') "
                                        "AND julianday(created_at) >= julianday(?)", (since_ct,), label="activity_log"):
                used["reviews"].add(row["restaurant_id"])
            for row in _rows_dict(conn, "SELECT DISTINCT restaurant_id FROM schedule_history WHERE published_at IS NOT NULL "
                                        "AND julianday(published_at) >= julianday(?)", (since,), label="schedule_history",
                                  optional=True):
                used["labor"].add(row["restaurant_id"])
            for row in _rows_dict(conn, "SELECT DISTINCT restaurant_id FROM ingredients WHERE "
                                        "julianday(last_recount_at) >= julianday(?)", (since_ct,), label="ingredients"):
                used["inventory"].add(row["restaurant_id"])
            for row in _rows_dict(conn, "SELECT DISTINCT restaurant_id FROM invoice_imports WHERE applied_at IS NOT NULL "
                                        "AND julianday(applied_at) >= julianday(?)", (since,), label="invoice_imports",
                                  optional=True):
                used["inventory"].add(row["restaurant_id"])
            try:
                from marketing import REAL_PIECE_SQL
                for row in _rows_dict(conn, f"SELECT DISTINCT restaurant_id FROM marketing_content_log WHERE {REAL_PIECE_SQL} "
                                            "AND julianday(created_at) >= julianday(?)", (since,),
                                      label="marketing_content_log"):
                    used["marketing"].add(row["restaurant_id"])
            except Exception as e:
                _note_failure("marketing_content_log", e)
        finally:
            conn.close()
    real = _real(recs)
    out = []
    for m in _MODULE_TABS:
        row = {"module": m}
        for group, pred in (("paying", lambda r: r["billing"]["status"] in PAYING_STATES),
                            ("trial", lambda r: r["billing"]["status"] in ("trial", "pending", ""))):
            entitled = [r for r in real if pred(r) and any(x["key"] == m and x["enabled"] for x in r["modules"])]
            using = [r for r in entitled if r["id"] in used[m]]
            row[group] = {"entitled": len(entitled), "using": len(using),
                          "rate_pct": round(100.0 * len(using) / len(entitled), 1) if entitled else None}
        out.append(row)
    return {"ok": True, "days": days, "modules": out, "trial_conversion": _trial_conversion(recs, d),
            "basis": ("owner use in the window: a web screen view, or an action the module exists for (an approved "
                      "reply, a published schedule, a count or an applied invoice, a real post). iOS screen views are "
                      "not recorded, so iOS use counts only through those actions."),
            **_payload_meta(meta, bucket)}


# Durable queues (#75): counts by status, the oldest item still waiting and
# 24 hours of failures, for every queue the platform keeps in the database.
_QUEUES = (
    {"key": "guest_campaign_queue", "label": "Guest campaign texts", "table": "guest_campaign_queue",
     "pending": ("pending", "claimed", "sending"), "failed": ("failed",),
     "oldest": "SELECT MIN(c.created_at) AS at FROM guest_campaign_queue q JOIN guest_campaigns c ON c.id=q.campaign_id "
               "WHERE q.status IN ({p})", "failed_at": "COALESCE(sent_at, claimed_at)"},
    {"key": "delayed_actions", "label": "Actions with an undo window", "table": "delayed_actions",
     "pending": ("pending", "running"), "failed": ("failed",), "created": "created_at", "failed_at": "executed_at"},
    {"key": "alert_holds", "label": "Alerts held through service", "table": "alert_holds", "status_sql":
     "CASE WHEN sent_at IS NULL THEN 'held' ELSE 'released' END", "pending": ("held",), "failed": (),
     "created": "created_at", "failed_at": "sent_at"},
    {"key": "review_requests", "label": "Review requests", "table": "review_requests",
     "pending": ("pending", "queued"), "failed": ("failed",), "created": "sent_at", "failed_at": "sent_at"},
    {"key": "dsr_held_pushes", "label": "DSR pushes held through quiet hours", "table": "dsr_deliveries",
     "where": "channel='push'", "pending": ("held", "sending"), "failed": ("failed", "expired"),
     "created": "created_at", "failed_at": "created_at"},
    # E's outboxes (#75): a push or an outbound webhook is written 'queued'
    # before it is handed to its pool, 'delivering' while it is, and 'sent',
    # 'failed' or 'expired' (a restart left it too old to deliver) after.
    # push_deliveries / webhook_deliveries stay the ledgers of attempts.
    {"key": "push_outbox", "label": "Push notifications", "table": "push_outbox",
     "state_cols": ("state",), "pending": ("queued", "delivering"), "failed": ("failed", "expired"),
     "created": "created_at", "failed_at": "COALESCE(done_at, updated_at, created_at)"},
    {"key": "webhook_outbox", "label": "Outbound webhooks", "table": "webhook_outbox",
     "state_cols": ("state",), "pending": ("queued", "delivering"), "failed": ("failed", "expired"),
     "created": "created_at", "failed_at": "COALESCE(done_at, updated_at, created_at)"},
    # H's owed billing mail (#12): pending until sent, 'sending' while a
    # drain holds it, 'failed' once it gave up.
    {"key": "owed_sends", "label": "Owed billing email", "table": "owed_sends",
     "pending": ("pending", "sending"), "failed": ("failed",), "created": "created_at",
     "failed_at": "updated_at"},
)


def queues():
    with _collecting() as bucket:
        conn = get_conn()
        out = []
        day = _utc_stamp(_utcnow() - timedelta(days=1))
        try:
            for spec in _QUEUES:
                cols = _columns(conn, spec["table"])
                row = {"key": spec["key"], "label": spec["label"], "available": bool(cols)}
                status = spec.get("status_sql")
                if not status:
                    if "state_cols" in spec:
                        status = next((c for c in spec["state_cols"] if c in cols), None)
                    elif "status" in cols:
                        status = "status"
                if not cols or not status:
                    row.update(available=False, note="not recorded in this database yet")
                    out.append(row)
                    continue
                where = f"WHERE {spec['where']}" if spec.get("where") else ""
                counts = {r["s"]: r["n"] for r in _rows_dict(conn, f"SELECT {status} AS s, COUNT(*) AS n FROM {spec['table']} "
                                                                  f"{where} GROUP BY 1", label=spec["table"])}
                p = ",".join(f"'{x}'" for x in spec["pending"])
                if spec.get("oldest"):
                    oldest = (_one_dict(conn, spec["oldest"].format(p=p), label=spec["table"]) or {}).get("at")
                elif spec.get("created"):
                    extra = f" AND {spec['where']}" if spec.get("where") else ""
                    oldest = (_one_dict(conn, f"SELECT MIN({spec['created']}) AS at FROM {spec['table']} "
                                              f"WHERE {status} IN ({p}){extra}", label=spec["table"]) or {}).get("at")
                else:
                    oldest = None
                failed_24h = 0
                if spec["failed"]:
                    f = ",".join(f"'{x}'" for x in spec["failed"])
                    extra = f" AND {spec['where']}" if spec.get("where") else ""
                    failed_24h = (_one_dict(conn, f"SELECT COUNT(*) AS n FROM {spec['table']} WHERE {status} IN ({f}) "
                                                  f"AND {spec['failed_at']} >= ?{extra}", (day,),
                                            label=spec["table"]) or {}).get("n") or 0
                row.update(counts=counts, pending=sum(counts.get(s, 0) for s in spec["pending"]),
                           oldest_pending_at=_iso_z(oldest, "UTC"),
                           oldest_pending_age_hours=(round(_age_hours(oldest, "UTC"), 1) if oldest else None),
                           failed_24h=failed_24h)
                out.append(row)
        finally:
            conn.close()
    return {"ok": True, "queues": out, **_merge_problems(bucket)}


def data_sources(segment="customer"):
    """Every data source's sync state per restaurant (#46): failures in a
    row, since when, the last error, the next retry — failing first."""
    with _collecting() as bucket:
        recs, d, meta = _records_cached()
    try:
        import data_health as _dh
        labels = getattr(_dh, "OWNER_LABEL", {}) or {}
    except Exception:
        labels = {}
    rows, summary = [], {}
    for rec in recs:
        if segment != "all" and rec["segment"] != segment:
            continue
        for s in (d.get("source_health") or {}).get(rec["id"], []):
            n = int(s.get("consecutive_failures") or 0)
            rows.append({"restaurant_id": rec["id"], "restaurant": rec["name"], "brand": rec["brand"],
                         "source": s["source"], "label": labels.get(s["source"], s["source"]),
                         "provider": s.get("provider"), "failures": n, "state": "failing" if n else "ok",
                         "since": _iso_z(s.get("first_failed_at"), "UTC"), "last_error": (s.get("last_error") or "")[:200],
                         "error_class": s.get("error_class"), "next_retry_at": _iso_z(s.get("next_retry_at"), "UTC"),
                         "last_ok_at": _iso_z(s.get("last_ok_at"), "UTC"),
                         "last_attempt_at": _iso_z(s.get("last_attempt_at"), "UTC"), "data_through": s.get("data_through")})
            agg = summary.setdefault(s["source"], {"source": s["source"], "label": labels.get(s["source"], s["source"]),
                                                   "restaurants": 0, "failing": 0})
            agg["restaurants"] += 1
            agg["failing"] += 1 if n else 0
    rows.sort(key=lambda x: (-x["failures"], (x["restaurant"] or "").lower(), x["source"]))
    real = _real(recs)
    return {"ok": True, "rows": rows, "summary": sorted(summary.values(), key=lambda x: (-x["failing"], x["source"])),
            "data_health_median": _median([r["data_health"]["overall"] for r in real
                                           if r["data_health"].get("overall") is not None and not r["data_health"].get("stale")]),
            "stale_snapshots": sum(1 for r in real if r["data_health"].get("stale")),
            "issue_threshold": SOURCE_FAIL_ISSUE, **_payload_meta(meta, bucket)}


# One client's history on one line (#54): email, SMS, push, alerts, sign-ins
# (a staff PIN, a failed PIN and a lockout each say so), Stripe/DocuSign/
# admin events, job failures, AI failures, webhook deliveries and support
# notes — newest first, filterable by type, paged by `before`.
TIMELINE_TYPES = ("email", "sms", "push", "alert", "login", "billing", "admin", "job", "ai", "webhook", "note",
                  "activity")


def client_timeline(rid, types=None, limit=100, before=None):
    want = {t for t in (types or TIMELINE_TYPES) if t in TIMELINE_TYPES}
    limit = max(1, min(int(limit or 100), 500))
    cut = _parse_utc(before, "UTC") if before else None
    ev = []
    with _collecting() as bucket:
        conn = get_conn()
        try:
            if not _one_dict(conn, "SELECT id FROM restaurants WHERE id=?", (rid,)):
                return {"ok": False, "error": "Not found"}
            n = limit * 2

            def add(at, zone, kind, label, tone="neutral", detail=None, **extra):
                z = _iso_z(at, zone)
                if z and (cut is None or (_parse_utc(z, "UTC") or cut) < cut):
                    ev.append({"at": z, "type": kind, "label": label, "tone": tone, "detail": detail, **extra})
            if "email" in want:
                for e in _rows_dict(conn, "SELECT email_type, to_email, subject, sent_at, status, error FROM email_log "
                                          "WHERE restaurant_id=? ORDER BY id DESC LIMIT ?", (rid, n)):
                    bad = e["status"] in ("failed", "bounced", "complained")
                    add(e["sent_at"], "UTC", "email", f"Email {e['status'] or 'sent'} · {e['email_type']}",
                        "bad" if bad else "neutral", e["error"] if bad else e["subject"], to=e["to_email"])
            if "sms" in want:
                for s in _rows_dict(conn, "SELECT use_case, to_last4, status, error, created_at FROM sms_log "
                                          "WHERE restaurant_id=? ORDER BY id DESC LIMIT ?", (rid, n), optional=True):
                    bad = (s.get("status") or "") in ("failed", "undelivered", "error")
                    add(s["created_at"], "UTC", "sms", f"Text {s.get('status') or 'sent'} · {s.get('use_case') or 'sms'}",
                        "bad" if bad else "neutral", s.get("error"), to_last4=s.get("to_last4"))
            if "push" in want:
                for p in _rows_dict(conn, "SELECT alert_type, ok, error, created_at FROM push_deliveries WHERE restaurant_id=? "
                                          "ORDER BY id DESC LIMIT ?", (rid, n)):
                    add(p["created_at"], "UTC", "push", f"Push {'delivered' if p['ok'] else 'failed'} · {p['alert_type']}",
                        "neutral" if p["ok"] else "bad", p["error"])
            if "alert" in want:
                for a in _rows_dict(conn, "SELECT alert_type, fired_at FROM alert_log WHERE restaurant_id=? "
                                          "ORDER BY id DESC LIMIT ?", (rid, n)):
                    add(a["fired_at"], "UTC", "alert", f"Alert fired · {a['alert_type']}", "warn")
            if "login" in want:
                for l in _rows_dict(conn, "SELECT event, device_type, ip_address, created_at FROM login_history "
                                          "WHERE restaurant_id=? ORDER BY id DESC LIMIT ?", (rid, n)):
                    add(l["created_at"], "UTC", "login", _login_label(l.get("event"), l.get("device_type")),
                        _LOGIN_TONE.get(l.get("event"), "neutral"), None, event=l.get("event") or "login",
                        ip_address=l.get("ip_address"))
            if want & {"billing", "admin"}:
                for e in _rows_dict(conn, "SELECT source, event_type, amount, summary, created_at FROM admin_events "
                                          "WHERE restaurant_id=? ORDER BY id DESC LIMIT ?", (rid, n)):
                    kind = "billing" if e["source"] in ("stripe", "docusign") else "admin"
                    if kind in want:
                        bad = any(x in (e["event_type"] or "") for x in ("failed", "deleted", "canceled", "dispute"))
                        add(e["created_at"], "UTC", kind, f"{e['source'].capitalize()} · {e['summary'] or e['event_type']}",
                            "bad" if bad else "neutral", amount=e.get("amount"))
            if "job" in want:
                jobs, _runs, quality = _client_job_rows(conn, rid, None)
                for j in jobs:
                    add(j["created_at"], "UTC", "job", f"Job failed · {j['job']}", "bad", (j["error"] or "")[:200])
                for j in quality:
                    add(j["created_at"], "UTC", "ai", f"AI output check · {j['job']}", "warn", (j["error"] or "")[:200])
            if "ai" in want:
                for a in _rows_dict(conn, "SELECT action, model, error, created_at FROM ai_usage WHERE restaurant_id=? "
                                          "AND COALESCE(status,'ok')='error' ORDER BY id DESC LIMIT ?", (rid, n),
                                    optional=True):
                    add(a["created_at"], "UTC", "ai", f"AI call failed · {a['action']}", "bad", (a["error"] or "")[:200])
            if "webhook" in want:
                for h in _rows_dict(conn, "SELECT event_type, ok, error, created_at FROM webhook_deliveries "
                                          "WHERE restaurant_id=? ORDER BY id DESC LIMIT ?", (rid, n)):
                    add(h["created_at"], "UTC", "webhook", f"Webhook {'delivered' if h['ok'] else 'failed'} · {h['event_type']}",
                        "neutral" if h["ok"] else "bad", h["error"])
            if "note" in want:
                for s in _rows_dict(conn, "SELECT author, body, created_at FROM support_notes WHERE restaurant_id=? "
                                          "ORDER BY id DESC LIMIT ?", (rid, n), optional=True):
                    add(s["created_at"], "UTC", "note", f"Note by {s.get('author') or 'admin'}", "neutral",
                        (s.get("body") or "")[:300])
            if "activity" in want:
                for a in _rows_dict(conn, "SELECT event_type, created_at FROM activity_log WHERE restaurant_id=? "
                                          "AND event_type != 'tab_view' ORDER BY id DESC LIMIT ?", (rid, n)):
                    label = _ACTIVITY_LABELS.get(a["event_type"]) or (a["event_type"] or "").replace("_", " ").capitalize()
                    add(a["created_at"], OPERATOR_TZ, "activity", label)
        finally:
            conn.close()
    ev.sort(key=lambda e: e["at"], reverse=True)
    page = ev[:limit]
    return {"ok": True, "restaurant_id": rid, "types": sorted(want), "events": page,
            "next_before": page[-1]["at"] if len(ev) > limit else None, **_merge_problems(bucket)}


def billing_live(rid):
    """One client's subscriptions straight from Stripe — for its own billing
    tab only, never the fleet list (#37). An error is this client's alone.
    Also says whether the local mirror disagrees."""
    r = get_restaurant(rid)
    if not r:
        return {"ok": False, "error": "Not found"}
    key = os.getenv("STRIPE_SECRET_KEY", "")
    cid = getattr(r, "stripe_customer_id", None)
    if not key:
        return {"ok": True, "configured": False, "subscriptions": [], "note": "STRIPE_SECRET_KEY is not set here."}
    if not cid:
        return {"ok": True, "configured": True, "subscriptions": [], "note": "No Stripe customer on file."}
    try:
        import config as _config
        _stripe = _config.stripe_api(key)
        subs = _stripe.Subscription.list(customer=cid, status="all", limit=5)
        out = []
        for s in (subs.get("data") if hasattr(subs, "get") else subs["data"]) or []:
            item = ((s.get("items") or {}).get("data") or [None])[0]
            price = (item or {}).get("price") or {}
            out.append({"id": s.get("id"), "status": s.get("status"),
                        "amount": (price.get("unit_amount") / 100.0 if price.get("unit_amount") is not None else None),
                        "interval": (price.get("recurring") or {}).get("interval"),
                        "quantity": (item or {}).get("quantity"),
                        "trial_end": _stamp_any(s.get("trial_end")),
                        "current_period_end": _stamp_any(s.get("current_period_end")),
                        "cancel_at_period_end": s.get("cancel_at_period_end"),
                        "canceled_at": _stamp_any(s.get("canceled_at")),
                        "module_keys": (s.get("metadata") or {}).get("module_keys")})
    except Exception as e:
        return {"ok": False, "configured": True, "error": f"Stripe: {str(e)[:200]}"}
    conn = get_conn()
    try:
        mirror = _one_dict(conn, "SELECT * FROM stripe_subscriptions WHERE restaurant_id=?", (rid,),
                           label="stripe_subscriptions", optional=True) or {}
    finally:
        conn.close()
    live_first = next((s for s in out if s["id"] == mirror.get("subscription_id")), out[0] if out else None)
    return {"ok": True, "configured": True, "customer_id": cid, "subscriptions": out,
            "mirror": {"subscription_id": mirror.get("subscription_id"), "status": mirror.get("status")} if mirror else None,
            "mirror_stale": bool(mirror and live_first and (mirror.get("status") or None) not in (None, live_first["status"]))}


# ── what the read-only support role may see (#87) ───────────────────────────
#
# A support login reads the console but must not carry away tenants'
# contact details, sign-in IPs and devices, or Stripe identifiers. The
# payloads are redacted on the way out (admin_routes registers the hook for
# every GET under /admin/api/ answered to a support viewer): known fields are
# masked by name, and an email address or a Stripe id anywhere in free text
# is masked too.

_EMAIL_KEYS = {"owner_email", "email", "to_email", "customer_email", "recipient", "to", "contact_email"}
_PHONE_KEYS = {"owner_phone", "phone", "to_phone", "customer_phone", "from_phone"}
_IP_KEYS = {"ip_address", "ip"}
_UA_KEYS = {"user_agent"}
_STRIPE_KEYS = {"stripe_customer_id", "customer_id", "subscription_id", "stripe_subscription_id", "invoice_id",
                "payment_intent", "session_id", "customer"}
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_STRIPE_RE = re.compile(r"\b((?:cus|sub|in|pi|cs|ch|seti|pm)_)[A-Za-z0-9]{6,}([A-Za-z0-9]{4})\b")
_IP_RE = re.compile(r"\b(\d{1,3}\.\d{1,3})\.\d{1,3}\.\d{1,3}\b")
# A phone number inside free text (an error, a note): E.164, a formatted
# North American number, or a bare run of ten digits.
_PHONE_TEXT_RE = re.compile(r"(?<![\w.])(?:\+\d{10,15}|\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}|\d{10})(?![\w.])")


def _mask_email(v):
    return _EMAIL_RE.sub(lambda m: f"{m.group(1)}•••@{m.group(2)}", str(v))


def _mask_phone(v):
    digits = _DIGITS.sub("", str(v))
    return f"•••-•••-{digits[-4:]}" if len(digits) >= 4 else "•••"


def _mask_ip(v):
    s = str(v)
    if ":" in s and "." not in s:
        return s.split(":")[0] + ":••••"
    return _IP_RE.sub(lambda m: f"{m.group(1)}.x.x", s)


def _mask_text(s):
    """Free text as support sees it: emails, Stripe ids, phone numbers and
    IPv4 addresses masked wherever they sit (C's #87 contract)."""
    s = _STRIPE_RE.sub(lambda m: f"{m.group(1)}••••{m.group(2)}", _mask_email(s))
    s = _PHONE_TEXT_RE.sub(lambda m: _mask_phone(m.group(0)), s)
    return _IP_RE.sub(lambda m: f"{m.group(1)}.x.x", s)


def redact_for_support(obj, key=None):
    """A payload with the support role's masking applied; never mutates."""
    if isinstance(obj, dict):
        return {k: redact_for_support(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_for_support(v, key) for v in obj]
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    k = (key or "").lower()
    if k in _EMAIL_KEYS:
        return _mask_email(obj) if "@" in str(obj) else ("•••" if obj else obj)
    if k in _PHONE_KEYS:
        return _mask_phone(obj) if obj else obj
    if k in _IP_KEYS:
        return _mask_ip(obj) if obj else obj
    if k in _UA_KEYS:
        return "(hidden)" if obj else obj
    if k in _STRIPE_KEYS:
        s = str(obj)
        return (s[:4] + "••••" + s[-4:]) if len(s) > 10 else ("••••" if s else s)
    if isinstance(obj, str):
        return _mask_text(obj)
    return obj


def viewer_role():
    """'admin' | 'support' | another role | None for the request being
    answered: from what the auth layer put on flask.g (workstream A), else
    the session itself."""
    try:
        from flask import g, has_request_context
        if not has_request_context():
            return None
        for attr in ("admin_role", "viewer_role"):
            v = getattr(g, attr, None)
            if isinstance(v, str) and v:
                return v
        for attr in ("current_user", "admin_user", "user"):
            u = getattr(g, attr, None)
            if isinstance(u, dict) and u:
                return "admin" if u.get("is_admin") else (u.get("role") or None)
        import auth
        u = auth.get_current_user() or {}
        return "admin" if u.get("is_admin") else (u.get("role") or None)
    except Exception:
        return None


# ── One read of the platform's state (#158) ──────────────────────────────────
#
# Overview's system tiles and the Operations header each derived a state per
# system in the browser, from different payloads, and disagreed. This is the
# one server reading, from the sources each workstream keeps: D's heartbeat
# and job ledger, F's request metrics, 5xx log and provider probes, E's
# messaging problems and outboxes, G's AI health, and the fleet's
# integrations and inbound webhooks.

OPS_SYSTEMS = ("scheduler", "jobs", "api", "email", "sms", "push", "ai", "integrations")
_STATE_RANK = {"ok": 0, "unknown": 1, "warn": 2, "bad": 3}


def _sys(state, reason=None, since=None, **extra):
    return {"state": state, "reason": reason, "since": since, **extra}


def _ago_z(minutes):
    """The UTC moment `minutes` ago, as the console's Z stamp."""
    return (_utcnow() - timedelta(minutes=float(minutes))).strftime(_ZFMT) if minutes is not None else None


def ops_state():
    """{systems: {scheduler, jobs, api, email, sms, push, ai, integrations:
    {state: ok | warn | bad | unknown, reason, since}}, worst, generated_at}.
    Each system is read on its own and reads `unknown` (with why) when its
    source cannot be — never a page failure. Nothing here writes."""
    path = _current_db_path()
    systems = {}

    def guard(name, fn):
        try:
            systems[name] = fn()
        except AdminBusy:
            systems[name] = _sys("unknown", "The console is building the fleet view — try again in a moment.")
        except Exception as e:
            log.warning("ops state %s unreadable: %s", name, e)
            systems[name] = _sys("unknown", f"unreadable: {str(e)[:120]}")

    providers = {}
    try:
        import provider_health as _ph
        providers = _ph.latest(path) or {}
    except Exception as e:
        log.warning("provider probes unreadable: %s", e)
    try:
        import notify as _n_state
        messaging = list(_n_state.messaging_problems(db_path=path) or [])
    except Exception:
        messaging = []

    def probe_bad(name):
        p = providers.get(name) or {}
        if p.get("state") == "failing":
            return _sys("bad", f"The {name.replace('_', ' ').title()} probe is failing: {p.get('detail') or 'refused'}",
                        _iso_z(p.get("checked_at"), "UTC"))
        return None

    def scheduler():
        import status_manager as _sm_state
        st = _sm_state.scheduler_state(path)
        beat = st.get("beat_age_minutes")
        since = _ago_z(beat)
        if st["state"] == "stale":
            return _sys("bad", f"No heartbeat for {int(beat)} minutes — nothing scheduled is running.", since)
        if st["state"] == "wedged":
            return _sys("bad", f"Stuck in {st.get('running_job')} for {int(st.get('running_minutes') or 0)} minutes "
                               f"(its bound is {st.get('running_bound_minutes')}).", since)
        if st["state"] == "stalled":
            return _sys("warn", f"No tick has completed in {int(st.get('loop_completed_age_minutes') or 0)} minutes "
                                "— a tick is failing part-way.", _ago_z(st.get("loop_completed_age_minutes")))
        if st["state"] == "unknown":
            return _sys("unknown", "The heartbeat could not be read.")
        return _sys("ok", f"Beat {beat:.0f} minute{'s' if round(beat) != 1 else ''} ago.", since)

    def jobs():
        import ops as _ops_state
        overdue = _ops_state.jobs_overdue(db_path=path)
        if overdue:
            names = ", ".join(j["job"] for j in overdue[:4]) + ("…" if len(overdue) > 4 else "")
            oldest = min((j["last_ok_at"] for j in overdue if j.get("last_ok_at")), default=None)
            return _sys("bad", f"{len(overdue)} job{'s' if len(overdue) != 1 else ''} past {'their' if len(overdue) != 1 else 'its'} "
                               f"SLA: {names}.", _iso_z(oldest, "UTC"), overdue=len(overdue))
        conn = get_conn(path)
        try:
            kind = " AND COALESCE(kind,'job')='job'" if _has_cols(conn, "job_failures", ("kind",)) else ""
            row = _one_dict(conn, "SELECT COUNT(*) AS n, COUNT(DISTINCT job) AS jobs, MIN(created_at) AS first_at "
                                  f"FROM job_failures WHERE created_at >= datetime('now','-1 day'){kind}",
                            label="job_failures") or {}
        finally:
            conn.close()
        if row.get("n"):
            return _sys("warn", f"{row['n']} failure{'s' if row['n'] != 1 else ''} in 24 hours across "
                                f"{row['jobs']} job{'s' if row['jobs'] != 1 else ''}.", _iso_z(row.get("first_at"), "UTC"))
        return _sys("ok", "Every job ran within its SLA.")

    def api():
        import http_layer
        rm = http_layer.request_metrics()
        win = max(1, rm.get("window_seconds", 300) // 60)
        if rm["requests"] >= 20 and rm["server_error_rate"] >= 5.0:
            return _sys("bad", f"{rm['server_error_rate']:g}% of customer requests failed (5xx) in the last "
                               f"{win} minutes.", "now")
        try:
            import platform_monitor as _pm_state
            recent = _pm_state.recent_server_errors(hours=1, limit=1, db_path=path)
        except Exception:
            recent = {"total": 0, "latest": []}
        if recent.get("total"):
            latest = (recent.get("latest") or [{}])[0]
            return _sys("warn", f"{recent['total']} server error{'s' if recent['total'] != 1 else ''} in the last hour.",
                        _iso_z(latest.get("created_at"), "UTC"))
        if rm.get("p95_ms") and rm["p95_ms"] > 2000:
            return _sys("warn", f"Slow: p95 {rm['p95_ms']:.0f} ms over the last {win} minutes.", "now")
        if not rm["requests"]:
            return _sys("ok", f"No customer traffic in the last {win} minutes.")
        return _sys("ok", f"{rm['rpm']:g} requests a minute, p95 {rm['p95_ms']:.0f} ms.")

    def email():
        bad = probe_bad("resend")
        if bad:
            return bad
        mine = [m for m in messaging if "Resend" in m or "Operator address" in m]
        conn = get_conn(path)
        try:
            rates = _email_rates(conn, _windows()).get("7d") or {}
            today = _one_dict(conn, "SELECT SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, COUNT(*) AS n "
                                    "FROM email_log WHERE sent_at >= datetime('now','-1 day')", label="email_log") or {}
        finally:
            conn.close()
        if rates.get("enough"):
            for kind, crit in (("bounce", EMAIL_BOUNCE_CRIT_PCT), ("complaint", EMAIL_COMPLAINT_CRIT_PCT)):
                pct = rates.get(f"{kind}_rate")
                if pct is not None and pct >= crit:
                    return _sys("bad", f"Email {kind} rate {pct:g}% this week.", None)
        if mine:
            return _sys("warn", mine[0])
        if rates.get("enough"):
            for kind, warn in (("bounce", EMAIL_BOUNCE_WARN_PCT), ("complaint", EMAIL_COMPLAINT_WARN_PCT)):
                pct = rates.get(f"{kind}_rate")
                if pct is not None and pct >= warn:
                    return _sys("warn", f"Email {kind} rate {pct:g}% this week.", None)
        if today.get("failed"):
            return _sys("warn", f"{today['failed']} of {today['n']} email{'s' if today['n'] != 1 else ''} failed in 24 hours.")
        return _sys("ok", f"{today.get('n') or 0} email{'s' if (today.get('n') or 0) != 1 else ''} in 24 hours.")

    def sms():
        bad = probe_bad("twilio")
        if bad:
            return bad
        import notify as _n_sms
        hour = _n_sms.sms_stats(hours=1, db_path=path)
        if hour.get("account_errors"):
            return _sys("bad", f"{hour['account_errors']} text{'s' if hour['account_errors'] != 1 else ''} failed in "
                               "the last hour with a Twilio account-level error.", "now")
        mine = [m for m in messaging if "Twilio" in m or "text" in m]
        if mine:
            return _sys("warn", mine[0])
        day = _n_sms.sms_stats(hours=24, db_path=path)
        by = day.get("by_status") or {}
        failed = sum(v for k, v in by.items() if k in ("failed", "undelivered", "error"))
        if failed:
            return _sys("warn", f"{failed} of {day['attempted']} text{'s' if day['attempted'] != 1 else ''} failed in 24 hours.")
        if (providers.get("twilio") or {}).get("state") == "unconfigured":
            return _sys("unknown", "Twilio is not configured on this server.")
        return _sys("ok", f"{day.get('attempted') or 0} text{'s' if (day.get('attempted') or 0) != 1 else ''} in 24 hours.")

    def push():
        bad = probe_bad("apns")
        if bad:
            return bad
        import push as _push_state
        ob = _push_state.outbox_counts(db_path=path)
        oldest = _age_hours(ob.get("oldest_pending_at"), "UTC")
        if oldest is not None and oldest * 60 > 30:
            return _sys("warn", f"A push has waited {int(oldest * 60)} minutes to go out.",
                        _iso_z(ob.get("oldest_pending_at"), "UTC"))
        if ob.get("failed_24h"):
            return _sys("warn", f"{ob['failed_24h']} push{'es' if ob['failed_24h'] != 1 else ''} failed in 24 hours.")
        return _sys("ok", "Pushes are going out.")

    def ai():
        import ai_utils as _ai_state
        h = _ai_state.ai_health(path)
        state = {"operational": "ok", "degraded": "warn", "outage": "bad"}.get(h.get("status"), "unknown")
        return _sys(state, h.get("reason") or ("Calls are going through." if state == "ok" else None),
                    _iso_z((h.get("last_auth_error") or {}).get("at"), "UTC") if state != "ok" else None)

    def integrations():
        conn = get_conn(path)
        try:
            hooks = _webhook_health(conn)
        finally:
            conn.close()
        failing_hooks = [p for p in hooks["providers"] if p.get("problem")]
        if failing_hooks:
            p = failing_hooks[0]
            return _sys("bad", f"{p['label']} webhook is failing verification — check {p['secret_env']}.",
                        p.get("first_failure_at"))
        for name in ("stripe", "google_places", "anthropic"):
            bad = probe_bad(name)
            if bad:
                return bad
        recs, _d, _meta = _records_cached()
        broken = [(r, i) for r in _real(recs) for i in r["integrations"] if i["state"] == "error"]
        if broken:
            clients = len({r["id"] for r, _i in broken})
            first = min((i.get("error_since") or i.get("last_success") for _r, i in broken
                         if i.get("error_since") or i.get("last_success")), default=None)
            return _sys("warn", f"{len(broken)} integration{'s' if len(broken) != 1 else ''} failing at {clients} "
                                f"client{'s' if clients != 1 else ''}.", first)
        return _sys("ok", "Every connected integration is syncing.")

    for name, fn in (("scheduler", scheduler), ("jobs", jobs), ("api", api), ("email", email), ("sms", sms),
                     ("push", push), ("ai", ai), ("integrations", integrations)):
        guard(name, fn)
    worst = max((s["state"] for s in systems.values()), key=lambda st: _STATE_RANK.get(st, 1), default="unknown")
    return {"ok": True, "systems": systems, "worst": worst, "generated_at": _utcnow().strftime(_ZFMT)}
