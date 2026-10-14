"""Schedule fix round 10/11/26, workstream G2 — the edit predictor's
backtest is a rolling origin, and its cost has a direction (blind re-audit
LEARNING-20)."""
import schedule_learning as sl


def _row(d, emp, start="5:00pm"):
    return {"date": d, "day": "", "employee": emp, "role": "Server", "shift_start": start, "shift_end": "10:00pm",
            "scheduled_hours": "5", "notes": ""}


def _weeks(n, edited_from=None):
    """n weeks of 70 rows; from week `edited_from` on, Zed's rows are always
    changed (a habit that starts late) — and a few others each week."""
    out = []
    for i in range(n):
        d = f"2026-0{1 + i // 4}-{10 + (i % 4) * 7:02d}"
        rows, edited = [], []
        for k, who in enumerate(("Ana", "Ben", "Cy", "Dee", "Eve", "Flo", "Gia", "Hal", "Ivy", "Zed")):
            for s in range(7):
                rows.append(_row(d, who, start=f"{4 + s % 3}:00pm"))
                edited.append((who == "Zed" and (edited_from is None or i >= edited_from)) or (k + s + i) % 13 == 0)
        out.append({"history_id": i + 1, "week_start": d, "rows": rows, "edited": edited})
    return out


def test_the_backtest_never_predicts_a_week_from_weeks_after_it():
    seen = []
    real = sl.fit_edit_model

    def spy(weeks):
        seen.append([w["history_id"] for w in weeks])
        return real(weeks)
    weeks = _weeks(8)
    try:
        sl.fit_edit_model = spy
        bt = sl.edit_prediction_backtest(list(reversed(weeks)))      # order given does not matter
    finally:
        sl.fit_edit_model = real
    for i, ids in enumerate(seen):
        assert ids == list(range(1, i + 1)), "a week was predicted from a later one"
    assert bt["skipped"] == sl.PREDICT_MIN_WEEKS and bt["weeks"] == 8 - sl.PREDICT_MIN_WEEKS


def test_a_habit_that_only_starts_late_is_not_credited_backwards():
    """Leave-one-out let the later weeks' Zed habit 'predict' the early weeks
    that had none — a hit rate earned on the future."""
    weeks = _weeks(8, edited_from=5)
    rolling = sl.edit_prediction_backtest(weeks)
    early = [w for w in rolling["per_week"] if w["week_start"] < weeks[5]["week_start"]]
    assert early and all(w["flagged"] == 0 for w in early)


def test_a_retimed_row_never_costs_the_person():
    weeks = _weeks(6)
    for w in weeks:
        w["features"] = sl.row_features(w["rows"])
        w["kinds"] = ["retime" if e and r["employee"] == "Zed" else ("move" if e else None)
                      for r, e in zip(w["rows"], w["edited"])]
    by_person, by_slot = sl._edit_kind_tallies(weeks)
    zed = sl.row_features([_row("2026-03-01", "Zed")])[0]
    ana = sl.row_features([_row("2026-03-01", "Ana")])[0]
    assert sl._edit_kind_for(zed, by_person, by_slot) == "retime"
    assert sl._edit_kind_for(ana, by_person, by_slot) == "move"


def test_the_edited_kinds_read_the_diff():
    d = {"removed": [{"date": "2026-03-01", "employee": "Ana", "shift_start": "5:00pm"}],
         "retimed": [{"date": "2026-03-01", "employee": "Zed", "old_start": "4:00pm"}],
         "role_changed": [{"date": "2026-03-02", "employee": "Cy", "shift_start": "5:00pm"}]}
    k = sl._edited_kinds(d)
    assert k[("2026-03-01", "ana", "5:00pm")] == "move"
    assert k[("2026-03-01", "zed", "4:00pm")] == "retime"
    assert k[("2026-03-02", "cy", "5:00pm")] == "role"
