"""Admin audit #26 (10/27/26), fix stream WS-1 — backend data accuracy,
health and performance. One regression test per fixed defect, each on the
fixture's throwaway database (never a real one). Finding IDs are the audit
reports' (03-kpi K-*, 04-workflows F*, 06-performance P-*, 07-ops-code O*/C*)."""
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

import admin_ops
import ai_utils
import models
import ops
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    """Every module's get_conn — bound copies included (CLAUDE.md's bound
    import hazard) — opens this test's database."""
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    if hasattr(ai_utils, "_budget_cache"):
        ai_utils._budget_cache.clear()


def _rest(db_path, name, **kw):
    return create_restaurant(Restaurant(name=name, owner_email=kw.pop("owner_email", f"{name.split()[0].lower()}@ex.com"),
                                        module_reviews=1, **kw), db_path=db_path)


def _sql(db_path, sql, args=()):
    c = get_conn(db_path)
    c.execute(sql, args)
    c.commit()
    c.close()


def _ago_utc(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


# ── F5: one malformed row no longer takes down the fleet build ──────────────

def test_f5_a_malformed_row_reads_as_a_stand_in_not_a_failed_build(db_path):
    good = _rest(db_path, "Good Grill", billing_status="active")
    bad = _rest(db_path, "Bad Bistro", billing_status="active")
    _sql(db_path, "UPDATE restaurants SET contract_signed_at='10/1/26' WHERE id=?", (bad,))
    out = admin_ops.clients()
    recs = {r["id"]: r for r in out["clients"]}
    assert good in recs and bad in recs
    # The unparseable stamp itself is no age, not a crash.
    assert recs[bad]["billing"]["days_since_signed"] is None


def test_f5_a_row_that_raises_is_isolated_and_named(db_path, monkeypatch):
    good = _rest(db_path, "Good Grill", billing_status="active")
    bad = _rest(db_path, "Bad Bistro", billing_status="active")
    real = admin_ops._integrations_for

    def boom(r, hooks, d=None):
        if r["id"] == bad and r.get("owner_email"):
            raise ValueError("garbage in a column")
        return real(r, hooks, d)
    monkeypatch.setattr(admin_ops, "_safe_row", lambda r: {"id": r["id"], "name": r.get("name"),
                                                             "billing_status": "active", "owner_email": None})
    monkeypatch.setattr(admin_ops, "_integrations_for", boom)
    out = admin_ops.clients()
    recs = {r["id"]: r for r in out["clients"]}
    assert recs[good]["health"] != "critical" or not recs[good].get("record_unreadable")
    assert recs[bad]["record_unreadable"] is True
    assert [i["key"] for i in recs[bad]["issues"]] == [f"{bad}:record_unreadable"]
    assert any(e["query"] == f"location_record:{bad}" for e in out["errors"])
    # Another client's page still opens.
    assert admin_ops.client_detail(good)["ok"] is True


# ── K-05: billed MRR applies quantity once ──────────────────────────────────

def test_k05_quantity_is_not_applied_twice(db_path):
    import billing_jobs
    rid = _rest(db_path, "Qty House", billing_status="active", stripe_customer_id="cus_Q1")
    _sql(db_path, "INSERT INTO stripe_subscriptions (restaurant_id, subscription_id, status, interval, interval_count, "
                  "amount_cents, quantity) VALUES (?,?,?,?,?,?,?)", (rid, "sub_Q1", "active", "month", 1, 104700, 3))
    c = get_conn(db_path)
    row = dict(c.execute("SELECT * FROM stripe_subscriptions").fetchone())
    c.close()
    k = admin_ops.overview()["kpis"]
    assert k["mrr"] == billing_jobs.billed_mrr(row) == 1047.0
    sub = next(r for r in admin_ops.clients()["clients"] if r["id"] == rid)["billing"]["subscription"]
    assert sub["amount"] == 1047.0 and sub["unit_amount"] == 349.0 and sub["monthly_gross"] == 1047.0


# ── K-10: list MRR is the list price of what is billed ─────────────────────

def test_k10_list_mrr_leaves_trialing_subscriptions_out(db_path):
    rid = _rest(db_path, "Trialing Tap", billing_status="active", stripe_customer_id="cus_T1")
    _sql(db_path, "INSERT INTO stripe_subscriptions (restaurant_id, subscription_id, status, interval, interval_count, "
                  "amount_cents, quantity) VALUES (?,?,?,?,?,?,?)", (rid, "sub_T1", "trialing", "month", 1, 34900, 1))
    k = admin_ops.overview()["kpis"]
    assert k["mrr"] == 0 and k["mrr_list"] == 0
    assert k["mrr_committed"] == 349.0 and k["mrr_committed_list"] > 0
