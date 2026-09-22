"""Edge cases for the Meta (Instagram + Facebook) connection: the OAuth
callback in social_routes.py and the token refresh job in scheduler.py.

What these protect:

  * the callback binds a page only to the restaurant that started the flow —
    a bare numeric `state` is forgeable by anyone (MOD-MKT-5);
  * every failure answer from Meta (no business account, a refused or
    non-JSON token exchange) writes nothing and ends in the popup's error
    message, never a 500;
  * the refresh job names a timeout (MOD-MKT-2), and only moves the stored
    expiry forward when Meta actually handed back a token;
  * disconnect clears Instagram and Facebook together.

Graph is never reached: `requests` is swapped in sys.modules for a fake.
"""
import os
import sys

import pytest
from flask import Flask

import auth
import models
import social_routes
from models import Restaurant, create_restaurant, get_restaurant
from social_routes import social_bp

# Imported up front, not lazily inside a test: these swap `requests` in
# sys.modules for a fake, and a module first imported while the fake is in
# place (notify does `import requests` at top level) would keep it for the
# rest of the process.
import gmb  # noqa: E402,F401
import marketing  # noqa: E402,F401
import notify  # noqa: E402,F401
import scheduler  # noqa: E402,F401
import weather  # noqa: E402,F401


@pytest.fixture(scope="module")
def _template_db(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("mkt_meta_template") / "template.db")
    models.init_db(db_path=path)
    models.ensure_columns(db_path=path)
    auth.init_auth(db_path=path)
    return path


@pytest.fixture
def db_path(tmp_path, _template_db):
    """A copy of a module-built template (init_db + ensure_columns +
    init_auth) instead of conftest's per-test migration run."""
    import shutil
    path = str(tmp_path / "test_reviews.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(_template_db + suffix):
            shutil.copy(_template_db + suffix, path + suffix)
    return path


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, social_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    monkeypatch.setenv("META_APP_ID", "app-id")
    monkeypatch.setenv("META_APP_SECRET", "app-secret")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")


@pytest.fixture
def app():
    flask_app = Flask(__name__)
    flask_app.register_blueprint(social_bp)
    return flask_app


def _restaurant(db_path, name="Meta Co", **fields):
    rid = create_restaurant(Restaurant(name=name, owner_email="owner@meta.test", module_marketing=1),
                            db_path=db_path)
    if fields:
        models.update_restaurant(rid, fields, db_path=db_path)
    return rid


class FakeResp:
    def __init__(self, status, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else str(body)

    def json(self):
        if self._body is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body


class FakeGraph:
    """Stands in for `requests`; answers by (method, url substring[, param
    predicate]) in order, recording every call and its kwargs."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def _answer(self, method, url, kw):
        self.calls.append((method, url, kw))
        for route in self.routes:
            m, needle, resp = route[:3]
            when = route[3] if len(route) > 3 else None
            if m == method and needle in url and (when is None or when(kw)):
                return resp(url, kw) if callable(resp) else resp
        raise AssertionError(f"unexpected Graph call {method} {url} {kw}")

    def get(self, url, **kw):
        return self._answer("GET", url, kw)

    def post(self, url, **kw):
        return self._answer("POST", url, kw)


def _graph(monkeypatch, routes):
    fake = FakeGraph(routes)
    monkeypatch.setitem(sys.modules, "requests", fake)
    return fake


def _is_code_exchange(kw):
    return "code" in (kw.get("params") or {})


def _is_long_exchange(kw):
    return (kw.get("params") or {}).get("grant_type") == "fb_exchange_token"


ATTACKER_OAUTH = [
    ("GET", "oauth/access_token", FakeResp(200, {"access_token": "short-attacker"}), _is_code_exchange),
    ("GET", "oauth/access_token", FakeResp(200, {"access_token": "long-attacker"}), _is_long_exchange),
    ("GET", "me/accounts", FakeResp(200, {"data": [{"id": "attacker_page", "access_token": "attacker-page-token"}]})),
    ("GET", "attacker_page", FakeResp(200, {"instagram_business_account": {"id": "attacker_ig"}})),
]


def _meta_fields(db_path, rid):
    r = get_restaurant(rid, db_path=db_path)
    return {"ig_token": r.ig_token, "ig_user_id": r.ig_user_id,
            "fb_page_token": r.fb_page_token, "fb_page_id": r.fb_page_id,
            "ig_token_expires": r.ig_token_expires}


# ── #1 unsigned numeric state (MOD-MKT-5) ─────────────────────────────────

def test_a_callback_with_a_bare_numeric_state_does_not_bind_a_page_to_that_restaurant(app, db_path, monkeypatch):
    """A6 Meta #1 / MOD-MKT-5: the dialog URL is public; an attacker who
    completes it with their own Meta account and state=<victim id> must not
    replace the victim's connection (their queue would then post to the
    attacker's page)."""
    victim = _restaurant(db_path, name="Victim", ig_token="victim-ig", ig_user_id="victim_ig",
                         fb_page_token="victim-fb", fb_page_id="victim_page")
    _graph(monkeypatch, ATTACKER_OAUTH)
    app.test_client().get(f"/instagram/callback?code=attacker-code&state={victim}")
    after = _meta_fields(db_path, victim)
    assert after["ig_user_id"] == "victim_ig"
    assert after["fb_page_id"] == "victim_page"
    assert after["ig_token"] == "victim-ig"


def test_a_signed_mobile_state_connects_the_restaurant_that_asked(app, db_path, monkeypatch):
    """A6 Meta #1 control: the signed path (gmb.sign_mobile_state) works."""
    import gmb
    rid = _restaurant(db_path)
    _graph(monkeypatch, ATTACKER_OAUTH)
    resp = app.test_client().get(f"/instagram/callback?code=c&state={gmb.sign_mobile_state(rid)}")
    assert resp.status_code == 302
    assert "status=connected" in resp.headers["Location"]
    after = _meta_fields(db_path, rid)
    assert (after["ig_user_id"], after["fb_page_id"]) == ("attacker_ig", "attacker_page")


def test_a_tampered_mobile_state_connects_nothing(app, db_path, monkeypatch):
    """A6 Meta #1: a forged signature is refused and says so to the app."""
    import gmb
    rid = _restaurant(db_path)
    good = gmb.sign_mobile_state(rid)
    rid_s, exp, sig = good.split(":")
    forged = f"{rid_s}:{exp}:{'0' * len(sig)}"
    _graph(monkeypatch, ATTACKER_OAUTH)
    resp = app.test_client().get(f"/instagram/callback?code=c&state={forged}")
    assert "status=error" in resp.headers["Location"]
    assert not _meta_fields(db_path, rid)["ig_token"]


# ── #2 no Instagram business account ──────────────────────────────────────

def test_no_instagram_business_account_on_any_page_writes_nothing_and_says_so(app, db_path, monkeypatch):
    """A6 Meta #2: pages exist, none has an IG business account."""
    rid = _restaurant(db_path)
    _graph(monkeypatch, ATTACKER_OAUTH[:3] + [("GET", "attacker_page", FakeResp(200, {"id": "attacker_page"}))])
    resp = app.test_client().get(f"/instagram/callback?code=c&state={rid}")
    assert resp.status_code == 200
    assert b"no_ig_account" in resp.data
    after = _meta_fields(db_path, rid)
    assert not after["ig_token"] and not after["fb_page_token"]


# ── #3 token exchange failures ────────────────────────────────────────────

def test_a_refused_token_exchange_writes_nothing_and_tells_the_popup(app, db_path, monkeypatch):
    """A6 Meta #3: Meta answers 400 to the code exchange (reused code)."""
    rid = _restaurant(db_path)
    _graph(monkeypatch, [("GET", "oauth/access_token",
                          FakeResp(400, {"error": {"message": "This authorization code has been used."}}))])
    resp = app.test_client().get(f"/instagram/callback?code=used&state={rid}")
    assert resp.status_code == 200
    assert b"token_failed" in resp.data
    assert not _meta_fields(db_path, rid)["ig_token"]


def test_a_callback_with_no_code_writes_nothing(app, db_path, monkeypatch):
    """A6 Meta #3: the owner pressed Cancel on Meta's dialog."""
    rid = _restaurant(db_path)
    fake = _graph(monkeypatch, [])
    resp = app.test_client().get(f"/instagram/callback?error=access_denied&state={rid}")
    assert b"no_code" in resp.data
    assert fake.calls == []


@pytest.mark.xfail(strict=True, reason="MOD-A6-oauth-3: a 200 token-exchange answer that is not JSON makes r.json() raise; the callback 500s instead of showing the popup's error")
def test_a_non_json_token_exchange_answer_ends_in_the_popups_error_not_a_500(app, db_path, monkeypatch):
    """A6 Meta #3: an edge proxy page with a 200 status."""
    rid = _restaurant(db_path)
    _graph(monkeypatch, [("GET", "oauth/access_token", FakeResp(200, None, text="<html>Please wait…</html>"))])
    resp = app.test_client().get(f"/instagram/callback?code=c&state={rid}")
    assert resp.status_code == 200
    assert b"ig:'error'" in resp.data
    assert not _meta_fields(db_path, rid)["ig_token"]


# ── #4 / #5 the refresh job ───────────────────────────────────────────────

def _expiring(db_path, name="Expiring Co", with_fb=True):
    fields = {"ig_token": "old-ig", "ig_user_id": "igu", "ig_token_expires": "2000-01-02"}
    if with_fb:
        fields.update({"fb_page_token": "old-fb", "fb_page_id": "fbp", "fb_token_expires": "2000-01-02"})
    return _restaurant(db_path, name=name, **fields)


@pytest.mark.xfail(strict=True, reason="MOD-MKT-2: refresh_expiring_tokens calls Graph oauth/access_token with no timeout= from the scheduler thread")
def test_the_token_refresh_job_names_a_timeout_on_every_graph_call(db_path, monkeypatch):
    """A6 Meta #4 / MOD-MKT-2: one hung refresh stalls the single scheduler
    thread — every job, not just marketing."""
    import scheduler
    _expiring(db_path)
    fake = _graph(monkeypatch, [("GET", "oauth/access_token", FakeResp(200, {"access_token": "new"}))])
    scheduler.refresh_expiring_tokens()
    assert fake.calls, "setup: the refresh should have run"
    assert all(kw.get("timeout") for _m, _u, kw in fake.calls), fake.calls


def test_a_real_refresh_saves_both_new_tokens_and_a_later_expiry(db_path, monkeypatch):
    """A6 Meta #5 control: the normal path."""
    import scheduler
    rid = _expiring(db_path)

    def answer(url, kw):
        old = kw["params"]["fb_exchange_token"]
        return FakeResp(200, {"access_token": "new-" + old, "token_type": "bearer", "expires_in": 5183944})
    _graph(monkeypatch, [("GET", "oauth/access_token", answer)])
    scheduler.refresh_expiring_tokens()
    r = get_restaurant(rid, db_path=db_path)
    assert (r.ig_token, r.fb_page_token) == ("new-old-ig", "new-old-fb")
    assert r.ig_token_expires > "2026-01-01"
    assert r.fb_token_expires == r.ig_token_expires


@pytest.mark.xfail(strict=True, reason="MOD-A6-oauth-5: a 200 refresh answer with no access_token keeps the old token but still pushes ig_token_expires 60 days out")
def test_a_refresh_that_hands_back_no_token_does_not_push_the_expiry_out(db_path, monkeypatch):
    """A6 Meta #5: the old token was not renewed, so the stored expiry must
    not move — otherwise the job stops trying for ~53 days while the real
    token dies, and every scheduled post fails."""
    import scheduler
    rid = _expiring(db_path, with_fb=False)
    _graph(monkeypatch, [("GET", "oauth/access_token", FakeResp(200, {}))])
    scheduler.refresh_expiring_tokens()
    r = get_restaurant(rid, db_path=db_path)
    assert r.ig_token == "old-ig"
    assert r.ig_token_expires == "2000-01-02"


def test_a_refused_refresh_leaves_the_stored_token_and_expiry_alone(db_path, monkeypatch):
    """A6 Meta #5: Meta says the token is already invalid."""
    import scheduler
    rid = _expiring(db_path)
    _graph(monkeypatch, [("GET", "oauth/access_token",
                          FakeResp(400, {"error": {"message": "Error validating access token", "code": 190}}))])
    scheduler.refresh_expiring_tokens()
    r = get_restaurant(rid, db_path=db_path)
    assert (r.ig_token, r.ig_token_expires) == ("old-ig", "2000-01-02")


def test_one_restaurants_refresh_blowing_up_does_not_stop_the_next(db_path, monkeypatch):
    """A6 Meta #4: a connection error on one restaurant is per-restaurant."""
    import scheduler
    first = _expiring(db_path, name="First", with_fb=False)
    second = _expiring(db_path, name="Second", with_fb=False)
    models.update_restaurant(first, {"ig_token": "boom"}, db_path=db_path)

    def answer(url, kw):
        if kw["params"]["fb_exchange_token"] == "boom":
            raise ConnectionError("connection reset")
        return FakeResp(200, {"access_token": "fresh"})
    _graph(monkeypatch, [("GET", "oauth/access_token", answer)])
    scheduler.refresh_expiring_tokens()
    assert get_restaurant(second, db_path=db_path).ig_token == "fresh"


# ── #6 disconnect ─────────────────────────────────────────────────────────

def test_disconnect_clears_instagram_and_facebook_together(app, db_path, monkeypatch):
    """A6 Meta #6: one button, both platforms — a half-disconnected account
    would keep publishing to Facebook."""
    rid = _restaurant(db_path, ig_token="t", ig_user_id="u", fb_page_token="f", fb_page_id="p")
    uid = auth.create_user(rid, "owner", "owner@meta.test", "correct-horse-battery", db_path=db_path)
    client = app.test_client()
    client.set_cookie("session_token", auth.create_session(uid, restaurant_id=rid, db_path=db_path))
    client.set_cookie("csrf_js", "tok")
    resp = client.post("/api/instagram-disconnect", headers={"X-CSRF": "tok"})
    assert resp.get_json() == {"ok": True}
    after = _meta_fields(db_path, rid)
    assert not any([after["ig_token"], after["ig_user_id"], after["fb_page_token"], after["fb_page_id"]])
    status = client.get("/api/instagram-status").get_json()
    assert status == {"connected": False, "fb_connected": False}
