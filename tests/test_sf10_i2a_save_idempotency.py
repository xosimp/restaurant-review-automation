"""A retried schedule save lands once (schedule audit 10/10/26 BUGSLIFE-22).

The phone sends a save (`/mobile/api/labor/schedule/score`, `save: true`)
with `retryTransient: true`: one silent retry when the request dies in
transit. When the server had already committed the first, the retry ran the
whole save again — with the same rows it wrote a second identical "edited"
version and ran the learning capture twice; with an edit, it refused the
owner's own save as somebody else's ("<owner> changed this week after you
opened it"). A save now carries an idempotency key the server honours
(mobile_api._idempotent_save on strategy_routes._idempotent): a repeat is
answered from the first save.
"""
import sys

import pytest
from flask import Flask

import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import schedule_economics, schedule_intel, shift_requests, shift_quality, staff_schedule  # noqa: E401,F401
import staff_settings, strategy_jobs, time_off, labor_replacements  # noqa: E401,F401

import auth, client_api, labor, mobile_api, models, push, schedule_learning, schedule_versions, strategy_routes  # noqa: E401
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
    init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    yield


@pytest.fixture
def captured(monkeypatch):
    """Each run of the save's learning capture."""
    calls = []
    real = schedule_learning.capture_save

    def spy(*a, **k):
        calls.append(a[1:3])
        return real(*a, **k)
    monkeypatch.setattr(schedule_learning, "capture_save", spy)
    return calls


def _setup(db_path):
    rid = create_restaurant(Restaurant(name="Retry Co", owner_email="o@retry.test", timezone="America/Chicago",
                                       module_labor=1), db_path=db_path)
    owner = create_user(rid, "will", "o@retry.test", "pw-retry-1234", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    hid = models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 12, 40, 28, CSV, [], db_path=db_path)
    schedule_versions.append(rid, hid, "generated", CSV, db_path=db_path)
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    tok = create_session(owner, device_type="ios", db_path=db_path)
    return rid, hid, app.test_client(), {"Authorization": "Bearer " + tok}


def _rows(tuesday="Bob"):
    return [{"date": "2026-10-12", "day": "Monday", "employee": "Ana", "role": "Server",
             "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""},
            {"date": "2026-10-13", "day": "Tuesday", "employee": tuesday, "role": "Server",
             "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}]


def _save(c, h, hid, rows, version, key=None, header=False):
    body = {"rows": rows, "save": True, "history_id": hid, "version": version}
    headers = dict(h)
    if key and header:
        headers["Idempotency-Key"] = key
    elif key:
        body["idempotency_key"] = key
    return c.post("/mobile/api/labor/schedule/score", headers=headers, json=body)


def _edited(rid, hid, db_path):
    return [v for v in schedule_versions.list_versions(rid, hid, db_path=db_path) if v.get("reason") == "edited"]


# ── the bug, as it was ─────────────────────────────────────────────────────

def test_without_a_key_a_replayed_save_is_the_week_already_and_writes_nothing(db_path, captured):
    """What the phone's transient retry met after a committed save — a 409
    "will changed this week" from its own save. A client that sends no key
    (the web, an older app) is covered too since D1 (10/10/26): rows that
    are the week already write nothing and are no conflict."""
    rid, hid, c, h = _setup(db_path)
    first = _save(c, h, hid, _rows("Cy"), 1)
    assert first.status_code == 200 and first.get_json()["version"] == 2
    again = _save(c, h, hid, _rows("Cy"), 1)
    assert again.status_code == 200 and again.get_json()["unchanged"] is True and again.get_json()["version"] == 2
    assert len(_edited(rid, hid, db_path)) == 1 and len(captured) == 1


def test_without_a_key_a_replayed_unchanged_save_writes_no_version_and_learns_nothing(db_path, captured):
    rid, hid, c, h = _setup(db_path)
    assert _save(c, h, hid, _rows(), 1).status_code == 200
    assert _save(c, h, hid, _rows(), 1).status_code == 200
    assert len(_edited(rid, hid, db_path)) == 0 and len(captured) == 0


# ── the fix ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("header", [False, True])
def test_a_replayed_save_with_its_key_is_answered_from_the_first(db_path, captured, header):
    rid, hid, c, h = _setup(db_path)
    first = _save(c, h, hid, _rows("Cy"), 1, key="save-abc", header=header)
    assert first.status_code == 200
    again = _save(c, h, hid, _rows("Cy"), 1, key="save-abc", header=header)
    assert again.status_code == 200, again.get_json()
    one, two = first.get_json(), again.get_json()
    assert two["saved"] is True and two["version"] == one["version"] == 2
    assert two == one
    assert len(_edited(rid, hid, db_path)) == 1 and len(captured) == 1


def test_an_unchanged_save_replayed_with_its_key_writes_no_second_version(db_path, captured):
    # Rows that are the week already write no version at all (D1).
    rid, hid, c, h = _setup(db_path)
    assert _save(c, h, hid, _rows(), 1, key="same-rows").status_code == 200
    assert _save(c, h, hid, _rows(), 1, key="same-rows").status_code == 200
    assert len(_edited(rid, hid, db_path)) == 0 and len(captured) == 0


def test_a_replay_while_the_first_is_still_being_made_is_told_so_and_writes_nothing(db_path, captured):
    rid, hid, c, h = _setup(db_path)
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO idempotent_responses (restaurant_id, route, idem_key) VALUES (?,?,?)",
                 (rid, f"schedule_save:{hid}", "in-flight"))
    conn.commit()
    conn.close()
    r = _save(c, h, hid, _rows("Cy"), 1, key="in-flight")
    assert r.status_code == 409 and r.get_json()["in_progress"] is True
    assert "still being made" in r.get_json()["error"]
    assert _edited(rid, hid, db_path) == [] and captured == []


def test_a_first_that_never_answered_stops_holding_its_key_after_the_save_window(db_path, captured):
    """A process that died mid-save leaves its key; past the save's own
    window (2 minutes, not a model scan's 10) the next Save runs."""
    rid, hid, c, h = _setup(db_path)
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO idempotent_responses (restaurant_id, route, idem_key, created_at) "
                 "VALUES (?,?,?, datetime('now', ?))",
                 (rid, f"schedule_save:{hid}", "orphan", f"-{mobile_api.SAVE_PENDING_MINUTES + 1} minutes"))
    conn.commit()
    conn.close()
    r = _save(c, h, hid, _rows("Cy"), 1, key="orphan")
    assert r.status_code == 200 and r.get_json()["saved"]
    assert len(_edited(rid, hid, db_path)) == 1


def test_a_failed_first_save_is_not_remembered_so_the_retry_saves(db_path, captured, monkeypatch):
    rid, hid, c, h = _setup(db_path)
    real = schedule_versions.write_on
    boom = {"n": 0}

    def flaky(*a, **k):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("disk full")
        return real(*a, **k)
    monkeypatch.setattr(schedule_versions, "write_on", flaky)
    first = _save(c, h, hid, _rows("Cy"), 1, key="retry-me")
    assert first.status_code == 500
    again = _save(c, h, hid, _rows("Cy"), 1, key="retry-me")
    assert again.status_code == 200 and again.get_json()["saved"] and again.get_json()["version"] == 2
    assert len(_edited(rid, hid, db_path)) == 1


def test_a_new_save_with_a_new_key_is_a_new_save(db_path, captured):
    rid, hid, c, h = _setup(db_path)
    v2 = _save(c, h, hid, _rows("Cy"), 1, key="k1").get_json()["version"]
    v3 = _save(c, h, hid, _rows("Dee"), v2, key="k2").get_json()
    assert v3["saved"] and v3["version"] == 3
    assert len(_edited(rid, hid, db_path)) == 2 and len(captured) == 2


def test_a_key_is_the_weeks_alone_and_a_rescore_never_stores_one(db_path, captured):
    rid, hid, c, h = _setup(db_path)
    hid2 = models.save_schedule_history(rid, "2026-10-19", "2026-10-25", 12, 40, 28,
                                        CSV.replace("2026-10-12", "2026-10-19").replace("2026-10-13", "2026-10-20"),
                                        [], db_path=db_path)
    schedule_versions.append(rid, hid2, "generated", CSV, db_path=db_path)
    assert _save(c, h, hid, _rows("Cy"), 1, key="shared").status_code == 200
    rows2 = [dict(r, date=r["date"].replace("-10-12", "-10-19").replace("-10-13", "-10-20")) for r in _rows("Cy")]
    assert _save(c, h, hid2, rows2, 1, key="shared").get_json()["history_id"] == hid2
    r = c.post("/mobile/api/labor/schedule/score", headers=h,
               json={"rows": _rows("Zed"), "history_id": hid, "idempotency_key": "rescore"})
    assert r.status_code == 200 and r.get_json()["saved"] is False
    conn = models.get_conn(db_path)
    try:
        keys = {row[0] for row in conn.execute("SELECT idem_key FROM idempotent_responses")}
    finally:
        conn.close()
    assert "rescore" not in keys


def test_the_web_twin_runs_the_same_save(db_path, captured, monkeypatch):
    """/api/labor/schedule/score reaches the same body through _m, which
    unwraps the login check — the key is honoured there too."""
    rid, hid, c, h = _setup(db_path)
    user = {"id": 1, "restaurant_id": rid, "username": "will", "role": "client", "is_admin": 1}
    app = Flask(__name__)
    body = {"rows": _rows("Cy"), "save": True, "history_id": hid, "version": 1, "idempotency_key": "web-1"}
    with app.test_request_context("/api/labor/schedule/score", method="POST", json=body):
        first = client_api._m("mobile_score_schedule")(user)
    with app.test_request_context("/api/labor/schedule/score", method="POST", json=body):
        again = client_api._m("mobile_score_schedule")(user)
    assert first[1] == again[1] == 200
    assert first[0].get_json() == again[0].get_json()
    assert len(_edited(rid, hid, db_path)) == 1


# ── the phone sends its key ────────────────────────────────────────────────

def test_the_phone_sends_one_key_per_distinct_save():
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "ios", "CavnarAI", "CavnarAI", "Features", "Labor", "LaborViewModel.swift"),
               encoding="utf-8").read()
    body = src[src.index("struct ScoreBody: Encodable {"):]
    body = body[:body.index("\n    }\n")]
    assert 'case idempotencyKey = "idempotency_key"' in body
    rescore = src[src.index("private func performRescore(save: Bool) async {"):]
    rescore = rescore[:rescore.index("\n    // MARK: - Operational Score")]
    assert "Self.saveKey(body)" in rescore and "sendScore(body)" in rescore
    send = src[src.index("private func sendScore("):]
    send = send[:send.index("\n    }\n")]
    assert "Self.saveInProgress(error)" in send and "retryTransient: true" in send
