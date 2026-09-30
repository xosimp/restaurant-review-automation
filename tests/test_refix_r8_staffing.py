"""Re-audit 9/29/26 — staffing asks across modules (R8).

CROSSMODULE-5   Home's one thing said "build the next schedule to your
                target, starting with Tuesday" while a live campaign aimed
                to fill that Tuesday.
CROSSMODULE-21  Home's "Trim <day> on the next schedule" was guarded against
                a campaign for THIS week's night.
CROSSMODULE-6   three nightly reports asking for one more dishwasher Friday
                stacked into three "+1"s.
CROSSMODULE-17  a review "+1" with no daypart defaulted to dinner.
CROSSMODULE-10  reviews to labor was open-loop: nothing kept whether the
                published week carried the "+1", or what followed.
"""
import inspect
import json
from datetime import date, timedelta

import pytest

import business_intelligence as bi
import demand_signals
import models
import rec_ledger
import staffing_signals as ss
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(bi, "get_conn", redirect, raising=False)
    monkeypatch.setattr(demand_signals, "DB_PATH", db_path, raising=False)
    yield


def _rid(name="Staff R8 Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


def _exec(sql, args=()):
    conn = models.get_conn()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


LAB = {"is_live": True, "period_days": 28, "date_range": {"days": 28}, "labor_target": 30,
       "potential_savings_monthly": 900.0,
       "dow_summary": {"Monday": 28, "Tuesday": 36, "Wednesday": 29, "Thursday": 28, "Friday": 27,
                       "Saturday": 26, "Sunday": 28}}


def _campaign(rid, day):
    assert demand_signals.record_marketing(rid, day, "Text to 412 guests to fill Tuesday", "campaign")


def _labor_candidate(rid):
    return next(c for c in bi.one_thing_candidates(rid, {"labor": LAB}, []) if c["modules"] == ["labor"])


# ── CROSSMODULE-5 / 21 ───────────────────────────────────────────────────────

def test_the_one_thing_starts_with_the_heaviest_day_nothing_is_filling():
    """Proof 4."""
    rid = _rid()
    assert _labor_candidate(rid)["what"].endswith("starting with Tuesday")
    _campaign(rid, ss.next_draft_date(rid, "Tuesday"))
    c = _labor_candidate(rid)
    assert c["what"].endswith("starting with Wednesday"), c["what"]
    assert c["start_day"] == "Wednesday" and c["trim_guards"][0]["day"] == "Tuesday"
    assert "Not suggesting a Tuesday trim"[1:] in c["why"]
    # The money line says it beside the figure.
    money = bi.money_at_stake(rid, data={"labor": LAB})
    labor_line = next(l for l in money["ranked"] if l["module"] == "labor")
    assert "Tuesday trim" in labor_line["guard"] and "Start with Wednesday." in labor_line["guard"]


def test_every_day_guarded_drops_the_starting_clause():
    rid = _rid()
    for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
        _campaign(rid, ss.next_draft_date(rid, d))
    c = _labor_candidate(rid)
    assert c["what"] == "Build the next schedule to your 30% target" and c["start_day"] is None


def test_a_campaign_for_another_weeks_night_does_not_guard_next_weeks_schedule():
    """A Tuesday campaign in any week but the one the next draft covers
    guards nothing. This week's Tuesday is that week only on a Monday or a
    Tuesday - from Wednesday the next Tuesday IS the draft's - so the test
    failed five days a week on CI (9/30/26). The other week is this week's
    Tuesday while it is still ahead, else the week after the draft's."""
    rid = _rid()
    draft = ss.next_draft_date(rid, "Tuesday")
    this_week = draft - timedelta(days=7)
    other = this_week if this_week >= bi._local_today(rid) else draft + timedelta(days=7)
    assert other != draft
    _campaign(rid, other)
    assert _labor_candidate(rid)["what"].endswith("starting with Tuesday")


def test_the_next_draft_date_is_the_week_the_next_draft_covers():
    from schedule_engine import _week_monday
    for today in (date(2026, 9, 28), date(2026, 9, 29), date(2026, 10, 4)):       # Mon, Tue, Sun
        monday = _week_monday(today)
        got = ss.next_draft_date(1, "Tuesday", today=today)
        assert got == monday + timedelta(days=1) and got > today
    assert ss.next_draft_date(1, "Funday") is None


def test_homes_trim_is_guarded_on_the_next_schedules_night():
    import home_brief
    src = inspect.getsource(home_brief)
    assert 'on_date=_stsig.next_draft_date(rid, trim["day"])' in src


# ── CROSSMODULE-6 ────────────────────────────────────────────────────────────

def test_one_dsr_ask_in_three_reports_is_one_plus_one():
    """Proof 2."""
    rid = _rid()
    t = date.today()
    texts = ["Add a dishwasher Friday at 7pm", "Schedule one more dishwasher for Friday dinner",
             "Bring in another dishwasher on Friday"]
    for i, txt in enumerate(texts):
        narrative = {"actions_tomorrow": [{"kind": "adjust_staffing", "text": txt, "why": "short at the dish pit",
                                           "key": "dsr_action:adjust_staffing:labor"}]}
        _exec("INSERT INTO dsr_reports (restaurant_id, business_date, narrative_json, status, version) "
              "VALUES (?,?,?,?,?)", (rid, (t - timedelta(days=1 + i)).isoformat(), json.dumps(narrative), "final", 1))
    nxt = t + timedelta(days=(7 - t.weekday()) % 7 or 7)
    week = [(nxt + timedelta(days=i)).isoformat() for i in range(7)]
    reqs = ss.dsr_requirements(rid, week, today=t)
    assert [(r["day"], r["role"], r["delta"], r["reports"]) for r in reqs] == [("Friday", "dishwasher", 1, 3)]
    assert reqs[0]["action_text"] == texts[0], "the newest report's words"
    assert "asked in 3 nightly reports" in reqs[0]["text"]
    block = ss.soft_block(reqs)
    assert block.count("+1 dishwasher") == 1 and "asked in 3 nightly reports (one ask, one person" in block


# ── CROSSMODULE-17 ───────────────────────────────────────────────────────────

def _shift(rid, day, start, hours, name, role="server"):
    _exec("INSERT INTO shift_facts (restaurant_id, business_date, employee_name, employee_key, role, shift_start, "
          "actual_hours, source) VALUES (?,?,?,?,?,?,?,?)", (rid, day, name, name.lower(), role, start, hours, "upload"))


def test_a_lunch_restaurants_plus_one_is_for_lunch_not_dinner(monkeypatch):
    rid = _rid()
    today = date.today()
    fridays = [today - timedelta(days=(today.weekday() - 4) % 7 + 7 * k) for k in range(1, 4)]
    for f in fridays:
        _shift(rid, f.isoformat(), "10:00", 6.0, f"Ana{f}")
        _shift(rid, f.isoformat(), "11:00", 5.0, f"Bo{f}")
    assert ss.busiest_daypart(rid, "Friday") == "morning"
    assert ss.busiest_daypart(rid, "Monday") is None
    import review_intelligence as ri
    monkeypatch.setattr(ri, "get_diagnoses", lambda *a, **k: [
        {"category": "service", "cause": "The floor is understaffed on Fridays.", "recommended_action": "Add a server."}])
    monkeypatch.setattr(ss, "service_clusters", lambda *a, **k: [
        {"category": "service", "label": "service", "mentions": 7, "days": ["Friday", "Monday"], "daypart": None,
         "role": None, "text": "guests complain about service on Fridays (7 reviews)"}])
    nxt = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    week = [(nxt + timedelta(days=i)).isoformat() for i in range(7)]
    reqs = {r["day"]: r for r in ss.review_requirements(rid, week)}
    assert reqs["Friday"]["daypart"] == "morning" and "Friday lunch" in reqs["Friday"]["text"]
    assert reqs["Monday"]["daypart"] is None and "(all day)" in reqs["Monday"]["text"]
    assert reqs["Friday"]["key"] == "staff_add:friday:morning:server"
    assert reqs["Monday"]["key"] == "staff_add:monday:day:server"
    # A whole-day requirement reads every row that day.
    mon = reqs["Monday"]["date"]
    rows = [{"date": mon, "employee": "Cy", "role": "Server", "shift_start": "11:00am", "shift_end": "3:00pm"},
            {"date": mon, "employee": "Di", "role": "Server", "shift_start": "5:00pm", "shift_end": "10:00pm"}]
    got = ss.applied([reqs["Monday"]], rows, {("Monday", "morning"): {"server": 1}, ("Monday", "night"): {"server": 1}})
    assert got[0]["applied"] is True and got[0]["scheduled"] == 2
    assert "all day" in ss.soft_block([reqs["Monday"]])


# ── CROSSMODULE-10 ───────────────────────────────────────────────────────────

def test_a_published_week_that_carries_the_plus_one_records_it_and_measures_the_complaints(db_path):
    import outcomes
    rid = _rid()
    fri = (date.today() + timedelta(days=(4 - date.today().weekday()) % 7 + 7)).isoformat()
    req = {"source": "reviews", "day": "Friday", "date": fri, "daypart": "night", "role": "server", "delta": 1,
           "category": "service", "key": ss.staff_add_key("Friday", "night", "server"),
           "text": "+1 server Friday dinner — the reviews diagnosis: guests complain about service on Fridays",
           "typical": 2, "scheduled": 3, "applied": True}
    ids = ss.present_requirements(rid, [req])
    assert ids.get(req["key"])
    row = models.get_conn(db_path).execute("SELECT expected_metric, module FROM rec_instances WHERE restaurant_id=? "
                                           "AND key=?", (rid, req["key"])).fetchone()
    assert row["expected_metric"] == "complaints:service" and row["module"] == "schedule"
    csv = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + "\n".join(
        f"{fri},Friday,{n},Server,5:00pm,11:00pm,6," for n in ("Ann", "Ben", "Cal"))
    hid = models.save_schedule_history(rid, fri, fri, 18, 40, 30, csv, [], db_path=db_path)
    _exec("UPDATE schedule_history SET review_json=? WHERE id=?", (json.dumps({"soft_requirements": [req]}), hid))
    started = []
    orig = outcomes.autostart_implemented
    outcomes.autostart_implemented = lambda r, k, **kw: started.append(k) or orig(r, k, **kw)
    try:
        assert ss.record_published(rid, hid) == [req["key"]]
    finally:
        outcomes.autostart_implemented = orig
    ev = models.get_conn(db_path).execute("SELECT event, dedupe, meta FROM rec_events WHERE restaurant_id=? AND key=? "
                                          "AND event='implemented'", (rid, req["key"])).fetchone()
    assert ev and ev["dedupe"] == f"implemented:schedule:{hid}" and json.loads(ev["meta"])["category"] == "service"
    assert started == [req["key"]], "the complaint theme's tracker is started from the change"
    assert ss.record_published(rid, hid) == [], "once per published week"
    # The ask a published week carried is not asked of the next draft while it is measured.
    assert req["key"] in rec_ledger.silenced_keys(rid)


def test_a_week_that_left_the_plus_one_out_records_nothing(db_path):
    rid = _rid()
    fri = (date.today() + timedelta(days=(4 - date.today().weekday()) % 7 + 7)).isoformat()
    req = {"source": "reviews", "day": "Friday", "date": fri, "daypart": "night", "role": "server", "delta": 1,
           "category": "service", "key": ss.staff_add_key("Friday", "night", "server"), "text": "+1", "typical": 2}
    ss.present_requirements(rid, [req])
    csv = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + "\n".join(
        f"{fri},Friday,{n},Server,5:00pm,11:00pm,6," for n in ("Ann", "Ben"))
    hid = models.save_schedule_history(rid, fri, fri, 12, 40, 30, csv, [], db_path=db_path)
    _exec("UPDATE schedule_history SET review_json=? WHERE id=?", (json.dumps({"soft_requirements": [req]}), hid))
    assert ss.record_published(rid, hid) == []


def test_publish_and_the_draft_wire_the_loop():
    import client_api
    import schedule_engine
    assert "_stsig_pub.record_published(rid, schedule_id" in inspect.getsource(client_api)
    assert "_stsig_ap.present_requirements(restaurant_id" in inspect.getsource(schedule_engine)
    import rec_learning
    assert rec_learning.SURFACE_EXTRA_KINDS["review_diagnosis"] == ("staff_add",)
    assert rec_ledger.KIND_TOPIC["staff_add"] == "staffing"
