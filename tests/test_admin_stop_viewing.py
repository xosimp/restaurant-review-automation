"""/admin/stop-viewing returns to the admin login instead of a 500 (models has no get_session_user)."""
from flask import Flask

import admin_routes


def test_stop_viewing_redirects_and_clears_the_cookie(monkeypatch):
    deleted = []
    monkeypatch.setattr(admin_routes, "get_session_user", lambda t: {"id": 1} if t == "tok" else None)
    monkeypatch.setattr(admin_routes, "delete_session", lambda t: deleted.append(t))
    app = Flask(__name__)
    app.register_blueprint(admin_routes.admin_bp)
    c = app.test_client()
    c.set_cookie("session_token", "tok")
    r = c.get("/admin/stop-viewing")
    assert r.status_code == 302 and "/login?next=/admin" in r.headers["Location"] and deleted == ["tok"]
