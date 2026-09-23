"""The recommendation-trust audit, Reviews / Food Cost / Marketing / Intel.

What these tests protect, by audit item:
  #14  a public reply never commits the restaurant to an action, a comp or a
       staff conversation nobody told Cavnar about — and an owner's edit is
       held to the same check as the model's draft;
  #15  the auto-approve rule never earns trust from its own output;
  #21  every model-written recommendation has a stable key, is logged as
       shown, and an answered one does not come back;
  #22  one stored read per restaurant and data, shared by web and phone;
  #26  one-tap reprice, and a tracker only for a price that followed a
       suggestion;
  #31  one competitor parser, no recommendation promoted from an unverified
       read, each one citing real reviews; the AI-visibility checklist's
       reasons are its own;
  #35  food-cost confidence comes from weeks of data and recipe provenance,
       against the restaurant's own target;
  #36  a de-duplicated monthly total;
  #41  suggested vs chosen is kept (replies, recipes, orders, prices);
  #47  a lapsed segment gets a drafted win-back, never sent on its own;
  #16  owner-facing dates are M/D/YY.
No model or network is reached: every model call is stubbed.
"""
import json
import sqlite3
import types
from datetime import date, datetime, timedelta

import pytest

import ai_guard
import client_api
import models
import rec_ledger
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    """Everything that resolves get_conn through models at call time reads
    the test database."""
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import webhooks
    monkeypatch.setattr(webhooks, "get_conn", fake, raising=False)
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    client_api._insight_cache.clear()
    yield
    client_api._insight_cache.clear()


def _rid(db_path, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Gia Mia"), owner_email="o@x.test", **kw),
                             db_path=db_path)


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _msg(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


# ── #14 the reply guard ─────────────────────────────────────────────────────

@pytest.mark.parametrize("claim", [
    "I have spoken with our kitchen team about this.",
    "We will be retraining the line this week.",
    "Going forward, every order will be double-checked.",
    "Please join us for a complimentary dinner on us.",
    "Our manager has spoken to the server.",
    "We have retrained our staff.",
    "We've made changes to the menu.",
    "I'll make sure this never happens again.",
    "The server in question has been spoken to.",
    "We'd like to offer a full refund.",
    "Your next visit is on the house.",
])
def test_the_guard_catches_every_kind_of_unsupported_commitment(claim):
    assert ai_guard.unsupported_commitments(claim), claim


@pytest.mark.parametrize("fine", [
    "We are so sorry your pasta arrived cold — that is not the evening we want for anyone.",
    "Thank you for taking a chance on us. Count on us to welcome you back.",
    "Please reach out to me directly so we can make it right.",
    "We're thrilled you loved the tiramisu!",
    "Our kitchen team is delighted you enjoyed it.",
    "I will pass your kind words to the team.",
])
def test_an_apology_or_an_invitation_is_not_a_commitment(fine):
    assert ai_guard.unsupported_commitments(fine) == []


def test_the_low_star_prompt_no_longer_asks_the_model_to_invent_actions(db_path, monkeypatch):
    import drafter
    rid = _rid(db_path)
    seen = {}
    monkeypatch.setattr(drafter, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(drafter, "create_with_retry",
                        lambda client, **kw: seen.setdefault("prompt", kw["messages"][0]["content"])
                        and _msg("We are sorry the soup was cold. Please reach out so we can make it right."))
    c = _conn(db_path)
    rev = c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, processed, "
                    "response_status, fetched_at) VALUES (?,?,?,?,?,?,1,'pending',datetime('now'))",
                    (rid, "google", "e1", "Ann", 1, "Cold soup")).lastrowid
    c.commit(); c.close()
    drafter.draft_response(rev, 1, "Cold soup", "negative", "Gia Mia", restaurant_id=rid)
    p = seen["prompt"]
    assert "explain what will be done differently" not in p
    assert "Do NOT state or promise any action" in p
    assert "never offer a refund" in p


def _drafted(db_path, rid, draft, rating=5, status="drafted"):
    c = _conn(db_path)
    rv = c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, processed, "
                   "response_status, draft_response, review_date, fetched_at) VALUES (?,?,?,?,?,?,1,?,?,datetime('now'),datetime('now'))",
                   (rid, "google", f"x{datetime.utcnow().timestamp()}{draft[:5]}", "Ann", rating, "Nice",
                    status, draft)).lastrowid
    c.commit(); c.close()
    return rv


def test_an_owner_edit_is_held_to_the_same_guard(db_path):
    rid = _rid(db_path)
    rv = _drafted(db_path, rid, "Thanks for coming in!")
    out, st = client_api._do_save_draft(rv, rid, "Thanks for coming in! Your next dinner is on the house.")
    assert st == 200 and out["needs_review"] is True
    c = _conn(db_path)
    row = c.execute("SELECT draft_needs_review, draft_review_reason, original_draft, draft_edited FROM reviews "
                    "WHERE id=?", (rv,)).fetchone()
    c.close()
    assert row["draft_needs_review"] == 1 and "on the house" in row["draft_review_reason"]
    # Suggested vs chosen: the model's text is kept, not overwritten.
    assert row["original_draft"] == "Thanks for coming in!" and row["draft_edited"] == 1


def test_a_clean_edit_clears_the_flag_and_keeps_the_first_original(db_path):
    rid = _rid(db_path)
    rv = _drafted(db_path, rid, "We have retrained our staff. Come back soon.")
    client_api._do_save_draft(rv, rid, "Sorry about that. Come back soon.")
    client_api._do_save_draft(rv, rid, "So sorry about that. Please come back soon.")
    c = _conn(db_path)
    row = c.execute("SELECT draft_needs_review, original_draft FROM reviews WHERE id=?", (rv,)).fetchone()
    c.close()
    assert row["draft_needs_review"] == 0
    assert row["original_draft"] == "We have retrained our staff. Come back soon."


def test_saving_an_unchanged_draft_is_not_an_edit(db_path):
    rid = _rid(db_path)
    rv = _drafted(db_path, rid, "Thanks for coming in!")
    client_api._do_save_draft(rv, rid, "Thanks for coming in!")
    c = _conn(db_path)
    row = c.execute("SELECT draft_edited, original_draft FROM reviews WHERE id=?", (rv,)).fetchone()
    c.close()
    assert not row["draft_edited"] and row["original_draft"] is None


# ── #15 auto-approve trust ──────────────────────────────────────────────────

def _approved(db_path, rid, n, rating=5, action="approved_as_is", edited=0):
    c = _conn(db_path)
    for i in range(n):
        c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, processed, "
                  "response_status, draft_response, approved_at, response_action, draft_edited, fetched_at) "
                  "VALUES (?,?,?,?,?,?,1,'posted','Thanks!',datetime('now','-1 day'),?,?,datetime('now'))",
                  (rid, "google", f"{action}{rating}{i}", "G", rating, "ok", action, edited))
    c.commit(); c.close()


def test_auto_approved_replies_never_count_as_owner_approvals(db_path):
    rid = _rid(db_path)
    _approved(db_path, rid, 12, action="auto_approved")
    t = models.auto_approve_trust(rid, db_path=db_path)
    assert t[5]["approved"] == 0 and t[5]["trusted"] is False
    _approved(db_path, rid, 10, action="approved_as_is")
    assert models.auto_approve_trust(rid, db_path=db_path)[5]["trusted"] is True


def test_skipped_drafts_count_against_trust(db_path):
    rid = _rid(db_path)
    _approved(db_path, rid, 10, rating=4)
    c = _conn(db_path)
    for i in range(3):
        c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, processed, "
                  "response_status, draft_response, skipped_at, fetched_at) VALUES (?,?,?,?,?,?,1,'skipped','x',datetime('now'),datetime('now'))",
                  (rid, "google", f"sk{i}", "G", 4, "ok"))
    c.commit(); c.close()
    t = models.auto_approve_trust(rid, db_path=db_path)[4]
    assert t["skipped"] == 3 and t["trusted"] is False and t["edit_rate"] == pytest.approx(3 / 13)


def test_the_rules_approval_is_marked_auto_and_a_persons_is_not(db_path, monkeypatch):
    import gmb
    monkeypatch.setattr(gmb, "is_connected", lambda rid: False)
    rid = _rid(db_path)
    a = _drafted(db_path, rid, "Thanks!")
    b = _drafted(db_path, rid, "Thanks again!")
    assert client_api._do_approve(a, rid)[1] == 200            # no request: the scheduler's rule
    from flask import Flask
    with Flask(__name__).test_request_context():
        assert client_api._do_approve(b, rid)[1] == 200        # a person, in a request
    c = _conn(db_path)
    acts = {r["id"]: r["response_action"] for r in c.execute("SELECT id, response_action FROM reviews")}
    c.close()
    assert acts[a] == "auto_approved" and acts[b] == "approved_as_is"
    # ...and the rule's reply is not a style example for the next draft.
    assert [e["response"] for e in models.get_approved_examples(rid, db_path=db_path)] == ["Thanks again!"]


def test_a_skip_is_dated(db_path):
    rid = _rid(db_path)
    rv = _drafted(db_path, rid, "Thanks!")
    client_api._do_skip(rv, rid)
    c = _conn(db_path)
    assert c.execute("SELECT skipped_at FROM reviews WHERE id=?", (rv,)).fetchone()[0]
    c.close()


# ── #21 / #22 keyed recommendations, one stored read ────────────────────────

FOOD_READ = ("Food cost is running hot this week, led by salmon waste.\n\n"
             "1. Cut the salmon order by one case — $240/month, medium confidence, low effort\n"
             "2. Portion the fries to spec — $90/month, medium confidence, low effort")


def test_each_numbered_line_gets_a_stable_key_and_an_answer_hides_it(db_path):
    rid = _rid(db_path)
    items = client_api.insight_rec_items(rid, FOOD_READ, "insight_food", "food", "food")
    assert [i["key"].split(":")[0] for i in items] == ["insight_food", "insight_food"]
    again = client_api.insight_rec_items(rid, FOOD_READ.replace("Cut the", "cut  the"), "insight_food", "food", "food")
    assert again[0]["key"] == items[0]["key"]                      # same words, same recommendation
    html = client_api.format_insight_html(FOOD_READ, rec_items=items, surface="food", module="food")
    assert html.count('data-rec-event="completed"') == 2 and 'class="cbtn' in html
    rec_ledger.record(rid, items[0]["key"], "dismissed", meta={"kind": "not_for_us"}, db_path=db_path)
    items = client_api.insight_rec_items(rid, FOOD_READ, "insight_food", "food", "food")
    html = client_api.format_insight_html(FOOD_READ, rec_items=items, surface="food", module="food")
    assert "salmon order" not in html and "Portion the fries" in html
    c = _conn(db_path)
    assert c.execute("SELECT COUNT(*) FROM rec_events WHERE event='shown'").fetchone()[0] >= 2
    c.close()


def test_an_unverified_read_offers_no_controls(db_path):
    rid = _rid(db_path)
    items = client_api.insight_rec_items(rid, FOOD_READ, "insight_food", "food", "food", promote=False)
    assert items and not any(i["controls"] for i in items)
    html = client_api.format_insight_html(FOOD_READ, rec_items=items, surface="food", module="food")
    assert "data-rec-key" not in html


def test_the_mobile_payload_carries_keys_beside_the_lines(db_path):
    import mobile_api
    rid = _rid(db_path)
    items = client_api.insight_rec_items(rid, FOOD_READ, "insight_food", "food", "food")
    rec_ledger.record(rid, items[1]["key"], "completed", db_path=db_path)
    items = client_api.insight_rec_items(rid, FOOD_READ, "insight_food", "food", "food")
    out = mobile_api._insight_json(FOOD_READ, items)
    assert len(out["insight_recommendations"]) == 1 and out["insight_rec_keys"] == [items[0]["key"]]


def test_a_diagnosis_action_is_keyed_by_its_subject_not_its_wording(db_path):
    rid = _rid(db_path)
    d1 = {"category": "slow_service", "recommended_action": "Add a runner on Fridays.", "cause": "x"}
    d2 = {"category": "slow_service", "recommended_action": "Put a second runner on Friday nights.", "cause": "y"}
    out = client_api.present_diagnoses(rid, [d1], "diag_review", "reviews", "reviews")
    assert out[0]["rec_key"] == "diag_review:slow_service" and out[0]["answered"] is False
    rec_ledger.record(rid, "diag_review:slow_service", "completed", db_path=db_path)
    assert client_api.present_diagnoses(rid, [d2], "diag_review", "reviews", "reviews")[0]["answered"] is True


def test_the_reviews_do_today_line_is_keyed_and_removed_once_answered(db_path):
    rid = _rid(db_path)
    text = "\U0001f4ca This week: 12 reviews.\n✅ Do today: Call Ann about the cold soup."
    p = client_api._review_insight_recs(rid, {"insight": text, "figures_verified": True, "names_verified": True})
    assert p["recs"] and p["recs"][0]["kind"] == "do_today"
    rec_ledger.record(rid, p["recs"][0]["key"], "completed", db_path=db_path)
    p = client_api._review_insight_recs(rid, {"insight": text, "figures_verified": True, "names_verified": True})
    assert p["recs"] == [] and "Do today" not in p["insight"] and "This week" in p["insight"]
    # A read naming a guest Cavnar could not find is not something to act on.
    p = client_api._review_insight_recs(rid, {"insight": text.replace("Call Ann", "Call Bea"),
                                              "names_verified": False})
    assert p["recs"] == []


def test_one_stored_read_serves_web_and_phone_until_the_data_changes(db_path, monkeypatch):
    import inventory
    rid = _rid(db_path)
    calls = []
    monkeypatch.setattr(inventory, "create_with_retry", lambda *a, **k: calls.append(1) or _msg(FOOD_READ))
    monkeypatch.setattr(inventory, "get_client", lambda *a, **k: object())
    items, is_live, analysis = inventory.analysis_for(rid, items=inventory.load_inventory(), is_live=True)
    first = inventory.get_claude_insights(analysis, restaurant_id=rid, items=items, is_live=True)
    client_api._insight_cache.clear()                                  # a restart, or the other client
    second = inventory.get_claude_insights(analysis, restaurant_id=rid, items=items, is_live=True)
    assert first == second and len(calls) == 1
    analysis = dict(analysis, total_waste_cost_week=analysis["total_waste_cost_week"] + 50)
    inventory.get_claude_insights(analysis, restaurant_id=rid, items=items, is_live=True)
    assert len(calls) == 2                                             # the data moved: a new read


def test_the_food_prompt_no_longer_contradicts_itself(db_path, monkeypatch):
    import inventory
    rid = _rid(db_path)
    seen = {}
    monkeypatch.setattr(inventory, "create_with_retry",
                        lambda client, **kw: seen.setdefault("p", kw["messages"][0]["content"]) and _msg(FOOD_READ))
    monkeypatch.setattr(inventory, "get_client", lambda *a, **k: object())
    items, _l, analysis = inventory.analysis_for(rid, items=inventory.load_inventory(), is_live=True)
    inventory.get_claude_insights(analysis, restaurant_id=rid, items=items, is_live=True)
    assert "Plain flowing prose throughout — no line that starts with a dash or number" not in seen["p"]
    assert 'Number each one: start with "1. "' in seen["p"]


def test_marketing_web_and_phone_share_one_read(db_path, monkeypatch):
    import ai_utils
    rid = _rid(db_path, module_marketing=1)
    calls = []
    monkeypatch.setattr(ai_utils, "create_with_retry",
                        lambda *a, **k: calls.append(1) or _msg("Hi, push the patio.\n\n1. Post the carbonara tonight.\n2. Feature brunch Sunday."))
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    web, st = client_api._do_mkt_insight(rid)
    phone, st2 = client_api._do_mkt_insight(rid, raw=True)
    assert st == st2 == 200 and len(calls) == 1
    assert "carbonara" in web["insight"] and "data-rec-key" in web["insight"]
    assert phone["insight"].startswith("Hi,") and len(phone["recs"]) == 2
    client_api._insight_cache.clear()
    client_api._do_mkt_insight(rid, raw=True)
    assert len(calls) == 1                                             # the stored read, after a restart


def test_marketing_reach_needs_a_real_threshold_before_it_calls_a_direction(db_path, monkeypatch):
    import ai_utils
    rid = _rid(db_path, module_marketing=1)
    c = _conn(db_path)
    # Four weeks, one post each, drifting down 3% — not a trend, not a pivot.
    for w, reach in enumerate((410, 405, 400, 398)):
        c.execute("INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, reach, created_at) "
                  "VALUES (?,?,?,?,?,datetime('now', ?))", (rid, "instagram_post", f"t{w}", f"p{w}", reach,
                                                             f"-{(3 - w) * 7 + 1} days"))
    c.commit(); c.close()
    seen = {}
    monkeypatch.setattr(ai_utils, "create_with_retry",
                        lambda client, **kw: seen.setdefault("p", kw["messages"][0]["content"]) and _msg("Hi, x.\n\n1. a\n2. b"))
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    client_api._do_mkt_insight(rid)
    assert "DECLINING" not in seen["p"] and "strategy pivot" not in seen["p"]


# ── #26 one-tap reprice ─────────────────────────────────────────────────────

def _suggestion(dish="Carbonara", item_id=7, price=18.0, suggested=19.25):
    return {"dish": dish, "menu_item_id": item_id, "sell_price": price, "suggested_price": suggested,
            "plate_cost": 6.0, "monthly_margin_lost": 120.0}


def test_one_tap_sets_the_suggested_price_and_records_suggested_vs_chosen(db_path, monkeypatch):
    import menu_intelligence as mi
    import inventory_ledger
    rid = _rid(db_path)
    monkeypatch.setattr(mi, "reprice_suggestions",
                        lambda r, db_path=None: {"available": True, "suggestions": [_suggestion()]})
    set_to = []
    monkeypatch.setattr(inventory_ledger, "set_menu_item_price", lambda r, i, p: set_to.append((i, p)) or True)
    out, st = mi.apply_reprice(rid, dish="carbonara", price=None, user_id=5, db_path=db_path)
    assert st == 200 and out["price"] == 19.25 and set_to == [(7, 19.25)] and out["tracked"]
    d = mi.reprice_decisions(rid, db_path=db_path)[0]
    assert (d["old_price"], d["suggested_price"], d["chosen_price"], d["source"]) == (18.0, 19.25, 19.25, "one_tap")
    assert rec_ledger.silenced(rid, "reprice:Carbonara", db_path=db_path)
    c = _conn(db_path)
    evs = [r[0] for r in c.execute("SELECT event FROM rec_events WHERE key='reprice:Carbonara' ORDER BY id")]
    c.close()
    assert "accepted" in evs and "completed" in evs


def test_an_owner_adjusted_price_is_what_is_recorded(db_path, monkeypatch):
    import menu_intelligence as mi
    import inventory_ledger
    rid = _rid(db_path)
    monkeypatch.setattr(mi, "reprice_suggestions",
                        lambda r, db_path=None: {"available": True, "suggestions": [_suggestion()]})
    monkeypatch.setattr(inventory_ledger, "set_menu_item_price", lambda r, i, p: True)
    out, st = mi.apply_reprice(rid, dish="Carbonara", price=19.0, db_path=db_path)
    assert st == 200 and mi.reprice_decisions(rid, db_path=db_path)[0]["chosen_price"] == 19.0
    assert mi.apply_reprice(rid, dish="Lasagna", db_path=db_path)[1] == 409        # no suggestion, no tap
    assert mi.apply_reprice(rid, dish="Carbonara", price="free", db_path=db_path)[1] == 400


@pytest.mark.parametrize("old,new", [(None, 18.0), (18.0, 0), (18.0, None), (18.0, 17.0)])
def test_a_first_price_a_clear_or_a_cut_starts_no_tracker(db_path, monkeypatch, old, new):
    import menu_intelligence as mi
    rid = _rid(db_path)
    assert mi.record_price_change(rid, 7, old, new, suggestion=_suggestion(), db_path=db_path) is None
    assert mi.reprice_decisions(rid, db_path=db_path) == []


def test_a_typed_price_following_a_suggestion_is_tracked(db_path):
    import menu_intelligence as mi
    rid = _rid(db_path)
    assert mi.record_price_change(rid, 7, 18.0, 19.5, suggestion=_suggestion(), db_path=db_path)
    assert mi.reprice_decisions(rid, db_path=db_path)[0]["source"] == "manual"


def test_the_reprice_list_carries_its_key_and_drops_an_answered_dish(db_path, monkeypatch):
    import menu_intelligence as mi
    rid = _rid(db_path)
    monkeypatch.setattr(mi, "reprice_suggestions", lambda r, db_path=None: {
        "available": True, "suggestions": [_suggestion(), _suggestion("Lasagna", 8, 16.0, 17.0)]})
    out = mi.presented_suggestions(rid, db_path=db_path)
    assert [s["rec_key"] for s in out["suggestions"]] == ["reprice:Carbonara", "reprice:Lasagna"]
    rec_ledger.record(rid, "reprice:Lasagna", "dismissed", meta={"kind": "not_for_us"}, db_path=db_path)
    assert [s["dish"] for s in mi.presented_suggestions(rid, db_path=db_path)["suggestions"]] == ["Carbonara"]


def test_both_reprice_routes_exist():
    import hosted_dashboard  # noqa: F401  (registers the blueprints)
    rules = {r.rule for r in hosted_dashboard.app.url_map.iter_rules()}
    assert "/api/food-cost/reprice/apply" in rules and "/mobile/api/food-cost/reprice/apply" in rules


# ── #31 intel ───────────────────────────────────────────────────────────────

INTEL = """Hi, here is your snapshot.

WHAT COMPETITORS ARE DOING WELL:
- Lou's Diner nails the breakfast rush [R1]
- 1. a stray numbered line

Recommendations:
1. Greet every table inside a minute [R2]
2. Push the carbonara on the chalkboard [R1, R3]"""


def test_one_parser_answers_every_surface():
    from competitor_intel_format import parse_competitor_intel, extract_recs
    p = parse_competitor_intel(INTEL)
    assert p["recommendations"] == extract_recs(INTEL) == ["Greet every table inside a minute",
                                                          "Push the carbonara on the chalkboard"]
    assert p["recommendation_items"][1]["cites"] == ["R1", "R3"]


def test_an_unverified_read_promotes_no_recommendations():
    from competitor_intel_format import parse_competitor_intel, extract_recs, format_intel
    text = INTEL + "\n\nUNVERIFIED: states figures that were not in the data: $4,300."
    p = parse_competitor_intel(text)
    assert p["recommendations"] == [] and extract_recs(text) == [] and p["withheld_recommendations"] == 2
    assert "held back" in str(format_intel(text)) and "Greet every table" not in str(format_intel(text))


def test_nothing_worth_acting_on_is_an_answer():
    from competitor_intel_format import parse_competitor_intel, NOTHING_TO_ACT_ON
    p = parse_competitor_intel("Hi.\n\nRecommendations:\n" + NOTHING_TO_ACT_ON)
    assert p["recommendations"] == [] and p["nothing_to_act_on"] is True


def _comps():
    return [{"name": "Lou's Diner", "rating": 4.2, "review_count": 300,
             "reviews": [{"rating": 2, "text": "Waited forever to be greeted", "time": "a month ago"},
                         {"rating": 5, "text": "Great eggs", "time": "a week ago"}]},
            {"name": "Tony's Pizza", "rating": 3.9, "review_count": 120,
             "reviews": [{"rating": 1, "text": "Cold pizza", "time": "2 weeks ago"}]}]


def test_a_recommendation_must_cite_reviews_that_exist(monkeypatch):
    import competitor
    comps = _comps()
    reply = ("Hi, here is your competitive landscape snapshot.\n\nRecommendations:\n"
             "1. Greet every table inside a minute [R1]\n2. Push the carbonara\n3. Serve slices hotter than the pizza place [R9]")
    seen = {}
    monkeypatch.setattr(competitor, "create_with_retry",
                        lambda client, **kw: seen.setdefault("p", kw["messages"][0]["content"]) and _msg(reply))
    monkeypatch.setattr(competitor, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(competitor, "ANTHROPIC_KEY", "k", raising=False)
    out = competitor.generate_competitor_insight("Mine", comps, restaurant_id=1)
    from competitor_intel_format import parse_competitor_intel
    items = parse_competitor_intel(out)["recommendation_items"]
    assert [i["text"] for i in items] == ["Greet every table inside a minute"] and items[0]["cites"] == ["R1"]
    assert comps[0]["reviews"][0]["ref"] == "R1" and comps[1]["reviews"][0]["ref"] == "R3"
    assert "[R1 · 2★" in seen["p"]
    assert "between ZERO and THREE" in seen["p"] and "exactly three" not in seen["p"].lower()


def test_no_cited_recommendation_left_says_so(monkeypatch):
    import competitor
    from competitor_intel_format import NOTHING_TO_ACT_ON
    monkeypatch.setattr(competitor, "create_with_retry", lambda *a, **k: _msg(
        "Hi.\n\nRecommendations:\n1. Do something vague\n2. Another vague one"))
    monkeypatch.setattr(competitor, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(competitor, "ANTHROPIC_KEY", "k", raising=False)
    out = competitor.generate_competitor_insight("Mine", _comps(), restaurant_id=1)
    assert NOTHING_TO_ACT_ON in out and "vague" not in out


def test_the_intel_screen_gets_keyed_recs_with_their_cited_reviews(db_path):
    rid = _rid(db_path)
    comps = _comps()
    for i, r in enumerate([r for c in comps for r in c["reviews"]], 1):
        r["ref"] = f"R{i}"
    c = _conn(db_path)
    c.execute("UPDATE restaurants SET competitor_intel=? WHERE id=?",
              (json.dumps({"competitors": comps, "insight": INTEL, "generated_at": "2026-09-20"}), rid))
    c.commit(); c.close()
    p = client_api.intel_recs_payload(rid)
    assert [r["key"].split(":")[0] for r in p["recs"]] == ["insight_intel", "insight_intel"]
    assert [q["competitor"] for q in p["recs"][1]["cites"]] == ["Lou's Diner", "Tony's Pizza"]
    rec_ledger.record(rid, p["recs"][0]["key"], "dismissed", meta={"kind": "not_for_us"}, db_path=db_path)
    assert len(client_api.intel_recs_payload(rid)["recs"]) == 1


def test_no_checklist_item_borrows_another_items_reason():
    """"unlocks live listing data and Google Posts" was pasted onto the
    description, phone, website and hours items. Read the source so every
    branch is covered, not just the ones one fixture reaches."""
    import inspect
    import re
    src = inspect.getsource(client_api._do_ai_visibility_inner)
    reasons = re.findall(r'"label": "([^"]+)"[^}]*?"why_it_matters": "([^"]+)"', src)
    assert reasons
    for label, why in reasons:
        if "Google Posts" in why:
            assert "Google Business Profile" in label or "GBP connected" in label or "OAuth" in label, label
    assert "AND post_id IS NOT NULL" in src                             # published posts only


# ── #35 / #36 food cost confidence and the de-duplicated total ─────────────

def test_waste_confidence_comes_from_weeks_of_data():
    import food_cost_intelligence as fci
    assert [fci._waste_confidence(w) for w in (1, 2, 3, 6)] == ["low", "medium", "high", "high"]


def test_the_waste_driver_says_when_it_is_one_week(db_path, monkeypatch):
    import food_cost_intelligence as fci
    import inventory
    rid = _rid(db_path)
    analysis = {"is_live": True, "waste_items": [{"item": "Salmon", "recoverable_cost": 60.0, "waste_pct": 20,
                                                  "waste_tolerance_pct": 5, "waste_cost": 90.0}]}
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([], True, analysis))
    out = fci.cost_drivers(rid, db_path=db_path)
    w = [d for d in out["drivers"] if d["kind"] == "waste"][0]
    assert w["confidence"] == "low" and "one week of data" in w["evidence"]
    c = _conn(db_path)
    for wk in (0, 7, 14):
        c.execute("INSERT INTO inventory_history (restaurant_id, week_end, waste_json) VALUES (?,?,?)",
                  (rid, (date.today() - timedelta(days=wk)).isoformat(), json.dumps({"top_items": ["Salmon"]})))
    c.commit(); c.close()
    w = [d for d in fci.cost_drivers(rid, db_path=db_path)["drivers"] if d["kind"] == "waste"][0]
    assert w["confidence"] == "high" and w["weeks_of_data"] == 3


def test_the_menu_driver_uses_the_restaurants_target_and_recipe_provenance(db_path, monkeypatch):
    import food_cost_intelligence as fci
    import inventory
    import inventory_ledger as il
    rid = _rid(db_path)
    c = _conn(db_path)
    c.execute("UPDATE restaurants SET food_cost_target=28 WHERE id=?", (rid,))
    mid = c.execute("INSERT INTO menu_items (restaurant_id, name, is_active) VALUES (?,?,1)", (rid, "Pasta")).lastrowid
    a = c.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, is_active) VALUES (?,?,?,?,1)",
                  (rid, "Flour", "lb", 1.0)).lastrowid
    b = c.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, is_active) VALUES (?,?,?,?,1)",
                  (rid, "Eggs", "ea", 1.0)).lastrowid
    c.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit, source) VALUES (?,?,?,?)",
              (mid, a, 1, "draft_accepted"))
    c.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit, source) VALUES (?,?,?,?)",
              (mid, b, 1, "owner"))
    c.commit(); c.close()
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([], True, {"is_live": True, "waste_items": []}))
    # 32% is under the old fixed 35% and over this restaurant's 28% target.
    monkeypatch.setattr(il, "menu_profitability", lambda r: {"priced": [
        {"id": mid, "name": "Pasta", "sell_price": 25.0, "plate_cost": 8.0, "food_cost_pct": 32.0, "units_sold": 400}]})
    monkeypatch.setattr(il, "inferred_variance", lambda r: {"material": []})
    out = fci.cost_drivers(rid, db_path=db_path)
    m = [d for d in out["drivers"] if d["kind"] == "menu"][0]
    assert m["target_pct"] == 28 and m["confidence"] == "medium" and m["recipe_unreviewed_lines"] == 1
    assert "accepted unedited" in m["evidence"] and sorted(m["ingredients"]) == ["Eggs", "Flour"]


def test_one_ingredient_is_counted_once_in_the_total():
    import food_cost_intelligence as fci
    drivers = [{"kind": "waste", "item": "Salmon", "dollars_monthly": 200.0},
               {"kind": "price", "item": "salmon", "dollars_monthly": 150.0},
               {"kind": "menu", "item": "Salmon Plate", "ingredients": ["Salmon", "Rice"], "dollars_monthly": 300.0},
               {"kind": "portion", "item": "Fries", "dollars_monthly": 50.0}]
    d = fci.deduplicated_total(drivers)
    assert d["total"] == 350.0 and d["groups"] == 2 and d["overlapping"] == 1


def test_a_stale_diagnosis_says_when_it_was_written(db_path):
    import food_cost_intelligence as fci
    rid = _rid(db_path)
    c = _conn(db_path)
    c.execute("INSERT INTO food_cost_diagnoses (restaurant_id, headline, cause, confidence, window_days, generated_at) "
              "VALUES (?,?,?,?,28,datetime('now','-3 days'))", (rid, "h", "Salmon over-ordered", "medium"))
    c.commit(); c.close()
    d = fci.get_diagnosis(rid, db_path=db_path, include_stale=True)
    assert d["stale"] and d["stale_note"].startswith("From a read on ") and "-" not in d["as_of"]


# ── #41 suggested vs chosen, and order trust ────────────────────────────────

def test_an_accepted_recipe_draft_records_what_was_kept_and_what_was_changed(db_path):
    import recipes
    rid = _rid(db_path)
    c = _conn(db_path)
    mid = c.execute("INSERT INTO menu_items (restaurant_id, name, is_active) VALUES (?,?,1)", (rid, "Pizza")).lastrowid
    a = c.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, is_active) VALUES (?,?,?,?,1)",
                  (rid, "Mozzarella", "lb", 4.0)).lastrowid
    b = c.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, is_active) VALUES (?,?,?,?,1)",
                  (rid, "Dough", "ea", 1.0)).lastrowid
    did = c.execute("INSERT INTO recipe_drafts (restaurant_id, menu_item_id, lines_json, status) VALUES (?,?,?,'pending')",
                    (rid, mid, json.dumps([{"ingredient_id": a, "qty": 0.25}, {"ingredient_id": b, "qty": 1}]))).lastrowid
    c.commit(); c.close()
    out = recipes.accept(rid, did, lines=[{"ingredient_id": a, "qty": 0.25}, {"ingredient_id": b, "qty": 1.5}],
                         db_path=db_path)
    assert out["ok"] and out["edited"] == 1
    c = _conn(db_path)
    src = {r["ingredient_id"]: r["source"] for r in c.execute("SELECT ingredient_id, source FROM recipe_ingredients")}
    row = c.execute("SELECT accepted_lines_json, edited_lines FROM recipe_drafts WHERE id=?", (did,)).fetchone()
    c.close()
    assert src == {a: "draft_accepted", b: "draft_edited"}
    assert row["edited_lines"] == 1 and json.loads(row["accepted_lines_json"])[1]["qty"] == 1.5


def _po(db_path, rid, total, days_ago, source=None, edited=0, email="s@x.com"):
    c = _conn(db_path)
    c.execute("INSERT INTO purchase_orders (restaurant_id, po_number, supplier_name, supplier_email, items_json, "
              "total_cost, status, sent_at, source, edited) VALUES (?,?,?,?,?,?,?,datetime('now', ?),?,?)",
              (rid, f"PO-{total}-{days_ago}", "Fresh", email, "[]", total, "sent", f"-{days_ago} days", source, edited))
    c.commit(); c.close()


def test_order_trust_counts_only_owner_sent_unedited_orders(db_path):
    import ordering
    rid = _rid(db_path)
    _po(db_path, rid, 400, 30, source="automatic")
    _po(db_path, rid, 410, 25, source="owner", edited=1)
    _po(db_path, rid, 420, 20, source="owner")
    _po(db_path, rid, 430, 15)                                    # before provenance existed: the owner's
    t = ordering.supplier_trust(rid, "s@x.com", db_path=db_path)
    assert t["orders"] == 2 and t["trusted"] is False
    _po(db_path, rid, 440, 12, source="owner")
    assert ordering.supplier_trust(rid, "s@x.com", db_path=db_path)["trusted"] is True


def test_an_automatic_order_is_held_on_a_stale_count_and_says_why(db_path):
    import ordering
    rid = _rid(db_path)
    for t, d in ((400, 30), (450, 20), (420, 12)):
        _po(db_path, rid, t, d, source="owner")
    c = _conn(db_path)
    ing = c.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, is_active, last_recount_at) "
                    "VALUES (?,?,?,?,1,?)", (rid, "Romaine", "case", 20.0,
                                             (date.today() - timedelta(days=12)).isoformat())).lastrowid
    c.commit(); c.close()
    group = {"supplier_email": "s@x.com", "total_cost": 430, "items": [{"ingredient_id": ing, "qty": 2}]}
    ok, why = ordering.order_can_go(rid, group, db_path=db_path)
    assert not ok and why.startswith("held:") and "Romaine" in why and "/" in why
    c = _conn(db_path)
    c.execute("UPDATE ingredients SET last_recount_at=? WHERE id=?", (date.today().isoformat(), ing))
    c.commit(); c.close()
    assert ordering.order_can_go(rid, group, db_path=db_path)[0] is True


def test_a_sent_po_records_its_source_and_draft(db_path, monkeypatch):
    import emails
    rid = _rid(db_path)
    monkeypatch.setattr(emails, "send_supplier_order_email", lambda **k: None)
    group = {"supplier_email": "s@x.com", "supplier_name": "Fresh", "total_cost": 40.0,
             "items": [{"ingredient_id": 1, "item": "Romaine", "qty": 2, "unit_cost": 20.0}]}
    r = models.get_restaurant(rid, db_path=db_path) if "db_path" in models.get_restaurant.__code__.co_varnames \
        else models.get_restaurant(rid)
    sent, failed = client_api._send_supplier_orders(rid, r, [group], {"id": 1}, source="automatic")
    assert sent and not failed
    c = _conn(db_path)
    row = c.execute("SELECT source, edited, draft_items_json FROM purchase_orders").fetchone()
    c.close()
    assert row["source"] == "automatic" and row["edited"] == 0 and json.loads(row["draft_items_json"])[0]["qty"] == 2


# ── marketing: noise band, pieces this month ────────────────────────────────

def test_a_lift_inside_the_weekdays_own_noise_is_no_clear_change():
    import marketing_signals as ms
    band = ms.noise_band_pct([1000, 1150, 870, 990])
    assert band > ms.MIN_NOISE_BAND_PCT
    assert ms.lift_verdict(3.0, band) == "no_clear_change"
    assert ms.lift_verdict(band + 1, band) == "lifted" and ms.lift_verdict(-band - 1, band) == "dropped"
    assert ms.noise_band_pct([1000, 1000]) == ms.MIN_NOISE_BAND_PCT


def test_pieces_this_month_leave_out_markers_and_job_drafts(db_path):
    import marketing
    rid = _rid(db_path)
    c = _conn(db_path)
    rows = [("instagram_post", None, "owner"), ("calendar_instagram_post", None, "marker"),
            ("instagram_post", None, "job"), ("instagram_post", "p1", "job"), ("facebook_post", None, None)]
    for ct, pid, origin in rows:
        c.execute("INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, origin) "
                  "VALUES (?,?,?,?,?)", (rid, ct, "t", pid, origin))
    c.commit(); c.close()
    # the owner's piece, the published job draft and the legacy row
    assert marketing.pieces_this_month(rid) == 3
    assert marketing._content_origin("calendar_x") == "marker" and marketing._content_origin("x") == "job"


# ── #47 win-back ────────────────────────────────────────────────────────────

def _lapsed(db_path, rid, n, days=45):
    import guest_marketing as gm
    gm.init_guest_marketing(db_path)
    c = _conn(db_path)
    for i in range(n):
        c.execute("INSERT INTO guest_contacts (restaurant_id, phone, consent, last_visit, visit_count) "
                  "VALUES (?,?,1,?,2)", (rid, f"+1555000{i:04d}",
                                         (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")))
    c.commit(); c.close()


def test_a_lapsed_segment_gets_a_drafted_win_back_that_is_never_sent_on_its_own(db_path, monkeypatch):
    import guest_marketing as gm
    rid = _rid(db_path, name="Gia Mia")
    _lapsed(db_path, rid, 6)
    sent = []
    monkeypatch.setattr(gm, "send_sms", lambda *a, **k: sent.append(a) or True)
    out = gm.winback_suggestion(rid, db_path=db_path)
    d = out["draft"]
    assert out["available"] and d["segment"] == "lapsed_30" and d["segment_size"] == 6
    assert d["rec_key"] == "winback:lapsed_30" and "Gia Mia" in d["message"] and len(d["message"]) <= gm.CAMPAIGN_MAX_CHARS
    assert "No past win-back text" in d["return"]["text"] and sent == []
    assert gm.winback_suggestion(rid, db_path=db_path)["draft"]["id"] == d["id"]      # one pending draft
    assert ai_guard.unsupported_commitments(d["message"]) == []


def test_too_few_lapsed_guests_is_not_a_campaign(db_path):
    import guest_marketing as gm
    rid = _rid(db_path)
    _lapsed(db_path, rid, 2)
    assert gm.winback_suggestion(rid, db_path=db_path)["available"] is False


def test_sending_goes_through_the_campaign_gates_and_answers_the_recommendation(db_path, monkeypatch):
    import guest_marketing as gm
    rid = _rid(db_path)
    _lapsed(db_path, rid, 6)
    d = gm.winback_suggestion(rid, db_path=db_path)["draft"]
    started = []
    monkeypatch.setattr(gm, "start_campaign", lambda r, msg, segment="all", **k: started.append((msg, segment))
                        or {"ok": True, "queued": True, "total": 6})
    out = gm.send_winback(rid, d["id"], message="We miss you at Gia Mia!", user_id=3, db_path=db_path)
    assert out["ok"] and started == [("We miss you at Gia Mia!", "lapsed_30")]
    assert rec_ledger.silenced(rid, "winback:lapsed_30", db_path=db_path)
    assert gm.send_winback(rid, d["id"], db_path=db_path)["ok"] is False             # answered once
    c = _conn(db_path)
    row = c.execute("SELECT status, sent_message, message FROM guest_campaign_drafts WHERE id=?", (d["id"],)).fetchone()
    c.close()
    assert row["status"] == "sent" and row["sent_message"] != row["message"]


def test_quiet_hours_refuse_the_send_and_leave_the_draft_waiting(db_path, monkeypatch):
    import guest_marketing as gm
    rid = _rid(db_path)
    _lapsed(db_path, rid, 6)
    d = gm.winback_suggestion(rid, db_path=db_path)["draft"]
    monkeypatch.setattr(gm, "guest_sms_allowed_now", lambda r: False)
    out = gm.send_winback(rid, d["id"], db_path=db_path)
    assert out["ok"] is False and out.get("blocked") == "quiet_hours"
    assert gm.winback_suggestion(rid, db_path=db_path)["draft"]["id"] == d["id"]


def test_not_for_us_retires_the_segment(db_path):
    import guest_marketing as gm
    rid = _rid(db_path)
    _lapsed(db_path, rid, 6, days=70)
    d = gm.winback_suggestion(rid, db_path=db_path)["draft"]
    assert d["segment"] == "lapsed_60"
    assert gm.dismiss_winback(rid, d["id"], db_path=db_path)["ok"]
    nxt = gm.winback_suggestion(rid, db_path=db_path)
    assert not nxt["available"] or nxt["draft"]["segment"] != "lapsed_60"


# ── #16 dates ───────────────────────────────────────────────────────────────

def test_the_campaign_read_dates_are_m_d_yy(db_path, monkeypatch):
    import guest_marketing as gm
    rid = _rid(db_path)
    monkeypatch.setattr(gm, "campaign_history", lambda r, limit=20, db_path=None: [
        {"sent_count": 50, "clicks": 9, "segment": "all", "segment_label": "Everyone", "created_at": "2026-09-02 18:00:00"},
        {"sent_count": 40, "clicks": 1, "segment": "all", "segment_label": "Everyone", "created_at": "2026-08-12 18:00:00"}])
    out = gm.diagnose(rid, db_path=db_path)
    blob = json.dumps(out)
    assert "9/2/26" in blob and "2026-09-02" not in blob
