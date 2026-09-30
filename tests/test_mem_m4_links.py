"""links: cross-module links were found fresh on every call and forgotten,
and only one of five kinds reached a decision (memory audit 9/29/26, M4).

Links are kept (link_memory, bi_links) — first and last found, weeks
running, resolved by the owner's answer or by no longer being found — so a
recurring link escalates and a resolved one is remembered. Each kind has a
consumer: the food links reach the food diagnosis, a dish guests complain
about is guarded from a one-tap reprice and kept off marketing's
promotions, marketing reads its own links. The DSR and marketing x labor
kinds are new."""
from datetime import date, timedelta

import pytest

import business_intelligence as bi
import link_memory as lm
import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    import menu_intelligence
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    for m in (bi, menu_intelligence):
        monkeypatch.setattr(m, "get_conn", redirect, raising=False)
    yield


MON = date(2026, 9, 7)          # a Monday: ISO week 37


def _rid(name="Link Co", **flags):
    r = Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test")
    for k, v in flags.items():
        setattr(r, k, v)
    return create_restaurant(r)


def _exec(sql, args=()):
    conn = models.get_conn()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _link(kind="reviews_x_food_cost", day="Friday", **kw):
    return dict({"kind": kind, "day": day, "subject": bi._link_subject("service", day),
                 "modules": ["reviews", "food_cost"], "headline": f"{day} carries the service complaints and 40% of waste",
                 "category": "service", "mentions": 6, "confirm_by": f"Walk {day} prep."}, **kw)


# ── kept, not rediscovered ───────────────────────────────────────────────────

def test_a_link_is_remembered_and_escalates_when_it_recurs():
    rid = _rid()
    first = lm.observe(rid, [_link()], today=MON)[0]["memory"]
    assert first["first_seen"] == MON.isoformat() and first["weeks_running"] == 1 and not first["recurring"]
    assert first["label"] == "First found 9/7/26"
    same_day = lm.observe(rid, [_link()], today=MON)[0]["memory"]
    assert same_day["times_seen"] == 1, "one write per link per day"
    lm.observe(rid, [_link()], today=MON + timedelta(days=8))
    third = lm.observe(rid, [_link()], today=MON + timedelta(days=15))[0]["memory"]
    assert third["weeks_running"] == 3 and third["recurring"] and third["times_seen"] == 3
    assert third["label"] == "Found 3 weeks running, since 9/7/26"
    # A week with no sighting breaks the run.
    later = lm.observe(rid, [_link()], today=MON + timedelta(days=36))[0]["memory"]
    assert later["weeks_running"] == 1 and not later["recurring"]


def test_the_recurring_link_is_the_one_thing_and_says_why():
    rid = _rid()
    fresh = _link(day="Monday", subject="service:monday")
    old = _link()
    for d in (0, 7, 14):
        lm.observe(rid, [old], today=MON + timedelta(days=d))
    links = lm.observe(rid, [fresh, old], today=MON + timedelta(days=14))
    cands = bi.one_thing_candidates(rid, {}, links)
    link_c = [c for c in cands if c["key"].startswith("link:")]
    assert len(link_c) == 1 and link_c[0]["key"] == bi.link_key(old), "the one that has stood longest"
    c = link_c[0]
    assert c["recurring"] and "found 3 weeks running, since 9/7/26" in c["why"]
    assert c["score"] == bi.URGENCY_WEIGHT["important"] * bi.UNPRICED_FLOOR * bi.RECURRING_LINK_WEIGHT
    assert c["urgency"] == "important", "escalated, never critical"


def test_an_answered_link_is_remembered_and_reopens_only_if_it_stays():
    import rec_ledger
    rid = _rid()
    link = _link()
    lm.observe(rid, [link], today=MON)
    rec_ledger.record(rid, bi.link_key(link), "completed", surface="home")
    done_on = date.today()
    assert lm.settle(rid, today=done_on)["answered"] == 1
    assert lm.active(rid, today=done_on) == [], "a resolved link is not acted on"
    soon = lm.observe(rid, [link], today=done_on + timedelta(days=3))[0]["memory"]
    assert soon["resolved"] and not soon["came_back"], "the windows have not rolled yet"
    back = lm.observe(rid, [link], today=done_on + timedelta(days=lm.RECUR_AFTER_DAYS))[0]["memory"]
    assert back["came_back"]["after"] == "done" and back["recurring"]
    from time_utils import mdy
    assert back["label"] == f"Still found after you marked it done on {mdy(done_on)}"


def test_a_declined_link_is_kept_and_never_resurfaced():
    import rec_ledger
    rid = _rid()
    link = _link()
    lm.observe(rid, [link], today=MON)
    rec_ledger.record(rid, bi.link_key(link), "dismissed", surface="home", meta={"kind": "not_for_us"})
    lm.settle(rid, today=date.today())
    again = lm.observe(rid, [link], today=date.today() + timedelta(days=200))[0]["memory"]
    assert again["declined"] and again["resolved"] and not again["came_back"]
    assert lm.active(rid, today=date.today() + timedelta(days=200)) == []


def test_a_link_a_later_read_no_longer_finds_is_gone_and_can_come_back():
    rid = _rid()
    link = _link()
    lm.observe(rid, [link], today=MON)
    # A manager's read without Food Cost did not look for it: no miss.
    lm.observe(rid, [], consulted={"reviews", "labor"}, today=MON + timedelta(days=20))
    assert lm.settle(rid, today=MON + timedelta(days=21))["gone"] == 0
    # A read that consulted both modules, 20 days on, did not find it.
    lm.observe(rid, [], consulted={"reviews", "food_cost"}, today=MON + timedelta(days=20))
    assert lm.settle(rid, today=MON + timedelta(days=21))["gone"] == 1
    back = lm.observe(rid, [link], today=MON + timedelta(days=40))[0]["memory"]
    assert back["came_back"]["after"] == "gone" and back["weeks_running"] == 1
    assert back["label"] == "Back on 10/17/26 after it went away"


def test_correlations_keep_what_they_find():
    rid = _rid()
    data = {"reviews": {"clusters": [{"category": "service", "mentions": 6, "window_days": 90,
                                      "weekday": {"value": "Friday", "share": 0.6}}]},
            "food_cost": {"weekday_waste": {"worst_day": {"weekday": "Friday", "share": 0.4, "events": 12}}},
            "labor": {}}
    links = bi.correlations(rid, data=data)
    assert [l["kind"] for l in links] == ["reviews_x_food_cost"]
    assert links[0]["memory"]["times_seen"] == 1
    assert lm.history(rid)[0]["link_key"] == bi.link_key(links[0])


# ── the new kinds ────────────────────────────────────────────────────────────

def _cluster(day="Friday"):
    return {"category": "service", "mentions": 6, "window_days": 90, "weekday": {"value": day, "share": 0.6},
            "first_seen": (date.today() - timedelta(days=60)).isoformat(), "last_seen": date.today().isoformat()}


def _dsr(by):
    return {"window_days": 56, "start": (date.today() - timedelta(days=55)).isoformat(),
            "end": date.today().isoformat(), "nights": sum(v["nights"] for v in by.values()),
            "by_weekday": by}


def test_complaints_on_the_no_show_weekday_are_a_dsr_link():
    rid = _rid()
    by = {"Friday": {"nights": 6, "no_show_nights": 3}, "Tuesday": {"nights": 6, "no_show_nights": 1},
          "Wednesday": {"nights": 6, "no_show_nights": 0}}
    links = bi.correlations(rid, data={"reviews": {"clusters": [_cluster()]}, "dsr": _dsr(by)})
    link = next(l for l in links if l["kind"] == "dsr_x_reviews")
    assert link["modules"] == ["reviews", "dsr"] and link["day"] == "Friday"
    assert "daily report logged a no-show on 3 of the last 6 Fridays" in link["headline"]
    assert link["act"]["nav"].startswith("labor/schedule")
    # Floors: one no-show night is one night; a no-show weekday no worse than
    # the rest is not a concentration.
    one = {"Friday": {"nights": 6, "no_show_nights": 1}, "Tuesday": {"nights": 6, "no_show_nights": 0}}
    flat = {"Friday": {"nights": 6, "no_show_nights": 2}, "Tuesday": {"nights": 6, "no_show_nights": 2}}
    for b in (one, flat):
        assert not [l for l in bi.correlations(rid, data={"reviews": {"clusters": [_cluster()]}, "dsr": _dsr(b)})
                    if l["kind"] == "dsr_x_reviews"]


def test_the_daily_reports_nights_are_read_from_dsr_metrics(db_path):
    rid = _rid()
    d = date.today() - timedelta(days=1)
    while d.strftime("%A") != "Friday":
        d -= timedelta(days=1)
    for i, v in enumerate((2, 1, 0)):
        day = (d - timedelta(days=7 * i)).isoformat()
        _exec("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) VALUES (?,?,?,?,?)",
              (rid, day, "labor.no_shows", v, "ready"))
    _exec("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) VALUES (?,?,?,?,?)",
          (rid, (d - timedelta(days=1)).isoformat(), "labor.no_shows", None, "ready"))
    got = bi._dsr_nights(rid, db_path)
    assert got["by_weekday"] == {"Friday": {"nights": 3, "no_show_nights": 2}}, "an unmeasured night is absent"


def _labor():
    return {"is_live": True, "period_days": 28, "date_range": {"days": 28},
            "dow_summary": {"Monday": 28, "Tuesday": 36, "Wednesday": 29, "Thursday": 28, "Friday": 27,
                            "Saturday": 26, "Sunday": 28}}


def test_a_fill_campaign_on_the_heaviest_labor_day_is_a_marketing_x_labor_link():
    rid = _rid()
    mk = {"posts_published": 0, "fill_campaigns": [{"day": "Tuesday", "sent": 412, "total": 412, "queued": False,
                                                     "on": "2026-09-20"}]}
    links = bi.correlations(rid, data={"labor": _labor(), "marketing": mk})
    link = next(l for l in links if l["kind"] == "marketing_x_labor")
    assert link["headline"].startswith("A text to fill Tuesday went to 412 guests on 9/20/26, and Tuesday runs 7.1 "
                                       "points heavier on labor")
    assert "Hold any cut to Tuesday" in link["confirm_by"] and link["modules"] == ["marketing", "labor"]
    lean = dict(mk, fill_campaigns=[dict(mk["fill_campaigns"][0], day="Saturday")])
    assert not [l for l in bi.correlations(rid, data={"labor": _labor(), "marketing": lean})
                if l["kind"] == "marketing_x_labor"]
    sample = dict(_labor(), is_live=False)
    assert not [l for l in bi.correlations(rid, data={"labor": sample, "marketing": mk})
                if l["kind"] == "marketing_x_labor"], "sample shifts are not this restaurant's labor"


def test_the_fill_campaigns_come_from_the_campaign_record(db_path):
    import guest_marketing
    guest_marketing.init_guest_marketing(db_path)
    rid = _rid()
    for day, sent, status in (("tuesday", 412, "done"), ("Friday", 0, "done"), ("Monday", 0, "waiting"),
                              (None, 90, "done")):
        _exec("INSERT INTO guest_campaigns (restaurant_id, message, sent_count, total, status, target_day) "
              "VALUES (?,?,?,?,?,?)", (rid, "Come in", sent, 400, status, day))
    got = bi._fill_campaigns(rid, (date.today() - timedelta(days=30)).isoformat(), db_path)
    assert [(c["day"], c["queued"]) for c in got] == [("Monday", True), ("Tuesday", False)]


def test_the_snapshot_says_it_reads_the_daily_report():
    import data_freshness
    assert "dsr" in data_freshness.TOOL_SOURCES["read_business_snapshot"]


# ── the consumers ────────────────────────────────────────────────────────────

def _menu_link(dish="the brisket"):
    return {"kind": "reviews_x_menu", "dish": dish, "subject": bi._link_subject("food_quality", dish),
            "modules": ["reviews", "food_cost"], "category": "food_quality", "mentions": 5,
            "headline": f"Guests name {dish} in 5 food quality complaints, and it is also a cost driver"}


def test_a_dish_guests_complain_about_is_guarded_from_a_one_tap_reprice(db_path, monkeypatch):
    import menu_intelligence as mi
    from tests.test_strategic_foundations import _dish
    rid = _rid(module_inventory=1)
    _dish(db_path, rid, "Brisket Plate", 12.0, 3.30, qty=1.0, sold=300)
    _dish(db_path, rid, "Wings", 12.0, 3.30, qty=1.0, sold=300)
    monkeypatch.setattr("inventory.load_inventory_for_restaurant", lambda r: ([{"item": "x"}], True))
    monkeypatch.setattr("inventory.compute_item_trends", lambda r, items: {})
    monkeypatch.setattr("inventory.build_price_watch", lambda t: [
        {"item": n, "kind": "trend", "change_pct": 10.0, "old_price": 3.00, "new_price": 3.30}
        for n in ("Brisket Plate base", "Wings base")])
    lm.observe(rid, [_menu_link()])
    sg = {s["dish"]: s for s in mi.reprice_suggestions(rid)["suggestions"]}
    assert sg["Brisket Plate"]["guard"]["text"] == ("Guests are naming the brisket in 5 food quality complaints — "
                                                    "look at the plate before raising its price.")
    assert "guard" not in sg["Wings"]
    import inspect
    import action_queue
    import home_brief
    for mod in (home_brief, action_queue):
        assert 'not x.get("guard")' in inspect.getsource(mod), mod.__name__


def test_marketing_never_promotes_a_dish_on_the_do_not_promote_list(monkeypatch):
    import marketing_opportunities as mo
    import menu_intelligence
    rid = _rid()
    rows = [{"name": n, "units_sold": u, "margin": m, "action": a} for n, u, m, a in (
        ("Brisket Plate", 20, 14.0, "promote"), ("Soup", 90, 4.0, None), ("Salad", 80, 5.0, None),
        ("Tart", 10, 12.0, "promote"), ("Wings", 60, 6.0, None))]
    monkeypatch.setattr(menu_intelligence, "dish_scorecard",
                        lambda *a, **k: {"available": True, "has_sales_data": True, "dishes": rows})
    monkeypatch.setattr(mo, "_sales_days", lambda *a, **k: 28)
    before = [c["subject"] for c in mo.dish_margins(rid)]
    assert before == ["Brisket Plate", "Tart"]
    lm.observe(rid, [_menu_link()])
    assert [c["subject"] for c in mo.dish_margins(rid)] == ["Tart"]


def test_the_food_diagnosis_and_marketing_read_their_links_fenced():
    import ai_guard
    import memory_context
    rid = _rid()
    lm.observe(rid, [_link(), _menu_link(),
                     {"kind": "marketing_x_reviews", "subject": "reviews_up", "modules": ["marketing", "reviews"],
                      "headline": "6 posts went out and reviews rose 40%"}])
    food = memory_context.memory_context(rid, "food_diagnosis")
    assert "Friday carries the service complaints" in food.text and "not a proven cause" in food.text
    assert ai_guard.UNTRUSTED_OPEN in food.text, "guest-derived words are fenced"
    assert "6 posts went out" not in food.text, "each surface reads its own kinds"
    mkt = memory_context.memory_context(rid, "marketing")
    # The dish is fenced, the instruction is trusted (memory re-audit 9/29/26, PROMPTS-1).
    assert "the brisket" in mkt.text and "DO NOT PROMOTE that dish" in mkt.text and "6 posts went out" in mkt.text
    assert "Friday carries" not in mkt.text


def test_the_nightly_pass_settles_the_links():
    import learning_memory
    rid = _rid()
    out = learning_memory.nightly(rid)
    assert out["steps"]["links"] == {"answered": 0, "gone": 0}
