"""
ai_eval.py — frozen evaluation sets for the top model surfaces, scored with
the deterministic checks the app already runs, against recorded baselines
(Audit #25 R13: A07-02, A07-06).

Why it exists. Model moves shipped fleet-wide with no offline evaluation: on
10/17/26 every Haiku 4.5 call went to Haiku 5.5 and every Sonnet 5 call to
Sonnet 5.5, and on 10/19 the invoice read went to Opus 5.5 with "no
stored-invoice eval has been run". In the tests a model upgrade showed up
only as changed model-name pins. Here:

  THE SET (ai_eval_items, created at boot with the AI-ops tables). freeze()
  copies up to N stored production inputs of one surface WHERE THE DATABASE
  IS (run it on Railway): the orchestrator's kept first-rung requests
  (ai_run_requests, a sampled share of runs) and, where those are too few,
  the ai_calls trace's kept prompt text (Ask's final calls, always) — every
  request scrubbed again with ai_orchestrator._redacted_request. Only
  learning-eligible restaurants (no demo, test or internal account). An
  invoice photo is never stored (invoices: only its sha256), so an invoice
  item is the owner-confirmed ground truth (invoice_imports.applied_json)
  and its scan is supplied at run time (scripts/run_ai_eval.py --files).
  The set is versioned and no retention rule touches it.

  THE SCORE (score_item). Per answer: a provider refusal, a truncation, a
  schema that does not parse (analyser._validate_analysis, the DSR's JSON,
  invoices.extraction_verdict), response_validation's verdict on the text
  against the prompt it was given (context_text: F1 figure checks and the
  rest), ai_guard.unsupported_figures, and for an invoice the confirmed
  lines it got right. "Refused" is the owner getting no answer: a refusal,
  a truncation, a schema failure, or the validator refusing or withholding
  (for the DSR, every line dropped).

  THE BASELINE (eval_baselines/<surface>__<model>.json). Aggregate scores
  only — never an input, an answer or a name: the repo is public. Each
  records the prompt template it was measured on (template_hash) and the
  REFERENCE it was accepted against: the baseline in force before it
  (its model, template, set and scores), on the SAME frozen set. A change
  of model or prompt is accepted when, against that reference, the refusal
  rate is at most the reference's plus REFUSAL_SLACK (2 points), the
  figure-mismatch rate is not above it and the invoice line agreement is
  not below it (acceptance()) — and every baseline also clears absolute
  floors (baseline_findings: at least BASELINE_MIN_SCORED answers scored,
  provider errors at most BASELINE_MAX_ERROR_RATE of the calls, refusals at
  most BASELINE_MAX_REFUSAL_RATE). A run that misses any of it is never
  written (scripts/run_ai_eval.py exits 1); a quality miss may be written
  only with --accept-regression "<why>", which the file records (Re-audit
  B7 P1, 10/27/26: a model refusing every answer used to write a passing
  baseline, and a model change was never compared with the model before
  it). tests/test_ai_model_change_needs_eval.py re-checks every verdict
  (fingerprint_problems), not only that the file exists, and fails when
  ai_utils.MODELS, a tier's default model or an eval surface's prompt
  template differs from the recorded fingerprint
  (eval_baselines/MODEL_FINGERPRINT.json).

  THE PROMPT (TEMPLATES, template_parts, template_hash). Each eval
  surface's prompt text as the code holds it — a static system prompt, a
  schema, the literal parts of a format string or of the f-string a
  function builds, with the strings and constants those interpolate.
  Editing it moves the surface's template hash, and the gate wants a
  baseline measured on the new text: `scripts/run_ai_eval.py template
  --surface S` exports the edited parts, and `run --candidate FILE` on
  Railway replays the frozen set with them substituted for the deployed
  ones (render_with). An edit the frozen set cannot replay (a new value, a
  changed note inside a value) is refused; it ships only recorded as
  unmeasured with its reason (fingerprint --unmeasured-template).

  THE SMOKE (run_smoke_eval, the weekly registry job ai_smoke_eval). OFF
  BY DEFAULT: nothing runs unless AI_SMOKE_EVAL=1, and nothing can be
  judged until the sets are frozen and the baselines measured. On, it
  replays a few frozen items of each text surface on the production model,
  inside AI_SMOKE_EVAL_BUDGET_USD, records the scores (ai_eval_runs) and
  their deviations from the baseline; ai_quality_watch warns on a
  deviation. A surface whose calls mostly failed at the provider is a
  failed run and a deviation; a production model moved off the code's (an
  env var, a console override) with no baseline of its own is compared
  with the code model's and named as a deviation. Sends nothing.

Every model call goes through ai_utils.create_with_retry (the model gateway,
the ledger — action "ai_eval", no restaurant). Tests inject `send`.
"""
import hashlib
import json
import logging
import os
import time
import uuid
import zlib
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


EVAL_ACTION = "ai_eval"
ROOT = os.path.dirname(os.path.abspath(__file__))
BASELINE_DIR = os.path.join(ROOT, "eval_baselines")
FINGERPRINT_FILE = os.path.join(BASELINE_DIR, "MODEL_FINGERPRINT.json")
PENDING = "pending first run"
MEASURED = "measured"
DEFAULT_N = 25
# Acceptance against a baseline (the audit's thresholds).
REFUSAL_SLACK = 0.02
# The floors every baseline clears whatever it is compared with (re-audit
# B7 P1): enough answers scored to mean anything (an invoice set is smaller —
# its scans come by hand), provider errors under a tenth of the calls (a run
# the provider broke measured nothing), and refusals under BASELINE_MAX_
# REFUSAL_RATE (the owner getting no answer on a third of the set is not a
# baseline, whatever the model before it did).
BASELINE_MIN_SCORED = 10
BASELINE_MIN_SCORED_INVOICE = 5
BASELINE_MAX_ERROR_RATE = 0.10
BASELINE_MAX_REFUSAL_RATE = 0.30
SMOKE_ENV = "AI_SMOKE_EVAL"
SMOKE_BUDGET_ENV = "AI_SMOKE_EVAL_BUDGET_USD"
SMOKE_BUDGET_DEFAULT = 2.0
SMOKE_ITEMS_ENV = "AI_SMOKE_EVAL_ITEMS"
SMOKE_ITEMS_DEFAULT = 8
# A smoke surface whose refusal rate moved this many points either way from
# its baseline is a deviation the platform check warns on, beside the
# acceptance thresholds (a refusal rate that FELL a lot is a changed model
# too: worth a look, never a page).
SMOKE_DEVIATION_POINTS = 0.10

# surface -> what it is. `workflow`: the ai_workflows policy (and ledger
# action) of the production call; `kind`: how its answer is scored; `rv`:
# the response_validation surface its text is checked as; `source`: where
# freeze() reads its inputs.
SURFACES = {
    "dsr_narrative": {"workflow": "dsr_narrative", "kind": "dsr", "rv": "dsr", "source": "kept"},
    "review_reply": {"workflow": "draft_response", "kind": "prose", "rv": "reply_public", "source": "kept"},
    "ask": {"workflow": "ask_cavnar", "kind": "prose", "rv": "ask", "source": "trace"},
    "invoice_extract": {"workflow": "invoice_extract", "kind": "invoice", "rv": None, "source": "invoice"},
    "review_analysis": {"workflow": "review_analysis", "kind": "analysis", "rv": None, "source": "kept"},
}
# Surfaces the weekly smoke can replay from the database alone (an invoice
# needs its scan).
SMOKE_SURFACES = tuple(s for s, spec in SURFACES.items() if spec["kind"] != "invoice")

# Each surface's prompt as the code holds it (module docstring, THE PROMPT):
#   const    a module constant used as it stands — a static system prompt or
#            a JSON schema. `field` holds exactly it, or (`suffix`) opens with
#            it and the rest is per call.
#   format   a str.format template (`src`: module, constant, the function
#            whose .format call fills it): its literal parts; the values per
#            call. `inner`: the strings and module constants the call passes.
#   fstring  the f-string a function assigns to a variable (`src`: module,
#            function, variable): its literal parts. `inner`: every other
#            string of the function (the notes it interpolates — logging and
#            docstrings left out) and the module constants it reads.
# `field`: where the part sits in a frozen request ("system", the first
# "user" turn, the output "schema"); None for invoice_extract, whose request
# is built at run time from the scan (scripts/run_ai_eval.py).
TEMPLATES = {
    "dsr_narrative": (
        {"name": "system", "kind": "const", "src": ("dsr.narrative", "SYSTEM_PROMPT"), "field": "system"},
        {"name": "schema", "kind": "const", "src": ("dsr.narrative", "OUTPUT_SCHEMA"), "field": "schema"},
    ),
    "review_reply": (
        {"name": "prompt", "kind": "fstring", "src": ("drafter", "draft_response", "prompt"), "field": "user",
         "suffix": True},
    ),
    "ask": (
        {"name": "system_static", "kind": "const", "src": ("ask_cavnar", "_SYSTEM_STATIC"), "field": "system",
         "suffix": True},
    ),
    "invoice_extract": (
        {"name": "prompt", "kind": "const", "src": ("invoices", "_PROMPT"), "field": None},
        {"name": "schema", "kind": "const", "src": ("invoices", "_SCHEMA"), "field": None},
    ),
    "review_analysis": (
        {"name": "prompt", "kind": "format", "src": ("analyser", "ANALYSE_PROMPT", "analyse_review"),
         "field": "user", "suffix": True},
    ),
}
# Calls whose arguments are not prompt text (a log line, a capture).
_NOT_PROMPT_CALLS = frozenset(("print", "debug", "info", "warning", "warn", "error", "exception", "critical",
                               "capture", "_degraded"))


def _now_utc():
    return datetime.now(timezone.utc)


def _z(obj) -> bytes:
    return zlib.compress(json.dumps(obj, default=str).encode("utf-8", "replace"))


def _unz(blob):
    try:
        return json.loads(zlib.decompress(blob).decode("utf-8")) if blob else None
    except Exception as e:
        log.warning("ai_eval: an item's blob is unreadable: %s", e)
        return None


# ── the models in force ─────────────────────────────────────────────────────

def _policy(surface):
    import ai_workflows as wf
    return wf.POLICIES[SURFACES[surface]["workflow"]]


def _start_tier(pol):
    """The rung most restaurants start on: a canaried cheap first rung is
    the canary restaurants' only (ai_workflows.canary_start)."""
    import ai_workflows as wf
    ladder = list(pol.ladder)
    if pol.canary and len(ladder) >= 2 and wf.DEFAULT not in ladder:
        return ladder[1]
    return ladder[0]


def code_model(surface) -> str:
    """The model `surface` runs on as the CODE sets it (no env var, no
    console override): what a baseline file is named after."""
    import ai_utils
    import ai_workflows as wf
    pol = _policy(surface)
    tier = _start_tier(pol)
    if tier == wf.DEFAULT:
        return ai_utils.MODELS[pol.purpose][1]
    return wf.default_tier_models()[tier]["model"]


def production_model(surface) -> str:
    """The model `surface` runs on in this process — env pins, the tier
    table and the canary as route_for reads them, for no one restaurant."""
    import ai_workflows as wf
    pol = _policy(surface)
    start = _start_tier(pol)
    return wf.route_for(pol, 0, start=None if start == pol.ladder[0] else start).model


def model_fingerprint() -> dict:
    """{"models": {purpose: default model}, "tiers": {tier: model/effort},
    "previous": {...}, "templates": {surface: template hash}, "hash"}: every
    default a model change moves — the call sites' own (ai_utils.MODELS)
    and the tiers' (ai_workflows.default_tier_models), not the env vars —
    and each eval surface's prompt template (template_hash)."""
    import ai_utils
    import ai_workflows as wf
    body = {"models": {p: d for p, (_env, d) in sorted(ai_utils.MODELS.items())},
            "tiers": wf.default_tier_models(),
            "previous": {"T1": ai_utils.HAIKU},
            "templates": {s: template_hash(s) for s in SURFACES}}
    body["hash"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return body


def read_fingerprint(path=None) -> dict:
    try:
        with open(path or FINGERPRINT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# ── prompt templates (what a prompt edit moves) ─────────────────────────────

def _canon(value) -> str:
    """A constant as stable text for the hash."""
    if isinstance(value, str):
        return value
    if isinstance(value, (set, frozenset)):
        value = sorted(value, key=str)
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(value)


def _module_tree(modname):
    import ast
    import importlib
    mod = importlib.import_module(modname)
    with open(mod.__file__, encoding="utf-8") as f:
        return mod, ast.parse(f.read())


def _function(tree, name):
    import ast
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise LookupError(f"ai_eval.TEMPLATES names {name}(), which is gone — update the surface's template")


def _strings(node, skip_ids=frozenset()):
    """Every str constant under `node`, in source order, leaving out
    docstrings, the arguments of logging / print / capture calls and the
    nodes whose id() is in `skip_ids`."""
    import ast
    out = []

    def visit(n):
        if id(n) in skip_ids:
            return
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
            if name in _NOT_PROMPT_CALLS:
                return
        children = list(ast.iter_child_nodes(n))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and ast.get_docstring(n) is not None:
            children = [c for c in children if c is not n.body[0]]
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        for c in children:
            visit(c)
    visit(node)
    return out


def _constants_read(node, mod) -> dict:
    """{NAME: canonical value} of the module's UPPER_CASE constants `node` reads."""
    import ast
    import re
    names = sorted({n.id for n in ast.walk(node) if isinstance(n, ast.Name)
                    and re.fullmatch(r"_?[A-Z][A-Z0-9_]*", n.id)})
    return {n: _canon(getattr(mod, n)) for n in names if hasattr(mod, n)}


def _format_segments(template) -> list:
    """The literal parts of a str.format template around its fields."""
    import string
    segs, cur = [], ""
    for literal, field, _spec, _conv in string.Formatter().parse(template):
        cur += literal or ""
        if field is not None:
            segs.append(cur)
            cur = ""
    segs.append(cur)
    return segs


def _part(spec) -> dict:
    """One TEMPLATES entry as {kind, field, suffix, segments, inner}."""
    import ast
    import importlib
    kind, src = spec["kind"], spec["src"]
    out = {"kind": kind, "field": spec.get("field"), "suffix": bool(spec.get("suffix")), "segments": [],
           "inner": []}
    if kind == "const":
        value = getattr(importlib.import_module(src[0]), src[1])
        out["segments"] = [value if isinstance(value, str) else _canon(value)]
        out["json"] = not isinstance(value, str)
        return out
    mod, tree = _module_tree(src[0])
    if kind == "format":
        out["segments"] = _format_segments(getattr(mod, src[1]))
        fn = _function(tree, src[2])
        call = next((n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "format" and isinstance(n.func.value, ast.Name)
                     and n.func.value.id == src[1]), None)
        if call is None:
            raise LookupError(f"{src[0]}.{src[2]}() no longer calls {src[1]}.format — update ai_eval.TEMPLATES")
        args = list(call.args) + [k.value for k in call.keywords]
        out["inner"] = [s for a in args for s in _strings(a)] + [f"{k}={v}" for k, v in sorted(
            {k: v for a in args for k, v in _constants_read(a, mod).items()}.items())]
        return out
    if kind == "fstring":
        fn = _function(tree, src[1])
        js = next((n.value for n in ast.walk(fn) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == src[2] for t in n.targets)
                   and isinstance(n.value, ast.JoinedStr)), None)
        if js is None:
            raise LookupError(f"{src[0]}.{src[1]}() no longer builds `{src[2]}` as an f-string — update "
                              "ai_eval.TEMPLATES")
        segs, cur, literal_ids = [], [], set()
        for v in js.values:
            if isinstance(v, ast.Constant):
                cur.append(str(v.value))
                literal_ids.add(id(v))
            else:
                segs.append("".join(cur))
                cur = []
        segs.append("".join(cur))
        out["segments"] = segs
        out["inner"] = _strings(fn, skip_ids=frozenset(literal_ids)) + [
            f"{k}={v}" for k, v in sorted(_constants_read(fn, mod).items())]
        return out
    raise ValueError(f"unknown template kind {kind!r}")


def template_parts(surface) -> dict:
    """{part name: {kind, field, suffix, segments, inner}} — `surface`'s
    prompt as the code in this process holds it."""
    return {spec["name"]: _part(spec) for spec in TEMPLATES[surface]}


def parts_hash(parts) -> str:
    """The template hash of a template_parts() dict (a candidate's too)."""
    body = {name: {"segments": list(p.get("segments") or []), "inner": list(p.get("inner") or [])}
            for name, p in sorted((parts or {}).items())}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8", "replace")).hexdigest()[:16]


def template_hash(surface) -> str:
    """The hash of `surface`'s prompt template in this process's code."""
    return parts_hash(template_parts(surface))


def _field_text(request, field):
    """The text (or, for "schema", the dict) at `field` of a request; None
    when it has none."""
    req = request or {}
    if field == "system":
        s = req.get("system")
        if isinstance(s, str):
            return s
        if isinstance(s, (list, tuple)) and s and isinstance(s[0], dict):
            return s[0].get("text")
        return None
    if field == "user":
        msgs = req.get("messages") or []
        if not msgs or not isinstance(msgs[0], dict):
            return None
        c = msgs[0].get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, (list, tuple)):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    return b.get("text")
        return None
    if field == "schema":
        return (((req.get("output_config") or {}).get("format") or {}).get("schema"))
    return None


def _set_field(request, field, value):
    """`request` with `field` replaced by `value` (a deep copy)."""
    req = json.loads(json.dumps(request, default=str))
    if field == "system":
        if isinstance(req.get("system"), (list, tuple)) and req["system"] and isinstance(req["system"][0], dict):
            req["system"][0]["text"] = value
        else:
            req["system"] = value
    elif field == "user":
        c = req["messages"][0].get("content")
        if isinstance(c, (list, tuple)):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    b["text"] = value
                    break
        else:
            req["messages"][0]["content"] = value
    elif field == "schema":
        req.setdefault("output_config", {}).setdefault("format", {"type": "json_schema"})["schema"] = value
    return req


def _split(part, text):
    """The per-call values of `text` rendered from `part` (its literal
    segments in order, then the suffix), or None when it was not."""
    import re
    if text is None:
        return None
    if part.get("json"):
        try:
            return [] if _canon(text if not isinstance(text, str) else json.loads(text)) == part["segments"][0] \
                else None
        except (TypeError, ValueError):
            return None
    if not isinstance(text, str):
        return None
    segs = list(part["segments"])
    pattern = "".join(re.escape(s) + ("(.*?)" if i < len(segs) - 1 else "") for i, s in enumerate(segs))
    pattern += "(.*)" if part.get("suffix") else ""
    m = re.fullmatch(pattern, text, flags=re.DOTALL)
    return list(m.groups()) if m else None


def _compatible(deployed, cand) -> str:
    """Why `cand` cannot be replayed on requests rendered from `deployed`
    (the same part), or ""."""
    if cand.get("kind") != deployed.get("kind") or bool(cand.get("json")) != bool(deployed.get("json")):
        return "the part changed kind"
    if list(cand.get("inner") or []) != list(deployed.get("inner") or []):
        return ("it changes text inside a value (a note or constant the prompt interpolates), which the frozen "
                "requests hold already rendered")
    if len(cand.get("segments") or []) != len(deployed.get("segments") or []):
        return "it adds or removes a value"
    for i, seg in enumerate(deployed["segments"][1:-1], start=1):
        if seg == "" and cand["segments"][i] != "":
            return "it puts text between two values the frozen requests hold side by side"
    return ""


def candidate_problems(surface, candidate) -> list:
    """Why `candidate` (template_parts of the edited code) cannot be
    replayed on this surface's frozen set with the deployed template."""
    deployed = template_parts(surface)
    parts = (candidate or {}).get("parts") or {}
    out = []
    if set(parts) != set(deployed):
        return [f"the candidate's parts {sorted(parts)} are not the surface's {sorted(deployed)}"]
    for name, dp in deployed.items():
        why = _compatible(dp, parts[name])
        if why:
            out.append(f"{name}: {why}")
    return out


def render_with(surface, request, candidate):
    """The frozen `request` with the deployed template's parts replaced by
    `candidate`'s (candidate_problems() empty), or None when the request was
    not rendered from the deployed template."""
    deployed = template_parts(surface)
    out = request
    for name, dp in deployed.items():
        if dp["field"] is None:
            continue
        values = _split(dp, _field_text(out, dp["field"]))
        if values is None:
            return None
        cp = candidate["parts"][name]
        if dp.get("json"):
            out = _set_field(out, dp["field"], json.loads(cp["segments"][0]))
            continue
        segs = list(cp["segments"])
        text = "".join(seg + (values[i] if i < len(segs) - 1 else "") for i, seg in enumerate(segs))
        if dp.get("suffix"):
            text += values[-1]
        out = _set_field(out, dp["field"], text)
    return out


def rendered_from_deployed(surface, request) -> bool:
    """Whether a frozen request was rendered from the deployed template."""
    for dp in template_parts(surface).values():
        if dp["field"] is not None and _split(dp, _field_text(request, dp["field"])) is None:
            return False
    return True


def load_candidate(text) -> dict:
    """A candidate exported by `scripts/run_ai_eval.py template` (JSON
    text): {surface, parts, template} with the hash recomputed here —
    never taken on trust."""
    body = json.loads(text)
    if not isinstance(body, dict) or not isinstance(body.get("parts"), dict):
        raise ValueError("not a template export (scripts/run_ai_eval.py template --surface S)")
    body["template"] = parts_hash(body["parts"])
    return body


# ── the gate ────────────────────────────────────────────────────────────────

def _min_scored(surface) -> int:
    return BASELINE_MIN_SCORED_INVOICE if SURFACES[surface]["kind"] == "invoice" else BASELINE_MIN_SCORED


def quality_findings(surface, scores, hit_bound=False) -> tuple:
    """(invalid, regressions) of a run's or a baseline's scores on their
    own: `invalid` can never be a baseline (too few scored, the provider
    failing, the run cut short by its budget); `regressions` (refusals over
    the absolute floor) only with an accepted reason."""
    s = scores or {}
    scored, errors = int(s.get("scored") or 0), int(s.get("errors") or 0)
    invalid, regress = [], []
    if scored < _min_scored(surface):
        invalid.append(f"only {scored} answers scored (at least {_min_scored(surface)})")
    calls = scored + errors
    if calls and errors / float(calls) > BASELINE_MAX_ERROR_RATE + 1e-9:
        invalid.append(f"{errors} of {calls} calls failed at the provider (at most "
                       f"{BASELINE_MAX_ERROR_RATE * 100:g}%) — the run measured the outage, not the model")
    if hit_bound:
        invalid.append("the run stopped at its budget before the set was done (raise --max-usd)")
    r = s.get("refusal_rate")
    if r is not None and r > BASELINE_MAX_REFUSAL_RATE + 1e-9:
        regress.append(f"refusal rate {r * 100:.1f}% (at most {BASELINE_MAX_REFUSAL_RATE * 100:g}% for any baseline)")
    return invalid, regress


def reference_findings(scores, set_version, reference) -> tuple:
    """(invalid, regressions) of a run against the reference it is compared
    with: a different set or a different number of items is no comparison;
    acceptance() misses are regressions."""
    ref = reference or {}
    if ref.get("new_set") or ref.get("scores") is None:
        return [], []
    invalid = []
    if ref.get("set_version") != set_version:
        invalid.append(f"compared with a baseline on set {ref.get('set_version')}, measured on set {set_version}")
    n, rn = (scores or {}).get("n"), (ref.get("scores") or {}).get("n")
    if n is not None and rn is not None and int(n) != int(rn):
        invalid.append(f"measured on {n} items against the reference's {rn} (no --limit on a baseline run)")
    return invalid, acceptance(scores or {}, ref.get("scores"))


def in_force(surface, record=None) -> dict:
    """{model, template, status, set_version} the recorded fingerprint holds
    for `surface` — what production runs (Railway's checkout carries the
    fingerprint its code was gated with)."""
    rec = record if record is not None else read_fingerprint()
    return dict(((rec.get("baselines") or {}).get(surface)) or {})


def choose_reference(surface, model, template, set_version, record=None, new_set=False) -> tuple:
    """(reference or None, invalid, regressions) for a run of `surface` on
    `model` with prompt `template` over frozen set `set_version`:
      * re-measuring what is in force: its own measured baseline on the same
        set ("a regressed re-measure never overwrites"); on another set only
        with new_set (the comparison starts over there);
      * a change of model or prompt: the baseline in force, which must be
        measured on the same set — the model before it is measured first;
      * nothing measured yet for the surface and nothing changing: None, a
        first measurement (the absolute floors still apply)."""
    cur = in_force(surface, record)
    in_model, in_tpl = cur.get("model") or code_model(surface), cur.get("template")
    if model == in_model and (in_tpl is None or template == in_tpl):
        b = read_baseline(surface, model) or {}
        if b.get("status") != MEASURED or (b.get("template") and b.get("template") != template):
            return None, [], []
        if b.get("set_version") == set_version:
            return ({"model": model, "template": b.get("template"), "set_version": set_version,
                     "n": (b.get("scores") or {}).get("n"), "scores": _score_subset(b.get("scores"))}, [], [])
        if new_set:
            return {"new_set": True, "model": model, "template": template, "from_set": b.get("set_version")}, [], []
        return None, [f"the {model} baseline was measured on set {b.get('set_version')}: run with --version "
                      f"{b.get('set_version')}, or --new-set to start the comparison over on set {set_version}"], []
    b = read_baseline(surface, in_model) or {}
    if b.get("status") != MEASURED or (in_tpl and b.get("template") != in_tpl):
        return None, [], [f"nothing in force to compare with: measure {in_model} with the prompt in force on this set "
                          f"first (run --surface {surface} --model {in_model} --version {set_version} --baseline)"]
    if b.get("set_version") != set_version:
        return None, [f"the {in_model} baseline in force was measured on set {b.get('set_version')}: run on --version "
                      f"{b.get('set_version')}, or re-measure {in_model} on set {set_version} first (--new-set)"], []
    return ({"model": in_model, "template": b.get("template"), "set_version": set_version,
             "n": (b.get("scores") or {}).get("n"), "scores": _score_subset(b.get("scores"))}, [], [])


def run_verdict(surface, result, record=None, new_set=False) -> dict:
    """{reference, invalid, regressions} of a run(): what decides whether it
    may be written as a baseline (scripts/run_ai_eval.py run --baseline)."""
    scores = result.get("scores") or {}
    invalid, regress = quality_findings(surface, scores, hit_bound=bool(result.get("hit_bound")))
    if not result.get("template"):
        invalid.append("the frozen items were not all rendered from one prompt template (re-freeze the set)")
    ref, inv2, reg2 = choose_reference(surface, result.get("model"), result.get("template"), result.get("version"),
                                       record=record, new_set=new_set)
    inv3, reg3 = reference_findings(scores, result.get("version"), ref)
    return {"reference": ref, "invalid": invalid + inv2 + inv3, "regressions": regress + reg2 + reg3}


def baseline_problems(surface, b, model, template, record) -> list:
    """Why measured baseline `b` (the file for `model`) does not clear the
    gate for the code as it stands (`template`): the verdict re-checked from
    what the file records, not its presence."""
    out = []
    unmeasured = (record.get("unmeasured_templates") or {}).get(surface) or {}
    if b.get("template") != template:
        if not (unmeasured.get("template") == template and str(unmeasured.get("reason") or "").strip()):
            out.append(f"its baseline was measured on prompt template {b.get('template') or '(unrecorded)'} and the "
                       f"code's is {template}: export it (scripts/run_ai_eval.py template --surface {surface}), "
                       f"measure it on Railway (run --surface {surface} --candidate FILE --baseline), or record why "
                       "it cannot be (fingerprint --write --unmeasured-template ... --reason ...)")
    invalid, regress = quality_findings(surface, b.get("scores"))
    ref = b.get("reference") or {}
    inv2, reg2 = reference_findings(b.get("scores"), b.get("set_version"), ref)
    invalid += inv2
    regress += reg2
    cur = in_force(surface, record)
    expected = None
    if b.get("template") == template:                   # the baseline claims the code as it stands
        if cur and (cur.get("model") != model or (cur.get("template") and cur.get("template") != template)):
            expected = cur                              # a change the fingerprint has not recorded yet
        elif cur.get("previous"):
            expected = cur["previous"]                  # the change it recorded
    same_thing = ref.get("model") == model and ref.get("template") == b.get("template")
    if expected and not same_thing and not (ref.get("model") == expected.get("model")
                                            and ref.get("template") == expected.get("template")
                                            and ref.get("scores") is not None):
        regress.append(f"it was not compared with the baseline in force before the change ({expected.get('model')}, "
                       f"template {expected.get('template')}) on the same set")
    out += invalid
    if regress and not str(b.get("accepted_regression") or "").strip():
        out += regress
    return out


def fingerprint_problems(record, bootstrap) -> list:
    """Why the models and prompts in the code may not ship as they are,
    against the recorded fingerprint `record`
    (tests/test_ai_model_change_needs_eval.py):
      * the code's model defaults or an eval surface's prompt template
        differ from the recorded ones — measure, then record the fingerprint;
      * a surface's baseline on the model and prompt it now runs on is not
        measured — allowed only while both are still the ones the tooling
        shipped with (`bootstrap`: {surface: {model, template}}), whose
        baselines were "pending first run";
      * a measured baseline whose verdict does not hold (baseline_problems):
        the floors, the comparison with what was in force before the change
        on the same set, the prompt it was measured on;
      * a tier whose default model changed (record["tiers_changed"]) is not
        held to the canary restaurants (ai_workflows.TIER_CANARY_DEFAULT)
        and its window was not closed (scripts/run_ai_eval.py fingerprint
        --close-canary, at least a week later)."""
    import ai_workflows as wf
    if isinstance(bootstrap, str):                  # the old single-hash form: nothing is bootstrapped
        bootstrap = {}
    out = []
    cur = model_fingerprint()
    if record.get("hash") != cur["hash"]:
        moved = sorted(p for p, m in cur["models"].items() if (record.get("models") or {}).get(p) != m)
        moved += sorted(f"tier {t}" for t, v in cur["tiers"].items() if (record.get("tiers") or {}).get(t) != v)
        if cur["previous"] != record.get("previous"):
            moved.append("the T1 canary's previous model")
        moved += sorted(f"the {s} prompt" for s, h in cur["templates"].items()
                        if (record.get("templates") or {}).get(s) != h)
        out.append("the AI models or eval prompts in the code differ from eval_baselines/MODEL_FINGERPRINT.json ("
                   + (", ".join(moved) or "fingerprint") + "): measure each moved surface on Railway (scripts/"
                   "run_ai_eval.py run --surface <s> --baseline; a prompt edit with --candidate), commit the baseline "
                   "files, then `scripts/run_ai_eval.py fingerprint --write`")
    for surface in SURFACES:
        model, template = code_model(surface), cur["templates"][surface]
        b = read_baseline(surface, model) or {}
        if b.get("status") != MEASURED:
            boot = (bootstrap or {}).get(surface) or {}
            if b.get("status") == PENDING and boot.get("model") == model and boot.get("template") == template:
                continue
            name = os.path.basename(baseline_path(surface, model))
            out.append(f"{surface} has no measured baseline on {model} with its prompt as it stands (eval_baselines/"
                       f"{name} is {b.get('status') or 'missing'}) — a model or prompt change ships with its eval")
            continue
        out += [f"{surface}: {p}" for p in baseline_problems(surface, b, model, template, record)]
    canaried = {t.strip() for t in str(wf.TIER_CANARY_DEFAULT or "").split(",") if t.strip()}
    for tier in record.get("tiers_changed") or []:
        if not record.get("canary_closed") and tier not in canaried:
            out.append(f"tier {tier}'s model changed: name it in ai_workflows.TIER_CANARY_DEFAULT for at least a week "
                       "(the canary restaurants first), then close the window with "
                       "`scripts/run_ai_eval.py fingerprint --close-canary`")
    return out


# ── baselines (scores only) ────────────────────────────────────────────────

def _safe(model):
    return "".join(c if c.isalnum() or c in "-." else "_" for c in str(model or "unknown"))


def baseline_path(surface, model) -> str:
    return os.path.join(BASELINE_DIR, f"{surface}__{_safe(model)}.json")


def read_baseline(surface, model):
    """The recorded baseline for `surface` on `model`, or None."""
    try:
        with open(baseline_path(surface, model), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# The only keys a baseline file may hold: aggregate numbers and labels.
# `template`: the prompt template hash it was measured on; `reference`: what
# it was accepted against (_REFERENCE_KEYS — the scores, never an input);
# `accepted_regression`: the reason a quality miss was written anyway.
_BASELINE_KEYS = ("surface", "model", "status", "set_version", "n", "scores", "measured_on", "build", "note",
                  "template", "reference", "accepted_regression")
_SCORE_KEYS = ("n", "scored", "refusal_rate", "figure_mismatch_rate", "figures_per_item", "schema_fail_rate",
               "truncated_rate", "rv_caught_rate", "line_agreement", "errors", "cost_usd")
_REFERENCE_KEYS = ("model", "template", "set_version", "n", "scores", "new_set", "from_set")


def _score_subset(scores) -> dict:
    return {k: (scores or {}).get(k) for k in _SCORE_KEYS if k in (scores or {})}


def baseline_body(result, reference=None, accepted_regression=None) -> dict:
    """What write_baseline stores for a run(): its aggregate scores and
    labels, the prompt template it ran on, the reference it was compared
    with and any accepted regression's reason — nothing else: no input,
    answer, name or restaurant."""
    scores = _score_subset(result.get("scores"))
    body = {"surface": result["surface"], "model": result["model"], "status": MEASURED,
            "set_version": result.get("version"), "n": scores.get("scored"),
            "scores": scores, "measured_on": _now_utc().strftime("%Y-%m-%d"),
            "build": (os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT") or "")[:12] or None,
            "template": result.get("template")}
    if reference:
        ref = {k: reference[k] for k in _REFERENCE_KEYS if k in reference}
        if "scores" in ref:
            ref["scores"] = _score_subset(ref["scores"])
        body["reference"] = ref
    if accepted_regression and str(accepted_regression).strip():
        body["accepted_regression"] = str(accepted_regression).strip()[:300]
    return body


def write_baseline(result, path=None, reference=None, accepted_regression=None) -> str:
    body = baseline_body(result, reference=reference, accepted_regression=accepted_regression)
    assert set(body) <= set(_BASELINE_KEYS)
    out = path or baseline_path(result["surface"], result["model"])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(body, f, indent=2, sort_keys=True)
        f.write("\n")
    return out


def acceptance(scores, baseline) -> list:
    """The acceptance thresholds a run's `scores` misses against
    `baseline` (a baseline file's dict, or its scores): the refusal rate
    above the baseline's + REFUSAL_SLACK, more figure mismatches, a lower
    invoice line agreement. [] when it passes or there is nothing to compare."""
    base = (baseline or {}).get("scores", baseline) or {}
    out = []
    r, br = scores.get("refusal_rate"), base.get("refusal_rate")
    if r is not None and br is not None and r > br + REFUSAL_SLACK + 1e-9:
        out.append(f"refusal rate {r * 100:.1f}% against the baseline's {br * 100:.1f}% (+{REFUSAL_SLACK * 100:g} "
                   "points allowed)")
    f_, bf = scores.get("figure_mismatch_rate"), base.get("figure_mismatch_rate")
    if f_ is not None and bf is not None and f_ > bf + 1e-9:
        out.append(f"figure mismatches in {f_ * 100:.1f}% of answers against the baseline's {bf * 100:.1f}%")
    la, bla = scores.get("line_agreement"), base.get("line_agreement")
    if la is not None and bla is not None and la < bla - 1e-9:
        out.append(f"invoice line agreement {la * 100:.1f}% against the baseline's {bla * 100:.1f}%")
    return out


# ── the set ─────────────────────────────────────────────────────────────────

def _eligible(restaurant_id, db_path=None) -> bool:
    if restaurant_id is None:
        return False
    try:
        import models
        return bool(models.learning_eligible(int(restaurant_id), db_path=db_path))
    except Exception as e:
        log.warning("ai_eval: eligibility of restaurant %s unreadable: %s", restaurant_id, e)
        return False


def _request_from_trace(text, surface):
    """A request rebuilt from an ai_calls trace's prompt text
    (ai_utils._prompt_text: "[system]", then each "[role]" turn): the
    system block and the turns, the last a user turn."""
    import re
    parts = re.split(r"(?m)^\[(system|user|assistant)\]\n", str(text or ""))
    system, messages = "", []
    for i in range(1, len(parts) - 1, 2):
        role, body = parts[i], parts[i + 1].strip()
        body = re.sub(r"\n\n\[tools: \d+\][^\n]*$", "", body).strip()
        if not body:
            continue
        if role == "system":
            system = body
        elif messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n\n" + body
        else:
            messages.append({"role": role, "content": body})
    while messages and messages[-1]["role"] != "user":
        messages.pop()
    if not messages:
        return None
    req = {"messages": messages, "max_tokens": 2000}
    if system:
        req["system"] = system
    if SURFACES[surface]["kind"] == "dsr":
        from dsr import narrative
        req["output_config"] = {"format": {"type": "json_schema", "schema": narrative.OUTPUT_SCHEMA}}
        req["max_tokens"] = narrative.MAX_TOKENS
    return req


def _kept_candidates(workflow, limit, db_path=None):
    import ai_orchestrator
    out = []
    for run_id, rid, request in ai_orchestrator.kept_requests(workflow, limit=limit, db_path=db_path):
        out.append({"key": f"run:{run_id}", "restaurant_id": rid, "request": request,
                    "model": (request or {}).get("model")})
    return out


def _trace_candidates(surface, action, limit, db_path=None):
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT call_id, restaurant_id, model, prompt_z FROM ai_calls WHERE action=? "
                            "AND prompt_z IS NOT NULL AND outcome='ok' AND COALESCE(stop_reason, 'end_turn')="
                            "'end_turn' ORDER BY created_at DESC LIMIT ?", (action, int(limit))).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            text = zlib.decompress(r["prompt_z"]).decode("utf-8", "replace")
        except Exception as e:
            log.warning("ai_eval: trace %s unreadable: %s", r["call_id"], e)
            continue
        req = _request_from_trace(text, surface)
        if req:
            out.append({"key": f"call:{r['call_id']}", "restaurant_id": r["restaurant_id"], "request": req,
                        "model": r["model"]})
    return out


def _invoice_truth(row):
    """The ground truth an invoice item carries: the owner's confirmed costs
    (applied_json, by the owner — scripts/invoice_model_eval.truth_for's
    rule), the printed total the production read found, its line count."""
    try:
        body = json.loads(row["lines_json"] or "{}")
    except ValueError:
        body = {}
    try:
        applied = json.loads(row["applied_json"]) if row["applied_json"] else []
    except ValueError:
        applied = []
    confirmed = {}
    for a in applied if isinstance(applied, list) else []:
        if isinstance(a, dict) and a.get("ingredient_id") and a.get("new_cost") is not None \
                and a.get("by", "owner") == "owner":
            confirmed[str(int(a["ingredient_id"]))] = float(a["new_cost"])
    return {"restaurant_id": row["restaurant_id"], "import_id": row["id"], "image_sha": row["image_sha"],
            "confirmed": confirmed, "invoice_total": (body.get("total_check") or {}).get("invoice_total"),
            "lines": len(body.get("lines") or [])}


def _invoice_candidates(limit, db_path=None):
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT id, restaurant_id, image_sha, lines_json, applied_json FROM invoice_imports "
                            "WHERE applied_json IS NOT NULL AND image_sha IS NOT NULL ORDER BY id DESC LIMIT ?",
                            (int(limit),)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        truth = _invoice_truth(r)
        if truth["confirmed"]:
            out.append({"key": f"invoice:{r['id']}", "restaurant_id": r["restaurant_id"], "request": None,
                        "model": None, "reference": truth})
    return out


def freeze(surface, n=DEFAULT_N, version=None, db_path=None) -> dict:
    """Copy up to `n` stored production inputs of `surface` into
    ai_eval_items under `version` (default v<UTC date>). Returns {surface,
    version, frozen, already, skipped: {reason: count}}. Reads the database
    it runs against — production's on Railway — and writes nothing else."""
    import ai_orchestrator
    spec = SURFACES[surface]
    version = version or "v" + _now_utc().strftime("%Y%m%d")
    pool = max(int(n) * 4, 20)
    if spec["source"] == "invoice":
        cands = _invoice_candidates(pool, db_path)
    elif spec["source"] == "trace":
        cands = _trace_candidates(surface, spec["workflow"], pool, db_path)
    else:
        cands = _kept_candidates(spec["workflow"], pool, db_path)
        if len(cands) < pool:
            cands += _trace_candidates(surface, spec["workflow"], pool - len(cands), db_path)
    out = {"surface": surface, "version": version, "frozen": 0, "already": 0,
           "skipped": {"not_eligible": 0, "unreadable": 0, "other_template": 0}}
    # The prompt template in force here (production's, on Railway): an input
    # rendered from an older prompt measures that prompt, not this one, so it
    # is left out and every item records the template it was rendered from.
    deployed = template_hash(surface)
    conn = get_conn(db_path)
    try:
        for c in cands:
            if out["frozen"] + out["already"] >= int(n):
                break
            if not _eligible(c.get("restaurant_id"), db_path):
                out["skipped"]["not_eligible"] += 1
                continue
            request = c.get("request")
            reference = dict(c.get("reference") or {})
            if request is not None:
                if not rendered_from_deployed(surface, request):
                    out["skipped"]["other_template"] += 1
                    continue
                reference["template"] = deployed
                try:
                    request = ai_orchestrator._redacted_request(dict(request))
                except Exception as e:
                    log.warning("ai_eval: %s not scrubbed, not frozen: %s", c["key"], e)
                    out["skipped"]["unreadable"] += 1
                    continue
            cur = conn.execute(
                "INSERT INTO ai_eval_items (surface, version, item_key, restaurant_id, source_model, request_z, "
                "reference_json) VALUES (?,?,?,?,?,?,?) ON CONFLICT (surface, version, item_key) DO NOTHING",
                (surface, version, c["key"], c.get("restaurant_id"), c.get("model"),
                 _z(request) if request is not None else None,
                 json.dumps(reference) if reference else None))
            if cur.rowcount == 1:
                out["frozen"] += 1
            else:
                out["already"] += 1
        conn.commit()
    finally:
        conn.close()
    return out


def latest_version(surface, db_path=None):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT version FROM ai_eval_items WHERE surface=? ORDER BY id DESC LIMIT 1",
                           (surface,)).fetchone()
        return row["version"] if row else None
    finally:
        conn.close()


def load_items(surface, version=None, limit=None, db_path=None) -> list:
    """[{item_key, restaurant_id, request, reference}] of `surface`'s set
    (its newest version by default), in a stable order."""
    version = version or latest_version(surface, db_path)
    if not version:
        return []
    conn = get_conn(db_path)
    try:
        sql = ("SELECT item_key, restaurant_id, source_model, request_z, reference_json FROM ai_eval_items "
               "WHERE surface=? AND version=? ORDER BY id")
        args = [surface, version]
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            ref = json.loads(r["reference_json"]) if r["reference_json"] else None
        except ValueError:
            ref = None
        out.append({"item_key": r["item_key"], "restaurant_id": r["restaurant_id"], "version": version,
                    "source_model": r["source_model"], "request": _unz(r["request_z"]), "reference": ref})
    return out


# ── scoring ─────────────────────────────────────────────────────────────────

def eval_request(request, model) -> dict:
    """The frozen request sent to `model`: its system, turns, max_tokens and
    output format kept; the model replaced. On the model the request was
    made for, the production route's thinking and effort are kept — the
    eval measures the call production makes (re-audit B7); on another model
    they are left out (create_with_retry applies the model's own
    defaults)."""
    req = request or {}
    same = bool(req.get("model")) and req.get("model") == model
    kw = {k: req[k] for k in ("system", "messages", "max_tokens", "output_config") if k in req}
    if same and req.get("thinking"):
        kw["thinking"] = req["thinking"]
    kw["model"] = model
    oc = dict(kw.get("output_config") or {})
    if not same:
        oc.pop("effort", None)
    if oc:
        kw["output_config"] = oc
    else:
        kw.pop("output_config", None)
    kw["max_tokens"] = int(kw.get("max_tokens") or 2000)
    return kw


def _text_of(msg):
    import ai_utils
    try:
        return ai_utils.extract_text(msg) or ""
    except Exception as e:
        log.warning("ai_eval: answer text unreadable: %s", e)
        return ""


def score_item(surface, request, msg) -> dict:
    """One answer, scored (module docstring): {refused, truncated,
    schema_fail, figures, rv, rv_dropped}. `request` is what was sent (its
    prompt text is the context the figures are checked against)."""
    import ai_guard
    import ai_utils
    import response_validation as rv
    spec = SURFACES[surface]
    out = {"refused": False, "model_refused": False, "truncated": False, "schema_fail": False, "figures": 0,
           "rv": None, "rv_dropped": 0}
    stop = getattr(msg, "stop_reason", None)
    if stop == "refusal":
        out.update(refused=True, model_refused=True)
        return out
    if stop == "max_tokens":
        out.update(refused=True, truncated=True)
        return out
    text = _text_of(msg)
    context = ai_utils._prompt_text(request or {})
    kind = spec["kind"]
    if kind == "analysis":
        try:
            import analyser
            analyser._validate_analysis(ai_utils.parse_json_reply(text, expect=dict, message=msg))
        except Exception:
            out.update(schema_fail=True, refused=True)
        return out
    if kind == "dsr":
        try:
            from dsr import narrative
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError("not an object")
            lines = [ln for ln in narrative._narrative_text(parsed).splitlines() if ln.strip()]
        except Exception:
            out.update(schema_fail=True, refused=True)
            return out
        res = rv.validate_lines(lines, rv.ValidationContext(surface=spec["rv"], context_text=context))
        out["rv_dropped"] = len(res.dropped)
        out["rv"] = "refuse" if lines and not res.lines else ("caveat" if res.dropped else "pass")
        out["refused"] = not res.lines
        out["figures"] = len(ai_guard.unsupported_figures("\n".join(lines), context))
        return out
    # Prose (a review reply, an Ask answer).
    if not text.strip():
        out["refused"] = True
        return out
    v = rv.validate(text, rv.ValidationContext(surface=spec["rv"], context_text=context))
    out["rv"] = v.verdict
    out["refused"] = v.verdict in ("refuse", "withhold")
    out["figures"] = len(ai_guard.unsupported_figures(text, context))
    return out


def score_invoice(item, extracted, verdict_ok, lines_right=0, truncated=False) -> dict:
    """An invoice read against its item's ground truth: {refused,
    schema_fail, confirmed, lines_right}. The line agreement is lines_right
    over confirmed: the confirmed ingredients the read's proposal priced
    within a cent or 1% — scripts/invoice_model_eval.score's matching after
    invoices.propose, run by scripts/run_ai_eval.py with the scan. `verdict_ok`
    is invoices.extraction_verdict passing."""
    truth = (item or {}).get("reference") or {}
    return {"refused": extracted is None or not verdict_ok or bool(truncated), "schema_fail": extracted is None,
            "truncated": bool(truncated), "model_refused": False, "figures": 0, "rv": None, "rv_dropped": 0,
            "confirmed": len(truth.get("confirmed") or {}), "lines_right": int(lines_right or 0)}


def aggregate(scored, errors=0, cost_usd=0.0) -> dict:
    """The run's aggregate scores from its items' scores."""
    n = len(scored)
    out = {"n": n + int(errors), "scored": n, "errors": int(errors), "cost_usd": round(float(cost_usd or 0), 4)}
    if not n:
        return out
    out["refusal_rate"] = round(sum(1 for s in scored if s.get("refused")) / n, 4)
    out["figure_mismatch_rate"] = round(sum(1 for s in scored if (s.get("figures") or 0) > 0) / n, 4)
    out["figures_per_item"] = round(sum(int(s.get("figures") or 0) for s in scored) / n, 3)
    out["schema_fail_rate"] = round(sum(1 for s in scored if s.get("schema_fail")) / n, 4)
    out["truncated_rate"] = round(sum(1 for s in scored if s.get("truncated")) / n, 4)
    checked = [s for s in scored if s.get("rv")]
    if checked:
        out["rv_caught_rate"] = round(sum(1 for s in checked if s["rv"] != "pass") / len(checked), 4)
    confirmed = sum(int(s.get("confirmed") or 0) for s in scored)
    if confirmed:
        out["line_agreement"] = round(sum(int(s.get("lines_right") or 0) for s in scored) / confirmed, 4)
    return out


def _send_live(kwargs):
    import ai_utils
    import data_health
    return ai_utils.create_with_retry(ai_utils.get_client(timeout=180.0), retries=1, restaurant_id=None,
                                      action=EVAL_ACTION, readiness=data_health.NOT_APPLICABLE, **kwargs)


def _cost(model, msg) -> float:
    import ai_utils
    u = getattr(msg, "usage", None)
    if u is None:
        return 0.0
    try:
        return float(ai_utils._estimate_cost(model, int(getattr(u, "input_tokens", 0) or 0),
                                             int(getattr(u, "output_tokens", 0) or 0),
                                             int(getattr(u, "cache_creation_input_tokens", 0) or 0),
                                             int(getattr(u, "cache_read_input_tokens", 0) or 0)))
    except Exception as e:
        log.warning("ai_eval: cost unreadable: %s", e)
        return 0.0


def run(surface, model=None, version=None, limit=None, budget_usd=None, send=None, db_path=None,
        candidate=None) -> dict:
    """Replay `surface`'s frozen set on `model` (default: the production
    model) and score every answer. {surface, model, version, template,
    scores, hit_bound}. `send(kwargs) -> message` defaults to a live call
    through ai_utils.create_with_retry; a test passes its own. Stops before
    the next call once `budget_usd` is spent. An invoice set runs only from
    scripts/run_ai_eval.py --files (its scans are never stored).

    `candidate` (load_candidate: an edited prompt's parts) replays each item
    with the deployed template's parts replaced by the candidate's
    (render_with) — an item that does not split on the deployed template is
    an error — and the run's `template` is the candidate's. Without one,
    `template` is the one every item records it was rendered from (freeze),
    or None when they differ (no baseline can come of it)."""
    spec = SURFACES[surface]
    if spec["kind"] == "invoice":
        raise ValueError("invoice_extract runs from scripts/run_ai_eval.py with --files (its scans are never stored)")
    if candidate is not None:
        why = candidate_problems(surface, candidate)
        if why:
            raise ValueError("the candidate prompt cannot be replayed on the frozen set: " + "; ".join(why))
    model = model or production_model(surface)
    items = load_items(surface, version=version, limit=limit, db_path=db_path)
    send = send or _send_live
    scored, errors, cost, hit_bound = [], 0, 0.0, False
    templates = {(i.get("reference") or {}).get("template") for i in items}
    for item in items:
        if budget_usd is not None and cost >= float(budget_usd):
            hit_bound = True
            break
        request = item.get("request")
        if request and candidate is not None:
            request = render_with(surface, request, candidate)
        if not request:
            errors += 1
            continue
        kwargs = eval_request(request, model)
        try:
            msg = send(kwargs)
        except Exception as e:
            # A provider failure is not the model's answer: counted apart,
            # never as a refusal. The class only — no input or answer text.
            log.warning("ai_eval: %s %s failed: %s", surface, item["item_key"], type(e).__name__)
            errors += 1
            continue
        cost += _cost(model, msg)
        scored.append(score_item(surface, kwargs, msg))
    if candidate is not None:
        template = candidate.get("template")
    else:
        template = next(iter(templates)) if len(templates) == 1 else None
    return {"surface": surface, "model": model, "version": items[0]["version"] if items else version,
            "template": template, "scores": aggregate(scored, errors=errors, cost_usd=cost), "hit_bound": hit_bound}


def record_run(result, trigger="cli", deviations=None, run_key=None, db_path=None) -> str:
    """One ai_eval_runs row for a run(): its aggregate scores only."""
    run_key = run_key or uuid.uuid4().hex[:16]
    body = dict(result.get("scores") or {})
    if deviations is not None:
        body["deviations"] = list(deviations)
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO ai_eval_runs (run_key, surface, version, model, n, scores_json, cost_usd, "
                     "trigger_kind, build) VALUES (?,?,?,?,?,?,?,?,?)",
                     (run_key, result["surface"], result.get("version"), result["model"],
                      int(body.get("scored") or 0), json.dumps(body, default=str)[:4000],
                      float(body.get("cost_usd") or 0.0), str(trigger)[:20],
                      (os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT") or "")[:12] or None))
        conn.commit()
    finally:
        conn.close()
    return run_key


def latest_runs(trigger="smoke", db_path=None) -> dict:
    """{surface: {model, scores, created_at}} — the newest recorded run of
    each surface by `trigger`."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT surface, model, scores_json, created_at FROM ai_eval_runs WHERE trigger_kind=? "
                            "ORDER BY id DESC LIMIT 200", (trigger,)).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        if r["surface"] in out:
            continue
        try:
            scores = json.loads(r["scores_json"] or "{}")
        except ValueError:
            scores = {}
        out[r["surface"]] = {"model": r["model"], "scores": scores, "created_at": r["created_at"]}
    return out


def smoke_deviations(scores, baseline) -> list:
    """acceptance() plus a refusal rate SMOKE_DEVIATION_POINTS off the
    baseline's in either direction."""
    out = acceptance(scores, baseline)
    base = (baseline or {}).get("scores", baseline) or {}
    r, br = scores.get("refusal_rate"), base.get("refusal_rate")
    if r is not None and br is not None and abs(r - br) >= SMOKE_DEVIATION_POINTS and not out:
        out.append(f"refusal rate {r * 100:.1f}% against the baseline's {br * 100:.1f}%")
    return out


def run_smoke_eval(db_path=None, send=None) -> dict:
    """The weekly registry job ai_smoke_eval (Audit #25 A07-06). Off unless
    AI_SMOKE_EVAL=1 (a clean run that attempted nothing). On: up to
    AI_SMOKE_EVAL_ITEMS frozen items of each surface in SMOKE_SURFACES on
    its production model, inside AI_SMOKE_EVAL_BUDGET_USD for the whole
    pass, each surface's scores recorded (ai_eval_runs, trigger 'smoke')
    with their deviations from its baseline. Sends nothing. Returns the
    standard counts: a surface with no frozen set is skipped, one whose run
    raised failed, the budget running out is the bound."""
    counts = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False, "deviated": 0}
    if (os.getenv(SMOKE_ENV) or "").strip() != "1":
        counts["off"] = 1
        return counts
    try:
        budget = float(os.getenv(SMOKE_BUDGET_ENV) or SMOKE_BUDGET_DEFAULT)
    except ValueError:
        budget = SMOKE_BUDGET_DEFAULT
    try:
        per = max(1, int(os.getenv(SMOKE_ITEMS_ENV) or SMOKE_ITEMS_DEFAULT))
    except ValueError:
        per = SMOKE_ITEMS_DEFAULT
    import contextlib
    try:
        import model_gateway
        scope = model_gateway.call_class_scope("bulk")       # behind every owner's and every job's call
    except Exception as e:
        log.warning("ai_eval: the gateway's bulk class unavailable: %s", e)
        scope = contextlib.nullcontext()
    with scope:
        return _smoke_pass(counts, budget, per, db_path, send)


def smoke_baseline(surface, model) -> tuple:
    """(baseline, notes) a smoke run of `surface` on `model` is judged
    against: the model's own measured baseline; else — the production model
    moved off the code's by an env var or a console override, the one case
    the gate never sees (re-audit B7) — the code model's, with the gap
    named as a deviation."""
    own = read_baseline(surface, model)
    if own and own.get("status") == MEASURED:
        return own, []
    code = code_model(surface)
    if model == code:
        return own, []
    ref = read_baseline(surface, code)
    if ref and ref.get("status") == MEASURED:
        return ref, [f"no baseline for the model in force ({model}); compared with the code model's ({code})"]
    return None, [f"no baseline for the model in force ({model}), and none measured for the code model ({code})"]


def _smoke_pass(counts, budget, per, db_path, send):
    spent, run_key = 0.0, uuid.uuid4().hex[:16]
    for surface in SMOKE_SURFACES:
        if spent >= budget:
            counts["hit_bound"] = True
            break
        if not latest_version(surface, db_path):
            counts["skipped"] += 1
            continue
        counts["attempted"] += 1
        try:
            res = run(surface, limit=per, budget_usd=budget - spent, send=send, db_path=db_path)
            spent += float(res["scores"].get("cost_usd") or 0.0)
            counts["hit_bound"] = counts["hit_bound"] or res["hit_bound"]
            baseline, notes = smoke_baseline(surface, res["model"])
            dev = notes + smoke_deviations(res["scores"], baseline)
            scored, errors = int(res["scores"].get("scored") or 0), int(res["scores"].get("errors") or 0)
            broken = scored == 0 or errors > scored
            if broken:
                # The provider failing the calls (a retired model id, a key
                # that lost access) is not a clean week: a failed surface
                # and a deviation (re-audit B7 P1 probe: every call raising
                # read as ok with no deviation).
                dev.insert(0, f"{errors} of {scored + errors} calls failed at the provider — nothing was measured")
            record_run(res, trigger="smoke", deviations=dev, run_key=run_key, db_path=db_path)
            counts["deviated"] += 1 if dev else 0
            if broken:
                import ops
                ops.capture(RuntimeError(f"smoke eval of {surface} on {res['model']}: {errors} of "
                                         f"{scored + errors} calls failed at the provider"),
                            job="ai_smoke_eval", context=f"surface={surface}")
                counts["failed"] += 1
            else:
                counts["ok"] += 1
        except Exception as e:
            import ops
            ops.capture(e, job="ai_smoke_eval", context=f"surface={surface}")
            counts["failed"] += 1
    counts["cost_usd"] = round(spent, 4)
    return counts
