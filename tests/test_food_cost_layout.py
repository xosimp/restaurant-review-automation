"""Food Cost's head: full-width position tiles and working blocks, the
obsidian tile, the web supplier control, and the price monitor opening on
the pantry.

The food cost card, recipes block and count sheet used to be children of
the hero's left flex column — squeezed beside the pulse chips into a
340px strip down the left of a 1,400px page.
"""
import os
import sqlite3

import pytest

import models

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    return open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def test_the_position_and_work_blocks_are_outside_the_hero_column():
    """Density round (#7, #8, #43): food cost % is the hero figure, in the
    header's right column; the CFO card is the first "why" block after the
    header; the working blocks are one-line rows in "This week's work",
    after the why, each keeping its section id and data-nav."""
    s = _src()
    panel = s[s.index('id="panel-inventory"'):]
    top = panel.index('<div class="hb-top">')
    left = panel[top:panel.index('<div class="fc2-top-right">', top)]
    for i in ("fc2-fcp", "fc2-cov", "fc2-wsrc", "fc2-recipes", "fc2-count", "fc2-cfo"):
        assert f'id="{i}"' not in left, i + " is still inside the hero's left column"
    hero = panel[panel.index('<div class="fc2-hero-nums">'):panel.index('<div class="fc2-position">')]
    assert '<div id="fc2-fcp" class="fc2-fcp fc2-pos fc2-hero-fcp" hidden></div>' in hero
    assert '<div id="fc2-cov" class="fc2-cov fc2-pos" hidden></div>' in panel
    order = [panel.index(m) for m in ('<div class="fc2-hero-nums">', '<div id="fc2-cfo" hidden></div>',
                                      'aria-label="Waste trend"', 'id="inv-insight"', 'id="fc2-work"')]
    assert order == sorted(order), "position, then why, then this week's work"
    for i in ("fc2-recipes", "fc2-count", "fc2-suppliers"):
        assert f'<section id="{i}" class="fc2-block"' in panel, i
        k = i.split("-")[1]
        row = panel[panel.index(f'id="fc2-work-{k}"'):]
        assert row.index(f'<section id="{i}"') < row.index("</details>"), i + " sits inside its row"
    assert 'data-nav="inventory/count"' in panel[panel.index('id="fc2-work-count"'):panel.index('id="fc2-work-suppliers"')]
    for rule in (".fc2-position{", ".fc2-pos{", ".fc2-block{", ".fc2-bh{", ".fc2-sup-grid{"):
        assert rule in s, rule


def test_the_obsidian_tile_is_the_web_twin_of_glowbadge():
    s = _src()
    tile = s[s.index(".ob-tile{"):s.index(".ob-tile svg{")]
    assert "#2c2c2e" in tile and "#161618" in tile, "the two obsidian stops GlowBadge uses"
    assert "mask-composite" in tile, "the lit edge is a masked gradient ring"
    assert ".ob-tile:after{" in tile, "the ember seated on the right edge"
    # every working block and both Labor marks use it
    assert s.count('<span class="ic ob-tile" aria-hidden="true">') == 2
    assert "function fc2BlockHead(icon,kicker,title,cnt,sub)" in s
    assert '<span class="ob-tile" aria-hidden="true">' in s
    assert ".lb2-op .ic{background" not in s and "glow-ember)}" not in s[s.index(".lb2-op .ic{"):s.index(".lb2-op .ic{") + 80]


def test_the_web_can_assign_suppliers():
    s = _src()
    assert "function fc2LoadSuppliers()" in s and "function fc2SupplierPick(sel)" in s
    assert "function fc2AssignAllUnassigned(btn)" in s
    assert "/api/food-cost/ingredient-supplier" in s
    assert "fc2LoadCountSheet();fc2LoadRecipeDrafts();fc2LoadSuppliers();" in s


def test_the_count_sheet_carries_the_supplier_for_the_web_block(db_path, monkeypatch):
    import strategy_routes
    monkeypatch.setattr(models, "DB_PATH", db_path)
    # inventory_ledger.list_ingredients calls get_conn() bare; its default
    # was bound to the real DB_PATH at import, so point the function itself.
    real_get_conn = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda p=None: real_get_conn(p or db_path))
    from models import Restaurant
    rid = models.create_restaurant(Restaurant(name="Sup", owner_email="o@x.test", module_inventory=1), db_path=db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, supplier_name, supplier_email) VALUES (?,?,?,?,?)",
                 (rid, "Butter", "lb", "Sysco", "orders@sysco.example"))
    conn.commit(); conn.close()
    monkeypatch.setattr(strategy_routes, "_sees_food", lambda u: True)
    out, code = strategy_routes._do_count_sheet_get({"restaurant_id": rid, "id": 1})
    assert code == 200 and out["items"][0]["supplier_email"] == "orders@sysco.example"
    assert out["items"][0]["supplier_name"] == "Sysco"


def test_the_price_monitor_reads_the_pantry_for_a_ledger_account():
    """This used to pin "never over a real submission". Friction audit
    U2-14 changed it on purpose: for a ledger account the rows always come
    from the ingredient list (invoices keep prices current, usage comes from
    sales), read-only until Edit, and the typed tracker is only an override."""
    src = open(os.path.join(ROOT, "hosted_dashboard.py"), encoding="utf-8").read()
    block = src[src.index('"from_pantry": True'):]
    guard = src[src.index("# With a live pantry"):src.index('"from_pantry": True')]
    assert "list_ingredients(rid)" in guard
    assert '"price": (round(float(r["unit_cost"]), 2)' in block
    assert 'not (_food_cost_data and (_food_cost_data.get("current") or {}).get("items"))' not in guard
    assert "Candidate for future cleanup after additional verification".lower() in guard.lower().replace("\n    # ", " ")
    s = _src()
    tracker = s[s.index('id="fc2-tracker"') - 400:s.index('<span id="fc-tab-tracker">')]
    assert "{% set _fc_ledger = food_cost_data and food_cost_data.current and food_cost_data.current.from_pantry %}" in tracker
    assert "{{ ' readonly' if _fc_ledger }}" in tracker and 'onclick="fcEditPrices(this)"' in tracker
    assert "and not _fc_ledger %}" in tracker, "no drift between a ledger read and an old typed week"
    assert ".fc2-ledger-rows:not(.editing) .fc2-submit{display:none}" in s
