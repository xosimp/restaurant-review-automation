"""Scheduler audit 10/10/26, workstream E — AI-20: the restaurant's usual
shifts as templates per role family, weekday and daypart, used in code (the
"solver" draft source's shape) and as the shape prompt's compact table."""
import schedule_fallback as fb
import schedule_rules as sr
import shift_templates as st

WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
PAST_FRIDAYS = ["2026-09-18", "2026-09-25", "2026-10-02", "2026-10-09"]


def _rows():
    out = []
    for d in PAST_FRIDAYS:
        out.append({"date": d, "role": "Server", "shift_start": "4:30pm", "shift_end": "10:00pm"})
        out.append({"date": d, "role": "Server", "shift_start": "4:30pm", "shift_end": "10:00pm"})
        out.append({"date": d, "role": "Server", "shift_start": "6:00pm", "shift_end": "10:00pm"})
        out.append({"date": d, "role": "Server", "shift_start": "11:00am", "shift_end": "9:00pm"})
    return out


def test_templates_are_the_usual_starts_and_ends_best_first():
    tpl = st.templates(_rows())
    dinner = tpl[("server", "Friday", "night")]
    assert dinner[0]["start"] == "4:30pm" and dinner[0]["end"] == "10:00pm" and dinner[0]["share"] == 0.667
    assert dinner[1]["start"] == "6:00pm"
    lunch = tpl[("server", "Friday", "morning")]
    assert lunch[0]["start"] == "11:00am" and lunch[0]["hours"] == 10.0
    share = st.straight_share(_rows())
    assert share[("server", "Friday")] == 0.25                      # the one opener-to-close


def test_a_slot_takes_its_own_template_else_the_roles_at_that_daypart():
    tpl = st.templates(_rows())
    picks = [st.pick(tpl, "Server", "Friday", "night", i=i, n=3)["start"] for i in range(3)]
    assert picks == ["4:30pm", "4:30pm", "6:00pm"]                  # spread in proportion
    assert st.pick(tpl, "Server", "Saturday", "night")["start"] == "4:30pm"   # pooled from other days
    assert st.pick(tpl, "Line Cook", "Friday", "night") is None


def test_the_shape_prompt_gets_a_compact_table():
    text = st.prompt_table(st.templates(_rows()), days=DAYS)
    assert "THIS RESTAURANT'S USUAL SHIFTS" in text
    assert "Server dinner: Fri 4:30pm-10:00pm 67%, 6:00pm-10:00pm 33%" in text


def test_the_code_shaped_draft_lays_requirements_on_templates_and_solves_people():
    names = ["Ann", "Bob", "Cat"]
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=names,
                       active={n.lower() for n in names}, blocked_dates={"ann": {WEEK[4]: "on approved time off"}})
    roles = {n: "Server" for n in names}
    got = fb.solver_rows([WEEK[4]], {f"{WEEK[4]}|night": {"Server": 2}}, c, base_rows=_rows(),
                         signals={"roster": names, "roster_roles": roles}, roster_roles=roles, max_seconds=3)
    assert len(got["rows"]) == 2 and sorted(r["shift_start"] for r in got["rows"]) == ["4:30pm", "6:00pm"]
    assert sorted(r["employee"] for r in got["rows"]) == ["Bob", "Cat"]          # Ann is off — never assigned
    assert not [v for v in sr.violations(got["rows"], c) if v["hard"]]
