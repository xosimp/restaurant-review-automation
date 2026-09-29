"""Memory audit 9/29/26 (workstream M2, ask_reach): Ask can see what the
other surfaces told the owner, what is coming, how its forecasts held up,
the close-outs, marketing's results, the daily report's Tomorrow and graded
predictions, and the card the owner is looking at — each scoped to the
login asking.
"""
import json
from datetime import date, datetime, timedelta

import pytest

import ask_cavnar
import ask_cavnar_tools as tools
import auth
import models
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

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
    import guest_marketing
    guest_marketing.init_guest_marketing(db_path=db_path)
    yield


def _rid(**kw):
    kw.setdefault("name", "Reach Co")
    kw.setdefault("owner_email", "reach@x.test")
    kw.setdefault("timezone", "America/Chicago")
    return create_restaurant(Restaurant(**kw))


def _run(name, rid, args, user):
    view = tools.viewer_restaurant(get_restaurant(rid), user)
    return json.loads(tools.run_read_tool(name, rid, args, restaurant=view))


def _today(rid):
    from time_utils import restaurant_now_by_id
    return restaurant_now_by_id(rid, naive=True).date()


def test_the_new_reach_tools_are_registered_reads():
    for name in ("read_recent_reads", "read_upcoming", "read_forecast_record", "read_closeouts",
                 "read_marketing_results", "read_past_conversations"):
        assert tools._BY_NAME[name]["kind"] == "read", name
        assert name in ask_cavnar._TOOL_LABELS, name
    assert tools.reads_public_text("read_closeouts") and tools.reads_public_text("read_recent_reads")


def test_read_recent_reads_returns_what_the_food_tab_said_with_its_date_and_hides_it_from_a_manager():
    import insight_store
    rid = _rid()
    insight_store.put(rid, "food", "fp1", "Cut salmon orders to two cases: 31% of it was wasted last week.")
    out = _run("read_recent_reads", rid, {}, OWNER)
    food = next(r for r in out["reads"] if r["what"] == "the Food Cost read")
    assert "Cut salmon orders" in food["text"] and "/" in food["date"] and "-" not in food["date"]
    assert not any(r["what"] == "the Food Cost read" for r in _run("read_recent_reads", rid, {}, MANAGER)["reads"])


def test_read_upcoming_lists_the_owners_party_closures_time_off_and_posts_by_date():
    import demand_signals
    rid = _rid(module_labor=1, module_marketing=1)
    today = _today(rid)
    fri = today + timedelta(days=3)
    demand_signals.save(rid, [{"date": fri.isoformat(), "kind": "event", "label": "Private party, 60 covers",
                               "covers": 60}])
    import schedule_rules
    schedule_rules.change_closed_dates(rid, add=[(today + timedelta(days=5)).isoformat()])
    conn = models.get_conn()
    conn.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
                 "VALUES (?,?,?,?, 'approved')", (rid, "Maria", fri.isoformat(), fri.isoformat()))
    conn.execute("INSERT INTO marketing_scheduled_posts (restaurant_id, platform, topic, body, scheduled_for) "
                 "VALUES (?,?,?,?,?)", (rid, "instagram", "Friday special", "Come in!", f"{fri.isoformat()}T17:00:00"))
    conn.commit()
    conn.close()
    out = _run("read_upcoming", rid, {}, OWNER)
    kinds = {(i["kind"], i["date"]) for i in out["items"]}
    assert ("event", fri.isoformat()) in kinds and ("time_off", fri.isoformat()) in kinds
    assert ("post", fri.isoformat()) in kinds and any(i["kind"] == "closed" for i in out["items"])
    party = next(i for i in out["items"] if i["kind"] == "event")
    assert party["covers"] == 60 and party["day"] == f"{fri.month}/{fri.day}/{fri.year % 100:02d}"
    # a view without Marketing is not shown the scheduled posts, one without
    # Labor not the time off
    view = tools.viewer_restaurant(get_restaurant(rid), MANAGER)
    view._ask_denied = frozenset({"marketing", "labor"})
    items = json.loads(tools.run_read_tool("read_upcoming", rid, {}, restaurant=view))["items"]
    assert not any(i["kind"] in ("post", "time_off") for i in items)
    assert any(i["kind"] == "event" for i in items)


def test_read_forecast_record_carries_every_kind_and_a_manager_loses_the_food_ones():
    import forecast_log
    rid = _rid(module_labor=1, module_inventory=1)
    owner = _run("read_forecast_record", rid, {}, OWNER)
    assert set(owner["forecasts"]) == set(forecast_log.KINDS)
    assert "daily_demand" in owner
    mgr = _run("read_forecast_record", rid, {}, MANAGER)
    assert "waste_week" not in mgr["forecasts"] and "labor_week" in mgr["forecasts"]


def test_read_closeouts_returns_the_closers_words_fenced():
    import ai_guard
    rid = _rid()
    conn = models.get_conn()
    day = (_today(rid) - timedelta(days=1)).isoformat()
    conn.execute("INSERT INTO close_outs (restaurant_id, business_date, went_wrong, eighty_sixed, submitted_by) "
                 "VALUES (?,?,?,?,?)", (rid, day, "Walk-in door sticking", "Salmon", "Dana"))
    conn.commit()
    conn.close()
    out = _run("read_closeouts", rid, {}, OWNER)
    assert out["count"] == 1 and "Walk-in door sticking" in out["closeouts"][0]["notes"]
    assert ai_guard.UNTRUSTED_OPEN in out["closeouts"][0]["notes"]


def test_read_marketing_results_counts_a_return_only_once_its_window_closed():
    rid = _rid(module_marketing=1)
    conn = models.get_conn()
    old = (datetime.utcnow() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    new = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO guest_campaigns (restaurant_id, message, sent_count, created_at, visits_matched, "
                 "attribution_through, segment) VALUES (?,?,?,?,?,?,?)",
                 (rid, "Come back", 100, old, 7, (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%d"),
                  "lapsed_30"))
    conn.execute("INSERT INTO guest_campaigns (restaurant_id, message, sent_count, created_at, visits_matched, "
                 "attribution_through, segment) VALUES (?,?,?,?,?,?,?)",
                 (rid, "Tuesday deal", 80, new, 2, datetime.utcnow().strftime("%Y-%m-%d"), "all"))
    conn.commit()
    conn.close()
    out = _run("read_marketing_results", rid, {}, OWNER)
    by = {c["sent"]: c for c in out["guest_texts"]}
    assert by[100]["came_back"] == 7 and by[80]["came_back"] is None and "not closed" in by[80]["return_note"]
    assert out["winback"]["measured"] is True


def test_read_dsr_carries_tomorrow_and_the_graded_predictions_about_the_night(monkeypatch):
    from test_dsr_memory import _ejs, _night, TUE
    from dsr import predictions, store
    db = models.DB_PATH
    r = _ejs(db)
    rep = _night(db, r.id, TUE)
    store.save_section(rep["id"], "tomorrow", {
        "date": (TUE + timedelta(days=1)).isoformat(), "items": [{"text": "Maria is off", "kind": "time_off"}],
        "forecast": {"net": 4100}, "confidence": None,
        "predictions": [{"key": "sales_range", "text": "Sales between $3,500 and $4,600"},
                        {"key": "sales_budget", "text": "Sales expected above budget ($4,000)"}]}, db_path=db)
    predictions.record(r.id, (TUE - timedelta(days=1)).isoformat(), TUE.isoformat(),
                       [{"key": "sales_range", "metric": "sales.net", "op": "between", "low": 4000, "high": 6000,
                         "text": "Sales between $4,000 and $6,000"}], db_path=db,
                       made_at=datetime(2026, 9, 21, 12, 0))
    conn = models.get_conn()
    conn.execute("UPDATE dsr_predictions SET outcome='correct', actual=5000 WHERE restaurant_id=?", (r.id,))
    conn.commit()
    conn.close()
    owner = _run("read_dsr", r.id, {"date": TUE.isoformat()}, OWNER)
    assert owner["tomorrow"]["date"] == "9/23/26" and "Maria is off" in owner["tomorrow"]["items"]
    assert "Sales expected above budget ($4,000)" in owner["tomorrow"]["predictions"]
    assert owner["predictions_about_this_night"]["items"][0]["outcome"] == "correct"
    mgr = _run("read_dsr", r.id, {"date": TUE.isoformat()}, MANAGER)
    assert not any("budget" in p for p in mgr["tomorrow"]["predictions"]), "the budget is the owner's"


def test_the_card_on_screen_is_resolved_from_the_ledger_for_a_login_that_may_see_it():
    import rec_ledger
    rid = _rid()
    rec_ledger.present(rid, "trim_day:Tuesday", "labor", "home", title="Trim Tuesday dinner by one server")
    hint = ask_cavnar.screen_hint(rid, {"panel": "home", "entity": {"type": "rec", "id": "trim_day:Tuesday"}},
                                  viewer=OWNER)
    assert "Trim Tuesday dinner by one server" in hint and "Labor" in hint and "not answered yet" in hint
    import ai_guard
    assert ai_guard.UNTRUSTED_OPEN in hint
    assert "the recommendation keyed nope:x" in ask_cavnar.screen_hint(
        rid, {"entity": {"type": "rec", "id": "nope:x"}}, viewer=OWNER)
