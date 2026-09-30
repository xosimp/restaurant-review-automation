"""Ask's "which target applied then" (Will, 9/29/26): change_log.value_as_of
got its first reader, read_target_history. A date is the restaurant's own
day — changed_at is UTC, so a 9pm Central change belongs to that evening's
date, not the next one — and a target a login may not see stays out."""
import json

import pytest

import ask_cavnar
import ask_cavnar_tools as tools
import change_log
import models
from models import Restaurant, create_restaurant, get_restaurant

OWNER = {"id": 1, "role": "client", "is_admin": 0, "username": "erik"}
MANAGER = {"id": 2, "role": "manager", "is_admin": 0, "username": "dana"}


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    import sys
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid():
    return create_restaurant(Restaurant(name="Target Co", owner_email="t@x.test", timezone="America/Chicago",
                                        module_labor=1, module_inventory=1, food_cost_target=28.0,
                                        labor_target_pct=30.0))


def _change(rid, field, old, new, at):
    c = models.get_conn()
    c.execute("INSERT INTO change_log (restaurant_id, kind, entity, field, old_value, new_value, source, changed_at) "
              "VALUES (?,?,?,?,?,?,?,?)", (rid, "target", "restaurant", field, json.dumps(old), json.dumps(new),
                                           "owner", at))
    c.commit()
    c.close()


def _run(rid, args, user):
    view = tools.viewer_restaurant(get_restaurant(rid), user)
    return json.loads(tools.run_read_tool("read_target_history", rid, args, restaurant=view))


def test_a_date_is_the_restaurants_day_not_utcs():
    rid = _rid()
    # 8/10 02:30 UTC is 8/9 9:30pm in Chicago: the change belongs to 8/9.
    _change(rid, "food_cost_target", 30.0, 28.0, "2026-08-10 02:30:00")
    assert change_log.value_as_of(rid, "food_cost_target", "2026-08-09")["value"] == 28.0
    assert change_log.value_as_of(rid, "food_cost_target", "2026-08-08")["value"] == 30.0


def test_the_tool_answers_which_target_applied_then_and_lists_the_changes():
    rid = _rid()
    _change(rid, "food_cost_target", 30.0, 29.0, "2026-07-01 15:00:00")
    _change(rid, "food_cost_target", 29.0, 28.0, "2026-09-01 15:00:00")
    out = _run(rid, {"as_of": "2026-08-15"}, OWNER)
    food = next(t for t in out["applied"] if t["label"] == "Food cost target")
    assert food["shown"] == "29%" and food["set_on"] == "7/1/26" and food["set_by"] == "the owner"
    assert out["as_of"] == "8/15/26"
    labor = next(t for t in out["applied"] if t["label"] == "Labor target")
    assert labor["shown"] == "30%" and labor["set_on"] is None      # never changed: today's value
    assert [c["to"] for c in out["changes"]] == ["28%", "29%"] and out["changes"][0]["on"] == "9/1/26"
    before = _run(rid, {"as_of": "2026-06-01"}, OWNER)
    assert next(t for t in before["applied"] if t["label"] == "Food cost target")["shown"] == "30%"


def test_a_login_without_food_cost_never_sees_the_food_targets():
    rid = _rid()
    _change(rid, "food_cost_target", 30.0, 28.0, "2026-09-01 15:00:00")
    view = tools.viewer_restaurant(get_restaurant(rid), OWNER)
    view._ask_denied = frozenset({"inventory"})
    out = json.loads(tools.run_read_tool("read_target_history", rid, {"as_of": "2026-09-02"}, restaurant=view))
    labels = {t["label"] for t in out["applied"]} | {t["label"] for t in out["now"]}
    assert "Food cost target" not in labels and "Waste target" not in labels and "Labor target" in labels
    assert out["changes"] == []


def test_the_tool_is_registered_labelled_and_refuses_a_bad_date():
    assert tools._BY_NAME["read_target_history"]["kind"] == "read"
    assert "read_target_history" in ask_cavnar._TOOL_LABELS
    assert "error" in _run(_rid(), {"as_of": "August"}, OWNER)


def test_value_as_of_has_a_reader_now():
    assert "read_target_history" in change_log.__doc__


def test_a_stepper_clicked_five_times_is_one_change_and_a_round_trip_is_none():
    rid = _rid()
    for i, (a, b) in enumerate([(2.5, 3.0), (3.0, 3.5), (3.5, 4.0), (4.0, 4.5), (4.5, 5.0)]):
        _change(rid, "waste_target_pct", a, b, f"2026-09-28 19:2{i}:00")
    _change(rid, "labor_target_pct", 30.0, 28.0, "2026-09-20 15:00:00")
    _change(rid, "labor_target_pct", 28.0, 30.0, "2026-09-20 15:03:00")
    _change(rid, "waste_target_pct", 5.0, 4.0, "2026-09-29 15:00:00")      # a later, separate edit
    got = change_log.target_changes(rid)
    assert [(c["label"], c["from"], c["to"]) for c in got] == [("Waste target", "5%", "4%"),
                                                               ("Waste target", "2.5%", "5%")]
