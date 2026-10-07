"""Slow owner AI calls as jobs the client polls (AI cost audit 10/7/26 #57).

The invoice read, the recipe-card read and the Campaign Studio's three
drafts held a request thread for the whole model call. A client that sends
`async` now gets a job id at once (ai_async.start on the owner AI pool) and
polls /api/ai-jobs/<id> (and its mobile twin) for exactly the answer the
synchronous route would have given, status code included. A client that
does not send it — an iOS build already in the field — is answered as
before. What is pinned here: the flag, the join, the answer and its status,
who may read it, a job the store failed, the routes both ways, the rate
limit counted once, and that every client in the repository asks.
"""
import io
import json
import time
import types

import pytest
from flask import Flask, request

import ai_async
import ai_utils
import auth
import client_api
import invoices
import mobile_api
import models
import ops
import recipes
from models import Restaurant, create_restaurant

NS = types.SimpleNamespace


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, invoices, recipes, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: False)
    return db_path


class _Now:
    """An executor that runs the job at once, on this thread."""

    def submit(self, fn, *a, **k):
        fn(*a, **k)


class _Held:
    """An executor that keeps the job, so it stays pending."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *a, **k):
        self.jobs.append((fn, a, k))


@pytest.fixture
def now(monkeypatch):
    monkeypatch.setattr(ai_async, "_executor", lambda: _Now())


def _rid(db, **kw):
    kw.setdefault("module_inventory", 1)
    kw.setdefault("module_marketing", 1)
    return create_restaurant(Restaurant(name="Jobs Co", owner_email="o@x.test", **kw), db_path=db)


# ── the flag ────────────────────────────────────────────────────────────────

def test_async_is_asked_by_a_query_arg_a_form_field_or_the_json_body():
    app = Flask(__name__)
    for kw in ({"query_string": {"async": "1"}}, {"data": {"async": "true"}}, {"json": {"async": True}},
               {"json": {"async": "yes", "prompt": "x"}}):
        with app.test_request_context("/x", method="POST", **kw):
            assert ai_async.wants_async(request), kw
    for kw in ({}, {"json": {"prompt": "x"}}, {"json": {"async": False}}, {"query_string": {"async": "0"}}):
        with app.test_request_context("/x", method="POST", **kw):
            assert not ai_async.wants_async(request), kw


# ── the job ─────────────────────────────────────────────────────────────────

def test_a_job_answers_exactly_what_the_route_would_have_with_its_status(db, now):
    rid = _rid(db)
    job, joined = ai_async.start("campaign_text", rid, {"prompt": "fill Tuesday"},
                                 lambda: ({"ok": False, "error": "Cavnar AI didn't use that draft"}, 422), by_user=7)
    assert joined is False
    payload, status = ai_async.result(job, rid, user={"id": 7})
    assert status == 422 and payload["error"] == "Cavnar AI didn't use that draft"
    assert payload["status"] == "done" and payload["job_id"] == job


def test_a_pending_job_says_so_and_the_same_request_joins_it(db, monkeypatch):
    held = _Held()
    monkeypatch.setattr(ai_async, "_executor", lambda: held)
    rid = _rid(db)
    a, joined_a = ai_async.start("invoice_scan", rid, {"sha": "abc"}, lambda: ({"ok": True}, 200), by_user=7)
    b, joined_b = ai_async.start("invoice_scan", rid, {"sha": "abc"}, lambda: ({"ok": True}, 200), by_user=7)
    c, joined_c = ai_async.start("invoice_scan", rid, {"sha": "def"}, lambda: ({"ok": True}, 200), by_user=7)
    assert (b, joined_b) == (a, True), "the same file pressed again joins the read"
    assert c != a and joined_c is False, "another invoice is its own read"
    assert len(held.jobs) == 2
    payload, status = ai_async.result(a, rid, user={"id": 7})
    assert status == 200 and payload["status"] == "pending" and payload["ok"] is True
    fn, args, kw = held.jobs[0]
    fn(*args, **kw)
    assert ai_async.result(a, rid, user={"id": 7})[0]["status"] == "done"


def test_only_the_login_that_started_a_job_or_an_admin_reads_it(db, now):
    rid = _rid(db)
    other = _rid(db)
    job, _ = ai_async.start("invoice_scan", rid, {"sha": "x"}, lambda: ({"ok": True, "invoice": {}}, 200), by_user=7)
    assert ai_async.result(job, rid, user={"id": 8})[1] == 404
    assert ai_async.result(job, other, user={"id": 7})[1] == 404, "another restaurant's job is not found"
    assert ai_async.result(job, rid, user={"id": 99, "is_admin": 1})[1] == 200
    assert ai_async.result("no-such-job", rid, user={"id": 7})[1] == 404


def test_a_job_the_store_failed_says_so_in_its_own_words(db, monkeypatch):
    held = _Held()
    monkeypatch.setattr(ai_async, "_executor", lambda: held)
    rid = _rid(db)
    job, _ = ai_async.start("recipe_scan", rid, {"sha": "y"}, lambda: ({"ok": True}, 200), by_user=7)
    # The boot sweep (or a poll past the deadline) fails it with the
    # schedule's sentence: never shown for an invoice or a draft.
    ops.finish_async_job(job, "error", {"ok": False, "error": "Generation was interrupted — please try again."})
    payload, status = ai_async.result(job, rid, user={"id": 7})
    assert status == 503 and payload["error"] == ai_async.DEAD_JOB_MESSAGE and "schedule" not in payload["error"]
    # The job that finishes later cannot overwrite that verdict (P-22).
    fn, args, kw = held.jobs[0]
    fn(*args, **kw)
    assert ai_async.result(job, rid, user={"id": 7})[1] == 503


def test_a_job_that_raises_is_a_500_in_words_and_captured(db, now, monkeypatch):
    rid = _rid(db)
    seen = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: seen.append((type(e).__name__, k.get("job"))))

    def boom():
        raise RuntimeError("provider exploded")
    job, _ = ai_async.start("campaign_email", rid, {"prompt": "x"}, boom, by_user=7)
    payload, status = ai_async.result(job, rid, user={"id": 7})
    assert status == 500 and payload["error"] == ai_async.FAILED_MESSAGE and "exploded" not in payload["error"]
    assert ("RuntimeError", "ai_async_campaign_email") in seen


def test_the_job_runs_off_the_request_thread_as_the_person_who_asked(db):
    """The pool thread has no request: who asked is taken at start
    (ai_utils.attributed), so the model calls are the owner's (#148)."""
    rid = _rid(db)
    seen = {}

    def work():
        import threading
        seen["thread"] = threading.current_thread().name
        seen["attr"] = ai_utils._attribution()
        seen["on_request"] = ai_utils.on_request_thread()
        return {"ok": True}, 200
    with ai_utils.ai_context(trigger="owner", actor_user_id=7):
        job, _ = ai_async.start("campaign_post", rid, {"topic": "x"}, work, by_user=7)
    for _ in range(100):
        if ai_async.result(job, rid, user={"id": 7})[0].get("status") == "done":
            break
        time.sleep(0.05)
    assert seen["thread"].startswith("owner-ai")
    assert seen["attr"][0] == "owner" and seen["attr"][1] == 7
    assert seen["on_request"] is False, "the interactive slot guard does not apply off a request"


# ── the routes ──────────────────────────────────────────────────────────────

@pytest.fixture
def strategy_client(db):
    from strategy_routes import strategy_bp, strategy_mobile_bp
    app = Flask(__name__)
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    return app.test_client()


def _as(monkeypatch, rid, uid=5):
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": uid, "restaurant_id": rid, "is_admin": 0,
                                                           "role": "client", "username": "u", "email": "u@x.com"})


def _upload(client, path, async_=True, key=None):
    data = {"file": (io.BytesIO(b"\x89PNG invoice"), "invoice.png", "image/png")}
    if async_:
        data["async"] = "1"
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(path, data=data, content_type="multipart/form-data", headers=headers)


def test_an_invoice_scan_asked_async_is_a_job_and_the_poll_is_the_old_answer(strategy_client, db, now, monkeypatch):
    rid = _rid(db)
    _as(monkeypatch, rid)
    monkeypatch.setattr(invoices, "scan", lambda r, data, mt, user_id=None: {"id": 3, "lines": [], "duplicate": False})
    r = _upload(strategy_client, "/api/food-cost/invoices")
    assert r.status_code == 202
    start = r.get_json()
    assert start["ok"] and start["async"] and start["job_id"] and start["wait_seconds"] == ai_async.JOB_SECONDS["invoice_scan"]
    p = strategy_client.get(f"/api/ai-jobs/{start['job_id']}")
    assert p.status_code == 200 and p.get_json()["invoice"]["id"] == 3 and p.get_json()["status"] == "done"


def test_an_invoice_scan_without_the_flag_is_answered_as_before(strategy_client, db, monkeypatch):
    rid = _rid(db)
    _as(monkeypatch, rid)
    monkeypatch.setattr(invoices, "scan", lambda r, data, mt, user_id=None: {"id": 4, "lines": []})
    monkeypatch.setattr(ai_async, "start", lambda *a, **k: pytest.fail("an old app's scan is never a job"))
    r = _upload(strategy_client, "/api/food-cost/invoices", async_=False)
    assert r.status_code == 200 and r.get_json()["invoice"]["id"] == 4 and "job_id" not in r.get_json()


def test_an_async_upload_refuses_a_bad_file_before_any_job(strategy_client, db, monkeypatch):
    rid = _rid(db)
    _as(monkeypatch, rid)
    monkeypatch.setattr(ai_async, "start", lambda *a, **k: pytest.fail("no job for a file that cannot be read"))
    r = strategy_client.post("/api/food-cost/invoices", content_type="multipart/form-data",
                             data={"file": (io.BytesIO(b"x"), "a.heic", "image/heic"), "async": "1"})
    assert r.status_code == 400


def test_an_async_scan_is_not_remembered_by_its_idempotency_key(strategy_client, db, now, monkeypatch):
    """A stored job id would hand a failed read back for a week: the job
    store joins a repeat while it runs, and invoices.scan's own sha check
    answers one that has finished."""
    rid = _rid(db)
    _as(monkeypatch, rid)
    calls = []
    monkeypatch.setattr(invoices, "scan", lambda r, data, mt, user_id=None: calls.append(1) or {"id": 5, "lines": []})
    a = _upload(strategy_client, "/api/food-cost/invoices", key="k1").get_json()
    b = _upload(strategy_client, "/api/food-cost/invoices", key="k1").get_json()
    assert a["job_id"] != b["job_id"] and len(calls) == 2
    c = models.get_conn(db)
    assert c.execute("SELECT COUNT(*) FROM idempotent_responses").fetchone()[0] == 0
    c.close()


def test_a_recipe_card_asked_async_is_a_job(strategy_client, db, now, monkeypatch):
    rid = _rid(db)
    _as(monkeypatch, rid)
    monkeypatch.setattr(recipes, "extract_from_image", lambda r, data, mt, user_id=None: {"id": 9, "lines": []})
    start = _upload(strategy_client, "/api/food-cost/recipes/scan").get_json()
    p = strategy_client.get(f"/api/ai-jobs/{start['job_id']}").get_json()
    assert p["ok"] and p["draft"]["id"] == 9


@pytest.fixture
def mobile_client(db, monkeypatch):
    from auth import create_user, init_auth
    import auth_routes
    init_auth(db_path=db)
    real = models.get_conn
    for mod in (auth, auth_routes, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db), raising=False)
    auth_routes._login_attempts.clear()
    from strategy_routes import strategy_mobile_bp
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    app.register_blueprint(strategy_mobile_bp)
    app.register_blueprint(client_api.client_bp)
    rid = _rid(db)
    create_user(rid, "owner", "owner@x.test", "correct-horse", db_path=db)
    c = app.test_client()
    tok = c.post("/mobile/api/login", json={"username": "owner", "password": "correct-horse"}).get_json()["token"]
    c.h = {"Authorization": f"Bearer {tok}"}
    c.rid = rid
    return c


def test_the_studio_text_draft_asked_async_polls_to_the_draft(mobile_client, now, monkeypatch):
    import guest_marketing
    monkeypatch.setattr(guest_marketing, "draft_campaign_message", lambda r, **k: "See you Tuesday")
    r = mobile_client.post("/mobile/api/guest-campaign/draft", json={"prompt": "Fill Tuesday", "async": True},
                           headers=mobile_client.h)
    assert r.status_code == 202
    p = mobile_client.get(f"/mobile/api/ai-jobs/{r.get_json()['job_id']}", headers=mobile_client.h)
    assert p.status_code == 200 and p.get_json()["message"] == "See you Tuesday" and p.get_json()["segment"]


def test_a_refused_draft_comes_back_through_the_poll_with_its_422(mobile_client, now, monkeypatch):
    import guest_email

    def refuse(r, goal="", topic=""):
        raise ValueError("newsletter copy rejected: award or ranking claim ('famous')")
    monkeypatch.setattr(guest_email, "draft_newsletter", refuse)
    r = mobile_client.post("/mobile/api/guest-newsletter/draft", json={"prompt": "Fill Tuesday", "async": True},
                           headers=mobile_client.h)
    p = mobile_client.get(f"/mobile/api/ai-jobs/{r.get_json()['job_id']}", headers=mobile_client.h)
    assert p.status_code == 422 and "award or ranking claim" in p.get_json()["error"]


def test_a_studio_draft_without_the_flag_is_answered_as_before(mobile_client, monkeypatch):
    import guest_marketing
    monkeypatch.setattr(guest_marketing, "draft_campaign_message", lambda r, **k: "Written now")
    r = mobile_client.post("/mobile/api/guest-campaign/draft", json={"prompt": "Fill Tuesday"},
                           headers=mobile_client.h)
    assert r.status_code == 200 and r.get_json()["message"] == "Written now" and "job_id" not in r.get_json()


def test_the_post_draft_counts_its_rate_limit_once(mobile_client, now, monkeypatch):
    import marketing
    counted = []
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda key, **k: counted.append(key) or False)
    monkeypatch.setattr(marketing, "generate_content", lambda *a, **k: "A post")
    monkeypatch.setattr(client_api, "_post_tags_safe", lambda *a, **k: {})
    r = mobile_client.post("/mobile/api/marketing/generate-content", json={"topic": "Tuesday", "async": True},
                           headers=mobile_client.h)
    p = mobile_client.get(f"/mobile/api/ai-jobs/{r.get_json()['job_id']}", headers=mobile_client.h).get_json()
    assert p["ok"] and p["content"] == "A post"
    assert [k for k in counted if k.startswith("gencontent:")] == [f"gencontent:{mobile_client.rid}"]


# ── every client asks ───────────────────────────────────────────────────────

SRC = open("templates/dashboard.html", encoding="utf-8").read()


def _between(a, b):
    i = SRC.index(a)
    return SRC[i:SRC.index(b, i)]


def test_the_web_asks_for_jobs_and_polls_them():
    helper = _between("function aiJobAwait(d, opts){", "window.aiJobAwait = aiJobAwait;")
    assert "'/api/ai-jobs/' + encodeURIComponent(d.job_id)" in helper and "r.status === 'pending'" in helper
    assert "if (!d || !d.job_id || d.ok === false) return Promise.resolve(d);" in helper   # an old answer passes
    scan = _between("window.fc2InvoiceScan=function(inp){", "window.fc2LoadPendingInvoices")
    assert "fd.append('async','1')" in scan and ".then(apiJson).then(aiJobAwait)" in scan
    draft = _between("function cpDraft(k, prompt, plan) {", "window.cpRewrite = function(k) {")
    assert "body['async'] = true;" in draft and "_cpPost(url, body).then(aiJobAwait)" in draft


def test_the_web_poll_resolves_to_the_jobs_answer_in_node():
    """aiJobAwait run for real: an answer with no job passes through; a job
    is polled while pending and resolves with what the route answered."""
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    fn = _between("function aiJobAwait(d, opts){", "window.aiJobAwait = aiJobAwait;")
    js = ("var polls = 0, window = {};\n"
          "function setTimeout(f, ms) { f(); }\n"
          "function apiJson(r) { return Promise.resolve(r); }\n"
          "function fetch(url) { polls++; return Promise.resolve(polls < 3 ? {ok: true, status: 'pending'}"
          " : {ok: false, status: 'done', error: 'refused', url: url}); }\n"
          + fn +
          "\nPromise.all([aiJobAwait({ok: true, content: 'now'}), aiJobAwait({ok: true, job_id: 'j1'})])"
          ".then(function(r) { console.log(JSON.stringify({direct: r[0], job: r[1], polls: polls})); });\n")
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip())
    assert got["direct"] == {"ok": True, "content": "now"}
    assert got["job"]["error"] == "refused" and got["job"]["url"] == "/api/ai-jobs/j1" and got["polls"] == 3


def test_the_phone_asks_for_jobs_and_polls_them():
    root = "ios/CavnarAI/CavnarAI/"
    api = open(root + "Core/APIClient.swift", encoding="utf-8").read()
    assert '"/mobile/api/ai-jobs/\\(jobId)"' in api and "func resolveAIJob" in api and 'case jobId = "job_id"' in api
    for f in ("Features/FoodCost/InvoiceScanSheet.swift", "Features/FoodCost/RecipeDraftsSheet.swift"):
        src = open(root + f, encoding="utf-8").read()
        assert 'query: ["async": "1"]' in src and "client.resolveAIJob(started)" in src, f
    text = open(root + "Features/Marketing/GuestTextClubViewModel.swift", encoding="utf-8").read()
    assert 'case runAsJob = "async"' in text and "client.resolveAIJob(started)" in text


def test_the_poll_route_has_its_web_and_mobile_twin():
    from strategy_routes import strategy_bp, strategy_mobile_bp
    app = Flask(__name__)
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    rules = {str(r) for r in app.url_map.iter_rules()}
    assert "/api/ai-jobs/<job_id>" in rules and "/mobile/api/ai-jobs/<job_id>" in rules
