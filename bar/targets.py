"""
bar/targets.py — the owner's own alcohol cost targets (Account → Targets).
"""
import models as _models


def owner_targets(restaurant_id, db_path=None) -> dict:
    """{None: overall, "beer": .., "liquor": .., "wine": ..} — the owner's own
    alcohol cost targets (models.BAR_TARGET_FIELDS), each None when unset."""
    try:
        r = _models.get_restaurant(restaurant_id, db_path=db_path) if db_path else _models.get_restaurant(restaurant_id)
    except Exception:
        r = None
    out = {}
    for cat, field in _models.BAR_TARGET_FIELDS.items():
        v = getattr(r, field, None) if r is not None else None
        try:
            out[cat] = float(v) if v is not None else None
        except (TypeError, ValueError):
            out[cat] = None
    return out
