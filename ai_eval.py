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
  only — never an input, an answer or a name: the repo is public. A model
  change is accepted when, on the same set, the refusal rate is at most the
  baseline's plus REFUSAL_SLACK (2 points), the figure-mismatch rate is not
  above the baseline's and the invoice line agreement is not below it
  (acceptance()). tests/test_ai_model_change_needs_eval.py fails when
  ai_utils.MODELS or a tier's default model differs from the recorded
  fingerprint (eval_baselines/MODEL_FINGERPRINT.json) without a measured
  baseline for every surface on the models now in force.

  THE SMOKE (run_smoke_eval, the weekly registry job ai_smoke_eval). Off
  unless AI_SMOKE_EVAL=1. Replays a few frozen items of each text surface
  on the production model, inside AI_SMOKE_EVAL_BUDGET_USD, records the
  scores (ai_eval_runs) and their deviations from the baseline;
  ai_quality_watch warns on a deviation. Sends nothing.

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
    "previous": {...}, "hash"}: every default a model change moves — the
    call sites' own (ai_utils.MODELS) and the tiers' (ai_workflows.
    default_tier_models), not the env vars."""
    import ai_utils
    import ai_workflows as wf
    body = {"models": {p: d for p, (_env, d) in sorted(ai_utils.MODELS.items())},
            "tiers": wf.default_tier_models(),
            "previous": {"T1": ai_utils.HAIKU}}
    body["hash"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return body


def read_fingerprint(path=None) -> dict:
    try:
        with open(path or FINGERPRINT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def fingerprint_problems(record, bootstrap_hash) -> list:
    """Why the models in the code may not ship as they are, against the
    recorded fingerprint `record` (tests/test_ai_model_change_needs_eval.py):
      * the code's model defaults differ from the recorded ones — measure
        each surface on its new model, then record the fingerprint;
      * a surface's baseline on the model it now runs on is not measured —
        allowed only for the fingerprint the tooling shipped with
        (`bootstrap_hash`: "pending first run" then), never after a change;
      * a tier whose default model changed (record["tiers_changed"]) is not
        held to the canary restaurants (ai_workflows.TIER_CANARY_DEFAULT)
        and its window was not closed (scripts/run_ai_eval.py fingerprint
        --close-canary, at least a week later)."""
    import ai_workflows as wf
    out = []
    cur = model_fingerprint()
    if record.get("hash") != cur["hash"]:
        moved = sorted(p for p, m in cur["models"].items() if (record.get("models") or {}).get(p) != m)
        moved += sorted(f"tier {t}" for t, v in cur["tiers"].items() if (record.get("tiers") or {}).get(t) != v)
        if cur["previous"] != record.get("previous"):
            moved.append("the T1 canary's previous model")
        out.append("the AI models in the code differ from eval_baselines/MODEL_FINGERPRINT.json ("
                   + (", ".join(moved) or "fingerprint") + "): run scripts/run_ai_eval.py run --surface <s> --baseline "
                   "on Railway for every surface on its new model, commit the baseline files, then "
                   "`scripts/run_ai_eval.py fingerprint --write`")
    for surface in SURFACES:
        model = code_model(surface)
        b = read_baseline(surface, model) or {}
        if b.get("status") == MEASURED and (b.get("scores") or {}).get("scored"):
            continue
        if record.get("hash") == bootstrap_hash and cur["hash"] == bootstrap_hash and b.get("status") == PENDING:
            continue
        name = os.path.basename(baseline_path(surface, model))
        out.append(f"{surface} has no measured baseline on {model} (eval_baselines/{name} is "
                   f"{b.get('status') or 'missing'}) — a model change ships with its eval")
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
_BASELINE_KEYS = ("surface", "model", "status", "set_version", "n", "scores", "measured_on", "build", "note")
_SCORE_KEYS = ("n", "scored", "refusal_rate", "figure_mismatch_rate", "figures_per_item", "schema_fail_rate",
               "truncated_rate", "rv_caught_rate", "line_agreement", "errors", "cost_usd")


def baseline_body(result) -> dict:
    """What write_baseline stores for a run(): its aggregate scores and
    labels, nothing else — no input, answer, name or restaurant."""
    scores = {k: result["scores"].get(k) for k in _SCORE_KEYS if k in (result.get("scores") or {})}
    return {"surface": result["surface"], "model": result["model"], "status": MEASURED,
            "set_version": result.get("version"), "n": scores.get("scored"),
            "scores": scores, "measured_on": _now_utc().strftime("%Y-%m-%d"),
            "build": (os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT") or "")[:12] or None}


def write_baseline(result, path=None) -> str:
    body = baseline_body(result)
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
           "skipped": {"not_eligible": 0, "unreadable": 0}}
    conn = get_conn(db_path)
    try:
        for c in cands:
            if out["frozen"] + out["already"] >= int(n):
                break
            if not _eligible(c.get("restaurant_id"), db_path):
                out["skipped"]["not_eligible"] += 1
                continue
            request = c.get("request")
            if request is not None:
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
                 json.dumps(c["reference"]) if c.get("reference") else None))
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
    output format kept; the model replaced, and the production route's
    thinking and effort left out (create_with_retry applies the model's own
    defaults)."""
    kw = {k: request[k] for k in ("system", "messages", "max_tokens", "output_config") if k in (request or {})}
    kw["model"] = model
    oc = dict(kw.get("output_config") or {})
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


def run(surface, model=None, version=None, limit=None, budget_usd=None, send=None, db_path=None) -> dict:
    """Replay `surface`'s frozen set on `model` (default: the production
    model) and score every answer. {surface, model, version, scores,
    hit_bound}. `send(kwargs) -> message` defaults to a live call through
    ai_utils.create_with_retry; a test passes its own. Stops before the
    next call once `budget_usd` is spent. An invoice set runs only from
    scripts/run_ai_eval.py --files (its scans are never stored)."""
    spec = SURFACES[surface]
    if spec["kind"] == "invoice":
        raise ValueError("invoice_extract runs from scripts/run_ai_eval.py with --files (its scans are never stored)")
    model = model or production_model(surface)
    items = load_items(surface, version=version, limit=limit, db_path=db_path)
    send = send or _send_live
    scored, errors, cost, hit_bound = [], 0, 0.0, False
    for item in items:
        if budget_usd is not None and cost >= float(budget_usd):
            hit_bound = True
            break
        if not item.get("request"):
            errors += 1
            continue
        kwargs = eval_request(item["request"], model)
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
    return {"surface": surface, "model": model, "version": items[0]["version"] if items else version,
            "scores": aggregate(scored, errors=errors, cost_usd=cost), "hit_bound": hit_bound}


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
            dev = smoke_deviations(res["scores"], read_baseline(surface, res["model"]))
            record_run(res, trigger="smoke", deviations=dev, run_key=run_key, db_path=db_path)
            counts["deviated"] += 1 if dev else 0
            counts["ok"] += 1
        except Exception as e:
            import ops
            ops.capture(e, job="ai_smoke_eval", context=f"surface={surface}")
            counts["failed"] += 1
    counts["cost_usd"] = round(spent, 4)
    return counts
