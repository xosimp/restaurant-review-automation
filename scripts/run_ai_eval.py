#!/usr/bin/env python3
"""Freeze, replay and baseline the AI evaluation sets (ai_eval, Audit #25
R13 — PROMPT_LIBRARY.md → Evaluation).

Run where the database is — on Railway, `railway ssh "python3
scripts/run_ai_eval.py ..."` — because the sets are stored production inputs:

    python3 scripts/run_ai_eval.py freeze --surface dsr_narrative --n 25
    python3 scripts/run_ai_eval.py run --surface dsr_narrative                 # the production model
    python3 scripts/run_ai_eval.py run --surface dsr_narrative --model claude-sonnet-5-5 --baseline
    python3 scripts/run_ai_eval.py run --surface invoice_extract --files ~/invoices --baseline
    python3 scripts/run_ai_eval.py fingerprint            # the recorded fingerprint against the code
    python3 scripts/run_ai_eval.py fingerprint --write    # record it (after the baselines are in)
    python3 scripts/run_ai_eval.py fingerprint --close-canary   # a week after a tier change

Surfaces: dsr_narrative, review_reply, ask, invoice_extract, review_analysis.

`run` prints the aggregate scores and, when a baseline is recorded for the
surface and model, the acceptance verdict (exit 1 when it fails):
refusal rate at most the baseline's + 2 points, figure mismatches not above
the baseline's, invoice line agreement not below it. `--baseline` writes
eval_baselines/<surface>__<model>.json — SCORES ONLY, never an input, an
answer, a name or a restaurant (the repo is public); commit it with the
change that moves the model. An invoice's scan is never stored, so
invoice_extract runs only with --files (each file found by its sha256, as
scripts/invoice_model_eval.py does) and freeze records the owner-confirmed
ground truth only.

A run's calls go through ai_utils.create_with_retry (the gateway, the
ledger — action "ai_eval", no restaurant). `--max-usd` bounds a run (default
5). Nothing is sent to anyone.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _print(obj):
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _invoice_verdict_ok(extracted) -> bool:
    """invoices.extraction_verdict's rule on a parsed read: a JSON object
    whose product lines add up to its printed total (or cannot be checked)."""
    import invoices
    if not isinstance(extracted, dict):
        return False
    try:
        total = float(extracted["invoice_total"]) if extracted.get("invoice_total") else None
        chk = invoices.totals_check(extracted.get("lines"), total)
    except (TypeError, ValueError):
        chk = None
    return not (chk and not chk["plausible"])


def _invoice_run(args):
    """invoice_extract against its frozen ground truth, the scans from
    --files: each matched by sha256 and read on the model
    (scripts/invoice_model_eval.run_one — the production request on a
    route), checked by extraction_verdict's rule, its lines scored by
    scripts/invoice_model_eval.score after invoices.propose."""
    import ai_eval
    import ai_workflows as wf
    import invoices
    from scripts import invoice_model_eval as ime
    model = args.model or ai_eval.production_model("invoice_extract")
    items = {(i.get("reference") or {}).get("image_sha"): i
             for i in ai_eval.load_items("invoice_extract", version=args.version)}
    files = [f for f in ime.load_files(args.files) if f["sha"] in items]
    # The production read keeps its own settings (the default route); any
    # other model goes out on a plain route of its own.
    arm = {"name": "eval", "tier": wf.DEFAULT if model == invoices.MODEL else "eval", "model": model, "effort": None}
    scored, errors, cost = [], 0, 0.0
    for f in files:
        if cost >= args.max_usd:
            break
        item = items[f["sha"]]
        extracted, err, usage = ime.run_one(arm, f)
        cost += float(usage.get("cost_usd") or 0)
        if extracted is None and not str(err or "").startswith(("stop_reason", "not JSON")):
            errors += 1               # the provider failed: not the model's answer
            continue
        truth = dict(item["reference"])
        truth["confirmed"] = {int(k): v for k, v in (truth.get("confirmed") or {}).items()}
        score = ime.score(extracted, truth) if extracted else {}
        scored.append(ai_eval.score_invoice(item, extracted, _invoice_verdict_ok(extracted),
                                            lines_right=score.get("lines_right", 0),
                                            truncated=str(err or "").endswith("max_tokens")))
    return {"surface": "invoice_extract", "model": model,
            "version": args.version or ai_eval.latest_version("invoice_extract"),
            "scores": ai_eval.aggregate(scored, errors, cost), "hit_bound": cost >= args.max_usd,
            "files_matched": len(files), "items": len(items)}


def cmd_freeze(args):
    import ai_eval
    _print(ai_eval.freeze(args.surface, n=args.n, version=args.version))
    return 0


def cmd_run(args):
    import ai_eval
    if args.surface == "invoice_extract":
        if not args.files:
            print("invoice_extract needs --files: its scans are never stored (scripts/invoice_model_eval.py).")
            return 2
        res = _invoice_run(args)
    else:
        res = ai_eval.run(args.surface, model=args.model, version=args.version, limit=args.limit,
                          budget_usd=args.max_usd)
    ai_eval.record_run(res, trigger="cli")
    _print({k: res[k] for k in ("surface", "model", "version", "scores", "hit_bound") if k in res})
    base = ai_eval.read_baseline(res["surface"], res["model"])
    failed = []
    if base and base.get("status") == ai_eval.MEASURED:
        failed = ai_eval.acceptance(res["scores"], base)
        print("acceptance:", "PASS" if not failed else "FAIL — " + "; ".join(failed))
    if args.baseline:
        if not res["scores"].get("scored"):
            print("no answer was scored — no baseline written")
            return 1
        print("baseline written:", ai_eval.write_baseline(res))
    return 1 if failed and not args.baseline else 0


def _fingerprint_record(prev=None):
    """The fingerprint file's body for the code as it stands: the models,
    the tiers, each surface's baseline status on its code model, and the
    tier canary window a tier change opens (ai_workflows.TIER_CANARY_DEFAULT)."""
    import ai_eval
    fp = ai_eval.model_fingerprint()
    baselines = {}
    for s in ai_eval.SURFACES:
        model = ai_eval.code_model(s)
        b = ai_eval.read_baseline(s, model)
        baselines[s] = {"model": model, "status": (b or {}).get("status") or ai_eval.PENDING}
    rec = {"hash": fp["hash"], "models": fp["models"], "tiers": fp["tiers"], "previous": fp["previous"],
           "baselines": baselines, "bootstrap": False}
    prev = prev or {}
    changed = sorted(t for t, v in fp["tiers"].items() if prev.get("tiers") and prev["tiers"].get(t) != v)
    if changed:
        today = datetime.now(timezone.utc).date()
        rec["tiers_changed"] = changed
        rec["tier_canary_until"] = (today + timedelta(days=7)).isoformat()
    elif prev.get("tiers_changed") and not prev.get("canary_closed"):
        rec["tiers_changed"] = prev["tiers_changed"]
        rec["tier_canary_until"] = prev.get("tier_canary_until")
    return rec


def cmd_fingerprint(args):
    import ai_eval
    try:
        with open(ai_eval.FINGERPRINT_FILE, encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = {}
    if args.close_canary:
        until = prev.get("tier_canary_until")
        if not prev.get("tiers_changed"):
            print("no tier canary window is open")
            return 1
        if until and datetime.now(timezone.utc).date().isoformat() < until:
            print(f"the canary window runs until {until}: a week on the canary restaurants first")
            return 1
        prev["canary_closed"] = datetime.now(timezone.utc).date().isoformat()
        _write(ai_eval.FINGERPRINT_FILE, prev)
        print("canary closed — empty ai_workflows.TIER_CANARY_DEFAULT in the same commit")
        return 0
    rec = _fingerprint_record(prev)
    if not args.write:
        _print({"recorded": prev.get("hash"), "code": rec["hash"], "same": prev.get("hash") == rec["hash"],
                "baselines": rec["baselines"]})
        return 0 if prev.get("hash") == rec["hash"] else 1
    pending = [s for s, b in rec["baselines"].items() if b["status"] != ai_eval.MEASURED]
    if pending and prev.get("hash") != rec["hash"]:
        print("baselines still pending for " + ", ".join(pending) + " — the test will fail until each is measured "
              "on its new model (run --baseline); written anyway so the gap shows in the file.")
    _write(ai_eval.FINGERPRINT_FILE, rec)
    print("fingerprint written:", rec["hash"])
    return 0


def _write(path, body):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(body, f, indent=2, sort_keys=True)
        f.write("\n")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--surface", required=True)
    f.add_argument("--n", type=int, default=25)
    f.add_argument("--version")
    r = sub.add_parser("run")
    r.add_argument("--surface", required=True)
    r.add_argument("--model")
    r.add_argument("--version")
    r.add_argument("--limit", type=int)
    r.add_argument("--files")
    r.add_argument("--max-usd", type=float, default=5.0)
    r.add_argument("--baseline", action="store_true")
    g = sub.add_parser("fingerprint")
    g.add_argument("--write", action="store_true")
    g.add_argument("--close-canary", action="store_true")
    args = p.parse_args(argv)
    import ai_eval
    if getattr(args, "surface", None) and args.surface not in ai_eval.SURFACES:
        p.error(f"--surface must be one of {', '.join(ai_eval.SURFACES)}")
    return {"freeze": cmd_freeze, "run": cmd_run, "fingerprint": cmd_fingerprint}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
