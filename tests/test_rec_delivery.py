"""Recommendation ROI audit — coverage and record accuracy.

Every recommendation that is shown is recorded exactly once, at the moment
it is actually delivered, and is answerable. What these pin, by audit item:
  #6   shown is recorded at delivery (a sent email, a report someone reads),
       never when a review, digest or report is BUILT — and the one thing is
       recorded only on an email that renders it;
  #7   a brief-push tap is an open on brief_push, not alert_push;
  #10  only what an owner can answer is presented, and payloads say which;
  #25  the Labor read's lines and its diagnosis are on the ledger;
  #26  Home's one thing and its links are presented by the route serving them;
  #32  links carry rec= and src=, and a load from one records `opened`;
  #40  the owner's edits to reply drafts are measured, deterministically;
  #41  content ideas, the AI-visibility roadmap, loss flags, the digest's
       move, the monthly next move, standby and overtime moves are presented;
  #48  Ask's concrete suggestions are keyed, and answers can be rated.
No model, network, email, SMS or push is reached."""
import json
import sqlite3
import types
from datetime import date, datetime, timedelta

import pytest
from flask import Flask

import auth
import business_intelligence as bi
import client_api
import emails
import mobile_api
import models
import morning_brief
import rec_delivery
import rec_ledger
import strategy_routes
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    for mod in (morning_brief, bi):
        monkeypatch.setattr(mod, "get_conn", fake, raising=False)
    import webhooks
    monkeypatch.setattr(webhooks, "get_conn", fake, raising=False)
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    auth.init_auth(db_path=db_path)
    client_api._insight_cache.clear()
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("AI called")))
    yield
    client_api._insight_cache.clear()


def _rid(db_path, **kw):
    fields = dict(name="Gia Mia", owner_email="o@x.test", owner_name="Sam Owner", module_reviews=1)
    fields.update(kw)
    return create_restaurant(Restaurant(**fields), db_path=db_path)


def _user(rid, uid=7, role="owner"):
    return {"id": uid, "restaurant_id": rid, "base_restaurant_id": rid, "username": "owner", "role": role,
            "is_admin": 0, "email": "o@x.test"}


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _shown(db_path, rid, surface=None):
    c = _conn(db_path)
    try:
        q = "SELECT key, surface FROM rec_events WHERE restaurant_id=? AND event='shown'"
        args = [rid]
        if surface:
            q += " AND surface=?"
            args.append(surface)
        return sorted((r["key"], r["surface"]) for r in c.execute(q, args).fetchall())
    finally:
        c.close()


def _events(db_path, rid, event):
    c = _conn(db_path)
    try:
        return [dict(r) for r in c.execute("SELECT key, surface, event FROM rec_events WHERE restaurant_id=? "
                                           "AND event=?", (rid, event)).fetchall()]
    finally:
        c.close()


class _Sent:
    def __init__(self, ok=True):
        self.ok, self.error, self.status_code, self.attempts = ok, (None if ok else "boom"), None, 1


FIX_FIRST = {"key": "cut_waste:Salmon", "what": "Cut salmon waste", "why": "it is the biggest driver",
             "modules": ["food_cost"], "dollars_monthly": 240.0}
PRIORITIES = [{"key": "money:food_cost", "label": "Food cost drivers", "monthly": 900.0}]


def _brief(fix_first=FIX_FIRST):
    return {"fix_first": dict(fix_first) if fix_first else None,
            "money": {"ranked": [dict(p) for p in PRIORITIES]}, "links": []}


# ── rec_delivery: stage only inside a delivery ──────────────────────────────

def test_stage_records_nothing_outside_a_delivery_and_only_on_flush(db_path):
    rid = _rid(db_path)
    item = [{"key": "reprice:Carbonara", "module": "food", "title": "Reprice"}]
    assert rec_delivery.stage(rid, "digest", item) is False
    assert _shown(db_path, rid) == []
    with rec_delivery.collect() as shown:
        assert rec_delivery.stage(rid, "digest", item) is True
    assert _shown(db_path, rid) == []                      # the send has not succeeded yet
    shown.flush()
    assert _shown(db_path, rid) == [("reprice:Carbonara", "digest")]


def test_money_ranking_owed_replies_and_pulse_news_are_never_presented():
    for key in ("money:labor", "money:food_cost", "urgent_reviews", "intraday_pulse:2026-09-23"):
        assert not rec_delivery.presentable(key) and not rec_delivery.answerable(key)
    for key in ("link:reviews_x_labor:service:friday", "schedule_to_target:30%", "pulse_cut:2026-09-23:ana",
                "quiet_night:2026-09-25", "review_requests", "cut_waste:Salmon"):
        assert rec_delivery.presentable(key) and rec_delivery.answerable(key)
    # Bookkeeping keys are presented (the trail keeps them) but not answerable.
    assert rec_delivery.presentable("standby:2026-09-24:ana") and not rec_delivery.answerable("standby:2026-09-24:ana")
    with rec_delivery.collect() as shown:
        rec_delivery.stage(1, "weekly_email", [{"key": "money:labor"}, {"key": "urgent_reviews"}])
    assert shown.groups == []


def test_link_appends_rec_and_src_and_keeps_the_fragment():
    assert rec_delivery.link("https://x.test/?tab=labor", "trim_day:Monday", "alert_email") == \
        "https://x.test/?tab=labor&rec=trim_day%3AMonday&src=alert_email"
    assert rec_delivery.link("https://x.test/#dsr/2026-09-23", "k", "dsr_email") == \
        "https://x.test/?rec=k&src=dsr_email#dsr/2026-09-23"
    assert rec_delivery.link("https://x.test/", None, "digest") == "https://x.test/"


# ── #6 the weekly and monthly reviews present at delivery, not at build ─────

def test_building_the_weekly_or_monthly_review_presents_nothing(db_path, monkeypatch):
    import weekly_review, monthly_review
    rid = _rid(db_path)
    monkeypatch.setattr(bi, "executive_brief", lambda *a, **k: _brief())
    weekly_review.build(rid, today=date(2026, 9, 23), db_path=db_path)
    monthly_review.build(rid, today=date(2026, 9, 1), db_path=db_path)
    assert _shown(db_path, rid) == []


def test_the_month_ready_push_no_longer_records_the_monthly_email(db_path, monkeypatch):
    import scheduler, push
    rid = _rid(db_path)
    monkeypatch.setattr(bi, "executive_brief", lambda *a, **k: _brief())
    monkeypatch.setattr(push, "get_device_tokens", lambda *a, **k: [{"user_id": 7, "token": "t"}])
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: None)
    r = models.get_restaurant(rid, db_path)
    assert scheduler._push_month_ready(r) == 1
    assert _shown(db_path, rid) == []


def test_the_weekly_email_renders_the_one_thing_it_records_and_links_it(db_path, monkeypatch):
    import weekly_review
    rid = _rid(db_path)
    monkeypatch.setattr(weekly_review, "build", lambda *a, **k: {
        "week": "9/14/26 – 9/20/26", "compared_with": "9/7/26 – 9/13/26", "metrics": [], "results": [],
        "priorities": [dict(p) for p in PRIORITIES], "fix_first": dict(FIX_FIRST)})
    link = {"kind": "reviews_x_labor", "subject": "service:friday", "headline": "Friday complaints on your leanest Friday",
            "evidence": ["3 complaints"], "modules": ["reviews", "labor"]}
    monkeypatch.setattr(bi, "correlations", lambda *a, **k: [dict(link)])
    with rec_delivery.collect() as shown:
        html = "".join(emails._weekly_review_sections(rid))
    assert "If you only do one thing this week" in html and "Cut salmon waste" in html
    assert "rec=cut_waste%3ASalmon" in html and "src=weekly_email" in html
    assert "rec=link%3Areviews_x_labor%3Aservice%3Afriday" in html
    keys = shown.keys("weekly_email")
    # The one thing and the link are staged; the money ranking is not.
    assert keys == ["cut_waste:Salmon", "link:reviews_x_labor:service:friday"]
    assert _shown(db_path, rid) == []


def test_an_answered_link_is_not_this_weeks_finding(db_path, monkeypatch):
    import weekly_review
    rid = _rid(db_path)
    monkeypatch.setattr(weekly_review, "build", lambda *a, **k: {
        "week": "w", "compared_with": "c", "metrics": [], "results": [], "priorities": [], "fix_first": None})
    link = {"kind": "k", "subject": "s", "headline": "H", "evidence": [], "modules": ["reviews", "labor"]}
    monkeypatch.setattr(bi, "correlations", lambda *a, **k: [dict(link)])
    rec_ledger.record(rid, "link:k:s", "dismissed", surface="home", meta={"kind": "not_for_us"})
    with rec_delivery.collect() as shown:
        html = "".join(emails._weekly_review_sections(rid))
    assert "What connects" not in html and shown.groups == []


def _digest_setup(db_path, monkeypatch, ok):
    import scheduler, reporter, weekly_review
    rid = _rid(db_path, module_labor=0, module_inventory=0, module_marketing=0)
    c = _conn(db_path)
    c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
              "fetched_at, processed) VALUES (?, 'google', 'w1', 'A', 5, 'Great', ?, ?, 1)",
              (rid, datetime.now().strftime("%Y-%m-%d"), datetime.now().isoformat()))
    c.commit(); c.close()
    monkeypatch.setattr(weekly_review, "build", lambda *a, **k: {
        "week": "w", "compared_with": "c", "metrics": [], "results": [], "priorities": [],
        "fix_first": dict(FIX_FIRST)})
    monkeypatch.setattr(bi, "correlations", lambda *a, **k: [])
    monkeypatch.setattr(reporter, "generate_ai_digest_summary",
                        lambda *a, **k: {"headline": "A good week.", "action": "Call the fish supplier about Friday."})
    monkeypatch.setattr(models, "get_restaurants_for_digest", lambda day, *a, **k: [{"id": rid}])
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    monkeypatch.setattr(scheduler, "get_owner_emails", lambda r: ["o@x.test"])
    sent = []
    monkeypatch.setattr(scheduler._emails, "deliver", lambda **k: sent.append(k) or _Sent(ok))
    scheduler.run_weekly_digests()
    return rid, sent


def test_a_delivered_digest_presents_what_it_rendered_once(db_path, monkeypatch):
    import reporter
    rid, sent = _digest_setup(db_path, monkeypatch, ok=True)
    assert len(sent) == 1
    move = reporter.digest_move_key("Call the fish supplier about Friday.")
    assert _shown(db_path, rid) == sorted([("cut_waste:Salmon", "weekly_email"), (move, "digest")])
    html = sent[0]["payload"]["html"]
    assert f"rec={move.replace(':', '%3A')}" in html and "src=digest" in html


def test_a_failed_digest_presents_nothing(db_path, monkeypatch):
    rid, sent = _digest_setup(db_path, monkeypatch, ok=False)
    assert len(sent) == 1 and _shown(db_path, rid) == []


def test_the_monthly_email_presents_its_one_thing_and_next_move_only_when_sent(db_path, monkeypatch):
    import monthly_review
    rid = _rid(db_path)
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    monkeypatch.setattr(monthly_review, "build", lambda *a, **k: {
        "month": "August 2026", "months": 1, "metrics": [], "results": [], "goals": [], "compared_with": "July",
        "priorities": [dict(p) for p in PRIORITIES], "fix_first": dict(FIX_FIRST)})
    monkeypatch.setattr(emails, "generate_email_personalization", lambda ctx, fallback, **k: fallback)
    monkeypatch.setattr(emails, "restaurant_usage", lambda rid: {"owns": {"reviews": True}, "used": {},
                                                                 "pending_reviews": 3})
    outcome = {"ok": False}
    monkeypatch.setattr(emails, "deliver", lambda **k: _Sent(outcome["ok"]))
    emails.send_monthly_summary_email("o@x.test", "Gia Mia", "Sam", restaurant_id=rid)
    assert _shown(db_path, rid) == []
    outcome["ok"] = True
    emails.send_monthly_summary_email("o@x.test", "Gia Mia", "Sam", restaurant_id=rid)
    assert _shown(db_path, rid) == sorted([("cut_waste:Salmon", "monthly_email"),
                                           ("monthly_move:approve_replies", "monthly_email")])


# ── #6 / #32 the daily report ───────────────────────────────────────────────

def _narr(key="dsr_action:control_hours:labor"):
    return {"actions_tomorrow": [{"key": key, "text": "Trim a server on Tuesday dinner.", "why": "Labor ran 34%.",
                                  "kind": "control_hours", "cites": ["labor.pct"], "dollars_monthly": None,
                                  "urgency": "next_schedule", "effort": "low"}]}


def test_the_dsr_email_presents_its_actions_only_when_delivered_and_links_them(db_path, monkeypatch):
    from dsr import deliver
    rid = _rid(db_path)
    r = models.get_restaurant(rid, db_path)
    payload = {"business_date": "2026-09-22", "narrative": _narr(), "facts": {"blocks": {}}, "view": "owner"}
    monkeypatch.setattr(deliver, "_render", lambda *a, **k: payload)
    monkeypatch.setattr(deliver, "_finish", lambda *a, **k: None)
    html_seen = []

    def send(to, d, restaurant_id=None):
        html_seen.append(emails.dsr_email(d)[1])
        return _Sent(False)
    monkeypatch.setattr(emails, "send_dsr_email", send)
    user = {"id": 7, "email": "o@x.test"}
    deliver._send_email(db_path, 1, r, {"id": 1}, user, deliver.FIRST)
    assert _shown(db_path, rid) == []
    assert "rec=dsr_action%3Acontrol_hours%3Alabor" in html_seen[0] and "src=dsr_email" in html_seen[0]
    monkeypatch.setattr(emails, "send_dsr_email", lambda *a, **k: _Sent(True))
    deliver._send_email(db_path, 1, r, {"id": 1}, user, deliver.FIRST)
    assert _shown(db_path, rid) == [("dsr_action:control_hours:labor", "dsr_email")]


def test_the_dsr_view_presents_recent_actions_and_marks_answered_ones(db_path, monkeypatch):
    rid = _rid(db_path)
    u = _user(rid)
    today = datetime.utcnow().date()
    monkeypatch.setattr(strategy_routes, "_local_today", lambda u: today)
    payload = {"narrative": _narr()}
    strategy_routes._dsr_present_view(u, today - timedelta(days=1), payload)
    a = payload["narrative"]["actions_tomorrow"][0]
    assert a["rec_key"] == "dsr_action:control_hours:labor" and a["answerable"] is True and a["answered"] is False
    assert _shown(db_path, rid, "dsr") == [("dsr_action:control_hours:labor", "dsr")]
    rec_ledger.record(rid, a["rec_key"], "completed", surface="dsr")
    again = {"narrative": _narr()}
    strategy_routes._dsr_present_view(u, today - timedelta(days=1), again)
    b = again["narrative"]["actions_tomorrow"][0]
    assert b["answered"] is True and b["answerable"] is False
    # A report read weeks later is history: nothing is presented for it.
    old = {"narrative": _narr("dsr_action:reorder:food")}
    strategy_routes._dsr_present_view(u, today - timedelta(days=20), old)
    assert ("dsr_action:reorder:food", "dsr") not in _shown(db_path, rid)


def test_dsr_email_is_a_labelled_surface():
    assert "dsr_email" in rec_ledger.SURFACES and rec_ledger.SURFACE_LABELS["dsr_email"] == "daily report email"
    assert set(rec_ledger.SURFACE_LABELS) == set(rec_ledger.SURFACES)


# ── #7 brief-push opens by surface ──────────────────────────────────────────

def _open(user, body):
    app = Flask(__name__)
    with app.test_request_context("/mobile/api/notifications/opened", method="POST", json=body):
        mobile_api.mobile_mark_notification_opened.__wrapped__(user)


def test_a_brief_push_tap_is_an_open_on_brief_push(db_path):
    rid = _rid(db_path)
    rec_ledger.present(rid, "schedule:next-week", "labor", "brief_push")
    rec_ledger.present(rid, "negative_trend", "reviews", "alert_push")
    user = _user(rid)
    _open(user, {"type": "morning_brief", "rec_key": "schedule:next-week"})            # an older app: type only
    _open(user, {"type": "negative_trend", "rec_key": "negative_trend", "alert_id": 5})
    _open(user, {"type": "x", "rec_key": "schedule:next-week", "surface": "brief_push", "alert_id": 6})
    got = sorted((e["key"], e["surface"]) for e in _events(db_path, rid, "opened"))
    assert got == [("negative_trend", "alert_push"), ("schedule:next-week", "brief_push"),
                   ("schedule:next-week", "brief_push")]


def test_the_open_surface_ignores_a_payload_surface_that_is_not_a_push():
    assert mobile_api.notification_open_surface({"surface": "home", "type": "morning_brief"}) == "brief_push"
    assert mobile_api.notification_open_surface({"surface": "weekly_email"}) == "alert_push"
    assert mobile_api.notification_open_surface({}) == "alert_push"


def test_the_brief_push_carries_its_surface_history_id_and_answerable(db_path, monkeypatch):
    import push, notify
    rid = _rid(db_path)
    fired = []
    monkeypatch.setattr(morning_brief, "recipients", lambda *a, **k: [{"id": 7, "email": "o@x.test", "role": "owner"}])
    monkeypatch.setattr(push, "get_device_tokens", lambda *a, **k: [{"user_id": 7, "token": "t"}])
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: fired.append(k["data"]))
    monkeypatch.setattr(notify, "record_notification", lambda *a, **k: 41)
    monkeypatch.setattr(morning_brief, "build", lambda *a, **k: {"date": "2026-09-23", "lines": [
        {"key": "schedule", "tone": "action", "rec": "schedule:next-week", "text": "Build it.", "ask": "Build?"},
        {"key": "money", "tone": "neutral", "rec": "money:labor", "text": "Money.", "ask": "How?"}]})
    monkeypatch.setattr(morning_brief, "_dedupe", lambda rid, b, db: b)
    morning_brief.deliver(rid, db_path=db_path)
    assert fired[0]["surface"] == "brief_push" and fired[0]["alert_id"] == 41
    assert fired[0]["rec_key"] == "schedule:next-week" and fired[0]["answerable"] is True
    # The money ranking line is shown but never presented.
    assert _shown(db_path, rid) == [("schedule:next-week", "brief_push")]


def test_the_brief_email_does_not_carry_a_key_nobody_can_answer():
    assert "rec=" not in morning_brief._ask_url("How?", "money:labor")
    assert "rec=schedule%3Anext-week&src=brief_email" in morning_brief._ask_url("Build?", "schedule:next-week")


# ── #25 the Labor read ──────────────────────────────────────────────────────

LABOR_NOTE = ("Sam, labor ran 34% against 30%.\n\nRecommendations:\n1. Trim one server from Tuesday dinner.\n"
              "2. Move Ana's Saturday close to Friday.\n3. Cap overtime at 40 hours for the cooks.")


@pytest.fixture
def http(monkeypatch, db_path):
    rid = _rid(db_path, module_labor=1)
    user = _user(rid)
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: user, raising=False)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    app.register_blueprint(strategy_routes.strategy_bp)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    c = app.test_client()
    c.rid = rid
    return c


_BEARER = {"Authorization": "Bearer t"}


def _labor_stubs(monkeypatch):
    import labor
    analysis = {"is_live": True, "total_sales": 10000, "total_labor_cost": 3400, "overall_labor_pct": 34.0,
                "labor_target": 30, "period_days": 14, "dow_summary": {"Tuesday": 38.0},
                "overtime_risk": [], "role_summary": {}}
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda rid: dict(analysis))
    monkeypatch.setattr(labor, "labor_note", lambda *a, **k: LABOR_NOTE)
    return analysis


def test_the_web_labor_read_is_keyed_presented_and_answerable(http, db_path, monkeypatch):
    _labor_stubs(monkeypatch)
    body = http.get("/api/labor-insight").get_json()
    items = body["rec_items"]
    assert [i["rec_key"].split(":")[0] for i in items] == ["insight_labor"] * 3
    assert all(i["answerable"] for i in items) and len(body["recs"]) == 3
    assert 'data-rec-key="insight_labor:' in body["insight"]
    shown = _shown(db_path, http.rid, "labor")
    assert sorted(k for k, _ in shown if k.startswith("insight_labor:")) == sorted(i["rec_key"] for i in items)
    # The diagnosis's check is keyed on what it is about.
    assert body["diagnosis"]["rec_key"] == "diag_labor:weekday:tuesday"
    assert ("diag_labor:weekday:tuesday", "labor") in shown
    # Answered on the web, the line is gone from the next load — web and phone.
    rec_ledger.record(http.rid, items[0]["rec_key"], "completed", surface="labor")
    again = http.get("/api/labor-insight").get_json()
    assert "Trim one server" not in again["insight"] and len(again["recs"]) == 2
    phone = http.get("/mobile/api/labor/insight", headers=_BEARER).get_json()
    assert phone["insight_recommendations"][0].startswith("Move Ana")
    assert phone["insight_rec_keys"] == [i["rec_key"] for i in items[1:]]
    assert phone["diagnosis"]["rec_key"] == "diag_labor:weekday:tuesday"


# ── #26 Home's one thing and links ──────────────────────────────────────────

def test_the_cross_module_route_presents_its_one_thing_and_links(http, db_path, monkeypatch):
    link = {"kind": "reviews_x_labor", "subject": "service:friday", "headline": "Friday",
            "modules": ["reviews", "labor"]}
    answered = {"kind": "k", "subject": "old", "headline": "Old", "modules": ["reviews", "food_cost"]}
    ff = {"key": "schedule_to_target:30%", "what": "Build to 30%", "modules": ["labor"], "urgency": "important"}
    monkeypatch.setattr(bi, "executive_brief", lambda *a, **k: {"fix_first": dict(ff), "links": [dict(link), dict(answered)]})
    rec_ledger.record(http.rid, "link:k:old", "dismissed", surface="home", meta={"kind": "not_for_us"})
    body = http.get("/api/cross-module").get_json()
    assert body["fix_first"]["rec_key"] == "schedule_to_target:30%" and body["fix_first"]["answerable"] is True
    assert [l["rec_key"] for l in body["links"]] == ["link:reviews_x_labor:service:friday"]
    assert body["links"][0]["answerable"] is True
    assert _shown(db_path, http.rid, "home") == sorted([("link:reviews_x_labor:service:friday", "home"),
                                                         ("schedule_to_target:30%", "home")])


def test_owed_replies_as_the_one_thing_are_shown_but_not_presented(http, db_path, monkeypatch):
    ff = {"key": "urgent_reviews", "what": "Reply to the 2 reviews", "modules": ["reviews"], "urgency": "critical"}
    monkeypatch.setattr(bi, "executive_brief", lambda *a, **k: {"fix_first": dict(ff), "links": []})
    body = http.get("/mobile/api/cross-module", headers=_BEARER).get_json()
    assert body["fix_first"]["rec_key"] == "urgent_reviews" and body["fix_first"]["answerable"] is False
    assert _shown(db_path, http.rid) == []


# ── #32 keyed links and the open they record ────────────────────────────────

def test_the_alert_email_button_and_sms_name_the_recommendation(monkeypatch):
    import notify
    monkeypatch.setenv("SMS_TRACKED_LINKS", "1")
    url = notify.alert_url("labor_over", rec="labor_over:2026-09-14", src="alert_email")
    assert "tab=labor" in url and "rec=labor_over%3A2026-09-14&src=alert_email" in url
    html = notify._resolve_cta(f'<a href="{notify.CTA_PLACEHOLDER}">x</a>', "1star", 12, rec="review:12")
    assert "?tab=reviews&review=12&rec=review%3A12&src=alert_email" in html
    sms = "🔴 1★ Review — Gia Mia\n\"Cold food\"\nRespond now · dashboard.cavnar.ai"
    keyed = notify.keyed_sms_text(sms, "review:12")
    assert keyed.endswith("Respond now · dashboard.cavnar.ai/?rec=review%3A12&src=alert_sms")
    assert notify.keyed_sms_text(sms, None) == sms
    # Off by default: the keyed address can add a billed segment (Will's call).
    monkeypatch.delenv("SMS_TRACKED_LINKS")
    assert notify.keyed_sms_text(sms, "review:12") == sms
    # The push body is built from the plain text, never the keyed link.
    assert "rec=" not in notify.push_body(sms, "1★ review")


def test_a_delivered_alert_sends_the_keyed_sms_and_email(db_path, monkeypatch):
    import notify
    monkeypatch.setenv("SMS_TRACKED_LINKS", "1")
    rid = _rid(db_path)
    c = _conn(db_path)
    c.execute("UPDATE restaurants SET urgent_via_sms=1, urgent_via_email=1 WHERE id=?", (rid,))
    c.commit(); c.close()
    notify.add_alert_contact(rid, "Sam", "+15555550100", sms_consent=True, db_path=db_path)
    texts, mails = [], []
    monkeypatch.setattr(notify, "send_sms", lambda to, body: texts.append(body) or True)
    monkeypatch.setattr(notify, "_send_alert_email", lambda to, subj, html, **k: mails.append(html) or True)
    monkeypatch.setattr(notify, "brief_pushed_emails", lambda *a, **k: set())
    notify.deliver_alert(rid, "labor_over", "Labor over · dashboard.cavnar.ai", "Labor over",
                         notify._alert_email_html("Gia Mia", "Labor", ["x"]), db_path=db_path,
                         recs=[notify.alert_rec("labor_over", subject="2026-09-14")])
    assert texts and "dashboard.cavnar.ai/?rec=labor_over%3A2026-09-14&src=alert_sms" in texts[0]
    assert mails and "?tab=labor&rec=labor_over%3A2026-09-14&src=alert_email" in mails[0]


def test_a_dashboard_load_from_a_keyed_link_records_one_open(db_path):
    rid = _rid(db_path)
    user = _user(rid)
    assert rec_delivery.record_link_open(user, {"rec": "labor_over:x", "src": "alert_sms"}) is False  # never shown
    rec_ledger.present(rid, "labor_over:x", "labor", "alert_sms")
    assert rec_delivery.record_link_open(user, {"rec": "labor_over:x", "src": "alert_sms"}) is True
    assert rec_delivery.record_link_open(user, {"rec": "labor_over:x", "src": "alert_sms"}) is False  # same day
    # With a question in it, dashboard.html's ?ask= handler records the open.
    assert rec_delivery.record_link_open(user, {"rec": "labor_over:x", "src": "weekly_email", "ask": "q"}) is False
    assert [(e["key"], e["surface"]) for e in _events(db_path, rid, "opened")] == [("labor_over:x", "alert_sms")]


def test_the_sign_in_redirect_keeps_the_links_query():
    src = open("hosted_dashboard.py").read()
    assert "nxt = request.full_path if request.query_string else request.path" in src
    assert "rec_delivery.record_link_open(current_user, request.args)" in src


# ── #41 the remaining sources ───────────────────────────────────────────────

def test_content_calendar_ideas_are_presented_and_writing_one_accepts_it(http, db_path, monkeypatch):
    import marketing
    ideas = [{"day": "Tuesday", "angle": "Tuesday pasta night", "type": "instagram_post"},
             {"day": "Friday", "angle": "Wine flight", "type": "instagram_post"}]
    monkeypatch.setattr(marketing, "get_content_calendar_ideas", lambda **k: [dict(i) for i in ideas])
    body = http.get("/api/content-calendar").get_json()
    keys = [i["rec_key"] for i in body["ideas"]]
    assert all(k.startswith("content_idea:") for k in keys) and all(i["answerable"] for i in body["ideas"])
    assert sorted(k for k, _ in _shown(db_path, http.rid, "marketing")) == sorted(keys)
    marketing.mark_calendar_idea_used(http.rid, "instagram_post", "Tuesday pasta night")
    again = http.get("/api/content-calendar").get_json()["ideas"]
    assert [i["answered"] for i in again] == [True, False]
    assert [e["key"] for e in _events(db_path, http.rid, "accepted")] == [keys[0]]


def test_the_ai_visibility_roadmap_is_built_on_the_server_and_presented(db_path):
    rid = _rid(db_path)
    payload = {"ok": True, "presence_score": 60, "social_posts_30d": 9, "review_total": 38, "resp_rate": 80,
               "checklist": [{"label": "Build to 50+ Google reviews (38 so far)", "done": False},
                             {"label": "Excellent review response rate (80%)", "done": True}]}
    cards = client_api.ai_visibility_roadmap(payload)
    assert [c["key"] for c in cards] == ["aiv_roadmap:reviews", "aiv_roadmap:gbp", "aiv_roadmap:responses",
                                         "aiv_roadmap:social"]
    assert [c["done"] for c in cards] == [False, False, True, True]
    assert cards[0]["detail"] == "38 of 50 reviews — 12 more to go"
    out = client_api.present_ai_visibility_roadmap(rid, dict(payload), user_id=7)
    assert [c["answerable"] for c in out["roadmap"]] == [True, True, False, False]
    assert _shown(db_path, rid) == [("aiv_roadmap:gbp", "intel"), ("aiv_roadmap:reviews", "intel")]


def test_asks_visibility_read_presents_no_roadmap(db_path, monkeypatch):
    import ask_cavnar_tools
    rid = _rid(db_path)
    monkeypatch.setattr(client_api, "_do_ai_visibility",
                        lambda rid: ({"ok": True, "checklist": [], "ai_score": 10}, 200))
    ask_cavnar_tools._read_ai_visibility(rid)
    assert _shown(db_path, rid) == []


def test_loss_flags_are_keyed_owner_only_and_presented_on_home(db_path, monkeypatch):
    import loss_detection
    rid = _rid(db_path)
    week = "2026-09-14"
    sig = {"available": True, "week": [week, "2026-09-20"], "note": "n",
           "flagged": [{"type": "spike", "key": f"loss:{week}:comp:spike", "headline": "Comps ran 3x",
                        "alternative": "a"}], "kinds": []}
    monkeypatch.setattr(loss_detection, "signals", lambda *a, **k: json.loads(json.dumps(sig)))
    out, status = strategy_routes._do_loss_signals(_user(rid))
    assert status == 200 and out["flagged"][0]["rec_key"] == f"loss:{week}:comp:spike"
    assert out["flagged"][0]["answerable"] is True
    assert _shown(db_path, rid) == [(f"loss:{week}:comp:spike", "home")]
    _, status = strategy_routes._do_loss_signals(_user(rid, uid=8, role="manager"))
    assert status == 403


def test_the_loss_detector_keys_its_flags_like_the_issue_it_files():
    src = open("loss_detection.py").read()
    assert '"key": f"loss:{week_start.isoformat()}:{kind}:spike"' in src
    assert '"key": f"loss:{week_start.isoformat()}:{kind}:{top}"' in src
    assert 'source_key=f"loss:{week}:{entry[\'kind\']}:{f.get(\'approver\')}"' in open("strategy_jobs.py").read()


def test_a_delivered_draft_presents_its_standby_and_overtime_moves(db_path):
    rid = _rid(db_path)
    result = {"standby_days": [{"date": "2026-09-26", "day": "Saturday", "standby": {"employee": "Ana", "role": "Server"}}],
              "overtime_forecast": [{"employee": "Bo", "text": "Bo hits 44h", "candidate": {"date": "2026-09-27", "saves": 60}},
                                    {"employee": "Cy", "text": "Cy hits 41h"}]}
    client_api.present_schedule_result(rid, result, user_id=7)
    assert result["standby_days"][0]["rec_key"] == "standby:2026-09-26:ana"
    assert result["standby_days"][0]["answerable"] is False          # bookkeeping: its answer is the on-call ask
    assert result["overtime_forecast"][0]["rec_key"] == "overtime_move:Bo:2026-09-27"
    assert "rec_key" not in result["overtime_forecast"][1]
    assert _shown(db_path, rid) == [("overtime_move:Bo:2026-09-27", "schedule_review"),
                                    ("standby:2026-09-26:ana", "schedule_review")]


def test_the_status_poll_presents_only_a_finished_draft():
    for src in (open("client_api.py").read(), open("mobile_api.py").read()):
        assert 'if job["status"] == "done":' in src
        assert "present_schedule_result(current_user[\"restaurant_id\"], result, current_user.get(\"id\"))" in src


# ── #40 the owner's edits to reply drafts ───────────────────────────────────

def test_compare_is_deterministic_and_reads_the_edit():
    import reply_edits
    draft = ("Hi Maria! Thank you so much for the kind words about our carbonara! We are so sorry the wait was "
             "long on Friday. We hope to see you again soon!")
    final = "Maria, thanks for the kind words about the carbonara. See you next time."
    a, b = reply_edits.compare(draft, final), reply_edits.compare(draft, final)
    assert a == b
    assert a["category"] in ("heavy", "rewrite") and 0 < a["distance"] <= 1
    assert "shortened" in a["signals"] and "removed_exclamations" in a["signals"]
    assert "removed_apology" in a["signals"]
    same = reply_edits.compare(draft, draft)
    assert same == {"distance": 0.0, "category": "unchanged", "words_before": same["words_before"],
                    "words_after": same["words_before"], "signals": []}


def test_the_style_note_needs_a_pattern_and_is_stable():
    import reply_edits
    edit = {"category": "heavy", "signals": ["shortened", "removed_exclamations"], "words_after": 20}
    assert reply_edits.style_note([edit, edit]) == ""                                  # two is not a pattern
    note = reply_edits.style_note([edit, edit, dict(edit, signals=["shortened"]), dict(edit, words_after=30)])
    assert note == reply_edits.style_note([edit, edit, dict(edit, signals=["shortened"]), dict(edit, words_after=30)])
    assert "OWNER'S EDITS" in note and "they cut the draft down" in note
    assert "they take out exclamation marks" in note and "about 20 words" in note
    assert "{" not in note and "Maria" not in note


def _approved_review(db_path, rid, ext, draft, final, action="approved_as_is", edited=1):
    c = _conn(db_path)
    cur = c.execute(
        "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, fetched_at, "
        "response_status, draft_response, original_draft, draft_edited) "
        "VALUES (?, 'google', ?, 'A', 5, 'Good', '2026-09-01', '2026-09-01', 'drafted', ?, ?, ?)",
        (rid, ext, final, draft if edited else None, edited))
    c.commit()
    rid_ = cur.lastrowid
    c.close()
    return rid_


def test_an_approval_records_the_edit_and_edited_replies_lead_the_examples(db_path, monkeypatch):
    import drafter
    rid = _rid(db_path)
    monkeypatch.setattr(client_api, "get_conn", lambda *a, **k: models.get_conn())
    long_draft = "Thank you so much for coming in! We loved having you and hope to see you again soon! Cheers!"
    ids = [_approved_review(db_path, rid, f"e{i}", long_draft, f"Thanks for coming in, see you soon {i}.")
           for i in range(3)]
    plain = _approved_review(db_path, rid, "p", None, "Thanks!", edited=0)
    app = Flask(__name__)
    with app.test_request_context("/"):
        for i in ids + [plain]:
            payload, status = client_api._do_approve(i, rid)
            assert status == 200, payload
    c = _conn(db_path)
    rows = {r["id"]: r for r in c.execute("SELECT id, edit_category, edit_signals FROM reviews").fetchall()}
    c.close()
    assert all(rows[i]["edit_category"] in ("heavy", "rewrite", "light") for i in ids)
    assert rows[plain]["edit_category"] == "unchanged"
    examples = models.get_approved_examples(rid, limit=4)
    assert examples[-1]["response"] == "Thanks!"                     # the owner's own words first
    note = drafter.get_owner_edit_note(rid)
    assert "OWNER'S EDITS" in note and "they take out exclamation marks" in note


# ── #48 Ask: keyed suggestions and "was this useful?" ───────────────────────

ANSWER = ("Labor ran hot on Tuesday.\n\n1. **Trim** one server from Tuesday dinner.\n"
          "2. Move Ana's close to Friday to save $120.\n- Labor has been high.\n- Call the fish supplier today.")


def test_suggestions_are_read_deterministically_from_the_answer():
    import ask_cavnar
    got = ask_cavnar.extract_suggestions(ANSWER)
    assert [g["text"] for g in got] == ["Trim one server from Tuesday dinner.",
                                        "Move Ana's close to Friday to save $120.", "Call the fish supplier today."]
    # A line carrying a figure the answer's own check could not trace is dropped.
    assert [g["text"] for g in ask_cavnar.extract_suggestions(ANSWER, ["$120"])] == [
        "Trim one server from Tuesday dinner.", "Call the fish supplier today."]
    assert ask_cavnar.extract_suggestions("Labor is at 31%. Nothing to change.") == []


def test_suggestions_are_presented_on_ask_and_an_answered_one_drops(db_path):
    import ask_cavnar
    rid = _rid(db_path)
    first = ask_cavnar.record_suggestions(rid, ANSWER, {"unverified_figures": []}, user_id=7)
    assert len(first) == 3 and all(s["answerable"] and s["rec_key"].startswith("ask_tip:") for s in first)
    assert sorted(k for k, _ in _shown(db_path, rid, "ask")) == sorted(s["rec_key"] for s in first)
    rec_ledger.record(rid, first[0]["rec_key"], "dismissed", surface="ask", meta={"kind": "not_for_us"})
    again = ask_cavnar.record_suggestions(rid, ANSWER, {}, user_id=7)
    assert [s["rec_key"] for s in again] == [s["rec_key"] for s in first[1:]]


def _answer(db_path, rid, user_id=7):
    cid = models.save_ask_message(rid, "user", "How is labor?", user_id=user_id)
    models.save_ask_message(rid, "assistant", "Labor ran hot.", user_id=user_id, conversation_id=cid)
    return models.latest_ask_answer_id(rid, cid, user_id=user_id)


def test_ask_feedback_is_recorded_replaced_and_summarised(http, db_path):
    mid = _answer(db_path, http.rid)
    r = http.post("/api/ask-cavnar/feedback", json={"message_id": mid, "helpful": False, "note": "Too long"})
    body = r.get_json()
    assert r.status_code == 200 and body["feedback"]["helpful"] is False and body["feedback"]["note"] == "Too long"
    r = http.post("/mobile/api/ask-cavnar/feedback", json={"turn_id": mid, "helpful": True}, headers=_BEARER)
    assert r.status_code == 200 and r.get_json()["summary"]["rated"] == 1
    assert r.get_json()["summary"]["helpful"] == 1
    import ask_cavnar
    assert "1 of 1 answers rated helpful" in ask_cavnar._feedback_context(http.rid)


def test_ask_feedback_refuses_another_restaurants_or_another_logins_answer(http, db_path):
    other = _rid(db_path, name="Elsewhere")
    theirs = _answer(db_path, other, user_id=99)
    r = http.post("/api/ask-cavnar/feedback", json={"message_id": theirs, "helpful": True})
    assert r.status_code == 404
    colleague = _answer(db_path, http.rid, user_id=8)
    assert http.post("/api/ask-cavnar/feedback", json={"message_id": colleague, "helpful": True}).status_code == 404
    c = _conn(db_path)
    assert c.execute("SELECT COUNT(*) FROM ask_feedback").fetchone()[0] == 0
    c.close()
    assert http.post("/api/ask-cavnar/feedback", json={"message_id": "x", "helpful": True}).status_code == 400
    mine = _answer(db_path, http.rid)
    assert http.post("/api/ask-cavnar/feedback", json={"message_id": mine, "helpful": "yes"}).status_code == 400


def test_the_ask_feedback_table_is_created_at_boot():
    src = open("models.py").read()
    assert "CREATE TABLE IF NOT EXISTS ask_feedback" in src
    assert "CREATE TABLE IF NOT EXISTS ask_feedback" not in open("strategy_routes.py").read()
