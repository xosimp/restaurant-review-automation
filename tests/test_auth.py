"""auth.py — password verification, session lifecycle, and the
login_required/admin_required gates every protected route depends on.
Zero coverage before this despite being the actual security kernel: a bug
here means account takeover or a session that never expires."""
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask
from werkzeug.security import generate_password_hash

import auth
from auth import (
    create_user, verify_password, update_password,
    create_session, get_session_user, get_sessions_for_user,
    revoke_other_sessions, delete_session, switch_active_restaurant,
    set_user_role, login_required, admin_required, INACTIVITY_HOURS,
    init_auth,
)
from models import create_restaurant, get_conn, Restaurant


@pytest.fixture(autouse=True)
def _init_auth_tables(db_path):
    """conftest.py's db_path fixture only runs models.init_db()/ensure_columns() —
    users/sessions live in auth.py's own AUTH_SCHEMA, applied by init_auth()."""
    init_auth(db_path=db_path)


def _restaurant(db_path, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Auth Test Co"), owner_email="a@x.com", **kw), db_path=db_path)


# ── password verification ────────────────────────────────────────────────────

def test_verify_password_succeeds_with_correct_password(db_path):
    rid = _restaurant(db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    user = verify_password("alice", "correct-horse", db_path=db_path)
    assert user is not None
    assert user["username"] == "alice"


def test_verify_password_fails_with_wrong_password(db_path):
    rid = _restaurant(db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    assert verify_password("alice", "wrong-password", db_path=db_path) is None


def test_verify_password_fails_for_unknown_username(db_path):
    assert verify_password("nobody", "whatever", db_path=db_path) is None


def test_password_is_hashed_not_stored_plaintext(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    conn = get_conn(db_path)
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    assert row["password_hash"] != "correct-horse"


def test_verify_password_username_is_case_insensitive(db_path):
    rid = _restaurant(db_path)
    create_user(rid, "Alice", "alice@x.com", "correct-horse", db_path=db_path)
    assert verify_password("ALICE", "correct-horse", db_path=db_path) is not None


# ── legacy mixed-case usernames (bypassing create_user's own lowercasing) ────
# get_user_by_username always lowercases its input before querying, but the
# username column has no COLLATE NOCASE — so a row written with a mixed-case
# username via raw SQL (not create_user()) could never log in again,
# regardless of what was typed. Found live: hosted_dashboard.py's
# _ensure_gia_mia_vibe() did exactly this on every deploy, via a raw
# "UPDATE users SET ... username=?" with "Brian" (now fixed to "brian").

def _insert_raw_user(db_path, rid, username, email="legacy@x.com", password="pw"):
    """Simulates a username written outside create_user() — e.g. a raw SQL
    UPDATE/INSERT in a seed or 'ensure profile' script."""
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO users (restaurant_id, username, email, password_hash) VALUES (?,?,?,?)",
        (rid, username, email, generate_password_hash(password))
    )
    conn.commit()
    conn.close()


def test_legacy_mixed_case_username_cannot_login_before_normalization(db_path):
    rid = _restaurant(db_path)
    _insert_raw_user(db_path, rid, "Brian")
    assert verify_password("brian", "pw", db_path=db_path) is None
    assert verify_password("Brian", "pw", db_path=db_path) is None


def test_init_auth_normalizes_legacy_mixed_case_usernames(db_path):
    """Running init_auth again — as every app boot does — must repair any
    mixed-case username written outside create_user(), so a legacy row
    starts logging in again on the very next deploy with no manual fix."""
    rid = _restaurant(db_path)
    _insert_raw_user(db_path, rid, "Brian")

    init_auth(db_path=db_path)  # simulates the next app boot

    assert verify_password("brian", "pw", db_path=db_path) is not None
    assert verify_password("Brian", "pw", db_path=db_path) is not None


def test_username_normalization_does_not_disturb_already_lowercase_usernames(db_path):
    rid = _restaurant(db_path)
    create_user(rid, "alice", "alice@x.com", "pw", db_path=db_path)
    init_auth(db_path=db_path)  # should be a no-op for this row
    assert verify_password("alice", "pw", db_path=db_path) is not None


def test_update_password_changes_what_verifies(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.com", "old-password", db_path=db_path)
    update_password(uid, "new-password", db_path=db_path)
    assert verify_password("alice", "old-password", db_path=db_path) is None
    assert verify_password("alice", "new-password", db_path=db_path) is not None


def test_inactive_user_cannot_verify(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE users SET is_active=0 WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    assert verify_password("alice", "correct-horse", db_path=db_path) is None


# ── session lifecycle ────────────────────────────────────────────────────────

def test_valid_session_resolves_to_user(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.com", "pw", db_path=db_path)
    token = create_session(uid, db_path=db_path)
    user = get_session_user(token, db_path=db_path)
    assert user is not None
    assert user["id"] == uid


def test_unknown_token_returns_none(db_path):
    assert get_session_user("not-a-real-token", db_path=db_path) is None


def test_empty_token_returns_none(db_path):
    assert get_session_user("", db_path=db_path) is None
    assert get_session_user(None, db_path=db_path) is None


def test_expired_session_returns_none(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.com", "pw", db_path=db_path)
    token = create_session(uid, db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE sessions SET expires_at=datetime('now','-1 day') WHERE token=?", (token,))
    conn.commit()
    conn.close()
    assert get_session_user(token, db_path=db_path) is None


def test_session_expires_after_inactivity_timeout(db_path):
    """The actual auto-logout mechanism — a session with a valid expires_at
    is still killed if last_active is older than INACTIVITY_HOURS."""
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.com", "pw", db_path=db_path)
    token = create_session(uid, db_path=db_path)
    stale = (datetime.utcnow() - timedelta(hours=INACTIVITY_HOURS + 1)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    conn.execute("UPDATE sessions SET last_active=? WHERE token=?", (stale, token))
    conn.commit()
    conn.close()
    assert get_session_user(token, db_path=db_path) is None
    # The inactivity check deletes the row outright, not just rejects it
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    conn.close()
    assert row is None


def test_session_within_inactivity_window_stays_valid(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.com", "pw", db_path=db_path)
    token = create_session(uid, db_path=db_path)
    recent = (datetime.utcnow() - timedelta(hours=INACTIVITY_HOURS - 1)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    conn.execute("UPDATE sessions SET last_active=? WHERE token=?", (recent, token))
    conn.commit()
    conn.close()
    assert get_session_user(token, db_path=db_path) is not None


def test_getting_session_user_refreshes_last_active(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.com", "pw", db_path=db_path)
    token = create_session(uid, db_path=db_path)
    stale = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    conn.execute("UPDATE sessions SET last_active=? WHERE token=?", (stale, token))
    conn.commit()
    conn.close()
    get_session_user(token, db_path=db_path)
    conn = get_conn(db_path)
    row = conn.execute("SELECT last_active FROM sessions WHERE token=?", (token,)).fetchone()
    conn.close()
    refreshed = datetime.fromisoformat(row["last_active"][:19])
    assert (datetime.utcnow() - refreshed) < timedelta(minutes=1)


def test_deleted_session_returns_none(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.com", "pw", db_path=db_path)
    token = create_session(uid, db_path=db_path)
    delete_session(token, db_path=db_path)
    assert get_session_user(token, db_path=db_path) is None


def test_revoke_other_sessions_keeps_current_kills_rest(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.com", "pw", db_path=db_path)
    current = create_session(uid, db_path=db_path)
    other1 = create_session(uid, db_path=db_path)
    other2 = create_session(uid, db_path=db_path)
    revoke_other_sessions(uid, current, db_path=db_path)
    assert get_session_user(current, db_path=db_path) is not None
    assert get_session_user(other1, db_path=db_path) is None
    assert get_session_user(other2, db_path=db_path) is None


def test_get_sessions_for_user_marks_current(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.com", "pw", db_path=db_path)
    current = create_session(uid, db_path=db_path)
    create_session(uid, db_path=db_path)
    sessions = get_sessions_for_user(uid, current_token=current, db_path=db_path)
    assert len(sessions) == 2
    current_flags = [s["is_current"] for s in sessions]
    assert current_flags.count(True) == 1


def test_sessions_are_isolated_per_user(db_path):
    """A session token only ever resolves to the user it was issued for —
    the actual cross-account-takeover check."""
    rid_a = _restaurant(db_path, name="Co A")
    rid_b = _restaurant(db_path, name="Co B")
    uid_a = create_user(rid_a, "alice", "alice@x.com", "pw", db_path=db_path)
    uid_b = create_user(rid_b, "bob", "bob@x.com", "pw", db_path=db_path)
    token_a = create_session(uid_a, db_path=db_path)
    user = get_session_user(token_a, db_path=db_path)
    assert user["id"] == uid_a
    assert user["id"] != uid_b
    assert user["restaurant_id"] == rid_a


# ── owner / multi-location active-restaurant override ───────────────────────

def test_owner_active_restaurant_overrides_base(db_path):
    rid_base = _restaurant(db_path, name="Base Location")
    rid_other = _restaurant(db_path, name="Other Location")
    uid = create_user(rid_base, "owner1", "owner1@x.com", "pw", db_path=db_path)
    set_user_role(uid, "owner", db_path=db_path)
    token = create_session(uid, db_path=db_path)
    switch_active_restaurant(token, rid_other, db_path=db_path)
    user = get_session_user(token, db_path=db_path)
    assert user["restaurant_id"] == rid_other
    assert user["base_restaurant_id"] == rid_base


def test_non_owner_has_no_active_restaurant_override(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.com", "pw", db_path=db_path)
    token = create_session(uid, db_path=db_path)
    user = get_session_user(token, db_path=db_path)
    assert user["restaurant_id"] == rid
    assert user["base_restaurant_id"] == rid


# ── login_required / admin_required decorators ──────────────────────────────
# Full redirect-branch behavior needs url_for("auth.login") registered on a
# real app; the JSON branch (the one that actually matters — every dashboard
# fetch() call hits this) doesn't, since it returns before ever calling
# redirect()/url_for(). Covered end-to-end via a real app in test_auth_routes.py.

def test_login_required_rejects_with_401_json_when_no_session(monkeypatch):
    monkeypatch.setattr(auth, "get_current_user", lambda: None)

    @login_required
    def protected(current_user):
        return {"reached": True}

    app = Flask(__name__)
    with app.test_request_context("/api/whatever", method="POST"):
        resp, status = protected()
        assert status == 401
        assert resp.get_json()["session_expired"] is True


def test_login_required_passes_through_current_user(monkeypatch):
    fake_user = {"id": 1, "is_admin": 0}
    monkeypatch.setattr(auth, "get_current_user", lambda: fake_user)

    @login_required
    def protected(current_user):
        return current_user

    app = Flask(__name__)
    with app.test_request_context("/api/whatever", method="POST"):
        assert protected() is fake_user


def test_admin_required_rejects_non_admin_with_401_json(monkeypatch):
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "is_admin": 0})

    @admin_required
    def protected(current_user):
        return {"reached": True}

    app = Flask(__name__)
    with app.test_request_context("/api/whatever", method="POST"):
        resp, status = protected()
        assert status == 401
        assert resp.get_json()["session_expired"] is True


def test_admin_required_passes_through_admin_user(monkeypatch):
    admin_user = {"id": 1, "is_admin": 1}
    monkeypatch.setattr(auth, "get_current_user", lambda: admin_user)

    @admin_required
    def protected(current_user):
        return current_user

    app = Flask(__name__)
    with app.test_request_context("/api/whatever", method="POST"):
        assert protected() is admin_user
