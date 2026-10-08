"""iOS parity audit 10/7/26 — performance, network and battery (#37, #56).

#37  The Labor, Reviews and Marketing reads called a model on the request
     thread on every miss. Asked with `async`, a read renders from what is
     stored with model calls refused (ai_utils.stored_reads_only); a miss is
     written on the owner AI pool (insight_refresh / ai_async) while the
     last stored read, with its age, or `pending` answers — one refresh at
     a time, polled by `refresh_job`.
#56  JSON over 1 KB is gzipped (http_layer.compress_response, kept), and
     Home, Labor, the review inbox's first page and the Daily Report answer
     with a per-login ETag and a bodiless 304 on If-None-Match. A stream is
     never compressed or buffered.
"""
import gzip
import json

import pytest
from flask import Flask, Response, g, jsonify

import ai_async
import ai_orchestrator
import ai_utils
import auth
import auth_routes
import client_api
import http_layer
import insight_refresh
import insight_store
import mobile_api
import models
from auth import create_user, init_auth
from auth_routes import _login_attempts
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant


# ── fixtures ────────────────────────────────────────────────────────────────

class _Held:
    """An executor that keeps the job, so it stays pending until run()."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *a, **k):
        self.jobs.append((fn, a, k))

    def run_all(self):
        jobs, self.jobs = self.jobs, []
        for fn, a, k in jobs:
            fn(*a, **k)


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    _login_attempts.clear()
    # The process caches are module globals: each test starts cold.
    monkeypatch.setattr(client_api, "_insight_cache", {})
    monkeypatch.setattr(client_api, "_LABOR_READ_FP", {})
    yield db_path
    _login_attempts.clear()


@pytest.fixture
def held(monkeypatch):
    h = _Held()
    monkeypatch.setattr(ai_async, "_executor", lambda: h)
    return h


@pytest.fixture
def client(db):
    app = Flask(__name__)
    http_layer.register(app)
    app.register_blueprint(mobile_bp)
    return app.test_client()


def _rid(db):
    return create_restaurant(Restaurant(name="Perf Co", owner_email="p@x.test"), db_path=db)


def _token(client, db, rid, username="perf"):
    create_user(rid, username, f"{username}@x.test", "correct-horse", db_path=db)
    return client.post("/mobile/api/login", json={"username": username, "password": "correct-horse"}).get_json()["token"]


def _h(token, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


@pytest.fixture
def fake_labor_read(monkeypatch):
    """labor_read_text as the real one behaves: a stored read is returned,
    a miss calls the model (refused under stored_reads_only), and a written
    read is cached for the next render."""
    calls = {"model": 0}

    def fake(rid, analysis=None):
        ai_utils.refuse_if_stored_only("labor_insight")
        calls["model"] += 1
        text = "Labor ran 31% this week.\n\nRecommendations:\n1. Trim Sunday's close."
        client_api.labor_cache_put(rid, analysis, text)
        insight_store.put(rid, "labor", "fp-new", text)
        return text, analysis

    monkeypatch.setattr(client_api, "labor_read_text", fake)
    monkeypatch.setattr(client_api, "labor_analysis_safe", lambda rid: None)
    monkeypatch.setattr(client_api, "_labor_diagnosis_safe", lambda *a, **k: {"available": False})
    return calls


# ── #37: stored reads only ──────────────────────────────────────────────────

def test_a_model_call_under_stored_reads_only_never_leaves():
    sent = []

    class _Client:
        class messages:
            @staticmethod
            def create(**kw):
                sent.append(kw)

    with pytest.raises(ai_utils.StoredReadMiss):
        with ai_utils.stored_reads_only():
            ai_utils.create_with_retry(_Client(), restaurant_id=1, action="labor_insight",
                                       readiness=None, model="claude-haiku-4-5", max_tokens=5,
                                       messages=[{"role": "user", "content": "x"}])
    assert sent == []


def test_a_workflow_run_is_refused_before_it_is_recorded(monkeypatch):
    attempts = []
    monkeypatch.setattr(ai_orchestrator.wf, "policy",
                        lambda *a, **k: pytest.fail("the probe must not read a policy or record a run"))
    with pytest.raises(ai_utils.StoredReadMiss):
        with ai_utils.stored_reads_only():
            ai_orchestrator.generate("labor_insight", 1, lambda route, notes: attempts.append(route))
    assert attempts == []


def test_a_miss_is_not_an_exception_the_read_bodies_swallow():
    """Every read body ends in `except Exception` (its stale / error copy):
    a miss must pass through it."""
    assert not issubclass(ai_utils.StoredReadMiss, Exception)


def test_background_work_never_inherits_stored_reads_only():
    seen = []
    with ai_utils.stored_reads_only():
        run = ai_utils.background_runner(lambda: seen.append(ai_utils._STORED_ONLY.get()))
    run()
    assert seen == [False]


# ── #37: the serve() contract ───────────────────────────────────────────────

def _serve(rid, store, refresh_job=None, refresh_result=({"ok": True}, 200)):
    def render():
        if store.get("read"):
            return {"insight": store["read"]}, 200
        ai_utils.refuse_if_stored_only("x")
        return {"insight": "written on the request thread"}, 200

    def refresh():
        if refresh_result[1] == 200 and not refresh_result[0].get("stale"):
            store["read"] = "fresh read"
        return refresh_result

    def stale(note):
        if not store.get("old"):
            return None
        return {"insight": store["old"], "stale_note": note("10/6/26")}, 200

    return insight_refresh.serve(
        "test", rid, render=render, refresh=refresh, stale=stale,
        pending=lambda job: ({"insight": insight_refresh.PENDING_MESSAGE}, 200),
        failed=lambda err, st: ({"insight": err or "unavailable", "error": err or "unavailable"}, st),
        refresh_job=refresh_job)


def test_a_cold_read_answers_pending_and_starts_one_refresh(db, held):
    rid = _rid(db)
    store = {}
    payload, status = _serve(rid, store)
    assert status == 200 and payload["pending"] is True and payload["status"] == "pending"
    assert payload["insight"] == insight_refresh.PENDING_MESSAGE and payload["refresh_job"]
    assert len(held.jobs) == 1


def test_a_stale_read_is_served_at_once_with_its_age_while_a_new_one_is_written(db, held):
    rid = _rid(db)
    store = {"old": "Monday's read"}
    payload, status = _serve(rid, store)
    assert status == 200 and payload["insight"] == "Monday's read"
    assert payload["stale"] is True and payload["refreshing"] is True and payload["pending"] is False
    assert payload["stale_note"] == "From a read on 10/6/26 — Cavnar AI is writing a newer one."
    assert len(held.jobs) == 1


def test_only_one_refresh_runs_at_a_time(db, held):
    rid = _rid(db)
    store = {}
    a, _ = _serve(rid, store)
    b, _ = _serve(rid, store)
    assert a["refresh_job"] == b["refresh_job"], "a second open joins the read being written"
    assert len(held.jobs) == 1
    # Polling a pending job renders nothing and starts nothing.
    c, _ = _serve(rid, store, refresh_job=a["refresh_job"])
    assert c["pending"] is True and len(held.jobs) == 1


def test_a_written_read_is_served_on_the_next_poll(db, held):
    rid = _rid(db)
    store = {}
    first, _ = _serve(rid, store)
    held.run_all()
    payload, status = _serve(rid, store, refresh_job=first["refresh_job"])
    assert status == 200 and payload == {"insight": "fresh read"}


def test_a_refresh_that_failed_is_said_once_and_never_retried_on_each_poll(db, held):
    rid = _rid(db)
    store = {"old": "Monday's read"}
    failing = ({"insight": "Cavnar AI is paused", "error": "Cavnar AI is paused"}, 429)
    first, _ = _serve(rid, store, refresh_result=failing)
    held.run_all()
    payload, status = _serve(rid, store, refresh_job=first["refresh_job"], refresh_result=failing)
    assert payload["insight"] == "Monday's read" and payload["stale"] is True and payload["refreshing"] is False
    assert "couldn't be written" in payload["stale_note"]
    assert held.jobs == [], "no second job on a poll of a failed one"
    # With no read to fall back on, the route's own error and status.
    store.pop("old")
    payload, status = _serve(rid, store, refresh_job=first["refresh_job"], refresh_result=failing)
    assert status == 429 and payload["error"] == "Cavnar AI is paused"


# ── #37: the routes ─────────────────────────────────────────────────────────

def test_the_synchronous_route_stays_for_a_client_that_does_not_ask(client, db, held, fake_labor_read):
    rid = _rid(db)
    token = _token(client, db, rid)
    data = client.get("/mobile/api/labor/insight", headers=_h(token)).get_json()
    assert data["ok"] is True and data["insight"].startswith("Labor ran 31%")
    assert fake_labor_read["model"] == 1 and held.jobs == []


def test_mobile_labor_cold_read_is_pending_then_the_written_read(client, db, held, fake_labor_read):
    rid = _rid(db)
    token = _token(client, db, rid)
    data = client.get("/mobile/api/labor/insight?async=1", headers=_h(token)).get_json()
    assert data["ok"] is True and data["pending"] is True and data["status"] == "pending"
    # The read's own fields carry the placeholder, so an older build decodes it.
    assert data["insight_intro"] == insight_refresh.PENDING_MESSAGE
    assert data["insight_recommendations"] == [] and data["refresh_job"]
    assert fake_labor_read["model"] == 0, "the request thread never called the model"
    held.run_all()
    assert fake_labor_read["model"] == 1
    done = client.get(f"/mobile/api/labor/insight?async=1&refresh_job={data['refresh_job']}",
                      headers=_h(token)).get_json()
    assert done["ok"] is True and done["insight"].startswith("Labor ran 31%")
    assert not done.get("pending") and not done.get("refreshing")


def test_mobile_labor_stale_read_is_the_stored_one_after_a_deploy(client, db, held, fake_labor_read):
    """The process cache is empty (a deploy): the stored read answers, with
    its age, while the new one is written."""
    rid = _rid(db)
    token = _token(client, db, rid)
    insight_store.put(rid, "labor", "fp-old", "Labor ran 29% last week.")
    data = client.get("/mobile/api/labor/insight?async=1", headers=_h(token)).get_json()
    assert data["insight"] == "Labor ran 29% last week." and data["insight_intro"]
    assert data["stale"] is True and data["refreshing"] is True and data["as_of"]
    assert "writing a newer one" in data["stale_note"]
    assert len(held.jobs) == 1 and fake_labor_read["model"] == 0


def test_web_labor_twin_answers_the_same_way(db, held, fake_labor_read, monkeypatch):
    rid = _rid(db)
    app = Flask(__name__)
    with app.test_request_context("/api/labor-insight?async=1"):
        payload, status = insight_refresh.serve(
            "labor", rid, render=lambda: client_api._labor_insight_web(rid, 1),
            refresh=lambda: client_api.labor_refresh(rid), stale=lambda note: None,
            pending=lambda job: ({"insight": insight_refresh.PENDING_MESSAGE}, 200),
            failed=lambda err, st: ({"insight": err}, st))
    assert payload["pending"] is True and len(held.jobs) == 1
    held.run_all()
    assert client_api.labor_cached_read(rid, None).startswith("Labor ran 31%")


# ── #56: compression ────────────────────────────────────────────────────────

@pytest.fixture
def bare():
    app = Flask(__name__)
    http_layer.register(app)

    @app.route("/mobile/api/home")
    def home():
        g.cavnar_current_user = {"id": int(_who.get("id", 1)), "restaurant_id": int(_who.get("rid", 4))}
        return jsonify(ok=True, rows=["row %d" % i for i in range(_who.get("rows", 300))])

    @app.route("/mobile/api/reviews")
    def reviews():
        g.cavnar_current_user = {"id": 1, "restaurant_id": 4}
        return jsonify(ok=True, reviews=["r%d" % i for i in range(300)])

    @app.route("/mobile/api/labor/insight")
    def not_tagged():
        g.cavnar_current_user = {"id": 1, "restaurant_id": 4}
        return jsonify(ok=True, insight="x" * 2000)

    @app.route("/mobile/api/ask/stream")
    def stream():
        def gen():
            yield "data: " + json.dumps({"type": "progress", "label": "x" * 2000}) + "\n\n"
        return Response(gen(), mimetype="text/event-stream")

    return app.test_client()


_who = {}


@pytest.fixture(autouse=True)
def _reset_who():
    _who.clear()
    yield
    _who.clear()


def test_json_over_a_kilobyte_is_gzipped_when_the_client_takes_it(bare):
    resp = bare.get("/mobile/api/labor/insight", headers={"Accept-Encoding": "gzip"})
    assert resp.headers["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(resp.data))["ok"] is True
    assert "Accept-Encoding" in resp.headers["Vary"]


def test_a_stream_is_never_compressed_or_buffered(bare):
    resp = bare.get("/mobile/api/ask/stream", headers={"Accept-Encoding": "gzip"})
    assert "Content-Encoding" not in resp.headers
    assert resp.is_streamed and "ETag" not in resp.headers
    assert b"progress" in resp.data


# ── #56: ETag / 304 ─────────────────────────────────────────────────────────

def test_home_answers_with_an_etag_and_a_bodiless_304_when_unchanged(bare):
    first = bare.get("/mobile/api/home", headers={"Accept-Encoding": "gzip"})
    tag = first.headers["ETag"]
    assert tag.startswith('W/"') and "Authorization" in first.headers["Vary"]
    assert first.headers["Cache-Control"] == "no-store", "never put in a shared or disk cache"
    again = bare.get("/mobile/api/home", headers={"If-None-Match": tag, "Accept-Encoding": "gzip"})
    assert again.status_code == 304 and again.data == b""
    assert again.headers["ETag"] == tag and "Content-Encoding" not in again.headers
    assert again.headers["Cache-Control"] == "no-store"


def test_a_changed_body_or_another_login_is_a_full_answer(bare):
    tag = bare.get("/mobile/api/home").headers["ETag"]
    _who["rows"] = 301
    assert bare.get("/mobile/api/home", headers={"If-None-Match": tag}).status_code == 200
    _who.clear()
    _who["id"] = 2
    other = bare.get("/mobile/api/home", headers={"If-None-Match": tag})
    assert other.status_code == 200 and other.headers["ETag"] != tag
    _who.clear()
    _who["rid"] = 5
    assert bare.get("/mobile/api/home", headers={"If-None-Match": tag}).status_code == 200


def test_only_the_first_page_of_the_inbox_and_the_named_reads_are_tagged(bare):
    assert "ETag" in bare.get("/mobile/api/reviews").headers
    assert "ETag" in bare.get("/mobile/api/reviews?offset=0&filter=all").headers
    assert "ETag" not in bare.get("/mobile/api/reviews?offset=50").headers
    assert "ETag" not in bare.get("/mobile/api/labor/insight").headers


def test_the_tagged_paths_are_home_labor_reviews_and_the_daily_report():
    for path in ("/mobile/api/home", "/mobile/api/labor", "/mobile/api/reviews", "/mobile/api/dsr",
                 "/mobile/api/dsr/2026-10-06"):
        assert any(p.match(path) for p in http_layer._ETAG_PATHS), path
    for path in ("/mobile/api/dsr/2026-10-06/status", "/api/home/brief", "/mobile/api/labor/insight",
                 "/mobile/api/reviews/12"):
        assert not any(p.match(path) for p in http_layer._ETAG_PATHS), path


def test_a_request_with_no_login_is_never_tagged():
    app = Flask(__name__)
    http_layer.register(app)

    @app.route("/mobile/api/home")
    def home():
        return jsonify(ok=False, error="x" * 2000)

    resp = app.test_client().get("/mobile/api/home")
    assert "ETag" not in resp.headers


def test_the_real_inbox_route_answers_304_to_its_own_tag(client, db):
    rid = _rid(db)
    token = _token(client, db, rid)
    first = client.get("/mobile/api/reviews", headers=_h(token))
    assert first.status_code == 200 and first.headers.get("ETag")
    again = client.get("/mobile/api/reviews", headers=_h(token, **{"If-None-Match": first.headers["ETag"]}))
    assert again.status_code == 304 and again.data == b""


# ── every client asks (#37) ─────────────────────────────────────────────────

DASH = open("templates/dashboard.html", encoding="utf-8").read()


def _between(src, a, b):
    i = src.index(a)
    return src[i:src.index(b, i)]


def test_the_web_asks_every_module_read_stale_while_refresh():
    for route in ("/api/review-insight", "/api/labor-insight", "/api/mkt-insight"):
        assert f"insightSwr('{route}', function(d, interim){{" in DASH, route
        assert f"fetch('{route}')" not in DASH, route


def test_the_web_poll_shows_the_older_read_then_the_new_one_in_node():
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    fn = _between(DASH, "function insightSwr(url, onAnswer, onFail){", "window.insightSwr = insightSwr;")
    js = ("var AI_POLL_STEPS=[1500,3000,5000], calls = [], urls = [], window = {};\n"
          "function setTimeout(f, ms) { f(); }\n"
          "function apiJson(r) { return Promise.resolve(r); }\n"
          "var answers = [{ok: true, pending: true, refreshing: true, refresh_job: 'j1', insight: 'placeholder'},\n"
          "  {ok: true, stale: true, refreshing: true, refresh_job: 'j1', insight: 'Monday'},\n"
          "  {ok: true, insight: 'Today'}];\n"
          "function fetch(url) { urls.push(url); return Promise.resolve(answers.shift()); }\n"
          + fn +
          "\ninsightSwr('/api/labor-insight', function(d, interim) { calls.push([d.insight, interim]);"
          " if (!interim) console.log(JSON.stringify({calls: calls, urls: urls})); });\n")
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip())
    assert got["calls"] == [["Monday", True], ["Today", False]], "the placeholder is never shown"
    assert got["urls"] == ["/api/labor-insight?async=1", "/api/labor-insight?async=1&refresh_job=j1",
                           "/api/labor-insight?async=1&refresh_job=j1"]


def test_the_phone_asks_every_module_read_stale_while_refresh_and_follows_it():
    root = "ios/CavnarAI/CavnarAI/"
    api = open(root + "Core/APIClient.swift", encoding="utf-8").read()
    assert 'var query = ["async": "1"]' in api and 'query["refresh_job"] = refreshJob' in api
    for f, path in (("Features/Labor/LaborAnalyticsViewModel.swift", "/mobile/api/labor/insight"),
                    ("Features/Reviews/ReviewsAnalyticsViewModel.swift", "/mobile/api/reviews/insight"),
                    ("Features/Marketing/MarketingAnalyticsViewModel.swift", "/mobile/api/marketing/insight")):
        src = open(root + f, encoding="utf-8").read()
        assert f'static let insightPath = "{path}"' in src, f
        assert "client.sendInsight(Self.insightPath)" in src and "InsightRefresh.follow(Self.insightPath" in src, f


def test_the_mobile_catch_all_404_says_the_route_is_missing():
    """ReviewById falls back to paging the inbox only for a route the
    server does not have — never for the route's own 'not found'."""
    src = open("hosted_dashboard.py", encoding="utf-8").read()
    handler = _between(src, "def page_not_found(e):", "if _json_api_path():")
    assert "unknown_route=True" in handler
    swift = open("ios/CavnarAI/CavnarAI/Features/Reviews/ReviewByIdView.swift", encoding="utf-8").read()
    assert "where error.status == 404" in swift and "Self.isMissingRoute(error)" in swift
    assert "try? await client.send(\"/mobile/api/reviews/\\(reviewID)\"" not in swift
