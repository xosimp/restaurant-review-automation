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


def _snoozed(restaurant_id, today, db_path):
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT key FROM action_snoozes WHERE restaurant_id=? AND until_date > ?",
                            (restaurant_id, today.isoformat())).fetchall()
    finally:
        conn.close()
    return {r["key"] for r in rows}


def snooze(restaurant_id, key, days=SNOOZE_DAYS, user_id=None, db_path=DB_PATH, today=None):
    """Put one item back tomorrow (or up to MAX_SNOOZE_DAYS out)."""
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
        for i in issues.list_issues(restaurant_id, status="unresolved", limit=10, db_path=db_path):
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
    if getattr(restaurant, "module_reviews", 0) and _sees(viewer, "reviews"):
        conn = get_conn(db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, SUM(rating <= 2) AS bad FROM reviews WHERE restaurant_id=? "
                "AND deleted_at IS NULL AND response_status IN ('pending','drafted')",
                (restaurant_id,)).fetchone()
        finally:
            conn.close()
        if row and row["n"]:
            add("reviews:waiting", "reviews",
                f"{row['n']} review{'' if row['n'] == 1 else 's'} waiting on a reply",
                "important" if (row["bad"] or 0) else "watch",
                {"label": "Open Reviews", "module": "reviews"},
                detail=(f"{row['bad']} at 2 stars or worse" if row["bad"] else None),
                module="reviews", count=int(row["n"]))

    # ── things Cavnar proposed and nobody answered ──
    try:
        conn = get_conn(db_path)
        try:
            # Confirming or dismissing writes its OWN row rather than
            # updating the proposal, so "still proposed" means no later row
            # settled it — without this the queue kept asking about
            # something the owner had already confirmed.
            rows = conn.execute(
                "SELECT p.action, p.summary, MAX(p.created_at) AS at FROM ask_cavnar_actions p "
                "WHERE p.restaurant_id=? AND p.outcome='proposed' "
                "AND p.created_at >= datetime('now','-7 days') "
                "AND NOT EXISTS (SELECT 1 FROM ask_cavnar_actions s WHERE s.restaurant_id=p.restaurant_id "
                "                AND s.action=p.action AND s.outcome!='proposed' "
                "                AND s.created_at >= p.created_at) "
                "GROUP BY p.action, p.summary ORDER BY at DESC LIMIT 5", (restaurant_id,)).fetchall()
        finally:
            conn.close()
        for r in rows:
            add(f"proposal:{r['action']}", "proposal", r["summary"] or r["action"].replace("_", " "),
                "watch", {"label": "Open Ask", "module": "ask"},
                detail="Proposed, never confirmed or dismissed", module="ask")
    except Exception:
        pass

    # ── money left on the table in Food Cost ──
    if getattr(restaurant, "module_inventory", 0) and _sees(viewer, "inventory"):
        try:
            import invoices
            pending = [i for i in invoices.list_imports(restaurant_id, db_path=db_path)
                       if not i.get("applied_at")]
            if pending:
                add("invoice:pending", "invoice",
                    f"{len(pending)} scanned invoice{'' if len(pending) == 1 else 's'} not applied",
                    "important", {"label": "Review costs", "module": "inventory"},
                    detail="Ingredient costs still say the old price", module="inventory",
                    count=len(pending))
        except Exception:
            pass
        try:
            import menu_intelligence
            sg = [x for x in (menu_intelligence.reprice_suggestions(restaurant_id, db_path=db_path)
                              .get("suggestions") or []) if (x.get("monthly_margin_lost") or 0) >= 25]
            if sg:
                worst = sg[0]
                add("reprice", "reprice",
                    f"{len(sg)} dish{'' if len(sg) == 1 else 'es'} priced below their new cost",
                    "watch", {"label": "Open Food Cost", "module": "inventory"},
                    detail=f"{worst['dish']} alone is about ${worst['monthly_margin_lost']:,.0f}/month",
                    module="inventory", count=len(sg))
        except Exception:
            pass

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
    live.sort(key=lambda i: rank.get(i["severity"], 3))
    return {"items": live, "snoozed": len(out) - len(live),
            "note": ("Everything still open, across every module. Snoozing puts an item back "
                     "tomorrow — it never goes away on its own.")}
