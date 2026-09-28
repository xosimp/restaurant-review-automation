"""marketing_opportunities.py — the Marketing Opportunity Feed (9/28/26).

Opening Marketing used to start at a blank prompt: the owner had to think of
every campaign. The signals that say what to do next already existed, spread
across other modules — a reliably slow weekday (demand), last year's holiday
night (schedule_economics), a POS category falling (the nightly report), a
high-margin dish nobody orders (the dish scorecard), a dish guests praise
(reviews), a list nobody has contacted, nothing posted in a fortnight. This
gathers them into ranked cards, each with a one-tap draft for the Campaign
Studio.

Deterministic: no model call and no network on build (the drafting happens
when the owner taps, through the Studio's own rate-limited routes). Every
figure is measured and carries its basis. A dollar figure is a measured GAP
— what the slow night or the category is short of — labelled as such, never
an expected return: no campaign's effect here has been measured against a
holdout yet, so nothing predicts one (marketing audit 9/28/26, DATA-6/§5).
Opportunities the data cannot support honestly are not shown at all:
birthdays (none stored), lapsed guests ("last visit" is a sign-up, not a POS
visit — DATA-1), weather-driven takeout (no channel split or observed weather
— DATA-5).

Kinds, as rec_ledger keys (one answer silences a card on every surface):
  slow_day:<Weekday>        a reliably slow weekday in the next 7 days
  holiday_promo:<ISO date>  a holiday in the next 21 days, with last year's
                            measured night when there is one
  category_dip:<Category>   a POS category down past its own weekly swing
  dish_promote:<Dish>       earns above the menu's median, sells below it
  dish_praise:<Dish>        named positively in reviews, never negatively
  list_idle:text / :email   opted-in guests not contacted in 30+ days
  post_this_week            nothing live in 10+ days (Home's own key)
"""
import hashlib
import json
from datetime import date, datetime, timedelta
from statistics import pstdev

import models as _models
from models import DB_PATH

CACHE_KIND = "mkt_opps"
VERSION = 1

SLOW_LOOKAHEAD_DAYS = 7
SLOW_CARDS = 2
HOLIDAY_LOOKAHEAD_DAYS = 21
HOLIDAY_UNMEASURED_DAYS = 14      # a holiday with no history of its own: only when close
HOLIDAY_MOVE_PCT = 10             # last year's night this far from a typical one is worth acting on
CATEGORY_WINDOW_DAYS = 28
CATEGORY_MIN_NIGHTS = 20          # measured nights in each 28-night window
CATEGORY_MIN_BASE = 500.0         # dollars over the earlier window
CATEGORY_MIN_DROP_PCT = 10        # the floor under the category's own swing
PRAISE_MIN_MENTIONS = 3
DISH_CARDS = 2
LIST_IDLE_DAYS = 30
TEXT_LIST_MIN = 25
EMAIL_LIST_MIN = 10
POST_IDLE_DAYS = 10               # marketing_signals.QUIET_AFTER_DAYS
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
ALL_CHANNELS = ("text", "email", "social")


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    return _models.get_conn(db_path) if db_path is not None else _models.get_conn()


def _key(kind, subject=None):
    import rec_ledger
    return rec_ledger.rec_key(kind, subject)


def _money(v):
    return f"${float(v):,.0f}"


def _mdy(d):
    from time_utils import mdy
    return mdy(d.isoformat() if hasattr(d, "isoformat") else d)


def _local_today(restaurant_id):
    import demand
    return demand.local_today(restaurant_id)


def _card(key, kind, title, why, *, facts=(), stake=None, when=None, days_away=None, prompt="",
          channels=ALL_CHANNELS, evidence=None, sources=(), score=0):
    """One opportunity. `evidence` None means a fact (a list, a posting gap):
    it carries no confidence, never a stand-in figure. `stake` is a measured
    gap {"amount", "label"}, never an expected return."""
    return {"key": key, "kind": kind, "title": title, "why": why, "facts": [f for f in facts if f],
            "stake": stake, "when": when.isoformat() if hasattr(when, "isoformat") else when,
            "days_away": days_away, "action": {"prompt": prompt[:280], "channels": list(channels)},
            "evidence": evidence, "sources": list(sources), "score": round(float(score), 2)}


# ── the signals ─────────────────────────────────────────────────────────────

def slow_nights(restaurant_id, today, db_path=DB_PATH):
    """A weekday that runs reliably under a typical day (demand.slow_days:
    15% under, 3+ samples, 75% of its nights below), at its next occurrence
    in the next week. The gap is the weekday's typical night against a
    typical day — what the night is short of, not what a campaign returns."""
    import demand
    sd = demand.slow_days(restaurant_id, db_path=db_path)
    if not sd.get("available"):
        return []
    typical_day = sd.get("typical_day")
    if not typical_day:
        return []
    out = []
    for d in sd.get("slow_days") or []:
        day = d.get("day")
        if day not in WEEKDAYS:
            continue
        ahead = (WEEKDAYS.index(day) - today.weekday()) % 7 or 7
        if ahead > SLOW_LOOKAHEAD_DAYS:
            continue
        target = today + timedelta(days=ahead)
        fc = demand.forecast_day(restaurant_id, target, db_path=db_path)
        if not fc.get("available"):
            continue
        pct = float(d.get("vs_average_pct") or 0)
        hist = demand._weekday_history(restaurant_id, day, today + timedelta(days=1), db_path=db_path)
        under = sum(1 for v in hist if v < typical_day)
        night = float(fc["typical_sales"])
        gap = typical_day - night
        out.append(_card(
            _key("slow_day", day), "slow_night", f"Fill {day}, {_mdy(target)}",
            f"{day}s here run {abs(round(pct))}% under a typical day: {under} of the last {len(hist)} did.",
            facts=[f"{_money(night)} a typical {day} ({fc['samples']} {day}s)",
                   f"{_money(typical_day)} a typical day"],
            stake=({"amount": round(gap, 2), "label": f"a {day} night under a typical day"} if gap > 0 else None),
            when=target, days_away=ahead, prompt=f"Fill {day} dinner",
            evidence={"n": fc["samples"], "kind": "nights", "basis": f"the last {fc['samples']} {day}s' sales"},
            sources=("sales", "pos"), score=90 - 3 * ahead))
    # The nearest two: three slow nights in a week is one message, not three cards.
    return sorted(out, key=lambda c: c["days_away"])[:SLOW_CARDS]


def holidays(restaurant_id, now, db_path=DB_PATH):
    """The holidays ahead, from demand.upcoming_holidays: said with last
    year's measured night (schedule_economics.holiday_lift) when there is
    one, and only named — close by, with nothing claimed — when there isn't."""
    import demand
    out = []
    for h in demand.upcoming_holidays(restaurant_id, now=now, days=HOLIDAY_LOOKAHEAD_DAYS, db_path=db_path):
        away = int(h.get("days_away") or 0)
        if away < 1:
            continue
        name, iso = h.get("name") or "A holiday", h.get("date")
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        wd = WEEKDAYS[d.weekday()]
        lift = h.get("lift_pct")
        if h.get("claim_kind") == "measured" and lift is not None and abs(lift) >= HOLIDAY_MOVE_PCT:
            up = lift > 0
            out.append(_card(
                _key("holiday_promo", iso), "holiday",
                (f"Get ready for {name}, {wd} {_mdy(d)}" if up else f"Bring guests in for {name}, {wd} {_mdy(d)}"),
                f"Last year {name} ran {abs(int(lift))}% {'above' if up else 'under'} a typical {wd} here.",
                facts=[h.get("based_on") or ""],
                when=d, days_away=away,
                prompt=(f"Promote our {name} plans" if up else f"Bring guests in for {name}"),
                evidence={"n": 1, "kind": "nights", "basis": h.get("based_on") or f"last year's {name}"},
                sources=("sales", "pos"), score=84 - away))
        elif h.get("claim_kind") != "measured" and away <= HOLIDAY_UNMEASURED_DAYS:
            out.append(_card(
                _key("holiday_promo", iso), "holiday", f"Plan for {name}, {wd} {_mdy(d)}",
                f"{name} is {away} day{'' if away == 1 else 's'} away. There's no {name} of your own on file "
                f"yet to say how it goes here.",
                when=d, days_away=away, prompt=f"Promote our {name} plans",
                evidence=None, score=46 - away / 2.0))
    return out


def category_dips(restaurant_id, today, db_path=DB_PATH):
    """A POS category (the nightly report's `sales.cat:` metrics, the owner's
    own department mapping) whose last 28 nights fell past its own weekly
    swing against the 28 before. 28 nights is four of every weekday, so the
    windows compare like with like; a window with fewer than 20 measured
    nights, or a category under $500 in the earlier one, says nothing."""
    try:
        from dsr import store as dstore
    except Exception:
        return []
    names = [m for m in dstore.metric_names(restaurant_id, db_path=db_path) if m.startswith("sales.cat:")]
    if not names:
        return []
    end = today - timedelta(days=1)
    last_start = end - timedelta(days=CATEGORY_WINDOW_DAYS - 1)
    prior_start = last_start - timedelta(days=CATEGORY_WINDOW_DAYS)
    out = []
    for metric in names:
        series = dstore.metric_series(restaurant_id, metric, prior_start.isoformat(), end.isoformat(),
                                      db_path=db_path)
        last, prior, weeks = [], [], [0.0] * 4
        for bd, v in series:
            try:
                d = date.fromisoformat(str(bd)[:10])
                val = float(v)
            except (TypeError, ValueError):
                continue
            if d >= last_start:
                last.append(val)
            else:
                prior.append(val)
                weeks[min(3, (d - prior_start).days // 7)] += val
        if len(last) < CATEGORY_MIN_NIGHTS or len(prior) < CATEGORY_MIN_NIGHTS:
            continue
        total_last, total_prior = sum(last), sum(prior)
        if total_prior < CATEGORY_MIN_BASE:
            continue
        pct = (total_last - total_prior) / total_prior * 100.0
        mean_week = total_prior / 4.0
        band = max(CATEGORY_MIN_DROP_PCT, round(pstdev(weeks) / mean_week * 100)) if mean_week > 0 else None
        if band is None or pct > -band:
            continue
        name = metric[len("sales.cat:"):].strip() or "A category"
        nights = min(len(last), len(prior))
        out.append(_card(
            _key("category_dip", name), "category_dip", f"Win back {name.lower()} sales",
            f"{name} is down {abs(round(pct))}%: {_money(total_last)} over the last {CATEGORY_WINDOW_DAYS} nights "
            f"vs {_money(total_prior)} the {CATEGORY_WINDOW_DAYS} before.",
            facts=[f"{nights} of {CATEGORY_WINDOW_DAYS} nights measured in each window",
                   f"past its usual {band}% week-to-week swing"],
            stake={"amount": round(total_prior - total_last, 2),
                   "label": f"less than the {CATEGORY_WINDOW_DAYS} nights before"},
            prompt=f"Promote our {name.lower()}",
            evidence={"n": nights, "kind": "trading_days",
                      "basis": f"{name} sales on {nights} measured nights in each window"},
            sources=("pos", "sales"), score=70 + min(15.0, abs(pct) / 2.0)))
    return out


def dishes(restaurant_id, db_path=DB_PATH):
    """Two dish signals: a dish that earns above the menu's median margin and
    sells below its median units (the scorecard's "puzzles", verdict
    promote), and a dish guests name positively in reviews and never
    negatively (reviews' dish entities, 90 days). No dollar figure: nothing
    has measured what featuring a dish does here."""
    import menu_intelligence
    out, taken = [], set()
    try:
        sc = menu_intelligence.dish_scorecard(restaurant_id, db_path=db_path)
    except Exception as e:
        print(f"[mkt_opps] dish scorecard unavailable for {restaurant_id}: {e}")
        sc = {}
    if sc.get("available") and sc.get("has_sales_data"):
        rows = sc.get("dishes") or []
        usable = [d for d in rows if d.get("units_sold") and d.get("margin") is not None]
        if len(usable) >= 4:
            units = sorted(float(d["units_sold"]) for d in usable)
            margins = sorted(float(d["margin"]) for d in usable)
            mid = len(usable) // 2
            med_u = units[mid] if len(usable) % 2 else (units[mid - 1] + units[mid]) / 2
            med_m = margins[mid] if len(usable) % 2 else (margins[mid - 1] + margins[mid]) / 2
            days = _sales_days(restaurant_id, db_path)
            promote = sorted((d for d in usable if d.get("action") == "promote"),
                             key=lambda d: -float(d["margin"]))[:DISH_CARDS]
            for d in promote:
                name = d["name"]
                taken.add(name.lower())
                pos = int(d.get("positive_mentions") or 0)
                out.append(_card(
                    _key("dish_promote", name), "dish_promote", f"Put {name} in front of guests",
                    f"It earns {'$%.2f' % float(d['margin'])} a plate (menu median {'$%.2f' % med_m}) but sold "
                    f"{float(d['units_sold']):,.0f} in the last 28 days (median {med_u:,.0f}).",
                    facts=[f"Named positively in {pos} review{'' if pos == 1 else 's'}" if pos >= 2 else ""],
                    prompt=f"Feature our {name}",
                    evidence={"n": days, "kind": "trading_days", "basis": f"{days} days of item sales"},
                    sources=("pos", "sales"), score=62 + (4 if pos >= 2 else 0)))
    try:
        praised = menu_intelligence.dish_praise(restaurant_id, db_path=db_path)
    except Exception as e:
        print(f"[mkt_opps] dish praise unavailable for {restaurant_id}: {e}")
        praised = []
    for it in praised:
        name = it["name"]
        if name.lower() in taken:
            continue
        pos = int(it.get("positive_mentions") or 0)
        if pos < PRAISE_MIN_MENTIONS or int(it.get("negative_mentions") or 0) > 0:
            continue
        taken.add(name.lower())
        out.append(_card(
            _key("dish_praise", name), "dish_praise", f"Feature {name}: guests love it",
            f"Named positively in {pos} reviews in the last 90 days, never negatively.",
            prompt=f"Feature our {name}, a guest favorite",
            evidence={"n": pos, "kind": "reviews", "basis": f"{pos} reviews that named it"},
            sources=("reviews",), score=56 + min(6, pos - PRAISE_MIN_MENTIONS)))
        if sum(1 for c in out if c["kind"] == "dish_praise") >= DISH_CARDS:
            break
    return out


def _sales_days(restaurant_id, db_path):
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT COUNT(DISTINCT business_date) AS n FROM menu_item_sales "
                               "WHERE restaurant_id=? AND business_date >= date('now','-28 days')",
                               (restaurant_id,)).fetchone()
        finally:
            conn.close()
        return int(row["n"] or 0) if row else 0
    except Exception:
        return 0


def lists(restaurant_id, now, db_path=DB_PATH):
    """Opted-in guests nobody has contacted in LIST_IDLE_DAYS. Counted in
    SQL. A text contact whose only YES was to a review-link invite is left
    out: that YES agreed to a review link, not to marketing (audit SMS-1).
    The text count is the one the Studio shows and the send texts
    (guest_marketing.marketing_text_sql, which holds review-only guests out
    at the source since 9/28/26, OPP-6)."""
    from guest_marketing import marketing_text_sql
    conn = get_conn(db_path)
    try:
        def one(sql, args):
            try:
                r = conn.execute(sql, args).fetchone()
                return r[0] if r else None
            except Exception:
                return None
        texts = one("SELECT COUNT(*) FROM guest_contacts g WHERE g.restaurant_id=? AND "
                    + marketing_text_sql("g"), (restaurant_id,)) or 0
        emails = one("SELECT COUNT(DISTINCT LOWER(TRIM(email))) FROM guest_contacts WHERE restaurant_id=? "
                     "AND email IS NOT NULL AND TRIM(email)!='' AND email_consent=1 "
                     "AND COALESCE(email_unsubscribed,0)=0 AND LOWER(TRIM(email)) NOT IN "
                     "(SELECT email FROM email_suppressions WHERE scope IS NULL OR scope='' OR scope='guest')",
                     (restaurant_id,)) or 0
        last_text = one("SELECT MAX(created_at) FROM guest_campaigns WHERE restaurant_id=? AND sent_count>0",
                        (restaurant_id,))
        last_email = one("SELECT MAX(created_at) FROM guest_newsletters WHERE restaurant_id=?", (restaurant_id,))
    finally:
        conn.close()
    out = []
    for kind, n, floor, last, channel, noun in (("text", texts, TEXT_LIST_MIN, last_text, "text", "texted"),
                                                ("email", emails, EMAIL_LIST_MIN, last_email, "email", "emailed")):
        if n < floor:
            continue
        since = _days_since(last, now)
        if since is not None and since < LIST_IDLE_DAYS:
            continue
        why = (f"The last one went out {since} days ago." if since is not None
               else f"They've never been {noun}.")
        out.append(_card(
            _key("list_idle", kind), "list_idle",
            (f"Text your {n:,} opted-in guests" if kind == "text" else f"Email the {n:,} guests on your list"),
            why,
            prompt=("Send our guests what's good this week" if kind == "text"
                    else "Send our email list what's new this week"),
            channels=(channel,), evidence=None,
            score=(55 if since is None else 50 + min(10.0, since / 10.0))))
    return out


def posting(restaurant_id, now, restaurant=None, db_path=DB_PATH):
    """Nothing live for POST_IDLE_DAYS, when an account is connected to post
    to. Home's own key (post_this_week), so an answer on either surface holds
    on both."""
    r = restaurant
    if r is None:
        return []
    connected = bool(getattr(r, "ig_token", None)) or bool(getattr(r, "fb_page_token", None) and getattr(r, "fb_page_id", None)) \
        or bool(getattr(r, "gmb_refresh_token", None) and getattr(r, "gmb_location_id", None))
    if not connected:
        return []
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT MAX(COALESCE(posted_at, created_at)) FROM marketing_content_log "
                           "WHERE restaurant_id=? AND post_id IS NOT NULL", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    since = _days_since(row[0] if row else None, now)
    if since is not None and since < POST_IDLE_DAYS:
        return []
    return [_card("post_this_week", "posting", "Get a post out this week",
                  (f"Nothing has gone live in {since} days." if since is not None else "Nothing has been posted yet."),
                  prompt="Share what's good this week", channels=("social",), evidence=None,
                  score=44 + (min(6.0, since / 5.0) if since is not None else 3))]


def _days_since(value, now):
    if not value:
        return None
    try:
        when = datetime.strptime(str(value).replace("T", " ")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return max(0, (now.date() - when).days)


# ── build, cache, present ───────────────────────────────────────────────────

def build(restaurant_id, db_path=DB_PATH, now=None, restaurant=None) -> list:
    """Every opportunity this restaurant's data supports right now, ranked.
    Each source is isolated: one failing never empties the feed."""
    from time_utils import restaurant_now_by_id
    now = now or restaurant_now_by_id(restaurant_id, naive=True)
    today = now.date()
    if restaurant is None:
        restaurant = _models.get_restaurant(restaurant_id)
    cards = []
    for name, fn in (("slow nights", lambda: slow_nights(restaurant_id, today, db_path)),
                     ("holidays", lambda: holidays(restaurant_id, now, db_path)),
                     ("categories", lambda: category_dips(restaurant_id, today, db_path)),
                     ("dishes", lambda: dishes(restaurant_id, db_path)),
                     ("lists", lambda: lists(restaurant_id, now, db_path)),
                     ("posting", lambda: posting(restaurant_id, now, restaurant, db_path))):
        try:
            cards.extend(fn() or [])
        except Exception as e:
            print(f"[mkt_opps] {name} failed for {restaurant_id}: {e}")
    seen, out = set(), []
    for c in sorted(cards, key=lambda c: -c["score"]):
        if c["key"] in seen:
            continue
        seen.add(c["key"])
        out.append(c)
    return out


def fingerprint(restaurant_id, today, db_path=DB_PATH) -> str:
    """What the feed depends on, read cheaply: the local date (sales history
    moves nightly), the newest review, list sizes and the last send or post.
    The same inputs serve the stored feed, so web and phone read one answer."""
    parts = [VERSION, today.isoformat()]
    conn = get_conn(db_path)
    try:
        for sql in ("SELECT MAX(id) FROM reviews WHERE restaurant_id=?",
                    "SELECT COUNT(*), MAX(created_at) FROM guest_contacts WHERE restaurant_id=?",
                    "SELECT MAX(created_at) FROM guest_campaigns WHERE restaurant_id=?",
                    "SELECT MAX(created_at) FROM guest_newsletters WHERE restaurant_id=?",
                    "SELECT MAX(COALESCE(posted_at, created_at)) FROM marketing_content_log WHERE restaurant_id=?",
                    "SELECT MAX(business_date) FROM dsr_metrics WHERE restaurant_id=?",
                    "SELECT COUNT(*), SUM(COALESCE(sell_price,0)) FROM menu_items WHERE restaurant_id=? "
                    "AND is_active=1",
                    "SELECT COUNT(*) FROM recipe_ingredients WHERE menu_item_id IN "
                    "(SELECT id FROM menu_items WHERE restaurant_id=?)"):
            try:
                row = conn.execute(sql, (restaurant_id,)).fetchone()
                parts.append(list(row) if row else None)
            except Exception:
                parts.append(None)
    finally:
        conn.close()
    return hashlib.sha256(json.dumps(parts, default=str).encode("utf-8")).hexdigest()[:24]


def cached_build(restaurant_id, db_path=DB_PATH, now=None, restaurant=None) -> list:
    import insight_store
    from time_utils import restaurant_now_by_id
    now = now or restaurant_now_by_id(restaurant_id, naive=True)
    fp = fingerprint(restaurant_id, now.date(), db_path)
    stored = insight_store.get(restaurant_id, CACHE_KIND, fp, db_path=db_path)
    if isinstance(stored, list):
        return stored
    cards = build(restaurant_id, db_path=db_path, now=now, restaurant=restaurant)
    insight_store.put(restaurant_id, CACHE_KIND, fp, cards, db_path=db_path)
    return cards


def feed(restaurant_id, user_id=None, db_path=DB_PATH, surface="marketing") -> dict:
    """The feed for a page: stored cards the owner hasn't answered
    (insight_store.present_recs drops those, and logs the rest as shown),
    each with ONE measured confidence (rec_trust.assess, K1) — a fact (a
    list or a posting gap) carries none."""
    import insight_store
    import rec_trust
    restaurant = _models.get_restaurant(restaurant_id)
    cards = cached_build(restaurant_id, db_path=db_path, restaurant=restaurant)
    items = [dict(c, text=c["title"], module="marketing", model_written=False,
                  evidence_sources=["marketing"] + [s for s in c.get("sources") or [] if s != "marketing"],
                  dollar_value=None) for c in cards]
    shown = insight_store.present_recs(restaurant_id, "marketing", surface, items, user_id=user_id, db_path=db_path)
    ctx = rec_trust.Context(restaurant_id, restaurant=restaurant, db_path=db_path)
    out = []
    for c in shown:
        conf = None
        if c.get("evidence"):
            try:
                conf = rec_trust.assess(restaurant_id, c["key"], evidence=c["evidence"],
                                        sources=tuple(c.get("sources") or ()), ctx=ctx)
            except Exception as e:
                print(f"[mkt_opps] confidence unavailable for {c['key']}: {e}")
        item = {k: c.get(k) for k in ("key", "rec_id", "kind", "title", "why", "facts", "stake", "when",
                                      "days_away", "action", "score")}
        item["confidence"] = conf
        out.append(item)
    return {"ok": True, "items": out, "checked": CHECKED}


# What the feed looks at, for the empty state ("Nothing stands out right
# now") — the owner sees what was checked, not a blank.
CHECKED = ["slow nights ahead", "holidays", "category sales", "dish margins and sales",
           "dishes guests praise", "your text and email lists", "your posting"]
