"""Where the three halves of the Recommendation ROI work meet.

The outcome engine (outcomes.py), the ledger (rec_ledger.py) and the
coverage work were built in parallel; these tests pin the seams between
them: a tracker is tied to its recommendation the moment it starts, the
owner's check-in changes the result it is about, a disowned change gives its
day back to the next change in its family, one dish has one reprice key on
every surface, an Ask proposal's "Not now" takes the same reason codes as
every other, and the win push never claims a cause.
"""
import json
from datetime import date, timedelta

import pytest

import metrics
import models
import outcomes
import rec_ledger
from models import Restaurant, create_restaurant, get_conn

from tests.test_outcomes_roi import _evaluated_win, _insert, _rid


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, outcomes, metrics, rec_ledger):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(rec_ledger, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(outcomes, "_holidays_between", lambda s, e: {})


TODAY = date.today()


def _tracker_id(db_path, rid, key):
    c = get_conn(db_path)
    row = c.execute("SELECT tracker_id FROM rec_instances WHERE restaurant_id=? AND key=? "
                    "ORDER BY rec_id DESC LIMIT 1", (rid, key)).fetchone()
    c.close()
    return row["tracker_id"] if row else None


def test_a_tracker_is_linked_to_its_recommendation_the_moment_it_starts(db_path):
    rid = _rid(db_path)
    rec_ledger.present(rid, "trim_day:Monday", "labor", "home", title="Trim Monday", db_path=db_path)
    o = outcomes.record(rid, "home", "trim_day:Monday", "Trim Monday", "labor_pct", db_path=db_path)
    assert _tracker_id(db_path, rid, "trim_day:Monday") == o["id"]
    # A source key that is no recommendation links nothing and raises nothing.
    other = outcomes.record(rid, "slow_day_campaign", "campaign:Tuesday:2026-09-01", "Guest text",
                            "weekday_sales:Tuesday", db_path=db_path)
    assert other["id"]


def test_no_i_didnt_make_it_stops_the_result_counting_and_yes_brings_it_back(db_path):
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=60)
    win = _evaluated_win(db_path, rid, t0)
    assert win["counts"] and win["verdict"] == "improved"
    outcomes.accrue_due(rid, db_path=db_path)
    before = outcomes.cumulative(rid, db_path=db_path)
    assert before["total"] and before["total"] > 0

    off = outcomes.apply_checkin(rid, win["id"], "no", False, db_path=db_path)
    assert off["counts"] is False and outcomes.disowned(off)
    assert "you said this change wasn't made" in off["attribution_label"]
    assert outcomes.total_value(rid, db_path=db_path)["monthly"] == 0
    assert outcomes.cumulative(rid, db_path=db_path)["gained"] == 0
    assert outcomes.accrue_due(rid, db_path=db_path) == 0            # a disowned change accrues nothing

    back = outcomes.apply_checkin(rid, win["id"], "yes", False, db_path=db_path)
    assert back["counts"] is True and back["attribution"] == win["attribution"]
    outcomes.accrue_due(rid, db_path=db_path)
    assert outcomes.cumulative(rid, db_path=db_path)["total"] == before["total"]


def test_conditions_changed_caps_the_grade_and_says_why(db_path):
    rid = _rid(db_path)
    oid = _insert(db_path, rid, "Trim", "labor_pct", 400.0, "2026-06-01", attribution="consistent")
    r = outcomes.apply_checkin(rid, oid, "yes", True, db_path=db_path)
    # "Something else changed" takes the result out of Delivered as it takes
    # it out of learning (CA2 #7 — it used to keep counting here while
    # rec_learning read it as unknown).
    assert r["attribution"] == "associated" and r["counts"] is False
    assert "you said something else changed" in r["attribution_label"]
    # Answering again without the change restores the grade it had.
    r = outcomes.apply_checkin(rid, oid, "yes", False, db_path=db_path)
    assert r["attribution"] == "consistent" and r["counts"] is True


def test_another_restaurants_tracker_is_untouched(db_path):
    rid, other = _rid(db_path), _rid(db_path, name="Other")
    oid = _insert(db_path, other, "Trim", "labor_pct", 400.0, "2026-06-01")
    assert outcomes.apply_checkin(rid, oid, "no", False, db_path=db_path) is None
    assert outcomes.get_outcome(oid, db_path=db_path)["counts"] is True


def test_a_disowned_change_gives_its_day_to_the_next_change_in_its_family(db_path):
    rid = _rid(db_path)
    base = {"restaurant_id": rid, "status": "evaluated", "source_key": "k", "module": "inventory"}
    waste = dict(base, id=_insert(db_path, rid, "Waste", "weekly_waste", 300.0, "2026-06-01", module="inventory"),
                 metric="weekly_waste", verdict="improved")
    food = dict(base, id=_insert(db_path, rid, "Food", "food_cost_pct", 400.0, "2026-06-01", module="inventory"),
                metric="food_cost_pct", verdict="improved")
    conn = get_conn(db_path)
    outcomes._put_day(conn, waste, "2026-06-01", 10.0, True, "window")
    outcomes._put_day(conn, food, "2026-06-01", 13.0, True, "window")    # the counted one
    conn.commit()
    conn.close()
    assert outcomes.cumulative(rid, db_path=db_path)["gained"] == 13.0
    outcomes.apply_checkin(rid, food["id"], "no", False, db_path=db_path)
    assert outcomes.cumulative(rid, db_path=db_path)["gained"] == 10.0   # waste's day now counts


def test_the_checkin_route_applies_the_answer_to_the_result(db_path, monkeypatch):
    import auth
    from flask import Flask
    from strategy_routes import strategy_bp
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    rid = _rid(db_path)
    oid = _insert(db_path, rid, "Trim", "labor_pct", 400.0, "2026-06-01", source="home")
    rec_ledger.present(rid, "home:trim", "labor", "home", title="Trim", db_path=db_path)
    rec_ledger.link_tracker(rid, "home:trim", oid, db_path=db_path)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": rid, "role": "client",
                                                           "is_admin": 0, "username": "o", "email": "o@x"})
    app = Flask(__name__)
    app.register_blueprint(strategy_bp)
    resp = app.test_client().post("/api/recs/checkin", json={"key": "home:trim", "did_it": "no",
                                                             "conditions_changed": False})
    assert resp.status_code == 200, resp.get_json()
    assert outcomes.get_outcome(oid, db_path=db_path)["counts"] is False


def test_one_dish_has_one_reprice_key_on_every_surface():
    import menu_intelligence
    assert menu_intelligence.reprice_key("  Carbonara ") == menu_intelligence.reprice_key("Carbonara")
    import inspect
    import action_queue
    import home_brief
    import reporter
    for mod in (action_queue, home_brief, reporter):
        assert 'f"reprice:{' not in inspect.getsource(mod), mod.__name__


def test_ask_not_now_takes_the_same_reason_codes(db_path, monkeypatch):
    import client_api
    rid = _rid(db_path)
    bad = client_api._do_record_ask_action(rid, 1, {"action": "send_order", "outcome": "dismissed",
                                                     "reason_code": "because"})
    assert bad[1] == 400
    ok = client_api._do_record_ask_action(rid, 1, {"action": "send_order", "outcome": "dismissed",
                                                    "reason_code": "too_costly"})
    assert ok[1] == 200


def test_the_win_push_names_what_moved_never_a_cause(db_path, monkeypatch):
    import strategy_jobs
    sent = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda rid, kind, title, body, *a, **k: sent.append((title, body)) or 1)
    rid = _rid(db_path)
    # A result read alongside another change on the same number is not a
    # win at all (re-audit B2 #5): it counts neither way, and is not announced.
    tangled = _insert(db_path, rid, "Trim", "labor_pct", 400.0, "2026-06-01", attribution="associated",
                      concurrent=json.dumps([{"kind": "price_change", "label": "a menu price change",
                                              "date": "2026-06-05"}]))
    assert outcomes.get_outcome(tangled, db_path=db_path)["counts"] is False
    assert strategy_jobs._tell_owners_what_worked([outcomes.get_outcome(tangled, db_path=db_path)], db_path) == 0
    oid = _insert(db_path, rid, "Trim", "labor_pct", 400.0, "2026-07-01", attribution="associated")
    row = outcomes.get_outcome(oid, db_path=db_path)
    assert strategy_jobs._tell_owners_what_worked([row], db_path) == 1
    title, body = sent[0]
    assert "worked" not in title.lower() and title.startswith("Labor %")
    # A disowned result is not announced at all.
    outcomes.apply_checkin(rid, oid, "no", False, db_path=db_path)
    sent.clear()
    assert strategy_jobs._tell_owners_what_worked([outcomes.get_outcome(oid, db_path=db_path)], db_path) == 0


def test_outcomes_can_be_asked_for_by_id_and_name_their_checkin_key(db_path, monkeypatch):
    import auth
    from flask import Flask
    from strategy_routes import strategy_bp
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    rid = _rid(db_path)
    old = _insert(db_path, rid, "Old trim", "labor_pct", 100.0, "2025-06-01", source="home")
    for i in range(60):                                   # push the old one past the newest 50
        _insert(db_path, rid, f"Other {i}", "labor_pct", 10.0, "2026-06-01", source=f"manual{i}")
    rec_ledger.present(rid, "home:old trim", "labor", "home", title="Old trim", db_path=db_path)
    rec_ledger.link_tracker(rid, "home:old trim", old, db_path=db_path)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": rid, "role": "client",
                                                           "is_admin": 0, "username": "o", "email": "o@x"})
    app = Flask(__name__)
    app.register_blueprint(strategy_bp)
    c = app.test_client()
    newest = c.get("/api/outcomes").get_json()["outcomes"]
    assert old not in [r["id"] for r in newest]
    asked = c.get(f"/api/outcomes?ids={old}").get_json()["outcomes"]
    assert [r["id"] for r in asked] == [old] and asked[0]["checkin_key"] == "home:old trim"
    # A tracker with no recommendation behind it offers no check-in.
    assert all(r["checkin_key"] is None for r in newest)
    assert c.get("/api/outcomes?ids=1,x").status_code == 400
    # Another restaurant's id is simply not returned.
    other = _rid(db_path, name="Other Co")
    theirs = _insert(db_path, other, "Theirs", "labor_pct", 10.0, "2026-06-01")
    assert c.get(f"/api/outcomes?ids={theirs}").get_json()["outcomes"] == []
