"""Employee self-signup: phone → code → join code → claim a name → PIN.

The owner does nothing per employee. The control is not on who may SIGN UP —
an account with no membership can see nothing, because every staff route
derives the restaurant from the membership and there isn't one — it is on
which NAME may be claimed, and each name may be claimed exactly once.

That single constraint carries most of this file. Without it, anyone holding
the join code could register as "Jordan P." and inherit Jordan's schedule and
tick Jordan's tasks, because task_completions records completed_by as a name
string.
"""
import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models
from auth import (create_session, create_user, get_join_code, init_auth,
                  set_membership_pin, upsert_membership)
from models import Restaurant, create_restaurant, get_conn
from staff_routes import staff_bp


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)


@pytest.fixture
def app(db_path):
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(staff_bp)
    flask_app.register_blueprint(client_api.client_bp)
    flask_app.register_blueprint(mobile_api.mobile_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, name="Simple EJ's", owner="erik@x.test"):
    rid = create_restaurant(Restaurant(name=name, owner_email=owner), db_path=db_path)
    models.init_manual_team_members(db_path)
    return rid


def _roster(db_path, rid, people=(("Jordan P.", "Bartender"), ("Dana K.", "Server"))):
    for name, role in people:
        models.add_manual_team_member(rid, name, role, db_path=db_path)


def _owner(db_path, rid, username="erik"):
    uid = create_user(rid, username, f"{username}@x.test", "pw", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    return uid


def _verified(client, phone="5550142233"):
    """Walk a phone through steps 1 and 2, returning the signup token."""
    started = client.post("/staff/api/signup/start",
                          json={"phone": phone, "optin": True}).get_json()
    assert started["ok"], started
    code = started["dev_code"]
    done = client.post("/staff/api/signup/verify",
                       json={"phone": phone, "code": code}).get_json()
    assert done["ok"], done
    return done["signup_token"]


def _signup(client, db_path, rid, phone="5550142233", name="Jordan P.", pin="5063"):
    token = _verified(client, phone)
    code = get_join_code(rid, db_path=db_path)
    # As the iOS app claims: with its device identity, which is what earns
    # the bearer token in the body (a browser gets only the HttpOnly cookie,
    # SEC-36).
    return client.post("/staff/api/signup/claim",
                       json={"signup_token": token, "join_code": code,
                             "employee_name": name, "pin": pin, "device_id": "test-device"})


# ── the happy path ─────────────────────────────────────────────────────────

def test_an_employee_can_create_their_own_account(client, db_path):
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    resp = _signup(client, db_path, rid)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["employee_name"] == "Jordan P."
    assert body["job_role"] == "Bartender"
    assert body["token"]


def test_the_new_account_can_immediately_see_its_own_portal(client, db_path):
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    token = _signup(client, db_path, rid).get_json()["token"]
    me = client.get("/staff/api/me", headers={"Authorization": f"Bearer {token}"}).get_json()
    assert me["ok"] is True
    assert me["employee"]["name"] == "Jordan P."
    assert me["employee"]["has_pin"] is True


def test_the_claimed_pin_works_on_the_shared_tablet_too(client, db_path):
    """One credential, both places — the phone they signed up on and the
    tablet on the pass."""
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    _signup(client, db_path, rid, pin="5063")
    code = get_join_code(rid, db_path=db_path)
    roster = client.get(f"/staff/api/roster/{code}").get_json()
    mine = [r for r in roster["roster"] if r["name"] == "Jordan P."][0]
    signin = client.post(f"/staff/r/{code}/login",
                         json={"membership_id": mine["membership_id"], "pin": "5063",
                               "nonce": roster["login_nonce"]})
    assert signin.status_code == 200


def test_the_owner_does_nothing_and_still_sees_who_signed_up(client, db_path):
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    owner = _owner(db_path, rid)
    _signup(client, db_path, rid)
    client.set_cookie("session_token", create_session(owner, db_path=db_path))
    listed = client.get("/api/account/staff").get_json()
    row = [s for s in listed["staff"] if s["name"] == "Jordan P."][0]
    assert row["self_signup"] is True
    assert row["claimed_by_phone"] == "+15550142233"
    assert row["claimed_at"]
    assert listed["join_code"]
    assert listed["unclaimed"] == ["Dana K."]


# ── the constraint that makes open signup safe ─────────────────────────────

def test_a_name_can_only_be_claimed_once(client, db_path):
    """The attack open signup would otherwise allow: register as someone who
    already works here and inherit their schedule and their task record."""
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    assert _signup(client, db_path, rid, phone="5550142233").status_code == 200
    second = _signup(client, db_path, rid, phone="5550148888", name="Jordan P.")
    assert second.status_code == 400
    assert "isn't available" in second.get_json()["error"]


def test_a_claimed_name_disappears_from_the_list(client, db_path):
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    _signup(client, db_path, rid)
    token = _verified(client, "5550148888")
    code = get_join_code(rid, db_path=db_path)
    names = client.get(f"/staff/api/signup/claimable/{code}?signup_token={token}").get_json()
    assert [n["name"] for n in names["names"]] == ["Dana K."]


def test_a_name_that_is_not_on_the_roster_cannot_be_invented(client, db_path):
    """No junk entries, and no name that misses the schedule."""
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    resp = _signup(client, db_path, rid, name="Somebody Else")
    assert resp.status_code == 400
    assert "isn't available" in resp.get_json()["error"]


def test_the_claimable_pool_is_never_sample_data(client, db_path, monkeypatch):
    """A restaurant with no roster and no schedule offers nothing — rather
    than offering bundled fixtures, which would let a stranger claim a person
    who does not work there."""
    import labor
    called = []
    monkeypatch.setattr(labor, "load_shifts_for_restaurant",
                        lambda *a, **k: called.append(1) or [])
    rid = _restaurant(db_path)
    token = _verified(client)
    code = get_join_code(rid, db_path=db_path)
    body = client.get(f"/staff/api/signup/claimable/{code}?signup_token={token}").get_json()
    assert body["names"] == []
    assert body["none_left"] is True
    assert not called


def test_the_job_role_comes_from_the_roster_not_the_employee(client, db_path):
    """The job decides which checklist they may complete, so it is not
    something the person signing up gets to assert."""
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    token = _verified(client)
    code = get_join_code(rid, db_path=db_path)
    made = client.post("/staff/api/signup/claim",
                       json={"signup_token": token, "join_code": code,
                             "employee_name": "Jordan P.", "pin": "5063",
                             "job_role": "Manager"}).get_json()
    assert made["job_role"] == "Bartender"


# ── phone verification ─────────────────────────────────────────────────────

def test_an_unverified_phone_cannot_claim_anything(client, db_path):
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    code = get_join_code(rid, db_path=db_path)
    resp = client.post("/staff/api/signup/claim",
                       json={"signup_token": "made-up", "join_code": code,
                             "employee_name": "Jordan P.", "pin": "5063"})
    assert resp.status_code == 400
    assert "expired" in resp.get_json()["error"]


def test_an_unverified_phone_cannot_read_the_roster(client, db_path):
    """The claimable list is real people's names."""
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    code = get_join_code(rid, db_path=db_path)
    resp = client.get(f"/staff/api/signup/claimable/{code}?signup_token=nope")
    assert resp.status_code == 401


def test_a_wrong_code_is_refused_and_counted(client, db_path):
    client.post("/staff/api/signup/start", json={"phone": "5550142233", "optin": True})
    for _ in range(auth.SIGNUP_MAX_ATTEMPTS):
        bad = client.post("/staff/api/signup/verify",
                          json={"phone": "5550142233", "code": "000000"})
        assert bad.status_code == 400
    locked = client.post("/staff/api/signup/verify",
                         json={"phone": "5550142233", "code": "000000"}).get_json()
    assert "Too many tries" in locked["error"]


def test_a_signup_token_is_single_use(client, db_path):
    """Two people cannot ride one verified phone."""
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    token = _verified(client)
    code = get_join_code(rid, db_path=db_path)
    first = client.post("/staff/api/signup/claim",
                        json={"signup_token": token, "join_code": code,
                              "employee_name": "Jordan P.", "pin": "5063"})
    assert first.status_code == 200
    second = client.post("/staff/api/signup/claim",
                         json={"signup_token": token, "join_code": code,
                               "employee_name": "Dana K.", "pin": "7412"})
    assert second.status_code == 400


def test_a_short_number_is_refused(client, db_path):
    resp = client.post("/staff/api/signup/start", json={"phone": "555", "optin": True})
    assert resp.status_code == 400


def test_a_code_is_refused_without_consent(client, db_path):
    """Server-side half of the A2P 10DLC opt-in requirement (Twilio's own
    rejection: "the opt-in checkbox is missing or appears to be
    pre-selected"). The client box is a courtesy — a direct call has to be
    refused too, or the consent isn't real."""
    resp = client.post("/staff/api/signup/start", json={"phone": "5550142233"})
    assert resp.status_code == 400
    assert "consent" in resp.get_json()["error"].lower()

    resp2 = client.post("/staff/api/signup/start",
                        json={"phone": "5550142233", "optin": False})
    assert resp2.status_code == 400


def test_the_dev_code_never_appears_on_a_deployment(client, db_path, monkeypatch):
    """The escape hatch that makes this testable locally must not be one that
    ships."""
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    body = client.post("/staff/api/signup/start",
                       json={"phone": "5550142233", "optin": True}).get_json()
    assert "dev_code" not in body


def test_the_dev_code_appears_locally_even_when_twilio_is_configured(client, db_path, monkeypatch):
    """The fallback keys on the DEPLOYMENT, not on whether the send worked.

    Twilio answers 201 the moment it accepts a message, and a carrier can
    reject it seconds later — so "sent" is not a fact the send can report.
    Keying the fallback on that 201 produced the worst outcome available: no
    text arrived AND no code was shown.
    """
    for var in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "CAVNAR_FORCE_SECURE_COOKIES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACfake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "faketoken")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15550000000")
    import notify
    # Queued, exactly as Twilio reports a message it has merely accepted.
    monkeypatch.setattr(notify, "send_sms", lambda *a, **k: True)

    body = client.post("/staff/api/signup/start",
                       json={"phone": "5550142233", "optin": True}).get_json()
    assert body["sms_sent"] is True
    assert body.get("dev_code"), "no code shown even though nothing was delivered"
    # And it is a code that actually works.
    assert client.post("/staff/api/signup/verify",
                       json={"phone": "5550142233",
                             "code": body["dev_code"]}).get_json()["ok"] is True


def test_the_configured_check_reads_the_names_notify_actually_uses(monkeypatch):
    """These names are not guessable and must match notify.py exactly — this
    asked for TWILIO_PHONE_NUMBER, which exists nowhere."""
    import notify
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        monkeypatch.setenv(var, "x")
    assert auth._sms_configured() is True
    monkeypatch.delenv("TWILIO_FROM_NUMBER")
    assert auth._sms_configured() is False
    # The source of truth: notify.py reads these three and no others.
    src = open(notify.__file__).read()
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        assert f'os.getenv("{var}"' in src, var


# ── the join code ──────────────────────────────────────────────────────────

def test_the_join_code_is_typeable(db_path):
    """It gets read off a whiteboard and typed on a phone, so the characters
    people confuse are simply not in the alphabet."""
    rid = _restaurant(db_path)
    code = get_join_code(rid, db_path=db_path)
    assert len(code) == auth.JOIN_CODE_LENGTH
    assert not (set(code) & set("IO01"))


def test_the_join_code_works_wherever_the_long_token_does(client, db_path):
    """An employee should never have to know which kind of code they hold."""
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    _signup(client, db_path, rid)
    code = get_join_code(rid, db_path=db_path)
    assert client.get(f"/staff/api/roster/{code}").get_json()["ok"] is True
    assert client.get(f"/staff/r/{code}").status_code == 200


def test_lowercase_and_spacing_still_resolve(client, db_path):
    rid = _restaurant(db_path)
    code = get_join_code(rid, db_path=db_path)
    body = client.get(f"/staff/api/signup/where/{code.lower()}").get_json()
    assert body["ok"] is True
    assert body["restaurant"] == "Simple EJ's"


def test_rotating_the_link_kills_the_old_join_code(client, db_path):
    rid = _restaurant(db_path)
    old = get_join_code(rid, db_path=db_path)
    auth.rotate_staff_portal_token(rid, db_path=db_path)
    new = get_join_code(rid, db_path=db_path)
    assert new != old
    assert client.get(f"/staff/api/signup/where/{old}").status_code == 404
    assert client.get(f"/staff/api/signup/where/{new}").status_code == 200


def test_a_join_code_does_not_reach_another_restaurant(client, db_path):
    rid_a = _restaurant(db_path, "A")
    rid_b = _restaurant(db_path, "B", owner="other@x.test")
    _roster(db_path, rid_b, (("Stranger S.", "Cook"),))
    token = _verified(client)
    code_a = get_join_code(rid_a, db_path=db_path)
    body = client.get(f"/staff/api/signup/claimable/{code_a}?signup_token={token}").get_json()
    assert body["names"] == []


# ── one person, one identity ───────────────────────────────────────────────

def test_the_same_phone_at_a_second_restaurant_reuses_the_identity(client, db_path):
    """The whole reason identity and membership are separate tables: one
    person who works two jobs is one account with two memberships, not two
    accounts."""
    rid_a = _restaurant(db_path, "A")
    rid_b = _restaurant(db_path, "B", owner="other@x.test")
    _roster(db_path, rid_a, (("Jordan P.", "Bartender"),))
    _roster(db_path, rid_b, (("Jordan P.", "Server"),))
    first = _signup(client, db_path, rid_a).get_json()
    second = _signup(client, db_path, rid_b, pin="7412").get_json()
    assert first["ok"] and second["ok"]

    user = auth.get_user_by_phone("5550142233", db_path=db_path)
    memberships = auth.get_memberships_for_user(user["id"], db_path=db_path)
    assert len(memberships) == 2
    assert {m["restaurant_id"] for m in memberships} == {rid_a, rid_b}
    assert {m["job_role"] for m in memberships} == {"Bartender", "Server"}


# ── the owner's undo ───────────────────────────────────────────────────────

def test_an_owner_can_take_a_wrongly_claimed_name_back(client, db_path):
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    owner = _owner(db_path, rid)
    token = _signup(client, db_path, rid).get_json()["token"]
    mid = [s for s in auth.get_memberships_for_restaurant(rid, role="employee",
                                                          db_path=db_path)][0]["id"]

    client.set_cookie("session_token", create_session(owner, db_path=db_path))
    assert client.post(f"/api/account/staff/{mid}/unlink").status_code == 200
    # Signed out immediately, not at the end of the shift.
    assert auth.get_session_user(token, db_path=db_path) is None


def test_an_unlinked_name_goes_back_on_the_list(client, db_path):
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    _signup(client, db_path, rid)
    mid = auth.get_memberships_for_restaurant(rid, role="employee", db_path=db_path)[0]["id"]
    auth.unlink_claimed_membership(mid, rid, db_path=db_path)

    token = _verified(client, "5550148888")
    code = get_join_code(rid, db_path=db_path)
    names = client.get(f"/staff/api/signup/claimable/{code}?signup_token={token}").get_json()
    assert "Jordan P." in [n["name"] for n in names["names"]]


def test_the_unlinked_phone_cannot_take_it_again(client, db_path):
    """Otherwise 'not them' is a revolving door."""
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    _signup(client, db_path, rid, phone="5550142233")
    mid = auth.get_memberships_for_restaurant(rid, role="employee", db_path=db_path)[0]["id"]
    auth.unlink_claimed_membership(mid, rid, db_path=db_path)

    again = _signup(client, db_path, rid, phone="5550142233")
    assert again.status_code == 400
    assert "can't be used here" in again.get_json()["error"]


def test_a_manager_cannot_unlink(client, db_path):
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    _signup(client, db_path, rid)
    mid = auth.get_memberships_for_restaurant(rid, role="employee", db_path=db_path)[0]["id"]
    mgr = create_user(rid, "mgr", "mgr@x.test", "pw", db_path=db_path)
    upsert_membership(mgr, rid, "manager", db_path=db_path)
    client.set_cookie("session_token", create_session(mgr, db_path=db_path))
    assert client.post(f"/api/account/staff/{mid}/unlink").status_code == 403


# ── the boundary still holds ───────────────────────────────────────────────

def test_a_self_signed_up_employee_is_still_locked_out_of_the_console(client, db_path):
    """Everything the audit hardened has to survive a new front door."""
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    token = _signup(client, db_path, rid).get_json()["token"]
    for path in ("/api/labor/team", "/api/food-cost/menu-profitability",
                 "/api/team/inbox", "/api/account/staff"):
        client.set_cookie("session_token", token)
        assert client.get(path).status_code == 403, path


def test_a_weak_pin_is_refused_at_signup(client, db_path):
    rid = _restaurant(db_path)
    _roster(db_path, rid)
    resp = _signup(client, db_path, rid, pin="1234")
    assert resp.status_code == 400
    assert "sequence" in resp.get_json()["error"].lower()


# ── OTP traffic goes to its own campaign, not the alert campaign ──────────

def test_the_signup_code_is_sent_on_the_otp_service_not_the_alert_service(
        client, db_path, monkeypatch):
    """A Messaging Service maps to exactly one A2P Campaign. Routing OTP
    traffic through the alert service would be the mixed-use-case pattern
    carriers filter hardest — this is what keeps them apart."""
    import notify
    monkeypatch.setattr(notify, "TWILIO_SID", "ACfake")
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "faketoken")
    monkeypatch.setattr(notify, "TWILIO_FROM", "+15550000000")
    monkeypatch.setattr(notify, "TWILIO_MESSAGING_SERVICE_SID", "MGalert000000000000000000000000")
    monkeypatch.setattr(notify, "TWILIO_OTP_MESSAGING_SERVICE_SID", "MGotp0000000000000000000000000")

    sent = {}

    class FakeResponse:
        status_code = 201
        text = ""

    def fake_post(url, auth, data, timeout):
        sent.update(data)
        return FakeResponse()

    monkeypatch.setattr(notify.requests, "post", fake_post)

    ok = client.post("/staff/api/signup/start",
                     json={"phone": "5550142233", "optin": True}).get_json()
    assert ok["ok"] is True
    assert sent.get("MessagingServiceSid") == "MGotp0000000000000000000000000"
    assert "From" not in sent


def test_the_otp_send_falls_back_to_plain_from_when_no_otp_service_is_set(
        client, db_path, monkeypatch):
    """Until the second Messaging Service is provisioned, OTP sends must
    still go out — on TWILIO_FROM, never silently borrowing the alert
    campaign's service."""
    import notify
    monkeypatch.setattr(notify, "TWILIO_SID", "ACfake")
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "faketoken")
    monkeypatch.setattr(notify, "TWILIO_FROM", "+15550000000")
    monkeypatch.setattr(notify, "TWILIO_MESSAGING_SERVICE_SID", "MGalert000000000000000000000000")
    monkeypatch.setattr(notify, "TWILIO_OTP_MESSAGING_SERVICE_SID", "")

    sent = {}

    class FakeResponse:
        status_code = 201
        text = ""

    def fake_post(url, auth, data, timeout):
        sent.update(data)
        return FakeResponse()

    monkeypatch.setattr(notify.requests, "post", fake_post)

    ok = client.post("/staff/api/signup/start",
                     json={"phone": "5550142233", "optin": True}).get_json()
    assert ok["ok"] is True
    assert sent.get("From") == "+15550000000"
    assert "MessagingServiceSid" not in sent
