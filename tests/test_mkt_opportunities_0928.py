"""The Marketing Opportunity Feed, 9/28/26 (marketing_opportunities), and
its re-audit fixes the same day (OPP-1 … OPP-19, AUX-3/7/9, first audit #43).

Opening Marketing shows what Cavnar AI found worth doing, from measured
signals: a reliably slow weekday ahead, last year's holiday night, a POS
category falling past its own swing, a high-margin dish nobody orders, a
dish guests praise, an opted-in list nobody has contacted, nothing posted
lately. Every figure is measured and says what it is — a gap, never an
expected return — and a card rests on a floor or doesn't appear. Answered
cards go; facts carry no confidence; nothing calls a model on load.

Dates: nothing here is shaped by the day the suite runs. The slow-night
rules are checked with EVERY weekday as "today" (WEEK, a fixed Monday to
Sunday, passed in); the feed-level tests put the slow night two days ahead
of the host date (SLOW), inside a window that leaves today out."""
import json
import time
from datetime import date, datetime, timedelta

import pytest
from flask import Flask

import auth
import auth_routes
import client_api
import demand
import guest_email
import guest_marketing as gm
import insight_store
import marketing_opportunities as mo
import marketing_signals
import menu_intelligence
import mobile_api
import models
import rec_learning
import rec_ledger
import schedule_economics
import strategy_routes
from auth import create_user, init_auth
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant, get_conn, get_restaurant, update_restaurant
from time_utils import mdy

SRC = open("templates/dashboard.html", encoding="utf-8").read()
# The restaurant's date (operator time when no zone is set), as the feed
# reads it - not the machine's (CI is UTC: tomorrow after 7pm Central).
from time_utils import restaurant_now as _rnow
TODAY = _rnow(None, naive=True).date()
# The slow night is always two days ahead of the day the suite runs. Pinned
# to "Tuesday" it failed every Tuesday: the 56-day window then drops the
# oldest Tuesday and "8 of the last 8" reads "7 of the last 7" (re-audit OPP-20).
SLOW = (TODAY + timedelta(days=2)).strftime("%A")
NOW = datetime.combine(TODAY, datetime.min.time()).replace(hour=12)
# A fixed week, Monday 10/5/26 to Sunday 10/11/26: every weekday is "today" once.
WEEK = [date(2026, 10, 5) + timedelta(days=i) for i in range(7)]
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
OWNER = {"id": 1, "role": "owner", "is_admin": 0}
MANAGER = {"id": 5, "role": "manager", "is_admin": 0, "grants": frozenset()}


@pytest.fixture
def db(db_path, monkeypatch):
    gm.init_guest_marketing(db_path)
    insight_store.init_insight_store(db_path)
    rec_ledger.init_rec_ledger(db_path)
    from dsr import store as dstore
    dstore.init_dsr(db_path)
    import metrics
    import outcomes
    real = models.get_conn
    for mod in (models, gm, menu_intelligence, schedule_economics, guest_email, marketing_signals, metrics,
                outcomes):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


def _rid(db, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Opp Co"), owner_email="o@x.test", module_marketing=1, **kw),
                             db_path=db)


def _sql(db, sql, *args):
    c = get_conn(db)
    c.execute(sql, args)
    c.commit()
    c.close()


def _history(db, rid, today, slow_day, slow=3000.0, usual=7000.0, weeks=8, finals=None, sales=None):
    """`weeks` weeks of finished nights before `today`, the slow weekday at
    `slow`. Today is on file too, as an unfinished night (final=0, $40 so
    far) that must never count. `finals` / `sales` override a night's flag
    or takings by ISO date."""
    c = get_conn(db)
    for i in range(0, weeks * 7 + 1):
        d = today - timedelta(days=i)
        name = d.strftime("%A")
        amount = slow + (i % 3) * 10 if name == slow_day else usual + (i % 5) * 10
        final = 1
        if i == 0:
            amount, final = 40.0, 0
        iso = d.isoformat()
        if finals and iso in finals:
            final = finals[iso]
        if sales and iso in sales:
            amount = sales[iso]
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, final) "
                  "VALUES (?,?,?,?,?)", (rid, iso, name, amount, final))
    c.commit()
    c.close()


def _labor(db, rid, slow_day=SLOW, slow=3000.0, usual=7000.0, weeks=8):
    """The host-dated history the feed-level tests read (today left out)."""
    _history(db, rid, TODAY, slow_day, slow=slow, usual=usual, weeks=weeks)


def _last(today, day):
    """The newest night of `day` before `today`."""
    return today - timedelta(days=((today.weekday() - DAYS.index(day)) % 7) or 7)


# ── slow nights (OPP-16, OPP-20) ────────────────────────────────────────────

@pytest.mark.parametrize("ahead", range(1, 8))
@pytest.mark.parametrize("today", WEEK, ids=lambda d: d.strftime("%a"))
def test_a_slow_night_is_one_window_on_every_weekday(db, today, ahead):
    rid = _rid(db)
    target = today + timedelta(days=ahead)
    day = target.strftime("%A")
    _history(db, rid, today, day)
    cards = mo.slow_nights(rid, today, db)
    assert [c["key"] for c in cards] == [rec_ledger.rec_key("slow_day", day)] == [f"slow_day:{day}"]
    c = cards[0]
    g = demand.weekday_gaps(rid, today=today, db_path=db)
    row = next(x for x in g["days"] if x["day"] == day)
    assert g["end"] == today.isoformat()                                   # tonight's unfinished night is out
    assert c["title"] == f"Fill {day}, {mdy(target.isoformat())}" and c["days_away"] == ahead
    assert c["why"] == (f"{day}s here run {abs(row['vs_typical_pct'])}% under a typical day: "
                        f"8 of the last 8 came in under it.")
    assert c["facts"] == [f"${row['median_sales']:,.0f} a typical {day} (8 {day}s)",
                          f"${g['typical_day']:,.0f} a typical day"]
    # The % and the $ are one measurement: the gap IS the typical day minus
    # the night, and the % is that gap over the typical day.
    assert c["stake"] == {"amount": round(g["typical_day"] - row["median_sales"], 2),
                          "label": f"a {day} night under a typical day"}
    assert abs(row["vs_typical_pct"]) == round((1 - row["median_sales"] / g["typical_day"]) * 100)
    assert c["action"] == {"prompt": f"Fill {day} dinner", "channels": ["text", "email", "social"]}
    assert gm.plan_campaign(c["action"]["prompt"])["target_day"] == day
    assert c["evidence"]["kind"] == "nights" and c["evidence"]["n"] == 8


@pytest.mark.parametrize("today", WEEK, ids=lambda d: d.strftime("%a"))
def test_an_unfinished_night_never_counts_and_n_of_m_counts_what_it_says(db, today):
    rid = _rid(db)
    day = (today + timedelta(days=3)).strftime("%A")
    newest = _last(today, day)
    # The newest one is still open in the POS ($10 so far): out, not a $10 night.
    _history(db, rid, today, day, finals={newest.isoformat(): 0}, sales={newest.isoformat(): 10.0})
    c = mo.slow_nights(rid, today, db)[0]
    assert "7 of the last 7 came in under it" in c["why"] and f"(7 {day}s)" in c["facts"][0]
    # Two of eight nights above the typical day: still reliably slow (75%),
    # and the sentence counts the six that came in under it.
    rid2 = _rid(db, name="Two")
    older = [(_last(today, day) - timedelta(weeks=w)).isoformat() for w in (2, 5)]
    _history(db, rid2, today, day, sales={iso: 7600.0 for iso in older})
    c2 = mo.slow_nights(rid2, today, db)[0]
    assert "6 of the last 8 came in under it" in c2["why"]


def test_every_slow_weekday_is_a_candidate_and_the_feed_shows_the_nearest_two(db):
    rid = _rid(db)
    c = get_conn(db)
    slow = {(TODAY + timedelta(days=k)).strftime("%A") for k in (1, 2, 3)}
    for i in range(1, 57):
        d = TODAY - timedelta(days=i)
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales) VALUES (?,?,?,?)",
                  (rid, d.isoformat(), d.strftime("%A"), 3000.0 if d.strftime("%A") in slow else 8000.0))
    c.commit()
    c.close()
    cards = mo.slow_nights(rid, TODAY, db)
    assert [x["days_away"] for x in cards] == [1, 2, 3]                     # uncapped at build
    assert len({f for card in cards for f in card["facts"] if f.endswith("a typical day")}) == 1
    shown = [i for i in mo.feed(rid, db_path=db)["items"] if i["kind"] == "slow_night"]
    assert [i["days_away"] for i in shown] == [1, 2]


def test_no_history_no_slow_card_and_the_source_says_why(db):
    out = mo.slow_nights(_rid(db), TODAY, db)
    assert out == [] and out.state == "no_data" and out.note


# ── holidays (OPP-1, OPP-2) ─────────────────────────────────────────────────

def test_a_measured_holiday_names_last_years_weekday_and_never_promises_plans(db):
    rid = _rid(db)
    # Veterans Day 2025 was a Tuesday: $9,000 against Tuesdays of $6,000 around it.
    for d, s in (("2025-11-11", 9000), ("2025-10-14", 6000), ("2025-10-21", 6000), ("2025-10-28", 6000),
                 ("2025-11-04", 6000), ("2025-11-18", 6000)):
        _sql(db, "INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales) VALUES (?,?,?,?)",
             rid, d, date.fromisoformat(d).strftime("%A"), s)
    cards = mo.holidays(rid, datetime(2026, 11, 1, 12), db, restaurant=get_restaurant(rid, db_path=db))
    assert [c["key"] for c in cards] == ["holiday_promo:2026-11-11"]
    c = cards[0]
    assert c["title"] == "Get ready for Veterans Day, Wednesday 11/11/26"      # this year's night
    assert c["why"] == "Last year's Veterans Day ran 50% above a typical Tuesday here."   # last year's
    assert c["action"]["prompt"] == "Invite guests in for Veterans Day"
    assert c["facts"] == ["Veterans Day 2025: $9,000 against a typical Tuesday of $6,000"]
    assert c["stake"] is None and c["evidence"]["n"] == 1
    # The Labor banner's label reads last year's weekday too.
    h = demand.upcoming_holidays(rid, now=datetime(2026, 11, 1, 12), db_path=db)[0]
    assert h["last_year_weekday"] == "Tuesday" and "a typical Tuesday here" in h["label"]


def test_holiday_cards_say_only_the_holidays_own_name(db):
    """The calendar's hints ("Halloween — great for themed specials",
    "Veterans Day — many restaurants offer free/discounted meals for
    veterans") never reach a title, a why or a draft goal (OPP-1)."""
    rid = _rid(db)
    r = get_restaurant(rid, db_path=db)
    for now in (datetime(2026, 10, 20, 12), datetime(2026, 11, 1, 12), datetime(2026, 12, 15, 12),
                datetime(2027, 3, 25, 12), datetime(2026, 6, 25, 12)):
        for c in mo.holidays(rid, now, db, restaurant=r):
            for text in (c["title"], c["why"], c["action"]["prompt"]):
                assert " — " not in text and "plans" not in text and "discount" not in text
                assert "themed specials" not in text and "brunch days" not in text
    halloween = mo.holidays(rid, datetime(2026, 10, 20, 12), db, restaurant=r)[0]
    assert halloween["title"] == "Plan for Halloween, Saturday 10/31/26" and halloween["evidence"] is None
    assert "no Halloween of your own on file" in halloween["why"]
    assert halloween["action"]["prompt"] == "Invite guests in for Halloween"
    update_restaurant(rid, {"skip_holidays": "halloween"}, db_path=db)
    assert mo.holidays(rid, datetime(2026, 10, 20, 12), db, restaurant=get_restaurant(rid, db_path=db)) == []


def test_the_super_bowls_date_is_an_estimate_and_never_measured(db):
    rid = _rid(db)
    # A big "Super Bowl" night last year on the calendar's guess: still no claim.
    for d, s in (("2025-02-09", 12000), ("2025-01-26", 5000), ("2025-02-02", 5000), ("2025-02-16", 5000)):
        _sql(db, "INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales) VALUES (?,?,?,?)",
             rid, d, date.fromisoformat(d).strftime("%A"), s)
    cards = mo.holidays(rid, datetime(2026, 1, 28, 12), db, restaurant=get_restaurant(rid, db_path=db))
    sb = next(c for c in cards if c["subject"] == "Super Bowl Sunday")
    assert sb["title"] == "Plan for Super Bowl Sunday, expected around 2/8/26"
    assert "isn't fixed" in sb["why"] and sb["evidence"] is None and "ran" not in sb["why"]


# ── category dips (OPP-3, OPP-4) ────────────────────────────────────────────

def _cat(db, rid, name, value, today=TODAY, skip=()):
    """56 nights before `today`; `value(d)` a night's sales, `skip` dates unmeasured."""
    c = get_conn(db)
    end = today - timedelta(days=1)
    for i in range(56):
        d = end - timedelta(days=i)
        if d in skip:
            continue
        c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) VALUES (?,?,?,?,?)",
                  (rid, d.isoformat(), f"sales.cat:{name}", value(d), "final"))
    c.commit()
    c.close()


def _windows(today):
    end = today - timedelta(days=1)
    return end - timedelta(days=27), end          # the last window's first night, and its last


def test_a_category_down_past_its_swing_is_a_card_and_a_steady_one_is_not(db):
    rid = _rid(db)
    start, _end = _windows(TODAY)
    _cat(db, rid, "Wine", lambda d: 70.0 if d >= start else 90.0)
    _cat(db, rid, "Beer", lambda d: 78.0 if d >= start else 80.0)
    cards = mo.category_dips(rid, TODAY, db)
    assert [c["key"] for c in cards] == ["category_dip:Wine"]
    c = cards[0]
    assert c["title"] == "Win back wine sales"
    assert c["why"] == ("Wine is down 22% a night: $70 a night over the last 28 nights vs $90 the 28 before, "
                        "the same weekdays compared.")
    assert c["facts"] == ["28 of 28 nights measured lately, 28 of 28 before", "its weeks usually swing 0%"]
    assert c["stake"] == {"amount": 560.0, "label": "less over 28 nights than the same nights before"}
    assert c["action"]["prompt"] == "Promote our wine"


def test_unmeasured_nights_are_never_zero_and_coverage_must_match(db):
    """OPP-3 reproduced: $100 every night, 8 of the last 28 unmeasured, read
    as "down 29%" when totals were compared."""
    rid = _rid(db)
    start, end = _windows(TODAY)
    _cat(db, rid, "Wine", lambda d: 100.0, skip={end - timedelta(days=i) for i in range(8)})
    _cat(db, rid, "Cider", lambda d: 10.0 if d < start else 2.0)                 # a $280 base
    assert mo.category_dips(rid, TODAY, db) == []


@pytest.mark.parametrize("today", WEEK, ids=lambda d: d.strftime("%a"))
def test_nights_are_compared_weekday_for_weekday(db, today):
    """Fridays take $300, other nights $100, nothing moved. The last window
    is missing its two newest Fridays: summed, or averaged unmatched, that
    read as a dip past the 10% floor. Each night against the same weekday
    four weeks before, it is flat."""
    rid = _rid(db)
    start, end = _windows(today)
    fridays = sorted((d for d in (end - timedelta(days=i) for i in range(28)) if d.weekday() == 4), reverse=True)[:2]
    _cat(db, rid, "Liquor", lambda d: 300.0 if d.weekday() == 4 else 100.0, today=today, skip=set(fridays))
    assert mo.category_dips(rid, today, db) == []


def test_the_real_swing_is_stated_and_is_the_bar(db):
    """The earlier window's weeks run $100 / $130 a night: a 13% swing. An
    11% dip is inside it; a 22% one is past it, and the card says 13%, not
    the 10% floor."""
    rid = _rid(db)
    start, end = _windows(TODAY)
    prior_start = start - timedelta(days=28)
    weekly = lambda d: 100.0 if ((d - prior_start).days // 7) % 2 == 0 else 130.0   # noqa: E731
    _cat(db, rid, "Wine", lambda d: 102.0 if d >= start else weekly(d))
    assert mo.category_dips(rid, TODAY, db) == []
    rid2 = _rid(db, name="Two")
    _cat(db, rid2, "Wine", lambda d: 90.0 if d >= start else weekly(d))
    c = mo.category_dips(rid2, TODAY, db)[0]
    assert "its weeks usually swing 13%" in c["facts"] and "down 22% a night" in c["why"]


def test_unmapped_and_a_remapped_category_say_nothing(db):
    rid = _rid(db)
    start, _end = _windows(TODAY)
    _cat(db, rid, "Unmapped", lambda d: 40.0 if d >= start else 90.0)
    assert mo.category_dips(rid, TODAY, db) == []                            # "Unmapped" is not a category
    _cat(db, rid, "Wine", lambda d: 40.0 if d >= start else 90.0)
    assert [c["key"] for c in mo.category_dips(rid, TODAY, db)] == ["category_dip:Wine"]
    # A department re-mapped inside the windows moves sales between categories.
    _sql(db, "INSERT INTO dsr_category_map (restaurant_id, pos_name, category, updated_at) "
             "VALUES (?,?,?,datetime('now','-3 days'))", rid, "Red Wine", "Wine")
    out = mo.category_dips(rid, TODAY, db)
    assert out == [] and out.state == "no_data" and "re-mapped on" in out.note
    _sql(db, "UPDATE dsr_category_map SET updated_at=datetime('now','-80 days') WHERE restaurant_id=?", rid)
    assert [c["key"] for c in mo.category_dips(rid, TODAY, db)] == ["category_dip:Wine"]


# ── dishes (OPP-5, OPP-7, OPP-8, OPP-11, OPP-18) ────────────────────────────

def _review(db, rid, i, sentiment, dishes):
    _sql(db, "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, sentiment, "
             "entities, processed, review_date, fetched_at) VALUES (?,?,?,?,?,?,?,?,1,?,datetime('now'))",
         rid, "google", f"r{i}", "A", 5, "x", sentiment, json.dumps({"dishes": dishes}),
         (TODAY - timedelta(days=3)).isoformat())


def _menu(db, rid, *names):
    for n in names:
        _sql(db, "INSERT INTO menu_items (restaurant_id, name, is_active) VALUES (?,?,1)", rid, n)


def test_praise_counts_reviews_not_mentions(db):
    """One review that says "carbonara", "the carbonara" and "Carbonara
    pasta" is one review, not three (OPP-7)."""
    rid = _rid(db)
    _menu(db, rid, "Carbonara", "Ribeye")
    _review(db, rid, 1, "positive", ["carbonara", "the carbonara", "Carbonara pasta"])
    _review(db, rid, 2, "positive", ["carbonara"])
    praise = menu_intelligence.dish_praise(rid, db)
    assert [(p["name"], p["positive_reviews"], p["negative_reviews"]) for p in praise] == [("Carbonara", 2, 0)]
    assert mo.dish_praise_cards(rid, db, praise=praise) == []                 # 2 reviews: under the floor
    _review(db, rid, 3, "positive", ["Carbonara"])
    cards = mo.dishes(rid, db)
    assert [c["key"] for c in cards] == ["dish_praise:Carbonara"]
    c = cards[0]
    assert c["why"] == "Named in 3 positive reviews in the last 90 days, and in no negative one."
    assert c["action"]["prompt"] == "Feature our Carbonara" and "favorite" not in json.dumps(c)
    assert c["stake"] is None and c["evidence"]["kind"] == "reviews"


def test_dish_praise_counts_only_unambiguous_mentions(db):
    rid = _rid(db)
    _menu(db, rid, "Carbonara", "Chicken Parm", "Chicken Wings")
    for i, (dish, sent) in enumerate((("carbonara", "positive"), ("the carbonara", "positive"),
                                       ("chicken", "positive"), ("carbonara", "negative"))):
        _review(db, rid, i, sent, [dish])
    out = {it["name"]: (it["positive_reviews"], it["negative_reviews"]) for it in menu_intelligence.dish_praise(rid, db)}
    assert out == {"Carbonara": (2, 1)}                                     # "chicken" names two dishes: neither


def test_the_word_index_matches_exactly_what_the_matcher_matches(db):
    """dish_praise compares each mention only with the items sharing a word
    with it (OPP-18): the same answer as _same_thing over the whole menu."""
    from business_intelligence import _same_thing
    rid = _rid(db)
    names = ["Ribeye Steak", "Steak Frites", "Pie", "Key Lime Pie", "Carbonara", "The Burger", "Veggie Burger",
             "Fish & Chips", "Fish Tacos", "Chips and Salsa", "Crème Brûlée", "Ribeye-Steak Sandwich"]
    _menu(db, rid, *names)
    said = ["the ribeye", "steak", "pie", "lime pie", "carbonara", "burger", "fish", "tacos", "salsa",
            "creme brulee", "Crème Brûlée", "chips", "the fish tacos", "sandwich"]
    mentions = [{"dish": s, "sentiment": "positive", "review_id": i} for i, s in enumerate(said)]
    got = {p["name"]: p["positive_reviews"] for p in menu_intelligence.dish_praise(rid, db, mentions=mentions)}
    want = {}
    for m in mentions:
        hits = [n for n in names if _same_thing(m["dish"], n)]
        if len(hits) == 1:
            want[hits[0]] = want.get(hits[0], 0) + 1
    assert got == want


def test_dish_praise_is_bounded_and_one_build_reads_the_mentions_once(db, monkeypatch):
    rid = _rid(db)
    _menu(db, rid, *[f"House Special {i:04d}" for i in range(3000)], "Carbonara")
    mentions = [{"dish": f"special {i:04d}", "sentiment": "positive", "review_id": i} for i in range(400)]
    t0 = time.monotonic()
    menu_intelligence.dish_praise(rid, db, mentions=mentions)
    assert time.monotonic() - t0 < 1.5
    src = open("menu_intelligence.py", encoding="utf-8").read()
    assert "ORDER BY id DESC LIMIT ?" in src and "DISH_PRAISE_MAX_ITEMS" in src
    calls = []
    real = menu_intelligence._dish_mentions
    monkeypatch.setattr(menu_intelligence, "_dish_mentions", lambda *a, **k: calls.append(1) or real(*a, **k))
    mo.build(rid, db_path=db, now=NOW)
    assert len(calls) == 1


def _scorecard(monkeypatch, rows):
    dishes = [{"name": n, "units_sold": u, "margin": m, "action": a, **extra}
              for n, u, m, a, extra in rows]
    monkeypatch.setattr(menu_intelligence, "dish_scorecard",
                        lambda *a, **k: {"available": True, "has_sales_data": True, "dishes": dishes})


def test_a_high_margin_low_selling_dish_is_promoted_against_the_menu_median(db, monkeypatch):
    rid = _rid(db)
    _scorecard(monkeypatch, (("Short Rib", 22, 14.2, "promote", {}), ("Burger", 60, 6.0, "reprice", {}),
                             ("Salad", 41, 9.1, None, {}), ("Wings", 50, 11.0, None, {})))
    praise = [{"name": "Short Rib", "positive_reviews": 3, "negative_reviews": 0}]
    cards = mo.dish_margins(rid, db, praise=praise)
    assert [c["key"] for c in cards] == ["dish_promote:Short Rib"]
    c = cards[0]
    assert c["why"] == "It earns $14.20 a plate (menu median $10.05) but sold 22 in the last 28 days (median 46)."
    assert c["facts"] == ["Named in 3 positive reviews"] and c["food"] is True


def test_a_dish_with_a_unit_warning_is_no_margin_card_and_no_part_of_the_medians(db, monkeypatch):
    rid = _rid(db)
    _scorecard(monkeypatch, (("Short Rib", 22, 14.2, "promote", {}), ("Burger", 60, 6.0, "reprice", {}),
                             ("Salad", 41, 9.1, None, {}), ("Wings", 50, 11.0, None, {}),
                             ("Lobster", 3, 88.0, "promote", {"unit_warning": "high"})))
    cards = mo.dish_margins(rid, db)
    assert [c["key"] for c in cards] == ["dish_promote:Short Rib"]
    assert "(menu median $10.05)" in cards[0]["why"]                         # Lobster isn't in it


def _dish_world(db, monkeypatch, rid):
    _scorecard(monkeypatch, (("Short Rib", 22, 14.2, "promote", {}), ("Pork Chop", 20, 13.5, "promote", {}),
                             ("Duck", 18, 12.9, "promote", {}), ("Burger", 60, 6.0, "reprice", {}),
                             ("Salad", 41, 9.1, None, {}), ("Wings", 50, 11.0, None, {})))
    monkeypatch.setattr(menu_intelligence, "dish_praise", lambda *a, **k: [
        {"name": "Short Rib", "positive_reviews": 4, "negative_reviews": 0}])


def test_plate_margins_are_for_food_cost_logins_only(db, monkeypatch):
    """OPP-5: the stored feed is per restaurant, so the filter is per viewer
    at read time — and a manager can't answer (and so silence) one either."""
    rid = _rid(db, module_inventory=1)
    _dish_world(db, monkeypatch, rid)
    owner = [i["key"] for i in mo.feed(rid, db_path=db, user=OWNER, show_all=True)["items"]]
    assert "dish_promote:Short Rib" in owner and "dish_praise:Short Rib" not in owner    # one card per dish
    mgr = mo.feed(rid, db_path=db, user=MANAGER, show_all=True)
    keys = [i["key"] for i in mgr["items"]]
    assert not any(k.startswith("dish_promote:") for k in keys) and "dish_praise:Short Rib" in keys
    assert "dish margins and sales" not in [s["label"] for s in mgr["sources"]]
    assert "$14.20" not in json.dumps(mgr)
    assert rec_learning.answerable_episode(MANAGER, rid, "dish_promote:Short Rib", db_path=db) is None
    assert rec_learning.answerable_episode(OWNER, rid, "dish_promote:Short Rib", db_path=db) is not None
    # No Food Cost module: nobody reads margins in Marketing.
    rid2 = _rid(db, name="No FC", module_inventory=0)
    assert not any(i["kind"] == "dish_promote" for i in mo.feed(rid2, db_path=db, user=OWNER, show_all=True)["items"])


def test_answered_cards_go_before_the_caps(db, monkeypatch):
    """OPP-11: passing the top two dishes brings the third up; it used to be
    capped away before the answers were read."""
    rid = _rid(db, module_inventory=1)
    _dish_world(db, monkeypatch, rid)
    first = [i["key"] for i in mo.feed(rid, db_path=db, user=OWNER, show_all=True)["items"]
             if i["key"].startswith("dish_promote:")]
    assert first == ["dish_promote:Short Rib", "dish_promote:Pork Chop"]
    for k in first:
        rec_ledger.record(rid, k, "dismissed", surface="marketing", meta={"kind": "not_for_us"}, db_path=db)
    after = [i["key"] for i in mo.feed(rid, db_path=db, user=OWNER, show_all=True)["items"]]
    assert "dish_promote:Duck" in after
    assert "dish_praise:Short Rib" not in after                            # the same advice, declined


# ── lists and posting (OPP-15, OPP-17) ──────────────────────────────────────

def _contacts(db, rid, n, email=0, invite_yes=0):
    c = get_conn(db)
    for i in range(n):
        phone = f"+1555040{i:04d}"
        c.execute("INSERT INTO guest_contacts (restaurant_id, phone, name, consent, email, email_consent) "
                  "VALUES (?,?,?,1,?,?)", (rid, phone, f"G{i}", f"g{i}@x.test" if i < email else None, 1 if i < email else 0))
        if i < invite_yes:
            c.execute("INSERT INTO sms_optin_invites (restaurant_id, phone, external_ref, response) VALUES (?,?,?,?)",
                      (rid, phone, f"ref{i}", "yes"))
    c.commit()
    c.close()


def test_idle_lists_are_counted_without_review_link_yeses(db):
    rid = _rid(db)
    assert mo.lists(rid, NOW, db).state == "no_data"
    _contacts(db, rid, 30, email=12, invite_yes=6)
    # Rows written the pre-9/28/26 way (a review-link YES stored as consent=1)
    # are moved to review-only consent by the boot backfill; from then on the
    # feed, the Studio and the send read one rule (marketing_text_sql, OPP-6).
    gm.init_guest_marketing(db)
    cards = {c["key"]: c for c in mo.lists(rid, NOW, db)}
    assert set(cards) == {"list_idle:email"}                                # 24 marketing-consented texts: under 25
    assert cards["list_idle:email"]["title"] == "Email the 12 guests on your list"
    assert cards["list_idle:email"]["why"] == "They've never been emailed."
    assert cards["list_idle:email"]["evidence"] is None and cards["list_idle:email"]["action"]["channels"] == ["email"]
    _sql(db, "INSERT INTO guest_newsletters (restaurant_id, subject, body, content_hash, total, created_at) "
             "VALUES (?,?,?,?,?,?)", rid, "S", "B", "h", 12, (NOW - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"))
    assert mo.lists(rid, NOW, db) == []                                     # emailed 10 days ago


def _post(db, rid, ago_days, utc_now, post_id="p1"):
    _sql(db, "INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, created_at) "
             "VALUES (?,?,?,?,?)", rid, "instagram_post", "t", post_id,
         (utc_now - timedelta(days=ago_days)).strftime("%Y-%m-%d %H:%M:%S"))


def test_posting_follows_homes_rule_and_the_one_connected_rule(db):
    rid = _rid(db)
    utc = datetime(2026, 10, 7, 15, 0)
    r = lambda: get_restaurant(rid, db_path=db)   # noqa: E731
    assert mo.posting(rid, NOW, r(), db, utc_now=utc).state == "no_data"
    # A token without the account id can't post (Instagram's publish needs both).
    update_restaurant(rid, {"ig_token": "tok"}, db_path=db)
    assert mo.posting(rid, NOW, r(), db, utc_now=utc) == []
    update_restaurant(rid, {"ig_user_id": "17841"}, db_path=db)
    first = mo.posting(rid, NOW, r(), db, utc_now=utc)
    assert [c["key"] for c in first] == ["first_post"] and first[0]["action"]["channels"] == ["social"]
    # Drafted but never live: Home says nothing, and so does the feed.
    _sql(db, "INSERT INTO marketing_content_log (restaurant_id, content_type, topic) VALUES (?,?,?)",
         rid, "instagram_post", "draft")
    assert mo.posting(rid, NOW, r(), db, utc_now=utc) == []
    _post(db, rid, 10, utc)
    assert mo.posting(rid, NOW, r(), db, utc_now=utc) == []                 # 10 days: not MORE than 10
    rid2 = _rid(db, name="Two")
    update_restaurant(rid2, {"fb_page_token": "t", "fb_page_id": "p"}, db_path=db)
    _post(db, rid2, 10.5, utc)
    cards = mo.posting(rid2, NOW, get_restaurant(rid2, db_path=db), db, utc_now=utc)
    assert cards[0]["key"] == "post_this_week" and cards[0]["why"] == "Nothing has gone live in 10 days."
    # One rule: the Studio's globals come from it, and Home reads it too.
    assert "social_channels.instagram" in SRC and "restaurant.ig_token %}true" not in SRC
    home = open("home_brief.py", encoding="utf-8").read()
    assert "_channels_of(r)" in home and 'posted_age > _POST_IDLE and mkt.get("can_post")' in home
    assert "MAX(COALESCE(posted_at, created_at)) AS t FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL" in home
    import marketing_publish
    assert marketing_publish.channels_of({"ig_token": "a"}) == {"instagram": False, "facebook": False, "google": False}


def test_a_dish_name_with_a_colon_keeps_its_whole_tag():
    assert "dish:steak: ribeye" in rec_ledger.tags_for("dish_praise:Steak: Ribeye", module="marketing")
    assert "dish:short rib" in rec_ledger.tags_for("dish_promote:Short Rib", module="marketing")


# ── recurring answers (OPP-9) ───────────────────────────────────────────────

def _silence_days(db, rid, key):
    c = get_conn(db)
    row = c.execute("SELECT julianday(silenced_until) - julianday(closed_at) FROM rec_instances "
                    "WHERE restaurant_id=? AND key=? ORDER BY created_at DESC LIMIT 1", (rid, key)).fetchone()
    c.close()
    return round(row[0])


def test_done_and_pass_on_a_recurring_card_hold_for_the_occurrence_not_for_years(db):
    rid = _rid(db)
    for key in ("slow_day:Tuesday", "slow_day:Friday", "list_idle:text", "category_dip:Wine", "post_this_week",
                "reprice:Soup"):
        rec_ledger.present(rid, key, "marketing", "marketing", db_path=db)
    done = rec_ledger.SILENCE_DAYS["done"]
    for key in ("slow_day:Tuesday", "list_idle:text", "category_dip:Wine", "post_this_week", "reprice:Soup"):
        rec_ledger.record(rid, key, "completed", surface="marketing", silence_days=done, db_path=db)
    assert _silence_days(db, rid, "slow_day:Tuesday") == 6                  # until next Tuesday's card
    assert _silence_days(db, rid, "list_idle:text") == 30
    assert _silence_days(db, rid, "category_dip:Wine") == 28
    assert _silence_days(db, rid, "post_this_week") == 7
    assert _silence_days(db, rid, "reprice:Soup") == done                  # not recurring: unchanged
    rec_ledger.record(rid, "slow_day:Friday", "dismissed", surface="marketing", meta={"kind": "not_for_us"},
                      db_path=db)
    assert _silence_days(db, rid, "slow_day:Friday") == rec_ledger.RECURRING_DECLINE_DAYS == 90
    # A season's "not for us" is a decline on every surface for all of it
    # (the quiet-night push reads it), not only while 60+ days remain.
    _sql(db, "UPDATE rec_instances SET closed_at=datetime('now','-50 days'), "
             "silenced_until=datetime('now','+40 days') WHERE restaurant_id=? AND key=?", rid, "slow_day:Friday")
    assert "guest_outreach:day:friday" in insight_store.declined_signatures(rid, db_path=db)


def test_old_ten_year_silences_on_recurring_keys_are_brought_back(db):
    rid = _rid(db)
    rec_ledger.present(rid, "slow_day:Monday", "labor", "brief_email", db_path=db)
    rec_ledger.present(rid, "reprice:Soup", "food", "home", db_path=db)
    _sql(db, "UPDATE rec_instances SET status='completed', closed_at=datetime('now','-2 days'), "
             "silenced_until=datetime('now','+3640 days') WHERE restaurant_id=?", rid)
    c = get_conn(db)
    assert rec_ledger.cap_recurring_silences(c) == 1
    c.commit()
    c.close()
    assert _silence_days(db, rid, "slow_day:Monday") == 6 and _silence_days(db, rid, "reprice:Soup") > 3000
    assert "slow_day:Monday" in rec_ledger.silenced_keys(rid, db_path=db)   # still this week's answer


def test_the_answer_says_how_long_a_recurring_card_is_hidden(db):
    rid = _rid(db)
    rec_ledger.present(rid, "list_idle:email", "marketing", "marketing", db_path=db)
    rec_ledger.present(rid, "holiday_promo:2026-11-11", "marketing", "marketing", db_path=db)
    u = {"id": 1, "restaurant_id": rid, "role": "client"}
    with Flask(__name__).test_request_context(json={"key": "list_idle:email", "event": "dismissed",
                                                    "kind": "not_for_us", "module": "marketing",
                                                    "surface": "marketing"}):
        out, st = strategy_routes._do_rec_event(u)
    assert st == 200 and out["message"] == "Noted — hidden for 90 days"
    with Flask(__name__).test_request_context(json={"key": "holiday_promo:2026-11-11", "event": "dismissed",
                                                    "kind": "not_for_us", "module": "marketing",
                                                    "surface": "marketing"}):
        out, st = strategy_routes._do_rec_event(u)
    # A one-off's "not for us" is a year, re-offered after (memory audit
    # 9/29/26, "silences"): never "won't come back".
    assert out["message"] == "Noted — Cavnar AI won’t suggest it again for a year"


# ── the feed: logging, confidence, sources (OPP-10, OPP-14, OPP-15) ─────────

def _rows(db, rid):
    c = get_conn(db)
    rows = {r["key"]: dict(r) for r in c.execute("SELECT key, confidence_pct FROM rec_instances WHERE restaurant_id=?",
                                                 (rid,)).fetchall()}
    c.close()
    return rows


def test_only_cards_on_screen_are_logged_and_with_the_confidence_shown(db):
    rid = _rid(db)
    _labor(db, rid)
    _contacts(db, rid, 30, email=12)
    update_restaurant(rid, {"fb_page_token": "t", "fb_page_id": "p"}, db_path=db)
    d = mo.feed(rid, db_path=db, user=OWNER)
    keys = [i["key"] for i in d["items"]]
    assert len(keys) == 4 and d["visible"] == mo.VISIBLE == 3
    assert keys[0] == f"slow_day:{SLOW}" and "first_post" in keys
    rows = _rows(db, rid)
    assert set(rows) == set(keys[:3])                                       # the fourth is behind "Show more"
    slow = next(i for i in d["items"] if i["key"] == f"slow_day:{SLOW}")
    assert isinstance(slow["confidence"], dict) and rows[slow["key"]]["confidence_pct"] == slow["confidence"]["pct"]
    assert next(i for i in d["items"] if i["key"] == "list_idle:email")["confidence"] is None
    assert d["items"][3]["rec_id"] is None and d["items"][0]["rec_id"]
    mo.feed(rid, db_path=db, user=OWNER, show_all=True)
    assert set(_rows(db, rid)) == set(keys)


def test_the_feed_drops_answered_cards(db):
    rid = _rid(db)
    _labor(db, rid)
    assert [i["key"] for i in mo.feed(rid, db_path=db)["items"]] == [f"slow_day:{SLOW}"]
    rec_ledger.record(rid, f"slow_day:{SLOW}", "dismissed", surface="marketing", meta={"kind": "not_for_us"},
                      db_path=db)
    assert mo.feed(rid, db_path=db)["items"] == []


def test_every_source_says_checked_no_data_or_failed(db, monkeypatch):
    rid = _rid(db)
    _labor(db, rid)
    captured, puts = [], []
    import ops
    monkeypatch.setattr(ops, "capture", lambda e, **k: captured.append(k.get("context")))
    monkeypatch.setattr(mo, "category_dips", lambda *a, **k: 1 / 0)
    real_put = insight_store.put
    monkeypatch.setattr(insight_store, "put", lambda *a, **k: puts.append(1) or real_put(*a, **k))
    d = mo.feed(rid, db_path=db)
    state = {s["key"]: s["state"] for s in d["sources"]}
    assert state["categories"] == "failed" and state["slow_nights"] == "checked" and state["holidays"] == "checked"
    assert state["lists"] == "no_data" and state["posting"] == "no_data"
    assert [i["key"] for i in d["items"]] == [f"slow_day:{SLOW}"]            # one failure never empties it
    assert any("source=categories" in (c or "") for c in captured)          # to ops, not only a print
    assert puts == []                                                       # and not stored for the day
    assert "category sales" not in d["checked"] and "slow nights ahead" in d["checked"]


def test_the_feed_is_stored_and_rebuilt_when_any_input_moves(db, monkeypatch):
    rid = _rid(db)
    calls = []
    real = mo.build
    monkeypatch.setattr(mo, "build", lambda *a, **k: calls.append(1) or real(*a, **k))

    def rebuilt():
        n = len(calls)
        mo.cached_build(rid, db_path=db, now=NOW)
        return len(calls) > n

    assert rebuilt() and not rebuilt()
    _contacts(db, rid, 1, email=1)
    assert rebuilt()
    moves = (
        ("UPDATE restaurants SET ig_token='t', ig_user_id='u' WHERE id=?", (rid,)),          # an account connected
        ("UPDATE guest_contacts SET unsubscribed=1 WHERE restaurant_id=?", (rid,)),           # STOP
        ("UPDATE guest_contacts SET email_consent=0 WHERE restaurant_id=?", (rid,)),          # email consent
        ("INSERT INTO sms_optin_invites (restaurant_id, phone, external_ref, response, responded_at) "
         "VALUES (?,'+15550000001','x','yes',datetime('now'))", (rid,)),                     # an invite YES
        ("INSERT INTO email_suppressions (email, reason) VALUES ('b@x.test','bounce')", ()),  # a suppression
        ("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, final) VALUES (?,?,?,?,1)",
         (rid, (TODAY - timedelta(days=1)).isoformat(), "x", 500.0)),                        # a night's sales
        ("UPDATE labor_daily_history SET final=0 WHERE restaurant_id=?", (rid,)),              # ...reopened
        ("INSERT INTO menu_items (restaurant_id, name, is_active, sell_price) VALUES (?,'Soup',1,9)", (rid,)),
        ("INSERT INTO menu_item_sales (restaurant_id, menu_item_id, business_date, qty_sold) "
         "VALUES (?,1,?,3)", (rid, (TODAY - timedelta(days=2)).isoformat())),                 # item sales
        ("INSERT INTO ingredients (restaurant_id, name, unit_cost) VALUES (?,'Leek',2)", (rid,)),
        ("UPDATE ingredients SET unit_cost=3 WHERE restaurant_id=?", (rid,)),                  # a cost change
        ("INSERT INTO dsr_category_map (restaurant_id, pos_name, category) VALUES (?,'Reds','Wine')", (rid,)),
        ("UPDATE restaurants SET skip_holidays='halloween' WHERE id=?", (rid,)),
    )
    for sql, args in moves:
        _sql(db, sql, *args)
        assert rebuilt(), sql
    # A review is read when the analyser has processed it, and only when it
    # names a dish.
    _sql(db, "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, processed, "
             "review_date, fetched_at) VALUES (?,'google','n1','A',5,'x',0,date('now'),datetime('now'))", rid)
    assert not rebuilt()
    _sql(db, "UPDATE reviews SET processed=1, sentiment='positive', entities=? WHERE external_id='n1'",
         json.dumps({"dishes": []}))
    assert not rebuilt()
    _sql(db, "UPDATE reviews SET entities=? WHERE external_id='n1'", json.dumps({"dishes": ["soup"]}))
    assert rebuilt()


def test_no_model_call_on_build():
    src = open("marketing_opportunities.py", encoding="utf-8").read()
    for banned in ("create_with_retry", "get_client", "requests.", "urlopen"):
        assert banned not in src, banned


# ── a send that began on a card (OPP-10) ────────────────────────────────────

def test_a_studio_send_on_any_channel_marks_its_card_implemented(db):
    rid = _rid(db)
    rec_ledger.present(rid, "holiday_promo:2026-11-11", "marketing", "marketing", db_path=db)
    rec_ledger.present(rid, "dish_praise:Carbonara", "marketing", "marketing", db_path=db)
    assert mo.implemented_by_send(rid, "holiday_promo:2026-11-11", "email", user_id=1, sent=40, db_path=db)
    client_api._track_campaign_outcome(rid, {"rec_key": "dish_praise:Carbonara"}, {"ok": True, "sent": 12}, 1)
    c = get_conn(db)
    st = {r["key"]: r["status"] for r in c.execute("SELECT key, status FROM rec_instances WHERE restaurant_id=?",
                                                    (rid,)).fetchall()}
    c.close()
    assert st == {"holiday_promo:2026-11-11": "implemented", "dish_praise:Carbonara": "implemented"}
    assert not mo.implemented_by_send(rid, "reprice:Soup", "text", db_path=db)            # not a feed card
    assert not mo.implemented_by_send(rid, "list_idle:text", "text", db_path=db)          # never shown


def test_every_send_route_takes_the_cards_key_and_the_studio_passes_it():
    # A text campaign is queued and sent later (slice C's durable queue), so
    # its card key is stored on the campaign (guest_campaigns.rec_key) and
    # marked by the tracker once texts actually went.
    gm_src = open("guest_marketing.py", encoding="utf-8").read()
    assert 'implemented_by_send(restaurant_id, rec_key, "text"' in gm_src and "rec_key=rec_key" in gm_src
    assert 'rec_key=data.get("rec_key")' in open("mobile_api.py", encoding="utf-8").read()
    for path, needle in (("mobile_api.py", 'implemented_by_send(rid, data.get("rec_key"), "email"'),
                         ("client_api.py", 'implemented_by_send(rid, data.get("rec_key"), "social"'),
                         ("social_routes.py", 'data.get("rec_key"), "social"'),
                         ("mobile_api.py", 'data.get("rec_key"), "social"')):
        assert needle in open(path, encoding="utf-8").read(), (path, needle)
    assert open("social_routes.py", encoding="utf-8").read().count('data.get("rec_key"), "social"') == 2
    assert open("mobile_api.py", encoding="utf-8").read().count('data.get("rec_key"), "social"') == 2
    snap = _between("function cpSnapshot(ready) {", "function cpMarkSent(")
    assert "mktOppRecKey()" in snap and snap.count("rec_key: rk") == 2 and "b.rec_key = rk;" in snap
    assert "function mktOppRecKey() { return (window._cp && _cp.recKey && _cp.prompt === _cp.recFor)" in SRC


# ── the route ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(db, monkeypatch):
    init_auth(db_path=db)
    real = models.get_conn
    for mod in (auth, auth_routes, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db))
    auth_routes._login_attempts.clear()
    app = Flask(__name__)
    app.register_blueprint(mobile_bp)
    return app.test_client()


def _login(client, db, module=1):
    rid = create_restaurant(Restaurant(name="Route Co", owner_email="r@x.test", module_marketing=module), db_path=db)
    create_user(rid, "owner", "owner@x.test", "correct-horse", db_path=db)
    tok = client.post("/mobile/api/login", json={"username": "owner", "password": "correct-horse"}).get_json()["token"]
    return rid, {"Authorization": f"Bearer {tok}"}


def test_the_route_serves_the_feed_behind_the_module(client, db):
    rid, h = _login(client, db)
    _labor(db, rid)
    d = client.get("/mobile/api/marketing/opportunities", headers=h).get_json()
    assert d["ok"] and d["items"][0]["key"] == f"slow_day:{SLOW}" and d["sources"] and d["visible"] == 3
    assert client.get("/mobile/api/marketing/opportunities?show=all", headers=h).get_json()["ok"]
    src = open("client_api.py", encoding="utf-8").read()
    i = src.index('@client_bp.route("/api/marketing/opportunities")')
    assert '_m("mobile_marketing_opportunities")' in src[i:i + 300]
    assert 'show_all=request.args.get("show") == "all"' in open("mobile_api.py", encoding="utf-8").read()


def test_the_route_needs_marketing(client, db):
    _, h = _login(client, db, module=0)
    assert client.get("/mobile/api/marketing/opportunities", headers=h).status_code == 403


# ── one answer to "what should I do this week" (AUX-3, AUX-7, #43) ──────────

def test_the_marketing_brief_is_written_from_the_feeds_cards(db, monkeypatch):
    import ai_utils
    rid = _rid(db)
    _labor(db, rid)
    seen = {}

    class _Msg:
        stop_reason = "end_turn"
        content = []

    monkeypatch.setattr(ai_utils, "create_with_retry", lambda client, **k: seen.update(k) or _Msg())
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(ai_utils, "extract_text",
                        lambda m: f"Hi, fill {SLOW} this week.\n\n1. Post a {SLOW} dinner photo.\n2. Text the club.")
    monkeypatch.setattr(client_api, "_insight_cache", {})
    client_api._do_mkt_insight(rid, raw=True)
    prompt = seen["messages"][0]["content"]
    card = mo.context_lines(rid)[0]
    assert card["key"] == f"slow_day:{SLOW}" and f"- {card['line']}" in prompt
    assert "Line 1 names the first of these as this week's biggest opportunity" in prompt
    src = open("client_api.py", encoding="utf-8").read()
    assert '{"missing_inputs": _missing_m}' in src and '_missing_m.remove("sales")' in src


def test_the_brief_never_carries_a_plate_margin(db, monkeypatch):
    rid = _rid(db, module_inventory=1)
    _dish_world(db, monkeypatch, rid)
    lines = mo.context_lines(rid, db_path=db)
    assert lines and not any(l["kind"] == "dish_promote" or "$14.20" in l["line"] for l in lines)


def test_the_calendar_knows_the_weeks_dates_its_channels_lists_and_the_feed(db, monkeypatch):
    import marketing
    rid = _rid(db)
    _labor(db, rid)
    seen = []

    class _Msg:
        stop_reason = "end_turn"

    week = json.dumps([{"day": d, "platform": "Instagram & FB", "angle": f"A {d} post", "type": "instagram_post"}
                       for d in ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")])
    monkeypatch.setattr(marketing, "create_with_retry", lambda client, **k: seen.append(k) or _Msg())
    monkeypatch.setattr(marketing, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(marketing, "extract_text", lambda m: week)
    monkeypatch.setattr(marketing_signals, "weather_signal", lambda rid_: {})
    marketing.get_content_calendar_ideas(rid, force=True)
    prompt = seen[0]["messages"][0]["content"]
    assert "THIS WEEK'S DATES: Sunday " in prompt and "(today)" in prompt
    assert "Accounts connected to post to: none yet" in prompt
    assert "Guests who can be texted: 0. Guests on the email list: 0." in prompt
    assert "Nobody can be texted yet: no SMS or loyalty_nudge ideas" in prompt
    assert "Include at least one SMS/loyalty_nudge idea per week" not in prompt
    assert f"Fill {SLOW}" in prompt                                         # the feed's card


def test_weather_notes_name_a_patio_or_takeout_only_when_the_restaurant_has_one(db, monkeypatch):
    import weather
    rid = _rid(db)
    d1, d2 = (TODAY + timedelta(days=1)).isoformat(), (TODAY + timedelta(days=2)).isoformat()
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda r, days: [
        {"date": d1, "high_f": 80, "short_forecast": "Sunny"},
        {"date": d2, "high_f": 55, "short_forecast": "Rain likely"}])
    notes = marketing_signals.weather_signal(rid)["notes"]
    assert not any("patio" in n or "takeout" in n for n in notes) and any("Rain likely" in n for n in notes)
    update_restaurant(rid, {"patio_roles_json": '["Server"]', "delivery_pct": 20}, db_path=db)
    notes = " ".join(marketing_signals.weather_signal(rid)["notes"])
    assert "80°F — patio weather" in notes and "Rain likely — delivery and takeout push" in notes


def _brief_world(monkeypatch, db):
    import morning_brief, issues, goals, metrics, outcomes
    real = models.get_conn
    for mod in (morning_brief, demand, issues, outcomes, goals, metrics):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db), raising=False)


@pytest.mark.parametrize("ahead", range(1, 8))
def test_the_morning_brief_says_the_feeds_slow_night_under_its_key_for_marketing_only_too(db, monkeypatch, ahead):
    import morning_brief
    _brief_world(monkeypatch, db)
    monday = WEEK[0]
    day = (monday + timedelta(days=ahead)).strftime("%A")
    rid = _rid(db)                                                          # Marketing only: no Labor
    _history(db, rid, monday, day)
    r = models.get_restaurant(rid, db_path=db)
    lines = {l["key"]: l for l in morning_brief.build(rid, restaurant=r, db_path=db, today=monday)["lines"]}
    line = lines["slow_day"]
    card = mo.slow_nights(rid, monday, db)[0]
    assert line["rec"] == card["key"] == f"slow_day:{day}"
    g = next(x for x in demand.weekday_gaps(rid, today=monday, db_path=db)["days"] if x["day"] == day)
    assert f"{day}s run about {abs(g['vs_typical_pct'])}% under a typical day." == line["text"]
    assert line["action"]["nav"] == f"marketing/opportunities?card=slow_day%3A{day}"
    assert morning_brief._LINE_MODULE["slow_day"] == "marketing"


def test_the_digest_says_the_same_slow_night_measured_and_nothing_more(db, monkeypatch):
    import reporter
    rid = _rid(db)
    _labor(db, rid)
    staged = []
    monkeypatch.setattr(reporter, "_digest_impressions", lambda r, items: staged.extend(items))
    html = "".join(reporter._follow_through_sections(rid))
    assert "Your quietest day" in html and f"{SLOW}s run about" in html and "came in under it" in html
    assert "cheapest thing" not in html and "tracks what it does" not in html
    assert {"key": f"slow_day:{SLOW}", "module": "marketing", "title": f"Fill {SLOW}s"} in staged


def test_the_campaigns_cause_lives_in_performance_not_the_social_brief():
    brief = _between('<section class="lb2-ai mkt-brief"', "</section>")
    assert 'id="guest-diag"' not in brief
    perf = _between('<section class="cp-sec" aria-labelledby="cp-perf-h">', "</section>")
    assert '<div class="cp-perf-cause"><div class="diag" id="guest-diag" hidden></div></div>' in perf
    assert "q.classList.contains('cp-perf-cause')" in SRC
    assert "fetch('/api/marketing/diagnosis'" in SRC                        # the data route stays


# ── the drafters never tell guests the owner's aim ─────────────────────────

def test_every_drafter_forbids_announcing_a_slow_night():
    assert "never say or hint that a night is slow" in open("guest_marketing.py", encoding="utf-8").read()
    assert "never say or hint that a night is slow" in open("guest_email.py", encoding="utf-8").read()
    assert "Never say or hint that a night is slow" in open("marketing.py", encoding="utf-8").read()
    assert "Nothing is new, back, better or changed unless the owner's words say so." in \
        open("guest_marketing.py", encoding="utf-8").read()


def test_the_new_kinds_have_topics_and_dish_kinds_tag_the_dish():
    for k in ("holiday_promo", "category_dip", "dish_promote", "dish_praise", "list_idle"):
        assert k in rec_ledger.KIND_TOPIC, k
    assert "dish_promote" in rec_learning.FOOD_KINDS


# ── the page (OPP-12, OPP-14, OPP-19) ───────────────────────────────────────

def _between(a, b):
    i = SRC.index(a)
    return SRC[i:SRC.index(b, i)]


def test_the_feed_leads_marketing_under_the_header():
    # The sub-tabs moved to the top of the page in the Studio's step bar
    # (owner, 10/2/26); the feed leads the Content tab only - it showed above
    # Campaigns, Scheduled and Analytics too (owner, 10/5/26).
    panel = _between('id="panel-marketing"', '<div id="mkt-tab-content" role="tabpanel"')
    assert 'id="mkt-opps"' not in panel, "not above the tabs any more"
    content = _between('<div id="mkt-tab-content" role="tabpanel"', 'id="mkt-tab-campaigns"')
    assert '<section class="mkt-opps" id="mkt-opps" data-nav="marketing/opportunities"' in content
    assert panel.index('id="mkt-tab-content-btn"') < panel.index('class="hb-top"')
    focus = _between("function mktOppFocus() {", "// \"Draft it\"")
    assert "switchMktTab('content')" in focus, "a link to a card opens the Content tab first"
    assert '<header class="ss-top mkt-top">' in panel and '<span class="ss-name">Marketing Studio</span>' in panel
    assert '<nav class="ss-steps mkt-steps" role="tablist" aria-label="Marketing sections">' in panel


def test_cards_reuse_home_anatomy_one_primary_answer_controls_and_a_named_draft_button():
    card = _between("function mktOppCard(o, i) {", "function mktOppsSourceWords()")
    assert "hb-card hb-rec hb-rise mkt-opp" in card
    assert "recControlsHtml(o.key, 'marketing', 'marketing', {noTrack: 1})" in card
    assert "(i === 0 ? 'cbtn-primary' : 'cbtn-secondary')" in card
    assert "cavConfLine(o.confidence" in card and "o.stake.label" in card
    assert "aria-label=\"Draft it: ' + esc(o.title || '') + '\"" in card


def test_the_feed_loads_on_the_pulse_and_a_failed_reload_leaves_no_stale_show_more():
    load = _between("function loadMktOpps(quiet) {", "function mktOppWhen(o) {")
    assert '<div class="dr-pulse wide mkt-opps-load" role="status" aria-label="Loading opportunities"><i></i></div>' in load
    assert "if (seq !== _mktOppSt.seq) return;" in load
    fail = _between("function mktOppsFailed(d) {", "function mktOppWhen(o) {")
    assert "foot.innerHTML = ''" in fail and "loadFailed(list, d)" in fail


def test_show_more_logs_the_rest_and_the_list_stays_open_after_an_answer():
    paint = _between("function mktPaintOpps() {", "window.mktOppsMore = function(btn, after) {")
    assert "folded = extra > 0 && !_mktOppSt.open" in paint
    more = _between("window.mktOppsMore = function(btn, after) {", "recOnAnswered(function(key) {")
    assert "fetch('/api/marketing/opportunities?show=all'" in more and "_mktOppSt.open = true;" in more
    answered = _between("recOnAnswered(function(key) {\n  var had = false;", "function mktOppFlash(key) {")
    assert "mktPaintOpps();" in answered and "!next.rec_id) loadMktOpps(true)" in answered


def test_draft_it_is_one_at_a_time_asks_before_replacing_and_reaches_only_real_channels():
    click = _between("document.addEventListener('click', function(e) {\n  var t = e.target && e.target.closest ? e.target.closest('[data-opp-draft]')",
                     "// ── Campaign Studio (owner, 9/28/26)")
    assert "if (_mktOppSt.drafting || mktOppDrafting()) return;" in click
    assert "'Replace your draft? Tap again'" in click and "mktOppHasDraft(o)" in click
    assert "if (!_mktOppSt.opened[key]) {" in click and "event: 'opened'" in click   # once per card
    assert "cbtnBusy(t, 'Drafting')" in click
    chans = _between("function mktOppChans(o) {", "function mktOppNoChannel(o) {")
    assert "+ov.subscribers > 0" in chans and "+ov.email_subscribers > 0" in chans and "cpSocialPlats().length > 0" in chans
    draft = _between("window.mktOppDraft = function(o, done) {", "document.addEventListener('click', function(e) {\n  var t = e.target && e.target.closest ? e.target.closest('[data-opp-draft]')")
    # A card with nowhere to go says so and never reaches cpCreate (which
    # turns text on when no channel is): a posting card never drafts a text.
    assert draft.index("if (!ch.text && !ch.email && !ch.social)") < draft.index("cpCreate();")
    assert "switchMktTab('campaigns')" in draft and "_cp.chanSet = true" in draft and "_cp.recKey = o.key" in draft
    assert "loadMktOpps();\n      loadGuestOverview();" in SRC


def test_an_empty_feed_says_what_each_read_found():
    empty = _between("function mktOppsEmpty() {", "function mktPaintOpps() {")
    assert "'Nothing stands out right now.'" in empty and "' Cavnar AI checked '" in empty
    assert "' Nothing to read yet for '" in empty and "Couldn’t check" in empty
    assert "dish margins and sales" in open("marketing_opportunities.py", encoding="utf-8").read()


def test_a_link_to_one_card_brings_it_into_view():
    focus = _between("function mktOppFocus() {", "// \"Draft it\": the Campaign Studio")
    assert "p.query.card" in focus and "mktOppsMore(null, function() { mktOppFlash(key); })" in focus


def test_number_wrapping_never_splits_an_escaped_apostrophe():
    """The AI monitor read "Review checks haven&#39;t run yet" on Erik's live
    account (owner, 9/28/26): nums() wrapped the 39 inside &#39;. Both
    escape-then-wrap helpers now skip entities, as the shared num() does."""
    i = SRC.index("function nums(x) {")
    assert "split(/(&#?[a-z0-9]+;)/i)" in SRC[i:i + 300]
    j = SRC.index("'how accurate '+e(what)+' are here: '")
    assert "split(/(&#?[a-z0-9]+;)/i)" in SRC[j:j + 200]
