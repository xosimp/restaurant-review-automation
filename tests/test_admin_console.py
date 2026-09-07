"""The admin console's data layer (admin_ops) and its routes: every page
answers for an admin, nothing answers for a client, and the health record
rolls problems up rather than averaging them away."""
import pytest
from flask import Flask

import admin_routes
import auth
import client_api
import mobile_api
import models
from admin_routes import admin_bp
from auth_routes import auth_bp
from auth import create_user, init_auth
from models import create_restaurant, get_conn, Restaurant


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, admin_routes, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect)
    import admin_ops
    monkeypatch.setattr(admin_ops, "get_conn", redirect)
    init_auth(db_path=db_path)
    from models import init_two_fa_backup_codes, init_email_log
    init_two_fa_backup_codes(db_path=db_path)
    init_email_log(db_path=db_path)
    import ai_utils
    conn = get_conn(db_path)
    conn.executescript(ai_utils._USAGE_TABLE_SQL)
    conn.commit(); conn.close()


@pytest.fixture
def client():
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_bp)
    app.register_blueprint(auth_bp)
    return app.test_client()


def _as(monkeypatch, is_admin, rid=None):
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": rid, "is_admin": int(is_admin), "username": "will" if is_admin else "client", "email": "x@x.com", "role": "client"})


def _seed(db_path):
    rid = create_restaurant(Restaurant(name="Corner Bar", owner_email="o@x.com", module_reviews=1, module_labor=1,
                                       module_inventory=1, module_marketing=1, location_group="Corner Bar Group",
                                       location_name="Downtown"), db_path=db_path)
    rid2 = create_restaurant(Restaurant(name="Corner Bar", owner_email="o@x.com", module_reviews=1,
                                        location_group="Corner Bar Group", location_name="Uptown"), db_path=db_path)
    create_user(rid, "cornerbar", "o@x.com", "corner-pass-1", db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE restaurants SET toast_restaurant_guid='g', toast_client_id='c', toast_sync_error='401 from Toast' WHERE id=?", (rid2,))
    conn.execute("INSERT INTO email_log (restaurant_id, email_type, to_email, subject, status, error) VALUES (?, 'digest', 'o@x.com', 'Weekly', 'failed', 'bounced')", (rid,))
    conn.execute("INSERT INTO ai_usage (restaurant_id, action, model, input_tokens, output_tokens, cost_usd) VALUES (?, 'draft_response', 'claude-sonnet-5', 100, 50, 0.01)", (rid,))
    conn.commit(); conn.close()
    return rid, rid2


def test_every_console_page_answers_for_an_admin(client, db_path, monkeypatch):
    rid, _ = _seed(db_path)
    _as(monkeypatch, True)
    for path in ["/admin/api/overview", "/admin/api/clients", f"/admin/api/client/{rid}", "/admin/api/integrations",
                 "/admin/api/ai?days=7", "/admin/api/emails", "/admin/api/notifications", "/admin/api/billing",
                 "/admin/api/jobs", "/admin/api/issues", "/admin/api/activity", "/admin/api/search?q=corner", "/admin/api/system"]:
        r = client.get(path)
        assert r.status_code == 200, (path, r.data[:200])
        assert r.get_json()["ok"] is True, path


def test_nothing_answers_for_a_client_login(client, db_path, monkeypatch):
    rid, _ = _seed(db_path)
    _as(monkeypatch, False, rid)
    for path in ["/admin/api/overview", f"/admin/api/client/{rid}", "/admin/api/billing", "/admin/api/search?q=corner"]:
        r = client.get(path, headers={"Accept": "application/json"})
        assert r.status_code in (401, 302, 403), path
        assert b'"ok": true' not in r.data.lower()


def test_brand_health_is_the_worst_location_not_an_average(client, db_path, monkeypatch):
    rid, rid2 = _seed(db_path)
    _as(monkeypatch, True)
    d = client.get("/admin/api/overview").get_json()
    brand = next(b for b in d["brands"] if b["brand"] == "Corner Bar Group")
    assert brand["count"] == 2
    loc2 = client.get(f"/admin/api/client/{rid2}").get_json()["client"]
    assert loc2["health"] == "critical"                     # Toast is erroring
    assert any(i["key"] == f"{rid2}:int:toast" for i in loc2["issues"])
    assert brand["health"] == "critical"                    # the brand inherits it
    assert any(i["title"] == "Toast POS needs attention" and i["restaurant_id"] == rid2 for i in d["issues"])


def test_issues_can_be_resolved_and_reopened(client, db_path, monkeypatch):
    rid, rid2 = _seed(db_path)
    _as(monkeypatch, True)
    key = f"{rid2}:int:toast"
    assert client.post("/admin/api/issues/resolve", json={"key": key, "note": "fixed creds"}).get_json()["ok"]
    d = client.get("/admin/api/issues").get_json()
    assert not any(i["key"] == key for i in d["issues"])
    assert any(r["key"] == key and r["note"] == "fixed creds" for r in d["resolved"])
    assert client.post("/admin/api/issues/resolve", json={"key": key, "undo": True}).get_json()["ok"]
    assert any(i["key"] == key for i in client.get("/admin/api/issues").get_json()["issues"])


def test_search_says_what_kind_of_thing_it_found(client, db_path, monkeypatch):
    _seed(db_path)
    _as(monkeypatch, True)
    res = client.get("/admin/api/search?q=corner").get_json()["results"]
    kinds = {r["type"] for r in res}
    assert {"location", "owner", "brand"} <= kinds


def test_job_runs_are_recorded_by_run_job(db_path):
    import ops
    ops.run_job("probe_job", lambda: 42)
    ops.run_job("probe_job_bad", lambda: 1 / 0)
    conn = get_conn(db_path)
    rows = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT job, ok, error FROM job_runs").fetchall()}
    conn.close()
    assert rows["probe_job"][0] == 1 and rows["probe_job_bad"][0] == 0 and "division" in rows["probe_job_bad"][1]
