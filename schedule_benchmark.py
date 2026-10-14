"""
schedule_benchmark.py — the schedule generator run end to end on restaurants
that are not Simple EJ's (blind re-audit 10/11/26 ARCH-22, AI-28, ARCH-19;
cross-workstream contract #7).

Why it exists. Every default and heuristic of the scheduler was tuned on one
client — a sports bar whose owners are on the floor most of the week — and
the experiment machinery that should validate them needs five generating
restaurants per arm before it may conclude anything. The re-audit's stress
lens built a dozen other restaurants in throwaway databases and found the
code drafts wrong in ways no test saw: 11-12 hour lunch-to-close doubles,
lunch-only staff given no hours, a 24-hour diner with one person on
overnight, teens with no shifts, a week with no server that said nothing,
and a second Generate press that returned a different week. Here those
restaurants are kept, as data, and run the same way on every change.

THE ARCHETYPES (ARCHETYPES). Each is a restaurant spec — hours, roster with
roles and settings (availability, minors, maximums, salaried pay), its usual
week (the pattern its history repeats), daily sales, floors, closures, time
off — built into the CURRENT database through the app's own writes
(create_restaurant, update_restaurant, save_client_data, staff_settings,
time_off). 6-person cafe, 22 bistro, 60 full-service (a Wednesday week),
120, 250 high-volume, 500, 24-hour diner, teen-heavy QSR (14-15s and
16-17s), single manager, multi-location org (shared staff), daypart-limited
staff, zero history, plus the late-night bar and mass-time-off weeks the
stress lens measured. `scale` shrinks or grows the scalable ones (the fast
test subset runs them small).

THE RUN (run_archetype). The real generation job
(schedule_engine._run_schedule_job — the parse, the manager plan, the repair
loop, the gate, the review and the one save) with SCHEDULE_DRAFT_SOURCE set
to one of the code-only draft sources (ARMS: "solver" — shapes + CP-SAT —
and "carry_forward"), RUNS times on the same week. No model is called: the
run refuses when a provider key is in the environment, and refuses any
database outside a temporary directory (it writes restaurants).

THE METRICS (measure). Read from the SAVED week, never from the engine's
own account of it:
  hard            hard breaches by kind (schedule_rules.violations, the
                  engine's oracle) — and `independent`, checks that trust
                  nothing in the engine: overlaps, rows on closed or
                  time-off dates, people not on the roster, the federal
                  14-15 rules (outside school hours, 7pm, 3h a school day),
                  cross-location double booking, and every minute someone
                  is on with no manager (the owner's non-negotiable)
  unfilled        person-hours of the archetype's own usual crew per role
                  per half hour (its floors where it has no history) left
                  unstaffed, and their value: by schedule_objective.
                  slot_value when that module is present (contract #1),
                  else the stated fallback — the day's sales over the day's
                  needed person-half-hours (what one needed person carries
                  that day), so a busy day's hole costs more than a quiet one
  dollars         labor dollars at the archetype's own rates (hourly people;
                  overtime at 1.5x), the engine's hourly budget, and dollars
                  over it
  overtime_hours  hours past 40 for hourly people
  long_shifts     shifts over 10 hours, and the length histogram
  usual_days_kept the share of each person's usual (weekday) days the draft
                  kept, of the ones they could work this week
  starved         people who usually work 15h+ given under half of it
  determinism     whether RUNS presses gave the same week (row sets)
  seconds, peak_rss_mb (the script isolates each arm in its own process so
                  the figure is that run's own)
  quality, gate   Shift Quality's score, and the publish gate's blockers

THE GATE (failed_invariants). The invariants every heuristic must hold on
every archetype (INVARIANTS): zero independent legal breaches, zero minutes
of floor without a manager in open hours, deterministic, no shift over 10
hours the restaurant never works, usual days kept >= 70%, nobody starved, no
open day with a needed role at zero people. tests/test_schedule_benchmark.py
holds the fast subset to them — the ones that fail today as strict
xfails naming the finding that fixes them, so a fix flips them and the gate
ratchets. scripts/schedule_benchmark.py runs every archetype and both arms
and prints the comparison table.

The golden set (schedule_eval) is the replay half: real published weeks,
scored with scripts/schedule_model_eval.py --set; it needs a copy of
production's database, which the synthetic half here does not.
"""
import json
import os
import random
import sys
import tempfile
import time
from datetime import date as _date, timedelta as _td

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
ARMS = ("solver", "carry_forward")
RUNS = 3
# The fast subset tests/test_schedule_benchmark.py runs in one process
# (about a minute): [archetype, arms, runs, scale]. Every archetype and both
# arms at full size are the script's (scripts/schedule_benchmark.py).
FAST_PLAN = (("cafe_6", ARMS, 3, 1.0), ("daypart_limited", ARMS, 2, 0.5), ("qsr_teens", ARMS, 1, 0.5),
             ("single_manager", ARMS, 1, 1.0))
# Opt-in in the script (--heavy): one generation needs gigabytes (STRESS-11).
HEAVY = ("mega_500",)
# The gate's thresholds (INVARIANTS).
USUAL_DAYS_KEPT_MIN = 70.0
LONG_SHIFT_HOURS = 10.0
STARVED_USUAL_HOURS = 15.0
STARVED_SHARE = 0.5
PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PERPLEXITY_API_KEY")


class BenchmarkRefused(RuntimeError):
    """The benchmark would touch something it must not: a database outside a
    temporary directory, or a provider key that could make a model call."""


# ── the archetypes ───────────────────────────────────────────────────────

def _fmt(m) -> str:
    m = int(m) % 1440
    h, mm = divmod(m, 60)
    return f"{(h % 12) or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


def _m(t) -> int:
    import schedule_time as _st
    return _st.parse_minutes(t)


def _spec(key, label, why, people, pattern, open_, close, sales, **kw) -> dict:
    """One restaurant: people [(name, role, settings)], pattern [(weekday,
    name, role, start, end)] — the week its history repeats."""
    out = {"key": key, "label": label, "why": why, "people": people, "pattern": pattern, "open": dict(open_),
           "close": dict(close), "sales": dict(sales), "rates": dict(kw.pop("rates", RATES)),
           "salaried": kw.pop("salaried", []), "floors": kw.pop("floors", None), "weeks": kw.pop("weeks", 8),
           "closed_days": kw.pop("closed_days", ()), "time_off": kw.pop("time_off", []),
           "cols": kw.pop("cols", {}), "siblings": kw.pop("siblings", [])}
    out.update(kw)
    return out


RATES = {"Server": 9.0, "Bartender": 10.0, "Host": 13.0, "Busser": 9.0, "Line Cook": 18.0, "Prep Cook": 17.0,
         "Cook": 17.0, "Dishwasher": 15.0, "Barista": 14.0, "Crew": 13.0, "Manager": 24.0,
         "Kitchen Manager": 24.0, "Shift Lead": 17.0}


def cafe_6(scale=1.0):
    """The stress lens's s01: a 7am-3pm cafe, one manager (the owner) on
    every day, two baristas, a cook, two weekend-only people."""
    o = {d: "7:00am" for d in DAYS}
    c = {d: "3:00pm" for d in DAYS}
    pat = []
    for d in DAYS:
        # the owner every day; each of the crew six days at most — the
        # weekend pair opens on Sunday and doubles up on Saturday
        pat.append((d, "Mia Owner", "Manager", "6:30am", "3:00pm"))
        if d == "Sunday":
            pat += [(d, "Eli Stone", "Barista", "6:30am", "12:00pm"), (d, "Fay Moss", "Cook", "6:30am", "2:30pm")]
        else:
            pat += [(d, "Ben Ruiz", "Barista", "6:30am", "12:00pm"), (d, "Dev Park", "Cook", "6:30am", "2:30pm")]
        if d != "Monday":
            pat.append((d, "Cara Lin", "Barista", "10:00am", "3:00pm"))
        if d == "Saturday":
            pat += [(d, "Eli Stone", "Barista", "8:00am", "2:00pm"), (d, "Fay Moss", "Cook", "8:00am", "2:00pm")]
    people = [("Mia Owner", "Manager", {}), ("Ben Ruiz", "Barista", {}), ("Cara Lin", "Barista", {}),
              ("Dev Park", "Cook", {}), ("Eli Stone", "Barista", {}), ("Fay Moss", "Cook", {})]
    return _spec("cafe_6", "6-person cafe", "STRESS s01: one manager (the owner) every day, weekend-only staff, 6:30am prep",
                 people, pat, o, c, {d: 1500 for d in DAYS}, salaried=[{"name": "Mia Owner", "annual": 52000}])


_B_OPEN = {d: "11:00am" for d in DAYS}
_B_CLOSE = {d: ("11:00pm" if d in ("Friday", "Saturday") else "10:00pm") for d in DAYS}
_B_MGRS = [("Alex Rivera", "Manager"), ("Bea Chen", "Manager")]
_B_STAFF = ([(f"Server {c}", "Server") for c in "ABCDEFGH"] + [(f"Cook {c}", "Line Cook") for c in "ABCDE"] +
            [(f"Bar {c}", "Bartender") for c in "ABC"] + [("Host A", "Host"), ("Host B", "Host"),
                                                          ("Dish A", "Dishwasher"), ("Dish B", "Dishwasher")])


def _bistro_pattern(staff=None, close=None):
    """The stress lens's mid.py week: two managers handing over at 4-5pm, and
    each of the others on five days, lunch or dinner by turn."""
    staff = _B_STAFF if staff is None else staff
    close = close or _B_CLOSE
    pat = []
    a, b = _B_MGRS[0][0], _B_MGRS[1][0]
    for i, d in enumerate(DAYS):
        cl = close[d]
        if d in ("Monday", "Tuesday"):
            pat.append((d, a, "Manager", "10:30am", cl))
        elif d in ("Saturday", "Sunday"):
            pat.append((d, b, "Manager", "10:30am", cl))
        else:
            pat.append((d, a, "Manager", "10:30am", "5:00pm"))
            pat.append((d, b, "Manager", "4:00pm", cl))
        for j, (n, role) in enumerate(staff):
            if (j + i) % 7 in (5, 6):
                continue
            if role == "Server":
                s, e = ("11:00am", "4:00pm") if j % 2 == i % 2 else ("4:00pm", cl)
            elif role == "Line Cook":
                s, e = ("10:00am", "4:00pm") if j % 2 == i % 2 else ("4:00pm", cl)
            elif role == "Bartender":
                s, e = ("4:00pm", cl) if j else ("11:00am", "5:00pm")
            elif role == "Host":
                s, e = ("11:00am", "3:00pm") if j % 2 else ("4:30pm", "9:30pm")
            else:
                s, e = ("11:00am", "5:00pm") if j % 2 else ("5:00pm", cl)
            pat.append((d, n, role, s, e))
    return pat


def _bistro_sales():
    return {d: (9000 if d in ("Friday", "Saturday") else 6000) for d in DAYS}


def _bistro_people():
    return [(n, r, {}) for n, r in _B_MGRS + _B_STAFF]


def bistro_22(scale=1.0):
    return _spec("bistro_22", "22-person bistro", "STRESS s03: the baseline full-service week",
                 _bistro_people(), _bistro_pattern(), _B_OPEN, _B_CLOSE, _bistro_sales(),
                 salaried=[{"name": "Alex Rivera", "annual": 55000}, {"name": "Bea Chen", "annual": 52000}])


def daypart_limited(scale=1.0):
    """The stress lens's s10b: four lunch-only servers and two lunch-only
    cooks whose history is consistent with it; the others work dinner."""
    lunch = ["Server A", "Server B", "Server C", "Server D", "Cook A", "Cook B"]
    dinner = ["Server E", "Server F", "Server G", "Server H", "Cook C", "Cook D", "Cook E"]
    staff = list(_B_STAFF)
    if scale < 1.0:
        keep = {"Server A", "Server B", "Server E", "Server F", "Cook A", "Cook C", "Cook D", "Bar A", "Bar B",
                "Host A", "Dish A"}
        staff = [p for p in staff if p[0] in keep]
        lunch = [n for n in lunch if n in keep]
        dinner = [n for n in dinner if n in keep]
    pat = []
    for (d, n, r, s, e) in _bistro_pattern(staff):
        if n in lunch:
            s, e = ("11:00am", "4:00pm") if r == "Server" else ("10:00am", "4:00pm")
        elif n in dinner:
            s, e = ("4:00pm", _B_CLOSE[d])
        pat.append((d, n, r, s, e))
    people = [(n, r, {"daypart_availability": {d: "morning" for d in DAYS}} if n in lunch else {})
              for n, r in _B_MGRS + staff]
    return _spec("daypart_limited", "Daypart-limited staff", "STRESS s10b / STRESS-2: lunch-only people starved",
                 people, pat, _B_OPEN, _B_CLOSE, _bistro_sales(),
                 salaried=[{"name": "Alex Rivera", "annual": 55000}, {"name": "Bea Chen", "annual": 52000}])


def mass_time_off(scale=1.0):
    """The stress lens's s15: 13 of 22 on approved time off all week."""
    out = [n for n, r in _B_STAFF if r in ("Server", "Line Cook", "Host")][:12] + ["Bea Chen"]
    return _spec("mass_time_off", "Mass time off", "STRESS s15 / STRESS-9: no server all week, said nowhere",
                 _bistro_people(), _bistro_pattern(), _B_OPEN, _B_CLOSE, _bistro_sales(),
                 salaried=[{"name": "Alex Rivera", "annual": 55000}, {"name": "Bea Chen", "annual": 52000}],
                 time_off=[(n, 0, 6) for n in out])


def zero_history(scale=1.0):
    """The stress lens's s09: a new restaurant with a roster and floors and
    no history at all."""
    floors = {"Server": {"morning": 2, "night": 4}, "Line Cook": {"morning": 1, "night": 2},
              "Bartender": {"morning": 1, "night": 1}, "Host": {"morning": 1, "night": 1}}
    return _spec("zero_history", "Zero history", "STRESS s09 / STRESS-20: all 4h shifts, unfloored roles empty",
                 _bistro_people(), [], _B_OPEN, _B_CLOSE, {}, floors=floors, weeks=0,
                 salaried=[{"name": "Alex Rivera", "annual": 55000}, {"name": "Bea Chen", "annual": 52000}])


_GRID_ROLES = (("Server", 80), ("Bartender", 25), ("Line Cook", 50), ("Prep Cook", 20), ("Dishwasher", 20),
               ("Host", 20), ("Busser", 20), ("Manager", 10), ("Kitchen Manager", 5))
_GRID_SHIFTS = {"Server": [("11:00am", "4:00pm"), ("4:00pm", "11:00pm")],
                "Bartender": [("11:00am", "5:00pm"), ("5:00pm", "12:00am")],
                "Line Cook": [("10:00am", "4:00pm"), ("4:00pm", "11:00pm")], "Prep Cook": [("7:00am", "3:00pm")],
                "Dishwasher": [("11:00am", "5:00pm"), ("5:00pm", "12:00am")],
                "Host": [("11:00am", "4:00pm"), ("4:30pm", "10:00pm")],
                "Busser": [("11:00am", "4:00pm"), ("5:00pm", "11:00pm")],
                "Manager": [("10:00am", "6:00pm"), ("4:00pm", "12:00am")],
                "Kitchen Manager": [("9:00am", "5:00pm"), ("3:00pm", "11:00pm")]}


def _grid(key, label, why, size, seed=7, week_start_day=None, floors_per_100=None, sales_per_person=180):
    """The stress lens's s02c: a high-volume grill of `size` people in the
    mix _GRID_ROLES (per 290), each on 4 days a week (managers 5) at their
    role's lunch or dinner shift; open 11am-11pm, Friday and Saturday to
    midnight. Floors scale with the roster."""
    o = {d: "11:00am" for d in DAYS}
    c = {d: ("12:00am" if d in ("Friday", "Saturday") else "11:00pm") for d in DAYS}
    total = sum(n for _r, n in _GRID_ROLES)
    rng = random.Random(seed * 1009 + size)
    people = []
    for role, n in _GRID_ROLES:
        for i in range(max(2 if role != "Kitchen Manager" else 1, int(round(n * size / total)))):
            people.append((f"{role.split()[0]}{i:02d} {chr(65 + i % 26)}{i // 26}.", role))
    pat = []
    for idx, (name, role) in enumerate(people):
        days = rng.sample(DAYS, 5 if "Manager" in role else 4)
        for d in days:
            s, e = _GRID_SHIFTS[role][(idx + DAYS.index(d)) % len(_GRID_SHIFTS[role])]
            pat.append((d, name, role, s, e))
    f = floors_per_100 or {"Server": (3, 6), "Line Cook": (2, 3), "Bartender": (1, 2), "Host": (1, 1)}
    floors = {r: {"morning": max(1, int(round(a * size / 100))), "night": max(1, int(round(b * size / 100)))}
              for r, (a, b) in f.items()}
    per = sales_per_person * size
    sales = {d: (int(per * 1.5) if d in ("Friday", "Saturday") else per) for d in DAYS}
    mgrs = [n for n, r in people if r == "Manager"]
    cols = {}
    if week_start_day is not None:
        cols["week_start_day"] = week_start_day
    return _spec(key, label, why, [(n, r, {}) for n, r in people], pat, o, c, sales, floors=floors, cols=cols,
                 salaried=[{"name": n, "annual": 58000} for n in mgrs])


def full_service_60(scale=1.0):
    return _grid("full_service_60", "60-person full service (Wednesday week)",
                 "Wednesday-Tuesday payroll week (STRESS s06a) at 60 people", int(round(60 * scale)),
                 week_start_day=2)


def large_120(scale=1.0):
    return _grid("large_120", "120-person restaurant", "the 74-120 band where determinism broke (STRESS-10)",
                 int(round(120 * scale)))


def high_volume_250(scale=1.0):
    return _grid("high_volume_250", "250-person high volume", "STRESS s02: doubles, memory and time at 250",
                 int(round(250 * scale)))


def mega_500(scale=1.0):
    return _grid("mega_500", "500-person restaurant", "STRESS s02c: 7.6 GB for one generation (STRESS-11)",
                 int(round(500 * scale)))


def diner_24h(scale=1.0):
    """The stress lens's s04, staffed as a real diner is: open around the
    clock (12:00am to 12:00am), three 8-hour shifts a day with a manager, 4
    servers and 3 cooks on each; every person keeps one shift and works five
    days (two off in a row), two relief managers cover the others' days off.
    (The auditor's own s04 put every server and cook on all seven days.)"""
    o = {d: "12:00am" for d in DAYS}
    c = {d: "12:00am" for d in DAYS}
    sh = (("Day", "6:00am", "2:00pm", (5, 6)), ("Swing", "2:00pm", "10:00pm", (0, 1)),
          ("Grave", "10:00pm", "6:00am", (2, 3)))
    pat, people = [], [("Mgr Relief", "Manager", {}), ("Mgr Relief Two", "Manager", {})]
    for k, (tag, s, e, off) in enumerate(sh):
        people.append((f"Mgr {tag}", "Manager", {}))
        servers = [f"Srv {tag} {x}" for x in "ABCDEF"]
        cooks = [f"Cook {tag} {x}" for x in "ABCDE"]
        people += [(n, "Server", {}) for n in servers] + [(n, "Line Cook", {}) for n in cooks]
        for i, d in enumerate(DAYS):
            relief = "Mgr Relief Two" if tag == "Grave" else "Mgr Relief"
            pat.append((d, relief if i in off else f"Mgr {tag}", "Manager", s, e))
            for group, need in ((servers, 4), (cooks, 3)):
                free = [n for p, n in enumerate(group) if (i + p) % 7 not in (5, 6)]
                pat += [(d, n, "Server" if group is servers else "Line Cook", s, e) for n in free[:need]]
    return _spec("diner_24h", "24-hour diner", "STRESS s04 / STRESS-4: overnight clipped at midnight",
                 people, pat, o, c, {d: 7000 for d in DAYS})


def late_bar(scale=1.0):
    """The stress lens's s05: a bar open 4pm to 2am, 3am Friday and Saturday."""
    o = {d: "4:00pm" for d in DAYS}
    c = {d: ("3:00am" if d in ("Friday", "Saturday") else "2:00am") for d in DAYS}
    people = [("Rae Kim", "Manager", {}), ("Sol Ortiz", "Manager", {})] + \
        [(f"Bar {x}", "Bartender", {}) for x in "ABCDEF"] + [(f"Barback {x}", "Busser", {}) for x in "AB"] + \
        [(f"Door {x}", "Host", {}) for x in "AB"] + [(f"Kitchen {x}", "Line Cook", {}) for x in "ABC"]
    pat = []
    for i, d in enumerate(DAYS):
        cl = c[d]
        pat.append((d, "Rae Kim" if i % 2 == 0 else "Sol Ortiz", "Manager", "3:30pm", cl))
        bars = [f"Bar {x}" for x in "ABCDEF"]
        for j in range(4 if d in ("Friday", "Saturday") else 2):
            pat.append((d, bars[(i + j) % 6], "Bartender", "4:00pm" if j % 2 == 0 else "8:00pm", cl))
        pat.append((d, f"Barback {'AB'[i % 2]}", "Busser", "6:00pm", cl))
        if d in ("Thursday", "Friday", "Saturday"):
            pat.append((d, f"Door {'AB'[i % 2]}", "Host", "9:00pm", cl))
        pat.append((d, f"Kitchen {'ABC'[i % 3]}", "Line Cook", "3:30pm", "11:00pm"))
    return _spec("late_bar", "Late-night bar", "STRESS s05: 2-3am closes (DST held)", people, pat, o, c,
                 {d: (8000 if d in ("Friday", "Saturday") else 3500) for d in DAYS})


def qsr_teens(scale=1.0):
    """A counter-service spot (10:30am-10pm) that runs on teenagers: 14-15s
    after school on weekdays (4-7pm, the federal school-night limit) and
    weekend days, 16-17s evenings, adult cooks and shift leads (the
    managers). Reproduces STRESS s08 (teens given no hours; a 4h minimum
    shape against a 3h school-day cap) and p01 (no school-hours rule)."""
    n_young = max(2, int(round(8 * scale)))
    n_teen = max(2, int(round(8 * scale)))
    o = {d: "10:30am" for d in DAYS}
    c = {d: "10:00pm" for d in DAYS}
    young = [f"Kid {i:02d}" for i in range(n_young)]
    teen = [f"Teen {i:02d}" for i in range(n_teen)]
    cooks = [f"Cook {i:02d}" for i in range(max(2, int(round(4 * scale))))]
    leads = ["Lead Ana", "Lead Bo", "Lead Cy"]
    pat = []
    for i, d in enumerate(DAYS):
        weekend = d in ("Saturday", "Sunday")
        pat.append((d, leads[i % 3], "Manager", "10:00am", "4:30pm"))
        pat.append((d, leads[(i + 1) % 3], "Manager", "4:00pm", "10:30pm"))
        for j, k in enumerate(young):
            if (i + j) % 2:
                continue
            pat.append((d, k, "Crew", "10:30am", "4:30pm") if weekend else (d, k, "Crew", "4:00pm", "7:00pm"))
        for j, k in enumerate(teen):
            if (i + j) % 3 == 0:
                continue
            pat.append((d, k, "Crew", "11:00am", "5:00pm") if weekend else (d, k, "Crew", "5:00pm", "10:00pm"))
        for j, k in enumerate(cooks):
            if (i + j) % 7 in (5, 6):
                continue                                  # five days a week
            pat.append((d, k, "Cook", "10:00am", "4:00pm") if (i + j) % 2 else (d, k, "Cook", "4:00pm", "10:00pm"))
    people = [(k, "Crew", {"minor_age_band": "14-15"}) for k in young] + \
        [(k, "Crew", {"minor_age_band": "16-17"}) for k in teen] + [(k, "Cook", {}) for k in cooks] + \
        [(k, "Manager", {}) for k in leads]
    return _spec("qsr_teens", "Teen-heavy QSR", "STRESS s08 / p01 / STRESS-5 / STRESS-6: minors", people, pat,
                 o, c, {d: (5200 if d in ("Friday", "Saturday", "Sunday") else 3800) for d in DAYS},
                 cols={"jurisdiction": "IL"})


def single_manager(scale=1.0):
    """An owner-operator who is the only manager, closed Mondays, open
    11am-9pm; a crew of eleven. Reproduces STRESS-18 (the single-manager
    week's permanent blocker) and the opening-prep allowance (STRESS-3)."""
    o = {d: "11:00am" for d in DAYS}
    c = {d: "9:00pm" for d in DAYS}
    crew = [("Sam Cole", "Server"), ("Tia Gray", "Server"), ("Uma Bell", "Server"), ("Vic Lane", "Server"),
            ("Wes Hart", "Line Cook"), ("Xia Wong", "Line Cook"), ("Yan Diaz", "Line Cook"),
            ("Zed Fox", "Dishwasher"), ("Ada Lowe", "Dishwasher"), ("Bo Reed", "Host"), ("Cy Hale", "Bartender")]
    pat = []
    for i, d in enumerate(DAYS):
        if d == "Monday":
            continue
        pat.append((d, "Owner Jo", "Manager", "10:30am", "9:30pm"))
        for j, (n, r) in enumerate(crew):
            if (i + j) % 6 == 0:
                continue
            if r == "Line Cook":
                s, e = ("10:00am", "4:00pm") if (i + j) % 2 else ("3:30pm", "9:30pm")
            elif r in ("Server", "Dishwasher"):
                s, e = ("11:00am", "4:00pm") if (i + j) % 2 else ("4:00pm", "9:30pm")
            else:
                s, e = ("4:00pm", "9:30pm")
            pat.append((d, n, r, s, e))
    people = [("Owner Jo", "Manager", {})] + [(n, r, {}) for n, r in crew]
    return _spec("single_manager", "Single manager", "STRESS-18 / STRESS-3: one manager, closed Mondays",
                 people, pat, o, c, {d: (0 if d == "Monday" else 3200) for d in DAYS}, closed_days=("Monday",),
                 salaried=[{"name": "Owner Jo", "annual": 60000}])


def multi_location_org(scale=1.0):
    """Two locations of one owner (same location group): three people work
    at both. The first location's week is generated and published, then the
    second's is measured — a shared person must not be double-booked."""
    shared = [("Gil Shared", "Server"), ("Hana Shared", "Line Cook"), ("Ivo Shared", "Bartender")]
    o = {d: "11:00am" for d in DAYS}
    c = {d: "10:00pm" for d in DAYS}

    def site(tag, own, shared_days):
        pat = []
        for i, d in enumerate(DAYS):
            # three managers, an opener and a closer a day: five days each
            pat.append((d, f"{tag} Mgr {'ABC'[i % 3]}", "Manager", "10:30am", "4:30pm"))
            pat.append((d, f"{tag} Mgr {'ABC'[(i + 1) % 3]}", "Manager", "4:00pm", "10:30pm"))
            for j, (n, r) in enumerate(own):
                if (i + j) % 7 in (5, 6):
                    continue
                s, e = ("11:00am", "4:00pm") if (i + j) % 2 else ("4:00pm", "10:00pm")
                if r == "Line Cook":
                    s = "10:00am" if s == "11:00am" else "3:30pm"
                pat.append((d, n, r, s, e))
            for n, r in shared:
                if d in shared_days:
                    s, e = shared_days[d]
                    pat.append((d, n, r, s, e))
        people = [(f"{tag} Mgr {x}", "Manager", {}) for x in "ABC"] + \
            [(n, r, {}) for n, r in own] + [(n, r, {}) for n, r in shared]
        return people, pat

    own_a = [(f"North {r.split()[0]} {k}", r) for r, ks in (("Server", "ABCDE"), ("Line Cook", "ABC"),
                                                             ("Bartender", "AB"), ("Dishwasher", "AB"))
             for k in ks]
    own_b = [(f"South {r.split()[0]} {k}", r) for r, ks in (("Server", "ABCD"), ("Line Cook", "AB"),
                                                             ("Bartender", "A"), ("Dishwasher", "A"))
             for k in ks]
    # The shared three: North's lunches Monday to Thursday, South's dinners
    # Thursday to Sunday — Thursday is both, at different times.
    lunch, dinner = ("11:00am", "3:30pm"), ("4:00pm", "10:00pm")
    pa, ta = site("North", own_a, {d: lunch for d in ("Monday", "Tuesday", "Wednesday", "Thursday")})
    pb, tb = site("South", own_b, {d: dinner for d in ("Thursday", "Friday", "Saturday", "Sunday")})
    sib = _spec("multi_location_org_north", "North location", "the sibling whose week is published first",
                pa, ta, o, c, {d: 5000 for d in DAYS}, cols={"location_group": "Benchmark Group",
                                                              "location_name": "North"})
    return _spec("multi_location_org", "Multi-location org (shared staff)",
                 "E-26 org siblings: a shared person never double-booked across locations", pb, tb, o, c,
                 {d: 4500 for d in DAYS}, cols={"location_group": "Benchmark Group", "location_name": "South"},
                 siblings=[sib])


ARCHETYPES = {
    "cafe_6": cafe_6, "bistro_22": bistro_22, "full_service_60": full_service_60, "large_120": large_120,
    "high_volume_250": high_volume_250, "mega_500": mega_500, "diner_24h": diner_24h, "qsr_teens": qsr_teens,
    "single_manager": single_manager, "multi_location_org": multi_location_org,
    "daypart_limited": daypart_limited, "zero_history": zero_history, "late_bar": late_bar,
    "mass_time_off": mass_time_off,
}


# ── building one into the database ─────────────────────────────────────

def _guard() -> None:
    """Refuse a real database or a provider key (the module docstring)."""
    import models
    roots = {os.path.realpath(tempfile.gettempdir()), os.path.realpath("/tmp"), os.path.realpath("/private/tmp"),
             os.path.realpath("/private/var/folders")}
    path = os.path.realpath(models.DB_PATH)
    if not any(path.startswith(r + os.sep) for r in roots):
        raise BenchmarkRefused(f"the benchmark writes restaurants; {models.DB_PATH} is not a temporary database "
                               "(point RAILWAY_VOLUME_MOUNT_PATH at an empty temporary directory first)")
    live = [k for k in PROVIDER_KEYS if (os.environ.get(k) or "").strip()]
    if live:
        raise BenchmarkRefused(f"the benchmark makes no model call; unset {', '.join(live)}")


def _holiday_dates(year) -> set:
    try:
        import schedule_economics
        return set(schedule_economics._holiday_dates(year))
    except Exception:
        return set()


def next_week_start(week_start_day=0, today=None, avoid_holidays=True) -> str:
    """The first date after `today` on the restaurant's week-start weekday
    (0 = Monday) — the week a Generate press drafts — moved on a week at a
    time (at most three) past any week holding a holiday on the engine's
    own calendar (schedule_economics), so the same benchmark run in late
    November measures an ordinary week, not Thanksgiving. A generation must
    run on today's clock (sales on file are judged against today, D-33), so
    the week cannot be pinned to a date; it can be kept ordinary."""
    t = today or _date.today()
    start = t + _td(days=(int(week_start_day or 0) - t.weekday()) % 7 or 7)
    for _ in range(3 if avoid_holidays else 0):
        days = {(start + _td(days=i)).isoformat() for i in range(7)}
        if not days & (_holiday_dates(start.year) | _holiday_dates(start.year + 1)):
            break
        start += _td(days=7)
    return start.isoformat()


def _write_restaurant(spec, owner_email) -> int:
    import models
    from models import Restaurant, create_restaurant
    rid = create_restaurant(Restaurant(name=spec["label"], owner_email=owner_email))
    cols = {"open_times_json": json.dumps(spec["open"]), "close_times_json": json.dumps(spec["close"]),
            "role_rates_json": json.dumps(spec["rates"]), "timezone": "America/Chicago"}
    if spec["salaried"]:
        cols["salaried_staff_json"] = json.dumps(spec["salaried"])
    if spec["floors"]:
        cols["role_floors_json"] = json.dumps(spec["floors"])
    if spec["closed_days"] and "compliance_json" not in spec["cols"]:
        # The archetype's closed weekdays are the restaurant's closures, as
        # an owner saves them (schedule_rules.closures): with only its hours
        # on file a closed Monday read as a trading day the manager plan had
        # to staff (found by workstream R, 10/14/26).
        cols["compliance_json"] = json.dumps({"closed_weekdays": list(spec["closed_days"])})
    cols.update(spec["cols"])
    models.update_restaurant(rid, cols)
    return rid


def _write_history(rid, spec, today) -> int:
    """The pattern repeated for spec["weeks"] weeks up to yesterday, as the
    shifts upload, and the days' sales."""
    import models
    weeks = int(spec["weeks"] or 0)
    if not weeks or not spec["pattern"]:
        return 0
    end = today
    start = end - _td(days=7 * weeks)
    lines = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes"]
    d = start
    while d < end:
        wd = d.strftime("%A")
        if wd not in spec["closed_days"]:
            for (day, emp, role, s, e) in spec["pattern"]:
                if day != wd:
                    continue
                hrs = ((_m(e) - _m(s)) % 1440 or 1440) / 60
                lines.append(f"{d.isoformat()},{wd},{emp},{role},{s},{e},{hrs:.2f},{hrs:.2f},"
                             f"{spec['sales'].get(wd, 0)},")
        d += _td(days=1)
    models.save_client_data(rid, "shifts", "\n".join(lines) + "\n")
    conn = models.get_conn()
    try:
        d = start
        while d < end:
            wd = d.strftime("%A")
            s_ = spec["sales"].get(wd, 0)
            if s_ and wd not in spec["closed_days"]:
                conn.execute("INSERT OR REPLACE INTO labor_daily_history (restaurant_id, date, day_of_week, sales, "
                             "labor_cost, labor_pct, total_hours) VALUES (?,?,?,?,?,?,?)",
                             (rid, d.isoformat(), wd, s_, round(s_ * 0.28, 2), 28.0, 0))
            d += _td(days=1)
        conn.commit()
    finally:
        conn.close()
    return len(lines) - 1


def _write_people(rid, spec, week_dates) -> None:
    import models
    import staff_settings as ss
    import time_off as to
    for name, role, settings in spec["people"]:
        models.add_manual_team_member(rid, name, role=role)
        if settings:
            ss.upsert(rid, name, **settings)
    for name, a, b in spec["time_off"]:
        got, err = to.request_time_off(rid, name, week_dates[a], week_dates[b], reason="benchmark")
        if err:
            raise RuntimeError(f"time off for {name} refused: {err}")
        to.decide(rid, dict(got)["id"], True, decided_by="owner")


def build(spec, today=None) -> dict:
    """The archetype in the current (temporary) database: {rid, week_start,
    week_dates, siblings: [rid]}. Siblings (another location of the same
    owner) are built first under the same owner email."""
    _guard()
    today = today or _date.today()
    email = f"bench-{spec['key']}-{random.Random(spec['key']).randint(1000, 9999)}@example.com"
    wsd = int(spec["cols"].get("week_start_day") or 0)
    week_start = next_week_start(wsd, today)
    week_dates = [(_date.fromisoformat(week_start) + _td(days=i)).isoformat() for i in range(7)]
    sib_ids = []
    for sib in spec["siblings"]:
        srid = _write_restaurant(sib, email)
        _write_history(srid, sib, today)
        _write_people(srid, sib, week_dates)
        sib_ids.append(srid)
    rid = _write_restaurant(spec, email)
    _write_history(rid, spec, today)
    _write_people(rid, spec, week_dates)
    return {"rid": rid, "week_start": week_start, "week_dates": week_dates, "siblings": sib_ids}


# ── one generation ─────────────────────────────────────────────────────

def generate(rid, week_start, arm) -> dict:
    """The real generation job on the code-only draft source `arm`, its
    finish captured: {status, result, seconds}."""
    import ops
    import schedule_engine as se
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}: {ARMS}")
    _guard()
    done = {}
    real = ops.finish_async_job
    prev = os.environ.get("SCHEDULE_DRAFT_SOURCE")
    ops.finish_async_job = lambda j, s, r: done.update(status=s, result=r)
    os.environ["SCHEDULE_DRAFT_SOURCE"] = arm
    t0 = time.monotonic()
    c0 = cpu_seconds()
    try:
        se._run_schedule_job(None, rid, week_start=week_start)
    finally:
        ops.finish_async_job = real
        if prev is None:
            os.environ.pop("SCHEDULE_DRAFT_SOURCE", None)
        else:
            os.environ["SCHEDULE_DRAFT_SOURCE"] = prev
    return {"status": done.get("status"), "result": done.get("result") or {},
            "seconds": round(time.monotonic() - t0, 2), "cpu_seconds": round(cpu_seconds() - c0, 1)}


def cpu_seconds() -> float:
    """CPU this process and its finished children have used (user + system):
    a solve run in its own process (schedule_cpsat.run) counts too."""
    import resource
    tot = 0.0
    for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN):
        ru = resource.getrusage(who)
        tot += ru.ru_utime + ru.ru_stime
    return tot


def saved_rows(history_id) -> list:
    import models
    import schedule_versions as v
    conn = models.get_conn()
    try:
        r = conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (history_id,)).fetchone()
    finally:
        conn.close()
    return v.rows_from_csv(r["schedule_csv"]) if r else []


def _publish_directly(history_id) -> None:
    """The sibling's week stands as published (published_at set): what the
    second location's generation reads (schedule_rules._sibling_weeks). The
    publish flow's notifications are not what is measured here."""
    import models
    conn = models.get_conn()
    try:
        conn.execute("UPDATE schedule_history SET published_at=? WHERE id=?",
                     (time.strftime("%Y-%m-%d %H:%M:%S"), history_id))
        conn.commit()
    finally:
        conn.close()


# ── the measures ───────────────────────────────────────────────────────

def _span(row):
    import schedule_time as _st
    return _st.span(row)


def _fam(role) -> str:
    import shift_quality as sq
    return sq.role_family(role)


def _need(spec, week_dates) -> dict:
    """{(date, half-hour minute on the business timeline, role family):
    people} — the archetype's own usual crew (its pattern on that weekday),
    else its floors over the open hours when it has no history. Closed
    days need nobody."""
    need = {}
    by_day = {}
    for (day, _n, role, s, e) in spec["pattern"]:
        by_day.setdefault(day, []).append((role, s, e))
    for d in week_dates:
        wd = _date.fromisoformat(d).strftime("%A")
        if wd in spec["closed_days"]:
            continue
        if by_day.get(wd):
            for role, s, e in by_day[wd]:
                if _fam(role) == "manager":
                    continue
                sp = _span({"shift_start": s, "shift_end": e})
                if not sp:
                    continue
                for t in range(sp[0] - sp[0] % 30, sp[1], 30):
                    if t >= sp[0]:
                        k = (d, t, _fam(role))
                        need[k] = need.get(k, 0) + 1
        elif spec["floors"]:
            o, c = _m(spec["open"][wd]), _m(spec["close"][wd])
            if c <= o:
                c += 1440
            split = 15 * 60
            for role, parts in spec["floors"].items():
                for t in range(o, c, 30):
                    k_ = int(parts.get("morning" if t < split else "night") or 0)
                    if k_:
                        need[(d, t, _fam(role))] = k_
    return need


def _slot_values(spec, need) -> tuple:
    """(value of one person short for one half hour, by (date, half-hour,
    role family); which pricing). schedule_objective's slot_value when the
    module is present (contract #1); else the day's sales over its needed
    person-half-hours."""
    try:
        import schedule_objective as _obj           # contract #1 (workstream S)
    except ImportError:
        _obj = None
    if _obj is not None and hasattr(_obj, "slot_value") and hasattr(_obj, "ObjectiveContext"):
        try:
            # the need itself is the demand the weights are read from (a
            # busy day's half hour needs more people than a quiet one's)
            by = {}
            for (d, t, f), n in need.items():
                by.setdefault((d, f), {})[int(t)] = int(n)
            ctx = _obj.ObjectiveContext.build(spec.get("_rid"), sorted({k[0] for k in need}), need=by)
            return {k: float(_obj.slot_value(ctx, k[0], k[1], k[2]) or 0.0) for k in need}, "schedule_objective"
        except Exception as e:                      # never costs the benchmark
            print(f"[benchmark] schedule_objective.slot_value unusable ({e!r}); the stated fallback prices")
    if not any(spec["sales"].values()):
        return {}, "no_sales_on_file"
    per_day = {}
    for (d, _t, _f), n in need.items():
        per_day[d] = per_day.get(d, 0) + n
    out = {}
    for k in need:
        wd = _date.fromisoformat(k[0]).strftime("%A")
        sales = float(spec["sales"].get(wd) or 0)
        out[k] = round(sales / per_day[k[0]], 4) if per_day[k[0]] and sales else 1.0
    return out, "day_sales_per_needed_person_half_hour"


def _on(rows, week_dates) -> dict:
    """{(date, half-hour, role family): people on} for the rows."""
    on = {}
    for r in rows:
        sp = _span(r)
        if not sp or r.get("date") not in week_dates:
            continue
        f = _fam(r.get("role"))
        for t in range(sp[0] - sp[0] % 30, sp[1], 30):
            if t >= sp[0]:
                k = (r["date"], t, f)
                on[k] = on.get(k, 0) + 1
    return on


def _independent(rows, spec, c, week_dates, sibling_rows=()) -> dict:
    """Checks that trust nothing in the engine (module docstring)."""
    import schedule_rules as sr
    out = {}

    def add(kind, item):
        out.setdefault(kind, []).append(item)

    by_person = {}
    managers = {n.lower() for n, r, _s in spec["people"] if _fam(r) == "manager"}
    minors1415 = {n.lower() for n, _r, s in spec["people"] if (s or {}).get("minor_age_band") == "14-15"}
    roster = {n.lower() for n, _r, _s in spec["people"]}
    off = {}
    for n, a, b in spec["time_off"]:
        for d in week_dates[a:b + 1]:
            off.setdefault(n.lower(), set()).add(d)
    closed = {d for d in week_dates if _date.fromisoformat(d).strftime("%A") in spec["closed_days"]}
    w0 = _date.fromisoformat(week_dates[0])
    for r in rows:
        sp = _span(r)
        name = " ".join(str(r.get("employee") or "").split()).lower()
        if not sp or not name:
            continue
        d = r.get("date")
        base = (_date.fromisoformat(d) - w0).days * 1440
        by_person.setdefault(name, []).append((base + sp[0], base + sp[1], r))
        if d in closed:
            add("row_on_closed_date", (r["employee"], d))
        if d in off.get(name, ()):
            add("row_on_time_off", (r["employee"], d))
        if name not in roster:
            add("not_on_roster", (r["employee"], d))
        if name in minors1415:
            school = sr.is_school_day(d)
            if school and sp[0] < 15 * 60 and sp[1] > 7 * 60:
                add("minor_14_15_school_hours", (r["employee"], d, r["shift_start"], r["shift_end"]))
            summer = 6 <= _date.fromisoformat(d).month <= 8
            if sp[1] > (21 if summer else 19) * 60:
                add("minor_14_15_late", (r["employee"], d, r["shift_end"]))
            if school and (sp[1] - sp[0]) > 180:
                add("minor_14_15_over_3h_school_day", (r["employee"], d, round((sp[1] - sp[0]) / 60, 2)))
    for name, lst in by_person.items():
        lst.sort(key=lambda t: (t[0], t[1]))
        for (a0, a1, ra), (b0, _b1, rb) in zip(lst, lst[1:]):
            if b0 < a1:
                add("overlap", (ra["employee"], ra["date"], rb["date"]))
    for sr_ in sibling_rows:
        name = " ".join(str(sr_.get("employee") or "").split()).lower()
        sp = _span(sr_)
        if not sp or name not in by_person or sr_.get("date") not in week_dates:
            continue
        base = (_date.fromisoformat(sr_["date"]) - w0).days * 1440
        for a, b, r in by_person[name]:
            if a < base + sp[1] and base + sp[0] < b:
                add("double_booked_across_locations", (r["employee"], r["date"]))
    # every minute someone is on and no manager is, within the open hours
    unmanaged = 0
    if managers:
        for i, d in enumerate(week_dates):
            wd = _date.fromisoformat(d).strftime("%A")
            if d in closed:
                continue
            o, cl = _m(spec["open"][wd]), _m(spec["close"][wd])
            if cl <= o:
                cl += 1440
            lo, hi = i * 1440 + o, i * 1440 + cl
            floor, mgr = [0] * (hi - lo), [0] * (hi - lo)
            for name, lst in by_person.items():
                for a, b, _r in lst:
                    for t in range(max(a, lo), min(b, hi)):
                        floor[t - lo] += 1
                        if name in managers:
                            mgr[t - lo] += 1
            unmanaged += sum(1 for t in range(hi - lo) if floor[t] and not mgr[t])
    return {"breaches": {k: len(v) for k, v in out.items()}, "examples": {k: v[:3] for k, v in out.items()},
            "unmanaged_open_minutes": unmanaged}


def measure(spec, built, rows, result, sibling_rows=()) -> dict:
    """Every metric of one saved week (module docstring)."""
    import models
    import schedule_rules as sr
    rid, week_dates = built["rid"], built["week_dates"]
    rest = models.get_restaurant(rid)
    c = sr.build_constraints(rid, week_dates, [_date.fromisoformat(d).strftime("%A") for d in week_dates], rest)
    viols = sr.violations(rows, c)
    hard = {}
    for v in viols:
        if v.get("hard"):
            hard[v["kind"]] = hard.get(v["kind"], 0) + 1
    ind = _independent(rows, spec, c, week_dates, sibling_rows)
    # unfilled against the archetype's own usual crew
    need = _need(spec, week_dates)
    spec["_rid"] = rid
    values, pricing = _slot_values(spec, need)
    on = _on(rows, week_dates)
    short_h, short_v, by_role = 0.0, 0.0, {}
    for k, n in need.items():
        gap = max(0, n - on.get(k, 0))
        if gap:
            short_h += gap * 0.5
            short_v += gap * values.get(k, 0.0)
            by_role[k[2]] = round(by_role.get(k[2], 0.0) + gap * 0.5, 1)
    need_days = {(k[0], k[2]) for k in need}
    have_days = {(k[0], k[2]) for k, n in on.items() if n}
    empty = sorted(need_days - have_days)
    # hours, dollars, overtime
    roles = {n.lower(): r for n, r, _s in spec["people"]}
    salaried = {s["name"].lower() for s in spec["salaried"]}
    hours, days = {}, {}
    lens = {}
    for r in rows:
        sp = _span(r)
        if not sp:
            continue
        h = (sp[1] - sp[0]) / 60.0
        name = " ".join(str(r.get("employee") or "").split()).lower()
        hours[name] = hours.get(name, 0.0) + h
        days.setdefault(name, set()).add(_date.fromisoformat(r["date"]).strftime("%A"))
        lens[int(round(h))] = lens.get(int(round(h)), 0) + 1
    dollars, ot = 0.0, 0.0
    for name, h in hours.items():
        if name in salaried:
            continue
        rate = float(spec["rates"].get(roles.get(name, ""), 14.0))
        over = max(0.0, h - 40.0)
        ot += over
        dollars += h * rate + over * 0.5 * rate
    budget = result.get("labor_budget_dollars") or None
    # usual days kept, starved
    usual_days, usual_hours = {}, {}
    for (day, n, _role, s, e) in spec["pattern"]:
        k = n.lower()
        usual_days.setdefault(k, set()).add(day)
        sp = _span({"shift_start": s, "shift_end": e})
        usual_hours[k] = usual_hours.get(k, 0.0) + ((sp[1] - sp[0]) / 60.0 if sp else 0.0)
    off_days = {}
    for n, a, b in spec["time_off"]:
        off_days.setdefault(n.lower(), set()).update(_date.fromisoformat(d).strftime("%A")
                                                     for d in week_dates[a:b + 1])
    possible, kept = 0, 0
    for k, ds in usual_days.items():
        for wd in ds - off_days.get(k, set()):
            possible += 1
            kept += 1 if wd in days.get(k, set()) else 0
    # A person shared with another location: the hours that location gave
    # them this week count toward theirs (they were not starved, they were
    # busy elsewhere).
    elsewhere = {}
    for r in sibling_rows:
        sp = _span(r)
        if sp and r.get("date") in week_dates:
            k = " ".join(str(r.get("employee") or "").split()).lower()
            elsewhere[k] = elsewhere.get(k, 0.0) + (sp[1] - sp[0]) / 60.0
    starved = sorted(k for k, uh in usual_hours.items()
                     if uh >= STARVED_USUAL_HOURS and not off_days.get(k)
                     and hours.get(k, 0.0) + elsewhere.get(k, 0.0) < STARVED_SHARE * uh)
    usual_long = any(((_span({"shift_start": s, "shift_end": e}) or (0, 0))[1] -
                      (_span({"shift_start": s, "shift_end": e}) or (0, 0))[0]) / 60.0 > LONG_SHIFT_HOURS
                     for (_d, n, role, s, e) in spec["pattern"] if _fam(role) != "manager")
    long_shifts = sum(1 for r in rows if _fam(r.get("role")) != "manager" and _span(r)
                      and (_span(r)[1] - _span(r)[0]) / 60.0 > LONG_SHIFT_HOURS + 1e-6)
    q = result.get("quality") or {}
    gate = {}
    hid = result.get("history_id")
    if hid:
        try:
            import schedule_publish as sp_
            rv = sp_.publish_review(rid, hid)
            gate = {"blockers": len(rv.get("blockers") or []),
                    "blocker_keys": sorted({str(b.get("key")) for b in rv.get("blockers") or []})}
        except Exception as e:
            gate = {"error": repr(e)[:160]}
    return {
        "rows": len(rows), "people_scheduled": len(hours),
        "hard": hard, "hard_total": sum(hard.values()),
        "independent": ind["breaches"], "independent_total": sum(ind["breaches"].values()),
        "independent_examples": ind["examples"], "unmanaged_open_minutes": ind["unmanaged_open_minutes"],
        "unfilled_hours": round(short_h, 1), "unfilled_value": round(short_v, 2) if values else None,
        "unfilled_pricing": pricing,
        "unfilled_by_role": by_role, "empty_role_days": len(empty), "empty_role_day_examples": empty[:6],
        "labor_dollars": round(dollars, 2), "budget_dollars": budget,
        "over_budget_dollars": round(max(0.0, dollars - float(budget)), 2) if budget else None,
        "overtime_hours": round(ot, 1), "long_shifts": long_shifts, "usual_has_long_shifts": usual_long,
        "shift_length_histogram": dict(sorted(lens.items())),
        "usual_days_kept_pct": round(100.0 * kept / possible, 1) if possible else None,
        "starved": starved, "quality": q.get("score"), "gate": gate,
        "max_person_hours": round(max(hours.values()), 1) if hours else 0.0,
    }


def _signature(rows) -> list:
    return sorted((r.get("date"), " ".join(str(r.get("employee") or "").split()).lower(), _fam(r.get("role")),
                   r.get("shift_start"), r.get("shift_end")) for r in rows)


def run_archetype(key, arms=ARMS, runs=RUNS, scale=1.0, today=None) -> dict:
    """Build archetype `key` once, then generate its week `runs` times on each
    arm: {key, label, why, people, week_start, arms: {arm: {metrics of the
    first run, deterministic, identical_runs, seconds, seconds_max, status,
    error}}, failed: {arm: [invariant]}}."""
    spec = ARCHETYPES[key](scale)
    built = build(spec, today=today)
    sibling_rows = []
    for srid in built["siblings"]:
        got = generate(srid, built["week_start"], ARMS[0])
        hid = (got["result"] or {}).get("history_id")
        if hid:
            _publish_directly(hid)
            sibling_rows += saved_rows(hid)
    out = {"key": key, "label": spec["label"], "why": spec["why"], "people": len(spec["people"]),
           "scale": scale, "week_start": built["week_start"], "arms": {}, "failed": {}}
    for arm in arms:
        sigs, secs, cpus, first = [], [], [], None
        for i in range(runs):
            got = generate(built["rid"], built["week_start"], arm)
            res = got["result"] or {}
            secs.append(got["seconds"])
            cpus.append(got.get("cpu_seconds") or 0.0)
            if got["status"] != "done" or not res.get("history_id"):
                first = first or {"status": got["status"], "error": str(res.get("error") or "")[:300]}
                sigs.append(None)
                continue
            rows = saved_rows(res["history_id"])
            sigs.append(_signature(rows))
            if first is None:
                first = measure(spec, built, rows, res, sibling_rows)
                first["status"] = "done"
                first["draft_source"] = ((res.get("draft_source") or {}).get("source")
                                         if isinstance(res.get("draft_source"), dict) else res.get("draft_source"))
        same = sum(1 for s in sigs[1:] if s is not None and s == sigs[0])
        first = dict(first or {})
        first.update(runs=runs, identical_runs=same + 1 if sigs and sigs[0] is not None else 0,
                     # one run proves nothing about the next press
                     deterministic=(None if runs < 2 else
                                    bool(sigs) and sigs[0] is not None and all(s == sigs[0] for s in sigs)),
                     seconds=round(sum(secs) / len(secs), 2) if secs else None, seconds_max=max(secs or [0]),
                     cpu_seconds=round(sum(cpus) / len(cpus), 1) if cpus else None)
        out["arms"][arm] = first
        out["failed"][arm] = failed_invariants(first)
    return out


# ── the gate ───────────────────────────────────────────────────────────

INVARIANTS = {
    "legal": "no independent legal breach (overlap, closed or time-off date, off roster, federal 14-15 rules, "
             "double-booked across locations)",
    "manager": "no minute of the open hours with someone on and no manager",
    "deterministic": "every Generate press gives the same week",
    "no_long_shifts": f"no shift over {LONG_SHIFT_HOURS:g}h unless the restaurant works them",
    "usual_days": f"at least {USUAL_DAYS_KEPT_MIN:g}% of each person's usual days kept",
    "nobody_starved": f"nobody who usually works {STARVED_USUAL_HOURS:g}h+ given under "
                      f"{int(STARVED_SHARE * 100)}% of it",
    "roles_staffed": "no open day with a needed role at zero people",
    "saved": "the generation saved a week",
}


def failed_invariants(m) -> list:
    """The INVARIANTS one arm's run breaks — [] when it holds them all."""
    if not m or m.get("status") != "done":
        return ["saved"]
    out = []
    if m.get("independent_total"):
        out.append("legal")
    if m.get("unmanaged_open_minutes"):
        out.append("manager")
    if m.get("deterministic") is False:
        out.append("deterministic")
    if m.get("long_shifts") and not m.get("usual_has_long_shifts"):
        out.append("no_long_shifts")
    if m.get("usual_days_kept_pct") is not None and m["usual_days_kept_pct"] < USUAL_DAYS_KEPT_MIN:
        out.append("usual_days")
    if m.get("starved"):
        out.append("nobody_starved")
    if m.get("empty_role_days"):
        out.append("roles_staffed")
    return out


# ── the table ──────────────────────────────────────────────────────────

COLUMNS = (("people", "people"), ("hard", "hard_total"), ("indep", "independent_total"),
           ("no-mgr min", "unmanaged_open_minutes"), ("unfilled h", "unfilled_hours"),
           ("unfilled $", "unfilled_value"), ("labor $", "labor_dollars"), ("budget $", "budget_dollars"),
           ("OT h", "overtime_hours"), (">10h", "long_shifts"), ("usual %", "usual_days_kept_pct"),
           ("starved", "starved"), ("empty role-days", "empty_role_days"), ("det", "identical_runs"),
           ("sec", "seconds"), ("CPU s", "cpu_seconds"), ("RSS MB", "peak_rss_mb"),
           ("solve RSS MB", "child_peak_rss_mb"), ("quality", "quality"), ("gate", "gate"))


def _cell(m, key, people):
    if key == "people":
        return str(people)
    v = m.get(key)
    if key == "starved":
        return str(len(v or []))
    if key == "identical_runs":
        return f"{v}/{m.get('runs')}" if v is not None else "-"
    if key == "gate":
        return str((v or {}).get("blockers", "-"))
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.1f}" if abs(v) < 1000 else f"{v:,.0f}"
    return str(v)


def table(results) -> str:
    """The comparison as a Markdown table: one row per archetype and arm."""
    head = "| archetype | arm | " + " | ".join(h for h, _k in COLUMNS) + " | fails |"
    lines = [head, "|" + "---|" * (len(COLUMNS) + 3)]
    for res in results:
        for arm, m in res["arms"].items():
            if m.get("status") != "done":
                lines.append(f"| {res['key']} | {arm} | " + " | ".join("-" for _ in COLUMNS) +
                             f" | not saved: {m.get('status')} {m.get('error', '')[:60]} |")
                continue
            lines.append(f"| {res['key']} | {arm} | " + " | ".join(_cell(m, k, res["people"]) for _h, k in COLUMNS)
                         + " | " + (", ".join(res["failed"].get(arm) or []) or "none") + " |")
    return "\n".join(lines)


def peak_rss_mb(children=False) -> float:
    """This process's peak resident memory, MB (ru_maxrss is bytes on macOS,
    kilobytes on Linux) — or, `children`, the largest finished child's (a
    solve run in its own process, schedule_cpsat.run)."""
    import resource
    r = resource.getrusage(resource.RUSAGE_CHILDREN if children else resource.RUSAGE_SELF).ru_maxrss
    return round(r / (1024.0 * 1024.0) if sys.platform == "darwin" else r / 1024.0, 1)
