"""A model change ships with its eval (Audit #25 R13, A07-02).

On 10/17/26 every Haiku 4.5 call moved to Haiku 5.5 and every Sonnet 5 call
to Sonnet 5.5 for every restaurant; in the tests that showed up only as
changed model-name pins. Now:

  * the code's model defaults (ai_utils.MODELS, ai_workflows.
    default_tier_models, the T1 canary's previous model) must match the
    fingerprint recorded in eval_baselines/MODEL_FINGERPRINT.json;
  * every eval surface must have a MEASURED baseline on the model it runs
    on (eval_baselines/<surface>__<model>.json, scores only) — except for
    the fingerprint the tooling shipped with, whose baselines are "pending
    first run": the NEXT change needs real ones;
  * a tier whose default model changed is held to the canary restaurants
    (ai_workflows.TIER_CANARY_DEFAULT) until its window is closed.

To change a model: measure each surface on the new model on Railway
(scripts/run_ai_eval.py run --surface S --model M --baseline), commit the
baseline files, then `scripts/run_ai_eval.py fingerprint --write`.
"""
import glob
import json
import os

import pytest

import ai_eval
import ai_utils
import ai_workflows

# The fingerprint of the models in force when the eval tooling shipped
# (10/26/26). Only it may carry baselines "pending first run".
BOOTSTRAP_HASH = "afbaeb3e0004ae34"


def test_the_models_in_the_code_match_the_recorded_fingerprint_with_their_evals():
    problems = ai_eval.fingerprint_problems(ai_eval.read_fingerprint(), BOOTSTRAP_HASH)
    assert not problems, "\n".join(problems)


def test_the_recorded_fingerprint_names_each_surfaces_model():
    rec = ai_eval.read_fingerprint()
    for surface in ai_eval.SURFACES:
        assert rec["baselines"][surface]["model"] == ai_eval.code_model(surface), surface
        assert os.path.exists(ai_eval.baseline_path(surface, ai_eval.code_model(surface))), surface


def test_a_moved_call_site_default_needs_a_fresh_fingerprint_and_measured_baselines(monkeypatch):
    rec = ai_eval.read_fingerprint()
    monkeypatch.setitem(ai_utils.MODELS, "drafter", ("DRAFTER_MODEL", "claude-sonnet-6"))
    problems = ai_eval.fingerprint_problems(rec, BOOTSTRAP_HASH)
    assert any("drafter" in p and "fingerprint" in p for p in problems), problems


def test_rewriting_the_fingerprint_alone_does_not_pass_a_tier_change(monkeypatch):
    """The loophole: record the new fingerprint without measuring. The
    baselines on the new model are missing, and the hash is not the
    bootstrap's, so "pending" is no longer allowed."""
    tiers = ai_workflows.default_tier_models()
    tiers["T2"] = {"model": "claude-sonnet-6", "effort": None}
    monkeypatch.setattr(ai_workflows, "default_tier_models", lambda: json.loads(json.dumps(tiers)))
    cur = ai_eval.model_fingerprint()
    rec = dict(ai_eval.read_fingerprint(), hash=cur["hash"], models=cur["models"], tiers=cur["tiers"],
               tiers_changed=["T2"])
    problems = ai_eval.fingerprint_problems(rec, BOOTSTRAP_HASH)
    assert any("no measured baseline on claude-sonnet-6" in p for p in problems), problems
    assert any("tier T2's model changed" in p for p in problems), problems


def test_a_tier_change_is_canaried_until_its_window_closes(monkeypatch, tmp_path):
    rec = dict(ai_eval.read_fingerprint(), tiers_changed=["T1"])
    monkeypatch.setattr(ai_workflows, "TIER_CANARY_DEFAULT", "")
    assert any("TIER_CANARY_DEFAULT" in p for p in ai_eval.fingerprint_problems(rec, BOOTSTRAP_HASH))
    monkeypatch.setattr(ai_workflows, "TIER_CANARY_DEFAULT", "T1")
    assert not any("TIER_CANARY_DEFAULT" in p for p in ai_eval.fingerprint_problems(rec, BOOTSTRAP_HASH))
    monkeypatch.setattr(ai_workflows, "TIER_CANARY_DEFAULT", "")
    closed = dict(rec, canary_closed="2026-11-02")
    assert not any("TIER_CANARY_DEFAULT" in p for p in ai_eval.fingerprint_problems(closed, BOOTSTRAP_HASH))


def test_the_tier_table_reads_its_defaults_from_one_place(monkeypatch):
    for k in ("AI_TIER_T1_MODEL", "AI_TIER_T2_MODEL", "AI_TIER_T3_MODEL", "AI_TIER_T4_MODEL", "AI_TIER_CANARY"):
        monkeypatch.delenv(k, raising=False)
    table, defaults = ai_workflows._tier_table(), ai_workflows.default_tier_models()
    assert {t: v["model"] for t, v in table.items()} == {t: v["model"] for t, v in defaults.items()}


def test_every_baseline_file_holds_scores_only():
    for path in glob.glob(os.path.join(ai_eval.BASELINE_DIR, "*.json")):
        if os.path.basename(path) == os.path.basename(ai_eval.FINGERPRINT_FILE):
            continue
        body = json.load(open(path))
        assert set(body) <= set(ai_eval._BASELINE_KEYS), (path, set(body) - set(ai_eval._BASELINE_KEYS))
        assert set(body.get("scores") or {}) <= set(ai_eval._SCORE_KEYS), path
        for v in (body.get("scores") or {}).values():
            assert v is None or isinstance(v, (int, float)), (path, v)
