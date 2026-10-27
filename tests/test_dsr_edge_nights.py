"""The nightly report's edge nights (audit #25 A02-06, A02-07, A02-08).

  * a close after midnight — a bar closing 2:00am on Friday: nothing runs at
    1:30am Saturday, Friday's night runs after its close, its deadline is
    4:00am Saturday;
  * the fall-back night (US clocks go back Sun 11/1/26 in America/Chicago):
    a 2:00am close on business date Saturday 10/31/26, and the ambiguous
    1:30am close, across the repeated hour;
  * a settledness check that FAILS is not a settled night (A02-06);
  * a dead run's claim is taken over by exactly one of two passes (A02-08).

The fake POS, the fake other blocks and the fake narrative are
test_dsr_pipeline's (`world`); Sales and Labor are the real blocks. Times
handed to the pipeline are naive UTC, as the scheduler's.
"""
import json
import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import dsr
import models
import ops
from dsr import pipeline, store
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant
from tests.test_dsr_pipeline import DAYS, db, world  # noqa: F401  (fixtures)

CHI = ZoneInfo("America/Chicago")


def utc(y, mo, d, h, mi=0, tz=CHI, fold=0):
    """Local wall clock in `tz` (fold=1: the second of a repeated hour) as
    naive UTC."""
    return datetime(y, mo, d, h, mi, tzinfo=tz, fold=fold).astimezone(timezone.utc).replace(tzinfo=None)


def _bar(db_path, late_day, close="2:00am", open_="4:00pm", name="Late Bar"):
    """11am-11pm every day, except `late_day`: `open_` to `close` the next
    morning."""
    rid = create_restaurant(Restaurant(name=name, owner_email="late@x.test", timezone="America/Chicago"),
                            db_path=db_path)
    opens = {d: "11:00am" for d in DAYS}
    closes = {d: "11:00pm" for d in DAYS}
    opens[late_day], closes[late_day] = open_, close
    update_restaurant(rid, {"open_times_json": json.dumps(opens), "close_times_json": json.dumps(closes)},
                      db_path=db_path)
    return get_restaurant(rid, db_path=db_path)


def _labor(db_path, rid, day):
    """The night's labor in the daily archive, so the Labor block is in."""
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales, "
                 "total_hours) VALUES (?,?,?,?,?,?,?)", (rid, day.isoformat(), day.strftime("%A"), 22.0, 440.0,
                                                          2000.0, 32.0))
    conn.commit()
    conn.close()


def _reports(rid, db_path):
    return {x["business_date"]: x for x in store.list_reports(rid, db_path=db_path)}


# ── a close after midnight ──────────────────────────────────────────────────

FRIDAY = date(2026, 10, 23)


def test_a_2am_friday_close_runs_after_its_close_and_never_at_1_30(db, world):
    r = _bar(db, "Friday")
    _labor(db, r.id, FRIDAY)
    assert pipeline.close_at(r, FRIDAY) == datetime(2026, 10, 24, 2, 0)
    assert pipeline.deadline_at(r, FRIDAY) == datetime(2026, 10, 24, 4, 0)
    # 1:30am Saturday: Friday is still trading; nothing has just closed.
    at_130 = utc(2026, 10, 24, 1, 30)
    assert pipeline.just_closed(r, pipeline.local_time(r, at_130)) is None
    assert pipeline.run_night(r, FRIDAY, pipeline.TRIGGER_SWEEP, now_utc=at_130, db_path=db)["action"] \
        == "before_close"
    pipeline.run_sweep(now_utc=at_130, db_path=db)
    assert FRIDAY.isoformat() not in _reports(r.id, db)
    # 2:40am Saturday: the sweep owns Friday's night, and it runs.
    at_240 = utc(2026, 10, 24, 2, 40)
    assert pipeline.just_closed(r, pipeline.local_time(r, at_240)) == FRIDAY
    pipeline.run_sweep(now_utc=at_240, db_path=db)
    rep = store.get_report(r.id, FRIDAY, db_path=db)
    assert rep and rep["status"] == "final" and rep["business_date"] == FRIDAY.isoformat()
    assert rep["stages"]["closed_by"] == "pos"
    # Saturday's own night (11pm close) is not touched at 2:40am.
    assert "2026-10-24" not in _reports(r.id, db)


def test_a_2am_close_still_waiting_on_the_pos_goes_out_by_its_4am_deadline(db, world):
    r = _bar(db, "Friday")
    world["closed"] = False                     # the POS never records its Close Day
    world["checks"] = True                      # ...but holds the night's checks
    out = pipeline.run_night(r, FRIDAY, pipeline.TRIGGER_SWEEP, now_utc=utc(2026, 10, 24, 2, 40), db_path=db)
    assert out["action"] == "awaiting_close"
    nxt = datetime.fromisoformat(out["next_attempt_at"])
    assert nxt <= utc(2026, 10, 24, 4, 0), "never past the 4am deadline"
    out = pipeline.run_night(r, FRIDAY, pipeline.TRIGGER_SWEEP, now_utc=utc(2026, 10, 24, 4, 5), db_path=db)
    rep = store.get_report(r.id, FRIDAY, db_path=db)
    assert out["action"] == "final" and rep["stages"]["closed_by"] == pipeline.CLOSED_BY_CHECKS


# ── the fall-back night, 10/31 → 11/1/26 ────────────────────────────────────

SATURDAY = date(2026, 10, 31)


def test_the_fall_back_night_runs_after_its_2am_close_and_its_deadline_is_4am_cst(db, world):
    r = _bar(db, "Saturday")
    _labor(db, r.id, SATURDAY)
    # 2:00am on 11/1 happens once, in CST (the clocks go back at 2:00 CDT).
    assert pipeline.to_utc(r, pipeline.close_at(r, SATURDAY)) == datetime(2026, 11, 1, 8, 0)
    assert pipeline.deadline_at(r, SATURDAY) == datetime(2026, 11, 1, 4, 0)
    assert pipeline.to_utc(r, pipeline.deadline_at(r, SATURDAY)) == datetime(2026, 11, 1, 10, 0)
    # 1:30am happens twice — CDT (06:30 UTC) and CST (07:30 UTC): neither runs Saturday's night.
    for at in (utc(2026, 11, 1, 1, 30), utc(2026, 11, 1, 1, 30, fold=1), utc(2026, 11, 1, 1, 59, fold=1)):
        assert pipeline.run_night(r, SATURDAY, pipeline.TRIGGER_SWEEP, now_utc=at, db_path=db)["action"] \
            == "before_close", at
        pipeline.run_sweep(now_utc=at, db_path=db)
        assert SATURDAY.isoformat() not in _reports(r.id, db), at
    assert utc(2026, 11, 1, 1, 30, fold=1) - utc(2026, 11, 1, 1, 30) == timedelta(hours=1)
    # 2:40am CST: it runs, and is Saturday's.
    pipeline.run_sweep(now_utc=datetime(2026, 11, 1, 8, 40), db_path=db)
    rep = store.get_report(r.id, SATURDAY, db_path=db)
    assert rep and rep["status"] == "final" and rep["business_date"] == "2026-10-31"
    stamps = [rep["stages"][k] for k in ("scheduled", "collecting", "writing", "final")]
    assert stamps == sorted(stamps)


def test_the_fall_back_night_waiting_on_the_pos_is_bounded_by_4am_cst(db, world):
    r = _bar(db, "Saturday")
    world["closed"], world["checks"] = False, True
    out = pipeline.run_night(r, SATURDAY, pipeline.TRIGGER_SWEEP, now_utc=datetime(2026, 11, 1, 8, 40), db_path=db)
    assert out["action"] == "awaiting_close"
    assert datetime.fromisoformat(out["next_attempt_at"]) <= datetime(2026, 11, 1, 10, 0)
    # 3:55am CST is still before the deadline; 4:05am CST is past it.
    assert pipeline.run_night(r, SATURDAY, pipeline.TRIGGER_SWEEP, now_utc=datetime(2026, 11, 1, 9, 55),
                              db_path=db)["action"] in ("awaiting_close", "waiting")
    out = pipeline.run_night(r, SATURDAY, pipeline.TRIGGER_SWEEP, now_utc=datetime(2026, 11, 1, 10, 5), db_path=db)
    assert out["action"] == "final"
    assert store.get_report(r.id, SATURDAY, db_path=db)["stages"]["closed_by"] == pipeline.CLOSED_BY_CHECKS


def test_an_ambiguous_1_30am_close_is_read_at_its_first_occurrence_and_the_pos_decides(db, world):
    """A 1:30am close on the fall-back night is ambiguous; the stated close
    is its FIRST occurrence (to_utc, fold=0 — 06:30 UTC), so the close poll
    can start an hour early. That is safe: the POS's own Close Day decides
    when the night is collected (a closeday answer of "open" waits), and a
    POS with no close-day record waits close + grace on the wall clock,
    which across the repeated hour is the LATER 2:00am (08:00 UTC)."""
    r = _bar(db, "Saturday", close="1:30am")
    assert pipeline.to_utc(r, pipeline.close_at(r, SATURDAY)) == datetime(2026, 11, 1, 6, 30)
    assert pipeline.deadline_at(r, SATURDAY) == datetime(2026, 11, 1, 4, 0)
    assert pipeline.run_night(r, SATURDAY, pipeline.TRIGGER_SWEEP, now_utc=datetime(2026, 11, 1, 6, 20),
                              db_path=db)["action"] == "before_close"
    # The POS has not closed the day at 1:40am CDT: the night waits for it.
    world["closed"] = False
    out = pipeline.run_night(r, SATURDAY, pipeline.TRIGGER_SWEEP, now_utc=datetime(2026, 11, 1, 6, 40), db_path=db)
    assert out["action"] == "awaiting_close"
    # A POS with no close-day record: closed at close + 30 minutes of WALL
    # clock — 2:00am, which on this night is the CST one.
    world["closeday"] = False
    for at, closed in ((datetime(2026, 11, 1, 6, 59), False), (datetime(2026, 11, 1, 7, 10), False),
                       (datetime(2026, 11, 1, 8, 0), True)):
        assert bool(pipeline.day_closed(r, SATURDAY, at, pipeline.TRIGGER_SWEEP)) is closed, at


# ── A02-06: a failed settledness check is not a settled night ──────────────

def test_a_failed_archive_check_keeps_sales_awaiting_until_the_deadline(db, world, monkeypatch):
    import pos_archive
    from dsr import block_sales

    def broken(*a, **k):
        raise RuntimeError("archive read failed")
    monkeypatch.setattr(pos_archive, "night_trusted", broken)
    r = _bar(db, "Friday", close="11:00pm", open_="11:00am")
    day = date(2026, 9, 22)
    ctx = dsr.Context(r, day, db_path=db, now_utc=datetime(2026, 9, 23, 4, 10), trigger=pipeline.TRIGGER_SWEEP)
    ctx.day_closed = "pos"
    blk = block_sales.collect(ctx)
    assert blk["status"] == dsr.AWAITING
    assert blk["reason"] == "The POS is still receiving tonight's checks"
    assert blk["detail"]["why"] == "archive check failed" and blk["detail"]["waiting_for"] == "tickets_settling"
    # Through the pipeline: retried before the deadline, never FINAL on it…
    out = pipeline.run_night(r, day, pipeline.TRIGGER_SWEEP, now_utc=datetime(2026, 9, 23, 4, 10), db_path=db)
    assert out["action"] == "retry" and "sales" in out["awaiting"]
    assert store.get_report(r.id, day, db_path=db)["status"] not in ("final", "provisional")
    # …and at the 4am deadline it goes out provisional, not final.
    out = pipeline.run_night(r, day, pipeline.TRIGGER_SWEEP, now_utc=datetime(2026, 9, 23, 9, 5), db_path=db)
    assert out["action"] == "provisional" and out["required_missing"] == ["sales"]
    rows = models.get_conn(db).execute("SELECT error FROM job_failures WHERE job='dsr_sales'").fetchall()
    assert any("archive read failed" in (x[0] or "") for x in rows), "the failure is captured, never silent"


# ── A02-08: one taker for a dead run's claim ────────────────────────────────

def test_two_passes_on_a_stale_claim_take_it_over_exactly_once(db, world, monkeypatch):
    r = _bar(db, "Friday", close="11:00pm", open_="11:00am")
    day = date(2026, 9, 22)
    ops.init_ops(db)
    key = f"{pipeline.CLAIM_JOB}:{pipeline._period(r.id, day, 1)}"
    conn = models.get_conn(db)
    # Stamped by the database's own clock, well past the dead-run age.
    conn.execute("INSERT INTO job_period_claims (job_key, claimed_at) VALUES (?, datetime('now', ?))",
                 (key, f"-{pipeline.STALE_RUN_MINUTES + 30} minutes"))
    conn.commit()
    old = conn.execute("SELECT claimed_at FROM job_period_claims WHERE job_key=?", (key,)).fetchone()[0]
    conn.close()
    # Both passes reach the takeover together: each has read the stale stamp.
    gate = threading.Barrier(2, timeout=20)
    real = store.get_report

    def both_here(*a, **k):
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            pass
        return real(*a, **k)
    monkeypatch.setattr(store, "get_report", both_here)
    results, errors = [], []

    def take():
        try:
            results.append(pipeline._claim(r.id, day, 1, db))
        except Exception as e:                       # surfaced below, never swallowed
            errors.append(e)
    threads = [threading.Thread(target=take) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert sorted(results) == [False, True], "exactly one pass takes a dead run over"
    stamp = models.get_conn(db).execute("SELECT claimed_at FROM job_period_claims WHERE job_key=?",
                                        (key,)).fetchone()[0]
    assert stamp != old
