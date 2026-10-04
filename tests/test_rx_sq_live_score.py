"""Schedule re-audit 10/4/26 UI-5: the phone's live re-score and what-if
score a week exactly as the save path does. The phone now sends the week's
id (and the hour targets it was generated against); the server scores with
those targets, so the same rows read the same number on every path."""
import json
import sys

import pytest
from flask import Flask

import auth, client_api, labor, mobile_api, models, schedule_versions, push
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
CSV = (HEADER + "2026-10-12,Monday,Ana,Server,4:00pm,10:00pm,6,\n"
       "2026-10-13,Tuesday,Bob,Server,4:00pm,10:00pm,6,\n")
TARGETS = {"2026-10-12": 6, "2026-10-13": 30}


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
    for mod in (models, auth, schedule_versions):
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda *a, **k: {"is_live": True})
    monkeypatch.setattr(labor, "load_shifts_for_restaurant", lambda *a, **k: [])
    init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    yield


def _rows():
    return [{"date": "2026-10-12", "day": "Monday", "employee": "Ana", "role": "Server",
             "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""},
            {"date": "2026-10-13", "day": "Tuesday", "employee": "Bob", "role": "Server",
             "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}]


def test_ui5_live_rescore_with_the_week_id_scores_as_the_save_path(db_path):
    rid = create_restaurant(Restaurant(name="UI Co", owner_email="o@ui.test", timezone="America/Chicago",
                                       module_labor=1), db_path=db_path)
    owner = create_user(rid, "will", "o@ui.test", "pw-ui-1234", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    hid = models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 12, 40, 28, CSV, [], db_path=db_path)
    schedule_versions.append(rid, hid, "generated", CSV, db_path=db_path)
    for n in ("Ana", "Bob"):
        models.add_manual_team_member(rid, n, role="Server", db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE schedule_history SET quality_json=? WHERE id=?",
                 (json.dumps({"score": 70, "daily_target_hours": TARGETS}), hid))
    conn.commit()
    conn.close()
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    c = app.test_client()
    h = {"Authorization": "Bearer " + create_session(owner, device_type="ios", db_path=db_path)}

    def score(body):
        r = c.post("/mobile/api/labor/schedule/score", headers=h, json=dict(body, rows=_rows(), save=False))
        assert r.status_code == 200, r.get_json()
        q = r.get_json()["quality"] or {}
        return q.get("score"), sorted(d["key"] for d in q.get("dimensions") or [])

    # What the phone sends now: the week's id (its targets when it holds
    # them), never empty targets with no id.
    by_id = score({"history_id": hid, "daily_target_hours": {}})
    with_targets = score({"history_id": hid, "daily_target_hours": TARGETS})
    explicit = score({"daily_target_hours": TARGETS})
    assert by_id == with_targets == explicit
    assert "labor_efficiency" in by_id[1]
