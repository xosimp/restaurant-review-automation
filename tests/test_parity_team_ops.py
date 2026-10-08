"""Web vs iOS parity audit (10/7/26), owner-side team operations — the
backend half:

  #26  a "lineup brief waiting" push when the pre-shift nudge drafts the
       brief, to the logins who approve it, with Approve on it
       (push.CATEGORY_LINEUP); a manager who pressed "Draft it for me" is
       not pushed about their own draft
  #71  a teammate's direct message reaches the recipient's phone
       (models.send_team_message), whichever platform sent it; its typed
       lock-screen Reply answers the sender (push.CATEGORY_MESSAGE)
  #10  an employee's message to the managers carries the same typed Reply
       (employee_message + thread_id)
  #67  the staff pulse on the web: the close-out line and the 14-day tile
  /s/<token>  the availability link shows the published shifts its save
       rules out (conflicts_text), as the app does

Every send is stubbed: push.fire_push and strategy_jobs._reach record their
calls, and the conftest blocks the network.
"""
import os
import re
from datetime import date

import pytest
from flask import Flask

import auth
import models
import push
from auth import create_session, create_user, init_auth, set_user_role
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def redirected(path=None, *a, **k):
        return real(db_path)
    import client_api
    import mobile_api
    import strategy_jobs
    import time_off
    import staff_settings
    for mod in (models, auth, client_api, mobile_api, strategy_jobs, time_off, staff_settings):
        monkeypatch.setattr(mod, "get_conn", redirected, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    return db_path


@pytest.fixture
def pushes(monkeypatch):
    sent = []

    def fake_fire(restaurant_id, alert_type, title, body, data=None, db_path=None, user_ids=None, **kw):
        sent.append({"rid": restaurant_id, "type": alert_type, "title": title, "body": body,
                     "data": dict(data or {}), "user_ids": user_ids})
        return 1
    monkeypatch.setattr(push, "fire_push", fake_fire)
    return sent


def _restaurant(db_path, name="Maple & Rye"):
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


def _login(rid, username, role=None):
    uid = create_user(rid, username, f"{username}@x.test", "unused", generated=True)
    if role:
        set_user_role(uid, role)
    return uid


# ── the new types are mapped everywhere a type must be ──────────────────────

NEW_TYPES = ("lineup_brief_waiting", "team_message")


def test_every_new_type_is_mapped_everywhere():
    import client_api
    import nav
    import notify
    ios = _read("ios", "CavnarAI", "CavnarAI", "Push", "DeepLinkRouter.swift")
    for t in NEW_TYPES:
        assert t in client_api._NOTIFICATION_LABELS, t
        assert t in push.NOTIFICATION_MODULE, t
        assert push.priority_of(t) == push.P2_OPPORTUNITY, t
        assert t in models.NON_ALERT_TYPES, t
        assert t in nav._ALERT_NAV, t
        assert f'"{t}"' in ios, t
    assert "lineup_brief_waiting" in notify.BRIEFING_ALWAYS, "a task before service is never held by the budget"
    assert "lineup_brief_waiting" in push.ACTIONABLE_TYPES
    assert "team_message" in push._UNCOLLAPSIBLE_TYPES, "a second message must not replace the first"


def test_the_message_and_lineup_categories_and_where_they_open():
    assert push._category("employee_message", {"thread_id": 7}) == push.CATEGORY_MESSAGE
    assert push._category("employee_message", {}) == "", "no thread, nothing to reply to"
    assert push._category("team_message", {"sender_id": 3}) == push.CATEGORY_MESSAGE
    # Approve only when the push carries the whole draft and its revision
    # (re-audit 10/8/26); anything less is Open only.
    assert push._category("lineup_brief_waiting", {"day": "2026-10-02", "draft": "Big night.",
                                                   "draft_complete": True, "brief_rev": "r1"}) == push.CATEGORY_LINEUP
    assert push._category("lineup_brief_waiting", {"day": "2026-10-02"}) == push.CATEGORY_LINEUP_REVIEW
    assert push._category("lineup_brief_waiting", {}) == ""
    assert push.nav_for("team_message", {"sender_id": 3}) == "messages/3"
    assert push.nav_for("lineup_brief_waiting", {"day": "2026-10-02"}) == "labor/lineup"
    # The categories the app registers (PushManager.swift) carry the same ids.
    swift = _read("ios", "CavnarAI", "CavnarAI", "Push", "PushManager.swift")
    for cat in (push.CATEGORY_MESSAGE, push.CATEGORY_LINEUP, push.CATEGORY_LINEUP_REVIEW):
        assert f'"{cat}"' in swift, cat
    assert "UNTextInputNotificationAction" in swift, "Reply is typed in place on the lock screen"


# ── #26: the lineup brief waiting push ──────────────────────────────────────

DAY = date(2026, 10, 2)
ITEMS = [
    {"kind": "volume", "text": "Expect a busy Friday — typically about 25% busier than an average day."},
    {"kind": "rush", "text": "Busiest around 6–8pm on a usual Friday."},
]


@pytest.fixture
def brief(db, monkeypatch):
    """staff_brief.draft with its model, readiness and lineup lines stubbed,
    and _reach recorded."""
    import ai_utils
    import data_health
    import preshift
    import staff_roster
    import strategy_jobs
    from types import SimpleNamespace
    preshift.invalidate()
    monkeypatch.setattr(preshift, "build", lambda rid, day=None, db_path=None: {
        "day": (day or DAY).isoformat(), "weekday": "Friday", "items": list(ITEMS)})
    monkeypatch.setattr(preshift, "business_day", lambda rid: DAY)
    monkeypatch.setattr(staff_roster, "roster_names_for_restaurant", lambda rid, db_path=None: [])
    monkeypatch.setattr(data_health, "readiness", lambda *a, **k: {"decision": "proceed", "reason": "",
                                                                   "prompt_block": "", "data_state": {}})
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda client, **kw: SimpleNamespace(
        content=[SimpleNamespace(type="text", text="Busy Friday tonight, with the rush around 6–8pm.")],
        stop_reason="end_turn", _cavnar_call_id=None))
    reached = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: reached.append((a, k)) or 1)
    yield reached
    preshift.invalidate()


def test_the_nudges_draft_tells_the_approvers_once(brief, db):
    import staff_brief
    from permissions import SCHEDULE_PUBLISH
    rid = _restaurant(db)
    row = staff_brief.draft(rid, day=DAY)
    assert row["draft_status"] == "drafted"
    assert len(brief) == 1
    args, kw = brief[0]
    assert args[0] == rid and args[1] == "lineup_brief_waiting"
    assert args[4]["day"] == "2026-10-02", "Approve posts the day it names"
    assert "10/2/26" in args[3] and "2026-10-02" not in args[3], "the body reads M/D/YY"
    assert kw["permissions"] == [SCHEDULE_PUBLISH] and kw["deciders"] is True
    # A second call returns the stored draft: no second model call, no second push.
    staff_brief.draft(rid, day=DAY)
    assert len(brief) == 1


def test_a_draft_already_approved_or_asked_for_is_not_announced(brief, db):
    import staff_brief
    rid = _restaurant(db)
    staff_brief.draft(rid, day=DAY, announce=False)
    assert brief == [], "the manager who pressed Draft it for me is reading it"
    rid2 = _restaurant(db, "Second")
    owner = _login(rid2, "erik2")
    staff_brief.approve(rid2, {"id": owner, "role": "client"}, day=DAY, text="Big night, team.")
    staff_brief.draft(rid2, day=DAY)
    assert brief == [], "an approved brief waits on nobody"


def test_the_draft_route_does_not_push_the_manager_who_asked(brief, db):
    import staff_knowledge_routes as skr
    app = Flask(__name__)
    app.register_blueprint(skr.knowledge_mobile_bp)
    client = app.test_client()
    rid = _restaurant(db)
    manager = _login(rid, "mgr", role="manager")
    h = {"Authorization": f"Bearer {create_session(manager)}"}
    r = client.post("/mobile/api/staff-brief/draft", json={"day": "2026-10-02"}, headers=h)
    assert r.status_code == 200 and r.get_json()["brief"]["draft_status"] == "drafted"
    assert brief == []
    # Re-audit 10/8/26: an approve that carries no text (the push's blind
    # button) is refused — staff read only words the approver read.
    r = client.post("/mobile/api/staff-brief/approve", json={"day": "2026-10-02"}, headers=h)
    assert r.status_code == 400 and "read it" in r.get_json()["error"]
    draft = client.get("/mobile/api/staff-brief?day=2026-10-02", headers=h).get_json()["brief"]["draft_text"]
    r = client.post("/mobile/api/staff-brief/approve", json={"day": "2026-10-02", "text": draft}, headers=h)
    assert r.status_code == 200 and r.get_json()["brief"]["approved_text"].startswith("Busy Friday")


def test_the_waiting_notice_never_raises(db, monkeypatch):
    import staff_brief
    import strategy_jobs

    def boom(*a, **k):
        raise RuntimeError("push down")
    monkeypatch.setattr(strategy_jobs, "_reach", boom)
    assert staff_brief.announce_waiting(_restaurant(db), DAY) == 0


# ── #71: a direct message pushes the recipient ──────────────────────────────

def test_a_direct_message_pushes_only_the_recipient_with_the_sender_to_reply_to(db, pushes):
    rid = _restaurant(db)
    owner = _login(rid, "erik")
    manager = _login(rid, "jheflin", role="manager")
    auth.upsert_membership(owner, rid, "client", employee_name="Erik Lund")
    msg = models.send_team_message(rid, owner, manager, "Can you close tonight?")
    assert msg["body"] == "Can you close tonight?"
    assert len(pushes) == 1
    p = pushes[0]
    assert p["type"] == "team_message" and p["user_ids"] == [manager]
    assert p["title"] == "Erik Lund", "the person's name, not the login's"
    assert p["body"] == "Can you close tonight?"
    assert p["data"]["sender_id"] == owner and p["data"]["message_id"] == msg["id"]


def test_a_login_without_the_inbox_is_not_pushed_and_a_failed_push_keeps_the_message(db, pushes, monkeypatch):
    rid = _restaurant(db)
    owner = _login(rid, "erik")
    member = _login(rid, "teammate", role="member")
    models.send_team_message(rid, owner, member, "hello")
    assert pushes == [], "a member cannot open Messages: a push would be a dead end"

    def boom(*a, **k):
        raise RuntimeError("apns down")
    monkeypatch.setattr(push, "fire_push", boom)
    manager = _login(rid, "mgr", role="manager")
    assert models.send_team_message(rid, owner, manager, "still saved")["body"] == "still saved"


def test_the_web_and_mobile_send_both_push(db, pushes):
    import client_api
    import mobile_api
    app = Flask(__name__)
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    client = app.test_client()
    rid = _restaurant(db)
    owner = _login(rid, "erik")
    manager = _login(rid, "mgr", role="manager")
    h = {"Authorization": f"Bearer {create_session(owner)}"}
    r = client.post("/mobile/api/team/messages", json={"recipient_id": manager, "body": "from the phone"}, headers=h)
    assert r.status_code == 200 and r.get_json()["ok"]
    client.set_cookie("session_token", create_session(manager))
    client.set_cookie("csrf_js", "t")
    r = client.post("/api/team/messages", json={"recipient_id": owner, "body": "from the web"},
                    headers={"X-CSRF-Token": "t"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert [(p["user_ids"], p["body"]) for p in pushes] == [([manager], "from the phone"), ([owner], "from the web")]


# ── #67: the staff pulse on the web ─────────────────────────────────────────

def test_the_web_shows_the_pulse_on_the_close_out_and_as_a_14_day_tile():
    page = _read("templates", "dashboard.html")
    i = page.index("function renderCloseout(")
    assert "c.staff_pulse" in page[i:i + 4000], "the close-out shows tonight's pulse line"
    assert 'id="lb2-pulse"' in page
    assert "/api/labor/staff-pulse?days=14" in page
    j = page.index("function renderPulse(")
    body = page[j:j + 3000]
    assert "d.enough" in body and "d.message" in body, "below the floor: the count sentence, nothing else"


# ── /s/<token>: the link shows the conflicts its save left ──────────────────

HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


def _publish(db_path, rid, rows, week_start="2026-10-05", week_end="2026-10-11"):
    conn = models.get_conn(db_path)
    try:
        models._ensure_history_columns(conn)
        cur = conn.execute("INSERT INTO schedule_history (restaurant_id, generated_at, week_start, week_end, "
                           "schedule_csv, published_at) VALUES (?, datetime('now'), ?, ?, ?, datetime('now'))",
                           (rid, week_start, week_end, HEAD + "\n".join(rows)))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_own_conflicts_reads_the_published_shifts_the_stored_availability_rules_out(db, monkeypatch):
    import staff_settings as ss
    import strategy_jobs
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: 1)
    rid = _restaurant(db)
    _publish(db, rid, ["2026-10-09,Friday,Ben,Server,5:00pm,11:00pm,6,",
                       "2026-10-06,Tuesday,Ben,Server,11:00am,4:00pm,5,"])
    today = date(2026, 10, 2)
    exp = ss.own_availability(rid, "Ben", db_path=db, today=today)["updated_at"]
    res = ss.save_own_availability(rid, "Ben", exp, unavailable_days=["Friday"], db_path=db, today=today)
    assert res["ok"]
    got = ss.own_conflicts(rid, "Ben", db_path=db, today=today)
    assert [(c["date"], c["shift_start"]) for c in got] == [("2026-10-09", "5:00pm")]
    assert ss.conflicts_text(got) == res["conflicts_text"]


def test_the_link_page_shows_the_conflicts_after_a_save(db, monkeypatch):
    import client_api
    import staff_settings as ss
    from staff_routes import staff_bp
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.register_blueprint(staff_bp)
    app.register_blueprint(client_api.client_bp)
    client = app.test_client()
    rid = _restaurant(db)
    sid = _publish(db, rid, ["2026-10-09,Friday,Ben,Server,5:00pm,11:00pm,6,"])
    token = models.create_schedule_share(rid, sid, "Ben", db_path=db)
    monkeypatch.setattr(ss, "own_conflicts", lambda rid_, name, **k: [
        {"date": "2026-10-09", "day": "Friday", "shift_start": "5:00pm", "shift_end": "11:00pm", "role": "Server",
         "reason": "you marked Fridays unavailable"}] if name == "Ben" else [])
    page = client.get(f"/s/{token}?saved=1").get_data(as_text=True)
    m = re.search(r'class="conflicts"[^>]*>([^<]*)<', page)
    assert m and "Fri 10/9/26" in m.group(1) and "ask to drop it" in m.group(1), page[-2000:]
    assert 'class="conflicts"' not in client.get(f"/s/{token}").get_data(as_text=True), \
        "only after a save"
