"""Workstream F — the routes: status-page incidents (#61) and the admin
system card (#125), through real admin sessions on a throwaway app."""
import pytest
from flask import Flask

import admin_routes
import auth
import auth_routes
import client_api
import models
import ops
import status_manager
import status_routes
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant

CSRF = "fix-f-csrf"


@pytest.fixture(autouse=True)
def _redirect_db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, admin_routes, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    status_manager.seed_default_services()
    import platform_monitor
    platform_monitor.init_platform_tables(db_path)
    monkeypatch.setattr(ops, "backup_status", None, raising=False)


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.secret_key = "fix-f"
    flask_app.register_blueprint(admin_routes.admin_bp)
    flask_app.register_blueprint(auth_routes.auth_bp)
    flask_app.register_blueprint(status_routes.status_bp)
    return flask_app


def _client(app, token):
    c = app.test_client()
    c.set_cookie("session_token", token)
    c.set_cookie("csrf_js", CSRF)
    return c


def _post(c, url, **kw):
    headers = kw.pop("headers", {})
    headers.setdefault("X-CSRF", CSRF)
    return c.post(url, headers=headers, **kw)


def _admin_client(app, db_path):
    home = create_restaurant(Restaurant(name="Cavnar HQ", owner_email="will@cavnar.test"), db_path=db_path)
    uid = create_user(home, "will", "will@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    return _client(app, create_session(uid, db_path=db_path))


def _owner_client(app, db_path):
    rid = create_restaurant(Restaurant(name="Client Grill", owner_email="o@client.test"), db_path=db_path)
    uid = create_user(rid, "owner", "o@client.test", "Owner-pass-2026", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    return _client(app, create_session(uid, db_path=db_path))


# ── #61: incidents can be listed, updated and resolved ──────────────────────

def test_an_incident_is_posted_listed_updated_and_resolved(app, db_path):
    c = _admin_client(app, db_path)
    r = _post(c, "/admin/status/incident", json={"title": "Email delayed", "body": "Resend is slow",
                                                  "severity": "outage", "affected_keys": ["email"]})
    assert r.status_code == 200
    inc_id = r.get_json()["id"]
    listed = c.get("/admin/status/incidents").get_json()
    assert [i["id"] for i in listed["open"]] == [inc_id]
    assert listed["open"][0]["affected_keys"] == ["email"] and listed["open"][0]["updates"]
    assert "resolved" in listed["statuses"]

    # the public page holds Email at the incident's severity
    pub = app.test_client().get("/api/status").get_json()
    email = [s for s in pub["services"] if s["service_key"] == "email"][0]
    assert email["status"] == "outage" and pub["overall"] == "outage"

    r = _post(c, f"/admin/status/incident/{inc_id}/update", json={"message": "Sending again", "status": "monitoring"})
    assert r.status_code == 200 and r.get_json()["incident"]["status"] == "monitoring"
    r = _post(c, f"/admin/status/incident/{inc_id}/resolve", json={"message": "All clear"})
    body = r.get_json()
    assert r.status_code == 200 and body["incident"]["status"] == "resolved" and body["incident"]["resolved_at"]
    listed = c.get("/admin/status/incidents").get_json()
    assert listed["open"] == [] and listed["resolved"][0]["id"] == inc_id
    assert app.test_client().get("/api/status").get_json()["overall"] == "operational"
    again = _post(c, f"/admin/status/incident/{inc_id}/resolve", json={})
    assert again.get_json()["already_resolved"] is True


def test_incident_writes_are_validated_instead_of_500ing(app, db_path):
    c = _admin_client(app, db_path)
    assert _post(c, "/admin/status/incident", json={"title": "x", "severity": "apocalypse"}).status_code == 400
    assert _post(c, "/admin/status/incident", json={"title": "x", "status": "identified"}).status_code == 400
    assert _post(c, "/admin/status/incident", json={"title": "x", "affected_keys": ["nope"]}).status_code == 400
    assert _post(c, "/admin/status/incident/999/update", json={"message": "hi"}).status_code == 404
    inc = _post(c, "/admin/status/incident", json={"title": "Slow"}).get_json()["id"]
    assert _post(c, f"/admin/status/incident/{inc}/update", json={"message": "hi", "status": "bogus"}).status_code == 400
    assert _post(c, "/admin/status/incident/999/resolve", json={}).status_code == 404
    assert _post(c, "/admin/status/update", json={"service_key": "nope", "status": "outage"}).status_code == 400


def test_incident_routes_are_admin_only(app, db_path):
    owner = _owner_client(app, db_path)
    assert owner.get("/admin/status/incidents").status_code == 403
    assert _post(owner, "/admin/status/incident/1/resolve", json={}).status_code == 403


def test_the_services_list_carries_the_valid_values(app, db_path):
    d = _admin_client(app, db_path).get("/admin/status/services").get_json()
    assert {s["key"] for s in d["services"]} >= {"email", "storage", "scheduler"}
    assert d["incident_statuses"] == ["investigating", "monitoring", "resolved"]
    assert "effective" in d


# ── #125: the system card ───────────────────────────────────────────────────

def test_the_system_endpoint_names_every_required_key_and_the_platform_state(app, db_path, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_KEY", "")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "")
    d = _admin_client(app, db_path).get("/admin/api/system").get_json()
    assert d["ok"] is True
    for label in ("Credential key", "Backup encryption key", "Session secret", "Staff PIN pepper",
                  "DocuSign webhook", "Admin 2FA enforced", "Anthropic (Claude)"):
        assert label in d["services"], label
    assert d["services"]["Credential key"] is False and d["services"]["Backup encryption key"] is False
    assert "Operator SMS" not in d["services"], "optional keys are not counted as missing"
    assert any(k["label"] == "Operator SMS" for k in d["keys"])
    assert any("CREDENTIAL_KEY" in w for w in d["warnings"])
    sysd = d["system"]
    assert sysd["disk"]["state"] in ("ok", "low", "critical", "unknown")
    assert "wal_mb" in sysd["disk"] and sysd["database"]["journal"]
    assert {"lease", "drill", "backup", "ai", "rollups_24h", "server_errors_24h", "boots", "providers"} <= set(sysd)


def test_the_system_endpoint_is_not_for_owners(app, db_path):
    r = _owner_client(app, db_path).get("/admin/api/system", headers={"Accept": "application/json"})
    assert r.status_code in (302, 401, 403)
