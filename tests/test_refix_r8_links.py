"""Re-audit 9/29/26 — the cross-module links (R8).

CROSSMODULE-2   a declined or completed link still reached Ask's snapshot,
                read_business_snapshot and the schedule draft ("reflect this
                in the draft").
CROSSMODULE-3   the one-thing pick kept only the top link BEFORE filtering
                answered ones: one declined recurring link blocked the rest.
CROSSMODULE-4   "still found after you marked it done" could never be seen:
                the ledger held a Done link's key silent for a year.
CROSSMODULE-8   link memory moved only while a surface read it, and a
                measurement that stopped was recorded as a finding resolved.
CROSSMODULE-12  the marketing x labor link never learned what the campaign
                did and kept saying "hold the cut" after the night.
CROSSMODULE-14  one complaint about "the chicken" blocked every chicken dish.
CROSSMODULE-15  link_memory.history() had no caller: Ask could not say a
                link was declined or came back.
CROSSMODULE-16  the Opportunity Feed's cache fingerprint left out bi_links.
"""
from datetime import date, timedelta

import pytest

import business_intelligence as bi
import link_memory as lm
import models
import rec_ledger
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


def _rid(name="Link R8 Co", **flags):
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


def _one(sql, args=()):
    conn = models.get_conn()
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def _dish(db_path, rid, name, price, ingredient_cost, sold=None):
    """One costed menu item with its own ingredient and recent sales —
    written on THIS test's database (an imported helper's bound get_conn
    would be the first test's: CLAUDE.md, bound imports)."""
    conn = models.get_conn(db_path)
    try:
        ing = conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost) VALUES (?,?,?,?)",
                           (rid, f"{name} base", "ea", ingredient_cost)).lastrowid
        mid = conn.execute("INSERT INTO menu_items (restaurant_id, toast_guid, name, sell_price) VALUES (?,?,?,?)",
                           (rid, f"g-{name}", name, price)).lastrowid
        conn.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
                     (mid, ing, 1.0))
        if sold:
            conn.execute("INSERT INTO menu_item_sales (restaurant_id, menu_item_id, business_date, qty_sold) "
                         "VALUES (?,?,?,?)", (rid, mid, (date.today() - timedelta(days=2)).isoformat(), sold))
        conn.commit()
        return mid
    finally:
        conn.close()


def _link(kind, day, subject, modules, headline):
    return {"kind": kind, "day": day, "subject": subject, "modules": modules, "headline": headline,
            "confirm_by": f"Check {day}.", "category": "service", "mentions": 6}


LEAN_FRIDAY = {"reviews": {"clusters": [{"category": "service", "mentions": 7, "window_days": 90,
                                         "weekday": {"value": "Friday", "share": 0.6}}]},
               "labor": {"is_live": True, "period_days": 28, "date_range": {"days": 28},
                         "dow_summary": {"Monday": 30, "Tuesday": 31, "Wednesday": 30, "Thursday": 30,
                                         "Friday": 25, "Saturday": 29, "Sunday": 30}},
               "food_cost": None, "marketing": None, "visibility": None, "dsr": None,
               "degraded": [], "modules_off": [], "complete": True}


def _decline(rid, key, title="x"):
    rec_ledger.present_many(rid, [{"key": key, "module": "home", "title": title}], "home")
    rec_ledger.record(rid, key, "dismissed", surface="home", meta={"kind": "not_for_us"})


# ── CROSSMODULE-2 ────────────────────────────────────────────────────────────

def test_a_declined_link_leaves_asks_snapshot_and_the_schedule_draft(monkeypatch):
    """Proof 3."""
    import schedule_engine
    rid = _rid()
    monkeypatch.setattr(bi, "gather", lambda *a, **k: dict(LEAN_FRIDAY))
    first = bi.correlations(rid)
    key = bi.link_key(first[0])
    assert first[0]["kind"] == "reviews_x_labor"
    # Before the answer, the draft reads it as a QUESTION carrying what it is not.
    notes = schedule_engine._sched_notes_with_findings(rid, "")
    assert "question" in notes and "not a proven cause" in notes and "What it is not:" in notes
    assert "reflect this in the draft" not in notes
    _decline(rid, key)
    lm.settle(rid)
    brief = bi.executive_brief(rid)
    assert brief["links"] == [] and brief["links_answered"] == 1
    snap = bi.snapshot_block(rid)
    assert "leaner on labor" not in snap
    assert "already answered" in snap and "read_restaurant_memory" in snap
    assert schedule_engine._sched_notes_with_findings(rid, "") == ""


def test_the_filter_holds_before_the_nightly_settle_and_for_a_logins_own_answer(monkeypatch):
    rid = _rid()
    monkeypatch.setattr(bi, "gather", lambda *a, **k: dict(LEAN_FRIDAY))
    key = bi.link_key(bi.correlations(rid)[0])
    rec_ledger.present_many(rid, [{"key": key, "module": "home", "title": "x"}], "home")
    # A delegate's decline silences the link for that login only.
    rec_ledger.record(rid, key, "dismissed", surface="home", meta={"kind": "not_for_us"}, user_id=77,
                      authority="delegate")
    assert bi.executive_brief(rid)["links"], "the owner still sees it"
    assert bi.executive_brief(rid, viewer=77)["links"] == [], "the manager who declined it does not"
    # An owner's answer holds everywhere the moment it is given, before settle.
    rec_ledger.record(rid, key, "dismissed", surface="home", meta={"kind": "not_for_us"})
    assert bi.executive_brief(rid)["links"] == []


# ── CROSSMODULE-3 ────────────────────────────────────────────────────────────

def test_a_declined_recurring_link_no_longer_blocks_a_fresh_one():
    """Proof 1."""
    rid = _rid()
    today = date.today()
    old = _link("reviews_x_labor", "Friday", "service:friday", ["reviews", "labor"], "Friday complaints on a lean Friday")
    fresh = _link("dsr_x_reviews", "Tuesday", "service:tuesday", ["reviews", "dsr"],
                  "Tuesday complaints on no-show Tuesdays")
    for d in (21, 14, 7):
        lm.observe(rid, [dict(old)], today=today - timedelta(days=d))
    _decline(rid, bi.link_key(old))
    lm.settle(rid, today=today)
    links = lm.observe(rid, [dict(fresh), dict(old)], today=today)
    cands = bi.one_thing_candidates(rid, {}, links)
    assert [c["key"] for c in cands if c["key"].startswith("link:")] == [bi.link_key(fresh)]
    pick = bi.pick_one_thing(rid, cands, log_rank=False)
    assert pick and pick["key"] == bi.link_key(fresh)


def test_a_declined_links_weeks_do_not_run_on():
    rid = _rid()
    mon = date(2026, 9, 7)
    link = _link("reviews_x_labor", "Friday", "service:friday", ["reviews", "labor"], "h")
    lm.observe(rid, [dict(link)], today=mon)
    lm.observe(rid, [dict(link)], today=mon + timedelta(days=7))
    _decline(rid, bi.link_key(link))
    lm.settle(rid, today=mon + timedelta(days=8))
    for w in (2, 3, 4, 5):
        got = lm.observe(rid, [dict(link)], today=mon + timedelta(days=7 * w))[0]["memory"]
    assert got["weeks_running"] == 2 and got["declined"]


# ── CROSSMODULE-4 ────────────────────────────────────────────────────────────

def test_a_done_link_found_again_after_the_window_reaches_the_surfaces_again():
    rid = _rid()
    link = _link("reviews_x_labor", "Friday", "service:friday", ["reviews", "labor"], "h")
    key = bi.link_key(link)
    lm.observe(rid, [dict(link)], today=date(2026, 9, 7))
    rec_ledger.present_many(rid, [{"key": key, "module": "home", "title": "h"}], "home")
    rec_ledger.record(rid, key, "completed", surface="home")
    assert key in rec_ledger.silenced_keys(rid)
    today = date.today()
    lm.settle(rid, today=today)
    soon = lm.observe(rid, [dict(link)], today=today + timedelta(days=3))[0]["memory"]
    assert soon["resolved"] and key in rec_ledger.silenced_keys(rid)
    back = lm.observe(rid, [dict(link)], today=today + timedelta(days=lm.RECUR_AFTER_DAYS + 1))[0]["memory"]
    assert back["came_back"]["after"] == "done" and not back["resolved"]
    assert key not in rec_ledger.silenced_keys(rid), "the Done's silence is lifted when the link recurs"
    row = _one("SELECT silence_rule FROM rec_instances WHERE restaurant_id=? AND key=?", (rid, key))
    assert row["silence_rule"] == "link_recurred"
    kept, _ans = bi.unanswered_links(rid, [back and dict(link, memory=back)])
    assert kept, "the reopened link reaches the brief"


def test_a_decline_is_never_lifted_by_a_recurrence():
    rid = _rid()
    link = _link("reviews_x_labor", "Friday", "service:friday", ["reviews", "labor"], "h")
    key = bi.link_key(link)
    lm.observe(rid, [dict(link)], today=date(2026, 9, 7))
    _decline(rid, key)
    lm.settle(rid, today=date.today())
    lm.observe(rid, [dict(link)], today=date.today() + timedelta(days=90))
    assert key in rec_ledger.silenced_keys(rid)


# ── CROSSMODULE-8 ────────────────────────────────────────────────────────────

def test_a_lapsed_measurement_is_not_a_consulted_module():
    data = {"reviews": {"brief": {"coverage": {"total": 40}}, "clusters": []},
            "food_cost": {"weekday_waste": {"has_data": False}, "brief": {}},
            "labor": {"is_live": True}, "marketing": {"posts_published": 0},
            "visibility": {"ai_score": 40, "stale": True}, "dsr": None}
    got = lm.consulted_modules(data)
    assert "food_cost" not in got, "waste logging lapsed: a waste link was not looked for"
    assert "intel" not in got and {"reviews", "labor", "marketing"} <= got
    data["food_cost"]["weekday_waste"]["has_data"] = True
    assert "food_cost" in lm.consulted_modules(data)


def test_the_nightly_pass_observes_the_links_once_a_day(monkeypatch):
    import learning_memory
    rid = _rid()
    monkeypatch.setattr(bi, "gather", lambda *a, **k: dict(LEAN_FRIDAY))
    assert [n for n, _f in learning_memory.STEPS].index("observe_links") < \
        [n for n, _f in learning_memory.STEPS].index("links"), "observed before it is settled"
    out = learning_memory.nightly(rid)
    assert out["steps"]["observe_links"] == {"found": 1}
    assert _one("SELECT COUNT(*) AS n FROM bi_links WHERE restaurant_id=?", (rid,))["n"] == 1


# ── CROSSMODULE-12 ───────────────────────────────────────────────────────────

LAB = {"is_live": True, "period_days": 28, "date_range": {"days": 28},
       "dow_summary": {"Monday": 28, "Tuesday": 36, "Wednesday": 29, "Thursday": 28, "Friday": 27,
                       "Saturday": 26, "Sunday": 28}}


def test_a_campaign_link_ends_with_what_its_night_measured():
    import event_memory
    rid = _rid()
    today = bi._local_today(rid)
    sent = today - timedelta(days=10)
    fc = {"day": "Tuesday", "sent": 412, "total": 412, "queued": False, "on": sent.isoformat()}
    night = bi._campaign_night_date(fc, rid)
    assert night.strftime("%A") == "Tuesday" and sent <= night < today
    # While the night is ahead, the link holds the cut.
    live = dict(fc, on=today.isoformat())
    links = [l for l in bi.correlations(rid, data={"labor": LAB, "marketing": {"fill_campaigns": [live]}})
             if l["kind"] == "marketing_x_labor"]
    assert links and "Hold any cut to Tuesday" in links[0]["confirm_by"]
    key = bi.link_key(links[0])
    # The night passed and was measured: the link ends, saying what it measured.
    _exec("INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, raw_label, lift_pct) "
          "VALUES (?,?,?,?,?,?,?)", (rid, night.isoformat(), "Tuesday", "campaign", "guest text campaign",
                                     "guest text campaign", 4.0))
    assert event_memory.campaign_night(rid, night)["lift_pct"] == 4.0
    links = [l for l in bi.correlations(rid, data={"labor": LAB, "marketing": {"fill_campaigns": [fc]}})
             if l["kind"] == "marketing_x_labor"]
    assert not links, "no more 'hold the cut' once the night has passed"
    row = _one("SELECT resolved_by, headline FROM bi_links WHERE restaurant_id=? AND link_key=?", (rid, key))
    assert row["resolved_by"] == "measured" and "+4% sales against a typical Tuesday" in row["headline"]
    assert "not proof" in row["headline"]
    # The verdict reaches the schedule's memory for that weekday.
    import memory_context
    lines = lm.link_lines(memory_context.MemoryRequest(rid, "schedule"))
    assert lines and lines[0]["subject"] == "labor:day:tuesday" and "+4%" in lines[0]["text"]
    # A new campaign for the same night reopens it.
    again = [l for l in bi.correlations(rid, data={"labor": LAB, "marketing": {"fill_campaigns": [live]}})
             if l["kind"] == "marketing_x_labor"]
    assert again and not again[0]["memory"]["resolved"]
    assert again[0]["memory"]["label"].endswith("with a new campaign")


def test_a_campaign_night_with_nothing_measured_closes_its_window():
    rid = _rid()
    today = bi._local_today(rid)
    live = {"day": "Tuesday", "sent": 412, "total": 412, "queued": False, "on": today.isoformat()}
    key = bi.link_key(next(l for l in bi.correlations(rid, data={"labor": LAB, "marketing": {"fill_campaigns": [live]}})
                           if l["kind"] == "marketing_x_labor"))
    past = dict(live, on=(today - timedelta(days=10)).isoformat())
    bi.correlations(rid, data={"labor": LAB, "marketing": {"fill_campaigns": [past]}})
    row = _one("SELECT resolved_by, headline FROM bi_links WHERE restaurant_id=? AND link_key=?", (rid, key))
    assert row["resolved_by"] == "window_closed" and "not known" in row["headline"]


# ── CROSSMODULE-14 ───────────────────────────────────────────────────────────

def test_one_complaint_about_the_chicken_blocks_one_chicken_dish(db_path):
    """same.py: "the chicken" matched Chicken Parm, the Chicken Caesar Salad
    and the Buffalo Chicken Wings."""
    rid = _rid(module_inventory=1)
    parm = _dish(db_path, rid, "Chicken Parm", 18.0, 4.0, sold=40)
    caesar = _dish(db_path, rid, "Chicken Caesar Salad", 14.0, 3.0, sold=120)
    wings = _dish(db_path, rid, "Buffalo Chicken Wings", 12.0, 3.0, sold=80)
    link = {"kind": "reviews_x_menu", "dish": "the chicken", "subject": bi._link_subject("food_quality", "the chicken"),
            "modules": ["reviews", "food_cost"], "category": "food_quality", "mentions": 5,
            "headline": "Guests name the chicken in 5 food quality complaints, and it is also a cost driver"}
    mid, name = lm.resolve_menu_item(rid, "the chicken")
    assert (mid, name) == (caesar, "Chicken Caesar Salad"), "the best seller the guests' words name"
    lm.observe(rid, [dict(link, menu_item_id=mid)])
    listed = lm.do_not_promote(rid)
    assert lm.names_dish(listed, "Chicken Caesar Salad") and lm.names_dish(listed, "x", menu_item_id=caesar)
    for other_id, other in ((parm, "Chicken Parm"), (wings, "Buffalo Chicken Wings")):
        assert not lm.names_dish(listed, other), other
        assert not lm.names_dish(listed, other, menu_item_id=other_id), other
        assert lm.dish_guard(rid, other, menu_item_id=other_id) is None
    # The driver's own dish wins when Food Cost's driver is a menu item.
    assert lm.resolve_menu_item(rid, "the chicken", driver_item="Chicken Parm", driver_kind="menu")[0] == parm
    # An ingredient driver prefers the dish that uses it.
    assert lm.resolve_menu_item(rid, "the chicken", driver_item="Buffalo Chicken Wings base",
                                driver_kind="waste")[0] == wings


def test_a_link_stored_before_resolution_is_resolved_when_read(db_path):
    rid = _rid(module_inventory=1)
    _dish(db_path, rid, "Chicken Parm", 18.0, 4.0, sold=40)
    caesar = _dish(db_path, rid, "Chicken Caesar Salad", 14.0, 3.0, sold=120)
    lm.observe(rid, [{"kind": "reviews_x_menu", "dish": "the chicken", "subject": "food_quality:the chicken",
                      "modules": ["reviews", "food_cost"], "headline": "h"}])
    assert _one("SELECT menu_item_id FROM bi_links WHERE restaurant_id=?", (rid,))["menu_item_id"] is None
    entry = lm.do_not_promote(rid)["the chicken"]
    assert entry["menu_item_id"] == caesar and not lm.names_dish({"the chicken": entry}, "Chicken Parm")


# ── CROSSMODULE-15 ───────────────────────────────────────────────────────────

def test_ask_reads_the_link_history_labelled_and_by_module_view():
    import ask_cavnar_tools as act
    rid = _rid()
    lab = _link("reviews_x_labor", "Friday", "service:friday", ["reviews", "labor"], "Friday complaints, lean Friday")
    food = {"kind": "reviews_x_food_cost", "day": "Friday", "subject": "service:friday",
            "modules": ["reviews", "food_cost"], "headline": "Friday carries 40% of waste"}
    lm.observe(rid, [dict(lab), dict(food)], today=date(2026, 9, 7))
    _decline(rid, bi.link_key(lab))
    lm.settle(rid)
    hist = {h["kind"]: h for h in lm.history_lines(rid)}
    assert hist["reviews_x_labor"]["status"] == "you passed on it" and hist["reviews_x_labor"]["resolved_on"]
    assert hist["reviews_x_food_cost"]["status"] == "open" and hist["reviews_x_food_cost"]["first_found"] == "9/7/26"
    assert [h["kind"] for h in lm.history_lines(rid, denied={"inventory"})] == ["reviews_x_labor"]
    assert "reviews_x_labor" not in [h["kind"] for h in lm.history_lines(rid, denied={"labor"})]
    got = act._read_restaurant_memory(rid)
    assert {h["kind"]: h["status"] for h in got["links"]} == {"reviews_x_labor": "you passed on it",
                                                               "reviews_x_food_cost": "open"}
    assert "you passed on it" in got["links_note"]


# ── CROSSMODULE-16 ───────────────────────────────────────────────────────────

def test_the_feed_fingerprint_moves_with_the_do_not_promote_links():
    import marketing_opportunities as mo
    rid = _rid()
    today = date.today()
    before = mo.fingerprint(rid, today)
    lm.observe(rid, [{"kind": "reviews_x_menu", "dish": "the brisket", "subject": "food_quality:the brisket",
                      "modules": ["reviews", "food_cost"], "headline": "h"}])
    found = mo.fingerprint(rid, today)
    assert found != before, "a link found at noon changes the feed at noon"
    _exec("UPDATE bi_links SET resolved_at=?, resolved_by='gone' WHERE restaurant_id=?", (today.isoformat(), rid))
    assert mo.fingerprint(rid, today) != found, "and so does one that resolves"
