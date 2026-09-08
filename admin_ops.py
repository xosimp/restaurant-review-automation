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
import os
import re
from datetime import datetime, timedelta

from models import get_conn, get_restaurant

# The published tiers (pricing.html / the one-sheet) by module count, for MRR.
MONTHLY_BY_MODULES = {1: 349, 2: 649, 3: 899, 4: 1199}

INTEGRATIONS = ("google_business", "toast", "square", "clover", "instagram", "webhook")


# ── time helpers ─────────────────────────────────────────────────────────────

# Two stamp conventions live in this database: sqlite's datetime('now')
# writes UTC with a space ("2026-09-06 23:17:24"); Python's isoformat()
# writes local time with a T ("2026-09-06T18:17:24"). Everything here is
# normalised to naive LOCAL time so ages and "since" are right for both.
_UTC_OFFSET = datetime.now() - datetime.utcnow()


def _parse(ts):
    if not ts:
        return None
    raw = str(ts).strip()
    s = raw.replace("Z", "")
    d = None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if d is None:
        try:
            d = datetime.fromisoformat(s)
            if d.tzinfo:
                return (d - d.utcoffset()).replace(tzinfo=None) + _UTC_OFFSET
        except Exception:
            return None
    if raw.endswith("Z") or ("T" not in raw and " " in raw):
        d = d + _UTC_OFFSET
    return d


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


def _iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ── raw loads ────────────────────────────────────────────────────────────────

def _rows(conn, sql, args=()):
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except Exception:
        return []


def _one(conn, sql, args=()):
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
    day = _iso(now - timedelta(days=1))
    week = _iso(now - timedelta(days=7))
    month = _iso(now - timedelta(days=30))
    today = now.strftime("%Y-%m-%d")

    rests = _rows(conn, "SELECT * FROM restaurants ORDER BY id")
    users = _rows(conn, "SELECT id, restaurant_id, username, email, role, is_admin, is_active, created_at, last_login FROM users")
    by_rid = {}
    for u in users:
        by_rid.setdefault(u["restaurant_id"], []).append(u)

    def per_rid(sql, args=(), key="restaurant_id"):
        out = {}
        for r in _rows(conn, sql, args):
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
                      (_iso(now - timedelta(days=14)), week))
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
    ingredients = per_rid("SELECT restaurant_id, COUNT(*) AS n FROM ingredients GROUP BY restaurant_id")
    marketing = per_rid("SELECT restaurant_id, COUNT(*) AS pieces, MAX(created_at) AS last_at FROM marketing_content_log GROUP BY restaurant_id")
    sched_posts = per_rid("SELECT restaurant_id, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending FROM marketing_scheduled_posts GROUP BY restaurant_id")
    schedules = per_rid("SELECT restaurant_id, MAX(generated_at) AS last_at, COUNT(*) AS n FROM schedule_history GROUP BY restaurant_id")
    logins = per_rid("SELECT restaurant_id, MAX(created_at) AS last_at, COUNT(*) AS n FROM login_history GROUP BY restaurant_id")
    sessions = per_rid("""SELECT u.restaurant_id AS restaurant_id, COUNT(*) AS n FROM sessions s JOIN users u ON u.id=s.user_id
                          WHERE s.expires_at > datetime('now') GROUP BY u.restaurant_id""")
    guests = per_rid("SELECT restaurant_id, COUNT(*) AS n FROM guest_contacts WHERE consent=1 AND unsubscribed=0 GROUP BY restaurant_id")
    job_failures = _rows(conn, "SELECT id, job, error, context, created_at FROM job_failures WHERE created_at >= ? ORDER BY id DESC", (week,))
    resolved = {r["key"]: r for r in _rows(conn, "SELECT key, resolved_at, note FROM admin_issue_resolutions")}
    conn.close()

    return dict(now=now, rests=rests, users=by_rid, reviews=reviews, ai_month=ai_month, ai_today=ai_today,
                ai_prev=ai_prev, ai_week=ai_week, ai_failed_week=ai_failed_week, emails=emails, pushes=pushes, tokens=tokens, alerts=alerts,
                webhooks=webhooks, client_data=client_data, ingredients=ingredients, marketing=marketing,
                sched_posts=sched_posts, schedules=schedules, logins=logins, sessions=sessions, guests=guests,
                job_failures=job_failures, resolved=resolved)


# ── the per-location health record ──────────────────────────────────────────

def _integrations_for(r, hooks):
    """Every external connection, in one shape: state, last success, error."""
    ig_exp = _parse(r.get("ig_token_expires"))
    ig_days_left = None if not ig_exp else (ig_exp - datetime.now()).days
    out = [
        {"key": "google_business", "label": "Google Business",
         "connected": bool(r.get("gmb_refresh_token")),
         "configured": bool(r.get("google_place_id") or r.get("gmb_location_id")),
         "last_success": r.get("last_fetched_at"), "error": None,
         "auth": "oauth" if r.get("gmb_refresh_token") else ("place id only" if r.get("google_place_id") else "none")},
        {"key": "toast", "label": "Toast POS",
         "connected": bool(r.get("toast_restaurant_guid") and r.get("toast_client_id")) and not r.get("toast_sync_error"),
         "configured": bool(r.get("toast_restaurant_guid")),
         "last_success": r.get("toast_last_synced"), "error": r.get("toast_sync_error") or None,
         "auth": "credentials" if r.get("toast_client_id") else "none"},
        {"key": "square", "label": "Square POS",
         "connected": bool(r.get("square_access_token")) and not r.get("square_sync_error"),
         "configured": bool(r.get("square_access_token")),
         "last_success": r.get("square_last_synced"), "error": r.get("square_sync_error") or None,
         "auth": "token" if r.get("square_access_token") else "none"},
        {"key": "clover", "label": "Clover POS",
         "connected": bool(r.get("clover_api_token")) and not r.get("clover_sync_error"),
         "configured": bool(r.get("clover_api_token")),
         "last_success": r.get("clover_last_synced"), "error": r.get("clover_sync_error") or None,
         "auth": "token" if r.get("clover_api_token") else "none"},
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
    pos = bool(r.get("toast_restaurant_guid") or r.get("square_access_token") or r.get("clover_api_token"))
    mods = [
        {"key": "reviews", "label": "Reviews", "enabled": bool(r.get("module_reviews")),
         "configured": bool(r.get("gmb_refresh_token") or r.get("google_place_id") or r.get("yelp_business_id")),
         "receiving": bool(rv.get("total")), "last_data": rv.get("last_review_at") or r.get("last_fetched_at")},
        {"key": "labor", "label": "Labor", "enabled": bool(r.get("module_labor")),
         "configured": pos or bool(cd.get("has_shifts")),
         "receiving": bool(sc.get("n")) or pos or bool(cd.get("has_shifts")),
         "last_data": r.get("toast_last_synced") or r.get("square_last_synced") or r.get("clover_last_synced") or cd.get("updated_at")},
        {"key": "inventory", "label": "Food Cost", "enabled": bool(r.get("module_inventory")),
         "configured": bool(ing.get("n")) or bool(cd.get("has_inventory")),
         "receiving": bool(r.get("inventory_updated_at")) or bool(cd.get("has_inventory")),
         "last_data": r.get("inventory_updated_at") or cd.get("updated_at")},
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
        "freshness": {"reviews": r.get("last_fetched_at"), "pos": r.get("toast_last_synced") or r.get("square_last_synced") or r.get("clover_last_synced"),
                      "inventory": r.get("inventory_updated_at"), "intel": r.get("competitor_updated_at")},
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
    }


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
            sev = "critical" if i["key"] in ("toast", "square", "clover", "google_business") else "warning"
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
    day = _iso(now - timedelta(days=1))
    ai_today = _one(conn, "SELECT COUNT(*) AS n, ROUND(COALESCE(SUM(cost_usd),0),2) AS cost FROM ai_usage WHERE created_at >= ?", (today,)) or {}
    emails_today = _one(conn, "SELECT SUM(CASE WHEN status='failed' THEN 0 ELSE 1 END) AS sent, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed FROM email_log WHERE sent_at >= ?", (today,)) or {}
    push_today = _one(conn, "SELECT SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS sent, SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failed FROM push_deliveries WHERE created_at >= ?", (today,)) or {}
    alerts_today = _one(conn, "SELECT COUNT(*) AS n FROM alert_log WHERE fired_at >= ?", (today,)) or {}
    jobs_failed_24h = _one(conn, "SELECT COUNT(*) AS n, COUNT(DISTINCT job) AS jobs FROM job_failures WHERE created_at >= ?", (day,)) or {}
    ai_failed_24h = _one(conn, "SELECT COUNT(*) AS n FROM ai_usage WHERE created_at >= ? AND COALESCE(status,'ok')='error'", (day,)) or {}
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
    return {"ok": True, "kpis": kpis, "issues": issues[:60], "activity": activity(limit=30, d=d)["events"],
            "brands": [{k: v for k, v in b.items() if k != "locations"} | {"location_ids": [l["id"] for l in b["locations"]]} for b in _brands(recs)],
            "generated_at": _iso(now)}


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
    month = _iso(d["now"] - timedelta(days=30))
    ai_by_action = _rows(conn, "SELECT action, model, COUNT(*) AS calls, ROUND(SUM(cost_usd),4) AS cost, SUM(input_tokens)+SUM(output_tokens) AS tokens, MAX(created_at) AS last_at FROM ai_usage WHERE restaurant_id=? AND created_at >= ? GROUP BY action, model ORDER BY cost DESC", (rid, month))
    ai_recent = _rows(conn, "SELECT action, model, input_tokens, output_tokens, cost_usd, created_at, COALESCE(status,'ok') AS status, error FROM ai_usage WHERE restaurant_id=? ORDER BY id DESC LIMIT 40", (rid,))
    ai_failed = _rows(conn, "SELECT action, model, error, created_at FROM ai_usage WHERE restaurant_id=? AND COALESCE(status,'ok')='error' ORDER BY id DESC LIMIT 20", (rid,))
    events = _rows(conn, "SELECT id, source, event_type, amount, summary, created_at FROM admin_events WHERE restaurant_id=? ORDER BY id DESC LIMIT 40", (rid,))
    ai_daily = _rows(conn, "SELECT substr(created_at,1,10) AS day, COUNT(*) AS calls, ROUND(SUM(cost_usd),4) AS cost FROM ai_usage WHERE restaurant_id=? AND created_at >= ? GROUP BY day ORDER BY day", (rid, month))
    emails = _rows(conn, "SELECT email_type, to_email, subject, sent_at, status, error FROM email_log WHERE restaurant_id=? ORDER BY id DESC LIMIT 60", (rid,))
    pushes = _rows(conn, "SELECT alert_type, status, ok, attempts, error, created_at FROM push_deliveries WHERE restaurant_id=? ORDER BY id DESC LIMIT 40", (rid,))
    devices = _rows(conn, "SELECT id, user_id, environment, created_at, last_success_at, consecutive_failures, disabled_reason FROM device_tokens WHERE restaurant_id=?", (rid,))
    alerts = _rows(conn, "SELECT alert_type, review_id, fired_at FROM alert_log WHERE restaurant_id=? ORDER BY id DESC LIMIT 40", (rid,))
    acts = _rows(conn, "SELECT event_type, event_data, created_at FROM activity_log WHERE restaurant_id=? ORDER BY id DESC LIMIT 60", (rid,))
    logins = _rows(conn, "SELECT event, ip_address, user_agent, device_type, created_at FROM login_history WHERE restaurant_id=? ORDER BY id DESC LIMIT 30", (rid,))
    sessions = _rows(conn, "SELECT s.created_at, s.last_active, s.device_type, s.ip_address, u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE u.restaurant_id=? AND s.expires_at > datetime('now') ORDER BY s.last_active DESC", (rid,))
    jobs = _rows(conn, "SELECT job, error, context, created_at FROM job_failures WHERE context LIKE ? OR context LIKE ? ORDER BY id DESC LIMIT 40", (f"%rid={rid}%", f"%{rec['name']}%"))
    runs = _rows(conn, "SELECT job, started_at, finished_at, duration_ms, ok, error FROM job_runs WHERE context LIKE ? ORDER BY id DESC LIMIT 20", (f"%rid={rid}%",))
    posts = _rows(conn, "SELECT platform, content_type, topic, scheduled_for, status, error, attempts, posted_at FROM marketing_scheduled_posts WHERE restaurant_id=? ORDER BY id DESC LIMIT 20", (rid,))
    hooks = _rows(conn, "SELECT event_type, status, ok, attempts, error, created_at FROM webhook_deliveries WHERE restaurant_id=? ORDER BY id DESC LIMIT 20", (rid,))
    schedules = _rows(conn, "SELECT id, generated_at, week_start, week_end, hours_scheduled, hours_budget FROM schedule_history WHERE restaurant_id=? ORDER BY id DESC LIMIT 10", (rid,))
    notes = _rows(conn, "SELECT id, employee_name, notes, created_at FROM staff_notes WHERE restaurant_id=? ORDER BY id DESC", (rid,))
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
    since = _iso(now - timedelta(days=days))
    today = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m-01")
    totals = _one(conn, "SELECT COUNT(*) AS calls, ROUND(COALESCE(SUM(cost_usd),0),2) AS cost, COALESCE(SUM(input_tokens),0) AS tin, COALESCE(SUM(output_tokens),0) AS tout FROM ai_usage WHERE created_at >= ?", (since,)) or {}
    t_today = _one(conn, "SELECT COUNT(*) AS calls, ROUND(COALESCE(SUM(cost_usd),0),2) AS cost FROM ai_usage WHERE created_at >= ?", (today,)) or {}
    t_month = _one(conn, "SELECT COUNT(*) AS calls, ROUND(COALESCE(SUM(cost_usd),0),2) AS cost FROM ai_usage WHERE created_at >= ?", (month,)) or {}
    by_action = _rows(conn, "SELECT action, model, COUNT(*) AS calls, ROUND(SUM(cost_usd),4) AS cost, ROUND(AVG(input_tokens+output_tokens)) AS avg_tokens FROM ai_usage WHERE created_at >= ? GROUP BY action, model ORDER BY cost DESC", (since,))
    by_client = _rows(conn, "SELECT a.restaurant_id, r.name, r.location_group, COUNT(*) AS calls, ROUND(SUM(a.cost_usd),4) AS cost FROM ai_usage a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE a.created_at >= ? GROUP BY a.restaurant_id ORDER BY cost DESC", (since,))
    daily = _rows(conn, "SELECT substr(created_at,1,10) AS day, COUNT(*) AS calls, ROUND(SUM(cost_usd),4) AS cost FROM ai_usage WHERE created_at >= ? GROUP BY day ORDER BY day", (since,))
    by_provider = _rows(conn, "SELECT CASE WHEN model LIKE '%perplexity%' OR model LIKE 'sonar%' OR action LIKE '%visibility%' THEN 'Perplexity' ELSE 'Claude' END AS provider, COUNT(*) AS calls, ROUND(SUM(cost_usd),4) AS cost FROM ai_usage WHERE created_at >= ? GROUP BY provider", (since,))
    failures = _rows(conn, "SELECT action AS job, model, COUNT(*) AS n, MAX(created_at) AS last_at, MAX(error) AS sample FROM ai_usage WHERE created_at >= ? AND COALESCE(status,'ok')='error' GROUP BY action, model ORDER BY n DESC", (since,))
    failed_total = _one(conn, "SELECT COUNT(*) AS n, SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS n_24h FROM ai_usage WHERE created_at >= ? AND COALESCE(status,'ok')='error'", (_iso(now - timedelta(days=1)), since)) or {}
    recent = _rows(conn, "SELECT a.id, a.restaurant_id, r.name, a.action, a.model, a.input_tokens, a.output_tokens, a.cost_usd, a.created_at, COALESCE(a.status,'ok') AS status, a.error FROM ai_usage a LEFT JOIN restaurants r ON r.id=a.restaurant_id ORDER BY a.id DESC LIMIT 80")
    recent_failed = _rows(conn, "SELECT a.id, a.restaurant_id, r.name, a.action, a.model, a.created_at, a.error FROM ai_usage a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE COALESCE(a.status,'ok')='error' ORDER BY a.id DESC LIMIT 40")
    # Anomalies: a client whose last 7 days is >4x its previous 7, or any
    # action that ran >200 times in a day for one restaurant (a loop).
    week = _iso(now - timedelta(days=7)); prev = _iso(now - timedelta(days=14))
    wk = {r["restaurant_id"]: r["n"] for r in _rows(conn, "SELECT restaurant_id, COUNT(*) AS n FROM ai_usage WHERE created_at >= ? GROUP BY restaurant_id", (week,))}
    pv = {r["restaurant_id"]: r["n"] for r in _rows(conn, "SELECT restaurant_id, COUNT(*) AS n FROM ai_usage WHERE created_at >= ? AND created_at < ? GROUP BY restaurant_id", (prev, week))}
    loops = _rows(conn, "SELECT a.restaurant_id, r.name, a.action, substr(a.created_at,1,10) AS day, COUNT(*) AS n FROM ai_usage a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE a.created_at >= ? GROUP BY a.restaurant_id, a.action, day HAVING n >= 200 ORDER BY n DESC", (since,))
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
    now = datetime.now(); week = _iso(now - timedelta(days=7)); today = now.strftime("%Y-%m-%d")
    rows = _rows(conn, "SELECT e.id, e.restaurant_id, r.name AS restaurant, r.location_group AS brand, e.email_type, e.to_email, e.subject, e.sent_at, e.status, e.error FROM email_log e LEFT JOIN restaurants r ON r.id=e.restaurant_id ORDER BY e.id DESC LIMIT ?", (limit,))
    by_type = _rows(conn, "SELECT email_type, COUNT(*) AS n, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed FROM email_log WHERE sent_at >= ? GROUP BY email_type ORDER BY n DESC", (week,))
    daily = _rows(conn, "SELECT substr(sent_at,1,10) AS day, COUNT(*) AS n, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed FROM email_log WHERE sent_at >= ? GROUP BY day ORDER BY day", (_iso(now - timedelta(days=30)),))
    storms = _rows(conn, "SELECT to_email, COUNT(*) AS n FROM email_log WHERE sent_at >= ? GROUP BY to_email HAVING n >= 8 ORDER BY n DESC", (today,))
    suppressed = _rows(conn, "SELECT * FROM email_suppressions ORDER BY rowid DESC LIMIT 50")
    totals = _one(conn, "SELECT COUNT(*) AS n, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed FROM email_log WHERE sent_at >= ?", (today,)) or {}
    conn.close()
    return {"ok": True, "rows": rows, "by_type": by_type, "daily": daily, "storms": storms, "suppressed": suppressed, "today": totals}


def notifications(limit=200):
    conn = get_conn()
    now = datetime.now(); today = now.strftime("%Y-%m-%d"); week = _iso(now - timedelta(days=7))
    pushes = _rows(conn, "SELECT p.id, p.restaurant_id, r.name AS restaurant, p.device_token_id, d.user_id, u.username, p.alert_type, p.status, p.ok, p.attempts, p.error, p.created_at FROM push_deliveries p LEFT JOIN restaurants r ON r.id=p.restaurant_id LEFT JOIN device_tokens d ON d.id=p.device_token_id LEFT JOIN users u ON u.id=d.user_id ORDER BY p.id DESC LIMIT ?", (limit,))
    devices = _rows(conn, "SELECT d.id, d.restaurant_id, r.name AS restaurant, u.username, d.environment, d.created_at, d.last_success_at, d.consecutive_failures, d.disabled_reason FROM device_tokens d LEFT JOIN restaurants r ON r.id=d.restaurant_id LEFT JOIN users u ON u.id=d.user_id ORDER BY d.id DESC")
    alerts = _rows(conn, "SELECT a.id, a.restaurant_id, r.name AS restaurant, a.alert_type, a.review_id, a.fired_at FROM alert_log a LEFT JOIN restaurants r ON r.id=a.restaurant_id ORDER BY a.id DESC LIMIT ?", (limit,))
    by_type = _rows(conn, "SELECT alert_type, COUNT(*) AS n FROM alert_log WHERE fired_at >= ? GROUP BY alert_type ORDER BY n DESC", (week,))
    storms = _rows(conn, "SELECT a.restaurant_id, r.name AS restaurant, COALESCE(r.alert_max_per_day,0) AS cap, COUNT(*) AS n FROM alert_log a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE a.fired_at >= ? GROUP BY a.restaurant_id HAVING n >= 10 ORDER BY n DESC", (today,))
    caps = _rows(conn, "SELECT id AS restaurant_id, name AS restaurant, alert_max_per_day AS cap FROM restaurants WHERE COALESCE(alert_max_per_day,0) > 0 ORDER BY name")
    scheduled = _rows(conn, "SELECT p.id, p.restaurant_id, r.name AS restaurant, p.platform, p.content_type, p.topic, p.scheduled_for, p.status, p.error, p.attempts FROM marketing_scheduled_posts p LEFT JOIN restaurants r ON r.id=p.restaurant_id ORDER BY p.scheduled_for DESC LIMIT 60")
    today_push = _one(conn, "SELECT SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS sent, SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failed FROM push_deliveries WHERE created_at >= ?", (today,)) or {}
    conn.close()
    return {"ok": True, "pushes": pushes, "devices": devices, "alerts": alerts, "by_type": by_type, "storms": storms, "caps": caps, "scheduled_posts": scheduled, "today_push": today_push}


def billing():
    recs, _ = _records()
    rows = []
    live = {}
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if key:
        try:
            import stripe as _stripe
            _stripe.api_key = key
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
    "ops_failure_digest":      {"cadence": "8am daily", "what": "Email Will the failure digest", "target": ("ops", "send_failure_digest"), "sends": True},
    "backup_db":               {"cadence": "2am nightly", "what": "Back the SQLite database up", "target": ("scheduler", "backup_db")},
}


def run_job_now(name, actor):
    """Run one scheduled job right now, on a background thread, recorded in
    job_runs exactly like a scheduled run — context says who asked."""
    import importlib, threading
    spec = RUNNABLE_JOBS.get(name)
    if not spec:
        return {"ok": False, "error": "Unknown job"}
    conn = get_conn()
    running = _one(conn, "SELECT id, started_at FROM job_runs WHERE job=? AND finished_at IS NULL AND started_at >= ? ORDER BY id DESC LIMIT 1",
                   (name, _iso(datetime.now() - timedelta(minutes=30))))
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
    now = datetime.now(); day = _iso(now - timedelta(days=1)); week = _iso(now - timedelta(days=7))
    failures = _rows(conn, "SELECT id, job, error, context, created_at FROM job_failures ORDER BY id DESC LIMIT 100")
    grouped = _rows(conn, "SELECT job, COUNT(*) AS n, MAX(created_at) AS last_at, MIN(created_at) AS first_at, MAX(error) AS sample FROM job_failures WHERE created_at >= ? GROUP BY job ORDER BY n DESC", (week,))
    runs = _rows(conn, "SELECT id, job, started_at, finished_at, duration_ms, ok, error, context FROM job_runs ORDER BY id DESC LIMIT 120")
    last_ok = _rows(conn, "SELECT job, MAX(finished_at) AS last_ok, ROUND(AVG(duration_ms)) AS avg_ms, COUNT(*) AS runs FROM job_runs WHERE ok=1 AND started_at >= ? GROUP BY job", (week,))
    stuck = _rows(conn, "SELECT id, job, started_at, context FROM job_runs WHERE finished_at IS NULL AND started_at < ? ORDER BY started_at", (_iso(now - timedelta(minutes=30)),))
    posts = _rows(conn, "SELECT p.id, r.name AS restaurant, p.platform, p.topic, p.scheduled_for, p.status, p.attempts, p.error FROM marketing_scheduled_posts p LEFT JOIN restaurants r ON r.id=p.restaurant_id WHERE p.status IN ('pending','failed') ORDER BY p.scheduled_for LIMIT 40")
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
    conn.execute("CREATE TABLE IF NOT EXISTS admin_issue_resolutions (key TEXT PRIMARY KEY, resolved_at TEXT DEFAULT (datetime('now')), note TEXT, actor TEXT)")
    conn.execute("INSERT OR REPLACE INTO admin_issue_resolutions (key, resolved_at, note, actor) VALUES (?, datetime('now'), ?, ?)", (key, note, actor))
    conn.commit(); conn.close()
    return {"ok": True}


def unresolve_issue(key):
    conn = get_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS admin_issue_resolutions (key TEXT PRIMARY KEY, resolved_at TEXT DEFAULT (datetime('now')), note TEXT, actor TEXT)")
    conn.execute("DELETE FROM admin_issue_resolutions WHERE key=?", (key,))
    conn.commit(); conn.close()
    return {"ok": True}


def resolved_issues():
    conn = get_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS admin_issue_resolutions (key TEXT PRIMARY KEY, resolved_at TEXT DEFAULT (datetime('now')), note TEXT, actor TEXT)")
    rows = _rows(conn, "SELECT key, resolved_at, note, actor FROM admin_issue_resolutions ORDER BY resolved_at DESC LIMIT 100")
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
    now = datetime.now(); since = _iso(now - timedelta(days=14))
    ev = []
    for a in _rows(conn, "SELECT a.restaurant_id, r.name, a.event_type, a.event_data, a.created_at FROM activity_log a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE a.created_at >= ? ORDER BY a.id DESC LIMIT 120", (since,)):
        label = _ACTIVITY_LABELS.get(a["event_type"], a["event_type"].replace("_", " ").capitalize() if a["event_type"] else None)
        if label is None:
            continue
        ev.append({"at": a["created_at"], "restaurant_id": a["restaurant_id"], "restaurant": a["name"], "kind": "account", "label": label, "tone": "neutral"})
    for l in _rows(conn, "SELECT l.restaurant_id, r.name, l.device_type, l.created_at FROM login_history l LEFT JOIN restaurants r ON r.id=l.restaurant_id WHERE l.created_at >= ? ORDER BY l.id DESC LIMIT 60", (since,)):
        ev.append({"at": l["created_at"], "restaurant_id": l["restaurant_id"], "restaurant": l["name"], "kind": "login", "label": f"Signed in ({l['device_type'] or 'web'})", "tone": "neutral"})
    for r in _rows(conn, "SELECT id, name, created_at FROM restaurants WHERE created_at >= ? ORDER BY id DESC", (since,)):
        ev.append({"at": r["created_at"], "restaurant_id": r["id"], "restaurant": r["name"], "kind": "signup", "label": "Restaurant created", "tone": "good"})
    for e in _rows(conn, "SELECT e.restaurant_id, r.name, e.email_type, e.sent_at, e.error FROM email_log e LEFT JOIN restaurants r ON r.id=e.restaurant_id WHERE e.status='failed' AND e.sent_at >= ? ORDER BY e.id DESC LIMIT 40", (since,)):
        ev.append({"at": e["sent_at"], "restaurant_id": e["restaurant_id"], "restaurant": e["name"], "kind": "email", "label": f"Email failed · {e['email_type']}", "tone": "bad", "detail": e["error"]})
    for p in _rows(conn, "SELECT p.restaurant_id, r.name, p.alert_type, p.created_at, p.error FROM push_deliveries p LEFT JOIN restaurants r ON r.id=p.restaurant_id WHERE p.ok=0 AND p.created_at >= ? ORDER BY p.id DESC LIMIT 40", (since,)):
        ev.append({"at": p["created_at"], "restaurant_id": p["restaurant_id"], "restaurant": p["name"], "kind": "push", "label": f"Push failed · {p['alert_type']}", "tone": "bad", "detail": p["error"]})
    for j in _rows(conn, "SELECT job, error, created_at FROM job_failures WHERE created_at >= ? ORDER BY id DESC LIMIT 40", (since,)):
        ev.append({"at": j["created_at"], "restaurant_id": None, "restaurant": "Platform", "kind": "job", "label": f"Job failed · {j['job']}", "tone": "bad", "detail": (j["error"] or "")[:140]})
    for e in _rows(conn, "SELECT e.restaurant_id, r.name, e.source, e.event_type, e.summary, e.created_at FROM admin_events e LEFT JOIN restaurants r ON r.id=e.restaurant_id WHERE e.created_at >= ? ORDER BY e.id DESC LIMIT 40", (since,)):
        bad = any(x in e["event_type"] for x in ("failed", "deleted", "canceled", "past_due"))
        ev.append({"at": e["created_at"], "restaurant_id": e["restaurant_id"], "restaurant": e["name"] or "Unmatched customer", "kind": e["source"],
                   "label": f"{e['source'].capitalize()} · {e['summary'] or e['event_type']}", "tone": "bad" if bad else ("good" if e["event_type"] in ("invoice.paid", "contract.signed") else "neutral")})
    for a in _rows(conn, "SELECT a.restaurant_id, r.name, a.alert_type, a.fired_at FROM alert_log a LEFT JOIN restaurants r ON r.id=a.restaurant_id WHERE a.fired_at >= ? AND a.alert_type IN ('1star','health','labor_over') ORDER BY a.id DESC LIMIT 30", (since,)):
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
    for r in _rows(conn, "SELECT id, name, location_group, location_name, neighborhood, owner_email, owner_name, stripe_customer_id FROM restaurants WHERE name LIKE ? OR location_group LIKE ? OR location_name LIKE ? OR neighborhood LIKE ? OR owner_email LIKE ? OR owner_name LIKE ? OR stripe_customer_id LIKE ? OR CAST(id AS TEXT) = ? LIMIT 20", (like, like, like, like, like, like, like, q)):
        res.append({"type": "location", "id": r["id"], "title": r["name"], "sub": " · ".join(x for x in [r["location_group"], r["location_name"], r["neighborhood"]] if x) or r["owner_email"]})
    for u in _rows(conn, "SELECT u.id, u.username, u.email, u.role, u.restaurant_id, r.name FROM users u LEFT JOIN restaurants r ON r.id=u.restaurant_id WHERE u.username LIKE ? OR u.email LIKE ? OR CAST(u.id AS TEXT) = ? LIMIT 20", (like, like, q)):
        res.append({"type": "owner" if u["role"] in ("client", "owner") else "login", "id": u["restaurant_id"], "user_id": u["id"], "title": u["username"], "sub": f"{u['email']} · {u['name'] or 'no restaurant'} · {u['role']}"})
    for g in _rows(conn, "SELECT location_group AS g, COUNT(*) AS n FROM restaurants WHERE location_group LIKE ? GROUP BY location_group LIMIT 10", (like,)):
        res.append({"type": "brand", "id": None, "title": g["g"], "sub": f"{g['n']} locations"})
    conn.close()
    return {"ok": True, "results": res[:40]}
