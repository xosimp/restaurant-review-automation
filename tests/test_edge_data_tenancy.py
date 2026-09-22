"""Edge cases where a tenant or account boundary moves under a live login
(DATA audit: tenancy and account lifecycle).

What these protect:
  * A multi-location owner with two tabs open: switching location in one tab
    must not turn the other tab's next save into a write against the location
    it was not rendered for, and a job started in that tab must stay
    pollable. (DATA-10)
  * Removing or deactivating a teammate cuts off their phone's pushes, not
    just their sessions. (DATA-53)
  * An invited teammate never exists, even for one commit, with the
    primary-login ('client') role. (DATA-56)
  * Removing or deactivating an employee ends their staff-portal session,
    their PIN sign-in and the schedule links already emailed to them.
    (DATA-59)

Every sender (APNs, email, SMS) is stubbed. Tests marked xfail(strict=True)
assert the correct behaviour against a confirmed defect and flip to a
failure the day it is fixed.
"""
import sqlite3
import sys

import pytest
from flask import Flask

# Imported eagerly, before any test patches models.get_conn (see
# tests/test_alert_holds.py for why a late first import is a hazard).
import admin_routes
import auth
import auth_routes
import client_api
import mobile_api
import models
import notify
import ops
import push
import staff_settings
from admin_routes import admin_bp
from auth import (create_session, create_staff_session, create_user, get_session_user,
                  init_auth, invite_team_member, revoke_team_member, set_membership_pin,
                  set_user_role, upsert_membership, verify_membership_pin)
from client_api import client_bp
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant, get_restaurant


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    """Every loaded module whose `get_conn` is the real models.get_conn (a
    bound import) is pointed at this test's database, plus the named ones."""
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, auth, auth_routes, client_api, mobile_api, admin_routes, push,
                staff_settings):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    for mod in (models, auth, push, notify):
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    yield


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(client_bp)
    flask_app.register_blueprint(mobile_bp)
    flask_app.register_blueprint(admin_bp)
    return flask_app


def _loc(name, **kw):
    kw.setdefault("owner_email", "ann@a.test")
    return create_restaurant(Restaurant(name=name, **kw))


# ── DATA-10 · two tabs, one session, two locations ──────────────────────────

def _two_location_owner(app):
    lakeview = _loc("Syrup Lakeview", location_group="Syrup", location_name="Lakeview")
    wicker = _loc("Syrup Wicker Park", location_group="Syrup", location_name="Wicker Park")
    uid = create_user(lakeview, "ann", "ann@a.test", "correct-horse-1")
    set_user_role(uid, "owner")
    token = create_session(uid)
    client = app.test_client()
    client.set_cookie("session_token", token)
    return lakeview, wicker, client


def _alert_state(rid):
    r = get_restaurant(rid)
    return {"alert_1star": r.alert_1star, "alert_5star": r.alert_5star,
            "contacts": [c["phone"] for c in notify.get_alert_contacts(rid)]}


def _rendered_for(rid):
    """What a tab would carry to say which location it was rendered for."""
    return {"X-Restaurant-Id": str(rid), "X-Cavnar-Restaurant-Id": str(rid)}


def test_switching_location_moves_the_session_to_the_new_location(app):
    """The premise of the two-tab case, pinned: the switch is per session,
    so every tab sharing the cookie now acts on the new location."""
    lakeview, wicker, client = _two_location_owner(app)
    assert client.post("/api/switch-location", json={"restaurant_id": wicker}).status_code == 200
    token = client.get_cookie("session_token").value
    assert get_session_user(token)["restaurant_id"] == wicker


@pytest.mark.xfail(strict=True, reason="DATA-10: the active location is per session, and no write carries "
                                       "the location its page was rendered for, so tab A saves into B")
def test_a_write_from_a_tab_rendered_for_another_location_is_refused(app):
    lakeview, wicker, client = _two_location_owner(app)
    models.update_restaurant(lakeview, {"alert_1star": 1, "alert_5star": 1})
    models.update_restaurant(wicker, {"alert_1star": 0, "alert_5star": 0})
    notify.add_alert_contact(wicker, "Wicker GM", "+15125550222", sms_consent=True)
    wicker_before = _alert_state(wicker)

    # Tab A has Lakeview's alert settings open. In tab B the owner switches
    # to Wicker Park. Then tab A's Save goes out.
    assert client.post("/api/switch-location", json={"restaurant_id": wicker}).status_code == 200
    resp = client.post("/api/alert-settings", headers=_rendered_for(lakeview), json={
        "restaurant_id": lakeview,
        "contacts": [{"name": "Lakeview GM", "phone": "+15125550111"}],
        "alert_1star": 1, "alert_5star": 1, "urgent_via_email": 1,
    })

    assert _alert_state(wicker) == wicker_before, \
        "Lakeview's form landed on Wicker Park's alert settings"
    assert resp.status_code == 409 or _alert_state(lakeview)["contacts"] == ["+15125550111"]


@pytest.mark.xfail(strict=True, reason="DATA-10: read_async_job is scoped to the session's new location, "
                                       "so a job started in tab A answers 'Job not found' after tab B switches")
def test_a_job_started_in_one_tab_is_still_pollable_after_another_tab_switches(app):
    lakeview, wicker, client = _two_location_owner(app)
    ops.start_async_job("job-lakeview-1", "schedule", lakeview)
    ops.finish_async_job("job-lakeview-1", "done", {"ok": True, "schedule_csv": "date,employee\n"})

    assert client.post("/api/switch-location", json={"restaurant_id": wicker}).status_code == 200
    resp = client.get("/api/schedule-status/job-lakeview-1", headers=_rendered_for(lakeview))

    # Either the tab gets its own result, or it is told plainly that the
    # location changed underneath it. "Job not found" is neither.
    assert resp.status_code in (200, 409), resp.get_json()


# ── DATA-53 · a removed teammate's phone ────────────────────────────────────

@pytest.fixture
def pushes(monkeypatch):
    """Run fire_push's pool inline and record which device each push would
    have gone to. Nothing reaches APNs."""
    sent = []

    class _Inline:
        def submit(self, fn, *a, **k):
            fn(*a, **k)
    monkeypatch.setattr(push, "_push_executor", lambda: _Inline())
    monkeypatch.setattr(push, "_deliver",
                        lambda token_row, *a, **k: sent.append(token_row["apns_token"]) or {"ok": True})
    return sent


def _team_with_phones():
    rid = _loc("Simple EJ's", owner_email="erik@ej.test")
    owner = create_user(rid, "erik", "erik@ej.test", "owner-pass-1")
    mate = invite_team_member(rid, "Dana", "dana@ej.test", role="manager")["user_id"]
    push.register_device_token(owner, rid, "tok-owner", "production")
    push.register_device_token(mate, rid, "tok-dana", "production")
    return rid, owner, mate


def test_every_active_teammates_phone_gets_the_restaurants_push(pushes):
    """Baseline for the two tests below: before anyone is removed, both
    phones receive a restaurant-wide alert."""
    rid, _owner, _mate = _team_with_phones()
    push.fire_push(rid, "1star", "1-star review", "Cold food.")
    assert sorted(pushes) == ["tok-dana", "tok-owner"]


def test_a_revoked_teammates_device_gets_nothing(pushes):
    rid, owner, mate = _team_with_phones()
    assert revoke_team_member(rid, mate, acting_user_id=owner)["ok"] is True

    push.fire_push(rid, "1star", "1-star review", "Cold food.")

    assert "tok-owner" in pushes
    assert "tok-dana" not in pushes, "the removed manager still receives the restaurant's alerts"


def test_a_deactivated_logins_device_gets_nothing(app, pushes):
    rid, owner, mate = _team_with_phones()
    admin_rid = _loc("Cavnar HQ", owner_email="will@x.test")
    admin_uid = create_user(admin_rid, "will", "will@x.test", "admin-pass-1", is_admin=True)
    admin = app.test_client()
    admin.set_cookie("session_token", create_session(admin_uid))
    assert admin.post(f"/admin/deactivate-client/{mate}").get_json()["ok"] is True

    push.fire_push(rid, "1star", "1-star review", "Cold food.")

    assert "tok-owner" in pushes
    assert "tok-dana" not in pushes


# ── DATA-56 · the invite's role ─────────────────────────────────────────────

def _record_role_at_insert():
    """A trigger that records the role each users row is INSERTED with, so
    the check holds however invite_team_member is written."""
    conn = models.get_conn()
    conn.executescript("""
        CREATE TABLE _edge_role_at_insert (email TEXT, role TEXT);
        CREATE TRIGGER _edge_log_role AFTER INSERT ON users BEGIN
            INSERT INTO _edge_role_at_insert VALUES (NEW.email, COALESCE(NULLIF(NEW.role,''),'client'));
        END;
    """)
    conn.commit()
    conn.close()


def _roles_at_insert(email):
    conn = models.get_conn()
    rows = [r[0] for r in conn.execute("SELECT role FROM _edge_role_at_insert WHERE email=?", (email,))]
    conn.close()
    return rows


def test_an_invited_teammate_ends_up_with_the_role_they_were_invited_as():
    rid = _loc("Simple EJ's", owner_email="erik@ej.test")
    create_user(rid, "erik", "erik@ej.test", "owner-pass-1")
    out = invite_team_member(rid, "Dana", "dana@ej.test", role="manager")
    assert out["ok"] is True
    conn = models.get_conn()
    role = conn.execute("SELECT role FROM users WHERE id=?", (out["user_id"],)).fetchone()[0]
    conn.close()
    assert role == "manager"


@pytest.mark.xfail(strict=True, reason="DATA-56: create_user commits the login with the default 'client' "
                                       "role and set_user_role narrows it in a separate commit")
def test_an_invited_user_never_exists_with_the_client_role():
    rid = _loc("Simple EJ's", owner_email="erik@ej.test")
    create_user(rid, "erik", "erik@ej.test", "owner-pass-1")
    _record_role_at_insert()

    assert invite_team_member(rid, "Dana", "dana@ej.test", role="member")["ok"] is True

    assert _roles_at_insert("dana@ej.test") == ["member"]


@pytest.mark.xfail(strict=True, reason="DATA-56: when the role-narrowing commit fails, the invited login "
                                       "is left live with the primary-login 'client' role")
def test_an_invite_whose_role_write_fails_leaves_no_owner_level_login(monkeypatch):
    rid = _loc("Simple EJ's", owner_email="erik@ej.test")
    create_user(rid, "erik", "erik@ej.test", "owner-pass-1")

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(auth, "set_user_role", locked)

    try:
        invite_team_member(rid, "Dana", "dana@ej.test", role="member")
    except Exception:
        pass

    conn = models.get_conn()
    principals = conn.execute(
        "SELECT id FROM users WHERE email='dana@ej.test' AND is_active=1 "
        "AND COALESCE(NULLIF(role,''),'client') IN ('client','owner')").fetchall()
    conn.close()
    assert principals == [], "a teammate invite left a live owner-level login behind"


# ── DATA-59 · a removed or deactivated employee ─────────────────────────────

SCHEDULE_CSV = """date,day,employee,role,shift_start,shift_end,scheduled_hours,notes
2026-09-21,Monday,Jordan P.,Server,16:00,22:00,6.0,
2026-09-21,Monday,Marcus T.,Cook,08:00,16:00,8.0,
"""


def _restaurant_with_employee():
    rid = _loc("Simple EJ's", owner_email="erik@ej.test")
    owner = create_user(rid, "erik", "erik@ej.test", "owner-pass-1")
    models.add_manual_team_member(rid, "Jordan P.", role="Server", added_by="erik")
    emp = create_user(rid, "jordan", "jordan@ej.test", "unused-pass-1")
    m = upsert_membership(emp, rid, "employee", employee_name="Jordan P.")
    set_membership_pin(m["id"], rid, "8317")
    staff_token = create_staff_session(emp, rid)

    conn = models.get_conn()
    sid = conn.execute("""
        INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled,
                                      hours_budget, labor_target, schedule_csv, summary_json)
        VALUES (?, '2026-09-21', '2026-09-27', 14, 20, 30, ?, '[]')""", (rid, SCHEDULE_CSV)).lastrowid
    conn.commit()
    conn.close()
    share = models.create_schedule_share(rid, sid, "Jordan P.", sent_to="jordan@ej.test")
    return {"rid": rid, "owner": owner, "membership_id": m["id"],
            "staff_token": staff_token, "share": share}


def _remove_via_the_app(app, world):
    token = create_session(world["owner"])
    resp = app.test_client().post("/mobile/api/labor/team/remove",
                                  json={"employee_name": "Jordan P."},
                                  headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True, resp.get_json()


def _deactivate(app, world):
    staff_settings.upsert(world["rid"], "Jordan P.", active=False, updated_by="erik")


def test_an_employee_on_the_roster_can_use_their_session_pin_and_link():
    """Baseline for the tests below: before removal, all three work."""
    w = _restaurant_with_employee()
    assert get_session_user(w["staff_token"]) is not None
    assert verify_membership_pin(w["membership_id"], w["rid"], "8317")["ok"] is True
    share = models.get_schedule_share(w["share"])
    assert share is not None and not share["expired"]


@pytest.mark.parametrize("remove", [_remove_via_the_app, _deactivate], ids=["removed", "deactivated"])
@pytest.mark.xfail(strict=True, reason="DATA-59: removal deletes only the manual roster row and "
                                       "deactivation only affects generation; staff sessions survive")
def test_a_removed_employees_staff_session_ends(app, remove):
    w = _restaurant_with_employee()
    remove(app, w)
    assert get_session_user(w["staff_token"]) is None


@pytest.mark.parametrize("remove", [_remove_via_the_app, _deactivate], ids=["removed", "deactivated"])
@pytest.mark.xfail(strict=True, reason="DATA-59: the employee's membership stays active, so their PIN "
                                       "still signs them in")
def test_a_removed_employee_cannot_sign_in_with_their_pin(app, remove):
    w = _restaurant_with_employee()
    remove(app, w)
    assert verify_membership_pin(w["membership_id"], w["rid"], "8317")["ok"] is False


@pytest.mark.parametrize("remove", [_remove_via_the_app, _deactivate], ids=["removed", "deactivated"])
@pytest.mark.xfail(strict=True, reason="DATA-59: schedule share links are untouched by removal and stay "
                                       "valid for SCHEDULE_SHARE_TTL_DAYS (60)")
def test_a_removed_employee_cannot_open_old_schedule_links(app, remove):
    w = _restaurant_with_employee()
    remove(app, w)
    share = models.get_schedule_share(w["share"])
    assert share is None or share["expired"] is True
