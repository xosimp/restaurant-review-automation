#!/usr/bin/env python3
"""Replay real schedule weeks across model × effort × prompt variant.

Every schedule call's full input and answer is stored (schedule_model_calls,
schedule_output.record_call — schedule audit 10/3/26 PR-31). This replays
stored generations on other arms and scores each week on what the owner's
week depends on, before and after the deterministic backstops:

  hard        hard breaches in the model's own rows (schedule_rules.violations)
  mgr_min     minutes somebody is on with no manager, before / after repair
  ft_under    full-time people under their stated minimum hours
  repaired    rows the backstops added, removed or changed
  quality     Shift Quality of the repaired week (shift_quality.score_rows)
  unmet       what the repaired week does not meet (schedule_output.unmet_items)
  tokens, seconds, and cost per completed week (a week is complete when no
  call refused, errored or stopped short, and every trading day has rows)

    python3 scripts/schedule_model_eval.py --restaurant 5 --weeks 8
    python3 scripts/schedule_model_eval.py --ids 7012,7020 --models claude-opus-5-5,claude-sonnet-5-5 \\
        --efforts high,medium --prompts stored,rerender --live

The stored production answer is always scored as the "production" arm, free.
Prompt variants: "stored" (the exact request the week was generated with),
"rerender" (the prompt today's code builds from the stored arguments),
"rerender:<contract>" (the same, answered on another output contract —
schedule_output.CONTRACTS: "rerender:compact" the one-letter-key rows,
"rerender:shape" the slots without names, whose people are then solved in
code by schedule_engine.assign_shape_slots against the week's Constraints
before the week is scored — AI cost audit 10/7/26 #69, #70), or
"module:function" (a transform of the stored request: fn(request, call) ->
request; its user turn is the list of text blocks schedule_prompt.
request_content builds — the standing instructions, the restaurant's week,
this request).

    python3 scripts/schedule_model_eval.py --restaurant 5 --tiers T3 \\
        --prompts rerender,rerender:compact,rerender:shape --live

A shape arm's week also reports `unstaffed_slots`: slots nobody could
legally work, left out of the week (never filled illegally). Without --live nothing is called: the plan and an estimate of its
cost (from the stored calls' own token counts) are printed. A live run's calls
are metered in ai_usage as action "schedule_model_eval" against no restaurant.

Reads the database — a copy of production's, for real weeks. Saves nothing.
"""
import argparse
import copy
import importlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EVAL_ACTION = "schedule_model_eval"
PRODUCTION = "production"


# ── arms ───────────────────────────────────────────────────────────────────

def contract_of(prompt):
    """The output contract a prompt variant asks for: "compact" for
    "rerender:compact", None for every other variant (the call's own)."""
    import schedule_output as so
    head, _, tail = str(prompt or "").partition(":")
    if head == "rerender" and tail:
        if tail not in so.CONTRACTS:
            raise ValueError(f"unknown contract {tail!r} (one of {', '.join(so.CONTRACTS)})")
        return tail
    return None


def _is_rerender(prompt) -> bool:
    return prompt == "rerender" or str(prompt or "").startswith("rerender:")


def arms_from(models, efforts, prompts) -> list:
    """[{"name", "model", "effort", "prompt", "contract"}] — every
    combination; a model that takes no effort (the thinking-off shape) gets
    one arm per prompt."""
    import labor
    out, seen = [], set()
    for m in models:
        for e in (efforts if labor.schedule_model_thinks(m) else [None]):
            for p in prompts:
                key = (m, e, p)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"name": f"{m}/{e or '-'}/{p}", "model": m, "effort": e, "prompt": p,
                            "contract": contract_of(p)})
    return out


def tier_arms(tiers, prompts) -> list:
    """Arms for tiers of the labor_schedule ladder (ai_workflows: T3 Sonnet
    5.5, T4 Opus 5.5, each at its tier's effort) - what the generator runs
    on since 10/7/26 (AI orchestration, owner decision 3), named
    "<tier>:<model>/<effort>/<prompt>" so a report reads by tier."""
    import ai_workflows as wf
    table = wf._tier_table()
    out = []
    for t in tiers:
        if t not in table:
            raise ValueError(f"unknown tier {t!r} (one of {sorted(table)})")
        m, e = table[t]["model"], table[t]["effort"]
        for p in prompts:
            out.append({"name": f"{t}:{m}/{e or '-'}/{p}", "model": m, "effort": e, "prompt": p, "tier": t,
                        "contract": contract_of(p)})
    return out


def apply_arm(request: dict, arm: dict) -> dict:
    """The request as `arm` would send it: its model, and the thinking,
    effort and max_tokens labor.generate_optimized_schedule gives that
    model. The output format and the prompt are kept."""
    import labor
    req = copy.deepcopy(request)
    for k in ("restaurant_id", "action", "readiness", "stream", "deadline"):
        req.pop(k, None)              # create_with_retry's own, not the API's
    req["model"] = arm["model"]
    oc = dict(req.get("output_config") or {})
    if labor.schedule_model_thinks(arm["model"]):
        req["thinking"] = {"type": "adaptive", "display": "summarized"}
        oc["effort"] = arm.get("effort") or labor.SCHEDULE_EFFORT
        req["max_tokens"] = labor.SCHEDULE_MAX_TOKENS_THINKING
    else:
        req.pop("thinking", None)
        oc.pop("effort", None)
        req["max_tokens"] = 16000
    if oc:
        req["output_config"] = oc
    else:
        req.pop("output_config", None)
    return req


def default_call_model(request: dict):
    """One live call, streamed, metered as an evaluation (no restaurant, so
    no restaurant's budget pays for it)."""
    import ai_utils
    import data_health
    return ai_utils.create_with_retry(ai_utils.get_client(timeout=360.0), readiness=data_health.NOT_APPLICABLE,
                                      restaurant_id=None, action=EVAL_ACTION, stream=True, **request)


def _transform(spec):
    mod, _, fn = spec.partition(":")
    return getattr(importlib.import_module(mod), fn)


# ── one call, one arm ──────────────────────────────────────────────────────

def _text_of(msg) -> str:
    from ai_utils import extract_text
    try:
        return extract_text(msg) or ""
    except Exception:
        return ""


def _usage(msg) -> dict:
    u = getattr(msg, "usage", None)
    return {"input_tokens": int(getattr(u, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
            "cache_read_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0),
            "cache_write_tokens": int(getattr(u, "cache_creation_input_tokens", 0) or 0)}


def _cost(model, usage) -> float:
    from ai_utils import _estimate_cost
    return _estimate_cost(model, usage.get("input_tokens"), usage.get("output_tokens"),
                          usage.get("cache_write_tokens"), usage.get("cache_read_tokens"))


def _csv_rows(text, dates=None) -> list:
    """Rows of a CSV-contract answer (the fallback), as the job reads them."""
    import schedule_versions as sv
    from schedule_output import CSV_HEADER
    body = (text or "").split("---SUMMARY---", 1)[0]
    lines = [ln.strip().strip('"') for ln in body.split("\n") if ln.count(",") >= 5
             and not ln.lower().replace(" ", "").startswith("date,day,employee")]
    rows = sv.rows_from_csv(CSV_HEADER + "\n" + "\n".join(lines))
    return [r for r in rows if not dates or r.get("date") in set(dates)]


def rows_of_answer(text, call) -> dict:
    """{"rows", "complete": bool} of one stored or replayed answer, read by
    the contract the call asked for."""
    import schedule_output as so
    req = call.get("request") or {}
    if (req.get("output_config") or {}).get("format"):
        # Read by the contract the call answered on (#69, #70): a shape
        # answer's slots come back with no names, for run() to assign.
        p = so.parse_answer(text, dates=call.get("dates"), contract=call.get("contract"))
        rows = [{k: v for k, v in r.items() if k != "_slot"} for r in p["rows"]]
        return {"rows": rows, "complete": p["parsed"] and not p["partial_dates"]}
    rows = _csv_rows(text, call.get("dates"))
    return {"rows": rows, "complete": bool(rows)}


def replay_call(call, arm, call_model) -> dict:
    """One stored call on `arm`: {"rows", "complete", "stop_reason", "usage",
    "cost", "seconds", "error"}."""
    t0 = time.time()
    try:
        if _is_rerender(arm["prompt"]):
            return _rerender(call, arm, call_model, t0)
        request = apply_arm(call.get("request") or {}, arm)
        if arm["prompt"] != "stored":
            request = _transform(arm["prompt"])(request, call)
        msg = call_model(request)
    except Exception as e:
        return {"rows": [], "complete": False, "stop_reason": None, "usage": {}, "cost": 0.0,
                "seconds": round(time.time() - t0, 1), "error": f"{type(e).__name__}: {e}"[:300]}
    stop = getattr(msg, "stop_reason", None)
    usage = _usage(msg)
    got = rows_of_answer(_text_of(msg), call) if stop != "refusal" else {"rows": [], "complete": False}
    return {"rows": got["rows"], "complete": got["complete"] and stop not in ("max_tokens", "refusal",
                                                                             "model_context_window_exceeded"),
            "stop_reason": stop, "usage": usage, "cost": round(_cost(arm["model"], usage), 4),
            "seconds": round(time.time() - t0, 1), "error": None}


def _rerender(call, arm, call_model, t0) -> dict:
    """The prompt today's code builds from the call's stored arguments,
    answered on `arm`. labor.generate_optimized_schedule runs whole — its
    prompt, schema and parse — with its model call replaced by the arm's
    and its record of the call switched off (an evaluation is not a week).
    An arm with a contract ("rerender:shape") answers on it; a shape
    answer's slots come back as rows with no names (run() assigns them)."""
    import labor
    inputs = dict(call.get("inputs") or {})
    if not inputs:
        raise ValueError("no stored arguments to rebuild the prompt from")
    # The arm's contract, else the one the call answered on — never whatever
    # SCHEDULE_CONTRACT happens to say where the eval runs.
    import schedule_output as so
    inputs["contract"] = arm.get("contract") or inputs.get("contract") or so.contract_base(call.get("contract"))
    seen = {}

    def _fake_create(client, **kw):
        msg = call_model(apply_arm(kw, arm))
        seen["msg"] = msg
        return msg
    saved = (labor.create_with_retry, labor._record_schedule_call)
    labor.create_with_retry = _fake_create
    labor._record_schedule_call = lambda *a, **k: None
    try:
        inputs["generation_id"] = None
        result = labor.generate_optimized_schedule(**inputs)
    finally:
        labor.create_with_retry, labor._record_schedule_call = saved
    msg = seen.get("msg")
    usage = _usage(msg) if msg is not None else {}
    import schedule_versions as sv
    slots = [{k: v for k, v in r.items() if k != "_slot"} for r in result.get("shape_rows") or []]
    return {"rows": sv.rows_from_csv(result.get("schedule_csv") or "") + slots,
            "complete": not result.get("truncated") and not result.get("partial_dates"),
            "stop_reason": result.get("stop_reason"), "usage": usage,
            "cost": round(_cost(arm["model"], usage), 4) if usage else 0.0,
            "seconds": round(time.time() - t0, 1), "error": None}


def _merge_rows(calls, results) -> list:
    """The week's rows from its calls, as the generation combined them: per
    date and part of the roster, the last call that wrote that date wins (a
    retry replaces its slice; a truncated whole-week call is replaced by the
    parts written after it)."""
    by_key = {}
    for call, res in zip(calls, results):
        shift = (((((call.get("request") or {}).get("output_config") or {}).get("format") or {}).get("schema") or {})
                 .get("properties", {}).get("days", {}).get("items", {}).get("properties", {})
                 .get("shifts", {}).get("items", {}).get("properties", {}))
        # The roster enum under the schema's key or the compact one (#70); a
        # shape schema names nobody, so its call's roster stands in.
        enum = (shift.get("employee") or shift.get("e") or {}).get("enum")
        chunk = frozenset(enum or [n for n, _r in ((call.get("inputs") or {}).get("roster") or [])])
        dated = {}
        for r in res.get("rows") or []:
            dated.setdefault(r.get("date"), []).append(r)
        for d in call.get("dates") or []:
            if d in dated or (res.get("complete") and not res.get("error")):
                by_key[(d, chunk)] = dated.get(d, [])
    return [r for _k, rows in sorted(by_key.items(), key=lambda kv: kv[0][0]) for r in rows]


# ── scoring a week ─────────────────────────────────────────────────────────

def week_context(generation, calls, db_path=None) -> dict:
    """What every arm of one week is scored against, built once so the
    comparison is fair: the Constraints for the week (the roster the
    generation used), its hours budget and daily targets, its trading
    dates."""
    import schedule_rules as sr
    from datetime import date as _d, timedelta as _t
    from models import get_restaurant, get_conn
    rid = generation["restaurant_id"]
    monday = _d.fromisoformat(generation["week_start"])
    week = [(monday + _t(days=i)).isoformat() for i in range(7)]
    days = [_d.fromisoformat(x).strftime("%A") for x in week]
    restaurant = get_restaurant(rid)
    c = sr.build_constraints(rid, week, days, restaurant)
    first = next((x for x in calls if x.get("inputs")), {}) or {}
    roster = [(str(n), str(r or "")) for n, r in ((first.get("inputs") or {}).get("roster") or [])]
    if roster:
        c.roster_names = [n for n, _r in roster]
        c.active = {n.strip().lower() for n, _r in roster}
        c.roster_roles = {n: r for n, r in roster}
    c.closed_dates = set(c.closed_dates or ()) | set((first.get("inputs") or {}).get("closed_dates") or ())
    budget, targets = 0.0, {}
    if generation.get("history_id"):
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            h = conn.execute("SELECT hours_budget, economics_json FROM schedule_history WHERE id=?",
                             (generation["history_id"],)).fetchone()
        finally:
            conn.close()
        if h:
            budget = float(h["hours_budget"] or 0)
            try:
                targets = (json.loads(h["economics_json"] or "{}") or {}).get("daily_target_hours") or {}
            except ValueError:
                targets = {}
    return {"restaurant_id": rid, "week": week, "constraints": c, "roster_roles": dict(roster),
            "hours_budget": budget, "daily_target_hours": targets,
            "trading": [x for x in week if x not in c.closed_dates]}


def assign_open_slots(ctx, rows, signals=None) -> tuple:
    """(rows, unstaffed): rows with no name — a shape answer's slots — given
    their people by schedule_engine.assign_shape_slots against the week's
    Constraints, with every named row held as it is; the slots nobody can
    legally work are left out and counted. Rows that all carry a name come
    back unchanged."""
    named = [r for r in rows if (r.get("employee") or "").strip()]
    open_rows = [r for r in rows if not (r.get("employee") or "").strip()]
    if not open_rows:
        return rows, 0
    from schedule_engine import assign_shape_slots
    weights = None
    if signals is None:
        try:
            from schedule_engine import quality_inputs_from_db, _quality_signals
            inputs = quality_inputs_from_db(ctx["restaurant_id"], daily_target_hours=ctx.get("daily_target_hours"),
                                            week_rows=named)
            signals, weights = _quality_signals(ctx["restaurant_id"], inputs)
        except Exception as e:
            print(f"  slot signals not built ({type(e).__name__}: {e}); solving on the rules alone")
            c = ctx["constraints"]
            signals = {"roster": list(getattr(c, "roster_names", None) or []),
                       "roster_roles": dict(ctx.get("roster_roles") or {})}
    got = assign_shape_slots(open_rows, named, ctx["constraints"], signals=signals, weights=weights,
                             roster_roles=ctx.get("roster_roles") or None)
    return named + got["assigned"], len(got["unassigned"])


def default_repair(rows, c, roster_roles=None) -> list:
    """The job's own repair (schedule_engine.repair_rows — the ranked loop
    every generation runs, schedule audit 10/3/26 P-47): the person repairs,
    a manager every minute, the owner's floors, the stations, the closers,
    the overtime rebalance and the minimum hours, in rank order until nothing
    changes. Pass --repair module:function to replay another."""
    from schedule_engine import repair_rows
    return repair_rows([dict(r) for r in rows], c, roster_roles)


def _sig(r):
    return ((r.get("employee") or "").strip().lower(), r.get("date") or "", r.get("role") or "",
            r.get("shift_start") or "", r.get("shift_end") or "")


def _breaches(rows, c) -> dict:
    import schedule_rules as sr
    viols = sr.violations(rows, c)
    prof = sr.breach_profile(rows, c, viols)
    return {"viols": viols, "hard": sum(1 for v in viols if v.get("hard")),
            "manager_minutes": int(sum((prof.get("manager") or {}).values()))}


def _full_time_under(rows, c) -> int:
    import schedule_output as so
    return sum(1 for u in so.unmet_items(rows, constraints=c) if u["kind"] == "min_hours" and u.get("full_time"))


def _quality(rid, rows, targets):
    try:
        from schedule_engine import quality_inputs_from_db, _quality_signals
        import shift_quality as sq
        inputs = quality_inputs_from_db(rid, daily_target_hours=targets, week_rows=rows)
        signals, weights = _quality_signals(rid, inputs)
        q = sq.score_rows(rows, profiles=inputs.get("shift_profiles") or None, weights=weights, **signals)
        return q
    except Exception as e:
        print(f"  quality not scored: {type(e).__name__}: {e}")
        return {"checked": False}


def score_week(ctx, rows, repair=None) -> dict:
    """The scores of one arm's week (see the module docstring)."""
    import schedule_output as so
    c = ctx["constraints"]
    before = _breaches(rows, c)
    fixed = (repair or default_repair)(rows, c, ctx.get("roster_roles"))
    after = _breaches(fixed, c)
    q = _quality(ctx["restaurant_id"], fixed, ctx.get("daily_target_hours"))
    unmet = so.unmet_items(fixed, constraints=c, violations=after["viols"], quality=q,
                           hours_budget=ctx.get("hours_budget"))
    changed = len({_sig(r) for r in rows} ^ {_sig(r) for r in fixed})
    written = {r.get("date") for r in rows if r.get("date")}
    return {"rows": len(rows), "hard_before": before["hard"], "hard_after": after["hard"],
            "manager_minutes_before": before["manager_minutes"], "manager_minutes_after": after["manager_minutes"],
            "full_time_under_min": _full_time_under(fixed, c), "rows_repaired": changed,
            "quality": (q or {}).get("score") if (q or {}).get("checked") else None,
            "unmet": len(unmet), "days_missing": sorted(set(ctx["trading"]) - written)}


# ── the run ────────────────────────────────────────────────────────────────

def load_weeks(restaurant_ids=None, history_ids=None, weeks=12, db_path=None) -> list:
    """[(generation, calls)] — the stored generations to replay, newest first."""
    import schedule_output as so
    out = []
    for g in so.generations(restaurant_ids=restaurant_ids, history_ids=history_ids, limit=weeks, db_path=db_path):
        calls = so.load_calls(generation_id=g["generation_id"], db_path=db_path)
        if calls:
            out.append((g, calls))
    return out


def production_result(calls) -> list:
    """The stored answers, read as the arm "production"."""
    out = []
    for call in calls:
        got = rows_of_answer(call.get("answer") or "", call) if call.get("outcome") not in ("refused", "error") \
            else {"rows": [], "complete": False}
        usage = call.get("usage") or {}
        out.append({"rows": got["rows"],
                    "complete": got["complete"] and call.get("outcome") not in ("truncated", "refused", "error"),
                    "stop_reason": call.get("stop_reason"), "usage": usage,
                    "cost": round(_cost(call.get("model"), usage), 4) if usage else 0.0,
                    "seconds": call.get("seconds") or 0.0, "error": call.get("error")})
    return out


def run(weeks, arms, call_model=None, repair=None, live=False, db_path=None) -> dict:
    """{"arms": {name: summary}, "by_tier": {tier: summary}, "weeks": [per-week
    detail]}. The production arm is always scored; the other arms only when
    `live` (or with an injected `call_model`, which is what tests pass).
    `by_tier` rolls every week up by the ladder tier that wrote it — a tier
    arm's replays, and production's weeks written on one tier — so the
    ladder's decision reads hard breaches, manager minutes and unmet items
    beside quality and cost (re-audit 10/7/26 #8: the first decision rested
    on one week and on quality and cost alone)."""
    call_model = call_model or default_call_model
    detail, per_arm, per_tier = [], {}, {}
    for generation, calls in weeks:
        ctx = week_context(generation, calls, db_path=db_path)
        row = {"generation_id": generation["generation_id"], "restaurant_id": generation["restaurant_id"],
               "week_start": generation["week_start"], "history_id": generation.get("history_id"),
               # The tier(s) production wrote it on (schedule_model_calls.tier, 10/7/26).
               "tiers": sorted({c.get("tier") for c in calls if c.get("tier")}), "arms": {}}
        plan = [({"name": PRODUCTION}, production_result(calls))]
        if live:
            for arm in arms:
                plan.append((arm, [replay_call(call, arm, call_model) for call in calls]))
        for arm, results in plan:
            rows = _merge_rows(calls, results)
            # A shape arm's slots (or a stored shape week's) are given their
            # people the way the generation gives them (#69), then scored.
            rows, unstaffed = assign_open_slots(ctx, rows)
            s = score_week(ctx, rows, repair=repair)
            s["unstaffed_slots"] = unstaffed
            usage = {k: sum(int((r.get("usage") or {}).get(k) or 0) for r in results)
                     for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")}
            s.update(usage)
            s["cost"] = round(sum(float(r.get("cost") or 0) for r in results), 4)
            s["seconds"] = round(sum(float(r.get("seconds") or 0) for r in results), 1)
            s["errors"] = [r["error"] for r in results if r.get("error")]
            s["stop_reasons"] = [r.get("stop_reason") for r in results]
            s["completed"] = (all(r.get("complete") for r in results) and not s["errors"]
                              and not s["days_missing"])
            row["arms"][arm["name"]] = s
            per_arm.setdefault(arm["name"], []).append(s)
            tier = arm.get("tier") if arm["name"] != PRODUCTION else \
                (row["tiers"][0] if len(row["tiers"]) == 1 else None)
            if tier:
                per_tier.setdefault(tier, []).append(s)
        detail.append(row)
    return {"arms": {name: summarize(scores) for name, scores in per_arm.items()},
            "by_tier": {t: summarize(scores) for t, scores in sorted(per_tier.items())}, "weeks": detail}


def summarize(scores) -> dict:
    """An arm across its weeks: means, and cost per completed week."""
    def mean(k):
        vals = [s[k] for s in scores if s.get(k) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None
    done = sum(1 for s in scores if s.get("completed"))
    total_cost = sum(float(s.get("cost") or 0) for s in scores)
    return {"weeks": len(scores), "completed": done,
            "hard_before": mean("hard_before"), "hard_after": mean("hard_after"),
            "manager_minutes_before": mean("manager_minutes_before"),
            "manager_minutes_after": mean("manager_minutes_after"),
            "full_time_under_min": mean("full_time_under_min"), "rows_repaired": mean("rows_repaired"),
            "quality": mean("quality"), "unmet": mean("unmet"), "unstaffed_slots": mean("unstaffed_slots"),
            "input_tokens": mean("input_tokens"), "output_tokens": mean("output_tokens"),
            "seconds": mean("seconds"), "cost": round(total_cost, 4),
            "cost_per_completed_week": round(total_cost / done, 4) if done else None}


def estimate(weeks, arms) -> dict:
    """{arm name: dollars} for a live run — the stored calls' own token
    counts priced at each arm's model (output tokens at another effort will
    differ: an estimate, said as one)."""
    out = {}
    for arm in arms:
        total = 0.0
        for _g, calls in weeks:
            for call in calls:
                total += _cost(arm["model"], call.get("usage") or {})
        out[arm["name"]] = round(total, 2)
    return out


def _print(report):
    cols = ("weeks", "completed", "hard_before", "hard_after", "manager_minutes_before", "manager_minutes_after",
            "full_time_under_min", "rows_repaired", "quality", "unmet", "input_tokens", "output_tokens",
            "seconds", "cost_per_completed_week")
    heads = ("weeks", "done", "hard", "hard_fix", "mgr_min", "mgr_fix", "ft_under", "repaired", "quality",
             "unmet", "tok_in", "tok_out", "secs", "$/week")
    width = max([len(n) for n in report["arms"]] + [10])
    print(f"{'arm':<{width}}  " + "  ".join(f"{h:>9}" for h in heads))
    for name, s in report["arms"].items():
        print(f"{name:<{width}}  " + "  ".join(f"{'-' if s.get(k) is None else s.get(k):>9}" for k in cols))
    if report.get("by_tier"):
        # The ladder's evidence by tier (re-audit 10/7/26 #8): manager
        # minutes and unmet items sit beside quality and cost.
        tcols = ("weeks", "completed", "hard_after", "manager_minutes_before", "manager_minutes_after", "unmet",
                 "quality", "cost_per_completed_week")
        theads = ("weeks", "done", "hard_fix", "mgr_min", "mgr_fix", "unmet", "quality", "$/week")
        print("\nby tier")
        print(f"{'tier':<{width}}  " + "  ".join(f"{h:>9}" for h in theads))
        for tier, s in report["by_tier"].items():
            print(f"{tier:<{width}}  " + "  ".join(f"{'-' if s.get(k) is None else s.get(k):>9}" for k in tcols))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--restaurant", type=int, action="append", help="weeks of this restaurant (repeatable)")
    ap.add_argument("--ids", help="comma-separated schedule_history ids")
    ap.add_argument("--weeks", type=int, default=12, help="newest stored weeks to replay")
    ap.add_argument("--models", default="claude-opus-5-5,claude-sonnet-5-5")
    ap.add_argument("--efforts", default="high")
    ap.add_argument("--prompts", default="stored",
                    help="stored, rerender, rerender:compact, rerender:shape, or module:function (comma-separated)")
    ap.add_argument("--tiers", help="labor_schedule ladder tiers to replay instead of --models/--efforts (e.g. T3,T4)")
    ap.add_argument("--repair", help="module:function(rows, constraints, roster_roles) -> rows")
    ap.add_argument("--live", action="store_true", help="make the calls (costs money); otherwise plan only")
    ap.add_argument("--json", help="write the full report here")
    args = ap.parse_args(argv)

    history_ids = [int(x) for x in (args.ids or "").split(",") if x.strip()]
    weeks = load_weeks(restaurant_ids=args.restaurant, history_ids=history_ids or None, weeks=args.weeks)
    if not weeks:
        ap.error("no stored schedule calls for those weeks (schedule_model_calls is filled by generations "
                 "made since it was added)")
    prompts = [p.strip() for p in args.prompts.split(",") if p.strip()]
    if args.tiers:
        arms = tier_arms([t.strip() for t in args.tiers.split(",") if t.strip()], prompts)
    else:
        arms = arms_from([m.strip() for m in args.models.split(",") if m.strip()],
                         [e.strip() for e in args.efforts.split(",") if e.strip()], prompts)
    calls = sum(len(c) for _g, c in weeks)
    print(f"{len(weeks)} week(s), {calls} stored call(s), {len(arms)} arm(s)")
    for name, dollars in estimate(weeks, arms).items():
        print(f"  {name}: about ${dollars:,.2f} at the stored token counts")
    if not args.live:
        print("Plan only — pass --live to make these calls. Scoring the stored production answers:")
    repair = None
    if args.repair:
        fn = _transform(args.repair)
        repair = lambda rows, c, roster_roles=None: fn(rows, c, roster_roles)  # noqa: E731
    report = run(weeks, arms, repair=repair, live=args.live)
    _print(report)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=1, default=str)
        print(f"report written to {args.json}")


if __name__ == "__main__":
    main()
