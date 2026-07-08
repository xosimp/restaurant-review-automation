"""is_full_tier() — the "all 4 modules on" business rule that used to be
copy-pasted independently in scheduler.py (weekly competitor job),
hosted_dashboard.py (whether to load competitor_data), and dashboard.html
(whether to show the Intel tab). Centralized here so the rule can't drift
between copies if it ever changes."""
from models import is_full_tier, create_restaurant, Restaurant


def _restaurant(db_path, **modules):
    defaults = dict(module_reviews=0, module_labor=0, module_inventory=0, module_marketing=0)
    defaults.update(modules)
    return create_restaurant(Restaurant(name="Tier Test Co", owner_email="t@x.com", **defaults), db_path=db_path)


def test_all_four_modules_on_is_full_tier(db_path):
    from models import get_restaurant
    rid = _restaurant(db_path, module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1)
    r = get_restaurant(rid, db_path=db_path)
    assert is_full_tier(r) is True


def test_missing_any_one_module_is_not_full_tier(db_path):
    from models import get_restaurant
    combos = [
        dict(module_reviews=0, module_labor=1, module_inventory=1, module_marketing=1),
        dict(module_reviews=1, module_labor=0, module_inventory=1, module_marketing=1),
        dict(module_reviews=1, module_labor=1, module_inventory=0, module_marketing=1),
        dict(module_reviews=1, module_labor=1, module_inventory=1, module_marketing=0),
    ]
    for combo in combos:
        rid = _restaurant(db_path, **combo)
        r = get_restaurant(rid, db_path=db_path)
        assert is_full_tier(r) is False, f"expected not-full-tier for {combo}"


def test_none_restaurant_is_not_full_tier():
    assert is_full_tier(None) is False
