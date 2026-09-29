"""/admin/stop-viewing: a GET only asks, a POST ends only a view-as session.

It used to delete whatever session the cookie named on a GET, so any link
anywhere signed its visitor out (SECURITY-15, fix round A). The POST now
ends an admin-view-as session and nothing else."""
from flask import Flask

import admin_routes


def _app():
    app = Flask(__name__)
    app.register_blueprint(admin_routes.admin_bp)
    return app


CSRF = "stop-viewing-csrf"


def _client():
    """The double-submit pair on every request, so these behave the same
    whether or not hosted_dashboard has wired csrf_protect onto admin_bp in
    this process."""
    c = _app().test_client()
    c.set_cookie("session_token", "tok")
    c.set_cookie("csrf_js", CSRF)
    return c


def test_a_get_never_ends_a_session(monkeypatch):
    deleted = []
    monkeypatch.setattr(admin_routes, "get_session_user", lambda t: {"id": 1, "device_type": "web"} if t == "tok" else None)
    monkeypatch.setattr(admin_routes, "delete_session", lambda t: deleted.append(t))
    c = _client()
    r = c.get("/admin/stop-viewing")
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin") and deleted == []


def test_a_get_on_a_view_as_session_asks_and_does_nothing(monkeypatch):
    deleted = []
    monkeypatch.setattr(admin_routes, "get_session_user",
                        lambda t: {"id": 1, "device_type": "admin-view-as", "acting_admin_id": 9} if t == "tok" else None)
    monkeypatch.setattr(admin_routes, "delete_session", lambda t: deleted.append(t))
    c = _client()
    r = c.get("/admin/stop-viewing")
    assert r.status_code == 200 and "Stop viewing" in r.get_data(as_text=True) and deleted == []


def test_a_post_ends_the_view_as_session_and_signs_in_again_without_a_saved_admin_session(monkeypatch):
    deleted = []
    monkeypatch.setattr(admin_routes, "get_session_user",
                        lambda t: {"id": 1, "device_type": "admin-view-as", "acting_admin_id": 9,
                                   "restaurant_id": 3, "username": "owner"} if t == "tok" else None)
    monkeypatch.setattr(admin_routes, "delete_session", lambda t: deleted.append(t))
    c = _client()
    r = c.post("/admin/stop-viewing", headers={"X-CSRF": CSRF})
    assert r.status_code == 302 and "/login?next=/admin" in r.headers["Location"] and deleted == ["tok"]


def test_a_post_never_ends_an_ordinary_session(monkeypatch):
    deleted = []
    monkeypatch.setattr(admin_routes, "get_session_user", lambda t: {"id": 1, "device_type": "web"} if t == "tok" else None)
    monkeypatch.setattr(admin_routes, "delete_session", lambda t: deleted.append(t))
    c = _client()
    r = c.post("/admin/stop-viewing", headers={"X-CSRF": CSRF})
    assert r.status_code == 302 and deleted == []
