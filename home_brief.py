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
            surface="home", role=None, _card=True, reason_code=None, require_existing=False):
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
    stored on the ledger answer beside the free `reason`.

    A setup or health nudge (HOME_SETUP_KEYS) is hidden on Home only — it
    is not a recommendation, so nothing is written to the ledger.
    `require_existing` (the route's answers, K2) never starts a ledger
    episode at the answer. The key is kept to the ledger's own 160
    characters: cut to 120 here, a long key's answer was filed under a key
    nobody was shown."""
    key = (key or "").strip()[:160]
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
        if key in HOME_SETUP_KEYS:
            pass                                 # Home's own hide; not an answer to advice
        elif kind == "snooze":
            until = (_dtl.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            smeta = {"until": until, "days": days}
            if code:
                smeta["reason_code"] = code
            rec_ledger.record(rid, key, "snoozed", surface=surface, user_id=user_id, role=role,
                              meta=smeta, snooze_until=until, require_existing=require_existing)
        else:
            meta = {"kind": _LEDGER_KIND[kind]}
            if reason:
                meta["reason"] = reason
            if code:
                meta["reason_code"] = code
            rec_ledger.record(rid, key, "completed" if kind == "done" else "dismissed", surface=surface,
                              user_id=user_id, role=role, meta=meta, silence_days=days,
                              require_existing=require_existing)
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
    n = conn.execute("DELETE FROM home_dismissals WHERE restaurant_id=? AND key IN (?, ?)",
                     (rid, (key or "").strip()[:160], (key or "").strip()[:120])).rowcount
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
    """Parse the timestamp styles the DB holds into an aware UTC datetime,
    through the one parser (time_utils.parse_stamp, CA3 F15): sqlite
    datetime('now') (space) is UTC, an offset or Z is that instant, a naive
    'T' stamp (Python isoformat from this server) is server-local, and a
    bare date is UTC midnight. A string that does not parse whole falls back
    to its leading date. None when unparseable."""
    if not v:
        return None
    from time_utils import parse_stamp
    s = str(v).strip()
    d = parse_stamp(s, naive_tz="UTC" if len(s) <= 10 else "local")
    if d is None and len(s) > 10:
        d = parse_stamp(s[:10], naive_tz="UTC")
    return d


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


def pos_health_label(state) -> str:
    """The provider's name as an owner reads it ("Toast", "RPOWER")."""
    p = (state or {}).get("provider") or "POS"
    return {"rpower": "RPOWER", "pos": "POS"}.get(p, p.title())


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
    import pos_health
    _pos = pos_health.pos_sync_state(r, now=now)
    if _pos["state"] == "error":
        # Any provider, RPOWER included (CA3 F6) — not Toast alone.
        issues.append(("critical", f"{pos_health_label(_pos)} sync failing"))
    # The registry's reviews rule (data_freshness.review_fetch_state), not a
    # 3-day cut of its own, and last_fetched_at read as the Chicago-local
    # stamp it is — _ts read it as server-local, hours off on a UTC host
    # (re-audit B3#9, #20; B6 low).
    import data_freshness
    _rv = data_freshness.review_fetch_state(r, now=now)
    if _rv["state"] == "stale":
        issues.append(("important", f"Reviews not refreshed since {_rv['as_of']}"))
    if (awaiting.get("n") or 0) > 0:
        issues.append(("watch", f"{_plural(awaiting['n'], 'reply', 'replies')} waiting for approval"))
    sev_rank = {"critical": 3, "important": 2, "watch": 1}
    worst = max((sev_rank[s] for s, _ in issues), default=0)
    health = {3: "critical", 2: "important", 1: "watch", 0: "healthy"}[worst]
    return {"id": rid, "name": r.get("location_name") or r["name"], "restaurant_name": r["name"],
            "health": health, "attention": len(issues), "top_issue": issues[0][1] if issues else None,
            "rating_30d": rating.get("r"), "reviews_30d": rating.get("n") or 0}


# ── the brief ────────────────────────────────────────────────────────────────

def build_home_brief(current_user, fresh=False, present=True):
    """Home's payload for this login. `present=False` builds it for a
    screen that does not render Home — Ask's opening fallback — and records
    nothing: what the owner has answered is still left out, but no card is
    logged as shown (re-audit C5 / K5). Such a build is never cached, so
    the next real Home load still presents what it shows."""
    rid = current_user["restaurant_id"]
    key = (rid, current_user.get("id"))
    if not fresh:
        hit = _CACHE.get(key)
        if hit and (datetime.now(timezone.utc) - hit[0]).total_seconds() < _CACHE_TTL:
            return hit[1], 200
    payload, status = _build(current_user) if present else _build(current_user, present=False)
    if status == 200 and present:
        _cache_put(key, payload)
    return payload, status


def _answered_only(restaurant_id, items, surface, user_id=None, db_path=None) -> dict:
    """present_many's answer without its write: {key: None} for a key an
    answer is silencing, a truthy placeholder otherwise. For a build that
    must filter what the owner answered and record nothing."""
    try:
        import rec_ledger
        silenced = rec_ledger.silenced_keys(restaurant_id)
    except Exception:
        silenced = set()
    return {it["key"]: (None if it["key"] in silenced else 0) for it in (items or []) if it.get("key")}


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


def diagnosis_evidence(dg, n, kind, basis, flags=(), coverage=None):
    """rec_trust.diagnosis_evidence — a stored diagnosis's evidence input.
    Home's cards read rec_trust.review_diagnosis_input /
    food_diagnosis_input now (group P); no caller remains — candidate for
    future cleanup after additional verification."""
    import rec_trust
    return rec_trust.diagnosis_evidence(dg, n, kind, basis, flags=flags, coverage=coverage)


def _mkt_evidence(rid) -> dict:
    """client_api.marketing_read_evidence — posts with measured performance
    in the last 8 weeks. Never raises."""
    try:
        import client_api
        return client_api.marketing_read_evidence(rid)
    except Exception as e:
        print(f"[home] marketing evidence unavailable for {rid}: {e}")
        return {"n": None, "basis": "the posts could not be read"}


def card_confidence(ctx, key, evidence, sources=()):
    """ONE confidence per card or attention item: the measured
    Recommendation Confidence (rec_trust.assess, contract K1) — Evidence
    Strength from what stands behind the card, Historical Accuracy from
    this restaurant's measured record of the kind, Data Freshness from the
    sources it rests on. It replaced intelligence.confidence.card_confidence
    and its stand-in 0.3/0.55/0.8 score (confidence audit, 9/24/26). Never
    raises; never fails the brief."""
    import rec_trust
    return rec_trust.assess(ctx.rid, key, evidence=evidence, sources=sources, ctx=ctx)


# Review attention thresholds, the same size both ways (CA1 H7): a 30-day
# rating that moved RATING_MOVE_STARS against the 30 days before, each side
# resting on at least REVIEW_MOVE_MIN_N reviews; a negative share that moved
# NEGATIVE_SHARE_MOVE between the last two weeks and the two before, each
# side at least REVIEW_MOVE_MIN_N reviews and the moving side at least two
# negative reviews. Below the floors, nothing is said either way.
RATING_MOVE_STARS = 0.3
NEGATIVE_SHARE_MOVE = 0.15
REVIEW_MOVE_MIN_N = 3
# A weekday is "the heavy day" only when its gap over the other days
# exceeds TRIM_GAP_MIN_PTS and TRIM_GAP_SIGMA standard deviations of the
# other days' labor % (the worst of seven is biased upward — CA2 #14).
TRIM_GAP_MIN_PTS = 4.0
TRIM_GAP_SIGMA = 1.64
# The trim card's monthly dollars need the weekday seen at least this many
# times (one day is not a rate) and the period at labor.MIN_DAYS_TO_EXTRAPOLATE.
TRIM_MIN_WEEKDAYS_FOR_DOLLARS = 2


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


def _conf_weight(conf) -> float:
    """The confidence discount on a card's rank: 0.6 at 0%, 1.0 at 100%, from
    the measured percentage (K1); the band word only for an object without
    one; 0.85 — neutral — when the card carries no confidence at all."""
    if not isinstance(conf, dict):
        return 0.85
    pct = conf.get("pct")
    if pct is not None:
        try:
            return 0.6 + 0.4 * max(0.0, min(100.0, float(pct))) / 100.0
        except (TypeError, ValueError):
            pass
    if conf.get("version"):
        return _CONF_WEIGHT["low"]       # measured, and not measurable: low
    return _CONF_WEIGHT.get(conf.get("band"), 0.85)


def rank_score(rec) -> float:
    dollars = rec.get("dollars_monthly")
    return round(_URGENCY.get(rec.get("timeframe"), 1.0)
                 * max(float(dollars or 0), _UNPRICED)
                 * _EASE.get(rec.get("effort"), 0.8)
                 * _conf_weight(rec.get("confidence")), 2)


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
    """The food drivers' monthly dollars with each ingredient counted ONCE —
    food_cost_intelligence.deduplicated_total, the one definition.

    cost_drivers can name one ingredient several times — its waste, its
    price rise, its usage over recipe — each measured from the same spend.
    This used to keep the largest driver per item NAME, while the Food Cost
    card grouped by a menu driver's ingredients, so Home read "At stake
    $750/mo" beside "$400/month across 3 drivers" for the same drivers
    (NS3 H3, R11). Now both read the same function."""
    import food_cost_intelligence as _fci
    drv = [dict(d, dollars_monthly=float(d.get("dollars_monthly") or 0)) for d in (drivers or [])]
    if not drv:
        return 0.0
    return round(float(_fci.deduplicated_total(drv)["total"]), 2)


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
# Home's setup and health nudges — the product's own state (a connection
# missing or failing, data gone stale, a post that failed to publish), not
# advice an owner takes or declines. They are never presented to the ledger
# (every one expired as "ignored" and taught the rankers the owner ignores
# advice), carry answerable=false, and a hide on one is Home's own
# (home_dismissals), never a ledger answer (re-audit B7). Value: the module
# whose view permission may hide it.
HOME_SETUP_KEYS = {"google_not_connected": "reviews", "reviews_stale": "reviews", "toast_sync": "labor",
                   "pos_sync": "labor",
                   "inventory_stale": "food", "post_failed": "marketing", "social_not_connected": "marketing"}
# Attention items and cards that state a FACT about what is on file — the
# setup and health nudges, reviews and drafts waiting, a response rate, the
# items below par, the people over 40 hours — not advice whose support can
# be weighed. They carry no confidence at all (B4 H5, B1 C1, B6 low): a
# "70% confidence" on "3 drafts waiting" told the owner nothing and invited
# them to doubt a fact. Only recommendations and measured findings get one.
HOME_FACT_KEYS = frozenset(set(HOME_SETUP_KEYS) | {
    "urgent_reviews", "stale_low_reviews", "awaiting_approval", "low_response_rate", "critical_low", "overtime",
    "publish_drafts"})


def attention_answerable(a) -> bool:
    """Whether a Needs-attention item is a recommendation the owner can
    answer from Home: dismissable (a critical item never is), not a setup or
    health nudge, and a key the ledger may present (rec_delivery)."""
    import rec_delivery
    return bool(a.get("dismissable")) and a.get("key") not in HOME_SETUP_KEYS \
        and rec_delivery.answerable(a.get("rec_key") or a.get("key"))
# What the clients render (web renderFocus/renderAttention/renderRecs, iOS
# HomeActionDeck/HomeRecommendations): at most this many of each.
HOME_ATTENTION_SHOWN = 4
HOME_RECS_SHOWN = 3

# Where each attention item's button lands (nav.py). "Reply now" used to
# open the whole inbox unfiltered and "See the list" the top of Food Cost
# (Friction audit #2): the item names its filter or section now, and an item
# not listed here opens its module.
ATTENTION_NAV = {
    "urgent_reviews": "reviews?filter=urgent", "stale_low_reviews": "reviews?filter=pending",
    "awaiting_approval": "reviews?filter=pending", "low_response_rate": "reviews?filter=pending",
    "google_not_connected": "account/integrations", "reviews_stale": "account/integrations",
    "pos_sync": "account/integrations", "social_not_connected": "account/integrations",
    "overtime": "labor/schedule", "inventory_stale": "inventory/count", "critical_low": "inventory/order",
}
# The same for the header's quick actions (quick_actions[].nav).
QUICK_NAV = {
    "publish": "reviews?filter=pending", "urgent": "reviews?filter=urgent", "ask": "ask",
    "schedule": "labor/schedule", "order": "inventory/order", "post": "marketing",
}


def attention_nav(key, module) -> str:
    """The nav path an attention item (or anything keyed like one) opens."""
    import nav as _nav
    if key in ATTENTION_NAV:
        return ATTENTION_NAV[key]
    return _nav.path({"competitor": "intel", "food": "inventory"}.get(module, module or "home"))


def ledger_key(key):
    return LEDGER_KEY.get(key, key)


def signature_of(key, title):
    """What a Home item is about (insight_store.advice_signature), or None.
    Never raises: an unreadable item is simply not matched across surfaces."""
    try:
        import insight_store
        return insight_store.advice_signature(key, title)
    except Exception:
        return None


def home_declined_signatures(rid) -> set:
    """Every advice signature this restaurant said "not for us" to on any
    surface (insight_store.declined_signatures). Empty on failure: a read
    that fails shows the item rather than hiding advice nobody declined."""
    try:
        import insight_store
        return insight_store.declined_signatures(rid)
    except Exception as e:
        print(f"[home] declined signatures unavailable for {rid}: {e}")
        return set()


def other_days_mean(dow, day) -> float:
    """The mean labor % of every weekday but `day` — what "N pts above your
    other days" is measured against. A mean that included the day itself
    understated the gap it names. 0 when there is nothing to compare."""
    vals = [float(v) for k, v in (dow or {}).items() if v and k != day]
    return sum(vals) / len(vals) if vals else 0


def home_freshness(ctx, active_keys, labor_live, inv_live, google_connected=False, reviews_on_file=0) -> list:
    """Home's freshness strip (contract K4): one entry per module and data
    source — {module, source, state, pct, as_of, as_of_iso, basis} — from
    data_freshness, plus the legacy key/label/at/note. A module on sample
    data says so and is never dated; a source that does not apply (a POS not
    connected) is left out, except that a module with no source at all says
    what is missing."""
    import data_freshness
    out = []

    def entry(module, label, st):
        out.append({"module": module, "source": st.get("key"), "state": st.get("state"), "pct": st.get("pct"),
                    "as_of": st.get("as_of"), "as_of_iso": st.get("as_of_iso"), "basis": st.get("basis"),
                    "error": st.get("error"), "last_ok_at": st.get("last_ok_at"),
                    "key": module, "label": label, "at": st.get("as_of_iso"), "note": st.get("basis")})

    def sample(module, label, basis):
        out.append({"module": module, "source": None, "state": "sample", "pct": None, "as_of": None,
                    "as_of_iso": None, "basis": basis, "error": None,
                    "key": module, "label": label, "at": None, "note": basis})

    plan = []
    if "reviews" in active_keys:
        plan.append(("reviews", "Reviews", ("reviews",), True))
    if "labor" in active_keys:
        plan.append(("labor", "Labor", data_freshness.MODULE_SOURCES["labor"], labor_live))
    if "inventory" in active_keys:
        plan.append(("inventory", "Food cost", data_freshness.MODULE_SOURCES["inventory"], inv_live))
    if "marketing" in active_keys:
        plan.append(("marketing", "Marketing", data_freshness.MODULE_SOURCES["marketing"], True))
    if "intel" in active_keys:
        plan.append(("intel", "Intel", data_freshness.MODULE_SOURCES["intel"], True))
    for module, label, keys, live in plan:
        if not live:
            sample(module, label, "sample data — upload shifts or connect your POS" if module == "labor"
                   else "sample data — add a count or connect your POS")
            continue
        states = ctx.sources(keys)
        shown = [st for st in states if st.get("state") != "not_connected"]
        if not shown:
            st = dict(states[0]) if states else {"key": None, "state": "not_connected", "pct": None}
            if module == "reviews" and not google_connected and reviews_on_file:
                st["basis"] = "imported reviews — Google not connected"
            shown = [st]
        for st in shown:
            entry(module, label, st)
    return out


def _data_health_compact(rid, restaurant=None):
    """data_health.compact(snapshot) for Home, or None — never raises, and
    a Home build never fails because the score could not be read."""
    try:
        import data_health
        return data_health.compact(data_health.snapshot(rid, restaurant=restaurant))
    except Exception as e:
        print(f"[home] data health unavailable for {rid}: {e}")
        return None


def stalest_as_of(freshness):
    """M/D/YY of the oldest date among the dated sources, or None."""
    dated = [f for f in (freshness or []) if f.get("pct") is not None and f.get("as_of_iso")]
    if not dated:
        return None
    return min(dated, key=lambda f: f["as_of_iso"]).get("as_of")


def trim_day_read(dow, by_day, labor_target, period_days):
    """The trim-day card's read, or None (CA2 #14, CA4 F11).

    The heaviest weekday is named only when its gap over the other days
    exceeds max(TRIM_GAP_MIN_PTS, TRIM_GAP_SIGMA × the standard deviation of
    the other days' labor %) — the worst of seven averages is biased upward,
    so "4 points worse" means little where weekdays already swing by 5 — and
    it runs over the owner's target.

    Its monthly dollars are that day's labor cost above the target on the
    days that carry sales (a day with no sales figure has no excess to
    measure), averaged per day × 52 ÷ 12 — and only once the period reaches
    labor.MIN_DAYS_TO_EXTRAPOLATE and the weekday was seen
    TRIM_MIN_WEEKDAYS_FOR_DOLLARS times; below either there is no monthly
    figure, only the gap. Returns {day, pct, others_mean, gap, threshold,
    n_days, monthly, basis}."""
    from intelligence.stats import sd as _sd
    from labor import MIN_DAYS_TO_EXTRAPOLATE
    vals = {k: float(v) for k, v in (dow or {}).items() if v}
    if len(vals) < 2:
        return None
    worst_day, pct = max(vals.items(), key=lambda kv: kv[1])
    day = worst_day
    others = [v for k, v in vals.items() if k != day]
    # Against the OTHER days only (H-26): a mean that included the day
    # itself understated the gap it names.
    mean = other_days_mean(dow, worst_day)
    spread = _sd(others)
    threshold = max(TRIM_GAP_MIN_PTS, TRIM_GAP_SIGMA * spread) if spread is not None else TRIM_GAP_MIN_PTS
    gap = pct - mean
    if gap < threshold or pct <= float(labor_target):
        return None
    excess, n_days = 0.0, 0
    for dstr, dd in (by_day or {}).items():
        try:
            if datetime.strptime(dstr, "%Y-%m-%d").strftime("%A") != day:
                continue
        except (TypeError, ValueError):
            continue
        sales = dd.get("sales")
        if sales is None or float(sales) <= 0:
            continue
        n_days += 1
        excess += max(0.0, float(dd.get("labor_cost") or 0) - float(sales) * float(labor_target) / 100.0)
    monthly, basis = None, None
    if n_days >= TRIM_MIN_WEEKDAYS_FOR_DOLLARS and int(period_days or 0) >= MIN_DAYS_TO_EXTRAPOLATE:
        from metrics import WEEKS_PER_MONTH as _WPM   # one month definition (NS3 L5)
        monthly = round(excess / n_days * _WPM, 2) or None
        if monthly:
            basis = (f"{day}s only: their labor above your {float(labor_target):g}% target, averaged over "
                     f"{n_days} {day}s with sales, × 52 ÷ 12")
    return {"day": day, "pct": pct, "others_mean": mean, "gap": round(gap, 1), "threshold": round(threshold, 1),
            "n_days": n_days, "monthly": monthly, "basis": basis}


def trim_metric(day) -> str:
    """The metric a trim-day card is tracked on: the weekday's own labor %
    when metrics registers one (the outcome group's weekday labor metric),
    else overall labor % — never a metric metrics cannot measure."""
    try:
        import metrics
        reg = getattr(metrics, "_REGISTRY", {}) or {}
        for name in ("weekday_labor_pct", "labor_pct_weekday"):
            if name in reg:
                return f"{name}:{day}"
    except Exception:
        pass
    return "labor_pct"


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


# How many critically-low items the attention card names.
CRITICAL_LOW_NAMED = 4


# The peer ledger's metric families, as Home names them: the module the
# "How you compare" card for them lives on, and what an owner calls them.
_RUNG_FAMILY = {"format": (("reviews", "marketing"), "reviews and marketing"),
                "labor": (("labor",), "labor"),
                "food": (("inventory",), "food cost")}
# Only a real comparison is announced: a published figure is context, and
# "your own normal" was always there.
_RUNG_WHOM = {"peers": "restaurants like yours", "platform": "every restaurant on Cavnar AI"}


def comparison_changes(events, active_keys) -> list:
    """Home's "new comparison available" lines (Benchmarking #44): one per
    metric family that reached a peer or platform comparison since the last
    visit, for a module this login can see — {text, tone, module, at}."""
    import json as _json
    out, seen = [], set()
    for ev in events or ():
        try:
            d = _json.loads(ev.get("event_data") or "{}")
        except (TypeError, ValueError):
            continue
        fam, to = d.get("family"), d.get("to")
        if fam in seen or fam not in _RUNG_FAMILY or to not in _RUNG_WHOM:
            continue
        mods, what = _RUNG_FAMILY[fam]
        mod = next((m for m in mods if m in (active_keys or ())), None)
        if not mod:
            continue
        seen.add(fam)
        out.append({"text": f"New comparison available: your {what} figures are now compared with {_RUNG_WHOM[to]}",
                    "tone": "good", "module": mod, "at": ev.get("created_at")})
    return out


def _build(current_user, present=True):
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
    # "your 30% target", or "Cavnar's starting target of 30%" when nobody set
    # it (Benchmarking audit #13) — thresholds.target_for, the one target
    # read (re-audit #10): on a starting target an overage is a watch.
    import thresholds as _thr_tgt
    _labor_tgt_for = _thr_tgt.target_for(r, "labor")
    labor_target = _labor_tgt_for["pct"]
    _labor_tgt = _labor_tgt_for["phrase"]
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
    # A comparison that became available since the last visit — the peer
    # ledger's `benchmark_rung_up` (intelligence.jobs.record_assignments),
    # which nothing read (Benchmarking #44, R2-19).
    rung_ups = _rows_dict(conn, "SELECT event_data, created_at FROM activity_log WHERE restaurant_id=? AND "
                                "event_type='benchmark_rung_up' AND created_at >= ? ORDER BY id DESC LIMIT 6",
                          (rid, since_sql))
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

    # What every card's confidence reads once for this build: the ledger,
    # each source's freshness (labor dated by its own analysis), each kind's
    # measured record (rec_trust.Context).
    import rec_trust
    import data_freshness
    trust_ctx = rec_trust.Context(rid, restaurant=restaurant,
                                  freshness_context={"labor": labor if labor_live else None})

    def sources_of(module, extra=()):
        return tuple(dict.fromkeys(data_freshness.sources_for([module]) + tuple(extra or ())))

    def add_attn(key, severity, title, detail, module, action_label, action="open_module", since=None, evidence=None,
                 rec_key=None, ev=None, sources=None, **extra):
        """Every attention item carries the `evidence` line it rests on. A
        RECOMMENDATION or a measured finding carries ONE measured confidence
        (K4) from `ev`, its Evidence Strength input (confidence_engine.
        evidence). A FACT (HOME_FACT_KEYS — a setup or health nudge, a
        failing sync, reviews or drafts waiting, a critical count) carries
        none: `confidence` is None, never a default "1 row counted" that read
        a permanent 70% (B4 H5, B1 C1)."""
        conf = None
        if ev is not None and key not in HOME_FACT_KEYS:
            srcs = sources if sources is not None else sources_of(module)
            conf = card_confidence(trust_ctx, rec_key or ledger_key(key), ev, srcs)
        item = {"key": key, "rec_key": rec_key, "severity": severity, "title": title, "detail": detail, "module": module,
                "action": {"label": action_label, "kind": action, "module": module,
                           "nav": attention_nav(key, module)},
                "since": since, "evidence": evidence, "location": restaurant.location_name or None,
                "confidence": conf}
        item.update(extra)
        attention.append(item)

    def add_win(key, title, detail, module):
        wins.append({"key": key, "title": title, "detail": detail, "module": module})

    def add_rec(key, title, why, evidence, impact, module, timeframe,
                action_label=None, metric=None, *, ev=None, sources=None, dollars=None, dollars_basis=None,
                if_ignored=None, effort=None, alternative=None, action=None, evidence_sources=None,
                model_written=False):
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
        # figure — with `dollars_basis` saying its scope), how sure
        # (`confidence`: ONE measured Recommendation Confidence, K1 — the
        # old `strength` pill is gone, CA4 F1), and what happens if it is
        # ignored (`if_ignored`).
        recs.append({"key": key, "title": title, "why": why, "evidence": evidence, "impact": impact, "module": module,
                     "timeframe": timeframe, "metric": metric,
                     "action_label": action_label or "Open " + {"inventory": "Food Cost"}.get(module, module.title()),
                     "dollars_monthly": round(float(dollars), 2) if dollars else None,
                     "dollars_basis": dollars_basis if dollars else None,
                     "if_ignored": if_ignored, "effort": effort, "alternative": alternative,
                     # A one-tap finish when there is one (a reprice at the
                     # suggested price), else None and the card opens its module.
                     "action": action,
                     "evidence_sources": evidence_sources or [module], "model_written": bool(model_written),
                     # A card that acts on a fact (the drafts waiting)
                     # carries no confidence (HOME_FACT_KEYS, B4 H5).
                     "confidence": (None if key in HOME_FACT_KEYS else
                                    card_confidence(trust_ctx, key, ev,
                                                    sources if sources is not None else sources_of(module)))})

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
        # last_fetched_at is Chicago local with a 'T' (models); _ts read it
        # as server-local time. admin_ops.fetched_at_ct reads both styles.
        try:
            import admin_ops as _ao_hb
            _fat = _ao_hb.fetched_at_ct(r.get("last_fetched_at"))
            last_fetch_age = (now - _fat.astimezone(timezone.utc)).total_seconds() / 86400.0 if _fat else None
        except Exception:
            last_fetch_age = _age_days(r.get("last_fetched_at"), now)
        # Places-only reviews are a sample (five at a time), and say so in
        # the evidence behind every review card (CA3 F13).
        # One helper for every review surface (re-audit B3#7, B4 M1).
        _rv_flags = data_freshness.review_evidence_flags(r)
        _rv_src = ("reviews",) if google_connected else ()

        # attention
        if urgent:
            add_attn("urgent_reviews", "critical", f"{_plural(urgent, 'urgent review')} unanswered",
                     "Low-star reviews mentioning something serious are still waiting on a reply.", "reviews", "Reply now",
                     evidence=f"{urgent} of {total} reviews flagged urgent")
        if (stale_unanswered.get("n") or 0) > 0 and not urgent:
            n = stale_unanswered["n"]
            add_attn("stale_low_reviews", "important", f"{_plural(n, 'low-star review')} unanswered for 2+ days",
                     "Guests read how you respond. A 48-hour reply keeps the thread on your side.", "reviews", "Answer them",
                     since="48h+", evidence=f"{n} reviews at 3 stars or below, unanswered 48h+")
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
        # Stale by the registry's reviews rule — the same reading that caps
        # every review card's Data Freshness — not a 3-day cut of Home's own
        # (re-audit B3#9).
        _rv_fetch = data_freshness.review_fetch_state(r, now=now)
        if google_connected and _rv_fetch["state"] == "stale" and last_fetch_age is not None:
            add_attn("reviews_stale", "important", f"Reviews haven't refreshed since {_rv_fetch['as_of']}",
                     "The Google connection may need re-authorising. Numbers below are as of the last pull.", "account", "Check connection",
                     since=f"{int(last_fetch_age)}d")
        # trend: negative share last 2 weeks vs prior 2
        if len(sentiment) >= 4:
            last2 = sentiment[-2:]; prior2 = sentiment[-4:-2]
            ln = sum(w["negative"] for w in last2); lt = sum(w["total"] for w in last2)
            pn = sum(w["negative"] for w in prior2); pt = sum(w["total"] for w in prior2)
            if lt >= REVIEW_MOVE_MIN_N and pt >= REVIEW_MOVE_MIN_N:
                lshare = ln / lt; pshare = pn / pt
                if lshare - pshare >= NEGATIVE_SHARE_MOVE and ln >= 2:
                    # Only a classified theme is named: with none on file
                    # the line says nothing about a theme (CA4 F15 — it
                    # used to say "Service").
                    _theme = (f" {top_issues[0]['label']} is the most-mentioned theme." if top_issues else "")
                    # Its own key (T4, B4 M11): "negative_trend" is the
                    # 8-week rating-slope ALERT (notify), a different
                    # finding — under one key a "Not for us" on either
                    # silenced both for ten years.
                    add_attn("negative_share", "important", f"Negative reviews up to {lshare:.0%} of the last two weeks",
                             f"Up from {pshare:.0%} the two weeks before.{_theme}", "reviews", "See what changed",
                             since="2 weeks", evidence=f"{ln} of {lt} negative vs {pn} of {pt}",
                             ev={"n": min(lt, pt), "kind": "reviews", "flags": _rv_flags,
                                 "basis": f"{lt} reviews in the last two weeks against {pt} the two before"},
                             sources=_rv_src)
                elif pshare - lshare >= NEGATIVE_SHARE_MOVE and pn >= 2:
                    add_win("sentiment_improving", "Fewer negative reviews", f"{lshare:.0%} of the last two weeks vs {pshare:.0%} before.", "reviews")
        # rating movement — the same threshold both ways (CA1 H7)
        rating_delta = (_pct_delta(avg30, prev_avg)
                        if (n30 >= REVIEW_MOVE_MIN_N and prev_n >= REVIEW_MOVE_MIN_N) else None)
        if rating_delta is not None and rating_delta <= -RATING_MOVE_STARS:
            add_attn("rating_drop", "important", f"30-day rating slipped to {avg30:.1f}★",
                     f"Down from {prev_avg:.1f}★ the previous 30 days across {n30} reviews.", "reviews", "Open reviews",
                     since="30d", evidence=f"{n30} reviews this 30 days against {prev_n} the 30 before",
                     ev={"n": min(n30, prev_n), "kind": "reviews", "flags": _rv_flags,
                         "basis": f"{n30} reviews in the last 30 days against {prev_n} the 30 before"},
                     sources=_rv_src)
        elif rating_delta is not None and rating_delta >= RATING_MOVE_STARS:
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
        elif rating_delta is not None and rating_delta <= -RATING_MOVE_STARS:
            interp = (f"Rating is slipping — {top_issues[0]['label'].lower()} complaints are the theme."
                      if top_issues else "Rating is slipping.")
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
                         "state": "bad" if urgent else ("warn" if (awaiting or (rating_delta is not None and rating_delta <= -RATING_MOVE_STARS) or (total >= 5 and rate < 50)) else ("neutral" if total == 0 else "good")),
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
                    "Reputation · response rate", "reviews", "Today", "Publish now",
                    metric=None,
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
                if dg:
                    # Evidence is the count of reviews behind it; the model's
                    # own rating only LOWERS it, never raises it (E3, CA4 F2):
                    # "High confidence — a diagnosis read from 3 reviews" is
                    # no longer possible.
                    # The Reviews diagnosis card's own key ("diag_review:<cat>"),
                    # so "Not for us" on either one holds on both (M-9).
                    from client_api import diagnosis_rec_key as _drk_rv
                    add_rec(_drk_rv("diag_review", dg) or f"top_issue:{cat}",
                            str(dg["recommended_action"]).strip().rstrip("."),
                            (dg.get("cause") or f"{lbl} is the most-mentioned complaint over 90 days.").strip(),
                            f"{lbl} raised in {cnt} reviews over 90 days"
                            + ((" · " + (dg.get("stale_note") or "diagnosis older than a week").rstrip("."))
                               if dg.get("stale") else ""),
                            "Reviews · rating", "reviews", "This week",
                            "See the reviews", metric=f"complaints:{cat}",
                            # THE review diagnosis input — the Reviews tab's
                            # and the hero's too, so one diagnosis shows one
                            # figure (B1 H3, B4 M1): its mention_count, not
                            # this card's top-issue count.
                            ev=rec_trust.review_diagnosis_input(dg, r),
                            sources=_rv_src,
                            if_ignored=f"{lbl.lower()} stays the most-mentioned complaint",
                            effort="medium", alternative=dg.get("alternative_cause"), model_written=True)
                else:
                    add_rec(f"top_issue:{cat}", f"Read the {cnt} {lbl.lower()} complaints and pick one fix for this week",
                            f"{lbl} is the most-mentioned complaint in the last 90 days.",
                            f"{lbl} raised in {cnt} reviews over 90 days", "Reviews · rating", "reviews", "This week",
                            "See the reviews",
                            metric=f"complaints:{cat}",
                            ev={"n": cnt, "kind": "reviews", "basis": f"{cnt} reviews in 90 days", "flags": _rv_flags},
                            sources=_rv_src,
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
            brief_lines.append({"text": f"Rating {avg30:.1f}★ over the last 30 days" + (f", {rating_delta:+.1f} vs the month before" if rating_delta is not None else "") + f" · {_plural(n30, 'new review')}.", "tone": "bad" if (rating_delta is not None and rating_delta <= -RATING_MOVE_STARS) else ("good" if (rating_delta or 0) >= RATING_MOVE_STARS else "neutral"), "module": "reviews"})
        if total:
            ask.append("What changed in my reviews this month?")
        if top_issues:
            ask.append(f"What are guests saying about {top_issues[0]['label'].lower()}?")

    # ── POS sync (any provider) ─────────────────────────────────────────────
    # pos_health reads every provider by name — RPOWER included, which no
    # Home surface read before (CA3 F6). A failing sync is critical: labor,
    # sales and depletion stop updating under every card.
    if "labor" in active_keys or "inventory" in active_keys:
        import pos_health
        _pos_state = pos_health.pos_sync_state(r, now=now)
        if _pos_state["state"] == "error":
            _pname = pos_health_label(_pos_state)
            add_attn("pos_sync", "critical", f"{_pname} sync is failing",
                     f"Last error: {str(_pos_state.get('error') or '')[:120]}. Labor, sales and depletion numbers "
                     "stop updating until it's fixed.", "account", "Fix connection",
                     evidence=(f"last good sync {_mdy(_pos_state['last_synced_iso'])}"
                               if _pos_state.get("last_synced_iso") else "no successful sync on file"),
                     provider=_pos_state.get("provider"))
        else:
            # A sync that simply stopped — or keeps running while its sales
            # stopped arriving — has no error column, and only an amber pill
            # said so (DH4-2). The registry dates the POS by its last day
            # carrying sales; stale (or failing in the sync ledger) is a card.
            _pos_reg = (trust_ctx.sources(("pos",)) or [{}])[0]
            if _pos_reg.get("state") == "stale" or (_pos_reg.get("error") and _pos_reg.get("state") != "not_connected"):
                _pname = pos_health_label(_pos_state)
                add_attn("pos_sync", "critical" if _pos_reg.get("error") else "important",
                         f"{_pname} data has stopped arriving",
                         f"{_pos_reg.get('basis') or 'The POS data is out of date'}. Labor, sales and depletion "
                         "numbers are held at their last good day until it syncs again.",
                         "account", "Check connection",
                         evidence=(f"sales through {_pos_reg['as_of']}" if _pos_reg.get("as_of")
                                   else "no sales on file"),
                         provider=_pos_state.get("provider"))

    # ── Labor ───────────────────────────────────────────────────────────────
    if "labor" in active_keys:
        if not labor_live or not labor:
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
            # The analysis's own partial-data flags (CA3 F4): how many of
            # its shift days carry sales is the coverage every labor claim
            # rests on, and each flag weakens the evidence.
            from labor import MIN_DAYS_TO_EXTRAPOLATE as _MIN_DAYS
            period_days = int(labor.get("period_days") or days or 0)
            _missing = len(labor.get("days_missing_sales") or [])
            _with_sales = max(0, days - _missing)
            _lab_cov = (_with_sales / float(days)) if days else None
            _lab_flags = tuple(f for f, on in (("days_missing_sales", _missing),
                                               ("hours_are_estimated", labor.get("hours_are_estimated")),
                                               ("days_with_conflicting_sales", labor.get("days_with_conflicting_sales")),
                                               ("period_too_short", period_days < _MIN_DAYS)) if on)
            _cover_note = f"{_with_sales} of {days} days carry sales" if _missing else ""
            # The floors every labor claim on this page shares (re-audit
            # B3#8, B4 M4): the attention card, the SMS alert and the win
            # need MIN_DAYS_TO_EXTRAPOLATE days of shifts, and the tile,
            # brief line and win need the labor data's own freshness — one
            # day of shifts said "5.8 pts over target" in the tile and "Labor
            # 35.8% against a 30% target" in the brief, and a green "under
            # target" win could stand on a month-old file.
            import confidence_engine as _ce_lab
            _lab_srcs = trust_ctx.sources(sources_of("labor"))
            _lab_fr = _ce_lab.freshness(_lab_srcs)
            _lab_stalest = next((s for s in _lab_srcs if s.get("key") == _lab_fr.get("stalest")), None) or {}
            _lab_short = period_days < _MIN_DAYS
            _lab_stale = _lab_fr.get("pct") is not None and _lab_fr["pct"] < _ce_lab.STALE_BELOW
            _lab_readable = not _lab_short and not _lab_stale
            if over >= LABOR_OVER_TARGET_PTS and period_days >= _MIN_DAYS:
                # Never from a cold start: one day of shifts is not "labor
                # over target" (CA3 F5) — labor.MIN_DAYS_TO_EXTRAPOLATE.
                add_attn("labor_over", ("watch" if not _labor_tgt_for["alerts_allowed"]
                                        else ("important" if over < 6 else "critical")),
                         f"Labor at {pct:.1f}% — {over:.1f} pts over {_labor_tgt}",
                         f"Across the last {days} days of shifts" + (f" ({_cover_note})" if _cover_note else "")
                         # The figure is the WHOLE schedule's weekly gap, so the
                         # sentence says so: it read "recoverable by trimming the
                         # overstaffed days" over $975/week when those days carried
                         # $600 (NS3 M10). An opportunity, never money saved.
                         + (f"; about ${savings:,.0f}/week above target across the whole schedule — an opportunity, not money saved." if savings > 0 else "."), "labor", "Open labor",
                         since=f"{days}d",
                         # Labor on the days with sales, the pair of total_sales (NS3 H4).
                         evidence=f"${float(labor.get('costed_labor', labor.get('total_labor_cost')) or 0):,.0f} labor on ${float(labor.get('total_sales') or 0):,.0f} sales"
                         + (f" · {_cover_note}" if _cover_note else ""),
                         # The labor alert's key: its latest period.
                         rec_key=(f"labor_over:{str(labor_hist[0].get('period_start') or '')[:10]}"
                                  if labor_hist and labor_hist[0].get("period_start") else "labor_over"),
                         ev={"n": _with_sales, "kind": "trading_days", "coverage": _lab_cov, "flags": _lab_flags,
                             "basis": f"{_with_sales} days of shifts with sales"
                                      + (f" of {days}" if _missing else "")},
                         dollars_weekly=(round(savings, 2) if savings > 0 else None),
                         dollars_basis=("a week of the whole schedule's gap to your target, all shifts in the period"
                                        if savings > 0 else None))
            elif over <= 0 and _lab_readable:
                add_win("labor_on_target", f"Labor at {pct:.1f}% — under target", f"{abs(over):.1f} pts under {_labor_tgt} over {days} days.", "labor")
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
                trim = trim_day_read(dow, labor.get("by_day") or {}, labor_target, period_days)
                if trim:
                    worst_day, worst_pct, mean = trim["day"], trim["pct"], trim["others_mean"]
                    n_days = trim["n_days"]
                    add_rec(f"trim_day:{worst_day}", f"Trim {worst_day} staffing on the next schedule",
                            f"{worst_day} runs {worst_pct - mean:.0f} pts above your other days without the sales to justify it.",
                            f"{worst_day} labor {worst_pct:.1f}% vs {mean:.1f}% on your other days · target {labor_target:.0f}%"
                            + (f" · {_cover_note}" if _cover_note else ""),
                            "Labor · weekly cost", "labor", "Next schedule", "Rebuild the schedule",
                            # The weekday's own labor % when metrics can
                            # measure it, not overall labor % — any labor
                            # move was credited to this card (CA2 #14).
                            metric=trim_metric(worst_day), dollars=trim["monthly"],
                            dollars_basis=trim["basis"],
                            ev={"n": n_days, "kind": "weekdays", "coverage": _lab_cov, "flags": _lab_flags,
                                "basis": f"{_plural(n_days, worst_day)} with sales in your shift data"},
                            if_ignored=f"{worst_day}s keep running about {worst_pct - labor_target:.0f} pts over {_labor_tgt_for['label']}",
                            effort="medium")
            if delta is not None and delta >= 1.5:
                add_change(f"Labor % rose {delta:+.1f} pts vs the previous period ({pct:.1f}%)", "bad", "labor")
            elif delta is not None and delta <= -1.5:
                add_change(f"Labor % fell {delta:+.1f} pts vs the previous period ({pct:.1f}%)", "good", "labor")
                add_win("labor_improving", f"Labor down {abs(delta):.1f} pts", f"{pct:.1f}% this period vs {prev_pct:.1f}% before.", "labor")
            if last_schedule and _ts(last_schedule.get("generated_at")) and _ts(last_schedule["generated_at"]) >= since_dt:
                hs = float(last_schedule.get("hours_scheduled") or 0); hb = float(last_schedule.get("hours_budget") or 0)
                add_change(f"New schedule built for {_mdy(last_schedule.get('week_start')) or 'next week'}" + (f" · {int(round(hb - hs))} hrs under budget" if hb and hs and hs < hb else ""), "good", "labor", last_schedule.get("generated_at"))
            _lab_interp = ((f"{over:.1f} pts over target. " + (f"{max(dow.items(), key=lambda kv: kv[1] or 0)[0]} is the heaviest day." if dow else "")) if over > 0 else f"On target. {min(dow.items(), key=lambda kv: kv[1] or 99)[0] if dow else ''} runs leanest.".strip())
            _lab_state = "bad" if over > 6 else ("warn" if over > 0 else "good")
            if _lab_short:
                # Below the floor: "—" and what is needed, never a verdict.
                _lab_interp = (f"{_plural(period_days, 'day')} of shifts so far — labor % reads against "
                               f"your target from {_MIN_DAYS} days.")
                _lab_state = "neutral"
            elif _lab_stale:
                _lab_interp = f"Out of date — {_lab_stalest.get('basis') or 'the shifts are old'}. " + _lab_interp
                _lab_state = "neutral"
            snapshot.append({"key": "labor", "label": "Labor", "status": "available",
                             "value": "—" if _lab_short else f"{pct:.1f}", "unit": "" if _lab_short else "% of sales",
                             "delta": (None if _lab_short else
                                       ({"value": f"{delta:+.1f} pts", "label": "vs prior period", "good": delta <= 0} if delta is not None else {"value": f"target {labor_target:.0f}%", "label": "", "good": over <= 0})),
                             "secondary": [{"label": "Target" if _labor_tgt_for["source"] == "set" else "Starting target", "value": f"{labor_target:.0f}%"}, {"label": "Over 40h", "value": str(ot_now["people"] if ot_now else 0)}, {"label": "Recoverable", "value": f"${savings:,.0f}/wk" if savings > 0 else "—"}],
                             "interpretation": _lab_interp,
                             "state": _lab_state,
                             "below_floor": _lab_short, "stale": _lab_stale,
                             "spark": hist_pcts[-8:], "spark_label": "labor % by period" if len(hist_pcts) > 1 else None,
                             "attention": (over >= LABOR_OVER_TARGET_PTS and not _lab_short) or bool(ot_now), "sample": False,
                             # The last day the shifts cover, not when a file
                             # was written (CA3 F2).
                             "last_data": (labor.get("date_range") or {}).get("end") or client_data.get("updated_at"),
                             "coverage_note": _cover_note or None})
            if _lab_short:
                brief_lines.append({"text": f"Labor: {_plural(period_days, 'day')} of shifts so far — a read "
                                            f"against your target needs {_MIN_DAYS}.", "tone": "neutral", "module": "labor"})
            elif _lab_stale:
                brief_lines.append({"text": f"Labor {pct:.1f}% as of {_lab_fr.get('as_of') or 'the last upload'} — "
                                            "the data under it is out of date.", "tone": "neutral", "module": "labor"})
            else:
                brief_lines.append({"text": f"Labor {pct:.1f}% against " + (f"a {labor_target:.0f}% target" if _thr_tgt.target_source(r, "labor") == "set" else _labor_tgt) + (f", {delta:+.1f} pts vs last period" if delta is not None else "") + ".", "tone": "bad" if over >= LABOR_OVER_TARGET_PTS else ("good" if over <= 0 else "neutral"), "module": "labor"})
            ask.append("Why is labor over target?" if over > 0 else "Where can I save on labor next week?")
            if last_schedule and last_schedule.get("week_end"):
                upcoming.append({"label": f"Schedule through {last_schedule['week_end']}", "when": last_schedule.get("week_end"), "module": "labor", "kind": "schedule"})

    # ── Food cost ───────────────────────────────────────────────────────────
    if "inventory" in active_keys:
        if not inv_live or not inv:
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
            # Counts are dated by the OLDEST count among the counted items
            # (ingredients.last_recount_at). restaurants.inventory_updated_at
            # is never written, so this attention could never fire and the
            # module always read "fresh" (CA3 F9).
            _inv_state = trust_ctx.sources(("inventory",))[0]
            inv_age = _inv_state.get("lag_days")
            if inv_age is not None and inv_age > 14:
                add_attn("inventory_stale", "watch", f"Oldest inventory count is {int(inv_age)} days old",
                         "Waste and reorder flags drift the longer the count sits.", "inventory", "Quick count",
                         since=f"{int(inv_age)}d", evidence=_inv_state.get("basis"))
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
                         ", ".join(str(c.get("item", ""))[:22] for c in crit[:CRITICAL_LOW_NAMED]) + " — likely to run out before the next delivery.", "inventory", "See the list",
                         evidence=f"{len(reorder)} more to reorder soon",
                         rec_key=stock_key(crit[0].get("item")))
                # Only the items the card NAMES (its detail lists the
                # first four) are shown on it: presenting ten logged items
                # nobody read (re-audit C4). A count, not a ranking.
                attention[-1]["rec_keys"] = [stock_key(c.get("item")) for c in crit[:CRITICAL_LOW_NAMED]]
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
                    # The measured band (K6), not the one the model gave
                    # itself (E3); the object rides beside it.
                    _dgc = _dg.get("confidence_detail") if isinstance(_dg.get("confidence_detail"), dict) else {}
                    cfo_why = {"cause": _dg["cause"], "confidence": _dgc.get("band") or _dg.get("confidence"),
                               "confidence_detail": _dgc or None, "stale": _dg.get("stale")}
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
                _alt = None
                if _dg and _dg.get("alternative_cause") and str(d0.get("item") or "").lower() in json.dumps(_dg.get("drivers") or []).lower():
                    _alt = _dg["alternative_cause"]
                add_rec(_bi_hb.driver_key(d0), _bi_hb.driver_action(d0),
                        d0["evidence"][:1].upper() + d0["evidence"][1:] + ".",
                        f"${d0['dollars_monthly']:,.0f}/month · {d0['difficulty']} effort",
                        "Food cost · margin", "inventory", "This week", "See the numbers",
                        # Waste is tracked against waste, not food cost % (#46).
                        metric="weekly_waste" if d0.get("kind") == "waste" else "food_cost_pct",
                        dollars=d0["dollars_monthly"],
                        # The driver's own evidence input, measured by the
                        # driver (food_cost_intelligence.driver_evidence).
                        ev=d0.get("evidence_input") or {"n": None, "basis": "the ledger"},
                        if_ignored=d0["if_ignored"][:1].upper() + d0["if_ignored"][1:],
                        effort=d0.get("difficulty"), alternative=_alt)
            elif top and float(top.get("waste_cost") or 0) >= 40:
                _wc = float(top.get("waste_cost") or 0)
                add_rec(f"cut_waste:{top.get('item', 'item')}", f"Cut the {top.get('item', 'top-item')} order — it's the biggest waste line",
                        f"It's the single biggest line in last week's waste — {top.get('waste_pct', 0)}% of what you ordered.",
                        f"${_wc:,.0f} wasted last week · ${recoverable:,.0f}/mo could be recovered across items (an opportunity)", "Food cost · margin", "inventory", "Next order",
                        "Adjust the order",
                        metric="weekly_waste",
                        dollars=float(top.get("recoverable_cost") or 0) * __import__("metrics").WEEKS_PER_MONTH or None,
                        dollars_basis="one week's recoverable waste on this item × 52 ÷ 12",
                        ev={"n": 1, "kind": "waste_weeks", "basis": "one week of waste counts"},
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
                        _dg.get("headline") or "", "Food cost · margin", "inventory", "This week",
                        "See the numbers", metric="food_cost_pct", dollars=_dg.get("dollars_at_stake"),
                        # THE food diagnosis input (the Food Cost card's and
                        # the hero's too): weeks of counts behind it plus its
                        # corroborating modules, capped by the CAPPED band
                        # (B1 H3/H4, R9) — one figure everywhere.
                        ev=rec_trust.food_diagnosis_input(rid, _dg),
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
                        "Food cost · margin", "inventory", "This week", "See the price",
                        metric="food_cost_pct", dollars=x["monthly_margin_lost"],
                        dollars_basis=x.get("monthly_basis"),
                        ev={"n": max((int(dv.get("weeks") or 1) for dv in (x.get("drivers") or [{}])), default=1),
                            "kind": "price_weeks",
                            "flags": () if x.get("units_sold_30d") else ("no_sales_mix",),
                            "basis": f"ingredient price history and {x.get('monthly_basis') or 'the sales mix'}"},
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
                # One week's waste above tolerance projected to a month:
                # an opportunity, labelled as one (NS3 M3).
                brief_lines.append({"text": f"About ${recoverable:,.0f}/month of food waste above tolerance could be recovered (an opportunity projected from one week), led by {top.get('item') if top else 'a few items'}.", "tone": "warn", "module": "inventory",
                                    "claim_kind": "opportunity"})
            # A week with no waste logged is not_measured (inventory), never a
            # win: nothing recorded is not "under control" (CA4 F11).
            _waste_measured = inv.get("benchmark_state") != "not_measured"
            # The win says what the label says — where the rate sits against
            # the owner's target (or Cavnar's starting one), never
            # "Excellent" (Benchmarking #36) — and only when it is under or
            # near it.
            _wl = inv.get("benchmark_label") or ""
            if recoverable <= 0 and fc_pct is None and _waste_measured and inv.get("benchmark_tone") == "good" \
                    and _wl in ("Under target", "Near target"):
                _wt_basis = (inv.get("waste_target") or {}).get("basis") or "the 4–5% starting target"
                add_win("waste_low", f"Waste is {_wl.lower()}",
                        (f"{waste_rate}% of purchases this week" if waste_rate is not None else "This week")
                        + f" — {_wl.lower()} against {_wt_basis}.", "inventory")
            elif fc_target is not None and fc_pct is not None and fc_pct <= fc_target:
                add_win("food_cost_on_target", f"Food cost {fc_pct}% — on target",
                        f"At or under your own {fc_target}% target over the last 28 days.", "inventory")

            # The headline number is the margin position where it exists, and
            # the recoverable estimate only where it does not.
            _headline = f"{fc_pct}%" if fc_pct is not None else f"${recoverable:,.0f}"
            _unit = ("food cost" if fc_pct is not None else "est. recoverable / mo")
            # What the headline IS (NS3 R1, R10): a measured percentage, or
            # an opportunity projected from one week. Renderers read `unit`
            # and `kind` from here and never hardcode them (H2).
            _headline_kind = "measured" if fc_pct is not None else "opportunity"
            _delta = None
            if fc_pct is not None and fc_target is not None:
                _delta = {"value": f"{fc_pct - fc_target:+.1f} pts", "label": fc_label or "",
                          "good": fc_pct <= fc_target}
            elif waste_rate is not None:
                _delta = {"value": f"{waste_rate}% waste", "label": inv.get("benchmark_label") or "",
                          "good": _waste_measured and inv.get("benchmark_tone") in ("good", None)}
            _secondary = [{"label": "Critical low", "value": str(len(crit))},
                          {"label": "Reorder soon", "value": str(len(reorder))},
                          {"label": "Stock value", "value": f"${float(inv.get('total_stock_value') or 0):,.0f}"}]
            if drivers:
                # Each ingredient counted once (#36): salmon's waste, price
                # rise and over-recipe usage are three readings of one spend.
                _secondary.insert(0, {"label": "At stake",
                                      "value": f"${at_stake_monthly(drivers):,.0f}/mo",
                                      "kind": "opportunity"})
            snapshot.append({"key": "inventory", "label": "Food Cost", "status": "available",
                             "value": _headline, "unit": _unit, "kind": _headline_kind,
                             "delta": _delta,
                             "secondary": _secondary,
                             # WHY, not just what — the one thing the card
                             # could never say.
                             "interpretation": (
                                 cfo_why["cause"] if cfo_why else
                                 (f"{drivers[0]['label']} is the largest driver "
                                  f"(${drivers[0]['dollars_monthly']:,.0f}/month)." if drivers else
                                  (f"{top.get('item')} is the biggest waste line (${float(top.get('waste_cost') or 0):,.0f} last week)." if top else ("No waste flagged this week." if _waste_measured else "No waste logged this week — not measured.")))),
                             "why_confidence": (cfo_why or {}).get("confidence"),
                             "why_confidence_detail": (cfo_why or {}).get("confidence_detail"),
                             "state": "bad" if (crit or (fc_target and fc_pct and fc_pct > fc_target)) else ("warn" if recoverable > 0 else ("good" if (_waste_measured or fc_pct is not None) else "neutral")),
                             "spark": [], "spark_label": None,
                             "attention": bool(crit) or recoverable >= 200 or bool(fc_target and fc_pct and fc_pct > fc_target),
                             "sample": False, "last_data": _inv_state.get("as_of_iso")})
            ask.append("Where are my biggest food cost opportunities?")

    # ── Marketing ───────────────────────────────────────────────────────────
    if "marketing" in active_keys:
        last_age = _age_days(mkt.get("last_at"), now)
        posted_age = _age_days(mkt.get("last_posted_at"), now)
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
                    f"last post {int(posted_age)}d ago · {mkt.get('month', 0)} pieces drafted this month", "Marketing · reach", "marketing", "This week", "Draft a post",
                    # What the advice claims is that a post does something:
                    # its sample is the posts whose performance was measured
                    # (the Marketing read's own input, N_FULL "posts"), not
                    # "1 row" for the gap (B1 C1) — partial, since reach
                    # itself is not measured here.
                    ev=dict(_mkt_evidence(rid), flags=("partial",)),
                    if_ignored="nothing new goes out to your followers", effort="low")
        elif mkt.get("last_at") is None:
            add_rec("first_post", "Generate your first post", "Cavnar AI writes it in your voice from your reviews and menu — one click.", "no marketing content yet", "Marketing · reach", "marketing", "Today", "Generate a post",
                    # A setup step: nothing to measure, so no percentage —
                    # "Confidence not yet measurable", never a stand-in.
                    ev={"n": None, "basis": "a setup step — nothing to measure yet"},
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
                    f"{intel['competitors']} competitors compared {int(age)}d ago", "Intel · positioning", "intel", "This week", "Open Intel",
                    # A model-written comparison is an inference: flagged as
                    # one (never high) — not a "model_band" the model never
                    # gave (R9, B1 M1: the Why? panel said "the AI read rated
                    # itself medium", a constant 65). Its age is Data Freshness.
                    ev={"n": intel["competitors"], "kind": "competitors", "flags": ("inferred",),
                        "basis": f"a model-written comparison of {_plural(intel['competitors'], 'public listing')}"},
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

    for ch in comparison_changes(rung_ups, active_keys):
        add_change(ch["text"], ch["tone"], ch["module"], ch["at"])

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
    # ...and a "not for us" to the same ADVICE on any surface (H16): the
    # nightly report's "cut Tuesday's hours" or the Reviews "Do today" line
    # declined is Home's trim_day:Tuesday declined, whatever each key is.
    # insight_store.advice_signature is the one reading of what an item is
    # about; the DSR (dsr/narrative.settle_actions) and Reviews
    # (client_api._review_insight_recs) sides use the same two calls.
    declined_sigs = home_declined_signatures(rid)
    for a in attention:
        a["rec_key"] = a.get("rec_key") or ledger_key(a["key"])
        a["dismissable"] = a["severity"] != "critical"
        a["times_hidden"] = hidden_counts.get(a["rec_key"], 0)
        # The contract every payload carrying a recommendation keeps
        # (rec_delivery.answerable): only an answerable item is presented.
        a["answerable"] = attention_answerable(a)
        a["advice_signature"] = signature_of(a["rec_key"], a["title"])
    attention = [a for a in attention if not (a["dismissable"] and (
        a["rec_key"] in answered or (a["advice_signature"] and a["advice_signature"] in declined_sigs)))]

    # Data freshness per module and source (K4), from the one registry every
    # card's Data Freshness reads (data_freshness) — and the stalest date
    # under the page. The strip used to be built here and rendered nowhere,
    # with an unknown age reading "fresh" (CA3 F1, CA4 F7). Read before the
    # headline: "Running well" is never said over stale or unknown data
    # (re-audit B4 M4).
    freshness = home_freshness(trust_ctx, active_keys, labor_live, inv_live,
                               google_connected=google_connected, reviews_on_file=int(rstats.get("total") or 0))
    data_as_of = stalest_as_of(freshness)
    _not_current = [f for f in freshness if f.get("state") in ("stale", "unknown")]
    # "Live" / "current" is said only of a source that is current by its
    # data date AND inside one cadence of its last success (data_health's
    # cadence rule, #27): a nightly POS whose 3am sync stopped two days ago
    # can still read 80% on its sales date, and it is not live.
    import data_health as _dh
    _count_live = sum(1 for f in freshness if _dh.counts_as_current(f, now))

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
    elif wins and _not_current:
        headline = (f"Nothing flagged — but {_plural(len(_not_current), 'data source')} "
                    f"{'is' if len(_not_current) == 1 else 'are'} out of date")
        headline_tone = "neutral"
    elif wins and _count_live:
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

    for _r in recs:
        _r["advice_signature"] = signature_of(_r["key"], _r["title"])
    dismissed_recs = [r for r in recs if r["key"] in answered]
    recs = [r for r in recs if r["key"] not in answered
            and not (r["advice_signature"] and r["advice_signature"] in declined_sigs)]
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
    # The dollars a card is shown with, corrected by this restaurant's own
    # measured results of the kind once there are enough of them (F6):
    # `dollars_adjusted` / `calibration_n` / `calibration_note` beside the
    # raw `dollars_monthly`, which stays what the ledger snapshots.
    for _r in recs:
        try:
            import rec_learning as _rl_cal
            _rl_cal.attach_dollar_calibration(_r, learned)
        except Exception as e:
            print(f"[home] dollar calibration unavailable for {_r.get('key')}: {e}")
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
    import rec_delivery
    for r in recs:
        r["answerable"] = rec_delivery.answerable(r["key"])

    # Every card and attention item this Home shows is an impression in the
    # ledger (#37) — one episode per key, one `shown` per surface per day.
    # A key the ledger says is answered comes back None and is not shown.
    # Only what the owner can ANSWER here is presented (re-audit B7): a
    # setup or health nudge, a critical item (never dismissable) and a key
    # rec_delivery never presents (urgent_reviews) would each expire as
    # "ignored" with no answer ever offered. An item that drops out (its
    # key came back answered) lets the next one into view, which is then
    # presented too — at most one more pass.
    shown = {}
    done = set()
    for _pass in range(2):
        rendered_attention = attention[:HOME_ATTENTION_SHOWN]
        batch = rec_delivery.only_presentable([
            # Each carries the confidence it is shown with, snapshotted at
            # delivery (K3).
            *({"key": a["rec_key"], "module": _LEDGER_MODULE.get(a["module"], "home"), "title": a["title"],
               "position": i, "confidence": a.get("confidence"),
               "confidence_band": (a.get("confidence") or {}).get("band")}
              for i, a in enumerate(rendered_attention) if a["answerable"]),
            # A card that stands for several (critically low: one key per
            # item) shows each of them.
            *({"key": k, "module": _LEDGER_MODULE.get(a["module"], "home"), "title": a["title"], "position": i,
               "confidence": a.get("confidence"), "confidence_band": (a.get("confidence") or {}).get("band")}
              for i, a in enumerate(rendered_attention) if a["answerable"]
              for k in (a.get("rec_keys") or []) if k != a["rec_key"]),
            *({"key": r["key"], "module": _LEDGER_MODULE.get(r["module"], "home"), "title": r["title"],
               "position": 100 + i, "dollar_value": r.get("dollars_monthly"),
               "confidence": r.get("confidence"),
               "confidence_band": (r.get("confidence") or {}).get("band"),
               "evidence_sources": r.get("evidence_sources"), "model_written": r.get("model_written"),
               "cavnar_completes": bool(r.get("action")), "expected_metric": r.get("metric"),
               # The price it asks for: a new one is a new recommendation
               # (rec_ledger supersedes the open episode, ROI #37).
               "target": ((r.get("action") or {}).get("price") if (r.get("action") or {}).get("kind") == "reprice"
                          else None)}
              for i, r in enumerate(recs) if r["answerable"])])
        batch = [it for it in batch if it["key"] not in done]
        if not batch:
            break
        try:
            # A build that must record nothing (the Ask opening's fallback,
            # re-audit C5) filters what was answered without writing.
            got = (rec_ledger.present_many if present else _answered_only)(
                rid, batch, "home", user_id=current_user.get("id"))
        except Exception:
            got = {}
        if not got:
            break
        done.update(got)
        shown.update(got)
        before = [a["rec_key"] for a in attention[:HOME_ATTENTION_SHOWN]]
        attention = [a for a in attention if not a["answerable"] or shown.get(a["rec_key"], True) is not None]
        recs = [r for r in recs if not r["answerable"] or shown.get(r["key"], True) is not None]
        if [a["rec_key"] for a in attention[:HOME_ATTENTION_SHOWN]] == before:
            break

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
    for _q in quick:
        _q["nav"] = QUICK_NAV.get(_q["key"])
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
        # The group's one ranking floor (thresholds.GROUP_RANK_MIN_REVIEWS,
        # fix I12, now the platform's rating floor) and the group brief's one
        # ranking rule (benchmark_views.rank_by_rating, Benchmarking #19): a
        # location is "strongest" only on a gap beyond noise.
        import benchmark_views as _bv_p
        _rank_p = _bv_p.rank_by_rating([{"id": l["id"], "name": l["name"], "rating": l.get("rating_30d"),
                                         "n": l.get("reviews_30d")} for l in locations])
        worst = max(locations, key=lambda l: ({"critical": 3, "important": 2, "watch": 1, "healthy": 0}[l["health"]], l["attention"]))
        portfolio = {"total": len(locations), "healthy": sum(1 for l in locations if l["health"] == "healthy"), "needing": len(needing),
                     "biggest_issue": ({"location": worst["name"], "id": worst["id"], "issue": worst["top_issue"]} if worst["top_issue"] else None),
                     "strongest": _rank_p["strongest"]}

    charts = {
        "rating": [{"label": _mdy(w.get("label"), (w.get("week_key") or "")[:4]), "avg": w.get("avg_rating") or 0, "pos": w.get("positive") or 0, "neg": w.get("negative") or 0, "total": w.get("total") or 0} for w in sentiment if w.get("total")],
        "labor": [{"label": _mdy(h.get("period_start")), "pct": h.get("labor_pct")} for h in labor_hist[::-1] if h.get("labor_pct") is not None] if labor_live else [],
        "labor_target": labor_target, "labor_target_label": _labor_tgt_for["label"],
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
        # The Restaurant Data Health Score beside the legacy freshness[]
        # (which older iOS builds still read): {overall, worst_line}. The
        # full payload — every source line, what is not connected, each
        # module's confidence impact — is GET /api/data-health (#21-23).
        "data_health": _data_health_compact(rid, restaurant),
        # What "Monitoring N signals" may honestly say: only sources that are
        # current count, beside the stalest date (E7).
        # `stale` counts the sources reading stale or unknown; `all_clear`
        # is whether an "All clear" box may be drawn at all — nothing needs
        # the owner, at least one source is current and none is stale
        # (re-audit B4 M4: the box ignored both).
        "monitoring": {"count_live": _count_live,
                       "sources": sum(1 for f in freshness if f.get("pct") is not None),
                       "stale": len(_not_current),
                       "all_clear": bool(not attention and _count_live and not _not_current),
                       "stalest_as_of": data_as_of},
        "brief": {"headline": headline, "tone": headline_tone, "lines": brief_lines[:4], "overnight": overnight,
                  "data_as_of": data_as_of,
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
                              SUM(COALESCE(NULLIF(review_date,''), fetched_at) >= date('now','-60 days') AND COALESCE(NULLIF(review_date,''), fetched_at) < date('now','-30 days')) AS n_prev,
                              SUM(rating<=2 AND COALESCE(NULLIF(review_date,''), fetched_at) >= date('now','-7 days')) AS low7
                       FROM reviews WHERE restaurant_id=? AND processed=1 AND deleted_at IS NULL""", (rid,)) or {}
    total = int(rs.get("total") or 0)
    rate = round(100.0 * int(rs.get("responded") or 0) / total) if total else None
    last_active = (_one_dict(conn, "SELECT MAX(created_at) AS t FROM login_history WHERE restaurant_id=?", (rid,)) or {}).get("t")
    labor = None
    _lab_ev = None
    cd = _one_dict(conn, "SELECT shifts_csv IS NOT NULL AND shifts_csv != '' AS live FROM client_data WHERE restaurant_id=?", (rid,)) or {}
    if r.get("module_labor") and cd.get("live"):
        try:
            from labor import analyse_shifts_for_restaurant
            la = analyse_shifts_for_restaurant(rid)
            if la.get("is_live"):
                import thresholds as _thr_g
                _tgt_g = _thr_g.target_for(r, "labor")
                target = _tgt_g["pct"]
                # People over 40h THIS payroll week, not every overtime week
                # in the upload (the same read as the location's own Home).
                try:
                    from time_utils import restaurant_now as _rn_loc
                    _ot_loc = overtime_this_week(la, _rn_loc(get_restaurant(rid)).date())
                except Exception:
                    _ot_loc = None
                labor = {"pct": float(la.get("overall_labor_pct") or 0), "target": target,
                         "target_label": _tgt_g["label"], "target_phrase": _tgt_g["phrase"],
                         "target_alerts": _tgt_g["alerts_allowed"],
                         "over": round(float(la.get("overall_labor_pct") or 0) - target, 1),
                         "overtime": (_ot_loc or {}).get("people", 0),
                         "period_days": int(la.get("period_days") or (la.get("date_range") or {}).get("days") or 0)}
                try:
                    from labor import diagnosis_evidence_input as _dei_loc
                    _lab_ev = _dei_loc(la)
                except Exception:
                    _lab_ev = None
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
    from labor import MIN_DAYS_TO_EXTRAPOLATE as _MIN_DAYS_G
    # The location Home's own floors (re-audit B4 M5): labor over target
    # needs MIN_DAYS_TO_EXTRAPOLATE days of shifts, and a rating move needs
    # REVIEW_MOVE_MIN_N reviews on BOTH sides and RATING_MOVE_STARS — the
    # group view flagged what the location's Home suppressed.
    if labor and labor["over"] >= LABOR_OVER_TARGET_PTS and labor.get("period_days", 0) >= _MIN_DAYS_G:
        # Against Cavnar's starting target (nobody set it) the issue says
        # so and is a watch, never "critical" — the location's own issue
        # list opens nothing on it (re-audit #10, R4-26).
        _sev_g = ("watch" if not labor.get("target_alerts", True)
                  else ("critical" if labor["over"] >= 6 else "important"))
        issues.append({"severity": _sev_g, "text": f"Labor {labor['pct']:.1f}% — {labor['over']:.1f} pts over "
                                                   f"{labor.get('target_phrase') or 'target'}", "module": "labor",
                       "kind": "labor_over"})
    if labor and labor["overtime"]:
        issues.append({"severity": "important", "text": f"{labor['overtime']} {'person' if labor['overtime'] == 1 else 'people'} over 40h this week", "module": "labor",
                       "kind": "overtime"})
    if inv and inv["critical_low"]:
        issues.append({"severity": "important", "text": f"{_plural(inv['critical_low'], 'item')} critically low", "module": "inventory",
                       "kind": "critical_low"})
    avg30 = rs.get("avg30"); prev = rs.get("avg_prev")
    if (avg30 and prev and (rs.get("n30") or 0) >= REVIEW_MOVE_MIN_N and (rs.get("n_prev") or 0) >= REVIEW_MOVE_MIN_N
            and round(avg30 - prev, 1) <= -RATING_MOVE_STARS):
        issues.append({"severity": "important", "text": f"Rating slipped to {avg30:.1f}★ (from {prev:.1f}★)", "module": "reviews",
                       "kind": "rating_drop"})
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
    _group_issue_confidence(rid, issues, labor, inv, rs, _lab_ev)
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


def _group_issue_confidence(rid, issues, labor, inv, rs, lab_ev):
    """Each group-view attention item that is advice carries its K1
    `confidence` (confidence round 2, T1) from the same evidence the
    location's own Home reads for it: labor over target from the days of
    shifts with sales (labor.diagnosis_evidence_input), a rating slip from
    the smaller of the two 30-day samples. A fact about the location's
    state — urgent reviews unanswered, a failing sync, unacknowledged
    issues, the people over 40 hours, the items critically low — carries
    none, as on the location's own Home (HOME_FACT_KEYS; B4 H5: facts carry
    no confidence). Never raises."""
    try:
        import rec_trust
        import data_freshness
        ctx = rec_trust.Context(rid)
    except Exception as e:
        print(f"[group] confidence unavailable for {rid}: {e}")
        return issues
    for i in issues:
        kind = i.get("kind")
        try:
            if kind == "labor_over" and lab_ev:
                ev, srcs = dict(lab_ev), data_freshness.sources_for(["labor"])
            elif kind == "rating_drop":
                n = min(int(rs.get("n30") or 0), int(rs.get("n_prev") or rs.get("n30") or 0))
                ev, srcs = ({"n": n, "kind": "reviews", "basis": f"{int(rs.get('n30') or 0)} reviews in the last 30 days"},
                            ("reviews",))
            else:
                continue
            i["confidence"] = rec_trust.assess(rid, kind, evidence=ev, sources=srcs, ctx=ctx)
        except Exception as e:
            print(f"[group] confidence failed for {rid}/{kind}: {e}")
    return issues


def _location_last_night(rid, user):
    """The newest night's report row for one location, as `user` may read
    it (dsr.access.summary with the location's restaurant, so the verdict
    and score are the report's own), or None when there is none or the
    login has no DSR. Never raises."""
    try:
        from dsr import access, store
        if access.view_for(user) is None:
            return None
        rows = store.list_reports(rid, limit=1)
        if not rows:
            return None
        return access.summary(rows[0], user, get_restaurant(rid))
    except Exception as e:
        print(f"[group] last night unavailable for {rid}: {e}")
        return None


def group_last_night(locs):
    """The group's total strip for the newest night any location reported:
    net summed over the locations that measured THAT night (never averaged,
    never mixing two nights), how many of how many, and vs budget only when
    every one of them carries it — a partial budget sum would read as the
    group's. None when no location has a measured night."""
    nights = [l.get("last_night") for l in locs if isinstance(l.get("last_night"), dict)]
    measured = [n for n in nights if isinstance(n.get("net"), (int, float)) and n.get("business_date")]
    if not measured:
        return None
    day = max(str(n["business_date"])[:10] for n in measured)
    same = [n for n in measured if str(n["business_date"])[:10] == day]
    budgets = [n.get("vs_budget") for n in same]
    from time_utils import mdy
    return {"business_date": day, "label": mdy(day), "net": round(sum(float(n["net"]) for n in same), 2),
            "locations": len(same), "of": len(locs),
            "vs_budget": (round(sum(float(b) for b in budgets), 2)
                          if budgets and all(isinstance(b, (int, float)) for b in budgets) else None)}


def _lower_first(text):
    t = str(text or "").strip().rstrip(".")
    return t[:1].lower() + t[1:] if len(t) > 1 and t[1:2].islower() else t


def group_summary_line(locs):
    """The group Home's one sentence (density fix #48), deterministic from
    the rows' own issue lines: "Evanston needs a look: labor 31.2% — 3.2 pts
    over target, 3 urgent reviews unanswered." — the worst location first,
    its first two issues, then how many more need a look. None when every
    location is healthy (the headline already says so)."""
    rank = {"critical": 0, "important": 1}
    needing = sorted([l for l in locs if l.get("health") in rank],
                     key=lambda l: (rank[l["health"]], -len(l.get("issues") or []), l.get("name") or ""))
    if not needing:
        return None
    top = needing[0]
    why = [_lower_first(i.get("text")) for i in (top.get("issues") or [])[:2] if i.get("text")]
    line = f"{top['name']} needs a look" + (f": {', '.join(why)}" if why else "")
    more = len(needing) - 1
    if more:
        line += f" · {_plural(more, 'more location')} too"
    return line + "."


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
        # Each location's last night (density fix #19): the same row its own
        # Home's status line reads (dsr.access.summary — the scorecard's
        # verdict, net, vs budget), for this login, so the table and the
        # switcher can compare locations on the night, not only on reviews.
        l["last_night"] = _location_last_night(l["id"], current_user)
    rank = {"critical": 0, "important": 1, "watch": 2}
    attention = []
    import nav as _nav_g
    for l in locs:
        for i in l["issues"]:
            # Where "Open" lands once it has switched to that location
            # (friction #24): the item's own place, not that location's
            # Home top. A reviews issue opens the inbox on the filter it is
            # about.
            if i.get("module") == "reviews":
                _urg = int(((l.get("reviews") or {}).get("urgent")) or 0)
                _gnav = _nav_g.path("reviews", filter="urgent" if _urg else "pending")
                _glabel = "Reply now" if _urg else "Open replies"
            else:
                _gnav, _glabel = _nav_g.path(i.get("module") or "home"), "Open"
            attention.append({**i, "location": l["name"], "restaurant_id": l["id"], "severity_rank": rank[i["severity"]],
                              "nav": _gnav, "action_label": _glabel})
    attention.sort(key=lambda a: (a["severity_rank"], a["location"]))
    # Strongest and weakest (Benchmarking #19; BM1-19, BM3-18, BM4-11): the
    # platform's rating floor (thresholds.GROUP_RANK_MIN_REVIEWS, now
    # RATING_MIN_REVIEWS), each location read against its own normal first,
    # and a name only when the gap is beyond noise — a 4.6 against a 4.5 on
    # a dozen reviews each is not a strongest and a weakest.
    import benchmark_views as _bv
    _rank = _bv.rank_by_rating([{"id": l["id"], "name": l["name"], "rating": l["reviews"]["rating_30d"],
                                 "n": l["reviews"]["reviews_30d"]} for l in locs])
    for l in locs:
        l["reviews"]["vs_own"] = _rank["vs_own"].get(l["id"])
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
                      "strongest": _rank["strongest"],
                      "weakest": _rank["weakest"],
                      "ranking_basis": _rank["basis"],
                      "heaviest_labor": ({"location": heaviest["name"], "id": heaviest["id"], "pct": heaviest["labor"]["pct"], "over": heaviest["labor"]["over"]} if heaviest and heaviest["labor"]["over"] > 0 else None),
                      "biggest_issue": ({"location": attention[0]["location"], "id": attention[0]["restaurant_id"], "issue": attention[0]["text"]} if attention else None),
                      "last_night": group_last_night(locs)},
        "summary_line": group_summary_line(locs),
    }
    # Location to location on the engine's `location` kind (#19): each
    # location against its own normal, then against the others, a gap
    # called only beyond noise. Never raises into the brief.
    try:
        payload["location_compare"] = _bv.location_compare(current_user)
    except Exception as e:
        print(f"[group] location comparison failed: {e}")
        payload["location_compare"] = None
    _cache_put(key, payload)
    return payload, 200
