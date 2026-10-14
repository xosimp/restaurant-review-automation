"""Workstream K of the 10/11/26 schedule fix round: measurement honesty.

The score must condemn 12-hour doubles, see a missing role, and not rise as
it knows less; savings are scheduled against scheduled and never reward a
thin week; the forecast's record says whether it beats a naive guess; the
schedule states the forecast it was sized on beside the one Home shows; and
the budget line says which budget binds.
"""
import json
from datetime import date, timedelta

import models
import prepublish
import schedule_economics as E
import shift_quality as sq
from tests.test_ios_parity_labor import _rid, db  # noqa: F401

FRI = "2026-10-23"


def _r(date_, emp, role, start="5:00pm", end="10:00pm"):
    return {"date": date_, "day": sq._day_name(date_), "employee": emp, "role": role,
            "shift_start": start, "shift_end": end, "notes": ""}


FRIDAY_CREW = {"Server": 7, "Cook": 4, "Dishwasher": 2, "Bartender": 2, "Host": 2}


def _friday(roles: dict) -> list:
    out = []
    for role, n in roles.items():
        out += [_r(FRI, f"{role}{i}", role) for i in range(n)]
    return out


# ── LOGIC-14: coverage sees a role with nobody on it ────────────────────────

def test_friday_dinner_with_no_dish_and_no_host_is_capped_without_a_curve():
    rows = _friday({"Server": 7, "Cook": 4, "Bartender": 2})
    out = sq.score_rows(rows, typical_headcount={("Friday", "night"): FRIDAY_CREW})
    shift = next(s for s in out["shifts"] if s["date"] == FRI and s["daypart"] == "night")
    cov = next(d for d in shift["dimensions"] if d["key"] == "coverage")
    # Role-blind it read 13/17 = 76 — over the 70 floor, "mostly covered".
    assert cov["score"] == sq.role_absent_cap(sq.role_criticality("Dishwasher")) == 44
    assert shift["capped_by"] == "coverage" and shift["score"] == 44
    assert set(cov["facts"]["absent_roles"]) == {"Dishwasher", "Host"}
    assert shift["weaknesses"][0].startswith("No dishwasher and no host on this shift")


def test_coverage_weighs_roles_by_criticality():
    short_cook = _friday({"Server": 7, "Cook": 3, "Dishwasher": 2, "Bartender": 2, "Host": 2})
    short_host = _friday({"Server": 7, "Cook": 4, "Dishwasher": 2, "Bartender": 2, "Host": 1})
    th = {("Friday", "night"): FRIDAY_CREW}

    def cov(rows):
        s = sq.score_rows(rows, typical_headcount=th)["shifts"][0]
        return next(d for d in s["dimensions"] if d["key"] == "coverage")["score"]
    # One head short either way: a cook costs more than a host.
    assert cov(short_cook) < cov(short_host) < 100


def test_a_restaurants_own_criticality_wins():
    assert sq.role_criticality("Host", overrides={"host": 1.6}) == 1.6
    assert sq.role_criticality("Dishwasher") == 1.4
    assert sq.role_criticality("Expo") == 0.9
    assert sq.role_criticality("Sommelier") == 1.0
    rows = _friday({"Server": 7, "Cook": 4, "Dishwasher": 2, "Bartender": 2})
    out = sq.score_rows(rows, typical_headcount={("Friday", "night"): FRIDAY_CREW},
                        role_criticality={"host": 1.6})
    cov = next(d for d in out["shifts"][0]["dimensions"] if d["key"] == "coverage")
    assert cov["score"] == sq.role_absent_cap(1.6) == 36


# ── BUGSGEN-22: the budget line never contradicts itself ────────────────────

def test_dollars_over_with_hours_at_budget_says_the_dollar_budget_binds():
    b = E.binding_budget(14390, 14092, 988, 988, overtime_premium=0)
    assert b["binds"] == "dollars"
    assert "988h scheduled against 988h" not in b["note"]
    assert "the hours are within budget" in b["note"]
    assert "the dollar budget is the one that binds" in b["note"]
    # The mix is paid above the blended rate the hours budget assumes.
    assert b["assumed_rate"] == round(14092 / 988, 2)
    assert b["paid_rate"] == round(14390 / 988, 2)
    assert f"${b['paid_rate']:,.2f}/h" in b["note"] and f"${b['assumed_rate']:,.2f}/h" in b["note"]


def test_overtime_premium_is_named_when_it_drives_the_overrun():
    b = E.binding_budget(10300, 10000, 700, 710, overtime_premium=280)
    assert b["binds"] == "dollars"
    assert "$280 of it is overtime premium" in b["note"]


def test_both_over_keeps_the_hours_beside_the_dollars():
    b = E.binding_budget(15000, 14000, 1050, 988)
    assert b["binds"] == "both"
    assert b["note"] == "1,050h scheduled against 988h"


def test_under_budget_is_not_a_binding_case():
    assert E.binding_budget(9000, 10000, 700, 700)["binds"] is None


def test_trim_conflict_line_names_the_binding_budget():
    line = E.budget_conflict_line({"over_by_dollars": 298, "budget_dollars": 14092, "scheduled": 988,
                                   "budget": 988, "held": {"rule": 1},
                                   "examples": [{"reason": "rule", "label": "manager on every shift"}]})
    assert "988h against 988h" not in line
    assert "the dollar budget is the one that binds" in line


# ── STRESS-14: the score condemns doubles the restaurant never works ────────

WEEK = [(date(2026, 10, 19) + timedelta(days=i)).isoformat() for i in range(7)]


def _crew_week(start, end, people=("Ann", "Ben", "Cal", "Dee", "Eve", "Fay"), role="Server", days=5):
    out = []
    for i, n in enumerate(people):
        for d in WEEK[(i % 2):(i % 2) + days]:
            out.append(dict(_r(d, n, role, start, end), scheduled_hours=str(
                sq._stime.span({"shift_start": start, "shift_end": end, "date": d})[1] / 60.0
                - sq._stime.span({"shift_start": start, "shift_end": end, "date": d})[0] / 60.0)))
    return out


TYPICAL_WEEK = {(sq._day_name(d), p): {"Server": 2} for d in WEEK for p in ("morning", "night")}


def test_a_week_of_twelve_hour_doubles_is_held_weak():
    normal = sq.score_rows(_crew_week("4:00pm", "10:00pm") + _crew_week("11:00am", "4:00pm", people=("Gus", "Hal")),
                           typical_headcount=TYPICAL_WEEK, max_shift_hours=12)
    doubles = sq.score_rows(_crew_week("11:00am", "11:00pm"), typical_headcount=TYPICAL_WEEK, max_shift_hours=12)
    sl = next(d for d in doubles["week_dimensions"] if d["key"] == "shift_length")
    assert sl["facts"]["long_days"] == sl["facts"]["days"] and sl["score"] == 0
    assert doubles["held_by"]["key"] == "shift_length"
    assert doubles["score"] < sq.SHIFT_LENGTH_FLOOR and doubles["band"] == "weak"
    assert "run longer than 10 hours" in doubles["held_by"]["text"]
    sl_n = next(d for d in normal["week_dimensions"] if d["key"] == "shift_length")
    assert sl_n["score"] == 100 and normal["held_by"] is None


def test_doubles_the_restaurant_works_are_its_norm():
    hist = []
    for i in range(30):
        d = (date(2026, 9, 1) + timedelta(days=i)).isoformat()
        hist.append({"employee": f"P{i % 6}", "date": d, "role": "Server", "shift_start": "11:00am",
                     "shift_end": "11:00pm", "actual_hours": 12})
    norms = sq.length_norms(hist)
    assert norms["server"]["p90"] == 12 and norms["server"]["days"] == 30
    out = sq.score_rows(_crew_week("11:00am", "11:00pm"), typical_headcount=TYPICAL_WEEK, max_shift_hours=12,
                        length_norms=norms)
    sl = next(d for d in out["week_dimensions"] if d["key"] == "shift_length")
    assert sl["score"] == 100 and sl["facts"]["limits"]["server"]["source"] == "yours"
    # Fewer than LENGTH_MIN_DAYS days of history is no norm.
    assert sq.length_norms(hist[:5]) == {}


def test_managers_days_are_not_scored_but_their_load_past_their_own_usual_is_said():
    rows = _crew_week("4:00pm", "10:00pm") + [
        dict(_r(d, "Mia", "Manager", "8:00am", "11:00pm"), scheduled_hours="15") for d in WEEK]
    ledger = {"Mia": [{"week": w, "hours": 45.0, "slots": [], "role": "Manager"}
                      for w in ("2026-09-28", "2026-10-05", "2026-10-12")]}
    out = sq.score_rows(rows, typical_headcount=TYPICAL_WEEK, max_shift_hours=12, managers={"mia": "Manager"},
                        max_shift_exempt=["mia"], hours_exempt=["mia"], load_ledger=ledger)
    sl = next(d for d in out["week_dimensions"] if d["key"] == "shift_length")
    assert sl["score"] == 100                                    # her 15h days are hers
    load = sl["facts"]["manager_load"]
    assert load and load[0]["name"] == "Mia" and load[0]["hours"] == 105 and load[0]["usual"] == 45
    assert any("not scored" in w and "Mia" in w for w in sl["weaknesses"])
    # The owner is never read this way.
    own = sq.score_rows(rows, typical_headcount=TYPICAL_WEEK, max_shift_hours=12, managers={"mia": "Owner"},
                        max_shift_exempt=["mia"], hours_exempt=["mia"], load_ledger=ledger)
    sl2 = next(d for d in own["week_dimensions"] if d["key"] == "shift_length")
    assert sl2["facts"]["manager_load"] == []


# ── TRUST-31 / STRESS-14: the score against how the restaurant's weeks ran ──

def _scored_weeks(db, rid, weeks):
    """weeks: [(quality_score, issues on each of 7 nights, unchanged_share)] —
    returns the acceptance weeks to stand in for schedule_versions."""
    acc, saved = [], []
    for i, (score, issues, kept) in enumerate(weeks):
        ws = (date(2026, 6, 1) + timedelta(days=7 * i))
        saved.append((ws, models.save_schedule_history(
            rid, ws.isoformat(), (ws + timedelta(days=6)).isoformat(), 10, 0, 30,
            "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n", [], db_path=db)))
    conn = models.get_conn()
    for (score, issues, kept), (ws, hid) in zip(weeks, saved):
        conn.execute("UPDATE schedule_history SET quality_score=? WHERE id=?", (score, hid))
        for k in range(7):
            conn.execute("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, issues) "
                         "VALUES (?,?,?,?,?)", (rid, hid, (ws + timedelta(days=k)).isoformat(), "night",
                                                issues if k == 0 else 0))
        acc.append({"history_id": hid, "week_start": ws.isoformat(), "unchanged_share": kept})
    conn.commit()
    conn.close()
    return acc


def test_the_score_is_shown_against_how_the_weeks_ran(db, monkeypatch):
    import schedule_calibration as cal
    import schedule_intel
    import schedule_versions as sv
    rid = _rid(db)
    acc = _scored_weeks(db, rid, [(85, 1, 0.9)] * 3 + [(55, 4, 0.6)] * 3 + [(70, 9, 0.1)])
    monkeypatch.setattr(sv, "acceptance", lambda r, weeks=26, **k: {"weeks": acc})
    monkeypatch.setattr(schedule_intel, "watched_dates",
                        lambda r, a, b, **k: {(date(2026, 6, 1) + timedelta(days=i)).isoformat() for i in range(60)})
    link = cal.score_outcome_link(rid)
    assert link["ready"] and link["predicts"] is True
    assert link["high"]["weeks"] == 3 and link["low"]["weeks"] == 3          # the 70 sits between
    assert link["high"]["issues_per_week"] == 1 and link["low"]["issues_per_week"] == 4
    assert link["text"] == ("Your weeks scoring 78+: 1 coverage or no-show issues a week and 10% of shifts "
                            "changed before sending; under 65: 4 and 40% (over 3 and 3 weeks).")


def test_below_the_floor_it_is_a_dash_with_the_count(db, monkeypatch):
    import schedule_calibration as cal
    import schedule_intel
    import schedule_versions as sv
    rid = _rid(db)
    acc = _scored_weeks(db, rid, [(85, 1, 0.9)] * 3 + [(55, 4, 0.6)] * 2)
    monkeypatch.setattr(sv, "acceptance", lambda r, weeks=26, **k: {"weeks": acc})
    monkeypatch.setattr(schedule_intel, "watched_dates", lambda r, a, b, **k: set())
    link = cal.score_outcome_link(rid)
    assert not link["ready"] and link["text"] is None
    assert "(3 and 2 so far)" in link["note"]


def test_a_score_that_does_not_track_the_weeks_says_so(db, monkeypatch):
    import schedule_calibration as cal
    import schedule_intel
    import schedule_versions as sv
    rid = _rid(db)
    acc = _scored_weeks(db, rid, [(90, 5, 0.5)] * 3 + [(50, 1, 0.9)] * 3)
    monkeypatch.setattr(sv, "acceptance", lambda r, weeks=26, **k: {"weeks": acc})
    monkeypatch.setattr(schedule_intel, "watched_dates",
                        lambda r, a, b, **k: {(date(2026, 6, 1) + timedelta(days=i)).isoformat() for i in range(60)})
    link = cal.score_outcome_link(rid)
    assert link["predicts"] is False and "have not yet meant better weeks" in link["text"]


def test_the_review_carries_the_link_and_the_measured_share(db, monkeypatch):
    import schedule_calibration as cal
    rid = _rid(db)
    hid = _priced_week(db, rid)
    monkeypatch.setattr(cal, "score_outcome_link", lambda r: {"ready": True, "text": "Your weeks scoring 78+: x."})
    q = prepublish._quality(rid, {"id": hid}, {"checked": True, "score": 92, "band": "fair",
                                              "measured": {"share": 0.3, "text": "Scored on 30% …"}})
    assert q["outcome_text"] == "Your weeks scoring 78+: x." and q["measured_share"] == 0.3
    assert q["measured_text"].startswith("Scored on 30%")


# ── LOGIC-15: knowing less never makes a week look better ───────────────────

def test_the_band_is_held_by_the_share_measured():
    assert sq.measured_band(95, 0.9) == ("excellent", None)
    assert sq.measured_band(95, 0.5) == ("good", "excellent")
    assert sq.measured_band(95, 0.2) == ("fair", "excellent")
    assert sq.measured_band(80, 0.3) == ("fair", "good")
    assert sq.measured_band(70, 0.1) == ("fair", None)        # no floor under fair
    assert sq.measured_band(95, None) == ("excellent", None)


def test_a_thin_data_week_cannot_read_excellent_and_says_why():
    rows = _friday(FRIDAY_CREW)
    out = sq.score_rows(rows, typical_headcount={("Friday", "night"): FRIDAY_CREW})
    assert out["score"] >= 90                                   # the number is what was judged
    m = out["measured"]
    assert m["share"] < sq.BAND_MEASURED_MIN["good"]
    assert out["band"] == "fair" and m["band_capped_from"] == "excellent"
    assert "operational_strength" in m["withdrawn"] and m["withdrawn_weight"] > 0
    assert m["text"].startswith(f"Scored on {int(round(100 * m['share']))}% of what a full score weighs")
    # Confidence says it beside the number, with the points it cost.
    conf = out["confidence"]
    assert any(b["reason"].startswith("The score stands on") for b in conf["breakdown"])
    # Every shift carries its own share.
    assert out["shifts"][0]["measured"]["share"] is not None


def test_measuring_more_raises_the_share_never_the_band_by_itself():
    rows = _friday(FRIDAY_CREW)
    th = {("Friday", "night"): FRIDAY_CREW}
    thin = sq.score_rows(rows, typical_headcount=th)
    rated = sq.score_rows(rows, typical_headcount=th, scores={r["employee"]: 4 for r in rows},
                          tenure={r["employee"]: 40 for r in rows})
    assert rated["measured"]["share"] > thin["measured"]["share"]


# ── TRUST-30: the forecast's record against a naive guess ───────────────────

def _forecast_weeks_with_naive(rid, weeks):
    """weeks: [(predicted, actual, naive_last)]"""
    conn = models.get_conn()
    for i, (p, a, n) in enumerate(weeks):
        end = (date(2026, 1, 4) + timedelta(days=7 * i)).isoformat()
        e = abs(p - a) / a * 100
        conn.execute("INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, actual, error_pct, "
                     "signed_error_pct, naive_last, scored_at) VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
                     (rid, "revenue_week", end, p, a, e, (p - a) / a * 100, n))
    conn.commit()
    conn.close()


def _priced_week(db, rid):
    for name, role in (("Erik", "Manager"), ("Ana", "Server")):
        models.add_manual_team_member(rid, name, role=role, db_path=db)
    csv = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
           "2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,\n2026-10-12,Monday,Ana,Server,4:00pm,10:00pm,6,")
    hid = models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 10, 0, 30, csv, [], db_path=db)
    conn = models.get_conn()
    conn.execute("UPDATE schedule_history SET economics_json=? WHERE id=?",
                 (json.dumps({"projected_revenue": 2000, "labor_budget_dollars": 400, "blended_rate": 20}), hid))
    conn.commit()
    conn.close()
    return hid


def test_a_forecast_worse_than_last_week_withholds_the_range(db, monkeypatch):
    rid = _rid(db)
    # Within 5% every week — but last week's sales were within 1%.
    _forecast_weeks_with_naive(rid, [(10500, 10000, 10050)] * 6)
    rec = prepublish.forecast_record(rid)
    assert rec["beats_last_week"] == 0 and rec["vs_last_week"] == 6
    assert rec["skill"] is not None and rec["skill"] < 0
    assert not prepublish.forecast_has_skill(rec)
    hid = _priced_week(db, rid)
    monkeypatch.setattr(prepublish, "_labor_target", lambda r: 30)
    lp = prepublish.review(rid, hid)["numbers"]["labor_pct"]
    assert lp["low"] is None and lp["high"] is None
    assert lp["track_record"].startswith("Beats last week's sales as a guess on 0 of the last 6 weeks")
    assert "no range" in lp["track_record"]
    assert lp["forecast_skill"]["range_withheld"] is True


def test_a_forecast_that_beats_last_week_says_so_beside_its_range(db, monkeypatch):
    rid = _rid(db)
    _forecast_weeks_with_naive(rid, [(10100, 10000, 11000)] * 5 + [(10000, 10000, 9000)])
    rec = prepublish.forecast_record(rid)
    assert rec["beats_last_week"] == 6 and rec["skill"] > 0
    hid = _priced_week(db, rid)
    monkeypatch.setattr(prepublish, "_labor_target", lambda r: 30)
    lp = prepublish.review(rid, hid)["numbers"]["labor_pct"]
    assert lp["low"] is not None and lp["high"] is not None
    assert "beats last week's sales as a guess on 6 of the last 6 weeks" in lp["track_record"]


def test_no_skill_earns_no_confidence(db, monkeypatch):
    rid = _rid(db)
    monkeypatch.setattr(prepublish, "forecast_record", lambda r: {
        "weeks": 8, "within_pct": 5, "weeks_within": 7, "mean_error_pct": 3.0, "skill": -0.4, "n_skill": 8,
        "beats_last_week": 1, "vs_last_week": 8, "last": None})
    out = prepublish._sure(rid, None, None, {})
    assert out["confidence"]["pct"] == 0
    assert "no better than a naive guess" in out["confidence"]["basis"][0]
