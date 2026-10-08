"""ai_learning — which route gives each workflow the best quality per dollar,
measured on production runs (AI orchestration design, owner-approved 10/7/26).

Weekly (job "ai_route_learning"), per workflow and per the route its runs
ended on (tier and model), over the last WINDOW_DAYS of ai_runs:

    runs, cost per run, p50/p95 latency, escalation rate, the share that
    passed their check, the owner's acceptance (accepted / edited / rejected
    / corrected, from record_outcome) and the mean outcome quality, and the
    shadow reviewer's mean score

It writes RECOMMENDATIONS to ai_route_recommendations for the admin console —
never applies one (owner decision 1, 10/7/26: recommend-only). The single
automatic action is a REVERT: a console override whose runs since it was
applied are accepted clearly less often than the runs before it goes back to
the policy it replaced, and the console says why.

What it recommends, and the evidence each needs (thresholds below):

  start_higher   a ladder's first rung escalates on ESCALATION_HIGH of runs:
                 starting on the second rung is cheaper than paying for both
  start_lower    shadow replays of a cheaper tier on the same inputs score
                 within SHADOW_TOLERANCE of production's, on enough pairs
  quality_low    owners accept fewer than ACCEPT_LOW of a route's runs

Nothing is recommended on fewer than MIN_RUNS runs (or MIN_OUTCOMES
outcomes, for acceptance): a quiet workflow stays where it is.
"""
import hashlib
import json
import logging
import os
import statistics
import threading

import ai_orchestrator as orch
import ai_workflows as wf

log = logging.getLogger("ai_learning")

WINDOW_DAYS = 28
MIN_RUNS = 20
MIN_OUTCOMES = 10
ESCALATION_HIGH = 0.25
ACCEPT_LOW = 0.60
SHADOW_TOLERANCE = 0.05
REVERT_DROP = 0.15          # acceptance points lost since an override, to revert it
REVERT_MIN_DAYS = 3


def _conn(db_path=None):
    return orch._conn(db_path)


def _pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))
    return xs[k]


def _accept(rows):
    with_outcome = [r for r in rows if r["outcome"] and r["outcome"] != "ignored"]
    if not with_outcome:
        return None, 0
    good = [r for r in with_outcome if r["outcome"] in ("accepted", "edited") and (r["outcome_quality"] or 0) >= 0.5]
    return len(good) / len(with_outcome), len(with_outcome)


def route_stats(days=WINDOW_DAYS, workflow=None, db_path=None) -> list:
    """[{workflow, tier, model, policy_version, runs, cost_per_run, p50_ms,
    p95_ms, escalation_rate, pass_rate, acceptance, outcomes, quality,
    shadow_score}] over production runs (never shadow replays), one row per
    route AND policy version: runs under an earlier version of the defaults
    are never pooled with today's (re-audit 10/7/26 #9)."""
    conn = _conn(db_path)
    try:
        sql = ("SELECT workflow, final_tier, final_model, status, escalations, latency_ms, cost_usd, outcome, "
               "outcome_quality, reviewer, reviewer_score, start_tier, policy_version FROM ai_runs "
               "WHERE shadow_of IS NULL AND created_at >= datetime('now', ?)")
        args = [f"-{int(days)} days"]
        if workflow:
            sql += " AND workflow=?"
            args.append(workflow)
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
    groups = {}
    for r in rows:
        groups.setdefault((r["workflow"], r["final_tier"], r["final_model"], r["policy_version"]), []).append(r)
    out = []
    for (wfl, tier, model, version), rs in sorted(groups.items(),
                                                  key=lambda kv: (kv[0][0], str(kv[0][1]), str(kv[0][3]))):
        acc, n_out = _accept(rs)
        q = [r["outcome_quality"] for r in rs if r["outcome_quality"] is not None]
        shadow = [r["reviewer_score"] for r in rs if r["reviewer_score"] is not None]
        out.append({
            "workflow": wfl, "tier": tier, "model": model, "policy_version": version, "runs": len(rs),
            "cost_per_run": round(sum(r["cost_usd"] or 0 for r in rs) / len(rs), 5),
            "p50_ms": _pct([r["latency_ms"] for r in rs], 0.5), "p95_ms": _pct([r["latency_ms"] for r in rs], 0.95),
            "escalation_rate": round(sum(1 for r in rs if (r["escalations"] or 0) > 0) / len(rs), 3),
            "pass_rate": round(sum(1 for r in rs if r["status"] == "ok") / len(rs), 3),
            "acceptance": round(acc, 3) if acc is not None else None, "outcomes": n_out,
            "quality": round(statistics.mean(q), 3) if q else None,
            "shadow_score": round(statistics.mean(shadow), 3) if shadow else None,
        })
    return out


def _escalation_by_start(days, db_path):
    """{workflow: (runs started on the first rung, of which escalated)} —
    only runs whose first attempt ran on the FIRST rung of the ladder in
    force (ai_runs.first_tier), under today's POLICY_VERSION, and since the
    workflow's console override was applied: a run on an earlier ladder
    says nothing about this one (re-audit 10/7/26 #9)."""
    since = {}
    for ov in orch.override_rows(db_path):
        since[ov["workflow"]] = ov.get("updated_at") or ""
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT workflow, first_tier, created_at, escalations FROM ai_runs "
                            "WHERE shadow_of IS NULL AND created_at >= datetime('now', ?) "
                            "AND (start_tier IS NULL OR start_tier='') AND policy_version=?",
                            (f"-{int(days)} days", wf.POLICY_VERSION)).fetchall()
    finally:
        conn.close()
    first = {}
    out = {}
    for r in rows:
        wfl = r["workflow"]
        if wfl not in first:
            pol = wf.policy(wfl, db_path)
            first[wfl] = pol.ladder[0] if pol.ladder else None
        if not r["first_tier"] or r["first_tier"] != first[wfl]:
            continue
        if since.get(wfl) and str(r["created_at"] or "") < since[wfl]:
            continue
        n, e = out.get(wfl, (0, 0))
        out[wfl] = (n + 1, e + (1 if (r["escalations"] or 0) > 0 else 0))
    return out


def _shadow_pairs(days, db_path):
    """{(workflow, candidate_tier): [(production_score, candidate_score, prod_cost, cand_cost)]}

    Both scores and both costs come from the SHADOW row (re-audit 10/7/26
    #2): production's text re-scored by the same rubric with the same
    context as the candidate's (context_json production_score), and
    production's FIRST call's cost (production_first_cost) — a pair
    recorded without either is not counted."""
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT workflow, final_tier tier, reviewer_score cs, cost_usd cc, "
            "json_extract(context_json, '$.production_score') ps, "
            "json_extract(context_json, '$.production_first_cost') pc FROM ai_runs "
            "WHERE shadow_of IS NOT NULL AND created_at >= datetime('now', ?) AND reviewer_score IS NOT NULL "
            "AND json_valid(context_json)", (f"-{int(days)} days",)).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        if r["ps"] is None or r["pc"] is None:
            continue
        out.setdefault((r["workflow"], r["tier"]), []).append((float(r["ps"]), r["cs"], float(r["pc"] or 0),
                                                              r["cc"] or 0))
    return out


def _open(conn, workflow, kind):
    return conn.execute("SELECT id FROM ai_route_recommendations WHERE workflow=? AND kind=? AND status='open'",
                        (workflow, kind)).fetchone()


def _recommend(conn, workflow, kind, summary, proposed, evidence):
    if _open(conn, workflow, kind):
        conn.execute("UPDATE ai_route_recommendations SET summary=?, proposed_json=?, evidence_json=?, "
                     "created_at=datetime('now') WHERE workflow=? AND kind=? AND status='open'",
                     (summary, json.dumps(proposed), json.dumps(evidence), workflow, kind))
    else:
        conn.execute("INSERT INTO ai_route_recommendations (workflow, kind, summary, proposed_json, evidence_json) "
                     "VALUES (?,?,?,?,?)", (workflow, kind, summary, json.dumps(proposed), json.dumps(evidence)))


def recommend(days=WINDOW_DAYS, db_path=None) -> list:
    """Write this week's recommendations; returns them."""
    made = []
    held = set()      # (workflow, kind) whose premise holds this pass
    stats = [s for s in route_stats(days, db_path=db_path) if s.get("policy_version") == wf.POLICY_VERSION]
    esc = _escalation_by_start(days, db_path)
    pairs = _shadow_pairs(days, db_path)
    conn = _conn(db_path)
    try:
        for wfl, (n, e) in esc.items():
            pol = wf.policy(wfl, db_path)
            if len(pol.ladder) < 2 or n < MIN_RUNS:
                continue
            rate = e / n
            if rate >= ESCALATION_HIGH:
                proposed = {"ladder": list(pol.ladder[1:])}
                summary = (f"{wfl}: {round(rate * 100)}% of {n} runs needed the stronger model — start there "
                           f"({pol.ladder[1]}), it costs less than paying for both")
                _recommend(conn, wfl, "start_higher", summary, proposed,
                           {"runs": n, "escalated": e, "rate": round(rate, 3)})
                made.append(summary)
                held.add((wfl, "start_higher"))
        for s in stats:
            if s["acceptance"] is not None and s["outcomes"] >= MIN_OUTCOMES and s["acceptance"] < ACCEPT_LOW:
                summary = (f"{s['workflow']} on {s['tier']} ({s['model']}): owners used "
                           f"{round(s['acceptance'] * 100)}% of {s['outcomes']} outputs — look at the prompt or a "
                           f"stronger route")
                _recommend(conn, s["workflow"], "quality_low", summary, {}, s)
                made.append(summary)
                held.add((s["workflow"], "quality_low"))
        for (wfl, tier), ps in pairs.items():
            if len(ps) < MIN_RUNS:
                continue
            pol = wf.policy(wfl, db_path)
            if candidate_tier(wfl, db_path) != tier:
                continue          # replays of a tier the ladder in force no longer starts above
            prod = statistics.mean(p for p, _c, _pc, _cc in ps)
            cand = statistics.mean(c for _p, c, _pc, _cc in ps)
            pc = statistics.mean(x for _p, _c, x, _cc in ps)
            cc = statistics.mean(x for _p, _c, _pc, x in ps)
            if cand >= prod - SHADOW_TOLERANCE and cc < pc:
                ladder = [tier] + [t for t in pol.ladder if t != tier]
                summary = (f"{wfl}: {tier} scored {cand:.2f} against production's {prod:.2f} on {len(ps)} replays, "
                           f"at {cc / pc * 100 if pc else 0:.0f}% of the cost — start on {tier}")
                _recommend(conn, wfl, "start_lower", summary, {"ladder": ladder},
                           {"pairs": len(ps), "prod_score": round(prod, 3), "cand_score": round(cand, 3),
                            "prod_cost": round(pc, 5), "cand_cost": round(cc, 5)})
                made.append(summary)
                held.add((wfl, "start_lower"))
        # An open recommendation whose premise no longer holds (the ladder
        # it was about changed, the version moved on, the evidence faded)
        # is closed, never left to be applied on stale evidence (#9).
        for r in conn.execute("SELECT id, workflow, kind FROM ai_route_recommendations WHERE status='open' "
                              "AND kind IN ('start_higher','start_lower','quality_low')").fetchall():
            if (r["workflow"], r["kind"]) not in held:
                conn.execute("UPDATE ai_route_recommendations SET status='expired', decided_by='system:ai_learning', "
                             "decided_at=datetime('now') WHERE id=?", (r["id"],))
        conn.commit()
    finally:
        conn.close()
    return made


def revert_regressions(db_path=None) -> list:
    """Revert a console override whose acceptance fell REVERT_DROP below the
    runs before it, once it has MIN_OUTCOMES outcomes and REVERT_MIN_DAYS of
    runs. The one automatic action (owner decision 1, 10/7/26)."""
    reverted = []
    for ov in orch.override_rows(db_path):
        wfl, since = ov["workflow"], ov["updated_at"]
        conn = _conn(db_path)
        try:
            after = [dict(r) for r in conn.execute(
                "SELECT outcome, outcome_quality FROM ai_runs WHERE workflow=? AND shadow_of IS NULL AND created_at>=? "
                "AND created_at <= datetime('now')", (wfl, since)).fetchall()]
            before = [dict(r) for r in conn.execute(
                "SELECT outcome, outcome_quality FROM ai_runs WHERE workflow=? AND shadow_of IS NULL AND created_at<? "
                "AND created_at >= datetime(?, ?)", (wfl, since, since, f"-{WINDOW_DAYS} days")).fetchall()]
            age_ok = conn.execute("SELECT julianday('now') - julianday(?) >= ?", (since, REVERT_MIN_DAYS)).fetchone()[0]
        finally:
            conn.close()
        a_after, n_after = _accept(after)
        a_before, n_before = _accept(before)
        if not age_ok or a_after is None or a_before is None or n_after < MIN_OUTCOMES or n_before < MIN_OUTCOMES:
            continue
        if a_before - a_after < REVERT_DROP:
            continue
        previous = json.loads(ov["previous_json"]) if ov.get("previous_json") else None
        reason = (f"owners used {round(a_after * 100)}% of outputs since this change on {since[:10]}, against "
                  f"{round(a_before * 100)}% before it")
        try:
            change = orch.set_override(wfl, previous, actor="system:ai_learning", reason="reverted: " + reason,
                                       db_path=db_path)
        except Exception as e:
            log.warning("revert of %s failed: %s", wfl, e)
            continue
        try:
            import admin_events
            admin_events.record_admin_action("system:ai_learning", "ai_route.reverted", target=("workflow", wfl),
                                             before=change["before"], after=change["after"], summary=reason,
                                             db_path=db_path)
        except Exception:
            pass
        conn = _conn(db_path)
        try:
            conn.execute("INSERT INTO ai_route_recommendations (workflow, kind, summary, proposed_json, evidence_json, "
                         "status, decided_by, decided_at) VALUES (?,?,?,?,?,'applied','system:ai_learning',"
                         "datetime('now'))", (wfl, "reverted", f"{wfl}: reverted — {reason}", json.dumps(previous),
                                              json.dumps({"before": a_before, "after": a_after,
                                                          "n_before": n_before, "n_after": n_after})))
            conn.commit()
        finally:
            conn.close()
        reverted.append(wfl)
    return reverted


def recommendations(status="open", db_path=None) -> list:
    conn = _conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM ai_route_recommendations WHERE status=? ORDER BY created_at DESC LIMIT 100",
            (status,)).fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def decide(rec_id, apply, actor, db_path=None) -> dict:
    """The console's answer to one recommendation: apply it (as an override,
    audited by the caller) or dismiss it."""
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM ai_route_recommendations WHERE id=? AND status='open'", (rec_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError("no open recommendation with that id")
    change = None
    if apply:
        proposed = json.loads(row["proposed_json"] or "{}") or {}
        if not proposed:
            raise ValueError("this recommendation has nothing to apply — it asks for a look, not a route")
        current = orch.overrides(db_path).get(row["workflow"]) or {}
        change = orch.set_override(row["workflow"], dict(current, **proposed), actor=actor,
                                   reason=f"recommendation {rec_id}: {row['summary']}", db_path=db_path)
    conn = _conn(db_path)
    try:
        conn.execute("UPDATE ai_route_recommendations SET status=?, decided_by=?, decided_at=datetime('now') "
                     "WHERE id=?", ("applied" if apply else "dismissed", str(actor or "")[:80], rec_id))
        conn.commit()
    finally:
        conn.close()
    return {"workflow": row["workflow"], "change": change}


def run_learning(db_path=None) -> dict:
    """The weekly job: recommendations, then reverts. ops.run_job result shape."""
    out = {"attempted": 2, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    try:
        out["recommendations"] = len(recommend(db_path=db_path))
        out["ok"] += 1
    except Exception as e:
        log.warning("route recommendations failed: %s", e)
        out["failed"] += 1
    try:
        out["reverted"] = revert_regressions(db_path=db_path)
        out["ok"] += 1
    except Exception as e:
        log.warning("route reverts failed: %s", e)
        out["failed"] += 1
    return out


# ── shadow arms: a cheaper tier replayed on kept production requests ────────
#
# The evidence `start_lower` needs (above). Weekly (job "ai_shadow_arms", on
# the AI lane), for each workflow whose policy starts on T2 or higher and
# that a rubric can judge, the next cheaper tier is replayed on a sample of
# the requests production runs kept (ai_orchestrator.keep_request, ~5% of
# runs) — through Message Batches, at half price, never on anyone's request
# path and never shown to anyone. When an answer lands (ai_batch_collect),
# its run is recorded (ai_runs.shadow_of = the production run) and both texts
# — production's, from its ai_calls trace, and the candidate's, cut and
# redacted the same way — pass the same deterministic check and are scored by
# the same Haiku rubric with the same context, on one background thread
# (score_pair; re-audit 10/7/26 #2). Only first-rung requests of runs that
# never escalated are replayed, and production's cost is its first call's. A
# production text the trace no longer keeps (AI_TRACE_KEEP_PER_ACTION keeps
# the newest ten per restaurant and action) is checked for before anything is
# sent: no comparison, no replay, nothing paid.
#
# Bounded: SHADOW_SAMPLE replays per workflow a week, and the whole week's
# shadow spend (replays and their scoring: every ledger row under a
# "shadow:" correlation id) inside AI_SHADOW_WEEKLY_USD.
SHADOW_WEEKLY_USD = float(os.getenv("AI_SHADOW_WEEKLY_USD", "2"))
SHADOW_SAMPLE = 20
SHADOW_BATCH_WORKFLOW = "shadow_arms"      # ai_batches' workflow (AI_BATCHES_WORKFLOWS)
SHADOW_ACTION = "shadow_arms"              # the ledger action: platform spend, never a restaurant's
# A replay's scoring: two Haiku rubric calls, ~1.5k tokens in and 100 out each.
SHADOW_REVIEW_ESTIMATE_USD = 0.004

# The rubric (ai_reviewer.RUBRICS) each replayable workflow is judged by.
# Left out on purpose: review_analysis and the menu extractions (labels and
# fields — right or wrong against a source a rubric cannot see),
# staff_translation (fidelity, not tone), email_personalization, staff_brief,
# task_sheet_starter, the recipe drafts and the content calendar (no rubric
# judges them yet), and everything in ai_orchestrator.NOT_REPLAYABLE.
SHADOW_RUBRICS = {
    "draft_response": "review_reply",
    "marketing_content": "marketing_post",
    "guest_campaign_draft": "guest_text",
    "guest_newsletter_draft": "guest_email",
    "staff_answer": "staff_answer",
    "dsr_narrative": "dsr",
    "labor_insight": "insight_read",
    "inventory_insight": "insight_read",
    "review_insight": "insight_read",
    "marketing_insight": "insight_read",
    # review_diagnosis and food_cost_diagnosis left out (re-audit 10/7/26
    # #6): they call create_with_retry directly, outside generate(), so they
    # keep no request to replay and an override would never reach them
    # (ai_workflows.CONSOLE_READ_ONLY).
    "weekly_digest": "insight_read",
    "competitor_insight": "insight_read",
}

# The deterministic check both texts of a pair pass before either is scored
# (re-audit 10/7/26 #2): the Response Validation Layer on the workflow's own
# surface, with the kept request's text as the context the figures must come
# from. Production passed its own, richer check when it ran; a candidate
# never ran one, so a replay that invents a figure, a name or an offer used
# to be scored on tone alone. A candidate the check refuses scores 0; a pair
# whose production text the check refuses is not counted (the check is
# blunter than production's own, and the pair would say nothing).
SHADOW_SURFACES = {
    "draft_response": "reply_public",
    "marketing_content": "social_post",
    "guest_campaign_draft": "guest_sms",
    "guest_newsletter_draft": "guest_sms",
    "staff_answer": "staff_answer",
    "dsr_narrative": "dsr",
    "labor_insight": "labor_insight",
    "inventory_insight": "food_insight",
    "review_insight": "review_insight",
    "marketing_insight": "marketing_insight",
    "weekly_digest": "digest",
    "competitor_insight": "intel",
}

# The next cheaper tier than a ladder's first rung. "default" (the call
# site's own model) and T1 have none.
_CHEAPER = {"T2": "T1", "T3": "T2", "T4": "T3"}


def candidate_tier(workflow, db_path=None):
    """The tier a shadow arm tries for `workflow`: one below where its
    policy in force starts, or None."""
    pol = wf.policy(workflow, db_path)
    return _CHEAPER.get(pol.ladder[0]) if pol.ladder else None


def shadow_workflows(db_path=None) -> list:
    """[(workflow, candidate tier)] the weekly job replays."""
    out = []
    for wfl in SHADOW_RUBRICS:
        if wfl not in wf.POLICIES or wfl in orch.NOT_REPLAYABLE:
            continue
        cand = candidate_tier(wfl, db_path)
        if cand:
            out.append((wfl, cand))
    return out


def shadow_spent(days=7, db_path=None) -> float:
    """Ledger dollars under a "shadow:" correlation id in the last `days`:
    the replays and the rubric calls that scored them. A range on the
    correlation index, not a LIKE."""
    conn = _conn(db_path)
    try:
        r = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM ai_usage WHERE correlation_id >= 'shadow:' "
                         "AND correlation_id < 'shadow;' AND created_at >= datetime('now', ?)",
                         (f"-{int(days)} days",)).fetchone()
        return float(r[0] or 0)
    except Exception:
        return 0.0
    finally:
        conn.close()


def _custom_id(run_id, tier):
    return f"sa-{tier}-" + hashlib.sha1(str(run_id).encode("utf-8")).hexdigest()[:24]


def _production_call(conn, run_id, workflow):
    """(call_id, price_version) of the production run's last good answer —
    the text it served (a gate's reviewer calls run under the same id with
    their own action) — or (None, None)."""
    row = conn.execute("SELECT call_id, price_version FROM ai_usage WHERE correlation_id=? AND action=? "
                       "AND outcome='ok' AND call_id IS NOT NULL ORDER BY id DESC LIMIT 1",
                       (run_id, workflow)).fetchone()
    return (row["call_id"], row["price_version"]) if row else (None, None)


def _first_call_cost(conn, run_id, workflow):
    """The ledger cost of the production run's FIRST call of the workflow
    (its first rung), or None — what a candidate on a cheaper first rung
    would replace. The run's whole cost carries its reviewer's calls and any
    escalation, which a one-call replay never pays (re-audit 10/7/26 #2)."""
    row = conn.execute("SELECT cost_usd FROM ai_usage WHERE correlation_id=? AND action=? ORDER BY id ASC LIMIT 1",
                       (run_id, workflow)).fetchone()
    return float(row["cost_usd"] or 0) if row and row["cost_usd"] is not None else None


def _as_traced(workflow, text, request=None):
    """`text` as the ai_calls trace keeps a production output: cut at the
    workflow's output cap and redacted with the names the request carries —
    so the candidate and production's traced text are read in the same form
    (re-audit 10/7/26 #2: the candidate was scored raw)."""
    import ai_utils
    try:
        names = ai_utils.guest_names_in(ai_utils._prompt_text(request or {}))
    except Exception:
        names = set()
    cap = ai_utils._trace_caps(workflow)[1]
    return ai_utils.redact_pii(str(text or "")[:cap], names)


def shadow_check(workflow, text, restaurant_id=None, context=""):
    """The deterministic check of one text of a pair (SHADOW_SURFACES): an
    ai_orchestrator.Verdict — failed when the validation engine refuses or
    withholds it. A workflow with no surface, or a check that cannot run,
    passes (it decides nothing either way)."""
    surface = SHADOW_SURFACES.get(workflow)
    if not surface:
        return orch.Verdict.passed(label="unchecked")
    try:
        import response_validation as rv
        v = rv.validate(str(text or ""), rv.ValidationContext(restaurant_id=restaurant_id, surface=surface,
                                                              context_text=str(context or "")))
    except Exception as e:
        log.info("shadow check did not run (%s): %s", workflow, e)
        return orch.Verdict.passed(label="unchecked")
    return orch.verdict_from_validation(v)


def _call_output(call_id):
    """A traced call's output text while ai_calls still keeps it, else None."""
    if not call_id:
        return None
    try:
        import ai_utils
        call = ai_utils.read_call(call_id) or {}
    except Exception:
        return None
    text = call.get("output")
    return text if text and str(text).strip() else None


def _request_context(request, limit=3000):
    """What the writer was given — the kept request's user turns — as the
    rubric's context: the same for both texts of a pair."""
    parts = []
    for m in (request or {}).get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts.extend(str(b.get("text") or "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return "\n\n".join(p for p in parts if p)[-limit:]


def shadow_arms(workflow, candidate_tier, sample=SHADOW_SAMPLE, budget_usd=None, db_path=None) -> dict:
    """Replay `candidate_tier` on up to `sample` kept production requests of
    `workflow` through Message Batches (the note above). Skips a request
    already replayed on that tier, a production run that did not pass or
    already ran on that tier, and one whose production text is no longer
    kept. `budget_usd` bounds this call's estimated spend (production's cost
    at the batch rate — a cheaper tier costs less — plus the scoring).
    Returns the job counts plus `submitted` and `cost_estimate`."""
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False,
           "submitted": 0, "cost_estimate": 0.0, "workflow": workflow, "tier": candidate_tier}
    if workflow not in SHADOW_RUBRICS or workflow in orch.NOT_REPLAYABLE or workflow not in wf.POLICIES:
        raise ValueError(f"{workflow} is not a shadow-replayable workflow")
    if candidate_tier not in set(_CHEAPER.values()):
        raise ValueError(f"the candidate tier must be one of {sorted(set(_CHEAPER.values()))}")
    import ai_batches
    import ai_utils
    import data_health
    if not ai_batches.enabled(SHADOW_BATCH_WORKFLOW):
        out["reason"] = "Message Batches are off here (not the production scheduler, or shadow_arms not listed)"
        return out
    t = wf._tier_table()[candidate_tier]
    route = wf.Route(tier=candidate_tier, model=t["model"], effort=t["effort"])
    # Only requests kept on the FIRST rung of the ladder in force, from runs
    # that started there and never escalated: the served text is then that
    # rung's own, and the replay is compared with exactly what it would
    # replace (re-audit 10/7/26 #2, #6).
    first = wf.policy(workflow, db_path).ladder[0]
    items, est_total = [], 0.0
    conn = _conn(db_path)
    try:
        for run_id, rid, request in orch.kept_requests(workflow, limit=max(1, int(sample)) * 3, db_path=db_path,
                                                       tier=first):
            if len(items) >= int(sample):
                break
            if conn.execute("SELECT 1 FROM ai_runs WHERE shadow_of=? AND final_tier=?",
                            (run_id, candidate_tier)).fetchone():
                out["skipped"] += 1
                continue
            prod = conn.execute("SELECT status, cost_usd, final_tier, escalations, start_tier FROM ai_runs "
                                "WHERE run_id=? AND shadow_of IS NULL", (run_id,)).fetchone()
            if (not prod or prod["status"] != "ok" or prod["final_tier"] == candidate_tier
                    or (prod["escalations"] or 0) > 0 or prod["start_tier"]):
                out["skipped"] += 1
                continue
            call_id, price_version = _production_call(conn, run_id, workflow)
            first_cost = _first_call_cost(conn, run_id, workflow)
            if not _call_output(call_id) or first_cost is None:
                out["skipped"] += 1
                continue
            est = first_cost * ai_utils.BATCH_PRICE_MULTIPLIER + 2 * SHADOW_REVIEW_ESTIMATE_USD
            if budget_usd is not None and est_total + est > float(budget_usd):
                out["hit_bound"] = True
                break
            est_total += est
            items.append({
                "custom_id": _custom_id(run_id, candidate_tier), "restaurant_id": None, "action": SHADOW_ACTION,
                "request": route.apply(request), "callback": "ai_learning:shadow_landed",
                "readiness": data_health.NOT_APPLICABLE,
                "context": {"workflow": workflow, "run_id": run_id, "tier": candidate_tier, "rid": rid,
                            "production_call_id": call_id, "production_first_cost": round(first_cost, 6),
                            "production_batched": price_version == ai_utils.BATCH_PRICE_VERSION}})
    finally:
        conn.close()
    out["cost_estimate"] = round(est_total, 6)
    if not items:
        return out
    from datetime import date
    with ai_utils.ai_context(correlation_id=f"shadow:{workflow[:24]}:{date.today().isoformat()}"):
        res = ai_batches.submit(SHADOW_BATCH_WORKFLOW, items)
    for state in res.values():
        out["attempted"] += 1
        if state == ai_batches.SUBMITTED:
            out["ok"] += 1
            out["submitted"] += 1
        elif state in (ai_batches.DUPLICATE, ai_batches.DISABLED):
            out["skipped"] += 1
        else:
            out["failed"] += 1
    return out


def _write_shadow_run(ctx, *, status, model=None, cost=None, tin=None, tout=None, latency=None, note=None,
                      detail=None, db_path=None):
    """The candidate's ai_runs row: shadow_of = the production run."""
    workflow = ctx.get("workflow") or ""
    pol = wf.POLICIES.get(workflow)
    run_id = orch.new_run_id(workflow)
    conn = _conn(db_path)
    try:
        prod = conn.execute("SELECT subject, restaurant_id FROM ai_runs WHERE run_id=?",
                            (ctx.get("run_id"),)).fetchone()
        conn.execute(
            "INSERT INTO ai_runs (run_id, restaurant_id, workflow, agent, policy_version, overridden, subject, "
            "\"trigger\", unattended, start_tier, final_tier, final_model, attempts, escalations, steps_json, "
            "status, verdict, reasons, latency_ms, cost_usd, input_tokens, output_tokens, context_json, "
            "finished_at, shadow_of) "
            "VALUES (?,?,?,?,?,0,?,'shadow',1,NULL,?,?,1,0,?,?,?,?,?,?,?,?,?,datetime('now'),?)",
            (run_id, prod["restaurant_id"] if prod else ctx.get("rid"), workflow, pol.agent if pol else None,
             wf.POLICY_VERSION, prod["subject"] if prod else None, ctx.get("tier"), model,
             json.dumps([{"tier": ctx.get("tier"), "model": model, "shadow": True}]), status,
             "pass" if status == "ok" else status, json.dumps([note]) if note else None, latency,
             round(float(cost), 6) if cost is not None else None, tin, tout,
             json.dumps(detail)[:2000] if detail else None, ctx.get("run_id")))
        conn.commit()
    finally:
        conn.close()
    return run_id


_SCORE_POOL = None
_SCORE_LOCK = threading.Lock()


def _score_async(fn):
    """Run a pair's scoring on one background thread — two Haiku calls a
    replay, never on the collector's loop thread — under the attribution and
    "shadow:" correlation of the callback that queued it. A restart before
    it runs leaves the pair unscored, and an unscored pair never counts."""
    global _SCORE_POOL
    import ai_utils
    with _SCORE_LOCK:
        if _SCORE_POOL is None:
            from concurrent.futures import ThreadPoolExecutor
            _SCORE_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-shadow-arms")
    _SCORE_POOL.submit(ai_utils.context_runner(ai_utils.attributed(fn)))


def _note_shadow(shadow_run_id, note, db_path=None):
    conn = _conn(db_path)
    try:
        conn.execute("UPDATE ai_runs SET reviewer_notes=? WHERE run_id=?", (note, shadow_run_id))
        conn.commit()
    finally:
        conn.close()


def score_pair(workflow, shadow_run_id, candidate_text, production_run_id, production_call_id, db_path=None):
    """Judge both texts of a pair the same way (re-audit 10/7/26 #2):

      * the same form — production's text as the ai_calls trace keeps it,
        the candidate's cut and redacted the same way (_as_traced);
      * the same deterministic check first (shadow_check): a candidate it
        refuses scores 0 without a rubric call; a production text it
        refuses leaves the pair uncounted;
      * the same rubric with the same context — production is always
        re-scored here, never credited with a gate's score given another
        context.

    Both scores are stored on the SHADOW run (production's in context_json
    production_score); the production run's own row is left as it ran.
    Returns (production score, candidate score); a side the rubric could not
    score is None, and that pair does not count."""
    import ai_reviewer
    kind = SHADOW_RUBRICS.get(workflow)
    prod_text = _call_output(production_call_id)
    if not kind or not prod_text:
        _note_shadow(shadow_run_id, "the production text is no longer kept" if kind else "no rubric for this workflow",
                     db_path)
        return None, None
    request = orch.kept_request(production_run_id, db_path=db_path)
    context = _request_context(request)
    cand_text = _as_traced(workflow, candidate_text, request)
    rid = None
    try:
        conn = _conn(db_path)
        try:
            row = conn.execute("SELECT restaurant_id FROM ai_runs WHERE run_id=?", (production_run_id,)).fetchone()
            rid = row["restaurant_id"] if row else None
        finally:
            conn.close()
    except Exception:
        rid = None
    pv = shadow_check(workflow, prod_text, rid, context)
    if not pv.ok:
        _note_shadow(shadow_run_id, ("production's text fails the replay check, so the pair is not counted: "
                                     + "; ".join(pv.reasons))[:500], db_path)
        return None, None
    cv = shadow_check(workflow, cand_text, rid, context)
    vp = ai_reviewer.review_text(kind, prod_text, restaurant_id=None, context=context, mode="haiku_shadow")
    ps = getattr(vp, "score", None)
    if cv.ok:
        vc = ai_reviewer.review_text(kind, cand_text, restaurant_id=None, context=context, mode="haiku_shadow")
        cs, notes = getattr(vc, "score", None), getattr(vc, "reasons", None) or []
    else:
        cs, notes = 0.0, ["refused by the check: " + ("; ".join(cv.reasons) or cv.label or "refuse")]
    conn = _conn(db_path)
    try:
        conn.execute("UPDATE ai_runs SET reviewer='haiku_shadow', reviewer_score=?, reviewer_notes=? WHERE run_id=?",
                     (cs if ps is not None else None, ("; ".join(notes))[:500] or None, shadow_run_id))
        conn.commit()
    finally:
        conn.close()
    if ps is not None:
        orch.attach(shadow_run_id, context={"production_score": ps, "candidate_check": cv.label or "pass"},
                    db_path=db_path)
    return ps, cs


def shadow_landed(item, message=None, error=None):
    """ai_batches callback for one shadow replay: record the candidate's run
    and queue the pair's scoring. Its cost is the batch ledger row's, put on
    production's basis — a replay of a synchronous run is compared at what
    the candidate would cost synchronously (the batch cost ÷ the batch
    multiplier), a replay of a batched run (the DSR narrative) at the batch
    cost; both figures stay in context_json. A replay that failed is
    recorded too, so it is not sent again."""
    import ai_utils
    ctx = dict(item.get("context") or {})
    if error is not None:
        _write_shadow_run(ctx, status="error", model=item.get("model"),
                          note=f"{type(error).__name__}: {error}"[:200])
        return
    text = ai_utils.extract_text(message) or ""
    outcome = ai_utils.outcome_of(message)
    cost = tin = tout = latency = batch_cost = None
    if item.get("call_id"):
        conn = _conn()
        try:
            row = conn.execute("SELECT cost_usd, input_tokens, output_tokens, latency_ms FROM ai_usage "
                               "WHERE call_id=? ORDER BY id DESC LIMIT 1", (item.get("call_id"),)).fetchone()
        finally:
            conn.close()
        if row:
            batch_cost = float(row["cost_usd"] or 0)
            tin, tout, latency = row["input_tokens"], row["output_tokens"], row["latency_ms"]
            cost = batch_cost if ctx.get("production_batched") else batch_cost / (ai_utils.BATCH_PRICE_MULTIPLIER or 1.0)
    ok = outcome == "ok" and bool(text.strip())
    run_id = _write_shadow_run(
        ctx, status="ok" if ok else "failed", model=item.get("model"), cost=cost, tin=tin, tout=tout,
        latency=latency, note=None if ok else f"the replay came back {outcome}",
        detail={"batch_cost_usd": batch_cost, "priced_as": "batch" if ctx.get("production_batched") else "list",
                "call_id": item.get("call_id"), "production_first_cost": ctx.get("production_first_cost")})
    if not ok:
        return
    workflow, prod_run, prod_call = ctx.get("workflow"), ctx.get("run_id"), ctx.get("production_call_id")
    _score_async(lambda: score_pair(workflow, run_id, text, prod_run, prod_call))


def run_shadow_arms(db_path=None) -> dict:
    """The weekly job (ai_shadow_arms): each workflow shadow_workflows()
    names on its next cheaper tier, SHADOW_SAMPLE replays at most, the
    week's shadow spend inside SHADOW_WEEKLY_USD. ops.run_job's result
    shape; attempted = workflows tried."""
    import ai_batches
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False, "submitted": 0,
           "budget_usd": SHADOW_WEEKLY_USD}
    pairs = shadow_workflows(db_path)
    if not ai_batches.enabled(SHADOW_BATCH_WORKFLOW):
        out["skipped"] = len(pairs)
        out["reason"] = "Message Batches are off here"
        return out
    spent = shadow_spent(7, db_path)
    remaining = SHADOW_WEEKLY_USD - spent
    out["spent_7d"] = round(spent, 4)
    for wfl, cand in pairs:
        if remaining <= 0:
            out["hit_bound"] = True
            out["skipped"] += 1
            continue
        out["attempted"] += 1
        try:
            r = shadow_arms(wfl, cand, sample=SHADOW_SAMPLE, budget_usd=remaining, db_path=db_path)
        except Exception as e:
            log.warning("shadow arms for %s failed: %s", wfl, e)
            out["failed"] += 1
            continue
        remaining -= float(r.get("cost_estimate") or 0)
        out["submitted"] += int(r.get("submitted") or 0)
        out["hit_bound"] = out["hit_bound"] or bool(r.get("hit_bound"))
        if r.get("failed") and not r.get("ok"):
            out["failed"] += 1
        else:
            out["ok"] += 1
    return out
