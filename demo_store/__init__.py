"""
demo_store — Halsted & Rye, the showcase demo restaurant (owner, 10/16/26).

A fictional Lakeview sports bar & grill that runs like a live RPOWER store:
its POS answers RPOWER's own Core API calls (demo_store.api, reached through
rpower._request when the restaurant's token is a demo-store token), so every
RPOWER pipeline — the archive, the labor sync, the Daily Sales Report, the
Bar, Food Cost, service performance, the live view — runs on it unchanged.
Today's checks post on the Chicago clock as the day goes; every past day is
the same every time it is asked for (seeded by its date).

Nothing here is a real restaurant, a real person or a real number: no row of
any client's data is copied into it (Will, 10/16/26: EJ's is confidential).
The account never sends anything — email, text, push or a schedule push to
a POS — and never calls Google, RPOWER or any outside data service for
itself (is_simulated, checked at each send and push).

  world      the restaurant: menu, recipes, staff, rooms, suppliers
  calendar   demand: weekday, season, Chicago's games and holidays, weather
  day        one business day's checks, lines, punches and payments
  api        RPOWER's Core API, answered from `day` and `world`
  build      create the account and fill its history (scripts/demo_store_build.py)
"""

TOKEN_PREFIX = "demo-store:"
TOKEN = TOKEN_PREFIX + "halsted-rye"
NAME = "Halsted & Rye"


def is_simulated(restaurant) -> bool:
    """True for a restaurant whose POS is the simulated store: a restaurant
    object or row, or an id. Never raises (a lookup failure is False — the
    callers that refuse sends treat their own failure separately)."""
    try:
        if isinstance(restaurant, int) or (isinstance(restaurant, str) and restaurant.isdigit()):
            from models import get_restaurant
            restaurant = get_restaurant(int(restaurant))
        if restaurant is None:
            return False
        tok = getattr(restaurant, "rpower_token", None)
        if tok is None:
            try:
                tok = restaurant["rpower_token"]
            except Exception:
                tok = None
        return str(tok or "").startswith(TOKEN_PREFIX)
    except Exception:
        return False
