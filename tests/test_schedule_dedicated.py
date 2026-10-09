"""A standing position (schedule_dedicated): Simple EJ's bar-tables bartender
(tables 201–205) on every night and on Saturday and Sunday daytime, placed
and pinned by code before the model writes, waived only by a manager's note
(Anthony, 10/9/26)."""
import json

import schedule_dedicated as ded
import schedule_rules as sr
import schedule_skeleton as sk

RULE = [{"label": "Bar tables 201–205", "family": "bartender",
         "parts": {"night": list(ded.DAYS), "morning": ["Saturday", "Sunday"]}, "waive_words": ["bar table"]}]
WEEK = [f"2026-10-{d:02d}" for d in range(12, 19)]          # Mon 10/12 - Sun 10/18


class _R:
    dedicated_shifts_json = json.dumps(RULE)


def _c():
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=list(ded.DAYS))
    return c


def _history():
    rows = []
    for d in ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"):
        rows.append({"date": d, "employee": "Old Hand", "role": "Bartender PM", "shift_start": "4:00pm",
                     "shift_end": "11:00pm"})
    for d in ("2026-10-10", "2026-10-11"):
        rows.append({"date": d, "employee": "Old Hand", "role": "Bartender AM", "shift_start": "10:00am",
                     "shift_end": "4:00pm"})
    return rows


ROSTER = {"Ana Bar": "Bartender PM", "Ben Bar": "Bartender AM", "Cy Cook": "Line Cook"}


def test_every_night_and_weekend_day_gets_one_pinned_bartender_at_the_usual_times():
    rules = ded.rules_of(_R())
    p = ded.plan(_c(), WEEK, rules, history=_history(), roster_roles=ROSTER)
    assert len(p["rows"]) == 9 and not p["unfilled"]              # 7 nights + Sat and Sun daytime
    nights = [r for r in p["rows"] if r["role"] == "Bartender PM"]
    assert len(nights) == 7 and all((r["shift_start"], r["shift_end"]) == ("4:00pm", "11:00pm") for r in nights)
    days = [r for r in p["rows"] if r["role"] == "Bartender AM"]
    assert sorted(r["day"] for r in days) == ["Saturday", "Sunday"]
    assert all(r["_pinned"] == ded.DEDICATED_SOURCE and r["employee"] in ("Ana Bar", "Ben Bar") for r in p["rows"])
    assert all(r["notes"].startswith("Bar tables 201–205") for r in p["rows"])
    assert "Cy Cook" not in {r["employee"] for r in p["rows"]}


def test_a_managers_note_waives_only_the_slots_it_names():
    rules = ded.rules_of(_R())
    w = ded.waivers("No bar tables bartender Tuesday night. Keep two bartenders Friday night.", rules)
    p = ded.plan(_c(), WEEK, rules, history=_history(), roster_roles=ROSTER, waived=w)
    assert not any(r["day"] == "Tuesday" for r in p["rows"]) and len(p["rows"]) == 8
    assert p["waived"][0]["note"].startswith("No bar tables bartender Tuesday night")
    assert any("as your note says" in line for line in ded.review_lines(p))


def test_a_slot_nobody_can_take_is_told_never_silent():
    c = _c()
    c.daypart_avail = {c.key("Ana Bar"): {"Friday": "off"}, c.key("Ben Bar"): {"Friday": "off"}}
    p = ded.plan(c, WEEK, ded.rules_of(_R()), history=_history(), roster_roles=ROSTER)
    assert any(u["date"] == "2026-10-16" and u["part"] == "night" for u in p["unfilled"])
    assert any("has nobody" in line for line in ded.review_lines(p))


def test_the_pin_survives_a_save_and_the_staff_read_only_the_label():
    import labor
    p = ded.plan(_c(), WEEK, ded.rules_of(_R()), history=_history(), roster_roles=ROSTER)
    stored = [{k: v for k, v in r.items() if not k.startswith("_")} for r in p["rows"]]
    again = sk.mark_pins(stored)
    assert all(r["_pinned"] == ded.DEDICATED_SOURCE for r in again)
    assert labor.staff_facing_note(stored[0]["notes"]) == "Bar tables 201–205"


def test_the_prompt_says_code_adds_them_and_the_screen_never_lists_them_as_managers():
    p = ded.plan(_c(), WEEK, ded.rules_of(_R()), history=_history(), roster_roles=ROSTER)
    block = ded.prompt_block(p)
    assert "STANDING POSITIONS — ALREADY SCHEDULED" in block and "do NOT write them" in block
    plan = {"rows": p["rows"], "dedicated": p}
    out = sk.payload(plan)
    assert out["shifts"] == [] and len(out["standing"]) == 9
    assert sk._plan_rows(plan["rows"]) == [], "never in MANAGER COVERAGE"


def test_no_rules_no_rows():
    class _N:
        dedicated_shifts_json = None
    assert ded.rules_of(_N()) == [] and ded.plan(_c(), WEEK, [], history=_history())["rows"] == []


def test_the_ai_tab_reads_a_waiver_as_an_allowance(db_path):
    import schedule_note_rules as snr
    from models import Restaurant, create_restaurant, update_restaurant
    rid = create_restaurant(Restaurant(name="EJ", owner_email="e@x.test"), db_path=db_path)
    update_restaurant(rid, {"dedicated_shifts_json": json.dumps(RULE)}, db_path=db_path)
    out = snr.read_notes(rid, "No bar tables bartender Tuesday night. Keep the patio open.", db_path=db_path)
    assert out[0]["kind"] == "waiver" and "Tuesday night" in out[0]["why"]
    assert out[1]["kind"] != "waiver"


def test_clock_in_times_become_quarter_hours():
    hist = [{"date": "2026-10-05", "employee": "X", "role": "Bartender PM", "shift_start": "3:56pm",
             "shift_end": "11:17pm"}]
    assert ded._usual(hist, "bartender", "Monday", "night") == ("Bartender PM", 16 * 60, 23 * 60 + 15)


def test_the_position_is_one_more_on_top_of_the_usual_crew():
    import schedule_requirements as req
    p = ded.plan(_c(), WEEK, ded.rules_of(_R()), history=_history(), roster_roles=ROSTER)
    adj = ded.requirement_adjustments(p)
    assert len(adj) == 9 and all(a["delta"] == 1 and a["firm"] for a in adj)
    typical = {("Wednesday", "night"): {"Bartender PM": 2}}
    rows = req.shift_requirements(["2026-10-14"], typical_headcount=typical, adjustments=adj)
    night = next(r for r in rows if r["daypart"] == "night")
    bar = next(e for e in night["roles"] if e["role"] == "Bartender PM")
    assert bar["required"] == 3, "the usual 2 plus the bar-tables bartender"
    assert "one more" in ded.prompt_block(p) and "MORE of its role on top of the usual crew" in ded.prompt_block(p)
