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


def _utc(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")


def _acct_key(username):
    return "acct:" + (username or "").strip().lower()


def _ip_key(ip):
    return "ip:" + (ip or "").strip()


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


def login_throttled(ip, username=None, db_path=DB_PATH):
    """(blocked, retry_after_seconds). Blocked when the account is inside a
    lock, or the IP has burned its window budget."""
    conn = get_conn(db_path)
    try:
        if username:
            key = _acct_key(username)
            recent = _count(conn, key, WINDOW_MINUTES)
            if recent >= ACCOUNT_MAX:
                day = _count(conn, key, 24 * 60)
                lock = lock_minutes_for(max(day, ACCOUNT_MAX))
                last = conn.execute("SELECT MAX(attempted_at) FROM login_attempts WHERE key=?", (key,)).fetchone()[0]
                try:
                    until = datetime.fromisoformat(str(last)) + timedelta(minutes=lock)
                    left = (until - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()
                except Exception:
                    left = lock * 60
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


def record_login_failure(ip, username=None, db_path=DB_PATH):
    """One failed attempt against both keys. When the account crosses the
    threshold, the owner's activity log says so — once per lock."""
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


def clear_login_failures(ip=None, username=None, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        if username:
            conn.execute("DELETE FROM login_attempts WHERE key=?", (_acct_key(username),))
        if ip:
            conn.execute("DELETE FROM login_attempts WHERE key=?", (_ip_key(ip),))
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
    """The takeover response. Returns how many logins were frozen."""
    from auth import revoke_all_trusted_devices
    conn = get_conn(db_path)
    try:
        ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE restaurant_id=? AND is_admin=0",
                                             (restaurant_id,)).fetchall()]
        for uid in ids:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            conn.execute("UPDATE users SET must_reset_password=1 WHERE id=?", (uid,))
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
