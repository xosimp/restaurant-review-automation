#!/usr/bin/env python3
"""Replay saved schedules through today's Shift Quality code.

Every change to the scorer or the optimizer is judged here first, against
real weeks, before anyone reads a number it produced. For each saved week
this prints the score it was stored with, the score today's code gives the
same rows, and (with --optimize) what the repair loop makes of it.

    python3 scripts/schedule_eval.py --restaurant 2
    python3 scripts/schedule_eval.py --ids 6953,6952 --optimize --detail
    python3 scripts/schedule_eval.py --restaurant 2 --solve      # the assignment solver (#47)

Reads the local database only. No model call is made and nothing is saved.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _stored_targets(quality: dict) -> dict:
    """The per-day hour targets the week was generated against, recovered
    from the stored evaluation (they belong to that generation and are not
    re-derivable from the database)."""
    out = {}
    for s in (quality or {}).get("shifts") or []:
        for d in s.get("dimensions") or []:
            if d.get("key") == "labor_efficiency":
                t = (d.get("facts") or {}).get("target_hours")
                if t and s.get("date"):
                    out[s["date"]] = float(t)
    return out


def _dims(q: dict) -> dict:
    return {d["key"]: d["score"] for d in (q or {}).get("dimensions") or []}


def evaluate(history_id: int, optimize: bool = False, solve: bool = False, seconds: float = None) -> dict:
    from models import get_conn
    import schedule_versions as sv
    from schedule_engine import quality_inputs_from_db, _quality_signals
    import shift_quality as sq

    conn = get_conn()
    try:
        row = conn.execute("SELECT id, restaurant_id, week_start, schedule_csv, quality_json FROM schedule_history "
                           "WHERE id=?", (history_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"id": history_id, "error": "not found"}
    stored = json.loads(row["quality_json"] or "null") or {}
    rows = sv.rows_from_csv(row["schedule_csv"])
    inputs = quality_inputs_from_db(row["restaurant_id"], daily_target_hours=_stored_targets(stored), week_rows=rows)
    signals, weights = _quality_signals(row["restaurant_id"], inputs)
    now = sq.score_rows(rows, profiles=inputs.get("shift_profiles") or None, weights=weights, **signals)
    out = {"id": row["id"], "restaurant_id": row["restaurant_id"], "week_start": row["week_start"],
           "rows": len(rows), "stored": stored.get("score"), "now": now.get("score"),
           "dims_stored": _dims(stored), "dims_now": _dims(now),
           "capped": [(s["day"][:3], s["daypart"][:1], s["score"], s["capped_by"])
                      for s in now.get("shifts") or [] if s.get("capped_by")],
           "confidence": (now.get("confidence") or {}).get("score")}
    if optimize:
        try:
            import schedule_optimizer as so
        except ImportError:
            out["optimized"] = None
        else:
            from models import get_restaurant
            r = get_restaurant(row["restaurant_id"])
            budget = None
            if r is not None and int(getattr(r, "trim_to_budget", 1) or 0):
                conn = get_conn()
                try:
                    h = conn.execute("SELECT hours_budget FROM schedule_history WHERE id=?", (row["id"],)).fetchone()
                finally:
                    conn.close()
                budget = float(h["hours_budget"] or 0) or None if h else None
            # The same ceilings the generation job holds the search to.
            res = so.optimize(rows, inputs, signals=signals, weights=weights,
                              constraints=inputs.get("constraints"), hours_budget=budget,
                              max_server_overlap=getattr(r, "section_count", None))
            out["optimized"] = res.get("after_score")
            out["changes"] = [c.get("reason") for c in res.get("changes") or []]
            out["seconds"] = res.get("seconds")
            out["dims_opt"] = _dims(res.get("quality"))
    if solve:
        import schedule_solver as ssv
        # The same call generation makes (schedule_engine._run_schedule_job),
        # on the saved week's rows: who works each shift, re-solved and judged.
        res = ssv.improve(rows, inputs, signals=signals, weights=weights, constraints=inputs.get("constraints"),
                          max_seconds=seconds or ssv.DEFAULT_SECONDS)
        st = res.get("stats") or {}
        out["solved"] = {"before": res.get("before_score"), "after": st.get("after_score", res.get("after_score")),
                         "applied": res.get("applied"), "status": st.get("status"),
                         "proved": st.get("proved_optimal"), "seconds": st.get("seconds_total"),
                         "search_seconds": st.get("seconds"), "slots": st.get("slots"), "people": st.get("people"),
                         "components": st.get("components"), "components_proved": st.get("components_proved"),
                         "nodes": st.get("nodes"), "infeasible": len(st.get("infeasible") or []),
                         "changes": [c.get("reason") for c in res.get("changes") or []]}
        out["dims_solved"] = _dims(res.get("quality"))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--restaurant", type=int, action="append", help="every saved week for this restaurant")
    ap.add_argument("--ids", help="comma-separated schedule_history ids")
    ap.add_argument("--limit", type=int, default=12, help="most recent weeks per restaurant")
    ap.add_argument("--optimize", action="store_true", help="also run the repair loop")
    ap.add_argument("--detail", action="store_true", help="per-dimension scores")
    ap.add_argument("--solve", action="store_true", help="also run the assignment solver (schedule_solver)")
    ap.add_argument("--seconds", type=float, default=None, help="solver time budget per week")
    args = ap.parse_args(argv)

    from models import get_conn
    ids = [int(x) for x in (args.ids or "").split(",") if x.strip()]
    if args.restaurant:
        conn = get_conn()
        try:
            for rid in args.restaurant:
                ids += [r["id"] for r in conn.execute(
                    "SELECT id FROM schedule_history WHERE restaurant_id=? AND quality_json IS NOT NULL "
                    "ORDER BY id DESC LIMIT ?", (rid, args.limit)).fetchall()]
        finally:
            conn.close()
    if not ids:
        ap.error("give --restaurant or --ids")

    results = [evaluate(i, optimize=args.optimize, solve=args.solve, seconds=args.seconds) for i in sorted(set(ids))]
    for r in results:
        if r.get("error"):
            print(f"{r['id']}: {r['error']}")
            continue
        line = f"{r['id']} r{r['restaurant_id']} {r['week_start']}: stored {r['stored']} → now {r['now']}"
        if args.optimize:
            line += f" → optimized {r.get('optimized')} ({len(r.get('changes') or [])} changes, {r.get('seconds')}s)"
        print(line + f"  conf {r['confidence']}  capped {r['capped']}")
        if args.solve and r.get("solved"):
            v = r["solved"]
            print(f"    solver: {v['before']} → {v['after']} ({'kept' if v['applied'] else 'draft kept'}), {v['status']}"
                  f"{' (proved optimal)' if v['proved'] else ''}, {v['slots']} slots / {v['people']} people, "
                  f"{v['components_proved']}/{v['components']} parts proved, {v['nodes']} nodes, "
                  f"{v['search_seconds']}s search / {v['seconds']}s total, {v['infeasible']} set aside")
        if args.detail:
            keys = sorted(set(r["dims_stored"]) | set(r["dims_now"]) | set(r.get("dims_opt") or {}))
            for k in keys:
                print(f"    {k:22s} {str(r['dims_stored'].get(k, '-')):>4} → {str(r['dims_now'].get(k, '-')):>4}"
                      + (f" → {str((r.get('dims_opt') or {}).get(k, '-')):>4}" if args.optimize else ""))
            for c in (r.get("changes") or [])[:12]:
                print("      · " + c)
            for c in ((r.get("solved") or {}).get("changes") or [])[:12]:
                print("      ◦ " + c)
    scored = [r for r in results if r.get("now") is not None]
    if scored:
        mean = lambda k: round(sum(r[k] for r in scored if r.get(k) is not None) /
                               max(1, len([r for r in scored if r.get(k) is not None])), 1)
        print(f"\nmean stored {mean('stored')}  now {mean('now')}" +
              (f"  optimized {mean('optimized')}" if args.optimize else ""))


if __name__ == "__main__":
    main()
