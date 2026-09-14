"""PIN authentication for staff memberships.

A 4-digit PIN is 10,000 possibilities. The hash protects a stolen database;
the per-membership lockout is what protects a tablet on a pass. Both are
tested here, along with the tenant scoping that keeps a guessed membership_id
from reaching another restaurant.
"""
import pytest

import auth
import models
from auth import (PIN_LOCKOUT_MINUTES, PIN_MAX_ATTEMPTS, PinError,
                  clear_membership_pin, create_session, create_staff_session,
                  create_user, get_session_user, init_auth, pin_lockout_state,
                  set_membership_pin, unlock_membership_pin, upsert_membership,
                  validate_pin, verify_membership_pin)
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(auth, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)


def _restaurant(db_path, name="Simple EJ's"):
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


def _employee(db_path, rid, username="jordan", name="Jordan P.", pin="8317"):
    uid = create_user(rid, username, f"{username}@x.test", "unused-password", db_path=db_path)
    m = upsert_membership(uid, rid, "employee", employee_name=name, db_path=db_path)
    if pin:
        set_membership_pin(m["id"], rid, pin, db_path=db_path)
    return uid, m["id"]


# ── PIN quality ────────────────────────────────────────────────────────────

def test_a_valid_pin_is_accepted():
    assert validate_pin("8317") == "8317"
    assert validate_pin(" 4092 ") == "4092"


@pytest.mark.parametrize("bad", ["", "abc", "12a4", "123", "123456789", "1111", "1234", "4321"])
def test_weak_or_malformed_pins_are_refused(bad):
    with pytest.raises(PinError):
        validate_pin(bad)


def test_doubles_and_dates_are_still_allowed():
    """Over-filtering a 10,000-wide space costs more than it buys — the
    lockout is the real control, so only the truly trivial shapes go."""
    for ok in ("1122", "0708", "2580"):
        assert validate_pin(ok) == ok


# ── storage ────────────────────────────────────────────────────────────────

def test_the_pin_is_never_stored_in_plain_text(db_path):
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin="8317")
    conn = get_conn(db_path)
    row = conn.execute("SELECT pin_hash FROM memberships WHERE id=?", (mid,)).fetchone()
    conn.close()
    assert row["pin_hash"]
    assert "8317" not in row["pin_hash"]
    assert len(row["pin_hash"]) > 40


def test_two_employees_may_share_a_pin(db_path):
    """A PIN is never an identifier — the flow resolves who first, then
    verifies — so a collision is not a conflict."""
    rid = _restaurant(db_path)
    _u1, m1 = _employee(db_path, rid, username="jordan", pin="8317")
    _u2, m2 = _employee(db_path, rid, username="dana", pin="8317")
    assert verify_membership_pin(m1, rid, "8317", db_path=db_path)["ok"]
    assert verify_membership_pin(m2, rid, "8317", db_path=db_path)["ok"]


def test_the_same_pin_hashes_differently_per_membership(db_path):
    rid = _restaurant(db_path)
    _u1, m1 = _employee(db_path, rid, username="jordan", pin="8317")
    _u2, m2 = _employee(db_path, rid, username="dana", pin="8317")
    conn = get_conn(db_path)
    hashes = [r["pin_hash"] for r in conn.execute(
        "SELECT pin_hash FROM memberships WHERE id IN (?,?)", (m1, m2))]
    conn.close()
    assert hashes[0] != hashes[1]


# ── verification ───────────────────────────────────────────────────────────

def test_the_correct_pin_verifies(db_path):
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin="8317")
    assert verify_membership_pin(mid, rid, "8317", db_path=db_path) == {"ok": True}


def test_a_wrong_pin_is_refused(db_path):
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin="8317")
    out = verify_membership_pin(mid, rid, "0000", db_path=db_path)
    assert out["ok"] is False


def test_a_membership_with_no_pin_cannot_sign_in(db_path):
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin=None)
    assert verify_membership_pin(mid, rid, "8317", db_path=db_path)["ok"] is False


def test_no_pin_and_wrong_pin_are_indistinguishable(db_path):
    """Otherwise anyone holding the portal link could enumerate which staff
    have access by watching the error text."""
    rid = _restaurant(db_path)
    _u1, with_pin = _employee(db_path, rid, username="jordan", pin="8317")
    _u2, without = _employee(db_path, rid, username="dana", pin=None)
    a = verify_membership_pin(with_pin, rid, "0000", db_path=db_path)
    b = verify_membership_pin(without, rid, "0000", db_path=db_path)
    assert a["error"] == b["error"]


def test_a_deactivated_membership_cannot_sign_in(db_path):
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin="8317")
    auth.set_membership_active(mid, rid, False, db_path=db_path)
    assert verify_membership_pin(mid, rid, "8317", db_path=db_path)["ok"] is False


def test_clearing_a_pin_removes_access_without_deleting_the_person(db_path):
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin="8317")
    assert clear_membership_pin(mid, rid, db_path=db_path) is True
    assert verify_membership_pin(mid, rid, "8317", db_path=db_path)["ok"] is False
    assert auth.get_membership(_uid, rid, db_path=db_path) is not None


# ── tenant isolation ───────────────────────────────────────────────────────

def test_a_pin_cannot_be_verified_against_another_restaurant(db_path):
    """The membership_id is a guessable integer. restaurant_id comes from the
    portal token, never the client, and is re-checked here."""
    rid_a = _restaurant(db_path, "Simple EJ's")
    rid_b = _restaurant(db_path, "Gia Mia")
    _uid, mid = _employee(db_path, rid_b, pin="8317")
    assert verify_membership_pin(mid, rid_a, "8317", db_path=db_path)["ok"] is False
    assert verify_membership_pin(mid, rid_b, "8317", db_path=db_path)["ok"] is True


def test_a_pin_cannot_be_set_across_tenants(db_path):
    rid_a = _restaurant(db_path, "Simple EJ's")
    rid_b = _restaurant(db_path, "Gia Mia")
    _uid, mid = _employee(db_path, rid_b, pin="8317")
    assert set_membership_pin(mid, rid_a, "5063", db_path=db_path) is False
    assert verify_membership_pin(mid, rid_b, "8317", db_path=db_path)["ok"] is True


def test_unlock_is_scoped_to_the_acting_restaurant(db_path):
    rid_a = _restaurant(db_path, "Simple EJ's")
    rid_b = _restaurant(db_path, "Gia Mia")
    _uid, mid = _employee(db_path, rid_b, pin="8317")
    for _ in range(PIN_MAX_ATTEMPTS):
        verify_membership_pin(mid, rid_b, "0000", db_path=db_path)
    assert unlock_membership_pin(mid, rid_a, db_path=db_path) is False
    assert pin_lockout_state(mid, db_path=db_path)["locked"] is True


# ── brute force ────────────────────────────────────────────────────────────

def test_repeated_misses_lock_the_membership_out(db_path):
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin="8317")
    for _ in range(PIN_MAX_ATTEMPTS - 1):
        out = verify_membership_pin(mid, rid, "0000", db_path=db_path)
        assert out["locked"] is False
    final = verify_membership_pin(mid, rid, "0000", db_path=db_path)
    assert final["locked"] is True
    assert pin_lockout_state(mid, db_path=db_path)["locked"] is True


def test_a_locked_membership_refuses_even_the_correct_pin(db_path):
    """Otherwise the lockout is decorative — an attacker who lands on the
    right PIN mid-lockout would still get in."""
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin="8317")
    for _ in range(PIN_MAX_ATTEMPTS):
        verify_membership_pin(mid, rid, "0000", db_path=db_path)
    out = verify_membership_pin(mid, rid, "8317", db_path=db_path)
    assert out["ok"] is False
    assert out["locked"] is True


def test_the_lockout_is_per_membership_not_per_restaurant(db_path):
    """A whole kitchen shares one NAT address, so an IP-scoped lockout would
    take the entire staff down after five typos by one person."""
    rid = _restaurant(db_path)
    _u1, locked_out = _employee(db_path, rid, username="jordan", pin="8317")
    _u2, unaffected = _employee(db_path, rid, username="dana", pin="4092")
    for _ in range(PIN_MAX_ATTEMPTS):
        verify_membership_pin(locked_out, rid, "0000", db_path=db_path)
    assert verify_membership_pin(locked_out, rid, "8317", db_path=db_path)["locked"] is True
    assert verify_membership_pin(unaffected, rid, "4092", db_path=db_path)["ok"] is True


def test_a_correct_pin_clears_the_failure_count(db_path):
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin="8317")
    for _ in range(PIN_MAX_ATTEMPTS - 1):
        verify_membership_pin(mid, rid, "0000", db_path=db_path)
    assert verify_membership_pin(mid, rid, "8317", db_path=db_path)["ok"] is True
    assert pin_lockout_state(mid, db_path=db_path)["failed_count"] == 0


def test_probing_a_membership_with_no_pin_is_also_throttled(db_path):
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin=None)
    for _ in range(PIN_MAX_ATTEMPTS):
        verify_membership_pin(mid, rid, "0000", db_path=db_path)
    assert pin_lockout_state(mid, db_path=db_path)["locked"] is True


def test_an_owner_can_unlock_and_a_reset_pin_clears_the_lockout(db_path):
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin="8317")
    for _ in range(PIN_MAX_ATTEMPTS):
        verify_membership_pin(mid, rid, "0000", db_path=db_path)
    assert unlock_membership_pin(mid, rid, db_path=db_path) is True
    assert verify_membership_pin(mid, rid, "8317", db_path=db_path)["ok"] is True

    for _ in range(PIN_MAX_ATTEMPTS):
        verify_membership_pin(mid, rid, "0000", db_path=db_path)
    set_membership_pin(mid, rid, "5063", db_path=db_path)
    assert verify_membership_pin(mid, rid, "5063", db_path=db_path)["ok"] is True


# ── sessions ───────────────────────────────────────────────────────────────

def test_a_staff_session_is_shift_length_and_tagged(db_path):
    rid = _restaurant(db_path)
    uid, _mid = _employee(db_path, rid, pin="8317")
    token = create_staff_session(uid, rid, db_path=db_path)
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT device_type, expires_at FROM sessions WHERE token=?",
        (auth.hash_session_token(token),)).fetchone()
    conn.close()
    assert row["device_type"] == "staff_pin"
    from datetime import datetime as _dt, timezone as _tz
    remaining = _dt.fromisoformat(row["expires_at"]) - _dt.now(_tz.utc)
    assert 0 < remaining.total_seconds() <= auth.STAFF_SESSION_HOURS * 3600 + 60


def test_rotating_a_pin_ends_the_sessions_it_had_issued(db_path):
    """A rotated PIN has to revoke the old one, not leave it working for the
    rest of the shift."""
    rid = _restaurant(db_path)
    uid, mid = _employee(db_path, rid, pin="8317")
    token = create_staff_session(uid, rid, db_path=db_path)
    assert get_session_user(token, db_path=db_path) is not None
    set_membership_pin(mid, rid, "5063", db_path=db_path)
    assert get_session_user(token, db_path=db_path) is None


def test_deactivating_a_membership_ends_its_live_sessions(db_path):
    rid = _restaurant(db_path)
    uid, mid = _employee(db_path, rid, pin="8317")
    token = create_staff_session(uid, rid, db_path=db_path)
    auth.set_membership_active(mid, rid, False, db_path=db_path)
    assert get_session_user(token, db_path=db_path) is None


def test_a_pin_change_does_not_sign_the_person_out_of_the_console(db_path):
    """Someone can hold a console login AND a PIN. Rotating the PIN must not
    kick them out of the dashboard."""
    rid = _restaurant(db_path)
    uid = create_user(rid, "dana", "dana@x.test", "real-password", db_path=db_path)
    m = upsert_membership(uid, rid, "manager", employee_name="Dana K.", db_path=db_path)
    set_membership_pin(m["id"], rid, "8317", db_path=db_path)
    console = create_session(uid, db_path=db_path)          # web/console session
    staff = create_staff_session(uid, rid, db_path=db_path)  # PIN session

    set_membership_pin(m["id"], rid, "5063", db_path=db_path)
    assert get_session_user(staff, db_path=db_path) is None
    assert get_session_user(console, db_path=db_path) is not None
