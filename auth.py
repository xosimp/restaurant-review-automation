from flask import redirect, url_for, request
from functools import wraps
"""
auth.py — User authentication for the Cavnar AI hosted dashboard
Handles: user table, password hashing, session management, login/logout
"""
import hashlib
import os
import re
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
-- ON DELETE CASCADE so a membership that is ever hard-deleted takes its
-- attempt counter with it. Memberships are soft-deleted today (is_active=0),
-- so this is belt-and-braces for a future hard delete — and databases created
-- before this clause exists keep the un-cascaded definition, which is why
-- init_auth() also sweeps orphans on every boot.
CREATE TABLE IF NOT EXISTS membership_pin_attempts (
    membership_id   INTEGER PRIMARY KEY REFERENCES memberships(id) ON DELETE CASCADE,
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

-- Per-IP throttle for the staff portal's public surface (roster reads and
-- sign-in attempts), replacing a module-level dict.
--
-- That dict had three problems and each one mattered: it reset on every
-- deploy, it was per-process so the real ceiling was 30 × gunicorn workers ×
-- replicas, and no key was ever removed, so it leaked one entry per IP
-- forever. Rows here are deleted as they age out of the window, so the table
-- is self-evicting rather than needing a sweeper.
CREATE TABLE IF NOT EXISTS portal_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ip              TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_portal_attempts_ip
    ON portal_attempts(ip, created_at);

-- Employee self-signup: phone verification, before any account exists.
--
-- The identity is NOT created here. users.restaurant_id is NOT NULL and ~390
-- routes read current_user["restaurant_id"] for tenant scoping, so an
-- identity that exists before it has a restaurant would be a brand-new
-- failure mode across the whole app. Instead the phone is verified first,
-- held here, and the users + memberships rows are written together at the
-- moment a name is claimed — by which point the restaurant is known.
--
-- Keyed by phone so one number has one live signup at a time, and rows are
-- deleted as they age out rather than needing a sweeper.
CREATE TABLE IF NOT EXISTS staff_signups (
    phone           TEXT    PRIMARY KEY,
    code_hash       TEXT    NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    sends           INTEGER NOT NULL DEFAULT 1,
    token_hash      TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_sent_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    verified_at     TEXT
);

-- One-shot tokens that make a captured PIN sign-in POST unreplayable.
--
-- The body is {membership_id, pin} and nothing else varied between requests,
-- so a captured one worked verbatim until the PIN changed. A nonce is minted
-- with the roster, spent by the sign-in, and cannot be spent twice.
CREATE TABLE IF NOT EXISTS portal_nonces (
    nonce           TEXT    PRIMARY KEY,
    restaurant_id   INTEGER NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_portal_nonces_created
    ON portal_nonces(created_at);

-- Owner-granted extras for one login at one location, on top of its role —
-- permissions.GRANTABLE only. Read on every request (get_session_user), so a
-- revoke takes effect on the manager's next click, not their next sign-in.
CREATE TABLE IF NOT EXISTS permission_grants (
    user_id         INTEGER NOT NULL REFERENCES users(id),
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    permission      TEXT    NOT NULL,
    granted_by      INTEGER,
    granted_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, restaurant_id, permission)
);

-- Per-login, per-location delivery choices. morning_brief NULL means the
-- role default (owners and managers receive it).
CREATE TABLE IF NOT EXISTS login_prefs (
    user_id         INTEGER NOT NULL REFERENCES users(id),
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    morning_brief   INTEGER,
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, restaurant_id)
);

-- One emailed/texted 2FA code per sign-in attempt (purpose 'login') or per
-- login setting 2FA up (purpose 'setup'). This used to be one slot of
-- plaintext columns on the restaurants row (two_fa_code / two_fa_pending),
-- so a manager signing in, or anyone pressing "Send test code", replaced the
-- code the owner was typing at that moment (SEC-20), and anyone who could
-- read the database could read a live code (SEC-39). Neither the pending
-- secret nor the code is stored: both are keyed hashes.
CREATE TABLE IF NOT EXISTS two_fa_challenges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    purpose         TEXT    NOT NULL DEFAULT 'login',
    pending_hash    TEXT    NOT NULL UNIQUE,
    code_hash       TEXT    NOT NULL,
    expires_at      TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_two_fa_challenges_user
    ON two_fa_challenges(restaurant_id, user_id, purpose);

-- Who opened each admin view-as session, and whether it may write. A view-as
-- opened by a read-only support login used to be a full client session that
-- could turn 2FA off or shorten review retention (SEC-12). Keyed by the
-- session's token hash, as sessions itself is.
CREATE TABLE IF NOT EXISTS view_as_sessions (
    token_hash      TEXT    PRIMARY KEY,
    opened_by       INTEGER,
    read_only       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Backup codes for a login's OWN second factor — Cavnar AI's internal logins
-- (admin, support), whose two-factor lives on the users row, not on a
-- restaurant. A restaurant's codes (two_fa_backup_codes) belong to that
-- restaurant's logins: an admin homed on a restaurant row must never pass
-- his second factor with that restaurant's codes, nor they with his.
-- Hashed like passwords; each works once.
CREATE TABLE IF NOT EXISTS user_backup_codes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    code_hash       TEXT    NOT NULL,
    used_at         TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_user_backup_codes_user
    ON user_backup_codes(user_id, used_at);

-- An internal login's authenticator-app (TOTP, RFC 6238) secret, when its
-- two-factor method is 'app' (R10, 9/29/26). Its own table, not columns on
-- users, so the secret never rides on the user dict every request loads
-- (the session lookup reads u.*). Both secrets are Fernet ciphertext under
-- CREDENTIAL_KEY (credentials.encrypt); enrolment is refused without a
-- working key. pending_secret is the one a QR code was just shown for: it
-- becomes secret only when a code from the app confirms it, and is dead
-- after pending_expires_at (15 minutes). last_step is the last 30-second
-- step a code was accepted for, so one code never signs in twice. One row
-- per login at most; emptied from every off-site backup (SCRUB_TABLES).
CREATE TABLE IF NOT EXISTS user_totp (
    user_id             INTEGER PRIMARY KEY,
    secret              TEXT,
    last_step           INTEGER,
    pending_secret      TEXT,
    pending_expires_at  TEXT,
    activated_at        TEXT,
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

# Indexes that reference columns added by the ALTER migrations below, so they
# cannot live in AUTH_SCHEMA — that script runs before the columns exist.
#
# sessions was designed for ~1 row per restaurant. The employee tier changes
# the access pattern to one row per employee per shift, and EVERY sign-in
# runs two DELETE … WHERE user_id=? statements (expiry prune, then the staff
# revoke). Unindexed, both are full scans of a table that now grows with
# headcount × shifts, and they all land at once during the pre-shift rush.
#
# login_history is append-only (it is what the Account sign-in history
# reads), so it grows fastest of all — one row per sign-in; only its
# retention window (LOGIN_HISTORY_RETENTION_DAYS, below) bounds it.
# get_login_history orders by created_at per user.
AUTH_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_sessions_user
    ON sessions(user_id, device_type);
CREATE INDEX IF NOT EXISTS idx_sessions_expires
    ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_login_history_user
    ON login_history(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_login_history_created
    ON login_history(created_at);
"""
# idx_login_history_created serves the retention prune (created_at < cutoff):
# idx_login_history_user leads with user_id, so that DELETE scanned the whole
# table under the write lock — at boot here (prune_login_history) and nightly
# once ops' retention registry carries login_history (fix round D #72).

# login_history is the only append-only table in the schema, so it is also the
# only one with no natural ceiling: 250k employees × one sign-in per shift is
# ~91M rows a year in the same SQLite file serving live traffic. 90 days is
# well past the security purpose it serves (the Account screen shows the last
# 50, and "was this me?" is a question people ask within days, not quarters).
LOGIN_HISTORY_RETENTION_DAYS = 90


# How long an admin "view as" session lasts: two hours from the moment it was
# opened, never extended by use (owner decision, 9/29/26). It was 12 hours
# sliding forward on every request, so a tab left open kept a client's
# identity alive indefinitely; the stored expiry is now the only clock.
VIEW_AS_HOURS = 2

# An admin or support session lasts 12 hours from sign-in, on every device
# (owner decision, 9/29/26). They used the owner defaults — 30 days, 8 hours
# idle — for the one login that can read and change every tenant.
ADMIN_SESSION_HOURS = 12

# Step-up: the sensitive admin actions need the password typed again within
# this many minutes (recent_auth_required, POST /admin/api/reauth).
RECENT_AUTH_MINUTES = 15


def sql_utc(dt=None) -> str:
    """A UTC instant in SQLite's own text form, 'YYYY-MM-DD HH:MM:SS' — the
    form datetime('now') produces. sessions.expires_at used to be written as
    ISO text with a 'T' and an offset, and '2026-09-29T08:00…' compares
    greater than '2026-09-29 21:00:00' as text, so every session outlived its
    expiry to the end of that UTC day (SECURITY-14)."""
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_utc(value):
    """A stored UTC stamp (either text form) as a naive UTC datetime, or None."""
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        try:
            return datetime.fromisoformat(str(value)[:19])
        except Exception:
            return None


def is_internal_login(user) -> bool:
    """Cavnar AI's own staff: an admin, or a read-only support login. Their
    second factor, session lifetime and sign-in throttling follow rules of
    their own, because one of these logins reaches every tenant."""
    if not user:
        return False
    try:
        if user.get("is_admin"):
            return True
        return str(user.get("role") or "").strip().lower() == "support"
    except Exception:
        return False

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
        # The employee's JOB title ("Bartender"), as distinct from their
        # AUTHORIZATION role ("employee"). It used to be derived per request
        # from shift data via a loader that falls back to bundled SAMPLE
        # shifts — so at a restaurant with no uploaded schedule, an employee
        # whose name collided with a fixture was handed that fixture's job
        # title, and the job title decides which task checklist they may
        # complete. Authorization must never be derived from invented data.
        "ALTER TABLE memberships ADD COLUMN job_role TEXT",
        # Employee self-signup. The phone is the identity: it is how a
        # returning employee is recognised at a second restaurant (one person,
        # two memberships) rather than ending up with two accounts, and it is
        # what an owner looks at to decide whether a claimed name is really
        # that person.
        "ALTER TABLE users ADD COLUMN phone TEXT",
        "ALTER TABLE memberships ADD COLUMN claimed_by_phone TEXT",
        "ALTER TABLE memberships ADD COLUMN claimed_at TEXT",
        # The employee's own "text me when my schedule is posted" — set from
        # an unchecked-by-default box on the staff portal, cleared by
        # unticking it. The signup text consent covers the one-time code
        # only; a schedule text needs this (people.reach, Friction #17).
        "ALTER TABLE memberships ADD COLUMN schedule_texts_at TEXT",
        # A short, typeable version of the portal token. The 32-character URL
        # token is fine to tap in a link and miserable to read off a whiteboard
        # and type on a phone, which is exactly what signup asks people to do.
        "ALTER TABLE staff_portal_tokens ADD COLUMN join_code TEXT",
        # The restaurant a staff-PIN session was minted for. A PIN identity
        # with memberships at two restaurants was resolved against its HOME
        # restaurant (users.restaurant_id) whichever tablet it signed in on,
        # so the wrong restaurant's schedule came up — and once the home
        # restaurant unlinked it, the session fell back to users.role 'client'
        # there and opened that owner console (SEC-1).
        "ALTER TABLE sessions ADD COLUMN staff_restaurant_id INTEGER",
        # SEC-19: a lockout's length escalates with the lockouts before it
        # that day, so slow online guessing against one PIN stops paying off.
        "ALTER TABLE membership_pin_attempts ADD COLUMN lockout_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE membership_pin_attempts ADD COLUMN last_locked_at TEXT",
        # SEC-18: a portal hit that turned out fine (a sign-in that worked, a
        # roster read with a real code) no longer spends the failure budget.
        "ALTER TABLE portal_attempts ADD COLUMN ok INTEGER NOT NULL DEFAULT 0",
        # Fix round A (9/29/26). What a session proved and who is behind it,
        # on the session row itself so every check fails closed:
        #   two_factor_at   — when this session passed a second factor (a
        #                     code, a backup code or a remembered device);
        #                     the admin gate reads it (SECURITY-1).
        #   reauth_at       — when the password was last typed for this
        #                     session: at sign-in, or again for a sensitive
        #                     admin action (recent_auth_required).
        #   acting_admin_id — the admin (or support login) behind a view-as
        #                     session; every write through it is theirs.
        #   read_only       — a view-as a support login opened. It lived only
        #                     in view_as_sessions, pruned after two days, and
        #                     a missing row read as writable (SECURITY-14).
        "ALTER TABLE sessions ADD COLUMN two_factor_at TEXT",
        "ALTER TABLE sessions ADD COLUMN reauth_at TEXT",
        "ALTER TABLE sessions ADD COLUMN acting_admin_id INTEGER",
        "ALTER TABLE sessions ADD COLUMN read_only INTEGER NOT NULL DEFAULT 0",
        # An internal login's (admin, support) own second factor. It used to
        # be the flag on whatever restaurant the admin happened to be homed
        # on — a client's, if the seed attached him to one — and nothing at
        # sign-in ever asked for it (SECURITY-1). A restaurant's logins keep
        # the restaurant-wide switch.
        "ALTER TABLE users ADD COLUMN two_fa_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN two_fa_method TEXT",
        # The Privacy Policy / Terms change this login dismissed the
        # dashboard notice for (policy_notice.POLICY_UPDATED_ON, ISO) — per
        # login, so one tap holds on every device and location, and a later
        # policy change (a new date) is shown again.
        "ALTER TABLE users ADD COLUMN policy_notice_dismissed TEXT",
    ]:
        try:
            import sqlite3 as _sql
            conn_m = _sql.connect(db_path)
            conn_m.execute(col_sql)
            conn_m.commit()
            conn_m.close()
        except Exception:
            pass  # Column already exists

    # A PIN identity is an employee, never a console login. They were created
    # with users.role's column default 'client', which is what the session
    # fell back to whenever no active membership resolved (SEC-1, SEC-29).
    try:
        import sqlite3 as _sql_bf
        conn_bf = _sql_bf.connect(db_path)
        conn_bf.execute("UPDATE users SET role='employee' WHERE email LIKE '%@staff.invalid' "
                        "AND (role IS NULL OR role='client')")
        conn_bf.commit()
        conn_bf.close()
    except Exception:
        pass

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

    # Indexes last: they name columns the ALTERs above add.
    try:
        conn_i = sqlite3.connect(db_path)
        conn_i.executescript(AUTH_INDEXES)
        conn_i.commit()
        conn_i.close()
    except Exception as exc:
        # Not fatal — the app runs correctly without them, just slowly — but a
        # silently missing index is exactly the kind of thing that is only
        # discovered under load, so it must be visible at boot.
        print(f"[auth] index creation failed: {exc}")

    # The 2FA code and pending secret used to live in plaintext on the
    # restaurants row (SEC-20/SEC-39). Nothing reads those columns any more
    # (two_fa_challenges replaced them); blank any value left from before so
    # no live code sits in the file or in a backup of it.
    try:
        conn_2fa = sqlite3.connect(db_path)
        conn_2fa.execute("UPDATE restaurants SET two_fa_code=NULL, two_fa_expires=NULL, two_fa_pending=NULL "
                         "WHERE COALESCE(two_fa_code,'')!='' OR COALESCE(two_fa_pending,'')!=''")
        conn_2fa.commit()
        conn_2fa.close()
    except Exception:
        pass  # restaurants not created yet (init_db runs first at boot)

    normalize_session_rows(db_path=db_path)
    backfill_memberships(db_path=db_path)
    prune_login_history(db_path=db_path)
    sweep_orphan_pin_attempts(db_path=db_path)

    # A missing pepper silently downgrades every staff PIN to something a
    # leaked database makes trivially brute-forceable. It must not be a
    # condition you only discover by reading the source.
    try:
        health = pin_pepper_health(db_path=db_path)
        if not health["ok"]:
            print(f"[auth] PIN PEPPER WARNING: {health['message']}")
    except Exception:
        pass
    # memberships is name-keyed (people.NAME_STORES) but created here, after
    # init_db's init_people ran: give it its person_id on the first boot too.
    try:
        import people as _people_cols
        _people_cols.ensure_person_id_columns(db_path)
    except Exception as e:
        print(f"[auth] person_id columns not added: {e}")


def normalize_session_rows(db_path: str = DB_PATH) -> dict:
    """Boot-time repair of session rows written under the old rules.
    Idempotent, and a no-op once every row is in the current shape.

      - expires_at / last_active written as ISO text ('…T…+00:00') are
        rewritten in SQLite's 'YYYY-MM-DD HH:MM:SS' UTC form. Compared as
        text with datetime('now'), the ISO form kept every session alive to
        the end of its expiry day in UTC (SECURITY-14). strftime() reads the
        offset and converts to UTC; an unreadable value is left alone (the
        session lookup compares through datetime(), which refuses it).
      - admin view-as sessions from before acting_admin_id existed are ended:
        nothing on them says who is behind them or whether they may write,
        and they slid forward 12 hours on every request.
      - an internal login's session is capped at ADMIN_SESSION_HOURS from
        when it was created (they were 30-day rows).
    Returns the row counts, for the test."""
    out = {"expiry_fixed": 0, "view_as_ended": 0, "internal_capped": 0}
    try:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                "UPDATE sessions SET expires_at=COALESCE(strftime('%Y-%m-%d %H:%M:%S', expires_at), expires_at) "
                "WHERE expires_at LIKE '%T%'")
            out["expiry_fixed"] = cur.rowcount or 0
            conn.execute(
                "UPDATE sessions SET last_active=COALESCE(strftime('%Y-%m-%d %H:%M:%S', last_active), last_active) "
                "WHERE last_active LIKE '%T%'")
            cur = conn.execute("DELETE FROM sessions WHERE device_type='admin-view-as' AND acting_admin_id IS NULL")
            out["view_as_ended"] = cur.rowcount or 0
            cur = conn.execute(
                "UPDATE sessions SET expires_at=datetime(created_at, ?) "
                "WHERE user_id IN (SELECT id FROM users WHERE is_admin=1 OR LOWER(COALESCE(role,''))='support') "
                "AND COALESCE(device_type,'web')<>'admin-view-as' "
                "AND datetime(expires_at) > datetime(created_at, ?)",
                (f"+{ADMIN_SESSION_HOURS} hours", f"+{ADMIN_SESSION_HOURS} hours"))
            out["internal_capped"] = cur.rowcount or 0
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        # Not fatal (the session lookup enforces all three on its own), but
        # visible: a boot that could not repair these should be known.
        print(f"[auth] session normalization skipped: {exc}")
    return out


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
        # Through get_conn like every other write path, so PRAGMA
        # foreign_keys=ON applies here too. Harmless for an INSERT…SELECT off
        # users, but a migration that writes under different pragmas than the
        # app is a footgun waiting for the first backfill that isn't harmless.
        conn = get_conn(db_path)
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


def prune_login_history(days: int = None, db_path: str = DB_PATH) -> int:
    """Drop sign-in history older than the retention window.

    login_history is deliberately append-only — it is what survives a session
    being expired, revoked or deduped, which is the whole reason it exists
    separately from `sessions`. "Append-only" was never meant to mean
    "unbounded"; a staff tier writes a row per employee per shift, so the
    table outgrows everything else in the file. Runs at boot; returns how many
    rows it removed so a test can assert the window is actually applied.
    """
    if days is None:
        # The one registry's window (ops, RETAIN_LOGIN_HISTORY_DAYS) — never
        # a second number of its own that could disagree with the nightly
        # prune — and nothing at all when that window is off or refused as
        # under its floor (memory audit 9/29/26, "retention_registry").
        # Before rows go at boot, the month's sign-ins are summarised the
        # same as the nightly pass does (history_rollups.roll_engagement).
        try:
            import ops as _ops_lh
            days = _ops_lh.retention_days("login_history")
        except Exception:
            days = LOGIN_HISTORY_RETENTION_DAYS
        if not days:
            return 0
        try:
            import history_rollups as _hr_lh
            _hr_lh.roll_engagement(db_path)
        except Exception:
            return 0            # no summary, no delete: the nightly pass retries
    try:
        conn = get_conn(db_path)
        try:
            cur = conn.execute(
                "DELETE FROM login_history WHERE created_at < datetime('now', ?)",
                (f"-{int(days)} days",))
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()
    except Exception:
        return 0


def sweep_orphan_pin_attempts(db_path: str = DB_PATH) -> int:
    """Delete lockout counters whose membership no longer exists.

    The table gained ON DELETE CASCADE, but only for databases created after
    that clause existed — SQLite cannot add a foreign-key action to a live
    table without rebuilding it. This covers the ones already out there, and
    costs nothing when there is nothing to sweep.
    """
    try:
        conn = get_conn(db_path)
        try:
            cur = conn.execute(
                "DELETE FROM membership_pin_attempts WHERE membership_id NOT IN "
                "(SELECT id FROM memberships)")
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()
    except Exception:
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
                                   include_inactive: bool = False,
                                   db_path: str = DB_PATH) -> list:
    """The memberships at one restaurant.

    include_inactive is for the OWNER-FACING management list only: a
    deactivated employee has to stay visible to be reactivated. Every
    staff-facing caller (the roster, the portal) leaves it off, so a
    deactivated person never appears on the sign-in screen.
    """
    conn = get_conn(db_path)
    sql = ("SELECT m.*, u.username, u.email, u.is_active AS user_is_active "
           "FROM memberships m JOIN users u ON u.id = m.user_id "
           "WHERE m.restaurant_id=?")
    if not include_inactive:
        sql += " AND m.is_active=1 AND u.is_active=1"
    args = [restaurant_id]
    if role:
        sql += " AND m.role=?"
        args.append(role)
    sql += " ORDER BY COALESCE(m.employee_name, u.username) COLLATE NOCASE"
    rows = conn.execute(sql, tuple(args)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_membership(user_id: int, restaurant_id: int, role: str,
                      employee_name: str = None, job_role: str = None,
                      db_path: str = DB_PATH) -> dict:
    """Create or update one identity's role at one restaurant."""
    from permissions import ROLE_PERMISSIONS, normalize_role
    role = normalize_role(role)
    if role not in ROLE_PERMISSIONS:
        raise ValueError(f"unknown role: {role!r}")
    conn = get_conn(db_path)
    try:
        conn.execute("""
            INSERT INTO memberships (user_id, restaurant_id, role, employee_name, job_role, updated_at)
            VALUES (?,?,?,?,?,datetime('now'))
            ON CONFLICT(user_id, restaurant_id) DO UPDATE SET
                role=excluded.role,
                employee_name=COALESCE(excluded.employee_name, memberships.employee_name),
                job_role=COALESCE(excluded.job_role, memberships.job_role),
                is_active=1,
                updated_at=datetime('now')
        """, (user_id, restaurant_id, role, (employee_name or "").strip() or None,
              (job_role or "").strip() or None))
        conn.commit()
        row = conn.execute(
            "SELECT * FROM memberships WHERE user_id=? AND restaurant_id=?",
            (user_id, restaurant_id)).fetchone()
    finally:
        conn.close()
    return dict(row)


def update_membership_details(membership_id: int, restaurant_id: int,
                              role: str = None, employee_name: str = None,
                              job_role: str = None, is_active: bool = None,
                              db_path: str = DB_PATH) -> Optional[dict]:
    """Day-two operations on one membership: promote, rename, reactivate.

    These were the gap that made the auth system un-operable by a customer:
    every other lifecycle step had an endpoint, but a promotion, a corrected
    spelling and a re-hire all required a database console. The rename matters
    most — employee_name is the join key to seven name-keyed tables, so a typo
    silently detaches someone from their own schedule, ratings and
    availability, and there was no way to fix it.

    Scoped by restaurant_id like every other membership write, so a guessed id
    belonging to another tenant matches nothing. Returns the updated row, or
    None when nothing matched.
    """
    sets, args = [], []
    if role is not None:
        from permissions import ROLE_PERMISSIONS, normalize_role
        role = normalize_role(role)
        if role not in ROLE_PERMISSIONS:
            raise ValueError(f"unknown role: {role!r}")
        sets.append("role=?")
        args.append(role)
        if role != "employee":
            # Out of the staff tier: the PIN goes with it (SEC-14).
            sets.append("pin_hash=NULL")
    if employee_name is not None:
        cleaned = (employee_name or "").strip()
        if not cleaned:
            raise ValueError("employee_name cannot be blank")
        sets.append("employee_name=?")
        args.append(cleaned)
    if job_role is not None:
        sets.append("job_role=?")
        args.append((job_role or "").strip() or None)
    if is_active is not None:
        sets.append("is_active=?")
        args.append(1 if is_active else 0)
    if not sets:
        return get_membership_by_id(membership_id, restaurant_id, db_path=db_path)

    sets.append("updated_at=datetime('now')")
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            f"UPDATE memberships SET {', '.join(sets)} WHERE id=? AND restaurant_id=?",
            tuple(args) + (membership_id, restaurant_id))
        conn.commit()
        if not cur.rowcount:
            return None
    finally:
        conn.close()
    # A demotion or a deactivation has to end the access it already granted,
    # not wait for a session to expire — same stance as set_membership_active.
    if is_active is False or role is not None:
        _end_staff_sessions_for_membership(membership_id, restaurant_id, db_path=db_path)
    return get_membership_by_id(membership_id, restaurant_id, db_path=db_path)


def get_membership_by_id(membership_id: int, restaurant_id: int,
                         db_path: str = DB_PATH) -> Optional[dict]:
    """One membership by id, scoped to the acting restaurant. Unlike
    get_membership() this does NOT filter on is_active — an owner managing a
    deactivated employee has to be able to see and reactivate them."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM memberships WHERE id=? AND restaurant_id=?",
                           (membership_id, restaurant_id)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


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
# Each further lockout inside a day doubles the last, up to a day. Every
# lockout used to be a flat 15 minutes with the counter reset, i.e. five
# guesses every quarter hour forever — the whole 4-digit space in about three
# weeks of patient guessing at one tablet (SEC-19). An owner can still lift a
# lock at once (Account → Staff → Unlock), which is the answer to a coworker
# locking someone out on purpose.
PIN_LOCKOUT_MAX_MINUTES = 24 * 60
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


# A KDF-shaped decoy for the paths that have nothing to check against.
#
# The identical error messages below stop an attacker READING which staff have
# access; they did nothing about the clock. A nonexistent or wrong-tenant
# membership returned in ~0.35 ms because it never reached scrypt, against
# ~57 ms for a real membership with a wrong PIN — a 160× tell that answered
# exactly the question the messages were written to refuse.
_DUMMY_PIN_HASH = None


def _burn_pin_cycles(candidate: str):
    """Spend a real KDF's worth of time on a path that has no hash to verify.

    Built lazily (and once per process) so a pepper set after import is still
    picked up, and so boot doesn't pay for a hash most deploys never need.
    """
    global _DUMMY_PIN_HASH
    try:
        if _DUMMY_PIN_HASH is None:
            _DUMMY_PIN_HASH = generate_password_hash(_peppered("0" * PIN_MIN_LENGTH))
        check_password_hash(_DUMMY_PIN_HASH, _peppered((candidate or "").strip()))
    except Exception:
        pass


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
    # A PIN hashed without the pepper is a PIN an offline attacker recovers
    # in seconds (10,000 guesses). Refuse to store one rather than warn.
    if not _pin_pepper():
        raise PinError("Staff PINs are unavailable until CAVNAR_PIN_PEPPER is configured on the server.")
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
            prior = 0
            try:
                prev = conn.execute(
                    "SELECT lockout_count FROM membership_pin_attempts WHERE membership_id=? "
                    "AND last_locked_at >= datetime('now', '-1 day')", (membership_id,)).fetchone()
                prior = (prev["lockout_count"] or 0) if prev else 0
            except Exception:
                prior = 0      # a database without the SEC-19 columns: flat lockouts, as before
            minutes = min(PIN_LOCKOUT_MINUTES * (2 ** prior), PIN_LOCKOUT_MAX_MINUTES)
            until = (datetime.utcnow() + _td(minutes=minutes)).isoformat()
            try:
                conn.execute("UPDATE membership_pin_attempts SET locked_until=?, failed_count=0, "
                             "lockout_count=?, last_locked_at=datetime('now') WHERE membership_id=?",
                             (until, prior + 1, membership_id))
            except Exception:
                conn.execute("UPDATE membership_pin_attempts SET locked_until=?, failed_count=0 "
                             "WHERE membership_id=?", (until, membership_id))
            conn.commit()
    finally:
        conn.close()
    return pin_lockout_state(membership_id, db_path=db_path)


def log_pin_event(membership_id: int, restaurant_id: int, event: str,
                  ip_address: str = None, db_path: str = DB_PATH) -> bool:
    """Record a PIN failure or lockout as a durable security event.

    membership_pin_attempts is a COUNTER, not a record: it resets to zero the
    moment the lockout fires and is deleted outright on the next successful
    sign-in. That makes it useless for the question that actually matters —
    "is someone working through the whole roster?" — because one failed
    attempt against each of forty employees leaves no trace anywhere.

    These go into login_history, which is already append-only, already
    per-user, and already what the Account screen reads, rather than a second
    events table nothing would ever look at.
    """
    if event not in ("pin_failed", "pin_locked"):
        return False
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute(
                "SELECT user_id FROM memberships WHERE id=? AND restaurant_id=?",
                (membership_id, restaurant_id)).fetchone()
            if not row:
                return False
            conn.execute(
                "INSERT INTO login_history (user_id, restaurant_id, event, ip_address, "
                "user_agent, device_type) VALUES (?,?,?,?,?,?)",
                (row["user_id"], restaurant_id, event, ip_address or "", "", "staff_pin"))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:
        # A security log that fails silently is worse than none, because it
        # reads as "no attacks" rather than "no logging".
        try:
            import ops as _ops_pe
            _ops_pe.capture(exc, job="log_pin_event",
                            context=f"membership_id={membership_id} event={event}")
        except Exception:
            print(f"[log_pin_event] failed for membership {membership_id}: {exc}")
        return False


def get_pin_security_events(restaurant_id: int, limit: int = 100,
                            db_path: str = DB_PATH) -> list:
    """PIN failures and lockouts across one restaurant, most recent first —
    the cross-employee view the per-membership counter cannot give."""
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute("""
                SELECT h.event, h.ip_address, h.created_at, h.user_id,
                       COALESCE(m.employee_name, u.username) AS name
                FROM login_history h
                JOIN users u ON u.id = h.user_id
                LEFT JOIN memberships m
                       ON m.user_id = h.user_id AND m.restaurant_id = h.restaurant_id
                WHERE h.restaurant_id=? AND h.event IN ('pin_failed','pin_locked')
                ORDER BY h.created_at DESC, h.id DESC
                LIMIT ?
            """, (restaurant_id, limit)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


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
                          ip_address: str = None, db_path: str = DB_PATH) -> dict:
    """Check a PIN. {ok} on success, {ok: False, error, locked} otherwise.

    restaurant_id is passed in from the portal token, never from the client,
    and is re-checked here so a tampered membership_id cannot reach across
    tenants even if it is a real id belonging to someone else's restaurant.

    Every failure path returns the same message AND spends the same time.
    Distinguishing "no PIN set" from "wrong PIN" would let anyone with the
    portal link enumerate which staff have access — and so would answering in
    0.35 ms instead of 57 ms, which is what the early returns used to do.
    """
    generic = {"ok": False, "error": "That PIN didn't match.", "locked": False}
    conn = get_conn(db_path)
    try:
        # Employees only: a membership promoted to manager kept its PIN and
        # a 4-digit PIN then opened a manager's console session (SEC-14).
        row = conn.execute(
            "SELECT id, pin_hash FROM memberships "
            "WHERE id=? AND restaurant_id=? AND is_active=1 AND role='employee'",
            (membership_id, restaurant_id)).fetchone()
    finally:
        conn.close()
    if not row:
        _burn_pin_cycles(pin)
        return generic

    state = pin_lockout_state(membership_id, db_path=db_path)
    if state["locked"]:
        mins = max(1, state["seconds_remaining"] // 60)
        return {"ok": False, "locked": True,
                "error": f"Too many tries. Ask a manager to unlock, or wait {mins} min."}

    if not row["pin_hash"]:
        # No PIN issued yet. Still counted, so the lockout also throttles
        # probing at memberships that cannot log in at all — and still pays
        # the KDF, so it is not distinguishable from a wrong PIN by timing.
        _burn_pin_cycles(pin)
        after_nopin = _record_pin_failure(membership_id, db_path=db_path)
        log_pin_event(membership_id, restaurant_id, "pin_failed",
                      ip_address=ip_address, db_path=db_path)
        if after_nopin["locked"]:
            log_pin_event(membership_id, restaurant_id, "pin_locked",
                          ip_address=ip_address, db_path=db_path)
        return generic

    candidate = (pin or "").strip()
    version, raw_hash = _decode_pin_hash(row["pin_hash"])
    if not check_password_hash(raw_hash, _peppered(candidate, version=version)):
        after = _record_pin_failure(membership_id, db_path=db_path)
        log_pin_event(membership_id, restaurant_id, "pin_failed",
                      ip_address=ip_address, db_path=db_path)
        if after["locked"]:
            log_pin_event(membership_id, restaurant_id, "pin_locked",
                          ip_address=ip_address, db_path=db_path)
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


def _pending_key() -> bytes:
    key = os.getenv("SECRET_KEY") or ""
    if not key:
        raise RuntimeError("SECRET_KEY is not set; cannot sign the 2FA pending token")
    return key.encode()


def make_pending_token(restaurant_id: int, user_id: int, secret: str) -> str:
    """The 2FA pending token: "rid:uid:secret:sig", base64. The user id used
    to ride unsigned, so a manager who passed their own password could edit
    it to the owner's id and be signed in as the owner (SEC-5)."""
    import hmac as _h, hashlib as _hl, base64 as _b
    body = f"{int(restaurant_id)}:{int(user_id)}:{secret}"
    sig = _h.new(_pending_key(), body.encode(), _hl.sha256).hexdigest()[:40]
    return _b.urlsafe_b64encode(f"{body}:{sig}".encode()).decode()


def read_pending_token(token: str):
    """(restaurant_id, user_id, secret) for a token make_pending_token
    issued, or None for anything tampered with, truncated or unsigned."""
    import hmac as _h, hashlib as _hl, base64 as _b
    try:
        decoded = _b.urlsafe_b64decode((token or "").encode()).decode()
        body, sig = decoded.rsplit(":", 1)
        rid_s, uid_s, secret = body.split(":", 2)
        expected = _h.new(_pending_key(), body.encode(), _hl.sha256).hexdigest()[:40]
        if not _h.compare_digest(sig, expected):
            return None
        return int(rid_s), int(uid_s), secret
    except Exception:
        return None


# ── admin view-as (SEC-12) ────────────────────────────────────────────────────

def record_view_as_session(token: str, opened_by, read_only: bool, db_path: str = DB_PATH) -> None:
    """Note who opened a view-as session and whether it is read-only (it is
    when a support login opened it). get_session_user reads it back."""
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM view_as_sessions WHERE created_at < datetime('now', '-2 days')")
        conn.execute("INSERT OR REPLACE INTO view_as_sessions (token_hash, opened_by, read_only) VALUES (?,?,?)",
                     (hash_session_token(token), opened_by, 1 if read_only else 0))
        conn.commit()
    finally:
        conn.close()


def _view_as_read_only(conn, token: str) -> bool:
    try:
        row = conn.execute("SELECT read_only FROM view_as_sessions WHERE token_hash=?",
                           (hash_session_token(token),)).fetchone()
    except Exception:
        # No table means no support-opened session can exist in this file.
        return False
    return bool(row and row[0])


def view_as_write_denied(user) -> bool:
    """True when this request is a write through a read-only view-as."""
    return bool(user and user.get("view_as_read_only")) and request.method not in ("GET", "HEAD", "OPTIONS")


_VIEW_AS_READ_ONLY_MSG = "This is a read-only support view. Nothing was changed."


# ── Where a 2FA code goes (SEC-20) ──────────────────────────────────────────
#
# To the person signing in, never to someone else. Every code used to go to
# the restaurant's owner_email/owner_phone, so a manager could not finish
# signing in without the owner reading them a code — and an owner who reads
# codes out on request is exactly who a phisher calls. The owner keeps the
# restaurant-wide on/off switch and hears about each sign-in from the login
# notice, but the code itself belongs to the login that asked for it.

NO_TWO_FA_DESTINATION = ("This login has no email address or phone number to send a sign-in code to. "
                         "Ask the account owner to add an email to your login under Team.")


def _mask_phone(phone: str) -> str:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return "(•••) •••-" + digits[-4:] if len(digits) >= 4 else "your phone"


def _mask_email(email: str) -> str:
    return email[:2] + "***@" + email.split("@")[-1]


def two_fa_destination(user, restaurant, method=None, strict=False):
    """Where this login's 2FA code goes: {"kind": "sms"|"email", "to",
    "masked", "name"}, or None when the login has nowhere of its own.

    The login's own users.email / users.phone. The restaurant's owner_email
    and owner_phone count as this login's only when it IS that owner — an
    account holder whose email matches owner_email, or one with no email of
    its own (the single login accounts started with, before team logins).

    `method` defaults to the restaurant's chosen two_fa_method. A text code
    for a login with no phone goes to its email instead, unless `strict`
    (the setup test, which must prove the channel being switched on).

    An internal login (admin, support) uses its own method
    (users.two_fa_method) and only its own email and phone — never the
    contact details of whatever restaurant it happens to be homed on."""
    from permissions import is_principal
    if not user:
        return None
    if is_internal_login(user):
        method = method or (user.get("two_fa_method") or "email")
        if method == TOTP_METHOD:
            # Nothing is sent: the code comes from the login's authenticator
            # app. Never falls back to email — a stolen password must not be
            # able to ask for a code by another channel (backup codes are the
            # way in without the phone).
            return {"kind": TOTP_METHOD, "to": None, "masked": "your authenticator app",
                    "name": user.get("name") or None}
        email = (user.get("email") or "").strip()
        email = email if "@" in email else ""
        phone = (user.get("phone") or "").strip()
        if method == "sms" and phone:
            return {"kind": "sms", "to": phone, "masked": _mask_phone(phone), "name": user.get("name") or None}
        if method == "sms" and strict:
            return None
        if email:
            return {"kind": "email", "to": email, "masked": _mask_email(email), "name": user.get("name") or None}
        return None
    if not restaurant:
        return None
    method = method or getattr(restaurant, "two_fa_method", None) or "email"
    email = (user.get("email") or "").strip()
    email = email if "@" in email else ""
    phone = (user.get("phone") or "").strip()
    owner_email = (getattr(restaurant, "owner_email", None) or "").strip()
    name = user.get("name") or None
    if is_principal(user) and (not email or email.lower() == owner_email.lower()):
        email = email or (owner_email if "@" in owner_email else "")
        phone = phone or (getattr(restaurant, "owner_phone", None) or "").strip()
        name = getattr(restaurant, "owner_name", None) or name
    if method == "sms" and phone:
        return {"kind": "sms", "to": phone, "masked": _mask_phone(phone), "name": name}
    if method == "sms" and strict:
        return None
    if email:
        return {"kind": "email", "to": email, "masked": _mask_email(email), "name": name}
    return None


ADMIN_CONSOLE_NAME = "the Cavnar AI admin console"


def send_two_fa_code(dest, restaurant, code) -> bool:
    """Send a code to a two_fa_destination(). True only when it went out.
    The email is logged against the restaurant (#119), so it appears in that
    client's email history; an internal login's code (whose label names the
    admin console, not a restaurant) is logged against none."""
    rname = getattr(restaurant, "name", None) or "your restaurant"
    if dest["kind"] == TOTP_METHOD or not dest.get("to"):
        return False                # an authenticator app is never sent a code
    if dest["kind"] == "sms":
        from notify import send_2fa_sms
        return bool(send_2fa_sms(dest["to"], rname, code))
    from emails import send_2fa_code
    return bool(send_2fa_code(dest["to"], rname, code, dest.get("name"),
                              restaurant_id=getattr(restaurant, "id", None)))


def _code_label(user, restaurant):
    """What the code message says it is for: the restaurant, or the admin
    console for an internal login (whose home row is not what it signs in to)."""
    if is_internal_login(user):
        from types import SimpleNamespace
        return SimpleNamespace(name=ADMIN_CONSOLE_NAME)
    return restaurant


def deliver_two_fa_code(user, restaurant, dest, code) -> dict:
    """Send a sign-in code and say what actually happened (COMMS-13).

    Returns {"sent": bool, "kind": "sms"|"email"|None, "masked": str|None,
    "fell_back": bool, "reason": str|None}. A text to a number that has
    texted STOP to the platform, or one the provider refused, falls back to
    the login's own email — the sign-in page used to say "We texted" whatever
    happened, so a STOPped owner waited for a code that never came."""
    label = _code_label(user, restaurant)

    def _send(d):
        try:
            return bool(send_two_fa_code(d, label, code))
        except Exception as exc:
            print(f"[2fa] code send failed for user {(user or {}).get('id')}: {exc}")
            return False

    out = {"sent": False, "kind": None, "masked": None, "fell_back": False, "reason": None}
    if not dest:
        out["reason"] = "no_destination"
        return out
    if dest["kind"] == TOTP_METHOD:
        # The app makes the code; there is nothing to send and no fallback.
        out.update(sent=True, kind=TOTP_METHOD, masked=dest["masked"])
        return out
    if dest["kind"] == "sms":
        stopped = False
        try:
            from notify import sms_stopped_phones
            stopped = bool(sms_stopped_phones([dest["to"]]))
        except Exception:
            stopped = False
        if not stopped and _send(dest):
            out.update(sent=True, kind="sms", masked=dest["masked"])
            return out
        out["reason"] = "sms_stopped" if stopped else "sms_failed"
        email_dest = two_fa_destination(user, restaurant, method="email")
        if email_dest and email_dest["kind"] == "email" and _send(email_dest):
            out.update(sent=True, kind="email", masked=email_dest["masked"], fell_back=True)
        return out
    if _send(dest):
        out.update(sent=True, kind=dest["kind"], masked=dest["masked"])
    else:
        out["reason"] = "email_failed"
    return out


def undelivered_code_message(result) -> str:
    """The sentence a sign-in shows when no code went out."""
    if (result or {}).get("reason") == "sms_stopped":
        return ("Your number has opted out of texts from us and there's no email on this login to fall back to. "
                "Use a backup code, or ask the account owner to add an email to your login.")
    return ("We couldn't send your code just now. Tap Resend code in a minute, or use a backup code.")


def login_needs_second_factor(user, restaurant) -> bool:
    """Whether this sign-in must pass a second factor before it gets a
    session. An internal login answers from its own users row — no lookup,
    so nothing can fail open or lock the only admin out; everyone else from
    their restaurant's switch."""
    if is_internal_login(user):
        return user_two_factor_enrolled(user)
    return bool(restaurant and getattr(restaurant, "two_fa_enabled", 0))


def session_cookie_max_age(user) -> int:
    """The session cookie's lifetime: the session's own for an internal
    login (ADMIN_SESSION_HOURS), 30 days for everyone else."""
    return ADMIN_SESSION_HOURS * 3600 if is_internal_login(user) else 30 * 24 * 3600


def two_fa_challenge_started_at(restaurant_id: int, user_id: int, pending: str,
                                db_path: str = DB_PATH):
    """When this sign-in's challenge was issued — the moment its password
    was typed (a resend replaces the code, not the row). None if gone."""
    if not pending:
        return None
    conn = get_conn(db_path)
    try:
        row = _find_two_fa_challenge(conn, restaurant_id, user_id, pending, "login")
        return row["created_at"] if row else None
    finally:
        conn.close()


# ── an internal login's own backup codes ────────────────────────────────────

def generate_user_backup_codes(user_id: int, count: int = 10, db_path: str = DB_PATH) -> list:
    """A fresh set for one login, replacing any before. Shown once."""
    codes = [f"{secrets.token_hex(4).upper()[:4]}-{secrets.token_hex(4).upper()[4:]}" for _ in range(count)]
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM user_backup_codes WHERE user_id=?", (user_id,))
        for code in codes:
            conn.execute("INSERT INTO user_backup_codes (user_id, code_hash) VALUES (?, ?)",
                         (user_id, generate_password_hash(code)))
        conn.commit()
    finally:
        conn.close()
    return codes


def verify_and_consume_user_backup_code(user_id: int, code: str, db_path: str = DB_PATH) -> bool:
    from models import normalize_backup_code
    code = normalize_backup_code(code)
    if not code:
        return False
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT id, code_hash FROM user_backup_codes WHERE user_id=? AND used_at IS NULL",
                            (user_id,)).fetchall()
        for row in rows:
            if check_password_hash(row["code_hash"], code):
                conn.execute("UPDATE user_backup_codes SET used_at=datetime('now') WHERE id=?", (row["id"],))
                conn.commit()
                return True
        return False
    finally:
        conn.close()


def count_unused_user_backup_codes(user_id: int, db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM user_backup_codes WHERE user_id=? AND used_at IS NULL",
                            (user_id,)).fetchone()[0]
    finally:
        conn.close()


def verify_backup_code_for(user, restaurant_id: int, code: str, db_path: str = DB_PATH) -> bool:
    """A backup code typed at a sign-in's second step: the login's own codes
    for an internal login, the restaurant's for everyone else."""
    if is_internal_login(user):
        return verify_and_consume_user_backup_code(user["id"], code, db_path=db_path)
    from models import verify_and_consume_backup_code
    return verify_and_consume_backup_code(restaurant_id, code, db_path=db_path)


# ── an internal login's authenticator app (TOTP, RFC 6238) ──────────────────
#
# A third method beside email and sms, for internal logins only: the code
# comes from an authenticator app (Duo Mobile, Google Authenticator, 1Password
# …) the login scanned a QR code into, so nothing is emailed or texted. SHA-1,
# 6 digits, 30-second steps, one step either side accepted for clock drift,
# and each step accepted once per login (user_totp.last_step). The secret is
# a credential: encrypted under CREDENTIAL_KEY, never logged, never in an
# audit payload, emptied from off-site backups.

TOTP_METHOD = "app"
TOTP_ISSUER = "Cavnar AI"
TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1
TOTP_SECRET_BYTES = 20          # 160 bits, RFC 4226's recommended length
TOTP_PENDING_MINUTES = 15


def totp_new_secret() -> str:
    """A fresh random secret, base32 without padding (32 characters)."""
    import base64
    return base64.b32encode(secrets.token_bytes(TOTP_SECRET_BYTES)).decode("ascii").rstrip("=")


def _totp_key(secret: str) -> bytes:
    import base64
    s = re.sub(r"[\s-]", "", secret or "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def totp_code(secret: str, step: int, digits: int = TOTP_DIGITS, digest: str = "sha1") -> str:
    """The code for one time step (HOTP, RFC 4226, over the step counter)."""
    import hmac as _h
    import struct
    mac = _h.new(_totp_key(secret), struct.pack(">Q", int(step)), digest).digest()
    off = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def totp_step(now=None) -> int:
    import time as _t
    return int((_t.time() if now is None else float(now)) // TOTP_STEP_SECONDS)


def totp_match(secret: str, code: str, now=None, last_step=None, window: int = TOTP_WINDOW):
    """The step `code` is valid for — within `window` steps of now and later
    than `last_step` — or None. Every candidate is compared in constant
    time, and all of them are compared whatever matched."""
    import hmac as _h
    typed = re.sub(r"\s", "", code or "")
    if not secret or not re.fullmatch(r"\d{%d}" % TOTP_DIGITS, typed):
        return None
    now_step = totp_step(now)
    found = None
    for step in range(now_step - window, now_step + window + 1):
        if _h.compare_digest(totp_code(secret, step), typed) and found is None:
            found = step
    if found is None or (last_step is not None and found <= int(last_step)):
        return None
    return found


def totp_uri(username: str, secret: str) -> str:
    """The otpauth:// URI an authenticator app reads from the QR code. It
    carries the secret: it is only ever rendered into the enrolment
    response, never logged, stored or sent to a third party."""
    from urllib.parse import quote
    label = quote(TOTP_ISSUER, safe="") + ":" + quote(username or "admin", safe="")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(TOTP_ISSUER, safe='')}"
            f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}")


def totp_qr_data_uri(uri: str) -> str:
    """The QR code for `uri` as an SVG data URI, drawn here (the qrcode
    package) — never by a third-party QR service, which would be handed the
    secret."""
    import base64
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathFillImage, box_size=10, border=4)
    svg = img.to_string()
    if isinstance(svg, str):
        svg = svg.encode("utf-8")
    return "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")


def totp_secret_groups(secret: str) -> str:
    """The secret in groups of four, for typing into an app by hand."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


def totp_available() -> bool:
    """Whether the app method can be offered: the secret must be stored
    encrypted, so it needs a working CREDENTIAL_KEY."""
    try:
        import credentials
        return credentials.key_state() == "ok"
    except Exception:
        return False


def _totp_seal(secret: str) -> str:
    import credentials
    if credentials.key_state() != "ok":
        raise RuntimeError("CREDENTIAL_KEY is not set, so an authenticator secret cannot be stored encrypted")
    sealed = credentials.encrypt(secret)
    if not sealed or not sealed.startswith(credentials.PREFIX) or credentials.decrypt(sealed) != secret:
        raise RuntimeError("the authenticator secret could not be encrypted")
    return sealed


def _totp_open(sealed):
    if not sealed:
        return None
    import credentials
    if not str(sealed).startswith(credentials.PREFIX):
        return None                 # never trust a secret that was not sealed
    return credentials.decrypt(sealed)


def start_totp_enrolment(user_id: int, db_path: str = DB_PATH) -> str:
    """A new PENDING secret for this login (replacing any earlier pending
    one), live for TOTP_PENDING_MINUTES. The active secret, if any, is
    untouched: the login keeps signing in with its current method until a
    code from the app confirms the new one. Returns the plaintext secret for
    the enrolment response only. Raises without a working CREDENTIAL_KEY."""
    secret = totp_new_secret()
    sealed = _totp_seal(secret)
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO user_totp (user_id, pending_secret, pending_expires_at, updated_at) "
            "VALUES (?, ?, datetime('now', ?), datetime('now')) "
            "ON CONFLICT(user_id) DO UPDATE SET pending_secret=excluded.pending_secret, "
            "pending_expires_at=excluded.pending_expires_at, updated_at=datetime('now')",
            (user_id, sealed, f"+{TOTP_PENDING_MINUTES} minutes"))
        conn.commit()
    finally:
        conn.close()
    return secret


def confirm_totp_enrolment(user_id: int, code: str, now=None, db_path: str = DB_PATH) -> str:
    """'ok' | 'wrong' | 'expired' | 'missing'. A correct code from the app
    for the pending secret makes it this login's active secret and its
    two-factor method 'app' (two_fa_enabled on) in one transaction; the step
    it matched is spent. The method it replaces (email, sms or an older app
    secret) stops working at that moment."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT pending_secret, pending_expires_at, datetime('now') > pending_expires_at AS dead "
                           "FROM user_totp WHERE user_id=?", (user_id,)).fetchone()
        if not row or not row["pending_secret"]:
            return "missing"
        if row["dead"]:
            conn.execute("UPDATE user_totp SET pending_secret=NULL, pending_expires_at=NULL WHERE user_id=?",
                         (user_id,))
            conn.commit()
            return "expired"
        secret = _totp_open(row["pending_secret"])
        step = totp_match(secret, code, now=now) if secret else None
        if step is None:
            return "wrong"
        cur = conn.execute(
            "UPDATE user_totp SET secret=pending_secret, last_step=?, pending_secret=NULL, "
            "pending_expires_at=NULL, activated_at=datetime('now'), updated_at=datetime('now') "
            "WHERE user_id=? AND pending_secret=?", (step, user_id, row["pending_secret"]))
        if (cur.rowcount or 0) != 1:
            conn.rollback()
            return "missing"        # replaced by a newer QR code under us
        conn.execute("UPDATE users SET two_fa_enabled=1, two_fa_method=? WHERE id=?", (TOTP_METHOD, user_id))
        conn.commit()
        return "ok"
    finally:
        conn.close()


def verify_user_totp(user_id: int, code: str, now=None, db_path: str = DB_PATH) -> bool:
    """A sign-in code from this login's authenticator app. True once per
    30-second step: the step is claimed with a compare-and-set, so the same
    code sent twice (or two requests racing) passes only once."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT secret, last_step FROM user_totp WHERE user_id=?", (user_id,)).fetchone()
        if not row or not row["secret"]:
            return False
        secret = _totp_open(row["secret"])
        step = totp_match(secret, code, now=now, last_step=row["last_step"]) if secret else None
        if step is None:
            return False
        cur = conn.execute("UPDATE user_totp SET last_step=?, updated_at=datetime('now') "
                           "WHERE user_id=? AND (last_step IS NULL OR last_step < ?)", (step, user_id, step))
        conn.commit()
        return (cur.rowcount or 0) == 1
    finally:
        conn.close()


def totp_pending_live(user_id: int, db_path: str = DB_PATH) -> bool:
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT 1 FROM user_totp WHERE user_id=? AND pending_secret IS NOT NULL "
                           "AND pending_expires_at > datetime('now')", (user_id,)).fetchone()
        return bool(row)
    finally:
        conn.close()


def clear_user_totp(user_id: int, db_path: str = DB_PATH) -> None:
    """Forget this login's authenticator secrets, active and pending."""
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM user_totp WHERE user_id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def uses_authenticator_app(user) -> bool:
    """An internal login whose two-factor is on and comes from an app."""
    return bool(user and is_internal_login(user) and user_two_factor_enrolled(user)
                and (user.get("two_fa_method") or "") == TOTP_METHOD)


def second_factor_throttle_key(user):
    """A per-login throttle key for a sign-in code that is not single-use
    per challenge. An emailed or texted code is new for every sign-in, so
    the per-address budget ("2fa:<ip>") bounds guessing it; an app's secret
    is fixed, so its guesses are also bounded per login, from any address."""
    return f"2fa-app:{user['id']}" if uses_authenticator_app(user) else None


def check_sign_in_code(user, restaurant_id: int, user_id: int, code: str, pending: str,
                       db_path: str = DB_PATH) -> str:
    """'ok' | 'wrong' | 'expired' | 'missing' for the code typed at a
    sign-in's second step (not consumed — the caller ends the challenge).
    A login on the app method is checked against its authenticator only: the
    challenge's own random code was never sent anywhere and never passes.
    Everyone else: the emailed or texted code (check_two_fa_code). A backup
    code is the caller's next try on 'wrong', for both."""
    if uses_authenticator_app(user) and user.get("id") == user_id:
        return "ok" if verify_user_totp(user_id, code, db_path=db_path) else "wrong"
    return check_two_fa_code(restaurant_id, user_id, code, pending=pending, consume=False, db_path=db_path)


# ── 2FA challenges (SEC-20) ─────────────────────────────────────────────────

TWO_FA_CODE_MINUTES = 10


def _two_fa_hash(kind: str, value: str) -> str:
    import hmac as _h, hashlib as _hl
    return _h.new(_pending_key(), f"2fa-{kind}:{value}".encode(), _hl.sha256).hexdigest()


def _new_two_fa_code() -> str:
    return str(secrets.randbelow(900000) + 100000)


def issue_two_fa_challenge(restaurant_id: int, user_id: int, purpose: str = "login",
                           db_path: str = DB_PATH):
    """Start a 2FA challenge for one login. Returns (pending_secret, code):
    the code goes to that login's own email or phone (two_fa_destination), the pending secret rides in
    the signed pending token (make_pending_token). Each sign-in attempt gets
    its own row, so two people signing in at one restaurant no longer
    overwrite each other's code. A 'setup' challenge ("Send test code") is one
    per login: a new one replaces that login's previous one and nobody
    else's."""
    pending = secrets.token_hex(24)
    pending_hash = _two_fa_hash("pending", pending)
    code = _new_two_fa_code()
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM two_fa_challenges WHERE expires_at < datetime('now', '-1 day')")
        if purpose != "login":
            conn.execute("DELETE FROM two_fa_challenges WHERE restaurant_id=? AND user_id=? AND purpose=?",
                         (restaurant_id, user_id, purpose))
        conn.execute(
            "INSERT INTO two_fa_challenges (restaurant_id, user_id, purpose, pending_hash, code_hash, expires_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now', ?))",
            (restaurant_id, user_id, purpose, pending_hash,
             _two_fa_hash("code", pending_hash + ":" + code), f"+{TWO_FA_CODE_MINUTES} minutes"))
        conn.commit()
    finally:
        conn.close()
    return pending, code


def _find_two_fa_challenge(conn, restaurant_id, user_id, pending, purpose):
    if pending is None:
        return conn.execute(
            "SELECT * FROM two_fa_challenges WHERE restaurant_id=? AND user_id=? AND purpose=? "
            "ORDER BY id DESC LIMIT 1", (restaurant_id, user_id, purpose)).fetchone()
    return conn.execute(
        "SELECT * FROM two_fa_challenges WHERE pending_hash=? AND restaurant_id=? AND user_id=? AND purpose=?",
        (_two_fa_hash("pending", pending), restaurant_id, user_id, purpose)).fetchone()


def two_fa_challenge_exists(restaurant_id: int, user_id: int, pending: str,
                            purpose: str = "login", db_path: str = DB_PATH) -> bool:
    """True when this pending secret was issued by a sign-in for exactly this
    login at this restaurant and has not been used yet."""
    if not pending:
        return False
    conn = get_conn(db_path)
    try:
        return _find_two_fa_challenge(conn, restaurant_id, user_id, pending, purpose) is not None
    finally:
        conn.close()


def reissue_two_fa_code(restaurant_id: int, user_id: int, pending: str,
                        db_path: str = DB_PATH):
    """Resend: a fresh code (and a fresh 10 minutes) for the same sign-in.
    Returns the new code, or None when the challenge is gone."""
    if not pending:
        return None
    conn = get_conn(db_path)
    try:
        row = _find_two_fa_challenge(conn, restaurant_id, user_id, pending, "login")
        if not row:
            return None
        code = _new_two_fa_code()
        conn.execute("UPDATE two_fa_challenges SET code_hash=?, expires_at=datetime('now', ?) WHERE id=?",
                     (_two_fa_hash("code", row["pending_hash"] + ":" + code), f"+{TWO_FA_CODE_MINUTES} minutes",
                      row["id"]))
        conn.commit()
        return code
    finally:
        conn.close()


def check_two_fa_code(restaurant_id: int, user_id: int, code: str, pending: str = None,
                      purpose: str = "login", consume: bool = True, db_path: str = DB_PATH) -> str:
    """'ok', 'wrong', 'expired' or 'missing' for a code typed against one
    login's challenge. 'setup' challenges are looked up by login (pending
    None); 'login' ones by the pending secret. A correct, unexpired code
    deletes the challenge when consume is set, so it works exactly once."""
    import hmac as _h
    conn = get_conn(db_path)
    try:
        row = _find_two_fa_challenge(conn, restaurant_id, user_id, pending, purpose)
        if not row:
            return "missing"
        expected = _two_fa_hash("code", row["pending_hash"] + ":" + (code or "").strip())
        if not _h.compare_digest(row["code_hash"], expected):
            return "wrong"
        expired = conn.execute("SELECT datetime('now') > ?", (row["expires_at"],)).fetchone()[0]
        if expired:
            return "expired"
        if consume:
            conn.execute("DELETE FROM two_fa_challenges WHERE id=?", (row["id"],))
            conn.commit()
        return "ok"
    finally:
        conn.close()


def end_two_fa_challenge(restaurant_id: int, user_id: int, pending: str,
                         db_path: str = DB_PATH) -> None:
    """Delete one sign-in's challenge once it has been passed (single use),
    whether it was passed with the code or with a backup code."""
    if not pending:
        return
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM two_fa_challenges WHERE pending_hash=? AND restaurant_id=? AND user_id=?",
                     (_two_fa_hash("pending", pending), restaurant_id, user_id))
        conn.commit()
    finally:
        conn.close()


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
        conn.execute(
            "INSERT INTO staff_portal_tokens (restaurant_id, token, join_code) VALUES (?,?,?)",
            (restaurant_id, token, _mint_join_code(conn)))
        conn.commit()
        return token
    finally:
        conn.close()


# No I, O, 0 or 1 — this gets read off a whiteboard and typed on a phone by
# someone who is about to start a shift, and those four are the characters
# people get wrong. 32^6 is ~1e9, which together with the signup throttle is
# far more than enough for a code that only names a restaurant.
_JOIN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
JOIN_CODE_LENGTH = 6


def _mint_join_code(conn) -> str:
    for _ in range(12):
        code = "".join(secrets.choice(_JOIN_ALPHABET) for _ in range(JOIN_CODE_LENGTH))
        clash = conn.execute(
            "SELECT 1 FROM staff_portal_tokens WHERE join_code=? AND revoked_at IS NULL",
            (code,)).fetchone()
        if not clash:
            return code
    # Twelve collisions against a billion-code space means something is very
    # wrong; a longer code is better than an infinite loop or a duplicate.
    return "".join(secrets.choice(_JOIN_ALPHABET) for _ in range(JOIN_CODE_LENGTH + 3))


def normalize_join_code(value) -> str:
    """What someone typed, as the code actually looks.

    People type lowercase, add the dash they saw, and hit O for 0. The first
    two are just tidied; the third cannot happen because those characters are
    not in the alphabet.
    """
    return "".join(c for c in (value or "").upper() if c in _JOIN_ALPHABET)


def get_join_code(restaurant_id: int, db_path: str = DB_PATH) -> str:
    """The restaurant's current join code, minting one for links that predate
    join codes entirely."""
    get_or_create_staff_portal_token(restaurant_id, db_path=db_path)
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, join_code FROM staff_portal_tokens WHERE restaurant_id=? "
            "AND revoked_at IS NULL ORDER BY id DESC LIMIT 1", (restaurant_id,)).fetchone()
        if not row:
            return ""
        if row["join_code"]:
            return row["join_code"]
        code = _mint_join_code(conn)
        conn.execute("UPDATE staff_portal_tokens SET join_code=? WHERE id=?",
                     (code, row["id"]))
        conn.commit()
        return code
    finally:
        conn.close()


def restaurant_for_join_code(code: str, db_path: str = DB_PATH) -> Optional[int]:
    """The restaurant a join code names, or None. Same contract as
    restaurant_for_portal_token: revoked codes resolve to nothing."""
    code = normalize_join_code(code)
    if not code:
        return None
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT restaurant_id FROM staff_portal_tokens "
            "WHERE join_code=? AND revoked_at IS NULL", (code,)).fetchone()
    finally:
        conn.close()
    return row["restaurant_id"] if row else None


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


# ── Staff portal throttle + replay protection ──────────────────────────────

# Per address, per 5 minutes: 30 hits that did not end well (a wrong PIN, a
# stale nonce, an unknown code, a signup step), and 300 of anything. Every
# hit used to count against the 30, successes included, so at shift change
# the 16th employee signing in on the restaurant's Wi-Fi (one NAT address, a
# roster read and a sign-in each) was refused (SEC-18).
PORTAL_MAX_ATTEMPTS = 30
PORTAL_MAX_REQUESTS = 300
PORTAL_WINDOW_SECONDS = 300
PORTAL_NONCE_MINUTES = 30


def record_portal_attempt(ip: str, db_path: str = DB_PATH):
    """Count one hit on the portal's public surface, and evict the expired.
    Returns the row's id, for mark_portal_attempt_ok once the hit turns out
    fine. Recorded BEFORE the work, so concurrent guesses all count.

    The delete is done here rather than in a scheduled sweep so the table can
    never grow past one window's worth of traffic, with no process that has to
    remember to run.
    """
    if not ip:
        return None
    try:
        conn = get_conn(db_path)
        try:
            cur = conn.execute("INSERT INTO portal_attempts (ip) VALUES (?)", (ip,))
            attempt_id = cur.lastrowid
            conn.execute("DELETE FROM portal_attempts WHERE created_at < datetime('now', ?)",
                         (f"-{PORTAL_WINDOW_SECONDS} seconds",))
            conn.commit()
            return attempt_id
        finally:
            conn.close()
    except Exception as exc:
        # Deliberately does not raise: a throttle that breaks the door it
        # guards is worse than the spraying it prevents, and the
        # per-membership lockout is still in force. But it must not be
        # INVISIBLE — a counter that has silently stopped counting looks
        # exactly like an estate nobody is attacking.
        _report_throttle_failure(exc, "record_portal_attempt", ip)
        return None


def mark_portal_attempt_ok(attempt_id, db_path: str = DB_PATH) -> None:
    """The hit recorded as attempt_id ended well: it still counts toward the
    address's overall ceiling, not toward its failure budget (SEC-18)."""
    if not attempt_id:
        return
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE portal_attempts SET ok=1 WHERE id=?", (attempt_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        _report_throttle_failure(exc, "mark_portal_attempt_ok", str(attempt_id))


def _report_throttle_failure(exc, where, ip):
    try:
        import ops as _ops_throttle
        _ops_throttle.capture(exc, job="portal_throttle", context=f"{where} ip={ip}")
    except Exception:
        print(f"[portal_throttle] {where} failed for {ip}: {exc}")


def portal_attempts_exceeded(ip: str, db_path: str = DB_PATH) -> bool:
    """True when this IP has burned its budget inside the sliding window."""
    if not ip:
        return False
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN ok=1 THEN 0 ELSE 1 END), 0) AS failed "
                "FROM portal_attempts WHERE ip=? AND created_at >= datetime('now', ?)",
                (ip, f"-{PORTAL_WINDOW_SECONDS} seconds")).fetchone()
        finally:
            conn.close()
        if not row:
            return False
        return row["failed"] >= PORTAL_MAX_ATTEMPTS or row["n"] >= PORTAL_MAX_REQUESTS
    except Exception as exc:
        # Fails open, for the same reason as above — and reported, for the
        # same reason as above.
        _report_throttle_failure(exc, "portal_attempts_exceeded", ip)
        return False


def issue_portal_nonce(restaurant_id: int, db_path: str = DB_PATH) -> str:
    """Mint a single-use token for one upcoming PIN sign-in."""
    nonce = secrets.token_urlsafe(18)
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("DELETE FROM portal_nonces WHERE created_at < datetime('now', ?)",
                         (f"-{PORTAL_NONCE_MINUTES} minutes",))
            conn.execute("INSERT INTO portal_nonces (nonce, restaurant_id) VALUES (?,?)",
                         (nonce, restaurant_id))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        return ""
    return nonce


def consume_portal_nonce(nonce: str, restaurant_id: int, db_path: str = DB_PATH) -> bool:
    """Spend a nonce. True exactly once per issued token.

    The DELETE is the check: rowcount is 1 for the first caller and 0 for
    every replay, so two simultaneous requests carrying the same nonce cannot
    both win regardless of ordering.
    """
    if not nonce:
        return False
    try:
        conn = get_conn(db_path)
        try:
            cur = conn.execute(
                "DELETE FROM portal_nonces WHERE nonce=? AND restaurant_id=? "
                "AND created_at >= datetime('now', ?)",
                (nonce, restaurant_id, f"-{PORTAL_NONCE_MINUTES} minutes"))
            conn.commit()
            return (cur.rowcount or 0) > 0
        finally:
            conn.close()
    except Exception:
        return False


# ── Employee self-signup ───────────────────────────────────────────────────
#
# An employee creates their own account: verify a phone, name the restaurant
# with a join code, claim their own name off the roster, set a PIN.
#
# The owner does nothing per employee. The control is not on WHO may sign up —
# an account with no membership can see nothing, because every staff route
# derives the restaurant from the membership and there isn't one — it is on
# WHICH NAME may be claimed, and each name may be claimed exactly once.
#
# That single constraint is what stops the interesting attack. Without it,
# anyone holding the join code could register as "Jordan P." and inherit
# Jordan's schedule and, worse, tick Jordan's tasks: task_completions records
# completed_by as a name string, so a self-asserted name turns the whole
# accountability feature into fiction. Claiming from the roster also means the
# name matches the schedule exactly, which is the other half of the problem —
# a typed name silently detaches an employee from their own shifts.

SIGNUP_CODE_TTL_MINUTES = 10
SIGNUP_TOKEN_TTL_MINUTES = 30
SIGNUP_MAX_ATTEMPTS = 5
SIGNUP_RESEND_COOLDOWN_SECONDS = 45
SIGNUP_MAX_SENDS = 5          # per phone, per live signup row


class SignupError(ValueError):
    """A signup step that cannot proceed, with a message safe to show."""


def display_phone(value) -> str:
    """A phone number the way an owner reads it: "(334) 568-9292" for any US
    number however it was stored ("+13345689292", "334.568.9292",
    "1 334 568 9292"); anything else (international, an extension, a
    fragment) is returned as it was. Display only — storage and sending keep
    normalize_phone's spelling."""
    raw = str(value or "").strip()
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    elif raw.startswith("+") and not raw.startswith("+1"):
        return raw
    if len(digits) != 10:
        return raw
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def normalize_phone(value) -> str:
    """One spelling for a phone number, so it works as an identity key."""
    from notify import _normalize_phone
    raw = (value or "").strip()
    if not raw:
        return ""
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) < 10:
        return ""
    return _normalize_phone(raw)


def _sms_configured() -> bool:
    """Whether Twilio is wired up — the same three variables notify.py reads.

    These names are not guessable and must match notify.py exactly: this asked
    for TWILIO_PHONE_NUMBER, which exists nowhere, so it reported "not
    configured" on a deployment where texting was fully set up.
    """
    import os
    return all(os.environ.get(k) for k in
               ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"))


def start_staff_signup(phone: str, optin: bool = False, db_path: str = DB_PATH) -> dict:
    """Send a verification code to a phone. {ok, dev_code} or raises.

    dev_code is returned ONLY when Twilio is unconfigured and this is not a
    deployed environment — otherwise local and test runs could never get past
    step one, and a developer would be tempted to build a bypass that ships.

    optin must be explicitly True. This is the server-side half of the A2P
    10DLC consent requirement (Twilio rejected the first campaign submission
    over exactly this: "the opt-in checkbox is missing or appears to be
    pre-selected"). The web/iOS clients refuse to call this without a
    genuinely unchecked-by-default box the person ticks themselves, but that
    is a UI courtesy — a direct API call could skip it, and consent that can
    be skipped is not consent a carrier will accept. Enforcing it here is
    what makes it real.
    """
    phone = normalize_phone(phone)
    if not phone:
        raise SignupError("Enter a mobile number we can text.")
    if not optin:
        raise SignupError("Check the box to consent to the text before we can send it.")

    code = f"{secrets.randbelow(1000000):06d}"
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM staff_signups WHERE created_at < datetime('now', ?)",
                     (f"-{SIGNUP_TOKEN_TTL_MINUTES} minutes",))
        row = conn.execute(
            "SELECT sends, last_sent_at FROM staff_signups WHERE phone=?", (phone,)).fetchone()
        if row:
            if (row["sends"] or 0) >= SIGNUP_MAX_SENDS:
                raise SignupError("Too many codes sent to that number. Try again later.")
            recent = conn.execute(
                "SELECT 1 FROM staff_signups WHERE phone=? AND last_sent_at > datetime('now', ?)",
                (phone, f"-{SIGNUP_RESEND_COOLDOWN_SECONDS} seconds")).fetchone()
            if recent:
                raise SignupError("We just texted you — give it a moment.")
            conn.execute(
                "UPDATE staff_signups SET code_hash=?, attempts=0, sends=sends+1, "
                "token_hash=NULL, verified_at=NULL, last_sent_at=datetime('now'), "
                "created_at=datetime('now') WHERE phone=?",
                (generate_password_hash(code), phone))
        else:
            conn.execute(
                "INSERT INTO staff_signups (phone, code_hash) VALUES (?,?)",
                (phone, generate_password_hash(code)))
        conn.commit()
    finally:
        conn.close()

    queued = False
    try:
        from notify import send_sms
        # use_case="otp" routes this through its own Messaging Service/
        # Campaign once TWILIO_OTP_MESSAGING_SERVICE_SID is set — a
        # verification code is a different A2P use case from the owner alert
        # campaign, and mixing the two on one campaign is what carriers
        # filter hardest.
        # Wording matches the A2P 10DLC campaign's sample messages exactly —
        # a reviewer can test-fire this flow and compare against what was
        # submitted, so the two must never drift apart.
        queued = send_sms(phone, f"Cavnar AI: Your verification code is {code}. "
                                 f"It expires in {SIGNUP_CODE_TTL_MINUTES} minutes. "
                                 f"Reply STOP to opt out.", use_case="otp")
    except Exception as exc:
        print(f"[staff_signup] SMS send failed for {phone}: {exc}")

    out = {"ok": True, "sms_sent": queued}
    # The local escape hatch is keyed on the DEPLOYMENT, not on whether the
    # send worked, because "it worked" is not something the send can tell us:
    # Twilio answers 201 the moment it accepts a message for delivery, and the
    # carrier can reject it seconds later (A2P 10DLC, unreachable handset, a
    # landline). Keying the fallback on that 201 produced the worst outcome
    # available — no text arrived AND no code was shown, because as far as the
    # server knew it had been sent.
    if not cookies_require_secure():
        print(f"[staff_signup] DEV CODE for {phone}: {code}")
        out["dev_code"] = code
        out["sms_configured"] = _sms_configured()
    return out


def verify_staff_signup(phone: str, code: str, db_path: str = DB_PATH) -> str:
    """Check the texted code. Returns a signup token, or raises SignupError.

    The token is what the claim step carries — the phone number itself is
    never trusted as proof, because it arrives from the client.
    """
    phone = normalize_phone(phone)
    if not phone:
        raise SignupError("Enter a mobile number we can text.")
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT code_hash, attempts FROM staff_signups "
            "WHERE phone=? AND created_at > datetime('now', ?)",
            (phone, f"-{SIGNUP_CODE_TTL_MINUTES} minutes")).fetchone()
        if not row:
            raise SignupError("That code expired. Ask for a new one.")
        if (row["attempts"] or 0) >= SIGNUP_MAX_ATTEMPTS:
            raise SignupError("Too many tries. Ask for a new code.")
        if not check_password_hash(row["code_hash"], (code or "").strip()):
            conn.execute("UPDATE staff_signups SET attempts=attempts+1 WHERE phone=?", (phone,))
            conn.commit()
            raise SignupError("That code didn't match.")
        token = secrets.token_urlsafe(24)
        conn.execute(
            "UPDATE staff_signups SET token_hash=?, verified_at=datetime('now'), attempts=0 "
            "WHERE phone=?", (hash_session_token(token), phone))
        conn.commit()
    finally:
        conn.close()
    return token


def phone_for_signup_token(token: str, db_path: str = DB_PATH) -> Optional[str]:
    """The verified phone behind a signup token, or None."""
    if not token:
        return None
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT phone FROM staff_signups WHERE token_hash=? AND verified_at IS NOT NULL "
            "AND verified_at > datetime('now', ?)",
            (hash_session_token(token), f"-{SIGNUP_TOKEN_TTL_MINUTES} minutes")).fetchone()
    finally:
        conn.close()
    return row["phone"] if row else None


def _consume_signup_token(token: str, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM staff_signups WHERE token_hash=?",
                     (hash_session_token(token),))
        conn.commit()
    finally:
        conn.close()


def claimable_names(restaurant_id: int, db_path: str = DB_PATH) -> list:
    """The names an employee may claim at this restaurant.

    The pool is the roster the owner already keeps for Operational Score plus
    whoever appears on the most recently published schedule — never invented,
    never a sample fixture. Anything already claimed is removed, so a name is
    claimable exactly once.
    """
    from staff_roster import roster_names_for_restaurant
    taken = set()
    conn = get_conn(db_path)
    try:
        for r in conn.execute(
                "SELECT employee_name FROM memberships "
                "WHERE restaurant_id=? AND employee_name IS NOT NULL AND is_active=1",
                (restaurant_id,)).fetchall():
            taken.add((r["employee_name"] or "").strip().lower())
    finally:
        conn.close()
    out, seen = [], set()
    for name, job in roster_names_for_restaurant(restaurant_id, db_path=db_path):
        key = name.strip().lower()
        if not key or key in taken or key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "job_role": job})
    return out


def _unfinished_claim(restaurant_id, wanted, phone, db_path):
    """This phone's own claim of `wanted` that stopped before its PIN was set.

    claim_staff_name is several commits (identity, membership, claim stamp,
    PIN, token). A failure after the membership was written — a lock, a full
    disk — left the name taken with no PIN, and the employee's retry was
    refused as "not available", with no way back but the manager (DATA-58).
    The same verified phone may finish it: the steps it repeats are
    idempotent, and the signup token is only consumed once it is done."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT m.employee_name, m.job_role FROM memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.restaurant_id=? AND m.is_active=1 AND m.pin_hash IS NULL "
            "AND LOWER(TRIM(m.employee_name))=LOWER(?) "
            "AND (m.claimed_by_phone=? OR u.phone=?)",
            (restaurant_id, wanted, phone, phone)).fetchone()
    finally:
        conn.close()
    return {"name": row["employee_name"], "job_role": row["job_role"]} if row else None


def claim_staff_name(signup_token: str, restaurant_id: int, employee_name: str,
                     pin: str, db_path: str = DB_PATH) -> dict:
    """Turn a verified phone plus a roster name into a real staff account.

    users and memberships are written together here, which is why
    users.restaurant_id never has to become nullable. A phone that already has
    an identity (this employee works at another location, or came back) gets a
    SECOND MEMBERSHIP on the same identity rather than a duplicate account —
    which is the whole reason identity and membership are separate tables.

    Raises SignupError with a message safe to show the employee.
    """
    phone = phone_for_signup_token(signup_token, db_path=db_path)
    if not phone:
        raise SignupError("That signup expired. Start again.")
    wanted = (employee_name or "").strip()
    if not wanted:
        raise SignupError("Pick your name from the list.")
    try:
        pin = validate_pin(pin)
    except PinError as pe:
        raise SignupError(str(pe))

    # The name must be on the roster AND unclaimed, re-checked here rather
    # than trusted from the list the client was shown a moment ago.
    available = {c["name"].strip().lower(): c for c in
                 claimable_names(restaurant_id, db_path=db_path)}
    match = available.get(wanted.lower()) or _unfinished_claim(restaurant_id, wanted, phone, db_path)
    if not match:
        raise SignupError("That name isn't available. Ask your manager.")

    # A name an owner unlinked is claimable again — someone new really may be
    # hired into it — but not by the phone it was taken away from.
    conn = get_conn(db_path)
    try:
        blocked = conn.execute(
            "SELECT 1 FROM memberships WHERE restaurant_id=? AND is_active=0 "
            "AND claimed_by_phone=?", (restaurant_id, phone)).fetchone()
    finally:
        conn.close()
    if blocked:
        raise SignupError("This phone can't be used here. Ask your manager.")

    existing = get_user_by_phone(phone, db_path=db_path)
    if existing:
        user_id = existing["id"]
    else:
        base = "".join(c for c in wanted.lower() if c.isalnum()) or "staff"
        username, suffix = f"{base}.{restaurant_id}", 1
        while get_user_by_username(username, db_path=db_path):
            suffix += 1
            username = f"{base}.{restaurant_id}.{suffix}"
        # A PIN identity must not also be a password login — that would be a
        # second, weaker way into the same account.
        user_id = create_user(restaurant_id, username, f"{username}@staff.invalid",
                              secrets.token_urlsafe(32), db_path=db_path, generated=True)
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE users SET phone=? WHERE id=?", (phone, user_id))
            conn.commit()
        finally:
            conn.close()

    membership = upsert_membership(user_id, restaurant_id, "employee",
                                   employee_name=match["name"],
                                   job_role=match.get("job_role"), db_path=db_path)
    conn = get_conn(db_path)
    try:
        conn.execute(
            "UPDATE memberships SET claimed_by_phone=?, claimed_at=datetime('now') WHERE id=?",
            (phone, membership["id"]))
        conn.commit()
    finally:
        conn.close()
    set_membership_pin(membership["id"], restaurant_id, pin, db_path=db_path)
    _consume_signup_token(signup_token, db_path=db_path)
    return {"user_id": user_id, "membership_id": membership["id"],
            "employee_name": match["name"], "job_role": match.get("job_role")}


def unlink_claimed_membership(membership_id: int, restaurant_id: int,
                              db_path: str = DB_PATH) -> bool:
    """Owner's undo for a name claimed by the wrong person.

    Deactivates the membership (which ends its sessions) so the name returns
    to the claimable pool for the person it belongs to, while claimed_by_phone
    stays on the row — that is what stops the same phone taking it again.
    """
    return set_membership_active(membership_id, restaurant_id, False, db_path=db_path)


def get_user_by_phone(phone: str, db_path: str = DB_PATH) -> Optional[dict]:
    phone = normalize_phone(phone)
    if not phone:
        return None
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE phone=? AND is_active=1 ORDER BY id LIMIT 1",
            (phone,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def restaurant_for_staff_code(code: str, db_path: str = DB_PATH) -> Optional[int]:
    """The restaurant behind either kind of staff code.

    There are two because they serve different moments — a 32-character token
    to tap in a link, a 6-character code to type off a whiteboard — but an
    employee should never have to know which one they are holding. Both
    resolve here, and both stop resolving together when an owner rotates.
    """
    return (restaurant_for_portal_token(code, db_path=db_path)
            or restaurant_for_join_code(code, db_path=db_path))


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

# The one password policy every owner-facing path applies — reset, change,
# admin reset: at least 8 characters, and not in a known breach (HIBP,
# security.password_pwned, which fails open). create_user holds every typed
# password to it; admin create-client passed whatever was typed straight in
# (SECURITY-9). Callers that mint a random secret themselves pass
# generated=True.
PASSWORD_MIN_LENGTH = 8

# Switched off only by the test suite (tests/conftest.py), whose fixtures
# create throwaway logins with short passwords; the policy's own tests switch
# it back on. Production never changes it.
ENFORCE_PASSWORD_POLICY = True


class PasswordPolicyError(ValueError):
    """A password the policy refuses. str() is the sentence to show."""
    @property
    def message(self):
        return self.args[0] if self.args else "Choose a different password."


def password_policy_error(password):
    """The sentence refusing `password`, or None when it passes."""
    if len(password or "") < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters."
    try:
        import security as _sec_pp
        if _sec_pp.password_pwned(password):
            return _sec_pp.PWNED_MESSAGE
    except Exception:
        pass    # the breach check fails open, as it does everywhere else
    return None


def create_user(restaurant_id: int, username: str, email: str,
                password: str, is_admin: bool = False,
                db_path: str = DB_PATH, role: str = None, generated: bool = False) -> int:
    """A login. `password` must pass password_policy_error unless the caller
    minted it at random (generated=True): a PIN identity's unused password,
    a teammate's temporary one, a support login's placeholder."""
    if not generated and ENFORCE_PASSWORD_POLICY:
        err = password_policy_error(password)
        if err:
            raise PasswordPolicyError(err)
    conn = get_conn(db_path)
    # Scored/stamped at creation too, not just on a later change — an
    # account whose password was never touched since Will set it up used
    # to have neither value, showing the Security sheet's tile a placeholder
    # "Set" with no real information behind it.
    from zoneinfo import ZoneInfo as _ZI_cu
    now = datetime.now(_ZI_cu('America/Chicago')).strftime('%Y-%m-%dT%H:%M:%S')
    # A staff-PIN identity (the @staff.invalid address both staff paths
    # mint) is an employee from the start, never the 'client' column default
    # a console login gets (SEC-1).
    #
    # `role`, when given, is written with the row itself. A teammate invite
    # used to insert the 'client' default — the primary-login role — and
    # narrow it in a second commit, so for that window, or for good if the
    # second write failed, an invited teammate held owner-level access
    # (DATA-56).
    if not role:
        role = "employee" if email.lower().strip().endswith("@staff.invalid") else "client"
    cur = conn.execute("""
        INSERT INTO users (restaurant_id, username, email, password_hash, is_admin, password_changed_at,
                           password_strength, role)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (restaurant_id, username.lower().strip(), email.lower().strip(),
          generate_password_hash(password), int(is_admin), now, password_strength(password), role))
    conn.commit()
    uid = cur.lastrowid
    conn.close()
    return uid

def clear_user_two_factor(user_id: int, db_path: str = DB_PATH) -> None:
    """An internal login's own second factor off: the flag and method, its
    backup codes, its authenticator-app secrets, and the devices it
    remembered."""
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE users SET two_fa_enabled=0, two_fa_method=NULL WHERE id=?", (user_id,))
        conn.execute("DELETE FROM user_backup_codes WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM user_totp WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM trusted_devices WHERE user_id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def apply_boot_two_factor_resets(env=None, db_path: str = DB_PATH) -> list:
    """Break-glass for an internal login locked out by its own second factor
    (codes not arriving, backup codes lost): ADMIN_2FA_RESET_USERNAMES
    (comma-separated) turns those logins' own two-factor off at boot. Only
    internal logins — a restaurant's switch is not touched. Set it, redeploy,
    sign in, turn two-factor back on, unset it. Every reset is recorded and,
    on Railway, sent to the operator. Returns the usernames reset."""
    env = os.environ if env is None else env
    names = [n.strip().lower() for n in (env.get("ADMIN_2FA_RESET_USERNAMES") or "").split(",") if n.strip()]
    done = []
    for n in names:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT id, is_admin, role FROM users WHERE LOWER(username)=?", (n,)).fetchone()
        finally:
            conn.close()
        if not row or not is_internal_login(dict(row)):
            print(f"[auth] ADMIN_2FA_RESET_USERNAMES: {n!r} is not an admin or support login — skipped")
            continue
        clear_user_two_factor(row["id"], db_path=db_path)
        done.append(n)
        line = (f"Break-glass: two-factor turned off for {n} at boot (ADMIN_2FA_RESET_USERNAMES). "
                "Turn it back on at /admin/two-factor, then unset the variable.")
        print("SECURITY WARNING: " + line)
        try:
            import admin_events
            admin_events.record("admin", "two_factor_reset_break_glass", summary=line[:300],
                                payload={"username": n}, db_path=db_path)
        except Exception:
            pass
        try:
            import config as _cfg_bg
            if _cfg_bg.on_railway():
                import ops
                ops.alert_will("Two-factor reset at boot for " + n, [line])
        except Exception:
            pass
    return done


ADMIN_HOME_NAME = "Cavnar AI Admin"


def ensure_admin_login(db_path: str = DB_PATH, env=None) -> dict:
    """Boot: create the operator's admin login when there is none at all
    (SECURITY-11, #136). It used to create one whenever no user was named
    ADMIN_USERNAME — so a rename, or deleting the admin's home restaurant
    (models.delete_restaurant takes its users), re-armed it — with
    ADMIN_PASSWORD or a password written in this repo, homed on whatever
    restaurant row came first, which it then relabelled billing 'internal'.

      - Seeds only when no is_admin row exists.
      - Never without ADMIN_PASSWORD, and only one that passes the password
        policy; otherwise nothing is created and the refusal is loud.
      - Homed on a restaurant row made for it (ADMIN_HOME_NAME, billing
        'internal'), reusing one made before; no other row is touched.
      - A creation or refusal is recorded in admin_events and, on Railway,
        sent to the operator (ops.alert_will).

    Returns {"action": "exists" | "created" | "refused", "reason", "user_id"}."""
    env = os.environ if env is None else env
    conn = get_conn(db_path)
    try:
        admin = conn.execute("SELECT id FROM users WHERE is_admin=1 LIMIT 1").fetchone()
    finally:
        conn.close()
    if admin:
        return {"action": "exists", "reason": None, "user_id": admin["id"]}
    username = (env.get("ADMIN_USERNAME") or "will").strip().lower()
    password = env.get("ADMIN_PASSWORD") or ""
    try:
        import config as _cfg_seed
        email = (env.get("ADMIN_EMAIL") or _cfg_seed.will_email() or "will@cavnar.ai").strip().lower()
        on_railway = _cfg_seed.on_railway()
    except Exception:
        email, on_railway = (env.get("ADMIN_EMAIL") or "will@cavnar.ai").strip().lower(), False
    reason = None
    if not password:
        reason = "ADMIN_PASSWORD is not set"
    else:
        err = password_policy_error(password)
        if err:
            reason = f"ADMIN_PASSWORD fails the password policy: {err}"
    uid = None
    if not reason:
        conn = get_conn(db_path)
        try:
            home = conn.execute("SELECT id FROM restaurants WHERE name=? AND billing_status='internal' "
                                "ORDER BY id LIMIT 1", (ADMIN_HOME_NAME,)).fetchone()
        finally:
            conn.close()
        try:
            if home:
                home_id = home["id"]
            else:
                from models import create_restaurant as _cr_seed, Restaurant as _R_seed
                home_id = _cr_seed(_R_seed(name=ADMIN_HOME_NAME, owner_email=email), db_path=db_path)
                conn = get_conn(db_path)
                try:
                    conn.execute("UPDATE restaurants SET billing_status='internal' WHERE id=?", (home_id,))
                    conn.commit()
                finally:
                    conn.close()
            # Checked against the policy just above; generated=True only
            # spares a second breach lookup.
            uid = create_user(home_id, username, email, password, is_admin=True, db_path=db_path, generated=True)
        except Exception as exc:
            reason = f"could not create the admin login: {exc}"
    action = "created" if uid else "refused"
    line = (f"Admin login {username!r} created on the {ADMIN_HOME_NAME} row at boot — no admin existed."
            if uid else f"No admin login exists and none was created: {reason}. "
                        "Set ADMIN_PASSWORD (8+ characters, not breached) and redeploy.")
    print(("" if uid else "SECURITY WARNING: ") + line)
    try:
        import admin_events
        admin_events.record("admin", "admin_seed_" + action, summary=line[:300],
                            payload={"username": username, "reason": reason}, db_path=db_path)
    except Exception:
        pass
    if on_railway:
        try:
            import ops
            ops.alert_will("Admin login " + ("created at boot" if uid else "missing — seed refused"), [line])
        except Exception:
            pass
    return {"action": action, "reason": reason, "user_id": uid}


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

_DUMMY_HASH = None


def _dummy_password_hash() -> str:
    """A hash made with the same method and cost as a real one, of a random
    secret nobody knows — what verify_password checks an unknown username's
    password against so both branches cost the same."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = generate_password_hash(secrets.token_hex(16))
    return _DUMMY_HASH


def verify_password(username: str, password: str,
                    db_path: str = DB_PATH) -> Optional[dict]:
    user = get_user_by_username(username, db_path)
    if not user:
        # Pay for one hash anyway (SEC-35). Returning here before any hashing
        # made an unknown username answer in microseconds and a known one in
        # ~100ms, so the login form enumerated usernames by timing.
        check_password_hash(_dummy_password_hash(), password or "")
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


def update_password(user_id: int, new_password: str, keep_token: str = None, db_path: str = DB_PATH):
    """Every password write goes through here: a reset, a change, an admin
    reset. It also ends every other session of this login (SEC-8 — a reset
    used to leave an intruder's session alive for 30 days) and clears
    must_reset_password, which a freeze or "This wasn't me" sets and which
    nothing ever cleared, so the owner could never sign in again (SEC-7).
    `keep_token` is the session making the change, which stays signed in."""
    conn = get_conn(db_path)
    from zoneinfo import ZoneInfo as _ZI_pw
    conn.execute(
        "UPDATE users SET password_hash=?, password_changed_at=?, password_strength=?, "
        "must_reset_password=0, reset_token=NULL, reset_token_expires=NULL WHERE id=?",
        (
            generate_password_hash(new_password),
            datetime.now(_ZI_pw('America/Chicago')).strftime('%Y-%m-%dT%H:%M:%S'),
            password_strength(new_password),
            user_id,
        ),
    )
    if keep_token:
        conn.execute("DELETE FROM sessions WHERE user_id=? AND token<>?", (user_id, hash_session_token(keep_token)))
    else:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def current_session_token():
    """The session token on this request (web cookie or iOS bearer), for a
    password change that must keep its own session."""
    try:
        auth_h = request.headers.get("Authorization", "")
        if auth_h.startswith("Bearer "):
            return auth_h[7:].strip() or None
        return request.cookies.get("session_token") or None
    except Exception:
        return None

# Roles an owner can give a login on their own team, with what the owner
# sees them called. 'client' is the stored value for an owner login (see the
# note in invite_team_member); a second one is a co-owner with every right the
# first has. 'owner' (the multi-location login) and 'employee' (PIN staff) are
# never assigned from here.
TEAM_ROLES = {"client": "Co-owner", "manager": "Manager", "member": "Teammate"}
_PRINCIPAL_ROLES = ("client", "owner")


def _principal_count(conn, restaurant_id, excluding=None):
    """Active, non-admin owner logins at this restaurant."""
    return conn.execute(
        "SELECT COUNT(*) FROM users WHERE restaurant_id=? AND is_active=1 AND is_admin=0 "
        "AND COALESCE(NULLIF(role,''),'client') IN ('client','owner') AND id != ?",
        (restaurant_id, excluding or -1)).fetchone()[0]


def invite_team_member(restaurant_id: int, name: str, email: str,
                       db_path: str = DB_PATH, role: str = "member") -> dict:
    """Self-serve version of what Will already does by hand for every
    client's primary login: create_user() already accepts an existing
    restaurant_id (its only two callers just always happen to pass a
    freshly-created one), so a second user on the same restaurant needs no
    new creation path — just a generated username/temp password and the
    same create_user() call. Returns {"ok": True, "user_id", "username",
    "temp_password"} on success, or {"ok": False, "error": "..."} on a
    duplicate username/email — never raises."""
    if role not in TEAM_ROLES:
        return {"ok": False, "error": "Pick Co-owner, Manager or Teammate."}
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
    # 'member' marks an invited teammate. The role column's existing
    # vocabulary is 'client' (every restaurant's primary login, the default)
    # and 'owner' (Will's multi-restaurant login that can switch its active
    # restaurant — see get_session_user). The first cut of this feature
    # gated invite/revoke on role == 'owner', which no client login has, so
    # the whole Team feature was invisible and 403'd for every real account.
    # The distinction that actually matters is "primary login vs. someone
    # that login invited", and that's what this value records — written
    # with the row, never narrowed afterwards (DATA-56).
    try:
        user_id = create_user(restaurant_id, candidate, email, temp_password,
                              is_admin=False, db_path=db_path, role=role, generated=True)
    except sqlite3.IntegrityError:
        return {"ok": False, "error": "That email is already in use."}
    # An invited teammate also gets a membership, so authorization for this
    # login resolves through the same Identity → Tenant → Role path every
    # other account now uses rather than falling back to users.role.
    try:
        upsert_membership(user_id, restaurant_id, role, employee_name=name,
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
    return {"ok": True, "user_id": user_id, "username": candidate, "temp_password": temp_password,
            "role": role}


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


def set_team_role(restaurant_id: int, user_id: int, role: str, acting_user_id: int,
                  db_path: str = DB_PATH) -> str:
    """Make a login on this restaurant a Co-owner, Manager or Teammate.
    Returns the new role; raises TeamAccessError (owner-facing text) for a
    role outside TEAM_ROLES, your own login, another restaurant's login, a
    staff or admin login, or demoting the last owner.

    users.role and the restaurant's membership row are written together:
    get_session_user reads the membership first, so updating only users.role
    would leave the old role in force. Both are read per request, so the
    change applies on that person's next click."""
    if role not in TEAM_ROLES:
        raise TeamAccessError("Pick Co-owner, Manager or Teammate.")
    if user_id == acting_user_id:
        raise TeamAccessError("You can't change your own role.")
    conn = get_conn(db_path)
    try:
        u = conn.execute("SELECT COALESCE(NULLIF(role,''),'client') AS role, is_admin FROM users "
                         "WHERE id=? AND restaurant_id=? AND is_active=1", (user_id, restaurant_id)).fetchone()
        if not u:
            raise TeamAccessError("That login isn't on this restaurant's team.")
        if u["is_admin"] or u["role"] not in TEAM_ROLES:
            raise TeamAccessError("That login's role can't be changed here.")
        if u["role"] in _PRINCIPAL_ROLES and role not in _PRINCIPAL_ROLES \
                and _principal_count(conn, restaurant_id, excluding=user_id) == 0:
            raise TeamAccessError("Every restaurant needs at least one owner.")
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        try:
            conn.execute("UPDATE memberships SET role=?, updated_at=datetime('now') "
                         "WHERE user_id=? AND restaurant_id=?", (role, user_id, restaurant_id))
        except sqlite3.OperationalError:
            pass    # a database predating memberships: users.role is the whole story
        conn.commit()
    finally:
        conn.close()
    return role


def get_team_members(restaurant_id: int, db_path: str = DB_PATH) -> list[dict]:
    conn = get_conn(db_path)
    # Console logins only. Staff-PIN identities (role 'employee', the
    # @staff.invalid address) are managed under Staff, and listing them here
    # showed an employee as an account holder (SEC-29).
    rows = conn.execute("""
        SELECT id, username, email, role, created_at, last_login, is_active
        FROM users WHERE restaurant_id=? AND is_active=1
          AND COALESCE(role, 'client') <> 'employee' AND email NOT LIKE '%@staff.invalid'
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
    # With co-owners, one owner can remove another — but never the last one,
    # or the restaurant is left with nobody who can administer it.
    target_role = conn.execute("SELECT COALESCE(NULLIF(role,''),'client') AS r FROM users WHERE id=?",
                               (user_id,)).fetchone()["r"]
    if target_role in _PRINCIPAL_ROLES and _principal_count(conn, restaurant_id, excluding=user_id) == 0:
        conn.close()
        return {"ok": False, "error": "Can't remove the only owner."}
    conn.execute("UPDATE users SET is_active=0 WHERE id=?", (user_id,))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    # Their private memory notes leave with them — archived for the owner to
    # see and keep or let go, no longer steering prompts no one can read
    # (memory re-audit 9/29/26, R3 revoked_login). Never blocks the revoke.
    try:
        import owner_memory
        owner_memory.retire_departed(restaurant_id, db_path=db_path)
    except Exception as e:
        print(f"[auth] departed login's memory not retired rid={restaurant_id}: {e}")
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


def install_proxy_fix(app):
    """Trust exactly one proxy hop, in one place.

    Railway terminates TLS at its edge, so every request reaches this process
    over plain HTTP with the real scheme and client address in X-Forwarded-*.
    Without this, request.is_secure is always False, request.remote_addr is
    the proxy for every visitor (which quietly merges the whole internet into
    one bucket for any per-IP throttle), and url_for(_external=True) builds
    http:// links.

    It exists as a function rather than three lines in hosted_dashboard so the
    choice — one hop, deliberately, not "however many the header claims" — is
    testable. Handlers must never read X-Forwarded-* themselves; the staff
    cookie once decided its Secure flag that way, which is an unvalidated
    header deciding a security property.
    """
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    return app


def cookies_require_secure() -> bool:
    """Whether session cookies must carry the Secure flag.

    One signal for every cookie this app sets. The staff cookie used to decide
    this for itself with `request.headers.get("X-Forwarded-Proto") == "https"`,
    which is a different question with a different answer: it asks the CLIENT
    (through a proxy header the app never validated, normalised, or installed
    ProxyFix to trust) instead of asking the DEPLOYMENT. If Railway's proxy
    ever spelled that header differently, staff sessions would have gone out
    over plaintext with no error and nothing to notice it by.

    Deployment is the right question because the answer cannot be influenced
    by a request: on Railway, everything is HTTPS, so Secure is unconditional.
    CAVNAR_FORCE_SECURE_COOKIES exists for any other TLS-terminating host.
    """
    import os
    import config
    return bool(config.on_railway() or os.getenv("CAVNAR_FORCE_SECURE_COOKIES"))


def create_session(user_id: int, days: int = 30,
                   ip_address: str = None, user_agent: str = None,
                   device_type: str = "web", device_id: str = None,
                   restaurant_id: int = None, hours: int = None,
                   db_path: str = DB_PATH, *, second_factor: bool = False,
                   password_verified_at=None) -> str:
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
    per-device identity to key off there.

    `second_factor` stamps two_factor_at: this sign-in passed a code, a
    backup code or a remembered device. `password_verified_at` stamps
    reauth_at — True for "the password was typed just now", or the UTC
    time it was typed (a 2FA sign-in passes the moment its challenge
    started). An internal login's session is capped at ADMIN_SESSION_HOURS
    whatever the caller asked for."""
    token = secrets.token_urlsafe(32)
    # Stored as a hash, like session tokens: a copy of this table must not
    # hand anyone a working "sign the owner out everywhere" link, nor the
    # live session token it names (SEC-37).
    from datetime import timedelta
    # `hours` is the staff-PIN path: a shift-length session rather than a
    # month, because those run on shared devices sitting on a pass or a host
    # stand. Everything else about the row is identical.
    span = timedelta(hours=hours) if hours else timedelta(days=days)
    conn = get_conn(db_path)
    try:
        who = conn.execute("SELECT is_admin, role FROM users WHERE id=?", (user_id,)).fetchone()
    except Exception:
        who = None
    if who is not None and is_internal_login({"is_admin": who["is_admin"], "role": who["role"]}):
        span = min(span, timedelta(hours=ADMIN_SESSION_HOURS))
    # SQLite's own text form, so every `expires_at > datetime('now')` in the
    # codebase compares instants, not strings (SECURITY-14).
    expires = sql_utc(datetime.now(timezone.utc) + span)
    now_sql = sql_utc()
    if password_verified_at is True:
        reauth_at = now_sql
    elif password_verified_at:
        reauth_at = str(password_verified_at)
    else:
        reauth_at = None
    # Prune expired sessions for this user (keep active ones for multi-device support)
    conn.execute("DELETE FROM sessions WHERE user_id=? AND datetime(expires_at) <= datetime('now')", (user_id,))
    if device_id:
        conn.execute("DELETE FROM sessions WHERE user_id=? AND device_id=?", (user_id, device_id))
    conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at, ip_address, user_agent, device_type, device_id, "
        "staff_restaurant_id, two_factor_at, reauth_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (hash_session_token(token), user_id, expires, ip_address or "", user_agent or "", device_type, device_id or "",
         restaurant_id if device_type == "staff_pin" else None,
         now_sql if second_factor else None, reauth_at)
    )
    # The event names the KIND of sign-in, not just that one happened. A staff
    # PIN sign-in and an owner console sign-in are different security events
    # with different expected patterns, and reading `device_type` alone to tell
    # them apart works only while that column happens to be populated — it is
    # nullable and defaulted, so an audit query would silently lump them.
    conn.execute(
        "INSERT INTO login_history (user_id, restaurant_id, event, ip_address, user_agent, device_type) VALUES (?,?,?,?,?,?)",
        (user_id, restaurant_id, "staff_login" if device_type == "staff_pin" else "login",
         ip_address or "", user_agent or "", device_type)
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


def session_handle(token_hash: str) -> str:
    """A stable, opaque handle for one session row, for lists and for the
    admin's single-session revoke: the first 16 hex characters of the stored
    SHA-256. It names the row; it cannot be turned back into the token."""
    return (token_hash or "")[:16]


def get_sessions_for_user(user_id: int, current_token: str = None,
                          db_path: str = DB_PATH) -> list:
    """Return all active sessions for a user, marking which is current."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT token, created_at, last_active, ip_address, user_agent, device_type, expires_at
        FROM sessions
        WHERE user_id=? AND datetime(expires_at) > datetime('now')
        ORDER BY last_active DESC
    """, (user_id,)).fetchall()
    conn.close()
    result = []
    for row in rows:
        result.append({
            # The stored value is a hash now, so the "hint" is just a stable
            # opaque handle for the UI, never part of the real token.
            "token_hint": row["token"][-6:],
            "session_id": session_handle(row["token"]),
            "is_current": bool(current_token) and row["token"] == hash_session_token(current_token),
            "created_at": row["created_at"],
            "last_active": row["last_active"],
            "expires_at": row["expires_at"],
            "ip_address": row["ip_address"] or "",
            "user_agent": row["user_agent"] or "",
            "device_type": row["device_type"] or "web",
            # A view-as session sits in the owner's own list: say what it is
            # instead of "Unknown device" (SECURITY-3).
            "is_view_as": (row["device_type"] or "") == "admin-view-as",
        })
    return result


def create_view_as_session(target_user_id: int, acting_admin: dict, *, read_only: bool,
                           ip_address: str = None, user_agent: str = None,
                           db_path: str = DB_PATH) -> str:
    """Mint an admin view-as session for the login `target_user_id`.

    VIEW_AS_HOURS from now, absolute (the session lookup refuses it past
    that, whatever expires_at says). The acting admin and read-only live on
    the session row, so the lookup can attribute every write and a missing
    row can never turn a support view writable. Returns the token."""
    from datetime import timedelta
    token = secrets.token_urlsafe(32)
    expires = sql_utc(datetime.now(timezone.utc) + timedelta(hours=VIEW_AS_HOURS))
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at, last_active, device_type, ip_address, user_agent, "
            "acting_admin_id, read_only) VALUES (?,?,?,datetime('now'),?,?,?,?,?)",
            (hash_session_token(token), target_user_id, expires, "admin-view-as", ip_address or "",
             (user_agent or "")[:400], acting_admin.get("id"), 1 if read_only else 0))
        conn.commit()
    finally:
        conn.close()
    try:
        record_view_as_session(token, acting_admin.get("id"), read_only=read_only, db_path=db_path)
    except Exception:
        pass    # history only; the session row is the authority
    return token


def revoke_other_sessions(user_id: int, current_token: str,
                          db_path: str = DB_PATH):
    """Delete all sessions for a user except the current one."""
    conn = get_conn(db_path)
    conn.execute("DELETE FROM sessions WHERE user_id=? AND token!=?",
                 (user_id, hash_session_token(current_token or "")))
    conn.commit()
    conn.close()

INACTIVITY_HOURS = 8  # Log out after 8 hours of inactivity

# Device kinds the 8-hour inactivity rule does not apply to, each for its own
# reason:
#
#   ios       — the window is a "walked away from the laptop" rule. It would
#               log a phone out (2FA and all) every time an owner checked the
#               app once a day. iOS relies on the 30-day hard expiry plus the
#               app's own Face ID lock on foreground.
#   staff_pin — these already carry a deliberately short hard expiry
#               (STAFF_SESSION_HOURS, one shift). Leaving them subject to the
#               8-hour rule as well made that constant dead code: an employee
#               who signed in at the start of a double and did not touch the
#               portal was signed out mid-shift, which is precisely the
#               friction a shift-length TTL was chosen to avoid. The hard
#               expiry IS the control here.
_INACTIVITY_EXEMPT_DEVICES = frozenset({"ios", "staff_pin"})

# The session row plus the membership for the restaurant the session is ACTING
# in, in one round trip. The active-restaurant overlay is expressed in SQL
# rather than applied afterwards so the join lands on the same restaurant the
# Python below resolves to; it deliberately reads u.role (the stored role),
# because that is what the owner overlay has always keyed off — the membership
# role that replaces it further down is a consequence of this join, not an
# input to it.
_SESSION_USER_SQL = """
    SELECT u.*, s.last_active, s.active_restaurant_id, s.device_type, s.staff_restaurant_id,
           s.created_at      AS _s_created_at,
           s.two_factor_at   AS _s_two_factor_at,
           s.reauth_at       AS _s_reauth_at,
           s.acting_admin_id AS _s_acting_admin_id,
           s.read_only       AS _s_read_only,
           m.id            AS _m_id,
           m.role          AS _m_role,
           m.employee_name AS _m_employee_name
    FROM sessions s
    JOIN users u ON s.user_id = u.id
    LEFT JOIN memberships m
           ON m.user_id = u.id
          AND m.is_active = 1
          AND m.restaurant_id = CASE
                WHEN u.role = 'owner' AND s.active_restaurant_id IS NOT NULL
                THEN s.active_restaurant_id
                WHEN s.staff_restaurant_id IS NOT NULL THEN s.staff_restaurant_id
                ELSE u.restaurant_id END
    WHERE s.token=? AND datetime(s.expires_at) > datetime('now') AND u.is_active=1
"""

# datetime() on both sides: an instant, whichever text form the row holds
# (normalize_session_rows rewrites the old ISO rows at boot; this is the
# belt to that brace), and an unreadable value is refused, never kept.
_SESSION_USER_SQL_NO_MEMBERSHIP = """
    SELECT u.*, s.last_active, s.active_restaurant_id, s.device_type FROM sessions s
    JOIN users u ON s.user_id = u.id
    WHERE s.token=? AND datetime(s.expires_at) > datetime('now') AND u.is_active=1
"""

# The session keys get_session_user lifts off the row onto the user dict.
_SESSION_FIELDS = (("_s_created_at", "session_created_at"), ("_s_two_factor_at", "two_factor_at"),
                   ("_s_reauth_at", "reauth_at"), ("_s_acting_admin_id", "acting_admin_id"),
                   ("_s_read_only", "_session_read_only"))


def _session_too_old(created_at, hours) -> bool:
    """True when a session created at `created_at` (UTC) is past `hours`
    old — or when its age cannot be read, which fails closed."""
    born = _parse_utc(created_at)
    if born is None:
        return True
    from datetime import timedelta
    return datetime.utcnow() - born > timedelta(hours=hours)


def _grants_for(conn, user_id, restaurant_id):
    """This login's owner-granted permissions at the location it is acting
    in. On the connection get_session_user already holds; a database without
    the table (an old fixture) simply has no grants."""
    try:
        return frozenset(r[0] for r in conn.execute(
            "SELECT permission FROM permission_grants WHERE user_id=? AND restaurant_id=?",
            (user_id, restaurant_id)).fetchall())
    except Exception:
        return frozenset()


class TeamAccessError(ValueError):
    """A refusal from set_grant / set_morning_brief_pref, worded for the
    owner. Routes return .message and nothing else: only this class's own
    text ever reaches a client, never an arbitrary exception's str()."""
    @property
    def message(self):
        return self.args[0] if self.args else "Couldn't update their access."


def get_team_access(restaurant_id, db_path: str = DB_PATH) -> dict:
    """{user_id: {"grants": set, "morning_brief": bool}} for every login at
    this restaurant — what Account → Team shows next to each person."""
    from permissions import GRANTABLE_ROLES, normalize_role
    conn = get_conn(db_path)
    try:
        users = conn.execute("SELECT id, role FROM users WHERE restaurant_id=? AND is_active=1",
                             (restaurant_id,)).fetchall()
        grants = conn.execute("SELECT user_id, permission FROM permission_grants WHERE restaurant_id=?",
                              (restaurant_id,)).fetchall()
        prefs = {r["user_id"]: r["morning_brief"] for r in conn.execute(
            "SELECT user_id, morning_brief FROM login_prefs WHERE restaurant_id=?", (restaurant_id,))}
    finally:
        conn.close()
    out = {}
    for u in users:
        out[u["id"]] = {"grants": set(), "role": normalize_role(u["role"]),
                        "morning_brief": morning_brief_default(u["role"])
                        if prefs.get(u["id"]) is None else bool(prefs[u["id"]])}
    for g in grants:
        if g["user_id"] in out and out[g["user_id"]]["role"] in GRANTABLE_ROLES:
            out[g["user_id"]]["grants"].add(g["permission"])
    return out


def morning_brief_default(role) -> bool:
    """Owners and managers get the morning brief unless someone turns it off;
    legacy teammates only when turned on."""
    from permissions import normalize_role
    return normalize_role(role) in ("owner", "client", "manager")


def set_grant(restaurant_id, user_id, permission, enabled, granted_by=None, db_path: str = DB_PATH):
    """Grant or revoke one GRANTABLE permission for a login at this restaurant.
    Raises ValueError for anything outside the fixed list, a login at another
    restaurant, or a role that can't take grants."""
    from permissions import GRANTABLE, GRANTABLE_ROLES, normalize_role
    if permission not in GRANTABLE:
        raise TeamAccessError("that access can't be granted")
    conn = get_conn(db_path)
    try:
        u = conn.execute("SELECT role FROM users WHERE id=? AND restaurant_id=? AND is_active=1",
                         (user_id, restaurant_id)).fetchone()
        if not u:
            raise TeamAccessError("that login isn't on this restaurant's team")
        if normalize_role(u["role"]) not in GRANTABLE_ROLES:
            raise TeamAccessError("owners already see everything; staff logins can't be granted access")
        if enabled:
            conn.execute("INSERT OR IGNORE INTO permission_grants (user_id, restaurant_id, permission, "
                         "granted_by) VALUES (?,?,?,?)", (user_id, restaurant_id, permission, granted_by))
        else:
            conn.execute("DELETE FROM permission_grants WHERE user_id=? AND restaurant_id=? AND permission=?",
                         (user_id, restaurant_id, permission))
        conn.commit()
    finally:
        conn.close()


def set_morning_brief_pref(restaurant_id, user_id, enabled, db_path: str = DB_PATH):
    """Whether this login gets THIS location's morning brief. A login is on
    the location's team when it is based there, holds an active membership
    there, or is the group's owner (based at another location of the same
    group — _still_in_group, the switcher's rule): morning_brief.recipients
    adds a group owner to every sibling's brief, and the setter used to
    refuse them ("that login isn't on this restaurant's team"), so a
    three-location owner could not turn off two of their three briefs
    (memory audit 9/29/26, owner_layers)."""
    conn = get_conn(db_path)
    try:
        u = conn.execute("SELECT role, restaurant_id FROM users WHERE id=? AND is_active=1",
                         (user_id,)).fetchone()
        on_team = bool(u) and int(u["restaurant_id"]) == int(restaurant_id)
        if u and not on_team:
            try:
                on_team = bool(conn.execute("SELECT 1 FROM memberships WHERE user_id=? AND restaurant_id=? "
                                            "AND is_active=1", (user_id, restaurant_id)).fetchone())
            except Exception:
                on_team = False
        if u and not on_team:
            from permissions import normalize_role as _nr
            # The same logins morning_brief.recipients adds at a sibling.
            on_team = _nr(u["role"]) == "owner" and _still_in_group(conn, u["restaurant_id"], restaurant_id)
        if not on_team:
            raise TeamAccessError("that login isn't on this restaurant's team")
        from permissions import CONSOLE_ROLES, normalize_role
        if normalize_role(u["role"]) not in CONSOLE_ROLES:
            raise TeamAccessError("staff logins use the staff portal's pre-shift briefing instead")
        conn.execute("INSERT INTO login_prefs (user_id, restaurant_id, morning_brief, updated_at) "
                     "VALUES (?,?,?,datetime('now')) ON CONFLICT(user_id, restaurant_id) DO UPDATE SET "
                     "morning_brief=excluded.morning_brief, updated_at=excluded.updated_at",
                     (user_id, restaurant_id, 1 if enabled else 0))
        conn.commit()
    finally:
        conn.close()


_last_touch_failure = [0.0]


def _note_session_touch_failure(e):
    """A session could not record its activity: the database is refusing
    writes. The request carries on; ops hears about it at most once a
    minute rather than once per request."""
    import time as _t
    if _t.monotonic() - _last_touch_failure[0] < 60:
        return
    _last_touch_failure[0] = _t.monotonic()
    try:
        import ops
        ops.capture(e, job="session_last_active", context="the database refused a write; requests continue")
    except Exception:
        pass


def _still_in_group(conn, base_id, active_id) -> bool:
    """Whether `active_id` is still in the location group of `base_id`, by
    the same rule get_location_group applies: the same organization, or the
    same group name under the same owner email."""
    try:
        rows = {r["id"]: r for r in conn.execute(
            "SELECT id, location_group, owner_email, organization_id FROM restaurants WHERE id IN (?,?)",
            (base_id, active_id)).fetchall()}
    except Exception:
        return False
    b, a = rows.get(base_id), rows.get(active_id)
    if not a or not b or not (b["location_group"] or "").strip():
        return False
    if b["organization_id"] and a["organization_id"] and a["organization_id"] != b["organization_id"]:
        return False
    # The name and owner must still agree even inside one organization: an
    # admin moving a location to another owner edits these two fields.
    return ((a["location_group"] or "").strip() == (b["location_group"] or "").strip()
            and (a["owner_email"] or "").strip().lower() == (b["owner_email"] or "").strip().lower())


def get_session_user(token: str, db_path: str = DB_PATH, _revalidated: bool = False) -> Optional[dict]:
    if not token:
        return None
    conn = get_conn(db_path)
    joined = True
    try:
        row = conn.execute(_SESSION_USER_SQL, (hash_session_token(token),)).fetchone()
    except Exception:
        # A database predating the memberships table (an old fixture, a
        # partially-migrated copy) must still resolve sessions — the dual-read
        # was always a fallback, never a requirement.
        joined = False
        row = conn.execute(_SESSION_USER_SQL_NO_MEMBERSHIP,
                           (hash_session_token(token),)).fetchone()
    if not row:
        conn.close()
        return None
    device = row["device_type"] or "web"
    session_fields = {}
    if joined:
        for src, dst in _SESSION_FIELDS:
            try:
                session_fields[dst] = row[src]
            except (IndexError, KeyError):
                session_fields[dst] = None
    # Absolute lifetimes, whatever expires_at says — rows written before
    # these rules existed included. A view-as lasts VIEW_AS_HOURS from when it
    # was opened and must name the admin behind it (fail closed: one that
    # names nobody is refused). An internal login's own session lasts
    # ADMIN_SESSION_HOURS from sign-in.
    refuse = False
    if device == "admin-view-as":
        refuse = (not joined or session_fields.get("acting_admin_id") is None
                  or _session_too_old(session_fields.get("session_created_at"), VIEW_AS_HOURS))
    elif joined and is_internal_login({"is_admin": row["is_admin"], "role": row["role"]}):
        refuse = _session_too_old(session_fields.get("session_created_at"), ADMIN_SESSION_HOURS)
    acting_admin = None
    if not refuse and device == "admin-view-as":
        try:
            acting_admin = conn.execute("SELECT id, username, is_admin, role, is_active FROM users WHERE id=?",
                                        (session_fields.get("acting_admin_id"),)).fetchone()
        except Exception:
            acting_admin = None
        # The admin behind it must still be an active internal login: a
        # deactivated or demoted admin's view-as ends with their access.
        if not acting_admin or not acting_admin["is_active"] or not is_internal_login(dict(acting_admin)):
            refuse = True
    if refuse:
        try:
            conn.execute("DELETE FROM sessions WHERE token=?", (hash_session_token(token),))
            conn.commit()
        except Exception as _re:
            _note_session_touch_failure(_re)
        conn.close()
        return None
    # Check inactivity timeout, except for the device kinds above.
    is_ios_session = device in _INACTIVITY_EXEMPT_DEVICES
    last_active = row["last_active"] or ""
    if last_active and not is_ios_session:
        try:
            from datetime import datetime, timedelta
            la = datetime.fromisoformat(last_active[:19])  # always naive UTC, drops any tz suffix
            now_utc = datetime.utcnow()
            if now_utc - la > timedelta(hours=INACTIVITY_HOURS):
                # Session expired due to inactivity — delete it. The session
                # is refused whether or not the delete can be written.
                try:
                    conn.execute("DELETE FROM sessions WHERE token=?", (hash_session_token(token),))
                    conn.commit()
                except Exception as _de:
                    _note_session_touch_failure(_de)
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
    # Update last_active timestamp. A view-as session is no longer slid
    # forward here: its VIEW_AS_HOURS run from when it was opened, and use
    # never extends them (owner decision, 9/29/26).
    #
    # Best-effort and at most once a minute per session (DATA-1). This was an
    # unguarded write + commit on every authenticated request, so a full,
    # read-only or locked database failed every logged-in request — reads
    # included — after a 30 s busy wait each, while /health stayed green.
    # A minute of slack is nothing against an 8-hour inactivity window.
    _touch_due = True
    try:
        from datetime import datetime as _dt_la
        if (last_active
                and (_dt_la.utcnow() - _dt_la.fromisoformat(last_active[:19])).total_seconds() < 60):
            _touch_due = False
    except Exception:
        _touch_due = True
    if _touch_due:
        try:
            conn.execute("PRAGMA busy_timeout=1500")
            conn.execute("UPDATE sessions SET last_active=datetime('now') WHERE token=?", (hash_session_token(token),))
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            _note_session_touch_failure(e)
    user = dict(row)
    for _src, _dst in _SESSION_FIELDS:
        user.pop(_src, None)
    user.update(session_fields)
    session_read_only = user.pop("_session_read_only", None)
    staff_rid = user.pop("staff_restaurant_id", None)
    owner_switched = user.get("role") == "owner" and user.get("active_restaurant_id")

    # SEC-2: group membership used to be checked once, when the owner
    # switched. A location sold or moved to another owner stayed reachable
    # from every session already switched into it — for 30 days on iOS. The
    # switch is re-validated on every request (one indexed lookup, only for
    # a switched owner) and cleared the moment it no longer holds.
    # An active membership at the location is itself authorisation there.
    if owner_switched and user["active_restaurant_id"] != user.get("restaurant_id") \
            and not (joined and row["_m_id"] is not None) \
            and not _still_in_group(conn, user.get("restaurant_id"), user["active_restaurant_id"]):
        try:
            conn.execute("UPDATE sessions SET active_restaurant_id=NULL WHERE token=?", (hash_session_token(token),))
            conn.commit()
        except Exception as e:
            _note_session_touch_failure(e)
        conn.close()
        if _revalidated:
            return None
        return get_session_user(token, db_path=db_path, _revalidated=True)

    acting_rid = (user.get("active_restaurant_id") if owner_switched
                  else (staff_rid or user.get("restaurant_id")))
    user["grants"] = _grants_for(conn, user["id"], acting_rid)
    if device == "admin-view-as":
        # Read-only lives on the session row now; the old view_as_sessions
        # row still counts when it says read-only, never the other way.
        user["view_as_read_only"] = bool(session_read_only) or _view_as_read_only(conn, token)
        user["acting_admin"] = acting_admin["username"] if acting_admin else None
        user["acting_admin_role"] = "admin" if (acting_admin and acting_admin["is_admin"]) else "support"

    # SEC-1: fail closed. An identity that HAS memberships but none active
    # where this session acts is not authorised there — it used to fall back
    # to users.role ('client' for PIN identities), which opened the owner
    # console of a restaurant that had just removed the person. Only a login
    # with no membership rows at all (legacy, pre-backfill) keeps users.role,
    # and an owner acting in a validated group location keeps its role.
    if joined and row["_m_id"] is None and not owner_switched:
        try:
            has_any = conn.execute("SELECT 1 FROM memberships WHERE user_id=? LIMIT 1", (user["id"],)).fetchone()
        except Exception:
            has_any = None
        if has_any or user.get("role") == "employee":
            conn.close()
            return None
    conn.close()
    # For owners, active_restaurant_id in session overrides their base
    # restaurant_id; a staff-PIN session acts at the restaurant it was
    # minted for.
    if owner_switched:
        user["base_restaurant_id"] = user["restaurant_id"]
        user["restaurant_id"] = user["active_restaurant_id"]
    elif staff_rid:
        user["base_restaurant_id"] = staff_rid
        user["restaurant_id"] = staff_rid
    else:
        user["base_restaurant_id"] = user["restaurant_id"]

    # Dual-read: the membership for the restaurant this session is ACTING in
    # is the authority on role, because that is the whole point of separating
    # identity from authorization — the same person can be a manager at one
    # location and an employee at another, and users.role cannot express that.
    #
    # users.role is kept only for a login with no membership rows at all
    # (see the fail-closed check above). The membership is also what the
    # staff routes read to resolve an employee's own name.
    #
    # It rides along on the session query above rather than costing a second
    # one: this runs on all ~390 decorated routes plus the staff routes, and a
    # separate round trip per request was a permanent tax for a row that
    # changes about once a year per employee.
    m_id = row["_m_id"] if joined else None
    if m_id is not None:
        user["role"] = row["_m_role"]
        user["membership_id"] = m_id
        user["employee_name"] = row["_m_employee_name"]
    else:
        user["membership_id"] = None
        user["employee_name"] = None
    for k in ("_m_id", "_m_role", "_m_employee_name"):
        user.pop(k, None)
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
    """Set users.role on a restaurant's login (provisioning pairs it with
    upsert_membership). Never an admin row: nothing here should be able to
    rewrite the operator's own login (SECURITY #93). The console's role
    change goes through admin_set_role, which also writes the membership."""
    conn = get_conn(db_path)
    conn.execute("UPDATE users SET role=? WHERE id=? AND is_admin=0", (role, user_id))
    conn.commit()
    conn.close()


# The roles the admin console may give a restaurant's login: the owner
# path's three (TEAM_ROLES) plus 'owner', the multi-location login.
ADMIN_ASSIGNABLE_ROLES = ("client", "owner", "manager", "member")


def admin_set_role(user_id: int, role: str, acting_user_id: int = None, db_path: str = DB_PATH) -> dict:
    """The console's role change, on the owner path's rules (set_team_role):
    users.role and the membership at the login's home restaurant written
    together — get_session_user reads the membership, so writing users.role
    alone never took effect (SECURITY #60) — and the last owner cannot be
    demoted. Refuses admin rows (#93), support logins, staff PIN identities
    and your own login. Returns {"before", "after", "restaurant_id",
    "username"}; raises TeamAccessError with an operator-facing sentence."""
    role = (role or "").strip().lower()
    if role not in ADMIN_ASSIGNABLE_ROLES:
        raise TeamAccessError("Pick Owner (multi-location), Co-owner, Manager or Teammate.")
    if acting_user_id is not None and int(user_id) == int(acting_user_id):
        raise TeamAccessError("You can't change your own role.")
    conn = get_conn(db_path)
    try:
        u = conn.execute("SELECT id, restaurant_id, username, email, is_admin, "
                         "COALESCE(NULLIF(role,''),'client') AS role FROM users WHERE id=?", (user_id,)).fetchone()
        if not u:
            raise TeamAccessError("That login wasn't found.")
        if u["is_admin"]:
            raise TeamAccessError("An admin login's role can't be changed here.")
        if u["role"] == "support":
            raise TeamAccessError("A support login's access is managed under Support logins.")
        if u["role"] == "employee" or (u["email"] or "").lower().endswith("@staff.invalid"):
            raise TeamAccessError("A staff PIN identity has no console role.")
        before = u["role"]
        if before in _PRINCIPAL_ROLES and role not in _PRINCIPAL_ROLES \
                and _principal_count(conn, u["restaurant_id"], excluding=user_id) == 0:
            raise TeamAccessError("Every restaurant needs at least one owner.")
        conn.execute("UPDATE users SET role=? WHERE id=? AND is_admin=0", (role, user_id))
        try:
            cur = conn.execute("UPDATE memberships SET role=?, updated_at=datetime('now') "
                               "WHERE user_id=? AND restaurant_id=?", (role, user_id, u["restaurant_id"]))
            if not cur.rowcount:
                conn.execute("INSERT INTO memberships (user_id, restaurant_id, role) VALUES (?,?,?)",
                             (user_id, u["restaurant_id"], role))
        except sqlite3.OperationalError:
            pass    # a database predating memberships: users.role is the whole story
        conn.commit()
        return {"before": before, "after": role, "restaurant_id": u["restaurant_id"], "username": u["username"]}
    finally:
        conn.close()


def end_login_access(user_id: int, db_path: str = DB_PATH) -> dict:
    """End every way one login is signed in: its sessions (web, phone, staff
    PIN) and the devices it remembered for two-factor. Deactivate, and an
    admin's "sign out everywhere", share it. Returns the counts."""
    conn = get_conn(db_path)
    try:
        s = conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,)).rowcount or 0
        try:
            d = conn.execute("DELETE FROM trusted_devices WHERE user_id=?", (user_id,)).rowcount or 0
        except sqlite3.OperationalError:
            d = 0
        conn.commit()
        return {"sessions": s, "devices": d}
    finally:
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
    "/api/billing-info", "/api/account/pause", "/api/account/resume",
    # Closing the account stays reachable too, as it is on the phone
    # (/mobile/api/account below).
    "/api/account/request-deletion",
    "/account", "/mobile/api/account", "/mobile/api/login",
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
    # One person record (people.py, Friction #25): the team as Labor sees it.
    ("/api/people",                 "labor"),
    ("/mobile/api/people",          "labor"),
    # Task sheets by job code and shift (task_sheets.py): the owner's editor,
    # the day's bird's-eye view and the consistency report.
    ("/api/task-sheets",            "labor"),
    ("/mobile/api/task-sheets",     "labor"),
    # Food Cost
    ("/api/food-cost",              "inventory"),
    ("/api/inv-insight",            "inventory"),
    # The weekly waste series the Food Cost tab charts. Unmapped, a manager
    # without FOOD_COST_VIEW read it (SEC-24).
    ("/api/inv-trend",              "inventory"),
    ("/mobile/api/food-cost",       "inventory"),
    # Marketing
    ("/api/marketing/",             "marketing"),
    ("/api/mkt-",                   "marketing"),
    ("/api/generate-content",       "marketing"),
    ("/api/content-calendar",       "marketing"),
    ("/api/recent-topics",          "marketing"),
    ("/api/post-to-google",         "marketing"),
    # The web publish routes live in social_routes and were missing here, so
    # a restaurant without Marketing could still post to Meta (MOD-MKT-17).
    ("/api/post-to-facebook",       "marketing"),
    ("/api/post-to-instagram",      "marketing"),
    ("/api/gbp-listing",            "marketing"),
    ("/api/guest-",                 "marketing"),
    ("/mobile/api/marketing",       "marketing"),
    ("/mobile/api/guest-",          "marketing"),
    # Intel (derived: full tier + a connected listing)
    ("/api/intel/",                 "intel"),
    ("/api/ai-visibility",          "intel"),
    ("/mobile/api/intel",           "intel"),
)


# Every /api and /mobile/api route is either under a module prefix above or
# on this list, which says why it is deliberately NOT gated by a module
# (SEC-24; tests/test_edge_sec_permissions.py enforces it). A new route that
# is on neither fails that test, so "which module owns this?" gets asked
# when the route is written rather than discovered in an audit. Being on
# this list is not "open to everyone": the account-holder switches check
# permissions.is_principal / principal_only inside, team and loss routes
# their own permissions, and the admin paths admin_required.
_UNGATED_PREFIXES = (
    # Signing in and out, the session, and the account's own security. Not a
    # module; owner-only pieces check is_principal in the handler.
    "/mobile/api/login", "/mobile/api/verify-2fa", "/mobile/api/resend-2fa", "/mobile/api/apple-signin", "/mobile/api/register",
    "/mobile/api/forgot-password", "/mobile/api/reset-password", "/mobile/api/logout", "/mobile/api/me",
    "/mobile/api/device-tokens", "/api/sessions", "/mobile/api/sessions",
    "/api/change-password", "/api/update-email", "/api/send-2fa-test", "/api/verify-2fa-setup",
    "/api/toggle-2fa", "/api/toggle-login-notify", "/api/toggle-staff-signin-notify",
    "/api/switch-location", "/mobile/api/switch-location", "/api/group-locations", "/mobile/api/group-locations",
    # Account, settings, billing and team administration: restaurant-wide.
    # ("/api/account" also covers /api/account-settings/...)
    "/api/account", "/mobile/api/account", "/api/alert-settings", "/api/update-digest-day",
    "/api/send-test-digest", "/api/billing-info", "/api/theme", "/api/dismiss-onboarding",
    "/api/dismiss-welcome", "/api/email-history", "/api/send-referral", "/api/changelog",
    "/mobile/api/changelog", "/api/log-activity", "/api/activity", "/mobile/api/activity",
    "/api/team/", "/mobile/api/team/",
    # Integrations. Credential writes are principal_only in the handler.
    "/api/webhook", "/api/toast/", "/api/square/", "/api/clover/", "/api/rpower/",
    "/mobile/api/connections/", "/api/instagram-",
    # Cross-module surfaces: they read several modules and belong to none
    # (Home, Ask Cavnar, the action/issue/goal/outcome loop, notifications,
    # the morning brief), so a single module gate would be wrong for them.
    "/api/home", "/mobile/api/home", "/api/ask-cavnar", "/mobile/api/ask-cavnar",
    # The Restaurant Data Health Score reads every module's sources; its
    # Sync now only re-pulls the POS the restaurant already connected.
    "/api/data-health", "/mobile/api/data-health",
    # The Benchmark Engine spans modules; its payload is projected by the
    # login's module view permissions inside (intelligence.engine.payload_for).
    "/api/benchmarks", "/mobile/api/benchmarks",
    # Restaurant DNA spans modules too: the restaurant's own profile, each
    # dimension dropped inside when its module's view permission is not
    # held (intelligence.dna.payload_for).
    "/api/dna", "/mobile/api/dna",
    "/api/notifications", "/mobile/api/notifications", "/api/actions", "/mobile/api/actions",
    # The Command Center (command_center.py) spans modules; its registry and
    # search are projected by the login's module view permissions inside,
    # and a proposal is refused by Ask's own tool_allowed.
    "/api/command/", "/mobile/api/command/",
    "/api/issues", "/mobile/api/issues", "/api/goals", "/mobile/api/goals",
    "/api/outcomes", "/mobile/api/outcomes", "/api/decisions", "/mobile/api/decisions",
    "/api/metrics", "/mobile/api/metrics", "/api/value", "/mobile/api/value",
    "/api/cross-module", "/mobile/api/cross-module", "/api/good-news", "/mobile/api/good-news",
    "/api/milestones", "/mobile/api/milestones", "/api/monthly-review", "/mobile/api/monthly-review",
    "/api/morning-brief", "/mobile/api/morning-brief", "/api/closeout", "/mobile/api/closeout",
    "/api/tasks", "/mobile/api/tasks",
    # The nightly DSR: one report across every module, each block (and each
    # loss or owner-only line) withheld per login by dsr.access.
    "/api/dsr", "/mobile/api/dsr",
    # An owner's answer to any recommendation, from any surface (rec_ledger).
    "/api/recs/", "/mobile/api/recs/",
    # Comps and voids: LOSS_VIEW, checked in the handler.
    "/api/loss-signals", "/mobile/api/loss-signals",
    # Polled from every screen (the new-reviews count) or reached from the
    # review card on Home; the Reviews tab itself is gated above.
    "/api/review-count", "/api/mark-posted/", "/api/export-reviews",
    # Competitor intel decides its own availability (full system + listing).
    "/api/competitor-intel", "/api/refresh-competitor-intel",
    # Social posting and Meta's app-review endpoints (social_routes).
    "/api/post-to-facebook", "/api/post-to-instagram", "/api/post-insights", "/api/meta-review-test",
    # Public pages whose token is the credential.
    "/api/public/",
    # One-off admin tools (admin_required), and two login_required debug
    # endpoints (debug-insights, gbp-debug) that are candidates for removal
    # after verification — listed so they stay visible, not endorsed.
    "/api/debug-insights", "/api/gbp-debug", "/api/admin/",
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


def _module_permission_denied(user):
    """The module label this ROLE may not read, or None to allow it.

    The sibling of _module_blocked, asking the other question of the same path
    table: that one asks whether the restaurant BOUGHT the module, this asks
    whether the signed-in role may SEE it. They are genuinely different —
    a shift manager at a restaurant on the full plan is entitled to nothing
    they are not authorised for — and they fail in opposite directions.
    _module_blocked fails OPEN because it protects revenue; this fails CLOSED
    because it protects data, exactly like _console_denied.
    """
    try:
        if not user or user.get("is_admin"):
            return None
        key = _required_module(request.path or "")
        if not key:
            return None
        from permissions import MODULE_VIEW_PERMISSIONS, has_permission, enter_path_allowed
        needed = MODULE_VIEW_PERMISSIONS.get(key)
        if not needed or has_permission(user, needed):
            return None
        # Counting, receiving and logging waste are open to FOOD_COST_ENTER
        # without the margins (U2-27); those routes withhold every dollar.
        if key == "inventory" and enter_path_allowed(user, request.method, request.path or ""):
            return None
        from models import module_label
        return module_label(key)
    except Exception:
        return "This section"


def _module_permission_message(label):
    return f"{label} isn't part of your access. Ask the account owner to change it."


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


def _billing_state(user):
    """(paused, paused_until, hold) for the blocked branches below — so a
    pause the owner chose is never described as a lapse, and a hold only an
    admin lifts (a dispute, a full refund, an admin's pause: models.billing_hold,
    fix round H #114) is never offered the owner's Resume. Fails to
    (False, None, None)."""
    try:
        from models import get_restaurant, billing_hold
        r = get_restaurant(user["restaurant_id"])
        paused = (getattr(r, "billing_status", "") or "").lower() == "paused"
        return paused, (getattr(r, "paused_until", None) if paused else None), billing_hold(r)
    except Exception:
        return False, None, None


def _mdy(iso):
    try:
        y, m, d = str(iso)[:10].split("-")
        return f"{int(m)}/{int(d)}/{y[2:]}"
    except Exception:
        return None


_BILLING_HOLD_MESSAGE = ("Your account is on hold — contact Will at will@cavnar.ai to sort it out. "
                         "Your data is safe in the meantime.")


def _billing_blocked_message(user):
    """The lapse message, the pause message with its resume date, or the
    hold message. The phone shows this string on the Home tab, so it has to
    say which — a held account was told "Resume any time", and Resume
    refuses a hold (fix round H #114). The extras carry `locked` and
    `pause_reason` for a hold, as billing info and pause status do."""
    paused, until, hold = _billing_state(user)
    if hold:
        return _BILLING_HOLD_MESSAGE, {"paused": bool(paused), "paused_until": until if paused else None,
                                       "locked": True, "pause_reason": hold}
    if not paused:
        return _BILLING_BLOCKED_MESSAGE, {}
    when = _mdy(until)
    return ((f"Your subscription is paused until {when}. " if when else "Your subscription is paused. ")
            + "Resume any time from Account → Billing.",
            {"paused": True, "paused_until": until, "locked": False, "pause_reason": "self"})


def _billing_blocked_page(user):
    """A page navigation while the subscription is paused or lapsed.

    This used to return the JSON the fetch() calls get, so an owner who
    paused from Billing & subscription saw a raw error blob on the next
    reload — and the Resume button lives on the page they could no longer
    load. A paused owner gets the date and the button; a lapsed one gets
    the message and Will's address; a manager gets the message and who
    to ask. /api/account/resume is billing-exempt for exactly this page.
    """
    from flask import render_template
    paused = paused_until = hold = None
    can_resume = False
    try:
        from models import get_restaurant, billing_hold
        r = get_restaurant(user["restaurant_id"])
        paused = (getattr(r, "billing_status", "") or "").lower() == "paused"
        paused_until = getattr(r, "paused_until", None)
        # A dispute, refund or admin hold is lifted by an admin only (fix
        # round H #114): the page shows the hold and no Resume button — the
        # template already renders pause_lock; it was never passed.
        hold = billing_hold(r)
        from permissions import has_permission, TEAM_INVITE
        can_resume = bool(paused and not hold and has_permission(user, TEAM_INVITE))
    except Exception:
        pass
    return render_template("billing_paused.html", paused=paused, paused_until=paused_until,
                           pause_lock=hold, can_resume=can_resume,
                           message=(_BILLING_HOLD_MESSAGE if hold else _BILLING_BLOCKED_MESSAGE)), 402


_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _admin_wants_json():
    """_wants_json_response, plus the console's own GETs under /admin/api/
    and the phone's /mobile/api/ — fetch() and the app parse JSON — and any
    request whose Accept header prefers JSON: the console's api() says so on
    every call, so a read outside /admin/api/ (/admin/status/services,
    /admin/status/incidents) gets the 401 that sends the admin to sign in,
    not a 302 to a login page fetch() follows and can't parse. A browser
    navigation prefers text/html and still gets the redirect."""
    path = request.path or ""
    return (_wants_json_response() or path.startswith("/admin/api/") or path.startswith("/mobile/api/")
            or request.accept_mimetypes.best == "application/json")


# ── the internal logins' second factor (SECURITY-1) ──────────────────────────
#
# An admin or support login's two-factor lives on its own users row
# (users.two_fa_enabled / two_fa_method) and every sign-in path asks for it
# (auth_routes.login, the Google callback, mobile login, Sign in with Apple).
# The session records that it passed (sessions.two_factor_at), and this is
# what every gate reads:
#
#   'ok'     — no second factor on this login and the deployment does not
#              require one (ADMIN_REQUIRE_2FA unset: the only admin, 2FA off,
#              still signs in), or this session passed it.
#   'enrol'  — ADMIN_REQUIRE_2FA=1 and this login has none yet: it is sent to
#              /admin/two-factor, which it can reach (it used to be sent to
#              "/", which redirects every admin straight back to /admin).
#   'verify' — the login has a second factor but this session never passed
#              it (a session from before enrolment): it is ended, and signing
#              in again asks for the code.
#   'error'  — the state could not be read: this request is refused (fail
#              closed). Every input is on the session row the lookup already
#              read, so nothing persistent can put a login here; the old gate
#              looked up a restaurant and answered "fine" on any error.

# The enrolment page and its two posts are the only /admin routes a login in
# the 'enrol' state may reach.
_ADMIN_2FA_ENROL_ENDPOINTS = frozenset({"admin.admin_two_factor_page", "admin.admin_two_factor_send",
                                        "admin.admin_two_factor_verify"})
# The old name, kept for readers of it.
_ADMIN_2FA_EXEMPT = _ADMIN_2FA_ENROL_ENDPOINTS


def _flag(value) -> bool:
    return str(value if value is not None else "").strip().lower() not in ("", "0", "none", "null", "false")


def admin_two_factor_required() -> bool:
    """ADMIN_REQUIRE_2FA=1: every internal login must have a second factor.
    Opt-in: shipping it default-on locked the only admin out of /admin on
    the deploy that introduced it, before he had enrolled."""
    return (os.getenv("ADMIN_REQUIRE_2FA", "0") or "").strip() == "1"


def user_two_factor_enrolled(user) -> bool:
    """Whether an internal login has its own second factor turned on."""
    return _flag((user or {}).get("two_fa_enabled"))


def admin_second_factor_state(user) -> str:
    """'ok' | 'enrol' | 'verify' | 'error' for this session (see above).
    Anyone who is not an internal login is 'ok' here — a restaurant's logins
    are gated by their restaurant's switch at sign-in."""
    if not is_internal_login(user):
        return "ok"
    try:
        if user_two_factor_enrolled(user):
            return "ok" if user.get("two_factor_at") else "verify"
        return "enrol" if admin_two_factor_required() else "ok"
    except Exception as exc:
        try:
            import ops
            ops.capture(exc, job="admin_second_factor_gate", context=f"user_id={(user or {}).get('id')}")
        except Exception:
            pass
        return "error"


def _admin_two_factor_missing(user):
    """True when the internal-login gate refuses this session. No production
    code calls it since the status page moved to auth.admin_required (INT-1);
    two tests still stub it. Candidate for future cleanup after additional
    verification."""
    return admin_second_factor_state(user) != "ok"


def _second_factor_refusal(state):
    """The response for a session the gate refuses."""
    from flask import jsonify as _jsonify_sf
    from urllib.parse import quote as _quote_sf
    wants_json = _admin_wants_json()
    if state == "enrol":
        msg = "Turn on two-factor authentication for your login to use the admin console."
        if wants_json:
            return _jsonify_sf(ok=False, error=msg, two_factor_required=True, enrol_url="/admin/two-factor"), 403
        nxt = request.path if (request.path or "").startswith("/admin") else "/admin"
        return redirect("/admin/two-factor?next=" + _quote_sf(nxt, safe="/"))
    if state == "verify":
        # This session predates the second factor its login now has. It is
        # ended here, so the next sign-in is the one that asks for the code.
        tok = current_session_token()
        if tok:
            try:
                delete_session(tok)
            except Exception:
                pass
        msg = "Sign in again — your login now asks for a two-factor code."
        if wants_json:
            return _jsonify_sf(ok=False, error=msg, session_expired=True, two_factor_required=True), 401
        return redirect(url_for("auth.login", next="/admin"))
    msg = "We couldn't confirm your sign-in just now. Try again in a moment."
    if wants_json:
        return _jsonify_sf(ok=False, error=msg), 503
    return msg, 503


# ── view-as: every write attributed to the admin behind it (SECURITY-3) ──────

def _view_as_context(user):
    """For a view-as session, who is really acting — also put on flask.g
    (g.view_as) so anything recording a change can attribute it. None for
    every other session."""
    if not user or (user.get("device_type") or "") != "admin-view-as":
        return None
    ctx = {"acting_admin_id": user.get("acting_admin_id"), "acting_admin": user.get("acting_admin"),
           "acting_admin_role": user.get("acting_admin_role"),
           "as_user_id": user.get("id"), "as_username": user.get("username"),
           "restaurant_id": user.get("restaurant_id"), "read_only": bool(user.get("view_as_read_only"))}
    try:
        from flask import g as _g_va
        _g_va.view_as = ctx
    except Exception:
        pass
    return ctx


def _status_of(rv) -> int:
    try:
        if isinstance(rv, tuple):
            for part in rv[1:]:
                if isinstance(part, int):
                    return part
            return int(getattr(rv[0], "status_code", 200) or 200)
        return int(getattr(rv, "status_code", 200) or 200)
    except Exception:
        return 200


def record_view_as_write(ctx, status):
    """One admin_events row per write made through a view-as session: the
    admin behind it (name AND id, in the typed actor columns), the login it
    was made as, what was sent and how it ended — through the one audit
    call, admin_events.record_admin_action, so the fleet audit filters by
    the admin find it. Never raises — the record must not break the write."""
    if not ctx:
        return
    try:
        import admin_events
        who = ctx.get("acting_admin") or f"admin #{ctx.get('acting_admin_id')}"
        code = int(status)
        admin_events.record_admin_action(
            {"id": ctx.get("acting_admin_id"), "username": who}, "view_as_write",
            restaurant_id=ctx.get("restaurant_id"), target=f"user:{ctx.get('as_user_id')}",
            after={"actor_role": ctx.get("acting_admin_role"), "as_user_id": ctx.get("as_user_id"),
                   "as_username": ctx.get("as_username"), "method": request.method, "path": request.path,
                   "endpoint": request.endpoint, "status": code},
            result="ok" if code < 400 else ("denied" if code in (401, 403) else "error"),
            summary=f"{who} (viewing as {ctx.get('as_username')}) {request.method} {request.path} → {status}")
    except Exception:
        pass


# Reads through a view-as session that are recorded like its writes (memory
# re-audit 9/29/26, PEOPLE-20): the owner's memory in Account and Ask's
# history and chats. Support reading them is support's act, kept in the
# fleet audit as "view_as_read" — web and mobile paths alike.
_VIEW_AS_LOGGED_READS = re.compile(
    r"/(account/memory|ask-cavnar/history|ask-cavnar/conversations(/\d+)?)/?$")


def record_view_as_read(ctx):
    """One admin_events row ("view_as_read") when a view-as session reads
    one of _VIEW_AS_LOGGED_READS: the admin behind it, the login it was
    viewing as and the path. Never raises."""
    if not ctx:
        return
    try:
        if request.method not in ("GET", "HEAD") or not _VIEW_AS_LOGGED_READS.search(request.path or ""):
            return
        import admin_events
        who = ctx.get("acting_admin") or f"admin #{ctx.get('acting_admin_id')}"
        admin_events.record_admin_action(
            {"id": ctx.get("acting_admin_id"), "username": who}, "view_as_read",
            restaurant_id=ctx.get("restaurant_id"), target=f"user:{ctx.get('as_user_id')}",
            after={"actor_role": ctx.get("acting_admin_role"), "as_user_id": ctx.get("as_user_id"),
                   "as_username": ctx.get("as_username"), "path": request.path, "endpoint": request.endpoint},
            result="ok",
            summary=f"{who} (viewing as {ctx.get('as_username')}) read {request.path}")
    except Exception:
        pass


# Posts through a view-as session that change nothing and are not recorded
# as the admin's writes: the web tab ping, which admin_routes skips for a
# view-as session (it fires on every tab switch and would bury real writes).
_VIEW_AS_UNRECORDED = frozenset({"admin.log_activity_route"})


def _run_view_as_write(f, args, kwargs, user, ctx):
    if request.endpoint in _VIEW_AS_UNRECORDED:
        return f(*args, **kwargs, current_user=user)
    status = 500
    try:
        rv = f(*args, **kwargs, current_user=user)
        status = _status_of(rv)
        return rv
    finally:
        record_view_as_write(ctx, status)


def _bind_log_context(user, with_restaurant=True):
    """Put who this request is on every log line it writes (F's request 9:
    logging_setup; http_layer clears it at teardown), so a traceback names
    the login — and, for an owner's request, the restaurant, which is also
    what a 5xx sample is attributed to. An admin request binds the login
    only: its restaurant is the one the URL names, not the admin's home.
    Never raises.

    Also keeps the resolved login on flask.g (`cavnar_current_user`) for
    the request's lifetime, so a write deep in a helper can say who made it
    without re-reading the session (change_log.actor_context — memory audit
    9/29/26, "change_log")."""
    try:
        from flask import g as _g_bind
        _g_bind.cavnar_current_user = user
    except Exception:
        pass
    try:
        import logging_setup
        fields = {"user_id": (user or {}).get("id")}
        if with_restaurant:
            fields["restaurant_id"] = (user or {}).get("restaurant_id")
        logging_setup.bind(**fields)
    except Exception:
        pass


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            if _wants_json_response():
                from flask import jsonify as _jsonify_lr
                return _jsonify_lr(ok=False, error="Your session expired — please log in again.", session_expired=True), 401
            return redirect(url_for("auth.login", next=request.path))
        # An internal login reaches client routes too (an admin import names
        # a restaurant_id); the same second-factor gate as /admin, or
        # ADMIN_REQUIRE_2FA was one import route away from meaningless.
        if is_internal_login(user):
            state = admin_second_factor_state(user)
            if state != "ok":
                return _second_factor_refusal(state)
        _bind_log_context(user)
        if _console_denied(user):
            if _wants_json_response():
                from flask import jsonify as _jsonify_cd
                return _jsonify_cd(ok=False, error=_STAFF_WRONG_DOOR, staff_account=True), 403
            return redirect(url_for("staff.portal_home"))
        if _billing_blocked(user):
            if not _wants_json_response():
                return _billing_blocked_page(user)
            from flask import jsonify as _jsonify_bb
            _msg, _extra = _billing_blocked_message(user)
            return _jsonify_bb(ok=False, error=_msg, billing_inactive=True, **_extra), 402
        locked = _module_blocked(user)
        if locked:
            from flask import jsonify as _jsonify_ml
            return _jsonify_ml(ok=False, error=_module_blocked_message(locked),
                               module_locked=True, module=locked), 403
        unauthorised = _module_permission_denied(user)
        if unauthorised:
            from flask import jsonify as _jsonify_mp
            return _jsonify_mp(ok=False, error=_module_permission_message(unauthorised),
                               module_forbidden=True, module=unauthorised), 403
        view_as = _view_as_context(user)
        if view_as_write_denied(user):
            record_view_as_write(view_as, 403)
            from flask import jsonify as _jsonify_vr
            return _jsonify_vr(ok=False, error=_VIEW_AS_READ_ONLY_MSG, read_only=True), 403
        moved = _tab_location_moved(user)
        if moved:
            return moved
        if view_as and request.method not in _SAFE_METHODS:
            return _run_view_as_write(f, args, kwargs, user, view_as)
        if view_as:
            record_view_as_read(view_as)
        return f(*args, **kwargs, current_user=user)
    return decorated


# The one route a tab rendered for an old location must still reach.
_TAB_LOCATION_EXEMPT = frozenset({"client.switch_location"})


def _tab_location_moved(user):
    """A 409 when the page that sent this request was rendered for a
    different location than the session now has, else None.

    The active location lives on the session, not the tab, so after an owner
    switched to Wicker Park in tab B, tab A — still showing Lakeview's
    settings — saved Lakeview's form into Wicker Park, and a Lakeview job
    polled from tab A answered "Job not found" (DATA-10). The dashboard names
    the location it was rendered for on every request (_csrf_fetch.html);
    a request that names none (the phone, a script) is unaffected."""
    raw = (request.headers.get("X-Cavnar-Restaurant-Id") or "").strip()
    if not raw.isdigit() or request.endpoint in _TAB_LOCATION_EXEMPT:
        return None
    if int(raw) == int(user.get("restaurant_id") or 0):
        return None
    from flask import jsonify as _jsonify_tl
    return _jsonify_tl(ok=False, location_changed=True, restaurant_id=user.get("restaurant_id"),
                       error="You switched to another location in a different tab. "
                             "Reload this page to keep working here."), 409

# Support accounts (role='support', is_admin=0) may READ the admin console
# and open a view-as session; every other admin write needs the admin bit.
# Their own two-factor enrolment is theirs to write, too. (/admin/stop-viewing
# runs on the view-as session itself, never through admin_required.)
# Beyond view-as, only routes that act on the support login's OWN second
# factor — enrolment, switching method (with its password step-up), new
# backup codes, turning it off where it is not required. Each touches only
# current_user's row, so no one has to reset a support login's two-factor for
# it (Will, 9/29/26). The step-up grants nothing else: every other write is
# still refused here.
_SUPPORT_WRITE_OK = frozenset({"admin.view_as_client", "admin.admin_two_factor_send",
                               "admin.admin_two_factor_verify", "admin.admin_api_reauth",
                               "admin.admin_two_factor_backup_codes", "admin.admin_two_factor_disable"})


def current_admin_role():
    """'admin' | 'support' | None for this request — so the console's data
    layer (admin_ops) can redact what a read-only support login sees without
    being handed the user. admin_required sets flask.g.admin_role; outside
    it this resolves the session once."""
    try:
        from flask import g as _g_ar, has_request_context
        if not has_request_context():
            return None
        role = getattr(_g_ar, "admin_role", None)
        if role:
            return role
        user = get_current_user()
        if user and user.get("is_admin"):
            return "admin"
        if is_internal_login(user):
            return "support"
    except Exception:
        pass
    return None


def _admin_rate_limited(user):
    """The per-session ceiling on /admin (SECURITY-4, #88): a 429 response,
    or None. A limiter that cannot count lets the request through — it is an
    abuse brake, and it must never be what locks the operator out."""
    try:
        import security
        tok = current_session_token()
        key = hash_session_token(tok)[:24] if tok else f"user:{(user or {}).get('id')}"
        allowed, retry_after = security.admin_request_allowed(key, request.method not in _SAFE_METHODS)
    except Exception:
        return None
    if allowed:
        return None
    from flask import jsonify as _jsonify_rl
    msg = "Too many requests from this session. Wait a minute and try again."
    if _admin_wants_json():
        resp = _jsonify_rl(ok=False, error=msg, rate_limited=True, retry_after=retry_after)
    else:
        from flask import make_response as _mr_rl
        resp = _mr_rl(msg)
    resp.headers["Retry-After"] = str(retry_after)
    return resp, 429


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        is_support = bool(user) and not user.get("is_admin") and (user.get("role") == "support")
        if not user or not (user["is_admin"] or is_support):
            # The console's own GETs under /admin/api/ are fetch() calls too:
            # an expired one got a 302 to the login PAGE, which fetch follows
            # and then fails to parse (docs pass) — it answers 401 like a POST.
            if _admin_wants_json():
                from flask import jsonify as _jsonify_ar
                return _jsonify_ar(ok=False, error="Your session expired — please log in again.", session_expired=True), 401
            return redirect(url_for("auth.login"))
        limited = _admin_rate_limited(user)
        if limited:
            return limited
        if is_support and request.method not in _SAFE_METHODS \
                and request.endpoint not in _SUPPORT_WRITE_OK:
            from flask import jsonify as _jsonify_sr
            return _jsonify_sr(ok=False, error="Support accounts are read-only."), 403
        state = admin_second_factor_state(user)
        if state != "ok" and not (state == "enrol" and request.endpoint in _ADMIN_2FA_ENROL_ENDPOINTS):
            return _second_factor_refusal(state)
        try:
            from flask import g as _g_adm
            _g_adm.admin_role = "admin" if user.get("is_admin") else "support"
        except Exception:
            pass
        _bind_log_context(user, with_restaurant=False)
        return f(*args, **kwargs, current_user=user)
    return decorated


# ── step-up: the password again for a sensitive admin action ──────────────────

def reauth_is_recent(user, minutes: int = RECENT_AUTH_MINUTES) -> bool:
    """Whether this session's password was typed within `minutes` — at
    sign-in, or again through POST /admin/api/reauth."""
    at = _parse_utc((user or {}).get("reauth_at"))
    if at is None:
        return False
    from datetime import timedelta
    age = datetime.utcnow() - at
    return timedelta(minutes=-1) <= age <= timedelta(minutes=minutes)


def reauth_refusal(user=None, minutes: int = RECENT_AUTH_MINUTES):
    """The step-up's answer — 403 {reauth_required: true} — when this
    session's password was not typed in the last `minutes`, else None.

    recent_auth_required is this as a decorator. A route that needs the
    step-up for only some of what it does calls it directly: the legacy
    settings save when a payload changes the billing status, a module or the
    owner email; the review-account seed when it rotates the password; an
    offboarding step that acts on Stripe, DocuSign or the stored
    credentials. One answer, so the console's reauth prompt handles all of
    them the same way."""
    if user is None:
        user = get_current_user()
    if reauth_is_recent(user, minutes):
        return None
    from flask import jsonify as _jsonify_ra
    return _jsonify_ra(ok=False, reauth_required=True, reauth_url="/admin/api/reauth",
                       window_minutes=minutes,
                       error="Enter your password again to do this."), 403


def recent_auth_required(minutes: int = RECENT_AUTH_MINUTES):
    """Step-up for a sensitive admin action (owner decision, 9/29/26):
    refused with 403 {reauth_required: true} unless this session's password
    was typed in the last `minutes`. The console answers by asking for the
    password, POSTing /admin/api/reauth, and sending the action again.

    Goes UNDER admin_required, which resolves the session:

        @admin_bp.route(...)
        @admin_required
        @recent_auth_required()
        def route(..., current_user): ..."""
    if callable(minutes):           # used bare: @recent_auth_required
        return recent_auth_required()(minutes)

    def deco(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            refused = reauth_refusal(kwargs.get("current_user"), minutes)
            if refused:
                return refused
            return f(*args, **kwargs)
        return wrapped
    return deco


def mark_reauthenticated(token: str, db_path: str = DB_PATH) -> bool:
    """Stamp this session: its password was just typed again."""
    if not token:
        return False
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE sessions SET reauth_at=datetime('now') WHERE token=?",
                           (hash_session_token(token),))
        conn.commit()
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()


def mark_second_factor(token: str, db_path: str = DB_PATH) -> bool:
    """Stamp this session: it has just passed a second factor (the admin
    enrolment's confirming code)."""
    if not token:
        return False
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE sessions SET two_factor_at=datetime('now') WHERE token=?",
                           (hash_session_token(token),))
        conn.commit()
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()


def password_matches(user_id: int, password: str, db_path: str = DB_PATH) -> bool:
    """Whether `password` is this active login's password. Unlike
    verify_password it neither looks the login up by name nor stamps
    last_login — it is the step-up's check, not a sign-in."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT password_hash FROM users WHERE id=? AND is_active=1", (user_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        check_password_hash(_dummy_password_hash(), password or "")
        return False
    return check_password_hash(row["password_hash"], password or "")


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
        # The web decorator's internal-login gate, on the phone's door too.
        if is_internal_login(user):
            state = admin_second_factor_state(user)
            if state != "ok":
                return _second_factor_refusal(state)
        _bind_log_context(user)
        # Same console gate as the web decorator — the iOS app ships both the
        # owner dashboard and the staff portal against this one blueprint, so
        # a PIN session must be refused here too or the whole owner API is
        # reachable from the staff build.
        if _console_denied(user):
            from flask import jsonify as _jsonify_mcd
            return _jsonify_mcd(ok=False, error=_STAFF_WRONG_DOOR, staff_account=True), 403
        if _billing_blocked(user):
            from flask import jsonify as _jsonify_mbb
            _msg, _extra = _billing_blocked_message(user)
            return _jsonify_mbb(ok=False, error=_msg, billing_inactive=True, **_extra), 402
        locked = _module_blocked(user)
        if locked:
            from flask import jsonify as _jsonify_mml
            return _jsonify_mml(ok=False, error=_module_blocked_message(locked),
                                module_locked=True, module=locked), 403
        unauthorised = _module_permission_denied(user)
        if unauthorised:
            from flask import jsonify as _jsonify_mmp
            return _jsonify_mmp(ok=False, error=_module_permission_message(unauthorised),
                                module_forbidden=True, module=unauthorised), 403
        view_as = _view_as_context(user)
        if view_as_write_denied(user):
            record_view_as_write(view_as, 403)
            from flask import jsonify as _jsonify_mvr
            return _jsonify_mvr(ok=False, error=_VIEW_AS_READ_ONLY_MSG, read_only=True), 403
        if view_as and request.method not in _SAFE_METHODS:
            return _run_view_as_write(f, args, kwargs, user, view_as)
        if view_as:
            record_view_as_read(view_as)
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


def trusted_device_ok(restaurant_id: int, token: str, user_id: int | None,
                      db_path: str = DB_PATH, *, principal: bool = False) -> bool:
    """True when `token` is a live device that `user_id` remembered at this
    restaurant. A remembered device belongs to the login that ticked
    "remember" (create_trusted_device stores it): a manager's remembered
    phone must not skip the owner's second factor when the owner's password
    is typed on it. No user_id is refused.

    A token with no login on it — a row from before user_id was stored, or
    the legacy single-slot restaurants.two_fa_device_token — can't say whose
    it is, so only an account holder (`principal`, permissions.is_principal)
    may still use it; anyone else is asked for a code once."""
    if not token or user_id is None:
        return False
    conn = get_conn(db_path)
    try:
        row = conn.execute("""
            SELECT id FROM trusted_devices
            WHERE restaurant_id=? AND token_hash=? AND expires_at > datetime('now')
              AND (user_id=? OR (user_id IS NULL AND ?=1))
        """, (restaurant_id, _hash_device_token(token), user_id, 1 if principal else 0)).fetchone()
        if row:
            conn.execute("UPDATE trusted_devices SET last_used_at=datetime('now') WHERE id=?", (row["id"],))
            conn.commit()
            return True
        if not principal:
            return False
        legacy = conn.execute("SELECT two_fa_device_token FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        return bool(legacy and legacy["two_fa_device_token"] and legacy["two_fa_device_token"] == token)
    finally:
        conn.close()


def remembered_device_ok(user: dict, token: str, db_path: str = DB_PATH) -> bool:
    """trusted_device_ok for the login being authenticated (every sign-in
    path: password on web and mobile, Google SSO, Sign in with Apple)."""
    if not user or not token:
        return False
    try:
        from permissions import is_principal
        # An internal login counts only devices it remembered itself: a
        # token naming no login, or the restaurant's legacy single slot, is
        # the restaurant's — and the admin may be homed on a client's row.
        principal = is_principal(user) and not is_internal_login(user)
    except Exception:
        principal = False
    return trusted_device_ok(user.get("restaurant_id"), token, user.get("id"),
                             db_path=db_path, principal=principal)


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
    import models as _models_inv
    _models_inv._invalidate_request_cache(restaurant_id)


def create_login_report(user_id: int, session_token: str | None, db_path: str = DB_PATH) -> str:
    import secrets
    token = secrets.token_urlsafe(32)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO login_reports (token, user_id, session_token) VALUES (?,?,?)",
                 (hash_session_token(token), user_id, hash_session_token(session_token) if session_token else None))
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
        WHERE token IN (?, ?) AND used_at IS NULL AND created_at > datetime('now', '-7 days')
    """, (hash_session_token(token), token)).fetchone()
    if not row:
        conn.close()
        return None
    user_id = row["user_id"]
    conn.execute("UPDATE login_reports SET used_at=datetime('now') WHERE token IN (?, ?)",
                 (hash_session_token(token), token))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.execute("UPDATE users SET must_reset_password=1 WHERE id=?", (user_id,))
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if user and user["restaurant_id"]:
        conn.execute("DELETE FROM trusted_devices WHERE restaurant_id=?", (user["restaurant_id"],))
        conn.execute("UPDATE restaurants SET two_fa_device_token=NULL WHERE id=?", (user["restaurant_id"],))
        import models as _models_inv
        _models_inv._invalidate_request_cache(user["restaurant_id"])
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
    code = str(__import__("secrets").randbelow(900000) + 100000)
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
