"""The publish gate, shift requests, and the engine's own backstops.

Publishing used to be one click with no look at what the engine had
flagged; Friday's auto-publish sent a week the engine could not vouch
for; the staff portal showed drafts as if they were the schedule. And
the backstops filled the labor budget as though it were a quota.
"""
import datetime as dt
import json

import pytest

import auth
import client_api
import labor
import mobile_api
import models
import schedule_engine
import schedule_rules as sr
import schedule_versions as sv
import shift_requests as srq
import staff_schedule
import staff_settings as ss
from models import create_restaurant, Restaurant, get_conn

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
TODAY = dt.date(2026, 9, 28)


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn
    import time_off, staff_routes
    for mod in (models, auth, client_api, mobile_api, labor, schedule_engine, sr, sv, srq, ss, time_off,
                staff_schedule, staff_routes):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    rid = create_restaurant(Restaurant(name="Gate Co", owner_email="g@x.com"), db_path=db_path)
    for n in ("Ana", "Bob", "Cy"):
        models.add_manual_team_member(rid, n, role="Server", db_path=db_path)
    return rid


def _history(db_path, rid, csv_text, quality=None, review=None, published=False):
    conn = get_conn(db_path)
    models._ensure_history_columns(conn)
    cur = conn.execute(
        "INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, labor_target, "
        "schedule_csv, summary_json, quality_json, review_json, published_at) VALUES (?,?,?,?,?,?,?,'[]',?,?,?)",
        (rid, WEEK[0], WEEK[6], 12, 40, 30, csv_text,
         json.dumps(quality) if quality else None, json.dumps(review) if review else None,
         "2026-10-01 10:00:00" if published else None))
    conn.commit()
    hid = cur.lastrowid
    conn.close()
    return hid


CLEAN = HEADER + f"{WEEK[0]},Monday,Ana,Server,4:00pm,10:00pm,6.0,\n{WEEK[1]},Tuesday,Bob,Server,4:00pm,10:00pm,6.0,\n"


# ── publish blockers ───────────────────────────────────────────────────

def test_a_clean_week_has_no_blockers(db_path, rid):
    hid = _history(db_path, rid, CLEAN, quality={"checked": True, "band": "solid", "score": 78, "confidence": {"level": "high"}})
    assert client_api.publish_blockers(rid, hid) == []


def test_blockers_name_flagged_rows_hard_breaches_and_a_weak_verdict(db_path, rid):
    csv_text = CLEAN.replace(f"{WEEK[1]},Tuesday,Bob,Server,4:00pm,10:00pm,6.0,",
                             f"{WEEK[1]},Tuesday,Bob,Server,4:00pm,10:00pm,6.0,NEEDS REVIEW: approved time off")
    hid = _history(db_path, rid, csv_text,
                   quality={"checked": True, "band": "weak", "score": 41, "confidence": {"level": "low"}},
                   review={"hard": 1, "lines": ["⚠ Bob — Tuesday 4:00pm: approved time off"]})
    b = client_api.publish_blockers(rid, hid)
    assert "1 shift marked NEEDS REVIEW" in b
    assert "Bob — Tuesday 4:00pm: approved time off" in b
    assert any("weak week" in x for x in b) and any("too little to judge" in x for x in b)


def test_publish_refuses_blockers_until_acknowledged_then_stamps_published_at(db_path, rid, monkeypatch):
    csv_text = CLEAN.replace("6.0,\n", "6.0,NEEDS REVIEW: off roster\n", 1)
    hid = _history(db_path, rid, csv_text)
    conn = get_conn(db_path)
    conn.execute("INSERT OR REPLACE INTO staff_contacts (restaurant_id, employee_name, email) VALUES (?,?,?)", (rid, "Ana", "ana@x.com"))
    conn.execute("INSERT OR REPLACE INTO staff_contacts (restaurant_id, employee_name, email) VALUES (?,?,?)", (rid, "Bob", "bob@x.com"))
    conn.commit()
    conn.close()
    sent = []
    import emails
    monkeypatch.setattr(emails, "send_staff_schedule_email", lambda **kw: sent.append(kw["employee_name"]))
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)

    payload, status = client_api._publish_schedule(rid, hid, actor={"id": 1, "username": "will"})
    assert status == 409 and payload["needs_ack"] and payload["blockers"] and sent == []

    payload, status = client_api._publish_schedule(rid, hid, actor={"id": 1, "username": "will"}, acknowledge=True)
    assert status == 200 and payload["ok"] and payload["acknowledged"]
    assert sorted(sent) == ["Ana", "Bob"]
    conn = get_conn(db_path)
    row = conn.execute("SELECT published_at, published_by FROM schedule_history WHERE id=?", (hid,)).fetchone()
    conn.close()
    assert row["published_at"] and row["published_by"] == "will"
    versions = sv.list_versions(rid, hid, db_path=db_path)
    assert versions[-1]["reason"] == "published"


def test_auto_publish_holds_a_week_with_blockers():
    """Read at the source: the Friday job must ask publish_blockers and
    tell the owner (schedule_publish_held) before it can ever reach
    _publish_schedule. A fixture-shaped test of one restaurant would only
    cover that restaurant's branch."""
    import scheduler
    src = open(scheduler.__file__).read()
    assert "publish_blockers" in src and "schedule_publish_held" in src
    # The gate is read at the source level too: the job must consult
    # publish_blockers before it ever reaches _publish_schedule.
    fn = src[src.index("def run_auto_publish_schedules"):]
    assert fn.index("publish_blockers") < fn.index("delayed.schedule(")
    assert fn.index("schedule_publish_held") < fn.index("delayed.schedule(")


# ── the staff portal shows only published weeks ────────────────────────

def test_staff_portal_reads_only_published_weeks(db_path, rid):
    _history(db_path, rid, CLEAN, published=True)
    draft = HEADER + f"{WEEK[0]},Monday,Ana,Server,9:00am,3:00pm,6.0,\n"
    _history(db_path, rid, draft, published=False)
    data = staff_schedule.shifts_for_employee(rid, "Ana", today=dt.date(2026, 10, 4))
    assert data["published"] and data["upcoming"]
    blob = json.dumps(data)
    assert "4:00pm" in blob and "9:00am" not in blob


# ── shift requests ─────────────────────────────────────────────────────

def test_drop_decide_claim_writes_the_cover_back_as_a_version(db_path, rid, monkeypatch):
    hid = _history(db_path, rid, CLEAN, published=True)
    sv.append(rid, hid, "published", CLEAN, db_path=db_path)
    with pytest.raises(srq.ShiftRequestError):
        srq.request_drop(rid, "Ana", WEEK[0], "9:00am", db_path=db_path, today=TODAY)   # not her shift
    req = srq.request_drop(rid, "Ana", WEEK[0], "4:00pm", reason="exam", db_path=db_path, today=TODAY)
    assert req["status"] == "pending" and req["role"] == "Server"
    with pytest.raises(srq.ShiftRequestError):
        srq.request_drop(rid, "Ana", WEEK[0], "4:00pm", db_path=db_path, today=TODAY)   # already asked
    assert [r["id"] for r in srq.for_manager(rid, db_path=db_path)] == [req["id"]]
    assert srq.open_shifts(rid, db_path=db_path) == []

    decided = srq.decide(rid, req["id"], True, decided_by="will", db_path=db_path)
    assert decided["status"] == "open" and decided["decided_by"] == "will"
    assert [r["id"] for r in srq.open_shifts(rid, db_path=db_path)] == [req["id"]]

    # Bob already works Tuesday 4pm: Monday 4pm is fine for him. Cy is
    # deactivated, so he cannot take it, whatever the picker says.
    ss.upsert(rid, "Cy", active=False, db_path=db_path)
    with pytest.raises(srq.ShiftRequestError) as e:
        srq.claim(rid, req["id"], "Cy", db_path=db_path)
    assert sr.LABELS["inactive"] in str(e.value)
    covered = srq.claim(rid, req["id"], "Bob", db_path=db_path)
    assert covered["status"] == "covered" and covered["replacement_name"] == "Bob"
    conn = get_conn(db_path)
    csv_now = conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (hid,)).fetchone()["schedule_csv"]
    conn.close()
    rows = sv.rows_from_csv(csv_now)
    mon = next(r for r in rows if r["date"] == WEEK[0])
    assert mon["employee"] == "Bob" and "covered for Ana" in mon["notes"]
    assert sv.list_versions(rid, hid, db_path=db_path)[-1]["reason"] == "edited"
    assert srq.mine(rid, "Ana", db_path=db_path)[0]["status"] == "covered"


def test_denied_and_withdrawn_requests_change_nothing(db_path, rid):
    hid = _history(db_path, rid, CLEAN, published=True)
    req = srq.request_drop(rid, "Ana", WEEK[0], "4:00pm", db_path=db_path, today=TODAY)
    assert srq.decide(rid, req["id"], False, db_path=db_path)["status"] == "denied"
    assert srq.decide(rid, req["id"], True, db_path=db_path) is None       # decided once
    req2 = srq.request_drop(rid, "Bob", WEEK[1], "4:00pm", db_path=db_path, today=TODAY)
    assert srq.withdraw(rid, req2["id"], "Ana", db_path=db_path) is False   # not hers
    assert srq.withdraw(rid, req2["id"], "Bob", db_path=db_path) is True
    conn = get_conn(db_path)
    csv_now = conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (hid,)).fetchone()["schedule_csv"]
    conn.close()
    assert csv_now == CLEAN


def test_a_past_shift_cannot_be_dropped(db_path, rid):
    _history(db_path, rid, CLEAN, published=True)
    with pytest.raises(srq.ShiftRequestError):
        srq.request_drop(rid, "Ana", WEEK[0], "4:00pm", db_path=db_path, today=dt.date(2026, 10, 6))


# ── engine backstops honour the constraints ────────────────────────────

def _plain_constraints(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(date, emp, start, end, role="Pizza Cook", hours=6.0):
    return {"date": date, "day": DAYS[WEEK.index(date)], "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(hours), "notes": ""}


def test_role_floor_backstop_never_uses_someone_on_time_off(monkeypatch):
    monkeypatch.setattr(models, "get_staff_availability", lambda r, *a, **k: [])
    floors = {"Pizza Cook": {"morning": 0, "night": 1, "days": {}}}
    rows = [_row(WEEK[0], "Ana", "4:00pm", "10:00pm"), _row(WEEK[1], "Bob", "4:00pm", "10:00pm")]
    c = _plain_constraints(blocked_dates={"ana": {WEEK[2]: sr.LABELS["approved_time_off"]},
                                          "bob": {WEEK[2]: sr.LABELS["approved_time_off"]}})
    out, added, dates = schedule_engine._ensure_role_floors(rows, WEEK[:3], DAYS[:3], 1, {}, {}, floors=floors, constraints=c)
    assert WEEK[2] not in dates                       # both cooks are off Wednesday: gap stays visible
    c2 = _plain_constraints(blocked_dates={"ana": {WEEK[2]: sr.LABELS["approved_time_off"]}})
    out, added, dates = schedule_engine._ensure_role_floors(list(rows), WEEK[:3], DAYS[:3], 1, {}, {}, floors=floors, constraints=c2)
    assert dates == {WEEK[2]: 1}
    new = [r for r in out if r["date"] == WEEK[2]][0]
    assert new["employee"] == "Bob" and "floor" in new["notes"]


def test_role_floor_backstop_respects_the_hours_ceiling_and_rest(monkeypatch):
    monkeypatch.setattr(models, "get_staff_availability", lambda r, *a, **k: [])
    floors = {"Pizza Cook": {"morning": 1, "night": 0, "days": {}}}
    # Ana closes Monday; a Tuesday morning floor would break her rest. Bob
    # is at 38h already, a 4h shift would take him past 40.
    rows = [_row(WEEK[0], "Ana", "4:00pm", "11:30pm", hours=7.5), _row(WEEK[0], "Bob", "8:00am", "2:00pm", hours=38)]
    c = _plain_constraints(open_times={"Tuesday": "7:00am"}, close_times={"Tuesday": "10:00pm"})
    out, added, dates = schedule_engine._ensure_role_floors(rows, WEEK[:2], DAYS[:2], 1, {}, {}, floors=floors, constraints=c)
    assert dates == {}                                # nobody legal — no invented cook


def test_hours_top_up_never_spends_toward_the_budget(monkeypatch):
    """A week well under budget with every role at its usual headcount
    gains nothing: the budget is a ceiling, not a quota."""
    monkeypatch.setattr(models, "get_staff_availability", lambda r, *a, **k: [])
    rows = [_row(d, n, "4:00pm", "10:00pm", role="Server") for d in WEEK for n in ("Ana", "Bob")]
    targets = {d: 40.0 for d in WEEK}
    out, hours_added, dates = schedule_engine._top_up_hours_gap(
        list(rows), targets, hours_budget=280.0, hours_scheduled=84.0, restaurant_id=1,
        close_times={}, role_buffers={}, constraints=_plain_constraints())
    assert hours_added == 0 and dates == {} and len(out) == len(rows)


def test_hours_top_up_fills_only_a_genuinely_thin_day(monkeypatch):
    monkeypatch.setattr(models, "get_staff_availability", lambda r, *a, **k: [])
    rows = [_row(d, n, "4:00pm", "9:00pm", role="Server", hours=5.0) for d in WEEK[:6] for n in ("Ana", "Bob", "Cy")]
    rows.append(_row(WEEK[6], "Ana", "4:00pm", "9:00pm", role="Server", hours=5.0))     # Sunday has one server, not three
    targets = {d: 40.0 for d in WEEK}
    out, hours_added, dates = schedule_engine._top_up_hours_gap(
        list(rows), targets, hours_budget=280.0, hours_scheduled=95.0, restaurant_id=1,
        close_times={}, role_buffers={}, constraints=_plain_constraints())
    assert set(dates) == {WEEK[6]}
    added = [r for r in out if r["date"] == WEEK[6] and r not in rows]
    assert added and all("top-up" in r["notes"] for r in added)
    assert all(r["employee"] != "Ana" for r in added)     # never double-books


def test_top_up_honours_time_off_and_the_ceiling(monkeypatch):
    monkeypatch.setattr(models, "get_staff_availability", lambda r, *a, **k: [])
    rows = [_row(d, n, "4:00pm", "9:00pm", role="Server", hours=5.0) for d in WEEK[:6] for n in ("Ana", "Bob", "Cy")]
    rows.append(_row(WEEK[6], "Ana", "4:00pm", "9:00pm", role="Server", hours=5.0))
    c = _plain_constraints(blocked_dates={"bob": {WEEK[6]: sr.LABELS["approved_time_off"]}},
                           hours_limits={"cy": (None, 33)})
    out, hours_added, dates = schedule_engine._top_up_hours_gap(
        list(rows), {d: 40.0 for d in WEEK}, hours_budget=280.0, hours_scheduled=95.0, restaurant_id=1,
        close_times={}, role_buffers={}, constraints=c)
    assert dates == {}          # Bob is off, Cy would pass 33h: nobody legal, gap stays


# ── explanations ───────────────────────────────────────────────────────

def test_every_assignment_gets_an_explanation_built_only_from_facts_on_file():
    import shift_quality as sq
    rows = [_row(WEEK[5], "Ana", "4:00pm", "10:00pm", role="Bartender"),
            _row(WEEK[5], "Bob", "4:00pm", "10:00pm", role="Bartender")]
    ctxs = sq.build_contexts(rows, scores={"Ana": 5}, leader_flags={"Ana": True},
                             prior_pattern={"Ana": {"days": ["Saturday"], "dayparts": ["night"]}})
    ctx = ctxs[0]
    ana = sq.explain_assignment(rows[0], ctx)
    bob = sq.explain_assignment(rows[1], ctx)
    assert ana["employee"] == "Ana" and "Level 5" in ana["why"]
    assert "close" in ana["why"].lower() and "Saturday" in ana["why"]
    assert "Level" not in bob["why"]             # unrated says nothing about level
    assert bob["why"].endswith(".")               # but never empty
