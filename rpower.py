"""rpower.py — RPOWER Core API client.

RPOWER's Above-Store database, read through https://rpowerpos.com/api/v1/coreapi/.
Implements the same contract as toast.py/square.py/clover.py so pos.py can
treat it as one more provider, plus the item-level reads Food Cost needs.

Three things in RPOWER's own documentation shape this file, and each is a
trap that would otherwise produce confidently wrong numbers:

1.  **`time_stamp` is not business time.** RPOWER says so outright: "The
    time_stamp value represents the time a record was written or updated.
    This value will be changed any time data is reposted from RPOWER at the
    store. When seeking records associated with a particular day/time
    associated with business activity, rely on date values instead."

    So every analytic read here uses a `getbybusinessdate` endpoint. The
    `getbytimestamp` variants exist and are useful — but only as an
    incremental-sync cursor ("what changed since I last looked"), never as
    "what happened last week". This is the same mistake the Reviews module
    made with fetched_at and the Food Cost module made with its count dates.

2.  **Ticket ITEMS are not sales.** Also RPOWER's own words: "Ticket Items
    can be used to reconstruct what displays on a receipt, but it should not
    be used for calculating sales totals. Use the Ticket Sales set of
    endpoints to calculate sales totals and quantities." `ticketitem` rows
    include modifier lines (mod_level > 0, parent_atom pointing at the
    parent, qty 1, total 0), so summing them as units-sold counts a burger
    with four modifiers as five items. Everything here reads `ticketsales`.

3.  **Omitting `pagenumber` silently truncates at 1000 rows.** And RPOWER's
    own description of the parameter contradicts itself — "Sending a 1 ...
    will get you the first 1000 records. Sending a 2 will get you records
    2000-2999" skips 1000-1999. `_paged` below does not trust either reading:
    it pages until a short page arrives and detects overlap by primary key:
    an overlap is rows skipped as well as rows doubled, so the read starts
    again once and is refused if it overlaps again (blind re-audit 10/20/26
    R3-04) — never a short set returned as whole. See `PAGINATION_UNVERIFIED`.

Not documented anywhere in RPOWER's 120 endpoints: rate limits, and any
response code other than 200. Both are handled defensively here and both are
open questions with RPOWER.
"""
import json
import logging
import os
import random
import re
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta
from typing import Optional

log = logging.getLogger("rpower")

BASE_URL = "https://rpowerpos.com/api/v1/coreapi"

# RPOWER's intro links these endpoints over http://. Every example in the
# collection uses https, and a bearer token over plaintext is not acceptable
# regardless — so the base above is https and this module never downgrades.
PAGE_SIZE = 1000

# RPOWER's pagination description is self-contradictory (see module docstring).
# VERIFIED LIVE 10/10/26 against Simple EJ's (rid 5): 2,599 ticketsales lines
# for 10/10 came back over 3 pages (page 2 = rows 1000-1999, so `pagenumber`
# is 1-based and nothing is skipped), and the lines' sale total, 21,009.41,
# matched RPOWER's own ticket summary (`type_sale`) to the cent; 10/4 and
# 10/11 matched too (the DSR's source_checks; bar audit 10/14/26 RPW-16).
# _paged keeps its primary-key overlap check all the same: it costs nothing
# and is what would catch RPOWER changing the arithmetic. Still unknown:
# RPOWER's rate limits and its non-200 response shapes, and there is NO
# SANDBOX (18 Sep 2026: read-only against live stores).
PAGINATION_UNVERIFIED = False
PAGINATION_VERIFIED = "10/10/26: 2,599 lines over 3 pages, line total = ticket summary 21,009.41"

# No rate limit is documented. Justin (RPOWER, 25 Sep 2026): "I'll check with
# dev but I don't think so." Until dev confirms, these stay conservative
# defaults so a full historical backfill cannot look like an attack.
REQUEST_SPACING_SECONDS = float(os.getenv("RPOWER_REQUEST_SPACING_SECONDS", "0.15"))
MAX_RETRIES = 3
# Full-jitter backoff between tries (scale audit 10/19/26 A3-10): try n
# waits a random 0..RETRY_BACKOFF**n seconds, and nothing after the last
# try. A fixed 1/2/4 s wave from every worker at once retried in lockstep.
RETRY_BACKOFF = 2.0
# (connect, read) seconds (A3-15): a black-holed connection fails in 3 s,
# not 45, and one socket read may take 30 s. 45 s for both meant one dead
# request held a job thread for minutes across its three tries.
TIMEOUT_SECONDS = (3.05, 30)
# Small answers get a short read (INF-11): the DSR sweep polls closeday every
# 10 minutes per closing store, and one hung poll held the serial sweep.
TIMEOUT_BY_PATH = {"closeday/getbybusinessdate": (3.05, 10), "store/get": (3.05, 10)}
# A 429's Retry-After is slept up to this many seconds, never more (food &
# bar audit 10/16/26 POS-13): an uncapped header could hold a thread for as
# long as the server liked. A longer one is no longer cut short and retried
# early (A3-10): the call raises RPowerRateLimited with `retry_after`, and a
# queue row is rescheduled by it (job_queue.fail). On a web request thread
# a 429 or 5xx is not waited out at all — the request fails at once.
RETRY_AFTER_MAX_SECONDS = 10.0


def _reader_only() -> bool:
    """A page load, or a read made for an owner's question off the request
    thread (pos.archive_only — scale audit 10/19/26 A3-20): the archive and
    the kept lists only, never a live read of a day or a list."""
    if on_request_thread():
        return True
    try:
        import pos
        return pos.archive_only_active()
    except Exception:
        return False


def on_request_thread() -> bool:
    """True inside a Flask request: the code is running on one of the web
    process's four request threads, where nothing may wait on RPOWER."""
    try:
        from flask import has_request_context
        return bool(has_request_context())
    except Exception:
        return False

# A backfill asks for one business date at a time for item-level data (a busy
# day can exceed a single page) but takes date ranges where the API allows.
#
# RPOWER (Justin): 18 Sep 2026, "Month worth of data at a time" (the API's
# ceiling); 25 Sep 2026, "I don't have a problem with you pulling a week's
# worth of data at a time" (what he is comfortable with). We ask for the
# week: both range fetches below chunk through _chunk_range, so a month's
# backfill is five small requests rather than one at the ceiling.
MAX_RANGE_DAYS = 7


# ── bounded caches ──────────────────────────────────────────────────────────
#
# Every per-restaurant list this module keeps (sales types, menu, people, void
# reasons, catalogs, closed days) lived in a plain dict that was never
# evicted: a menu, its categories, tables and rooms run to 1-2 MB a store, so
# a long-lived process serving RPOWER's ~1,500 stores would have held a few GB
# (bar audit 10/14/26 RPW-10 / R2). Each is now least-recently-used and
# bounded; an evicted entry is simply read again. Same interface as the dicts
# they replace (get, [], clear, len), thread-safe.
CACHE_MAX_RESTAURANTS = 64        # per-restaurant entries kept in each cache
CACHE_MAX_KEYED = 1024            # caches keyed (restaurant, something): catalogs, closed days


class _LRU:
    def __init__(self, maxsize):
        self.maxsize = int(maxsize)
        self._d = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            if key not in self._d:
                return default
            self._d.move_to_end(key)
            return self._d[key]

    def __getitem__(self, key):
        with self._lock:
            self._d.move_to_end(key)
            return self._d[key]

    def __setitem__(self, key, value):
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)

    def __contains__(self, key):
        with self._lock:
            return key in self._d

    def __len__(self):
        with self._lock:
            return len(self._d)

    def __delitem__(self, key):
        with self._lock:
            del self._d[key]

    def pop(self, key, default=None):
        with self._lock:
            return self._d.pop(key, default)

    def clear(self):
        with self._lock:
            self._d.clear()

    def keys(self):
        with self._lock:
            return list(self._d.keys())


class RPowerError(Exception):
    """Any failure reaching or reading RPOWER."""


class RPowerNoSalesYet(RPowerError):
    """RPOWER answered, with nothing posted yet for the business date — a
    state, not a failure: at opening no check has closed, and RPOWER's
    cloud copy can trail the store by hours (Simple EJ's, 10/1/26: nothing
    from 11am until 3pm, $1,825 at 3pm). intraday reads `not_yet`."""
    not_yet = True


class RPowerArchiveMiss(RPowerError):
    """A read made on a web request for days the local archive does not
    hold (food & bar audit 10/16/26 POS-13). A page load never reads
    RPOWER's sales live — one Food Cost open could make dozens of 45-second
    requests on one of the four request threads — so the caller states the
    figure as unknown instead; the nightly archive fills the days."""
    archive_miss = True


class RPowerAuthError(RPowerError):
    """The token is missing, expired or not authorized for this store.

    Separate from RPowerError because the caller's response is different: a
    transport failure is worth retrying, a rejected token is worth telling
    the owner about and never retrying on a loop.
    """


class RPowerTryLater(RPowerError):
    """Not sent, or refused, for a reason that passes: `retry_after` seconds
    say when to ask again. A queue row that raises one is rescheduled by it
    (job_queue.fail), never retried on its fixed backoff into the same wall
    (scale audit 10/19/26 A3-10)."""

    def __init__(self, message, retry_after=60.0):
        super().__init__(message)
        self.retry_after = max(1.0, float(retry_after or 0))


class RPowerUnavailable(RPowerTryLater):
    """The RPOWER breaker is open (pos_io): RPOWER has been failing, and
    nothing is sent until the half-open probe answers."""


class RPowerRateLimited(RPowerTryLater):
    """RPOWER answered 429 with a Retry-After longer than this thread will
    wait (RETRY_AFTER_MAX_SECONDS)."""


class RPowerBudgetExceeded(RPowerTryLater):
    """Our own request budget (the per-token and fleet buckets) had no slot
    within the wait allowed: the request is not sent over budget (A3-14)."""


# ── credentials ─────────────────────────────────────────────────────────────

def _creds(restaurant_id: int) -> dict:
    """{token, cg, store_mid, name, credential_id} for a restaurant.

    The token comes from the partner credential the store is bound to
    (credentials.pos_credentials, Audit #23 F7 A7-01) — one sealed row every
    store on that token shares, rotated in one step — and from the
    restaurant's own legacy rpower_token only while it has no credential id.
    A bound store whose credential is revoked or unreadable has NO token: it
    never falls back to the per-row copy of the old one.

    Inside a models.read_scope() the answer is kept for the block (A1-22):
    every helper of one archive or one sync used to read the restaurant row
    and decrypt the token again — 20 to 30 times a night per store."""
    import models
    return models.scope_read(("restaurant", int(restaurant_id), "rpower_creds"), lambda: _read_creds(restaurant_id))


def _read_creds(restaurant_id: int) -> dict:
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    if not r:
        raise RPowerAuthError("no such restaurant")
    cred_id = getattr(r, "rpower_credential_id", None)
    if cred_id:
        import credentials
        token = (credentials.credential_token(cred_id) or "").strip()
    else:
        token = (getattr(r, "rpower_token", None) or "").strip()
    return {
        "token": token,
        "cg": getattr(r, "rpower_cg", None),
        "store_mid": (getattr(r, "rpower_store_mid", None) or "").strip() or None,
        "name": getattr(r, "rpower_store_name", None),
        "credential_id": int(cred_id) if cred_id else None,
    }


def is_connected(restaurant_id: int) -> bool:
    """A token alone is not a connection.

    cg and store_mid come back from /store/get and are required by every
    subsequent call, so a restaurant with a pasted-but-unverified token is
    not connected — it is halfway through setup, and treating it as connected
    is how a sync job starts failing silently every night.
    """
    try:
        c = _creds(restaurant_id)
    except RPowerError:
        return False
    return bool(c["token"] and c["cg"] and c["store_mid"])


def has_token(restaurant_id: int) -> bool:
    """A token is on file, whether or not bootstrap has run."""
    try:
        return bool(_creds(restaurant_id)["token"])
    except RPowerError:
        return False


# ── transport ───────────────────────────────────────────────────────────────

# Which store a request is for, so the request ledger (pos_io, A3-19) and the
# per-store budget can name it: set by _ctx / bootstrap on this thread just
# before the reads they make, cleared by list_stores for a token on its own.
_io_ctx = threading.local()


def _note_store(restaurant_id=None, store_mid=None):
    _io_ctx.rid = int(restaurant_id) if restaurant_id else None
    _io_ctx.store = str(store_mid) if store_mid else None
    _io_ctx.cred = _io_ctx.tok = None                   # _ctx names the credential after this


def _current_rid():
    return getattr(_io_ctx, "rid", None)


# ── request rate: a two-level budget shared by every thread and every
# process (bar audit 10/14/26 RPW-10 / R2; scale audit 10/19/26 A3-14) ─────
#
# The spacing used to be a process-global "last request at" with no lock: the
# four pos_sync workers raced it, and the queue worker, the web process and
# the scheduler each kept their own clock, so one store's token could be asked
# four or five times as often as REQUEST_SPACING_SECONDS meant. The budget is
# a token bucket in the database (rpower_rate_bucket, one row per bucket,
# keyed by a hash — never the token), taken with a compare-and-swap so two
# processes cannot both spend the same slot; a thread lock serialises this
# process's own threads in front of it. When the table can't be read (a fresh
# test database, a locked file) the process-local spacing, under the same
# lock, is the fallback — never no spacing.
#
# Two levels (A3-14 / ONB-3 / INF-12):
#   1. per token — REQUEST_SPACING_SECONDS between requests on one token, as
#      before; with RPOWER_BUDGET_PER_STORE=1 the bucket is per (token,
#      store) instead, for when RPOWER confirms its limit is per store and
#      one partner token serves many stores;
#   2. the fleet — at most RPOWER_FLEET_RPS requests a second across every
#      token and store (0 turns the level off), the ceiling agreed with
#      RPOWER (default 10/s until they state one).
# Over budget a request is no longer sent anyway after its wait: it raises
# RPowerBudgetExceeded and a queue row is rescheduled (never "fire after
# 60 s", which made the bucket a suggestion).
RATE_BURST = 1                    # requests a quiet token may make back to back
RATE_MAX_WAIT_SECONDS = 60.0      # one request never waits longer than this for its slot
RATE_REQUEST_THREAD_WAIT_SECONDS = 5.0   # ...nor, on a web request thread, longer than this
BUDGET_PER_STORE = os.getenv("RPOWER_BUDGET_PER_STORE", "0").strip().lower() in ("1", "true", "on", "yes")
FLEET_RPS = float(os.getenv("RPOWER_FLEET_RPS", "10") or 0)
FLEET_BUCKET = "rpower:fleet"
_rate_lock = threading.Lock()     # guards the two dicts below
_token_locks = {}                 # bucket -> the lock its own requests queue on
_last_request_at = {}             # bucket -> monotonic time of its last request (the fallback)


def init_rate_limit(db_path=None):
    """The shared request budget's table (boot only, models.init_db)."""
    import models
    conn = models.get_conn(db_path) if db_path else models.get_conn()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS rpower_rate_bucket (
            bucket      TEXT PRIMARY KEY,
            tokens      REAL NOT NULL,
            updated_at  REAL NOT NULL
        )""")
        conn.commit()
    finally:
        conn.close()


def _bucket_key(token: str, store=None) -> str:
    """The per-token budget's bucket: keyed by the partner credential's id
    when the request is for a store bound to one (Audit #23 F7, A7-01 /
    A5-01) — so a rotation keeps the same bucket, and every store on one
    credential shares it — else by a hash of the token (never the token)."""
    import hashlib
    cred = getattr(_io_ctx, "cred", None) if getattr(_io_ctx, "tok", None) == token else None
    if cred:
        return f"rpower:cred:{int(cred)}" + (f"|{store}" if store else "")
    raw =str(token or "") + (f"|{store}" if store else "")
    return "rpower:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _take_shared_slot(token: str, spacing: float, key: str = None):
    """Seconds to wait before this request may go (0: go now, the slot is
    taken), or None when the shared budget can't be reached."""
    import models
    key = key or _bucket_key(token)
    try:
        conn = models.get_conn()
    except Exception:
        return None
    try:
        for _ in range(8):                         # compare-and-swap retries
            now = time.time()
            row = conn.execute("SELECT tokens, updated_at FROM rpower_rate_bucket WHERE bucket=?",
                               (key,)).fetchone()
            if row is None:
                cur = conn.execute("INSERT INTO rpower_rate_bucket (bucket, tokens, updated_at) VALUES (?,?,?) "
                                   "ON CONFLICT(bucket) DO NOTHING", (key, float(RATE_BURST - 1), now))
                conn.commit()
                if cur.rowcount == 1:
                    return 0.0
                continue
            tokens = min(float(RATE_BURST), float(row[0]) + max(0.0, now - float(row[1])) / spacing)
            if tokens < 1.0:
                return (1.0 - tokens) * spacing
            cur = conn.execute("UPDATE rpower_rate_bucket SET tokens=?, updated_at=? WHERE bucket=? AND updated_at=?",
                               (tokens - 1.0, now, key, float(row[1])))
            conn.commit()
            if cur.rowcount == 1:
                return 0.0
        return spacing                               # contended: wait one slot and ask again
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    finally:
        conn.close()


# Lanes (blind re-audit 10/20/26 R3-01; pos_io.lane): live reads and the
# night's report are served ahead of the nightly and bulk work. In this
# process a background request never takes a slot while a live or close
# request is waiting on the same bucket; across processes, while live or
# close requests are being made on a bucket (its ":live" mark, younger than
# LIVE_ACTIVE_SECONDS) background requests also pass a ":bg" bucket at
# BACKGROUND_SHARE of its rate, so at least the rest is left for live reads.
# Bulk (onboarding, history backfill) always passes it. The per-bucket lock
# is held only to take a slot, never while waiting for one, and is acquired
# with the request's own time limit (R3-07): a web request thread no longer
# queues behind a job thread sleeping out a 60-second wait.
BACKGROUND_SHARE = min(1.0, max(0.05, float(os.getenv("RPOWER_BACKGROUND_SHARE", "0.5") or 0.5)))
LIVE_ACTIVE_SECONDS = 120.0
_LIVE_MARK_EVERY = 5.0            # a process writes a bucket's live mark at most this often
_LIVE_READ_EVERY = 2.0            # ...and reads it at most this often
_priority_waiting = {}            # bucket -> live/close requests of this process waiting on it
_live_marked = {}                 # bucket -> monotonic time this process last wrote its live mark
_live_seen = {}                   # bucket -> (monotonic time read, live mark active)


def _bucket_lock(key):
    with _rate_lock:
        lock = _token_locks.get(key)
        if lock is None:
            if len(_token_locks) > 4096:             # bounded: idle tokens' locks go
                _token_locks.clear()
            lock = _token_locks[key] = threading.Lock()
        return lock


def _budget_refused(limit, wait=5.0, why="our own budget"):
    return RPowerBudgetExceeded(f"no RPOWER request slot within {limit:.0f}s ({why})", retry_after=max(wait, 5.0))


def _wait_for_bucket(token, key, spacing, limit, lane="nightly", deadline=None):
    """Take one slot of bucket `key`, waiting up to `limit` seconds (or until
    `deadline`, a time.monotonic() value). Raises RPowerBudgetExceeded when
    none came in time. The bucket's process lock is held only while a slot
    is taken, never while waiting for one."""
    import pos_io
    deadline = deadline if deadline is not None else time.monotonic() + float(limit)
    priority = lane not in pos_io.BACKGROUND_LANES
    lock = _bucket_lock(key)
    if priority:
        with _rate_lock:
            _priority_waiting[key] = _priority_waiting.get(key, 0) + 1
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _budget_refused(limit)
            if not priority and _priority_waiting.get(key):
                time.sleep(min(0.05, remaining))     # a live or close request goes first
                continue
            if not lock.acquire(timeout=remaining):
                raise _budget_refused(limit, why="this process's queue for the bucket")
            try:
                wait = _take_shared_slot(token, spacing, key=key)
                now = time.monotonic()
                if wait is None:
                    # The local fallback: reserve the next slot under the lock,
                    # wait for it outside it.
                    slot = max(now, _last_request_at.get(key, 0.0) + spacing)
                    if slot - now > remaining:
                        raise _budget_refused(limit, wait=slot - now)
                    _last_request_at[key] = slot
                    wait = slot - now
                    if wait > 0:
                        lock.release()
                        try:
                            time.sleep(wait)
                        finally:
                            lock.acquire()
                    return
                if wait <= 0:
                    with _rate_lock:
                        if len(_last_request_at) > 4096:
                            _last_request_at.clear()
                        _last_request_at[key] = now
                    return
            finally:
                lock.release()
            time.sleep(max(0.005, min(wait, 1.0, remaining)))
    finally:
        if priority:
            with _rate_lock:
                n = _priority_waiting.get(key, 0) - 1
                if n > 0:
                    _priority_waiting[key] = n
                else:
                    _priority_waiting.pop(key, None)


def _mark_live(key):
    """Note, for every process, that live or close requests are being made
    on bucket `key` (its ":live" row; written at most every
    _LIVE_MARK_EVERY seconds by a process). Never raises."""
    now = time.monotonic()
    if now - _live_marked.get(key, -1e9) < _LIVE_MARK_EVERY:
        return
    _live_marked[key] = now
    _live_seen[key] = (now, True)
    import models
    try:
        conn = models.get_conn()
        try:
            conn.execute("INSERT INTO rpower_rate_bucket (bucket, tokens, updated_at) VALUES (?, 0, ?) "
                         "ON CONFLICT(bucket) DO UPDATE SET updated_at=excluded.updated_at",
                         (key + ":live", time.time()))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _live_active(key) -> bool:
    """Whether live or close requests were made on bucket `key` in the last
    LIVE_ACTIVE_SECONDS (any process). Read at most every _LIVE_READ_EVERY
    seconds; unreadable reads as not active (the background share is a
    priority, not a safety limit — the bucket itself still holds)."""
    now = time.monotonic()
    seen = _live_seen.get(key)
    if seen and now - seen[0] < _LIVE_READ_EVERY:
        return seen[1]
    active = False
    import models
    try:
        conn = models.get_conn()
        try:
            row = conn.execute("SELECT updated_at FROM rpower_rate_bucket WHERE bucket=?", (key + ":live",)).fetchone()
        finally:
            conn.close()
        active = bool(row) and time.time() - float(row[0]) < LIVE_ACTIVE_SECONDS
    except Exception:
        active = False
    if len(_live_seen) > 4096:
        _live_seen.clear()
    _live_seen[key] = (now, active)
    return active


def _take_slot(token, key, spacing, limit, lane, deadline):
    """One slot of bucket `key` for a request of `lane` (see Lanes above)."""
    import pos_io
    if lane == "bulk" or (lane in pos_io.BACKGROUND_LANES and _live_active(key)):
        _wait_for_bucket(token, key + ":bg", spacing / BACKGROUND_SHARE, limit, lane=lane, deadline=deadline)
    _wait_for_bucket(token, key, spacing, limit, lane=lane, deadline=deadline)
    if lane not in pos_io.BACKGROUND_LANES:
        _mark_live(key)


def _sleep_for_spacing(token: str = ""):
    """Wait for this request's slot in both budgets (see the note above),
    in this thread's lane (pos_io.current_lane)."""
    import pos_io
    spacing = float(REQUEST_SPACING_SECONDS or 0)
    # A page load waits a few seconds for its slot at most (POS-13); a job
    # waits up to RATE_MAX_WAIT_SECONDS.
    limit = RATE_REQUEST_THREAD_WAIT_SECONDS if on_request_thread() else RATE_MAX_WAIT_SECONDS
    deadline = time.monotonic() + limit
    lane = pos_io.current_lane()
    if spacing > 0:
        store = getattr(_io_ctx, "store", None) if BUDGET_PER_STORE else None
        _take_slot(token, _bucket_key(token, store), spacing, limit, lane, deadline)
    # The fleet ceiling stands on its own (R3-08): RPOWER saying it has no
    # per-token limit (spacing 0) never switches the fleet-wide one off.
    if FLEET_RPS and FLEET_RPS > 0:
        _take_slot(token, FLEET_BUCKET, 1.0 / float(FLEET_RPS), limit, lane, deadline)


def _backoff(attempt: int) -> float:
    """Full jitter: a random wait in 0..RETRY_BACKOFF**attempt seconds."""
    return random.uniform(0, float(RETRY_BACKOFF) ** attempt)


def _retry_after(resp) -> float:
    try:
        return max(0.0, float((resp.headers or {}).get("Retry-After", 0) or 0))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _request(token: str, path: str, params: dict = None) -> object:
    """One GET against the Core API, with retries on the failures worth
    retrying and none on the failures that will not improve.

    RPOWER documents only 200 responses across all 120 endpoints, so the
    status handling here is defensive rather than specified: anything 2xx is
    taken as success, 401/403 raise RPowerAuthError (never retried — a bad
    token is bad on the third attempt too), 429 and 5xx back off, and a
    non-JSON body is an error rather than something to guess at.

    Every request passes the RPOWER breaker (pos_io: refused at once while
    it is open, one caller probing once it lapses), both request budgets,
    and is counted in the request ledger by endpoint and store (A3-10,
    A3-14, A3-19).
    """
    import requests
    import pos_io

    # Halsted & Rye, the showcase demo (demo_store): a demo-store token is
    # answered by the simulated store with RPOWER's own records, never sent
    # to rpowerpos.com — and so never counted, budgeted or breaker-gated.
    if str(token or "").startswith("demo-store:"):
        from demo_store import api as _demo_api
        return _demo_api.respond(token, path, params)

    url = f"{BASE_URL}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    rid = _current_rid()
    page_load = on_request_thread()
    last_error = None
    for attempt in range(MAX_RETRIES):
        last = attempt + 1 >= MAX_RETRIES
        gate, wait = pos_io.breaker_allow("rpower")
        if gate == "open":
            pos_io.record("rpower", rid, path, "skipped")
            raise RPowerUnavailable(f"RPOWER requests are paused for {wait:.0f}s: it has been failing "
                                    f"({last_error or 'see the breaker'})", retry_after=wait)
        probe = gate == "probe"
        try:
            _sleep_for_spacing(token)
        except RPowerBudgetExceeded:
            pos_io.record("rpower", rid, path, "skipped")
            raise
        t0 = time.monotonic()
        try:
            resp = requests.get(url, headers=headers, params=params or {},
                                timeout=TIMEOUT_BY_PATH.get(path, TIMEOUT_SECONDS))
        except Exception as e:
            ms = (time.monotonic() - t0) * 1000.0
            pos_io.record("rpower", rid, path, "transport", ms)
            pos_io.breaker_failure("rpower", rid, f"{path}: {type(e).__name__}", probe=probe)
            last_error = f"request failed: {e}"
            if page_load:
                break                                # one try on a page load, never three
            if not last:
                time.sleep(_backoff(attempt))
            continue
        ms = (time.monotonic() - t0) * 1000.0
        status = int(getattr(resp, "status_code", 0) or 0)

        if status in (401, 403):
            pos_io.record("rpower", rid, path, "s4xx", ms)
            pos_io.breaker_success("rpower", probe=probe)
            raise RPowerAuthError(
                f"RPOWER rejected the token ({status}). Check that the token is "
                f"current and that it is authorized for this store.")
        if status == 429 or status >= 500:
            # Undocumented but assumed to exist. Honour Retry-After when the
            # server sends one rather than guessing over the top of it.
            pos_io.record("rpower", rid, path, "s429" if status == 429 else "s5xx", ms)
            if status >= 500:
                pos_io.breaker_failure("rpower", rid, f"{path}: HTTP {status}", probe=probe)
            else:
                pos_io.breaker_success("rpower", probe=probe)     # it answered: up, but asking us to slow
            last_error = f"HTTP {status}"
            if page_load:
                raise RPowerError(f"{path} answered HTTP {status}; not retried on a page load")
            ra = _retry_after(resp)
            if ra > RETRY_AFTER_MAX_SECONDS:
                raise RPowerRateLimited(f"{path} answered HTTP {status} with Retry-After {ra:.0f}s", retry_after=ra)
            if not last:
                time.sleep(max(_backoff(attempt), ra))
            continue
        if not resp.ok:
            pos_io.record("rpower", rid, path, "s4xx", ms)
            pos_io.breaker_success("rpower", probe=probe)
            raise RPowerError(f"HTTP {status} from {path}: {resp.text[:200]}")
        try:
            body = resp.json()
        except ValueError:
            # A maintenance page answered 200: RPOWER is not serving data.
            pos_io.record("rpower", rid, path, "s5xx", ms)
            pos_io.breaker_failure("rpower", rid, f"{path}: non-JSON body", probe=probe)
            raise RPowerError(f"{path} returned a non-JSON body: {resp.text[:200]}")
        pos_io.record("rpower", rid, path, "ok", ms, rows=len(body) if isinstance(body, list) else 1)
        pos_io.breaker_success("rpower", probe=probe)
        return body

    raise RPowerError(f"{path} failed after {MAX_RETRIES} attempts: {last_error}")


def _row_key(row: dict) -> str:
    """A stable identity for a record, for the overlap check in _paged.

    RPOWER's tables use `rid` for transactional records and `mid` for
    configuration records; `atom` distinguishes line items within a ticket.
    Falls back to the whole row so an unkeyed table still de-duplicates
    correctly rather than silently dropping distinct records.
    """
    for k in ("rid", "mid"):
        if row.get(k):
            return f"{k}:{row[k]}:{row.get('atom', '')}"
    import json as _j
    return _j.dumps(row, sort_keys=True, default=str)


def _paged(token: str, path: str, params: dict, max_pages: int = 200) -> list:
    """Every page of a result set, with the pagination ambiguity handled.

    RPOWER says omitting `pagenumber` returns only the first 1000 records —
    silently, with nothing in the response to say more exist. It then
    describes page 2 as "records 2000-2999", which would skip 1000-1999.
    Either the prose is wrong or the API is, and getting it wrong means a
    busy Saturday's sales are quietly half-missing.

    So this does not rely on the arithmetic at all. It walks pages from 1,
    stops on the first short page, and keys every row: a page that repeats
    rows already seen means the server is not advancing the way the caller
    assumed, and that is raised rather than returned as a doubled total.

    A page that PARTLY repeats the one before is rows lost, not only rows
    doubled (blind re-audit 10/20/26 R3-04): pages are fixed 1,000-row
    offsets, so k rows seen twice are k rows never returned — rows inserted
    or re-sorted between two requests on a sort key that is not unique
    (`date`, `time_stamp`; RPOWER documents no unique one). The whole read
    starts again from page 1, once; a second overlap raises, so no caller
    stores or reports a short set as whole.
    """
    for attempt in range(PAGED_ATTEMPTS):
        try:
            return _paged_once(token, path, params, max_pages)
        except _PageOverlap as e:
            if attempt + 1 >= PAGED_ATTEMPTS:
                raise RPowerError(f"{path} could not be read whole: {e}; nothing was kept from this read")
            log.warning("[rpower] %s: %s — reading it again from page 1", path, e)


# Reads of a paged set before an overlap is an error (R3-04).
PAGED_ATTEMPTS = 2


class _PageOverlap(Exception):
    """A page shared rows with the pages before it: rows were skipped."""


def _paged_once(token: str, path: str, params: dict, max_pages: int = 200) -> list:
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        body = _request(token, path, {**params, "pagenumber": page})
        rows = body if isinstance(body, list) else ([body] if body else [])
        if not rows:
            break

        fresh = []
        duplicates = 0
        for row in rows:
            if not isinstance(row, dict):
                fresh.append(row)
                continue
            key = _row_key(row)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            fresh.append(row)

        # A fully repeated page means paging is not advancing. A SHORT one is
        # the end of the set. A FULL one means more rows exist that we cannot
        # reach — returning what we have stored a busy Saturday at the first
        # thousand lines as the whole night, so it fails the sync instead and
        # the previous data stays (pos.py's contract, DH2-6).
        if duplicates and not fresh:
            if len(rows) >= PAGE_SIZE:
                raise RPowerError(f"{path} truncated: page {page} repeated the previous page in full, so "
                                  "RPOWER's paging is not advancing and the rest of the rows can't be read")
            log.warning("[rpower] %s page %s repeated entirely — stopping", path, page)
            break
        if duplicates:
            raise _PageOverlap(f"page {page} repeated {duplicates} row(s) of the pages before it, so as many "
                               "were never returned (rows moved between requests)")
        out.extend(fresh)
        if len(rows) < PAGE_SIZE:
            break
    else:
        # More pages than the ceiling: the result is incomplete, never
        # returned as though it were whole (DH2-6).
        raise RPowerError(f"{path} truncated: more than {max_pages} pages; nothing was saved from this read")
    return out


def _d(value) -> str:
    """A date in the form every RPOWER endpoint wants: YYYY-MM-DD, no time."""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _biz_date(raw) -> Optional[str]:
    """The business date off a record, as YYYY-MM-DD.

    RPOWER returns dates as "2020-01-21T00:00:00" — a date with a zeroed time
    component, not a timestamp. Slicing is deliberate: parsing and
    re-formatting would invite a timezone conversion, and there is no
    timezone here to convert.
    """
    if not raw:
        return None
    return str(raw)[:10] or None


# ── bootstrap ───────────────────────────────────────────────────────────────

class for_store:
    """`with rpower.for_store(rid):` — the store/get reads inside are
    counted against `rid` in the request ledger (a connect or bootstrap);
    outside one, a token's store/get counts as no store."""

    def __init__(self, restaurant_id):
        self.rid = restaurant_id

    def __enter__(self):
        self.prev = getattr(_io_ctx, "pin", None)
        _io_ctx.pin = self.rid
        return self

    def __exit__(self, *exc):
        _io_ctx.pin = self.prev
        return False


def list_stores(token: str) -> list:
    """Every store this token may read. RPOWER's documented first call."""
    _note_store(getattr(_io_ctx, "pin", None))
    body = _request(token, "store/get")
    rows = body if isinstance(body, list) else ([body] if body else [])
    if len(rows) >= PAGE_SIZE:
        # A full first page (scale audit A3-16): every other RPOWER list
        # truncates at 1,000 without `pagenumber`, so a partner token that
        # sees more stores is read through _paged — never bound from the
        # first thousand only. Under 1,000 stores this is never asked.
        rows = _paged(token, "store/get", {})
    return rows


def test_token(token: str) -> dict:
    """Does this token work, and what does it see?

    Used by the admin connect screen before anything is saved, so a typo is
    caught at paste time rather than by a sync failing quietly at 5am.
    """
    try:
        stores = list_stores(token)
    except RPowerAuthError as e:
        return {"ok": False, "error": str(e)}
    except RPowerError as e:
        return {"ok": False, "error": f"Couldn't reach RPOWER: {e}"}
    if not stores:
        return {"ok": False,
                "error": "The token works but is not authorized for any store yet. "
                         "RPOWER has to grant it access to this customer."}
    return {
        "ok": True,
        "stores": [{
            "cg": s.get("cg"),
            "store_mid": s.get("mid"),
            "name": s.get("name"),
            "serial_number": s.get("serial_number"),
            "timezone": s.get("timezone"),
            "week_dow": s.get("week_dow"),
            "ot_dow": s.get("ot_dow"),
            "open_days": s.get("open_days"),
        } for s in stores],
    }


def bootstrap(restaurant_id: int, store_mid: str = None, probe: dict = None) -> dict:
    """Resolve and store cg + store_mid for a restaurant that has a token.

    RPOWER issues a token that may see several stores, so when more than one
    comes back the caller must choose — this refuses rather than picking the
    first, because silently binding a restaurant to the wrong store produces
    a module full of somebody else's numbers that looks entirely normal.

    Also carries across the settings RPOWER already knows and Cavnar was
    asking admins to type: the IANA timezone, and the day the payroll week
    starts on (`ot_dow`), which is what the labor module anchors overtime to.
    """
    from models import update_restaurant, get_restaurant
    c = _creds(restaurant_id)
    if not c["token"]:
        return {"ok": False, "error": "No RPOWER token on file for this restaurant."}

    # `probe`: the caller's own test_token answer for this same token (the
    # admin save reads store/get once, not twice — scale audit A3-16).
    if not (probe and probe.get("ok")):
        with for_store(restaurant_id):
            probe = test_token(c["token"])
    if not probe["ok"]:
        update_restaurant(restaurant_id, {"rpower_sync_error": probe["error"]})
        return probe

    stores = probe["stores"]
    if store_mid:
        chosen = next((s for s in stores if str(s["store_mid"]) == str(store_mid)), None)
        if not chosen:
            return {"ok": False, "error": "That store isn't one this token can see.",
                    "stores": stores}
    elif len(stores) == 1:
        chosen = stores[0]
    else:
        return {"ok": False, "needs_choice": True, "stores": stores,
                "error": f"This token can see {len(stores)} stores — pick which one "
                         f"this restaurant is."}

    # One store, one restaurant. A store bound to two restaurants syncs its
    # sales and labor into both, and each looks entirely normal (fix round
    # #143). A demo may mirror a live store, as it may a live Google listing.
    r = get_restaurant(restaurant_id)
    # A demo reads a LIVE store only with the audited admin override (Audit
    # #23 F7, A7-15): a demo's logins are shared and reviewers' accounts are
    # demos, and a live store is a real restaurant's sales, wages and staff.
    # The simulated store (demo-store:) needs nothing.
    refusal = demo_live_store_refusal(r, c["token"])
    if refusal:
        return {"ok": False, "stores": stores, "error": refusal}
    if not (r is not None and int(getattr(r, "is_demo", 0) or 0)):
        from models import pos_binding_conflict
        clash = pos_binding_conflict("rpower", (chosen.get("cg"), chosen.get("store_mid")),
                                     exclude_id=restaurant_id)
        if clash:
            return {"ok": False, "stores": stores,
                    "error": f"That RPOWER store is already connected to {clash}. One store can feed "
                             f"only one restaurant — disconnect it there first, or pick another store."}

    fields = {
        "rpower_cg": chosen["cg"],
        "rpower_store_mid": str(chosen["store_mid"]),
        "rpower_store_name": chosen.get("name"),
        "rpower_verified_at": datetime.utcnow().isoformat(timespec="seconds"),
        "rpower_sync_error": None,
        # Connected again: the POS purge clock stops (A7-13).
        "pos_disconnected_at": None,
    }
    # RPOWER's own timezone and payroll-week anchor, which are the same two
    # settings an admin was being asked to know. Only filled when Cavnar has
    # nothing already — an operator's explicit choice outranks the POS.
    if chosen.get("timezone") and not (r and r.timezone and r.timezone != "America/Chicago"):
        fields["timezone"] = chosen["timezone"]
    suggested_week_start = None
    if chosen.get("ot_dow") is not None and r is not None and not getattr(r, "week_start_day", 0):
        # Two different conventions for the same fact, so this conversion is
        # spelled out rather than assumed: RPOWER's ot_dow read as 1-based
        # starting Monday (1=Mon..7=Sun), against Cavnar's week_start_day,
        # Python's weekday() — 0-based, also starting Monday.
        # UNCONFIRMED (bar audit 10/14/26 RPW-17 / R26): the collection does
        # not define ot_dow at all, and its own `open_days` sample "NYYYYYN"
        # reads Sunday-first. EJ's came through this path as week_start_day 2
        # (Wednesday) from ot_dow 3. So it is no longer APPLIED (scale audit
        # 10/19/26 ONB-11): silently off by one is a payroll week that starts
        # on the wrong day and an overtime split in the wrong place, at every
        # store bootstrapped after. It is recorded as the setup step's
        # suggestion and a person confirms the day during setup
        # (onboarding.confirm 'week_start'); an operator's week_start_day
        # always wins.
        try:
            suggested_week_start = (int(chosen["ot_dow"]) - 1) % 7
        except (TypeError, ValueError):
            suggested_week_start = None
    update_restaurant(restaurant_id, fields)
    # A store held for its owner's check (bulk onboarding, A7-02) learns the
    # serial number the owner confirms it with.
    if r is not None and getattr(r, "pos_owner_hold", None):
        try:
            import bulk_provisioning
            bulk_provisioning.record_store_facts(restaurant_id, chosen)
        except Exception as e:
            log.warning("rpower bootstrap: owner-check facts not recorded for %s: %s", restaurant_id, e)
    if suggested_week_start is not None:
        try:
            import onboarding
            onboarding.suggest(restaurant_id, "week_start",
                               {"week_start_day": suggested_week_start, "ot_dow": chosen.get("ot_dow"),
                                "label": onboarding._WEEKDAYS[suggested_week_start]},
                               source="rpower ot_dow (unconfirmed reading)")
        except Exception as e:
            log.warning("rpower bootstrap: week-start suggestion not recorded for %s: %s", restaurant_id, e)
    # The store's history and first sync, off this thread, now — not at 3am
    # (scale audit 10/19/26 ONB-6). The showcase demo refreshes on its own.
    if not str(c["token"]).startswith("demo-store:"):
        import strategy_jobs
        strategy_jobs.enqueue_pos_onboard(restaurant_id, f"{chosen.get('cg')}:{chosen.get('store_mid')}")
    return {"ok": True, "store": chosen, "applied": sorted(fields),
            "suggested_week_start_day": suggested_week_start}


# ── the store's lists, kept per consolidation group (scale audit 10/19/26
# A3-07) ─────────────────────────────────────────────────────────────────────
#
# Sales types, void reasons, jobs, employees, payroll categories, the menu,
# its categories and departments, tables, rooms, meal times, profit centers,
# payment methods, payout categories and price levels were each read per
# process with a 10-minute cache: the web process and the queue worker each
# found them cold every night, menuitem/salescategory were read by two
# readers and job/getbycg by three, and every store in one group read the
# same `getbycg` list again — about 15 requests on every cold archive. Each
# list is now one row of pos_catalogs per (group[, store], path), read by
# every process and every store of the group, refreshed when it is older
# than CATALOG_MAX_AGE_SECONDS. A night that meets an id a kept list does
# not name reads that list afresh once (archive_rows: refresh on a miss), so
# a sales type, item or employee added today is never filed as unknown.
# A web request uses the kept copy at any age and never reads a list live
# (A3-09) — except inside allow_live_reads(), which no web route uses since
# the push preview went to the kept lists (blind re-audit 10/20/26 R3-07).
#
# Audit #23 (10/21/26):
#   * A1-20: a kept list is refreshed when it is a day old (was six hours) —
#     a night or a punch that names an id the list lacks already reads it
#     afresh (archive_rows' refresh on a miss, _people_for), so the age
#     refresh only catches renames; one unknown employee is read alone
#     (employee/getbymid) instead of the whole group's list.
#   * A7-03 / A3-07: a list is kept per CREDENTIAL and group (scope
#     "c<credential id>:<cg>[:<store>]", or "t<token digest>:<cg>…" for a
#     store on a legacy per-row token) — two RPOWER customers that happen to
#     share a cg number can never read each other's lists — and employee
#     records are PROJECTED before they are kept: names, payroll id and term
#     date, never the phone, mobile or anything else RPOWER sends; a phone
#     survives only as a keyed digest (phone_key), which is all the staff
#     sign-up check needs. A scope no bound restaurant uses any more is
#     purged (purge_catalogs, nightly in ops.prune_ledgers).
CATALOG_MAX_AGE_SECONDS = float(os.getenv("RPOWER_CATALOG_MAX_AGE_SECONDS", "86400"))
_list_cache = _LRU(CACHE_MAX_KEYED)

# What is kept of an employee record (A7-03). Every reader of the kept list
# reads only these (employee_display_name, distinct_employee_names,
# _name_tag, pos_employees, fetch_people).
EMPLOYEE_KEEP = ("mid", "fname", "mname", "lname", "name", "aname", "payroll_id", "term_date")
_PHONE_FIELDS = ("phone", "mobile", "cell", "phone2")
PHONE_KEY_SECRET = "pos_phone_keys"


def phone_key(raw):
    """A keyed digest of a phone number's last ten digits (HMAC under this
    install's kept secret), or None — so a POS phone can be MATCHED (the
    staff sign-up check, A7-10) without being kept. Never reversible from a
    database copy without app_secrets."""
    import hashlib
    import hmac
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())[-10:]
    if len(digits) < 10:
        return None
    try:
        from models import kept_secret
        key = kept_secret(PHONE_KEY_SECRET, 1)
    except Exception:
        key = None
    if not key:
        return None
    return hmac.new(key, digits.encode(), hashlib.sha256).hexdigest()[:32]


def _project(path, rows):
    """A list as it may be kept: employee records cut to EMPLOYEE_KEEP plus
    their phones' keyed digests (`phone_keys`); every other list as sent."""
    if path not in ("employee/getbycg", "employee/getbymid"):
        return rows
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        keep = {k: r.get(k) for k in EMPLOYEE_KEEP if k in r}
        keys = sorted({k for k in (phone_key(r.get(f)) for f in _PHONE_FIELDS) if k})
        if keys:
            keep["phone_keys"] = keys
        elif r.get("phone_keys"):
            keep["phone_keys"] = list(r["phone_keys"])
        out.append(keep)
    return out


def catalog_scope(token, cg, credential_id=None, store_mid=None) -> str:
    """The pos_catalogs scope for a store's list (A7-03 / A3-07)."""
    if str(token or "").startswith("demo-store:"):
        head = "demo:"
    elif credential_id:
        head = f"c{int(credential_id)}:"
    else:
        import credentials
        head = f"t{credentials.fingerprint(token)[:12]}:"
    return head + str(cg) + (f":{store_mid}" if store_mid else "")


def _scope(restaurant_id, token, base, by_store):
    """The scope for the store _ctx just resolved on this thread (its
    credential is the one _ctx noted for this token)."""
    cred = getattr(_io_ctx, "cred", None) if getattr(_io_ctx, "tok", None) == token else None
    return catalog_scope(token, base["cg"], cred, base["storemid"] if by_store else None)


class _flag:
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.prev = getattr(_io_ctx, self.name, False)
        setattr(_io_ctx, self.name, True)
        return self

    def __exit__(self, *exc):
        setattr(_io_ctx, self.name, self.prev)
        return False


def fresh_lists():
    """`with rpower.fresh_lists():` — every list read inside goes to RPOWER
    (and is kept again), past every cache."""
    return _flag("fresh")


def allow_live_reads():
    """`with rpower.allow_live_reads():` — an explicit action on a web
    request may read a list RPOWER has not been asked for yet. No route
    uses it since 10/20/26 (R3-07: the push preview reads the kept lists)."""
    return _flag("allow_live")


def _fresh() -> bool:
    return bool(getattr(_io_ctx, "fresh", False))


def _list(restaurant_id: int, path: str, by_store: bool = False, sortorder="name", extra=None) -> list:
    """Every row of one RPOWER configuration list for this store's group (or
    the store itself, `by_store`): this process's copy for
    CATALOG_CACHE_SECONDS, else the group's kept copy (pos_catalogs) while
    younger than CATALOG_MAX_AGE_SECONDS, else RPOWER — and kept."""
    import pos_io
    token, base = _ctx(restaurant_id)
    scope = _scope(restaurant_id, token, base, by_store)
    key = (scope, path)
    page_load = _reader_only() and not getattr(_io_ctx, "allow_live", False)
    if not _fresh():
        hit = _list_cache.get(key)
        if hit and time.monotonic() - hit[0] < CATALOG_CACHE_SECONDS:
            return hit[1]
        kept = pos_io.catalog_get("rpower", scope, path)
        if kept is not None and (page_load or time.time() - kept[0] < CATALOG_MAX_AGE_SECONDS):
            _list_cache[key] = (time.monotonic(), kept[1])
            return kept[1]
    if page_load:
        raise RPowerArchiveMiss(f"RPOWER's {path} list is not kept yet; not read live during a page load")
    params = dict(base) if by_store else {"cg": base["cg"]}
    params.update(extra or {})
    if sortorder:
        params["sortorder"] = sortorder
    rows = _project(path, _paged(token, path, params))
    pos_io.catalog_put("rpower", scope, path, rows)
    _list_cache[key] = (time.monotonic(), rows)
    return rows


def _list_add_one(restaurant_id: int, list_path: str, mid_path: str, mid) -> bool:
    """Read ONE record by id (`mid_path`, e.g. employee/getbymid — collection
    #6 and its siblings) and add it to the kept list `list_path` (A1-20),
    instead of re-reading the group's whole list for one new hire. True when
    the record came back and was kept. Never for the simulated store."""
    import pos_io
    token, base = _ctx(restaurant_id)
    if str(token).startswith("demo-store:") or not mid:
        return False
    body = _request(token, mid_path, {"mid": str(mid)})
    recs = [r for r in (body if isinstance(body, list) else [body]) if isinstance(r, dict) and r.get("mid")]
    if not recs:
        return False
    scope = _scope(restaurant_id, token, base, False)
    kept = pos_io.catalog_get("rpower", scope, list_path)
    rows = list(kept[1]) if kept else list(_list(restaurant_id, list_path))
    have = {str(r.get("mid")) for r in rows}
    rows += [r for r in _project(list_path, recs) if str(r.get("mid")) not in have]
    pos_io.catalog_put("rpower", scope, list_path, rows)
    _list_cache[(scope, list_path)] = (time.monotonic(), rows)
    return True


def _live_scopes(db_path=None) -> set:
    """The group-level scopes (no store part) of every restaurant still bound
    to an RPOWER store."""
    import models
    import credentials
    conn = models.get_conn(db_path) if db_path else models.get_conn()
    try:
        rows = conn.execute("SELECT rpower_cg, rpower_token, rpower_credential_id FROM restaurants "
                            "WHERE COALESCE(rpower_store_mid,'') <> '' AND rpower_cg IS NOT NULL").fetchall()
    finally:
        conn.close()
    out = set()
    for r in rows:
        cred = r["rpower_credential_id"]
        tok = None if cred else credentials.decrypt(r["rpower_token"])
        if not cred and not tok:
            continue
        out.add(catalog_scope(tok or "", r["rpower_cg"], cred))
    return out


def _group_scope(scope: str):
    """'c3:9999:12' -> 'c3:9999'; a scope from before A7-03 ('9999:12',
    'demo:9999') -> its own group part, which no live scope matches unless
    it is the simulated store's."""
    parts = str(scope).split(":")
    if parts[0] == "demo" or parts[0][:1] in ("c", "t"):
        return ":".join(parts[:2])
    return None                                # the old cg-only form: never live


POS_CATALOG_ORPHAN_DAYS = int(os.getenv("RETAIN_POS_CATALOG_ORPHAN_DAYS", "7"))


def purge_catalogs(db_path=None, orphan_days=None) -> int:
    """Delete RPOWER lists no bound restaurant reads any more (A7-03): a
    scope whose group no restaurant is bound to (a disconnect, an account
    delete, a credential retired) once it has not been refreshed for
    `orphan_days` (RETAIN_POS_CATALOG_ORPHAN_DAYS, 7), and at once every list
    kept under the pre-A7-03 cg-only scope — those held RPOWER's employee
    records whole (phones and all). Returns rows deleted. Nightly
    (ops.prune_ledgers) and once at boot."""
    import models
    days = POS_CATALOG_ORPHAN_DAYS if orphan_days is None else int(orphan_days)
    live = _live_scopes(db_path)
    cutoff = time.time() - max(0, days) * 86400.0
    conn = models.get_conn(db_path) if db_path else models.get_conn()
    n = 0
    try:
        rows = conn.execute("SELECT scope, path, fetched_at FROM pos_catalogs WHERE provider='rpower'").fetchall()
        for r in rows:
            group = _group_scope(r["scope"])
            if group is not None and group in live:
                continue
            if group is not None and float(r["fetched_at"] or 0) >= cutoff:
                continue                       # an orphan, but recent: a reconnect may want it
            n += conn.execute("DELETE FROM pos_catalogs WHERE provider='rpower' AND scope=? AND path=?",
                              (r["scope"], r["path"])).rowcount
        conn.commit()
    finally:
        conn.close()
    _list_cache.clear()
    return int(n or 0)


def migrate_catalog_scopes(db_path=None) -> dict:
    """Once per boot, idempotent (A7-03 / A3-07): every list kept under the
    old cg-only scope is copied, projected, to the new scope of each bound
    restaurant on that group — so no store reads its lists cold after the
    deploy — and then the old row (which held employees' phones) is
    deleted. {moved, deleted}."""
    import models
    import credentials
    out = {"moved": 0, "deleted": 0}
    conn = models.get_conn(db_path) if db_path else models.get_conn()
    try:
        old = [dict(r) for r in conn.execute("SELECT scope, path, payload, fetched_at FROM pos_catalogs "
                                             "WHERE provider='rpower'").fetchall()
               if _group_scope(r["scope"]) is None or str(r["scope"]).startswith("demo:")]
        bound = conn.execute("SELECT rpower_cg, rpower_store_mid, rpower_token, rpower_credential_id FROM restaurants "
                             "WHERE COALESCE(rpower_store_mid,'') <> '' AND rpower_cg IS NOT NULL").fetchall()
    finally:
        conn.close()
    targets = []
    for b in bound:
        cred = b["rpower_credential_id"]
        tok = None if cred else (credentials.decrypt(b["rpower_token"]) or "")
        if not cred and not tok:
            continue
        targets.append((str(b["rpower_cg"]), str(b["rpower_store_mid"]), tok or "", cred))
    for r in old:
        scope = str(r["scope"])
        demo = scope.startswith("demo:")
        parts = scope.split(":")[1:] if demo else scope.split(":")
        cg, store = parts[0], (parts[1] if len(parts) > 1 else None)
        try:
            rows = json.loads(r["payload"] or "[]")
        except (TypeError, ValueError):
            rows = []
        rows = _project(r["path"], rows)
        dests = set()
        for t_cg, t_store, tok, cred in targets:
            if t_cg != cg or (store is not None and t_store != store):
                continue
            if demo != str(tok).startswith("demo-store:"):
                continue
            dests.add(catalog_scope(tok, cg, cred, store))
        conn = models.get_conn(db_path) if db_path else models.get_conn()
        try:
            for dest in sorted(dests - {scope}):
                if conn.execute("SELECT 1 FROM pos_catalogs WHERE provider='rpower' AND scope=? AND path=?",
                                (dest, r["path"])).fetchone():
                    continue
                conn.execute("INSERT INTO pos_catalogs (provider, scope, path, payload, rows_n, fetched_at) "
                             "VALUES ('rpower',?,?,?,?,?)",
                             (dest, r["path"], json.dumps(rows, default=str), len(rows), float(r["fetched_at"] or 0)))
                out["moved"] += 1
            if demo:
                # The simulated store keeps its scope; only its employees are projected.
                if r["path"].startswith("employee/"):
                    conn.execute("UPDATE pos_catalogs SET payload=? WHERE provider='rpower' AND scope=? AND path=?",
                                 (json.dumps(rows, default=str), scope, r["path"]))
            else:
                out["deleted"] += conn.execute("DELETE FROM pos_catalogs WHERE provider='rpower' AND scope=? "
                                               "AND path=?", (scope, r["path"])).rowcount
            conn.commit()
        finally:
            conn.close()
    _list_cache.clear()
    return out


def _one_store_scope(fn):
    """Run one store's unit of RPOWER work (a night's archive, a shifts
    build, a live read) inside models.read_scope() (Audit #23 A1-22): the
    restaurant row is read and its token resolved once for the unit, not by
    every helper it calls. One unit only — never a loop over stores, which
    must see a row change under it. Shares an open scope."""
    import functools

    @functools.wraps(fn)
    def run(*a, **k):
        import models
        with models.read_scope():
            return fn(*a, **k)
    return run


def _ctx(restaurant_id: int) -> tuple:
    """(token, base params) for a connected restaurant, or raise."""
    c = _creds(restaurant_id)
    if not (c["token"] and c["cg"] and c["store_mid"]):
        raise RPowerAuthError(
            "RPOWER is not fully connected — run bootstrap to resolve the store.")
    _note_store(restaurant_id, c["store_mid"])          # the ledger's and the budget's store
    _io_ctx.cred = c.get("credential_id")              # the budget's credential (A7-01)
    _io_ctx.tok = c["token"]
    return c["token"], {"cg": c["cg"], "storemid": c["store_mid"]}


# ── sales type: what actually counts as revenue ─────────────────────────────

def sales_types(restaurant_id: int) -> dict:
    """Every sales type for this store, keyed by mid.

    This is the difference between net sales and a number that quietly
    includes comps, voids, gratuities and waste. RPOWER models each type with
    `impacts_sales` / `impacts_costs` plus explicit `type_comp`, `type_waste`,
    `type_void`-style flags, and a ticket line carries the type it was rung
    under. Summing sales without consulting this returns gross activity, and
    every food cost percentage computed against it is wrong by whatever the
    restaurant comps in a week.
    """
    hit = _sales_type_cache.get(restaurant_id)
    if hit and not _fresh() and time.monotonic() - hit[0] < SALES_TYPE_CACHE_SECONDS:
        return hit[1]
    token, base = _ctx(restaurant_id)
    rows = _list(restaurant_id, "salestype/getbycg", sortorder=None)
    out = {str(r.get("mid")): r for r in rows if r.get("mid")}
    _sales_type_cache[restaurant_id] = (time.monotonic(), out)
    return out


# Read once per this many seconds: every sales read consults the list, and
# the nightly jobs read several days in a row (9/29/26).
SALES_TYPE_CACHE_SECONDS = 600
_sales_type_cache = _LRU(CACHE_MAX_RESTAURANTS)


def clear_caches():
    """Every per-restaurant list this module caches (the test setup calls it)."""
    for c in (_sales_type_cache, _menu_cache, _people_cache, _void_reason_cache, _catalog_cache, _closed_cache,
              _job_cat_cache, _list_cache, _archive_refreshed, _live_seen, _live_marked, _people_refreshed,
              _scope_cache):
        c.clear()
    try:
        import pos_io
        pos_io.reset_for_tests()
    except Exception:
        pass


def _counts_as_revenue(row: dict, types: dict) -> bool:
    """Whether one ticketsales line belongs in net sales."""
    if row.get("voided"):
        return False
    st = types.get(str(row.get("slstype_mid")))
    if st is None:
        # An unknown sales type is not assumed to be revenue. RPOWER's own
        # data drives this decision and a missing lookup means we cannot make
        # it — counting it in would inflate sales, which is the direction that
        # makes food cost % look better than it is.
        return False
    if not st.get("impacts_sales"):
        return False
    for flag in ("type_comp", "type_waste", "type_error", "type_return",
                 "type_refund", "type_grat", "type_hidgrat", "type_housegrat",
                 "type_tax", "type_fee"):
        if st.get(flag):
            return False
    return True


# ── the reads pos.py and the modules need ───────────────────────────────────

def fetch_business_days(restaurant_id: int, start_date, end_date) -> dict:
    """{'YYYY-MM-DD': net_sales} over a business-date range.

    Matches toast.fetch_business_days' contract exactly so cogs.py and the
    labor normaliser can use either provider without knowing which.

    Net, not gross: every line is filtered through `_counts_as_revenue`.
    Built from `ticketsales` rather than `closeday` because closeday records
    the close event (from/thru date and shift) and not the money.
    """
    return _business_days(restaurant_id, start_date, end_date)


def _business_days(restaurant_id: int, start_date, end_date, use_archive: bool = True) -> dict:
    token, base = _ctx(restaurant_id)
    types = sales_types(restaurant_id)
    out = {}
    rows = (_day_rows(restaurant_id, token, base, "ticketsales/getbybusinessdate", start_date, end_date)
            if use_archive else
            [r for cs, ce in _chunk_range(start_date, end_date)
             for r in _paged(token, "ticketsales/getbybusinessdate",
                             {**base, "startdate": _d(cs), "enddate": _d(ce), "sortorder": "date"})])
    for row in rows:
        day = _biz_date(row.get("date"))
        if not day or not _counts_as_revenue(row, types):
            continue
        out[day] = round(out.get(day, 0.0) + float(row.get("sales") or 0), 2)
    return out


def _loss_kind(row: dict, types: dict):
    """comp | void | refund, or None for an ordinary sale.

    A void is either a line flagged `voided` or a line of an "error" sales
    type: RPOWER stores the store's voids as Error Correct lines (Simple
    EJ's, 9/29/26: 64 in a week, `voided` 0 on every line of 9,720), so a
    flag-only test counted no voids at all. A transfer to another check is a
    "nonsale" line and is no loss."""
    st = types.get(str(row.get("slstype_mid"))) or {}
    if row.get("voided") or st.get("type_error"):
        return "void"
    if st.get("type_comp"):
        return "comp"
    if st.get("type_refund") or st.get("type_return"):
        return "refund"
    return None


VOID_REASON_CACHE_SECONDS = 600
_void_reason_cache = _LRU(CACHE_MAX_RESTAURANTS)


def void_reasons(restaurant_id: int) -> dict:
    """{reason mid: name} — voidreason/getbycg, read once per
    VOID_REASON_CACHE_SECONDS. The reason on every comp and void line was
    kept as a bare id and never read (RPower endpoint audit, 9/29/26)."""
    hit = _void_reason_cache.get(restaurant_id)
    if hit and not _fresh() and time.monotonic() - hit[0] < VOID_REASON_CACHE_SECONDS:
        return hit[1]
    token, base = _ctx(restaurant_id)
    out = {str(r["mid"]): _tidy_name(r.get("name")) for r in
           _list(restaurant_id, "voidreason/getbycg") if r.get("mid")}
    _void_reason_cache[restaurant_id] = (time.monotonic(), out)
    return out


def _loss_amount(row: dict) -> float:
    """What the line was worth: the item's regular price x qty, else its
    price x qty, else |sales|. RPOWER leaves both prices 0 on comp and
    error-correct lines and carries the value in `sales` — preferring a zero
    price made every comp at Simple EJ's $0.00 (9/29/26: $332.59 in a week)."""
    qty = abs(float(row.get("qty") or 1) or 1)
    for key in ("regular_price", "price"):
        try:
            unit = abs(float(row.get(key) or 0))
        except (TypeError, ValueError):
            unit = 0.0
        if unit > 0:
            return round(unit * qty, 2)
    try:
        return round(abs(float(row.get("sales") or 0)), 2)
    except (TypeError, ValueError):
        return 0.0


def fetch_loss_lines(restaurant_id: int, start_date, end_date) -> list:
    """Every comp, void and refund line over a business-date range.

    Fields come from RPOWER's documented ticketsales response: `voided`,
    `slstype_mid` (resolved through sales_types), `mgr_mid` (the manager who
    approved a comp or refund), `voidmgr_mid` and `voidrsn_mid` (who voided it
    and why), `shift`, `qty`, `price`, `regular_price`, `sales`.

    The APPROVER is the attribution, not the server. That is the loss-
    prevention convention and it is also the fair one: a comp is a manager's
    decision, and the data says whose.

    Checked live 9/29/26: a comp line's `sales` IS the comped value and both
    prices are 0, so _loss_amount falls back to |sales|. Each line carries its
    reason's name (void_reasons) and the approver's name — "as reported by
    RPOWER".
    """
    token, base = _ctx(restaurant_id)
    types = sales_types(restaurant_id)
    try:
        reasons = void_reasons(restaurant_id)
    except RPowerAuthError:
        raise
    except Exception as e:                       # the lines still count; only the words are missing
        log.warning("[rpower] void reasons unavailable for %s: %s", restaurant_id, e)
        reasons = {}
    try:
        names = (_people_cached(restaurant_id).get("employees") or {})
    except RPowerAuthError:
        raise
    except Exception as e:
        log.warning("[rpower] manager names unavailable for %s: %s", restaurant_id, e)
        names = {}
    out = []
    for row in _day_rows(restaurant_id, token, base, "ticketsales/getbybusinessdate", start_date, end_date):
        kind = _loss_kind(row, types)
        if not kind:
            continue
        approver = row.get("voidmgr_mid") if kind == "void" else row.get("mgr_mid")
        approver = str(approver).strip() if approver not in (None, "", 0, "0") else None
        rmid = str(row.get("voidrsn_mid") or "").strip()
        out.append({"business_date": _biz_date(row.get("date")), "kind": kind,
                    "amount": _loss_amount(row), "approver": approver,
                    "approver_name": names.get(approver) if approver else None,
                    "reason": reasons.get(rmid) or None, "reason_id": rmid or None,
                    "shift": row.get("shift")})
    return out


def fetch_order_selections(restaurant_id: int, business_date) -> list:
    """One business date's sold items, in toast.fetch_order_selections' shape:
    [{"item": {"guid": <menuitem_mid>}, "quantity": n}, ...].

    Shaped this way on purpose — inventory_ledger.compute_daily_depletion
    reads exactly this, and matching it means recipe-driven depletion, menu
    discovery and menu_item_sales all work for an RPOWER restaurant without a
    line of change in that module.

    Reads `ticketsales`, NOT `ticketitem`: RPOWER states plainly that ticket
    items are for reconstructing a receipt and must not be used for totals,
    and ticketitem's modifier rows would count a burger with four modifiers
    as five items sold.
    """
    token, base = _ctx(restaurant_id)
    types = sales_types(restaurant_id)
    day = _d(business_date)
    rows = _day_rows(restaurant_id, token, base, "ticketsales/getbybusinessdate", day, day)
    reasons = None

    by_item = {}
    for row in rows:
        if row.get("voided"):
            continue
        st = types.get(str(row.get("slstype_mid")))
        # A comped or wasted plate still LEAVES THE KITCHEN — it depletes
        # stock even though it earns nothing. So depletion deliberately keeps
        # what `_counts_as_revenue` drops, and only skips lines that never
        # became food: voids, returns and refunds.
        if st and (st.get("type_return") or st.get("type_refund")):
            continue
        # RPOWER's voids are Error Correct lines with `voided` 0 (see
        # _loss_kind): "Rang In Wrong" was counted as a dish sold and
        # depleted (food & bar audit 10/16/26 POS-07). One is kept only when
        # its reason says the food was made (pos_food_waste.is_waste_reason:
        # "Error Made", "Sent Back") — that plate left the kitchen.
        if st and st.get("type_error"):
            if reasons is None:
                try:
                    reasons = void_reasons(restaurant_id)
                except RPowerAuthError:
                    raise
                except Exception as e:           # no reason words: a void is not counted as made
                    log.warning("[rpower] void reasons unavailable for %s: %s", restaurant_id, e)
                    reasons = {}
            from pos_food_waste import is_waste_reason
            if not is_waste_reason(reasons.get(str(row.get("voidrsn_mid") or "").strip())):
                continue
        guid = row.get("menuitem_mid")
        if not guid:
            continue
        qty = float(row.get("qty") or 0)
        if qty <= 0:
            continue
        by_item[guid] = by_item.get(guid, 0.0) + qty
    return [{"item": {"guid": g}, "quantity": q} for g, q in by_item.items()]


def fetch_menu_items(restaurant_id: int) -> list:
    """The menu, for discovery and costing.

    `last_cost` is RPOWER's own cost-per-item where a store maintains it —
    a theoretical plate cost with no recipe mapping required. It is commonly
    0 (RPOWER's own examples show 0), so it is passed through as-is and the
    caller decides whether it is usable rather than this module pretending
    an unpopulated field is a measurement.
    """
    token, base = _ctx(restaurant_id)
    rows = _list(restaurant_id, "menuitem/getbycg")
    return [{
        "guid": r.get("mid"),
        "name": r.get("name"),
        "is_modifier": bool(r.get("is_mod")),
        "sales_category_mid": r.get("slscat_mid"),
        "sales_type_mid": r.get("slstype_mid"),
        "last_cost": r.get("last_cost"),
        "plu": r.get("plu") or None,
        "upc": r.get("upc") or None,
        "aqu_count": r.get("aqu_count"),
    } for r in rows if r.get("mid")]


def fetch_sales_catalog(restaurant_id: int) -> dict:
    """The store's sales categories (with their department) and menu items,
    every bar-relevant field RPOWER documents for them (bar audit 10/14/26
    RPW-9 / F5) — what pos_archive.sync_catalog keeps:

      categories  salescategory/getbycg: name, slsdep_mid -> the
                  department's name (salesdepartment/getbycg), is_bar,
                  alt_qty_unit, alt_rpt_unit, aqu_in_aru (the alternate
                  quantity: ounces poured, where a store configures it)
      items       menuitem/getbycg: name, slscat_mid, is_mod, last_cost,
                  plu, upc, aqu_count

    Field names are the collection's (docs/integrations/
    rpower_core_api.postman.json). Values are passed through as RPOWER sends
    them; 0 / "" mean "not kept" (last_cost is commonly 0), which the
    readers decide, never this."""
    menu = _catalog(restaurant_id, "menuitem/getbycg")
    cats = _catalog(restaurant_id, "salescategory/getbycg")
    deps = _catalog(restaurant_id, "salesdepartment/getbycg")
    categories = []
    for mid, c in cats.items():
        dep_id = _id(c.get("slsdep_mid"))
        dep = (deps.get(dep_id or "") or {}).get("name")
        categories.append({
            "category_id": str(mid), "name": (str(c.get("name") or "").strip() or None),
            "department_id": dep_id, "department": (str(dep).strip() or None) if dep else None,
            "is_bar": 1 if _num(c.get("is_bar")) else 0,
            "alt_qty_unit": (str(c.get("alt_qty_unit") or "").strip() or None),
            "alt_rpt_unit": (str(c.get("alt_rpt_unit") or "").strip() or None),
            "aqu_in_aru": _num(c.get("aqu_in_aru"), None)})
    items = []
    for mid, m in menu.items():
        items.append({
            "item_id": str(mid), "name": clean_item_name(m.get("name")) or None,
            "category_id": _id(m.get("slscat_mid")), "is_mod": 1 if _num(m.get("is_mod")) else 0,
            "last_cost": _num(m.get("last_cost"), None),
            "plu": (str(m.get("plu") or "").strip() or None), "upc": (str(m.get("upc") or "").strip() or None),
            "aqu_count": _num(m.get("aqu_count"), None)})
    return {"categories": categories, "items": items}


# ── menu names: what a sale's menuitem_mid is called ────────────────────────
#
# A ticketsales line names its item only by `menuitem_mid`, so discovery
# stored Simple EJ's 373 items as 19-digit ids ("2256405207682086913") and the
# recipe editor was an endless list of numbers (owner, 9/28/26). The menu
# (menuitem/getbycg) carries the name and the sales category; RPOWER's
# `is_mod` flag was 0 on every modifier at EJ's, so what a line IS comes from
# the store's own sales category: a category in parentheses — "(Modifiers)",
# "(Food Add-Ons)", "(Liquor Add-Ons)" — groups modifiers and add-ons, and a
# discount, gratuity, gift card, open-price or retail category is money, not
# a menu item. Checked against EJ's live menu: all 373 sold ids resolved.
MENU_CACHE_SECONDS = 600
_menu_cache = _LRU(CACHE_MAX_RESTAURANTS)
_NOT_ITEM_CATEGORY_RE = re.compile(r"discount|gratuit|gift\s*card|\bopen\b|\bretail\b", re.I)


def clean_item_name(raw) -> str:
    """A POS button name as a person reads it: EJ's names end in a stray
    backtick ("Espresso Shot`")."""
    return re.sub(r"[`\s]+$", "", str(raw or "")).strip()


def menu_item_kind(category, is_mod=False) -> str:
    """'modifier', 'not_item' or 'dish' (a drink is a menu item too) from the
    store's sales category — see the note above."""
    c = str(category or "").strip()
    if is_mod or (c.startswith("(") and c.endswith(")")):
        return "modifier"
    if _NOT_ITEM_CATEGORY_RE.search(c):
        return "not_item"
    return "dish"


def menu_lookup(restaurant_id: int) -> dict:
    """{menuitem_mid: {"name", "category", "kind"}} for this store's menu,
    read once per MENU_CACHE_SECONDS (a discovery pass scans a week of days)."""
    hit = _menu_cache.get(restaurant_id)
    if hit and not _fresh() and time.monotonic() - hit[0] < MENU_CACHE_SECONDS:
        return hit[1]
    token, base = _ctx(restaurant_id)
    cats = {str(r.get("mid")): r.get("name") or ""
            for r in _list(restaurant_id, "salescategory/getbycg")}
    out = {}
    for m in fetch_menu_items(restaurant_id):
        category = clean_item_name(cats.get(str(m.get("sales_category_mid")), ""))
        out[str(m["guid"])] = {"name": clean_item_name(m.get("name")), "category": category or None,
                               "kind": menu_item_kind(category, m.get("is_modifier"))}
    _menu_cache[restaurant_id] = (time.monotonic(), out)
    return out


# ── the nightly DSR: one day's sales, and whether the store closed it ───────

# A ticketsales line whose menu item, category or department is not in the
# lookups is still money the store took; it is filed here rather than
# dropped, and the DSR shows it as unmapped.
UNASSIGNED_DEPARTMENT = "Unassigned"


SOLD_LOOKBACK_DAYS = 90


def department_sold(restaurant_id: int, days: int = SOLD_LOOKBACK_DAYS, today=None) -> dict:
    """{department: [item categories]} that actually SOLD in the last `days`
    days of the local ticket archive (pos_ticket_lines: sale, discount and
    comp lines - the kinds a night's department dollars hold): what a
    department holds when the owner maps it (dsr store.note_held). Not the
    POS's catalog - RPOWER files Gratuity, Received on Account and Tax
    Exempt under Simple EJ's "Other" beside Darts, and the owner mapped
    "Other" to Darts for the darts it sells (10/5/26). Not one night either:
    a category sold monthly (Catering, Banquet) is the department's, not
    news. {} when nothing is archived."""
    from models import get_conn
    from datetime import date as _date, timedelta as _td
    if today is None:                     # the restaurant's own date, never the server's (POS-18)
        from time_utils import restaurant_now_by_id
        today = restaurant_now_by_id(restaurant_id, naive=True).date()
    since = (today - _td(days=days)).isoformat()
    conn = get_conn()
    try:
        ids = [r["item_id"] for r in conn.execute(
            "SELECT DISTINCT item_id FROM pos_ticket_lines WHERE restaurant_id=? AND provider='rpower' "
            "AND kind IN ('sale', 'discount', 'comp') AND business_date >= ? AND item_id IS NOT NULL",
            (restaurant_id, since))]
    finally:
        conn.close()
    if not ids:
        return {}
    menu = _catalog(restaurant_id, "menuitem/getbycg")
    cats = _catalog(restaurant_id, "salescategory/getbycg")
    deps = _catalog(restaurant_id, "salesdepartment/getbycg")
    out = {}
    for mid in ids:
        cat = cats.get(str((menu.get(str(mid)) or {}).get("slscat_mid"))) or {}
        name = (cat.get("name") or "").strip()
        dep = ((deps.get(str(cat.get("slsdep_mid"))) or {}).get("name") or "").strip() or UNASSIGNED_DEPARTMENT
        if name:
            out.setdefault(dep, set()).add(name)
    return {d: sorted(v) for d, v in out.items()}

# How far back a closeday row may start and still cover the day asked about
# (a store that missed a close and closed two days at once: from_date is the
# earlier day, thru_date the later).
CLOSEDAY_LOOKBACK_DAYS = 3

# UNVERIFIED until the first live night — RPOWER's collection documents these
# fields but not their semantics, and there is no sandbox to observe them:
#   * a comp line's `sales`: the comped value, zero or negative? The day's
#     comps take |sales|, else |regular_price x qty|, else |price x qty|, and
#     gross counts that value once. If comp lines turn out to sit BESIDE a
#     normal sale line for the same plate, gross double-counts the comp —
#     net is right either way (it is the same revenue lines
#     fetch_business_days sums). `source_checks` puts RPOWER's own per-ticket
#     type_sale / type_comp / type_discnt totals beside ours to settle it.
#   * a discount line's sign (negative in the ticket summary sample:
#     discount -3.75, type_discnt -3.75).
#   * whether a closeday row covering the date means every shift is closed
#     (from_shift / thru_shift are read but not interpreted).
DAY_SALES_UNVERIFIED = True


# The sales types that ring money off a check: a discount, a promotion and a
# "hash" (house) discount are all discounts. The archive (line_kind) and the
# nightly report (_line_kind) each had their own list — the archive missed
# type_promo and the report type_hashdisc, so a $5 promo was a negative SALE
# in the archive and a discount in the report (food & bar audit 10/16/26
# POS-06). Both now classify through _classify.
DISCOUNT_FLAGS = ("type_discnt", "type_promo", "type_hashdisc")


def _classify(row: dict, types: dict) -> str:
    """The ONE classification of a ticketsales line:
    void | comp | refund | unknown | nonsale | excluded | discount | sale.

    sale + discount are exactly the lines `_counts_as_revenue` keeps, so the
    report's net, fetch_business_days' net and the archive's ticket net are
    one number by construction."""
    if row.get("voided"):
        return "void"
    st = types.get(str(row.get("slstype_mid")))
    if st is None:
        return "unknown"
    # An "error" sales type is a void, as in _loss_kind: RPOWER stores the
    # store's voids as Error Correct lines (`voided` is 0 on every line at
    # Simple EJ's). Without this the night's voids read $0 while the service
    # block listed 6 voided lines for $34.48, and the report called "voids
    # zero" a win beside them (owner, 10/13/26). Net is unchanged: an error
    # line never counted as revenue.
    if st.get("type_error"):
        return "void"
    if st.get("type_comp"):
        return "comp"
    if st.get("type_refund") or st.get("type_return"):
        return "refund"
    if not _counts_as_revenue(row, types):
        return "nonsale" if st.get("type_nonsale") else "excluded"
    if any(st.get(f) for f in DISCOUNT_FLAGS):
        return "discount"
    return "sale"


def _line_kind(row: dict, types: dict) -> str:
    """What one ticketsales line is to the day's sales (the nightly report):
    sale | discount | comp | refund | void | excluded | unknown — _classify,
    with a non-sale line excluded like any other line outside revenue."""
    kind = _classify(row, types)
    return "excluded" if kind == "nonsale" else kind


def _line_value(row: dict) -> float:
    """The dollar value of a comp, void or refund line (see
    DAY_SALES_UNVERIFIED): |sales|, else the rung price times quantity."""
    sales = float(row.get("sales") or 0)
    if sales:
        return abs(sales)
    qty = abs(float(row.get("qty") or 1) or 1)
    for key in ("regular_price", "price"):
        unit = float(row.get(key) or 0)
        if unit:
            return abs(unit) * qty
    return 0.0


def _hour_of(raw) -> Optional[str]:
    """"HH" from an RPOWER local wall-clock stamp ("2020-01-21T11:16:00").
    RPOWER's "never set" is 2000-01-01, which is no hour at all."""
    s = str(raw or "")
    if len(s) < 13 or s.startswith("2000-01-01"):
        return None
    return s[11:13] if s[11:13].isdigit() else None


def _parts():
    return {"gross": 0.0, "discounts": 0.0, "comps": 0.0}


@_one_store_scope
def fetch_day_sales(restaurant_id: int, business_date) -> dict:
    """One business date's sales in pos.fetch_day_sales' PARTS shape — pos
    nets them by pos.NET_DEDUCTIONS, the one place gross becomes net.

    Reads (all by business date, all paged):
      ticketsales/getbybusinessdate  every line: the money, by sales type
      ticket/getbybusinessdate       per ticket: guest_count, tax, open_dttm,
                                     is_cancelled, and RPOWER's own type_*
                                     totals for source_checks
      salestype/getbycg              via sales_types(): what each line IS
      menuitem/getbycg -> salescategory/getbycg -> salesdepartment/getbycg
                                     the line's department (Erik's sheet
                                     splits by these)

    Every line goes through the same sales-type rules as the rest of this
    module (`_line_kind` over `_counts_as_revenue`): gross is sale lines plus
    the value of comped items; discounts are the discount/promo lines
    (RPOWER rings them as negative lines, so the figure is their negation);
    comps are the comp lines' value; voids and refunds are reported and never
    in gross. A line whose sales type is missing from the lookup is counted
    in source_checks, never guessed into revenue.

    A transaction is a ticket with at least one sale line. Guests are the
    sum of guest_count over those tickets, None when the store does not
    record covers (all zero). Hours are the ticket's open hour, local wall
    clock — the item's own time when the ticket is missing.
    """
    token, base = _ctx(restaurant_id)
    day = _d(business_date)
    types = sales_types(restaurant_id)
    lines = _day_rows(restaurant_id, token, base, "ticketsales/getbybusinessdate", day, day)
    tickets = _day_rows(restaurant_id, token, base, "ticket/getbybusinessdate", day, day)
    menu = _catalog(restaurant_id, "menuitem/getbycg")
    cats = _catalog(restaurant_id, "salescategory/getbycg")
    deps = _catalog(restaurant_id, "salesdepartment/getbycg")

    def department(item):
        cat = cats.get(str((item or {}).get("slscat_mid"))) or {}
        dep = deps.get(str(cat.get("slsdep_mid"))) or {}
        return (dep.get("name") or "").strip() or UNASSIGNED_DEPARTMENT

    def sales_category(item):
        # The item category inside its department ("Darts" in "Other"):
        # what a mapped department holds (dsr block_sales.categorize).
        cat = cats.get(str((item or {}).get("slscat_mid"))) or {}
        return (cat.get("name") or "").strip() or None

    live = {}
    for t in tickets:
        if _biz_date(t.get("date")) != day or t.get("is_cancelled"):
            continue
        live[str(t.get("rid"))] = t

    total = _parts()
    voids = refunds = 0.0
    by_dep, by_hour, items, by_dep_cat = {}, {}, {}, {}
    sale_tickets = set()
    checks = {"lines": 0, "lines_sale": 0.0, "lines_discount": 0.0, "lines_comp_sales": 0.0,
              "unknown_type_lines": 0, "unknown_type_sales": 0.0, "excluded_lines": 0}
    for row in lines:
        if _biz_date(row.get("date")) != day:
            continue
        checks["lines"] += 1
        kind = _line_kind(row, types)
        sales = float(row.get("sales") or 0)
        if kind == "unknown":
            checks["unknown_type_lines"] += 1
            checks["unknown_type_sales"] = round(checks["unknown_type_sales"] + sales, 2)
            continue
        if kind == "excluded":
            checks["excluded_lines"] += 1
            continue
        if kind == "void":
            voids += _line_value(row)
            continue
        if kind == "refund":
            refunds += _line_value(row)
            continue
        item = menu.get(str(row.get("menuitem_mid"))) or {}
        filed = row.get(ARCHIVED_FILING)
        dep = (filed or {}).get("department") or department(item)
        ticket = live.get(str(row.get("ticket_rid")))
        hour = _hour_of((ticket or {}).get("open_dttm")) or _hour_of(row.get("item_dttm"))
        delta = _parts()
        if kind == "sale":
            delta["gross"] = sales
            checks["lines_sale"] = round(checks["lines_sale"] + sales, 2)
            sale_tickets.add(str(row.get("ticket_rid")))
        elif kind == "discount":
            delta["discounts"] = -sales
            checks["lines_discount"] = round(checks["lines_discount"] + sales, 2)
        else:  # comp
            value = _line_value(row)
            delta["gross"] = value
            delta["comps"] = value
            checks["lines_comp_sales"] = round(checks["lines_comp_sales"] + sales, 2)
        buckets = [total, by_dep.setdefault(dep, _parts())]
        sub = filed.get("sales_category") if filed else sales_category(item)
        if sub:
            buckets.append(by_dep_cat.setdefault(dep, {}).setdefault(sub, _parts()))
        if hour:
            buckets.append(by_hour.setdefault(hour, _parts()))
        if kind != "discount" and not item.get("is_mod"):
            # A discount line is not something sold, and a modifier is part
            # of the plate it modifies — both stay in the money, neither is
            # an "item" in the top and bottom lists.
            key = str(row.get("menuitem_mid") or "")
            it = items.setdefault(key, {"name": (clean_item_name(item.get("name"))
                                                 or (filed or {}).get("item_name") or "Unknown item"),
                                        "department": dep, "qty": 0.0, "parts": _parts(),
                                        "guid": key or None})
            buckets.append(it["parts"])
            if kind == "sale":
                it["qty"] += float(row.get("qty") or 0)
        for b in buckets:
            for k, v in delta.items():
                b[k] += v

    counted = [live[t] for t in sale_tickets if t in live]
    guests = sum(int(t.get("guest_count") or 0) for t in counted)
    tax = round(sum(float(t.get("tax") or 0) for t in live.values()), 2) if live else None
    for k in ("type_sale", "type_discnt", "type_promo", "type_comp"):
        checks[f"ticket_{k}"] = round(sum(float(t.get(k) or 0) for t in live.values()), 2)
    checks["tickets"] = len(live)
    return {
        "gross": total["gross"], "discounts": total["discounts"], "comps": total["comps"],
        "voids": round(voids, 2), "refunds": round(refunds, 2), "tax": tax,
        "transactions": len(sale_tickets),
        # Tickets missing from the summary feed carry no guest count, so a
        # partial feed cannot report covers honestly.
        "guests": guests if guests and len(counted) == len(sale_tickets) else None,
        "by_department": by_dep, "by_hour": by_hour,
        "by_department_category": by_dep_cat,
        # Sold at least once; an item that was only ever comped was served,
        # not sold, and its value is already in comps.
        "items": [it for it in items.values() if it["qty"] > 0],
        "source_checks": checks,
    }


def fetch_day_closed(restaurant_id: int, business_date) -> bool:
    """Whether the store has run its close for `business_date`: a
    closeday/getbybusinessdate row whose from_date..thru_date covers it.

    The DSR's trigger (plan §4): RPOWER writes this record when the store
    closes the day, which is the moment its tickets for the day are final.
    Asked over a few days either side, because a store that skipped a close
    closes two days in one record (from_date is the earlier one)."""
    token, base = _ctx(restaurant_id)
    day = date.fromisoformat(_d(business_date))
    rows = _paged(token, "closeday/getbybusinessdate", {
        **base, "startdate": _d(day - timedelta(days=CLOSEDAY_LOOKBACK_DAYS)),
        "enddate": _d(day + timedelta(days=1)), "sortorder": "from_date"})
    want = day.isoformat()
    for row in rows:
        start = _biz_date(row.get("from_date"))
        end = _biz_date(row.get("thru_date")) or start
        if start and start <= want <= end:
            return True
    return False


def fetch_time_entries(restaurant_id: int, start_date, end_date) -> list:
    """Timeclock punches over a date range.

    RPOWER has already split regular, overtime and doubletime hours and pay,
    and carries the job name and payroll id — so unlike Toast, none of that
    has to be re-derived. `break_minutes` is reported so a caller can decide
    whether the restaurant's breaks are paid; nothing here assumes.
    """
    token, base = _ctx(restaurant_id)
    out, seen = [], set()
    for chunk_start, chunk_end in _chunk_range(start_date, end_date):
        # timeclock/getbydaterange reads `enddate` as EXCLUSIVE (checked
        # live 9/28/26: 9/15..9/15 returned no punches, 9/15..9/16 returned
        # 9/15's), unlike the getbybusinessdate reads. Asked for its own
        # last day, each seven-day chunk dropped it - every Wednesday of
        # Simple EJ's 60-day sync was missing from Labor. So the day after
        # is asked for, and only this chunk's own days are kept.
        rows = _paged(token, "timeclock/getbydaterange", {
            **base, "startdate": _d(chunk_start), "enddate": _d(chunk_end + timedelta(days=1)),
            "sortorder": "in_dttm"})
        for r in rows:
            dt = _punch_dt(r.get("in_dttm"))
            if dt is not None and not (chunk_start <= dt.date() <= chunk_end):
                continue
            key = r.get("rid") or (r.get("emp_mid"), r.get("in_dttm"), r.get("job_mid"))
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
    return out


def _chunk_range(start_date, end_date, max_days: int = MAX_RANGE_DAYS):
    """Split a date range into request-sized chunks.

    A year-long backfill in one call would be one enormous paged read against
    an API with no documented rate limit or timeout behaviour. Chunking keeps
    each request bounded and lets a partial failure cost one week rather
    than the whole history.
    """
    start = start_date if hasattr(start_date, "toordinal") else date.fromisoformat(_d(start_date))
    end = end_date if hasattr(end_date, "toordinal") else date.fromisoformat(_d(end_date))
    if end < start:
        return
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=max_days - 1), end)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


# ── labor: timeclock -> the shifts CSV labor.py already reads ───────────────

_DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _punch_dt(raw) -> Optional[datetime]:
    """One timeclock punch as a datetime. RPOWER sends local wall-clock with
    no offset ("2019-06-23T06:58:00"), which is what the CSV wants, so this
    parses without attaching or converting a timezone."""
    if not raw:
        return None
    try:
        dt = datetime.strptime(str(raw)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    # RPOWER writes 2000-01-01T00:00:00 for "no value" — a ticket not closed,
    # a store with no kitchen screens (kvs/bump), no "needed by" time, a punch
    # never edited (checked on Simple EJ's, 9/29/26). It is never a time.
    return None if dt.year <= 2000 else dt


def punch_business_date(t: datetime) -> date:
    """The business date a punch that STARTS at local wall-clock `t` is
    worked for — the schedule's own rule (schedule_time.NIGHT_TAIL_BEFORE: a
    start before 4am is the small hours of the night before). Every RPOWER
    punch path files by it: the live clock-ins, the archive's punches, the
    nightly labor rows (blind re-audit 10/20/26 R3-10 / R9-09). The clock-in
    and archive paths used 5am, so a 4:30am baker was "hasn't clocked in"
    against a 4:30 shift and filed on the night before; the labor rows used
    the calendar date, so a 12:10am job change was set against the next
    day's sales."""
    from schedule_time import NIGHT_TAIL_BEFORE
    return t.date() - timedelta(days=1) if t.hour * 60 + t.minute < NIGHT_TAIL_BEFORE else t.date()


def _punch_window(day) -> tuple:
    """(start, end) local wall-clock of the punches worked for business date
    `day` (punch_business_date's boundary to the same one the next morning)."""
    from schedule_time import NIGHT_TAIL_BEFORE
    start = datetime.combine(day, datetime.min.time()) + timedelta(minutes=NIGHT_TAIL_BEFORE)
    return start, start + timedelta(days=1)


def _is_closed_punch(e) -> bool:
    """A finished punch: an out time that is a time. RPOWER writes an open
    punch's out_dttm as 2000-01-01T00:00:00 — a truthy string, so a bare
    `e.get("out_dttm")` counted every punch still on the clock as closed
    (blind re-audit 10/20/26 R3-06)."""
    return _punch_dt(e.get("out_dttm")) is not None


# ── who each punch belongs to ───────────────────────────────────────────────
#
# A timeclock row names its person and job only by reference: `emp_mid` /
# `payroll_id` and `job_mid` / `job_id`. `job_id` is the job's `ext_id`,
# which RPOWER's sample shows as "COOK" but a real store sets to whatever it
# likes — Simple EJ's first live sync (9/28/26) stored 16 roles such as
# "Q39U9N" and 74 employees named by payroll codes such as "20CY2G", and every
# labor screen showed them. The names live in the store's own job and
# employee lists, read once per sync; a pull where more than
# UNRESOLVED_MAX_SHARE of the punches still have no person FAILS rather than
# saving codes (the same rule toast._require_resolved applies).
UNRESOLVED_MAX_SHARE = 0.05


class RPowerUnresolved(RPowerError):
    """Too many punches could not be matched to an RPOWER employee."""


def _tidy_name(raw) -> str:
    """One name part as a person would write it: stray punctuation from data
    entry dropped, and ALL-CAPS or all-lower POS entry set in title case
    (mixed case is kept — the owner typed "McKenzie" that way)."""
    s = " ".join(str(raw or "").replace("\\", " ").split()).strip(" ,.;")
    if s and (s.isupper() or s.islower()):
        s = " ".join(w[:1].upper() + w[1:].lower() for w in s.split(" "))
    return s


def employee_display_name(emp: dict) -> str:
    """"First Last" from an RPOWER employee record: fname + lname, else the
    `name` field ("LAST, FIRST" is turned around), else ''."""
    first, last = _tidy_name(emp.get("fname")), _tidy_name(emp.get("lname"))
    middle = _tidy_name(emp.get("mname"))
    # Entered in the wrong boxes: the LAST name in First, the first name in
    # Middle, a middle initial in Last (Simple EJ's new hires, 9/30/26:
    # "Royer · Katie · M" read "Royer M" for Katie Royer; "Benedict ·
    # Danielle · J", "GORDON · KAILEY · I"). A one-letter last name beside a
    # middle name is that pattern: Middle + First — the same name the record
    # gives once it is corrected in RPOWER, so a fix there renames nobody.
    if middle and len(last.replace(".", "")) == 1 and first and len(first) > 1:
        return f"{middle} {first}"
    if first or last:
        return f"{first} {last}".strip()
    name = str(emp.get("name") or "")
    if "," in name:
        last, _, first = name.partition(",")
        return f"{_tidy_name(first)} {_tidy_name(last)}".strip()
    return _tidy_name(name)


# Station logins (Simple EJ's, 9/28/26): RPOWER employee records named for a
# station, not a person - "Day Bar", "Night Bar", "Pm Host", "To Go AM",
# "Party AM". Each is clocked in at the same minute as the real person on
# that job (Day Bar 9:51-3:31 beside Amy Baylis 9:51-3:31 on 9/17) to ring
# the station and pool its tips; RPOWER's payroll pays nothing through it.
# Counted as a person, every one of its hours was a second copy of someone
# else's, costed at the blended rate, and it headed the overtime list
# ("To Am worked 61.8h") as if it were somebody. A record is a station only
# when BOTH hold: every word of its name is a station, daypart or job word
# (this store's own job names included), and none of its punches carries
# pay. A person with no rate on file is still a person. "System Manager"
# (12h on Manager FOH, no pay, 9/28/26) is RPOWER's own login, not a manager.
_STATION_WORDS = frozenset((
    "am pm day night morning evening lunch dinner brunch late overnight "
    "bar host hostess to go togo take out takeout pickup curbside delivery online "
    "party parties event events catering banquet private patio terrace lounge room "
    "upstairs downstairs main front back floor foh boh kitchen expo line prep "
    "server servers busser bussers barback bartender cashier counter register "
    "station shared house tips tip pool training trainee manager mgr test "
    "generic staff team service system admin rpower "
    # A shared cash drawer: Simple EJ's "Office Drawer" (never paid, clocked
    # in as Manager BOH and Host PM) was on the roster as a person - the
    # Bench's only BOH dinner manager (owner, 10/19/26).
    "office drawer cash till").split())


def _name_words(name) -> list:
    return [w for w in re.split(r"[^a-z0-9]+", str(name or "").lower()) if w]


def is_station_name(name, job_names=()) -> bool:
    """Every word of `name` is a station / daypart / job word."""
    words = _name_words(name)
    vocab = set(_STATION_WORDS)
    for j in job_names or ():
        vocab.update(_name_words(j))
    return bool(words) and all(w in vocab for w in words)


def _paid(entry) -> bool:
    return any(float(entry.get(k) or 0) > 0 for k in ("reg_rate", "reg_pay", "ot_pay", "dt_pay"))


def station_logins(entries: list, people: dict) -> dict:
    """{emp_mid: name} of the station logins among these punches: a station
    name (fetch_people's `station_names`) with no pay on any punch."""
    cands = people.get("station_names") or {}
    if not cands:
        return {}
    paid = {str(e.get("emp_mid") or "") for e in entries or [] if _paid(e)}
    return {mid: nm for mid, nm in cands.items() if mid not in paid}


def _name_tag(emp: dict) -> str:
    """What sets a record apart from another of the same name: its middle
    name, else the part after a second comma of `name` ("Avelar, Oscar, J" -
    Simple EJ's two Oscar Avelars, 10/14/26), else ''."""
    mid = _tidy_name(emp.get("mname"))
    if mid:
        return mid
    parts = [p.strip() for p in str(emp.get("name") or "").split(",")]
    return _tidy_name(parts[2]) if len(parts) > 2 else ""


def distinct_employee_names(records) -> dict:
    """{mid: display name} for RPOWER employee records, with two people who
    would read the same name told apart (owner, 10/14/26: two "Oscar
    Avelar"s - different records, pay and jobs - were one person to every
    schedule, overtime and rating check, a 68.6h week). A record with a
    distinguishing middle part reads "Oscar J. Avelar"; the one without
    keeps the plain name; records still alike all carry their payroll id. A
    name only one record has is unchanged, so nobody else is renamed."""
    base, groups = {}, {}
    for e in records or []:
        m = str(e.get("mid") or "").strip()
        name = employee_display_name(e)
        if not (m and name):
            continue
        base[m] = (name, e)
        groups.setdefault(name.lower(), []).append(m)
    out = {m: v[0] for m, v in base.items()}
    for key, mids in groups.items():
        if len(mids) < 2:
            continue
        named = {}
        for m in mids:
            name, e = base[m]
            tag = _name_tag(e)
            if tag:
                first, _, last = name.partition(" ")
                tag = tag + "." if len(tag) == 1 else tag
                name = f"{first} {tag} {last}".strip() if last else f"{name} {tag}"
            named[m] = name
        counts = {}
        for n in named.values():
            counts[n.lower()] = counts.get(n.lower(), 0) + 1
        for m in mids:
            # Still alike after the middle part: every one of them carries its
            # payroll id - the plain name stays nobody's, so a schedule sent to
            # the POS never picks one of two identical records by guess.
            if counts[named[m].lower()] > 1:
                named[m] = f"{named[m]} ({str(base[m][1].get('payroll_id') or m)[-6:]})"
            out[m] = named[m]
    return out


def fetch_people(restaurant_id: int) -> dict:
    """{"jobs": {job_mid: name}, "employees": {emp_mid: "First Last"},
    "station_names": {emp_mid: POS name}} for this store's consolidation
    group — one paged read of each list. `station_names` are records whose
    name is a station's (is_station_name); station_logins() decides from
    their punches."""
    token, base = _ctx(restaurant_id)
    jobs, emps, stations, payroll = {}, {}, {}, {}
    for j in _list(restaurant_id, "job/getbycg"):
        name = _tidy_name(j.get("name"))
        if j.get("mid") and name:
            jobs[str(j["mid"])] = name
    emp_recs = list(_list(restaurant_id, "employee/getbycg"))
    distinct = distinct_employee_names(emp_recs)
    for e in emp_recs:
        name = distinct.get(str(e.get("mid") or "").strip())
        if e.get("mid") and str(e.get("payroll_id") or "").strip():
            payroll[str(e["mid"])] = str(e["payroll_id"]).strip()
        if e.get("mid") and name:
            emps[str(e["mid"])] = name
            # the POS's own full name ("To Go AM"; fname+lname lost the "Go")
            full = _tidy_name(" ".join(str(e.get(k) or "") for k in ("fname", "mname", "lname")).strip()
                              or e.get("name"))
            if is_station_name(full, jobs.values()):
                stations[str(e["mid"])] = full
    # `payroll`: each employee's payroll id, which a punch read back from the
    # archive carries no copy of (build_shifts_csv, scale audit A3-06).
    return {"jobs": jobs, "employees": emps, "station_names": stations, "payroll": payroll}


def fetch_customers(restaurant_id: int) -> dict:
    """{customer mid: {name, phone, email, company}} - RPOWER's customer
    file for this store's consolidation group (customer access granted for
    Simple EJ's 10/8/26). A record with a company is a house account or a
    delivery service, never a guest (guest_hub.match_pos_visits)."""
    token, base = _ctx(restaurant_id)
    out = {}
    for c in _paged(token, "customer/getbycg", {"cg": base["cg"]}):
        mid = str(c.get("mid") or "").strip()
        if mid:
            out[mid] = {"name": _tidy_name(c.get("name")) or None, "phone": str(c.get("phone") or "").strip() or None,
                        "email": str(c.get("email") or "").strip() or None,
                        "company": str(c.get("company") or "").strip() or None}
    return out


# One customer record per id, kept in this process only (never written
# anywhere), shared by every store of one group on one credential.
CUSTOMER_CACHE_SECONDS = 6 * 3600
_customer_cache = _LRU(CACHE_MAX_KEYED * 4)


def fetch_customers_by_mid(restaurant_id: int, mids) -> dict:
    """{customer mid: {name, phone, email, company}} for just these ids —
    customer/getbymid (collection #6), one request per id not already read
    for this group in the last CUSTOMER_CACHE_SECONDS (Audit #23 A1-06 /
    A7-07). The guest match used to page the group's WHOLE customer file
    (names, phones, emails, addresses, card_acct) every night to resolve the
    handful of ids on last night's checks. An id RPOWER does not answer is
    left out."""
    token, base = _ctx(restaurant_id)
    group = _scope(restaurant_id, token, base, False)
    out = {}
    for mid in sorted({str(m).strip() for m in mids or () if str(m or "").strip()}):
        hit = _customer_cache.get((group, mid))
        if hit and time.monotonic() - hit[0] < CUSTOMER_CACHE_SECONDS:
            if hit[1]:
                out[mid] = hit[1]
            continue
        body = _request(token, "customer/getbymid", {"mid": mid})
        recs = [c for c in (body if isinstance(body, list) else [body]) if isinstance(c, dict)]
        c = next((c for c in recs if str(c.get("mid") or "").strip() == mid), recs[0] if len(recs) == 1 else None)
        rec = None
        if c:
            rec = {"name": _tidy_name(c.get("name")) or None, "phone": str(c.get("phone") or "").strip() or None,
                   "email": str(c.get("email") or "").strip() or None,
                   "company": str(c.get("company") or "").strip() or None}
        _customer_cache[(group, mid)] = (time.monotonic(), rec)
        if rec:
            out[mid] = rec
    return out


# ── during service (the live view) ───────────────────────────────────────
#
# RPOWER's above-store database is fed while the store trades. Read-only
# check on Simple EJ's, 9/29/26 10:04pm: the whole day was there (838 lines,
# 120 tickets, 32 punches); the first sale (11:21am) was posted by 11:25am and
# the last (9:41pm) by 10:02pm (`time_stamp` is UTC and is rewritten when a
# ticket closes, so these are the latest write, an upper bound on arrival).
# The vendor's "a month at most, a week preferred" is how much one request
# may span, not how late the data is. So pos.fetch_sales_today and
# pos.fetch_clock_ins_today, which look these up by name, now reach RPOWER.

PEOPLE_CACHE_SECONDS = 600
_people_cache = _LRU(CACHE_MAX_RESTAURANTS)


def employee_names(restaurant_id: int) -> dict:
    """{employee mid: "First Last"} — who approved a comp or edited a punch,
    for the nightly report (dsr.block_service). Cached with the people list."""
    return dict((_people_cached(restaurant_id) or {}).get("employees") or {})


_job_cat_cache = _LRU(CACHE_MAX_RESTAURANTS)


def job_categories(restaurant_id: int) -> dict:
    """{job name lower-cased: payroll category name} — RPOWER's own Front of
    House / Back of House on each job (job.pyrlcat_mid → payrollcategory),
    for labor by department. A job with no category is left out. Cached for
    PEOPLE_CACHE_SECONDS like the people list."""
    hit = _job_cat_cache.get(restaurant_id)
    if hit and not _fresh() and time.monotonic() - hit[0] < PEOPLE_CACHE_SECONDS:
        return hit[1]
    token, base = _ctx(restaurant_id)
    cats = {str(c.get("mid")): _tidy_name(c.get("name"))
            for c in _list(restaurant_id, "payrollcategory/getbycg")
            if c.get("mid")}
    out = {}
    for j in _list(restaurant_id, "job/getbycg"):
        name, cat = _tidy_name(j.get("name")), cats.get(str(j.get("pyrlcat_mid") or ""))
        if name and cat:
            out[name.lower()] = cat
    _job_cat_cache[restaurant_id] = (time.monotonic(), out)
    return out


def _people_cached(restaurant_id: int) -> dict:
    """fetch_people, read once per PEOPLE_CACHE_SECONDS: the coverage check
    runs every 20 minutes through service and the lists rarely change."""
    hit = _people_cache.get(restaurant_id)
    if hit and not _fresh() and time.monotonic() - hit[0] < PEOPLE_CACHE_SECONDS:
        return hit[1]
    out = fetch_people(restaurant_id)
    _people_cache[restaurant_id] = (time.monotonic(), out)
    return out


@_one_store_scope
def fetch_sales_today(restaurant_id: int, business_date) -> float:
    """Net sales posted so far for this business date — the same revenue
    rules as fetch_business_days, so the hourly figure and the night's total
    agree. Raises when nothing has posted yet: "no sales yet" and "RPOWER
    has not answered" are not the same as $0 (pos.fetch_sales_today)."""
    day = _d(business_date)
    # An open day by definition: straight from RPOWER, no archive question.
    value = (_business_days(restaurant_id, business_date, business_date, use_archive=False) or {}).get(day)
    if value is None:
        raise RPowerNoSalesYet(f"RPOWER has no sales posted for {day} yet")
    return float(value)


@_one_store_scope
def fetch_clock_ins_today(restaurant_id: int, business_date) -> list:
    """Everyone who has clocked in this business day: [{"employee", "role",
    "clocked_in_at", "external_id"}] — the shape intraday.coverage_gaps
    reads. The day is the punches punch_business_date files on it (4am local
    to 4am, the schedule's own boundary; RPOWER's punch times are local wall
    clock), so a 12:30am clock-in during a late close is tonight's and a
    4:30am baker's is this morning's. Station logins (a "Bar 1" record) are
    left out, as in the labor sync. `external_id` is the employee's RPOWER
    id, which people.person_aliases keys the person on.

    A punch whose employee the kept lists do not name reads them afresh
    once (_people_for); one still unnamed is counted as "Unknown employee
    (POS …1234)" with `unknown` True — never a reason to refuse the whole
    read (blind re-audit 10/20/26 R3-06: one new hire's punch blanked live
    labor and the coverage check for up to six hours, or, among 21 or more
    punches, vanished, and they were told "hasn't clocked in")."""
    day = business_date if hasattr(business_date, "toordinal") else date.fromisoformat(_d(business_date))
    start, end = _punch_window(day)
    entries = [e for e in fetch_time_entries(restaurant_id, day, day + timedelta(days=1)) or []
               if (lambda t: t is not None and start <= t < end)(_punch_dt(e.get("in_dttm")))]
    if not entries:
        return []
    people = _people_for(restaurant_id, entries)
    stations = station_logins(entries, people)
    jobs, emps = people.get("jobs") or {}, people.get("employees") or {}
    rows = []
    for e in entries:
        mid = str(e.get("emp_mid") or "").strip()
        if not mid or mid in stations:
            continue
        known = mid in emps
        rows.append({"employee": emps[mid] if known else f"Unknown employee (POS …{mid[-4:]})",
                     **({} if known else {"unknown": True}),
                     "role": jobs.get(str(e.get("job_mid") or "")) or (e.get("job_id") or "").strip() or "Staff",
                     "clocked_in_at": _punch_dt(e.get("in_dttm")).isoformat(),
                     # The punch's end once they clocked out, and its pay
                     # rate — live labor to date reads both (labor_live).
                     "clocked_out_at": (_punch_dt(e.get("out_dttm")).isoformat()
                                        if _punch_dt(e.get("out_dttm")) else None),
                     "hourly_wage": _num(e.get("reg_rate")) or None,
                     "external_id": mid})
    return rows


def _require_resolved(entries: list, people: dict) -> None:
    """Refuse a pull whose punches mostly can't be matched to a person: saved
    under payroll codes they read as strangers and never merge with the
    names every other screen uses."""
    closed = [e for e in entries or [] if _is_closed_punch(e)]
    if not closed:
        return
    names = people.get("employees") or {}
    missing = sum(1 for e in closed if str(e.get("emp_mid") or "") not in names)
    if missing and missing / float(len(closed)) > UNRESOLVED_MAX_SHARE:
        raise RPowerUnresolved(
            f"{missing} of {len(closed)} RPOWER punches could not be matched to an employee; "
            "nothing was saved from this read")


def normalise_entries(time_entries: list, sales_by_date: dict, people: dict = None) -> list:
    """Timeclock punches into labor.py's CSV rows.

    Two things RPOWER gives us that Toast does not, and both are kept rather
    than recomputed:

    * Hours are already split into reg/ot/dt. labor.py derives overtime
      itself from a weekly total, which is a reasonable approximation when
      that is all you have — but RPOWER's figures come from the payroll
      engine that actually pays these people, and a derived number that
      disagrees with the paycheck is worse than no number. `actual_hours` is
      therefore the sum of all three, and the split travels in `notes` so it
      is visible rather than discarded.
    * The job is the store's own job, so `role` is what the restaurant calls
      it ("Server AM") rather than something inferred from a name.

    `people` is fetch_people(): the job and employee names. Without it (or
    for a reference the lists don't carry) the row falls back to RPOWER's
    codes — build_shifts_csv never saves that, it requires the names first.

    `sales` is attached per day, not per shift — the same convention
    toast.normalise_entries uses, so labor.py's day aggregation is unchanged.
    """
    people = people or {}
    job_names = people.get("jobs") or {}
    emp_names = people.get("employees") or {}
    # Not labor: a station login's punches double someone else's (above).
    stations = station_logins(time_entries, people)
    rows = []
    for e in time_entries or []:
        if str(e.get("emp_mid") or "") in stations:
            continue
        start = _punch_dt(e.get("in_dttm"))
        if not start:
            continue
        end = _punch_dt(e.get("out_dttm"))
        # A punch with no out time is a shift still running. It has no
        # duration to report and including it as a zero-hour shift would drag
        # every average down, so it is skipped rather than guessed at.
        if not end or end <= start:
            continue
        # The business date the punch was worked for (R9-09): a 12:10am job
        # change is the night before's, as the archive and live paths file it.
        day = punch_business_date(start).isoformat()
        reg = float(e.get("reg_hours") or 0)
        ot = float(e.get("ot_hours") or 0)
        dt_h = float(e.get("dt_hours") or 0)
        actual = round(reg + ot + dt_h, 3)
        if actual <= 0:
            # Fall back to the clock only when RPOWER reported no hours at
            # all — a punch pair with no payroll hours behind it is usually a
            # correction, and the wall-clock duration is the honest read.
            actual = round((end - start).total_seconds() / 3600.0, 3)
        notes = []
        if ot:
            notes.append(f"OT {ot:g}h")
        if dt_h:
            notes.append(f"DT {dt_h:g}h")
        if e.get("break_minutes"):
            notes.append(f"break {int(e['break_minutes'])}m")
        mid = str(e.get("emp_mid") or "").strip()
        named = mid in emp_names
        rows.append({
            "date": day,
            "day": _DOW_NAMES[date.fromisoformat(day).weekday()],
            "employee": (emp_names.get(mid)
                         or (e.get("payroll_id") or e.get("emp_mid") or "").strip()),
            # The employee's own RPOWER ids (memory audit 9/29/26, identity):
            # the record id every punch carries, and the payroll id the
            # schedule write-back needs. A name can change; these do not —
            # people.resolve_rows keys the person on them. `employee_named`
            # is "0" when the name above is the payroll-code fallback, so a
            # code never renames a person.
            "employee_ext_id": mid,
            "employee_payroll_id": str(e.get("payroll_id") or "").strip(),
            "employee_named": "1" if named else "0",
            "role": (job_names.get(str(e.get("job_mid") or ""))
                     or (e.get("job_id") or "").strip()),
            "shift_start": start.strftime("%H:%M"),
            "shift_end": end.strftime("%H:%M"),
            # RPOWER's timeclock is what was WORKED. It carries no schedule,
            # so scheduled_hours is left equal to actual rather than invented
            # — a fabricated variance is worse than none — and
            # `schedule_known` says so: no attendance reader may read a
            # no-show out of it (memory audit 9/29/26, attendance).
            "scheduled_hours": actual,
            "schedule_known": "0",
            "actual_hours": actual,
            "sales": sales_by_date.get(day, ""),
            "notes": ", ".join(notes),
            # What RPOWER's payroll pays this person an hour; blank when it
            # has no rate on file, and labor.py then uses the owner's rate
            # for the role or the blended rate (labor._shift_rate).
            "pay_rate": round(float(e.get("reg_rate") or 0), 2) if float(e.get("reg_rate") or 0) > 0 else "",
        })
    rows.sort(key=lambda r: (r["date"], r["shift_start"]))
    return rows


# The nightly labor read (scale audit 10/19/26 A3-06): the last this-many
# days are read from RPOWER's timeclock every night — where a manager still
# edits punches — and a day before them comes from the archive's punches
# when they were read from RPOWER this long after the day (pos_archive.
# settled_punch_dates); any other day is read live as before. The live read
# refreshes the archive's punches of the archived days it covers, so a day
# leaves the live window with its punches re-read. 60 or more reads all 60
# days live, as before 10/19/26.
LABOR_LIVE_DAYS = int(os.getenv("RPOWER_LABOR_LIVE_DAYS", "14"))


def _labor_entries(restaurant_id: int, start, end) -> tuple:
    """(entries, live entries, archived entries) — timeclock rows in RPOWER's
    shape for calendar days start..end: a day whose punches the archive holds
    settled (that day and the business date before it, whose after-midnight
    punches fall on it) from pos_punches, every other day from RPOWER in
    week-sized runs. A punch is one entry, whichever side it came from."""
    import pos_archive
    live_from = max(start, end - timedelta(days=LABOR_LIVE_DAYS))
    covered = set()
    if live_from > start:
        try:
            settled = pos_archive.settled_punch_dates(restaurant_id, "rpower", (start - timedelta(days=1)).isoformat(),
                                                      live_from.isoformat(), LABOR_LIVE_DAYS)
        except Exception as e:                       # the archive is an optimisation, never a dependency
            log.warning("[rpower] archived punches not consulted for %s: %s", restaurant_id, e)
            settled = set()
        d = start
        while d < live_from:
            if d.isoformat() in settled and (d - timedelta(days=1)).isoformat() in settled:
                covered.add(d)
            d += timedelta(days=1)
    archived = []
    if covered:
        bdates = sorted({x.isoformat() for d in covered for x in (d, d - timedelta(days=1))})
        held = set()
        for p in pos_archive.archived_punches(restaurant_id, "rpower", bdates):
            start_at = _punch_dt(p.get("clock_in"))
            if start_at is None or start_at.date() not in covered:
                continue
            # One punch is one entry: a punch stored under two business dates
            # (a re-archive across the 10/20/26 boundary change; Postgres's
            # partitioned key carries the date) is counted once.
            if str(p["punch_id"]) in held:
                continue
            held.add(str(p["punch_id"]))
            archived.append({"rid": p["punch_id"], "emp_mid": p.get("employee_id"), "in_dttm": p.get("clock_in"),
                             "out_dttm": p.get("clock_out"), "job_mid": p.get("job_id"), "job_id": p.get("role") or "",
                             "reg_hours": p.get("reg_hours"), "ot_hours": p.get("ot_hours"),
                             "dt_hours": p.get("dt_hours"), "reg_rate": p.get("reg_rate"),
                             "break_minutes": p.get("break_minutes"), "payroll_id": ""})
    live, runs, run, d = [], [], [], start
    while d <= end + timedelta(days=1):
        if d <= end and d not in covered:
            run.append(d)
        elif run:
            live.extend(fetch_time_entries(restaurant_id, run[0], run[-1]) or [])
            runs.append((run[0], run[-1]))
            run = []
        d += timedelta(days=1)
    seen = {str(e.get("rid")) for e in live if e.get("rid")}
    archived = [e for e in archived if str(e["rid"]) not in seen]
    return live + archived, live, archived, runs


# A store's people lists are read afresh for an unknown punch at most this
# often (the live clock-ins run every 15 minutes during service: an id the
# POS never lists must not re-read them every time).
PEOPLE_REFRESH_SECONDS = 600
_people_refreshed = _LRU(CACHE_MAX_RESTAURANTS)


def _people_for(restaurant_id: int, entries: list) -> dict:
    """The kept people lists, read afresh once (per PEOPLE_REFRESH_SECONDS)
    when a punch — on the clock or finished — names an employee or job they
    do not hold: a hire since they were kept. A new hire's first shift used
    to read the six-hour-old list and blank live labor (R3-06)."""
    people = _people_cached(restaurant_id)
    emps, jobs = people.get("employees") or {}, people.get("jobs") or {}
    seen = [e for e in entries if _punch_dt(e.get("in_dttm")) is not None
            and not str(e.get("emp_mid") or "") in (people.get("station_names") or {})]
    new_emps = {str(e.get("emp_mid") or "") for e in seen if str(e.get("emp_mid") or "") not in emps}
    new_jobs = {str(_id(e.get("job_mid"))) for e in seen if _id(e.get("job_mid")) and str(_id(e.get("job_mid"))) not in jobs}
    if new_emps or new_jobs:
        hit = _people_refreshed.get(restaurant_id)
        if hit is None or time.monotonic() - hit >= PEOPLE_REFRESH_SECONDS:
            _people_refreshed[restaurant_id] = time.monotonic()
            # One new hire on a known job: read that one record (A1-20), not
            # the group's whole employee list. Anything more, or a failed
            # single read, refreshes the lists as before.
            single = len(new_emps) == 1 and not new_jobs and "" not in new_emps
            added = False
            if single and not _reader_only():
                try:
                    added = _list_add_one(restaurant_id, "employee/getbycg", "employee/getbymid", next(iter(new_emps)))
                except Exception as e:      # the single read is an optimisation: any failure reads the list
                    log.info("rpower employee/getbymid for rid %s failed, reading the list: %s", restaurant_id, e)
            if added:
                _people_cache.pop(restaurant_id, None)
                people = _people_cached(restaurant_id)
            else:
                with fresh_lists():
                    people = _people_cached(restaurant_id)
    return people


def _refresh_archived_punches(restaurant_id: int, live: list, runs: list, people: dict) -> None:
    """Store this live read's punches over the archive's for each archived
    business date it fully covered (its own calendar day and the next one,
    where its after-midnight punches fall), stamping when they were read.
    A covered date the read found no punch for is passed as empty: a day the
    archive also holds empty (a store closed on Mondays) is stamped read and
    so settles — it was re-read live every night for 60 days, with the day
    after it (blind re-audit 10/20/26 R3-14) — while an empty answer never
    wipes a stored day that has punches (pos_archive.refresh_punches). Never
    raises."""
    try:
        import pos_archive
        by_day = {}
        for e in live:
            t = _punch_dt(e.get("in_dttm"))
            if t is None:
                continue
            by_day.setdefault(punch_business_date(t), []).append(e)
        span = {}
        for a, z in runs:
            b = a
            while b + timedelta(days=1) <= z:
                span[b.isoformat()] = _punch_records(by_day.get(b) or [], b.isoformat(), people)
                b += timedelta(days=1)
        if span:
            pos_archive.refresh_punches(restaurant_id, "rpower", span)
    except Exception as e:
        log.warning("[rpower] archived punches not refreshed for %s: %s", restaurant_id, e)


@_one_store_scope
def build_shifts_csv(restaurant_id: int, days: int = 60) -> Optional[str]:
    """The last `days` of labor, in the CSV shape labor.load_shifts expects."""
    import csv
    import io as _io

    # The restaurant's own date (POS-18): on a UTC host the server's date is
    # tomorrow after 7pm Central.
    from time_utils import restaurant_now_by_id
    end = restaurant_now_by_id(restaurant_id, naive=True).date()
    start = end - timedelta(days=days)
    entries, live, archived, runs = _labor_entries(restaurant_id, start, end)
    people = _people_for(restaurant_id, entries) if entries else {}
    for e in archived:                      # the archive keeps no payroll id: the employee list has it
        e["payroll_id"] = (people.get("payroll") or {}).get(str(e.get("emp_mid") or ""), "")
    _require_resolved(entries, people)
    _refresh_archived_punches(restaurant_id, live, runs, people)
    sales = fetch_business_days(restaurant_id, start, end)
    # A punch before 4am on the window's first calendar day is the night
    # before's (punch_business_date, R9-09) — a day outside this window that
    # this read does not hold whole: left out, never stored as that day
    # (shift_facts replaces the window the rows' dates span).
    before = (start - timedelta(days=1)).isoformat()
    rows = [r for r in normalise_entries(entries, sales, people) if r["date"] != before]
    left_out = station_logins(entries, people)
    if left_out:
        log.info("[rpower] rid=%s station logins left out of labor: %s",
                 restaurant_id, ", ".join(sorted(set(left_out.values()))))
    if not rows:
        return ""
    fieldnames = ["date", "day", "employee", "role", "shift_start", "shift_end",
                  "scheduled_hours", "actual_hours", "sales", "notes", "pay_rate",
                  "employee_ext_id", "employee_payroll_id", "employee_named", "schedule_known"]
    buf = _io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def sync_to_db(restaurant_id: int) -> dict:
    """Nightly sync — the same contract toast.sync_to_db has, so pos.py can
    call either without knowing which."""
    from models import save_client_data, update_restaurant

    try:
        csv_str = build_shifts_csv(restaurant_id, days=60)
        if not csv_str:
            update_restaurant(restaurant_id, {
                "rpower_sync_error": "No timeclock data returned for the last 60 days"})
            return {"ok": False, "error": "No shift data returned from RPOWER"}

        rows = csv_str.count("\n") - 1
        import pos
        pos.save_synced_shifts(restaurant_id, csv_str, "rpower")
        update_restaurant(restaurant_id, {
            "rpower_last_synced": datetime.utcnow().isoformat(timespec="seconds"),
            "rpower_sync_error": None})

        # The per-day labor archive (labor_daily_history — what year-over-
        # year and the trend charts read) and the payroll-week labor periods
        # were written by pos.save_synced_shifts above, with the pull's
        # provenance. This used to save the whole file again without it and
        # then append a rolling-window "period": Simple EJ's 9/1 period was
        # saved three times on its first day (memory audit 9/29/26,
        # labor_periods).
        try:
            # Same as the Toast sync: keep menu_items current so the dish a
            # post names, the count sheet and recipe drafts have RPOWER's
            # items to match against — not only when Food Cost is open.
            try:
                import inventory_ledger
                inventory_ledger.discover_menu_items(restaurant_id, days=2)
                # Dish sales for days the nightly job never recorded (it
                # starts from the night it first runs): read from RPOWER,
                # units only, then nothing until a day goes missing again.
                inventory_ledger.backfill_item_sales(restaurant_id, days=60)
                # The Daily Sales Report's grids for the nights before it
                # started are their own job now (strategy_jobs.
                # run_dsr_backfill, `dsr_backfill`, after this sync): a
                # failure there used to be swallowed here with no job_runs
                # row (bar audit 10/14/26 RPW-13 / R11).
            except Exception as _menu_e:
                log.warning("[rpower sync] menu item discovery error for %s: %s", restaurant_id, _menu_e)
        except Exception as e:
            log.warning("[rpower] labor archive failed for %s: %s", restaurant_id, e)

        return {"ok": True, "rows": rows}
    except RPowerAuthError as e:
        update_restaurant(restaurant_id, {"rpower_sync_error": str(e)})
        return {"ok": False, "error": str(e), "auth": True}
    except Exception as e:
        update_restaurant(restaurant_id, {"rpower_sync_error": str(e)[:300]})
        return {"ok": False, "error": str(e)}


def demo_live_store_refusal(restaurant, token):
    """The sentence refusing a DEMO restaurant a live RPOWER store without
    the admin override (A7-15), else None. A demo-store: token is simulated
    and always allowed; a real restaurant is never refused here."""
    if restaurant is None or not int(getattr(restaurant, "is_demo", 0) or 0):
        return None
    if str(token or "").startswith("demo-store:") or int(getattr(restaurant, "pos_live_mirror", 0) or 0):
        return None
    return ("This is a demo restaurant, and that is a live RPOWER store. A demo may mirror a live store only "
            "with the live-mirror override (RPOWER → Live store on a demo), which is audited.")


# What an owner's screen says about a failed sync (Audit #23 F7, A7-14): one
# fixed sentence per class of failure. The raw text — the vendor's body, a
# URL with the cg and store id — is the admin console's only
# (rpower_sync_error, /admin/rpower/status).
OWNER_ERROR_SENTENCES = {
    "auth": "RPOWER refused Cavnar AI's access to this store. We've been alerted and are on it.",
    "setup": "RPOWER isn't fully connected yet. Cavnar AI is finishing the setup.",
    "unavailable": "RPOWER wasn't answering when we last synced. We'll try again automatically.",
    "no_data": "RPOWER sent no timeclock data for the last 60 days.",
    "unresolved": "Some timeclock punches name people RPOWER hasn't listed yet. We'll try again tonight.",
    "other": "The last RPOWER sync didn't finish. We've been alerted and will try again.",
}


def owner_error_class(raw) -> Optional[str]:
    """The class of a stored sync error (see OWNER_ERROR_SENTENCES), or None
    for no error."""
    t = str(raw or "").strip().lower()
    if not t:
        return None
    if "rejected the token" in t or "(401)" in t or "(403)" in t or "authorized for" in t:
        return "auth"
    if "not fully connected" in t or "no rpower token" in t or "bootstrap" in t or "isn't one this token" in t:
        return "setup"
    if "no timeclock data" in t:
        return "no_data"
    if "punches name" in t or "unresolved" in t or "not listed" in t:
        return "unresolved"
    if any(k in t for k in ("paused", "http 5", "http 429", "failed after", "request failed", "non-json",
                            "timed out", "timeout", "unavailable", "try later", "budget")):
        return "unavailable"
    return "other"


def owner_error_sentence(raw) -> Optional[str]:
    cls = owner_error_class(raw)
    return OWNER_ERROR_SENTENCES[cls] if cls else None


def get_connection_status(restaurant_id: int) -> dict:
    """What the account screen shows. Distinguishes the three real states:
    nothing on file, a token that has never been verified, and connected.
    `error` is the raw text (admins only); `owner_error` the fixed sentence
    an owner's screen shows (A7-14)."""
    try:
        c = _creds(restaurant_id)
    except RPowerError as e:
        return {"connected": False, "state": "error", "error": str(e), "owner_error": owner_error_sentence(e)}
    if not c["token"]:
        return {"connected": False, "state": "not_configured", "credential_id": c.get("credential_id")}
    if not (c["cg"] and c["store_mid"]):
        return {"connected": False, "state": "needs_bootstrap", "credential_id": c.get("credential_id"),
                "message": "Token saved — choose which RPOWER store this is."}
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    raw = getattr(r, "rpower_sync_error", None)
    return {
        "connected": True, "state": "connected",
        "store_name": c["name"], "cg": c["cg"], "store_mid": c["store_mid"],
        "credential_id": c.get("credential_id"),
        "last_synced": getattr(r, "rpower_last_synced", None),
        "error": raw, "owner_error": owner_error_sentence(raw),
    }


# ── write path: pushing a Cavnar schedule back into RPOWER ─────────────────

def fetch_job_codes(restaurant_id: int) -> dict:
    """{job name lower-cased: RPOWER job code (ext_id)} — the reverse of the
    names the sync stores as `role`, for anything that writes back."""
    token, base = _ctx(restaurant_id)
    return {_tidy_name(j.get("name")).lower(): str(j.get("ext_id") or "").strip()
            for j in _list(restaurant_id, "job/getbycg")
            if _tidy_name(j.get("name")) and str(j.get("ext_id") or "").strip()}


def pos_employees(restaurant_id: int) -> list:
    """Every employee record in the store's group: [{"mid", "name",
    "payroll_id", "current"}]. `current` is no term date (RPOWER writes
    2000-01-01 for "none")."""
    token, base = _ctx(restaurant_id)
    out = []
    recs = list(_list(restaurant_id, "employee/getbycg"))
    distinct = distinct_employee_names(recs)
    for e in recs:
        term = str(e.get("term_date") or "")
        out.append({"mid": _id(e.get("mid")), "name": distinct.get(str(e.get("mid") or "").strip()) or employee_display_name(e),
                    "payroll_id": str(e.get("payroll_id") or "").strip(),
                    "current": not term or term[:4] <= "2000"})
    return out


def employee_phone_keys(restaurant_id: int, name, keyfn=None):
    """The phone digests (phone_key) on the POS employee record(s) whose
    name is `name` — read from the KEPT list only, never RPOWER on this
    thread — for the staff sign-up check (Audit #23 A7-10). None when no
    kept record has that name (or the store is not connected); an empty set
    when the record carries no phone. `keyfn` normalises a name for the
    comparison (auth._name_key)."""
    import pos_io
    try:
        token, base = _ctx(restaurant_id)
    except RPowerError:
        return None
    kept = pos_io.catalog_get("rpower", _scope(restaurant_id, token, base, False), "employee/getbycg")
    if not kept:
        return None
    recs = kept[1] or []
    names = distinct_employee_names(recs)
    norm = keyfn or (lambda s: " ".join(str(s or "").lower().split()))
    want = norm(name)
    hits = [r for r in recs if want and (norm(names.get(str(r.get("mid") or "").strip()) or "") == want
                                          or norm(employee_display_name(r)) == want)]
    if not hits:
        return None
    return {k for r in hits for k in (r.get("phone_keys") or [])}


def _payroll_by_name(restaurant_id: int) -> dict:
    """{name_key: payroll id} for current employees whose name is theirs
    alone. The fallback when Cavnar holds no POS id for a person (Simple
    EJ's has none linked, 9/29/26) — the same exact-name rule the role sync
    uses; a name two current people share matches neither."""
    import staff_settings as _ss
    seen, out = set(), {}
    for e in pos_employees(restaurant_id):
        if not (e["current"] and e["payroll_id"] and e["name"]):
            continue
        k = _ss.name_key(e["name"])
        if k in seen:
            out.pop(k, None)
            continue
        seen.add(k)
        out[k] = e["payroll_id"]
    return out


def _schedule_payload(restaurant_id: int, base: dict, shifts: list, job_codes: dict = None,
                      payroll_by_name: dict = None) -> tuple:
    """(payload, {payroll id: [rows]}, skipped) — the push body, built one way
    for the push and its preview. A person's payroll id is the shift's own,
    then the one Cavnar keeps (people.person_aliases, source rpower_payroll),
    then RPOWER's own list by exact name (read only when needed)."""
    by_employee, skipped = {}, []
    try:
        import people as _people
        import staff_settings as _ss
        _payroll = _people.external_ids(restaurant_id, "rpower_payroll")
    except Exception:
        _payroll, _ss = {}, None
    for s in shifts or []:
        pid = str(s.get("employee_payroll_id") or s.get("payroll_id") or "").strip()
        if not pid and _ss is not None and s.get("employee"):
            key = _ss.name_key(s.get("employee"))
            pid = str(_payroll.get(key) or "").strip()
            if not pid:
                if payroll_by_name is None:
                    try:
                        payroll_by_name = _payroll_by_name(restaurant_id)
                    except RPowerError:
                        payroll_by_name = {}
                pid = str(payroll_by_name.get(key) or "").strip()
        job = (s.get("job") or s.get("role") or "").strip()
        # RPOWER: "the job code is the job name" (laborschedule/push). A
        # caller may still pass fetch_job_codes() to send ext_ids instead.
        job = (job_codes or {}).get(job.lower(), job)
        start, end = s.get("start"), s.get("end")
        # An employee with no payroll id cannot be matched to anyone at the
        # store. Pushing them under a blank id would either fail the whole
        # batch or silently attach hours to nobody, so they are reported back
        # instead of dropped.
        if not (pid and job and start and end):
            skipped.append({"employee": s.get("employee") or pid or "(unnamed)",
                            "why": "no payroll id at the POS" if not pid else "missing job or times"})
            continue
        by_employee.setdefault(pid, []).append({
            "inTime": str(start)[:19], "jobCode": job, "outTime": str(end)[:19]})
    payload = {"storeMid": base["storemid"],
               "schedules": [{"payrollid": pid, "schedules": rows}
                             for pid, rows in by_employee.items()]}
    return payload, by_employee, skipped


MAX_SHIFT_HOURS = 16


def preview_labor_schedule(restaurant_id: int, shifts: list, job_codes: dict = None) -> dict:
    """What push_labor_schedule WOULD send, checked against the store's own
    records — and never sent. Reads only (employees, jobs, who holds which
    job). `problems` lists anything RPOWER would reject or misfile: a payroll
    id no current employee has, a job the store does not have or the person
    does not hold, a time not in RPOWER's format, a shift that ends before it
    starts or runs past MAX_SHIFT_HOURS, two shifts of one person that overlap.

    On a web request the lists are the kept copies only (blind re-audit
    10/20/26 R3-07: the preview read them live, holding one of the four
    request threads on RPOWER); a list never kept yet is RPowerArchiveMiss,
    and the push itself (a queue row) reads what it needs."""
    return _preview_labor_schedule(restaurant_id, shifts, job_codes)


def _preview_labor_schedule(restaurant_id, shifts, job_codes=None):
    import re
    token, base = _ctx(restaurant_id)
    emps = pos_employees(restaurant_id)
    payload, by_employee, skipped = _schedule_payload(restaurant_id, base, shifts, job_codes)
    by_payroll = {e["payroll_id"]: e for e in emps if e["payroll_id"] and e["current"]}
    names = {n.lower() for n in _people_cached(restaurant_id).get("jobs", {}).values()}
    codes = set((job_codes or {}).values())
    held = {}
    for j in fetch_employee_jobs(restaurant_id):
        held.setdefault(j["external_id"], set()).add((j["role"] or "").lower())
    code_to_name = {v: k for k, v in (job_codes or {}).items()}
    fmt = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
    problems = []
    for pid, rows in by_employee.items():
        emp = by_payroll.get(pid)
        who = (emp or {}).get("name") or pid
        if not emp:
            problems.append({"payrollid": pid, "problem": "no current employee has this payroll id"})
        spans = []
        for r in rows:
            job = r["jobCode"]
            job_name = code_to_name.get(job, job).lower()
            if job.lower() not in names and job not in codes:
                problems.append({"employee": who, "shift": r, "problem": f"the store has no job \"{job}\""})
            elif emp and held.get(emp["mid"]) is not None and job_name not in held[emp["mid"]]:
                problems.append({"employee": who, "shift": r,
                                 "problem": f"{who} does not hold \"{job}\" at the POS"})
            if not (fmt.match(r["inTime"]) and fmt.match(r["outTime"])):
                problems.append({"employee": who, "shift": r, "problem": "time not in YYYY-MM-DDTHH:mm:ss"})
                continue
            a, b = datetime.fromisoformat(r["inTime"]), datetime.fromisoformat(r["outTime"])
            if b <= a or (b - a).total_seconds() > MAX_SHIFT_HOURS * 3600:
                problems.append({"employee": who, "shift": r,
                                 "problem": "ends before it starts" if b <= a else f"longer than {MAX_SHIFT_HOURS} hours"})
            spans.append((a, b, r))
        spans.sort(key=lambda x: x[0])
        for (a1, b1, r1), (a2, b2, r2) in zip(spans, spans[1:]):
            if a2 < b1:
                problems.append({"employee": who, "shift": r2, "problem": f"overlaps {r1['inTime']}–{r1['outTime']}"})
    return {"ok": bool(by_employee) and not problems, "sent": False, "would_send": payload,
            "employees": len(by_employee), "shifts": sum(len(v) for v in by_employee.values()),
            "skipped": skipped, "problems": problems}


def push_labor_schedule(restaurant_id: int, shifts: list, job_codes: dict = None) -> dict:
    """Send a generated schedule to the store's POS.

    The only write in the Core API, and it is OFF by default: RPOWER states
    "Labor schedule push is not enabled by default. If you would like the
    ability to push labor schedules to stores, email a request." So a 401/403
    here means the feature is not enabled for this token, which is a
    different problem from a bad token and is reported as such.

    Enabled for our token 9/29/26 (Justin, RPOWER). Nothing calls it on its
    own yet: preview_labor_schedule builds the same body and checks it
    against the store's records without sending, and the first live push
    waits on Will.

    A second push for the same week: Justin (10/6/26, unconfirmed) believes
    "the newest record applies once it hits the store". The body names only
    the people who have shifts, so whether a person LEFT OUT of a corrected
    push keeps their earlier shifts is still open — settle it with RPOWER
    before a corrected week is pushed live.

    `shifts` is Cavnar's own shape — [{employee_payroll_id or employee, job
    or role, start, end}] (labor.timed_shifts_from_csv) — grouped here into
    RPOWER's per-employee structure. Times are local wall
    clock, matching what RPOWER documents and what the timeclock returns.
    """
    import demo_store
    if demo_store.is_simulated(restaurant_id):
        return {"ok": False, "error": "This is a demo store: a schedule is never pushed to a POS from it."}
    token, base = _ctx(restaurant_id)
    payload, by_employee, skipped = _schedule_payload(restaurant_id, base, shifts, job_codes)
    if not by_employee:
        return {"ok": False, "error": "No shifts had a payroll id and job to push.",
                "skipped": skipped}

    import requests
    import pos_io
    # The one write passes the breaker and is counted like every read (A3-10 / A3-19).
    gate, wait = pos_io.breaker_allow("rpower")
    if gate == "open":
        pos_io.record("rpower", restaurant_id, "laborschedule/push", "skipped")
        return {"ok": False, "skipped": skipped, "retry_after": wait,
                "error": f"RPOWER has been failing; the push waits {int(wait)}s for it to answer again."}
    t0 = time.monotonic()
    try:
        resp = requests.post(
            f"{BASE_URL}/laborschedule/push",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload, timeout=TIMEOUT_SECONDS)
    except Exception as e:
        pos_io.record("rpower", restaurant_id, "laborschedule/push", "transport", (time.monotonic() - t0) * 1000)
        pos_io.breaker_failure("rpower", restaurant_id, f"laborschedule/push: {type(e).__name__}", probe=gate == "probe")
        return {"ok": False, "error": f"Couldn't reach RPOWER: {e}", "skipped": skipped}
    _ms = (time.monotonic() - t0) * 1000
    _st = int(getattr(resp, "status_code", 0) or 0)
    pos_io.record("rpower", restaurant_id, "laborschedule/push",
                  "ok" if resp.ok else ("s5xx" if _st >= 500 else ("s429" if _st == 429 else "s4xx")), _ms)
    if _st >= 500:
        pos_io.breaker_failure("rpower", restaurant_id, f"laborschedule/push: HTTP {_st}", probe=gate == "probe")
    else:
        pos_io.breaker_success("rpower", probe=gate == "probe")

    if resp.status_code in (401, 403):
        return {"ok": False, "skipped": skipped,
                "error": "RPOWER refused the schedule push. It is disabled by default — "
                         "email integrations@rpower.com to enable it for this token."}
    if not resp.ok:
        return {"ok": False, "skipped": skipped,
                "error": f"RPOWER returned HTTP {resp.status_code}: {resp.text[:200]}"}
    return {"ok": True, "employees": len(by_employee),
            "shifts": sum(len(v) for v in by_employee.values()), "skipped": skipped}


# ── the ticket-level archive (pos_archive.py stores these) ──────────────────
#
# RPower endpoint audit, 9/29/26, Critical #2: only daily totals were kept, so
# every night's ticket detail — the server, the table, the kitchen's fire and
# bump times, covers, tips, the reason on each comp — was summed and thrown
# away. RPOWER itself asks integrators to archive. These return one business
# date as provider-neutral rows; pos_archive writes them.

CATALOG_CACHE_SECONDS = 600
_catalog_cache = _LRU(CACHE_MAX_KEYED)


_scope_cache = _LRU(CACHE_MAX_KEYED)


def _list_scope(restaurant_id: int, by_store: bool = False) -> str:
    """The pos_catalogs scope of a store's list: its consolidation group
    (and store, `by_store`) — remembered for CATALOG_CACHE_SECONDS."""
    key = (restaurant_id, bool(by_store))
    hit = _scope_cache.get(key)
    if hit and time.monotonic() - hit[0] < CATALOG_CACHE_SECONDS:
        return hit[1]
    token, base = _ctx(restaurant_id)
    scope = _scope(restaurant_id, token, base, by_store)
    _scope_cache[key] = (time.monotonic(), scope)
    return scope


def _catalog(restaurant_id: int, path: str, by_store: bool = False) -> dict:
    """{mid: row} of one RPOWER list (tables, rooms, meal times, profit
    centers), read once per CATALOG_CACHE_SECONDS — through _list, so the
    group's kept copy answers before RPOWER does. Kept once per group (or
    store) and list, not per restaurant (blind re-audit 10/20/26 R3-19: every
    store of a group held its own copy of the same lists)."""
    key = (_list_scope(restaurant_id, by_store), path)
    hit = _catalog_cache.get(key)
    if hit and not _fresh() and time.monotonic() - hit[0] < CATALOG_CACHE_SECONDS:
        return hit[1]
    out = {str(r["mid"]): r for r in _list(restaurant_id, path, by_store=by_store) if r.get("mid")}
    _catalog_cache[key] = (time.monotonic(), out)
    return out


def _raw(value):
    """A reference kept exactly as RPOWER sent it (as text), None only when absent."""
    return None if value is None else str(value)


def _id(value):
    """An RPOWER reference, or None for its empty forms ("", 0, "0")."""
    v = str(value if value is not None else "").strip()
    return None if v in ("", "0", "0.0") else v


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _local_stamp(raw):
    dt = _punch_dt(raw)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None


def line_kind(row: dict, types: dict) -> str:
    """What one ticketsales line is, as the archive stores it: sale | comp |
    void | refund | discount | nonsale | other — _classify, the same rule the
    nightly report uses (POS-06), with a line of an unknown or excluded
    sales type (tax, gratuity, fee) filed as "other"."""
    kind = _classify(row, types)
    return "other" if kind in ("unknown", "excluded") else kind


@_one_store_scope
def archive_rows(restaurant_id: int, business_date) -> dict:
    """One business date's tickets, sale lines and punches, every field the
    archive keeps, names resolved from RPOWER's own lists:
    {"tickets": [...], "lines": [...], "punches": [...], "max_stamp"}.

    Reads ticket/, ticketsales/ and ticketpayment/getbybusinessdate,
    payout/getbydaterange and timeclock/getbydaterange (the day and the next,
    kept to the business day: punch_business_date's 4am to 4am), and the lists: sales types,
    menu, void reasons, people, tables and rooms (table/room getbystore),
    meal times, profit centers, payment methods and payout categories."""
    day = business_date if hasattr(business_date, "toordinal") else date.fromisoformat(_d(business_date))
    token, base = _ctx(restaurant_id)
    iso = _d(day)
    # The night's records first, then the lists that name them: a list kept
    # for the group (_list, A3-07) that does not name an id the night
    # carries — a sales type, item, employee, table, payment method added
    # since it was read — is read afresh once, so nothing is filed unknown.
    raw_lines = _paged(token, "ticketsales/getbybusinessdate",
                       {**base, "startdate": iso, "enddate": iso, "sortorder": "date"})
    raw_tickets = _paged(token, "ticket/getbybusinessdate",
                         {**base, "startdate": iso, "enddate": iso, "sortorder": "date"})
    start, end = _punch_window(day)            # the schedule's boundary (R3-10)
    raw_punches = [e for e in fetch_time_entries(restaurant_id, day, day + timedelta(days=1)) or []
                   if (lambda t: t is not None and start <= t < end)(_punch_dt(e.get("in_dttm")))]
    raw_payments = _paged(token, "ticketpayment/getbybusinessdate",
                          {**base, "startdate": iso, "enddate": iso, "sortorder": "date"})
    # RPOWER's own example spells this one `startDate` (and `enddate`), a
    # single day asked as start == end. UNPROVEN (bar audit 10/14/26 RPW-17
    # / R26): that payout's enddate is inclusive — timeclock/getbydaterange
    # treats its enddate as exclusive, and EJ's has one payout in 94 days (a
    # $0.01 test, 8/29) to judge by. To probe once on a live store (the lead,
    # read-only): ask a day with a known payout as start == end and as end =
    # day + 1, and compare the rows.
    raw_payouts = _paged(token, "payout/getbydaterange",
                         {**base, "startDate": iso, "enddate": iso, "sortorder": "date"})
    lists = _archive_lists(restaurant_id)
    missed = _archive_misses(lists, raw_lines, raw_tickets, raw_punches, raw_payments, raw_payouts)
    if missed:
        lists = _refresh_missed(restaurant_id, lists, missed)
    types, menu, reasons, people = lists["types"], lists["menu"], lists["reasons"], lists["people"]
    emps, jobs = people.get("employees") or {}, people.get("jobs") or {}
    tables, rooms, meals, centers = lists["tables"], lists["rooms"], lists["meals"], lists["centers"]
    # Each line's department and sales category as RPOWER filed it the night
    # it was archived (bar audit 10/14/26 F5): the Bar maps POS item
    # categories to beer / liquor / wine, and a night read back from the
    # archive needs them without asking RPOWER's catalog again.
    menu_rows, cat_rows, dep_rows = lists["menu_rows"], lists["cat_rows"], lists["dep_rows"]

    def _name(cat, mid):
        row = cat.get(mid) if mid else None
        return _tidy_name(row.get("name")) if row and row.get("name") else None

    def _filed(item_mid):
        cat_id = _id((menu_rows.get(item_mid or "") or {}).get("slscat_mid"))
        cat = cat_rows.get(cat_id or "") or {}
        dep = dep_rows.get(_id(cat.get("slsdep_mid")) or "") or {}
        # Spelled exactly as fetch_day_sales spells it (the report's
        # `sales.depcat:` keys), so the two meet on one name.
        return (cat_id, ((cat.get("name") or "").strip() or None),
                ((dep.get("name") or "").strip() or (UNASSIGNED_DEPARTMENT if item_mid else None)))

    stamps = []
    lines, net_by_ticket = [], {}
    # Our line totals beside RPOWER's own per-ticket type totals (POS-11):
    # pos_archive.archive_day reconciles them and raises a discrepancy.
    checks = {"lines_sale": 0.0, "lines_discount": 0.0, "unknown_type_lines": 0, "unknown_type_sales": 0.0}
    by_flag = {}
    for r in raw_lines:
        if _biz_date(r.get("date")) != iso or not r.get("rid"):
            continue
        kind = line_kind(r, types)
        tid = _id(r.get("ticket_rid"))
        item = _id(r.get("menuitem_mid"))
        sales = round(_num(r.get("sales")), 2)
        if kind in ("sale", "discount") and tid:
            net_by_ticket[tid] = round(net_by_ticket.get(tid, 0.0) + sales, 2)
        if kind in ("sale", "discount"):
            key = "lines_sale" if kind == "sale" else "lines_discount"
            checks[key] = round(checks[key] + sales, 2)
            flag = _type_flag(types.get(str(r.get("slstype_mid"))))
            if flag:
                by_flag[flag] = round(by_flag.get(flag, 0.0) + sales, 2)
        elif _classify(r, types) == "unknown":
            checks["unknown_type_lines"] += 1
            checks["unknown_type_sales"] = round(checks["unknown_type_sales"] + sales, 2)
        approver = _id(r.get("voidmgr_mid") if kind == "void" else r.get("mgr_mid"))
        reason = _id(r.get("voidrsn_mid"))
        stamps.append(str(r.get("time_stamp") or ""))
        cat_id, cat_name, dep_name = _filed(item)
        lines.append({
            # Bar-relevant ticketsales fields (F5), as the collection names
            # them: `cost` (RPOWER's cost of the line, where kept), `discounted`
            # (undocumented: kept to test whether it is RPOWER's own share of
            # a check discount — bar.sales reads it only when a ticket's lines
            # add up to its discount lines), `aqu_count` (alternate quantity,
            # e.g. ounces), `fraction`, and `atom` (the line's id inside its
            # ticket, which ticketitem's parent_atom points at).
            "cost": round(_num(r.get("cost")), 4), "discounted": round(_num(r.get("discounted")), 2),
            "aqu_count": _num(r.get("aqu_count"), None), "fraction": _num(r.get("fraction"), None),
            "atom": _id(r.get("atom")), "category_id": cat_id, "sales_category": cat_name,
            "department": dep_name,
            # The rid with the atom, the identity _paged keys a line on
            # (_row_key): a line keyed on its rid alone was lost whenever it
            # shared a rid with another in the INSERT OR REPLACE (POS-10).
            "line_id": line_key(r), "ticket_id": tid, "business_date": iso,
            "item_id": item, "item_name": (menu.get(item) or {}).get("name") if item else None,
            "item_kind": (menu.get(item) or {}).get("kind") if item else None,
            "kind": kind, "qty": _num(r.get("qty"), 0.0), "sales": sales,
            "price": round(_num(r.get("price")), 2), "regular_price": round(_num(r.get("regular_price")), 2),
            "loss_amount": _loss_amount(r) if kind in ("comp", "void", "refund") else None,
            "reason": reasons.get(reason) if reason else None, "approver_id": approver,
            "item_at": _local_stamp(r.get("item_dttm")),
            "mealtime": _name(meals, _id(r.get("mealtime_mid"))),
            "profit_center": _name(centers, _id(r.get("pcenter_mid"))),
            "price_level_id": _id(r.get("prclvl_mid")),
            "source_stamp": str(r.get("time_stamp") or "")[:19] or None,
            # Raw, exactly as RPOWER sent them: _raw_from_archive rebuilds the
            # line from these for the readers that derive from raw rows.
            "sales_type_id": _raw(r.get("slstype_mid")), "voided": _num(r.get("voided")),
            "mgr_id": _raw(r.get("mgr_mid")), "void_mgr_id": _raw(r.get("voidmgr_mid")),
            "reason_id": _raw(r.get("voidrsn_mid")), "shift": _raw(r.get("shift"))})
    links_ok = _link_modifiers(token, base, iso, lines) is not None
    tickets = []
    for t in raw_tickets:
        if _biz_date(t.get("date")) != iso or not t.get("rid"):
            continue
        tid = str(t["rid"])
        table = tables.get(_id(t.get("table_mid")) or "") or {}
        room = rooms.get(_id(table.get("room_mid")) or "") or {}
        server = _id(t.get("main_server"))
        stamps.append(str(t.get("time_stamp") or ""))
        tickets.append({
            "ticket_id": tid, "business_date": iso, "ticket_no": _id(t.get("ticket")),
            "opened_at": _local_stamp(t.get("open_dttm")), "closed_at": _local_stamp(t.get("close_dttm")),
            "fired_at": _local_stamp(t.get("kvs_dttm")), "bumped_at": _local_stamp(t.get("bump_dttm")),
            "need_at": _local_stamp(t.get("need_dttm")),
            "server_id": server, "server_name": emps.get(server) if server else None,
            "table_id": _id(t.get("table_mid")), "table_name": _tidy_name(table.get("name")) or None,
            "room_name": _tidy_name(room.get("name")) or None,
            "is_bar": 1 if room.get("is_bar") else 0,
            # The customer RPOWER attached to the check (cust_mid; 1 is its
            # no-customer placeholder) - guest_hub matches a real one to a guest.
            "customer_id": _id(t.get("cust_mid")),
            "guest_count": int(_num(t.get("guest_count"))), "entree_count": int(_num(t.get("entree_count"))),
            "bev_count": int(_num(t.get("bev_count"))), "net_sales": net_by_ticket.get(tid, 0.0),
            "discount": round(_num(t.get("discount")), 2), "tip": round(_num(t.get("tip")), 2),
            "grat": round(_num(t.get("grat")), 2), "tax": round(_num(t.get("tax")), 2),
            "mealtime": _name(meals, _id(t.get("mealtime_mid"))),
            "profit_center": _name(centers, _id(t.get("pcenter_mid"))),
            "cancelled": 1 if t.get("is_cancelled") else 0,
            "source_stamp": str(t.get("time_stamp") or "")[:19] or None,
            **{k: round(_num(t.get(k)), 2) for k in ("type_sale", "type_discnt", "type_promo", "type_comp")}})
    punches = _punch_records(raw_punches, iso, people)
    stamps.extend(str(e.get("time_stamp") or "") for e in raw_punches if e.get("rid"))
    methods = lists["methods"]
    payments = []
    for p in raw_payments:
        if _biz_date(p.get("date")) != iso or not p.get("rid"):
            continue
        m = methods.get(_id(p.get("paymeth_mid")) or "") or {}
        payments.append({
            "payment_id": str(p["rid"]), "ticket_id": _id(p.get("ticket_rid")), "business_date": iso,
            "method": _tidy_name(m.get("name")) or None, "is_cash": 1 if m.get("is_cash") else 0,
            "is_card": 1 if m.get("is_cc") else 0, "amount": round(_num(p.get("paid_ticket")), 2),
            "tip": round(_num(p.get("paid_tip")), 2), "tip_fee": round(_num(p.get("tip_fee")), 2)})
    cats = lists["payout_cats"]
    payouts = []
    for p in raw_payouts:
        if _biz_date(p.get("date")) != iso or not p.get("rid"):
            continue
        c = cats.get(_id(p.get("pocat_mid")) or "") or {}
        mgr = _id(p.get("mgr_mid")) or _id(p.get("emp_mid"))
        payouts.append({
            "payout_id": str(p["rid"]), "business_date": iso, "category": _tidy_name(c.get("name")) or None,
            "is_payin": 1 if p.get("is_payin") else 0, "amount": round(_num(p.get("net")), 2),
            "manager_id": mgr, "manager_name": emps.get(mgr) if mgr else None,
            "paid_at": _local_stamp(p.get("dttm")), "reference": (str(p.get("reference") or "").strip() or None)})
    # RPOWER's own total of each sales type our revenue lines carry, from
    # the tickets it summarises them on (cancelled tickets left out, as the
    # report leaves them out): the two sides of the nightly reconcile.
    live = [t for t in raw_tickets if _biz_date(t.get("date")) == iso and t.get("rid") and not t.get("is_cancelled")]
    flags = set(by_flag) | {"type_sale", *DISCOUNT_FLAGS}
    theirs = {f: round(sum(_num(t.get(f)) for t in live), 2) for f in sorted(flags)}
    # RPOWER's "Cash Discount" item (a cash-payment discount rung as a menu item, often posted after close)
    # is only partly - or not at all - in its own ticket's type totals, while the ticket's lines carry it
    # whole: at EJ's a -$14.50 one on a ticket of its own (10/8/26) and -$3.99 of a -$7.98 one (10/17/26).
    # Our net keeps the line (it is money not taken); the shortfall it explains on a ticket is counted on
    # its own (cash_discount_unreflected), not as an unexplained gap (owner, 10/19/26: the reconcile paged it
    # every re-read).
    cash = {}
    for ln in lines:
        if ln["kind"] in ("sale", "discount") and ln["ticket_id"] and _CASH_DISCOUNT.search(ln.get("item_name") or ""):
            cash[ln["ticket_id"]] = round(cash.get(ln["ticket_id"], 0.0) + ln["sales"], 2)
    explained = 0.0
    for t in live:
        tid = _id(t.get("rid"))
        c = cash.get(tid, 0.0)
        if c < 0:
            d = round(net_by_ticket.get(tid, 0.0) - sum(_num(t.get(f)) for f in flags), 2)
            if c - 0.005 <= d < 0:
                explained = round(explained + d, 2)
    checks.update(lines_by_type=dict(sorted(by_flag.items())), tickets_by_type=theirs,
                  gap=round(sum(by_flag.values()) - sum(theirs.values()) - explained, 2), tickets=len(live),
                  cash_discount_unreflected=explained)
    return {"tickets": tickets, "lines": lines, "punches": punches, "payments": payments, "payouts": payouts,
            "max_stamp": (max(s for s in stamps if s)[:19] if any(stamps) else None),
            "links_ok": links_ok, "checks": checks}


def _punch_records(raw_punches: list, iso: str, people: dict) -> list:
    """One business date's timeclock rows as the archive keeps them
    (pos_punches) — archive_rows, and the nightly labor read's refresh of a
    recent day (build_shifts_csv)."""
    stations = station_logins(raw_punches, people)
    emps, jobs = people.get("employees") or {}, people.get("jobs") or {}
    punches = []
    for e in raw_punches:
        if not e.get("rid"):
            continue
        mid = _id(e.get("emp_mid"))
        punches.append({
            "punch_id": str(e["rid"]), "business_date": iso,
            "employee_id": mid, "employee_name": emps.get(mid) if mid else None,
            "job_id": _id(e.get("job_mid")), "role": jobs.get(_id(e.get("job_mid")) or "") or None,
            "clock_in": _local_stamp(e.get("in_dttm")), "clock_out": _local_stamp(e.get("out_dttm")),
            "reg_hours": _num(e.get("reg_hours")), "ot_hours": _num(e.get("ot_hours")),
            "dt_hours": _num(e.get("dt_hours")), "reg_rate": _num(e.get("reg_rate")),
            "ot_rate": _num(e.get("ot_rate")),
            "pay": round(sum(_num(e.get(k)) for k in ("reg_pay", "ot_pay", "dt_pay", "spread_pay", "callin_pay")), 2),
            "ot_pay": round(_num(e.get("ot_pay")) + _num(e.get("dt_pay")), 2),
            "tips": round(_num(e.get("tips_total")), 2), "tips_net": round(_num(e.get("tip_net")), 2),
            "grats": round(_num(e.get("grats_actual")), 2),
            "break_minutes": _num(e.get("break_minutes")),
            "meal_minutes": _num(e.get("meal1_minutes")) + _num(e.get("meal2_minutes")),
            "rest_minutes": sum(_num(e.get(f"rest{i}_minutes")) for i in (1, 2, 3)),
            # editmgr_mid is one placeholder id on every punch at EJ's; an edit
            # is real only when it has a real edit time.
            "edited_by": _id(e.get("editmgr_mid")) if _punch_dt(e.get("edit_dttm")) else None,
            "edited_at": _local_stamp(e.get("edit_dttm")),
            "edit_what": _id(e.get("edit_what")),
            "is_station": 1 if mid in stations else 0,
            "source_stamp": str(e.get("time_stamp") or "")[:19] or None})
    return punches


_CASH_DISCOUNT = re.compile(r"\bcash\s*discount\b", re.I)


def _archive_lists(restaurant_id: int) -> dict:
    """Every list archive_rows names a night's records from (_list's kept
    copies, or afresh inside fresh_lists())."""
    return {"types": sales_types(restaurant_id), "menu": menu_lookup(restaurant_id),
            "reasons": void_reasons(restaurant_id), "people": _people_cached(restaurant_id),
            "tables": _catalog(restaurant_id, "table/getbystore", by_store=True),
            "rooms": _catalog(restaurant_id, "room/getbystore", by_store=True),
            "meals": _catalog(restaurant_id, "mealtime/getbycg"),
            "centers": _catalog(restaurant_id, "profitcenter/getbycg"),
            "menu_rows": _catalog(restaurant_id, "menuitem/getbycg"),
            "cat_rows": _catalog(restaurant_id, "salescategory/getbycg"),
            "dep_rows": _catalog(restaurant_id, "salesdepartment/getbycg"),
            "methods": _catalog(restaurant_id, "paymentmethod/getbycg"),
            "payout_cats": _catalog(restaurant_id, "payoutcategory/getbycg")}


def _archive_misses(lists, lines, tickets, punches, payments, payouts) -> list:
    """The kinds of list a night's records name an id the kept copy does not
    hold — sales types (what counts as revenue), menu items (where a line
    is filed), tables, jobs, payment methods, payout categories. Void reasons
    and employee names are left out: a placeholder id on every line (RPOWER's
    "1" reason) would read the lists afresh on every night."""
    jobs = (lists["people"].get("jobs") or {})
    want = (
        [("types", r.get("slstype_mid")) for r in lines] + [("menu_rows", r.get("menuitem_mid")) for r in lines]
        + [("tables", r.get("table_mid")) for r in tickets] + [("jobs", r.get("job_mid")) for r in punches]
        + [("methods", r.get("paymeth_mid")) for r in payments]
        + [("payout_cats", r.get("pocat_mid")) for r in payouts])
    have = {"types": lists["types"], "menu_rows": lists["menu_rows"], "jobs": jobs, "tables": lists["tables"],
            "methods": lists["methods"], "payout_cats": lists["payout_cats"]}
    return sorted({k for k, v in want if _id(v) and str(_id(v)) not in have[k]})


# A list read afresh for a miss is not read afresh again for the same store
# within this many seconds: an id the POS no longer lists (a deleted item on
# a backfilled night) would otherwise re-read it for every night of a backfill.
ARCHIVE_REFRESH_SECONDS = 600
_archive_refreshed = _LRU(CACHE_MAX_KEYED)


def _refresh_missed(restaurant_id: int, lists: dict, missed: list) -> dict:
    """Re-read, afresh, the lists `missed` names (each once per
    ARCHIVE_REFRESH_SECONDS per store) into a copy of `lists`."""
    out = dict(lists)
    for kind in missed:
        key = (restaurant_id, kind)
        hit = _archive_refreshed.get(key)
        if hit and time.monotonic() - hit < ARCHIVE_REFRESH_SECONDS:
            continue
        _archive_refreshed[key] = time.monotonic()
        with fresh_lists():
            if kind == "types":
                out["types"] = sales_types(restaurant_id)
            elif kind == "menu_rows":
                out.update(menu=menu_lookup(restaurant_id),
                           menu_rows=_catalog(restaurant_id, "menuitem/getbycg"),
                           cat_rows=_catalog(restaurant_id, "salescategory/getbycg"),
                           dep_rows=_catalog(restaurant_id, "salesdepartment/getbycg"))
            elif kind == "tables":
                out.update(tables=_catalog(restaurant_id, "table/getbystore", by_store=True),
                           rooms=_catalog(restaurant_id, "room/getbystore", by_store=True))
            elif kind == "jobs":
                out["people"] = _people_cached(restaurant_id)
            elif kind == "methods":
                out["methods"] = _catalog(restaurant_id, "paymentmethod/getbycg")
            elif kind == "payout_cats":
                out["payout_cats"] = _catalog(restaurant_id, "payoutcategory/getbycg")
    return out


def _type_flag(st):
    """The type_* flag a sales type is (RPOWER sets one per type), or None."""
    for k, v in (st or {}).items():
        if k.startswith("type_") and v:
            return k
    return None


def line_key(row: dict) -> str:
    """An archived line's id: RPOWER's rid, with the line's atom (its id
    inside the ticket) when it has one - _row_key's identity, so two lines
    _paged kept apart are never one row in pos_ticket_lines (POS-10)."""
    atom = _id(row.get("atom"))
    return f"{row['rid']}:{atom}" if atom else str(row["rid"])


def _link_modifiers(token: str, base: dict, iso: str, lines: list) -> int:
    """Set `parent_atom`, `mod_level` and `parent_line_id` on the night's
    archived sale lines from ticketitem/getbybusinessdate — the only RPOWER
    read that says which drink a "Double", "Rocks" or "(Liquor Add-Ons)" line
    modifies (bar audit 10/14/26 RPW-9 / F5). RPOWER's own warning holds:
    ticket items are NEVER summed for sales or quantities here — only their
    parent links are copied onto the ticketsales lines, matched on (ticket,
    atom). A failed read leaves the links empty and the night archived (the
    links are an addition, not the record); returns the lines linked, or
    None when the read failed: pos_archive records the day's links_ok from
    it, and a day without its links is archived again (POS-12)."""
    try:
        items = _paged(token, "ticketitem/getbybusinessdate",
                       {**base, "startdate": iso, "enddate": iso, "sortorder": "date"})
    except (RPowerAuthError, RPowerTryLater):
        raise                    # a paused RPOWER ends the night's read whole, never a night stored without links
    except Exception as e:
        log.warning("[rpower] ticketitem links unread for %s: %s", iso, e)
        return None
    links = {}
    for it in items:
        if _biz_date(it.get("date")) != iso:
            continue
        tid, atom = _id(it.get("ticket_rid")), _id(it.get("atom"))
        if tid and atom:
            links[(tid, atom)] = (_id(it.get("parent_atom")), int(_num(it.get("mod_level"))))
    by_atom = {(l["ticket_id"], l.get("atom")): l["line_id"] for l in lines if l.get("atom")}
    linked = 0
    for l in lines:
        hit = links.get((l["ticket_id"], l.get("atom")))
        if not hit:
            continue
        parent, level = hit
        l["parent_atom"], l["mod_level"] = parent, level
        l["parent_line_id"] = by_atom.get((l["ticket_id"], parent)) if parent else None
        linked += 1
    return linked


# The change feeds a restatement is watched through (food & bar audit
# 10/16/26 POS-19): a sale line rewritten, and a ticket rewritten with no
# sale line touched — a cancellation, a guest-count or room change, a tip
# adjusted on the check. Both are RPOWER's documented getbytimestamp reads
# (docs/integrations/rpower_core_api.postman.json: GetTicketSalesByTimeStamp,
# GetTicketByTimeStamp), asked in the same week-sized chunks.
CHANGE_FEEDS = ("ticketsales/getbytimestamp", "ticket/getbytimestamp")


# Change reads ask with a time, not only a date (blind re-audit 10/20/26
# R3-03): `startdate` / `enddate` as YYYY-MM-DDTHH:MM:SS — RPOWER's own
# collection asks ticketitem/getbytimestamp that way. A date-only window was
# whole UTC days (2–4 of them, about 7 pages) for a question about the last
# half hour. Two allowances while RPOWER has not said how it reads the time
# (Q5: UTC, or the store's local time):
#   * the local filter keeps a write only when it is later than `since`
#     less CHANGE_OVERLAP (clock skew between us and RPOWER);
#   * the window asked for starts RPOWER_CHANGE_MARGIN_HOURS earlier still,
#     so a server reading the time as US local (up to 10 hours behind UTC)
#     still returns every write after `since`. Set it to 0 once RPOWER
#     confirms the filter is UTC.
# RPOWER_CHANGE_DATETIME=0 (or a datetime window answered with an error,
# remembered for the process) goes back to date-only windows.
CHANGE_DATETIME = os.getenv("RPOWER_CHANGE_DATETIME", "1").strip().lower() not in ("0", "false", "off", "no")
CHANGE_MARGIN = timedelta(hours=max(0.0, float(os.getenv("RPOWER_CHANGE_MARGIN_HOURS", "10") or 0)))
CHANGE_OVERLAP = timedelta(minutes=15)
_change_datetime_refused = {"at": None}


def changed_business_dates(restaurant_id: int, since_utc: datetime, until_utc: datetime) -> set:
    """Business dates with a sale line or a ticket written or rewritten in
    RPOWER's database between two UTC moments (changed_business_stamps)."""
    return set(changed_business_stamps(restaurant_id, since_utc, until_utc))


def changed_business_stamps(restaurant_id: int, since_utc: datetime, until_utc: datetime) -> dict:
    """{business date: its newest write stamp} for the sale lines and
    tickets written or rewritten in RPOWER's database between two UTC
    moments — CHANGE_FEEDS, which RPOWER documents as "when a record was
    last inserted or updated" (UTC). A store re-posting a closed day moves
    its time_stamp, so a date already archived shows up here; the archive
    re-reads it only when that stamp is newer than the newest write it
    stored the day with (pos_archive._moved, R3-02).

    Asked a week at a time (RPOWER's ask, MAX_RANGE_DAYS; bar audit 10/14/26
    RPW-17 / R26): after an outage `changes_checked_to` can fall weeks
    behind, and one request spanning all of it is exactly the oversized read
    the vendor asked us not to make. Each request's end is the next one's
    start, so nothing falls between two requests. With a time in the window
    (CHANGE_DATETIME, above) it runs from `since` less the margin to
    `until`; date-only, from since's date to the day after until's (enddate
    possibly exclusive)."""
    token, base = _ctx(restaurant_id)
    lo = (since_utc - CHANGE_OVERLAP).strftime("%Y-%m-%d %H:%M:%S")
    if CHANGE_DATETIME and _change_datetime_refused["at"] is None:
        try:
            return _change_read(token, base, lo, since_utc - CHANGE_OVERLAP - CHANGE_MARGIN,
                                until_utc + timedelta(minutes=1), True)
        except (RPowerAuthError, RPowerTryLater):
            raise
        except RPowerError as e:
            if "HTTP 4" not in str(e):
                raise
            # RPOWER refused a time in the window: date-only from here on in
            # this process (said once in the log).
            _change_datetime_refused["at"] = time.time()
            log.warning("[rpower] change read refused a datetime window (%s); date-only windows from now on", e)
    return _change_read(token, base, lo, since_utc, until_utc + timedelta(days=1), False)


def _change_read(token, base, lo, start, stop, with_time) -> dict:
    fmt = (lambda v: v.strftime("%Y-%m-%dT%H:%M:%S")) if with_time else _d
    if not with_time:
        start = date.fromisoformat(_d(start))
        stop = date.fromisoformat(_d(stop))
    out = {}
    cursor = start
    while True:
        end = min(cursor + timedelta(days=MAX_RANGE_DAYS), stop)
        for feed in CHANGE_FEEDS:
            rows = _paged(token, feed, {
                **base, "startdate": fmt(cursor), "enddate": fmt(end), "sortorder": "time_stamp"})
            for r in rows:
                stamp = str(r.get("time_stamp") or "").replace("T", " ")[:19]
                day = _biz_date(r.get("date"))
                if day and stamp and stamp > lo:
                    out[day] = max(out.get(day) or "", stamp)
        if end >= stop:
            break
        cursor = end
    return out


def fetch_employee_jobs(restaurant_id: int) -> list:
    """Every active job each employee holds at this store:
    [{"external_id", "role", "rate", "primary", "salaried"}] —
    employeejob/getbystore (RPower endpoint audit, 9/29/26, High ROI #5).
    Who can work which job: 83 of Simple EJ's current staff hold two or
    more. RPOWER keeps job rows active for people long gone (429 employees
    held jobs, 105 were current), so the caller keeps only people it knows.

    Kept like every list (_list, pos_catalogs per store): the owner's push
    preview on a web request reads the kept copy and never RPOWER (blind
    re-audit 10/20/26 R3-07 — it read this list live on every open)."""
    token, base = _ctx(restaurant_id)
    ppl = _people_cached(restaurant_id)
    jobs, emps = ppl.get("jobs") or {}, ppl.get("employees") or {}
    out = []
    for r in _list(restaurant_id, "employeejob/getbystore", by_store=True, sortorder="mid",
                   extra={"isinactive": 0}):
        emp, job = _id(r.get("emp_mid")), _id(r.get("job_mid"))
        if not emp or not job or r.get("is_inactive") or not jobs.get(job):
            continue
        out.append({"external_id": emp, "employee_name": emps.get(emp), "role": jobs[job],
                    "rate": round(_num(r.get("reg_rate")), 2),
                    "primary": bool(r.get("is_primary")), "salaried": bool(r.get("is_salary"))})
    return out


def fetch_menu_prices(restaurant_id: int) -> list:
    """The store's price for each menu item at each price level:
    [{"item_id", "item_name", "level_id", "level", "price"}] —
    menuitemprice/getbystore with pricelevel/getbycg names (RPower endpoint
    audit, High ROI #7). Only items on the menu and prices above zero
    (Simple EJ's: 10,476 rows, 546 menu items, levels "Level 1".."Level 9")."""
    token, base = _ctx(restaurant_id)
    menu = menu_lookup(restaurant_id)
    # A sale line names its level by number (prclvl_mid "1"); the price list
    # by the level's record id. The level's seq_id is that number (checked on
    # Simple EJ's, 9/29/26: Level 1..9 = seq_id 1..9), so levels are keyed on
    # it and match the archived lines' price_level_id.
    levels = {k: (str(v.get("seq_id")) if v.get("seq_id") not in (None, "") else k, _tidy_name(v.get("name")))
              for k, v in _catalog(restaurant_id, "pricelevel/getbycg").items()}
    out = []
    for r in _paged(token, "menuitemprice/getbystore", {**base, "sortorder": "mid"}):
        item, level, price = _id(r.get("menuitem_mid")), _id(r.get("prclvl_mid")), round(_num(r.get("price")), 2)
        if not item or not level or price <= 0 or item not in menu:
            continue
        key, lname = levels.get(level, (level, None))
        out.append({"item_id": item, "item_name": menu[item].get("name"), "level_id": key,
                    "level": lname, "price": price})
    return out


ORDERS_DUE_CACHE_SECONDS = 3600
ORDERS_DUE_KIND = "orders_due"
ORDERS_DUE_KEEP_DAYS = 14          # the window the nightly run keeps (pos_archive.run_for)


def _drop_dated_orders_due(restaurant_id):
    """The store's snapshot rows from before 10/20/26, one per end date
    ("orders_due:YYYY-MM-DD", R3-16). Never raises."""
    import models
    try:
        conn = models.get_conn()
        try:
            conn.execute("DELETE FROM pos_live_snapshot WHERE restaurant_id=? AND kind LIKE 'orders_due:%'",
                         (int(restaurant_id),))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("[rpower] old orders-due snapshots not dropped for %s: %s", restaurant_id, e)



def fetch_orders_due(restaurant_id: int, start_date, end_date) -> list:
    """Orders due at a later time — catering, scheduled pick-up — by the time
    they are needed: [{"due_at", "date", "guests", "net_sales", "ticket_no"}]
    (ticket/getbyneeddatetime; RPower endpoint audit, High ROI #11). Only
    tickets with a real "needed by" time: RPOWER's 2000-01-01 means none
    (every ticket at Simple EJ's, 9/29/26)."""
    token, base = _ctx(restaurant_id)
    # Kept for ORDERS_DUE_CACHE_SECONDS (scale audit 10/19/26 A3-18): Ask's
    # read_upcoming asked RPOWER on every question about the days ahead.
    # One row per store (kind "orders_due", the window in its value — blind
    # re-audit 10/20/26 R3-16: a kind per end date added a row per store per
    # day, never pruned). A read for an owner (a web request, or Ask's
    # archive-only read) takes the kept window at any age when it covers the
    # days asked and never reads RPOWER (R3-07); the nightly archive run
    # keeps it (pos_archive.run_for).
    import pos_io
    start_iso, end_iso = _d(start_date), _d(end_date)
    kept = pos_io.snapshot_get(restaurant_id, ORDERS_DUE_KIND, start_iso)
    value = kept["value"] if kept is not None else None
    covers = (isinstance(value, dict) and isinstance(value.get("rows"), list)
              and str(value.get("end") or "") >= end_iso)
    if covers and (kept["age"] < ORDERS_DUE_CACHE_SECONDS or _reader_only()):
        rows = value["rows"]
    elif _reader_only():
        raise RPowerArchiveMiss("orders due are not kept for these days yet; not read live for an owner's read")
    else:
        rows = _paged(token, "ticket/getbyneeddatetime", {**base, "startdate": start_iso,
                                                           "enddate": _d(end_date + timedelta(days=1)),
                                                           "sortorder": "need_dttm"})
        pos_io.snapshot_put(restaurant_id, ORDERS_DUE_KIND, start_iso,
                            {"end": end_iso,
                             "rows": [{k: t.get(k) for k in ("need_dttm", "is_cancelled", "guest_count", "ticket",
                                                             "items")} for t in rows]})
        _drop_dated_orders_due(restaurant_id)
    out = []
    for t in rows:
        due = _punch_dt(t.get("need_dttm"))
        if due is None or t.get("is_cancelled") or not (start_date <= due.date() <= end_date):
            continue
        out.append({"due_at": due.strftime("%Y-%m-%dT%H:%M:%S"), "date": due.date().isoformat(),
                    "guests": int(_num(t.get("guest_count"))), "ticket_no": _id(t.get("ticket")),
                    "net_sales": round(_num(t.get("items")), 2)})
    return out


# ── reading a closed day from the archive (9/29/26) ─────────────────────────
#
# The same day's ticketsales were downloaded by four readers every night —
# the nightly report (fetch_day_sales), stock depletion
# (fetch_order_selections), comps and voids (fetch_loss_lines) and daily net
# (fetch_business_days) — plus the catalogs each time. Now each asks
# _day_rows: a closed day the archive holds (pos_archive.ready_dates — it
# archives a closed day on first ask and re-stores a recent day RPOWER
# re-posted) is rebuilt from the archive with RPOWER's own field names, and
# the reader's own code runs on it unchanged; any other day is read from
# RPOWER as before.

CLOSED_CACHE_SECONDS = 600
_closed_cache = _LRU(CACHE_MAX_KEYED)


def day_closed_cached(restaurant_id: int, business_date) -> bool:
    """fetch_day_closed, remembered: a closed day forever (it stays closed),
    an open one for CLOSED_CACHE_SECONDS."""
    key = (restaurant_id, _d(business_date))
    hit = _closed_cache.get(key)
    if hit and (hit[1] or time.monotonic() - hit[0] < CLOSED_CACHE_SECONDS):
        return hit[1]
    closed = bool(fetch_day_closed(restaurant_id, business_date))
    _closed_cache[key] = (time.monotonic(), closed)
    return closed


# The key a row rebuilt from the archive carries its archived filing under
# (never one of RPOWER's own field names).
ARCHIVED_FILING = "_archived_filing"


def _stamp_raw(value):
    """An archived local time back in RPOWER's form, or None."""
    return value if value else None


def _raw_from_archive(restaurant_id: int, path: str, dates: list) -> list:
    """Archived rows for `dates` in the shape `path` returns from RPOWER —
    only the fields the readers use (see _day_rows)."""
    from models import get_conn
    if not dates:
        return []
    marks = ",".join("?" for _ in dates)
    conn = get_conn()
    try:
        if path == "ticketsales/getbybusinessdate":
            rows = conn.execute(
                f"SELECT line_id, ticket_id, business_date, item_id, qty, sales, price, regular_price, item_at, "
                f"sales_type_id, voided, mgr_id, void_mgr_id, reason_id, shift, department, sales_category, "
                f"item_name FROM pos_ticket_lines "
                f"WHERE restaurant_id=? AND provider='rpower' AND business_date IN ({marks})",
                (restaurant_id, *dates)).fetchall()
            # ARCHIVED_FILING: where RPOWER filed the line the night it was
            # archived (layout 4). fetch_day_sales prefers it to today's
            # catalog, so a night re-read after an item moved department, or
            # was deleted, stays in the department it sold under (POS-20).
            return [{"rid": r["line_id"], "ticket_rid": r["ticket_id"], "date": f"{r['business_date']}T00:00:00",
                     "menuitem_mid": r["item_id"], "qty": r["qty"], "sales": r["sales"], "price": r["price"],
                     "regular_price": r["regular_price"], "item_dttm": _stamp_raw(r["item_at"]),
                     "slstype_mid": r["sales_type_id"], "voided": r["voided"], "mgr_mid": r["mgr_id"],
                     "voidmgr_mid": r["void_mgr_id"], "voidrsn_mid": r["reason_id"], "shift": r["shift"],
                     ARCHIVED_FILING: ({"department": r["department"], "sales_category": r["sales_category"],
                                        "item_name": r["item_name"]} if r["department"] else None)}
                    for r in rows]
        if path == "ticket/getbybusinessdate":
            rows = conn.execute(
                f"SELECT ticket_id, business_date, opened_at, cancelled, guest_count, tax, type_sale, type_discnt, "
                f"type_promo, type_comp FROM pos_tickets WHERE restaurant_id=? AND provider='rpower' "
                f"AND business_date IN ({marks})", (restaurant_id, *dates)).fetchall()
            return [{"rid": r["ticket_id"], "date": f"{r['business_date']}T00:00:00",
                     "open_dttm": _stamp_raw(r["opened_at"]), "is_cancelled": r["cancelled"],
                     "guest_count": r["guest_count"], "tax": r["tax"], "type_sale": r["type_sale"],
                     "type_discnt": r["type_discnt"], "type_promo": r["type_promo"], "type_comp": r["type_comp"]}
                    for r in rows]
    finally:
        conn.close()
    raise ValueError(f"no archived form for {path}")


def _day_rows(restaurant_id: int, token: str, base: dict, path: str, start_date, end_date) -> list:
    """`path`'s rows (ticketsales or ticket getbybusinessdate) over a
    business-date range: every closed day the archive holds from the archive,
    the rest from RPOWER in the usual week-sized requests."""
    days = []
    d = start_date if hasattr(start_date, "toordinal") else date.fromisoformat(_d(start_date))
    end = end_date if hasattr(end_date, "toordinal") else date.fromisoformat(_d(end_date))
    while d <= end:
        days.append(d)
        d += timedelta(days=1)
    page_load = _reader_only()
    try:
        import pos_archive
        ready = pos_archive.ready_dates(restaurant_id, "rpower", days, live=not page_load)
    except Exception as e:                       # the archive is an optimisation, never a dependency
        log.warning("[rpower] archive not consulted for %s: %s", restaurant_id, e)
        ready = set()
    if page_load:
        # A web request reads the archive and nothing else (POS-13): the days
        # it does not hold are unknown to the page, never a live read on one
        # of the four request threads.
        missing = [d.isoformat() for d in days if d.isoformat() not in ready]
        if missing:
            raise RPowerArchiveMiss(
                f"{len(missing)} of {len(days)} day(s) not in the POS archive yet "
                f"({missing[0]}{'...' if len(missing) > 1 else ''}); not read live during a page load")
    rows = _raw_from_archive(restaurant_id, path, sorted(ready)) if ready else []
    run = []
    for d in days + [None]:
        if d is not None and d.isoformat() not in ready:
            run.append(d)
            continue
        if run:
            for cs, ce in _chunk_range(run[0], run[-1]):
                rows.extend(_paged(token, path, {**base, "startdate": _d(cs), "enddate": _d(ce), "sortorder": "date"}))
            run = []
    return rows
