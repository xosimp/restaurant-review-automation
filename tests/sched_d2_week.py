"""A realistic restaurant week for the solver and optimizer tests (schedule
fix round 10/3/26, workstream D2): sixty-odd people over AM/PM job codes,
two salaried owners and two floor managers, minors, time off, windows,
certificates, the published tail, floors, closers, owner rules, a section
count, ratings for about half the team — every rule the sweep checks has
something to say. Pure: builds rows, a schedule_rules.Constraints and the
scorer's signals; reads nothing."""
import random
from datetime import date, timedelta

import schedule_rules as sr
import shift_quality as sq

WEEK = [(date(2026, 10, 5) + timedelta(days=i)).isoformat() for i in range(7)]      # Mon 10/5 - Sun 10/11
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def row(d, emp, start, end, role="Server", notes=""):
    r = {"date": d, "day": sr._weekday_of(d), "employee": emp, "role": role,
         "shift_start": start, "shift_end": end, "notes": notes}
    r["scheduled_hours"] = sr.hours_text(sr.span_hours(r))
    return r


def big_week(seed=7, people=60, breaches=True):
    """(rows, constraints, signals) — `signals` are what shift_quality.
    score_rows and the passes read, keyed by display name as the engine
    keys them."""
    rng = random.Random(seed)
    roles = (["Server AM"] * 9 + ["Server PM"] * 12 + ["Bartender PM"] * 6 + ["Line Cook"] * 12
             + ["Dishwasher"] * 6 + ["Host"] * 5)
    names = [f"P{i:02d}" for i in range(people)]
    roster_roles = {n: roles[i % len(roles)] for i, n in enumerate(names)}
    owners, managers = ["Erik", "Jim"], ["Anthony", "Andrew"]
    for n in owners:
        roster_roles[n] = "Owner"
    for n in managers:
        roster_roles[n] = "Manager FOH"
    everyone = names + owners + managers
    by_role = {}
    for n, r in roster_roles.items():
        by_role.setdefault(r, []).append(n)

    rows = []
    shapes = {"Server AM": [("10:30am", "3:00pm")] * 4, "Server PM": [("4:30pm", "10:30pm")] * 5,
              "Bartender PM": [("4:00pm", "11:30pm")] * 2, "Line Cook": [("10:00am", "3:00pm")] * 2 + [("3:30pm", "10:30pm")] * 3,
              "Dishwasher": [("11:00am", "3:00pm"), ("4:00pm", "11:00pm")], "Host": [("11:00am", "2:30pm"), ("5:00pm", "9:30pm")]}
    load = {n: 0.0 for n in everyone}
    for di, d in enumerate(WEEK):
        busy = DAYS[di] in ("Friday", "Saturday")
        for role, shape in shapes.items():
            for k, (s, e) in enumerate(shape + (shape[-1:] if busy else [])):
                pool = sorted(by_role[role], key=lambda n: (load[n], rng.random()))
                pick = next((n for n in pool if not any(r["employee"] == n and r["date"] == d for r in rows)), pool[0])
                r = row(d, pick, s, e, role)
                rows.append(r)
                load[pick] += float(r["scheduled_hours"])
        # a manager from open to close: an opener and a closer leg, with a
        # gap on some days so the manager rule has something to say
        lead = (owners + managers)[di % 4]
        late = (owners + managers)[(di + 1) % 4]
        rows.append(row(d, lead, "10:00am", "4:30pm", roster_roles[lead]))
        rows.append(row(d, late, "5:00pm" if (breaches and di in (1, 4)) else "4:00pm", "11:30pm", roster_roles[late]))

    minors = {names[4].lower(), names[30].lower()}
    bands = {names[4].lower(): "16-17", names[30].lower(): "14-15"}
    blocked = {names[1].lower(): {WEEK[2]: "on approved time off"}, names[22].lower(): {WEEK[5]: "on approved time off"}}
    unavailable = {names[7].lower(): {"Sunday"}, names[40].lower(): {"Monday"}}
    daypart = {names[11].lower(): {"Wednesday": "morning"}, names[18].lower(): {"Friday": "night"}}
    windows = {names[13].lower(): {"Thursday": (sr.parse_minutes("11:00am"), sr.parse_minutes("9:00pm"))}}
    certs = {n.lower(): {"alcohol"} for n in by_role["Bartender PM"][:-1]}
    limits = {names[2].lower(): (30.0, 45.0), names[3].lower(): (None, 24.0), names[25].lower(): (20.0, 32.0)}
    base_rows = {names[5].lower(): [row("2026-10-04", names[5], "5:00pm", "11:30pm", roster_roles[names[5]]),
                                    row("2026-10-03", names[5], "5:00pm", "11:30pm", roster_roles[names[5]])],
                 names[16].lower(): [row("2026-10-04", names[16], "4:30pm", "10:30pm", "Server PM")]}
    base_hours = {}
    for low, rs in base_rows.items():
        for r in rs:
            base_hours.setdefault(low, {})
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS),
                       roster_names=list(everyone), active={n.lower() for n in everyone})
    c.compliance = dict(sr.DEFAULTS, meal_break_after_hours=7, daily_ot_hours=None)
    c.minors, c.minor_bands = minors, bands
    c.blocked_dates, c.unavailable_days, c.daypart_avail, c.time_windows = blocked, unavailable, daypart, windows
    c.certifications, c.role_requirements = certs, {"bartender pm": {"alcohol"}}
    c.hours_limits = limits
    c.base_rows, c.base_hours = base_rows, base_hours
    c.salaried = {n.lower() for n in owners}
    c.salaried_cap = 55.0
    c.managers = {n.lower(): roster_roles[n] for n in owners + managers}
    c.acting_managers = {names[21].lower(): {WEEK[6]}}
    c.role_families = {"server am": "server", "server pm": "server", "bartender pm": "bartender"}
    c.held_roles = {names[9].lower(): {"bartender pm"}}
    c.known_roles = {n.lower(): {roster_roles[n].lower()} for n in everyone}
    c.known_roles[names[9].lower()].add("bartender pm")
    c.roster_roles = dict(roster_roles)
    c.open_times = {d: "10:00am" for d in DAYS}
    c.close_times = {d: ("11:00pm" if d in ("Friday", "Saturday") else "10:00pm") for d in DAYS}
    c.close_mins = {"bartender": 30}
    c.role_floors = {"Line Cook": {"morning": 2, "night": 3}, "Server PM": {"night": 4}}
    c.closers_by_role = {"bartender": {n.lower() for n in by_role["Bartender PM"][:3]}}
    c.keyholders = set(c.closers_by_role["bartender"])
    c.section_cap = 7
    c.foh_roles = {"server am", "server pm"}
    c.owner_rules = [{"role": "Host", "min": 1, "days": ["Friday", "Saturday"], "daypart": None,
                      "text": "always a host on Fridays and Saturdays", "match": "role"}]
    c.pending_off = {names[8].lower(): {WEEK[3]}}
    c.trainees = {names[44].lower(): {"target_role": "Line Cook", "trainer": names[27], "until": WEEK[6]}}
    c.role_names = {r.lower(): r for r in set(roster_roles.values())}
    if breaches:
        # somebody on approved time off, a minor past their end, a rest gap
        rows.append(row(WEEK[2], names[1], "4:30pm", "10:30pm", roster_roles[names[1]]))
        rows.append(row(WEEK[3], names[4], "5:00pm", "11:00pm", roster_roles[names[4]]))

    scores = {n: rng.randint(1, 5) for n in everyone if rng.random() < 0.5}
    signals = {
        "roster": list(everyone), "roster_roles": dict(roster_roles), "scores": scores,
        "leader_flags": {n: True for n in by_role["Bartender PM"][:3]},
        "leader_rules": [{"role": "Bartender PM", "count": 1, "attribute": "can_close", "closing": True},
                         {"role": "Server PM", "count": 2, "min_score": 4, "days": ["Friday", "Saturday"]}],
        "typical_headcount": {(DAYS[i], p): {"Server AM": 4, "Line Cook": 2, "Dishwasher": 1, "Host": 1} if p == "morning"
                              else {"Server PM": 5, "Bartender PM": 2, "Line Cook": 3, "Dishwasher": 1, "Host": 1}
                              for i in range(7) for p in ("morning", "night")},
        "demand_by_day": {"Friday": 25.0, "Saturday": 35.0, "Monday": -20.0},
        "daily_target_hours": {d: 160.0 for d in WEEK},
        "tenure": {n: rng.randint(0, 60) for n in everyone},
        "reliability": {n: {"no_show_rate": 0.25 if i % 17 == 3 else 0.02, "shifts": 30,
                            "late_risk": i % 13 == 5, "late_rate": 0.3 if i % 13 == 5 else 0.05}
                        for i, n in enumerate(everyone)},
        "pairs": {"avoid": {frozenset({names[0].lower(), names[12].lower()})},
                  "prefer": {frozenset({names[14].lower(), names[15].lower()})}},
        "preferences": {names[6]: {"preferred_dayparts": ["morning"], "desired_hours": 20},
                        names[10]: {"preferred_dayparts": ["night"]}},
        "prior_pattern": {n: {"days": [DAYS[i % 7], DAYS[(i + 2) % 7]], "dayparts": ["night"]}
                          for i, n in enumerate(names[:30])},
        "cross_trained": {names[9]: ["Server PM", "Bartender PM"], names[33]: ["Line Cook", "Dishwasher"]},
        "managers": dict(c.managers), "acting_managers": {k: set(v) for k, v in c.acting_managers.items()},
        "salaried": {n.lower() for n in owners}, "role_families": dict(c.role_families),
        "held_roles": {k: set(v) for k, v in c.held_roles.items()},
        "open_times": dict(c.open_times), "close_times": dict(c.close_times),
        "role_floors": dict(c.role_floors), "section_cap": c.section_cap, "cap_roles": sorted(c.foh_roles),
        "hours_limits": {n: c.hours_limits[n.lower()] for n in everyone if n.lower() in c.hours_limits},
        "max_shift_hours": 12.0, "weekly_ceiling": 40.0,
        "overtime": {"line": 40.0, "bucket_of": {d: c.bucket(d) for d in WEEK}, "published": {},
                     "rates": {"server am": 9.0, "server pm": 9.0, "bartender pm": 12.0, "line cook": 18.0,
                               "dishwasher": 15.0, "host": 13.0, "manager foh": 24.0},
                     "default_rate": 15.0, "rules": {}},
    }
    return rows, c, signals
