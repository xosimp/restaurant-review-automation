"""scheduler.py's nightly ingredient depletion job — must only process
Toast-connected restaurants that actually have a recipe configured, and one
restaurant's failure must never stop the rest (same isolation contract as
pos.sync_all(), which the labor sync already relies on)."""
import pytest

import models
import scheduler
from models import create_restaurant, Restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    monkeypatch.setattr(models, "get_conn", redirect)


def _restaurant_with_recipe(db_path, name, toast_client_id=None):
    import inventory_ledger
    rid = create_restaurant(
        Restaurant(name=name, owner_email=f"{name}@x.com", module_inventory=1,
                   toast_client_id=toast_client_id, toast_client_secret="secret" if toast_client_id else None,
                   toast_restaurant_guid="guid-rest" if toast_client_id else None),
        db_path=db_path
    )
    ingredient_id = inventory_ledger.create_ingredient(rid, name="Butter", current_stock=10)
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO menu_items (restaurant_id, toast_guid, name) VALUES (?,?,?)",
                       (rid, "guid-item", "Grilled Cheese"))
    menu_item_id = cur.lastrowid
    conn.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
                (menu_item_id, ingredient_id, 0.1))
    conn.commit()
    conn.close()
    return rid, ingredient_id


def _restaurant_without_recipe(db_path, name):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name}@x.com", module_inventory=1), db_path=db_path)


def test_skips_restaurants_with_no_recipe_configured(db_path, monkeypatch):
    import inventory_ledger
    connected_rid, ingredient_id = _restaurant_with_recipe(db_path, "Connected Co", toast_client_id="demo")
    _restaurant_without_recipe(db_path, "No Recipe Co")

    monkeypatch.setattr("toast.is_connected", lambda rid: True)
    monkeypatch.setattr("toast.fetch_business_days", lambda rid, start, end: {"2026-08-20": 500.0})
    calls = []
    def _fake_depletion(rid, bd):
        calls.append(rid)
        return {"ingredients_updated": 0, "unmapped_selections": []}
    monkeypatch.setattr(inventory_ledger, "compute_daily_depletion", _fake_depletion)

    scheduler.run_daily_depletion_sync()

    assert calls == [connected_rid]  # only the recipe-configured restaurant was touched


def test_skips_restaurants_with_recipe_but_toast_disconnected(db_path, monkeypatch):
    import inventory_ledger
    rid, _ = _restaurant_with_recipe(db_path, "Disconnected Co", toast_client_id=None)

    monkeypatch.setattr("toast.is_connected", lambda r: False)
    calls = []
    monkeypatch.setattr(inventory_ledger, "compute_daily_depletion",
                        lambda r, bd: calls.append(r) or {"ingredients_updated": 0, "unmapped_selections": []})

    scheduler.run_daily_depletion_sync()
    assert calls == []


def test_one_restaurant_failure_does_not_stop_the_rest(db_path, monkeypatch):
    import inventory_ledger
    rid_a, _ = _restaurant_with_recipe(db_path, "Broken Co", toast_client_id="demo")
    rid_b, _ = _restaurant_with_recipe(db_path, "Healthy Co", toast_client_id="demo")

    monkeypatch.setattr("toast.is_connected", lambda rid: True)
    monkeypatch.setattr("toast.fetch_business_days", lambda rid, start, end: {"2026-08-20": 500.0})

    processed = []
    def _fake_depletion(rid, bd):
        if rid == rid_a:
            raise RuntimeError("malformed recipe data")
        processed.append(rid)
        return {"ingredients_updated": 0, "unmapped_selections": []}
    monkeypatch.setattr(inventory_ledger, "compute_daily_depletion", _fake_depletion)

    scheduler.run_daily_depletion_sync()  # must not raise
    assert processed == [rid_b]


def test_unmapped_selections_are_logged_not_silently_dropped(db_path, monkeypatch, caplog):
    import inventory_ledger, logging
    rid, _ = _restaurant_with_recipe(db_path, "Unmapped Co", toast_client_id="demo")

    monkeypatch.setattr("toast.is_connected", lambda r: True)
    monkeypatch.setattr("toast.fetch_business_days", lambda r, start, end: {"2026-08-20": 500.0})
    monkeypatch.setattr(inventory_ledger, "compute_daily_depletion",
                        lambda r, bd: {"ingredients_updated": 0,
                                      "unmapped_selections": [{"toast_guid": "mystery"}]})

    with caplog.at_level(logging.WARNING):
        scheduler.run_daily_depletion_sync()
    assert any("unmapped" in rec.message.lower() for rec in caplog.records)


# ── check_stale_inventory: ledger-aware freshness ────────────────────────

def test_stale_check_uses_ledger_freshness_for_migrated_restaurants(db_path, monkeypatch):
    """A migrated restaurant's client_data.updated_at never changes again —
    the check must look at ingredients.updated_at instead, or every
    ledger-driven restaurant would report 'never uploaded' forever."""
    import inventory_ledger
    from datetime import datetime, timedelta

    monkeypatch.setattr(scheduler, "RESEND_API_KEY", "fake-key")
    sent = {}
    monkeypatch.setattr("resend.Emails.send", lambda payload: sent.update(payload))

    fresh_rid, fresh_iid = _restaurant_with_recipe(db_path, "Fresh Ledger Co", toast_client_id="demo")
    stale_rid, stale_iid = _restaurant_with_recipe(db_path, "Stale Ledger Co", toast_client_id="demo")

    # Backdate the stale restaurant's ingredient rows so it looks untouched
    # for a while — the fresh one keeps its just-created updated_at.
    conn = get_conn(db_path)
    old = (datetime.now() - timedelta(days=10)).isoformat()
    conn.execute("UPDATE ingredients SET updated_at=? WHERE restaurant_id=?", (old, stale_rid))
    conn.commit()
    conn.close()

    monkeypatch.setattr("toast.is_connected", lambda rid: True)
    scheduler.check_stale_inventory()

    html = sent.get("html", "")
    assert "Stale Ledger Co" in html
    assert "Fresh Ledger Co" not in html


def test_stale_check_flags_disconnected_toast_restaurant_explicitly(db_path, monkeypatch):
    from datetime import datetime, timedelta

    monkeypatch.setattr(scheduler, "RESEND_API_KEY", "fake-key")
    sent = {}
    monkeypatch.setattr("resend.Emails.send", lambda payload: sent.update(payload))

    rid, iid = _restaurant_with_recipe(db_path, "Unplugged Co", toast_client_id="demo")
    conn = get_conn(db_path)
    old = (datetime.now() - timedelta(days=10)).isoformat()
    conn.execute("UPDATE ingredients SET updated_at=? WHERE restaurant_id=?", (old, rid))
    conn.commit()
    conn.close()

    monkeypatch.setattr("toast.is_connected", lambda r: False)
    scheduler.check_stale_inventory()

    assert "Toast disconnected" in sent.get("html", "")
