"""Schedule fix round 10/10/26, workstream J3 — MARKET-26: call-out
insurance. A standby suggestion becomes a consented, paid on-call slot,
and the coverage check calls that person the moment somebody is missing."""
import datetime as dt
import json

import pytest

import models
import standby_insurance as sb
import strategy_jobs
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import people
    monkeypatch.setattr(people, "tell", lambda *a, **k: "push")
    monkeypatch.setattr(people, "tell_deciders", lambda *a, **k: None)
    yield


TODAY = dt.date(2026, 10, 10)
FRI = dt.date(2026, 10, 16)
HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


def _rid():
    rid = create_restaurant(Restaurant(name="Standby Co", owner_email="s@x.test", module_labor=1, hourly_rate=15.0))
    for n in ("Ana", "Bob", "Cy"):
        models.add_manual_team_member(rid, n, role="Server")
    conn = models.get_conn()
    csv = HEAD + f"{FRI.isoformat()},Friday,Ana,Server,5:00pm,10:00pm,5,\n"
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at) "
                 "VALUES (?,?,?,?,datetime('now'))", (rid, (FRI - dt.timedelta(days=4)).isoformat(),
                                                       (FRI + dt.timedelta(days=2)).isoformat(), csv))
    conn.commit()
    conn.close()
    return rid


def test_the_standby_pay_is_the_owners_policy_and_says_when_it_is_the_default():
    rid = _rid()
    pay, basis = sb.standby_pay(rid, "Bob", "Server")
    assert pay == pytest.approx(sb.DEFAULT_PAY_HOURS * 15.0) and "the default" in basis
    sb.set_policy(rid, flat_pay=25, by="erik")
    pay, basis = sb.standby_pay(rid, "Bob", "Server")
    assert pay == 25.0 and "your standby policy" in basis
    with pytest.raises(sb.StandbyError):
        sb.set_policy(rid)


def test_an_offer_needs_somebody_free_in_the_role_and_a_yes_is_time_stamped_consent():
    rid = _rid()
    with pytest.raises(sb.StandbyError):
        sb.offer(rid, FRI.isoformat(), "Server", "Nobody", "5:00pm", "10:00pm", today=TODAY)
    slot = sb.offer(rid, FRI.isoformat(), "Server", "Bob", "5:00pm", "10:00pm", by="erik", today=TODAY)
    assert slot["status"] == "offered" and slot["via"] == "push"
    assert "I agree to be on call" in slot["terms"] and "10/16/26" in slot["terms"]
    with pytest.raises(sb.StandbyError):
        sb.offer(rid, FRI.isoformat(), "Server", "Bob", "5:00pm", "10:00pm", today=TODAY)
    with pytest.raises(sb.StandbyError):
        sb.respond(rid, slot["id"], "Bob", True)                      # no agreement ticked
    with pytest.raises(sb.StandbyError):
        sb.respond(rid, slot["id"], "Cy", True, agree=True)           # not theirs
    yes = sb.respond(rid, slot["id"], "Bob", True, agree=True, channel="portal")
    assert yes["status"] == "accepted" and yes["consent_at"] and yes["consent_text"] == slot["terms"]
    assert yes["consent_channel"] == "portal" and yes["consent_version"] == sb.CONSENT_VERSION
    mine = sb.for_staff(rid, "Bob", today=TODAY)
    assert mine[0]["status"] == "accepted" and "expected_cost" not in mine[0]


def test_the_coverage_check_calls_the_standby_the_moment_somebody_is_missing(monkeypatch):
    rid = _rid()
    slot = sb.offer(rid, FRI.isoformat(), "Server", "Bob", "5:00pm", "10:00pm", today=TODAY)
    sb.respond(rid, slot["id"], "Bob", True, agree=True)
    r = models.get_restaurant(rid)
    import intraday
    import issues
    import live_activities
    import staff_comms
    import time_utils
    local = dt.datetime.combine(FRI, dt.time(17, 30))
    monkeypatch.setattr(strategy_jobs, "_slot_iter", lambda *a, **k: iter([r]))
    monkeypatch.setattr(strategy_jobs, "_open_now", lambda *a, **k: True)
    conn = models.get_conn()
    cid = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) VALUES (?,?,?,1)",
                       (rid, "Erik", "+15555550100")).lastrowid
    conn.execute("INSERT INTO issue_routing (restaurant_id, role, contact_id) VALUES (?,?,?)", (rid, "manager", cid))
    conn.commit()
    conn.close()
    import notify
    monkeypatch.setattr(notify, "send_sms", lambda *a, **k: False)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: local)
    monkeypatch.setattr(time_utils, "business_date", lambda *a, **k: FRI)
    monkeypatch.setattr(live_activities, "note_coverage", lambda *a, **k: None)
    monkeypatch.setattr(staff_comms, "late_hold", lambda *a, **k: None)
    missing = {"employee": "Ana", "role": "Server", "shift_start": "5:00pm", "shift_end": "10:00pm", "minutes_late": 30}
    monkeypatch.setattr(intraday, "coverage_gaps", lambda *a, **k: {
        "available": True, "missing": [missing], "business_date": FRI.isoformat(), "scheduled_rows": [missing],
        "arrived_keys": [], "released": []})
    asked = []

    def _ask(restaurant_id, issue_id, name, **kw):
        asked.append((issue_id, name, kw.get("actor")))
        return {"ok": True, "via": "push", "name": name, "offer_id": 77}
    monkeypatch.setattr(intraday, "ask_to_cover", _ask)
    strategy_jobs.run_coverage_check()
    assert asked and asked[0][1] == "Bob" and "standby" in asked[0][2]
    iss = issues.get_issue(rid, asked[0][0])
    meta = json.loads(iss["meta_json"])
    assert meta["covers"][0]["name"] == "Bob" and meta["covers"][0]["standby"] is True
    s = sb.slots(rid)[0]
    assert s["status"] == "dispatched" and s["offer_id"] == 77 and s["issue_id"] == asked[0][0]
    # A second pass does not call them twice.
    strategy_jobs.run_coverage_check()
    assert len(asked) == 1


def test_after_the_night_a_standby_not_called_is_owed_the_standby_pay():
    rid = _rid()
    slot = sb.offer(rid, FRI.isoformat(), "Server", "Bob", "5:00pm", "10:00pm", today=TODAY)
    sb.respond(rid, slot["id"], "Bob", True, agree=True)
    assert sb.settle(rid, today=FRI + dt.timedelta(days=1)) == 1
    s = sb.slots(rid)[0]
    assert s["status"] == "released" and s["pay_owed"] == pytest.approx(sb.DEFAULT_PAY_HOURS * 15.0)
    assert sb.settle(rid, today=FRI + dt.timedelta(days=2)) == 0     # settled once


def test_withdrawing_after_a_yes_still_owes_the_pay():
    rid = _rid()
    slot = sb.offer(rid, FRI.isoformat(), "Server", "Bob", "5:00pm", "10:00pm", today=TODAY)
    sb.respond(rid, slot["id"], "Bob", True, agree=True)
    w = sb.withdraw(rid, slot["id"], by="erik")
    assert w["status"] == "withdrawn" and w["pay_owed"] == pytest.approx(30.0)


def test_a_short_nights_sales_cost_is_stated_only_from_enough_measured_nights():
    rid = _rid()
    conn = models.get_conn()
    # Twelve Fridays of sales ($4000); five of them a server short ($3400).
    for k in range(1, 13):
        d = FRI - dt.timedelta(weeks=k)
        short = k in (2, 4, 6, 8, 10)
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales) VALUES (?,?,?,?)",
                     (rid, d.isoformat(), "Friday", 3400.0 if short else 4000.0))
        if short:
            conn.execute("INSERT INTO attendance_events (restaurant_id, employee_name, employee_key, business_date, "
                         "shift_start, role, outcome, source) VALUES (?,?,?,?,?,?,?,?)",
                         (rid, "Ana", "ana", d.isoformat(), "5:00pm", "Server", "no_show", "coverage_check"))
    conn.commit()
    conn.close()
    c = sb.short_night_cost(rid, "Server", "Friday", today=TODAY)
    assert c["sales"] and c["sales"]["nights"] == 5 and c["sales"]["loss"] == pytest.approx(600, abs=1)
    assert sb.short_night_cost(rid, "Server", "Monday", today=TODAY)["sales"] is None
