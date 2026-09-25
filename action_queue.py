"""
action_queue.py — one list of what is still open, across every module.

The product could always tell an owner what was wrong. What it could not
tell them was what was still OUTSTANDING: an issue nobody acknowledged, a
reply nobody wrote, an Ask proposal nobody confirmed, an invoice scanned and
never applied, a price never changed, next week's schedule never built.
Each lived on its own screen, so "what's left?" meant opening six.

Two rules:

  EVERY ITEM IS SOMETHING A PERSON CAN FINISH. Nothing goes in here that is
  merely informational — a metric that moved is a brief line, not a task.

  SNOOZE, NEVER DISMISS. "Not today" is an honest answer and the one this
  list has to accept; it takes the item off today's queue and puts it back
  tomorrow. Something that should never come back gets resolved at source
  (resolve the issue, reply to the review, apply the invoice).

Items are filtered by what the reader may see — the same permission line the
routes and the brief draw — so a manager without Food Cost never gets an
invoice or a repricing task.
"""
from datetime import date, timedelta

from models import get_conn, DB_PATH

# What a snooze is worth. Long enough to mean "not today", short enough that
# nothing quietly disappears for a week.
SNOOZE_DAYS = 1
MAX_SNOOZE_DAYS = 14



def reprice_title(x) -> str:
    """The reprice item's title. "Priced below its new cost" only when the
    price IS under the plate cost: a rise that lifted a dish's food-cost %
    is not selling at a loss, and every reprice read that way (NS3 M14)."""
    try:
        below = float(x["sell_price"]) < float(x["plate_cost"])
    except (KeyError, TypeError, ValueError):
        below = False
    if below:
        return f"Reprice {x['dish']} — it is priced below its new cost"
    pct = x.get("food_cost_pct_now")
    return (f"Reprice {x['dish']} — an ingredient rise lifted its food cost to {pct}%" if pct is not None
            else f"Reprice {x['dish']} — an ingredient rise lifted its food cost")

def _snoozed(restaurant_id, today, db_path):
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT key FROM action_snoozes WHERE restaurant_id=? AND until_date > ?",
                            (restaurant_id, today.isoformat())).fetchall()
    finally:
        conn.close()
    return {r["key"] for r in rows}


def _local_midnight_utc(restaurant_id, day_iso, db_path=DB_PATH) -> str:
    """Midnight at the start of `day_iso` in the restaurant's own timezone,
    as the UTC stamp rec_ledger compares against. The queue's snooze ends at
    local midnight (action_snoozes.until_date is a local date); sent as
    "<date> 00:00:00" it was read as UTC and the item came back on Home and
    in the brief about five hours early for a US restaurant."""
    from datetime import datetime, time, timezone
    from time_utils import restaurant_tz
    name = None
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT timezone FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
            name = row["timezone"] if row else None
        finally:
            conn.close()
    except Exception:
        name = None
    local = datetime.combine(date.fromisoformat(day_iso), time.min).replace(tzinfo=restaurant_tz(name))
    return local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# Queue items that are TASKS, not recommendations: an issue someone filed,
# a teammate's shift or time-off request, invoices waiting to be applied.
# Each is finished at its source (resolved, approved or declined, applied);
# nothing about them is advice an owner takes or turns down, and no surface
# offers Done / Not for us on one. Presenting them put a recommendation in
# every acceptance figure that could only ever expire "ignored" — a shift
# request answered in Labor left its episode open (re-audit C7). They are
# listed, snoozable, and never enter the ledger.
TASK_KEY_PREFIXES = ("issue:", "shift_request:", "time_off:", "invoice:")


def is_task(key) -> bool:
    """Whether a queue key is a task (TASK_KEY_PREFIXES), not a
    recommendation. The answer routes use it too: snoozing a task is not an
    answer to a recommendation and has no episode behind it."""
    return str(key or "").startswith(TASK_KEY_PREFIXES)


def snooze(restaurant_id, key, days=SNOOZE_DAYS, user_id=None, db_path=DB_PATH, today=None):
    """Put one item back tomorrow (or up to MAX_SNOOZE_DAYS out).

    action_snoozes keeps one row per key — the latest snooze — so the
    history of how often something was put off lived nowhere. The ledger
    keeps it: every snooze is also a rec_ledger "snoozed" event with its
    `until`, and the same key is quiet on every other surface until then."""
    today = today or date.today()
    days = max(1, min(int(days or SNOOZE_DAYS), MAX_SNOOZE_DAYS))
    until = (today + timedelta(days=days)).isoformat()
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO action_snoozes (restaurant_id, key, until_date, snoozed_by) VALUES (?,?,?,?) "
            "ON CONFLICT(restaurant_id, key) DO UPDATE SET until_date=excluded.until_date, "
            "snoozed_by=excluded.snoozed_by, created_at=datetime('now')",
            (restaurant_id, str(key)[:120], until, user_id))
        conn.commit()
    finally:
        conn.close()
    if is_task(key):
        # A task is not a recommendation: its snooze lives in action_snoozes
        # alone (TASK_KEY_PREFIXES).
        return {"key": key, "until": until}
    try:
        import rec_ledger
        rec_ledger.record(restaurant_id, str(key)[:160], "snoozed", surface="queue", user_id=user_id,
                          meta={"until": until, "days": days},
                          snooze_until=_local_midnight_utc(restaurant_id, until, db_path),
                          db_path=db_path)
    except Exception as e:
        print(f"[action_queue] snooze not recorded in the ledger: {e}")
    return {"key": key, "until": until}


def _sees(viewer, module_key):
    """Whether this reader may see a module's work. None = the owner's full
    view (the scheduler, a test)."""
    if viewer is None:
        return True
    from permissions import MODULE_VIEW_PERMISSIONS, has_permission
    perm = MODULE_VIEW_PERMISSIONS.get(module_key)
    return perm is None or has_permission(viewer, perm)


def items(restaurant_id, viewer=None, db_path=DB_PATH, today=None, restaurant=None):
    """Everything still open, most pressing first.

    Each item: {key, kind, title, detail, severity, module, action, count}.
    `action` names what finishes it — a route the client already has (as
    {"web", "mobile"}, since the two clients have different prefixes) or a
    module to open — never a new mechanism.
    """
    from models import get_restaurant
    today = today or date.today()
    restaurant = restaurant or get_restaurant(restaurant_id)
    out = []

    def add(key, kind, title, severity, action, detail=None, module=None, count=None):
        out.append({"key": key, "kind": kind, "title": title, "detail": detail,
                    "severity": severity, "module": module, "action": action, "count": count})

    # ── issues nobody has picked up ──
    try:
        import issues
        for i in issues.list_issues(restaurant_id, status="unresolved", limit=10, db_path=db_path,
                                    sees_loss=issues.viewer_sees_loss(viewer)):   # A-8
            waiting = i["status"] == "open"
            add(f"issue:{i['id']}", "issue", i["title"],
                "critical" if (waiting and i["severity"] == "high") else
                ("important" if waiting else "watch"),
                # Both halves, like an Ask proposal's route: the web client
                # posts the web path and the app the mobile one. A single
                # path 404s on whichever client it wasn't written for.
                {"label": "Resolve", "method": "POST",
                 "route": {"web": f"/api/issues/{i['id']}/resolve",
                           "mobile": f"/mobile/api/issues/{i['id']}/resolve"}},
                detail=(f"{i['assignee_name']} has it" if i.get("assignee_name") else "Unassigned")
                       + (", not acknowledged yet" if waiting else ""),
                module="issues")
    except Exception:
        pass

    # ── replies the guests are waiting on ──
    # Only reviews from the last REPLY_OWED_MAX_AGE_DAYS: connecting Google
    # imports years of history, which is not a reply anyone owes today.
    if getattr(restaurant, "module_reviews", 0) and _sees(viewer, "reviews"):
        from thresholds import REPLY_OWED_MAX_AGE_DAYS
        conn = get_conn(db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, SUM(rating <= 2) AS bad FROM reviews WHERE restaurant_id=? "
                "AND deleted_at IS NULL AND response_status IN ('pending','drafted') "
                "AND COALESCE(NULLIF(review_date,''), fetched_at) >= date('now', ?)",
                (restaurant_id, f"-{int(REPLY_OWED_MAX_AGE_DAYS)} days")).fetchone()
        finally:
            conn.close()
        if row and row["n"]:
            add("no_response", "reviews",
                f"{row['n']} review{'' if row['n'] == 1 else 's'} from the last 30 days waiting on a reply",
                # A guest who left 1-2 stars this month is waiting: Home calls
                # that critical, and so does the queue (it is never deduped).
                "critical" if (row["bad"] or 0) else "watch",
                {"label": "Open Reviews", "module": "reviews"},
                detail=(f"{row['bad']} at 2 stars or worse" if row["bad"] else None),
                module="reviews", count=int(row["n"]))

    # ── things Cavnar proposed and nobody answered ──
    try:
        conn = get_conn(db_path)
        try:
            # Confirming or dismissing writes its OWN row. A proposal is
            # settled by a row that names it (proposal_id) — or, from an
            # older client that sent no id, by any later answer to the same
            # action. Grouping by action + summary used to merge two
            # different proposals into one item and settle both with one
            # answer; each proposal is its own item now, keyed "ask:<id>"
            # exactly as rec_ledger and Ask key it.
            rows = conn.execute(
                "SELECT p.id, p.action, p.summary, p.created_at AS at FROM ask_cavnar_actions p "
                "WHERE p.restaurant_id=? AND p.outcome='proposed' "
                "AND p.created_at >= datetime('now','-7 days') "
                "AND NOT EXISTS (SELECT 1 FROM ask_cavnar_actions s WHERE s.restaurant_id=p.restaurant_id "
                "                AND s.outcome!='proposed' AND ("
                "                    s.proposal_id = p.id OR "
                "                    (s.proposal_id IS NULL AND s.action=p.action AND s.created_at >= p.created_at))) "
                "ORDER BY p.created_at DESC, p.id DESC LIMIT 20", (restaurant_id,)).fetchall()
        finally:
            conn.close()
        seen_summaries = set()
        for r in rows:
            # The same sentence proposed twice is one thing to answer: keep
            # the newest.
            label = (r["summary"] or r["action"].replace("_", " ")).strip()
            if label.lower() in seen_summaries:
                continue
            seen_summaries.add(label.lower())
            add(f"ask:{r['id']}", "proposal", label, "watch",
                # Opens THIS proposal: Ask is asked about it by name.
                {"label": "Open it", "module": "ask", "proposal_id": r["id"],
                 "ask": f"Show me the proposal you made: {label}"},
                detail="Proposed, never confirmed or dismissed", module="ask")
            if len(seen_summaries) >= 5:
                break
    except Exception as e:
        print(f"[action_queue] proposals unavailable: {e}")

    # ── money left on the table in Food Cost ──
    if getattr(restaurant, "module_inventory", 0) and _sees(viewer, "inventory"):
        try:
            import invoices
            # Includes a trusted supplier's invoice with lines left for a
            # person, and the action opens the invoice itself - it used to
            # open Food Cost's empty invoice card (friction audit U2-2).
            pending = invoices.pending_imports(restaurant_id, db_path=db_path)
            if pending:
                import nav as _nav
                add("invoice:pending", "invoice",
                    f"{len(pending)} scanned invoice{'' if len(pending) == 1 else 's'} not applied",
                    "important", {"label": "Review costs", "module": "inventory",
                                  "nav": (_nav.path("invoice", pending[0]["id"]) if len(pending) == 1
                                          else _nav.path("inventory", "invoices"))},
                    detail="Ingredient costs still say the old price", module="inventory",
                    count=len(pending))
        except Exception:
            pass
        try:
            import menu_intelligence
            sg = [x for x in (menu_intelligence.reprice_suggestions(restaurant_id, db_path=db_path)
                              .get("suggestions") or []) if (x.get("monthly_margin_lost") or 0) >= 25]
            # One item per dish, each with its own key: a single "reprice"
            # key meant snoozing one dish snoozed every dish. The action is
            # the one-tap apply at the suggested price.
            for x in sg[:3]:
                act = ({"label": f"Reprice to ${x['suggested_price']:.2f}", "method": "POST",
                        "route": {"web": "/api/food-cost/reprice/apply",
                                  "mobile": "/mobile/api/food-cost/reprice/apply"},
                        "body": {"dish": x["dish"], "price": x["suggested_price"]}}
                       if x.get("suggested_price") else {"label": "Open Food Cost", "module": "inventory"})
                add(menu_intelligence.reprice_key(x['dish']), "reprice", reprice_title(x),
                    "watch", act,
                    detail=f"about ${x['monthly_margin_lost']:,.0f}/month of margin at today's price",
                    module="inventory")
        except Exception:
            pass

    # ── what the team is waiting on ──
    # Requests nobody answered and a drafted week staff do not have yet —
    # the same things strategy_jobs.labor_waiting reminds the manager of at
    # 9am, here as things to finish, each with its own key.
    if getattr(restaurant, "module_labor", 0) and _sees(viewer, "labor"):
        from time_utils import mdy
        conn = get_conn(db_path)
        try:
            try:
                reqs = conn.execute(
                    "SELECT id, employee_name, date, shift_start, kind FROM shift_change_requests "
                    "WHERE restaurant_id=? AND status='pending' AND date >= ? ORDER BY date, shift_start LIMIT 10",
                    (restaurant_id, today.isoformat())).fetchall()
            except Exception:
                reqs = []
            try:
                offs = conn.execute(
                    "SELECT id, employee_name, start_date, end_date FROM staff_time_off "
                    "WHERE restaurant_id=? AND status='pending' AND end_date >= ? ORDER BY start_date LIMIT 10",
                    (restaurant_id, today.isoformat())).fetchall()
            except Exception:
                offs = []
            try:
                unsent = conn.execute(
                    "SELECT h.id, h.week_start FROM schedule_history h WHERE h.restaurant_id=? "
                    "AND h.week_start >= ? AND h.week_start <= ? AND h.published_at IS NULL "
                    "AND h.superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history p "
                    "WHERE p.restaurant_id=h.restaurant_id AND p.week_start=h.week_start "
                    "AND p.published_at IS NOT NULL) ORDER BY h.id DESC LIMIT 1",
                    (restaurant_id, today.isoformat(), (today + timedelta(days=3)).isoformat())).fetchone()
            except Exception:
                unsent = None
        finally:
            conn.close()
        for r in reqs:
            what = "swap" if (r["kind"] or "") == "swap" else "drop"
            add(f"shift_request:{r['id']}", "shift_request",
                f"{r['employee_name']} asked to {what} {mdy(r['date'])} {r['shift_start'] or ''}".rstrip(),
                "important", {"label": "Answer it", "module": "labor"},
                detail="Waiting on your answer", module="labor")
        for r in offs:
            add(f"time_off:{r['id']}", "time_off",
                f"{r['employee_name']} asked for time off from {mdy(r['start_date'])}",
                "important", {"label": "Answer it", "module": "labor"},
                detail=f"Through {mdy(r['end_date'])}", module="labor")
        if unsent:
            add(f"schedule_unsent:{unsent['id']}", "schedule",
                f"The week of {mdy(unsent['week_start'])} is drafted but staff don't have it",
                "critical", {"label": "Send now", "module": "labor", "history_id": unsent["id"]},
                detail="It starts within three days", module="labor")

    # ── next week's schedule ──
    if getattr(restaurant, "module_labor", 0) and _sees(viewer, "labor") and today.weekday() >= 3:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT 1 FROM schedule_history WHERE restaurant_id=? AND "
                               "generated_at >= datetime('now','-5 days') LIMIT 1",
                               (restaurant_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            add("schedule:next-week", "schedule", "Next week's schedule isn't built",
                "important", {"label": "Build it", "module": "labor"}, module="labor")

    hidden = _snoozed(restaurant_id, today, db_path)
    rank = {"critical": 0, "important": 1, "watch": 2}
    live = [i for i in out if i["key"] not in hidden]
    # One "no" everywhere: an answer given on Home, in the brief or anywhere
    # else silences the same key here (rec_ledger.silenced_keys). Issues are
    # finished at source, so they are never silenced by a recommendation.
    try:
        import rec_ledger
        silenced = rec_ledger.silenced_keys(restaurant_id, db_path=db_path)
    except Exception:
        silenced = set()
    answered = [i for i in live if i["kind"] != "issue" and i["key"] in silenced]
    live = [i for i in live if i not in answered]
    # One piece of news once a day: what Home or the brief already said today
    # drops out of the queue, unless it is critical.
    try:
        import decisions
        seen = decisions.shown_elsewhere_today(
            restaurant_id, [i["key"] for i in live if i["kind"] != "issue"], "queue", db_path=db_path)
    except Exception:
        seen = set()
    shown = [i for i in live if i["key"] in seen and i["severity"] != "critical"]
    live = [i for i in live if i not in shown]
    live.sort(key=lambda i: rank.get(i["severity"], 3))
    # The contract fields every payload carrying a recommendation shares
    # (API_REFERENCE.md → Recommendation fields): a task carries no key and
    # `answerable` false; a recommendation its key and whether Done / Not
    # for us apply. Only recommendations are presented.
    import rec_delivery
    for i in live:
        if is_task(i["key"]):
            i["answerable"] = False
        else:
            i["rec_key"] = i["key"]
            i["answerable"] = rec_delivery.answerable(i["key"])
    rec_delivery.present_now(restaurant_id, "queue", [
        {"key": i["key"], "module": _LEDGER_MODULE.get(i["module"], "ops"), "title": i["title"],
         "position": n} for n, i in enumerate(live) if not is_task(i["key"])],
        user_id=(viewer or {}).get("id"), db_path=db_path)
    return {"items": live, "snoozed": len(out) - len(live) - len(shown) - len(answered),
            "shown_elsewhere": len(shown),
            "note": ("Everything still open, across every module. Snoozing puts an item back "
                     "tomorrow — it never goes away on its own.")}


_LEDGER_MODULE = {"reviews": "reviews", "inventory": "food", "labor": "labor", "ask": "ask", "issues": "ops"}
