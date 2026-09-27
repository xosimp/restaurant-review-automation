"""Owner round, 9/26/26 night: the inventory system seam (Back Office
first), Google's public rating as the owner's rating, the What-changed date,
Re-run AI visibility, the supplier send table and its modal, the prices
save's failure path, and the Food Cost headers."""
import os
import sys
import types
from datetime import date

import pytest

SRC = open("templates/dashboard.html", encoding="utf-8").read()


def _between(a, b):
    i = SRC.index(a)
    return SRC[i:SRC.index(b, i)]


# ── the inventory system seam ───────────────────────────────────────────────

@pytest.fixture
def fake_provider(db_path, monkeypatch):
    import models
    import inventory_sync
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    rid = models.create_restaurant(models.Restaurant(name="Sync Co", owner_email="s@x.test", module_inventory=1),
                                   db_path=db_path)
    conn = real(db_path)
    conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, par_level) VALUES (?,?,?,?,?)",
                 (rid, "Olive Oil", "bottle", 12.0, 8.0))
    conn.commit()
    conn.close()
    mod = types.ModuleType("fake_inv_provider")
    mod.label = "Fake Office"
    mod.state = {"connected": True, "payload": {}, "raise": None}
    mod.is_connected = lambda r: mod.state["connected"] and r == rid
    mod.connected_ids = lambda: [rid] if mod.state["connected"] else []

    def fetch(r):
        if mod.state["raise"]:
            raise mod.state["raise"]
        return mod.state["payload"]
    mod.fetch_inventory = fetch
    monkeypatch.setitem(sys.modules, "fake_inv_provider", mod)
    monkeypatch.setattr(inventory_sync, "PROVIDERS", {"fake": "fake_inv_provider"})
    return rid, mod, real, db_path


def test_a_sync_fills_food_costs_own_tables_once(fake_provider):
    import inventory_sync
    rid, mod, real, db_path = fake_provider
    today = date.today().isoformat()
    mod.state["payload"] = {
        "items": [{"name": "olive  oil", "unit_cost": 14.5, "par_level": 10, "ref": "BO-1",
                   "supplier_name": "Sysco", "supplier_email": "o@sysco.test"},
                  {"name": "Pizza Dough Balls", "unit": "each", "unit_cost": 0.85, "par_level": 120, "ref": "BO-2"}],
        "counts": [{"ref": "BO-1", "qty": 6, "counted_on": today},
                   {"name": "Pizza Dough Balls", "qty": 90, "counted_on": today},
                   {"name": "Unknown", "qty": 3, "counted_on": today}, {"ref": "BO-2", "qty": -1, "counted_on": today}],
    }
    assert not inventory_sync.status(rid)["synced"]          # connected, nothing landed yet
    out = inventory_sync.sync_restaurant(rid)
    assert out["ok"] and out["created"] == 1 and out["counts"] == 2
    conn = real(db_path)
    rows = {r["name"]: dict(r) for r in conn.execute(
        "SELECT name, unit_cost, par_level, supplier_email, external_ref, current_stock FROM ingredients "
        "WHERE restaurant_id=?", (rid,)).fetchall()}
    conn.close()
    assert rows["Olive Oil"]["unit_cost"] == 14.5 and rows["Olive Oil"]["external_ref"] == "BO-1"
    assert rows["Olive Oil"]["supplier_email"] == "o@sysco.test" and rows["Olive Oil"]["current_stock"] == 6
    assert rows["Pizza Dough Balls"]["current_stock"] == 90
    st = inventory_sync.status(rid)
    assert st["synced"] and st["label"] == "Fake Office" and st["synced_at"]
    # The same night twice writes no second count.
    assert inventory_sync.sync_restaurant(rid)["counts"] == 0


def test_a_failed_fetch_is_said_and_writes_nothing(fake_provider):
    import inventory_sync
    rid, mod, real, db_path = fake_provider
    mod.state["raise"] = RuntimeError("401 from the provider")
    out = inventory_sync.sync_restaurant(rid)
    assert not out["ok"] and "401" in out["error"]
    st = inventory_sync.status(rid)
    assert not st["synced"] and "401" in st["error"]


def test_back_office_is_not_connected_until_its_api_exists(monkeypatch):
    import backoffice
    import credentials
    monkeypatch.delenv("BACKOFFICE_API_BASE", raising=False)
    assert backoffice.connected_ids() == [] and backoffice.is_connected(1) is False
    with pytest.raises(backoffice.BackOfficeNotReady):
        backoffice.fetch_inventory(1)
    assert "backoffice_api_key" in credentials.FIELDS                 # encrypted at rest
    sched = open("scheduler.py", encoding="utf-8").read()
    assert "        run_inventory_sync()\n" in sched and "def run_inventory_sync():" in sched


def test_the_page_steps_aside_once_a_sync_lands():
    assert "{% set _fc_synced = inv_sync and inv_sync.synced %}" in SRC
    assert '<div class="fc2-submit"{% if _fc_synced %} hidden{% endif %}>' in SRC
    count = _between("function fc2LoadCountSheet(){", "function fc2LogWaste(btn){")
    assert "var src=d.source||{},synced=!!src.synced" in count and "(synced?' readonly':'')" in count
    assert "h+='</div>'+(synced?'':'<div class=\"fc2-count-ft\">" in count
    sup = _between("function fc2LoadSuppliers(){", "function fc2SupplierPost(")
    assert "(synced?' disabled':'')" in sup
    routes = open("strategy_routes.py", encoding="utf-8").read()
    assert '"ledger_mark": int(mark or 0), "source": source}' in routes


# ── Google's public rating ──────────────────────────────────────────────────

def test_the_owners_rating_is_googles_public_one(db_path, monkeypatch):
    import models
    import competitor
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    rid = models.create_restaurant(models.Restaurant(name="Rated", owner_email="r@x.test",
                                                     google_place_id="PLACE123"), db_path=db_path)
    competitor._remember_own_listing("PLACE123", ["bar"], 2, 4.4, 812)
    r = models.get_restaurant(rid, db_path)
    assert r.gbp_rating == 4.4 and r.gbp_review_count == 812
    conn = real(db_path)
    assert conn.execute("SELECT gbp_rating_updated_at FROM restaurants WHERE id=?", (rid,)).fetchone()[0]
    conn.close()
    comp = open("competitor.py", encoding="utf-8").read()
    assert '"fields": "geometry,name,vicinity,types,price_level,rating,user_ratings_total"' in comp
    # Neither the web nor the phone passes the imported reviews' average in.
    hd = open("hosted_dashboard.py", encoding="utf-8").read()
    assert "_cif.own_rating(getattr(restaurant, \"gbp_rating\", None),\n                                     getattr(restaurant, \"gbp_review_count\", None))" in hd
    mob = open("mobile_api.py", encoding="utf-8").read()
    assert '_own = _own_rating(getattr(restaurant, "gbp_rating", None), getattr(restaurant, "gbp_review_count", None))' in mob
    assert "your imported reviews' average" not in SRC


# ── Intel ───────────────────────────────────────────────────────────────────

def test_a_stored_instant_is_the_restaurants_day():
    from time_utils import local_iso
    assert local_iso("2026-09-27T00:35:12+00:00", "America/Chicago") == "2026-09-26"
    assert local_iso("2026-09-27 00:35:12", "America/Chicago") == "2026-09-26"
    assert local_iso(None) == ""
    hd = open("hosted_dashboard.py", encoding="utf-8").read()
    assert "@app.template_filter('local_iso')" in hd and "return local_iso(value, tz)" in hd


def test_what_changed_is_one_line_and_ai_visibility_reruns_from_the_top():
    assert "<div class=\"in2-upd\" style=\"margin-top:4px\"><span>'+sub+'</span></div>'" in SRC
    assert 'onclick="aivRerunFromTop()" class="cbtn cbtn-secondary">Re-run AI visibility</button>' in SRC
    fn = _between("function aivRerunFromTop() {", "\n}\n")
    assert "window.scrollTo(" in fn and "runAIVisibility(btn)" in fn


# ── Food Cost ───────────────────────────────────────────────────────────────

def test_the_supplier_send_table_holds_its_shape_and_confirms_in_a_modal():
    assert "table class=\"fc2-mt fc2-so-t\"><colgroup>" in SRC and ".fc2-so-t{table-layout:fixed}" in SRC
    assert "var SO_MAX_QTY = 9999;" in SRC and "if(q>SO_MAX_QTY){t.value=SO_MAX_QTY;}" in SRC
    assert ".fc2-send .fc2-sup-order .g{border-bottom:none" in SRC
    assert 'id="so-confirm-modal" class="cmodal so-modal"' in SRC and "cModal.open('so-confirm-modal')" in SRC
    assert "cf.innerHTML='Email <b>'" not in SRC


def test_a_failed_prices_save_says_why_and_retries_a_restart_once():
    fn = _between("function submitFoodCostCount(_retried) {", "\nfunction clientUpload(")
    assert "if(!_retried&&_st>=502&&_st<=504)" in fn and "submitFoodCostCount(true)" in fn
    assert "'Error — try again'" not in fn and "msg.textContent" not in fn


def test_food_cost_actions_sit_level_with_their_titles():
    assert "#panel-inventory .fc2-sec .hd{align-items:flex-start}" in SRC
    assert ".fc2-allprices>.fc2-sec{margin-top:10px;padding-top:0}" in SRC
