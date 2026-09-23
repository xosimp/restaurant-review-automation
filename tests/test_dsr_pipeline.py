"""dsr.pipeline — the nightly stage machine and the sweep that drives it.

The restaurant here is in Chicago, open 11am–11pm every day. Business date
Tuesday 9/22/26 closes at 11pm CDT = 04:00 UTC on 9/23; its provisional
deadline is 4am CDT = 09:00 UTC. Times below are UTC, as the scheduler's.

The POS is a fake (pos.connected_provider / fetch_day_closed /
fetch_day_sales), the Food, Reviews, Marketing, Intel and Closeout blocks and
the narrative are fakes in sys.modules — so this file tests the pipeline and
never depends on another engineer's collectors — and Sales and Labor are the
real blocks.
"""
import json
import sys
import time
import types
from datetime import date, datetime, timedelta

import pytest

import models
import ops
import pos
import dsr
from dsr import pipeline, store, block_labor
from models import Restaurant, create_restaurant, update_restaurant, get_restaurant

DAY = date(2026, 9, 22)
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
OTHERS = ("food", "reviews", "marketing", "intel", "closeout")


def U(hour, minute=0, day=23):
    return datetime(2026, 9, day, hour, minute)


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    return db_path


def _sales_day(net=2000.0):
    return {"gross": net + 100, "net": net, "transactions": 80, "guests": 120, "discounts": 60.0, "comps": 40.0,
            "voids": 0.0, "refunds": 0.0, "tax": 160.0, "by_department": {"Food": net},
            "by_hour": {"12": net * 0.4, "19": net * 0.6}, "items": [], "net_deductions": ["discounts", "comps"],
            "source_checks": {}}


def _module(name, fn):
    mod = types.ModuleType(name)
    mod.collect = fn
    return mod


@pytest.fixture
def world(db, monkeypatch):
    """The fake POS and the fake other blocks, all steerable from `state`."""
    state = {"closed": True, "closeday": True, "sales_calls": 0, "narratives": [], "no_pos": set()}
    monkeypatch.setattr(pos, "connected_provider",
                        lambda rid: (None, None) if rid in state["no_pos"] else ("fakepos", object()))

    def closed(rid, day):
        if not state["closeday"]:
            raise pos.POSCapabilityError("fakepos keeps no close-day record")
        return state["closed"], "fakepos"
    monkeypatch.setattr(pos, "fetch_day_closed", closed)

    def sales(rid, day):
        state["sales_calls"] += 1
        return _sales_day(), "fakepos"
    monkeypatch.setattr(pos, "fetch_day_sales", sales)

    for name in OTHERS:
        def collect(ctx, _name=name):
            hook = state.get(f"hook_{_name}")
            if hook:
                return hook(ctx)
            return dsr.block(dsr.READY, source="fake", metrics={"n": 1})
        monkeypatch.setitem(sys.modules, f"dsr.block_{name}", _module(f"dsr.block_{name}", collect))

    narrative = types.ModuleType("dsr.narrative")

    def write(ctx, facts):
        state["narratives"].append(facts)
        return {"ok": True, "narrative": {"executive_summary": {"text": "A good night.", "facts": ["sales.net"]}},
                "reason": None}
    narrative.write = write
    monkeypatch.setitem(sys.modules, "dsr.narrative", narrative)
    return state


def _restaurant(db, name="Pipe Co", **fields):
    rid = create_restaurant(Restaurant(name=name, owner_email="p@x.com", timezone="America/Chicago"), db_path=db)
    update_restaurant(rid, {"open_times_json": json.dumps({d: "11:00am" for d in DAYS}),
                            "close_times_json": json.dumps({d: "11:00pm" for d in DAYS}), **fields}, db_path=db)
    return get_restaurant(rid, db_path=db)


def _labor_in(db, rid, day=DAY):
    conn = models.get_conn(db)
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales, "
                 "total_hours) VALUES (?,?,?,?,?,?,?)", (rid, day.isoformat(), "Tuesday", 22.0, 440.0, 2000.0, 32.0))
    conn.commit()
    conn.close()


def _run(r, now, db, trigger=pipeline.TRIGGER_SWEEP, **kw):
    return pipeline.run_night(r, DAY, trigger, now_utc=now, db_path=db, **kw)


def _stages_in_order(report):
    s = report["stages"]
    return [s[k] for k in ("scheduled", "collecting")] + [s["blocks"][b] for b in dsr.BLOCKS] + \
        [s["writing"], s["final"]]


# ── the stage machine ───────────────────────────────────────────────────────

def test_stages_run_in_order_and_each_block_is_saved_as_it_finishes(db, world, monkeypatch):
    r = _restaurant(db)
    _labor_in(db, r.id)
    tick = {"n": 0}

    def clock():
        tick["n"] += 1
        return (datetime(2026, 9, 23, 4, 10) + timedelta(seconds=tick["n"])).strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr(store, "_now", clock)
    seen = {}

    def food(ctx):
        # Mid-collection: the earlier blocks are already on the report, the
        # summary is not, and the later block can read them from ctx.
        rep = store.get_report(ctx.restaurant_id, ctx.business_date, db_path=db)
        seen.update(status=rep["status"], saved=sorted(rep["facts"]["blocks"]),
                    writing="writing" in rep["stages"], ctx=sorted(ctx.blocks))
        return dsr.block(dsr.READY, source="fake")
    world["hook_food"] = food

    out = _run(r, U(4, 10), db)
    assert out["action"] == "final"
    rep = store.get_report(r.id, DAY, db_path=db)
    assert rep["status"] == "final" and not rep["provisional"] and rep["finalized_at"]
    order = _stages_in_order(rep)
    assert order == sorted(order) and len(set(order)) == len(order)
    assert seen == {"status": "collecting", "saved": ["labor", "sales"], "writing": False, "ctx": ["labor", "sales"]}
    assert rep["stages"]["closed_by"] == "pos"
    assert rep["stages"]["narrative"] == {"status": "written", "reason": None}
    assert rep["narrative"]["executive_summary"]["text"] == "A good night."
    assert rep["facts"]["fiscal"]["week_start"] and rep["facts"]["missing"] == []


def test_stage_times_are_the_real_clock_not_the_runs_now(db, world):
    r = _restaurant(db)
    _labor_in(db, r.id)
    _run(r, U(4, 10), db)
    rep = store.get_report(r.id, DAY, db_path=db)
    for stamp in _stages_in_order(rep):
        at = datetime.fromisoformat(stamp.replace(" ", "T"))
        assert abs((datetime.utcnow() - at).total_seconds()) < 120


def test_nothing_is_generated_before_close(db, world):
    r = _restaurant(db)
    assert _run(r, U(2, 0), db)["action"] == "before_close"         # 9pm CDT
    pipeline.run_sweep(now_utc=U(2, 0), db_path=db)
    assert store.get_report(r.id, DAY, db_path=db) is None
    # The sweep's night at 9pm is the one that last CLOSED — Monday's.
    assert [x["business_date"] for x in store.list_reports(r.id, db_path=db)] == ["2026-09-21"]


def test_a_closed_weekday_has_no_report(db, world):
    r = _restaurant(db, close_times_json=json.dumps({d: "11:00pm" for d in DAYS if d != "Tuesday"}),
                    open_times_json=json.dumps({d: "11:00am" for d in DAYS if d != "Tuesday"}))
    assert _run(r, U(6, 0), db)["action"] == "closed_day"
    assert store.get_report(r.id, DAY, db_path=db) is None


def test_the_pos_close_is_polled_every_ten_minutes(db, world):
    r = _restaurant(db)
    world["closed"] = False
    out = _run(r, U(4, 5), db)
    assert (out["action"], out["next_attempt_at"]) == ("awaiting_close", "2026-09-23 04:15:00")
    rep = store.get_report(r.id, DAY, db_path=db)
    assert rep["status"] == "awaiting_close" and rep["attempts"] == 0 and rep["facts"]["blocks"] == {}
    assert _run(r, U(4, 10), db)["action"] == "waiting"
    assert _run(r, U(4, 15), db)["next_attempt_at"] == "2026-09-23 04:25:00"
    world["closed"] = True
    _labor_in(db, r.id)
    assert _run(r, U(4, 25), db)["action"] == "final"
    assert store.get_report(r.id, DAY, db_path=db)["stages"]["closed_by"] == "pos"


def test_a_pos_with_no_close_record_is_closed_a_grace_period_after_close(db, world):
    r = _restaurant(db)
    _labor_in(db, r.id)
    world["closeday"] = False
    assert _run(r, U(4, 10), db)["action"] == "awaiting_close"      # 11:10pm, inside the grace
    out = _run(r, U(4, 31), db)
    assert out["action"] == "final"
    assert store.get_report(r.id, DAY, db_path=db)["stages"]["closed_by"] == "close_time"


def test_awaiting_blocks_are_retried_on_backoff_and_only_they_are_recollected(db, world):
    r = _restaurant(db)                  # no labor on file: the sync hasn't run
    expected = [(U(4, 10), "2026-09-23 04:20:00", 1), (U(4, 20), "2026-09-23 04:40:00", 2),
                (U(4, 40), "2026-09-23 05:20:00", 3), (U(5, 20), "2026-09-23 06:00:00", 4)]
    for now, nxt, attempts in expected:
        out = _run(r, now, db)
        assert (out["action"], out["awaiting"], out["next_attempt_at"]) == ("retry", ["labor"], nxt), now
        assert store.get_report(r.id, DAY, db_path=db)["attempts"] == attempts
    assert _run(r, U(5, 30), db)["action"] == "waiting"
    assert world["sales_calls"] == 1                   # sales was ready the first time
    assert world["narratives"] == []                   # no summary until the facts are in


def test_the_deadline_sends_it_out_provisional_with_the_reasons(db, world):
    r = _restaurant(db)
    out = _run(r, U(9, 5), db)                         # 4:05am CDT
    assert out["action"] == "provisional" and out["awaiting"] == ["labor"]
    rep = store.get_report(r.id, DAY, db_path=db)
    assert rep["status"] == "provisional" and rep["provisional"] is True
    assert rep["facts"]["missing"] == [block_labor.REASON_SYNC_PENDING]
    # The summary is written over what there is, and is told what is missing.
    assert world["narratives"][0]["missing"] == [block_labor.REASON_SYNC_PENDING]
    assert rep["next_attempt_at"] == "2026-09-23 10:05:00"


def test_late_data_makes_a_new_version_and_the_old_one_is_never_edited(db, world):
    r = _restaurant(db)
    _run(r, U(9, 5), db)
    v1 = store.get_report(r.id, DAY, db_path=db)
    assert _run(r, U(10, 5), db)["action"] == "still_awaiting"
    assert store.get_report(r.id, DAY, db_path=db)["version"] == 1
    _labor_in(db, r.id)                               # the 3am sync landed
    out = _run(r, U(11, 5), db)
    assert out["action"] == "final" and out["version"] == 2
    old, new = store.get_report(r.id, DAY, version=1, db_path=db), store.get_report(r.id, DAY, db_path=db)
    assert old["status"] == "provisional" and old["facts"] == v1["facts"] and old["next_attempt_at"] is None
    assert new["trigger"] == pipeline.TRIGGER_LATE and new["stages"]["supersedes"] == 1
    assert new["facts"]["blocks"]["labor"]["status"] == dsr.READY and new["facts"]["missing"] == []
    # Sales was carried over with the time it was really collected.
    assert new["stages"]["blocks"]["sales"] == v1["stages"]["blocks"]["sales"]
    assert world["sales_calls"] == 1
    assert store.metric_series(r.id, "labor.cost", DAY, DAY, db_path=db) == [(DAY.isoformat(), 440.0)]
    assert [v["version"] for v in store.versions(r.id, DAY, db_path=db)] == [1, 2]


def test_a_provisional_night_stops_being_checked_after_the_late_window(db, world):
    r = _restaurant(db)
    _run(r, U(9, 5), db)
    out = _run(r, U(10, 0, day=25) + timedelta(hours=1), db)     # past deadline + 48h
    assert out["action"] == "expired"
    assert store.get_report(r.id, DAY, db_path=db)["next_attempt_at"] is None


# ── never twice ─────────────────────────────────────────────────────────────

def test_a_final_night_is_never_run_twice_unless_forced(db, world):
    r = _restaurant(db)
    _labor_in(db, r.id)
    assert _run(r, U(4, 10), db)["action"] == "final"
    assert _run(r, U(4, 30), db)["action"] == "none"
    assert _run(r, U(4, 30), db, trigger=pipeline.TRIGGER_MANUAL)["action"] == "none"
    pipeline.run_sweep(now_utc=U(4, 40), db_path=db)
    assert [v["version"] for v in store.versions(r.id, DAY, db_path=db)] == [1]
    assert world["sales_calls"] == 1 and len(world["narratives"]) == 1
    forced = _run(r, U(5, 0), db, force=True)
    assert (forced["action"], forced["version"]) == ("final", 2)


def test_a_held_claim_is_someone_else_running_it(db, world):
    r = _restaurant(db)
    _labor_in(db, r.id)
    assert ops.claim_period("dsr", f"{r.id}:{DAY}:v1")
    assert _run(r, U(4, 10), db)["action"] == "in_progress"
    assert store.get_report(r.id, DAY, db_path=db) is None
    # A claim a deploy killed: held long past STALE_RUN_MINUTES, no progress.
    conn = models.get_conn(db)
    conn.execute("UPDATE job_period_claims SET claimed_at=datetime('now','-2 hours') WHERE job_key=?",
                 (f"dsr:{r.id}:{DAY}:v1",))
    conn.commit()
    conn.close()
    assert _run(r, U(4, 20), db)["action"] == "final"


def test_a_waiting_night_gives_its_claim_back_and_a_finished_one_keeps_it(db, world):
    r = _restaurant(db)
    world["closed"] = False
    _run(r, U(4, 5), db)
    assert not ops.period_claimed("dsr", f"{r.id}:{DAY}:v1")
    world["closed"] = True
    _labor_in(db, r.id)
    _run(r, U(4, 15), db)
    assert ops.period_claimed("dsr", f"{r.id}:{DAY}:v1")


# ── bounded failure ─────────────────────────────────────────────────────────

def _failures(db, job):
    conn = models.get_conn(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM job_failures WHERE job=?", (job,)).fetchone()[0]
    finally:
        conn.close()


def test_a_crashing_collector_is_retried_then_marked_unavailable(db, world):
    r = _restaurant(db)
    _labor_in(db, r.id)

    def broken(ctx):
        raise KeyError("food exploded")
    world["hook_food"] = broken
    for now in (U(4, 10), U(4, 20), U(4, 40)):
        assert _run(r, now, db)["action"] == "retry"
    out = _run(r, U(5, 20), db)
    assert out["action"] == "final"
    rep = store.get_report(r.id, DAY, db_path=db)
    assert rep["facts"]["blocks"]["food"]["status"] == dsr.UNAVAILABLE
    assert rep["facts"]["missing"] == [dsr.MISSING_TEXT["food"]]
    assert rep["stages"]["crashes"] == {"food": 4}
    assert _failures(db, "dsr_food") == 4


def test_a_pipeline_that_keeps_erroring_is_failed_after_bounded_retries(db, world, monkeypatch):
    r = _restaurant(db)

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(pipeline, "_save_fiscal", boom)
    expected = [(U(4, 10), "2026-09-23 04:20:00"), (U(4, 20), "2026-09-23 04:40:00"),
                (U(4, 40), "2026-09-23 05:20:00")]
    for now, nxt in expected:
        out = _run(r, now, db)
        assert (out["action"], out["next_attempt_at"]) == ("error_retry", nxt)
    out = _run(r, U(5, 20), db)
    assert out["action"] == "failed"
    rep = store.get_report(r.id, DAY, db_path=db)
    assert rep["status"] == "failed" and "disk full" in rep["error"]
    assert _failures(db, "dsr") == 4                   # each one reached the admin console
    assert _run(r, U(6, 0), db)["action"] == "none"    # the sweep leaves a failed night alone


def test_the_narrative_is_skipped_not_invented_when_sales_are_not_in(db, world, monkeypatch):
    r = _restaurant(db)
    _labor_in(db, r.id)
    monkeypatch.setattr(pos, "fetch_day_sales", lambda rid, day: (_ for _ in ()).throw(TimeoutError("slow")))
    out = _run(r, U(9, 5), db)
    rep = store.get_report(r.id, DAY, db_path=db)
    assert out["action"] == "provisional" and rep["narrative"] is None
    assert rep["stages"]["narrative"] == {"status": "skipped", "reason": pipeline.NO_SUMMARY_SALES_PENDING}
    assert world["narratives"] == []


def test_a_block_not_built_yet_is_not_available_yet(db, world, monkeypatch):
    r = _restaurant(db)
    _labor_in(db, r.id)
    monkeypatch.delitem(sys.modules, "dsr.block_marketing")
    monkeypatch.delitem(sys.modules, "dsr.narrative")
    real = pipeline._import
    monkeypatch.setattr(pipeline, "_import", lambda name: None if name in ("dsr.block_marketing", "dsr.narrative")
                        else real(name))
    assert _run(r, U(4, 10), db)["action"] == "final"
    rep = store.get_report(r.id, DAY, db_path=db)
    assert rep["facts"]["blocks"]["marketing"]["reason"] == pipeline.NOT_AVAILABLE_YET
    assert rep["stages"]["narrative"] == {"status": "skipped", "reason": pipeline.NOT_AVAILABLE_YET}


# ── Close day ───────────────────────────────────────────────────────────────

def test_close_day_starts_the_night_now(db, world, monkeypatch):
    r = _restaurant(db)
    _labor_in(db, r.id)
    world["closed"] = False                       # the POS hasn't closed; the manager says it's done
    monkeypatch.setattr(pipeline, "_spawn", lambda fn: fn())
    monkeypatch.setattr(pipeline, "datetime", _FrozenDatetime)
    out = pipeline.start_manual(r, DAY, db_path=db)
    assert out["started"] is True
    rep = store.get_report(r.id, DAY, db_path=db)
    assert rep["status"] == "final" and rep["trigger"] == "manual" and rep["stages"]["closed_by"] == "manual"
    assert pipeline.start_manual(r, DAY, db_path=db)["started"] is False


class _FrozenDatetime(datetime):
    """9:30pm CDT on the night — before close, which only Close day may skip."""
    @classmethod
    def utcnow(cls):
        return datetime(2026, 9, 23, 2, 30)


# ── the sweep ───────────────────────────────────────────────────────────────

def test_the_sweep_runs_each_restaurant_past_its_own_close(db, world):
    chicago = _restaurant(db, "Chicago")
    la = _restaurant(db, "LA", timezone="America/Los_Angeles")        # closes 06:00 UTC
    off = _restaurant(db, "Off", dsr_enabled=0)
    nopos = _restaurant(db, "No POS")
    world["no_pos"].add(nopos.id)
    for r in (chicago, la, off, nopos):
        _labor_in(db, r.id)
    pipeline.run_sweep(now_utc=U(4, 10), db_path=db)
    assert store.get_report(chicago.id, DAY, db_path=db)["status"] == "final"
    # 9:10pm in LA: Tuesday hasn't closed there. The night that most
    # recently closed is Monday's, which had no report — so it is caught up
    # (past its deadline, labor for it not on file: provisional, labelled).
    assert store.get_report(la.id, DAY, db_path=db) is None
    assert store.get_report(la.id, DAY - timedelta(days=1), db_path=db)["status"] == "provisional"
    for r in (off, nopos):
        assert store.list_reports(r.id, db_path=db) == [], r.name
    pipeline.run_sweep(now_utc=U(6, 10), db_path=db)
    assert store.get_report(la.id, DAY, db_path=db)["status"] == "final"


def test_the_sweep_picks_up_retries_and_late_data_when_they_come_due(db, world):
    r = _restaurant(db)
    pipeline.run_sweep(now_utc=U(9, 5), db_path=db)           # deadline: provisional
    assert store.get_report(r.id, DAY, db_path=db)["status"] == "provisional"
    _labor_in(db, r.id)
    assert pipeline.run_sweep(now_utc=U(9, 30), db_path=db)["restaurants"] == 0   # not due yet
    pipeline.run_sweep(now_utc=U(10, 5), db_path=db)
    assert store.get_report(r.id, DAY, db_path=db)["version"] == 2


def test_the_sweep_is_bounded_and_resumes_after_its_cursor(db, world, monkeypatch):
    rs = [_restaurant(db, f"R{i}") for i in range(3)]
    ran = []

    def slow_night(r, day, trigger, now_utc=None, db_path=None, force=False):
        ran.append(r.id)
        time.sleep(0.05)
        return {"ok": True, "action": "final"}
    monkeypatch.setattr(pipeline, "run_night", slow_night)
    monkeypatch.setattr(pipeline, "SWEEP_MAX_SECONDS", 0.01)
    first = pipeline.run_sweep(now_utc=U(4, 10), db_path=db)
    assert (first["done"], first["hit_bound"]) == (1, True) and ran == [rs[0].id]
    assert pipeline._read_cursor(db) == rs[0].id
    pipeline.run_sweep(now_utc=U(4, 10), db_path=db)
    assert ran[1] == rs[1].id                     # picked up where it stopped, not at the top
    monkeypatch.setattr(pipeline, "SWEEP_MAX_SECONDS", 60)
    ran.clear()
    pipeline.run_sweep(now_utc=U(4, 10), db_path=db)
    assert ran == [rs[2].id, rs[0].id, rs[1].id]  # a full pass, still starting after the cursor


def test_the_scheduler_runs_the_sweep_every_tick_claimed_per_ten_minutes():
    import inspect
    import re
    import scheduler
    import admin_ops
    src = inspect.getsource(scheduler.scheduler_loop)
    assert re.search(r'claim_period\("dsr_sweep", f"\{today\}-\{now\.hour\}-\{now\.minute // 10\}"\)', src)
    assert 'run_job("dsr_sweep", run_sweep)' in src
    assert admin_ops.RUNNABLE_JOBS["dsr_sweep"]["target"] == ("dsr.pipeline", "run_sweep")
