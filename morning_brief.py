"""
morning_brief.py — the one thing the owner reads before service.

Every number here already existed somewhere in the product; the owner had to
open the app to find any of it. Home's brief is on demand, the weekly digest
is weekly, and Ask's opening waits to be opened. This delivers — to the
owner's phone at their own opening hour — the handful of lines that change
what they do today:

  * how yesterday went against a normal day of the same weekday
  * month-to-date prime cost, projected
  * the one thing to do first, ranked across modules by dollars
  * what's open and who owns it
  * what came back from things they already changed
  * goals, and today's expected day

DETERMINISTIC. No model writes this. Every line is read from a module that
already measures it, so the brief cannot state a figure the data doesn't
carry — the failure ai_guard.verify_figures exists to catch elsewhere cannot
happen here by construction. Each line carries an "ask" prompt so a tap opens
Ask Cavnar on exactly that question.
"""
import logging
import os
from datetime import date, datetime, timedelta

from models import get_conn, DB_PATH

log = logging.getLogger(__name__)

# A "morning" brief is not sent after this local hour. A deploy or an outage
# that delays the scheduler to 9pm should skip the day, not text the owner a
# morning brief at dinner service.
LATEST_SEND_HOUR = 14


def _money(v):
    return f"${v:,.0f}"


def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:
        log.warning("morning_brief: %s failed: %s", getattr(fn, "__name__", fn), e)
        return None



def _reviews_waiting(restaurant_id, db_path=DB_PATH):
    """Reviews with no reply yet, and how many of those are 2 stars or
    worse. The same statuses the Reviews inbox counts as outstanding."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS waiting, SUM(rating <= 2) AS urgent FROM reviews "
            "WHERE restaurant_id=? AND deleted_at IS NULL "
            "AND response_status IN ('pending','drafted')", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    return {"waiting": int(row["waiting"] or 0), "urgent": int(row["urgent"] or 0)} if row else {}


def _critical_low(restaurant_id):
    """Items the inventory analysis calls critically low — live data only, so
    a restaurant still on sample figures is never told to order anything."""
    from inventory import load_inventory_for_restaurant, analysis_for
    items, is_live = load_inventory_for_restaurant(restaurant_id)
    if not is_live or not items:
        return []
    analysis = (analysis_for(restaurant_id, items=items, is_live=True) or (None, None, {}))[2]
    return [x["item"] for x in (analysis.get("critical_low") or [])]


def _schedule_drafted_recently(restaurant_id, db_path=DB_PATH, days=5):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT 1 FROM schedule_history WHERE restaurant_id=? AND "
                           "generated_at >= datetime('now', ?) LIMIT 1",
                           (restaurant_id, f"-{days} days")).fetchone()
    finally:
        conn.close()
    return bool(row)


def _day_context(restaurant, day):
    """", 88° and sunny — Labor Day" — the weather and the calendar, appended
    to the day's line. Returns "" when neither is known."""
    bits = []
    try:
        import weather
        rows = weather.get_forecast_for_week(restaurant, [day.isoformat()]) or []
        if rows:
            w = rows[0]
            piece = str(w.get("short_forecast") or "").lower()
            if w.get("high_f"):
                piece = f"{w['high_f']}° and {piece}" if piece else f"a high of {w['high_f']}°"
            if w.get("precip_pct"):
                piece += f", {w['precip_pct']}% chance of rain"
            if piece:
                bits.append(piece)
    except Exception as e:
        log.warning("morning_brief weather failed: %s", e)
    try:
        from marketing import get_upcoming_holidays
        stamp = day.strftime("(%b %d)")
        upcoming = get_upcoming_holidays(datetime.combine(day, datetime.min.time())) or ""
        todays = [h.replace(stamp, "").strip() for h in upcoming.split(", ") if stamp in h]
        if todays:
            bits.append(todays[0])
    except Exception as e:
        log.warning("morning_brief holidays failed: %s", e)
    return (" — " + " · ".join(bits)) if bits else ""


def build(restaurant_id, restaurant=None, today=None, db_path=DB_PATH, viewer=None):
    """The brief as structured lines. Each line: {"key", "text", "tone", "ask"}.
    tone is good | bad | neutral | action.

    `viewer` is the login it is for (a current_user-shaped dict). A manager's
    brief is built from what THEY may see — the same viewer_restaurant Ask
    uses, so a module their role can't read (and its dollars in the money
    ranking) is left out, and comps & voids appear only with LOSS_VIEW. None
    means the owner's full view."""
    from models import get_restaurant
    from permissions import has_permission, LOSS_VIEW
    import demand, issues, outcomes, goals
    from ask_cavnar_tools import viewer_restaurant, metric_visible
    restaurant = restaurant or get_restaurant(restaurant_id)
    if viewer is not None:
        restaurant = viewer_restaurant(restaurant, viewer)
    denied = getattr(restaurant, "_ask_denied", frozenset())
    sees_loss = viewer is None or has_permission(viewer, LOSS_VIEW)
    today = today or date.today()
    lines = []

    # ── yesterday ── (daily sales live in the Labor module's history)
    y = (_safe(demand.yesterday_vs_typical, restaurant_id, today=today, db_path=db_path)
         if "labor" not in denied else None)
    if y and y.get("available"):
        if y["off"]:
            tone = "good" if y["direction"] == "above" else "bad"
            text = (f"Yesterday: {_money(y['actual'])}, {abs(y['pct']):.0f}% {y['direction']} a typical "
                    f"{y['weekday']} ({_money(y['typical'])}).")
        else:
            tone = "neutral"
            text = f"Yesterday: {_money(y['actual'])}, a normal {y['weekday']}."
        ask = (f"Why was yesterday's {y['weekday']} {y['direction']} a normal one?" if y["off"]
               else f"How did yesterday compare to a normal {y['weekday']}?")
        lines.append({"key": "yesterday", "text": text, "tone": tone, "ask": ask})

    # ── prime cost ──
    if getattr(restaurant, "module_inventory", 0):
        import food_cost_intelligence as fci
        pp = _safe(fci.profitability_projection, restaurant_id, db_path=db_path)
        if pp and pp.get("available") and pp.get("prime_cost_pct") is not None:
            delta = pp.get("prime_pct_delta")
            vs = (f", {abs(delta):.1f} points {'above' if delta > 0 else 'below'} last month"
                  if delta not in (None, 0) else "")
            lines.append({"key": "prime_cost", "tone": "bad" if (delta or 0) > 1 else "neutral",
                          "text": f"Prime cost month to date: {pp['prime_cost_pct']:.1f}% of sales{vs}.",
                          "ask": "What's driving my prime cost this month?"})

    # ── the one thing ──
    import business_intelligence as bi
    eb = _safe(bi.executive_brief, restaurant_id, restaurant=restaurant, db_path=db_path)
    if eb and eb.get("fix_first"):
        f = eb["fix_first"]
        lines.append({"key": "fix_first", "tone": "action",
                      "text": f"If you only do one thing: {f.get('what')}.",
                      "ask": f"Walk me through this: {f.get('what')}"})
    top = ((eb or {}).get("money") or {}).get("ranked") or []
    if top:
        t = top[0]
        amount = (f"{_money(t['monthly_low'])}-{_money(t['monthly_high'])}" if t.get("is_range")
                  else _money(t["monthly"]))
        lines.append({"key": "money", "tone": "neutral",
                      "text": f"Biggest dollar opportunity: {t['label']}, {amount}/month.",
                      "ask": f"How do I go after the {t['label'].lower()} opportunity?"})

    # ── accountability ──
    s = _safe(issues.summary, restaurant_id, db_path=db_path)
    if s and (s["open"] or s["acknowledged"]):
        age = (f" — oldest waiting {s['oldest_open_hours']:.0f}h unacknowledged"
               if s.get("oldest_open_hours") else "")
        lines.append({"key": "issues", "tone": "bad" if s["open"] else "neutral",
                      "text": f"Open issues: {s['open']} unacknowledged, {s['acknowledged']} in hand{age}.",
                      "ask": "What issues are still open and who has them?"})

    # ── what came back ──
    results = [r for r in (_safe(outcomes.recent_results, restaurant_id, days=1, db_path=db_path,
                                  today=today) or []) if metric_visible(restaurant, r.get("metric"))]
    for r in results[:2]:
        lines.append({"key": f"outcome:{r['id']}",
                      "tone": {"improved": "good", "worsened": "bad"}.get(r.get("verdict"), "neutral"),
                      "text": "Result: " + outcomes.summarise(r),
                      "ask": f"Tell me more about the result of: {r['title']}"})

    # ── goals ──
    visible_goals = [g for g in (_safe(goals.progress, restaurant_id, db_path=db_path, today=today) or [])
                     if metric_visible(restaurant, g.get("metric"))]
    for g in visible_goals[:2]:
        if g["state"] == "unknown":
            continue
        lines.append({"key": f"goal:{g['id']}",
                      "tone": {"met": "good", "moving_right_way": "good",
                               "moving_wrong_way": "bad", "missed": "bad"}.get(g["state"], "neutral"),
                      "text": "Goal — " + goals.summarise(g),
                      "ask": f"How do I hit my {g['label'].lower()} goal?"})

    # ── loss signals — the owner, or a manager explicitly granted LOSS_VIEW ──
    import loss_detection
    ls = _safe(loss_detection.signals, restaurant_id, today=today, db_path=db_path) if sees_loss else None
    for f in ((ls or {}).get("flagged") or [])[:1]:
        lines.append({"key": "loss", "tone": "bad",
                      "text": f"Worth reviewing: {f['headline']}.",
                      "ask": "Show me the comp and void pattern from last week."})

    # ── last night, in the closer's own words ──
    # The only account of a service this product cannot see (the POS syncs
    # at 3am). Placed high on purpose: it is the one thing here a person
    # wrote rather than a number that was measured.
    import closeout
    co = _safe(closeout.latest, restaurant_id, today, db_path=db_path)
    if co and (co.get("business_date") or "") >= (today - timedelta(days=1)).isoformat():
        summary = closeout.summarise(co)
        if summary:
            lines.append({"key": "closeout", "tone": "action" if co.get("went_wrong") else "neutral",
                          "text": summary,
                          "ask": "What should I do about last night's close-out?"})

    # ── waiting on a reply ── (the owner's own inbox, before service)
    if getattr(restaurant, "module_reviews", 0) and "reviews" not in denied:
        rs = _safe(_reviews_waiting, restaurant_id, db_path) or {}
        if rs.get("waiting"):
            urgent = rs.get("urgent") or 0
            extra = f", {urgent} of them 2 stars or worse" if urgent else ""
            lines.append({"key": "reviews", "tone": "bad" if urgent else "action",
                          "text": f"{rs['waiting']} review{'' if rs['waiting'] == 1 else 's'} "
                                  f"waiting on a reply{extra}.",
                          "ask": "Which reviews still need a reply, and what should I say?"})

    # ── running low ── (what the kitchen will hit today, not next week)
    if getattr(restaurant, "module_inventory", 0) and "inventory" not in denied:
        low = _safe(_critical_low, restaurant_id) or []
        if low:
            named = ", ".join(low[:3]) + (f" and {len(low) - 3} more" if len(low) > 3 else "")
            lines.append({"key": "stock", "tone": "bad",
                          "text": f"Running low: {named}.",
                          "ask": "What do I need to order today?"})

    # ── next week's schedule ── (Thursday onward, if nothing is drafted yet)
    if getattr(restaurant, "module_labor", 0) and "labor" not in denied and today.weekday() >= 3:
        if not _safe(_schedule_drafted_recently, restaurant_id, db_path):
            lines.append({"key": "schedule", "tone": "action",
                          "text": "Next week's schedule hasn't been built yet.",
                          "ask": "Build next week's schedule."})

    # ── a slow day worth acting on ──
    # Mondays only: which weekday is quiet does not change overnight, and a
    # line that says the same thing every morning is one people stop
    # reading. It is also in the weekly digest.
    if getattr(restaurant, "module_labor", 0) and "labor" not in denied and today.weekday() == 0:
        sd = _safe(demand.slow_days, restaurant_id, db_path=db_path) or {}
        slow = (sd.get("slow_days") or [])[:1]
        if slow:
            d = slow[0]
            lines.append({"key": "slow_day", "tone": "neutral",
                          "text": f"{d['day']}s run about {abs(d['vs_average_pct'])}% under a normal day.",
                          "ask": f"How do I fill {d['day']}s?"})

    # ── today ──
    fc = _safe(demand.forecast_day, restaurant_id, today, db_path=db_path) if "labor" not in denied else None
    if fc and fc.get("available"):
        lines.append({"key": "today", "tone": "neutral",
                      "text": (f"Today looks like a typical {fc['weekday']}: about {_money(fc['typical_sales'])} "
                               f"(range {_money(fc['low'])}-{_money(fc['high'])} over {fc['samples']} weeks)"
                               + (_day_context(restaurant, today) or "") + "."),
                      "ask": "What should I focus on before service today?"})
    elif restaurant is not None:
        # No forecast yet, but the weather and the calendar are still worth
        # knowing — and they are the only "today" the first weeks have.
        context = _day_context(restaurant, today)
        if context:
            lines.append({"key": "today", "tone": "neutral",
                          "text": "Today" + context + ".",
                          "ask": "What should I focus on before service today?"})
    return {"restaurant_id": restaurant_id, "date": today.isoformat(), "lines": lines}


def push_text(brief, restaurant_name):
    """A title and a two-line body for the lock screen. The lead is the most
    actionable line, not the first — 'one thing to do' beats 'yesterday'."""
    order = {"action": 0, "bad": 1, "good": 2, "neutral": 3}
    lines = sorted(brief["lines"], key=lambda l: order.get(l["tone"], 9))
    if not lines:
        return None
    body = lines[0]["text"]
    if len(lines) > 1:
        body += " " + lines[1]["text"]
    return {"title": f"Good morning — {restaurant_name}", "body": body[:230]}


def _ask_url(prompt):
    """A link that opens the dashboard and asks that question — the email's
    version of the push's one-tap into Ask (dashboard.html reads ?ask=)."""
    from urllib.parse import quote
    base = (os.getenv("BASE_URL") or "https://dashboard.cavnar.ai").rstrip("/")
    return f"{base}/?ask={quote(prompt or '', safe='')}"


def _email_html(brief, restaurant_name):
    import html
    dot = {"good": "#2d6a4f", "bad": "#c0392b", "action": "#c84b2f", "neutral": "#7a736a"}
    rows = "".join(
        f'<tr><td style="padding:10px 0;border-top:1px solid #ece7dd;vertical-align:top;width:14px">'
        f'<div style="width:8px;height:8px;border-radius:4px;background:{dot.get(l["tone"], "#7a736a")};'
        f'margin-top:6px"></div></td><td style="padding:10px 0 10px 8px;border-top:1px solid #ece7dd;'
        f'font-size:15px;line-height:1.55;color:#1a1714">{html.escape(l["text"])}'
        # Every line is a question you can ask about it — the email's
        # equivalent of tapping the push, which opens Ask on that line.
        + (f'<br><a href="{html.escape(_ask_url(l.get("ask")), quote=True)}" '
           f'style="font-size:13px;color:#c84b2f;text-decoration:none">Ask about this &rarr;</a>'
           if l.get("ask") else "")
        + '</td></tr>'
        for l in brief["lines"])
    return (f'<p style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#7a736a;'
            f'margin:0 0 6px">{html.escape(restaurant_name)} · {brief["date"]}</p>'
            f'<h1 style="font-size:22px;margin:0 0 14px;color:#0e0c0a">Your morning brief</h1>'
            f'<table role="presentation" style="width:100%;border-collapse:collapse">{rows}</table>'
            f'<p style="font-size:13px;color:#7a736a;margin:18px 0 0">Every figure above is measured '
            f'from your own data. Open Cavnar AI and ask about any line.</p>')


def recipients(restaurant_id, db_path=DB_PATH):
    """Everyone who gets this restaurant's brief: its console logins whose
    brief preference is on (owners and managers by default — the point is
    that the people running the floor start the day informed), plus the
    group owner whose login lives on another location. Never employees (PIN
    identities, no console) and never admin logins. Each carries the grants
    their brief is built with."""
    from auth import _grants_for, morning_brief_default
    from permissions import CONSOLE_ROLES, normalize_role
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, role, is_admin, email FROM users WHERE restaurant_id=? AND COALESCE(is_active,1)=1",
            (restaurant_id,)).fetchall()]
        # A multi-location owner's login lives on the group's base restaurant.
        rows += [dict(r) for r in conn.execute(
            "SELECT u.id, u.role, u.is_admin, u.email FROM users u "
            "JOIN restaurants b ON b.id=u.restaurant_id JOIN restaurants r ON r.id=? "
            "WHERE u.role='owner' AND COALESCE(u.is_active,1)=1 AND b.location_group IS NOT NULL "
            "AND b.location_group=r.location_group AND b.owner_email=r.owner_email AND b.id != r.id",
            (restaurant_id,)).fetchall()]
        try:
            prefs = {r["user_id"]: r["morning_brief"] for r in conn.execute(
                "SELECT user_id, morning_brief FROM login_prefs WHERE restaurant_id=?", (restaurant_id,))}
        except Exception:
            prefs = {}
        out, seen = [], set()
        for u in rows:
            if u["id"] in seen or u["is_admin"] or normalize_role(u["role"]) not in CONSOLE_ROLES:
                continue
            seen.add(u["id"])
            on = morning_brief_default(u["role"]) if prefs.get(u["id"]) is None else bool(prefs[u["id"]])
            if not on:
                continue
            u["grants"] = _grants_for(conn, u["id"], restaurant_id)
            u["restaurant_id"] = restaurant_id
            out.append(u)
    finally:
        conn.close()
    return out


def _view_key(user):
    """Two logins with the same view get the same brief — built once."""
    from permissions import has_permission, LOSS_VIEW, MODULE_VIEW_PERMISSIONS
    return (frozenset(k for k, p in MODULE_VIEW_PERMISSIONS.items() if has_permission(user, p)),
            has_permission(user, LOSS_VIEW))


def deliver(restaurant_id, restaurant=None, today=None, db_path=DB_PATH):
    """Build and send each recipient THEIR brief. Push to that person's own
    devices when they have the app; email them otherwise — never both,
    because a brief that arrives twice is one people learn to ignore.
    Returns counts."""
    from models import get_restaurant
    import push
    restaurant = restaurant or get_restaurant(restaurant_id)
    name = restaurant.location_name or restaurant.name
    people = recipients(restaurant_id, db_path)
    if not people:
        return {"sent": 0, "reason": "nobody is set to receive it"}
    devices = {}
    for t in (_safe(push.get_device_tokens, restaurant_id, db_path) or []):
        if not t.get("disabled_reason"):
            devices.setdefault(int(t.get("user_id") or 0), []).append(t)
    built, pushed, emailed, empty = {}, 0, 0, 0
    for u in people:
        key = _view_key(u)
        if key not in built:
            built[key] = build(restaurant_id, restaurant=restaurant, today=today, db_path=db_path, viewer=u)
        brief = built[key]
        if not brief["lines"]:
            empty += 1
            continue
        if devices.get(u["id"]):
            pt = push_text(brief, name)
            lead = next((l for l in brief["lines"] if l["tone"] == "action"), brief["lines"][0])
            # This person's devices only: two recipients can hold different briefs.
            push.fire_push(restaurant_id, "morning_brief", pt["title"], pt["body"],
                           data={"ask_prompt": lead["ask"]}, db_path=db_path, user_ids={u["id"]})
            pushed += 1
        elif u.get("email"):
            from emails import deliver as _deliver, _branded_email, _from_email
            result = _deliver(email_type="send_morning_brief", restaurant_id=restaurant_id, payload={
                "from": f"Cavnar AI <{_from_email()}>", "to": [u["email"]],
                "subject": f"Your morning brief — {name}",
                "html": _branded_email(_email_html(brief, name))})
            # Read .ok explicitly rather than leaning on SendResult.__bool__.
            if getattr(result, "ok", False):
                emailed += 1
    return {"sent": pushed + emailed, "push": pushed, "email": emailed, "empty": empty,
            "recipients": len(people)}


def run_due(db_path=DB_PATH, now_utc=None):
    """Scheduler entry point: send each restaurant's brief once, at or after
    its own local brief hour, and never after LATEST_SEND_HOUR local."""
    from models import get_all_restaurants
    from time_utils import restaurant_now
    import ops
    sent, skipped = 0, 0
    for r in get_all_restaurants(db_path):
        if not getattr(r, "morning_brief_enabled", 1):
            continue
        if (getattr(r, "billing_status", None) or "trial").lower() not in ("active", "internal", "trial"):
            continue
        local = restaurant_now(r, naive=True)
        hour = int(getattr(r, "morning_brief_hour", 7) or 7)
        if not (hour <= local.hour < LATEST_SEND_HOUR):
            continue
        if not ops.claim_period(f"morning_brief:{r.id}", local.date().isoformat()):
            continue
        try:
            out = deliver(r.id, restaurant=r, today=local.date(), db_path=db_path)
            sent += 1 if out.get("sent") else 0
            skipped += 0 if out.get("sent") else 1
        except Exception as e:
            ops.capture(e, job="morning_brief", context=f"restaurant_id={r.id}")
    return {"sent": sent, "skipped": skipped}
