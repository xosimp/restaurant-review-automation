"""Schedule fix round 10/10/26, workstream G1 — what the manager changes
after a week went out is classified and read (LEARNING-16): demand adds and
cuts feed the requirement residual, a likely call-off asks the one-tap why
at once, and the planned and as-run scores are read as plan adherence."""
from datetime import date, timedelta

import pytest

import models
import schedule_learning as sl
import schedule_memory as sm
import schedule_scorecard as ssc
import schedule_versions as sv
import staff_settings
from models import Restaurant, create_restaurant

HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    import client_api
    monkeypatch.setattr(client_api, "invalidate_insight_cache", lambda *a, **k: None)
    yield


def _rid():
    return create_restaurant(Restaurant(name="React Co", owner_email="rc@x.test", module_labor=1))


def _csv(rows):
    return HEAD + "".join(f"{d.isoformat()},{d.strftime('%A')},{n},Server,5:00pm,10:00pm,5,\n" for d, n in rows)


def _sent_week(rid, rows, published=True):
    d0 = rows[0][0]
    ws = d0 - timedelta(days=d0.weekday())
    conn = models.get_conn()
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at) "
                       "VALUES (?,?,?,?,datetime('now'))",
                       (rid, ws.isoformat(), (ws + timedelta(days=6)).isoformat(), _csv(rows))).lastrowid
    conn.commit()
    conn.close()
    sv.append(rid, hid, "generated", _csv(rows), saved_by="Cavnar AI", saved_authority="system")
    sv.append(rid, hid, "published", _csv(rows), saved_by="erik", saved_authority="principal")
    return hid


def _save(rid, hid, rows):
    conn = models.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        step = sv.step_origins(sv.latest_rows(conn, hid), sv.rows_from_csv(_csv(rows)))
        version = sv.write_on(conn, rid, hid, "edited", _csv(rows), saved_by="erik", saved_authority="principal",
                              row_origins=step["stored"])
        conn.commit()
    finally:
        conn.close()
    return sl.capture_save(rid, hid, version, step, user={"id": 1, "username": "erik", "role": "owner"},
                           published=True)


def test_each_reaction_has_a_class():
    assert sl.classify_reaction("removed", {"attendance": {"outcome": "called_out"}}) == "call_off_replacement"
    assert sl.classify_reaction("moved", {"attendance": {"outcome": "no_show"}}) == "call_off_replacement"
    assert sl.classify_reaction("added", {}) == "demand_add"
    assert sl.classify_reaction("removed", {}) == "demand_cut"
    assert sl.classify_reaction("moved", {}) == "personal_swap"
    assert sl.classify_reaction("retimed", {}) == "retime"


def test_a_server_added_after_the_week_went_out_twice_feeds_the_requirement():
    rid = _rid()
    for w in (3, 2):
        fri = date.today() - timedelta(days=(date.today().weekday() - 4) % 7) - timedelta(weeks=w)
        hid = _sent_week(rid, [(fri, "Ana"), (fri, "Bob")])
        _save(rid, hid, [(fri, "Ana"), (fri, "Bob"), (fri, "Cy")])       # busy: one more, after it went out
    obs = sm.observations(rid, kinds=("reaction",))
    assert obs and all((o["value"] or {}).get("class") == "demand_add" for o in obs)
    got = sl.reaction_headcount(rid)
    assert got[("Friday", "night")]["Server"]["delta"] == 1
    learned = sl.learned_headcount(rid)
    assert learned[("Friday", "night")]["Server"]["delta"] == 1
    assert learned[("Friday", "night")]["Server"]["source"] == "reactions"


def test_a_call_off_replacement_teaches_no_headcount():
    rid = _rid()
    for w in (3, 2):
        fri = date.today() - timedelta(days=(date.today().weekday() - 4) % 7) - timedelta(weeks=w)
        hid = _sent_week(rid, [(fri, "Ana"), (fri, "Bob")])
        conn = models.get_conn()
        conn.execute("INSERT INTO attendance_events (restaurant_id, employee_name, employee_key, business_date, "
                     "shift_start, outcome, source) VALUES (?,?,?,?,?,?,?)",
                     (rid, "Bob", "bob", fri.isoformat(), "5:00pm", "called_out", "manual"))
        conn.commit()
        conn.close()
        _save(rid, hid, [(fri, "Ana")])
    assert {(o["value"] or {}).get("class") for o in sm.observations(rid, kinds=("reaction",))} == \
        {"call_off_replacement"}
    assert sl.reaction_headcount(rid) == {}


def test_a_person_taken_off_tomorrow_is_asked_about_at_once():
    rid = _rid()
    for w in range(6, 1, -1):                          # well past the first weeks
        d = date.today() - timedelta(weeks=w)
        _sent_week(rid, [(d, "Ana")])
    tomorrow = date.today() + timedelta(days=1)
    hid = _sent_week(rid, [(tomorrow, "Ana"), (tomorrow, "Bob")])
    q = _save(rid, hid, [(tomorrow, "Ana")])["why_questions"]
    assert q and q[0]["employee"] == "Bob" and "call_off" in [o["answer"] for o in q[0]["options"]]


def test_the_planned_and_as_run_scores_are_read_as_plan_adherence():
    rid = _rid()
    mon = date.today() - timedelta(days=date.today().weekday()) - timedelta(weeks=3)
    hid = _sent_week(rid, [(mon, "Ana")])
    for kind, score in (("sq_planned", 80), ("sq_as_run", 70)):
        sm.observe(rid, kind, date=mon.isoformat(), daypart="night", history_id=hid, phase="as_run",
                   origin="system", authority="system", value={"score": score},
                   fact_key=f"{kind}|{hid}|{mon.isoformat()}|night")
    m = ssc.compute_week(rid, hid)
    assert m["plan_adherence"] == {"shifts": 1, "mean_delta": -10.0}
