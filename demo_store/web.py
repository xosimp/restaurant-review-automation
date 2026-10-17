"""
demo_store.web — Halsted & Rye's website, simulated: Google Analytics 4 and
Search Console as web_analytics would read them (owner, 10/17/26: "give this
demo account fake google analytics data").

Nothing is fetched from Google: web_analytics.sync reads these instead of
the APIs when the restaurant is the simulated store (demo_store.is_simulated).
Every day is the same every time it is asked for (seeded by its date) and
moves with the store's own demand (demo_store.calendar.demand): a Cubs or
Bears night, a holiday and the season lift visits the way they lift sales.
The shapes are read_ga4's and read_gsc's: {(iso day, metric): value}.
"""
import math
from datetime import date, timedelta

from demo_store import calendar as cal

PROPERTY_ID = "400000001"                 # a well-formed GA4 id that no Google property answers to
SITE_URL = "https://halstedandrye.example/"   # .example: reserved, never a real site
BOOKING = "exploretock.com"
ORDERING = "doordash.com"

# What people search before they come (query, share of the month's clicks, typical position).
QUERIES = (
    ("halsted and rye", 0.30, 1.1),
    ("halsted & rye chicago", 0.09, 1.3),
    ("sports bar near wrigley field", 0.08, 4.6),
    ("bars near wrigley", 0.07, 6.2),
    ("best smash burger chicago", 0.05, 8.4),
    ("bears game bar lakeview", 0.05, 3.8),
    ("italian beef lakeview", 0.04, 5.1),
    ("wings near me", 0.04, 9.7),
    ("halsted and rye menu", 0.04, 1.2),
    ("lakeview restaurants", 0.03, 11.3),
    ("cubs bar wrigleyville", 0.03, 5.6),
    ("happy hour lakeview", 0.03, 7.9),
    ("brunch wrigleyville", 0.02, 12.4),
    ("old style on draft chicago", 0.02, 3.2),
    ("halsted and rye reservations", 0.02, 1.4),
    ("trivia night lakeview", 0.015, 8.8),
    ("late night food lakeview", 0.015, 10.2),
    ("halsted rye hours", 0.015, 1.0),
    ("group dinner wrigleyville", 0.01, 9.1),
    ("watch party bar chicago", 0.01, 13.5),
)


def _rng(*key):
    return cal._rng("web", *key)


def _visits(day: date) -> float:
    """A day's sessions: about one visit per $45 of the night's sales, a little
    ahead of it (people look the day before a game), never zero."""
    d = cal.demand(day)
    nxt = cal.demand(day + timedelta(days=1))
    target = d["target"] or 1500.0
    pull = 0.75 * target + 0.25 * (nxt["target"] or target)
    r = _rng("visits", day.isoformat())
    return max(40.0, pull / 45.0 * math.exp(r.gauss(0, 0.09)))


def read_ga4(property_id, start: date, end: date) -> dict:
    """{(day, metric): value} the way web_analytics.read_ga4 returns it."""
    out = {}
    d = start
    while d <= end:
        iso, r = d.isoformat(), _rng("ga4", d.isoformat())
        s = round(_visits(d))
        users = round(s * (0.82 + 0.06 * r.random()))
        pv = round(s * (2.3 + 0.5 * r.random()))
        engaged = round(s * (0.58 + 0.08 * r.random()))
        game = bool(cal.demand(d)["events"])
        mix = {"Organic Search": 0.47, "Direct": 0.24, "Organic Social": 0.13 + (0.04 if game else 0.0),
               "Referral": 0.07, "Paid Social": 0.05, "Email": 0.03}
        tot = sum(mix.values())
        for k, v in (("sessions", s), ("users", users), ("pageviews", pv), ("engaged_sessions", engaged)):
            out[(iso, k)] = float(v)
        for ch, share in mix.items():
            out[(iso, f"channel:{ch}")] = float(round(s * share / tot))
        book = round(s * (0.045 + (0.03 if game else 0.0)) * (0.8 + 0.4 * r.random()))
        order = round(s * 0.035 * (0.8 + 0.4 * r.random()))
        out[(iso, "booking_clicks")] = float(book)
        out[(iso, f"click:{BOOKING}")] = float(book)
        out[(iso, "ordering_clicks")] = float(order)
        out[(iso, f"click:{ORDERING}")] = float(order)
        out[(iso, "menu_views")] = float(round(s * (0.38 + 0.08 * r.random())))
        d += timedelta(days=1)
    return out


def read_gsc(site_url, start: date, end: date) -> dict:
    """{(day, metric): value} the way web_analytics.read_gsc returns it."""
    out = {}
    d = start
    while d <= end:
        iso, r = d.isoformat(), _rng("gsc", d.isoformat())
        s = _visits(d)
        clicks = round(s * 0.42 * (0.9 + 0.2 * r.random()))
        out[(iso, "search_clicks")] = float(clicks)
        out[(iso, "search_impressions")] = float(round(clicks * (11.5 + 3.0 * r.random())))
        out[(iso, "search_position")] = round(6.4 + 1.6 * r.random(), 2)
        d += timedelta(days=1)
    return out


def query_rows(site_url, start: date, end: date, dimensions=None, limit=25000) -> list:
    """Search Console's query rows over [start, end] (web_analytics.gsc_query's shape)."""
    total = sum(v for (_d, m), v in read_gsc(site_url, start, end).items() if m == "search_clicks")
    rows = []
    for q, share, pos in QUERIES[:limit]:
        r = _rng("query", q, end.isoformat())
        clicks = round(total * share * (0.85 + 0.3 * r.random()))
        ctr = max(0.02, 0.32 / (1.0 + 0.55 * (pos - 1.0)))
        rows.append({"keys": [q], "clicks": clicks, "impressions": round(clicks / ctr),
                     "position": round(pos * (0.92 + 0.16 * r.random()), 1)})
    return rows
