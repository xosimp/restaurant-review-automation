"""Memory audit 9/29/26, workstream M1 — the trust automations earn.

  undo          an undo of the auto-publish or a trusted supplier's order
                counts against the trust that queued it: N clean runs are
                needed after one, and the owner is asked why.
  trust_ledger  a reply band's auto-approve trust records when it was earned,
                on what, and when it lapsed; while held it is kept on weak
                credit (auto-posts the owner left standing), a retraction
                counts against it, and a lapse is surfaced, never silent.
"""
import json

import pytest

import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    # delayed and ordering bind models.get_conn at import (CLAUDE.md, bound
    # imports): point their copies at this test's database too.
    import delayed
    import ordering
    for mod in (delayed, ordering):
        if hasattr(mod, "get_conn"):
            monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path))
    yield


def _rid(db_path, name="Trust Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"), db_path=db_path)


def _sql(db_path, sql, *args):
    c = models.get_conn(db_path)
    cur = c.execute(sql, args)
    c.commit()
    last = cur.lastrowid
    c.close()
    return last


# ── undo ─────────────────────────────────────────────────────────────────────

def test_an_undone_auto_publish_needs_clean_runs_again(db_path, monkeypatch):
    import delayed
    import schedule_intel
    monkeypatch.setattr(schedule_intel, "watched_dates", lambda rid, a, b, db=None: {a, b})
    rid = _rid(db_path)
    for i, wk in enumerate(("2026-08-31", "2026-09-07", "2026-09-14")):
        _sql(db_path, "INSERT INTO schedule_history (restaurant_id, week_start, week_end, published_at) "
                      "VALUES (?,?,?, datetime('now', ?))", rid, wk, wk, f"-{30 - i} days")
    assert models.schedule_publish_trust(rid, db_path=db_path) == 3
    row = delayed.schedule(rid, "schedule_publish", {"schedule_id": 1, "automatic": True}, 60, db_path=db_path)
    assert delayed.cancel(rid, row["id"], actor={"username": "owner"}, db_path=db_path)
    assert models.schedule_publish_trust(rid, db_path=db_path) == 0
    assert delayed.last_undo(rid, "schedule_publish", db_path=db_path)
    # Weeks published after the undo earn it back.
    _sql(db_path, "INSERT INTO schedule_history (restaurant_id, week_start, week_end, published_at) "
                  "VALUES (?,?,?, datetime('now', '+1 minute'))", rid, "2026-09-21", "2026-09-27")
    assert models.schedule_publish_trust(rid, db_path=db_path) == 1


def test_an_undone_supplier_order_needs_clean_orders_again(db_path):
    import delayed
    import ordering
    rid = _rid(db_path)
    for i in range(3):
        _sql(db_path, "INSERT INTO purchase_orders (restaurant_id, po_number, supplier_email, items_json, total_cost, "
                      "status, sent_at, source) VALUES (?,?,?,?,?,?, datetime('now', ?), 'owner')",
             rid, f"PO{i}", "fresh@co.test", "[]", 100, "sent", f"-{10 - i} days")
    assert ordering.supplier_trust(rid, "fresh@co.test", db_path=db_path)["trusted"]
    row = delayed.schedule(rid, "order_send", {"supplier_email": "fresh@co.test", "automatic": True}, 60,
                           db_path=db_path)
    delayed.cancel(rid, row["id"], db_path=db_path, reason_code="wrong_content")
    t = ordering.supplier_trust(rid, "fresh@co.test", db_path=db_path)
    assert not t["trusted"] and t["clean_since_undo"] == 0
    # Another supplier is untouched.
    for i in range(3):
        _sql(db_path, "INSERT INTO purchase_orders (restaurant_id, po_number, supplier_email, items_json, total_cost, "
                      "status, sent_at, source) VALUES (?,?,?,?,?,?, datetime('now', ?), 'owner')",
             rid, f"PX{i}", "other@co.test", "[]", 100, "sent", f"-{10 - i} days")
    assert ordering.supplier_trust(rid, "other@co.test", db_path=db_path)["trusted"]


def test_the_undo_asks_why_and_keeps_the_answer(db_path, monkeypatch):
    import delayed
    import strategy_routes as sr
    from flask import Flask
    rid = _rid(db_path)
    row = delayed.schedule(rid, "schedule_publish", {"schedule_id": 1, "automatic": True}, 60, db_path=db_path)
    u = {"id": 1, "restaurant_id": rid, "role": "client", "is_admin": True}
    monkeypatch.setattr(sr, "_may_undo", lambda u, kind: True)
    with Flask("t").test_request_context(json={}):
        out, st = sr._do_delayed_cancel(u, row["id"])
    assert st == 200 and out["ask_why"]["route"].endswith(f"/{row['id']}/why")
    assert {o["code"] for o in out["ask_why"]["options"]} == set(delayed.UNDO_REASONS)
    with Flask("t").test_request_context(json={"reason_code": "wanted_changes", "reason": "Ana is off Friday"}):
        out, st = sr._do_delayed_why(u, row["id"])
    assert st == 200
    c = models.get_conn(db_path)
    res = json.loads(c.execute("SELECT result_json FROM delayed_actions WHERE id=?", (row["id"],)).fetchone()[0])
    c.close()
    assert res["reason_code"] == "wanted_changes" and res["reason"] == "Ana is off Friday"


# ── trust ledger ─────────────────────────────────────────────────────────────

def _reviews(db_path, rid, n, action="approved_as_is", status="posted", days_ago=2, rating=5, start=0):
    for i in range(n):
        _sql(db_path, "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, response_status, "
                      "draft_response, response_action, approved_at, fetched_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?, datetime('now', ?), datetime('now'))",
             rid, "google", f"r{start + i}", "G", rating, "Great", status, "Thanks!", action, f"-{days_ago} days")


def test_a_band_records_when_it_was_earned_and_is_kept_on_weak_credit(db_path):
    import automation_trust as at
    rid = _rid(db_path)
    _reviews(db_path, rid, 10, days_ago=25)
    t = models.auto_approve_trust(rid, db_path=db_path)[5]
    assert t["trusted"] and t["earned_at"]
    st = at.states(rid, "reply_band", db_path=db_path)["5"]
    assert st["state"] == "earned" and json.loads(st["basis"])["approved"] == 10
    # A month on, the person approvals have aged out; the band's own clean
    # auto-posts keep it (weighted at half a person's approval).
    _sql(db_path, "UPDATE reviews SET approved_at=datetime('now','-40 days') WHERE restaurant_id=?", rid)
    _reviews(db_path, rid, 20, action="auto_approved", days_ago=10, start=100)
    t = models.auto_approve_trust(rid, db_path=db_path)[5]
    assert t["trusted"] and t["weak_credit"] == 10.0 and t["autoposted_clean"] == 20
    # Earning never rests on the rule's own output: a band never earned is
    # not trusted on auto-posts alone.
    rid2 = _rid(db_path, "Fresh Co")
    _reviews(db_path, rid2, 30, action="auto_approved", days_ago=10, start=500)
    assert not models.auto_approve_trust(rid2, db_path=db_path)[5]["trusted"]


def test_a_lapse_is_recorded_and_surfaced_never_silent(db_path):
    import automation_trust as at
    rid = _rid(db_path)
    _reviews(db_path, rid, 10, days_ago=25)
    assert models.auto_approve_trust(rid, db_path=db_path)[5]["trusted"]
    _sql(db_path, "UPDATE reviews SET approved_at=datetime('now','-40 days') WHERE restaurant_id=?", rid)
    t = models.auto_approve_trust(rid, db_path=db_path)[5]
    assert not t["trusted"] and t["lapsed"]["reason"] == "fewer than 10 approvals in 30 days"
    items = at.lapsed_items(rid, db_path=db_path)
    assert items and items[0]["subject"] == "5" and "went back to you" in items[0]["text"]
    assert "-" not in items[0]["lapsed_on"]
    # Earned back: the ledger says so.
    _reviews(db_path, rid, 10, days_ago=1, start=200)
    assert models.auto_approve_trust(rid, db_path=db_path)[5]["trusted"]
    assert at.states(rid, "reply_band", db_path=db_path)["5"]["state"] == "earned"
    assert at.lapsed_items(rid, db_path=db_path) == []
