"""How likely (10/19/26): every prediction carries the chance it comes true
from the forecast's own spread, a call under CALL_MIN_PROB is not made, and
the accuracy counts the range and the calls apart and sets what was said
against what came true."""
import math

import dsr
from dsr import predictions, store
from tests.test_dsr_narrative import rest, strong_night  # noqa: F401

PREV = "2026-09-18"
DAY = "2026-09-19"


def _fc(typical=10000.0, sd=0.12, n=8, low=8200.0, high=12000.0):
    return {"available": True, "weekday": "Saturday", "typical_sales": typical, "samples": n,
            "low": low, "high": high, "range_coverage_pct": 80, "log_sd": sd, "sd_n": n}


def test_a_clear_budget_call_states_how_likely():
    preds = predictions.build(_fc(typical=10000.0), budget_net=8500.0)
    call = [p for p in preds if p["key"] == "sales_budget"][0]
    assert call["op"] == "gt" and 0.85 < call["prob"] < 0.95
    assert call["text"].startswith("Sales expected above budget ($8,500) · ") and call["text"].endswith("% likely")
    rng = preds[0]
    assert rng["key"] == "sales_range" and rng["prob"] == 0.8 and rng["text"].endswith("· 80% likely")


def test_a_coin_flip_against_the_budget_is_not_called():
    preds = predictions.build(_fc(typical=10000.0), budget_net=9900.0)
    assert [p["key"] for p in preds] == ["sales_range"]


def test_without_enough_spread_the_call_is_made_the_old_way():
    preds = predictions.build(_fc(typical=10000.0, n=3, low=None, high=None), budget_net=9900.0)
    assert [p["key"] for p in preds] == ["sales_budget"]
    assert preds[0]["prob"] is None and preds[0]["text"] == "Sales expected above budget ($9,900)"


def test_p_above_is_the_t_interval_read_as_a_probability():
    fc = _fc(sd=0.1, n=8)
    assert abs(predictions.p_above(fc, 10000, 10000) - 0.5) < 1e-9
    hi = predictions.p_above(fc, 10000, 10000 * math.exp(-0.1 * (1 + 1 / 8) ** 0.5))
    assert 0.8 < hi < 0.84          # one scaled sd under the centre, t(7)
    assert predictions.p_above(_fc(n=4), 10000, 9000) is None


def test_a_weak_measured_rain_effect_is_not_called():
    weak = {"applies": True, "direction": "down", "median_lift_pct": -3.0, "n": 6}
    strong = {"applies": True, "direction": "down", "median_lift_pct": -25.0, "n": 6}
    wx = {"precip_pct": 80}
    assert not [p for p in predictions.build(_fc(), weather=wx, effects={"rain": weak}) if p["key"] == "rain"]
    rain = [p for p in predictions.build(_fc(), weather=wx, effects={"rain": strong}) if p["key"] == "rain"][0]
    assert rain["op"] == "lt" and rain["prob"] >= predictions.CALL_MIN_PROB and rain["text"].endswith("% likely")


def test_accuracy_counts_kinds_apart_and_calibrates(rest, db_path):  # noqa: F811
    store.init_dsr(db_path) if hasattr(store, "init_dsr") else None
    for i in range(6):
        day = f"2026-09-{10 + i:02d}"
        predictions.record(rest.id, f"2026-09-{9 + i:02d}", day, [
            {"key": "sales_range", "metric": "sales.net", "op": "between", "low": 1.0, "high": 2.0, "prob": 0.8,
             "text": "r", "basis": "b"},
            {"key": "sales_budget", "metric": "sales.net", "op": "gt", "value": 1.0, "prob": 0.7,
             "text": "c", "basis": "b"}], db_path=db_path)
        conn = store.get_conn(db_path)
        conn.execute("UPDATE dsr_predictions SET outcome=? WHERE restaurant_id=? AND for_date=? AND key='sales_range'",
                     (predictions.CORRECT if i < 5 else predictions.INCORRECT, rest.id, day))
        conn.execute("UPDATE dsr_predictions SET outcome=? WHERE restaurant_id=? AND for_date=? AND key='sales_budget'",
                     (predictions.CORRECT if i < 4 else predictions.INCORRECT, rest.id, day))
        conn.commit()
        conn.close()
    acc = predictions.accuracy(rest.id, "2026-09-20", db_path=db_path)
    assert acc["window_days"] == 90 and acc["graded"] == 12 and acc["correct"] == 9
    assert acc["by_kind"] == {"range": {"correct": 5, "graded": 6}, "calls": {"correct": 4, "graded": 6}}
    assert acc["calibration"] == {"said_pct": 75, "came_true_pct": 75, "n": 12}
    assert acc["detail"] == "Ranges held 5 of 6 · calls 4 of 6 · said 75%, came true 75%"
    owner_less = predictions.accuracy(rest.id, "2026-09-20", db_path=db_path, exclude_keys=["sales_budget"])
    assert owner_less["by_kind"]["calls"]["graded"] == 0 and "calls" not in owner_less["detail"]


def test_the_review_carries_each_items_probability(rest, db_path):  # noqa: F811
    predictions.record(rest.id, PREV, DAY, [
        {"key": "sales_range", "metric": "sales.net", "op": "between", "low": 15000.0, "high": 21000.0,
         "prob": 0.8, "text": "Sales between $15,000 and $21,000 · 80% likely", "basis": "b"}], db_path=db_path)
    predictions.grade(rest.id, DAY, strong_night(), db_path=db_path)
    y = predictions.review(rest.id, DAY, db_path=db_path)
    assert y["items"][0]["prob"] == 0.8 and y["items"][0]["outcome"] == predictions.CORRECT
    assert dsr.READY
