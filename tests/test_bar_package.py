"""The Bar page's code lives in the bar/ package (split from bar_cost.py
10/15/26); bar_cost stays the facade every caller imports. These hold the
facade to the names it always had and keep the package from importing it
back (a cycle)."""
import ast
import os

import bar_cost

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "bar")

# Every name bar_cost.py defined before the split (public and underscore):
# client_api, models.init_db and the tests reach the module by these.
FACADE = {
    "build", "classify", "product_key", "set_category", "init_bar_cost", "owner_targets", "display_name",
    "get_conn", "CATEGORIES", "LABELS", "POUR_CATEGORIES", "RULE_OF_THUMB", "WINDOW_DAYS",
    "PRICE_LOOKBACK_DAYS", "TREND_POINTS", "TREND_SAY_POINTS", "MAX_INSIGHTS", "_UOM_WORD",
    "_KEG_RE", "_FEE_RE", "_SUPPLY_RE", "_SELTZER_RE", "_NA_RE", "_WINE_RE", "_LIQUOR_RE", "_BEER_RE",
    "_BEER_HOUSES", "_SPIRIT_HOUSES", "_WINE_HOUSES", "_NA_HOUSES",
    "_mdy", "_money", "_lines", "_sales", "_window", "_gaps", "_judge", "_pour_cost", "_trend",
    "_price_watch", "_distributors", "_products", "_insights", "_models",
}

# Where each name lives now: the facade's name is the owning module's object.
OWNER = {
    "build": "build", "classify": "classify", "product_key": "classify", "set_category": "classify",
    "init_bar_cost": "classify", "owner_targets": "targets", "display_name": "common",
    "get_conn": "common", "_lines": "purchases", "_sales": "sales", "RULE_OF_THUMB": "pour_cost",
    "_pour_cost": "pour_cost", "_trend": "pour_cost", "_gaps": "pour_cost", "_window": "pour_cost",
    "_judge": "pour_cost", "_price_watch": "prices", "_distributors": "spend", "_products": "spend",
    "_insights": "insights", "LABELS": "common", "CATEGORIES": "common",
}


def test_bar_cost_re_exports_every_name_it_had():
    missing = sorted(n for n in FACADE if not hasattr(bar_cost, n))
    assert missing == [], f"bar_cost no longer re-exports {missing}"


def test_each_facade_name_is_the_owning_modules_object():
    import importlib
    for name, mod in OWNER.items():
        owner = importlib.import_module("bar." + mod)
        assert getattr(bar_cost, name) is getattr(owner, name), f"bar_cost.{name} is not bar.{mod}.{name}"


def test_the_public_values_are_unchanged():
    assert bar_cost.CATEGORIES == ("beer", "liquor", "wine", "na", "supplies", "fee", "deposit", "other")
    assert bar_cost.POUR_CATEGORIES == ("beer", "liquor", "wine")
    assert set(bar_cost.RULE_OF_THUMB) == set(bar_cost.POUR_CATEGORIES)
    assert set(bar_cost.LABELS) == set(bar_cost.CATEGORIES)
    assert bar_cost.classify("SOUTHERN GLAZERS", "TITOS VODKA 1L", "BO") == ("liquor", True)


def _imports(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{path}: use absolute imports (from bar.x import ...) — the " \
                                    "architecture test reads a relative import as intelligence/"
            out.add(node.module or "")
    return out


def test_the_package_never_imports_the_facade():
    files = [f for f in os.listdir(PKG) if f.endswith(".py")]
    assert {"__init__.py", "common.py", "classify.py", "purchases.py", "sales.py", "pour_cost.py",
            "targets.py", "prices.py", "spend.py", "insights.py", "build.py"} <= set(files)
    for f in files:
        imps = _imports(os.path.join(PKG, f))
        assert not any(i == "bar_cost" or i.startswith("bar_cost.") for i in imps), f"bar/{f} imports bar_cost"


def test_get_conn_resolves_through_models_at_call_time(monkeypatch):
    import models
    import bar.common
    seen = []

    def fake(*a):
        seen.append(a)
        raise RuntimeError("stop")

    monkeypatch.setattr(models, "get_conn", fake)
    try:
        bar_cost.set_category(1, "x|1", "beer", db_path="nowhere.db")
    except RuntimeError:
        pass
    assert seen == [("nowhere.db",)]
    assert bar.common.get_conn is bar_cost.get_conn


def test_alcohol_bought_mixed_counts_in_the_total_and_in_no_one_category():
    # Owner, 10/16/26: Binny's sells every kind of alcohol on one receipt — it
    # can't be called beer, wine or liquor.
    import backoffice_ap
    from bar import pour_cost as pc
    assert "alcohol_mixed" in backoffice_ap.CATEGORIES
    cats = {c: {"key": c, "label": c, "_bought": b, "_sold": s, "incomplete": False}
            for c, b, s in (("beer", 200.0, 1000.0), ("liquor", 150.0, 1000.0), ("wine", 100.0, 300.0))}
    t = pc._total(cats, mixed=50.0)
    assert t["purchases"] == 500.0 and t["mixed_alcohol"] == 50.0 and t["pct"] == round(500 / 2300 * 100, 1)
    assert pc._total(cats)["purchases"] == 450.0
