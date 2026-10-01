"""
event_intel.gameday — marketing, prep, ordering, the season's money and the
heads-up before a big game (Event Intelligence phase 3, 10/1/26).

  item_mix(rid, e)      what past games of the same side, kickoff and
                        season class sold, item by item (pos_ticket_lines),
                        against a usual same weekday: the items that rose by
                        ITEM_MIN_EXTRA units and ITEM_MIN_RATIO times usual
  prep_lines(rid, e)    "Prep for about 54 Wings" — only when SEGMENT_MIN_N
                        such games were measured and the item rose on every
                        one; sized by what those games sold, never scaled by
                        a guess
  order_bump(rid, e)    the same rise through the recipes (recipe_ingredients):
                        the extra of each ingredient games like it used, in
                        its own unit — the game-week ordering bump, under the
                        same floor as prep
  send_plan(e, r)       when to text and email guests about a game: three
                        hours before kickoff inside the legal 8am–9pm texting
                        window, the email the day before. A starting rule,
                        said as one — nothing here has measured send times
  campaign_goal(rid, e) the Campaign Studio goal a game starts from, naming
                        the items game nights sell here (never a price or an
                        offer the owner didn't make)
  season_value(rid, s)  what the season's games brought at this restaurant:
                        each measured night's net over its usual same weekday,
                        summed, beside how many games went unmeasured. It is
                        what the games did — never Cavnar AI's value, never
                        summed into value_delivered
  week_note(rid, today) the Food Cost order section's line for a followed
                        game in the next ORDER_WINDOW_DAYS
  run_event_push()      the daily heads-up the afternoon before a big game:
                        one push per game, only when games like it were
                        measured BIG_LIFT above usual (or, with one measured,
                        the last one ran BIG_LAST_LIFT above), to the logins
                        who read labor

Nothing here calls a model, sends to a guest, or raises into its caller.
"""
import logging
from datetime import date, datetime, timedelta

from event_intel import engine, playbook, store

log = logging.getLogger(__name__)

ITEM_MIN_EXTRA = 5          # units an item must rise on a game night to be said
ITEM_MIN_RATIO = 1.5        # …and at least this many times a usual night
ITEMS_SHOWN = 5
ORDER_WINDOW_DAYS = 7
TEXT_LEAD_HOURS = 3
BIG_LIFT = 25.0             # a measured segment this far above usual is a big game
BIG_LAST_LIFT = 50.0        # with one measured game: the last one ran this far above
EVENT_PUSH_HOUR = 14        # the afternoon before (restaurant-local), a 4-hour window
PUSH_TYPE = "event_ahead"

_d = playbook._d
_mdy = playbook._mdy


def _num(v):
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def _weekday_of(dates):
    days = {_d(x).strftime("%A") for x in dates}
    return days.pop() if len(days) == 1 else None


# ── item mix ────────────────────────────────────────────────────────────────

def _items_on(restaurant_id, iso, db_path):
    """{item: units} sold on one business date; {} with no lines on file."""
    conn = store.get_conn(db_path)
    try:
        rows = conn.execute("SELECT item_name, SUM(qty) AS u FROM pos_ticket_lines WHERE restaurant_id=? AND "
                            "business_date=? AND kind='sale' AND item_kind='dish' AND COALESCE(voided,0)=0 "
                            "AND item_name IS NOT NULL GROUP BY item_name", (restaurant_id, iso)).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    return {r["item_name"]: float(r["u"] or 0) for r in rows if (r["u"] or 0) > 0}


def _kind_one(e) -> str:
    """"home game" / "home prime-time game" / "night like it"."""
    words = engine.kind_words(e)
    return words[:-1] if words.endswith("games") else "night like it"


def item_mix(restaurant_id, e, db_path=store.DB_PATH):
    """{"games": [{"date", "describe"}], "n", "usual_n", "weekday", "items":
    [{"item", "game", "usual", "extra", "every_game", "per_game"}], "text",
    "basis"} or None with no game of the same class with item lines on
    file and USUAL_MIN usual nights of its own.

    Each game is set against ITS OWN usual same weekday (audit 10/1/26, the
    same fix as playbook.staffing): an item's extra is the median of the
    per-game extras, and it counts when that clears ITEM_MIN_EXTRA units and
    ITEM_MIN_RATIO times usual; `every_game` when each game cleared both."""
    try:
        games = [g for g in engine.past_games(restaurant_id, e, db_path=db_path) if playbook._same_class(e, g["event"])]
        nights, usual_n = [], 0
        for g in games:
            iso = g["event"]["event_date"]
            items = _items_on(restaurant_id, iso, db_path)
            if not items:
                continue
            own = [u for u in (_items_on(restaurant_id, d, db_path)
                               for d in playbook.usual_nights(restaurant_id, iso, db_path=db_path)) if u]
            if len(own) < playbook.USUAL_MIN:
                continue
            names = set(items) | {k for u in own for k in u}
            nights.append({"date": iso, "describe": engine.describe(g["event"]), "items": items,
                           "usual": {k: engine._median([u.get(k, 0.0) for u in own]) for k in names}})
            usual_n += len(own)
        if not nights:
            return None
        out = []
        for name in {k for n in nights for k in n["items"]}:
            per = [n["items"].get(name, 0.0) for n in nights]
            own_usual = [n["usual"].get(name, 0.0) for n in nights]
            extras = [p - u for p, u in zip(per, own_usual)]
            game, usual, extra = engine._median(per), engine._median(own_usual), engine._median(extras)
            if extra < ITEM_MIN_EXTRA or game < ITEM_MIN_RATIO * usual:
                continue
            every = all(p - u >= ITEM_MIN_EXTRA and p >= ITEM_MIN_RATIO * u for p, u in zip(per, own_usual))
            out.append({"item": name, "game": game, "usual": usual, "extra": extra, "every_game": every,
                        "per_game": per})
        out.sort(key=lambda x: (-x["extra"], x["item"]))
        out = out[:ITEMS_SHOWN]
        n = len(nights)
        weekday = _weekday_of([x["date"] for x in nights])
        usual_word = f"a usual {weekday}" if weekday else "their usual weekday"
        said = ", ".join(f"{_num(x['game'])} {x['item']} (usual {_num(round(x['usual']))})" for x in out[:3])
        if not out:
            text = None
        elif n == 1:
            text = f"Your last {_kind_one(e)} sold {said} — against {usual_word}."
        else:
            text = f"Your last {n} {engine.kind_words(e)} sold a median {said} — against {usual_word}."
        return {"games": [{"date": x["date"], "describe": x["describe"]} for x in nights], "n": n,
                "usual_n": usual_n, "weekday": weekday, "items": out, "text": text,
                "basis": (f"items sold on {n} {engine.kind_words(e) if n != 1 else _kind_one(e)} (checks on file), "
                          f"each against the ordinary same weekdays before it; an item counts when it rose "
                          f"{ITEM_MIN_EXTRA}+ units and {ITEM_MIN_RATIO:g}× usual")}
    except Exception as ex:
        log.warning("event_intel.gameday item_mix failed rid=%s: %s", restaurant_id, ex)
        return None


def _planned(mix):
    """The items a plan may rest on: SEGMENT_MIN_N games, risen on every one."""
    if not mix or mix["n"] < engine.SEGMENT_MIN_N:
        return []
    return [x for x in mix["items"] if x["every_game"]]


def prep_lines(restaurant_id, e, mix=None, db_path=store.DB_PATH) -> list:
    """[{"item", "qty", "text"}] — what to prep for, sized by what games
    like it sold here; [] below the floor."""
    mix = mix if mix is not None else item_mix(restaurant_id, e, db_path=db_path)
    out = []
    for x in _planned(mix):
        sold = " and ".join(_num(p) for p in x["per_game"]) if len(x["per_game"]) <= 3 else \
            f"{_num(min(x['per_game']))}–{_num(max(x['per_game']))}"
        out.append({"item": x["item"], "qty": round(x["game"]),
                    "text": f"about {round(x['game'])} {x['item']} (your last {mix['n']} "
                            f"{engine.kind_words(e)} sold {sold}; a usual night {_num(round(x['usual']))})"})
    return out


# ── the game-week ordering bump ────────────────────────────────────────────

def order_bump(restaurant_id, e, mix=None, db_path=store.DB_PATH):
    """{"lines": [{"ingredient", "unit", "extra", "items"}], "text", "basis"}
    — the extra of each ingredient games like it used, through the recipes;
    None below the plan floor or with no recipe for the items that rose."""
    mix = mix if mix is not None else item_mix(restaurant_id, e, db_path=db_path)
    planned = _planned(mix)
    if not planned:
        return None
    conn = store.get_conn(db_path)
    try:
        by_ing = {}
        for x in planned:
            rows = conn.execute(
                "SELECT i.name, i.unit, ri.qty_per_unit FROM menu_items m JOIN recipe_ingredients ri "
                "ON ri.menu_item_id=m.id JOIN ingredients i ON i.id=ri.ingredient_id AND i.restaurant_id=m.restaurant_id "
                "WHERE m.restaurant_id=? AND lower(trim(m.name))=lower(trim(?)) AND COALESCE(i.is_active,1)=1",
                (restaurant_id, x["item"])).fetchall()
            for r in rows:
                try:
                    q = float(r["qty_per_unit"] or 0) * x["extra"]
                except (TypeError, ValueError):
                    continue
                if q <= 0:
                    continue
                g = by_ing.setdefault(r["name"], {"ingredient": r["name"], "unit": r["unit"] or "", "extra": 0.0,
                                                  "items": []})
                g["extra"] += q
                if x["item"] not in g["items"]:
                    g["items"].append(x["item"])
    except Exception as ex:
        log.warning("event_intel.gameday order_bump failed rid=%s: %s", restaurant_id, ex)
        return None
    finally:
        conn.close()
    lines = sorted(by_ing.values(), key=lambda g: -g["extra"])
    if not lines:
        return None
    for g in lines:
        g["extra"] = round(g["extra"], 1)
    said = ", ".join(f"{_num(g['extra'])} {g['unit']} {g['ingredient']}".replace("  ", " ") for g in lines[:4])
    return {"lines": lines, "n": mix["n"],
            "text": f"Order about {said} more than a usual week — what your last {mix['n']} {engine.kind_words(e)} "
                    f"used beyond a usual night.",
            "basis": "the items that rose on every one of those games, by what each sold beyond a usual same weekday, "
                     "through their recipes"}


# ── when to reach guests ────────────────────────────────────────────────────

def _clock_dt(dt):
    return engine._clock(dt.strftime("%H:%M"))


def send_plan(e):
    """{"text_at" (ISO local), "text_words", "email_by" (ISO date),
    "email_words", "basis"} for a dated game with a kickoff, else None."""
    try:
        import guest_marketing as gm
        if not e.get("event_date") or not e.get("kickoff_local"):
            return None
        day = _d(e["event_date"])
        kick = datetime.combine(day, datetime.strptime(str(e["kickoff_local"])[:5], "%H:%M").time())
        at = kick - timedelta(hours=TEXT_LEAD_HOURS)
        earliest = datetime.combine(day, datetime.min.time()).replace(hour=gm.GUEST_SMS_EARLIEST_HOUR)
        latest = datetime.combine(day, datetime.min.time()).replace(hour=gm.GUEST_SMS_LATEST_HOUR) - timedelta(hours=1)
        if at < earliest:
            # An early kickoff (a London game): the window's first hour if it
            # still leaves an hour, else the evening before.
            at = earliest if kick - earliest >= timedelta(hours=1) else \
                datetime.combine(day - timedelta(days=1), datetime.min.time()).replace(hour=17)
        at = min(at, latest)
        at = at.replace(minute=(at.minute // 15) * 15)
        lead = kick - at
        mins = int(lead.total_seconds() // 60)
        h, m = divmod(mins, 60)
        span = (f"{h} hour{'s' if h != 1 else ''}" if h else "") + (f"{' ' if h else ''}{m} minutes" if m else "")
        lead_words = "the evening before" if lead >= timedelta(hours=12) else f"{span} before {engine.start_word(e)}"
        email_day = day - timedelta(days=1)
        return {"text_at": at.isoformat(timespec="minutes"),
                "text_words": f"{at.strftime('%A')} around {_clock_dt(at)}, {lead_words}",
                "email_by": email_day.isoformat(),
                "email_words": f"{email_day.strftime('%A')} {_mdy(email_day.isoformat())}, the day before",
                "basis": (f"a starting rule — the text {TEXT_LEAD_HOURS} hours before {engine.start_word(e)} inside the "
                          f"{gm.guest_sms_window_label()} texting window, the email the day before; Cavnar AI "
                          f"hasn't measured send times here yet")}
    except Exception as ex:
        log.warning("event_intel.gameday send_plan failed: %s", ex)
        return None


def campaign_goal(restaurant_id, e, mix=None, db_path=store.DB_PATH) -> str:
    """"Bring guests in to watch Bears vs New York Jets on Sunday 10/4
    (12pm, FOX) — feature Wings and Salt Caramel Tini, what game nights sell
    here". Never a price, a discount or an offer: the owner adds those."""
    short = e.get("short_name") or e.get("series_name") or ""
    who = (f"{short} {'vs' if e.get('home_away') == 'home' else 'at'} {e['opponent']}" if e.get("opponent")
           else engine.describe(e, with_date=False))
    when = ""
    if e.get("event_date"):
        day = _d(e["event_date"])
        when = f" on {day.strftime('%A')} {day.month}/{day.day}"
    extras = [x for x in (engine._clock(e.get("kickoff_local")), e.get("broadcast")) if x]
    goal = f"Bring guests in to watch {who}{when}" + (f" ({', '.join(extras)})" if extras else "")
    try:
        mix = mix if mix is not None else item_mix(restaurant_id, e, db_path=db_path)
    except Exception:
        mix = None
    top = [x["item"] for x in (mix or {}).get("items") or []][:2]
    if top:
        goal += f" — feature {' and '.join(top)}, what game nights sell here"
    return goal[:200]


# ── the season's money ──────────────────────────────────────────────────────

def season_value(restaurant_id, series_id, today=None, season=None, db_path=store.DB_PATH):
    """{"played", "measured", "incremental", "games": [{"describe", "net",
    "usual", "extra"}], "text", "basis"} for the series' games played before
    `today` (this season when given), or None with none played."""
    try:
        rows = store.events_for([series_id], None, None, db_path=db_path)
        today = _d(today) if today else date.today()
        played = [x for x in rows if x["event_date"] < today.isoformat()
                  and x.get("status") not in ("cancelled", "postponed")]
        # This season unless one is named (audit 10/1/26: "so far" summed
        # every season in the catalog once a second one was loaded).
        if season is None and played:
            season = played[-1].get("season")
        if season is not None:
            played = [x for x in played if x.get("season") == season]
        if not played:
            return None
        word = played[0].get("short_name") or played[0].get("series_name") or "Event"
        outs = engine._outcomes(restaurant_id, [x["event_date"] for x in played], db_path, series_word=word)
        games, mixed, seen = [], 0, set()
        for x in played:
            o = outs.get(x["event_date"])
            if x["event_date"] in seen:
                continue                      # a doubleheader is one night
            seen.add(x["event_date"])
            if o and o.get("net") is not None and o.get("baseline") is not None:
                if int(o.get("confounded") or 0):
                    mixed += 1                # Christmas, a party: not the game's money alone
                    continue
                games.append({"describe": engine.describe(x), "date": x["event_date"], "net": float(o["net"]),
                              "usual": float(o["baseline"]), "extra": round(float(o["net"]) - float(o["baseline"]), 2)})
        total = round(sum(g["extra"] for g in games), 2)
        unmeasured = len(seen) - len(games) - mixed
        if games:
            text = (f"{word} games so far: {len(seen)} played, {len(games)} measured here — "
                    f"{'+' if total >= 0 else '−'}${abs(total):,.0f} over a usual same weekday"
                    + (f"; {unmeasured} without a usual night to measure against" if unmeasured else "")
                    + (f"; {mixed} left out with something else on that night" if mixed else "") + ".")
        else:
            text = f"{word} games so far: {len(seen)} played, none measured here yet."
        return {"played": len(seen), "measured": len(games), "incremental": total if games else None,
                "season": season, "mixed": mixed,
                "games": games, "text": text,
                "basis": ("each game night's net against the median of ordinary same weekdays in the 8 weeks before "
                          "(event memory) — what the games brought, before and after, not something Cavnar AI did")}
    except Exception as ex:
        log.warning("event_intel.gameday season_value failed rid=%s: %s", restaurant_id, ex)
        return None


# ── the Food Cost order section's game-week line ───────────────────────────

def week_note(restaurant_id, today=None, db_path=store.DB_PATH):
    """{"event_id", "describe", "text", "order", "basis"} for the next
    followed game within ORDER_WINDOW_DAYS, or None."""
    try:
        if today is None:
            import models
            today = engine._today(models.get_restaurant(restaurant_id, db_path=db_path))
        today = _d(today)
        followed = store.follows(restaurant_id, db_path=db_path)
        skip = store.dismissed(restaurant_id, db_path=db_path)
        rows = [e for e in store.events_for([f["series_id"] for f in followed], today,
                                            today + timedelta(days=ORDER_WINDOW_DAYS), db_path=db_path)
                if e.get("status") not in ("cancelled", "postponed") and e["id"] not in skip
                and engine.headline(restaurant_id, e, db_path=db_path)]
        if not rows:
            return None
        e = rows[0]
        mix = item_mix(restaurant_id, e, db_path=db_path)
        bump = order_bump(restaurant_id, e, mix=mix, db_path=db_path)
        if bump:
            text = bump["text"]
        elif mix and mix.get("text") and mix["n"] == 1:
            text = mix["text"] + " One game — not yet a pattern to order on."
        elif mix and mix.get("text") and _planned(mix):
            text = mix["text"] + " No recipe links those items to ingredients yet, so nothing is sized for the order."
        elif mix and mix.get("text"):
            text = mix["text"] + " Not on every game — not yet a pattern to order on."
        elif mix:
            what = _kind_one(e) if mix["n"] == 1 else f"{mix['n']} {engine.kind_words(e)}"
            text = f"Your last {what} sold no item well above a usual night — order for a usual week."
        else:
            text = "No game like it measured here yet — order for a usual week."
        return {"event_id": e["id"], "describe": engine.describe(e), "text": text, "order": bump,
                "items": (mix or {}).get("items") or [], "basis": (bump or mix or {}).get("basis")}
    except Exception as ex:
        log.warning("event_intel.gameday week_note failed rid=%s: %s", restaurant_id, ex)
        return None


# ── the heads-up the afternoon before a big game ───────────────────────────

def big_game(restaurant_id, e, db_path=store.DB_PATH):
    """(True, words) when games like this one ran big here, measured; else
    (False, None). Words name the measurement, never a guess."""
    eff = engine.effect_for(restaurant_id, e, db_path=db_path)
    if eff and eff.get("median_lift_pct") is not None:
        if float(eff["median_lift_pct"]) >= BIG_LIFT:
            return True, eff["basis"][0].upper() + eff["basis"][1:] + "."
        return False, None
    last = engine.last_like(restaurant_id, e, db_path=db_path)
    if (last and last.get("lift_pct") is not None and float(last["lift_pct"]) >= BIG_LAST_LIFT
            and playbook._same_class(e, last["event"])):
        return True, (f"Your last one like it, {engine.describe(last['event'], with_date=True)}, ran "
                      f"{float(last['lift_pct']):+.0f}% against a usual "
                      f"{_d(last['event']['event_date']).strftime('%A')} — one night.")
    return False, None


def _labor_logins(restaurant_id, db_path):
    """Every console login here who reads labor — brief preference or not:
    the push has its own per-type mute (preferences.push_allowed)."""
    import morning_brief
    from permissions import LABOR_VIEW, has_permission
    return {u["id"] for u in morning_brief.recipients(restaurant_id, db_path, include_opted_out=True)
            if has_permission(u, LABOR_VIEW)}


def _claim_key(restaurant_id):
    return f"event_push:{restaurant_id}"


def _claim_period(e):
    """One push per game per date: a game moved after its push gets one more."""
    return f"{e['id']}:{e.get('event_date')}"


def tomorrows_games(restaurant, today, db_path=store.DB_PATH) -> list:
    tmr = _d(today) + timedelta(days=1)
    followed = store.follows(restaurant.id, db_path=db_path)
    skip = store.dismissed(restaurant.id, db_path=db_path)
    return [e for e in store.events_for([f["series_id"] for f in followed], tmr, tmr, db_path=db_path)
            if e.get("status") not in ("cancelled", "postponed") and e["id"] not in skip
            and engine.headline(restaurant.id, e, db_path=db_path)]


def push_for(restaurant, today, db_path=store.DB_PATH, events=None):
    """[(event, title, body)] for tomorrow's big games at this restaurant
    (every one, not just the first). Pure read."""
    out = []
    for e in (events if events is not None else tomorrows_games(restaurant, today, db_path=db_path)):
        big, words = big_game(restaurant.id, e, db_path=db_path)
        if not big:
            continue
        body = [words]
        st = playbook.staffing(restaurant.id, e, db_path=db_path)
        if st and st.get("recommend"):
            body.append(st["text"].split(". On your last")[0] + ".")
        # When to text guests only where Marketing is on (the brief's rule).
        plan = send_plan(e) if getattr(restaurant, "module_marketing", 0) else None
        if plan:
            body.append(f"Guest text: {plan['text_words']}.")
        title = f"Tomorrow: {engine.describe(e, with_date=False)}"
        out.append((e, title[:120], " ".join(body)[:400]))
    return out


def run_event_push(db_path=None, restaurants=None) -> dict:
    """The afternoon before a big game, one push per game per restaurant
    (claimed on the game and its date), to the logins who read labor and have
    a phone that takes it. Push only, P3 — it never sounds through a Focus
    mode. Returns the standard slot counts: `attempted` a restaurant with an
    unclaimed game tomorrow, `skipped` one with nobody to push to or over its
    briefing budget."""
    import ops
    import push
    import strategy_jobs as sj
    from time_utils import restaurant_now
    db = db_path or store.DB_PATH
    st = {"attempted": 0, "failed": 0}
    sent = skipped = 0
    for r in sj._slot_iter("event_push", restaurants, db, state=st):
        try:
            local = restaurant_now(r, naive=True)
            if not (EVENT_PUSH_HOUR <= local.hour < EVENT_PUSH_HOUR + 4):
                continue
            # Claimed games are skipped BEFORE anything is computed: the job
            # runs every 20 minutes through the window (audit 10/1/26).
            todo = [e for e in tomorrows_games(r, local.date(), db_path=db)
                    if not ops.period_claimed(_claim_key(r.id), _claim_period(e))]
            if not todo:
                continue
        except Exception as ex:
            st["attempted"] += 1
            st["failed"] += 1
            ops.capture(ex, job="event_push", context=f"restaurant_id={r.id}")
            continue
        st["attempted"] += 1
        try:
            got = push_for(r, local.date(), db_path=db, events=todo)
            if not got:
                continue
            audience = sj.deliverable_audience(r.id, _labor_logins(r.id, db), db)
            import notify
            if not audience or not notify.briefing_allowed(r.id, PUSH_TYPE, db):
                skipped += 1
                continue
            import nav
            for e, title, body in got:
                if not ops.claim_period(_claim_key(r.id), _claim_period(e)):
                    continue
                alert_id = notify.record_notification(r.id, PUSH_TYPE, db_path=db, ref_kind="catalog_event",
                                                      ref_id=e["id"])
                push.fire_push(r.id, PUSH_TYPE, title, body,
                               data={"nav": nav.path("ask", q=f"How should we get ready for {engine.describe(e)}?"),
                                     "alert_id": alert_id, "surface": "alert_push", "answerable": False,
                                     "event_id": e["id"]},
                               db_path=db, user_ids=audience)
                sent += 1
        except Exception as ex:
            st["failed"] += 1
            ops.capture(ex, job="event_push", context=f"restaurant_id={r.id}")
    return sj._slot_counts(st, sent=sent, skipped=skipped)
