"""Fix round C — billing figures in the console: MRR from the Stripe
subscription mirror (once per subscription, annual ÷ 12, trialing
committed), groups billed once, no Stripe call per customer, pause reasons,
signed-but-unpaid, trial conversion, the daily snapshot, vendor costs."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import admin_ops
import config
from models import get_conn
from tests.test_fix_c_console import _mk, _rec, _rows, _sql, _utc, env  # noqa: F401

# Workstream H's mirror columns, created here as the contract names them.
MIRROR_COLS = ("status TEXT", "interval TEXT", "interval_count INTEGER", "amount_cents INTEGER", "currency TEXT",
               "quantity INTEGER", "discount_pct REAL", "trial_end TEXT", "current_period_end TEXT",
               "cancel_at_period_end INTEGER", "canceled_at TEXT", "ended_at TEXT", "cancellation_reason TEXT",
               "updated_at TEXT", "module_keys TEXT")


def _mirror(db_path):
    for col in MIRROR_COLS:
        _sql(db_path, f"ALTER TABLE stripe_subscriptions ADD COLUMN {col}")


def _sub(db_path, rid, sid, status="active", interval="month", amount=34900, discount=None, modules=None):
    _sql(db_path, "INSERT INTO stripe_subscriptions (restaurant_id, subscription_id, status, interval, "
                  "interval_count, amount_cents, currency, quantity, discount_pct, updated_at, module_keys) "
                  "VALUES (?,?,?,?,1,?,'usd',1,?,datetime('now'),?)",
         (rid, sid, status, interval, amount, discount, modules))


def _one(db_path, name, **kw):
    """A one-module (reviews) restaurant: $349 a month at list."""
    kw.setdefault("module_reviews", 1)
    for m in ("module_labor", "module_inventory", "module_marketing"):
        kw.setdefault(m, 0)
    return _mk(db_path, name, **kw)


def test_mrr_counts_a_group_once_at_the_annual_rate(db_path):
    _mirror(db_path)
    a = _one(db_path, "Grp A", billing_status="active", location_group="Grp", module_reviews=1)
    b = _one(db_path, "Grp B", billing_status="active", location_group="Grp", owner_email="grpa@x.test")
    c = _one(db_path, "Grp C", billing_status="active", location_group="Grp", owner_email="grpa@x.test")
    _sql(db_path, "UPDATE restaurants SET owner_email='grpa@x.test' WHERE id=?", (a,))
    _sub(db_path, a, "sub_grp", interval="year", amount=349000)          # $3,490 a year
    k = admin_ops.overview()["kpis"]
    assert k["mrr"] == pytest.approx(290.83) and k["mrr_billed"] == pytest.approx(290.83)
    assert k["mrr_list"] == pytest.approx(290.83) and k["mrr_mismatches"] == 0
    assert k["covered_locations"] == 2 and k["mrr_source"] == "mirror"
    for rid in (b, c):
        bill = _rec(rid)["billing"]
        assert bill["monthly"] == 0 and bill["billed_by"] == {"restaurant_id": a, "name": "Grp A"}
    assert sorted(_rec(a)["billing"]["covers"]) == [b, c]
    brand = next(x for x in admin_ops.overview()["brands"] if x["brand"] == "Grp")
    assert brand["monthly"] == pytest.approx(290.83)


def test_trialing_is_committed_discounts_net_and_list_mismatches_flag(db_path):
    _mirror(db_path)
    trialing = _one(db_path, "Trialing", billing_status="active")
    discounted = _one(db_path, "Discounted", billing_status="active")
    odd = _one(db_path, "Odd Price", billing_status="active")
    _sub(db_path, trialing, "sub_t", status="trialing")
    _sub(db_path, discounted, "sub_d", discount=20)
    _sub(db_path, odd, "sub_o", amount=30000)
    t = _rec(trialing)["billing"]
    assert t["monthly"] == 0 and t["committed_monthly"] == 349
    d = _rec(discounted)["billing"]
    assert d["monthly"] == pytest.approx(279.2) and d["list_mismatch"] is None
    o = _rec(odd)["billing"]
    assert o["monthly"] == 300 and o["list_mismatch"]["difference"] == -49
    k = admin_ops.overview()["kpis"]
    assert k["mrr_committed"] == 349 and k["mrr"] == pytest.approx(579.2) and k["mrr_mismatches"] == 1


def test_without_the_mirror_list_price_counts_once_per_group_and_says_so(db_path):
    a = _one(db_path, "Solo", billing_status="active")
    b = _one(db_path, "Twin 1", billing_status="active", location_group="Twins", owner_email="t@x.test")
    c = _one(db_path, "Twin 2", billing_status="active", location_group="Twins", owner_email="t@x.test")
    k = admin_ops.overview()["kpis"]
    assert k["mrr"] == 349 * 2 and k["mrr_source"] == "list" and k["mrr_fallback_accounts"] == 2
    assert _rec(c)["billing"]["billed_by"]["restaurant_id"] == b and _rec(a)["billing"]["mrr_source"] == "list"


def test_status_disagreement_and_module_mismatch_raise_issues(db_path):
    _mirror(db_path)
    gone = _one(db_path, "Gone In Stripe", billing_status="active")
    _sub(db_path, gone, "sub_g", status="canceled")
    mixed = _one(db_path, "Mixed", billing_status="active", module_labor=1)
    _sub(db_path, mixed, "sub_m", modules="reviews")
    keys = {i["key"] for i in admin_ops.overview()["issues"]}
    assert f"{gone}:stripe_mismatch" in keys and f"{mixed}:modules_mismatch" in keys


def test_the_billing_tab_never_calls_stripe_per_customer(db_path, monkeypatch):
    _mirror(db_path)
    rid = _one(db_path, "Live", billing_status="active", stripe_customer_id="cus_live123456")
    _sub(db_path, rid, "sub_live")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_unused")
    monkeypatch.setattr(config, "stripe_api", lambda *a, **k: pytest.fail("Stripe called from the fleet list"))
    b = admin_ops.billing()
    row = next(r for r in b["rows"] if r["restaurant_id"] == rid)
    assert b["ok"] and b["live_source"] == "mirror" and row["live"]["status"] == "active"
    assert row["margin"]["revenue_monthly"] == 349 and b["margin"]["arpa"] == 349


def test_one_clients_live_stripe_state_is_its_own(db_path, monkeypatch):
    rid = _one(db_path, "Live One", billing_status="active", stripe_customer_id="cus_one12345678")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_unused")

    class Sub:
        @staticmethod
        def list(**kw):
            assert kw["customer"] == "cus_one12345678"
            return {"data": [{"id": "sub_1", "status": "past_due", "items": {"data": [
                {"price": {"unit_amount": 34900, "recurring": {"interval": "month"}}, "quantity": 1}]},
                "metadata": {"module_keys": "reviews"}}]}

    class Fake:
        Subscription = Sub
    monkeypatch.setattr(config, "stripe_api", lambda key: Fake)
    out = admin_ops.billing_live(rid)
    assert out["ok"] and out["subscriptions"][0]["status"] == "past_due" and out["subscriptions"][0]["amount"] == 349

    class Boom:
        class Subscription:
            @staticmethod
            def list(**kw):
                raise RuntimeError("Stripe timed out")
    monkeypatch.setattr(config, "stripe_api", lambda key: Boom)
    out = admin_ops.billing_live(rid)
    assert out["ok"] is False and "timed out" in out["error"]


def test_a_dispute_pause_is_raised_and_carries_its_reason(db_path):
    rid = _one(db_path, "Disputed", billing_status="paused")
    _sql(db_path, "INSERT INTO admin_events (source, event_type, restaurant_id, summary, created_at) "
                  "VALUES ('stripe', 'charge.dispute.created', ?, 'x', datetime('now','-1 hours'))", (rid,))
    rec = _rec(rid)
    assert rec["billing"]["pause_reason"] == "dispute" and rec["billing"]["pause_reason_inferred"] is True
    issue = next(i for i in rec["issues"] if i["key"] == f"{rid}:paused:dispute")
    assert issue["severity"] == "critical"
    _sql(db_path, "ALTER TABLE restaurants ADD COLUMN pause_reason TEXT")
    selfp = _one(db_path, "Vacation", billing_status="paused")
    _sql(db_path, "UPDATE restaurants SET pause_reason='self', paused_until='2026-10-15' WHERE id=?", (selfp,))
    rec = _rec(selfp)
    assert rec["billing"]["pause_reason"] == "self" and rec["billing"]["paused_until"] == "2026-10-15"
    assert not any(":paused:" in i["key"] for i in rec["issues"])


def test_signed_and_never_paid_warns_at_seven_days_and_is_critical_at_thirty(db_path):
    rid = _one(db_path, "Signed Co", billing_status="trial", contract_status="signed")
    _sql(db_path, "UPDATE restaurants SET contract_status='signed', created_at='2026-07-01T09:00:00' WHERE id=?", (rid,))
    _sql(db_path, "INSERT INTO admin_events (source, event_type, restaurant_id, summary, created_at) "
                  "VALUES ('docusign', 'contract.signed', ?, 'Contract signed', datetime('now','-10 days'))", (rid,))
    rec = _rec(rid)
    issue = next(i for i in rec["issues"] if i["key"] == f"{rid}:signed_unpaid")
    assert issue["severity"] == "warning" and issue["title"] == "Signed 10 days ago, never paid"
    assert issue["action_route"] == f"/admin/resend-payment/{rid}"
    assert rec["billing"]["days_since_signed"] == 10 and rec["billing"]["days_in_trial"] >= 80
    _sql(db_path, "UPDATE admin_events SET created_at=datetime('now','-31 days')")
    issue = next(i for i in _rec(rid)["issues"] if i["key"] == f"{rid}:signed_unpaid")
    assert issue["severity"] == "critical"


def test_trial_conversion_reads_converted_at(db_path):
    assert admin_ops.overview()["kpis"]["trial_conversion"]["available"] is False
    _sql(db_path, "ALTER TABLE restaurants ADD COLUMN converted_at TEXT")
    a = _one(db_path, "Conv A", billing_status="active")
    _one(db_path, "Conv B", billing_status="trial")
    _sql(db_path, "UPDATE restaurants SET converted_at=datetime('now') WHERE id=?", (a,))
    tc = admin_ops.overview()["kpis"]["trial_conversion"]
    assert tc["available"] and tc["started"] == 2 and tc["converted"] == 1 and tc["rate_pct"] == 50.0


def test_the_daily_snapshot_is_one_row_per_day_and_refuses_on_errors(db_path):
    a = _one(db_path, "Snap A", billing_status="active")
    _one(db_path, "Snap B", billing_status="trial")
    out = admin_ops.snapshot_business_metrics()
    assert out["ok"] == 1 and out["failed"] == 0 and out["hit_bound"] is False
    again = admin_ops.snapshot_business_metrics()
    rows = _rows(db_path, "SELECT * FROM business_metrics_daily")
    assert again["ok"] == 1 and len(rows) == 1
    row = rows[0]
    assert row["mrr"] == 349 and row["paying_accounts"] == 1 and row["trial_accounts"] == 1 and row["signups"] == 2
    assert json.loads(row["state_json"])["paying"] == [a]
    assert _rows(db_path, "SELECT restaurant_id FROM account_risk_state") == [{"restaurant_id": a}]
    bm = admin_ops.business_metrics(days=30)
    assert bm["rows"][0]["cost_by_vendor"] == {} and "state_json" not in bm["rows"][0]
    _sql(db_path, "DROP TABLE alert_log")
    bad = admin_ops.snapshot_business_metrics()
    assert bad["failed"] == 1 and "alert_log" in bad["error"]


def test_vendor_costs_are_entered_audited_and_reach_the_margin(db_path):
    assert admin_ops.set_vendor_cost("nope", "2026-09", 10)["ok"] is False
    assert admin_ops.set_vendor_cost("railway", "2026-9", 10)["ok"] is False
    month = admin_ops._now_ct().strftime("%Y-%m")
    assert admin_ops.set_vendor_cost("railway", month, 20, "hobby plan", "will")["ok"]
    assert admin_ops.set_vendor_cost("railway", month, 25, None, "will")["amount_usd"] == 25
    vc = admin_ops.vendor_costs(months=2)
    assert vc["by_month"][-1]["entered"] == {"railway": 25.0}
    assert _rows(db_path, "SELECT COUNT(*) AS n FROM admin_events WHERE event_type='vendor_cost.set'")[0]["n"] == 2
    _one(db_path, "Margin Co", billing_status="active")
    m = admin_ops.billing()["margin"]
    assert m["fixed_cost_month"] == 25 and m["gross_margin"] == pytest.approx(349 - 25)


def test_the_overview_series_are_computed_on_the_server(db_path):
    rid = _one(db_path, "Series Co", billing_status="active")
    s = admin_ops.overview()["series"]
    assert s["source"] == "reconstructed" and "Reconstructed" in s["note"]
    assert len(s["signups"]) == 12 and s["signups"][-1]["n"] == 1 and len(s["mrr"]) == 6
    assert s["mrr"][-1]["mrr"] == 349
    admin_ops.snapshot_business_metrics()
    s = admin_ops.overview()["series"]
    assert s["source"] == "snapshots" and s["mrr"][-1]["mrr"] == 349 and rid
