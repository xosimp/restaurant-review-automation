"""An Operational Score for the role a shift is in, and an age (schedule audit
10/3/26 D-12).

One undated number per person: a 5 as Server counted the same when the
person was scheduled on Bar, and a rating from a year ago never faded or
asked to be looked at again.
"""
import datetime as dt

import pytest

import models
import schedule_rules as sr
import schedule_solver as solver
import shift_quality as sq

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid():
    return models.create_restaurant(models.Restaurant(name="Ratings Co", owner_email="ra@x.test", module_labor=1))


def _row(name, role, date="2026-10-07", start="5:00pm", end="10:00pm"):
    return {"date": date, "day": sq._day_name(date), "employee": name, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": "5", "notes": ""}


def test_a_score_for_one_role_is_kept_per_role_family():
    rid = _rid()
    models.set_capability(rid, "Ana", "overall", score=5)
    models.set_capability(rid, "Ana", "role:Bartender AM", score=2)
    assert models.get_operational_scores(rid) == {"Ana": 5}
    assert models.get_role_scores(rid) == {"Ana": {"bartender": 2}}
    # "Bartender PM" is the same role as "Bartender AM".
    models.set_capability(rid, "Ana", "role:Bartender PM", score=3)
    assert models.get_role_scores(rid) == {"Ana": {"bartender": 3}}
    with pytest.raises(models.CapabilityError):
        models.set_capability(rid, "Ana", "role:", score=3)


def test_the_scorer_judges_a_shift_on_the_score_for_its_role():
    sig = {"scores": {"Ana": 5, "Ben": 3}, "role_scores": {"Ana": {"bartender": 2}}}
    bar = sq.build_contexts([_row("Ana", "Bartender PM"), _row("Ben", "Server")], **sig)
    lunch = sq.build_contexts([_row("Ana", "Server", start="11:00am", end="3:00pm")], **sig)
    assert bar[0].scores["Ana"] == 2 and bar[0].scores["Ben"] == 3
    assert lunch[0].scores["Ana"] == 5          # no score for Server: the overall one


def test_the_solver_reads_the_score_for_the_role_too():
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(sr.DAYS), roster_names=["Ana", "Ben"],
                       active={"ana", "ben"})
    rows = [_row("Ana", "Bartender"), _row("Ben", "Server", date="2026-10-08")]
    P = solver.Problem(rows, c, {"roster": ["Ana", "Ben"], "scores": {"Ana": 5, "Ben": 3},
                                 "role_scores": {"Ana": {"bartender": 2}},
                                 "roster_roles": {"Ana": "Bartender", "Ben": "Server"}})
    ana = P.pidx["ana"]
    assert P._sc(ana, {"bartender"}) == 2
    assert P._sc(ana, {"server"}) == 5
    assert P._sc(P.pidx["ben"], {"bartender"}) == 3


def test_an_old_rating_asks_to_be_looked_at_again_and_keeps_counting(monkeypatch):
    rid = _rid()
    models.set_capability(rid, "Ana", "overall", score=4)
    models.set_capability(rid, "Ben", "overall", score=3)
    conn = models.get_conn()
    try:
        conn.execute("UPDATE staff_capabilities SET updated_at='2026-06-01 10:00:00' WHERE employee_name='Ana'")
        conn.commit()
    finally:
        conn.close()
    ages = models.rating_ages(rid, today=dt.date(2026, 10, 3))
    assert ages["Ana"]["due"] is True and ages["Ana"]["due_text"] == "Rated 6/1/26 — still right?"
    assert ages["Ben"]["due"] is False and ages["Ben"]["due_text"] is None
    assert models.get_operational_scores(rid)["Ana"] == 4       # never dropped silently
    monkeypatch.setattr(models, "_restaurant_today", lambda r: dt.date(2026, 10, 3))
    cov = models.capability_coverage(rid, ["Ana", "Ben"])
    assert cov["due_for_rerate"] == 1 and cov["due_for_rerate_names"] == ["Ana"]
