"""
studio_bench.py — the Schedule Studio's Bench stage: one body for the
capabilities the 10/10/26 scheduler audit invented (MARKET-17, -21, -22,
-26..-30, -32), owner-facing, behind each one's own sample floor.

`api_body()` is the one body of GET/POST /api/labor/bench and its phone twin
/mobile/api/labor/bench (client_api, mobile_api). GET ?part=:

  what_if     ?weekday=Saturday&role=server&change=-1 — service_sim.what_if
  standby     ?history_id= — standby_insurance.owner_view for the week
  ladder      ?history_id= — commitment_ladder.owner_view
  events      event_reactive.proposals (open first)
  strength    bench_strength.owner_view
  tips        tip_opportunity.owner_view                      (owner only)
  retention   retention_risk.assess                           (owner only)
  org         org_bench.owner_view; with ?date=&role=&start=&end= the
              bench's candidates for that gap
  outcomes    outcome_benchmark.restaurant_view

POST {action}: standby_offer, standby_withdraw, standby_policy,
event_approve, event_dismiss, retention_protect (owner only), bench_site
(owner only), bench_ask. Writes need the schedule-drafting permission.
"""
import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)

PARTS = ("what_if", "standby", "ladder", "events", "strength", "tips", "retention", "org", "outcomes")
OWNER_ONLY = ("tips", "retention")


def is_owner(user) -> bool:
    """The account holder (owner or the restaurant's primary login) or an
    admin — never a manager below them (retention and tips are theirs)."""
    from permissions import ROLE_CLIENT, ROLE_OWNER
    u = user or {}
    return bool(u.get("is_admin") or u.get("role") in (ROLE_OWNER, ROLE_CLIENT))


def _history(restaurant_id, history_id):
    if not history_id:
        return None, [], []
    import models
    from schedule_versions import rows_from_csv
    conn = models.get_conn()
    try:
        h = conn.execute("SELECT id, week_start, week_end, schedule_csv FROM schedule_history WHERE id=? AND "
                         "restaurant_id=?", (int(history_id), restaurant_id)).fetchone()
    finally:
        conn.close()
    if not h:
        return None, [], []
    d0, d1 = date.fromisoformat(str(h["week_start"])[:10]), date.fromisoformat(str(h["week_end"] or h["week_start"])[:10])
    dates = [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
    return h["id"], dates, rows_from_csv(h["schedule_csv"] or "")


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _name(user):
    u = user or {}
    return (u.get("username") or u.get("email") or "owner")[:120]


def get_part(restaurant_id, part, args, user=None) -> tuple:
    if part not in PARTS:
        return {"ok": False, "error": f"part must be one of {', '.join(PARTS)}"}, 400
    if part in OWNER_ONLY and not is_owner(user):
        return {"ok": False, "error": "Only the owner sees this."}, 403
    if part == "what_if":
        import service_sim
        change = _int(args.get("change"), -1)
        role = str(args.get("role") or "server").strip().lower()
        return service_sim.what_if(restaurant_id, args.get("weekday") or "Saturday", {role: change}), 200
    if part == "standby":
        import standby_insurance
        hid, dates, rows = _history(restaurant_id, args.get("history_id"))
        return dict(standby_insurance.owner_view(restaurant_id, week_dates=dates, rows=rows or None),
                    history_id=hid), 200
    if part == "ladder":
        import commitment_ladder
        hid, _d, _r = _history(restaurant_id, args.get("history_id"))
        return commitment_ladder.owner_view(restaurant_id, history_id=hid), 200
    if part == "events":
        import event_reactive
        items = event_reactive.proposals(restaurant_id)
        items.sort(key=lambda p: (p["status"] != "open", p["date"]))
        return {"ok": True, "proposals": items[:30]}, 200
    if part == "strength":
        import bench_strength
        return bench_strength.owner_view(restaurant_id), 200
    if part == "tips":
        import tip_opportunity
        return tip_opportunity.owner_view(restaurant_id), 200
    if part == "retention":
        import retention_risk
        return retention_risk.assess(restaurant_id), 200
    if part == "org":
        import org_bench
        out = org_bench.owner_view(restaurant_id)
        if args.get("date") and args.get("role"):
            out["gap"] = org_bench.candidates(restaurant_id, str(args.get("date"))[:10], args.get("role"),
                                              args.get("start") or "", args.get("end") or "")
        return out, 200
    import outcome_benchmark
    return outcome_benchmark.restaurant_view(restaurant_id), 200


def post_action(restaurant_id, data, user=None) -> tuple:
    d = data or {}
    action = d.get("action")
    by = _name(user)
    try:
        if action == "standby_offer":
            import standby_insurance
            slot = standby_insurance.offer(restaurant_id, d.get("date"), d.get("role"), d.get("employee"),
                                           d.get("shift_start"), d.get("shift_end"), by=by,
                                           history_id=_int(d.get("history_id")), chance=d.get("chance"),
                                           expected_cost=d.get("expected_cost"))
            return {"ok": True, "slot": slot}, 200
        if action == "standby_withdraw":
            import standby_insurance
            return {"ok": True, "slot": standby_insurance.withdraw(restaurant_id, _int(d.get("slot_id"), 0), by=by)}, 200
        if action == "standby_policy":
            import standby_insurance
            return {"ok": True, "policy": standby_insurance.set_policy(restaurant_id, d.get("pay_hours"),
                                                                       d.get("flat_pay"), d.get("note"), by=by)}, 200
        if action == "event_approve":
            import event_reactive
            return {"ok": True, "proposal": event_reactive.approve(restaurant_id, _int(d.get("proposal_id"), 0), by=by)}, 200
        if action == "event_dismiss":
            import event_reactive
            return {"ok": event_reactive.dismiss(restaurant_id, _int(d.get("proposal_id"), 0), by=by)}, 200
        if action == "retention_protect":
            if not is_owner(user):
                return {"ok": False, "error": "Only the owner can change this."}, 403
            import retention_risk
            return {"ok": True, **retention_risk.set_protect(restaurant_id, d.get("name"), bool(d.get("protect")), by=by)}, 200
        if action == "bench_site":
            if not is_owner(user):
                return {"ok": False, "error": "Only the owner can turn the bench on or off."}, 403
            import org_bench
            return {"ok": True, **org_bench.set_site(restaurant_id, bool(d.get("enabled")), by=by)}, 200
        if action == "bench_ask":
            import org_bench
            return {"ok": True, "request": org_bench.ask(restaurant_id, _int(d.get("member_id"), 0), d.get("date"),
                                                         d.get("role"), d.get("shift_start"), d.get("shift_end"),
                                                         by=by)}, 200
    except ValueError as e:          # every module's refusal is a ValueError subclass
        return {"ok": False, "error": str(e)}, 400
    return {"ok": False, "error": "unknown action"}, 400


def api_body(restaurant_id, method, data=None, args=None, user=None, may_write=False) -> tuple:
    """The one body of GET/POST /api/labor/bench and its phone twin."""
    if method == "POST":
        if not may_write:
            return {"ok": False, "error": "Only someone who builds the schedule can do this."}, 403
        return post_action(restaurant_id, data, user=user)
    return get_part(restaurant_id, str((args or {}).get("part") or "strength"), args or {}, user=user)
