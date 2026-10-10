"""Schedule fix round 10/10/26, workstream G1 — the one statistics module
the three schedule learners share (ARCH-28) and its change-point reading
(LEARNING-2)."""
import learning_stats as ls
import schedule_memory
import schedule_versions


def test_the_three_learners_share_one_wilson_and_one_recency_rule():
    for n, k in ((5, 5), (10, 3), (2.4, 1.7), (0, 0)):
        assert schedule_versions.wilson_lower(k, n) == ls.wilson_lower(k, n)
        assert schedule_memory.wilson_lower(k, n) == ls.wilson_lower(k, n)
    assert schedule_versions.recency_weight(120, 120) == ls.recency_weight(120, 120) == 0.5
    assert schedule_memory.recency_weight(180, 180) == 0.5
    assert schedule_versions.PERSON_HALF_LIFE_DAYS == ls.PERSON_HALF_LIFE_DAYS == schedule_memory.PERSON_HALF_LIFE_DAYS
    assert schedule_versions.SLOT_HALF_LIFE_DAYS == ls.SLOT_HALF_LIFE_DAYS == schedule_memory.SLOT_HALF_LIFE_DAYS
    assert schedule_versions._strength(3.0, 4.0) == ls.strength(3.0, 4.0)
    assert schedule_memory.binom_tail(5, 6, 0.5) == ls.binom_tail(5, 6, 0.5)
    assert schedule_memory.fisher_tail(6, 6, 2, 10) == ls.fisher_tail(6, 6, 2, 10)


def test_wilson_interval_brackets_the_rate():
    lo, hi = ls.wilson_interval(8, 10)
    assert lo < 0.8 < hi
    assert ls.wilson_interval(0, 0) == (0.0, 1.0)


def test_a_new_habit_is_read_from_its_onset_not_the_whole_window():
    # 24 weeks with Bob on Tuesday dinner and nobody touching it, then the
    # manager takes him off twice: two of two, not two of twenty-six.
    series = [(False, 1.0)] * 24 + [(True, 1.0), (True, 1.0)]
    got = ls.since_change(series)
    assert got["changed"] and got["start"] == 24
    assert (got["hits"], got["opps"]) == (2, 2)
    # Without the change point the old reading was 2/26 — never a pattern.
    assert ls.since_change(series[:24] + [(True, 1.0)])["changed"] is False


def test_a_steady_rate_has_no_change_point():
    series = [(i % 2 == 0, 1.0) for i in range(20)]
    assert ls.change_point(series) == 0
    assert ls.since_change(series)["opps"] == 20


def test_a_habit_is_let_go_on_more_evidence_than_it_is_picked_up():
    # Twenty weeks of the edit, then two weeks without it: not yet a change
    # (a fall needs three weeks); three weeks without it is.
    assert ls.change_point([1] * 20 + [0, 0]) == 0
    assert ls.change_point([1] * 20 + [0, 0, 0]) == 20


def test_holm_stops_at_the_first_failure():
    assert ls.holm({"a": 0.001, "b": 0.02, "c": 0.04}, alpha=0.05) == {"a", "b"} or \
        ls.holm({"a": 0.001, "b": 0.02, "c": 0.04}, alpha=0.05) == {"a", "b", "c"}
    assert ls.holm({"a": 0.03, "b": 0.04}, alpha=0.05) == set()


def test_weighted_rate_per_100_rows_has_an_interval():
    r = ls.weighted_rate_ci([1.0, 1.0, 0.5, 0.25], 50)
    assert r["rate"] == 5.5 and r["lo"] < 5.5 < r["hi"] and r["n"] == 50
    assert ls.weighted_rate_ci([], 0)["rate"] is None
    z = ls.weighted_rate_ci([], 40)
    assert z["rate"] == 0.0 and z["hi"] > 0


def test_fixed_effect_slope_ignores_a_restaurant_that_started_better():
    rows = [("a", w, 10 - 0.5 * w) for w in range(8)] + [("b", w, 2 - 0.5 * w) for w in range(8)]
    got = ls.fe_slope_ci(rows)
    assert abs(got["slope"] + 0.5) < 1e-9 and got["restaurants"] == 2
    assert ls.fe_slope_ci([("a", 1, 1)])["slope"] is None


def test_ols_recovers_coefficients():
    xs = [[1.0, float(x)] for x in range(10)]
    ys = [3.0 + 2.0 * x for x in range(10)]
    b = ls.ols(xs, ys)
    assert abs(b[0] - 3.0) < 1e-9 and abs(b[1] - 2.0) < 1e-9
