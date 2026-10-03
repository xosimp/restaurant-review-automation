"""Salaried people on the schedule (owner, 9/30/26): "Erik and Jim work a TON
in the restaurant, most owners do not — make sure the schedule knows."

A salaried person is paid the same whatever the hours. The generator read
every name as hourly: a 55-hour week for Erik tripped the 40h weekly
ceiling (a hard stop), daily overtime flags and the overtime rebalance, was
priced at the blended hourly rate, and was spent from the HOURLY hours
budget (sales x target / hourly rate), trimming hourly shifts to pay for it.
"""
import json

import schedule_economics as econ
import schedule_rules as sr

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _row(i, emp, start="10:00am", end="10:00pm", role="Manager FOH"):
    s, e = sr.parse_minutes(start), sr.parse_minutes(end)
    return {"date": WEEK[i], "day": DAYS[i], "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(round(((e - s) % 1440) / 60, 1)), "notes": ""}


def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    c.compliance = dict(sr.DEFAULTS, daily_ot_hours=8)
    c.active = {"erik baylis", "ana b.", "bo c."}
    c.salaried = {"erik baylis"}
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_a_salaried_week_on_the_floor_is_never_overtime_or_over_a_ceiling():
    c = _c()
    rows = [_row(i, "Erik Baylis") for i in range(5)]            # 5 x 12h = 60h
    kinds = {v["kind"] for v in sr.violations(rows, c)}
    assert not kinds & {"over_max_hours", "daily_ot"}
    # The owner may put them past the 55h code works them to (E-17): the
    # hard maximum is a week of long days, the code's own line is the cap.
    assert c.max_hours("Erik Baylis") == sr.SALARIED_HOURS_MAX
    assert sr.overtime_line(c, "Erik Baylis") == sr.SALARIED_HOURS_CAP == 55
    # the same week for an hourly person is still both
    hourly = [_row(i, "Ana B.") for i in range(5)]
    assert {"over_max_hours", "daily_ot"} <= {v["kind"] for v in sr.violations(hourly, c)}


def test_the_owners_own_limit_for_a_salaried_person_still_holds():
    c = _c(hours_limits={"erik baylis": (None, 50)})
    rows = [_row(i, "Erik Baylis") for i in range(5)]
    assert any(v["kind"] == "over_max_hours" for v in sr.violations(rows, c))


def test_overtime_is_never_moved_onto_a_salaried_person():
    c = _c(active={"erik baylis", "ana b."})
    rows = [_row(i, "Ana B.", "9:00am", "6:00pm", role="Manager FOH") for i in range(6)]     # 54h
    out = sr.rebalance_overtime(rows, c, roster_roles={"Ana B.": "Manager FOH", "Erik Baylis": "Manager FOH"})
    assert not any(m["to"] == "Erik Baylis" for m in out["moves"])


def test_salaried_hours_cost_nothing_hourly_and_are_never_trimmed_for_the_budget():
    c = _c()
    rows = [_row(i, "Erik Baylis") for i in range(5)] + [_row(0, "Ana B.", "4:00pm", "10:00pm", "Server"),
                                                          _row(0, "Bo C.", "5:00pm", "10:00pm", "Server")]
    cost = econ.priced_cost(rows, {}, 15.0, ceiling=40, salaried={"Erik Baylis"})
    assert cost["total"] == 15.0 * 11 and cost["overtime_hours"] == 0
    kept, trimmed, removed = econ.trim_to_budget(rows, 11, {}, constraints=c)
    assert trimmed == [] and removed == 0.0 and len(kept) == len(rows)


def test_the_constraints_and_prompt_know_who_is_salaried(db_path, monkeypatch):
    import models
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    rid = models.create_restaurant(models.Restaurant(name="Sal Co", owner_email="s@x.test", module_labor=1),
                                   db_path=db_path)
    models.update_restaurant(rid, {"salaried_staff_json": json.dumps([{"name": "Erik Baylis", "annual": 150000},
                                                                      {"name": "Jim", "annual": 150000},
                                                                      {"name": "Gabriel Huerta", "annual": 90000}])},
                             db_path=db_path)
    # Erik and Jim are on the roster; Gabriel is on no roster (Simple EJ's,
    # schedule audit 10/3/26 D-7) — the prompt names only the people the
    # roster check would let the model use, and the review names Gabriel.
    for n in ("Erik Baylis", "Jim"):
        models.add_manual_team_member(rid, n, role="Manager FOH", db_path=db_path)
    c = sr.build_constraints(rid, WEEK, DAYS, db_path=db_path)
    assert {"erik baylis", "jim"} <= c.salaried and c.is_salaried("ERIK  Baylis")
    # Each salaried person is marked in their own ROSTER line with their cap
    # (C1, PR-33); the rule is said once, naming nobody.
    import labor
    import schedule_prompt
    table = schedule_prompt.roster_table(labor._roster_people(
        [(n, "Manager FOH") for n in c.roster_names], facts=sr.person_facts(c)))
    assert "Erik Baylis | Manager FOH (manager) | Manager FOH | any day | salaried, at most 55h" in table
    assert "Jim | Manager FOH (manager) | Manager FOH | any day | salaried, at most 55h" in table
    assert "Salaried people (marked salaried in the ROSTER" in sr.prompt_block(c)
    assert "Gabriel" not in sr.prompt_block(c) and "Gabriel" not in table
    assert [u["name"] for u in c.unmatched if u["source"] == "salaried staff"] == ["Gabriel Huerta"]
