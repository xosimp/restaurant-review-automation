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
    ISO with an offset (toast/square/clover) or naive UTC (rpower)."""
    if not value:
        return None
    s = str(value).strip().replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _connected(r, name):
    """Whether the restaurant row carries this provider's connection, read
    from the columns only (no provider module import)."""
    cols = {"toast": ("toast_restaurant_guid",), "square": ("square_access_token",),
            "clover": ("clover_api_token",), "rpower": ("rpower_token", "rpower_store_mid")}
    return any(_get(r, c) for c in cols.get(name, ())) or bool(_get(r, f"{name}_last_synced"))


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
        stamp = parse_stamp(_get(r, f"{chosen}_last_synced"))
        err = _get(r, f"{chosen}_sync_error") or None
        out.update(provider=chosen, connected=True, error=err)
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
