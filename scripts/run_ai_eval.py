#!/usr/bin/env python3
"""Freeze, replay and baseline the AI evaluation sets (ai_eval, Audit #25
R13 — PROMPT_LIBRARY.md → Evaluation).

Run where the database is — on Railway, `railway ssh "python3
scripts/run_ai_eval.py ..."` — because the sets are stored production inputs:

    python3 scripts/run_ai_eval.py freeze --surface dsr_narrative --n 25
    python3 scripts/run_ai_eval.py run --surface dsr_narrative                 # the production model
    python3 scripts/run_ai_eval.py run --surface dsr_narrative --baseline      # first measure what is in force
    python3 scripts/run_ai_eval.py run --surface dsr_narrative --model claude-sonnet-6 --baseline
    python3 scripts/run_ai_eval.py run --surface invoice_extract --files ~/invoices --baseline
    python3 scripts/run_ai_eval.py template --surface ask > ask_prompt.json    # LOCALLY, on the edited code
    python3 scripts/run_ai_eval.py run --surface ask --candidate - --baseline < ask_prompt.json
    python3 scripts/run_ai_eval.py fingerprint            # the recorded fingerprint against the code
    python3 scripts/run_ai_eval.py fingerprint --write    # record it (after the baselines are in)
    python3 scripts/run_ai_eval.py fingerprint --write --unmeasured-template ask --reason "..."
    python3 scripts/run_ai_eval.py fingerprint --close-canary   # a week after a tier change

Surfaces: dsr_narrative, review_reply, ask, invoice_extract, review_analysis.

`run` prints the aggregate scores and the verdict against the REFERENCE —
the baseline in force before it, on the same frozen set
(ai_eval.choose_reference): re-measuring the model and prompt in force
compares with its own baseline; a new model or an edited prompt compares
with the baseline of the model and prompt in force (measure that first). It
exits 1 when the run is invalid (too few answers scored, more than a tenth
of the calls failing at the provider, cut short by its budget, a different
set or item count from the reference) or regressed (refusal rate over the
reference's + 2 points or over 30% outright, more figure mismatches, a
lower invoice line agreement). `--baseline` writes
eval_baselines/<surface>__<model>.json — SCORES ONLY, never an input, an
answer, a name or a restaurant (the repo is public) — only when the verdict
is clean; nothing is written otherwise. A regression may be written with
`--accept-regression "<why>"`, which the file records and the gate reads;
an invalid run never. Copy the file into the repo and commit it with the
change that moves the model or the prompt.

A prompt edit is measured BEFORE it ships: `template --surface S` (run on
the edited checkout, locally) exports the surface's prompt parts as JSON;
`run --candidate FILE` (on Railway; `-` reads stdin) replays the frozen set
with the deployed parts replaced by the candidate's, and records the
candidate's template hash (recomputed, never trusted). An edit the frozen
set cannot replay — a value added or removed, a note inside a value — is
refused: it ships only recorded with its reason (`fingerprint --write
--unmeasured-template S --reason "..."`), and is measured after deploy from
a fresh freeze.

An invoice's scan is never stored, so invoice_extract runs only with --files
(each file found by its sha256, as scripts/invoice_model_eval.py does) and
freeze records the owner-confirmed ground truth only.

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


def _invoice_run(args, candidate=None):
    """invoice_extract against its frozen ground truth, the scans from
    --files: each matched by sha256 and read on the model
    (scripts/invoice_model_eval.run_one — the production request on a
    route), checked by extraction_verdict's rule, its lines scored by
    scripts/invoice_model_eval.score after invoices.propose. A `candidate`
    prompt and schema stand in for invoices._PROMPT / _SCHEMA for the run
    (the request is built from them at run time)."""
    import ai_eval
    import invoices
    if candidate is None:
        return _invoice_pass(args, ai_eval.template_hash("invoice_extract"))
    saved = (invoices._PROMPT, invoices._SCHEMA)
    try:
        invoices._PROMPT = candidate["parts"]["prompt"]["segments"][0]
        invoices._SCHEMA = json.loads(candidate["parts"]["schema"]["segments"][0])
        return _invoice_pass(args, candidate["template"])
    finally:
        invoices._PROMPT, invoices._SCHEMA = saved


def _invoice_pass(args, template):
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
            "version": args.version or ai_eval.latest_version("invoice_extract"), "template": template,
            "scores": ai_eval.aggregate(scored, errors, cost), "hit_bound": cost >= args.max_usd,
            "files_matched": len(files), "items": len(items)}


def cmd_freeze(args):
    import ai_eval
    _print(ai_eval.freeze(args.surface, n=args.n, version=args.version))
    return 0


def _candidate(args):
    """The --candidate prompt export (a path, or - for stdin), its hash
    recomputed (ai_eval.load_candidate); None without one."""
    import ai_eval
    if not args.candidate:
        return None
    text = sys.stdin.read() if args.candidate == "-" else open(args.candidate, encoding="utf-8").read()
    cand = ai_eval.load_candidate(text)
    if cand.get("surface") and cand["surface"] != args.surface:
        raise SystemExit(f"the candidate is a {cand['surface']} prompt, not {args.surface}")
    return cand


def cmd_template(args):
    """The surface's prompt parts as this checkout holds them, for `run
    --candidate` on Railway (code text only — no input or answer)."""
    import ai_eval
    parts = ai_eval.template_parts(args.surface)
    _print({"surface": args.surface, "template": ai_eval.parts_hash(parts), "parts": parts})
    return 0


def cmd_run(args):
    import ai_eval
    cand = _candidate(args)
    if cand is not None and args.surface != "invoice_extract":
        why = ai_eval.candidate_problems(args.surface, cand)
        if why:
            print("the candidate prompt cannot be replayed on the frozen set: " + "; ".join(why))
            print("record it instead: scripts/run_ai_eval.py fingerprint --write --unmeasured-template "
                  f"{args.surface} --reason \"...\" (measure it after deploy from a fresh freeze)")
            return 2
    if args.surface == "invoice_extract":
        if not args.files:
            print("invoice_extract needs --files: its scans are never stored (scripts/invoice_model_eval.py).")
            return 2
        res = _invoice_run(args, cand)
    else:
        res = ai_eval.run(args.surface, model=args.model, version=args.version, limit=args.limit,
                          budget_usd=args.max_usd, candidate=cand)
    ai_eval.record_run(res, trigger="cli")
    _print({k: res[k] for k in ("surface", "model", "version", "template", "scores", "hit_bound") if k in res})
    verdict = ai_eval.run_verdict(args.surface, res, new_set=args.new_set)
    ref = verdict["reference"]
    if ref and ref.get("scores") is not None:
        print(f"reference: {ref['model']} (template {ref.get('template')}) on set {ref.get('set_version')}")
    elif ref and ref.get("new_set"):
        print(f"reference: none — the comparison starts over on set {res.get('version')} (was {ref.get('from_set')})")
    else:
        print("reference: none (a first measurement — the absolute floors apply)")
    for line in verdict["invalid"]:
        print("INVALID:", line)
    for line in verdict["regressions"]:
        print("REGRESSED:", line)
    if not verdict["invalid"] and not verdict["regressions"]:
        print("acceptance: PASS")
    if args.baseline:
        if verdict["invalid"]:
            print("no baseline written — the run is not a measurement")
            return 1
        if verdict["regressions"] and not (args.accept_regression or "").strip():
            print("no baseline written — a regression is written only with --accept-regression \"<why>\"")
            return 1
        path = ai_eval.write_baseline(res, reference=ref,
                                      accepted_regression=args.accept_regression if verdict["regressions"] else None)
        print("baseline written:", path)
        return 0
    return 1 if (verdict["invalid"] or verdict["regressions"]) else 0


def _fingerprint_record(prev=None):
    """The fingerprint file's body for the code as it stands: the models,
    the tiers, each eval surface's prompt template, each surface's baseline
    on its code model (status, set, template) with the model and template in
    force before a change (`previous` — the gate holds the new baseline to
    a comparison with it), the prompts recorded as unmeasured, and the tier
    canary window a tier change opens (ai_workflows.TIER_CANARY_DEFAULT)."""
    import ai_eval
    fp = ai_eval.model_fingerprint()
    prev = prev or {}
    baselines = {}
    for s in ai_eval.SURFACES:
        model, template = ai_eval.code_model(s), fp["templates"][s]
        b = ai_eval.read_baseline(s, model) or {}
        entry = {"model": model, "template": template, "status": b.get("status") or ai_eval.PENDING,
                 "set_version": b.get("set_version")}
        p = (prev.get("baselines") or {}).get(s) or {}
        if p and (p.get("model") != model or (p.get("template") and p.get("template") != template)):
            entry["previous"] = {k: p.get(k) for k in ("model", "template", "status", "set_version")}
        elif p.get("previous"):
            entry["previous"] = p["previous"]
        baselines[s] = entry
    rec = {"hash": fp["hash"], "models": fp["models"], "tiers": fp["tiers"], "previous": fp["previous"],
           "templates": fp["templates"], "baselines": baselines,
           # The tooling's own first record, its baselines still "pending first run" (informational: the gate
           # reads tests/test_ai_model_change_needs_eval.py's BOOTSTRAP_SURFACES).
           "bootstrap": bool(prev.get("bootstrap")) and all(b["status"] == ai_eval.PENDING
                                                             for b in baselines.values())}
    unmeasured = {}
    for s, u in (prev.get("unmeasured_templates") or {}).items():
        b = ai_eval.read_baseline(s, ai_eval.code_model(s)) or {}
        if u.get("template") == fp["templates"].get(s) and b.get("template") != u.get("template"):
            unmeasured[s] = u                       # still unmeasured: kept until a baseline on it lands
    if unmeasured:
        rec["unmeasured_templates"] = unmeasured
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
    if args.unmeasured_template:
        s = args.unmeasured_template
        if s not in ai_eval.SURFACES:
            print(f"--unmeasured-template must be one of {', '.join(ai_eval.SURFACES)}")
            return 2
        if not (args.reason or "").strip():
            print("--unmeasured-template needs --reason: why the frozen set cannot replay this prompt")
            return 2
        if not args.write:
            print("--unmeasured-template is recorded with --write")
            return 2
        rec.setdefault("unmeasured_templates", {})[s] = {
            "template": rec["templates"][s], "reason": args.reason.strip()[:300],
            "on": datetime.now(timezone.utc).date().isoformat()}
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
    r.add_argument("--candidate", help="an edited prompt exported by `template` (a path, or - for stdin)")
    r.add_argument("--new-set", action="store_true",
                   help="re-measure the model and prompt in force on a newer set: the comparison starts over there")
    r.add_argument("--accept-regression", default="",
                   help="write a regressed baseline anyway, recording this reason (never an invalid run)")
    t = sub.add_parser("template")
    t.add_argument("--surface", required=True)
    g = sub.add_parser("fingerprint")
    g.add_argument("--write", action="store_true")
    g.add_argument("--close-canary", action="store_true")
    g.add_argument("--unmeasured-template", help="record this surface's edited prompt as measured after deploy")
    g.add_argument("--reason", default="")
    args = p.parse_args(argv)
    import ai_eval
    if getattr(args, "surface", None) and args.surface not in ai_eval.SURFACES:
        p.error(f"--surface must be one of {', '.join(ai_eval.SURFACES)}")
    return {"freeze": cmd_freeze, "run": cmd_run, "template": cmd_template,
            "fingerprint": cmd_fingerprint}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
