"""Edge cases for direct Instagram/Facebook publishing (social_routes.py) and
the routes in front of it (web /api/post-to-*, /mobile/api/marketing/*).

What these protect:

  * every Graph call names a timeout, and the repo's timeout lint can see a
    call made through `import requests as _req` (MOD-MKT-2);
  * a publish is gated like every other marketing write: the Marketing module
    (MOD-MKT-17) and the approval role (MOD-MKT-17 — an invited teammate who
    "cannot approve" must not be able to publish directly);
  * Meta's odd answers — a non-JSON gateway page, a container that errors or
    never finishes, a raw "(#200) ..." message — become something the owner
    can act on rather than a 500 or Meta's own words (MOD-MKT-18);
  * an Instagram publish does not park a request thread for the full 20s
    container poll (MOD-MKT-3).

Graph is never reached: `requests` is swapped in sys.modules for a scripted
fake, and time.sleep is a recorder.
"""
import ast
import importlib.util
import os
import sys
import time
from datetime import timedelta

import pytest
from flask import Flask

import auth
import client_api
import marketing_publish as mp
import marketing_tags
import mobile_api
import models
import outcomes
import social_routes
from client_api import client_bp
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def _template_db(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("mkt_publish_template") / "template.db")
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
    for mod in (models, auth, mp, marketing_tags, outcomes, social_routes, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    import ops
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)


@pytest.fixture
def sleeps(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    return slept


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    flask_app.register_blueprint(social_bp)
    flask_app.register_blueprint(client_bp)
    flask_app.register_blueprint(mobile_bp)
    return flask_app


def _restaurant(db_path, name="Publish Co", module_marketing=1, connect=True):
    rid = create_restaurant(Restaurant(name=name, owner_email="owner@publish.test",
                                       module_marketing=module_marketing, timezone="America/Chicago"),
                            db_path=db_path)
    if connect:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET ig_token='igt', ig_user_id='igu', fb_page_token='fbt', "
                     "fb_page_id='fbp' WHERE id=?", (rid,))
        conn.commit()
        conn.close()
    return rid


CSRF = "csrf-test-token"


class Session:
    """A real logged-in session: a users row, a sessions row, and the cookie
    (web) or Bearer header (mobile) that carries it. Sends the double-submit
    CSRF pair on every web write, in case the blueprint has been wired."""

    def __init__(self, app, db_path, rid, role="client", username="owner"):
        self.client = app.test_client()
        uid = auth.create_user(rid, username, f"{username}@publish.test", "correct-horse-battery", db_path=db_path)
        auth.set_user_role(uid, role, db_path=db_path)
        self.token = auth.create_session(uid, restaurant_id=rid, db_path=db_path)
        self.client.set_cookie("session_token", self.token)
        self.client.set_cookie("csrf_js", CSRF)

    def post(self, path, **kw):
        headers = dict(kw.pop("headers", {}) or {})
        if path.startswith("/mobile/api/"):
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            headers["X-CSRF"] = CSRF
        return self.client.post(path, headers=headers, **kw)

    def get(self, path, **kw):
        headers = dict(kw.pop("headers", {}) or {})
        if path.startswith("/mobile/api/"):
            headers["Authorization"] = f"Bearer {self.token}"
        return self.client.get(path, headers=headers, **kw)


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
    """Stands in for the `requests` module; answers by (method, url
    substring), first match wins, and records every call with its kwargs."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def _answer(self, method, url, kw):
        self.calls.append((method, url, kw))
        for m, needle, resp in self.routes:
            if m == method and needle in url:
                return resp(url, kw) if callable(resp) else resp
        raise AssertionError(f"unexpected Graph call {method} {url}")

    def get(self, url, **kw):
        return self._answer("GET", url, kw)

    def post(self, url, **kw):
        return self._answer("POST", url, kw)

    def posts_to(self, needle):
        return [c for c in self.calls if c[0] == "POST" and needle in c[1]]


def _graph(monkeypatch, routes):
    fake = FakeGraph(routes)
    monkeypatch.setitem(sys.modules, "requests", fake)
    return fake


FB_OK = [("POST", "fbp/feed", FakeResp(200, {"id": "fbp_123"}))]


def _ig(status_code="FINISHED", publish=None):
    return [
        ("POST", "igu/media_publish", publish or FakeResp(200, {"id": "ig_post_1"})),
        ("POST", "igu/media", FakeResp(200, {"id": "container_1"})),
        ("GET", "container_1", FakeResp(200, {"status_code": status_code})),
    ]


IMG = "https://dashboard.cavnar.ai/m/tok.jpg"


# ── #2 no timeout (MOD-MKT-2) ─────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-MKT-2: _do_post_to_facebook calls Graph /feed with no timeout=")
def test_the_facebook_publish_names_a_timeout_on_its_graph_call(db_path, monkeypatch):
    """A6 direct #2 / MOD-MKT-2: a black-holed Graph connection must not
    hang the scheduler thread or a request thread forever."""
    rid = _restaurant(db_path)
    fake = _graph(monkeypatch, FB_OK)
    payload, _ = social_routes._do_post_to_facebook(rid, "Wings tonight", "")
    assert payload["ok"]
    assert all(kw.get("timeout") for _m, _u, kw in fake.calls), fake.calls


@pytest.mark.xfail(strict=True, reason="MOD-MKT-2: _do_post_to_instagram makes its create/poll/publish Graph calls with no timeout=")
def test_the_instagram_publish_names_a_timeout_on_every_graph_call(db_path, monkeypatch, sleeps):
    """A6 direct #2 / MOD-MKT-2."""
    rid = _restaurant(db_path)
    fake = _graph(monkeypatch, _ig())
    payload, _ = social_routes._do_post_to_instagram(rid, "Wings tonight", IMG, "")
    assert payload["ok"]
    missing = [(m, u) for m, u, kw in fake.calls if not kw.get("timeout")]
    assert missing == []


_VERBS = {"get", "post", "put", "delete", "patch", "head", "options", "request"}


def _untimed_calls(path):
    """Every requests call in `path` without timeout=, resolving any
    `import requests as X` alias at any scope (the check_timeouts.py blind
    spot)."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    aliases = {"requests"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "requests":
                    aliases.add(a.asname or "requests")
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (isinstance(fn, ast.Attribute) and fn.attr in _VERBS and isinstance(fn.value, ast.Name)
                and fn.value.id in aliases
                and not any(k.arg == "timeout" for k in node.keywords if k.arg)):
            out.append(f"{os.path.basename(path)}:{node.lineno} {fn.value.id}.{fn.attr}")
    return out


@pytest.mark.xfail(strict=True, reason="MOD-MKT-2: 12 aliased `_req.get/_req.post` Graph calls in social_routes/scheduler/admin_routes have no timeout")
def test_no_requests_call_on_the_meta_paths_goes_out_without_a_timeout_even_under_an_alias():
    """MOD-MKT-2: the AST scan the finding asks for, over the three files
    that make Meta calls through `import requests as _req`."""
    offenders = []
    for name in ("social_routes.py", "scheduler.py", "admin_routes.py"):
        offenders += _untimed_calls(os.path.join(ROOT, name))
    assert offenders == []


@pytest.mark.xfail(strict=True, reason="MOD-MKT-2: scripts/check_timeouts.py only knows the literal names requests/_requests/httpx/_httpx, so `_req.get(...)` passes")
def test_the_timeout_lint_sees_a_call_made_through_an_import_alias(tmp_path):
    """MOD-MKT-2: the repo lint itself, pointed at one aliased untimed call."""
    spec = importlib.util.spec_from_file_location("check_timeouts_edge", os.path.join(ROOT, "scripts", "check_timeouts.py"))
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)
    (tmp_path / "graph_caller.py").write_text(
        "def publish():\n"
        "    import requests as _req\n"
        "    return _req.post('https://graph.facebook.com/v21.0/1/feed', data={})\n")
    lint.ROOT = str(tmp_path)
    assert lint.offenders(), "the lint passed an untimed call made through `_req`"


def test_the_timeout_lint_still_catches_the_plain_spelling(tmp_path):
    """MOD-MKT-2 control: the lint works for `requests.get` — the harness
    above is measuring the alias gap, not a broken loader."""
    spec = importlib.util.spec_from_file_location("check_timeouts_edge2", os.path.join(ROOT, "scripts", "check_timeouts.py"))
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)
    (tmp_path / "plain_caller.py").write_text("import requests\nrequests.get('https://example.com')\n")
    lint.ROOT = str(tmp_path)
    assert len(lint.offenders()) == 1


# ── #3 non-JSON error body ────────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-A6-direct-3 (MOD-MKT-4): a non-JSON Graph error body makes r.json() raise inside /api/post-to-facebook, which 500s instead of refusing in JSON")
def test_a_gateway_page_from_facebook_comes_back_as_a_json_refusal(app, db_path, monkeypatch):
    """A6 direct #3: Meta's edge answers an HTML 502 now and then; the
    owner's browser does r.json() on whatever comes back."""
    rid = _restaurant(db_path)
    _graph(monkeypatch, [("POST", "fbp/feed", FakeResp(502, None, text="<html>502 Bad Gateway</html>"))])
    s = Session(app, db_path, rid)
    resp = s.post("/api/post-to-facebook", json={"caption": "Wings tonight"})
    assert resp.status_code < 500, resp.status_code
    body = resp.get_json()
    assert body is not None and body["ok"] is False and body.get("error")


# ── #4 the Instagram container ────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-A6-direct-4: a container whose status_code is ERROR is still sent to media_publish (the poll only breaks on FINISHED)")
def test_an_instagram_container_that_errors_is_never_sent_to_media_publish(db_path, monkeypatch, sleeps):
    """A6 direct #4: Meta rejected the image (wrong aspect ratio, unreachable
    URL). Publishing it anyway is a guaranteed failure with Meta's wording."""
    rid = _restaurant(db_path)
    fake = _graph(monkeypatch, _ig(status_code="ERROR"))
    payload, _ = social_routes._do_post_to_instagram(rid, "Wings tonight", IMG, "")
    assert not payload["ok"]
    assert fake.posts_to("igu/media_publish") == []


def test_an_instagram_container_that_never_finishes_is_polled_a_bounded_number_of_times(db_path, monkeypatch, sleeps):
    """A6 direct #4: IN_PROGRESS forever must not loop forever."""
    rid = _restaurant(db_path)
    fake = _graph(monkeypatch, _ig(status_code="IN_PROGRESS",
                                   publish=FakeResp(400, {"error": {"message": "Media ID is not available"}})))
    payload, _ = social_routes._do_post_to_instagram(rid, "Wings tonight", IMG, "")
    polls = [c for c in fake.calls if c[0] == "GET"]
    assert 1 <= len(polls) <= 10
    assert not payload["ok"]


# ── #5 double press ───────────────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-A6-direct-5: /api/post-to-facebook has no idempotency; a double press or a client retry posts the same copy twice")
def test_a_double_press_on_post_to_facebook_publishes_once(app, db_path, monkeypatch):
    """A6 direct #5: the second identical request seconds later (a double
    tap, or the client retrying after its own timeout) must not reach /feed."""
    rid = _restaurant(db_path)
    fake = _graph(monkeypatch, FB_OK)
    s = Session(app, db_path, rid)
    s.post("/api/post-to-facebook", json={"caption": "Wings tonight", "topic": "wings"})
    s.post("/api/post-to-facebook", json={"caption": "Wings tonight", "topic": "wings"})
    assert len(fake.posts_to("fbp/feed")) == 1


# ── #6 / Drafts #7: approval is not enforced at publish (MOD-MKT-17) ──────

def _schedule_body(rid):
    when = mp._local_now(rid) + timedelta(hours=3)
    return {"platform": "facebook", "body": "Unapproved copy", "scheduled_for": when.strftime("%Y-%m-%dT%H:%M")}


PUBLISH_PATHS = [
    "/api/post-to-facebook",
    "/api/post-to-instagram",
    "/api/marketing/schedule",
    "/mobile/api/marketing/post-to-facebook",
    "/mobile/api/marketing/post-to-instagram",
    "/mobile/api/marketing/schedule",
]


def _publish_request(s, path, rid):
    if path.endswith("/schedule"):
        return s.post(path, json=_schedule_body(rid))
    if "instagram" in path:
        return s.post(path, json={"caption": "Unapproved copy", "image_url": IMG})
    return s.post(path, json={"caption": "Unapproved copy"})


@pytest.mark.parametrize("path", PUBLISH_PATHS)
@pytest.mark.xfail(strict=True, reason="MOD-MKT-17: every publish/schedule route is only login_required; a 'member' who cannot approve can publish directly")
def test_an_invited_teammate_who_cannot_approve_cannot_publish_either(app, db_path, monkeypatch, sleeps, path):
    """A6 direct #6 / MOD-MKT-17: approve_draft refuses role 'member'; the
    routes that actually put copy on the public feed must refuse it too."""
    rid = _restaurant(db_path)
    fake = _graph(monkeypatch, FB_OK + _ig())
    s = Session(app, db_path, rid, role="member", username="teammate")
    resp = _publish_request(s, path, rid)
    assert resp.status_code == 403, (resp.status_code, resp.get_json())
    assert fake.calls == []


@pytest.mark.parametrize("path", PUBLISH_PATHS)
def test_the_primary_login_can_still_publish_on_every_route(app, db_path, monkeypatch, sleeps, path):
    """A6 direct #6 control: the gate above must not lock out the account
    that holds MARKETING_APPROVE."""
    rid = _restaurant(db_path)
    _graph(monkeypatch, FB_OK + _ig())
    s = Session(app, db_path, rid, role="client")
    resp = _publish_request(s, path, rid)
    assert resp.status_code == 200, (resp.status_code, resp.get_json())
    assert resp.get_json()["ok"] is True


# ── #7 no Marketing module ────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/api/post-to-facebook", "/api/post-to-instagram"])
@pytest.mark.xfail(strict=True, reason="MOD-MKT-17: /api/post-to-facebook and /api/post-to-instagram are missing from auth._MODULE_PREFIXES, so they skip the Marketing module gate")
def test_a_restaurant_without_marketing_cannot_publish_through_the_web_post_routes(app, db_path, monkeypatch, sleeps, path):
    """A6 direct #7 / MOD-MKT-17."""
    rid = _restaurant(db_path, module_marketing=0)
    fake = _graph(monkeypatch, FB_OK + _ig())
    s = Session(app, db_path, rid)
    resp = _publish_request(s, path, rid)
    assert resp.status_code == 403
    assert fake.calls == []


@pytest.mark.parametrize("path", ["/mobile/api/marketing/post-to-facebook", "/mobile/api/marketing/post-to-instagram",
                                  "/api/marketing/schedule", "/mobile/api/marketing/schedule"])
def test_a_restaurant_without_marketing_is_refused_on_the_gated_publish_routes(app, db_path, monkeypatch, sleeps, path):
    """A6 direct #7: the mobile twins and the schedule route are gated today
    (auth._MODULE_PREFIXES covers /mobile/api/marketing and /api/marketing/)."""
    rid = _restaurant(db_path, module_marketing=0)
    fake = _graph(monkeypatch, FB_OK + _ig())
    s = Session(app, db_path, rid)
    resp = _publish_request(s, path, rid)
    assert resp.status_code == 403
    assert fake.calls == []


def test_a_restaurant_without_marketing_cannot_post_to_google(app, db_path, monkeypatch):
    """A6 direct #7: /api/post-to-google is in the module table."""
    rid = _restaurant(db_path, module_marketing=0)
    import gmb
    called = []
    monkeypatch.setattr(gmb, "create_local_post", lambda *a, **k: called.append(1) or {"ok": True, "name": "x"})
    s = Session(app, db_path, rid)
    resp = s.post("/api/post-to-google", json={"summary": "Wings tonight"})
    assert resp.status_code == 403
    assert called == []


# ── #8 empty caption ──────────────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-A6-direct-8: _do_post_to_facebook sends an empty message to Graph /feed instead of refusing locally")
def test_an_empty_facebook_post_is_refused_without_calling_graph(db_path, monkeypatch):
    """A6 direct #8: an empty caption is always an owner mistake (a stale
    compose box); it should be refused here, not by Meta."""
    rid = _restaurant(db_path)
    fake = _graph(monkeypatch, FB_OK)
    payload, _ = social_routes._do_post_to_facebook(rid, "   ", "")
    assert not payload["ok"]
    assert fake.calls == []


def test_the_shared_publish_path_refuses_an_empty_body_before_any_platform(db_path, monkeypatch):
    """A6 direct #8: publish_now (the queue's path) already refuses."""
    rid = _restaurant(db_path)
    fake = _graph(monkeypatch, FB_OK)
    for platform in ("facebook", "instagram", "google"):
        result = mp.publish_now(rid, platform, "  ", media_token="tok", db_path=db_path)
        assert result == {"ok": False, "error": "There's no post text to publish.", "reached_platform": False}
    assert fake.calls == []


# ── #9 Meta's raw error text (MOD-MKT-18) ─────────────────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-MKT-18: social_routes returns Meta's raw error.message ('(#200) ...') to the owner verbatim")
def test_a_meta_permission_error_is_explained_not_echoed(app, db_path, monkeypatch):
    """A6 direct #9 / MOD-MKT-18: "(#200) If posting to a page, requires
    both pages_read_engagement and pages_manage_posts" means nothing to an
    owner; what they need is "reconnect Facebook"."""
    rid = _restaurant(db_path)
    meta_msg = ("(#200) If posting to a page, requires both pages_read_engagement "
                "and pages_manage_posts as an admin with sufficient administrative permission")
    _graph(monkeypatch, [("POST", "fbp/feed", FakeResp(403, {"error": {"message": meta_msg, "code": 200}}))])
    s = Session(app, db_path, rid)
    body = s.post("/api/post-to-facebook", json={"caption": "Wings tonight"}).get_json()
    assert body["ok"] is False and body.get("error")
    assert "(#200)" not in body["error"]
    assert "pages_manage_posts" not in body["error"]


# ── #10 an Instagram post holds its request thread (MOD-MKT-3) ────────────

@pytest.mark.xfail(strict=True, reason="MOD-MKT-3: _do_post_to_instagram sleeps up to 20s (10 x 2s) inside the calling request/scheduler thread")
def test_an_instagram_post_does_not_park_the_calling_thread_for_the_whole_container_poll(db_path, monkeypatch, sleeps):
    """A6 direct #10 / MOD-MKT-3: with gunicorn --threads 4, four owners
    posting to Instagram at once take the whole platform for 20s. The
    polling belongs off the request thread (a two-phase publish)."""
    rid = _restaurant(db_path)
    _graph(monkeypatch, _ig(status_code="IN_PROGRESS",
                            publish=FakeResp(400, {"error": {"message": "Media ID is not available"}})))
    social_routes._do_post_to_instagram(rid, "Wings tonight", IMG, "")
    assert sum(sleeps) < 20, f"slept {sum(sleeps)}s in the caller's thread"


# ── MOD-MKT-18: the two debug routes ──────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-MKT-18: /api/debug-insights queries a hard-coded post id with fb_page_token=None for a restaurant with no Facebook connection")
def test_debug_insights_makes_no_graph_call_for_a_restaurant_without_facebook(app, db_path, monkeypatch):
    """MOD-MKT-18: a live authenticated route that reads someone else's
    hard-coded post. (Not a deletion recommendation — see CLAUDE.md.)"""
    rid = _restaurant(db_path, connect=False)
    fake = _graph(monkeypatch, [("GET", "", FakeResp(200, {"id": "1206793632506765_122108397639307271"}))])
    s = Session(app, db_path, rid)
    s.get("/api/debug-insights")
    assert fake.calls == []


def test_meta_review_test_makes_no_graph_call_when_facebook_is_not_connected(app, db_path, monkeypatch):
    """MOD-MKT-18: the other temporary route returns early when unconnected."""
    rid = _restaurant(db_path, connect=False)
    fake = _graph(monkeypatch, [])
    s = Session(app, db_path, rid)
    body = s.get("/api/meta-review-test").get_json()
    assert body == {"ok": False, "error": "Facebook not connected"}
    assert fake.calls == []
