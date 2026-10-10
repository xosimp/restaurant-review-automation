"""Schedule fix round 10/10/26, workstream D2: "Keep my edits" merges per
shift (ARCH-8) and never silently reverts a staff cover or swap
(BUGSLIFE-16)."""
import sys

import pytest
from flask import Flask

import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import schedule_economics, schedule_intel, shift_requests, shift_quality, staff_schedule  # noqa: E401,F401
import staff_settings, strategy_jobs, time_off, labor_replacements  # noqa: E401,F401

import auth, client_api, labor, mobile_api, models, push, schedule_versions, strategy_routes
import schedule_rules as sr
import schedule_versions as sv
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
CSV = (HEADER + "2026-10-12,Monday,Ana,Server,4:00pm,10:00pm,6,\n"
       "2026-10-13,Tuesday,Bob,Server,4:00pm,10:00pm,6,\n"
       "2026-10-14,Wednesday,Cy,Server,4:00pm,10:00pm,6,\n")


def _r(date, day, who, start="4:00pm", end="10:00pm"):
    return {"date": date, "day": day, "employee": who, "role": "Server", "shift_start": start,
            "shift_end": end, "scheduled_hours": "6", "notes": ""}


BASE = [_r("2026-10-12", "Monday", "Ana"), _r("2026-10-13", "Tuesday", "Bob"), _r("2026-10-14", "Wednesday", "Cy")]


# ── the merge itself ────────────────────────────────────────────────────────

def test_edits_to_different_shifts_both_survive():
    mine = [dict(BASE[0], shift_start="5:00pm"), BASE[1], BASE[2]]           # manager retimes Monday
    theirs = [BASE[0], dict(BASE[1], employee="Dee"), BASE[2]]                 # staff: Dee took Bob's Tuesday
    out = sv.merge_rows(BASE, mine, theirs)
    got = {(r["date"], r["employee"], r["shift_start"]) for r in out["rows"]}
    assert got == {("2026-10-12", "Ana", "5:00pm"), ("2026-10-13", "Dee", "4:00pm"), ("2026-10-14", "Cy", "4:00pm")}
    assert out["conflicts"] == [] and out["applied"] == 1


def test_only_the_same_shift_changed_twice_is_a_conflict():
    mine = [BASE[0], dict(BASE[1], employee="Eve"), BASE[2]]
    theirs = [BASE[0], dict(BASE[1], employee="Dee"), BASE[2]]
    out = sv.merge_rows(BASE, mine, theirs)
    assert len(out["conflicts"]) == 1 and out["conflicts"][0]["base"]["employee"] == "Bob"
    assert {r["employee"] for r in out["rows"]} == {"Ana", "Eve", "Cy"}


def test_additions_and_removals_merge():
    mine = [BASE[0], BASE[1], BASE[2], _r("2026-10-15", "Thursday", "Fay")]   # manager adds Thursday
    theirs = [BASE[0], BASE[1]]                                               # Wednesday removed by the other save
    out = sv.merge_rows(BASE, mine, theirs)
    assert {(r["date"], r["employee"]) for r in out["rows"]} == {
        ("2026-10-12", "Ana"), ("2026-10-13", "Bob"), ("2026-10-15", "Fay")}


def test_the_shift_key_uses_a_shift_id_when_the_rows_carry_one():
    assert sv.shift_key({"shift_id": "abc", "date": "x", "employee": "y"}) == ("id", "abc")
    assert sv.shift_key({"date": "2026-10-12", "shift_start": "4:00pm", "employee": " Ana "}) == \
        ("2026-10-12", "4:00pm", "ana")


# ── through the save route ─────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def redirect(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if mod is not None and getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, auth, schedule_versions, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    for mod in (models, auth, schedule_versions, sr):
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda *a, **k: {"is_live": True})
    monkeypatch.setattr(labor, "load_shifts_for_restaurant", lambda *a, **k: [])
    monkeypatch.setattr(shift_requests, "_tell_staff", lambda *a, **k: [])
    monkeypatch.setattr(shift_requests, "_tell_managers", lambda *a, **k: None)
    init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    yield


def _setup(db_path):
    rid = create_restaurant(Restaurant(name="Merge Co", owner_email="o@m.test", timezone="America/Chicago",
                                       module_labor=1), db_path=db_path)
    owner = create_user(rid, "will", "o@m.test", "pw-merge-1234", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    hid = models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 18, 40, 28, CSV, [], db_path=db_path)
    sv.append(rid, hid, "generated", CSV, db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    sv.append(rid, hid, "published", CSV, db_path=db_path)
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    tok = create_session(owner, device_type="ios", db_path=db_path)
    return rid, hid, app.test_client(), {"Authorization": "Bearer " + tok}


def _stored(db_path, hid):
    conn = models.get_conn(db_path)
    try:
        return sv.rows_from_csv(conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (hid,)).fetchone()[0])
    finally:
        conn.close()


def _staff_cover(db_path, rid, hid, frm, to, date):
    """A cover written the way shift_requests writes one: a `swap` version,
    with its request row covered."""
    rows = _stored(db_path, hid)
    for r in rows:
        if r["employee"] == frm and r["date"] == date:
            r["employee"] = to
    import schedule_engine
    conn = models.get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        sv.write_on(conn, rid, hid, "swap", schedule_engine._rows_to_csv_text(rows), saved_by=to)
        conn.execute("INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start, "
                     "shift_end, role, status, replacement_name, kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (rid, hid, frm, date, "4:00pm", "10:00pm", "Server", "covered", to, "drop"))
        conn.commit()
    finally:
        conn.close()


def test_keep_my_edits_keeps_the_staff_cover_and_my_other_edit(db_path):
    rid, hid, c, h = _setup(db_path)
    base = sv.list_versions(rid, hid, db_path=db_path)[-1]["version"]
    _staff_cover(db_path, rid, hid, "Bob", "Dee", "2026-10-13")
    mine = [dict(BASE[0], shift_start="5:00pm"), BASE[1], BASE[2]]
    r = c.post("/mobile/api/labor/schedule/score", headers=h, json={"rows": mine, "save": True, "history_id": hid,
                                                                     "version": base})
    assert r.status_code == 409 and r.get_json()["conflict"]
    r = c.post("/mobile/api/labor/schedule/score", headers=h,
               json={"rows": mine, "save": True, "history_id": hid, "version": base, "keep_mine": True})
    d = r.get_json()
    assert r.status_code == 200 and d["saved"], d
    stored = {(x["date"], x["employee"], x["shift_start"]) for x in _stored(db_path, hid)}
    assert ("2026-10-13", "Dee", "4:00pm") in stored, "the staff cover survives Keep my edits"
    assert ("2026-10-12", "Ana", "5:00pm") in stored
    assert {(x["date"], x["employee"]) for x in d["merged_rows"]} >= {("2026-10-13", "Dee")}


def test_keep_my_edits_on_the_covered_shift_names_it_and_needs_a_second_tap(db_path):
    rid, hid, c, h = _setup(db_path)
    base = sv.list_versions(rid, hid, db_path=db_path)[-1]["version"]
    _staff_cover(db_path, rid, hid, "Bob", "Dee", "2026-10-13")
    mine = [BASE[0], dict(BASE[1], employee="Eve"), BASE[2]]
    body = {"rows": mine, "save": True, "history_id": hid, "version": base, "keep_mine": True}
    r = c.post("/mobile/api/labor/schedule/score", headers=h, json=body)
    d = r.get_json()
    assert r.status_code == 409 and d["staff_conflict"] and d["staff_conflicts"][0]["now"] == "Dee"
    assert ("2026-10-13", "Dee") in {(x["date"], x["employee"]) for x in _stored(db_path, hid)}
    r = c.post("/mobile/api/labor/schedule/score", headers=h, json=dict(body, override_staff=True))
    assert r.status_code == 200 and r.get_json()["saved"]
    assert ("2026-10-13", "Eve") in {(x["date"], x["employee"]) for x in _stored(db_path, hid)}
    # The cover no longer reads as covered: closed, both people told.
    conn = models.get_conn(db_path)
    try:
        st = conn.execute("SELECT status FROM shift_change_requests WHERE restaurant_id=?", (rid,)).fetchone()[0]
    finally:
        conn.close()
    assert st == "cancelled"
