"""
admin_ops.py — the data layer behind the admin console.

Everything the console shows is computed here, once, from tables that already
exist (restaurants, users, ai_usage, email_log, push_deliveries, device_tokens,
job_failures, job_runs, activity_log, login_history, alert_log, webhooks…) so
every page — Overview, Clients, Integrations, Billing, Jobs — reads the same
health record for a location and can never disagree about whether it is fine.

The model is OWNER (a users row) → BRAND (restaurants.location_group, or the
single restaurant itself) → LOCATION (a restaurants row). Health only ever
rolls UP: a brand is as healthy as its worst location.
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta

from models import get_conn, get_restaurant

log = logging.getLogger(__name__)

# MRR by module count. This used to be its own {1: 349, 2: 649, ...} literal,
# which is how the 2- and 3-module prices ended up living in three places and
# disagreeing in one of them: this table and pricing.html both said $649/$899
# while pricing.py — the one that actually bills — said $698/$1,047. Read it
# from pricing.py so there is nothing left to drift.
from pricing import TIERS as _TIERS
MONTHLY_BY_MODULES = {n: t["monthly"] for n, t in _TIERS.items()}

INTEGRATIONS = ("google_business", "toast", "square", "clover", "rpower", "instagram", "webhook")


# ── time helpers ─────────────────────────────────────────────────────────────

# Two stamp conventions live in this database: sqlite's datetime('now')
# writes UTC with a space ("2026-09-06 23:17:24"); Python's isoformat()
# writes local time with a T ("2026-09-06T18:17:24"). Everything here is
# normalised to naive LOCAL time so ages and "since" are right for both.


def _parse(ts):
    """A stored stamp as NAIVE server-local time, through the one parser
    (time_utils.parse_stamp, CA3 F15): an offset or Z is that instant,
    SQLite's space form is UTC, and a naive 'T' stamp or bare date is read
    as server-local — this module's rule since it was written."""
    from time_utils import parse_stamp
    d = parse_stamp(ts, naive_tz="local")
    return None if d is None else d.astimezone().replace(tzinfo=None)


def _age_hours(ts):
    d = _parse(ts)
    return None if d is None else max(0.0, (datetime.now() - d).total_seconds() / 3600)


def _age_days(ts):
    h = _age_hours(ts)
    return None if h is None else h / 24


def _since(ts):
    """'3h', '2d', '5w' — how long ago, for the attention list."""
    h = _age_hours(ts)
    if h is None:
        return "—"
    if h < 1:
        return f"{int(h * 60)}m"
    if h < 48:
        return f"{int(h)}h"
    d = h / 24
    if d < 14:
        return f"{int(d)}d"
    return f"{int(d / 7)}w"


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ── raw loads ────────────────────────────────────────────────────────────────

def _rows_dict(conn, sql, args=()):
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception:
        return []


def _one_dict(conn, sql, args=()):
    try:
        r = conn.execute(sql, args).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def _load_everything():
    """One connection, one pass: every per-restaurant signal the pages need."""
    conn = get_conn()
    try:
        import admin_events as _ae
        _ae._ensure(conn)
        import ai_utils as _ai
        conn.execute(_ai._USAGE_TABLE_SQL)
        _ai._ensure_usage_columns(conn)
        conn.commit()
    except Exception:
        pass
    now = datetime.now()
    day = _stamp(now - timedelta(days=1))
    week = _stamp(now - timedelta(days=7))
    month = _stamp(now - timedelta(days=30))
    today = now.strftime("%Y-%m-%d")

    rests = _rows_dict(conn, "SELECT * FROM restaurants ORDER BY id")
    users = _rows_dict(conn, "SELECT id, restaurant_id, username, email, role, is_admin, is_active, created_at, last_login FROM users")
    by_rid = {}
    for u in users:
        by_rid.setdefault(u["restaurant_id"], []).append(u)

    def per_rid(sql, args=(), key="restaurant_id"):
        out = {}
        for r in _rows_dict(conn, sql, args):
            out[r[key]] = r
        return out

    reviews = per_rid("""SELECT restaurant_id,
                               COUNT(*) AS total,
                               SUM(CASE WHEN response_status='drafted' THEN 1 ELSE 0 END) AS awaiting,
                               SUM(CASE WHEN response_status IN ('approved','posted') THEN 1 ELSE 0 END) AS responded,
                               SUM(CASE WHEN urgency='high' AND response_status NOT IN ('approved','posted','skipped')
                                         AND julianday(fetched_at) < julianday('now','-2 days') THEN 1 ELSE 0 END) AS urgent_stale,
                               MAX(fetched_at) AS last_review_at
                        FROM reviews WHERE deleted_at IS NULL GROUP BY restaurant_id""")
    ai_month = per_rid("""SELECT restaurant_id, COUNT(*) AS calls, ROUND(SUM(cost_usd),4) AS cost,
                                 SUM(input_tokens)+SUM(output_tokens) AS tokens, MAX(created_at) AS last_at,
                                 SUM(CASE WHEN COALESCE(status,'ok')='error' THEN 1 ELSE 0 END) AS failed
                          FROM ai_usage WHERE created_at >= ? GROUP BY restaurant_id""", (month,))
    ai_failed_week = per_rid("SELECT restaurant_id, COUNT(*) AS n, MAX(created_at) AS last_at, MAX(error) AS sample FROM ai_usage WHERE created_at >= ? AND COALESCE(status,'ok')='error' GROUP BY restaurant_id", (week,))
    ai_today = per_rid("SELECT restaurant_id, COUNT(*) AS calls, ROUND(SUM(cost_usd),4) AS cost FROM ai_usage WHERE created_at >= ? GROUP BY restaurant_id", (today,))
    ai_prev = per_rid("SELECT restaurant_id, COUNT(*) AS calls FROM ai_usage WHERE created_at >= ? AND created_at < ? GROUP BY restaurant_id",
                      (_stamp(now - timedelta(days=14)), week))
    ai_week = per_rid("SELECT restaurant_id, COUNT(*) AS calls FROM ai_usage WHERE created_at >= ? GROUP BY restaurant_id", (week,))
    emails = per_rid("""SELECT restaurant_id,
                              SUM(CASE WHEN sent_at >= ? THEN 1 ELSE 0 END) AS sent_7d,
                              SUM(CASE WHEN sent_at >= ? AND status='failed' THEN 1 ELSE 0 END) AS failed_7d,
                              MAX(CASE WHEN status='failed' THEN sent_at END) AS last_failed_at,
                              MAX(sent_at) AS last_sent_at
                       FROM email_log GROUP BY restaurant_id""", (week, week))
    pushes = per_rid("""SELECT restaurant_id,
                              SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS sent_7d,
                              SUM(CASE WHEN created_at >= ? AND ok=0 THEN 1 ELSE 0 END) AS failed_7d,
                              MAX(CASE WHEN ok=0 THEN created_at END) AS last_failed_at
                       FROM push_deliveries GROUP BY restaurant_id""", (week, week))
    tokens = per_rid("""SELECT restaurant_id, COUNT(*) AS devices,
                              SUM(CASE WHEN disabled_reason IS NOT NULL AND disabled_reason != '' THEN 1 ELSE 0 END) AS disabled
                       FROM device_tokens GROUP BY restaurant_id""")
    alerts = per_rid("SELECT restaurant_id, COUNT(*) AS fired_7d, MAX(fired_at) AS last_at FROM alert_log WHERE fired_at >= ? GROUP BY restaurant_id", (week,))
    webhooks = per_rid("SELECT restaurant_id, url, is_active, consecutive_failures, last_status, last_fired_at, disabled_reason FROM webhooks")
    client_data = per_rid("SELECT restaurant_id, shifts_csv IS NOT NULL AND shifts_csv != '' AS has_shifts, inventory_csv IS NOT NULL AND inventory_csv != '' AS has_inventory, updated_at FROM client_data")
    # last_count_at: the newest physical count (the column ordering reads).
    # restaurants.inventory_updated_at is never written by anything, so
    # inventory read "fresh" forever off it (CA3 F9).
    ingredients = per_rid("SELECT restaurant_id, COUNT(*) AS n, MAX(updated_at) AS cost_updated_at, "
                          "MAX(last_recount_at) AS last_count_at "
                          "FROM ingredients WHERE COALESCE(is_active,1)=1 GROUP BY restaurant_id")
    recipes = per_rid("""SELECT mi.restaurant_id AS restaurant_id, COUNT(DISTINCT mi.id) AS items,
                                COUNT(DISTINCT ri.menu_item_id) AS mapped
                         FROM menu_items mi LEFT JOIN recipe_ingredients ri ON ri.menu_item_id=mi.id
                         GROUP BY mi.restaurant_id""")
    labor_days = per_rid("SELECT restaurant_id, COUNT(DISTINCT date) AS n, MAX(date) AS last FROM labor_daily_history "
                         "WHERE date >= date('now','-30 days') AND sales > 0 GROUP BY restaurant_id")
    # The last day the labor data COVERS (a day with sales), not when a file
    # was last written — a sync that keeps landing shifts with no sales is
    # not current data (CA3 F2/F3).
    labor_last = per_rid("SELECT restaurant_id, MAX(date) AS last FROM labor_daily_history "
                         "WHERE sales > 0 GROUP BY restaurant_id")
    contacts = per_rid("SELECT restaurant_id, COUNT(*) AS n, SUM(COALESCE(sms_consent,0)) AS consented "
                       "FROM alert_contacts GROUP BY restaurant_id")
    routing = per_rid("SELECT restaurant_id, COUNT(*) AS n FROM issue_routing GROUP BY restaurant_id")
    stale_issues = per_rid("SELECT restaurant_id, COUNT(*) AS n FROM ops_issues WHERE status='open' "
                           "AND created_at <= datetime('now','-24 hours') GROUP BY restaurant_id")
    marketing = per_rid("SELECT restaurant_id, COUNT(*) AS pieces, MAX(created_at) AS last_at FROM marketing_content_log GROUP BY restaurant_id")
    sched_posts = per_rid("SELECT restaurant_id, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending FROM marketing_scheduled_posts GROUP BY restaurant_id")
    schedules = per_rid("SELECT restaurant_id, MAX(generated_at) AS last_at, COUNT(*) AS n FROM schedule_history GROUP BY restaurant_id")
    logins = per_rid("SELECT restaurant_id, MAX(created_at) AS last_at, COUNT(*) AS n FROM login_history GROUP BY restaurant_id")
    sessions = per_rid("""SELECT u.restaurant_id AS restaurant_id, COUNT(*) AS n FROM sessions s JOIN users u ON u.id=s.user_id
                          WHERE s.expires_at > datetime('now') GROUP BY u.restaurant_id""")
    guests = per_rid("SELECT restaurant_id, COUNT(*) AS n FROM guest_contacts WHERE consent=1 AND unsubscribed=0 GROUP BY restaurant_id")
    job_failures = _rows_dict(conn, "SELECT id, job, error, context, created_at FROM job_failures WHERE created_at >= ? ORDER BY id DESC", (week,))
    resolved = {r["key"]: r for r in _rows_dict(conn, "SELECT key, resolved_at, note FROM admin_issue_resolutions")}
    conn.close()

    return dict(now=now, rests=rests, users=by_rid, reviews=reviews, ai_month=ai_month, ai_today=ai_today,
                ai_prev=ai_prev, ai_week=ai_week, ai_failed_week=ai_failed_week, emails=emails, pushes=pushes, tokens=tokens, alerts=alerts,
                webhooks=webhooks, client_data=client_data, ingredients=ingredients, marketing=marketing,
                recipes=recipes, labor_days=labor_days, labor_last=labor_last, contacts=contacts, routing=routing,
                stale_issues=stale_issues,
                sched_posts=sched_posts, schedules=schedules, logins=logins, sessions=sessions, guests=guests,
                job_failures=job_failures, resolved=resolved)


# ── the per-location health record ──────────────────────────────────────────

def _pos_integration(r, name, label, configured, auth):
    """One POS provider's row, judged through pos_health.provider_state —
    the same provider-agnostic reading every other surface uses (CA3 F6) —
    so RPOWER is listed and a sync that stopped days ago without writing an
    error is not "connected"."""
    import pos_health
    st = pos_health.provider_state(r, name)
    err = r.get(f"{name}_sync_error") or None
    if not err and configured and st.get("state") == "stale":
        err = f"No successful sync in {int(st.get('age_days') or 0)} days"
    return {"key": name, "label": label,
            "connected": bool(configured) and st.get("state") in ("current", "aging"),
            "configured": bool(configured),
            "last_success": r.get(f"{name}_last_synced"), "error": err,
            "sync_state": st.get("state"), "age_days": st.get("age_days"),
            "auth": auth}


def review_source(r):
    """(source, label) for how this location's Google reviews arrive.
    Places — a place id with reviews_live and no Business Profile — returns
    at most five reviews a fetch, so it is a SAMPLE of the listing and never
    "Google Business connected" (CA3 F13)."""
    if (r.get("gmb_refresh_token") if isinstance(r, dict) else getattr(r, "gmb_refresh_token", None)):
        return "gbp", "Google Business"
    live = r.get("reviews_live") if isinstance(r, dict) else getattr(r, "reviews_live", 0)
    place = r.get("google_place_id") if isinstance(r, dict) else getattr(r, "google_place_id", None)
    if live and place:
        return "places_sampled", PLACES_SAMPLED_LABEL
    return "none", "Google Business"


PLACES_SAMPLED_LABEL = "Google reviews (sampled — Places returns 5 at a time)"
_POS_LABELS = {"toast": "Toast", "square": "Square", "clover": "Clover", "rpower": "RPOWER"}


def _integrations_for(r, hooks):
    """Every external connection, in one shape: state, last success, error."""
    ig_exp = _parse(r.get("ig_token_expires"))
    ig_days_left = None if not ig_exp else (ig_exp - datetime.now()).days
    source, g_label = review_source(r)
    out = [
        {"key": "google_business", "label": g_label, "source": source,
         "connected": bool(r.get("gmb_refresh_token")),
         "configured": bool(r.get("google_place_id") or r.get("gmb_location_id")),
         "last_success": r.get("last_fetched_at"), "error": None,
         "auth": "oauth" if r.get("gmb_refresh_token") else ("place id only" if r.get("google_place_id") else "none")},
        _pos_integration(r, "toast", "Toast POS", bool(r.get("toast_restaurant_guid")),
                         "credentials" if r.get("toast_client_id") else "none"),
        _pos_integration(r, "square", "Square POS", bool(r.get("square_access_token")),
                         "token" if r.get("square_access_token") else "none"),
        _pos_integration(r, "clover", "Clover POS", bool(r.get("clover_api_token")),
                         "token" if r.get("clover_api_token") else "none"),
        _pos_integration(r, "rpower", "RPOWER POS", bool(r.get("rpower_token") or r.get("rpower_store_mid")),
                         "token" if r.get("rpower_token") else "none"),
        {"key": "instagram", "label": "Instagram & Facebook",
         "connected": bool(r.get("ig_token")) and (ig_days_left is None or ig_days_left >= 0),
         "configured": bool(r.get("ig_token")),
         "last_success": None,
         "error": (f"Token expired {abs(ig_days_left)}d ago" if (r.get("ig_token") and ig_days_left is not None and ig_days_left < 0)
                   else (f"Token expires in {ig_days_left}d" if (r.get("ig_token") and ig_days_left is not None and ig_days_left <= 7) else None)),
         "auth": "oauth" if r.get("ig_token") else "none"},
    ]
    hook = hooks.get(r["id"])
    out.append({"key": "webhook", "label": "Outbound webhook",
                "connected": bool(hook and hook.get("is_active")),
                "configured": bool(hook),
                "last_success": hook.get("last_fired_at") if hook else None,
                "error": (hook.get("disabled_reason") or (f"{hook['consecutive_failures']} consecutive failures" if hook.get("consecutive_failures") else None)) if hook else None,
                "auth": "secret" if hook else "none"})
    # Only integrations the location actually uses count toward health.
    for i in out:
        i["in_use"] = i["configured"]
        i["state"] = ("error" if (i["configured"] and i["error"]) else "connected" if i["connected"] else "off")
    return out


def _modules_for(r, d):
    rid = r["id"]
    rv = d["reviews"].get(rid, {})
    cd = d["client_data"].get(rid, {})
    ing = d["ingredients"].get(rid, {})
    mk = d["marketing"].get(rid, {})
    sc = d["schedules"].get(rid, {})
    import pos_health
    pos_state = pos_health.pos_sync_state(r)
    pos = bool(pos_state.get("connected"))
    labor_last = (d.get("labor_last") or {}).get(rid, {}).get("last")
    mods = [
        {"key": "reviews", "label": "Reviews", "enabled": bool(r.get("module_reviews")),
         "configured": bool(r.get("gmb_refresh_token") or r.get("google_place_id") or r.get("yelp_business_id")),
         "receiving": bool(rv.get("total")), "last_data": rv.get("last_review_at") or r.get("last_fetched_at")},
        {"key": "labor", "label": "Labor", "enabled": bool(r.get("module_labor")),
         "configured": pos or bool(cd.get("has_shifts")),
         "receiving": bool(sc.get("n")) or pos or bool(cd.get("has_shifts")),
         "last_data": labor_last or (pos_state.get("last_synced") if pos else None) or cd.get("updated_at")},
        {"key": "inventory", "label": "Food Cost", "enabled": bool(r.get("module_inventory")),
         "configured": bool(ing.get("n")) or bool(cd.get("has_inventory")),
         "receiving": bool(ing.get("last_count_at")) or bool(cd.get("has_inventory")),
         "last_data": ing.get("last_count_at") or cd.get("updated_at")},
        {"key": "marketing", "label": "Marketing", "enabled": bool(r.get("module_marketing")),
         "configured": bool(r.get("ig_token") or r.get("gmb_refresh_token") or r.get("voice_notes")),
         "receiving": bool(mk.get("pieces")), "last_data": mk.get("last_at")},
        {"key": "intel", "label": "Intel", "enabled": bool(r.get("module_reviews") and r.get("module_labor") and r.get("module_inventory") and r.get("module_marketing")),
         "configured": bool(r.get("google_place_id")), "receiving": bool(r.get("competitor_intel")), "last_data": r.get("competitor_updated_at")},
    ]
    for m in mods:
        if not m["enabled"]:
            m["state"] = "off"
        elif not m["configured"]:
            m["state"] = "unconfigured"
        elif not m["receiving"]:
            m["state"] = "no_data"
        else:
            age = _age_days(m["last_data"])
            m["state"] = "stale" if (age is not None and age > (14 if m["key"] in ("inventory", "intel", "marketing") else 3)) else "healthy"
    return mods


def _onboarding_for(r, d, owner):
    rid = r["id"]
    rv = d["reviews"].get(rid, {})
    mods = {m["key"]: m for m in _modules_for(r, d)}
    steps = [
        {"key": "contract", "label": "Contract signed", "done": (r.get("contract_status") or "pending") == "signed"},
        {"key": "login", "label": "First sign-in", "done": bool(owner and owner.get("last_login"))},
        {"key": "reviews", "label": "Google reviews connected", "done": bool(r.get("gmb_refresh_token") or r.get("reviews_live"))},
        {"key": "voice", "label": "Brand voice set", "done": bool(r.get("voice_notes"))},
        {"key": "respond", "label": "First reply approved", "done": (rv.get("responded") or 0) > 0},
    ]
    if r.get("module_labor"):
        steps.append({"key": "labor", "label": "Labor data flowing", "done": mods["labor"]["receiving"]})
    if r.get("module_inventory"):
        steps.append({"key": "inventory", "label": "Inventory loaded", "done": mods["inventory"]["configured"]})
    if r.get("module_marketing"):
        steps.append({"key": "marketing", "label": "First post generated", "done": mods["marketing"]["receiving"]})
    if r.get("billing_status") not in ("internal",):
        steps.append({"key": "billing", "label": "Billing active", "done": r.get("billing_status") == "active"})
    done = sum(1 for s in steps if s["done"])
    return {"steps": steps, "done": done, "total": len(steps),
            "complete": done == len(steps), "dismissed": bool(r.get("onboarding_dismissed"))}


# ── data completeness & churn risk ──────────────────────────────────────────
# Every insight the product sells is only as good as the data under it, and a
# client whose data quietly stopped arriving is the client who decides the
# product "doesn't do much". Both reads here are rule-based and show their
# reasons — an admin must be able to say WHY a location scored what it did.

def _data_completeness(r, d):
    """{"score": 0-100 | None, "checks": [{key, label, ok, detail}]} over the
    checks that apply to this location's modules. None when none apply."""
    rid = r["id"]
    checks = []

    def add(key, label, ok, detail):
        checks.append({"key": key, "label": label, "ok": bool(ok), "detail": detail})

    if r.get("module_reviews"):
        _src, _label = review_source(r)
        add("reviews", "Google reviews connected", r.get("gmb_refresh_token") or r.get("reviews_live"),
            "sampled — Places returns 5 reviews a fetch; connect Business Profile to read them all"
            if _src == "places_sampled" else
            ("reviews fetch automatically" if (r.get("gmb_refresh_token") or r.get("reviews_live"))
             else "no live review source"))
    import pos_health
    _pos = pos_health.pos_sync_state(r)
    pos_fresh = _pos.get("last_synced")
    if r.get("module_labor") or r.get("module_inventory"):
        add("pos", "POS syncing", pos_fresh and not _pos.get("error") and (_age_days(pos_fresh) or 99) <= 3,
            f"{_POS_LABELS.get(_pos.get('provider'), 'POS')} sync error: {_pos.get('error')}" if _pos.get("error")
            else (f"last sync {_since(pos_fresh)}" if pos_fresh else "no POS sync on record"))
    if r.get("module_labor"):
        n = (d["labor_days"].get(rid) or {}).get("n") or 0
        add("labor", "Daily sales & labor, last 30 days", n >= 14, f"{n} of 30 days on file")
    if r.get("module_inventory"):
        rc = d["recipes"].get(rid) or {}
        items, mapped = rc.get("items") or 0, rc.get("mapped") or 0
        add("recipes", "Menu mapped to recipes", items and mapped / items >= 0.6,
            f"{mapped} of {items} menu items have recipes" if items else "no menu items")
        ing = d["ingredients"].get(rid) or {}
        age = _age_days(ing.get("cost_updated_at"))
        add("costs", "Ingredient costs current", ing.get("n") and age is not None and age <= 30,
            (f"{ing.get('n')} ingredients, costs last touched {_since(ing.get('cost_updated_at'))}"
             if ing.get("n") else "no ingredients"))
    ct = d["contacts"].get(rid) or {}
    add("contacts", "Someone consented to alert texts", (ct.get("consented") or 0) > 0,
        f"{ct.get('consented') or 0} of {ct.get('n') or 0} contacts consented")
    add("routing", "Issues routed to a manager", (d["routing"].get(rid) or {}).get("n"),
        "set" if (d["routing"].get(rid) or {}).get("n") else "bad reviews don't reach anyone on the floor")
    add("app", "Owner has the app", (d["tokens"].get(rid) or {}).get("devices"),
        "push-enabled device on file" if (d["tokens"].get(rid) or {}).get("devices") else "no device — briefs go by email")
    # "Setup completeness", not "data completeness" (G14): these are setup
    # checks, and the intelligence layer's feature completeness (the share of
    # features a restaurant can measure) is a different number that had the
    # same name. The label travels with the payload so no screen names it.
    if not checks:
        return {"score": None, "checks": [], "label": SETUP_COMPLETENESS_LABEL}
    return {"score": round(100 * sum(c["ok"] for c in checks) / len(checks)), "checks": checks,
            "label": SETUP_COMPLETENESS_LABEL}


SETUP_COMPLETENESS_LABEL = "Setup completeness"


# A live reviews restaurant should be fetched every four hours. This is
# generous against that: past it, the pass is genuinely not reaching them.
FETCH_STALE_HOURS = int(os.getenv("FETCH_STALE_HOURS", "12"))


def _churn_risk(r, d, last_active, completeness):
    """{"level": low|medium|high|n/a, "reasons": [...]}. Signals, not a model:
    each reason is a fact an admin can check and act on."""
    users = d["users"].get(r["id"], [])
    admin_home = bool(users) and all(u.get("is_admin") for u in users)
    if r.get("billing_status") in ("internal", "churned", "canceled") or r.get("is_demo") or admin_home:
        return {"level": "n/a", "reasons": []}
    rid, reasons, points = r["id"], [], 0
    idle = _age_days(last_active)
    joined = _age_days(r.get("created_at"))
    if joined is not None and joined < 14 and (idle is None or idle >= joined):
        pass    # still onboarding: no activity yet is not disengagement
    elif idle is None or idle >= 30:
        reasons.append("no owner activity in 30+ days" if idle is not None else "never active"); points += 3
    elif idle >= 14:
        reasons.append(f"no owner activity in {int(idle)} days"); points += 2
    wk = (d["ai_week"].get(rid) or {}).get("calls") or 0
    prev = (d["ai_prev"].get(rid) or {}).get("calls") or 0
    if prev >= 5 and wk < prev * 0.5:
        reasons.append(f"usage fell from {prev} to {wk} AI actions week over week"); points += 1
    score = (completeness or {}).get("score")
    if score is not None and score < 50:
        reasons.append(f"setup completeness {score}% — insights are thin"); points += 1
    if r.get("billing_status") == "past_due":
        reasons.append("billing past due"); points += 2
    if (d["reviews"].get(rid) or {}).get("urgent_stale"):
        reasons.append("urgent reviews unanswered 2+ days"); points += 1
    if (d["stale_issues"].get(rid) or {}).get("n"):
        reasons.append("issues open 24h+ with nobody acknowledging"); points += 1

    # VALUE DELIVERED, not just activity. Every signal above measures whether
    # the owner is USING the product; none measured whether it had been worth
    # anything to them. A fully engaged client who has been shown nothing
    # they can point at scored "low risk" right up to the renewal call, and
    # that is the client who actually leaves. Only counted once the account
    # is old enough to have closed a tracker — a three-week-old restaurant
    # with no measured results is normal, not a warning.
    if joined is not None and joined >= 90:
        try:
            import outcomes
            v = outcomes.total_value(rid)
            if not v["wins"] and not v["in_flight"]:
                reasons.append("nothing measured in %d days — no result to show at renewal"
                               % int(joined)); points += 2
            elif not v["wins"]:
                reasons.append("%d change%s being measured, none has landed yet"
                               % (v["in_flight"], "" if v["in_flight"] == 1 else "s")); points += 1
        except Exception:
            pass

    level = "high" if points >= 4 else "medium" if points >= 2 else "low"
    return {"level": level, "reasons": reasons}


def _pos_state_for(r):
    import pos_health
    return pos_health.pos_sync_state(r)


def location_record(r, d):
    """The one health record for a location. Everything else is a view of it."""
    rid = r["id"]
    users = d["users"].get(rid, [])
    owner = next((u for u in users if u.get("role") in ("client", "owner") and not u.get("is_admin")), users[0] if users else None)
    rv = d["reviews"].get(rid, {})
    ai = d["ai_month"].get(rid, {})
    em = d["emails"].get(rid, {})
    pu = d["pushes"].get(rid, {})
    tk = d["tokens"].get(rid, {})
    al = d["alerts"].get(rid, {})
    integrations = _integrations_for(r, d["webhooks"])
    modules = _modules_for(r, d)
    onboarding = _onboarding_for(r, d, owner)
    last_login = max([u.get("last_login") or "" for u in users] + [""]) or None
    last_active = max([last_login or "", r.get("last_activity") or "", (d["logins"].get(rid) or {}).get("last_at") or ""]) or None
    mod_count = sum(1 for m in modules if m["enabled"] and m["key"] != "intel")
    monthly = MONTHLY_BY_MODULES.get(mod_count, 0) if r.get("billing_status") == "active" else 0
    sp = d["sched_posts"].get(rid, {})

    issues = _issues_for(r, d, owner, integrations, modules, onboarding, last_active)
    completeness = _data_completeness(r, d)
    churn = _churn_risk(r, d, last_active, completeness)
    worst = max([i["severity_rank"] for i in issues] + [0])
    health = {0: "healthy", 1: "warning", 2: "critical"}[worst]
    if r.get("billing_status") in ("churned", "canceled") or (owner and not owner.get("is_active")):
        health = "inactive"

    return {
        "id": rid,
        "name": r.get("name"),
        # Will's own login lives on a restaurant row too — it is never a
        # client, never MRR.
        "is_admin_home": bool(users) and all(u.get("is_admin") for u in users),
        "brand": r.get("location_group") or r.get("name"),
        "location_name": r.get("location_name"),
        "city": r.get("neighborhood"),
        "is_demo": bool(r.get("is_demo")),
        "created_at": r.get("created_at"),
        "owner": ({"id": owner["id"], "username": owner["username"], "email": owner["email"], "role": owner.get("role"),
                   "is_active": bool(owner.get("is_active")), "last_login": owner.get("last_login")} if owner else None),
        "owner_name": r.get("owner_name"), "owner_email": r.get("owner_email"), "owner_phone": r.get("owner_phone"),
        "logins": [{"id": u["id"], "username": u["username"], "email": u["email"], "role": u.get("role"), "is_active": bool(u.get("is_active")), "last_login": u.get("last_login")} for u in users],
        "pos_system": r.get("pos_system"),
        "billing": {"status": r.get("billing_status") or "trial", "tier": r.get("service_tier"),
                    "stripe_customer_id": r.get("stripe_customer_id"), "contract_status": r.get("contract_status") or "pending",
                    "envelope_id": r.get("docusign_envelope_id"), "modules": mod_count, "monthly": monthly},
        "modules": modules,
        "integrations": integrations,
        "integration_health": ("error" if any(i["state"] == "error" for i in integrations)
                               else "ok" if any(i["state"] == "connected" for i in integrations) else "none"),
        "onboarding": onboarding,
        "freshness": {"reviews": r.get("last_fetched_at"), "pos": _pos_state_for(r).get("last_synced"),
                      "pos_state": _pos_state_for(r),
                      "inventory": (d["ingredients"].get(rid) or {}).get("last_count_at"),
                      "intel": r.get("competitor_updated_at")},
        "reviews": {"total": rv.get("total") or 0, "awaiting": rv.get("awaiting") or 0, "responded": rv.get("responded") or 0,
                    "urgent_stale": rv.get("urgent_stale") or 0},
        "ai": {"calls_30d": ai.get("calls") or 0, "cost_30d": float(ai.get("cost") or 0), "tokens_30d": ai.get("tokens") or 0,
               "calls_today": (d["ai_today"].get(rid) or {}).get("calls") or 0,
               "calls_7d": (d["ai_week"].get(rid) or {}).get("calls") or 0,
               "calls_prev_7d": (d["ai_prev"].get(rid) or {}).get("calls") or 0, "last_at": ai.get("last_at"),
               "failed_30d": ai.get("failed") or 0, "failed_7d": (d["ai_failed_week"].get(rid) or {}).get("n") or 0},
        "email": {"sent_7d": em.get("sent_7d") or 0, "failed_7d": em.get("failed_7d") or 0, "last_failed_at": em.get("last_failed_at"), "last_sent_at": em.get("last_sent_at")},
        "push": {"sent_7d": pu.get("sent_7d") or 0, "failed_7d": pu.get("failed_7d") or 0, "devices": tk.get("devices") or 0, "disabled": tk.get("disabled") or 0},
        "alerts_7d": al.get("fired_7d") or 0,
        "alert_cap": r.get("alert_max_per_day") or 0,
        "scheduled_posts": {"failed": sp.get("failed") or 0, "pending": sp.get("pending") or 0},
        "guests": (d["guests"].get(rid) or {}).get("n") or 0,
        "sessions": (d["sessions"].get(rid) or {}).get("n") or 0,
        "last_login": last_login,
        "last_active": last_active,
        "internal_notes": r.get("internal_notes"),
        "issues": issues,
        "health": health,
        # setup_completeness is the name; data_completeness stays as an alias
        # until admin.html reads the new key (group J).
        "setup_completeness": completeness,
        "data_completeness": completeness,
        "churn_risk": churn,
    }


# The review fetch runs at these Chicago hours (scheduler.py, _latest_slot).
REVIEW_FETCH_SLOTS = (8, 12, 16, 20)
# How long after a slot starts its pass may still be working through the
# list (run_daily_fetch is bounded and resumes from a cursor).
FETCH_SLOT_GRACE = timedelta(hours=1)


def _now_ct():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Chicago"))


def fetched_at_ct(raw):
    """restaurants.last_fetched_at as an aware Chicago time. models writes
    Chicago local with a 'T'; SQLite's datetime('now') (older rows, tests,
    hand fixes) is UTC with a space."""
    from zoneinfo import ZoneInfo
    from time_utils import parse_stamp
    d = parse_stamp(raw, naive_tz="America/Chicago")
    return None if d is None else d.astimezone(ZoneInfo("America/Chicago"))


def fetch_slots_missed(last_fetched_at, now=None) -> int:
    """How many review-fetch slots have come and gone (each given
    FETCH_SLOT_GRACE to finish) since this restaurant was last fetched.
    0 when it is current; None when it has never been fetched."""
    last = fetched_at_ct(last_fetched_at)
    if last is None:
        return None
    now = now or _now_ct()
    cutoff = now - FETCH_SLOT_GRACE
    missed, day = 0, cutoff.date()
    # Walk back over slot starts until one is at or before the last fetch.
    for back in range(0, 8):
        d = day - timedelta(days=back)
        for h in sorted(REVIEW_FETCH_SLOTS, reverse=True):
            start = datetime(d.year, d.month, d.day, h, tzinfo=cutoff.tzinfo)
            if start > cutoff:
                continue
            if start <= last:
                return missed
            missed += 1
    return missed


def _issues_for(r, d, owner, integrations, modules, onboarding, last_active):
    """Actionable problems for one location, each with severity and age."""
    rid = r["id"]
    out = []

    def add(key, title, severity, since=None, action=None, action_route=None, detail=None):
        k = f"{rid}:{key}"
        if k in d["resolved"]:
            return
        out.append({"key": k, "restaurant_id": rid, "title": title, "detail": detail, "severity": severity,
                    "severity_rank": {"warning": 1, "critical": 2}[severity], "since": _since(since) if since else "—",
                    "since_at": since, "action": action, "action_route": action_route})

    bs = r.get("billing_status") or "trial"
    if bs == "past_due":
        add("billing", "Stripe payment failed", "critical", r.get("last_activity"), "Resend payment link", f"/admin/resend-payment/{rid}")
    if bs in ("churned", "canceled"):
        add("canceled", "Subscription canceled", "warning", None, "Open billing")
    if (r.get("contract_status") or "pending") != "signed" and not r.get("is_demo") and bs not in ("internal", "churned"):
        age = _age_days(r.get("created_at"))
        if age is not None and age > 3:
            add("contract", "Contract still unsigned", "warning", r.get("created_at"), "Resend contract", f"/admin/resend-contract/{rid}")
    for i in integrations:
        if i["state"] == "error":
            sev = "critical" if i["key"] in ("toast", "square", "clover", "rpower", "google_business") else "warning"
            add(f"int:{i['key']}", f"{i['label']} needs attention", sev, i.get("last_success"),
                "Reconnect", None, i["error"])
    for m in modules:
        if m["state"] == "stale":
            add(f"stale:{m['key']}", f"{m['label']} data is stale", "warning", m["last_data"],
                "Sync now" if m["key"] in ("reviews", "labor") else "Check data",
                f"/admin/fetch-reviews/{rid}" if m["key"] == "reviews" else None,
                f"Last data {_since(m['last_data'])} ago")
        elif m["state"] == "no_data":
            add(f"nodata:{m['key']}", f"{m['label']} is on but has never received data", "warning", r.get("created_at"), "Check setup")
        elif m["state"] == "unconfigured":
            add(f"unconf:{m['key']}", f"{m['label']} is on but not configured", "warning", r.get("created_at"), "Open settings", f"/admin/client-settings/{rid}")
    # Fetch coverage on the schedule's own clock (DATA-7). "Stale" above is
    # keyed on 3 days of review DATA, which a quiet restaurant produces with
    # every fetch working; this says the fetch itself stopped reaching it.
    # One missed slot can be the bounded pass's tail; two is not.
    if (r.get("module_reviews") and not r.get("is_demo") and bs in ("active", "trial")
            and r.get("last_fetched_at")):
        missed = fetch_slots_missed(r.get("last_fetched_at"))
        if missed and missed >= 2:
            add("fetch_behind", f"Review fetch has missed {missed} scheduled runs", "warning",
                r.get("last_fetched_at"), "Sync now", f"/admin/fetch-reviews/{rid}",
                "Fetches run at 8am, noon, 4pm and 8pm Central.")
    rv = d["reviews"].get(rid, {})
    if (rv.get("urgent_stale") or 0) > 0:
        add("urgent", f"{rv['urgent_stale']} urgent review{'s' if rv['urgent_stale'] > 1 else ''} unanswered 48h+", "critical", None, "View as client", f"/admin/view-as/{rid}")
    em = d["emails"].get(rid, {})
    if (em.get("failed_7d") or 0) > 0:
        add("email", f"{em['failed_7d']} email{'s' if em['failed_7d'] > 1 else ''} failed this week", "warning", em.get("last_failed_at"), "Open emails")
    pu = d["pushes"].get(rid, {})
    tk = d["tokens"].get(rid, {})
    if (pu.get("failed_7d") or 0) > 0 or (tk.get("disabled") or 0) > 0:
        add("push", f"Push delivery failing ({pu.get('failed_7d') or 0} failed, {tk.get('disabled') or 0} dead device{'s' if (tk.get('disabled') or 0) != 1 else ''})", "warning", pu.get("last_failed_at"), "Open notifications")
    sp = d["sched_posts"].get(rid, {})
    if (sp.get("failed") or 0) > 0:
        add("posts", f"{sp['failed']} scheduled post{'s' if sp['failed'] > 1 else ''} failed to publish", "warning", None, "Open marketing")
    if not r.get("is_demo") and bs in ("active", "trial") and not onboarding["complete"] and not onboarding["dismissed"]:
        age = _age_days(r.get("created_at"))
        if age is not None and age > 7 and onboarding["done"] < max(2, onboarding["total"] // 2):
            add("onboarding", f"Onboarding stuck at {onboarding['done']}/{onboarding['total']}", "warning", r.get("created_at"), "Open onboarding")
    if bs == "active" and not r.get("is_demo"):
        age = _age_days(last_active)
        if age is None or age > 30:
            add("inactive", "No activity in 30+ days", "critical", last_active, "Reach out")
        elif age > 14:
            add("inactive", "No activity in 14+ days", "warning", last_active, "Reach out")
    ai = d["ai_month"].get(rid, {})
    wk = (d["ai_week"].get(rid) or {}).get("calls") or 0
    prev = (d["ai_prev"].get(rid) or {}).get("calls") or 0
    if wk >= 40 and prev and wk > 4 * prev:
        add("ai_spike", f"AI usage {wk // max(prev, 1)}× last week's ({wk} calls)", "warning", ai.get("last_at"), "Open AI ops")
    if float(ai.get("cost") or 0) > 25:
        add("ai_cost", f"${float(ai['cost']):.2f} AI spend in 30 days", "warning", ai.get("last_at"), "Open AI ops")
    af = d["ai_failed_week"].get(rid) or {}
    if (af.get("n") or 0) >= 3:
        add("ai_failures", f"{af['n']} AI calls failed this week", "critical" if af["n"] >= 10 else "warning", af.get("last_at"), "Open AI ops",
            detail=(af.get("sample") or "")[:160])
    return out


# ── page payloads ────────────────────────────────────────────────────────────

def _records(d=None):
    d = d or _load_everything()
    recs = [location_record(r, d) for r in d["rests"]]
    # Duplicate brand names across separate accounts (not multi-location groups).
    seen = {}
    for rec in recs:
        if rec["is_demo"] or rec["brand"] != rec["name"]:
            continue
        key = re.sub(r"[^a-z0-9]", "", (rec["name"] or "").lower())
        seen.setdefault(key, []).append(rec)
    for key, group in seen.items():
        if len(group) > 1 and key:
            for rec in group:
                k = f"{rec['id']}:dup"
                if k in d["resolved"]:
                    continue
                rec["issues"].append({"key": k, "restaurant_id": rec["id"], "title": f"Possible duplicate: {len(group)} accounts named \"{rec['name']}\"",
                                      "detail": None, "severity": "warning", "severity_rank": 1, "since": "—", "since_at": None,
                                      "action": "Review", "action_route": None})
                if rec["health"] == "healthy":
                    rec["health"] = "warning"
    return recs, d


def _brands(recs):
    """OWNER → BRAND → LOCATION. A brand is a location_group; a lone restaurant
    is its own brand. Brand health is the worst location's, never an average."""
    groups = {}
    for rec in recs:
        groups.setdefault(rec["brand"], []).append(rec)
    rank = {"inactive": -1, "healthy": 0, "warning": 1, "critical": 2}
    out = []
    for brand, locs in groups.items():
        worst = max(locs, key=lambda x: rank[x["health"]])
        owners = {}
        for l in locs:
            if l["owner"]:
                owners[l["owner"]["id"]] = l["owner"]
        out.append({"brand": brand, "locations": sorted(locs, key=lambda x: x["id"]), "count": len(locs),
                    "health": worst["health"], "owners": list(owners.values()),
                    "issues": sum(len(l["issues"]) for l in locs),
                    "monthly": sum(l["billing"]["monthly"] for l in locs),
                    "is_demo": all(l["is_demo"] for l in locs)})
    out.sort(key=lambda b: (-rank[b["health"]], b["brand"].lower()))
    return out


def overview():
    recs, d = _records()
    now = d["now"]
    real = [r for r in recs if not r["is_demo"] and not r["is_admin_home"] and r["billing"]["status"] != "internal"]
    month_start = now.strftime("%Y-%m-01")
    conn = get_conn()
    today = now.strftime("%Y-%m-%d")
    day = _stamp(now - timedelta(days=1))
    ai_today = _one_dict(conn, "SELECT COUNT(*) AS n, ROUND(COALESCE(SUM(cost_usd),0),2) AS cost FROM ai_usage WHERE created_at >= ?", (today,)) or {}
    emails_today = _one_dict(conn, "SELECT SUM(CASE WHEN status='failed' THEN 0 ELSE 1 END) AS sent, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed FROM email_log WHERE sent_at >= ?", (today,)) or {}
    push_today = _one_dict(conn, "SELECT SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS sent, SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failed FROM push_deliveries WHERE created_at >= ?", (today,)) or {}
    alerts_today = _one_dict(conn, "SELECT COUNT(*) AS n FROM alert_log WHERE fired_at >= ?", (today,)) or {}
    jobs_failed_24h = _one_dict(conn, "SELECT COUNT(*) AS n, COUNT(DISTINCT job) AS jobs FROM job_failures WHERE created_at >= ?", (day,)) or {}
    ai_failed_24h = _one_dict(conn, "SELECT COUNT(*) AS n FROM ai_usage WHERE created_at >= ? AND COALESCE(status,'ok')='error'", (day,)) or {}
    conn.close()
    from status_manager import scheduler_heartbeat_age_minutes
    try:
        hb = scheduler_heartbeat_age_minutes()
    except Exception:
        hb = None

    issues = sorted([i for r in recs for i in r["issues"]], key=lambda i: (-i["severity_rank"], i["title"]))
    for i in issues:
        rec = next(x for x in recs if x["id"] == i["restaurant_id"])
        i["restaurant"] = rec["name"]; i["brand"] = rec["brand"]; i["location_name"] = rec["location_name"]; i["owner"] = (rec["owner"] or {}).get("username")
    # Global job failures are issues too — one line per job, not one per firing.
    by_job = {}
    for f in d["job_failures"]:
        if _age_hours(f["created_at"]) is not None and _age_hours(f["created_at"]) <= 24:
            by_job.setdefault(f["job"], []).append(f)
    for job, fs in by_job.items():
        key = f"job:{job}"
        if key in d["resolved"]:
            continue
        issues.insert(0, {"key": key, "restaurant_id": None, "restaurant": "Platform", "brand": "Platform", "location_name": None, "owner": None,
                          "title": f"Job `{job}` failed {len(fs)}× in 24h", "detail": (fs[0]["error"] or "")[:160], "severity": "critical" if len(fs) >= 5 else "warning",
                          "severity_rank": 2 if len(fs) >= 5 else 1, "since": _since(fs[-1]["created_at"]), "since_at": fs[-1]["created_at"], "action": "Open jobs", "action_route": None})
    # FLEET COVERAGE. Every signal above answers "did something fail?"; none
    # answered "did something not happen?". The review fetch is now bounded
    # in time (scheduler.FETCH_MAX_SECONDS), which is correct, but it means
    # a pass can legitimately end without reaching everyone — and an
    # unreached restaurant looks exactly like a restaurant with no new
    # reviews. This is the one check that can tell them apart, and it is the
    # difference between noticing at 9am and hearing it from the owner.
    stale_fetch = []
    for r in recs:
        if r["is_demo"] or r["is_admin_home"] or r["billing"]["status"] in ("internal", "churned", "canceled"):
            continue
        if not (r.get("modules") and any(m["key"] == "reviews" and m["enabled"] for m in r["modules"])):
            continue
        last = (r.get("freshness") or {}).get("reviews")
        age = _age_hours(last)
        # Never fetched is only a problem once the account is past setup;
        # _onboarding_for already knows when that is.
        if age is None and (r.get("created_at") or "") > _stamp(now - timedelta(days=3)):
            continue
        if age is None or age > FETCH_STALE_HOURS:
            stale_fetch.append((r, age))
    if stale_fetch and "fleet:fetch_coverage" not in d["resolved"]:
        worst = max((a for _r, a in stale_fetch if a is not None), default=None)
        names = ", ".join(r["name"] for r, _a in stale_fetch[:4])
        issues.insert(0, {
            "key": "fleet:fetch_coverage", "restaurant_id": None, "restaurant": "Platform",
            "brand": "Platform", "location_name": None, "owner": None,
            "title": f"{len(stale_fetch)} restaurant{'' if len(stale_fetch) == 1 else 's'} "
                     f"not fetched in over {FETCH_STALE_HOURS}h",
            "detail": (f"{names}{'…' if len(stale_fetch) > 4 else ''}. "
                       f"Nothing failed — the pass did not reach them."),
            "severity": "critical" if len(stale_fetch) > max(3, len(recs) // 10) else "warning",
            "severity_rank": 2 if len(stale_fetch) > max(3, len(recs) // 10) else 1,
            "since": f"{int(worst)}h" if worst else "never", "since_at": None,
            "action": "Open jobs", "action_route": None})

    if hb is None or hb > 15:
        issues.insert(0, {"key": "scheduler", "restaurant_id": None, "restaurant": "Platform", "brand": "Platform", "location_name": None, "owner": None,
                          "title": "Scheduler heartbeat is stale" if hb is not None else "Scheduler has never stamped a heartbeat", "detail": f"{int(hb)} minutes" if hb else None,
                          "severity": "critical", "severity_rank": 2, "since": f"{int(hb)}m" if hb else "—", "since_at": None, "action": "Check Railway", "action_route": None})

    kpis = {
        "clients": len(real), "active": sum(1 for r in real if r["billing"]["status"] == "active"),
        "trial": sum(1 for r in real if r["billing"]["status"] == "trial"),
        "locations": len(recs), "brands": len(_brands(real)),
        "new_this_month": sum(1 for r in real if (r["created_at"] or "") >= month_start),
        "subscriptions": sum(1 for r in real if r["billing"]["status"] == "active" and r["billing"]["stripe_customer_id"]),
        "mrr": sum(r["billing"]["monthly"] for r in real),
        "past_due": sum(1 for r in real if r["billing"]["status"] == "past_due"),
        "canceled": sum(1 for r in real if r["billing"]["status"] in ("churned", "canceled")),
        "integrations_active": sum(1 for r in recs for i in r["integrations"] if i["state"] == "connected"),
        "integrations_failing": sum(1 for r in recs for i in r["integrations"] if i["state"] == "error"),
        "ai_calls_today": ai_today.get("n") or 0, "ai_cost_today": float(ai_today.get("cost") or 0), "ai_failures_24h": ai_failed_24h.get("n") or 0,
        "emails_today": emails_today.get("sent") or 0, "email_failures_today": emails_today.get("failed") or 0,
        "push_today": push_today.get("sent") or 0, "push_failures_today": push_today.get("failed") or 0,
        "alerts_today": alerts_today.get("n") or 0,
        "job_failures_24h": jobs_failed_24h.get("n") or 0, "jobs_failing": jobs_failed_24h.get("jobs") or 0,
        "attention": sum(1 for i in issues), "critical": sum(1 for i in issues if i["severity"] == "critical"),
        "scheduler_heartbeat_minutes": hb,
    }
    # Latency and error rate. Rolling, in-process, reset on deploy — see
    # http_layer.request_metrics for why it is not a table.
    try:
        from http_layer import request_metrics
        rm = request_metrics()
        kpis.update({"rpm": rm["rpm"], "error_rate": rm["error_rate"],
                     "server_error_rate": rm["server_error_rate"],
                     "p50_ms": rm["p50_ms"], "p95_ms": rm["p95_ms"]})
        if rm["requests"] >= 20 and rm["server_error_rate"] >= 5.0:
            issues.insert(0, {
                "key": "platform:error_rate", "restaurant_id": None, "restaurant": "Platform",
                "brand": "Platform", "location_name": None, "owner": None,
                "title": f"{rm['server_error_rate']:.0f}% of requests are 5xx",
                "detail": f"{rm['requests']} requests in the last {rm['window_seconds'] // 60} minutes.",
                "severity": "critical", "severity_rank": 2, "since": "now", "since_at": None,
                "action": "Open jobs", "action_route": None})
    except Exception as e:
        log.warning("request metrics unavailable: %s", e)
    return {"ok": True, "kpis": kpis, "issues": issues[:60], "activity": activity(limit=30, d=d)["events"],
            "brands": [{k: v for k, v in b.items() if k != "locations"} | {"location_ids": [l["id"] for l in b["locations"]]} for b in _brands(recs)],
            "generated_at": _stamp(now)}


def clients():
    recs, d = _records()
    return {"ok": True, "clients": recs, "brands": [{**{k: v for k, v in b.items() if k != "locations"}, "location_ids": [l["id"] for l in b["locations"]]} for b in _brands(recs)]}


def client_detail(rid):
    recs, d = _records()
    rec = next((r for r in recs if r["id"] == rid), None)
    if not rec:
        return {"ok": False, "error": "Not found"}
    siblings = [r for r in recs if r["brand"] == rec["brand"]]
    conn = get_conn()
    month = _stamp(d["now"] - timedelta(days=30))
    ai_by_action = _rows_dict(conn, "SELECT action, model, COUNT(*) AS calls, ROUND(SUM(cost_usd),4) AS cost, SUM(input_tokens)+SUM(output_tokens) AS tokens, MAX(created_at) AS last_at FROM ai_usage WHERE restaurant_id=? AND created_at >= ? GROUP BY action, model ORDER BY cost DESC", (rid, month))
    ai_recent = _rows_dict(conn, "SELECT action, model, input_tokens, output_tokens, cost_usd, created_at, COALESCE(status,'ok') AS status, error FROM ai_usage WHERE restaurant_id=? ORDER BY id DESC LIMIT 40", (rid,))
    ai_failed = _rows_dict(conn, "SELECT action, model, error, created_at FROM ai_usage WHERE restaurant_id=? AND COALESCE(status,'ok')='error' ORDER BY id DESC LIMIT 20", (rid,))
    events = _rows_dict(conn, "SELECT id, source, event_type, amount, summary, created_at FROM admin_events WHERE restaurant_id=? ORDER BY id DESC LIMIT 40", (rid,))
    ai_daily = _rows_dict(conn, "SELECT substr(created_at,1,10) AS day, COUNT(*) AS calls, ROUND(SUM(cost_usd),4) AS cost FROM ai_usage WHERE restaurant_id=? AND created_at >= ? GROUP BY day ORDER BY day", (rid, month))
    emails = _rows_dict(conn, "SELECT email_type, to_email, subject, sent_at, status, error FROM email_log WHERE restaurant_id=? ORDER BY id DESC LIMIT 60", (rid,))
    pushes = _rows_dict(conn, "SELECT alert_type, status, ok, attempts, error, created_at FROM push_deliveries WHERE restaurant_id=? ORDER BY id DESC LIMIT 40", (rid,))
    devices = _rows_dict(conn, "SELECT id, user_id, environment, created_at, last_success_at, consecutive_failures, disabled_reason FROM device_tokens WHERE restaurant_id=?", (rid,))
    alerts = _rows_dict(conn, "SELECT alert_type, review_id, fired_at FROM alert_log WHERE restaurant_id=? ORDER BY id DESC LIMIT 40", (rid,))
    acts = _rows_dict(conn, "SELECT event_type, event_data, created_at FROM activity_log WHERE restaurant_id=? ORDER BY id DESC LIMIT 60", (rid,))
    logins = _rows_dict(conn, "SELECT event, ip_address, user_agent, device_type, created_at FROM login_history WHERE restaurant_id=? ORDER BY id DESC LIMIT 30", (rid,))
    sessions = _rows_dict(conn, "SELECT s.created_at, s.last_active, s.device_type, s.ip_address, u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE u.restaurant_id=? AND s.expires_at > datetime('now') ORDER BY s.last_active DESC", (rid,))
    jobs = _rows_dict(conn, "SELECT job, error, context, created_at FROM job_failures WHERE context LIKE ? OR context LIKE ? ORDER BY id DESC LIMIT 40", (f"%rid={rid}%", f"%{rec['name']}%"))
    runs = _rows_dict(conn, "SELECT job, started_at, finished_at, duration_ms, ok, error FROM job_runs WHERE context LIKE ? ORDER BY id DESC LIMIT 20", (f"%rid={rid}%",))
    posts = _rows_dict(conn, "SELECT platform, content_type, topic, scheduled_for, status, error, attempts, posted_at FROM marketing_scheduled_posts WHERE restaurant_id=? ORDER BY id DESC LIMIT 20", (rid,))
    hooks = _rows_dict(conn, "SELECT event_type, status, ok, attempts, error, created_at FROM webhook_deliveries WHERE restaurant_id=? ORDER BY id DESC LIMIT 20", (rid,))
    try:
        schedules = _rows_dict(conn, "SELECT id, generated_at, week_start, week_end, hours_scheduled, hours_budget, "
                               "generation_seconds, published_at, published_by, review_json FROM schedule_history "
                               "WHERE restaurant_id=? ORDER BY id DESC LIMIT 10", (rid,))
        for sch in schedules:
            try:
                rv = json.loads(sch.pop("review_json", None) or "null") or {}
            except Exception:
                rv = {}
            sch["hard_breaches"] = rv.get("hard") or 0
    except Exception:
        schedules = _rows_dict(conn, "SELECT id, generated_at, week_start, week_end, hours_scheduled, hours_budget FROM schedule_history WHERE restaurant_id=? ORDER BY id DESC LIMIT 10", (rid,))
    notes = _rows_dict(conn, "SELECT id, employee_name, notes, created_at FROM staff_notes WHERE restaurant_id=? ORDER BY id DESC", (rid,))
    conn.close()
    r = get_restaurant(rid)
    errors = ([{"kind": "integration", "label": i["label"], "error": i["error"], "at": i.get("last_success")} for i in rec["integrations"] if i["error"]]
              + [{"kind": "email", "label": e["email_type"], "error": e["error"], "at": e["sent_at"]} for e in emails if e["status"] == "failed"]
              + [{"kind": "push", "label": p["alert_type"], "error": p["error"], "at": p["created_at"]} for p in pushes if not p["ok"]]
              + [{"kind": "post", "label": f"{p['platform']} · {p['topic'] or p['content_type']}", "error": p["error"], "at": p["scheduled_for"]} for p in posts if p["status"] == "failed"]
              + [{"kind": "job", "label": j["job"], "error": j["error"], "at": j["created_at"]} for j in jobs]
              + [{"kind": "ai", "label": f"{a['action']} · {a['model']}", "error": a["error"], "at": a["created_at"]} for a in ai_failed]
              + [{"kind": "webhook", "label": h["event_type"], "error": h["error"], "at": h["created_at"]} for h in hooks if not h["ok"]])
    errors.sort(key=lambda e: e.get("at") or "", reverse=True)
    return {"ok": True, "client": rec, "siblings": [s for s in siblings if s["id"] != rid],
            "profile": {"neighborhood": r.neighborhood, "vibe": r.vibe, "known_for": r.known_for, "timezone": r.timezone,
                        "voice_notes": r.voice_notes, "pos_system": r.pos_system, "google_place_id": r.google_place_id,
                        "yelp_business_id": r.yelp_business_id, "labor_target_pct": r.labor_target_pct,
                        "week_start_day": r.week_start_day,
                        "food_cost_target": r.food_cost_target, "digest_day": r.digest_day, "internal_notes": r.internal_notes},
            "ai": {"by_action": ai_by_action, "recent": ai_recent, "daily": ai_daily, "failed": ai_failed},
            "events": events, "emails": emails, "pushes": pushes, "devices": devices, "alerts": alerts,
            "activity": [{"type": a["event_type"], "data": a["event_data"], "at": a["created_at"]} for a in acts],
            "logins": logins, "sessions": sessions, "jobs": jobs, "job_runs": runs, "scheduled_posts": posts,
            "webhook_deliveries": hooks, "schedules": schedules, "staff_notes": notes, "errors": errors[:60]}


def integrations():
    recs, _ = _records()
    rows = []
    for r in recs:
        for i in r["integrations"]:
            if not i["in_use"] and i["key"] != "google_business":
                continue
            age = _age_days(i.get("last_success"))
            rows.append({"restaurant_id": r["id"], "restaurant": r["name"], "brand": r["brand"], "location_name": r["location_name"],
                         "integration": i["key"], "label": i["label"], "state": i["state"], "auth": i["auth"],
                         "last_success": i.get("last_success"), "error": i["error"],
                         # The per-location list's POS reading (CA3 F6), carried to the
                         # fleet list too so both say the same thing.
                         "sync_state": i.get("sync_state"), "age_days": i.get("age_days"),
                         "freshness": ("never" if i["state"] == "connected" and age is None else ("stale" if age is not None and age > 3 else "fresh")) if i["state"] != "off" else "—",
                         "health": r["health"]})
    systemic = {}
    for row in rows:
        if row["state"] == "error":
            systemic.setdefault(row["label"], 0)
            systemic[row["label"]] += 1
    return {"ok": True, "rows": rows, "systemic": [{"label": k, "failing": v} for k, v in systemic.items() if v >= 2]}


def ai_ops(days=30):
    conn = get_conn()
    now = datetime.now()
    since = _stamp(now - timedelta(days=days))
    today = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m-01")
    totals = _one_dict(conn, "SELECT COUNT(*) AS calls, ROUND(COALESCE(SUM(cost_usd),0),2) AS cost, COALESCE(SUM(input_tokens),0) AS tin, COALESCE(SUM(output_tokens),0) AS tout FROM ai_usage WHERE created_at >= ?", (since,)) or {}
    t_today = _one_dict(conn, "SELECT COUNT(*) AS calls, ROUND(COALESCE(SUM(cost_usd),0),2) AS cost FROM ai_usage WHERE created_at >= ?", (today,)) or {}
    t_month = _one_dict(conn, "SELECT COUNT(*) AS calls, ROUND(COALESCE(SUM(cost_usd),0),2) AS cost FROM ai_usage WHERE created_at >= ?", (month,)) or {}
    by_action = _rows_dict(conn, "SELECT action, model, COUNT(*) AS calls, ROUND(SUM(cost_usd),4) AS cost, ROUND(AVG(input_tokens+output_tokens)) AS avg_tokens FROM ai_usage WHERE created_at >= ? GROUP BY action, model ORDER BY cost DESC", (since,))
    by_client = _rows_dict(conn, "SELECT a.restaurant_id, r.name, r.location_group, COUNT(*) AS calls, ROUND(SUM(a.cost_usd),4) AS cost FROM ai_usage a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE a.created_at >= ? GROUP BY a.restaurant_id ORDER BY cost DESC", (since,))
    daily = _rows_dict(conn, "SELECT substr(created_at,1,10) AS day, COUNT(*) AS calls, ROUND(SUM(cost_usd),4) AS cost FROM ai_usage WHERE created_at >= ? GROUP BY day ORDER BY day", (since,))
    by_provider = _rows_dict(conn, "SELECT CASE WHEN model LIKE '%perplexity%' OR model LIKE 'sonar%' OR action LIKE '%visibility%' THEN 'Perplexity' ELSE 'Claude' END AS provider, COUNT(*) AS calls, ROUND(SUM(cost_usd),4) AS cost FROM ai_usage WHERE created_at >= ? GROUP BY provider", (since,))
    failures = _rows_dict(conn, "SELECT action AS job, model, COUNT(*) AS n, MAX(created_at) AS last_at, MAX(error) AS sample FROM ai_usage WHERE created_at >= ? AND COALESCE(status,'ok')='error' GROUP BY action, model ORDER BY n DESC", (since,))
    failed_total = _one_dict(conn, "SELECT COUNT(*) AS n, SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS n_24h FROM ai_usage WHERE created_at >= ? AND COALESCE(status,'ok')='error'", (_stamp(now - timedelta(days=1)), since)) or {}
    recent = _rows_dict(conn, "SELECT a.id, a.restaurant_id, r.name, a.action, a.model, a.input_tokens, a.output_tokens, a.cost_usd, a.created_at, COALESCE(a.status,'ok') AS status, a.error FROM ai_usage a LEFT JOIN restaurants r ON r.id=a.restaurant_id ORDER BY a.id DESC LIMIT 80")
    recent_failed = _rows_dict(conn, "SELECT a.id, a.restaurant_id, r.name, a.action, a.model, a.created_at, a.error FROM ai_usage a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE COALESCE(a.status,'ok')='error' ORDER BY a.id DESC LIMIT 40")
    # Anomalies: a client whose last 7 days is >4x its previous 7, or any
    # action that ran >200 times in a day for one restaurant (a loop).
    week = _stamp(now - timedelta(days=7)); prev = _stamp(now - timedelta(days=14))
    wk = {r["restaurant_id"]: r["n"] for r in _rows_dict(conn, "SELECT restaurant_id, COUNT(*) AS n FROM ai_usage WHERE created_at >= ? GROUP BY restaurant_id", (week,))}
    pv = {r["restaurant_id"]: r["n"] for r in _rows_dict(conn, "SELECT restaurant_id, COUNT(*) AS n FROM ai_usage WHERE created_at >= ? AND created_at < ? GROUP BY restaurant_id", (prev, week))}
    loops = _rows_dict(conn, "SELECT a.restaurant_id, r.name, a.action, substr(a.created_at,1,10) AS day, COUNT(*) AS n FROM ai_usage a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE a.created_at >= ? GROUP BY a.restaurant_id, a.action, day HAVING n >= 200 ORDER BY n DESC", (since,))
    conn.close()
    names = {c["restaurant_id"]: c["name"] for c in by_client}
    anomalies = [{"kind": "spike", "restaurant_id": rid, "restaurant": names.get(rid), "detail": f"{n} calls this week vs {pv.get(rid, 0)} the week before"}
                 for rid, n in wk.items() if n >= 40 and pv.get(rid, 0) and n > 4 * pv.get(rid, 0)]
    anomalies += [{"kind": "loop", "restaurant_id": l["restaurant_id"], "restaurant": l["name"], "detail": f"{l['action']} ran {l['n']}× on {l['day']}"} for l in loops]
    # Spend against the ceilings that actually stop calls (ai_utils), so the
    # page shows how close the account is rather than only what it has spent.
    try:
        import ai_utils as _ai
        budget = _ai.ai_budget_status()
    except Exception:
        budget = {}
    return {"ok": True, "days": days, "totals": {**totals, "today": t_today, "month": t_month}, "by_action": by_action,
            "by_client": by_client, "daily": daily, "by_provider": by_provider, "failures": failures, "recent": recent, "anomalies": anomalies,
            "budget": budget,
            "failed": {"n": failed_total.get("n") or 0, "n_24h": failed_total.get("n_24h") or 0}, "recent_failed": recent_failed}


def emails(limit=200):
    conn = get_conn()
    now = datetime.now(); week = _stamp(now - timedelta(days=7)); today = now.strftime("%Y-%m-%d")
    rows = _rows_dict(conn, "SELECT e.id, e.restaurant_id, r.name AS restaurant, r.location_group AS brand, e.email_type, e.to_email, e.subject, e.sent_at, e.status, e.error FROM email_log e LEFT JOIN restaurants r ON r.id=e.restaurant_id ORDER BY e.id DESC LIMIT ?", (limit,))
    by_type = _rows_dict(conn, "SELECT email_type, COUNT(*) AS n, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed FROM email_log WHERE sent_at >= ? GROUP BY email_type ORDER BY n DESC", (week,))
    daily = _rows_dict(conn, "SELECT substr(sent_at,1,10) AS day, COUNT(*) AS n, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed FROM email_log WHERE sent_at >= ? GROUP BY day ORDER BY day", (_stamp(now - timedelta(days=30)),))
    storms = _rows_dict(conn, "SELECT to_email, COUNT(*) AS n FROM email_log WHERE sent_at >= ? GROUP BY to_email HAVING n >= 8 ORDER BY n DESC", (today,))
    suppressed = _rows_dict(conn, "SELECT * FROM email_suppressions ORDER BY rowid DESC LIMIT 50")
    # Whether any of it was worth sending. Everything above counts what went
    # OUT. Read as a floor, not a rate — Apple Mail pre-fetches images (an
    # open nobody performed) and a reader with images off never registers.
    engagement = _rows_dict(conn,
        "SELECT email_type, COUNT(*) AS sent, "
        "SUM(CASE WHEN opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opened, "
        "SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicked "
        "FROM email_log WHERE sent_at >= ? AND status != 'failed' "
        "GROUP BY email_type ORDER BY sent DESC", (_stamp(now - timedelta(days=30)),))
    for row in engagement:
        row["open_rate"] = (round(100.0 * (row["opened"] or 0) / row["sent"], 1)
                            if row["sent"] else None)
    totals = _one_dict(conn, "SELECT COUNT(*) AS n, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed FROM email_log WHERE sent_at >= ?", (today,)) or {}
    conn.close()
    return {"ok": True, "rows": rows, "by_type": by_type, "daily": daily, "storms": storms,
            "suppressed": suppressed, "today": totals, "engagement": engagement}


def notifications(limit=200):
    conn = get_conn()
    now = datetime.now(); today = now.strftime("%Y-%m-%d"); week = _stamp(now - timedelta(days=7))
    pushes = _rows_dict(conn, "SELECT p.id, p.restaurant_id, r.name AS restaurant, p.device_token_id, d.user_id, u.username, p.alert_type, p.status, p.ok, p.attempts, p.error, p.created_at FROM push_deliveries p LEFT JOIN restaurants r ON r.id=p.restaurant_id LEFT JOIN device_tokens d ON d.id=p.device_token_id LEFT JOIN users u ON u.id=d.user_id ORDER BY p.id DESC LIMIT ?", (limit,))
    devices = _rows_dict(conn, "SELECT d.id, d.restaurant_id, r.name AS restaurant, u.username, d.environment, d.created_at, d.last_success_at, d.consecutive_failures, d.disabled_reason FROM device_tokens d LEFT JOIN restaurants r ON r.id=d.restaurant_id LEFT JOIN users u ON u.id=d.user_id ORDER BY d.id DESC")
    alerts = _rows_dict(conn, "SELECT a.id, a.restaurant_id, r.name AS restaurant, a.alert_type, a.review_id, a.fired_at FROM alert_log a LEFT JOIN restaurants r ON r.id=a.restaurant_id ORDER BY a.id DESC LIMIT ?", (limit,))
    by_type = _rows_dict(conn, "SELECT alert_type, COUNT(*) AS n FROM alert_log WHERE fired_at >= ? GROUP BY alert_type ORDER BY n DESC", (week,))
    storms = _rows_dict(conn, "SELECT a.restaurant_id, r.name AS restaurant, COALESCE(r.alert_max_per_day,0) AS cap, COUNT(*) AS n FROM alert_log a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE a.fired_at >= ? GROUP BY a.restaurant_id HAVING n >= 10 ORDER BY n DESC", (today,))
    caps = _rows_dict(conn, "SELECT id AS restaurant_id, name AS restaurant, alert_max_per_day AS cap FROM restaurants WHERE COALESCE(alert_max_per_day,0) > 0 ORDER BY name")
    scheduled = _rows_dict(conn, "SELECT p.id, p.restaurant_id, r.name AS restaurant, p.platform, p.content_type, p.topic, p.scheduled_for, p.status, p.error, p.attempts FROM marketing_scheduled_posts p LEFT JOIN restaurants r ON r.id=p.restaurant_id ORDER BY p.scheduled_for DESC LIMIT 60")
    today_push = _one_dict(conn, "SELECT SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS sent, SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failed FROM push_deliveries WHERE created_at >= ?", (today,)) or {}
    # Whether any of it was worth sending. Everything above counts what went
    # OUT; this is the first thing in the product that counts what came back.
    # Thirty days rather than seven: several of these types fire weekly, so a
    # seven-day open rate for them is one notification wide.
    month = _stamp(now - timedelta(days=30))
    engagement = _rows_dict(conn,
        "SELECT a.alert_type, COUNT(*) AS delivered, "
        "(SELECT COUNT(*) FROM notification_opens o WHERE o.alert_type=a.alert_type "
        " AND o.opened_at >= ?) AS opened "
        "FROM alert_log a WHERE a.fired_at >= ? GROUP BY a.alert_type ORDER BY delivered DESC",
        (month, month))
    for row in engagement:
        row["open_rate"] = (round(100.0 * row["opened"] / row["delivered"], 1)
                            if row["delivered"] else None)
    ignored = [r for r in engagement
               if r["delivered"] >= 20 and not r["opened"]]
    conn.close()
    return {"ok": True, "pushes": pushes, "devices": devices, "alerts": alerts,
            "by_type": by_type, "storms": storms, "caps": caps,
            "scheduled_posts": scheduled, "today_push": today_push,
            "engagement": engagement, "ignored": ignored}


def billing():
    recs, _ = _records()
    rows = []
    live = {}
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if key:
        try:
            import stripe as _stripe
            import config as _config
            _stripe = _config.stripe_api(key)
            for r in recs:
                cid = r["billing"]["stripe_customer_id"]
                if not cid:
                    continue
                subs = _stripe.Subscription.list(customer=cid, status="all", limit=3)
                for s in subs.auto_paging_iter():
                    item = (s["items"]["data"] or [None])[0]
                    live[r["id"]] = {"status": s["status"], "amount": (item["price"]["unit_amount"] / 100 if item and item["price"].get("unit_amount") else None),
                                     "interval": item["price"]["recurring"]["interval"] if item and item["price"].get("recurring") else None,
                                     "trial_end": s.get("trial_end"), "current_period_end": s.get("current_period_end"),
                                     "cancel_at_period_end": s.get("cancel_at_period_end"), "canceled_at": s.get("canceled_at")}
                    break
        except Exception as e:
            live["_error"] = str(e)[:200]
    for r in recs:
        b = r["billing"]
        rows.append({"restaurant_id": r["id"], "restaurant": r["name"], "brand": r["brand"], "owner": (r["owner"] or {}).get("username"),
                     "owner_email": r["owner_email"], "stripe_customer_id": b["stripe_customer_id"], "status": b["status"], "tier": b["tier"],
                     "modules": b["modules"], "monthly": b["monthly"], "contract_status": b["contract_status"], "envelope_id": b["envelope_id"],
                     "created_at": r["created_at"], "is_demo": r["is_demo"], "is_admin_home": r["is_admin_home"], "live": live.get(r["id"])})
    rows.sort(key=lambda x: ({"past_due": 0, "churned": 1, "canceled": 1, "trial": 2, "active": 3, "internal": 4, "paused": 1}.get(x["status"], 2), x["restaurant"].lower()))
    try:
        import admin_events
        events = admin_events.recent(limit=120)
    except Exception:
        events = []
    return {"ok": True, "rows": rows, "events": events, "stripe_live": bool(key), "stripe_error": live.get("_error"),
            "mrr": sum(x["monthly"] for x in rows if not x["is_demo"] and not x.get("is_admin_home"))}


# The scheduler's jobs, by the name ops.run_job records them under, so the
# console can show the same rows the scheduler writes and run one on demand.
RUNNABLE_JOBS = {
    "review_fetch":            {"cadence": "8am / 12pm / 4pm / 8pm CT", "what": "Fetch new reviews and draft replies", "target": ("scheduler", "run_daily_fetch")},
    "quality_calibration":     {"cadence": "Sunday 5am CT", "what": "Suggest Shift Quality weights (calibrate_weights, what Apply writes)", "target": ("strategy_jobs", "run_quality_calibration")},
    "schedule_outcomes":       {"cadence": "Monday 4am CT", "what": "Record what each published week actually did, by daypart", "target": ("strategy_jobs", "run_schedule_outcomes")},
    # Safe to run on demand: a re-check is written once and accrual reads
    # forward from each tracker's accrued_through, so a second pass adds
    # nothing. Sends nothing.
    "outcome_rechecks":        {"cadence": "6am CT daily", "what": "Re-check measured results at 90 days and accrue measured savings day by day", "target": ("strategy_jobs", "run_outcome_rechecks")},
    "reservation_sync":        {"cadence": "Wednesday 5am CT", "what": "Reservation feeds into events & reservations (no provider live yet)", "target": ("reservation_feeds", "run_reservation_sync")},
    "weekly_digests":          {"cadence": "9am on each client's digest day", "what": "Email weekly digests", "target": ("scheduler", "run_weekly_digests"), "sends": True},
    "pos_sync":                {"cadence": "3am nightly", "what": "Pull yesterday's Toast sales and labor", "target": ("scheduler", "run_toast_sync")},
    "inventory_depletion":     {"cadence": "5am nightly", "what": "Deplete inventory from POS sales", "target": ("scheduler", "run_daily_depletion_sync")},
    "marketing_metrics_sync":  {"cadence": "4am nightly", "what": "Refresh Instagram / Facebook post metrics", "target": ("scheduler", "run_marketing_metrics_sync")},
    "refresh_tokens":          {"cadence": "7am daily", "what": "Renew expiring OAuth tokens", "target": ("scheduler", "refresh_expiring_tokens")},
    "onboarding_emails":       {"cadence": "10am daily", "what": "Send day-2 / day-7 / day-30 onboarding emails", "target": ("scheduler", "run_onboarding_sequence"), "sends": True},
    "stale_inventory":         {"cadence": "Mon 10am", "what": "Nudge clients whose counts are stale", "target": ("scheduler", "check_stale_inventory"), "sends": True},
    "inactive_clients":        {"cadence": "Mon 11am", "what": "Flag clients who haven't signed in", "target": ("scheduler", "check_inactive_clients"), "sends": True},
    "toast_optin_invites":     {"cadence": "daily, for yesterday", "what": "Text opt-in invites to yesterday's Toast guests", "target": ("guest_marketing", "run_toast_optin_invites"), "sends": True},
    "review_request_followups":{"cadence": "hourly", "what": "Text post-visit review requests", "target": ("guest_marketing", "run_review_request_followups"), "sends": True},
    # Safe to run on demand despite "sends": every milestone fires at most
    # once ever (UNIQUE on restaurant_id + key), so a second run notifies
    # nobody.
    "milestones":              {"cadence": "9am local", "what": "Fire savings / anniversary / goal milestones", "target": ("strategy_jobs", "run_milestones"), "sends": True},
    "ops_failure_digest":      {"cadence": "8am daily", "what": "Email Will the failure digest", "target": ("ops", "send_failure_digest"), "sends": True},
    "backup_db":               {"cadence": "2am nightly", "what": "Back the SQLite database up", "target": ("scheduler", "backup_db")},
    "weekly_plan":             {"cadence": "Mon 7am local", "what": "The agent files the week's three actions as issues", "target": ("strategy_jobs", "run_weekly_plan")},
    "recipe_drafts":           {"cadence": "Tue 5am local", "what": "Draft recipes for POS dishes that have none", "target": ("strategy_jobs", "run_recipe_drafts")},
    "trusted_orders":          {"cadence": "Mon 8am local", "what": "Queue supplier orders that have earned it, with an hour to undo", "target": ("strategy_jobs", "run_trusted_orders"), "sends": True},
    "auto_publish_schedule":   {"cadence": "Fri 9am local", "what": "Queue the unedited Thursday draft to publish at 11am", "target": ("scheduler", "run_auto_publish_schedules"), "sends": True},
    "restore_drill":           {"cadence": "2nd of Jan / Apr / Jul / Oct, after the 2am backup", "what": "Restore the newest snapshot to scratch and prove it opens, migrates and kept its tokens", "target": ("scheduler", "run_restore_drill")},
    # Safe to run on demand: every night claims (restaurant, date, version)
    # and a finished version is never re-run, so a second pass does nothing.
    "dsr_sweep":               {"cadence": "every 10 minutes", "what": "Nightly DSR: past each close, poll the POS close, collect, write, finalise (provisional only when sales are missing at the deadline; v2 when they land)", "target": ("dsr.pipeline", "run_sweep")},
    # Safe to run on demand despite "sends": a held push is taken
    # (held -> sending) before it goes, so a second pass finds nothing.
    "dsr_delivery":            {"cadence": "every 10 minutes", "what": "Send the DSR pushes held through each restaurant's quiet hours, once they end", "target": ("dsr.deliver", "release_held"), "sends": True},
}


def run_job_now(name, actor):
    """Run one scheduled job right now, on a background thread, recorded in
    job_runs exactly like a scheduled run — context says who asked."""
    import importlib, threading
    spec = RUNNABLE_JOBS.get(name)
    if not spec:
        return {"ok": False, "error": "Unknown job"}
    conn = get_conn()
    running = _one_dict(conn, "SELECT id, started_at FROM job_runs WHERE job=? AND finished_at IS NULL AND started_at >= ? ORDER BY id DESC LIMIT 1",
                   (name, _stamp(datetime.now() - timedelta(minutes=30))))
    conn.close()
    if running:
        return {"ok": False, "error": f"{name} is already running (started {running['started_at']})"}
    mod, fn_name = spec["target"]
    try:
        fn = getattr(importlib.import_module(mod), fn_name)
    except Exception as e:
        return {"ok": False, "error": f"Could not load {mod}.{fn_name}: {e}"}
    import ops
    if name == "toast_optin_invites":
        from datetime import date as _d
        target = lambda: fn(business_date=_d.today() - timedelta(days=1))
    else:
        target = fn
    ctx = f"manual by {actor}"

    def _go():
        try:
            ops.run_job(name, target, context=ctx)
        except Exception:
            pass  # run_job already recorded the failure
    threading.Thread(target=_go, name=f"admin-run-{name}", daemon=True).start()
    return {"ok": True, "job": name, "context": ctx}


def set_alert_cap(rid, max_per_day, actor):
    """The storm brake: at most N alerts a day for one restaurant, 0 = off.
    Enforced in notify._check_dnd; this only sets the number."""
    try:
        n = int(max_per_day)
    except (TypeError, ValueError):
        return {"ok": False, "error": "max_per_day must be a number"}
    if n < 0 or n > 500:
        return {"ok": False, "error": "max_per_day must be between 0 and 500"}
    from models import update_restaurant
    if not get_restaurant(rid):
        return {"ok": False, "error": "Not found"}
    update_restaurant(rid, {"alert_max_per_day": n})
    try:
        import admin_events
        admin_events.record("admin", "alert_cap.set", restaurant_id=rid, amount=n,
                            summary=f"Alert cap set to {n or 'off'} by {actor}")
    except Exception:
        pass
    return {"ok": True, "restaurant_id": rid, "max_per_day": n}


def jobs():
    conn = get_conn()
    now = datetime.now(); day = _stamp(now - timedelta(days=1)); week = _stamp(now - timedelta(days=7))
    failures = _rows_dict(conn, "SELECT id, job, error, context, created_at FROM job_failures ORDER BY id DESC LIMIT 100")
    grouped = _rows_dict(conn, "SELECT job, COUNT(*) AS n, MAX(created_at) AS last_at, MIN(created_at) AS first_at, MAX(error) AS sample FROM job_failures WHERE created_at >= ? GROUP BY job ORDER BY n DESC", (week,))
    runs = _rows_dict(conn, "SELECT id, job, started_at, finished_at, duration_ms, ok, error, context FROM job_runs ORDER BY id DESC LIMIT 120")
    last_ok = _rows_dict(conn, "SELECT job, MAX(finished_at) AS last_ok, ROUND(AVG(duration_ms)) AS avg_ms, COUNT(*) AS runs FROM job_runs WHERE ok=1 AND started_at >= ? GROUP BY job", (week,))
    stuck = _rows_dict(conn, "SELECT id, job, started_at, context FROM job_runs WHERE finished_at IS NULL AND started_at < ? ORDER BY started_at", (_stamp(now - timedelta(minutes=30)),))
    posts = _rows_dict(conn, "SELECT p.id, r.name AS restaurant, p.platform, p.topic, p.scheduled_for, p.status, p.attempts, p.error FROM marketing_scheduled_posts p LEFT JOIN restaurants r ON r.id=p.restaurant_id WHERE p.status IN ('pending','failed') ORDER BY p.scheduled_for LIMIT 40")
    conn.close()
    from status_manager import scheduler_heartbeat_age_minutes
    try:
        hb = scheduler_heartbeat_age_minutes()
    except Exception:
        hb = None
    # In-flight async jobs. These used to be read out of two module-level
    # dicts, so the page only ever showed the jobs belonging to whichever
    # worker served the request; ops.async_jobs is one table every worker
    # writes to.
    inflight = []
    try:
        import ops as _ops
        for j in _ops.inflight_async_jobs(limit=20):
            inflight.append({"kind": j["kind"], "id": j["job_id"], "status": j["status"],
                             "restaurant_id": j.get("restaurant_id"), "started": j.get("created_at")})
    except Exception:
        pass
    schedule = [{"job": k, "cadence": v["cadence"], "runnable": True, "sends": v.get("sends", False), "what": v["what"]} for k, v in RUNNABLE_JOBS.items()]
    schedule.append({"job": "scheduled_posts", "cadence": f"every {os.getenv('SCHEDULER_TICK_SECONDS', '300')}s", "runnable": False, "sends": True, "what": "Publishes due marketing posts"})
    return {"ok": True, "heartbeat_minutes": hb, "failures": failures, "grouped": grouped, "runs": runs, "last_ok": last_ok,
            "stuck": stuck, "scheduled_posts": posts, "inflight": inflight, "schedule": schedule}


def issues():
    ov = overview()
    return {"ok": True, "issues": ov["issues"]}


def resolve_issue(key, note, actor):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO admin_issue_resolutions (key, resolved_at, note, actor) VALUES (?, datetime('now'), ?, ?)", (key, note, actor))
    conn.commit(); conn.close()
    return {"ok": True}


def unresolve_issue(key):
    conn = get_conn()
    conn.execute("DELETE FROM admin_issue_resolutions WHERE key=?", (key,))
    conn.commit(); conn.close()
    return {"ok": True}


def resolved_issues():
    conn = get_conn()
    rows = _rows_dict(conn, "SELECT key, resolved_at, note, actor FROM admin_issue_resolutions ORDER BY resolved_at DESC LIMIT 100")
    conn.close()
    return {"ok": True, "resolved": rows}


_ACTIVITY_LABELS = {
    "review_approved": "Approved a reply", "review_undo": "Undid an approval", "reviews_bulk_approved": "Published replies in bulk",
    "auto_approve_changed": "Changed auto-approve", "admin_settings_update": "Admin updated settings", "sessions_revoked_others": "Signed out other devices",
    "marketing_emails_changed": "Changed email preferences", "profile_updated": "Updated profile", "two_fa_enabled": "Turned on 2FA",
    "two_fa_disabled": "Turned off 2FA", "team_member_invited": "Invited a teammate", "team_member_revoked": "Removed a teammate",
    "data_exported": "Exported data", "recovery_email_set": "Set a recovery email", "password_changed": "Changed password",
    "email_changed": "Changed email", "backup_codes_regenerated": "Regenerated backup codes", "tab_view": None,
}


def activity(limit=60, d=None):
    conn = get_conn()
    now = datetime.now(); since = _stamp(now - timedelta(days=14))
    ev = []
    for a in _rows_dict(conn, "SELECT a.restaurant_id, r.name, a.event_type, a.event_data, a.created_at FROM activity_log a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE a.created_at >= ? ORDER BY a.id DESC LIMIT 120", (since,)):
        label = _ACTIVITY_LABELS.get(a["event_type"], a["event_type"].replace("_", " ").capitalize() if a["event_type"] else None)
        if label is None:
            continue
        ev.append({"at": a["created_at"], "restaurant_id": a["restaurant_id"], "restaurant": a["name"], "kind": "account", "label": label, "tone": "neutral"})
    for l in _rows_dict(conn, "SELECT l.restaurant_id, r.name, l.device_type, l.created_at FROM login_history l LEFT JOIN restaurants r ON r.id=l.restaurant_id WHERE l.created_at >= ? ORDER BY l.id DESC LIMIT 60", (since,)):
        ev.append({"at": l["created_at"], "restaurant_id": l["restaurant_id"], "restaurant": l["name"], "kind": "login", "label": f"Signed in ({l['device_type'] or 'web'})", "tone": "neutral"})
    for r in _rows_dict(conn, "SELECT id, name, created_at FROM restaurants WHERE created_at >= ? ORDER BY id DESC", (since,)):
        ev.append({"at": r["created_at"], "restaurant_id": r["id"], "restaurant": r["name"], "kind": "signup", "label": "Restaurant created", "tone": "good"})
    for e in _rows_dict(conn, "SELECT e.restaurant_id, r.name, e.email_type, e.sent_at, e.error FROM email_log e LEFT JOIN restaurants r ON r.id=e.restaurant_id WHERE e.status='failed' AND e.sent_at >= ? ORDER BY e.id DESC LIMIT 40", (since,)):
        ev.append({"at": e["sent_at"], "restaurant_id": e["restaurant_id"], "restaurant": e["name"], "kind": "email", "label": f"Email failed · {e['email_type']}", "tone": "bad", "detail": e["error"]})
    for p in _rows_dict(conn, "SELECT p.restaurant_id, r.name, p.alert_type, p.created_at, p.error FROM push_deliveries p LEFT JOIN restaurants r ON r.id=p.restaurant_id WHERE p.ok=0 AND p.created_at >= ? ORDER BY p.id DESC LIMIT 40", (since,)):
        ev.append({"at": p["created_at"], "restaurant_id": p["restaurant_id"], "restaurant": p["name"], "kind": "push", "label": f"Push failed · {p['alert_type']}", "tone": "bad", "detail": p["error"]})
    for j in _rows_dict(conn, "SELECT job, error, created_at FROM job_failures WHERE created_at >= ? ORDER BY id DESC LIMIT 40", (since,)):
        ev.append({"at": j["created_at"], "restaurant_id": None, "restaurant": "Platform", "kind": "job", "label": f"Job failed · {j['job']}", "tone": "bad", "detail": (j["error"] or "")[:140]})
    for e in _rows_dict(conn, "SELECT e.restaurant_id, r.name, e.source, e.event_type, e.summary, e.created_at FROM admin_events e LEFT JOIN restaurants r ON r.id=e.restaurant_id WHERE e.created_at >= ? ORDER BY e.id DESC LIMIT 40", (since,)):
        bad = any(x in e["event_type"] for x in ("failed", "deleted", "canceled", "past_due"))
        ev.append({"at": e["created_at"], "restaurant_id": e["restaurant_id"], "restaurant": e["name"] or "Unmatched customer", "kind": e["source"],
                   "label": f"{e['source'].capitalize()} · {e['summary'] or e['event_type']}", "tone": "bad" if bad else ("good" if e["event_type"] in ("invoice.paid", "contract.signed") else "neutral")})
    for a in _rows_dict(conn, "SELECT a.restaurant_id, r.name, a.alert_type, a.fired_at FROM alert_log a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE a.fired_at >= ? AND a.alert_type IN ('1star','health','labor_over') ORDER BY a.id DESC LIMIT 30", (since,)):
        ev.append({"at": a["fired_at"], "restaurant_id": a["restaurant_id"], "restaurant": a["name"], "kind": "alert", "label": f"Alert fired · {a['alert_type']}", "tone": "warn"})
    conn.close()
    ev.sort(key=lambda e: e["at"] or "", reverse=True)
    return {"ok": True, "events": ev[:limit]}


def search(q):
    q = (q or "").strip()
    if len(q) < 2:
        return {"ok": True, "results": []}
    like = f"%{q}%"
    conn = get_conn()
    res = []
    for r in _rows_dict(conn, "SELECT id, name, location_group, location_name, neighborhood, owner_email, owner_name, stripe_customer_id FROM restaurants WHERE name LIKE ? OR location_group LIKE ? OR location_name LIKE ? OR neighborhood LIKE ? OR owner_email LIKE ? OR owner_name LIKE ? OR stripe_customer_id LIKE ? OR CAST(id AS TEXT) = ? LIMIT 20", (like, like, like, like, like, like, like, q)):
        res.append({"type": "location", "id": r["id"], "title": r["name"], "sub": " · ".join(x for x in [r["location_group"], r["location_name"], r["neighborhood"]] if x) or r["owner_email"]})
    for u in _rows_dict(conn, "SELECT u.id, u.username, u.email, u.role, u.restaurant_id, r.name FROM users u LEFT JOIN restaurants r ON r.id=u.restaurant_id WHERE u.username LIKE ? OR u.email LIKE ? OR CAST(u.id AS TEXT) = ? LIMIT 20", (like, like, q)):
        res.append({"type": "owner" if u["role"] in ("client", "owner") else "login", "id": u["restaurant_id"], "user_id": u["id"], "title": u["username"], "sub": f"{u['email']} · {u['name'] or 'no restaurant'} · {u['role']}"})
    for g in _rows_dict(conn, "SELECT location_group AS g, COUNT(*) AS n FROM restaurants WHERE location_group LIKE ? GROUP BY location_group LIMIT 10", (like,)):
        res.append({"type": "brand", "id": None, "title": g["g"], "sub": f"{g['n']} locations"})
    conn.close()
    return {"ok": True, "results": res[:40]}


# ── recommendation acceptance (internal only) ────────────────────────────────
#
# Read from rec_ledger (rec_instances + rec_events): one row per episode of a
# recommendation. Admin-only — no owner ever sees these rates. An episode
# nobody answered counts in every denominator as ignored: dropping the
# ignored ones would report what people did with the recommendations they
# chose to touch, and every rate would read high.

RAS_WEIGHTS = {"opened": 0.15, "accepted": 0.40, "completed": 0.25, "outcome": 0.20}
RAS_MIN_N = 20            # below this, a score is noise: rates only, no RAS
_Z90 = 1.645


def _wilson(k, n, z=_Z90):
    """90% Wilson interval for k of n, as (low, high) shares; None when n=0."""
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return (round(max(0.0, (c - m) / d), 3), round(min(1.0, (c + m) / d), 3))


def _ras_block(eps):
    """Rates and the Recommendation Acceptance Score for a set of episodes."""
    n = len(eps)
    if not n:
        return {"n": 0, "ras": None}
    k = {x: sum(1 for e in eps if e.get(x)) for x in ("opened", "evidence", "accepted", "completed", "implemented",
                                                       "dismissed", "snoozed", "ignored", "outcome")}
    took = sum(1 for e in eps if e["accepted"] or e["completed"] or e.get("implemented"))
    # "Improved" only among what was taken: a verdict on an episode nobody
    # took is not the recommendation working, and counting it let the
    # outcome rate pass 100%. Verdicts are read through
    # rec_learning.learned_verdict (a disowned, conditions-changed,
    # informational, faded or reversed result is never a win), and the rate
    # is improved ÷ MEASURED — taken with a clear verdict — not ÷ taken
    # (confidence audit E13, CA2 #10).
    _taken = [e for e in eps if e["accepted"] or e["completed"] or e.get("implemented")]
    k["improved"] = sum(1 for e in _taken if e["improved"])
    k["measured"] = sum(1 for e in _taken if e.get("measured"))
    rates = {"opened": k["opened"] / n, "accepted": took / n, "completed": k["completed"] / n,
             # Of what was taken and measured, how much improved.
             "outcome": (k["improved"] / k["measured"]) if k["measured"] else 0.0}
    acts = sorted(e["hours_to_act"] for e in eps if e["hours_to_act"] is not None)
    out = {"n": n, "shown": sum(1 for e in eps if e["shown"]), "opened": k["opened"], "evidence": k["evidence"],
           "accepted": took, "completed": k["completed"], "implemented": k["implemented"],
           "dismissed": k["dismissed"], "snoozed": k["snoozed"],
           "ignored": k["ignored"], "outcomes": k["outcome"], "improved": k["improved"],
           "measured": k["measured"],
           "open_rate": round(rates["opened"], 3), "accept_rate": round(rates["accepted"], 3),
           "complete_rate": round(rates["completed"], 3), "outcome_rate": round(rates["outcome"], 3),
           "dismiss_rate": round(k["dismissed"] / n, 3), "ignore_rate": round(k["ignored"] / n, 3),
           "accept_ci90": _wilson(took, n),
           "median_hours_to_act": acts[len(acts) // 2] if acts else None,
           "ras": None}
    if n >= RAS_MIN_N:
        out["ras"] = round(100 * sum(RAS_WEIGHTS[x] * rates[x] for x in RAS_WEIGHTS), 1)
    return out


def _episodes(conn, since, restaurant_id=None):
    where, args = "i.created_at >= ?", [since]
    if restaurant_id:
        where += " AND i.restaurant_id=?"
        args.append(restaurant_id)
    inst = _rows_dict(conn, "SELECT i.*, r.name AS restaurant FROM rec_instances i LEFT JOIN restaurants r ON r.id=i.restaurant_id "
                            f"WHERE {where}", tuple(args))
    if not inst:
        return []
    evs = {}
    for e in _rows_dict(conn, "SELECT e.rec_id, e.event, e.surface, e.meta, e.at, e.role FROM rec_events e JOIN rec_instances i "
                              f"ON i.rec_id=e.rec_id WHERE {where} ORDER BY e.id", tuple(args)):
        evs.setdefault(e["rec_id"], []).append(e)
    import rec_ledger
    trackers = _trackers(conn, sorted({i["tracker_id"] for i in inst if i.get("tracker_id")}))
    out = []
    for i in inst:
        es = evs.get(i["rec_id"], [])
        names = {e["event"] for e in es}
        # Only what an owner was shown is a recommendation they could take
        # or ignore. Bookkeeping keys (a kind restored, weights applied, an
        # on-call ask) and episodes no surface ever showed — an answer or a
        # verdict arriving with nothing shown behind it — stay out of every
        # rate.
        if "shown" not in names or not rec_ledger.counts_in_acceptance(i["key"]):
            continue
        # A superseded episode is the same recommendation carried on under
        # new content by a newer one: counting both would count one
        # recommendation twice, and neither was answered nor ignored.
        if i["status"] == "superseded":
            continue
        # The episode's measured result, read ONLY through
        # rec_learning.learned_verdict (confidence audit E13): the ledger's
        # first outcome event was taken as the verdict, so a result the
        # owner disowned or that reversed at its re-check counted as a win.
        verdict = learned_episode_verdict(es, trackers.get(i.get("tracker_id")))
        answered = names & {"accepted", "completed", "dismissed", "implemented"}
        first_act = next((e["at"] for e in es if e["event"] in ("accepted", "completed", "dismissed",
                                                                 "implemented")), None)
        hours = None
        if first_act:
            a, c = _parse(first_act), _parse(i["created_at"])
            if a and c:
                hours = round(max(0.0, (a - c).total_seconds() / 3600), 1)
        # The ledger's own expiry rule (created 14+ days ago, no answer, not
        # snoozed) — the same one expire_stale closes episodes by.
        ignored = (not answered) and (i["status"] == "expired" or rec_ledger.is_stale(i))
        shown_surfaces = sorted({e["surface"] for e in es if e["event"] == "shown" and e["surface"]})
        try:
            sources = json.loads(i["evidence_sources"]) if i["evidence_sources"] else []
        except (TypeError, ValueError):
            sources = []
        out.append({"rec_id": i["rec_id"], "restaurant_id": i["restaurant_id"], "restaurant": i["restaurant"],
                    "key": i["key"], "kind": i["kind"] or (i["key"] or "").split(":", 1)[0], "module": i["module"] or "—",
                    "title": i["title"], "status": i["status"],
                    "surface": i["first_surface"] or (shown_surfaces[0] if shown_surfaces else "unknown"),
                    "has_dollars": bool(i["dollar_value"]), "dollar_value": i["dollar_value"],
                    "confidence_band": i["confidence_band"] or "none", "cross_module": bool(i["cross_module"]),
                    "model_written": bool(i["model_written"]), "cavnar_completes": bool(i["cavnar_completes"]),
                    "sources": sources, "position": i["first_position"],
                    "shown": "shown" in names, "opened": bool(names & {"opened", "evidence_viewed"}) or bool(answered),
                    "evidence": "evidence_viewed" in names, "accepted": "accepted" in names,
                    "completed": "completed" in names, "implemented": "implemented" in names,
                    "dismissed": "dismissed" in names, "snoozed": "snoozed" in names,
                    # "Could not be measured" is not an outcome (ROI #2).
                    "ignored": ignored, "outcome": verdict in _CLEAR, "improved": verdict == "improved",
                    "measured": verdict in _CLEAR, "verdict": verdict,
                    # The confidence it was shown with (K3 snapshot).
                    **{c: _col_or_none(i, c) for c in _SNAPSHOT_COLS},
                    "reason_codes": sorted({(_meta_of(e) or {}).get("reason_code") for e in es
                                            if (_meta_of(e) or {}).get("reason_code")}),
                    "hours_to_act": hours, "responder_role": next((e["role"] for e in es if e["event"] in (
                        "accepted", "completed", "dismissed") and e["role"]), None)})
    return out


_CLEAR = ("improved", "worsened", "no_clear_change")
_SNAPSHOT_COLS = ("confidence_pct", "evidence_pct", "accuracy_pct", "accuracy_n", "freshness_pct",
                  "freshness_as_of", "trust_version")
_TRACKER_COLS = ("id, status, verdict, dollars_monthly, evaluate_on, started_on, metric, after_start, "
                 "after_end, recheck_verdict, owner_checkin, source_key")


def _col_or_none(row, name):
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def _meta_of(e):
    try:
        return json.loads(e.get("meta") or "{}") or {}
    except (TypeError, ValueError):
        return {}


def _trackers(conn, tids):
    """{tracker id: recommendation_outcomes row} for the episodes' trackers."""
    out = {}
    for n in range(0, len(tids), 400):
        chunk = tids[n:n + 400]
        marks = ",".join("?" for _ in chunk)
        try:
            rows = _rows_dict(conn, f"SELECT {_TRACKER_COLS} FROM recommendation_outcomes WHERE id IN ({marks})",
                              tuple(chunk))
        except Exception:
            rows = _rows_dict(conn, f"SELECT id, status, verdict, dollars_monthly, evaluate_on FROM "
                                    f"recommendation_outcomes WHERE id IN ({marks})", tuple(chunk))
        for r in rows:
            out[r["id"]] = r
    return out


def learned_episode_verdict(events, tracker=None):
    """An episode's measured result through rec_learning.learned_verdict —
    the ledger's latest outcome event, else its tracker's current verdict,
    with the owner's latest check-in — or None when nothing was measured.
    The same reading rec_learning gives its own record."""
    import rec_learning
    verdict = None
    for e in events or []:
        if e.get("event") == "outcome":
            v = _meta_of(e).get("verdict")
            if v:
                verdict = v
    if tracker and tracker.get("status") == "evaluated":
        verdict = tracker.get("verdict") or verdict or "unknown"
    if verdict is None:
        return None
    checkins = [_meta_of(e) for e in (events or []) if e.get("event") == "checkin"]
    return rec_learning.learned_verdict(verdict, tracker, checkins[-1] if checkins else None)


def _pct_band(pct):
    import confidence_engine
    return confidence_engine.band(pct) if pct is not None else "not snapshotted"


def _group(eps, key_fn, label_fn=None, min_n=1):
    groups = {}
    for e in eps:
        groups.setdefault(key_fn(e), []).append(e)
    rows = []
    for g, members in groups.items():
        b = _ras_block(members)
        if b["n"] < min_n:
            continue
        b["group"] = label_fn(g) if label_fn else g
        rows.append(b)
    rows.sort(key=lambda r: -r["n"])
    return rows


def _surface_label(surface):
    import rec_ledger
    return rec_ledger.surface_label(surface)


def recommendation_acceptance(days=30, restaurant_id=None):
    """The internal Recommendation Acceptance dashboard: the funnel, the
    score, and what moves it — dollars or none, cross-module, model-written,
    Cavnar-prepared, confidence band, surface — plus the most ignored kinds
    and restaurants showing fatigue."""
    import models
    days = max(1, min(int(days or 30), 365))
    since = _stamp(datetime.utcnow() - timedelta(days=days))
    conn = models.get_conn()
    try:
        try:
            eps = _episodes(conn, since, restaurant_id)
        except Exception as e:           # the ledger table predates this database
            log.warning("recommendation_acceptance unavailable: %s", e)
            eps = []
    finally:
        conn.close()
    total = _ras_block(eps)
    funnel = [{"step": s, "n": total.get(k) or 0} for s, k in (
        ("Shown", "shown"), ("Opened", "opened"), ("Evidence viewed", "evidence"), ("Accepted", "accepted"),
        ("Completed", "completed"), ("Implemented", "implemented"), ("Outcome measured", "outcomes"),
        ("Improved", "improved"))]
    by_kind = _group(eps, lambda e: e["kind"] or "unknown")
    most_ignored = sorted((r for r in by_kind if r["n"] >= 5), key=lambda r: (-r["ignore_rate"], -r["n"]))[:10]
    by_rest = _group(eps, lambda e: (e["restaurant_id"], e["restaurant"] or f"#{e['restaurant_id']}"))
    for r in by_rest:
        r["restaurant_id"], r["group"] = r["group"]
    # Fatigue: plenty shown, little taken — the pattern that ends in an owner
    # tuning the product out.
    fatigue = [r for r in by_rest if r["n"] >= RAS_MIN_N and (r["dismiss_rate"] + r["ignore_rate"]) >= 0.75]
    return {"ok": True, "days": days, "restaurant_id": restaurant_id, "min_n": RAS_MIN_N, "weights": RAS_WEIGHTS,
            "total": total, "funnel": funnel,
            "by_module": _group(eps, lambda e: e["module"]),
            "by_kind": by_kind,
            "by_surface": _group(eps, lambda e: e["surface"], label_fn=_surface_label),
            "by_dollars": _group(eps, lambda e: "has a $ figure" if e["has_dollars"] else "no $ figure"),
            "by_cross_module": _group(eps, lambda e: "cross-module" if e["cross_module"] else "one module"),
            "by_model_written": _group(eps, lambda e: "model-written" if e["model_written"] else "rule-written"),
            "by_cavnar_completes": _group(eps, lambda e: "Cavnar prepares it" if e["cavnar_completes"] else "owner does it"),
            # By the confidence the owner was SHOWN (the K3 snapshot's
            # percentage → band) — not the legacy band column, which mixed
            # card bands, model self-ratings and the review trend's slope
            # (CA1 X5). Episodes from before the snapshot group apart.
            "by_confidence": _group(eps, lambda e: _pct_band(e.get("confidence_pct"))),
            "by_role": _group([e for e in eps if e["responder_role"]], lambda e: e["responder_role"]),
            "by_restaurant": by_rest,
            "most_ignored": most_ignored,
            "fatigue": fatigue}


# ── schedule generation experiments (internal only) ─────────────────────────
#
# The live A/B of schedule generation variants (schedule_experiments, audit
# #50): each generated week's arm, measured on how much of the draft went
# out unedited and on what the week then did. Admin-only — no owner ever
# sees an arm.

def schedule_experiments():
    """Per experiment and arm: weeks, acceptance with a 90% interval,
    outcomes, the draft's Shift Quality and the verdict under the minimum
    sample rule; plus every pin in force."""
    import schedule_experiments as sx
    try:
        return sx.readout()
    except Exception as e:           # the tables predate this database
        log.warning("schedule_experiments unavailable: %s", e)
        return {"ok": True, "experiments": [], "pins": [], "env_pin": None, "rule": sx.RULE,
                "min_weeks": sx.MIN_WEEKS_PER_ARM, "min_restaurants": sx.MIN_RESTAURANTS_PER_ARM,
                "error": "The experiment tables are not on this database yet."}


def set_schedule_experiment_pin(restaurant_id, experiment, arm, by="admin"):
    """Pin one restaurant to an arm ('off' = the control), or unpin (arm
    None) — the per-restaurant kill switch."""
    import schedule_experiments as sx
    import models
    if not models.get_restaurant(int(restaurant_id)):
        return {"ok": False, "error": "No such restaurant."}
    return sx.set_pin(int(restaurant_id), experiment, arm, pinned_by=by)


def promote_schedule_experiment(experiment, arm, by="admin", note=None):
    """The reviewed step that makes an experiment's winning arm the default
    (ROI #46): only the arm the readout's verdict calls, recorded with who
    promoted it; every restaurant then gets that arm from a stored setting —
    no code edit. Undone by revert_schedule_experiment."""
    import schedule_experiments as sx
    return sx.promote(experiment, arm, promoted_by=by, note=note)


def revert_schedule_experiment(experiment, by="admin"):
    import schedule_experiments as sx
    return sx.revert(experiment, reverted_by=by)


# ── recommendation calibration and missed detections (internal only) ────────
#
# ROI audit #43 and #44. Admin-only until the figures have enough behind
# them to be shown to an owner.

CALIBRATION_MIN_N = 5      # pairs per kind before a ratio is called


def recommendation_calibration(days=365, restaurant_id=None):
    """Each recommendation's predicted dollars (what it was shown with,
    rec_instances.dollar_value) against what its tracker measured
    (recommendation_outcomes.dollars_monthly; a no-clear-change result is $0
    realised), by kind. Only episodes taken and measured with a clear
    verdict; a result on a metric with no dollar reading is counted as
    `unpriced` and left out of the ratio. ratio = realised ÷ predicted over
    the kind; within_half = share of pairs whose realised figure landed
    within ±50% of the prediction."""
    import models
    days = max(1, min(int(days or 365), 730))
    since = _stamp(datetime.utcnow() - timedelta(days=days))
    where, args = "i.created_at >= ? AND i.dollar_value IS NOT NULL AND i.dollar_value > 0", [since]
    if restaurant_id:
        where += " AND i.restaurant_id=?"
        args.append(int(restaurant_id))
    conn = models.get_conn()
    try:
        try:
            rows = _rows_dict(conn, "SELECT i.rec_id, i.kind, i.key, i.status, i.dollar_value, o.verdict, "
                                    "o.dollars_monthly, o.id AS tracker_id, o.status AS tracker_status, "
                                    "o.recheck_verdict, o.owner_checkin, o.source_key "
                                    "FROM rec_instances i JOIN recommendation_outcomes o ON o.id=i.tracker_id "
                                    f"WHERE {where} AND o.status='evaluated'", tuple(args))
            _ck = {}
            for n in range(0, len(rows), 400):
                chunk = [r["rec_id"] for r in rows[n:n + 400]]
                marks = ",".join("?" for _ in chunk)
                for e in _rows_dict(conn, f"SELECT rec_id, meta FROM rec_events WHERE event='checkin' "
                                          f"AND rec_id IN ({marks}) ORDER BY at, id", tuple(chunk)):
                    _ck[e["rec_id"]] = _meta_of(e)
        except Exception as e:           # the columns predate this database
            log.warning("recommendation_calibration unavailable: %s", e)
            rows, _ck = [], {}
    finally:
        conn.close()
    by = {}
    import rec_learning
    for r in rows:
        if r["status"] not in ("accepted", "completed", "implemented"):
            continue
        # The verdict through learned_verdict (confidence audit E13): a
        # disowned or conditions-changed result is not a pair at all, and a
        # faded or reversed one realised nothing.
        r["verdict"] = rec_learning.learned_verdict(
            r["verdict"], {"recheck_verdict": r.get("recheck_verdict"), "owner_checkin": r.get("owner_checkin"),
                           "source_key": r.get("source_key")}, _ck.get(r.get("rec_id")))
        k = by.setdefault(r["kind"] or (r["key"] or "").split(":", 1)[0], {"pairs": [], "unpriced": 0})
        if r["verdict"] == "no_clear_change":
            k["pairs"].append((float(r["dollar_value"]), 0.0))
        elif r["verdict"] in ("improved", "worsened") and r["dollars_monthly"] is not None:
            k["pairs"].append((float(r["dollar_value"]), float(r["dollars_monthly"])))
        elif r["verdict"] in ("improved", "worsened"):
            k["unpriced"] += 1
    out = []
    for kind, v in by.items():
        pairs = v["pairs"]
        pred = sum(p for p, _ in pairs)
        real = sum(a for _, a in pairs)
        ratios = sorted(a / p for p, a in pairs if p)
        out.append({"kind": kind, "n": len(pairs), "unpriced": v["unpriced"], "predicted": round(pred, 2),
                    "realised": round(real, 2), "ratio": round(real / pred, 3) if pred else None,
                    "median_ratio": round(ratios[len(ratios) // 2], 3) if ratios else None,
                    "within_half": (round(sum(1 for x in ratios if 0.5 <= x <= 1.5) / len(ratios), 3)
                                    if ratios else None),
                    "enough": len(pairs) >= CALIBRATION_MIN_N})
    out.sort(key=lambda r: (-r["n"], r["kind"]))
    pred = sum(r["predicted"] for r in out)
    real = sum(r["realised"] for r in out)
    return {"ok": True, "days": days, "restaurant_id": restaurant_id, "min_n": CALIBRATION_MIN_N, "by_kind": out,
            "total": {"n": sum(r["n"] for r in out), "predicted": round(pred, 2), "realised": round(real, 2),
                      "ratio": round(real / pred, 3) if pred else None}}


# ── confidence calibration (K7, internal only) ─────────────────────────────
#
# What the owner was told (the Recommendation Confidence % snapshotted at
# delivery, K3) against what happened: taken episodes with a clear learned
# verdict, improved = 1. A reliability table by decile, a Brier score, and
# the same per dimension — each withheld below its floor.

CALIBRATION_FLOOR_N = RAS_MIN_N     # a band's observed rate below this is noise


def confidence_calibration(days=365, restaurant_id=None):
    """{bands:[{range, n, predicted_mean, observed_rate, low, high}], brier,
    by_kind:[{kind, n, predicted_mean, observed_rate, enough}],
    by_dimension:{evidence, accuracy, freshness}, floor_n, distrust:{n, by_kind}}.
    Owners never see a probability; this is Will's view of whether "72%"
    means 72%."""
    import models
    import confidence_engine as ce
    days = max(1, min(int(days or 365), 730))
    since = _stamp(datetime.utcnow() - timedelta(days=days))
    conn = models.get_conn()
    try:
        try:
            eps = _episodes(conn, since, restaurant_id)
        except Exception as e:           # the ledger / snapshot columns predate this database
            log.warning("confidence_calibration unavailable: %s", e)
            eps = []
    finally:
        conn.close()
    scored = [e for e in eps if (e["accepted"] or e["completed"] or e.get("implemented")) and e.get("measured")]

    def pairs(field):
        return [(e.get(field), 1 if e["improved"] else 0) for e in scored if e.get(field) is not None]

    overall = pairs("confidence_pct")
    by_kind = []
    groups = {}
    for p, y, kind in ((e.get("confidence_pct"), 1 if e["improved"] else 0, e["kind"]) for e in scored
                       if e.get("confidence_pct") is not None):
        groups.setdefault(kind, []).append((p, y))
    for kind, ps in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        enough = len(ps) >= CALIBRATION_MIN_N
        by_kind.append({"kind": kind, "n": len(ps), "predicted_mean": round(sum(p for p, _ in ps) / len(ps), 1),
                        "observed_rate": round(100.0 * sum(y for _, y in ps) / len(ps), 1) if enough else None,
                        "enough": enough})
    # The owner's "don't trust the data" answers, counted (E14).
    distrust = [e for e in eps if "dont_trust_data" in (e.get("reason_codes") or [])]
    dk = {}
    for e in distrust:
        dk[e["kind"]] = dk.get(e["kind"], 0) + 1
    return {"ok": True, "days": days, "restaurant_id": restaurant_id, "floor_n": CALIBRATION_FLOOR_N,
            "kind_floor_n": CALIBRATION_MIN_N, "n": len(overall),
            "bands": ce.reliability(overall, CALIBRATION_FLOOR_N),
            "brier": ce.brier(overall, CALIBRATION_FLOOR_N),
            "by_kind": by_kind,
            "by_dimension": {dim: {"bands": ce.reliability(pairs(f"{dim}_pct"), CALIBRATION_FLOOR_N),
                                   "brier": ce.brier(pairs(f"{dim}_pct"), CALIBRATION_FLOOR_N),
                                   "n": len(pairs(f"{dim}_pct"))}
                             for dim in ("evidence", "accuracy", "freshness")},
            "distrust": {"n": len(distrust),
                         "by_kind": sorted(({"kind": k, "n": v} for k, v in dk.items()), key=lambda x: -x["n"])},
            "rule": ("taken recommendations with a clear verdict read through learned_verdict; improved = 1; "
                     "an observed rate only at the floor")}


def missed_detections(days=30, restaurant_id=None, limit=200):
    """Problems that surfaced — an alert, an issue, a close-out 86 — with no
    recommendation covering their subject shown in the days before
    (rec_ledger.note_problem): by source, by subject kind, and the latest
    rows."""
    import models
    import rec_ledger
    days = max(1, min(int(days or 30), 365))
    since = _stamp(datetime.utcnow() - timedelta(days=days))
    where, args = "m.detected_at >= ?", [since]
    if restaurant_id:
        where += " AND m.restaurant_id=?"
        args.append(int(restaurant_id))
    conn = models.get_conn()
    try:
        try:
            rows = _rows_dict(conn, "SELECT m.*, r.name AS restaurant FROM rec_missed_detections m "
                                    "LEFT JOIN restaurants r ON r.id=m.restaurant_id "
                                    f"WHERE {where} ORDER BY m.detected_at DESC, m.id DESC", tuple(args))
        except Exception as e:           # the table predates this database
            log.warning("missed_detections unavailable: %s", e)
            rows = []
    finally:
        conn.close()
    by_source, by_kind = {}, {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        k = rec_ledger.kind_of(r["subject_key"])
        by_kind[k] = by_kind.get(k, 0) + 1
    return {"ok": True, "days": days, "restaurant_id": restaurant_id, "total": len(rows),
            "lookback_days": rec_ledger.MISSED_LOOKBACK_DAYS,
            "by_source": sorted(({"source": s, "n": n} for s, n in by_source.items()), key=lambda x: -x["n"]),
            "by_kind": sorted(({"kind": s, "n": n, "mapped": s in rec_ledger.PROBLEM_COVERAGE}
                               for s, n in by_kind.items()), key=lambda x: -x["n"]),
            "rows": rows[:int(limit)]}
