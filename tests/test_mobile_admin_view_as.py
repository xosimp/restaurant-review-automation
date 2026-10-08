"""Admin view-as from the app (10/8/26): an internal login signed in on the
iPhone opens the same view-as session the web's /admin/view-as/<id> does —
on the restaurant's owner login, VIEW_AS_HOURS long, read-only for support,
audited both ways — and the phone is never filed as the client's device."""
import pytest
from flask import Flask

import admin_routes
import auth
import auth_routes
import client_api
import mobile_api
import models
from auth import create_session, create_user, get_session_user, init_auth
from models import Restaurant, create_restaurant
from auth import upsert_membership


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, mobile_api, client_api, admin_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    import push
    push.init_push(db_path=db_path)


@pytest.fixture
def client():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    for bp in (auth_routes.auth_bp, mobile_api.mobile_bp, client_api.client_bp):
        app.register_blueprint(bp)
    return app.test_client()


def _setup(db_path):
    rid = create_restaurant(Restaurant(name="Simple EJ's", owner_email="erik@x.test"), db_path=db_path)
    owner = create_user(rid, "erik", "erik@x.test", "Owner-pass-2026", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    bare = create_restaurant(Restaurant(name="No Login Yet", owner_email="n@x.test"), db_path=db_path)
    hq = create_restaurant(Restaurant(name="Cavnar AI HQ", owner_email="w@x.test"), db_path=db_path)
    admin = create_user(hq, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    sup = create_user(hq, "sam", "s@x.test", "Support-pass-2026", role="support", db_path=db_path)
    return rid, owner, bare, admin, sup


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _events(db_path, action):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT event_type, actor_id, restaurant_id, after_json FROM admin_events WHERE event_type=?",
            (action,)).fetchall()]
    finally:
        conn.close()


def test_only_an_internal_login_lists_clients_or_opens_a_view(client, db_path):
    rid, owner, bare, admin, sup = _setup(db_path)
    mine = create_session(owner, device_type="ios", db_path=db_path)
    assert client.get("/mobile/api/admin/clients", headers=_bearer(mine)).status_code == 403
    assert client.post(f"/mobile/api/admin/view-as/{rid}", headers=_bearer(mine)).status_code == 403
    assert client.get("/mobile/api/admin/clients").status_code == 401


def test_the_list_is_the_restaurants_with_an_owner_login(client, db_path):
    rid, owner, bare, admin, sup = _setup(db_path)
    tok = create_session(admin, device_type="ios", db_path=db_path)
    body = client.get("/mobile/api/admin/clients", headers=_bearer(tok)).get_json()
    ids = [c["id"] for c in body["clients"]]
    assert rid in ids and bare not in ids
    assert body["hours"] == auth.VIEW_AS_HOURS and body["read_only"] is False


def test_an_admin_opens_a_writable_view_on_the_owner_login_and_it_is_audited(client, db_path):
    rid, owner, bare, admin, sup = _setup(db_path)
    tok = create_session(admin, device_type="ios", db_path=db_path)
    body = client.post(f"/mobile/api/admin/view-as/{rid}", headers=_bearer(tok)).get_json()
    assert body["ok"] and body["read_only"] is False and body["restaurant_name"] == "Simple EJ's"
    assert body["user"]["id"] == owner and body["user"]["is_admin"] is False
    viewing = get_session_user(body["token"], db_path=db_path)
    assert viewing["id"] == owner and viewing["device_type"] == "admin-view-as"
    assert viewing["acting_admin_id"] == admin
    started = _events(db_path, "view_as_started")
    assert len(started) == 1 and started[0]["actor_id"] == admin and started[0]["restaurant_id"] == rid
    assert '"ios"' in started[0]["after_json"]
    # The admin's own session is untouched: the app goes back to it.
    assert get_session_user(tok, db_path=db_path)["id"] == admin


def test_a_support_login_is_not_let_into_the_app_to_open_one(client, db_path):
    # The app's door already turns a support login away (it is not a
    # restaurant login); a view-as from the phone is an admin's.
    rid, owner, bare, admin, sup = _setup(db_path)
    tok = create_session(sup, device_type="ios", db_path=db_path)
    assert client.post(f"/mobile/api/admin/view-as/{rid}", headers=_bearer(tok)).status_code == 403
    assert client.get("/mobile/api/admin/clients", headers=_bearer(tok)).status_code == 403


def test_a_restaurant_with_no_owner_login_is_refused(client, db_path):
    rid, owner, bare, admin, sup = _setup(db_path)
    tok = create_session(admin, device_type="ios", db_path=db_path)
    assert client.post(f"/mobile/api/admin/view-as/{bare}", headers=_bearer(tok)).status_code == 404


def test_a_view_never_files_the_phone_as_the_clients_device(client, db_path, monkeypatch):
    rid, owner, bare, admin, sup = _setup(db_path)
    tok = create_session(admin, device_type="ios", db_path=db_path)
    view = client.post(f"/mobile/api/admin/view-as/{rid}", headers=_bearer(tok)).get_json()["token"]
    import push
    filed = []
    monkeypatch.setattr(push, "register_device_token", lambda *a, **k: filed.append(a))
    r = client.post("/mobile/api/device-tokens", headers=_bearer(view),
                    json={"apns_token": "ab" * 32, "environment": "production"})
    assert r.get_json() == {"ok": True, "skipped": "view_as"} and filed == []
    r = client.post("/mobile/api/live-activity-tokens", headers=_bearer(view),
                    json={"activity_type": "service", "kind": "start", "token": "cd" * 32})
    assert r.get_json()["skipped"] == "view_as"
    # The admin's own session still files it.
    client.post("/mobile/api/device-tokens", headers=_bearer(tok),
                json={"apns_token": "ab" * 32, "environment": "production"})
    assert filed and filed[0][0] == admin


def test_stopping_ends_only_a_view_and_records_it(client, db_path):
    rid, owner, bare, admin, sup = _setup(db_path)
    tok = create_session(admin, device_type="ios", db_path=db_path)
    view = client.post(f"/mobile/api/admin/view-as/{rid}", headers=_bearer(tok)).get_json()["token"]
    # An ordinary session is never signed out from here.
    assert client.post("/mobile/api/admin/stop-viewing", headers=_bearer(tok)).get_json()["stopped"] is False
    assert get_session_user(tok, db_path=db_path) is not None
    assert client.post("/mobile/api/admin/stop-viewing", headers=_bearer(view)).get_json()["stopped"] is True
    assert get_session_user(view, db_path=db_path) is None
    stopped = _events(db_path, "view_as_stopped")
    assert len(stopped) == 1 and stopped[0]["actor_id"] == admin
    # Again, once over: nothing to stop, still ok.
    assert client.post("/mobile/api/admin/stop-viewing", headers=_bearer(view)).get_json() == \
        {"ok": True, "stopped": False}


def test_a_read_only_view_can_still_be_stopped(client, db_path):
    rid, owner, bare, admin, sup = _setup(db_path)
    view = auth.create_view_as_session(owner, {"id": sup, "is_admin": 0}, read_only=True, db_path=db_path)
    assert client.post("/mobile/api/admin/stop-viewing", headers=_bearer(view)).get_json()["stopped"] is True
    assert get_session_user(view, db_path=db_path) is None
