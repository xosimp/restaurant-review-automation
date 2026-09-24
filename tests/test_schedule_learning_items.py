"""Schedule learning, audit items #39, #42, #43, #46 and #48.

  #39  the Shift Quality weights fitted to recorded outcomes: a joint fit,
       a minimum record, bounded steps from where the weights are now,
       coverage issues counted only on nights Cavnar was watching, and the
       outcome that drove each change named;
  #42  which draft rows the manager will change, predicted from smoothed
       edit rates over their own finished drafts, with the likelihood and
       why — and its hit rate measured on held-out weeks;
  #43  a starting headcount borrowed from similar restaurants for a
       restaurant with no history, only over MIN_COHORT, ratios only,
       labelled borrowed;
  #46  fairness planned over a multi-week rotation: who is next for a
       weekend off and a close, fed to the prompt and to the score;
  #48  sales per labor hour by daypart as an objective: a stated target,
       the prompt's hours per shift, a scored dimension that withdraws
       without sales, and the review's line.
"""
import datetime as dt
import json
import random
import sys

import pytest

import schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_quality, staff_settings  # noqa: E401,F401

import models
import schedule_economics as econ
import schedule_intel as si
import schedule_learning as sl
import schedule_requirements as req
import schedule_versions as sv
import shift_quality as sq
from intelligence import benchmarks, features, privacy, staffing
from models import create_restaurant, Restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    return db_path


def _restaurant(db_path, name="Learning Co", **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email="l@x.com"), db_path=db_path)
    if cols:
        c = models.get_conn(db_path)
        c.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
        c.commit()
        c.close()
    return rid


def _week(i, start=dt.date(2026, 10, 5)):
    s = start + dt.timedelta(weeks=i)
    return [(s + dt.timedelta(days=k)).isoformat() for k in range(7)]


def _row(date, emp, start="4:00pm", end="10:00pm", role="Server", hours=6.0, notes=""):
    return {"date": date, "day": dt.date.fromisoformat(date).strftime("%A"), "employee": emp, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": str(hours), "notes": notes}


def _csv(rows):
    return HEADER + "".join(",".join(str(r[c]) for c in sv.COLS) + "\n" for r in rows)


def _history(db_path, rid, week_start, csv_text=HEADER, published=False, quality=None, target=30):
    c = models.get_conn(db_path)
    cur = c.execute(
        "INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, labor_target, "
        "schedule_csv, summary_json, quality_json, published_at) VALUES (?,?,?,?,?,?,?,'[]',?,?)",
        (rid, week_start, week_start, 0, 100, target, csv_text, json.dumps(quality) if quality else None,
         "2026-10-01 10:00:00" if published else None))
    c.commit()
    hid = cur.lastrowid
    c.close()
    return hid


# ══ #39 — weights fitted to outcomes ══════════════════════════════════════

def _calibration_world(db_path, rid, weeks=9, shifts_per_week=6, curve_follows=False):
    """Coverage drives every outcome; fairness is noise. With curve_follows,
    coverage_curve tracks coverage closely but has no effect of its own."""
    rng = random.Random(7)
    c = models.get_conn(db_path)
    for i in range(weeks):
        days = _week(i)
        shifts, outs = [], []
        for k in range(shifts_per_week):
            d, part = days[k % 7], ("night" if k % 2 else "morning")
            cov = rng.choice([50, 60, 70, 80, 90, 100])
            dims = [{"key": "coverage", "score": cov}, {"key": "fairness", "score": rng.choice([40, 60, 80, 100])},
                    {"key": "stability", "score": 80}]
            if curve_follows:
                dims.append({"key": "coverage_curve", "score": max(0, min(100, cov + rng.choice([-20, -10, 0, 10, 20])))})
            shifts.append({"date": d, "daypart": part, "scored": True, "score": cov, "dimensions": dims})
            issues = 0 if cov >= 80 else (1 if cov >= 60 else 2)
            outs.append((d, part, issues, 4.8 if cov >= 80 else 3.9, 30 + (100 - cov) / 20))
        cur = c.execute(
            "INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, labor_target, "
            "schedule_csv, summary_json, quality_json, published_at) VALUES (?,?,?,0,0,30,?,'[]',?,'2026-10-01')",
            (rid, days[0], days[6], HEADER, json.dumps({"score": 80, "shifts": shifts})))
        for d, part, issues, rating, labor in outs:
            c.execute("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, people, issues, "
                      "review_rating, labor_pct) VALUES (?,?,?,?,10,2,?,?,?)", (rid, cur.lastrowid, d, part, issues, rating, labor))
    c.commit()
    c.close()


def _watch_everything(monkeypatch):
    monkeypatch.setattr(si, "watched_dates", lambda rid, a, b, db_path=None: {
        (dt.date.fromisoformat(a) + dt.timedelta(days=i)).isoformat()
        for i in range((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days + 1)})


def test_issues_count_only_on_nights_cavnar_was_watching(db, monkeypatch):
    rid = _restaurant(db)
    _calibration_world(db, rid)
    monkeypatch.setattr(si, "watched_dates", lambda *a, **k: set())
    blind = sl.calibrate_weights(rid, db_path=db)
    assert blind["ready"] and blind["watched_shifts"] == 0
    assert blind["dimensions"]["coverage"]["correlation"]["issues"] is None, "an unwatched quiet night is not a clean one"
    assert blind["dimensions"]["coverage"]["pairs"]["issues"] == 0
    assert "not counted" in blind["note"]
    _watch_everything(monkeypatch)
    seen = sl.calibrate_weights(rid, db_path=db)
    assert seen["watched_shifts"] == 54
    assert seen["dimensions"]["coverage"]["correlation"]["issues"] < -0.5


def test_one_apply_moves_a_weight_a_bounded_step_and_says_which_outcome_moved_it(db, monkeypatch):
    rid = _restaurant(db)
    _calibration_world(db, rid)
    _watch_everything(monkeypatch)
    default = sq.DEFAULT_WEIGHTS["coverage"]
    out = sl.calibrate_weights(rid, db_path=db, current_weights={})
    cov = out["dimensions"]["coverage"]
    assert cov["target"] == round(default * 1.3, 1), "the fit wants the full 30%"
    assert cov["suggested"] == round(default * 1.1, 1), "but one apply moves 10% of the default"
    assert cov["driver"]["outcome"] in ("issues", "review_rating", "labor_vs_target") and cov["driver"]["shifts"] == 54
    assert cov["explanation"].startswith("Coverage up: shifts where it scored higher had ")
    assert "54 shifts" in cov["explanation"]
    # From where the weights are now, not from the default: the next apply
    # takes the next step, and stops at the fitted weight.
    nearly = sl.calibrate_weights(rid, db_path=db, current_weights={"coverage": default * 1.25})
    assert nearly["dimensions"]["coverage"]["suggested"] == round(default * 1.3, 1)
    there = sl.calibrate_weights(rid, db_path=db, current_weights={"coverage": default * 1.3})
    assert there["dimensions"]["coverage"]["suggested"] == round(default * 1.3, 1)
    assert "already where the record points" in there["dimensions"]["coverage"]["explanation"]
    # A dimension that never varied cannot be read, and says why.
    assert out["dimensions"]["stability"]["suggested"] == sq.DEFAULT_WEIGHTS["stability"]
    assert out["dimensions"]["leadership"]["explanation"].startswith("Leadership has not been scored on enough shifts")
    assert out["applied"] is False and "coverage" in out["moving"]


def test_the_fit_is_joint_so_a_follower_does_not_take_the_leaders_credit(db, monkeypatch):
    """Scored one at a time, a dimension that merely moves with coverage
    looked as predictive as coverage. Fitted together, the one that drives
    the outcomes carries the larger effect."""
    rid = _restaurant(db)
    _calibration_world(db, rid, weeks=10, shifts_per_week=8, curve_follows=True)
    _watch_everything(monkeypatch)
    out = sl.calibrate_weights(rid, db_path=db)
    cov, curve = out["dimensions"]["coverage"], out["dimensions"]["coverage_curve"]
    for o in ("issues", "review_rating"):
        assert abs(curve["correlation"][o]) > 0.5, "on its own the follower correlates"
        assert abs(cov["coefficient"][o]) > abs(curve["coefficient"][o]), o


def test_calibration_below_the_minimum_record_suggests_nothing(db):
    rid = _restaurant(db)
    _calibration_world(db, rid, weeks=5)
    out = sl.calibrate_weights(rid, db_path=db)
    assert out["ready"] is False and "at least 8 weeks and 40 shifts" in out["reason"]
    assert "suggested_weights" not in out


# ══ #42 — predicting the manager's edits ══════════════════════════════════

def _draft(i):
    w = _week(i)
    rows = [_row(w[d], n) for n in ("Ana", "Ben", "Cy", "Dee", "Eve", "Flo") for d in range(5)]
    rows += [_row(w[5], "Zed"), _row(w[6], "Zed")]
    rows += [_row(w[5], "Gus", "10:00am", "3:00pm", hours=5, notes="added — coverage top-up"),
             _row(w[6], "Hal", "10:00am", "3:00pm", hours=5, notes="added — coverage top-up")]
    return rows


def _manager(i, rows):
    """Always takes Zed off the weekend, always moves the top-up rows'
    start, and each week takes one ordinary row off (a different one)."""
    out = []
    for r in rows:
        if r["employee"] == "Zed":
            continue
        if "top-up" in r["notes"]:
            r = dict(r, shift_start="11:00am", scheduled_hours="4")
        out.append(r)
    victim = ("Ana", "Ben", "Cy", "Dee", "Eve", "Flo")[i % 6]
    drop = next(j for j, r in enumerate(out) if r["employee"] == victim)
    return out[:drop] + out[drop + 1:]


def _edit_history(db, rid, weeks):
    for i in range(weeks):
        d = _draft(i)
        hid = _history(db, rid, _week(i)[0], _csv(d))
        sv.append(rid, hid, "generated", _csv(d), saved_by="Cavnar AI", db_path=db)
        sv.append(rid, hid, "edited", _csv(_manager(i, [dict(r) for r in d])), saved_by="will", db_path=db)


def test_no_prediction_under_the_minimum_history(db):
    rid = _restaurant(db)
    _edit_history(db, rid, 2)
    model = sl.edit_predictor(rid, db_path=db)
    assert model["ready"] is False and model["weeks"] == 2
    assert "needs at least 3 drafts" in model["reason"]
    assert sl.predict_row_edits(rid, _draft(9), db_path=db) == []


def test_rows_like_the_ones_the_manager_keeps_changing_are_flagged_with_why(db):
    rid = _restaurant(db)
    _edit_history(db, rid, 5)
    weeks = sl.prediction_weeks(rid, db_path=db)
    assert len(weeks) == 5 and all(sum(w["edited"]) == 5 for w in weeks)
    flagged = sl.predict_row_edits(rid, _draft(9), db_path=db)
    who = {(f["employee"], f["date"]) for f in flagged}
    w = _week(9)
    assert who == {("Zed", w[5]), ("Zed", w[6]), ("Gus", w[5]), ("Hal", w[6])}, flagged
    zed = next(f for f in flagged if f["employee"] == "Zed")
    assert zed["likelihood"] >= 0.5 and "you changed 10 of 10 of Zed's shifts" in zed["reason"]
    assert "likely you'll change it" in zed["text"] and f"{dt.date.fromisoformat(w[5]).month}/" in zed["text"]
    assert "-" not in zed["text"].split("—")[0], "the date is M/D/YY, never ISO"
    gus = next(f for f in flagged if f["employee"] == "Gus")
    assert "rows a fill-in pass added" in gus["reason"]


def test_the_likely_edit_percentage_is_withheld_until_its_backtest_has_weeks(db):
    """The per-row "% likely" is a naive-Bayes score, not a calibrated
    probability (CA1 L15). With three drafts the model is ready but no
    held-out week can be scored (each training set is two weeks), so nothing
    is shown — it used to be (fix I9)."""
    rid = _restaurant(db)
    _edit_history(db, rid, 3)
    assert sl.edit_predictor(rid, db_path=db)["ready"] is True
    assert sl.edit_prediction_backtest(sl.prediction_weeks(rid, db_path=db))["weeks"] < sl.PREDICT_MIN_BACKTEST_WEEKS
    assert sl.predict_row_edits(rid, _draft(9), db_path=db) == []


def test_every_likely_edit_carries_its_backtest_beside_it(db):
    rid = _restaurant(db)
    _edit_history(db, rid, 5)
    flagged = sl.predict_row_edits(rid, _draft(9), db_path=db)
    bt = sl.edit_prediction_backtest(sl.prediction_weeks(rid, db_path=db))
    assert flagged and all(f["backtest_hit_rate"] == bt["hit_rate"] and f["backtest_weeks"] == bt["weeks"]
                           for f in flagged)
    zed = next(f for f in flagged if f["employee"] == "Zed")
    assert f"right {bt['hits']} of {bt['flagged']} times on your last {bt['weeks']} drafts" in zed["text"]
    assert zed["calibration_note"] in zed["text"] and zed["base_rate"] == bt["base_rate"]


def test_held_out_hit_rate_on_the_edit_history(db):
    rid = _restaurant(db)
    _edit_history(db, rid, 6)
    bt = sl.edit_prediction_backtest(sl.prediction_weeks(rid, db_path=db))
    assert bt["weeks"] == 6 and bt["skipped"] == 0
    assert bt["hit_rate"] == 1.0, bt          # every flagged row was changed
    assert bt["recall"] == 0.8, bt            # the one-off removal each week is not predictable
    assert sl.edit_prediction_summary(rid, db_path=db)["backtest"]["hit_rate"] == 1.0


def test_held_out_hit_rate_on_a_noisy_manager_beats_chance_by_far():
    """A simulated manager whose edits depend on the person, the slot and
    who wrote the row, plus a 5% background rate: the predictor, fitted on
    the other weeks, is right about most rows it flags. (Across seeds 11-15
    the hit rate ran 61-82% against a 18-23% base rate.)"""
    rng = random.Random(11)
    people = ("Ana", "Ben", "Cy", "Dee", "Eve", "Flo", "Gia", "Zed")
    weeks = []
    for i in range(10):
        w = _week(i)
        rows, edited = [], []
        for n in people:
            for d in range(7):
                if rng.random() < 0.55:
                    part = rng.choice(("morning", "night"))
                    fill = rng.random() < 0.12
                    r = _row(w[d], n, *(("10:00am", "3:00pm") if part == "morning" else ("4:00pm", "10:00pm")),
                             notes="added — role floor" if fill else "")
                    p = 0.05
                    if n == "Zed":
                        p = 0.75
                    if fill:
                        p = max(p, 0.7)
                    if d == 5 and part == "night":
                        p = max(p, 0.4)
                    rows.append(r)
                    edited.append(rng.random() < p)
        weeks.append({"history_id": i, "week_start": w[0], "rows": rows, "edited": edited})
    bt = sl.edit_prediction_backtest(weeks)
    assert bt["weeks"] == 10 and bt["flagged"] >= 10
    # Measured: 49 of 60 flagged rows changed (82%), against 21.5% of all rows.
    assert bt["hit_rate"] >= 0.75 and bt["hit_rate"] >= 3 * bt["base_rate"], bt


def test_likely_edits_adds_predictions_after_the_repeated_edit_matches(monkeypatch):
    import schedule_engine as se
    sat = "2026-10-10"
    rows = [_row(sat, "Ann"), _row(sat, "Bob")]
    monkeypatch.setattr(sl, "predict_row_edits", lambda rid, rows, db_path=None, model=None: [
        {"index": 0, "kind": "predicted", "employee": "Ann", "date": sat, "likelihood": 0.8, "reason": "x", "text": "t"},
        {"index": 1, "kind": "predicted", "employee": "Bob", "date": sat, "likelihood": 0.7, "reason": "y", "text": "u"}])
    pats = [{"kind": "moved_off", "employee": "Ann", "day": "Saturday", "daypart": "night", "times": 3}]
    out = se.likely_edits(1, rows, patterns=pats)
    assert [o["kind"] for o in out] == ["moved_off", "predicted"], "Ann is flagged once, by the pattern"
    assert out[1]["employee"] == "Bob" and out[1]["likelihood"] == 0.7


# ══ #43 — borrowed starting headcount ═════════════════════════════════════

def test_role_families_cross_restaurants_not_role_names():
    assert staffing.family_of("Server") == "server" and staffing.family_of("Lead Server") == "server"
    assert staffing.family_of("Pizza Cook") == "line_cook" and staffing.family_of("Prep Cook") == "prep_cook"
    assert staffing.family_of("Bar Manager") == "manager" and staffing.family_of("Barback") == "barback"
    assert staffing.family_of("Bartender") == "bartender" and staffing.family_of("Dishwasher") == "dish"
    assert staffing.family_of("") is None


def _sales(db, rid, days, amount=2000.0, start=None):
    start = start or (dt.date.today() - dt.timedelta(days=days))
    c = models.get_conn(db)
    for i in range(days):
        d = start + dt.timedelta(days=i)
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, total_hours, labor_pct) "
                  "VALUES (?,?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), amount, 40, 28))
    c.commit()
    c.close()


def test_own_staffing_ratios_are_people_per_1k_of_sales(db):
    rid = _restaurant(db)
    _sales(db, rid, 10, amount=2000.0, start=dt.date(2026, 9, 1))
    shifts = []
    for i in range(10):
        d = (dt.date(2026, 9, 1) + dt.timedelta(days=i)).isoformat()
        shifts += [_row(d, "Ana"), _row(d, "Ben"), _row(d, "Cy", "10:00am", "3:00pm", role="Line Cook")]
    out = staffing.compute_ratios(rid, today=dt.date(2026, 9, 20), db_path=db, shifts=shifts)
    assert out == {"staff_per_1k.server.night": 1.0, "staff_per_1k.line_cook.morning": 0.5}
    assert privacy.assert_anonymous(out) == out
    assert staffing.compute_ratios(rid, today=dt.date(2026, 9, 20), db_path=db, shifts=shifts[:6]) == {}, "under 7 days"


def _cohort_rows(db, n, cohort="pizza", ratio=0.5):
    """n restaurants' feature rows carrying a server-at-dinner ratio."""
    week = features.iso_week(dt.date.today())
    ids = []
    for k in range(n):
        r = _restaurant(db, name=f"Peer {k}")
        ids.append(r)
        features.store(r, {"staff_per_1k.server.night": ratio + 0.01 * k}, week=week, db_path=db)
    return {r: cohort for r in ids}


def test_staffing_ratios_are_benchmarked_only_over_the_cohort_floor(db):
    cohorts = _cohort_rows(db, privacy.MIN_COHORT - 1)
    benchmarks.compute(db_path=db, cohorts=cohorts)
    assert benchmarks.band("pizza", "staff_per_1k.server.night", db_path=db) is None
    more = _cohort_rows(db, 1)
    benchmarks.compute(db_path=db, cohorts={**cohorts, **more})
    b = benchmarks.band("pizza", "staff_per_1k.server.night", db_path=db)
    assert b["n"] == privacy.MIN_COHORT and 0.5 <= b["p50"] <= 0.55


def _band(db, n, p50=0.5, metric="staff_per_1k.server.night"):
    c = models.get_conn(db)
    c.execute("INSERT INTO intel_benchmarks (cohort, metric, week, n, p25, p50, p75, mean) VALUES ('pizza',?,?,?,?,?,?,?)",
              (metric, features.iso_week(dt.date.today()), n, p50 - 0.1, p50, p50 + 0.1, p50))
    c.commit()
    c.close()


def test_a_new_restaurant_borrows_a_starting_headcount_from_its_cohort(db):
    rid = _restaurant(db, name="Tony's Pizzeria", category="pizza")
    _sales(db, rid, 14, amount=8000.0)
    _band(db, privacy.MIN_COHORT)
    out = staffing.starting_headcount(rid, roster_roles={"Ana": "Server", "Ben": "Server", "Cy": "Line Cook"},
                                      shifts=[], db_path=db)
    assert out["available"] and out["borrowed"] and out["n"] == privacy.MIN_COHORT
    assert out["headcount"][("Saturday", "night")] == {"Server": 4}, "0.5 per $1k × $8,000 of its OWN sales"
    assert out["ratios"] == [{"role_family": "server", "daypart": "night", "people_per_1k": 0.5, "n": 5,
                              "week": features.iso_week(dt.date.today())}]
    assert "Borrowed" in out["note"] and "5+ pizza restaurants" in out["note"]
    shaped = staffing.payload(out)
    assert privacy.assert_anonymous(shaped) is shaped
    assert shaped["headcount"]["Saturday|night"] == {"Server": 4}
    json.dumps(shaped)


def test_below_the_floor_nothing_is_borrowed_and_the_reason_is_said(db):
    rid = _restaurant(db, name="Tony's Pizzeria", category="pizza")
    _sales(db, rid, 14, amount=8000.0)
    _band(db, privacy.MIN_COHORT - 1)
    out = staffing.starting_headcount(rid, roster_roles={"Ana": "Server"}, shifts=[], db_path=db)
    assert out["available"] is False and "headcount" not in out
    assert out["reason"].startswith("Fewer than 5 similar restaurants (pizza)")


def test_no_category_no_sales_or_own_history_borrows_nothing(db):
    rid = _restaurant(db, name="Plain Name")
    _band(db, 6)
    assert "No restaurant type is set" in staffing.starting_headcount(rid, roster_roles={"A": "Server"}, shifts=[], db_path=db)["reason"]
    rid2 = _restaurant(db, name="Pie Shop", category="pizza")
    out = staffing.starting_headcount(rid2, roster_roles={"A": "Server"}, shifts=[], db_path=db)
    assert out["available"] is False and "no sales of its own" in out["reason"]
    rid3 = _restaurant(db, name="Old Pizza", category="pizza")
    _sales(db, rid3, 14, amount=8000.0)
    _history(db, rid3, "2026-09-07", published=True)
    out = staffing.starting_headcount(rid3, roster_roles={"A": "Server"}, shifts=[], db_path=db)
    assert out["available"] is False and out["own_history"] is True


def test_borrowed_figures_fill_only_gaps_and_say_so_in_the_requirements():
    own = {("Saturday", "night"): {"Server": 5}}
    merged, marks = staffing.merge_into_typical(own, {("Saturday", "night"): {"Server": 3, "Line Cook": 2},
                                                      ("Monday", "night"): {"Server": 2}})
    assert merged[("Saturday", "night")] == {"Server": 5, "Line Cook": 2}, "its own number stands"
    assert marks == {("Saturday", "night"): {"line cook"}, ("Monday", "night"): {"server"}}
    rows = req.shift_requirements(["2026-10-10", "2026-10-12"], typical_headcount=merged, borrowed=marks)
    block = req.requirements_block(rows)
    assert "Line Cook 2 (borrowed)" in block and "Server 5 (borrowed)" not in block and "Server 2 (borrowed)" in block


# ══ #46 — a multi-week rotation ═══════════════════════════════════════════

ROT_WEEKS = [_week(i, dt.date(2026, 8, 31)) for i in range(4)]


def _rotation_world(db, rid):
    for i, w in enumerate(ROT_WEEKS):
        rows = [_row(w[1], "Ana", "5:00pm", "11:00pm"), _row(w[5], "Ana", "5:00pm", "11:00pm"),
                _row(w[1], "Ben", "5:00pm", "9:00pm"), _row(w[5] if i else w[2], "Ben", "5:00pm", "9:00pm"),
                _row(w[2], "Cara", "5:00pm", "9:00pm"), _row(w[3], "Cara", "5:00pm", "9:00pm"),
                _row(w[0], "Dan", "11:00am", "3:00pm"), _row(w[1], "Dan", "11:00am", "3:00pm"),
                _row(w[2], "Dan", "5:00pm", "9:00pm")]
        if i == 0:
            rows.append(_row(w[5], "Cara", "5:00pm", "9:00pm"))
        hid = models.save_schedule_history(rid, w[0], w[6], 0, 0, 30, _csv(rows), [], db_path=db)
        c = models.get_conn(db)
        c.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
        c.commit()
        c.close()


def test_the_rotation_plans_who_is_next_for_a_weekend_off_and_a_close(db):
    rid = _restaurant(db)
    _rotation_world(db, rid)
    plan = si.rotation_plan(rid, today=dt.date(2026, 9, 28), db_path=db)
    servers = plan["roles"]["Server"]
    assert plan["weeks"] == 4
    assert servers["weekend_off"][:2] == ["Ana", "Ben"] and servers["weekend_streak"] == {"Ana": 4, "Ben": 3}
    assert servers["weekend_due"] == ["Ana"], "one a week in a four-person role"
    assert servers["rest_from_close"] == ["Ana"] and servers["next_close"][:3] == ["Ben", "Cara", "Dan"]
    assert "Servers — next weekend off: Ana (4 in a row), then Ben; next close: Ben, then Cara, Dan; " \
           "rest from closing: Ana." in plan["lines"]
    block = si.rotation_block(plan)
    assert "ROTATION PLAN" in block and "Ana (4 weekends in a row)" in block
    # A roster without Ana plans without her.
    assert "Ana" not in si.rotation_plan(rid, today=dt.date(2026, 9, 28), db_path=db,
                                         roster_roles={"Ben": "Server", "Cara": "Server", "Dan": "Server"}).get(
        "roles", {}).get("Server", {}).get("people", [])


def test_the_rotation_needs_two_published_weeks(db):
    rid = _restaurant(db)
    _rotation_world(db, rid)
    assert si.rotation_plan(rid, today=dt.date(2026, 9, 12), weeks=1, db_path=db) == {}


def _next_week(ana_on_saturday):
    w = _week(0, dt.date(2026, 9, 28))
    rows = [_row(w[1], "Ana", "5:00pm", "11:00pm"), _row(w[2], "Cara", "5:00pm", "9:00pm"),
            _row(w[1], "Ben", "5:00pm", "9:00pm"), _row(w[5], "Ben", "5:00pm", "9:00pm"),
            _row(w[0], "Dan", "11:00am", "3:00pm"), _row(w[2], "Dan", "5:00pm", "9:00pm")]
    rows.append(_row(w[5], "Ana" if ana_on_saturday else "Cara", "5:00pm", "11:00pm"))
    return rows


def _saturday_fairness(rows, plan):
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile()], rotation=plan,
                        typical_headcount={("Saturday", "night"): {"Server": 2}})
    sat = next(s for s in out["shifts"] if s["day"] == "Saturday" and s["daypart"] == "night")
    return next((d for d in sat["dimensions"] if d["key"] == "fairness"), None)


def test_fairness_judges_the_week_against_the_rotation(db):
    rid = _restaurant(db)
    _rotation_world(db, rid)
    plan = si.rotation_plan(rid, today=dt.date(2026, 9, 28), db_path=db)
    against = _saturday_fairness(_next_week(ana_on_saturday=True), plan)
    assert any("Ana is due a weekend off (4 weekends in a row) and is on Saturday dinner; Cara has this weekend free."
               == w for w in against["weaknesses"]), against
    follows = _saturday_fairness(_next_week(ana_on_saturday=False), plan)
    assert follows["score"] > against["score"]
    assert any("Ana gets the weekend off the rotation said was due." == s for s in follows["strengths"])
    # Without a plan, the week alone decides — as before.
    alone = _saturday_fairness(_next_week(ana_on_saturday=True), {})
    assert not any("due a weekend off" in w for w in (alone or {}).get("weaknesses", []))


# ══ #48 — sales per labor hour as an objective ════════════════════════════

SPLH = {wd: {"morning": {"sales": 1000.0, "hours": 10.0, "splh": 100.0},
             "night": {"sales": 3000.0, "hours": 50.0, "splh": 60.0}}
        for wd in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")}


def test_the_target_is_the_own_record_raised_only_to_meet_the_labor_target(db):
    rid = _restaurant(db)
    c = models.get_conn(db)
    for i in range(14):
        d = dt.date.today() - dt.timedelta(days=i + 1)
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, total_hours, labor_pct) "
                  "VALUES (?,?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), 4000, 60, 33))
    c.commit()
    c.close()
    over = econ.splh_objective(rid, splh=SPLH, labor_target_pct=30, db_path=db)
    assert over["available"] and over["scale"] == 1.1, "33% run against a 30% target"
    assert over["by_day"]["Saturday"] == {"morning": 110.0, "night": 66.0}
    assert "raised 10% to meet your 30% labor target (you have run 33.0%)" in over["targets"]["night"]["source"]
    under = econ.splh_objective(rid, splh=SPLH, labor_target_pct=35, db_path=db)
    assert under["scale"] == 1.0 and under["by_day"]["Saturday"]["night"] == 60.0, "never more hours than the record ran"
    assert econ.splh_objective(rid, splh={}, db_path=db)["available"] is False


def _obj():
    return {"available": True, "by_day": {wd: {"morning": 100.0, "night": 60.0} for wd in SPLH},
            "targets": {"morning": {"target": 100.0}, "night": {"target": 60.0}},
            "daypart_sales": {wd: {"morning": 1000.0, "night": 3000.0} for wd in SPLH}, "basis": "your own pace"}


def test_the_prompt_is_told_the_hours_each_shift_carries_at_its_target():
    block = econ.splh_objective_block(_obj(), ["2026-10-10"], {"2026-10-10": {"lift_pct": 20}})
    assert "SALES PER LABOR HOUR" in block and "Sat 10/10/26: lunch $100/labor-hour ≈ 12h, dinner $60/labor-hour ≈ 60h" in block


def _splh_score(rows, obj=None):
    obj = obj if obj is not None else _obj()
    targets = {wd: {p: econ.splh_target_for(obj, wd, p) for p in ("morning", "night")} for wd in SPLH} if obj else {}
    return sq.score_rows(rows, profiles=[sq.ShiftProfile()], typical_headcount={("Saturday", "night"): {"Server": 2}},
                         splh_targets=targets, daypart_sales=(obj or {}).get("daypart_sales") or {})


def test_splh_is_scored_per_shift_and_withdraws_without_sales():
    sat = "2026-10-10"
    lean = [_row(sat, n, "4:00pm", "10:00pm", hours=6) for n in ("A", "B", "C", "D", "E")]         # 30h: $100/h
    heavy = lean + [_row(sat, n, "4:00pm", "10:00pm", hours=6) for n in ("F", "G", "H", "I", "J")]  # 60h: $50/h
    dim = lambda out: next(d for d in out["shifts"][0]["dimensions"] if d["key"] == "splh")
    good, bad = dim(_splh_score(lean)), dim(_splh_score(heavy))
    assert good["score"] == 100 and good["facts"]["splh"] == 100
    assert bad["facts"] == {"splh": 50.0, "target": 60.0, "expected_sales": 3000.0, "hours": 60.0,
                            "hours_at_target": 50.0, "ratio": 0.83}
    assert bad["score"] == 77 and "about 10h more than the usual sales here carry" in bad["weaknesses"][0]
    none = _splh_score(lean, obj={})
    assert "splh" in none["shifts"][0]["not_applicable"], "no sales record: the dimension steps aside"


def test_the_review_reports_the_draft_against_the_objective():
    sat = "2026-10-10"
    rows = [_row(sat, n, "4:00pm", "10:00pm", hours=6) for n in ("A", "B", "C", "D", "E", "F", "G", "H", "I", "J")]
    rep = econ.splh_report(_obj(), rows)
    assert rep["by_daypart"]["night"] == {"splh": 50.0, "target": 60.0, "hours": 60.0, "hours_at_target": 50.0}
    assert rep["line"] == ("Sales per labor hour across the week: dinner $50 against $60. Furthest under: Sat 10/10/26 "
                           "dinner at $50 against $60, about 10h more than its usual sales carry.")
    assert econ.splh_report({"available": False}, rows) == {}


def test_splh_weight_sits_beside_labor_efficiency_not_over_the_operational_dimensions():
    w = sq.DEFAULT_WEIGHTS
    assert w["splh"] < w["labor_efficiency"] and w["splh"] < w["coverage"]
    assert "splh" in sq.DIMENSIONS and "splh" in sq.CUSTOMER_DIMENSIONS


# ══ surfaces: web and iOS agree ══════════════════════════════════════════

def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_the_record_panel_shows_the_learning_on_web_and_ios():
    import strategy_routes
    src = _read(strategy_routes.__file__)
    for key in ('"edit_prediction"', '"rotation"', '"splh_objective"', '"starting_points"'):
        assert key in src, key
    html = _read("templates/dashboard.html")
    for piece in ("d.edit_prediction", "d.starting_points", "d.splh_objective", "d.rotation", "dm.explanation",
                  "Borrowed starting headcount", "Up next:"):
        assert piece in html, piece
    swift = _read("ios/CavnarAI/CavnarAI/Features/Labor/ScheduleIntelSection.swift")
    for piece in ("editPredictionBlock(", "startingPointsBlock(", "rotationPlanBlock(", "d.explanation",
                  "Borrowed starting headcount", "objective: intel.splhObjective"):
        assert piece in swift, piece
    model = _read("ios/CavnarAI/CavnarAI/Features/Labor/ScheduleSetupViewModel.swift")
    for piece in ('"edit_prediction"', '"splh_objective"', '"starting_points"', "revenue, rotation", 'case stepPct = "step_pct"'):
        assert piece in model, piece


def test_likely_edits_show_in_the_review_on_web_and_ios():
    html = _read("templates/dashboard.html")
    assert "likely_edits: data.likely_edits" in html and "Likely edits" in html and "le.likelihood" in html
    panel = _read("ios/CavnarAI/CavnarAI/Features/Labor/ScheduleReviewPanel.swift")
    assert "likelyEditsBlock" in panel and "LIKELY EDITS" in panel
    assert 'case likelyEdits = "likely_edits"' in _read("ios/CavnarAI/CavnarAI/Features/Labor/LaborViewModel.swift")


def test_the_repair_loop_gives_the_due_weekend_off(db):
    import schedule_optimizer as so
    rid = _restaurant(db)
    _rotation_world(db, rid)
    plan = si.rotation_plan(rid, today=dt.date(2026, 9, 28), db_path=db)
    sig = {"roster": ["Ana", "Ben", "Cara", "Dan"], "availability": {}, "constraints": {}, "scores": {},
           "typical_headcount": {("Saturday", "night"): {"Server": 2}}, "rotation": plan}
    res = so.optimize(_next_week(ana_on_saturday=True), {"roster_roles": {n: "Server" for n in sig["roster"]}},
                      signals=sig, target=100)
    sat = "2026-10-03"
    assert "Ana" not in {r["employee"] for r in res["rows"] if r["date"] == sat}, res["changes"]
    assert any("Ana was due a weekend off" in c["reason"] for c in res["changes"])


def test_the_repair_loop_trims_a_shift_carrying_more_hours_than_its_sales():
    import schedule_optimizer as so
    sat, fri = "2026-10-10", "2026-10-09"
    names = ("Ann", "Bob", "Cat", "Dee", "Eve", "Flo")
    rows = [_row(sat, n, "5:00pm", "11:00pm", hours=6) for n in names] + [_row(fri, n, "5:00pm", "11:00pm", hours=6)
                                                                          for n in names]
    obj = _obj()
    sig = {"roster": list(names), "typical_headcount": {("Saturday", "night"): {"Server": 3},
                                                        ("Friday", "night"): {"Server": 6}},
           "availability": {}, "constraints": {}, "scores": {},
           "splh_targets": {wd: {p: econ.splh_target_for(obj, wd, p) for p in ("morning", "night")} for wd in SPLH},
           "daypart_sales": {"Saturday": {"night": 1800.0}, "Friday": {"night": 3600.0}}}
    res = so.optimize(rows, {}, signals=sig, target=100)
    assert res["after_score"] > res["before_score"]
    assert any("usual sales" in c["reason"] for c in res["changes"]), res["changes"]
    assert sum(1 for r in res["rows"] if r["date"] == sat) >= 3, "never below the crew the shift needs"


# ══ wiring: what generation and the scorer are handed ═════════════════════

def test_generation_gets_the_rotation_the_objective_and_a_borrowed_start_only_when_there_is_one(monkeypatch):
    import schedule_engine as se
    monkeypatch.setattr(si, "rotation_plan", lambda rid, roster_roles=None, **k: {"weeks": 3, "roles": {"Server": {}},
                                                                                  "roster": sorted(roster_roles)})
    monkeypatch.setattr(econ, "splh_objective", lambda rid, splh=None, labor_target_pct=None, **k: {
        "available": True, "labor_target_pct": labor_target_pct})
    monkeypatch.setattr(staffing, "starting_headcount", lambda rid, **k: {
        "available": True, "headcount": {("Monday", "night"): {"Server": 2}}, "note": "Borrowed: the median of 5+ x."})
    out = se.schedule_learning_inputs(9, None, [("Ana", "Server")], [], {}, 28)
    assert out["rotation"]["roster"] == ["Ana"] and out["splh_objective"]["labor_target_pct"] == 28
    assert out["borrowed_headcount"] == {("Monday", "night"): {"Server": 2}}
    assert out["starting_payload"]["headcount"] == {"Monday|night": {"Server": 2}}
    assert "BORROWED STARTING HEADCOUNT" in out["starting_block"]
    monkeypatch.setattr(staffing, "starting_headcount", lambda rid, **k: {"available": False, "reason": "why"})
    none = se.schedule_learning_inputs(9, None, [("Ana", "Server")], [], {}, 28)
    assert none["borrowed_headcount"] is None and none["starting_block"] == ""
    assert none["starting_payload"] == {"available": False, "reason": "why"}


def test_the_scorer_reads_the_generations_own_rotation_and_targets(monkeypatch):
    import schedule_engine as se
    monkeypatch.setattr(si, "rotation_plan", lambda *a, **k: pytest.fail("the generation's plan is used"))
    sig = se._learning_signals(9, {"rotation_plan": {"roles": {"Server": {}}}, "splh_objective": _obj()})
    assert sig["rotation"] == {"roles": {"Server": {}}}
    assert sig["splh_targets"]["Saturday"] == {"morning": 100.0, "night": 60.0}
    assert sig["daypart_sales"]["Saturday"]["night"] == 3000.0
    empty = se._learning_signals(9, {"rotation_plan": {}, "splh_objective": {"available": False}})
    assert empty == {"rotation": {}, "splh_targets": {}, "daypart_sales": {}}
