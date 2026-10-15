"""
bar/targets.py — the owner's own alcohol cost targets (Account → Targets),
read through thresholds.bar_target_for: the value (0 or less, 100 or more,
or not a number is unset — V18), and who set it, so "your target" is said
only when an account holder set it (R9).
"""
import models as _models
import thresholds as _thr

# Bar page category (None = overall) -> thresholds kind.
_KIND = {None: "bar", "beer": "beer", "liquor": "liquor", "wine": "wine"}


def _restaurant(restaurant_id, db_path=None):
    try:
        return (_models.get_restaurant(restaurant_id, db_path=db_path) if db_path
                else _models.get_restaurant(restaurant_id))
    except Exception as e:
        print(f"[bar.targets] restaurant {restaurant_id} unreadable: {e}")
        return None


def target_details(restaurant_id, db_path=None, restaurant=None) -> dict:
    """{None: overall, "beer": .., "liquor": .., "wine": ..}, each
    thresholds.bar_target_for's {pct, source, setter, label, phrase}."""
    r = restaurant if restaurant is not None else _restaurant(restaurant_id, db_path)
    return {cat: _thr.bar_target_for(r, kind) for cat, kind in _KIND.items()}


def owner_targets(restaurant_id, db_path=None) -> dict:
    """{None: overall, "beer": .., "liquor": .., "wine": ..} — the owner's own
    alcohol cost targets (models.BAR_TARGET_FIELDS) as numbers, each None
    when unset."""
    return {cat: t["pct"] for cat, t in target_details(restaurant_id, db_path).items()}
