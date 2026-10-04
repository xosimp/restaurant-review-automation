"""Schedule re-audit 10/4/26, workstream PIPE, through the routes: the
manager plan's pin survives every round trip (UI-6) — save, reload, apply
fixes and Improve.

The pin is the plan's mark in the row's notes ("Cavnar AI: manager plan —
why"), which the stored week keeps and every client sends back; the server
reads it into "_pinned" / "_pin_reason" on every path rows come in by
(schedule_skeleton.mark_pins), and a save carries it from the week as
stored when a client dropped it (carry_pins).
"""
from flask import Flask

from test_edge_sched_publish import (db, save_app, _restaurant, _save, _mobile_app, _bearer, _post_save,  # noqa: F401
                                     _one, W1, HEADER)
import schedule_engine
import schedule_optimizer
import schedule_rules
import schedule_skeleton as skel
import schedule_versions as sv
import shift_quality
import strategy_routes

PLAN = "Cavnar AI: manager plan — covers open to close"


def _csv(lines):
    return HEADER + "\n" + "\n".join(lines)


def _week():
    return _csv([f"{W1[0]},Monday,Mia,Server,10:00am,6:00pm,8,{PLAN}",
                 f"{W1[0]},Monday,Ana,Server,4:00pm,10:00pm,6,"])


def test_rows_from_a_client_keep_the_plans_pin_from_the_notes_or_the_row():
    body = {"rows": [
        {"date": W1[0], "employee": "Mia", "role": "Manager", "shift_start": "10:00am", "shift_end": "6:00pm",
         "notes": PLAN},
        {"date": W1[0], "employee": "Lu", "role": "Manager", "shift_start": "6:00pm", "shift_end": "11:00pm",
         "notes": "", "_pinned": "manager_plan", "_pin_reason": "covers close", "_rid": "r9"},
        {"date": W1[0], "employee": "Ana", "role": "Server", "shift_start": "4:00pm", "shift_end": "10:00pm",
         "notes": "", "_pinned": "kept"}]}
    rows = strategy_routes._rows_from_body(body)
    assert rows[0]["_pinned"] == "manager_plan" and rows[0]["_pin_reason"] == "covers open to close"
    # A pin the client kept puts the plan's mark back on the row, so a save keeps it.
    assert rows[1]["_pinned"] == "manager_plan" and rows[1]["_rid"] == "r9"
    assert skel.is_plan_note(rows[1]["notes"]) and "covers close" in rows[1]["notes"]
    # A redo's "kept" holds for that job alone, never from a client.
    assert "_pinned" not in rows[2]


def _stub_inputs(monkeypatch, rid):
    c = schedule_rules.Constraints(restaurant_id=rid, week_dates=list(W1), week_days=[])
    monkeypatch.setattr(strategy_routes, "_may_draft", lambda u: True)
    monkeypatch.setattr(schedule_engine, "quality_inputs_from_db", lambda *a, **k: {"constraints": c})
    monkeypatch.setattr(schedule_engine, "_quality_signals", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(schedule_engine, "_score_schedule_quality", lambda *a, **k: ({"score": 80}, {}))
    monkeypatch.setattr(schedule_engine, "present_quality", lambda *a, **k: None)


def test_apply_fixes_and_improve_see_the_pin_and_hand_it_back(db, monkeypatch):
    rid = _restaurant(db, module_labor=1)
    _stub_inputs(monkeypatch, rid)
    rows = sv.rows_from_csv(_week())                           # what a reopened week sends: no "_pinned"
    seen = {}

    def fixes(rows_in, *a, **k):
        seen["fixes"] = [r.get("_pinned") for r in rows_in]
        return {"rows": rows_in, "fixes": [], "unfixed": []}

    def optimize(rows_in, *a, **k):
        seen["optimize"] = [r.get("_pinned") for r in rows_in]
        return {"rows": rows_in, "changes": [], "applied": False}
    monkeypatch.setattr(shift_quality, "apply_fixes", fixes)
    monkeypatch.setattr(schedule_optimizer, "optimize", optimize)
    monkeypatch.setattr(schedule_optimizer, "summary", lambda res, sig: {"changes": []})
    u = {"restaurant_id": rid, "id": 1, "username": "erik", "role": "owner"}
    with Flask(__name__).test_request_context("/labor/schedule/apply-fixes", method="POST", json={"rows": rows}):
        body, status = strategy_routes._do_schedule_apply_fixes(u)
    assert status == 200 and seen["fixes"] == ["manager_plan", None]
    assert body["rows"][0]["_pinned"] == "manager_plan"
    with Flask(__name__).test_request_context("/labor/schedule/optimize", method="POST", json={"rows": rows}):
        body, status = strategy_routes._do_schedule_optimize(u)
    assert status == 200 and seen["optimize"] == ["manager_plan", None]
    assert body["rows"][0]["_pinned"] == "manager_plan"


def test_a_reopened_week_shows_its_plan_rows_pinned(db):
    rid = _restaurant(db, module_labor=1)
    hid = _save(db, rid, W1, _week())
    r = _mobile_app(db).test_client().get(f"/mobile/api/labor/schedule-history/{hid}", headers=_bearer(db, rid))
    rows = r.get_json()["preview_rows"]
    assert [(x["employee"], x.get("_pinned"), x.get("_pin_reason")) for x in rows] == [
        ("Mia", "manager_plan", "covers open to close"), ("Ana", None, None)]


def test_a_save_keeps_the_pin_when_the_client_sent_the_row_without_its_mark(db, save_app):
    rid = _restaurant(db, module_labor=1)
    hid = _save(db, rid, W1, _week())
    sv.append(rid, hid, "generated", _week(), saved_by="Cavnar AI")
    rows = sv.rows_from_csv(_week())
    rows[0]["notes"] = ""                                       # a client that dropped the note
    rows[0]["shift_end"], rows[0]["scheduled_hours"] = "5:00pm", "7"     # and moved it by hand
    r = _post_save(save_app, _bearer(db, rid), rows, history_id=hid, version=1)
    assert r.status_code == 200, r.get_json()
    stored = sv.rows_from_csv(_one(db, "SELECT schedule_csv FROM schedule_history WHERE id=?", hid)["schedule_csv"])
    mia = [x for x in stored if x["employee"] == "Mia"][0]
    assert mia["shift_end"] == "5:00pm" and skel.plan_reason(mia["notes"]) == "covers open to close"
    # ...and it reads back pinned.
    again = _mobile_app(db).test_client().get(f"/mobile/api/labor/schedule-history/{hid}",
                                              headers=_bearer(db, rid, name="gm"))
    assert again.get_json()["preview_rows"][0]["_pinned"] == "manager_plan"


def test_a_plan_shift_given_to_somebody_else_is_no_longer_the_plans():
    stored = sv.rows_from_csv(_week())
    sent = [dict(stored[0], employee="Bo", notes=""), stored[1]]
    out = skel.carry_pins(sent, stored)
    assert "_pinned" not in out[0] and not skel.is_plan_note(out[0]["notes"])
