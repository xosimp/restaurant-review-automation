"""
backoffice.py — Back Office (Buyers Edge Platform) as an inventory provider
for inventory_sync.py.

Status (9/26/26): Buyers Edge has not published its API (research note:
docs/plans/BACK_OFFICE_INTEGRATION.md — access is by partner arrangement).
Everything on Cavnar AI's side is in place: inventory_sync writes whatever
`fetch_inventory` returns into ingredients and the stock ledger, stamps the
sync, runs nightly with the POS job, and the Food Cost page steps its hand
entry aside once a sync lands. What is missing is only the HTTP calls in
`fetch_inventory`, written against the real documentation when the
credentials arrive — until then `is_connected` is False for every restaurant
(it also needs BACKOFFICE_API_BASE), so nothing calls it.

Credentials: restaurants.backoffice_api_key and backoffice_account_id.
"""
import os

label = "Back Office"


class BackOfficeNotReady(RuntimeError):
    """The adapter's HTTP calls are not written yet."""


def _api_base():
    return (os.getenv("BACKOFFICE_API_BASE") or "").strip()


def is_connected(restaurant_id: int) -> bool:
    if not _api_base():
        return False
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    return bool(r and (getattr(r, "backoffice_api_key", None) or "").strip())


def connected_ids() -> list:
    if not _api_base():
        return []
    from models import get_conn
    conn = get_conn()
    try:
        return [r["id"] for r in conn.execute(
            "SELECT id FROM restaurants WHERE COALESCE(backoffice_api_key,'')<>''").fetchall()]
    finally:
        conn.close()


def fetch_inventory(restaurant_id: int) -> dict:
    """{"items": [...], "counts": [...]} in inventory_sync's shape.

    To write when Buyers Edge grants access, mapping their records to:
      items  — item master: name, unit (their count unit), category,
               unit_cost (latest invoice price), par_level, case_size,
               supplier_name/email (vendor), ref (their item id)
      counts — each item's on-hand from their latest inventory count, with
               the count's business date as counted_on
    Page through every list (a truncated page must fail the sync, never
    store part of a count as the whole), name a timeout on every call
    (scripts/check_timeouts.py), and archive the raw response locally the
    way rpower.py does."""
    raise BackOfficeNotReady("Back Office's API calls are not written yet — see backoffice.py.")
