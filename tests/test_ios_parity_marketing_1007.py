"""iOS parity round (10/7/26), Marketing: the phone's post routes carry the
photo exactly as the web's do (#6), and the routes the iOS Campaign
Studio, Opportunity Feed, newsletter history and invite switch call answer
the fields the app decodes (#12, #17, #28, #30, #39, #40, #51, #60, #72,
#73, #74).

Meta and Google are never reached: `requests` is a scripted fake, and the
Google publish is a stub that records what it was asked to post. Nothing is
texted or emailed — the senders are stubbed.
"""

import os
import re
import sqlite3
import sys
import time

import pytest
from flask import Flask

import auth
import client_api
import marketing_media
import marketing_publish as mp
import mobile_api
import models
import social_routes
from client_api import client_bp
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant
from social_routes import social_bp

# Imported before any fake is in place, so none keeps one.
import gmb  # noqa: E402
import guest_email  # noqa: E402
import guest_marketing  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IOS = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Features", "Marketing")


# ── fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def _template_db(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("ios_mkt_template") / "template.db")
    models.init_db(db_path=path)
    models.ensure_columns(db_path=path)
    auth.init_auth(db_path=path)
    guest_marketing.init_guest_marketing(db_path=path)
    return path


@pytest.fixture
def db_path(tmp_path, _template_db):
    import shutil
    path = str(tmp_path / "test_reviews.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(_template_db + suffix):
            shutil.copy(_template_db + suffix, path + suffix)
    return path


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, mp, social_routes, client_api, mobile_api, marketing_media, guest_marketing,
                guest_email):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    import ops
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    monkeypatch.setattr(time, "sleep", lambda s: None)


@pytest.fixture
def app():
    flask_app = Flask(__name__)
    flask_app.register_blueprint(social_bp)
    flask_app.register_blueprint(client_bp)
    flask_app.register_blueprint(mobile_bp)
    return flask_app


class Phone:
    """A signed-in phone: the session token as a Bearer header."""

    def __init__(self, app, db_path, rid, role="client", username="owner"):
        self.client = app.test_client()
        uid = auth.create_user(rid, username, f"{username}@ios.test", "correct-horse-battery", db_path=db_path)
        auth.set_user_role(uid, role, db_path=db_path)
        self.h = {"Authorization": "Bearer " + auth.create_session(uid, restaurant_id=rid, db_path=db_path)}

    def post(self, path, **kw):
        return self.client.post(path, headers=self.h, **kw)

    def get(self, path, **kw):
        return self.client.get(path, headers=self.h, **kw)


class FakeResp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        if self._body is None:
            raise ValueError("Expecting value")
        return self._body


class FakeGraph:
    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def _answer(self, method, url, kw):
        self.calls.append((method, url, kw))
        for m, needle, resp in self.routes:
            if m == method and needle in url:
                return resp
        raise AssertionError(f"unexpected Graph call {method} {url}")

    def get(self, url, **kw):
        return self._answer("GET", url, kw)

    def post(self, url, **kw):
        return self._answer("POST", url, kw)


def _graph(monkeypatch, routes):
    fake = FakeGraph(routes)
    monkeypatch.setitem(sys.modules, "requests", fake)
    return fake


def _restaurant(db_path, name="iOS Co", **fields):
    rid = create_restaurant(Restaurant(name=name, owner_email="owner@ios.test", module_marketing=1,
                                       timezone="America/Chicago"), db_path=db_path)
    f = dict(ig_token="igt", ig_user_id="igu", fb_page_token="fbt", fb_page_id="fbp")
    f.update(fields)
    models.update_restaurant(rid, f, db_path=db_path)
    return rid


def _media(db_path, rid, token):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("INSERT INTO marketing_media (restaurant_id, token, mime, data) VALUES (?,?,?,?)",
                           (rid, token, "image/jpeg", b"x"))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _ig_routes():
    return [
        ("POST", "igu/media_publish", FakeResp(200, {"id": "ig_post_1"})),
        ("POST", "igu/media", FakeResp(200, {"id": "container_1"})),
        ("GET", "container_1", FakeResp(200, {"status_code": "FINISHED"})),
    ]


def _swift(name):
    with open(os.path.join(IOS, name), encoding="utf-8") as f:
        return f.read()


# ── #6: the phone's posts carry the photo ─────────────────────────────────

def test_a_phone_facebook_post_with_a_library_photo_posts_the_photo(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    media_id = _media(db_path, rid, "libtok")
    fake = _graph(monkeypatch, [("POST", "fbp/photos", FakeResp(200, {"id": "ph", "post_id": "fbp_78"}))])
    resp = Phone(app, db_path, rid).post("/mobile/api/marketing/post-to-facebook",
                                         json={"caption": "Tacos", "media_id": media_id})
    assert resp.get_json()["ok"], resp.get_json()
    method, url, kw = fake.calls[0]
    assert "fbp/photos" in url
    assert kw["data"]["url"].startswith("http") and kw["data"]["url"].endswith("/m/libtok.jpg")
    assert kw["data"]["message"] == "Tacos"


def test_a_phone_facebook_post_without_a_photo_is_a_text_post(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    fake = _graph(monkeypatch, [("POST", "fbp/feed", FakeResp(200, {"id": "fbp_9"}))])
    resp = Phone(app, db_path, rid).post("/mobile/api/marketing/post-to-facebook", json={"caption": "Wings tonight"})
    assert resp.get_json()["ok"]
    assert "fbp/feed" in fake.calls[0][1]


def test_a_phone_post_naming_another_restaurants_photo_is_refused_before_meta(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    other = _restaurant(db_path, name="Other Co")
    theirs = _media(db_path, other, "theirtok")
    fake = _graph(monkeypatch, [])
    phone = Phone(app, db_path, rid)
    for path in ("/mobile/api/marketing/post-to-facebook", "/mobile/api/marketing/post-to-instagram"):
        resp = phone.post(path, json={"caption": "Tacos", "media_id": theirs})
        assert resp.status_code == 400, path
        assert resp.get_json()["error"] == "That photo isn't in your library."
    assert fake.calls == []


def test_a_phone_instagram_post_by_media_id_sends_meta_an_absolute_url(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    media_id = _media(db_path, rid, "igtok")
    fake = _graph(monkeypatch, _ig_routes())
    resp = Phone(app, db_path, rid).post("/mobile/api/marketing/post-to-instagram",
                                         json={"caption": "Fall menu", "media_id": media_id})
    assert resp.get_json()["ok"], resp.get_json()
    create = next(c for c in fake.calls if c[1].endswith("igu/media"))
    assert create[2]["data"]["image_url"].startswith("http")
    assert create[2]["data"]["image_url"].endswith("/m/igtok.jpg")


def test_a_phone_instagram_post_with_a_relative_library_url_is_made_absolute(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    fake = _graph(monkeypatch, _ig_routes())
    resp = Phone(app, db_path, rid).post("/mobile/api/marketing/post-to-instagram",
                                         json={"caption": "Fall menu", "image_url": "/m/rel.jpg"})
    assert resp.get_json()["ok"], resp.get_json()
    create = next(c for c in fake.calls if c[1].endswith("igu/media"))
    assert re.match(r"^https?://[^/]+/m/rel\.jpg$", create[2]["data"]["image_url"])


def test_a_phone_google_post_by_media_id_carries_the_photo(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    media_id = _media(db_path, rid, "gtok")
    asked = {}
    monkeypatch.setattr(gmb, "is_connected", lambda r: True)

    def post_local(r, summary, cta_type=None, cta_url=None, photo_url=None):
        asked.update(summary=summary, photo_url=photo_url)
        return {"ok": True, "name": "accounts/1/locations/2/localPosts/5"}
    monkeypatch.setattr(gmb, "post_local", post_local)
    resp = Phone(app, db_path, rid).post("/mobile/api/marketing/google-post",
                                         json={"summary": "Pumpkin pie is back", "media_id": media_id,
                                               "cta_type": "", "cta_url": "", "topic": "Pie"})
    assert resp.get_json()["ok"], resp.get_json()
    assert asked["photo_url"].startswith("http") and asked["photo_url"].endswith("/m/gtok.jpg")


def test_the_phone_and_web_post_routes_resolve_the_photo_through_one_function():
    src = open(os.path.join(ROOT, "mobile_api.py"), encoding="utf-8").read()
    for name in ("def mobile_post_to_instagram", "def mobile_post_to_facebook"):
        body = src[src.index(name):]
        body = body[:body.index("\n@mobile_bp.route")]
        assert "photo_url_from(current_user[\"restaurant_id\"], data)" in body, name
    vm = _swift("MarketingViewModel.swift")
    assert "postToFacebook(mediaId: media?.id)" in vm
    assert "mediaId: mediaId),\n                      platform: \"Google\")" in vm


# ── #12 / #30 / #39: what the Studio and the Text Club read ───────────────

def test_the_segments_say_who_a_text_reaches_now(app, db_path):
    rid = _restaurant(db_path)
    body = Phone(app, db_path, rid).get("/mobile/api/guest-segments").get_json()
    assert body["ok"]
    for seg in body["segments"]:
        assert {"key", "label", "count", "eligible", "email_count"} <= set(seg)
    vm = _swift("GuestTextClubViewModel.swift")
    assert "?.reach ?? 0" in vm, "Send to N promises who a text reaches now (eligible)"


def test_the_overview_carries_the_window_the_phone_shows(app, db_path):
    rid = _restaurant(db_path)
    body = Phone(app, db_path, rid).get("/mobile/api/guest-overview").get_json()
    assert body["ok"]
    assert body["window"] == guest_marketing.guest_sms_window_label()
    assert "sending_now" in body and len(body["weekly"]) == 12
    for key in ("subscribers", "today", "last_30", "tap_rate", "back_rate", "sms", "email_subscribers",
                "mailing_address_set", "rate_min", "min_days_between"):
        assert key in body, key
    # Nothing on the phone's text screens hard-codes the hours any more.
    for name in ("GuestTextClubView.swift", "GuestTextClubViewModel.swift", "CampaignStudioView.swift",
                 "CampaignStudioViewModel.swift"):
        assert "8:00 AM and 9:00 PM" not in _swift(name), name


def test_a_held_text_outside_the_window_is_queued_for_the_morning(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(guest_marketing, "prepare_campaign", lambda *a, **k: ("all", None))
    seen = {}

    def start(rid_, message, segment="all", link_token=None, **kw):
        seen.update(kw)
        return {"ok": True, "queued": True, "campaign_id": 7, "total": 12, "waiting": True,
                "waiting_until": "8:00 AM", "segment": "all", "segment_label": "Everyone consented"}
    monkeypatch.setattr(guest_marketing, "start_campaign", start)
    resp = Phone(app, db_path, rid).post("/mobile/api/guest-campaign/send", json={
        "message": "See you Tuesday", "segment": "all", "type": "event", "target_day": "Tuesday",
        "link_url": "", "hold": True, "rec_key": "slow_day:Tuesday", "draft_ref": None})
    assert resp.status_code == 202
    assert seen["hold"] is True and seen["target_day"] == "Tuesday" and seen["rec_key"] == "slow_day:Tuesday"
    assert resp.get_json()["waiting_until"] == "8:00 AM"


def test_a_flagged_text_says_why(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(guest_marketing, "prepare_campaign", lambda *a, **k: (
        "all", {"ok": False, "blocked": "gate_flagged", "error": "Cavnar AI held this text back.",
                "reasons": ["It promises a discount you didn't write"]}))
    resp = Phone(app, db_path, rid).post("/mobile/api/guest-campaign/send",
                                         json={"message": "50% off tonight", "segment": "all"})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["blocked"] == "gate_flagged" and body["reasons"] == ["It promises a discount you didn't write"]
    # The phone decodes both and shows them (SendGateSheet).
    models_src = _swift("CampaignModels.swift")
    assert "reasons = c.mktStrings(.reasons)" in models_src and 'blocked == "gate_flagged"' in models_src


# ── #40: stop a campaign ──────────────────────────────────────────────────

def test_a_waiting_campaign_can_be_stopped_from_the_phone(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(guest_marketing, "cancel_campaign",
                        lambda r, cid: {"ok": True, "cancelled": 12, "campaign_id": cid, "status": "cancelled"})
    resp = Phone(app, db_path, rid).post("/mobile/api/guest-campaign/9/cancel")
    assert resp.status_code == 200 and resp.get_json()["cancelled"] == 12
    hist = Phone(app, db_path, rid, username="owner2").get("/mobile/api/guest-campaigns").get_json()
    assert hist["ok"] and "ledger" in hist and "window" in hist["ledger"]


# ── #17 / #72: the newsletter's honest results ────────────────────────────

def test_a_newsletter_with_no_address_on_file_asks_for_one(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(guest_email, "send_newsletter", lambda *a, **k: {
        "ok": False, "error": guest_email.NEEDS_ADDRESS, "needs_mailing_address": True})
    resp = Phone(app, db_path, rid).post("/mobile/api/guest-newsletter", json={
        "subject": "Fall", "body": "Hello", "design": {}, "segment": "all"})
    assert resp.status_code == 400 and resp.get_json()["needs_mailing_address"] is True


def test_only_the_owner_sets_the_mailing_address(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(guest_email, "send_newsletter", lambda *a, **k: {"ok": True})
    resp = Phone(app, db_path, rid, role="manager", username="mgr").post("/mobile/api/guest-newsletter", json={
        "subject": "Fall", "body": "Hello", "segment": "all", "mailing_address": "1 Main St, Chicago IL 60601"})
    assert resp.status_code == 403
    assert resp.get_json().get("owner_only") is True or "owner" in resp.get_json()["error"].lower()


def test_an_email_already_sent_today_answers_409_with_the_new_subscribers(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(guest_email, "send_newsletter", lambda *a, **k: {
        "ok": False, "already_sent": True, "newsletter_id": 4, "new_subscribers": 3, "sent_on": "10/7/26",
        "error": "Already sent on 10/7/26 — 3 new subscribers since.", "sent": 40, "failed": 0, "retryable": 0,
        "skipped": 0, "total": 40, "queued": 0})
    resp = Phone(app, db_path, rid).post("/mobile/api/guest-newsletter", json={
        "subject": "Fall", "body": "Hello", "design": {}, "segment": "all"})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["already_sent"] is True and body["new_subscribers"] == 3 and body["newsletter_id"] == 4


def test_retry_and_send_to_new_answer_the_counts_the_phone_reads(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    counts = {"sent": 41, "failed": 1, "retryable": 1, "skipped": 0, "total": 42, "queued": 0}
    monkeypatch.setattr(guest_email, "retry_failed", lambda r, n: dict({"ok": True, "newsletter_id": n,
                                                                        "retried": 2}, **counts))
    monkeypatch.setattr(guest_email, "send_to_new_subscribers", lambda r, n: dict({"ok": True, "newsletter_id": n,
                                                                                   "added": 3}, **counts))
    phone = Phone(app, db_path, rid)
    a = phone.post("/mobile/api/guest-newsletter/4/retry").get_json()
    b = phone.post("/mobile/api/guest-newsletter/4/send-new").get_json()
    for body in (a, b):
        assert body["ok"] and {"sent", "failed", "retryable", "skipped", "queued"} <= set(body)
    assert b["added"] == 3


def test_the_newsletter_history_is_a_floor_free_record(app, db_path):
    rid = _restaurant(db_path)
    body = Phone(app, db_path, rid).get("/mobile/api/guest-newsletters").get_json()
    assert body == {"ok": True, "newsletters": []}
    tab = _swift("CampaignsTab.swift")
    assert "Opens recorded" in tab and "Opens include Apple Mail auto-opens." in tab
    assert "at least" not in tab.lower()


# ── #28: the Opportunity Feed ─────────────────────────────────────────────

def test_the_feed_answers_the_phone_and_show_all_logs_the_rest(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    import marketing_opportunities
    calls = []

    def feed(r, user_id=None, user=None, surface="marketing", show_all=False):
        calls.append(show_all)
        return {"ok": True, "items": [], "visible": 3, "sources": [], "checked": []}
    monkeypatch.setattr(marketing_opportunities, "feed", feed)
    phone = Phone(app, db_path, rid)
    assert phone.get("/mobile/api/marketing/opportunities").get_json()["ok"]
    assert phone.get("/mobile/api/marketing/opportunities?show=all").get_json()["ok"]
    assert calls == [False, True]
    vm = _swift("MarketingOpportunityFeed.swift")
    assert 'query: open ? ["show": "all"] : [:]' in vm


# ── #73: the review-link invite switch ────────────────────────────────────

def test_the_invite_switch_is_the_owners_and_on_needs_the_acknowledgement(app, db_path):
    rid = _restaurant(db_path)
    owner = Phone(app, db_path, rid)
    state = owner.get("/mobile/api/guest-optin-invites").get_json()
    assert state["ok"] and state["enabled"] is False and state["disclosure"]
    manager = Phone(app, db_path, rid, role="manager", username="mgr")
    assert manager.post("/mobile/api/guest-optin-invites", json={"enabled": True, "acknowledged": True}).status_code == 403
    on = owner.post("/mobile/api/guest-optin-invites", json={"enabled": True, "acknowledged": True}).get_json()
    assert on["ok"] and on["enabled"] is True and on["acknowledged_at"]
    tab = _swift("CampaignsTab.swift")
    assert "OptinInvitesBody(enabled: on, acknowledged: on)" in tab


# ── #30 / #51: the Studio's drafts and sends name every key ───────────────

def test_the_studio_send_bodies_carry_every_key_the_routes_read():
    models_src = _swift("CampaignModels.swift")
    text = models_src[models_src.index("struct CampaignTextSendBody"):models_src.index("struct WinbackSendBody")]
    for key in ('"target_day"', '"link_url"', '"rec_key"', '"draft_ref"', "hold", "segment", "type", "message"):
        assert key in text, key
    mail = models_src[models_src.index("struct NewsletterSendBody"):models_src.index("struct StudioPostBody")]
    for key in ("subject", "body", "design", "segment", '"rec_key"', '"draft_ref"', '"mailing_address"'):
        assert key in mail, key
    post = models_src[models_src.index("struct StudioPostBody"):models_src.index("// MARK: - Opportunity Feed")]
    for key in ('"media_id"', '"image_url"', '"cta_type"', '"cta_url"', '"rec_key"', '"content_log_id"', "summary"):
        assert key in post, key
    vm = _swift("CampaignStudioViewModel.swift")
    for path in ('"/mobile/api/guest-campaign/draft"', '"/mobile/api/guest-newsletter/draft"',
                 '"/mobile/api/marketing/generate-content"', '"/mobile/api/guest-newsletter/preview"'):
        assert path in vm, path
    assert vm.count('case runAsJob = "async"') == 3 and vm.count("client.resolveAIJob(started)") == 3


def test_the_content_tab_is_social_only_on_the_phone():
    vm = _swift("MarketingViewModel.swift")
    assert "var socialContentTypes: [MarketingContentType] { contentTypes.filter(\\.isSocial) }" in vm
    view = _swift("MarketingView.swift")
    assert "ForEach(viewModel.socialContentTypes)" in view
    assert "Paste in a Weekly email" not in _swift("GuestTextClubView.swift")


# ── re-audit 10/8/26: a retry or a send-to-new is a send ────────────────────

def test_a_login_that_cannot_publish_cannot_retry_or_send_a_newsletter_to_new_subscribers(app, db_path, monkeypatch):
    rid = create_restaurant(Restaurant(name="Gate Co", owner_email="g@x.test", module_marketing=1), db_path=db_path)
    called = []
    monkeypatch.setattr(guest_email, "retry_failed", lambda *a, **k: called.append("retry") or {"ok": True})
    monkeypatch.setattr(guest_email, "send_to_new_subscribers", lambda *a, **k: called.append("new") or {"ok": True})
    member = Phone(app, db_path, rid, role="member", username="teammate")
    for path in ("/mobile/api/guest-newsletter/1/retry", "/mobile/api/guest-newsletter/1/send-new"):
        r = member.post(path, json={})
        assert r.status_code == 403, path
    assert called == []
    owner = Phone(app, db_path, rid)
    assert owner.post("/mobile/api/guest-newsletter/1/retry", json={}).status_code == 200
    assert owner.post("/mobile/api/guest-newsletter/1/send-new", json={}).status_code == 200
    assert called == ["retry", "new"]
