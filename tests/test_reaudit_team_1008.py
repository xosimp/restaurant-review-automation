"""Blind re-audit 10/8/26 — team operations and the staff browser portal.

What this holds:
  * the lineup brief's waiting push carries the draft and its revision; its
    lock-screen Approve approves exactly those words and only while the
    brief is unchanged (staff_brief.brief_rev / expected_rev) — a brief
    approved, withdrawn or rewritten since is refused, never overwritten;
    a push without the whole draft is Open only (CATEGORY_LINEUP_REVIEW)
  * the browser portal: forgot PIN, the fallback email, delete my login,
    switch location, hours/tips/stats/recognition, and one inbox count
  * the pre-session staff routes act only on a JSON body (the real CSRF
    guard), and a POST from another site's page is refused (Origin)

Every send is stubbed: the conftest blocks the network; pushes are captured.
"""
import json
import os
import re

import pytest
from flask import Flask

import auth
import models
import push
# `brief` stubs the draft's model and records _reach; it runs on this
# module's `db` (the staff portal's, which redirects every bound get_conn).
from tests.test_parity_team_ops import DAY, _login, brief  # noqa: F401
from tests.test_parity_team_ops import _restaurant as _team_restaurant
from tests.test_staff_web_portal import (CSRF, _app, _member, _post, _restaurant, _web, db, told)  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


# ── the lineup brief's lock-screen Approve ──────────────────────────────────

def _brief_client(db):
    import staff_knowledge_routes as skr
    app = Flask(__name__)
    app.register_blueprint(skr.knowledge_mobile_bp)
    rid = _team_restaurant(db)
    manager = _login(rid, "mgr", role="manager")
    return app.test_client(), rid, {"Authorization": f"Bearer {auth.create_session(manager)}"}


def test_the_waiting_push_carries_the_draft_and_its_revision(brief, db):
    import staff_brief
    rid = _team_restaurant(db)
    row = staff_brief.draft(rid, day=DAY)
    data = brief[0][0][4]
    assert data["draft"] == row["draft_text"] and data["draft"].startswith("Busy Friday")
    assert data["brief_rev"] == staff_brief.brief_rev(staff_brief._row(rid, DAY))


def test_fire_push_marks_the_lineup_draft_whole_and_picks_approve(monkeypatch, db):
    got = []

    class _Now:
        def submit(self, fn, token_row, alert_type, title, body, data, db, *rest):
            got.append(data)
    monkeypatch.setattr(push, "_push_executor", lambda: _Now())
    monkeypatch.setattr(push, "get_device_tokens", lambda *a, **k: [{"user_id": 1, "restaurant_id": 1}])
    rid = _team_restaurant(db)
    push.fire_push(rid, "lineup_brief_waiting", "t", "b",
                   data={"day": "2026-10-02", "draft": "Busy Friday tonight.", "brief_rev": "r1"}, db_path=db)
    assert got[0]["draft"] == "Busy Friday tonight." and got[0]["draft_complete"] is True
    assert push._category("lineup_brief_waiting", got[0]) == push.CATEGORY_LINEUP
    assert got[0]["nav"] == "labor/lineup"


def test_a_lineup_draft_cut_to_fit_loses_approve():
    payload = {"aps": {"alert": {"title": "t", "body": "b" * 3700}, "category": push.CATEGORY_LINEUP},
               "cavnar": {"alert_type": "lineup_brief_waiting", "day": "2026-10-02", "brief_rev": "r1",
                          "draft": "Big night, team. " * 30, "draft_complete": True}}
    out = json.loads(push._fit_payload(payload))
    assert len(json.dumps(out, separators=(",", ":")).encode()) <= push.APNS_MAX_PAYLOAD_BYTES
    assert out["cavnar"].get("draft_complete") is not True
    assert out["aps"]["category"] == push.CATEGORY_LINEUP_REVIEW, "words nobody can read whole are never one tap"
    # A review push cut the same way keeps its own category (F_push #3 is the reviews round's).
    review = {"aps": {"alert": {"body": "b" * 3700}, "category": push.CATEGORY_REVIEW_DRAFTED},
              "cavnar": {"draft": "Thanks! " * 100, "draft_complete": True}}
    push._fit_payload(review)
    assert review["aps"]["category"] == push.CATEGORY_REVIEW_DRAFTED


def test_the_lock_screen_approve_publishes_only_the_unchanged_draft(brief, db):
    import staff_brief
    client, rid, h = _brief_client(db)
    staff_brief.draft(rid, day=DAY)
    pushed = brief[0][0][4]
    body = {"day": "2026-10-02", "text": pushed["draft"], "expected_rev": pushed["brief_rev"]}
    r = client.post("/mobile/api/staff-brief/approve", json=body, headers=h)
    assert r.status_code == 200 and r.get_json()["brief"]["approved_text"] == pushed["draft"]
    # The same press again (a second phone, a retry): the brief changed — refused.
    again = client.post("/mobile/api/staff-brief/approve", json=body, headers=h)
    assert again.status_code == 400 and "changed since" in again.get_json()["error"]


def test_a_stale_lock_screen_approve_never_overwrites_an_edited_brief(brief, db):
    import staff_brief
    client, rid, h = _brief_client(db)
    staff_brief.draft(rid, day=DAY)
    pushed = brief[0][0][4]
    edited = client.post("/mobile/api/staff-brief/approve",
                         json={"day": "2026-10-02", "text": "Big Friday — pace the rush."}, headers=h)
    assert edited.status_code == 200
    stale = client.post("/mobile/api/staff-brief/approve",
                        json={"day": "2026-10-02", "text": pushed["draft"], "expected_rev": pushed["brief_rev"]},
                        headers=h)
    assert stale.status_code == 400
    assert staff_brief._row(rid, DAY)["approved_text"] == "Big Friday — pace the rush.", "the manager's words stand"


def test_a_withdrawn_brief_is_not_revived_by_an_old_notification(brief, db, monkeypatch):
    import staff_brief
    client, rid, h = _brief_client(db)
    staff_brief.draft(rid, day=DAY)
    # The push went out at 2pm; the approve and withdraw come later (the
    # revision's clock is the row's updated_at, to the second).
    conn = models.get_conn(db)
    conn.execute("UPDATE staff_briefs SET updated_at='2026-10-02 14:00:00' WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    pushed = dict(brief[0][0][4], brief_rev=staff_brief.brief_rev(staff_brief._row(rid, DAY)))
    assert client.post("/mobile/api/staff-brief/approve",
                       json={"day": "2026-10-02", "text": "Own words."}, headers=h).status_code == 200
    assert client.post("/mobile/api/staff-brief/withdraw", json={"day": "2026-10-02"}, headers=h).status_code == 200
    stale = client.post("/mobile/api/staff-brief/approve",
                        json={"day": "2026-10-02", "text": pushed["draft"], "expected_rev": pushed["brief_rev"]},
                        headers=h)
    assert stale.status_code == 400 and staff_brief._row(rid, DAY)["approved_text"] is None


def test_the_app_and_the_extension_handle_both_lineup_categories():
    swift = _read("ios", "CavnarAI", "CavnarAI", "Push", "PushManager.swift")
    assert f'"{push.CATEGORY_LINEUP_REVIEW}"' in swift
    assert '"expected_rev"' in swift and '"text": .null' not in swift, "never a blind approve"
    yml = _read("ios", "CavnarAI", "project.yml")
    ext = yml[yml.index("  CavnarNotificationContent:"):]
    assert push.CATEGORY_LINEUP in ext and push.CATEGORY_LINEUP_REVIEW in ext


# ── the browser portal: forgot PIN ──────────────────────────────────────────

def test_forgot_pin_from_the_browser_signs_in_with_the_new_pin(db, told, monkeypatch):
    monkeypatch.setenv("STAFF_SIGNUP_DEV_CODE", "1")
    monkeypatch.setattr(auth, "_sms_configured", lambda: False)
    rid = _restaurant(db)
    _uid, mid = _member(db, rid, "Ana", pin="8317")
    models.set_staff_contact(rid, "Ana", None, "(555) 014-2233", db_path=db)
    code = auth.get_join_code(rid, db_path=db)
    c = _app().test_client()
    pad = c.get(f"/staff/r/{code}").get_data(as_text=True)
    assert "startForgot()" in pad and "Forgot your PIN?" in pad
    origin = {"Origin": "http://localhost"}
    start = c.post("/staff/api/pin/forgot/start", json={"join_code": code, "phone": "(555) 014-2233"}, headers=origin)
    assert start.status_code == 200 and start.get_json()["dev_code"]
    found = c.post("/staff/api/pin/forgot/verify", json={"phone": "5550142233", "code": start.get_json()["dev_code"]},
                   headers=origin).get_json()
    assert found["ok"] and found["employee_name"] == "Ana"
    done = c.post("/staff/api/pin/forgot/set", json={"reset_token": found["reset_token"], "pin": "4827"}, headers=origin)
    assert done.status_code == 200 and done.get_json()["redirect"] == "/staff/home"
    assert "token" not in done.get_json(), "a browser's session is the cookie only"
    assert c.get("/staff/home").status_code == 200
    assert auth.verify_membership_pin(mid, rid, "4827", db_path=db)["ok"] is True


def test_the_pin_pad_page_drives_the_forgot_routes_and_a_preselected_name():
    page = _read("templates", "staff_login.html")
    for route in ("/staff/api/pin/forgot/start", "/staff/api/pin/forgot/verify", "/staff/api/pin/forgot/set"):
        assert f"'{route}'" in page, route
    assert "join_code: fpCode()" in page and "reset_token: fpToken" in page
    assert "#m=" in page, "Switch location lands on that location's pad with the name chosen"


# ── the browser portal: Me and Today ────────────────────────────────────────

def test_the_fallback_email_saves_and_clears_from_the_browser(db, told):
    rid = _restaurant(db)
    uid, _ = _member(db, rid, "Ana")
    c = _web(_app(), db, rid, uid)
    assert c.post("/staff/api/me/email", json={"email": "ana@x.test"}).status_code == 403, "CSRF holds"
    r = _post(c, "/staff/api/me/email", {"email": "ana@x.test"})
    assert r.status_code == 200 and r.get_json()["email"] == "ana@x.test"
    assert c.get("/staff/api/me").get_json()["employee"]["email"] == "ana@x.test"
    assert _post(c, "/staff/api/me/email", {"email": ""}).get_json()["email"] == ""


def test_delete_my_login_and_switch_location_from_the_browser(db, told):
    rid = _restaurant(db)
    uid, mid = _member(db, rid, "Ana")
    other = _restaurant(db)
    m2 = auth.upsert_membership(uid, other, "employee", employee_name="Ana", db_path=db)
    auth.set_membership_pin(m2["id"], other, "8317", db_path=db)
    c = _web(_app(), db, rid, uid)
    locs = c.get("/staff/api/me").get_json()["employee"]["locations"]
    assert len(locs) == 2
    sw = _post(c, "/staff/api/switch", {"restaurant_id": other}).get_json()
    assert sw["ok"] and sw["requires_pin"] and sw["membership_id"] == m2["id"] and sw["portal_token"]
    pad = c.get(f"/staff/r/{sw['portal_token']}").get_data(as_text=True)
    assert 'data-id="%d"' % m2["id"] in pad
    assert _post(c, "/staff/api/account/delete", {}).status_code == 400, "confirm: true is required"
    gone = _post(c, "/staff/api/account/delete", {"confirm": True})
    assert gone.status_code == 200 and gone.get_json()["deleted"] is True


def test_the_portal_shows_my_figures_and_has_every_account_action():
    page = _read("templates", "staff_portal.html")
    for route in ("/staff/api/stats", "/staff/api/earnings?days=14", "/staff/api/recognition",
                  "/staff/api/me/email", "/staff/api/switch", "/staff/api/account/delete"):
        assert f"'{route}'" in page, route
    assert "function statsTiles(" in page and "function recognitionCard(" in page
    assert "function attendanceCard(" in page and "function accountCard(" in page
    # Delete my login asks first (tier 2), as every outward action does.
    i = page.index("'delete-account'")
    assert "askConfirm(" in page[i:i + 600] and "confirm: true" in page[i:i + 1200]


def test_the_inbox_badge_has_one_count():
    page = _read("templates", "staff_portal.html")
    assert page.count("setBadge('badge-inbox'") == 1, "only paintInboxBadge paints it"
    assert "function inboxCount(d)" in page
    body = page[page.index("function inboxCount(d)"):page.index("function paintInboxBadge")]
    assert "d.unread" in body and "d.unread_messages" in body


# ── the pre-session routes: JSON only, and never from another site ─────────

def _pad(c, code):
    return re.search(r'var NONCE = "([^"]+)"', c.get(f"/staff/r/{code}").get_data(as_text=True)).group(1)


def test_pin_sign_in_reads_only_a_json_body(db):
    """The real guard against a forged sign-in: a cross-site page can send a
    form or text/plain, never application/json without a preflight."""
    rid = _restaurant(db)
    _uid, mid = _member(db, rid, "Ana", pin="8317")
    code = auth.get_join_code(rid, db_path=db)
    c = _app().test_client()
    fields = {"membership_id": str(mid), "pin": "8317", "nonce": _pad(c, code)}
    form = c.post(f"/staff/r/{code}/login", data=fields)
    assert form.status_code == 400 and c.get_cookie("staff_session") is None
    plain = c.post(f"/staff/r/{code}/login", data=json.dumps({"membership_id": mid, "pin": "8317",
                                                             "nonce": _pad(c, code)}),
                   content_type="text/plain")
    assert plain.status_code == 400 and c.get_cookie("staff_session") is None
    ok = c.post(f"/staff/r/{code}/login", json={"membership_id": mid, "pin": "8317", "nonce": _pad(c, code)})
    assert ok.status_code == 200 and c.get_cookie("staff_session") is not None
    src = _read("staff_routes.py")
    fn = src[src.index("def portal_authenticate"):src.index("# ── Self-signup")]
    assert "request.get_json(silent=True)" in fn and "request.form" not in fn and "force=True" not in fn


def test_a_pre_session_post_from_another_site_is_refused(db):
    rid = _restaurant(db)
    _uid, mid = _member(db, rid, "Ana", pin="8317")
    code = auth.get_join_code(rid, db_path=db)
    c = _app().test_client()
    for origin in ("https://evil.example", "null"):
        r = c.post(f"/staff/r/{code}/login", json={"membership_id": mid, "pin": "8317", "nonce": _pad(c, code)},
                   headers={"Origin": origin})
        assert r.status_code == 403 and c.get_cookie("staff_session") is None, origin
        assert c.post("/staff/api/pin/forgot/start", json={"join_code": code, "phone": "5550142233"},
                      headers={"Origin": origin}).status_code == 403
    # This site's own page, and the app (no Origin at all), sign in.
    same = c.post(f"/staff/r/{code}/login", json={"membership_id": mid, "pin": "8317", "nonce": _pad(c, code)},
                  headers={"Origin": "http://localhost"})
    assert same.status_code == 200
    app_call = _app().test_client().post(f"/staff/r/{code}/login",
                                         json={"membership_id": mid, "pin": "8317", "nonce": _pad(c, code),
                                               "device_id": "dev-1"})
    assert app_call.status_code == 200 and app_call.get_json()["token"]


def test_the_configured_public_origin_is_this_site(db, monkeypatch):
    import config
    monkeypatch.setenv("BASE_URL", "https://app.cavnar.ai")
    rid = _restaurant(db)
    _uid, mid = _member(db, rid, "Ana", pin="8317")
    code = auth.get_join_code(rid, db_path=db)
    c = _app().test_client()
    r = c.post(f"/staff/r/{code}/login", json={"membership_id": mid, "pin": "8317", "nonce": _pad(c, code)},
               headers={"Origin": "https://app.cavnar.ai"})
    assert r.status_code == 200 and config.base_url() == "https://app.cavnar.ai"


def test_the_csrf_comment_names_the_real_guard():
    src = _read("staff_routes.py")
    block = src[src.index("# ── CSRF for the browser portal"):src.index("def staff_csrf_check")]
    assert "a same-origin read a cross-site page cannot make" not in block
    assert "application/json" in block and "Origin" in block
