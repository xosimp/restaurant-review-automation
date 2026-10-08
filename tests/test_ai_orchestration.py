"""The AI orchestration layer (design owner-approved 10/7/26): the workflow
policy registry, the orchestrator's ladder / escalation / caps / depth guard /
reviewer gate, outcomes, kept requests and the learner's recommendations and
reverts."""
import json
import os
import re
import sqlite3

import pytest

import ai_learning
import ai_orchestrator as orch
import ai_utils
import ai_workflows as wf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _every_restaurant_on_the_canary(monkeypatch):
    """These tests hold each ladder as written; a canaried workflow starts on
    its cheap rung only for AI_CANARY_RESTAURANTS (context re-audit 10/7/26
    #3, held in tests/test_context_reaudit_1007.py), so every restaurant here
    is a canary restaurant."""
    monkeypatch.setenv("AI_CANARY_RESTAURANTS", "*")


@pytest.fixture
def db(db_path, monkeypatch):
    import models
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda p=None, *a, **k: real(p or db_path, *a, **k))
    orch._OVERRIDES.clear()
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 0.0)
    return db_path


# ── the registry ───────────────────────────────────────────────────────────

def test_every_policy_is_well_formed():
    for name, p in wf.POLICIES.items():
        assert p.workflow == name
        assert p.agent in wf.AGENTS, name
        assert p.purpose == "" or p.purpose in ai_utils.MODELS, (name, p.purpose)
        assert p.ladder and all(t in wf.TIERS or t == wf.DEFAULT for t in p.ladder), name
        assert all(t in wf.TRIGGERS for t in p.escalate_on), name
        assert not set(p.escalate_on) & set(wf.NEVER_TRIGGERS), name
        assert p.reviewer in wf.REVIEWERS and (p.reviewer_unattended in ("",) + wf.REVIEWERS), name
        if p.ladder != (wf.DEFAULT,):
            assert p.max_escalations <= len(p.ladder) - 1, name
        assert 0 <= p.shadow_rate <= 1
        assert 1 <= p.caps.calls <= 20 and p.caps.usd > 0


def _model_call_actions():
    """Every action= a create_with_retry call names, read from the source."""
    found = set()
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", ".claude", "tests", "node_modules", ".venv", "ios",
                                                 "public", "static", "templates", "docs", "scripts")]
        for f in files:
            if not f.endswith(".py") or f.startswith("test_"):
                continue
            src = open(os.path.join(dirpath, f), encoding="utf-8").read()
            for m in re.finditer(r"create_with_retry\(", src):
                window = src[m.end():m.end() + 900]
                a = re.search(r"""action\s*=\s*["']([a-z_]+)["']""", window)
                if a:
                    found.add(a.group(1))
    return found


def test_every_model_call_site_is_a_registered_workflow():
    missing = sorted(_model_call_actions() - set(wf.POLICIES))
    assert not missing, f"register these workflows in ai_workflows.POLICIES: {missing}"


def test_the_deliberate_non_agents_stay_model_free():
    assert set(wf.NOT_AGENTS) >= {"forecasting", "notifications", "morning_brief"}
    assert not set(wf.NOT_AGENTS) & set(wf.AGENTS)


def test_the_owner_decisions_are_in_the_policies():
    # Decision 2: guest texts and email blasts are gated by the Haiku reviewer.
    assert wf.POLICIES["guest_campaign_draft"].reviewer == "haiku_gate"
    assert wf.POLICIES["guest_newsletter_draft"].reviewer == "haiku_gate"
    # An auto-approved reply goes out with nobody reading it first.
    assert wf.POLICIES["draft_response"].reviewer_unattended == "haiku_gate"
    # Staff answers are read with no manager in between.
    assert wf.POLICIES["staff_answer"].reviewer == "haiku_gate"


def test_routes():
    tiers = wf._tier_table()
    r = wf.route_for(wf.POLICIES["labor_insight"], 0)
    assert (r.tier, r.model) == ("T1", tiers["T1"]["model"])
    r = wf.route_for(wf.POLICIES["labor_insight"], 1)
    assert r.tier == "T2"
    r = wf.route_for(wf.POLICIES["labor_insight"], 7)          # past the top: the top
    assert r.tier == "T2"
    # A "default" ladder is the call site's own model. (labor_schedule was the
    # example until 10/7/26, when it moved to (T3, T4) - owner decision 3.)
    d = wf.route_for(wf.POLICIES["invoice_extract"], 0)
    assert d.tier == wf.DEFAULT and d.model == ai_utils.model_for("invoices")
    s = wf.route_for(wf.POLICIES["labor_schedule"], 0)
    assert (s.tier, s.model, s.effort) == ("T3", tiers["T3"]["model"], tiers["T3"]["effort"])
    kw = {"model": "x", "thinking": {"type": "adaptive"}, "output_config": {"effort": "high", "format": {"a": 1}}}
    assert d.apply(kw) == kw                                   # the call site's own call, untouched
    t3 = wf.Route("T3", "claude-sonnet-5-5", "medium").apply(dict(kw))
    assert t3["model"] == "claude-sonnet-5-5" and t3["thinking"] == {"type": "adaptive"}
    assert t3["output_config"] == {"effort": "medium", "format": {"a": 1}}
    t2 = wf.Route("T2", "claude-sonnet-5").apply(dict(kw))
    assert "thinking" not in t2 and t2["output_config"] == {"format": {"a": 1}}


def test_a_route_skips_rungs_below_a_pre_routed_start():
    pol = wf.POLICIES["draft_response"]
    assert wf.rungs(pol) == 2 and wf.rungs(pol, start="T2") == 1
    assert wf.route_for(pol, 0, start="T2").tier == "T2"


def test_overrides_are_validated():
    base = wf.POLICIES["labor_insight"]
    assert wf.apply_override(base, {"ladder": ["T2"]}).ladder == ("T2",)
    for bad in ({"ladder": ["T9"]}, {"escalate_on": ["data_missing"]}, {"agent": "x"},
                {"max_escalations": 5}, {"reviewer": "someone"}, {"caps": {"calls": 0}}):
        with pytest.raises(ValueError):
            wf.apply_override(base, bad)


def test_an_override_applies_and_reverts(db):
    orch.set_override("labor_insight", {"ladder": ["T2"]}, actor="will", db_path=db)
    assert wf.policy("labor_insight", db).ladder == ("T2",)
    ch = orch.set_override("labor_insight", None, actor="will", db_path=db)
    assert ch["before"] == {"ladder": ["T2"]} and ch["after"] is None
    assert wf.policy("labor_insight", db).ladder == ("T1", "T2")
    with pytest.raises(ValueError):
        orch.set_override("nope", {"ladder": ["T1"]}, db_path=db)


# ── a run ──────────────────────────────────────────────────────────────────

def _runs(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM ai_runs ORDER BY created_at, rowid")]
    finally:
        conn.close()


def test_a_passing_run_uses_the_first_rung_and_is_recorded(db):
    seen = []
    rr = orch.generate("labor_insight", 7, attempt=lambda route, notes: seen.append((route.tier, notes)) or "ok",
                       check=lambda r: orch.Verdict.passed(), subject="labor:10/7", db_path=db)
    assert rr.ok and rr.result == "ok" and rr.tier == "T1" and rr.escalations == 0
    assert seen == [("T1", [])]
    (row,) = _runs(db)
    assert row["workflow"] == "labor_insight" and row["status"] == "ok" and row["final_tier"] == "T1"
    assert row["policy_version"] == wf.POLICY_VERSION and row["subject"] == "labor:10/7"


def test_a_refused_answer_climbs_one_rung_with_the_reasons(db):
    seen = []

    def attempt(route, notes):
        seen.append((route.tier, notes))
        return route.tier

    def check(result):
        return orch.Verdict.passed() if result == "T2" else orch.Verdict.failed("validation_refuse",
                                                                                  "an invented figure")
    rr = orch.generate("labor_insight", 7, attempt=attempt, check=check, db_path=db)
    assert rr.ok and rr.result == "T2" and rr.escalations == 1
    assert seen == [("T1", []), ("T2", ["an invented figure"])]
    assert json.loads(_runs(db)[0]["steps_json"])[0]["trigger"] == "validation_refuse"


def test_missing_data_and_untriggered_failures_never_escalate(db):
    calls = []
    rr = orch.generate("labor_insight", 7, attempt=lambda r, n: calls.append(r.tier) or "x",
                       check=lambda r: orch.Verdict.failed("data_missing", "no timecards"), db_path=db)
    assert calls == ["T1"] and rr.status == "failed" and not rr.ok
    calls.clear()
    orch.generate("inventory_insight", 7, attempt=lambda r, n: calls.append(r.tier) or "x",
                  check=lambda r: orch.Verdict.failed("validation_refuse"), db_path=db)
    assert calls == ["T2"]                                   # a one-rung ladder never climbs


def test_data_not_ready_is_a_hold_and_propagates(db):
    def attempt(route, notes):
        raise ai_utils.DataNotReady({"decision": "refuse", "reason": "stale"})
    with pytest.raises(ai_utils.DataNotReady):
        orch.generate("labor_insight", 7, attempt=attempt, db_path=db)
    assert _runs(db)[0]["status"] == "held"


def test_an_escalated_attempt_that_raises_keeps_the_first_answer(db):
    def attempt(route, notes):
        if route.tier == "T2":
            raise RuntimeError("overloaded")
        return "first"
    rr = orch.generate("labor_insight", 7, attempt=attempt,
                       check=lambda r: orch.Verdict.failed("validation_refuse", "x"), db_path=db)
    assert rr.result == "first" and rr.status == "failed"


def test_a_run_stops_at_its_dollar_cap(db, monkeypatch):
    monkeypatch.setattr(orch, "_spent", lambda run_id, db_path=None: (99.0, 0, 0, 1))
    calls = []
    rr = orch.generate("labor_insight", 7, attempt=lambda r, n: calls.append(r.tier) or "x",
                       check=lambda r: orch.Verdict.failed("validation_refuse", "x"), db_path=db)
    assert calls == ["T1"] and rr.status == "capped"


def test_workflows_cannot_nest_past_the_depth_limit(db):
    def deep(route, notes):
        return orch.generate("labor_insight", 7, attempt=inner, db_path=db).result

    def inner(route, notes):
        return orch.generate("marketing_insight", 7, attempt=lambda r, n: "leaf", db_path=db).result
    with pytest.raises(orch.RunRefused):
        orch.generate("review_insight", 7, attempt=deep, db_path=db)
    rows = _runs(db)
    assert any(r["status"] == "refused" for r in rows)
    child = [r for r in rows if r["workflow"] == "labor_insight"]
    assert child and child[0]["parent_run_id"]


def test_the_reviewer_gate_flags_and_the_run_escalates(db):
    seen = []

    def attempt(route, notes):
        seen.append(route.tier)
        return f"text on {route.tier}"

    def review(result, mode):
        assert mode == "haiku_gate"
        if result.endswith("T2"):
            return orch.Verdict.failed("reviewer_flag", "reads like an ad")
        return orch.Verdict(ok=True, score=0.9)
    rr = orch.generate("guest_campaign_draft", 7, attempt=attempt, check=lambda r: orch.Verdict.passed(),
                       review=review, db_path=db)
    assert seen == ["T2", "T3"] and rr.ok and rr.result == "text on T3"
    row = _runs(db)[0]
    assert row["reviewer"] == "haiku_gate" and row["reviewer_score"] == 0.9


def test_an_unattended_reply_gets_the_gate_an_approved_one_does_not(db, monkeypatch):
    # The shadow sample (draft_response scores 10% in the background) is off
    # here: this is about the gate.
    monkeypatch.setenv("AI_SHADOW_REVIEW", "0")
    modes = []

    def review(result, mode):
        modes.append(mode)
        return orch.Verdict(ok=True, score=0.8)
    orch.generate("draft_response", 7, attempt=lambda r, n: "reply", review=review, db_path=db)
    assert modes == []                                       # the owner approves it
    orch.generate("draft_response", 7, attempt=lambda r, n: "reply", review=review, unattended=True, db_path=db)
    assert modes == ["haiku_gate"]


def test_outcomes_land_on_the_subjects_newest_run(db):
    orch.generate("draft_response", 7, attempt=lambda r, n: "a", subject="review:1", db_path=db)
    assert orch.record_outcome("draft_response", 7, "review:1", "edited",
                               quality=orch.edit_quality("Thanks so much!", "Thanks so much, Ana!"), db_path=db)
    row = orch.subject_run("draft_response", 7, "review:1", db_path=db)
    assert row["outcome"] == "edited" and 0.7 < row["outcome_quality"] < 1
    assert not orch.record_outcome("draft_response", 7, "review:2", "accepted", db_path=db)
    assert not orch.record_outcome("draft_response", 7, "review:1", "loved", db_path=db)


def test_validation_verdict_words_map_to_triggers():
    assert orch.verdict_from_validation("refuse").trigger == "validation_refuse"
    assert orch.verdict_from_validation("withhold").trigger == "validation_withhold"
    assert orch.verdict_from_validation("caveat").ok


def test_a_sampled_run_keeps_its_request_redacted(db, monkeypatch):
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 1.0)

    def attempt(route, notes):
        orch.keep_request({"model": route.model, "max_tokens": 100,
                           "messages": [{"role": "user", "content": "call me at 312-555-0199 about the party"}]},
                          db_path=db)
        return "x"
    orch.generate("draft_response", 7, attempt=attempt, db_path=db)
    (run_id, rid, req), = orch.kept_requests("draft_response", db_path=db)
    assert rid == 7 and "312-555-0199" not in json.dumps(req)
    # Never for a tool loop or the schedule.
    assert "ask_cavnar" in orch.NOT_REPLAYABLE and "labor_schedule" in orch.NOT_REPLAYABLE


# ── the learner ────────────────────────────────────────────────────────────

def _fake_run(db, workflow, *, tier="T1", escalations=0, outcome=None, quality=None, days_ago=1, shadow_of=None,
              score=None, cost=0.01, run_id=None):
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO ai_runs (run_id, workflow, restaurant_id, final_tier, final_model, status, "
                     "escalations, latency_ms, cost_usd, outcome, outcome_quality, created_at, shadow_of, "
                     "reviewer_score) VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now', ?),?,?)",
                     (run_id or orch.new_run_id(workflow), workflow, 7, tier, "m", "ok", escalations, 1000, cost,
                      outcome, quality, f"-{days_ago} days", shadow_of, score))
        conn.commit()
    finally:
        conn.close()


def test_a_ladder_that_mostly_escalates_is_recommended_to_start_higher(db):
    for i in range(30):
        _fake_run(db, "labor_insight", tier="T2" if i < 12 else "T1", escalations=1 if i < 12 else 0)
    made = ai_learning.recommend(db_path=db)
    assert any("labor_insight" in m and "start there" in m for m in made)
    (rec,) = [r for r in ai_learning.recommendations(db_path=db) if r["kind"] == "start_higher"]
    assert json.loads(rec["proposed_json"]) == {"ladder": ["T2"]}
    # Nothing on a quiet workflow.
    for i in range(5):
        _fake_run(db, "marketing_insight", escalations=1)
    assert not any("marketing_insight" in m for m in ai_learning.recommend(db_path=db))
    # Applying it is an override; recommend-only means nothing changed before.
    assert wf.policy("labor_insight", db).ladder == ("T1", "T2")
    ai_learning.decide(rec["id"], True, actor="will", db_path=db)
    assert wf.policy("labor_insight", db).ladder == ("T2",)


def test_a_cheaper_tier_that_scores_as_well_in_shadow_is_recommended(db):
    for i in range(25):
        pid = f"run:p{i}"
        _fake_run(db, "review_insight", tier="T2", score=0.86, cost=0.010, run_id=pid)
        _fake_run(db, "review_insight", tier="T1", score=0.84, cost=0.004, shadow_of=pid)
    made = ai_learning.recommend(db_path=db)
    assert any("review_insight" in m and "start on T1" in m for m in made)


def test_an_override_that_hurts_acceptance_is_reverted(db):
    for _ in range(15):
        _fake_run(db, "labor_insight", outcome="accepted", quality=1.0, days_ago=10)
    orch.set_override("labor_insight", {"ladder": ["T1"]}, actor="will", db_path=db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE ai_route_overrides SET updated_at=datetime('now','-5 days')")
    conn.commit()
    conn.close()
    orch._OVERRIDES.clear()
    for _ in range(12):
        _fake_run(db, "labor_insight", outcome="rejected", quality=0.0, days_ago=2)
    assert ai_learning.revert_regressions(db_path=db) == ["labor_insight"]
    assert wf.policy("labor_insight", db).ladder == ("T1", "T2")
    assert any(r["kind"] == "reverted" for r in ai_learning.recommendations("applied", db_path=db))
