"""The rule sweep kept by person and by date, and the local scorer that
builds only the dates a move touched (schedule audit 10/3/26, workstream
D2: P-37, P-38, P-25's exact scoring).

What must hold above all: the kept sweep IS the sweep — the same entries
in the same order, over every kind of rule and every kind of edit — and the
exact scorer IS score_rows. Speed is only worth having on those terms.
"""
import json
import random
import time

import pytest

import schedule_rules as sr
import shift_quality as sq
from sched_d2_week import WEEK, big_week, row


def _variants():
    """The realistic week, and the same week with the rules nobody's base
    week exercises switched on: role start/end rules, the retired
    per-daypart switch with nobody to satisfy it, no managers with an
    acting one, a closed date, daily overtime, days off by hand."""
    rows, c, _sig = big_week()
    yield "base", rows, c
    rows, c, _sig = big_week(seed=3)
    c.role_times = {("server pm", "Friday", "night"): {"start": sr.parse_minutes("4:00pm"), "source": {}},
                    ("line cook", "Monday", "morning"): {"end": sr.parse_minutes("2:30pm"), "source": {}}}
    c.compliance = dict(c.compliance, daily_ot_hours=8, manager_on_duty=True)
    c.keyholders = set()
    yield "times+legacy", rows, c
    rows, c, _sig = big_week(seed=5)
    c.managers = {}
    c.acting_managers = {"p21": {WEEK[2], WEEK[6]}}
    c.closed_dates = {WEEK[0]}
    c.compliance = dict(c.compliance, min_consecutive_days_off=2)
    yield "acting+closed", rows, c
    # one person under two spellings, and two roster people an open "same
    # person?" question joins (D-8, E-25): the sweep reads each as one week
    rows, c, _sig = big_week(seed=8)
    c.aliases = {"pete": "p07"}
    c.linked = {"p11": "p11", "p12": "p11"}
    rows += [dict(r, employee="Pete") for r in rows if r["employee"] == "P07"][:2]
    yield "aliases+linked", rows, c


def _mutate(rng, rows):
    trial = [dict(r) for r in rows]
    k = rng.randrange(7)
    i, j = rng.randrange(len(trial)), rng.randrange(len(trial))
    if k == 0:
        trial[i]["employee"], trial[j]["employee"] = trial[j]["employee"], trial[i]["employee"]
    elif k == 1:
        trial[i]["shift_end"] = rng.choice(("11:30pm", "9:00pm", "1:00am"))
        trial[i]["scheduled_hours"] = str(sr.span_hours(trial[i]))
    elif k == 2:
        del trial[i]
    elif k == 3:
        trial.append(dict(trial[i], employee=trial[j]["employee"]))
    elif k == 4:
        trial[i]["employee"] = ""
    elif k == 5:
        trial[i]["date"] = rng.choice(WEEK)
    else:
        trial[i]["role"] = trial[j]["role"]
    return trial


@pytest.mark.parametrize("name,rows,c", list(_variants()), ids=lambda x: x if isinstance(x, str) else "")
def test_the_kept_sweep_is_the_sweep_exactly_over_randomised_edits(name, rows, c):
    """P-37: every trial a pass weighs re-sweeps only the people and dates
    it moved, and the answer is violations(rows, c) entry for entry, in
    order — so nothing that reads the sweep (the passes pick breaches out of
    it by position) can tell the difference."""
    sweep = sr.IncrementalSweep(c)
    rng = random.Random(len(name))
    current = [dict(r) for r in rows]
    kinds = set()
    for _ in range(120):
        trial = _mutate(rng, current)
        want = sr.violations(trial, c)
        kinds |= {v["kind"] for v in want}
        assert sweep.violations(trial) == want
        if rng.random() < 0.3:
            current = trial
    # the edits reached the rules they were meant to
    assert len(kinds) >= 10, kinds


def test_a_trial_re_sweeps_only_the_people_and_dates_it_moved():
    rows, c, _sig = big_week()
    sweep = sr.IncrementalSweep(c)
    sweep.violations(rows)
    people, dates = sweep.swept_people, sweep.swept_dates
    trial = [dict(r) for r in rows]
    a = next(i for i, r in enumerate(trial) if r["date"] == WEEK[1] and r["role"] == "Server PM")
    b = next(i for i, r in enumerate(trial) if r["date"] == WEEK[3] and r["role"] == "Server PM"
             and r["employee"] != trial[a]["employee"])
    trial[a]["employee"], trial[b]["employee"] = trial[b]["employee"], trial[a]["employee"]
    assert sweep.violations(trial) == sr.violations(trial, c)
    assert sweep.swept_people - people == 2 and sweep.swept_dates - dates == 2


def test_the_repair_passes_and_their_trials_never_sweep_the_whole_week_again(monkeypatch):
    """The overtime rebalance (up to 300 trials), the manager filler (120 a
    run), the person repairs (no limit), the close-out and the role time
    rules judged each trial with a whole-week sweep — about a thousand a
    generation (P-37). Now none sweeps the whole week at all."""
    rows, c, sig = big_week()
    c.role_times = {("server pm", "Friday", "night"): {"start": sr.parse_minutes("4:00pm"), "source": {}}}
    whole = []
    real = sr.violations

    def counted(rs, cc, person_only=False, day_only=False, **kw):
        if not person_only and not day_only and len(rs or []) > 40:
            whole.append(len(rs))
        return real(rs, cc, person_only=person_only, day_only=day_only, **kw)
    monkeypatch.setattr(sr, "violations", counted)
    roles = sig["roster_roles"]
    sr.rebalance_overtime(rows, c, roster_roles=roles)
    sr.close_out_gaps(rows, c)
    sr.cover_manager_gaps(rows, c)
    sr.fix_person_breaches(rows, c, roster_roles=roles)
    sr.apply_role_times(rows, c)
    assert whole == [], whole


def test_the_kept_sweep_is_several_times_cheaper_per_trial():
    rows, c, _sig = big_week()
    sweep = sr.IncrementalSweep(c)
    sweep.violations(rows)
    rng = random.Random(9)
    trials = []
    for _ in range(40):
        t = [dict(r) for r in rows]
        i, j = rng.randrange(len(t)), rng.randrange(len(t))
        t[i]["employee"], t[j]["employee"] = t[j]["employee"], t[i]["employee"]
        trials.append(t)
    t0 = time.process_time()
    for t in trials:
        sr.violations(t, c)
    full = time.process_time() - t0
    t0 = time.process_time()
    for t in trials:
        sweep.violations(t)
    kept = time.process_time() - t0
    assert kept < full / 2, (kept, full)


# ── the local scorer (P-38) ──────────────────────────────────────────────────

def _breaches(rows, c):
    v = sr.violations(rows, c)
    return ({(x["employee"].lower(), x["date"], x["shift_start"]) for x in v if x.get("no_show")},
            [x for x in v if x.get("hard")])


def test_the_local_scorer_builds_only_the_dates_a_move_touched():
    """score() used to build every context of the week on every call, so the
    "local" scorer re-did most of a whole-week score each time."""
    rows, c, sig = big_week()
    scorer = sq.LocalScorer(rows, **sig)
    built = scorer.dates_built
    assert built == len({r["date"] for r in rows})
    trial = [dict(r) for r in rows]
    i = next(k for k, r in enumerate(trial) if r["date"] == WEEK[4])
    trial[i]["shift_end"] = "11:30pm"
    trial[i]["scheduled_hours"] = str(sr.span_hours(trial[i]))
    scorer.score(trial)
    assert scorer.dates_built - built == 1
    # the same rows again: nothing built at all
    scorer.score(trial)
    assert scorer.dates_built - built == 1


def test_each_option_is_scored_with_its_own_unstanding_rows_and_breaches():
    """A pass hands each option's own sweep to the scorer (P-28): the cap a
    hard breach puts on a shift goes with the rows that carry it."""
    rows, c, sig = big_week()
    scorer = sq.LocalScorer(rows, **sig)
    fl, hb = _breaches(rows, c)
    with_own = scorer.score(rows, flagged=fl, hard_breaches=hb)
    full = sq.score_rows(rows, **dict(sig, flagged=fl, hard_breaches=hb))
    assert with_own == pytest.approx(full["raw_score"], abs=1e-3)
    assert with_own < scorer.score(rows)          # the breaches cost the week


def test_the_exact_scorer_is_score_rows_exactly_over_a_session_of_edits():
    """P-25: the Studio's re-score keeps one exact scorer per week being
    edited; whatever it reuses, its answer is score_rows' — every field."""
    rows, c, sig = big_week()
    sig["rotation"] = {"roles": {"Server PM": {"people": ["P05", "P10", "P15", "P20"], "weekend_due": ["P10"],
                                               "rest_from_close": ["P15"], "next_close": ["P20"]}}}
    scorer = sq.LocalScorer(rows, exact=True, **sig)
    rng = random.Random(11)
    current = [dict(r) for r in rows]
    for k in range(25):
        trial = [dict(r) for r in current]
        i = rng.randrange(len(trial))
        if k % 3 == 0:
            trial[i]["shift_end"] = "11:30pm"
            trial[i]["scheduled_hours"] = str(sr.span_hours(trial[i]))
        else:
            j = rng.randrange(len(trial))
            trial[i]["employee"], trial[j]["employee"] = trial[j]["employee"], trial[i]["employee"]
        fl, hb = _breaches(trial, c)
        got = scorer.evaluate(trial, flagged=fl, hard_breaches=hb)
        want = sq.score_rows(trial, **dict(sig, flagged=fl, hard_breaches=hb))
        assert json.dumps(got, sort_keys=True, default=str) == json.dumps(want, sort_keys=True, default=str)
        current = trial
    assert scorer.dates_built < 26 * len(WEEK)      # something was reused


def test_clock_and_date_strings_are_parsed_once_per_spelling():
    """The scorer's hot spot was strptime on the same few dozen strings."""
    sq._clock.cache_clear()
    rows, _c, sig = big_week()
    sq.score_rows(rows, **sig)
    info = sq._clock.cache_info()
    assert info.misses < 60 and info.hits > 10 * info.misses
    assert sr.parse_minutes("4:30pm") == 16 * 60 + 30 and sr.parse_minutes("16:30") == 990
    assert sq._slot_minutes(" 4:30 PM ") == 990 and sq._slot_minutes("") is None and sq._slot_minutes("x") is None
    assert sq.daypart_of("12:30am") == "night" and sq.daypart_of("9:00am") == "morning"
    assert sq._end_minutes("1:00am") == 25 * 60 and sq._end_minutes("?") == -1
