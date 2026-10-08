"""AI cost audit 10/7/26: the unattended work through Message Batches, with
a synchronous fallback by a cutoff (#59 the competitor read, #60 the recipe
drafts, #61 the digest's narrative, #62 the quiet-night post), the digest's
answer kept and its figure lines written in Python (#50, #82), the
marketing read's first line from the feed (#80), the monthly email's
opening line (#83), the invoice eval (#86), the recipe photo read once
(#87) and the social post's cache marker (#79, deliberately not added).

A fake batches client stands in for Anthropic and every model call is a
stub: nothing here reaches a model, a provider or a send.
"""
import json
import sqlite3
import types
from datetime import date, datetime, timedelta

import pytest

import models
from models import Restaurant, Review, create_restaurant, save_reviews

# Imported before any fixture patches get_conn (CLAUDE.md's bound-import hazard).
import ai_batches  # noqa: E402
import ai_utils  # noqa: E402
import client_api  # noqa: E402
import competitor  # noqa: E402
import emails  # noqa: E402
import inventory_ledger  # noqa: E402
import marketing  # noqa: E402
import recipes  # noqa: E402
import reporter  # noqa: E402
import review_intelligence  # noqa: E402
import scheduler  # noqa: E402
import strategy_jobs  # noqa: E402

NS = types.SimpleNamespace


class FakeBatches:
    def __init__(self):
        self.created, self.cancelled = [], []

    def create(self, requests):
        self.created.append([dict(r) for r in requests])
        return NS(id=f"msgbatch_{len(self.created)}")

    def retrieve(self, batch_id):
        return NS(id=batch_id, processing_status="in_progress", ended_at=None, request_counts=None)

    def results(self, batch_id):
        return iter([])

    def cancel(self, batch_id):
        self.cancelled.append(batch_id)


@pytest.fixture(autouse=True)
def db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH
    fake = lambda *a, **k: real(db_path if (not a or a[0] in (None, default)) else a[0])  # noqa: E731
    for mod in (models, recipes, review_intelligence):
        monkeypatch.setattr(mod, "get_conn", fake, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: None)
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: False)
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    ai_utils.reset_breaker()
    models._invalidate_tenant_names()
    yield db_path
    ai_utils.reset_breaker()
    models._invalidate_tenant_names()


@pytest.fixture
def batches(monkeypatch):
    fake = FakeBatches()
    monkeypatch.setattr(ai_batches, "_client", lambda: NS(messages=NS(batches=fake)))
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.delenv("AI_BATCHES_WORKFLOWS", raising=False)
    monkeypatch.delenv("AI_BATCHES_ENABLED", raising=False)
    return fake


def _rid(db, **kw):
    kw.setdefault("module_inventory", 1)
    kw.setdefault("module_marketing", 1)
    return create_restaurant(Restaurant(name=kw.pop("name", "Batch Bistro"), owner_email="o@x.test", **kw),
                             db_path=db)


def _msg(text, stop="end_turn"):
    return NS(id="msg_1", stop_reason=stop, content=[NS(type="text", text=text)],
              usage=NS(input_tokens=10, output_tokens=10, cache_creation_input_tokens=0, cache_read_input_tokens=0))


def _rows(db, sql, args=()):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


SEEN = []


def cb(item, message=None, error=None):
    SEEN.append({"item": item, "message": message, "error": error})


# ── ai_batches: the cutoff and the item's own run ───────────────────────────

def _item(cid, rid, **kw):
    import sys
    sys.modules.setdefault("test_ai_cost_batches_1007", sys.modules[__name__])
    return dict({"custom_id": cid, "restaurant_id": rid, "action": "t", "callback": "test_ai_cost_batches_1007:cb",
                 "request": {"model": "claude-sonnet-5", "max_tokens": 50,
                             "messages": [{"role": "user", "content": "hi"}]}}, **kw)


def test_an_item_past_its_cutoff_is_cancelled_and_its_caller_told_to_go_synchronous(db, batches):
    SEEN.clear()
    rid = _rid(db)
    out = ai_batches.submit("competitor_insight", [
        _item("late1", rid, cutoff_at=datetime.utcnow() - timedelta(minutes=1)),
        _item("ontime", rid, cutoff_at=datetime.utcnow() + timedelta(hours=3))])
    assert set(out.values()) == {ai_batches.SUBMITTED}
    res = ai_batches.run_collector()
    assert res["cut_off"] == 1 and res["skipped"] == 1
    assert [s["item"]["custom_id"] for s in SEEN] == ["late1"]
    err = SEEN[0]["error"]
    assert isinstance(err, ai_batches.BatchItemFailed) and err.kind == "cutoff"
    assert ai_batches.item("competitor_insight", "late1")["status"] == ai_batches.CANCELLED
    assert ai_batches.pending("competitor_insight", "ontime")
    assert batches.cancelled == [], "the batch still carries an item somebody waits on"


def test_each_item_carries_its_own_run_id_as_its_ledger_group(db, batches):
    rid = _rid(db)
    ai_batches.submit("recipe_draft", [_item("r1", rid, correlation_id="run:recipe_draft:a"),
                                       _item("r2", rid, correlation_id="run:recipe_draft:b")])
    assert len(batches.created) == 1, "one batch for the restaurant's dishes"
    assert {r["correlation_id"] for r in _rows(db, "SELECT correlation_id FROM ai_batch_items")} == {
        "run:recipe_draft:a", "run:recipe_draft:b"}


def test_an_older_items_table_gains_the_cutoff_column_at_boot(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE ai_batch_items (workflow TEXT NOT NULL, custom_id TEXT NOT NULL, batch_id TEXT, "
              "restaurant_id INTEGER, action TEXT, model TEXT, callback TEXT NOT NULL, context_json TEXT, "
              "request_z BLOB, status TEXT NOT NULL, created_at TEXT, PRIMARY KEY (workflow, custom_id))")
    c.commit()
    c.close()
    monkeypatch.setattr(ai_batches, "_conn", lambda db_path=None: sqlite3.connect(path))
    ai_batches.init_ai_batches()
    c = sqlite3.connect(path)
    assert "cutoff_at" in {r[1] for r in c.execute("PRAGMA table_info(ai_batch_items)")}
    c.close()


def test_the_four_unattended_workflows_batch_by_default():
    assert {"competitor_insight", "recipe_draft", "weekly_digest", "quiet_night_post"} <= set(
        ai_batches.DEFAULT_WORKFLOWS.split(","))


# ── #59 the weekly competitor read ──────────────────────────────────────────

COMPS = [{"place_id": "p1", "name": "Rival A", "rating": 4.5, "review_count": 120, "price_level": 2,
          "reviews": [{"rating": 2, "text": "Slow service on Friday", "time": "a week ago"}]}]


def _restaurant(db):
    return models.get_restaurant(_rid(db, google_place_id="gp1"))


def test_only_the_schedulers_sweep_batches_the_read(db, batches, monkeypatch):
    monkeypatch.setattr(competitor, "ANTHROPIC_KEY", "x")
    r = _restaurant(db)
    with ai_utils.ai_context(trigger="owner", actor_user_id=3):
        assert competitor._submit_insight_batch(r, list(COMPS), {}, [], None, [], {}) is None
    assert batches.created == []
    with ai_utils.ai_context(trigger="scheduler"):
        out = competitor._submit_insight_batch(r, json.loads(json.dumps(COMPS)), {"vibe": "cozy"}, [], "d", [], {})
    assert out["ok"] and out["batched"]
    (row,) = _rows(db, "SELECT * FROM ai_batch_items WHERE workflow='competitor_insight'")
    ctx = json.loads(row["context_json"])
    assert row["cutoff_at"] and row["correlation_id"] == ctx["run_id"] and ctx["prompt"]
    assert ctx["competitors"][0]["reviews"][0]["ref"] == "R1", "the review ids the read must cite ride along"
    sent = batches.created[0][0]["params"]
    assert sent["messages"][0]["content"] == ctx["prompt"]


def test_a_landed_read_is_judged_and_stored_as_the_synchronous_one(db, monkeypatch):
    r = _restaurant(db)
    stored, attempts = [], []
    monkeypatch.setattr(competitor, "finish_competitor_insight", lambda text, *a, **k: f"READ: {text}")
    monkeypatch.setattr(competitor, "_store_analysis", lambda rest, comps, insight, *a: stored.append(insight))
    monkeypatch.setattr(competitor, "_record_attempt", lambda rid, ok, error=None: attempts.append(ok))
    item = {"restaurant_id": r.id, "context": {"prompt": "P", "competitors": COMPS, "run_id": "run:ci:1"}}
    competitor.on_insight_batch(item, message=_msg("Rival A is slow."))
    assert stored == ["READ: Rival A is slow."] and attempts == [True]
    (run,) = _rows(db, "SELECT * FROM ai_runs WHERE run_id='run:ci:1'")
    assert run["workflow"] == "competitor_insight" and run["unattended"] == 1


def test_no_answer_writes_the_read_now_and_a_gate_refusal_writes_nothing(db, monkeypatch):
    r = _restaurant(db)
    stored, attempts, sync = [], [], []
    monkeypatch.setattr(competitor, "_store_analysis", lambda rest, comps, insight, *a: stored.append(insight))
    monkeypatch.setattr(competitor, "_record_attempt", lambda rid, ok, error=None: attempts.append(ok))
    monkeypatch.setattr(competitor, "generate_competitor_insight", lambda *a, **k: sync.append(1) or "NOW")
    item = {"restaurant_id": r.id, "context": {"competitors": COMPS}}
    competitor.on_insight_batch(item, error=ai_batches.BatchItemFailed("cutoff"))
    assert sync == [1] and stored == ["NOW"] and attempts == [True]
    competitor.on_insight_batch(item, error=ai_utils.AIBudgetExceeded("paused"))
    assert sync == [1] and stored == ["NOW"] and attempts == [True, False]


# ── #60 the recipe drafts: the cached list first, the dish last, batched ────

def _ingredients(db, rid, names=("Mozzarella", "Pizza Dough", "Basil", "Tomato")):
    c = models.get_conn(db)
    for n in names:
        c.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, is_active) VALUES (?,?,?,?,1)",
                  (rid, n, "oz", 1.0))
    c.commit()
    c.close()


def _dish(db, rid, name):
    c = models.get_conn(db)
    i = c.execute("INSERT INTO menu_items (restaurant_id, name, is_active) VALUES (?,?,1)", (rid, name)).lastrowid
    c.commit()
    c.close()
    return i


def test_the_recipe_prompt_puts_the_shared_list_first_under_a_cache_marker_and_the_dish_last():
    ings = [{"name": "Mozzarella", "unit": "oz"}, {"name": "Basil", "unit": "oz"}]
    req = recipes._draft_request("Margherita Pizza", ings, "wood oven", examples=None)
    head, tail = req["messages"][0]["content"]
    assert head["cache_control"] == {"type": "ephemeral"} and "cache_control" not in tail
    assert "- Mozzarella (unit: oz)" in head["text"] and "wood oven" in head["text"]
    assert "Margherita" not in head["text"], "the prefix is the same for every dish of the run"
    assert tail["text"].endswith("Draft the recipe for ONE plate of: Margherita Pizza\n\nReturn the JSON only.")
    # The prefix is identical across dishes, so the run's later calls read it.
    other = recipes._draft_request("Pepperoni Pizza", ings, "wood oven")
    assert other["messages"][0]["content"][0] == head


def test_the_tuesday_job_sends_its_dishes_as_one_batch_and_drafts_nothing_now(db, batches, monkeypatch):
    rid = _rid(db)
    _ingredients(db, rid)
    a, b = _dish(db, rid, "Margherita Pizza"), _dish(db, rid, "Caprese")
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: pytest.fail("batched, not called"))
    out = recipes.draft_missing(rid, batch=True)
    assert out == {"drafted": 0, "skipped": 0, "batched": 2}
    assert len(batches.created) == 1 and len(batches.created[0]) == 2
    rows = _rows(db, "SELECT * FROM ai_batch_items WHERE workflow='recipe_draft'")
    assert {json.loads(r["context_json"])["item"]["id"] for r in rows} == {a, b}
    assert all(r["cutoff_at"] for r in rows) and len({r["correlation_id"] for r in rows}) == 2


def test_an_owners_own_drafts_are_never_batched(db, batches, monkeypatch):
    rid = _rid(db)
    _ingredients(db, rid)
    d = _dish(db, rid, "Margherita Pizza")
    reply = _msg(json.dumps({"ingredients": [{"name": "Mozzarella", "qty": 4, "unit": "oz", "confidence": "medium"}],
                             "note": None}))
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: reply)
    out = recipes.draft_missing(rid, client=object(), items=[{"id": d, "name": "Margherita Pizza"}])
    assert out == {"drafted": 1, "skipped": 0} and batches.created == []


def test_a_landed_dish_is_drafted_and_a_lost_one_is_drafted_now(db, monkeypatch):
    rid = _rid(db)
    _ingredients(db, rid)
    a, b = _dish(db, rid, "Margherita Pizza"), _dish(db, rid, "Caprese")
    reply = _msg(json.dumps({"ingredients": [{"name": "Basil", "qty": 1, "unit": "oz", "confidence": "medium"}],
                             "note": None}))
    calls = []
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *x, **k: calls.append(k) or reply)
    monkeypatch.setattr(ai_utils, "get_client", lambda *x, **k: object())
    recipes.on_draft_batch({"restaurant_id": rid, "context": {"item": {"id": a, "name": "Margherita Pizza"},
                                                               "run_id": "run:recipe_draft:x"}}, message=reply)
    assert calls == [], "the batch's answer is the run's first rung"
    recipes.on_draft_batch({"restaurant_id": rid, "context": {"item": {"id": b, "name": "Caprese"}}},
                           error=ai_batches.BatchItemFailed("expired"))
    assert len(calls) == 1
    drafts = {d["menu_item_id"] for d in recipes.list_drafts(rid)}
    assert drafts == {a, b}
    # A dish that has a pending draft by the time its answer lands is left alone.
    recipes.on_draft_batch({"restaurant_id": rid, "context": {"item": {"id": a, "name": "Margherita Pizza"}}},
                           message=reply)
    assert len(recipes.list_drafts(rid)) == 2


def test_the_job_asks_for_batches():
    import inspect
    assert "recipes.draft_missing(r.id, db_path=db_path, batch=True)" in inspect.getsource(
        strategy_jobs.run_recipe_drafts)


# ── #82 #50 #61 the weekly digest ───────────────────────────────────────────

# Two days ago: the digest's week is whole local days ending yesterday
# (reporter.digest_window, AI cost audit 10/7/26 re-audit #6) — a review
# from today is next week's, whatever timezone the run is in.
_IN_WEEK = (date.today() - timedelta(days=2)).isoformat()


def _week(db, rid):
    save_reviews([
        Review(restaurant_id=rid, platform="google", external_id="d1", author="Dana Ray", rating=5,
               text="Lovely dinner.", review_date=_IN_WEEK),
        Review(restaurant_id=rid, platform="google", external_id="d2", author="Sam Lee", rating=4,
               text="Good tacos.", review_date=_IN_WEEK)], db_path=db)
    return reporter.build_report_from_db(rid, "Batch Bistro", days=7, db_path=db)


def test_the_figure_lines_are_written_from_the_figures():
    report = NS(total_reviews=12, avg_rating=4.6)
    facts = {"labor": {"pct": 31.24, "direction": "up", "from_pct": 29.0, "weeks": 3, "overtime_risk": 2,
                       "days": 28, "through": "10/4/26"},
             "inventory": {"top_waste_item": "Salmon", "waste_direction": "up", "waste_change_pct": 18.0,
                           "first_low": "Basil"}}
    out = reporter._templated_lines(["REVIEWS", "LABOR", "INVENTORY"], report, 9, 2, 1, facts, 0.2)
    assert out["reviews"] == "12 reviews this week, averaging 4.6★, up 0.2★ on last week (9 positive and 2 negative, 1 marked urgent)."
    assert out["labor"] == ("Labor was 31.2% of revenue over the 28 days of shifts through 10/4/26, up from 29.0% "
                            "over 3 weeks, with 2 people at overtime risk.")
    assert out["inventory"] == "Waste is up 18% on last week; the top waste item is Salmon; Basil is critically low."
    assert reporter._templated_lines(["LABOR"], report, 0, 0, 0, {"labor": None}, None) == {}


def test_the_model_writes_only_the_headline_marketing_and_action(db, monkeypatch):
    rid = _rid(db)
    report = _week(db, rid)
    seen = []
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: seen.append(k) or _msg(
        "HEADLINE: Pat, two new reviews this week.\nREVIEWS: Your rating beats every place in town.\n"
        "ACTION: none this week"))
    out = reporter.generate_ai_digest_summary(report, "Batch Bistro", "Pat", restaurant_id=rid)
    prompt = seen[0]["messages"][0]["content"]
    assert "REVIEWS: one short sentence" not in prompt and "LABOR: one short sentence" not in prompt
    assert "The REVIEWS, LABOR and INVENTORY lines are written for you" in prompt
    assert seen[0]["max_tokens"] == reporter.DIGEST_MAX_TOKENS
    assert out["reviews"] == "2 reviews this week, averaging 4.5★." and "beats" not in out["reviews"]


def test_the_same_week_is_written_once_and_served_again(db, monkeypatch):
    rid = _rid(db)
    report = _week(db, rid)
    calls = []
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: calls.append(1) or _msg(
        "HEADLINE: Pat, two new reviews this week."))
    first = reporter.generate_ai_digest_summary(report, "Batch Bistro", "Pat", restaurant_id=rid)
    again = reporter.generate_ai_digest_summary(report, "Batch Bistro", "Pat", restaurant_id=rid)
    assert calls == [1] and again == first, "a preview or a resend of the same week serves the kept answer"
    # New data is a new prompt: written again.
    save_reviews([Review(restaurant_id=rid, platform="google", external_id="d3", author="Lee Q", rating=3,
                         text="Fine.", review_date=_IN_WEEK)], db_path=db)
    report2 = reporter.build_report_from_db(rid, "Batch Bistro", days=7, db_path=db)
    reporter.generate_ai_digest_summary(report2, "Batch Bistro", "Pat", restaurant_id=rid)
    assert calls == [1, 1]


def test_the_night_before_the_narrative_goes_as_a_batch_and_the_send_reads_it(db, batches, monkeypatch):
    rid = _rid(db)
    report = _week(db, rid)
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: pytest.fail("no synchronous call"))
    # 3am local on the send day: before the 9am send (tomorrow's, so the
    # cutoff is ahead whatever the clock running the test says).
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now",
                        lambda r=None, naive=False: (datetime.now() + timedelta(days=1)).replace(hour=3, minute=0))
    out = reporter.generate_ai_digest_summary(report, "Batch Bistro", "Pat", restaurant_id=rid, precompute=True)
    assert out == {"_precompute": ai_batches.SUBMITTED}
    (row,) = _rows(db, "SELECT * FROM ai_batch_items WHERE workflow='weekly_digest'")
    ctx = json.loads(row["context_json"])
    assert row["cutoff_at"] and ctx["fingerprint"]
    reporter.on_digest_batch({"restaurant_id": rid, "context": ctx, "call_id": None},
                             message=_msg("HEADLINE: Pat, two new reviews this week."))
    sent = reporter.generate_ai_digest_summary(report, "Batch Bistro", "Pat", restaurant_id=rid)
    assert sent["headline"] == "Pat, two new reviews this week." and sent["reviews"]


def test_the_precompute_only_runs_where_batches_may(db, monkeypatch):
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    out = reporter.run_digest_precompute()
    assert out["attempted"] == 0 and "not enabled" in out["reason"]


def test_the_precompute_is_a_registered_job_on_the_ai_lane():
    import inspect
    import jobs_registry
    assert jobs_registry.JOBS["digest_precompute"]["target"] == ("reporter", "run_digest_precompute")
    assert jobs_registry.JOBS["digest_precompute"]["sends"] is False
    assert 'run_in_lane("ai", "digest_precompute", run_digest_precompute)' in inspect.getsource(
        scheduler.scheduler_loop)


# ── #62 the quiet-night post ────────────────────────────────────────────────

def test_the_quiet_night_post_is_queued_as_a_batch_item(db, batches, monkeypatch):
    rid = _rid(db)
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: pytest.fail("batched, not called"))
    out = strategy_jobs._draft_quiet_night_fill(models.get_restaurant(rid), {"weekday": "Tuesday"}, db)
    assert out == {"post_draft_queued": True}
    (row,) = _rows(db, "SELECT * FROM ai_batch_items WHERE workflow='quiet_night_post'")
    ctx = json.loads(row["context_json"])
    assert row["action"] == "marketing_content" and ctx["topic"] == "Tuesday" + strategy_jobs._QN_POST_SUFFIX
    assert ctx["state"]["prompt"] and row["cutoff_at"]


def test_without_batches_the_post_is_written_now_as_before(db, monkeypatch):
    rid = _rid(db)
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    import marketing_drafts
    monkeypatch.setattr(marketing, "generate_content", lambda *a, **k: "Come in Tuesday")
    monkeypatch.setattr(marketing_drafts, "save_draft", lambda *a, **k: {"ok": True, "id": 41})
    out = strategy_jobs._draft_quiet_night_fill(models.get_restaurant(rid), {"weekday": "Tuesday"}, db)
    assert out == {"post_draft_id": 41}


def test_a_landed_post_is_judged_as_a_fresh_draft_and_saved(db, monkeypatch):
    rid = _rid(db)
    import marketing_drafts
    seen, saved = {}, []
    monkeypatch.setattr(marketing, "generate_content", lambda *a, **k: seen.update(k) or "Come in Tuesday")
    monkeypatch.setattr(marketing_drafts, "save_draft", lambda r, body, **k: saved.append((body, k["topic"])) or {"ok": True})
    msg = _msg("Come in Tuesday")
    strategy_jobs.on_quiet_night_post({"restaurant_id": rid, "context": {
        "topic": "Tuesday" + strategy_jobs._QN_POST_SUFFIX, "state": {"prompt": "P"}, "run_id": "run:mc:1"}},
        message=msg)
    assert seen["first"] is msg and seen["state"] == {"prompt": "P"} and seen["run_id"] == "run:mc:1"
    assert saved == [("Come in Tuesday", "Tuesday" + strategy_jobs._QN_POST_SUFFIX)]
    now = []
    monkeypatch.setattr(strategy_jobs, "_quiet_night_post_now", lambda r, topic: now.append(topic) or {})
    strategy_jobs.on_quiet_night_post({"restaurant_id": rid, "context": {"topic": "T"}},
                                      error=ai_batches.BatchItemFailed("cutoff"))
    strategy_jobs.on_quiet_night_post({"restaurant_id": rid, "context": {"topic": "T"}},
                                      error=ai_utils.AIBudgetExceeded("paused"))
    assert now == ["T"], "a gate's refusal drafts nothing"


def test_a_batch_answer_is_the_first_rung_and_a_refusal_climbs_now(db, monkeypatch):
    """generate_content(first=…): the answer is cleaned and validated as a
    fresh draft would be; refused, the next rung is written synchronously."""
    rid = _rid(db)
    state = marketing.content_prompt("instagram_post", "Tuesday dinner", restaurant_id=rid, topic_is_owner=False)
    calls = []
    monkeypatch.setattr(marketing, "create_with_retry", lambda *a, **k: calls.append(k) or _msg("A cozy Tuesday."))
    monkeypatch.setattr(marketing, "get_client", lambda *a, **k: object())
    out = marketing.generate_content("instagram_post", "Tuesday dinner", restaurant_id=rid, topic_is_owner=False,
                                     first=_msg("**A cozy Tuesday** at the bistro."), state=state)
    assert calls == [] and str(out) == "A cozy Tuesday at the bistro."


def test_the_push_says_a_queued_post_is_being_drafted():
    import inspect
    src = inspect.getsource(strategy_jobs.run_demand_opportunity)
    assert "A post is being drafted for your approval" in src
    assert '**{k: v for k, v in drafted.items() if k == "post_draft_id"}' in src


# ── #79 the social post's cache marker, deliberately not added ─────────────

def test_the_social_post_request_carries_no_cache_marker():
    """The refusal retry runs one tier up (another model), and a prompt
    cache is per model: a marker would only add the cache-write premium."""
    req = marketing.social_post_request("Write a post")
    assert "cache_control" not in json.dumps(req)
    import ai_workflows as wf
    p = wf.POLICIES["marketing_content"]
    assert wf.route_for(p, 0).model != wf.route_for(p, 1).model


# ── #80 the marketing read's first line ────────────────────────────────────

def test_the_marketing_reads_first_line_names_the_first_card():
    feed = [{"key": "slow_day:Tuesday", "line": "Fill Tuesday, 10/13/26 — Tuesdays run 18% under a typical day."},
            {"key": "x", "line": "Something else — why"}]
    line1 = client_api.mkt_read_line1("Erik,", feed)
    assert line1 == "Erik, this week's biggest opportunity: Fill Tuesday, 10/13/26."
    assert client_api.mkt_read_line1("Erik,", []) is None
    raw = "Erik, Halloween is your moment.\n\n1. Post the pumpkin pizza.\n2. Text the club."
    assert client_api.mkt_with_line1(raw, line1) == line1 + "\n\n1. Post the pumpkin pizza.\n2. Text the club."
    assert client_api.mkt_with_line1("1. A.\n2. B.", line1) == line1 + "\n\n1. A.\n2. B."


# ── #83 the monthly email's opening line ────────────────────────────────────

def test_a_brief_opening_line_is_the_fallback_with_no_model_call(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: pytest.fail("no model call for brief"))
    assert emails.generate_email_personalization("ctx", "Simple EJ's picked up 12 new reviews.", restaurant_id=1,
                                                 brief=True) == "Simple EJ's picked up 12 new reviews."
    src = open("emails.py", encoding="utf-8").read()
    assert "brief=True" not in src and "summary_paragraph = fallback_paragraph" in src


# ── #87 the recipe photo, read once ─────────────────────────────────────────

class _FakeClient:
    def __init__(self, text):
        self.requests, self._text = [], text
        self.messages = self

    def create(self, **kw):
        self.requests.append(kw)
        return _msg(self._text)


_CARD = json.dumps({"menu_item_name": "Margherita Pizza", "yield": 4, "note": None,
                    "ingredients": [{"name": "Mozzarella", "qty": 16, "unit": "oz", "confidence": "high"}]})


def test_the_same_recipe_photo_opens_the_draft_it_already_made(db):
    rid = _rid(db)
    _ingredients(db, rid)
    fake = _FakeClient(_CARD)
    first = recipes.extract_from_image(rid, b"\xff\xd8 card", "image/jpeg", client=fake, db_path=db)
    again = recipes.extract_from_image(rid, b"\xff\xd8 card", "image/jpeg", client=fake, db_path=db)
    assert len(fake.requests) == 1 and again["duplicate"] is True and again["id"] == first["id"]
    assert first["duplicate"] is False
    recipes.accept(rid, first["id"], db_path=db, yield_count=None)
    with pytest.raises(recipes.RecipePhotoError) as err:
        recipes.extract_from_image(rid, b"\xff\xd8 card", "image/jpeg", client=fake, db_path=db)
    assert "already added" in str(err.value) and len(fake.requests) == 1


def test_a_rejected_card_may_be_read_again(db):
    rid = _rid(db)
    _ingredients(db, rid)
    fake = _FakeClient(_CARD)
    first = recipes.extract_from_image(rid, b"\xff\xd8 card2", "image/jpeg", client=fake, db_path=db)
    recipes.reject(rid, first["id"], db_path=db)
    recipes.extract_from_image(rid, b"\xff\xd8 card2", "image/jpeg", client=fake, db_path=db)
    assert len(fake.requests) == 2


# ── #86 the invoice eval ────────────────────────────────────────────────────

def test_the_invoice_eval_scores_an_arm_against_the_owners_lines(db, tmp_path):
    from scripts import invoice_model_eval as ev
    rid = _rid(db)
    c = models.get_conn(db)
    oil = c.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, is_active) "
                    "VALUES (?,?,?,?,1)", (rid, "Olive Oil", "gal", 30.0)).lastrowid
    salt = c.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, is_active) "
                     "VALUES (?,?,?,?,1)", (rid, "Kosher Salt", "lb", 1.0)).lastrowid
    data = b"\x89PNG invoice"
    import hashlib
    c.execute("INSERT INTO invoice_imports (restaurant_id, supplier, image_sha, lines_json, applied_json) "
              "VALUES (?,?,?,?,?)",
              (rid, "Sysco", hashlib.sha256(data).hexdigest(),
               json.dumps({"lines": [{}, {}], "total_check": {"invoice_total": 66.0}}),
               json.dumps([{"index": 0, "ingredient_id": oil, "new_cost": 32.0, "by": "owner"},
                           {"index": 1, "ingredient_id": salt, "new_cost": 1.0, "by": "rule"}])))
    c.commit()
    c.close()
    (tmp_path / "inv.png").write_bytes(data)
    files = ev.load_files(str(tmp_path))
    truth = ev.truth_for(files[0]["sha"], rid, db)
    assert truth["confirmed"] == {oil: 32.0}, "a line the trusted-supplier rule applied is not ground truth"
    extracted = {"supplier": "Sysco", "invoice_date": None, "invoice_total": 66.0,
                 "lines": [{"description": "OLIVE OIL", "quantity": 2, "unit": "GAL", "unit_price": 32.0,
                            "line_total": 64.0}]}
    s = ev.score(extracted, truth, db_path=db)
    assert s["lines_right"] == 1 and s["confirmed"] == 1 and s["total_right"] is True and s["adds_up"] == 1
    assert [a["name"] for a in ev.parse_arms(ev.DEFAULT_ARMS)] == ["default", "T4:low", "T4:medium", "T3:medium"]


def test_the_invoice_eval_calls_nothing_without_live(tmp_path, monkeypatch, db):
    from scripts import invoice_model_eval as ev
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8 x")
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: pytest.fail("no call without --live"))
    plan = ev.main(["--files", str(tmp_path), "--json"])
    assert plan["files"] == 1 and set(plan["estimate_usd"]) == {"default", "T4:low", "T4:medium", "T3:medium"}


def test_a_batched_competitor_read_is_not_this_weeks_until_it_lands(db_path, monkeypatch):
    # The weekly pass recorded success when the item was SENT, so a batched
    # read that later failed counted as this week's and the retry never ran;
    # and a restaurant with a read still out must not be read again.
    import scheduler, competitor, models, data_health as dh
    import ai_batches
    rid = models.create_restaurant(models.Restaurant(name="Batch Co", owner_email="b@x.test",
                                                     google_place_id="p-b", service_tier="full"), db_path=db_path)
    monkeypatch.setattr(models, "is_full_tier", lambda r: True)
    monkeypatch.setattr(competitor, "run_competitor_analysis",
                        lambda rid: {"ok": True, "batched": True, "competitors_analyzed": 5})
    recorded = []
    monkeypatch.setattr(scheduler, "_record", lambda *a, **k: recorded.append((a, k)))
    out = scheduler.run_weekly_competitor_analysis()
    assert out.get("batched") == 1 and out["ok"] == 1 and not recorded
    seen = []
    monkeypatch.setattr(ai_batches, "open_items", lambda wf, rid=None: ["ci-1"])
    monkeypatch.setattr(competitor, "run_competitor_analysis", lambda rid: seen.append(rid) or {"ok": True})
    scheduler.run_weekly_competitor_analysis(retry_only=True)
    assert seen == []


# ── the blind re-audit of 10/7/26 (#5, #6) ──────────────────────────────────

def test_a_quiet_night_post_a_gate_refused_is_not_promised(db, batches, monkeypatch):
    """#5: a gate's refusal (budget, breaker) was read as queued, and the
    push said a post was being drafted that never came. Blocked is not
    drafted: {} — the push's old wording — and nothing is written now
    either (the synchronous call would be refused the same)."""
    rid = _rid(db)
    monkeypatch.setattr(ai_batches, "submit", lambda workflow, items, **k: {items[0]["custom_id"]: ai_batches.BLOCKED})
    monkeypatch.setattr(strategy_jobs, "_quiet_night_post_now", lambda r, topic: pytest.fail("not written now"))
    out = strategy_jobs._draft_quiet_night_fill(models.get_restaurant(rid), {"weekday": "Tuesday"}, db)
    assert out == {}
    # A submit that failed outright still writes the post now, as before.
    monkeypatch.setattr(ai_batches, "submit",
                        lambda workflow, items, **k: {items[0]["custom_id"]: ai_batches.SUBMIT_FAILED})
    monkeypatch.setattr(strategy_jobs, "_quiet_night_post_now", lambda r, topic: {"post_draft_id": 7})
    assert strategy_jobs._draft_quiet_night_fill(models.get_restaurant(rid), {"weekday": "Tuesday"}, db) == \
        {"post_draft_id": 7}


def test_the_digest_week_is_whole_local_days_ending_yesterday(db, monkeypatch):
    """#6: the owner reads the week the email covers — the seven days ending
    yesterday, M/D/YY — and a review written today is next week's."""
    import time_utils
    rid = _rid(db)
    fixed = datetime(2026, 10, 7, 2, 0)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r=None, naive=False: fixed)
    save_reviews([
        Review(restaurant_id=rid, platform="google", external_id="w1", author="Dana Ray", rating=5,
               text="Lovely.", review_date="2026-09-30T19:00:00"),
        Review(restaurant_id=rid, platform="google", external_id="w2", author="Sam Lee", rating=4,
               text="Good.", review_date="2026-10-06T23:30:00"),
        Review(restaurant_id=rid, platform="google", external_id="w0", author="Old Ann", rating=1,
               text="Old.", review_date="2026-09-29T23:59:00"),
        Review(restaurant_id=rid, platform="google", external_id="w3", author="Lee Q", rating=3,
               text="Today.", review_date="2026-10-07T00:30:00")], db_path=db)
    report = reporter.build_report_from_db(rid, "Batch Bistro", days=7, db_path=db)
    assert (report.period_start, report.period_end) == ("9/30/26", "10/6/26")
    assert report.total_reviews == 2 and report.avg_rating == 4.5


def test_the_night_and_the_morning_build_the_same_prompt_and_the_send_reads_the_batch(db, batches, monkeypatch):
    """#6: the precompute (2am) and the send (9am) build their week on the
    same fixed edges, so a review the 8am fetch brings in from this morning
    does not make the night's answer a stranger: the prompt is byte for byte
    the same, and the send serves the batch's answer without a call."""
    import time_utils
    rid = _rid(db)
    day = date.today() + timedelta(days=1)          # tomorrow: the cutoff is ahead of the real clock
    now = {"t": datetime(day.year, day.month, day.day, 2, 0)}
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r=None, naive=False: now["t"])
    in_week = (day - timedelta(days=2)).isoformat()
    save_reviews([
        Review(restaurant_id=rid, platform="google", external_id="n1", author="Dana Ray", rating=5,
               text="Lovely dinner.", review_date=in_week),
        Review(restaurant_id=rid, platform="google", external_id="n2", author="Sam Lee", rating=4,
               text="Good tacos.", review_date=in_week)], db_path=db)
    bases = []
    real_fp = reporter.digest_fingerprint
    monkeypatch.setattr(reporter, "digest_fingerprint", lambda base, ready: bases.append(base) or real_fp(base, ready))
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: pytest.fail("the send paid again"))
    night = reporter.build_report_from_db(rid, "Batch Bistro", days=7, db_path=db)
    assert reporter.generate_ai_digest_summary(night, "Batch Bistro", "Pat", restaurant_id=rid,
                                               precompute=True) == {"_precompute": ai_batches.SUBMITTED}
    (row,) = _rows(db, "SELECT * FROM ai_batch_items WHERE workflow='weekly_digest'")
    ctx = json.loads(row["context_json"])
    reporter.on_digest_batch({"restaurant_id": rid, "context": ctx, "call_id": None},
                             message=_msg("HEADLINE: Pat, two new reviews this week."))
    # 8am: the fetch brings in a review written this morning, and drafts it.
    save_reviews([Review(restaurant_id=rid, platform="google", external_id="n3", author="Lee Q", rating=2,
                         text="Slow this morning.", review_date=f"{day.isoformat()}T07:30:00")], db_path=db)
    c = sqlite3.connect(db)
    c.execute("UPDATE reviews SET response_status='drafted', draft_response='Thanks, Lee.' WHERE external_id='n3'")
    c.commit()
    c.close()
    now["t"] = datetime(day.year, day.month, day.day, 9, 0)
    morning = reporter.build_report_from_db(rid, "Batch Bistro", days=7, db_path=db)
    sent = reporter.generate_ai_digest_summary(morning, "Batch Bistro", "Pat", restaurant_id=rid)
    assert sent["headline"] == "Pat, two new reviews this week."
    assert len(bases) == 2 and bases[0] == bases[1], "the night's prompt and the morning's differ"
    assert f"Period: {morning.period_start} to {morning.period_end}" in bases[1]
    assert morning.period_end == time_utils.mdy(day - timedelta(days=1))
