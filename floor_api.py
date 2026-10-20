"""
floor_api.py — the route bodies for the schedule on the floor (schedule audit
10/10/26, workstream J2). Mounted by strategy_routes' table, so each exists on
the web (/api/...) and the phone (/mobile/api/...) with one shared body:

  GET  /labor/floor-settings                 the switches (floor_settings)
  POST /labor/floor-settings                 {pos_push?, live_moves?, autopilot?, live_target_margin_pts?}
  GET  /labor/pos-schedule-push?schedule_id= the preview: every shift and what would happen to it
  POST /labor/pos-schedule-push              {schedule_id} — push now (queued + sent)
  GET  /labor/live                           the live labor read and today's moves
  POST /labor/live/moves/<id>/act            carry out a suggested move
  POST /labor/live/moves/<id>/dismiss        not this one
  POST /labor/live/moves/<id>/reverse        undo an automatic one (autopilot lapses)
  POST /labor/shift-edit/plan                one shift edit judged and priced, nothing saved
  POST /labor/shift-edit                     the edit saved (Ask's confirm card posts here)
  GET  /labor/cross-site/candidates          people at sibling locations who can take a shift
  POST /labor/cross-site/offer               offer an open shift to one of them

Each body takes (current_user) and returns (payload, status).
"""
from flask import request


def _sr():
    import strategy_routes
    return strategy_routes


def _body():
    return request.get_json(silent=True) or {}


def _rid(u):
    return u["restaurant_id"]


def _restaurant(u):
    from models import get_restaurant
    return get_restaurant(_rid(u))


# ── switches ────────────────────────────────────────────────────────────────

def floor_settings_get(u):
    if not _sr()._sees_labor(u):
        return _sr()._forbidden("Only someone who can see labor can see this.")
    import floor_settings
    import labor_live
    import pos
    name, mod = pos.connected_provider(_rid(u))
    caps = pos.capabilities(mod) if mod else {}
    # The week the preview opens on: the nearest published one not yet over.
    from models import get_conn
    import time_utils as _tu
    today = _tu.restaurant_now(_restaurant(u), naive=True).date().isoformat()
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL AND "
                           "superseded_by IS NULL AND substr(week_end,1,10) >= ? ORDER BY week_start, id DESC LIMIT 1",
                           (_rid(u), today)).fetchone()
    finally:
        conn.close()
    return {"ok": True, "settings": floor_settings.get(_restaurant(u)), "can_edit": _sr()._may_publish(u),
            "pos": {"provider": name, "label": {"rpower": "RPOWER"}.get(name, (name or "").title() or None),
                    "schedule_push": bool(caps.get("schedule_push")),
                    "schedule_preview": bool(caps.get("schedule_preview"))},
            "published_schedule_id": row["id"] if row else None,
            "autopilot": {k: labor_live.trust(_rid(u), k) for k in labor_live.KINDS}}, 200


def floor_settings_set(u):
    """SCHEDULE_PUBLISH: these decide what reaches the POS clock and staff."""
    if not _sr()._may_publish(u):
        return _sr()._forbidden("Your login can't change these.")
    import floor_settings
    b = _body()
    if b.get("pos_push") is True:
        # Turned on from the preview only: a POS that can't take a schedule
        # can't be switched on, and the owner has seen what would be sent.
        import pos_schedule_push
        name, mod = pos_schedule_push._provider(_rid(u))
        if mod is None or not callable(getattr(mod, "push_labor_schedule", None)):
            return {"ok": False, "error": f"{pos_schedule_push._label(name)} has no schedule write Cavnar AI can "
                                          "use."}, 400
        if not b.get("previewed"):
            return {"ok": False, "error": "Look at the preview first — it shows each shift and what the POS would "
                                          "do with it."}, 400
    try:
        out = floor_settings.save(_rid(u), {k: v for k, v in b.items() if k != "previewed"}, user=u)
    except floor_settings.SettingsError as e:
        return {"ok": False, "error": str(e)}, 400
    from client_api import log_account_event
    log_account_event(_rid(u), "floor_settings_changed", current_user=u,
                      detail=", ".join(f"{k}={b[k]}" for k in sorted(b) if k != "previewed")[:200])
    return {"ok": True, "settings": out}, 200


# ── the POS schedule push ───────────────────────────────────────────────────

def _schedule_id():
    try:
        return int(request.args.get("schedule_id") or (_body().get("schedule_id") if request.method == "POST" else 0)
                   or 0)
    except (TypeError, ValueError):
        return 0


def pos_push_preview(u):
    if not _sr()._sees_labor(u):
        return _sr()._forbidden("Only someone who can see labor can see this.")
    sid = _schedule_id()
    if not sid:
        return {"ok": False, "error": "Say which schedule (schedule_id)."}, 400
    import pos_schedule_push
    try:
        out = pos_schedule_push.preview(_rid(u), sid)
    except Exception as e:
        return {"ok": False, "error": f"The POS couldn't be read for the preview: {e}"}, 502
    return out, (200 if out.get("ok") else 400)


def pos_push_now(u):
    """Push the published week now — the owner's press, with the switch on."""
    if not _sr()._may_publish(u):
        return _sr()._forbidden("Your login can't send the schedule.")
    sid = _schedule_id()
    if not sid:
        return {"ok": False, "error": "Say which schedule (schedule_id)."}, 400
    import pos_schedule_push
    pid = pos_schedule_push.enqueue(_rid(u), sid, actor=u, reason="manual")
    if not pid:
        return {"ok": False, "error": "Turn on \"Send published weeks to the POS\" first — and only a week that "
                                      "has gone to staff can go."}, 400
    res = pos_schedule_push.run_push(pid)
    week = (pos_schedule_push._hist(_rid(u), sid) or {}).get("week_start")
    return {"ok": res.get("status") in ("sent", "unchanged", "partial"), **res,
            "push": pos_schedule_push.last_push(_rid(u), week) if week else None}, 200


# ── live labor ──────────────────────────────────────────────────────────────

def live_read(u):
    """The kept read when fresh (the 15-minute job's), else built again now
    from the shared live snapshot (pos._live_read) — never a POS request on
    this request thread (scale audit 10/19/26 A3-09): with no recent read the
    page states the last hourly reading and the schedule instead."""
    if not _sr()._sees_labor(u):
        return _sr()._forbidden("Only someone who can see labor can see this.")
    import labor_live
    import time_utils as _tu
    r = _restaurant(u)
    local = _tu.restaurant_now(r, naive=True)
    day = _tu.business_date(r, local)
    rd, age = labor_live.kept_read(r.id, day)
    if rd is None or age is None or age > labor_live.READ_FRESH_MINUTES:
        rd = labor_live.read(r, now_local=local, live=True)
        if rd.get("available"):
            try:
                rd["moves"] = labor_live.moves(r, rd=rd, now_local=local)
            except Exception:
                rd["moves"] = []
    return {"ok": True, "read": rd, "moves_today": labor_live.todays_moves(r.id, day),
            "can_act": _sr()._may_decide_requests(u)}, 200


def _move_call(u, move_id, fn):
    if not _sr()._may_decide_requests(u):
        return _sr()._forbidden("Your login can't change tonight's schedule.")
    out = fn(_rid(u), int(move_id), user=u)
    return out, (200 if out.get("ok") else 409)


def live_move_act(u, move_id):
    import labor_live
    return _move_call(u, move_id, labor_live.act)


def live_move_dismiss(u, move_id):
    import labor_live
    return _move_call(u, move_id, labor_live.dismiss)


def live_move_reverse(u, move_id):
    import labor_live
    return _move_call(u, move_id, labor_live.reverse)


# ── one shift edited (Ask's card, MARKET-20) ───────────────────────────────

def shift_edit_plan(u):
    if not _sr()._may_draft(u):
        return _sr()._forbidden("Your login can view labor but not change the schedule.")
    import shift_edits
    b = _body()
    out = shift_edits.plan(_rid(u), str(b.get("op") or "").lower(), b, history_id=b.get("history_id"))
    out.pop("_rows", None)
    # One shift's cost is that person's rate (R7-24): only for pay viewers.
    if not _sr()._sees_pay(u):
        out["cost"] = None
        out["pay_hidden"] = True
    return out, (200 if out.get("ok") else 400)


def shift_edit(u):
    if not _sr()._may_draft(u):
        return _sr()._forbidden("Your login can view labor but not change the schedule.")
    import shift_edits
    out = shift_edits.apply(_rid(u), _body(), user=u)
    out.pop("_rows", None)
    status = 200 if out.get("ok") else (409 if out.get("conflict") else 400)
    return out, status


# ── people from sibling locations (MARKET-16) ──────────────────────────────

def cross_site_candidates(u):
    if not _sr()._sees_labor(u):
        return _sr()._forbidden("Only someone who can see labor can see this.")
    import cross_site
    try:
        rid_ = int(request.args.get("request_id") or 0)
    except (TypeError, ValueError):
        rid_ = 0
    if not rid_:
        return {"ok": False, "error": "Say which open shift (request_id)."}, 400
    return {"ok": True, "candidates": cross_site.candidates(_rid(u), rid_)}, 200


def cross_site_offer(u):
    if not _sr()._may_decide_requests(u):
        return _sr()._forbidden("Your login can't offer shifts.")
    import cross_site
    b = _body()
    try:
        out = cross_site.offer(_rid(u), int(b.get("request_id") or 0), str(b.get("name") or ""),
                               int(b.get("home_restaurant_id") or 0), actor=u)
    except cross_site.CrossSiteError as e:
        return {"ok": False, "error": str(e)}, 400
    return dict(out, ok=True), 200


def shift_offer_reverse(u, offer_id):
    """Withdraw an automatic offer; automatic offers go back to the
    managers (request_autonomy.reverse_offer, MARKET-25)."""
    if not _sr()._may_decide_requests(u):
        return _sr()._forbidden("Your login can't change offers.")
    import request_autonomy
    out = request_autonomy.reverse_offer(_rid(u), int(offer_id), actor=u.get("username") or u.get("email"))
    return out, (200 if out.get("ok") else 404)


ROUTES = [
    ("/labor/shift-offers/<int:offer_id>/reverse", ["POST"], shift_offer_reverse, "shift_offer_reverse"),
    ("/labor/floor-settings", ["GET"], floor_settings_get, "floor_settings_get"),
    ("/labor/floor-settings", ["POST"], floor_settings_set, "floor_settings_set"),
    ("/labor/pos-schedule-push", ["GET"], pos_push_preview, "pos_schedule_push_preview"),
    ("/labor/pos-schedule-push", ["POST"], pos_push_now, "pos_schedule_push_now"),
    ("/labor/live", ["GET"], live_read, "live_labor_read"),
    ("/labor/live/moves/<int:move_id>/act", ["POST"], live_move_act, "live_move_act"),
    ("/labor/live/moves/<int:move_id>/dismiss", ["POST"], live_move_dismiss, "live_move_dismiss"),
    ("/labor/live/moves/<int:move_id>/reverse", ["POST"], live_move_reverse, "live_move_reverse"),
    ("/labor/shift-edit/plan", ["POST"], shift_edit_plan, "shift_edit_plan"),
    ("/labor/shift-edit", ["POST"], shift_edit, "shift_edit"),
    ("/labor/cross-site/candidates", ["GET"], cross_site_candidates, "cross_site_candidates"),
    ("/labor/cross-site/offer", ["POST"], cross_site_offer, "cross_site_offer"),
]
