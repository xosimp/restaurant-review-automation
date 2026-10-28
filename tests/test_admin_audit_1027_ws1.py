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


# ── K-01: fetch coverage counts only restaurants the fetch covers ──────────

def test_k01_fetch_behind_leaves_out_restaurants_the_fetch_never_covers(db_path):
    a = _rest(db_path, "Unconnected Trial", billing_status="trial")                      # no place id, no GBP
    b = _rest(db_path, "Fetch Off Grill", billing_status="active", google_place_id="ChIJx")  # switched off
    c = _rest(db_path, "Live Places Cafe", billing_status="active", google_place_id="ChIJy")
    old = _ago_utc(days=10)
    _sql(db_path, "UPDATE restaurants SET created_at=?, reviews_live=0, gmb_refresh_token=NULL", (old,))
    _sql(db_path, "UPDATE restaurants SET reviews_live=1 WHERE id=?", (c,))
    ct2 = (admin_ops._now_ct() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
    _sql(db_path, "UPDATE restaurants SET last_fetched_at=? WHERE id IN (?, ?)", (ct2, b, c))
    ov = admin_ops.overview()
    assert ov["kpis"]["fetch_behind"] == 1           # only the store the fetch covers
    recs = {r["id"]: r for r in admin_ops.clients()["clients"]}
    assert not any(i["key"].endswith(":fetch_behind") for i in recs[b]["issues"])
    assert any(i["key"] == f"{c}:fetch_behind" for i in recs[c]["issues"])
    assert recs[a]["review_fetch_eligible"] is False and recs[c]["review_fetch_eligible"] is True


# ── K-02: the AI-budget watch reads the gate's own arithmetic ──────────────

def test_k02_a_trial_schedule_day_is_not_a_spent_ai_day(db_path):
    rid = _rest(db_path, "Trial Kitchen", billing_status="trial")
    ai_utils.log_ai_usage(rid, "labor_schedule", "claude-sonnet-4-5", 0, 0, db_path=db_path, cost_usd=5.5)
    gate = ai_utils.ai_budget_status(rid, db_path=db_path)
    watch = [x for x in admin_ops.budget_watch() if x["restaurant_id"] == rid]
    assert not gate["day"]["over"]
    assert not any(x["scope"] == "ai_day" for x in watch)
    rec = next(r for r in admin_ops.clients()["clients"] if r["id"] == rid)
    assert not any(":ai_budget:" in i["key"] for i in rec["issues"])
    # Past the schedule allowance itself, the watch says so in its own words.
    ai_utils.log_ai_usage(rid, "labor_schedule", "claude-sonnet-4-5", 0, 0, db_path=db_path, cost_usd=7.0)
    ai_utils._budget_cache.clear()
    sched = [x for x in admin_ops.budget_watch() if x["restaurant_id"] == rid and x["scope"] == "schedule_day"]
    assert sched and sched[0]["over"] == ai_utils.ai_budget_status(rid, db_path=db_path)["schedule_day"]["over"]
    assert admin_ops.ai_budget_issues(sched)[0]["title"] == "Paused: today's schedule AI allowance is spent"


def test_k02_a_tenant_override_is_the_ceiling_the_watch_reads(db_path):
    import model_gateway
    rid = _rest(db_path, "Big Roster", billing_status="trial")
    ai_utils.log_ai_usage(rid, "insights", "claude-sonnet-4-5", 0, 0, db_path=db_path, cost_usd=6.0)
    model_gateway.set_tenant_budget(rid, daily_usd=100.0, monthly_usd=1000.0, reason="t", set_by="t", db_path=db_path)
    model_gateway._budget_memo.clear()
    assert not ai_utils.ai_budget_status(rid, db_path=db_path)["day"]["over"]
    assert not [x for x in admin_ops.budget_watch() if x["restaurant_id"] == rid]


# ── K-03: a job run that failed without raising reaches Overview ───────────

def test_k03_a_failed_run_is_a_failed_job_on_overview(db_path):
    name = "prune_ledgers"
    ops.run_job(name, lambda: {"attempted": 3, "ok": 0, "failed": 3, "skipped": 0, "hit_bound": False}, db_path=db_path)
    ov = admin_ops.overview()
    k = ov["kpis"]
    row = next(j for j in admin_ops.jobs()["jobs"] if j["job"] == name)
    assert row["state"] == "failed"
    assert k["job_failures_24h"] == 1 and k["jobs_failing"] == 1 and k["jobs_runs_failed"] == [name]
    assert any(i["key"] == f"job:{name}" and i["severity"] == "critical" for i in ov["issues"])
    assert admin_ops.badges()["kpis"]["job_failures_24h"] == 1


def test_k03_a_partial_run_is_counted_apart(db_path):
    name = "prune_ledgers"
    ops.run_job(name, lambda: {"attempted": 4, "ok": 3, "failed": 1, "skipped": 0, "hit_bound": False}, db_path=db_path)
    k = admin_ops.overview()["kpis"]
    assert k["jobs_partial"] == 1 and k["jobs_runs_partial"] == [name] and k["jobs_failing"] == 0


# ── K-04: gross margin is one consistent sum ────────────────────────────────

def test_k04_gross_margin_counts_each_cost_once_and_only_the_customers(db_path):
    rid = _rest(db_path, "Margin Bistro", billing_status="active")
    demo = _rest(db_path, "Showcase Demo", billing_status="trial", is_demo=1)
    ai_utils.log_ai_usage(rid, "insights", "claude-sonnet-4-5", 0, 0, db_path=db_path, cost_usd=2.0)
    ai_utils.log_ai_usage(demo, "insights", "claude-sonnet-4-5", 0, 0, db_path=db_path, cost_usd=5.0)
    ai_utils.log_ai_usage(None, "sales_audit", "claude-sonnet-4-5", 0, 0, db_path=db_path, cost_usd=3.0)
    month = admin_ops._now_ct().strftime("%Y-%m")
    _sql(db_path, "INSERT INTO vendor_costs (vendor, month, amount_usd) VALUES ('anthropic', ?, 10)", (month,))
    _sql(db_path, "INSERT INTO vendor_costs (vendor, month, amount_usd) VALUES ('railway', ?, 20)", (month,))
    b = admin_ops.billing()
    m = b["margin"]
    assert m["variable_customer_30d"] == 2.0 and m["variable_platform_30d"] == 3.0
    assert m["variable_internal_30d"] == 5.0 and m["variable_cost_30d"] == 5.0
    assert m["fixed_cost_month"] == 20.0 and m["entered_metered"] == {"anthropic": 10.0}
    assert m["gross_margin"] == pytest.approx(b["mrr"] - 5.0 - 20.0)


def test_k04_a_failed_vendor_cost_read_is_unknown_not_zero(db_path, monkeypatch):
    _rest(db_path, "Margin Bistro", billing_status="active")
    real = admin_ops._rows_dict

    def flaky(conn, sql, args=(), label=None, optional=False):
        if label == "vendor_costs":
            admin_ops._note_failure("vendor_costs", RuntimeError("database is locked"))
            return []
        return real(conn, sql, args, label=label, optional=optional)
    monkeypatch.setattr(admin_ops, "_rows_dict", flaky)
    b = admin_ops.billing()
    assert b["margin"]["fixed_cost_month"] is None and b["margin"]["gross_margin"] is None
    assert b["margin"]["fixed_read_failed"] is True
    assert any(e["query"] == "vendor_costs" for e in b["errors"])


# ── K-06: a covered group location is judged with its payer ────────────────

def test_k06_a_covered_location_has_no_contract_of_its_own_to_chase(db_path):
    payer = _rest(db_path, "Group Main", billing_status="active", owner_email="own@ex.com", location_group="Grp")
    sib = _rest(db_path, "Group Second", billing_status="active", owner_email="own@ex.com", location_group="Grp")
    _sql(db_path, "UPDATE restaurants SET created_at=?", (_ago_utc(days=10),))
    _sql(db_path, "UPDATE restaurants SET contract_status='signed' WHERE id=?", (payer,))
    recs = {r["id"]: r for r in admin_ops.clients()["clients"]}
    sib_rec = recs[sib]
    assert sib_rec["billing"]["billed_by"]["restaurant_id"] == payer
    assert f"{sib}:contract" not in [i["key"] for i in sib_rec["issues"]]
    assert next(s for s in sib_rec["onboarding"]["steps"] if s["key"] == "contract")["done"]
    assert sib_rec["billing"]["contract_covered"] is True
    assert sib_rec["billing"]["contract_covered_by"]["restaurant_id"] == payer
    b = admin_ops.billing()
    row = next(r for r in b["rows"] if r["restaurant_id"] == sib)
    assert row["contract_covered"] is True and b["totals"]["contract_unsigned"] == 0


def test_k06_a_covered_location_reads_its_groups_owner_and_activity(db_path):
    payer = _rest(db_path, "Duo Main", billing_status="active", owner_email="duo@ex.com", location_group="Duo")
    sib = _rest(db_path, "Duo Second", billing_status="active", owner_email="duo@ex.com", location_group="Duo")
    _sql(db_path, "UPDATE restaurants SET created_at=?", (_ago_utc(days=40),))
    now_ct = admin_ops._now_ct().strftime("%Y-%m-%dT%H:%M:%S")
    _sql(db_path, "INSERT INTO users (restaurant_id, username, email, password_hash, role, is_admin, is_active, last_login) "
                  "VALUES (?,?,?,?,?,?,?,?)", (payer, "duo", "duo@ex.com", "x", "owner", 0, 1, now_ct))
    recs = {r["id"]: r for r in admin_ops.clients()["clients"]}
    s = recs[sib]
    assert (s["owner"] or {}).get("username") == "duo" and s["owner_via"] == payer
    assert s["last_active"] is not None
    assert next(x for x in s["onboarding"]["steps"] if x["key"] == "login")["done"]
    assert "never active" not in s["churn_risk"]["reasons"]
    # A lone location with no login keeps reading none.
    lone = _rest(db_path, "Lone Diner", billing_status="active")
    assert next(r for r in admin_ops.clients()["clients"] if r["id"] == lone)["owner"] is None


# ── K-07: one paying-account definition ─────────────────────────────────────

def test_k07_paying_accounts_is_one_figure_everywhere(db_path):
    for i in range(3):
        _rest(db_path, f"Brand Loc{i}", billing_status="active", owner_email="grp@ex.com", location_group="Brand")
    _rest(db_path, "Solo Diner", billing_status="past_due")
    _rest(db_path, "Gone Grill", billing_status="canceled")
    ov = admin_ops.overview()
    k = ov["kpis"]
    admin_ops.snapshot_business_metrics(db_path=db_path)
    c = get_conn(db_path)
    snap = dict(c.execute("SELECT paying_accounts FROM business_metrics_daily").fetchone())
    c.close()
    assert k["paying_accounts"] == snap["paying_accounts"] == 2      # one group payer + the past-due store
    assert k["covered_locations"] == 2 and k["accounts_live"] == 4
    assert k["active"] == 3                                           # the per-location status count is unchanged


# ── K-08: one "failed AI call" ──────────────────────────────────────────────

def test_k08_failed_ai_calls_mean_one_thing_on_every_screen(db_path):
    rid = _rest(db_path, "Fail Grill", billing_status="active")
    ai_utils.log_ai_usage(rid, "insights", "claude-sonnet-4-5", 0, 0, db_path=db_path, status="error", error="boom")
    for _ in range(3):
        ai_utils.log_ai_usage(rid, "draft_reply", "claude-sonnet-4-5", 10, 10, db_path=db_path, outcome="refused")
    ai_utils.log_ai_usage(rid, "places_details", "google-places-details", 0, 0, db_path=db_path, status="error",
                          error="timeout", vendor="google_places")
    ai_utils.log_ai_usage(rid, "visibility", "sonar", 0, 0, db_path=db_path, status="error", error="x", vendor="perplexity")
    # A stream cut part-way and retried: its own row, not a second failure.
    ai_utils.log_ai_usage(rid, "insights", "claude-sonnet-4-5", 5, 5, db_path=db_path, status="error", outcome="error",
                          reason="stream_cut")
    ov = admin_ops.overview()["kpis"]
    tab = admin_ops.ai_ops(30)["failed"]
    rec = next(r for r in admin_ops.clients()["clients"] if r["id"] == rid)["ai"]
    assert ov["ai_failures_24h"] == tab["n_24h"] == rec["failed_7d"] == rec["failed_30d"] == 1
    assert tab["not_ok_24h"] == 3 and tab["scope"] == ov["ai_failed_scope"] == rec["failed_scope"] == "Claude"


# ── K-09: each "today" names its clock, and the AI page carries both ───────

def test_k09_the_ai_page_carries_the_owner_day_beside_the_budget_day(db_path):
    rid = _rest(db_path, "Clock Cafe", billing_status="active")
    now = datetime.now(timezone.utc)
    utc_mid = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ct_mid = admin_ops._today_start().astimezone(timezone.utc)
    lo, hi = sorted([utc_mid, ct_mid])
    mid = lo + (hi - lo) / 2
    ai_utils.log_ai_usage(rid, "insights", "claude-sonnet-4-5", 0, 0, db_path=db_path, cost_usd=1.0)
    _sql(db_path, "UPDATE ai_usage SET created_at=?", (mid.strftime("%Y-%m-%d %H:%M:%S"),))
    ov = admin_ops.overview()["kpis"]
    ai = admin_ops.ai_ops(30)
    assert ai["totals"]["today_ct"]["calls"] == ov["ai_calls_today"]
    assert ai["totals"]["today"]["calls"] != ai["totals"]["today_ct"]["calls"]
    assert "UTC" in ai["labels"]["today"] and "Central" in ai["labels"]["today_ct"]
    assert "Central" in ov["ai_today_label"]


# ── K-11: the Integrations tab counts what the cards above it count ────────

def test_k11_integration_counts_are_customer_connections(db_path):
    _rest(db_path, "Real One", billing_status="active")
    d = _rest(db_path, "Demo Store", billing_status="trial", is_demo=1)
    _sql(db_path, "UPDATE restaurants SET toast_restaurant_guid='g1', toast_sync_error='boom' WHERE id=?", (d,))
    k = admin_ops.overview()["kpis"]
    ig = admin_ops.integrations(page=1)
    assert k["integrations_failing"] == ig["counts"]["error"] == 0
    assert ig["counts_internal"]["error"] == 1
    assert not any(r["restaurant_id"] == d for r in ig["rows"])
    assert any(r["restaurant_id"] == d for r in admin_ops.integrations()["rows"])   # internal callers keep every row


# ── K-12: per-account cost and retention have the right bases ──────────────

def test_k12_customer_cost_and_ever_paid_are_served(db_path):
    rid = _rest(db_path, "Paying Place", billing_status="active")
    trial = _rest(db_path, "Trial Place", billing_status="trial")
    demo = _rest(db_path, "Demo Place", billing_status="trial", is_demo=1)
    ai_utils.log_ai_usage(rid, "insights", "claude-sonnet-4-5", 0, 0, db_path=db_path, cost_usd=2.0)
    ai_utils.log_ai_usage(demo, "insights", "claude-sonnet-4-5", 0, 0, db_path=db_path, cost_usd=5.0)
    ai_utils.log_ai_usage(None, "sales_audit", "claude-sonnet-4-5", 0, 0, db_path=db_path, cost_usd=3.0)
    t = admin_ops.ai_ops(30)["totals"]
    assert t["cost"] == 10.0 and t["customer_cost"] == 2.0 and t["customer_accounts_with_cost"] == 1
    rows = {r["id"]: r for r in admin_ops.clients_page(per_page=200)["items"]}
    assert rows[rid]["ever_paid"] is True and rows[trial]["ever_paid"] is False


# ── K-13 / K-14 / K-16 (P3) ─────────────────────────────────────────────────

def test_k13_instagram_has_no_sync_so_it_is_never_never_synced(db_path):
    rid = _rest(db_path, "Insta Cafe", billing_status="active")
    future = (datetime.now(timezone.utc) + timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    _sql(db_path, "UPDATE restaurants SET ig_token='tok', ig_token_expires=? WHERE id=?", (future, rid))
    rows = [r for r in admin_ops.integrations()["rows"] if r["restaurant_id"] == rid and r["integration"] == "instagram"]
    assert rows and rows[0]["state"] == "connected" and rows[0]["freshness"] == "—"
    assert admin_ops.integrations(page=1)["counts"]["never"] == 0


def test_k14_the_clients_daily_ai_bars_leave_blocked_calls_out(db_path):
    rid = _rest(db_path, "Bars Bistro", billing_status="active")
    ai_utils.log_ai_usage(rid, "insights", "claude-sonnet-4-5", 0, 0, db_path=db_path, cost_usd=1.0)
    ai_utils.log_blocked(rid, "insights", "claude-sonnet-4-5", "budget", detail="daily budget")
    d = admin_ops.client_detail(rid)
    assert sum(x["calls"] for x in d["ai"]["daily"]) == d["client"]["ai"]["calls_30d"] == 1


def test_k16_a_support_only_account_is_internal_and_learning_exclusion_is_named(db_path):
    sup = _rest(db_path, "Support Desk", billing_status="trial")
    _sql(db_path, "INSERT INTO users (restaurant_id, username, email, password_hash, role, is_admin, is_active) "
                  "VALUES (?,?,?,?,?,?,?)", (sup, "help", "help@ex.com", "x", "support", 0, 1))
    named = _rest(db_path, "Preview Test", billing_status="trial")
    recs = {r["id"]: r for r in admin_ops.clients()["clients"]}
    assert recs[sup]["segment"] == "internal"
    assert recs[named]["segment"] == "customer" and recs[named]["learning_exclusion"] == "test_name"


def test_k15_a_store_with_unstaged_steps_open_is_finishing_not_live():
    import onboarding
    steps = [{"key": "contract", "done": True}, {"key": "login", "done": True},
             {"key": "voice", "done": False}, {"key": "billing", "done": True}]
    assert onboarding.stage_of(steps) == "finishing"
    assert onboarding.stage_of([dict(s, done=True) for s in steps]) == "live"
    b = onboarding.board([{"id": 1, "name": "Slow Setup", "steps": steps,
                           "days_since_signup": onboarding.ONBOARDING_STUCK_DAYS + 5}])
    assert [x["stage"] for x in b["stuck"]] == ["finishing"]
    assert next(s for s in b["stages"] if s["key"] == "live")["count"] == 0


# ── Coordinator addition: Reinstate is offered for a bounce only ───────────

def test_reinstate_is_offered_only_for_a_bounce_suppression(db_path):
    bounced = _rest(db_path, "Bounce Bar", billing_status="active", owner_email="b@ex.com")
    opted = _rest(db_path, "Optout Oven", billing_status="active", owner_email="o@ex.com")
    models.suppress_email("b@ex.com", "bounced", db_path=db_path)
    models.suppress_email("o@ex.com", "complained", db_path=db_path)
    recs = {r["id"]: r for r in admin_ops.clients()["clients"]}
    b = next(i for i in recs[bounced]["issues"] if ":suppressed:" in i["key"])
    o = next(i for i in recs[opted]["issues"] if ":suppressed:" in i["key"])
    assert b["action_route"] == "/admin/api/suppressions/reinstate" and b["action_payload"] == {"email": "b@ex.com"}
    assert o["action_route"] is None and o["action_kind"] == "link" and "opted out" in o["detail"]


# ── O5: a failed read is flagged, never drawn as zero or empty ─────────────

def _fail_label(monkeypatch, label_or_table):
    real_rows, real_one = admin_ops._rows_dict, admin_ops._one_dict

    def hit(sql, label):
        return (label or admin_ops._label_of(sql)) == label_or_table

    def rows(conn, sql, args=(), label=None, optional=False):
        if hit(sql, label):
            admin_ops._note_failure(label or admin_ops._label_of(sql), RuntimeError("database is locked"))
            return []
        return real_rows(conn, sql, args, label=label, optional=optional)

    def one(conn, sql, args=(), label=None, optional=False):
        if hit(sql, label):
            admin_ops._note_failure(label or admin_ops._label_of(sql), RuntimeError("database is locked"))
            return None
        return real_one(conn, sql, args, label=label, optional=optional)
    monkeypatch.setattr(admin_ops, "_rows_dict", rows)
    monkeypatch.setattr(admin_ops, "_one_dict", one)


def test_o5_a_failed_job_failures_read_reaches_the_jobs_payload(db_path, monkeypatch):
    _fail_label(monkeypatch, "job_failures")
    out = admin_ops.jobs()
    assert out["ok"] is True and out["failures"] == []
    assert "job_failures" in out["read_failed"]
    assert any(e["query"] == "job_failures" for e in out["query_errors"])


def test_o5_a_failed_ai_read_reaches_the_ai_payload(db_path, monkeypatch):
    _fail_label(monkeypatch, "ai_usage")
    out = admin_ops.ai_ops(30)
    assert "ai_usage" in out["read_failed"] and out["errors"]
    client = admin_ops.ai_client(_rest(db_path, "Any Place", billing_status="active"))
    assert "ai_usage" in client["read_failed"]


# ── O4: coverage and failure share on each job row ──────────────────────────

def test_o4_a_sweep_cut_short_every_pass_and_a_failing_share_are_on_the_job_row(db_path):
    name = "prune_ledgers"
    for _ in range(3):
        ops.run_job(name, lambda: {"attempted": 600, "ok": 600, "failed": 0, "skipped": 0, "hit_bound": True,
                                   "eligible": 1500}, db_path=db_path)
    row = next(j for j in admin_ops.jobs()["jobs"] if j["job"] == name)
    assert row["coverage"]["attempted"] == 600 and row["coverage"]["eligible"] == 1500
    assert row["coverage"]["hit_bound"] is True and row["bound_hit_streak"] == 3 and row["bound_capped"] is True
    out = admin_ops.jobs()
    assert name in out["jobs_bound_capped"]
    for _ in range(2):
        ops.run_job(name, lambda: {"attempted": 10, "ok": 2, "failed": 8, "skipped": 0, "hit_bound": False},
                    db_path=db_path)
    row = next(j for j in admin_ops.jobs()["jobs"] if j["job"] == name)
    assert row["failed_share"] == 0.8 and row["failing"] == {"why": "failing", "runs": ops.FAILING_ALERT_RUNS}
    assert row["bound_hit_streak"] == 0


# ── F9: a Run now request nothing took reads expired ───────────────────────

def test_f9_an_untaken_run_now_request_reads_expired(db_path):
    _sql(db_path, "INSERT INTO job_run_requests (job, requested_by, requested_at) VALUES ('prune_ledgers', 'will', ?)",
         (_ago_utc(minutes=ops.RUN_REQUEST_TTL_MINUTES + 15),))
    _sql(db_path, "INSERT INTO job_run_requests (job, requested_by) VALUES ('prune_ledgers', 'will')")
    out = admin_ops.jobs()
    states = [r["effective_status"] for r in out["requests"]]
    assert states == ["pending", "expired"]
    assert out["requests"][1]["expired_reason"].startswith("Not started within")
    assert out["requests_24h"]["expired"] == 1 and out["requests_24h"]["pending"] == 1


def test_f9_run_now_says_when_the_scheduler_is_not_beating(db_path, monkeypatch, scheduler_heartbeat):
    import scheduler
    scheduler_heartbeat(120)
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    job = next(n for n, s in admin_ops.RUNNABLE_JOBS.items() if not s["sends"])
    out = admin_ops.run_job_now(job, "will")
    assert out["queued"] is True and out["scheduler_state"] == "stale" and "expires unstarted" in out["message"]


# ── F6: an RPOWER token refusal names the admin's step, not the owner's ────

def test_f6_rpower_refusal_tells_the_admin_to_rotate_and_the_owner_nothing(db_path):
    import data_health
    rid = _rest(db_path, "Rpower Room", billing_status="active")
    _sql(db_path, "UPDATE restaurants SET rpower_token='t', rpower_store_mid='m', rpower_auth_failed_at=? WHERE id=?",
         (_ago_utc(hours=2), rid))
    rec = next(r for r in admin_ops.clients()["clients"] if r["id"] == rid)
    rp = next(i for i in rec["integrations"] if i["key"] == "rpower")
    assert "Rotate token" in rp["error"] and "until the owner reconnects" not in rp["error"]
    hint = data_health.fix_hint("pos", {"provider": "rpower", "error_class": "auth"})
    assert "Reconnect" not in hint and "Nothing to do on your side" in hint
    # Any other POS's refused token is still the owner's to reconnect.
    assert data_health.fix_hint("pos", {"provider": "toast", "error_class": "auth"}).startswith("Reconnect Toast")


# ── P-01: the platform issues are kept with the memo; MAX reads windowed ───

def test_p01_the_rail_poll_reuses_the_platform_issues_of_its_memo(db_path, monkeypatch):
    from flask import Flask
    _rest(db_path, "Poll Place", billing_status="active")
    admin_ops.invalidate_fleet_cache()
    calls = []
    real = admin_ops._platform_issues
    monkeypatch.setattr(admin_ops, "_platform_issues", lambda recs, d: (calls.append(1), real(recs, d))[1])
    app = Flask(__name__)
    with app.test_request_context("/admin/api/badges"):
        a = admin_ops.badges()
        b = admin_ops.badges()
        admin_ops.issues_page()
    assert len(calls) == 1 and a["counts"] == b["counts"]
    # Off a request thread every call computes fresh.
    admin_ops.badges()
    assert len(calls) == 2
    admin_ops.invalidate_fleet_cache()


def test_p01_derived_resend_events_are_read_on_a_window(db_path):
    old = _ago_utc(days=10)
    _sql(db_path, "INSERT INTO email_log (restaurant_id, email_type, to_email, subject, sent_at, status, opened_at) "
                  "VALUES (NULL, 'digest', 'a@ex.com', 's', ?, 'delivered', ?)", (old, old))
    conn = get_conn(db_path)
    try:
        wh = admin_ops._webhook_health(conn)
    finally:
        conn.close()
    resend = next(p for p in wh["providers"] if p["provider"] == "resend")
    assert resend["source"] != "derived" or resend["last_verified_at"] is None


# ── P-03: the Email chart's daily counts come from the database ────────────

def test_p03_email_daily_counts_are_grouped_in_sql_on_chicago_days(db_path, monkeypatch):
    now = datetime.now(timezone.utc)
    for i, status in enumerate(("delivered", "bounced", "failed", "delivered")):
        _sql(db_path, "INSERT INTO email_log (restaurant_id, email_type, to_email, subject, sent_at, status) "
                      "VALUES (NULL, 'digest', ?, 's', ?, ?)",
             (f"p{i}@ex.com", (now - timedelta(hours=1 + i)).strftime("%Y-%m-%d %H:%M:%S"), status))
    seen = []
    real = admin_ops._rows_dict
    monkeypatch.setattr(admin_ops, "_rows_dict", lambda conn, sql, *a, **k: (seen.append(sql), real(conn, sql, *a, **k))[1])
    out = admin_ops.emails()
    assert not any(sql.strip().startswith("SELECT sent_at, status FROM email_log") for sql in seen)
    assert sum(d["n"] for d in out["daily"]) == 4 and sum(d["failed"] for d in out["daily"]) == 2
    assert len(out["daily"]) == 30 and out["daily"][-1]["day"] == admin_ops._now_ct().date().isoformat()


# ── P-04: the calibration reads take the heavy slot; the fleet view is capped

def test_p04_calibration_reads_are_bounded(db_path, monkeypatch):
    from flask import Flask
    out = admin_ops.confidence_calibration(days=365)
    assert out["days"] == admin_ops.FLEET_CALIBRATION_MAX_DAYS and out["days_capped"] is True
    rid = _rest(db_path, "Own View", billing_status="active")
    assert admin_ops.confidence_calibration(days=365, restaurant_id=rid)["days"] == 365
    assert admin_ops.recommendation_calibration(days=365)["days"] == admin_ops.FLEET_CALIBRATION_MAX_DAYS
    monkeypatch.setattr(admin_ops, "HEAVY_WAIT_SECONDS", 0.01)
    assert admin_ops._heavy_slots.acquire(timeout=1)
    try:
        with Flask(__name__).test_request_context("/admin/api/calibration"):
            for fn in (admin_ops.confidence_calibration, admin_ops.recommendation_calibration):
                with pytest.raises(admin_ops.AdminBusy):
                    fn(days=365)
    finally:
        admin_ops._heavy_slots.release()


# ── P-06: the DSR sweep asks about the restaurant row it already holds ─────

def test_p06_connected_provider_for_reads_the_row_in_hand(db_path):
    import pos
    rid = _rest(db_path, "Row In Hand", billing_status="active")
    r = models.get_restaurant(rid)
    r.toast_restaurant_guid, r.toast_client_id, r.toast_client_secret = "g", "c", "s"
    assert pos.connected_provider(rid) == (None, None)            # the stored row has no credentials
    assert pos.connected_provider_for(r)[0] == "toast"             # the row in hand is what was read
    assert pos.connected_provider_for({"id": rid}) == (None, None)
    from dsr import pipeline
    assert "connected_provider_for(r)" in open(pipeline.__file__).read()


# ── P-07: the fleet read leaves the forecast blobs out; the memo keeps no rows

def test_p07_the_fleet_read_is_an_explicit_column_list(db_path):
    from flask import Flask
    rid = _rest(db_path, "Weather Grill", billing_status="active")
    _sql(db_path, "UPDATE restaurants SET weather_hourly_json=?, weather_cache_json=?, competitor_intel=? WHERE id=?",
         ("[" + "1," * 5000 + "1]", "{}", json.dumps({"rivals": ["x"]}), rid))
    d = admin_ops._load_everything()
    row = next(r for r in d["rests"] if r["id"] == rid)
    assert "weather_hourly_json" not in row and "weather_cache_json" not in row
    assert row["competitor_intel"] == 1 and "google_place_id" in row and "billing_status" in row
    rec = next(r for r in admin_ops.clients()["clients"] if r["id"] == rid)
    assert next(m for m in rec["modules"] if m["key"] == "intel")["receiving"] is True
    admin_ops.invalidate_fleet_cache()
    with Flask(__name__).test_request_context("/admin/api/clients"):
        admin_ops.clients()
        assert "rests" not in admin_ops._fleet_state["memo"]["d"]
    admin_ops.invalidate_fleet_cache()


# ── P-08: the client page's siblings carry only what the page draws ────────

def test_p08_siblings_are_slim_in_the_record_shape(db_path):
    a = _rest(db_path, "Corner Bar", billing_status="active", owner_email="o@ex.com", location_group="Corner",
              location_name="Downtown")
    b = _rest(db_path, "Corner Bar", billing_status="active", owner_email="o@ex.com", location_group="Corner",
              location_name="Uptown")
    sib = admin_ops.client_detail(a)["siblings"]
    assert [s["id"] for s in sib] == [b]
    s = sib[0]
    assert "modules" not in s and "integrations" not in s and "logins" not in s
    # Everything the page's slimOf() and locName() read.
    assert s["location_name"] == "Uptown" and s["billing"]["status"] == "active"
    assert isinstance(s["issues"], list) and set(s["onboarding"]) == {"done", "total"}
    assert "score" in s["setup_completeness"] and "level" in s["churn_risk"]
    assert len(json.dumps(s)) < len(json.dumps(next(r for r in admin_ops.clients()["clients"] if r["id"] == b))) / 3
