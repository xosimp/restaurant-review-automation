"""The CSRF cookie must be issued exactly once per response.

A route that mints its own token for a plain HTML form used to have it
overwritten by ensure_csrf_cookie's after_request hook, which checked only
the incoming request. The result was two Set-Cookie: csrf_js headers with
different values, so the browser kept one and the form submitted the other
— every first-time staff availability submission was rejected as a forgery.
"""
import pytest
from flask import Blueprint, Flask, make_response

from csrf import CSRF_COOKIE, csrf_protect, ensure_csrf_cookie


@pytest.fixture
def app():
    bp = Blueprint("t", __name__)

    @bp.route("/mints-its-own")
    def mints_its_own():
        resp = make_response("<form><input name='csrf_token' value='route-token'></form>")
        resp.set_cookie(CSRF_COOKIE, "route-token")
        return resp

    @bp.route("/plain")
    def plain():
        return "hello"

    @bp.route("/submit", methods=["POST"])
    def submit():
        return "accepted"

    flask_app = Flask(__name__)
    csrf_protect(bp)
    flask_app.register_blueprint(bp)
    flask_app.after_request(ensure_csrf_cookie)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _csrf_cookies(resp):
    return [h for h in resp.headers.getlist("Set-Cookie") if h.startswith(f"{CSRF_COOKIE}=")]


def test_a_route_minting_its_own_token_is_not_overwritten(client):
    resp = client.get("/mints-its-own")
    issued = _csrf_cookies(resp)
    assert len(issued) == 1, f"expected one csrf cookie, got {issued}"
    assert "route-token" in issued[0]


def test_a_plain_route_still_gets_a_token_issued(client):
    assert len(_csrf_cookies(client.get("/plain"))) == 1


def test_a_request_that_already_has_the_cookie_is_not_reissued(client):
    client.set_cookie(CSRF_COOKIE, "existing")
    assert _csrf_cookies(client.get("/plain")) == []


def test_the_token_a_form_was_given_is_the_one_that_validates(client):
    """End to end: render the form, submit exactly what it contained."""
    client.get("/mints-its-own")
    resp = client.post("/submit", data={"csrf_token": "route-token"})
    assert resp.status_code == 200


def test_a_mismatched_token_is_still_rejected(client):
    client.get("/mints-its-own")
    assert client.post("/submit", data={"csrf_token": "wrong"}).status_code == 403
