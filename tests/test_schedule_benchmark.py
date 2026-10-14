"""The schedule benchmark (blind re-audit 10/11/26 ARCH-22, AI-28, ARCH-19;
contract #7): archetype restaurants that are not Simple EJ's, run end to end
through the real generation job on the code-only draft sources, and the
invariants every heuristic must hold on every one of them.

The fast subset (schedule_benchmark.FAST_PLAN, about a minute) runs once per
module, in its own process with its own throwaway database and no provider
key (scripts/schedule_benchmark.run_batch). Each (archetype, arm, invariant)
is its own test; the ones the code breaks today are strict xfails naming the
finding whose fix makes them pass — a fix flips them and the gate ratchets.
"""
import datetime as dt
import importlib.util
import os

import pytest

import schedule_benchmark as sb
import schedule_cpsat as cs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _script():
    spec = importlib.util.spec_from_file_location("schedule_benchmark_script",
                                                  os.path.join(ROOT, "scripts", "schedule_benchmark.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── the archetypes are well-formed restaurants ──────────────────────────────

@pytest.mark.parametrize("key", sorted(sb.ARCHETYPES))
def test_every_archetype_is_a_consistent_restaurant(key):
    spec = sb.ARCHETYPES[key](1.0)
    names = {n for n, _r, _s in spec["people"]}
    assert len(names) == len(spec["people"]), "two people with one name"
    assert spec["open"].keys() == spec["close"].keys() == set(sb.DAYS)
    for (day, n, role, s, e) in spec["pattern"]:
        assert n in names and day in sb.DAYS and day not in spec["closed_days"]
        assert sb._m(s) is not None and sb._m(e) is not None
    # nobody usually works two overlapping shifts, or 7 days, in the usual week
    by = {}
    for (day, n, _r, s, e) in spec["pattern"]:
        by.setdefault(n, []).append(day)
    assert all(len(set(ds)) == len(ds) for ds in by.values())
    assert all(len(ds) <= 6 for n, ds in by.items() if n not in ("Mia Owner", "Owner Jo")), by
    assert spec["key"] == key and spec["why"]


def test_the_contract_archetypes_are_all_there():
    want = {"cafe_6", "bistro_22", "full_service_60", "large_120", "high_volume_250", "mega_500", "diner_24h",
            "qsr_teens", "single_manager", "multi_location_org", "daypart_limited", "zero_history"}
    assert want <= set(sb.ARCHETYPES)
    assert sb.ARCHETYPES["high_volume_250"]()["people"].__len__() == 250
    assert len(sb.ARCHETYPES["mega_500"]()["people"]) == 500
    assert sb.ARCHETYPES["full_service_60"]()["cols"]["week_start_day"] == 2   # a Wednesday week
    teens = sb.ARCHETYPES["qsr_teens"]()
    assert {s.get("minor_age_band") for _n, _r, s in teens["people"]} >= {"14-15", "16-17"}
    assert sb.ARCHETYPES["multi_location_org"]()["siblings"]


def test_the_week_is_the_next_one_on_the_restaurants_own_week_start_and_never_a_holiday_week():
    sun = dt.date(2026, 10, 11)
    assert sb.next_week_start(0, sun) == "2026-10-12"                 # Monday weeks
    assert sb.next_week_start(2, sun) == "2026-10-14"                 # Wednesday weeks
    # the Monday week holding Thanksgiving (11/26/26) is passed over
    assert sb.next_week_start(0, dt.date(2026, 11, 22)) == "2026-11-30"
    assert sb.next_week_start(0, dt.date(2026, 11, 22), avoid_holidays=False) == "2026-11-23"


# ── the measures trust nothing in the engine ──────────────────────────────

WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]


def _row(d, n, role, s, e):
    return {"date": d, "employee": n, "role": role, "shift_start": s, "shift_end": e}


def _tiny():
    return sb._spec("tiny", "Tiny", "unit", [("Max", "Manager", {}), ("Kid", "Crew", {"minor_age_band": "14-15"}),
                                             ("Ann", "Crew", {})],
                    [("Monday", "Ann", "Crew", "11:00am", "3:00pm"), ("Monday", "Max", "Manager", "11:00am", "3:00pm")],
                    {d: "11:00am" for d in sb.DAYS}, {d: "3:00pm" for d in sb.DAYS}, {d: 1000 for d in sb.DAYS},
                    time_off=[("Ann", 1, 1)], closed_days=("Sunday",))


def test_independent_checks_find_what_the_engine_must_never_let_through():
    spec = _tiny()
    rows = [_row(WEEK[0], "Max", "Manager", "11:00am", "3:00pm"),
            _row(WEEK[0], "Ann", "Crew", "11:00am", "2:00pm"), _row(WEEK[0], "Ann", "Crew", "1:00pm", "3:00pm"),
            _row(WEEK[1], "Ann", "Crew", "11:00am", "3:00pm"),                      # on approved time off
            _row(WEEK[2], "Kid", "Crew", "11:00am", "2:00pm"),                      # school hours, no manager
            _row(WEEK[3], "Kid", "Crew", "3:00pm", "8:00pm"),                       # past 7pm, over 3h
            _row(WEEK[6], "Ann", "Crew", "11:00am", "3:00pm"),                      # a closed day
            _row(WEEK[4], "Zed", "Crew", "11:00am", "3:00pm")]                      # not on the roster
    sib = [_row(WEEK[4], "Ann", "Crew", "10:00am", "12:00pm")]
    rows.append(_row(WEEK[4], "Ann", "Crew", "11:00am", "3:00pm"))
    got = sb._independent(rows, spec, None, WEEK, sibling_rows=sib)
    b = got["breaches"]
    assert b["overlap"] == 1 and b["row_on_time_off"] == 1 and b["row_on_closed_date"] == 1
    assert b["not_on_roster"] == 1 and b["double_booked_across_locations"] == 1
    assert b["minor_14_15_school_hours"] == 1 and b["minor_14_15_late"] == 1
    assert b["minor_14_15_over_3h_school_day"] == 1
    # Wednesday 11am-2pm and the rest of the week's crew-only hours: no manager on
    assert got["unmanaged_open_minutes"] >= 180


def test_unfilled_is_the_usual_crew_left_unstaffed_and_a_busy_days_hole_costs_more():
    spec = sb._spec("t", "T", "unit", [("A", "Server", {}), ("B", "Server", {})],
                    [("Monday", "A", "Server", "11:00am", "1:00pm"), ("Friday", "A", "Server", "11:00am", "1:00pm"),
                     ("Friday", "B", "Server", "11:00am", "1:00pm")],
                    {d: "11:00am" for d in sb.DAYS}, {d: "10:00pm" for d in sb.DAYS},
                    {"Monday": 400, "Friday": 4000})
    need = sb._need(spec, WEEK)
    assert need[(WEEK[0], 660, "server")] == 1 and need[(WEEK[4], 660, "server")] == 2
    values, pricing = sb._slot_values(spec, need)
    assert pricing in ("day_sales_per_needed_person_half_hour", "schedule_objective")
    assert values[(WEEK[4], 660, "server")] > values[(WEEK[0], 660, "server")]
    # the floors stand in when there is no history, and no sales means no dollar figure
    z = sb._spec("z", "Z", "unit", [("A", "Server", {})], [], {d: "11:00am" for d in sb.DAYS},
                 {d: "1:00pm" for d in sb.DAYS}, {}, floors={"Server": {"morning": 2, "night": 1}})
    zn = sb._need(z, WEEK)
    assert zn[(WEEK[0], 660, "server")] == 2 and len(zn) == 7 * 4
    # one objective (contract #1): a hole is priced at the stated rate whether
    # or not sales are on file
    zv, zp = sb._slot_values(z, zn)
    assert zp == "schedule_objective" and zv and all(v > 0 for v in zv.values())


def test_the_gate_reads_each_invariant():
    ok = {"status": "done", "independent_total": 0, "unmanaged_open_minutes": 0, "deterministic": True,
          "long_shifts": 0, "usual_days_kept_pct": 90.0, "starved": [], "empty_role_days": 0}
    assert sb.failed_invariants(ok) == []
    assert sb.failed_invariants(dict(ok, deterministic=None)) == []           # one run proves nothing
    assert sb.failed_invariants(dict(ok, deterministic=False)) == ["deterministic"]
    assert sb.failed_invariants(dict(ok, long_shifts=3)) == ["no_long_shifts"]
    assert sb.failed_invariants(dict(ok, long_shifts=3, usual_has_long_shifts=True)) == []
    assert sb.failed_invariants(dict(ok, usual_days_kept_pct=50.0, starved=["x"])) == ["usual_days", "nobody_starved"]
    assert sb.failed_invariants({"status": "error"}) == ["saved"]
    assert set(sb.INVARIANTS) >= {"legal", "manager", "deterministic", "no_long_shifts", "usual_days",
                                  "nobody_starved", "roles_staffed", "saved"}


def test_the_benchmark_refuses_a_real_database_and_a_provider_key(monkeypatch):
    import models
    monkeypatch.setattr(models, "DB_PATH", os.path.join(ROOT, "reviews.db"))
    with pytest.raises(sb.BenchmarkRefused):
        sb._guard()
    monkeypatch.setattr(models, "DB_PATH", os.path.join(os.path.realpath("/tmp"), "x", "reviews.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live")
    with pytest.raises(sb.BenchmarkRefused):
        sb._guard()
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    sb._guard()


def test_the_child_process_carries_no_provider_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live")
    monkeypatch.setenv("SOMEVENDOR_API_KEY", "x")
    env = _script()._child_env("/tmp/v")
    assert "ANTHROPIC_API_KEY" not in env and "SOMEVENDOR_API_KEY" not in env
    assert env["RAILWAY_VOLUME_MOUNT_PATH"] == "/tmp/v"


def test_the_table_has_a_row_per_archetype_and_arm():
    res = [{"key": "cafe_6", "people": 6, "arms": {
        "solver": {"status": "done", "hard_total": 1, "identical_runs": 3, "runs": 3, "gate": {"blockers": 1},
                   "starved": ["a"], "unfilled_value": 1954.59},
        "carry_forward": {"status": "error", "error": "boom"}},
        "failed": {"solver": ["roles_staffed"], "carry_forward": ["saved"]}}]
    t = sb.table(res).splitlines()
    assert len(t) == 4 and "| cafe_6 | solver |" in t[2] and "3/3" in t[2] and "roles_staffed" in t[2]
    assert "not saved" in t[3]


def test_the_pipeline_carries_no_name_from_a_removed_one_client_rule():
    """ARCH-22: the repair loop's floor top-up dates were still carried as
    pizza_added_dates (a pizza cook on every shift, one client's rule,
    removed long ago). They are the floors' dates by name now."""
    src = open(os.path.join(ROOT, "schedule_job.py"), encoding="utf-8").read()
    assert "pizza" not in src.lower()
    assert "backstop_floor_added_dates=ctx.floor_added_dates" in src


# ── the fast subset, end to end ────────────────────────────────────────────

# (archetype, arm, invariant) the code breaks today, and the finding whose
# fix makes it hold. Strict: a fix that makes one pass fails here until the
# entry is removed — the gate only ever tightens.
KNOWN = {
    ("cafe_6", "solver", "roles_staffed"): "STRESS-18 (R): the one manager's salaried cap leaves Sunday "
                                           "unmanaged, so the solver staffs nobody",
    ("cafe_6", "carry_forward", "manager"): "STRESS-18 (R): carried Sunday crew with no manager on",
    ("single_manager", "solver", "manager"): "STRESS-18 / STRESS-3 (R): one manager's week leaves a gap",
}


@pytest.fixture(scope="module")
def fast():
    if not cs.available():
        pytest.skip("OR-Tools not installed")
    plan = [[k, list(arms), runs, scale] for k, arms, runs, scale in sb.FAST_PLAN]
    return {r["key"]: r for r in _script().run_batch(plan, timeout=600)}


def _cases():
    out = []
    for key, arms, _runs, _scale in sb.FAST_PLAN:
        for arm in arms:
            for inv in sb.INVARIANTS:
                why = KNOWN.get((key, arm, inv))
                marks = [pytest.mark.xfail(strict=True, reason=why)] if why else []
                out.append(pytest.param(key, arm, inv, marks=marks, id=f"{key}-{arm}-{inv}"))
    return out


@pytest.mark.parametrize("key,arm,inv", _cases())
def test_the_fast_subset_holds_every_invariant(fast, key, arm, inv):
    m = fast[key]["arms"][arm]
    assert inv not in sb.failed_invariants(m), (inv, sb.INVARIANTS[inv], {
        k: m.get(k) for k in ("hard", "independent_examples", "unmanaged_open_minutes", "long_shifts",
                              "usual_days_kept_pct", "starved", "empty_role_day_examples", "identical_runs")})


def test_the_fast_subset_saves_a_week_on_both_code_arms_without_a_model(fast):
    for key, arms, runs, _scale in sb.FAST_PLAN:
        for arm in arms:
            m = fast[key]["arms"][arm]
            assert m["status"] == "done", (key, arm, m)
            assert m["draft_source"] in (arm, None) or arm in str(m["draft_source"])
            assert m["runs"] == runs and m["rows"] > 0 and m["seconds"] < 120
            assert m["unfilled_pricing"] in ("schedule_objective", "day_sales_per_needed_person_half_hour")


def test_no_legal_rule_is_broken_on_any_fast_archetype(fast):
    """The engine's own oracle agrees with the independent checks: no hard
    breach of a legal kind on any code-drafted week."""
    legal = {"overlap", "double_booked", "minor_too_late", "minor_too_early", "minor_daily_hours",
             "minor_weekly_hours", "approved_time_off", "unavailable_day", "unavailable_daypart", "elsewhere",
             "over_max_hours", "not_on_roster"}
    for key, r in fast.items():
        for arm, m in r["arms"].items():
            assert not legal & set(m["hard"]), (key, arm, m["hard"])
            assert m["independent_total"] == 0, (key, arm, m["independent_examples"])
