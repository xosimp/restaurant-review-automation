"""DSR phase 3 — the Food, Reviews, Marketing, Intel and Closeout collectors
(dsr/block_*.py) and the close-out's new fields.

Every collector is `collect(ctx) -> dsr.block(...)`. What each test pins:
a ready block carries the night's real figures; every non-ready status says
why; an unmeasured figure is None, never 0; and another restaurant's rows
never reach this one's block. No network, no model.
"""
import json
import sys
from datetime import date, datetime, timedelta

import pytest
from flask import Flask

import models
import auth
import dsr
from dsr import common
from dsr import block_food, block_reviews, block_marketing, block_intel, block_closeout
import closeout
import covers
import demand_signals
import food_cost_intelligence
import guest_marketing
import inventory
import inventory_ledger
import marketing_drafts
import marketing_publish
import notify
import ops
import pos
import weather
import admin_ops
from models import create_restaurant, Restaurant

DAY = "2026-09-22"          # a Tuesday; the restaurants are on Chicago time
# 12:30am the next morning, Chicago (05:30 UTC): the report runs after close.
AFTER_CLOSE = datetime(2026, 9, 23, 5, 30)


@pytest.fixture
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
    auth.init_auth(db_path=db_path)                # users (a draft names who wrote it)
    guest_marketing.init_guest_marketing(db_path)
    models.init_competitor_snapshots(db_path)
    ops.init_ops(db_path)
    return db_path


def _rid(db, name="DSR Co", **flags):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name[:3].lower()}@x.com"), db_path=db)
    if flags:
        _sql(db, f"UPDATE restaurants SET {', '.join(k + '=?' for k in flags)} WHERE id=?", (*flags.values(), rid))
    return rid


def _sql(db, sql, args=()):
    c = models.get_conn(db)
    cur = c.execute(sql, args)
    c.commit()
    lid = cur.lastrowid
    c.close()
    return lid


def _ctx(db, rid, day=DAY, now=AFTER_CLOSE):
    return dsr.Context(models.get_restaurant(rid, db_path=db), day, db_path=db, now_utc=now)


def _no_zero_for_unknown(block, keys):
    for k in keys:
        assert block["metrics"][k] is None, (k, block["metrics"][k])


# ── the shared window ───────────────────────────────────────────────────────

def test_a_business_date_runs_from_the_start_hour_to_the_next_morning(db):
    r = models.get_restaurant(_rid(db), db_path=db)
    start, end = common.local_bounds(r, date(2026, 9, 22))
    assert (start, end) == (datetime(2026, 9, 22, 5), datetime(2026, 9, 23, 5))
    assert common.utc_bounds(r, date(2026, 9, 22)) == ("2026-09-22 10:00:00", "2026-09-23 10:00:00")
    assert common.in_local_day("2026-09-23T01:10:00", r, date(2026, 9, 22))     # a 1am review is last night's
    assert not common.in_local_day("2026-09-23T06:00:00", r, date(2026, 9, 22))
    assert common.in_local_day("2026-09-22", r, date(2026, 9, 22))             # a bare CSV date


def test_a_late_close_stretches_the_night(db):
    rid = _rid(db, open_times_json=json.dumps({"Tuesday": "4:00pm"}), close_times_json=json.dumps({"Tuesday": "6:00am"}))
    r = models.get_restaurant(rid, db_path=db)
    assert common.local_bounds(r, date(2026, 9, 22))[1] == datetime(2026, 9, 23, 6)


# ── Food ────────────────────────────────────────────────────────────────────

def _kitchen(db, rid, sales=True, count=True):
    """Beef ($20/lb, 0.25 lb a burger) and buns; 10 burgers and 5 fries sold
    on DAY; a count of the beef on DAY came in a pound under the ledger."""
    beef = _sql(db, "INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, par_level, current_stock, "
                    "avg_daily_usage) VALUES (?,?,?,?,?,?,?)", (rid, "Beef", "lb", 20.0, 20, 2, 3))
    bun = _sql(db, "INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, par_level, current_stock, "
                   "avg_daily_usage) VALUES (?,?,?,?,?,?,?)", (rid, "Bun", "ea", 0.5, 50, 12, 4))
    burger = _sql(db, "INSERT INTO menu_items (restaurant_id, toast_guid, name, sell_price) VALUES (?,?,?,?)",
                  (rid, f"g-burger-{rid}", "Burger", 12.0))
    fries = _sql(db, "INSERT INTO menu_items (restaurant_id, toast_guid, name, sell_price) VALUES (?,?,?,?)",
                 (rid, f"g-fries-{rid}", "Fries", 4.0))
    _sql(db, "INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)", (burger, beef, 0.25))
    _sql(db, "INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)", (burger, bun, 1))
    if sales:
        _sql(db, "INSERT INTO menu_item_sales (restaurant_id, menu_item_id, business_date, qty_sold) VALUES (?,?,?,?)",
             (rid, burger, DAY, 10))
        _sql(db, "INSERT INTO menu_item_sales (restaurant_id, menu_item_id, business_date, qty_sold) VALUES (?,?,?,?)",
             (rid, fries, DAY, 5))
        _sql(db, "INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, source) "
                 "VALUES (?,?,?,?,?,?)", (rid, beef, "depletion", 2.5, DAY, "toast"))
    if count:
        _sql(db, "INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, source) "
                 "VALUES (?,?,?,?,?,?)", (rid, beef, "waste", 1.0, DAY, "inferred"))
        _sql(db, "INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, source) "
                 "VALUES (?,?,?,?,?,?)", (rid, beef, "recount", 2.0, DAY, "manual"))
    return {"beef": beef, "bun": bun, "burger": burger, "fries": fries}


def test_food_is_ready_with_the_nights_estimate_waste_stock_and_variance(db):
    rid = _rid(db)
    _kitchen(db, rid)
    b = block_food.collect(_ctx(db, rid))
    m = b["metrics"]
    assert b["status"] == dsr.READY and b["reason"] is None
    # 10 burgers × ($5 beef + $0.50 bun); fries have no recipe.
    assert m["est_food_cost"] == 55.0
    assert m["recipe_coverage_pct"] == 66.7            # 10 of 15 units had a costed recipe
    assert m["est_food_cost_pct"] == 45.8              # $55 on $120 of burgers
    assert b["detail"]["estimate"]["estimated"] is True and "Estimated" in b["detail"]["estimate"]["label"]
    assert b["detail"]["estimate"]["without_recipe"][0]["dish"] == "Fries"
    assert (m["waste_logged"], m["waste_inferred"], m["waste_counted"]) == (20.0, 20.0, 0.0)
    assert (m["critical_low"], m["low_stock"]) == (1, 1)
    assert b["detail"]["stock"]["critical"][0]["item"] == "Beef"
    # 1 lb gap on 2.5 lb theoretical = 40%, $20: material.
    assert (m["variance_cost"], m["variance_items"]) == (20.0, 1)
    assert b["detail"]["variance"]["items"][0]["dish"] == "Burger"


def test_the_estimated_food_cost_pct_needs_most_units_costed_and_speaks_in_units(db):
    """CA1 D5 (fix I11): no coverage floor, and "dishes" where the figure is
    counted in units. 10 burgers against 25 uncosted fries is 29% covered —
    the costed dollars stay, the percentage is withheld and says why."""
    rid = _rid(db)
    k = _kitchen(db, rid)
    _sql(db, "UPDATE menu_item_sales SET qty_sold=25 WHERE restaurant_id=? AND menu_item_id=?", (rid, k["fries"]))
    b = block_food.collect(_ctx(db, rid))
    m, est = b["metrics"], b["detail"]["estimate"]
    assert m["recipe_coverage_pct"] == 28.6 and m["est_food_cost"] == 55.0
    assert m["est_food_cost_pct"] is None
    assert est["coverage_floor_pct"] == block_food.ESTIMATE_MIN_COVERAGE_PCT
    assert "withheld until it reaches" in est["coverage_note"]
    assert "units sold" in est["label"] and "dishes should have cost" not in est["label"]


def test_food_recoverable_is_the_deduplicated_total_not_the_plain_sum(db, monkeypatch):
    rid = _rid(db)
    _kitchen(db, rid)
    drivers = [{"label": "Beef waste", "dollars_monthly": 100.0, "confidence": "medium", "item": "Beef"},
               {"label": "Beef price up", "dollars_monthly": 80.0, "confidence": "high", "item": "Beef"}]
    monkeypatch.setattr(food_cost_intelligence, "cost_drivers", lambda rid_, db_path=None: {
        "drivers": drivers, "total_monthly": 180.0,
        "total_monthly_deduplicated": food_cost_intelligence.deduplicated_total(drivers)["total"],
        "complete": True, "degraded_sources": [], "total_basis": "the largest driver per ingredient"})
    b = block_food.collect(_ctx(db, rid))
    # NS3 H3: the driver total is published as what it is — at stake across
    # the drivers — and "recoverable" is inventory's figure, one meaning on
    # every surface.
    assert b["metrics"]["drivers_at_stake_monthly"] == 100.0
    import inventory
    assert b["metrics"]["recoverable_monthly"] == inventory.analysis_for(rid)[2]["recoverable_monthly"]
    assert b["detail"]["recoverable"]["kind"] == "opportunity"


def test_food_waits_for_the_nights_item_sales_and_says_so(db):
    rid = _rid(db)
    k = _kitchen(db, rid, sales=False, count=False)
    # Item sales arrived on earlier nights: tonight's are coming.
    _sql(db, "INSERT INTO menu_item_sales (restaurant_id, menu_item_id, business_date, qty_sold) VALUES (?,?,?,?)",
         (rid, k["burger"], "2026-09-20", 7))
    b = block_food.collect(_ctx(db, rid))
    assert b["status"] == dsr.AWAITING
    assert b["reason"] == "Item sales for 9/22/26 haven't synced yet — the food cost estimate follows"
    _no_zero_for_unknown(b, ("est_food_cost", "est_food_cost_pct", "recipe_coverage_pct"))
    # Everything else is already in the block.
    assert b["metrics"]["critical_low"] == 1
    # A week later they never came: no item sales that night, not "awaiting".
    later = block_food.collect(_ctx(db, rid, now=datetime(2026, 9, 30, 12)))
    assert later["status"] == dsr.READY and later["metrics"]["est_food_cost"] is None
    assert "No item sales were recorded for 9/22/26" in later["detail"]["note"]


def test_food_never_reports_zero_for_what_was_not_measured(db):
    rid = _rid(db)
    _sql(db, "INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, par_level, current_stock, "
             "avg_daily_usage) VALUES (?,?,?,?,?,?,?)", (rid, "Salt", "lb", 1.0, 1, 5, 0.1))
    b = block_food.collect(_ctx(db, rid))
    assert b["status"] == dsr.READY
    # No recipes, no count that night, no count that week.
    _no_zero_for_unknown(b, ("est_food_cost", "recipe_coverage_pct", "waste_logged", "waste_inferred",
                             "variance_cost", "variance_items"))
    assert "No recipes are set up" in b["detail"]["note"]
    assert "Nothing was counted" in b["detail"]["waste"]["note"]
    assert "No count was taken" in b["detail"]["variance"]["note"]


def test_food_on_an_inventory_sheet_has_no_nightly_waste_or_variance(db):
    rid = _rid(db)
    csv = "item,category,par_level,current_stock,unit_cost,avg_daily_usage,last_order_qty,waste_last_week,unit\n" \
          "Beef,Protein,20,2,20,3,10,1,lb\n"
    _sql(db, "INSERT INTO client_data (restaurant_id, inventory_csv) VALUES (?,?)", (rid, csv))
    b = block_food.collect(_ctx(db, rid))
    assert b["status"] == dsr.READY and b["metrics"]["critical_low"] == 1
    _no_zero_for_unknown(b, ("waste_logged", "variance_cost", "est_food_cost"))
    assert "per week on your inventory sheet" in b["detail"]["waste"]["note"]


def test_food_says_why_when_it_is_not_ready(db, monkeypatch):
    off = _rid(db, "Off Co", module_inventory=0)
    b = block_food.collect(_ctx(db, off))
    assert b["status"] == dsr.NOT_CONNECTED and b["reason"] == "Food Cost isn't switched on for this location"

    bare = _rid(db, "Bare Co")                   # no ingredients, no sheet: sample data
    b = block_food.collect(_ctx(db, bare))
    assert b["status"] == dsr.NOT_CONNECTED and b["reason"].startswith("Inventory isn't set up")
    assert b["metrics"] == {}

    def boom(*a, **k):
        raise RuntimeError("ledger locked")
    monkeypatch.setattr(inventory, "analysis_for", boom)
    b = block_food.collect(_ctx(db, bare))
    assert b["status"] == dsr.UNAVAILABLE and b["reason"] == "Inventory data unavailable"


def test_food_never_reads_another_restaurants_kitchen(db):
    mine, theirs = _rid(db, "Mine Co"), _rid(db, "Theirs Co")
    _kitchen(db, theirs)
    b = block_food.collect(_ctx(db, mine))
    assert b["status"] == dsr.NOT_CONNECTED               # theirs is set up; mine is not
    _kitchen(db, mine, sales=False, count=False)
    b = block_food.collect(_ctx(db, mine, now=datetime(2026, 9, 30, 12)))
    assert b["metrics"]["est_food_cost"] is None and b["metrics"]["waste_logged"] is None
    assert b["metrics"]["variance_cost"] is None


# ── Reviews ─────────────────────────────────────────────────────────────────

def _review(db, rid, at, rating=5, sentiment="positive", cats=("food",), urgency="normal",
            status="pending", posted_at=None, ext=None):
    import uuid
    return _sql(db, "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                    "fetched_at, sentiment, categories, urgency, response_status, posted_at, processed, summary, "
                    "draft_response) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, "google", ext or uuid.uuid4().hex[:10], "Guest", rating, "text", at, at, sentiment,
                 json.dumps(list(cats)) if cats is not None else None, urgency, status, posted_at,
                 1 if sentiment else 0, f"{rating} stars", "Thanks!" if status == "drafted" else None))


def _live(db, rid, fetched):
    _sql(db, "UPDATE restaurants SET reviews_live=1, last_fetched_at=? WHERE id=?", (fetched, rid))


def test_reviews_ready_with_the_nights_reviews_themes_urgent_drafts_and_replies(db):
    rid = _rid(db)
    _live(db, rid, "2026-09-23T08:05:00")                 # the 8am fetch came after the night ended
    _review(db, rid, "2026-09-22T12:00:00", 5, "positive", ("food", "service"))
    _review(db, rid, "2026-09-22T23:30:00", 2, "negative", ("wait time", "food"), urgency="high")
    _review(db, rid, "2026-09-23T01:10:00", 4, None, None)            # after midnight: still last night; not analysed
    _review(db, rid, "2026-09-23T06:00:00", 1, "negative", ("service",))   # the next business day
    _review(db, rid, "2026-09-21T20:00:00", 3, "neutral", ("value",))      # the night before
    # A reply posted at 3pm Chicago on the night, one the next morning.
    _review(db, rid, "2026-09-10", 5, status="posted", posted_at="2026-09-22 20:00:00")
    _review(db, rid, "2026-09-10", 5, status="posted", posted_at="2026-09-23 11:00:00")
    # One draft owed, written today (whatever today is).
    _sql(db, "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, fetched_at, "
             "processed, response_status, draft_response) VALUES (?,?,?,?,?,?,date('now'),datetime('now'),1,'drafted','Thanks')",
         (rid, "google", "draft-1", "D", 4, "ok"))
    b = block_reviews.collect(_ctx(db, rid))
    m = b["metrics"]
    assert b["status"] == dsr.READY and b["detail"]["sync"]["state"] == "complete"
    assert (m["received"], m["positive"], m["negative"], m["not_analysed"], m["urgent"]) == (3, 1, 1, 1, 1)
    # Three reviews are not a night's rating: the floor metrics applies
    # everywhere (thresholds.RATING_MIN_REVIEWS = 5; CA1 D6, fix I11). The
    # count is stated and the rating is not.
    assert m["avg_rating"] is None
    assert b["detail"]["rating_note"] == "3 reviews — not rated (a rating needs 5)"
    assert (m["drafts_awaiting"], m["replies_posted"]) == (1, 1)
    themes = b["detail"]["themes"]
    assert [t["theme"] for t in themes["negative"]] == ["food", "wait time"]
    assert [t["theme"] for t in themes["positive"]] == ["food", "service"]
    assert b["detail"]["urgent"][0]["rating"] == 2


def test_reviews_is_awaiting_when_the_fetch_is_behind_and_counts_nothing_it_cannot(db):
    rid = _rid(db)
    _live(db, rid, "2026-09-20T20:05:00")                 # three days of missed fetches
    _review(db, rid, "2026-09-22T12:00:00", 5)
    b = block_reviews.collect(_ctx(db, rid))
    assert b["status"] == dsr.AWAITING and b["reason"] == "Review sync delayed"
    _no_zero_for_unknown(b, ("received", "avg_rating", "positive", "negative", "urgent"))
    assert b["metrics"]["replies_posted"] == 0            # our own posting is known either way

    never = _rid(db, "Never Co")
    _sql(db, "UPDATE restaurants SET reviews_live=1 WHERE id=?", (never,))
    b = block_reviews.collect(_ctx(db, never))
    assert b["status"] == dsr.AWAITING and b["reason"] == "Reviews haven't synced yet"


def test_reviews_on_schedule_but_not_since_close_is_ready_and_says_how_far_it_runs(db):
    rid = _rid(db)
    _live(db, rid, "2026-09-22T20:05:00")                 # the 8pm fetch; the report runs at 11:30pm
    b = block_reviews.collect(_ctx(db, rid, now=datetime(2026, 9, 23, 4, 30)))
    assert b["status"] == dsr.READY and b["detail"]["sync"]["state"] == "current"
    assert "run through 9/22/26 · 8:05pm" in b["detail"]["sync"]["note"]
    # Nothing arrived: a count of zero, and no average of nothing.
    assert b["metrics"]["received"] == 0 and b["metrics"]["avg_rating"] is None


def test_reviews_not_connected_says_why(db):
    b = block_reviews.collect(_ctx(db, _rid(db)))
    assert b["status"] == dsr.NOT_CONNECTED and b["reason"] == "Google reviews aren't connected"
    off = _rid(db, "Off Co", module_reviews=0, reviews_live=1)
    b = block_reviews.collect(_ctx(db, off))
    assert b["status"] == dsr.NOT_CONNECTED and b["reason"] == "Reviews aren't switched on for this location"


def test_reviews_never_counts_another_restaurants_reviews(db):
    mine, theirs = _rid(db, "Mine Co"), _rid(db, "Theirs Co")
    _live(db, mine, "2026-09-23T08:05:00")
    _live(db, theirs, "2026-09-23T08:05:00")
    _review(db, theirs, "2026-09-22T12:00:00", 1, "negative", urgency="high", ext="same-id")
    _review(db, theirs, "2026-09-10", 5, status="posted", posted_at="2026-09-22 20:00:00")
    _review(db, mine, "2026-09-22T13:00:00", 5, ext="same-id")
    b = block_reviews.collect(_ctx(db, mine))
    assert (b["metrics"]["received"], b["metrics"]["urgent"], b["metrics"]["replies_posted"]) == (1, 0, 0)
    assert b["metrics"]["avg_rating"] is None                   # one review: counted, not rated
    assert b["detail"]["rating_note"].startswith("1 review — not rated")


# ── Marketing ───────────────────────────────────────────────────────────────

def _post(db, rid, at, reach=0, likes=0, post_id="p1", topic="Patio night"):
    return _sql(db, "INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, post_platform, "
                    "created_at, posted_at, reach, likes) VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, "instagram", topic, post_id, "instagram", at, at, reach, likes))


def _campaign(db, rid, at, sent=40, token=None, visits=None, through=None):
    return _sql(db, "INSERT INTO guest_campaigns (restaurant_id, message, sent_count, failed_count, created_at, "
                    "link_token, visits_matched, attribution_through) VALUES (?,?,?,?,?,?,?,?)",
                (rid, "Half-price wine tonight", sent, 1, at, token, visits, through))


def test_marketing_ready_with_posts_campaigns_and_whats_next(db):
    rid = _rid(db)
    _post(db, rid, "2026-09-22 22:00:00", reach=500, likes=40)          # 5pm Chicago: the night
    _post(db, rid, "2026-09-23 12:00:00", reach=900, likes=90, post_id="p2")   # next day
    _post(db, rid, "2026-09-22 22:30:00", post_id=None)                # a draft, never published
    _sql(db, "INSERT INTO marketing_links (restaurant_id, token, target_url, clicks) VALUES (?,?,?,?)",
         (rid, "tok1", "https://x.test", 7))
    _campaign(db, rid, "2026-09-22 23:00:00", token="tok1")
    _campaign(db, rid, "2026-09-15 23:00:00", visits=3, through="2026-09-21")   # earlier, measured
    _sql(db, "INSERT INTO guest_newsletters (restaurant_id, subject, body, content_hash, total, created_at) "
             "VALUES (?,?,?,?,?,?)", (rid, "Fall menu", "b", "h", 120, "2026-09-22 18:00:00"))
    for when, status in (("2026-09-24T17:00:00", "scheduled"), ("2026-10-15T17:00:00", "scheduled"),
                         ("2026-09-22T17:00:00", "failed")):
        _sql(db, "INSERT INTO marketing_scheduled_posts (restaurant_id, platform, topic, body, scheduled_for, status) "
                 "VALUES (?,?,?,?,?,?)", (rid, "facebook", "Trivia", "b", when, status))
    marketing_drafts.save_draft(rid, "Taco Tuesday is back", topic="Tacos", db_path=db)
    approved = marketing_drafts.save_draft(rid, "Approved one", db_path=db)["id"]
    _sql(db, "UPDATE marketing_drafts SET status='approved' WHERE id=?", (approved,))
    b = block_marketing.collect(_ctx(db, rid))
    m = b["metrics"]
    assert b["status"] == dsr.READY
    assert (m["posts_published"], m["reach"], m["engagement"], m["engagement_rate"]) == (1, 500, 40, 8.0)
    assert m["posts_failed"] == 1
    assert (m["campaigns_sent"], m["texts_sent"], m["campaign_clicks"]) == (2, 40, 7)
    assert m["campaign_visits"] is None                   # not attributed yet: not zero
    assert b["detail"]["campaigns"]["earlier_results"][0]["visits_matched"] == 3
    assert (m["scheduled_next_7d"], m["drafts_awaiting"]) == (1, 1)
    assert b["detail"]["upcoming"]["scheduled"][0]["at"] == "9/24/26 · 5:00pm"


def test_marketing_never_reports_zero_engagement_for_an_unmeasured_post(db):
    rid = _rid(db)
    _post(db, rid, "2026-09-22 22:00:00")                 # no insights yet: reach 0 is "not measured"
    _campaign(db, rid, "2026-09-22 23:00:00")             # no tracked link, no attribution
    b = block_marketing.collect(_ctx(db, rid))
    assert b["metrics"]["posts_published"] == 1
    _no_zero_for_unknown(b, ("reach", "engagement", "engagement_rate", "campaign_clicks", "campaign_visits"))


def test_marketing_says_why_when_it_is_not_ready(db, monkeypatch):
    off = _rid(db, "Off Co", module_marketing=0)
    b = block_marketing.collect(_ctx(db, off))
    assert b["status"] == dsr.NOT_CONNECTED and b["reason"] == "Marketing isn't switched on for this location"

    def boom(*a, **k):
        raise RuntimeError("no table")
    monkeypatch.setattr(block_marketing, "_posts", boom)
    monkeypatch.setattr(block_marketing, "_campaigns", boom)
    b = block_marketing.collect(_ctx(db, _rid(db)))
    assert b["status"] == dsr.UNAVAILABLE and b["reason"] == "Marketing data unavailable"


def test_marketing_never_shows_another_restaurants_posts_or_campaigns(db):
    mine, theirs = _rid(db, "Mine Co"), _rid(db, "Theirs Co")
    _post(db, theirs, "2026-09-22 22:00:00", reach=500, likes=40)
    _campaign(db, theirs, "2026-09-22 23:00:00")
    marketing_drafts.save_draft(theirs, "Theirs", db_path=db)
    b = block_marketing.collect(_ctx(db, mine))
    m = b["metrics"]
    assert (m["posts_published"], m["campaigns_sent"], m["drafts_awaiting"]) == (0, 0, 0)
    assert m["reach"] is None


# ── Intel ───────────────────────────────────────────────────────────────────

def _periods(*rows):
    return [{"name": name, "startTime": f"{d}T{'06' if day else '18'}:00:00-05:00", "isDaytime": day,
             "temperature": t, "shortForecast": sf, "probabilityOfPrecipitation": {"value": p}}
            for d, name, day, t, sf, p in rows]


def _forecast(db, rid, periods, cached_at=None):
    _sql(db, "UPDATE restaurants SET latitude=41.9, longitude=-87.6, weather_cache_json=?, weather_cached_at=? WHERE id=?",
         (json.dumps(periods), (cached_at or datetime.now()).isoformat(), rid))


def test_intel_ready_with_the_forecast_events_and_measured_traffic(db):
    rid = _rid(db)
    _forecast(db, rid, _periods((DAY, "Tuesday", True, 84, "Chance Rain Showers", 60),
                                (DAY, "Tuesday Night", False, 70, "Mostly Cloudy", 20)))
    demand_signals.save(rid, [{"date": DAY, "kind": "event", "label": "Cubs home game"},
                              {"date": DAY, "kind": "reservations", "covers": 40}], db_path=db)
    # Eight Tuesdays of 80 covers before, 100 on the night.
    covers.save(rid, [{"date": (date(2026, 9, 22) - timedelta(weeks=w)).isoformat(), "covers": 80}
                      for w in range(1, 9)] + [{"date": DAY, "covers": 100}], db_path=db)
    b = block_intel.collect(_ctx(db, rid))
    m, d = b["metrics"], b["detail"]
    assert b["status"] == dsr.READY
    assert d["weather"]["summary"] == "Chance Rain Showers · high 84° · 60% rain"
    assert d["weather"]["basis"] == "forecast" and "not observed" in d["weather"]["note"]
    assert (m["weather_high_f"], m["weather_low_f"], m["weather_precip_pct"]) == (84, 70, 60)
    assert d["events"]["summary"] == "Cubs home game"
    assert (m["events_listed"], m["reservations_covers"]) == (1, 40)
    assert (m["covers"], m["covers_vs_typical_pct"]) == (100, 25.0)
    assert d["traffic"]["typical_covers"] == 80


def test_intel_says_nothing_listed_and_omits_traffic_it_did_not_measure(db):
    rid = _rid(db)                                        # no coordinates, no events, no covers
    b = block_intel.collect(_ctx(db, rid))
    assert b["status"] == dsr.READY
    assert b["detail"]["events"]["summary"] == "Nothing listed" and b["metrics"]["events_listed"] == 0
    assert b["detail"]["weather"]["summary"] is None
    _no_zero_for_unknown(b, ("weather_high_f", "weather_precip_pct", "competitor_moves", "reservations_covers"))
    assert "traffic" not in b["detail"] and "covers" not in b["metrics"]
    assert b["detail"]["competitors"]["tracked"] is False


def test_intel_lists_a_holiday_on_the_date(db):
    b = block_intel.collect(_ctx(db, _rid(db), day="2026-10-31", now=datetime(2026, 11, 1, 6)))
    assert b["detail"]["events"]["summary"] == "Halloween"


def test_intel_uses_tonight_when_the_days_forecast_is_gone(db):
    rid = _rid(db)
    _forecast(db, rid, _periods((DAY, "Tonight", False, 61, "Mostly Clear", 0)))
    b = block_intel.collect(_ctx(db, rid))
    assert b["detail"]["weather"]["summary"] == "Mostly Clear · low 61°"
    assert b["metrics"]["weather_high_f"] is None and b["metrics"]["weather_low_f"] == 61


def test_a_refresh_after_dark_keeps_the_days_forecast_from_the_older_copy(db, monkeypatch):
    rid = _rid(db)
    _forecast(db, rid, _periods((DAY, "Tuesday", True, 84, "Sunny", 0)),
              cached_at=datetime.now() - timedelta(hours=weather._CACHE_HOURS + 5))
    monkeypatch.setattr(weather, "_fetch_periods_ex", lambda lat, lon: (
        _periods((DAY, "Tonight", False, 61, "Clear", 0), ("2026-09-23", "Wednesday", True, 80, "Sunny", 0)), None))
    got = weather.forecast_for_day(models.get_restaurant(rid, db_path=db), DAY, db_path=db)
    assert got["day"]["short_forecast"] == "Sunny" and got["night"]["low_f"] == 61


def test_intel_competitor_moves_are_only_seen_on_a_night_a_check_ran(db, monkeypatch):
    rid = _rid(db)
    moves = [{"name": "Luigi's", "place_id": "p1", "kind": "moved", "line": "Luigi's is up 0.4★ (4.2 → 4.6)",
              "evidence": "100 → 130 Google reviews (30 new)", "rating_now": 4.6, "size": 0.4}]
    monkeypatch.setattr(notify, "competitor_changes", lambda rid_, db_path=None: moves if rid_ == rid else [])
    _sql(db, "INSERT INTO competitor_snapshots (restaurant_id, place_id, name, rating, captured_at) VALUES (?,?,?,?,?)",
         (rid, "p1", "Luigi's", 4.2, "2026-09-15 15:00:00"))
    b = block_intel.collect(_ctx(db, rid))
    assert b["metrics"]["competitor_moves"] is None       # no check that night: unknown, not 0
    assert b["detail"]["competitors"]["last_check"] == "9/15/26"
    _sql(db, "INSERT INTO competitor_snapshots (restaurant_id, place_id, name, rating, captured_at) VALUES (?,?,?,?,?)",
         (rid, "p1", "Luigi's", 4.6, "2026-09-22 15:00:00"))
    b = block_intel.collect(_ctx(db, rid))
    assert b["metrics"]["competitor_moves"] == 1
    assert b["detail"]["competitors"]["moves"][0]["name"] == "Luigi's"


def test_intel_unavailable_only_when_nothing_could_be_read(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    for target, name in ((block_intel, "_weather"), (block_intel, "_competitors"), (block_intel, "_holiday"),
                         (demand_signals, "upcoming")):
        monkeypatch.setattr(target, name, boom)
    b = block_intel.collect(_ctx(db, _rid(db)))
    assert b["status"] == dsr.UNAVAILABLE and b["reason"] == "Local data unavailable"


def test_an_unreadable_events_list_is_not_nothing_listed(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("locked")
    monkeypatch.setattr(demand_signals, "upcoming", boom)
    b = block_intel.collect(_ctx(db, _rid(db)))
    assert b["status"] == dsr.READY                      # the rest of the block still reads
    assert b["metrics"]["events_listed"] is None and b["detail"]["events"]["summary"] is None
    assert "events" in b["detail"]["unavailable_parts"]


def test_intel_never_shows_another_restaurants_events_covers_or_competitors(db, monkeypatch):
    mine, theirs = _rid(db, "Mine Co"), _rid(db, "Theirs Co")
    demand_signals.save(theirs, [{"date": DAY, "kind": "event", "label": "Their gala"}], db_path=db)
    covers.save(theirs, [{"date": (date(2026, 9, 22) - timedelta(weeks=w)).isoformat(), "covers": 80}
                         for w in range(0, 9)], db_path=db)
    _sql(db, "INSERT INTO competitor_snapshots (restaurant_id, place_id, name, rating, captured_at) VALUES (?,?,?,?,?)",
         (theirs, "p1", "Luigi's", 4.6, "2026-09-22 15:00:00"))
    b = block_intel.collect(_ctx(db, mine))
    assert b["detail"]["events"]["summary"] == "Nothing listed"
    assert "covers" not in b["metrics"] and b["detail"]["competitors"]["tracked"] is False


# ── Closeout ────────────────────────────────────────────────────────────────

def test_the_closeout_takes_the_six_dsr_fields(db):
    rid = _rid(db)
    closeout.save(rid, {"went_well": "Patio ran clean", "equipment": "Fryer 2 down",
                        "vip_guests": "The Hendersons, table 12", "maintenance": "Leak under the bar sink",
                        "shift_notes": "Short a busser", "general_notes": "New menu cards",
                        "influence": "Rain kept the patio empty until 7"},
                  submitted_by="Sam", business_date=DAY, db_path=db)
    got = closeout.get(rid, DAY, db_path=db)
    assert got["influence"] == "Rain kept the patio empty until 7" and got["equipment"] == "Fryer 2 down"
    assert set(closeout.DSR_FIELDS) == {"equipment", "vip_guests", "maintenance", "shift_notes",
                                        "general_notes", "influence"}


def test_an_older_app_re_filing_four_lines_keeps_the_dsr_fields(db):
    rid = _rid(db)
    closeout.save(rid, {"went_wrong": "Slow tickets", "influence": "Cubs game let out at 9"},
                  business_date=DAY, db_path=db)
    # The pre-DSR app sends only its four keys.
    closeout.save(rid, {"went_well": "", "went_wrong": "Slow tickets till 8", "eighty_sixed": "", "callouts": ""},
                  business_date=DAY, db_path=db)
    got = closeout.get(rid, DAY, db_path=db)
    assert got["went_wrong"] == "Slow tickets till 8" and got["influence"] == "Cubs game let out at 9"
    # Sending a field empty clears it.
    closeout.save(rid, {"influence": ""}, business_date=DAY, db_path=db)
    assert closeout.get(rid, DAY, db_path=db)["influence"] is None
    with pytest.raises(ValueError):
        closeout.save(rid, {k: "" for k in closeout.FIELDS}, business_date=DAY, db_path=db)


def test_the_brief_line_carries_equipment_and_maintenance(db):
    line = closeout.summarise({"submitted_by": "Sam", "equipment": "Fryer 2 down", "maintenance": "Leak"})
    assert line == "Sam at close — equipment: Fryer 2 down · maintenance: Leak"


def test_closeout_block_returns_the_managers_own_words(db):
    rid = _rid(db)
    closeout.save(rid, {"went_wrong": "  Ticket times over 20 min  ", "influence": "Rain, muggy — patio dead"},
                  submitted_by="Sam", user_id=7, business_date=DAY, db_path=db)
    _sql(db, "UPDATE close_outs SET created_at='2026-09-23 04:48:00' WHERE restaurant_id=?", (rid,))
    b = block_closeout.collect(_ctx(db, rid))
    d = b["detail"]
    assert b["status"] == dsr.READY and b["source"] == "manager"
    assert d["written_by"] == "manager" and d["verbatim"] is True
    assert d["fields"]["influence"] == "Rain, muggy — patio dead"
    assert d["fields"]["went_wrong"] == "Ticket times over 20 min"
    assert d["fields"]["equipment"] is None
    assert (d["submitted_by"], d["user_id"], d["filed_at_label"]) == ("Sam", 7, "9/22/26 · 11:48pm")
    assert b["metrics"] == {"filed": 1, "fields_written": 2}


def test_no_closeout_filed_is_said_not_an_error(db, monkeypatch):
    b = block_closeout.collect(_ctx(db, _rid(db)))
    assert b["status"] == dsr.UNAVAILABLE and b["reason"] == "No manager closeout filed"
    assert b["metrics"] == {"filed": 0}

    def boom(*a, **k):
        raise RuntimeError("locked")
    monkeypatch.setattr(closeout, "get", boom)
    b = block_closeout.collect(_ctx(db, _rid(db, "Other Co")))
    assert b["status"] == dsr.UNAVAILABLE and b["metrics"] == {"filed": None}


def test_closeout_block_never_reads_another_restaurants_closeout(db):
    mine, theirs = _rid(db, "Mine Co"), _rid(db, "Theirs Co")
    closeout.save(theirs, {"influence": "Theirs"}, business_date=DAY, db_path=db)
    b = block_closeout.collect(_ctx(db, mine))
    assert b["status"] == dsr.UNAVAILABLE and b["metrics"] == {"filed": 0}


@pytest.fixture
def routes(db):
    from strategy_routes import strategy_bp, strategy_mobile_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    return app.test_client()


def test_the_closeout_routes_take_the_new_fields_on_web_and_mobile(routes, db, monkeypatch):
    rid = _rid(db)
    monkeypatch.setattr(auth, "get_current_user", lambda: {
        "id": 5, "restaurant_id": rid, "is_admin": 0, "role": "manager", "username": "sam", "email": "s@x.com"})
    got = routes.get("/api/closeout").get_json()
    assert got["ok"] and got["dsr_fields"] == list(closeout.DSR_FIELDS)
    assert got["labels"]["influence"].startswith("Influence — what was going on")
    body = routes.post("/api/closeout", json={"went_well": "Clean", "vip_guests": "Mayor Lee"}).get_json()
    assert body["ok"] and body["closeout"]["vip_guests"] == "Mayor Lee"

    uid = auth.create_user(rid, "gm", "gm@x.com", "pw", db_path=db)
    token = auth.create_session(uid, db_path=db)
    body = routes.post("/mobile/api/closeout", json={"influence": "Rain", "equipment": "Ice machine"},
                       headers={"Authorization": f"Bearer {token}"}).get_json()
    assert body["ok"] and body["closeout"]["influence"] == "Rain" and body["closeout"]["vip_guests"] == "Mayor Lee"
    assert routes.post("/mobile/api/closeout", json={"influence": "x"}).status_code == 401
