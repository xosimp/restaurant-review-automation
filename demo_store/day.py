"""
demo_store.day — one business day at Halsted & Rye, as RPOWER would hold it.

`week(week_start)` lays out the payroll week (Monday first, RPOWER's ot_dow
1): who works which shift (the published schedule, `roster`) and when each
person actually clocked in and out (`punches`, overtime split past 40 hours
as RPOWER splits it). `business_day(day)` then seats the night's parties
against that floor — bar tabs, tables, takeout, the odd catering order —
and rings every check: sale lines at the night's price level, $0 kitchen
modifiers, doubles and add-ons linked to the drink or plate they modify,
comps and voids with a manager and a reason, discounts as their own
negative lines, card payments with tips, kitchen fire and bump times.

Records carry RPOWER's own field names and conventions (2000-01-01 for "no
value", time_stamp in UTC, ids as strings). Every day is seeded by its
date, so asking for it twice gives the same night.
"""
import math
import random
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from demo_store import calendar as cal
from demo_store import world as w

TZ = ZoneInfo(w.TIMEZONE)
NEVER = "2000-01-01T00:00:00"
BUSINESS_DAY_START_HOUR = 5


def _rng(*key) -> random.Random:
    return random.Random("|".join(str(k) for k in ("hr-day",) + key))


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _utc(dt: datetime) -> str:
    return dt.replace(tzinfo=TZ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _at(day: date, hours: float) -> datetime:
    return datetime.combine(day, datetime.min.time()) + timedelta(minutes=round(hours * 60))


def busy(day: date) -> float:
    """The night's volume against an ordinary $11,000 night."""
    return cal.demand(day)["target"] / 11000.0


# ── the week's floor: shifts, then punches ──────────────────────────────────

def _slots(day: date) -> list:
    """[(job, start h, end h)] the night needs, from its expected volume."""
    if cal.is_closed(day):
        return []
    b = busy(day)
    wk = day.weekday()
    weekend = wk >= 5
    open_h, close_h = cal.HOURS[wk]
    games = bool(cal.demand(day)["window"])
    late = close_h > 24
    out = []

    def add(job, s, e, n=1):
        for _ in range(max(0, int(n))):
            out.append((job, s, e))

    clamp = lambda v, lo, hi: max(lo, min(hi, int(round(v))))
    # Managers: an opener and a closer every day, a second closer on the big nights.
    add("Manager", open_h - 1.0, 17.0)
    add("Manager", 16.0, close_h + 0.75)
    if b > 1.75:
        add("Manager", 18.0, close_h + 0.5)
    add("Server", open_h - 0.25, 15.5, 3 if weekend else (2 if b > 0.85 else 1))
    add("Server", 16.0, 22.0, clamp(b * 3.1, 2, 7))
    add("Server", 17.0, min(close_h, 24) + 0.5, 1 if b > 0.8 else 0)
    add("Bartender", open_h - 0.5, 17.0, 2 if (weekend or (games and b > 0.9)) else 1)
    if b < 0.8:
        add("Bartender", 16.0, close_h + 0.75, 1)
        add("Bartender", 18.0, 23.0, 1)
    else:
        add("Bartender", 16.0, close_h + 0.75, clamp(b * 1.9, 2, 4))
    if late or b > 1.6:
        add("Bartender", 20.0, close_h + 0.75, 1)
    add("Barback", 18.0, close_h + 0.5, (1 if b > 0.9 else 0) + (1 if b > 1.7 else 0))
    if weekend:
        add("Barback", 11.0, 17.0, 1)
    add("Host", 11.0, 15.0, 1 if (weekend or b > 0.9) else 0)
    add("Host", 16.5, 22.0, (1 if b > 0.75 else 0) + (1 if b > 1.3 else 0))
    add("Busser", 17.0, 22.5, clamp(b * 1.3, 1, 3))
    if weekend:
        add("Busser", 10.0, 15.0, 1)
    if wk < 5:
        add("Kitchen Manager", 9.0, 18.0)
    if 1 <= wk <= 5:
        add("Sous Chef", 14.0, 23.0)
    add("Line Cook", open_h - 1.0, 16.5, 3 if weekend else (2 if b > 0.9 else 1))
    add("Line Cook", 16.0, 23.0, clamp(b * 2.0, 1 if (b < 0.8 and 1 <= wk <= 5) else 2, 5))
    if late:
        add("Line Cook", 20.0, close_h + 0.25, 1)
    add("Prep Cook", 8.0, 15.0, 1 + (1 if (b > 1.3 or wk == 5) else 0))
    add("Dishwasher", open_h - 0.5, 16.0, 1)
    add("Dishwasher", 16.0, close_h + 0.5, 1 + (1 if b > 1.4 else 0))
    return out


def _days_off(e, week_start: date) -> set:
    """Each person's usual days off, varied a little week to week."""
    r = _rng("off", e["mid"], week_start.isoformat())
    want = 7 - max(1, min(6, round(e["hours"] / 7.5))) if e["hours"] else 7
    base = _rng("off-base", e["mid"])
    pref = base.sample(range(7), 7)
    off = set(pref[:want])
    if r.random() < 0.25 and want < 7:
        off = set(pref[1:want + 1])
    return off


@lru_cache(maxsize=64)
def week(week_start: date) -> dict:
    """{"roster": [shift], "punches": {iso day: [punch]}} for the Monday-first week."""
    assert week_start.weekday() == 0
    pools = w.by_job()
    hours = {}
    roster = []
    offs = {e["mid"]: _days_off(e, week_start) for e in w.employees() if not e["station"]}
    for i in range(7):
        day = week_start + timedelta(days=i)
        working = set()
        slots = sorted(_slots(day), key=lambda s: (s[1], -(s[2] - s[1])))
        for job, s, e in slots:
            cands = [p for p in pools.get(job, []) if p["mid"] not in working
                     and day.isoformat() >= p["start"]]
            if not cands:
                continue

            def score(p):
                left = p["hours"] - hours.get(p["mid"], 0.0)
                pen = 0.0
                if i in offs.get(p["mid"], set()):
                    pen += 30.0
                if p.get("secondary"):
                    pen += 12.0
                if hours.get(p["mid"], 0.0) + (e - s) > max(40.0, p["hours"] + 1):
                    pen += 120.0               # overtime only when nobody else can take it
                return (-(left - pen), p["mid"])
            pick = sorted(cands, key=score)[0]
            working.add(pick["mid"])
            hours[pick["mid"]] = hours.get(pick["mid"], 0.0) + (e - s)
            roster.append({"date": day.isoformat(), "emp_mid": pick["mid"], "name": pick["name"], "job": job,
                           "start": _at(day, s), "end": _at(day, e), "rate": pick["rate"]})
    punches = {}
    worked = {}
    rng = _rng("punch", week_start.isoformat())
    for sh in sorted(roster, key=lambda x: x["start"]):
        day = date.fromisoformat(sh["date"])
        if w.JOBS[sh["job"]][3] or next(e for e in w.employees() if e["mid"] == sh["emp_mid"])["job"] in (
                "General Manager", "Owner"):
            continue                                   # salaried: no punch
        if rng.random() < 0.012:
            continue                                   # a no-show
        late = rng.random() < 0.06
        t_in = sh["start"] + timedelta(minutes=(rng.uniform(8, 26) if late else rng.gauss(-4, 4)))
        t_out = sh["end"] + timedelta(minutes=rng.gauss(7, 11))
        if t_out <= t_in + timedelta(minutes=60):
            t_out = t_in + timedelta(minutes=90)
        brk = 30 if (sh["job"] in ("Line Cook", "Prep Cook", "Dishwasher", "Sous Chef", "Kitchen Manager")
                     and (t_out - t_in).total_seconds() > 6 * 3600) else 0
        hrs = max(0.0, (t_out - t_in).total_seconds() / 3600.0 - brk / 60.0)
        before = worked.get(sh["emp_mid"], 0.0)
        reg = max(0.0, min(hrs, 40.0 - before))
        ot = hrs - reg
        worked[sh["emp_mid"]] = before + hrs
        rate = float(sh["rate"] or 0)
        edited = rng.random() < 0.03
        n = len(punches.get(sh["date"], [])) + 1
        punches.setdefault(sh["date"], []).append({
            "rid": "76%s%03d%06d" % (day.strftime("%Y%m%d"), int(sh["emp_mid"][-3:]), n),
            "cg": w.CG, "store_mid": w.STORE_MID, "tc": 0,
            "in_dttm": _stamp(t_in), "out_dttm": _stamp(t_out),
            "emp_mid": sh["emp_mid"], "job_mid": w.JOB[sh["job"]], "lcenter_mid": "3",
            "payroll_id": next(e["payroll_id"] for e in w.employees() if e["mid"] == sh["emp_mid"]),
            "job_id": w.JOBS[sh["job"]][0], "break_flags": 0, "break_minutes": brk,
            "reg_hours": round(reg, 4), "ot_hours": round(ot, 4), "dt_hours": 0,
            "reg_rate": rate, "ot_rate": round(rate * 1.5, 2), "dt_rate": round(rate * 2, 2), "min_wage": 16.6,
            "reg_pay": round(reg * rate, 2), "ot_pay": round(ot * rate * 1.5, 2), "dt_pay": 0,
            "spread_pay": 0, "callin_pay": 0, "tips_total": 0, "tip_net": 0, "grats_actual": 0,
            "meal1_minutes": brk, "meal2_minutes": 0, "rest1_minutes": 0, "rest2_minutes": 0, "rest3_minutes": 0,
            "pay_period": "B", "ot_start_date": _stamp(datetime.combine(week_start, datetime.min.time())),
            "editmgr_mid": "1",
            "edit_dttm": _stamp(t_out + timedelta(hours=rng.uniform(9, 14))) if edited else NEVER,
            "edit_what": "OUT" if edited else "",
            "time_stamp": _utc(t_out + timedelta(minutes=2)), "rec_flags": 0,
            "_job": sh["job"], "_name": sh["name"]})
    return {"roster": roster, "punches": punches}


def week_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def roster_for(day: date) -> list:
    return [s for s in week(week_of(day))["roster"] if s["date"] == day.isoformat()]


def punches_for(day: date) -> list:
    """The day's punches, plus the night before's that ran past midnight are
    the day before's — business day by clock-in."""
    return list(week(week_of(day))["punches"].get(day.isoformat(), []))


# ── the night ──────────────────────────────────────────────────────────────

_FOOD_WEIGHTS_DINING = [("Burgers", 30), ("Sandwiches", 22), ("Entrees", 15), ("Tacos", 10), ("Salads", 9),
                        ("Wings", 8), ("Flatbreads", 6)]
_FOOD_WEIGHTS_BAR = [("Wings", 34), ("Burgers", 20), ("Starters", 18), ("Sandwiches", 14), ("Flatbreads", 8),
                     ("Tacos", 6)]
_DRINK_DINING = [("Draft Beer", 33), ("Cocktails", 19), ("Wine by the Glass", 17), ("Bottles & Cans", 8),
                 ("Vodka", 3), ("Whiskey", 3), ("N/A Beverages", 34), ("N/A Beer & Mocktails", 2)]
_DRINK_BAR = [("Draft Beer", 42), ("Cocktails", 13), ("Bottles & Cans", 14), ("Vodka", 7), ("Whiskey", 7),
              ("Tequila", 4), ("Rum & Gin", 2), ("Wine by the Glass", 5), ("N/A Beverages", 6),
              ("N/A Beer & Mocktails", 2)]
_CARDS = [("Visa", 46), ("MasterCard", 22), ("American Express", 12), ("Apple Pay", 7), ("Discover", 4),
          ("Cash", 8), ("Gift Card", 1)]
# What a few POS drinks sell more of than the rest, within their category.
_POPULAR = {"Miller Lite Draft": 3.0, "Old Style Draft": 2.2, "Modelo Draft": 1.6, "Anti-Hero IPA Draft": 1.4,
            "Titos": 3.0, "Well Vodka": 1.6, "Jameson": 2.4, "House Margarita": 2.2, "Spicy Margarita": 1.6,
            "Halsted Old Fashioned": 1.8, "Wrigleyville Lemonade": 1.5, "Coke": 2.5, "Diet Coke": 1.5,
            "Halsted Smash Burger": 2.6, "Italian Beef": 2.0, "Wings 10 Pc": 2.5, "Loaded Tots": 1.6,
            "Brisket Tacos": 1.4, "Nashville Hot Chicken": 1.5, "Bud Light": 1.8, "Michelob Ultra": 1.8,
            "High Noon": 1.6, "Cabernet Glass": 1.4, "Pinot Grigio Glass": 1.3, "Malort Shot": 1.2,
            "Fireball Shot": 1.4, "Green Tea Shot": 1.3}


def _pick(rng, pairs):
    total = sum(x[1] for x in pairs)
    r = rng.random() * total
    for k, wt in pairs:
        r -= wt
        if r <= 0:
            return k
    return pairs[-1][0]


@lru_cache(maxsize=1)
def _by_category() -> dict:
    out = {}
    for m in w.menu():
        out.setdefault(m["category"], []).append(m)
    return out


def _item(rng, category):
    rows = _by_category()[category]
    return _pick(rng, [(m, _POPULAR.get(m["name"], 1.0)) for m in rows])


def _on(punches, jobs, t: datetime) -> list:
    out = []
    for p in punches:
        if p["_job"] in jobs and p["in_dttm"] <= _stamp(t) <= p["out_dttm"]:
            out.append(p)
    return out


def _sample_time(rng, day, weights) -> datetime:
    hours = list(weights.items())
    h = _pick(rng, hours)
    return _at(day, h + rng.random())


class _Ticket:
    __slots__ = ("no", "open", "close", "room", "table", "pcenter", "meal", "server", "guests", "lines", "items",
                 "kind", "need", "hh")

    def __init__(self, **k):
        for s in self.__slots__:
            setattr(self, s, k.get(s))
        self.lines = []
        self.items = []


def _meal(day, t: datetime) -> str:
    h = t.hour + (24 if t.date() > day else 0)
    if day.weekday() >= 5 and h < 15:
        return "Brunch"
    if h < 15:
        return "Lunch"
    if h < 18 and day.weekday() < 5:
        return "Happy Hour"
    if h < 22:
        return "Dinner"
    return "Late Night"


@lru_cache(maxsize=48)
def business_day(day: date) -> dict:
    """{tickets, lines, items, payments, payouts, punches, closed_at} for one business date."""
    d = cal.demand(day)
    punches = [dict(p) for p in punches_for(day)]
    if d["closed"]:
        return {"tickets": [], "lines": [], "items": [], "payments": [], "payouts": [], "punches": punches,
                "closed_at": None}
    rng = _rng("night", day.isoformat())
    weights = cal.hour_weights(day)
    open_h, close_h = cal.HOURS[day.weekday()]
    closing = _at(day, close_h)
    patio = day.month in (5, 6, 7, 8, 9) and "rain" not in d["reasons"]
    gameday = bool(d["window"])
    managers = lambda t: _on(punches, ("Manager",), t) or _on(punches, ("Kitchen Manager", "Sous Chef"), t)
    tickets, total, no = [], 0.0, 100
    target = d["target"]
    stations = [e for e in w.employees() if e["station"]]
    online = next(e for e in stations if e["first"] == "Online")
    bar_station = next(e for e in stations if e["first"] == "Bar")

    def ring(tk, m, qty=1, at=None, price=None, parent=None, kind="sale", reason=None, mgr=None):
        unit = m["price"] if price is None else price
        if tk.hh and m.get("hh_price") is not None and price is None:
            unit = m["hh_price"]
        atom = len(tk.lines) + 1
        st = {"sale": "Sale", "nonsale": "Non Sale", "comp": "Comp", "void": "Error Correct",
              "discount": "Discount"}[kind]
        if kind == "sale" and unit == 0:
            st = "Non Sale"
        sales = round(unit * qty, 2)
        tk.lines.append({"atom": atom, "m": m, "qty": qty, "unit": unit, "sales": sales, "st": st,
                         "at": at or tk.open, "parent": parent, "reason": reason, "mgr": mgr})
        return atom

    def order(tk, guests, room_kind, t):
        """Ring a party's food and drink."""
        late_night = _meal(day, t) == "Late Night"
        brunch = _meal(day, t) == "Brunch"
        food_at = t + timedelta(minutes=rng.uniform(6, 14))
        drinks_at = t + timedelta(minutes=rng.uniform(1, 5))
        if room_kind == "takeout":
            for _ in range(guests):
                m = _item(rng, _pick(rng, _FOOD_WEIGHTS_DINING if not brunch else [("Brunch", 1)]))
                a = ring(tk, m, at=t)
                _mods(tk, m, a, t)
            if rng.random() < 0.3:
                ring(tk, _item(rng, "Sides"), at=t)
            if rng.random() < 0.25:
                ring(tk, _item(rng, "N/A Beverages"), at=t)
            return
        # food
        eat = 0.92 if room_kind == "dining" else (0.25 if late_night else 0.45)
        weights_food = (_FOOD_WEIGHTS_DINING if room_kind == "dining" else _FOOD_WEIGHTS_BAR)
        if gameday and room_kind == "bar":
            weights_food = [("Wings", 50)] + [x for x in weights_food if x[0] != "Wings"]
        if room_kind == "dining" and guests >= 2 and rng.random() < 0.45 + 0.1 * guests:
            ring(tk, _item(rng, "Starters"), at=food_at - timedelta(minutes=4))
        kids = guests >= 4 and rng.random() < 0.3
        for g in range(guests):
            if kids and g >= guests - 1 - (1 if guests >= 5 else 0):
                ring(tk, _item(rng, "Kids"), at=food_at)
                ring(tk, w.item_by_name()["Kids Drink"], at=drinks_at)
                continue
            if rng.random() < eat:
                cat = "Brunch" if (brunch and rng.random() < 0.8) else _pick(rng, weights_food)
                m = _item(rng, cat)
                a = ring(tk, m, at=food_at)
                _mods(tk, m, a, food_at)
            if rng.random() < 0.10:
                ring(tk, _item(rng, "Sides"), at=food_at)
        # drinks: a first round, then more the longer they stay
        rounds = 1 + (rng.random() < 0.55) + (rng.random() < 0.25)
        if room_kind == "bar":
            rounds = max(1, int(rng.gauss(2.6 + (0.7 if gameday else 0), 1.0)))
        if brunch:
            rounds = max(1, rounds - 1)
        when = drinks_at
        for r in range(rounds):
            if room_kind == "bar" and guests >= 3 and gameday and r == 0 and rng.random() < 0.35:
                ring(tk, _item(rng, "Beer Towers"), at=when)
                when += timedelta(minutes=rng.uniform(25, 45))
                continue
            if room_kind == "bar" and guests >= 2 and r == 0 and rng.random() < 0.08:
                ring(tk, _item(rng, "Buckets"), at=when)
                when += timedelta(minutes=rng.uniform(25, 45))
                continue
            if r > 0 and rng.random() < (0.22 if late_night else 0.08):
                ring(tk, _item(rng, "Shots"), qty=guests, at=when)
            if room_kind == "dining" and guests >= 4 and r == 0 and rng.random() < 0.08:
                ring(tk, _item(rng, "Wine Bottles"), at=when)
                when += timedelta(minutes=rng.uniform(20, 40))
                continue
            for _g in range(guests):
                if r > 0 and rng.random() < 0.35:
                    continue
                if brunch and rng.random() < 0.45:
                    m = w.item_by_name()["Mimosa" if rng.random() < 0.6 else "Bloody Mary"]
                else:
                    m = _item(rng, _pick(rng, _DRINK_BAR if room_kind == "bar" else _DRINK_DINING))
                a = ring(tk, m, at=when)
                if m["category"] in ("Vodka", "Whiskey", "Tequila", "Rum & Gin"):
                    if rng.random() < 0.12:
                        ring(tk, w.item_by_name()["Make It a Double"], at=when, parent=a)
                    ring(tk, w.item_by_name()[_pick(rng, [("Rocks", 4), ("Soda", 5), ("Neat", 1)])], at=when,
                         parent=a)
            when += timedelta(minutes=rng.uniform(25, 45))
        if room_kind == "dining" and rng.random() < 0.12 * guests:
            ring(tk, _item(rng, "Desserts"), at=food_at + timedelta(minutes=rng.uniform(30, 45)))

    def _mods(tk, m, atom, t):
        if m["category"] == "Burgers":
            ring(tk, w.item_by_name()[_pick(rng, [("Medium Rare", 3), ("Medium", 5), ("Medium Well", 3),
                                                  ("Well Done", 1)])], at=t, parent=atom)
        if m["category"] == "Wings":
            ring(tk, w.item_by_name()[_pick(rng, [("Hot", 5), ("BBQ", 3), ("Garlic Parm", 2), ("Dry Rub", 1)])],
                 at=t, parent=atom)
        if m["category"] in ("Burgers", "Sandwiches", "Salads") and rng.random() < 0.18:
            add = _pick(rng, [("Add Bacon", 4), ("Add Avocado", 2), ("Sub Tots", 3), ("Sub Onion Rings", 2),
                              ("Extra Patty", 1), ("Add Chicken", 1), ("Extra Ranch", 2)])
            ring(tk, w.item_by_name()[add], at=t, parent=atom)
        if rng.random() < 0.07:
            ring(tk, w.item_by_name()[_pick(rng, [("No Onion", 3), ("No Tomato", 2), ("Sauce on Side", 3),
                                                  ("Extra Crispy", 1), ("Gluten Free Bun", 1),
                                                  ("Allergy - Nuts", 1)])], at=t, parent=atom)

    # Parties, until the night's sales are rung.
    while total < target:
        no += 1
        t = _sample_time(rng, day, weights)
        if t >= closing - timedelta(minutes=10):
            continue
        h = t.hour + (24 if t.date() > day else 0)
        bar_share = 0.45
        if h >= 22:
            bar_share = 0.80
        elif day.weekday() < 5 and 15 <= h < 18:
            bar_share = 0.60
        if h in d["window"]:
            bar_share = max(bar_share, 0.68)
        takeout = {True: 0.12, False: 0.06}[h < 15] if h < 22 else 0.02
        roll = rng.random()
        if roll < takeout:
            kind, room = "takeout", "Takeout"
            guests = 1 + (rng.random() < 0.45) + (rng.random() < 0.2)
        elif roll < takeout + bar_share * (1 - takeout):
            kind = "bar"
            room = "Main Bar" if rng.random() < 0.78 else "Bar Rail"
            guests = _pick(rng, [(1, 42), (2, 34), (3, 13), (4, 8), (5, 3)])
        else:
            kind = "dining"
            room = "Patio" if (patio and rng.random() < 0.32) else ("Back Room" if rng.random() < 0.28 else
                                                                     "Dining Room")
            guests = _pick(rng, [(1, 4), (2, 40), (3, 15), (4, 24), (5, 9), (6, 6), (8, 2)])
        hh = kind == "bar" and day.weekday() < 5 and 15 <= h < 18
        tk = _Ticket(no=no, open=t, room=room, guests=guests, kind=kind, hh=hh, meal=_meal(day, t),
                     pcenter=w.ROOMS[room][2], table=rng.choice(w.ROOMS[room][1]))
        if kind == "takeout":
            tk.server = online["mid"] if rng.random() < 0.6 else None
        elif kind == "bar":
            pool = _on(punches, ("Bartender",), t)
            tk.server = rng.choice(pool)["emp_mid"] if pool else bar_station["mid"]
        else:
            pool = _on(punches, ("Server",), t)
            tk.server = rng.choice(pool)["emp_mid"] if pool else None
        if tk.server is None:
            pool = _on(punches, ("Server", "Bartender", "Host"), t)
            tk.server = rng.choice(pool)["emp_mid"] if pool else online["mid"]
        order(tk, guests, kind, t)
        stay = {"takeout": (14, 26), "bar": (40, 150), "dining": (50, 95)}[kind]
        last = max(l["at"] for l in tk.lines) if tk.lines else t
        tk.close = min(max(last + timedelta(minutes=rng.uniform(8, 20)), t + timedelta(minutes=rng.uniform(*stay))),
                       closing + timedelta(minutes=25))
        total += sum(l["sales"] for l in tk.lines if l["st"] == "Sale")
        tickets.append(tk)

    # Catering and game-day party packs, ordered ahead (need_dttm).
    cat_n = (1 if rng.random() < 0.28 else 0) + (1 if (gameday and rng.random() < 0.6) else 0)
    for _ in range(cat_n):
        no += 1
        need = _at(day, rng.choice([11.5, 12.0, 17.0, 17.5]))
        tk = _Ticket(no=no, open=need - timedelta(hours=rng.uniform(20, 70)), room="Takeout", guests=1,
                     kind="catering", hh=False, meal=_meal(day, need), pcenter="Catering", table="To Go",
                     server=online["mid"], need=need)
        ring(tk, w.item_by_name()["Game Day Wing Party Pack" if gameday or rng.random() < 0.5 else "Taco Bar for 20"],
             qty=1 + (rng.random() < 0.3), at=need)
        tk.close = need + timedelta(minutes=10)
        tickets.append(tk)

    # Staff meals, half off, after the lunch rush and before the dinner shift.
    for p in punches:
        if p["_job"] in ("Manager",) or rng.random() > 0.22:
            continue
        no += 1
        t = min(max(datetime.fromisoformat(p["in_dttm"]) + timedelta(hours=rng.uniform(2, 4)), _at(day, open_h)),
                closing - timedelta(minutes=30))
        tk = _Ticket(no=no, open=t, room="Dining Room", guests=1, kind="staff", hh=False, meal=_meal(day, t),
                     pcenter="Dining", table="10", server=p["emp_mid"])
        m = _item(rng, _pick(rng, _FOOD_WEIGHTS_BAR))
        ring(tk, m, at=t)
        mgr = managers(t)
        ring(tk, w.item_by_name()["50% Staff Meal"], price=-round(m["price"] * 0.5, 2), kind="discount", at=t,
             mgr=(mgr[0]["emp_mid"] if mgr else None))
        tk.close = t + timedelta(minutes=rng.uniform(15, 30))
        tickets.append(tk)

    # Losses: comps, voids, check discounts.
    for tk in tickets:
        if tk.kind in ("staff", "catering"):
            continue
        mgr = managers(tk.open)
        mgr_id = mgr[0]["emp_mid"] if mgr else None
        heavy = tk.server == next((e["mid"] for e in w.employees() if e["name"] == w.COMP_HEAVY), None)
        sale_lines = [l for l in tk.lines if l["st"] == "Sale" and l["parent"] is None]
        if sale_lines and rng.random() < (0.105 if heavy else 0.034):
            l = rng.choice(sale_lines)
            food = l["m"]["kind"] == "food"
            reason = (_pick(rng, [("Kitchen Error", 35), ("Guest Unhappy", 25), ("Long Ticket Time", 15),
                                  ("Manager Comp", 15), ("Object in Food", 3), ("Walk Out", 3)]) if food else
                      _pick(rng, [("Manager Comp", 70), ("Guest Unhappy", 20), ("Walk Out", 4)]))
            l.update(st="Comp", reason=reason, mgr=mgr_id)
        if sale_lines and rng.random() < 0.021:
            l = rng.choice(sale_lines)
            if l["st"] == "Sale":
                reason = _pick(rng, [("Rang In Wrong", 50), ("Guest Changed Mind", 30), ("Wrong Check", 15),
                                     ("Manager Test", 5)])
                l.update(st="Error Correct", reason=reason, mgr=mgr_id)
        h = tk.open.hour + (24 if tk.open.date() > day else 0)
        if tk.kind == "bar" and h >= 22 and rng.random() < 0.06:
            net = sum(l["sales"] for l in tk.lines if l["st"] == "Sale")
            ring(tk, w.item_by_name()["Industry 20%"], price=-round(net * 0.2, 2), kind="discount", mgr=mgr_id)
        elif rng.random() < 0.006:
            net = sum(l["sales"] for l in tk.lines if l["st"] == "Sale")
            ring(tk, w.item_by_name()["First Responder 15%"], price=-round(net * 0.15, 2), kind="discount",
                 mgr=mgr_id)
        elif rng.random() < 0.004:
            net = sum(l["sales"] for l in tk.lines if l["st"] == "Sale")
            ring(tk, w.item_by_name()["Manager 100% Comp"], price=-round(net, 2), kind="discount", mgr=mgr_id)
        if tk.kind == "dining" and rng.random() < 0.012:
            desserts = [l for l in tk.lines if l["m"]["category"] == "Desserts" and l["st"] == "Sale"]
            if desserts:
                ring(tk, w.item_by_name()["Birthday Dessert"], price=-desserts[0]["unit"], kind="discount",
                     mgr=mgr_id)

    return _render(day, tickets, punches, rng)


def _render(day, tickets, punches, rng) -> dict:
    """RPOWER's records for the night."""
    ymd = day.strftime("%Y%m%d")
    iso = f"{day.isoformat()}T00:00:00"
    out_t, out_l, out_i, out_p = [], [], [], []
    tips_by = {}
    food_by_half_hour = {}
    for tk in tickets:
        if any(l["m"]["kind"] == "food" for l in tk.lines):
            k = (tk.open.hour, tk.open.minute // 30)
            food_by_half_hour[k] = food_by_half_hour.get(k, 0) + 1
    methods = {name: w.mid("paymeth", i + 1) for i, (name, *_r) in enumerate(w.PAYMENT_METHODS)}
    for tk in tickets:
        rid = "77%s%05d%04d" % (ymd, tk.no, 0)
        sale = sum(l["sales"] for l in tk.lines if l["st"] == "Sale")
        disc = sum(l["sales"] for l in tk.lines if l["st"] == "Discount")
        comp = sum(l["sales"] for l in tk.lines if l["st"] == "Comp")
        net = round(sale + disc, 2)
        tax = round(max(0.0, net) * w.TAX_RATE, 2)
        food = [l for l in tk.lines if l["m"]["kind"] == "food" and l["st"] in ("Sale", "Comp")]
        kvs = bump = NEVER
        if food and tk.kind != "catering":
            fire = min(l["at"] for l in food)
            load = food_by_half_hour.get((tk.open.hour, tk.open.minute // 30), 1)
            mins = max(6.0, min(38.0, 8.5 + 0.55 * load + rng.gauss(0, 2.5)))
            kvs, bump = _stamp(fire), _stamp(fire + timedelta(minutes=mins))
        card = _pick(rng, _CARDS) if net > 0 else "Cash"
        tip = 0.0
        if tk.kind in ("bar", "dining") and net > 0 and card != "Cash":
            pct = rng.gauss(0.205 if tk.kind == "dining" else 0.22, 0.04)
            tip = round(max(0.0, net * pct), 2)
        if tk.server:
            tips_by[tk.server] = tips_by.get(tk.server, 0.0) + tip
        closed = tk.close
        table_n = list(w.ROOMS).index(tk.room) * 100 + w.ROOMS[tk.room][1].index(tk.table) + 1
        out_t.append({
            "rid": rid, "cg": w.CG, "store_mid": w.STORE_MID, "date": iso, "shift": "A", "ticket": tk.no,
            "pcenter_mid": w.PCENTER[tk.pcenter], "mealtime_mid": w.MEAL[tk.meal], "mgr_mid": "1",
            "cashier_mid": tk.server or "1", "cust_mid": "1", "slstype_mid": "1",
            "open_bdate": iso, "open_bshift": "A", "open_dttm": _stamp(tk.open), "close_dttm": _stamp(closed),
            "need_dttm": _stamp(tk.need) if tk.need else NEVER, "need_dttm_sel": 1 if tk.need else 0,
            "sale_dttm": _stamp(tk.open), "kvs_dttm": kvs, "bump_dttm": bump,
            "main_server": tk.server or "1", "first_server": tk.server or "1", "last_server": tk.server or "1",
            "table_mid": w.mid("table", table_n), "is_cancelled": 0, "guest_count": tk.guests,
            "entree_count": sum(1 for l in tk.lines if l["m"]["kind"] == "food" and not l["m"]["is_mod"]),
            "bev_count": sum(int(l["qty"]) for l in tk.lines if l["m"]["kind"] == "drink" and not l["m"]["is_mod"]),
            "items": round(sale, 2), "discount": round(disc, 2), "tip": tip, "grat": 0, "tax": tax,
            "type_sale": round(sale, 2), "type_discnt": round(disc, 2), "type_promo": 0, "type_comp": round(comp, 2),
            "type_error": round(sum(l["sales"] for l in tk.lines if l["st"] == "Error Correct"), 2),
            "time_stamp": _utc(closed + timedelta(minutes=1)), "rec_flags": 0,
            "_open": tk.open, "_close": closed, "_room": tk.room, "_kind": tk.kind})
        for l in tk.lines:
            m = l["m"]
            lrid = "78%s%05d%04d" % (ymd, tk.no, l["atom"])
            atom = int("%05d%04d" % (tk.no, l["atom"]))
            parent_atom = int("%05d%04d" % (tk.no, l["parent"])) if l["parent"] else 0
            out_l.append({
                "rid": lrid, "ticket_rid": rid, "cg": w.CG, "store_mid": w.STORE_MID, "date": iso, "shift": "A",
                "ticket": tk.no, "atom": atom, "pcenter_mid": w.PCENTER[tk.pcenter],
                "mealtime_mid": w.MEAL[tk.meal], "mgr_mid": (l["mgr"] or "1") if l["st"] in ("Comp", "Discount") else "1",
                "cust_mid": "1", "menuitem_mid": m["mid"],
                "voidrsn_mid": w.REASON.get(l["reason"], "1") if l["reason"] else "1",
                "voidmgr_mid": (l["mgr"] or "1") if l["st"] == "Error Correct" else "1",
                "slstype_mid": w.ST[l["st"]], "prclvl_mid": "2" if (tk.hh and m.get("hh_price") is not None) else "1",
                "taxcat_mid": "1", "item_dttm": _stamp(l["at"]), "qty": l["qty"], "fraction": 0,
                "price": l["unit"] if l["st"] in ("Sale", "Non Sale") else 0,
                "sales": l["sales"], "cost": 0, "discounted": 0, "voided": 0,
                "regular_price": l["unit"] if l["st"] in ("Sale", "Non Sale") else 0, "par_price": 0,
                "aqu_count": 0, "time_stamp": _utc(l["at"] + timedelta(minutes=2)), "rec_flags": 0,
                "_at": l["at"]})
            out_i.append({"cg": w.CG, "date": iso, "shift": "A", "store_mid": w.STORE_MID, "ticket": tk.no,
                          "atom": atom, "menuitem_mid": m["mid"], "item_dttm": _stamp(l["at"]), "qty": l["qty"],
                          "parent_atom": parent_atom, "mod_level": 1 if l["parent"] else 0,
                          "rid": "75%s%05d%04d" % (ymd, tk.no, l["atom"]), "ticket_rid": rid, "_at": l["at"]})
        if net > 0:
            parts = 2 if (tk.guests >= 4 and rng.random() < 0.25) else 1
            for n in range(parts):
                share = round((net + tax) / parts, 2)
                out_p.append({"rid": "79%s%05d%04d" % (ymd, tk.no, n + 1), "ticket_rid": rid, "cg": w.CG,
                              "store_mid": w.STORE_MID, "date": iso, "shift": "A", "ticket": tk.no, "number": n + 1,
                              "paymeth_mid": methods[card], "cashier_mid": tk.server or "1",
                              "target_amt": share, "paid_ticket": share, "paid_tip": round(tip / parts, 2),
                              "tip_fee": round(tip / parts * 0.025, 2) if card != "Cash" else 0, "tip": round(tip / parts, 2),
                              "tax": round(tax / parts, 2), "time_stamp": _utc(closed), "rec_flags": 0,
                              "_at": closed})
    for p in punches:
        if p["_job"] in ("Server", "Bartender"):
            t = round(tips_by.get(p["emp_mid"], 0.0), 2)
            p["tips_total"] = t
            p["tip_net"] = round(t * 0.975, 2)
    payouts = []
    prng = _rng("payout", day.isoformat())
    if prng.random() < 0.45:
        cat = _pick(prng, [("Cash Short/Over", 4), ("Paid Out - Supplies", 3), ("Paid Out - DJ", 1),
                           ("Paid Out - Repairs", 1)])
        amt = {"Cash Short/Over": round(prng.uniform(-12, 15), 2), "Paid Out - Supplies": round(prng.uniform(18, 85), 2),
               "Paid Out - DJ": 250.0, "Paid Out - Repairs": round(prng.uniform(60, 240), 2)}[cat]
        at = _at(day, 22.5)
        payouts.append({"rid": "74%s%04d" % (ymd, 1), "cg": w.CG, "date": iso, "store_mid": w.STORE_MID, "shift": "A",
                        "pocat_mid": w.mid("pocat", w.PAYOUT_CATEGORIES.index(cat) + 1), "paymeth_mid": methods["Cash"],
                        "emp_mid": "1", "mgr_mid": next((p["emp_mid"] for p in punches if p["_job"] == "Manager"), "1"),
                        "dttm": _stamp(at), "base": abs(amt), "adjust": 0, "net": amt, "is_payin": 0,
                        "reference": cat.replace("Paid Out - ", ""), "time_stamp": _utc(at), "rec_flags": 0, "_at": at})
    closed_at = _at(day, cal.HOURS[day.weekday()][1] + 0.9)
    return {"tickets": out_t, "lines": out_l, "items": out_i, "payments": out_p, "payouts": payouts,
            "punches": punches, "closed_at": closed_at}
