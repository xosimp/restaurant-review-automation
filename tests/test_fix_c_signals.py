"""Fix round C — signals the console now raises or shows: a revoked Google
connection, a suppressed owner address, Data Health read honestly, reviews
the AI gave up on, deletion requests with their deadline, failed jobs apart
from AI-quality findings, bounce and complaint rates, webhook verification,
queues, actions that act, search, per-client job failures, adoption, one
client timeline, the support role's masking, notification figures and the
Intelligence value totals."""
from datetime import datetime, timedelta, timezone

import pytest

import admin_ops
import models
from tests.test_fix_c_console import _app, _as_admin, _mk, _rec, _rows, _sql, _utc, env  # noqa: F401


def _now():
    return datetime.now(timezone.utc)


# ── #44: a revoked Business Profile is an error, stamped where it happens ──

def test_a_revoked_google_connection_is_stamped_and_raised(db_path, monkeypatch):
    import gmb
    rid = _mk(db_path, "Google Co", google_place_id="ChIJx", reviews_live=1, module_reviews=1)
    _sql(db_path, "UPDATE restaurants SET gmb_refresh_token='rt', gmb_access_token='at', "
                  "gmb_token_expires='2000-01-01T00:00:00' WHERE id=?", (rid,))
    monkeypatch.setattr(gmb, "refresh_access_token",
                        lambda tok: (_ for _ in ()).throw(gmb.GoogleTokenRevoked("invalid_grant")))
    assert gmb.get_valid_token(rid) is None
    r = models.get_restaurant(rid, db_path=db_path)
    assert r.gmb_refresh_token is None and r.gmb_revoked_at
    rec = _rec(rid)
    g = next(i for i in rec["integrations"] if i["key"] == "google_business")
    assert g["state"] == "error" and g["revoked_at"] and "revoked" in g["error"]
    issue = next(i for i in rec["issues"] if i["key"] == f"{rid}:int:google_business")
    assert issue["severity"] == "critical" and issue["action_kind"] == "link"
    assert admin_ops.overview()["kpis"]["integrations_failing"] >= 1


def test_a_business_profile_that_keeps_failing_is_an_error(db_path):
    import data_health
    rid = _mk(db_path, "Fallback Co", google_place_id="ChIJy", module_reviews=1)
    _sql(db_path, "UPDATE restaurants SET gmb_refresh_token='rt' WHERE id=?", (rid,))
    for _ in range(admin_ops.GBP_FAILING_SLOTS):
        data_health.record_attempt(rid, "gbp", False, provider="gbp", error="location not matched", db_path=db_path)
    g = next(i for i in _rec(rid)["integrations"] if i["key"] == "google_business")
    assert g["state"] == "error" and "hasn't answered" in g["error"]


# ── #45: a suppressed owner address ─────────────────────────────────────────

def test_a_suppressed_owner_address_is_critical_with_a_reinstate_action(db_path):
    rid = _mk(db_path, "Bounce Co", owner_email="owner@bounce.test")
    _sql(db_path, "INSERT INTO email_suppressions (email, reason, detail) VALUES ('owner@bounce.test', 'bounced', 'x')")
    issue = next(i for i in _rec(rid)["issues"] if i["category"] == "suppressed")
    assert issue["severity"] == "critical" and issue["action_payload"] == {"email": "owner@bounce.test"}
    assert issue["action_route"] == "/admin/api/suppressions/reinstate" and issue["action_kind"] == "post"
    _sql(db_path, "UPDATE email_suppressions SET scope='guest'")
    assert not any(i["category"] == "suppressed" for i in _rec(rid)["issues"])


# ── #46: Data Health read honestly ──────────────────────────────────────────

def _dh(db_path, rid, day, sources):
    import json
    _sql(db_path, "INSERT OR REPLACE INTO data_health_daily (restaurant_id, date, overall, sources_json) VALUES (?,?,?,?)",
         (rid, day, 80, json.dumps(sources)))


def test_pending_and_not_connected_are_not_healthy_and_a_stale_snapshot_is_not_used(db_path):
    rid = _mk(db_path, "DH Co", module_reviews=1, google_place_id="p", module_marketing=1)
    _sql(db_path, "UPDATE restaurants SET voice_notes='v' WHERE id=?", (rid,))
    _sql(db_path, "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, fetched_at) "
                  "VALUES (?, 'google', 'e1', 'a', 5, 't', ?)", (rid, admin_ops._now_ct().strftime("%Y-%m-%dT%H:%M:%S")))
    _sql(db_path, "INSERT INTO marketing_content_log (restaurant_id, content_type, created_at) VALUES (?, 'post', datetime('now'))", (rid,))
    today = admin_ops._now_ct().date().isoformat()
    _dh(db_path, rid, today, [{"key": "reviews", "state": "pending"}, {"key": "marketing", "state": "not_connected"}])
    mods = {m["key"]: m["state"] for m in _rec(rid)["modules"]}
    assert mods["reviews"] == "no_data" and mods["marketing"] == "unconfigured"
    _sql(db_path, "DELETE FROM data_health_daily")
    old = (admin_ops._now_ct().date() - timedelta(days=5)).isoformat()
    _dh(db_path, rid, old, [{"key": "reviews", "state": "stale"}])
    rec = _rec(rid)
    assert rec["data_health"]["stale"] is True and rec["data_health"]["age_days"] == 5
    assert {m["key"]: m["state"] for m in rec["modules"]}["reviews"] == "healthy"


def test_repeated_source_failures_are_an_issue_and_listed(db_path):
    import data_health
    rid = _mk(db_path, "Source Co")
    for _ in range(3):
        data_health.record_attempt(rid, "weather", False, provider="nws", error="HTTP 503", db_path=db_path)
    issue = next(i for i in _rec(rid)["issues"] if i["key"] == f"{rid}:source:weather")
    assert issue["severity"] == "warning" and "3 in a row" in issue["title"]
    ds = admin_ops.data_sources()
    assert ds["rows"][0]["source"] == "weather" and ds["rows"][0]["failures"] == 3 and ds["rows"][0]["since"]


# ── #124: reviews the AI gave up on ────────────────────────────────────────

def test_stalled_reviews_are_issues_per_client_and_fleet_wide(db_path):
    rid = _mk(db_path, "Stall Co")
    for i in range(3):
        _sql(db_path, "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, processed, "
                      "analysis_attempts, fetched_at) VALUES (?, 'google', ?, 'a', 2, 't', 0, 5, ?)",
             (rid, f"s{i}", admin_ops._now_ct().strftime("%Y-%m-%dT%H:%M:%S")))
    rec = _rec(rid)
    assert rec["reviews"]["stalled"] == 3
    issue = next(i for i in rec["issues"] if i["key"] == f"{rid}:stalled_reviews")
    assert issue["action_route"] == f"/admin/api/client/{rid}/retry-ai"
    ov = admin_ops.overview()
    assert ov["kpis"]["stalled_reviews"] == 3 and any(i["key"] == "stalled:fleet" for i in ov["issues"])


def test_awaiting_counts_reviews_that_never_got_a_draft(db_path):
    rid = _mk(db_path, "Await Co")
    for i, status in enumerate(("pending", "drafted", "approved")):
        _sql(db_path, "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, response_status, "
                      "fetched_at) VALUES (?, 'google', ?, 'a', 4, 't', ?, ?)",
             (rid, f"w{i}", status, admin_ops._now_ct().strftime("%Y-%m-%dT%H:%M:%S")))
    rv = _rec(rid)["reviews"]
    assert (rv["awaiting"], rv["draft_ready"], rv["no_draft"]) == (2, 1, 1)


# ── #34: an account-deletion request, with its deadline ────────────────────

def test_a_deletion_request_is_critical_with_its_due_date(db_path):
    from time_utils import mdy
    rid = _mk(db_path, "Leaving Co")
    at = _now() - timedelta(days=3)
    _sql(db_path, "UPDATE restaurants SET deletion_requested_at=? WHERE id=?", (_utc(at), rid))
    issue = next(i for i in admin_ops.overview()["issues"] if i["key"] == f"{rid}:deletion")
    due = (at + timedelta(days=30)).astimezone(admin_ops._now_ct().tzinfo).date()
    assert issue["severity"] == "critical" and issue["resolvable"] is False and mdy(due) in issue["title"]
    assert _rec(rid)["deletion"]["days_left"] in (26, 27)
    _sql(db_path, "UPDATE restaurants SET deletion_requested_at=? WHERE id=?", (_utc(_now() - timedelta(days=33)), rid))
    assert "overdue by" in next(i for i in _rec(rid)["issues"] if i["key"] == f"{rid}:deletion")["title"]


# ── #58: failed jobs are jobs ───────────────────────────────────────────────

def test_ai_quality_findings_are_not_failed_jobs(db_path):
    """AI output findings are ai_quality_events rows (fix round G, #58) —
    never job_failures — counted apart from failed jobs, and a job_failures
    row is judged by its kind column (fix round D)."""
    import ai_utils
    import ops
    rid = _mk(db_path, "Quality Co")
    for _ in range(6):
        ai_utils.record_quality_event("labor_insight", "figures", restaurant_id=rid,
                                      detail="stated figures not present in its input: ['$9']", db_path=db_path)
    ops.capture(RuntimeError("Toast 500"), job="pos_sync", context=f"restaurant_id={rid}", db_path=db_path)
    ov = admin_ops.overview()
    assert ov["kpis"]["job_failures_24h"] == 1 and ov["kpis"]["ai_quality_24h"] == 6
    assert ov["kpis"]["job_kind_basis"] == "job_failures.kind"
    keys = {i["key"] for i in ov["issues"]}
    assert "job:pos_sync" in keys and "job:labor_insight" not in keys
    # The column decides: a failure captured as an AI finding is one, whatever its text.
    ops.capture(RuntimeError("guard finding"), job="labor_insight", context=f"restaurant_id={rid}",
                kind="ai_quality", db_path=db_path)
    k = admin_ops.overview()["kpis"]
    assert k["job_failures_24h"] == 1 and k["ai_quality_24h"] == 7
    quality = admin_ops.client_detail(rid)["ai_quality"]
    assert len(quality) == 7 and {q["source"] for q in quality} == {"ai_quality_events", "job_failures"}
    assert [j["job"] for j in admin_ops.client_detail(rid)["jobs"]] == ["pos_sync"]


# ── #59: bounces and complaints are not deliveries ─────────────────────────

def test_bounce_rates_raise_and_bounces_are_not_sent(db_path):
    rid = _mk(db_path, "Mail Co")
    for i in range(60):
        status = "bounced" if i < 6 else ("complained" if i == 6 else "delivered")
        _sql(db_path, "INSERT INTO email_log (restaurant_id, email_type, to_email, subject, status, sent_at) "
                      "VALUES (?, 'digest', ?, 's', ?, datetime('now','-1 hours'))", (rid, f"a{i}@x.test", status))
    e = admin_ops.emails()
    wk = e["rates"]["7d"]
    assert wk["accepted"] == 60 and wk["bounces"] == 6 and wk["bounce_rate"] == 10.0 and wk["enough"]
    assert e["today"]["sent"] == 53 and e["today"]["bounced"] == 6
    ov = admin_ops.overview()
    b = next(i for i in ov["issues"] if i["key"] == "email:bounce_rate")
    assert b["severity"] == "critical"
    assert any(i["key"] == "email:complaint_rate" for i in ov["issues"])
    assert ov["kpis"]["email_failures_today"] == 7 and ov["kpis"]["emails_today"] == 53
    assert next(i for i in _rec(rid)["issues"] if i["key"] == f"{rid}:email")["title"].startswith("7 emails failed")


# ── #74: inbound webhooks that stopped verifying ───────────────────────────

def test_signature_failures_with_no_verified_event_raise_a_critical(db_path):
    import ops
    ops.capture(RuntimeError("Webhook signature verification failed"), job="stripe_webhook",
                context="bad signature", db_path=db_path)
    ov = admin_ops.overview()
    issue = next(i for i in ov["issues"] if i["key"] == "webhook:stripe")
    assert issue["severity"] == "critical" and "STRIPE_WEBHOOK_SECRET" in issue["detail"]
    stripe = next(p for p in admin_ops.integrations()["webhooks_inbound"]["providers"] if p["provider"] == "stripe")
    assert stripe["failures_24h"] == 1 and stripe["last_verified_at"] is None
    _sql(db_path, "INSERT INTO admin_events (source, event_type, summary) VALUES ('stripe', 'invoice.paid', 'ok')")
    assert not any(i["key"] == "webhook:stripe" for i in admin_ops.overview()["issues"])


# ── #75: durable queues ─────────────────────────────────────────────────────

def test_queues_report_counts_oldest_and_failures(db_path):
    rid = _mk(db_path, "Queue Co")
    _sql(db_path, "INSERT INTO delayed_actions (restaurant_id, kind, execute_at, status, created_at) "
                  "VALUES (?, 'order', datetime('now','+1 hours'), 'pending', datetime('now','-2 hours'))", (rid,))
    _sql(db_path, "INSERT INTO delayed_actions (restaurant_id, kind, execute_at, status, executed_at) "
                  "VALUES (?, 'order', datetime('now'), 'failed', datetime('now'))", (rid,))
    q = {x["key"]: x for x in admin_ops.queues()["queues"]}
    d = q["delayed_actions"]
    assert d["available"] and d["pending"] == 1 and d["failed_24h"] == 1 and 1.9 < d["oldest_pending_age_hours"] < 2.1
    assert q["push_outbox"]["available"] is False        # no queued state column yet (workstream E)


# ── #62: actions that act ───────────────────────────────────────────────────

def test_issue_actions_say_what_to_do(db_path):
    rid = _mk(db_path, "Act Co", billing_status="active", owner_name="Erik Smith")
    _sql(db_path, "UPDATE restaurants SET created_at='2026-01-01T09:00:00', toast_restaurant_guid='g', "
                  "toast_sync_error='401' WHERE id=?", (rid,))
    issues = {i["category"]: i for i in _rec(rid)["issues"]}
    assert issues["inactive"]["action_kind"] == "mailto" and "Checking%20in" in issues["inactive"]["action_href"]
    assert issues["int"]["action_kind"] == "link" and issues["int"]["action_href"] == f"#client/{rid}?tab=data"
    assert issues["unconf"]["action_kind"] == "link"


# ── #63: search by what an owner or a provider would quote ─────────────────

def test_search_finds_phone_place_envelope_and_pos_ids(db_path):
    for i in range(10):                      # a two-digit id: search needs two characters
        _mk(db_path, f"Filler {i}")
    rid = _mk(db_path, "Search Co", owner_phone="(314) 555-0199", google_place_id="ChIJsearch123",
              stripe_customer_id="cus_search9999")
    _sql(db_path, "UPDATE restaurants SET docusign_envelope_id='env-7788', rpower_store_mid='MID42' WHERE id=?", (rid,))
    for q, match in (("314-555-0199", "owner phone"), ("ChIJsearch123", "Google Place ID"),
                     ("env-7788", "DocuSign envelope"), ("MID42", "RPOWER store"), ("cus_search9999", "Stripe customer")):
        res = admin_ops.search(q)["results"]
        hit = next(r for r in res if r["id"] == rid)
        assert hit["match"] == match, q
    res = admin_ops.search(str(rid))["results"]
    assert any(r["type"] == "location" and r["match"] == "restaurant id" for r in res)


# ── #67: a client's own job failures ────────────────────────────────────────

def test_client_job_failures_match_the_restaurant_exactly(db_path):
    """By job_failures.restaurant_id (fix round D, #67), which capture stamps
    from a "restaurant_id=N" / "rid=N" context or takes as an argument: rid 5
    never matches rid 50, and a context that only names the restaurant
    matches nothing."""
    import ops
    rid = _mk(db_path, "Five")
    ops.capture(RuntimeError("mine"), job="pos_sync", context=f"restaurant_id={rid}", db_path=db_path)
    ops.capture(RuntimeError("theirs"), job="pos_sync", context=f"rid={rid}0", db_path=db_path)
    ops.capture(RuntimeError("named"), job="pos_sync", context="Five", db_path=db_path)
    assert [j["error"] for j in admin_ops.client_detail(rid)["jobs"]] == ["mine"]
    # The column decides, not the text: passed with a context that names no
    # restaurant, it matches; a row stamped for another restaurant does not,
    # whatever its context says.
    ops.capture(RuntimeError("explicit"), job="pos_sync", context="toast sync", restaurant_id=rid, db_path=db_path)
    ops.capture(RuntimeError("elsewhere"), job="pos_sync", context=f"restaurant_id={rid}", restaurant_id=rid + 1,
                db_path=db_path)
    assert [j["error"] for j in admin_ops.client_detail(rid)["jobs"]] == ["explicit", "mine"]


# ── #71: adoption is use, not entitlement ──────────────────────────────────

def test_adoption_counts_use_not_flags(db_path):
    used = _mk(db_path, "Uses Reviews", billing_status="active")
    _mk(db_path, "Never Looks", billing_status="active")
    models.log_activity(used, "reviews", db_path=db_path)
    reviews = next(m for m in admin_ops.adoption()["modules"] if m["module"] == "reviews")
    assert reviews["paying"] == {"entitled": 2, "using": 1, "rate_pct": 50.0}


# ── #54: one timeline per client ────────────────────────────────────────────

def test_the_timeline_merges_and_labels_staff_pin_events(db_path):
    rid = _mk(db_path, "Timeline Co")
    import auth
    uid = auth.create_user(rid, "tl_owner", "tl@x.test", "a-long-pass-1", db_path=db_path)
    uid = uid if isinstance(uid, int) else _rows(db_path, "SELECT id FROM users WHERE username='tl_owner'")[0]["id"]
    _sql(db_path, "INSERT INTO login_history (user_id, restaurant_id, event, device_type, created_at) "
                  "VALUES (?, ?, 'pin_failed', 'staff_pin', datetime('now','-2 hours'))", (uid, rid))
    _sql(db_path, "INSERT INTO email_log (restaurant_id, email_type, to_email, subject, status, sent_at) "
                  "VALUES (?, 'digest', 'tl@x.test', 'Weekly', 'bounced', datetime('now','-1 hours'))", (rid,))
    t = admin_ops.client_timeline(rid)
    labels = [e["label"] for e in t["events"]]
    assert labels[0] == "Email bounced · digest" and "Staff PIN failed" in labels
    assert [e["type"] for e in admin_ops.client_timeline(rid, types=["login"])["events"]] == ["login"]
    assert admin_ops.client_timeline(99999)["ok"] is False


# ── #87: the support role's view ────────────────────────────────────────────

def test_support_reads_are_masked_and_admin_reads_are_not(db_path, monkeypatch):
    rid = _mk(db_path, "Private Co", owner_email="erik@private.test", owner_phone="314-555-0123",
              stripe_customer_id="cus_privatepriv1234")
    client = _app().test_client()
    _as_admin(monkeypatch, role="support", is_admin=0)
    r = client.get(f"/admin/api/client/{rid}")
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and r.headers.get("X-Redacted") == "support"
    assert "erik@private.test" not in body and "314-555-0123" not in body and "cus_privatepriv1234" not in body
    c = r.get_json()["client"]
    assert c["owner_email"].startswith("e•••@") and c["owner_phone"].endswith("0123")
    _as_admin(monkeypatch)
    body = client.get(f"/admin/api/client/{rid}").get_data(as_text=True)
    assert "erik@private.test" in body


# ── #92 / #160: notification figures that mean what they say ────────────────

def test_notification_figures(db_path):
    rid = _mk(db_path, "Alerting Co")
    for _ in range(12):
        _sql(db_path, "INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (?, '1star', datetime('now'))", (rid,))
    for i in range(4):
        _sql(db_path, "INSERT INTO push_deliveries (device_token_id, restaurant_id, alert_type, ok, attempts) "
                      "VALUES (1, ?, '1star', 1, 1)", (rid,))
    _sql(db_path, "INSERT INTO notification_opens (restaurant_id, alert_type) VALUES (?, '1star')", (rid,))
    for i in range(70):
        _sql(db_path, "INSERT INTO marketing_scheduled_posts (restaurant_id, platform, body, scheduled_for, status) "
                      "VALUES (?, 'ig', 'b', '2026-09-01T10:00:00', ?)", (rid, "failed" if i < 65 else "scheduled"))
    n = admin_ops.notifications()
    row = next(e for e in n["engagement"] if e["alert_type"] == "1star")
    assert (row["alerts"], row["delivered"], row["opened"], row["open_rate"]) == (12, 4, 1, 25.0)
    # E's alert_storm_caps is merged: automatic caps are read (none is on here).
    assert n["storm_counts"]["today"] == 1 and n["auto_caps_supported"] is True and n["auto_caps"] == []
    assert n["posts_failed_total"] == 65 and len(n["scheduled_posts"]) == 60
    assert _rec(rid)["scheduled_posts"]["pending"] == 5


def test_the_suppression_count_is_not_capped(db_path):
    for i in range(55):
        _sql(db_path, "INSERT INTO email_suppressions (email, reason) VALUES (?, 'bounced')", (f"s{i}@x.test",))
    e = admin_ops.emails()
    assert len(e["suppressed"]) == 50 and e["suppressed_total"] == 55


# ── #57: the Intelligence value totals ─────────────────────────────────────

def test_intelligence_savings_read_each_figures_own_key(db_path, monkeypatch):
    import value_delivered
    from intelligence import dashboard
    rid = _mk(db_path, "Value Co", billing_status="active")
    monkeypatch.setattr(value_delivered, "delivered", lambda *a, **k: {"monthly": 100.0})
    monkeypatch.setattr(value_delivered, "avoided", lambda *a, **k: {"dollars": 40.0})
    monkeypatch.setattr(value_delivered, "surfaced", lambda *a, **k: {"dollars": 25.0})
    monkeypatch.setattr(value_delivered, "opportunity", lambda *a, **k: {"monthly": 300.0})

    class R:
        id = rid
    s = dashboard._savings([R()], db_path)
    assert (s["delivered"], s["avoided"], s["surfaced"], s["opportunity"]) == (100, 40, 25, 300)
    assert s["source"] == "live" and s["periods"]["surfaced"] == "the last 30 days of alerts"
    out = dashboard.snapshot_value_figures(db_path=db_path, max_seconds=30)
    assert out["ok"] >= 1 and out["failed"] == 0
    monkeypatch.setattr(value_delivered, "avoided", lambda *a, **k: pytest.fail("computed live"))
    s = dashboard._savings([R()], db_path)
    assert s["source"] == "nightly" and s["avoided"] == 40
