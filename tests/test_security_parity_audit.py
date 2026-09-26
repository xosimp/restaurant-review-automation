"""Independent security/parity audit, server fixes (9/25/26).

Each test names the finding it pins and fails on the code before the fix:

 1. a loss issue is not resolved, reassigned or acted on by id by a login
    without LOSS_VIEW;
 2. alert settings, the digest day, sign-in alerts, the monthly review and
    marketing mail are the owner's, on web, mobile and through Ask;
 3. a remembered device belongs to the login that remembered it;
 4. Google Business and Instagram/Facebook connect and disconnect are
    owner-only, like every other connection;
 5. a queued supplier order's dollars are not shown to a login without food
    cost;
 6. generation is never a GET (schedule, content calendar);
 7. a goal a login may not see is not ended by id;
 8. a 2FA resend that did not go says so (502);
 9. an approved or posted reply is never marked skipped;
10. a bulk run that only held drafts still drops Home's cache;
11. the flagged-draft sentence reads cleanly whatever held the draft;
12. an answered recommendation starts no tracker on a replayed answer.

No model, network, email, SMS or push is reached.
"""
import inspect
import sys
import uuid
from datetime import datetime

import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models
import strategy_routes
from models import Restaurant, Review, create_restaurant, save_reviews


@pytest.fixture(autouse=True)
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    return db_path


def _rid(**cols):
    for m in ("module_labor", "module_inventory", "module_reviews", "module_marketing"):
        cols.setdefault(m, 1)
    return create_restaurant(Restaurant(name="Audit Grill", owner_email="owner@x.test", **cols))


def _owner(rid):
    return {"id": 1, "restaurant_id": rid, "role": "client", "is_admin": 0, "grants": ()}


def _manager(rid):
    return {"id": 2, "restaurant_id": rid, "role": "manager", "is_admin": 0, "grants": ()}


def _route(fn, user, body=None, args=(), query=None):
    with Flask(__name__).test_request_context(json=body or {}, query_string=query or {}):
        return fn(user, *args)


def _q(sql, args=()):
    c = models.get_conn()
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _x(sql, args=()):
    c = models.get_conn()
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _app():
    import auth_routes
    import social_routes
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "test"
    for bp in (mobile_api.mobile_bp, client_api.client_bp, strategy_routes.strategy_bp,
               strategy_routes.strategy_mobile_bp, auth_routes.auth_bp, social_routes.social_bp):
        app.register_blueprint(bp)
    return app


def _login(rid, name, role):
    uid = auth.create_user(rid, name, f"{name}@x.test", "pw-123456", db_path=models.DB_PATH)
    auth.upsert_membership(uid, rid, role, db_path=models.DB_PATH)
    _x("UPDATE users SET role=? WHERE id=?", (role, uid))
    return uid, {"Authorization": f"Bearer {auth.create_session(uid, db_path=models.DB_PATH)}"}


def _web_as(monkeypatch, rid, role):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 7 if role == "client" else 8, "restaurant_id": rid, "is_admin": 0,
                                 "role": role, "username": role, "email": f"{role}@x.test", "grants": ()})


# ── 1. loss issues ─────────────────────────────────────────────────────────

def test_1_a_manager_cannot_resolve_reassign_or_ask_cover_on_a_loss_issue_by_id():
    import issues
    rid = _rid()
    issue, _tok = issues.create_issue(rid, "loss", "Comps up 40% — approved by Jordan", notify=False)
    iid = issue["id"]
    out, st = _route(strategy_routes._do_issue_resolve, _manager(rid), args=(iid,))
    assert st == 404 and "issue" not in out
    out, st = _route(strategy_routes._do_issue_reassign, _manager(rid), {"contact_id": 1}, args=(iid,))
    assert st == 404 and "issue" not in out
    out, st = _route(strategy_routes._do_issue_ask_cover, _manager(rid), {"name": "Ana"}, args=(iid,))
    assert st == 404
    assert issues.get_issue(rid, iid)["status"] != "resolved"
    # A login with LOSS_VIEW (the owner) still can.
    out, st = _route(strategy_routes._do_issue_resolve, _owner(rid), args=(iid,))
    assert st == 200 and out["issue"]["status"] == "resolved"


def test_1_a_manager_still_resolves_an_ordinary_issue():
    import issues
    rid = _rid()
    issue, _tok = issues.create_issue(rid, "manual", "Walk-in door sticks", notify=False)
    out, st = _route(strategy_routes._do_issue_resolve, _manager(rid), args=(issue["id"],))
    assert st == 200 and out["issue"]["status"] == "resolved"


# ── 2. owner-only settings ─────────────────────────────────────────────────

def test_2_mobile_alert_settings_refuse_a_manager_and_keep_the_recipients(monkeypatch):
    rid = _rid()
    models.update_restaurant(rid, {"alert_extra_emails": "owner2@x.test", "alert_1star": 1})
    _uid, mgr = _login(rid, "mgr", "manager")
    r = _app().test_client().post("/mobile/api/account/alert-settings", headers=mgr,
                                  json={"alert_extra_emails": "me@evil.test", "alert_1star": False})
    assert r.status_code == 403 and r.get_json()["owner_only"] is True
    after = models.get_restaurant(rid)
    assert after.alert_extra_emails == "owner2@x.test" and after.alert_1star == 1
    _uid, own = _login(rid, "boss", "client")
    r = _app().test_client().post("/mobile/api/account/alert-settings", headers=own,
                                  json={"alert_extra_emails": "gm@x.test", "alert_1star": True})
    assert r.status_code == 200 and models.get_restaurant(rid).alert_extra_emails == "gm@x.test"


def test_2_web_alert_settings_and_digest_day_refuse_a_manager(monkeypatch):
    rid = _rid()
    models.update_restaurant(rid, {"alert_1star": 1, "digest_day": "monday"})
    _web_as(monkeypatch, rid, "manager")
    c = _app().test_client()
    r = c.post("/api/alert-settings", json={"alert_1star": False})
    assert r.status_code == 403 and r.get_json()["owner_only"] is True
    r = c.post("/api/update-digest-day", json={"day": "friday"})
    assert r.status_code == 403
    after = models.get_restaurant(rid)
    assert after.alert_1star == 1 and after.digest_day == "monday"


@pytest.mark.parametrize("body_name,payload,column", [
    ("_do_login_notify", {"enabled": False}, "login_notify"),
    ("_do_monthly_review_pref", {"enabled": False}, "monthly_review_enabled"),
    ("_do_marketing_opt_out", {"opted_out": True}, "marketing_emails_opt_out"),
])
def test_2_the_owners_email_switches_refuse_a_manager(body_name, payload, column):
    rid = _rid()
    before = getattr(models.get_restaurant(rid), column)
    body = getattr(client_api, body_name)
    out, st = _route(lambda u: body(rid, payload, u), _manager(rid))
    assert st == 403 and out["owner_only"] is True
    assert getattr(models.get_restaurant(rid), column) == before
    out, st = _route(lambda u: body(rid, payload, u), _owner(rid))
    assert st == 200


def test_2_the_web_sign_in_alert_route_refuses_a_manager(monkeypatch):
    rid = _rid()
    models.update_restaurant(rid, {"login_notify": 1})
    _web_as(monkeypatch, rid, "manager")
    r = _app().test_client().post("/api/account-settings/login-notify", json={"enabled": False})
    assert r.status_code == 403 and models.get_restaurant(rid).login_notify == 1


def test_2_ask_cannot_change_an_owner_setting_for_a_manager():
    import ask_cavnar_tools as tools
    rid = _rid()
    models.update_restaurant(rid, {"login_notify": 1})
    view = tools.viewer_restaurant(models.get_restaurant(rid), _manager(rid))
    out = tools._apply_setting(rid, setting="login_notify", value=False, _viewer=view)
    assert out["ok"] is False and models.get_restaurant(rid).login_notify == 1
    assert tools._BY_NAME["change_setting"].get("wants_viewer") is True
    owner_view = tools.viewer_restaurant(models.get_restaurant(rid), _owner(rid))
    assert tools._apply_setting(rid, setting="login_notify", value=False, _viewer=owner_view)["ok"] is True


# ── 3. trusted devices ─────────────────────────────────────────────────────

def test_3_a_remembered_device_is_the_login_that_remembered_it():
    rid = _rid()
    tok = auth.create_trusted_device(rid, 42, "iPhone · app", db_path=models.DB_PATH)
    assert auth.trusted_device_ok(rid, tok, 42, db_path=models.DB_PATH)
    assert not auth.trusted_device_ok(rid, tok, 43, db_path=models.DB_PATH)
    assert not auth.trusted_device_ok(rid, tok, 43, db_path=models.DB_PATH, principal=True)
    assert not auth.trusted_device_ok(rid, tok, None, db_path=models.DB_PATH)
    # A row that names no login is honoured only for an account holder.
    anon = auth.create_trusted_device(rid, None, "Mac · web", db_path=models.DB_PATH)
    assert not auth.trusted_device_ok(rid, anon, 43, db_path=models.DB_PATH)
    assert auth.trusted_device_ok(rid, anon, 43, db_path=models.DB_PATH, principal=True)


def test_3_a_managers_remembered_device_does_not_skip_the_owners_second_factor(monkeypatch):
    import emails
    monkeypatch.setattr(emails, "send_2fa_code", lambda *a, **k: True)
    rid = _rid()
    models.update_restaurant(rid, {"two_fa_enabled": 1})
    owner_uid, _h = _login(rid, "boss", "client")
    mgr_uid, _h = _login(rid, "mgr", "manager")
    mgr_device = auth.create_trusted_device(rid, mgr_uid, "iPhone · app", db_path=models.DB_PATH)
    own_device = auth.create_trusted_device(rid, owner_uid, "iPhone · app", db_path=models.DB_PATH)
    c = _app().test_client()
    r = c.post("/mobile/api/login", json={"username": "boss", "password": "pw-123456", "device_token": mgr_device})
    assert r.get_json()["requires_2fa"] is True and "token" not in r.get_json()
    r = c.post("/mobile/api/login", json={"username": "boss", "password": "pw-123456", "device_token": own_device})
    assert r.get_json()["requires_2fa"] is False and r.get_json()["token"]


def test_3_every_sign_in_path_passes_the_authenticating_login():
    import auth_routes
    src = inspect.getsource(auth_routes) + inspect.getsource(mobile_api)
    assert "trusted_device_ok(" not in src.replace("def trusted_device_ok(", "")
    assert src.count("remembered_device_ok") >= 4   # web + SSO, mobile + Apple


# ── 4. Google Business and Instagram connections ───────────────────────────

def test_4_mobile_google_and_instagram_connect_and_disconnect_are_owner_only(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("META_APP_ID", "app")
    rid = _rid()
    models.update_restaurant(rid, {"gmb_location_id": "loc-1", "ig_user_id": "ig-1"})
    _uid, mgr = _login(rid, "mgr", "manager")
    c = _app().test_client()
    for method, path in (("get", "/mobile/api/connections/google/authorize"),
                         ("delete", "/mobile/api/connections/google"),
                         ("get", "/mobile/api/connections/instagram/authorize"),
                         ("delete", "/mobile/api/connections/instagram")):
        r = getattr(c, method)(path, headers=mgr)
        assert r.status_code == 403 and r.get_json()["owner_only"] is True, path
    after = models.get_restaurant(rid)
    assert after.gmb_location_id == "loc-1" and after.ig_user_id == "ig-1"
    _uid, own = _login(rid, "boss", "client")
    assert c.get("/mobile/api/connections/google/authorize", headers=own).status_code == 200
    assert c.delete("/mobile/api/connections/instagram", headers=own).status_code == 200


def test_4_web_google_and_instagram_connect_and_disconnect_are_owner_only(monkeypatch):
    import auth_routes
    import social_routes
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    rid = _rid()
    models.update_restaurant(rid, {"gmb_location_id": "loc-1", "ig_user_id": "ig-1"})
    mgr = _manager(rid)
    app = _app()
    for fn in (auth_routes.gmb_connect, auth_routes.gmb_disconnect,
               social_routes.instagram_connect, social_routes.instagram_disconnect):
        with app.test_request_context(method="POST", json={}):
            resp = inspect.unwrap(fn)(current_user=mgr)
        assert resp[1] == 403, fn.__name__
    with app.test_request_context("/auth/google/callback?code=c&state=n:1"):
        body, st = inspect.unwrap(auth_routes.gmb_callback)(current_user=mgr)
    assert st == 403 and "Only the account owner" in body
    after = models.get_restaurant(rid)
    assert after.gmb_location_id == "loc-1" and after.ig_user_id == "ig-1"


# ── 5. a queued order's dollars ────────────────────────────────────────────

def test_5_a_queued_orders_dollars_are_for_logins_that_see_food_cost():
    import activity
    import delayed
    rid = _rid()
    delayed.schedule(rid, "order_send", {"supplier_email": "s@x.test"}, 10,
                     label="Sending the Sysco order ($1,234, 5 items)")
    out, _ = _route(strategy_routes._do_delayed_pending, _manager(rid))
    assert out["actions"][0]["label"] == "Sending the Sysco order (5 items)"
    out, _ = _route(strategy_routes._do_delayed_pending, _owner(rid))
    assert out["actions"][0]["label"] == "Sending the Sysco order ($1,234, 5 items)"
    feed = activity.build(rid, denied={"inventory"})
    assert feed["queued"] and "$" not in feed["queued"][0]["text"]
    assert "$1,234" in activity.build(rid)["queued"][0]["text"]


# ── 6. no generation on a GET ──────────────────────────────────────────────

def test_6_the_content_calendar_get_reads_and_never_generates(monkeypatch):
    import marketing
    rid = _rid()
    _web_as(monkeypatch, rid, "client")
    monkeypatch.setattr(marketing, "get_content_calendar_ideas",
                        lambda **k: pytest.fail("a GET generated a calendar"))
    c = _app().test_client()
    r = c.get("/api/content-calendar?force=1")
    assert r.status_code == 405 and r.get_json()["error"]
    body = c.get("/api/content-calendar").get_json()
    assert body["ideas"] == [] and body["none_yet"] is True and body["error"]
    week = [{"day": "Monday", "angle": "Pasta night", "type": "instagram_post"}]
    monkeypatch.setattr(marketing, "get_cached_calendar", lambda *a, **k: [dict(i) for i in week])
    assert [i["angle"] for i in c.get("/api/content-calendar").get_json()["ideas"]] == ["Pasta night"]


def test_6_generate_week_is_a_post(monkeypatch):
    import marketing
    rid = _rid()
    _web_as(monkeypatch, rid, "client")
    calls = []
    monkeypatch.setattr(marketing, "get_cached_calendar", lambda *a, **k: None)
    monkeypatch.setattr(marketing, "get_content_calendar_ideas",
                        lambda **k: calls.append(k) or [{"day": "Monday", "angle": "Pasta", "type": "instagram_post"}])
    c = _app().test_client()
    assert c.post("/api/content-calendar", json={}).get_json()["ideas"]
    assert c.post("/api/content-calendar", json={"force": False}).get_json()["ideas"]
    assert [k["force"] for k in calls] == [True, False]


def test_6_generate_schedule_is_post_only():
    rules = [r for r in _app().url_map.iter_rules() if r.rule == "/api/generate-schedule"]
    assert rules and all("GET" not in r.methods for r in rules)


# ── 7. ending a goal ───────────────────────────────────────────────────────

def test_7_a_manager_cannot_end_a_food_cost_goal():
    import goals
    rid = _rid()
    g = goals.set_goal(rid, "food_cost_pct", 30)
    out, st = _route(strategy_routes._do_goal_end, _manager(rid), args=(g["id"],))
    assert st == 404
    assert _q("SELECT status FROM owner_goals WHERE id=?", (g["id"],))[0]["status"] == "active"
    out, st = _route(strategy_routes._do_goal_end, _owner(rid), args=(g["id"],))
    assert st == 200
    assert _q("SELECT status FROM owner_goals WHERE id=?", (g["id"],))[0]["status"] == "abandoned"


# ── 8. a 2FA resend that did not go ────────────────────────────────────────

@pytest.mark.parametrize("failure", ["false", "raise"])
def test_8_a_failed_2fa_resend_is_a_502_with_a_sentence(monkeypatch, failure):
    import emails
    import auth_routes
    monkeypatch.setattr(emails, "send_2fa_code", lambda *a, **k: True)
    auth_routes._login_attempts.clear()
    rid = _rid()
    models.update_restaurant(rid, {"two_fa_enabled": 1})
    _login(rid, "boss", "client")
    c = _app().test_client()
    pending = c.post("/mobile/api/login", json={"username": "boss", "password": "pw-123456"}).get_json()["pending_token"]

    def _send(*a, **k):
        if failure == "raise":
            raise RuntimeError("provider down")
        return False
    monkeypatch.setattr(auth, "send_two_fa_code", _send)
    r = c.post("/mobile/api/resend-2fa", json={"pending_token": pending})
    assert r.status_code == 502
    body = r.get_json()
    assert body["ok"] is False and "new code" in body["error"]


# ── 9–11. replies ──────────────────────────────────────────────────────────

def _review(rid, **cols):
    when = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    save_reviews([Review(restaurant_id=rid, platform="google", external_id=f"x-{uuid.uuid4().hex[:10]}",
                         author="Sam P.", rating=5, text="Lovely dinner.", review_date=when)],
                 db_path=models.DB_PATH)
    review_id = _q("SELECT MAX(id) AS id FROM reviews WHERE restaurant_id=?", (rid,))[0]["id"]
    cols.setdefault("processed", 1)
    cols.setdefault("sentiment", "positive")
    cols.setdefault("draft_response", "Thanks so much, Sam!")
    _x("UPDATE reviews SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), review_id))
    return review_id


@pytest.mark.parametrize("status", ["posted", "approved"])
def test_9_an_approved_or_posted_reply_is_never_skipped(status):
    rid = _rid()
    rev = _review(rid, response_status=status)
    out, st = client_api._do_skip(rev, rid)
    assert st == 409 and out["ok"] is False and out["error"]
    assert _q("SELECT response_status FROM reviews WHERE id=?", (rev,))[0]["response_status"] == status


def test_9_a_draft_is_still_skipped_and_a_missing_one_is_404():
    rid = _rid()
    rev = _review(rid, response_status="drafted")
    assert client_api._do_skip(rev, rid) == ({"ok": True}, 200)
    assert _q("SELECT response_status FROM reviews WHERE id=?", (rev,))[0]["response_status"] == "skipped"
    assert client_api._do_skip(99999, rid)[1] == 404


def test_9_the_phone_skip_answers_409_for_a_posted_reply():
    rid = _rid()
    _uid, own = _login(rid, "boss", "client")
    rev = _review(rid, response_status="posted")
    r = _app().test_client().post(f"/mobile/api/reviews/{rev}/skip", headers=own)
    assert r.status_code == 409 and r.get_json()["error"]


def test_10_a_bulk_run_that_only_held_drafts_drops_homes_cache(monkeypatch):
    import drafter
    rid = _rid()
    _review(rid, response_status="drafted")
    monkeypatch.setattr(drafter, "check_reply", lambda *a, **k: ("states a specific action", ""))
    dropped = []
    monkeypatch.setattr(client_api, "_invalidate_home", lambda r: dropped.append(r))
    out, st = client_api._do_approve_all(rid)
    assert st == 200 and out["approved"] == 0 and out["held_for_review"] == 1
    assert dropped == [rid]


def test_11_the_owner_reason_reads_cleanly_from_every_source():
    import drafter
    assert drafter.owner_reason("held from auto-approve: Cavnar AI would reword part of this reply "
                                "before it goes out") == drafter.REWORD_REVIEW_REASON
    assert drafter.owner_reason("held from bulk publish: makes a claim a public reply must not: x") \
        == "makes a claim a public reply must not: x"
    assert drafter.owner_reason("states a specific action the restaurant may not have taken: comp") \
        == "states a specific action the restaurant may not have taken: comp"
    assert drafter.owner_reason(None) == drafter.DEFAULT_REVIEW_REASON


@pytest.mark.parametrize("stored,expected", [
    ("held from auto-approve: Cavnar AI would reword part of this reply before it goes out",
     "it has wording Cavnar AI would change before it goes out."),
    ("held from bulk publish: names a staff member in public", "it names a staff member in public."),
    ("states something Cavnar AI cannot confirm", "it states something Cavnar AI cannot confirm."),
])
def test_11_the_flagged_draft_refusal_is_one_clean_sentence(stored, expected):
    rid = _rid()
    rev = _review(rid, response_status="drafted", draft_needs_review=1, draft_review_reason=stored)
    with Flask(__name__).test_request_context():
        out, st = client_api._do_approve(rev, rid)
    assert st == 409 and out["needs_review"] is True
    assert out["error"] == f"Read this reply before you post it: {expected} Open the review to post it anyway."
    assert "held from" not in out["review_reason"]


def test_11_bulk_and_auto_approve_store_the_plain_reason(monkeypatch):
    import drafter
    rid = _rid()
    rev = _review(rid, response_status="drafted")
    monkeypatch.setattr(drafter, "check_reply", lambda *a, **k: ("names a staff member in public", ""))
    monkeypatch.setattr(client_api, "_invalidate_home", lambda r: None)
    client_api._do_approve_all(rid)
    assert _q("SELECT draft_review_reason FROM reviews WHERE id=?", (rev,))[0]["draft_review_reason"] \
        == "names a staff member in public"
    import scheduler
    src = inspect.getsource(scheduler.auto_approve_five_stars)
    assert "held from auto-approve" not in src


# ── 12. a replayed answer ──────────────────────────────────────────────────

def test_12_a_replayed_done_after_pass_starts_no_tracker(monkeypatch):
    import rec_ledger
    rid = _rid()
    for key in ("trim_day:Friday", "trim_day:Monday"):
        rec_ledger.present(rid, key, "labor", "home", title="Trim")
    started = []
    monkeypatch.setattr(strategy_routes, "_start_rec_tracker",
                        lambda *a, **k: (started.append(a[1]) or (None, None)))
    out, st = _route(strategy_routes._do_rec_event, _owner(rid),
                     {"key": "trim_day:Friday", "event": "dismissed", "kind": "not_for_us"})
    assert st == 200
    out, st = _route(strategy_routes._do_rec_event, _owner(rid),
                     {"key": "trim_day:Friday", "event": "completed", "surface": "labor"})
    assert st == 200 and out["already_answered"] is True and started == []
    # An open episode still starts one.
    out, st = _route(strategy_routes._do_rec_event, _owner(rid),
                     {"key": "trim_day:Monday", "event": "completed", "surface": "labor"})
    assert st == 200 and started == ["trim_day:Monday"] and "already_answered" not in out
