"""Schedule re-audit 10/4/26, UI lens: an owner's edits never vanish.

UI-1 / UI-4  the saved week's version travels with its rows (history detail,
             generation, save, Send), a version that changed no row is not a
             conflict, a real change by someone else still is.
UI-3         a copy of a week a newer one replaced is read-only: no save, no
             Send, no send-changes; the detail and publish-check say why.
UI-2 / UI-9  a rules save changes only what it sends, and a refused save
             stores nothing.
UI-10        a decided part-day time-off request keeps its label.

The reproductions are the auditor's (work-UI/test_ui_versions.py,
test_ui_superseded.py, test_ui_rules_wipe.py), turned to assert the fix.
"""
import sys
from datetime import date, timedelta

import pytest
from flask import Flask

# Imported before any fixture patches models.get_conn (the generation
# harness, tests/test_schedule_a2_generation.py).
import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import schedule_economics, schedule_intel, shift_requests, shift_quality, staff_schedule  # noqa: E401,F401
import staff_settings, strategy_jobs, time_off, labor_replacements  # noqa: E401,F401

import auth, client_api, labor, mobile_api, models, push, schedule_versions, strategy_routes
import schedule_rules as sr
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
CSV = (HEADER + "2026-10-12,Monday,Ana,Server,4:00pm,10:00pm,6,\n"
       "2026-10-13,Tuesday,Bob,Server,4:00pm,10:00pm,6,\n")


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
    monkeypatch.setattr(client_api, "publish_review", lambda *a, **k: {"blockers": [], "notes": []})
    init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    yield


def _setup(db_path, name="UI Co"):
    rid = create_restaurant(Restaurant(name=name, owner_email="o@ui.test", timezone="America/Chicago",
                                       module_labor=1), db_path=db_path)
    owner = create_user(rid, "will", "o@ui.test", "pw-ui-1234", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    hid = models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 12, 40, 28, CSV, [], db_path=db_path)
    schedule_versions.append(rid, hid, "generated", CSV, db_path=db_path)
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    tok = create_session(owner, device_type="ios", db_path=db_path)
    return rid, hid, app.test_client(), {"Authorization": "Bearer " + tok}


def _rows(tuesday_person, monday_person="Ana"):
    return [{"date": "2026-10-12", "day": "Monday", "employee": monday_person, "role": "Server",
             "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""},
            {"date": "2026-10-13", "day": "Tuesday", "employee": tuesday_person, "role": "Server",
             "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}]


def _save(c, h, hid, rows, version):
    body = {"rows": rows, "save": True, "history_id": hid}
    if version is not None:
        body["version"] = version
    return c.post("/mobile/api/labor/schedule/score", headers=h, json=body)


def _stored(db_path, hid):
    conn = models.get_conn(db_path)
    try:
        return conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (hid,)).fetchone()[0]
    finally:
        conn.close()


# ── UI-1 / UI-4: the version travels with the rows ─────────────────────────

def test_history_detail_hands_over_the_rows_with_their_version(db_path):
    rid, hid, c, h = _setup(db_path)
    d = c.get(f"/mobile/api/labor/schedule-history/{hid}", headers=h).get_json()
    assert d["ok"] and d["version"] == 1
    assert [r["employee"] for r in d["preview_rows"]] == ["Ana", "Bob"]
    s = _save(c, h, hid, _rows("Cy"), d["version"]).get_json()
    assert s["saved"] and s["version"] == 2
    d2 = c.get(f"/mobile/api/labor/schedule-history/{hid}", headers=h).get_json()
    assert d2["version"] == 2 and d2["preview_rows"][1]["employee"] == "Cy"


def test_save_after_send_on_the_same_screen_is_not_a_conflict_with_yourself(db_path):
    """UI-4: the auditor's repro — save, Send, save again from the same screen."""
    rid, hid, c, h = _setup(db_path)
    r1 = _save(c, h, hid, _rows("Cy"), 1)
    assert r1.status_code == 200 and r1.get_json()["version"] == 2
    rp = c.post("/mobile/api/labor/publish-schedule", headers=h, json={"schedule_id": hid, "acknowledge": True})
    assert rp.status_code == 200 and rp.get_json()["ok"]
    assert rp.get_json()["version"] == 3                      # the Send's own version comes back
    # The client that kept the save's version (2), as an older screen would.
    r2 = _save(c, h, hid, _rows("Dee"), 2)
    assert r2.status_code == 200, r2.get_json()
    assert "Dee" in _stored(db_path, hid)
    # And one that adopted the Send's version.
    r3 = _save(c, h, hid, _rows("Eve"), r2.get_json()["version"])
    assert r3.status_code == 200


def test_send_changes_returns_its_version_and_the_next_save_goes_through(db_path):
    rid, hid, c, h = _setup(db_path)
    c.post("/mobile/api/labor/publish-schedule", headers=h, json={"schedule_id": hid, "acknowledge": True})
    v = schedule_versions.list_versions(rid, hid, db_path=db_path)[-1]["version"]
    s = _save(c, h, hid, _rows("Cy"), v).get_json()
    assert s["saved"]
    rs = c.post("/mobile/api/labor/publish-schedule", headers=h, json={"schedule_id": hid, "acknowledge": True})
    out = rs.get_json()
    assert out["ok"] and out.get("changes_sent") and out["version"] == s["version"] + 1
    assert _save(c, h, hid, _rows("Dee"), s["version"]).status_code == 200


def test_stale_rows_from_an_older_version_are_refused_not_saved_over_a_newer_edit(db_path):
    """UI-1: the phone's cached rows (v1) must not overwrite the web's v2."""
    rid, hid, c, h = _setup(db_path)
    assert _save(c, h, hid, _rows("Cy"), 1).status_code == 200            # the web: Tuesday -> Cy, v2
    stale = _rows("Bob", monday_person="Eve")                              # the phone: its v1 rows + Monday
    r = _save(c, h, hid, stale, 1)
    body = r.get_json()
    assert r.status_code == 409 and body["conflict"] and body["latest_version"] == 2
    assert body["saved_by"] == "will" and body["lines"]                   # who changed what
    assert "Cy" in _stored(db_path, hid) and "Eve" not in _stored(db_path, hid)
    # Keep mine: the owner chose it, saved against the version they were shown.
    k = _save(c, h, hid, stale, body["latest_version"])
    assert k.status_code == 200 and "Eve" in _stored(db_path, hid)


def test_a_conflict_names_the_change_not_a_send_that_changed_nothing(db_path):
    rid, hid, c, h = _setup(db_path)
    _save(c, h, hid, _rows("Cy"), 1)
    c.post("/mobile/api/labor/publish-schedule", headers=h, json={"schedule_id": hid, "acknowledge": True})
    r = _save(c, h, hid, _rows("Zed"), 1).get_json()                     # based on v1, rows changed since
    assert r["conflict"] and r["latest_version"] == 3
    assert r["lines"] and r["lines"] != ["No changes in this version."]


def test_a_save_naming_no_version_is_still_refused(db_path):
    rid, hid, c, h = _setup(db_path)
    r = _save(c, h, hid, _rows("Cy"), None)
    assert r.status_code == 409 and r.get_json()["conflict"]
    assert "Cy" not in _stored(db_path, hid)


def test_rows_changed_since_compares_rows_not_numbers(db_path):
    rid, hid, c, h = _setup(db_path)
    schedule_versions.append(rid, hid, "published", CSV, db_path=db_path)            # v2, no change
    conn = models.get_conn(db_path)
    try:
        assert schedule_versions.rows_changed_since(conn, hid, 1) is None
        assert schedule_versions.rows_changed_since(conn, hid, 2) is None
        assert schedule_versions.rows_changed_since(conn, hid, None) == 2
        assert schedule_versions.rows_changed_since(conn, hid, 9) == 2
    finally:
        conn.close()
    schedule_versions.append(rid, hid, "swap", CSV.replace("Bob", "Cy"), db_path=db_path)  # v3, a real change
    conn = models.get_conn(db_path)
    try:
        assert schedule_versions.rows_changed_since(conn, hid, 2) == 3
        assert schedule_versions.rows_changed_since(conn, hid, 3) is None
    finally:
        conn.close()


def test_generation_payload_carries_the_version_it_saved(db_path, monkeypatch):
    """The generation result is the first payload that hands rows to a
    client: it carries the version its rows were stored as, the same number
    the history detail answers with — run through the real job with the
    model stubbed (the tests/test_schedule_a2_generation.py harness)."""
    import schedule_engine as se
    rid, _hid, c, h = _setup(db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [], raising=False)
    for name in ("Max", "Ana"):
        models.add_manual_team_member(rid, name, role="General Manager" if name == "Max" else "Server",
                                      db_path=db_path)
    week = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25"]
    base = {"ok": True, "schedule_csv": HEADER + "2026-10-19,Monday,Max,General Manager,10:00am,6:00pm,8,\n"
                                                 "2026-10-19,Monday,Ana,Server,4:00pm,10:00pm,6,\n",
            "week_dates": week, "week_days": [date.fromisoformat(d).strftime("%A") for d in week], "summary": [],
            "hours_budget": 0, "daily_target_hours": {}, "labor_target": 30, "blended_rate": 20.0,
            "roster_roles": {"Max": "General Manager", "Ana": "Server"}}
    monkeypatch.setattr(se, "_build_schedule_result", lambda r, week_start=None: dict(base))
    done = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda job_id, status, result: done.update(status=status, result=result))
    se._run_schedule_job("ui-version-job", rid)
    assert done.get("status") == "done", done
    res = done["result"]
    newest = schedule_versions.list_versions(rid, res["history_id"], db_path=db_path)[-1]["version"]
    assert res["version"] == newest == 1
    d = c.get(f"/mobile/api/labor/schedule-history/{res['history_id']}", headers=h).get_json()
    assert d["version"] == res["version"]
    # Saving the generated rows against that version goes straight through.
    s = _save(c, h, res["history_id"], res["preview_rows"], res["version"])
    assert s.status_code == 200 and s.get_json()["version"] == 2


# ── UI-3: a replaced copy is read-only ─────────────────────────────────────

def test_a_replaced_draft_cannot_be_saved_or_sent(db_path):
    rid, old, c, h = _setup(db_path)
    new = models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 12, 40, 28, CSV.replace("Bob", "Zed"), [],
                                       db_path=db_path)
    s = _save(c, h, old, _rows("Cy"), 1)
    assert s.status_code == 409 and s.get_json()["replaced"] and s.get_json()["superseded_by"] == new
    assert "Cy" not in _stored(db_path, old)
    p = c.post("/mobile/api/labor/publish-schedule", headers=h, json={"schedule_id": old, "acknowledge": True})
    assert p.status_code == 409 and p.get_json()["replaced"]
    conn = models.get_conn(db_path)
    try:
        assert conn.execute("SELECT published_at FROM schedule_history WHERE id=?", (old,)).fetchone()[0] is None
    finally:
        conn.close()
    # The newer draft is the one that sends, and the week is live.
    assert c.post("/mobile/api/labor/publish-schedule", headers=h,
                  json={"schedule_id": new, "acknowledge": True}).get_json()["ok"]
    conn = models.get_conn(db_path)
    try:
        assert models._live_week_row(conn, rid, "2026-10-12")["id"] == new
    finally:
        conn.close()


def test_the_shared_publish_body_refuses_a_replaced_copy_for_every_caller(db_path):
    """The delayed run of a Send queued before the newer draft calls
    _publish_schedule directly — it refuses too."""
    rid, old, c, h = _setup(db_path)
    models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 12, 40, 28, CSV, [], db_path=db_path)
    out, status = client_api._publish_schedule(rid, old, {"role": "automation"}, acknowledge=True)
    assert status == 409 and out["replaced"]


def test_changes_to_a_retired_published_copy_are_not_sent(db_path):
    rid, a, c, h = _setup(db_path)
    c.post("/mobile/api/labor/publish-schedule", headers=h, json={"schedule_id": a, "acknowledge": True})
    b = models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 12, 40, 28, CSV, [], db_path=db_path)
    schedule_versions.append(rid, b, "generated", CSV, db_path=db_path)
    c.post("/mobile/api/labor/publish-schedule", headers=h, json={"schedule_id": b, "acknowledge": True})
    out, status = client_api.send_schedule_changes(rid, a, {"username": "will"}, acknowledge=True)
    assert status == 409 and out["replaced"] and "went to staff" in out["error"]


def test_detail_and_publish_check_say_a_draft_was_replaced(db_path):
    rid, old, c, h = _setup(db_path)
    new = models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 12, 40, 28, CSV, [], db_path=db_path)
    d = c.get(f"/mobile/api/labor/schedule-history/{old}", headers=h).get_json()
    assert d["superseded_by"] == new and "newer draft" in d["replaced_reason"]
    fresh = c.get(f"/mobile/api/labor/schedule-history/{new}", headers=h).get_json()
    assert fresh["replaced_reason"] is None
    pc = c.get(f"/mobile/api/labor/publish-check?schedule_id={old}", headers=h).get_json()
    assert pc["superseded_by"] == new and pc["replaced_reason"]


# ── UI-2 / UI-9: rules saves are partial and all-or-nothing ────────────────

OWNER_SET = {"rules": {"weekly_hours_ceiling": 35, "notice_days": 14, "daily_ot_hours": 8, "min_shift_hours": 4},
             "role_floors": {"Server": {"morning": 2, "night": 4}, "Cook": {"morning": 1, "night": 2}},
             "jurisdiction": "CA", "role_arrivals": {"Cook": 15}, "foh_roles": ["Server", "Bartender"],
             "patio_roles": ["Server"], "role_cross_training": {"Server": 50},
             "role_requirements": {"Bartender": ["bartender"]}, "cut_floor_default": 3}


def _rules(c, h):
    return c.get("/mobile/api/labor/rules", headers=h).get_json()


def test_a_save_that_sends_only_some_rules_keeps_every_other(db_path):
    rid, hid, c, h = _setup(db_path)
    assert c.post("/mobile/api/labor/rules", headers=h, json=OWNER_SET).get_json()["ok"]
    before = _rules(c, h)
    # The body an older sheet sends after a failed load: empty maps, one rule.
    r = c.post("/mobile/api/labor/rules", headers=h,
               json={"rules": {"keyholder_until_close": True}, "role_floors": {}, "role_arrivals": {},
                     "role_requirements": {}, "role_cross_training": {}})
    assert r.get_json()["ok"]
    after = _rules(c, h)
    for k in ("weekly_hours_ceiling", "notice_days", "daily_ot_hours", "min_shift_hours"):
        assert after["rules"][k] == before["rules"][k], k
    for k in ("role_floors", "role_arrivals", "role_requirements", "role_cross_training", "jurisdiction",
              "foh_roles", "patio_roles", "cut_floor_default"):
        assert after[k] == before[k], k


def test_one_role_changes_alone_and_a_role_sent_null_is_removed(db_path):
    rid, hid, c, h = _setup(db_path)
    c.post("/mobile/api/labor/rules", headers=h, json=OWNER_SET)
    r = c.post("/mobile/api/labor/rules", headers=h,
               json={"role_floors": {"server": {"morning": 3, "night": 4}, "Cook": None},
                     "role_arrivals": {"Cook": None, "Server": 10},
                     "role_cross_training": {"Server": None}, "role_requirements": {"Bartender": []}})
    assert r.get_json()["ok"]
    after = _rules(c, h)
    assert set(after["role_floors"]) == {"server"} and after["role_floors"]["server"]["morning"] == 3
    assert after["role_arrivals"] == {"Server": 10}
    assert after["role_cross_training"] == {} and after["role_requirements"] == {}


def test_rules_default_puts_a_rule_back_to_its_default(db_path):
    rid, hid, c, h = _setup(db_path)
    c.post("/mobile/api/labor/rules", headers=h, json={"rules": {"weekly_hours_ceiling": 35, "notice_days": 14}})
    r = c.post("/mobile/api/labor/rules", headers=h, json={"rules": {}, "rules_default": ["weekly_hours_ceiling"]})
    assert r.get_json()["ok"]
    after = _rules(c, h)["rules"]
    assert after["weekly_hours_ceiling"] == sr.DEFAULTS["weekly_hours_ceiling"]
    assert after["notice_days"] == 14


def test_the_rules_keep_the_closures_and_the_closures_keep_the_rules(db_path):
    rid, hid, c, h = _setup(db_path)
    c.post("/mobile/api/labor/rules", headers=h, json={"rules": {"weekly_hours_ceiling": 35},
                                                       "closed_weekdays": ["Monday"]})
    c.post("/mobile/api/labor/rules", headers=h, json={"rules": {"notice_days": 7}})
    d = _rules(c, h)
    assert d["closures"]["closed_weekdays"] == ["Monday"] and d["rules"]["weekly_hours_ceiling"] == 35
    c.post("/mobile/api/labor/rules", headers=h, json={"closed_weekdays": ["Tuesday"]})
    d = _rules(c, h)
    assert d["closures"]["closed_weekdays"] == ["Tuesday"] and d["rules"]["notice_days"] == 7


@pytest.mark.parametrize("bad", [{"section_count": 99}, {"salaried_cap": 10},
                                 {"role_cross_training": {"Server": "lots"}}, {"jurisdiction": "ZZ"},
                                 {"closed_weekdays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                                                      "Saturday", "Sunday"]},
                                 {"closer_roles": "Server"}, {"cut_floor_default": 0},
                                 {"reservation_provider": "nope"}])
def test_a_refused_rules_save_stores_nothing(db_path, bad):
    """UI-9: the auditor's repro and every other refusal the route has."""
    rid, hid, c, h = _setup(db_path)
    c.post("/mobile/api/labor/rules", headers=h, json=OWNER_SET)
    before = _rules(c, h)
    body = {"rules": {"weekly_hours_ceiling": 30}, "role_floors": {"Server": {"morning": 3}},
            "station_edit": None, "foh_roles": ["Host"], **bad}
    r = c.post("/mobile/api/labor/rules", headers=h, json=body)
    assert r.status_code == 400, r.get_json()
    after = _rules(c, h)
    for k in ("rules", "role_floors", "foh_roles", "closures", "role_cross_training", "jurisdiction"):
        assert after[k] == before[k], k


def test_merged_compliance_is_pure_and_keeps_what_it_is_not_told():
    stored = {"weekly_hours_ceiling": 35.0, "closed_weekdays": ["Monday"], "keyholder_until_close": False}
    out = sr.merged_compliance(stored, {"notice_days": 7}, reset=["keyholder_until_close", "not_a_rule"])
    assert out == {"weekly_hours_ceiling": 35.0, "closed_weekdays": ["Monday"], "notice_days": 7.0}
    assert stored == {"weekly_hours_ceiling": 35.0, "closed_weekdays": ["Monday"], "keyholder_until_close": False}


# ── UI-10: a decided part-day request keeps its label ──────────────────────

def test_deciding_a_part_day_request_returns_its_label(db_path):
    import time_off
    rid, hid, c, h = _setup(db_path)
    day = (date.today() + timedelta(days=10)).isoformat()
    row, err = time_off.request_time_off(rid, "Ana", day, day, end_time="4:00pm", db_path=db_path)
    assert not err
    listed = {x["id"]: x for x in time_off.recent(rid, db_path=db_path)}[row["id"]]
    r = c.post(f"/mobile/api/labor/time-off/{row['id']}/decide", headers=h, json={"decision": "approve"})
    out = r.get_json()
    assert out["ok"] and out["request"]["span_label"] == listed["span_label"]
    assert "until 4:00pm" in out["request"]["span_label"]


# ── The web client: the wiring each fix needs (dashboard.html) ─────────────

def _dash():
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "templates" / "dashboard.html").read_text()


def _fn(src, name):
    i = src.index("function " + name + "(")
    j = src.find("\nfunction ", i + 10)
    return src[i:j if j > 0 else i + 6000]


def test_web_keeps_the_version_that_came_with_its_rows():
    src = _dash()
    # Never fetched after the rows drew (the race UI-1 names).
    assert "_schedLoadVersion" not in src
    hr = _fn(src, "_schedHandleResult")
    assert "_schedVersion = (data.version === undefined || data.version === null) ? null : data.version;" in hr
    assert "version: d.version, superseded_by: d.superseded_by" in src            # openScheduleDraft
    rs = _fn(src, "rescoreSchedule")
    assert "_schedVersion = d.version" in rs and "body.version = _schedVersion" in rs
    assert "_schedVersion = (d.version === undefined || d.version === null) ? null : d.version;" in \
        _fn(src, "reloadScheduleFromHistory")
    assert "_schedVersion = d.version" in _fn(src, "publishScheduleNow")         # UI-4: the Send's version


def test_web_conflict_offers_keep_or_reload_and_never_discards():
    src = _dash()
    sc = _fn(src, "_schedShowConflict")
    assert "schedKeepMine(this)" in sc and "reloadScheduleFromHistory(this, true)" in sc
    km = _fn(src, "schedKeepMine")
    assert "_schedVersion = window._schedConflictAt" in km and "rescoreSchedule(btn)" in km
    rl = _fn(src, "reloadScheduleFromHistory")
    assert "window.confirm(" in rl and "_schedDirty" in rl


def test_web_replaced_copy_is_read_only():
    src = _dash()
    assert 'id="sched-replaced"' in src and ".sched-ro #sched-edit-bar" in src
    for name in ("rescoreSchedule", "saveAndSendSchedule", "publishScheduleNow", "schedEditRow", "schedRemoveRow",
                 "schedAddRow", "swEdit", "swAddShift", "applyShiftSwap"):
        assert "_schedReadOnly()" in _fn(src, name)[:400], name
    assert "if (_schedReplaced)" in _fn(src, "schedAfterEdit")
    assert "d.replaced" in _fn(src, "rescoreSchedule") and "d.replaced" in _fn(src, "publishScheduleNow")
    assert "c.replaced_reason" in src                                            # psLabel reads publish-check


def test_web_rules_save_is_partial():
    src = _dash()
    i = src.index("window.saveRules=function")
    body = src[i:src.index("jsend('/api/labor/rules',body", i)]
    assert "rules_default:reset" in body and "reset.push(rk)" in body
    assert "floors[role]=(spec.morning!==undefined||spec.night!==undefined||anyDay)?spec:null" in body
    assert "an===''?null" in body and "cn===''?null" in body and "picked.length?picked:null" in body
