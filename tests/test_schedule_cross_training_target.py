"""#29 — the cross-training target is per role and the owner's to set.

It was one code constant (a third of every shift able to cover a second
station) for every role in every restaurant. It is now stored on the
restaurant per role (role_cross_training_json, the four touch points),
edited with the other per-role rules on web and iOS, read by the scorer,
and a role left unset takes a sensible default for that role.
"""
import pytest

import models
import schedule_rules as sr
import shift_quality as sq
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

SAT = "2026-10-10"


def _row(emp, role):
    return {"date": SAT, "day": "Saturday", "employee": emp, "role": role, "shift_start": "4:00pm",
            "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}


def _cross(out):
    return next(d for d in out["shifts"][0]["dimensions"] if d["key"] == "cross_training")


ROWS = [_row("Ana", "Server"), _row("Bob", "Server"), _row("Cy", "Server"),
        _row("Dee", "Dishwasher"), _row("Eve", "Dishwasher")]
FLEX = {"Ana": ["Server", "Bartender"]}
KW = dict(profiles=[sq.ShiftProfile()], typical_headcount={("Saturday", "night"): {"Server": 3, "Dishwasher": 2}},
          cross_trained=FLEX)


def test_each_role_has_a_sensible_default_target():
    assert sq.cross_training_target_for("Bartender") == 0.5
    assert sq.cross_training_target_for("Line Cook") == sq.CROSS_TRAINING_DEFAULTS["cook"]
    assert sq.cross_training_target_for("Dishwasher") < sq.cross_training_target_for("Server")
    assert sq.cross_training_target_for("Sommelier") == sq.CROSS_TRAINING_DEFAULT


def test_the_owners_number_wins_and_zero_means_not_judged():
    assert sq.cross_training_target_for("Server", {"server": 0.6}) == 0.6
    assert sq.cross_training_target_for("Server", {"Server": 0.0}) == 0.0
    base = _cross(sq.score_rows(ROWS, **KW))
    # the dish crew is judged against its own (lower) bar, per role
    assert set(base["facts"]["by_role"]) == {"Server", "Dishwasher"}
    assert base["facts"]["by_role"]["Server"]["target"] == 0.34
    # the owner says dishwashers are not expected to flex: only servers count
    out = _cross(sq.score_rows(ROWS, cross_training_targets={"dishwasher": 0.0}, **KW))
    assert set(out["facts"]["by_role"]) == {"Server"}
    assert out["score"] > base["score"]


def test_a_higher_owner_target_scores_the_same_floor_lower():
    easy = _cross(sq.score_rows(ROWS, cross_training_targets={"server": 0.2, "dishwasher": 0.0}, **KW))
    hard = _cross(sq.score_rows(ROWS, cross_training_targets={"server": 1.0, "dishwasher": 0.0}, **KW))
    assert easy["score"] == 100 and hard["score"] == 33
    assert any("the target is 100%" in w for w in hard["weaknesses"])


def test_the_stored_percents_become_shares():
    r = Restaurant(name="x", owner_email="x@x.com", role_cross_training_json='{"Server": 40, "Host": "bad", "Cook": 150}')
    assert sr.role_cross_training(r) == {"server": 0.4, "cook": 1.0}
    assert sr.role_cross_training(Restaurant(name="y", owner_email="y@y.com")) == {}


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    return create_restaurant(Restaurant(name="Cross Co", owner_email="c@x.com"), db_path=db_path)


def test_the_column_round_trips_through_update_restaurant(rid, db_path):
    update_restaurant(rid, {"role_cross_training_json": '{"Server": 45}'}, db_path=db_path)
    assert get_restaurant(rid, db_path).role_cross_training_json == '{"Server": 45}'


def test_the_rules_route_saves_and_returns_the_targets(rid, monkeypatch):
    import strategy_routes
    owner = {"id": 1, "restaurant_id": rid, "role": "client", "is_admin": False, "username": "o"}
    monkeypatch.setattr(strategy_routes, "_body", lambda: {"role_cross_training": {"Server": "45", "Host": 0, "Cook": ""}})
    payload, status = strategy_routes._do_compliance_set(owner)
    assert status == 200 and payload["role_cross_training"] == {"Server": 45, "Host": 0}
    payload, status = strategy_routes._do_compliance_get(owner)
    assert status == 200
    assert payload["role_cross_training"] == {"Server": 45, "Host": 0}
    assert payload["cross_training_default"] == 34
    monkeypatch.setattr(strategy_routes, "_body", lambda: {"role_cross_training": {"Server": "lots"}})
    payload, status = strategy_routes._do_compliance_set(owner)
    assert status == 400 and "percent" in payload["error"]


def test_the_quality_signals_carry_the_owners_targets(rid, db_path):
    import schedule_engine as se
    update_restaurant(rid, {"role_cross_training_json": '{"Server": 50}'}, db_path=db_path)
    sig, _w = se._quality_signals(rid, {})
    assert sig["cross_training_targets"] == {"server": 0.5}
