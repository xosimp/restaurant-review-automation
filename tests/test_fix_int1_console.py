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
