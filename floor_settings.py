"""
floor_settings.py — the owner's switches for the schedule on the floor
(schedule audit 10/10/26, workstream J2), one JSON column
(restaurants.floor_settings_json):

  pos_push     — write each published week to the POS's time clock, where it
                 enforces early clock-ins and late clock-outs
                 (pos_schedule_push). OFF until the owner turns it on after
                 seeing a preview.
  live_moves   — the live labor read suggests legal, priced cuts, extensions
                 and call-ins during service (labor_live). ON: a suggestion
                 changes nothing by itself.
  autopilot    — a live move the manager has approved often enough, and never
                 reversed, may be offered to the staff member without the
                 manager's tap (labor_live, earned autonomy). OFF.
  live_target_margin_pts — how far the projected day's labor % may run over
                 the target before a cut is suggested.

Every write goes through update_restaurant, which records it in the
change log ("Schedule on the floor").
"""
import json

DEFAULTS = {"pos_push": False, "pos_push_on_at": None, "pos_push_on_by": None,
            "live_moves": True, "autopilot": False, "live_target_margin_pts": 1.0}
_BOOLS = ("pos_push", "live_moves", "autopilot")
_BOUNDS = {"live_target_margin_pts": (0.0, 10.0)}


class SettingsError(ValueError):
    pass


def get(restaurant) -> dict:
    """The restaurant's switches over DEFAULTS. Never raises."""
    out = dict(DEFAULTS)
    raw = getattr(restaurant, "floor_settings_json", None) if restaurant is not None else None
    try:
        stored = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        return out
    for k in _BOOLS:
        if isinstance(stored.get(k), bool):
            out[k] = stored[k]
    for k in ("pos_push_on_at", "pos_push_on_by"):
        if isinstance(stored.get(k), str):
            out[k] = stored[k]
    for k, (lo, hi) in _BOUNDS.items():
        try:
            if stored.get(k) is not None:
                out[k] = max(lo, min(hi, float(stored[k])))
        except (TypeError, ValueError):
            pass
    return out


def get_for(restaurant_id, db_path=None) -> dict:
    try:
        import models
        return get(models.get_restaurant(restaurant_id, db_path) if db_path else models.get_restaurant(restaurant_id))
    except Exception:
        return dict(DEFAULTS)


def save(restaurant_id, data: dict, user=None, db_path=None) -> dict:
    """Merge `data` (only the keys DEFAULTS knows, each checked) and store
    it. Turning pos_push on stamps when and by whom. Returns the settings."""
    from datetime import datetime
    if not isinstance(data, dict):
        raise SettingsError("send the settings as an object")
    new = get_for(restaurant_id, db_path)
    for k in _BOOLS:
        if k in data:
            if not isinstance(data[k], bool):
                raise SettingsError(f"{k} is true or false")
            if k == "pos_push" and data[k] and not new["pos_push"]:
                new["pos_push_on_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                who = (user or {}).get("username") or (user or {}).get("email") if isinstance(user, dict) else None
                new["pos_push_on_by"] = (str(who)[:120] if who else None)
            new[k] = data[k]
    for k, (lo, hi) in _BOUNDS.items():
        if k in data:
            try:
                v = float(data[k])
            except (TypeError, ValueError):
                raise SettingsError(f"{k} is a number")
            if not lo <= v <= hi:
                raise SettingsError(f"{k} is between {lo:g} and {hi:g}")
            new[k] = v
    from models import update_restaurant
    update_restaurant(restaurant_id, {"floor_settings_json": json.dumps(new)},
                      **({"db_path": db_path} if db_path else {}))
    return new
