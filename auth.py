from flask import redirect, url_for, request
from functools import wraps
"""
auth.py — User authentication for the Cavnar AI hosted dashboard
Handles: user table, password hashing, session management, login/logout
"""
import hashlib
import sqlite3
import secrets
from datetime import datetime, timezone
from typing import Optional
from werkzeug.security import generate_password_hash, check_password_hash
from models import DB_PATH, get_conn

# ── Schema extension ──────────────────────────────────────────────────────────

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    username        TEXT    NOT NULL UNIQUE,
    email           TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_login      TEXT,
    reset_token     TEXT,
    reset_token_expires TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token           TEXT    PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT    NOT NULL,
    last_active     TEXT    NOT NULL DEFAULT (datetime('now')),
    ip_address      TEXT,
    user_agent      TEXT
);

-- Append-only, never pruned/deleted — unlike `sessions` (hard-deleted on
-- expiry/revoke/device-dedup), this is what powers the Account "sign-in
-- activity" history, so a login has to stay visible here regardless of
-- what later happens to the session row it produced.
CREATE TABLE IF NOT EXISTS login_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    restaurant_id   INTEGER,
    event           TEXT    NOT NULL DEFAULT 'login',
    ip_address      TEXT,
    user_agent      TEXT,
    device_type     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- "Remember this device for 30 days" (2FA). One row per remembered device,
-- so they can be listed and revoked individually — the older single
-- restaurants.two_fa_device_token column only ever held the LAST device.
CREATE TABLE IF NOT EXISTS trusted_devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL,
    user_id         INTEGER,
    token_hash      TEXT    NOT NULL UNIQUE,
    label           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_used_at    TEXT,
    expires_at      TEXT    NOT NULL
);

-- One-time "This wasn't me" links minted per sign-in for the login-alert
-- email. Consuming one revokes every session and forces a password reset.
CREATE TABLE IF NOT EXISTS login_reports (
    token           TEXT    PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    session_token   TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    used_at         TEXT
);

-- Identity → Tenant → Role.
--
-- users.restaurant_id welds an identity to exactly one restaurant, which is
-- why a person who works at two locations needs two accounts today. This is
-- the edge that replaces it: one row per (person, restaurant), carrying the
-- role they hold THERE. The same identity can be an employee at one location
-- and a manager at another without a second login.
--
-- users.restaurant_id is deliberately NOT dropped. ~390 routes read
-- current_user["restaurant_id"] for tenant scoping, and it keeps meaning
-- exactly what it means today (the home restaurant). This table is the
-- source of truth for AUTHORIZATION; that column stays the source of truth
-- for SCOPING until a later phase retires it.
--
-- employee_name is the join back to the seven tables keyed by staff name
-- (staff_capabilities, staff_availability, staff_contacts, staff_notes,
-- manual_team_members, schedule_shares, capability_changes). Employees have
-- always been name strings from POS shift data rather than a roster this app
-- owns; a membership points AT one rather than renaming anything.
--
-- pin_hash is NULL until an owner issues a PIN. A membership without one is
-- inert: it grants nothing and cannot sign in, which is what lets the whole
-- feature roll out restaurant by restaurant.
CREATE TABLE IF NOT EXISTS memberships (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    role            TEXT    NOT NULL,
    employee_name   TEXT,
    pin_hash        TEXT,
    pin_set_at      TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT,
    UNIQUE(user_id, restaurant_id)
);
CREATE INDEX IF NOT EXISTS idx_memberships_restaurant
    ON memberships(restaurant_id, is_active);
CREATE INDEX IF NOT EXISTS idx_memberships_user
    ON memberships(user_id, is_active);

-- Per-membership PIN throttling.
--
-- A 4-6 digit PIN is 10^4-10^6 possibilities, so hashing is not the defence
-- — throttling is. It cannot reuse auth_routes._login_attempts: that counter
-- is per-IP, in memory, and per-process, so one kitchen behind a single NAT
-- would share a 5-attempt budget across the whole staff and a deploy would
-- reset it. This is per-membership and persisted.
CREATE TABLE IF NOT EXISTS membership_pin_attempts (
    membership_id   INTEGER PRIMARY KEY REFERENCES memberships(id),
    failed_count    INTEGER NOT NULL DEFAULT 0,
    last_failed_at  TEXT,
    locked_until    TEXT
);

-- The staff portal's front door: a long random per-restaurant token that
-- identifies WHICH restaurant's roster to show, so an employee never types a
-- restaurant name. Same shape as schedule_shares.token, which has served the
-- unauthenticated /s/<token> schedule page for exactly this reason.
-- Revocable and re-mintable; it authorises nothing on its own.
CREATE TABLE IF NOT EXISTS staff_portal_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    token           TEXT    NOT NULL UNIQUE,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    revoked_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_staff_portal_restaurant
    ON staff_portal_tokens(restaurant_id, revoked_at);
"""

def init_auth(db_path: str = DB_PATH):
    # Tables must exist before the ALTER migrations below can run against them —
    # on a genuinely fresh database (a new deploy, or any test fixture) these
    # ran in the opposite order, so every ALTER silently failed against a
    # not-yet-existing table and CREATE TABLE IF NOT EXISTS then created users/
    # sessions missing role, google_id, and active_restaurant_id entirely.
    conn = sqlite3.connect(db_path)
    conn.executescript(AUTH_SCHEMA)
    conn.commit()
    conn.close()
    # Migrations — for databases created before these columns existed.
    for col_sql in [
        "ALTER TABLE sessions ADD COLUMN last_active TEXT NOT NULL DEFAULT (datetime('now'))",
        "ALTER TABLE sessions ADD COLUMN ip_address TEXT",
        "ALTER TABLE sessions ADD COLUMN user_agent TEXT",
        "ALTER TABLE sessions ADD COLUMN active_restaurant_id INTEGER",
        "ALTER TABLE sessions ADD COLUMN device_type TEXT NOT NULL DEFAULT 'web'",
        "ALTER TABLE sessions ADD COLUMN device_id TEXT",
        "ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'client'",
        "ALTER TABLE users ADD COLUMN google_id TEXT",
        "ALTER TABLE users ADD COLUMN apple_user_id TEXT",
        "ALTER TABLE users ADD COLUMN password_changed_at TEXT",
        "ALTER TABLE users ADD COLUMN password_strength TEXT",
        "ALTER TABLE users ADD COLUMN recovery_email TEXT",
        "ALTER TABLE users ADD COLUMN recovery_email_pending TEXT",
        "ALTER TABLE users ADD COLUMN recovery_email_code TEXT",
        "ALTER TABLE users ADD COLUMN recovery_email_expires TEXT",
        "ALTER TABLE users ADD COLUMN must_reset_password INTEGER DEFAULT 0",
        # Who may change Operational Scores, shift targets, profiles and the
        # quality weighting. Defaults ON so every existing login keeps the
        # access it has.
        #
        # This comment used to claim the team-invite flow created teammates
        # with it OFF. It never did — invite_team_member() calls create_user(),
        # which never writes this column, so every invited teammate got the
        # DEFAULT 1 and the permission was inert: documented, enforced in ten
        # places, and impossible to actually switch off. The role now carries
        # the real decision (permissions.TEAM_RATE) and this column is a
        # per-login OVERRIDE an owner sets deliberately through
        # /account/team/<id>/can-manage.
        "ALTER TABLE users ADD COLUMN can_manage_team INTEGER DEFAULT 1",
    ]:
        try:
            import sqlite3 as _sql
            conn_m = _sql.connect(db_path)
            conn_m.execute(col_sql)
            conn_m.commit()
            conn_m.close()
        except Exception:
            pass  # Column already exists

    # Normalize any mixed-case usernames written outside create_user() (e.g.
    # raw SQL in a seed/"ensure" script, as _ensure_gia_mia_vibe() in
    # hosted_dashboard.py used to). get_user_by_username() always lowercases
    # its input before querying, but the username column has no COLLATE
    # NOCASE, so a stored mixed-case value can never match and that account
    # can never log in again, regardless of what's typed. Idempotent — a
    # no-op once every username is already lowercase, safe to run every boot.
    try:
        conn_n = sqlite3.connect(db_path)
        conn_n.execute("UPDATE users SET username = LOWER(username) WHERE username != LOWER(username)")
        conn_n.commit()
        conn_n.close()
    except Exception:
        pass

    backfill_memberships(db_path=db_path)

    # A missing pepper silently downgrades every staff PIN to something a
    # leaked database makes trivially brute-forceable. It must not be a
    # condition you only discover by reading the source.
    try:
        health = pin_pepper_health(db_path=db_path)
        if not health["ok"]:
            print(f"[auth] PIN PEPPER WARNING: {health['message']}")
    except Exception:
        pass


def backfill_memberships(db_path: str = DB_PATH) -> int:
    """Give every existing login the membership it implicitly already had.

    Runs on every boot and is idempotent — the INSERT ... SELECT only picks
    up users with no row yet, so it is a no-op from the second run onward.
    Deriving (restaurant_id, role) straight off the user row means an
    existing account's authorization is bit-for-bit what it was before this
    table existed; nothing is invented and nothing needs a deploy window.

    Returns how many rows it created, for the migration test to assert on.
    """
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute("""
            INSERT INTO memberships (user_id, restaurant_id, role, is_active, created_at)
            SELECT u.id, u.restaurant_id, COALESCE(NULLIF(TRIM(u.role), ''), 'client'),
                   u.is_active, COALESCE(u.created_at, datetime('now'))
            FROM users u
            WHERE u.restaurant_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM memberships m
                  WHERE m.user_id = u.id AND m.restaurant_id = u.restaurant_id
              )
        """)
        created = cur.rowcount or 0
        conn.commit()
        conn.close()
        return created
    except Exception:
        # Same fail-quiet stance as the column migrations above: a backfill
        # failure must not stop the app booting, and the dual-read in
        # get_session_user() falls back to users.restaurant_id regardless.
        return 0


# ── Memberships (Identity → Tenant → Role) ────────────────────────────────

def get_membership(user_id: int, restaurant_id: int,
                   db_path: str = DB_PATH) -> Optional[dict]:
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT * FROM memberships WHERE user_id=? AND restaurant_id=? AND is_active=1",
        (user_id, restaurant_id)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_memberships_for_user(user_id: int, db_path: str = DB_PATH) -> list:
    """Every restaurant this identity can act in. The multi-restaurant story:
    one person, many memberships, a different role in each."""
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM memberships WHERE user_id=? AND is_active=1 ORDER BY id",
        (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_memberships_for_restaurant(restaurant_id: int, role: str = None,
                                   db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    sql = ("SELECT m.*, u.username, u.email, u.is_active AS user_is_active "
           "FROM memberships m JOIN users u ON u.id = m.user_id "
           "WHERE m.restaurant_id=? AND m.is_active=1 AND u.is_active=1")
    args = [restaurant_id]
    if role:
        sql += " AND m.role=?"
        args.append(role)
    sql += " ORDER BY COALESCE(m.employee_name, u.username) COLLATE NOCASE"
    rows = conn.execute(sql, tuple(args)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_membership(user_id: int, restaurant_id: int, role: str,
                      employee_name: str = None, db_path: str = DB_PATH) -> dict:
    """Create or update one identity's role at one restaurant."""
    from permissions import ROLE_PERMISSIONS, normalize_role
    role = normalize_role(role)
    if role not in ROLE_PERMISSIONS:
        raise ValueError(f"unknown role: {role!r}")
    conn = get_conn(db_path)
    try:
        conn.execute("""
            INSERT INTO memberships (user_id, restaurant_id, role, employee_name, updated_at)
            VALUES (?,?,?,?,datetime('now'))
            ON CONFLICT(user_id, restaurant_id) DO UPDATE SET
                role=excluded.role,
                employee_name=COALESCE(excluded.employee_name, memberships.employee_name),
                is_active=1,
                updated_at=datetime('now')
        """, (user_id, restaurant_id, role, (employee_name or "").strip() or None))
        conn.commit()
        row = conn.execute(
            "SELECT * FROM memberships WHERE user_id=? AND restaurant_id=?",
            (user_id, restaurant_id)).fetchone()
    finally:
        conn.close()
    return dict(row)


def set_membership_active(membership_id: int, restaurant_id: int, active: bool,
                          db_path: str = DB_PATH) -> bool:
    """Activate/deactivate one membership. Scoped by restaurant_id so an
    owner can never toggle a membership belonging to another tenant."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE memberships SET is_active=?, updated_at=datetime('now') "
            "WHERE id=? AND restaurant_id=?",
            (1 if active else 0, membership_id, restaurant_id))
        conn.commit()
        changed = cur.rowcount > 0
    finally:
        conn.close()
    if changed and not active:
        # Revoking access has to end the sessions it already granted, not
        # wait up to 14 hours for them to expire. Same stance as
        # revoke_team_member, which kills sessions rather than trusting TTL.
        _end_staff_sessions_for_membership(membership_id, restaurant_id, db_path=db_path)
    return changed


# ── PIN authentication ────────────────────────────────────────────────────
#
# PINs are treated as passwords: hashed with the same werkzeug KDF the
# password column uses, never stored or logged in the clear, and compared
# only through check_password_hash.
#
# The shortness is handled by throttling, not by the hash. A 4-digit PIN is
# 10,000 possibilities — a KDF slows an OFFLINE attacker with the database in
# hand, but does nothing about an online one typing at a tablet. That is what
# membership_pin_attempts is for, and why the lockout is per-membership and
# persisted rather than reusing auth_routes' per-IP in-memory counter (an
# entire kitchen shares one NAT address, and a deploy resets it).
#
# A PIN is never an identifier. The flow resolves WHO first (tap your name),
# then verifies. Two employees may hold the same PIN with no collision and no
# way to enumerate one from the other.

PIN_MIN_LENGTH = 4
PIN_MAX_LENGTH = 8
PIN_MAX_ATTEMPTS = 5
PIN_LOCKOUT_MINUTES = 15
# A staff session is a shift, not a month. These are frequently shared
# devices sitting on a pass or a host stand.
STAFF_SESSION_HOURS = 14


class PinError(ValueError):
    """A PIN that would be unsafe or meaningless to store."""


def _pin_pepper() -> str:
    """App-level secret mixed into every PIN before hashing.

    With a 4-digit space, a stolen database is otherwise brute-forceable
    offline against any KDF given enough hardware — 10,000 candidates per
    membership is nothing. The pepper lives outside the database (env), so
    dumping the DB alone is not enough to test candidates.
    """
    import os
    return os.environ.get("CAVNAR_PIN_PEPPER", "")


# Hashes are written with the pepper VERSION they were made under, so the
# pepper can be rotated without invalidating every PIN in the estate. An
# unversioned hash predates this and is read as v0 (empty pepper).
_PIN_PEPPER_VERSION = "v1"
_PIN_HASH_PREFIX = "pep"


def _peppered(pin: str, version: str = None) -> str:
    """The string actually handed to the KDF.

    v0 is the unpeppered form kept only so hashes written before versioning
    still verify; everything new is written at the current version.
    """
    version = version or _PIN_PEPPER_VERSION
    if version == "v0":
        return f"::{pin}"
    return f"{_pin_pepper()}::{pin}"


def _encode_pin_hash(raw_hash: str) -> str:
    return f"{_PIN_HASH_PREFIX}{_PIN_PEPPER_VERSION}${raw_hash}"


def _decode_pin_hash(stored: str):
    """(version, raw_hash) for a stored PIN hash."""
    if stored and stored.startswith(_PIN_HASH_PREFIX):
        marker, _, raw = stored.partition("$")
        return marker[len(_PIN_HASH_PREFIX):], raw
    return "v0", stored


def pin_pepper_health(db_path: str = DB_PATH) -> dict:
    """Whether PINs are actually protected by a pepper right now.

    A 4-digit secret hashed with no pepper is brute-forceable offline the
    moment the database leaks, so "the env var was never set in production"
    has to be loud rather than an invisible downgrade. init_auth() logs this
    at boot and admin_ops surfaces it; see also scripts/check_pin_pepper.py.
    """
    configured = bool(_pin_pepper())
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT pin_hash FROM memberships WHERE pin_hash IS NOT NULL").fetchall()
        finally:
            conn.close()
        total = len(rows)
        unpeppered = sum(1 for r in rows if _decode_pin_hash(r["pin_hash"])[0] == "v0")
    except Exception:
        return {"configured": configured, "pins": 0, "unpeppered": 0,
                "ok": configured, "message": "Unable to read membership PINs."}
    ok = configured and unpeppered == 0
    if not configured and total:
        message = (f"CAVNAR_PIN_PEPPER is not set and {total} staff PIN(s) exist. "
                   "Those hashes are brute-forceable offline if the database leaks.")
    elif not configured:
        message = ("CAVNAR_PIN_PEPPER is not set. Set it before issuing any staff PIN.")
    elif unpeppered:
        message = (f"{unpeppered} of {total} staff PIN(s) predate the current pepper "
                   "and are re-hashed on next successful sign-in.")
    else:
        message = "OK"
    return {"configured": configured, "pins": total, "unpeppered": unpeppered,
            "ok": ok, "message": message}


def validate_pin(pin: str) -> str:
    """Normalize and refuse PINs that aren't worth the name."""
    pin = (pin or "").strip()
    if not pin.isdigit():
        raise PinError("A PIN must be digits only.")
    if not (PIN_MIN_LENGTH <= len(pin) <= PIN_MAX_LENGTH):
        raise PinError(f"A PIN must be {PIN_MIN_LENGTH}–{PIN_MAX_LENGTH} digits.")
    if len(set(pin)) == 1:
        raise PinError("That PIN is too easy to guess — don't repeat one digit.")
    # Straight runs up or down (1234, 4321, 9876). Everything else — including
    # dates and doubles like 1122 — is allowed; over-filtering a 4-digit space
    # shrinks it faster than it helps, and the lockout is the real control.
    digits = [int(c) for c in pin]
    deltas = {b - a for a, b in zip(digits, digits[1:])}
    if deltas in ({1}, {-1}):
        raise PinError("That PIN is too easy to guess — avoid sequences.")
    return pin


def set_membership_pin(membership_id: int, restaurant_id: int, pin: str,
                       db_path: str = DB_PATH) -> bool:
    """Set or replace a membership's PIN, scoped to the acting restaurant.

    Clears any lockout (an owner resetting a PIN is the documented way out of
    one) and ends that membership's live staff sessions, so a rotated PIN
    genuinely revokes the old one rather than leaving it usable until expiry.
    """
    pin = validate_pin(pin)
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE memberships SET pin_hash=?, pin_set_at=datetime('now'), "
            "updated_at=datetime('now') WHERE id=? AND restaurant_id=?",
            (_encode_pin_hash(generate_password_hash(_peppered(pin))),
             membership_id, restaurant_id))
        conn.commit()
        changed = cur.rowcount > 0
        if changed:
            conn.execute("DELETE FROM membership_pin_attempts WHERE membership_id=?",
                         (membership_id,))
            conn.commit()
    finally:
        conn.close()
    if changed:
        _end_staff_sessions_for_membership(membership_id, restaurant_id, db_path=db_path)
    return changed


def clear_membership_pin(membership_id: int, restaurant_id: int,
                         db_path: str = DB_PATH) -> bool:
    """Remove a PIN. The membership stays, but can no longer sign in — which
    is how an owner takes staff access away without deleting the person."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE memberships SET pin_hash=NULL, pin_set_at=NULL, "
            "updated_at=datetime('now') WHERE id=? AND restaurant_id=?",
            (membership_id, restaurant_id))
        conn.commit()
        changed = cur.rowcount > 0
    finally:
        conn.close()
    if changed:
        _end_staff_sessions_for_membership(membership_id, restaurant_id, db_path=db_path)
    return changed


def pin_lockout_state(membership_id: int, db_path: str = DB_PATH) -> dict:
    """{locked, failed_count, seconds_remaining} for one membership."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT failed_count, locked_until FROM membership_pin_attempts "
            "WHERE membership_id=?", (membership_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"locked": False, "failed_count": 0, "seconds_remaining": 0}
    remaining = 0
    if row["locked_until"]:
        try:
            until = datetime.fromisoformat(str(row["locked_until"]))
            remaining = max(0, int((until - datetime.utcnow()).total_seconds()))
        except Exception:
            remaining = 0
    return {"locked": remaining > 0, "failed_count": row["failed_count"] or 0,
            "seconds_remaining": remaining}


def _record_pin_failure(membership_id: int, db_path: str = DB_PATH) -> dict:
    """Count one miss and lock the membership out once it hits the ceiling."""
    from datetime import timedelta as _td
    conn = get_conn(db_path)
    try:
        conn.execute("""
            INSERT INTO membership_pin_attempts (membership_id, failed_count, last_failed_at)
            VALUES (?, 1, datetime('now'))
            ON CONFLICT(membership_id) DO UPDATE SET
                failed_count = membership_pin_attempts.failed_count + 1,
                last_failed_at = datetime('now')
        """, (membership_id,))
        conn.commit()
        row = conn.execute("SELECT failed_count FROM membership_pin_attempts "
                           "WHERE membership_id=?", (membership_id,)).fetchone()
        count = row["failed_count"] if row else 1
        if count >= PIN_MAX_ATTEMPTS:
            until = (datetime.utcnow() + _td(minutes=PIN_LOCKOUT_MINUTES)).isoformat()
            conn.execute("UPDATE membership_pin_attempts SET locked_until=?, failed_count=0 "
                         "WHERE membership_id=?", (until, membership_id))
            conn.commit()
    finally:
        conn.close()
    return pin_lockout_state(membership_id, db_path=db_path)


def _clear_pin_failures(membership_id: int, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM membership_pin_attempts WHERE membership_id=?", (membership_id,))
    conn.commit()
    conn.close()


def unlock_membership_pin(membership_id: int, restaurant_id: int,
                          db_path: str = DB_PATH) -> bool:
    """Owner-initiated unlock, scoped to the acting restaurant."""
    conn = get_conn(db_path)
    try:
        owned = conn.execute("SELECT 1 FROM memberships WHERE id=? AND restaurant_id=?",
                             (membership_id, restaurant_id)).fetchone()
    finally:
        conn.close()
    if not owned:
        return False
    _clear_pin_failures(membership_id, db_path=db_path)
    return True


def verify_membership_pin(membership_id: int, restaurant_id: int, pin: str,
                          db_path: str = DB_PATH) -> dict:
    """Check a PIN. {ok} on success, {ok: False, error, locked} otherwise.

    restaurant_id is passed in from the portal token, never from the client,
    and is re-checked here so a tampered membership_id cannot reach across
    tenants even if it is a real id belonging to someone else's restaurant.

    Every failure path returns the same message. Distinguishing "no PIN set"
    from "wrong PIN" would let anyone with the portal link enumerate which
    staff have access.
    """
    generic = {"ok": False, "error": "That PIN didn't match.", "locked": False}
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, pin_hash FROM memberships "
            "WHERE id=? AND restaurant_id=? AND is_active=1",
            (membership_id, restaurant_id)).fetchone()
    finally:
        conn.close()
    if not row:
        return generic

    state = pin_lockout_state(membership_id, db_path=db_path)
    if state["locked"]:
        mins = max(1, state["seconds_remaining"] // 60)
        return {"ok": False, "locked": True,
                "error": f"Too many tries. Ask a manager to unlock, or wait {mins} min."}

    if not row["pin_hash"]:
        # No PIN issued yet. Still counted, so the lockout also throttles
        # probing at memberships that cannot log in at all.
        _record_pin_failure(membership_id, db_path=db_path)
        return generic

    candidate = (pin or "").strip()
    version, raw_hash = _decode_pin_hash(row["pin_hash"])
    if not check_password_hash(raw_hash, _peppered(candidate, version=version)):
        after = _record_pin_failure(membership_id, db_path=db_path)
        if after["locked"]:
            mins = max(1, after["seconds_remaining"] // 60)
            return {"ok": False, "locked": True,
                    "error": f"Too many tries. Ask a manager to unlock, or wait {mins} min."}
        return generic

    _clear_pin_failures(membership_id, db_path=db_path)
    # Transparent upgrade: a hash written under an older pepper version is
    # rewritten under the current one the first time its owner signs in, so a
    # rotation drains on its own instead of needing every PIN reissued.
    if version != _PIN_PEPPER_VERSION:
        try:
            conn_up = get_conn(db_path)
            try:
                conn_up.execute(
                    "UPDATE memberships SET pin_hash=?, updated_at=datetime('now') WHERE id=?",
                    (_encode_pin_hash(generate_password_hash(_peppered(candidate))), membership_id))
                conn_up.commit()
            finally:
                conn_up.close()
        except Exception as exc:
            try:
                import ops as _ops_pin
                _ops_pin.capture(exc, job="pin_pepper_upgrade",
                                 context=f"membership_id={membership_id}")
            except Exception:
                print(f"[pin_pepper_upgrade] failed for membership {membership_id}: {exc}")
    return {"ok": True}


def create_staff_session(user_id: int, restaurant_id: int, ip_address: str = None,
                         user_agent: str = None, device_id: str = None,
                         db_path: str = DB_PATH) -> str:
    """A shift-length session tagged so it is distinguishable from a console
    one. Reuses the sessions table wholesale — same hashed-token storage,
    same expiry handling, same revoke paths."""
    return create_session(user_id, days=0, ip_address=ip_address,
                          user_agent=user_agent, device_type="staff_pin",
                          device_id=device_id, restaurant_id=restaurant_id,
                          hours=STAFF_SESSION_HOURS, db_path=db_path)


def get_or_create_staff_portal_token(restaurant_id: int, db_path: str = DB_PATH) -> str:
    """The restaurant's current staff-portal token, minting one on first use.

    This identifies WHICH restaurant's roster to show and nothing else. It
    authorises no data on its own — every read still requires a PIN session —
    so its only real secret value is the roster of first names behind it.
    """
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT token FROM staff_portal_tokens WHERE restaurant_id=? AND revoked_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (restaurant_id,)).fetchone()
        if row:
            return row["token"]
        token = secrets.token_urlsafe(24)
        conn.execute("INSERT INTO staff_portal_tokens (restaurant_id, token) VALUES (?,?)",
                     (restaurant_id, token))
        conn.commit()
        return token
    finally:
        conn.close()


def rotate_staff_portal_token(restaurant_id: int, db_path: str = DB_PATH) -> str:
    """Revoke the current link and mint a new one — the answer to a link
    that leaked, or an employee who left with it saved."""
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE staff_portal_tokens SET revoked_at=datetime('now') "
                     "WHERE restaurant_id=? AND revoked_at IS NULL", (restaurant_id,))
        conn.commit()
    finally:
        conn.close()
    return get_or_create_staff_portal_token(restaurant_id, db_path=db_path)


def restaurant_for_portal_token(token: str, db_path: str = DB_PATH) -> Optional[int]:
    """The restaurant a portal token belongs to, or None if unknown/revoked.

    This is the ONLY way a staff request names a restaurant. Nothing reads a
    restaurant_id off the request body, which is what keeps the whole tier
    inside one tenant.
    """
    if not token:
        return None
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT restaurant_id FROM staff_portal_tokens WHERE token=? AND revoked_at IS NULL",
            (token,)).fetchone()
    finally:
        conn.close()
    return row["restaurant_id"] if row else None


def staff_login_required(f):
    """Gate for the staff portal. The mirror image of login_required.

    Requires a live session whose identity holds an EMPLOYEE-tier membership.
    A console login (owner/manager) is refused here for the same reason an
    employee is refused the dashboard: these are two different products, and
    letting a session drift between them is how scoping bugs start. An owner
    who wants to see the staff view signs in with their own PIN.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        from flask import jsonify as _jsonify_sr
        token = request.cookies.get("staff_session")
        if not token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
        user = get_session_user(token) if token else None
        wants_json = request.path.startswith("/staff/api") or request.method != "GET"
        if not user:
            if wants_json:
                return _jsonify_sr(ok=False, error="Your shift session ended — sign in again.",
                                   session_expired=True), 401
            return redirect(url_for("staff.portal_entry"))
        from permissions import TASKS_VIEW_OWN, has_permission
        if not has_permission(user, TASKS_VIEW_OWN) or not user.get("membership_id"):
            if wants_json:
                return _jsonify_sr(ok=False, error="This isn't a staff account."), 403
            return redirect(url_for("staff.portal_entry"))
        return f(*args, **kwargs, current_user=user)
    return decorated


def _end_staff_sessions_for_membership(membership_id: int, restaurant_id: int,
                                       db_path: str = DB_PATH):
    """Drop every staff session belonging to this membership's identity at
    this restaurant. Console sessions are left alone — a manager who also has
    a PIN should not be signed out of the dashboard because their PIN
    changed."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT user_id FROM memberships WHERE id=? AND restaurant_id=?",
                           (membership_id, restaurant_id)).fetchone()
        if row:
            conn.execute(
                "DELETE FROM sessions WHERE user_id=? AND device_type='staff_pin'",
                (row["user_id"],))
            conn.commit()
    except Exception as exc:
        # This revoke is a security control: if it fails, a deactivated
        # employee or a rotated PIN leaves a live session behind for up to a
        # full shift. Swallowing that silently is exactly the failure mode
        # that makes a revocation feature untrustworthy, so it is reported.
        try:
            import ops as _ops_staff
            _ops_staff.capture(exc, job="staff_session_revoke",
                               context=f"membership_id={membership_id} restaurant_id={restaurant_id}")
        except Exception:
            print(f"[staff_session_revoke] failed for membership {membership_id}: {exc}")
    finally:
        conn.close()


# ── User CRUD ─────────────────────────────────────────────────────────────────

def create_user(restaurant_id: int, username: str, email: str,
                password: str, is_admin: bool = False,
                db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    # Scored/stamped at creation too, not just on a later change — an
    # account whose password was never touched since Will set it up used
    # to have neither value, showing the Security sheet's tile a placeholder
    # "Set" with no real information behind it.
    from zoneinfo import ZoneInfo as _ZI_cu
    now = datetime.now(_ZI_cu('America/Chicago')).strftime('%Y-%m-%dT%H:%M:%S')
    cur = conn.execute("""
        INSERT INTO users (restaurant_id, username, email, password_hash, is_admin, password_changed_at, password_strength)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (restaurant_id, username.lower().strip(), email.lower().strip(),
          generate_password_hash(password), int(is_admin), now, password_strength(password)))
    conn.commit()
    uid = cur.lastrowid
    conn.close()
    return uid

def get_user_by_username(username: str, db_path: str = DB_PATH) -> Optional[dict]:
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT * FROM users WHERE username=? AND is_active=1",
        (username.lower().strip(),)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def get_user_by_restaurant_id(restaurant_id: int, db_path: str = DB_PATH) -> Optional[dict]:
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT * FROM users WHERE restaurant_id=? AND is_active=1 AND is_admin=0 LIMIT 1",
        (restaurant_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def get_user_by_id(user_id: int, db_path: str = DB_PATH) -> Optional[dict]:
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def verify_password(username: str, password: str,
                    db_path: str = DB_PATH) -> Optional[dict]:
    user = get_user_by_username(username, db_path)
    if not user:
        return None
    if not check_password_hash(user["password_hash"], password):
        return None
    # Update last login
    conn = get_conn(db_path)
    from zoneinfo import ZoneInfo as _ZI_auth
    conn.execute("UPDATE users SET last_login=? WHERE id=?",
                 (datetime.now(_ZI_auth('America/Chicago')).strftime('%Y-%m-%dT%H:%M:%S'), user["id"]))
    conn.commit()
    conn.close()
    return user

def password_strength(password: str) -> str:
    """A rough, display-only strength label — not a security gate (the
    8-character minimum is enforced separately at the call sites). Scored
    from length plus how many character classes it mixes, computed once
    here from the plaintext at set-time since the hash can't be scored
    later."""
    classes = sum([
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(not c.isalnum() for c in password),
    ])
    if len(password) >= 12 and classes >= 3:
        return "strong"
    if len(password) >= 8 and classes >= 2:
        return "good"
    return "weak"


def update_password(user_id: int, new_password: str, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    from zoneinfo import ZoneInfo as _ZI_pw
    conn.execute(
        "UPDATE users SET password_hash=?, password_changed_at=?, password_strength=? WHERE id=?",
        (
            generate_password_hash(new_password),
            datetime.now(_ZI_pw('America/Chicago')).strftime('%Y-%m-%dT%H:%M:%S'),
            password_strength(new_password),
            user_id,
        ),
    )
    conn.commit()
    conn.close()

def invite_team_member(restaurant_id: int, name: str, email: str,
                       db_path: str = DB_PATH) -> dict:
    """Self-serve version of what Will already does by hand for every
    client's primary login: create_user() already accepts an existing
    restaurant_id (its only two callers just always happen to pass a
    freshly-created one), so a second user on the same restaurant needs no
    new creation path — just a generated username/temp password and the
    same create_user() call. Returns {"ok": True, "user_id", "username",
    "temp_password"} on success, or {"ok": False, "error": "..."} on a
    duplicate username/email — never raises."""
    email = email.lower().strip()
    base = (email.split("@")[0] or name.lower().replace(" ", "")).strip() or "member"
    username = "".join(c for c in base if c.isalnum()) or "member"
    temp_password = secrets.token_urlsafe(9)
    conn = get_conn(db_path)
    candidate = username
    suffix = 1
    while conn.execute("SELECT 1 FROM users WHERE username=?", (candidate,)).fetchone():
        suffix += 1
        candidate = f"{username}{suffix}"
    conn.close()
    try:
        user_id = create_user(restaurant_id, candidate, email, temp_password,
                              is_admin=False, db_path=db_path)
    except sqlite3.IntegrityError:
        return {"ok": False, "error": "That email is already in use."}
    # 'member' marks an invited teammate. The role column's existing
    # vocabulary is 'client' (every restaurant's primary login, the default)
    # and 'owner' (Will's multi-restaurant login that can switch its active
    # restaurant — see get_session_user). The first cut of this feature
    # gated invite/revoke on role == 'owner', which no client login has, so
    # the whole Team feature was invisible and 403'd for every real account.
    # The distinction that actually matters is "primary login vs. someone
    # that login invited", and that's what this value records.
    set_user_role(user_id, "member", db_path=db_path)
    # An invited teammate also gets a membership, so authorization for this
    # login resolves through the same Identity → Tenant → Role path every
    # other account now uses rather than falling back to users.role.
    try:
        upsert_membership(user_id, restaurant_id, "member", employee_name=name,
                          db_path=db_path)
    except Exception as exc:
        # The login still works (get_session_user falls back to users.role),
        # but a missing membership means this teammate is invisible to the
        # staff roster and to any future per-location assignment — worth
        # knowing about rather than discovering later.
        try:
            import ops as _ops_invite
            _ops_invite.capture(exc, job="invite_membership",
                                context=f"user_id={user_id} restaurant_id={restaurant_id}")
        except Exception:
            print(f"[invite_team_member] membership write failed for {user_id}: {exc}")
    return {"ok": True, "user_id": user_id, "username": candidate, "temp_password": temp_password}


def set_can_manage_team(restaurant_id: int, user_id: int, allowed: bool,
                        db_path: str = DB_PATH) -> bool:
    """Turn the team-management override on or off for one login.

    Scoped by restaurant_id so an owner can only ever change a login on their
    own restaurant, and never the tenant next door by guessing a user id.
    """
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE users SET can_manage_team=? WHERE id=? AND restaurant_id=?",
            (1 if allowed else 0, user_id, restaurant_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_team_members(restaurant_id: int, db_path: str = DB_PATH) -> list[dict]:
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT id, username, email, role, created_at, last_login, is_active
        FROM users WHERE restaurant_id=? AND is_active=1
        ORDER BY created_at
    """, (restaurant_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def revoke_team_member(restaurant_id: int, user_id: int, acting_user_id: int,
                       db_path: str = DB_PATH) -> dict:
    """Deactivates a teammate's login and kills their sessions immediately —
    revocation shouldn't wait for a 30-day token to expire on its own.
    Guards against the three ways this could go wrong: revoking someone
    from a *different* restaurant entirely (cross-tenant safety — restaurant_id
    is always the acting owner's own, never trusted from the request body),
    revoking yourself, and revoking the last remaining active login for a
    restaurant (which would lock everyone out, including the owner)."""
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT id FROM users WHERE id=? AND restaurant_id=? AND is_active=1",
        (user_id, restaurant_id)
    ).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "That teammate wasn't found."}
    if user_id == acting_user_id:
        conn.close()
        return {"ok": False, "error": "You can't revoke your own access."}
    active_count = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE restaurant_id=? AND is_active=1",
        (restaurant_id,)
    ).fetchone()["n"]
    if active_count <= 1:
        conn.close()
        return {"ok": False, "error": "Can't remove the only remaining login."}
    conn.execute("UPDATE users SET is_active=0 WHERE id=?", (user_id,))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


def list_users(db_path: str = DB_PATH) -> list[dict]:
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT u.*, r.name as restaurant_name
        FROM users u
        JOIN restaurants r ON u.restaurant_id = r.id
        ORDER BY u.created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── Session management ────────────────────────────────────────────────────────

def hash_session_token(token: str) -> str:
    """What actually goes in the sessions table.

    Tokens used to be stored verbatim, which made any copy of the database a
    ring of live master keys: the daily backup emailed every logged-in
    owner's bearer token off the server in plaintext, and anything that could
    read the file could impersonate any user for the full 30-day session life
    without a password or a 2FA prompt. Only the hash is stored now, so a
    database disclosure no longer yields anything replayable.

    SHA-256 rather than scrypt/bcrypt deliberately: this runs on every
    authenticated request, and the input is 256 bits of secrets.token_urlsafe
    entropy, not a human-chosen password — there is no dictionary to attack,
    so a slow KDF would buy nothing and cost latency on the hot path.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int, days: int = 30,
                   ip_address: str = None, user_agent: str = None,
                   device_type: str = "web", device_id: str = None,
                   restaurant_id: int = None, hours: int = None,
                   db_path: str = DB_PATH) -> str:
    """Every call used to unconditionally INSERT a new row, so a device that
    just re-logs in (session expired, signed out, reinstalled) piled up a
    fresh row every time — the Devices list in Account then showed several
    "different" devices that were really the same phone signing in
    repeatedly, since nothing here ever expired for 30 days. `device_id` is
    a UUID the client generates once and persists (Keychain on iOS — see
    DeviceIdentity.swift), sent on every login-family request; when present,
    any of THIS user's existing sessions for that same device are replaced
    rather than added to. Web logins (and any client that doesn't send one)
    keep the old accumulate-until-expiry behavior, since there's no stable
    per-device identity to key off there."""
    token = secrets.token_urlsafe(32)
    from datetime import timedelta
    # `hours` is the staff-PIN path: a shift-length session rather than a
    # month, because those run on shared devices sitting on a pass or a host
    # stand. Everything else about the row is identical.
    span = timedelta(hours=hours) if hours else timedelta(days=days)
    expires = (datetime.now(timezone.utc) + span).isoformat()
    conn = get_conn(db_path)
    # Prune expired sessions for this user (keep active ones for multi-device support)
    conn.execute("DELETE FROM sessions WHERE user_id=? AND expires_at <= datetime('now')", (user_id,))
    if device_id:
        conn.execute("DELETE FROM sessions WHERE user_id=? AND device_id=?", (user_id, device_id))
    conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at, ip_address, user_agent, device_type, device_id) VALUES (?,?,?,?,?,?,?)",
        (hash_session_token(token), user_id, expires, ip_address or "", user_agent or "", device_type, device_id or "")
    )
    conn.execute(
        "INSERT INTO login_history (user_id, restaurant_id, event, ip_address, user_agent, device_type) VALUES (?,?,?,?,?,?)",
        (user_id, restaurant_id, "login", ip_address or "", user_agent or "", device_type)
    )
    conn.commit()
    conn.close()
    return token


def get_login_history(user_id: int, limit: int = 50, db_path: str = DB_PATH) -> list[dict]:
    """Every past login for this user, most recent first — unlike
    get_sessions_for_user() above, this never drops a row when its session
    later expires, gets revoked, or gets deduped by device_id, since it's
    written once at login time and never touched again."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT event, ip_address, user_agent, device_type, created_at
        FROM login_history
        WHERE user_id=?
        ORDER BY created_at DESC
        LIMIT ?
    """, (user_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_sessions_for_user(user_id: int, current_token: str = None,
                          db_path: str = DB_PATH) -> list:
    """Return all active sessions for a user, marking which is current."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT token, created_at, last_active, ip_address, user_agent, device_type
        FROM sessions
        WHERE user_id=? AND expires_at > datetime('now')
        ORDER BY last_active DESC
    """, (user_id,)).fetchall()
    conn.close()
    result = []
    for row in rows:
        result.append({
            # The stored value is a hash now, so the "hint" is just a stable
            # opaque handle for the UI, never part of the real token.
            "token_hint": row["token"][-6:],
            "is_current": bool(current_token) and row["token"] == hash_session_token(current_token),
            "created_at": row["created_at"],
            "last_active": row["last_active"],
            "ip_address": row["ip_address"] or "",
            "user_agent": row["user_agent"] or "",
            "device_type": row["device_type"] or "web",
        })
    return result


def revoke_other_sessions(user_id: int, current_token: str,
                          db_path: str = DB_PATH):
    """Delete all sessions for a user except the current one."""
    conn = get_conn(db_path)
    conn.execute("DELETE FROM sessions WHERE user_id=? AND token!=?",
                 (user_id, hash_session_token(current_token or "")))
    conn.commit()
    conn.close()

INACTIVITY_HOURS = 8  # Log out after 8 hours of inactivity

def get_session_user(token: str, db_path: str = DB_PATH) -> Optional[dict]:
    if not token:
        return None
    conn = get_conn(db_path)
    row = conn.execute("""
        SELECT u.*, s.last_active, s.active_restaurant_id, s.device_type FROM sessions s
        JOIN users u ON s.user_id = u.id
        WHERE s.token=? AND s.expires_at > datetime('now') AND u.is_active=1
    """, (hash_session_token(token),)).fetchone()
    if not row:
        conn.close()
        return None
    # Check inactivity timeout — exempt iOS sessions. The web session's
    # 8-hour inactivity window is a reasonable "walked away from the laptop"
    # rule, but it would log a phone out (2FA and all) every single time an
    # owner checked the app once a day. iOS sessions rely on the 30-day hard
    # expiry above plus the app's own Face ID lock on foreground instead.
    is_ios_session = (row["device_type"] or "web") == "ios"
    last_active = row["last_active"] or ""
    if last_active and not is_ios_session:
        try:
            from datetime import datetime, timedelta
            la = datetime.fromisoformat(last_active[:19])  # always naive UTC, drops any tz suffix
            now_utc = datetime.utcnow()
            if now_utc - la > timedelta(hours=INACTIVITY_HOURS):
                # Session expired due to inactivity — delete it
                conn.execute("DELETE FROM sessions WHERE token=?", (hash_session_token(token),))
                conn.commit()
                conn.close()
                return None
        except Exception as e:
            # A last_active this can't parse means the 8-hour inactivity
            # window silently stops applying to that session — a security
            # control quietly switching itself off is exactly the kind of
            # failure that must not be invisible.
            try:
                import ops
                ops.capture(e, job="session_inactivity_check",
                            context=f"last_active={last_active!r}")
            except Exception:
                pass
    # Update last_active timestamp
    conn.execute("UPDATE sessions SET last_active=datetime('now') WHERE token=?", (hash_session_token(token),))
    conn.commit()
    conn.close()
    user = dict(row)
    # For owners, active_restaurant_id in session overrides their base restaurant_id
    if user.get("role") == "owner" and user.get("active_restaurant_id"):
        user["base_restaurant_id"] = user["restaurant_id"]
        user["restaurant_id"] = user["active_restaurant_id"]
    else:
        user["base_restaurant_id"] = user["restaurant_id"]

    # Dual-read: the membership for the restaurant this session is ACTING in
    # is the authority on role, because that is the whole point of separating
    # identity from authorization — the same person can be a manager at one
    # location and an employee at another, and users.role cannot express that.
    #
    # It is a fallback, not a requirement: a session whose membership row is
    # missing (backfill hasn't run, or a brand-new login racing it) keeps the
    # users.role it has always had, so nothing can be locked out by this
    # table's absence. The membership is also what the staff routes read to
    # resolve an employee's own name, so it is attached either way.
    try:
        membership = get_membership(user["id"], user["restaurant_id"], db_path=db_path)
    except Exception:
        membership = None
    if membership:
        user["role"] = membership["role"]
        user["membership_id"] = membership["id"]
        user["employee_name"] = membership.get("employee_name")
    else:
        user["membership_id"] = None
        user["employee_name"] = None
    return user

def switch_active_restaurant(token: str, restaurant_id: int, db_path: str = DB_PATH):
    """Set the active restaurant for an owner's session."""
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE sessions SET active_restaurant_id=? WHERE token=?",
        (restaurant_id, hash_session_token(token))
    )
    conn.commit()
    conn.close()

def set_user_role(user_id: int, role: str, db_path: str = DB_PATH):
    """Set role on a user: 'client' or 'owner'."""
    conn = get_conn(db_path)
    conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
    conn.commit()
    conn.close()

def delete_session(token: str, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM sessions WHERE token=?", (hash_session_token(token),))
    conn.commit()
    conn.close()

def update_last_login(user_id: int, db_path: str = DB_PATH):
    from zoneinfo import ZoneInfo as _ZI_a
    """Update last_login timestamp for a user."""
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE users SET last_login=? WHERE id=?",
        (datetime.now(_ZI_a('America/Chicago')).strftime('%Y-%m-%dT%H:%M:%S'), user_id)
    )
    conn.commit()
    conn.close()

def get_current_user():
    """Get the current logged-in user from session cookie."""
    from flask import request
    token = request.cookies.get("session_token")
    if not token:
        return None
    return get_session_user(token)

def _wants_json_response():
    """True for the AJAX/fetch calls the dashboard makes constantly (POST/DELETE
    mutations, and any GET to an /api/ route), False for a real page navigation.
    A redirect-to-login on an expired-session fetch() call used to come back as
    a 302 to an HTML login page; fetch() follows it silently and .json() then
    throws, surfacing as a generic "Request failed" toast with no indication
    the session actually expired. Returning real JSON here lets the existing
    error handling show something meaningful instead."""
    if request.method != "GET":
        return True
    return request.path.startswith("/api/")

# Paths that stay reachable after a subscription lapses. A locked-out owner
# must still be able to see why, pay, export their data, and sign out —
# otherwise the block is indistinguishable from a broken app and there is no
# self-service route back to paying.
_BILLING_EXEMPT_PREFIXES = (
    "/login", "/logout", "/health", "/static/", "/privacy", "/terms",
    "/api/billing-info", "/account", "/mobile/api/account", "/mobile/api/login",
    "/mobile/api/logout", "/mobile/api/me", "/mobile/api/forgot-password",
    "/mobile/api/reset-password", "/admin",
)


def _billing_blocked(user) -> bool:
    """True when this request should be refused for a lapsed subscription."""
    try:
        if not user or user.get("is_admin"):
            return False
        path = request.path or ""
        if any(path.startswith(p) for p in _BILLING_EXEMPT_PREFIXES):
            return False
        from models import subscription_allows_access
        return not subscription_allows_access(user["restaurant_id"])
    except Exception:
        # Fail open — see models.subscription_allows_access.
        return False


_BILLING_BLOCKED_MESSAGE = (
    "This subscription is no longer active. Contact will@cavnar.ai to reactivate."
)


# ── module entitlement ──────────────────────────────────────────────────────
#
# Modules are sold separately (see pricing.py), and the dashboard and the app
# both hide the tabs a client hasn't bought. Hiding is not enforcing: nothing
# stopped a Reviews-only client from calling the Labor, Food Cost, Marketing
# or Intel endpoints directly and getting the whole feature, Claude calls
# included, at Cavnar's cost.
#
# The gate lives here rather than as a per-route decorator for the same
# reason the billing check does: the decorator has already resolved the user,
# a new route under an existing prefix is covered the day it is written, and
# there is one table to audit instead of ninety decorators to keep in sync.
#
# Longest prefix wins, so a more specific path can opt out of its family's
# module by listing itself with a different one. Anything not listed is
# ungated — Home, Ask Cavnar, Account, notifications and the public token
# pages deliberately span or sit outside the modules.
_MODULE_PREFIXES = (
    # Reviews
    ("/api/reviews",                "reviews"),
    ("/api/review-stats",           "reviews"),
    ("/api/review-insight",         "reviews"),
    ("/api/review-request-stats",   "reviews"),
    ("/api/send-review-request",    "reviews"),
    ("/api/response-performance",   "reviews"),
    ("/api/topic-heatmap",          "reviews"),
    ("/api/sentiment-trend",        "reviews"),
    ("/api/regenerate-draft",       "reviews"),
    ("/api/save-draft",             "reviews"),
    ("/api/import-tripadvisor",     "reviews"),
    ("/api/templates",              "reviews"),
    ("/api/brand-voice",            "reviews"),
    ("/approve/",                   "reviews"),
    ("/skip/",                      "reviews"),
    ("/undo/",                      "reviews"),
    ("/retract/",                   "reviews"),
    ("/mobile/api/reviews",         "reviews"),
    ("/mobile/api/review-stats",    "reviews"),
    ("/mobile/api/review-request-stats", "reviews"),
    ("/mobile/api/send-review-request",  "reviews"),
    ("/mobile/api/templates",       "reviews"),
    # Labor
    ("/api/labor",                  "labor"),
    ("/api/generate-schedule",      "labor"),
    ("/api/schedule-status",        "labor"),
    ("/api/download-schedule",      "labor"),
    ("/mobile/api/labor",           "labor"),
    # Food Cost
    ("/api/food-cost",              "inventory"),
    ("/api/inv-insight",            "inventory"),
    ("/mobile/api/food-cost",       "inventory"),
    # Marketing
    ("/api/marketing/",             "marketing"),
    ("/api/mkt-",                   "marketing"),
    ("/api/generate-content",       "marketing"),
    ("/api/content-calendar",       "marketing"),
    ("/api/recent-topics",          "marketing"),
    ("/api/post-to-google",         "marketing"),
    ("/api/gbp-listing",            "marketing"),
    ("/api/guest-",                 "marketing"),
    ("/mobile/api/marketing",       "marketing"),
    ("/mobile/api/guest-",          "marketing"),
    # Intel (derived: full tier + a connected listing)
    ("/api/intel/",                 "intel"),
    ("/api/ai-visibility",          "intel"),
    ("/mobile/api/intel",           "intel"),
)


def _required_module(path: str):
    """The module this path belongs to, or None. Longest match wins."""
    best = None
    for prefix, key in _MODULE_PREFIXES:
        if path.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, key)
    return best[1] if best else None


def _module_blocked(user):
    """The module label to refuse this request for, or None to allow it."""
    try:
        if not user or user.get("is_admin"):
            return None
        key = _required_module(request.path or "")
        if not key:
            return None
        from models import module_label, restaurant_has_module
        if restaurant_has_module(user["restaurant_id"], key):
            return None
        return module_label(key)
    except Exception:
        # Fail open — a lookup failure must not take a paying client's tab
        # away. See models.restaurant_has_module.
        return None


def _module_blocked_message(label):
    return f"{label} isn't part of this plan. Contact will@cavnar.ai to add it."


_STAFF_WRONG_DOOR = ("This is the owner dashboard. Open your staff portal link "
                     "to see your shifts and tasks.")


def _console_denied(user):
    """True when this identity may not use the owner/manager console at all.

    This is the gate that makes an employee tier safe to add. Employee PIN
    sessions are real sessions in the same table, resolved by the same
    get_session_user(), so WITHOUT this check a PIN would open every one of
    the ~370 routes behind these two decorators — labor analytics, food cost,
    financials, the review inbox, billing, the lot.

    It is checked per-request rather than at login because a role can be
    changed (or a membership deactivated) while a session is live, and the
    next request must respect that rather than waiting 30 days for expiry.

    Deliberately NOT modelled on _module_blocked/_billing_blocked, both of
    which fail OPEN on exception. Those protect revenue; this protects data,
    so it fails CLOSED — an identity whose permissions can't be resolved does
    not get the console.
    """
    try:
        from permissions import DASHBOARD_ACCESS, has_permission
        return not has_permission(user, DASHBOARD_ACCESS)
    except Exception:
        return True


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            if _wants_json_response():
                from flask import jsonify as _jsonify_lr
                return _jsonify_lr(ok=False, error="Your session expired — please log in again.", session_expired=True), 401
            return redirect(url_for("auth.login", next=request.path))
        if _console_denied(user):
            if _wants_json_response():
                from flask import jsonify as _jsonify_cd
                return _jsonify_cd(ok=False, error=_STAFF_WRONG_DOOR, staff_account=True), 403
            return redirect(url_for("staff.portal_home"))
        if _billing_blocked(user):
            from flask import jsonify as _jsonify_bb
            return _jsonify_bb(ok=False, error=_BILLING_BLOCKED_MESSAGE,
                               billing_inactive=True), 402
        locked = _module_blocked(user)
        if locked:
            from flask import jsonify as _jsonify_ml
            return _jsonify_ml(ok=False, error=_module_blocked_message(locked),
                               module_locked=True, module=locked), 403
        return f(*args, **kwargs, current_user=user)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user or not user["is_admin"]:
            if _wants_json_response():
                from flask import jsonify as _jsonify_ar
                return _jsonify_ar(ok=False, error="Your session expired — please log in again.", session_expired=True), 401
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs, current_user=user)
    return decorated

def mobile_login_required(f):
    """Bearer-token variant of login_required for the iOS app's /mobile/api/
    routes. There's no cookie jar to read from on a native client, so this
    reads Authorization: Bearer <token> instead — everything else about the
    session (30-day expiry, owner active_restaurant_id overlay, the iOS
    inactivity-timeout exemption above) is identical, since both decorators
    resolve through the same get_session_user(). Always responds with JSON;
    this blueprint has no HTML page to redirect an expired session to."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
        user = get_session_user(token) if token else None
        if not user:
            from flask import jsonify as _jsonify_mlr
            return _jsonify_mlr(ok=False, error="Your session expired — please log in again.", session_expired=True), 401
        # Same console gate as the web decorator — the iOS app ships both the
        # owner dashboard and the staff portal against this one blueprint, so
        # a PIN session must be refused here too or the whole owner API is
        # reachable from the staff build.
        if _console_denied(user):
            from flask import jsonify as _jsonify_mcd
            return _jsonify_mcd(ok=False, error=_STAFF_WRONG_DOOR, staff_account=True), 403
        if _billing_blocked(user):
            from flask import jsonify as _jsonify_mbb
            return _jsonify_mbb(ok=False, error=_BILLING_BLOCKED_MESSAGE,
                                billing_inactive=True), 402
        locked = _module_blocked(user)
        if locked:
            from flask import jsonify as _jsonify_mml
            return _jsonify_mml(ok=False, error=_module_blocked_message(locked),
                                module_locked=True, module=locked), 403
        return f(*args, **kwargs, current_user=user)
    return decorated


# ═══════════════════════════════════════════════════════════════════════
# Trusted devices (2FA "remember this device"), login reports, recovery
# email, forced password reset
# ═══════════════════════════════════════════════════════════════════════

def describe_user_agent(user_agent: str) -> str:
    ua = user_agent or ""
    if "Cavnar-iOS" in ua or "CavnarAI" in ua: return "iPhone app"
    if "iPhone" in ua: return "iPhone"
    if "iPad" in ua: return "iPad"
    if "Android" in ua: return "Android"
    if "Windows" in ua: return "Windows"
    if "Macintosh" in ua or "Mac OS" in ua: return "Mac"
    return "Device"


def _hash_device_token(token: str) -> str:
    import hashlib
    return hashlib.sha256(("cavnar-trusted:" + token).encode()).hexdigest()


def create_trusted_device(restaurant_id: int, user_id: int | None, label: str,
                          days: int = 30, db_path: str = DB_PATH) -> str:
    """Mints a new remember-token, stores only its hash, returns the token."""
    import secrets
    from datetime import datetime, timedelta
    token = secrets.token_hex(32)
    expires = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    conn.execute("""
        INSERT INTO trusted_devices (restaurant_id, user_id, token_hash, label, expires_at)
        VALUES (?,?,?,?,?)
    """, (restaurant_id, user_id, _hash_device_token(token), label, expires))
    conn.commit()
    conn.close()
    return token


def trusted_device_ok(restaurant_id: int, token: str, db_path: str = DB_PATH) -> bool:
    """True when `token` is a live remembered device for this restaurant.
    Also honours the legacy single-slot restaurants.two_fa_device_token so
    nobody gets re-prompted just because this table appeared."""
    if not token:
        return False
    conn = get_conn(db_path)
    try:
        row = conn.execute("""
            SELECT id FROM trusted_devices
            WHERE restaurant_id=? AND token_hash=? AND expires_at > datetime('now')
        """, (restaurant_id, _hash_device_token(token))).fetchone()
        if row:
            conn.execute("UPDATE trusted_devices SET last_used_at=datetime('now') WHERE id=?", (row["id"],))
            conn.commit()
            return True
        legacy = conn.execute("SELECT two_fa_device_token FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        return bool(legacy and legacy["two_fa_device_token"] and legacy["two_fa_device_token"] == token)
    finally:
        conn.close()


def get_trusted_devices(restaurant_id: int, db_path: str = DB_PATH) -> list[dict]:
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT id, label, created_at, last_used_at, expires_at FROM trusted_devices
        WHERE restaurant_id=? AND expires_at > datetime('now') ORDER BY created_at DESC, id DESC
    """, (restaurant_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def revoke_trusted_device(restaurant_id: int, device_id: int, db_path: str = DB_PATH) -> bool:
    conn = get_conn(db_path)
    cur = conn.execute("DELETE FROM trusted_devices WHERE id=? AND restaurant_id=?", (device_id, restaurant_id))
    conn.commit()
    conn.close()
    return (cur.rowcount or 0) > 0


def revoke_all_trusted_devices(restaurant_id: int, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM trusted_devices WHERE restaurant_id=?", (restaurant_id,))
    # The legacy single slot too, or the last-remembered device stays remembered.
    conn.execute("UPDATE restaurants SET two_fa_device_token=NULL WHERE id=?", (restaurant_id,))
    conn.commit()
    conn.close()


def create_login_report(user_id: int, session_token: str | None, db_path: str = DB_PATH) -> str:
    import secrets
    token = secrets.token_urlsafe(32)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO login_reports (token, user_id, session_token) VALUES (?,?,?)",
                 (token, user_id, session_token))
    conn.commit()
    conn.close()
    return token


def consume_login_report(token: str, db_path: str = DB_PATH) -> dict | None:
    """'This wasn't me': revokes EVERY session for the user (not just the
    reported one — if one sign-in was an attacker, assume the password is
    burned), forgets every trusted device, and flags the account so the
    next sign-in is refused until the password is reset. Returns the user
    row, or None for an unknown / already-used / stale (>7 day) link."""
    conn = get_conn(db_path)
    row = conn.execute("""
        SELECT user_id FROM login_reports
        WHERE token=? AND used_at IS NULL AND created_at > datetime('now', '-7 days')
    """, (token,)).fetchone()
    if not row:
        conn.close()
        return None
    user_id = row["user_id"]
    conn.execute("UPDATE login_reports SET used_at=datetime('now') WHERE token=?", (token,))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.execute("UPDATE users SET must_reset_password=1 WHERE id=?", (user_id,))
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if user and user["restaurant_id"]:
        conn.execute("DELETE FROM trusted_devices WHERE restaurant_id=?", (user["restaurant_id"],))
        conn.execute("UPDATE restaurants SET two_fa_device_token=NULL WHERE id=?", (user["restaurant_id"],))
    conn.commit()
    conn.close()
    return dict(user) if user else None


def clear_must_reset_password(user_id: int, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("UPDATE users SET must_reset_password=0 WHERE id=?", (user_id,))
    conn.commit()
    conn.close()


def start_recovery_email(user_id: int, email: str, db_path: str = DB_PATH) -> str:
    """Stores the address as pending and returns the 6-digit code that has
    to come back through verify_recovery_email before it counts."""
    import random
    from datetime import datetime, timedelta
    code = str(random.randint(100000, 999999))
    expires = (datetime.utcnow() + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    conn.execute("""
        UPDATE users SET recovery_email_pending=?, recovery_email_code=?, recovery_email_expires=? WHERE id=?
    """, (email.lower().strip(), code, expires, user_id))
    conn.commit()
    conn.close()
    return code


def verify_recovery_email(user_id: int, code: str, db_path: str = DB_PATH) -> str | None:
    conn = get_conn(db_path)
    row = conn.execute("""
        SELECT recovery_email_pending, recovery_email_code, recovery_email_expires FROM users WHERE id=?
    """, (user_id,)).fetchone()
    if (not row or not row["recovery_email_pending"] or not row["recovery_email_code"]
            or row["recovery_email_code"] != (code or "").strip()
            or not row["recovery_email_expires"] or row["recovery_email_expires"] < __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")):
        conn.close()
        return None
    email = row["recovery_email_pending"]
    conn.execute("""
        UPDATE users SET recovery_email=?, recovery_email_pending=NULL, recovery_email_code=NULL,
                         recovery_email_expires=NULL WHERE id=?
    """, (email, user_id))
    conn.commit()
    conn.close()
    return email


def remove_recovery_email(user_id: int, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""
        UPDATE users SET recovery_email=NULL, recovery_email_pending=NULL, recovery_email_code=NULL,
                         recovery_email_expires=NULL WHERE id=?
    """, (user_id,))
    conn.commit()
    conn.close()
