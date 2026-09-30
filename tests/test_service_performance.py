"""Measured server, room and kitchen performance from the POS archive
(RPower endpoint audit, 9/29/26, Critical #4)."""
import json
from datetime import date

import pytest

import ask_cavnar_tools as tools
import models
import service_performance as sp
from models import Restaurant, create_restaurant, get_restaurant

TODAY = date(2026, 9, 30)


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    import sys
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", fake)
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(sp, "get_conn", fake)
    yield


def _ticket(rid, n, server, day, net, room="Dining Room", opened="18:00", closed="19:10", guests=2,
            entrees=2, drinks=2, tip=0.0, is_bar=0, fired=None, bumped=None):
    conn = models.get_conn()
    conn.execute(
        "INSERT INTO pos_tickets (restaurant_id, provider, ticket_id, business_date, opened_at, closed_at, fired_at, "
        "bumped_at, server_id, server_name, room_name, is_bar, guest_count, entree_count, bev_count, net_sales, tip, "
        "mealtime) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, "rpower", f"t{n}", day, f"{day}T{opened}:00", f"{day}T{closed}:00", fired, bumped, server[0],
         server[1], room, is_bar, guests, entrees, drinks, net, tip, "Dinner"))
    conn.execute("INSERT OR IGNORE INTO pos_archive_days (restaurant_id, provider, business_date) VALUES (?,?,?)",
                 (rid, "rpower", day))
    conn.commit()
    conn.close()


def _rid():
    return create_restaurant(Restaurant(name="Perf Co", owner_email="p@x.test"))


DANA, BO = ("E1", "Dana Reyes"), ("E2", "Bo Park")


def test_a_server_past_the_floor_gets_measured_figures_and_one_below_gets_a_count(db_path):
    rid = _rid()
    for i in range(25):
        _ticket(rid, i, DANA, f"2026-09-{1 + i % 28:02d}", 100.0, guests=2, entrees=2, drinks=3, tip=20.0 if i % 2 else 0)
    for i in range(5):
        _ticket(rid, 100 + i, BO, "2026-09-20", 60.0)
    out = sp.servers(rid, days=30, today=TODAY)
    dana, bo = out["servers"]
    assert (dana["server"], dana["tickets"], dana["sales_per_cover"], dana["drinks_per_entree"]) == ("Dana Reyes", 25, 50.0, 1.5)
    assert dana["tip_rate_pct"] == 20.0 and dana["tickets_with_tip"] == 12
    assert dana["median_turn_minutes"] == 70
    assert bo == {"server": "Bo Park", "tickets": 5, "enough": False}      # a count only below 20 tickets


def test_turns_skip_bars_pickups_and_tickets_left_open(db_path):
    rid = _rid()
    _ticket(rid, 1, DANA, "2026-09-20", 50.0, closed="18:45")                      # 45-minute table
    _ticket(rid, 2, DANA, "2026-09-20", 50.0, room="Pick-Up", closed="18:01")
    _ticket(rid, 3, DANA, "2026-09-20", 50.0, room="Bar Room", is_bar=1, closed="23:30")
    _ticket(rid, 4, DANA, "2026-09-20", 50.0, opened="11:00", closed="23:59")    # left open: over the bound
    f = sp._figures(sp._tickets(rid, date(2026, 9, 20), date(2026, 9, 20), models.DB_PATH)[0], {})
    assert (f["median_turn_minutes"], f["turns_measured"]) == (45, 1)


def test_kitchen_speed_needs_the_pos_to_record_it(db_path):
    rid = _rid()
    _ticket(rid, 1, DANA, "2026-09-20", 50.0)
    assert sp.kitchen(rid, days=30, today=TODAY)["available"] is False
    _ticket(rid, 2, DANA, "2026-09-21", 50.0, fired="2026-09-21T18:05:00", bumped="2026-09-21T18:17:00")
    k = sp.kitchen(rid, days=30, today=TODAY)
    assert k["available"] and k["median_minutes"] == 12.0 and k["by_hour"] == [{"hour": "18", "median_minutes": 12.0, "tickets": 1}]


def test_nothing_archived_says_so(db_path):
    assert sp.servers(_rid(), today=TODAY) == {"available": False, "window": ["2026-09-02", "2026-09-29"],
                                                "reason": "no tickets archived for these days yet"}


def test_the_ask_tool_is_the_account_holders_only(db_path):
    rid = _rid()
    owner = {"id": 1, "role": "client", "is_admin": 0, "username": "erik", "restaurant_id": rid}
    manager = {"id": 2, "role": "manager", "is_admin": 0, "username": "dana", "restaurant_id": rid}
    import permissions
    orig = permissions.is_principal
    permissions_is = lambda u: (u or {}).get("role") == "client"  # noqa: E731
    try:
        permissions.is_principal = permissions_is
        assert tools.tool_allowed("read_service_performance", tools.viewer_restaurant(get_restaurant(rid), owner))
        assert not tools.tool_allowed("read_service_performance", tools.viewer_restaurant(get_restaurant(rid), manager))
    finally:
        permissions.is_principal = orig
    _ticket(rid, 1, DANA, "2026-09-20", 50.0)
    out = json.loads(tools.run_read_tool("read_service_performance", rid, {"days": 30},
                                         restaurant=get_restaurant(rid)))
    assert out["available"] and "/" in out["window"][0] and "-" not in out["window"][0]
