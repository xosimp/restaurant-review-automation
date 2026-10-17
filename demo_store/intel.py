"""
demo_store.intel — Halsted & Rye's Intel, simulated (owner, 10/17/26: "give
this demo account an intel tab with all populated fake data").

Nothing here asks Google or an AI search engine anything. The block's
competitors, their reviews, the competitor read, the weekly movement, the
restaurant's own rating and the AI-visibility answers are written here and
go through each feature's own storage path (competitor._store_analysis,
event_memory, the AI-visibility run itself with these answers in place of
Perplexity's), so the Intel tab reads the demo exactly as it reads a client.
Every competitor and review is fictional; every day reads the same whenever
it is generated (seeded by its date).
"""
from datetime import date, datetime, timedelta

from demo_store import calendar as cal

PLACE_ID = "demo-halsted-rye"          # never a Google Place ID: the Places gateway refuses the demo anyway
CITY, STATE = "Chicago", "IL"
MENU_URL = "https://halstedandrye.example/menu"
SAME = "same cuisine type and similar price level"
ADDED = "added by you"

# (place_id, name, rating, review_count, price_level, vicinity, match_basis)
COMPETITORS = (
    ("hr-comp-seventh", "Seventh Inning Social", 4.2, 1840, 2, "N Clark St, Lakeview", SAME),
    ("hr-comp-northbound", "Northbound Alehouse", 4.4, 960, 2, "N Southport Ave, Lakeview", SAME),
    ("hr-comp-bleacher", "Bleacher & Brine", 4.0, 2310, 2, "W Addison St, Wrigleyville", SAME),
    ("hr-comp-cornerkick", "Corner Kick Tavern", 4.3, 610, 1, "N Sheffield Ave, Lakeview", SAME),
    ("hr-comp-ashland", "Ashland Ember Grill", 4.5, 1120, 2, "N Ashland Ave, Roscoe Village", SAME),
    ("hr-comp-ivyhall", "Ivy Hall Taproom", 4.1, 430, 2, "N Halsted St, Lakeview", ADDED),
)

# What guests wrote about each (stars, text, days ago). Refs R1.. follow this order.
REVIEWS = {
    "hr-comp-seventh": ((5, "The rooftop patio is the best seat in the neighborhood on a warm Cubs afternoon. Got there early and it filled up fast.", 6),
                        (2, "Great spot until the game let out — then it took forty minutes to get a second round and nobody checked on us.", 13),
                        (4, "Solid nachos, big TVs, loud in the best way. Service is hit or miss on game days.", 24)),
    "hr-comp-northbound": ((5, "The rotating local draft list is why we keep coming back. Bartenders know every beer on the board.", 4),
                           (4, "Good food, great beer, a little pricey for a sports bar but worth it for the list.", 17),
                           (3, "Nice place but the kitchen closes early, so no late-night food after a night game.", 31)),
    "hr-comp-bleacher": ((2, "Waited over an hour on a Cubs Saturday with no call-ahead list. Wings came out lukewarm.", 3),
                         (2, "So crowded you can't move, and the wings were cold by the time they reached us.", 9),
                         (4, "Huge place, tons of screens, fun crowd. Expect a wait on game days.", 20)),
    "hr-comp-cornerkick": ((5, "Cheap drinks and the friendliest bartenders in Lakeview. A real neighborhood tavern.", 5),
                           (3, "Love the vibe, but the menu is short — mostly fried food and not much else.", 15),
                           (4, "Great for soccer mornings, they open early for the big matches.", 28)),
    "hr-comp-ashland": ((5, "Best burger I've had in Chicago, and booking ahead meant no wait even on a Bears Sunday.", 2),
                        (5, "Reservations, attentive service and a burger worth the trip. Calm even on game nights.", 11),
                        (4, "Pricier than the bars by Wrigley, but the food is a step above.", 26)),
    "hr-comp-ivyhall": ((4, "New taproom with a great patio. Food menu is still finding its feet.", 7),
                        (3, "Beer was great, food took forever and came out in the wrong order.", 19),
                        (4, "Good spot before a game, quieter than the places on Clark.", 33)),
}

INSIGHT = """Halsted & Rye holds its own on a block crowded with game-day bars: Ashland Ember Grill is the one rated above you, and Bleacher & Brine is the busiest room with the most complaints about waits.

WHAT COMPETITORS ARE DOING WELL:
- Ashland Ember Grill: guests single out the burger and the reservations on game nights [R13, R14].
- Northbound Alehouse: regulars praise the rotating local draft list [R4].
- Seventh Inning Social: guests love the rooftop patio on warm Cubs afternoons [R1].

WHAT COMPETITORS ARE DOING POORLY:
- Bleacher & Brine: guests describe long game-day waits with no call-ahead list, and cold wings [R7, R8].
- Seventh Inning Social: guests say service slows once the game lets out [R2].
- Corner Kick Tavern: guests call the menu short and mostly fried food [R11].

Recommendations:
1. Run a call-ahead list for Cubs and Bears nights and say so on the listing; the wait is the block's most common complaint [R7].
2. Put the smash burger front and centre on game-day menus; guests single out Ashland Ember Grill's [R13].
3. Rotate one local draft each week and name it on the board; Northbound Alehouse's regulars praise their list [R4].
4. Keep the kitchen open past the late game; guests at Northbound Alehouse note its kitchen closes early [R6]."""

# The Business Profile as a listing read would find it (the checklist's description, phone, website, hours).
LISTING = {
    "read": True,
    "description": ("Halsted & Rye is a neighborhood sports bar three blocks from Wrigley Field: a scratch kitchen "
                    "known for smash burgers, Italian beef and wings, ten drafts including Old Style, and every "
                    "Cubs, Bears, Bulls and Blackhawks game on."),
    "phone": "(773) 555-0142",
    "website": "https://halstedandrye.example/",
    "has_hours": True,
}

# What an AI search engine cites for the answers below (.example: never a real site).
SOURCES = ("https://www.yelp.example/biz/halsted-and-rye-chicago",
           "https://chicago.eater.example/maps/best-sports-bars-wrigleyville",
           "https://www.timeout.example/chicago/bars/best-sports-bars",
           "https://www.chicagoreader.example/food-drink/lakeview-bars",
           "https://halstedandrye.example/")

_AS_OF = {"day": None}     # the build backdates a run by setting this; None = today


def _today():
    from demo_store import api
    return _AS_OF["day"] or api.business_today()


def _rng(*key):
    return cal._rng("intel", *key)


def competitors_on(day: date) -> list:
    """The block as a check on `day` finds it: each competitor's rating and
    review count drift week by week (seeded), its three newest reviews with
    refs R1.. in order."""
    out, ref = [], 0
    weeks_back = max(0, (_today() - day).days // 7)
    for pid, name, rating, count, price, vicinity, basis in COMPETITORS:
        r = _rng("comp", pid, day.isocalendar()[1], day.year)
        drift = (r.random() - 0.5) * 0.08 - 0.004 * weeks_back * (1 if pid == "hr-comp-bleacher" else 0)
        reviews = []
        for stars, text, ago in REVIEWS[pid]:
            ref += 1
            d = day - timedelta(days=ago)
            reviews.append({"author": f"Google reviewer {ref}", "rating": stars, "text": text,
                            "time": f"{ago} days ago" if ago < 7 else f"{ago // 7} weeks ago",
                            "date": d.isoformat(), "ts": int(datetime(d.year, d.month, d.day).timestamp()),
                            "ref": f"R{ref}"})
        rc = max(15, count - 9 * weeks_back + int(r.random() * 6))
        out.append({"place_id": pid, "name": name, "rating": round(min(4.9, max(3.5, rating + drift)), 1),
                    "review_count": rc, "vicinity": vicinity, "price_level": price, "types": ["bar", "restaurant"],
                    "match_basis": basis, "rating_is_provisional": rc < 15, "distance_m": 400 + 180 * ref % 900,
                    "business_status": "OPERATIONAL", "custom": basis == ADDED, "reviews": reviews,
                    "reviews_at": day.isoformat()})
    return out


def own_rating(db_reviews: list) -> tuple:
    """(rating, count) of Halsted & Rye's own Google listing: its imported reviews over a long history
    (the build's base of 812 earlier reviews, as fill_neighborhood always assumed)."""
    n, s = 812, 812 * 4.5
    for stars in db_reviews:
        n += 1
        s += stars
    return round(s / n, 1), n


def ai_answer(query: str, kind: str):
    """(answer, sources) for one AI-visibility question, as an AI search
    engine answers it: Halsted & Rye named in Lakeview, Chicago more often
    as weeks pass (its listing and reviews improved), two or three of the
    block's competitors beside it."""
    day = _today()
    week = day.isocalendar()[1] + 52 * (day.year - 2026)
    r = _rng("ai", query, week)
    if kind == "branded":
        return ("Halsted & Rye is a neighborhood sports bar in Lakeview, Chicago, three blocks from Wrigley Field. "
                "Guests praise the smash burgers, Italian beef and wings, ten drafts including Old Style, and every "
                "Cubs and Bears game on its screens. It gets busy on game days."), [SOURCES[0], SOURCES[4], SOURCES[1]]
    # Each question has its own fixed bar (seeded by the question alone) and the share of answers naming
    # Halsted & Rye rises week by week, so a question that names it keeps naming it: a steady climb, never
    # a coin toss that reads as a slide.
    share = min(0.82, 0.30 + 0.035 * (week - 34) + {"cuisine": 0.10, "occasion": 0.05}.get(kind, 0.0))
    bar = _rng("ai-bar", query).random()
    names = [c[1] for c in COMPETITORS]
    r.shuffle(names)
    picks = names[:2 + int(r.random() * 2)]
    lines = []
    if bar < share:
        lines.append("Halsted & Rye in Lakeview, Chicago for smash burgers, Italian beef and every game on")
    blurbs = {"Seventh Inning Social": "for its rooftop patio near Wrigley Field",
              "Northbound Alehouse": "for a rotating list of local drafts",
              "Bleacher & Brine": "for a huge room full of screens",
              "Corner Kick Tavern": "for cheap drinks and early soccer mornings",
              "Ashland Ember Grill": "for one of the city's best burgers",
              "Ivy Hall Taproom": "for a quieter pre-game patio"}
    for n in picks:
        lines.append(f"{n} in Chicago {blurbs[n]}")
    r.shuffle(lines)
    answer = "Good options: " + "; ".join(lines) + "."
    return answer, list(SOURCES[1:4])


# ── what the build and the nightly upkeep write ─────────────────────────────

def _store_read(rid, day):
    """Today's competitor read through competitor's own storage path (blob, snapshot, market history, the
    read kept as history), then validated on read as every stored read is."""
    import competitor
    import models
    prev = _AS_OF["day"]
    _AS_OF["day"] = day
    try:
        comps = competitors_on(day)
    finally:
        _AS_OF["day"] = prev
    r = models.get_restaurant(rid)
    out = competitor._store_analysis(r, comps, INSIGHT, [], None, [c["place_id"] for c in comps if c.get("custom")],
                                     None, {})
    r = models.get_restaurant(rid)
    try:
        import json
        competitor.current_intel(rid, json.loads(r.competitor_intel), persist=True)
    except Exception as e:
        print(f"[demo_store.intel] read not validated: {e}")
    return out


def _own_rating_now(rid):
    import models
    import event_memory
    conn = models.get_conn()
    try:
        stars = [int(x[0]) for x in conn.execute("SELECT rating FROM reviews WHERE restaurant_id=? AND rating IS NOT NULL",
                                                 (rid,)).fetchall()]
    finally:
        conn.close()
    rating, count = own_rating(stars)
    models.update_restaurant(rid, {"gbp_rating": rating, "gbp_review_count": count})
    event_memory.record_own_rating(rid, rating, count, source="google")
    return rating, count


def ai_visibility_run(rid, day=None):
    """One AI-visibility run through the real pipeline (client_api._do_ai_visibility_inner), the answers
    from ai_answer; backdated to `day` when one is given."""
    import client_api
    import models
    _AS_OF["day"] = day
    try:
        client_api._aivis_cache.pop(rid, None)
        res = client_api._do_ai_visibility_inner(rid, force=True)
    finally:
        _AS_OF["day"] = None
    if day is not None:
        stamp = f"{day.isoformat()} 10:00:00"
        conn = models.get_conn()
        try:
            row = conn.execute("SELECT id FROM ai_visibility_runs WHERE restaurant_id=? ORDER BY id DESC LIMIT 1",
                               (rid,)).fetchone()
            if row:
                conn.execute("UPDATE ai_visibility_runs SET created_at=? WHERE id=?", (stamp, row[0]))
                try:
                    conn.execute("UPDATE ai_visibility_query_runs SET created_at=? WHERE run_id=?", (stamp, row[0]))
                except Exception:
                    conn.rollback()
            conn.commit()
        finally:
            conn.close()
    client_api._aivis_cache.pop(rid, None)
    payload = res[0] if isinstance(res, tuple) else res
    return (payload or {}).get("ai_score")


def fill(rid, weeks=12, ai_weeks=8):
    """The whole Intel tab: the listing settings, `weeks` of weekly competitor checks (movement, market
    history, the own-rating line), today's read, and `ai_weeks` of AI-visibility runs."""
    import json
    import event_memory
    import models
    today = _today()
    models.update_restaurant(rid, {
        "google_place_id": PLACE_ID, "menu_url": MENU_URL,
        "aivis_city_json": json.dumps({"place_id": PLACE_ID, "city": CITY, "state": STATE,
                                       "at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")}),
    })
    conn = models.get_conn()
    try:
        for t in ("competitor_snapshots", "competitor_rating_monthly", "market_events", "own_rating_history"):
            conn.execute(f"DELETE FROM {t} WHERE restaurant_id=?", (rid,))
        for t in ("ai_visibility_query_runs", "ai_visibility_runs"):
            try:
                conn.execute(f"DELETE FROM {t} WHERE restaurant_id=?", (rid,))
            except Exception:
                conn.rollback()
        conn.commit()
    finally:
        conn.close()
    # The weekly checks before today: snapshots, market history and the own rating, each on its Monday.
    stars_by_week = {}
    conn = models.get_conn()
    try:
        rows = conn.execute("SELECT review_date, rating FROM reviews WHERE restaurant_id=? AND rating IS NOT NULL "
                            "ORDER BY review_date", (rid,)).fetchall()
    finally:
        conn.close()
    for k in range(weeks, 0, -1):
        mon = today - timedelta(days=today.weekday() + 7 * k)
        _AS_OF["day"] = today
        comps = competitors_on(mon)
        _AS_OF["day"] = None
        conn = models.get_conn()
        try:
            for c in comps:
                conn.execute("INSERT INTO competitor_snapshots (restaurant_id, place_id, name, rating, review_count, "
                             "price_level, match_basis, captured_at) VALUES (?,?,?,?,?,?,?,?)",
                             (rid, c["place_id"], c["name"], c["rating"], c["review_count"], c["price_level"],
                              c["match_basis"], f"{mon.isoformat()} 06:00:00"))
            conn.commit()
        finally:
            conn.close()
        event_memory.record_market_snapshot(rid, comps, at=datetime(mon.year, mon.month, mon.day, 6))
        stars = [int(x["rating"]) for x in rows if str(x["review_date"])[:10] < mon.isoformat()]
        rating, count = own_rating(stars)
        event_memory.record_own_rating(rid, rating, count, source="google", at=datetime(mon.year, mon.month, mon.day, 6))
        stars_by_week[mon] = (rating, count)
    _own_rating_now(rid)
    _store_read(rid, today)
    scores = []
    for k in range(ai_weeks - 1, 0, -1):
        scores.append(ai_visibility_run(rid, today - timedelta(days=today.weekday() + 7 * k)))
    scores.append(ai_visibility_run(rid))
    return {"weeks": weeks, "competitors": len(COMPETITORS), "ai_scores": scores}


def refresh(rid, today):
    """One night's upkeep: the own rating every night; the competitor read (with its snapshot and market
    history) on Mondays or once the stored one is five days old, so it never reads stale; an AI-visibility
    run on Mondays."""
    import models
    out = {"own": _own_rating_now(rid)}
    r = models.get_restaurant(rid)
    try:
        at = datetime.fromisoformat(str(getattr(r, "competitor_updated_at", "") or "")[:19])
        old = (datetime.utcnow() - at).days >= 5
    except ValueError:
        old = True
    if today.weekday() == 0 or old:
        out["read"] = bool(_store_read(rid, today).get("ok"))
    if today.weekday() == 0:
        out["ai"] = ai_visibility_run(rid)
    return out
