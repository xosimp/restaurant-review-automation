from flask import redirect, url_for, request
from functools import wraps
"""
auth.py — User authentication for the Cavnar AI hosted dashboard
Handles: user table, password hashing, session management, login/logout
"""
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
"""

def init_auth(db_path: str = DB_PATH):
    # Migrations
    for col_sql in [
        "ALTER TABLE sessions ADD COLUMN last_active TEXT NOT NULL DEFAULT (datetime('now'))",
        "ALTER TABLE sessions ADD COLUMN ip_address TEXT",
        "ALTER TABLE sessions ADD COLUMN user_agent TEXT",
    ]:
        try:
            import sqlite3 as _sql
            conn_m = _sql.connect(db_path)
            conn_m.execute(col_sql)
            conn_m.commit()
            conn_m.close()
        except Exception:
            pass  # Column already exists
    conn = sqlite3.connect(db_path)
    conn.executescript(AUTH_SCHEMA)
    conn.commit()
    conn.close()

# ── User CRUD ─────────────────────────────────────────────────────────────────

def create_user(restaurant_id: int, username: str, email: str,
                password: str, is_admin: bool = False,
                db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    cur = conn.execute("""
        INSERT INTO users (restaurant_id, username, email, password_hash, is_admin)
        VALUES (?, ?, ?, ?, ?)
    """, (restaurant_id, username.lower().strip(), email.lower().strip(),
          generate_password_hash(password), int(is_admin)))
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

def update_password(user_id: int, new_password: str, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                 (generate_password_hash(new_password), user_id))
    conn.commit()
    conn.close()

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

def create_session(user_id: int, days: int = 30,
                   ip_address: str = None, user_agent: str = None,
                   db_path: str = DB_PATH) -> str:
    token = secrets.token_urlsafe(32)
    from datetime import timedelta
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    conn = get_conn(db_path)
    # Prune expired sessions for this user (keep active ones for multi-device support)
    conn.execute("DELETE FROM sessions WHERE user_id=? AND expires_at <= datetime('now')", (user_id,))
    conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at, ip_address, user_agent) VALUES (?,?,?,?,?)",
        (token, user_id, expires, ip_address or "", user_agent or "")
    )
    conn.commit()
    conn.close()
    return token


def get_sessions_for_user(user_id: int, current_token: str = None,
                          db_path: str = DB_PATH) -> list:
    """Return all active sessions for a user, marking which is current."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT token, created_at, last_active, ip_address, user_agent
        FROM sessions
        WHERE user_id=? AND expires_at > datetime('now')
        ORDER BY last_active DESC
    """, (user_id,)).fetchall()
    conn.close()
    result = []
    for row in rows:
        result.append({
            "token_hint": row["token"][-6:],
            "is_current": row["token"] == current_token,
            "created_at": row["created_at"],
            "last_active": row["last_active"],
            "ip_address": row["ip_address"] or "",
            "user_agent": row["user_agent"] or "",
        })
    return result


def revoke_other_sessions(user_id: int, current_token: str,
                          db_path: str = DB_PATH):
    """Delete all sessions for a user except the current one."""
    conn = get_conn(db_path)
    conn.execute("DELETE FROM sessions WHERE user_id=? AND token!=?",
                 (user_id, current_token))
    conn.commit()
    conn.close()

INACTIVITY_HOURS = 8  # Log out after 8 hours of inactivity

def get_session_user(token: str, db_path: str = DB_PATH) -> Optional[dict]:
    if not token:
        return None
    conn = get_conn(db_path)
    row = conn.execute("""
        SELECT u.*, s.last_active FROM sessions s
        JOIN users u ON s.user_id = u.id
        WHERE s.token=? AND s.expires_at > datetime('now') AND u.is_active=1
    """, (token,)).fetchone()
    if not row:
        conn.close()
        return None
    # Check inactivity timeout
    last_active = row["last_active"] or ""
    if last_active:
        try:
            from datetime import datetime, timedelta, timezone
            la = datetime.fromisoformat(last_active.replace("Z",""))
            # Compare both in UTC to avoid timezone mismatch
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            if now_utc - la > timedelta(hours=INACTIVITY_HOURS):
                # Session expired due to inactivity — delete it
                conn.execute("DELETE FROM sessions WHERE token=?", (token,))
                conn.commit()
                conn.close()
                return None
        except Exception:
            pass
    # Update last_active timestamp
    conn.execute("UPDATE sessions SET last_active=datetime('now') WHERE token=?", (token,))
    conn.commit()
    conn.close()
    return dict(row)

def delete_session(token: str, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
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

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return redirect(url_for("auth.login", next=request.path))
        return f(*args, **kwargs, current_user=user)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user or not user["is_admin"]:
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs, current_user=user)
    return decorated
