"""
demo_store.calendar — how busy Halsted & Rye is on a date, and when.

A weekday's base, the Chicago season, the store's own growth, its holidays
(Halsted Street carries the Pride Parade and Northalsted Market Days), and
Chicago's games from the same season files Event Intelligence reads
(event_intel/seasons), so a Bears Sunday in the demo is a Bears Sunday in
the catalog and the event learners measure a real lift. Before the 2026
files begin, the 2025 football and baseball calendars are approximated.
Seeded by the date: the same day is the same day every time.
"""
import glob
import json
import math
import os
import random
from datetime import date, timedelta
from functools import lru_cache

# Net sales on an ordinary week of an average month (season 1.0).
BASE_BY_WEEKDAY = {0: 6400, 1: 6800, 2: 8000, 3: 9400, 4: 13300, 5: 15100, 6: 10900}
SEASON = {1: 0.80, 2: 0.84, 3: 0.95, 4: 0.98, 5: 1.06, 6: 1.14, 7: 1.12, 8: 1.08, 9: 1.03, 10: 1.00,
          11: 0.93, 12: 0.98}
GROWTH_PER_YEAR = 0.05
GROWTH_ANCHOR = date(2026, 1, 1)
NOISE_SIGMA = 0.07

# Open hours (local, 24h; past 24 is after midnight on the same business day).
HOURS = {0: (11, 24), 1: (11, 24), 2: (11, 24), 3: (11, 24), 4: (11, 26), 5: (10, 26), 6: (10, 23)}

_SEASONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "event_intel", "seasons")

# series slug fragment -> (home lift, away lift, window before start (h), window after start (h))
_SERIES = {
    "nfl-chicago-bears": (0.62, 0.55, 1.5, 4.0),
    "mlb-chicago-cubs": (0.38, 0.07, 2.5, 4.5),
    "nba-chicago-bulls": (0.05, 0.03, 0.5, 2.5),
    "nhl-chicago-blackhawks": (0.04, 0.03, 0.5, 2.5),
    "mlb-chicago-white-sox": (0.02, 0.01, 0.5, 3.0),
    "mls-chicago-fire": (0.06, 0.04, 0.5, 2.5),
}


def _rng(*key) -> random.Random:
    return random.Random("|".join(str(k) for k in ("halsted-rye",) + key))


@lru_cache(maxsize=1)
def _catalog() -> dict:
    """{iso date: [(series, home_away, kickoff hour float, season_type)]} from the bundled season files."""
    out = {}
    for path in glob.glob(os.path.join(_SEASONS_DIR, "*.json")):
        slug = os.path.basename(path)
        series = next((s for s in _SERIES if slug.startswith(s)), None)
        if not series:
            continue
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        for e in data.get("events") or []:
            if not e.get("date") or e.get("status") in ("postponed", "cancelled"):
                continue
            k = str(e.get("kickoff") or "19:00")
            try:
                h, m = k.split(":")[:2]
                start = int(h) + int(m) / 60.0
            except Exception:
                start = 19.0
            out.setdefault(e["date"], []).append((series, e.get("home_away") or "home", start,
                                                  e.get("season_type") or "regular"))
    return out


def _approx_2025(day: date) -> list:
    """Bears Sundays (9/7/25-1/4/26, one bye) and Cubs homestands (4/1-9/28/25) before the 2026 files."""
    out = []
    if date(2025, 9, 7) <= day <= date(2026, 1, 4) and day.weekday() == 6 and day != date(2025, 10, 19):
        start = 12.0 if day.toordinal() % 3 else 15.42
        out.append(("nfl-chicago-bears", "home" if day.toordinal() % 2 else "away", start, "regular"))
    if date(2025, 9, 1) <= day <= date(2025, 9, 28):
        if (day.toordinal() // 6) % 2 == 0 and day.weekday() != 0:
            out.append(("mlb-chicago-cubs", "home", 13.33 if day.weekday() >= 5 else 18.67, "regular"))
    return out


def events(day: date) -> list:
    if day < date(2026, 2, 1):
        return _approx_2025(day)
    return list(_catalog().get(day.isoformat(), []))


# (lift, label) — the store's own big days.
def _holiday(day: date):
    y = day.year
    thanksgiving = date(y, 11, 1) + timedelta(days=(3 - date(y, 11, 1).weekday()) % 7 + 21)
    memorial = date(y, 5, 31) - timedelta(days=date(y, 5, 31).weekday())
    labor = date(y, 9, 1) + timedelta(days=(0 - date(y, 9, 1).weekday()) % 7)
    pride = date(y, 6, 30) - timedelta(days=(date(y, 6, 30).weekday() + 1) % 7)       # last Sunday of June
    market_sat = date(y, 8, 1) + timedelta(days=(5 - date(y, 8, 1).weekday()) % 7 + 7)  # second weekend of August
    super_bowl = date(y, 2, 1) + timedelta(days=(6 - date(y, 2, 1).weekday()) % 7 + 7)
    parade = date(y, 3, 17) - timedelta(days=(date(y, 3, 17).weekday() - 5) % 7)       # Saturday before St. Patrick's
    table = {
        date(y, 12, 25): (-1.0, "Christmas Day (closed)"),
        date(y, 12, 24): (-0.45, "Christmas Eve"),
        date(y, 12, 31): (0.42, "New Year's Eve"),
        date(y, 1, 1): (-0.25, "New Year's Day"),
        thanksgiving: (-0.62, "Thanksgiving"),
        thanksgiving - timedelta(days=1): (0.48, "Thanksgiving Eve"),
        date(y, 10, 31): (0.36, "Halloween"),
        date(y, 3, 17): (0.95, "St. Patrick's Day"),
        parade: (0.80, "St. Patrick's Parade"),
        super_bowl: (0.70, "Super Bowl Sunday"),
        pride: (1.15, "Pride Parade on Halsted"),
        market_sat: (0.62, "Northalsted Market Days"),
        market_sat + timedelta(days=1): (0.55, "Northalsted Market Days"),
        date(y, 7, 4): (-0.22, "Fourth of July"),
        memorial: (0.12, "Memorial Day"),
        labor: (0.08, "Labor Day"),
        date(y, 5, 5): (0.30, "Cinco de Mayo"),
    }
    return table.get(day)


def is_closed(day: date) -> bool:
    h = _holiday(day)
    return bool(h and h[0] <= -1.0)


@lru_cache(maxsize=1024)
def demand(day: date) -> dict:
    """{target, closed, reasons, events, window: {hour: lift}} — the night's net-sales target
    and the extra pull each hour gets from its games."""
    if is_closed(day):
        return {"target": 0.0, "closed": True, "reasons": [_holiday(day)[1]], "events": [], "window": {}}
    rng = _rng("demand", day.isoformat())
    base = BASE_BY_WEEKDAY[day.weekday()] * SEASON[day.month]
    growth = 1.0 + GROWTH_PER_YEAR * ((day - GROWTH_ANCHOR).days / 365.0)
    lift, reasons, window = 0.0, [], {}
    hol = _holiday(day)
    if hol:
        lift += hol[0]
        reasons.append(hol[1])
    evs = events(day)
    for series, ha, start, stype in evs:
        home, away, before, after = _SERIES[series]
        l = home if ha == "home" else away
        if stype == "postseason":
            l *= 1.6
        elif stype == "preseason":
            l *= 0.4
        # A game the bar is closed for barely counts; a noon Bears game on a weekday is rare.
        open_h, close_h = HOURS[day.weekday()]
        if start + after < open_h or start - before > close_h:
            continue
        lift += l
        reasons.append(series)
        for h in range(int(math.floor(start - before)), int(math.ceil(start + after))):
            window[h] = window.get(h, 0.0) + l * 2.4
    # Rain or a cold snap: a few nights a month, the patio's season hit hardest.
    if rng.random() < 0.10:
        lift -= 0.10 if day.month in (5, 6, 7, 8, 9) else 0.06
        reasons.append("rain")
    noise = math.exp(rng.gauss(0, NOISE_SIGMA))
    target = max(1500.0, base * growth * (1.0 + lift) * noise)
    return {"target": round(target, 2), "closed": False, "reasons": reasons, "events": evs, "window": window}


def hour_weights(day: date) -> dict:
    """{hour: weight} over the store's open hours: lunch, the happy-hour lift, dinner, the late bar,
    weekend brunch, and each game's window."""
    open_h, close_h = HOURS[day.weekday()]
    wk = day.weekday()
    weekend = wk >= 5
    base = {}
    for h in range(open_h, close_h):
        hh = h % 24
        if weekend and hh < 15:
            w = {10: 0.9, 11: 1.4, 12: 1.6, 13: 1.4, 14: 1.0}.get(hh, 0.6)
        else:
            w = {11: 0.8, 12: 1.3, 13: 1.0, 14: 0.5, 15: 0.5, 16: 0.8, 17: 1.2, 18: 1.6, 19: 1.7, 20: 1.4,
                 21: 1.1, 22: 0.8, 23: 0.6, 0: 0.5, 1: 0.35}.get(hh, 0.3)
        if wk in (4, 5) and hh in (22, 23, 0, 1):
            w *= 1.6
        base[h] = w
    d = demand(day)
    for h, l in (d.get("window") or {}).items():
        if h in base:
            base[h] *= 1.0 + l
    return base
