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
import json
import logging
import statistics

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
    """[{workflow, tier, model, runs, cost_per_run, p50_ms, p95_ms,
    escalation_rate, pass_rate, acceptance, outcomes, quality, shadow_score}]
    over production runs (never shadow replays)."""
    conn = _conn(db_path)
    try:
        sql = ("SELECT workflow, final_tier, final_model, status, escalations, latency_ms, cost_usd, outcome, "
               "outcome_quality, reviewer, reviewer_score, start_tier FROM ai_runs WHERE shadow_of IS NULL "
               "AND created_at >= datetime('now', ?)")
        args = [f"-{int(days)} days"]
        if workflow:
            sql += " AND workflow=?"
            args.append(workflow)
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
    groups = {}
    for r in rows:
        groups.setdefault((r["workflow"], r["final_tier"], r["final_model"]), []).append(r)
    out = []
    for (wfl, tier, model), rs in sorted(groups.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        acc, n_out = _accept(rs)
        q = [r["outcome_quality"] for r in rs if r["outcome_quality"] is not None]
        shadow = [r["reviewer_score"] for r in rs if r["reviewer_score"] is not None]
        out.append({
            "workflow": wfl, "tier": tier, "model": model, "runs": len(rs),
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
    """{workflow: (runs started on the first rung, of which escalated)}"""
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT workflow, COUNT(*) n, SUM(CASE WHEN escalations>0 THEN 1 ELSE 0 END) e "
                            "FROM ai_runs WHERE shadow_of IS NULL AND created_at >= datetime('now', ?) "
                            "AND (start_tier IS NULL OR start_tier='') GROUP BY workflow",
                            (f"-{int(days)} days",)).fetchall()
    finally:
        conn.close()
    return {r["workflow"]: (int(r["n"] or 0), int(r["e"] or 0)) for r in rows}


def _shadow_pairs(days, db_path):
    """{(workflow, candidate_tier): [(production_score, candidate_score, prod_cost, cand_cost)]}"""
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT s.workflow, s.final_tier tier, s.reviewer_score cs, s.cost_usd cc, p.reviewer_score ps, "
            "p.cost_usd pc FROM ai_runs s JOIN ai_runs p ON p.run_id = s.shadow_of "
            "WHERE s.shadow_of IS NOT NULL AND s.created_at >= datetime('now', ?) "
            "AND s.reviewer_score IS NOT NULL AND p.reviewer_score IS NOT NULL", (f"-{int(days)} days",)).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        out.setdefault((r["workflow"], r["tier"]), []).append((r["ps"], r["cs"], r["pc"] or 0, r["cc"] or 0))
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
    stats = route_stats(days, db_path=db_path)
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
        for s in stats:
            if s["acceptance"] is not None and s["outcomes"] >= MIN_OUTCOMES and s["acceptance"] < ACCEPT_LOW:
                summary = (f"{s['workflow']} on {s['tier']} ({s['model']}): owners used "
                           f"{round(s['acceptance'] * 100)}% of {s['outcomes']} outputs — look at the prompt or a "
                           f"stronger route")
                _recommend(conn, s["workflow"], "quality_low", summary, {}, s)
                made.append(summary)
        for (wfl, tier), ps in pairs.items():
            if len(ps) < MIN_RUNS:
                continue
            prod = statistics.mean(p for p, _c, _pc, _cc in ps)
            cand = statistics.mean(c for _p, c, _pc, _cc in ps)
            pc = statistics.mean(x for _p, _c, x, _cc in ps)
            cc = statistics.mean(x for _p, _c, _pc, x in ps)
            pol = wf.policy(wfl, db_path)
            if cand >= prod - SHADOW_TOLERANCE and cc < pc:
                ladder = [tier] + [t for t in pol.ladder if t != tier]
                summary = (f"{wfl}: {tier} scored {cand:.2f} against production's {prod:.2f} on {len(ps)} replays, "
                           f"at {cc / pc * 100 if pc else 0:.0f}% of the cost — start on {tier}")
                _recommend(conn, wfl, "start_lower", summary, {"ladder": ladder},
                           {"pairs": len(ps), "prod_score": round(prod, 3), "cand_score": round(cand, 3),
                            "prod_cost": round(pc, 5), "cand_cost": round(cc, 5)})
                made.append(summary)
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
