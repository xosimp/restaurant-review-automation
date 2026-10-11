"""Read-only: what the scheduler audit's G2 round changes for one restaurant.

Prints, for a restaurant (scheduler audit 10/10/26, workstream G2):

  * the usual crew per weekday, meal and role at its first published Cavnar
    week (go-live) and now, and each slot's provenance — a drop since
    go-live that the baseline holds back for want of outcome evidence
    (LEARNING-5: "compare typical_headcount at go-live vs now per slot");
  * the labor standards at the rush (staffing_model): per role family and
    meal, the work one person carries, its lead or lag, the fixed crew;
  * next week's work-driven numbers beside the usual crew (RM-17);
  * the backtested forecast's choice (RM-19) and the stored one-more-person
    estimates (RM-20).

Writes nothing. Never sends anything.

    python scripts/staffing_requirements_report.py --restaurant 5
    railway ssh "/opt/venv/bin/python scripts/staffing_requirements_report.py --restaurant 5"
"""
import argparse
import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--restaurant", type=int, required=True)
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    a = ap.parse_args(argv)
    rid = a.restaurant
    import labor
    import requirement_provenance as prov
    import staffing_model as sm
    import demand_model as dm
    import staffing_elasticity as se
    import schedule_requirements as req
    out = {"restaurant_id": rid}
    live = prov.go_live(rid)
    out["go_live"] = live
    now = labor.staffing_baseline(rid)
    out["now"] = {f"{d} {p}": v for (d, p), v in (now.get("typical_headcount") or {}).items()}
    out["provenance"] = {f"{d} {p}": v for (d, p), v in (now.get("headcount_provenance") or {}).items()}
    out["held_trends"] = [t for t in now.get("headcount_trends") or [] if t.get("held")]
    if live:
        then = labor.staffing_baseline(rid, before=live)
        out["at_go_live"] = {f"{d} {p}": v for (d, p), v in (then.get("typical_headcount") or {}).items()}
    model = sm.measure(rid)
    out["standards"] = {f: {"lag": e.get("lag"), "parts": {p: {k: pe.get(k) for k in ("std", "own", "n", "prior",
                                                                                         "source", "fixed")}
                                                           for p, pe in (e.get("parts") or {}).items()}}
                        for f, e in (model.get("families") or {}).items()}
    # The week a Generate press drafts (the restaurant's own week start and
    # today), with the owner's floors — the requirement the draft is held to.
    import schedule_engine as _engine
    import schedule_rules as sr
    from models import get_restaurant
    first = date.fromisoformat(_engine.generation_request(rid)["week_start"])
    week = [(first + timedelta(days=k)).isoformat() for k in range(7)]
    rows = req.shift_requirements(week, typical_headcount=now.get("typical_headcount") or {}, staffing_model=model,
                                  concurrent=now.get("concurrent") or {},
                                  role_floors=sr.role_floors(get_restaurant(rid)))
    out["next_week"] = [{"date": r["date"], "daypart": r["daypart"], "driven": r.get("driven") or [],
                         "roles": {x["role"]: {"required": x["required"], "typical": x["typical"]}
                                   for x in r["roles"]}} for r in rows]
    fc = dm.forecast_dates(rid, week)
    out["forecast"] = {"choice": fc.get("choice"), "ratios": {d: e.get("ratio") for d, e in
                                                              (fc.get("dates") or {}).items()}}
    out["elasticity"] = {f"{f} {p}": e for (f, p), e in se.stored(rid).items()}
    if a.json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    print(f"Restaurant {rid} — first published Cavnar week: {live or 'none yet'}")
    for k in sorted(out["now"]):
        was = (out.get("at_go_live") or {}).get(k) or {}
        print(f"  {k}: now {out['now'][k]}" + (f"; at go-live {was}" if live else "")
              + (f"; provenance {out['provenance'].get(k)}" if out["provenance"].get(k) else ""))
    for t in out["held_trends"]:
        print(f"  HELD: {t['day']} {t['part']} {t['role']}: {t.get('reason')}")
    print("Labor standards at the rush:")
    for f, e in sorted(out["standards"].items()):
        print(f"  {f} (lag {e['lag']}): {json.dumps(e['parts'], default=str)}")
    print("Next week (required vs usual; driven = sized from the work):")
    for r in out["next_week"]:
        print(f"  {r['date']} {r['daypart']}: {r['roles']}" + (f" driven {r['driven']}" if r["driven"] else ""))
    print(f"Forecast: {json.dumps(out['forecast'], default=str)}")
    print(f"One more person (stored): {json.dumps(out['elasticity'], default=str)[:2000]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
