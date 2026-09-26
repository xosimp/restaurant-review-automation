"""Labor, scheduling and staff: the phone reads what the web reads
(web-vs-iOS parity audit, 9/25/26).

- The mobile /labor payload carries labor.staffing_board and
  labor.money_went, the same objects the web Labor tab renders, gated the
  same way (live shifts only).
- The send sheet's staff list is the week being sent, not the newest week
  on file, and "reachable" counts every channel Send uses.
- /api/generate-schedule and /mobile/api/labor/generate-schedule are one
  body: a rate-limited press answers 429 on both.
"""
import json
import sys
from datetime import date, timedelta

import pytest
from flask import Flask

import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import ai_utils
import auth
import client_api
import labor
import mobile_api
import models
import strategy_routes
from models import create_restaurant, Restaurant


@pytest.fixture
def db(db_path, monkeypatch):
    """Every module that bound models.get_conn at import reads the
    throwaway database, found by identity rather than by a hand list."""
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    return db_path


def _app():
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(strategy_routes.strategy_bp)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    return app


def _restaurant(db, **cols):
    rid = create_restaurant(Restaurant(name="Parity Grill", owner_email="p@x.test"), db_path=db)
    if cols:
        conn = models.get_conn(db)
        conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?",
                     (*cols.values(), rid))
        conn.commit()
        conn.close()
    return rid


def _bearer(db, rid, name="owner", role="client"):
    uid = auth.create_user(rid, name, f"{name}@x.test", "pw", db_path=db)
    conn = models.get_conn(db)
    conn.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    conn.commit()
    conn.close()
    return {"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"}


def _web_login(monkeypatch, rid):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 7, "restaurant_id": rid, "is_admin": 0, "role": "client",
                                 "username": "owner", "email": "o@x.test"})


def _live_shifts():
    """Fourteen days: heavy Mondays on thin sales (overstaffed), lean
    Saturdays on big sales, one server past 40 every week and a second
    server with room to take the hours."""
    rows = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes"]
    today = date.today()
    for back in range(14, 0, -1):
        d = today - timedelta(days=back)
        wd = d.strftime("%A")
        sales = {"Monday": 1500, "Saturday": 9000}.get(wd, 4000)
        crew = {"Monday": 6, "Saturday": 2}.get(wd, 3)
        rows.append(f"{d.isoformat()},{wd},Olive,Server,10:00,20:00,10,10,{sales},")
        rows.append(f"{d.isoformat()},{wd},Pat,Server,16:00,20:00,4,4,{sales},")
        for e in range(crew):
            rows.append(f"{d.isoformat()},{wd},Cook{e},Cook,11:00,19:00,8,8,{sales},")
    return "\n".join(rows) + "\n"


def test_mobile_labor_carries_the_web_staffing_board_and_money_went(db):
    rid = _restaurant(db, module_labor=1, hourly_rate=18, labor_target_pct=28)
    models.save_client_data(rid, "shifts", _live_shifts(), source="upload", db_path=db)
    # What the web Labor tab renders (hosted_dashboard -> dashboard.html).
    web = labor.analyse_shifts_for_restaurant(rid)
    assert web["is_live"] and web["staffing_board"]

    body = _app().test_client().get("/mobile/api/labor", headers=_bearer(db, rid)).get_json()
    assert body["ok"] is True
    board = body["staffing_board"]
    assert board == json.loads(json.dumps(web["staffing_board"]))
    assert body["money_went"] == json.loads(json.dumps(web["money_went"]))
    assert body["blended_rate"] == pytest.approx(web["blended_rate"])
    # The board the phone draws is the web's: the three lanes and the strip.
    assert set(board) >= {"overstaffed", "lean", "overtime", "summary"}
    assert board["overtime"], "the fixture's 70-hour server is overtime"
    assert board["overstaffed"], "the fixture's heavy Mondays run over target"
    # Overtime lane: people past 40 only, never "near" rows.
    names = {x["title"] for x in board["overtime"]}
    assert names == {e["employee"] for e in web["overtime_risk"] if e.get("status") == "overtime"}
    card = board["overtime"][0]
    for key in ("severity", "dollars_text", "say", "ask", "extra_text", "hours_text"):
        assert card.get(key) not in (None, ""), key


def test_sample_data_sends_no_board_to_the_phone(db):
    """The web draws no staffing cards on the sample week; neither does iOS."""
    rid = _restaurant(db, module_labor=1)
    body = _app().test_client().get("/mobile/api/labor", headers=_bearer(db, rid)).get_json()
    assert body["ok"] is True and body["is_live"] is False
    assert body["staffing_board"] is None
    assert body["money_went"] == []


def _history(db, rid, week_start, names):
    csv = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + "".join(
        f"{week_start},Monday,{n},Server,4:00pm,10:00pm,6,\n" for n in names)
    return models.save_schedule_history(rid, week_start, week_start, 6.0 * len(names), 40.0, 28.0,
                                        csv, [], db_path=db)


def test_the_send_sheet_lists_the_week_being_sent(db):
    rid = _restaurant(db, module_labor=1)
    older = _history(db, rid, "2026-10-05", ["Ana"])
    _history(db, rid, "2026-10-12", ["Bob", "Cy"])
    models.set_staff_contact(rid, "Ana", "ana@staff.test", None, db_path=db)
    client = _app().test_client()
    h = _bearer(db, rid)

    sent = client.get(f"/mobile/api/labor/staff-contacts?schedule_id={older}", headers=h).get_json()
    assert sent["schedule_id"] == older
    assert [c["employee_name"] for c in sent["contacts"]] == ["Ana"]
    assert sent["contacts"][0]["channel"] == "email"
    assert sent["reachable"] == 1
    assert sent["reach"]["total"] == 1 and sent["reach"]["by_email"] == 1

    # No id: the newest week, as the web contacts manager always read it.
    latest = client.get("/mobile/api/labor/staff-contacts", headers=h).get_json()
    assert [c["employee_name"] for c in latest["contacts"]] == ["Bob", "Cy"]
    assert latest["reachable"] == 0
    assert all(c["channel"] is None for c in latest["contacts"])

    # Another restaurant's week is not found, not read.
    other = _restaurant(db)
    theirs = _history(db, other, "2026-10-05", ["Zed"])
    assert client.get(f"/mobile/api/labor/staff-contacts?schedule_id={theirs}", headers=h).status_code == 404


def test_publish_check_answers_on_the_mobile_path(db):
    """iOS reads blockers and reach before Send, from the same body."""
    rid = _restaurant(db, module_labor=1)
    sid = _history(db, rid, (date.today() + timedelta(days=7)).isoformat(), ["Ana"])
    body = _app().test_client().get(f"/mobile/api/labor/publish-check?schedule_id={sid}",
                                    headers=_bearer(db, rid)).get_json()
    assert body["ok"] is True and body["schedule_id"] == sid
    assert body["reach"]["total"] == 1
    assert "blockers" in body and "blocker_keys" in body and "can_publish" in body


def test_generate_schedule_rate_limit_is_429_on_web_and_mobile(db, monkeypatch):
    rid = _restaurant(db, module_labor=1)
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: True)
    client = _app().test_client()
    mobile = client.post("/mobile/api/labor/generate-schedule", headers=_bearer(db, rid), json={})
    assert mobile.status_code == 429
    assert "wait a moment" in mobile.get_json()["error"]
    _web_login(monkeypatch, rid)
    web = client.post("/api/generate-schedule", json={})
    assert web.status_code == 429
    assert web.get_json()["error"] == mobile.get_json()["error"]


def test_web_generate_schedule_still_reads_week_start_from_the_query(db, monkeypatch):
    rid = _restaurant(db, module_labor=1)
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: False)
    _web_login(monkeypatch, rid)
    r = _app().test_client().post("/api/generate-schedule?week_start=next-ish", json={})
    assert r.status_code == 400


def test_web_generate_schedule_is_post_only(db, monkeypatch):
    """A GET is guarded by neither CSRF nor view-as-read-only, and this
    starts a paid model job — so it is POST only, like the phone's twin."""
    rid = _restaurant(db, module_labor=1)
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: False)
    _web_login(monkeypatch, rid)
    assert _app().test_client().get("/api/generate-schedule").status_code == 405


def test_the_web_generate_route_is_the_mobile_body():
    import inspect
    src = inspect.getsource(client_api.generate_schedule_json)
    assert '_m("mobile_generate_schedule")' in src
    assert "ai_rate_limited" not in src


def test_the_web_generate_press_reads_a_429_answer_for_its_sentence():
    """The web Labor tab reads the generate answer through apiJson, which
    keeps a non-2xx body's own `error`, and shows that sentence — so the
    429 now shared with the phone reads "Too many schedule generations…",
    not a generic HTTP failure."""
    import os
    src = open(os.path.join(os.path.dirname(__file__), "..", "templates", "dashboard.html"), encoding="utf-8").read()
    at = src.index("fetch('/api/generate-schedule'")
    block = src[at:at + 600]
    assert ".then(apiJson)" in block
    assert "startData.error" in block
    helper = src[src.index("function apiJson(r){"):src.index("function loadFailed(")]
    assert "if (!d.error)" in helper   # a server sentence is kept, never overwritten
