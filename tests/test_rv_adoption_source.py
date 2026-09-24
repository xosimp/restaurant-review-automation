"""Every model call site runs the Response Validation Layer (workstream A,
NS6 §C Tests — "every create_with_retry or ask_with_tools module calls
validate", following tests/test_tenant_isolation.py's source-shape guard).

Read from the source, not from one rendered payload (feedback: a test on one
fixture only covers that fixture's branches): every function in the
codebase that calls ai_utils.create_with_retry or ask_cavnar.ask_with_tools
either calls the engine itself (response_validation.validate / enforce /
validate_lines / apply), or names the function in its own module that
validates its output, or is on the allowlist below — with the reason it is
a structural validator rather than owner- or guest-facing prose.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_CALLS = {"create_with_retry", "ask_with_tools"}
ENGINE_CALLS = {"validate", "enforce", "validate_lines", "apply"}

# (module, function) → why its output is not owner/guest prose the engine
# reads. Each has its own structural validator.
ALLOWLIST = {
    ("analyser.py", "analyse_review"):
        "review classification JSON: _validate_entities / _severity_floor / _escalate_urgency",
    ("recipes.py", "extract_from_image"):
        "recipe extraction from a photo: line_confidence caps each line; the owner confirms every line",
    ("recipes.py", "draft_missing"):
        "recipe drafts: line_confidence caps each line; the owner confirms every line",
    ("invoices.py", "extract"):
        "invoice extraction: propose() keeps every write in Python; confirmed per line",
    ("sales_audit_notes_ai.py", "_call_claude"):
        "internal sales-audit notes (admin only): _value_in_note drops what the note does not state",
    ("competitor.py", "fetch_menu_from_pdf_bytes"):
        "menu extraction: spot_check_menu keeps only items found in the source text",
    ("competitor.py", "fetch_menu_from_url"):
        "menu extraction: spot_check_menu keeps only items found in the source page",
    ("labor.py", "generate_optimized_schedule"):
        "schedule rows (CSV / JSON schema): schedule_rules.violations repairs and sweeps them; "
        "its prose note is validated in _drop_note_bullets",
}

# (module, function that calls the model) → the function in the same module
# that runs the engine on its output.
VALIDATED_IN = {
    # _write → verify → check_item → Facts.rv_check (validate_lines per item).
    ("dsr/narrative.py", "_write"): "rv_check",
}

# Callers of ask_cavnar.ask_with_tools that only relay its answer: the answer
# is validated inside ask_with_tools (ask_cavnar._finish) before it returns.
RELAYS = {
    ("client_api.py", "_do_ask_cavnar"): "the web / mobile Ask route relays ask_with_tools' validated answer",
    ("client_api.py", "_ask_cavnar_stream_response"): "the streamed Ask route relays the validated answer",
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


def _called_names(node) -> set:
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


def _engine_called(node) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ENGINE_CALLS:
            base = n.func.value
            if isinstance(base, ast.Name) and base.id in ("rv", "response_validation", "_rv") or \
                    isinstance(base, ast.Name) and base.id.endswith("rv"):
                return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in (
                "validate_response", "rv_enforce", "rv_validate", "rv_validate_lines"):
            return True
    return False


def _functions(tree):
    """Top-level functions and methods, outermost first (a nested helper
    belongs to the function it sits in)."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(node)
    return out


def _model_call_sites():
    sites = []
    for mod in _modules():
        if mod in ("ai_utils.py", "response_validation.py"):
            continue
        path = os.path.join(ROOT, mod)
        try:
            src = open(path, encoding="utf-8").read()
        except OSError:
            continue
        if not any(c in src for c in MODEL_CALLS):
            continue
        tree = ast.parse(src)
        funcs = {f.name: f for f in _functions(tree)}
        inner = set()
        for f in funcs.values():
            for n in ast.walk(f):
                if n is not f and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    inner.add(n.name)
        for name, f in funcs.items():
            if name in inner or name == "ask_with_tools" and mod == "ask_cavnar.py" and False:
                continue
            direct = set()
            for n in ast.walk(f):
                if isinstance(n, ast.Call):
                    fn = n.func
                    nm = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
                    if nm in MODEL_CALLS:
                        direct.add(nm)
            if direct:
                sites.append((mod, name, f, funcs))
    return sites


SITES = _model_call_sites()


def test_the_scan_finds_the_known_call_sites():
    found = {(m, n) for m, n, _f, _fs in SITES}
    for expected in [("inventory.py", "get_claude_insights"), ("labor.py", "get_claude_insights"),
                     ("ask_cavnar.py", "ask_with_tools"), ("drafter.py", "draft_response"),
                     ("marketing.py", "generate_content_calendar"), ("emails.py", "_personalise")]:
        assert any(m == expected[0] for m, _n in found), expected


@pytest.mark.parametrize("site", SITES, ids=[f"{m}:{n}" for m, n, _f, _fs in SITES])
def test_every_model_call_site_validates_its_output(site):
    mod, name, func, funcs = site
    if (mod, name) in ALLOWLIST:
        return
    if _engine_called(func):
        return
    if (mod, name) in RELAYS:
        ask = next(s for s in SITES if s[0] == "ask_cavnar.py" and s[1] == "ask_with_tools")
        assert "ask_with_tools" in _called_names(func)
        assert _engine_called(ask[2]) or any(_engine_called(ask[3][c]) for c in _called_names(ask[2])
                                             if c in ask[3]), "ask_with_tools no longer validates"
        return
    via = VALIDATED_IN.get((mod, name))
    if via:
        assert via in funcs and _engine_called(funcs[via]), f"{mod}:{via} does not run the engine"
        return
    # The output goes through a function of the same module that runs the
    # engine (ask_with_tools → _finish).
    if any(c in funcs and c != name and _engine_called(funcs[c]) for c in _called_names(func)):
        return
    # A function whose model output returns to a caller in the same module
    # that validates it (the common shape: _call_model → caller).
    callers = [f for f in funcs.values() if name in _called_names(f) and f.name != name]
    if callers and all(_engine_called(c) for c in callers):
        return
    pytest.fail(f"{mod}:{name} calls a model and never runs response_validation "
                f"(validate / enforce / validate_lines / apply) on the output — adopt it, or add it to "
                f"ALLOWLIST with the structural validator that stands in for it")


def test_the_allowlist_names_only_real_call_sites():
    found = {(m, n) for m, n, _f, _fs in SITES}
    stale = [k for k in ALLOWLIST if k not in found]
    assert not stale, f"allowlisted but no longer a model call site: {stale}"
