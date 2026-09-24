"""NS5 (9/24/26) — can Cavnar publish or suggest a schedule that breaks the
rules? Each test replays one of the audit's probes (scratchpad/ns5/) and
failed before its fix.

H2 notice_short · H3 publish gate recomputed at send time · H4 minor age
bands · M7 soft flags on an unattended publish · M8 coverage floor and
keyholder until close · L13 pack wording · L14 a midnight close.
"""
import datetime as dt
import json

import pytest

import auth
import client_api
import delayed
import labor
import mobile_api
import models
import schedule_engine
import schedule_rules as sr
import schedule_versions as sv
import staff_settings as ss
import time_off
from models import create_restaurant, Restaurant, get_conn, update_restaurant, get_restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
# A school-year week (well after Labor Day).
WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn
    import staff_routes
    import intraday
    import strategy_jobs
    import labor_replacements
    for mod in (models, auth, client_api, mobile_api, labor, schedule_engine, sr, sv, ss, time_off,
                staff_routes, delayed, intraday, strategy_jobs):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    rid = create_restaurant(Restaurant(name="Safety Co", owner_email="s@x.com"), db_path=db_path)
    for n, role in (("Ana", "Server"), ("Bob", "Server"), ("Kid", "Host"), ("Cook1", "Cook"), ("Mgr", "Manager")):
        models.add_manual_team_member(rid, n, role=role, db_path=db_path)
    return rid


def _history(db_path, rid, csv_text, review=None, week=WEEK, published=False):
    conn = get_conn(db_path)
    models._ensure_history_columns(conn)
    cur = conn.execute(
        "INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, labor_target, "
        "schedule_csv, summary_json, quality_json, review_json, published_at) VALUES (?,?,?,?,?,?,?,'[]',NULL,?,?)",
        (rid, week[0], week[-1], 12, 400, 30, csv_text, json.dumps(review) if review else None,
         "2026-10-01 10:00:00" if published else None))
    conn.commit()
    hid = cur.lastrowid
    conn.close()
    return hid


def _sweep(rid, csv_text, week=WEEK, days=DAYS):
    c = sr.build_constraints(rid, week, days)
    return sr.violations(sv.rows_from_csv(csv_text), c)


def _kinds(v):
    return [(x["kind"], x["hard"]) for x in v]


# ── H4: minors by age band ─────────────────────────────────────────────

MINOR_WEEK = HEADER + (f"{WEEK[1]},Tuesday,Kid,Host,4:00pm,10:00pm,6.0,\n"
                       f"{WEEK[2]},Wednesday,Kid,Host,6:00am,2:00pm,8.0,\n"
                       f"{WEEK[3]},Thursday,Kid,Host,4:00pm,10:00pm,6.0,\n"
                       f"{WEEK[4]},Friday,Kid,Host,2:00pm,10:00pm,8.0,\n"
                       f"{WEEK[5]},Saturday,Kid,Host,2:00pm,9:00pm,7.0,\n")


def test_a_14_15_year_old_in_a_school_week_is_held_to_the_federal_floor(db_path, rid):
    ss.upsert(rid, "Kid", minor_age_band="14-15", db_path=db_path)
    v = _sweep(rid, MINOR_WEEK)
    hard = {(x["kind"], x["date"]) for x in v if x["hard"]}
    assert ("minor_early", WEEK[2]) in hard                     # 6am start, floor is 7am
    assert ("minor_late", WEEK[1]) in hard                      # 10pm on a school night, floor is 7pm
    assert any(k == "minor_hours" and d == WEEK[1] for k, d in hard)   # 6h on a school day, cap 3h
    assert any(k == "minor_week_hours" for k, _d in hard)       # 35h in a school week, cap 18h
    assert all(x["hard"] for x in v if x["kind"].startswith("minor_") and x["kind"] != "minor_age_unknown")


def test_a_16_17_year_old_keeps_the_owner_minor_rule_and_nothing_federal(db_path, rid):
    ss.upsert(rid, "Kid", minor_age_band="16-17", db_path=db_path)
    kinds = {x["kind"] for x in _sweep(rid, MINOR_WEEK)}
    assert "minor_early" not in kinds and "minor_week_hours" not in kinds
    assert "minor_age_unknown" not in kinds


def test_a_minor_with_no_band_is_asked_for_one(db_path, rid):
    ss.upsert(rid, "Kid", is_minor=True, db_path=db_path)
    v = _sweep(rid, MINOR_WEEK)
    unknown = [x for x in v if x["kind"] == "minor_age_unknown"]
    assert len(unknown) == 1 and not unknown[0]["hard"]


def test_summer_moves_the_14_15_curfew_to_9pm(db_path, rid):
    ss.upsert(rid, "Kid", minor_age_band="14-15", db_path=db_path)
    summer = ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-11", "2026-07-12"]
    csv_text = HEADER + f"{summer[0]},Monday,Kid,Host,1:00pm,8:30pm,7.5,\n"
    kinds = {x["kind"] for x in _sweep(rid, csv_text, week=summer)}
    assert "minor_late" not in kinds and "minor_hours" not in kinds
    csv_text = HEADER + f"{summer[0]},Monday,Kid,Host,1:00pm,9:30pm,8.0,\n"
    assert "minor_late" in {x["kind"] for x in _sweep(rid, csv_text, week=summer)}


def test_the_age_band_round_trips_and_clears_with_is_minor(db_path, rid):
    row = ss.upsert(rid, "Kid", minor_age_band="14–15", db_path=db_path)   # an en dash, as typed
    assert row["minor_age_band"] == "14-15" and row["is_minor"] is True
    row = ss.upsert(rid, "Kid", is_minor=False, db_path=db_path)
    assert row["minor_age_band"] is None and row["is_minor"] is False
    with pytest.raises(ss.StaffSettingsError):
        ss.upsert(rid, "Kid", minor_age_band="12-13", db_path=db_path)


def test_the_prompt_states_minor_limits_as_checked_starting_values(db_path, rid):
    ss.upsert(rid, "Kid", minor_age_band="14-15", db_path=db_path)
    block = sr.prompt_block(sr.build_constraints(rid, WEEK, DAYS))
    assert "MINORS (Kid): no later than" not in block
    assert "starting values, not legal advice" in block
    assert "Kid (age 14-15): start no earlier than 7:00am" in block


# ── L13: pack values are starting values ───────────────────────────────

def test_a_pack_is_starting_values_never_rules_that_apply(db_path, rid):
    update_restaurant(rid, {"jurisdiction": "IL"}, db_path=db_path)
    block = sr.prompt_block(sr.build_constraints(rid, WEEK, DAYS))
    assert "rules apply" not in block
    assert "Starting values from the Illinois pack" in block
    pack = sr.compliance(get_restaurant(rid, db_path))["_pack"]
    assert any("Chicago ordinance" in n for n in pack["notes"])


# ── L14: a midnight close ──────────────────────────────────────────────

def test_stays_after_close_fires_for_a_midnight_close(db_path, rid):
    update_restaurant(rid, {"close_times_json": json.dumps({"Monday": "12:00am"}),
                            "role_close_min_json": json.dumps({"Bartender": 60})}, db_path=db_path)
    csv_text = HEADER + f"{WEEK[0]},Monday,Ana,Bartender,4:00pm,9:00pm,5.0,\n"
    v = [x for x in _sweep(rid, csv_text) if x["kind"] == "ends_before_role_close"]
    assert v and "1:00am" in v[0]["detail"]
    ok = HEADER + f"{WEEK[0]},Monday,Ana,Bartender,6:00pm,1:00am,7.0,\n"
    assert not [x for x in _sweep(rid, ok) if x["kind"] == "ends_before_role_close"]


# ── M8: floors and keyholder cover are hard ────────────────────────────

def test_a_role_floor_is_a_hard_rule(db_path, rid):
    sr.save_role_floors(rid, {"Server": {"night": 3}}, db_path=db_path)
    csv_text = HEADER + f"{WEEK[5]},Saturday,Ana,Server,4:00pm,10:00pm,6.0,\n"
    v = [x for x in _sweep(rid, csv_text) if x["kind"] == "coverage_floor"]
    assert v and v[0]["hard"] and "your floor is 3" in v[0]["detail"]
    hid = _history(db_path, rid, csv_text)
    assert any("floor is 3" in b for b in client_api.publish_blockers(rid, hid))


def test_a_keyholder_stays_until_close_by_default(db_path, rid):
    ss.upsert(rid, "Mgr", certifications=["keyholder"], db_path=db_path)
    csv_text = HEADER + (f"{WEEK[5]},Saturday,Mgr,Manager,2:00pm,8:00pm,6.0,\n"
                         f"{WEEK[5]},Saturday,Cook1,Cook,4:00pm,11:30pm,7.5,\n")
    v = [x for x in _sweep(rid, csv_text) if x["kind"] == "keyholder_until_close"]
    assert v and v[0]["hard"]
    ok = csv_text.replace("2:00pm,8:00pm,6.0", "4:00pm,11:30pm,7.5")
    assert not [x for x in _sweep(rid, ok) if x["kind"] == "keyholder_until_close"]
    sr.save_compliance(rid, {"keyholder_until_close": False}, db_path=db_path)
    assert not [x for x in _sweep(rid, csv_text) if x["kind"] == "keyholder_until_close"]


def test_nobody_on_at_close_is_hard_when_the_close_is_known(db_path, rid):
    update_restaurant(rid, {"close_times_json": json.dumps({"Saturday": "11:00pm"})}, db_path=db_path)
    csv_text = HEADER + f"{WEEK[5]},Saturday,Cook1,Cook,2:00pm,8:00pm,6.0,\n"
    v = [x for x in _sweep(rid, csv_text) if x["kind"] == "nobody_at_close"]
    assert v and v[0]["hard"]


# ── H3: the gate is computed against today's data ──────────────────────

CLEAN = HEADER + (f"{WEEK[4]},Friday,Ana,Server,4:00pm,10:00pm,6.0,\n"
                  f"{WEEK[4]},Friday,Mgr,Manager,4:00pm,11:00pm,7.0,\n")


def test_time_off_approved_after_the_draft_blocks_the_publish(db_path, rid):
    """Probe P4: the draft was checked clean; Ana's time off was approved
    afterwards; the gate still read the saved review and returned []."""
    v = _sweep(rid, CLEAN)
    hid = _history(db_path, rid, CLEAN, review=sr.summarize(v))
    assert client_api.publish_blockers(rid, hid) == []
    row, _err = time_off.request_time_off(rid, "Ana", WEEK[4], WEEK[4], reason="wedding", db_path=db_path)
    time_off.decide(rid, row["id"], True, decided_by="owner", db_path=db_path)
    b = client_api.publish_blockers(rid, hid)
    assert any("Ana" in x and "approved time off" in x for x in b), b


def test_a_delayed_publish_is_held_by_a_blocker_that_appeared_after_it_was_queued(db_path, rid, monkeypatch):
    hid = _history(db_path, rid, CLEAN)
    sent = []
    import emails
    monkeypatch.setattr(emails, "send_staff_schedule_email", lambda **kw: sent.append(kw["employee_name"]))
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    told = []
    import strategy_jobs
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: told.append(a[1]))
    act = delayed.schedule(rid, "schedule_publish", {"schedule_id": hid, "manual": True, "acknowledge": []}, 5,
                           db_path=db_path)
    row, _err = time_off.request_time_off(rid, "Ana", WEEK[4], WEEK[4], reason="wedding", db_path=db_path)
    time_off.decide(rid, row["id"], True, decided_by="owner", db_path=db_path)
    delayed.run_due(db_path=db_path, now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10))
    assert sent == [] and told[-1] == "schedule_publish_held"
    conn = get_conn(db_path)
    st = conn.execute("SELECT status FROM delayed_actions WHERE id=?", (act["id"],)).fetchone()["status"]
    conn.close()
    assert st == "failed"


def test_an_acknowledgement_covers_only_the_blockers_it_was_given_for(db_path, rid, monkeypatch):
    flagged = CLEAN.replace("6.0,\n", "6.0,NEEDS REVIEW: off roster\n", 1)
    hid = _history(db_path, rid, flagged)
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    keys = [b["key"] for b in client_api.publish_review(rid, hid)["blockers"]]
    row, _err = time_off.request_time_off(rid, "Mgr", WEEK[4], WEEK[4], reason="x", db_path=db_path)
    time_off.decide(rid, row["id"], True, decided_by="owner", db_path=db_path)
    out, status = client_api._publish_schedule(rid, hid, delayed.AUTOMATION_ACTOR, acknowledge=keys)
    assert status == 409 and any("Mgr" in b for b in out["new_blockers"])


# ── H2: the notice rule ────────────────────────────────────────────────

def test_notice_short_blocks_a_week_inside_the_notice_window(db_path, rid):
    sr.save_compliance(rid, {"notice_days": 14}, db_path=db_path)
    hid = _history(db_path, rid, CLEAN)
    rv = client_api.publish_review(rid, hid, today=dt.date(2026, 10, 1))          # 4 days out
    assert [b["key"] for b in rv["blockers"]] == ["notice_short"]
    assert "14-day schedule notice rule" in rv["blockers"][0]["text"] and "10/5/26" in rv["blockers"][0]["text"]
    assert client_api.publish_review(rid, hid, today=dt.date(2026, 9, 20))["blockers"] == []   # 15 days out


def test_auto_publish_never_sends_inside_the_notice_window(db_path, rid, monkeypatch):
    import scheduler, strategy_jobs, time_utils
    update_restaurant(rid, {"auto_publish_schedule": 1, "billing_status": "active"}, db_path=db_path)
    sr.save_compliance(rid, {"notice_days": 14}, db_path=db_path)
    _history(db_path, rid, CLEAN)
    friday = dt.datetime(2026, 10, 2, 9, 30)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: friday)
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda *a, **k: friday)
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    monkeypatch.setattr(models, "schedule_publish_trust", lambda *a, **k: 99)
    reached = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: reached.append((a[1], a[3])))
    monkeypatch.setattr(models, "get_all_restaurants", lambda *a, **k: [get_restaurant(rid, db_path)])
    out = scheduler.run_auto_publish_schedules()
    assert out["queued"] == 0 and reached[0][0] == "schedule_publish_held"
    assert "notice rule" in reached[0][1]


def test_an_edit_inside_the_notice_window_warns_of_predictability_pay():
    comp = {"notice_days": 14}
    w = sr.late_change_warning(comp, [WEEK[0], WEEK[1]], dt.date(2026, 10, 1))
    assert w.startswith("2 days of shifts changed inside your 14-day notice window")
    assert "may owe" in w and "check with counsel" in w
    assert sr.late_change_warning({"notice_days": None}, [WEEK[0]], dt.date(2026, 10, 1)) is None
    assert sr.late_change_warning(comp, [WEEK[0]], dt.date(2026, 9, 1)) is None


# ── M7: soft flags on an unattended publish ────────────────────────────

def test_soft_flags_a_person_decides_hold_the_unattended_publish_only(db_path, rid):
    sr.save_compliance(rid, {"meal_break_after_hours": 6}, db_path=db_path)
    long_day = HEADER + (f"{WEEK[2]},Wednesday,Bob,Server,10:00am,9:30pm,11.5,\n"
                         f"{WEEK[2]},Wednesday,Mgr,Manager,10:00am,10:00pm,12.0,\n")
    hid = _history(db_path, rid, long_day)
    assert client_api.publish_blockers(rid, hid) == []                 # a person may send it
    held = client_api.publish_blockers(rid, hid, unattended=True)
    assert held and all("meal break" in b for b in held)
