"""toast.sync_to_db()'s menu-item discovery hook — every Toast sync (manual
'Sync now' or nightly) should also refresh menu_items, not just shifts, so
new dishes show up in the recipe editor without a separate manual step."""
import pytest

import models
import toast
from models import create_restaurant, Restaurant, get_conn, update_restaurant


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    monkeypatch.setattr(models, "get_conn", redirect)


def _demo_restaurant(db_path):
    # toast_client_id/secret/restaurant_guid aren't part of create_restaurant()'s
    # INSERT (only set via the separate "save Toast credentials" flow) — set
    # them with update_restaurant() after creation, same as that real flow.
    rid = create_restaurant(Restaurant(name="Demo Sync Co", owner_email="demo@x.com"), db_path=db_path)
    update_restaurant(rid, {"toast_client_id": "demo", "toast_client_secret": "demo",
                            "toast_restaurant_guid": "demo"}, db_path=db_path)
    return rid


def test_sync_to_db_discovers_menu_items_in_demo_mode(db_path):
    """Demo mode is the cheap, HTTP-free way to exercise this — real Toast
    credentials would need a full API mock, but the discovery hook's own
    logic (fetch_business_days not being demo-aware) is exactly what this
    test would have caught before the fix in discover_menu_items()."""
    rid = _demo_restaurant(db_path)
    result = toast.sync_to_db(rid)
    assert result["ok"] is True

    conn = get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) AS n FROM menu_items WHERE restaurant_id=?", (rid,)).fetchone()["n"]
    conn.close()
    assert n == 4  # _demo_order_selections' fixed 4-item demo menu


def test_sync_to_db_still_succeeds_if_menu_discovery_fails(db_path, monkeypatch):
    """A menu-discovery bug must never break the labor sync it's piggybacking
    on — that's the business-critical part."""
    rid = _demo_restaurant(db_path)
    import inventory_ledger
    monkeypatch.setattr(inventory_ledger, "discover_menu_items",
                        lambda rid, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    result = toast.sync_to_db(rid)
    assert result["ok"] is True
