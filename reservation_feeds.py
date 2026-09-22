"""
reservation_feeds.py — reservation counts from the book, into demand_signals.

demand_signals takes what the owner types or pastes. This is the frame for
the feed doing it instead: one provider per restaurant, one sync a week
ahead of the Thursday draft, writing rows with source=<provider> so the
schedule reads them exactly like a pasted CSV.

No provider is live. Each one below says what it needs (an API key or an
OAuth grant the platform has not been issued) and refuses cleanly until
then; the settings screen shows the same sentence. When a provider goes
live, only its `fetch` changes.
"""
from datetime import date, timedelta

from models import get_conn, DB_PATH, get_restaurant


class NotConfigured(RuntimeError):
    pass


def _tock(restaurant, start, end):
    raise NotConfigured("Tock exports covers by date from the Tock dashboard; the API needs a partner key Cavnar AI does not hold yet. Paste the export into Events & reservations for now.")


def _opentable(restaurant, start, end):
    raise NotConfigured("OpenTable's cover data needs a Connect partner grant; until then paste the reservation report into Events & reservations.")


def _resy(restaurant, start, end):
    raise NotConfigured("Resy's API is partner-only; paste the reservation report into Events & reservations for now.")


PROVIDERS = {
    "tock": {"label": "Tock", "fetch": _tock},
    "opentable": {"label": "OpenTable", "fetch": _opentable},
    "resy": {"label": "Resy", "fetch": _resy},
}


def available() -> list:
    return [{"code": k, "label": v["label"], "live": False} for k, v in PROVIDERS.items()]


def status(restaurant) -> dict:
    """What the settings screen and the schedule panel say about the feed."""
    code = (getattr(restaurant, "reservation_provider", None) or "").strip().lower()
    if not code:
        return {"provider": None, "configured": False, "live": False, "message": "No reservation system connected — paste covers into Events & reservations."}
    p = PROVIDERS.get(code)
    if not p:
        return {"provider": code, "configured": False, "live": False, "message": f"{code} is not a reservation system this build knows."}
    key = (getattr(restaurant, "reservation_api_key", None) or "").strip()
    try:
        p["fetch"](restaurant, date.today(), date.today())
        live = True
        message = f"{p['label']} connected."
    except NotConfigured as e:
        live = False
        message = str(e)
    return {"provider": code, "label": p["label"], "configured": bool(key), "live": live, "message": message}


def sync(restaurant_id, days: int = 21, db_path=DB_PATH) -> dict:
    """Pull covers for the next `days` days and write them as reservation
    signals. Returns {written, skipped, error}; never raises."""
    import demand_signals
    r = get_restaurant(restaurant_id, db_path)
    if not r:
        return {"written": 0, "skipped": 0, "error": "no restaurant"}
    code = (getattr(r, "reservation_provider", None) or "").strip().lower()
    p = PROVIDERS.get(code)
    if not p:
        return {"written": 0, "skipped": 0, "error": "no provider"}
    start, end = date.today(), date.today() + timedelta(days=days)
    try:
        rows = p["fetch"](r, start, end)     # [{date, covers}]
    except NotConfigured as e:
        return {"written": 0, "skipped": 0, "error": str(e)}
    except Exception as e:
        return {"written": 0, "skipped": 0, "error": f"{p['label']} sync failed: {e}"}
    out = demand_signals.save(restaurant_id, [{"date": x["date"], "kind": "reservations", "covers": x["covers"]} for x in rows],
                              source=code, db_path=db_path)
    return {"written": out["written"], "skipped": out["skipped"], "error": None}


def run_reservation_sync(db_path=DB_PATH) -> dict:
    """Weekly job: every restaurant with a provider set. Unconfigured ones
    cost one dict each and are counted, not retried."""
    conn = get_conn(db_path)
    try:
        ids = [r["id"] for r in conn.execute("SELECT id FROM restaurants WHERE reservation_provider IS NOT NULL AND reservation_provider<>'' AND module_labor=1").fetchall()]
    except Exception:
        ids = []
    finally:
        conn.close()
    synced = skipped = 0
    for rid in ids:
        res = sync(rid, db_path=db_path)
        if res.get("error"):
            skipped += 1
        else:
            synced += 1
    return {"synced": synced, "skipped": skipped}
