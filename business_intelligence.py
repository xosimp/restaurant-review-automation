"""
business_intelligence.py — the layer that reads across modules.

Every module in this platform answers its own question well and stops at its
own edge. Reviews knows guests complained about service on Fridays; Labor
knows Friday runs the leanest; Food Cost knows Friday carries most of the
waste. Nothing put those three sentences next to each other, and the whole
value of an owner having all three modules is exactly that sentence.

Audit #15 found why: `review_intelligence` and `food_cost_intelligence` each
carry the same comment — "ask_cavnar.build_context already composes all of
them" — while build_context only ever concatenated per-module AGGREGATES and
never the causal layers. Three components each assumed a fourth did the
joining. This is that fourth component.

Two rules it does not break:

  A LINK NEEDS BOTH SIDES TO CLEAR THEIR OWN MODULE'S FLOOR. Nothing here
  invents a threshold. A complaint cluster is a cluster because
  review_intelligence said so (MIN_CLUSTER_MENTIONS, CONCENTRATION_MIN_SHARE);
  a waste day is concentrated because food_cost_intelligence said so
  (MIN_EVENTS_PER_BUCKET, 1.6x an even week). This module only asks whether
  two already-established findings point at the same day, dish or shift. That
  is deliberately a weak test to fail and a strong one to pass: it cannot
  manufacture a pattern, because it cannot see anything that is not already
  a pattern in its own module.

  CO-OCCURRENCE IS NOT CAUSE. Every link says what would confirm it and what
  else would explain it. labor.py:974 already forbids asserting that a lean
  day cost revenue or slowed service — "this system has no service-time,
  wait-time or cover-count data, so you cannot tell which it was" — and that
  constraint survives being joined to a review. A link between a lean Friday
  and Friday complaints is a QUESTION worth asking, phrased as one.

The money roll-up is the other half. Food Cost knows what its drivers are
worth per month, Labor knows what optimised scheduling is worth per month,
Reviews knows what the rating slide puts at risk per month. Ranked together
they answer "where is the money", which no single module can.
"""
import logging
from datetime import date, timedelta

from models import DB_PATH, get_conn

log = logging.getLogger(__name__)


# ── one question's reads (AI cost audit 10/7/26 #33) ───────────────────────
#
# One Ask question computed the cross-module brief twice (the snapshot's
# ACROSS THE BUSINESS block, then read_business_snapshot) and the full labor
# analysis three times (the snapshot's LABOR block, gather() inside each
# brief). Inside question_memo() each is computed once per key and handed
# out as a copy. It is a scope, not a cache: it lives for one question (Ask
# opens it around a turn), nothing outside one is memoised, and a write
# inside the question drops the restaurant's entries (forget_question_memo,
# from ask_cavnar.invalidate_context) — so it can never serve a stale read
# across requests. A contextvar, so the worker threads Ask runs reads on
# (ai_utils.context_runner) share the question's memo.
import contextlib as _contextlib
import contextvars as _contextvars
import copy as _copy

_QUESTION_MEMO = _contextvars.ContextVar("cavnar_bi_question_memo", default=None)


@_contextlib.contextmanager
def question_memo():
    """Memoise executive_brief and shift_analysis for the block. Nested
    blocks share the outer one."""
    if _QUESTION_MEMO.get() is not None:
        yield
        return
    token = _QUESTION_MEMO.set({})
    try:
        yield
    finally:
        _QUESTION_MEMO.reset(token)


def _memoised(key, fn):
    memo = _QUESTION_MEMO.get()
    if memo is None:
        return fn()
    if key in memo:
        return _copy.deepcopy(memo[key])
    value = fn()
    try:
        memo[key] = _copy.deepcopy(value)
    except Exception:
        pass                    # not copyable: computed again next time, never shared
    return value


def forget_question_memo(restaurant_id=None):
    """Drop the open question's entries for a restaurant (all, for None) —
    the data under them changed. A no-op outside question_memo()."""
    memo = _QUESTION_MEMO.get()
    if not memo:
        return
    for k in [k for k in memo if restaurant_id is None or k[1] == int(restaurant_id)]:
        memo.pop(k, None)


def shift_analysis(restaurant_id):
    """labor.analyse_shifts_for_restaurant, once per question (#33)."""
    from labor import analyse_shifts_for_restaurant
    return _memoised(("shifts", int(restaurant_id)), lambda: analyse_shifts_for_restaurant(restaurant_id))


def _viewer_scope(restaurant, viewer):
    """What an executive brief depends on besides the restaurant id: the
    module flags of the (viewer's) restaurant and the login whose answers
    filter the links."""
    flags = None if restaurant is None else tuple(
        bool(getattr(restaurant, f, 0)) for f in ("module_reviews", "module_labor", "module_inventory",
                                                   "module_marketing"))
    who = viewer.get("id") if isinstance(viewer, dict) else (None if viewer is None else repr(viewer))
    return flags, who

# A weekday link needs both sides pointing at the same day. Two modules each
# naming a concentrated day is already past their own floors, so no extra
# threshold is applied here — see the module docstring.
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday")

# How far a weekday's labor percentage must sit below the period average
# before it is described as "the leanest day". Below this the difference is
# noise and naming a day would be inventing a pattern.
LEAN_DAY_MIN_GAP_PTS = 2.0

# A weekday average needs more than one of that weekday behind it. labor.py's
# dow_summary is a mean with no count attached, so two weeks is the shortest
# period in which every weekday has been seen at least twice.
MIN_PERIOD_DAYS_FOR_WEEKDAY = 14

# Mirrors labor.MIN_DAYS_TO_EXTRAPOLATE — the point below which labor.py
# itself withholds a monthly projection, so a zero coming back from it means
# "too short", not "nothing to recover".
_LABOR_MIN_DAYS_TO_PROJECT = 7

# A cross-module money line is only worth an owner's attention above this.
# Under it the figure is real but the action it implies costs more than it
# returns, and listing it crowds out the ones that matter.
MIN_MONTHLY_DOLLARS = 50.0


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _cat(category):
    """A complaint category as prose. The stored values are snake_case
    identifiers, and "takeout_delivery" was reaching the What Connects card
    on the dashboard looking like a variable name."""
    from analyser import category_label
    return category_label(category)


def _norm(text):
    """Loose match key for a dish or role named by two different modules.

    Reviews get a dish name from the analyser's entity extraction ("the
    ribeye"); Food Cost gets it from the POS menu ("Ribeye Steak"). Exact
    string equality would never fire.
    """
    # Punctuation becomes a SPACE, not nothing — "Ribeye-Steak" has to
    # normalise to "ribeye steak" for the word-run test below to see the word
    # "ribeye" in it at all.
    return " ".join("".join(ch if (ch.isalnum() or ch == " ") else " "
                            for ch in (text or "").lower()).split())


# Below this a dish name is too generic to match on: "pie", "dip" and "ale"
# appear inside unrelated words and sentences, and a link built on one would
# be the manufactured pattern this module exists to avoid.
_MIN_DISH_TOKEN = 4


def _same_thing(a, b):
    """True when two modules' names for a dish plainly refer to one dish.

    Reviews get the name from the analyser's entity extraction ("the
    ribeye"); Food Cost gets it from the POS menu ("Ribeye Steak") or from a
    driver's evidence sentence. Exact equality would never fire.

    The test is a shared significant WORD, not a substring: "Ribeye-Steak"
    and "the ribeye" share "ribeye" and are one dish, while a substring test
    would also match "ribeye" inside "ribeyeburgersauce" and a whole-run test
    would miss the pair entirely. Words under _MIN_DISH_TOKEN never count —
    "pie", "dip" and "ale" are too generic to carry a link on their own, and
    neither are the filler words a guest writes around a dish name.
    """
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return False
    if x == y:
        return True
    left = {t for t in x.split() if len(t) >= _MIN_DISH_TOKEN and t not in _STOPWORDS}
    right = {t for t in y.split() if len(t) >= _MIN_DISH_TOKEN and t not in _STOPWORDS}
    return bool(left & right)


# Words long enough to pass the length floor but that carry no identity — a
# driver's evidence sentence is full of them, and "over" or "cost" matching
# between a dish name and a sentence about cost would link anything to
# anything.
_STOPWORDS = {
    "the", "and", "with", "from", "over", "under", "this", "that", "than",
    "cost", "costs", "price", "prices", "waste", "wasted", "menu", "item",
    "items", "recipe", "portion", "month", "monthly", "week", "weekly",
    "your", "their", "have", "been", "were", "into", "more", "less", "each",
    "about", "against", "every", "some", "most", "very",
}


# ── gathering ──────────────────────────────────────────────────────────────

def _safe(label, fn, degraded):
    """Run one module's entry point, recording failure rather than hiding it.

    A cross-module read that silently drops a module is worse than one that
    fails: the answer looks complete and is missing the half that mattered.
    Same contract food_cost_intelligence.cost_drivers already uses for its
    five driver sources.
    """
    try:
        return fn()
    except Exception as e:
        log.warning("business_intelligence: %s unavailable: %s", label, e)
        degraded.append(label)
        return None


def gather(restaurant_id: int, restaurant=None, db_path: str = DB_PATH) -> dict:
    """Every module's own executive read, in one pass.

    Module flags are honoured: a restaurant without Food Cost is not asked
    for a food cost brief, and its absence is reported as "not on this plan"
    rather than as a failure or as zero.
    """
    from models import get_restaurant
    restaurant = restaurant or get_restaurant(restaurant_id)
    degraded, off = [], []
    out = {"reviews": None, "food_cost": None, "labor": None,
           "marketing": None, "visibility": None, "dsr": None}

    def _on(flag):
        return bool(getattr(restaurant, flag, 0)) if restaurant else False

    if _on("module_reviews"):
        import review_intelligence as ri
        out["reviews"] = _safe("reviews", lambda: {
            "brief": ri.executive_brief(restaurant_id, db_path=db_path),
            "clusters": ri.complaint_clusters(restaurant_id, db_path=db_path),
            "diagnoses": ri.get_diagnoses(restaurant_id, db_path=db_path, include_stale=True),
        }, degraded)
    else:
        off.append("reviews")

    if _on("module_inventory"):
        import food_cost_intelligence as fci
        out["food_cost"] = _safe("food_cost", lambda: {
            "brief": fci.executive_brief(restaurant_id, db_path=db_path),
            "weekday_waste": fci.weekday_waste(restaurant_id, db_path=db_path),
        }, degraded)
    else:
        off.append("food_cost")

    if _on("module_labor"):
        out["labor"] = _safe("labor", lambda: shift_analysis(restaurant_id), degraded)
        # Sample shift data is not this restaurant's labor. The Labor tab
        # shows it so the module isn't blank before the first upload; reading
        # it here would put invented days into an executive answer.
        if out["labor"] and not out["labor"].get("is_live"):
            out["labor"] = {"is_live": False}
    else:
        off.append("labor")

    if _on("module_marketing"):
        out["marketing"] = _safe("marketing", lambda: _marketing_activity(restaurant_id, db_path), degraded)
    else:
        off.append("marketing")

    out["visibility"] = _safe("visibility", lambda: _visibility(restaurant_id, db_path), degraded)

    # The daily report's nights (memory audit 9/29/26, "links"): the
    # no-shows it measured by weekday, which the DSR link joins to a
    # complaint weekday. A labor figure, so read only where Labor is.
    if _on("module_labor"):
        out["dsr"] = _safe("dsr", lambda: _dsr_nights(restaurant_id, db_path), degraded)

    out["degraded"] = degraded
    out["modules_off"] = off
    out["complete"] = not degraded
    return out


def _marketing_activity(restaurant_id, db_path=DB_PATH):
    """What marketing actually did in the last 30 days.

    Deliberately NOT joined to review volume or sales as a cause. A campaign
    and a busy week co-occurring is not evidence one produced the other, and
    this product has no covers, no attribution and no control group. It is
    carried so a cross-module answer can say what else was happening in the
    same window, which is genuinely useful and is not a causal claim.
    """
    since = (date.today() - timedelta(days=30)).isoformat()
    conn = get_conn(db_path)
    try:
        posts = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(reach),0) AS reach FROM marketing_content_log "
            "WHERE restaurant_id=? AND post_id IS NOT NULL AND date(created_at) >= ?",
            (restaurant_id, since)).fetchone()
        camps = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(sent_count),0) AS sent FROM guest_campaigns "
            "WHERE restaurant_id=? AND date(created_at) >= ?",
            (restaurant_id, since)).fetchone()
    finally:
        conn.close()
    return {"window_days": 30,
            "posts_published": int(posts["n"] or 0) if posts else 0,
            "reach": int(posts["reach"] or 0) if posts else 0,
            "campaigns_sent": int(camps["n"] or 0) if camps else 0,
            "texts_delivered": int(camps["sent"] or 0) if camps else 0,
            "fill_campaigns": _fill_campaigns(restaurant_id, since, db_path)}


def _fill_campaigns(restaurant_id, since, db_path=DB_PATH):
    """The fill-a-night text campaigns of the window — a campaign with a
    target day (guest_marketing's "Fill Tuesday" goal) that went out or is
    queued: [{"day", "sent", "total", "queued", "on"}], newest first. The
    marketing x labor link reads them. [] when none, or when the column
    is not there."""
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT target_day, COALESCE(sent_count,0) AS sent, COALESCE(total,0) AS total, "
            "COALESCE(status,'done') AS status, date(created_at) AS on_day FROM guest_campaigns "
            "WHERE restaurant_id=? AND target_day IS NOT NULL AND target_day != '' AND date(created_at) >= ? "
            "AND COALESCE(status,'done') IN ('sending','waiting','done') ORDER BY created_at DESC, id DESC",
            (restaurant_id, since)).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        queued = r["status"] in ("sending", "waiting")
        if not queued and not int(r["sent"] or 0):
            continue
        day = str(r["target_day"]).strip().capitalize()
        if day in _WEEKDAYS:
            out.append({"day": day, "sent": int(r["sent"] or 0), "total": int(r["total"] or 0),
                        "queued": queued, "on": r["on_day"]})
    return out


def _conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports): the
    readers added with the links (memory audit 9/29/26)."""
    import models
    return models.get_conn(db_path) if db_path and db_path != models.DB_PATH else models.get_conn()


# The daily report's no-show link (memory audit 9/29/26, "links"). Floors on
# the report's side, as the other kinds have on theirs: enough measured
# nights of the weekday, more than one with a no-show, and that weekday's
# no-show rate well above the other nights' — one no-show is one night.
DSR_LINK_WINDOW_DAYS = 56
DSR_LINK_MIN_NIGHTS = 3
DSR_LINK_MIN_HITS = 2
DSR_LINK_CONCENTRATION = 2.0


def _dsr_nights(restaurant_id, db_path=DB_PATH):
    """{"window_days", "start", "end", "nights", "by_weekday": {day:
    {"nights", "no_show_nights"}}, "covers": {day: {"nights", "guests",
    "hours"}}} from the daily report's measured labor.no_shows, and its
    measured guests (sales.guests) against labor hours (labor.hours) on the
    same nights, over DSR_LINK_WINDOW_DAYS (a night the report could not
    measure is absent, never zero). None when nothing was measured."""
    since = (date.today() - timedelta(days=DSR_LINK_WINDOW_DAYS)).isoformat()
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT business_date, value FROM dsr_metrics WHERE restaurant_id=? AND metric='labor.no_shows' "
            "AND business_date >= ? AND value IS NOT NULL ORDER BY business_date", (restaurant_id, since)).fetchall()
        g_rows = conn.execute(
            "SELECT business_date, metric, value FROM dsr_metrics WHERE restaurant_id=? "
            "AND metric IN ('sales.guests','labor.hours') AND business_date >= ? AND value IS NOT NULL "
            "ORDER BY business_date", (restaurant_id, since)).fetchall()
    except Exception:
        return None
    finally:
        conn.close()
    by = {}
    for r in rows:
        try:
            day = date.fromisoformat(str(r["business_date"])[:10]).strftime("%A")
            v = float(r["value"])
        except (TypeError, ValueError):
            continue
        b = by.setdefault(day, {"nights": 0, "no_show_nights": 0})
        b["nights"] += 1
        b["no_show_nights"] += 1 if v > 0 else 0
    # Guests per labor hour, per weekday (re-audit 9/29/26, CROSSMODULE-18):
    # the report measures the covers the links used to ask the owner to
    # "check". A night counts only with both figures measured and hours > 0.
    per_night = {}
    for r in g_rows:
        try:
            per_night.setdefault(str(r["business_date"])[:10], {})[r["metric"]] = float(r["value"])
        except (TypeError, ValueError):
            continue
    covers = {}
    for iso, m in per_night.items():
        if m.get("sales.guests") is None or not m.get("labor.hours"):
            continue
        try:
            day = date.fromisoformat(iso).strftime("%A")
        except ValueError:
            continue
        c = covers.setdefault(day, {"nights": 0, "guests": 0.0, "hours": 0.0})
        c["nights"] += 1
        c["guests"] += m["sales.guests"]
        c["hours"] += m["labor.hours"]
    if not by and not covers:
        return None
    dates = [str(r["business_date"])[:10] for r in rows] + list(per_night)
    return {"window_days": DSR_LINK_WINDOW_DAYS, "start": min(dates), "end": max(dates),
            "nights": sum(b["nights"] for b in by.values()), "by_weekday": by, "covers": covers}


# Guests per labor hour is stated for a weekday only with this many measured
# nights of it, and of the other nights (the DSR link's own night floor).
COVERS_MIN_NIGHTS = DSR_LINK_MIN_NIGHTS


def measured_guests(restaurant_id, weekdays=None, days=28, today=None, db_path=DB_PATH):
    """The guests the daily reports MEASURED (dsr_metrics sales.guests, and
    labor.hours on the same nights) over the last `days` days against the
    `days` before — restricted to `weekdays` when given: {"nights",
    "avg_guests", "gplh", "before_nights", "before_avg_guests",
    "before_gplh", "days"}; gplh (guests per labor hour) is None where hours
    were not measured. None below COVERS_MIN_NIGHTS measured nights now —
    a night the report could not measure is absent, never zero. The one
    reading both diagnoses' "Guests" line and the links rest on (re-audit
    9/29/26, CROSSMODULE-18). Never raises."""
    today = today or _local_today(restaurant_id)
    start = today - timedelta(days=2 * days)
    try:
        conn = _conn(db_path)
        try:
            rows = conn.execute(
                "SELECT business_date, metric, value FROM dsr_metrics WHERE restaurant_id=? "
                "AND metric IN ('sales.guests','labor.hours') AND business_date >= ? AND business_date < ? "
                "AND value IS NOT NULL", (restaurant_id, start.isoformat(), today.isoformat())).fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    nights = {}
    for r in rows:
        try:
            nights.setdefault(str(r["business_date"])[:10], {})[r["metric"]] = float(r["value"])
        except (TypeError, ValueError):
            continue
    days_set = set(weekdays or ())
    cut = (today - timedelta(days=days)).isoformat()
    win = {"now": [], "before": []}
    for iso, m in nights.items():
        if m.get("sales.guests") is None:
            continue
        try:
            if days_set and date.fromisoformat(iso).strftime("%A") not in days_set:
                continue
        except ValueError:
            continue
        win["now" if iso >= cut else "before"].append(m)

    def _read(ms):
        if not ms:
            return None, None
        g = sum(m["sales.guests"] for m in ms)
        both = [m for m in ms if m.get("labor.hours")]
        hrs = sum(m["labor.hours"] for m in both)
        return round(g / len(ms), 1), (round(sum(m["sales.guests"] for m in both) / hrs, 1) if hrs else None)
    if len(win["now"]) < COVERS_MIN_NIGHTS:
        return None
    avg, gplh = _read(win["now"])
    b_avg, b_gplh = _read(win["before"]) if len(win["before"]) >= COVERS_MIN_NIGHTS else (None, None)
    return {"nights": len(win["now"]), "avg_guests": avg, "gplh": gplh,
            "before_nights": len(win["before"]) if b_avg is not None else 0,
            "before_avg_guests": b_avg, "before_gplh": b_gplh, "days": days}


def _covers_per_hour(dsr_nights, day):
    """{"day", "nights", "gplh", "other_nights", "other_gplh"} — guests per
    labor hour the daily reports measured on `day`s against the other
    nights, or None below COVERS_MIN_NIGHTS on either side."""
    cov = (dsr_nights or {}).get("covers") or {}
    d = cov.get(day) or {}
    if int(d.get("nights") or 0) < COVERS_MIN_NIGHTS or not d.get("hours"):
        return None
    on = sum(int(v.get("nights") or 0) for w, v in cov.items() if w != day)
    og = sum(float(v.get("guests") or 0) for w, v in cov.items() if w != day)
    oh = sum(float(v.get("hours") or 0) for w, v in cov.items() if w != day)
    if on < COVERS_MIN_NIGHTS or not oh:
        return None
    return {"day": day, "nights": int(d["nights"]), "gplh": round(float(d["guests"]) / float(d["hours"]), 1),
            "other_nights": on, "other_gplh": round(og / oh, 1)}


def _dsr_no_show_day(dsr_nights, day):
    """{"nights", "hits", "other_nights", "other_hits"} when the daily
    report's no-shows concentrate on `day` past the DSR_LINK floors, else
    None."""
    by = (dsr_nights or {}).get("by_weekday") or {}
    d = by.get(day) or {}
    n, k = int(d.get("nights") or 0), int(d.get("no_show_nights") or 0)
    if n < DSR_LINK_MIN_NIGHTS or k < DSR_LINK_MIN_HITS:
        return None
    on = sum(int(v.get("nights") or 0) for w, v in by.items() if w != day)
    ok = sum(int(v.get("no_show_nights") or 0) for w, v in by.items() if w != day)
    if on and (k / n) < DSR_LINK_CONCENTRATION * (ok / on):
        return None
    return {"nights": n, "hits": k, "other_nights": on, "other_hits": ok}


def _visibility(restaurant_id, db_path=DB_PATH):
    """AI search visibility — an entire module that never reached the
    assistant's context, only a tool it had to think to call.

    STORED RUNS ONLY. client_api._do_ai_visibility looks like the obvious
    call and is the wrong one here: on a cache miss it fires six to eight
    live Perplexity queries through a 1.3s pace gate, so putting it on this
    path would make every single Ask question — "what time do we open?" —
    trigger a paid visibility run, block the answer for ~10 seconds, and eat
    the owner's own aivis rate-limit budget. The scheduled weekly job is what
    produces these rows; this reads the last one it wrote.
    """
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT ai_score, gbp_score, created_at, answered, appeared FROM ai_visibility_runs "
            "WHERE restaurant_id=? AND ai_score IS NOT NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    from ai_guard import freshness
    import notify
    # The registry's visibility rule (DH3-18): its own 21-day cut read "current"
    # for a week after every other surface called the same run out of date.
    fresh = freshness(row["created_at"], source="visibility")
    # The range travels with the point (fix I4): a score from a handful of
    # questions is only as precise as its 90% interval, and Ask quotes this.
    lo, hi = notify.visibility_range(row["appeared"], row["answered"])
    return {"ai_score": row["ai_score"], "gbp_score": row["gbp_score"],
            "ai_score_low": lo, "ai_score_high": hi, "answered": row["answered"],
            # The age travels with the score. A visibility number from six
            # weeks ago reads exactly like this morning's without it.
            "as_of": fresh["as_of"], "age_days": fresh["age_days"],
            "stale": fresh["stale"]}


# ── the links ──────────────────────────────────────────────────────────────

def _lean_days(labor):
    """Weekdays whose labor percentage runs materially below this
    restaurant's own weekday average, with the gap in points.

    Not "understaffed" — labor.py is explicit that this data cannot tell
    running-lean from running-efficient.

    Two floors, both about not reading a pattern into one shift. The period
    has to span at least MIN_PERIOD_DAYS_FOR_WEEKDAY so each weekday average
    rests on more than a single observation — dow_summary is a mean with no
    count attached, so a restaurant with nine days synced can hand back a
    "Tuesday" built from exactly one Tuesday, and a link on that is a link on
    one shift. And at least three weekdays must carry data, or the average
    the gap is measured against is itself one or two days.
    """
    if _f((labor or {}).get("period_days")) < MIN_PERIOD_DAYS_FOR_WEEKDAY:
        return {}
    dow = (labor or {}).get("dow_summary") or {}
    vals = [v for v in dow.values() if isinstance(v, (int, float)) and v > 0]
    if len(vals) < 3:
        return {}
    avg = sum(vals) / len(vals)
    return {day: round(avg - pct, 1) for day, pct in dow.items()
            if isinstance(pct, (int, float)) and pct > 0 and (avg - pct) >= LEAN_DAY_MIN_GAP_PTS}


def _heavy_days(labor):
    """Weekdays whose labor percentage runs materially ABOVE this
    restaurant's own weekday average, with the gap in points — _lean_days's
    mirror, with its floors (a period of MIN_PERIOD_DAYS_FOR_WEEKDAY, three
    weekdays with data, LEAN_DAY_MIN_GAP_PTS). The day Labor's own trim
    advice points at (memory audit 9/29/26, the marketing x labor link)."""
    if _f((labor or {}).get("period_days")) < MIN_PERIOD_DAYS_FOR_WEEKDAY:
        return {}
    dow = (labor or {}).get("dow_summary") or {}
    vals = [v for v in dow.values() if isinstance(v, (int, float)) and v > 0]
    if len(vals) < 3:
        return {}
    avg = sum(vals) / len(vals)
    return {day: round(pct - avg, 1) for day, pct in dow.items()
            if isinstance(pct, (int, float)) and pct > 0 and (pct - avg) >= LEAN_DAY_MIN_GAP_PTS}


def _cluster_days(cluster):
    """The weekday(s) a complaint cluster concentrates on, if any cleared
    review_intelligence's own concentration floor."""
    if cluster.get("weekday_pair"):
        return list(cluster["weekday_pair"]["days"])
    if cluster.get("weekday"):
        return [cluster["weekday"]["value"]]
    return []


def correlations(restaurant_id: int, data: dict = None, restaurant=None,
                 db_path: str = DB_PATH) -> list:
    """Findings that no single module could reach, each with its evidence,
    what would confirm it, and what else would explain it.

    Returns [] when nothing lines up — which is the common and correct
    outcome, and far better than a manufactured connection.
    """
    data = data or gather(restaurant_id, restaurant=restaurant, db_path=db_path)
    links = []

    reviews = data.get("reviews") or {}
    food = data.get("food_cost") or {}
    labor = data.get("labor") or {}
    clusters = reviews.get("clusters") or []

    lean = _lean_days(labor) if labor.get("is_live") else {}
    waste_day = ((food.get("weekday_waste") or {}).get("worst_day") or {}).get("weekday")
    waste_share = ((food.get("weekday_waste") or {}).get("worst_day") or {}).get("share")

    for c in clusters[:5]:
        days = _cluster_days(c)

        # ── complaints and the leanest day fall on the same weekday ──
        for day in days:
            if day in lean:
                # Evidence from the joined modules' own inputs, and the two
                # windows compared (DH3-10): complaints from June against
                # this quarter's shifts are different periods, said so.
                _periods = _window_overlap(_cluster_window(c), _labor_window(labor))
                links.append({
                    "kind": "reviews_x_labor",
                    "day": day,
                    "category": c.get("category"), "mentions": c.get("mentions"),
                    # What the link is about, for its recommendation key: one
                    # answer silences THIS pairing, not every cross-module
                    # link of the kind (H-13).
                    "subject": _link_subject(c.get("category"), day),
                    "modules": ["reviews", "labor"],
                    "claim_kind": "inferred",
                    # No superlative. _lean_days returns every day past the
                    # gap, so calling this one "the leanest" is a claim the
                    # data may not support when two days qualify — and a
                    # small false claim inside an otherwise sound finding is
                    # the kind an owner never thinks to check.
                    "headline": (f"{c['mentions']} {_cat(c['category'])} complaints concentrate on "
                                 f"{day}, which runs {lean[day]} points leaner on labor "
                                 f"than this restaurant's weekday average"
                                 + (" — from different periods" if _periods.get("different") else "")),
                    "evidence": [
                        # A weekday PAIR's share covers both days, so it must
                        # not be reported against the one day this link
                        # happens to match on — "80% on Friday" when the
                        # measured figure was 80% across Friday and Saturday
                        # is a number the owner would act on and could not
                        # reproduce.
                        f"{c['mentions']} negative reviews naming {_cat(c['category'])} over "
                        f"{c['window_days']} days, {_concentration_phrase(c)}",
                        f"{day} averages {lean[day]} points below this restaurant's own "
                        f"weekday average labor percentage",
                    ] + ([_periods["line"]] if _periods.get("different") else []),
                    "evidence_inputs": [_cluster_evidence_input(c), _labor_evidence_input(labor)],
                    "periods": "different" if _periods.get("different") else (
                        "overlapping" if _periods.get("known") else "unknown"),
                    "review_ids": (c.get("review_ids") or [])[:5],
                    # labor.py:974 — the data cannot distinguish lean from
                    # efficient, so this is never stated as a cause.
                    "not_a_cause": ("Running lean is not evidence of a service failure. This "
                                    "product has no service-time, wait-time or cover-count "
                                    "data, so the two facts sharing a day is a question, not "
                                    "a finding."),
                    "confirm_by": (f"Check whether {day} covers rose while hours stayed flat, "
                                   f"and read the {day} reviews against that shift's roster."),
                    "alternative": ("The same day may simply be the busiest, which raises both "
                                    "complaint volume and sales-per-labor-hour independently."),
                })
                # The covers the daily report measured (re-audit 9/29/26,
                # CROSSMODULE-18): where guests per labor hour is measured
                # on this weekday and the others, the link states it and no
                # longer asks the owner to check it by hand.
                _cv = _covers_per_hour(data.get("dsr"), day)
                if _cv:
                    _lk = links[-1]
                    _lk["evidence"].append(
                        f"The daily reports measured {_cv['gplh']:g} guests per labor hour on {day}s "
                        f"({_cv['nights']} nights) against {_cv['other_gplh']:g} on the other nights "
                        f"({_cv['other_nights']})")
                    _lk["evidence_inputs"].append(
                        {"n": _cv["nights"], "kind": "count", "n_full": DSR_LINK_WINDOW_DAYS // 7,
                         "basis": f"{_cv['nights']} {day}s with guests and labor hours measured"})
                    _lk["covers"] = _cv
                    _lk["not_a_cause"] = ("Running lean is not evidence of a service failure. The daily reports "
                                          "measure guests per labor hour, but there is no service-time or wait-time "
                                          "data, and a review does not say which night it was about — so the two "
                                          "facts sharing a day is a question, not a finding.")
                    _lk["confirm_by"] = (f"Read the {day} reviews against that shift's roster — {day}'s guests per "
                                         f"labor hour is already measured above.")
                break

        # ── complaints and waste land on the same weekday ──
        if waste_day and waste_day in days:
            links.append({
                "kind": "reviews_x_food_cost",
                "day": waste_day,
                "category": c.get("category"), "mentions": c.get("mentions"),
                "subject": _link_subject(c.get("category"), waste_day),
                "modules": ["reviews", "food_cost"],
                "claim_kind": "inferred",
                "headline": (f"{waste_day} carries both the {_cat(c['category'])} complaints and "
                             f"{int(_f(waste_share) * 100)}% of the week's waste"),
                "evidence": [
                    # Same rule as the labor link: a pair's share covers both
                    # days and must not be reported against the one day this
                    # link matched on.
                    f"{c['mentions']} negative reviews naming {_cat(c['category'])}, "
                    f"{_concentration_phrase(c)}",
                    f"{waste_day} holds {int(_f(waste_share) * 100)}% of waste dollars against "
                    f"an even week of {int(100 / 7)}%",
                ],
                "evidence_inputs": [_cluster_evidence_input(c), _waste_day_evidence_input(food)],
                "review_ids": (c.get("review_ids") or [])[:5],
                "confirm_by": (f"Walk {waste_day} prep: over-prepping and re-firing both show "
                               f"up as waste and as guests waiting."),
                "alternative": ("A high-volume day produces more of everything — more waste, "
                                "more reviews — without the two being connected."),
            })

        # ── a dish guests name is a dish running over recipe ──
        dish = (c.get("dish") or {}).get("value")
        if dish:
            # _as_action's shape: the driver's own words are in "what" (its
            # label) and in the evidence lines under it. A dish named by the
            # analyser can land in either, so both are checked.
            for d in ((food.get("brief") or {}).get("needs_attention_now") or []) + \
                     ((food.get("brief") or {}).get("can_wait") or []):
                haystack = [d.get("what") or ""] + [str(e) for e in (d.get("evidence") or [])]
                if any(_same_thing(dish, h) for h in haystack):
                    # The ONE menu row the complaint is about (re-audit
                    # 9/29/26, CROSSMODULE-14): the driver's dish, else the
                    # best-selling menu item the guests' words name.
                    _dk, _di = _driver_parts(d)
                    try:
                        import link_memory as _lm_menu
                        _mid, _mname = _lm_menu.resolve_menu_item(restaurant_id, dish, driver_item=_di,
                                                                  driver_kind=_dk,
                                                                  db_path=None if db_path == DB_PATH else db_path)
                    except Exception:
                        _mid, _mname = None, None
                    links.append({
                        "kind": "reviews_x_menu",
                        "dish": dish,
                        "menu_item_id": _mid, "menu_item": _mname,
                        "category": c.get("category"), "mentions": c.get("mentions"),
                        "subject": _link_subject(c.get("category"), dish),
                        "modules": ["reviews", "food_cost"],
                        "claim_kind": "inferred",
                        "headline": (f"Guests name {dish} in {c['mentions']} {_cat(c['category'])} "
                                     f"complaints, and it is also a cost driver"),
                        "evidence": [
                            f"{c['mentions']} negative reviews naming {dish}",
                            f"Food Cost ranks it at ${_f(d.get('dollars_monthly')):,.0f}/month"
                            if d.get("dollars_monthly") else "Food Cost lists it as a driver",
                        ],
                        "evidence_inputs": [_cluster_evidence_input(c),
                                            d.get("evidence_input") or _driver_evidence_input(d)],
                        "review_ids": (c.get("review_ids") or [])[:5],
                        "confirm_by": ("Weigh three plates against the recipe card. Portion "
                                       "drift shows up on both sides of this at once."),
                        "alternative": ("A popular dish appears in more complaints and carries "
                                        "more cost simply because it sells more."),
                    })
                    break

        # ── complaints on the weekday the daily report keeps logging no-shows ──
        # The DSR had no link (memory audit 9/29/26, "links"): its nightly
        # no-show count is measured per night, so a complaint weekday that
        # is also the no-show weekday is a pairing no module sees alone.
        # Both floors hold: the cluster's own concentration, and the
        # report's (_dsr_no_show_day). Never a cause.
        dn = data.get("dsr") or {}
        for day in days:
            hit = _dsr_no_show_day(dn, day)
            if not hit:
                continue
            _periods = _window_overlap(_cluster_window(c), (dn.get("start"), dn.get("end"))
                                       if dn.get("start") and dn.get("end") else None,
                                       what="the daily reports")
            links.append({
                "kind": "dsr_x_reviews",
                "day": day,
                "category": c.get("category"), "mentions": c.get("mentions"),
                "subject": _link_subject(c.get("category"), day),
                "modules": ["reviews", "dsr"],
                "claim_kind": "inferred",
                "headline": (f"{c['mentions']} {_cat(c['category'])} complaints concentrate on {day}, and the "
                             f"daily report logged a no-show on {hit['hits']} of the last {hit['nights']} {day}s"
                             + (" — from different periods" if _periods.get("different") else "")),
                "evidence": [
                    f"{c['mentions']} negative reviews naming {_cat(c['category'])} over "
                    f"{c['window_days']} days, {_concentration_phrase(c)}",
                    f"A no-show on {hit['hits']} of {hit['nights']} {day}s in the last "
                    f"{DSR_LINK_WINDOW_DAYS // 7} weeks of daily reports, against {hit['other_hits']} of "
                    f"{hit['other_nights']} other nights",
                ] + ([_periods["line"]] if _periods.get("different") else []),
                "evidence_inputs": [_cluster_evidence_input(c),
                                    {"n": hit["nights"], "kind": "count", "n_full": DSR_LINK_WINDOW_DAYS // 7,
                                     "basis": f"{hit['nights']} {day}s measured in the daily report"}],
                "periods": "different" if _periods.get("different") else (
                    "overlapping" if _periods.get("known") else "unknown"),
                "review_ids": (c.get("review_ids") or [])[:5],
                "not_a_cause": ("A no-show leaves the floor short, but a review does not say which night it "
                                "was written about, so the two sharing a weekday is a question, not a finding."),
                "confirm_by": (f"Read the {day} reviews against the {day}s the daily report logged a no-show; "
                               f"if they line up, put someone on call for {day}."),
                "alternative": (f"{day} may simply be the busiest night, which raises complaint volume and "
                                f"what one missing person costs at the same time."),
            })
            break

    # ── marketing × reviews: a posting month and a review-volume move ──
    # Marketing and Intel contributed nothing to the cross-module argument:
    # gather() read them and no link kind used them, so removing either
    # module visibly cost the others nothing. Both links below are
    # CO-MOVEMENTS with the same honesty furniture as the rest — a floor on
    # each side, what would confirm it, what else explains it — and neither
    # is a revenue attribution, which marketing has no honest data for.
    mk = data.get("marketing") or {}
    if (mk.get("posts_published") or 0) >= MIN_POSTS_FOR_LINK:
        vol = _review_volume_shift(restaurant_id, db_path)
        if vol and vol["now"] >= MIN_REVIEWS_FOR_LINK and abs(vol["pct"]) >= REVIEW_SHIFT_PCT:
            up = vol["pct"] > 0
            links.append({
                "kind": "marketing_x_reviews",
                "subject": "reviews_up" if up else "reviews_down",
                "modules": ["marketing", "reviews"],
                "claim_kind": "inferred",
                "headline": (f"{mk['posts_published']} posts went out in the last 30 days and reviews "
                             f"{'rose' if up else 'fell'} {abs(vol['pct']):.0f}% against the 30 days before"),
                "evidence": [
                    f"{mk['posts_published']} posts published in the last 30 days"
                    + (f", reaching about {mk['reach']:,}" if mk.get("reach") else ""),
                    f"{vol['now']} reviews in the last 30 days against {vol['before']} in the 30 before",
                ],
                "evidence_inputs": [
                    {"n": int(mk.get("posts_published") or 0), "kind": "posts",
                     "basis": f"{mk['posts_published']} posts in the last 30 days"},
                    {"n": int(vol["now"] or 0), "kind": "reviews",
                     "basis": f"{vol['now']} reviews in the last 30 days"}],
                "not_a_cause": ("Posts and reviews moving in the same month is a co-movement. This product "
                                "has no click or visit data tying a post to a guest who then reviewed."),
                "confirm_by": ("Look at whether the new reviews mention what the posts were about, and "
                               "whether the same weeks last year moved the same way."),
                "alternative": "A seasonal week, a holiday, or a press mention moves review volume on its own.",
            })

    # ── marketing × labor: a fill-a-night campaign on the day labor runs heaviest ──
    # (memory audit 9/29/26, "links"): Marketing texting guests to fill a
    # night and Labor's own numbers calling that night the heaviest of the
    # week point at one day from two sides. Both floors hold: the campaign
    # went out (or is queued) — a record, not a guess — and the day clears
    # _heavy_days's floors. Not a verdict on either: what the campaign does
    # to the night is not measured yet.
    heavy = _heavy_days(labor) if labor.get("is_live") else {}
    _fill_seen = set()
    for fc in (mk.get("fill_campaigns") or []):
        day = fc.get("day")
        if day not in heavy or day in _fill_seen:
            continue
        _fill_seen.add(day)
        from time_utils import mdy as _mdy_fc
        # The campaign's own night (re-audit 9/29/26, CROSSMODULE-12): once
        # it has passed, "hold any cut until the window closes" is over and
        # the link says what the night MEASURED (event_memory.campaign_night,
        # the one campaign measurement) and ends — resolved "measured", or
        # "window_closed" with nothing measured — instead of repeating
        # "not measured yet" for thirty days.
        night = _campaign_night_date(fc, restaurant_id)
        if night is not None and night < _local_today(restaurant_id):
            _end_campaign_link(restaurant_id, day, fc, night, heavy[day], db_path)
            continue
        sent_line = (f"A text to fill {day} is queued for {fc['total']:,} guests" if fc.get("queued") else
                     f"A text to fill {day} went to {fc['sent']:,} guests on {_mdy_fc(fc.get('on'))}")
        links.append({
            "kind": "marketing_x_labor",
            "day": day,
            "subject": _link_subject("fill", day),
            "modules": ["marketing", "labor"],
            "claim_kind": "inferred",
            "headline": (f"{sent_line}, and {day} runs {heavy[day]} points heavier on labor than this "
                         f"restaurant's weekday average"),
            "evidence": [sent_line,
                         f"{day} averages {heavy[day]} points above this restaurant's own weekday average labor "
                         f"percentage"],
            "evidence_inputs": [_labor_evidence_input(labor)],
            "not_a_cause": (f"A heavy labor % on a slow night is sales falling short of the crew it needs, not proof "
                            f"{day} is overstaffed — and what the campaign does to {day} is not measured yet."),
            "confirm_by": (f"Hold any cut to {day} until the campaign's window closes, then compare {day}'s sales "
                           f"and labor % with the {day}s before it."),
            "alternative": (f"The campaign may not bring enough guests to change what {day} needs; the labor "
                            f"figure is from before it went out."),
        })

    # ── intel × reviews: AI visibility and the rating moving together ──
    vis = data.get("visibility") or {}
    if vis.get("ai_score") is not None and not vis.get("stale"):
        # A drop only when the two runs' 90% ranges do not overlap — the
        # same rule as the owner's drop alert (notify._ai_visibility_drop).
        # This compared two POINT scores against 15 points, and with six
        # questions one question flipping moves the point ~17: Home could
        # state a drop the Intel page's own range called noise (CA4 F4).
        prev_run, drop = _visibility_drop(restaurant_id, db_path)
        if drop is not None and drop >= VISIBILITY_DROP_POINTS:
            # The reviews brief carries clusters and diagnoses, not the
            # trend, so read it from its own module — a first draft looked
            # for a key that does not exist and would never have fired.
            try:
                import review_intelligence as _ri
                trend = _ri.rating_trend(restaurant_id, weeks=8, db_path=db_path) or {}
            except Exception:
                trend = {}
            direction = trend.get("direction")
            # rating_trend says "declining" (review_intelligence.
            # TREND_DIRECTIONS); this checked "down" and never fired.
            if direction == "declining":
                prev = prev_run.get("ai_score")
                links.append({
                    "kind": "intel_x_reviews",
                    "subject": "visibility_down",
                    "modules": ["intel", "reviews"],
                    "claim_kind": "inferred",
                    "headline": (f"Your AI-search visibility fell {drop:.0f} points and your weekly rating "
                                 f"has been slipping over the same stretch"),
                    "evidence": [
                        f"AI visibility {prev} → {vis['ai_score']} between the last two weekly runs"
                        + (f" (ranges {prev_run.get('low')}-{prev_run.get('high')} and "
                           f"{vis.get('ai_score_low')}-{vis.get('ai_score_high')}, which do not overlap)"
                           if prev_run.get("low") is not None and vis.get("ai_score_low") is not None else ""),
                        f"Rating trend down over the last 8 weeks"
                        + (f" ({trend['first']:.1f} → {trend['latest']:.1f})"
                           if trend.get("first") and trend.get("latest") else ""),
                    ],
                    "not_a_cause": ("AI assistants weight recent rating and review volume, so a slipping "
                                    "rating can lower visibility — but a competitor's new listing or a "
                                    "profile change lowers it just as well."),
                    "confirm_by": "Re-run the visibility check after the next fortnight of reviews and see whether it recovers with the rating.",
                    "alternative": "A nearby competitor improved their profile, or Google changed what it shows.",
                })

    for link in links:
        act = link_action(link)
        if act:
            link["act"] = act
    # Kept, not rediscovered (memory audit 9/29/26, "links"): each link
    # carries its `memory` — first and last found, weeks running, whether
    # it came back — and a read that consulted a link's modules without
    # finding it stamps the miss. Never fails the read.
    try:
        import link_memory
        link_memory.observe(restaurant_id, links, consulted=link_memory.consulted_modules(data), db_path=db_path)
    except Exception as e:
        log.warning("business_intelligence: links not remembered for rid=%s: %s", restaurant_id, e)
    return links


def _local_today(restaurant_id):
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id).date()
    except Exception:
        return date.today()


def _campaign_night_date(fc, restaurant_id):
    """The night a fill campaign aims at: the first `day` on or after the
    day it went out (event_memory._campaign_nights' rule); for a queued one,
    the next `day` from today. None when unreadable."""
    try:
        idx = _WEEKDAYS.index(fc.get("day"))
    except ValueError:
        return None
    try:
        start = _local_today(restaurant_id) if fc.get("queued") else date.fromisoformat(str(fc.get("on"))[:10])
    except (TypeError, ValueError):
        return None
    return start + timedelta(days=(idx - start.weekday()) % 7)


def _end_campaign_link(restaurant_id, day, fc, night, heavy_pts, db_path=DB_PATH):
    """End the marketing x labor link for a campaign whose night has
    passed (link_memory.end): "measured" with the night's own lift against
    a typical same weekday when event_memory recorded it, else
    "window_closed". The outcome sentence is kept on the link and is what
    the schedule reads for that weekday (link_memory.link_lines). Never
    raises."""
    try:
        import event_memory
        import link_memory
        from time_utils import mdy
        key = link_key({"kind": "marketing_x_labor", "subject": _link_subject("fill", day)})
        got = event_memory.campaign_night(restaurant_id, night, db_path=None if db_path == DB_PATH else db_path)
        if got and got.get("lift_pct") is not None:
            lift = float(got["lift_pct"])
            outcome = (f"The {mdy(night)} {day} a text campaign aimed at filling measured {lift:+.0f}% sales against "
                       f"a typical {day} here — before and after, not proof. {day} had run {heavy_pts} points "
                       f"heavier on labor than the weekday average.")
            by = "measured"
        else:
            outcome = (f"The {mdy(night)} {day} a text campaign aimed at filling has passed with no sales "
                       f"measurement of the night, so what it did is not known.")
            by = "window_closed"
        link_memory.end(restaurant_id, key, by, headline=outcome,
                        detail={"outcome": outcome, "night": night.isoformat(), "lift_pct": (got or {}).get("lift_pct"),
                                "sent": fc.get("sent")},
                        db_path=None if db_path == DB_PATH else db_path)
    except Exception as e:
        log.warning("business_intelligence: campaign link not ended for rid=%s: %s", restaurant_id, e)


def link_action(link):
    """{"label", "nav"} - where the owner does something about a cross-module
    finding (friction audit U4-9): complaints on a weekday open that day's
    schedule, a waste day opens the count, a dish opens its price, a
    posting month opens Marketing, a visibility drop opens Intel. The nav
    path is nav.py's grammar; a client that cannot focus it opens the
    module. None for a kind with nowhere better than the module itself."""
    import nav
    kind = link.get("kind")
    if kind == "reviews_x_labor" and link.get("day"):
        return {"label": f"Open {link['day']}'s schedule", "nav": nav.path("labor", "schedule", day=link["day"])}
    if kind == "reviews_x_food_cost":
        return {"label": "Log or count the waste", "nav": nav.path("inventory", "count", day=link.get("day"))}
    if kind == "reviews_x_menu" and link.get("dish"):
        return {"label": f"Look at {link['dish']}'s price", "nav": nav.path("inventory", "menu", dish=link["dish"])}
    if kind == "marketing_x_reviews":
        return {"label": "Open Marketing", "nav": nav.path("marketing")}
    if kind == "intel_x_reviews":
        return {"label": "Open AI visibility", "nav": nav.path("intel")}
    if kind in ("dsr_x_reviews", "marketing_x_labor") and link.get("day"):
        return {"label": f"Open {link['day']}'s schedule", "nav": nav.path("labor", "schedule", day=link["day"])}
    return None


# Floors for the two co-movement links above. A handful of posts against a
# handful of reviews is two small numbers moving, not a pattern.
MIN_POSTS_FOR_LINK = 4
MIN_REVIEWS_FOR_LINK = 10
REVIEW_SHIFT_PCT = 30.0
VISIBILITY_DROP_POINTS = 15


def _review_volume_shift(restaurant_id, db_path=DB_PATH):
    """Reviews in the last 30 days against the 30 before, on the shared
    review time axis. None when either window is empty."""
    from models import REVIEW_TIME_AXIS_BARE
    conn = get_conn(db_path)
    try:
        now_n = conn.execute(
            f"SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
            f"AND date({REVIEW_TIME_AXIS_BARE}) >= date('now','-30 days')", (restaurant_id,)).fetchone()[0]
        before_n = conn.execute(
            f"SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
            f"AND date({REVIEW_TIME_AXIS_BARE}) >= date('now','-60 days') "
            f"AND date({REVIEW_TIME_AXIS_BARE}) < date('now','-30 days')", (restaurant_id,)).fetchone()[0]
    except Exception:
        return None
    finally:
        conn.close()
    if not before_n or not now_n:
        return None
    return {"now": int(now_n), "before": int(before_n),
            "pct": (now_n - before_n) / before_n * 100.0}


def _visibility_drop(restaurant_id, db_path=DB_PATH):
    """(previous_run, drop_points) when the last two comparable runs show a
    drop whose 90% ranges do not overlap (notify._ai_visibility_drop — the
    owner's drop alert reads the same rule), else ({}, None). previous_run
    is {"ai_score", "low", "high"}."""
    try:
        from models import last_two_ai_visibility_runs
        import notify
        runs = last_two_ai_visibility_runs(restaurant_id, db_path)
        got = notify._ai_visibility_drop(runs)
    except Exception:
        return {}, None
    if not got:
        return {}, None
    now_s, prev_s, _moved = got
    prev = runs[1]
    lo, hi = notify.visibility_range(prev.get("appeared"), prev.get("answered"))
    return {"ai_score": prev_s, "low": lo, "high": hi}, float(prev_s) - float(now_s)


def _previous_visibility(restaurant_id, db_path=DB_PATH, before=None):
    """The ai_score from the run before the latest one, or None.

    No caller since the intel x reviews link moved to _visibility_drop
    (9/24/26). Candidate for future cleanup after additional verification."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT ai_score FROM ai_visibility_runs WHERE restaurant_id=? AND ai_score IS NOT NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 2", (restaurant_id,)).fetchall()
    except Exception:
        return None
    finally:
        conn.close()
    return int(rows[1][0]) if len(rows) >= 2 else None


def _concentration_phrase(c):
    """How the cluster's weekday concentration actually reads.

    review_intelligence reports either a single dominant day or a dominant
    PAIR ("Friday and Saturday dinner" being the real shape of a weekend
    service problem). The pair's share is across both days, so it has to be
    stated that way — attributing it to whichever of the two this link
    matched on would overstate a measured figure by roughly double.
    """
    pair = c.get("weekday_pair")
    if pair:
        return (f"{int(_f(pair.get('share')) * 100)}% across "
                f"{' and '.join(pair['days'])} together")
    day = c.get("weekday")
    if day:
        return f"{int(_f(day.get('share')) * 100)}% on {day['value']}"
    return "no single day dominant"


# ── where the money is ─────────────────────────────────────────────────────

def money_at_stake(restaurant_id: int, data: dict = None, restaurant=None,
                   db_path: str = DB_PATH) -> dict:
    """Every module's monthly dollar figure, ranked together.

    Each module already computes what its own problem is worth per month.
    Nothing ranked them against each other, so an owner could read "$740 in
    cost drivers" on one tab and "$1,900 in scheduling" on another with no
    indication which to spend Tuesday on.

    A module with no figure is listed as unavailable with its reason. It is
    never entered as zero — a missing measurement ranked as $0 would push a
    real problem down the list.
    """
    data = data or gather(restaurant_id, restaurant=restaurant, db_path=db_path)
    lines, unavailable = [], []

    fc = ((data.get("food_cost") or {}).get("brief") or {}).get("money_involved") or {}
    if fc.get("monthly_at_stake") is not None:
        # Never "computed" (NS3 H3): the drivers are estimates of money
        # being spent (price, usage over recipe) and opportunities (waste,
        # menu and supplier gaps), each scaled to a month.
        _fk = [k for k, v in (fc.get("totals_by_kind") or {}).items() if v]
        lines.append({"module": "food_cost", "label": "Food cost drivers",
                      "monthly": round(_f(fc["monthly_at_stake"]), 2),
                      "claim_kind": "estimate" if _fk == ["estimate"] else "opportunity",
                      "money_kinds": fc.get("totals_by_kind") or {},
                      "basis": "ranked cost drivers, dollars per month"
                               + (" — estimated costs and opportunities, not added together elsewhere"
                                  if len(_fk) > 1 else "")})
    else:
        unavailable.append({"module": "food_cost",
                            "reason": fc.get("reason") or "no driver figure available"})

    labor = data.get("labor") or {}
    if not labor.get("is_live"):
        unavailable.append({"module": "labor", "reason": "no real shift data uploaded yet"})
    elif labor.get("potential_savings_monthly"):
        # The heaviest day a trim would start from, guarded as the one
        # thing's is (CROSSMODULE-5): a campaign aimed at filling that night
        # is said beside the figure, never contradicted by it.
        _day, _guarded = start_day(restaurant_id, {k: v for k, v in (labor.get("dow_summary") or {}).items() if v},
                                   db_path=db_path)
        lines.append({"module": "labor", "label": "Scheduling against target",
                      "monthly": round(_f(labor["potential_savings_monthly"]), 2),
                      # A gap to target projected to a month: an
                      # opportunity, never money saved (NS3 R1).
                      "claim_kind": "opportunity",
                      "basis": (f"gap above the {labor.get('labor_target', 30)}% target over "
                                f"{labor.get('period_days', 0)} days synced"),
                      "guard": ((_guarded[0]["why"] + (f" Start with {_day}." if _day else ""))
                                if _guarded else None)})
    elif labor.get("period_too_short_to_project") or \
            _f(labor.get("period_days")) < _LABOR_MIN_DAYS_TO_PROJECT:
        unavailable.append({"module": "labor",
                            "reason": "period too short to project a monthly figure"})
    else:
        # A zero is not a missing measurement here, and reporting it as "too
        # short" was simply the wrong sentence: labor at or under target has
        # nothing above target to recover, which is a result worth saying.
        unavailable.append({"module": "labor",
                            "reason": "labor is at or under target — nothing above it to recover"})

    # revenue_at_risk returns a RANGE and a direction, never a point figure —
    # it is an elasticity forecast, not a measurement. Collapsing it to one
    # number here would publish exactly the false precision that module went
    # out of its way to refuse. The midpoint is carried for ORDERING only and
    # is deliberately not what the snapshot prints.
    money = ((data.get("reviews") or {}).get("brief") or {}).get("costing_money") or {}
    upside = None
    if money.get("available"):
        low, high = _f(money.get("monthly_low")), _f(money.get("monthly_high"))
        lo, hi = min(abs(low), abs(high)), max(abs(low), abs(high))
        entry = {"module": "reviews", "label": "Rating movement",
                 "monthly": round((lo + hi) / 2, 2), "monthly_low": round(lo),
                 "monthly_high": round(hi), "is_range": True,
                 "rating_delta": money.get("rating_delta"),
                 "claim_kind": "forecast",
                 "basis": (f"{_f(money.get('rating_delta')):+.2f}★ against a "
                           f"{money.get('elasticity_low_pct')}-{money.get('elasticity_high_pct')}% "
                           f"revenue-per-star range on "
                           f"{money.get('sales_source') or 'trailing sales'}")}
        # An improving rating is money on the table, not money at stake. Ranking
        # it beside two costs to recover would tell an owner to go and fix
        # something that is already going right.
        if money.get("direction") == "upside":
            upside = entry
        else:
            lines.append(entry)
    else:
        unavailable.append({"module": "reviews",
                            "reason": money.get("reason") or "no rating movement large enough to price"})

    for l in lines + ([upside] if upside else []):
        l["key"] = f"money:{l['module']}"      # its identity in rec_ledger
    material = [l for l in lines if l["monthly"] >= MIN_MONTHLY_DOLLARS]
    material.sort(key=lambda l: -l["monthly"])
    return {
        "ranked": material,
        "upside": upside,
        "below_floor": [l for l in lines if l["monthly"] < MIN_MONTHLY_DOLLARS],
        "unavailable": unavailable,
        "floor": MIN_MONTHLY_DOLLARS,
        # Deliberately NO total. These are a measured cost, a scheduling gap
        # and an elasticity forecast — three different methods measuring three
        # different things. A sum would be the single most quotable number on
        # the screen and the least defensible one, and audit #14 already
        # caught a model inventing exactly that kind of total.
        "total_note": ("Do not add these together. They come from three different methods — "
                       "food cost drivers (estimated costs already being spent, plus waste, menu "
                       "and supplier gaps that are opportunities), a scheduling gap against "
                       "target (an opportunity), and a forecast from rating elasticity — none of "
                       "them is money saved. Quote them separately, each with its own basis."),
    }


# ── the one thing to do ────────────────────────────────────────────────────
#
# "If you only do one thing: Food cost drivers." was the morning brief's
# lead line: the top of the money ranking is a MODULE LABEL, and the food
# brief's own concrete fix ("Salmon Fillet waste above tolerance, $74/mo")
# was dropped on the way. The one thing is now always an action a person can
# start today, from the module that measured it — the top driver's own fix,
# a stored diagnosis's recommended_action, the replies that are owed — and
# they are ranked against each other by urgency x dollars, so five unanswered
# one-star reviews that Home already calls critical beat a $124/month food
# line rather than losing to it for having no dollar figure.

# Urgency weights. Critical is what Home marks critical (a guest waiting on a
# reply to a 1-2 star review); important is what two modules agree on or a
# gap against the owner's own target; normal is everything else.
URGENCY_WEIGHT = {"critical": 10.0, "important": 2.0, "normal": 1.0}
# A cross-module link found link_memory.ESCALATE_WEEKS weeks running, or back
# after it was answered or went away, counts this much more in the ranking
# (memory audit 9/29/26, "links") — below critical however long it stands.
RECURRING_LINK_WEIGHT = 2.0
# An action with no dollar figure is ranked as if it carried this much — not
# zero (a missing measurement is never $0), and not enough to beat a real
# line on its own.
UNPRICED_FLOOR = 50.0


def _link_subject(*parts) -> str:
    """What a link is about, as a key fragment ("service:friday")."""
    return ":".join(str(p or "").strip().lower()[:60] for p in parts)


def link_key(link) -> str:
    """The recommendation key for a cross-module link: its kind AND what it
    is about ("link:reviews_x_labor:service:Friday")."""
    link = link or {}
    kind = link.get("kind") or "cross"
    subject = str(link.get("subject") or "").strip()[:100]
    if not subject:
        import hashlib
        subject = hashlib.sha1(str(link.get("headline") or "").encode()).hexdigest()[:10]
    return f"link:{kind}:{subject}"


def issue_key(category) -> str:
    """The recommendation key for a review complaint theme, the same on Home,
    the brief and the ledger."""
    return f"top_issue:{str(category or '').strip()[:60]}"


def driver_key(driver) -> str:
    """A price driver is the price-spike alert's own news, so it carries that
    alert's key ("price_spike:<item>") and one answer silences both."""
    kind, item = _driver_parts(driver)
    if kind == "price" and item:
        return f"price_spike:{item}"
    label = str((driver or {}).get("label") or (driver or {}).get("what") or "")
    return f"food_cost_driver:{label[:40]}"


_DRIVER_LABELS = (("waste", r"^(.+?) waste above tolerance$"), ("portion", r"^(.+?) usage over recipe$"),
                  ("price", r"^(.+?) price up (\d+)%$"), ("sourcing", r"^(.+?) cheaper from (.+)$"),
                  ("menu", r"^(.+?) runs at ([\d.]+)% food cost$"))


def _driver_parts(driver):
    """(kind, item) of a driver, read from its label when the trimmed shape
    (fci.executive_brief's fix_first) dropped them."""
    import re
    d = driver or {}
    kind, item = d.get("kind"), d.get("item")
    if not kind:
        label = str(d.get("label") or d.get("what") or "").strip()
        for k, pat in _DRIVER_LABELS:
            m = re.match(pat, label)
            if m:
                return k, m.group(1)
    return kind, item


def driver_action(driver) -> str:
    """A food-cost driver as the thing to DO about it, verb first.

    Drivers are labelled as findings ("Salmon Fillet waste above
    tolerance"); an owner reading a list of findings has to work out the
    action for each. The kind decides the verb. A driver without its kind
    (fci.executive_brief's trimmed shape) is read from its label, whose five
    shapes cost_drivers fixes."""
    import re
    d = driver or {}
    label = str(d.get("label") or d.get("what") or "").strip()
    kind, item = _driver_parts(d)
    if not kind or not item:
        return label
    if kind == "waste":
        return f"Cut the {item} order — waste is above tolerance"
    if kind == "portion":
        return f"Check {item} portions against the recipe — usage runs over"
    if kind == "price":
        m = re.search(r"up (\d+)%", label)
        return f"Get a second quote on {item} — the price is up {m.group(1)}%" if m else f"Get a second quote on {item}"
    if kind == "sourcing":
        m = re.search(r"cheaper from (.+)$", label)
        return f"Buy {item} from {m.group(1)} — it's cheaper there" if m else f"Re-source {item}"
    if kind == "menu":
        m = re.search(r"runs at ([\d.]+)% food cost", label)
        return (f"Re-cost or reprice {item} — it runs at {m.group(1)}% food cost" if m
                else f"Re-cost or reprice {item}")
    return label


def _low_star_counts(restaurant_id, db_path=DB_PATH) -> dict:
    """{1: n, 2: n} — 1- and 2-star reviews from the last
    REPLY_OWED_MAX_AGE_DAYS with no reply, the ones Home marks critical, by
    rating. Imported history is not owed a reply."""
    from thresholds import REPLY_OWED_MAX_AGE_DAYS
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT rating, COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL AND rating <= 2 "
            "AND response_status IN ('pending','drafted') "
            "AND COALESCE(NULLIF(review_date,''), fetched_at) >= date('now', ?) GROUP BY rating",
            (restaurant_id, f"-{int(REPLY_OWED_MAX_AGE_DAYS)} days")).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    out = {1: 0, 2: 0}
    for rating, n in rows:
        if rating in out:
            out[rating] = int(n or 0)
    return out


def _low_star_waiting(restaurant_id, db_path=DB_PATH) -> int:
    """How many 1-2 star reviews are owed a reply (_low_star_counts)."""
    return sum(_low_star_counts(restaurant_id, db_path=db_path).values())


def low_star_headline(counts) -> tuple:
    """(headline, evidence) naming only the ratings actually waiting: two
    2-star reviews are "your 2 unanswered 2-star reviews", never "1- and
    2-star" (Will, 9/30/26: it read as if a 1-star had come in)."""
    ones, twos = int(counts.get(1) or 0), int(counts.get(2) or 0)
    n = ones + twos
    if not n:
        return None, None
    if ones and twos:
        head = f"Reply to your {n} unanswered low-star reviews ({ones} one-star, {twos} two-star)"
        ev = f"{ones} at 1 star and {twos} at 2 stars from the last 30 days with no reply"
    else:
        stars = 1 if ones else 2
        head = (f"Reply to your unanswered {stars}-star review" if n == 1
                else f"Reply to your {n} unanswered {stars}-star reviews")
        ev = f"{n} review{'' if n == 1 else 's'} at {stars} star{'' if stars == 1 else 's'} from the last 30 days with no reply"
    return head, ev


def one_thing_candidates(restaurant_id, data, links=None, db_path=DB_PATH) -> list:
    """Every concrete action the modules can name, each with its key,
    urgency, dollars (or None) and evidence, ranked by urgency x dollars."""
    out = []
    reviews = data.get("reviews") or {}
    reviews_brief = reviews.get("brief") or {}
    food_brief = (data.get("food_cost") or {}).get("brief") or {}
    labor = data.get("labor") or {}

    def add(key, what, why, modules, urgency="normal", dollars=None, evidence=None, claim_kind="measured",
            **extra):
        if not what:
            return
        out.append({"key": key, "what": str(what).strip().rstrip("."), "why": why, "modules": modules,
                    "urgency": urgency, "dollars_monthly": round(float(dollars), 2) if dollars else None,
                    "evidence": [e for e in (evidence or []) if e], "claim_kind": claim_kind, **extra})

    if data.get("reviews") is not None:
        head, ev = low_star_headline(_low_star_counts(restaurant_id, db_path=db_path))
        if head:
            add("urgent_reviews", head,
                "a guest who complained is waiting, and every later reader sees the silence",
                ["reviews"], urgency="critical",
                evidence=[ev],
                # A fact — replies owed — carries no confidence (B4 H5):
                # Home's urgent_reviews item carries none either.
                fact=True)

    # The link that has stood longest goes first (memory audit 9/29/26,
    # "links"): a link found weeks running, or back after it was answered
    # or went away, is the one worth the owner's attention — each read
    # used to rediscover it as new, in whatever order the clusters came.
    def _standing(l):
        m = l.get("memory") or {}
        return (not m.get("recurring"), -int(m.get("weeks_running") or 0))
    # Answered links go BEFORE the cap (re-audit 9/29/26, CROSSMODULE-3): a
    # declined link that had stood four weeks ranked first, was cut to the
    # one link candidate, then dropped as silenced — and blocked every other
    # link from leading. executive_brief passes only unanswered links; the
    # memory check here holds for any other caller.
    live = [l for l in (links or []) if not ((l.get("memory") or {}).get("resolved")
                                             or (l.get("memory") or {}).get("declined"))]
    for top in sorted(live, key=_standing)[:1]:
        # "link:<kind>:<subject>" — a bare "link:<kind>" meant one "Not for
        # us" silenced every future link of that kind for ten years (H-13).
        mem = top.get("memory") or {}
        why = top.get("headline")
        if mem.get("recurring") and mem.get("label") and why:
            why = f"{why} — {mem['label'][:1].lower()}{mem['label'][1:]}"
        add(link_key(top), top.get("confirm_by") or top.get("headline"),
            why, top.get("modules") or [], urgency="important",
            evidence=top.get("evidence"), claim_kind="inferred",
            confirm_by=top.get("confirm_by"), link_headline=top.get("headline"),
            evidence_input=link_evidence_input(top),
            recurring=bool(mem.get("recurring")), link_memory=mem or None)

    fx = food_brief.get("fix_first")
    if fx and (fx.get("what") or fx.get("label")):
        add(driver_key(fx), driver_action(fx),
            ("otherwise " + fx["if_ignored"]) if fx.get("if_ignored") else None, ["food_cost"],
            dollars=fx.get("dollars_monthly"), evidence=[fx.get("evidence")],
            claim_kind="computed",
            dollars_basis="this driver alone, from its own recorded usage and prices, per month",
            evidence_input=fx.get("evidence_input") or _driver_evidence_input(fx))
    else:
        try:
            import food_cost_intelligence as fci
            # Only a CURRENT read (memory audit 9/29/26, "stale_diagnoses"):
            # a stale or retired diagnosis's action and old dollars used to
            # become the one thing at any age.
            dg = fci.get_diagnosis(restaurant_id, db_path=db_path) \
                if data.get("food_cost") is not None else None
        except Exception:
            dg = None
        if dg and dg.get("recommended_action"):
            # The Food Cost card's own key for this diagnosis (its lead
            # driver), so one answer holds on the brief, Home and the
            # module alike; "food_diagnosis" had no subject and one answer
            # silenced every future diagnosis (M-9, H-13).
            from client_api import diagnosis_rec_key
            import rec_trust
            add(diagnosis_rec_key("diag_food", dg) or "food_diagnosis", dg["recommended_action"],
                dg.get("cause"), ["food_cost"],
                dollars=dg.get("dollars_at_stake"), evidence=[dg.get("headline")], claim_kind="inferred",
                alternative=dg.get("alternative_cause"), model_written=True,
                # THE food diagnosis input — the Food Cost card's and
                # Home's too (B1 H3/H4): weeks of counts, corroborating
                # modules, the CAPPED band (R9).
                evidence_input=rec_trust.food_diagnosis_input(restaurant_id, dg, db_path=db_path))

    rfx = reviews_brief.get("fix_first")
    if rfx and rfx.get("what"):
        cat = rfx["what"]
        _mentions = next((int(p.get("mentions") or 0) for p in (reviews_brief.get("biggest_problems") or [])
                          if p.get("category") == cat), None)
        if _mentions is None:
            import re as _re
            _m = _re.match(r"\s*(\d+)", str(rfx.get("evidence") or ""))
            _mentions = int(_m.group(1)) if _m else None
        # A current diagnosis only: a stale one's action is not the one
        # thing (memory audit 9/29/26); retired ones never reach here.
        dg = next((d for d in (reviews.get("diagnoses") or []) if d.get("category") == cat
                   and d.get("recommended_action") and not d.get("stale")), None)
        if dg:
            # The diagnosis's action carries the Reviews card's key, so an
            # answer on either holds on both (M-9).
            from client_api import diagnosis_rec_key
            import rec_trust
            add(diagnosis_rec_key("diag_review", dg) or issue_key(cat), dg["recommended_action"],
                dg.get("cause"), ["reviews"],
                evidence=[rfx.get("evidence")], claim_kind="inferred",
                alternative=dg.get("alternative_cause"), model_written=True,
                # THE review diagnosis input — the Reviews tab's and Home's
                # too (B1 H3); the sampled flag is added in
                # one_thing_confidence from the same helper.
                evidence_input=rec_trust.review_diagnosis_input(dg))
        else:
            add(issue_key(cat), f"Read the {_cat(cat)} complaints and pick one fix for this week",
                rfx.get("why"), ["reviews"], evidence=[rfx.get("evidence")], claim_kind="computed",
                evidence_input={"n": _mentions, "kind": "reviews",
                                "basis": f"{_mentions} negative reviews on this theme"})

    if labor.get("is_live") and _f(labor.get("potential_savings_monthly")) > 0:
        dow = {k: v for k, v in (labor.get("dow_summary") or {}).items() if v}
        if dow:
            # Where to START is the heaviest day nothing else is aimed at
            # filling on the next schedule (staffing_signals.trim_guard, the
            # check every trim makes — re-audit 9/29/26, CROSSMODULE-5/21):
            # "starting with Tuesday" beside a live campaign to fill that
            # Tuesday was the hero and the Labor tab contradicting each other.
            day, guarded = start_day(restaurant_id, dow, db_path=db_path)
            target = labor.get("labor_target", 30)
            what = (f"Build the next schedule to your {target:g}% target, starting with {day}" if day else
                    f"Build the next schedule to your {target:g}% target")
            why = f"{day} runs the heaviest labor % the next schedule can trim" if day else \
                "labor runs above your target"
            if guarded:
                why += " — " + guarded[0]["why"][:1].lower() + guarded[0]["why"][1:].rstrip(".")
            # Its own key: this is the WHOLE schedule's gap to target, not
            # Home's trim_day:<day> (that one day's excess, a different
            # figure) — under one key, one answer silenced both. It is the
            # same news as the money ranking's "Scheduling against target"
            # (money:labor), which a brief says once (morning_brief).
            import rec_ledger as _rl_bi
            add(_rl_bi.rec_key("schedule_to_target", f"{target:g}%"),
                what, why, ["labor"], urgency="important", start_day=day,
                trim_guards=[{"day": g["day"], "why": g["why"]} for g in guarded] or None,
                dollars=labor.get("potential_savings_monthly"),
                evidence=[f"gap above the {target:g}% target over {labor.get('period_days', 0)} days synced"],
                claim_kind="computed", same_as="money:labor",
                # Three labor dollar figures can sit on one Home page; each
                # says its scope (CA4 F14).
                dollars_basis="the whole schedule's gap to your target, per month",
                evidence_input=_labor_evidence_input(labor))

    for c in out:
        c["score"] = URGENCY_WEIGHT.get(c["urgency"], 1.0) * max(c["dollars_monthly"] or 0.0, UNPRICED_FLOOR)
        if c.get("recurring"):
            # Escalated, never made critical (critical is a guest waiting):
            # two modules agreeing week after week is that many readings,
            # not one (link_memory.ESCALATE_WEEKS).
            c["score"] = round(c["score"] * RECURRING_LINK_WEIGHT, 2)
    out.sort(key=lambda c: -c["score"])
    return out


def start_day(restaurant_id, dow, db_path=DB_PATH) -> tuple:
    """(day or None, guarded): the heaviest weekday of `dow` ({day: labor
    %}) that no live campaign or post is aimed at filling on the NEXT
    schedule — staffing_signals.trim_guard, on the date that weekday falls
    in the week the next draft covers (next_draft_date; CROSSMODULE-21) —
    and the heavier days it passed over, each {day, why}. None when every
    day is guarded. Never raises: a guard that cannot be read guards
    nothing, as on Home."""
    guarded = []
    for day, _pct in sorted(((k, v) for k, v in (dow or {}).items() if v), key=lambda kv: -kv[1]):
        try:
            import staffing_signals
            g = staffing_signals.trim_guard(restaurant_id, day,
                                            on_date=staffing_signals.next_draft_date(restaurant_id, day),
                                            db_path=None if db_path == DB_PATH else db_path)
        except Exception as e:
            log.warning("business_intelligence: trim guard unavailable for rid=%s: %s", restaurant_id, e)
            g = {}
        if g.get("suppress"):
            guarded.append({"day": day, "why": g.get("why") or f"a campaign is aimed at filling {day}"})
            continue
        return day, guarded
    return None, guarded


# A link whose two figures cover different periods is held here: the two
# facts may never have been true at the same time (DH3-10).
DIFFERENT_PERIODS_CAP = 49


def _iso(v):
    s = str(v or "").strip()[:10]
    return s if len(s) == 10 and s[4] == "-" and s[7] == "-" else None


def _cluster_window(c):
    """(first, last) ISO dates of a complaint cluster's reviews, or None."""
    a, b = _iso((c or {}).get("first_seen")), _iso((c or {}).get("last_seen"))
    return (a, b) if a and b else None


def _labor_window(labor):
    """(start, end) ISO dates of the labor analysis's shifts, or None."""
    dr = (labor or {}).get("date_range") or {}
    a, b = _iso(dr.get("start")), _iso(dr.get("end"))
    return (a, b) if a and b else None


def _window_overlap(a, b, what="the labor figures") -> dict:
    """{known, different, line}: whether two (start, end) windows share any
    day, and the owner's line naming both when they do not."""
    if not a or not b:
        return {"known": False, "different": False, "line": None}
    different = a[1] < b[0] or b[1] < a[0]
    from time_utils import mdy_range
    line = (f"Different periods: the complaints run {mdy_range(a[0], a[1])}, {what} "
            f"{mdy_range(b[0], b[1])} — the two were never measured over the same days") if different else None
    return {"known": True, "different": different, "line": line}


def _cluster_evidence_input(c) -> dict:
    """A complaint cluster's own Evidence Strength input: its mentions."""
    n = int((c or {}).get("mentions") or 0)
    return {"n": n, "kind": "reviews",
            "basis": f"{n} reviews on {_cat((c or {}).get('category'))} over {(c or {}).get('window_days') or 90} days"}


def _waste_day_evidence_input(food) -> dict:
    """The worst waste day's own input: the waste events behind its share."""
    wd = ((food or {}).get("weekday_waste") or {})
    worst = wd.get("worst_day") or {}
    n = int(worst.get("events") or 0)
    per = int(wd.get("min_events_per_bucket") or 4)
    return {"n": n, "kind": "count", "n_full": max(1, per * 2),
            "basis": f"{n} waste events on {worst.get('weekday') or 'that day'} over {wd.get('window_days') or 56} days"}


def link_evidence_input(link) -> dict:
    """A cross-module link's Evidence Strength input — the one input for the
    same link as the one-thing candidate and as a "What connects" card (T1),
    so both surfaces show one figure. Two modules moving together is an
    inference, never a measured cause.

    Its evidence is the WEAKER of the joined modules' own inputs (DH3-10:
    `evidence_inputs` — the cluster's mentions, the labor window's trading
    days, the waste day's events, the driver's own evidence): two sentences
    read the same whether they rested on 3 reviews and 9 days or on 40 and
    90. A link whose windows do not overlap is "different periods" and held
    to DIFFERENT_PERIODS_CAP. A link without inputs (an older payload)
    counts its cited figures, as before."""
    import confidence_engine as ce
    link = link or {}
    mods = [m for m in (link.get("modules") or []) if m]
    corr = max(0, len(set(mods)) - 1)
    inputs = [i for i in (link.get("evidence_inputs") or []) if isinstance(i, dict) and i.get("n") is not None]
    if inputs:
        scored = []
        for i in inputs:
            try:
                scored.append((ce.evidence(**i).get("pct") or 0, i))
            except TypeError:
                continue
        if scored:
            _pct, weakest = min(scored, key=lambda t: t[0])
            out = dict(weakest)
            out["flags"] = tuple(dict.fromkeys(tuple(out.get("flags") or ()) + ("inferred",)))
            out["corroborating"] = corr
            out["basis"] = (f"{str(weakest.get('basis') or 'its weaker input').rstrip('.')} — the weaker of the "
                            f"{len(scored)} inputs it joins; {len(mods)} modules moving together, an inference")
            if link.get("periods") == "different":
                out["cap"] = min(float(out.get("cap", 100)), DIFFERENT_PERIODS_CAP)
                out["cap_reason"] = "its two figures cover different periods"
            return out
    # Each module past the first is an independent reading that agrees —
    # bounded corroboration (B4 M2); the inferred cap still holds the causal
    # wording under "high".
    return {"n": len([e for e in (link.get("evidence") or []) if e]), "kind": "evidence_items",
            "flags": ("inferred",), "corroborating": corr,
            "basis": f"{len(mods)} modules moving together — an inference, "
                     "not a measured cause"}


def link_confidence(restaurant_id, link, key=None, db_path=DB_PATH, ctx=None) -> dict:
    """The K1 confidence of a "What connects" link (T1): link_evidence_input,
    this restaurant's record of the link kind, the freshness of the modules
    it joins. Never raises."""
    return one_thing_confidence(restaurant_id, {"key": key or link_key(link), "modules": (link or {}).get("modules"),
                                                "evidence_input": link_evidence_input(link)},
                                db_path=db_path, ctx=ctx)


def _driver_evidence_input(fx):
    """A food fix_first's evidence input when the brief did not carry it."""
    try:
        import food_cost_intelligence as fci
        kind, item = _driver_parts(fx)
        return fci.driver_evidence(dict(fx, kind=kind, item=item))
    except Exception:
        return {"n": None, "basis": "the ledger"}


def _labor_evidence_input(labor):
    """Evidence for a whole-schedule labor claim: the trading days with
    sales, the share of shift days that carry sales, and the analysis's
    partial-data flags (CA3 F4)."""
    days = int((labor.get("date_range") or {}).get("days") or labor.get("period_days") or 0)
    missing = len(labor.get("days_missing_sales") or [])
    flags = tuple(f for f, on in (("days_missing_sales", missing),
                                  ("hours_are_estimated", labor.get("hours_are_estimated")),
                                  ("days_with_conflicting_sales", labor.get("days_with_conflicting_sales")))
                  if on)
    return {"n": max(0, days - missing), "kind": "trading_days",
            "coverage": ((days - missing) / float(days)) if days else None, "flags": flags,
            "basis": f"{max(0, days - missing)} days of shifts with sales" + (f" of {days}" if missing else "")}


def one_thing_confidence(restaurant_id, c, db_path=DB_PATH, ctx=None):
    """The K1 confidence of a one-thing candidate (the Home hero, CA1 H8 —
    it carried none that any surface showed), from its own evidence input
    and the sources of the modules it rests on. A FACT candidate (replies
    owed) carries none: None (B4 H5). Never raises."""
    if c.get("fact"):
        return None
    try:
        import rec_trust
        import data_freshness
        mods = ["inventory" if m == "food_cost" else m for m in (c.get("modules") or [])]
        ev = c.get("evidence_input")
        if ev and "reviews" in mods:
            # The Places "sampled" flag, from the one helper every review
            # surface reads (re-audit B3#7, B4 M1).
            ctx = ctx or rec_trust.Context(restaurant_id, db_path=db_path)
            fl = data_freshness.review_evidence_flags(ctx.row())
            if fl:
                ev = dict(ev, flags=tuple(dict.fromkeys(tuple(ev.get("flags") or ()) + fl)))
        return rec_trust.assess(restaurant_id, c.get("key") or "", evidence=ev,
                                sources=data_freshness.sources_for(mods), db_path=db_path, ctx=ctx)
    except Exception as e:
        log.warning("one thing: confidence unavailable: %s", e)
        import confidence_engine
        return confidence_engine.unknown()


def pick_one_thing(restaurant_id, candidates, db_path=DB_PATH, learned=None, ctx=None, log_rank=True):
    """The top candidate the owner has not already answered, and whose kind
    they have not stopped answering (decisions.quiet_kinds) — a "no" on Home
    is a no here too, and a kind ignored four times running never leads.

    Candidates are weighed by what this restaurant's own answers and
    measured results taught the ledger (rec_learning.effectiveness, a
    bounded 0.6–1.25× on the score; ROI #24, #29, #47) and re-sorted —
    except that a critical candidate keeps exactly its place: the weights
    reorder only the non-critical candidates between critical ones, so
    nothing learned can put anything ahead of a critical item or move one
    down. `learned` may be passed in (tests, a caller that already built
    it)."""
    if not candidates:
        return None
    try:
        import rec_ledger
        import decisions
        silenced = rec_ledger.silenced_keys(restaurant_id, db_path=db_path)
        quiet = decisions.quiet_kinds(restaurant_id, db_path=db_path)
    except Exception as e:
        log.warning("one thing: ledger unavailable: %s", e)
        silenced, quiet = set(), set()
    if learned is None:
        try:
            import rec_learning
            learned = rec_learning.effectiveness(restaurant_id, db_path=db_path)
            # The day's holdout arm (LOOPS-3): neutral weights on held-out days.
            learned = rec_learning.apply_holdout(learned, restaurant_id, "one_thing")
        except Exception as e:
            log.warning("one thing: effectiveness unavailable: %s", e)
    if learned is not None:
        ordered, run = [], []

        def flush():
            ordered.extend(sorted(run, key=lambda x: -(x.get("score") or 0)))
            run.clear()
        import rec_learning as _rl_rank
        for c in candidates:
            c = dict(c)
            if c.get("urgency") == "critical":
                flush()
                ordered.append(c)
                continue
            # A kind the restaurant's own record says to stop proposing is
            # never the one thing (memory audit 9/29/26, "thresholds").
            try:
                if hasattr(learned, "held") and learned.held(rec_ledger.kind_of(str(c.get("key") or ""))):
                    continue
            except Exception as e:
                log.warning("one thing: hold check failed for %s: %s", c.get("key"), e)
            base = float(c.get("score") or 0)
            info = _rl_rank.weigh(learned, c["key"], title=c.get("what"))
            w, why = info["weight"], info["why"]
            if w != 1.0:
                c["score"] = round(float(c.get("score") or 0) * w, 2)
                # The weight, why, and the prior rung and model version it
                # rested on (rec_learning.learned_note, PLATFORM-1/3).
                c["learned"] = _rl_rank.learned_note(learned, c["key"], w, why)
            # What learning did to its rank, logged with the showing
            # (memory audit 9/29/26, rank_log).
            c["rank"] = _rl_rank.rank_meta(c, base, info)
            run.append(c)
        flush()
        candidates = ordered
    # Advice pulling against other advice (memory audit 9/29/26,
    # "conflicts"): a candidate the owner settled against is held; the
    # weaker of two that conflict carries `conflict` to the hero.
    try:
        import lever_conflicts
        rest = lever_conflicts.apply(restaurant_id, [c for c in candidates if c.get("urgency") != "critical"],
                                     lever_conflicts.facts(restaurant_id, db_path=db_path), db_path=db_path)
        keep = {c.get("key") for c in rest}
        by_key = {c.get("key"): c for c in rest}
        candidates = [c if c.get("urgency") == "critical" else by_key[c.get("key")]
                      for c in candidates if c.get("urgency") == "critical" or c.get("key") in keep]
    except Exception as e:
        log.warning("one thing: lever conflicts unavailable: %s", e)
    # "Not for us" to the same advice on any surface (H16) — the nightly
    # report's Tuesday cut declined is this hero's Tuesday cut declined.
    # Read once, only if something non-critical could lead.
    declined_sigs = None
    for c in candidates:
        if c["key"] in silenced:
            continue
        try:
            import insight_store
            sig = insight_store.advice_signature(c["key"], c.get("what"))
        except Exception:
            sig = None
        if sig and c.get("urgency") != "critical":
            if declined_sigs is None:
                try:
                    import insight_store
                    declined_sigs = insight_store.declined_signatures(restaurant_id, db_path=db_path)
                except Exception as e:
                    log.warning("one thing: declined signatures unavailable: %s", e)
                    declined_sigs = set()
            if sig in declined_sigs:
                continue
        # A quiet kind never leads — unless the candidate is critical: the
        # owner going quiet on a kind is not an answer to an emergency, and
        # "reply to the 1-star reviews" skipped for "post this week" was
        # the defect (re-audit B7).
        if c["key"].split(":", 1)[0] in quiet and c.get("urgency") != "critical":
            continue
        if c.get("same_as") and c["same_as"] in silenced:
            continue            # answered under the other name for the same news
        out = dict(c)
        # The hero carries its measured confidence (K4), and says whether
        # the model wrote it.
        # `ctx` is the build's rec_trust.Context when the caller has one, so
        # the hero and What connects give one key one figure (B4 M1).
        out["confidence"] = one_thing_confidence(restaurant_id, out, db_path=db_path, ctx=ctx)
        out["model_written"] = bool(out.get("model_written"))
        out["advice_signature"] = sig
        # The owner's "not for us" to the OPPOSITE advice on the same night
        # is evidence for this one, said beside it — never a silence of it
        # (re-audit 9/29/26, CROSSMODULE-1): "we never cut Tuesday" agrees
        # with "hold any cut to Tuesday".
        agrees = _agrees_with_decline(restaurant_id, sig, db_path=db_path)
        if agrees:
            out["agrees_with"] = agrees
        # Its dollars beside the calibrated figure, the rule Home cards
        # follow (rec_learning.attach_dollar_calibration, F6).
        try:
            import rec_learning
            rec_learning.attach_dollar_calibration(out, learned)
        except Exception as e:
            log.warning("one thing: dollar calibration unavailable: %s", e)
        # The pick and the candidates it beat, logged once a day (rank_log):
        # acceptance of the hero can be read against what was not shown.
        if log_rank:
            try:
                import rec_ledger
                idx = next((i for i, x in enumerate(candidates) if x.get("key") == c.get("key")), 0)
                rest = [x for x in candidates[idx + 1: idx + 8] if x.get("key")]
                rec_ledger.log_rank_build(restaurant_id, "one_thing",
                                          shown=[dict(out.get("rank") or {}, key=out["key"])],
                                          not_shown=[dict(x.get("rank") or {}, key=x["key"]) for x in rest],
                                          version=getattr(learned, "version", None), db_path=db_path,
                                          arm=getattr(learned, "arm", None))
            except Exception as e:
                log.warning("one thing: rank log unavailable: %s", e)
        return out
    return None


def _agrees_with_decline(restaurant_id, sig, db_path=DB_PATH):
    """{"signature", "on", "title", "text"} when the owner declined the
    opposite direction of the same staffing advice (a trim, against a hold
    or an add; a hold or an add, against a trim), else None. Never
    raises."""
    try:
        import insight_store
        base, direction = insight_store.split_signature(sig)
        if not sig or base.split(":", 1)[0] != "labor":
            return None
        opposite = [base] if direction else [f"{base}:{d}" for d in insight_store.DIRECTIONS]
        declines = insight_store.declines_by_signature(restaurant_id, db_path=db_path)
        hit = next(((o, declines[o]) for o in opposite if o in declines), None)
        if not hit:
            return None
        from time_utils import mdy
        on = mdy(str(hit[1].get("on") or "")[:10])
        return {"signature": hit[0], "on": on, "title": hit[1].get("title"),
                "text": f"You passed on the opposite advice ({hit[1].get('title') or 'the other way'}) on {on} — "
                        f"this agrees with that."}
    except Exception as e:
        log.warning("one thing: opposite decline unreadable: %s", e)
        return None


# ── the one brief ──────────────────────────────────────────────────────────

def unanswered_links(restaurant_id, links, viewer=None, db_path=DB_PATH) -> tuple:
    """(open, answered): `links` split by whether the owner has answered
    them — resolved in link_memory (Done, the change made, "not for us",
    measured), silenced by an answer on any surface (rec_ledger.
    silenced_keys, and `viewer`'s own), or the same advice declined
    elsewhere (insight_store.declined_signatures, direction and all).

    The ONE filter every reader of the brief's links goes through (re-audit
    9/29/26, CROSSMODULE-2): Ask's snapshot, read_business_snapshot and the
    schedule draft used to re-raise a link the owner had declined, and the
    draft was told to "reflect this". Declined links reach Ask only through
    the link history (link_memory.history), labelled. Never raises."""
    links = list(links or [])
    if not links:
        return [], []
    try:
        import rec_ledger
        silenced = rec_ledger.silenced_keys(restaurant_id, db_path=db_path, viewer=viewer)
    except Exception as e:
        log.warning("business_intelligence: silences unavailable for rid=%s: %s", restaurant_id, e)
        silenced = set()
    declined = None
    keep, answered = [], []
    for l in links:
        m = l.get("memory") or {}
        key = link_key(l)
        if m.get("resolved") or m.get("declined") or key in silenced:
            answered.append(l)
            continue
        try:
            import insight_store
            sig = insight_store.advice_signature(key, l.get("confirm_by") or l.get("headline"))
            if sig:
                if declined is None:
                    declined = insight_store.declined_signatures(restaurant_id, db_path=db_path)
                if sig in declined:
                    answered.append(l)
                    continue
        except Exception as e:
            log.warning("business_intelligence: link signature unreadable: %s", e)
        keep.append(l)
    return keep, answered


def executive_brief(restaurant_id: int, restaurant=None, db_path: str = DB_PATH, ctx=None, viewer=None,
                    log_rank: bool = False) -> dict:
    """One cross-module read: where the money is, what connects, what to do
    first, and what could not be answered.

    Deterministic — no model runs here. Everything is either measured by a
    module or explicitly reported as unavailable.

    `log_rank` logs the one thing's pick and the candidates it beat as the
    day's "one_thing" rank build (pick_one_thing). Only the owner's Home
    passes it (strategy_routes._do_cross_module): every other caller — Ask,
    a manager's view, the schedule engine, the morning brief per recipient,
    the weekly and monthly reviews — overwrote the day's row with a
    candidate set the owner never saw (memory re-audit 9/29/26,
    CROSSMODULE-13).

    `links` are the links the owner has NOT answered (unanswered_links —
    `viewer`'s own answers too, when a login is given); `links_answered`
    counts the rest, which only the link history names.

    Inside question_memo() (an Ask question) it is computed once per
    restaurant, viewer scope and database (#33) — never when it logs the
    day's rank or reads a caller's ranking context.
    """
    if ctx is None and not log_rank:
        return _memoised(("brief", int(restaurant_id), db_path, _viewer_scope(restaurant, viewer)),
                         lambda: _executive_brief(restaurant_id, restaurant=restaurant, db_path=db_path,
                                                  viewer=viewer))
    return _executive_brief(restaurant_id, restaurant=restaurant, db_path=db_path, ctx=ctx, viewer=viewer,
                            log_rank=log_rank)


def _executive_brief(restaurant_id, restaurant=None, db_path=DB_PATH, ctx=None, viewer=None, log_rank=False):
    data = gather(restaurant_id, restaurant=restaurant, db_path=db_path)
    found = correlations(restaurant_id, data=data, db_path=db_path)
    links, answered_links = unanswered_links(restaurant_id, found, viewer=viewer, db_path=db_path)
    money = money_at_stake(restaurant_id, data=data, db_path=db_path)

    reviews_brief = (data.get("reviews") or {}).get("brief") or {}
    food_brief = (data.get("food_cost") or {}).get("brief") or {}

    # What to do first, across modules rather than within one: a concrete
    # action, never a module label, ranked by urgency x dollars.
    candidates = one_thing_candidates(restaurant_id, data, links, db_path=db_path)
    first = pick_one_thing(restaurant_id, candidates, db_path=db_path, ctx=ctx, log_rank=log_rank)

    unanswered = []
    for m in data.get("modules_off", []):
        unanswered.append(f"{m} is not on this plan")
    for m in data.get("degraded", []):
        unanswered.append(f"{m} could not be read this time")
    for u in money.get("unavailable", []):
        unanswered.append(f"{u['module']}: {u['reason']}")

    return {
        "fix_first": first,
        "links": links,
        "links_answered": len(answered_links),
        "money": money,
        "reviews": _trim_reviews(reviews_brief),
        "food_cost": _trim_food(food_brief),
        "labor": _trim_labor(data.get("labor") or {}),
        "marketing": data.get("marketing"),
        "visibility": data.get("visibility"),
        # The daily report's measured nights the DSR link reads.
        "dsr": data.get("dsr"),
        "modules_consulted": [k for k in ("reviews", "food_cost", "labor", "marketing", "visibility", "dsr")
                              if data.get(k)],
        "modules_off": data.get("modules_off", []),
        "degraded": data.get("degraded", []),
        "complete": data.get("complete", True),
        "unanswered": unanswered,
    }


def _trim_reviews(b):
    if not b:
        return None
    return {"biggest_problems": (b.get("biggest_problems") or [])[:3],
            "fix_first": b.get("fix_first"), "trend": b.get("trend"),
            "costing_money": b.get("costing_money"), "coverage": b.get("coverage")}


def _trim_food(b):
    if not b:
        return None
    return {"why": b.get("why"), "fix_first": b.get("fix_first"),
            "needs_attention_now": b.get("needs_attention_now"),
            "food_cost": b.get("food_cost"), "profitability": b.get("profitability"),
            "trust": b.get("trust"), "worsened": b.get("worsened")}


def _trim_labor(a):
    if not a or not a.get("is_live"):
        return {"is_live": False}
    return {"is_live": True, "labor_pct": a.get("overall_labor_pct"),
            "target": a.get("labor_target"),
            "monthly_opportunity": a.get("potential_savings_monthly"),
            "overstaffed_days": len(a.get("overstaffed_days") or []),
            "dow_summary": a.get("dow_summary"),
            "period_days": a.get("period_days")}


# ── the text the assistant reads ───────────────────────────────────────────

# The section's first line — what Ask strips when a turn already carries
# read_business_snapshot's full result (ask_cavnar._without_across).
SNAPSHOT_HEADER = "ACROSS THE BUSINESS (computed, not written by a model)"


def snapshot_block(restaurant_id: int, restaurant=None, db_path: str = DB_PATH) -> str:
    """The cross-module section of Ask Cavnar's context snapshot.

    Short on purpose. The full brief is a tool call away; this exists so the
    model opens every conversation already knowing where the money is and
    what lines up, instead of only finding out when it happens to call the
    right tool.
    """
    try:
        brief = executive_brief(restaurant_id, restaurant=restaurant, db_path=db_path)
    except Exception as e:
        log.warning("business_intelligence snapshot failed: %s", e)
        return ""

    lines = [SNAPSHOT_HEADER]

    money = brief.get("money") or {}
    ranked = money.get("ranked") or []
    if ranked:
        lines.append("- Monthly dollars at stake, ranked:")
        for r in ranked:
            # A range prints as a range. The midpoint exists to order this
            # list and is never shown, because it is not a figure anyone
            # measured.
            amount = (f"${r['monthly_low']:,.0f}-${r['monthly_high']:,.0f}"
                      if r.get("is_range") else f"${r['monthly']:,.0f}")
            lines.append(f"    {amount} — {r['label']} ({r['module']}; {r['basis']})")
        lines.append(f"    {money.get('total_note')}")
    if money.get("upside"):
        u = money["upside"]
        lines.append(f"- Going the right way: {u['label']} is worth "
                     f"${u['monthly_low']:,.0f}-${u['monthly_high']:,.0f}/month of UPSIDE "
                     f"({u['basis']}) — not a problem to fix.")

    if brief.get("links"):
        lines.append("- What lines up across modules:")
        for l in brief["links"][:3]:
            # How long it has stood (link_memory), so Ask can say a link
            # recurs rather than presenting it as new each time.
            _m = l.get("memory") or {}
            lines.append(f"    {l['headline']}" + (f" ({_m['label']})" if _m.get("label") else ""))
            lines.append(f"      confirm by: {l.get('confirm_by')}")
            lines.append(f"      could also be: {l.get('alternative')}")
            if l.get("not_a_cause"):
                lines.append(f"      NOT a cause: {l['not_a_cause']}")
    elif brief.get("modules_consulted"):
        lines.append("- Nothing lines up across modules right now. Say that plainly rather "
                     "than connecting two findings yourself.")
    if brief.get("links_answered"):
        # Answered links are left out above (CROSSMODULE-2); the history
        # names them, labelled (CROSSMODULE-15).
        n = int(brief["links_answered"])
        lines.append(f"- {n} other cross-module link{'s' if n != 1 else ''} the owner already answered (done, "
                     f"declined or ended) {'are' if n != 1 else 'is'} left out. Do not raise "
                     f"{'them' if n != 1 else 'it'} as new; read_restaurant_memory's link history says what "
                     f"each was and how it was answered.")

    if brief.get("fix_first"):
        f = brief["fix_first"]
        lines.append(f"- If they only do one thing: {f.get('what')} "
                     f"({', '.join(f.get('modules') or [])}) — {f.get('why')}"
                     + (f" (the owner passed on the opposite advice on {f['agrees_with']['on']}; this agrees "
                        f"with that)" if (f.get("agrees_with") or {}).get("on") else ""))

    vis = brief.get("visibility")
    if vis and vis.get("ai_score") is not None:
        age = (f", measured {vis['as_of']}" + (" — out of date" if vis.get("stale") else "")
               if vis.get("as_of") else "")
        lines.append(f"- AI search visibility: {vis['ai_score']}{age}")

    mkt = brief.get("marketing")
    if mkt and (mkt.get("posts_published") or mkt.get("campaigns_sent")):
        lines.append(f"- Marketing in the same 30 days: {mkt['posts_published']} posts published, "
                     f"{mkt['campaigns_sent']} text campaigns. This is what ELSE was happening — "
                     f"it is not evidence any of it caused the numbers above.")

    # Only worth the tokens when it actually said something. A restaurant on
    # one module would otherwise get a header and a list of the modules it
    # does not have, on every question, forever — so "could not be answered"
    # is appended only when there is something above it to qualify, and
    # modules the client simply does not own are not a gap worth naming.
    if len(lines) == 1:
        return ""
    gaps = [u for u in (brief.get("unanswered") or []) if "not on this plan" not in u]
    if gaps:
        lines.append(f"- Could not be answered: {'; '.join(gaps[:4])}")
    return "\n".join(lines) + "\n"
