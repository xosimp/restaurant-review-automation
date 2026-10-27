"""Scale re-audit 10/20/26, fix agent G8 — onboarding at scale, backup and
restore, disaster recovery, observability (R8-03 … R8-23, R7-12, and the
R1-01 / R2-03 backup residue).

What these protect:
  * R8-03: the nightly backup and the restore drill are job-queue rows the
    queue worker runs (the maintenance group, one at a time), never inline
    on the scheduler loop; the prune and the drill wait for tonight's
    backup to finish.
  * R8-04 / R8-05: a database put back to an earlier point under the running
    service freezes scheduling and the queue until a person confirms; the
    queue claims nothing during RESTORE_FROM.
  * R8-12: RESTORE_FROM on the Postgres backend refuses loudly.
  * R8-06: the Postgres volume is measured against PG_VOLUME_MAX_BYTES and
    paged; unset is a warning.
  * R8-14: storm pages are one fleet page; a missing DSR pages only above a
    share of the DSR fleet.
  * R8-17: the export copies partitioned tables partition by partition.
  * R8-18: the scrub compacts beside the file, never in the temp directory.
  * R8-07 / R8-10 / R8-19 / R7-12: bulk provisioning's billing status, its
    onboarding archive, one apply at a time, the owner match shown.
  * R8-08 / R8-09 / R8-21: the backfill cap from the fleet budget, stale
    slots released, the history step done at 60 days.
  * R8-11: the owner confirms the labor setup from Account; R8-22: partner
    attach needs the step-up.
"""
import inspect
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

import job_queue
import jobs_registry
import models
import ops
import scheduler


@pytest.fixture
def db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default) else path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirected)
        except Exception:
            pass
    monkeypatch.setattr(models, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    for name in ("RESTORE_FROM", "MAINTENANCE_READ_ONLY", "RESTORE_GUARD", "BULK_PROVISION_BILLING_STATUS",
                 "PG_VOLUME_MAX_BYTES"):
        monkeypatch.delenv(name, raising=False)
    ops._claim_fallback.clear()
    import auth
    auth.init_auth(db_path=db_path)
    return db_path


def _q(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _x(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


_COUNTS = {"attempted": 1, "ok": 1, "failed": 0, "skipped": 0, "hit_bound": False}


# ── R8-03: the backup is a queue row the worker runs ────────────────────────

def test_the_backup_and_the_drill_are_maintenance_rows_never_inline_on_the_loop():
    loop = inspect.getsource(scheduler.scheduler_loop)
    assert 'queue_scheduled_job("backup_db", str(today))' in loop
    assert 'queue_scheduled_job("restore_drill"' in loop
    assert 'run_job("backup_db"' not in loop and 'run_job("restore_drill"' not in loop
    assert job_queue.KINDS["scheduled_job"][0] == "maintenance" and "maintenance" in job_queue.GROUPS
    assert job_queue.group_concurrency("maintenance") == 1 and job_queue.max_running("maintenance") == 1
    assert jobs_registry.JOBS["backup_db"]["queue"] == "maintenance"


def test_a_queued_backup_runs_through_run_job_under_its_own_name(db, monkeypatch):
    ran = []
    monkeypatch.setattr(scheduler, "backup_db", lambda: ran.append(1) or dict(_COUNTS))
    qid = scheduler.queue_scheduled_job("backup_db", "2026-10-20")
    assert scheduler.queue_scheduled_job("backup_db", "2026-10-20") == qid, "one row per period"
    assert not scheduler._scheduled_job_settled("backup_db", "2026-10-20")
    job = job_queue.claim("test-worker", groups=("maintenance",))
    assert job and job["kind"] == "scheduled_job" and job["grp"] == "maintenance"
    assert job_queue.run_one(job) == {"job": "backup_db", "ok": True}
    assert ran == [1]
    run = _q(db, "SELECT job, ok, claim_job FROM job_runs WHERE job='backup_db'")
    assert run and run[-1]["ok"] == 1
    assert scheduler._scheduled_job_settled("backup_db", "2026-10-20")


def test_a_failed_queued_backup_is_tried_again_by_the_queue(db, monkeypatch):
    def boom():
        raise RuntimeError("volume full")
    monkeypatch.setattr(scheduler, "backup_db", boom)
    scheduler.queue_scheduled_job("backup_db", "2026-10-21")
    job = job_queue.claim("test-worker", groups=("maintenance",))
    job_queue.run_one(job)
    row = _q(db, "SELECT state, attempts FROM jobs WHERE id=?", (job["id"],))[0]
    assert row["state"] == "queued" and row["attempts"] == 1, "a retryable non-sending job goes back on the queue"


def test_the_prune_and_the_drill_wait_for_tonights_backup_until_six(db):
    now = datetime(2026, 11, 2, 2, 30)
    assert scheduler._backup_settled(now, now.date()) is False, "no row yet: the backup has not run"
    scheduler.queue_scheduled_job("backup_db", str(now.date()))
    assert scheduler._backup_settled(now, now.date()) is False
    _x(db, "UPDATE jobs SET state='done'")
    assert scheduler._backup_settled(now, now.date()) is True
    assert scheduler._backup_settled(datetime(2026, 11, 3, 6, 5), datetime(2026, 11, 3).date()) is True


def test_run_now_of_a_queued_job_queues_it_for_the_worker(db, monkeypatch):
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    import admin_ops
    out = admin_ops.run_job_now("backup_db", "will")
    assert out["ok"] and out["queued"]

    class _Pulsed:
        def _already_running(self, *a):
            return None
    assert scheduler._run_manual_requests(_Pulsed()) == 1
    rows = _q(db, "SELECT kind, external_id FROM jobs")
    assert rows and rows[0]["kind"] == "scheduled_job" and rows[0]["external_id"].startswith("run_now:backup_db:")


def test_the_drill_refuses_to_start_without_room(tmp_path, monkeypatch):
    import shutil
    real = shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage", lambda p: real(p)._replace(free=10 * 1048576))
    with pytest.raises(RuntimeError, match="not enough free space"):
        scheduler._drill_room(str(tmp_path), 100 * 1048576, "the decompressed snapshot")
    scheduler._drill_room(str(tmp_path), 1048576, "the decompressed snapshot")


def test_the_backup_peak_is_what_the_warnings_ahead_of_it_read():
    import onboarding
    import status_manager
    assert scheduler.BACKUP_PEAK_SPACE_FACTOR == pytest.approx(2.75)
    assert scheduler.BACKUP_FREE_SPACE_FACTOR < scheduler.BACKUP_PEAK_SPACE_FACTOR < status_manager.DISK_LOW_DB_MULTIPLE
    assert "BACKUP_PEAK_SPACE_FACTOR" in inspect.getsource(onboarding.storage_room)


# ── R8-18: the scrub compacts beside the file ───────────────────────────────

def test_the_scrub_compacts_beside_the_copy_not_in_the_temp_directory(db, tmp_path):
    import offsite_backup
    copy = str(tmp_path / "offsite_work.db")
    import shutil
    shutil.copyfile(db, copy)
    offsite_backup.redact(copy)
    assert not os.path.exists(copy + ".vacuum")
    c = sqlite3.connect(copy)
    try:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("PRAGMA freelist_count").fetchone()[0] == 0
    finally:
        c.close()
    src = inspect.getsource(offsite_backup.redact)
    assert 'VACUUM INTO' in src and 'conn.execute("VACUUM")' not in src


# ── R8-04 / R8-05: a restored database freezes the sends ────────────────────

def test_a_database_restored_under_the_service_freezes_scheduling_and_the_queue(db, monkeypatch):
    import config
    import restore_guard
    monkeypatch.setenv("RESTORE_GUARD", "1")
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    paged = []
    monkeypatch.setattr(ops, "page_operator", lambda key, *a, **k: paged.append(key) or {"sent": True})
    for _ in range(3):
        assert restore_guard.check_and_stamp()["state"] == "ok"
    assert config.scheduling_allowed()
    # The database goes back two ticks: a Railway restore under the service.
    value = _q(db, "SELECT value FROM job_cursors WHERE key='restore_epoch'")[0]["value"]
    db_id, seq = value.rsplit(":", 1)
    _x(db, "UPDATE job_cursors SET value=? WHERE key='restore_epoch'", (f"{db_id}:{int(seq) - 2}",))
    assert restore_guard.check_and_stamp()["state"] == "detected"
    assert paged == ["restore_detected"]
    assert restore_guard.check_and_stamp()["state"] == "frozen"
    assert config.restore_frozen() and not config.scheduling_allowed()
    assert scheduler._restore_guard_holds() is True
    job_queue.enqueue("auto_publish", {}, restaurant_id=None, wake=False)
    assert job_queue.frozen() == "restore_detected" and job_queue.claim("w") is None
    # A person reconciles and confirms: the next tick runs.
    assert restore_guard.confirm("will")["was_frozen"]
    assert not config.restore_frozen() and config.scheduling_allowed()
    assert restore_guard.check_and_stamp()["state"] == "ok"


def test_a_new_database_is_baselined_never_frozen(db, monkeypatch):
    import restore_guard
    monkeypatch.setenv("RESTORE_GUARD", "1")
    restore_guard.check_and_stamp()
    _x(db, "DELETE FROM job_cursors WHERE key='restore_epoch'")       # a fresh database
    assert restore_guard.check_and_stamp()["state"] == "ok"
    assert restore_guard.frozen() is None


def test_the_guard_is_off_off_railway_by_default(db, monkeypatch):
    import restore_guard
    for v in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID"):
        monkeypatch.delenv(v, raising=False)
    assert restore_guard.check_and_stamp() == {"state": "off"}
    assert not os.path.exists(restore_guard.epoch_path())


def test_the_queue_claims_nothing_during_a_restore(db, monkeypatch):
    job_queue.enqueue("auto_publish", {}, restaurant_id=None, wake=False)
    monkeypatch.setenv("RESTORE_FROM", "/app/data/backups/x.db.gz")
    assert job_queue.frozen() == "restore_from" and job_queue.claim("w") is None
    monkeypatch.delenv("RESTORE_FROM")
    monkeypatch.setenv("MAINTENANCE_READ_ONLY", "1")
    assert job_queue.claim("w") is None


def test_restore_from_refuses_on_the_postgres_backend(db, monkeypatch, tmp_path):
    import db_restore
    snap = tmp_path / "snap.db"
    snap.write_bytes(b"")
    monkeypatch.setenv("RESTORE_FROM", str(snap))
    monkeypatch.setenv("ALLOW_LOCAL_PG", "1")    # off Railway DATABASE_URL is ignored otherwise
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@h/db")
    monkeypatch.delenv("DB_BACKEND", raising=False)
    with pytest.raises(RuntimeError, match="serves Postgres"):
        db_restore.restore_if_requested(db_path=db)
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    with pytest.raises(RuntimeError, match="failed its checks"):
        db_restore.restore_if_requested(db_path=db)    # the SQLite rollback path is still the old one


# ── R8-06: the Postgres volume ──────────────────────────────────────────────

def test_the_postgres_volume_pages_critical_and_warns_low_or_unset(db, monkeypatch):
    import status_manager
    assert status_manager.pg_volume_state(db)["state"] in ("n/a", "unset", "ok")
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    for state, urgent in (("critical", True), ("low", False), ("unset", False)):
        pv = {"state": state, "used_mb": 900, "max_mb": 1000, "pct_used": 90.0, "db_mb": 850, "wal_mb": 50}
        monkeypatch.setattr(status_manager, "pg_volume_state", lambda *a, _pv=pv, **k: _pv)
        out = ops.check_platform_sla(send=False, db_path=db, write_ok=True)
        assert any("Postgres" in p for p in out["problems"]), (state, out["problems"])
        # The volume's urgency alone: on CI's long Postgres run the shared
        # database also held a 46-minute-old scheduler heartbeat, which is
        # urgent on its own and read "low" as a page (10/24/26).
        assert out.get("pg_volume", {}).get("state") == state
        assert ops._has_urgent({"pg_volume": out["pg_volume"]}, {}, write_ok=True) is urgent, state


def test_the_app_volume_is_judged_against_the_live_database_on_postgres(db):
    import status_manager
    small = status_manager.disk_state(db, live_mb=1.0)
    assert small["backend"] == "postgres" and small["db_mb"] == 1.0
    huge = status_manager.disk_state(db, live_mb=10 ** 9)
    assert huge["state"] == "critical"


# ── R8-14: pages that scale with the fleet ──────────────────────────────────

def test_a_missing_dsr_pages_only_above_a_share_of_the_dsr_fleet(db):
    missing = [{"restaurant_id": 9001, "restaurant": "Lone"}]
    assert ops._dsr_missing_pages(missing, db) is True, "one store of one: still a page"
    rids = [models.create_restaurant(models.Restaurant(name=f"DSR {i}", owner_email=f"d{i}@x.test"), db_path=db)
            for i in range(100)]
    c = sqlite3.connect(db)
    for rid in rids:
        c.execute("INSERT INTO dsr_reports (restaurant_id, business_date, status) VALUES (?, ?, 'final')",
                  (rid, (datetime.utcnow().date() - timedelta(days=1)).isoformat()))
    c.commit()
    c.close()
    assert ops._dsr_missing_pages(missing, db) is False, "one store of 101: the console's list, not a page"
    assert ops._dsr_missing_pages([{"restaurant_id": i} for i in rids[:8]], db) is True


def test_an_alert_storm_is_one_fleet_page(db, monkeypatch):
    import notify
    rids = [models.create_restaurant(models.Restaurant(name=f"Storm {i}", owner_email=f"s{i}@x.test"))
            for i in range(3)]
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    pages = []
    monkeypatch.setattr(ops, "page_operator", lambda key, subject, lines, cooldown_minutes=0:
                        pages.append((key, subject, cooldown_minutes)) or {"sent": True})
    monkeypatch.setattr(ops, "alert_will", lambda *a, **k: pytest.fail("a per-restaurant alert_will"))
    for rid in rids:
        notify._apply_storm_cap(rid, 99, db)
    assert [p[0] for p in pages] == ["alert_storm"] * 3 and all(p[2] == 60 for p in pages)
    assert "3 restaurants" in pages[-1][1]


# ── R8-07 / R8-10 / R8-19 / R7-12: bulk provisioning ────────────────────────

CSV = """restaurant_name,owner_email,owner_name,modules,rpower_cg,rpower_store_mid
Bulk One,one@bulk.test,One,"reviews,labor",1280,7001
"""


def test_bulk_stores_start_at_the_configured_billing_status_and_the_dry_run_says_so(db, monkeypatch):
    import billing_jobs
    import bulk_provisioning as bp
    monkeypatch.setattr(billing_jobs, "enqueue", lambda *a, **k: (1, True))
    rows, _ = bp.parse_csv(CSV)
    plan = bp.plan(rows)
    assert plan["billing"]["status"] == "trial" and plan["billing"]["ok"] and "trial" in plan["billing"]["note"]
    monkeypatch.setenv("BULK_PROVISION_BILLING_STATUS", "comped")
    assert bp.plan(rows)["billing"]["ok"] is False
    assert bp.apply(rows, "will")["ok"] is False, "an unknown status refuses the apply"
    monkeypatch.setenv("BULK_PROVISION_BILLING_STATUS", "active")
    out = bp.apply(rows, "will", send_welcome=False)
    rid = out["rows"][0]["restaurant_id"]
    assert models.get_restaurant(rid).billing_status == "active"


def test_a_token_bound_store_gets_its_onboarding_archive_queued(db, monkeypatch):
    import billing_jobs
    import bulk_provisioning as bp
    import rpower
    import strategy_jobs
    queued = []
    monkeypatch.setattr(billing_jobs, "enqueue", lambda *a, **k: (1, True))
    monkeypatch.setattr(strategy_jobs, "enqueue_pos_onboard", lambda rid, key="": queued.append((rid, key)) or True)
    monkeypatch.setattr(rpower, "test_token", lambda t: {"ok": True, "stores": [
        {"cg": 1280, "store_mid": "7001", "name": "Bulk One POS"}]})
    rows, _ = bp.parse_csv(CSV)
    out = bp.apply(rows, "will", rpower_token="partner-tok", send_welcome=False)
    assert queued == [(out["rows"][0]["restaurant_id"], "1280:7001")]


def test_a_second_apply_waits_for_the_running_one(db, monkeypatch):
    import billing_jobs
    import bulk_provisioning as bp
    monkeypatch.setattr(billing_jobs, "enqueue", lambda *a, **k: (1, True))
    _x(db, "INSERT INTO provision_batches (created_by, status) VALUES ('other-admin', 'running')")
    rows, _ = bp.parse_csv(CSV)
    out = bp.apply(rows, "will")
    assert out["ok"] is False and "being applied now" in out["error"]
    assert not _q(db, "SELECT id FROM restaurants WHERE name='Bulk One'")
    # A crashed apply (running for hours) does not hold the next one forever.
    _x(db, "UPDATE provision_batches SET created_at=datetime('now','-3 hours')")
    assert bp.apply(rows, "will", send_welcome=False)["counts"]["created"] == 1


def test_a_row_matched_since_the_plan_is_left_alone(db, monkeypatch):
    import billing_jobs
    import bulk_provisioning as bp
    monkeypatch.setattr(billing_jobs, "enqueue", lambda *a, **k: (1, True))
    rows, _ = bp.parse_csv(CSV)
    real_plan = bp.plan

    def plan_then_race(rows_, db_path=None, **k):
        p = real_plan(rows_, db_path, **k)
        rid = models.create_restaurant(models.Restaurant(name="Raced", owner_email="r@x.test"))
        models.update_restaurant(rid, {"rpower_cg": 1280, "rpower_store_mid": "7001"})
        return p
    monkeypatch.setattr(bp, "plan", plan_then_race)
    out = bp.apply(rows, "will", send_welcome=False)
    assert out["counts"]["created"] == 0 and out["counts"]["exists"] == 1


def test_the_dry_run_shows_a_row_that_joins_an_existing_owner(db):
    import auth
    import bulk_provisioning as bp
    auth.init_auth(db_path=db)
    first = models.create_restaurant(models.Restaurant(name="Owner HQ", owner_email="one@bulk.test"))
    auth.create_user(first, "onebulk", "one@bulk.test", "Pass-word-2026!", role="owner")
    rows, _ = bp.parse_csv(CSV.replace("1280,7001", ","))
    plan = bp.plan(rows)
    # Since Audit #23 (A7-02) a join needs the row's own merge_owner=yes:
    # without it the dry run shows the join as an error to fix.
    assert plan["rows"][0]["action"] == "error" and "merge_owner=yes" in plan["rows"][0]["error"]
    assert plan["rows"][0]["joins_owner"]["restaurant_id"] == first
    for row in rows:
        row["merge_owner"] = "yes"
    plan = bp.plan(rows)
    assert plan["rows"][0]["action"] == "create" and plan["rows"][0]["joins_owner"]["restaurant_id"] == first
    assert plan["joins_owner"] == 1
    # An admin login with the same address never matches.
    _x(db, "UPDATE users SET is_admin=1 WHERE email='one@bulk.test'")
    assert not bp.plan(rows)["rows"][0].get("joins_owner")


# ── R8-08 / R8-09 / R8-21: backfill throughput and slots ────────────────────

def test_the_backfill_cap_is_sized_from_the_fleet_budget(monkeypatch):
    import onboarding
    import rpower
    monkeypatch.setattr(onboarding, "BACKFILL_MAX_STORES", None)
    monkeypatch.setattr(rpower, "FLEET_RPS", 10.0)
    assert onboarding.backfill_cap() == onboarding.BACKFILL_CAP_CEILING
    monkeypatch.setattr(rpower, "FLEET_RPS", 0.1)
    assert onboarding.backfill_cap() == onboarding.BACKFILL_CAP_FLOOR
    monkeypatch.setattr(onboarding, "BACKFILL_MAX_STORES", 3)
    assert onboarding.backfill_cap() == 3, "an operator's cap is kept as given"


def _store(n, token=True):
    rid = models.create_restaurant(models.Restaurant(name=f"Slot {n}", owner_email=f"slot{n}@x.test"))
    models.update_restaurant(rid, {"rpower_token": "tok" if token else None, "rpower_cg": 1280,
                                   "rpower_store_mid": f"s{n}"})
    return rid


def test_a_disconnected_or_failing_store_releases_its_slot(db, monkeypatch):
    import onboarding
    import pos_archive
    monkeypatch.setattr(onboarding, "BACKFILL_MAX_STORES", 2)
    a, b, c = (_store(i) for i in range(3))
    connected = {a, b, c}
    monkeypatch.setattr(pos_archive, "provider_for", lambda rid: ("rpower", object()) if rid in connected
                        else (None, None))
    assert onboarding.backfill_admission(a, "rpower", 80, 7)["admit"]
    assert onboarding.backfill_admission(b, "rpower", 80, 7)["admit"]
    assert onboarding.backfill_admission(c, "rpower", 80, 7)["reason"] == "waiting_for_slot"
    connected.discard(a)                       # a's token was removed
    assert onboarding.backfill_admission(c, "rpower", 80, 7)["admit"], "a's slot was released for c"
    why = _q(db, "SELECT held_reason FROM pos_backfill_slots WHERE restaurant_id=?", (a,))[0]["held_reason"]
    assert "no longer has its POS connected" in why
    # b archived nothing in three nights: released for the next store.
    d = _store(9)
    connected.add(d)
    _x(db, "UPDATE pos_backfill_slots SET admitted_at=datetime('now','-5 days') WHERE restaurant_id=?", (b,))
    assert onboarding.backfill_admission(d, "rpower", 80, 7)["admit"]
    assert "no day archived" in _q(db, "SELECT held_reason FROM pos_backfill_slots WHERE restaurant_id=?",
                                   (b,))[0]["held_reason"]


def test_a_slot_held_for_room_is_never_released_as_stale(db, monkeypatch):
    import onboarding
    import pos_archive
    monkeypatch.setattr(onboarding, "BACKFILL_MAX_STORES", 1)
    monkeypatch.setattr(pos_archive, "provider_for", lambda rid: ("rpower", object()))
    a, b = _store(1), _store(2)
    assert onboarding.backfill_admission(a, "rpower", 80, 7)["admit"]
    _x(db, "UPDATE pos_backfill_slots SET admitted_at=datetime('now','-9 days'), held_reason='no room'")
    assert onboarding.backfill_admission(b, "rpower", 80, 7)["reason"] == "waiting_for_slot"
    state = onboarding.backfill_state()
    assert state["used"] == 1 and state["cap"] == 1


def test_the_admission_counts_and_takes_a_slot_in_one_write():
    import onboarding
    src = inspect.getsource(onboarding.backfill_admission)
    assert src.index("begin_write") < src.index("SELECT COUNT(*) FROM pos_backfill_slots")


def test_the_history_step_is_done_at_sixty_days():
    import onboarding
    r = {"rpower_store_mid": "m", "rpower_last_synced": "x"}
    steps = {s["key"]: s for s in onboarding.pos_steps(r, {"archive_days": 60})}
    assert steps["pos_history"]["done"] and steps["pos_history"]["left"] == 29
    assert not {s["key"]: s for s in onboarding.pos_steps(r, {"archive_days": 59})}["pos_history"]["done"]


# ── R8-11: the owner confirms the labor setup ───────────────────────────────

def test_the_owner_confirms_the_labor_setup_and_the_auto_draft_is_released(db):
    import client_api
    import onboarding
    rid = models.create_restaurant(models.Restaurant(name="Setup Co", owner_email="o@setup.test"))
    models.update_restaurant(rid, {"module_labor": 1})
    onboarding.start_setup(rid)
    onboarding.suggest(rid, "week_start", {"week_start_day": 2, "label": "Wednesday"}, source="rpower")
    owner = {"id": 1, "restaurant_id": rid, "role": "owner", "username": "erik"}
    manager = {"id": 2, "restaurant_id": rid, "role": "manager", "username": "gm"}
    body, status = client_api._do_setup_confirmations(rid, None, manager)
    assert status == 403
    body, status = client_api._do_setup_confirmations(rid, None, owner)
    assert status == 200 and body["holds_auto_draft"] and "week_start" in body["pending"]
    body, status = client_api._do_setup_confirmations(rid, {"step": "week_start", "value": 2}, owner)
    assert status == 200 and body["applied"] == {"week_start_day": 2}
    assert models.get_restaurant(rid).week_start_day == 2
    assert onboarding.confirmations(rid)["week_start"]["confirmed_by"] == "owner:erik"
    for step in onboarding.OWNER_STEPS:
        client_api._do_setup_confirmations(rid, {"step": step}, owner)
    assert onboarding.labor_setup_pending(models.get_restaurant(rid)) == []
    assert client_api._do_setup_confirmations(rid, {"step": "dsr_calendar"}, owner)[1] == 400


def test_the_owner_setup_routes_have_web_and_mobile_twins():
    import client_api
    import mobile_api
    assert "_do_setup_confirmations" in inspect.getsource(mobile_api.mobile_account_setup)
    assert "_do_setup_confirmations" in inspect.getsource(mobile_api.mobile_account_setup_confirm)
    assert "_do_setup_confirmations" in inspect.getsource(client_api.account_setup_confirm)


# ── R8-22: partner attach needs the step-up ─────────────────────────────────

def test_partner_attach_carries_the_step_up():
    import onboarding_routes
    src = inspect.getsource(onboarding_routes.admin_partner_attach)
    assert "@recent_auth_required()" in src


# ── R8-13: the outside watchdog alerts on a reported failure ────────────────

def test_the_watchdog_keeps_a_failure_until_it_has_alerted():
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "workers", "watchdog", "worker.js"), encoding="utf-8") as f:
        js = f.read()
    assert 'env.WATCH.put("fail"' in js and 'env.WATCH.get("fail")' in js
    assert "FAILS_BEFORE_ALERT" in js and "failed && !failed.alerted" in js
    assert "if (ping && ping.fail)" not in js, "the failure no longer rides the heartbeat"


# ── R8-17: partitioned tables export partition by partition (live) ──────────

PG = (os.getenv("CAVNAR_TEST_PG_URL") or "").strip()
live = pytest.mark.skipif(not PG, reason="CAVNAR_TEST_PG_URL names no scratch Postgres")


@pytest.fixture
def scratch_pg():
    import uuid
    from urllib.parse import urlsplit, urlunsplit
    import psycopg
    import db_dialect
    name = f"g8_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    p = urlsplit(PG)
    url = urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))
    yield url
    db_dialect.close_idle(url)
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@live
def test_the_export_reads_each_partition_in_its_own_snapshot(scratch_pg, tmp_path):
    import contextlib
    import io
    from datetime import date
    import psycopg
    import pg_copy
    src = str(tmp_path / "source.db")
    with contextlib.redirect_stdout(io.StringIO()):
        models.init_db(src)
        models.ensure_columns(src)
    rid = models.create_restaurant(models.Restaurant(name="P", owner_email="p@x.com"), db_path=src)
    s = sqlite3.connect(src)
    for month in ("2026-08", "2026-09", "2026-10"):
        for i in range(4):
            s.execute("INSERT INTO ai_usage (restaurant_id, action, model, cost_usd, created_at) "
                      "VALUES (?, 't', 'm', 0.01, ?)", (rid, f"{month}-0{i + 1} 12:00:00"))
    s.commit()
    s.close()
    models.checkpoint(src)
    dst = psycopg.connect(scratch_pg, autocommit=False)
    try:
        assert pg_copy.copy_database(src, dst, today=date(2026, 10, 20))["verify"]["ok"]
        parts = pg_copy._pg_partitions(dst)
        dst.rollback()
    finally:
        dst.close()
    assert len(parts.get("ai_usage") or []) >= 3, parts.get("ai_usage")
    out = str(tmp_path / "export.db")
    rep = pg_copy.export_to_sqlite(out, url=scratch_pg)
    assert rep["verify"]["ok"] and rep["tables"]["ai_usage"]["rows"] == 12
    assert rep["tables"]["ai_usage"]["partitions"] == len(parts["ai_usage"])
    e = sqlite3.connect(out)
    try:
        assert e.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0] == 12
        assert e.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0] >= 1
    finally:
        e.close()
