"""The web and phone twins are wired by name; this resolves every name
(audit #25 A04-03 / R21).

About 150 web routes reach their phone body by string — client_api's
`_m("mobile_x")` does `getattr(mobile_api, name)` and strips ONE decorator
(`__wrapped__`) — and mobile_api reaches back into client_api through
`_capi.<name>` (and `_capi.<route>.__wrapped__`). A rename passed py_compile
and every test and turned the web half into a 500; a decorator added ABOVE
`@mobile_login_required` would be the one stripped, and the web call would
then run the Bearer check against a cookie session (every web user a 401).
The old guards were source-text asserts that the string is present.

This file:
- resolves every `_m("…")` to a function in mobile_api whose OUTERMOST
  wrapper is `mobile_login_required` (and which is not wrapped by it twice);
- resolves every `_capi.<name>` mobile_api uses, and checks each
  `_capi.<route>.__wrapped__` strips exactly `login_required`;
- calls every web route that delegates through `_m` once, through the real
  client_bp as a signed-in owner, and asserts it does not 500 (31 of them
  were referenced by no test at all).
"""
import ast
import os
import re
import socket

import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")
CSRF = {"X-CSRF": "twin-csrf"}


def _tree(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _m_names():
    """{mobile function name: [client_api functions that delegate to it]}."""
    out = {}
    for fn in ast.walk(_tree("client_api.py")):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_m"
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                out.setdefault(node.args[0].value, []).append(fn.name)
    return out


def _capi_uses():
    """(attribute names used as `_capi.<name>`, names used as `_capi.<name>.__wrapped__`)."""
    names, wrapped = set(), set()
    for node in ast.walk(_tree("mobile_api.py")):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "_capi":
            names.add(node.attr)
        if (isinstance(node, ast.Attribute) and node.attr == "__wrapped__" and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name) and node.value.value.id == "_capi"):
            wrapped.add(node.value.attr)
    return names, wrapped


def _wrapper_code(decorator):
    """The code object every function the decorator wraps shares (its inner
    `decorated`): how a wrapper is recognised without reading source."""
    return decorator(lambda *a, **k: None).__code__


MOBILE_AUTH = _wrapper_code(auth.mobile_login_required)
WEB_AUTH = _wrapper_code(auth.login_required)


# ── the names resolve, and the stripped decorator is the auth one ───────────

def test_the_scan_finds_the_delegations():
    """Guards the guard: a scan that matched nothing would pass below."""
    names = _m_names()
    assert len(names) >= 140, len(names)
    assert "mobile_verify_2fa_setup" in names or any("2fa" in n for n in names)
    used, wrapped = _capi_uses()
    assert len(used) >= 50 and wrapped >= {"home_dismiss_api", "home_assign_api", "dismiss_onboarding"}


@pytest.mark.parametrize("name", sorted(_m_names()))
def test_every_web_to_phone_name_resolves_to_a_bearer_guarded_view(name):
    fn = getattr(mobile_api, name, None)
    assert callable(fn), f"client_api delegates to mobile_api.{name}, which does not exist ({_m_names()[name]})"
    assert getattr(fn, "__code__", None) is MOBILE_AUTH, (
        f"mobile_api.{name}'s outermost decorator is not @mobile_login_required: client_api._m strips the "
        f"outermost one, so the web call would run whatever is under it")
    inner = getattr(fn, "__wrapped__", None)
    assert inner is not None and getattr(inner, "__code__", None) is not MOBILE_AUTH, \
        f"mobile_api.{name} is wrapped by mobile_login_required twice: the web call would still check a Bearer token"
    assert client_api._m(name) is inner


def test_the_check_sees_a_decorator_stacked_above_the_auth_one():
    """The hazard itself: a rate limiter (any functools.wraps decorator)
    added above @mobile_login_required is what _m would strip."""
    import functools

    def limiter(f):
        @functools.wraps(f)
        def limited(*a, **k):
            return f(*a, **k)
        return limited

    def body(current_user):
        return current_user
    guarded = auth.mobile_login_required(body)
    assert guarded.__code__ is MOBILE_AUTH
    assert limiter(guarded).__code__ is not MOBILE_AUTH
    assert auth.mobile_login_required(guarded).__wrapped__.__code__ is MOBILE_AUTH      # wrapped twice


def test_every_capi_name_the_phone_uses_exists():
    used, _ = _capi_uses()
    missing = sorted(n for n in used if not hasattr(client_api, n))
    assert missing == [], f"mobile_api calls client_api names that do not exist: {missing}"


@pytest.mark.parametrize("name", sorted(_capi_uses()[1]))
def test_every_capi_route_the_phone_unwraps_is_login_required_outermost(name):
    fn = getattr(client_api, name)
    assert getattr(fn, "__code__", None) is WEB_AUTH, (
        f"client_api.{name}'s outermost decorator is not @login_required: mobile_api strips it with __wrapped__")
    assert getattr(fn.__wrapped__, "__code__", None) is not WEB_AUTH


# ── every delegating web route answers without a 500 ────────────────────────

def _delegating_endpoints():
    """{client_bp endpoint: rule} for every web route whose view delegates
    through _m."""
    delegators = {d for ds in _m_names().values() for d in ds}
    app = Flask(__name__, template_folder=TEMPLATES)
    app.register_blueprint(client_api.client_bp)
    out = {}
    for rule in app.url_map.iter_rules():
        if rule.endpoint.startswith("client.") and rule.endpoint.split(".", 1)[1] in delegators:
            out[rule.endpoint] = rule
    return out


_ENDPOINTS = _delegating_endpoints()


@pytest.fixture
def owner_client(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)

    def _blocked(*a, **k):
        raise OSError("network blocked in the twin smoke")
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket.socket, "connect", lambda self, *a, **k: _blocked())
    for k in ("ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "GOOGLE_PLACES_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(k, "")
    rid = models.create_restaurant(models.Restaurant(name="Twin Co", owner_email="twin@x.test", service_tier="full",
                                                     billing_status="active", module_reviews=1, module_labor=1,
                                                     module_inventory=1, module_marketing=1), db_path=db_path)
    uid = auth.create_user(rid, "twinowner", "twin@x.test", "twin-owner-pass", db_path=db_path)
    auth.upsert_membership(uid, rid, "client", db_path=db_path)
    app = Flask(__name__, template_folder=TEMPLATES)
    app.secret_key = "test-secret"
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    c = app.test_client()
    c.set_cookie("session_token", auth.create_session(uid, db_path=db_path))
    c.set_cookie("csrf_js", CSRF["X-CSRF"])
    return c


def test_the_smoke_covers_the_delegating_routes():
    assert len(_ENDPOINTS) >= 140, len(_ENDPOINTS)


@pytest.mark.parametrize("endpoint", sorted(_ENDPOINTS))
def test_every_delegating_web_route_answers_without_a_500(owner_client, endpoint):
    """No unhandled exception (a renamed body is an AttributeError, a
    mis-stripped decorator a TypeError) and no 401 (the cookie session
    reached the body). A route's own deliberate error answer — a 502/503
    for a provider or model the suite does not have, a handled 500 that
    ops.capture-d — is the route's business, not the wiring's."""
    from flask import got_request_exception
    rule = _ENDPOINTS[endpoint]
    crashed = []

    def _record(sender, exception, **extra):
        crashed.append(f"{type(exception).__name__}: {exception}")
    got_request_exception.connect(_record, owner_client.application)
    path = re.sub(r"<(?:int:)?[^>]+>", lambda m: "1" if m.group(0).startswith("<int:") else "x", rule.rule)
    try:
        for method in sorted(set(rule.methods or ()) & {"GET", "POST", "PUT", "PATCH", "DELETE"}):
            if method == "GET":
                resp = owner_client.get(path, headers=CSRF)
            else:
                resp = owner_client.open(path, method=method, json={}, headers=CSRF)
            assert not crashed, f"{method} {rule.rule} raised: {crashed}"
            assert resp.status_code != 401, f"{method} {rule.rule} -> 401: the web session did not reach the body"
    finally:
        got_request_exception.disconnect(_record, owner_client.application)
