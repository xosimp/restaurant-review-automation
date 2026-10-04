"""
reservation_feeds.py — reservation counts from the book, into demand_signals.

demand_signals takes what the owner types or pastes. This is the frame for
the feed doing it instead: one provider per restaurant, one sync a week
the day before the restaurant's draft (Thursday unless the owner picked
another day), writing rows with source=<provider> so the
schedule reads them exactly like a pasted CSV.

No provider is live. Each one below says what it needs (an API key or an
OAuth grant the platform has not been issued) and refuses cleanly until
then; the settings screen shows the same sentence. When a provider goes
live, only its `fetch` changes.

What works without any partnership (schedule audit 10/3/26 D-31): every
one of these systems exports its bookings as a report, and that file
imports as it is under Events & reservations (demand_signals.
parse_reservation_export — one row per booking, summed per date, cancelled
and no-show bookings left out), source "report". The schedule reads those
rows exactly as it would a live feed's.

What a live provider still needs, precisely:
  1. Cavnar AI admitted to the provider's partner program, and the
     credentials it issues (a partner API key, or OAuth client id/secret)
     stored as a platform secret — none of the three is self-serve.
  2. The restaurant's own id at the provider (its restaurant / venue /
     business id) and its consent for Cavnar AI to read its bookings
     (restaurants.reservation_api_key holds what the provider's flow
     returns).
  3. `fetch(restaurant, start, end)` written against the provider's
     documented reservations endpoint: [{date, covers}] for the range,
     cancelled and no-show bookings out, every request with a timeout
     (scripts/check_timeouts.py), paging to the end (a truncated page fails
     the sync rather than storing part of a night), the raw response kept.
"""
from datetime import date, timedelta

import models as _models_mod


def get_conn(db_path=None):
    """models.get_conn, resolved at call time — CLAUDE.md's bound-import
    hazard. `from models import get_conn` bound whichever function models
    held when this module was first imported, so a test that happened to
    import it while models.get_conn was patched left every later caller on
    that test's database."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()


def _path(db_path):
    """The database: the caller's, else models.DB_PATH as it is now."""
    return db_path if db_path is not None else _models_mod.DB_PATH


class NotConfigured(RuntimeError):
    pass


_IMPORT_INSTEAD = ("Until then, export your reservations report from {p} and import the file under Events & "
                   "reservations — each booked night counts the same as a live feed.")


def _tock(restaurant, start, end):
    raise NotConfigured("A live Tock feed needs Cavnar AI admitted to Tock's partner program (the partner key "
                        "Tock issues for its API) and your Tock business id authorized for it. "
                        + _IMPORT_INSTEAD.format(p="Tock"))


def _opentable(restaurant, start, end):
    raise NotConfigured("A live OpenTable feed needs Cavnar AI approved as an OpenTable partner (the client "
                        "credentials OpenTable issues) and your OpenTable restaurant id authorized for it. "
                        + _IMPORT_INSTEAD.format(p="OpenTable"))


def _resy(restaurant, start, end):
    raise NotConfigured("A live Resy feed needs a Resy partner key (Resy issues these to approved partners only) "
                        "and your Resy venue id authorized for Cavnar AI. " + _IMPORT_INSTEAD.format(p="Resy"))


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
        return {"provider": None, "configured": False, "live": False,
                "message": "No reservation system connected — import your reservation system's booking export "
                           "(or type the covers) under Events & reservations.",
                "import": True}
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


def sync(restaurant_id, days: int = 21, db_path=None) -> dict:
    """Pull covers for the next `days` days and write them as reservation
    signals. Returns {written, skipped, error}; never raises."""
    import demand_signals
    db_path = _path(db_path)
    r = _models_mod.get_restaurant(restaurant_id, db_path)
    if not r:
        return {"written": 0, "skipped": 0, "error": "no restaurant"}
    code = (getattr(r, "reservation_provider", None) or "").strip().lower()
    p = PROVIDERS.get(code)
    if not p:
        return {"written": 0, "skipped": 0, "error": "no provider", "not_configured": True}
    start, end = date.today(), date.today() + timedelta(days=days)
    try:
        rows = p["fetch"](r, start, end)     # [{date, covers}]
    except NotConfigured as e:
        return {"written": 0, "skipped": 0, "error": str(e), "not_configured": True}
    except Exception as e:
        return {"written": 0, "skipped": 0, "error": f"{p['label']} sync failed: {e}"}
    out = demand_signals.save(restaurant_id, [{"date": x["date"], "kind": "reservations", "covers": x["covers"]} for x in rows],
                              source=code, db_path=db_path)
    return {"written": out["written"], "skipped": out["skipped"], "error": None}


def run_reservation_sync(db_path=None, weekday=None) -> dict:
    """Every restaurant with a provider set - with `weekday` (today, 0 =
    Monday), only those whose schedule is drafted tomorrow, so each feed
    lands the day before its own draft (models.auto_draft_weekday; the
    scheduler runs this daily). Unconfigured ones cost one dict each and
    are counted, not retried. Returns the standard counts (#39): a feed
    whose provider is not live yet is skipped, one that fails is failed."""
    from models import AUTO_DRAFT_WEEKDAY_DEFAULT
    db_path = _path(db_path)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT id, auto_draft_weekday FROM restaurants WHERE reservation_provider IS NOT NULL "
                            "AND reservation_provider<>'' AND module_labor=1").fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    ids = [r["id"] for r in rows
           if weekday is None or (r["auto_draft_weekday"] if r["auto_draft_weekday"] is not None
                                  else AUTO_DRAFT_WEEKDAY_DEFAULT) == (weekday + 1) % 7]
    synced = skipped = failed = 0
    for rid in ids:
        res = sync(rid, db_path=db_path)
        if res.get("not_configured"):
            skipped += 1
        elif res.get("error"):
            failed += 1
            try:
                import ops
                ops.capture(RuntimeError(res["error"]), job="reservation_sync", context=f"restaurant_id={rid}")
            except Exception:
                pass
        else:
            synced += 1
    return {"attempted": synced + failed, "ok": synced, "failed": failed, "skipped": skipped, "hit_bound": False,
            "synced": synced}
