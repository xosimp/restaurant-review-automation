"""Audit #25 R13 — the AI evaluation sets (ai_eval, scripts/run_ai_eval.py):
freeze stored production inputs (scrubbed, learning-eligible restaurants
only), replay them through an injected model, score every answer with the
deterministic checks, write baselines that hold scores only, the acceptance
thresholds, and the weekly smoke job (off unless AI_SMOKE_EVAL=1).

No test here reaches a model: every answer is a stub passed as `send`.
"""
import json
import os
import sqlite3
import types

import pytest

import ai_eval
import ai_orchestrator
import jobs_registry
import models


@pytest.fixture(autouse=True)
def db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH
    monkeypatch.setattr(models, "get_conn", lambda path=None, *a, **k: real(db_path if path in (None, default) else path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.delenv("AI_SMOKE_EVAL", raising=False)
    yield db_path


def _rid(name="Harbor Grill", **kw):
    return models.create_restaurant(models.Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test", **kw))


def _msg(text, stop="end_turn", tin=1000, tout=200):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason=stop,
                                 usage=types.SimpleNamespace(input_tokens=tin, output_tokens=tout,
                                                             cache_creation_input_tokens=0,
                                                             cache_read_input_tokens=0))


REPLY_PROMPT = ("Write a reply to this review. Guest: Ann Smith (ann.smith@example.com). "
                "Review: The brisket was dry and the wait was 40 minutes. Our average ticket is $32.")


def _keep(db_path, rid, run_id, prompt=REPLY_PROMPT, workflow="draft_response"):
    ai_orchestrator._store_request(run_id, 1, workflow, rid, "T2",
                                   {"model": "claude-sonnet-5-5", "max_tokens": 600, "system": "You reply to reviews.",
                                    "messages": [{"role": "user", "content": prompt}]}, db_path=db_path)


# ── freeze ──────────────────────────────────────────────────────────────────

def test_freeze_copies_kept_requests_scrubbed_and_skips_demo_accounts(db):
    rid, demo = _rid(), _rid("Sample Bistro", is_demo=1)
    _keep(db, rid, "run:a")
    _keep(db, rid, "run:b")
    _keep(db, demo, "run:c")
    out = ai_eval.freeze("review_reply", n=25, version="v-test")
    assert out["frozen"] == 2 and out["skipped"]["not_eligible"] == 1
    items = ai_eval.load_items("review_reply", version="v-test")
    assert {i["item_key"] for i in items} == {"run:run:a", "run:run:b"}
    text = json.dumps(items[0]["request"])
    assert "ann.smith@example.com" not in text and "brisket" in text, "re-scrubbed like the trace"
    # Freezing again adds nothing.
    assert ai_eval.freeze("review_reply", n=25, version="v-test")["already"] == 2


def test_freeze_reads_asks_final_calls_from_the_trace(db):
    import zlib
    rid = _rid()
    prompt = "[system]\nYou are Cavnar AI.\n\n[user]\nHow were sales last week?\n\n[assistant]\nLet me look.\n\n" \
             "[user]\nTool result: net sales $12,400.\n\n[tools: 3] read_sales, read_labor, read_reviews"
    c = sqlite3.connect(db)
    try:
        c.execute("INSERT INTO ai_calls (call_id, restaurant_id, action, model, outcome, stop_reason, prompt_z) "
                  "VALUES ('c1', ?, 'ask_cavnar', 'claude-sonnet-5-5', 'ok', 'end_turn', ?)",
                  (rid, zlib.compress(prompt.encode())))
        c.commit()
    finally:
        c.close()
    assert ai_eval.freeze("ask", n=5, version="v1")["frozen"] == 1
    (item,) = ai_eval.load_items("ask", version="v1")
    req = item["request"]
    assert req["system"] == "You are Cavnar AI."
    assert [m["role"] for m in req["messages"]] == ["user", "assistant", "user"]
    assert "[tools" not in req["messages"][-1]["content"] and "$12,400" in req["messages"][-1]["content"]


def test_an_invoice_item_holds_the_owner_confirmed_costs_never_the_scan(db):
    rid = _rid()
    c = sqlite3.connect(db)
    try:
        c.execute("INSERT INTO invoice_imports (restaurant_id, supplier, image_sha, lines_json, applied_json) "
                  "VALUES (?, 'Sysco', 'sha-1', ?, ?)",
                  (rid, json.dumps({"lines": [{"description": "Brisket"}], "total_check": {"invoice_total": 412.5}}),
                   json.dumps([{"ingredient_id": 7, "new_cost": 5.25, "by": "owner"},
                               {"ingredient_id": 8, "new_cost": 1.1, "by": "trusted_supplier"}])))
        c.commit()
    finally:
        c.close()
    assert ai_eval.freeze("invoice_extract", n=5, version="v1")["frozen"] == 1
    (item,) = ai_eval.load_items("invoice_extract", version="v1")
    assert item["request"] is None
    assert item["reference"]["confirmed"] == {"7": 5.25} and item["reference"]["image_sha"] == "sha-1"
    with pytest.raises(ValueError, match="--files"):
        ai_eval.run("invoice_extract", send=lambda kw: pytest.fail("no call without the scan"))


# ── scoring ─────────────────────────────────────────────────────────────────

def test_run_scores_refusals_and_untraced_figures(db):
    rid = _rid()
    for i in range(4):
        _keep(db, rid, f"run:{i}")
    ai_eval.freeze("review_reply", n=4, version="v1")
    answers = iter([
        _msg("Thank you for telling us about the brisket. We are sorry about the 40-minute wait."),  # clean
        _msg("We have cut our wait times by 35% since then."),                                        # untraced 35%
        _msg("", stop="refusal"),                                                                     # refused
        _msg("Thank you for your visit.", stop="max_tokens"),                                         # truncated
    ])
    sent = []

    def send(kw):
        sent.append(kw)
        return next(answers)
    res = ai_eval.run("review_reply", model="claude-test-model", send=send)
    s = res["scores"]
    assert res["model"] == "claude-test-model" and all(k["model"] == "claude-test-model" for k in sent)
    assert s["scored"] == 4 and s["errors"] == 0
    # The provider's refusal, the truncation, and the validator refusing the
    # public reply's invented 35% (F1) — the owner gets none of the three.
    assert s["refusal_rate"] == 0.75, s
    assert s["truncated_rate"] == 0.25 and s["rv_caught_rate"] == 0.5
    assert s["figure_mismatch_rate"] == 0.25 and s["figures_per_item"] == 0.25
    assert s["cost_usd"] > 0


def test_a_provider_failure_is_an_error_never_a_refusal(db):
    rid = _rid()
    _keep(db, rid, "run:1")
    ai_eval.freeze("review_reply", n=1, version="v1")

    def send(kw):
        raise RuntimeError("overloaded")
    s = ai_eval.run("review_reply", send=send)["scores"]
    assert s["errors"] == 1 and s["scored"] == 0 and "refusal_rate" not in s


def test_the_dsr_and_analysis_answers_are_schema_checked():
    req = {"messages": [{"role": "user", "content": "Net sales were $19,850. Labor ran 24.2%."}]}
    good = json.dumps({"executive_summary": {"text": "Net sales were $19,850 and labor ran 24.2%.",
                                             "cites": ["sales.net", "labor.pct"]}})
    assert ai_eval.score_item("dsr_narrative", req, _msg(good))["schema_fail"] is False
    bad = ai_eval.score_item("dsr_narrative", req, _msg("Here is the night: great."))
    assert bad["schema_fail"] and bad["refused"]
    invented = json.dumps({"executive_summary": {"text": "Net sales were $24,000.", "cites": ["sales.net"]}})
    assert ai_eval.score_item("dsr_narrative", req, _msg(invented))["figures"] == 1
    ok = json.dumps({"sentiment": "negative", "urgency": "normal", "categories": ["food_quality"],
                     "summary": "Dry brisket and a long wait."})
    assert ai_eval.score_item("review_analysis", req, _msg(ok))["schema_fail"] is False
    assert ai_eval.score_item("review_analysis", req, _msg(json.dumps({"sentiment": "furious"})))["schema_fail"]


# ── baselines and acceptance ────────────────────────────────────────────────

def test_a_baseline_holds_scores_only(db, tmp_path):
    rid = _rid()
    _keep(db, rid, "run:1")
    ai_eval.freeze("review_reply", n=1, version="v1")
    res = ai_eval.run("review_reply", model="claude-test-model",
                      send=lambda kw: _msg("Ann, thank you — the brisket at $32 should not be dry."))
    path = ai_eval.write_baseline(res, path=str(tmp_path / "b.json"))
    raw = open(path).read()
    body = json.loads(raw)
    assert set(body) <= set(ai_eval._BASELINE_KEYS) and set(body["scores"]) <= set(ai_eval._SCORE_KEYS)
    assert body["status"] == "measured" and body["n"] == 1 and body["set_version"] == "v1"
    for word in ("brisket", "Ann", "ann.smith", "dry", "Harbor"):
        assert word not in raw, f"customer text {word!r} in a baseline (the repo is public)"


def test_acceptance_thresholds():
    base = {"scores": {"refusal_rate": 0.10, "figure_mismatch_rate": 0.05, "line_agreement": 0.90}}
    assert ai_eval.acceptance({"refusal_rate": 0.12, "figure_mismatch_rate": 0.05, "line_agreement": 0.9}, base) == []
    fails = ai_eval.acceptance({"refusal_rate": 0.13, "figure_mismatch_rate": 0.06, "line_agreement": 0.89}, base)
    assert len(fails) == 3 and "refusal rate" in fails[0] and "figure" in fails[1] and "line agreement" in fails[2]
    assert ai_eval.acceptance({"refusal_rate": 0.5}, {"status": "pending first run"}) == []


# ── the weekly smoke ────────────────────────────────────────────────────────

def test_the_smoke_eval_is_off_unless_asked(db):
    out = ai_eval.run_smoke_eval(send=lambda kw: pytest.fail("off by default"))
    assert out["attempted"] == 0 and out["off"] == 1
    spec = jobs_registry.JOBS["ai_smoke_eval"]
    assert spec["sends"] is False and spec["target"] == ("ai_eval", "run_smoke_eval") and not spec.get("retry")


def test_the_smoke_eval_records_scores_and_deviations_within_its_budget(db, monkeypatch, tmp_path):
    monkeypatch.setenv("AI_SMOKE_EVAL", "1")
    monkeypatch.setenv("AI_SMOKE_EVAL_ITEMS", "3")
    monkeypatch.setattr(ai_eval, "BASELINE_DIR", str(tmp_path))
    rid = _rid()
    for i in range(3):
        _keep(db, rid, f"run:{i}")
    ai_eval.freeze("review_reply", n=3, version="v1")
    model = ai_eval.production_model("review_reply")
    with open(ai_eval.baseline_path("review_reply", model), "w") as f:
        json.dump({"surface": "review_reply", "model": model, "status": "measured",
                   "scores": {"scored": 3, "refusal_rate": 0.0, "figure_mismatch_rate": 0.0}}, f)
    out = ai_eval.run_smoke_eval(send=lambda kw: _msg("", stop="refusal"))
    assert out["attempted"] == 1 and out["ok"] == 1 and out["deviated"] == 1
    assert out["skipped"] == len(ai_eval.SMOKE_SURFACES) - 1, "surfaces with no frozen set are skipped"
    runs = ai_eval.latest_runs("smoke")
    assert runs["review_reply"]["scores"]["refusal_rate"] == 1.0
    assert "refusal rate" in runs["review_reply"]["scores"]["deviations"][0]


def test_the_smoke_budget_stops_before_the_next_call(db, monkeypatch):
    monkeypatch.setenv("AI_SMOKE_EVAL", "1")
    monkeypatch.setenv("AI_SMOKE_EVAL_BUDGET_USD", "0.000001")
    rid = _rid()
    for i in range(3):
        _keep(db, rid, f"run:{i}")
    ai_eval.freeze("review_reply", n=3, version="v1")
    calls = []
    out = ai_eval.run_smoke_eval(send=lambda kw: calls.append(1) or _msg("Thanks for visiting."))
    assert len(calls) == 1 and out["hit_bound"] is True
