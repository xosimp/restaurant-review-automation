"""The recommendation-trust audit: why owners ignored Cavnar's
recommendations, and the fixes on Home, the morning brief, the reviews and
the value ledger.

Each test names the audit item it pins. The common thread is rec_ledger:
one identity per recommendation, so an answer anywhere holds everywhere and
the same news is said once."""
import json
import uuid
from datetime import date, datetime, timedelta

import pytest

import auth
import business_intelligence as bi
import client_api
import decisions
import home_brief
import mobile_api
import models
import rec_ledger
from auth import init_auth
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import review_intelligence, food_cost_intelligence, morning_brief, action_queue, outcomes, issues
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, mobile_api, home_brief, bi, review_intelligence,
                food_cost_intelligence, morning_brief, action_queue, outcomes, issues):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    import ai_utils
    c = get_conn(db_path); c.executescript(ai_utils._USAGE_TABLE_SQL); c.commit(); c.close()
    home_brief.invalidate()
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("AI called")))


def _rid(db_path, **kw):
    fields = dict(name="Trust Co", owner_email="o@x.test", owner_name="Sam Owner", module_reviews=1)
    fields.update(kw)
    return create_restaurant(Restaurant(**fields), db_path=db_path)


def _user(rid, uid=1, role="owner"):
    return {"id": uid, "restaurant_id": rid, "base_restaurant_id": rid, "username": "owner", "role": role,
            "is_admin": 0, "email": "o@x.test"}


def _review(db_path, rid, rating, status="pending", days_ago=1, urgency="normal", cats=None):
    c = get_conn(db_path)
    # A drafted review has its draft: Home counts only replies a publish can
    # actually post (models.reply_queue_counts, re-audit M-2).
    c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
              "fetched_at, sentiment, urgency, response_status, processed, categories, draft_response) "
              "VALUES (?, 'google', ?, 'A', ?, 'text', date('now', ?), datetime('now'), ?, ?, ?, 1, ?, ?)",
              (rid, uuid.uuid4().hex, rating, f"-{days_ago} days", "negative" if rating <= 2 else "positive",
               urgency, status, json.dumps(cats) if cats else None,
               "Thank you!" if status == "drafted" else None))
    c.commit(); c.close()


# ── #2 overtime: this week only, the real premium, live data only ──────────

def _labor(today, live=True):
    wk = (today - timedelta(days=today.weekday())).isoformat()
    last = (today - timedelta(days=today.weekday() + 7)).isoformat()
    return {"is_live": live, "week_start_day": 0, "overtime_hours": 20.0, "overtime_premium": 200.0,
            "overtime_risk": [
                {"employee": "Ana", "hours": 46.0, "week_start": wk, "status": "overtime"},
                {"employee": "Bo", "hours": 44.0, "week_start": wk, "status": "overtime"},
                {"employee": "Cy", "hours": 50.0, "week_start": last, "status": "overtime"},
                {"employee": "Di", "hours": 38.0, "week_start": wk, "status": "near"}]}


def test_overtime_counts_this_week_and_prices_it_from_the_analysis():
    today = date(2026, 9, 23)
    ot = home_brief.overtime_this_week(_labor(today), today)
    assert ot["people"] == 2 and ot["hours"] == 10.0
    # $200 premium over 20 OT hours in the period = $10/hour, x 10 hours this week
    assert ot["premium"] == 100.0
    assert home_brief.overtime_this_week(_labor(today, live=False), today) is None


def test_the_phone_never_shows_sample_overtime(db_path, monkeypatch):
    """mobile_api had no is_live check: sample shifts read "7 staff members
    in overtime" on a brand-new account."""
    import labor
    rid = _rid(db_path, module_labor=1)
    today = date.today()
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant",
                        lambda *a, **k: dict(_labor(today, live=False), overall_labor_pct=40))
    monkeypatch.setattr(home_brief, "build_home_brief", lambda *a, **k: ({}, 500))
    payload, _ = mobile_api._do_mobile_home(_user(rid))
    assert not any(i["type"] == "labor_overtime" for i in payload["needs_attention"])
    assert "38" not in json.dumps(payload["needs_attention"])


# ── #3 money surfaced: dollars only, role-filtered ─────────────────────────

def test_money_surfaced_sums_only_dollar_alerts_and_respects_the_role(db_path):
    rid = _rid(db_path)
    c = get_conn(db_path)
    for t, v in (("food_waste", 180.0), ("critical_low", 3.0), ("price_spike", 12.0),
                 ("demand_opportunity", 6400.0)):
        c.execute("INSERT INTO alert_log (restaurant_id, alert_type, value) VALUES (?,?,?)", (rid, t, v))
    c.commit(); c.close()
    out = models.money_surfaced(rid, db_path=db_path)
    assert out["dollars"] == 180.0 and [i["alert_type"] for i in out["items"]] == ["food_waste"]
    assert models.money_surfaced(rid, db_path=db_path, denied_modules={"inventory"})["dollars"] == 0.0
    import value_delivered
    assert value_delivered.breakdown(rid, db_path=db_path, denied_modules={"inventory"})["surfaced"]["dollars"] == 0.0


# ── #4 one confidence per card ──────────────────────────────────────────────

def test_a_card_has_one_confidence_from_its_own_evidence():
    from intelligence.confidence import card_confidence
    c = card_confidence("high", "12 reviews in 90 days", {"band": "low", "factors": []})
    assert c["band"] == "high" and c["label"] == "High confidence" and c["caution"] is None
    # only a MEASURED record at this restaurant moves it, and by one band
    # (read from the factor's structured `measured`, never by sniffing its
    # note — confidence audit E4)
    down = card_confidence("high", "x", {"factors": [{"name": "restaurant_history", "value": 0.2, "measured": 3,
                                                     "improved": 0,
                                                     "note": "this restaurant: 0 of 3 measured improved"}]})
    assert down["band"] == "medium" and down["adjusted"] == "down"
    assert "held" not in down["reason"] and "0 of 3 measured here improved" in down["reason"]
    unmeasured = card_confidence("high", "x", {"factors": [{"name": "restaurant_history", "value": 0.1, "measured": 0,
                                                           "note": "this restaurant: 0 of 2 accepted, none measured yet"}]})
    assert unmeasured["band"] == "high"
    # a note that merely SAYS "measured" moves nothing
    sniff = card_confidence("high", "x", {"factors": [{"name": "restaurant_history", "value": 0.1,
                                                      "note": "this restaurant: 0 of 3 measured improved"}]})
    assert sniff["band"] == "high"
    # an unknown band is low, never medium (CA6 duplicate #2)
    assert card_confidence("bogus", "x", None)["band"] == "low"
    # no stand-in score: the constant 0.3/0.55/0.8 map is gone
    assert "score" not in c


def test_home_cards_never_print_two_confidences(db_path):
    rid = _rid(db_path)
    for _ in range(12):
        _review(db_path, rid, 1, cats=["service"])
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    rec = next(r for r in p["recommendations"] if r["key"].startswith("top_issue:"))
    assert rec["confidence"]["label"] and rec["confidence"]["reason"]
    assert "confidence" not in (rec["evidence"] or "")
    for field in ("why", "timeframe", "impact", "if_ignored"):
        assert rec.get(field), field
    # One verdict per card: the separate `strength` pill is gone (CA4 F1),
    # the measured confidence (K1) is the only one.
    assert "strength" not in rec
    assert set(rec["confidence"]["dimensions"]) == {"evidence", "accuracy", "freshness"}


# ── #5 the one thing is an action, ranked by urgency x dollars ─────────────

def test_the_one_thing_is_a_concrete_action_and_urgent_reviews_beat_a_small_food_line(db_path):
    rid = _rid(db_path, module_inventory=1)
    for _ in range(5):
        _review(db_path, rid, 1)
    data = {"reviews": {"brief": {}, "diagnoses": []}, "labor": {}, "food_cost": {"brief": {"fix_first": {
        "what": "Salmon Fillet waste above tolerance", "dollars_monthly": 124.0, "confidence": "high",
        "difficulty": "low", "evidence": "wasted 20%", "if_ignored": "the same share keeps going in the bin"}}}}
    ranked = bi.one_thing_candidates(rid, data, [], db_path=db_path)
    assert ranked[0]["key"] == "urgent_reviews" and ranked[0]["urgency"] == "critical"
    food = next(c for c in ranked if c["modules"] == ["food_cost"])
    assert food["what"] == "Cut the Salmon Fillet order — waste is above tolerance"
    assert food["what"] != "Food cost drivers"
    first = bi.pick_one_thing(rid, ranked, db_path=db_path)
    assert first["key"] == "urgent_reviews"
    # answered on Home: the one thing moves on to the next action
    rec_ledger.record(rid, "urgent_reviews", "dismissed", surface="home", meta={"kind": "hide"}, db_path=db_path)
    assert bi.pick_one_thing(rid, ranked, db_path=db_path)["key"] == food["key"]


def test_driver_actions_are_verb_first_for_every_driver_shape():
    assert bi.driver_action({"label": "Ribeye price up 12%"}) == "Get a second quote on Ribeye — the price is up 12%"
    assert bi.driver_action({"label": "Mozzarella cheaper from Sysco"}).startswith("Buy Mozzarella from Sysco")
    assert bi.driver_action({"label": "Risotto runs at 44% food cost"}).startswith("Re-cost or reprice Risotto")
    assert bi.driver_action({"kind": "portion", "item": "Shrimp", "label": "x"}).startswith("Check Shrimp portions")


# ── #6 only recent reviews are owed a reply ────────────────────────────────

def test_home_counts_only_recent_drafts_and_names_the_older_ones(db_path):
    rid = _rid(db_path)
    for _ in range(3):
        _review(db_path, rid, 5, status="drafted")
    for _ in range(40):
        _review(db_path, rid, 4, status="drafted", days_ago=400)
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    a = next(x for x in p["attention"] if x["key"] == "awaiting_approval")
    assert a["title"].startswith("3 replies drafted")
    assert "40 older drafts not counted" in a["evidence"]
    # the tap publishes the number on the label, not 25 including history
    assert a["action"]["label"] == "Publish 3 replies" and a["action"]["count"] == 3
    # The "Publish the 3 drafted replies" card is the same news as that item,
    # so it is not said twice on one screen (reaudit H-14).
    assert not any(r["key"] == "publish_drafts" for r in p["recommendations"])


def test_the_queue_counts_only_recent_reviews(db_path):
    import action_queue
    rid = _rid(db_path)
    _review(db_path, rid, 5, days_ago=90)
    # A Tuesday: from Thursday on, "next week's schedule" joins the list.
    assert action_queue.items(rid, db_path=db_path, today=date(2026, 9, 22))["items"] == []


# ── #7 the second hide asks why ─────────────────────────────────────────────

def test_times_hidden_reaches_the_card_after_it_comes_back(db_path):
    rid = _rid(db_path)
    for _ in range(12):
        _review(db_path, rid, 1, cats=["service"])
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    key = next(r["key"] for r in p["recommendations"] if r["key"].startswith("top_issue:"))
    home_brief.dismiss(rid, key, user_id=1)
    c = get_conn(db_path)          # the fortnight passes
    c.execute("UPDATE home_dismissals SET expires_at=datetime('now','-1 day') WHERE key=?", (key,))
    c.execute("UPDATE rec_instances SET silenced_until=NULL WHERE key=?", (key,))
    c.commit(); c.close()
    p2, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    assert next(r for r in p2["recommendations"] if r["key"] == key)["times_hidden"] == 1


# ── #10 delivered value credits only real owner actions ────────────────────

def test_an_opened_alert_is_informational_never_delivered_value(db_path):
    import outcomes
    rid = _rid(db_path, module_labor=1)
    t = outcomes.observe(rid, "alert_labor_over", user_id=1, db_path=db_path)
    c = get_conn(db_path)
    c.execute("UPDATE recommendation_outcomes SET status='evaluated', verdict='improved', dollars_monthly=400, "
              "after_value=28, baseline_value=31 WHERE id=?", (t["id"],))
    c.commit(); c.close()
    assert outcomes.realised(rid, db_path=db_path) == []
    assert outcomes.total_value(rid, db_path=db_path)["monthly"] == 0
    row = outcomes.list_outcomes(rid, db_path=db_path)[0]
    assert row["informational"] and "not counted as value" in outcomes.summarise(row)
    # and it never blocks a real owner action on the same metric
    assert outcomes.observe(rid, "schedule_published", user_id=1, db_path=db_path)


def test_that_one_worked_names_real_dates_not_a_window_the_owner_never_set():
    import outcomes
    msg = outcomes.win_message({"title": "Published a schedule — week of 2026-09-07", "metric": "labor_pct",
                                "metric_label": "Labor %", "after_start": "2026-09-08",
                                "after_end": "2026-10-05"})
    assert "window you set" not in msg and "9/8/26" in msg and "10/5/26" in msg and "9/7/26" in msg


# ── #16 dates, #38 deep links ──────────────────────────────────────────────

def test_the_brief_email_reads_m_d_yy_and_its_links_carry_the_key(db_path):
    import morning_brief
    html = morning_brief._email_html({"date": "2026-09-23", "lines": [
        {"key": "reviews", "rec": "no_response", "tone": "bad", "text": "2 reviews", "ask": "Which?"}]}, "Trust Co")
    assert "9/23/26" in html and "2026-09-23" not in html
    assert "rec=no_response" in html and "src=brief_email" in html


def test_home_toasts_format_result_dates():
    s = open("templates/dashboard.html").read()
    a = s.index('id="panel-home"'); b = s.index("<!-- /panel-home -->")
    panel = s[a:b]
    assert "result on '+d.outcome.evaluate_on" not in panel
    assert "mdy(d.outcome.evaluate_on)" in panel
    # The link's open is recorded by the server on page load (re-audit C14);
    # the page no longer posts it too, which counted a tap twice.
    assert "rec_delivery.record_link_open" in panel
    import rec_delivery
    assert callable(rec_delivery.record_link_open)
    swift = open("ios/CavnarAI/CavnarAI/Features/Home/HomeFollowThrough.swift").read()
    assert "result on \\(on)" not in swift and "CavnarDate.mdy(on)" in swift


# ── #17 one "no" everywhere, #18 the same news once a day ──────────────────

def test_a_no_on_home_is_a_no_in_the_brief(db_path):
    import morning_brief
    rid = _rid(db_path)
    _review(db_path, rid, 5)
    r = models.get_restaurant(rid, db_path=db_path)
    keys = lambda: [l["key"] for l in morning_brief.build(rid, restaurant=r, db_path=db_path)["lines"]]
    assert "reviews" in keys()
    home_brief.dismiss(rid, "no_response", kind="not_for_us", user_id=1)
    assert "reviews" not in keys()
    home_brief.undismiss(rid, "no_response")
    assert "reviews" in keys()


def test_the_brief_drops_what_home_already_said_today_unless_critical(db_path):
    import morning_brief
    rid = _rid(db_path)
    brief = {"lines": [{"key": "schedule", "rec": "schedule:next-week", "text": "x", "tone": "action"},
                       {"key": "reviews", "rec": "no_response", "critical": True, "text": "y", "tone": "bad"},
                       {"key": "today", "text": "z", "tone": "neutral"}]}
    rec_ledger.present_many(rid, [{"key": "schedule:next-week", "module": "labor"},
                                  {"key": "no_response", "module": "reviews"}], "home", db_path=db_path)
    out = morning_brief._dedupe(rid, brief, db_path=db_path)
    assert [l["key"] for l in out["lines"]] == ["reviews", "today"]
    # the brief's own showings never count against it
    rid2 = _rid(db_path)
    rec_ledger.present_many(rid2, [{"key": "schedule:next-week", "module": "labor"}], "brief_push", db_path=db_path)
    assert len(morning_brief._dedupe(rid2, brief, db_path=db_path)["lines"]) == 3


def test_weekly_priorities_leave_out_what_the_owner_answered(db_path, monkeypatch):
    import weekly_review
    rid = _rid(db_path)
    monkeypatch.setattr(bi, "executive_brief", lambda *a, **k: {"money": {"ranked": [
        {"key": "money:labor", "label": "Scheduling against target", "monthly": 900},
        {"key": "money:food_cost", "label": "Food cost drivers", "monthly": 300}]}, "fix_first": None})
    rec_ledger.record(rid, "money:labor", "dismissed", surface="home", meta={"kind": "not_for_us"}, db_path=db_path)
    review = weekly_review.build(rid, today=date(2026, 9, 23), db_path=db_path)
    assert [p["key"] for p in review["priorities"]] == ["money:food_cost"]


# ── #19 / #42 one complaint, one instruction, with its alternative ─────────

def test_top_issue_uses_the_diagnosis_recommended_action_and_alternative(db_path):
    rid = _rid(db_path)
    for _ in range(12):
        _review(db_path, rid, 1, cats=["service"])
    c = get_conn(db_path)
    c.execute("INSERT INTO review_diagnoses (restaurant_id, category, window_days, mention_count, cause, "
              "alternative_cause, confidence, recommended_action) VALUES (?, 'service', 90, 12, "
              "'Friday dinner is short one server.', 'The kitchen is slow on Fridays.', 'medium', "
              "'Add a second server on Friday from 6 to 9.')", (rid,))
    c.commit(); c.close()
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    # The diagnosis's action carries the Reviews card's own key, so one
    # answer holds on Home and in the module (re-audit M-9).
    rec = next(r for r in p["recommendations"] if r["key"] == "diag_review:service")
    assert rec["title"] == "Add a second server on Friday from 6 to 9"
    assert rec["alternative"] == "The kitchen is slow on Fridays."
    assert rec["confidence"]["band"] == "medium" and rec["model_written"]
    assert "most-mentioned complaint" not in rec["title"]


# ── #24 order, #45 quieter, #36 at stake ───────────────────────────────────

def test_recommendations_rank_by_urgency_dollars_and_ease_not_module():
    recs = [{"key": "post_this_week", "timeframe": "This week", "effort": "low", "confidence": {"band": "medium"}},
            {"key": "food_cost_driver:Salmon", "timeframe": "This week", "effort": "low", "dollars_monthly": 600,
             "confidence": {"band": "high"}},
            {"key": "top_issue:service", "timeframe": "This week", "effort": "medium", "confidence": {"band": "low"}}]
    assert [r["key"] for r in home_brief.order_recommendations(recs)][0] == "food_cost_driver:Salmon"
    quiet = home_brief.order_recommendations(recs, quiet_kinds={"food_cost_driver"})
    assert quiet[2]["key"] == "food_cost_driver:Salmon" and quiet[2]["quiet"]


def test_a_kind_ignored_four_times_goes_quiet_until_restored(db_path):
    rid = _rid(db_path)
    c = get_conn(db_path)
    for i in range(4):
        c.execute("INSERT INTO rec_instances (rec_id, restaurant_id, key, kind, status, created_at) "
                  "VALUES (?, ?, ?, 'post_this_week', 'expired', datetime('now', ?))",
                  (uuid.uuid4().hex, rid, "post_this_week", f"-{40 - i * 7} days"))
    c.commit(); c.close()
    assert decisions.quiet_kinds(rid, db_path=db_path) == {"post_this_week"}
    assert decisions.restore_kind(rid, "post_this_week", db_path=db_path)
    assert decisions.quiet_kinds(rid, db_path=db_path) == set()


def test_a_quiet_kind_never_leads_the_brief(db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(decisions, "quiet_kinds", lambda *a, **k: {"trim_day"})
    cands = [{"key": "trim_day:Friday", "what": "Trim Friday"}, {"key": "top_issue:service", "what": "Fix it"}]
    assert bi.pick_one_thing(rid, cands, db_path=db_path)["key"] == "top_issue:service"


def test_at_stake_counts_each_ingredient_once():
    drivers = [{"item": "Salmon", "dollars_monthly": 300}, {"item": "salmon", "dollars_monthly": 120},
               {"item": "Ribeye", "dollars_monthly": 90}, {"dollars_monthly": 10}]
    assert home_brief.at_stake_monthly(drivers) == 400.0


# ── #37 impressions, needs-attention answers ───────────────────────────────

def test_home_records_every_card_and_attention_item_as_shown(db_path):
    rid = _rid(db_path, module_marketing=1)
    for _ in range(4):
        _review(db_path, rid, 5, status="drafted")
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    c = get_conn(db_path)
    shown = {r["key"] for r in c.execute("SELECT key FROM rec_events WHERE restaurant_id=? AND event='shown' "
                                         "AND surface='home'", (rid,))}
    c.close()
    # Every item the owner can ANSWER is recorded; a setup/health nudge
    # (no social account connected) is not advice and is never recorded
    # (re-audit B7).
    assert {a["rec_key"] for a in p["attention"] if a["answerable"]} <= shown
    assert {r["key"] for r in p["recommendations"] if r["answerable"]} <= shown
    assert "no_response" in shown, "Home's drafted-replies item carries the brief's key"
    social = next(a for a in p["attention"] if a["key"] == "social_not_connected")
    assert social["answerable"] is False and "social_not_connected" not in shown


def test_needs_attention_can_be_answered_except_a_critical_item(db_path):
    rid = _rid(db_path, module_marketing=1)
    _review(db_path, rid, 1, urgency="high")
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    social = next(a for a in p["attention"] if a["key"] == "social_not_connected")
    urgent = next(a for a in p["attention"] if a["key"] == "urgent_reviews")
    assert social["dismissable"] and not urgent["dismissable"]
    home_brief.dismiss(rid, "social_not_connected", kind="snooze", user_id=1)
    home_brief.dismiss(rid, "urgent_reviews", user_id=1)
    p2, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    keys = [a["key"] for a in p2["attention"]]
    assert "social_not_connected" not in keys and "urgent_reviews" in keys
    assert rec_ledger.silenced(rid, "social_not_connected", db_path=db_path)


# ── #43 hand a card to someone ─────────────────────────────────────────────

def test_assigning_a_card_opens_an_issue_keyed_to_it(db_path, monkeypatch):
    import issues
    rid = _rid(db_path)
    c = get_conn(db_path)
    cid = c.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) "
                    "VALUES (?, 'Gina GM', '+15555550100', 1)", (rid,)).lastrowid
    c.commit(); c.close()
    monkeypatch.setattr(issues, "_notify", lambda *a, **k: None)
    assert home_brief.assignees(rid) == [{"id": cid, "name": "Gina GM"}]
    out, status = home_brief.assign(rid, "trim_day:Friday", "Trim Friday staffing", cid, user_id=1)
    assert status == 200 and out["issue"]["assignee_name"] == "Gina GM"
    c = get_conn(db_path)
    row = c.execute("SELECT source_key FROM ops_issues WHERE id=?", (out["issue"]["id"],)).fetchone()
    ev = c.execute("SELECT meta FROM rec_events WHERE key='trim_day:Friday' AND event='accepted'").fetchone()
    c.close()
    assert row["source_key"] == "trim_day:Friday" and json.loads(ev["meta"])["delegated"] is True
    assert home_brief.assign(rid, "x", "y", 99999)[1] == 400


def test_assign_route_is_owner_only(db_path, monkeypatch):
    from flask import Flask
    rid = _rid(db_path)
    app = Flask(__name__); app.register_blueprint(client_api.client_bp)
    monkeypatch.setattr(auth, "get_current_user", lambda: _user(rid, role="manager"))
    r = app.test_client().post("/api/home/assign", json={"key": "k", "title": "t", "contact_id": 1})
    assert r.status_code == 403


# ── #46 only what the data shows ────────────────────────────────────────────

def test_unsourced_claims_are_gone():
    src = open("home_brief.py").read() + open("mobile_api.py").read()
    for claim in ("Answered reviews rank higher", "see measurably more new guests",
                  "Better than most independents", "accounts that post weekly hold reach",
                  "things nearby restaurants are doing that you aren't", "len(ot) * 38", "ot_count * 38"):
        assert claim not in src, claim


# ── keys shared with the alerts, Ask's own reasons, cover requests ─────────

def test_home_keys_are_the_alert_keys():
    """A "not for us" on Home must silence the matching alert, so a card
    carries the alert's own key (notify.alert_rec)."""
    assert bi.driver_key({"label": "Ribeye price up 12%"}) == "price_spike:Ribeye"
    assert bi.driver_key({"kind": "price", "item": "Salmon", "label": "Salmon price up 8%"}) == "price_spike:Salmon"
    assert bi.driver_key({"label": "Salmon waste above tolerance"}) == "food_cost_driver:Salmon waste above tolerance"
    assert home_brief.ledger_key("awaiting_approval") == "no_response"
    src = open("home_brief.py").read()
    # One key per critically-low item, the alert's own (reaudit H-13).
    assert home_brief.stock_key("Salmon") == "stock_low:Salmon"
    assert 'rec_key=stock_key(crit[0]' in src and 'f"labor_over:{' in src


def test_decisions_show_the_reason_the_owner_gave_ask(db_path):
    rid = _rid(db_path)
    pid = models.log_ask_action(rid, "send_guest_campaign", summary="Text the club", outcome="proposed",
                                db_path=db_path)
    models.log_ask_action(rid, "send_guest_campaign", summary="Text the club", outcome="dismissed",
                          proposal_id=pid, reason="we just texted them", db_path=db_path)
    row = next(r for r in decisions.history(rid, db_path=db_path) if r["key"] == f"ask:{pid}")
    assert row["answer"] == "dismissed" and row["reason"] == "we just texted them"


def test_home_issue_rows_offer_to_ask_a_suggested_cover():
    s = open("templates/dashboard.html").read()
    a = s.index('id="panel-home"'); b = s.index("<!-- /panel-home -->")
    panel = s[a:b]
    assert "data-cover-issue" in panel and "/ask-cover'" in panel and "to cover</button>" in panel
    swift = open("ios/CavnarAI/CavnarAI/Features/Home/HomeDay.swift").read()
    assert "/ask-cover" in swift and "coversToAsk" in swift
