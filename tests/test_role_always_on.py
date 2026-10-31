"""A role the restaurant keeps on every minute it is open, and one it wants
on from the open (owner, 10/30/26 — Simple EJ's Tue 11/10/26 draft: Amy
behind the bar 10:00am-1:30pm, Stephanie from 3:00pm, "there can NEVER be a
bartender gap like this with no bartender on"; and the opening host on at
noon for an 11am open)."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

import schedule_rules as sr
from tests.clock import next_monday

MONDAY = next_monday()
WEEK = [(MONDAY + timedelta(days=i)).isoformat() for i in range(7)]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
TUE = 1
ROLES = {"Erik": "Owner", "Amy": "Bartender AM", "Stephanie": "Bartender PM", "Gabriel Sanchez": "Bartender PM",
         "Evan Price": "Bartender PM", "Sloane": "Host AM", "Rita": "Host PM", "Ben": "Line Cook"}


def _c(roles=None, **kw):
    roles = ROLES if roles is None else roles
    c = sr.Constraints(restaurant_id=5, week_dates=list(WEEK), week_days=DAYS)
    c.compliance = dict(sr.DEFAULTS)
    c.roster_names = list(roles)
    c.active = {n.lower() for n in roles}
    c.known_roles = {n.lower(): {r.lower()} for n, r in roles.items()}
    c.role_names = {r.lower(): r for r in roles.values()}
    c.managers = {"erik": "Owner"} if "Erik" in roles else {}
    c.open_times = {d: "11:00am" for d in DAYS}
    c.close_times = {d: "2:00am" for d in DAYS}
    # The owner's availability (EJ's): Gabriel is off Monday-Wednesday,
    # Evan can start no earlier than 5:30pm.
    c.unavailable_days = {"gabriel sanchez": {"Monday", "Tuesday", "Wednesday"}}
    c.time_windows = {"evan price": {d: (17 * 60 + 30, None) for d in DAYS}}
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(emp, start, end, i=TUE, role=None):
    r = {"date": WEEK[i], "day": DAYS[i], "employee": emp, "role": role or ROLES.get(emp, "Server"),
         "shift_start": start, "shift_end": end, "notes": ""}
    r["scheduled_hours"] = sr.hours_text(sr.span_hours(r, ""))
    return r


def _tuesday():
    return [_row("Erik", "10:00am", "2:00am"), _row("Amy", "10:00am", "1:30pm"),
            _row("Stephanie", "3:00pm", "2:00am"), _row("Sloane", "11:00am", "5:00pm"),
            _row("Rita", "5:00pm", "11:00pm"), _row("Ben", "10:00am", "9:00pm")]


def _of(rows, c, kind):
    return [v for v in sr.violations(rows, c) if v["kind"] == kind]


# ── (a) the gap is caught ──────────────────────────────────────────────────

def test_the_bartender_gap_between_1_30_and_3_is_a_hard_day_level_breach():
    c, rows = _c(), _tuesday()
    gaps = sr.role_cover_gaps(rows, c)
    assert gaps == {WEEK[TUE]: [{"kind": "role_gap", "family": "bartender", "start": 13 * 60 + 30,
                                 "end": 15 * 60, "first_start": None}]}
    v = _of(rows, c, "role_gap")
    assert len(v) == 1 and v[0]["hard"] and v[0]["day_level"] and v[0]["floor_role"] == "bartender"
    assert "1:30pm" in v[0]["detail"] and "3:00pm" in v[0]["detail"] and "bartender" in v[0]["detail"]
    assert sr.tier_of(v[0]) == sr.TIER_COVERAGE and sr.breach_id(v[0]) == ("role_gap", WEEK[TUE], "bartender")
    # The review shows it on the day (hard_days, what the grid's day head reads).
    hd = sr.summarize(sr.violations(rows, c))["hard_days"]
    assert any(h["kind"] == "role_gap" and h["date"] == WEEK[TUE] for h in hd)


def test_a_job_code_of_the_family_counts_and_the_small_hours_close_counts_to_the_next_day():
    c = _c()
    # Named by the owner (held the whole time the restaurant is open):
    # Stephanie off at 1:00am for a 2:00am close leaves the last hour a gap.
    c.compliance["always_on_roles"] = ["Bartender"]
    rows = [_row("Erik", "10:00am", "2:00am"), _row("Amy", "11:00am", "3:00pm"),
            _row("Stephanie", "3:00pm", "1:00am")]
    assert [(g["start"], g["end"]) for g in sr.role_cover_gaps(rows, c)[WEEK[TUE]]] == [(25 * 60, 26 * 60)]
    # Amy and Stephanie's AM/PM codes are one role: back to back is covered.
    rows[2] = _row("Stephanie", "3:00pm", "2:00am")
    assert sr.role_cover_gaps(rows, c) == {}


def test_the_incremental_sweep_reads_the_breach_as_the_full_sweep_does():
    c, rows = _c(), _tuesday()
    full = [(v["kind"], v["index"], v.get("detail")) for v in sr.violations(rows, c)]
    inc = [(v["kind"], v["index"], v.get("detail")) for v in sr.IncrementalSweep(c).violations(rows)]
    assert sorted(full) == sorted(inc)
    assert any(k == "role_gap" for k, _i, _d in inc)


# ── (b) the backstop closes it inside everyone's availability ──────────────

def test_the_backstop_closes_the_gap_with_a_bartender_who_can_work_it():
    c, rows = _c(), _tuesday()
    out = sr.cover_role_gaps(rows, c)
    assert sr.role_cover_gaps(out["rows"], c) == {}
    assert not _of(out["rows"], c, "role_gap")
    assert not out["left"] and out["shortfall"] is None
    fix = (out["extended"] + out["added"])[0]
    assert fix["family"] == "bartender" and "1:30pm" in fix["reason"]
    # Nothing about anybody's own legality got new: Gabriel (off Tuesdays)
    # and Evan (not before 5:30pm) are never the answer.
    assert fix["employee"] not in ("Gabriel Sanchez", "Evan Price")
    hard_person = [v for v in sr.violations(out["rows"], c) if v["hard"] and sr.tier_of(v) == sr.TIER_LEGAL]
    assert hard_person == []


def test_when_only_unavailable_bartenders_could_cover_it_the_gap_stays_said_with_why():
    # Amy and Stephanie can't stretch (their own windows); the only other
    # bartenders are Gabriel (off Tuesdays) and Evan (from 5:30pm).
    c = _c(time_windows={"evan price": {d: (17 * 60 + 30, None) for d in DAYS},
                         "amy": {d: (10 * 60, 13 * 60 + 30) for d in DAYS},
                         "stephanie": {d: (15 * 60, None) for d in DAYS}})
    rows = _tuesday()
    out = sr.cover_role_gaps(rows, c)
    assert sr.role_cover_gaps(out["rows"], c)
    assert not any(r.get("employee") in ("Gabriel Sanchez", "Evan Price") for r in out["rows"])
    assert out["left"] and out["left"][0]["family"] == "bartender" and out["shortfall"]
    why = {x["employee"]: x["why"] for x in out["left"][0]["reasons"]}
    assert "unavailable" in why["Gabriel Sanchez"] and "5:30pm" in why["Evan Price"]
    # Never silent: still a hard breach for the review and the publish gate.
    assert _of(out["rows"], c, "role_gap")


# ── (c) a restaurant with no bartender is untouched ───────────────────────

def test_a_restaurant_with_no_bartender_on_the_roster_is_untouched():
    roles = {"Erik": "Owner", "Sloane": "Host AM", "Ben": "Line Cook"}
    c = _c(roles=roles)
    rows = [_row("Erik", "10:00am", "2:00am", role="Owner"), _row("Sloane", "11:00am", "5:00pm", role="Host AM"),
            _row("Ben", "10:00am", "9:00pm", role="Line Cook")]
    assert sr.always_on_families(c) == []
    assert sr.role_cover_gaps(rows, c) == {}
    assert not _of(rows, c, "role_gap")
    out = sr.cover_role_gaps(rows, c)
    assert out["rows"] == rows and not out["extended"] and not out["added"]


def test_the_default_closes_holes_in_the_bar_and_naming_it_holds_it_open_to_close():
    # A bar that opens at 4pm: by default nobody is owed a bartender from
    # the 11am open — only no hole between the first bartender on and the
    # last off (the stay until close is the closer rule's).
    c = _c()
    rows = [_row("Erik", "10:00am", "2:00am"), _row("Stephanie", "4:00pm", "11:00pm")]
    assert sr.role_cover_gaps(rows, c) == {}
    # The owner naming the family holds it the whole time they're open.
    c.compliance["always_on_roles"] = ["bartender"]
    assert [(g["start"], g["end"]) for g in sr.role_cover_gaps(rows, c)[WEEK[TUE]]] == [(11 * 60, 16 * 60),
                                                                                         (23 * 60, 26 * 60)]


def test_the_owner_can_turn_the_bartender_default_off():
    c = _c()
    c.compliance["always_on_roles"] = []
    assert sr.role_cover_gaps(_tuesday(), c) == {}


# ── (d) a host on at the open ─────────────────────────────────────────────

def test_a_host_on_at_open_is_held_when_the_owner_asks_for_it():
    c = _c()
    rows = _tuesday()
    rows[3] = _row("Sloane", "12:00pm", "5:00pm")
    assert not _of(rows, c, "role_late_open")            # nobody asked yet: no rule
    c.compliance["on_at_open_roles"] = ["Host"]
    v = _of(rows, c, "role_late_open")
    assert len(v) == 1 and v[0]["hard"] and v[0]["floor_role"] == "host"
    assert "11:00am" in v[0]["detail"] and "12:00pm" in v[0]["detail"]
    out = sr.cover_role_gaps(rows, c)
    assert not _of(out["rows"], c, "role_late_open") and not _of(out["rows"], c, "role_gap")
    sloane = [r for r in out["rows"] if r["employee"] == "Sloane"]
    assert sloane and sloane[0]["shift_start"] == "11:00am"


# ── the setting: one place, compliance_json ───────────────────────────────

def test_the_settings_live_in_the_scheduling_rules():
    stored = {"always_on_roles": ["Bartender"], "on_at_open_roles": "host, Host"}
    comp = sr.compliance(SimpleNamespace(compliance_json=json.dumps(stored), id=5))
    assert comp["always_on_roles"] == ["bartender"] and comp["on_at_open_roles"] == ["host"]
    merged = sr.merged_compliance({"weekly_hours_ceiling": 40}, {"on_at_open_roles": ["Host"]})
    assert merged == {"weekly_hours_ceiling": 40, "on_at_open_roles": ["host"]}
    assert "always_on_roles" not in sr.merged_compliance({"always_on_roles": []}, {"always_on_roles": None})
    assert "on_at_open_roles" in sr.RESETTABLE


@pytest.mark.parametrize("kind", ["role_gap", "role_late_open"])
def test_the_kinds_are_hard_labelled_and_ranked_with_the_floors(kind):
    assert kind in sr.HARD and kind in sr.LABELS and sr.BREACH_TIER[kind] == sr.TIER_COVERAGE
