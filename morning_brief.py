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


def build(restaurant_id, restaurant=None, today=None, db_path=DB_PATH):
    """The brief as structured lines. Each line: {"key", "text", "tone", "ask"}.
    tone is good | bad | neutral | action."""
    from models import get_restaurant
    import demand, issues, outcomes, goals
    restaurant = restaurant or get_restaurant(restaurant_id)
    today = today or date.today()
    lines = []

    # ── yesterday ──
    y = _safe(demand.yesterday_vs_typical, restaurant_id, today=today, db_path=db_path)
    if y and y.get("available"):
        if y["off"]:
            tone = "good" if y["direction"] == "above" else "bad"
            text = (f"Yesterday: {_money(y['actual'])}, {abs(y['pct']):.0f}% {y['direction']} a typical "
                    f"{y['weekday']} ({_money(y['typical'])}).")
        else:
            tone = "neutral"
            text = f"Yesterday: {_money(y['actual'])}, a normal {y['weekday']}."
        lines.append({"key": "yesterday", "text": text, "tone": tone,
                      "ask": f"Why was yesterday's {y['weekday']} {y['direction']} a normal one?"})

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
    for r in (_safe(outcomes.recent_results, restaurant_id, days=1, db_path=db_path, today=today) or [])[:2]:
        lines.append({"key": f"outcome:{r['id']}",
                      "tone": {"improved": "good", "worsened": "bad"}.get(r.get("verdict"), "neutral"),
                      "text": "Result: " + outcomes.summarise(r),
                      "ask": f"Tell me more about the result of: {r['title']}"})

    # ── goals ──
    for g in (_safe(goals.progress, restaurant_id, db_path=db_path, today=today) or [])[:2]:
        if g["state"] == "unknown":
            continue
        lines.append({"key": f"goal:{g['id']}",
                      "tone": {"met": "good", "moving_right_way": "good",
                               "moving_wrong_way": "bad", "missed": "bad"}.get(g["state"], "neutral"),
                      "text": "Goal — " + goals.summarise(g),
                      "ask": f"How do I hit my {g['label'].lower()} goal?"})

    # ── loss signals (owner only — never routed to a manager) ──
    import loss_detection
    ls = _safe(loss_detection.signals, restaurant_id, today=today, db_path=db_path)
    for f in ((ls or {}).get("flagged") or [])[:1]:
        lines.append({"key": "loss", "tone": "bad",
                      "text": f"Worth reviewing: {f['headline']}.",
                      "ask": "Show me the comp and void pattern from last week."})

    # ── today ──
    fc = _safe(demand.forecast_day, restaurant_id, today, db_path=db_path)
    if fc and fc.get("available"):
        lines.append({"key": "today", "tone": "neutral",
                      "text": f"Today looks like a typical {fc['weekday']}: about {_money(fc['typical_sales'])} "
                              f"(range {_money(fc['low'])}-{_money(fc['high'])} over {fc['samples']} weeks).",
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


def _email_html(brief, restaurant_name):
    import html
    dot = {"good": "#2d6a4f", "bad": "#c0392b", "action": "#c84b2f", "neutral": "#7a736a"}
    rows = "".join(
        f'<tr><td style="padding:10px 0;border-top:1px solid #ece7dd;vertical-align:top;width:14px">'
        f'<div style="width:8px;height:8px;border-radius:4px;background:{dot.get(l["tone"], "#7a736a")};'
        f'margin-top:6px"></div></td><td style="padding:10px 0 10px 8px;border-top:1px solid #ece7dd;'
        f'font-size:15px;line-height:1.55;color:#1a1714">{html.escape(l["text"])}</td></tr>'
        for l in brief["lines"])
    return (f'<p style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#7a736a;'
            f'margin:0 0 6px">{html.escape(restaurant_name)} · {brief["date"]}</p>'
            f'<h1 style="font-size:22px;margin:0 0 14px;color:#0e0c0a">Your morning brief</h1>'
            f'<table role="presentation" style="width:100%;border-collapse:collapse">{rows}</table>'
            f'<p style="font-size:13px;color:#7a736a;margin:18px 0 0">Every figure above is measured '
            f'from your own data. Open Cavnar AI and ask about any line.</p>')


def deliver(restaurant_id, restaurant=None, today=None, db_path=DB_PATH):
    """Build and send. Push when the owner has the app on a device; email
    otherwise — never both, because a brief that arrives twice is one the
    owner learns to ignore. Returns what was sent."""
    from models import get_restaurant
    restaurant = restaurant or get_restaurant(restaurant_id)
    brief = build(restaurant_id, restaurant=restaurant, today=today, db_path=db_path)
    if not brief["lines"]:
        return {"sent": None, "reason": "nothing measured to report yet"}
    name = restaurant.location_name or restaurant.name
    import push
    tokens = _safe(push.get_device_tokens, restaurant_id, db_path) or []
    if tokens:
        pt = push_text(brief, name)
        lead = next((l for l in brief["lines"] if l["tone"] == "action"), brief["lines"][0])
        push.fire_push(restaurant_id, "morning_brief", pt["title"], pt["body"],
                       data={"ask_prompt": lead["ask"]}, db_path=db_path)
        return {"sent": "push", "lines": len(brief["lines"])}
    if restaurant.owner_email:
        from emails import deliver as _deliver, _branded_email, _from_email
        result = _deliver(email_type="send_morning_brief", restaurant_id=restaurant_id, payload={
            "from": f"Cavnar AI <{_from_email()}>", "to": [restaurant.owner_email],
            "subject": f"Your morning brief — {name}",
            "html": _branded_email(_email_html(brief, name))})
        # Read .ok explicitly rather than leaning on SendResult.__bool__, so
        # the intent survives if that dunder ever changes.
        ok = bool(getattr(result, "ok", False))
        return {"sent": "email" if ok else None, "lines": len(brief["lines"])}
    return {"sent": None, "reason": "no device and no owner email"}


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
