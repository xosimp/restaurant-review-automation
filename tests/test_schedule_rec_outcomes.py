"""An accepted schedule recommendation is measured: "Fill the gap on Friday
night" against what that Friday night recorded, and "Trim about Nh" against
labor % (outcomes)."""
import sys

import pytest

import models
import rec_ledger
import schedule_intel as si
from models import create_restaurant, Restaurant


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
    return db_path


def _events(db, rid):
    c = models.get_conn(db)
    rows = c.execute("SELECT event, meta FROM rec_events WHERE restaurant_id=? ORDER BY id", (rid,)).fetchall()
    c.close()
    return [r["event"] for r in rows], rows


def test_a_friday_after_acceptance_with_no_issues_is_improved_once(db):
    import datetime as dt
    rid = create_restaurant(Restaurant(name="Gap Co", owner_email="g@x.com"), db_path=db)
    text = "Fill the gap on Friday night: nobody on Bartender."
    si.record_recommendation(rid, "coverage", text, "accepted")
    rec_ledger.record(rid, si.schedule_rec_key("coverage", text), "accepted")
    fri = dt.date.today() + dt.timedelta(days=(4 - dt.date.today().weekday()) % 7 or 7)
    c = models.get_conn(db)
    hid = c.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end) VALUES (?,?,?)",
                    (rid, (fri - dt.timedelta(days=4)).isoformat(), (fri + dt.timedelta(days=2)).isoformat())).lastrowid
    c.execute("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, issues) VALUES (?,?,?,?,?)",
              (rid, hid, fri.isoformat(), "night", 0))
    c.commit(); c.close()
    later = fri + dt.timedelta(days=5)
    assert si.measure_accepted_recommendations(rid, db_path=db, today=later) == 1
    assert si.measure_accepted_recommendations(rid, db_path=db, today=later) == 0      # idempotent
    events, rows = _events(db, rid)
    assert events[-1] == "outcome" and '"improved"' in rows[-1]["meta"]


def test_a_week_not_over_yet_is_not_measured(db):
    import datetime as dt
    rid = create_restaurant(Restaurant(name="Gap Co", owner_email="g@x.com"), db_path=db)
    text = "Fill the gap on Friday night: nobody on Bartender."
    si.record_recommendation(rid, "coverage", text, "accepted")
    fri = dt.date.today() + dt.timedelta(days=(4 - dt.date.today().weekday()) % 7 or 7)
    c = models.get_conn(db)
    hid = c.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end) VALUES (?,?,?)",
                    (rid, (fri - dt.timedelta(days=4)).isoformat(), (fri + dt.timedelta(days=2)).isoformat())).lastrowid
    c.execute("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, issues) VALUES (?,?,?,?,?)",
              (rid, hid, fri.isoformat(), "night", 2))
    c.commit(); c.close()
    assert si.measure_accepted_recommendations(rid, db_path=db, today=fri) == 0


def test_labor_waiting_names_close_requests_and_an_unsent_draft(db):
    import datetime as dt
    import strategy_jobs
    rid = create_restaurant(Restaurant(name="Wait Co", owner_email="w@x.com"), db_path=db)
    today = dt.date(2026, 10, 1)
    c = models.get_conn(db)
    c.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date) VALUES (?,?,?,?)",
              (rid, "Ana", "2026-10-02", "2026-10-03"))
    c.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date) VALUES (?,?,?,?)",
              (rid, "Far", "2026-10-20", "2026-10-21"))                       # not close: no line
    c.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end) VALUES (?,?,?)",
              (rid, "2026-10-04", "2026-10-10"))
    c.commit(); c.close()
    w = strategy_jobs.labor_waiting(rid, db_path=db, today=today)
    text = " ".join(w["lines"])
    assert "Ana's time off from 10/2/26" in text and "Far" not in text
    assert "week of 10/4/26 is drafted" in text and w["history_id"]
    assert not strategy_jobs.labor_waiting(rid, db_path=db, today=today, draft=False)["history_id"]


def test_a_time_off_request_tells_the_managers(db, monkeypatch):
    import shift_requests, time_off
    told = []
    monkeypatch.setattr(shift_requests, "_tell_managers", lambda rid, title, body, dbp: told.append((title, body)))
    rid = create_restaurant(Restaurant(name="Ask Co", owner_email="a@x.com"), db_path=db)
    row, err = time_off.request_time_off(rid, "Ana", "2026-10-02", "2026-10-02", db_path=db,
                                         today=__import__("datetime").date(2026, 10, 1))
    assert row and not err
    assert told and told[0][0] == "Time off request" and "Ana asked for 10/2/26 off" in told[0][1]


def test_one_review_by_id_is_scoped_to_its_restaurant(db):
    a = create_restaurant(Restaurant(name="A", owner_email="a@x.com"), db_path=db)
    b = create_restaurant(Restaurant(name="B", owner_email="b@x.com"), db_path=db)
    c = models.get_conn(db)
    rid_a = c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                      "fetched_at) VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))",
                      (a, "google", "x1", "Ana", 2, "Cold food")).lastrowid
    c.commit(); c.close()
    assert [r["id"] for r in models.get_reviews_data(a, review_id=rid_a)] == [rid_a]
    assert models.get_reviews_data(b, review_id=rid_a) == []
