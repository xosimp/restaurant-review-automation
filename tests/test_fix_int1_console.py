"""Integration wave INT-1 — the console's data layer reading what the other
workstreams store, and the reads the console pass (UI-1) asked for."""
from datetime import datetime, timedelta, timezone

import admin_ops
from tests.test_fix_c_console import _mk, _rec, _sql, _utc, env  # noqa: F401


def _cap(db_path, rid, until, lifted=False, n=14):
    _sql(db_path, "INSERT INTO alert_storm_caps (restaurant_id, local_day, until_at, alerts_in_window, threshold, "
                  "suppressed, lifted_at) VALUES (?,?,?,?,?,?,?)",
         (rid, until.strftime("%Y-%m-%d"), _utc(until), n, 10, 3, _utc(until) if lifted else None))


# ── the automatic storm cap, per client (UI-1 request 2; E's #92) ──────────

def test_the_client_record_carries_its_storm_cap_or_none(db_path):
    capped, lifted, calm = _mk(db_path, "Storm Co"), _mk(db_path, "Lifted Co"), _mk(db_path, "Calm Co")
    later = datetime.now(timezone.utc) + timedelta(hours=5)
    _cap(db_path, capped, later)
    _cap(db_path, lifted, later, lifted=True)
    _cap(db_path, calm, datetime.now(timezone.utc) - timedelta(hours=1))          # expired at midnight
    cap = _rec(capped)["storm_cap"]
    assert cap["until"] == later.strftime("%Y-%m-%dT%H:%M:%SZ") and cap["until_at"] == _utc(later)
    assert cap["reason"] == "14 alerts in an hour (limit 10)" and cap["suppressed"] == 3
    assert cap["cap"] == "health and safety alerts only"
    assert _rec(lifted)["storm_cap"] is None and _rec(calm)["storm_cap"] is None
    assert admin_ops.client_detail(capped)["storm_cap"]["alerts_in_window"] == 14


def test_the_notifications_page_reads_the_caps_e_stores(db_path):
    rid = _mk(db_path, "Storm Co")
    _cap(db_path, rid, datetime.now(timezone.utc) + timedelta(hours=2))
    n = admin_ops.notifications()
    assert n["auto_caps_supported"] is True
    assert [c["restaurant_id"] for c in n["auto_caps"]] == [rid] and n["auto_caps"][0]["restaurant"] == "Storm Co"


# ── onboarding rows carry the contract (UI-1 request 3) ─────────────────────

def test_onboarding_rows_carry_the_contract_status(db_path):
    signed = _mk(db_path, "Signed Co", billing_status="trial")
    _sql(db_path, "UPDATE restaurants SET contract_status='signed' WHERE id=?", (signed,))
    waiting = _mk(db_path, "Waiting Co", billing_status="trial")
    rows = {r["id"]: r for r in admin_ops.onboarding_list()["rows"]}
    assert rows[signed]["contract_status"] == "signed"
    assert rows[waiting]["contract_status"] == "pending"


# ── the clients list filters the browser used to apply (UI-1 request 4) ────

def test_the_clients_list_filters_on_the_server(db_path):
    old = _mk(db_path, "Old Co", billing_status="active")
    new = _mk(db_path, "New Co", billing_status="active")
    _sql(db_path, "UPDATE restaurants SET created_at=? WHERE id=?",
         (_utc(datetime.now(timezone.utc) - timedelta(days=90)), old))

    def ids(**kw):
        return {r["id"] for r in admin_ops.clients_page(per_page=100, **kw)["items"]}
    assert ids(joined_days=30) == {new}
    assert ids(inactive_days=14) == {old, new}          # nobody has signed in to either
    with_issues = {r["id"] for r in admin_ops.clients()["clients"] if r["issues"]}
    assert ids(has_issues="1") == with_issues & {old, new}
    assert ids(churn="nonsense") == set()


# ── G's AI issues in the client record (#122, #140, #48) ───────────────────

def _ai_row(db_path, rid, cost, outcome="ok", when="datetime('now')", action="draft", vendor="anthropic"):
    _sql(db_path, "INSERT INTO ai_usage (restaurant_id, action, model, input_tokens, output_tokens, cost_usd, "
                  f"created_at, vendor, outcome, status, \"trigger\") VALUES (?, ?, 'claude-sonnet-5', 10, 10, ?, {when}, "
                  "?, ?, ?, 'owner')", (rid, action, cost, vendor, outcome, "ok" if outcome == "ok" else "error"))


def test_a_ceiling_nearly_spent_is_an_issue_and_thirty_dollars_a_month_is_not(db_path):
    trial = _mk(db_path, "Trial Co", billing_status="trial")
    active = _mk(db_path, "Busy Co", billing_status="active")
    _ai_row(db_path, trial, 4.5)                     # 90% of a trial's $5 a day
    for _ in range(6):
        _ai_row(db_path, active, 5.0, when="datetime('now','-3 days')")     # $30 this month, under its ceiling
    issues = {i["key"]: i for i in _rec(trial)["issues"]}
    b = issues[f"{trial}:ai_budget:ai_day"]
    assert b["severity"] == "warning" and "90% of today's AI budget" in b["title"] and "$4.50 of $5.00" in b["detail"]
    assert not any(k.startswith(f"{active}:ai_cost") or k.startswith(f"{active}:ai_budget")
                   for k in (i["key"] for i in _rec(active)["issues"])), "the old $25 rule is gone"


def test_an_ai_cost_anomaly_is_a_client_issue(db_path):
    rid = _mk(db_path, "Spiky Co", billing_status="active")
    for d in range(3, 17):
        _ai_row(db_path, rid, 0.10, when=f"datetime('now','-{d} days')")
    _ai_row(db_path, rid, 6.0)
    keys = [i["key"] for i in _rec(rid)["issues"]]
    assert any(k.startswith(f"{rid}:ai_anomaly:cost:") for k in keys)


def test_a_blocked_call_is_not_a_call(db_path):
    rid = _mk(db_path, "Blocked Co", billing_status="active")
    _ai_row(db_path, rid, 0.02)
    for _ in range(5):
        _ai_row(db_path, rid, 0.0, outcome="blocked")
    rec = _rec(rid)
    assert rec["ai"]["calls_today"] == 1 and rec["ai"]["calls_30d"] == 1
    assert admin_ops.overview()["kpis"]["ai_calls_today"] == 1


# ── support masking in free text (C's #87 contract) ────────────────────────

def test_support_sees_phones_and_ips_masked_inside_free_text():
    out = admin_ops.redact_for_support({"error": "Twilio 21610 to +15125550123 from 10.1.2.3",
                                        "note": "call (512) 555-0199, or 5125550100", "when": "2026-09-29 10:00:00"})
    assert "+15125550123" not in out["error"] and out["error"].endswith("•••-•••-0123 from 10.1.x.x")
    assert "555-0199" not in out["note"] and "5125550100" not in out["note"] and "0199" in out["note"]
    assert out["when"] == "2026-09-29 10:00:00", "a timestamp is not a phone number"


# ── /admin/api/issues carries the fleet payload meta ───────────────────────

def test_the_issues_payload_says_how_fresh_it_is(db_path):
    _mk(db_path, "Meta Co")
    out = admin_ops.issues()
    for k in ("generated_at", "cached", "age_seconds", "windows", "errors", "query_errors", "unavailable"):
        assert k in out, k
