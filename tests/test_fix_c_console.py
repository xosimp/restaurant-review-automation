"""Fix round C — the admin console's data layer: one zone per column,
query failures that surface, occurrence-scoped resolutions, the fleet memo
and its busy refusal, the churn read without a query per restaurant, owner
activity, one account filter, paging."""
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from flask import Flask

import admin_ops
import admin_routes
import auth
import client_api
import mobile_api
import models
from admin_routes import admin_bp
from auth_routes import auth_bp
from models import create_restaurant, get_conn, Restaurant

CT = ZoneInfo("America/Chicago")


def _setup_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, auth, admin_routes, client_api, mobile_api, admin_ops):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    models.init_two_fa_backup_codes(db_path=db_path)
    models.init_email_log(db_path=db_path)
    models.init_staff_notes(db_path=db_path)
    import push, webhooks, guest_marketing, ai_utils
    push.init_push(db_path)
    webhooks.init_webhooks(db_path)
    guest_marketing.init_guest_marketing(db_path)
    c = real(db_path)
    c.executescript(ai_utils._USAGE_TABLE_SQL)
    c.commit()
    c.close()
    admin_ops.invalidate_fleet_cache()


@pytest.fixture(autouse=True)
def env(monkeypatch, db_path):
    _setup_db(monkeypatch, db_path)
    yield db_path
    admin_ops.invalidate_fleet_cache()
    with admin_ops._fleet_cv:
        admin_ops._fleet_state.update(building=False, waiters=0)


def _sql(db_path, sql, args=()):
    c = get_conn(db_path)
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _rows(db_path, sql, args=()):
    c = get_conn(db_path)
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _mk(db_path, name="Corner Bar", **kw):
    kw.setdefault("owner_email", f"{name.lower().replace(' ', '')}@x.test")
    return create_restaurant(Restaurant(name=name, **kw), db_path=db_path)


def _utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _app():
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_bp)
    app.register_blueprint(auth_bp)
    return app


def _as_admin(monkeypatch, role="client", is_admin=1):
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": None, "is_admin": is_admin,
                                                           "username": "will", "email": "w@x.test", "role": role})


def _rec(rid):
    return next(r for r in admin_ops.clients()["clients"] if r["id"] == rid)


# ── #129 / #89: every stamp in its own column's zone ─────────────────────────

def test_the_nightly_false_alarm_is_gone(db_path, monkeypatch):
    """3:30am Central, every restaurant fetched at the 8pm run: nothing was
    missed. Reading the Chicago stamp as UTC made them 12.4h old — a critical
    "not fetched in over 12h" every night (FIGURES-9)."""
    now = datetime(2026, 9, 22, 3, 30, tzinfo=CT)
    monkeypatch.setattr(admin_ops, "_now_ct", lambda: now)
    rids = [_mk(db_path, f"Fetch {i}", module_reviews=1, billing_status="active") for i in range(4)]
    for rid in rids:
        _sql(db_path, "UPDATE restaurants SET last_fetched_at='2026-09-21T20:05:00', created_at='2026-08-01T09:00:00' "
                      "WHERE id=?", (rid,))
    keys = {i["key"] for i in admin_ops.overview()["issues"]}
    assert "fleet:fetch_coverage" not in keys
    assert not any(k.endswith(":fetch_behind") for k in keys)
    # Last fetched at 11am the day before: noon, 4pm and 8pm all passed.
    _sql(db_path, "UPDATE restaurants SET last_fetched_at='2026-09-21T11:00:00' WHERE id=?", (rids[0],))
    ov = admin_ops.overview()
    fleet = next(i for i in ov["issues"] if i["key"] == "fleet:fetch_coverage")
    assert fleet["title"].startswith("1 restaurant missed") and "Fetch 0" in fleet["detail"]
    assert ov["kpis"]["fetch_behind"] == 1


def test_today_is_midnight_central_against_utc_sent_at(db_path, monkeypatch):
    """A send that failed at 10:14pm Central is today's failure, whatever
    the server's zone (FIGURES-4). email_log.sent_at is UTC (workstream E)."""
    now = datetime(2026, 9, 28, 22, 30, tzinfo=CT)
    monkeypatch.setattr(admin_ops, "_now_ct", lambda: now)
    rid = _mk(db_path)
    _sql(db_path, "INSERT INTO email_log (restaurant_id, email_type, to_email, subject, sent_at, status, error) "
                  "VALUES (?, 'digest', 'o@x.test', 's', ?, 'failed', 'boom')",
         (rid, _utc(datetime(2026, 9, 28, 22, 14, tzinfo=CT))))
    _sql(db_path, "INSERT INTO email_log (restaurant_id, email_type, to_email, subject, sent_at, status) "
                  "VALUES (?, 'digest', 'o@x.test', 's', ?, 'sent')", (rid, _utc(datetime(2026, 9, 27, 23, 0, tzinfo=CT))))
    k = admin_ops.overview()["kpis"]
    assert k["email_failures_today"] == 1 and k["emails_today"] == 0
    assert admin_ops.emails()["today"]["failed"] == 1
    assert admin_ops.emails()["windows"]["labels"]["today"] == "since midnight Central"


def test_ages_read_chicago_and_utc_columns_as_instants():
    """A Chicago 'T' stamp and a UTC space stamp of the same instant age the
    same; the old parser read the 'T' one as server-local."""
    inst = datetime.now(timezone.utc) - timedelta(hours=5)
    chi = inst.astimezone(CT).strftime("%Y-%m-%dT%H:%M:%S")
    utc = inst.strftime("%Y-%m-%d %H:%M:%S")
    assert abs(admin_ops._age_hours(chi) - admin_ops._age_hours(utc, "UTC")) < 0.02
    assert admin_ops._iso_z(chi) == admin_ops._iso_z(utc, "UTC") == inst.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── #47: a failed query is reported, never read as zero ─────────────────────

def test_a_failing_query_is_reported_not_read_as_zero(db_path):
    rid = _mk(db_path, module_reviews=1)
    _sql(db_path, "DROP TABLE alert_log")
    ov = admin_ops.overview()
    assert ov["ok"] and any(e["query"] == "alert_log" for e in ov["errors"])
    assert ov["query_errors"] == ov["errors"]
    # The client page keeps `errors` for the client's own failures.
    d = admin_ops.client_detail(rid)
    assert isinstance(d["errors"], list) and any(e["query"] == "alert_log" for e in d["query_errors"])


def test_a_table_not_created_yet_is_unavailable_not_an_error(db_path):
    _mk(db_path)
    ov = admin_ops.overview()
    assert not any(e["query"] == "stripe_subscriptions" for e in ov["errors"])
    assert admin_ops.clients()["clients"][0]["billing"]["mrr_source"] is None


def test_query_failures_are_captured_once_per_window(db_path, monkeypatch):
    import ops
    seen = []
    monkeypatch.setattr(ops, "capture", lambda *a, **k: seen.append((a, k)))
    admin_ops._capture_last.clear()
    c = get_conn(db_path)
    for _ in range(3):
        admin_ops._rows_dict(c, "SELECT * FROM nowhere_at_all")
    c.close()
    assert len(seen) == 1 and "nowhere_at_all" in seen[0][1]["context"]


# ── #24: a resolution covers the occurrence it saw ─────────────────────────

def _email_fail(db_path, rid, at):
    _sql(db_path, "INSERT INTO email_log (restaurant_id, email_type, to_email, subject, sent_at, status, error) "
                  "VALUES (?, 'digest', 'o@x.test', 's', ?, 'failed', 'bounced')", (rid, _utc(at)))


def test_resolve_covers_one_occurrence_and_a_newer_one_reopens(db_path):
    rid = _mk(db_path)
    key = f"{rid}:email"
    _email_fail(db_path, rid, datetime.now(timezone.utc) - timedelta(hours=5))
    assert any(i["key"] == key for i in _rec(rid)["issues"])
    out = admin_ops.resolve_issue(key, "mailbox fixed", "will")
    assert out["ok"] and out["scope"] == "occurrence"
    rec = _rec(rid)
    assert not any(i["key"] == key for i in rec["issues"])
    assert rec["issues_resolved"][0]["resolution"]["resolved_by"] == "will"
    _email_fail(db_path, rid, datetime.now(timezone.utc) - timedelta(minutes=5))
    issue = next(i for i in _rec(rid)["issues"] if i["key"] == key)
    assert issue["reopened"]["reason"] == "newer_occurrence" and issue["reopened"]["note"] == "mailbox fixed"
    hist = _rows(db_path, "SELECT action FROM admin_issue_resolution_history WHERE key=?", (key,))
    assert [h["action"] for h in hist] == ["resolved"]


def test_a_cleared_condition_retires_its_resolution(db_path):
    rid = _mk(db_path)
    key = f"{rid}:email"
    _email_fail(db_path, rid, datetime.now(timezone.utc) - timedelta(hours=1))
    admin_ops.clients()
    assert admin_ops.resolve_issue(key, "", "will")["ok"]
    _sql(db_path, "DELETE FROM email_log")
    admin_ops.clients()                      # the condition is gone: the resolution retires
    assert not _rows(db_path, "SELECT key FROM admin_issue_resolutions WHERE key=?", (key,))
    assert _rows(db_path, "SELECT action FROM admin_issue_resolution_history WHERE key=? ORDER BY id",
                 (key,))[-1]["action"] == "cleared"


def test_a_rolling_condition_stays_resolved_while_it_lasts(db_path):
    """30 days of AI spend over $25 has no single occurrence: resolved, it
    holds while the condition lasts — a new AI call does not reopen it."""
    import ai_utils
    rid = _mk(db_path)
    ai_utils.log_ai_usage(rid, "insights", "claude-x", 10, 10, db_path=db_path)
    _sql(db_path, "UPDATE ai_usage SET cost_usd=30")
    assert admin_ops.resolve_issue(f"{rid}:ai_cost", "expected", "will")["scope"] == "condition"
    ai_utils.log_ai_usage(rid, "insights", "claude-x", 10, 10, db_path=db_path)
    assert not any(i["key"] == f"{rid}:ai_cost" for i in _rec(rid)["issues"])


def test_a_legacy_resolution_does_not_mute_a_newer_payment_failure(db_path):
    """`5:billing` resolved in October must not hide January's failure."""
    rid = _mk(db_path, billing_status="past_due")
    _sql(db_path, "INSERT INTO admin_issue_resolutions (key, resolved_at, note, actor) VALUES (?, ?, 'fixed', 'will')",
         (f"{rid}:billing", _utc(datetime.now(timezone.utc) - timedelta(days=90))))
    _sql(db_path, "INSERT INTO admin_events (source, event_type, restaurant_id, summary, created_at) "
                  "VALUES ('stripe', 'invoice.payment_failed', ?, 'x', ?)",
         (rid, _utc(datetime.now(timezone.utc) - timedelta(days=1))))
    issue = next(i for i in _rec(rid)["issues"] if i["key"] == f"{rid}:billing")
    assert issue["reopened"]["reason"] == "newer_occurrence"
    assert issue["since"] in ("1d", "24h", "23h")            # aged from the failure, not the owner's last click


def test_self_clearing_and_legal_issues_refuse_resolve(db_path, monkeypatch):
    rid = _mk(db_path)
    for key in ("scheduler", "platform:error_rate", f"{rid}:deletion"):
        out = admin_ops.resolve_issue(key, "", "will")
        assert out["ok"] is False and out["resolvable"] is False and out["error"]
    _as_admin(monkeypatch)
    r = _app().test_client().post("/admin/api/issues/resolve", json={"key": "scheduler"})
    assert r.status_code == 409 and r.get_json()["resolvable"] is False
    hb = next(i for i in admin_ops.overview()["issues"] if i["key"] == "scheduler")
    assert hb["resolvable"] is False


def test_the_console_can_send_the_occurrence_it_saw(db_path, monkeypatch):
    rid = _mk(db_path)
    _email_fail(db_path, rid, datetime.now(timezone.utc) - timedelta(hours=3))
    issue = next(i for i in _rec(rid)["issues"] if i["key"] == f"{rid}:email")
    _as_admin(monkeypatch)
    r = _app().test_client().post("/admin/api/issues/resolve",
                                  json={"key": issue["key"], "occurrence_at": issue["occurrence_at"]})
    assert r.status_code == 200 and r.get_json()["scope"] == "occurrence"
    row = _rows(db_path, "SELECT occurrence_at FROM admin_issue_resolutions WHERE key=?", (issue["key"],))[0]
    assert row["occurrence_at"] == issue["occurrence_at"]


# ── #32 / #36: one fleet build, shared, bounded ────────────────────────────

def test_the_memo_serves_request_threads_only(db_path):
    rid = _mk(db_path)
    app = _app()
    with app.test_request_context("/admin/api/overview"):
        a = admin_ops._records_cached()
        b = admin_ops._records_cached()
        assert a[2]["cached"] is False and b[2]["cached"] is True and b[0] is a[0]
        admin_ops.invalidate_fleet_cache()
        assert admin_ops._records_cached()[2]["cached"] is False
    with app.test_request_context("/admin/api/overview?fresh=1"):
        assert admin_ops._records_cached()[2]["cached"] is True           # seconds old: shared
        admin_ops._fleet_state["memo"]["at"] -= 10
        assert admin_ops._records_cached()[2]["cached"] is False          # Refresh rebuilds
    # Off a request thread every call builds: a direct caller sees its write.
    _sql(db_path, "UPDATE restaurants SET name='Renamed' WHERE id=?", (rid,))
    assert _rec(rid)["name"] == "Renamed"


def test_an_admin_write_drops_the_memo(db_path, monkeypatch):
    _mk(db_path)
    _as_admin(monkeypatch)
    client = _app().test_client()
    assert client.get("/admin/api/overview").status_code == 200
    gen = admin_ops._fleet_state["gen"]
    client.post("/admin/api/issues/resolve", json={"key": "fleet:fetch_coverage"})
    assert admin_ops._fleet_state["gen"] > gen


def test_a_request_past_the_waiter_limit_is_refused_with_retry_after(db_path, monkeypatch):
    _mk(db_path)
    _as_admin(monkeypatch)
    with admin_ops._fleet_cv:
        admin_ops._fleet_state.update(building=True, waiters=admin_ops.FLEET_MAX_WAITERS)
    try:
        r = _app().test_client().get("/admin/api/clients")
        assert r.status_code == 503 and r.headers["Retry-After"] == str(admin_ops.BUSY_RETRY_AFTER)
        assert r.headers["X-Admin-Busy"] == "1" and r.get_json()["busy"] is True
    finally:
        with admin_ops._fleet_cv:
            admin_ops._fleet_state.update(building=False, waiters=0)


def test_a_waiter_shares_the_build_in_flight(db_path, monkeypatch):
    _mk(db_path)
    started, release = threading.Event(), threading.Event()
    real = admin_ops._records
    calls = []

    def slow():
        calls.append(1)
        started.set()
        release.wait(5)
        return real()
    monkeypatch.setattr(admin_ops, "_records", slow)
    app = _app()
    out = {}

    def first():
        with app.test_request_context("/admin/api/overview"):
            out["a"] = admin_ops._records_cached()

    t = threading.Thread(target=first)
    t.start()
    started.wait(5)

    def second():
        with app.test_request_context("/admin/api/clients"):
            out["b"] = admin_ops._records_cached()
    t2 = threading.Thread(target=second)
    t2.start()
    release.set()
    t.join(10)
    t2.join(10)
    assert len(calls) == 1 and out["b"][0] is out["a"][0]


def test_badges_read_the_memo_and_count_by_segment(db_path, monkeypatch):
    rid = _mk(db_path, billing_status="past_due")
    _mk(db_path, "Demo Diner", is_demo=1, billing_status="past_due")
    _as_admin(monkeypatch)
    client = _app().test_client()
    client.get("/admin/api/overview")
    b = client.get("/admin/api/badges").get_json()
    assert b["ok"] and b["cached"] is True
    assert b["counts"]["customer"]["critical"] >= 1 and b["counts"]["internal"]["total"] >= 1
    assert b["kpis"]["past_due"] == 1 and rid


def test_issues_are_annotated_once_at_build(db_path):
    rid = _mk(db_path, billing_status="past_due", location_group="Corner Group", location_name="Downtown")
    ov = admin_ops.overview()
    i = next(x for x in ov["issues"] if x["key"] == f"{rid}:billing")
    assert (i["restaurant"], i["brand"], i["location_name"]) == ("Corner Bar", "Corner Group", "Downtown")


# ── #43 / #83: churn risk without a query per restaurant, paying only ──────

def test_outcome_counts_match_total_value(db_path):
    import outcomes
    from tests.test_outcomes_roi import _insert
    rid = _mk(db_path, billing_status="active", module_labor=1)
    _insert(db_path, rid, "Trim lunch", "labor_pct", 400.0, "2026-05-01")
    _insert(db_path, rid, "Trim dinner", "labor_pct", 250.0, "2026-07-01")
    _insert(db_path, rid, "Worse", "labor_pct", 100.0, "2026-06-01", verdict="worsened")
    _insert(db_path, rid, "Promo", "sales", 900.0, "2026-06-15")
    _sql(db_path, "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, status, "
                  "started_on, evaluate_on) VALUES (?, 'ask', 'ask:t', 'Tracking', 'labor_pct', 'tracking', "
                  "'2026-09-01', '2026-10-01')", (rid,))
    c = get_conn(db_path)
    got = admin_ops._outcome_counts(c, [rid])[rid]
    c.close()
    v = outcomes.total_value(rid, db_path=db_path)
    assert got == {"wins": v["wins"], "in_flight": v["in_flight"]}


def test_the_fleet_load_opens_no_connection_per_restaurant(db_path, monkeypatch):
    for i in range(12):
        rid = _mk(db_path, f"Old {i}", billing_status="active")
        _sql(db_path, "UPDATE restaurants SET created_at='2026-01-01T09:00:00' WHERE id=?", (rid,))
    opened = []
    real = admin_ops.get_conn
    monkeypatch.setattr(admin_ops, "get_conn", lambda *a, **k: opened.append(1) or real(*a, **k))
    import outcomes
    monkeypatch.setattr(outcomes, "total_value", lambda *a, **k: pytest.fail("total_value per restaurant"))
    admin_ops._records()
    assert len(opened) <= 3


def test_churn_risk_scores_paying_accounts_only(db_path):
    trial = _mk(db_path, "Trial Co", billing_status="trial")
    paying = _mk(db_path, "Paying Co", billing_status="active")
    for rid in (trial, paying):
        _sql(db_path, "UPDATE restaurants SET created_at='2026-01-01T09:00:00' WHERE id=?", (rid,))
    assert _rec(trial)["churn_risk"]["level"] == "n/a" and _rec(trial)["churn_risk"]["why_not"] == "not paying yet"
    c = _rec(paying)["churn_risk"]
    assert c["scored"] and c["level"] in ("medium", "high")


def test_high_risk_for_a_week_raises_the_value_recap_issue(db_path):
    rid = _mk(db_path, "Quiet Co", billing_status="past_due")
    _sql(db_path, "UPDATE restaurants SET created_at='2026-01-01T09:00:00' WHERE id=?", (rid,))
    assert _rec(rid)["churn_risk"]["level"] == "high"
    _sql(db_path, "INSERT INTO account_risk_state (restaurant_id, level, since, updated_at) VALUES (?, 'high', ?, ?)",
         (rid, _utc(datetime.now(timezone.utc) - timedelta(days=8)), _utc(datetime.now(timezone.utc))))
    issue = next(i for i in _rec(rid)["issues"] if i["key"] == f"{rid}:churn_risk")
    assert issue["action_route"] == f"/admin/api/client/{rid}/value-recap" and issue["action_kind"] == "post"


# ── #49: owner activity, from the owner ─────────────────────────────────────

def test_last_active_reads_sessions_and_ignores_staff_and_view_as(db_path):
    rid = _mk(db_path, billing_status="active")
    uid = auth.create_user(rid, "owner1", "owner1@x.test", "a-long-pass-1", db_path=db_path)
    uid = uid if isinstance(uid, int) else _rows(db_path, "SELECT id FROM users WHERE username='owner1'")[0]["id"]
    _sql(db_path, "UPDATE users SET last_login=? WHERE id=?",
         ((datetime.now(CT) - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%S"), uid))
    ios_at = datetime.now(timezone.utc) - timedelta(hours=2)
    _sql(db_path, "INSERT INTO sessions (token, user_id, expires_at, last_active, device_type) VALUES "
                  "('t1', ?, ?, ?, 'ios')", (uid, (datetime.now(timezone.utc) + timedelta(days=20)).isoformat(), _utc(ios_at)))
    # An admin's view-as and a staff PIN sign-in an hour ago are not the owner.
    _sql(db_path, "INSERT INTO sessions (token, user_id, expires_at, last_active, device_type) VALUES "
                  "('t2', ?, ?, datetime('now'), 'admin-view-as')", (uid, (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()))
    _sql(db_path, "INSERT INTO login_history (user_id, restaurant_id, event, device_type) VALUES (?, ?, 'pin_failed', 'staff_pin')",
         (uid, rid))
    rec = _rec(rid)
    assert rec["last_active"] == ios_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert rec["on_ios"] is True and not any(i["key"] == f"{rid}:inactive" for i in rec["issues"])
    _sql(db_path, "DELETE FROM sessions WHERE token='t1'")
    rec = _rec(rid)
    assert any(i["key"] == f"{rid}:inactive" and i["severity"] == "warning" for i in rec["issues"])
    assert rec["activity"]["sign_in"] and rec["activity"]["session"] is None


# ── #141 / #152: one account filter; onboarding the operator owns ──────────

def test_internal_accounts_never_count_toward_attention(db_path):
    real = _mk(db_path, "Real Co", billing_status="past_due")
    demo = _mk(db_path, "Demo Co", is_demo=1, billing_status="past_due")
    test = _mk(db_path, "Test Co", billing_status="past_due")
    _sql(db_path, "UPDATE restaurants SET exclude_from_learning=1 WHERE id=?", (test,))
    ov = admin_ops.overview()
    keys = {i["key"] for i in ov["issues"]}
    assert f"{real}:billing" in keys and f"{demo}:billing" not in keys and f"{test}:billing" not in keys
    assert {i["restaurant_id"] for i in ov["internal_issues"]} >= {demo, test}
    assert ov["issue_counts"]["internal"]["critical"] >= 2
    assert ov["kpis"]["past_due"] == 1 and ov["kpis"]["internal_accounts"] == 2
    assert ov["kpis"]["critical"] == ov["issue_counts"]["customer"]["critical"] + ov["issue_counts"]["platform"]["critical"]
    assert ov["issues_total"] == len([i for i in ov["issues"]]) or ov["issues_truncated"]


def test_the_operators_onboarding_issue_ignores_the_owners_hidden_card(db_path):
    rid = _mk(db_path, "Slow Co", billing_status="trial", module_reviews=1)
    _sql(db_path, "UPDATE restaurants SET created_at='2026-08-01T09:00:00', onboarding_dismissed=1 WHERE id=?", (rid,))
    issue = next(i for i in _rec(rid)["issues"] if i["key"] == f"{rid}:onboarding")
    assert "hid their setup card" in issue["detail"]
    churned = _mk(db_path, "Gone Co", billing_status="churned")
    demo = _mk(db_path, "Demo Onb", is_demo=1)
    ids = {r["id"] for r in admin_ops.onboarding_list()["rows"]}
    assert rid in ids and churned not in ids and demo not in ids


def test_recommendation_rates_leave_out_demo_and_say_nothing_measured(db_path):
    import rec_ledger
    real = _mk(db_path, "Rec Real")
    demo = _mk(db_path, "Rec Demo", is_demo=1)
    rec_ledger.present_many(demo, [dict(key=f"d:{i}", module="labor", kind="d") for i in range(5)], "home")
    rec_ledger.present_many(real, [dict(key=f"k:{i}", module="labor", kind="k") for i in range(20)], "home")
    for i in range(10):
        rec_ledger.record(real, f"k:{i}", "accepted")
    t = admin_ops.recommendation_acceptance(days=30)["total"]
    assert t["n"] == 20 and t["outcome_rate"] is None and t["ras"] is None
    assert t["ras_partial"] == round(100 * (0.15 * 0.5 + 0.40 * 0.5) / 0.80, 1)
    assert admin_ops.recommendation_acceptance(days=30, restaurant_id=demo)["total"]["n"] == 5


# ── #79: paged on the server, counted from the full set ─────────────────────

def test_issues_and_clients_are_paged_with_full_counts(db_path):
    for i in range(7):
        _mk(db_path, f"Due {i}", billing_status="past_due")
    p = admin_ops.issues_page(segment="customer", severity="critical", per_page=3, page=2)
    assert p["total"] >= 7 and len(p["items"]) == 3 and p["page"] == 2 and p["pages"] >= 3
    assert p["counts"]["customer"]["critical"] >= 7
    q = admin_ops.issues_page(q="Due 4")
    assert all("Due 4" in i["restaurant"] for i in q["items"]) and q["total"] >= 1
    c = admin_ops.clients_page(per_page=5, sort="name")
    assert c["total"] == 7 and len(c["items"]) == 5 and set(c["items"][0]) >= {"id", "health", "monthly", "issues"}
    assert c["counts"]["status"]["past_due"] == 7


def test_the_new_read_routes_answer_for_an_admin(db_path, monkeypatch):
    rid = _mk(db_path, billing_status="active")
    _as_admin(monkeypatch)
    client = _app().test_client()
    for path in ("/admin/api/badges", "/admin/api/issues/list?per_page=5", "/admin/api/clients/list",
                 "/admin/api/onboarding", "/admin/api/adoption", "/admin/api/queues", "/admin/api/data-sources",
                 f"/admin/api/client/{rid}/timeline", f"/admin/api/client/{rid}/billing/live",
                 "/admin/api/vendor-costs", "/admin/api/business-metrics", "/admin/api/issues/resolutions"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.data[:300])
        assert r.get_json()["ok"] is True, path
