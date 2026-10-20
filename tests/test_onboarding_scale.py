"""Onboarding at scale (scale audit 10/19/26, A8 §9 — fix agent F8).

What these protect:
  * ONB-1: a store backfills POS history only while it holds one of
    POS_BACKFILL_MAX_STORES slots and the volumes have room for tonight's
    days AND the backup after them; a held store still archives last night.
  * ONB-2: a CSV of stores is dry-run first, then applied idempotently per
    store (a second apply creates nothing), one row's failure takes only that
    row back out, the owner gets the set-password welcome (never a password),
    and the apply is behind the step-up and audited.
  * ONB-7: checklists and the DSR calendar load from the console, validated
    by the owner route's own rules.
  * ONB-9: the tracker carries the POS, history and setup steps, and the
    board counts stores per stage.
  * ONB-10: partners exist, and change nothing about a store's contract or
    billing until PARTNER_BILLING=1.
"""
import json
import sys
from datetime import date, datetime, timezone

import pytest
from flask import Flask

import admin_events
import admin_routes
import auth
import auth_routes
import billing_jobs
import bulk_provisioning  # noqa: F401  (imported before _world patches every bound get_conn)
import client_setup  # noqa: F401
import provisioning  # noqa: F401
import task_sheets  # noqa: F401
import models
import onboarding
import onboarding_routes
import ops
import pos_archive
from auth import create_session, create_user, init_auth
from models import Restaurant, create_restaurant, update_restaurant

CSRF = "onb-csrf"


@pytest.fixture(autouse=True)
def _world(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, auth, auth_routes, admin_routes, ops):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(pos_archive, "get_conn", lambda *a, **k: real(db_path))
    init_auth(db_path=db_path)
    models.init_email_log(db_path=db_path)
    monkeypatch.setattr(admin_routes, "_submit_admin_job", lambda job_id, fn, *a: fn(*a))
    monkeypatch.delenv("PARTNER_BILLING", raising=False)


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.secret_key = "onb"
    for bp in (admin_routes.admin_bp, auth_routes.auth_bp, onboarding_routes.onboarding_bp):
        flask_app.register_blueprint(bp)
    return flask_app


def _admin(app, db_path, stepped_up=True):
    hq = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="will@cavnar.test"), db_path=db_path)
    uid = create_user(hq, "will", "will@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, password_verified_at=True if stepped_up else None,
                                                 db_path=db_path))
    c.set_cookie("csrf_js", CSRF)
    return c


def _post(c, url, body=None):
    return c.post(url, json=body if body is not None else {}, headers={"X-CSRF": CSRF})


def _q(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ── ONB-1: backfill slots and room ───────────────────────────────────────────

def _store(db_path, n):
    rid = create_restaurant(Restaurant(name=f"Store {n}", owner_email=f"s{n}@x.test"), db_path=db_path)
    update_restaurant(rid, {"rpower_token": "tok", "rpower_cg": 1280, "rpower_store_mid": f"m{n}"}, db_path=db_path)
    return rid


def test_only_the_slot_cap_of_stores_backfill_at_once_and_a_finished_one_frees_its_slot(db_path, monkeypatch):
    monkeypatch.setattr(onboarding, "BACKFILL_MAX_STORES", 2)
    a, b, c = (_store(db_path, i) for i in range(3))
    assert onboarding.backfill_admission(a, "rpower", 80, 7)["admit"] is True
    assert onboarding.backfill_admission(b, "rpower", 80, 7)["admit"] is True
    held = onboarding.backfill_admission(c, "rpower", 80, 7)
    assert held["admit"] is False and held["reason"] == "waiting_for_slot" and held["slots_used"] == 2
    # A store keeps its slot night to night; finishing frees it for the next.
    assert onboarding.backfill_admission(a, "rpower", 73, 7)["admit"] is True
    assert onboarding.backfill_admission(a, "rpower", 0, 0)["reason"] == "complete"
    assert onboarding.backfill_admission(c, "rpower", 80, 7)["admit"] is True
    assert onboarding.backfill_state()["used"] == 2


def test_no_backfill_without_room_for_the_days_and_the_backup_after_them(db_path, monkeypatch):
    import shutil
    rid = _store(db_path, 9)
    real = shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage", lambda p: real(p)._replace(free=1024))
    out = onboarding.backfill_admission(rid, "rpower", 80, 7)
    assert out["admit"] is False and out["reason"] == "no_room" and "MB free" in out["detail"]
    row = _q(db_path, "SELECT held_reason FROM pos_backfill_slots WHERE restaurant_id=?", (rid,))[0]
    assert row["held_reason"], "the console says why the history waits"
    monkeypatch.setattr(shutil, "disk_usage", real)
    assert onboarding.backfill_admission(rid, "rpower", 80, 7)["admit"] is True


def test_a_held_store_still_archives_last_night(db_path, monkeypatch):
    import pos
    rid = _store(db_path, 5)
    calls = []

    class _Fake:
        def archive_rows(self, r, day):
            calls.append(day.isoformat())
            return {"tickets": [], "lines": [], "punches": [], "max_stamp": None}

        def changed_business_dates(self, r, since, until):
            return set()
    monkeypatch.setattr(pos, "PROVIDERS", None)
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", _Fake()))
    monkeypatch.setattr(onboarding, "backfill_admission",
                        lambda *a, **k: {"admit": False, "reason": "waiting_for_slot"})
    out = pos_archive.run_for(rid, today=date(2026, 9, 30), now_utc=datetime(2026, 9, 30, 9, tzinfo=timezone.utc))
    assert calls == ["2026-09-29"], "only last night: the history waits for a slot"
    assert out["backfill_held"] == "waiting_for_slot" and out["backfill_left"] == pos_archive.BACKFILL_DAYS - 1


def test_the_backup_reads_the_live_database_size_on_either_backend(db_path):
    assert onboarding.live_database_bytes(db_path) > 0


# ── ONB-2: bulk provisioning ─────────────────────────────────────────────────

CSV = """restaurant_name,owner_email,owner_name,timezone,modules,rpower_cg,rpower_store_mid
Wave One,one@wave.test,Ann One,America/New_York,"reviews,labor",1280,9001
Wave Two,two@wave.test,Bo Two,,reviews,1280,9002
Bad Row,not-an-email,,,,,
"""


def test_a_list_is_dry_run_first_and_nothing_is_written(db_path):
    import bulk_provisioning as bp
    rows, err = bp.parse_csv(CSV)
    assert err is None and len(rows) == 3
    plan = bp.plan(rows)
    assert plan["counts"] == {"create": 2, "exists": 0, "error": 1}
    assert "email" in plan["rows"][2]["error"]
    assert not _q(db_path, "SELECT id FROM restaurants WHERE name LIKE 'Wave%'")


def test_applying_a_list_creates_each_store_once_with_a_welcome_and_no_password(db_path, monkeypatch):
    import bulk_provisioning as bp
    owed = []
    monkeypatch.setattr(billing_jobs, "enqueue", lambda kind, rid, key, **k: owed.append((kind, rid, k)) or (1, True))
    rows, _ = bp.parse_csv(CSV)
    out = bp.apply(rows, "will")
    assert out["ok"] and out["counts"] == {"created": 2, "exists": 0, "error": 1}
    made = {r["restaurant_name"]: r["restaurant_id"] for r in out["rows"] if r["action"] == "created"}
    one = models.get_restaurant(made["Wave One"])
    assert one.timezone == "America/New_York" and one.module_labor == 1 and one.rpower_store_mid == "9001"
    assert not one.rpower_token, "no token in the list: the store is reserved, the token pasted later"
    assert [k for k, _r, _p in owed] == ["welcome", "welcome"], "THE welcome: a set-password link, owed"
    # Applied again: every store already exists; nothing new.
    again = bp.apply(rows, "will")
    assert again["counts"] == {"created": 0, "exists": 2, "error": 1}
    assert len(_q(db_path, "SELECT id FROM restaurants WHERE name LIKE 'Wave%'")) == 2
    assert len(_q(db_path, "SELECT * FROM provision_rows")) == 6


def test_a_row_that_fails_part_way_is_taken_back_out_and_the_rest_go_on(db_path, monkeypatch):
    import bulk_provisioning as bp
    monkeypatch.setattr(billing_jobs, "enqueue", lambda *a, **k: (1, True))
    real = models.update_restaurant

    def flaky(rid, fields, *a, **k):
        if fields.get("rpower_store_mid") == "9002":
            raise RuntimeError("disk said no")
        return real(rid, fields, *a, **k)
    monkeypatch.setattr(models, "update_restaurant", flaky)
    rows, _ = bp.parse_csv(CSV)
    out = bp.apply(rows, "will")
    assert out["counts"]["created"] == 1 and out["counts"]["error"] == 2
    assert not _q(db_path, "SELECT id FROM restaurants WHERE name='Wave Two'"), "the half-made row is gone"
    assert not _q(db_path, "SELECT id FROM users WHERE email='two@wave.test'")


def test_a_partner_token_binds_each_store_it_sees_and_refuses_one_it_does_not(db_path, monkeypatch):
    import bulk_provisioning as bp
    import rpower
    monkeypatch.setattr(billing_jobs, "enqueue", lambda *a, **k: (1, True))
    monkeypatch.setattr(rpower, "test_token", lambda t: {"ok": True, "stores": [
        {"cg": 1280, "store_mid": "9001", "name": "Wave One POS", "timezone": "America/Chicago", "ot_dow": 3}]})
    rows, _ = bp.parse_csv(CSV)
    out = bp.apply(rows, "will", rpower_token="partner-tok")
    by = {r["restaurant_name"]: r for r in out["rows"]}
    assert by["Wave One"]["action"] == "created" and by["Wave Two"]["action"] == "error", {k: v.get("error") for k, v in by.items()}
    assert "cannot see" in by["Wave Two"]["error"]
    one = models.get_restaurant(by["Wave One"]["restaurant_id"])
    assert one.rpower_token == "partner-tok" and one.rpower_store_name == "Wave One POS"
    assert one.week_start_day == 0, "the payroll week is suggested, never applied (ONB-11)"
    assert onboarding.confirmations(one.id)["week_start"]["suggested"]["week_start_day"] == 2


def test_the_apply_route_needs_the_step_up_and_is_audited(app, db_path, monkeypatch):
    monkeypatch.setattr(billing_jobs, "enqueue", lambda *a, **k: (1, True))
    c = _admin(app, db_path, stepped_up=False)
    r = _post(c, "/admin/api/onboarding/provision/apply", {"csv": CSV})
    assert r.status_code == 403 and r.get_json()["reauth_required"] is True
    assert not _q(db_path, "SELECT id FROM restaurants WHERE name LIKE 'Wave%'")
    # The dry run writes nothing and needs no step-up.
    dry =_post(c, "/admin/api/onboarding/provision/dry-run", {"csv": CSV}).get_json()
    assert dry["counts"]["create"] == 2


def test_the_apply_route_runs_on_the_admin_pool_and_records_the_batch(app, db_path, monkeypatch):
    monkeypatch.setattr(billing_jobs, "enqueue", lambda *a, **k: (1, True))
    c = _admin(app, db_path)
    r = _post(c, "/admin/api/onboarding/provision/apply", {"csv": CSV, "send_welcome": False})
    assert r.status_code == 200, r.get_json()
    job = c.get(f"/admin/api/onboarding/provision/{r.get_json()['job_id']}").get_json()
    assert job["counts"]["created"] == 2
    actions = {row["event_type"] for row in _q(db_path, "SELECT event_type FROM admin_events")}
    assert {"bulk_provision.started", "bulk_provision.applied"} <= actions


# ── ONB-7: checklists and the DSR calendar from the console ─────────────────

DOC = {"sheets": [{"job_code": "Server AM", "shift_kind": "opening", "title": "Server opening",
                   "sections": [{"title": "Floor", "items": [{"label": "Check Tock"},
                                                             {"label": "Stock", "items": ["straws", "cups"]}]}]}]}


def test_checklists_load_from_the_console_dry_run_first_and_once(app, db_path):
    import task_sheets
    rid =create_restaurant(Restaurant(name="Sheet Co", owner_email="s@sheet.test"), db_path=db_path)
    c = _admin(app, db_path)
    dry = _post(c, f"/admin/api/onboarding/{rid}/checklists", {"doc": DOC}).get_json()
    assert dry["ok"] and dry["lines"] == 3 and not task_sheets.list_sheets(rid)
    done = _post(c, f"/admin/api/onboarding/{rid}/checklists", {"doc": json.dumps(DOC), "apply": True}).get_json()
    assert done == {"ok": True, "sheets_created": 1, "lines_added": 3}
    again = _post(c, f"/admin/api/onboarding/{rid}/checklists", {"doc": DOC, "apply": True}).get_json()
    assert again["lines_added"] == 0


def test_the_dsr_calendar_is_set_by_preset_with_the_owner_routes_rules(app, db_path):
    rid = create_restaurant(Restaurant(name="Fiscal Co", owner_email="f@fiscal.test"), db_path=db_path)
    c = _admin(app, db_path)
    bad = _post(c, f"/admin/api/onboarding/{rid}/dsr", {"preset": "445", "fiscal_year_start": "2025-12-31",
                                                        "fiscal_week_start_dow": 0, "apply": True})
    assert bad.status_code == 400 and "has to start on a Monday" in bad.get_json()["error"]
    ok = _post(c, f"/admin/api/onboarding/{rid}/dsr", {"preset": "445", "fiscal_year_start": "2025-12-31",
                                                       "fiscal_week_start_dow": 2, "apply": True}).get_json()
    assert ok["ok"]
    r = models.get_restaurant(rid)
    assert (r.fiscal_period_scheme, r.fiscal_year_start, r.fiscal_week_start_dow) == ("445", "2025-12-31", 2)
    assert onboarding.confirmations(rid)["dsr_calendar"]["status"] == "confirmed"


# ── ONB-9: the tracker's steps and the board ────────────────────────────────

def test_a_new_rpower_store_carries_pos_history_and_setup_steps(db_path):
    r = {"id": 1, "rpower_token": "t", "rpower_store_mid": "m", "rpower_last_synced": "2026-10-20",
         "module_labor": 1, "dsr_enabled": 1, "created_at": "2026-10-21 10:00:00"}
    opened = {"confirmations": {"week_start": {"status": "pending", "suggested": {"week_start_day": 2}}}}
    steps = {s["key"]: s for s in onboarding.extra_steps(r, dict(opened, archive_days=30, dsr_nights=59))}
    assert steps["pos_bound"]["done"] and steps["pos_first_data"]["done"]
    assert not steps["pos_history"]["done"] and steps["pos_history"]["left"] == 59
    assert steps["dsr_history"]["done"]
    assert {"setup:week_start", "setup:dsr_calendar", "checklists"} <= set(steps)
    assert not steps["setup:week_start"]["done"]
    # A restaurant set up before the checklist existed (no setup rows) is not
    # asked again, and its auto-draft is never held.
    assert not any(s["key"].startswith("setup:") for s in onboarding.extra_steps(r, {"archive_days": 89}))
    rid = create_restaurant(Restaurant(name="Old Co", owner_email="o@old.test"), db_path=db_path)
    update_restaurant(rid, {"module_labor": 1}, db_path=db_path)
    assert onboarding.labor_setup_pending(models.get_restaurant(rid)) == []
    onboarding.start_setup(rid)
    assert "week_start" in onboarding.labor_setup_pending(models.get_restaurant(rid))
    for k in ("week_start", "roles_rates", "salaried", "station_logins", "floors_closers", "availability"):
        onboarding.confirm(rid, k, value=2 if k == "week_start" else None, who="will")
    assert onboarding.labor_setup_pending(models.get_restaurant(rid)) == []


def test_the_board_counts_stores_per_stage_and_names_the_stuck(db_path):
    rows = [{"id": 1, "name": "A", "days_since_signup": 30,
             "steps": [{"key": "contract", "done": True}, {"key": "login", "done": True},
                       {"key": "pos_bound", "done": True}, {"key": "pos_history", "done": False}]},
            {"id": 2, "name": "B", "days_since_signup": 2, "steps": [{"key": "contract", "done": False}]},
            {"id": 3, "name": "C", "days_since_signup": 40, "steps": [{"key": "contract", "done": True}]}]
    b = onboarding.board(rows)
    counts = {s["key"]: s["count"] for s in b["stages"]}
    assert counts["history"] == 1 and counts["contract"] == 1 and counts["live"] == 1
    assert [s["id"] for s in b["stuck"]] == [1]


# ── ONB-10: partners change nothing until PARTNER_BILLING=1 ─────────────────

def test_a_partner_covers_contract_and_billing_only_when_switched_on(db_path, monkeypatch):
    rid = create_restaurant(Restaurant(name="Partner Store", owner_email="p@p.test"), db_path=db_path)
    pid = onboarding.create_partner("RPOWER Resale", billing_mode="partner", master_agreement_signed_at="2026-10-19")
    onboarding.attach_store(pid, rid)
    assert onboarding.skips_direct_contract(rid) is False
    facts = {"partner": {"billing_mode": "partner", "master_agreement_signed_at": "2026-10-19"}}
    assert onboarding.partner_covers(facts, "contract") is False
    monkeypatch.setenv("PARTNER_BILLING", "1")
    assert onboarding.skips_direct_contract(rid) is True and onboarding.partner_covers(facts, "billing") is True


def test_the_auto_draft_waits_for_a_new_stores_setup(db_path, monkeypatch):
    import strategy_jobs
    rid = create_restaurant(Restaurant(name="Draft Co", owner_email="d@draft.test"), db_path=db_path)
    update_restaurant(rid, {"module_labor": 1, "auto_draft_schedule": 1}, db_path=db_path)
    onboarding.start_setup(rid)
    out = strategy_jobs.run_auto_draft_schedules(db_path=db_path, enqueue=True)
    assert out["held_for_setup"] == 1


def test_a_new_client_and_a_provisioned_store_open_their_setup_checklist(db_path, monkeypatch):
    import bulk_provisioning as bp
    monkeypatch.setattr(billing_jobs, "enqueue", lambda *a, **k: (1, True))
    rows, _ = bp.parse_csv(CSV)
    out = bp.apply(rows, "will")
    rid = next(r["restaurant_id"] for r in out["rows"] if r["action"] == "created")
    assert set(onboarding.confirmations(rid)) == set(onboarding.SETUP_STEP_KEYS)
    import inspect
    assert "start_setup(rid)" in inspect.getsource(admin_routes.create_client)
