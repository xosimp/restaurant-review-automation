"""rec_learning.kind_record — Historical Accuracy input (confidence audit CA6 §A)."""
import rec_learning


def _ep(key, verdict, rec_id, tracker=None, status="completed"):
    return {"rec_id": rec_id, "key": key, "kind": key.split(":")[0], "shown": True, "state": status,
            "verdict": verdict, "tracker": tracker or {}}


def test_counts_only_shown_taken_clear_and_floors_at_five():
    eps = [_ep("trim_day:Mon", "improved", i) for i in range(4)]
    r = rec_learning.kind_record(1, "trim_day", episodes=eps, restaurant=None)
    assert r["measured"] == 4 and r["rate"] is None and r["source"] in ("none", "cohort")
    eps.append(_ep("trim_day:Tue", "worsened", 9))
    eps.append(_ep("trim_day:Wed", "improved", 10, status="dismissed"))   # not taken
    eps.append(_ep("trim_day:Thu", "unknown", 11))                        # disowned etc. read unknown
    r = rec_learning.kind_record(1, "trim_day", episodes=eps)
    assert (r["measured"], r["improved"], r["source"]) == (5, 4, "own")
    assert r["rate"] == 0.8 and r["low"] < 0.8 < r["high"]


def test_five_worsened_is_zero_not_one():
    eps = [_ep("cut_waste:x", "worsened", i) for i in range(5)]
    r = rec_learning.kind_record(1, "cut_waste", episodes=eps)
    assert r["rate"] == 0.0 and r["source"] == "own"
