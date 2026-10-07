"""AI orchestration phase 1 (observation) and phase 6 (learning), and the
console that shows them (design owner-approved 10/7/26; AI cost audit
10/7/26 #74, #95, #96, W4):

  * create_with_retry hands every request it is about to send to
    ai_orchestrator.keep_request — kept only for a sampled run, never able to
    break the call — and records the effort the call went out at (#74), which
    the daily rollup keeps per (action, model) as calls by effort.
  * a re-check of an unchanged stored text (drafter.recheck_draft_flags, at
    every boot, no model call) is not written to ai_validation_log again, and
    the daily validation rollup counts distinct texts (W4).
  * the learner and the shadow replays are scheduled jobs, registered and
    bounded; a shadow replay goes through Message Batches on the next cheaper
    tier, its landing records a run shadow_of production with the batch
    ledger's cost, and both texts are scored with the same rubric.
  * Engineering → AI routes and AI costs: every write needs the step-up and
    is audited with before and after; overrides are validated by the
    registry; the month's cost per restaurant and workflow, and the ledger
    against the entered invoices.
"""
import json
import os
import re
import sqlite3
import sys

import pytest
from flask import Flask

import admin_ops
import admin_routes
import ai_batches
import ai_learning
import ai_orchestrator as orch
import ai_reviewer
import ai_utils
import ai_workflows as wf
import auth
import jobs_registry
import models
import scheduler
from auth import create_session, create_user, init_auth
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSRF = "orch-console-csrf"


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    # Every connection to this test's file, whatever path a module bound at
    # import (auth's defaults hold the real DB_PATH).
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    for mod in (auth, admin_routes):
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    orch._OVERRIDES.clear()
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 0.0)
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: None)
    ai_utils._LAST_CALL.set(None)
    return db_path


def _q(db, sql, args=()):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _x(db, sql, args=()):
    c = sqlite3.connect(db)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


class _Usage:
    def __init__(self, i=1000, o=200):
        self.input_tokens, self.output_tokens = i, o
        self.cache_creation_input_tokens = self.cache_read_input_tokens = 0


class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Msg:
    def __init__(self, text="Labor ran 31% on Tuesday, two points over target.", stop="end_turn"):
        self.content = [_Block(text)]
        self.usage = _Usage()
        self.stop_reason = stop
        self.id = "msg_test"


class _Client:
    def __init__(self, text="Labor ran 31% on Tuesday, two points over target."):
        self.messages = self
        self.sent = []
        self.text = text

    def create(self, **kwargs):
        self.sent.append(kwargs)
        return _Msg(self.text)


# ── #74 and the keep_request hook ──────────────────────────────────────────

def test_the_effort_a_call_went_out_at_is_on_its_ledger_row_and_in_the_rollup(db):
    ai_utils.create_with_retry(_Client(), model=ai_utils.SONNET, max_tokens=50, restaurant_id=3,
                               action="labor_insight", output_config={"effort": "high"},
                               messages=[{"role": "user", "content": "x"}])
    # A model whose thinking is always on gets the default effort, recorded.
    ai_utils.create_with_retry(_Client(), model=ai_utils.OPUS_55, max_tokens=50, restaurant_id=3,
                               action="labor_insight", messages=[{"role": "user", "content": "x"}])
    ai_utils.create_with_retry(_Client(), model=ai_utils.HAIKU, max_tokens=50, restaurant_id=3,
                               action="labor_insight", messages=[{"role": "user", "content": "x"}])
    rows = _q(db, "SELECT model, effort FROM ai_usage WHERE action='labor_insight' ORDER BY id")
    assert [r["effort"] for r in rows] == ["high", ai_utils.ALWAYS_THINKING_DEFAULT_EFFORT, None]
    conn = models.get_conn(db)
    try:
        day = conn.execute("SELECT date(created_at) FROM ai_usage LIMIT 1").fetchone()[0]
        ai_utils._roll_usage_day(conn, day)
        conn.commit()
    finally:
        conn.close()
    by_model = {r["model"]: json.loads(r["efforts_json"]) for r in _q(
        db, "SELECT model, efforts_json FROM ai_usage_daily WHERE action='labor_insight'")}
    assert by_model[ai_utils.SONNET] == {"high": 1} and by_model[ai_utils.HAIKU] == {"": 1}
    # The rollup's key is unchanged: one row per (day, restaurant, vendor, action, model).
    pk = [r["name"] for r in _q(db, "PRAGMA table_info(ai_usage_daily)") if r["pk"]]
    assert set(pk) == {"day", "rid_key", "vendor", "action", "model"}


def test_a_sampled_run_keeps_the_request_create_with_retry_sends(db, monkeypatch):
    client = _Client()

    def attempt(route, notes):
        return ai_utils.create_with_retry(client, restaurant_id=7, action="review_insight", max_tokens=100,
                                          **route.apply({"model": "m", "messages": [
                                              {"role": "user", "content": "call 312-555-0199 re the review"}]}))
    orch.generate("review_insight", 7, attempt=attempt)
    assert orch.kept_requests("review_insight") == [], "an unsampled run keeps nothing"
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 1.0)
    rr = orch.generate("review_insight", 7, attempt=attempt)
    (run_id, rid, req), = orch.kept_requests("review_insight")
    assert run_id == rr.run_id and rid == 7
    assert req["model"] == client.sent[-1]["model"] and "312-555-0199" not in json.dumps(req)
    # Outside any run nothing is kept, and the hook adds no row.
    ai_utils.create_with_retry(client, model="m", max_tokens=5, messages=[{"role": "user", "content": "y"}])
    assert len(_q(db, "SELECT 1 FROM ai_run_requests")) == 1


def test_the_hook_never_breaks_the_call(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store down")
    monkeypatch.setattr(orch, "keep_request", boom)
    msg = ai_utils.create_with_retry(_Client(), model="m", max_tokens=5, messages=[{"role": "user", "content": "y"}])
    assert ai_utils.extract_text(msg)
    src = open(os.path.join(ROOT, "ai_utils.py"), encoding="utf-8").read()
    body = src[src.index("def create_with_retry("):src.index("def with_data_state(")]
    # After every gate (readiness, deadline, budget, breaker, the slot), once, before the send loop.
    assert body.index("_orch.keep_request(kwargs)") > body.index("acquire_interactive_slot()")
    assert body.index("_orch.keep_request(kwargs)") < body.index("while True:")


# ── W4: a re-check of an unchanged text is not counted again ───────────────

def test_a_recheck_of_an_unchanged_text_writes_no_second_row(db):
    args = dict(restaurant_id=42, surface="reply_public", action="recheck_flag", verdict="refuse",
                rules=["PUB3"], text_hash="abc123", mode="enforce", version="v9")
    ai_utils.log_validation(**args)
    ai_utils.log_validation(**args)
    assert len(_q(db, "SELECT 1 FROM ai_validation_log WHERE text_hash='abc123'")) == 1
    # A changed verdict or rules version is what a re-check exists to catch.
    ai_utils.log_validation(**dict(args, verdict="pass", rules=[]))
    ai_utils.log_validation(**dict(args, version="v10"))
    # A fresh model answer (a call behind it) always counts.
    ai_utils.log_validation(**dict(args, call_id="call-1"))
    assert len(_q(db, "SELECT 1 FROM ai_validation_log WHERE text_hash='abc123'")) == 4
    # Past the window it is written again.
    _x(db, "UPDATE ai_validation_log SET created_at=datetime('now','-30 days')")
    ai_utils.log_validation(**args)
    assert len(_q(db, "SELECT 1 FROM ai_validation_log WHERE text_hash='abc123'")) == 5


def test_the_daily_validation_rollup_counts_distinct_texts(db):
    for h in ("t1", "t1", "t1", "t2"):
        _x(db, "INSERT INTO ai_validation_log (restaurant_id, surface, action, verdict, rules, text_hash, mode, "
               "created_at) VALUES (5,'reply_public','recheck_flag','refuse','[]',?,'enforce','2026-10-01 10:00:00')",
           (h,))
    conn = models.get_conn(db)
    try:
        ai_utils._roll_validation_day(conn, "2026-10-01")
        conn.commit()
    finally:
        conn.close()
    (row,) = _q(db, "SELECT n, n_texts FROM ai_validation_daily WHERE day='2026-10-01'")
    assert row == {"n": 4, "n_texts": 2}


def test_the_boot_recheck_is_regex_only_and_counts_once(db, monkeypatch):
    import drafter
    rid = create_restaurant(Restaurant(name="Recheck Grill", owner_email="o@recheck.test"), db_path=db)
    _x(db, "INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, fetched_at, response_status, "
           "draft_response, draft_needs_review, draft_review_reason, author) VALUES (?, 'google', 'r1', 2, "
           "'Cold food', datetime('now'), 'drafted', 'We will refund you $50 and fire the cook.', 1, 'old', 'Ann')",
       (rid,))

    def no_model(*a, **k):
        raise AssertionError("the flag recheck made a model call")
    monkeypatch.setattr(ai_utils, "create_with_retry", no_model)
    drafter.recheck_draft_flags()
    drafter.recheck_draft_flags()
    rows = _q(db, "SELECT action FROM ai_validation_log WHERE surface='reply_public' AND restaurant_id=?", (rid,))
    assert [r["action"] for r in rows] == ["recheck_flag"], "two boots, one unchanged text: one row"


# ── the learner and the shadow replays are scheduled and bounded ───────────

def test_the_learner_and_the_shadow_replays_are_registered_jobs():
    src = __import__("inspect").getsource(scheduler.scheduler_loop)
    assert 'run_job("ai_route_learning", run_learning)' in src
    assert 'run_in_lane("ai", "ai_shadow_arms", run_shadow_arms)' in src
    lj, sj = jobs_registry.JOBS["ai_route_learning"], jobs_registry.JOBS["ai_shadow_arms"]
    assert lj["target"] == ("ai_learning", "run_learning") and not lj["sends"]
    assert sj["target"] == ("ai_learning", "run_shadow_arms") and sj["lane"] == "ai" and not sj["sends"]
    assert "shadow_arms" in ai_batches.DEFAULT_WORKFLOWS.split(",")


def test_shadow_replays_need_batches_and_stay_inside_the_weekly_cap(db, monkeypatch):
    monkeypatch.setattr(ai_batches, "enabled", lambda w: False)
    out = ai_learning.run_shadow_arms()
    assert out["attempted"] == 0 and out["skipped"] == len(ai_learning.shadow_workflows())
    monkeypatch.setattr(ai_batches, "enabled", lambda w: True)
    monkeypatch.setattr(ai_learning, "SHADOW_WEEKLY_USD", 0.5)
    _x(db, "INSERT INTO ai_usage (restaurant_id, action, model, input_tokens, output_tokens, cost_usd, "
           "correlation_id) VALUES (NULL, 'shadow_arms', 'm', 1, 1, 0.75, 'shadow:review_insight:2026-10-04')")
    out = ai_learning.run_shadow_arms()
    assert out["hit_bound"] and out["attempted"] == 0, "a spent week sends nothing"
    for k in ("attempted", "ok", "failed", "skipped", "hit_bound"):
        assert k in out


def test_only_judgeable_replayable_workflows_get_a_cheaper_candidate():
    pairs = dict(ai_learning.shadow_workflows())
    assert pairs["review_insight"] == "T1" and pairs["dsr_narrative"] == "T1"
    for wfl in pairs:
        assert wfl not in orch.NOT_REPLAYABLE and ai_learning.SHADOW_RUBRICS[wfl] in ai_reviewer.RUBRICS
    assert "labor_insight" not in pairs, "a ladder that starts on T1 has nothing cheaper"
    assert "review_analysis" not in ai_learning.SHADOW_RUBRICS
    with pytest.raises(ValueError):
        ai_learning.shadow_arms("labor_schedule", "T3")


def _production_run(db, monkeypatch, text="Labor ran 31% on Tuesday, two points over target."):
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 1.0)
    client = _Client(text)

    def attempt(route, notes):
        msg = ai_utils.create_with_retry(client, restaurant_id=7, action="review_insight", max_tokens=300,
                                         **route.apply({"model": "m", "messages": [
                                             {"role": "user", "content": "Ratings fell 0.2 this month."}]}))
        return ai_utils.extract_text(msg)
    rr = orch.generate("review_insight", 7, attempt=attempt, subject="reviews:2026-10")
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 0.0)
    return rr


def test_a_shadow_replay_runs_the_cheaper_tier_at_half_price_and_scores_both(db, monkeypatch):
    rr = _production_run(db, monkeypatch)
    sent = []
    monkeypatch.setattr(ai_batches, "enabled", lambda w: True)
    monkeypatch.setattr(ai_batches, "submit", lambda w, items: sent.extend(items) or
                        {it["custom_id"]: ai_batches.SUBMITTED for it in items})
    out = ai_learning.shadow_arms("review_insight", "T1", sample=5, budget_usd=1.0)
    assert out["submitted"] == 1 and out["ok"] == 1
    (item,) = sent
    assert item["request"]["model"] == wf._tier_table()["T1"]["model"]
    assert item["action"] == "shadow_arms" and item["restaurant_id"] is None, "platform spend, never the restaurant's"
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item["custom_id"]) and item["callback"] == "ai_learning:shadow_landed"
    # The answer lands: the collector ledgers it at the batch rate first.
    call_id = "shadowcall1"
    ai_utils.log_ai_usage(None, "shadow_arms", item["request"]["model"], 1000, 100, call_id=call_id, batch=True,
                          correlation_id="shadow:review_insight:2026-10-04")
    batch_cost = _q(db, "SELECT cost_usd FROM ai_usage WHERE call_id=?", (call_id,))[0]["cost_usd"]
    scores = {"Ratings dipped; reply to the two 2-star reviews.": 0.8,
              "Labor ran 31% on Tuesday, two points over target.": 0.9}
    seen_context = []

    def review_text(kind, draft, restaurant_id=None, context="", mode="haiku_gate"):
        assert kind == "insight_read" and mode == "haiku_shadow" and restaurant_id is None
        seen_context.append(context)
        return orch.Verdict(ok=True, score=scores[draft], label="pass")
    monkeypatch.setattr(ai_reviewer, "review_text", review_text)
    monkeypatch.setattr(ai_learning, "_score_async", lambda fn: fn())
    view = {"workflow": "shadow_arms", "custom_id": item["custom_id"], "restaurant_id": None,
            "action": "shadow_arms", "model": item["request"]["model"], "context": item["context"],
            "call_id": call_id, "status": "collecting"}
    ai_learning.shadow_landed(view, message=_Msg("Ratings dipped; reply to the two 2-star reviews."))
    (shadow,) = _q(db, "SELECT * FROM ai_runs WHERE shadow_of=?", (rr.run_id,))
    assert shadow["final_tier"] == "T1" and shadow["status"] == "ok" and shadow["reviewer_score"] == 0.8
    # Production ran synchronously: the candidate is compared at its synchronous price.
    assert shadow["cost_usd"] == pytest.approx(batch_cost / ai_utils.BATCH_PRICE_MULTIPLIER, rel=1e-4)
    assert json.loads(shadow["context_json"])["batch_cost_usd"] == pytest.approx(batch_cost)
    assert _q(db, "SELECT reviewer_score FROM ai_runs WHERE run_id=?", (rr.run_id,))[0]["reviewer_score"] == 0.9
    assert len(seen_context) == 2 and seen_context[0] == seen_context[1] and "Ratings fell" in seen_context[0]
    assert ai_learning._shadow_pairs(28, None)[("review_insight", "T1")]
    # A shadow row never counts as production in the stats or the outcomes.
    assert all(s["tier"] != "T1" for s in ai_learning.route_stats(workflow="review_insight"))
    # Replayed once on a tier: never again.
    sent.clear()
    out = ai_learning.shadow_arms("review_insight", "T1", sample=5, budget_usd=1.0)
    assert out["submitted"] == 0 and out["skipped"] == 1 and not sent


def test_no_production_text_means_no_replay(db, monkeypatch):
    _production_run(db, monkeypatch)
    _x(db, "UPDATE ai_calls SET output_z=NULL")
    monkeypatch.setattr(ai_batches, "enabled", lambda w: True)
    monkeypatch.setattr(ai_batches, "submit", lambda w, items: pytest.fail("sent a replay with nothing to compare"))
    out = ai_learning.shadow_arms("review_insight", "T1", sample=5)
    assert out["skipped"] == 1 and out["submitted"] == 0


def test_the_sample_and_the_budget_bound_one_workflow(db, monkeypatch):
    for _ in range(3):
        _production_run(db, monkeypatch)
    monkeypatch.setattr(ai_batches, "enabled", lambda w: True)
    sent = []
    monkeypatch.setattr(ai_batches, "submit", lambda w, items: sent.extend(items) or
                        {it["custom_id"]: ai_batches.SUBMITTED for it in items})
    assert ai_learning.shadow_arms("review_insight", "T1", sample=2)["submitted"] == 2
    sent.clear()
    out = ai_learning.shadow_arms("review_insight", "T1", sample=5, budget_usd=0.0)
    assert out["hit_bound"] and not sent


def test_a_failed_replay_is_recorded_and_not_scored(db, monkeypatch):
    rr = _production_run(db, monkeypatch)
    monkeypatch.setattr(ai_learning, "_score_async", lambda fn: pytest.fail("scored a failed replay"))
    ai_learning.shadow_landed({"model": "m", "context": {"workflow": "review_insight", "run_id": rr.run_id,
                                                         "tier": "T1"}},
                              error=ai_batches.BatchItemFailed("expired"))
    (row,) = _q(db, "SELECT status, reviewer_score FROM ai_runs WHERE shadow_of=?", (rr.run_id,))
    assert row == {"status": "error", "reviewer_score": None}


# ── the console: AI routes ─────────────────────────────────────────────────

@pytest.fixture
def app(db):
    init_auth(db_path=db)
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.secret_key = "orch-console"
    flask_app.register_blueprint(admin_routes.admin_bp)
    return flask_app


def _admin(app, db, stepped_up=True):
    rid = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="will@cavnar.test"), db_path=db)
    uid = create_user(rid, "will", "will@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db)
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, password_verified_at=True if stepped_up else None, db_path=db))
    c.set_cookie("csrf_js", CSRF)
    return c


def _post(c, url, body=None):
    return c.post(url, json=body if body is not None else {}, headers={"X-CSRF": CSRF})


def _audit(db, action):
    return _q(db, "SELECT * FROM admin_events WHERE event_type=? ORDER BY id", (action,))


_WRITES = [("/admin/api/ai-routes/labor_insight/override", {"override": {"ladder": ["T2"]}}),
           ("/admin/api/ai-routes/labor_insight/revert", {}),
           ("/admin/api/ai-routes/recommendations/1", {"apply": True}),
           ("/admin/api/ai-costs/invoices", {"vendor": "anthropic", "month": "2026-09", "amount_usd": 10})]


@pytest.mark.parametrize("path,body", _WRITES)
def test_every_console_write_needs_the_password_again(app, db, path, body):
    c = _admin(app, db, stepped_up=False)
    r = _post(c, path, body)
    assert r.status_code == 403 and r.get_json()["reauth_required"] is True
    assert not _q(db, "SELECT 1 FROM ai_route_overrides") and not _q(db, "SELECT 1 FROM ai_vendor_invoices")


def test_the_step_up_sits_directly_under_admin_required():
    src = open(os.path.join(ROOT, "admin_routes.py"), encoding="utf-8").read()
    for fn in ("admin_api_ai_route_override", "admin_api_ai_route_revert", "admin_api_ai_route_recommendation",
               "admin_api_ai_costs_invoice"):
        assert f"@admin_required\n@recent_auth_required()\ndef {fn}(" in src, fn
        body = src[src.index(f"def {fn}("):]
        body = body[:body.index("\n@admin_bp.route")]
        assert "_ai_console_answer(" in body, fn
    assert "admin_events.record_admin_action(" in src[src.index("def _ai_console_answer("):]


def test_the_routes_view_shows_the_policy_in_force_and_its_runs(app, db):
    c = _admin(app, db)
    _x(db, "INSERT INTO ai_runs (run_id, workflow, restaurant_id, final_tier, final_model, status, escalations, "
           "latency_ms, cost_usd) VALUES ('run:l:1','labor_insight',7,'T1','m','ok',0,800,0.002)")
    d = c.get("/admin/api/ai-routes").get_json()
    assert d["ok"] and not d["errors"]
    w = {x["workflow"]: x for x in d["workflows"]}
    assert set(w) == set(wf.POLICIES)
    li = w["labor_insight"]
    assert li["effective"]["ladder"] == ["T1", "T2"] and li["overridden"] == [] and li["override"] is None
    assert li["totals"]["runs"] == 1 and li["stats"][0]["tier"] == "T1"
    assert w["review_insight"]["shadow_candidate"] == "T1" and w["labor_schedule"]["replayable"] is False


def test_an_override_is_validated_minimal_audited_and_revertible(app, db):
    c = _admin(app, db)
    # Refused by the registry's own validation; nothing stored; the refusal audited.
    r = _post(c, "/admin/api/ai-routes/labor_insight/override", {"override": {"ladder": ["T9"]}})
    assert r.status_code == 400 and "ladder" in r.get_json()["error"]
    assert not _q(db, "SELECT 1 FROM ai_route_overrides")
    assert _audit(db, "ai_route.override_set")[-1]["result"] == "refused"
    # Fields equal to the default are not stored.
    r = _post(c, "/admin/api/ai-routes/labor_insight/override", {"reason": "start on Sonnet", "override": {
        "ladder": ["T2"], "reviewer": "rules", "shadow_rate": "0", "caps": {"calls": "2", "usd": "0.05",
                                                                           "seconds": "45"}}})
    assert r.status_code == 200 and r.get_json()["ok"], r.get_json()
    (row,) = _q(db, "SELECT override_json, updated_by, reason FROM ai_route_overrides")
    assert json.loads(row["override_json"]) == {"ladder": ["T2"]} and row["updated_by"] == "will"
    orch._OVERRIDES.clear()
    assert wf.policy("labor_insight").ladder == ("T2",)
    ev = _audit(db, "ai_route.override_set")[-1]
    assert ev["result"] == "ok" and json.loads(ev["after_json"]) == {"ladder": ["T2"]} and ev["before_json"] is None
    assert ev["target"] == "workflow:labor_insight"
    # A second override, then Revert puts the first back.
    _post(c, "/admin/api/ai-routes/labor_insight/override", {"override": {"ladder": ["T2"], "max_escalations": 0,
                                                                          "escalate_on": []}})
    r = _post(c, "/admin/api/ai-routes/labor_insight/revert")
    assert r.status_code == 200 and r.get_json()["after"] == {"ladder": ["T2"]}
    assert _audit(db, "ai_route.reverted")[-1]["result"] == "ok"
    d = c.get("/admin/api/ai-routes").get_json()
    li = [x for x in d["workflows"] if x["workflow"] == "labor_insight"][0]
    assert li["overridden"] == ["ladder"] and li["default"]["ladder"] == ["T1", "T2"]
    # Setting it back to the default leaves no override; a revert then has nothing to do.
    _post(c, "/admin/api/ai-routes/labor_insight/override", {"override": {"ladder": ["T1", "T2"]}})
    assert not _q(db, "SELECT 1 FROM ai_route_overrides")
    assert _post(c, "/admin/api/ai-routes/labor_insight/revert").status_code == 409
    assert _post(c, "/admin/api/ai-routes/nope/override", {"override": {}}).status_code == 404


def test_a_recommendation_is_applied_or_dismissed_from_the_console(app, db):
    c = _admin(app, db)
    _x(db, "INSERT INTO ai_route_recommendations (workflow, kind, summary, proposed_json, evidence_json) VALUES "
           "('labor_insight','start_higher','start there','{\"ladder\": [\"T2\"]}','{}')")
    _x(db, "INSERT INTO ai_route_recommendations (workflow, kind, summary, proposed_json, evidence_json) VALUES "
           "('review_insight','quality_low','look','{}','{}')")
    ids = [r["id"] for r in _q(db, "SELECT id FROM ai_route_recommendations ORDER BY id")]
    d = c.get("/admin/api/ai-routes").get_json()
    assert d["open_recommendations"] == 2
    r = _post(c, f"/admin/api/ai-routes/recommendations/{ids[0]}", {"apply": True})
    assert r.status_code == 200 and r.get_json()["after"] == {"ladder": ["T2"]}
    orch._OVERRIDES.clear()
    assert wf.policy("labor_insight").ladder == ("T2",)
    assert _audit(db, "ai_route.recommendation_applied")[-1]["result"] == "ok"
    # A recommendation that asks for a look has nothing to apply; it can be dismissed.
    assert _post(c, f"/admin/api/ai-routes/recommendations/{ids[1]}", {"apply": True}).status_code == 409
    assert _post(c, f"/admin/api/ai-routes/recommendations/{ids[1]}", {"apply": False}).status_code == 200
    assert _q(db, "SELECT status FROM ai_route_recommendations WHERE id=?", (ids[1],))[0]["status"] == "dismissed"
    assert _post(c, f"/admin/api/ai-routes/recommendations/{ids[1]}", {"apply": False}).status_code == 409


# ── the console: AI costs (#95, #96) ───────────────────────────────────────

def test_the_month_view_sums_by_restaurant_and_workflow_from_rollup_and_raw(app, db):
    c = _admin(app, db)
    a = create_restaurant(Restaurant(name="Alpha Grill", owner_email="a@x.test"), db_path=db)
    month = admin_ops._utcnow().strftime("%Y-%m")
    # A rolled day (ai_usage_daily) and the raw ledger after the newest rolled day.
    _x(db, "INSERT INTO ai_usage_daily (day, rid_key, restaurant_id, vendor, action, model, calls, cost_usd) "
           "VALUES (?, ?, ?, 'anthropic', 'labor_insight', 'm', 4, 0.40)", (f"{month}-01", a, a))
    _x(db, "INSERT INTO ai_usage_daily (day, rid_key, restaurant_id, vendor, action, model, calls, cost_usd) "
           "VALUES (?, ?, ?, 'anthropic', 'labor_insight', 'm', 9, 9.0)", (f"{month}-02", a, a))
    _x(db, "INSERT INTO ai_usage (restaurant_id, action, model, input_tokens, output_tokens, cost_usd, vendor, "
           "outcome, created_at) VALUES (?, 'labor_insight', 'm', 1, 1, 0.10, 'anthropic', 'ok', ?)",
       (a, f"{month}-02 09:00:00"))
    _x(db, "INSERT INTO ai_usage (restaurant_id, action, model, input_tokens, output_tokens, cost_usd, vendor, "
           "outcome, created_at) VALUES (?, 'competitor_insight', 'google-places-details', 0, 0, 0.017, "
           "'google_places', 'ok', ?)", (a, f"{month}-02 10:00:00"))
    _x(db, "INSERT INTO ai_usage (restaurant_id, action, model, input_tokens, output_tokens, cost_usd, vendor, "
           "outcome, created_at) VALUES (NULL, 'shadow_arms', 'm', 1, 1, 0.05, 'anthropic', 'ok', ?)",
       (f"{month}-02 11:00:00",))
    d = c.get(f"/admin/api/ai-costs?month={month}").get_json()
    assert d["ok"] and d["rolled_through"] == f"{month}-02"
    # The 2nd is read raw (the rollup of a day may be partial): 0.40 rolled + 0.10 + 0.017 + 0.05 raw.
    assert d["total_usd"] == pytest.approx(0.567, abs=1e-6)
    rest = {r["restaurant"]: r for r in d["restaurants"]}
    assert rest["Alpha Grill"]["cost_usd"] == pytest.approx(0.517, abs=1e-4)
    assert rest["Platform (no restaurant)"]["cost_usd"] == pytest.approx(0.05)
    flows = {(w["vendor"], w["action"]): w for w in d["workflows"]}
    assert flows[("anthropic", "labor_insight")]["calls"] == 5 and flows[("anthropic", "labor_insight")]["agent"] == "analyst"
    assert flows[("google_places", "competitor_insight")]["cost_usd"] == pytest.approx(0.017, abs=1e-4)
    assert {r["vendor"] for r in d["reconciliation"]} == {"anthropic", "google_places", "perplexity"}
    assert all(r["invoice_usd"] is None and r["ratio"] is None for r in d["reconciliation"])


def test_an_invoice_is_stored_audited_and_read_against_the_ledger(app, db):
    c = _admin(app, db)
    month = admin_ops._utcnow().strftime("%Y-%m")
    _x(db, "INSERT INTO ai_usage (restaurant_id, action, model, input_tokens, output_tokens, cost_usd, vendor, "
           "outcome, created_at) VALUES (NULL, 'labor_insight', 'm', 1, 1, 11.0, 'anthropic', 'ok', ?)",
       (f"{month}-03 09:00:00",))
    assert _post(c, "/admin/api/ai-costs/invoices", {"vendor": "openai", "month": month,
                                                     "amount_usd": 1}).status_code == 400
    assert _post(c, "/admin/api/ai-costs/invoices", {"vendor": "anthropic", "month": "Sep",
                                                     "amount_usd": 1}).status_code == 400
    r = _post(c, "/admin/api/ai-costs/invoices", {"vendor": "anthropic", "month": month, "amount_usd": "10",
                                                  "note": "inv 123"})
    assert r.status_code == 200 and r.get_json()["ok"]
    r = _post(c, "/admin/api/ai-costs/invoices", {"vendor": "anthropic", "month": month, "amount_usd": "10.00"})
    ev = _audit(db, "ai_invoice.recorded")
    assert [e["result"] for e in ev] == ["refused", "refused", "ok", "ok"]
    assert json.loads(ev[-1]["before_json"])["amount_usd"] == 10.0 and ev[-1]["target"] == f"invoice:anthropic:{month}"
    d = c.get(f"/admin/api/ai-costs?month={month}").get_json()
    (an,) = [x for x in d["reconciliation"] if x["vendor"] == "anthropic"]
    assert an["invoice_usd"] == 10.0 and an["ledger_usd"] == 11.0 and an["ratio"] == 1.1 and an["diff_usd"] == 1.0


# ── the page ───────────────────────────────────────────────────────────────

def _console_js():
    html = open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8").read()
    start = html.index("// ── AI routes (internal only")
    return html, html[start:html.index("// ── Alerts & notifications", start)]


def test_the_ai_console_tabs_are_es5_use_the_button_system_and_mdy_dates():
    html, js = _console_js()
    for pat in (r"`", r"\bconst\s", r"\blet\s", r"=>", r"\basync\s", r"\bawait\s", r"\?\.", r"\?\?"):
        assert not re.search(pat, js), pat
    buttons = re.findall(r"<button[^>]*>", js)
    assert buttons and all("cbtn" in b for b in buttons)
    assert "toISOString" not in js and "fmtD(" in js, "owner-facing dates go through fmtD/mdy (M/D/YY)"
    assert "['ai-routes', 'AI routes']" in html and "['ai-costs', 'AI costs']" in html
    assert "await aiRoutes()" in html and "await aiCosts()" in html
