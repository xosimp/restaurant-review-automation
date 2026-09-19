"""Signals and checklists become owned work (workflow audit #9, #10).

An alert tells the owner. An issue tells one person, records that they saw
it, and escalates when they don't — which is the difference between knowing
and handling.
"""
from datetime import date, datetime, timedelta

import pytest

import models
from models import create_restaurant, get_conn, Restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import issues, notify, push, auth
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, issues, notify, push, auth):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr("notify.send_sms", lambda *a, **k: True)


def _rid(db_path, **kw):
    kw.setdefault("name", "Ops Co")
    kw.setdefault("owner_email", "o@x.test")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _routed(db_path, rid):
    import issues
    conn = get_conn(db_path)
    cid = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) "
                       "VALUES (?, 'GM', '+15555550100', 1)", (rid,)).lastrowid
    conn.commit(); conn.close()
    issues.set_routing(rid, "manager", cid, db_path=db_path)
    return cid


def test_critically_low_stock_becomes_the_managers_issue(db_path, monkeypatch):
    import issues, inventory
    rid = _rid(db_path, module_inventory=1)
    _routed(db_path, rid)
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant", lambda r: ([{"item": "x"}], True))
    monkeypatch.setattr(inventory, "analysis_for",
                        lambda *a, **k: (None, None, {"critical_low": [{"item": "Chicken Breast"},
                                                                       {"item": "Romaine"},
                                                                       {"item": "Basil"}]}))
    opened = issues.open_from_signals(rid, db_path=db_path)
    assert [i["kind"] for i in opened] == ["stock"]
    assert opened[0]["severity"] == "high" and "Chicken Breast" in opened[0]["detail"]
    # Same signal tomorrow morning must not re-text anyone today.
    assert issues.open_from_signals(rid, db_path=db_path) == []


def test_labour_over_target_becomes_an_issue_once_a_week(db_path, monkeypatch):
    import issues, labor
    rid = _rid(db_path, module_labor=1)
    _routed(db_path, rid)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant",
                        lambda r: {"is_live": True, "labor_pct": 34.2})
    opened = issues.open_from_signals(rid, db_path=db_path)
    assert [i["kind"] for i in opened] == ["labor"]
    assert "34.2%" in opened[0]["title"]
    assert issues.open_from_signals(rid, db_path=db_path) == [], "one per week, not per day"


def test_sample_labour_data_never_opens_an_issue(db_path, monkeypatch):
    import issues, labor
    rid = _rid(db_path, module_labor=1)
    _routed(db_path, rid)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant",
                        lambda r: {"is_live": False, "labor_pct": 44.0})
    assert issues.open_from_signals(rid, db_path=db_path) == []


def test_no_routed_manager_means_no_automatic_issues(db_path, monkeypatch):
    import issues, inventory
    rid = _rid(db_path, module_inventory=1)
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant", lambda r: ([{"item": "x"}], True))
    monkeypatch.setattr(inventory, "analysis_for",
                        lambda *a, **k: (None, None, {"critical_low": [{"item": "Basil"}]}))
    assert issues.open_from_signals(rid, db_path=db_path) == []


def _checklist(db_path, rid, role="Line Cook", labels=("Check walk-in temp", "Stock the line")):
    from models import add_task_template
    return [add_task_template(rid, role, l, db_path=db_path) for l in labels]


def test_an_unfinished_opening_checklist_becomes_an_issue(db_path):
    import issues
    rid = _rid(db_path)
    _routed(db_path, rid)
    models.update_restaurant(rid, {"open_times_json": '{"Monday": "10:00am"}'}, db_path=db_path)
    _checklist(db_path, rid)
    monday = datetime(2026, 9, 21, 11, 30)          # 90 minutes after opening
    opened = issues.open_from_checklists(rid, db_path=db_path, now_local=monday)
    assert [i["kind"] for i in opened] == ["checklist"]
    assert "2 opening tasks" in opened[0]["title"]
    assert issues.open_from_checklists(rid, db_path=db_path, now_local=monday) == [], "once a day"


def test_the_checklist_is_given_an_hour_before_it_is_chased(db_path):
    import issues
    rid = _rid(db_path)
    _routed(db_path, rid)
    models.update_restaurant(rid, {"open_times_json": '{"Monday": "10:00am"}'}, db_path=db_path)
    _checklist(db_path, rid)
    assert issues.open_from_checklists(rid, db_path=db_path,
                                       now_local=datetime(2026, 9, 21, 10, 30)) == []


def test_a_completed_checklist_raises_nothing(db_path):
    import issues
    from models import set_task_completion
    rid = _rid(db_path)
    _routed(db_path, rid)
    models.update_restaurant(rid, {"open_times_json": '{"Monday": "10:00am"}'}, db_path=db_path)
    ids = _checklist(db_path, rid)
    for t in ids:
        set_task_completion(rid, t["id"], "2026-09-21", True, "Jordan", db_path=db_path)
    assert issues.open_from_checklists(rid, db_path=db_path,
                                       now_local=datetime(2026, 9, 21, 11, 30)) == []


def test_without_opening_hours_nothing_is_late(db_path):
    import issues
    rid = _rid(db_path)
    _routed(db_path, rid)
    _checklist(db_path, rid)
    assert issues.open_from_checklists(rid, db_path=db_path,
                                       now_local=datetime(2026, 9, 21, 23, 0)) == []
