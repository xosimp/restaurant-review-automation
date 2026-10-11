"""Scheduler audit 10/10/26, workstream E — TRUST-15, AI-21: "Cavnar AI's
note" is written from the FINISHED week and every bullet is checked against
its rows; the web renders it as a list."""
import json
import os
import types

import schedule_narrative as sn

WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]


def _row(d, who, role="Server"):
    return {"date": d, "employee": who, "role": role, "shift_start": "4:00pm", "shift_end": "10:00pm",
            "scheduled_hours": "6"}


ROWS = [_row(WEEK[4], "Ben Cole"), _row(WEEK[4], "Cy Dee", "Line Cook"), _row(WEEK[5], "Ana Ruiz")]


class _Msg:
    def __init__(self, bullets):
        self.content = [types.SimpleNamespace(type="text", text=json.dumps({"bullets": bullets}))]
        self.stop_reason = "end_turn"
        self.usage = None


def test_a_bullet_about_a_week_that_no_longer_exists_is_dropped():
    # The solver moved Ana off Friday: "Ana closes Friday" is no longer true.
    assert "Friday" in sn.final_rows_problem("Ana closes Friday for continuity", ROWS, ["Ana Ruiz", "Ben Cole"])
    assert sn.final_rows_problem("Ana on Saturday dinner", ROWS, ["Ana Ruiz"]) == ""
    assert "no shift" in sn.final_rows_problem("Dana covers the bar", ROWS, ["Dana Fox"])
    assert "Monday" in sn.final_rows_problem("Monday kept lean", ROWS)
    assert sn.final_rows_problem("Kept the week lean", ROWS) == ""


def test_the_note_is_written_from_the_final_week(monkeypatch):
    seen = {}

    def call(req):
        seen["req"] = req
        return _Msg(["Ben and Cy hold Friday dinner", "Ana closes Friday for continuity"])
    out = sn.write(5, ROWS, changes=["Ana moved from Friday to Saturday"], unmet=[{"what": "1 short of the floor",
                                                                                "day": "Friday"}],
                   roster=["Ana Ruiz", "Ben Cole", "Cy Dee"], call=call)
    text = seen["req"]["messages"][0]["content"]
    assert "Fri: Line Cook Cy Dee; Server Ben Cole" in text and "Ana moved from Friday to Saturday" in text
    assert seen["req"]["model"] == "claude-haiku-5-5"
    assert out["source"] == "final_week" and out["bullets"] == ["Ben and Cy hold Friday dinner"]
    assert out["dropped"] and "Friday" in out["dropped"][0]["why"]


def test_with_no_model_the_generations_bullets_stand_only_where_the_week_holds_them(monkeypatch):
    monkeypatch.setenv("SCHEDULE_NOTE_WRITE", "0")
    out = sn.write(5, ROWS, model_bullets=["Ana closes Friday", "Kept the week lean"], roster=["Ana Ruiz"])
    assert out["source"] == "model_checked" and out["bullets"] == ["Kept the week lean"]


def test_the_web_renders_the_note_as_a_list_and_the_instruction_as_chips():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates",
                            "dashboard.html"), encoding="utf-8").read()
    assert "_escHtml(data.narrative)" not in src                          # the comma-joined run-on
    assert "for (var nb = 0; nb < narList.length; nb++)" in src
    assert "Your request, as rules" in src and "chipList[cb]" in src


def test_the_job_replaces_the_models_note_with_the_final_weeks():
    import inspect
    import schedule_engine as se
    src = inspect.getsource(__import__("schedule_job"))
    assert "_snar.write(rid, preview_rows" in src and 'result["narrative"] = _note["bullets"]' in src
