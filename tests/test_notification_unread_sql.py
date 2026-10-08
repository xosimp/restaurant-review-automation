"""The bell's badge, counted in SQL (parity audit #81, 10/7/26).

`/notifications/unread-count` is read on every launch, foreground and push,
and it used to build the whole list — 40 rows with their reviews, refs,
labels and nav — to return two numbers. It now counts in one grouped query
(client_api._count_notifications_unread). The output must stay identical to
the list's own count: these tests build varied bells (two locations, more
rows than the window, every kind of `resolved`, opens by this login and by
another, read marks, priorities stored and defaulted, a role that cannot see
Food Cost) and hold the SQL count to the count of the list's rows.
"""
import random

import pytest

import auth
import client_api
import models
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, auth, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    models.init_shift_requests(db_path=db_path)


def _restaurant(db_path, name, group=None):
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test", location_group=group),
                             db_path=db_path)


def _user(db_path, rid, role="owner", name="owner", created_at="2026-01-01 00:00:00"):
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO users (restaurant_id, username, email, password_hash, created_at, role) "
                       "VALUES (?,?,?,?,?,?)", (rid, name, f"{name}@x.test", "x", created_at, role))
    conn.commit()
    uid = cur.lastrowid
    conn.close()
    return {"id": uid, "restaurant_id": rid, "base_restaurant_id": rid, "role": role, "is_admin": 0}


def _exec(db_path, sql, args=(), fk=True):
    conn = get_conn(db_path)
    if not fk:
        conn.execute("PRAGMA foreign_keys=OFF")     # a request needs no real schedule week here
    cur = conn.execute(sql, args)
    conn.commit()
    lid = cur.lastrowid
    conn.close()
    return lid


_TYPES = ["1star", "5star", "health", "labor_over", "critical_low", "price_spike", "issue", "coverage",
          "schedule_publish_pending", "shift_request", "demand_opportunity", "login", "competitor_move",
          "food_waste", "morning_brief", "order_send_pending"]


def _world(db_path, rng, rids, viewer, other_id, rows=70):
    """A bell of `rows` alerts across `rids`, newest last."""
    reviews = []
    for rid in rids:
        for status, deleted in (("drafted", None), ("posted", None), ("skipped", None), ("pending", None),
                                ("drafted", "2026-09-01 00:00:00"), ("approved", None)):
            reviews.append((rid, _exec(db_path,
                "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                "fetched_at, response_status, deleted_at) VALUES (?,'google',?,'Ann',2,'meh',datetime('now'),"
                "datetime('now'),?,?)", (rid, f"e{rng.random()}", status, deleted))))
    refs = []
    for rid in rids:
        for status in ("pending", "done", "cancelled"):
            refs.append((rid, "delayed_action", _exec(db_path,
                "INSERT INTO delayed_actions (restaurant_id, kind, execute_at, status) VALUES (?,?,?,?)",
                (rid, "order_send", "2026-10-08 10:00:00", status))))
        for status in ("pending", "approved"):
            refs.append((rid, "time_off", _exec(db_path,
                "INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
                "VALUES (?,?,?,?,?)", (rid, "Dana", "2026-10-10", "2026-10-11", status))))
        for status in ("pending", "denied"):
            refs.append((rid, "shift", _exec(db_path,
                "INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start, "
                "status) VALUES (?,?,?,?,?,?)", (rid, 1, "Dana", "2026-10-10", "10:00", status), fk=False)))
        for status in ("open", "acknowledged", "resolved"):
            refs.append((rid, "issue", _exec(db_path,
                "INSERT INTO ops_issues (restaurant_id, kind, title, status) VALUES (?,?,?,?)",
                (rid, "stock", "Low on limes", status))))
    ids = []
    for n in range(rows):
        rid = rng.choice(rids)
        t = rng.choice(_TYPES)
        fired = f"2026-10-{1 + n // 24:02d} {n % 24:02d}:{rng.randint(0, 59):02d}:00"
        review_id = ref_kind = ref_id = None
        roll = rng.random()
        if t in ("1star", "5star", "health") and roll < 0.85:
            review_id = rng.choice([r for x, r in reviews if x == rid] + [999999])
        elif roll < 0.6:
            ref_rid, ref_kind, ref_id = rng.choice(refs)
            if rng.random() < 0.15:
                ref_id = 888888                       # a ref whose row is gone
        elif roll < 0.7:
            ref_kind, ref_id = "catalog_event", 5     # a ref nobody resolves
        priority = rng.choice([None, None, 0, 1, 2, 3, 5])
        ids.append(_exec(db_path,
            "INSERT INTO alert_log (restaurant_id, alert_type, review_id, fired_at, priority, ref_kind, ref_id) "
            "VALUES (?,?,?,?,?,?,?)", (rid, t, review_id, fired, priority, ref_kind, ref_id)))
    for aid in rng.sample(ids, len(ids) // 3):
        who = viewer["id"] if rng.random() < 0.7 else other_id
        _exec(db_path, "INSERT INTO notification_opens (restaurant_id, user_id, alert_type, alert_log_id) "
                       "VALUES (?,?,?,?)", (rids[0], who, "x", aid))
    return ids


def _from_the_list(viewer, scope):
    body, _ = client_api._do_get_notifications(viewer["restaurant_id"], viewer, scope=scope)
    assert body["ok"], body
    rows = body["notifications"]
    return {"ok": True, "count": sum(1 for n in rows if n["unread"]),
            "urgent": sum(1 for n in rows if n["urgent"] and not n["resolved"])}


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("scope", [None, "group"])
def test_the_sql_count_is_the_lists_count(db_path, seed, scope):
    rng = random.Random(seed)
    a = _restaurant(db_path, "Bell A", group="Bells")
    b = _restaurant(db_path, "Bell B", group="Bells")
    owner = _user(db_path, a)
    other = _user(db_path, a, name="partner")
    _world(db_path, rng, [a, b], owner, other["id"])
    # A read mark partway through the window at one location.
    if seed % 2:
        _exec(db_path, "INSERT INTO notification_reads (user_id, restaurant_id, seen_at) VALUES (?,?,?)",
              (owner["id"], a, "2026-10-02 06:00:00"))
    want = _from_the_list(owner, scope)
    got, status = client_api._do_notifications_unread(owner, scope)
    assert status == 200
    assert got == want


@pytest.mark.parametrize("seed", range(4))
def test_a_role_that_cannot_see_food_cost_is_counted_the_lists_way(db_path, seed):
    rng = random.Random(100 + seed)
    a = _restaurant(db_path, "Bell M")
    manager = _user(db_path, a, role="manager", name="mgr", created_at="2026-10-02 00:00:00")
    _world(db_path, rng, [a], manager, 424242)
    want = _from_the_list(manager, None)
    assert client_api._do_notifications_unread(manager)[0] == want


def test_an_empty_bell_and_a_login_with_no_id(db_path):
    a = _restaurant(db_path, "Quiet")
    owner = _user(db_path, a)
    assert client_api._do_notifications_unread(owner)[0] == {"ok": True, "count": 0, "urgent": 0}
    _exec(db_path, "INSERT INTO alert_log (restaurant_id, alert_type, priority) VALUES (?,?,?)", (a, "health", 0))
    nobody = dict(owner, id=None)
    assert client_api._do_notifications_unread(nobody)[0] == _from_the_list(nobody, None) \
        == {"ok": True, "count": 1, "urgent": 1}


def test_the_count_no_longer_builds_the_list(db_path, monkeypatch):
    a = _restaurant(db_path, "Lean")
    owner = _user(db_path, a)
    _exec(db_path, "INSERT INTO alert_log (restaurant_id, alert_type) VALUES (?,?)", (a, "labor_over"))
    monkeypatch.setattr(client_api, "_do_get_notifications",
                        lambda *a, **k: pytest.fail("the badge must not build the list"))
    assert client_api._do_notifications_unread(owner)[0]["count"] == 1


def test_a_failure_reads_as_nothing_unread_never_an_error(db_path, monkeypatch):
    a = _restaurant(db_path, "Broken")
    owner = _user(db_path, a)
    monkeypatch.setattr(client_api, "_count_notifications_unread",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    assert client_api._do_notifications_unread(owner) == ({"ok": True, "count": 0, "urgent": 0}, 200)
