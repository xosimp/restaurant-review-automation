"""Scheduler audit 10/10/26, workstream E — ARCH-10: the model's JSON answer
reaches the engine as typed rows, never flattened to CSV and re-read by the
legacy free-text repairer; what the parse left out rides beside the rows."""
import json
import types

import labor
import schedule_engine as se

WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
_ANALYSIS = {"overall_labor_pct": 25, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
             "period_days": 0, "total_sales": 0}


class _Msg:
    def __init__(self, text, stop="end_turn"):
        self.content = [types.SimpleNamespace(type="text", text=text)]
        self.stop_reason = stop
        self.stop_details = None
        self.usage = None


def _answer(days):
    return json.dumps({"days": [{"date": d, "shifts": s} for d, s in days], "summary": ["Kept it lean."]})


def test_a_structured_answer_comes_back_as_typed_rows_with_what_it_left_out_beside_them(monkeypatch):
    ans = _answer([(WEEK[4], [{"employee": "Ana", "role": "Server", "start": "4:00pm", "end": "10:00pm"},
                              {"employee": "Ben", "role": "Line Cook", "start": "4:00pm", "end": "4:00pm"}])])
    monkeypatch.setattr(labor, "create_with_retry", lambda client, **kw: _Msg(ans))
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    out = labor.generate_optimized_schedule(_ANALYSIS, [], roster=[("Ana", "Server"), ("Ben", "Line Cook")],
                                            week_start=WEEK[0], hourly_rate=20, labor_target=30)
    assert out["rows"] == [{"date": WEEK[4], "day": "Friday", "employee": "Ana", "role": "Server",
                            "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6",
                            "notes": ""}]
    # The zero-length shift is named beside the rows, never as an in-band
    # "(left out)" pseudo-line the job had to recognise as text.
    assert len(out["dropped_rows"]) == 1 and "Ben" in out["dropped_rows"][0]
    assert "(left out)" not in out["schedule_csv"]


def test_typed_rows_are_never_put_through_the_csv_repairers_guesses():
    # A role that reads like a clock time is the CSV repairer's tell for a
    # dropped day column: it would have shifted every field. A schema row
    # cannot drop a column, so it is kept as written and only its weekday is
    # read from its date.
    row = {"date": WEEK[4], "day": "Tuesday", "employee": "Friday", "role": "4:00pm", "shift_start": "4:00pm",
           "shift_end": "10:00pm", "scheduled_hours": "6", "notes": "closer — NEEDS REVIEW: old"}
    (got, day, _text), = list(se._typed_answer_rows([row]))
    assert day == "Friday" and got["day"] == "Friday"
    assert got["employee"] == "Friday" and got["role"] == "4:00pm"
    assert got["notes"] == "closer"
    legacy = list(se._legacy_answer_rows(["h", ",".join(str(row[c]) for c in se._ANSWER_COLS)]))
    assert legacy[0][0]["employee"] == "Tuesday"              # the repairer's swap — never on typed rows


def test_the_call_loop_carries_typed_rows_end_to_end(monkeypatch):
    def gen(analysis, shifts, **kw):
        rows = [{"date": d, "day": se._date_of(d).strftime("%A"), "employee": "Ana", "role": "Server",
                 "shift_start": "11:00am", "shift_end": "3:00pm", "scheduled_hours": "4", "notes": ""}
                for d in WEEK]
        return {"schedule_csv": "header\n", "rows": rows, "dropped_rows": ["x: starts and ends at 4:00pm"],
                "narrative": [], "complete_dates": list(WEEK), "generation_seconds": 1}
    monkeypatch.setattr(labor, "generate_optimized_schedule", gen)
    out = se._generate_in_parts({}, [], [("Ana", "Server")], {"week_start": WEEK[0], "restaurant_id": None})
    assert [r["date"] for r in out["rows"]] == WEEK
    assert out["dropped_rows"] == ["x: starts and ends at 4:00pm"]
    # the CSV text is still written for the readers of schedule_csv
    assert out["schedule_csv"].count("\n") == len(WEEK)


def test_a_csv_contract_part_keeps_the_legacy_text_path(monkeypatch):
    def gen(analysis, shifts, **kw):
        body = "\n".join(f"{d},{se._date_of(d).strftime('%A')},Ana,Server,11:00am,3:00pm,4," for d in WEEK)
        return {"schedule_csv": "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + body,
                "rows": None, "narrative": [], "complete_dates": None, "generation_seconds": 1}
    monkeypatch.setattr(labor, "generate_optimized_schedule", gen)
    out = se._generate_in_parts({}, [], [("Ana", "Server")], {"week_start": WEEK[0], "restaurant_id": None})
    assert out["rows"] is None and out["schedule_csv"].count("Ana") == 7
