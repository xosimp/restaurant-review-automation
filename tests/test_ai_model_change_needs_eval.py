"""A model or prompt change ships with its eval (Audit #25 R13, A07-02;
re-audit B7 P1 / P2, 10/27/26).

On 10/17/26 every Haiku 4.5 call moved to Haiku 5.5 and every Sonnet 5 call
to Sonnet 5.5 for every restaurant; in the tests that showed up only as
changed model-name pins. The first gate checked that a baseline FILE
existed: a model refusing every answer wrote one and passed, and a new model
was never compared with the one before it. Now:

  * the code's model defaults (ai_utils.MODELS, ai_workflows.
    default_tier_models, the T1 canary's previous model) and each eval
    surface's prompt template (ai_eval.template_hash) must match the
    fingerprint recorded in eval_baselines/MODEL_FINGERPRINT.json;
  * every eval surface must have a MEASURED baseline on the model and the
    prompt it runs on, whose VERDICT holds (ai_eval.baseline_problems): the
    absolute floors, and — after a change — a comparison on the same frozen
    set with the baseline in force before it that passes acceptance (or a
    recorded reason for accepting the regression);
  * except while a surface's model and prompt are still the ones the
    tooling shipped with (BOOTSTRAP_SURFACES), whose baselines are "pending
    first run": the NEXT change of that surface needs real ones;
  * a tier whose default model changed is held to the canary restaurants
    (ai_workflows.TIER_CANARY_DEFAULT) until its window is closed.

To change a model: measure the surface's model in force on Railway
(scripts/run_ai_eval.py run --surface S --baseline), then the new one on
the same set (run --surface S --model M --baseline), commit the files, then
`scripts/run_ai_eval.py fingerprint --write`. To change a prompt: export it
(template --surface S), measure it on Railway (run --surface S --candidate
FILE --baseline), commit, `fingerprint --write`.
"""
import glob
import json
import os

import pytest

import ai_eval
import ai_utils
import ai_workflows

# The models and prompt templates in force when the eval gate shipped
# (10/26/26, templates added 10/27/26): only a surface still on BOTH may
# carry a baseline "pending first run". A merge that changes one of these
# prompts before any surface is measured re-pins it here in the same commit
# (ai_eval.template_hash(surface)); after the first measurement, never.
BOOTSTRAP_SURFACES = {
    "dsr_narrative": {"model": "claude-sonnet-5-5", "template": "078b234610df15ea"},
    "review_reply": {"model": "claude-sonnet-5-5", "template": "058e1de09a0952b1"},
    "ask": {"model": "claude-sonnet-5-5", "template": "44c3b3ae219451f1"},
    "invoice_extract": {"model": "claude-opus-5-5", "template": "6a4d8921338a934a"},
    "review_analysis": {"model": "claude-haiku-5-5", "template": "4391d153172ff25f"},
}


def test_the_models_in_the_code_match_the_recorded_fingerprint_with_their_evals():
    problems = ai_eval.fingerprint_problems(ai_eval.read_fingerprint(), BOOTSTRAP_SURFACES)
    assert not problems, "\n".join(problems)


def test_the_recorded_fingerprint_names_each_surfaces_model_and_prompt():
    rec = ai_eval.read_fingerprint()
    for surface in ai_eval.SURFACES:
        assert rec["baselines"][surface]["model"] == ai_eval.code_model(surface), surface
        assert rec["baselines"][surface]["template"] == ai_eval.template_hash(surface), surface
        assert os.path.exists(ai_eval.baseline_path(surface, ai_eval.code_model(surface))), surface


def test_a_moved_call_site_default_needs_a_fresh_fingerprint_and_measured_baselines(monkeypatch):
    rec = ai_eval.read_fingerprint()
    monkeypatch.setitem(ai_utils.MODELS, "drafter", ("DRAFTER_MODEL", "claude-sonnet-6"))
    problems = ai_eval.fingerprint_problems(rec, BOOTSTRAP_SURFACES)
    assert any("drafter" in p and "fingerprint" in p for p in problems), problems


def test_rewriting_the_fingerprint_alone_does_not_pass_a_tier_change(monkeypatch):
    """The loophole: record the new fingerprint without measuring. The
    baselines on the new model are missing, and the surfaces are no longer
    on their bootstrap model, so "pending" is no longer allowed."""
    tiers = ai_workflows.default_tier_models()
    tiers["T2"] = {"model": "claude-sonnet-6", "effort": None}
    monkeypatch.setattr(ai_workflows, "default_tier_models", lambda: json.loads(json.dumps(tiers)))
    cur = ai_eval.model_fingerprint()
    rec = dict(ai_eval.read_fingerprint(), hash=cur["hash"], models=cur["models"], tiers=cur["tiers"],
               tiers_changed=["T2"])
    problems = ai_eval.fingerprint_problems(rec, BOOTSTRAP_SURFACES)
    assert any("no measured baseline on claude-sonnet-6" in p for p in problems), problems
    assert any("tier T2's model changed" in p for p in problems), problems
    # A surface the change did not touch stays allowed on its bootstrap baseline.
    assert not any(p.startswith("review_analysis") for p in problems), problems


def test_a_tier_change_is_canaried_until_its_window_closes(monkeypatch, tmp_path):
    rec = dict(ai_eval.read_fingerprint(), tiers_changed=["T1"])
    monkeypatch.setattr(ai_workflows, "TIER_CANARY_DEFAULT", "")
    assert any("TIER_CANARY_DEFAULT" in p for p in ai_eval.fingerprint_problems(rec, BOOTSTRAP_SURFACES))
    monkeypatch.setattr(ai_workflows, "TIER_CANARY_DEFAULT", "T1")
    assert not any("TIER_CANARY_DEFAULT" in p for p in ai_eval.fingerprint_problems(rec, BOOTSTRAP_SURFACES))
    monkeypatch.setattr(ai_workflows, "TIER_CANARY_DEFAULT", "")
    closed = dict(rec, canary_closed="2026-11-02")
    assert not any("TIER_CANARY_DEFAULT" in p for p in ai_eval.fingerprint_problems(closed, BOOTSTRAP_SURFACES))


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
        ref = body.get("reference") or {}
        assert set(ref) <= set(ai_eval._REFERENCE_KEYS), path
        for v in (ref.get("scores") or {}).values():
            assert v is None or isinstance(v, (int, float)), (path, v)


# ── the verdicts (re-audit B7 P1): a temporary baseline store ───────────────

def _scores(refusal=0.04, scored=20, errors=0, fig=0.05, line=None):
    s = {"n": scored + errors, "scored": scored, "errors": errors, "refusal_rate": refusal,
         "figure_mismatch_rate": fig}
    if line is not None:
        s["line_agreement"] = line
    return s


def _write(surface, model, scores, set_version="v1", template=None, reference=None, accepted=None):
    res = {"surface": surface, "model": model, "version": set_version,
           "template": template or ai_eval.template_hash(surface), "scores": scores}
    return ai_eval.write_baseline(res, reference=reference, accepted_regression=accepted)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Every surface measured on its code model and prompt (set v1), and the
    fingerprint recorded over them."""
    from scripts import run_ai_eval
    monkeypatch.setattr(ai_eval, "BASELINE_DIR", str(tmp_path))
    monkeypatch.setattr(ai_eval, "FINGERPRINT_FILE", str(tmp_path / "MODEL_FINGERPRINT.json"))
    for s in ai_eval.SURFACES:
        _write(s, ai_eval.code_model(s), _scores(line=0.9 if s == "invoice_extract" else None))
    run_ai_eval._write(ai_eval.FINGERPRINT_FILE, run_ai_eval._fingerprint_record({}))
    assert ai_eval.fingerprint_problems(ai_eval.read_fingerprint(), {}) == []
    return tmp_path


def _move_invoice_model(monkeypatch, model="claude-opus-6"):
    monkeypatch.setitem(ai_utils.MODELS, "invoices", ("INVOICE_MODEL", model))
    return model


def _record_change():
    from scripts import run_ai_eval
    run_ai_eval._write(ai_eval.FINGERPRINT_FILE, run_ai_eval._fingerprint_record(ai_eval.read_fingerprint()))
    return ai_eval.read_fingerprint()


def test_a_new_model_that_refuses_more_than_the_model_before_it_fails_the_gate(store, monkeypatch):
    old = ai_eval.code_model("invoice_extract")
    new = _move_invoice_model(monkeypatch)
    ref = {"model": old, "template": ai_eval.template_hash("invoice_extract"), "set_version": "v1", "n": 20,
           "scores": _scores(refusal=0.0, line=0.9)}
    _write("invoice_extract", new, _scores(refusal=0.5 / 2.5, line=0.9), reference=ref)   # 20% against 0%
    rec = _record_change()
    assert rec["baselines"]["invoice_extract"]["previous"]["model"] == old
    problems = ai_eval.fingerprint_problems(rec, {})
    assert any(p.startswith("invoice_extract") and "refusal rate 20.0% against the baseline's 0.0%" in p
               for p in problems), problems
    # The same scores against a reference that refused as much pass.
    _write("invoice_extract", new, _scores(refusal=0.2, line=0.9), reference=dict(ref, scores=_scores(0.19, line=0.9)))
    assert ai_eval.fingerprint_problems(ai_eval.read_fingerprint(), {}) == []


def test_a_new_model_measured_with_no_comparison_fails_the_gate(store, monkeypatch):
    new = _move_invoice_model(monkeypatch)
    _write("invoice_extract", new, _scores(refusal=0.0, line=0.95))          # no reference at all
    problems = ai_eval.fingerprint_problems(_record_change(), {})
    assert any("not compared with the baseline in force before the change" in p for p in problems), problems


def test_a_comparison_on_another_set_or_item_count_is_no_comparison(store, monkeypatch):
    old = ai_eval.code_model("invoice_extract")
    new = _move_invoice_model(monkeypatch)
    ref = {"model": old, "template": ai_eval.template_hash("invoice_extract"), "set_version": "v1", "n": 20,
           "scores": _scores(line=0.9)}
    _write("invoice_extract", new, _scores(line=0.9), set_version="v2", reference=ref)
    problems = ai_eval.fingerprint_problems(_record_change(), {})
    assert any("baseline on set v1, measured on set v2" in p for p in problems), problems
    _write("invoice_extract", new, _scores(scored=12, line=0.9), reference=ref)
    problems = ai_eval.fingerprint_problems(ai_eval.read_fingerprint(), {})
    assert any("measured on 12 items against the reference's 20" in p for p in problems), problems


def test_a_baseline_refusing_everything_never_passes_even_as_a_first_measurement(store):
    s = "review_analysis"
    _write(s, ai_eval.code_model(s), _scores(refusal=1.0))
    problems = ai_eval.fingerprint_problems(ai_eval.read_fingerprint(), {})
    assert any(p.startswith(s) and "refusal rate 100.0%" in p for p in problems), problems
    _write(s, ai_eval.code_model(s), _scores(refusal=0.0, scored=2, errors=18))
    problems = ai_eval.fingerprint_problems(ai_eval.read_fingerprint(), {})
    assert any("only 2 answers scored" in p for p in problems) and any("failed at the provider" in p
                                                                      for p in problems), problems


def test_an_accepted_regression_passes_only_with_its_reason_and_never_an_invalid_run(store, monkeypatch):
    old = ai_eval.code_model("invoice_extract")
    new = _move_invoice_model(monkeypatch)
    ref = {"model": old, "template": ai_eval.template_hash("invoice_extract"), "set_version": "v1", "n": 20,
           "scores": _scores(refusal=0.0, line=0.95)}
    _write("invoice_extract", new, _scores(refusal=0.1, line=0.9), reference=ref,
           accepted="Opus 6 reads handwritten totals the old model skipped; the misses are faint thermal paper")
    assert ai_eval.fingerprint_problems(_record_change(), {}) == []
    _write("invoice_extract", new, _scores(refusal=0.1, scored=4, errors=16, line=0.9), reference=ref,
           accepted="no reason makes an outage a measurement")
    assert any("failed at the provider" in p for p in ai_eval.fingerprint_problems(ai_eval.read_fingerprint(), {}))


def test_editing_a_surfaces_system_text_fails_the_gate_until_it_is_measured(store, monkeypatch):
    """Re-audit B7 P2: prompt edits were ungated."""
    from dsr import narrative
    s = "dsr_narrative"
    old_tpl = ai_eval.template_hash(s)
    monkeypatch.setattr(narrative, "SYSTEM_PROMPT", narrative.SYSTEM_PROMPT + "\nNever lead with the weather.")
    new_tpl = ai_eval.template_hash(s)
    assert new_tpl != old_tpl
    problems = ai_eval.fingerprint_problems(ai_eval.read_fingerprint(), {})
    assert any("the dsr_narrative prompt" in p for p in problems), problems
    assert any(p.startswith("dsr_narrative: its baseline was measured on prompt template " + old_tpl) for p in problems)
    # Bootstrapping does not cover an edited prompt either.
    boot = {s: {"model": ai_eval.code_model(s), "template": old_tpl}}
    os.remove(ai_eval.baseline_path(s, ai_eval.code_model(s)))
    with open(ai_eval.baseline_path(s, ai_eval.code_model(s)), "w") as f:
        json.dump({"surface": s, "model": ai_eval.code_model(s), "status": ai_eval.PENDING}, f)
    assert any("no measured baseline" in p for p in ai_eval.fingerprint_problems(ai_eval.read_fingerprint(), boot))
    # Measured on the new prompt against the old on the same set: passes once recorded.
    ref = {"model": ai_eval.code_model(s), "template": old_tpl, "set_version": "v1", "n": 20, "scores": _scores()}
    _write(s, ai_eval.code_model(s), _scores(), template=new_tpl, reference=ref)
    assert ai_eval.fingerprint_problems(_record_change(), {}) == []


def test_a_prompt_edit_the_set_cannot_replay_ships_only_recorded_with_its_reason(store, monkeypatch):
    from dsr import narrative
    s = "dsr_narrative"
    monkeypatch.setattr(narrative, "SYSTEM_PROMPT", narrative.SYSTEM_PROMPT + " Mind the weather.")
    rec = _record_change()
    assert any("prompt template" in p for p in ai_eval.fingerprint_problems(rec, {}))
    rec["unmeasured_templates"] = {s: {"template": ai_eval.template_hash(s), "reason": ""}}
    assert any("prompt template" in p for p in ai_eval.fingerprint_problems(rec, {})), "a reason is required"
    rec["unmeasured_templates"][s]["reason"] = "adds a value the frozen requests do not hold; measured after deploy"
    assert ai_eval.fingerprint_problems(rec, {}) == []


# ── the CLI never writes what the gate would refuse ────────────────────────

def _cli(monkeypatch, result, argv):
    from scripts import run_ai_eval
    monkeypatch.setattr(ai_eval, "run", lambda *a, **k: dict(result))
    monkeypatch.setattr(ai_eval, "record_run", lambda *a, **k: "k")
    return run_ai_eval.main(argv)


def _result(surface, model, scores, version="v1"):
    return {"surface": surface, "model": model, "version": version, "template": ai_eval.template_hash(surface),
            "scores": scores, "hit_bound": False}


def test_run_baseline_for_a_regressed_new_model_exits_1_and_writes_nothing(store, monkeypatch):
    s = "review_analysis"
    new = "claude-haiku-6"
    path = ai_eval.baseline_path(s, new)
    rc = _cli(monkeypatch, _result(s, new, _scores(refusal=0.5)),
              ["run", "--surface", s, "--model", new, "--baseline"])
    assert rc == 1 and not os.path.exists(path)
    rc = _cli(monkeypatch, _result(s, new, _scores(refusal=0.05)),
              ["run", "--surface", s, "--model", new, "--baseline"])
    assert rc == 0 and os.path.exists(path)
    body = json.load(open(path))
    assert body["reference"]["model"] == ai_eval.code_model(s) and body["reference"]["set_version"] == "v1"


def test_a_regressed_re_measure_never_overwrites(store, monkeypatch):
    s = "review_analysis"
    model = ai_eval.code_model(s)
    before = open(ai_eval.baseline_path(s, model)).read()
    rc = _cli(monkeypatch, _result(s, model, _scores(refusal=0.2)), ["run", "--surface", s, "--baseline"])
    assert rc == 1 and open(ai_eval.baseline_path(s, model)).read() == before
    # On another set it is no comparison: refused unless the comparison is started over on purpose.
    rc = _cli(monkeypatch, _result(s, model, _scores(), version="v2"), ["run", "--surface", s, "--baseline"])
    assert rc == 1 and open(ai_eval.baseline_path(s, model)).read() == before
    rc = _cli(monkeypatch, _result(s, model, _scores(), version="v2"),
              ["run", "--surface", s, "--baseline", "--new-set"])
    assert rc == 0 and json.load(open(ai_eval.baseline_path(s, model)))["reference"]["new_set"] is True


def test_provider_errors_over_the_threshold_exit_nonzero_even_with_a_reason(store, monkeypatch):
    s = "review_analysis"
    model = ai_eval.code_model(s)
    before = open(ai_eval.baseline_path(s, model)).read()
    res = _result(s, model, _scores(scored=17, errors=3))                  # 3 of 20 failed: 15% > 10%
    assert _cli(monkeypatch, res, ["run", "--surface", s]) == 1
    assert _cli(monkeypatch, res, ["run", "--surface", s, "--baseline", "--accept-regression", "x"]) == 1
    assert open(ai_eval.baseline_path(s, model)).read() == before


def test_a_candidate_prompt_is_replayed_on_the_frozen_requests(monkeypatch):
    """`run --candidate`: the deployed parts swapped for the edited ones in
    each frozen request, the values kept; an edit the set cannot replay is
    refused."""
    from dsr import narrative
    s = "dsr_narrative"
    req = {"model": "claude-sonnet-5-5", "max_tokens": 2000,
           "system": [{"type": "text", "text": narrative.SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
           "messages": [{"role": "user", "content": "Net sales $19,850."}],
           "output_config": {"format": {"type": "json_schema", "schema": narrative.OUTPUT_SCHEMA}}}
    assert ai_eval.rendered_from_deployed(s, req)
    parts = json.loads(json.dumps(ai_eval.template_parts(s)))
    parts["system"]["segments"] = [narrative.SYSTEM_PROMPT + "\nNever lead with the weather."]
    cand = ai_eval.load_candidate(json.dumps({"surface": s, "parts": parts, "template": "forged"}))
    assert cand["template"] != "forged" and cand["template"] == ai_eval.parts_hash(parts)
    assert ai_eval.candidate_problems(s, cand) == []
    out = ai_eval.render_with(s, req, cand)
    assert out["system"][0]["text"].endswith("Never lead with the weather.")
    assert out["messages"] == req["messages"] and out["system"][0]["cache_control"] == {"type": "ephemeral"}
    # The review reply's f-string: its literal parts swap, its values stay.
    r = "review_reply"
    rp = ai_eval.template_parts(r)["prompt"]
    values = [f"<v{i}>" for i in range(len(rp["segments"]) - 1)]
    text = "".join(seg + (values[i] if i < len(values) else "") for i, seg in enumerate(rp["segments"]))
    rreq = {"messages": [{"role": "user", "content": text + "\n\nNOTE: escalated"}]}
    assert ai_eval.rendered_from_deployed(r, rreq)
    cparts = json.loads(json.dumps(ai_eval.template_parts(r)))
    cparts["prompt"]["segments"][-1] = cparts["prompt"]["segments"][-1] + " Keep it under 90 words."
    cand_r = ai_eval.load_candidate(json.dumps({"surface": r, "parts": cparts}))
    out = ai_eval.render_with(r, rreq, cand_r)["messages"][0]["content"]
    assert all(v in out for v in values) and out.endswith("Keep it under 90 words.\n\nNOTE: escalated")
    # A note inside a value moved: the frozen requests hold it rendered, so no replay.
    cparts["prompt"]["inner"] = cparts["prompt"]["inner"] + ["a new note"]
    bad = ai_eval.load_candidate(json.dumps({"surface": r, "parts": cparts}))
    assert any("inside a value" in p for p in ai_eval.candidate_problems(r, bad))
    assert ai_eval.render_with(r, {"messages": [{"role": "user", "content": "not this prompt"}]}, cand_r) is None
