"""Security controls that need a database, not a process dictionary.

The security audit's P1s in one place:

  login throttling  — durable, keyed by BOTH the client IP and the account,
                      with escalating lockouts. The old counter was a dict in
                      one gunicorn worker: reset on deploy, blind to an
                      attacker rotating addresses, and the reason the app
                      could not run more than one worker.
  lockout events    — an account that crosses the threshold gets a row in
                      activity_log the owner can read in Account → Security.
  breached passwords — Have I Been Pwned's k-anonymity range API on every
                      password set. Fails open (no network → no block) and
                      never sends the password, only five hex characters of
                      its SHA-1.
  freeze            — the takeover response: every session and trusted
                      device for a restaurant's logins gone, the next sign-in
                      refused until the password is reset.
  digest lines      — what security-relevant happened in the last day, for
                      the operator's morning email.
"""
import hashlib
import os
from datetime import datetime, timedelta, timezone

import models as _models

DB_PATH = None   # resolved at call time — see get_conn()


def get_conn(db_path=None):
    """Resolved through the models module at call time, never bound at
    import: a bound copy would ignore the test suite's redirect and write
    login attempts into the real database (CLAUDE.md's hazard, and exactly
    what happened the first time this module was written)."""
    return _models.get_conn(db_path or _models.DB_PATH)


# Per account: 5 failures in the window → 5 minutes; keep failing and the
# lock grows. Per IP: a wider budget when the attempts name accounts,
# because a kitchen shares one address — but attempts that name NO account
# (forgot-password, a reset code) keep the tight budget: there is no
# account to lock instead.
ACCOUNT_MAX = 5
IP_MAX = 25
IP_MAX_ANON = 5
WINDOW_MINUTES = 15
LOCK_STEPS = ((5, 5), (10, 30), (15, 24 * 60))   # (failures in 24h, lock minutes)


from time_utils import utc_stamp as _utc

def _acct_key(username):
    return "acct:" + (username or "").strip().lower()


def _acct_ip_key(username, ip):
    """An internal login's lock, per address (SECURITY-12): failures from one
    address lock that address out of that account, not the account."""
    return "acctip:" + (username or "").strip().lower() + "@" + (ip or "").strip()


def _ip_key(ip):
    return "ip:" + (ip or "").strip()


# An internal login (admin, support) is locked per address, so anyone who
# knows the username — the seed's default is "will" — could otherwise keep
# the founder out of /admin for 24 hours with 15 wrong guesses a day. Across
# every address together a much higher cap still stops a distributed guess.
INTERNAL_DAY_MAX = 100
INTERNAL_DAY_LOCK_MINUTES = 60


def _count(conn, key, minutes):
    row = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE key=? AND attempted_at >= ?",
                       (key, _utc(datetime.now(timezone.utc) - timedelta(minutes=minutes)))).fetchone()
    return int(row[0] or 0)


def lock_minutes_for(failures_24h):
    minutes = 0
    for threshold, m in LOCK_STEPS:
        if failures_24h >= threshold:
            minutes = m
    return minutes


def _seconds_since_last_until(conn, key, lock_minutes):
    last = conn.execute("SELECT MAX(attempted_at) FROM login_attempts WHERE key=?", (key,)).fetchone()[0]
    try:
        until = datetime.fromisoformat(str(last)) + timedelta(minutes=lock_minutes)
        return (until - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()
    except Exception:
        return lock_minutes * 60


def _lock_left(conn, key):
    """Seconds left on the escalating lock (LOCK_STEPS) for one key, or 0."""
    recent = _count(conn, key, WINDOW_MINUTES)
    if recent < ACCOUNT_MAX:
        return 0
    day = _count(conn, key, 24 * 60)
    left = _seconds_since_last_until(conn, key, lock_minutes_for(max(day, ACCOUNT_MAX)))
    return int(left) if left > 0 else 0


def _internal_day_left(conn, username):
    if _count(conn, _acct_key(username), 24 * 60) < INTERNAL_DAY_MAX:
        return 0
    left = _seconds_since_last_until(conn, _acct_key(username), INTERNAL_DAY_LOCK_MINUTES)
    return int(left) if left > 0 else 0


def login_throttled(ip, username=None, db_path=DB_PATH, *, internal=False, known_device=False):
    """(blocked, retry_after_seconds). Blocked when the account is inside a
    lock, or the IP has burned its window budget.

    `internal`: the account is an admin or support login — its lock is per
    address (plus INTERNAL_DAY_MAX across all of them). `known_device`: the
    request carries a device this login remembered at a two-factor sign-in —
    no account lock applies to it; the address budget still does."""
    conn = get_conn(db_path)
    try:
        if username and not known_device:
            if internal:
                left = _lock_left(conn, _acct_ip_key(username, ip)) or _internal_day_left(conn, username)
            else:
                left = _lock_left(conn, _acct_key(username))
            if left > 0:
                return True, int(left)
        if ip:
            key = _ip_key(ip)
            if _count(conn, key, WINDOW_MINUTES) >= IP_MAX:
                return True, WINDOW_MINUTES * 60
            if not username:
                anon = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE key=? AND kind='anon' AND attempted_at >= ?",
                                    (key, _utc(datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)))).fetchone()[0]
                if int(anon or 0) >= IP_MAX_ANON:
                    return True, WINDOW_MINUTES * 60
        return False, 0
    finally:
        conn.close()


def record_login_failure(ip, username=None, db_path=DB_PATH, *, internal=False):
    """One failed attempt against both keys. When the account crosses the
    threshold, the owner's activity log says so — once per lock. An
    internal login's failures also count against its per-address key, the
    one that locks it."""
    conn = get_conn(db_path)
    try:
        now = _utc()
        conn.execute("INSERT INTO login_attempts (key, kind, ip, attempted_at) VALUES (?,?,?,?)",
                     (_ip_key(ip), "ip" if username else "anon", ip or "", now))
        locked = False
        if username:
            key = _acct_key(username)
            conn.execute("INSERT INTO login_attempts (key, kind, ip, attempted_at) VALUES (?,?,?,?)",
                         (key, "account", ip or "", now))
            if internal:
                key = _acct_ip_key(username, ip)
                conn.execute("INSERT INTO login_attempts (key, kind, ip, attempted_at) VALUES (?,?,?,?)",
                             (key, "account_ip", ip or "", now))
            n = _count(conn, key, WINDOW_MINUTES)
            locked = n == ACCOUNT_MAX or (n > ACCOUNT_MAX and n % ACCOUNT_MAX == 0)
            if locked:
                row = conn.execute("SELECT restaurant_id FROM users WHERE LOWER(username)=? OR LOWER(email)=? LIMIT 1",
                                   (username.strip().lower(), username.strip().lower())).fetchone()
                conn.commit()
                if row and row["restaurant_id"]:
                    try:
                        from models import log_event
                        log_event(row["restaurant_id"], "login_locked",
                                  {"username": username.strip()[:60], "ip": (ip or "")[:64],
                                   "failures": n, "lock_minutes": lock_minutes_for(_count(conn, key, 24 * 60))},
                                  db_path=db_path)
                    except Exception:
                        pass
        conn.commit()
        return {"locked": locked}
    finally:
        conn.close()


def clear_login_failures(ip=None, username=None, db_path=DB_PATH, *, pair_ip=None):
    """After a success: the account's failures, and (pair_ip) that address's
    per-address lock on it. An address's own budget is only cleared when
    `ip` is passed (a purpose key such as "2fa:<ip>")."""
    conn = get_conn(db_path)
    try:
        if username:
            conn.execute("DELETE FROM login_attempts WHERE key=?", (_acct_key(username),))
            if pair_ip:
                conn.execute("DELETE FROM login_attempts WHERE key=?", (_acct_ip_key(username, pair_ip),))
        if ip:
            conn.execute("DELETE FROM login_attempts WHERE key=?", (_ip_key(ip),))
        conn.commit()
    finally:
        conn.close()


def clear_account_lock(username, db_path=DB_PATH) -> int:
    """Unlock one account everywhere: its account key and every per-address
    key. The console's "Clear lockout" and the break-glass unlock
    (scripts/unlock_login.py, LOGIN_UNLOCK_USERNAMES at boot). Returns the
    failure rows removed."""
    name = (username or "").strip().lower()
    if not name:
        return 0
    conn = get_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM login_attempts WHERE key=? OR key LIKE ? ESCAPE '\\'",
                           (_acct_key(name), "acctip:" + name.replace("\\", "\\\\").replace("%", "\\%")
                            .replace("_", "\\_") + "@%"))
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def apply_boot_unlocks(env=None, db_path=DB_PATH) -> list:
    """Break-glass: LOGIN_UNLOCK_USERNAMES (comma-separated) clears those
    accounts' sign-in locks at boot, for the day the only admin is locked
    out of the console that has the unlock button. Set it, redeploy, unset
    it. (scripts/unlock_login.py does the same from a shell on the box.)
    Returns the usernames it unlocked."""
    env = os.environ if env is None else env
    names = [n.strip().lower() for n in (env.get("LOGIN_UNLOCK_USERNAMES") or "").split(",") if n.strip()]
    done = []
    for n in names:
        try:
            removed = clear_account_lock(n, db_path=db_path)
            done.append(n)
            print(f"[security] break-glass unlock: {n} ({removed} failure rows cleared) — "
                  "unset LOGIN_UNLOCK_USERNAMES once you are back in")
            try:
                import admin_events
                admin_events.record("admin", "lockout_cleared_break_glass",
                                    summary=f"LOGIN_UNLOCK_USERNAMES cleared the sign-in lock on {n} at boot",
                                    payload={"username": n, "rows": removed}, db_path=db_path)
            except Exception:
                pass
        except Exception as exc:
            print(f"[security] break-glass unlock of {n} failed: {exc}")
    return done


def lockout_state(username, db_path=DB_PATH, *, internal=False) -> dict:
    """What the throttle holds against one account right now, for the
    console: {username, locked, seconds_left, failures_15m, failures_24h,
    addresses:[{ip, locked, seconds_left, failures_15m}]}."""
    name = (username or "").strip().lower()
    conn = get_conn(db_path)
    try:
        key = _acct_key(name)
        out = {"username": name, "failures_15m": _count(conn, key, WINDOW_MINUTES),
               "failures_24h": _count(conn, key, 24 * 60), "addresses": []}
        since = _utc(datetime.now(timezone.utc) - timedelta(hours=24))
        pairs = conn.execute("SELECT DISTINCT key, ip FROM login_attempts WHERE kind='account_ip' AND key LIKE ? "
                             "ESCAPE '\\' AND attempted_at >= ?",
                             ("acctip:" + name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "@%",
                              since)).fetchall()
        for p in pairs:
            left = _lock_left(conn, p["key"])
            out["addresses"].append({"ip": p["ip"], "locked": left > 0, "seconds_left": left,
                                     "failures_15m": _count(conn, p["key"], WINDOW_MINUTES)})
        left = _internal_day_left(conn, name) if internal else _lock_left(conn, key)
        out["locked"] = left > 0 or any(a["locked"] for a in out["addresses"])
        out["seconds_left"] = max([left] + [a["seconds_left"] for a in out["addresses"]])
        return out
    finally:
        conn.close()


def active_lockouts(db_path=DB_PATH) -> list:
    """Every account the throttle is holding right now (account-wide or at
    one address), newest failure first, for Access & activity."""
    conn = get_conn(db_path)
    try:
        since = _utc(datetime.now(timezone.utc) - timedelta(hours=24))
        rows = conn.execute(
            "SELECT key, kind, MAX(attempted_at) AS last_at FROM login_attempts "
            "WHERE kind IN ('account', 'account_ip') AND attempted_at >= ? GROUP BY key ORDER BY last_at DESC",
            (since,)).fetchall()
        names = set()
        for r in rows:
            k = r["key"]
            names.add(k[5:] if k.startswith("acct:") else k[len("acctip:"):].rsplit("@", 1)[0])
        internal = {n for n in names if n and conn.execute(
            "SELECT 1 FROM users WHERE LOWER(username)=? AND (is_admin=1 OR LOWER(COALESCE(role,''))='support')",
            (n,)).fetchone()}
    finally:
        conn.close()
    out = []
    for n in sorted(names):
        if not n:
            continue
        st = lockout_state(n, db_path=db_path, internal=n in internal)
        if st["locked"]:
            st["internal"] = n in internal
            out.append(st)
    return out


def record_reauth_miss(key, ip=None, db_path=DB_PATH) -> int:
    """One wrong password at an admin step-up (POST /admin/api/reauth),
    against that session's own key. Returns the misses in the window — the
    route ends the session at five, so a stolen cookie cannot guess."""
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO login_attempts (key, kind, ip, attempted_at) VALUES (?,?,?,?)",
                     (key, "reauth", ip or "", _utc()))
        conn.commit()
        return _count(conn, key, WINDOW_MINUTES)
    finally:
        conn.close()


def clear_reauth_misses(key, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM login_attempts WHERE key=?", (key,))
        conn.commit()
    finally:
        conn.close()


def prune_login_attempts(db_path=DB_PATH, days=2):
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?",
                     (_utc(datetime.now(timezone.utc) - timedelta(days=days)),))
        conn.commit()
    finally:
        conn.close()


# ── breached passwords ───────────────────────────────────────────────────────

def password_pwned(password, timeout=3.0):
    """True when the password appears in Have I Been Pwned. Only the first
    five characters of the SHA-1 leave the server. Any failure is False:
    an outage must never stop an owner changing their password."""
    if os.getenv("HIBP_DISABLED") == "1" or not password:
        return False
    try:
        import requests
        digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
        head, tail = digest[:5], digest[5:]
        r = requests.get(f"https://api.pwnedpasswords.com/range/{head}",
                         headers={"Add-Padding": "true", "User-Agent": "cavnar-ai"}, timeout=timeout)
        if r.status_code != 200:
            return False
        for line in r.text.splitlines():
            suffix, _, count = line.partition(":")
            if suffix.strip().upper() == tail and int((count or "0").strip() or 0) > 0:
                return True
        return False
    except Exception:
        return False


PWNED_MESSAGE = "That password appears in a known data breach — choose a different one."


# ── freeze ───────────────────────────────────────────────────────────────────

def freeze_restaurant(restaurant_id, actor=None, reason=None, db_path=DB_PATH):
    """The takeover response. Returns how many logins were frozen.

    Every login that can act at this restaurant, not only the ones homed
    here (SECURITY-13): a group owner or manager homed at another location
    with an active membership here, and any session switched into it, used
    to keep working through a freeze. Those logins' sessions and remembered
    devices end and each must reset its password, like the home logins.
    Admins are never frozen."""
    from auth import revoke_all_trusted_devices
    conn = get_conn(db_path)
    try:
        ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE restaurant_id=? AND is_admin=0",
                                             (restaurant_id,)).fetchall()]
        try:
            ids += [r["user_id"] for r in conn.execute(
                "SELECT m.user_id FROM memberships m JOIN users u ON u.id=m.user_id "
                "WHERE m.restaurant_id=? AND m.is_active=1 AND u.is_admin=0", (restaurant_id,)).fetchall()]
        except Exception:
            pass    # a database predating memberships: the home logins are all there is
        ids = sorted(set(ids))
        for uid in ids:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            conn.execute("UPDATE users SET must_reset_password=1 WHERE id=?", (uid,))
            try:
                conn.execute("DELETE FROM trusted_devices WHERE user_id=?", (uid,))
            except Exception:
                pass
        # Anyone else's session acting here right now (switched in, or a
        # staff PIN session minted for this restaurant) ends too — never an
        # admin's own session.
        try:
            conn.execute("DELETE FROM sessions WHERE (active_restaurant_id=? OR staff_restaurant_id=?) "
                         "AND user_id NOT IN (SELECT id FROM users WHERE is_admin=1)",
                         (restaurant_id, restaurant_id))
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()
    try:
        revoke_all_trusted_devices(restaurant_id, db_path=db_path)
    except Exception:
        pass
    try:
        from models import log_event
        log_event(restaurant_id, "account_frozen",
                  {"by": (actor or {}).get("username"), "reason": (reason or "")[:200], "logins": len(ids)},
                  db_path=db_path)
    except Exception:
        pass
    return len(ids)


# ── the operator's morning lines ─────────────────────────────────────────────

def digest_lines(db_path=DB_PATH, hours=24):
    since = _utc(datetime.now(timezone.utc) - timedelta(hours=hours))
    out = []
    conn = get_conn(db_path)
    try:
        try:
            locks = conn.execute("SELECT COUNT(*) FROM activity_log WHERE event_type='login_locked' AND created_at >= ?",
                                 (since,)).fetchone()[0]
            if locks:
                out.append(f"{locks} account lockout{'s' if locks != 1 else ''} after repeated failed sign-ins")
        except Exception:
            pass
        try:
            ips = conn.execute("SELECT COUNT(DISTINCT ip) FROM login_attempts WHERE kind='ip' AND attempted_at >= ?",
                               (since,)).fetchone()[0]
            fails = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE kind='account' AND attempted_at >= ?",
                                 (since,)).fetchone()[0]
            if fails >= 20:
                out.append(f"{fails} failed sign-ins from {ips} address{'es' if ips != 1 else ''}")
        except Exception:
            pass
        try:
            va = conn.execute("SELECT COUNT(*) FROM admin_events WHERE event_type='view_as_started' AND created_at >= ?",
                              (since,)).fetchone()[0]
            if va:
                out.append(f"{va} admin view-as session{'s' if va != 1 else ''} opened")
            fr = conn.execute("SELECT COUNT(*) FROM admin_events WHERE event_type='account_frozen' AND created_at >= ?",
                              (since,)).fetchone()[0]
            if fr:
                out.append(f"{fr} account{'s' if fr != 1 else ''} frozen by an admin")
        except Exception:
            pass
    finally:
        conn.close()
    return out


# ── the admin console's per-session ceiling (SECURITY-4, #88) ────────────────
#
# Nothing but the sign-in form was rate-limited: a stolen admin cookie, or a
# console bug stuck in a loop, could read every tenant or fire writes as fast
# as the four request threads allowed. auth.admin_required asks this before
# every /admin request. The console makes a handful of reads per screen and
# polls once every two minutes, so the ceilings are far above any person.
#
# Process-local on purpose: it counts requests, so a database row per request
# would put a write on every console read — the failure DATA-1 removed from
# the session touch. With gunicorn --workers 1 this is exact; with N workers
# each keeps its own window and the ceiling is N times higher (CLAUDE.md's
# list of process-local limits).
ADMIN_REQUESTS_PER_MINUTE = 240
ADMIN_WRITES_PER_MINUTE = 60
_ADMIN_WINDOW_SECONDS = 60
_ADMIN_MAX_KEYS = 2000
_admin_hits = {}
import threading as _threading
_admin_hits_lock = _threading.Lock()


def admin_request_allowed(key, is_write=False, now=None):
    """(allowed, retry_after_seconds) for one more /admin request on this
    session. `key` identifies the session (a prefix of its token hash)."""
    import time as _time
    now = _time.monotonic() if now is None else now
    cutoff = now - _ADMIN_WINDOW_SECONDS
    with _admin_hits_lock:
        hits = [h for h in _admin_hits.get(key, ()) if h[0] > cutoff]
        writes = sum(1 for h in hits if h[1])
        if len(hits) >= ADMIN_REQUESTS_PER_MINUTE or (is_write and writes >= ADMIN_WRITES_PER_MINUTE):
            oldest = min(h[0] for h in hits) if hits else now
            _admin_hits[key] = hits
            return False, max(1, int(oldest + _ADMIN_WINDOW_SECONDS - now) + 1)
        hits.append((now, bool(is_write)))
        _admin_hits[key] = hits
        if len(_admin_hits) > _ADMIN_MAX_KEYS:
            # Bounded: drop the sessions that have been quiet longest.
            for stale in sorted(_admin_hits, key=lambda k: max((h[0] for h in _admin_hits[k]), default=0))[
                    :len(_admin_hits) - _ADMIN_MAX_KEYS]:
                _admin_hits.pop(stale, None)
    return True, 0


def reset_admin_rate_limits():
    """Tests, and nothing else."""
    with _admin_hits_lock:
        _admin_hits.clear()


# ── JSON bodies that are not objects (SEC-32) ────────────────────────────────

_BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _reject_non_object_json():
    """before_request: a JSON body must be an object. Nearly every handler
    does `data = request.get_json() or {}` and then `data.get(...)`, so a body
    of "x" or [1] raised AttributeError and answered 500 with a traceback in
    the log — about 170 routes, including the unauthenticated sign-in ones.
    One check here instead of 170 isinstance tests. A body that is not valid
    JSON at all is left to the handler (Flask already answers that 400)."""
    from flask import request, jsonify
    if request.method not in _BODY_METHODS or not request.is_json:
        return None
    body = request.get_json(silent=True)
    if body is None or isinstance(body, dict):
        return None
    return jsonify(ok=False, error="The request body must be a JSON object."), 400


def json_object_guard(blueprint):
    """Attach the check above to a blueprint that reads JSON bodies. Not for
    webhook receivers, which take whatever shape the sender posts and verify
    it by signature."""
    if not getattr(blueprint, "_json_object_guard", False):
        blueprint.before_request(_reject_non_object_json)
        blueprint._json_object_guard = True
    return blueprint
