"""Fix round D — the sweeps, the manual syncs and the windows.

#39 #137 #120 #162  the review fetch counts a restaurant whose fetch reached
                    no provider as failed, saves a prefix cursor, the manual
                    "Sync now" runs the same pass for one restaurant, and the
                    unused urgent-review query is gone.
#137                the weekly sweeps skip what is done this week.
#84                 the unbounded walks are bounded and resumable.
#81 #131            briefs from a due-queue, bounded, with missed windows
                    recorded; local_due records a closed window too.
#17                 the DSR sweep's counts, and a night missing past its
                    deadline.
#65                 the manual POS syncs go through pos.sync_restaurant, and
                    the Instagram refresh has one implementation.
#161                `python main.py` no longer starts a second scheduler.
"""
import functools
import inspect
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

import pytest

import models
import ops
import scheduler
import strategy_jobs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def db(db_path, monkeypatch):
    # A call that passes the module default (update_restaurant, get_all_
    # restaurants — their db_path defaults were bound at import) lands here
    # too, not in the throwaway default volume.
    orig, default = models.get_conn, models.DB_PATH
    monkeypatch.setattr(models, "get_conn",
                        lambda path=None, *a, **k: orig(db_path if path in (None, default) else path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    for name in ("get_restaurant", "save_reviews", "get_pending_analysis", "get_pending_drafts",
                 "update_last_fetched", "get_approved_examples"):
        monkeypatch.setattr(models, name, functools.partial(getattr(models, name), db_path=db_path))
    ops._claim_fallback.clear()
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


def _rid(db_path, name, **kw):
    return models.create_restaurant(models.Restaurant(name=name, owner_email=f"{name[:3].lower()}@x.test", **kw),
                                    db_path=db_path)


def _no_ai(monkeypatch, order=None):
    import analyser, drafter, notify
    order = [] if order is None else order
    monkeypatch.setattr(analyser, "analyse_review", lambda *a, **k: order.append("analyse"))
    monkeypatch.setattr(drafter, "draft_response", lambda *a, **k: None)
    monkeypatch.setattr(notify, "fire_review_alerts", lambda *a, **k: order.append("alert"))
    monkeypatch.setattr(scheduler, "auto_approve_five_stars", lambda *a, **k: 0)


# ── the review fetch ────────────────────────────────────────────────────────

def test_a_restaurant_whose_fetch_reached_no_provider_is_failed(db, monkeypatch):
    import fetcher
    ok_rid = _rid(db, "Fine Grill", reviews_live=1, google_place_id="p-ok")
    bad_rid = _rid(db, "Broken Bistro", reviews_live=1, google_place_id="p-bad")
    _no_ai(monkeypatch)

    def fetch(place_id, rid):
        if place_id == "p-bad":
            raise RuntimeError("REQUEST_DENIED")
        return []
    monkeypatch.setattr(fetcher, "fetch_google", fetch)
    out = scheduler.run_daily_fetch()
    assert (out["attempted"], out["ok"], out["failed"]) == (2, 1, 1), \
        "a night on which Google refused a restaurant read as a clean run"
    assert ops.run_outcome(out)[0] == ops.RUN_PARTIAL
    assert models.get_restaurant(bad_rid).last_fetched_at is None
    assert models.get_restaurant(ok_rid).last_fetched_at


def test_the_fetch_cursor_is_the_finished_prefix_not_the_success_count(db, monkeypatch):
    import fetcher
    ids = [_rid(db, f"R{i}", reviews_live=1, google_place_id=f"p{i}") for i in range(4)]
    _no_ai(monkeypatch)
    monkeypatch.setattr(fetcher, "fetch_google",
                        lambda pid, rid: (_ for _ in ()).throw(RuntimeError("x")) if rid == ids[1] else [])
    scheduler.run_daily_fetch()
    cur = _q(db, "SELECT value FROM job_cursors WHERE key='review_fetch_cursor'")[0]["value"]
    assert int(cur) == ids[-1], "the cursor landed short by the number of failures"


def test_sync_now_runs_the_same_pass_for_one_restaurant_and_analyses_before_alerting(db, monkeypatch):
    import fetcher
    from models import Review
    target = _rid(db, "Target Cafe", reviews_live=1, google_place_id="p-t")
    other = _rid(db, "Other Cafe", reviews_live=1, google_place_id="p-o")
    _x(db, "INSERT INTO job_cursors (key, value) VALUES ('review_fetch_cursor', ?)", (str(other),))
    order, fetched = [], []

    def fetch(place_id, rid):
        fetched.append(rid)
        return [Review(restaurant_id=rid, platform="google", external_id=f"x-{rid}", author="G", rating=5,
                       text="no roach problem here, lovely", review_date=date.today().isoformat())]
    monkeypatch.setattr(fetcher, "fetch_google", fetch)
    _no_ai(monkeypatch, order)
    out = scheduler.run_daily_fetch(restaurant_ids=[target])
    assert fetched == [target] and out["ok"] == 1
    assert order.index("analyse") < order.index("alert"), "alerts fired on an unanalysed batch"
    assert models.get_restaurant(target).last_fetched_at, "Sync now never recorded the sync"
    assert _q(db, "SELECT value FROM job_cursors WHERE key='review_fetch_cursor'")[0]["value"] == str(other)


def test_the_admin_fetch_route_runs_the_shared_pass_and_refuses_locally(db, monkeypatch):
    import auth, admin_routes
    from flask import Flask
    from admin_routes import admin_bp
    rid = _rid(db, "Route Cafe", reviews_live=1, google_place_id="p-r")
    real = models.get_conn
    monkeypatch.setattr(admin_routes, "get_restaurant", lambda r: models.get_restaurant(r))
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_bp)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": None, "is_admin": 1,
                                                            "username": "will", "email": "w@x.com", "role": "admin"})
    c = app.test_client()
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    assert c.post(f"/admin/fetch-reviews/{rid}").status_code == 409
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    started = {}
    monkeypatch.setattr(ops, "run_admin_task", lambda kind, r, name, fn, *a, **k: started.update(
        kind=kind, rid=r, fn=fn, kw=k) or ("job-1", False))
    d = c.post(f"/admin/fetch-reviews/{rid}").get_json()
    assert d["ok"] and d["job_id"] == "job-1"
    assert started["fn"] is scheduler.run_daily_fetch and started["kw"]["restaurant_ids"] == [rid]


def test_the_unused_urgent_query_is_gone():
    src = inspect.getsource(scheduler.run_daily_fetch)
    assert "urgency='high'" not in src and "urgent = conn.execute" not in src


# ── #137: the weekly sweeps ─────────────────────────────────────────────────

def test_a_weekly_pass_re_run_after_a_reclaim_skips_what_is_done(db, monkeypatch):
    import competitor, data_health
    ids = [_rid(db, f"Full {i}", google_place_id=f"pl{i}", service_tier="full") for i in range(3)]
    monkeypatch.setattr(models, "is_full_tier", lambda r: True)
    data_health.record_attempt(ids[0], "competitor", True)
    seen = []
    monkeypatch.setattr(competitor, "run_competitor_analysis", lambda rid: seen.append(rid) or {"ok": True})
    out = scheduler.run_weekly_competitor_analysis()
    assert seen == ids[1:] and out["skipped"] == 1 and out["ok"] == 2


def test_the_weekly_cursor_is_saved_as_each_restaurant_finishes(db, monkeypatch):
    import competitor
    ids = [_rid(db, f"Full {i}", google_place_id=f"pl{i}", service_tier="full") for i in range(3)]
    monkeypatch.setattr(models, "is_full_tier", lambda r: True)
    cursors = []

    def analyse(rid):
        cursors.append(_q(db, "SELECT value FROM job_cursors WHERE key='competitor_sweep_cursor'"))
        return {"ok": True}
    monkeypatch.setattr(competitor, "run_competitor_analysis", analyse)
    scheduler.run_weekly_competitor_analysis()
    assert cursors[1] and cursors[1][0]["value"] == str(ids[0]), "the cursor was written only at the end"


# ── #84: bounded walks ──────────────────────────────────────────────────────

def test_a_bounded_walk_stops_reports_and_resumes(db, monkeypatch):
    import loss_detection
    ids = [_rid(db, f"Loss {i}") for i in range(4)]
    monkeypatch.setattr(strategy_jobs, "LOSS_SYNC_MAX_SECONDS", 0)
    seen = []
    monkeypatch.setattr(loss_detection, "sync", lambda rid, **k: seen.append(rid) or {"ok": False})
    out = strategy_jobs.run_loss_sync(db_path=db)
    assert out["hit_bound"] is True and seen == [ids[0]]
    strategy_jobs.run_loss_sync(db_path=db)
    assert seen == [ids[0], ids[1]], "the next pass started from the top again"


def test_bounded_each_reports_its_bound(db, monkeypatch):
    for i in range(3):
        _rid(db, f"Eval {i}")
    out = strategy_jobs.run_outcome_rechecks(db_path=db)
    assert {"attempted", "ok", "failed", "skipped", "hit_bound"} <= set(out)


def test_the_diagnoses_and_token_refresh_are_bounded():
    for fn in (scheduler.run_review_diagnoses, scheduler.run_food_cost_diagnoses, scheduler.refresh_expiring_tokens):
        assert "resumable_sweep(" in inspect.getsource(fn), fn.__name__
    for fn in (strategy_jobs.run_weekly_plan, strategy_jobs.run_recipe_drafts, strategy_jobs.run_issue_scan,
               strategy_jobs.run_trusted_orders, strategy_jobs.run_loss_sync):
        assert "_BoundedWalk(" in inspect.getsource(fn), fn.__name__


# ── #81 / #131: briefs and windows ──────────────────────────────────────────

def test_briefs_come_from_a_due_queue_with_a_bound_and_a_cursor(db, monkeypatch):
    import morning_brief, time_utils
    ids = [_rid(db, f"Brief {i}", timezone="America/Chicago") for i in range(3)]
    monkeypatch.setattr(time_utils, "restaurant_now", lambda tz=None, naive=False: datetime(2026, 9, 29, 8, 0))
    sent = []
    monkeypatch.setattr(morning_brief, "deliver", lambda rid, **k: sent.append(rid) or {"sent": 1})
    out = morning_brief.run_due(db_path=db, max_seconds=0)
    assert out["hit_bound"] and sent == [ids[0]]
    morning_brief.run_due(db_path=db)
    assert sorted(sent) == ids, "the rest were reached the next tick"
    assert morning_brief.run_due(db_path=db)["attempted"] == 0, "claimed once a day"


def test_a_brief_window_that_closed_unsent_is_recorded(db, monkeypatch):
    import morning_brief, time_utils
    rid = _rid(db, "Late Cafe", timezone="America/Chicago")
    monkeypatch.setattr(time_utils, "restaurant_now", lambda tz=None, naive=False: datetime(2026, 9, 29, 15, 0))
    monkeypatch.setattr(morning_brief, "deliver", lambda *a, **k: pytest.fail("a brief at 3pm"))
    out = morning_brief.run_due(db_path=db)
    assert out["missed"] == 1
    assert _q(db, "SELECT job, restaurant_id, local_date FROM missed_windows") == [
        {"job": "morning_brief", "restaurant_id": rid, "local_date": "2026-09-29"}]
    morning_brief.run_due(db_path=db)
    assert len(_q(db, "SELECT 1 FROM missed_windows")) == 1


def test_local_due_records_a_window_that_closed_with_nothing_claimed(db):
    r = type("R", (), {"id": 7, "timezone": "America/Chicago"})()
    assert scheduler.local_due(r, 9, claim_key="labor_reminders", now_local=datetime(2026, 9, 29, 15, 0)) is False
    assert _q(db, "SELECT job, restaurant_id FROM missed_windows") == [{"job": "labor_reminders", "restaurant_id": 7}]
    # a restaurant served inside its window is not a miss
    assert scheduler.local_due(r, 9, claim_key="milestones", now_local=datetime(2026, 9, 29, 10, 0)) is True
    scheduler.local_due(r, 9, claim_key="milestones", now_local=datetime(2026, 9, 29, 15, 0))
    assert len(_q(db, "SELECT 1 FROM missed_windows")) == 1


def test_retention_left_the_hourly_alert_job():
    src = inspect.getsource(scheduler.run_daily_alert_checks)
    assert "purge_expired_reviews" not in src.split('"""', 2)[2] and "prune_operational_logs" not in src
    assert "purge_expired_reviews" in inspect.getsource(scheduler.run_nightly_retention)
    assert '_ops.run_job("prune_ledgers", run_nightly_retention)' in inspect.getsource(scheduler.scheduler_loop)


# ── #17: the DSR ────────────────────────────────────────────────────────────

def test_the_dsr_sweep_counts_failed_nights(monkeypatch, db):
    from dsr import pipeline
    import pos
    rid = _rid(db, "DSR Cafe")
    monkeypatch.setattr(pipeline, "_nights_for", lambda r, rows, now: [date(2026, 9, 28)])
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("rpower", object()))
    monkeypatch.setattr(pipeline, "run_night", lambda *a, **k: {"action": "failed"})
    out = pipeline.run_sweep(now_utc=datetime(2026, 9, 29, 12, 0), db_path=db)
    assert (out["attempted"], out["failed"]) == (1, 1) and ops.run_outcome(out)[0] == ops.RUN_FAILED


def test_a_night_with_no_report_past_its_deadline_is_missing(monkeypatch, db):
    from dsr import pipeline, store
    import pos
    rid = _rid(db, "Nightly Cafe", timezone="America/Chicago")
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("rpower", object()))
    now = datetime(2026, 9, 29, 14, 0)          # 9am Chicago: past a 4am deadline + 1h
    missing = pipeline.nights_missing(db_path=db, now_utc=now)
    assert [(m["restaurant_id"], m["business_date"], m["status"]) for m in missing] == [(rid, "2026-09-28", "missing")]
    rep = store.create_report(rid, "2026-09-28", db_path=db)
    store.set_stage(rep["id"] if isinstance(rep, dict) else rep, "final", db_path=db)
    assert pipeline.nights_missing(db_path=db, now_utc=now) == []


# ── #65: manual POS syncs and the Instagram refresh ─────────────────────────

def test_manual_pos_sync_goes_through_pos_sync_restaurant(db, monkeypatch):
    import pos
    rid = _rid(db, "Toast Cafe")
    calls = []
    monkeypatch.setattr(pos, "sync_restaurant", lambda r, trigger="nightly", attempt=0: calls.append((r, trigger))
                        or {"ok": True, "provider": "toast"})
    job_id, joined = scheduler.start_manual_pos_sync(rid, "will")
    for _ in range(100):
        state = ops.read_async_job(job_id)
        if state and state["status"] != "pending":
            break
        time.sleep(0.02)
    assert calls == [(rid, "manual")] and state["status"] == "done" and state["result"]["ok"] is True
    run = _q(db, "SELECT job, restaurant_id, ok FROM job_runs WHERE job='pos_sync_one'")[0]
    assert run == {"job": "pos_sync_one", "restaurant_id": rid, "ok": 1}


def test_the_sync_buttons_no_longer_call_sync_to_db_directly():
    import toast_routes, rpower_routes
    for fn in (toast_routes.sync_toast, toast_routes.client_sync_toast, rpower_routes.sync_rpower,
               rpower_routes.rpower_sync_client):
        src = inspect.getsource(fn)
        assert "start_manual_pos_sync" in src and "sync_to_db(" not in src, fn.__name__


def test_the_instagram_refresh_moves_the_expiry_only_with_a_new_token(db, monkeypatch):
    import requests
    rid = _rid(db, "IG Cafe")
    models.update_restaurant(rid, {"ig_token": "old", "ig_token_expires": "2026-10-01"}, db_path=db)
    monkeypatch.setenv("META_APP_ID", "app")
    monkeypatch.setenv("META_APP_SECRET", "secret")

    class R:
        def __init__(self, body, status=200):
            self._b, self.status_code, self.text = body, status, str(body)

        def json(self):
            return self._b
    monkeypatch.setattr(requests, "get", lambda *a, **k: R({}))
    out = scheduler.refresh_ig_token(models.get_restaurant(rid))
    assert out["ok"] is False and models.get_restaurant(rid).ig_token_expires == "2026-10-01"
    monkeypatch.setattr(requests, "get", lambda *a, **k: R({"access_token": "new"}))
    assert scheduler.refresh_ig_token(models.get_restaurant(rid))["ok"] is True
    assert models.get_restaurant(rid).ig_token == "new"


# ── #161: main.py ───────────────────────────────────────────────────────────

def test_python_main_py_no_longer_starts_a_scheduler(tmp_path):
    env = dict(os.environ, RAILWAY_VOLUME_MOUNT_PATH=str(tmp_path), RAILWAY_ENVIRONMENT="production")
    open(tmp_path / "reviews.db", "a").close()
    p = subprocess.run([sys.executable, "main.py"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 2 and "no longer starts" in p.stdout
    env.pop("RAILWAY_ENVIRONMENT")
    env.pop("ALLOW_LOCAL_SCHEDULER", None)
    p = subprocess.run([sys.executable, "main.py", "--legacy-scheduler"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 2 and "Refusing" in p.stdout
