#!/usr/bin/env python3
"""Replay invoice scans across model routes and score them against what the
owner confirmed (AI cost audit 10/7/26 #86).

The invoice read (invoices.extract, the invoice_extract workflow) runs on
the call site's own Opus until an eval clears a cheaper route. The photo
or PDF itself is never stored — only its sha256 (invoice_imports.image_sha),
the production read (lines_json) and what the owner applied (applied_json:
each line's ingredient and new_cost, by the owner — a line the
trusted-supplier rule applied is not counted). So this takes a folder of
invoice files; each file's sha256 finds its import, and that import is the
ground truth:

  lines      the confirmed lines the arm got right: the line for the same
             ingredient, with a proposed unit cost within 1% (or a cent)
             of the cost the owner applied — after invoices.propose, the
             same Python matching production runs
  total      the arm's printed invoice total against the production read's
             (the owner never confirms a total; agreeing with a read whose
             lines the owner applied is the evidence available), within a
             cent
  adds_up    the arm's lines whose quantity × unit price is the printed line
             total (invoices._adds_up) — the arithmetic check, which needs
             no ground truth
  plausible  the lines sum against the printed total (propose's total_check)
  tokens, seconds and cost per invoice

A file with no import (never scanned, or another restaurant's) is scored
on adds_up and plausible only.

    python3 scripts/invoice_model_eval.py --files ~/invoices --restaurant 5
    python3 scripts/invoice_model_eval.py --files ~/invoices --arms default,T4:low,T4:medium,T3 --live

Arms: "default" (invoices.MODEL with its own settings — production), and
"<tier>[:<effort>]" for a tier of ai_workflows (T1 Haiku, T2 Sonnet 5, T3
Sonnet 5.5, T4 Opus 5.5) at that effort (the tier's own when omitted; a
thinking tier only). The default arms are production against T4 at low and
medium and T3 at medium.

Without --live nothing is called: the plan, the ground truth each file has,
and a rough cost estimate are printed. A live run's calls are metered in
ai_usage as action "invoice_model_eval" against no restaurant. Reads the
database — a copy of production's, for real imports. Saves nothing.
"""
import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EVAL_ACTION = "invoice_model_eval"
DEFAULT_ARMS = "default,T4:low,T4:medium,T3"
COST_TOLERANCE_PCT = 1.0
COST_TOLERANCE_ABS = 0.01
# Rough input sizes for the estimate only (a live run reads the real usage):
# a phone photo of a page is about 1,600 image tokens; a PDF page about 2,500.
EST_IMAGE_TOKENS = 1600
EST_PDF_PAGE_TOKENS = 2500
EST_PDF_BYTES_PER_PAGE = 60_000
EST_OUTPUT_TOKENS = 1500

_MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
          ".gif": "image/gif", ".pdf": "application/pdf"}


# ── arms ───────────────────────────────────────────────────────────────────

def parse_arms(spec) -> list:
    """[{"name", "tier", "model", "effort"}] from "default,T4:low,T3"."""
    import ai_workflows as wf
    import invoices
    table = wf._tier_table()
    out = []
    for raw in [a.strip() for a in str(spec or "").split(",") if a.strip()]:
        if raw == "default":
            out.append({"name": "default", "tier": wf.DEFAULT, "model": invoices.MODEL, "effort": None})
            continue
        tier, _, effort = raw.partition(":")
        if tier not in table:
            raise ValueError(f"unknown arm {raw!r} (default, or one of {sorted(table)}[:effort])")
        base = table[tier]
        if effort and not base["effort"]:
            raise ValueError(f"{tier} is not a thinking tier; it takes no effort")
        eff = effort or base["effort"]
        out.append({"name": f"{tier}:{eff}" if eff else tier, "tier": tier, "model": base["model"], "effort": eff})
    return out


def request_for(arm, data, media_type) -> dict:
    """invoices.extract's request on the arm's route (ai_workflows.Route —
    what the orchestrator would send on that rung)."""
    import ai_workflows as wf
    import invoices
    kw = dict(model=invoices.MODEL, max_tokens=8000,
              output_config={"format": {"type": "json_schema", "schema": invoices._SCHEMA}},
              messages=[{"role": "user", "content": [invoices._content_block(data, media_type),
                                                     {"type": "text", "text": invoices._PROMPT}]}])
    return wf.Route(tier=arm["tier"], model=arm["model"], effort=arm["effort"]).apply(kw)


# ── ground truth ───────────────────────────────────────────────────────────

def load_files(folder) -> list:
    """[{"path", "media_type", "data", "sha"}] for every invoice file in the folder."""
    out = []
    for name in sorted(os.listdir(folder)):
        mt = _MEDIA.get(os.path.splitext(name)[1].lower())
        if not mt:
            continue
        path = os.path.join(folder, name)
        with open(path, "rb") as f:
            data = f.read()
        out.append({"path": path, "media_type": mt, "data": data, "sha": hashlib.sha256(data).hexdigest()})
    return out


def truth_for(sha, restaurant_id=None, db_path=None) -> dict | None:
    """The stored import for this file: {"restaurant_id", "import_id",
    "confirmed": {ingredient_id: unit_cost}, "invoice_total", "lines"} or
    None when it was never scanned (or, with `restaurant_id`, not here)."""
    import models
    conn = models.get_conn(db_path or models.DB_PATH)
    try:
        q = "SELECT * FROM invoice_imports WHERE image_sha=?"
        args = [sha]
        if restaurant_id:
            q += " AND restaurant_id=?"
            args.append(int(restaurant_id))
        row = conn.execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        body = json.loads(row["lines_json"] or "{}")
    except ValueError:
        body = {}
    try:
        applied = json.loads(row["applied_json"]) if row["applied_json"] else []
    except ValueError:
        applied = []
    # The owner's lines only (invoices.apply: "by" owner): a line the
    # trusted-supplier rule applied on its own is the read grading itself.
    confirmed = {}
    for a in applied if isinstance(applied, list) else []:
        if isinstance(a, dict) and a.get("ingredient_id") and a.get("new_cost") is not None \
                and a.get("by", "owner") == "owner":
            confirmed[int(a["ingredient_id"])] = float(a["new_cost"])
    return {"restaurant_id": row["restaurant_id"], "import_id": row["id"], "confirmed": confirmed,
            "invoice_total": ((body.get("total_check") or {}).get("invoice_total")),
            "lines": len(body.get("lines") or [])}


# ── scoring ────────────────────────────────────────────────────────────────

def _close(a, b) -> bool:
    if a is None or b is None:
        return False
    diff = abs(float(a) - float(b))
    return diff <= COST_TOLERANCE_ABS or (b and diff / abs(float(b)) * 100 <= COST_TOLERANCE_PCT)


def score(extracted, truth, restaurant_id=None, db_path=None) -> dict:
    """One arm's read of one invoice, scored (module docstring)."""
    import invoices
    lines = extracted.get("lines") or []
    checked = [invoices._adds_up(ln) for ln in lines]
    out = {"lines_read": len(lines),
           "adds_up": sum(1 for c in checked if c is True),
           "adds_up_checkable": sum(1 for c in checked if c is not None)}
    rid = (truth or {}).get("restaurant_id") or restaurant_id
    proposal = invoices.propose(rid, extracted, db_path=db_path or invoices.DB_PATH) if rid else None
    if proposal is not None:
        out["plausible"] = (proposal.get("total_check") or {}).get("plausible")
    if truth and truth.get("confirmed"):
        got = {}
        for ln in (proposal or {}).get("lines") or []:
            if ln.get("ingredient_id") and ln.get("proposed_cost") is not None:
                got.setdefault(int(ln["ingredient_id"]), []).append(float(ln["proposed_cost"]))
        right = sum(1 for ing, cost in truth["confirmed"].items() if any(_close(c, cost) for c in got.get(ing, [])))
        out.update(confirmed=len(truth["confirmed"]), lines_right=right)
    if truth and truth.get("invoice_total") is not None:
        out["total_right"] = _close(extracted.get("invoice_total"), truth["invoice_total"])
    return out


def summarise(rows) -> dict:
    """Per arm: line accuracy over every confirmed line, total agreement,
    the arithmetic rate, invoices that failed, tokens, seconds, cost."""
    by = {}
    for r in rows:
        a = by.setdefault(r["arm"], {"invoices": 0, "failed": 0, "confirmed": 0, "lines_right": 0, "totals": 0,
                                     "totals_right": 0, "adds_up": 0, "adds_up_checkable": 0, "cost_usd": 0.0,
                                     "seconds": 0.0, "input_tokens": 0, "output_tokens": 0})
        a["invoices"] += 1
        if r.get("error"):
            a["failed"] += 1
            continue
        s = r.get("score") or {}
        a["confirmed"] += s.get("confirmed", 0)
        a["lines_right"] += s.get("lines_right", 0)
        if "total_right" in s:
            a["totals"] += 1
            a["totals_right"] += 1 if s["total_right"] else 0
        a["adds_up"] += s.get("adds_up", 0)
        a["adds_up_checkable"] += s.get("adds_up_checkable", 0)
        for k in ("cost_usd", "seconds", "input_tokens", "output_tokens"):
            a[k] += r.get(k) or 0
    for a in by.values():
        a["line_accuracy"] = round(a["lines_right"] / a["confirmed"], 3) if a["confirmed"] else None
        a["total_accuracy"] = round(a["totals_right"] / a["totals"], 3) if a["totals"] else None
        a["arithmetic_rate"] = round(a["adds_up"] / a["adds_up_checkable"], 3) if a["adds_up_checkable"] else None
        a["cost_per_invoice"] = round(a["cost_usd"] / max(1, a["invoices"] - a["failed"]), 4)
    return by


# ── running ────────────────────────────────────────────────────────────────

def estimate(files, arms) -> dict:
    """A rough plan cost: {arm: usd} from the file sizes (no call made)."""
    import ai_utils
    out = {}
    for arm in arms:
        usd = 0.0
        for f in files:
            if f["media_type"] == "application/pdf":
                tin = max(1, len(f["data"]) // EST_PDF_BYTES_PER_PAGE) * EST_PDF_PAGE_TOKENS
            else:
                tin = EST_IMAGE_TOKENS
            usd += ai_utils._estimate_cost(arm["model"], tin + 400, EST_OUTPUT_TOKENS)
        out[arm["name"]] = round(usd, 4)
    return out


def run_one(arm, f, client=None):
    """One arm on one file, live: (extracted or None, error, usage dict)."""
    import ai_utils
    import data_health
    t0 = time.monotonic()
    try:
        msg = ai_utils.create_with_retry(client or ai_utils.get_client(timeout=300.0),
                                         readiness=data_health.NOT_APPLICABLE, restaurant_id=None,
                                         action=EVAL_ACTION, **request_for(arm, f["data"], f["media_type"]))
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:200]}", {"seconds": time.monotonic() - t0}
    u = getattr(msg, "usage", None)
    tin, tout = int(getattr(u, "input_tokens", 0) or 0), int(getattr(u, "output_tokens", 0) or 0)
    usage = {"seconds": round(time.monotonic() - t0, 1), "input_tokens": tin, "output_tokens": tout,
             "cost_usd": ai_utils._estimate_cost(arm["model"], tin, tout,
                                                 int(getattr(u, "cache_creation_input_tokens", 0) or 0),
                                                 int(getattr(u, "cache_read_input_tokens", 0) or 0))}
    if getattr(msg, "stop_reason", None) in ("refusal", "max_tokens"):
        return None, f"stop_reason {msg.stop_reason}", usage
    try:
        return json.loads(ai_utils.extract_text(msg)), None, usage
    except ValueError:
        return None, "not JSON", usage


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--files", required=True, help="a folder of invoice photos / PDFs")
    ap.add_argument("--restaurant", type=int, help="only imports of this restaurant are ground truth")
    ap.add_argument("--arms", default=DEFAULT_ARMS)
    ap.add_argument("--db", help="the database (default models.DB_PATH)")
    ap.add_argument("--live", action="store_true", help="call the models (metered as invoice_model_eval)")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args(argv)
    arms = parse_arms(args.arms)
    files = load_files(args.files)
    truths = {f["sha"]: truth_for(f["sha"], args.restaurant, args.db) for f in files}
    plan = {"files": len(files), "with_truth": sum(1 for t in truths.values() if t),
            "confirmed_lines": sum(len((t or {}).get("confirmed") or {}) for t in truths.values()),
            "arms": [a["name"] for a in arms], "estimate_usd": estimate(files, arms)}
    if not args.live:
        print(json.dumps(plan, indent=2) if args.json else
              f"{plan['files']} file(s), {plan['with_truth']} with an import ({plan['confirmed_lines']} confirmed "
              f"lines); arms {', '.join(plan['arms'])}; rough cost {plan['estimate_usd']}. Add --live to run.")
        return plan
    rows = []
    for f in files:
        for arm in arms:
            extracted, err, usage = run_one(arm, f)
            row = {"file": os.path.basename(f["path"]), "arm": arm["name"], "error": err, **usage}
            if extracted is not None:
                row["score"] = score(extracted, truths.get(f["sha"]), args.restaurant, args.db)
            rows.append(row)
    result = {"plan": plan, "rows": rows, "summary": summarise(rows)}
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        for name, a in result["summary"].items():
            print(f"{name:14} lines {a['line_accuracy']}  totals {a['total_accuracy']}  arithmetic "
                  f"{a['arithmetic_rate']}  failed {a['failed']}/{a['invoices']}  ${a['cost_per_invoice']}/invoice  "
                  f"{a['seconds'] / max(1, a['invoices']):.0f}s")
    return result


if __name__ == "__main__":
    main()
