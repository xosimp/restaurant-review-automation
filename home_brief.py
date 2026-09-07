"""
home_brief.py — the web Home screen's daily operating brief, in one payload.

Everything on Home is derived here, server-side, from data the restaurant
actually has: review stats, the labor and inventory analyses, alerts,
schedules, scheduled posts, integrations and their freshness. Nothing calls
an AI model on page load — the brief is deterministic, so a refresh is
cheap and two owners looking at the same restaurant see the same thing.

Two rules the whole file follows:

* Sample data is never presented as the restaurant's own. Labor and Food
  Cost fall back to bundled sample files until the owner uploads or syncs;
  those modules are shown in a "sample" state with a setup nudge, and they
  never produce an attention item, a recommendation or a "change".
* Everything is scoped to `current_user["restaurant_id"]` — the location
  the owner is viewing. The portfolio strip for multi-location owners only
  reads the cheap signals per sibling location; it never averages them.
"""
import json
from datetime import datetime, timedelta, timezone

from models import get_conn, get_restaurant

_CACHE = {}
_CACHE_TTL = 60  # seconds — a refresh within a minute costs nothing

_REVIEW_FETCH_HOURS_CT = (8, 12, 16, 20)  # scheduler.py's review_fetch cadence
_DISMISS_DAYS = 14  # a dismissed recommendation stays gone this long, then can resurface if still true

_DISMISS_SQL = """
CREATE TABLE IF NOT EXISTS home_dismissals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'recommendation',
    dismissed_by INTEGER,
    dismissed_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    UNIQUE(restaurant_id, key) ON CONFLICT REPLACE
)
"""


def _dismissed_keys(conn, rid):
    try:
        conn.execute(_DISMISS_SQL)
        return {r["key"]: r for r in conn.execute("SELECT key, kind, dismissed_at, expires_at FROM home_dismissals WHERE restaurant_id=? AND expires_at > datetime('now')", (rid,)).fetchall()}
    except Exception:
        return {}


def dismiss(rid, key, kind="recommendation", user_id=None, days=_DISMISS_DAYS):
    """Hide one recommendation for this restaurant. Keys carry their subject
    ("trim_day:Monday", "cut_waste:Salmon Fillet"), so a different day or
    item is a new recommendation and comes through."""
    key = (key or "").strip()[:120]
    if not key:
        return {"ok": False, "error": "Missing key"}
    conn = get_conn()
    conn.execute(_DISMISS_SQL)
    conn.execute("INSERT INTO home_dismissals (restaurant_id, key, kind, dismissed_by, expires_at) VALUES (?,?,?,?, datetime('now', ?))",
                 (rid, key, kind, user_id, f"+{int(days)} days"))
    conn.commit(); conn.close()
    invalidate(rid)
    return {"ok": True, "key": key, "days": int(days)}


def undismiss(rid, key):
    conn = get_conn()
    conn.execute(_DISMISS_SQL)
    n = conn.execute("DELETE FROM home_dismissals WHERE restaurant_id=? AND key=?", (rid, (key or "").strip()[:120])).rowcount
    conn.commit(); conn.close()
    invalidate(rid)
    return {"ok": True, "restored": n}


# ── small helpers ────────────────────────────────────────────────────────────

def _ts(v):
    """Parse the two timestamp styles the DB holds — sqlite datetime('now')
    (UTC, space) and Python isoformat (local or tz-aware, 'T') — into an
    aware UTC datetime. None when unparseable."""
    if not v:
        return None
    s = str(v).strip()
    try:
        if "T" in s:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=datetime.now().astimezone().tzinfo)
            return d.astimezone(timezone.utc)
        d = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        return d.replace(tzinfo=timezone.utc)
    except Exception:
        try:
            d = datetime.strptime(s[:10], "%Y-%m-%d")
            return d.replace(tzinfo=timezone.utc)
        except Exception:
            return None


def _iso(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def _age_days(v, now):
    d = _ts(v)
    return (now - d).total_seconds() / 86400.0 if d else None


def _one(conn, sql, params=()):
    try:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def _rows(conn, sql, params=()):
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def _plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _pct_delta(cur, prev):
    if prev in (None, 0) or cur is None:
        return None
    return round(cur - prev, 1)


# ── per-location cheap health (portfolio strip) ──────────────────────────────

def _location_signal(conn, r, now):
    """The three cheap signals that decide whether a sibling location needs a
    look: urgent reviews, an integration error, stale review data. No labor
    or inventory analysis — those are heavy, and this runs once per location."""
    rid = r["id"]
    urgent = _one(conn, "SELECT COUNT(*) AS n FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL AND urgency='high' AND response_status NOT IN ('posted','approved','skipped')", (rid,)) or {}
    awaiting = _one(conn, "SELECT COUNT(*) AS n FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL AND response_status='drafted'", (rid,)) or {}
    rating = _one(conn, "SELECT ROUND(AVG(rating),1) AS r, COUNT(*) AS n FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL AND review_date >= date('now','-30 days')", (rid,)) or {}
    issues = []
    if (urgent.get("n") or 0) > 0:
        issues.append(("critical", f"{_plural(urgent['n'], 'urgent review')} unanswered"))
    if r.get("toast_restaurant_guid") and r.get("toast_sync_error"):
        issues.append(("critical", "Toast sync failing"))
    connected = bool(r.get("gmb_refresh_token") or r.get("reviews_live"))
    age = _age_days(r.get("last_fetched_at"), now)
    if connected and age is not None and age > 3:
        issues.append(("important", f"Reviews not refreshed in {int(age)} days"))
    if (awaiting.get("n") or 0) > 0:
        issues.append(("watch", f"{_plural(awaiting['n'], 'reply')} waiting for approval"))
    sev_rank = {"critical": 3, "important": 2, "watch": 1}
    worst = max((sev_rank[s] for s, _ in issues), default=0)
    health = {3: "critical", 2: "important", 1: "watch", 0: "healthy"}[worst]
    return {"id": rid, "name": r.get("location_name") or r["name"], "restaurant_name": r["name"],
            "health": health, "attention": len(issues), "top_issue": issues[0][1] if issues else None,
            "rating_30d": rating.get("r"), "reviews_30d": rating.get("n") or 0}


# ── the brief ────────────────────────────────────────────────────────────────

def build_home_brief(current_user, fresh=False):
    rid = current_user["restaurant_id"]
    key = (rid, current_user.get("id"))
    if not fresh:
        hit = _CACHE.get(key)
        if hit and (datetime.now(timezone.utc) - hit[0]).total_seconds() < _CACHE_TTL:
            return hit[1], 200
    payload, status = _build(current_user)
    if status == 200:
        _CACHE[key] = (datetime.now(timezone.utc), payload)
    return payload, status


def invalidate(rid=None):
    """Called after an action that changes what Home should say (publishing
    replies, switching location) — the next load recomputes."""
    if rid is None:
        _CACHE.clear()
        return
    for k in [k for k in _CACHE if k[0] == rid]:
        _CACHE.pop(k, None)


def _build(current_user):
    from models import get_review_stats, get_active_modules, get_sentiment_trend, get_top_issues, get_labor_history, is_in_quiet_hours
    from time_utils import restaurant_now
    import mobile_api as _mob

    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not restaurant:
        return {"ok": False, "error": "Restaurant not found"}, 404

    now = datetime.now(timezone.utc)
    local_now = restaurant_now(restaurant)
    active = get_active_modules(restaurant)
    active_keys = {m["key"] for m in active}
    conn = get_conn()
    r = _one(conn, "SELECT * FROM restaurants WHERE id=?", (rid,)) or {}

    # ── previous visit (this user) ──────────────────────────────────────────
    prev_login = _rows(conn, "SELECT created_at FROM login_history WHERE user_id=? ORDER BY id DESC LIMIT 2", (current_user.get("id"),))
    since_dt = _ts(prev_login[1]["created_at"]) if len(prev_login) > 1 else None
    if since_dt is None or (now - since_dt).total_seconds() < 3600:
        # First visit, or a re-login within the hour: "since yesterday" is the
        # honest window, not "since 4 minutes ago".
        since_dt = now - timedelta(days=1)
        since_label = "since yesterday"
    else:
        days = (now - since_dt).total_seconds() / 86400
        since_label = "since your last sign-in" + (f" ({int(days)}d ago)" if days >= 2 else "")
    since_sql = since_dt.strftime("%Y-%m-%d %H:%M:%S")
    since_iso_t = since_dt.strftime("%Y-%m-%dT%H:%M:%S")

    # ── reviews ─────────────────────────────────────────────────────────────
    rstats = get_review_stats(rid) if "reviews" in active_keys else {}
    sentiment = get_sentiment_trend(rid, weeks=8) if "reviews" in active_keys else []
    top_issues = get_top_issues(rid, days=90, limit=3) if "reviews" in active_keys else []
    google_connected = bool(r.get("gmb_refresh_token") or r.get("reviews_live"))
    reviews_since = _one(conn, "SELECT COUNT(*) AS n, ROUND(AVG(rating),1) AS avg, SUM(rating<=2) AS low FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL AND (fetched_at >= ? OR fetched_at >= ?)", (rid, since_sql, since_iso_t)) or {}
    replies_since = _one(conn, "SELECT SUM(response_status='posted' AND posted_at >= ?) AS posted, SUM(response_status IN ('approved','posted') AND approved_at >= ?) AS approved FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL", (since_iso_t, since_iso_t, rid)) or {}
    stale_unanswered = _one(conn, "SELECT COUNT(*) AS n FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL AND rating<=3 AND response_status IN ('pending','drafted') AND julianday(fetched_at) < julianday('now','-2 days')", (rid,)) or {}
    rating_prev = _one(conn, "SELECT ROUND(AVG(rating),1) AS r, COUNT(*) AS n FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL AND review_date >= date('now','-60 days') AND review_date < date('now','-30 days')", (rid,)) or {}

    # ── labor ───────────────────────────────────────────────────────────────
    labor, labor_live = None, False
    if "labor" in active_keys:
        try:
            from labor import analyse_shifts_for_restaurant
            labor = analyse_shifts_for_restaurant(rid)
            labor_live = bool(labor.get("is_live"))
        except Exception:
            labor = None
    labor_target = float(r.get("labor_target_pct") or 30.0)
    labor_hist = get_labor_history(rid, limit=8) if labor_live else []
    client_data = _one(conn, "SELECT updated_at, shifts_source, inventory_source FROM client_data WHERE restaurant_id=?", (rid,)) or {}
    last_schedule = _one(conn, "SELECT generated_at, week_start, week_end, hours_scheduled, hours_budget FROM schedule_history WHERE restaurant_id=? ORDER BY id DESC LIMIT 1", (rid,))

    # ── food cost ───────────────────────────────────────────────────────────
    inv, inv_live = {}, False
    if "inventory" in active_keys:
        try:
            from inventory import load_inventory_for_restaurant, analyse_inventory
            items, inv_live = load_inventory_for_restaurant(rid)
            inv = analyse_inventory(items) if items else {}
            inv_live = bool(inv_live)
        except Exception:
            inv = {}

    # ── marketing ───────────────────────────────────────────────────────────
    mkt = {}
    if "marketing" in active_keys:
        mkt["month"] = (_one(conn, "SELECT COUNT(*) AS n FROM marketing_content_log WHERE restaurant_id=? AND created_at >= date('now','start of month')", (rid,)) or {}).get("n") or 0
        mkt["week"] = (_one(conn, "SELECT COUNT(*) AS n FROM marketing_content_log WHERE restaurant_id=? AND julianday(created_at) >= julianday('now','-7 days')", (rid,)) or {}).get("n") or 0
        mkt["last_at"] = (_one(conn, "SELECT MAX(created_at) AS t FROM marketing_content_log WHERE restaurant_id=?", (rid,)) or {}).get("t")
        mkt["last_posted_at"] = (_one(conn, "SELECT MAX(created_at) AS t FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL", (rid,)) or {}).get("t")
        mkt["scheduled"] = _rows(conn, "SELECT id, platform, topic, content_type, scheduled_for, status FROM marketing_scheduled_posts WHERE restaurant_id=? AND status IN ('scheduled','pending') AND scheduled_for >= datetime('now') ORDER BY scheduled_for LIMIT 3", (rid,))
        mkt["failed"] = _rows(conn, "SELECT id, platform, topic, error, scheduled_for FROM marketing_scheduled_posts WHERE restaurant_id=? AND status='failed' ORDER BY id DESC LIMIT 3", (rid,))
        mkt["posted_since"] = (_one(conn, "SELECT COUNT(*) AS n FROM marketing_scheduled_posts WHERE restaurant_id=? AND status='posted' AND posted_at >= ?", (rid, since_sql)) or {}).get("n") or 0
        mkt["ig_connected"] = bool(r.get("ig_token"))
        mkt["fb_connected"] = bool(r.get("fb_page_token"))

    # ── intel ───────────────────────────────────────────────────────────────
    intel = None
    if "intel" in active_keys:
        intel = {"updated_at": r.get("competitor_updated_at"), "recs": 0, "competitors": 0}
        if r.get("competitor_intel"):
            try:
                from competitor_intel_format import extract_recs
                blob = json.loads(r["competitor_intel"])
                intel["recs"] = len(extract_recs(blob.get("insight", "")))
                intel["competitors"] = len(blob.get("competitors") or [])
            except Exception:
                pass

    # ── alerts ──────────────────────────────────────────────────────────────
    alerts_7d = _rows(conn, "SELECT alert_type, COUNT(*) AS n, MAX(fired_at) AS last_at FROM alert_log WHERE restaurant_id=? AND julianday(fired_at) >= julianday('now','-7 days') GROUP BY alert_type ORDER BY n DESC", (rid,))
    alerts_since = (_one(conn, "SELECT COUNT(*) AS n FROM alert_log WHERE restaurant_id=? AND fired_at >= ?", (rid, since_sql)) or {}).get("n") or 0
    recent_alerts = _rows(conn, """SELECT a.alert_type, a.review_id, a.fired_at, rv.rating, rv.response_status, rv.author
                                   FROM alert_log a LEFT JOIN reviews rv ON rv.id=a.review_id
                                   WHERE a.restaurant_id=? AND julianday(a.fired_at) >= julianday('now','-7 days')
                                   ORDER BY a.id DESC LIMIT 12""", (rid,))
    dismissed = _dismissed_keys(conn, rid)
    conn.close()

    # The Reviews tab's AI read, only if it's already been generated and is
    # still in the 5-minute cache — Home never triggers a model call itself.
    ai_insight = None
    try:
        from client_api import _cache_get
        cached = _cache_get("review-insight:" + str(rid))
        if cached and isinstance(cached, str) and cached.strip():
            ai_insight = {"source": "reviews", "text": cached.strip()[:900]}
    except Exception:
        ai_insight = None

    # ═════════════════════════════════════════════════════════════════════════
    # Derivations
    # ═════════════════════════════════════════════════════════════════════════
    attention = []   # dicts with severity critical|important|watch
    wins = []        # positive signals
    recs = []        # recommendations
    changes = []     # since last visit
    brief_lines = []
    snapshot = []
    freshness = []
    upcoming = []
    ask = []

    def add_attn(key, severity, title, detail, module, action_label, action="open_module", since=None, evidence=None):
        attention.append({"key": key, "severity": severity, "title": title, "detail": detail, "module": module,
                          "action": {"label": action_label, "kind": action, "module": module},
                          "since": since, "evidence": evidence, "location": restaurant.location_name or None})

    def add_win(key, title, detail, module):
        wins.append({"key": key, "title": title, "detail": detail, "module": module})

    def add_rec(key, title, why, evidence, impact, module, timeframe, strength="moderate", action_label=None):
        recs.append({"key": key, "title": title, "why": why, "evidence": evidence, "impact": impact, "module": module,
                     "timeframe": timeframe, "strength": strength, "action_label": action_label or "Open " + {"inventory": "Food Cost"}.get(module, module.title())})

    def add_change(text, tone, module, at=None):
        changes.append({"text": text, "tone": tone, "module": module, "at": at})

    # ── Reviews ────────────────────────────────────────────────────────────
    if "reviews" in active_keys:
        total = int(rstats.get("total") or 0)
        urgent = int(rstats.get("urgent") or 0)
        awaiting = int(rstats.get("awaiting_approval") or 0)
        rate = float(rstats.get("response_rate") or 0)
        avg30 = float(rstats.get("avg_rating_30d") or 0)
        n30 = int(rstats.get("last_30d") or 0)
        prev_avg = float(rating_prev.get("r") or 0)
        prev_n = int(rating_prev.get("n") or 0)
        last_fetch_age = _age_days(r.get("last_fetched_at"), now)

        # freshness
        if google_connected:
            state = "fresh" if (last_fetch_age is not None and last_fetch_age <= 1) else ("stale" if last_fetch_age is not None else "missing")
            freshness.append({"key": "reviews", "label": "Reviews", "at": r.get("last_fetched_at"), "state": state,
                              "note": "Google Business connected" if state != "stale" else f"last pulled {int(last_fetch_age)}d ago"})
        else:
            freshness.append({"key": "reviews", "label": "Reviews", "at": r.get("last_fetched_at"), "state": "missing" if total == 0 else "manual",
                              "note": "Google not connected" if total == 0 else "imported reviews — Google not connected"})

        # attention
        if urgent:
            add_attn("urgent_reviews", "critical", f"{_plural(urgent, 'urgent review')} unanswered",
                     "Low-star reviews mentioning something serious are still waiting on a reply.", "reviews", "Reply now",
                     evidence=f"{urgent} of {total} reviews flagged urgent")
        if (stale_unanswered.get("n") or 0) > 0 and not urgent:
            n = stale_unanswered["n"]
            add_attn("stale_low_reviews", "important", f"{_plural(n, 'low-star review')} unanswered for 2+ days",
                     "Guests read how you respond. A 48-hour reply keeps the thread on your side.", "reviews", "Answer them",
                     since="48h+")
        if awaiting:
            add_attn("awaiting_approval", "watch" if awaiting < 5 else "important", f"{_plural(awaiting, 'reply')} drafted, waiting for you",
                     "Written in your voice. Approve them in one click and Google-connected replies post right away.", "reviews",
                     f"Publish {min(awaiting, 25)}", action="publish_replies", evidence=f"{awaiting} drafted")
        if total >= 5 and rate < 50:
            add_attn("low_response_rate", "watch", f"Response rate at {rate:.0f}%",
                     "Restaurants answering 80%+ of reviews see measurably more new guests.", "reviews", "Answer reviews",
                     evidence=f"{int(rstats.get('responded') or 0)} of {total} answered")
        if not google_connected and total == 0:
            add_attn("google_not_connected", "important", "Google Business isn't connected",
                     "Nothing flows in until it is — reviews, drafts, alerts all start here.", "account", "Connect Google",
                     evidence="No review source")
        if google_connected and last_fetch_age is not None and last_fetch_age > 3:
            add_attn("reviews_stale", "important", f"Reviews haven't refreshed in {int(last_fetch_age)} days",
                     "The Google connection may need re-authorising. Numbers below are as of the last pull.", "account", "Check connection",
                     since=f"{int(last_fetch_age)}d")
        # trend: negative share last 2 weeks vs prior 2
        if len(sentiment) >= 4:
            last2 = sentiment[-2:]; prior2 = sentiment[-4:-2]
            ln = sum(w["negative"] for w in last2); lt = sum(w["total"] for w in last2)
            pn = sum(w["negative"] for w in prior2); pt = sum(w["total"] for w in prior2)
            if lt >= 3 and pt >= 3:
                lshare = ln / lt; pshare = pn / pt
                if lshare - pshare >= 0.15 and ln >= 2:
                    add_attn("negative_trend", "important", f"Negative reviews up to {lshare:.0%} of the last two weeks",
                             f"Up from {pshare:.0%} the two weeks before. {top_issues[0]['label'] if top_issues else 'Service'} is the most-mentioned theme.", "reviews", "See what changed",
                             since="2 weeks", evidence=f"{ln} of {lt} negative vs {pn} of {pt}")
                elif pshare - lshare >= 0.15 and pn >= 2:
                    add_win("sentiment_improving", "Fewer negative reviews", f"{lshare:.0%} of the last two weeks vs {pshare:.0%} before.", "reviews")
        # rating movement
        rating_delta = _pct_delta(avg30, prev_avg) if (n30 >= 3 and prev_n >= 3) else None
        if rating_delta is not None and rating_delta <= -0.3:
            add_attn("rating_drop", "important", f"30-day rating slipped to {avg30:.1f}★",
                     f"Down from {prev_avg:.1f}★ the previous 30 days across {n30} reviews.", "reviews", "Open reviews", since="30d")
        elif rating_delta is not None and rating_delta >= 0.2:
            add_win("rating_up", f"30-day rating up to {avg30:.1f}★", f"From {prev_avg:.1f}★ the previous 30 days.", "reviews")
        if rate >= 80 and total >= 10:
            add_win("response_rate", f"{rate:.0f}% of reviews answered", "Better than most independents — keep it there.", "reviews")

        # snapshot card
        interp = None
        if total == 0:
            interp = "No reviews yet — connect Google to start." if not google_connected else "Connected — reviews arrive on the next pull."
        elif urgent:
            interp = f"{_plural(urgent, 'urgent review')} need a reply first."
        elif rating_delta is not None and rating_delta <= -0.3:
            interp = f"Rating is slipping — {top_issues[0]['label'].lower() if top_issues else 'recent'} complaints are the theme."
        elif awaiting:
            interp = f"{_plural(awaiting, 'drafted reply')} ready to publish."
        else:
            interp = f"{_plural(n30, 'new review')} in 30 days · {rate:.0f}% answered."
        spark = [w["avg_rating"] for w in sentiment if w.get("total")] if sentiment else []
        snapshot.append({"key": "reviews", "label": "Reviews", "status": "available",
                         "value": f"{avg30:.1f}" if n30 else (f"{float(rstats.get('avg_rating') or 0):.1f}" if total else "—"),
                         "unit": "★ · 30 days" if n30 else ("★ all time" if total else ""),
                         "delta": ({"value": f"{rating_delta:+.1f}", "label": "vs prior 30d", "good": rating_delta >= 0} if rating_delta is not None else None),
                         "secondary": [{"label": "Answered", "value": f"{rate:.0f}%"}, {"label": "Urgent", "value": str(urgent)}, {"label": "Awaiting", "value": str(awaiting)}],
                         "interpretation": interp,
                         "state": "bad" if urgent else ("warn" if (awaiting or (rating_delta is not None and rating_delta <= -0.3) or (total >= 5 and rate < 50)) else ("neutral" if total == 0 else "good")),
                         "spark": spark[-8:], "spark_label": "avg rating · 8 weeks" if len(spark) > 1 else None,
                         "attention": bool(urgent or awaiting), "sample": False, "last_data": r.get("last_fetched_at")})

        # recommendations
        if awaiting >= 3:
            add_rec("publish_drafts", f"Publish the {awaiting} drafted replies", "Answered reviews rank higher and reassure the next guest reading them.",
                    f"{awaiting} replies drafted in your voice · {rate:.0f}% of reviews currently answered", "Reputation · response rate", "reviews", "Today", "strong", "Publish now")
        if top_issues and total >= 10:
            lbl, cnt = top_issues[0]['label'], int(top_issues[0].get('count') or 0)
            if cnt >= 3:
                add_rec(f"top_issue:{lbl}", f"Look into {lbl.lower()} — it's the most-mentioned complaint", "Repeat themes in negative reviews are the fixable kind.",
                        f"{lbl} raised in {cnt} reviews over 90 days", "Reviews · rating", "reviews", "This week", "strong" if cnt >= 5 else "moderate", "See the reviews")
        # changes
        if (reviews_since.get("n") or 0) > 0:
            n = reviews_since["n"]
            add_change(f"{_plural(n, 'new review')} came in" + (f" · avg {float(reviews_since.get('avg') or 0):.1f}★" if n >= 2 else "") + (f" · {_plural(int(reviews_since.get('low') or 0), 'low-star')}" if reviews_since.get("low") else ""),
                       "bad" if reviews_since.get("low") else "neutral", "reviews")
        if (replies_since.get("posted") or 0) > 0:
            add_change(f"{_plural(int(replies_since['posted']), 'reply', 'replies')} posted to Google", "good", "reviews")
        elif (replies_since.get("approved") or 0) > 0:
            add_change(f"{_plural(int(replies_since['approved']), 'reply', 'replies')} approved", "good", "reviews")
        # brief + ask
        if urgent:
            brief_lines.append({"text": f"{_plural(urgent, 'urgent review')} unanswered — start there.", "tone": "bad", "module": "reviews"})
        elif n30:
            brief_lines.append({"text": f"Rating {avg30:.1f}★ over the last 30 days" + (f", {rating_delta:+.1f} vs the month before" if rating_delta is not None else "") + f" · {_plural(n30, 'new review')}.", "tone": "bad" if (rating_delta is not None and rating_delta <= -0.3) else ("good" if (rating_delta or 0) >= 0.2 else "neutral"), "module": "reviews"})
        if total:
            ask.append("What changed in my reviews this month?")
        if top_issues:
            ask.append(f"What are guests saying about {top_issues[0]['label'].lower()}?")

    # ── Labor ───────────────────────────────────────────────────────────────
    if "labor" in active_keys:
        if not labor_live or not labor:
            freshness.append({"key": "labor", "label": "Labor", "at": None, "state": "sample", "note": "sample data — upload shifts or connect your POS"})
            snapshot.append({"key": "labor", "label": "Labor", "status": "available", "value": "—", "unit": "",
                             "delta": None, "secondary": [], "interpretation": "Showing sample data until your shifts are in. Upload a shifts export or connect Toast.",
                             "state": "sample", "spark": [], "spark_label": None, "attention": False, "sample": True, "last_data": None,
                             "setup": {"label": "Add your shifts", "module": "labor"}})
        else:
            pct = float(labor.get("overall_labor_pct") or 0)
            over = pct - labor_target
            days = int((labor.get("date_range") or {}).get("days") or 0)
            ot = [o for o in (labor.get("overtime_risk") or []) if o.get("status") == "overtime"]
            savings = float(labor.get("potential_savings") or 0)
            dow = labor.get("dow_summary") or {}
            hist_pcts = [h["labor_pct"] for h in labor_hist[::-1] if h.get("labor_pct") is not None]
            prev_pct = hist_pcts[-2] if len(hist_pcts) >= 2 else None
            delta = _pct_delta(pct, prev_pct) if prev_pct is not None else None
            src_age = _age_days(client_data.get("updated_at"), now)
            freshness.append({"key": "labor", "label": "Labor", "at": client_data.get("updated_at") or r.get("toast_last_synced"),
                              "state": "fresh" if (src_age is not None and src_age <= 8) else ("stale" if src_age is not None else "fresh"),
                              "note": ("Toast synced" if r.get("toast_restaurant_guid") and not r.get("toast_sync_error") else f"{days}-day shift export")})
            if r.get("toast_restaurant_guid") and r.get("toast_sync_error"):
                add_attn("toast_sync", "critical", "Toast sync is failing", f"Last error: {str(r['toast_sync_error'])[:120]}. Labor and depletion numbers stop updating until it's fixed.", "account", "Fix connection")
            if over > 3:
                add_attn("labor_over", "important" if over < 6 else "critical", f"Labor at {pct:.1f}% — {over:.1f} pts over your {labor_target:.0f}% target",
                         f"Across the last {days} days of shifts" + (f"; about ${savings:,.0f}/week recoverable by trimming the overstaffed days." if savings > 0 else "."), "labor", "Open labor",
                         since=f"{days}d", evidence=f"${float(labor.get('total_labor_cost') or 0):,.0f} labor on ${float(labor.get('total_sales') or 0):,.0f} sales")
            elif over <= 0:
                add_win("labor_on_target", f"Labor at {pct:.1f}% — under target", f"{abs(over):.1f} pts under your {labor_target:.0f}% target over {days} days.", "labor")
            if ot:
                add_attn("overtime", "important", f"{_plural(len(ot), 'staff member')} in overtime this week",
                         f"Roughly ${len(ot) * 38:,}+ in overtime premium at current hours.", "labor", "Open schedule",
                         evidence=", ".join(o.get("employee", o.get("name", ""))[:18] for o in ot[:3]))
            # day-of-week recommendation
            if len(dow) >= 4:
                vals = [v for v in dow.values() if v]
                mean = sum(vals) / len(vals) if vals else 0
                worst_day, worst_pct = max(dow.items(), key=lambda kv: kv[1] or 0)
                if mean and worst_pct - mean >= 4 and worst_pct > labor_target:
                    add_rec(f"trim_day:{worst_day}", f"Trim {worst_day} staffing", f"{worst_day} runs {worst_pct - mean:.0f} pts above your other days without the sales to justify it.",
                            f"{worst_day} labor {worst_pct:.1f}% vs {mean:.1f}% average · target {labor_target:.0f}%", "Labor · weekly cost", "labor", "Next schedule",
                            "strong" if worst_pct - mean >= 6 else "moderate", "Rebuild the schedule")
            if delta is not None and delta >= 1.5:
                add_change(f"Labor % rose {delta:+.1f} pts vs the previous period ({pct:.1f}%)", "bad", "labor")
            elif delta is not None and delta <= -1.5:
                add_change(f"Labor % fell {delta:+.1f} pts vs the previous period ({pct:.1f}%)", "good", "labor")
                add_win("labor_improving", f"Labor down {abs(delta):.1f} pts", f"{pct:.1f}% this period vs {prev_pct:.1f}% before.", "labor")
            if last_schedule and _ts(last_schedule.get("generated_at")) and _ts(last_schedule["generated_at"]) >= since_dt:
                hs = float(last_schedule.get("hours_scheduled") or 0); hb = float(last_schedule.get("hours_budget") or 0)
                add_change(f"New schedule built for {last_schedule.get('week_start') or 'next week'}" + (f" · {int(round(hb - hs))} hrs under budget" if hb and hs and hs < hb else ""), "good", "labor", last_schedule.get("generated_at"))
            snapshot.append({"key": "labor", "label": "Labor", "status": "available", "value": f"{pct:.1f}", "unit": "% of sales",
                             "delta": ({"value": f"{delta:+.1f} pts", "label": "vs prior period", "good": delta <= 0} if delta is not None else {"value": f"target {labor_target:.0f}%", "label": "", "good": over <= 0}),
                             "secondary": [{"label": "Target", "value": f"{labor_target:.0f}%"}, {"label": "Overtime", "value": str(len(ot))}, {"label": "Recoverable", "value": f"${savings:,.0f}/wk" if savings > 0 else "—"}],
                             "interpretation": (f"{over:.1f} pts over target. " + (f"{max(dow.items(), key=lambda kv: kv[1] or 0)[0]} is the heaviest day." if dow else "")) if over > 0 else f"On target. {min(dow.items(), key=lambda kv: kv[1] or 99)[0] if dow else ''} runs leanest.".strip(),
                             "state": "bad" if over > 6 else ("warn" if over > 0 else "good"),
                             "spark": hist_pcts[-8:], "spark_label": "labor % by period" if len(hist_pcts) > 1 else None,
                             "attention": over > 3 or bool(ot), "sample": False, "last_data": client_data.get("updated_at")})
            brief_lines.append({"text": f"Labor {pct:.1f}% against a {labor_target:.0f}% target" + (f", {delta:+.1f} pts vs last period" if delta is not None else "") + ".", "tone": "bad" if over > 3 else ("good" if over <= 0 else "neutral"), "module": "labor"})
            ask.append("Why is labor over target?" if over > 0 else "Where can I save on labor next week?")
            if last_schedule and last_schedule.get("week_end"):
                upcoming.append({"label": f"Schedule through {last_schedule['week_end']}", "when": last_schedule.get("week_end"), "module": "labor", "kind": "schedule"})

    # ── Food cost ───────────────────────────────────────────────────────────
    if "inventory" in active_keys:
        if not inv_live or not inv:
            freshness.append({"key": "inventory", "label": "Food cost", "at": None, "state": "sample", "note": "sample data — add a count or connect your POS"})
            snapshot.append({"key": "inventory", "label": "Food Cost", "status": "available", "value": "—", "unit": "",
                             "delta": None, "secondary": [], "interpretation": "Showing sample data until your inventory is in. Do a quick count or connect Toast for depletion.",
                             "state": "sample", "spark": [], "spark_label": None, "attention": False, "sample": True, "last_data": None,
                             "setup": {"label": "Add inventory", "module": "inventory"}})
        else:
            recoverable = float(inv.get("recoverable_monthly") or 0)
            waste_rate = inv.get("waste_rate_pct")
            crit = inv.get("critical_low") or []
            reorder = inv.get("reorder_soon") or []
            waste_items = inv.get("waste_items") or []
            top = waste_items[0] if waste_items else None
            inv_age = _age_days(r.get("inventory_updated_at"), now)
            freshness.append({"key": "inventory", "label": "Food cost", "at": r.get("inventory_updated_at"),
                              "state": "fresh" if (inv_age is not None and inv_age <= 8) else ("stale" if inv_age is not None else "fresh"),
                              "note": "counts current" if (inv_age is not None and inv_age <= 8) else (f"last count {int(inv_age)}d ago" if inv_age is not None else "inventory on file")})
            if inv_age is not None and inv_age > 14:
                add_attn("inventory_stale", "watch", f"Inventory last counted {int(inv_age)} days ago", "Waste and reorder flags drift the longer the count sits.", "inventory", "Quick count", since=f"{int(inv_age)}d")
            if crit:
                add_attn("critical_low", "important", f"{_plural(len(crit), 'item')} critically low",
                         ", ".join(str(c.get("item", ""))[:22] for c in crit[:4]) + " — likely to run out before the next delivery.", "inventory", "See the list",
                         evidence=f"{len(reorder)} more to reorder soon")
            if top and float(top.get("waste_cost") or 0) >= 40:
                add_rec(f"cut_waste:{top.get('item', 'item')}", f"Cut {top.get('item', 'top-item')} waste", f"It's the single biggest line in last week's waste — {top.get('waste_pct', 0)}% of what you ordered.",
                        f"${float(top.get('waste_cost') or 0):,.0f} wasted last week · ${recoverable:,.0f}/mo recoverable across items", "Food cost · margin", "inventory", "Next order",
                        "strong" if float(top.get("waste_cost") or 0) >= 100 else "moderate", "Adjust the order")
            if recoverable > 0:
                brief_lines.append({"text": f"${recoverable:,.0f}/month of recoverable food waste, led by {top.get('item') if top else 'a few items'}.", "tone": "warn", "module": "inventory"})
            else:
                add_win("waste_low", "Waste is under control", f"{inv.get('benchmark_label') or 'Low'} waste rate" + (f" ({waste_rate}%)" if waste_rate is not None else "") + " this week.", "inventory")
            snapshot.append({"key": "inventory", "label": "Food Cost", "status": "available", "value": f"${recoverable:,.0f}", "unit": "recoverable / mo",
                             "delta": ({"value": f"{waste_rate}% waste", "label": inv.get("benchmark_label") or "", "good": inv.get("benchmark_color") in ("green", None)} if waste_rate is not None else None),
                             "secondary": [{"label": "Critical low", "value": str(len(crit))}, {"label": "Reorder soon", "value": str(len(reorder))}, {"label": "Stock value", "value": f"${float(inv.get('total_stock_value') or 0):,.0f}"}],
                             "interpretation": (f"{top.get('item')} is the biggest waste line (${float(top.get('waste_cost') or 0):,.0f} last week)." if top else "No waste flagged this week."),
                             "state": "bad" if crit else ("warn" if recoverable > 0 else "good"),
                             "spark": [], "spark_label": None, "attention": bool(crit) or recoverable >= 200, "sample": False, "last_data": r.get("inventory_updated_at")})
            ask.append("Where are my biggest food cost opportunities?")

    # ── Marketing ───────────────────────────────────────────────────────────
    if "marketing" in active_keys:
        last_age = _age_days(mkt.get("last_at"), now)
        posted_age = _age_days(mkt.get("last_posted_at"), now)
        freshness.append({"key": "marketing", "label": "Marketing", "at": mkt.get("last_at"), "state": "fresh" if mkt.get("ig_connected") or mkt.get("fb_connected") else "manual",
                          "note": ("Instagram connected" if mkt.get("ig_connected") else ("Facebook connected" if mkt.get("fb_connected") else "no social account connected"))})
        if mkt.get("failed"):
            f = mkt["failed"][0]
            add_attn("post_failed", "important", f"{_plural(len(mkt['failed']), 'scheduled post')} failed to publish",
                     f"{(f.get('platform') or '').title()}: {str(f.get('error') or 'unknown error')[:100]}", "marketing", "Fix and retry")
        if not mkt.get("ig_connected") and not mkt.get("fb_connected"):
            add_attn("social_not_connected", "watch", "No social account connected", "Posts can be drafted and copied, but one-click publishing and post metrics need Instagram or Facebook connected.", "account", "Connect Instagram")
        if posted_age is not None and posted_age > 10 and (mkt.get("ig_connected") or mkt.get("fb_connected")):
            add_rec("post_this_week", "Get a post out this week", f"Nothing has gone live in {int(posted_age)} days; accounts that post weekly hold reach.",
                    f"last post {int(posted_age)}d ago · {mkt.get('month', 0)} pieces drafted this month", "Marketing · reach", "marketing", "This week", "moderate", "Draft a post")
        elif mkt.get("last_at") is None:
            add_rec("first_post", "Generate your first post", "Cavnar writes it in your voice from your reviews and menu — one click.", "no marketing content yet", "Marketing · reach", "marketing", "Today", "early", "Generate a post")
        if mkt.get("posted_since"):
            add_change(f"{_plural(mkt['posted_since'], 'scheduled post')} went live", "good", "marketing")
        for p in mkt.get("scheduled") or []:
            upcoming.append({"label": f"{(p.get('platform') or '').title()} post · {p.get('topic') or p.get('content_type') or 'scheduled'}", "when": p.get("scheduled_for"), "module": "marketing", "kind": "post"})
        snapshot.append({"key": "marketing", "label": "Marketing", "status": "available", "value": str(mkt.get("month", 0)), "unit": "pieces this month",
                         "delta": ({"value": f"{mkt.get('week', 0)} this week", "label": "", "good": (mkt.get("week") or 0) > 0}),
                         "secondary": [{"label": "Scheduled", "value": str(len(mkt.get("scheduled") or []))}, {"label": "Last live", "value": (f"{int(posted_age)}d ago" if posted_age is not None else "—")}, {"label": "Channels", "value": ", ".join(x for x, on in (("IG", mkt.get("ig_connected")), ("FB", mkt.get("fb_connected"))) if on) or "none"}],
                         "interpretation": (f"Next post {mkt['scheduled'][0].get('scheduled_for', '')[:10]} on {(mkt['scheduled'][0].get('platform') or '').title()}." if mkt.get("scheduled") else ("Nothing scheduled — draft something for this week." if mkt.get("ig_connected") or mkt.get("fb_connected") else "Connect Instagram to publish and track posts from here.")),
                         "state": "bad" if mkt.get("failed") else ("warn" if (posted_age is not None and posted_age > 10) or not (mkt.get("ig_connected") or mkt.get("fb_connected")) else "good"),
                         "spark": [], "spark_label": None, "attention": bool(mkt.get("failed")), "sample": False, "last_data": mkt.get("last_at")})
        ask.append("What should I post about this week?")

    # ── Intel ───────────────────────────────────────────────────────────────
    if intel is not None:
        age = _age_days(intel.get("updated_at"), now)
        freshness.append({"key": "intel", "label": "Intel", "at": intel.get("updated_at"), "state": "fresh" if (age is not None and age <= 8) else ("stale" if age is not None else "missing"),
                          "note": f"{intel['competitors']} competitors tracked" if intel["competitors"] else "no competitors added"})
        snapshot.append({"key": "intel", "label": "Intel", "status": "available", "value": str(intel["recs"]) if intel["competitors"] else "—", "unit": "recommendations" if intel["competitors"] else "",
                         "delta": None, "secondary": [{"label": "Competitors", "value": str(intel["competitors"])}, {"label": "Updated", "value": (f"{int(age)}d ago" if age is not None else "—")}],
                         "interpretation": "Weekly competitor read is ready." if intel["recs"] else "Add competitors on the Intel tab to get a weekly comparison.",
                         "state": "good" if intel["recs"] else "neutral", "spark": [], "spark_label": None, "attention": False, "sample": False, "last_data": intel.get("updated_at")})
        if intel["recs"] and age is not None and age <= 8:
            add_rec("intel_recs", f"{_plural(intel['recs'], 'competitor move')} worth a look", "The weekly Intel pass found things nearby restaurants are doing that you aren't.",
                    f"{intel['competitors']} competitors compared {int(age)}d ago", "Intel · positioning", "intel", "This week", "moderate", "Open Intel")

    # ── coming-soon modules (compact, never data) ──────────────────────────
    for m in active:
        if m["status"] == "coming_soon":
            snapshot.append({"key": m["key"], "label": m["label"], "status": "coming_soon", "value": "", "unit": "", "delta": None, "secondary": [],
                             "interpretation": "Coming soon.", "state": "neutral", "spark": [], "spark_label": None, "attention": False, "sample": False, "last_data": None})

    # ── alerts (unresolved = still needing action) ──────────────────────────
    alert_items = []
    from client_api import _NOTIFICATION_LABELS, _NOTIFICATION_MODULE
    seen = set()
    for a in recent_alerts:
        k = (a["alert_type"], a.get("review_id"))
        if k in seen:
            continue
        seen.add(k)
        resolved = a.get("review_id") and a.get("response_status") in ("posted", "approved", "skipped")
        alert_items.append({"type": a["alert_type"], "label": _NOTIFICATION_LABELS.get(a["alert_type"], a["alert_type"]),
                            "fired_at": a["fired_at"], "review_id": a.get("review_id"), "module": _NOTIFICATION_MODULE.get(a["alert_type"], "reviews"),
                            "resolved": bool(resolved), "new": (_ts(a["fired_at"]) or now) >= since_dt,
                            "severity": "critical" if a["alert_type"] in ("1star", "health", "neg_spike") else ("important" if a["alert_type"] in ("2star", "negative_trend", "rating_threshold", "labor_over", "no_response") else "watch")})
    if alerts_since:
        add_change(f"{_plural(alerts_since, 'alert')} fired", "warn", "alerts")

    # ── upcoming: next review pull, digest, quiet hours ────────────────────
    from zoneinfo import ZoneInfo
    try:
        ct = ZoneInfo("America/Chicago")
    except Exception:
        ct = timezone.utc
    if "reviews" in active_keys and google_connected:
        now_ct = datetime.now(ct)
        nxt = next((h for h in _REVIEW_FETCH_HOURS_CT if h > now_ct.hour), None)
        nxt_dt = now_ct.replace(hour=nxt, minute=0, second=0, microsecond=0) if nxt else (now_ct + timedelta(days=1)).replace(hour=_REVIEW_FETCH_HOURS_CT[0], minute=0, second=0, microsecond=0)
        upcoming.append({"label": "Next review pull", "when": nxt_dt.astimezone(restaurant_now(restaurant).tzinfo).isoformat(), "module": "reviews", "kind": "fetch"})
    digest_day = (r.get("digest_day") or "monday").lower()
    days_map = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if digest_day in days_map:
        ahead = (days_map.index(digest_day) - local_now.weekday()) % 7
        if ahead == 0 and local_now.hour >= 9:
            ahead = 7
        upcoming.append({"label": "Weekly digest email", "when": (local_now + timedelta(days=ahead)).replace(hour=9, minute=0, second=0, microsecond=0).isoformat(), "module": "account", "kind": "digest"})
    upcoming.sort(key=lambda u: u.get("when") or "")

    # ── order, brief headline, empty states ────────────────────────────────
    sev_rank = {"critical": 0, "important": 1, "watch": 2}
    attention.sort(key=lambda a: sev_rank[a["severity"]])
    for a in attention:
        a["severity_rank"] = sev_rank[a["severity"]]
    critical = sum(1 for a in attention if a["severity"] == "critical")
    important = sum(1 for a in attention if a["severity"] == "important")
    if critical:
        headline = f"{_plural(critical, 'thing')} need{'s' if critical == 1 else ''} you now"
        headline_tone = "bad"
    elif important:
        headline = f"{_plural(important, 'thing')} worth handling today"
        headline_tone = "warn"
    elif attention:
        headline = "Steady — a couple of things to watch"
        headline_tone = "neutral"
    elif wins:
        headline = "Running well — nothing needs you right now"
        headline_tone = "good"
    else:
        headline = "Quiet so far today"
        headline_tone = "neutral"

    overnight = _mob._home_overnight(rid)
    receipts = _mob._home_weekly_receipts(rid, active_keys, inv if inv_live else {})
    checklist = _mob._setup_checklist(restaurant, rstats, labor, active_keys)
    from value_delivered import compute_total_value_delivered, record_value_snapshot, get_value_history
    total_value = compute_total_value_delivered(rid)
    try:
        record_value_snapshot(rid, total_value)
    except Exception:
        pass
    value_history = get_value_history(rid, days=365)

    has_any_data = bool(rstats.get("total")) or labor_live or inv_live or bool(mkt.get("last_at") if mkt else False)
    empty_state = None
    if not has_any_data:
        empty_state = {"kind": "new_account", "title": f"Welcome{', ' + (restaurant.owner_name or current_user.get('username') or '') if (restaurant.owner_name or current_user.get('username')) else ''}.",
                       "body": "Your brief fills in as data arrives: reviews the moment Google is connected, labor once shifts are in, food cost after a first count. Start with the checklist."}

    dismissed_recs = [r for r in recs if r["key"] in dismissed]
    recs = [r for r in recs if r["key"] not in dismissed]

    # ── quick actions ──────────────────────────────────────────────────────
    quick = []
    if "reviews" in active_keys and int(rstats.get("awaiting_approval") or 0):
        quick.append({"key": "publish", "label": f"Publish {min(int(rstats['awaiting_approval']), 25)} replies", "kind": "publish_replies", "module": "reviews", "count": int(rstats["awaiting_approval"])})
    if "reviews" in active_keys and int(rstats.get("urgent") or 0):
        quick.append({"key": "urgent", "label": "Answer urgent reviews", "kind": "open_module", "module": "reviews", "count": int(rstats["urgent"])})
    quick.append({"key": "ask", "label": "Ask Cavnar AI", "kind": "ask", "module": None, "count": None})
    if "labor" in active_keys and labor_live:
        quick.append({"key": "schedule", "label": "Build next week's schedule", "kind": "open_module", "module": "labor", "count": None})
    if "inventory" in active_keys and inv_live and (inv.get("critical_low") or inv.get("reorder_soon")):
        quick.append({"key": "order", "label": "Review the order draft", "kind": "open_module", "module": "inventory", "count": len(inv.get("reorder_soon") or []) + len(inv.get("critical_low") or [])})
    if "marketing" in active_keys:
        quick.append({"key": "post", "label": "Draft a post", "kind": "open_module", "module": "marketing", "count": None})
    quick.append({"key": "alerts", "label": "All alerts", "kind": "alerts", "module": None, "count": sum(1 for a in alert_items if a["new"] and not a["resolved"]) or None})
    quick = quick[:6]
    ask = (["What should I focus on today?"] + ask)[:5]

    # ── location context ───────────────────────────────────────────────────
    locations = []
    group_name = None
    if current_user.get("role") == "owner":
        try:
            from models import get_location_group
            base = get_restaurant(current_user.get("base_restaurant_id") or rid)
            if base and base.location_group:
                group_name = base.location_group
                c2 = get_conn()
                for lr in get_location_group(base.location_group):
                    sig = _location_signal(c2, lr, now)
                    sig["active"] = lr["id"] == rid
                    locations.append(sig)
                c2.close()
        except Exception:
            locations = []
    portfolio = None
    if len(locations) > 1:
        needing = [l for l in locations if l["health"] in ("critical", "important")]
        best = max((l for l in locations if l.get("rating_30d")), key=lambda l: (l["rating_30d"], l["reviews_30d"]), default=None)
        worst = max(locations, key=lambda l: ({"critical": 3, "important": 2, "watch": 1, "healthy": 0}[l["health"]], l["attention"]))
        portfolio = {"total": len(locations), "healthy": sum(1 for l in locations if l["health"] == "healthy"), "needing": len(needing),
                     "biggest_issue": ({"location": worst["name"], "id": worst["id"], "issue": worst["top_issue"]} if worst["top_issue"] else None),
                     "strongest": ({"location": best["name"], "id": best["id"], "rating": best["rating_30d"]} if best else None)}

    payload = {
        "ok": True,
        "generated_at": _iso(now),
        "local_now": local_now.isoformat(),
        "greeting_name": (restaurant.owner_name or current_user.get("username") or "").split(" ")[0].title() if (restaurant.owner_name or current_user.get("username")) else None,
        "context": {"restaurant_name": restaurant.name, "location_name": restaurant.location_name or None, "group_name": group_name,
                    "view": "location" if group_name else "single", "locations": locations, "portfolio": portfolio, "timezone": str(local_now.tzinfo)},
        "freshness": freshness,
        "brief": {"headline": headline, "tone": headline_tone, "lines": brief_lines[:4], "overnight": overnight,
                  "counts": {"critical": critical, "important": important, "watch": sum(1 for a in attention if a["severity"] == "watch"), "wins": len(wins)}},
        "attention": attention[:8],
        "wins": wins[:4],
        "snapshot": snapshot,
        "recommendations": recs[:5],
        "dismissed": [{"key": r["key"], "title": r["title"], "until": dismissed[r["key"]]["expires_at"]} for r in dismissed_recs],
        "ai_insight": ai_insight,
        "changes": {"since": _iso(since_dt), "since_label": since_label, "items": changes[:8]},
        "alerts": alert_items[:8],
        "quick_actions": quick,
        "ask_suggestions": ask,
        "upcoming": upcoming[:5],
        "value": {"total": total_value, "history": value_history},
        "receipts": receipts,
        "setup_checklist": checklist,
        "quiet_hours_active": is_in_quiet_hours(rid),
        "alert_quiet_end": r.get("alert_quiet_end"),
        "empty_state": empty_state,
    }
    return payload, 200


# ── consolidated view: every location in the owner's group ─────────────────

def _location_record(conn, r, now):
    """One location's row for the consolidated view. Cheap by default; the
    labor and inventory analyses run only when that location has live data,
    so a seven-location owner doesn't pay for seven sample analyses."""
    rid = r["id"]
    sig = _location_signal(conn, r, now)
    rs = _one(conn, """SELECT COUNT(*) AS total, SUM(response_status IN ('posted','approved')) AS responded,
                              SUM(response_status='drafted') AS awaiting,
                              SUM(urgency='high' AND response_status NOT IN ('posted','approved','skipped')) AS urgent,
                              ROUND(AVG(CASE WHEN review_date >= date('now','-30 days') THEN rating END),1) AS avg30,
                              SUM(review_date >= date('now','-30 days')) AS n30,
                              ROUND(AVG(CASE WHEN review_date >= date('now','-60 days') AND review_date < date('now','-30 days') THEN rating END),1) AS avg_prev,
                              SUM(rating<=2 AND review_date >= date('now','-7 days')) AS low7
                       FROM reviews WHERE restaurant_id=? AND processed=1 AND deleted_at IS NULL""", (rid,)) or {}
    total = int(rs.get("total") or 0)
    rate = round(100.0 * int(rs.get("responded") or 0) / total) if total else None
    last_active = (_one(conn, "SELECT MAX(created_at) AS t FROM login_history WHERE restaurant_id=?", (rid,)) or {}).get("t")
    labor = None
    cd = _one(conn, "SELECT shifts_csv IS NOT NULL AND shifts_csv != '' AS live FROM client_data WHERE restaurant_id=?", (rid,)) or {}
    if r.get("module_labor") and cd.get("live"):
        try:
            from labor import analyse_shifts_for_restaurant
            la = analyse_shifts_for_restaurant(rid)
            if la.get("is_live"):
                target = float(r.get("labor_target_pct") or 30.0)
                labor = {"pct": float(la.get("overall_labor_pct") or 0), "target": target,
                         "over": round(float(la.get("overall_labor_pct") or 0) - target, 1),
                         "overtime": sum(1 for o in (la.get("overtime_risk") or []) if o.get("status") == "overtime")}
        except Exception:
            labor = None
    inv = None
    if r.get("module_inventory"):
        try:
            from inventory import load_inventory_for_restaurant, analyse_inventory
            items, live = load_inventory_for_restaurant(rid)
            if live and items:
                a = analyse_inventory(items)
                inv = {"recoverable": float(a.get("recoverable_monthly") or 0), "critical_low": len(a.get("critical_low") or []), "waste_rate": a.get("waste_rate_pct")}
        except Exception:
            inv = None
    issues = []
    if sig["top_issue"]:
        issues.append({"severity": sig["health"] if sig["health"] != "healthy" else "watch", "text": sig["top_issue"], "module": "reviews"})
    if labor and labor["over"] > 3:
        issues.append({"severity": "critical" if labor["over"] >= 6 else "important", "text": f"Labor {labor['pct']:.1f}% — {labor['over']:.1f} pts over target", "module": "labor"})
    if labor and labor["overtime"]:
        issues.append({"severity": "important", "text": f"{_plural(labor['overtime'], 'staff member')} in overtime", "module": "labor"})
    if inv and inv["critical_low"]:
        issues.append({"severity": "important", "text": f"{_plural(inv['critical_low'], 'item')} critically low", "module": "inventory"})
    avg30 = rs.get("avg30"); prev = rs.get("avg_prev")
    if avg30 and prev and (rs.get("n30") or 0) >= 3 and avg30 - prev <= -0.3:
        issues.append({"severity": "important", "text": f"Rating slipped to {avg30:.1f}★ (from {prev:.1f}★)", "module": "reviews"})
    rank = {"critical": 3, "important": 2, "watch": 1}
    worst = max((rank[i["severity"]] for i in issues), default=0)
    health = {3: "critical", 2: "important", 1: "watch", 0: "healthy"}[worst]
    issues.sort(key=lambda i: -rank[i["severity"]])
    return {"id": rid, "name": r.get("location_name") or r["name"], "restaurant_name": r["name"], "health": health,
            "issues": issues, "attention": len(issues),
            "reviews": {"total": total, "rating_30d": avg30, "reviews_30d": int(rs.get("n30") or 0), "rating_prev": prev,
                        "response_rate": rate, "urgent": int(rs.get("urgent") or 0), "awaiting": int(rs.get("awaiting") or 0), "low_7d": int(rs.get("low7") or 0)},
            "labor": labor, "inventory": inv,
            "google_connected": bool(r.get("gmb_refresh_token") or r.get("reviews_live")),
            "last_active": last_active, "last_fetched_at": r.get("last_fetched_at")}


def build_group_brief(current_user, fresh=False):
    """The consolidated view for an owner with several locations: one row per
    location, the attention list across all of them, and the portfolio
    summary. Numbers are never averaged across locations — the strongest and
    weakest are named, and every metric stays attached to its location."""
    key = ("group", current_user.get("base_restaurant_id") or current_user["restaurant_id"], current_user.get("id"))
    if not fresh:
        hit = _CACHE.get(key)
        if hit and (datetime.now(timezone.utc) - hit[0]).total_seconds() < _CACHE_TTL:
            return hit[1], 200
    if current_user.get("role") != "owner":
        return {"ok": False, "error": "Only the owner login sees all locations"}, 403
    from models import get_location_group
    base = get_restaurant(current_user.get("base_restaurant_id") or current_user["restaurant_id"])
    if not base or not base.location_group:
        return {"ok": False, "error": "No location group on this account"}, 400
    now = datetime.now(timezone.utc)
    conn = get_conn()
    locs = [_location_record(conn, r, now) for r in get_location_group(base.location_group)]
    conn.close()
    for l in locs:
        l["active"] = l["id"] == current_user["restaurant_id"]
    rank = {"critical": 0, "important": 1, "watch": 2}
    attention = []
    for l in locs:
        for i in l["issues"]:
            attention.append({**i, "location": l["name"], "restaurant_id": l["id"], "severity_rank": rank[i["severity"]]})
    attention.sort(key=lambda a: (a["severity_rank"], a["location"]))
    rated = [l for l in locs if l["reviews"]["rating_30d"] and l["reviews"]["reviews_30d"] >= 3]
    best = max(rated, key=lambda l: (l["reviews"]["rating_30d"], l["reviews"]["reviews_30d"]), default=None)
    worst = min(rated, key=lambda l: (l["reviews"]["rating_30d"], -l["reviews"]["reviews_30d"]), default=None)
    heaviest = max([l for l in locs if l["labor"]], key=lambda l: l["labor"]["over"], default=None)
    critical = sum(1 for a in attention if a["severity"] == "critical")
    needing = [l for l in locs if l["health"] in ("critical", "important")]
    if critical:
        headline = f"{_plural(critical, 'critical item')} across {_plural(len(set(a['location'] for a in attention if a['severity']=='critical')), 'location')}"; tone = "bad"
    elif needing:
        headline = f"{_plural(len(needing), 'location')} need{'s' if len(needing) == 1 else ''} a look"; tone = "warn"
    else:
        headline = "All locations healthy"; tone = "good"
    payload = {
        "ok": True, "scope": "group", "generated_at": _iso(now), "group_name": base.location_group,
        "greeting_name": (base.owner_name or current_user.get("username") or "").split(" ")[0].title() or None,
        "headline": headline, "tone": tone,
        "locations": locs, "attention": attention[:12],
        "portfolio": {"total": len(locs), "healthy": sum(1 for l in locs if l["health"] == "healthy"), "needing": len(needing),
                      "urgent_reviews": sum(l["reviews"]["urgent"] for l in locs), "awaiting": sum(l["reviews"]["awaiting"] for l in locs),
                      "strongest": ({"location": best["name"], "id": best["id"], "rating": best["reviews"]["rating_30d"]} if best else None),
                      "weakest": ({"location": worst["name"], "id": worst["id"], "rating": worst["reviews"]["rating_30d"]} if worst and worst is not best else None),
                      "heaviest_labor": ({"location": heaviest["name"], "id": heaviest["id"], "pct": heaviest["labor"]["pct"], "over": heaviest["labor"]["over"]} if heaviest and heaviest["labor"]["over"] > 0 else None),
                      "biggest_issue": ({"location": attention[0]["location"], "id": attention[0]["restaurant_id"], "issue": attention[0]["text"]} if attention else None)},
    }
    _CACHE[key] = (datetime.now(timezone.utc), payload)
    return payload, 200
