"""Push payloads name where they open and what can be done from them
(friction audit #3 / #22, 9/25/26).

Every push carries a `nav` path (nav.py) so the app opens the review, the
pending send or the request — not a module's top — and the actionable kinds
carry a category whose buttons act without opening the app: Approve & post
on a reply that may be published unread, Undo on a queued automatic send,
Approve / Deny on a staff request. Nothing is sent: delivery is stubbed.
"""
import json
import os

import pytest

import models
import push
from models import Restaurant, create_restaurant, get_conn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _world(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(push, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(push, "DB_PATH", db_path)
    push.init_push(db_path=db_path)


def _rid(db_path):
    return create_restaurant(Restaurant(name="Nav Co", owner_email="o@x.test"), db_path=db_path)


def _review(db_path, rid, *, status="drafted", draft="Thanks so much!", flagged=0, urgency="normal", days=1):
    conn = get_conn(db_path)
    cur = conn.execute(
        """INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text,
           review_date, fetched_at, sentiment, urgency, processed, response_status,
           draft_response, draft_needs_review)
           VALUES (?,'google',?,'Ann',5,'Lovely',datetime('now', ?),datetime('now', ?),
                   'positive',?,1,?,?,?)""",
        (rid, f"ext-{status}-{flagged}-{urgency}-{days}", f"-{days} days", f"-{days} days",
         urgency, status, draft, flagged))
    conn.commit()
    rid_ = cur.lastrowid
    conn.close()
    return rid_


def _capture(monkeypatch):
    got = []

    class _Now:
        def submit(self, fn, token_row, alert_type, title, body, data, db, *rest):
            got.append((alert_type, data))
    monkeypatch.setattr(push, "_push_executor", lambda: _Now())
    monkeypatch.setattr(push, "get_device_tokens", lambda *a, **k: [{"user_id": 1, "restaurant_id": 1}])
    return got


# ── nav: the item, else the section, else the module ───────────────────────

@pytest.mark.parametrize("alert_type,data,expected", [
    ("1star", {"review_id": 412}, "review/412"),
    ("schedule_publish_pending", {"delayed_action_id": 9}, "action/9"),
    ("order_send_pending", {"delayed_action_id": 7}, "action/7"),
    ("shift_request", {"request_id": 5, "request_kind": "shift"}, "request/shift-5"),
    ("shift_request", {}, "labor/requests"),
    ("schedule_publish_held", {"schedule_id": 88}, "schedule/88"),
    ("schedule_drafted", {}, "labor/schedule"),
    ("critical_low", {}, "inventory/order"),
    ("dsr", {"business_date": "2026-09-24"}, "dsr/night/2026-09-24"),
    ("dsr", {}, "dsr"),
    ("morning_brief", {"ask_prompt": "Why was Friday slow?"}, "ask"),
    ("competitor_move", {}, "intel"),
    ("labor_over", {}, "labor"),
    ("login", {}, "account/security"),
])
def test_nav_names_the_thing_the_notification_is_about(alert_type, data, expected):
    assert push.nav_for(alert_type, data) == expected


def test_a_callers_own_nav_wins():
    assert push.nav_for("1star", {"review_id": 3, "nav": "reviews?filter=urgent"}) == "reviews?filter=urgent"


def test_every_push_carries_a_nav(db_path, monkeypatch):
    got = _capture(monkeypatch)
    rid = _rid(db_path)
    push.fire_push(rid, "schedule_publish_pending", "t", "b", data={"delayed_action_id": 4}, db_path=db_path)
    push.fire_push(rid, "price_spike", "t", "b", db_path=db_path)
    assert got[0][1]["nav"] == "action/4"
    assert got[1][1]["nav"] == "inventory"


# ── categories ─────────────────────────────────────────────────────────────

def test_a_publishable_draft_gets_approve_from_the_lock_screen(db_path, monkeypatch):
    got = _capture(monkeypatch)
    rid = _rid(db_path)
    ready = _review(db_path, rid)
    push.fire_push(rid, "no_response", "t", "b", data={"review_id": ready}, db_path=db_path)
    data = got[0][1]
    assert data["draft_ready"] is True
    assert push._category("no_response", data) == push.CATEGORY_REVIEW_DRAFTED


@pytest.mark.parametrize("kw", [
    {"status": "pending", "draft": None},       # nothing drafted yet (the usual new-review alert)
    {"flagged": 1},                             # read-first: the draft states something unverified
    {"urgency": "high"},                        # an urgent review is never published unread
    {"status": "posted"},                       # already out
])
def test_a_draft_that_must_be_read_first_only_opens(db_path, monkeypatch, kw):
    got = _capture(monkeypatch)
    rid = _rid(db_path)
    rev = _review(db_path, rid, **kw)
    push.fire_push(rid, "1star", "t", "b", data={"review_id": rev}, db_path=db_path)
    data = got[0][1]
    assert data["draft_ready"] is False
    assert push._category("1star", data) == push.CATEGORY_REVIEW


def test_another_restaurants_review_is_never_draft_ready(db_path):
    a = _rid(db_path)
    b = create_restaurant(Restaurant(name="Other", owner_email="x@x.test"), db_path=db_path)
    rev = _review(db_path, b)
    assert push._review_draft_ready(a, rev, db_path) is False
    assert push._review_draft_ready(b, rev, db_path) is True


def test_a_queued_send_gets_undo_and_a_request_gets_approve_deny():
    assert push._category("schedule_publish_pending", {"delayed_action_id": 3}) == push.CATEGORY_UNDOABLE
    assert push._category("order_send_pending", {"delayed_action_id": 3}) == push.CATEGORY_UNDOABLE
    # Without the action id there is nothing to undo.
    assert push._category("schedule_publish_pending", {}) == ""
    assert push._category("shift_request", {"request_id": 5, "request_kind": "shift"}) == push.CATEGORY_REQUEST
    assert push._category("shift_request", {"tab": "labor"}) == ""


def test_the_category_reaches_the_aps_payload(db_path, monkeypatch):
    rid = _rid(db_path)
    calls = []

    class _Resp:
        status_code = 200

        def json(self):
            return {}

    class _Client:
        def post(self, url, content=None, headers=None):
            calls.append(json.loads(content))
            return _Resp()
    monkeypatch.setattr(push, "_client", lambda: _Client())
    monkeypatch.setattr(push, "_provider_jwt", lambda: "jwt")
    row = {"restaurant_id": rid, "environment": "sandbox", "apns_token": "t" * 64, "user_id": 1}
    push._deliver(row, "order_send_pending", "t", "b",
                  {"delayed_action_id": 11, "nav": "action/11"}, db_path=db_path)
    assert calls[0]["aps"]["category"] == push.CATEGORY_UNDOABLE
    assert calls[0]["cavnar"]["nav"] == "action/11"


def test_a_staff_request_push_carries_its_id(monkeypatch):
    import shift_requests
    import strategy_jobs
    told = []
    monkeypatch.setattr(strategy_jobs, "_reach",
                        lambda rid, t, title, body, data, db, **k: told.append((t, data)))
    monkeypatch.setattr(shift_requests, "_email_staff", lambda *a, **k: 0)
    shift_requests._notify(1, "drop_asked", {"id": 42, "employee_name": "Dana", "date": "2026-09-26",
                                             "shift_start": "10:00", "shift_end": "16:00"})
    assert told and told[0][0] == "shift_request"
    assert told[0][1]["request_id"] == 42 and told[0][1]["request_kind"] == "shift"


# ── the app registers exactly the categories the server sends ──────────────

def test_the_app_registers_every_category_the_server_sends():
    src = open(os.path.join(ROOT, "ios/CavnarAI/CavnarAI/Push/PushManager.swift"), encoding="utf-8").read()
    for cat in (push.CATEGORY_REVIEW, push.CATEGORY_BRIEF, push.CATEGORY_ISSUE,
                push.CATEGORY_REVIEW_DRAFTED, push.CATEGORY_UNDOABLE, push.CATEGORY_REQUEST):
        assert f'"{cat}"' in src, cat
    # The background actions sit behind the phone's own unlock.
    assert ".authenticationRequired" in src


def test_mobile_home_attention_items_carry_a_nav():
    src = open(os.path.join(ROOT, "mobile_api.py"), encoding="utf-8").read()
    assert '"nav": a.get("nav")' in src
    assert '"nav": "reviews?filter=urgent"' in src
