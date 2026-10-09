"""A page that needs sign-in sends the browser to /login with the WHOLE
destination, query included: the app's "… on the web" rows open
/?nav=<section> in a fresh browser, and dropping ?nav= landed the owner on
Home (iOS re-audit 10/8/26). safe_next_url still refuses off-site targets."""
from urllib.parse import parse_qs, urlsplit

from flask import Flask

import auth
from auth_routes import auth_bp, safe_next_url


def _app():
    app = Flask(__name__)
    app.register_blueprint(auth_bp)

    @app.route("/")
    @auth.login_required
    def index(current_user):
        return "home"

    return app


def _next(location):
    return parse_qs(urlsplit(location).query).get("next", [""])[0]


def test_the_nav_query_survives_the_sign_in_redirect(monkeypatch):
    monkeypatch.setattr(auth, "get_current_user", lambda: None)
    r = _app().test_client().get("/?nav=account/notifications", headers={"Accept": "text/html"})
    assert r.status_code == 302
    assert _next(r.headers["Location"]) == "/?nav=account/notifications"
    assert safe_next_url(_next(r.headers["Location"])) == "/?nav=account/notifications"


def test_a_bare_path_has_no_trailing_question_mark(monkeypatch):
    monkeypatch.setattr(auth, "get_current_user", lambda: None)
    r = _app().test_client().get("/", headers={"Accept": "text/html"})
    assert _next(r.headers["Location"]) == "/"


def test_an_off_site_next_is_still_refused():
    assert safe_next_url("//evil.example/?nav=x") == "/"
    assert safe_next_url("https://evil.example/") == "/"
