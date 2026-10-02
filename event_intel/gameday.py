"""
event_intel.gameday — marketing, prep, ordering, the season's money and the
heads-up before a big game (Event Intelligence phase 3, 10/1/26).

  item_mix(rid, e)      what past games of the same kind (engine.game_class
                        and kickoff class) sold, item by item
                        (pos_ticket_lines), against a usual same weekday: the
                        items that rose by ITEM_MIN_EXTRA units and
                        ITEM_MIN_RATIO times usual. Confounded nights are left
                        out while clean ones suffice (store.clean_first);
                        when they can't, it says so and plans nothing
  item_mix_visible(…)   who may read it — one rule for every surface
  prep_lines(rid, e)    "Prep for about 54 Wings" — only when SEGMENT_MIN_N
                        such games were measured and the item rose on every
                        one; sized by what those games sold, never scaled by
                        a guess
  order_bump(rid, e)    the same rise through the recipes (recipe_ingredients):
                        the extra of each ingredient games like it used, in
                        its own unit — the game-week ordering bump, under the
                        same floor as prep
  send_plan(e, tz)      when to text and email guests about a game: three
                        hours before kickoff (on the restaurant's clock)
                        inside the legal 8am–9pm texting window, the email
                        the day before. A starting rule, said as one —
                        nothing here has measured send times
  plan_ahead(rid, e)    send_plan with nothing already gone at the
                        restaurant — what the brief, the push and Ask say
  guest_text_visible(…) who may read the send times — one rule for every
                        surface: the Marketing module and the login's view
  campaign_goal(rid, e) the Campaign Studio goal a game starts from, naming
                        the items game nights sell here past the plan floor
                        (never a price or an offer the owner didn't make)
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
                        the last clean one of its kind ran BIG_LAST_LIFT
                        above), to the logins who read labor

Nothing here calls a model, sends to a guest, or raises into its caller.
"""
import logging
from datetime import datetime, timedelta

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

def _same_kind(e, g) -> bool:
    """Games a plan for `e` may rest on: the same engine.game_class (side,
    preseason, the home ground or another — re-audit SD-02, A1 handoff 6)
    and the same kickoff class (prime time or not)."""
    return engine.same_kind(e, g)


def _items_by_date(restaurant_id, dates, db_path) -> dict:
    """{date: {item: units}} sold on each business date, in ONE query for
    a whole item_mix (re-audit X-2: a usual night was read once per game it
    served, each over its own connection). A date with no lines is absent."""
    dates = sorted({str(d)[:10] for d in dates if d})
    if not dates:
        return {}
    marks = ",".join("?" for _ in dates)
    conn = store.get_conn(db_path)
    try:
        rows = conn.execute(f"SELECT business_date, item_name, SUM(qty) AS u FROM pos_ticket_lines WHERE "
                            f"restaurant_id=? AND business_date IN ({marks}) AND kind='sale' AND item_kind='dish' "
                            f"AND COALESCE(voided,0)=0 AND item_name IS NOT NULL GROUP BY business_date, item_name",
                            [restaurant_id] + dates).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    out = {}
    for r in rows:
        if (r["u"] or 0) > 0:
            out.setdefault(r["business_date"], {})[r["item_name"]] = float(r["u"])
    return out


def _kind_one(e) -> str:
    """"home game" / "home prime-time game" / "night like it"."""
    words = engine.kind_words(e)
    return words[:-1] if words.endswith("games") else "night like it"


def item_mix(restaurant_id, e, db_path=store.DB_PATH):
    """{"games": [{"date", "describe"}], "n", "usual_n", "weekday", "items":
    [{"item", "game", "usual", "extra", "every_game", "per_game"}],
    "confounded", "mixed", "text", "basis"} or None with no game of the
    same kind (_same_kind) with item lines on file and USUAL_MIN usual
    nights of its own.

    Each game is set against ITS OWN usual same weekday (audit 10/1/26, the
    same fix as playbook.staffing): an item's extra is the median of the
    per-game extras, and it counts when that clears ITEM_MIN_EXTRA units and
    ITEM_MIN_RATIO times usual; `every_game` when each game cleared both.

    The clean-nights rule (store.clean_first, effect_for's): a night that
    had something else on is left out while clean nights reach
    SEGMENT_MIN_N; when they can't, every night counts, `confounded` is
    True, the text says so, and nothing is planned on it (_planned,
    re-audit P3-01). Reads are bounded by engine.past_games and made once
    per call: the game nights' lines in one query, the usual nights' in
    one more (re-audit X-2)."""
    try:
        tz = engine.restaurant_clock(restaurant_id, db_path=db_path)
        games = [g for g in engine.past_games(restaurant_id, e, db_path=db_path) if _same_kind(e, g["event"])]
        sold = _items_by_date(restaurant_id, [g["event"]["event_date"] for g in games], db_path)
        games = [g for g in games if sold.get(g["event"]["event_date"])]
        usual = {g["event"]["event_date"]: playbook.usual_nights(restaurant_id, g["event"]["event_date"],
                                                                  db_path=db_path) for g in games}
        sold.update(_items_by_date(restaurant_id, {d for v in usual.values() for d in v} - set(sold), db_path))
        nights = []
        for g in games:
            iso = g["event"]["event_date"]
            own = [sold[d] for d in usual[iso] if sold.get(d)]
            if len(own) < playbook.USUAL_MIN:
                continue
            items = sold[iso]
            names = set(items) | {k for u in own for k in u}
            nights.append({"date": iso, "describe": engine.describe(g["event"], tz=tz), "items": items,
                           "usual_n": len(own), "outcome": g["outcome"],
                           "usual": {k: engine._median([u.get(k, 0.0) for u in own]) for k in names}})
        nights, confounded = store.clean_first(nights, engine.SEGMENT_MIN_N)
        if not nights:
            return None
        mixed = sum(1 for x in nights if store.confounded(x["outcome"]))
        out = []
        for name in {k for n in nights for k in n["items"]}:
            per = [n["items"].get(name, 0.0) for n in nights]
            own_usual = [n["usual"].get(name, 0.0) for n in nights]
            extras = [p - u for p, u in zip(per, own_usual)]
            game, usual_u, extra = engine._median(per), engine._median(own_usual), engine._median(extras)
            if extra < ITEM_MIN_EXTRA or game < ITEM_MIN_RATIO * usual_u:
                continue
            every = all(p - u >= ITEM_MIN_EXTRA and p >= ITEM_MIN_RATIO * u for p, u in zip(per, own_usual))
            out.append({"item": name, "game": game, "usual": usual_u, "extra": extra, "every_game": every,
                        "per_game": per})
        out.sort(key=lambda x: (-x["extra"], x["item"]))
        out = out[:ITEMS_SHOWN]
        n = len(nights)
        weekday = _weekday_of([x["date"] for x in nights])
        usual_word = f"a usual {weekday}" if weekday else "their usual weekday"
        said = ", ".join(f"{_num(x['game'])} {x['item']} (usual {_num(round(x['usual']))})" for x in out[:3])
        also = ""
        if confounded:
            also = (", though that night had something else on too" if n == 1 else
                    f", though {mixed} of those {n} nights had something else on too")
        if not out:
            text = None
        elif n == 1:
            text = f"Your last {_kind_one(e)} sold {said} — against {usual_word}{also}."
        else:
            text = f"Your last {n} {engine.kind_words(e)} sold a median {said} — against {usual_word}{also}."
        return {"games": [{"date": x["date"], "describe": x["describe"]} for x in nights], "n": n,
                "usual_n": sum(x["usual_n"] for x in nights), "weekday": weekday, "items": out,
                "confounded": confounded, "mixed": mixed, "text": text,
                "basis": (f"items sold on {n} {engine.kind_words(e) if n != 1 else _kind_one(e)} (checks on file), "
                          f"each against the ordinary same weekdays before it; an item counts when it rose "
                          f"{ITEM_MIN_EXTRA}+ units and {ITEM_MIN_RATIO:g}× usual"
                          + ("; some of those nights had something else on, so no plan rests on them" if confounded
                             else "; a night that had something else on is left out while clean ones suffice"))}
    except Exception as ex:
        log.warning("event_intel.gameday item_mix failed rid=%s: %s", restaurant_id, ex)
        return None


def _planned(mix):
    """The items a plan may rest on: SEGMENT_MIN_N games — never a mix that
    had to count a night with something else on — risen on every one."""
    if not mix or mix.get("confounded") or mix.get("n", 0) < engine.SEGMENT_MIN_N:
        return []
    return [x for x in mix.get("items") or [] if x["every_game"]]


def unplanned_words(mix, verb="prep"):
    """Why a mix that says something carries no plan, said after its text:
    one game, nights that had something else on, or items that didn't rise
    on every game. None when it carries a plan or says nothing. For Food
    Cost's game week ("order") and the report's day after ("prep")."""
    if not mix or not mix.get("text") or _planned(mix):
        return None
    if mix.get("n") == 1:
        return f"One game — not yet a pattern to {verb} on."
    if mix.get("confounded"):
        return f"Not yet a pattern to {verb} on."
    return f"Not on every game — not yet a pattern to {verb} on."


def item_mix_visible(user=None, denied=None) -> bool:
    """Who may read a game's item mix (units sold on game nights against
    usual, and the prep sized from them) — the ONE rule for the brief's game
    alert, Ask's read_events, Food Cost's game week and the report's day
    after (re-audit X-8): a login with the Labor view (the brief's sales
    figures) or the Food Cost view (the menu). It carries no dollars.
    `denied` is a viewer's denied module keys (Ask and the brief: "labor",
    "inventory"; the report's withheld blocks: "labor", "food"); `user` a
    login dict. Neither: an internal caller, which may."""
    if denied is not None:
        d = set(denied)
        return not ("labor" in d and d & {"inventory", "food"})
    if user is None or user.get("is_admin"):
        return True
    from permissions import FOOD_COST_VIEW, LABOR_VIEW, has_permission
    return bool(has_permission(user, LABOR_VIEW) or has_permission(user, FOOD_COST_VIEW))


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
            # ONE recipe per item name: the newest ACTIVE menu item of that
            # name that has one. Two rows can share a name (an inactive
            # "Wings" kept beside its replacement, a manual row beside a
            # POS-discovered one, RPOWER's cleaned names) and the old join
            # added the extra once per row (re-audit P3-03).
            one = conn.execute(
                "SELECT m.id FROM menu_items m WHERE m.restaurant_id=? AND lower(trim(m.name))=lower(trim(?)) "
                "AND COALESCE(m.is_active,1)=1 AND EXISTS (SELECT 1 FROM recipe_ingredients ri "
                "WHERE ri.menu_item_id=m.id) ORDER BY m.id DESC LIMIT 1", (restaurant_id, x["item"])).fetchone()
            if not one:
                continue
            rows = conn.execute(
                "SELECT i.name, i.unit, ri.qty_per_unit FROM recipe_ingredients ri JOIN ingredients i "
                "ON i.id=ri.ingredient_id AND i.restaurant_id=? WHERE ri.menu_item_id=? AND COALESCE(i.is_active,1)=1",
                (restaurant_id, one["id"])).fetchall()
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


def send_plan(e, tz=None):
    """{"text_at" (ISO, the restaurant's clock), "text_words", "email_by"
    (ISO date), "email_words", "basis"} for a dated game with a kickoff, else
    None. `tz` is the restaurant's clock (a Restaurant or an IANA name):
    the kickoff is moved onto it first (engine.local_kickoff, re-audit
    P2-04) — a 12:00 Central game is 1pm in South Bend, and the texting
    window is the guests' own clock. Without it, the catalog's."""
    try:
        import guest_marketing as gm
        lk = engine.local_kickoff(e, tz)
        if not lk or not lk[1]:
            return None
        day = lk[0]
        kick = datetime.combine(day, datetime.strptime(lk[1], "%H:%M").time())
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


def guest_text_visible(restaurant, denied=None, user=None) -> bool:
    """Who may read WHEN to text and email guests about a game — the ONE
    rule for the brief's game alert, the big-game push and Ask's read_events
    (re-audit 2 R3-03, R3-04, RX-04): the restaurant has the Marketing
    module on AND the reader may view Marketing. `restaurant` is the
    location (or Ask's viewer_restaurant, whose module flags already follow
    the login); `denied` a viewer's denied module keys (the brief's and
    Ask's: "marketing"); `user` a login dict (the push's recipient). With
    neither, the module flag alone (an internal caller)."""
    if restaurant is None or not getattr(restaurant, "module_marketing", 0):
        return False
    if denied is not None and "marketing" in set(denied):
        return False
    if user is not None and not user.get("is_admin"):
        from permissions import MARKETING_VIEW, has_permission
        return bool(has_permission(user, MARKETING_VIEW))
    return True


def plan_ahead(restaurant_id, e, tz=None):
    """send_plan with nothing already gone at the restaurant (re-audit 2
    R3-02, R2-06): None once the text time has passed there (an early
    kickoff's "the evening before" is gone on game day — playbook._past, the
    test the brief and the push used on their own), and the email half
    (`email_by`, `email_words` None) once its day is over. The one reader of
    a send time for every surface."""
    plan = send_plan(e, tz=tz)
    if not plan or playbook._past(restaurant_id, plan["text_at"]):
        return None
    if playbook._past(restaurant_id, f"{plan['email_by']}T23:59"):
        plan = dict(plan, email_by=None, email_words=None)
    return plan


def campaign_goal(restaurant_id, e, mix=None, db_path=store.DB_PATH) -> str:
    """"Bring guests in to watch Bears vs New York Jets on Sunday 10/4/26
    (12pm, FOX) — feature Wings and Salt Caramel Tini, what game nights sell
    here". Never a price, a discount or an offer: the owner adds those.

    The date is M/D/YY and the day and start are the restaurant's clock
    (re-audit P3-11, X-9, P2-04). "What game nights sell here" is said only
    of the items a plan may rest on (_planned); one clean game's items are
    "what your last game sold"; anything else names no item (P3-12)."""
    from time_utils import mdy
    tz = engine.restaurant_clock(restaurant_id, db_path=db_path) if restaurant_id else None
    short = e.get("short_name") or e.get("series_name") or ""
    who = (f"{short} {'vs' if e.get('home_away') == 'home' else 'at'} {e['opponent']}" if e.get("opponent")
           else engine.describe(e, with_date=False, tz=tz))
    lk = engine.local_kickoff(e, tz)
    when = f" on {lk[0].strftime('%A')} {mdy(lk[0])}" if lk else ""
    extras = [x for x in (engine._clock(lk[1] if lk else e.get("kickoff_local")), e.get("broadcast")) if x]
    goal = f"Bring guests in to watch {who}{when}" + (f" ({', '.join(extras)})" if extras else "")
    try:
        mix = mix if mix is not None else item_mix(restaurant_id, e, db_path=db_path)
    except Exception:
        mix = None
    planned = [x["item"] for x in _planned(mix)][:2]
    if planned:
        goal += f" — feature {' and '.join(planned)}, what game nights sell here"
    elif mix and mix.get("n") == 1 and not mix.get("confounded") and mix.get("items"):
        goal += f" — feature {' and '.join(x['item'] for x in mix['items'][:2])}, what your last game sold"
    return goal[:200]


# ── the season's money ──────────────────────────────────────────────────────

def season_value(restaurant_id, series_id, today=None, season=None, db_path=store.DB_PATH):
    """{"played", "measured", "incremental", "waiting", "games": [{"describe",
    "net", "usual", "extra"}], "text", "basis"} for the series' games played
    before `today` (the restaurant's own date by default; this season when
    given), or None with none played.

    "Played" is store.played — never a cancelled or postponed game, nor an
    if-necessary game with no result yet (re-audit P3-05, P4-04): those are
    counted apart as `waiting`, neither played nor unmeasured."""
    try:
        # Never a game this restaurant removed from its list (store.dismissed,
        # re-audit 2 R3-05): the rule every other reader of games applies —
        # Ask's `recent` beside it in the same answer left it out.
        skip = store.dismissed(restaurant_id, db_path=db_path)
        rows = [x for x in store.events_for([series_id], None, None, db_path=db_path) if x["id"] not in skip]
        tz = engine.restaurant_clock(restaurant_id, db_path=db_path)
        today = _d(today) if today else store.local_today(tz)
        played = [x for x in rows if store.played(x, today=today)]
        waiting = [x for x in rows if store.unresolved(x, today=today)]
        # This season unless one is named (audit 10/1/26: "so far" summed
        # every season in the catalog once a second one was loaded).
        if season is None and played:
            season = played[-1].get("season")
        if season is not None:
            played = [x for x in played if x.get("season") == season]
            waiting = [x for x in waiting if x.get("season") == season]
        if not played:
            return None
        n_wait = len({x["event_date"] for x in waiting})
        word = played[0].get("short_name") or played[0].get("series_name") or "Event"
        outs = engine._outcomes(restaurant_id, [x["event_date"] for x in played], db_path, series_word=word)
        games, mixed, seen = [], 0, set()
        for x in played:
            o = outs.get(x["event_date"])
            if x["event_date"] in seen:
                continue                      # a doubleheader is one night
            seen.add(x["event_date"])
            if o and o.get("net") is not None and o.get("baseline") is not None:
                if store.confounded(o):
                    mixed += 1                # Christmas, a party: not the game's money alone
                    continue
                games.append({"describe": engine.describe(x, tz=tz), "date": x["event_date"], "net": float(o["net"]),
                              "usual": float(o["baseline"]), "extra": round(float(o["net"]) - float(o["baseline"]), 2)})
        total = round(sum(g["extra"] for g in games), 2)
        unmeasured = len(seen) - len(games) - mixed
        wait = (f"; {n_wait} if-necessary game{'s' if n_wait != 1 else ''} not counted until a result is in"
                if n_wait else "")
        if games:
            text = (f"{word} games so far: {len(seen)} played, {len(games)} measured here — "
                    f"{'+' if total >= 0 else '−'}${abs(total):,.0f} over a usual same weekday"
                    + (f"; {unmeasured} without a usual night to measure against" if unmeasured else "")
                    + (f"; {mixed} left out with something else on that night" if mixed else "") + wait + ".")
        else:
            text = f"{word} games so far: {len(seen)} played, none measured here yet{wait}."
        return {"played": len(seen), "measured": len(games), "incremental": total if games else None,
                "season": season, "mixed": mixed, "waiting": n_wait,
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
        # Two headline games on one date: the one with a measured effect
        # leads and the others are named — never "order for a usual week"
        # beside a measured game that day (re-audit P4-11, as playbook.alert).
        same_day = [x for x in rows if x["event_date"] == rows[0]["event_date"]]
        e = next((x for x in same_day if engine.effect_for(restaurant_id, x, db_path=db_path)), same_day[0])
        others = [x for x in same_day if x["id"] != e["id"]]
        mix = item_mix(restaurant_id, e, db_path=db_path)
        bump = order_bump(restaurant_id, e, mix=mix, db_path=db_path)
        why = unplanned_words(mix, "order")
        if bump:
            text = bump["text"]
        elif why:
            text = mix["text"] + " " + why
        elif mix and mix.get("text") and _planned(mix):
            text = mix["text"] + " No recipe links those items to ingredients yet, so nothing is sized for the order."
        elif mix:
            what = _kind_one(e) if mix["n"] == 1 else f"{mix['n']} {engine.kind_words(e)}"
            text = f"Your last {what} sold no item well above a usual night — order for a usual week."
        else:
            text = "No game like it measured here yet — order for a usual week."
        tz = engine.restaurant_clock(restaurant_id, db_path=db_path)
        if others:
            text += " Also that day: " + "; ".join(engine.describe(o, with_date=False, tz=tz) for o in others) + "."
        return {"event_id": e["id"], "describe": engine.describe(e, tz=tz), "text": text, "order": bump,
                "others": [o["id"] for o in others],
                "items": (mix or {}).get("items") or [], "basis": (bump or mix or {}).get("basis")}
    except Exception as ex:
        log.warning("event_intel.gameday week_note failed rid=%s: %s", restaurant_id, ex)
        return None


# ── the heads-up the afternoon before a big game ───────────────────────────

def big_game(restaurant_id, e, db_path=store.DB_PATH, tz=None):
    """(True, words) when games like this one ran big here, measured; else
    (False, None). Words name the measurement, never a guess; `tz` the
    restaurant's clock for the date said.

    effect_for never crosses engine.game_class (A1), so a preseason game is
    never judged on regular-season nights (re-audit P3-02). With fewer than
    SEGMENT_MIN_N measured, the last game of the same kind (_same_kind:
    game_class and prime time) decides alone — and never a night that had
    something else on too (store.confounded, re-audit P3-06): one holiday
    night is not a reason to push.

    A measured figure that had to count such a night (`confounded`: too few
    clean nights, so a Christmas Eve sits in the median) is not big either
    (re-audit 2 R3-01, item_mix's own rule of planning nothing on a mixed
    set): it falls to the clean-last-game rule above, as if the mixed nights
    were not there."""
    eff = engine.effect_for(restaurant_id, e, db_path=db_path)
    if eff and eff.get("median_lift_pct") is not None and not eff.get("confounded"):
        if float(eff["median_lift_pct"]) >= BIG_LIFT:
            return True, eff["basis"][0].upper() + eff["basis"][1:] + "."
        return False, None
    last = next((g for g in engine.past_games(restaurant_id, e, db_path=db_path) if _same_kind(e, g["event"])),
                None)
    if not last or store.confounded(last["outcome"]):
        return False, None
    lift = last["outcome"].get("lift_pct")
    if lift is not None and float(lift) >= BIG_LAST_LIFT:
        return True, (f"Your last one like it, {engine.describe(last['event'], with_date=True, tz=tz)}, ran "
                      f"{float(lift):+.0f}% against a usual "
                      f"{_d(last['event']['event_date']).strftime('%A')} — one night.")
    return False, None


def _labor_readers(restaurant_id, db_path) -> dict:
    """{login id: login} — every console login here who reads the module the
    push is FOR (push.audience_of: Labor, the bell's rule for its row too,
    re-audit 2 R3-07), brief preference or not: the push has its own
    per-type mute (preferences.push_allowed)."""
    import morning_brief
    import push
    from permissions import MODULE_VIEW_PERMISSIONS, has_permission
    need = MODULE_VIEW_PERMISSIONS[push.audience_of(PUSH_TYPE)]
    return {u["id"]: u for u in morning_brief.recipients(restaurant_id, db_path, include_opted_out=True)
            if has_permission(u, need)}


def _labor_logins(restaurant_id, db_path):
    return set(_labor_readers(restaurant_id, db_path))


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


def ask_for(e, tz=None) -> str:
    """The question a game's push opens Ask on, and its bell row too
    (client_api's notifications, re-audit P3-09): about THIS game, on the
    restaurant's clock — never "tomorrow's game" relative to when it is
    tapped."""
    return f"How should we get ready for {engine.describe(e, tz=tz)}?"


def _push_parts(restaurant, events, db_path=store.DB_PATH):
    """[(event, title, body without the guest text, the guest-text sentence
    or None)] for tomorrow's big games — the guest text apart, because who
    may read it is decided per recipient (guest_text_visible, re-audit 2
    R3-04). The sentence is there only where the restaurant has Marketing
    on, said as the starting rule it is, and never a time already gone
    (plan_ahead: an early kickoff's "the evening before" is often before the
    push, re-audit P3-07)."""
    out = []
    for e in events:
        big, words = big_game(restaurant.id, e, db_path=db_path, tz=restaurant)
        if not big:
            continue
        body = [words]
        st = playbook.staffing(restaurant.id, e, db_path=db_path)
        if st and st.get("recommend"):
            body.append(st["text"].split(". On your last")[0] + ".")
        plan = plan_ahead(restaurant.id, e, tz=restaurant) if guest_text_visible(restaurant) else None
        guest = f"Guest text, as a starting rule: {plan['text_words']}." if plan else None
        title = f"Tomorrow: {engine.describe(e, with_date=False, tz=restaurant)}"
        out.append((e, title[:120], " ".join(body), guest))
    return out


def _body(base, guest=None) -> str:
    return (base + (" " + guest if guest else ""))[:400]


def push_for(restaurant, today, db_path=store.DB_PATH, events=None):
    """[(event, title, body)] for tomorrow's big games at this restaurant
    (every one, not just the first), the body as a login who may read the
    guest text reads it. Pure read. Dates and times are the restaurant's
    clock (engine.local_kickoff, re-audit P2-04)."""
    events = events if events is not None else tomorrows_games(restaurant, today, db_path=db_path)
    return [(e, title, _body(base, guest))
            for e, title, base, guest in _push_parts(restaurant, events, db_path=db_path)]


def run_event_push(db_path=None, restaurants=None) -> dict:
    """The afternoon before a big game, one push per game per restaurant
    (claimed on the game and its date), to the logins who read labor and have
    a phone that takes it. Push only, P3 — it never sounds through a Focus
    mode, and inside the location's quiet hours it arrives silently (the
    `quiet` flag strategy_jobs._reach sets, re-audit P3-10). The briefing
    budget is checked before EACH push: two big games on one day are two
    briefings (re-audit P3-08). Returns the standard slot counts:
    `attempted` a restaurant with an unclaimed game tomorrow, `skipped` one
    with nobody to push to or a game held by its briefing budget."""
    import models
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
            got = _push_parts(r, todo, db_path=db)
            if not got:
                continue
            readers = _labor_readers(r.id, db)
            # A phone that takes it AND a login whose own choices let it
            # through (push off, this type muted, their own quiet hours — the
            # test fire_push applies per device): a push nobody would get
            # writes no budgeted briefing row and is not "sent" (re-audit 2
            # R3-06).
            import preferences
            _pc = {}
            audience = {u for u in sj.deliverable_audience(r.id, set(readers), db)
                        if preferences.push_allowed(u, r.id, PUSH_TYPE, db_path=db, _cache=_pc)}
            import notify
            if not audience:
                skipped += 1
                continue
            # Who may read the guest text: the one rule (guest_text_visible —
            # the restaurant's Marketing module and the login's Marketing
            # view, re-audit 2 R3-04), per recipient.
            with_text = {u for u in audience if guest_text_visible(r, user=readers.get(u))}
            import nav
            for e, title, base, guest in got:
                # Before EACH push: the one before it counted (an event_ahead
                # row is a budgeted briefing). Held, it stays unclaimed.
                if not notify.briefing_allowed(r.id, PUSH_TYPE, db):
                    skipped += 1
                    break
                if not ops.claim_period(_claim_key(r.id), _claim_period(e)):
                    continue
                alert_id = notify.record_notification(r.id, PUSH_TYPE, db_path=db, ref_kind="catalog_event",
                                                      ref_id=e["id"])
                data = {"nav": nav.path("ask", q=ask_for(e, tz=r)), "alert_id": alert_id, "surface": "alert_push",
                        "answerable": False, "event_id": e["id"]}
                try:
                    if models.is_in_quiet_hours(r.id, db_path=db):
                        data["quiet"] = True
                except Exception as qe:
                    log.warning("event_intel.gameday quiet-hours check failed rid=%s: %s", r.id, qe)
                groups = [(with_text, _body(base, guest)), (audience - with_text, _body(base))] if guest \
                    else [(audience, _body(base))]
                queued = [push.fire_push(r.id, PUSH_TYPE, title, body, data=dict(data), db_path=db, user_ids=ids)
                          for ids, body in groups if ids]
                # fire_push returns how many devices it was queued for; 0 on
                # every call is nobody: the row comes back out of the budget.
                if queued and all(q == 0 for q in queued):
                    notify.withdraw_notification(r.id, alert_id, db_path=db)
                    skipped += 1
                    continue
                sent += 1
        except Exception as ex:
            st["failed"] += 1
            ops.capture(ex, job="event_push", context=f"restaurant_id={r.id}")
    return sj._slot_counts(st, sent=sent, skipped=skipped)
