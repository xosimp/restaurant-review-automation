"""Blind re-audit 10/11/26 — the live fixes made at once: the gate's exact
fill appended CSV text to a list of rows (AttributeError, so the gate never
filled), a redo of a draft saved under an older week start snapped to the
restaurant's new week (EJ's moved Monday -> Wednesday on 10/10/26), and
importing scheduler loaded the production .env into any process."""
import datetime as dt
import pathlib

import pytest

import models
import schedule_engine as se
import schedule_versions as sv
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)


def test_the_gates_exact_fill_carries_the_solved_rows():
    week = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
            "2026-10-14,Wednesday,Ana,Server,4:00pm,10:00pm,6,\n")
    extra = [{"date": "2026-10-14", "day": "Wednesday", "employee": "Ben", "role": "Server",
              "shift_start": "5:00pm", "shift_end": "9:00pm", "scheduled_hours": "4", "notes": ""}]
    out = se._gate_fill_answer({"rows": []}, 1, None, ["2026-10-14"], extra_rows=extra, csv_text=week)
    assert out and {r["employee"] for r in out["rows"]} == {"Ana", "Ben"}


def test_a_redo_of_a_draft_saved_on_the_old_week_start_keeps_its_own_week():
    rid = create_restaurant(Restaurant(name="EJ", owner_email="e@x.test"))
    hid = models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 40, 100, 30,
                                       "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                                       "2026-10-15,Thursday,Ana,Server,4:00pm,10:00pm,6,\n", [])
    models.update_restaurant(rid, {"week_start_day": 2})
    today = dt.datetime(2026, 10, 10)
    req = se.generation_request(rid, dates=["2026-10-15"], history_id=hid, today=today)
    assert req["week_start"] == "2026-10-12"
    # The saved draft's own week, wherever its dates are named from.
    assert se.week_start_day_for(models.get_restaurant(rid), "2026-10-12") == 0
    # A week with no draft yet follows the restaurant's week start.
    assert se.week_start_day_for(models.get_restaurant(rid), "2026-10-21") == 2
    assert se.week_start_day_of(rid) == 2
    # A fresh Generate still drafts the restaurant's own (Wednesday) week.
    assert dt.date.fromisoformat(se.generation_request(rid, today=today)["week_start"]).weekday() == 2


def test_scheduler_never_loads_the_env_file_on_import():
    src = (pathlib.Path(__file__).resolve().parents[1] / "scheduler.py").read_text()
    assert "load_dotenv(" not in src
