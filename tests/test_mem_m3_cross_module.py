"""Staffing reads what reviews, the DSR and marketing know about its nights
(memory audit 9/29/26: reviews_to_labor, dsr_to_schedule, mkt_to_staffing).

  * A "slow service" cluster on Friday/Saturday dinner sat beside "Trim
    Saturday staffing"; a dinner complaint posted on Sunday was scored
    against Sunday lunch.
  * Three Friday reports said "add a dishwasher Friday" and Thursday's draft
    repeated the same Friday.
  * The owner texted 412 guests to fill Tuesday; the draft staffed a slow
    Tuesday, Home said "Trim Tuesday staffing", the lineup and the kitchen
    never heard.
"""
import json
from datetime import date, datetime, timedelta

import pytest

import demand
import demand_signals
import models
import preshift
import schedule_intel
import staffing_signals
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(demand_signals, "get_conn", fake)
    monkeypatch.setattr(demand, "get_conn", fake, raising=False)
    yield


def _rid(**kw):
    fields = dict(name="Cross Co", owner_email="c@x.test", module_labor=1, module_reviews=1, module_marketing=1)
    fields.update(kw)
    return create_restaurant(Restaurant(**fields))


def _q(sql, args=()):
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ── marketing → staffing ────────────────────────────────────────────────────

def test_a_fill_tuesday_text_is_a_signal_on_tuesday_that_every_reader_sees():
    rid = _rid()
    assert demand_signals.record_campaign(rid, "tuesday", 412, campaign_id=9, today=date(2026, 9, 29)) is True
    row = _q("SELECT * FROM demand_signals WHERE restaurant_id=?", (rid,))[0]
    assert row["date"] == "2026-09-29" and row["source"] == "campaign" and row["ref"] == "campaign:9"
    assert row["label"] == "Text to 412 guests to fill Tuesday"
    assert row["lift_pct"] is None                         # no measured campaign lift yet: the assumed path
    block = demand_signals.prompt_block(demand_signals.by_date(rid, ["2026-09-29"]), ["2026-09-29"])
    assert "Text to 412 guests to fill Tuesday" in block and "ASSUMED" in block
    # The owner's words are fenced and the date is M/D/YY, like all memory in a prompt.
    import ai_guard
    assert "Tuesday 9/29/26: " + ai_guard.UNTRUSTED_OPEN in block and "2026-09-29" not in block
    # No trim of that night, and the reason is said.
    g = staffing_signals.trim_guard(rid, "Tuesday", on_date=date(2026, 9, 29))
    assert g["suppress"] is True and "Text to 412 guests" in g["why"] and "9/29/26" in g["why"]


def test_the_lift_is_this_restaurants_measured_median_after_three_campaigns():
    rid = _rid()
    conn = models.get_conn()
    for i, pct in enumerate((12.0, 20.0, 8.0)):
        conn.execute("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, status, "
                     "delta_pct, started_on, evaluate_on) VALUES (?,?,?,?,?,?,?,'2026-08-01','2026-08-29')",
                     (rid, "slow_day_campaign", f"campaign:Tuesday:{i}", "t", "weekday_sales:Tuesday", "evaluated", pct))
    conn.commit()
    conn.close()
    assert demand_signals.measured_campaign_lift(rid) == {"lift_pct": 12, "n": 3}
    demand_signals.record_campaign(rid, "Tuesday", 300, today=date(2026, 9, 29))
    assert _q("SELECT lift_pct FROM demand_signals WHERE restaurant_id=?", (rid,))[0]["lift_pct"] == 12


def test_a_post_about_a_dish_reaches_the_lineup_and_the_prep_list():
    rid = _rid()
    conn = models.get_conn()
    mid = conn.execute("INSERT INTO menu_items (restaurant_id, name, is_active) VALUES (?,?,1)",
                       (rid, "Margherita Pizza")).lastrowid
    conn.commit()
    conn.close()
    day = date(2026, 10, 2)
    assert demand_signals.record_post(rid, day.isoformat(), "Margherita Pizza night", platform="instagram",
                                      post_id=5) is True
    assert demand_signals.record_post(rid, day.isoformat(), "a nice photo of the dining room") is False
    promoted = demand.promoted_dishes(rid, day)
    assert promoted and promoted[0]["dish"] == "Margherita Pizza" and promoted[0]["menu_item_id"] == mid
    items = preshift.build(rid, day=day)["items"]
    assert any(i["kind"] == "promotion" and "Margherita Pizza" in i["text"] for i in items)


def test_a_campaign_that_went_out_writes_its_signal(monkeypatch):
    import guest_marketing
    rid = _rid()
    monkeypatch.setattr("outcomes.start", lambda *a, **k: None)
    guest_marketing.track_campaign_outcome(rid, "Friday", {"ok": True, "sent": 88})
    rows = _q("SELECT label, source FROM demand_signals WHERE restaurant_id=?", (rid,))
    assert rows == [{"label": "Text to 88 guests to fill Friday", "source": "campaign"}]


# ── reviews → labor ─────────────────────────────────────────────────────────

def _review(rid, rating, posted, daypart=None):
    conn = models.get_conn()
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, review_date, fetched_at, "
                 "processed, sentiment, entities) VALUES (?,?,?,?,?,?,datetime('now'),1,?,?)",
                 (rid, "google", f"x{posted}{rating}{daypart}", rating, "t", posted,
                  "negative" if rating <= 2 else "positive", json.dumps({"daypart": daypart} if daypart else {})))
    conn.commit()
    conn.close()


def test_a_dinner_review_posted_sunday_afternoon_is_saturday_nights():
    rid = _rid()
    by = {("2026-09-26", "night"): {"hours": 30}, ("2026-09-27", "morning"): {"hours": 20},
          ("2026-09-27", "night"): {"hours": 10}}
    _review(rid, 2, "2026-09-27T14:05:00", daypart="dinner")          # before Sunday's dinner began
    _review(rid, 4, "2026-09-27", daypart=None)                        # no meal named: the busiest daypart
    from zoneinfo import ZoneInfo
    conn = models.get_conn()
    try:
        placed = schedule_intel._place_reviews(conn, rid, by, ZoneInfo("America/Chicago"))
    finally:
        conn.close()
    assert placed[("2026-09-26", "night")] == [{"rating": 2.0, "how": "daypart", "lag": 1}]
    assert placed[("2026-09-27", "morning")] == [{"rating": 4.0, "how": "hours", "lag": 0}]


def test_a_service_cluster_on_the_night_cautions_a_trim_and_ranks_it_lower(monkeypatch):
    rid = _rid()
    import review_intelligence as ri
    monkeypatch.setattr(ri, "complaint_clusters", lambda r, **k: [{
        "category": "service", "mentions": 7, "role": {"value": "server"}, "daypart": {"value": "dinner"},
        "weekday": None, "weekday_pair": {"days": ["Friday", "Saturday"]}}])
    g = staffing_signals.trim_guard(rid, "Saturday", daypart="night")
    assert g["suppress"] is False and g["rank_penalty"] > 0
    assert "service on Fridays and Saturdays at dinner (7 reviews)" in g["caution"]
    assert staffing_signals.trim_guard(rid, "Tuesday")["caution"] is None


def test_a_staffing_diagnosis_is_a_soft_plus_one_for_the_draft(monkeypatch):
    rid = _rid()
    import review_intelligence as ri
    monkeypatch.setattr(ri, "complaint_clusters", lambda r, **k: [{
        "category": "wait_time", "mentions": 9, "role": {"value": "server"}, "daypart": {"value": "dinner"},
        "weekday": {"value": "Friday"}, "weekday_pair": None}])
    monkeypatch.setattr(ri, "get_diagnoses", lambda r, **k: [{
        "category": "wait_time", "cause": "Friday dinner is understaffed on the floor",
        "recommended_action": "Add a server Friday dinner", "what_would_confirm": "wait times with 5 servers",
        "as_of": "9/28/26"}])
    week = [(date(2026, 10, 5) + timedelta(days=i)).isoformat() for i in range(7)]
    reqs = staffing_signals.review_requirements(rid, week)
    assert len(reqs) == 1 and reqs[0]["text"].startswith("+1 server Friday dinner") and reqs[0]["date"] == "2026-10-09"
    block = staffing_signals.soft_block(reqs)
    assert "SOFT STAFFING REQUIREMENTS" in block and "Friday 10/9/26 dinner: +1 server Friday dinner" in block
    import ai_guard
    assert "(to confirm: " + ai_guard.wrap_untrusted("wait times with 5 servers") + ")" in block
    rows = [{"date": "2026-10-09", "employee": n, "role": "Server", "shift_start": "5:00pm", "shift_end": "10:00pm"}
            for n in ("A", "B", "C")]
    done = staffing_signals.applied(reqs, rows, {("Friday", "night"): {"Server": 2}})
    assert done[0]["applied"] is True and done[0]["scheduled"] == 3


# ── the DSR → the schedule ──────────────────────────────────────────────────

def _metric(rid, day, metric, value):
    conn = models.get_conn()
    conn.execute("INSERT OR REPLACE INTO dsr_metrics (restaurant_id, business_date, metric, value, status) "
                 "VALUES (?,?,?,?,'ready')", (rid, day, metric, value))
    conn.commit()
    conn.close()


def test_what_the_last_nights_showed_reaches_the_draft(monkeypatch):
    import ai_guard
    import event_memory
    monkeypatch.setattr(event_memory, "night_facts", lambda r, day, db_path=None: [
        {"kind": "event", "label": "cubs game", "display": "Cubs game (closer's note)"}]
        if str(day) == "2026-09-25" else [], raising=False)
    rid = _rid()
    fridays = [date(2026, 9, 25) - timedelta(weeks=w) for w in range(3)]
    for f in fridays:
        _metric(rid, f.isoformat(), "labor.no_shows", 1)
        _metric(rid, f.isoformat(), "labor.overtime_hours", 4)
        _metric(rid, f.isoformat(), "labor.vs_target_pts", 2.0)
    week = [(date(2026, 9, 28) + timedelta(days=i)).isoformat() for i in range(7)]
    block = staffing_signals.last_nights_block(rid, week, today=date(2026, 9, 28))
    assert "WHAT THE LAST NIGHTS SHOWED" in block
    assert "Fridays (3 nights reported): 3 no-shows, 12 overtime hours, labor 2.0 pts over target" in block
    assert "Tuesdays" not in block                       # nothing measured, nothing said
    # What the night was (event_memory, M5) is a person's words: fenced.
    assert ai_guard.wrap_untrusted("9/25/26: Cubs game (closer's note)") in block


def test_an_open_dsr_staffing_action_is_a_soft_requirement_until_its_week_passes():
    rid = _rid()
    conn = models.get_conn()
    conn.execute("INSERT INTO dsr_reports (restaurant_id, business_date, version, status, narrative_json) "
                 "VALUES (?,?,1,'final',?)",
                 (rid, "2026-09-25", json.dumps({"actions_tomorrow": [
                     {"kind": "adjust_staffing", "text": "Add a dishwasher Friday at 7pm.", "key": "dsr_action:adj:1"},
                     {"kind": "control_hours", "text": "Cut a server early Tuesday.", "key": "dsr_action:ctl:1"}]})))
    conn.commit()
    conn.close()
    week = [(date(2026, 9, 28) + timedelta(days=i)).isoformat() for i in range(7)]
    reqs = staffing_signals.dsr_requirements(rid, week, today=date(2026, 9, 28))
    assert [(r["day"], r["role"]) for r in reqs] == [("Friday", "dishwasher")]
    assert "the 9/25/26 report" in reqs[0]["text"]
    # In the prompt the report's action is the DSR model's own words, fenced;
    # the date is M/D/YY.
    import ai_guard
    block = staffing_signals.soft_block(reqs)
    assert "Friday 10/2/26 dinner: +1 dishwasher — the 9/25/26 report asked, in its words: " in block
    assert ai_guard.wrap_untrusted("Add a dishwasher Friday at 7pm.") in block and "2026-10-02" not in block
    # A week later the Friday it was about has passed.
    later = [(date(2026, 10, 12) + timedelta(days=i)).isoformat() for i in range(7)]
    assert staffing_signals.dsr_requirements(rid, later, today=date(2026, 10, 12)) == []


def test_the_dsr_drops_a_cut_of_a_night_the_owner_is_filling():
    from dsr import narrative
    rid = _rid()
    demand_signals.record_campaign(rid, "Wednesday", 200, today=date(2026, 9, 29))

    class _Ctx:
        restaurant_id = rid
        day = "2026-09-29"
        db_path = None
    g = narrative._staffing_guard({"kind": "control_hours", "text": "Cut one server early tomorrow."}, _Ctx())
    assert g["suppress"] is True
    add = narrative._staffing_guard({"kind": "adjust_staffing", "text": "Add a host tomorrow."}, _Ctx())
    assert add == {}
