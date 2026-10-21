"""
pos_health.py — one provider-agnostic reading of a restaurant's POS sync.

Every surface that says whether the POS is current (Home's sync-error
attention, the status page, admin integrations, mobile connections, the
confidence engine's Data Freshness) reads this instead of naming one
provider's columns. It resolves the provider by name over pos.PROVIDERS and
reads f"{provider}_last_synced" / f"{provider}_sync_error", the way
dsr/block_labor.py already does — so RPOWER (Simple EJ's) is never missed
again.

The current / aging / stale cut-offs are NOT this module's own: they are
data_freshness's "pos" row read through confidence_engine.state (age_state
below), so every surface that names a POS state agrees with the confidence
engine's Data Freshness (re-audit B6#5, B3#9).

Pure over the restaurant row; no network, no writes.
"""
from datetime import datetime, timezone

# Providers whose sync columns live on restaurants. A restaurant with
# credentials for several is read by the one with the freshest sync stamp
# (ties in this order). Kept in step with pos.PROVIDERS by
# tests/test_pos_health.py.
PROVIDER_NAMES = ("toast", "square", "clover", "rpower")

# The credential columns that mean a provider is connected. A leftover
# `{name}_last_synced` is NOT a connection: a disconnect that left the stamp
# behind read as a connected POS whose data decayed to 0% fresh, which
# zeroed every labor and food card's confidence and named the restaurant on
# the public status page (re-audit B6#1).
CREDENTIAL_COLUMNS = {"toast": ("toast_restaurant_guid",), "square": ("square_access_token",),
                      "clover": ("clover_api_token",),
                      "rpower": ("rpower_token", "rpower_credential_id", "rpower_store_mid")}

# A stamp this far ahead of now is not a sync that happened (a few minutes
# of clock skew between hosts is tolerated).
FUTURE_TOLERANCE_DAYS = 1.0 / 24


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
    """Whether the restaurant row carries this provider's credentials, read
    from the columns only (no provider module import). Credentials only —
    never the sync stamp (CREDENTIAL_COLUMNS)."""
    return any(_get(r, c) for c in CREDENTIAL_COLUMNS.get(name, ()))


def age_state(age_days, error=None) -> tuple:
    """(pct, state) for a sync this many days old — the ONE POS freshness
    rule: data_freshness's "pos" row (expected lag, grace, horizon, the
    error ceiling) and confidence_engine's current/aging/stale cut-offs.
    An error is its own state; an unknown age is `unknown` with pct 0."""
    import data_freshness
    import confidence_engine as ce
    if age_days is None:
        return 0, ("error" if error else "unknown")
    pct = data_freshness.age_pct("pos", age_days, error=bool(error))
    return pct, ("error" if error else ce.state(pct))


def provider_state(r, name, now=None) -> dict:
    """The same reading as pos_sync_state for ONE named provider, whether or
    not it is the one pos_sync_state would choose — for surfaces that list
    every provider (admin integrations). Never raises."""
    now = now or datetime.now(timezone.utc)
    out = {"provider": name, "connected": False, "last_synced": None, "last_synced_iso": None,
           "age_days": None, "error": None, "state": "not_connected",
           "pct": None, "future": False, "unreadable": False}
    try:
        if not _connected(r, name):
            return out
        raw = _get(r, f"{name}_last_synced")
        stamp = parse_stamp(raw)
        err = _get(r, f"{name}_sync_error") or None
        shown = err
        if name == "rpower" and err:
            # Every surface this reading reaches shows an owner one fixed
            # sentence per failure class, never RPOWER's raw text (Audit #23
            # A7-14); the console reads the row's own column.
            import rpower as _rp_sentence
            shown = _rp_sentence.owner_error_sentence(err)
        out.update(connected=True, error=shown, error_raw=err, unreadable=bool(raw) and stamp is None)
        age = None
        if stamp is not None:
            age = (now - stamp).total_seconds() / 86400.0
            if age < -FUTURE_TOLERANCE_DAYS:
                # A stamp in the future is a clock or data error, not a sync
                # that happened: its age is unknown, never "current"
                # (re-audit B3#14).
                out["future"] = True
                age = None
            else:
                age = max(0.0, age)
                out.update(last_synced=stamp.isoformat(), last_synced_iso=stamp.date().isoformat(),
                           age_days=round(age, 2))
        pct, state = age_state(age, err)
        out.update(pct=pct, state=state)
    except Exception as e:
        print(f"[pos_health] unreadable: {e}")
        out["state"] = "unknown"
    return out


def primary_provider(r, names=None, now=None):
    """The ONE provider a restaurant's POS data comes from — what the nightly
    sync dispatches to (pos.connected_provider) and what every freshness
    reading reads (pos_sync_state), so the two can never name different
    providers during a migration (DH2-13). `names` limits the choice to
    providers the caller knows are connected (default: every provider whose
    credentials are on the row).

    The owner's recorded POS (restaurants.pos_system) when it names one of
    them; else the one with the freshest sync stamp (a restaurant that moved
    from Toast to RPOWER is not judged on a dead Toast stamp, re-audit
    B3#16), ties in PROVIDER_NAMES order. None when nothing is connected."""
    now = now or datetime.now(timezone.utc)
    cands = [n for n in PROVIDER_NAMES if (n in names if names is not None else _connected(r, n))]
    if names is not None:
        cands += [n for n in names if n not in cands]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    named = str(_get(r, "pos_system") or "").strip().lower()
    if named in cands:
        return named
    best = None
    for name in cands:
        stamp = parse_stamp(_get(r, f"{name}_last_synced"))
        age = None
        if stamp is not None:
            age = (now - stamp).total_seconds() / 86400.0
            if age < -FUTURE_TOLERANCE_DAYS:
                age = None
        rank = (age is not None, -(age or 0.0))
        if best is None or rank > best[0]:
            best = (rank, name)
    return best[1]


def pos_sync_state(r, now=None) -> dict:
    """{provider, connected, last_synced, last_synced_iso, age_days, error,
    state, pct, future} for the restaurant's POS. state: not_connected |
    error | current | aging | stale | unknown, from age_state (current while
    the registry's pos row reads ≥ 80% — about 2.9 days after the sync;
    aging to 50%, 5 days; stale after). With credentials for several
    providers the one with the freshest stamp is read, so a restaurant that
    moved from Toast to RPOWER is not judged on a dead Toast stamp
    (re-audit B3#16). Never raises."""
    now = now or datetime.now(timezone.utc)
    out = {"provider": None, "connected": False, "last_synced": None, "last_synced_iso": None,
           "age_days": None, "error": None, "state": "not_connected",
           "pct": None, "future": False, "unreadable": False}
    try:
        # The primary provider (primary_provider): the owner's recorded POS,
        # else the freshest stamp — the same one the nightly sync uses.
        name = primary_provider(r, now=now)
        return provider_state(r, name, now=now) if name else out
    except Exception as e:
        print(f"[pos_health] unreadable: {e}")
        out["state"] = "unknown"
    return out


# ── the fleet's POS, as one reading (scale audit 10/19/26 OPS-6) ────────────
#
# A provider outage, or a token RPOWER revoked for every store, used to be one
# "RPOWER POS needs attention" console row per store and no page. The fleet
# reading names the share of connected stores failing per provider; past
# FLEET_OUTAGE_SHARE (and at least FLEET_OUTAGE_MIN_STORES stores, so one
# store's own trouble stays that store's issue) it is one outage: one
# platform issue in the console (admin_ops) and one page
# (ops.check_platform_sla).
import os as _os

FLEET_OUTAGE_SHARE = float(_os.getenv("POS_FLEET_OUTAGE_SHARE", "0.05"))
FLEET_OUTAGE_MIN_STORES = int(_os.getenv("POS_FLEET_OUTAGE_MIN_STORES", "2"))


def _fleet(rows):
    """{provider: {connected, failing: [row], share, outage}} from (provider,
    failing, row) triples."""
    out = {}
    for prov, failing, row in rows:
        f = out.setdefault(prov, {"connected": 0, "failing": [], "share": 0.0, "outage": False})
        f["connected"] += 1
        if failing:
            f["failing"].append(row)
    for f in out.values():
        n = len(f["failing"])
        f["share"] = round(n / float(f["connected"]), 3) if f["connected"] else 0.0
        f["outage"] = n >= FLEET_OUTAGE_MIN_STORES and f["share"] > FLEET_OUTAGE_SHARE
    return out


def fleet_from_records(recs) -> dict:
    """The fleet reading from the admin console's records (customer
    locations that are not ended or paused): a store failing is one whose
    POS integration row reads `error`."""
    rows = []
    for rec in recs or []:
        if rec.get("segment") != "customer" or (rec.get("billing") or {}).get("status") in (
                "churned", "canceled", "cancelled", "paused"):
            continue
        for i in rec.get("integrations") or []:
            if i.get("key") in PROVIDER_NAMES and i.get("configured"):
                rows.append((i["key"], i.get("state") == "error", {"id": rec.get("id"), "name": rec.get("name")}))
    return _fleet(rows)


def fleet_state(restaurants=None, now=None) -> dict:
    """The fleet reading from the restaurants table (every live, non-demo
    restaurant's primary POS through pos_sync_state). Never raises."""
    try:
        if restaurants is None:
            from models import get_all_restaurants, in_service
            restaurants = [r for r in get_all_restaurants() if in_service(r)]
        rows = []
        for r in restaurants:
            if int(_get(r, "is_demo") or 0):
                continue
            st = pos_sync_state(r, now=now)
            if st.get("connected"):
                rows.append((st["provider"], st["state"] == "error",
                             {"id": _get(r, "id"), "name": _get(r, "name")}))
        return _fleet(rows)
    except Exception as e:
        print(f"[pos_health] fleet unreadable: {e}")
        return {}
