"""Integration wave INT-1 — the console's data layer reading what the other
workstreams store, and the reads the console pass (UI-1) asked for."""
from datetime import datetime, timedelta, timezone

import admin_ops
import auth
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


# ── H's billing ledgers raise issues the console can act on ────────────────

def test_past_due_is_aged_from_the_invoice_and_acts_with_a_card_update_link(db_path):
    rid = _mk(db_path, "Late Co", billing_status="past_due")
    failed = datetime.now(timezone.utc) - timedelta(days=4)
    _sql(db_path, "INSERT INTO stripe_invoices (invoice_id, restaurant_id, status, attempt_count, "
                  "amount_remaining_cents, next_payment_attempt, last_failed_at) VALUES ('in_1', ?, 'open', 2, 64900, "
                  "?, ?)", (rid, _utc(datetime.now(timezone.utc) + timedelta(days=3)), _utc(failed)))
    i = next(x for x in _rec(rid)["issues"] if x["key"] == f"{rid}:billing")
    assert i["action_route"] == f"/admin/api/billing/{rid}/card-update-link" and i["action_kind"] == "post"
    assert i["since_at"] == failed.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert "$649.00 due" in i["detail"] and "2 attempts" in i["detail"]


def test_a_hold_is_an_issue_whose_action_lifts_it(db_path):
    rid = _mk(db_path, "Held Co", billing_status="paused")
    _sql(db_path, "UPDATE restaurants SET pause_reason='admin' WHERE id=?", (rid,))
    rec = _rec(rid)
    assert rec["billing"]["hold"] == "admin"
    i = next(x for x in rec["issues"] if x["key"] == f"{rid}:paused:admin")
    assert i["action_route"] == f"/admin/api/billing/{rid}/lift-hold" and i["action_payload"] == {"note": ""}


def test_owed_mail_that_failed_and_reconcile_findings_are_issues(db_path):
    rid = _mk(db_path, "Ledger Co", billing_status="active")
    _sql(db_path, "INSERT INTO owed_sends (restaurant_id, kind, dedupe_key, status, last_error) "
                  "VALUES (?, 'receipt', 'receipt:in_9', 'failed', 'suppressed address')", (rid,))
    _sql(db_path, "INSERT INTO billing_reconcile (restaurant_id, checked_at, local_status, stripe_status, mismatches, "
                  "first_seen_at) VALUES (?, datetime('now'), 'active', 'canceled', ?, datetime('now','-1 day'))",
         (rid, '[{"kind": "status", "detail": "local active, Stripe canceled"}]'))
    issues = {i["key"]: i for i in _rec(rid)["issues"]}
    assert "receipt: suppressed address" in issues[f"{rid}:owed_sends"]["detail"]
    assert "local active, Stripe canceled" in issues[f"{rid}:reconcile"]["detail"]


def test_a_declined_contract_says_so(db_path):
    rid = _mk(db_path, "Declined Co", billing_status="trial")
    _sql(db_path, "UPDATE restaurants SET contract_status='declined' WHERE id=?", (rid,))
    _sql(db_path, "INSERT INTO docusign_envelopes (envelope_id, restaurant_id, status, status_at, status_reason) "
                  "VALUES ('env1', ?, 'declined', datetime('now'), 'Wrong module count')", (rid,))
    issues = {i["key"]: i for i in _rec(rid)["issues"]}
    i = issues[f"{rid}:contract:declined"]
    assert i["severity"] == "critical" and "Wrong module count" in i["detail"]
    assert f"{rid}:contract" not in issues


def test_the_stored_module_mismatch_is_the_issue(db_path):
    rid = _mk(db_path, "Plan Co", billing_status="active", module_reviews=1)
    _sql(db_path, "INSERT INTO stripe_subscriptions (restaurant_id, subscription_id, status, module_mismatch) "
                  "VALUES (?, 'sub_1', 'active', ?)", (rid, '{"stripe": ["labor", "reviews"], "local": ["reviews"]}'))
    i = next(x for x in _rec(rid)["issues"] if x["key"] == f"{rid}:modules_mismatch")
    assert "Stripe: labor, reviews" in i["detail"] and "Here: reviews" in i["detail"]


# ── one server reading of the platform's state (UI-2 request 1, #158) ─────

def test_the_ops_state_names_every_system_and_says_why(db_path, monkeypatch, scheduler_heartbeat):
    import status_manager
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    _mk(db_path, "State Co", billing_status="active")
    scheduler_heartbeat(60)
    out = admin_ops.ops_state()
    assert set(out["systems"]) == set(admin_ops.OPS_SYSTEMS)
    for s in out["systems"].values():
        assert s["state"] in ("ok", "warn", "bad", "unknown") and "reason" in s and "since" in s
    sched = out["systems"]["scheduler"]
    assert sched["state"] == "bad" and "No heartbeat for 60 minutes" in sched["reason"] and out["worst"] == "bad"
    scheduler_heartbeat(1)
    assert admin_ops.ops_state()["systems"]["scheduler"]["state"] == "ok"


def test_the_ops_state_route_answers_admins_and_support(db_path, monkeypatch):
    import status_manager
    from flask import Flask
    import status_routes
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    app = Flask(__name__)
    app.register_blueprint(status_routes.status_bp)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 2, "restaurant_id": None, "is_admin": 0,
                                                           "role": "support", "username": "sup"})
    monkeypatch.setattr(auth, "admin_second_factor_state", lambda u: "ok")
    r = app.test_client().get("/admin/api/ops/state")
    assert r.status_code == 200 and set(r.get_json()["systems"]) == set(admin_ops.OPS_SYSTEMS)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 3, "restaurant_id": 1, "is_admin": 0, "role": "owner"})
    assert app.test_client().get("/admin/api/ops/state").status_code == 403


# ── the operations payloads say how old they are; jobs group by kind ──────

def test_jobs_group_failures_by_job_and_kind_and_payloads_carry_generated_at(db_path):
    import ops
    ops.capture(RuntimeError("Toast 500"), job="pos_sync", context="restaurant_id=1", db_path=db_path)
    ops.capture(RuntimeError("finding"), job="pos_sync", kind="ai_quality", context="restaurant_id=1", db_path=db_path)
    j = admin_ops.jobs()
    groups = {(g["job"], g["kind"]): g["n"] for g in j["grouped"]}
    assert groups[("pos_sync", "job")] == 1 and groups[("pos_sync", "ai_quality")] == 1
    for payload in (j, admin_ops.emails(), admin_ops.notifications(), admin_ops.ai_ops(days=7)):
        assert payload["generated_at"].endswith("Z")
    assert len(admin_ops.ai_ops(days=7)["daily"]) == 7, "every day of the window, zero-filled"


def test_the_system_card_shows_the_last_operator_page(db_path, monkeypatch):
    import ops
    import platform_monitor
    import status_manager
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    ops._record_operator_alert("platform_sla_alert", "Cavnar AI: the platform needs you",
                               {"sent": True, "channels": {"email": True}})
    card = platform_monitor.system_report(db_path)
    assert card["operator_alert"]["key"] == "platform_sla_alert"


# ── the deletion issue opens B2's offboarding checklist (#34) ──────────────

def test_the_deletion_issue_opens_the_checklist_and_says_what_is_left(db_path):
    import offboarding
    offboarding.init_offboarding(db_path)
    rid = _mk(db_path, "Leaving Co")
    _sql(db_path, "UPDATE restaurants SET deletion_requested_at=datetime('now','-2 days') WHERE id=?", (rid,))
    issue = next(i for i in _rec(rid)["issues"] if i["key"] == f"{rid}:deletion")
    assert issue["action_href"] == f"#client/{rid}?tab=offboarding"
    assert "Left on the checklist:" in issue["detail"]


# ── A's session ids on the client page ─────────────────────────────────────

def test_client_sessions_carry_the_id_the_revoke_route_takes(db_path):
    rid = _mk(db_path, "Session Co")
    uid = auth.create_user(rid, "sess_owner", "so@x.test", "a-long-pass-1", db_path=db_path)
    uid = uid if isinstance(uid, int) else None
    token = auth.create_session(uid, db_path=db_path)
    row = admin_ops.client_detail(rid)["sessions"][0]
    assert row["session_id"] == auth.hash_session_token(token)[:16] and not row["is_view_as"]


# ── the overview's backup tile and a failed backup (D #1, #2, #28) ─────────

def test_the_overview_carries_the_backup_and_a_failed_backup_is_an_issue(db_path):
    _sql(db_path, "INSERT INTO backup_runs (started_at, finished_at, local_ok, offsite_ok, detail_json) "
                  "VALUES (datetime('now','-1 hour'), datetime('now','-1 hour'), 0, 0, ?)", ('{"error": "disk full"}',))
    ov = admin_ops.overview()
    assert ov["backup"]["state"] == "failed" and "storage" in ov
    issue = next(i for i in ov["issues"] if i["key"] == "backup")
    assert issue["severity"] == "critical" and "disk full" in issue["detail"] and issue["resolvable"] is False
    assert admin_ops.resolve_issue("backup", "", "will")["resolvable"] is False

# ── the alert cap is one typed audit row (INT-2 handoff; B2's record_admin_action) ──

def test_the_alert_cap_is_a_typed_audit_row_with_the_number_either_side(db_path):
    import json
    rid = _mk(db_path, "Capped Co")
    assert admin_ops.set_alert_cap(rid, 12, {"id": 3, "username": "will"})["ok"]
    assert admin_ops.set_alert_cap(rid, 0, "will")["ok"]          # an older caller's username
    from tests.test_fix_c_console import _rows
    rows = _rows(db_path, "SELECT source, actor, actor_id, target, before_json, after_json, summary FROM admin_events "
                          "WHERE event_type='alert_cap.set' ORDER BY id")
    assert [r["source"] for r in rows] == ["admin", "admin"]
    assert (rows[0]["actor"], rows[0]["actor_id"], rows[0]["target"]) == ("will", 3, f"restaurant:{rid}")
    assert json.loads(rows[0]["before_json"]) == {"alert_max_per_day": 0}
    assert json.loads(rows[0]["after_json"]) == {"alert_max_per_day": 12}
    assert rows[0]["summary"] == "Alert cap set to 12 by will"
    assert json.loads(rows[1]["before_json"]) == {"alert_max_per_day": 12} and rows[1]["actor_id"] is None
    assert rows[1]["summary"] == "Alert cap set to off by will"

