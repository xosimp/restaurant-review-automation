"""Every model call site runs the readiness gate (Data Freshness audit #2,
DH5-2), in the style of tests/test_rv_adoption_source.py.

Read from the source, not from one rendered payload: every call to
ai_utils.create_with_retry passes `readiness=` explicitly — the answer of
data_health.readiness(...) for a call that reads restaurant data (asked in
the same function, so the prompt carries its DATA STATE block and the
validation layer its data_state), or data_health.NOT_APPLICABLE for a call
that rests on no data source, which must be on NOT_APPLICABLE_SITES with
the reason. A new call site that passes neither fails here.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (module, function) → why it rests on no data source. Reply drafts, social
# posts and the calendar, invoice / recipe OCR, sales-audit notes,
# onboarding personalisation and guest SMS (the audit's list), and the two
# competitor menu extractions, which read only the document handed in.
NOT_APPLICABLE_SITES = {
    ("drafter.py", "draft_response"): "a reply to one review, written from that review",
    ("marketing.py", "generate_content"): "a social post drafted from the owner's topic",
    ("marketing.py", "get_content_calendar_ideas"): "calendar ideas from the profile and holidays",
    ("emails.py", "generate_email_personalization"): "onboarding email copy from typed counts",
    ("analyser.py", "analyse_review"): "classifies one review's own text",
    ("recipes.py", "draft_missing"): "recipe drafts the owner confirms line by line",
    ("recipes.py", "extract_from_image"): "OCR of a recipe photo the owner confirms",
    ("invoices.py", "extract"): "invoice OCR the owner confirms line by line",
    ("sales_audit_notes_ai.py", "_call_claude"): "internal notes from the auditor's own input",
    ("guest_marketing.py", "draft_campaign_message"): "guest SMS copy from the owner's offer",
    ("competitor.py", "fetch_menu_from_pdf_bytes"): "menu extraction from the supplied PDF",
    ("competitor.py", "fetch_menu_from_url"): "menu extraction from the supplied page",
}


def _modules():
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel = os.path.relpath(dirpath, ROOT)
        parts = rel.split(os.sep)
        if any(p.startswith(".") or p in ("tests", "scripts", "ios", "node_modules", "venv", ".venv",
                                           "__pycache__", "docs", "public", "templates", "static")
               for p in parts if p != "."):
            dirnames[:] = []
            continue
        for f in filenames:
            if f.endswith(".py"):
                out.append(os.path.normpath(os.path.join(rel, f)) if rel != "." else f)
    return sorted(out)


def _name(func):
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _sites():
    """[(module, enclosing function, Call node, function node)] for every
    create_with_retry call outside ai_utils."""
    sites = []
    for mod in _modules():
        if mod == "ai_utils.py":
            continue
        src = open(os.path.join(ROOT, mod), encoding="utf-8").read()
        if "create_with_retry" not in src:
            continue
        tree = ast.parse(src)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and _name(n.func) == "create_with_retry":
                    # the innermost function holding the call
                    inner = [f for f in ast.walk(fn) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                             and f is not fn and n in list(ast.walk(f))]
                    if inner:
                        continue
                    sites.append((mod, fn.name, n, fn))
    return sites


SITES = _sites()


def test_the_scan_finds_the_known_call_sites():
    found = {(m, f) for m, f, _n, _fn in SITES}
    for expected in [("inventory.py", "get_claude_insights"), ("labor.py", "get_claude_insights"),
                     ("ask_cavnar.py", "ask_with_tools"), ("reporter.py", "generate_ai_digest_summary"),
                     ("dsr/narrative.py", "_write"), ("drafter.py", "draft_response")]:
        assert expected in found, expected
    assert len(SITES) >= 20


@pytest.mark.parametrize("site", SITES, ids=[f"{m}:{f}:{n.lineno}" for m, f, n, _fn in SITES])
def test_every_create_with_retry_call_passes_readiness(site):
    mod, fname, call, fn = site
    kw = {k.arg: k.value for k in call.keywords if k.arg}
    assert "readiness" in kw, (f"{mod}:{fname} (line {call.lineno}) calls create_with_retry without "
                               "readiness= — ask data_health.readiness(...) first, or pass "
                               "data_health.NOT_APPLICABLE and list it in NOT_APPLICABLE_SITES")
    value = kw["readiness"]
    if isinstance(value, ast.Attribute) and value.attr == "NOT_APPLICABLE":
        assert (mod, fname) in NOT_APPLICABLE_SITES, (
            f"{mod}:{fname} passes NOT_APPLICABLE but is not on the list of calls that rest on no data")
        return
    # A data-dependent call: the gate is asked in the same function.
    asked = any(isinstance(n, ast.Call) and str(_name(n.func) or "").endswith("readiness")
                for n in ast.walk(fn))
    assert asked, f"{mod}:{fname} passes readiness= but never asks data_health.readiness(...)"


def test_the_not_applicable_list_names_only_real_call_sites():
    found = {(m, f) for m, f, _n, _fn in SITES}
    stale = [k for k in NOT_APPLICABLE_SITES if k not in found]
    assert not stale, f"listed as resting on no data but no longer a call site: {stale}"
