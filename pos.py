"""
pos.py — one front door for every POS integration.

toast.py, square.py, and clover.py are three near-identical copy-pastes of the
same shape (is_connected / test_credentials / build_shifts_csv / sync_to_db),
and features kept landing in only one of them: the nightly labor sync was
Toast-only, so a Square or Clover client silently never got fresh shift data.
Adding a 4th provider means implementing this module's PROVIDER_API and adding
one registry line — not copying 500 lines and hoping every call site notices.
"""
import logging

log = logging.getLogger("pos")

# Each provider module must expose:
#   is_connected(restaurant_id) -> bool
#   sync_to_db(restaurant_id) -> {"ok": bool, ...}
#   build_shifts_csv(restaurant_id, days=60) -> str|None
PROVIDER_API = ("is_connected", "sync_to_db", "build_shifts_csv")


def _load_providers():
    import toast, square, clover
    return {"toast": toast, "square": square, "clover": clover}


# Populated lazily so tests can inject fakes and a broken provider import
# can't take down the app at boot.
PROVIDERS = None


def get_providers():
    global PROVIDERS
    if PROVIDERS is None:
        PROVIDERS = _load_providers()
    return PROVIDERS


def connected_provider(restaurant_id):
    """(name, module) for the first provider this restaurant is connected to,
    else (None, None)."""
    for name, mod in get_providers().items():
        try:
            if mod.is_connected(restaurant_id):
                return name, mod
        except Exception as e:
            log.error(f"pos.is_connected crashed for {name}: {e}")
    return None, None


def sync_restaurant(restaurant_id):
    """Sync whichever POS this restaurant uses. Uniform result shape."""
    name, mod = connected_provider(restaurant_id)
    if not mod:
        return {"ok": False, "provider": None, "error": "No POS connected"}
    try:
        result = mod.sync_to_db(restaurant_id) or {}
        result.setdefault("ok", False)
        result["provider"] = name
        return result
    except Exception as e:
        return {"ok": False, "provider": name, "error": str(e)}


def sync_all():
    """Nightly: sync every restaurant that has ANY provider connected —
    not just Toast. One restaurant failing never blocks the rest."""
    from models import get_all_restaurants
    import ops
    results = []
    for r in get_all_restaurants():
        name, mod = connected_provider(r.id)
        if not mod:
            continue
        result = sync_restaurant(r.id)
        results.append({"restaurant": r.name, **result})
        if result["ok"]:
            log.info(f"POS sync OK [{name}] {r.name} — {result.get('rows', '?')} rows")
        else:
            log.warning(f"POS sync failed [{name}] {r.name}: {result.get('error')}")
            ops.capture(Exception(result.get("error", "unknown")),
                        job="pos_sync", context=f"{name} {r.name}")
    return results


def connection_status(restaurant_id):
    """Uniform status for UI: which provider, connected or not."""
    name, mod = connected_provider(restaurant_id)
    return {"connected": bool(mod), "provider": name}
