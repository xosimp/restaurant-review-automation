"""business_intelligence.py — the layer that reads across modules.

The invariants here are mostly about restraint. This module can only see
findings that already cleared their own module's evidence floor, it must
never state a co-occurrence as a cause, it must never rank a missing
measurement as zero, and it must never add three different estimation
methods into one number. Each of those is a way to manufacture confidence
out of nothing, and each has a test.
"""
import json
from datetime import date, datetime, timedelta

import pytest

import business_intelligence as bi
import models
from models import (create_restaurant, get_restaurant, get_conn, Restaurant,
                    save_reviews, Review, update_analysis)


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    """The bound-import gotcha, and this module hits every instance of it.

    review_intelligence, food_cost_intelligence and business_intelligence all
    do `from models import get_conn` at module top level — a reference bound
    at import time and completely independent of patching models.get_conn.
    Patching only the models attribute redirected some of the chain and left
    the rest reading the real reviews.db, which showed up as clusters that
    silently would not form. Same gotcha documented in test_guest_marketing.py
    and inventory_ledger.py's header.
    """
    import review_intelligence
    import food_cost_intelligence
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for module in (models, bi, review_intelligence, food_cost_intelligence):
        monkeypatch.setattr(module, "get_conn", redirect, raising=False)


def _restaurant(db_path, **flags):
    defaults = dict(module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1)
    defaults.update(flags)
    rid = create_restaurant(Restaurant(name="Cross Co", owner_email="x@x.com", **defaults),
                            db_path=db_path)
    return rid


def _negative_review(db_path, rid, external_id, when, categories, entities=None, text="Slow."):
    save_reviews([Review(restaurant_id=rid, platform="google", external_id=external_id,
                         author="G", rating=2, text=text,
                         review_date=when.strftime("%Y-%m-%d %H:%M:%S"))], db_path=db_path)
    conn = get_conn(db_path)
    row = conn.execute("SELECT id FROM reviews WHERE external_id=?", (external_id,)).fetchone()
    conn.close()
    update_analysis(row["id"], "negative", categories, "summary", "normal", db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE reviews SET entities=?, severity=?, specific_complaint=? WHERE id=?",
                 (json.dumps(entities or {}), "service", "waited too long", row["id"]))
    conn.commit()
    conn.close()
    return row["id"]


def _fridays(n):
    """`n` recent Fridays, newest first — the weekday every concentration
    test below leans on."""
    out, d = [], date.today()
    while len(out) < n:
        if d.weekday() == 4:
            out.append(datetime.combine(d, datetime.min.time()).replace(hour=19))
        d -= timedelta(days=1)
    return out


# ── the matching that makes a dish link possible ───────────────────────────

def test_dish_names_from_two_modules_match_without_being_identical():
    """Reviews get a dish from entity extraction ("the ribeye"); Food Cost
    gets it from the POS menu ("Ribeye Steak"). Exact equality would mean
    this link could never fire at all."""
    assert bi._same_thing("ribeye", "Ribeye Steak")
    assert bi._same_thing("Ribeye-Steak", "the ribeye")
    assert bi._same_thing("House Salad", "house salad")


def test_a_dish_name_is_not_matched_inside_a_longer_word():
    """A shared WORD, not a shared substring. Without that, "ribeye" matches
    "ribeyeburgersauce" and the link means nothing."""
    assert not bi._same_thing("ribeye", "ribeyeburgersauce")
    assert not bi._same_thing("salmon", "Chicken Thigh")


def test_filler_words_in_an_evidence_sentence_never_make_a_match():
    """A driver's evidence reads "portion over recipe, $240/month". Matching
    a dish to it on "over" or "cost" would link anything to anything."""
    assert not bi._same_thing("House Salad", "portion over recipe costs more this month")
    # The real dish name in the same sentence still matches.
    assert bi._same_thing("House Salad", "salad portion running over recipe")


def test_short_generic_dish_names_never_match():
    """"pie", "dip" and "ale" appear inside unrelated words and sentences. A
    link built on one would be exactly the manufactured pattern this module
    exists to avoid."""
    assert not bi._same_thing("pie", "Shepherds Pie")
    assert not bi._same_thing("ale", "Ale House Burger")


# ── links require both sides to have cleared their own floor ───────────────

def test_no_links_when_one_module_has_nothing(db_path):
    rid = _restaurant(db_path)
    assert bi.correlations(rid) == []


def test_a_complaint_weekday_links_to_a_lean_labour_day(db_path, monkeypatch):
    rid = _restaurant(db_path)
    for i, when in enumerate(_fridays(4)):
        _negative_review(db_path, rid, f"fri-{i}", when, ["service"])

    # Friday runs materially below this restaurant's own weekday average.
    monkeypatch.setattr(
        "labor.analyse_shifts_for_restaurant",
        lambda r: {"is_live": True, "dow_summary": {"Monday": 30.0, "Tuesday": 30.0,
                                                    "Wednesday": 30.0, "Friday": 22.0},
                   "potential_savings_monthly": 0, "labor_target": 30, "period_days": 28})

    links = bi.correlations(rid)
    labour_links = [l for l in links if l["kind"] == "reviews_x_labor"]
    assert labour_links, "a concentrated complaint weekday on the leanest day should link"
    link = labour_links[0]
    assert "Friday" in link["headline"]
    assert "leanest" not in link["headline"], "no superlative the data cannot support"
    assert link["modules"] == ["reviews", "labor"]
    assert link["review_ids"], "a link must carry the reviews it rests on"


def test_a_lean_day_link_never_states_a_cause(db_path, monkeypatch):
    """labor.py is explicit that this data cannot tell running-lean from
    running-efficient — there is no service-time, wait-time or cover-count
    data. Joining it to a review does not create that evidence, so the link
    has to carry the refusal with it."""
    rid = _restaurant(db_path)
    for i, when in enumerate(_fridays(4)):
        _negative_review(db_path, rid, f"fri-{i}", when, ["service"])
    monkeypatch.setattr(
        "labor.analyse_shifts_for_restaurant",
        lambda r: {"is_live": True, "dow_summary": {"Monday": 30.0, "Tuesday": 30.0,
                                                    "Wednesday": 30.0, "Friday": 22.0},
                   "potential_savings_monthly": 0, "labor_target": 30, "period_days": 28})

    link = [l for l in bi.correlations(rid) if l["kind"] == "reviews_x_labor"][0]
    assert link["claim_kind"] == "inferred", "never 'measured'"
    assert link["not_a_cause"], "the refusal has to travel with the link"
    assert "service-time" in link["not_a_cause"]
    assert link["alternative"], "every link offers another explanation"
    assert link["confirm_by"], "every link says what would settle it"


def test_a_labour_day_barely_below_average_is_not_called_lean(db_path, monkeypatch):
    """Under LEAN_DAY_MIN_GAP_PTS the difference is noise, and naming a day
    on it would be inventing the pattern."""
    rid = _restaurant(db_path)
    for i, when in enumerate(_fridays(4)):
        _negative_review(db_path, rid, f"fri-{i}", when, ["service"])
    monkeypatch.setattr(
        "labor.analyse_shifts_for_restaurant",
        lambda r: {"is_live": True, "dow_summary": {"Monday": 30.0, "Tuesday": 30.0,
                                                    "Wednesday": 30.0, "Friday": 29.5},
                   "potential_savings_monthly": 0, "labor_target": 30, "period_days": 28})
    assert [l for l in bi.correlations(rid) if l["kind"] == "reviews_x_labor"] == []


def test_sample_labour_data_never_produces_a_link(db_path, monkeypatch):
    """load_shifts_for_restaurant falls back to a bundled sample roster so the
    Labor tab isn't blank. Linking a real review to an invented Friday would
    be the worst output this module could produce."""
    rid = _restaurant(db_path)
    for i, when in enumerate(_fridays(4)):
        _negative_review(db_path, rid, f"fri-{i}", when, ["service"])
    monkeypatch.setattr(
        "labor.analyse_shifts_for_restaurant",
        lambda r: {"is_live": False, "dow_summary": {"Monday": 30.0, "Friday": 10.0},
                   "potential_savings_monthly": 9999, "period_days": 28})
    assert [l for l in bi.correlations(rid) if l["kind"] == "reviews_x_labor"] == []


def test_scattered_complaints_produce_no_weekday_link(db_path, monkeypatch):
    """review_intelligence only reports a weekday concentration past its own
    share floor. Spread across the week there is no day to link to."""
    rid = _restaurant(db_path)
    base = datetime.now() - timedelta(days=20)
    for i in range(6):
        _negative_review(db_path, rid, f"spread-{i}", base + timedelta(days=i), ["service"])
    monkeypatch.setattr(
        "labor.analyse_shifts_for_restaurant",
        lambda r: {"is_live": True,
                   "dow_summary": {d: 30.0 for d in
                                   ("Monday", "Tuesday", "Wednesday", "Thursday", "Saturday")}
                              | {"Friday": 20.0},
                   "potential_savings_monthly": 0, "labor_target": 30, "period_days": 28})
    assert [l for l in bi.correlations(rid) if l["kind"] == "reviews_x_labor"] == []


# ── money: never zero, never summed, never a fake point figure ─────────────

def test_a_missing_measurement_is_reported_not_ranked_as_zero(db_path, monkeypatch):
    """A module with no figure ranked at $0 would push a real problem down
    the list — the same rule food_cost_intelligence follows for projections."""
    rid = _restaurant(db_path)
    monkeypatch.setattr("labor.analyse_shifts_for_restaurant", lambda r: {"is_live": False})
    money = bi.money_at_stake(rid)
    assert all(line["monthly"] > 0 for line in money["ranked"])
    reasons = {u["module"]: u["reason"] for u in money["unavailable"]}
    assert "labor" in reasons
    assert "shift data" in reasons["labor"]


def test_the_money_roll_up_refuses_to_publish_a_total(db_path):
    """Three different methods measuring three different things. A total
    would be the most quotable number on the screen and the least
    defensible — audit #14 already caught a model inventing exactly that."""
    money = bi.money_at_stake(_restaurant(db_path))
    assert "total_monthly" not in money
    assert "Do not add these together" in money["total_note"]


def test_a_rating_forecast_stays_a_range(db_path, monkeypatch):
    """revenue_at_risk returns a range and refuses a point estimate on
    purpose. Collapsing it here would publish precision the module went out
    of its way not to claim."""
    rid = _restaurant(db_path)
    monkeypatch.setattr(bi, "gather", lambda *a, **k: {
        "reviews": {"brief": {"costing_money": {
            "available": True, "direction": "at_risk", "rating_delta": -0.4,
            "monthly_low": -900, "monthly_high": -1600,
            "elasticity_low_pct": 5.0, "elasticity_high_pct": 9.0,
            "sales_source": "30 days of synced sales"}}},
        "food_cost": None, "labor": {"is_live": False}, "marketing": None,
        "visibility": None, "degraded": [], "modules_off": [], "complete": True})
    line = [l for l in bi.money_at_stake(rid)["ranked"] if l["module"] == "reviews"][0]
    assert line["is_range"] is True
    assert (line["monthly_low"], line["monthly_high"]) == (900, 1600)
    assert line["claim_kind"] == "forecast"


def test_an_improving_rating_is_upside_not_money_at_stake(db_path, monkeypatch):
    """Ranking an improving rating beside two costs to recover would tell an
    owner to go and fix something that is already going right."""
    rid = _restaurant(db_path)
    monkeypatch.setattr(bi, "gather", lambda *a, **k: {
        "reviews": {"brief": {"costing_money": {
            "available": True, "direction": "upside", "rating_delta": 0.4,
            "monthly_low": 900, "monthly_high": 1600,
            "elasticity_low_pct": 5.0, "elasticity_high_pct": 9.0,
            "sales_source": "30 days of synced sales"}}},
        "food_cost": None, "labor": {"is_live": False}, "marketing": None,
        "visibility": None, "degraded": [], "modules_off": [], "complete": True})
    money = bi.money_at_stake(rid)
    assert [l for l in money["ranked"] if l["module"] == "reviews"] == []
    assert money["upside"]["monthly_high"] == 1600


def test_the_snapshot_prints_a_range_as_a_range(db_path, monkeypatch):
    """The midpoint exists to order the list. Printing it would invent a
    figure nobody measured."""
    rid = _restaurant(db_path)
    monkeypatch.setattr(bi, "money_at_stake", lambda *a, **k: {
        "ranked": [{"module": "reviews", "label": "Rating movement", "monthly": 1250.0,
                    "monthly_low": 900, "monthly_high": 1600, "is_range": True,
                    "claim_kind": "forecast", "basis": "-0.40★ against elasticity"}],
        "upside": None, "unavailable": [], "below_floor": [], "floor": 50.0,
        "total_note": "Do not add these together."})
    monkeypatch.setattr(bi, "correlations", lambda *a, **k: [])
    block = bi.snapshot_block(rid)
    assert "$900-$1,600" in block
    assert "1,250" not in block, "the ordering midpoint must never be printed"


# ── degradation is reported, never hidden ──────────────────────────────────

def test_a_failing_module_is_named_rather_than_dropped(db_path, monkeypatch):
    """A cross-module read that silently drops a module is worse than one
    that fails: the answer looks complete and is missing the half that
    mattered."""
    rid = _restaurant(db_path)

    def _boom(*a, **k):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr("food_cost_intelligence.executive_brief", _boom)
    data = bi.gather(rid)
    assert "food_cost" in data["degraded"]
    assert data["complete"] is False


def test_a_module_the_client_does_not_have_is_off_not_degraded(db_path):
    """Not on the plan and failed to read are different facts, and an owner
    reading "could not be answered" deserves the right one."""
    rid = _restaurant(db_path, module_inventory=0, module_marketing=0)
    data = bi.gather(rid)
    assert set(data["modules_off"]) == {"food_cost", "marketing"}
    assert data["degraded"] == []


def test_the_brief_says_what_it_could_not_answer(db_path):
    rid = _restaurant(db_path, module_labor=0)
    brief = bi.executive_brief(rid)
    assert any("labor" in u for u in brief["unanswered"])


# ── the snapshot block ─────────────────────────────────────────────────────

def test_the_snapshot_block_is_empty_when_there_is_nothing_to_say(db_path, monkeypatch):
    """A header with no lines under it is tokens on every single question for
    no information."""
    rid = _restaurant(db_path, module_reviews=0, module_labor=0,
                      module_inventory=0, module_marketing=0)
    monkeypatch.setattr(bi, "_visibility", lambda r: None)
    assert bi.snapshot_block(rid) == ""


def test_the_snapshot_says_plainly_when_nothing_lines_up(db_path, monkeypatch):
    """Silence reads as "I didn't look". The model has to be told the test
    ran and found nothing, or it will connect two findings itself."""
    rid = _restaurant(db_path)
    monkeypatch.setattr(bi, "correlations", lambda *a, **k: [])
    monkeypatch.setattr(bi, "money_at_stake", lambda *a, **k: {
        "ranked": [{"module": "food_cost", "label": "Food cost drivers", "monthly": 300.0,
                    "claim_kind": "computed", "basis": "ranked drivers"}],
        "upside": None, "unavailable": [], "below_floor": [], "floor": 50.0,
        "total_note": "Do not add these together."})
    block = bi.snapshot_block(rid)
    assert "Nothing lines up across modules" in block


def test_marketing_activity_is_context_never_a_cause(db_path, monkeypatch):
    """A campaign and a busy week co-occurring is not evidence one produced
    the other, and this product has no covers, attribution or control group."""
    rid = _restaurant(db_path)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO marketing_content_log (restaurant_id, content_type, post_id, "
                 "post_platform, created_at) VALUES (?,?,?,?,datetime('now'))",
                 (rid, "instagram_post", "p1", "instagram"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(bi, "correlations", lambda *a, **k: [])
    block = bi.snapshot_block(rid)
    if "Marketing in the same 30 days" in block:
        assert "not evidence any of it caused" in block


def test_a_weekday_average_built_on_one_shift_never_produces_a_link(db_path, monkeypatch):
    """dow_summary is a mean with no count attached, so a restaurant with nine
    days synced hands back a "Tuesday" built from exactly one Tuesday. A link
    on that is a link on one shift."""
    rid = _restaurant(db_path)
    for i, when in enumerate(_fridays(4)):
        _negative_review(db_path, rid, f"fri-{i}", when, ["service"])
    monkeypatch.setattr(
        "labor.analyse_shifts_for_restaurant",
        lambda r: {"is_live": True, "period_days": 9,
                   "dow_summary": {"Monday": 30.0, "Tuesday": 30.0,
                                   "Wednesday": 30.0, "Friday": 18.0},
                   "potential_savings_monthly": 0, "labor_target": 30})
    assert [l for l in bi.correlations(rid) if l["kind"] == "reviews_x_labor"] == []


def test_the_headline_states_the_measured_gap(db_path, monkeypatch):
    rid = _restaurant(db_path)
    for i, when in enumerate(_fridays(4)):
        _negative_review(db_path, rid, f"fri-{i}", when, ["service"])
    monkeypatch.setattr(
        "labor.analyse_shifts_for_restaurant",
        lambda r: {"is_live": True, "period_days": 28,
                   "dow_summary": {"Monday": 30.0, "Tuesday": 30.0,
                                   "Wednesday": 30.0, "Friday": 22.0},
                   "potential_savings_monthly": 0, "labor_target": 30})
    link = [l for l in bi.correlations(rid) if l["kind"] == "reviews_x_labor"][0]
    # Average of 30/30/30/22 is 28, so Friday is 6.0 points leaner.
    assert "6.0 points leaner" in link["headline"]


def test_a_weekday_pair_share_is_reported_across_both_days(db_path, monkeypatch):
    """review_intelligence reports a dominant PAIR when that is the real
    shape ("Friday and Saturday dinner"). Its share covers both days, so
    attributing it to whichever day this link matched on would overstate a
    measured figure by roughly double."""
    pair_cluster = {
        "category": "service", "mentions": 8, "window_days": 90,
        "weekday_pair": {"days": ["Friday", "Saturday"], "count": 7, "share": 0.88},
        "weekday": {"value": "Friday", "count": 4, "share": 0.5},
        "review_ids": [1, 2, 3], "dish": None,
    }
    phrase = bi._concentration_phrase(pair_cluster)
    assert "88% across Friday and Saturday together" == phrase
    assert "88% on Friday" not in phrase


def test_a_single_dominant_day_reads_as_that_day():
    single = {"weekday": {"value": "Friday", "count": 5, "share": 1.0}, "weekday_pair": None}
    assert bi._concentration_phrase(single) == "100% on Friday"


def test_labour_at_target_is_reported_as_such_not_as_too_short(db_path, monkeypatch):
    """A zero here is a result, not a missing measurement. Reporting "period
    too short" when the real answer is "you are at target" tells an owner to
    go and sync more data to answer a question that is already answered."""
    rid = _restaurant(db_path)
    monkeypatch.setattr(bi, "gather", lambda *a, **k: {
        "reviews": None, "food_cost": None, "marketing": None, "visibility": None,
        "labor": {"is_live": True, "period_days": 30, "potential_savings_monthly": 0},
        "degraded": [], "modules_off": [], "complete": True})
    reason = {u["module"]: u["reason"] for u in bi.money_at_stake(rid)["unavailable"]}["labor"]
    assert "at or under target" in reason
    assert "too short" not in reason


def test_a_genuinely_short_labour_period_still_says_too_short(db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(bi, "gather", lambda *a, **k: {
        "reviews": None, "food_cost": None, "marketing": None, "visibility": None,
        "labor": {"is_live": True, "period_days": 4, "potential_savings_monthly": 0},
        "degraded": [], "modules_off": [], "complete": True})
    reason = {u["module"]: u["reason"] for u in bi.money_at_stake(rid)["unavailable"]}["labor"]
    assert "too short" in reason
