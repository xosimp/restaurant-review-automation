"""
pos_health.py — one provider-agnostic reading of a restaurant's POS sync.

Every surface that says whether the POS is current (Home's sync-error
attention, the status page, admin integrations, mobile connections, the
confidence engine's Data Freshness) reads this instead of naming one
provider's columns. It resolves the provider by name over pos.PROVIDERS and
reads f"{provider}_last_synced" / f"{provider}_sync_error", the way
dsr/block_labor.py already does — so RPOWER (Simple EJ's) is never missed
again.

Pure over the restaurant row; no network, no writes.
"""
from datetime import datetime, timezone

# Providers whose sync columns live on restaurants, in the order a restaurant
# connected to several is read (the first with a stamp wins). Kept in step
# with pos.PROVIDERS by tests/test_pos_health.py.
PROVIDER_NAMES = ("toast", "square", "clover", "rpower")


def _get(r, name):
    if r is None:
        return None
    if isinstance(r, dict):
        return r.get(name)
    return getattr(r, name, None)


def parse_stamp(value):
    """A sync stamp as an aware UTC datetime, or None. Provider stamps are
    ISO with an offset (toast/square/clover) or naive UTC (rpower). The one
    parser is time_utils.parse_stamp; naive means UTC for every sync stamp."""
    from time_utils import parse_stamp as _parse_stamp
    return _parse_stamp(value, naive_tz="UTC")


def _connected(r, name):
    """Whether the restaurant row carries this provider's connection, read
    from the columns only (no provider module import)."""
    cols = {"toast": ("toast_restaurant_guid",), "square": ("square_access_token",),
            "clover": ("clover_api_token",), "rpower": ("rpower_token", "rpower_store_mid")}
    return any(_get(r, c) for c in cols.get(name, ())) or bool(_get(r, f"{name}_last_synced"))


def provider_state(r, name, now=None) -> dict:
    """The same reading as pos_sync_state for ONE named provider, whether or
    not it is the one pos_sync_state would choose — for surfaces that list
    every provider (admin integrations). Never raises."""
    now = now or datetime.now(timezone.utc)
    out = {"provider": name, "connected": False, "last_synced": None, "last_synced_iso": None,
           "age_days": None, "error": None, "state": "not_connected"}
    try:
        if not _connected(r, name):
            return out
        stamp = parse_stamp(_get(r, f"{name}_last_synced"))
        err = _get(r, f"{name}_sync_error") or None
        out.update(connected=True, error=err)
        if stamp is not None:
            age = max(0.0, (now - stamp).total_seconds() / 86400.0)
            out.update(last_synced=stamp.isoformat(), last_synced_iso=stamp.date().isoformat(),
                       age_days=round(age, 2))
        if err:
            out["state"] = "error"
        elif stamp is None:
            out["state"] = "unknown"
        else:
            out["state"] = "current" if age <= 1.5 else ("aging" if age <= 3 else "stale")
    except Exception as e:
        print(f"[pos_health] unreadable: {e}")
        out["state"] = "unknown"
    return out


def pos_sync_state(r, now=None) -> dict:
    """{provider, connected, last_synced, last_synced_iso, age_days, error,
    state} for the restaurant's POS. state: not_connected | error | current
    | aging | stale | unknown. Thresholds: current ≤ 1.5 days (a nightly
    sync), aging ≤ 3 days, stale after. Never raises."""
    now = now or datetime.now(timezone.utc)
    out = {"provider": None, "connected": False, "last_synced": None, "last_synced_iso": None,
           "age_days": None, "error": None, "state": "not_connected"}
    try:
        chosen = None
        for name in PROVIDER_NAMES:
            if _connected(r, name):
                if chosen is None or (_get(r, f"{name}_last_synced") and not _get(r, f"{chosen}_last_synced")):
                    chosen = name
        if chosen is None:
            return out
        return provider_state(r, chosen, now=now)
    except Exception as e:
        print(f"[pos_health] unreadable: {e}")
        out["state"] = "unknown"
    return out
