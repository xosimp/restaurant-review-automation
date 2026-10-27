"""Audit #25 A07-14 — every model call with a registered workflow policy
goes through the workflow's route, so the tier canary (AI_TIER_CANARY) and
the tier env vars reach it.

Ask's conversation summary called create_with_retry on model_for(
"ask_summary") straight: with AI_TIER_CANARY=T1 a restaurant outside the
canary stayed on Haiku 5.5, around the documented rollback lever.
"""
import ast
import os

import pytest

import ai_utils
import ai_workflows as wf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _summary_model(monkeypatch, restaurant_id):
    import ask_conversations
    seen = {}

    def create(client, **kw):
        seen.update(kw)
        raise RuntimeError("stop here")
    monkeypatch.setattr(ai_utils, "create_with_retry", create)
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    with pytest.raises(RuntimeError):
        ask_conversations._summarize_call(restaurant_id, None, [{"role": "user", "content": "How were sales?",
                                                                  "created_at": "2026-10-26 12:00:00"}])
    return seen


def test_asks_summary_follows_the_t1_canary(monkeypatch):
    for k in ("ASK_SUMMARY_MODEL", "AI_TIER_T1_MODEL", "AI_TIER_T1_PREVIOUS_MODEL", "AI_CANARY_PERCENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AI_TIER_CANARY", "T1")
    monkeypatch.setenv("AI_CANARY_RESTAURANTS", "4")
    assert _summary_model(monkeypatch, 5)["model"] == ai_utils.HAIKU, "outside the canary: T1's previous model"
    assert _summary_model(monkeypatch, 4)["model"] == ai_utils.HAIKU_55
    monkeypatch.setenv("AI_TIER_CANARY", "")
    seen = _summary_model(monkeypatch, 5)
    assert seen["model"] == ai_utils.HAIKU_55 and seen["action"] == "ask_summary"
    assert seen["max_tokens"] and seen["system"] and seen["messages"]


def test_an_ask_summary_model_pin_still_wins(monkeypatch):
    monkeypatch.setenv("AI_TIER_CANARY", "T1")
    monkeypatch.setenv("ASK_SUMMARY_MODEL", "claude-pinned-test")
    assert _summary_model(monkeypatch, 5)["model"] == "claude-pinned-test"


# ── every create_with_retry with a registered policy uses its route ─────────

# Call sites that stay on their own model (model_for), each with why — and
# the test below holds the invariant that makes that safe: with no env var
# set, the call site's model IS the route's.
OWN_MODEL = {
    ("review_intelligence.py", "diagnose"):
        "review_diagnosis: sync and batch share diagnosis_request (CONSOLE_READ_ONLY); ladder T2",
    ("food_cost_intelligence.py", "diagnose"):
        "food_cost_diagnosis: sync and batch share diagnosis_request (CONSOLE_READ_ONLY); ladder T2",
    ("sales_audit_notes_ai.py", "_call_claude"):
        "audit_notes_read: an admin-only read on its call site's own model (ladder 'default')",
}
_ROUTED_BY = {"route_for", "generate", "model_for_run"}


def _module_constants(tree):
    out = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            out[n.targets[0].id] = n.value.value
    return out


def _sites():
    """[(module, outermost function, workflow, routed)] for every
    create_with_retry call whose action names a registered workflow."""
    skip = {"tests", "scripts", "ios", ".venv", "venv", "node_modules", "docs", "public", "templates", "static",
            "workers", "eval_baselines"}
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for f in filenames:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dirpath, f)
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            if rel in ("ai_utils.py", "ai_eval.py"):
                continue
            src = open(path, encoding="utf-8").read()
            if "create_with_retry" not in src:
                continue
            tree = ast.parse(src)
            consts = _module_constants(tree)

            def visit(node, chain):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        visit(child, chain + [child])
                        continue
                    if isinstance(child, ast.Call) and chain:
                        fn = child.func
                        name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
                        if name == "create_with_retry":
                            action = None
                            for k in child.keywords:
                                if k.arg == "action":
                                    if isinstance(k.value, ast.Constant):
                                        action = k.value.value
                                    elif isinstance(k.value, ast.Name):
                                        action = consts.get(k.value.id)
                            if action in wf.POLICIES:
                                routed = any(
                                    any(isinstance(a, ast.arg) and a.arg == "route" for a in fn_.args.args
                                        + fn_.args.kwonlyargs)
                                    or any(isinstance(c, ast.Call) and (getattr(c.func, "attr", None) in _ROUTED_BY
                                                                       or getattr(c.func, "id", None) in _ROUTED_BY)
                                           for c in ast.walk(fn_))
                                    for fn_ in chain)
                                out.append((rel, chain[0].name, action, routed))
                    visit(child, chain)
            visit(tree, [])
    return out


SITES = _sites()


def test_the_scan_finds_the_known_routed_sites():
    found = {(m, f) for m, f, _a, _r in SITES}
    for expected in [("ask_conversations.py", "_summarize_call"), ("analyser.py", "analyse_review"),
                     ("drafter.py", "draft_response"), ("dsr/narrative.py", "run")]:
        assert expected in found, expected


@pytest.mark.parametrize("site", SITES, ids=[f"{m}:{f}" for m, f, _a, _r in SITES])
def test_every_call_with_a_registered_policy_uses_its_route(site):
    mod, func, action, routed = site
    if routed:
        return
    assert (mod, func) in OWN_MODEL, (
        f"{mod}:{func} calls create_with_retry(action={action!r}) — a registered workflow — on its own model: "
        "send it through ai_workflows.route_for(policy, ..., restaurant_id=...) and route.apply(kwargs), or add it "
        "to OWN_MODEL with why")


@pytest.mark.parametrize("site", sorted(OWN_MODEL))
def test_a_call_on_its_own_model_runs_the_model_its_route_names(site, monkeypatch):
    """The invariant behind OWN_MODEL: with no env var set, the call site's
    own model and its workflow route's model are the same — a tier default
    moving makes them differ, and this fails until the site is routed."""
    workflow = {"review_intelligence.py": "review_diagnosis", "food_cost_intelligence.py": "food_cost_diagnosis",
                "sales_audit_notes_ai.py": "audit_notes_read"}[site[0]]
    pol = wf.POLICIES[workflow]
    for k in [env for env, _d in ai_utils.MODELS.values()] + ["AI_TIER_T1_MODEL", "AI_TIER_T2_MODEL",
                                                             "AI_TIER_T3_MODEL", "AI_TIER_T4_MODEL", "AI_TIER_CANARY"]:
        monkeypatch.delenv(k, raising=False)
    assert ai_utils.model_for(pol.purpose) == wf.route_for(pol, 0).model, workflow
    assert site in {(m, f) for m, f, _a, r in SITES if not r}, f"{site} is routed now: drop it from OWN_MODEL"
