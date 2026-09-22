"""
strategy_routes.py — the HTTP surface for the strategic-foundation modules:
issues, goals, outcome tracking, the dish scorecard and reprice suggestions,
invoice scanning, the demand forecast, loss signals and the morning brief.

Every route exists twice, web (/api/...) and mobile (/mobile/api/...), and
both halves call ONE shared body — the web/mobile-twin rule in CLAUDE.md. The
bodies take (current_user) and return (payload, status).

PERMISSIONS
  * Module gating comes from the path, as everywhere else: /food-cost/... is
    the Food Cost module plus FOOD_COST_VIEW (so a manager never sees margins
    or invoice prices), /labor/... is Labor. See auth._MODULE_PREFIXES.
  * Loss signals need LOSS_VIEW: owners hold it, and an owner may grant it
    to a manager (permission_grants) — a signal can name the approving
    manager, so it is a deliberate per-person choice. Issue routing and the
    brief's send settings are principal-only (TEAM_INVITE). The brief itself
    is built per viewer from what that login may see.
  * Goals and outcomes on food-cost metrics are hidden from a login without
    FOOD_COST_VIEW, the same line the Food Cost tab draws.

The public issue page (/i/<token>) lives here too, on its own blueprint: the
token IS the credential, there is no session and no CSRF cookie to carry.
"""
from datetime import date

from flask import Blueprint, jsonify, request, render_template, abort

from auth import login_required, mobile_login_required

strategy_bp = Blueprint("strategy", __name__)
strategy_mobile_bp = Blueprint("strategy_mobile", __name__, url_prefix="/mobile/api")
issue_link_bp = Blueprint("issue_link", __name__)

# Metrics a login without FOOD_COST_VIEW may not see goals/outcomes for.
_FOOD_METRICS = {"food_cost_pct", "weekly_waste"}


def _rid(u):
    return u["restaurant_id"]


def _principal(u):
    from permissions import has_permission, TEAM_INVITE
    return bool(u.get("is_admin")) or has_permission(u, TEAM_INVITE)


def _sees_food(u):
    from permissions import has_permission, FOOD_COST_VIEW
    return bool(u.get("is_admin")) or has_permission(u, FOOD_COST_VIEW)


def _metric_visible(u, metric):
    base = (metric or "").split(":", 1)[0]
    return base not in _FOOD_METRICS or _sees_food(u)


def _local_today(u):
    from time_utils import restaurant_now_by_id
    return restaurant_now_by_id(_rid(u), naive=True).date()


def _body():
    return request.get_json(silent=True) or {}


def _forbidden(msg="Only the account owner can see this."):
    return {"ok": False, "error": msg}, 403


def _limited(u, bucket, max_calls, window_secs):
    """Per-restaurant limiter for routes that text a phone or run a paid
    model. The daily AI budget stops runaway spend; this stops one screen
    (or one script) from texting a manager twenty times in a minute."""
    from ai_utils import ai_rate_limited
    return ai_rate_limited(f"{bucket}:{_rid(u)}", max_calls=max_calls, window_secs=window_secs)


_SLOW_DOWN = ({"ok": False, "error": "That's a lot in a short time — wait a few minutes and try again."}, 429)


# ── issues ────────────────────────────────────────────────────────────────────

def _do_issues_list(u):
    import issues
    status = request.args.get("status") or "unresolved"
    return {"ok": True, "issues": issues.list_issues(_rid(u), status=status),
            "summary": issues.summary(_rid(u))}, 200


def _do_issue_create(u):
    import issues
    b = _body()
    title = (b.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "Give the issue a title."}, 400
    contact = b.get("assignee_contact_id")
    if _limited(u, "issue_text", 20, 3600):
        return _SLOW_DOWN
    try:
        issue, _token = issues.create_issue(
            _rid(u), "manual", title, detail=(b.get("detail") or "").strip() or None,
            severity=b.get("severity") or "normal",
            assignee_contact_id=int(contact) if contact not in (None, "") else None,
            created_by=u.get("id"))
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True, "issue": issue}, 200


def _do_issue_resolve(u, issue_id):
    import issues
    out = issues.resolve(_rid(u), issue_id, note=(_body().get("note") or "").strip() or None)
    if not out:
        return {"ok": False, "error": "Issue not found."}, 404
    return {"ok": True, "issue": out}, 200


def _do_issue_reassign(u, issue_id):
    import issues
    if _limited(u, "issue_text", 20, 3600):
        return _SLOW_DOWN
    try:
        out = issues.reassign(_rid(u), issue_id, int(_body().get("contact_id")))
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": str(e) or "Pick a contact."}, 400
    if not out:
        return {"ok": False, "error": "Issue not found or already resolved."}, 404
    return {"ok": True, "issue": out}, 200


def _do_routing_get(u):
    if not _principal(u):
        return _forbidden()
    import issues
    from notify import get_alert_contacts
    contacts = [{"id": c["id"], "name": c["name"], "sms_consent": bool(c.get("sms_consent"))}
                for c in (get_alert_contacts(_rid(u)) or [])]
    return {"ok": True, "routing": issues.get_routing(_rid(u)), "contacts": contacts}, 200


def _do_routing_set(u):
    if not _principal(u):
        return _forbidden()
    import issues
    b = _body()
    role = b.get("role")
    if role not in ("manager", "escalation"):
        return {"ok": False, "error": "role must be manager or escalation"}, 400
    try:
        if b.get("contact_id") in (None, ""):
            issues.clear_routing(_rid(u), role)
        else:
            issues.set_routing(_rid(u), role, int(b["contact_id"]),
                               escalate_after_minutes=b.get("escalate_after_minutes"))
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True, "routing": issues.get_routing(_rid(u))}, 200


# ── goals & outcomes ──────────────────────────────────────────────────────────

def _do_goals_list(u):
    import goals
    return {"ok": True, "goals": [g for g in goals.progress(_rid(u))
                                  if _metric_visible(u, g.get("metric"))]}, 200


def _do_goal_set(u):
    import goals
    b = _body()
    if not _metric_visible(u, b.get("metric")):
        return _forbidden("Food cost goals are for logins that can see food cost.")
    try:
        g = goals.set_goal(_rid(u), b.get("metric"), b.get("target"), deadline=b.get("deadline"),
                           note=b.get("note"), user_id=u.get("id"))
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True, "goal": g}, 200


def _do_goal_end(u, goal_id):
    import goals
    goals.end_goal(_rid(u), goal_id)
    return {"ok": True}, 200


def _do_outcomes_list(u):
    import outcomes
    rows = outcomes.list_outcomes(_rid(u), status=request.args.get("status") or None)
    live = {p["id"]: p for p in outcomes.progress(_rid(u))}
    out = []
    for r in rows:
        if not _metric_visible(u, r.get("metric")):
            continue
        if r["id"] in live:
            r = {**r, **{k: v for k, v in live[r["id"]].items() if k.startswith("interim")}}
        out.append({**r, "summary": outcomes.summarise(r) if r.get("status") != "tracking" else None})
    return {"ok": True, "outcomes": out, "caveat": outcomes.CAUSATION_CAVEAT}, 200


def _do_outcome_record(u):
    import outcomes
    b = _body()
    title = (b.get("title") or "").strip()
    if not title or not b.get("metric"):
        return {"ok": False, "error": "A title and a metric are required."}, 400
    if not _metric_visible(u, b.get("metric")):
        return _forbidden("Food cost tracking is for logins that can see food cost.")
    wd = b.get("window_days")
    if wd not in (None, ""):
        try:
            wd = int(wd)
        except (TypeError, ValueError):
            return {"ok": False, "error": "window_days must be a whole number"}, 400
        if not 7 <= wd <= 180:
            return {"ok": False, "error": "Track for between 7 and 180 days."}, 400
    else:
        wd = None
    try:
        o = outcomes.record(_rid(u), b.get("source") or "manual",
                            b.get("source_key") or f"manual:{title.lower()[:80]}",
                            title, b["metric"], user_id=u.get("id"),
                            window_days=wd)
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    # A tracker whose BASELINE could not be measured will come back "unknown"
    # when its window closes, 28 days from now, having told the owner
    # nothing. Still allowed — it is their change to track, and the data may
    # arrive tomorrow — but say so at the moment they press the button
    # rather than a month later.
    warning = None
    if o.get("baseline_value") is None:
        warning = (f"Tracking started, but {(o.get('metric_label') or o['metric']).lower()} "
                   f"can't be read right now ({o.get('baseline_detail') or 'no data'}), so "
                   f"there may be nothing to compare against.")
    return {"ok": True, "outcome": o, "warning": warning}, 200


def _do_outcome_abandon(u, outcome_id):
    import outcomes
    outcomes.abandon(_rid(u), outcome_id)
    return {"ok": True}, 200


def _do_value(u):
    """What Cavnar AI has been worth, and what is still on the table.

    Three figures that are never summed into each other (see
    value_delivered.py), plus the all-time biggest measured win and — where
    a sales audit is linked to the account — what that audit estimated
    against what has since been measured.

    Food-cost dollars are filtered the same way goals and outcomes are: a
    manager without FOOD_COST_VIEW sees the labor and reviews halves and
    not the margin ones.
    """
    import value_delivered
    rid = _rid(u)
    # Filtered BEFORE anything is summed. An earlier version of this route
    # stripped by_module and the opportunity items but left the headline
    # total whole, which handed a manager without FOOD_COST_VIEW the margin
    # dollars straight back by subtraction.
    denied = set() if _metric_visible(u, "food_cost_pct") else {"inventory"}
    out = value_delivered.breakdown(rid, denied_modules=denied)
    # Distinct work since sign-up. Needs no button and carries no dollars,
    # so it is not filtered by module permission — a manager may know the
    # product drafted 200 replies.
    try:
        led = value_delivered.ledger(rid)
        out["ledger"] = dict(led, lines=value_delivered.ledger_lines(led))
    except Exception as e:
        import ops
        ops.capture(e, job="value_ledger", context=f"restaurant_id={rid}")

    # The audit comparison is owner-level: it carries whole-business dollars
    # and what Will quoted at the table.
    try:
        from permissions import has_permission, TEAM_INVITE
        if has_permission(u, TEAM_INVITE):
            import promise
            out["promise"] = promise.compare(rid)
    except Exception as e:
        import ops
        ops.capture(e, job="value_promise", context=f"restaurant_id={rid}")
    return {"ok": True, **out}, 200


def _do_metrics(u):
    import metrics
    keys = [k for k in ("labor_pct", "food_cost_pct", "sales", "avg_rating", "weekly_waste")
            if _metric_visible(u, k)]
    return {"ok": True, "metrics": [{"key": k, **metrics.describe(k)} for k in keys]}, 200


# ── food cost: menu + invoices ────────────────────────────────────────────────

def _do_dish_scorecard(u):
    import menu_intelligence
    return {"ok": True, **menu_intelligence.dish_scorecard(_rid(u))}, 200


def _do_reprice(u):
    import menu_intelligence
    return {"ok": True, **menu_intelligence.reprice_suggestions(_rid(u))}, 200


def _do_invoice_scan(u):
    import invoices
    f = request.files.get("file")
    if not f:
        return {"ok": False, "error": "Attach a photo or PDF of the invoice."}, 400
    if _limited(u, "invoice_scan", 10, 600):
        return _SLOW_DOWN
    data = f.read()
    media_type = (f.mimetype or "").lower()
    if media_type == "image/jpg":
        media_type = "image/jpeg"
    try:
        out = invoices.scan(_rid(u), data, media_type, user_id=u.get("id"))
        # Supplier memory (ordering.py): a supplier whose scans the owner
        # has applied unchanged three times gets its clean lines applied on
        # scan; flagged lines still wait.
        try:
            import ordering
            out = ordering.auto_apply_if_trusted(_rid(u), out, user_id=u.get("id"))
        except Exception as _ae:
            import ops
            ops.capture(_ae, job="invoice_auto_apply", context=f"restaurant_id={_rid(u)}")
    except invoices.InvoiceError as e:
        return {"ok": False, "error": str(e)}, 400
    except Exception as e:
        from ai_utils import AIBudgetExceeded
        if isinstance(e, AIBudgetExceeded):
            return {"ok": False, "error": str(e)}, 429
        import ops
        ops.capture(e, job="invoice_scan", context=f"restaurant_id={_rid(u)}")
        return {"ok": False, "error": "The invoice couldn't be read right now. Try again shortly."}, 502
    return {"ok": True, "invoice": out}, 200


def _do_invoice_list(u):
    import invoices
    return {"ok": True, "invoices": invoices.list_imports(_rid(u))}, 200


def _do_invoice_get(u, import_id):
    import invoices
    out = invoices.get_import(_rid(u), import_id)
    if not out:
        return {"ok": False, "error": "Invoice not found."}, 404
    return {"ok": True, "invoice": out}, 200


def _do_invoice_apply(u, import_id):
    import invoices
    sel = _body().get("lines")
    if not isinstance(sel, list) or not sel:
        return {"ok": False, "error": "Pick at least one line to update."}, 400
    out = invoices.apply(_rid(u), import_id, sel, user_id=u.get("id"))
    return out, (200 if out.get("ok") else 409)


# ── the action queue ──────────────────────────────────────────────────────────

def _do_actions(u):
    """Everything still open for this login, most pressing first."""
    import action_queue
    return {"ok": True, **action_queue.items(_rid(u), viewer=u, today=_local_today(u))}, 200


def _do_action_snooze(u):
    import action_queue
    b = _body()
    key = (b.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "Which item?"}, 400
    out = action_queue.snooze(_rid(u), key, days=b.get("days") or action_queue.SNOOZE_DAYS,
                              user_id=u.get("id"), today=_local_today(u))
    return {"ok": True, **out}, 200


# ── close-out ─────────────────────────────────────────────────────────────────

def _do_closeout_get(u):
    """Tonight's close-out (or the last one filed), and the questions to
    answer. Any console login may read and write it: the person who closes
    is usually not the owner."""
    import closeout
    from models import get_restaurant
    r = get_restaurant(_rid(u))
    day = closeout.business_date_for(r)
    return {"ok": True, "business_date": day.isoformat(),
            "closeout": closeout.get(_rid(u), day.isoformat()),
            "previous": closeout.latest(_rid(u), before=(day.isoformat())),
            "fields": list(closeout.FIELDS)}, 200


def _do_closeout_save(u):
    import closeout
    from models import get_restaurant
    b = _body()
    try:
        entry = closeout.save(_rid(u), b, user_id=u.get("id"),
                              submitted_by=(u.get("username") or "").title() or None,
                              restaurant=get_restaurant(_rid(u)))
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True, "closeout": entry}, 200


# ── labor: demand ─────────────────────────────────────────────────────────────

def _do_demand(u):
    import demand
    raw = (request.args.get("day") or "").strip()
    try:
        day = date.fromisoformat(raw) if raw else _local_today(u)
    except ValueError:
        return {"ok": False, "error": "day must be YYYY-MM-DD"}, 400
    return {"ok": True, "day": day.isoformat(), "forecast": demand.forecast_day(_rid(u), day),
            "slow_days": demand.slow_days(_rid(u)), "prep": demand.prep_list(_rid(u), day)}, 200


def _do_auto_draft_get(u):
    from models import get_restaurant
    r = get_restaurant(_rid(u))
    return {"ok": True, "enabled": bool(getattr(r, "auto_draft_schedule", 0)),
            "external_tool": getattr(r, "external_scheduling_tool", None) or ""}, 200


def _do_auto_draft_set(u):
    from models import update_restaurant
    b = _body()
    fields = {}
    if "enabled" in b:
        fields["auto_draft_schedule"] = 1 if b["enabled"] else 0
    if "external_tool" in b:
        fields["external_scheduling_tool"] = (str(b["external_tool"] or "").strip()[:60]) or None
    if not fields:
        return {"ok": False, "error": "Nothing to change."}, 400
    update_restaurant(_rid(u), fields)
    return _do_auto_draft_get(u)


def _do_auto_publish_get(u):
    from models import get_restaurant, schedule_publish_trust, SCHEDULE_PUBLISH_TRUST_MIN
    r = get_restaurant(_rid(u))
    trust = schedule_publish_trust(_rid(u))
    return {"ok": True, "enabled": bool(getattr(r, "auto_publish_schedule", 0)),
            "trust": trust, "needed": SCHEDULE_PUBLISH_TRUST_MIN,
            "armed": bool(getattr(r, "auto_publish_schedule", 0)) and trust >= SCHEDULE_PUBLISH_TRUST_MIN}, 200


def _do_auto_publish_set(u):
    from models import update_restaurant
    from client_api import log_account_event
    b = _body()
    if "enabled" not in b:
        return {"ok": False, "error": "Nothing to change."}, 400
    update_restaurant(_rid(u), {"auto_publish_schedule": 1 if b["enabled"] else 0})
    log_account_event(_rid(u), "auto_publish_changed", current_user=u, detail="on" if b["enabled"] else "off")
    return _do_auto_publish_get(u)


def _do_auto_order_get(u):
    from models import get_restaurant
    import ordering
    r = get_restaurant(_rid(u))
    # The record per supplier, so the switch says what it would do.
    suppliers = []
    try:
        from models import get_conn
        conn = get_conn()
        try:
            rows = conn.execute("SELECT DISTINCT supplier_name, supplier_email FROM ingredients WHERE restaurant_id=? "
                                "AND is_active=1 AND supplier_email IS NOT NULL AND supplier_email != ''", (_rid(u),)).fetchall()
        finally:
            conn.close()
        for row in rows:
            t = ordering.supplier_trust(_rid(u), row["supplier_email"])
            suppliers.append({"name": row["supplier_name"] or row["supplier_email"], "email": row["supplier_email"],
                              "orders": t["orders"], "median_total": t["median_total"], "trusted": t["trusted"],
                              "needed": max(0, ordering.ORDER_TRUST_MIN - t["orders"])})
    except Exception:
        pass
    return {"ok": True, "enabled": bool(getattr(r, "auto_order_trusted", 0)), "suppliers": suppliers,
            "undo_minutes": ordering.ORDER_UNDO_MINUTES}, 200


def _do_auto_order_set(u):
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can change ordering.")
    from models import update_restaurant
    from client_api import log_account_event
    b = _body()
    if "enabled" not in b:
        return {"ok": False, "error": "Nothing to change."}, 400
    update_restaurant(_rid(u), {"auto_order_trusted": 1 if b["enabled"] else 0})
    log_account_event(_rid(u), "auto_order_changed", current_user=u, detail="on" if b["enabled"] else "off")
    return _do_auto_order_get(u)


# ── #7 the count sheet, pre-filled ───────────────────────────────────────────
# Counts were typed from scratch (a CSV column) while the ledger already
# held the theoretical on-hand — last recount + receiving − depletion. The
# sheet opens filled with that number; the owner corrects, not types.

def _do_count_sheet_get(u):
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can count.")
    import inventory_ledger
    rows = inventory_ledger.list_ingredients(_rid(u))
    out = [{"ingredient_id": r["id"], "name": r["name"], "unit": r.get("unit") or "",
            "category": r.get("category") or "", "expected": r.get("current_stock"),
            "par_level": r.get("par_level"), "last_recount_at": r.get("last_recount_at"),
            "avg_daily_usage": r.get("avg_daily_usage"),
            # The web's Suppliers block reads these; the order draft groups by them.
            "supplier_name": r.get("supplier_name") or "",
            "supplier_email": r.get("supplier_email") or ""} for r in rows]
    return {"ok": True, "items": out, "count": len(out)}, 200


def _do_count_sheet_save(u):
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can count.")
    import inventory_ledger
    from client_api import log_account_event
    b = _body()
    items = b.get("items")
    if not isinstance(items, list) or not items:
        return {"ok": False, "error": "Nothing counted."}, 400
    if len(items) > 500:
        return {"ok": False, "error": "That is more items than one count holds."}, 400
    # The date is the count's place in the ledger, so it has to be one.
    # It went straight to record_recount unchecked: "9/21/26" — the
    # product's own display format — sorts after every ISO date and sat
    # inside every future 7-day window, and a future date did the same
    # (MOD-FC-15).
    day = str(b.get("date") or "").strip() or None
    if day:
        from datetime import date as _date
        try:
            parsed = _date.fromisoformat(day)
        except ValueError:
            return {"ok": False, "error": "Pick the count date from the calendar."}, 400
        if len(day) != 10 or parsed > _local_today(u):
            return {"ok": False, "error": "A count can't be dated in the future."
                    if parsed > _local_today(u) else "Pick the count date from the calendar."}, 400
        day = parsed.isoformat()
    written, skipped = 0, []
    for it in items:
        try:
            ing_id = int(it.get("ingredient_id"))
            qty = float(it.get("counted"))
        except (TypeError, ValueError, AttributeError):
            skipped.append(it)
            continue
        if qty < 0 or qty > 1e7:
            skipped.append(it)
            continue
        try:
            inventory_ledger.record_recount(_rid(u), ing_id, qty, event_date=day, source="count_sheet")
            written += 1
        except Exception:
            skipped.append(it)
    if written:
        log_account_event(_rid(u), "inventory_counted", current_user=u, detail=f"{written} items")
    return {"ok": written > 0, "written": written, "skipped": len(skipped),
            "error": None if written else "No usable lines — each needs an ingredient and a number."}, (200 if written else 400)


def _do_recipe_drafts(u):
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can review recipes.")
    import recipes
    # `missing` is what the "draft them now" button offers; `ingredients` is
    # whether a draft can be attempted at all (it may only use what is on
    # the list). Both best-effort: the drafts themselves are the point.
    missing = ingredients = 0
    try:
        import inventory_ledger
        missing = len(recipes.missing_recipes(_rid(u)))
        ingredients = len([i for i in (inventory_ledger.list_ingredients(_rid(u)) or []) if i.get("name")])
    except Exception:
        pass
    return {"ok": True, "drafts": recipes.list_drafts(_rid(u)), "missing": missing,
            "ingredients": ingredients}, 200


def _do_recipe_draft_now(u):
    """The owner pastes the menu (or asks for the POS dishes that have no
    recipe) and Cavnar drafts recipes from the ingredient list — the
    accept gate is unchanged. Bounded to RECIPE_DRAFT_LIMIT model calls a
    request, so the response arrives inside the worker's timeout; the
    reply says how many are left and the button offers the next batch."""
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can draft recipes.")
    import recipes
    from client_api import log_account_event
    if _limited(u, "recipe_draft", 6, 600):
        return _SLOW_DOWN
    text = _body().get("menu") or ""
    if len(text) > 20_000:
        return {"ok": False, "error": "That is more than a menu — paste the dishes, one a line."}, 400
    try:
        if text.strip():
            out = recipes.draft_from_menu(_rid(u), text, user_id=u.get("id"), limit=recipes.RECIPE_DRAFT_LIMIT)
        else:
            out = {"ok": True, **recipes.draft_missing(_rid(u), limit=recipes.RECIPE_DRAFT_LIMIT)}
        if out.get("ok"):
            out["remaining"] = len(recipes.missing_recipes(_rid(u)))
    except Exception as e:
        import ops
        ops.capture(e, job="recipe_draft_now", context=f"restaurant_id={_rid(u)}")
        return {"ok": False, "error": "Couldn't draft just now — try again in a minute."}, 500
    if out.get("drafted"):
        log_account_event(_rid(u), "recipes_drafted", current_user=u, detail=f"{out['drafted']} drafts")
    return out, (200 if out.get("ok") else 400)


def _do_recipe_draft_accept(u, draft_id):
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can review recipes.")
    import recipes
    from client_api import log_account_event
    out = recipes.accept(_rid(u), int(draft_id), lines=_body().get("lines"), user_id=u.get("id"))
    if out.get("ok"):
        log_account_event(_rid(u), "recipe_accepted", current_user=u, detail=f"draft #{draft_id}, {out['written']} lines")
    return out, (200 if out.get("ok") else 409)


def _do_recipe_draft_reject(u, draft_id):
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can review recipes.")
    import recipes
    out = recipes.reject(_rid(u), int(draft_id), user_id=u.get("id"))
    return out, (200 if out.get("ok") else 409)


def _do_recipes_import(u):
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can import recipes.")
    import recipes
    from client_api import log_account_event
    text = _body().get("csv") or ""
    if len(text) > 400_000:
        return {"ok": False, "error": "That file is too large for one import."}, 400
    out = recipes.import_csv(_rid(u), text)
    if out.get("ok"):
        log_account_event(_rid(u), "recipes_imported", current_user=u, detail=f"{out['written']} lines")
    return out, (200 if out.get("ok") else 400)


def _do_weekly_plan_get(u):
    from models import get_restaurant
    r = get_restaurant(_rid(u))
    return {"ok": True, "enabled": bool(getattr(r, "weekly_plan_enabled", 0))}, 200


def _do_weekly_plan_set(u):
    if not _principal(u):
        return _forbidden("Only the account owner can turn the weekly plan on.")
    from models import update_restaurant
    from client_api import log_account_event
    b = _body()
    if "enabled" not in b:
        return {"ok": False, "error": "Nothing to change."}, 400
    update_restaurant(_rid(u), {"weekly_plan_enabled": 1 if b["enabled"] else 0})
    log_account_event(_rid(u), "weekly_plan_changed", current_user=u, detail="on" if b["enabled"] else "off")
    return _do_weekly_plan_get(u)


SEND_DELAY_CHOICES = (0, 5, 15)


def _do_send_delay_get(u):
    from models import get_restaurant
    r = get_restaurant(_rid(u))
    return {"ok": True, "minutes": int(getattr(r, "send_delay_minutes", 0) or 0), "choices": list(SEND_DELAY_CHOICES)}, 200


def _do_send_delay_set(u):
    if not _principal(u):
        return _forbidden("Only the account owner can change this.")
    from models import update_restaurant
    from client_api import log_account_event
    try:
        minutes = int(_body().get("minutes"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "minutes must be a number"}, 400
    if minutes not in SEND_DELAY_CHOICES:
        return {"ok": False, "error": f"Pick one of {', '.join(str(c) for c in SEND_DELAY_CHOICES)} minutes."}, 400
    update_restaurant(_rid(u), {"send_delay_minutes": minutes})
    log_account_event(_rid(u), "send_delay_changed", current_user=u, detail=f"{minutes} min")
    return _do_send_delay_get(u)


def _do_trust(u):
    """Why Cavnar AI stopped asking — every earned automation and the
    record behind it, in one place (moat audit #17)."""
    from models import get_restaurant, auto_approve_trust, schedule_publish_trust, SCHEDULE_PUBLISH_TRUST_MIN, AUTO_APPROVE_TRUST_MIN
    import ordering
    r = get_restaurant(_rid(u))
    out = {"ok": True, "auto_approve": {"enabled": bool(getattr(r, "auto_approve_earned", 0)),
                                        "min_approved": AUTO_APPROVE_TRUST_MIN,
                                        "bands": {str(k): v for k, v in auto_approve_trust(_rid(u)).items()}},
           "schedule": {"enabled": bool(getattr(r, "auto_publish_schedule", 0)),
                        "unedited_in_a_row": schedule_publish_trust(_rid(u)), "needed": SCHEDULE_PUBLISH_TRUST_MIN},
           "suppliers": [], "invoices": []}
    if _sees_food(u):
        try:
            from models import get_conn
            conn = get_conn()
            try:
                rows = conn.execute("SELECT DISTINCT supplier_name, supplier_email FROM ingredients WHERE restaurant_id=? "
                                    "AND is_active=1 AND supplier_email IS NOT NULL AND supplier_email != ''", (_rid(u),)).fetchall()
                inv = conn.execute("SELECT DISTINCT supplier FROM invoice_imports WHERE restaurant_id=? AND supplier IS NOT NULL",
                                   (_rid(u),)).fetchall()
            finally:
                conn.close()
            out["orders_enabled"] = bool(getattr(r, "auto_order_trusted", 0))
            for row in rows:
                t = ordering.supplier_trust(_rid(u), row["supplier_email"])
                out["suppliers"].append({"name": row["supplier_name"] or row["supplier_email"], **t,
                                         "needed": max(0, ordering.ORDER_TRUST_MIN - t["orders"])})
            for row in inv:
                t = ordering.invoice_trust(_rid(u), row["supplier"])
                out["invoices"].append({"supplier": row["supplier"], **t,
                                        "needed": max(0, ordering.INVOICE_TRUST_MIN - t["full_accepts"])})
        except Exception:
            pass
    return out, 200


def _do_memory_add(u):
    """The owner adds a fact directly — the profile is theirs to write, not
    only the assistant's to keep."""
    from models import remember_ask_fact
    from client_api import log_account_event
    b = _body()
    fact = (b.get("fact") or "").strip()[:300]
    kind = (b.get("kind") or "context").strip()
    if not fact:
        return {"ok": False, "error": "Write the fact first."}, 400
    if kind not in ("goal", "context", "preference", "followup"):
        kind = "context"
    try:
        saved = remember_ask_fact(_rid(u), fact, kind=kind, source="Account", user_id=u.get("id"))
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    log_account_event(_rid(u), "memory_added", current_user=u, detail=fact[:120])
    return {"ok": True, "fact": saved.get("fact") if isinstance(saved, dict) else fact}, 200


def _do_decisions(u):
    import decisions
    rows = decisions.history(_rid(u), limit=40)
    if not _sees_food(u):
        rows = [r for r in rows if not ((r.get("outcome") or {}).get("metric") or "").startswith(("food_cost", "weekly_waste"))]
    return {"ok": True, "decisions": rows}, 200


def _sees_labor(u):
    from permissions import has_permission, LABOR_VIEW
    return bool(u.get("is_admin")) or has_permission(u, LABOR_VIEW)


def _do_time_off_list(u):
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can review time off.")
    import time_off
    rows = time_off.recent(_rid(u))
    return {"ok": True, "requests": rows, "pending": sum(1 for r in rows if r["status"] == "pending")}, 200


def _do_time_off_decide(u, request_id):
    # Deciding time off changes who can be scheduled — the same power as
    # deciding a shift request, not a view-only labor login's (SCHED-22).
    if not _may_draft(u):
        return _forbidden("Your login can view labor but not decide time off.")
    import time_off
    from client_api import log_account_event
    b = _body()
    decision = (b.get("decision") or "").strip().lower()
    if decision not in ("approve", "deny"):
        return {"ok": False, "error": "decision must be approve or deny."}, 400
    row = time_off.decide(_rid(u), request_id, decision == "approve", decided_by=u.get("id"), note=b.get("note"))
    if not row:
        return {"ok": False, "error": "That request was already answered, or is not yours."}, 404
    log_account_event(_rid(u), "time_off_decided", current_user=u,
                      detail=f"{row['employee_name']} {row['start_date']}–{row['end_date']}: {row['status']}")
    out = {"ok": True, "request": row}
    if row["status"] == "approved":
        # A week staff already have may put them on those days: name each
        # shift so the manager covers it (SCHED-22 / MOD-EMP-8).
        try:
            conflicts = time_off.published_conflicts(_rid(u), row["employee_name"], row["start_date"], row["end_date"])
        except Exception:
            conflicts = []
        out["conflicts"] = conflicts
        if conflicts:
            from time_utils import mdy
            shifts = ", ".join(f"{(c.get('day') or '')[:3]} {mdy(c['date'])} {c['shift_start']}".strip() for c in conflicts[:4])
            out["warning"] = (f"{row['employee_name']} is still on the published schedule for {shifts}"
                              + (f" and {len(conflicts) - 4} more" if len(conflicts) > 4 else "")
                              + " — cover or move those shifts.")
    return out, 200


def _do_covers_get(u):
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see covers.")
    import covers
    return {"ok": True, "days": covers.recent(_rid(u))}, 200


def _do_covers_save(u):
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can enter covers.")
    import covers
    from client_api import log_account_event
    b = _body()
    rows = b.get("rows")
    if not isinstance(rows, list):
        rows = covers.parse_csv(b.get("csv") or "")
    if not rows:
        return {"ok": False, "error": "Send rows of date and covers, or a CSV with those two columns."}, 400
    out = covers.save(_rid(u), rows, source="manual")
    if out["written"]:
        log_account_event(_rid(u), "covers_imported", current_user=u, detail=f"{out['written']} days")
    return {"ok": True, **out}, 200


# ── Scheduling: the roster, per-person facts, pairings, dated signals, rules,
#    versions, requests. One body each, served to web and iOS alike. ────────

def _may_rate(u):
    from permissions import has_permission, TEAM_RATE
    return bool(u.get("is_admin")) or has_permission(u, TEAM_RATE)


def _may_draft(u):
    from permissions import has_permission, SCHEDULE_DRAFT
    return bool(u.get("is_admin")) or has_permission(u, SCHEDULE_DRAFT)


def _who(u):
    return (u.get("username") or u.get("email") or "").strip()[:120] or None


def _do_roster_get(u):
    """Everyone on the roster, active and deactivated, with the facts the
    schedule respects about them and their Operational Score."""
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see the roster.")
    import staff_settings as _ss
    from models import get_operational_scores, get_leader_flags
    rid = _rid(u)
    scores = get_operational_scores(rid)
    closers = get_leader_flags(rid)
    rel = {}
    try:
        rel = _ss.reliability(rid)
    except Exception:
        rel = {}
    out = []
    for e in _ss.roster(rid, include_inactive=True):
        out.append({**e, "score": scores.get(e["name"]), "can_close": bool(closers.get(e["name"])),
                    "reliability": rel.get(e["name"])})
    suggested = []
    try:
        import schedule_intel as _si
        suggested = _si.chemistry_suggestions(rid)
    except Exception:
        suggested = []
    roster_low = {e["name"].strip().lower() for e in out}
    off_roster = sorted(n for n in scores if n.strip().lower() not in roster_low) if roster_low else []
    return {"ok": True, "roster": out, "pairs": _ss.pairs(rid), "suggested_pairs": suggested,
            "ratings_off_roster": off_roster,
            "choices": {"employment_type": list(_ss.EMPLOYMENT_TYPES), "daypart": list(_ss.DAYPART_CHOICES),
                        "days": list(_ss.DAYS), "certifications": list(_ss.CERTIFICATIONS)}, "can_edit": _may_rate(u)}, 200


def _do_staff_settings_set(u):
    if not _may_rate(u):
        return _forbidden("Your login can view the team but not change their settings.")
    import staff_settings as _ss
    from client_api import log_account_event
    b = _body()
    try:
        row = _ss.upsert(_rid(u), b.get("employee_name") or b.get("name"),
                         active=b.get("active"), employment_type=b.get("employment_type"),
                         min_hours=b.get("min_hours"), max_hours=b.get("max_hours"),
                         daypart_availability=b.get("daypart_availability"), is_minor=b.get("is_minor"),
                         time_windows=b.get("time_windows"), certifications=b.get("certifications"),
                         preferred_dayparts=b.get("preferred_dayparts"), desired_hours=b.get("desired_hours"),
                         updated_by=_who(u))
    except _ss.StaffSettingsError as e:
        return {"ok": False, "error": str(e)}, 400
    changed = [k for k in ("active", "employment_type", "min_hours", "max_hours", "daypart_availability", "is_minor",
                           "time_windows", "certifications", "preferred_dayparts", "desired_hours") if k in b]
    log_account_event(_rid(u), "staff_settings_changed", current_user=u,
                      detail=f"{row['employee_name']}: {', '.join(changed) or 'no change'}")
    return {"ok": True, "settings": row}, 200


def _do_staff_pairs_set(u):
    if not _may_rate(u):
        return _forbidden("Your login can view the team but not change pairings.")
    import staff_settings as _ss
    b = _body()
    try:
        row = _ss.set_pair(_rid(u), b.get("a"), b.get("b"), (b.get("kind") or "").strip().lower(),
                           note=b.get("note"), created_by=_who(u))
    except _ss.StaffSettingsError as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True, "pair": row, "pairs": _ss.pairs(_rid(u))}, 200


def _do_staff_pair_delete(u, pair_id):
    if not _may_rate(u):
        return _forbidden("Your login can view the team but not change pairings.")
    import staff_settings as _ss
    if not _ss.delete_pair(_rid(u), pair_id):
        return {"ok": False, "error": "That pairing is not yours, or is already gone."}, 404
    return {"ok": True, "pairs": _ss.pairs(_rid(u))}, 200


def _do_demand_signals_get(u):
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see this.")
    import demand_signals as _ds
    from datetime import date, timedelta
    start = (request.args.get("start") or date.today().isoformat())[:10]
    end = (request.args.get("end") or (date.today() + timedelta(days=60)).isoformat())[:10]
    return {"ok": True, "signals": _ds.upcoming(_rid(u), start, end)}, 200


def _do_demand_signals_save(u):
    if not _may_draft(u):
        return _forbidden("Your login can view labor but not change the schedule's inputs.")
    import demand_signals as _ds
    from client_api import log_account_event
    b = _body()
    rows = b.get("rows")
    if not isinstance(rows, list):
        rows = _ds.parse_reservations_csv(b.get("csv") or "")
    if not rows:
        return {"ok": False, "error": "Send rows (date, kind, label, covers or lift) or a CSV of date,covers."}, 400
    out = _ds.save(_rid(u), rows, source=(b.get("source") or "manual"), created_by=_who(u))
    if out["written"]:
        log_account_event(_rid(u), "demand_signals_saved", current_user=u, detail=f"{out['written']} dates")
    return {"ok": True, **out}, 200


def _do_demand_signal_delete(u, signal_id):
    if not _may_draft(u):
        return _forbidden("Your login can view labor but not change the schedule's inputs.")
    import demand_signals as _ds
    if not _ds.delete(_rid(u), signal_id):
        return {"ok": False, "error": "Not found."}, 404
    return {"ok": True}, 200


def _do_compliance_get(u):
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see the rules.")
    import schedule_rules as _sr
    import compliance_packs
    import reservation_feeds
    from models import get_restaurant
    r = get_restaurant(_rid(u))
    rules = _sr.compliance(r)
    pack = rules.pop("_pack", None)
    return {"ok": True, "rules": rules, "defaults": dict(_sr.DEFAULTS),
            "role_floors": _sr.role_floors(r), "can_edit": _principal(u),
            "jurisdiction": getattr(r, "jurisdiction", None), "pack": pack, "packs": compliance_packs.available(),
            "role_arrivals": _sr._load_json(getattr(r, "role_arrival_json", None), {}),
            "role_close_mins": _sr._load_json(getattr(r, "role_close_min_json", None), {}),
            "manager_rule_unusable": bool(rules.get("manager_on_duty")) and not _keyholders(_rid(u)),
            "role_requirements": _sr._load_json(getattr(r, "role_requirements_json", None), {}),
            "foh_roles": _sr._load_json(getattr(r, "foh_roles_json", None), []) or ["Server"],
            "patio_roles": _sr._load_json(getattr(r, "patio_roles_json", None), []),
            "trim_to_budget": bool(int(getattr(r, "trim_to_budget", 1) or 0)),
            "closures": _sr.closures(r),
            "certifications": list(__import__("staff_settings").CERTIFICATIONS),
            "reservation_feed": reservation_feeds.status(r), "reservation_providers": reservation_feeds.available()}, 200


def _do_compliance_set(u):
    if not _principal(u):
        return _forbidden("Only the account owner can change the scheduling rules.")
    import schedule_rules as _sr
    from client_api import log_account_event
    b = _body()
    out = {}
    if isinstance(b.get("rules"), dict):
        out["rules"] = _sr.save_compliance(_rid(u), b["rules"])
    if "role_floors" in b:
        floors = b.get("role_floors") if isinstance(b.get("role_floors"), dict) else {}
        out["role_floors"] = _sr.save_role_floors(_rid(u), floors)
    if "closed_weekdays" in b or "closed_dates" in b:
        cw = b.get("closed_weekdays") if isinstance(b.get("closed_weekdays"), list) else None
        cd = b.get("closed_dates") if isinstance(b.get("closed_dates"), list) else None
        try:
            out["closures"] = _sr.save_closures(_rid(u), closed_weekdays=cw, closed_dates=cd)
        except ValueError as e:
            return {"ok": False, "error": str(e)}, 400
    import json as _j
    from models import update_restaurant
    settings = {}
    if "jurisdiction" in b:
        import compliance_packs
        code = (b.get("jurisdiction") or "").strip().upper()
        if code and code not in compliance_packs.PACKS:
            return {"ok": False, "error": f"No rule pack for {code}."}, 400
        settings["jurisdiction"] = code or None
        out["jurisdiction"] = code or None
    if "role_arrivals" in b and isinstance(b.get("role_arrivals"), dict):
        clean = {}
        for k, v in b["role_arrivals"].items():
            try:
                clean[str(k).strip()[:60]] = max(-240, min(240, int(v)))
            except (TypeError, ValueError):
                continue
        settings["role_arrival_json"] = _j.dumps(clean) if clean else None
        out["role_arrivals"] = clean
    if "role_close_mins" in b and isinstance(b.get("role_close_mins"), dict):
        clean = {}
        for k, v in b["role_close_mins"].items():
            try:
                clean[str(k).strip()[:60]] = max(0, min(240, int(v)))
            except (TypeError, ValueError):
                continue
        settings["role_close_min_json"] = _j.dumps(clean) if clean else None
        out["role_close_mins"] = clean
    if "role_requirements" in b and isinstance(b.get("role_requirements"), dict):
        clean = {str(k).strip()[:60]: sorted({str(x).strip().lower()[:40] for x in (v or []) if str(x).strip()})
                 for k, v in b["role_requirements"].items() if str(k).strip()}
        clean = {k: v for k, v in clean.items() if v}
        settings["role_requirements_json"] = _j.dumps(clean) if clean else None
        out["role_requirements"] = clean
    for key, col in (("foh_roles", "foh_roles_json"), ("patio_roles", "patio_roles_json")):
        if key in b and isinstance(b.get(key), list):
            clean = sorted({str(x).strip()[:60] for x in b[key] if str(x).strip()})
            settings[col] = _j.dumps(clean) if clean else None
            out[key] = clean
    if "trim_to_budget" in b:
        settings["trim_to_budget"] = 1 if b.get("trim_to_budget") else 0
        out["trim_to_budget"] = bool(b.get("trim_to_budget"))
    if "reservation_provider" in b or "reservation_api_key" in b:
        import reservation_feeds
        prov = (b.get("reservation_provider") or "").strip().lower()
        if prov and prov not in reservation_feeds.PROVIDERS:
            return {"ok": False, "error": f"{prov} is not a reservation system this build knows."}, 400
        settings["reservation_provider"] = prov or None
        if "reservation_api_key" in b:
            settings["reservation_api_key"] = (b.get("reservation_api_key") or "").strip()[:200] or None
        out["reservation_provider"] = prov or None
    if settings:
        update_restaurant(_rid(u), settings)
        if "reservation_provider" in settings:
            import reservation_feeds
            from models import get_restaurant
            out["reservation_feed"] = reservation_feeds.status(get_restaurant(_rid(u)))
    if not out:
        return {"ok": False, "error": "Send rules, role_floors, or a setting."}, 400
    log_account_event(_rid(u), "schedule_rules_changed", current_user=u, detail=", ".join(out))
    return {"ok": True, **out}, 200


def _do_schedule_versions(u, history_id):
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see schedules.")
    import schedule_versions as _sv
    return {"ok": True, "versions": _sv.list_versions(_rid(u), history_id),
            "draft_vs_published": _sv.draft_vs_published(_rid(u), history_id)}, 200


def _rows_from_body(b):
    cols = ("date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes")
    raw = b.get("rows")
    if not isinstance(raw, list) or not raw or len(raw) > 2000:
        return None
    return [{c: str(r.get(c) or "")[:200] for c in cols} for r in raw if isinstance(r, dict)]


def _do_schedule_violations(u):
    """Every rule the rows on screen break — for the review panel after
    an edit, so the owner never publishes on a stale verdict."""
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can check a schedule.")
    import schedule_rules as _sr
    from schedule_engine import quality_inputs_from_db
    b = _body()
    rows = _rows_from_body(b)
    if not rows:
        return {"ok": False, "error": "rows required"}, 400
    inputs = quality_inputs_from_db(_rid(u), week_rows=rows)
    c = inputs.get("constraints")
    if c is None:
        return {"ok": True, "violations": [], "review": _sr.summarize([])}, 200
    if inputs.get("roster"):
        c.active = {n.lower() for n in inputs["roster"]}
        c.roster_names = list(inputs["roster"])
    viols = _sr.violations(rows, c)
    out = {"ok": True, "violations": viols, "review": _sr.summarize(viols),
           "pending_time_off": inputs.get("pending_time_off") or {}}
    # What the edit moves in hours and overtime-priced dollars, when the
    # page sends the rows it started from.
    base = _rows_from_body({"rows": b.get("baseline_rows")}) if isinstance(b.get("baseline_rows"), list) else None
    if base is not None:
        try:
            import schedule_economics as _econ
            from models import get_role_rates
            rates = get_role_rates(_rid(u))
            out["cost"] = _econ.cost_delta(base, rows, rates, (rates or {}).get("_default"),
                                           ceiling=c.compliance.get("weekly_hours_ceiling") or 40)
        except Exception:
            out["cost"] = None
    return out, 200


def _do_schedule_apply_fixes(u):
    """Put somebody legal on every row that breaks a hard rule, re-score,
    and hand the rows back — the owner still decides whether to save."""
    if not _may_draft(u):
        return _forbidden("Your login can view labor but not change the schedule.")
    import schedule_rules as _sr
    import shift_quality as _sq
    from schedule_engine import quality_inputs_from_db, _quality_signals, _score_schedule_quality
    rows = _rows_from_body(_body())
    if not rows:
        return {"ok": False, "error": "rows required"}, 400
    inputs = quality_inputs_from_db(_rid(u), week_rows=rows)
    c = inputs.get("constraints")
    if c is None:
        return {"ok": False, "error": "The rules could not be loaded for this week."}, 500
    if inputs.get("roster"):
        c.active = {n.lower() for n in inputs["roster"]}
        c.roster_names = list(inputs["roster"])
    viols = _sr.violations(rows, c)
    signals, weights = _quality_signals(_rid(u), inputs)
    out = _sq.apply_fixes(rows, [v for v in viols if v["hard"]], profiles=inputs.get("shift_profiles") or None,
                          weights=weights, **signals)
    fixed_rows = out["rows"]
    after = _sr.violations(fixed_rows, c)
    quality, what_if = _score_schedule_quality(_rid(u), fixed_rows, inputs)
    return {"ok": True, "rows": fixed_rows, "fixes": out["fixes"], "unfixed": out["unfixed"],
            "violations": after, "review": _sr.summarize(after), "quality": quality, "what_if": what_if}, 200


def _do_shift_requests_list(u):
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see shift requests.")
    import shift_requests as _sq_req
    return {"ok": True, "requests": _sq_req.for_manager(_rid(u)), "open": _sq_req.open_shifts(_rid(u))}, 200


def _do_shift_request_decide(u, request_id):
    if not _may_draft(u):
        return _forbidden("Your login can view labor but not decide shift requests.")
    import shift_requests as _sq_req
    from client_api import log_account_event
    b = _body()
    decision = (b.get("decision") or "").strip().lower()
    if decision not in ("approve", "deny"):
        return {"ok": False, "error": "decision must be approve or deny."}, 400
    try:
        row = _sq_req.decide(_rid(u), request_id, decision == "approve", decided_by=_who(u),
                             replacement=(b.get("replacement") or "").strip() or None)
    except _sq_req.ShiftRequestError as e:
        return {"ok": False, "error": str(e)}, 400
    if not row:
        return {"ok": False, "error": "That request was already answered, or is not yours."}, 404
    log_account_event(_rid(u), "shift_request_decided", current_user=u,
                      detail=f"{row['employee_name']} {row['date']} {row['shift_start']}: {row['status']}")
    return {"ok": True, "request": row}, 200


def _keyholders(rid) -> bool:
    """Whether anybody can satisfy a manager-on-duty rule here."""
    try:
        import schedule_rules as _sr
        c = _sr.build_constraints(rid, [], [])
        return bool(c.keyholders)
    except Exception:
        return False


def _do_learned_patterns(u):
    """What the draft has learned from the manager's edits, with the
    owner's say: each one can be dismissed or restored."""
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see this.")
    import schedule_versions as _sv
    import schedule_intel as _si
    rid = _rid(u)
    dismissed = _si.dismissed_patterns(rid)
    out = []
    for p in _sv.learned_patterns(rid, min_repeats=1):
        key = _si.pattern_key(p)
        out.append({**p, "key": key, "active": p["times"] >= 2 and key not in dismissed, "dismissed": key in dismissed})
    return {"ok": True, "patterns": out, "can_edit": _may_draft(u)}, 200


def _do_learned_pattern_set(u):
    if not _may_draft(u):
        return _forbidden("Your login can view labor but not change what the draft learns.")
    import schedule_intel as _si
    b = _body()
    key = (b.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "key required"}, 400
    if b.get("dismissed", True):
        _si.dismiss_pattern(_rid(u), key, actor=_who(u))
    else:
        _si.restore_pattern(_rid(u), key)
    return {"ok": True, "key": key, "dismissed": bool(b.get("dismissed", True))}, 200


def _do_recommendation_event(u):
    """The owner accepted or dismissed a recommendation — the ledger that
    decides which kinds keep being shown."""
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can do this.")
    import schedule_intel as _si
    b = _body()
    action = (b.get("action") or "").strip().lower()
    if action not in ("accepted", "dismissed"):
        return {"ok": False, "error": "action is accepted or dismissed"}, 400
    _si.record_recommendation(_rid(u), (b.get("kind") or "other")[:60], b.get("key") or "", action, actor=_who(u))
    return {"ok": True, "suppressed_kinds": sorted(_si.suppressed_kinds(_rid(u)))}, 200


def _do_schedule_intel(u):
    """The record behind the draft: outcomes by daypart, the rotation
    ledger, what staff keep dropping and claiming, who could hold a
    station, and pairs the record suggests."""
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see this.")
    import schedule_intel as _si
    import schedule_economics as _econ
    rid = _rid(u)
    def _safe(fn, default):
        try:
            return fn()
        except Exception:
            return default
    mentored = _safe(lambda: _si.mentoring(rid), {})
    return {"ok": True,
            "outcomes": _safe(lambda: _si.outcomes_by_daypart(rid), {}),
            "ledger": _safe(lambda: _si.fairness_ledger(rid), {}),
            "behaviour": _safe(lambda: _si.behaviour_preferences(rid), {}),
            "could_hold": _si.could_hold(mentored), "mentored": mentored,
            "suggested_pairs": _safe(lambda: _si.chemistry_suggestions(rid), []),
            "splh": _safe(lambda: _econ.splh_by_daypart(rid), {}),
            "revenue": _safe(lambda: _econ.projected_weekly_revenue(rid), {}),
            "suppressed_recommendation_kinds": sorted(_safe(lambda: _si.suppressed_kinds(rid), set()))}, 200


def _do_reservation_sync(u):
    if not _principal(u):
        return _forbidden("Only the account owner can sync the reservation feed.")
    import reservation_feeds
    res = reservation_feeds.sync(_rid(u))
    return {"ok": not res.get("error"), **res}, (200 if not res.get("error") else 400)


def _do_recipe_scan(u):
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can add recipes.")
    import recipes
    f = request.files.get("file")
    if not f:
        return {"ok": False, "error": "Attach a photo of the recipe card."}, 400
    if _limited(u, "recipe_scan", 10, 600):
        return _SLOW_DOWN
    data = f.read()
    media_type = (f.mimetype or "").lower()
    if media_type == "image/jpg":
        media_type = "image/jpeg"
    try:
        draft = recipes.extract_from_image(_rid(u), data, media_type, user_id=u.get("id"))
    except (recipes.RecipePhotoError, ValueError) as e:
        return {"ok": False, "error": str(e)}, 400
    except Exception as e:
        from ai_utils import AIBudgetExceeded
        if isinstance(e, AIBudgetExceeded):
            return {"ok": False, "error": str(e)}, 429
        import ops
        ops.capture(e, job="recipe_scan", context=f"restaurant_id={_rid(u)}")
        return {"ok": False, "error": "The card couldn't be read right now. Try again shortly."}, 502
    return {"ok": True, "draft": draft}, 200


def _do_post_tags(u, post_id):
    """Correct what a post was about. Only a menu item of this restaurant,
    only a known occasion or kind; anything else is refused, not guessed."""
    import marketing_tags
    from models import get_conn
    b = _body()
    overrides = {}
    if "menu_item_id" in b:
        try:
            overrides["menu_item_id"] = int(b["menu_item_id"]) if b["menu_item_id"] not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            return {"ok": False, "error": "menu_item_id must be a number."}, 400
    if "occasion" in b:
        occ = (b.get("occasion") or "").strip().lower() or None
        if occ and occ not in marketing_tags.OCCASIONS:
            return {"ok": False, "error": f"occasion must be one of {', '.join(marketing_tags.OCCASIONS)}."}, 400
        overrides["occasion"] = occ
    if "post_kind" in b:
        kind = (b.get("post_kind") or "").strip().lower() or None
        if kind and kind not in marketing_tags.KINDS:
            return {"ok": False, "error": f"post_kind must be one of {', '.join(marketing_tags.KINDS)}."}, 400
        overrides["post_kind"] = kind
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, topic FROM marketing_content_log WHERE id=? AND restaurant_id=?", (int(post_id), _rid(u))).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "Post not found."}, 404
    # An explicit "no item" must clear inference too, so it is passed as a
    # sentinel the tagger treats as set.
    tags = marketing_tags.tag_row(row["id"], _rid(u), row["topic"], overrides=overrides, clear_item=("menu_item_id" in overrides and overrides["menu_item_id"] is None))
    tags["label"] = marketing_tags.label(tags)
    return {"ok": True, "tags": tags}, 200


def _do_ai_visibility_queries(u):
    from models import ai_visibility_query_history
    return {"ok": True, **ai_visibility_query_history(_rid(u))}, 200


def _do_marketing_diagnosis(u):
    import guest_marketing
    return {"ok": True, "diagnosis": guest_marketing.diagnose(_rid(u))}, 200


def _do_memory_list(u):
    """What Ask Cavnar remembers about this restaurant, with who added it —
    so the owner can read and correct the memory that shapes every answer."""
    from models import get_ask_memory
    return {"ok": True, "facts": get_ask_memory(_rid(u))}, 200


def _do_memory_forget(u):
    from models import forget_ask_fact
    from client_api import log_account_event
    fact = (_body().get("fact") or "").strip()
    if not fact:
        return {"ok": False, "error": "Which fact?"}, 400
    ok = forget_ask_fact(_rid(u), fact)
    if ok:
        log_account_event(_rid(u), "memory_forgotten", current_user=u, detail=fact[:120])
    return ({"ok": True} if ok else {"ok": False, "error": "No fact like that."}), (200 if ok else 404)


def _do_delayed_pending(u):
    import delayed
    return {"ok": True, "actions": delayed.pending(_rid(u))}, 200


def _do_delayed_cancel(u, action_id):
    import delayed
    from client_api import log_account_event
    ok = delayed.cancel(_rid(u), int(action_id), actor=u)
    if ok:
        log_account_event(_rid(u), "delayed_action_cancelled", current_user=u, detail=f"#{action_id}")
    return ({"ok": True} if ok else {"ok": False, "error": "That already went out, or was already undone."}), (200 if ok else 409)


# ── principal-only ────────────────────────────────────────────────────────────

def _loss_viewer(u):
    from permissions import has_permission, LOSS_VIEW
    return has_permission(u, LOSS_VIEW)


def _do_loss_signals(u):
    if not _loss_viewer(u):
        return _forbidden("Comps & voids are visible to the owner, or to a manager the owner has "
                          "given access.")
    import loss_detection
    return {"ok": True, **loss_detection.signals(_rid(u))}, 200


def _do_cross_module(u):
    """What two modules saw that neither could see alone.

    `business_intelligence.correlations()` has existed since audit #15 and
    had exactly one consumer — `executive_brief`, which had no route. So the
    single finding this platform can produce that no single-module tool can
    ("your Friday complaints fall on your leanest Friday") reached an owner
    only as one compressed line in the morning brief, or if the Ask model
    happened to choose the tool. This is that surface.

    Returns [] often and deliberately. `correlations` cannot manufacture a
    link — both sides must already have cleared their own module's floor —
    so an empty list means the modules genuinely do not agree on anything,
    which is the common and correct outcome.
    """
    import business_intelligence as bi
    from models import get_restaurant
    from ask_cavnar_tools import viewer_restaurant
    # Built from what THIS login may see, the same way the brief is. A
    # manager without FOOD_COST_VIEW must not read a margin link.
    r = viewer_restaurant(get_restaurant(_rid(u)), u)
    brief = bi.executive_brief(_rid(u), restaurant=r)
    # Every card that carries a question carries the question to ask. The
    # web phrased this in JS and iOS had no affordance at all; one string
    # from here means both surfaces ask Cavnar the same thing.
    links = [dict(l, ask=f"Tell me more about this: {l.get('headline', '')}")
             for l in (brief.get("links") or [])]
    return {"ok": True,
            "links": links,
            "fix_first": brief.get("fix_first"),
            "modules_consulted": brief.get("modules_consulted") or [],
            "modules_off": brief.get("modules_off") or [],
            "unanswered": brief.get("unanswered") or []}, 200


def _do_good_news(u):
    """Records, streaks and complaints that stopped.

    The counterpart to /loss-signals: everything else in this product looks
    for trouble. Filtered by the same rule as goals and outcomes — a manager
    without FOOD_COST_VIEW is not congratulated on a margin record.
    """
    import good_news
    from ask_cavnar_tools import viewer_restaurant
    from models import get_restaurant
    # Every module this login may not see, not just food cost. An earlier
    # version denied {"inventory"} alone, which left a manager without the
    # Labor permission reading sales and labor-percentage records — the
    # same class of leak the ROI audit found in /api/value, where the
    # breakdown was filtered and the headline total was not.
    #
    # viewer_restaurant is the single answer to "what may this login see",
    # and it fails closed: a role lookup that errors hides everything.
    view = viewer_restaurant(get_restaurant(_rid(u)), u)
    denied = set(getattr(view, "_ask_denied", frozenset()))
    items = good_news.all_good_news(_rid(u), today=_local_today(u),
                                    restaurant=view, denied_modules=denied)
    items = [dict(i, ask=f"What's behind this: {i.get('headline', '')}") for i in items]
    return {"ok": True, "items": items, "caveat": good_news.CAVEAT}, 200


# ── self-serve pause ──────────────────────────────────────────────────────────
# The only path off the product was an email to Will asking to cancel. A
# pause is the smaller decision an owner can make for themselves: Stripe
# collection stops (pause_collection, behaviour "void" — nothing is invoiced
# and nothing accrues), the account moves to the product's existing "paused"
# state, and Stripe resumes collection on the date chosen. Data keeps
# flowing while paused — reviews are fetched on reviews_live, not billing —
# so the day they come back the brief is current, not a month stale.

PAUSE_DAYS = (14, 30, 60)


def _stripe_client():
    import os
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key:
        return None
    import stripe as _stripe
    _stripe.api_key = key
    return _stripe


def _active_subscription(stripe_mod, customer_id):
    for status in ("active", "trialing", "past_due"):
        subs = stripe_mod.Subscription.list(customer=customer_id, status=status, limit=3)
        if subs.data:
            return subs.data[0]
    return None


def _do_pause(u):
    if not _principal(u):
        return _forbidden("Only the account owner can pause the subscription.")
    from datetime import timedelta
    from models import get_restaurant, update_restaurant
    from client_api import log_account_event
    b = _body()
    try:
        days = int(b.get("days") or 30)
    except (TypeError, ValueError):
        return {"ok": False, "error": "days must be a whole number"}, 400
    if days not in PAUSE_DAYS:
        return {"ok": False, "error": f"Pick {', '.join(str(d) for d in PAUSE_DAYS[:-1])} or {PAUSE_DAYS[-1]} days."}, 400
    rid = _rid(u)
    r = get_restaurant(rid)
    if not r:
        return {"ok": False, "error": "Restaurant not found"}, 404
    if (r.billing_status or "").lower() == "paused":
        return {"ok": False, "error": "Already paused."}, 409
    # In the restaurant's own clock, so "paused until 10/20" is their
    # 10/20 — and Stripe resumes at that local moment, not a UTC one.
    from time_utils import restaurant_now
    resumes = restaurant_now(r) + timedelta(days=days)
    try:
        stripe_mod = _stripe_client()
    except Exception as e:
        # A key without the library (or the reverse) must not pause the
        # product while Stripe keeps collecting.
        import ops
        ops.capture(e, job="pause_subscription", context=f"restaurant_id={rid}")
        return {"ok": False, "error": "Stripe isn't reachable from this server — nothing changed. Reply to Will."}, 502
    if stripe_mod and r.stripe_customer_id:
        try:
            sub = _active_subscription(stripe_mod, r.stripe_customer_id)
            if not sub:
                return {"ok": False, "error": "No active subscription to pause — reply to Will."}, 409
            stripe_mod.Subscription.modify(
                sub.id, pause_collection={"behavior": "void", "resumes_at": int(resumes.timestamp())})
        except Exception as e:
            import ops
            ops.capture(e, job="pause_subscription", context=f"restaurant_id={rid}")
            return {"ok": False, "error": "Stripe did not accept the pause — nothing changed. Reply to Will."}, 502
    else:
        # No Stripe on this account (a trial, or a manually billed client):
        # the product pauses; there is no collection to stop.
        pass
    # Every location billed together pauses together, the way the webhook
    # moves siblings — one owner, one subscription, one state.
    try:
        from webhook_routes import _sibling_restaurant_ids
        rids = _sibling_restaurant_ids(rid)
    except Exception:
        rids = [rid]
    for _r in rids:
        update_restaurant(_r, {"billing_status": "paused", "paused_until": resumes.date().isoformat()})
    log_account_event(rid, "subscription_paused", current_user=u, detail=f"{days} days, resumes {resumes.date().isoformat()}")
    try:
        import emails as _emails
        _emails.deliver(email_type="pause_notice_ops", restaurant_id=rid, payload={
            "from": _emails.sender("ops"), "to": [_emails._from_email()],
            "subject": f"Paused: {r.name} — {days} days, resumes {resumes.date().isoformat()}",
            "preheader": "A client paused their own subscription.",
            "html": _emails._branded_email(f"<p>{r.name} paused for {days} days from the app. Stripe resumes "
                                           f"collection on {resumes.date().isoformat()}.</p>")})
    except Exception as e:
        import ops
        ops.capture(e, job="pause_notice", context=f"restaurant_id={rid}")
    return {"ok": True, "paused_until": resumes.date().isoformat(), "days": days}, 200


def _do_resume(u):
    if not _principal(u):
        return _forbidden("Only the account owner can resume the subscription.")
    from models import get_restaurant, update_restaurant
    from client_api import log_account_event
    rid = _rid(u)
    r = get_restaurant(rid)
    if not r or (r.billing_status or "").lower() != "paused":
        return {"ok": False, "error": "Not paused."}, 409
    try:
        stripe_mod = _stripe_client()
    except Exception as e:
        import ops
        ops.capture(e, job="resume_subscription", context=f"restaurant_id={rid}")
        return {"ok": False, "error": "Stripe isn't reachable from this server — reply to Will."}, 502
    if stripe_mod and r.stripe_customer_id:
        try:
            sub = _active_subscription(stripe_mod, r.stripe_customer_id)
            if sub:
                stripe_mod.Subscription.modify(sub.id, pause_collection="")
        except Exception as e:
            import ops
            ops.capture(e, job="resume_subscription", context=f"restaurant_id={rid}")
            return {"ok": False, "error": "Stripe did not accept the resume — reply to Will."}, 502
    try:
        from webhook_routes import _sibling_restaurant_ids
        rids = _sibling_restaurant_ids(rid)
    except Exception:
        rids = [rid]
    for _r in rids:
        update_restaurant(_r, {"billing_status": "active", "paused_until": None})
    log_account_event(rid, "subscription_resumed", current_user=u)
    return {"ok": True}, 200


def _do_pause_status(u):
    from models import get_restaurant
    r = get_restaurant(_rid(u))
    return {"ok": True, "paused": (getattr(r, "billing_status", "") or "").lower() == "paused",
            "paused_until": getattr(r, "paused_until", None), "days": list(PAUSE_DAYS),
            "can_pause": _principal(u)}, 200


def _do_activity(u):
    """What Cavnar AI has been doing for this restaurant — the activity feed
    (activity.py). Filtered by what this login may see, the same way the
    brief and good news are."""
    import activity
    from ask_cavnar_tools import viewer_restaurant
    from models import get_restaurant
    view = viewer_restaurant(get_restaurant(_rid(u)), u)
    denied = set(getattr(view, "_ask_denied", frozenset()))
    return activity.feed(_rid(u), restaurant=view, denied=denied), 200


def _do_monthly_review(u):
    """Last month, read the way an owner reads a P&L — the same build the
    monthly email sends on the 1st, so the screen and the email agree.
    Metrics this login may not see are left out the way goals are."""
    import monthly_review
    from models import get_restaurant
    rid = _rid(u)
    review = monthly_review.build(rid, today=_local_today(u), restaurant=get_restaurant(rid))
    review["metrics"] = [m for m in review["metrics"] if _metric_visible(u, m.get("key"))]
    review["results"] = [r for r in (review.get("results") or []) if _metric_visible(u, r.get("metric"))]
    for m in review["metrics"]:
        m["ask"] = f"What moved my {m['label'].lower()} in {review['month']}?"
    return {"ok": True, "review": review, "headline": monthly_review.headline(review),
            "ask": f"Walk me through {review['month']}",
            "yoy": {m["key"]: monthly_review.yoy_clause(m) for m in review["metrics"]}}, 200


def _do_milestones(u):
    """Moments worth marking, newest first, plus anything not yet shown.

    `unseen` drives the one-time in-app moment; the client marks it seen so
    the promise that it appears once is kept across devices — which the
    localStorage flag it replaces could not do.
    """
    import milestones
    # A savings milestone's body carries whole-business measured dollars,
    # which include food-cost results — so it is owner-level for the same
    # reason /api/value's promise block is. A goal milestone names the
    # metric it was set on, which may be one this login cannot see.
    #
    # An anniversary and "every review answered" carry neither, so a manager
    # still gets the moments that are genuinely theirs rather than an empty
    # screen. Filtering by kind rather than blocking the route is the
    # difference between a permission boundary and a wall.
    money_kinds = {"savings", "goal"}
    allowed = None if _principal(u) else (lambda m: m.get("kind") not in money_kinds)

    def _visible(rows):
        return rows if allowed is None else [m for m in rows if allowed(m)]

    return {"ok": True,
            "items": _visible(milestones.recent(_rid(u), limit=10)),
            "unseen": _visible(milestones.recent(_rid(u), limit=3, unseen_only=True))}, 200


def _do_milestone_seen(u):
    import milestones
    key = (_body().get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "Which milestone?"}, 400
    return {"ok": True, "marked": milestones.mark_seen(_rid(u), key)}, 200


def _do_morning_brief(u):
    """This login's own brief — built from what THEY may see, the same way
    it is delivered to them. The send settings are the restaurant's, so only
    the owner may change them (can_edit)."""
    import morning_brief
    from models import get_restaurant
    r = get_restaurant(_rid(u))
    return {"ok": True,
            "brief": morning_brief.build(_rid(u), restaurant=r, today=_local_today(u), viewer=u),
            "settings": {"enabled": bool(getattr(r, "morning_brief_enabled", 1)),
                         "hour": int(getattr(r, "morning_brief_hour", 7) or 7),
                         "hold_alerts": bool(getattr(r, "alert_hold_during_service", 1)),
                         "briefing_level": getattr(r, "briefing_level", None) or "normal",
                         "preshift_nudge_hour": int(getattr(r, "preshift_nudge_hour", 0) or 0)},
            "can_edit": _principal(u)}, 200


def _do_morning_brief_settings(u):
    if not _principal(u):
        return _forbidden()
    from models import update_restaurant
    import morning_brief
    b = _body()
    fields = {}
    if "enabled" in b:
        fields["morning_brief_enabled"] = 1 if b["enabled"] else 0
    if "hour" in b:
        try:
            hour = int(b["hour"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "hour must be a whole number"}, 400
        if not (4 <= hour < morning_brief.LATEST_SEND_HOUR):
            return {"ok": False, "error": f"Pick an hour between 4 and "
                                          f"{morning_brief.LATEST_SEND_HOUR - 1}."}, 400
        fields["morning_brief_hour"] = hour
    if "hold_alerts" in b:
        fields["alert_hold_during_service"] = 1 if b["hold_alerts"] else 0
    if "briefing_level" in b:
        level = str(b["briefing_level"] or "").lower()
        if level not in ("calm", "normal", "all"):
            return {"ok": False, "error": "briefing_level must be calm, normal or all"}, 400
        fields["briefing_level"] = level
    if "preshift_nudge_hour" in b:
        try:
            nudge = int(b["preshift_nudge_hour"] or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "preshift_nudge_hour must be a whole number"}, 400
        if nudge and not (12 <= nudge <= 20):
            return {"ok": False, "error": "Pick an hour between noon and 8pm, or off."}, 400
        fields["preshift_nudge_hour"] = nudge
    if not fields:
        return {"ok": False, "error": "Nothing to change."}, 400
    update_restaurant(_rid(u), fields)
    return {"ok": True, **fields}, 200


# ── registration: every body on both blueprints ───────────────────────────────

_ROUTES = [
    # (path, methods, body, endpoint)
    ("/issues", ["GET"], _do_issues_list, "issues_list"),
    ("/issues", ["POST"], _do_issue_create, "issue_create"),
    ("/issues/<int:issue_id>/resolve", ["POST"], _do_issue_resolve, "issue_resolve"),
    ("/issues/<int:issue_id>/reassign", ["POST"], _do_issue_reassign, "issue_reassign"),
    ("/issues/routing", ["GET"], _do_routing_get, "issue_routing_get"),
    ("/issues/routing", ["POST"], _do_routing_set, "issue_routing_set"),
    ("/goals", ["GET"], _do_goals_list, "goals_list"),
    ("/goals", ["POST"], _do_goal_set, "goal_set"),
    ("/goals/<int:goal_id>/end", ["POST"], _do_goal_end, "goal_end"),
    ("/outcomes", ["GET"], _do_outcomes_list, "outcomes_list"),
    ("/outcomes", ["POST"], _do_outcome_record, "outcome_record"),
    ("/outcomes/<int:outcome_id>/abandon", ["POST"], _do_outcome_abandon, "outcome_abandon"),
    ("/metrics", ["GET"], _do_metrics, "metrics_list"),
    ("/value", ["GET"], _do_value, "value_summary"),
    ("/food-cost/dish-scorecard", ["GET"], _do_dish_scorecard, "dish_scorecard"),
    ("/food-cost/reprice", ["GET"], _do_reprice, "reprice"),
    ("/food-cost/invoices", ["GET"], _do_invoice_list, "invoice_list"),
    ("/food-cost/invoices", ["POST"], _do_invoice_scan, "invoice_scan"),
    ("/food-cost/invoices/<int:import_id>", ["GET"], _do_invoice_get, "invoice_get"),
    ("/food-cost/invoices/<int:import_id>/apply", ["POST"], _do_invoice_apply, "invoice_apply"),
    ("/actions", ["GET"], _do_actions, "actions_list"),
    ("/actions/snooze", ["POST"], _do_action_snooze, "actions_snooze"),
    ("/closeout", ["GET"], _do_closeout_get, "closeout_get"),
    ("/closeout", ["POST"], _do_closeout_save, "closeout_save"),
    ("/labor/demand", ["GET"], _do_demand, "demand"),
    ("/labor/auto-draft", ["GET"], _do_auto_draft_get, "auto_draft_get"),
    ("/labor/auto-draft", ["POST"], _do_auto_draft_set, "auto_draft_set"),
    ("/loss-signals", ["GET"], _do_loss_signals, "loss_signals"),
    ("/cross-module", ["GET"], _do_cross_module, "cross_module"),
    ("/good-news", ["GET"], _do_good_news, "good_news"),
    ("/milestones", ["GET"], _do_milestones, "milestones_list"),
    ("/monthly-review", ["GET"], _do_monthly_review, "monthly_review"),
    ("/activity", ["GET"], _do_activity, "activity"),
    ("/labor/auto-publish", ["GET"], _do_auto_publish_get, "auto_publish_get"),
    ("/labor/auto-publish", ["POST"], _do_auto_publish_set, "auto_publish_set"),
    ("/food-cost/recipe-drafts", ["GET"], _do_recipe_drafts, "recipe_drafts"),
    ("/food-cost/recipe-drafts/<int:draft_id>/accept", ["POST"], _do_recipe_draft_accept, "recipe_draft_accept"),
    ("/food-cost/recipe-drafts/<int:draft_id>/reject", ["POST"], _do_recipe_draft_reject, "recipe_draft_reject"),
    ("/food-cost/recipes/import", ["POST"], _do_recipes_import, "recipes_import"),
    ("/food-cost/recipes/draft", ["POST"], _do_recipe_draft_now, "recipe_draft_now"),
    ("/labor/weekly-plan", ["GET"], _do_weekly_plan_get, "weekly_plan_get"),
    ("/labor/weekly-plan", ["POST"], _do_weekly_plan_set, "weekly_plan_set"),
    ("/account/send-delay", ["GET"], _do_send_delay_get, "send_delay_get"),
    ("/account/send-delay", ["POST"], _do_send_delay_set, "send_delay_set"),
    ("/food-cost/count-sheet", ["GET"], _do_count_sheet_get, "count_sheet_get"),
    ("/food-cost/count-sheet", ["POST"], _do_count_sheet_save, "count_sheet_save"),
    ("/food-cost/auto-order", ["GET"], _do_auto_order_get, "auto_order_get"),
    ("/food-cost/auto-order", ["POST"], _do_auto_order_set, "auto_order_set"),
    ("/account/memory", ["GET"], _do_memory_list, "memory_list"),
    ("/account/memory/add", ["POST"], _do_memory_add, "memory_add"),
    ("/marketing/posts/<int:post_id>/tags", ["POST"], _do_post_tags, "post_tags"),
    ("/intel/ai-visibility/queries", ["GET"], _do_ai_visibility_queries, "ai_visibility_queries"),
    ("/marketing/diagnosis", ["GET"], _do_marketing_diagnosis, "marketing_diagnosis"),
    ("/labor/time-off", ["GET"], _do_time_off_list, "time_off_list"),
    ("/labor/time-off/<int:request_id>/decide", ["POST"], _do_time_off_decide, "time_off_decide"),
    ("/labor/covers", ["GET"], _do_covers_get, "covers_get"),
    ("/labor/covers", ["POST"], _do_covers_save, "covers_save"),
    ("/labor/roster", ["GET"], _do_roster_get, "roster_get"),
    ("/labor/staff-settings", ["POST"], _do_staff_settings_set, "staff_settings_set"),
    ("/labor/staff-pairs", ["POST"], _do_staff_pairs_set, "staff_pairs_set"),
    ("/labor/staff-pairs/<int:pair_id>", ["DELETE"], _do_staff_pair_delete, "staff_pair_delete"),
    ("/labor/demand-signals", ["GET"], _do_demand_signals_get, "demand_signals_get"),
    ("/labor/demand-signals", ["POST"], _do_demand_signals_save, "demand_signals_save"),
    ("/labor/demand-signals/<int:signal_id>", ["DELETE"], _do_demand_signal_delete, "demand_signal_delete"),
    ("/labor/rules", ["GET"], _do_compliance_get, "schedule_rules_get"),
    ("/labor/rules", ["POST"], _do_compliance_set, "schedule_rules_set"),
    ("/labor/schedule-history/<int:history_id>/versions", ["GET"], _do_schedule_versions, "schedule_versions"),
    ("/labor/schedule/violations", ["POST"], _do_schedule_violations, "schedule_violations"),
    ("/labor/schedule/apply-fixes", ["POST"], _do_schedule_apply_fixes, "schedule_apply_fixes"),
    ("/labor/shift-requests", ["GET"], _do_shift_requests_list, "shift_requests_list"),
    ("/labor/shift-requests/<int:request_id>/decide", ["POST"], _do_shift_request_decide, "shift_request_decide"),
    ("/labor/learned-patterns", ["GET"], _do_learned_patterns, "learned_patterns"),
    ("/labor/learned-patterns", ["POST"], _do_learned_pattern_set, "learned_pattern_set"),
    ("/labor/schedule/recommendation", ["POST"], _do_recommendation_event, "schedule_recommendation_event"),
    ("/labor/intel", ["GET"], _do_schedule_intel, "schedule_intel"),
    ("/labor/reservations/sync", ["POST"], _do_reservation_sync, "reservation_sync"),
    ("/food-cost/recipes/scan", ["POST"], _do_recipe_scan, "recipe_scan"),
    ("/account/trust", ["GET"], _do_trust, "trust"),
    ("/decisions", ["GET"], _do_decisions, "decisions"),
    ("/account/memory/forget", ["POST"], _do_memory_forget, "memory_forget"),
    ("/actions/pending", ["GET"], _do_delayed_pending, "delayed_pending"),
    ("/actions/<int:action_id>/cancel", ["POST"], _do_delayed_cancel, "delayed_cancel"),
    ("/account/pause", ["GET"], _do_pause_status, "pause_status"),
    ("/account/pause", ["POST"], _do_pause, "pause"),
    ("/account/resume", ["POST"], _do_resume, "resume"),
    ("/milestones/seen", ["POST"], _do_milestone_seen, "milestone_seen"),
    ("/morning-brief", ["GET"], _do_morning_brief, "morning_brief"),
    ("/morning-brief/settings", ["POST"], _do_morning_brief_settings, "morning_brief_settings"),
]


def _wrap(body, decorator):
    def view(current_user, **kw):
        payload, status = body(current_user, **kw)
        resp = jsonify(**payload)
        resp.headers["Cache-Control"] = "no-store"
        return resp, status
    view.__name__ = body.__name__
    return decorator(view)


for _path, _methods, _body_fn, _ep in _ROUTES:
    strategy_bp.add_url_rule("/api" + _path, endpoint=_ep, methods=_methods,
                             view_func=_wrap(_body_fn, login_required))
    strategy_mobile_bp.add_url_rule(_path, endpoint=_ep, methods=_methods,
                                    view_func=_wrap(_body_fn, mobile_login_required))


# ── the public issue link ─────────────────────────────────────────────────────

@issue_link_bp.route("/i/<token>", methods=["GET", "POST"])
def issue_page(token):
    """What the assignee's text message opens.

    GET has NO side effect. Messaging apps fetch links to build previews
    (iMessage does it the moment the text lands), so "opening the link
    acknowledges it" would acknowledge every issue before a human saw it,
    and the escalation it exists to trigger would never fire. The assignee
    taps "I'm on it" (action=ack) or "Mark resolved" (action=resolve).
    """
    import issues
    done = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "ack":
            issue = issues.acknowledge(token)
            done = "ack"
        elif action == "resolve":
            issue = issues.resolve_by_token(
                token, note=(request.form.get("note") or "").strip()[:500] or None)
            done = "resolve"
        else:
            abort(400)
    else:
        issue = issues.by_token(token)
    if not issue:
        abort(404)
    resp = render_template("issue.html", issue=issue, token=token, done=done)
    return resp, 200, {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                       "X-Robots-Tag": "noindex"}
