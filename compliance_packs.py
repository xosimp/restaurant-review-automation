"""
compliance_packs.py — the jurisdiction rule library and the coverage engine.

The rules dict (schedule_rules.DEFAULTS) has the knobs — daily overtime,
meal breaks, rest between shifts, notice, minors — and this module says what
a jurisdiction sets them to, for whom, from when, and on what authority.

Three things changed with the scheduler audit of 10/10/26 (MARKET-1..6,
LOGIC-21, RM-15):

  * Every value is a FACT WITH A SOURCE: `_v(value, source, effective)` —
    an official URL (statute, ordinance, agency rule or FAQ) and the date it
    took effect. A value that could not be confirmed against its source is
    left out and listed in EXTERNAL, for counsel — never filled from memory.
  * A state's general rules (meal and rest breaks, daily overtime, minors)
    are separate from the ordinances that cover only some employers
    (predictive scheduling: Chicago, New York City, Seattle, Philadelphia,
    Oregon, ...). An ordinance applies only where the restaurant is in its
    place AND meets its coverage thresholds; `evaluate` says which apply,
    which do not and why, and which it cannot decide without an answer
    (employee and location counts). Simple EJ's, in St. Charles IL, is held
    to Illinois's rules and told in so many words that Chicago's Fair
    Workweek Ordinance does not cover it.
  * The rules sit UNDER the owner's own values, as before (`apply_for`):
    anything the owner typed wins, and everything a jurisdiction set is
    shown with its source. `conflicts` names an owner value laxer than a
    law that covers them.

These are public figures checked against their sources on REVIEWED_ON, not
legal advice; the screens say so, and the library is to be re-checked by
counsel every quarter (REVIEW_EVERY_DAYS).
"""
import json
from datetime import date, datetime

REVIEWED_ON = "2026-10-10"
REVIEW_EVERY_DAYS = 92
COUNSEL_WORDING = "Public rules checked against their sources — not legal advice; check with counsel."


def _v(value, source, effective, note=None, until=None) -> dict:
    """One sourced value: in force from `effective` (ISO) until `until`."""
    return {"value": value, "source": source, "effective": effective, "until": until, "note": note}


# ── sources ────────────────────────────────────────────────────────────────

S = {
    # Illinois
    "IL_ODRISA": "https://www.ilga.gov/Documents/legislation/ilcs/documents/082001400K3.htm",
    "IL_ODRISA_FAQ": "https://labor.illinois.gov/faqs/odrisa-faq.html",
    "IL_ODRISA_ACT": "https://www.ilga.gov/legislation/ilcs/ilcs3.asp?ActID=2405&ChapterID=68",
    # California
    "CA_LC512": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=LAB&sectionNum=512",
    "CA_LC510": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=LAB&sectionNum=510",
    "CA_LC2267": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=LAB&sectionNum=226.7",
    "CA_MEAL_FAQ": "https://www.dir.ca.gov/dlse/faq_mealperiods.htm",
    "CA_WO5": "https://www.dir.ca.gov/IWC/IWCArticle5.pdf",
    # New York
    "NY_LL162": "https://www.nysenate.gov/legislation/laws/LAB/162",
    "NY_MEAL_FAQ": "https://dol.ny.gov/meal-and-rest-periods-faqs",
    "NY_146": "https://forms.labor.ny.gov/WP/CR146.pdf",
    # Washington, Oregon, Massachusetts, Colorado and the other break states
    "WA_MEAL": "https://app.leg.wa.gov/wac/default.aspx?cite=296-126-092",
    "WA_LNI": "https://lni.wa.gov/workers-rights/workplace-policies/rest-breaks-meal-periods-and-schedules",
    "OR_MEAL": "https://secure.sos.state.or.us/oard/view.action?ruleNumber=839-020-0050",
    "OR_BOLI": "https://www.oregon.gov/boli/workers/pages/meals-and-breaks.aspx",
    "MA_MEAL": "https://malegislature.gov/Laws/GeneralLaws/PartI/TitleXXI/Chapter149/Section100",
    "CO_COMPS": "https://www.sos.state.co.us/CCR/GenerateRulePdf.do?ruleVersionId=12406&fileName=7+CCR+1103-1",
    "NV_608": "https://www.leg.state.nv.us/nrs/nrs-608.html",
    "MN_177254": "https://www.revisor.mn.gov/statutes/cite/177.254",
    "MN_DLI": "https://www.dli.mn.gov/breaks",
    "KY_337365": "https://apps.legislature.ky.gov/law/statutes/statute.aspx?id=49845",
    "TN_BREAKS": "https://www.tn.gov/workforce/employees/labor-laws/labor-laws-redirect/wages-breaks.html",
    "NH_275": "https://gc.nh.gov/rsa/html/XXIII/275/275-30-a.htm",
    "RI_28314": "https://webserver.rilegislature.gov/Statutes/TITLE28/28-3/28-3-14.htm",
    "WV_21310A": "https://code.wvlegislature.gov/21-3-10A/",
    "ND_46020702": "https://www.ndlegis.gov/information/acdata/pdf/46-02-07.pdf",
    "ME_601": "https://legislature.maine.gov/statutes/26/title26sec601.html",
    "DE_707": "https://delcode.delaware.gov/title19/c007/sc01/index.html",
    "CT_3151II": "https://www.cga.ct.gov/current/pub/chap_557.htm",
}


# ── meal and rest breaks (MARKET-6) ────────────────────────────────────────
#
# A meal rule: {"min_hours": shift length that owes it, "inclusive": True
# when a shift of exactly min_hours owes it ("7.5 continuous hours") and
# False for "more than", "minutes", "paid", and its timing — any of
# "start_after" (hours from the shift's start before which it may not begin),
# "start_by" (hours from start by which it must begin), "end_by_before_end"
# (hours before the shift's end it must be over), "clock" ([earliest,
# latest] clock times it must sit in), "midway" (centred in the shift)}.
# breaks.py places them; this is only what the law says.

# Rest periods by hours worked (Oregon BOLI's table; Colorado COMPS Rule
# 5.2's is the same): [up to hours, rest periods].
_REST_TABLE_4H = [[2, 0], [6, 1], [10, 2], [14, 3], [18, 4], [22, 5], [24, 6]]


def _meal(min_hours, minutes, inclusive=False, paid=False, **timing):
    return dict({"min_hours": float(min_hours), "minutes": int(minutes), "inclusive": bool(inclusive),
                 "paid": bool(paid)}, **timing)


# ── the library ────────────────────────────────────────────────────────────
#
# JURISDICTIONS: {code: entry}. A STATE entry (level "state", no coverage)
# is every employer in that state. An ORDINANCE entry ("city" or a covered
# state law) has `places` (lower-case city names; None = statewide) and
# `coverage` — it applies only to the employers its thresholds name.
#
# `rules`: {compliance key: [_v(...)]}, the schedule_rules.DEFAULTS knobs.
# Other facts (meal_breaks, rest_breaks, break_premium, predictability_pay,
# good_faith_estimate, change_requests, access_to_hours, records_years) are
# read by the modules that need them (breaks, predictability_pay,
# fair_workweek, ops retention) through values_in_force.

JURISDICTIONS = {
    "IL": {
        "label": "Illinois", "level": "state", "state": "IL",
        # One Day Rest in Seven Act: 24 consecutive hours off in every
        # calendar week — held as six days in a row at most (stricter across
        # a week's boundary). No rest-between-shifts law in Illinois: the
        # 10 hours is Chicago's ordinance, for covered employers only.
        "rules": {"max_consecutive_days": [_v(6, S["IL_ODRISA_ACT"], None,
                                              note="In force (its enactment date not stated here). At least 24 "
                                                   "consecutive hours of rest in every calendar week (820 ILCS "
                                                   "140/2); held as six days in a row at most.")]},
        "meal_breaks": [_v([_meal(7.5, 20, inclusive=True, start_by=5.0),
                            _meal(12.0, 20, inclusive=True)],
                           S["IL_ODRISA"], "2023-01-01",
                           note="20 minutes beginning no later than 5 hours into a 7.5-hour work period; another 20 "
                                "for every additional 4.5 continuous hours (820 ILCS 140/3 as amended by "
                                "P.A. 102-828).")],
        "break_penalty": [_v({"per_offense_under_25": 250, "per_offense_25_plus": 500},
                             S["IL_ODRISA_FAQ"], "2023-01-01",
                             note="A civil penalty to the employee per offense (each day a meal period is not "
                                  "provided), not a pay premium.")],
        "notes": ["One day of rest in seven (820 ILCS 140) — the engine's days-in-a-row rule covers it."],
    },
    "CA": {
        "label": "California", "level": "state", "state": "CA",
        "rules": {
            "daily_ot_hours": [_v(8, S["CA_LC510"], "2000-01-01", note="1.5x past 8 hours in a workday.")],
            "daily_dt_hours": [_v(12, S["CA_LC510"], "2000-01-01", note="2x past 12 hours in a workday.")],
            "seventh_day_ot": [_v(True, S["CA_LC510"], "2000-01-01",
                                  note="The seventh consecutive day of a workweek: 1.5x for its first 8 hours, "
                                       "2x past 8 (Labor Code 510; Wage Order 5 §3(A)(1) says 'consecutive').")],
            "meal_break_after_hours": [_v(5, S["CA_LC512"], "2000-01-01")],
        },
        "meal_breaks": [_v([_meal(5, 30, start_by=5.0), _meal(10, 30, start_by=10.0)],
                           S["CA_LC512"], "2000-01-01",
                           note="A 30-minute meal for a work period over 5 hours, begun no later than the end of "
                                "the fifth hour; a second over 10 hours, by the end of the tenth (DIR FAQ). The "
                                "first may be waived by mutual consent when the day is no more than 6 hours.")],
        "rest_breaks": [_v({"minutes": 10, "per_hours": 4, "major_fraction": True, "min_shift_hours": 3.5,
                            "paid": True}, S["CA_WO5"], "2001-01-01")],
        "break_premium": [_v({"hours_at_regular_rate": 1, "per": "workday", "applies_to": ["meal", "rest"]},
                             S["CA_LC2267"], "2001-01-01",
                             note="One additional hour of pay at the regular rate for each workday a meal or "
                                  "rest period is not provided.")],
        "split_shift": [_v({"premium": "one hour's pay at the minimum wage", "applies": True}, S["CA_WO5"],
                           "2001-01-01",
                           note="Wage Order 5 §4(C). Priced only once the minimum wage that applies at the "
                                "restaurant is on file (local minimum wages differ) — see EXTERNAL.")],
        "notes": ["Daily overtime past 8 hours and double time past 12; the seventh consecutive day of a "
                  "workweek at premium rates.",
                  "A split shift owes one hour's pay at the minimum wage (Wage Order 5 §4(C))."],
    },
    "NY": {
        "label": "New York", "level": "state", "state": "NY",
        "rules": {"meal_break_after_hours": [_v(6, S["NY_LL162"], "2000-01-01")]},
        "meal_breaks": [_v([_meal(6, 30, clock=["11:00am", "2:00pm"], spans_clock=["11:00am", "2:00pm"]),
                            _meal(6, 45, midway=True, starts_between=["1:00pm", "6:00am"]),
                            _meal(0, 20, clock=["5:00pm", "7:00pm"], spans_clock=["11:00am", "7:00pm"],
                                  span_strict=True)],
                           S["NY_LL162"], "2000-01-01",
                           note="Shifts over 6 hours across 11am-2pm: 30 minutes between 11am and 2pm. A shift "
                                "over 6 hours starting between 1pm and 6am: 45 minutes midway. A workday from "
                                "before 11am to after 7pm: another 20 minutes between 5pm and 7pm (DOL FAQ).")],
        "spread_of_hours": [_v({"hours": 10, "premium": "one hour at the basic minimum hourly rate"},
                               S["NY_146"], "2016-12-31",
                               note="12 NYCRR 146-1.6: every restaurant employee whatever their rate. Flagged, "
                                    "priced once the minimum wage that applies is on file (see EXTERNAL).")],
        "notes": ["No daily overtime in New York (weekly over 40 only, 12 NYCRR 146-1.4)."],
    },
    "WA": {
        "label": "Washington", "level": "state", "state": "WA",
        "rules": {"meal_break_after_hours": [_v(5, S["WA_MEAL"], "2000-01-01")]},
        "meal_breaks": [_v([_meal(5, 30, start_after=2.0, start_by=5.0)], S["WA_MEAL"], "2000-01-01",
                           note="30 minutes for a shift over 5 hours, begun no sooner than 2 and no later than "
                                "5 hours from the start.")],
        "rest_breaks": [_v({"minutes": 10, "per_hours": 4, "major_fraction": False, "paid": True,
                            "no_more_than_hours_without": 3}, S["WA_MEAL"], "2000-01-01")],
    },
    "OR": {
        "label": "Oregon", "level": "state", "state": "OR",
        "rules": {"meal_break_after_hours": [_v(6, S["OR_MEAL"], "2000-01-01",
                                                note="Owed at 6 hours or more (inclusive).")]},
        "meal_breaks": [_v([_meal(6, 30, inclusive=True, start_after=2.0, start_by=4.0, max_shift_hours=7.0),
                            _meal(7, 30, inclusive=False, start_after=3.0, start_by=5.0),
                            _meal(14, 30, inclusive=True)],
                           S["OR_MEAL"], "2000-01-01",
                           note="30 minutes at 6+ hours: in a period of 7 hours or less after the second hour "
                                "and before the fifth begins; over 7 hours after the third and before the sixth "
                                "begins (BOLI table: a second meal at 14 hours).")],
        "rest_breaks": [_v({"minutes": 10, "per_hours": 4, "paid": True, "table": _REST_TABLE_4H},
                           S["OR_BOLI"], "2000-01-01", note="BOLI's table of rest periods by hours worked.")],
        "notes": ["Rule history: a temporary amendment (BLI 8-2025) ran 6/1/25 to 11/23/25 — re-check with counsel."],
    },
    "MA": {
        "label": "Massachusetts", "level": "state", "state": "MA",
        "rules": {"meal_break_after_hours": [_v(6, S["MA_MEAL"], "2000-01-01")]},
        "meal_breaks": [_v([_meal(6, 30)], S["MA_MEAL"], "2000-01-01",
                           note="30 minutes when the day is more than six hours; may be unpaid when free of "
                                "all duties.")],
    },
    "CO": {
        "label": "Colorado", "level": "state", "state": "CO",
        "rules": {"meal_break_after_hours": [_v(5, S["CO_COMPS"], "2026-02-01")]},
        "meal_breaks": [_v([_meal(5, 30, start_after=1.0, end_by_before_end=1.0)], S["CO_COMPS"], "2026-02-01",
                           note="COMPS Order #40 Rule 5.1: 30 minutes when the shift exceeds 5 consecutive "
                                "hours, at least an hour after the start and an hour before the end where "
                                "practical.")],
        "rest_breaks": [_v({"minutes": 10, "per_hours": 4, "paid": True, "table": _REST_TABLE_4H},
                           S["CO_COMPS"], "2026-02-01")],
        "break_premium": [_v({"minutes_of_wages": 10, "applies_to": ["rest"]}, S["CO_COMPS"], "2026-02-01",
                             note="A missed rest period is a failure to pay 10 minutes of wages (Rule 5.2.4).")],
        "notes": ["Daily overtime past 12 hours in a workday or 12 consecutive hours (COMPS Rule 4.1.1)."],
    },
    "NV": {
        "label": "Nevada", "level": "state", "state": "NV",
        "rules": {},
        "meal_breaks": [_v([_meal(8, 30, inclusive=True)], S["NV_608"], "2000-01-01",
                           note="Half an hour for a continuous period of 8 hours (NRS 608.019).")],
        "rest_breaks": [_v({"minutes": 10, "per_hours": 4, "major_fraction": True, "min_shift_hours": 3.5,
                            "paid": True}, S["NV_608"], "2000-01-01")],
        "notes": ["Daily overtime past 8 hours applies only to employees paid under 1.5x the minimum wage "
                  "(NRS 608.018) — not set automatically."],
    },
    "MN": {
        "label": "Minnesota", "level": "state", "state": "MN",
        "rules": {},
        "meal_breaks": [_v([_meal(6, 30, inclusive=True)], S["MN_177254"], "2026-01-01")],
        "rest_breaks": [_v({"minutes": 15, "per_hours": 4, "major_fraction": False, "paid": True},
                           S["MN_DLI"], "2026-01-01")],
    },
    "TN": {
        "label": "Tennessee", "level": "state", "state": "TN",
        "rules": {},
        "meal_breaks": [_v([_meal(6, 30, inclusive=True, start_after=1.0)], S["TN_BREAKS"], "2000-01-01",
                           note="Not in or before the first hour. Waiters and waitresses are exempt by "
                                "Department rule (0800-5-2-.04(3)).")],
    },
    "NH": {
        "label": "New Hampshire", "level": "state", "state": "NH",
        "rules": {},
        "meal_breaks": [_v([_meal(5, 30)], S["NH_275"], "2000-01-01",
                           note="Not owed where the employee can eat while working and the employer allows it.")],
    },
    "RI": {
        "label": "Rhode Island", "level": "state", "state": "RI",
        "rules": {},
        "meal_breaks": [_v([_meal(6, 20, inclusive=True, max_shift_hours=7.99), _meal(8, 30, inclusive=True)],
                           S["RI_28314"], "2000-01-01",
                           note="20 minutes within a 6-hour shift, 30 within an 8-hour shift; employers with "
                                "fewer than 3 people on a shift are exempt.")],
    },
    "WV": {
        "label": "West Virginia", "level": "state", "state": "WV",
        "rules": {},
        "meal_breaks": [_v([_meal(6, 20, inclusive=True)], S["WV_21310A"], "2000-01-01")],
    },
    "ND": {
        "label": "North Dakota", "level": "state", "state": "ND",
        "rules": {},
        "meal_breaks": [_v([_meal(5, 30)], S["ND_46020702"], "2000-01-01",
                           note="When two or more employees are on duty; the employee may waive it.")],
    },
    "ME": {
        "label": "Maine", "level": "state", "state": "ME",
        "rules": {},
        "meal_breaks": [_v([_meal(6, 30)], S["ME_601"], "2000-01-01",
                           note="Fewer than 3 employees on duty with frequent paid short breaks are exempt.")],
    },
    "DE": {
        "label": "Delaware", "level": "state", "state": "DE",
        "rules": {},
        "meal_breaks": [_v([_meal(7.5, 30, inclusive=True, start_after=2.0, end_by_before_end=2.0)],
                           S["DE_707"], "2000-01-01")],
    },
    "CT": {
        "label": "Connecticut", "level": "state", "state": "CT",
        "rules": {},
        "meal_breaks": [_v([_meal(7.5, 30, inclusive=True, start_after=2.0, end_by_before_end=2.0)],
                           S["CT_3151II"], "2000-01-01",
                           note="Not owed where the employer gives 30 or more total minutes of paid rest or "
                                "meal periods.")],
    },
}

# ── predictive scheduling (MARKET-1..4) ────────────────────────────────────
#
# An ordinance entry's facts:
#   coverage.employer   {employees_min, locations_min, scope, kinds,
#                        employees_here_min} — the employer test
#   coverage.employee   {hourly_max, annual_max, salaried_excluded}
#   rules               notice_days, min_rest_hours, rest_mode,
#                       rest_premium_multiplier / rest_premium_flat,
#                       rest_premium_scope ("hours_inside" | "shift")
#   predictability_pay  {window_days, min_change_minutes, consent_exempts,
#                        tiers: [{kinds, under_hours, at_least_hours, pay}]}
#                       pay: {"hours": n} | {"fraction_of_lost": f} |
#                       {"flat": $} | {"hours_by_shift": [[max_shift_h, n], ...]}
#                       | {"min_hours_of": n, "or": "shift_hours"|"lost_hours"}
#   good_faith_estimate {due, period_days, contents}
#   change_requests     {answer_days, applies_to}
#   access_to_hours     {post_hours | accept_hours, to}
#   records_years
# Change kinds (predictability_pay.classify): add, extend, move (no hours
# lost), reduce, cut.

S.update({
    "CHI_CODE": "https://codelibrary.amlegal.com/codes/chicago/latest/chicago_il/0-0-0-2639928",
    "CHI_NOTICE": "https://codelibrary.amlegal.com/codes/chicago/latest/chicago_il/0-0-0-2639967",
    "CHI_PAY": "https://codelibrary.amlegal.com/codes/chicago/latest/chicago_il/0-0-0-2639980",
    "CHI_REST": "https://codelibrary.amlegal.com/codes/chicago/latest/chicago_il/0-0-0-2640012",
    "CHI_HOURS": "https://codelibrary.amlegal.com/codes/chicago/latest/chicago_il/0-0-0-2640005",
    "CHI_RECORDS": "https://codelibrary.amlegal.com/codes/chicago/latest/chicago_il/0-0-0-2640045",
    "CHI_BACP": "https://www.chicago.gov/city/en/depts/bacp/supp_info/fairworkweek.html",
    "NYC_COVER": "https://codelibrary.amlegal.com/codes/newyorkcity/latest/NYCadmin/0-0-0-245232",
    "NYC_NOTICE": "https://codelibrary.amlegal.com/codes/newyorkcity/latest/NYCadmin/0-0-0-36426",
    "NYC_PAY": "https://codelibrary.amlegal.com/codes/newyorkcity/latest/NYCadmin/0-0-0-133714",
    "NYC_RULE622": "https://codelibrary.amlegal.com/codes/newyorkcity/latest/NYCrules/0-0-0-133990",
    "NYC_REST": "https://codelibrary.amlegal.com/codes/newyorkcity/latest/NYCadmin/0-0-0-36469",
    "NYC_RECORDS": "https://codelibrary.amlegal.com/codes/newyorkcity/latest/NYCrules/0-0-0-103873",
    "SEA_CODE": "https://library.municode.com/wa/seattle/codes/municipal_code?nodeId=TIT14HURI_CH14.22SESC",
    "SF_COVER": "https://codelibrary.amlegal.com/codes/san_francisco/latest/sf_laboremployment/0-0-0-1307",
    "SF_PAY": "https://codelibrary.amlegal.com/codes/san_francisco/latest/sf_laboremployment/0-0-0-1320",
    "SF_HOURS": "https://codelibrary.amlegal.com/codes/san_francisco/latest/sf_laboremployment/0-0-0-1174",
    "SF_RECORDS": "https://codelibrary.amlegal.com/codes/san_francisco/latest/sf_laboremployment/0-0-0-1357",
    "SF_USES": "https://sfplanning.org/permit/chain-stores",
    "LA_FWW": "https://wagesla.lacity.gov/fair-work-week-information",
    "PHL_COVER": "https://codelibrary.amlegal.com/codes/philadelphia/latest/philadelphia_pa/0-0-0-280805",
    "PHL_NOTICE": "https://codelibrary.amlegal.com/codes/philadelphia/latest/philadelphia_pa/0-0-0-280824",
    "PHL_PAY": "https://codelibrary.amlegal.com/codes/philadelphia/latest/philadelphia_pa/0-0-0-280838",
    "PHL_REST": "https://codelibrary.amlegal.com/codes/philadelphia/latest/philadelphia_pa/0-0-0-280860",
    "PHL_HOURS": "https://codelibrary.amlegal.com/codes/philadelphia/latest/philadelphia_pa/0-0-0-280863",
    "PHL_RECORDS": "https://codelibrary.amlegal.com/codes/philadelphia/latest/philadelphia_pa/0-0-0-280891",
    "PHL_TIPPED": "https://www.phila.gov/media/20260717115727/fair-workweek-tipped-rate-predictability-pay-2026.pdf",
    "OR_PS": "https://www.oregonlegislature.gov/bills_laws/ors/ors653.html",
    "EVA_ORD": "https://cityofevanston.org/Documents/Business/Fair%20Workweek/Correctly%20Amended%2024O23.pdf",
    "EVA_PAGE": "https://cityofevanston.org/departments/city_managers_office/economic_development/fair_workweek.php",
    "BRK_CODE": "https://berkeley.municipal.codes/BMC/13.102",
    "EMV_FAQ": "https://www.emeryville.org/files/assets/city/v/1/development/documents/fair-workweek-ordinance/"
               "fww-faq_updated.july2024.pdf",
})

_DAYS14 = 14 * 24
# Oregon's predictive-scheduling law is in the current ORS; the date it took
# effect was not read on an official source, so none is stated (EXTERNAL).
_OR_START = "In force; its start date was not read on an official source."

ORDINANCES = {
    "IL-CHICAGO": {
        "label": "Chicago", "level": "city", "state": "IL", "places": ["chicago"], "place_label": "Chicago",
        "law": "Chicago's Fair Workweek Ordinance",
        "in_force": [_v(True, S["CHI_CODE"], "2020-07-01")],
        "coverage": {
            "employer": [_v({"employees_min": 250, "locations_min": 30, "scope": "global",
                             "covered_why": "A restaurant employer with 250+ employees and 30+ locations worldwide."},
                            S["CHI_CODE"], "2020-07-01",
                            note="Also at least 50 covered employees; three or fewer Chicago locations owned by one "
                                 "employer under a sole franchise are not covered.")],
            "employee": [_v({"hourly_max": 33.85, "annual_max": 64945.55}, S["CHI_BACP"], "2026-07-01",
                            note="Indexed every July 1.")],
        },
        "rules": {
            "notice_days": [_v(14, S["CHI_NOTICE"], "2022-07-01")],
            "min_rest_hours": [_v(10, S["CHI_REST"], "2020-07-01")],
            "rest_mode": [_v("consent_premium", S["CHI_REST"], "2020-07-01")],
            "rest_premium_multiplier": [_v(1.25, S["CHI_REST"], "2020-07-01")],
            "rest_premium_scope": [_v("shift", S["CHI_REST"], "2020-07-01",
                                      note="1.25x the regular rate for the whole shift that starts within 10 hours.")],
        },
        "predictability_pay": [_v({
            "window_days": 14, "min_change_minutes": None, "consent_exempts": True, "per_change_consent": True,
            "tiers": [{"kinds": ["add", "extend", "move"], "under_hours": _DAYS14, "pay": {"hours": 1}},
                      {"kinds": ["cut", "reduce"], "under_hours": _DAYS14, "at_least_hours": 24, "pay": {"hours": 1}},
                      {"kinds": ["cut", "reduce"], "under_hours": 24, "pay": {"fraction_of_lost": 0.5}}]},
            S["CHI_PAY"], "2020-07-01",
            note="Exempt: a mutually agreed shift trade, a change mutually agreed and confirmed in writing, a change "
                 "the employee asked for in writing, documented discipline, force majeure.")],
        "good_faith_estimate": [_v({"due": "at_hire", "period_days": 90,
                                    "contents": ["average_weekly_hours", "on_call", "days_times"]},
                                   S["CHI_NOTICE"], "2020-07-01")],
        "change_requests": [_v({"answer_days": 3, "applies_to": "estimate"}, S["CHI_NOTICE"], "2020-07-01",
                               note="A written answer within three days to a request to change the estimate.")],
        "access_to_hours": [_v({"to": "existing_qualified", "post_hours": None}, S["CHI_HOURS"], "2020-07-01",
                               note="Offer to qualified existing covered employees first; the offer states its "
                                    "acceptance deadline.")],
        "records_years": [_v(3, S["CHI_RECORDS"], "2020-07-01")],
        "notes": ["Chicago Fair Workweek: 14 days' notice, predictability pay for late changes, 10 hours' rest "
                  "unless the employee agrees (1.25x for that shift), a good-faith estimate at hire, records 3 years."],
    },
    "IL-EVANSTON": {
        "label": "Evanston", "level": "city", "state": "IL", "places": ["evanston"], "place_label": "Evanston",
        "law": "Evanston's Fair Workweek Ordinance",
        "in_force": [_v(True, S["EVA_PAGE"], "2024-01-01")],
        "coverage": {
            "employer": [_v({"employees_min": 200, "locations_min": 30, "scope": "global"}, S["EVA_ORD"],
                            "2024-01-01",
                            note="A franchisee under 100 employees is covered when its network has more than 30 "
                                 "locations; three or fewer Evanston locations of one employer under a sole "
                                 "franchise are not.")],
            "employee": [_v({}, S["EVA_ORD"], "2024-01-01", note="No wage ceiling.")],
        },
        "rules": {
            "notice_days": [_v(14, S["EVA_ORD"], "2024-01-01")],
            "min_rest_hours": [_v(11, S["EVA_ORD"], "2024-01-01")],
            "rest_mode": [_v("consent_premium", S["EVA_ORD"], "2024-01-01")],
            "rest_premium_multiplier": [_v(1.5, S["EVA_ORD"], "2024-01-01")],
            "rest_premium_scope": [_v("hours_inside", S["EVA_ORD"], "2024-01-01")],
        },
        "predictability_pay": [_v({
            "window_days": 14, "min_change_minutes": None, "consent_exempts": True,
            "tiers": [{"kinds": ["add", "extend", "move", "cut", "reduce"], "under_hours": _DAYS14,
                       "at_least_hours": 24, "pay": {"hours": 1}},
                      {"kinds": ["add", "extend", "move"], "under_hours": 24, "pay": {"hours": 1}},
                      {"kinds": ["cut", "reduce"], "under_hours": 24, "pay": {"min_hours_of": 4, "or": "shift_hours"}}]},
            S["EVA_ORD"], "2024-01-01")],
        "good_faith_estimate": [_v({"due": "at_hire", "period_days": 90, "contents": ["average_weekly_hours", "days_times"]},
                                   S["EVA_ORD"], "2024-01-01")],
        "change_requests": [_v({"answer_days": 3, "applies_to": "estimate"}, S["EVA_ORD"], "2024-01-01")],
        "access_to_hours": [_v({"to": "part_time", "accept_hours": 72, "accept_hours_short_work": 24},
                               S["EVA_ORD"], "2024-01-01",
                               note="72 hours to accept work longer than two weeks, 24 hours for two weeks or less.")],
        "records_years": [_v(3, S["EVA_ORD"], "2024-01-01")],
    },
    "NY-NYC": {
        "label": "New York City", "level": "city", "state": "NY",
        "places": ["new york", "new york city", "nyc", "manhattan", "brooklyn", "queens", "bronx", "the bronx",
                   "staten island"], "place_label": "New York City",
        "law": "New York City's Fair Workweek Law (fast food)",
        "in_force": [_v(True, S["NYC_NOTICE"], "2017-11-26")],
        "coverage": {
            "employer": [_v({"locations_min": 30, "scope": "national", "kinds": ["fast_food"],
                             "kind_question": "Is the restaurant fast food (limited service, guests pay before eating)?",
                             "kind_why": "It covers fast food establishments only; full-service restaurants are not "
                                         "covered."}, S["NYC_COVER"], "2017-11-26",
                            note="One of 30+ establishments nationally, a franchise network counted together.")],
            "employee": [_v({"salaried_excluded": True}, S["NYC_COVER"], "2017-11-26")],
        },
        "rules": {
            "notice_days": [_v(14, S["NYC_NOTICE"], "2017-11-26")],
            "min_rest_hours": [_v(11, S["NYC_REST"], "2017-11-26")],
            "rest_mode": [_v("consent_premium", S["NYC_REST"], "2017-11-26",
                             note="A clopening needs the employee's written request or consent.")],
            "rest_premium_flat": [_v(100, S["NYC_REST"], "2017-11-26", note="$100 for each instance.")],
            "rest_premium_scope": [_v("instance", S["NYC_REST"], "2017-11-26")],
        },
        "predictability_pay": [_v({
            "window_days": 14, "min_change_minutes": 15, "consent_exempts": False,
            "tiers": [{"kinds": ["add", "extend", "move"], "under_hours": _DAYS14, "at_least_hours": 7 * 24,
                       "pay": {"flat": 10}},
                      {"kinds": ["cut", "reduce"], "under_hours": _DAYS14, "at_least_hours": 7 * 24, "pay": {"flat": 20}},
                      {"kinds": ["add", "extend", "move"], "under_hours": 7 * 24, "pay": {"flat": 15}},
                      {"kinds": ["cut", "reduce"], "under_hours": 7 * 24, "at_least_hours": 24, "pay": {"flat": 45}},
                      {"kinds": ["cut", "reduce"], "under_hours": 24, "pay": {"flat": 75}}]},
            S["NYC_PAY"], "2017-11-26",
            note="Consent does not waive the premium (rule 7-622); a change the employee asked for in writing and a "
                 "voluntary trade are exempt. A change of 15 minutes or less is not one.")],
        "good_faith_estimate": [_v({"due": "first_schedule", "contents": ["regular_schedule"]}, S["NYC_NOTICE"],
                                   "2017-11-26")],
        "access_to_hours": [_v({"to": "existing", "post_hours": 72}, S["NYC_COVER"], "2017-11-26",
                               note="Posted for three consecutive calendar days and sent electronically.")],
        "records_years": [_v(3, S["NYC_RECORDS"], "2017-11-26")],
    },
    "WA-SEATTLE": {
        "label": "Seattle", "level": "city", "state": "WA", "places": ["seattle"], "place_label": "Seattle",
        "law": "Seattle's Secure Scheduling Ordinance",
        "in_force": [_v(True, S["SEA_CODE"], "2017-07-01")],
        "coverage": {
            "employer": [_v({"employees_min": 500, "scope": "global",
                             "full_service_locations_min": 40}, S["SEA_CODE"], "2017-07-01",
                            note="A full-service restaurant also needs 40+ full-service locations worldwide; a "
                                 "franchise network is counted together.")],
            "employee": [_v({"salaried_excluded": True}, S["SEA_CODE"], "2017-07-01",
                            note="Employees who work in Seattle at least half the time.")],
        },
        "rules": {
            "notice_days": [_v(14, S["SEA_CODE"], "2017-07-01")],
            "min_rest_hours": [_v(10, S["SEA_CODE"], "2017-07-01")],
            "rest_mode": [_v("consent_premium", S["SEA_CODE"], "2017-07-01")],
            "rest_premium_multiplier": [_v(1.5, S["SEA_CODE"], "2017-07-01")],
            "rest_premium_scope": [_v("hours_inside", S["SEA_CODE"], "2017-07-01")],
        },
        "predictability_pay": [_v({
            "window_days": 14, "min_change_minutes": None, "consent_exempts": True,
            "tiers": [{"kinds": ["add", "extend", "move"], "under_hours": _DAYS14, "pay": {"hours": 1}},
                      {"kinds": ["cut", "reduce"], "under_hours": _DAYS14, "pay": {"fraction_of_lost": 0.5}}]},
            S["SEA_CODE"], "2017-07-01",
            note="Exempt: mutual swaps, voluntary hours offered by written mass communication, changes the employee "
                 "asked for in writing, accepted access-to-hours offers, discipline, force majeure.")],
        "good_faith_estimate": [_v({"due": "at_hire", "revise": "yearly",
                                    "contents": ["median_hours", "on_call"]}, S["SEA_CODE"], "2017-07-01")],
        "access_to_hours": [_v({"to": "existing", "post_hours": 72}, S["SEA_CODE"], "2017-07-01",
                               note="Posted for at least three consecutive calendar days.")],
        "records_years": [_v(3, S["SEA_CODE"], "2017-07-01")],
    },
    "CA-SF": {
        "label": "San Francisco", "level": "city", "state": "CA", "places": ["san francisco", "sf"],
        "place_label": "San Francisco", "law": "San Francisco's Formula Retail Employee Rights Ordinances",
        "in_force": [_v(True, S["SF_COVER"], "2015-07-03")],
        "coverage": {
            "employer": [_v({"locations_min": 40, "scope": "global", "employees_here_min": 20}, S["SF_COVER"],
                            "2015-07-03", note="Formula retail uses include restaurants (SF Planning).")],
        },
        "rules": {"notice_days": [_v(14, S["SF_PAY"], "2015-07-03", note="Two weeks.")]},
        "predictability_pay": [_v({
            "window_days": 7, "min_change_minutes": None, "consent_exempts": False,
            "tiers": [{"kinds": ["add", "extend", "move", "cut", "reduce"], "under_hours": 7 * 24, "at_least_hours": 24,
                       "pay": {"hours": 1}},
                      {"kinds": ["add", "extend", "move", "cut", "reduce"], "under_hours": 24,
                       "pay": {"hours_by_shift": [[4, 2], [None, 4]]}}]},
            S["SF_PAY"], "2015-07-03",
            note="Exempt: a trade or a change the employee asked for, cover for another's absence on under 7 days' "
                 "notice, mandatory overtime, force majeure.")],
        "good_faith_estimate": [_v({"due": "before_start", "contents": ["min_shifts_month"]}, S["SF_PAY"], "2015-07-03")],
        "access_to_hours": [_v({"to": "part_time", "accept_hours": 72}, S["SF_HOURS"], "2015-07-03")],
        "records_years": [_v(3, S["SF_RECORDS"], "2015-07-03")],
    },
    "CA-LA": {
        "label": "Los Angeles", "level": "city", "state": "CA", "places": ["los angeles"], "place_label": "Los Angeles",
        "law": "Los Angeles's Fair Work Week Ordinance",
        "in_force": [_v(True, S["LA_FWW"], "2023-04-01")],
        "coverage": {"employer": [_v({"kinds": ["retail"], "kind_why": "It covers retail businesses only; "
                                      "restaurants are not covered."}, S["LA_FWW"], "2023-04-01")]},
        "rules": {},
    },
    "CA-BERKELEY": {
        "label": "Berkeley", "level": "city", "state": "CA", "places": ["berkeley"], "place_label": "Berkeley",
        "law": "Berkeley's Fair Workweek Ordinance",
        "in_force": [_v(True, S["BRK_CODE"], "2024-01-12")],
        "coverage": {
            "employer": [_v({"employees_min": 100, "scope": "global", "employees_here_min": 10}, S["BRK_CODE"],
                            "2024-01-12", note="A franchisee counts its network's employees together.")],
            "employee": [_v({"salaried_excluded": True}, S["BRK_CODE"], "2024-01-12")],
        },
        "rules": {
            "notice_days": [_v(14, S["BRK_CODE"], "2024-01-12")],
            "min_rest_hours": [_v(11, S["BRK_CODE"], "2024-01-12")],
            "rest_mode": [_v("consent_premium", S["BRK_CODE"], "2024-01-12")],
            "rest_premium_multiplier": [_v(1.5, S["BRK_CODE"], "2024-01-12")],
            "rest_premium_scope": [_v("hours_inside", S["BRK_CODE"], "2024-01-12")],
        },
        "predictability_pay": [_v({
            "window_days": 14, "min_change_minutes": 10, "consent_exempts": False,
            "tiers": [{"kinds": ["add", "extend", "move", "cut", "reduce"], "under_hours": _DAYS14,
                       "at_least_hours": 24, "pay": {"hours": 1}},
                      {"kinds": ["add", "extend", "move"], "under_hours": 24, "pay": {"hours": 1}},
                      {"kinds": ["cut", "reduce"], "under_hours": 24, "pay": {"min_hours_of": 4, "or": "lost_hours"}}]},
            S["BRK_CODE"], "2024-01-12",
            note="Exempt: mutual swaps, employee-initiated changes, staying up to 30 minutes to finish serving, a "
                 "start or end within 10 minutes, force majeure.")],
        "good_faith_estimate": [_v({"due": "before_start", "contents": ["schedule"]}, S["BRK_CODE"], "2024-01-12")],
        "access_to_hours": [_v({"to": "part_time", "accept_hours": 24}, S["BRK_CODE"], "2024-01-12")],
        "records_years": [_v(3, S["BRK_CODE"], "2024-01-12")],
    },
    "CA-EMERYVILLE": {
        "label": "Emeryville", "level": "city", "state": "CA", "places": ["emeryville"], "place_label": "Emeryville",
        "law": "Emeryville's Fair Workweek Ordinance",
        "in_force": [_v(True, S["EMV_FAQ"], "2017-07-01")],
        "coverage": {
            "employer": [_v({"employees_min": 56, "scope": "global", "employees_here_min": 20, "kinds": ["fast_food"],
                             "kind_why": "It covers fast food firms only; full-service restaurants are not covered."},
                            S["EMV_FAQ"], "2017-07-01",
                            note="Franchisees in a network of more than 12 locations are covered.")],
            "employee": [_v({"salaried_excluded": True}, S["EMV_FAQ"], "2017-07-01")],
        },
        "rules": {
            "notice_days": [_v(14, S["EMV_FAQ"], "2017-07-01", note="Two weeks.")],
            "min_rest_hours": [_v(11, S["EMV_FAQ"], "2017-07-01")],
            "rest_mode": [_v("consent_premium", S["EMV_FAQ"], "2017-07-01",
                             note="Consent form requirements were not confirmed; consent is asked for anyway.")],
            "rest_premium_multiplier": [_v(1.5, S["EMV_FAQ"], "2017-07-01")],
            "rest_premium_scope": [_v("hours_inside", S["EMV_FAQ"], "2017-07-01")],
        },
        "predictability_pay": [_v({
            "window_days": 14, "min_change_minutes": 10, "consent_exempts": False,
            "tiers": [{"kinds": ["add", "extend", "move", "cut", "reduce"], "under_hours": _DAYS14,
                       "at_least_hours": 24, "pay": {"hours": 1}},
                      {"kinds": ["add", "extend", "move"], "under_hours": 24, "pay": {"hours": 1}},
                      {"kinds": ["cut", "reduce"], "under_hours": 24, "pay": {"min_hours_of": 4, "or": "shift_hours"}}]},
            S["EMV_FAQ"], "2017-07-01",
            note="Asking volunteers to go home early still owes it (city FAQ).")],
        "good_faith_estimate": [_v({"due": "before_start", "contents": ["schedule"]}, S["EMV_FAQ"], "2017-07-01")],
        "access_to_hours": [_v({"to": "part_time", "accept_hours": None}, S["EMV_FAQ"], "2017-07-01",
                               note="Offered in increments of at least 4 hours; the acceptance window was not "
                                    "confirmed.")],
        "records_years": [_v(3, S["EMV_FAQ"], "2017-07-01")],
    },
    "PA-PHILADELPHIA": {
        "label": "Philadelphia", "level": "city", "state": "PA", "places": ["philadelphia"],
        "place_label": "Philadelphia", "law": "Philadelphia's Fair Workweek Employment Standards",
        "in_force": [_v(True, S["PHL_COVER"], "2020-04-01")],
        "coverage": {
            "employer": [_v({"employees_min": 250, "locations_min": 30, "scope": "global"}, S["PHL_COVER"],
                            "2020-04-01", note="Chains and franchises counted together.")],
            "employee": [_v({"salaried_excluded": True}, S["PHL_COVER"], "2020-04-01",
                            note="Overtime-eligible (non-exempt) employees.")],
        },
        "rules": {
            "notice_days": [_v(14, S["PHL_NOTICE"], "2021-01-01")],
            "min_rest_hours": [_v(9, S["PHL_REST"], "2020-04-01")],
            "rest_mode": [_v("consent_premium", S["PHL_REST"], "2020-04-01")],
            "rest_premium_flat": [_v(40, S["PHL_REST"], "2020-04-01", note="$40 for each such shift.")],
            "rest_premium_scope": [_v("instance", S["PHL_REST"], "2020-04-01")],
        },
        "predictability_pay": [_v({
            "window_days": 14, "min_change_minutes": 20, "consent_exempts": False,
            "tiers": [{"kinds": ["add", "extend", "move"], "under_hours": _DAYS14, "pay": {"hours": 1}},
                      {"kinds": ["cut", "reduce"], "under_hours": _DAYS14, "pay": {"fraction_of_lost": 0.5}}],
            "tipped_rate": {"below": 7.25, "rate": 17.88, "from": "2026-07-01", "until": "2027-07-01",
                            "source": S["PHL_TIPPED"]}},
            S["PHL_PAY"], "2021-01-01",
            note="Exempt: a change the employee asked for in writing, a mutual trade, a start or end within 20 "
                 "minutes, voluntary hours offered by written mass communication, force majeure.")],
        "good_faith_estimate": [_v({"due": "at_hire", "period_days": 90,
                                    "contents": ["average_weekly_hours", "on_call", "days_times"]},
                                   S["PHL_NOTICE"], "2020-04-01")],
        "access_to_hours": [_v({"to": "existing", "post_hours": 72}, S["PHL_HOURS"], "2020-04-01")],
        "records_years": [_v(2, S["PHL_RECORDS"], "2020-04-01")],
    },
    "OR-PREDICTIVE": {
        "label": "Oregon predictive scheduling", "level": "state", "state": "OR", "places": None,
        "place_label": "Oregon", "law": "Oregon's predictive scheduling law (ORS 653.412-653.485)",
        "in_force": [_v(True, S["OR_PS"], None, note="In force; its start date was not read on an official source.")],
        "coverage": {
            "employer": [_v({"employees_min": 500, "scope": "global"}, S["OR_PS"], None,
                            note="Food services employers with 500+ employees worldwide, a chain or integrated "
                                 "enterprise counted together.")],
            "employee": [_v({"salaried_excluded": True}, S["OR_PS"], None, note=_OR_START)],
        },
        "rules": {
            "notice_days": [_v(14, S["OR_PS"], None, note=_OR_START)],
            "min_rest_hours": [_v(10, S["OR_PS"], None, note=_OR_START)],
            "rest_mode": [_v("consent_premium", S["OR_PS"], None, note=_OR_START)],
            "rest_premium_multiplier": [_v(1.5, S["OR_PS"], None, note=_OR_START)],
            "rest_premium_scope": [_v("hours_inside", S["OR_PS"], None, note=_OR_START)],
        },
        "predictability_pay": [_v({
            "window_days": 14, "min_change_minutes": 30, "consent_exempts": True,
            "tiers": [{"kinds": ["add", "extend", "move"], "under_hours": _DAYS14, "pay": {"hours": 1}},
                      {"kinds": ["cut", "reduce"], "under_hours": _DAYS14, "pay": {"fraction_of_lost": 0.5}}]},
            S["OR_PS"], None,
            note="Exempt: a start or end moved 30 minutes or less, mutual swaps, a change the employee asked for in "
                 "writing, the voluntary standby list, hours added for unanticipated needs the employee consents to "
                 "in writing, force majeure.")],
        "good_faith_estimate": [_v({"due": "at_hire", "contents": ["median_monthly_hours"]}, S["OR_PS"], None, note=_OR_START)],
        "records_years": [_v(3, S["OR_PS"], None, note=_OR_START)],
    },
}

EXTERNAL_ORDINANCES = [
    {"what": "Chicago: the 6/1/26 rules' 15-minute threshold for predictability pay, per-change consent and "
             "time-stamped postings",
     "why": "Read only from an excerpt of the official rules PDF (chicago.gov refused the fetch). Not applied: every "
            "change is priced until counsel confirms the threshold."},
    {"what": "Chicago: the access-to-hours posting period", "why": "No fixed period was found; the owner sets the "
                                                                    "offer's deadline."},
    {"what": "Chicago: covered-employee wage ceilings before 7/1/26", "why": "Only the 7/1/26 figures were read "
                                                                              "on chicago.gov."},
    {"what": "Oregon predictive scheduling start date", "why": "Not read on an official source; the law is in force."},
    {"what": "Emeryville: the rest consent form and the access-to-hours acceptance window",
     "why": "Not stated in the city's FAQ or notice."},
    {"what": "Written answers to schedule-change requests outside Chicago and Evanston",
     "why": "Seattle, Philadelphia, Oregon and Emeryville set no deadline that could be confirmed."},
    {"what": "Massachusetts 14-15 earliest start and school-year end time",
     "why": "MGL c.149 §60/§65 and the state's guide disagree (6:30 vs 7am; 6 vs 7pm)."},
    {"what": "New Jersey 16-17 summer hours and minors' meal break",
     "why": "The statute and the Department's page disagree (10h/50h vs 8h/40h; 5 vs 6 hours)."},
    {"what": "Pennsylvania 16-17 weekly maximum outside school", "why": "The Department's pages say 44 and 45."},
    {"what": "Minor-specific meal rules for California, New York, Texas, Georgia and Arizona",
     "why": "None found on an official source."},
]

# State minor tables: {state: {band: {rule: value, "source", "sources"}}}
# — merged over the federal floor by schedule_rules.minor_rules, which keeps
# whichever value is STRICTER (a 14-15 year old at an employer the federal
# rule covers is held to both). Keys:
#   earliest_start, latest_end_school / latest_end_summer (date-based; the
#   state's own summer by summer_from "MM-DD" / summer_until "MM-DD" or
#   "labor_day"), latest_end_before_school_day / _before_non_school_day,
#   max_daily_school_day, max_daily_other_day, max_daily_in_school_week
#   (every day of a school week), max_weekly_school_week,
#   max_weekly_other_week, max_days_week, meal {after_hours, minutes,
#   only_if_day_hours}.
# Each value was read on the official source in `sources` on REVIEWED_ON;
# where two official sources disagree the value is left out (the federal
# floor or the owner's minor rule holds) and the conflict is in EXTERNAL.
# "School day" in these laws follows the minor's own school or district;
# the restaurant's school calendar (school_calendar) stands in for it.

def _msrc(url, label, effective=None):
    return {"url": url, "label": label, "effective": effective, "checked": REVIEWED_ON}


_IL_CL = _msrc("https://ftp.ilga.gov/ILCS/Ch%200820/Act%200206/", "820 ILCS 206 (Child Labor Law of 2024)",
               "2025-01-01")
_CA_CL = _msrc("https://www.dir.ca.gov/dlse/MinorsSummaryCharts.pdf", "DLSE minors summary chart (Labor Code 1391)")
_NY142 = _msrc("https://www.nysenate.gov/legislation/laws/LAB/142", "NY Labor Law 142")
_NY143 = _msrc("https://www.nysenate.gov/legislation/laws/LAB/143", "NY Labor Law 143")
_WA_CL = _msrc("https://app.leg.wa.gov/wac/default.aspx?cite=296-125-101", "WAC 296-125-101/-111/-121",
               "2026-07-01")
_OR_CL = _msrc("https://secure.sos.state.or.us/oard/displayDivisionRules.action?selectedDivision=3846",
               "OAR 839-021")
_MA_GUIDE = _msrc("https://www.mass.gov/info-details/massachusetts-laws-regulating-minors-work-hours",
                  "mass.gov: laws regulating minors' work hours")
_MA_67 = _msrc("https://malegislature.gov/Laws/GeneralLaws/PartI/TitleXXI/Chapter149/Section67", "MGL c.149 §66-67")
_TX_CL = _msrc("https://www.twc.texas.gov/sites/default/files/ui/docs/chapter-51-employment-of-children-twc.pdf",
               "Texas Labor Code 51.013", "2021-09-01")
_FL_CL = _msrc("http://www.leg.state.fl.us/statutes/index.cfm?App_mode=Display_Statute&URL=0400-0499/0450/"
               "Sections/0450.081.html", "Fla. Stat. 450.081")
_PA_CL = _msrc("https://www.pa.gov/agencies/dli/resources/compliance-laws-and-regulations/labor-management-relations/"
               "child-labor-act.html", "PA L&I: Child Labor Act")
_OH_CL = _msrc("https://codes.ohio.gov/ohio-revised-code/section-4109.07", "ORC 4109.07", "2026-10-09")
_MI_110 = _msrc("https://www.legislature.mi.gov/Laws/MCL?objectName=mcl-409-110", "MCL 409.110", "2025-04-02")
_MI_111 = _msrc("https://www.legislature.mi.gov/Laws/MCL?objectName=mcl-409-111", "MCL 409.111-.112")
_GA_CL = _msrc("https://dol.georgia.gov/document/child-labor/title-39-regulation-employment-minors/download",
               "O.C.G.A. 39-2-3, 39-2-7")
_NC_CL = _msrc("https://www.ncleg.gov/EnactedLegislation/Statutes/HTML/BySection/Chapter_95/GS_95-25.5.html",
               "G.S. 95-25.5")
_NJ_CL = _msrc("https://www.nj.gov/labor/wageandhour/tools-resources/laws/childlaborlaws.shtml",
               "N.J.S.A. 34:2-21.3")
_AZ_CL = _msrc("https://www.azleg.gov/ars/23/00233.htm", "A.R.S. 23-233")
_VA_CL = _msrc("https://law.lis.virginia.gov/admincode/title16/agency15/chapter40/section30/", "16VAC15-40-30")


def _band(src, **rules):
    return dict(rules, sources=[src], source=src["label"])


STATE_MINOR_RULES = {
    "IL": {"14-15": _band(_IL_CL, earliest_start="7:00am", latest_end_school="7:00pm", latest_end_summer="9:00pm",
                          max_daily_school_day=3.0, max_daily_other_day=8.0, max_weekly_school_week=18.0,
                          max_weekly_other_week=40.0, meal={"after_hours": 5.0, "minutes": 30})},
    "CA": {"14-15": _band(_CA_CL, earliest_start="7:00am", latest_end_school="7:00pm", latest_end_summer="9:00pm",
                          max_daily_school_day=3.0, max_daily_other_day=8.0, max_weekly_school_week=18.0,
                          max_weekly_other_week=40.0),
           "16-17": _band(_CA_CL, earliest_start="5:00am", latest_end_before_school_day="10:00pm",
                          latest_end_before_non_school_day="12:30am", max_daily_school_day=4.0,
                          max_daily_other_day=8.0, max_weekly_school_week=48.0, max_weekly_other_week=48.0)},
    "NY": {"14-15": _band(_NY142, earliest_start="7:00am", latest_end_school="7:00pm", latest_end_summer="9:00pm",
                          summer_from="06-21", max_daily_school_day=3.0, max_daily_other_day=8.0,
                          max_weekly_school_week=18.0, max_weekly_other_week=40.0, max_days_week=6),
           "16-17": _band(_NY143, earliest_start="6:00am", latest_end_before_school_day="10:00pm",
                          latest_end_before_non_school_day="12:00am", max_daily_school_day=4.0,
                          max_daily_other_day=8.0, max_weekly_school_week=28.0, max_weekly_other_week=48.0,
                          max_days_week=6,
                          note="Midnight before a day off school needs parental consent during the school year; "
                               "the law's 4 hours is for a day before a school day (Friday is held to it too).")},
    "WA": {"14-15": _band(_WA_CL, earliest_start="7:00am", latest_end_before_school_day="7:00pm",
                          latest_end_before_non_school_day="9:00pm", max_daily_school_day=3.0,
                          max_daily_other_day=8.0, max_weekly_school_week=16.0, max_weekly_other_week=40.0,
                          max_days_week=6, meal={"after_hours": 4.0, "minutes": 30},
                          note="The 3-hour cap is for a school day before another school day; Friday is held to it "
                               "too. A responsible adult supervises after 8pm."),
           "16-17": _band(_WA_CL, earliest_start="7:00am", latest_end_before_school_day="10:00pm",
                          latest_end_before_non_school_day="12:00am", max_daily_school_day=4.0,
                          max_daily_other_day=8.0, max_weekly_school_week=20.0, max_weekly_other_week=48.0,
                          max_days_week=6, meal={"after_hours": 5.0, "minutes": 30},
                          note="5am starts are allowed in weeks school is out; 7am is held every week.")},
    "OR": {"14-15": _band(_OR_CL, earliest_start="7:00am", latest_end_school="7:00pm", latest_end_summer="9:00pm",
                          max_daily_school_day=3.0, max_daily_other_day=8.0, max_weekly_school_week=18.0,
                          max_weekly_other_week=40.0),
           "16-17": _band(_OR_CL, max_weekly_school_week=44.0, max_weekly_other_week=44.0)},
    "MA": {"14-15": _band(_MA_GUIDE, summer_from="07-01", max_daily_school_day=3.0, max_daily_other_day=8.0,
                          max_weekly_school_week=18.0, max_weekly_other_week=40.0, max_days_week=6,
                          latest_end_summer="9:00pm",
                          note="The statute and the state's guide disagree on the earliest start and the school-year "
                               "end time; the federal 7am-7pm holds until counsel confirms (EXTERNAL)."),
           "16-17": _band(_MA_67, earliest_start="6:00am", latest_end_before_school_day="10:00pm",
                          latest_end_before_non_school_day="12:00am", max_daily_school_day=9.0,
                          max_daily_other_day=9.0, max_weekly_school_week=48.0, max_weekly_other_week=48.0,
                          max_days_week=6,
                          note="Restaurants may keep a 16-17 year old until midnight on a night not before a school "
                               "day; an adult supervises after 8pm.")},
    "TX": {"14-15": _band(_TX_CL, max_daily_school_day=8.0, max_daily_other_day=8.0, max_weekly_school_week=48.0,
                          max_weekly_other_week=48.0, latest_end_before_school_day="10:00pm",
                          note="Where the federal rule covers the employer its stricter limits hold.")},
    "FL": {"14-15": _band(_FL_CL, earliest_start="7:00am", latest_end_before_school_day="7:00pm",
                          latest_end_before_non_school_day="9:00pm", max_daily_school_day=3.0,
                          max_daily_other_day=8.0, max_weekly_school_week=15.0, max_weekly_other_week=40.0,
                          meal={"after_hours": 4.0, "minutes": 30}),
           "16-17": _band(_FL_CL, earliest_start="6:30am", latest_end_before_school_day="11:00pm",
                          max_daily_school_day=8.0, max_weekly_school_week=30.0,
                          meal={"after_hours": 4.0, "minutes": 30, "only_if_day_hours": 8.0},
                          note="A parent or the school superintendent may waive the 30-hour school week.")},
    "PA": {"14-15": _band(_PA_CL, earliest_start="7:00am", latest_end_school="7:00pm", latest_end_summer="9:00pm",
                          max_daily_school_day=3.0, max_daily_other_day=8.0, max_weekly_school_week=18.0,
                          max_weekly_other_week=40.0, meal={"after_hours": 5.0, "minutes": 30},
                          note="From the Department of Labor & Industry's pages; the statute text was not readable."),
           "16-17": _band(_PA_CL, earliest_start="6:00am", latest_end_school="12:00am", max_daily_school_day=8.0,
                          max_daily_other_day=10.0, max_weekly_school_week=28.0, max_weekly_other_week=48.0,
                          meal={"after_hours": 5.0, "minutes": 30})},
    "OH": {"14-15": _band(_OH_CL, earliest_start="7:00am", latest_end_school="7:00pm", latest_end_summer="9:00pm",
                          summer_until="09-01", max_daily_school_day=3.0, max_daily_other_day=8.0,
                          max_weekly_school_week=18.0, max_weekly_other_week=40.0,
                          meal={"after_hours": 5.0, "minutes": 30}),
           "16-17": _band(_OH_CL, latest_end_before_school_day="11:00pm", meal={"after_hours": 5.0, "minutes": 30},
                          note="Applies to 16-17 year olds required to attend school; no start before 7am on a "
                               "school day (6am if they did not work past 8pm the night before).")},
    "MI": {"14-15": _band(_MI_110, earliest_start="7:00am", latest_end_school="7:00pm", latest_end_summer="9:00pm",
                          max_daily_in_school_week=3.0, max_weekly_school_week=18.0, max_weekly_other_week=40.0,
                          meal={"after_hours": 5.0, "minutes": 30}),
           "16-17": _band(_MI_111, earliest_start="6:00am", latest_end_before_school_day="10:30pm",
                          latest_end_before_non_school_day="11:30pm", max_daily_school_day=10.0,
                          max_daily_other_day=10.0, max_weekly_school_week=24.0, max_weekly_other_week=48.0,
                          max_days_week=6, meal={"after_hours": 5.0, "minutes": 30},
                          note="24 hours in a school week applies to students.")},
    "GA": {"14-15": _band(_GA_CL, earliest_start="6:00am", latest_end_school="9:00pm", latest_end_summer="9:00pm",
                          max_daily_school_day=4.0, max_daily_other_day=8.0, max_weekly_school_week=40.0,
                          max_weekly_other_week=40.0)},
    "NC": {"14-15": _band(_NC_CL, earliest_start="7:00am", latest_end_school="7:00pm", latest_end_summer="9:00pm",
                          max_daily_school_day=3.0, max_daily_other_day=8.0, max_weekly_school_week=18.0,
                          max_weekly_other_week=40.0, meal={"after_hours": 5.0, "minutes": 30}),
           "16-17": _band(_NC_CL, latest_end_before_school_day="11:00pm",
                          note="No work 11pm-5am before a school day unless a parent and the principal approve "
                               "in writing.")},
    "NJ": {"14-15": _band(_NJ_CL, earliest_start="7:00am", latest_end_school="7:00pm", latest_end_summer="9:00pm",
                          max_daily_school_day=3.0, max_daily_other_day=8.0, max_weekly_school_week=18.0,
                          max_weekly_other_week=40.0),
           "16-17": _band(_NJ_CL, earliest_start="6:00am", latest_end_school="11:00pm", latest_end_summer="11:00pm",
                          max_daily_school_day=8.0, max_daily_other_day=8.0, max_weekly_school_week=40.0,
                          max_weekly_other_week=40.0,
                          note="The statute allows 10 hours a day and 50 a week in the summer (21.3(b)); held to "
                               "8 and 40 until counsel confirms.")},
    "AZ": {"14-15": _band(_AZ_CL, max_daily_school_day=3.0, max_daily_other_day=8.0, max_weekly_school_week=18.0,
                          max_weekly_other_week=40.0)},
    "VA": {"14-15": _band(_VA_CL, earliest_start="7:00am", latest_end_school="7:00pm", latest_end_summer="9:00pm",
                          max_daily_school_day=3.0, max_daily_other_day=8.0, max_weekly_school_week=18.0,
                          max_weekly_other_week=40.0, meal={"after_hours": 5.0, "minutes": 30})},
}

# What the library deliberately does NOT state because it could not be
# confirmed against an official source — for counsel (never guessed).
EXTERNAL = [
    {"what": "California split-shift premium and New York spread-of-hours premium in dollars",
     "why": "Both are 'one hour at the minimum wage'; the minimum wage that applies (state, county or city) "
            "is not on file per restaurant."},
    {"what": "Kentucky meal period length", "why": "KRS 337.355 says only 'a reasonable period for lunch'."},
    {"what": "Nebraska meal period", "why": "Neb. Rev. Stat. 48-212 covers assembling plants, workshops and "
                                            "mechanical establishments, likely not restaurants."},
]

ALL_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

# Kept for callers that list or validate the state packs by code.
PACKS = {k: v for k, v in JURISDICTIONS.items() if v.get("level") == "state"}


# ── reading the library ────────────────────────────────────────────────────

def _d(value):
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def value_on(versions, on=None):
    """The version of a sourced value in force on `on` (the latest whose
    effective date has come and whose `until` has not), or None."""
    if not versions:
        return None
    if isinstance(versions, dict):
        versions = [versions]
    d = _d(on)
    best = None
    for v in versions:
        try:
            eff = _d(v.get("effective") or "1900-01-01")
            until = _d(v["until"]) if v.get("until") else None
        except (TypeError, ValueError):
            continue
        if eff <= d and (until is None or d < until):
            if best is None or eff >= _d(best.get("effective") or "1900-01-01"):
                best = v
    return best


def entry(code):
    code = (code or "").strip().upper()
    return JURISDICTIONS.get(code) or ORDINANCES.get(code)


def available() -> list:
    """The state packs a restaurant can pick, by label."""
    return [{"code": k, "label": v["label"]} for k, v in sorted(PACKS.items(), key=lambda kv: kv[1]["label"])]


def minor_table(state) -> dict:
    """{band: {rule: value, "source", "sources"}} for a state, {} when the
    library has none (the federal floor alone then holds)."""
    return STATE_MINOR_RULES.get((state or "").strip().upper()) or {}


def _norm_city(city) -> str:
    c = " ".join(str(city or "").lower().replace(".", "").split())
    for pre in ("city of ", "village of ", "town of "):
        if c.startswith(pre):
            c = c[len(pre):]
    return c.replace("saint ", "st ")


# ── the restaurant's compliance profile ────────────────────────────────────
#
# restaurants.compliance_profile_json: {"state", "city", "employees" (the
# employer's total, every location, worldwide — what the ordinances count),
# "locations" (likewise), "kind" ("full_service" | "fast_food" | "other"),
# "franchise" (bool), "confirmed_at", "confirmed_by"}. Before an owner
# confirms one, the state is the pack they picked (restaurants.jurisdiction)
# and a suggestion is read from the place Google has on file.

PROFILE_KINDS = ("full_service", "fast_food", "other")


def _stored_profile(restaurant) -> dict:
    raw = getattr(restaurant, "compliance_profile_json", None) if restaurant is not None else None
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def suggested_place(restaurant) -> dict:
    """{"state", "city", "from"} read from what is already on file — the
    city and state Google lists for the restaurant's Place ID
    (aivis_city_json), else the mailing address — for the owner to
    confirm. No paid lookup."""
    try:
        stored = json.loads(getattr(restaurant, "aivis_city_json", None) or "{}") or {}
    except (TypeError, ValueError):
        stored = {}
    if isinstance(stored, dict) and stored.get("city") and stored.get("state"):
        st = str(stored["state"]).strip().upper()
        if st in ALL_STATES:
            return {"state": st, "city": str(stored["city"]).strip(), "from": "Google's listing"}
    addr = (getattr(restaurant, "mailing_address", None) or "").strip()
    if addr:
        import re
        m = re.search(r"([A-Za-z .'-]+),\s*([A-Z]{2})\b\s*\d{5}", addr)
        if m and m.group(2) in ALL_STATES:
            return {"state": m.group(2), "city": m.group(1).strip().split(",")[-1].strip(),
                    "from": "your mailing address"}
    return {}


def profile(restaurant) -> dict:
    """The facts coverage is decided on, as stored, with the state falling
    back to the picked pack and a suggestion from the place on file."""
    p = _stored_profile(restaurant)
    out = {"state": (p.get("state") or getattr(restaurant, "jurisdiction", None) or "").strip().upper() or None,
           "city": (p.get("city") or "").strip() or None,
           "employees": _int(p.get("employees")), "locations": _int(p.get("locations")),
           "employees_here": _int(p.get("employees_here")),
           "kind": p.get("kind") if p.get("kind") in PROFILE_KINDS else None,
           "franchise": bool(p.get("franchise")) if p.get("franchise") is not None else None,
           "confirmed_at": p.get("confirmed_at"), "confirmed_by": p.get("confirmed_by")}
    out["confirmed"] = bool(out["confirmed_at"])
    sug = suggested_place(restaurant) if restaurant is not None else {}
    out["suggested"] = sug or None
    if not out["state"] and sug.get("state"):
        out["state_from"] = sug.get("from")
    return out


def _int(v):
    try:
        n = int(float(v))
        return n if n >= 0 else None
    except (TypeError, ValueError):
        return None


def clean_profile(data) -> dict:
    """A profile save, checked. Raises ValueError with the owner's sentence."""
    if not isinstance(data, dict):
        raise ValueError("Send the restaurant's state, city and size.")
    st = str(data.get("state") or "").strip().upper()
    if st and st not in ALL_STATES:
        raise ValueError("Pick the state by its two-letter code.")
    city = " ".join(str(data.get("city") or "").split())[:80]
    out = {"state": st or None, "city": city or None}
    for k in ("employees", "locations", "employees_here"):
        if data.get(k) in (None, ""):
            out[k] = None
            continue
        n = _int(data.get(k))
        if n is None or n > 5_000_000:
            raise ValueError({"employees": "Employees", "locations": "Locations",
                              "employees_here": "Employees in this city"}[k] + " must be a whole number.")
        out[k] = n
    if out.get("locations") == 0:
        raise ValueError("A restaurant has at least one location.")
    kind = data.get("kind")
    if kind not in (None, "") and kind not in PROFILE_KINDS:
        raise ValueError("Kind is full_service, fast_food or other.")
    out["kind"] = kind or None
    if data.get("franchise") is not None:
        out["franchise"] = bool(data.get("franchise"))
    return out


def save_profile(restaurant_id, data, by=None, db_path=None) -> dict:
    """Store a confirmed profile and keep restaurants.jurisdiction (the
    picked state pack) in step with its state."""
    from models import update_restaurant, get_restaurant, DB_PATH
    clean = clean_profile(data)
    clean["confirmed_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if by:
        clean["confirmed_by"] = str(by)[:120]
    fields = {"compliance_profile_json": json.dumps(clean)}
    fields["jurisdiction"] = clean["state"] if clean.get("state") in PACKS else None
    update_restaurant(restaurant_id, fields, db_path=db_path or DB_PATH)
    return profile(get_restaurant(restaurant_id, db_path or DB_PATH))


def state_of(restaurant) -> str:
    """The restaurant's state code ('' when unknown) — what the minor
    tables and the state rules key on."""
    if restaurant is None:
        return ""
    return profile(restaurant).get("state") or ""


# ── coverage ───────────────────────────────────────────────────────────────

def _coverage_status(e, prof, on):
    """("applies" | "not_covered" | "unknown", why, questions)."""
    cov = e.get("coverage") or {}
    if not cov:
        return "applies", "It covers every employer here.", []
    rule = (value_on(cov.get("employer"), on) or {}).get("value") or {}
    questions, why = [], []
    kinds = rule.get("kinds")
    if kinds:
        if not set(kinds) & set(PROFILE_KINDS):
            return "not_covered", rule.get("kind_why") or "It does not cover restaurants.", []
        if not prof.get("kind"):
            questions.append({"key": "kind", "text": rule.get("kind_question") or
                              "Is the restaurant full service, fast food, or something else?"})
        elif prof["kind"] not in kinds:
            return ("not_covered", rule.get("kind_why") or
                    f"It covers {', '.join(k.replace('_', ' ') for k in kinds)} restaurants only.", [])
    where = {"global": " worldwide", "national": " nationally"}.get(rule.get("scope"), "")
    checks = [("employees_min", "employees", f"How many employees does the business have in total, counting every "
                                             f"location{where}?"),
              ("locations_min", "locations", f"How many locations does the business have{where}?"),
              ("employees_here_min", "employees_here", "How many employees work at your locations in this city?")]
    if rule.get("full_service_locations_min") is not None:
        if not prof.get("kind"):
            questions.append({"key": "kind", "text": "Is the restaurant full service, fast food, or something else?"})
        elif prof["kind"] == "full_service":
            checks.append(("full_service_locations_min", "locations",
                           f"How many locations does the business have{where}?"))
    for fld, label, ask_text in checks:
        need = rule.get(fld)
        if need is None:
            continue
        have = prof.get(label)
        noun = "employees in this city" if label == "employees_here" else label
        if have is None:
            q = {"key": label, "text": ask_text}
            if q not in questions:
                questions.append(q)
        elif have < need:
            why.append(f"it counts {need:,}+ {noun}{where if label != 'employees_here' else ''} and you have {have:,}")
    if why:
        return "not_covered", "Not covered: " + "; ".join(why) + ".", []
    if questions:
        return "unknown", "Covered only above its size thresholds — answer the questions to decide.", questions
    return "applies", rule.get("covered_why") or "Your size meets its thresholds.", []


def evaluate(restaurant, on=None) -> dict:
    """Which jurisdictions' rules hold this restaurant, explicitly.

    {"profile", "on", "jurisdictions": [{code, label, level, law, status:
    "applies" | "not_covered" | "unknown" | "outside", why, questions,
    sources}], "applies": [codes], "questions": [...], "reviewed_on",
    "wording"}. "outside": an ordinance of the restaurant's state for a
    place it is not in, said so the owner sees why it does not apply."""
    on = _d(on)
    prof = profile(restaurant)
    out = {"profile": prof, "on": on.isoformat(), "jurisdictions": [], "applies": [], "questions": [],
           "reviewed_on": REVIEWED_ON, "wording": COUNSEL_WORDING}
    st = prof.get("state")
    if not st:
        out["questions"].append({"key": "state", "text": "Which state is the restaurant in?"})
        return out
    if st in JURISDICTIONS:
        e = JURISDICTIONS[st]
        out["jurisdictions"].append({"code": st, "label": e["label"], "level": "state", "law": "State law",
                                     "status": "applies", "why": "State rules cover every employer in the state.",
                                     "questions": [], "sources": _sources_of(e)})
        out["applies"].append(st)
    else:
        out["jurisdictions"].append({"code": st, "label": ALL_STATES.get(st, st), "level": "state",
                                     "law": "State law", "status": "applies",
                                     "why": "No state meal, rest or overtime rules beyond federal are in the "
                                            "library for this state — federal rules apply.",
                                     "questions": [], "sources": []})
    city = _norm_city(prof.get("city"))
    for code, e in ORDINANCES.items():
        if e.get("state") != st:
            continue
        places = e.get("places")
        base = {"code": code, "label": e["label"], "level": e.get("level", "city"), "law": e.get("law"),
                "sources": _sources_of(e)}
        if places is not None:
            if not city:
                out["jurisdictions"].append(dict(base, status="unknown", why="Which city the restaurant is in decides "
                                                 "whether this applies.", questions=[{"key": "city",
                                                 "text": "Which city is the restaurant in?"}]))
                out["questions"].append({"key": "city", "text": "Which city is the restaurant in?"})
                continue
            if city not in {_norm_city(p) for p in places}:
                out["jurisdictions"].append(dict(base, status="outside",
                                                 why=f"{prof.get('city')} is not {e['place_label']} — "
                                                     f"{e['law']} does not apply.", questions=[]))
                continue
        status, why, qs = _coverage_status(e, prof, on)
        if not value_on(e.get("in_force"), on) and e.get("in_force"):
            status, why, qs = "not_covered", "Not yet in force on this date.", []
        out["jurisdictions"].append(dict(base, status=status, why=why, questions=qs))
        if status == "applies":
            out["applies"].append(code)
        for q in qs:
            if q not in out["questions"]:
                out["questions"].append(q)
    return out


def _sources_of(e) -> list:
    urls = []
    for key, val in e.items():
        vs = val if isinstance(val, list) else ([val] if isinstance(val, dict) and "source" in val else [])
        for v in vs:
            if isinstance(v, dict) and v.get("source") and v["source"] not in urls:
                urls.append(v["source"])
    for vs in (e.get("rules") or {}).values():
        for v in vs:
            if v.get("source") and v["source"] not in urls:
                urls.append(v["source"])
    return urls


def applying(restaurant, on=None) -> list:
    """The entries that hold this restaurant on `on`, state first."""
    ev = evaluate(restaurant, on)
    return [(c, entry(c)) for c in ev["applies"] if entry(c)]


def values_in_force(restaurant, on=None) -> dict:
    """{fact: {"value", "source", "effective", "note", "by": code}} for every
    fact of every jurisdiction that holds the restaurant on `on` — an
    ordinance's over its state's where both set one."""
    on = _d(on)
    out = {}
    for code, e in applying(restaurant, on):
        for key, val in e.items():
            if key in ("label", "level", "state", "places", "place_label", "law", "coverage", "rules", "notes",
                       "in_force"):
                continue
            if isinstance(val, list) and val and isinstance(val[0], dict) and "source" in val[0]:
                v = value_on(val, on)
                if v:
                    out[key] = dict(v, by=code)
        for key, versions in (e.get("rules") or {}).items():
            v = value_on(versions, on)
            if v:
                out["rule:" + key] = dict(v, by=code)
    return out


def rule_values(restaurant, on=None) -> dict:
    """{compliance key: value} the jurisdictions set (rules only)."""
    return {k[5:]: v["value"] for k, v in values_in_force(restaurant, on).items() if k.startswith("rule:")}


def fact(restaurant, key, on=None):
    """One non-rule fact in force (meal_breaks, predictability_pay, ...) as
    its sourced dict, or None."""
    return values_in_force(restaurant, on).get(key)


def apply_for(compliance: dict, restaurant, owner_set: dict = None, on=None) -> dict:
    """Merge every jurisdiction that covers `restaurant` under the owner's
    own values. Returns the merged rules with `_pack` saying what came from
    where (codes, labels, applied values with their sources, notes)."""
    merged = dict(compliance or {})
    merged.pop("_pack", None)
    ev = evaluate(restaurant, on)
    if not ev["applies"] and not (ev["profile"].get("state")):
        return merged
    owner_set = owner_set or {}
    vals = values_in_force(restaurant, on)
    applied, sources = {}, {}
    for k, v in vals.items():
        if not k.startswith("rule:"):
            continue
        key = k[5:]
        if key in owner_set and owner_set[key] not in (None, ""):
            continue
        if v["value"] is None:
            continue
        merged[key] = v["value"]
        applied[key] = v["value"]
        sources[key] = {"url": v["source"], "effective": v["effective"], "by": v["by"], "note": v.get("note")}
    labels = [entry(c)["label"] for c in ev["applies"] if entry(c)]
    st = ev["profile"].get("state")
    state_label = (JURISDICTIONS.get(st) or {}).get("label") or ALL_STATES.get(st or "", st)
    notes = []
    for c in ev["applies"]:
        notes += list((entry(c) or {}).get("notes") or [])
    for j in ev["jurisdictions"]:
        if j["status"] in ("outside", "not_covered", "unknown") and j["level"] != "state":
            notes.append(f"{j['label']} ({j.get('law')}): {j['why']}")
    law = law_keys({k[5:]: v["value"] for k, v in vals.items() if k.startswith("rule:")}, merged)
    merged["_pack"] = {"code": st, "codes": list(ev["applies"]), "label": state_label,
                       "labels": labels, "applied": applied, "sources": sources, "notes": notes, "law": law,
                       "questions": ev["questions"], "reviewed_on": REVIEWED_ON,
                       "wording": f"Rules from {' and '.join(labels) or state_label} — {COUNSEL_WORDING}"}
    return merged


def apply(compliance: dict, jurisdiction: str, owner_set: dict = None, on=None) -> dict:
    """One STATE's rules merged under the owner's own values (the pack a
    restaurant picked before it had a profile). Ordinances are never
    applied by code alone — they need evaluate's coverage answer."""
    code = (jurisdiction or "").strip().upper()
    e = PACKS.get(code)
    merged = dict(compliance or {})
    if not e:
        merged.pop("_pack", None)
        return merged
    owner_set = owner_set or {}
    applied = {}
    for key, versions in (e.get("rules") or {}).items():
        if key in owner_set and owner_set[key] not in (None, ""):
            continue
        v = value_on(versions, on)
        if v and v["value"] is not None:
            merged[key] = v["value"]
            applied[key] = v["value"]
    lawv = {key: (value_on(vs, on) or {}).get("value") for key, vs in (e.get("rules") or {}).items()}
    merged["_pack"] = {"code": code, "codes": [code], "label": e["label"], "labels": [e["label"]],
                       "applied": applied, "notes": list(e.get("notes") or []),
                       "law": law_keys({k: v for k, v in lawv.items() if v is not None}, merged),
                       "wording": f"Rules from {e['label']} — {COUNSEL_WORDING}"}
    return merged


# Which way is stricter for a rule a law sets: +1 more is stricter (more
# rest, more notice), -1 less is (fewer days in a row, an earlier meal or
# daily overtime line).
LAW_DIRECTION = {"min_rest_hours": 1, "notice_days": 1, "max_consecutive_days": -1,
                 "meal_break_after_hours": -1, "daily_ot_hours": -1, "daily_dt_hours": -1}


def law_keys(law_values: dict, merged: dict) -> list:
    """The compliance keys whose value in force is a covering jurisdiction's
    law (schedule_rules.law_backed -> TIER_LEGAL, schedule audit 10/10/26
    LOGIC-1): the law's own value, or one the owner typed that is at least as
    strict. Only a law that COVERS the restaurant is in `law_values` — a
    Chicago rest rule is never law for a restaurant outside Chicago (J1).
    An owner value laxer than the law is the owner's policy, named by
    `conflicts`."""
    out = []
    for k, lv in (law_values or {}).items():
        mine = (merged or {}).get(k)
        if lv is None or mine in (None, ""):
            continue
        d = LAW_DIRECTION.get(k)
        if d is None:
            if mine == lv:
                out.append(k)
            continue
        try:
            if float(mine) * d >= float(lv) * d:
                out.append(k)
        except (TypeError, ValueError):
            continue
    return sorted(out)


# Owner values laxer than a law that covers them: (rule, how to compare).
_FLOOR_RULES = {"notice_days": "min", "min_rest_hours": "min", "meal_break_after_hours": "max",
                "daily_ot_hours": "max"}


def conflicts(restaurant, owner_values: dict, on=None) -> list:
    """[{"key", "owner", "law", "by", "text"}]: where an owner's own value
    is laxer than a jurisdiction that covers them (a shorter notice, a
    later meal break, rest switched off under a rest law)."""
    out = []
    owner_values = owner_values or {}
    for k, v in values_in_force(restaurant, on).items():
        if not k.startswith("rule:"):
            continue
        key = k[5:]
        if key not in owner_values or owner_values[key] in (None, ""):
            if key == "min_rest_hours" and owner_values.get("rest_mode") == "off":
                out.append({"key": key, "owner": "off", "law": v["value"], "by": v["by"],
                            "text": f"You switched rest between shifts off; {entry(v['by'])['label']} sets "
                                    f"{v['value']:g} hours."})
            continue
        how = _FLOOR_RULES.get(key)
        try:
            ov, lv = float(owner_values[key]), float(v["value"])
        except (TypeError, ValueError):
            continue
        if (how == "min" and ov < lv) or (how == "max" and ov > lv):
            out.append({"key": key, "owner": ov, "law": lv, "by": v["by"],
                        "text": f"Your {key.replace('_', ' ')} is {ov:g}; {entry(v['by'])['label']} sets {lv:g}."})
    return out


def records_hold_days(restaurant, on=None) -> int:
    """How long the scheduling records of this restaurant must be kept
    under a law that covers it (days), 0 when none says."""
    v = fact(restaurant, "records_years", on)
    try:
        return int(round(float(v["value"]) * 365)) if v else 0
    except (TypeError, ValueError):
        return 0


def library_summary() -> dict:
    """What the library holds, for the owner's screen and for counsel."""
    return {"reviewed_on": REVIEWED_ON, "review_every_days": REVIEW_EVERY_DAYS,
            "states": sorted(PACKS), "ordinances": sorted(ORDINANCES),
            "minor_states": sorted(STATE_MINOR_RULES), "external": EXTERNAL + EXTERNAL_ORDINANCES,
            "wording": COUNSEL_WORDING}


def employee_covered(restaurant, rate=None, annual=None, salaried=False, on=None, code=None):
    """Whether one person is a covered employee of the ordinance (`code`,
    else the predictive ordinance that holds the restaurant): True, False,
    or None when it turns on a figure not on file. No ordinance: False."""
    on = _d(on)
    codes = [code] if code else [c for c, e in applying(restaurant, on) if e.get("predictability_pay")]
    if not codes:
        return False
    e = entry(codes[0]) or {}
    rule = (value_on((e.get("coverage") or {}).get("employee"), on) or {}).get("value") or {}
    if salaried and rule.get("salaried_excluded"):
        return False
    hmax, amax = rule.get("hourly_max"), rule.get("annual_max")
    if salaried and amax is not None:
        return None if annual is None else float(annual) <= float(amax)
    if hmax is not None:
        return None if rate is None else float(rate) <= float(hmax)
    return True


def predictive_entry(restaurant, on=None):
    """(code, entry) of the predictive-scheduling law that holds the
    restaurant on `on`, or (None, None)."""
    for c, e in applying(restaurant, on):
        if e.get("predictability_pay") or (e.get("rules") or {}).get("notice_days"):
            if e.get("level") == "state" and c in JURISDICTIONS:
                continue
            return c, e
    return None, None
