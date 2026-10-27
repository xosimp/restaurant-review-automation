"""
home_snapshots.py — Home's slow blocks kept as per-restaurant read models
(scale audit 10/19/26, FE-01; built off the request since the blind
re-audit 10/20/26, R6-F4/F5/F6/F9/F10/F12).

Four of Home's reads were full builds on every load, on a request thread:
/morning-brief (13.4 s average in production), /good-news (11.1 s),
/monthly-review (5.8 s) and /cross-module (3.3 s) — about 33 s of the web
process's four request threads per Home open, and the iPhone's Home ran the
same reads (the bodies are shared by the web and /mobile/api twins). None of
them calls a model; they are deterministic reads over a restaurant's data,
so the answer is the same until the data moves. The group Home's per-location
row (`location_record`) is the fifth.

Each is kept in `home_snapshots`, one row per (restaurant, block, scope):

  * the SCOPE is who the answer is for. The morning brief and the
    cross-module links are built for one login (their silences, their
    learned ranking, what their role may see), so their scope is the login's
    identity and permissions (viewer_scope); good news depends only on the
    modules the viewer is denied (and the salaries); the monthly review's
    build is the same for every viewer but for the salaries (the route
    filters it per login afterwards).
  * `as_of` is the restaurant's business date: a row from another day is
    never served.
  * `stamp` is the data version (data_stamp): cheap indexed reads of what an
    owner's actions and the day's feeds change — answers and silences,
    issues, goals, tracked results, the nightly report, reviews and their
    replies, the shift history, the close-out, a schedule draft or Send, an
    inventory count — and the restaurant's settings (always the RAW row,
    never a viewer's view of it: R6-F9).
  * `viewer_json` is the login it was built for (viewer_ref: ids, role and
    grants — never a credential) and whether it may see the salaries, so the
    queue worker can rebuild it exactly; `opened_on` the last business date a
    request read it (the precompute builds what was opened lately).

NOTHING IS BUILT ON A REQUEST THREAD (R6-F4/F5). Every build runs in the
queue worker process (job_queue kind `home_snapshot`, group `home`: its own
CPU slots, per-tenant fairness, one running job a restaurant):

  1. the precompute (run_precompute, hourly from the scheduler): at
     HOME_PRECOMPUTE_HOUR local, before the owner wakes and after the 3am POS
     chain, one job per restaurant rebuilds every scope opened in the last
     HOME_PRECOMPUTE_ACTIVE_DAYS days — principals and managers alike, and
     the group view's location rows — so the first open of the day reads a
     row. (The morning brief's send keeps each recipient's brief too.)
  2. a read (serve) on a request:
       * a row for today → served as it is (one read of one row). Its stamp
         no longer matching (the owner's own write, a feed landing) → served
         with state "stale" and rebuilt at the interactive class: the route
         says `stale: true` and the client reads it again in `retry_after`
         seconds (R6-F6). Older than REFRESH_SECONDS with nothing moved →
         rebuilt at the bulk class, served as it is.
       * no row for today → a build is queued at the interactive class and
         the read raises SnapshotPreparing: the route answers
         `preparing: true` with `retry_after`, and the client reads it again.
     HOME_SNAPSHOT_COLD=request is the rollback: a missing row is built on
     the request (the FE-01 behaviour; the test suite runs this way, see
     tests/conftest.py, and the queue path has its own tests).
  3. a read off a request (a script, a test, a job) builds inline as before.

A queued rebuild is keyed on (restaurant, block, scope, day, stamp) — and the
REFRESH_SECONDS bucket for an age refresh, the hour for a cold build — so N
reads of one stale row queue one job; a job whose row is already current
when it runs does nothing.

What a stored answer must never carry past an owner's action is re-checked
at serve time by the routes: the brief's lines an answer silences
(morning_brief._drop_answered), the cross-module links answered since
(the route's presentation), the monthly review's per-login filters.

HOME_SNAPSHOTS=0 turns the store off (every read builds, as before).
"""
import hashlib
import json
import logging
import os
import threading
import time
import weakref

log = logging.getLogger("home_snapshots")

BLOCKS = ("morning_brief", "good_news", "monthly_review", "cross_module",
          # One location's row of the group view (home_brief._location_record,
          # FE-09): the same for every viewer.
          "location_record")
# The blocks built for one login (their scope is viewer_scope); the others
# are shared by every viewer with the same denied set / salary view.
PER_VIEWER = ("morning_brief", "cross_module")

# A kept answer older than this is rebuilt in the background on its next
# read even when no stamped input moved (what the stamp does not cover:
# a POS sync's intraday sales, the weather, the calendar).
REFRESH_SECONDS = int(os.getenv("HOME_SNAPSHOT_REFRESH_SECONDS", "900"))
# What a "preparing" / "stale" answer tells the client to wait before it
# reads again.
RETRY_AFTER_SECONDS = max(1, int(os.getenv("HOME_SNAPSHOT_RETRY_AFTER", "4")))
# The precompute: from this local hour (the 3am POS chain has landed) until
# PRECOMPUTE_UNTIL, once a business day per restaurant.
PRECOMPUTE_HOUR = int(os.getenv("HOME_PRECOMPUTE_HOUR", "5"))
PRECOMPUTE_UNTIL = PRECOMPUTE_HOUR + 3
# A scope opened within this many days is precomputed; one not opened for
# KEEP_DAYS is forgotten (the precompute prunes it).
ACTIVE_DAYS = int(os.getenv("HOME_PRECOMPUTE_ACTIVE_DAYS", "14"))
KEEP_DAYS = 45

_DDL = (
    """CREATE TABLE IF NOT EXISTS home_snapshots (
        restaurant_id INTEGER NOT NULL,
        block         TEXT NOT NULL,
        scope_key     TEXT NOT NULL,
        as_of         TEXT NOT NULL,
        stamp         TEXT NOT NULL,
        built_at      REAL NOT NULL,
        build_ms      INTEGER,
        payload       TEXT NOT NULL,
        viewer_json   TEXT,
        opened_on     TEXT,
        PRIMARY KEY (restaurant_id, block, scope_key))""",
)
# Columns added after the table shipped (R6-F4): migrated at boot.
_COLUMNS = (("viewer_json", "TEXT"), ("opened_on", "TEXT"))


def get_conn(db_path=None):
    """Resolved through models at call time (CLAUDE.md: never a bound copy)."""
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def enabled() -> bool:
    return os.getenv("HOME_SNAPSHOTS", "1").strip().lower() not in ("0", "false", "no", "off")


def cold_mode() -> str:
    """What a request does when nothing is kept for today: "queue" (the
    default — queue the build and answer preparing) or "request" (build it
    on the request: the FE-01 behaviour, kept as the rollback)."""
    return "request" if os.getenv("HOME_SNAPSHOT_COLD", "queue").strip().lower() == "request" else "queue"


def init_home_snapshots(db_path=None):
    """The table and its later columns — at boot (models.init_db), never on
    a request."""
    import models
    conn = get_conn(db_path)
    try:
        for stmt in _DDL:
            conn.execute(stmt)
        conn.commit()
        for col, typ in _COLUMNS:
            if models._apply_migration(conn, f"ALTER TABLE home_snapshots ADD COLUMN {col} {typ}"):
                conn.commit()
        # The precompute's read: the restaurants with a scope opened lately.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_home_snapshots_opened ON home_snapshots(opened_on, restaurant_id)")
        conn.commit()
    finally:
        conn.close()


# ── keys ────────────────────────────────────────────────────────────────────

def _digest(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:20]


def viewer_scope(user) -> str:
    """Who an answer built for `user` is for: the login, the role and grants
    its permissions come from, whether it is an admin or a view-as session,
    and whose answers it records (permissions.answer_authority). Two logins
    never share a row; a login whose role or grants change gets a new one."""
    u = user or {}
    try:
        from permissions import answer_authority, normalize_role
        authority = answer_authority(u)
        role = normalize_role(u.get("role"))
    except Exception:
        authority, role = "?", str(u.get("role") or "")
    return "v:" + _digest({
        "id": u.get("id"), "role": role, "grants": sorted(str(g) for g in (u.get("grants") or ())),
        "admin": bool(u.get("is_admin")), "acting": u.get("acting_admin_id"),
        "acting_role": u.get("acting_admin_role"), "device": u.get("device_type") == "admin-view-as",
        "authority": authority})


# What a rebuild off the request needs of the login it is for: what
# permissions, view-as and the builders read — never a credential, an email
# or a phone (the row and the queue's payload keep it).
_VIEWER_FIELDS = ("id", "restaurant_id", "base_restaurant_id", "active_restaurant_id", "role", "is_admin",
                  "grants", "device_type", "acting_admin_id", "acting_admin", "acting_admin_role",
                  "view_as_read_only", "view_as_full_control", "membership_id", "employee_name",
                  "username", "can_manage_team")


def viewer_ref(user):
    """The login `user` as a rebuild needs it (a current_user-shaped dict of
    _VIEWER_FIELDS), or None."""
    if not isinstance(user, dict):
        return None
    out = {k: user.get(k) for k in _VIEWER_FIELDS if k in user}
    if "grants" in out:
        out["grants"] = sorted(str(g) for g in (out["grants"] or ()))
    return out


def salary_scope() -> str:
    """Whether this read may carry the salaries (models.viewer_sees_salaries:
    the account holder's, decided from the request's login): an all-in labor
    figure in a build gives them away, so it is part of every shared scope."""
    try:
        from models import viewer_sees_salaries
        return "s1" if viewer_sees_salaries() else "s0"
    except Exception:
        return "s0"


def denied_scope(denied) -> str:
    """An answer that depends only on the modules a viewer may not see (and
    whether it may see the salaries)."""
    return "d:" + (",".join(sorted(str(d) for d in (denied or ()))) or "-") + ":" + salary_scope()


def shared_scope() -> str:
    """An answer the same for every viewer but for the salaries."""
    return "all:" + salary_scope()


def _salary_for(scope_key, viewer) -> bool:
    """The salary view a rebuild of `scope_key` runs under: the one written
    into a shared scope's key, else the login's own (permissions.sees_salaries:
    the account holder, never support acting through view-as — B3 10/26/26)."""
    s = str(scope_key or "")
    if s.endswith(":s1"):
        return True
    if s.endswith(":s0"):
        return False
    try:
        from permissions import sees_salaries
        return bool(sees_salaries(viewer or {}))
    except Exception as e:
        logging.getLogger(__name__).warning(f"[home_snapshots] salary view unreadable, built hourly: {e!r}")
        return False


# Restaurant fields that move without the Home answers moving (activity
# stamps): left out of the settings fingerprint, or every tab switch would
# miss. A timestamp field is a data feed's, and REFRESH_SECONDS covers it.
def _volatile(field) -> bool:
    return field.startswith("last_") or field.endswith("_at")


def _restaurant_fingerprint(restaurant):
    if restaurant is None:
        return None
    try:
        import dataclasses
        data = dataclasses.asdict(restaurant)
    except Exception:
        data = dict(getattr(restaurant, "__dict__", {}) or {})
    return _digest({k: v for k, v in data.items() if not _volatile(str(k)) and not str(k).startswith("_")})


# Each is one indexed read for one restaurant. A failing read stamps "?"
# (the store then keys on what it could read; REFRESH_SECONDS bounds the rest).
# The close-out, schedule drafts and Sends and inventory counts joined
# 10/20/26 (R6-F6): a saved close-out or a Send waited out the 15 minutes.
_STAMP_SQL = (
    ("silences", "SELECT COUNT(*), MAX(id), MAX(until) FROM rec_silences WHERE restaurant_id=?"),
    ("issues", "SELECT status, COUNT(*), MAX(id), MAX(resolved_at), MAX(acknowledged_at) FROM ops_issues "
               "WHERE restaurant_id=? GROUP BY status ORDER BY status"),
    ("goals", "SELECT status, COUNT(*), MAX(id) FROM owner_goals WHERE restaurant_id=? GROUP BY status ORDER BY status"),
    ("outcomes", "SELECT status, COUNT(*), MAX(id) FROM recommendation_outcomes WHERE restaurant_id=? "
                 "GROUP BY status ORDER BY status"),
    ("dsr", "SELECT COUNT(*), MAX(id), MAX(finalized_at) FROM dsr_reports WHERE restaurant_id=?"),
    ("reviews", "SELECT response_status, COUNT(*), MAX(id) FROM reviews WHERE restaurant_id=? "
                "GROUP BY response_status ORDER BY response_status"),
    ("shifts", "SELECT updated_at, shifts_stale FROM client_data WHERE restaurant_id=?"),
    ("closeout", "SELECT COUNT(*), MAX(id), MAX(created_at) FROM close_outs WHERE restaurant_id=?"),
    ("schedules", "SELECT COUNT(*), MAX(id), MAX(published_at), MAX(republished_at) FROM schedule_history "
                  "WHERE restaurant_id=?"),
    ("inventory", "SELECT COUNT(*), MAX(id) FROM inventory_history WHERE restaurant_id=?"),
)


def _raw_restaurant(restaurant_id, restaurant=None, db_path=None):
    """The restaurant's own row — never a viewer's view of it, whose module
    flags are zeroed for what the login may not see (R6-F9: the web stored
    good news under the view's stamp and the phone checked it against the
    raw row's, so a manager's rows rebuilt on every alternation). The
    request's memo makes this a dict lookup on a request."""
    try:
        from models import get_restaurant
        raw = get_restaurant(int(restaurant_id), db_path=db_path) if db_path else get_restaurant(int(restaurant_id))
        if raw is not None:
            return raw
    except Exception:
        pass
    return restaurant


def data_stamp(restaurant_id, restaurant=None, db_path=None) -> str:
    """The data version the kept answers are checked against. Memoised for
    the request (a Home read asks for it once per block) on the restaurant
    and database only: whatever object a route passes, the fingerprint is
    the raw row's, so every block and both clients agree (R6-F9)."""
    memo, mkey = None, (int(restaurant_id), db_path)
    try:
        from flask import g, has_request_context
        if has_request_context():
            memo = g.setdefault("_home_snapshot_stamps", {})
            if mkey in memo:
                return memo[mkey]
    except Exception:
        memo = None
    parts = []
    conn = get_conn(db_path)
    try:
        for name, sql in _STAMP_SQL:
            try:
                parts.append([name, [list(r) for r in conn.execute(sql, (int(restaurant_id),)).fetchall()]])
            except Exception:
                parts.append([name, "?"])
                try:
                    conn.rollback()           # Postgres: a failed read aborts the transaction
                except Exception:
                    pass
    finally:
        conn.close()
    parts.append(["settings", _restaurant_fingerprint(_raw_restaurant(restaurant_id, restaurant, db_path))])
    stamp = _digest(parts)
    if memo is not None:
        memo[mkey] = stamp
    return stamp


# ── the store ───────────────────────────────────────────────────────────────

def _read(restaurant_id, block, scope_key, db_path=None):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT as_of, stamp, built_at, payload, viewer_json, opened_on FROM home_snapshots "
                           "WHERE restaurant_id=? AND block=? AND scope_key=?",
                           (int(restaurant_id), block, scope_key)).fetchone()
        return dict(row) if row else None
    except Exception as e:
        log.warning("home snapshot unreadable rid=%s %s: %s", restaurant_id, block, e)
        return None
    finally:
        conn.close()


def _encode(payload) -> str:
    # Flask's own encoder (dates as the routes' jsonify writes them), so a
    # kept answer reads exactly as the built one did.
    try:
        from flask import json as _fjson
        return _fjson.dumps(payload)
    except Exception:
        return json.dumps(payload, default=str)


def _viewer_json(viewer, scope_key):
    ref = viewer_ref(viewer)
    try:
        return json.dumps({"viewer": ref, "salary": _salary_for(scope_key, ref)}, default=str)
    except Exception:
        return None


def _write(restaurant_id, block, scope_key, as_of, stamp, payload, build_ms, db_path=None, viewer=None,
           opened_on=None):
    """Keep one answer. A cache of a pure build: a failed write is logged and
    the caller still has its answer."""
    try:
        text = _encode(payload)
    except Exception as e:
        log.warning("home snapshot not encodable rid=%s %s: %s", restaurant_id, block, e)
        return False
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO home_snapshots (restaurant_id, block, scope_key, as_of, stamp, built_at, build_ms, payload, "
            "viewer_json, opened_on) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(restaurant_id, block, scope_key) DO UPDATE SET "
            "as_of=excluded.as_of, stamp=excluded.stamp, built_at=excluded.built_at, "
            "build_ms=excluded.build_ms, payload=excluded.payload, "
            "viewer_json=COALESCE(excluded.viewer_json, home_snapshots.viewer_json), "
            "opened_on=COALESCE(excluded.opened_on, home_snapshots.opened_on)",
            (int(restaurant_id), block, scope_key, str(as_of), stamp, time.time(), int(build_ms), text,
             _viewer_json(viewer, scope_key) if viewer is not None else None, opened_on))
        conn.commit()
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning("home snapshot not kept rid=%s %s: %s", restaurant_id, block, e)
        return False
    finally:
        conn.close()


def _mark_opened(restaurant_id, block, scope_key, as_of, db_path=None):
    """A request read this row today: once a business day per row, so the
    precompute knows which scopes are in use."""
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE home_snapshots SET opened_on=? WHERE restaurant_id=? AND block=? AND scope_key=? "
                     "AND (opened_on IS NULL OR opened_on<>?)",
                     (str(as_of), int(restaurant_id), block, scope_key, str(as_of)))
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning("home snapshot open not noted rid=%s %s: %s", restaurant_id, block, e)
    finally:
        conn.close()


def invalidate(restaurant_id=None, db_path=None) -> int:
    """Forget kept answers (one restaurant's, or all): the next read builds."""
    conn = get_conn(db_path)
    try:
        if restaurant_id is None:
            cur = conn.execute("DELETE FROM home_snapshots")
        else:
            cur = conn.execute("DELETE FROM home_snapshots WHERE restaurant_id=?", (int(restaurant_id),))
        conn.commit()
        return cur.rowcount or 0
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        conn.close()


# ── the builders (one per block; the routes and the queue worker share them) ─

def _today(as_of):
    from datetime import date
    return as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of)[:10])


def build_block(block, restaurant_id, viewer, as_of, ctx=None):
    """`block`'s answer for `viewer` on business date `as_of` — what its
    route keeps. Runs wherever it is called; the salary view is the caller's
    (a request's login, or models.salary_view in the queue worker)."""
    rid = int(restaurant_id)
    today = _today(as_of)
    from models import get_restaurant
    if block == "morning_brief":
        import morning_brief
        return morning_brief.build(rid, restaurant=get_restaurant(rid), today=today, viewer=viewer)
    if block == "good_news":
        import good_news
        from ask_cavnar_tools import viewer_restaurant
        view = viewer_restaurant(get_restaurant(rid), viewer)
        denied = set(getattr(view, "_ask_denied", frozenset()))
        return good_news.all_good_news(rid, today=today, restaurant=view, denied_modules=denied)
    if block == "monthly_review":
        import monthly_review
        return monthly_review.build(rid, today=today, restaurant=get_restaurant(rid))
    if block == "cross_module":
        import business_intelligence as bi
        from ask_cavnar_tools import viewer_restaurant
        r = viewer_restaurant(get_restaurant(rid), viewer)
        if ctx is None:
            try:
                import rec_trust
                ctx = rec_trust.Context(rid)
            except Exception as e:
                log.warning("cross-module trust context unavailable rid=%s: %s", rid, e)
        try:
            from permissions import answer_authority
            log_rank = answer_authority(viewer) == "principal"
        except Exception:
            log_rank = False
        return bi.executive_brief(rid, restaurant=r, ctx=ctx, viewer=viewer, log_rank=log_rank)
    if block == "location_record":
        import home_brief
        return home_brief.build_location_record(rid)
    raise ValueError(f"unknown Home block {block!r}")


# ── building: in the queue worker; inline only off a request ─────────────────

class _KeyLock:
    """A lock that can be held weakly: the table below forgets a key's lock
    only once nobody holds or waits on it."""
    __slots__ = ("lock", "__weakref__")

    def __init__(self):
        self.lock = threading.Lock()

    def __enter__(self):
        self.lock.acquire()
        return self

    def __exit__(self, *exc):
        self.lock.release()
        return False


# key -> its lock, while anyone holds a reference (R6-F12: the table used to
# be cleared whole past 5,000 keys while builds held their locks, so a
# second request built the same key beside the first).
_key_locks = weakref.WeakValueDictionary()
_key_locks_guard = threading.Lock()


def _key_lock(key):
    with _key_locks_guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = _KeyLock()
            _key_locks[key] = lock
        return lock


def _build_and_keep(restaurant_id, block, scope_key, as_of, stamp, build, db_path=None, viewer=None,
                    opened_on=None):
    t0 = time.monotonic()
    payload = build()
    _write(restaurant_id, block, scope_key, as_of, stamp, payload, (time.monotonic() - t0) * 1000, db_path,
           viewer=viewer, opened_on=opened_on)
    return payload


def _on_request() -> bool:
    try:
        from flask import has_request_context
        return bool(has_request_context())
    except Exception:
        return False


def _enqueue(restaurant_id, block, scope_key, as_of, stamp, viewer, klass, bucket, db_path=None) -> bool:
    """One queued (re)build of a key — the same key, stamp and bucket queue
    one job however many reads ask. Never raises."""
    try:
        import job_queue
        ref = viewer_ref(viewer)
        idem = f"home:{int(restaurant_id)}:{block}:{scope_key}:{as_of}:{stamp}:{bucket}"
        job_queue.enqueue("home_snapshot", {"block": block, "scope_key": scope_key, "as_of": str(as_of),
                                            "viewer": ref},
                          restaurant_id=int(restaurant_id), priority_class=klass, idempotency_key=idem,
                          # A finished row under the same key is queued again: the read
                          # that asks found the row still missing or stale.
                          requeue_states=("done", "expired"), db_path=db_path)
        return True
    except Exception as e:
        log.warning("home snapshot build not queued rid=%s %s: %s", restaurant_id, block, e)
        return False


def _start_refresh(key, restaurant_id, block, scope_key, as_of, stamp, viewer, moved, db_path=None) -> bool:
    """A kept row needs rebuilding: queued — at the interactive class when
    its stamp moved (an owner is looking at an answer their own write made
    stale), else at the bulk class in its REFRESH_SECONDS bucket."""
    klass = "interactive" if moved else "bulk"
    bucket = "moved" if moved else f"age{int(time.time() // max(1, REFRESH_SECONDS))}"
    return _enqueue(restaurant_id, block, scope_key, as_of, stamp, viewer, klass, bucket, db_path)


class SnapshotMiss(Exception):
    """Nothing kept for this key today, and the caller asked not to build
    (kept_only): Home's combined read names the block in `missing`."""


class SnapshotPreparing(SnapshotMiss):
    """Nothing kept for this key today; its build is queued (R6-F4). The
    route answers `preparing: true` and the client reads again in
    RETRY_AFTER_SECONDS."""


_kept_only = threading.local()
_states = threading.local()


class kept_only:
    """`with kept_only(): ...` — serve() raises SnapshotMiss instead of
    building (strategy_routes._do_home_follow); on a request the build is
    queued first."""

    def __enter__(self):
        self._was = getattr(_kept_only, "on", False)
        _kept_only.on = True
        return self

    def __exit__(self, *exc):
        _kept_only.on = self._was
        return False


def _note(block, state):
    d = getattr(_states, "d", None)
    if d is None:
        d = _states.d = {}
    d[block] = state


def state(block):
    """What the last serve() of `block` on this thread did: "kept" (a
    current row), "stale" (a row whose inputs moved since — rebuilding),
    "preparing" (none yet — building) or "built" (built here)."""
    return (getattr(_states, "d", None) or {}).get(block)


def flags(block) -> dict:
    """The fields a route adds to its answer for `block`'s last serve:
    {"stale": True, "retry_after": n} or {"preparing": True, ...} or {}."""
    s = state(block)
    if s in ("stale", "preparing"):
        return {s: True, "retry_after": RETRY_AFTER_SECONDS}
    return {}


def keep(block, restaurant_id, scope_key, as_of, payload, restaurant=None, db_path=None, viewer=None) -> bool:
    """Keep an answer something else already built for this scope today —
    the morning brief's send builds each recipient's brief before they open
    Home, so their first open reads it (the precompute). Never raises."""
    if not enabled():
        return False
    try:
        stamp = data_stamp(restaurant_id, restaurant, db_path)
        return _write(restaurant_id, block, scope_key, str(as_of), stamp, payload, 0, db_path, viewer=viewer)
    except Exception as e:
        log.warning("home snapshot not kept rid=%s %s: %s", restaurant_id, block, e)
        return False


def serve(block, restaurant_id, scope_key, as_of, build, restaurant=None, db_path=None, fresh=False, viewer=None):
    """The kept answer for (restaurant, block, scope) today.

    On a request nothing is built (R6-F4): a missing row queues its build
    and raises SnapshotPreparing, a stale one is served and queued (state()
    says which; flags() is what the route adds). Off a request — or with
    HOME_SNAPSHOT_COLD=request — a missing row is built here by `build`, a
    no-argument callable, once per key. `viewer` is the login the answer is
    for (what the queued build runs as). Returns a fresh decoded copy each
    call (callers may change it). `fresh` (the client's ?fresh=1) rebuilds
    whatever is kept: queued on a request, inline off one."""
    _note(block, "kept")
    if not enabled():
        if getattr(_kept_only, "on", False):
            raise SnapshotMiss(block)
        _note(block, "built")
        return build()
    as_of = str(as_of)
    on_request = _on_request()
    queue_it = on_request and cold_mode() == "queue"
    kept = getattr(_kept_only, "on", False)
    stamp = data_stamp(restaurant_id, restaurant, db_path)
    key = (int(restaurant_id), block, scope_key, db_path)
    opened = as_of if on_request else None
    if fresh and not kept:
        if not queue_it:
            _note(block, "built")
            payload = _build_and_keep(restaurant_id, block, scope_key, as_of, stamp, build, db_path,
                                      viewer=viewer, opened_on=opened)
            return json.loads(_encode(payload))
        _enqueue(restaurant_id, block, scope_key, as_of, stamp, viewer, "interactive",
                 f"fresh{int(time.time() // 60)}", db_path)
        row = _read(restaurant_id, block, scope_key, db_path)
        if row is None or row["as_of"] != as_of:
            _note(block, "preparing")
            raise SnapshotPreparing(block)
        _note(block, "stale")
        return json.loads(row["payload"])
    row = _read(restaurant_id, block, scope_key, db_path)
    if row is None or row["as_of"] != as_of:
        if kept or queue_it:
            if queue_it:
                # The build, queued at the interactive class: an owner waits.
                # (HOME_SNAPSHOT_COLD=request: Home's combined read builds a
                # missing block when the client asks for it by name.)
                _enqueue(restaurant_id, block, scope_key, as_of, stamp, viewer, "interactive",
                         f"h{int(time.time() // 3600)}", db_path)
            _note(block, "preparing")
            raise (SnapshotMiss(block) if kept else SnapshotPreparing(block))
        with _key_lock(key):
            # A request that waited on another's build reads its row.
            row = _read(restaurant_id, block, scope_key, db_path)
            if row is None or row["as_of"] != as_of:
                _note(block, "built")
                payload = _build_and_keep(restaurant_id, block, scope_key, as_of, stamp, build, db_path,
                                          viewer=viewer, opened_on=opened)
                return json.loads(_encode(payload))
    moved = row["stamp"] != stamp
    if moved or time.time() - float(row["built_at"] or 0) > REFRESH_SECONDS:
        who = viewer if viewer is not None else _stored_viewer(row)
        _start_refresh(key, restaurant_id, block, scope_key, as_of, stamp, who, moved, db_path)
        if moved:
            _note(block, "stale")
    if on_request and row.get("opened_on") != as_of:
        _mark_opened(restaurant_id, block, scope_key, as_of, db_path)
    return json.loads(row["payload"])


def _stored_viewer(row):
    try:
        return (json.loads(row.get("viewer_json") or "null") or {}).get("viewer")
    except Exception:
        return None


# ── the queue worker's side ──────────────────────────────────────────────────

def _business_today(restaurant_id):
    from time_utils import restaurant_now_by_id
    return restaurant_now_by_id(int(restaurant_id), naive=True).date().isoformat()


def rebuild(restaurant_id, block, scope_key, as_of, viewer=None, since=None, db_path=None) -> str:
    """Build and keep one key for business date `as_of`, as `viewer` would
    read it (their salary view: models.salary_view). "skipped" when the day
    has turned or the row is already current (another job, or the morning
    send, kept it after `since`); else "built"."""
    import models
    rid = int(restaurant_id)
    if str(as_of) != _business_today(rid):
        return "skipped"
    stamp = data_stamp(rid, None, db_path)
    row = _read(rid, block, scope_key, db_path)
    if row and row["as_of"] == str(as_of) and row["stamp"] == stamp and \
            (since is None or float(row["built_at"] or 0) >= float(since)):
        return "skipped"
    if block in PER_VIEWER and not viewer:
        raise ValueError(f"a {block} rebuild needs the login it is for")
    with models.salary_view(_salary_for(scope_key, viewer)):
        _build_and_keep(rid, block, scope_key, str(as_of), stamp,
                        lambda: build_block(block, rid, viewer, as_of), db_path, viewer=viewer)
    return "built"


def run_queued(job):
    """job_queue handler for `home_snapshot` rows: one key's (re)build, or a
    restaurant's precompute (`mode: precompute`)."""
    p = job.get("payload") or {}
    rid = job.get("restaurant_id")
    if p.get("mode") == "precompute":
        return precompute_restaurant(rid, p.get("as_of"))
    out = rebuild(rid, p["block"], p["scope_key"], p["as_of"], viewer=p.get("viewer"),
                  since=job.get("enqueued_at"))
    return {"result": out}


def _still_active(conn, viewer) -> bool:
    """The login a stored row was built for still signs in (an admin's
    view-as is never precomputed: it is a support session, not a morning)."""
    if not viewer or viewer.get("acting_admin_id") or viewer.get("device_type") == "admin-view-as":
        return False
    try:
        row = conn.execute("SELECT COALESCE(is_active, 1) AS a FROM users WHERE id=?",
                           (int(viewer.get("id") or 0),)).fetchone()
        return bool(row and row["a"])
    except Exception:
        return False


def precompute_restaurant(restaurant_id, as_of=None, db_path=None) -> dict:
    """Every scope of this restaurant opened in the last ACTIVE_DAYS days,
    built for `as_of` (its business date) before anyone opens Home — the
    owner's and each manager's brief and links, each good-news view, the
    monthly review in each salary view, and its group-view row. Standard
    counts; a scope that fails is counted and the rest go on."""
    from datetime import date, timedelta
    rid = int(restaurant_id)
    as_of = str(as_of or _business_today(rid))
    since_day = (date.fromisoformat(as_of) - timedelta(days=ACTIVE_DAYS)).isoformat()
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT block, scope_key, as_of, viewer_json FROM home_snapshots WHERE restaurant_id=? "
            "AND opened_on IS NOT NULL AND opened_on>=? ORDER BY block, scope_key", (rid, since_day)).fetchall()]
        todo = []
        for r in rows:
            viewer = _stored_viewer(r)
            if r["block"] in PER_VIEWER and not _still_active(conn, viewer):
                out["skipped"] += 1
                continue
            todo.append((r["block"], r["scope_key"], viewer))
    finally:
        conn.close()
    # The brief first: it is what the morning open leads with.
    order = {b: i for i, b in enumerate(BLOCKS)}
    for block, scope_key, viewer in sorted(todo, key=lambda t: order.get(t[0], 99)):
        out["attempted"] += 1
        try:
            if rebuild(rid, block, scope_key, as_of, viewer=viewer, db_path=db_path) == "built":
                out["ok"] += 1
            else:
                out["skipped"] += 1
        except Exception as e:
            out["failed"] += 1
            try:
                import ops
                ops.capture(e, job="home_precompute", context=f"restaurant_id={rid} block={block}")
            except Exception:
                log.warning("home precompute failed rid=%s %s: %s", rid, block, e)
    return out


def run_precompute(now=None, db_path=None) -> dict:
    """The hourly scheduler job: one queued precompute per restaurant whose
    local clock is in [PRECOMPUTE_HOUR, PRECOMPUTE_UNTIL) — keyed on its
    business date, so a restaurant is queued once a day however many hours
    the window spans — and the scopes nobody opened for KEEP_DAYS forgotten.
    Only restaurants with a Home scope opened in the last ACTIVE_DAYS are
    read (an index range on opened_on), so the pass is bounded by use, not
    by the fleet. Standard counts."""
    from datetime import datetime, timedelta, timezone
    from time_utils import restaurant_tz
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    if not enabled():
        out["skipped"] = 1
        return out
    now = now or datetime.now(timezone.utc)
    floor = (now - timedelta(days=ACTIVE_DAYS + 1)).date().isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT r.id AS id, r.timezone AS tz FROM restaurants r WHERE r.id IN "
            "(SELECT DISTINCT restaurant_id FROM home_snapshots WHERE opened_on>=?)", (floor,)).fetchall()
        tz_by_id = {int(r["id"]): r["tz"] for r in rows}
    finally:
        conn.close()
    items = []
    for rid, tz in sorted(tz_by_id.items()):
        try:
            local = now.astimezone(restaurant_tz(tz))
        except Exception:
            out["failed"] += 1
            continue
        if not (PRECOMPUTE_HOUR <= local.hour < PRECOMPUTE_UNTIL):
            continue
        day = local.date().isoformat()
        out["attempted"] += 1
        items.append({"payload": {"mode": "precompute", "as_of": day}, "restaurant_id": rid,
                      "idempotency_key": f"home_pre:{rid}:{day}",
                      # Not started by the end of the window: the day's opens
                      # build on their own.
                      "deadline": time.time() + 3 * 3600})
    if items:
        try:
            import job_queue
            created = job_queue.enqueue_many("home_snapshot", items, priority_class="bulk", db_path=db_path)
            out["ok"] += created
            out["skipped"] += len(items) - created
        except Exception as e:
            out["failed"] += len(items)
            log.warning("home precompute not queued: %s", e)
    prune(db_path=db_path, now=now)
    return out


def prune(db_path=None, now=None) -> int:
    """Forget the rows nobody opened for KEEP_DAYS (a login removed, a role
    changed: a scope no request will ask for again). Returns how many."""
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=KEEP_DAYS)).date().isoformat()
    built_cutoff = time.time() - KEEP_DAYS * 86400
    conn = get_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM home_snapshots WHERE (opened_on IS NOT NULL AND opened_on<?) "
                           "OR (opened_on IS NULL AND built_at<?)", (cutoff, built_cutoff))
        conn.commit()
        return cur.rowcount or 0
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning("home snapshots not pruned: %s", e)
        return 0
    finally:
        conn.close()
