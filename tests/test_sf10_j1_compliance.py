"""Schedule audit 10/10/26, workstream J1 — the compliance engine (RM-15):
MARKET-1 (a draft horizon a notice rule can be met in), MARKET-2 (a priced
predictability-pay ledger), MARKET-3 / LOGIC-21 (rest as a jurisdiction
value with a consent-and-premium path, no global default), MARKET-4
(good-faith estimates, written answers, access to hours, three-year
records), MARKET-5 (a sourced jurisdiction library with coverage; state
minor tables; California premiums), MARKET-6 (breaks placed inside shifts)
and LEARNING-18 (a school calendar with a safe default)."""
import csv
import datetime as dt
import io
import json
from types import SimpleNamespace

import pytest

import test_edge_sched_publish as T
from test_edge_sched_publish import db, save_app  # noqa: F401  (fixtures)

import breaks
import compliance_packs as cp
import fair_workweek as fww
import models
import predictability_pay as pp
import schedule_economics as econ
import schedule_rules as sr
import schedule_versions as sv
import school_calendar as sc
import shift_quality as sq
import shift_requests as srq
import staff_consent

CHICAGO_BIG = {"state": "IL", "city": "Chicago", "employees": 400, "locations": 35, "kind": "full_service"}
EJS = {"state": "IL", "city": "St. Charles", "employees": 45, "locations": 1, "kind": "full_service"}


def _r(profile=None, jurisdiction=None, **kw):
    return SimpleNamespace(id=1, jurisdiction=jurisdiction, aivis_city_json=None, mailing_address=None,
                           compliance_profile_json=json.dumps(profile) if profile else None, **kw)


def _row(d, n, s, e, role="Server"):
    a, b = sr.parse_minutes(s), sr.parse_minutes(e)
    hours = round(((b - a) % (24 * 60)) / 60, 2)
    return {"date": d, "day": dt.date.fromisoformat(d).strftime("%A"), "employee": n, "role": role,
            "shift_start": s, "shift_end": e, "scheduled_hours": str(hours), "notes": ""}


# ── coverage: the library and who it holds (MARKET-5, RM-15) ─────────────────

def test_simple_ejs_is_held_to_illinois_and_told_chicagos_ordinance_does_not_cover_it():
    ev = cp.evaluate(_r(EJS))
    by = {j["code"]: j for j in ev["jurisdictions"]}
    assert ev["applies"] == ["IL"]
    assert by["IL-CHICAGO"]["status"] == "outside" and "St. Charles is not Chicago" in by["IL-CHICAGO"]["why"]
    assert by["IL-EVANSTON"]["status"] == "outside"
    assert cp.rule_values(_r(EJS)) == {"max_consecutive_days": 6}   # ODRISA; no notice, no rest rule
    assert cp.fact(_r(EJS), "meal_breaks")["source"].startswith("https://www.ilga.gov/")


def test_an_illinois_restaurant_outside_chicago_has_no_rest_rule_as_law(db):
    """Illinois law is the One Day Rest in Seven Act and its meal periods; the
    10-hour rest between shifts is Chicago's ordinance, for covered employers
    only. Simple EJ's (St. Charles) is never held to a 10h rest ban."""
    rid = T._restaurant(db)
    models.update_restaurant(rid, {"compliance_profile_json": json.dumps(EJS), "jurisdiction": "IL"})
    comp = sr.compliance(models.get_restaurant(rid))
    assert comp["min_rest_hours"] is None and sr.rest_mode(comp) == "off"
    assert comp["_pack"]["law"] == ["max_consecutive_days"] and comp["max_consecutive_days"] == 6
    c = sr.build_constraints(rid, T.W1, [T._day(d) for d in T.W1])
    assert not sr.law_backed(c, "min_rest_hours") and sr.law_backed(c, "max_consecutive_days")
    assert not [v for v in sr.violations(CLOPEN_CY, c) if v["kind"] in ("rest_gap", "rest_premium")]
    # the same rest rule set by EJ's owner is their policy, not law
    sr.save_compliance(rid, {"min_rest_hours": 10})
    c = sr.build_constraints(rid, T.W1, [T._day(d) for d in T.W1])
    gap = [v for v in sr.violations(CLOPEN_CY, c) if v["kind"] == "rest_gap"]
    assert gap and sr.tier_of(gap[0]) == sr.TIER_POLICY
    # a covered Chicago employer: the ordinance's rest is law, with its consent path
    models.update_restaurant(rid, {"compliance_profile_json": json.dumps(CHICAGO_BIG)})
    sr.save_compliance(rid, {"min_rest_hours": None})
    c = sr.build_constraints(rid, T.W1, [T._day(d) for d in T.W1])
    assert sr.law_backed(c, "min_rest_hours") and sr.law_backed(c, "notice_days")
    gap = [v for v in sr.violations(CLOPEN_CY, c) if v["kind"] == "rest_gap"]
    assert gap and sr.tier_of(gap[0]) == sr.TIER_LEGAL and "agree to it in writing" in gap[0]["detail"]


def test_chicago_applies_only_above_its_thresholds_and_asks_when_it_cannot_tell():
    assert "IL-CHICAGO" in cp.evaluate(_r(CHICAGO_BIG))["applies"]
    small = cp.evaluate(_r(dict(CHICAGO_BIG, employees=40, locations=1)))
    chi = next(j for j in small["jurisdictions"] if j["code"] == "IL-CHICAGO")
    assert chi["status"] == "not_covered" and "250+ employees" in chi["why"]
    unknown = cp.evaluate(_r({"state": "IL", "city": "Chicago"}))
    chi = next(j for j in unknown["jurisdictions"] if j["code"] == "IL-CHICAGO")
    assert chi["status"] == "unknown" and {q["key"] for q in chi["questions"]} == {"employees", "locations"}
    vals = cp.rule_values(_r(CHICAGO_BIG))
    assert vals["notice_days"] == 14 and vals["min_rest_hours"] == 10 and vals["rest_mode"] == "consent_premium"
    assert vals["rest_premium_multiplier"] == 1.25


def test_every_library_value_carries_a_source_and_an_effective_date_or_says_it_is_unconfirmed():
    for code, e in list(cp.JURISDICTIONS.items()) + list(cp.ORDINANCES.items()):
        for key, vs in e.items():
            if isinstance(vs, list) and vs and isinstance(vs[0], dict) and "source" in vs[0]:
                for v in vs:
                    assert v["source"].startswith("http"), (code, key)
                    assert v["effective"] or v.get("note"), (code, key)
        for key, vs in (e.get("rules") or {}).items():
            for v in vs:
                assert v["source"].startswith("http"), (code, key)
    for st, bands in cp.STATE_MINOR_RULES.items():
        for band, rules in bands.items():
            assert rules["sources"] and rules["sources"][0]["url"].startswith("http"), (st, band)
    assert cp.library_summary()["external"]                    # what counsel must confirm is listed


def test_nyc_covers_fast_food_only_and_la_covers_no_restaurant():
    nyc = cp.evaluate(_r({"state": "NY", "city": "Brooklyn", "locations": 50, "kind": "full_service"}))
    assert next(j for j in nyc["jurisdictions"] if j["code"] == "NY-NYC")["status"] == "not_covered"
    ff = cp.evaluate(_r({"state": "NY", "city": "Brooklyn", "locations": 50, "kind": "fast_food"}))
    assert "NY-NYC" in ff["applies"]
    la = cp.evaluate(_r({"state": "CA", "city": "Los Angeles", "employees": 5000, "locations": 500,
                         "kind": "fast_food"}))
    assert next(j for j in la["jurisdictions"] if j["code"] == "CA-LA")["status"] == "not_covered"


def test_chicagos_wage_ceiling_decides_who_is_a_covered_employee():
    r = _r(CHICAGO_BIG)
    assert cp.employee_covered(r, rate=20) is True
    assert cp.employee_covered(r, rate=40) is False
    assert cp.employee_covered(r, rate=None) is None
    assert cp.employee_covered(_r(EJS), rate=20) is False       # no law, nobody covered


def test_an_owner_value_laxer_than_a_covering_law_is_named():
    out = cp.conflicts(_r(CHICAGO_BIG), {"notice_days": 7})
    assert out and out[0]["key"] == "notice_days" and "Chicago sets 14" in out[0]["text"]


def test_the_profile_is_checked_and_keeps_the_state_pack_in_step(db):
    rid = T._restaurant(db)
    with pytest.raises(ValueError):
        cp.save_profile(rid, {"state": "ZZ"})
    prof = cp.save_profile(rid, EJS, by="erik")
    r = models.get_restaurant(rid)
    assert prof["confirmed"] and r.jurisdiction == "IL"
    assert sr.compliance(r)["min_rest_hours"] is None


# ── rest: no default, a jurisdiction value, a consent-and-premium path ───────

def _c(**comp):
    c = sr.Constraints(restaurant_id=1, week_dates=T.W1, week_days=[T._day(d) for d in T.W1])
    c.compliance = dict(sr.DEFAULTS, **comp)
    return c


CLOPEN = [_row(T.W1[0], "Ana", "4:00pm", "11:30pm"), _row(T.W1[1], "Ana", "7:00am", "3:00pm")]
CLOPEN_CY = [_row(T.W1[0], "Cy", "4:00pm", "11:30pm"), _row(T.W1[1], "Cy", "7:00am", "3:00pm")]


def test_no_rest_rule_by_default_and_the_owners_own_is_a_ban():
    assert sr.DEFAULTS["min_rest_hours"] is None
    assert not [v for v in sr.violations(CLOPEN, _c()) if v["kind"] in ("rest_gap", "rest_premium")]
    v = [v for v in sr.violations(CLOPEN, _c(min_rest_hours=10)) if v["kind"] == "rest_gap"]
    assert v and v[0]["hard"] and "agree" not in v[0]["detail"]


def test_under_a_consent_rule_a_clopen_is_hard_until_agreed_then_a_priced_soft_flag():
    c = _c(min_rest_hours=10, rest_mode="consent_premium", rest_premium_multiplier=1.5,
           rest_premium_scope="hours_inside")
    v = [x for x in sr.violations(CLOPEN, c) if x["kind"] == "rest_gap"]
    assert v and v[0]["hard"] and "unless they agree to it in writing" in v[0]["detail"]
    assert not c.rest_ok("Ana", CLOPEN[1], CLOPEN)[0]
    c.rest_waivers = {"ana": {"standing": False, "dates": {T.W1[1]}}}
    kinds = {x["kind"]: x for x in sr.violations(CLOPEN, c)}
    assert "rest_gap" not in kinds and not kinds["rest_premium"]["hard"]
    assert kinds["rest_premium"]["premium_hours"] == 2.5          # 7.5h rest: 2.5h of the shift inside the 10h
    assert c.rest_ok("Ana", CLOPEN[1], CLOPEN)[0]
    assert "rest_premium" in sr.HOLD_UNATTENDED


def test_the_rest_premium_is_priced_in_the_weeks_cost():
    c = _c(min_rest_hours=10, rest_mode="consent_premium", rest_premium_multiplier=1.25, rest_premium_scope="shift")
    c.rest_waivers = {"ana": {"standing": True, "dates": set()}}
    pricing = {"role_rates": {"server": 20}, "blended_rate": 20, "ceiling": 40, "rest_rule": econ._rest_rule_of(c)}
    out = econ.price_week(CLOPEN, pricing, rounded=False)
    assert out["premiums"]["rest"] == pytest.approx(8 * 20 * 0.25)   # the whole later shift at 1.25x (Chicago)


# ── California daily double time and the seventh day (MARKET-5) ──────────────

def test_california_double_time_past_twelve_and_the_seventh_day_are_priced():
    long_day = [_row(T.W1[0], "Ana", "8:00am", "10:00pm")]          # 14 hours
    base = econ.priced_cost(long_day, {"server": 20}, 20, daily_ot_hours=8, rounded=False)
    dt_ = econ.priced_cost(long_day, {"server": 20}, 20, daily_ot_hours=8, daily_dt_hours=12, rounded=False)
    assert dt_["premiums"]["double_time"] == pytest.approx(2 * 20)       # 2h past 12 at 2x
    assert dt_["total"] - base["total"] == pytest.approx(2 * 20 * 0.5)  # those 2h were 1.5x, now 2x
    week = [_row(d, "Ana", "9:00am", "3:00pm") for d in T.W1]          # seven days, 6h each
    seven = econ.priced_cost(week, {"server": 20}, 20, seventh_day_ot=True, bucket=lambda d: "w", rounded=False)
    assert seven["premiums"]["seventh_day"] == pytest.approx(6 * 20 * 0.5)
    assert cp.rule_values(_r({"state": "CA"}))["daily_dt_hours"] == 12


# ── the draft horizon (MARKET-1) ─────────────────────────────────────────────

def test_a_fourteen_day_notice_moves_the_draft_three_weeks_out():
    thu = dt.date(2026, 10, 8)
    assert sr.first_publishable_week(thu, 0, None) == dt.date(2026, 10, 12)
    assert sr.first_publishable_week(thu, 0, 14) == dt.date(2026, 10, 26)
    assert sr.first_publishable_week(thu, 0, 14, publish_lag_days=1) == dt.date(2026, 10, 26)
    h = sr.draft_horizon({"notice_days": 14}, thu, 2)                  # a Wednesday week
    assert h["week_start"] == "2026-10-28" and h["live_weeks"] == ["2026-10-14", "2026-10-21"]


def test_generate_and_the_auto_draft_target_the_first_week_publishable_on_time(db):
    import schedule_engine as se
    rid = T._restaurant(db)
    thu = dt.datetime(2026, 10, 8, 9, 0)
    assert se.generation_request(rid, today=thu)["week_start"] == "2026-10-12"
    models.update_restaurant(rid, {"compliance_profile_json": json.dumps(CHICAGO_BIG)})
    assert se.generation_request(rid, today=thu)["week_start"] == "2026-10-26"
    # a named week is still the named week
    assert se.generation_request(rid, week_start="2026-10-13", today=thu)["week_start"] == "2026-10-12"
    r = models.get_restaurant(rid)
    assert se.default_week_start(r, thu, publish_lag_days=1).strftime("%Y-%m-%d") == "2026-10-26"


def test_the_auto_drafts_fallback_day_writes_the_week_its_draft_day_targeted(db):
    """H1's fallback day (the day after the draft day) counts the notice
    horizon from the draft day, so a failed draft is retried for the same
    week rather than one further out — a week is never skipped."""
    import strategy_jobs as sj
    rid = T._restaurant(db)
    models.update_restaurant(rid, {"compliance_profile_json": json.dumps(CHICAGO_BIG), "week_start_day": 2,
                                   "auto_draft_weekday": 0})             # Wednesday weeks, Monday drafts
    r = models.get_restaurant(rid)
    assert models.auto_draft_weekday(r) == 0
    on_day = sj.auto_draft_target(r, "2026-10-12")                     # Monday
    assert on_day == "2026-10-28"                                      # 16 days out: 14 notice + 1 lag
    assert sj.auto_draft_target(r, "2026-10-13") == on_day             # Tuesday, the fallback
    models.update_restaurant(rid, {"compliance_profile_json": json.dumps(EJS)})
    r = models.get_restaurant(rid)
    assert sj.auto_draft_target(r, "2026-10-12") == "2026-10-14"       # no notice rule: next week
    assert sj.auto_draft_target(r, "2026-10-13") == "2026-10-14"


# ── the predictability-pay ledger (MARKET-2) ─────────────────────────────────

def test_changes_are_classified_per_person_and_shift():
    before = [_row(T.W2[0], "Ana", "4:00pm", "10:00pm"), _row(T.W2[1], "Bob", "11:00am", "3:00pm"),
              _row(T.W2[2], "Cy", "5:00pm", "11:00pm")]
    after = [_row(T.W2[0], "Ana", "5:00pm", "10:00pm"), _row(T.W2[1], "Bob", "10:00am", "3:00pm"),
             _row(T.W2[4], "Cy", "5:00pm", "11:00pm"), _row(T.W2[3], "Dee", "5:00pm", "9:00pm")]
    kinds = {c["employee"]: c for c in pp.classify(before, after)}
    assert kinds["Ana"]["kind"] == "reduce" and kinds["Ana"]["hours_lost"] == 1.0
    assert kinds["Bob"]["kind"] == "extend" and kinds["Bob"]["hours_added"] == 1.0
    assert kinds["Cy"]["kind"] == "move" and kinds["Cy"]["date"] == T.W2[4]
    assert kinds["Dee"]["kind"] == "add"


def test_chicago_and_new_york_price_the_same_changes_by_their_own_tiers():
    before = [_row("2026-10-20", "Ana", "4:00pm", "10:00pm"), _row("2026-10-23", "Dee", "5:00pm", "11:00pm")]
    after = [_row("2026-10-20", "Ana", "5:00pm", "10:00pm"), _row("2026-10-23", "Dee", "5:10pm", "11:00pm")]
    sent = dt.datetime(2026, 10, 20, 9, 0)
    chi = {r["employee"]: r for r in pp.price(pp.classify(before, after),
                                             cp.value_on(cp.ORDINANCES["IL-CHICAGO"]["predictability_pay"])["value"],
                                             sent, rate_of=lambda n, r: 20.0)}
    assert chi["Ana"]["amount"] == 10.0                   # cut under 24h: half the lost hour
    assert chi["Dee"]["amount"] == 20.0                   # over 24h: one hour (no unconfirmed 15-min threshold)
    nyc = {r["employee"]: r for r in pp.price(pp.classify(before, after),
                                             cp.value_on(cp.ORDINANCES["NY-NYC"]["predictability_pay"])["value"],
                                             sent, rate_of=lambda n, r: 20.0)}
    assert nyc["Ana"]["amount"] == 75.0                   # NYC: $75 for a cut under 24 hours
    assert nyc["Dee"]["status"] == "exempt" and "15 minutes" in nyc["Dee"]["exempt_reason"]
    far = pp.price(pp.classify(before, after), cp.value_on(cp.ORDINANCES["IL-CHICAGO"]["predictability_pay"])["value"],
                   dt.datetime(2026, 10, 1, 9, 0), rate_of=lambda n, r: 20.0)
    assert far == []                                      # 14+ days' notice: nothing owed, nothing recorded


def _covered_week(db, profile=CHICAGO_BIG):
    rid = T._restaurant(db, module_labor=1)
    models.update_restaurant(rid, {"compliance_profile_json": json.dumps(profile),
                                   "role_rates_json": json.dumps({"Server": 20})})
    csv_text = T._week_csv(T.W2[:3])
    hid = T._save(db, rid, T.W2, csv_text, published=True)
    sv.append(rid, hid, "published", csv_text, saved_by="Owner")
    return rid, hid, csv_text


def test_an_edit_to_a_posted_week_is_priced_before_send_and_recorded_on_send(db, save_app, monkeypatch):
    import time_utils, client_api
    rid, hid, csv_text = _covered_week(db)
    now = dt.datetime(2026, 10, 12, 8, 0)                 # the morning of the week's first day
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda *a, **k: now)
    monkeypatch.setattr(client_api, "_send_week_to_staff", lambda *a, **k: ([], [], []))
    rows = T._rows(csv_text)
    rows[1]["shift_end"] = "7:00pm"                       # Bob's 5-9pm cut to 5-7pm today
    resp = T._post_save(save_app, T._bearer(db, rid), rows, history_id=hid, version=1)
    body = resp.get_json()
    assert resp.status_code == 200, body
    pay = body["predictability_pay"]
    assert pay and pay["total"] == 20.0 and "Chicago" in body["late_change_warning"]   # 50% of 2h at $20
    review = client_api.publish_review(rid, hid, today=now.date())
    assert review["predictability_pay"]["total"] == 20.0
    out, status = client_api.send_schedule_changes(rid, hid, actor={"username": "owner"}, acknowledge=True)
    assert status == 200, out
    led = pp.ledger(rid)
    assert [(x["employee_name"], x["change_kind"], x["amount"], x["status"]) for x in led] == \
        [("Bob", "reduce", 20.0, "owed")]
    text = pp.export_csv(rid)
    line = list(csv.reader(io.StringIO(text)))[1]
    assert line[0] == "10/12/26" and line[1] == "Bob" and line[8] == "20.00"


def test_a_change_the_person_agreed_to_in_writing_is_exempt_where_the_law_accepts_it(db, monkeypatch):
    import time_utils
    rid, hid, csv_text = _covered_week(db)
    now = dt.datetime(2026, 10, 12, 8, 0)
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda *a, **k: now)
    rows = T._rows(csv_text)
    rows[1]["shift_end"] = "7:00pm"
    ask = staff_consent.ask(rid, "Bob", "shift_change", "I agree to leave at 7pm.", subject_date=rows[1]["date"],
                            subject={"after": {"start": "5:00pm", "end": "7:00pm"}})
    staff_consent.answer(rid, ask["id"], "Bob", True)
    out = pp.preview(rid, T._rows(csv_text), rows)
    assert out["covered"] and out["total"] == 0 and out["rows"][0]["status"] == "exempt"
    assert "agreed in writing" in out["rows"][0]["exempt_reason"]


def test_no_law_no_ledger_and_the_owners_notice_rule_keeps_its_sentence(db, monkeypatch):
    import time_utils
    rid, hid, csv_text = _covered_week(db, profile=EJS)
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda *a, **k: dt.datetime(2026, 10, 12, 8, 0))
    rows = T._rows(csv_text)
    rows[1]["shift_end"] = "7:00pm"
    assert pp.preview(rid, T._rows(csv_text), rows)["covered"] is False
    assert pp.record_send(rid, hid, rows)["recorded"] == 0


def test_the_send_home_suggestion_nets_out_the_predictability_pay(db, monkeypatch):
    import time_utils
    rid, hid, _ = _covered_week(db)
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda *a, **k: dt.datetime(2026, 10, 12, 20, 0))
    out = pp.cut_premium(rid, "Bob", "Server", "2026-10-12", 1.0, 4.0)
    assert out["amount"] == 10.0 and "Chicago" in out["law"]


# ── consent in one tap: a claim of a clopen (MARKET-3) ───────────────────────

def test_a_clopen_claim_asks_for_written_consent_and_goes_through_with_it(db, monkeypatch):
    import time_utils
    rid = T._restaurant(db, module_labor=1)
    models.update_restaurant(rid, {"compliance_profile_json": json.dumps(CHICAGO_BIG)})
    # Cy closes Monday (Ana and Bob run the floor and keep their own turnaround)
    text = T.HEADER + "\n" + (f"{T.W2[0]},Monday,Ana,Server,11:00am,7:00pm,8,\n"
                              f"{T.W2[0]},Monday,Bob,Server,4:00pm,11:30pm,7.5,\n"
                              f"{T.W2[0]},Monday,Cy,Server,4:00pm,11:30pm,7.5,\n"
                              f"{T.W2[1]},Tuesday,Ana,Server,7:00am,3:00pm,8,\n")
    hid = T._save(db, rid, T.W2, text, published=True)
    sv.append(rid, hid, "published", text, saved_by="Owner")
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda *a, **k: dt.datetime(2026, 10, 9, 9, 0))
    monkeypatch.setattr(srq, "_notify", lambda *a, **k: None)
    post = srq.post_open_shift(rid, T.W2[1], "7:00am", shift_end="3:00pm", role="Server", actor="owner",
                               broadcast=False, now=dt.datetime(2026, 10, 9, 9, 0))["request"]
    with pytest.raises(srq.RestConsentNeeded) as e:
        srq.claim(rid, post["id"], "Cy", actor="Cy", now=dt.datetime(2026, 10, 9, 9, 0))
    assert e.value.consent["date"] == T.W2[1] and "7.5 hours' rest" in e.value.consent["text"]
    out = srq.claim(rid, post["id"], "Cy", actor="Cy", now=dt.datetime(2026, 10, 9, 9, 0), consent_rest=True)
    assert out["status"] == "covered"
    kinds = {c["kind"]: c for c in staff_consent.for_person(rid, "Cy")["recent"]}
    assert kinds["rest_waiver"]["status"] == "agreed" and kinds["rest_waiver"]["answered_at"]
    assert kinds["added_hours"]["status"] == "agreed"                 # accepting the shift is written consent too


# ── good-faith estimates, written answers, access to hours (MARKET-4) ────────

def test_a_good_faith_estimate_is_drafted_issued_and_acknowledged(db):
    rid, hid, _ = _covered_week(db)
    est = fww.draft_estimate(rid, "Bob")
    assert est["hours_low"] == 12.0 and est["period_days"] == 90 and est["law"] == "Chicago"
    row = fww.issue_estimate(rid, est, issued_by="owner")
    assert row["text"].startswith("Publish Co: your good-faith schedule estimate for your first 90 days")
    with pytest.raises(fww.FairWorkweekError):
        fww.acknowledge_estimate(rid, row["id"], "Ana")
    assert fww.acknowledge_estimate(rid, row["id"], "Bob")["acknowledged_at"]


def test_a_request_to_change_the_estimate_is_due_a_written_answer_in_three_days(db):
    rid, hid, _ = _covered_week(db)
    req = fww.request_change(rid, "Bob", "Fewer Sundays please")
    due = dt.datetime.strptime(req["due_at"], "%Y-%m-%d %H:%M:%S")
    assert 2.9 < (due - dt.datetime.utcnow()).total_seconds() / 86400 <= 3.0
    with pytest.raises(fww.FairWorkweekError):
        fww.answer_request(rid, req["id"], "granted", "")
    assert fww.answer_request(rid, req["id"], "modified", "Every other Sunday off.")["status"] == "modified"


def test_added_hours_go_to_current_staff_first(db, monkeypatch):
    rid, hid, _ = _covered_week(db, profile=dict(CHICAGO_BIG, city="Seattle", state="WA", employees=600,
                                                 locations=45))
    monkeypatch.setattr(srq, "_notify", lambda *a, **k: None)
    added = pp.classify([], [_row(T.W2[2], "Newbie", "5:00pm", "9:00pm")])
    assert fww.added_hours_without_offer(rid, added)          # Seattle: offer to current staff first
    offer = fww.offer_hours(rid, T.W2[2], "5:00pm", "9:00pm", role="Server", actor="owner",
                            now=dt.datetime(2026, 10, 1, 9))
    assert offer["closes_at"] == "2026-10-04 09:00:00"         # Seattle: posted 72 hours


def test_chicago_states_no_posting_period_so_the_owner_sets_one(db, monkeypatch):
    rid, hid, _ = _covered_week(db)
    monkeypatch.setattr(srq, "_notify", lambda *a, **k: None)
    with pytest.raises(fww.FairWorkweekError):
        fww.offer_hours(rid, T.W2[2], "5:00pm", "9:00pm", role="Server", actor="owner",
                        now=dt.datetime(2026, 10, 1, 9))
    assert fww.offer_hours(rid, T.W2[2], "5:00pm", "9:00pm", role="Server", actor="owner", hours_window=48,
                           now=dt.datetime(2026, 10, 1, 9))["id"]


# ── three-year records (MARKET-4) ────────────────────────────────────────────

def test_a_covered_restaurant_keeps_its_records_three_years(db):
    import ops
    held_rid, _h, _c2 = _covered_week(db)
    other = T._restaurant(db)
    for t in ("staff_consents", "compliance_pay_ledger", "good_faith_estimates", "fww_requests", "fww_access_offers"):
        assert ops._RETENTION_DAYS[t] == 1095 and ops.retention_floor(t) == 1095
    conn = models.get_conn(db)
    try:
        assert ops._records_held(conn) == [held_rid]
        old = (dt.date.today() - dt.timedelta(days=800)).isoformat()
        for rid in (held_rid, other):
            conn.execute("INSERT INTO attendance_events (restaurant_id, employee_name, employee_key, business_date, "
                         "shift_start, outcome, source) VALUES (?, 'Ana', 'ana', ?, '5:00pm', 'on_time', 'manual')",
                         (rid, old))
        conn.commit()
    finally:
        conn.close()
    ops.prune_ledgers(db_path=db)
    conn = models.get_conn(db)
    try:
        left = {r[0] for r in conn.execute("SELECT restaurant_id FROM attendance_events").fetchall()}
    finally:
        conn.close()
    assert left == {held_rid}


# ── minors: state tables and the school calendar (MARKET-5, LEARNING-18) ─────

def _minor_c(state, band="14-15", cal=None, **comp):
    c = _c(**comp)
    c.minors, c.minor_bands, c.jurisdiction = {"kid"}, {"kid": band}, state
    c.school_calendar = cal or {"years": []}
    return c


def test_a_state_table_tightens_the_federal_floor():
    wa = sr.minor_rules("14-15", "WA")
    assert wa["max_weekly_school_week"] == 16.0 and wa["max_days_week"] == 6
    assert "WAC 296-125" in wa["source"]
    assert sr.minor_rules("16-17", "CA")["latest_end_before_non_school_day"] == "12:30am"
    week = [_row(d, "Kid", "4:00pm", "7:00pm") for d in T.W1[:6]]   # 18h, six days, in a school week
    kinds = [v["kind"] for v in sr.violations(week, _minor_c("WA")) if v["hard"]]
    assert "minor_week_hours" in kinds                              # over Washington's 16
    assert "minor_week_hours" not in [v["kind"] for v in sr.violations(week, _minor_c("IL")) if v["hard"]]


def test_six_days_a_week_at_most_where_the_state_says_so():
    summer = ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-11", "2026-07-12"]
    rows = [_row(d, "Kid", "10:00am", "2:00pm") for d in summer]
    c = _minor_c("NY", max_consecutive_days=14)
    c.week_dates = summer
    days = [v for v in sr.violations(rows, c) if v["kind"] == "minor_week_hours" and "days in a week" in v["detail"]]
    assert days and days[0]["date"] == summer[6]


def test_new_yorks_later_summer_end_starts_june_21():
    c = _minor_c("NY")
    assert sr.minor_latest(c, "kid", "2026-06-10")[1] == "7:00pm"
    assert sr.minor_latest(c, "kid", "2026-06-25")[1] == "9:00pm"


def test_august_weekdays_are_school_days_until_a_calendar_says_otherwise():
    assert sc.is_school_day("2026-08-12") and not sc.is_school_day("2026-07-15")
    cal = sc.clean_calendar({"district": "D303", "years": [{"start": "2026-08-19", "end": "2027-06-04",
                                                           "breaks": [{"start": "2026-12-21", "end": "2027-01-01"}]}]})
    assert not sc.is_school_day("2026-08-12", cal) and sc.is_school_day("2026-08-20", cal)
    assert not sc.is_school_day("2026-12-22", cal)
    # the hours limits follow it: 5h on Aug 12 is a breach by default, not under the district's calendar
    row = [_row("2026-08-12", "Kid", "10:00am", "3:00pm")]
    assert "minor_hours" in [v["kind"] for v in sr.violations(row, _minor_c("IL"))]
    assert "minor_hours" not in [v["kind"] for v in sr.violations(row, _minor_c("IL", cal=cal))]
    assert sc.needs_prompt({"years": []}, dt.date(2026, 7, 15), True)["school_year"] == "2026-27"
    assert not sc.needs_prompt(cal, dt.date(2026, 9, 1), True)["ask"]
    with pytest.raises(ValueError):
        sc.clean_calendar({"years": [{"start": "2026-08-19", "end": "2026-08-01"}]})


# ── breaks placed inside shifts (MARKET-6) ───────────────────────────────────

IL_MEALS = {"meals": cp.value_on(cp.JURISDICTIONS["IL"]["meal_breaks"])["value"]}


def test_illinois_meals_are_placed_by_hour_five_and_staggered_off_the_rush():
    rows = [_row(T.W1[0], "Ana", "10:00am", "6:00pm"), _row(T.W1[0], "Bo", "10:00am", "6:00pm"),
            _row(T.W1[0], "Cy", "10:00am", "1:00pm")]
    plan = breaks.plan_day(rows, IL_MEALS, {12: 0.3, 13: 0.25})
    a, b = plan[0][0], plan[1][0]
    assert a["minutes"] == 20 and a["start"] <= 10 * 60 + 5 * 60 and a["placed"]
    assert a["end"] <= b["start"] or b["end"] <= a["start"]        # never both at once
    assert 2 not in plan                                           # a 3-hour shift owes none
    assert not (12 * 60 <= a["start"] < 14 * 60)                   # not in the rush


def test_coverage_by_the_hour_takes_a_person_off_the_floor_for_their_meal():
    rows = [_row(T.W1[0], "Ana", "10:00am", "6:00pm"), _row(T.W1[0], "Bo", "10:00am", "6:00pm")]

    def ctx(rules):
        return sq.ShiftContext(date=T.W1[0], day="Monday", daypart="morning", rows=rows, day_rows=rows,
                               role_floors={"Server": 2}, open_minutes=10 * 60, close_minutes=22 * 60,
                               break_rules=rules)
    full = sq.dim_coverage_curve(ctx(None))
    on_breaks = sq.dim_coverage_curve(ctx({"meals": [dict(m, minutes=30) for m in IL_MEALS["meals"]]}))
    assert full.score == 100 and on_breaks.score < 100


def test_a_meal_the_law_requires_that_cannot_fit_is_the_only_meal_flag():
    rows = [_row(T.W1[0], "Ana", "10:00am", "9:00pm")]
    c = _c(meal_break_after_hours=6)
    assert "meal_break" not in [v["kind"] for v in sr.violations(rows, c)]     # placed midway: no flag
    c.break_rules = {"meals": [{"min_hours": 5, "minutes": 30, "start_after": 10.9, "end_by_before_end": 1.0}]}
    assert "meal_break" in [v["kind"] for v in sr.violations(rows, c)]


def test_a_missed_meal_on_the_punches_owes_californias_premium_hour():
    rules = breaks.rules_for({}, {"meal_breaks": cp.value_on(cp.JURISDICTIONS["CA"]["meal_breaks"]),
                                  "break_premium": cp.value_on(cp.JURISDICTIONS["CA"]["break_premium"])})
    out = breaks.audit_punches([{"employee_name": "Ana", "business_date": "2026-10-05",
                                 "clock_in": "2026-10-05T10:00:00", "clock_out": "2026-10-05T17:00:00",
                                 "meal_minutes": 0, "reg_rate": 18}], rules)
    assert out[0]["kind"] == "missed_meal" and out[0]["premium_dollars"] == 18.0


# ── the owner's screens, web and phone (one body) ────────────────────────────

def test_the_compliance_screen_reads_coverage_sources_and_the_horizon(db, monkeypatch):
    import strategy_routes
    rid, hid, _ = _covered_week(db)
    u = {"id": 1, "restaurant_id": rid, "is_admin": 1}
    monkeypatch.setattr(strategy_routes, "_local_today", lambda u: dt.date(2026, 10, 8))
    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context("/api/labor/compliance?brief=1"):
        brief, _ = strategy_routes._do_compliance_engine_get(u)
    assert set(brief) == {"ok", "horizon"} and brief["horizon"]["week_start"] == "2026-10-26"
    with app.test_request_context("/api/labor/compliance"):
        out, status = strategy_routes._do_compliance_engine_get(u)
    assert status == 200 and "IL-CHICAGO" in out["evaluation"]["applies"]
    keys = {x["key"]: x for x in out["in_force"]}
    assert keys["notice_days"]["source"].startswith("https://codelibrary.amlegal.com/codes/chicago")
    assert out["rest"]["mode"] == "consent_premium" and out["horizon"]["weeks_ahead"] >= 2
    assert "ledger" in out and "estimates" in out


# ── the auto-publish under a notice rule (MARKET-1) ──────────────────────────

from test_edge_sched_publish import friday  # noqa: E402,F401  (fixture)

W3 = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25"]


def test_the_auto_publish_sends_the_horizon_week_when_the_nearer_ones_are_with_staff(db, friday):  # noqa: F811
    import scheduler
    rid = T._auto_restaurant(db)
    models.update_restaurant(rid, {"compliance_profile_json": json.dumps(CHICAGO_BIG)})
    T._save(db, rid, T.W1, T._week_csv(T.W1[:5]), published=True)
    T._save(db, rid, T.W2, T._week_csv(T.W2[:5]), published=True)
    far = T._save(db, rid, W3, T._week_csv(W3[:5]))
    scheduler.run_auto_publish_schedules()
    assert T._queued(db, rid) == [{"schedule_id": far, "automatic": True}]


# ── the staff side, over HTTP ────────────────────────────────────────────────

def _staff_app(db):
    from flask import Flask
    import auth
    from staff_routes import staff_bp
    auth.init_auth(db_path=db)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(staff_bp)
    return app


def _staff_client(db, app, rid, name):
    import auth
    uid = auth.create_user(rid, name.lower(), f"{name.lower()}{rid}@x.test", "unused", db_path=db)
    auth.upsert_membership(uid, rid, "employee", employee_name=name, db_path=db)
    c = app.test_client()
    c.set_cookie("staff_session", auth.create_staff_session(uid, rid, db_path=db))
    return c


def test_staff_agree_in_the_app_and_a_standing_agreement_needs_a_consent_rule(db):
    rid, hid, _ = _covered_week(db)
    app = _staff_app(db)
    cy = _staff_client(db, app, rid, "Cy")
    ask = staff_consent.ask(rid, "Cy", "shift_change", "I agree to start at 6pm on 10/14/26.", subject_date=T.W2[2])
    d = cy.get("/staff/api/compliance").get_json()
    assert d["ok"] and d["consents"]["pending"][0]["id"] == ask["id"] and d["rest"]["hours"] == 10
    r = cy.post(f"/staff/api/consents/{ask['id']}/answer", json={"agree": True}).get_json()
    assert r["ok"] and r["consent"]["status"] == "agreed" and r["consent"]["answered_at"]
    standing = cy.post("/staff/api/consents/rest-standing", json={"agree": True}).get_json()
    assert standing["consent"]["kind"] == "rest_waiver_standing"
    other, _h, _c = _covered_week(db, profile=EJS)
    bo = _staff_client(db, app, other, "Bo")
    resp = bo.post("/staff/api/consents/rest-standing", json={"agree": True})
    assert resp.status_code == 400                                 # no rest rule takes an agreement at EJ's


def test_the_staff_schedule_shows_the_breaks_placed_in_the_shift(db):
    import staff_schedule
    text = T.HEADER + "\n" + (f"{T.W2[0]},Monday,Ana,Server,10:00am,6:00pm,8,\n"
                              f"{T.W2[0]},Monday,Bob,Server,10:00am,6:00pm,8,\n")
    import schedule_rows
    placed = staff_schedule._placed_breaks(schedule_rows.parse(text), "Ana", {"meals": IL_MEALS["meals"]})
    meal = placed[(T.W2[0], "10:00am")][0]
    assert meal["kind"] == "meal" and meal["minutes"] == 20 and meal["placed"]
