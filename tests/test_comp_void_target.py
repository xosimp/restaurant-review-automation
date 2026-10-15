"""Comps & voids against the owner's target (Simple EJ's, 10/15/26: Erik's
2%). The "given away" line counted discounts too — $479 of the night's $510 —
so comps and voids are their own figure, judged against
restaurants.comp_void_target_pct, discounts left out."""
import pathlib

import models
from dsr.block_service import comp_void

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _facts(comps=30.75, voids=0.0, gross=7686.91, discounts=479.24):
    return {"blocks": {
        "sales": {"metrics": {"gross_items": gross, "comps": comps, "discounts": discounts}},
        "service": {"detail": {"loss": {"given": {"total": comps + discounts, "by_reason": []},
                                        "voids": {"total": voids}}}}}}


def test_comps_and_voids_are_judged_without_discounts():
    got = comp_void(_facts(), 2)
    assert got["total"] == 30.75 and got["pct_of_gross"] == 0.4      # not the 6.6% given away
    assert got["target"] == 2.0 and got["over"] is False
    over = comp_void(_facts(comps=200, voids=80, gross=8000), 2)
    assert over["pct_of_gross"] == 3.5 and over["over"] is True
    assert comp_void(_facts(), None)["over"] is None                  # no target: no verdict
    assert comp_void(_facts(gross=0), 2) is None                      # no gross: no figure, never 0%


def test_the_target_is_a_restaurant_column_on_every_touch_point():
    src = (ROOT / "models.py").read_text()
    assert "comp_void_target_pct: Optional[float]" in src
    assert '("restaurants", "comp_void_target_pct", "REAL")' in src
    assert '"comp_void_target_pct",' in src.split("allowed = {", 1)[1][:6000] or \
        '"comp_void_target_pct"' in src[src.index("def update_restaurant"):][:8000]
    assert 'comp_void_target_pct=row["comp_void_target_pct"]' in src
    routes = (ROOT / "strategy_routes.py").read_text()
    assert '"comp_void_target_pct": (0.1, 25.0)' in routes and '"wine_cost_target", "comp_void_target_pct")' in routes
    # The figure behind it is LOSS_VIEW's, so is its target.
    get = routes[routes.index("def _do_targets_get"):routes.index("def _do_targets_set")]
    assert "_loss_viewer(u)" in get and 't["comp_void_target_pct"] = None' in get


def test_the_target_saves_and_reads_back(tmp_path):
    db = str(tmp_path / "t.db")
    models.init_db(db)
    rid = models.create_restaurant(models.Restaurant(name="Bar", owner_email="bar@example.com"), db_path=db)
    models.update_restaurant(rid, {"comp_void_target_pct": 2.0}, db_path=db)
    assert models.get_restaurant(rid, db_path=db).comp_void_target_pct == 2.0


def test_the_card_and_the_targets_page_carry_it():
    html = (ROOT / "templates" / "dashboard.html").read_text()
    assert 'data-tg="comp_void_target_pct"' in html and "comp_void_target_pct: 1}" in html
    loss = html[html.index("function lossHtml(svc)"):html.index("function registerHtml(")]
    assert "L.comp_void" in loss and "Comps &amp; voids" in loss and "your '" in loss
