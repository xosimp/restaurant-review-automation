"""Double-submit CSRF enforcement — exercised through a real Flask test
client, the same request path production traffic takes."""
import pytest
from flask import Blueprint, Flask, jsonify

from csrf import csrf_protect, ensure_csrf_cookie, CSRF_COOKIE, CSRF_HEADER


@pytest.fixture
def client():
    app = Flask(__name__)
    bp = Blueprint("guarded", __name__)

    @bp.route("/api/thing", methods=["GET", "POST", "DELETE"])
    def thing():
        return jsonify(ok=True)

    csrf_protect(bp)
    app.register_blueprint(bp)
    app.after_request(ensure_csrf_cookie)
    return app.test_client()


def test_get_passes_without_token(client):
    assert client.get("/api/thing").status_code == 200


def test_post_without_token_blocked(client):
    resp = client.post("/api/thing")
    assert resp.status_code == 403
    assert resp.get_json()["ok"] is False


def test_post_with_matching_header_passes(client):
    client.set_cookie(CSRF_COOKIE, "tok123")
    resp = client.post("/api/thing", headers={CSRF_HEADER: "tok123"})
    assert resp.status_code == 200


def test_post_with_wrong_header_blocked(client):
    client.set_cookie(CSRF_COOKIE, "tok123")
    assert client.post("/api/thing", headers={CSRF_HEADER: "evil"}).status_code == 403


def test_delete_is_protected_too(client):
    assert client.delete("/api/thing").status_code == 403
    client.set_cookie(CSRF_COOKIE, "tok123")
    assert client.delete("/api/thing", headers={CSRF_HEADER: "tok123"}).status_code == 200


def test_json_body_field_accepted_as_fallback(client):
    client.set_cookie(CSRF_COOKIE, "tok123")
    resp = client.post("/api/thing", json={"csrf_token": "tok123"})
    assert resp.status_code == 200


def test_cookie_issued_on_first_response(client):
    resp = client.get("/api/thing")
    cookies = resp.headers.getlist("Set-Cookie")
    assert any(CSRF_COOKIE + "=" in c for c in cookies)


def test_fetch_wrapper_is_es5_and_loaded_first():
    """The wrapper include must exist, be ES5-only, and sit right after <body>
    in every fetch-using template."""
    import os, re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    wrapper = open(os.path.join(root, "templates", "_csrf_fetch.html"), encoding="utf-8").read()
    for pat in (r"`", r"\bconst\s", r"\blet\s", r"=>", r"\basync\s"):
        assert not re.search(pat, wrapper), f"non-ES5 syntax in _csrf_fetch.html: {pat}"
    for name in ("dashboard", "admin", "client_settings", "client_data"):
        src = open(os.path.join(root, "templates", name + ".html"), encoding="utf-8").read()
        body_pos = src.find("<body>")
        include_pos = src.find('{% include "_csrf_fetch.html" %}')
        assert include_pos != -1, f"{name}.html missing csrf fetch wrapper"
        assert body_pos < include_pos < body_pos + 200, f"{name}.html wrapper not immediately after <body>"
