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

import models as _models_gc
from models import get_restaurant


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports): a
    bound `from models import get_conn` read a different database from every
    other module whenever models.get_conn was redirected."""
    return _models_gc.get_conn() if db_path is None else _models_gc.get_conn(db_path)

_CACHE = {}
_CACHE_TTL = 60  # seconds — a refresh within a minute costs nothing
# A ceiling on live entries. Nothing ever removed an expired one, so the
# dict held one full Home payload per (restaurant, login) that had ever
# loaded it, for the life of the process (MOD-HOME-3).
_CACHE_MAX = 2000


def _cache_put(key, payload):
    """Store one payload, first dropping every expired entry and, past
    _CACHE_MAX, the oldest ones."""
    now = datetime.now(timezone.utc)
    _CACHE.pop(key, None)            # re-inserted below, so dict order stays oldest-first
    # Oldest first, so the sweep stops at the first entry that is both fresh
    # and inside the cap: amortised O(1) per write.
    while _CACHE:
        k = next(iter(_CACHE))
        at = _CACHE[k][0]
        if len(_CACHE) >= _CACHE_MAX or (now - at).total_seconds() >= _CACHE_TTL:
            _CACHE.pop(k, None)
        else:
            break
    _CACHE[key] = (now, payload)

_REVIEW_FETCH_HOURS_CT = (8, 12, 16, 20)  # scheduler.py's review_fetch cadence
_DISMISS_DAYS = 14  # a dismissed recommendation stays gone this long, then can resurface if still true

def _safe_readiness(rid, restaurant, r, rstats, labor_live, inv_live, mkt):
    try:
        return readiness(rid, restaurant, r, rstats, labor_live, inv_live, mkt)
    except Exception as e:
        print(f"[home] readiness unavailable: {e}")
        return None


def readiness(rid, restaurant, r, rstats, labor_live, inv_live, mkt):
    """What is connected, and what the product can therefore measure.

    admin_ops computes completeness and churn risk for Will; the owner had
    no equivalent, so a thin dashboard read as a thin product rather than a
    missing count. Per module: connected (data is flowing), what is
    measurable right now (metrics.trailing has a value), and — when it is
    not — the one action that would light it up. Never a score: a number
    here would be a grade on the owner, and the point is the next step.
    """
    import metrics
    mods = []
    def measurable(keys):
        out = []
        for k in keys:
            try:
                if metrics.trailing(rid, k)["value"] is not None:
                    out.append(metrics.describe(k)["label"])
            except Exception:
                pass
        return out
    if getattr(restaurant, "module_reviews", 0):
        connected = bool(r.get("gmb_refresh_token") or r.get("reviews_live"))
        mods.append({"key": "reviews", "label": "Reviews", "connected": connected,
                     "measurable": measurable(["avg_rating"]) if connected else [],
                     "next": None if connected else "Connect Google in Account",
                     "module": "account"})
    if getattr(restaurant, "module_labor", 0):
        mods.append({"key": "labor", "label": "Labor", "connected": bool(labor_live),
                     "measurable": measurable(["labor_pct", "sales"]) if labor_live else [],
                     "next": None if labor_live else "Sync your POS or upload a shift export",
                     "module": "labor"})
    if getattr(restaurant, "module_inventory", 0):
        mods.append({"key": "inventory", "label": "Food Cost", "connected": bool(inv_live),
                     "measurable": measurable(["food_cost_pct", "weekly_waste"]) if inv_live else [],
                     "next": None if inv_live else "Enter a first count",
                     "module": "inventory"})
    if getattr(restaurant, "module_marketing", 0):
        posting = bool((mkt or {}).get("last_at"))
        mods.append({"key": "marketing", "label": "Marketing", "connected": posting,
                     "measurable": [], "next": None if posting else "Generate a first post",
                     "module": "marketing"})
    on = [m for m in mods if m["connected"]]
    return {"modules": mods, "connected": len(on), "total": len(mods),
            "measurable": sorted({x for m in on for x in m["measurable"]}),
            "complete": all(m["connected"] for m in mods) if mods else False}


def _dismissed_keys(conn, rid):
    try:
        return {r["key"]: r for r in conn.execute("SELECT key, kind, dismissed_at, expires_at FROM home_dismissals WHERE restaurant_id=? AND expires_at > datetime('now')", (rid,)).fetchall()}
    except Exception:
        return {}


# How long each kind of dismissal holds. "Hide" comes back in a fortnight if
# still true — the retention audit counted a rejected recommendation
# resurfacing nine times in six months with no way to say why. "Done" and
# "not for us" are answers, and an answer should not be asked again.
_DISMISS_DAYS_BY_KIND = {"recommendation": _DISMISS_DAYS, "done": 3650, "not_for_us": 3650,
                         # "Not today" on a Needs-attention item: back tomorrow.
                         "snooze": 1}
# The ledger's name for each Home answer (rec_ledger.SILENCE_DAYS keys).
_LEDGER_KIND = {"recommendation": "hide", "done": "done", "not_for_us": "not_for_us"}


def times_hidden(conn, rid):
    """{key: how many times it has been hidden, ever}. A recommendation
    hidden twice is a question the product should ask, not re-ask."""
    try:
        return {r["key"]: int(r["n"] or 1) for r in conn.execute(
            "SELECT key, COALESCE(times, 1) AS n FROM home_dismissals WHERE restaurant_id=?", (rid,)).fetchall()}
    except Exception:
        return {}


def dismiss(rid, key, kind="recommendation", user_id=None, days=None, reason=None, title=None,
            surface="home", role=None, _card=True, reason_code=None):
    """Hide one recommendation for this restaurant. Keys carry their subject
    ("trim_day:Monday", "cut_waste:Salmon Fillet"), so a different day or
    item is a new recommendation and comes through.

    kind: recommendation (two weeks) | done | not_for_us (effectively for
    good) | snooze (back after `days`, default one). Returns the row's
    expiry so the client can say which.

    Every answer is also written to rec_ledger, which is what makes a "no"
    on Home a "no" in the brief, the weekly email, the digest and the queue
    (they all read rec_ledger.silenced_keys). `reason_code` is the owner's
    one-tap why (rec_ledger.REASON_CODES — the route refuses any other),
    stored on the ledger answer beside the free `reason`."""
    key = (key or "").strip()[:120]
    if not key:
        return {"ok": False, "error": "Missing key"}
    kind = kind if kind in _DISMISS_DAYS_BY_KIND else "recommendation"
    try:
        days = int(days) if days else _DISMISS_DAYS_BY_KIND[kind]
    except (TypeError, ValueError):
        days = _DISMISS_DAYS_BY_KIND[kind]
    days = max(1, min(days, 3650))
    conn = get_conn()
    prior = conn.execute("SELECT COALESCE(times, 1) AS n FROM home_dismissals WHERE restaurant_id=? AND key=?",
                         (rid, key)).fetchone()
    times = (int(prior["n"]) + 1) if prior else 1
    conn.execute("INSERT INTO home_dismissals (restaurant_id, key, kind, dismissed_by, expires_at, times) "
                 "VALUES (?,?,?,?, datetime('now', ?), ?)", (rid, key, kind, user_id, f"+{int(days)} days", times))
    conn.commit(); conn.close()
    invalidate(rid)
    reason = (reason or "").strip()[:200]
    try:
        import rec_ledger
        from datetime import datetime as _dtl
        code = reason_code if reason_code in rec_ledger.REASON_CODES else None
        if kind == "snooze":
            until = (_dtl.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            smeta = {"until": until, "days": days}
            if code:
                smeta["reason_code"] = code
            rec_ledger.record(rid, key, "snoozed", surface=surface, user_id=user_id, role=role,
                              meta=smeta, snooze_until=until)
        else:
            meta = {"kind": _LEDGER_KIND[kind]}
            if reason:
                meta["reason"] = reason
            if code:
                meta["reason_code"] = code
            rec_ledger.record(rid, key, "completed" if kind == "done" else "dismissed", surface=surface,
                              user_id=user_id, role=role, meta=meta, silence_days=days)
    except Exception as e:
        print(f"[home] dismissal not recorded in the ledger: {e}")
    # The critically-low card lists every unanswered item under the first
    # one's key; an answer to the card is an answer to each item on it.
    if _card and key.startswith("stock_low:"):
        quiet = _stock_quiet(rid)
        for k in _current_stock_keys(rid):
            if k != key and k not in quiet:
                dismiss(rid, k, kind=kind, user_id=user_id, days=days, surface=surface, role=role, _card=False,
                        reason_code=reason_code)
    # The why, remembered: "not doing X: the patio closes in October" is a
    # preference the assistant reads back in every future answer.
    if reason:
        try:
            from models import remember_ask_fact
            remember_ask_fact(rid, f"Not doing \u201c{(title or key)[:80]}\u201d: {reason}", kind="preference",
                              source="Home", user_id=user_id)
        except Exception:
            pass
    return {"ok": True, "key": key, "kind": kind, "days": int(days), "remembered": bool(reason)}


def undismiss(rid, key, _card=True):
    """"Use again": the Home row goes, and so does the ledger's silence, so
    the key can be said on every surface again. The critically-low card is
    answered for every item on it (dismiss), so "Use again" on the card
    restores every item it answered, not only the first."""
    if _card and (key or "").startswith("stock_low:"):
        try:
            quiet = _stock_quiet(rid)
            for k in _current_stock_keys(rid):
                if k != key and k in quiet:
                    undismiss(rid, k, _card=False)
        except Exception as e:
            print(f"[home] stock card restore incomplete: {e}")
    conn = get_conn()
    n = conn.execute("DELETE FROM home_dismissals WHERE restaurant_id=? AND key=?", (rid, (key or "").strip()[:120])).rowcount
    conn.commit(); conn.close()
    try:
        import rec_ledger
        if rec_ledger.unsilence(rid, (key or "").strip()[:160]):
            n = n or 1
    except Exception as e:
        print(f"[home] ledger unsilence failed: {e}")
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


def _iso_z(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def _age_days(v, now):
    d = _ts(v)
    return (now - d).total_seconds() / 86400.0 if d else None


def _one_dict(conn, sql, params=()):
    try:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def _rows_dict(conn, sql, params=()):
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def _plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _mdy(v, year=None):
    """Home shows every date as M/D/YY. Accepts 'YYYY-MM-DD…', 'M/D' (+ year), or passes through."""
    if not v:
        return v
    s = str(v)
    try:
        if len(s) >= 10 and s[4] == "-":
            d = datetime.strptime(s[:10], "%Y-%m-%d")
            return f"{d.month}/{d.day}/{d.year % 100:02d}"
        if "/" in s and s.count("/") == 1 and year:
            m, d = s.split("/")
            return f"{int(m)}/{int(d)}/{int(year) % 100:02d}"
    except Exception:
        pass
    return s


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
    from thresholds import REPLY_OWED_MAX_AGE_DAYS as _OWED_DAYS
    # Urgent means a reply still owed: the last REPLY_OWED_MAX_AGE_DAYS, as
    # on the location's own Home.
    urgent = _one_dict(conn, "SELECT COUNT(*) AS n FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL AND urgency='high' AND response_status NOT IN ('posted','approved','skipped') AND COALESCE(NULLIF(review_date,''), fetched_at) >= date('now', ?)", (rid, f"-{int(_OWED_DAYS)} days")) or {}
    awaiting = _one_dict(conn, "SELECT COUNT(*) AS n FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL AND response_status='drafted'", (rid,)) or {}
    rating = _one_dict(conn, "SELECT ROUND(AVG(rating),1) AS r, COUNT(*) AS n FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL AND COALESCE(NULLIF(review_date,''), fetched_at) >= date('now','-30 days')", (rid,)) or {}
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
        issues.append(("watch", f"{_plural(awaiting['n'], 'reply', 'replies')} waiting for approval"))
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
        _cache_put(key, payload)
    return payload, status


def invalidate(rid=None):
    """Called after an action that changes what Home should say (publishing
    replies, switching location) — the next load recomputes.

    Two key shapes live in _CACHE: a single-location brief keyed (rid, uid)
    and a group brief keyed ("group", base_rid, uid). Matching only k[0]
    against rid skipped every group entry, since k[0] there is the literal
    string "group" — so a group rollup kept serving pre-change numbers for
    the rest of its TTL. Both shapes are matched now.
    """
    if rid is None:
        _CACHE.clear()
        return
    # Every group entry goes, not just the ones keyed to this restaurant: a
    # group brief aggregates all of an owner's locations, so a change at any
    # one of them makes every group rollup that includes it wrong, and the
    # key only records the owner's BASE location, never the others.
    stale = [k for k in _CACHE if k[0] == rid or k[0] == "group"]
    for k in stale:
        _CACHE.pop(k, None)


import models as _models_listen
_models_listen.on_restaurant_change(lambda rid: invalidate(rid))   # a settings save (DATA-39)


def invalidate_user(user_id):
    """Drop one login's cached Homes — every location's and its group brief.
    What a location switch needs: it used to call invalidate() with no rid,
    which cleared every tenant's cache on the platform (MOD-HOME-3). Both
    key shapes end with the login's id."""
    for k in [k for k in _CACHE if k and k[-1] == user_id]:
        _CACHE.pop(k, None)


def _kind_score(rid, key, metric, restaurant):
    """The intelligence engine's read of this recommendation KIND here — used
    only to adjust a card's own confidence (intelligence.confidence
    .card_confidence), never printed beside it."""
    try:
        import intelligence
        return intelligence.confidence_for(rid, str(key).split(":", 1)[0], metric=metric, restaurant=restaurant)
    except Exception:
        return None


def _card_confidence(rid, key, metric, restaurant, band, reason):
    """ONE confidence per card, from the card's own evidence."""
    try:
        from intelligence.confidence import card_confidence
        return card_confidence(band, reason, _kind_score(rid, key, metric, restaurant))
    except Exception:
        return {"band": band, "label": f"{band.capitalize()} confidence", "reason": reason, "adjusted": None,
                "caution": None, "score": {"low": 0.3, "medium": 0.55, "high": 0.8}.get(band, 0.55)}


# ── what a card is worth doing first ────────────────────────────────────────
#
# Home listed recommendations in module order — reviews, labor, food,
# marketing, intel — so a $900/month food line sat under a "draft a post".
# The order is urgency x dollars x ease, with the card's own confidence as a
# discount: a thing due today that is worth money and easy to do leads.
_URGENCY = {"Today": 3.0, "This week": 2.0, "Next schedule": 1.5, "Next order": 1.5}
_EASE = {"low": 1.0, "medium": 0.8, "high": 0.6}
_CONF_WEIGHT = {"high": 1.0, "medium": 0.85, "low": 0.6}
# A card with no dollar figure ranks as if it carried this much: not zero
# (unmeasured is not worthless), not enough to beat a measured line alone.
_UNPRICED = 50.0


def rank_score(rec) -> float:
    dollars = rec.get("dollars_monthly")
    return round(_URGENCY.get(rec.get("timeframe"), 1.0)
                 * max(float(dollars or 0), _UNPRICED)
                 * _EASE.get(rec.get("effort"), 0.8)
                 * _CONF_WEIGHT.get(((rec.get("confidence") or {}).get("band")), 0.85), 2)


def order_recommendations(recs, quiet_kinds=(), learned=None):
    """Highest rank first; a kind the owner has let expire unanswered four
    times running (decisions.quiet_kinds) drops below the top three.

    `learned` is this restaurant's effectiveness model
    (rec_learning.effectiveness — callable key -> (weight, why)): what it
    learned from this restaurant's own answers and measured results moves
    each card's rank by a bounded weight (0.6–1.25×; ROI audit #24, #47,
    #29). It reorders only — nothing is dropped — and a card with
    `critical` severity is never weighed down."""
    for r in recs:
        r["rank_score"] = rank_score(r)
        if learned is not None and r.get("severity") != "critical":
            try:
                w, why = learned(r["key"])
            except Exception as e:
                print(f"[home] learned weight unavailable for {r.get('key')}: {e}")
                w, why = 1.0, []
            if w != 1.0:
                r["rank_score"] = round(r["rank_score"] * w, 2)
                r["learned"] = {"weight": w, "why": why[:3]}
    ranked = sorted(recs, key=lambda r: -r["rank_score"])
    quiet = set(quiet_kinds or ())
    loud = [r for r in ranked if r["key"].split(":", 1)[0] not in quiet]
    soft = [r for r in ranked if r["key"].split(":", 1)[0] in quiet]
    for r in soft:
        r["quiet"] = True
    return loud[:3] + soft + loud[3:]


def at_stake_monthly(drivers) -> float:
    """The food drivers' monthly dollars with each ingredient counted ONCE.

    cost_drivers can name one ingredient several times — its waste, its
    price rise, its usage over recipe — each measured from the same spend.
    Summing all of them counted salmon three times. Each item keeps its
    largest driver; drivers with no item stand alone."""
    best, loose = {}, 0.0
    for d in drivers or []:
        dollars = float(d.get("dollars_monthly") or 0)
        item = str(d.get("item") or "").strip().lower()
        if not item:
            loose += dollars
            continue
        best[item] = max(best.get(item, 0.0), dollars)
    return round(sum(best.values()) + loose, 2)


def overtime_this_week(labor, today):
    """The payroll week containing `today`: who is past 40 hours in it and
    what the overtime premium (the extra half-time) is costing.

    Home used to say "N staff members in overtime this week … Roughly
    ${N * 38}+" — N counted every overtime week in the whole upload, and $38
    was a constant. This is the current week only, and the premium is the
    analysis's own (labor.overtime_premium over its overtime hours) applied
    to this week's hours past 40. None when nobody is over this week."""
    if not labor or not labor.get("is_live"):
        return None
    try:
        from labor import _week_key
        wk = _week_key(today.isoformat(), int(labor.get("week_start_day") or 0))
    except Exception:
        return None
    from labor import OVERTIME_THRESHOLD_HOURS
    rows = [o for o in (labor.get("overtime_risk") or [])
            if o.get("status") == "overtime" and o.get("week_start") == wk]
    if not rows:
        return None
    ot_hours = round(sum(max(0.0, float(o.get("hours") or 0) - OVERTIME_THRESHOLD_HOURS) for o in rows), 1)
    total_hours = float(labor.get("overtime_hours") or 0)
    premium = None
    if total_hours > 0 and labor.get("overtime_premium") is not None:
        premium = round(float(labor["overtime_premium"]) / total_hours * ot_hours, 2)
    return {"people": len(rows), "hours": ot_hours, "premium": premium, "week_start": wk,
            "names": [str(o.get("employee") or "")[:18] for o in rows[:3]],
            "estimated": any(o.get("hours_estimated") for o in rows)}


# Home's module keys as rec_ledger's module vocabulary.
_LEDGER_MODULE = {"reviews": "reviews", "labor": "labor", "inventory": "food", "marketing": "marketing",
                  "intel": "intel", "account": "home", "alerts": "home"}


def _may_assign(user) -> bool:
    """Whoever may set issue routing may hand a card to someone (the same
    line strategy_routes._principal draws)."""
    if user.get("is_admin"):
        return True
    try:
        from permissions import has_permission, TEAM_INVITE
        return has_permission(user, TEAM_INVITE)
    except Exception:
        return False


# Home's attention keys, as the rec_ledger key the SAME news carries on the
# brief, the queue and the ALERT (notify.alert_rec) — "reviews waiting" is one
# piece of news whichever surface says it, so one answer silences it
# everywhere. Items whose key carries a subject ("labor_over:<period>",
# "stock_low:<item>") set it where they are built.
LEDGER_KEY = {"awaiting_approval": "no_response"}
# A card key -> the attention item's ledger key that says the same news.
SAME_NEWS = {"publish_drafts": "no_response"}
# What the clients render (web renderFocus/renderAttention/renderRecs, iOS
# HomeActionDeck/HomeRecommendations): at most this many of each.
HOME_ATTENTION_SHOWN = 4
HOME_RECS_SHOWN = 3


def ledger_key(key):
    return LEDGER_KEY.get(key, key)


def other_days_mean(dow, day) -> float:
    """The mean labor % of every weekday but `day` — what "N pts above your
    other days" is measured against. A mean that included the day itself
    understated the gap it names. 0 when there is nothing to compare."""
    vals = [float(v) for k, v in (dow or {}).items() if v and k != day]
    return sum(vals) / len(vals) if vals else 0


def stock_key(item) -> str:
    """One critically-low item's recommendation key — the alert's own
    ("stock_low:Salmon", notify.alert_rec)."""
    import rec_ledger
    return rec_ledger.rec_key("stock_low", str(item or "?"))


def _stock_quiet(rid) -> set:
    try:
        import rec_ledger
        return rec_ledger.silenced_keys(rid)
    except Exception:
        return set()


def _current_stock_keys(rid) -> list:
    """The stock keys a critically-low card would show right now (live
    inventory only) — what one answer on that card answers."""
    try:
        from inventory import analysis_for
        items, is_live, analysis = analysis_for(rid)
        if not (items and is_live):
            return []
        return [stock_key(x.get("item")) for x in ((analysis or {}).get("critical_low") or [])[:10]]
    except Exception as e:
        print(f"[home] critical-low items unavailable: {e}")
        return []


def assignees(rid):
    """This restaurant's consented alert contacts — who a card can be
    handed to (issues.create_issue refuses anyone else)."""
    conn = get_conn()
    try:
        return [{"id": r["id"], "name": r["name"]} for r in conn.execute(
            "SELECT id, name FROM alert_contacts WHERE restaurant_id=? AND COALESCE(sms_consent,0)=1 "
            "ORDER BY name", (rid,)).fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def assign(rid, key, title, contact_id, detail=None, user_id=None, role=None, surface="home"):
    """Hand a Home card to a person: an ops_issues row keyed to the
    recommendation (source_key = its key, so the issue's resolution closes
    the recommendation in rec_ledger.sync_existing), texted to the chosen
    consented contact, and recorded as accepted-and-delegated."""
    import issues
    import rec_ledger
    key = (key or "").strip()[:160]
    title = (title or "").strip()[:200]
    if not key or not title:
        return {"ok": False, "error": "Missing recommendation"}, 400
    try:
        contact_id = int(contact_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Pick someone to hand it to."}, 400
    try:
        issue, _token = issues.create_issue(rid, "recommendation", title, detail=(detail or None),
                                            source_key=key, assignee_contact_id=contact_id,
                                            created_by=user_id)
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    rec_ledger.record(rid, key, "accepted", surface=surface, user_id=user_id, role=role,
                      meta={"delegated": True, "issue_id": issue.get("id"),
                            "assignee": issue.get("assignee_name")})
    invalidate(rid)
    return {"ok": True, "issue": {"id": issue.get("id"), "assignee_name": issue.get("assignee_name"),
                                  "status": issue.get("status")}}, 200


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
    # Bought is not the same as allowed: a shift manager at a full-plan
    # restaurant still may not read Food Cost (permissions.ROLE_MANAGER), and
    # Home — and the Ask opening, which is built from this payload — must
    # drop every module the role can't see, exactly as its routes refuse it.
    if not current_user.get("is_admin"):
        from permissions import MODULE_VIEW_PERMISSIONS, has_permission
        active = [m for m in active
                  if m["key"] not in MODULE_VIEW_PERMISSIONS
                  or has_permission(current_user, MODULE_VIEW_PERMISSIONS[m["key"]])]
    active_keys = {m["key"] for m in active}
    conn = get_conn()
    r = _one_dict(conn, "SELECT * FROM restaurants WHERE id=?", (rid,)) or {}

    # ── previous visit (this user) ──────────────────────────────────────────
    prev_login = _rows_dict(conn, "SELECT created_at FROM login_history WHERE user_id=? ORDER BY id DESC LIMIT 2", (current_user.get("id"),))
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
    reviews_since = _one_dict(conn, "SELECT COUNT(*) AS n, ROUND(AVG(rating),1) AS avg, SUM(rating<=2) AS low FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL AND (fetched_at >= ? OR fetched_at >= ?)", (rid, since_sql, since_iso_t)) or {}
    replies_since = _one_dict(conn, "SELECT SUM(response_status='posted' AND posted_at >= ?) AS posted, SUM(response_status IN ('approved','posted') AND approved_at >= ?) AS approved FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL", (since_iso_t, since_iso_t, rid)) or {}
    # Urgent and stale-unanswered count only reviews from the last
    # REPLY_OWED_MAX_AGE_DAYS — the window the brief, the queue and the
    # alerts use. A first Google connect imports years of history, and two
    # 2023 urgent reviews are not a reply anyone owes today.
    from thresholds import REPLY_OWED_MAX_AGE_DAYS as _OWED_DAYS
    _owed_since = f"-{int(_OWED_DAYS)} days"
    stale_unanswered = _one_dict(conn, "SELECT COUNT(*) AS n FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL AND rating<=3 AND response_status IN ('pending','drafted') AND julianday(fetched_at) < julianday('now','-2 days') AND COALESCE(NULLIF(review_date,''), fetched_at) >= date('now', ?)", (rid, _owed_since)) or {}
    urgent_owed = _one_dict(conn, "SELECT COUNT(*) AS n FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL AND urgency='high' AND response_status NOT IN ('posted','approved','skipped') AND COALESCE(NULLIF(review_date,''), fetched_at) >= date('now', ?)", (rid, _owed_since)) or {}
    rating_prev = _one_dict(conn, "SELECT ROUND(AVG(rating),1) AS r, COUNT(*) AS n FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL AND COALESCE(NULLIF(review_date,''), fetched_at) >= date('now','-60 days') AND COALESCE(NULLIF(review_date,''), fetched_at) < date('now','-30 days')", (rid,)) or {}

    # The client_data row carries the whole shifts CSV. Read once here and
    # handed to Labor and Food Cost, which each read it for themselves —
    # three reads of a year of shifts on every cold Home build (MOD-HOME-2).
    stored = None
    if "labor" in active_keys or "inventory" in active_keys:
        from models import get_client_data as _get_client_data
        try:
            stored = _get_client_data(rid)
        except Exception:
            stored = None

    # ── labor ───────────────────────────────────────────────────────────────
    labor, labor_live = None, False
    if "labor" in active_keys:
        try:
            from labor import analyse_shifts_for_restaurant
            labor = analyse_shifts_for_restaurant(rid, client_data=stored)
            labor_live = bool(labor.get("is_live"))
        except Exception:
            labor = None
    from notify import labor_target_for as _labor_target_for
    labor_target = _labor_target_for(r)
    labor_hist = get_labor_history(rid, limit=8) if labor_live else []
    client_data = _one_dict(conn, "SELECT updated_at, shifts_source, inventory_source FROM client_data WHERE restaurant_id=?", (rid,)) or {}
    last_schedule = _one_dict(conn, "SELECT generated_at, week_start, week_end, hours_scheduled, hours_budget FROM schedule_history WHERE restaurant_id=? ORDER BY id DESC LIMIT 1", (rid,))

    # ── food cost ───────────────────────────────────────────────────────────
    inv, inv_live = {}, False
    if "inventory" in active_keys:
        try:
            from inventory import analysis_for
            items, inv_live, inv = analysis_for(rid, client_data=stored)
            inv = inv if items else {}
        except Exception:
            inv = {}

    # ── marketing ───────────────────────────────────────────────────────────
    mkt = {}
    if "marketing" in active_keys:
        # Real pieces only (marketing.pieces_this_month): not calendar markers
        # or the weekly job's own unseen drafts.
        import marketing as _mkt
        mkt["month"] = _mkt.pieces_this_month(rid)
        # The same real-piece count as "this month" (M-27): all rows here
        # put "5 this week" under "3 this month".
        mkt["week"] = _mkt.count_pieces(rid, "-7 days")
        mkt["last_at"] = (_one_dict(conn, "SELECT MAX(created_at) AS t FROM marketing_content_log WHERE restaurant_id=?", (rid,)) or {}).get("t")
        mkt["last_posted_at"] = (_one_dict(conn, "SELECT MAX(created_at) AS t FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL", (rid,)) or {}).get("t")
        mkt["scheduled"] = _rows_dict(conn, "SELECT id, platform, topic, content_type, scheduled_for, status FROM marketing_scheduled_posts WHERE restaurant_id=? AND status IN ('scheduled','pending') AND scheduled_for >= datetime('now') ORDER BY scheduled_for LIMIT 3", (rid,))
        mkt["failed"] = _rows_dict(conn, "SELECT id, platform, topic, error, scheduled_for FROM marketing_scheduled_posts WHERE restaurant_id=? AND status='failed' ORDER BY id DESC LIMIT 3", (rid,))
        mkt["posted_since"] = (_one_dict(conn, "SELECT COUNT(*) AS n FROM marketing_scheduled_posts WHERE restaurant_id=? AND status='posted' AND posted_at >= ?", (rid, since_sql)) or {}).get("n") or 0
        try:
            mkt["post_result"] = _one_dict(conn,
                "SELECT c.topic, COALESCE(c.posted_at, c.created_at) AS posted_at, a.lift_pct, a.item_lift_pct, "
                "       a.reviews_mentioning, a.verdict, m.name AS menu_item_name "
                "FROM marketing_attribution a JOIN marketing_content_log c ON c.id = a.content_log_id "
                "LEFT JOIN menu_items m ON m.id = c.menu_item_id "
                "WHERE a.restaurant_id=? AND a.lift_pct IS NOT NULL AND julianday(COALESCE(c.posted_at, c.created_at)) >= julianday('now','-10 days') "
                "ORDER BY COALESCE(c.posted_at, c.created_at) DESC LIMIT 1", (rid,))
        except Exception:
            mkt["post_result"] = None
        mkt["ig_connected"] = bool(r.get("ig_token"))
        mkt["fb_connected"] = bool(r.get("fb_page_token"))

    # ── intel ───────────────────────────────────────────────────────────────
    intel = None
    if "intel" in active_keys:
        intel = {"updated_at": r.get("competitor_updated_at"), "recs": 0, "competitors": 0, "withheld": 0}
        if r.get("competitor_intel"):
            # Open recommendations only — not ones the owner answered, not
            # ones an unverified read withheld (M-20).
            try:
                from client_api import intel_open_recs
                _io = intel_open_recs(rid, restaurant=r)
                intel["recs"] = len(_io["recs"])
                intel["competitors"] = _io["competitors"]
                intel["withheld"] = _io["withheld"]
            except Exception as e:
                print(f"[home] intel recs failed for {rid}: {e}")

    # ── alerts ──────────────────────────────────────────────────────────────
    alerts_7d = _rows_dict(conn, "SELECT alert_type, COUNT(*) AS n, MAX(fired_at) AS last_at FROM alert_log WHERE restaurant_id=? AND julianday(fired_at) >= julianday('now','-7 days') GROUP BY alert_type ORDER BY n DESC", (rid,))
    alerts_since_by_type = _rows_dict(conn, "SELECT alert_type, COUNT(*) AS n FROM alert_log WHERE restaurant_id=? AND fired_at >= ? GROUP BY alert_type", (rid, since_sql))
    recent_alerts = _rows_dict(conn, """SELECT a.alert_type, a.review_id, a.fired_at, rv.rating, rv.response_status, rv.author
                                   FROM alert_log a LEFT JOIN reviews rv ON rv.id=a.review_id
                                   WHERE a.restaurant_id=? AND julianday(a.fired_at) >= julianday('now','-7 days')
                                   ORDER BY a.id DESC LIMIT 12""", (rid,))
    dismissed = _dismissed_keys(conn, rid)
    # Read while the connection is open. This call used to sit after
    # conn.close(), so it always returned {} and the "you've hidden this
    # before — tell us why" prompt could never fire (#7).
    hidden_counts = times_hidden(conn, rid)
    # Replies owed: reviews from the last REPLY_OWED_MAX_AGE_DAYS only.
    # get_review_stats counts every drafted review ever imported, so a
    # freshly connected Google account read "212 drafted, waiting for you"
    # about years of history (#6). Older ones are counted, and said, apart.
    # The count is the set a bulk publish may post (models.reply_queue_
    # counts): urgent and flagged drafts are held for a person and named
    # apart, so "Publish N" never counts a reply the tap would not — or
    # must not — post (M-2). iOS Home reads the same function.
    conn.close()
    try:
        from models import reply_queue_counts
        owed = reply_queue_counts(rid)
    except Exception as e:
        print(f"[home] reply queue count failed for {rid}: {e}")
        owed = {}

    # Alerts are scoped to what this login may see, exactly as the
    # notification list is (client_api._sees). Home read alert_log with no
    # module or role filter, so a shift manager's Home listed — and counted
    # in "N alerts fired" — the Food Cost alerts that role cannot open
    # (MOD-HOME-1).
    from client_api import _NOTIFICATION_MODULE, _sees

    def _visible(alert_type):
        return _sees(current_user, _NOTIFICATION_MODULE.get(alert_type, "reviews"))
    alerts_7d = [a for a in alerts_7d if _visible(a["alert_type"])]
    recent_alerts = [a for a in recent_alerts if _visible(a["alert_type"])]
    alerts_since = sum(int(a.get("n") or 0) for a in alerts_since_by_type if _visible(a["alert_type"]))

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

    def add_attn(key, severity, title, detail, module, action_label, action="open_module", since=None, evidence=None,
                 rec_key=None):
        attention.append({"key": key, "rec_key": rec_key, "severity": severity, "title": title, "detail": detail, "module": module,
                          "action": {"label": action_label, "kind": action, "module": module},
                          "since": since, "evidence": evidence, "location": restaurant.location_name or None})

    def add_win(key, title, detail, module):
        wins.append({"key": key, "title": title, "detail": detail, "module": module})

    def add_rec(key, title, why, evidence, impact, module, timeframe, strength="moderate",
                action_label=None, metric=None, *, conf=("medium", None), dollars=None, if_ignored=None,
                effort=None, alternative=None, action=None, evidence_sources=None, model_written=False):
        """`metric` is what would have to move for this recommendation to have
        worked. It is what makes the card trackable: the client posts it to
        /api/outcomes, which takes the baseline now and re-measures when the
        window closes.

        A recommendation with no honest metric carries None and renders
        without a Track control. Inventing one — pointing "post more this
        week" at sales — would produce a tracker that reads every unrelated
        thing that moved sales as proof the post worked, which is worse than
        not measuring it.
        """
        # Every card answers five questions (#20): what to do (the title,
        # verb first), why now (`why`), what is at stake in dollars when it
        # was measured (`dollars_monthly`, else None — never an invented
        # figure), how sure (`confidence`: ONE band from the card's own
        # evidence, which the kind's measured record here may nudge by one
        # step, #4), and what happens if it is ignored (`if_ignored`).
        recs.append({"key": key, "title": title, "why": why, "evidence": evidence, "impact": impact, "module": module,
                     "timeframe": timeframe, "strength": strength, "metric": metric,
                     "action_label": action_label or "Open " + {"inventory": "Food Cost"}.get(module, module.title()),
                     "dollars_monthly": round(float(dollars), 2) if dollars else None,
                     "if_ignored": if_ignored, "effort": effort, "alternative": alternative,
                     # A one-tap finish when there is one (a reprice at the
                     # suggested price), else None and the card opens its module.
                     "action": action,
                     "evidence_sources": evidence_sources or [module], "model_written": bool(model_written),
                     "confidence": _card_confidence(rid, key, metric, restaurant, conf[0], conf[1])})

    def add_change(text, tone, module, at=None):
        changes.append({"text": text, "tone": tone, "module": module, "at": at})

    # ── Reviews ────────────────────────────────────────────────────────────
    if "reviews" in active_keys:
        total = int(rstats.get("total") or 0)
        urgent = int(urgent_owed.get("n") or 0)
        # Drafted replies to reviews from the last 30 days; older drafts are
        # history and are named separately, never counted in (#6).
        awaiting = int(owed.get("publishable") or 0)
        awaiting_older = int(owed.get("older") or 0)
        awaiting_held = int(owed.get("held") or 0)
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
            add_attn("awaiting_approval", "watch" if awaiting < 5 else "important", f"{_plural(awaiting, 'reply', 'replies')} drafted, waiting for you",
                     "Replies to reviews from the last 30 days, written in your voice. Approve them in one click and "
                     "Google-connected replies post right away.", "reviews",
                     # Names what the tap publishes, not just a count —
                     # "Publish 1" alone doesn't say what gets published.
                     f"Publish {min(awaiting, 25)} {_plural(min(awaiting, 25), 'reply', 'replies').split(' ', 1)[1]}",
                     action="publish_replies",
                     evidence=f"{awaiting} drafted"
                     + (f" · {awaiting_held} urgent or flagged held for you to read" if awaiting_held else "")
                     + (f" · {awaiting_older} older drafts not counted" if awaiting_older else ""))
            # The tap publishes exactly what the label counts (approve-all
            # takes a limit, newest first) — not 25 including the history.
            attention[-1]["action"]["count"] = min(awaiting, 25)
        if total >= 5 and rate < 50:
            # Only what the data shows: the count. The old line claimed a
            # guest-count effect of answering reviews that had no source (#46).
            add_attn("low_response_rate", "watch", f"Response rate at {rate:.0f}%",
                     f"{int(rstats.get('responded') or 0)} of {total} reviews have a reply.", "reviews", "Answer reviews",
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
            add_win("response_rate", f"{rate:.0f}% of reviews answered",
                    f"{int(rstats.get('responded') or 0)} of {total} reviews have a reply.", "reviews")

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
            # No metric: publishing replies is not measured against the
            # rating (it was, and a rating move a fortnight later would have
            # been credited to the replies, #46). "Rank higher" had no source.
            add_rec("publish_drafts", f"Publish the {awaiting} drafted replies",
                    f"{awaiting} guests from the last 30 days have a reply written and not yet posted.",
                    f"{awaiting} replies drafted in your voice · {rate:.0f}% of reviews currently answered"
                    + (f" · {awaiting_older} older drafts not counted" if awaiting_older else ""),
                    "Reputation · response rate", "reviews", "Today", "strong", "Publish now",
                    metric=None, conf=("high", f"a count of {awaiting} drafts on file"),
                    if_ignored="those guests, and everyone who reads their reviews, see no reply",
                    effort="low", action={"kind": "publish_replies", "count": min(awaiting, 25)})
        if top_issues and total >= 10:
            cat = top_issues[0].get("category") or top_issues[0]["label"]
            lbl, cnt = top_issues[0]['label'], int(top_issues[0].get('count') or 0)
            if cnt >= 3:
                # One complaint, one instruction (#19): the stored diagnosis's
                # recommended_action when there is one — the Reviews tab has
                # it — with its alternative explanation (#42). Without one,
                # the card says what the data supports and nothing more.
                dg = None
                try:
                    import review_intelligence as _ri_hb
                    dg = next((d for d in _ri_hb.get_diagnoses(rid, include_stale=True)
                               if d.get("category") == cat and d.get("recommended_action")), None)
                except Exception:
                    dg = None
                ev_band = "high" if cnt >= 8 else ("medium" if cnt >= 5 else "low")
                if dg:
                    band = dg.get("confidence") if dg.get("confidence") in ("low", "medium", "high") else ev_band
                    # The Reviews diagnosis card's own key ("diag_review:<cat>"),
                    # so "Not for us" on either one holds on both (M-9).
                    from client_api import diagnosis_rec_key as _drk_rv
                    add_rec(_drk_rv("diag_review", dg) or f"top_issue:{cat}",
                            str(dg["recommended_action"]).strip().rstrip("."),
                            (dg.get("cause") or f"{lbl} is the most-mentioned complaint over 90 days.").strip(),
                            f"{lbl} raised in {cnt} reviews over 90 days"
                            + ((" · " + (dg.get("stale_note") or "diagnosis older than a week").rstrip("."))
                               if dg.get("stale") else ""),
                            "Reviews · rating", "reviews", "This week", "strong" if cnt >= 5 else "moderate",
                            "See the reviews", metric=f"complaints:{cat}",
                            conf=(band, f"a diagnosis read from {cnt} reviews"
                                  + ("; written over a week ago" if dg.get("stale") else "")),
                            if_ignored=f"{lbl.lower()} stays the most-mentioned complaint",
                            effort="medium", alternative=dg.get("alternative_cause"), model_written=True)
                else:
                    add_rec(f"top_issue:{cat}", f"Read the {cnt} {lbl.lower()} complaints and pick one fix for this week",
                            f"{lbl} is the most-mentioned complaint in the last 90 days.",
                            f"{lbl} raised in {cnt} reviews over 90 days", "Reviews · rating", "reviews", "This week",
                            "strong" if cnt >= 5 else "moderate", "See the reviews",
                            metric=f"complaints:{cat}",
                            conf=(ev_band, f"{cnt} reviews in 90 days"),
                            if_ignored=f"{lbl.lower()} stays the most-mentioned complaint", effort="medium")
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
            from thresholds import LABOR_OVER_TARGET_PTS
            pct = float(labor.get("overall_labor_pct") or 0)
            over = pct - labor_target
            days = int((labor.get("date_range") or {}).get("days") or 0)
            # This payroll week only, with the analysis's own premium (#2).
            ot_now = overtime_this_week(labor, local_now.date())
            # Shown as "/week" below — use the per-week figure, not the
            # whole-period gap.
            savings = float(labor.get("potential_savings_weekly") or 0)
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
            if over >= LABOR_OVER_TARGET_PTS:
                add_attn("labor_over", "important" if over < 6 else "critical", f"Labor at {pct:.1f}% — {over:.1f} pts over your {labor_target:.0f}% target",
                         f"Across the last {days} days of shifts" + (f"; about ${savings:,.0f}/week recoverable by trimming the overstaffed days." if savings > 0 else "."), "labor", "Open labor",
                         since=f"{days}d", evidence=f"${float(labor.get('total_labor_cost') or 0):,.0f} labor on ${float(labor.get('total_sales') or 0):,.0f} sales",
                         # The labor alert's key: its latest period.
                         rec_key=(f"labor_over:{str(labor_hist[0].get('period_start') or '')[:10]}"
                                  if labor_hist and labor_hist[0].get("period_start") else "labor_over"))
            elif over <= 0:
                add_win("labor_on_target", f"Labor at {pct:.1f}% — under target", f"{abs(over):.1f} pts under your {labor_target:.0f}% target over {days} days.", "labor")
            if ot_now:
                n_ot = ot_now["people"]
                prem = ot_now["premium"]
                add_attn("overtime", "important",
                         f"{n_ot} {'person' if n_ot == 1 else 'people'} over 40h this week",
                         (f"About ${prem:,.0f} in overtime premium — the extra half-time on "
                          f"{ot_now['hours']:g} hours past 40" if prem is not None else
                          f"{ot_now['hours']:g} hours past 40 so far")
                         + (" (from scheduled hours — no clock-ins on file)." if ot_now["estimated"] else "."),
                         "labor", "Open schedule", evidence=", ".join(n for n in ot_now["names"] if n))
            # day-of-week recommendation
            if len(dow) >= 4:
                worst_day, worst_pct = max(dow.items(), key=lambda kv: kv[1] or 0)
                mean = other_days_mean(dow, worst_day)
                if mean and worst_pct - mean >= 4 and worst_pct > labor_target:
                    # What that day costs above the owner's own target, from
                    # the days in the upload — a measured figure, per month.
                    by_day = labor.get("by_day") or {}
                    excess, n_days = 0.0, 0
                    for dstr, dd in by_day.items():
                        try:
                            if datetime.strptime(dstr, "%Y-%m-%d").strftime("%A") != worst_day:
                                continue
                        except (TypeError, ValueError):
                            continue
                        n_days += 1
                        excess += max(0.0, float(dd.get("labor_cost") or 0) - float(dd.get("sales") or 0) * labor_target / 100.0)
                    trim_monthly = round(excess / n_days * 52.0 / 12.0, 2) if n_days else None
                    add_rec(f"trim_day:{worst_day}", f"Trim {worst_day} staffing on the next schedule",
                            f"{worst_day} runs {worst_pct - mean:.0f} pts above your other days without the sales to justify it.",
                            f"{worst_day} labor {worst_pct:.1f}% vs {mean:.1f}% on your other days · target {labor_target:.0f}%", "Labor · weekly cost", "labor", "Next schedule",
                            "strong" if worst_pct - mean >= 6 else "moderate", "Rebuild the schedule",
                            metric="labor_pct", dollars=trim_monthly,
                            conf=("high" if n_days >= 4 else ("medium" if n_days >= 2 else "low"),
                                  f"{_plural(n_days, worst_day)} in your shift data"),
                            if_ignored=f"{worst_day}s keep running about {worst_pct - labor_target:.0f} pts over your target",
                            effort="medium")
            if delta is not None and delta >= 1.5:
                add_change(f"Labor % rose {delta:+.1f} pts vs the previous period ({pct:.1f}%)", "bad", "labor")
            elif delta is not None and delta <= -1.5:
                add_change(f"Labor % fell {delta:+.1f} pts vs the previous period ({pct:.1f}%)", "good", "labor")
                add_win("labor_improving", f"Labor down {abs(delta):.1f} pts", f"{pct:.1f}% this period vs {prev_pct:.1f}% before.", "labor")
            if last_schedule and _ts(last_schedule.get("generated_at")) and _ts(last_schedule["generated_at"]) >= since_dt:
                hs = float(last_schedule.get("hours_scheduled") or 0); hb = float(last_schedule.get("hours_budget") or 0)
                add_change(f"New schedule built for {_mdy(last_schedule.get('week_start')) or 'next week'}" + (f" · {int(round(hb - hs))} hrs under budget" if hb and hs and hs < hb else ""), "good", "labor", last_schedule.get("generated_at"))
            snapshot.append({"key": "labor", "label": "Labor", "status": "available", "value": f"{pct:.1f}", "unit": "% of sales",
                             "delta": ({"value": f"{delta:+.1f} pts", "label": "vs prior period", "good": delta <= 0} if delta is not None else {"value": f"target {labor_target:.0f}%", "label": "", "good": over <= 0}),
                             "secondary": [{"label": "Target", "value": f"{labor_target:.0f}%"}, {"label": "Over 40h", "value": str(ot_now["people"] if ot_now else 0)}, {"label": "Recoverable", "value": f"${savings:,.0f}/wk" if savings > 0 else "—"}],
                             "interpretation": (f"{over:.1f} pts over target. " + (f"{max(dow.items(), key=lambda kv: kv[1] or 0)[0]} is the heaviest day." if dow else "")) if over > 0 else f"On target. {min(dow.items(), key=lambda kv: kv[1] or 99)[0] if dow else ''} runs leanest.".strip(),
                             "state": "bad" if over > 6 else ("warn" if over > 0 else "good"),
                             "spark": hist_pcts[-8:], "spark_label": "labor % by period" if len(hist_pcts) > 1 else None,
                             "attention": over >= LABOR_OVER_TARGET_PTS or bool(ot_now), "sample": False, "last_data": client_data.get("updated_at")})
            brief_lines.append({"text": f"Labor {pct:.1f}% against a {labor_target:.0f}% target" + (f", {delta:+.1f} pts vs last period" if delta is not None else "") + ".", "tone": "bad" if over >= LABOR_OVER_TARGET_PTS else ("good" if over <= 0 else "neutral"), "module": "labor"})
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
            # One recommendation per item ("stock_low:Salmon", the alert's own
            # key): an item answered anywhere drops off the card, and the
            # card goes only when every item on it is answered. Keyed to the
            # first item alone, answering the salmon alert hid "Salmon,
            # Chicken critically low" entirely.
            if crit:
                _sq = _stock_quiet(rid)
                crit = [c for c in crit if stock_key(c.get("item")) not in _sq]
            if crit:
                add_attn("critical_low", "important", f"{_plural(len(crit), 'item')} critically low",
                         ", ".join(str(c.get("item", ""))[:22] for c in crit[:4]) + " — likely to run out before the next delivery.", "inventory", "See the list",
                         evidence=f"{len(reorder)} more to reorder soon",
                         rec_key=stock_key(crit[0].get("item")))
                attention[-1]["rec_keys"] = [stock_key(c.get("item")) for c in crit[:10]]
            # The CFO read. The module's morning headline was
            # "$X recoverable/month" — a waste-recovery estimate, when the
            # question an operator opens with is where their margin is. Food
            # cost % and the ranked cost drivers were both computed and
            # neither reached this screen. Best-effort: Home must render even
            # when the ledger cannot answer.
            fc_pct, fc_target, fc_label = None, None, None
            drivers, cfo_why, _dg = [], None, None
            try:
                import food_cost_intelligence as _fci_hb
                _ev = _fci_hb.build_evidence(rid)
                _fc = _ev["food_cost"]
                if _fc.get("ok"):
                    fc_pct, fc_target, fc_label = _fc["pct"], _fc.get("target"), _fc.get("label")
                drivers = (_ev["drivers"].get("drivers") or [])
                _dg = _fci_hb.get_diagnosis(rid, include_stale=True)
                if _dg and _dg.get("cause"):
                    cfo_why = {"cause": _dg["cause"], "confidence": _dg.get("confidence"),
                               "stale": _dg.get("stale")}
            except Exception:
                pass

            # The recommendation is now the top RANKED driver — ranked by
            # dollars, then confidence, then ease — rather than whichever item
            # happened to waste the most last week. A driver carries its own
            # evidence, its confidence, how hard it is and what happens if it
            # is ignored, none of which the old waste-only line could say.
            if drivers:
                # The card is the driver's own FIX, verb first ("Cut the
                # Salmon Fillet order — waste is above tolerance"), not its
                # finding label (#20). One confidence: the driver's own, from
                # its own measurement — the evidence line no longer carries
                # a second one (#4).
                import business_intelligence as _bi_hb
                d0 = drivers[0]
                _conf_why = {"waste": "counted waste against its own tolerance band",
                             "portion": "physical counts against recipes — portioning, prep loss or a miscount all fit",
                             "price": "this ingredient's recorded price history",
                             "sourcing": "two suppliers' prices on file for the same unit",
                             "menu": "this dish's plate cost against its own sales"}.get(d0.get("kind"), "the ledger")
                _alt = None
                if _dg and _dg.get("alternative_cause") and str(d0.get("item") or "").lower() in json.dumps(_dg.get("drivers") or []).lower():
                    _alt = _dg["alternative_cause"]
                add_rec(_bi_hb.driver_key(d0), _bi_hb.driver_action(d0),
                        d0["evidence"][:1].upper() + d0["evidence"][1:] + ".",
                        f"${d0['dollars_monthly']:,.0f}/month · {d0['difficulty']} effort",
                        "Food cost · margin", "inventory", "This week",
                        "strong" if d0["dollars_monthly"] >= 150 else "moderate", "See the numbers",
                        # Waste is tracked against waste, not food cost % (#46).
                        metric="weekly_waste" if d0.get("kind") == "waste" else "food_cost_pct",
                        dollars=d0["dollars_monthly"],
                        conf=(d0.get("confidence") or "medium", _conf_why),
                        if_ignored=d0["if_ignored"][:1].upper() + d0["if_ignored"][1:],
                        effort=d0.get("difficulty"), alternative=_alt)
            elif top and float(top.get("waste_cost") or 0) >= 40:
                _wc = float(top.get("waste_cost") or 0)
                add_rec(f"cut_waste:{top.get('item', 'item')}", f"Cut the {top.get('item', 'top-item')} order — it's the biggest waste line",
                        f"It's the single biggest line in last week's waste — {top.get('waste_pct', 0)}% of what you ordered.",
                        f"${_wc:,.0f} wasted last week · ${recoverable:,.0f}/mo recoverable across items", "Food cost · margin", "inventory", "Next order",
                        "strong" if _wc >= 100 else "moderate", "Adjust the order",
                        metric="weekly_waste",
                        dollars=float(top.get("recoverable_cost") or 0) * 52.0 / 12.0 or None,
                        conf=("medium", "one week of waste counts"),
                        if_ignored="the same share keeps going in the bin every week", effort="low")
            elif _dg and _dg.get("recommended_action"):
                # The Food Cost card's key for this diagnosis — its lead
                # driver — not the static "food_diagnosis", whose one Done
                # or Not for us hid every future food diagnosis on Home for
                # ten years, whatever drove it (M-9, H-13).
                from client_api import diagnosis_rec_key as _drk_fd
                add_rec(_drk_fd("diag_food", _dg) or "food_diagnosis",
                        str(_dg["recommended_action"]).strip().rstrip("."),
                        (_dg.get("cause") or "").strip() or "From the stored food cost diagnosis.",
                        _dg.get("headline") or "", "Food cost · margin", "inventory", "This week", "moderate",
                        "See the numbers", metric="food_cost_pct", dollars=_dg.get("dollars_at_stake"),
                        conf=(_dg.get("confidence") if _dg.get("confidence") in ("low", "medium", "high") else "medium",
                              "a diagnosis of this restaurant's own ledger"
                              + (("; " + (_dg.get("stale_note") or "written over a week ago").rstrip(".").lower()
                                  .replace("from a read on", "read on")) if _dg.get("stale") else "")),
                        if_ignored="food cost stays where it is", effort="medium",
                        alternative=_dg.get("alternative_cause"), model_written=True)

            # One-tap reprice: a dish whose ingredients rose, at the price
            # that restores its old food cost % (menu_intelligence), applied
            # by POST /api/food-cost/reprice/apply {dish, price}.
            try:
                import menu_intelligence as _mi_hb
                _sg = [x for x in ((_mi_hb.reprice_suggestions(rid) or {}).get("suggestions") or [])
                       if (x.get("monthly_margin_lost") or 0) >= 25 and x.get("suggested_price")]
            except Exception:
                _sg = []
            _named = str((drivers[0].get("item") if drivers else "") or "").lower()
            for x in _sg[:1]:
                if str(x.get("dish") or "").lower() == _named:
                    continue
                _ing = ((x.get("drivers") or [{}])[0] or {}).get("ingredient") or "An ingredient"
                add_rec(_mi_hb.reprice_key(x['dish']), f"Reprice {x['dish']} to ${x['suggested_price']:.2f}",
                        f"{_ing} rose — {x['dish']} now runs at {x.get('food_cost_pct_now')}% food cost, "
                        f"up from {x.get('food_cost_pct_before')}%.",
                        f"about ${x['monthly_margin_lost']:,.0f}/month of margin at today's price",
                        "Food cost · margin", "inventory", "This week", "moderate", "See the price",
                        metric="food_cost_pct", dollars=x["monthly_margin_lost"],
                        conf=("high" if x.get("units_sold_30d") else "medium",
                              f"ingredient price history and {x.get('monthly_basis') or 'the sales mix'}"),
                        if_ignored="every plate keeps selling at the thinner margin", effort="low",
                        action={"kind": "reprice", "dish": x["dish"], "price": x["suggested_price"],
                                "label": f"Reprice to ${x['suggested_price']:.2f}"})

            # The brief line leads with the margin position when it can be
            # measured, and falls back to recoverable waste when it cannot.
            if fc_pct is not None:
                _over = (fc_target is not None and fc_pct > fc_target)
                brief_lines.append({
                    "text": f"Food cost {fc_pct}% of sales"
                            + (f" against a {fc_target}% target" if fc_target else f" — {fc_label}")
                            + (f", led by {drivers[0]['label'].lower()}" if drivers else "") + ".",
                    "tone": "bad" if _over else "good", "module": "inventory"})
            elif recoverable > 0:
                brief_lines.append({"text": f"${recoverable:,.0f}/month of recoverable food waste, led by {top.get('item') if top else 'a few items'}.", "tone": "warn", "module": "inventory"})
            if recoverable <= 0 and fc_pct is None:
                add_win("waste_low", "Waste is under control", f"{inv.get('benchmark_label') or 'Low'} waste rate" + (f" ({waste_rate}%)" if waste_rate is not None else "") + " this week.", "inventory")
            elif fc_target is not None and fc_pct is not None and fc_pct <= fc_target:
                add_win("food_cost_on_target", f"Food cost {fc_pct}% — on target",
                        f"At or under your own {fc_target}% target over the last 28 days.", "inventory")

            # The headline number is the margin position where it exists, and
            # the recoverable estimate only where it does not.
            _headline = f"{fc_pct}%" if fc_pct is not None else f"${recoverable:,.0f}"
            _unit = ("food cost" if fc_pct is not None else "recoverable / mo")
            _delta = None
            if fc_pct is not None and fc_target is not None:
                _delta = {"value": f"{fc_pct - fc_target:+.1f} pts", "label": fc_label or "",
                          "good": fc_pct <= fc_target}
            elif waste_rate is not None:
                _delta = {"value": f"{waste_rate}% waste", "label": inv.get("benchmark_label") or "",
                          "good": inv.get("benchmark_tone") in ("good", None)}
            _secondary = [{"label": "Critical low", "value": str(len(crit))},
                          {"label": "Reorder soon", "value": str(len(reorder))},
                          {"label": "Stock value", "value": f"${float(inv.get('total_stock_value') or 0):,.0f}"}]
            if drivers:
                # Each ingredient counted once (#36): salmon's waste, price
                # rise and over-recipe usage are three readings of one spend.
                _secondary.insert(0, {"label": "At stake",
                                      "value": f"${at_stake_monthly(drivers):,.0f}/mo"})
            snapshot.append({"key": "inventory", "label": "Food Cost", "status": "available",
                             "value": _headline, "unit": _unit,
                             "delta": _delta,
                             "secondary": _secondary,
                             # WHY, not just what — the one thing the card
                             # could never say.
                             "interpretation": (
                                 cfo_why["cause"] if cfo_why else
                                 (f"{drivers[0]['label']} is the largest driver "
                                  f"(${drivers[0]['dollars_monthly']:,.0f}/month)." if drivers else
                                  (f"{top.get('item')} is the biggest waste line (${float(top.get('waste_cost') or 0):,.0f} last week)." if top else "No waste flagged this week."))),
                             "why_confidence": (cfo_why or {}).get("confidence"),
                             "state": "bad" if (crit or (fc_target and fc_pct and fc_pct > fc_target)) else ("warn" if recoverable > 0 else "good"),
                             "spark": [], "spark_label": None,
                             "attention": bool(crit) or recoverable >= 200 or bool(fc_target and fc_pct and fc_pct > fc_target),
                             "sample": False, "last_data": r.get("inventory_updated_at")})
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
            # Only what the data shows (#46): "accounts that post weekly hold
            # reach" had no source here — reach is not measured.
            add_rec("post_this_week", "Get a post out this week", f"Nothing has gone live in {int(posted_age)} days.",
                    f"last post {int(posted_age)}d ago · {mkt.get('month', 0)} pieces drafted this month", "Marketing · reach", "marketing", "This week", "moderate", "Draft a post",
                    conf=("medium", "the posting gap is measured; what a post does for reach is not"),
                    if_ignored="nothing new goes out to your followers", effort="low")
        elif mkt.get("last_at") is None:
            add_rec("first_post", "Generate your first post", "Cavnar AI writes it in your voice from your reviews and menu — one click.", "no marketing content yet", "Marketing · reach", "marketing", "Today", "early", "Generate a post",
                    conf=("medium", "a setup step — nothing to measure yet"),
                    if_ignored="the Marketing module has nothing to schedule or measure", effort="low")
        if mkt.get("posted_since"):
            add_change(f"{_plural(mkt['posted_since'], 'scheduled post')} went live", "good", "marketing")
        # What the latest measured post did — sales against the same weekday,
        # the dish's own units, reviews that named it. Read from the cached
        # attribution row, so Home stays a read; the sync computes it.
        pr = mkt.get("post_result")
        if pr:
            # Coloured and worded from the noise-band verdict, never the
            # sign (M-23): +1% on a weekday that swings 15% was a green win.
            _verdict = pr.get("verdict")
            bits = [f"{pr['lift_pct']:+.0f}% sales vs the same weekday"
                    + (" — within its normal swing" if _verdict == "no_clear_change" else "")]
            if pr.get("item_lift_pct") is not None and pr.get("menu_item_name"):
                bits.append(f"{pr['menu_item_name']} {pr['item_lift_pct']:+.0f}%")
            if pr.get("reviews_mentioning"):
                bits.append(f"{_plural(int(pr['reviews_mentioning']), 'review')} mentioned it")
            add_change(f"Your post on {pr.get('topic') or 'the last post'}: " + " · ".join(bits),
                       {"lifted": "good", "dropped": "bad"}.get(_verdict, "neutral"), "marketing",
                       at=pr.get("posted_at"))
        for p in mkt.get("scheduled") or []:
            upcoming.append({"label": f"{(p.get('platform') or '').title()} post · {p.get('topic') or p.get('content_type') or 'scheduled'}", "when": p.get("scheduled_for"), "module": "marketing", "kind": "post"})
        snapshot.append({"key": "marketing", "label": "Marketing", "status": "available", "value": str(mkt.get("month", 0)), "unit": "pieces this month",
                         "delta": ({"value": f"{mkt.get('week', 0)} this week", "label": "", "good": (mkt.get("week") or 0) > 0}),
                         "secondary": [{"label": "Scheduled", "value": str(len(mkt.get("scheduled") or []))}, {"label": "Last live", "value": (f"{int(posted_age)}d ago" if posted_age is not None else "—")}, {"label": "Channels", "value": ", ".join(x for x, on in (("IG", mkt.get("ig_connected")), ("FB", mkt.get("fb_connected"))) if on) or "none"}],
                         # M/D/YY, like every owner-facing date (M-26, H-20).
                         "interpretation": (f"Next post {_mdy(mkt['scheduled'][0].get('scheduled_for', ''))} on {(mkt['scheduled'][0].get('platform') or '').title()}." if mkt.get("scheduled") else ("Nothing scheduled — draft something for this week." if mkt.get("ig_connected") or mkt.get("fb_connected") else "Connect Instagram to publish and track posts from here.")),
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
                         # Three different states, said apart (M-20): no
                         # competitors yet; tracked but nothing open; open.
                         "interpretation": ("Weekly competitor read is ready." if intel["recs"]
                                            else ("Add competitors on the Intel tab to get a weekly comparison."
                                                  if not intel["competitors"]
                                                  else ("This week's suggestions were held back — their figures couldn't be checked."
                                                        if intel.get("withheld")
                                                        else "Nothing open from this week's competitor read."))),
                         "state": "good" if intel["recs"] else "neutral", "spark": [], "spark_label": None, "attention": False, "sample": False, "last_data": intel.get("updated_at")})
        if intel["recs"] and age is not None and age <= 8:
            # Say only what the data shows (#46): the read is a model-written
            # comparison, not a finding that neighbours do things "you aren't".
            add_rec("intel_recs", f"Read the {_plural(intel['recs'], 'suggestion')} from this week's competitor comparison",
                    f"The weekly Intel pass compared you with {_plural(intel['competitors'], 'nearby competitor')} {int(age)}d ago.",
                    f"{intel['competitors']} competitors compared {int(age)}d ago", "Intel · positioning", "intel", "This week", "moderate", "Open Intel",
                    conf=("low" if age > 7 else "medium", "a model-written comparison of public listings"),
                    if_ignored="the suggestions age out at the next weekly pass", effort="medium", model_written=True)

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

    # ── one "no" everywhere ────────────────────────────────────────────────
    # An answer given on Home, in the brief, the queue or an email silences
    # the key here too (#17); home_dismissals is still read (silenced_keys
    # includes it) so answers from before the ledger hold. Needs attention
    # answers the same way — hide, not today — except a critical item, which
    # is the product's own health or a guest waiting, and is never hidden.
    import rec_ledger
    import decisions
    try:
        silenced = rec_ledger.silenced_keys(rid)
    except Exception:
        silenced = set()
    answered = set(dismissed) | silenced
    for a in attention:
        a["rec_key"] = a.get("rec_key") or ledger_key(a["key"])
        a["dismissable"] = a["severity"] != "critical"
        a["times_hidden"] = hidden_counts.get(a["rec_key"], 0)
    attention = [a for a in attention if not (a["dismissable"] and (a["rec_key"] in answered))]

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
    # The headline as THIS viewer may see it, a monthly run-rate of what was
    # measured, with where it came from (H-8). A filtered figure is not the
    # restaurant's, so it is neither snapshotted nor drawn against the
    # restaurant-wide history.
    from value_delivered import headline as _value_headline, record_value_snapshot, get_value_history, home_block
    try:
        _vh = _value_headline(rid, user=current_user)
    except Exception as e:
        print(f"[home] value headline failed for {rid}: {e}")
        _vh = {"monthly": 0, "by_module": [], "label": "measured, per month", "restaurant_wide": False}
    total_value = _vh["monthly"]
    if _vh.get("restaurant_wide"):
        try:
            # The day's point is the NET figure (re-audit A29): a history of
            # improvements alone rose while things got worse.
            record_value_snapshot(rid, _vh.get("net_monthly", total_value))
        except Exception as e:
            print(f"[home] value snapshot failed for {rid}: {e}")
        value_history = get_value_history(rid, days=365)
    else:
        value_history = []

    has_any_data = bool(rstats.get("total")) or labor_live or inv_live or bool(mkt.get("last_at") if mkt else False)
    empty_state = None
    if not has_any_data:
        # "Start with the checklist" pointed at a checklist retired in Sep
        # 2026 (_setup_checklist returns [] unconditionally), so the first
        # thing a new owner read told them to use something that is not on
        # the screen.
        #
        # What replaces it is the same first look the welcome email leads
        # with: Google's own rating for this restaurant and how it sits
        # against the comparable places nearest it. It costs a Places call
        # on a screen that by definition has nothing else to render, and it
        # means the first session says something true about the business
        # rather than describing what will happen later.
        _look_lines = []
        if r.get("google_place_id"):
            try:
                import first_look
                # Request path — details only (see first_look.PLACES_TIMEOUT).
                _look_lines = first_look.lines(first_look.build(r["google_place_id"]))
            except Exception as _fle:
                print(f"[home] first look unavailable: {_fle}")
        empty_state = {"kind": "new_account",
                       "title": f"Welcome{', ' + (restaurant.owner_name or current_user.get('username') or '') if (restaurant.owner_name or current_user.get('username')) else ''}.",
                       "body": ("Your brief fills in as data arrives: reviews the moment Google is "
                                "connected, labor once shifts are in, food cost after a first count."),
                       "first_look": _look_lines}

    for _r in recs:
        _r["times_hidden"] = hidden_counts.get(_r["key"], 0)

    dismissed_recs = [r for r in recs if r["key"] in answered]
    recs = [r for r in recs if r["key"] not in answered]
    # One piece of news in one place: a card that says what an attention
    # item already says ("Publish the 5 drafted replies" beside "5 replies
    # drafted, waiting for you") is left out while that item is on the page,
    # and stays out once the item is answered.
    _att_keys = {a["rec_key"] for a in attention}
    recs = [r for r in recs if not (SAME_NEWS.get(r["key"]) and
                                    (SAME_NEWS[r["key"]] in _att_keys or SAME_NEWS[r["key"]] in answered))]
    # Ordered by urgency x dollars x ease (#24), and a kind the owner has let
    # expire unanswered four times running goes quieter (#45).
    try:
        quiet = decisions.quiet_kinds(rid)
    except Exception:
        quiet = set()
    # ...and what this restaurant's own answers and results taught the
    # ledger weighs each card, within bounds (rec_learning, ROI #24/#47).
    try:
        import rec_learning
        learned = rec_learning.effectiveness(rid, restaurant=restaurant)
    except Exception as e:
        print(f"[home] effectiveness model unavailable for {rid}: {e}")
        learned = None
    recs = order_recommendations(recs, quiet, learned=learned)
    quieter = []
    for _r in recs:
        _k = _r["key"].split(":", 1)[0]
        if _r.get("quiet") and _k not in [q["kind"] for q in quieter]:
            quieter.append({"kind": _k, "label": decisions.kind_label(_k)})

    # Only what the clients render is logged as shown — and later counted as
    # ignored. Web shows the focus card plus three attention rows ("+N
    # more" beyond them) and three cards (two beside a focus card that leads
    # with the first); iOS a deck of the first four attention items and
    # three cards. The payload keeps every attention item (web's "+N more"
    # counts them) and exactly the cards both clients show.
    recs = recs[:HOME_RECS_SHOWN]
    rendered_attention = attention[:HOME_ATTENTION_SHOWN]

    # Every card and attention item this Home shows is an impression in the
    # ledger (#37) — one episode per key, one `shown` per surface per day.
    # A key the ledger says is answered comes back None and is not shown.
    try:
        shown = rec_ledger.present_many(rid, [
            *({"key": a["rec_key"], "module": _LEDGER_MODULE.get(a["module"], "home"), "title": a["title"],
               "position": i} for i, a in enumerate(rendered_attention)),
            # A card that stands for several (critically low: one key per
            # item) shows each of them.
            *({"key": k, "module": _LEDGER_MODULE.get(a["module"], "home"), "title": a["title"], "position": i}
              for i, a in enumerate(rendered_attention) for k in (a.get("rec_keys") or []) if k != a["rec_key"]),
            *({"key": r["key"], "module": _LEDGER_MODULE.get(r["module"], "home"), "title": r["title"],
               "position": 100 + i, "dollar_value": r.get("dollars_monthly"),
               "confidence_band": (r.get("confidence") or {}).get("band"),
               "evidence_sources": r.get("evidence_sources"), "model_written": r.get("model_written"),
               "cavnar_completes": bool(r.get("action")), "expected_metric": r.get("metric"),
               # The price it asks for: a new one is a new recommendation
               # (rec_ledger supersedes the open episode, ROI #37).
               "target": ((r.get("action") or {}).get("price") if (r.get("action") or {}).get("kind") == "reprice"
                          else None)}
              for i, r in enumerate(recs))], "home", user_id=current_user.get("id"))
    except Exception:
        shown = {}
    if shown:
        attention = [a for a in attention if not a["dismissable"] or shown.get(a["rec_key"], True) is not None]
        recs = [r for r in recs if shown.get(r["key"], True) is not None]

    # ── quick actions ──────────────────────────────────────────────────────
    quick = []
    # The same count the attention card and approve-all use (M-2): the tap
    # publishes min(n, 25) of exactly these, and `count` is what it posts.
    _pub = int(owed.get("publishable") or 0)
    if "reviews" in active_keys and _pub:
        quick.append({"key": "publish", "label": f"Publish {min(_pub, 25)} {'reply' if min(_pub, 25) == 1 else 'replies'}",
                      "kind": "publish_replies", "module": "reviews", "count": min(_pub, 25)})
    if "reviews" in active_keys and int(urgent_owed.get("n") or 0):
        quick.append({"key": "urgent", "label": "Answer urgent reviews", "kind": "open_module", "module": "reviews", "count": int(urgent_owed["n"])})
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
    from permissions import LOCATION_SWITCH as _LOC_SWITCH, has_permission as _has_perm
    if _has_perm(current_user, _LOC_SWITCH):
        try:
            from models import get_location_group
            base = get_restaurant(current_user.get("base_restaurant_id") or rid)
            if base and base.location_group:
                group_name = base.location_group
                c2 = get_conn()
                for lr in get_location_group(base.location_group, owner_email=base.owner_email):
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

    charts = {
        "rating": [{"label": _mdy(w.get("label"), (w.get("week_key") or "")[:4]), "avg": w.get("avg_rating") or 0, "pos": w.get("positive") or 0, "neg": w.get("negative") or 0, "total": w.get("total") or 0} for w in sentiment if w.get("total")],
        "labor": [{"label": _mdy(h.get("period_start")), "pct": h.get("labor_pct")} for h in labor_hist[::-1] if h.get("labor_pct") is not None] if labor_live else [],
        "labor_target": labor_target,
        "labor_days": ([{"day": k[:3], "pct": v} for k, v in (labor.get("dow_summary") or {}).items() if v] if (labor_live and labor) else []),
        "waste": ([{"item": w.get("item"), "cost": float(w.get("waste_cost") or 0)} for w in (inv.get("waste_items") or [])[:5]] if inv_live else []),
    }

    readiness = _safe_readiness(rid, restaurant, r, rstats, labor_live, inv_live, mkt)

    payload = {
        "ok": True,
        "readiness": readiness,
        "charts": charts,
        "generated_at": _iso_z(now),
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
        # Kinds gone quieter because the last four went unanswered, with the
        # way back (POST /api/home/dismiss {restore_kind}).
        "quieter": quieter,
        # Who a card can be handed to — consented alert contacts, and only
        # for a login that may open issues (#43).
        "assignees": (assignees(rid) if _may_assign(current_user) else []),
        "dismissed": [{"key": r["key"], "title": r["title"],
                       "until": dismissed[r["key"]]["expires_at"] if r["key"] in dismissed else None}
                      for r in dismissed_recs],
        "ai_insight": ai_insight,
        "changes": {"since": _iso_z(since_dt), "since_label": since_label, "items": changes[:8]},
        "alerts": alert_items[:8],
        "quick_actions": quick,
        "ask_suggestions": ask,
        "upcoming": upcoming[:5],
        # Contract K4: beside `total` (the improvements), what got worse, the
        # net, the dollars summed over measured days, the unpriced wins and
        # the sales lift (gross revenue, kept apart). A surface shows the net
        # when worsened.count > 0 (re-audit A29).
        "value": home_block(_vh, value_history),
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
    rs = _one_dict(conn, """SELECT COUNT(*) AS total, SUM(response_status IN ('posted','approved')) AS responded,
                              SUM(response_status='drafted') AS awaiting,
                              SUM(urgency='high' AND response_status NOT IN ('posted','approved','skipped') AND COALESCE(NULLIF(review_date,''), fetched_at) >= date('now','-30 days')) AS urgent,
                              ROUND(AVG(CASE WHEN COALESCE(NULLIF(review_date,''), fetched_at) >= date('now','-30 days') THEN rating END),1) AS avg30,
                              SUM(COALESCE(NULLIF(review_date,''), fetched_at) >= date('now','-30 days')) AS n30,
                              ROUND(AVG(CASE WHEN COALESCE(NULLIF(review_date,''), fetched_at) >= date('now','-60 days') AND COALESCE(NULLIF(review_date,''), fetched_at) < date('now','-30 days') THEN rating END),1) AS avg_prev,
                              SUM(rating<=2 AND COALESCE(NULLIF(review_date,''), fetched_at) >= date('now','-7 days')) AS low7
                       FROM reviews WHERE restaurant_id=? AND processed=1 AND deleted_at IS NULL""", (rid,)) or {}
    total = int(rs.get("total") or 0)
    rate = round(100.0 * int(rs.get("responded") or 0) / total) if total else None
    last_active = (_one_dict(conn, "SELECT MAX(created_at) AS t FROM login_history WHERE restaurant_id=?", (rid,)) or {}).get("t")
    labor = None
    cd = _one_dict(conn, "SELECT shifts_csv IS NOT NULL AND shifts_csv != '' AS live FROM client_data WHERE restaurant_id=?", (rid,)) or {}
    if r.get("module_labor") and cd.get("live"):
        try:
            from labor import analyse_shifts_for_restaurant
            la = analyse_shifts_for_restaurant(rid)
            if la.get("is_live"):
                from notify import labor_target_for as _labor_target_for
                target = _labor_target_for(r)
                # People over 40h THIS payroll week, not every overtime week
                # in the upload (the same read as the location's own Home).
                try:
                    from time_utils import restaurant_now as _rn_loc
                    _ot_loc = overtime_this_week(la, _rn_loc(get_restaurant(rid)).date())
                except Exception:
                    _ot_loc = None
                labor = {"pct": float(la.get("overall_labor_pct") or 0), "target": target,
                         "over": round(float(la.get("overall_labor_pct") or 0) - target, 1),
                         "overtime": (_ot_loc or {}).get("people", 0)}
        except Exception:
            labor = None
    inv = None
    if r.get("module_inventory"):
        try:
            from inventory import analysis_for
            items, live, a = analysis_for(rid)
            if live and items:
                inv = {"recoverable": float(a.get("recoverable_monthly") or 0), "critical_low": len(a.get("critical_low") or []), "waste_rate": a.get("waste_rate_pct")}
        except Exception:
            inv = None
    issues = []
    if sig["top_issue"]:
        issues.append({"severity": sig["health"] if sig["health"] != "healthy" else "watch", "text": sig["top_issue"], "module": "reviews"})
    from thresholds import LABOR_OVER_TARGET_PTS
    if labor and labor["over"] >= LABOR_OVER_TARGET_PTS:
        issues.append({"severity": "critical" if labor["over"] >= 6 else "important", "text": f"Labor {labor['pct']:.1f}% — {labor['over']:.1f} pts over target", "module": "labor"})
    if labor and labor["overtime"]:
        issues.append({"severity": "important", "text": f"{labor['overtime']} {'person' if labor['overtime'] == 1 else 'people'} over 40h this week", "module": "labor"})
    if inv and inv["critical_low"]:
        issues.append({"severity": "important", "text": f"{_plural(inv['critical_low'], 'item')} critically low", "module": "inventory"})
    avg30 = rs.get("avg30"); prev = rs.get("avg_prev")
    if avg30 and prev and (rs.get("n30") or 0) >= 3 and avg30 - prev <= -0.3:
        issues.append({"severity": "important", "text": f"Rating slipped to {avg30:.1f}★ (from {prev:.1f}★)", "module": "reviews"})
    # Accountability: issues nobody has picked up. Counted, not listed — the
    # portfolio row says WHERE follow-through is slipping; the issue list at
    # that location says what.
    try:
        oi = conn.execute(
            "SELECT SUM(status='open') AS open_n, SUM(status='acknowledged') AS ack_n, "
            "SUM(status='open' AND created_at <= datetime('now','-2 hours')) AS stale_n "
            "FROM ops_issues WHERE restaurant_id=?", (rid,)).fetchone()
        open_issues = {"open": int(oi["open_n"] or 0), "acknowledged": int(oi["ack_n"] or 0),
                       "stale": int(oi["stale_n"] or 0)}
    except Exception:
        open_issues = None
    if open_issues and open_issues["stale"]:
        issues.append({"severity": "important", "module": "issues",
                       "text": f"{_plural(open_issues['stale'], 'issue')} unacknowledged for 2h+"})
    rank = {"critical": 3, "important": 2, "watch": 1}
    worst = max((rank[i["severity"]] for i in issues), default=0)
    health = {3: "critical", 2: "important", 1: "watch", 0: "healthy"}[worst]
    issues.sort(key=lambda i: -rank[i["severity"]])
    return {"id": rid, "name": r.get("location_name") or r["name"], "restaurant_name": r["name"], "health": health,
            "issues": issues, "attention": len(issues), "open_issues": open_issues,
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
    from permissions import LOCATION_SWITCH as _LOC_SWITCH_G, has_permission as _has_perm_g
    if not _has_perm_g(current_user, _LOC_SWITCH_G):
        return {"ok": False, "error": "Only the owner login sees all locations"}, 403
    from models import get_location_group
    base = get_restaurant(current_user.get("base_restaurant_id") or current_user["restaurant_id"])
    if not base or not base.location_group:
        return {"ok": False, "error": "No location group on this account"}, 400
    now = datetime.now(timezone.utc)
    conn = get_conn()
    locs = [_location_record(conn, r, now) for r in get_location_group(base.location_group, owner_email=base.owner_email)]
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
        "ok": True, "scope": "group", "generated_at": _iso_z(now), "group_name": base.location_group,
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
    _cache_put(key, payload)
    return payload, 200
