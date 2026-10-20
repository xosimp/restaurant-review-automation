"""Labor's current-window analysis reads the window, not the history (scale
audit 10/19/26 FE-03 / DB-12).

analyse_shifts_for_restaurant — the dashboard shell's Labor panel, Home,
the group switcher, every Labor read — loaded every shift ever recorded
(shift_facts.history_rows with no `since`) and then kept 28 days. It now
reads the window: one MAX(business_date) and the window's rows, and the
analysis is the same. The inventory and Food Cost reads beside it no longer
load (and rebuild and write) the shift export at all.
"""
from datetime import date, timedelta

import pytest

import labor
import models
import shift_facts
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)


def _rid():
    return create_restaurant(Restaurant(name="Window Co", owner_email="w@x.test", module_labor=1))


def _history(rid, days=200):
    today = date.today()
    rows = []
    for i in range(days):
        d = today - timedelta(days=i + 1)
        for n, role in (("Ana B.", "Server"), ("Bo C.", "Cook")):
            rows.append({"date": d.isoformat(), "day": d.strftime("%A"), "employee": n, "role": role,
                         "shift_start": "16:00", "shift_end": "22:00", "scheduled_hours": "6",
                         "actual_hours": "6", "sales": "2500", "schedule_known": "0"})
    shift_facts.ingest(rid, rows, "rpower")
    if shift_facts.store_of(rid) != "facts":
        shift_facts.adopt(rid)
    assert shift_facts.store_of(rid) == "facts"


def test_the_window_is_read_not_the_history(monkeypatch):
    rid = _rid()
    _history(rid)
    seen = []
    real = shift_facts.history_rows
    monkeypatch.setattr(shift_facts, "history_rows",
                        lambda r, since=None, until=None, db_path=None: seen.append(since) or real(
                            r, since=since, until=until, db_path=db_path))
    a = labor.analyse_shifts_for_restaurant(rid)
    assert seen and all(s is not None for s in seen)
    latest = date.today() - timedelta(days=1)
    assert seen[0] == (latest - timedelta(days=labor.CURRENT_WINDOW_DAYS - 1)).isoformat()
    assert a["is_live"]


def test_the_analysis_is_the_same_as_the_whole_history_read(monkeypatch):
    rid = _rid()
    _history(rid)
    bounded = labor.analyse_shifts_for_restaurant(rid)
    monkeypatch.setattr(labor, "_window_rows", lambda *a, **k: None)
    whole = labor.analyse_shifts_for_restaurant(rid)
    assert bounded == whole


def test_a_future_typo_row_never_anchors_the_window():
    rid = _rid()
    _history(rid, days=60)
    assert shift_facts.latest_date(rid, until=(date.today() + timedelta(days=1)).isoformat()) == \
        (date.today() - timedelta(days=1)).isoformat()


def test_the_food_reads_never_load_the_shift_export(monkeypatch):
    calls = []
    real = models.get_client_data
    monkeypatch.setattr(models, "get_client_data",
                        lambda rid, db_path=models.DB_PATH, with_shifts=True: calls.append(with_shifts) or real(
                            rid, db_path=db_path, with_shifts=with_shifts))
    rid = _rid()
    import inventory
    import client_api
    inventory.analysis_for(rid)
    client_api.food_cost_tracker_data(rid)
    assert calls and not any(calls)
