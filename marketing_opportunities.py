"""marketing_opportunities.py — the Marketing Opportunity Feed (9/28/26).

Opening Marketing used to start at a blank prompt: the owner had to think of
every campaign. The signals that say what to do next already existed, spread
across other modules — a reliably slow weekday (demand), last year's holiday
night (schedule_economics), a POS category falling (the nightly report), a
high-margin dish nobody orders (the dish scorecard), a dish guests praise
(reviews), a list nobody has contacted, nothing posted in a while. This
gathers them into ranked cards, each with a one-tap draft for the Campaign
Studio. It is also the ONE answer to "what should I do this week": the
Marketing brief and the content calendar are written with its cards in
front of them (context_lines), and the morning brief's slow-night line and
the weekly digest read the same slow nights under the same key.

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
  slow_day:<Weekday>        a reliably slow weekday in the next 7 days (the
                            one key the morning brief, the digest and
                            outcomes use for it; recurring — rec_ledger.
                            RECURRING_DONE_DAYS)
  holiday_promo:<ISO date>  a holiday in the next 21 days, with last year's
                            measured night when there is one
  category_dip:<Category>   a POS category down past its own weekly swing
  dish_promote:<Dish>       earns above the menu's median, sells below it —
                            margins: Food Cost logins only (OPP-5)
  dish_praise:<Dish>        named in positive reviews, never a negative one
  list_idle:text / :email   opted-in guests not contacted in 30+ days
  post_this_week / first_post   Home's own posting keys and rule

Build → cache → present. build() reads every source into candidate cards
(uncapped) with each source's status (checked / no data / failed);
cached_build() stores it per restaurant under a fingerprint of everything it
read; feed() then, per viewer and per load: drops cards this login may not
see, drops answered cards, THEN caps each kind (re-audit OPP-11), assesses
confidence, and logs as shown only the cards on screen (the first VISIBLE,
or all once "Show more" asks — OPP-10).
"""
import hashlib
import json
from datetime import date, datetime, timedelta
from statistics import pstdev

import models as _models
from models import DB_PATH

CACHE_KIND = "mkt_opps"
VERSION = 2

SLOW_LOOKAHEAD_DAYS = 7
SLOW_CARDS = 2
HOLIDAY_LOOKAHEAD_DAYS = 21
HOLIDAY_UNMEASURED_DAYS = 14      # a holiday with no history of its own: only when close
HOLIDAY_MOVE_PCT = 10             # last year's night this far from a typical one is worth acting on
CATEGORY_WINDOW_DAYS = 28
CATEGORY_MIN_NIGHTS = 20          # nights measured in BOTH windows (the same weekday 28 days apart)
CATEGORY_MAX_COVERAGE_GAP = 2     # the two windows' measured nights may differ by at most this
CATEGORY_MIN_BASE = 500.0         # dollars: the earlier window's per-night mean over 28 nights
CATEGORY_MIN_DROP_PCT = 10        # the floor under the category's own swing
CATEGORY_REMAP_DAYS = 56          # a category map changed inside both windows compares nothing
PRAISE_MIN_REVIEWS = 3
DISH_CARDS = 2
LIST_IDLE_DAYS = 30
TEXT_LIST_MIN = 25
EMAIL_LIST_MIN = 10
# Home's rule, one rule (re-audit OPP-17): a posting card once MORE than this
# many days have passed since a post went live (home_brief reads this).
POST_IDLE_DAYS = 10
# Cards on screen before "Show more" (dashboard.html's .mkt-opps-list.collapsed).
VISIBLE = 3
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
ALL_CHANNELS = ("text", "email", "social")
# Every key kind the feed shows — what a Studio send may name as its card.
FEED_KINDS = ("slow_day", "holiday_promo", "category_dip", "dish_promote", "dish_praise", "list_idle",
              "post_this_week", "first_post")

# What the feed reads, for the empty state: each says checked, no data yet
# (and why), or couldn't be read (re-audit OPP-14).
SOURCES = (("slow_nights", "slow nights ahead"), ("holidays", "holidays"), ("categories", "category sales"),
           ("dish_margins", "dish margins and sales"), ("dish_praise", "dishes guests praise"),
           ("lists", "your text and email lists"), ("posting", "your posting"))
SOURCE_LABELS = dict(SOURCES)
# Sources whose figures are Food Cost's (plate margins): Food Cost logins only.
FOOD_SOURCES = ("dish_margins",)
# Every label, for a client that reads the old `checked` list.
CHECKED = [label for _k, label in SOURCES]


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


def _capture(e, restaurant_id, source):
    """A source that failed: to the ops log, never only a print (OPP-14)."""
    try:
        import ops
        ops.capture(e, job="marketing_opportunities", context=f"restaurant_id={restaurant_id} source={source}")
    except Exception:
        print(f"[mkt_opps] {source} failed for {restaurant_id}: {e}")


class Found(list):
    """One source's cards, and what it found to look at: "checked" (it read
    real data, whether or not a card came of it) or "no_data" (nothing to
    read yet — `note` says what's missing)."""

    def __init__(self, cards=(), state="checked", note=None):
        super().__init__(cards)
        self.state = state
        self.note = note


def _card(key, kind, title, why, *, subject=None, facts=(), stake=None, when=None, days_away=None, prompt="",
          channels=ALL_CHANNELS, evidence=None, sources=(), score=0, food=False):
    """One opportunity. `evidence` None means a fact (a list, a posting gap):
    it carries no confidence, never a stand-in figure. `stake` is a measured
    gap {"amount", "label"}, never an expected return. `food`: it states a
    plate margin — Food Cost logins only."""
    return {"key": key, "kind": kind, "subject": subject, "title": title, "why": why,
            "facts": [f for f in facts if f], "stake": stake,
            "when": when.isoformat() if hasattr(when, "isoformat") else when,
            "days_away": days_away, "action": {"prompt": prompt[:280], "channels": list(channels)},
            "evidence": evidence, "sources": list(sources), "score": round(float(score), 2), "food": bool(food)}


# ── the signals ─────────────────────────────────────────────────────────────

def slow_nights(restaurant_id, today, db_path=DB_PATH):
    """Every weekday that runs reliably under a typical day
    (demand.reliably_slow_nights), at its next occurrence in the next week.
    Its % and its $ come from one window of finished nights before today
    (re-audit OPP-16); the gap is the weekday's typical night against a
    typical day — what the night is short of, not what a campaign returns.
    "N of the last M came in under it" counts exactly that. Uncapped: the
    feed keeps the nearest SLOW_CARDS the owner hasn't answered."""
    import demand
    sd = demand.reliably_slow_nights(restaurant_id, today=today, db_path=db_path)
    if not sd.get("available"):
        return Found(state="no_data", note=sd.get("reason") or "not enough sales history yet")
    typical_day = sd["typical_day"]
    out = Found()
    for d in sd.get("slow") or []:
        day = d.get("day")
        if day not in WEEKDAYS:
            continue
        ahead = (WEEKDAYS.index(day) - today.weekday()) % 7 or 7
        if ahead > SLOW_LOOKAHEAD_DAYS:
            continue
        target = today + timedelta(days=ahead)
        n, night, gap = int(d["samples"]), float(d["median_sales"]), float(d["gap"])
        out.append(_card(
            _key("slow_day", day), "slow_night", f"Fill {day}, {_mdy(target)}",
            f"{day}s here run {abs(int(d['vs_typical_pct']))}% under a typical day: {d['under']} of the "
            f"last {n} came in under it.",
            subject=day,
            facts=[f"{_money(night)} a typical {day} ({n} {day}s)", f"{_money(typical_day)} a typical day"],
            stake=({"amount": round(gap, 2), "label": f"a {day} night under a typical day"} if gap > 0 else None),
            when=target, days_away=ahead, prompt=f"Fill {day} dinner",
            evidence={"n": n, "kind": "nights", "basis": f"the last {n} finished {day}s' sales"},
            sources=("sales", "pos"), score=90 - 3 * ahead))
    out.sort(key=lambda c: c["days_away"])
    return out


def holidays(restaurant_id, now, db_path=DB_PATH, restaurant=None):
    """The holidays ahead, from demand.upcoming_holidays, by their own name
    (never the calendar's hint for the model — OPP-1): said with last year's
    measured night, against THAT night's weekday (OPP-2), when there is one;
    only named — close by, with nothing claimed — when there isn't. A date
    the calendar only estimates (the Super Bowl) is said as an estimate and
    never measured. A holiday the owner told Cavnar AI to skip is skipped.
    The draft goal invites guests; it never says the restaurant has plans."""
    import demand
    skip = [s.strip().lower() for s in (getattr(restaurant, "skip_holidays", None) or "").split(",") if s.strip()]
    out = Found()
    for h in demand.upcoming_holidays(restaurant_id, now=now, days=HOLIDAY_LOOKAHEAD_DAYS, db_path=db_path):
        away = int(h.get("days_away") or 0)
        if away < 1:
            continue
        name = h.get("display_name") or demand.holiday_display_name(h.get("name")) or "A holiday"
        if any(s in name.lower() for s in skip):
            continue
        iso = h.get("date")
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        wd = WEEKDAYS[d.weekday()]
        approximate = bool(h.get("approximate")) or name in demand.APPROXIMATE_HOLIDAYS
        lift = h.get("lift_pct")
        if not approximate and h.get("claim_kind") == "measured" and lift is not None and abs(lift) >= HOLIDAY_MOVE_PCT:
            up = lift > 0
            ly_wd = h.get("last_year_weekday")
            against = f"a typical {ly_wd}" if ly_wd else "a typical night that week"
            out.append(_card(
                _key("holiday_promo", iso), "holiday",
                (f"Get ready for {name}, {wd} {_mdy(d)}" if up else f"Bring guests in for {name}, {wd} {_mdy(d)}"),
                f"Last year's {name} ran {abs(int(lift))}% {'above' if up else 'under'} {against} here.",
                subject=name, facts=[h.get("based_on") or ""],
                when=d, days_away=away, prompt=(f"Invite guests in for {name}" if up else f"Bring guests in for {name}"),
                evidence={"n": 1, "kind": "nights", "basis": h.get("based_on") or f"last year's {name}"},
                sources=("sales", "pos"), score=84 - away))
        elif (approximate or h.get("claim_kind") != "measured") and away <= HOLIDAY_UNMEASURED_DAYS:
            if approximate:
                title = f"Plan for {name}, expected around {_mdy(d)}"
                why = (f"{name} usually lands around {_mdy(d)}, {away} day{'' if away == 1 else 's'} away — its "
                       f"date isn't fixed, so check it before you plan.")
            else:
                title = f"Plan for {name}, {wd} {_mdy(d)}"
                why = (f"{name} is {away} day{'' if away == 1 else 's'} away. There's no {name} of your own on "
                       f"file yet to say how it goes here.")
            out.append(_card(
                _key("holiday_promo", iso), "holiday", title, why, subject=name,
                when=d, days_away=away, prompt=f"Invite guests in for {name}",
                evidence=None, score=46 - away / 2.0))
    return out


def _category_remapped(restaurant_id, db_path):
    """The newest change to this restaurant's POS → category map inside
    CATEGORY_REMAP_DAYS (a date), or None. The map keeps where a department
    is NOW, not where it was, so a re-map in either window moves a
    department's sales between categories and reads as a dip (re-audit
    OPP-4): until it is older than both windows, nothing is compared."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT MAX(updated_at) FROM dsr_category_map WHERE restaurant_id=? "
                           "AND updated_at >= datetime('now', ?)",
                           (restaurant_id, f"-{CATEGORY_REMAP_DAYS} days")).fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    try:
        return datetime.strptime(str(row[0])[:10], "%Y-%m-%d").date() if row and row[0] else None
    except ValueError:
        return None


def category_dips(restaurant_id, today, db_path=DB_PATH):
    """A POS category (the nightly report's `sales.cat:` metrics, the owner's
    own department mapping) whose last 28 nights fell past its own weekly
    swing against the 28 before (re-audit OPP-3).

    Each window is a PER-NIGHT mean over nights measured in both — each
    night paired with the same weekday 28 days earlier — so a missing night
    is never a $0 night and the weekday mix is the same on both sides. Both
    windows need CATEGORY_MIN_NIGHTS measured and near-equal coverage
    (CATEGORY_MAX_COVERAGE_GAP); the card states each window's coverage and
    the category's REAL week-to-week swing (the 10% floor is only a floor).
    The "Unmapped" bucket is not a category (the DSR scorecard skips it too),
    and a map changed inside the windows compares nothing (OPP-4)."""
    try:
        import dsr as _dsr
        from dsr import store as dstore
    except Exception:
        return Found(state="no_data", note="the nightly report isn't set up")
    unmapped = str(getattr(_dsr, "UNMAPPED", "Unmapped")).strip().lower()
    names = [m for m in dstore.metric_names(restaurant_id, db_path=db_path)
             if m.startswith("sales.cat:") and m[len("sales.cat:"):].strip().lower() not in ("", unmapped)]
    if not names:
        return Found(state="no_data", note="no nightly reports with sales categories yet")
    remapped = _category_remapped(restaurant_id, db_path)
    if remapped:
        return Found(state="no_data",
                     note=(f"your sales categories were re-mapped on {_mdy(remapped)}; they compare again "
                           f"from {_mdy(remapped + timedelta(days=CATEGORY_REMAP_DAYS))}"))
    end = today - timedelta(days=1)
    last_start = end - timedelta(days=CATEGORY_WINDOW_DAYS - 1)
    prior_start = last_start - timedelta(days=CATEGORY_WINDOW_DAYS)
    out = Found()
    for metric in names:
        series = dstore.metric_series(restaurant_id, metric, prior_start.isoformat(), end.isoformat(),
                                      db_path=db_path)
        last, prior = {}, {}
        for bd, v in series:
            try:
                d = date.fromisoformat(str(bd)[:10])
                val = float(v)
            except (TypeError, ValueError):
                continue
            (last if d >= last_start else prior)[d] = val
        n_last, n_prior = len(last), len(prior)
        if min(n_last, n_prior) < CATEGORY_MIN_NIGHTS or abs(n_last - n_prior) > CATEGORY_MAX_COVERAGE_GAP:
            continue
        pairs = [(last[d], prior[d - timedelta(days=CATEGORY_WINDOW_DAYS)]) for d in sorted(last)
                 if d - timedelta(days=CATEGORY_WINDOW_DAYS) in prior]
        if len(pairs) < CATEGORY_MIN_NIGHTS:
            continue
        mean_last = sum(a for a, _b in pairs) / len(pairs)
        mean_prior = sum(b for _a, b in pairs) / len(pairs)
        if mean_prior <= 0 or mean_prior * CATEGORY_WINDOW_DAYS < CATEGORY_MIN_BASE:
            continue
        pct = (mean_last - mean_prior) / mean_prior * 100.0
        # Its own swing: how far its weeks' per-night means usually move
        # (the earlier window's four weeks).
        weeks = [[] for _ in range(4)]
        for d, v in prior.items():
            weeks[min(3, (d - prior_start).days // 7)].append(v)
        week_means = [sum(w) / len(w) for w in weeks if w]
        if len(week_means) < 4:
            continue
        mid = sum(week_means) / len(week_means)
        swing = int(round(pstdev(week_means) / mid * 100)) if mid > 0 else None
        if swing is None:
            continue
        band = max(CATEGORY_MIN_DROP_PCT, swing)
        if pct > -band:
            continue
        name = metric[len("sales.cat:"):].strip()
        gap = sum(b - a for a, b in pairs)
        n = len(pairs)
        out.append(_card(
            _key("category_dip", name), "category_dip", f"Win back {name.lower()} sales",
            f"{name} is down {abs(round(pct))}% a night: {_money(mean_last)} a night over the last "
            f"{CATEGORY_WINDOW_DAYS} nights vs {_money(mean_prior)} the {CATEGORY_WINDOW_DAYS} before, "
            f"the same weekdays compared.",
            subject=name,
            facts=[f"{n_last} of {CATEGORY_WINDOW_DAYS} nights measured lately, {n_prior} of "
                   f"{CATEGORY_WINDOW_DAYS} before",
                   f"its weeks usually swing {swing}%"],
            stake=({"amount": round(gap, 2), "label": f"less over {n} nights than the same nights before"}
                   if gap > 0 else None),
            prompt=f"Promote our {name.lower()}",
            evidence={"n": n, "kind": "trading_days",
                      "basis": f"{name} sales on {n} nights measured in both windows"},
            sources=("pos", "sales"), score=70 + min(15.0, abs(pct) / 2.0)))
    return out


def dish_margins(restaurant_id, db_path=DB_PATH, praise=None, mentions=None):
    """A dish that earns above the menu's median margin and sells below its
    median units (the scorecard's "puzzles", verdict promote). A dish whose
    plate cost carries a unit warning is left out of the cards and the
    medians both: its margin is a units mix-up, not a margin (OPP-8). Every
    card states a plate margin, so it is marked `food` (Food Cost logins
    only — OPP-5). No dollar stake: nothing has measured what featuring a
    dish does here. `praise`: dish_praise's rows, for the review count."""
    import menu_intelligence
    sc = menu_intelligence.dish_scorecard(restaurant_id, db_path=db_path, mentions=mentions)
    if not (sc.get("available") and sc.get("has_sales_data")):
        return Found(state="no_data", note=sc.get("reason") or "no costed dishes with sales yet")
    rows = sc.get("dishes") or []
    usable = [d for d in rows if d.get("units_sold") and d.get("margin") is not None and not d.get("unit_warning")]
    if len(usable) < 4:
        return Found(state="no_data", note="fewer than four costed dishes with sales")
    units = sorted(float(d["units_sold"]) for d in usable)
    margins = sorted(float(d["margin"]) for d in usable)
    mid = len(usable) // 2
    med_u = units[mid] if len(usable) % 2 else (units[mid - 1] + units[mid]) / 2
    med_m = margins[mid] if len(usable) % 2 else (margins[mid - 1] + margins[mid]) / 2
    days = _sales_days(restaurant_id, db_path)
    praised = {str(p.get("name") or "").strip().lower(): int(p.get("positive_reviews") or 0) for p in praise or []}
    out = Found()
    promote = sorted((d for d in usable if d.get("action") == "promote"), key=lambda d: -float(d["margin"]))
    for i, d in enumerate(promote):
        name = d["name"]
        pos = praised.get(name.strip().lower(), 0)
        out.append(_card(
            _key("dish_promote", name), "dish_promote", f"Put {name} in front of guests",
            f"It earns {'$%.2f' % float(d['margin'])} a plate (menu median {'$%.2f' % med_m}) but sold "
            f"{float(d['units_sold']):,.0f} in the last 28 days (median {med_u:,.0f}).",
            subject=name,
            facts=[f"Named in {pos} positive review{'' if pos == 1 else 's'}" if pos >= 2 else ""],
            prompt=f"Feature our {name}",
            evidence={"n": days, "kind": "trading_days", "basis": f"{days} days of item sales"},
            sources=("pos", "sales"), score=62 + (4 if pos >= 2 else 0) - i * 0.1, food=True))
    return out


def dish_praise_cards(restaurant_id, db_path=DB_PATH, praise=None):
    """A dish guests named in PRAISE_MIN_REVIEWS+ positive reviews in the
    last 90 days and in no negative one — distinct reviews, not mentions
    (OPP-7). The draft goal is the dish, never "a guest favorite": that
    would be a public claim the owner didn't make."""
    import menu_intelligence
    if praise is None:
        praise = menu_intelligence.dish_praise(restaurant_id, db_path=db_path)
    if not praise:
        return Found(state="no_data", note="no reviews naming a dish on the menu in the last 90 days")
    out = Found()
    for it in praise:
        name = it["name"]
        pos, neg = int(it.get("positive_reviews") or 0), int(it.get("negative_reviews") or 0)
        if pos < PRAISE_MIN_REVIEWS or neg > 0:
            continue
        out.append(_card(
            _key("dish_praise", name), "dish_praise", f"Feature {name}: guests praise it",
            f"Named in {pos} positive reviews in the last 90 days, and in no negative one.",
            subject=name, prompt=f"Feature our {name}",
            evidence={"n": pos, "kind": "reviews", "basis": f"{pos} positive reviews that named it"},
            sources=("reviews",), score=56 + min(6, pos - PRAISE_MIN_REVIEWS)))
    return out


def dishes(restaurant_id, db_path=DB_PATH):
    """Both dish signals, uncapped (the feed caps them per viewer)."""
    import menu_intelligence
    mentions = menu_intelligence._dish_mentions(restaurant_id, db_path)
    praise = menu_intelligence.dish_praise(restaurant_id, db_path=db_path, mentions=mentions)
    return Found(list(dish_margins(restaurant_id, db_path, praise=praise, mentions=mentions))
                 + list(dish_praise_cards(restaurant_id, db_path, praise=praise)))


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
    from models import suppressed_scope_sql
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
                     "(SELECT email FROM email_suppressions WHERE " + suppressed_scope_sql("guest") + ")",
                     (restaurant_id,)) or 0
        last_text = one("SELECT MAX(created_at) FROM guest_campaigns WHERE restaurant_id=? AND sent_count>0",
                        (restaurant_id,))
        last_email = one("SELECT MAX(created_at) FROM guest_newsletters WHERE restaurant_id=?", (restaurant_id,))
    finally:
        conn.close()
    if not texts and not emails:
        return Found(state="no_data", note="no opted-in guests yet")
    out = Found()
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
            why, subject=kind,
            prompt=("Send our guests what's good this week" if kind == "text"
                    else "Send our email list what's new this week"),
            channels=(channel,), evidence=None,
            score=(55 if since is None else 50 + min(10.0, since / 10.0))))
    return out


def _utc_age_days(value, utc_now):
    """Home's own age (home_brief._age_days): fractional days since a UTC
    stamp, or None."""
    if not value:
        return None
    try:
        when = datetime.strptime(str(value).replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            when = datetime.strptime(str(value)[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return max(0.0, (utc_now - when).total_seconds() / 86400.0)


def posting(restaurant_id, now, restaurant=None, db_path=DB_PATH, utc_now=None):
    """Home's posting cards, by Home's rule (re-audit OPP-17): more than
    POST_IDLE_DAYS since a post went LIVE (post_this_week, "Nothing has gone
    live in N days"), or nothing made at all (first_post) — a restaurant
    with drafts but no live post gets neither, as on Home. Only when an
    account can take a post (marketing_publish.channels_of — the rule the
    Studio and Home read): a posting card only ever drafts a post, never a
    guest text (OPP-15). Home's keys, so an answer on either surface holds
    on both."""
    r = restaurant
    if r is None:
        return Found(state="no_data", note="no social account connected")
    from marketing_publish import channels_of
    if not any(channels_of(r).values()):
        return Found(state="no_data", note="no social account connected")
    conn = get_conn(db_path)
    try:
        live = conn.execute("SELECT MAX(COALESCE(posted_at, created_at)) FROM marketing_content_log "
                            "WHERE restaurant_id=? AND post_id IS NOT NULL", (restaurant_id,)).fetchone()
        made = conn.execute("SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=?",
                            (restaurant_id,)).fetchone()
    finally:
        conn.close()
    age = _utc_age_days(live[0] if live else None, utc_now or datetime.utcnow())
    if age is None:
        if made and made[0]:
            return Found()
        return Found([_card("first_post", "posting", "Get your first post out",
                            "Nothing has been posted yet.", prompt="Share what's good this week",
                            channels=("social",), evidence=None, score=47)])
    if age <= POST_IDLE_DAYS:
        return Found()
    return Found([_card("post_this_week", "posting", "Get a post out this week",
                        f"Nothing has gone live in {int(age)} days.",
                        prompt="Share what's good this week", channels=("social",), evidence=None,
                        score=44 + min(6.0, age / 5.0))])


def _days_since(value, now):
    if not value:
        return None
    try:
        when = datetime.strptime(str(value).replace("T", " ")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return max(0, (now.date() - when).days)


# ── build, cache, present ───────────────────────────────────────────────────

def build(restaurant_id, db_path=DB_PATH, now=None, restaurant=None) -> dict:
    """Every opportunity this restaurant's data supports right now, ranked
    and UNCAPPED, with each source's status:
    {"cards": [...], "sources": [{"key", "label", "state", "note"}]}.
    Each source is isolated: one failing never empties the feed, and says
    so ("failed", to ops.capture)."""
    from time_utils import restaurant_now_by_id
    now = now or restaurant_now_by_id(restaurant_id, naive=True)
    today = now.date()
    if restaurant is None:
        restaurant = _get_restaurant(restaurant_id, db_path)
    cards, sources, shared = [], [], {}

    def praise():
        # One scan of the reviews' dish mentions per build, for both dish
        # signals (OPP-18).
        if "praise" not in shared:
            import menu_intelligence
            shared["mentions"] = menu_intelligence._dish_mentions(restaurant_id, db_path)
            shared["praise"] = menu_intelligence.dish_praise(restaurant_id, db_path=db_path,
                                                             mentions=shared["mentions"])
        return shared["praise"]

    steps = (("slow_nights", lambda: slow_nights(restaurant_id, today, db_path)),
             ("holidays", lambda: holidays(restaurant_id, now, db_path, restaurant=restaurant)),
             ("categories", lambda: category_dips(restaurant_id, today, db_path)),
             ("dish_praise", lambda: dish_praise_cards(restaurant_id, db_path, praise=praise())),
             # The review count on a margin card is a fact beside it: a
             # praise read that failed leaves the card without it, not gone.
             ("dish_margins", lambda: dish_margins(restaurant_id, db_path, praise=shared.get("praise"),
                                                   mentions=shared.get("mentions"))),
             ("lists", lambda: lists(restaurant_id, now, db_path)),
             ("posting", lambda: posting(restaurant_id, now, restaurant, db_path)))
    for key, fn in steps:
        try:
            found = fn()
            state, note = getattr(found, "state", "checked"), getattr(found, "note", None)
            cards.extend(found or [])
        except Exception as e:
            state, note = "failed", None
            _capture(e, restaurant_id, key)
        sources.append({"key": key, "label": SOURCE_LABELS[key], "state": state, "note": note})
    order = [k for k, _l in SOURCES]
    sources.sort(key=lambda s: order.index(s["key"]))
    seen, out = set(), []
    for c in sorted(cards, key=lambda c: -c["score"]):
        if c["key"] in seen:
            continue
        seen.add(c["key"])
        out.append(c)
    return {"cards": out, "sources": sources}


def _get_restaurant(restaurant_id, db_path=DB_PATH):
    return (_models.get_restaurant(restaurant_id) if db_path in (None, DB_PATH)
            else _models.get_restaurant(restaurant_id, db_path=db_path))


def fingerprint(restaurant_id, today, db_path=DB_PATH, restaurant=None) -> str:
    """Everything the feed reads, read cheaply (re-audit OPP-15): the local
    date (sales history moves nightly), the accounts a post can go to, the
    holidays to skip, reviews that name a dish once they are processed (a
    review that names none moves nothing), consent, unsubscribes and email
    consent, invite YESes, suppressions, the last send and post, finished
    nights of sales, the category map and its nights, the menu, recipes,
    ingredient costs and item sales. The same inputs serve the stored feed,
    so web and phone read one answer."""
    parts = [VERSION, today.isoformat()]
    if restaurant is not None:
        try:
            from marketing_publish import channels_of
            parts.append(sorted(k for k, v in channels_of(restaurant).items() if v))
        except Exception:
            parts.append(None)
        parts.append((getattr(restaurant, "skip_holidays", None) or "").strip().lower())
        parts.append(int(getattr(restaurant, "module_inventory", 0) or 0))
    conn = get_conn(db_path)
    try:
        for sql, args in (
                ("SELECT COUNT(*), MAX(id) FROM reviews WHERE restaurant_id=? AND processed=1 "
                 "AND deleted_at IS NULL AND entities LIKE '%\"dishes\": [\"%'", (restaurant_id,)),
                ("SELECT COUNT(*), MAX(created_at), SUM(COALESCE(consent,0)), SUM(COALESCE(unsubscribed,0)), "
                 "SUM(COALESCE(email_consent,0)), SUM(COALESCE(email_unsubscribed,0)), MAX(consent_at), "
                 "COUNT(email) FROM guest_contacts WHERE restaurant_id=?", (restaurant_id,)),
                ("SELECT COUNT(*), MAX(responded_at) FROM sms_optin_invites WHERE restaurant_id=? "
                 "AND response='yes'", (restaurant_id,)),
                ("SELECT COUNT(*), MAX(id) FROM email_suppressions", ()),
                ("SELECT MAX(created_at), SUM(sent_count) FROM guest_campaigns WHERE restaurant_id=?",
                 (restaurant_id,)),
                ("SELECT MAX(created_at) FROM guest_newsletters WHERE restaurant_id=?", (restaurant_id,)),
                ("SELECT COUNT(*), MAX(COALESCE(posted_at, created_at)), COUNT(post_id) FROM marketing_content_log "
                 "WHERE restaurant_id=?", (restaurant_id,)),
                ("SELECT COUNT(*), MAX(date), SUM(sales), SUM(COALESCE(final, 1)) FROM labor_daily_history "
                 "WHERE restaurant_id=? AND date >= date(?, '-70 days')", (restaurant_id, today.isoformat())),
                ("SELECT COUNT(*), MAX(business_date), SUM(value) FROM dsr_metrics WHERE restaurant_id=? "
                 "AND metric LIKE 'sales.cat:%' AND business_date >= date(?, '-60 days')",
                 (restaurant_id, today.isoformat())),
                ("SELECT COUNT(*), MAX(updated_at) FROM dsr_category_map WHERE restaurant_id=?", (restaurant_id,)),
                ("SELECT COUNT(*), SUM(COALESCE(sell_price,0)), MAX(id) FROM menu_items WHERE restaurant_id=? "
                 "AND is_active=1", (restaurant_id,)),
                ("SELECT COUNT(*), SUM(COALESCE(qty_per_unit,0)) FROM recipe_ingredients WHERE menu_item_id IN "
                 "(SELECT id FROM menu_items WHERE restaurant_id=?)", (restaurant_id,)),
                ("SELECT COUNT(*), SUM(COALESCE(unit_cost,0)), MAX(updated_at) FROM ingredients "
                 "WHERE restaurant_id=?", (restaurant_id,)),
                ("SELECT COUNT(*), MAX(business_date), SUM(qty_sold) FROM menu_item_sales WHERE restaurant_id=? "
                 "AND business_date >= date(?, '-35 days')", (restaurant_id, today.isoformat()))):
            try:
                row = conn.execute(sql, args).fetchone()
                parts.append(list(row) if row else None)
            except Exception:
                parts.append(None)
    finally:
        conn.close()
    return hashlib.sha256(json.dumps(parts, default=str).encode("utf-8")).hexdigest()[:24]


def cached_build(restaurant_id, db_path=DB_PATH, now=None, restaurant=None) -> dict:
    """build(), stored per restaurant under its fingerprint. A build in
    which a source failed is served but not stored: the next load tries that
    source again instead of saying "couldn't check" all day."""
    import insight_store
    from time_utils import restaurant_now_by_id
    now = now or restaurant_now_by_id(restaurant_id, naive=True)
    if restaurant is None:
        restaurant = _get_restaurant(restaurant_id, db_path)
    fp = fingerprint(restaurant_id, now.date(), db_path, restaurant=restaurant)
    stored = insight_store.get(restaurant_id, CACHE_KIND, fp, db_path=db_path)
    if isinstance(stored, dict) and isinstance(stored.get("cards"), list):
        return stored
    built = build(restaurant_id, db_path=db_path, now=now, restaurant=restaurant)
    if not any(s["state"] == "failed" for s in built["sources"]):
        insight_store.put(restaurant_id, CACHE_KIND, fp, built, db_path=db_path)
    return built


def sees_margins(viewer, restaurant=None) -> bool:
    """Whether this login may see a plate margin: the Food Cost module on
    and FOOD_COST_VIEW (a manager has it withheld on purpose —
    permissions.ROLE_MANAGER). No viewer is an internal caller. Fails
    closed."""
    if restaurant is not None and not getattr(restaurant, "module_inventory", 0):
        return False
    if viewer is None or (isinstance(viewer, dict) and viewer.get("is_admin")):
        return True
    try:
        from permissions import has_permission, FOOD_COST_VIEW
        return bool(has_permission(viewer, FOOD_COST_VIEW))
    except Exception:
        return False


def _learned(restaurant_id, cards, restaurant=None, db_path=DB_PATH):
    """The cards reordered by what this restaurant's own results say (memory
    audit 9/29/26, mkt_results): each card's score times rec_learning's
    weight for its key — the ranker Home, the one-thing pick and the DSR
    already read. It only reorders (acceptance weighs little, upward lift
    comes only from measured success), and now that a guest text's result
    is linked to the card it answered (link_trackers), a slow night whose
    texts brought guests back rises. `learned_weight` rides on a moved
    card. Never raises: unread, the fixed scores stand."""
    try:
        import rec_learning
        learned = rec_learning.effectiveness(restaurant_id, db_path=db_path, restaurant=restaurant)
    except Exception as e:
        print(f"[mkt_opps] learned weights unavailable for {restaurant_id}: {e}")
        return cards
    out = []
    for c in cards:
        try:
            # The recommendation kind is the key's own (slow_day:<Day>), not
            # the card's display kind (slow_night).
            w, _why = learned(c["key"])
        except Exception:
            w = 1.0
        if w and abs(float(w) - 1.0) > 1e-9:
            c = dict(c, score=round(float(c["score"]) * float(w), 2), learned_weight=round(float(w), 3))
        out.append(c)
    return out


def _visible_cards(cards, answered, margins):
    """What one viewer's feed holds, in order: the cards it may see, the
    answered ones gone, THEN each kind capped (re-audit OPP-11) — the
    nearest SLOW_CARDS slow nights, the best DISH_CARDS dishes of each kind
    — with one card per dish: a praised dish whose "put it in front of
    guests" card is on the feed, or was answered, is the same advice
    (insight_store.advice_signature: marketing:dish:<name>)."""
    cards = [c for c in cards if (margins or not c.get("food")) and c["key"] not in answered]
    caps = {"slow_night": SLOW_CARDS, "dish_promote": DISH_CARDS, "dish_praise": DISH_CARDS}
    taken, kept, out = set(), {}, []
    for k in answered:
        if str(k).startswith("dish_promote:"):
            taken.add(str(k).split(":", 1)[1].strip().lower())
    # Promotions first, so the praise cap counts only praise cards that show.
    ordered = sorted(cards, key=lambda c: (c["kind"] != "dish_promote", -c["score"]))
    for c in ordered:
        kind = c["kind"]
        if kind in caps and kept.get(kind, 0) >= caps[kind]:
            continue
        dish = str(c.get("subject") or "").strip().lower()
        if kind == "dish_praise" and dish in taken:
            continue
        if kind == "dish_promote":
            taken.add(dish)
        kept[kind] = kept.get(kind, 0) + 1
        out.append(c)
    out.sort(key=lambda c: -c["score"])
    return out


_ITEM_KEYS = ("key", "kind", "title", "why", "facts", "stake", "when", "days_away", "action", "score",
              "learned_weight")


def feed(restaurant_id, user_id=None, db_path=DB_PATH, surface="marketing", user=None, show_all=False) -> dict:
    """The feed for one viewer and one load.

    Cards this login may not see go first (a plate margin without Food Cost —
    OPP-5; the stored build is per restaurant, so this is at read time), then
    the answered ones, then each kind is capped. Each card gets ONE measured
    confidence (rec_trust.assess, K1) BEFORE it is logged, so the ledger's
    snapshot at delivery carries the % the page shows (K3); a fact (a list,
    a posting gap) carries none. Only the cards on screen are logged as
    shown — the first VISIBLE, or every card once the owner asks for the
    rest (`show_all`, "Show more"): a card behind "Show more" nobody opened
    is not a card that was ignored (OPP-10). `sources` says what each read
    found (checked / no data / failed — OPP-14)."""
    import insight_store
    import rec_trust
    viewer = user
    uid = user_id if user_id is not None else ((viewer or {}).get("id") if isinstance(viewer, dict) else None)
    restaurant = _get_restaurant(restaurant_id, db_path)
    built = cached_build(restaurant_id, db_path=db_path, restaurant=restaurant)
    margins = sees_margins(viewer, restaurant)
    cards = [c for c in built["cards"] if margins or not c.get("food")]
    answered = insight_store.answered(restaurant_id, [c["key"] for c in cards], db_path=db_path)
    cards = _visible_cards(_learned(restaurant_id, cards, restaurant, db_path), answered, margins)
    ctx = rec_trust.Context(restaurant_id, restaurant=restaurant, db_path=db_path)
    items = []
    for c in cards:
        conf = None
        if c.get("evidence"):
            try:
                conf = rec_trust.assess(restaurant_id, c["key"], evidence=c["evidence"],
                                        sources=tuple(c.get("sources") or ()), ctx=ctx)
            except Exception as e:
                print(f"[mkt_opps] confidence unavailable for {c['key']}: {e}")
        items.append(dict(c, text=c["title"], module="marketing", model_written=False, confidence=conf,
                          evidence_sources=(["marketing"] + (["food"] if c.get("food") else [])
                                            + [s for s in c.get("sources") or [] if s != "marketing"]),
                          dollar_value=None))
    # Advice pulling against other advice (memory audit 9/29/26,
    # "conflicts"): promoting a dish whose ingredient is critically low, or
    # filling a night another surface says to trim — the card carries
    # `conflict`, or is held when the owner already settled it.
    try:
        import lever_conflicts
        items = lever_conflicts.apply(restaurant_id, items, lever_conflicts.facts(restaurant_id, db_path=db_path),
                                      db_path=db_path)
    except Exception as e:
        print(f"[mkt_opps] lever conflicts unavailable for {restaurant_id}: {e}")
    on_screen = items if show_all else items[:VISIBLE]
    shown = insight_store.present_recs(restaurant_id, "marketing", surface, on_screen, user_id=uid,
                                       db_path=db_path)
    ids = {s["key"]: s.get("rec_id") for s in shown}
    gone = {c["key"] for c in on_screen} - set(ids)          # answered since the build: silenced
    out = []
    for c in items:
        if c["key"] in gone:
            continue
        item = {k: c.get(k) for k in _ITEM_KEYS}
        item["rec_id"] = ids.get(c["key"])
        item["confidence"] = c.get("confidence")
        item["conflict"] = c.get("conflict")
        out.append(item)
    sources = [dict(s) for s in built.get("sources") or [] if margins or s["key"] not in FOOD_SOURCES]
    return {"ok": True, "items": out, "visible": VISIBLE, "sources": sources,
            "checked": [s["label"] for s in sources if s["state"] == "checked"]}


# ── what the other Marketing surfaces are told ──────────────────────────────

def context_lines(restaurant_id, db_path=DB_PATH, limit=5) -> list:
    """The feed's current cards as plain lines, for the Marketing brief and
    the content calendar (AUX-3): ranked, answered ones gone, capped as the
    feed caps them, and without a plate margin (the brief and the calendar
    are read by every Marketing login). Nothing is logged as shown — the
    feed logs its own. Each line: "Fill Tuesday, 10/6/26 — Tuesdays here run
    18% under a typical day: 8 of the last 8 came in under it." Never raises
    ([] when the feed can't be read)."""
    try:
        import insight_store
        restaurant = _get_restaurant(restaurant_id, db_path)
        built = cached_build(restaurant_id, db_path=db_path, restaurant=restaurant)
        cards = [c for c in built["cards"] if not c.get("food")]
        answered = insight_store.answered(restaurant_id, [c["key"] for c in cards], db_path=db_path)
        cards = _visible_cards(_learned(restaurant_id, cards, restaurant, db_path), answered, False)[:limit]
        return [{"key": c["key"], "kind": c["kind"], "sources": list(c.get("sources") or ()),
                 "channels": list((c.get("action") or {}).get("channels") or ()),
                 "line": f"{c['title']} — {c['why']}"} for c in cards]
    except Exception as e:
        _capture(e, restaurant_id, "context_lines")
        return []


def implemented_by_send(restaurant_id, rec_key, channel, user_id=None, sent=None, db_path=DB_PATH) -> bool:
    """A Campaign Studio send that began on a feed card ("Draft it" hands
    the card's key to the Studio, and each send route passes it here): the
    card was acted on, whichever channel went (re-audit OPP-10) — a holiday,
    dish, category or list card acted on through the Studio used to expire
    as ignored. Only a feed kind, and only an episode someone was shown
    (rec_ledger.implemented records nothing for a key nobody was shown).
    Idempotent per card, channel and day. Never raises."""
    key = str(rec_key or "").strip()[:160]
    if not key:
        return False
    try:
        import rec_ledger
        if rec_ledger.kind_of(key) not in FEED_KINDS:
            return False
        from time_utils import restaurant_now_by_id
        day = restaurant_now_by_id(restaurant_id, naive=True).date().isoformat()
        return bool(rec_ledger.implemented(restaurant_id, key, "marketing", user_id=user_id,
                                           source_ref=f"studio:{channel}:{day}",
                                           meta={"module": "marketing", "channel": channel, "sent": sent},
                                           db_path=db_path))
    except Exception as e:
        _capture(e, restaurant_id, "implemented_by_send")
        return False
