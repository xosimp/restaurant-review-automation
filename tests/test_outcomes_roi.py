"""The outcome engine after the Recommendation Outcome & ROI audit.

Each test names the audit item it pins (#1 … #49). The engine is
outcomes.py + metrics.py + value_delivered.py: what a tracker measures, what
it is compared against, how strongly a move is tied to the change, whether it
still holds at 90 days, and what it has been worth — net of what got worse,
one result per family of numbers, summed over days actually measured.

No model, network, email, SMS or push is reached. Holidays are pinned to none
unless a test sets them, so a grade never depends on the calendar the suite
happens to run on.
"""
import json
from datetime import date, timedelta

import pytest

import metrics
import models
import outcomes
import value_delivered
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    # Module scope on purpose: outcomes and metrics resolve get_conn at call
    # time, but patching them too keeps a stray bound copy honest.
    for mod in (models, outcomes, metrics):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(outcomes, "_holidays_between", lambda s, e: {})


TODAY = date.today()


def _rid(db_path, **kw):
    for m in ("module_labor", "module_inventory", "module_reviews", "module_marketing"):
        kw.setdefault(m, 1)
    return create_restaurant(Restaurant(name=kw.pop("name", "ROI Co"), owner_email="roi@x.test", **kw),
                             db_path=db_path)


def _days(db_path, rid, start, n, labor=300.0, sales=1000.0, skip=()):
    """labor_daily_history for n days from start. labor / sales may be a
    function of the date."""
    conn = get_conn(db_path)
    for i in range(n):
        d = start + timedelta(days=i)
        if d in skip:
            continue
        s = sales(d) if callable(sales) else sales
        lab = labor(d) if callable(labor) else labor
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_cost, sales, "
                     "labor_pct) VALUES (?,?,?,?,?,?)",
                     (rid, d.isoformat(), d.strftime("%A"), lab, s, round(lab / s * 100, 1) if s else None))
    conn.commit()
    conn.close()


def _row(db_path, oid):
    return outcomes.get_outcome(oid, db_path=db_path)


def _insert(db_path, rid, title, metric, dollars, started_on, verdict="improved", module=None,
            recheck_verdict=None, attribution=None, concurrent="[]", source="ask", baseline=32.0,
            after=29.0):
    """An evaluated tracker as outcomes.evaluate leaves it."""
    s = date.fromisoformat(started_on)
    e = s + timedelta(days=28)
    conn = get_conn(db_path)
    cur = conn.execute(
        "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, "
        "baseline_value, baseline_start, baseline_end, started_on, evaluate_on, after_value, after_start, "
        "after_end, verdict, delta, dollars_monthly, status, module, recheck_verdict, attribution, concurrent, "
        "baseline_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'evaluated', ?,?,?,?, 'prior window')",
        (rid, source, f"{source}:{title.lower()}", title, metric, baseline,
         (s - timedelta(days=28)).isoformat(), (s - timedelta(days=1)).isoformat(), started_on, e.isoformat(),
         after, started_on, (e - timedelta(days=1)).isoformat(), verdict, round(after - baseline, 2), dollars,
         module, recheck_verdict, attribution, concurrent))
    conn.commit()
    oid = cur.lastrowid
    conn.close()
    return oid


def _labor_change(db_path, rid, t0, before=300.0, after=250.0, until=None, after_fn=None):
    """Labor at `before` for the 28 days before t0 and `after` from t0 until
    yesterday (sales flat at 1,000): 30% then 25%."""
    until = until or TODAY
    n_before = 28
    _days(db_path, rid, t0 - timedelta(days=n_before), n_before, labor=before)
    n = (until - t0).days
    _days(db_path, rid, t0, n, labor=after_fn or after)


# ── #15 overtime hours ──────────────────────────────────────────────────────

_SHIFTS = """date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes
2026-06-01,Monday,Ana,Server,09:00,18:00,9,9,,
2026-06-02,Tuesday,Ana,Server,09:00,18:00,9,9,,
2026-06-03,Wednesday,Ana,Server,09:00,18:00,9,9,,
2026-06-04,Thursday,Ana,Server,09:00,18:00,9,9,,
2026-06-05,Friday,Ana,Server,09:00,18:00,9,9,,
2026-06-01,Monday,Ben,Cook,09:00,17:00,8,8,,
2026-06-08,Monday,Ana,Server,09:00,17:00,8,8,,
2026-06-09,Tuesday,Ana,Server,09:00,17:00,8,8,,
2026-06-10,Wednesday,Ana,Server,09:00,17:00,8,8,,
2026-06-11,Thursday,Ana,Server,09:00,17:00,8,8,,
2026-06-12,Friday,Ana,Server,09:00,17:00,8,8,,
"""


def test_15_overtime_hours_reads_whole_payroll_weeks_only(db_path):
    rid = _rid(db_path, hourly_rate=20.0)
    models.save_client_data(rid, "shifts", _SHIFTS, db_path=db_path)
    # Two whole Monday weeks: Ana 45h then 40h -> 5 overtime hours over 2 weeks.
    v, detail = metrics.measure(rid, "overtime_hours", "2026-06-01", "2026-06-14", db_path)
    assert v == 2.5 and "2 payroll weeks" in detail
    # The week of 6/1 is cut by the window edge: only the week of 6/8 is read,
    # and nobody passed 40 — a MEASURED zero, not an unknown.
    v2, _ = metrics.measure(rid, "overtime_hours", "2026-06-03", "2026-06-14", db_path)
    assert v2 == 0.0


def test_15_overtime_unknown_is_never_zero(db_path):
    rid = _rid(db_path)
    assert metrics.measure(rid, "overtime_hours", "2026-06-01", "2026-06-14", db_path)[0] is None
    models.save_client_data(rid, "shifts", _SHIFTS, db_path=db_path)
    # Weeks with no shifts on file are unknown, not weeks without overtime.
    v, detail = metrics.measure(rid, "overtime_hours", "2026-07-06", "2026-07-19", db_path)
    assert v is None and "no complete payroll week" in detail


def test_15_overtime_dollars_are_the_premium_at_the_blended_wage(db_path):
    rid = _rid(db_path, hourly_rate=20.0)
    models.save_client_data(rid, "shifts", _SHIFTS, db_path=db_path)
    per_hour = metrics.overtime_premium_per_hour(rid, db_path)
    assert per_hour == 10.0                       # 20/h x (1.5 - 1)
    # 2.5 fewer overtime hours a week, lower is better, priced per month.
    assert metrics.monthly_dollars(rid, "overtime_hours", -2.5, db_path) == round(2.5 * 10 * 52 / 12, 2)
    assert metrics.describe("overtime_hours")["family"] == "labor_cost"


def test_15_overtime_noise_band_has_a_floor():
    """15% of half an hour is not a band: a half-hour move is a wobble."""
    assert metrics.compare("overtime_hours", 0.5, 0.0)["verdict"] == "no_clear_change"
    assert metrics.compare("overtime_hours", 12.0, 8.0)["verdict"] == "improved"


# ── #19 review response time ────────────────────────────────────────────────

def _review(db_path, rid, ext, written, approved=None, status="approved"):
    conn = get_conn(db_path)
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, review_date, "
                 "fetched_at, response_status, approved_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (rid, "google", ext, 4, "ok", written, written, status, approved))
    conn.commit()
    conn.close()


def test_19_response_hours_is_the_median_reply_time_and_carries_no_dollars(db_path):
    from datetime import datetime
    rid = _rid(db_path)
    written = datetime(2026, 6, 2, 10, 0, 0)
    for i, hrs in enumerate((2, 4, 6, 8, 100)):
        _review(db_path, rid, f"r{i}", written.strftime("%Y-%m-%d %H:%M:%S"),
                (written + timedelta(hours=hrs)).strftime("%Y-%m-%d %H:%M:%S"))
    _review(db_path, rid, "waiting", "2026-06-03 10:00:00", None, status="drafted")
    v, detail = metrics.measure(rid, "response_hours", "2026-06-01", "2026-06-30", db_path)
    assert v == 6.0                                   # the median, not the 24h mean
    assert "median of 5 replies" in detail and "1 review from this window not answered" in detail
    assert metrics.monthly_dollars(rid, "response_hours", -3.0, db_path) is None
    assert metrics.describe("response_hours")["lower_is_better"] is True


def test_19_response_hours_with_too_few_replies_is_unknown(db_path):
    rid = _rid(db_path)
    for i in range(4):
        _review(db_path, rid, f"r{i}", "2026-06-02 10:00:00", "2026-06-02 12:00:00")
    assert metrics.measure(rid, "response_hours", "2026-06-01", "2026-06-30", db_path)[0] is None


# ── #30 baselines matched by weekday and season ─────────────────────────────

def test_30_labor_baseline_holds_the_same_weekdays(db_path):
    """A 20-day window is not whole weeks: the baseline starts three whole
    weeks back so it holds the same weekdays as the after-window."""
    rid = _rid(db_path)
    t0 = date(2026, 6, 1)
    _days(db_path, rid, t0 - timedelta(days=40), 40)
    o = outcomes.record(rid, "manual", "k20", "Trim", "labor_pct", window_days=20, today=t0, db_path=db_path)
    assert o["baseline_kind"] == "matched weekdays"
    assert o["baseline_start"] == (t0 - timedelta(days=21)).isoformat()
    assert o["baseline_end"] == (t0 - timedelta(days=2)).isoformat()
    assert o["baseline_value"] == 30.0


def test_30_a_rating_baseline_is_the_prior_window(db_path):
    rid = _rid(db_path)
    o = outcomes.record(rid, "manual", "kr", "Reply faster", "avg_rating", today=date(2026, 6, 1),
                        db_path=db_path)
    assert o["baseline_kind"] == "prior window" and o["baseline_value"] is None


def _seasonal_sales(t0):
    """Sales 1,000/day last year, 1,200 over last year's copy of the
    after-window (a seasonal jump), 1,100 over this year's baseline."""
    ly = timedelta(days=364)
    base = (t0 - timedelta(days=28), t0 - timedelta(days=1))
    after = (t0, t0 + timedelta(days=27))

    def fn(d):
        if after[0] - ly <= d <= after[1] - ly:
            return 1200.0
        if base[0] <= d <= base[1]:
            return 1100.0
        return 1000.0
    return fn


def test_30_sales_baseline_moves_as_the_same_weeks_moved_last_year(db_path):
    rid = _rid(db_path)
    t0 = date(2026, 6, 1)
    start = t0 - timedelta(days=420)
    _days(db_path, rid, start, 420, sales=_seasonal_sales(t0))
    o = outcomes.record(rid, "manual", "ks", "Patio open", "sales", today=t0, db_path=db_path)
    assert o["baseline_kind"] == "same weeks last year"
    assert o["baseline_raw"] == 1100.0
    assert o["baseline_value"] == 1320.0              # 1,100 x (1,200 / 1,000)
    assert "same weeks moved last year" in o["baseline_detail"]


def test_30_a_seasonal_rise_is_not_read_as_a_win(db_path):
    """The seasonal jump arrives on schedule: against the adjusted baseline
    it is no clear change, where the prior window would have called +20% a
    win."""
    rid = _rid(db_path)
    t0 = date(2026, 6, 1)
    _days(db_path, rid, t0 - timedelta(days=420), 420, sales=_seasonal_sales(t0))
    _days(db_path, rid, t0, 28, sales=1320.0)
    o = outcomes.record(rid, "manual", "ks", "Patio open", "sales", today=t0, db_path=db_path)
    done = outcomes.evaluate(o["id"], today=t0 + timedelta(days=28), db_path=db_path)
    assert done["verdict"] == "no_clear_change"
    assert metrics.compare("sales", 1100.0, 1320.0)["verdict"] == "improved"   # what the old baseline said


def test_30_without_a_year_of_history_it_stays_matched_weekdays(db_path):
    rid = _rid(db_path)
    t0 = date(2026, 6, 1)
    _days(db_path, rid, t0 - timedelta(days=28), 28)
    o = outcomes.record(rid, "manual", "ks", "Patio open", "sales", today=t0, db_path=db_path)
    assert o["baseline_kind"] == "matched weekdays" and o["baseline_value"] == 1000.0


def test_30_seasonal_comparisons_use_a_wider_band():
    assert metrics.compare("labor_pct", 30.0, 29.4)["verdict"] == "improved"
    assert metrics.compare("labor_pct", 30.0, 29.4, band_scale=outcomes.SEASONAL_BAND_SCALE)["verdict"] \
        == "no_clear_change"


# ── #16 / #23 evaluation stores the numbers and grades them ─────────────────

def test_16_23_evaluation_stores_before_after_change_and_a_consistent_grade(db_path):
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=28)
    _labor_change(db_path, rid, t0)
    o = outcomes.record(rid, "manual", "trim", "Trim Monday lunch", "labor_pct", today=t0, db_path=db_path)
    done = outcomes.evaluate(o["id"], today=TODAY, db_path=db_path)
    assert done["baseline_value"] == 30.0 and done["after_value"] == 25.0
    assert done["delta"] == -5.0 and done["delta_pct"] == round(-5.0 / 30.0 * 100, 1)
    assert done["baseline_kind"] == "matched weekdays"
    assert done["verdict"] == "improved"
    # Ten noise bands and nothing else on labor cost in the window.
    assert done["concurrent"] == [] and done["concurrent_checked"]
    assert done["attribution"] == "consistent"
    assert "cause" not in done["attribution_label"].replace("not proven cause", "")
    assert done["recheck_on"] == (t0 + timedelta(days=90)).isoformat()
    assert done["result_line"] == "Labor % 30% → 25%, improved"


def test_23_a_move_past_the_band_once_is_associated_and_carries_the_caveat(db_path):
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=28)
    _labor_change(db_path, rid, t0, after=292.0)      # 30% -> 29.2%: 1.6 bands
    o = outcomes.record(rid, "manual", "trim", "Trim", "labor_pct", today=t0, db_path=db_path)
    done = outcomes.evaluate(o["id"], today=TODAY, db_path=db_path)
    assert done["verdict"] == "improved" and done["attribution"] == "associated"
    assert outcomes.CAUSATION_CAVEAT in done["attribution_label"]


def test_23_grades():
    assert outcomes.grade("no_clear_change", 0.4, []) == "none"
    assert outcomes.grade("unknown", None, []) == "none"
    assert outcomes.grade("improved", 1.2, []) == "associated"
    assert outcomes.grade("improved", 3.0, []) == "consistent"
    assert outcomes.grade("improved", 3.0, [{"kind": "event"}]) == "associated"      # capped (#31)
    assert outcomes.grade("improved", 1.2, [], recheck_verdict="held") == "held"
    assert outcomes.grade("improved", 3.0, [{"kind": "tracker"}], recheck_verdict="held") == "associated"
    assert outcomes.grade("improved", 3.0, None, checked=False) == "associated"       # never checked


def test_unknown_stays_unknown_and_accrues_nothing(db_path):
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=28)
    o = outcomes.record(rid, "manual", "trim", "Trim", "labor_pct", today=t0, db_path=db_path)
    done = outcomes.evaluate(o["id"], today=TODAY, db_path=db_path)
    assert done["verdict"] == "unknown" and done["delta"] is None and done["dollars_monthly"] is None
    assert done["attribution"] == "none" and done["recheck_on"] is None
    assert outcomes.cumulative(rid, db_path=db_path)["total"] is None      # nothing measured is not $0


# ── #31 other changes in the window ─────────────────────────────────────────

def test_31_concurrent_changes_are_listed_and_cap_the_grade(db_path, monkeypatch):
    import rec_ledger
    import schedule_rules
    rid = _rid(db_path)
    s, e = TODAY - timedelta(days=5), TODAY + timedelta(days=5)
    r = {"id": 0, "restaurant_id": rid, "metric": "labor_pct", "source": "manual", "source_key": "mine",
         "baseline_start": (s - timedelta(days=28)).isoformat(), "baseline_end": (s - timedelta(days=1)).isoformat(),
         "baseline_kind": "matched weekdays"}
    conn = get_conn(db_path)
    conn.execute("INSERT INTO reprice_decisions (restaurant_id, dish, old_price, suggested_price, chosen_price, "
                 "source, created_at) VALUES (?,?,?,?,?,?,?)", (rid, "Carbonara", 18, 20, 20, "one_tap",
                                                               f"{TODAY.isoformat()} 12:00:00"))
    conn.execute("INSERT INTO demand_signals (restaurant_id, date, kind, label) VALUES (?,?,?,?)",
                 (rid, TODAY.isoformat(), "event", "Rehearsal dinner"))
    conn.commit()
    conn.close()
    schedule_rules.save_closures(rid, closed_dates=[(TODAY + timedelta(days=1)).isoformat()], db_path=db_path)
    rec_ledger.present(rid, "trim_day:Friday", "labor", "home", title="Trim Friday close")
    rec_ledger.record(rid, "trim_day:Friday", "accepted", surface="home")
    rec_ledger.present(rid, "cut_waste:Salmon", "food", "home", title="Cut salmon waste")
    rec_ledger.record(rid, "cut_waste:Salmon", "accepted", surface="home")       # another family
    monkeypatch.setattr(outcomes, "_holidays_between",
                        lambda a, b: {TODAY.isoformat(): "Test Day"} if a == s.isoformat() else {})
    conc = outcomes.find_concurrent(r, s.isoformat(), e.isoformat(), db_path)
    kinds = {c["kind"] for c in conc}
    assert kinds == {"price_change", "event", "closure", "accepted_rec", "holiday"}
    assert all(set(c) == {"kind", "label", "date"} for c in conc)
    assert not any(c["label"] == "Cut salmon waste" for c in conc)
    assert outcomes.grade("improved", 5.0, conc) == "associated"


def test_31_ratings_ignore_trading_volume_changes(db_path):
    """An event moves covers, not a star rating: only trackers and accepted
    recommendations on the same family count for reviews."""
    rid = _rid(db_path)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO demand_signals (restaurant_id, date, kind, label) VALUES (?,?,?,?)",
                 (rid, TODAY.isoformat(), "event", "Party"))
    conn.commit()
    conn.close()
    r = {"id": 0, "restaurant_id": rid, "metric": "avg_rating", "source": "manual", "source_key": "x"}
    assert outcomes.find_concurrent(r, (TODAY - timedelta(days=3)).isoformat(),
                                    (TODAY + timedelta(days=3)).isoformat(), db_path) == []


def test_31_an_overlapping_tracker_in_the_family_caps_the_evaluation(db_path):
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=28)
    _labor_change(db_path, rid, t0)
    a = outcomes.record(rid, "manual", "trim", "Trim", "labor_pct", today=t0, db_path=db_path)
    outcomes.record(rid, "manual", "ot", "Cap overtime", "overtime_hours", today=t0 + timedelta(days=3),
                    db_path=db_path)                  # a different metric, the same family
    done = outcomes.evaluate(a["id"], today=TODAY, db_path=db_path)
    assert [c["kind"] for c in done["concurrent"]] == ["tracker"]
    assert done["attribution"] == "associated"
    assert "can't be separated" in done["attribution_label"]


# ── #33 / #34 the 90-day re-check and "validated" ───────────────────────────

def _evaluated_win(db_path, rid, t0, after_fn=None):
    _labor_change(db_path, rid, t0, after_fn=after_fn)
    o = outcomes.record(rid, "manual", "trim", "Trim Monday lunch", "labor_pct", today=t0, db_path=db_path)
    return outcomes.evaluate(o["id"], today=t0 + timedelta(days=28), db_path=db_path)


def test_33_34_a_win_that_holds_at_90_days_is_validated(db_path):
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=100)
    done = _evaluated_win(db_path, rid, t0)
    assert outcomes.recheck(done["id"], today=t0 + timedelta(days=60), db_path=db_path) is None   # too early
    rc = outcomes.recheck(done["id"], today=t0 + timedelta(days=90), db_path=db_path)
    assert rc["recheck_verdict"] == "held" and rc["recheck_value"] == 25.0
    assert rc["attribution"] == "held" and rc["validated"] is True
    v = outcomes.total_value(rid, db_path=db_path)
    assert v["validated"] == 1 and v["validated_monthly"] == v["monthly"] > 0
    assert outcomes.recheck(done["id"], today=TODAY, db_path=db_path) is None     # once only


def test_33_a_win_that_faded_stops_counting_and_stops_accruing(db_path):
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=120)
    fell_back = t0 + timedelta(days=50)
    done = _evaluated_win(db_path, rid, t0, after_fn=lambda d: 250.0 if d < fell_back else 300.0)
    assert done["verdict"] == "improved"
    before = outcomes.total_value(rid, db_path=db_path)
    assert before["monthly"] > 0 and before["wins"] == 1
    rc = outcomes.recheck_due(rid, db_path=db_path, today=TODAY)
    assert [r["recheck_verdict"] for r in rc] == ["faded"]
    after = outcomes.total_value(rid, db_path=db_path)
    assert after["monthly"] == 0 and after["wins"] == 0 and after["faded"] == 1
    assert value_delivered.delivered(rid, db_path=db_path)["biggest"] is None
    assert "no longer counts" in rc[0]["attribution_label"]
    # Accrue to the end: nothing on or after the re-check date, and the days
    # the trailing reading stopped holding carry nothing.
    for _ in range(6):
        outcomes.accrue_due(rid, db_path=db_path, today=TODAY)
    conn = get_conn(db_path)
    rows = conn.execute("SELECT day, dollars, held, counted FROM outcome_value_days WHERE outcome_id=? "
                        "ORDER BY day", (done["id"],)).fetchall()
    conn.close()
    assert rows and max(r["day"] for r in rows) < rc[0]["recheck_on"]
    assert any(not r["held"] for r in rows)
    assert all(r["dollars"] == 0 and not r["counted"] for r in rows if not r["held"])
    cum = outcomes.cumulative(rid, db_path=db_path)
    assert cum["total"] > 0          # what was measured while it held stays measured


def test_33_legacy_wins_get_a_recheck_date_at_boot(db_path):
    rid = _rid(db_path)
    oid = _insert(db_path, rid, "Old win", "labor_pct", 400.0, "2026-01-05")
    conn = get_conn(db_path)
    conn.execute("UPDATE recommendation_outcomes SET module=NULL, baseline_kind=NULL, delta_pct=NULL, "
                 "recheck_on=NULL, concurrent=NULL WHERE id=?", (oid,))
    conn.commit()
    conn.close()
    assert outcomes.init_outcomes(db_path) == 1
    r = _row(db_path, oid)
    assert r["recheck_on"] == "2026-04-05"                    # 90 days from the start
    assert r["module"] == "labor" and r["baseline_kind"] == "prior window"
    assert r["delta_pct"] == round(-3.0 / 32.0 * 100, 1)
    assert r["concurrent_checked"] is False and r["attribution"] == "associated"
    assert outcomes.init_outcomes(db_path) == 0                # once


# ── #14 cumulative, day by day ──────────────────────────────────────────────

def test_14_the_window_accrues_only_its_measured_days(db_path):
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=28)
    skip = {t0 + timedelta(days=i) for i in (3, 10, 17, 24)}         # four days with no data
    _days(db_path, rid, t0 - timedelta(days=28), 28, labor=300.0)
    _days(db_path, rid, t0, 28, labor=250.0, skip=skip)
    o = outcomes.record(rid, "manual", "trim", "Trim", "labor_pct", today=t0, db_path=db_path)
    done = outcomes.evaluate(o["id"], today=TODAY, db_path=db_path)
    per_day = round(done["dollars_monthly"] / metrics.DAYS_PER_MONTH, 4)
    cum = outcomes.cumulative(rid, db_path=db_path)
    assert cum["days"] == 24 and cum["measured_days"] == 24
    assert cum["total"] == round(per_day * 24, 2)
    assert cum["since"] == t0.isoformat()
    assert cum["by_module"] == {"labor": round(per_day * 24, 2)}
    assert done["accrued_through"] == (TODAY - timedelta(days=1)).isoformat()


def test_14_days_after_the_window_accrue_while_the_win_holds(db_path):
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=45)
    done = _evaluated_win(db_path, rid, t0)
    window_total = outcomes.cumulative(rid, db_path=db_path)["total"]
    written = outcomes.accrue_due(rid, db_path=db_path, today=TODAY)
    assert written == 17                               # t0+28 .. yesterday
    cum = outcomes.cumulative(rid, db_path=db_path)
    per_day = round(done["dollars_monthly"] / metrics.DAYS_PER_MONTH, 4)
    assert cum["days"] == 45 and cum["total"] == round(window_total + per_day * 17, 2)
    assert outcomes.accrue_due(rid, db_path=db_path, today=TODAY) == 0    # a cursor, not a re-read


def test_14_a_day_with_no_data_yet_is_waited_for_then_passed_over(db_path):
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=40)
    done = _evaluated_win(db_path, rid, t0)
    conn = get_conn(db_path)
    conn.execute("DELETE FROM labor_daily_history WHERE restaurant_id=? AND date>=?",
                 (rid, (TODAY - timedelta(days=2)).isoformat()))
    conn.commit()
    conn.close()
    outcomes.accrue_due(rid, db_path=db_path, today=TODAY)
    # The last two days may still sync: not read past, not accrued.
    assert _row(db_path, done["id"])["accrued_through"] == (TODAY - timedelta(days=3)).isoformat()


def test_14_worsened_is_negative_and_related_numbers_count_once_a_day(db_path):
    rid = _rid(db_path)
    base = {"restaurant_id": rid, "status": "evaluated", "source_key": "k", "module": "inventory"}
    waste = dict(base, id=_insert(db_path, rid, "Waste", "weekly_waste", 300.0, "2026-06-01", module="inventory"),
                 metric="weekly_waste", verdict="improved")
    food = dict(base, id=_insert(db_path, rid, "Food", "food_cost_pct", 400.0, "2026-06-01", module="inventory"),
                metric="food_cost_pct", verdict="improved")
    labor = dict(base, id=_insert(db_path, rid, "Labor", "labor_pct", -150.0, "2026-06-01", verdict="worsened",
                                  module="labor"), metric="labor_pct", verdict="worsened", module="labor")
    conn = get_conn(db_path)
    for day in ("2026-06-01", "2026-06-02"):
        outcomes._put_day(conn, waste, day, 10.0, True, "window")
        outcomes._put_day(conn, food, day, 13.0, True, "window")    # larger: it is the one counted
        outcomes._put_day(conn, labor, day, -5.0, True, "window")
    outcomes._put_day(conn, waste, "2026-06-03", 10.0, True, "window")
    conn.commit()
    conn.close()
    cum = outcomes.cumulative(rid, db_path=db_path)
    assert cum["gained"] == 36.0 and cum["lost"] == 10.0 and cum["total"] == 26.0
    assert cum["by_module"] == {"inventory": 36.0, "labor": -10.0}
    assert cum["days"] == 3
    limited = outcomes.cumulative(rid, db_path=db_path, denied_modules={"inventory"})
    assert limited["total"] == -10.0 and "inventory" not in limited["by_module"]


def test_14_accrual_stops_at_the_horizon(db_path):
    r = {"status": "evaluated", "verdict": "improved", "dollars_monthly": 100.0, "source_key": "k",
         "metric": "labor_pct", "restaurant_id": 1, "id": 1, "started_on": "2025-01-01",
         "evaluate_on": "2025-01-29", "accrued_through": "2025-12-31"}
    assert outcomes.accrue_daily(r, today=date(2026, 6, 1)) == 0


# ── #1 net of what got worse, #4 one win per family ─────────────────────────

def test_1_worse_results_are_netted_beside_the_improvements(db_path):
    rid = _rid(db_path)
    _insert(db_path, rid, "Trim Monday", "labor_pct", 400.0, "2026-06-01", module="labor")
    _insert(db_path, rid, "New supplier", "food_cost_pct", -150.0, "2026-06-01", verdict="worsened",
            module="inventory")
    v = outcomes.total_value(rid, db_path=db_path)
    assert v["monthly"] == 400.0                     # improvements, as every surface reads them
    assert v["worsened"] == {"count": 1, "monthly": 150.0}
    assert v["net_monthly"] == 250.0
    assert v["net_by_module"] == {"labor": 400.0, "inventory": -150.0}
    assert "less $150/month" in v["net_note"]
    d = value_delivered.delivered(rid, db_path=db_path)
    assert d["net_monthly"] == 250.0 and d["worsened"]["monthly"] == 150.0


def test_1_net_below_zero_is_said_not_hidden(db_path):
    rid = _rid(db_path)
    _insert(db_path, rid, "Trim", "labor_pct", 100.0, "2026-06-01")
    _insert(db_path, rid, "Cheaper cheese", "food_cost_pct", -300.0, "2026-06-01", verdict="worsened")
    v = outcomes.total_value(rid, db_path=db_path)
    assert v["net_monthly"] == -200.0
    assert "below zero because that is what was measured" in v["net_note"]


def test_1_a_worse_result_that_recovered_at_its_recheck_is_no_longer_subtracted(db_path):
    rid = _rid(db_path)
    _insert(db_path, rid, "Cheaper cheese", "food_cost_pct", -300.0, "2026-06-01", verdict="worsened",
            recheck_verdict="faded")
    assert outcomes.total_value(rid, db_path=db_path)["worsened"] == {"count": 0, "monthly": 0.0}


def test_4_waste_and_food_cost_over_the_same_weeks_are_one_win(db_path):
    rid = _rid(db_path)
    _insert(db_path, rid, "Smaller salmon order", "weekly_waste", 300.0, "2026-06-01", module="inventory")
    _insert(db_path, rid, "Reprice salmon", "food_cost_pct", 400.0, "2026-06-10", module="inventory")
    v = outcomes.total_value(rid, db_path=db_path)
    assert v["monthly"] == 400.0 and v["wins"] == 1


@pytest.mark.parametrize("a,b", [("labor_pct", "overtime_hours"), ("sales", "weekday_sales:Tuesday"),
                                 ("avg_rating", "complaints:service")])
def test_4_each_family_counts_once_per_overlapping_window(db_path, a, b):
    rid = _rid(db_path)
    _insert(db_path, rid, "First", a, 300.0, "2026-06-01")
    _insert(db_path, rid, "Second", b, 200.0, "2026-06-15")
    assert outcomes.total_value(rid, db_path=db_path)["wins_measured"] == 1


def test_4_separate_windows_in_one_family_both_count(db_path):
    rid = _rid(db_path)
    _insert(db_path, rid, "June", "weekly_waste", 300.0, "2026-06-01")
    _insert(db_path, rid, "September", "food_cost_pct", 400.0, "2026-09-01")
    v = outcomes.total_value(rid, db_path=db_path)
    assert v["monthly"] == 700.0 and v["wins"] == 2


def test_4_overlapping_losses_in_one_family_are_subtracted_once(db_path):
    rid = _rid(db_path)
    _insert(db_path, rid, "A", "weekly_waste", -100.0, "2026-06-01", verdict="worsened")
    _insert(db_path, rid, "B", "food_cost_pct", -250.0, "2026-06-05", verdict="worsened")
    assert outcomes.total_value(rid, db_path=db_path)["worsened"] == {"count": 1, "monthly": 250.0}


# ── #5 credited to the recommendation's module ──────────────────────────────

def test_5_module_comes_from_the_recommendation_not_the_metric(db_path):
    import rec_ledger
    rid = _rid(db_path)
    rec_ledger.present(rid, "promote_day:Tuesday", "marketing", "home", title="Fill Tuesdays",
                       expected_metric="sales")
    o = outcomes.record(rid, "recommendation", "promote_day:Tuesday", "Fill Tuesdays", "sales",
                        today=date(2026, 6, 1), db_path=db_path)
    assert o["module"] == "marketing"                      # not labor, as "sales" used to be
    ask = outcomes.record(rid, "ask", "ask:happy hour", "Happy hour", "comp_rate", today=date(2026, 6, 1),
                          db_path=db_path)
    assert ask["module"] == "labor"                        # nothing recommended it: the metric's module
    assert outcomes.module_of("sales") == "other"
    campaign = outcomes.record(rid, "slow_day_campaign", "campaign:Wednesday:2026-06-01", "Text",
                               "weekday_sales:Wednesday", today=date(2026, 6, 1), db_path=db_path)
    assert campaign["module"] == "marketing"


def test_5_by_module_reads_the_stored_module(db_path):
    rid = _rid(db_path)
    _insert(db_path, rid, "Tuesday promo", "sales", 500.0, "2026-06-01", module="marketing")
    v = outcomes.total_value(rid, db_path=db_path)
    assert v["by_module"] == {"marketing": 500.0} and v["wins_by_module"] == {"marketing": 1}


def test_5_a_denied_metric_is_dropped_whoever_is_credited(db_path):
    """A food-cost number credited to Marketing is still food-cost dollars."""
    rid = _rid(db_path)
    _insert(db_path, rid, "Promo", "food_cost_pct", 500.0, "2026-06-01", module="marketing")
    assert outcomes.total_value(rid, db_path=db_path, denied_modules={"inventory"})["monthly"] == 0


# ── #49 review wins without dollars ─────────────────────────────────────────

def test_49_a_rating_win_counts_as_a_win_without_inventing_dollars(db_path):
    rid = _rid(db_path)
    _insert(db_path, rid, "Reply to every review", "avg_rating", None, "2026-06-01", module="reviews",
            baseline=4.2, after=4.4)
    v = outcomes.total_value(rid, db_path=db_path)
    assert v["monthly"] == 0 and v["wins"] == 0          # nothing priced
    assert v["wins_measured"] == 1 and v["wins_by_module"] == {"reviews": 1}
    assert v["unpriced_wins"][0]["line"] == "Average rating 4.2★ → 4.4★, improved"
    led = value_delivered.ledger(rid, db_path=db_path)
    assert led["outcomes_improved"] == 1
    assert "1 change measured as an improvement" in value_delivered.ledger_lines(led)


# ── #12 every stated rate in the payload ────────────────────────────────────

def test_12_rates_carry_the_reply_rate_and_every_other_stated_rate(db_path):
    rid = _rid(db_path)
    rates = value_delivered.delivered(rid, db_path=db_path)["rates"]
    assert rates["reply_rate"] == value_delivered.REPLY_RATE
    assert rates["agency_monthly"] == value_delivered.AGENCY_MONTHLY
    assert {"schedule_minutes", "invoice_minutes", "reply_minutes", "overtime_multiplier",
            "days_per_month", "weeks_per_month", "recheck_days", "accrual_horizon_days"} <= set(rates)
    # Rates are beside the figures, never one of them.
    assert set(value_delivered.breakdown(rid, db_path=db_path)) == {"delivered", "avoided", "opportunity",
                                                                   "surfaced"}


# ── interim readings ────────────────────────────────────────────────────────

def test_interim_reading_names_its_date_and_days_in(db_path):
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=10)
    _labor_change(db_path, rid, t0)
    outcomes.record(rid, "manual", "trim", "Trim", "labor_pct", today=t0, db_path=db_path)
    live = outcomes.progress(rid, db_path=db_path)[0]["interim"]
    assert live["as_of"] == (TODAY - timedelta(days=1)).isoformat() and live["days_in"] == 10
    assert live["value"] == 25.0 and live["delta"] == -5.0 and live["delta_pct"] == round(-5 / 30 * 100, 1)


# ── #39 DSR actions by kind ─────────────────────────────────────────────────

def test_39_every_dsr_action_kind_has_a_decision():
    from dsr.narrative import ACTION_KINDS
    assert set(outcomes.DSR_ACTION_METRICS) == set(ACTION_KINDS)
    assert all(m is None or metrics.known(m) for m in outcomes.DSR_ACTION_METRICS.values())
    assert outcomes.DSR_ACTION_METRICS["control_hours"] == "labor_pct"
    assert outcomes.DSR_ACTION_METRICS["reduce_waste"] == "weekly_waste"
    assert outcomes.DSR_ACTION_METRICS["reorder"] is None


def test_39_dsr_metric_is_authoritative(db_path):
    rid = _rid(db_path)
    assert outcomes.metric_for_rec(rid, "dsr_action:reorder:food/brioche-buns") == (None, True)
    assert outcomes.metric_for_rec(rid, "dsr_action:control_hours:labor") == ("labor_pct", True)
    assert outcomes.resolve_module(rid, "recommendation", "dsr_action:reduce_waste:food/fish",
                                   "weekly_waste", db_path=db_path) == "inventory"
