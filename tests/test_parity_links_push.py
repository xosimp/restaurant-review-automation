"""Links, push notifications and deep links — iOS parity fix round, 10/7/26.

#1   an email or text link opens the app: the issue fallback email names the
     issue (?nav=issue/<id>&loc=), and a link's rec/src open is recorded from
     the phone (POST /mobile/api/recs/link-open, rec_delivery.record_link_open).
#22  the quiet-night push opens Marketing's opportunity feed on the night's
     card and the drafted post (push.nav_for), not Marketing's top.
#35  a push the app may answer (rec_key + answerable) carries Done / Not for
     us (CAVNAR_REC / CAVNAR_REC_ASK).
#36  an Approve & post push carries the reply it publishes, clipped to fit
     APNs' 4 KB, and says whether the text is whole (`draft_complete`).
#49  every push opens where its bell row does (nav._ALERT_NAV).
#55  publish_held "Send now" (blocker_keys), critical_low "Draft order",
     login "This wasn't me" (login_user_id; POST /mobile/api/account/not-me),
     connection "Reconnect", dsr "Ask about last night"; resp_approved has no
     Reply.
Nothing is sent: pushes are captured, email is stubbed.
"""
import json
import os

import pytest
from flask import Flask

import auth
import auth_routes
import client_api
import delayed
import mobile_api
import models
import nav
import push
from auth import create_session, create_user, init_auth
from models import Restaurant, create_restaurant, get_conn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _world(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, auth, auth_routes, client_api, mobile_api, push):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(push, "DB_PATH", db_path)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    push.init_push(db_path=db_path)


def _rid(db_path, name="Links Co"):
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


def _capture(monkeypatch):
    got = []

    class _Now:
        def submit(self, fn, token_row, alert_type, title, body, data, db, *rest):
            got.append((alert_type, data))
    monkeypatch.setattr(push, "_push_executor", lambda: _Now())
    monkeypatch.setattr(push, "get_device_tokens", lambda *a, **k: [{"user_id": 1, "restaurant_id": 1}])
    return got


def _review(db_path, rid, draft):
    conn = get_conn(db_path)
    cur = conn.execute(
        """INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text,
           review_date, fetched_at, sentiment, urgency, processed, response_status, draft_response)
           VALUES (?,'google',?,'Ann',5,'Lovely',datetime('now','-1 days'),datetime('now','-1 days'),
                   'positive','normal',1,'drafted',?)""", (rid, f"ext-{len(draft)}", draft))
    conn.commit()
    rev = cur.lastrowid
    conn.close()
    return rev


# ── #49: the push opens where the bell row does ──────────────────────────────

@pytest.mark.parametrize("alert_type", sorted(nav._ALERT_NAV))
def test_every_push_opens_where_its_bell_row_does(alert_type):
    assert push.nav_for(alert_type, {}) == nav.for_notification(alert_type)


@pytest.mark.parametrize("alert_type,place", [
    ("staff_signin", "account/people"), ("price_spike", "inventory/invoices"),
    ("labor_over", "labor/overtime"), ("labor_reminder", "labor/waiting"),
    ("connection_lost", "account/integrations"), ("data_source_down", "account/integrations"),
    ("data_source_restored", "account/integrations"), ("demand_opportunity", "marketing/opportunities"),
])
def test_the_places_the_audit_named(alert_type, place):
    assert nav.for_notification(alert_type) == place
    assert push.nav_for(alert_type, {}) == place


# ── #22: the quiet night lands on its card and its draft ─────────────────────

def test_the_quiet_night_push_names_the_card_and_the_draft():
    assert push.nav_for("demand_opportunity", {"card": "slow_day:Tuesday", "post_draft_id": 41}) == \
        "marketing/opportunities?card=slow_day%3ATuesday&post_draft_id=41"
    # No post drafted (the model was skipped): the card alone.
    assert push.nav_for("demand_opportunity", {"card": "slow_day:Tuesday"}) == \
        "marketing/opportunities?card=slow_day%3ATuesday"


def test_the_quiet_night_job_no_longer_hard_codes_marketings_top():
    src = open(os.path.join(ROOT, "strategy_jobs.py"), encoding="utf-8").read()
    job = src[src.index("def run_demand_opportunity"):src.index("def quiet_night_declined")]
    assert '"nav": "marketing"' not in job
    assert '"card": f"slow_day:{out[\'weekday\']}"' in job


# ── #35 / #55: the categories ────────────────────────────────────────────────

def test_an_answerable_recommendation_gets_done_and_not_for_us():
    rec = {"rec_key": "labor_over:sat", "answerable": True}
    assert push._category("labor_over", rec) == push.CATEGORY_REC
    # A brief keeps its Ask button beside the answers.
    assert push._category("morning_brief", dict(rec, ask_prompt="Why?")) == push.CATEGORY_REC_ASK
    # Not answerable, or no key: no answers offered.
    assert push._category("labor_over", {"rec_key": "x", "answerable": False}) == ""
    assert push._category("labor_over", {"answerable": True}) == ""
    # A review alert keeps its reply buttons, an issue or coverage gap its own.
    assert push._category("1star", dict(rec, review_id=4)) == push.CATEGORY_REVIEW
    assert push._category("issue", rec) == push.CATEGORY_ISSUE
    assert push._category("coverage", rec) == push.CATEGORY_ISSUE


def test_the_types_that_had_no_button():
    assert push._category("schedule_publish_held", {"schedule_id": 8, "blocker_keys": ["no_manager:fri"]}) \
        == push.CATEGORY_PUBLISH_HELD
    # Without the keys it held on, Send now could not acknowledge them: none.
    assert push._category("schedule_publish_held", {"schedule_id": 8}) == ""
    assert push._category("critical_low", {}) == push.CATEGORY_STOCK
    assert push._category("critical_low", {"rec_key": "k", "answerable": True}) == push.CATEGORY_STOCK
    assert push._category("login", {"login_user_id": 3}) == push.CATEGORY_LOGIN
    assert push._category("login", {}) == ""
    assert push._category("data_source_down", {"rec_key": "k", "answerable": True}) == push.CATEGORY_CONNECTION
    assert push._category("connection_lost", {}) == push.CATEGORY_CONNECTION
    assert push._category("dsr", {"business_date": "2026-10-06"}) == push.CATEGORY_DSR
    # A reply that already went out is news — no Reply on it.
    assert push._category("resp_approved", {"review_id": 4}) == ""


def test_the_app_registers_every_category_the_server_sends():
    src = open(os.path.join(ROOT, "ios/CavnarAI/CavnarAI/Push/PushManager.swift"), encoding="utf-8").read()
    cats = [v for k, v in vars(push).items() if k.startswith("CATEGORY_")]
    assert len(cats) >= 13
    for cat in cats:
        assert f'"{cat}"' in src, cat


def test_the_content_extension_is_for_the_draft_and_the_report():
    yml = open(os.path.join(ROOT, "ios/CavnarAI/project.yml"), encoding="utf-8").read()
    ext = yml[yml.index("  CavnarNotificationContent:"):]
    assert "com.apple.usernotifications.content-extension" in ext
    assert push.CATEGORY_REVIEW_DRAFTED in ext and push.CATEGORY_DSR in ext


# ── #36: the draft rides the push ────────────────────────────────────────────

def test_an_approve_and_post_push_carries_the_whole_reply(db_path, monkeypatch):
    got = _capture(monkeypatch)
    rid = _rid(db_path)
    rev = _review(db_path, rid, "Thank you, Ann — see you soon!")
    push.fire_push(rid, "no_response", "t", "b", data={"review_id": rev}, db_path=db_path)
    data = got[0][1]
    assert data["draft_ready"] is True
    assert data["draft"] == "Thank you, Ann — see you soon!" and data["draft_complete"] is True


def test_a_long_reply_is_clipped_and_says_so(db_path, monkeypatch):
    got = _capture(monkeypatch)
    rid = _rid(db_path)
    long = "Thanks for the kind words. " * 80
    rev = _review(db_path, rid, long)
    push.fire_push(rid, "no_response", "t", "b", data={"review_id": rev}, db_path=db_path)
    data = got[0][1]
    assert data["draft_complete"] is False
    assert len(data["draft"]) == push.PUSH_DRAFT_MAX_CHARS and data["draft"].endswith("…")


def test_a_reply_that_must_be_read_first_carries_no_draft(db_path, monkeypatch):
    got = _capture(monkeypatch)
    rid = _rid(db_path)
    push.fire_push(rid, "1star", "t", "b", data={"review_id": 999}, db_path=db_path)
    assert got[0][1]["draft_ready"] is False and "draft" not in got[0][1]


def test_the_payload_always_fits_apns(db_path):
    # Non-ASCII is escaped (\\uXXXX, six bytes a character): the worst case.
    payload = {"aps": {"alert": {"title": "t" * 100, "body": "b" * 200}},
               "cavnar": {"alert_type": "no_response", "nav": "review/4",
                          "draft": "é" * push.PUSH_DRAFT_MAX_CHARS, "draft_complete": True}}
    out = push._fit_payload(payload)
    assert len(out) <= push.APNS_MAX_PAYLOAD_BYTES
    cav = json.loads(out)["cavnar"]
    assert cav["draft_complete"] is False and cav["draft"].endswith("…") and len(cav["draft"]) > 40
    # Nothing else is touched.
    assert json.loads(out)["aps"]["alert"]["body"] == "b" * 200
    # A payload already too big without the draft loses the draft entirely.
    huge = {"aps": {"alert": {"body": "x" * 4090}}, "cavnar": {"draft": "hello " * 50, "draft_complete": True}}
    push._fit_payload(huge)
    assert "draft" not in huge["cavnar"] and "draft_complete" not in huge["cavnar"]
    # A small one is untouched.
    small = {"aps": {}, "cavnar": {"draft": "Thanks!", "draft_complete": True}}
    push._fit_payload(small)
    assert small["cavnar"] == {"draft": "Thanks!", "draft_complete": True}


# ── #55: Send now on a held week ─────────────────────────────────────────────

def test_send_now_acknowledges_every_blocker_the_push_could_name():
    out = {"blocker_items": [{"key": "a", "text": "A"}, {"key": "b", "text": "B"}], "new_blockers": ["B"]}
    assert delayed._send_now_keys({"acknowledge": ["a"]}, out) == ["a", "b"]
    many = {"blocker_items": [{"key": k, "text": k} for k in "abcd"], "new_blockers": list("abcd")}
    assert delayed._send_now_keys({}, many) is None           # the push names three of four
    assert delayed._send_now_keys({}, {"new_blockers": ["x"]}) is None


def test_send_now_is_never_offered_over_a_hard_rule():
    """Re-audit 10/8/26: no manager on the floor (or any hard rule) is read
    in the week, never acknowledged from the lock screen."""
    out = {"blocker_items": [{"key": "rule:no_manager:2026-10-09:16:00", "text": "No manager Friday 4pm"}],
           "new_blockers": ["No manager Friday 4pm"]}
    assert delayed._send_now_keys({}, out) is None
    soft = {"blocker_items": [{"key": "notice:short", "text": "Inside the notice window"}],
            "new_blockers": ["Inside the notice window"]}
    assert delayed._send_now_keys({}, soft) == ["notice:short"]


def test_the_held_week_push_carries_its_keys(monkeypatch):
    import strategy_jobs
    told = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda rid, t, title, body, data, db, **k: told.append(data))
    delayed._tell_owner_schedule_held(1, {"schedule_id": 9}, ["No manager Friday"], "x", keys=["nm"])
    delayed._tell_owner_schedule_held(1, {"schedule_id": 9}, ["No manager Friday"], "x")
    assert told[0] == {"schedule_id": 9, "blocker_keys": ["nm"]}
    assert told[1] == {"schedule_id": 9}


# ── #55: This wasn't me ──────────────────────────────────────────────────────

def test_the_sign_in_push_names_its_login(db_path, monkeypatch):
    import emails
    import notify
    import ops
    got = []
    monkeypatch.setattr(emails, "send_login_notification", lambda *a, **k: None)
    monkeypatch.setattr(ops, "claim_cooldown", lambda *a, **k: True)
    monkeypatch.setattr(push, "fire_push", lambda rid, t, title, body, data=None, **k: got.append(data))
    monkeypatch.setattr(notify, "_log_alert", lambda *a, **k: None)
    notify.send_login_alert(1, "Co", "o@x.test", "1.2.3.4", "ua", user_id=7)
    assert got[0]["login_user_id"] == 7
    assert push._category("login", got[0]) == push.CATEGORY_LOGIN


@pytest.fixture
def mobile(db_path):
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    return app.test_client()


def _signed_in(db_path, rid, name="ann"):
    uid = create_user(rid, name, f"{name}@x.test", "correct-horse-battery-9", db_path=db_path)
    token = create_session(uid, device_type="ios", db_path=db_path)
    return uid, {"Authorization": f"Bearer {token}"}


def test_this_wasnt_me_signs_the_login_out_everywhere(db_path, mobile, monkeypatch):
    sent = []
    monkeypatch.setattr(auth_routes, "not_me_aftercare", lambda user: sent.append(user["id"]) or "ann@x.test")
    rid = _rid(db_path)
    uid, h = _signed_in(db_path, rid)
    _, h_web = uid, {"Authorization": "Bearer " + create_session(uid, db_path=db_path)}
    report = auth.create_login_report(uid, None, db_path=db_path)
    # A passkey added from the compromised session (re-audit 10/8/26).
    conn = get_conn(db_path)
    conn.execute("INSERT INTO user_passkeys (user_id, credential_id, public_key) VALUES (?, 'cred-x', x'00')", (uid,))
    conn.commit()
    conn.close()
    r = mobile.post("/mobile/api/account/not-me", json={"login_user_id": uid}, headers=h)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["signed_out"] is True and "ann@x.test" in r.get_json()["message"]
    assert sent == [uid]
    conn = get_conn(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (uid,)).fetchone()[0] == 0
        assert conn.execute("SELECT must_reset_password FROM users WHERE id=?", (uid,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM user_passkeys WHERE user_id=?", (uid,)).fetchone()[0] == 0
    finally:
        conn.close()
    # Every session is gone, this phone's and the other one's.
    assert mobile.post("/mobile/api/account/not-me", json={"login_user_id": uid}, headers=h_web).status_code == 401
    # The email's link is spent with it: one report.
    assert auth.consume_login_report(report, db_path=db_path) is None


def test_this_wasnt_me_only_answers_its_own_sign_in(db_path, mobile, monkeypatch):
    monkeypatch.setattr(auth_routes, "not_me_aftercare", lambda user: pytest.fail("nothing to send"))
    rid = _rid(db_path)
    uid, h = _signed_in(db_path, rid)
    for body in ({"login_user_id": uid + 50}, {}, {"login_user_id": "x"}):
        r = mobile.post("/mobile/api/account/not-me", json=body, headers=h)
        assert r.status_code == 409, body
    conn = get_conn(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (uid,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_the_email_link_and_the_push_share_one_revoke():
    src = open(os.path.join(ROOT, "auth.py"), encoding="utf-8").read()
    consume = src[src.index("def consume_login_report"):src.index("def report_not_me")]
    report = src[src.index("def report_not_me"):src.index("def _sign_out_everywhere")]
    assert "_sign_out_everywhere(conn" in consume and "_sign_out_everywhere(conn" in report
    routes = open(os.path.join(ROOT, "auth_routes.py"), encoding="utf-8").read()
    page = routes[routes.index("def login_not_me"):routes.index("def _csrf_json_ok")]
    assert "not_me_aftercare(user)" in page


# ── #1: links ────────────────────────────────────────────────────────────────

def test_the_issue_fallback_email_opens_the_issue():
    import issues
    url = issues._issue_url(12, 3)
    assert url.endswith("/?nav=issue/12&loc=3")
    src = open(os.path.join(ROOT, "issues.py"), encoding="utf-8").read()
    assert "/?tab=home" not in src


def test_a_link_open_from_the_phone_is_recorded_once(db_path, mobile, monkeypatch):
    import rec_delivery
    seen = []
    monkeypatch.setattr(rec_delivery, "record_link_open",
                        lambda user, args: seen.append((user["id"], dict(args))) or True)
    rid = _rid(db_path)
    uid, h = _signed_in(db_path, rid)
    r = mobile.post("/mobile/api/recs/link-open", json={"rec": "labor_over:x", "src": "alert_email", "rid": rid},
                    headers=h)
    assert r.status_code == 200 and r.get_json() == {"ok": True, "recorded": True}
    assert seen == [(uid, {"rec": "labor_over:x", "src": "alert_email", "rid": str(rid)})]
    assert mobile.post("/mobile/api/recs/link-open", json={"src": "x"}, headers=h).status_code == 400
    assert mobile.post("/mobile/api/recs/link-open", json={"rec": "k"}).status_code == 401
