"""account_health.py — Account health and the security checkup, scored once
on the server for both clients (iOS parity audit 10/7/26, #89 and the
"Security checkup" matrix row).

Both rules used to live in the clients: the web's acctHealthRefresh scored
six account-health items in JavaScript from data attributes the template
stamped, and its renderCheckup scored six security items; the app scored a
different seven (its own weights, "good" passwords counted, stale sessions
by age). The same account read differently on the two. Now:

  security_checkup(user)  the six shared security items, their points and
                          the score — the web's rules. The app adds its own
                          device-lock item beside them (a setting of that
                          phone, not of the account), outside the score.
  payload(user)           the six account-health items (profile, people,
                          integrations, notifications, security,
                          subscription), the score and ring tone, the one
                          sentence and the one fix, the modules on the plan,
                          and the measured-value line — `delivered` only,
                          never an opportunity, surfaced or avoided figure
                          (CLAUDE.md "Value delivered").

Reads only: nothing here writes, and no provider is called (the
subscription item reads the stored billing_status, not Stripe).
Routes: GET /api/account/health, /mobile/api/account/health;
GET /api/account/security-summary, /mobile/api/account/security-summary.
"""
from __future__ import annotations

import models as _models_mod


# ── security checkup ─────────────────────────────────────────────────────────

# key, title, points — the web's weights; they sum to 100.
CHECKUP_ITEMS = (
    ("two_fa", "Two-factor authentication", 30),
    ("password", "Password strength", 15),
    ("backup_codes", "Backup codes on hand", 15),
    ("login_notify", "Sign-in notifications", 10),
    ("recovery_email", "Recovery email", 15),
    ("devices", "No stale devices", 15),
)
# More trusted devices than this reads as stale (the web's rule).
MAX_TRUSTED_DEVICES = 3


def security_inputs(user, restaurant=None) -> dict:
    """The numbers the checkup is scored from, for this login."""
    from models import count_unused_backup_codes, get_restaurant
    from auth import get_trusted_devices, get_sessions_for_user
    rid = user["restaurant_id"]
    r = restaurant or get_restaurant(rid)
    try:
        sessions = len(get_sessions_for_user(user["id"]))
    except Exception:
        sessions = 1
    try:
        trusted = len(get_trusted_devices(rid))
    except Exception:
        trusted = 0
    try:
        codes = count_unused_backup_codes(rid)
    except Exception:
        codes = 0
    return {
        "two_fa_enabled": bool(r and r.two_fa_enabled),
        "two_fa_method": (getattr(r, "two_fa_method", None) or "email") if r else "email",
        "backup_codes_remaining": codes,
        "trusted_devices": trusted,
        "login_notify": bool(r and getattr(r, "login_notify", 0)),
        "recovery_email": user.get("recovery_email"),
        "password_strength": user.get("password_strength"),
        "password_changed_at": user.get("password_changed_at"),
        "active_sessions": sessions,
    }


def security_checkup(user, inputs=None, restaurant=None) -> dict:
    """{score, max, items: [{key, title, points, earned, detail, fix_label}]}
    — one rule for the web's Security card and the app's checkup."""
    d = inputs or security_inputs(user, restaurant)
    two_fa = d["two_fa_enabled"]
    strength = (d.get("password_strength") or "").lower()
    codes = int(d.get("backup_codes_remaining") or 0)
    trusted = int(d.get("trusted_devices") or 0)
    sessions = int(d.get("active_sessions") or 1)
    facts = {
        "two_fa": (two_fa,
                   ("On · " + ("text" if d.get("two_fa_method") == "sms" else "email")) if two_fa
                   else "Off — a password alone gets in", "Turn on"),
        "password": (strength == "strong",
                     f"Rated {strength}" if strength else "Unrated — set a new one to score it", "Change"),
        "backup_codes": (two_fa and codes > 0,
                         f"{codes} unused" if two_fa else "Needs two-factor first", "Regenerate"),
        "login_notify": (bool(d.get("login_notify")),
                         "On — each person hears about their own sign-ins" if d.get("login_notify")
                         else "Off — you won't hear about new sign-ins", "Turn on"),
        "recovery_email": (bool(d.get("recovery_email")),
                           d.get("recovery_email") or "None — losing your sign-in email means losing the account",
                           "Add"),
        "devices": (trusted <= MAX_TRUSTED_DEVICES,
                    f"{trusted} trusted, {sessions} signed in", "Review"),
    }
    items, score = [], 0
    for key, title, points in CHECKUP_ITEMS:
        earned, detail, fix = facts[key]
        earned = bool(earned)
        if earned:
            score += points
        items.append({"key": key, "title": title, "points": points, "earned": earned,
                      "detail": detail, "fix_label": None if earned else fix})
    return {"score": score, "max": sum(p for _k, _t, p in CHECKUP_ITEMS), "items": items}


# ── account health ───────────────────────────────────────────────────────────

# Label, and the one fix per item: its button label. The order is the order
# a fix is offered in (the worst item first within it).
HEALTH_ITEMS = (
    ("integrations", "Integrations", "Fix your data connection"),
    ("notifications", "Notifications", "Set up alerts"),
    ("subscription", "Subscription", "Update your payment method"),
    ("security", "Security", "Turn on two-factor"),
    ("profile", "Restaurant profile", "Finish your profile"),
    ("people", "People", "Invite a teammate"),
)
# The order the web draws the items in.
DISPLAY_ORDER = ("profile", "people", "integrations", "notifications", "security", "subscription")

_ALERT_SWITCHES = ("alert_1star", "alert_2star", "alert_health", "alert_neg_spike", "alert_negative_trend",
                   "alert_no_response", "alert_5star", "alert_rating_threshold", "alert_labor_over",
                   "alert_any_review")

# The modules a plan can carry, in the Feature access card's words.
FEATURES = (
    ("reviews", "Reviews", "AI-drafted replies, urgent flags, response tracking"),
    ("labor", "Labor", "Schedules, labor %, operational scores"),
    ("food_cost", "Food Cost", "Ingredient spend, waste, menu margins"),
    ("marketing", "Marketing", "Guest outreach, social posts, campaigns"),
    ("intel", "Intel", "Competitor tracking & AI visibility — included with the full plan"),
)


def connections(restaurant) -> dict:
    """Every connection the account can hold, connected or not — Google
    Business, Instagram & Facebook, each POS, website analytics — and whether
    a connected POS is failing. The one count both clients show."""
    r = restaurant
    pos = {}
    pos_error = False
    for name, id_attr in (("toast", "toast_restaurant_guid"), ("square", "square_location_id"),
                          ("clover", "clover_merchant_id"), ("rpower", "rpower_store_mid")):
        has_id = bool(getattr(r, id_attr, None))
        err = bool(getattr(r, f"{name}_sync_error", None))
        pos[name] = has_id and not err
        pos_error = pos_error or (has_id and err)
    try:
        import web_analytics as _wa
        web = bool(_wa.clean_property_id(getattr(r, "ga4_property_id", None))
                   or _wa.clean_site_url(getattr(r, "gsc_site_url", None)))
    except Exception:
        web = False
    rows = {"google_business": bool(getattr(r, "gmb_refresh_token", None)),
            "instagram": bool(getattr(r, "ig_token", None) or getattr(r, "fb_page_token", None)),
            **pos, "web_analytics": web}
    return {"rows": rows, "connected": sum(1 for v in rows.values() if v), "total": len(rows),
            "pos_error": pos_error}


def _people_counts(rid):
    team = staff = None
    try:
        from auth import get_team_members
        team = len(get_team_members(rid))
    except Exception:
        pass
    try:
        from auth import get_memberships_for_restaurant
        staff = sum(1 for m in get_memberships_for_restaurant(rid, role="employee") if m.get("is_active", 1))
    except Exception:
        pass
    return team, staff


def _data_health_overall(user):
    try:
        import data_health
        d = data_health.payload_for(user)
        return (d or {}).get("overall") if (d or {}).get("ok") else None
    except Exception as e:
        print(f"[account_health] data health unavailable rid={user.get('restaurant_id')}: {e}")
        return None


def measured_value(user) -> dict:
    """The measured-value line: value_delivered.delivered only, net of what
    got worse, scoped to what this login may see. {line, net_monthly,
    evaluated, in_flight, measured}; `net_monthly` is None until something
    was measured — never 0 standing in for "nothing yet"."""
    rid = user["restaurant_id"]
    try:
        import value_delivered
        scope = value_delivered.viewer_scope(rid, user)
        try:
            import outcomes
            if not outcomes.metric_visible_to(user, "food_cost_pct"):
                scope["denied_modules"] = set(scope.get("denied_modules") or ()) | {"inventory"}
        except Exception:
            pass
        d = value_delivered.delivered(rid, scope=scope)
    except Exception as e:
        print(f"[account_health] measured value unavailable rid={rid}: {e}")
        return {"line": None, "net_monthly": None, "evaluated": None, "in_flight": None, "measured": False}
    worse = d.get("worsened") or {}
    n = int(d.get("evaluated") or 0)
    flight = int(d.get("in_flight") or 0)
    net = d.get("net_monthly")
    if net is None:
        net = d.get("monthly")
    if int(d.get("wins") or 0) > 0 or int(worse.get("count") or 0) > 0:
        # Changes were measured but no dollar figure came with them: "—",
        # never $0 standing in for a figure nobody has (re-audit 10/8/26, #16).
        amount = "—" if net is None else ("−" if net < 0 else "") + "$" + f"{abs(round(net)):,}"
        line = f"Measured, net: {amount}{'' if net is None else '/mo'} · {n} change{'' if n == 1 else 's'} measured"
        return {"line": line, "net_monthly": None if net is None else round(float(net), 2), "evaluated": n,
                "in_flight": flight, "measured": True}
    if flight > 0:
        line = f"Nothing measured yet — {flight} change{'' if flight == 1 else 's'} being measured"
    else:
        line = "Nothing measured yet"
    return {"line": line, "net_monthly": None, "evaluated": n, "in_flight": flight, "measured": False}


def features(restaurant) -> list:
    """The modules on this plan: [{key, label, detail, on}]."""
    r = restaurant
    on = {"reviews": bool(getattr(r, "module_reviews", 0)), "labor": bool(getattr(r, "module_labor", 0)),
          "food_cost": bool(getattr(r, "module_inventory", 0)),
          "marketing": bool(getattr(r, "module_marketing", 0)),
          "intel": bool(_models_mod.is_full_tier(r))}
    return [{"key": k, "label": label, "detail": detail, "on": on[k]} for k, label, detail in FEATURES]


# The subscription item as the app says it (re-audit 10/8/26, #15): the app
# points to no payment page (App Store Guideline 3.1.1), so it never tells an
# owner to update a card or a payment method — billing is under the service
# agreement. The fix still opens Account → Billing, read only.
IOS_BILLING_NOTE = "Billing is handled under your service agreement"
IOS_SUBSCRIPTION_FIX = "See your plan"


def payload(user, restaurant=None, include_value=True, surface="web") -> dict:
    """GET /api/account/health and its mobile twin (`surface="ios"`, which
    words the subscription item neutrally — IOS_BILLING_NOTE). See the
    module doc."""
    from permissions import is_principal
    from models import get_restaurant
    rid = user["restaurant_id"]
    r = restaurant or get_restaurant(rid)
    if not r:
        return {"ok": False, "error": "Restaurant not found"}
    principal = is_principal(user)
    items = {}
    pts, max_pts = 0.0, 6.0

    def put(key, state, sub, say=None):
        items[key] = {"state": state, "sub": sub, "say": say or sub}

    # Profile: the four details the AI writes with.
    missing = [label for attr, label in (("owner_name", "your name"), ("owner_phone", "a phone number"),
                                         ("timezone", "your timezone"),
                                         ("voice_notes", "your brand voice (how you sound)"))
               if not str(getattr(r, attr, None) or "").strip()]
    have = 4 - len(missing)
    if have >= 4:
        pts += 1
        put("profile", "ok", "Complete — contact, timezone and brand voice all set")
    elif have >= 2:
        pts += .5
        put("profile", "warn", "Missing " + " and ".join(missing) + " — the AI writes better with "
            + ("it" if len(missing) == 1 else "them"), "profile: " + ", ".join(missing) + " missing")
    else:
        put("profile", "bad", "Missing " + ", ".join(missing) + " — add your contact details and how you sound",
            "your profile is mostly empty")

    # People: other logins, and staff signed up to the staff app.
    team, staff = _people_counts(rid)
    if team is None and staff is None:
        put("people", "warn", "Couldn't read who's on the account just now")
    else:
        tm, st = (team or 0) > 1, (staff or 0) > 0
        if tm and st:
            pts += 1
            put("people", "ok", f"{team} logins · {staff} staff signed up")
        elif tm or st:
            pts += .6
            put("people", "warn",
                "Teammates in — no staff have signed up yet" if tm else f"{staff} staff signed up · only you use the dashboard",
                "no staff have signed up" if tm else "only you use the dashboard")
        else:
            put("people", "bad", "Just you — invite a teammate or post the staff join code",
                "only you are on the account")

    # Integrations: the data under the connections when Data health has a
    # figure, else the connections themselves.
    conn = connections(r)
    n_conn = conn["connected"]
    dh = _data_health_overall(user)
    if conn["pos_error"]:
        put("integrations", "bad", "A POS connection needs attention", "your POS stopped syncing")
    elif dh and dh.get("pct") is not None:
        dp = round(float(dh["pct"]))
        if dp >= 80:
            pts += 1
            put("integrations", "ok", f"Data {dp}% current · {n_conn} connected")
        elif dp >= 50:
            pts += .5
            put("integrations", "warn", f"Data {dp}% current — a source is aging", f"data is {dp}% current")
        else:
            put("integrations", "bad", f"Data {dp}% current" + (f" — {dh['reason']}" if dh.get("reason") else ""),
                f"data is only {dp}% current")
    elif n_conn >= 2:
        pts += 1
        put("integrations", "ok", f"{n_conn} connected — replies post, data flows")
    elif n_conn == 1:
        pts += .5
        put("integrations", "warn", "1 connected — add your POS for live labor and sales", "connect your POS")
    else:
        put("integrations", "bad", "Nothing connected — start with Google Business", "nothing is connected")

    # Notifications: an alert switched on, and someone to send it to.
    alerts_on = any(getattr(r, a, 0) for a in _ALERT_SWITCHES)
    digest_on = bool(getattr(r, "digest_enabled", 0))
    try:
        from notify import get_alert_contacts
        contacts = len(get_alert_contacts(rid) or [])
    except Exception:
        contacts = None
    if alerts_on and (contacts is None or contacts > 0):
        pts += 1
        put("notifications", "ok", "Alerts on" + (" · weekly digest on" if digest_on else ""))
    elif alerts_on or digest_on:
        pts += .5
        put("notifications", "warn",
            "Alerts on, but no contact to send them to" if alerts_on else "Digest on · no real-time alerts",
            "nobody receives alerts" if alerts_on else "no real-time alerts")
    else:
        put("notifications", "bad", "No alerts — you'd only find problems by logging in", "no alerts are on")

    # Security: the checkup's score.
    checkup = security_checkup(user, restaurant=r)
    sec = checkup["score"]
    if sec >= 70:
        pts += 1
        put("security", "ok", f"Checkup {sec} / 100")
    elif sec >= 40:
        pts += .5
        put("security", "warn", f"Checkup {sec} / 100 — turn on two-factor", "two-factor is off")
    else:
        put("security", "bad", f"Checkup {sec} / 100 — start with two-factor",
            f"the security checkup is {sec} / 100")

    # Subscription: the account holder's alone; anyone else's score leaves
    # it out rather than counting it unset. The stored state — no Stripe call.
    if not principal:
        max_pts -= 1
    else:
        status = (getattr(r, "billing_status", None) or "").lower()
        if status in ("active", "internal"):
            pts += 1
            put("subscription", "ok", "Active")
        elif status == "trial":
            pts += 1
            put("subscription", "ok", "Trial — first charge on day 31")
        elif status == "past_due":
            put("subscription", "bad",
                f"Payment past due — {IOS_BILLING_NOTE.lower()}" if surface == "ios"
                else "Payment past due — update your card", "the payment is past due")
        elif status == "paused":
            put("subscription", "warn", "Paused — billing and the briefs resume on their own", "the subscription is paused")
        else:
            put("subscription", "warn", "No subscription on file — Will can set one up", "no subscription is on file")

    score = round(pts / max_pts * 100) if max_pts else 0
    tone = "good" if score >= 90 else ("warn" if score >= 60 else "bad")
    labels = {k: label for k, label, _fix in HEALTH_ITEMS}
    fixes = {k: fix for k, _label, fix in HEALTH_ITEMS}
    if surface == "ios":
        fixes["subscription"] = IOS_SUBSCRIPTION_FIX
    order = [k for k, _l, _f in HEALTH_ITEMS]
    bad = [k for k in order if k in items and items[k]["state"] == "bad"]
    warn = [k for k in order if k in items and items[k]["state"] == "warn"]
    lead = bad or warn

    def _list(keys):
        return "; ".join(items[k]["say"] for k in keys[:3])

    if bad:
        say = {"lead": f"{len(bad)} thing{' needs' if len(bad) == 1 else 's need'} you:", "text": _list(bad) + "."}
    elif warn:
        say = {"lead": "Nearly set up.", "text": "Worth doing: " + _list(warn) + "."}
    else:
        say = {"lead": "Everything that matters is set up.", "text": ""}
    sub = ("Everything that matters is set up." if score >= 90 else
           "Nearly there — the items below would make Cavnar AI work harder for you." if score >= 60 else
           "A few things to set up before Cavnar AI can do its best work.")
    out = {
        "ok": True,
        "score": score,
        "tone": tone,
        "sub": sub,
        "say": say,
        "fix": {"key": lead[0], "label": fixes[lead[0]]} if lead else None,
        "items": [dict(items[k], key=k, label=labels[k]) for k in DISPLAY_ORDER if k in items],
        "connections": conn,
        "checkup": checkup,
        "features": features(r),
    }
    if include_value:
        out["measured"] = measured_value(user)
    return out
