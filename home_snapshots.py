"""
home_snapshots.py — Home's slow blocks kept as per-restaurant read models
(scale audit 10/19/26, FE-01).

Four of Home's reads were full builds on every load, on a request thread:
/morning-brief (13.4 s average in production), /good-news (11.1 s),
/monthly-review (5.8 s) and /cross-module (3.3 s) — about 33 s of the web
process's four request threads per Home open, and the iPhone's Home ran the
same reads (the bodies are shared by the web and /mobile/api twins). None of
them calls a model; they are deterministic reads over a restaurant's data,
so the answer is the same until the data moves.

Each is now kept in `home_snapshots`, one row per (restaurant, block, scope):

  * the SCOPE is who the answer is for. The morning brief and the
    cross-module links are built for one login (their silences, their
    learned ranking, what their role may see), so their scope is the login's
    identity and permissions (viewer_scope); good news depends only on the
    modules the viewer is denied; the monthly review's build is the same
    for every viewer (the route filters it per login afterwards).
  * `as_of` is the restaurant's business date: a row from another day is
    never served — the first open of a day builds (once per scope).
  * `stamp` is the data version (data_stamp): cheap indexed reads of what an
    owner's actions and the day's feeds change — answers and silences,
    issues, goals, tracked results, the nightly report, reviews and their
    replies, the shift history's stamp — and the restaurant's settings.

A read (serve):
  1. a row for today → served as it is (a read of one row);
     when its stamp no longer matches, or it is older than REFRESH_SECONDS,
     it is rebuilt OFF the request — on this module's small pool, one
     rebuild per key at a time — and the next open reads the new one;
  2. no row for today → built on the request (once: a second request for
     the same key waits for the first build and reads its row) and kept.

What a stored answer must never carry past an owner's action is re-checked
at serve time by the routes: the brief's lines an answer silences
(morning_brief._drop_answered), the cross-module links answered since
(the route's presentation), the monthly review's per-login filters.

HOME_SNAPSHOTS=0 turns the store off (every read builds, as before).
The pool is per process (HOME_SNAPSHOT_WORKERS): it is one of the bounded
pools CLAUDE.md lists for --workers > 1.
"""
import concurrent.futures
import hashlib
import json
import logging
import os
import threading
import time

log = logging.getLogger("home_snapshots")

BLOCKS = ("morning_brief", "good_news", "monthly_review", "cross_module",
          # One location's row of the group view (home_brief._location_record,
          # FE-09): the same for every viewer.
          "location_record")

# A kept answer older than this is rebuilt in the background on its next
# read even when no stamped input moved (what the stamp does not cover:
# a POS sync's intraday sales, the weather, the calendar).
REFRESH_SECONDS = int(os.getenv("HOME_SNAPSHOT_REFRESH_SECONDS", "900"))
# The background rebuild pool, and how many rebuilds may wait for it
# before a stale read is simply served again without one.
WORKERS = max(1, int(os.getenv("HOME_SNAPSHOT_WORKERS", "2")))
MAX_PENDING = int(os.getenv("HOME_SNAPSHOT_MAX_PENDING", "200"))

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
        PRIMARY KEY (restaurant_id, block, scope_key))""",
)


def get_conn(db_path=None):
    """Resolved through models at call time (CLAUDE.md: never a bound copy)."""
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def enabled() -> bool:
    return os.getenv("HOME_SNAPSHOTS", "1").strip().lower() not in ("0", "false", "no", "off")


def init_home_snapshots(db_path=None):
    """The table — at boot (models.init_db), never on a request."""
    conn = get_conn(db_path)
    try:
        for stmt in _DDL:
            conn.execute(stmt)
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
    return _digest({k: v for k, v in data.items() if not _volatile(str(k))})


# Each is one indexed read for one restaurant. A failing read stamps "?"
# (the store then keys on what it could read; REFRESH_SECONDS bounds the rest).
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
)


def data_stamp(restaurant_id, restaurant=None, db_path=None) -> str:
    """The data version the kept answers are checked against. Memoised for
    the request (a Home read asks for it once per block)."""
    memo = None
    try:
        from flask import g, has_request_context
        if has_request_context():
            memo = g.setdefault("_home_snapshot_stamps", {})
            if restaurant_id in memo:
                return memo[restaurant_id]
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
    finally:
        conn.close()
    parts.append(["settings", _restaurant_fingerprint(restaurant)])
    stamp = _digest(parts)
    if memo is not None:
        memo[restaurant_id] = stamp
    return stamp


# ── the store ───────────────────────────────────────────────────────────────

def _read(restaurant_id, block, scope_key, db_path=None):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT as_of, stamp, built_at, payload FROM home_snapshots "
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


def _write(restaurant_id, block, scope_key, as_of, stamp, payload, build_ms, db_path=None):
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
            "INSERT INTO home_snapshots (restaurant_id, block, scope_key, as_of, stamp, built_at, build_ms, payload) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(restaurant_id, block, scope_key) DO UPDATE SET "
            "as_of=excluded.as_of, stamp=excluded.stamp, built_at=excluded.built_at, "
            "build_ms=excluded.build_ms, payload=excluded.payload",
            (int(restaurant_id), block, scope_key, str(as_of), stamp, time.time(), int(build_ms), text))
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


# ── building: once per key on a request, off the request when stale ─────────

_key_locks = {}
_key_locks_guard = threading.Lock()
_refreshing = set()
_refresh_guard = threading.Lock()
_pool = None
_pool_guard = threading.Lock()


def _key_lock(key):
    with _key_locks_guard:
        lock = _key_locks.get(key)
        if lock is None:
            if len(_key_locks) > 5000:
                _key_locks.clear()
            lock = _key_locks[key] = threading.Lock()
        return lock


def _executor():
    global _pool
    with _pool_guard:
        if _pool is None:
            _pool = concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS,
                                                          thread_name_prefix="home-snapshot")
        return _pool


def _build_and_keep(restaurant_id, block, scope_key, as_of, stamp, build, db_path=None):
    t0 = time.monotonic()
    payload = build()
    _write(restaurant_id, block, scope_key, as_of, stamp, payload, (time.monotonic() - t0) * 1000, db_path)
    return payload


def _request_state():
    """What a rebuild off the request needs to read exactly as the request
    did: the app, the request's path and query, and its flask.g (the login
    — models.viewer_sees_salaries, permissions' view-as — and the request's
    memos). None outside a request."""
    try:
        from flask import current_app, g, has_request_context, request
        if not has_request_context():
            return None
        state = {k: v for k, v in g.__dict__.items() if not k.startswith("_inflight")}
        return (current_app._get_current_object(), request.path,
                (request.query_string or b"").decode("utf-8", "replace"), state)
    except Exception:
        return None


def _refresh(key, restaurant_id, block, scope_key, as_of, stamp, build, db_path, req=None):
    """The rebuild, on the pool. Started from a request, it runs inside a
    request context made like that one, with its flask.g: everything the
    build reads from the request (whose salaries, view-as, and that it may
    not wait on a provider — rpower.on_request_thread) reads as it did on the
    request, so the kept answer is the one the request would have built."""
    try:
        if req is not None:
            app, path, query, state = req
            from flask import g
            with app.test_request_context(path, query_string=query):
                g.__dict__.update(state)
                _build_and_keep(restaurant_id, block, scope_key, as_of, stamp, build, db_path)
        else:
            _build_and_keep(restaurant_id, block, scope_key, as_of, stamp, build, db_path)
    except Exception as e:
        try:
            import ops
            ops.capture(e, job=f"home_snapshot:{block}", restaurant_id=restaurant_id)
        except Exception:
            log.warning("home snapshot refresh failed rid=%s %s: %s", restaurant_id, block, e)
    finally:
        with _refresh_guard:
            _refreshing.discard(key)


def _start_refresh(key, restaurant_id, block, scope_key, as_of, stamp, build, db_path=None) -> bool:
    """One background rebuild per key; none while MAX_PENDING already wait
    (the stale answer is served again and the next read tries)."""
    with _refresh_guard:
        if key in _refreshing or len(_refreshing) >= MAX_PENDING:
            return False
        _refreshing.add(key)
    try:
        _executor().submit(_refresh, key, restaurant_id, block, scope_key, as_of, stamp, build, db_path,
                           _request_state())
        return True
    except Exception as e:
        with _refresh_guard:
            _refreshing.discard(key)
        log.warning("home snapshot refresh not started rid=%s %s: %s", restaurant_id, block, e)
        return False


class SnapshotMiss(Exception):
    """Nothing kept for this key today, and the caller asked not to build
    (kept_only): Home's combined read leaves the block for the client to
    read on its own, in parallel with the others."""


_kept_only = threading.local()


class kept_only:
    """`with kept_only(): ...` — serve() raises SnapshotMiss instead of
    building on the request (strategy_routes._do_home_follow)."""

    def __enter__(self):
        self._was = getattr(_kept_only, "on", False)
        _kept_only.on = True
        return self

    def __exit__(self, *exc):
        _kept_only.on = self._was
        return False


def keep(block, restaurant_id, scope_key, as_of, payload, restaurant=None, db_path=None) -> bool:
    """Keep an answer something else already built for this scope today —
    the morning brief's send builds each recipient's brief before they open
    Home, so their first open reads it (the precompute). Never raises."""
    if not enabled():
        return False
    try:
        stamp = data_stamp(restaurant_id, restaurant, db_path)
        return _write(restaurant_id, block, scope_key, str(as_of), stamp, payload, 0, db_path)
    except Exception as e:
        log.warning("home snapshot not kept rid=%s %s: %s", restaurant_id, block, e)
        return False


def serve(block, restaurant_id, scope_key, as_of, build, restaurant=None, db_path=None, fresh=False):
    """The kept answer for (restaurant, block, scope) today, or a new build.

    `build` is a no-argument callable returning a JSON-able answer; it runs
    on the request when nothing is kept for `as_of` (once per key), else on
    the pool when the kept one is stale. Returns a fresh decoded copy each
    call (callers may change it). Never raises past `build`'s own errors.
    `fresh` (an explicit refresh: the client's ?fresh=1) builds on the
    request and keeps the result, whatever is kept."""
    if not enabled():
        if getattr(_kept_only, "on", False):
            raise SnapshotMiss(block)
        return build()
    as_of = str(as_of)
    stamp = data_stamp(restaurant_id, restaurant, db_path)
    key = (int(restaurant_id), block, scope_key, db_path)
    row = None if fresh else _read(restaurant_id, block, scope_key, db_path)
    if fresh and not getattr(_kept_only, "on", False):
        payload = _build_and_keep(restaurant_id, block, scope_key, as_of, stamp, build, db_path)
        return json.loads(_encode(payload))
    if row is None or row["as_of"] != as_of:
        with _key_lock(key):
            # A request that waited on another's build reads its row.
            row = _read(restaurant_id, block, scope_key, db_path)
            if row is None or row["as_of"] != as_of:
                if getattr(_kept_only, "on", False):
                    raise SnapshotMiss(block)
                payload = _build_and_keep(restaurant_id, block, scope_key, as_of, stamp, build, db_path)
                return json.loads(_encode(payload))
    if row["stamp"] != stamp or time.time() - float(row["built_at"] or 0) > REFRESH_SECONDS:
        _start_refresh(key, restaurant_id, block, scope_key, as_of, stamp, build, db_path)
    return json.loads(row["payload"])
